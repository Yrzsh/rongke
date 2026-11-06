# -*- coding: utf-8 -*-
"""示例插件：给荣珂加一个「规范化术语表」图型 + 一个文本统计工具。

这个文件同时是**文档**与**活测试** —— `plugins_test.py` 会把它装进
一个临时注册表跑一遍，确认插件系统端到端可用。

复制到 `~/.rongke/plugins/` 即可生效，然后：
    rongke --plugins          # 看台账
    rongke --tools            # 工具列表里会多出 word_count

──────────────────────────────────────────────────────────────────
插件作者要记住的三件事
──────────────────────────────────────────────────────────────────
1. **模块级 `PLUGIN` 变量**是入口。类或实例都行。
2. **`version` 必填**。出问题时人得知道是哪个版本。
3. **图型必须带 `template`**（绘图函数源码）。只登记元数据会被拒绝 ——
   因为那样 suggest 会推荐一个画不出来的图型。
"""
from __future__ import annotations

from pathlib import Path

from rongke.plugins import Plugin
from rongke.tools import Tool


class WordCountTool:
    """一个最小的只读工具：数一段文本的字数。

    ★ 只读、不写盘 ⇒ `mutating=False`。这个字段决定工作区围栏要不要管它；
      标错会让一个只读工具被围栏拦下（或反过来让写盘工具绕过围栏）。
    """

    name = "word_count"
    description = (
        "数一段文本的字数与行数。中文按**汉字个数**算，英文按**单词数**算，"
        "两者相加作为总字数（这是中文稿件的通用口径；不这样算会把一段"
        "纯中文的 3000 字说成 1 个词）。\n"
        "只读，不写盘。适合在写作任务里自查篇幅。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要统计的文本"},
        },
        "required": ["text"],
    }
    domain = "core"
    mutating = False
    source = "plugin"

    def __call__(self, text: str = "") -> str:
        t = str(text or "")
        lines = [ln for ln in t.splitlines() if ln.strip()]
        han = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
        # 英文单词：连续的字母（不含汉字）算一个
        import re
        words = len(re.findall(r"[A-Za-z][A-Za-z'\-]*", t))
        total = han + words
        return (f"汉字 {han} 个 · 英文单词 {words} 个 · 合计 {total} 字 · "
                f"非空行 {len(lines)} 行")


def _applies_term_bar(cols):
    """这个图型什么时候适用：一张「标签列 + 计数列」的两列表。

    判据要**窄**。写成"只要有两个分类列就建议"会让它到处冒出来，
    把真正贴切的建议挤掉 —— 那比不扩展更糟（它污染了建议器的判断）。

    这里收紧到：
      · 正好两列
      · 第一列是**文本标签**（分类且每行基本唯一，不是重复的组名）
      · 第二列是**数值计数**（占比 ≥ 0.9，允许少量缺失）
    这样它只在真正的「术语表/排行表」上出现。
    """
    from rongke import analysis as A

    if len(cols) != 2:
        return None
    lab, cnt = cols[0], cols[1]
    if not lab.is_category or not cnt.is_numeric:
        return None
    # 标签列必须近乎唯一：`count` 那种重复的组名不是术语表
    if not lab.n_total or (lab.n_unique / lab.n_total) < 0.8:
        return None
    if float(getattr(cnt, "numeric_ratio", 0.0) or 0.0) < 0.9:
        return None
    return A.suggest(
        "term_bar", x=lab.name, y=cnt.name,
        why=f"「{lab.name}」逐行唯一的标签、配「{cnt.name}」这个数值 —— "
            f"这是排行表，横向条形能让长标签排得开、名次一眼可读。",
        rejected=["bar：竖着放时中文/长英文标签会挤成一团或被迫旋转，"
                  "而旋转标签会触发碰撞门的误报。",
                  "scatter：两个变量里有一个是名义标签，没有可比的量。"],
        confidence="medium")


class TermTablePlugin(Plugin):
    """示例插件：把「术语出现次数」横向条形图画进荣珂。"""

    name = "term-table"
    version = "0.1.0"
    description = "术语频次条形图 + 字数统计工具"
    requires = ()          # 不额外依赖；绘图走荣珂已有的 matplotlib 子进程

    def tools(self):
        yield Tool(
            name=WordCountTool.name,
            description=WordCountTool.description,
            parameters=WordCountTool.parameters,
            fn=WordCountTool(),
            domain=WordCountTool.domain,
            mutating=WordCountTool.mutating,
            source="plugin",
        )

    def chart_kinds(self):
        """登记一种图型。

        `template` 是一段 Python 源码，会被**原样注入**到生成脚本里。
        契约（与内置图型完全一致，必须严格遵守）：

          * 函数签名固定为 `def draw(df):` —— **只有一个参数**。
            存盘与 `__main__` 由 `_EPILOGUE` 统一负责，插件不要自己 savefig。
          * 返回 `fig`（单个 Figure 对象），**不要返回 tuple**。
          * `df` 是已经读好的 DataFrame，列名已改成干净标识符。
          * 标题/坐标轴名等**用占位符**注入，写法是 `{title!r}` 这种
            repr 形式 —— 生成脚本时会替换成字面量。

        ★ 可用占位符只有这 10 个（由 `_plan_args` 提供，多一个都会被
          `build_script` 的残留检查拦下并报错）：

              {x!r} {y!r} {group!r} {title!r} {xlabel!r} {ylabel!r}
              {fit!r} {table!r} {stem!r} {out_dir!r}

          ★ 注意是 `xlabel` / `ylabel`，**不是** `x_label` / `y_label`
            （不带下划线）。我第一版写错了，`build_script` 当场报
            「绘图模板里有没替换的占位符」，那条检查救了整整一轮。

          `width_mm` / `height_mm` **不是** body 的占位符（它们只在脚本头部
          `_PLOT_HEAD` 里被替换）。body 里请直接用已定义好的
          `WIDTH_MM` / `HEIGHT_MM` 常量。

          脚本头部还预置了 `plt` / `pd` / `Path`，并已设好 rcParams
          （字体、字号、矢量嵌入、dpi）——插件**不要**重复设置。

        ★ 反例（我第一版就是这么写错的）：
            def draw(df, spec):        ← 错！多一个参数，运行时
                                          TypeError: missing 1 required
                                          positional argument: 'spec'
          而且错得很隐蔽：图型能注册、会被建议、脚本能生成，
          只在**真跑**的时候才炸。所以 plugins_test 必须真跑一次。
        """
        template = '''
def draw(df):
    """术语频次横向条形图。"""
    labels = [str(v) for v in df.iloc[:, 0]]
    values = [float(v) for v in df.iloc[:, 1]]
    order = sorted(range(len(values)), key=lambda i: values[i])
    labels = [labels[i] for i in order]
    values = [values[i] for i in order]

    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    bars = ax.barh(labels, values, color="#4a6fa5", height=0.62)
    ax.set_xlabel({xlabel!r})
    # 数值标在条端 —— 横向条形图不标数值，读者没法读出量级。
    for b, v in zip(bars, values):
        ax.text(b.get_width(), b.get_y() + b.get_height() / 2,
                " %g" % v, va="center", ha="left", fontsize=6)
    # ★ 图例收进绘图区：既交代样本量，也让「文字容纳门」有框内的字可比。
    ax.legend([bars[0]], ["n = %d terms" % len(values)],
              loc="lower right", fontsize=6, handlelength=1.2,
              borderaxespad=0.3)
    ax.set_title({title!r})
    fig.tight_layout()
    return fig
'''
        return {
            "term_bar": {
                "family": "compare",
                "name": "术语频次（横向条形）",
                "needs": "1 个文本列（术语名）+ 1 个计数列",
                "template": template,
                # ★ `applies` 让这个图型**真的会被建议**。
                #   不给它，图型就只能靠用户显式 `kind="term_bar"` 才用得上——
                #   因为 recommend() 的内置判据（时间序列/散点/箱线/直方图/
                #   热图）不认识插件图型。这是"扩展了但等于没扩展"的关键一步。
                #
                #   签名：`applies(cols) -> Suggestion | None`
                #   `cols` 是 list[Column]，每列有 .name / .kind /
                #   .is_numeric / .is_category / .is_time / .n_unique /
                #   .n_total / .numeric_ratio 等属性。
                #   返回 None 表示"这批数据不适用"，内核不会责怪你。
                "applies": _applies_term_bar,
            }
        }


def _demo() -> None:
    """直接跑这个文件时的自检（插件作者可以照抄这个形状）。"""
    import sys
    p = TermTablePlugin()
    print("插件:", p.name, p.version, "—", p.description)
    print("工具:", [t.name for t in p.tools()])
    print("图型:", list(p.chart_kinds()))
    t = WordCountTool()
    print("自检:", t(text="中微子振荡的验证 experiment 完成"))
    print("\n装法：把这个文件复制到 ~/.rongke/plugins/ 再跑 rongke --plugins")
    sys.exit(0)


#: ★ 加载器认这个模块级变量。类或实例都行（类会自动实例化）。
PLUGIN = TermTablePlugin


if __name__ == "__main__":
    _demo()
