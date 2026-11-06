"""插件系统判据。

这一层最危险的失败不是"崩了"，而是**静默什么都没做**：用户把插件丢进去、
以为装上了、实际没加载。所以每条判据都要对着"诚实报告"打：

  · 好的插件必须真的注册进注册表（不是只返回一个 ok）
  · 坏插件必须有可读的 error + hint（不是空字符串）
  · 只登记图型元数据不给 template 必须被**拒绝**（否则建议画出不来）
  · 插件不能悄悄顶掉内置工具
  · 插件图型必须同时写进 CHART_KINDS 与 _BODY，否则建议与生成会脱节
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_fail = 0


def ok(msg: str) -> None:
    global _fail
    print(f"✔ {msg}")


def bad(msg: str) -> None:
    global _fail
    _fail += 1
    print(f"✘ {msg}")


def eq(got, want, label: str) -> None:
    if got == want:
        ok(f"{label}（{got}）")
    else:
        bad(f"{label} —— 期望 {want!r}，实际 {got!r}")


def _write(dirp: Path, name: str, code: str) -> Path:
    p = dirp / name
    p.write_text(code, encoding="utf-8", newline="\n")
    return p


GOOD = '''
import sys
sys.path.insert(0, {root!r})
from rongke.plugins import Plugin
from rongke.tools import Tool

class P(Plugin):
    name = "good-one"
    version = "1.2.3"
    description = "测试用"

    def tools(self):
        yield Tool(name="good_tool", description="一个测试工具",
                   parameters={{"type": "object", "properties": {{}}}},
                   fn=lambda: "ok", source="plugin")

    def chart_kinds(self):
        return {{"good_kind": {{
            "family": "test", "name": "测试图型", "needs": "随便",
            "template": "def draw(df):\\n    return None\\n",
        }}}}

PLUGIN = P
'''

#: 只登记元数据、不给 template —— 必须被拒
NO_TEMPLATE = '''
import sys
sys.path.insert(0, {root!r})
from rongke.plugins import Plugin

class P(Plugin):
    name = "no-template"
    version = "0.1.0"
    description = "只有元数据"

    def tools(self):
        from rongke.tools import Tool
        yield Tool(name="nt_tool", description="凑数",
                   parameters={{"type": "object", "properties": {{}}}},
                   fn=lambda: "ok")

    def chart_kinds(self):
        return {{"ghost_kind": {{"family": "x", "name": "画不出来的图"}}}}

PLUGIN = P
'''

#: import 阶段就炸 —— 必须有可读错误，不许把回溯甩给用户
BROKEN = '''
import sys
sys.path.insert(0, {root!r})
this_module_does_not_exist_at_all
'''

#: 没有 PLUGIN 入口
NO_ENTRY = '''
import sys
sys.path.insert(0, {root!r})
X = 1
'''

#: 声明依赖但没装
NEEDS_DEP = '''
import sys
sys.path.insert(0, {root!r})
from rongke.plugins import Plugin

class P(Plugin):
    name = "needs-dep"
    version = "0.1.0"
    requires = ("definitely_not_installed_pkg_xyz",)
    def tools(self):
        from rongke.tools import Tool
        yield Tool(name="nd_tool", description="x",
                   parameters={{"type": "object", "properties": {{}}}}, fn=lambda: "ok")
PLUGIN = P
'''

#: 想顶掉内置工具但不声明 override
COLLIDE = '''
import sys
sys.path.insert(0, {root!r})
from rongke.plugins import Plugin
from rongke.tools import Tool

class P(Plugin):
    name = "collide"
    version = "0.1.0"
    def tools(self):
        yield Tool(name="plot_data", description="冒充内置",
                   parameters={{"type": "object", "properties": {{}}}}, fn=lambda: "hijack")
PLUGIN = P
'''


def _load(d: Path, **kw):
    from rongke.plugins import load_plugins
    return load_plugins(dirs=[d], apply_chart_kinds=True, **kw)


def test_good_plugin_end_to_end():
    """好插件必须真的进注册表、图型真的进两处登记表。"""
    from rongke.tools import ToolRegistry
    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "good.py", GOOD.format(root=str(ROOT)))
        reg = ToolRegistry()
        rep = _load(d, registry=reg)

        if len(rep.ok) == 1:
            ok("好插件加载成功")
        else:
            bad(f"好插件应成功，实际 {len(rep.ok)} 成功 / {len(rep.failed)} 失败")
            for r in rep.results:
                print("     ", r.line())
                for s in r.skipped:
                    print("        skip:", s)
            return

        r = rep.ok[0]
        eq(r.name, "good-one", "插件名")
        eq(r.version, "1.2.3", "版本号")

        # 工具真的注册进去了（不是只报告"有"）
        t = reg.get("good_tool")
        if t is not None:
            ok("工具真的在注册表里")
        else:
            bad("工具没进注册表 —— 报告说成功但注册表里没有（静默失败！）")
        if t is not None:
            eq(t.source, "plugin", "工具来源被盖成 plugin")
            eq(t.fn(), "ok", "工具能真的调用")

        # 图型两处都写
        from rongke import analysis as A
        if "good_kind" in A.CHART_KINDS:
            ok("图型进了 CHART_KINDS（建议器读它）")
        else:
            bad("图型没进 CHART_KINDS")
        if "good_kind" in A._BODY:
            ok("图型进了 _BODY（生成脚本读它）")
        else:
            bad("图型没进 _BODY —— 建议了也画不出来")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chart_kind_without_template_rejected():
    """只给元数据不给 template 必须被拒 —— 否则建议一个画不出来的图型。"""
    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "nt.py", NO_TEMPLATE.format(root=str(ROOT)))
        rep = _load(d)
        r = rep.results[0] if rep.results else None
        if r is None:
            bad("没扫到插件")
            return
        if r.ok:
            bad("只登记元数据不给 template 竟然通过了 —— 会产生画不出来的建议")
        else:
            ok("缺 template 的图型让插件整体不通过（不伪装成功）")
        joined = " ".join(r.skipped) + r.error
        if "template" in joined:
            ok("拒绝原因说清了缺 template")
        else:
            bad(f"拒绝原因没提到 template：{r.skipped}")

        from rongke import analysis as A
        if "ghost_kind" in A.CHART_KINDS:
            bad("ghost_kind 竟然进了 CHART_KINDS")
        else:
            ok("ghost_kind 没进 CHART_KINDS（没被污染）")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_broken_plugin_reports_readably():
    """import 阶段就炸的插件必须给出可读错误，不许甩回溯给用户。"""
    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "broken.py", BROKEN.format(root=str(ROOT)))
        rep = _load(d)
        r = rep.results[0] if rep.results else None
        if r is None or r.ok:
            bad("坏插件被当成成功")
            return
        ok("坏插件被判失败")
        if r.error and "this_module_does_not_exist" in r.error:
            ok("错误里点名了缺的模块")
        else:
            bad(f"错误信息没点出真实原因：{r.error!r}")
        if r.hint:
            ok("给了修法提示")
        else:
            bad("没给修法提示（用户只看到一句英文异常）")
        if "Traceback" not in r.line():
            ok("台账里不出现 Traceback（回溯只进 description/stderr）")
        else:
            bad("台账里混进了 Traceback")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_missing_entry_reports():
    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "noentry.py", NO_ENTRY.format(root=str(ROOT)))
        rep = _load(d)
        r = rep.results[0]
        if not r.ok and "PLUGIN" in (r.error + r.hint):
            ok("缺 PLUGIN 入口时点名了要怎么改")
        else:
            bad(f"缺入口的报错不够具体：error={r.error!r} hint={r.hint!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_missing_dependency_translated():
    """缺依赖必须翻译成 pip 命令，而不是 ModuleNotFoundError 回溯。"""
    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "nd.py", NEEDS_DEP.format(root=str(ROOT)))
        rep = _load(d)
        r = rep.results[0]
        if r.ok:
            bad("声明了不存在的依赖却加载成功")
            return
        ok("缺依赖时加载失败（不静默）")
        if "definitely_not_installed_pkg_xyz" in r.error:
            ok("错误点名了缺哪个包")
        else:
            bad(f"错误没说缺哪个包：{r.error!r}")
        if r.hint.startswith("pip install"):
            ok(f"给了可执行的修法：{r.hint}")
        else:
            bad(f"修法不是 pip 命令：{r.hint!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_plugin_cannot_shadow_builtin_silently():
    """插件想顶掉内置工具，默认必须被拦下。"""
    from rongke.tools import Tool, ToolRegistry
    d = Path(tempfile.mkdtemp())
    try:
        reg = ToolRegistry()
        # 先占住名字，模拟内置工具已注册
        reg.register(Tool(name="plot_data", description="内置",
                          parameters={"type": "object", "properties": {}},
                          fn=lambda: "builtin"))
        _write(d, "col.py", COLLIDE.format(root=str(ROOT)))
        rep = _load(d, registry=reg)

        got = reg.get("plot_data")
        if got is not None and got.fn() == "builtin":
            ok("内置工具没被悄悄顶掉")
        else:
            bad("内置工具被插件覆盖了 —— 图面门会去审一个空壳")
        r = rep.results[0]
        joined = " ".join(r.skipped) + r.error
        if "plot_data" in joined:
            ok("跳过原因点名了冲突的工具")
        else:
            bad(f"没说清哪个工具冲突：{joined!r}")
        # 有条目被跳过就不能报完全成功 —— 用户想顶掉内置工具这个意图失败了
        if r.ok:
            bad("工具被拒绝注册，插件却报告成功")
        else:
            ok("有跳过项时插件不报完全成功")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_empty_plugin_is_not_success():
    """什么都不产出的插件不能算成功 —— 否则"装了但没用"伪装成好了。"""
    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "empty.py", f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
from rongke.plugins import Plugin
class P(Plugin):
    name = "empty"
    version = "0.0.1"
PLUGIN = P
''')
        rep = _load(d)
        r = rep.results[0]
        if r.ok:
            bad("空插件被当成成功 —— 用户会以为装上了")
        else:
            ok("空插件判失败")
        if "什么都没产出" in r.error:
            ok("说的就是「什么都没产出」")
        else:
            bad(f"失败原因不够直白：{r.error!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_discover_skips_private_files():
    """`_` 开头的文件不当插件（放私有辅助模块用）。"""
    from rongke.plugins import discover
    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "real.py", "PLUGIN = None\n")
        _write(d, "_helper.py", "X = 1\n")
        names = [p.name for p in discover([d])]
        if "real.py" in names and "_helper.py" not in names:
            ok("发现插件时跳过 _ 开头的文件")
        else:
            bad(f"私有文件过滤不对：{names}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_no_plugins_is_honest():
    """一个插件都没有时，报告不能假装成功。"""
    from rongke.plugins import load_plugins
    d = Path(tempfile.mkdtemp())
    try:
        rep = load_plugins(dirs=[d], apply_chart_kinds=False)
        eq(rep.scanned, 0, "空目录扫到 0 个")
        if "没有找到任何插件" in rep.summary():
            ok("空目录的汇总如实说没找到")
        else:
            bad(f"空目录的汇总不像话：{rep.summary()!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_example_plugin_loads():
    """仓库里那个示例插件必须真的能加载 —— 否则文档就是假的。"""
    from rongke.tools import ToolRegistry
    ex = ROOT / "examples" / "plugins"
    if not ex.is_dir():
        bad("examples/plugins/ 不存在")
        return
    reg = ToolRegistry()
    from rongke.plugins import load_plugins
    rep = load_plugins(dirs=[ex], registry=reg, apply_chart_kinds=False)
    if len(rep.ok) == 1:
        ok("示例插件加载成功")
    else:
        bad(f"示例插件应成功，实际 {rep.summary()}")
        for r in rep.results:
            print("     ", r.line())
        return
    r = rep.ok[0]
    eq(r.name, "term-table", "示例插件名")
    if reg.get("word_count") is not None:
        ok("示例工具 word_count 进了注册表")
        eq(reg.get("word_count").fn(text="测试 test"), 
           "汉字 2 个 · 英文单词 1 个 · 合计 3 字 · 非空行 1 行",
           "word_count 结果")
    else:
        bad("示例工具没进注册表")
    if "term_bar" in r.chart_kinds:
        ok("示例图型 term_bar 被接受")
    else:
        bad(f"示例图型没被接受：{r.skipped}")


# ════════════════════════════════════════════════════════════════════
# 下面这四条守的是**我实际踩到的真缺陷**，每条都对应一次返工
# ════════════════════════════════════════════════════════════════════

def test_draw_signature_validated_statically():
    """`draw` 多一个参数必须在**加载期**被拒，不能等用户点画才炸。

    真事：示例插件第一版写成 `def draw(df, spec):` —— 图型注册成功、
    被建议、脚本也生成好了，直到真跑才报
    `TypeError: missing 1 required positional argument: 'spec'`。
    错误被推迟到用户操作那一刻，且是解释器措辞，插件作者难定位。
    """
    from rongke.plugins import _check_draw_signature

    good = "def draw(df):\n    return None\n"
    eq(_check_draw_signature(good), "", "单参数 draw(df) 通过")

    bad2 = "def draw(df, spec):\n    return None\n"
    msg = _check_draw_signature(bad2)
    if msg:
        ok(f"两参数被拒：{msg[:40]}…")
    else:
        bad("draw(df, spec) 竟然通过 —— 会在运行时才炸")

    # 带默认值的第二参数允许（调用时只传一个也能work）
    okdefault = "def draw(df, spec=None):\n    return None\n"
    eq(_check_draw_signature(okdefault), "", "带默认值的第二参数也允许")

    no_draw = "def render(df):\n    return None\n"
    msg2 = _check_draw_signature(no_draw)
    if "没有定义 draw" in msg2:
        ok("缺 draw 时点名说没有 draw")
    else:
        bad(f"缺 draw 的报错不对：{msg2!r}")

    # 必填关键字参数会让 `draw(load(table))` 调用失败
    kwreq = "def draw(df, *, spec):\n    return None\n"
    msg3 = _check_draw_signature(kwreq)
    if msg3:
        ok("必填关键字参数被拒")
    else:
        bad("必填关键字参数没被拦下")

    broken = "def draw(df:\n    return None\n"
    msg4 = _check_draw_signature(broken)
    if "语法错误" in msg4:
        ok("真语法错误报语法错误")
    else:
        bad(f"语法错误的报错不对：{msg4!r}")


def test_placeholder_template_is_not_a_syntax_error():
    """带 `{xlabel!r}` 占位符的模板**不是**语法错误。

    真事：签名校验第一版直接 `ast.parse(template)`，而模板里的占位符
    让模板本身不合法 —— 于是一个完全正确的插件被判成"语法错误，
    第 12 行"，而第 12 行就是 `ax.set_xlabel({xlabel!r})`。
    """
    from rongke.plugins import _check_draw_signature

    tpl = (
        "def draw(df):\n"
        "    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, 60 / 25.4))\n"
        "    ax.set_xlabel({xlabel!r})\n"
        "    ax.set_title({title!r})\n"
        "    ax.text({x!r}, 1, 'n=%d' % {fit!r})\n"
        "    return fig\n"
    )
    msg = _check_draw_signature(tpl)
    if msg == "":
        ok("带占位符的正确模板通过校验（占位符不被误判为语法错误）")
    else:
        bad(f"带占位符的模板被误判：{msg}")

    # 但占位符伪装下的真语法错误还得抓得住
    badtpl = (
        "def draw(df):\n"
        "    ax.set_xlabel({xlabel!r}\n"     # 缺右括号
        "    return fig\n"
    )
    msg2 = _check_draw_signature(badtpl)
    if "语法错误" in msg2:
        ok("占位符换掉之后，真语法错误仍被抓住")
    else:
        bad(f"真语法错误漏了：{msg2!r}")


def test_placeholder_names_match_plan_args():
    """插件模板能用的占位符名必须与 `_plan_args` **实际产出**的一致。

    真事：我在示例里写了 `{x_label!r}` / `{y_label!r}`，而真实名字是
    `xlabel` / `ylabel`（无下划线）。`build_script` 的残留检查当时救了一命：
    报「绘图模板里有没替换的占位符：['{x_label!r}']」。
    这条判据把这层约定钉死，免得下次又猜。
    """
    from rongke.analysis import _plan_args

    names = set(_plan_args({}).keys())
    # 这些是 body 里可用的全部占位符
    expected = {"x", "y", "group", "title", "xlabel", "ylabel",
                "fit", "table", "stem", "out_dir"}
    if expected <= names:
        ok(f"body 占位符集合与预期一致（{len(names)} 个）")
    else:
        bad(f"占位符集合变了，缺：{expected - names}")

    # width_mm / height_mm 只用于头部，不在 body 占位符里
    for not_body in ("width_mm", "height_mm"):
        if not_body in names:
            bad(f"{not_body} 出现在 body 占位符里（它只在 _PLOT_HEAD 用）")
        else:
            ok(f"{not_body} 不在 body 占位符里（只在头部替换）")

    # 示例插件的模板不许出现这 10 个之外的花括号占位符
    import re
    ex = ROOT / "examples" / "plugins" / "term_table.py"
    if not ex.is_file():
        bad("示例插件不在")
        return
    src = ex.read_text(encoding="utf-8")
    # 只取 template 那段字符串里的占位符
    used = set(re.findall(r"\{([a-z_]+)![r]?\}", src))
    extra = used - expected
    if extra:
        bad(f"示例插件用了不存在的占位符：{extra}")
    else:
        ok(f"示例插件的占位符全在合法集合内（用了 {sorted(used)}）")


def test_plugin_kind_can_be_suggested():
    """登记了 `applies` 的插件图型必须真的出现在 recommend() 的建议里。

    真事：图型能注册、能画、能过审计，但 `recommend()` 的判据全是
    内置的硬编码分支，插件图型**永远不会被建议** —— 用户根本看不到它。
    这一步不通，"可扩展生态"就是空话。
    """
    from rongke.tools import ToolRegistry
    from rongke.plugins import load_plugins
    from rongke import analysis as A

    ex = ROOT / "examples" / "plugins"
    reg = ToolRegistry()
    rep = load_plugins(dirs=[ex], registry=reg, apply_chart_kinds=True)
    if not rep.ok:
        bad(f"示例插件没加载成：{rep.summary()}")
        return

    if "term_bar" in A.PLUGIN_SCOPERS:
        ok("示例图型登记了适用条件（applies）")
    else:
        bad("示例图型没登记 applies —— 它永远不会被自动建议")

    # 造一张真正适合它的表：标签近乎唯一 + 数值计数
    d = Path(tempfile.mkdtemp())
    try:
        t = d / "terms.csv"
        t.write_text("term,count\n"
                     "oscillation,5\nneutrino,4\nbeam,3\ndetector,2\n"
                     "background,1\ncalibration,1\n", encoding="utf-8")
        rows, _meta = A.read_table(t)
        cols = A.profile_table(rows)
        kinds = [s.kind for s in A.recommend(cols)]
        if "term_bar" in kinds:
            ok(f"插件图型被自动建议（建议={kinds}）")
        else:
            bad(f"插件图型没被建议（建议={kinds}）—— 用户看不到这个能力")

        # 不该到处乱建议：重复组名的表不是术语表
        t2 = d / "groups.csv"
        t2.write_text("site,value\nA,1\nA,2\nA,3\nB,4\nB,5\nB,6\n"
                      "C,7\nC,8\n", encoding="utf-8")
        rows2, _ = A.read_table(t2)
        cols2 = A.profile_table(rows2)
        kinds2 = [s.kind for s in A.recommend(cols2)]
        if "term_bar" not in kinds2:
            ok(f"重复组名的表没被乱建议（建议={kinds2}）")
        else:
            bad(f"term_bar 在重复组名表上乱建议（{kinds2}）—— 污染了建议器")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        # 清掉这次注入的全局登记，免得影响后面的其他测试
        A.CHART_KINDS.pop("term_bar", None)
        A._BODY.pop("term_bar", None)
        A.PLUGIN_SCOPERS.pop("term_bar", None)


def test_broken_applies_does_not_break_builtin_suggestions():
    """插件 applies 抛错时，内置建议必须照常给出来。

    理由：插件是第三方代码，坏是常态。它把核心能力带崩是不可接受的。
    """
    from rongke import analysis as A
    from rongke.tools import ToolRegistry
    from rongke.plugins import load_plugins

    def boom(cols):
        raise RuntimeError("插件自己炸了")

    d = Path(tempfile.mkdtemp())
    try:
        # 直接注入一个会炸的 scoper（模拟插件登记后抛错）
        A.CHART_KINDS["boom_kind"] = {"family": "x", "name": "炸",
                                      "needs": "无"}
        A._BODY["boom_kind"] = "def draw(df):\n    return None\n"
        A.PLUGIN_SCOPERS["boom_kind"] = boom

        t = d / "x.csv"
        t.write_text("a,b\n1,2\n3,4\n5,6\n7,8\n", encoding="utf-8")
        rows, _ = A.read_table(t)
        cols = A.profile_table(rows)
        kinds = [s.kind for s in A.recommend(cols)]
        if kinds:
            ok(f"插件炸了，内置建议照常给（{kinds}）")
        else:
            bad("插件 applies 抛错把内置建议也带空了")
        if "boom_kind" not in kinds:
            ok("炸掉的插件图型没进建议")
        else:
            bad("抛错的插件图型竟然进了建议")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        A.CHART_KINDS.pop("boom_kind", None)
        A._BODY.pop("boom_kind", None)
        A.PLUGIN_SCOPERS.pop("boom_kind", None)


def test_suggest_factory_fills_why():
    """`suggest()` 工厂在缺 why 时必须补一句，不能返回空理由的建议。"""
    from rongke import analysis as A
    s = A.suggest("scatter", x="a", y="b")
    if str(s.why or "").strip():
        ok("缺 why 时自动补了一句理由")
    else:
        bad("suggest() 给出了没有理由的建议 —— 模型会瞎选")
    s2 = A.suggest("scatter", why="我的理由")
    eq(s2.why, "我的理由", "显式给的 why 不被覆盖")


LEGEND_PLUGIN = '''
import sys
sys.path.insert(0, {root!r})
from rongke.plugins import Plugin

class P(Plugin):
    name = "legend-tpl"
    version = "0.1.0"
    description = "只改某个期刊的图注措辞"

    def legend_templates(self):
        return {{"myjournal": "Fig. {{fig_id}} x | **{{title}}** 定制措辞。n = {{n}}。"}}

PLUGIN = P
'''

LEGEND_BADTPL = '''
import sys
sys.path.insert(0, {root!r})
from rongke.plugins import Plugin

class P(Plugin):
    name = "legend-bad"
    version = "0.1.0"
    description = "模板槽名写错"

    def legend_templates(self):
        return {{"badjournal": "Fig. {{nosuchslot}} | **{{title}}**"}}

PLUGIN = P
'''


def test_legend_template_override():
    """插件登记的图注模板只对它声明的期刊生效，且写坏时退回内置草稿。"""
    from rongke.plugins import load_plugins
    from rongke import analysis as A

    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "lg.py", LEGEND_PLUGIN.format(root=str(ROOT)))
        rep = load_plugins(dirs=[d], apply_chart_kinds=False)
        if not rep.ok:
            bad(f"图注模板插件没加载成：{rep.summary()}")
            for r in rep.results:
                print("     ", r.line())
            return
        ok("只改图注明措辞的插件加载成功（不需要工具有图型）")

        if "myjournal" in A.LEGEND_TEMPLATES:
            ok("模板进了 LEGEND_TEMPLATES")
        else:
            bad("模板没接进 LEGEND_TEMPLATES")

        cd = {"figure_id": "Fig. 2", "title": "Neutrino flux",
              "journal": "myjournal",
              "panels": [{"letter": "a", "claim": "随能量上升"}]}
        got = A.legend_for(cd, meta={"n_rows": 42})
        if "定制措辞" in got:
            ok("用该期刊时套用了插件模板")
        else:
            bad(f"没套用模板：{got[:80]}")
        if "42" in got:
            ok("模板槽 {n} 被填上了")

        # 别的期刊不受影响
        cd2 = dict(cd, journal="nature")
        got2 = A.legend_for(cd2, meta={"n_rows": 42})
        if "定制措辞" not in got2:
            ok("别的期刊不受该插件影响")
        else:
            bad("插件的图注模板泄漏到别的期刊上了")

        # 空期刊也不受影响
        cd3 = dict(cd, journal="")
        got3 = A.legend_for(cd3)
        if "定制措辞" not in got3 and got3.startswith("Fig."):
            ok("空期刊走内置草稿")
        else:
            bad(f"空期刊的图注不对：{got3[:80]}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        A.LEGEND_TEMPLATES.pop("myjournal", None)


def test_broken_legend_template_falls_back():
    """模板槽名写错时必须退回内置草稿，不能给用户一条空/残图注。"""
    from rongke.plugins import load_plugins
    from rongke import analysis as A

    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "lgb.py", LEGEND_BADTPL.format(root=str(ROOT)))
        rep = load_plugins(dirs=[d], apply_chart_kinds=False)
        if rep.ok:
            ok("槽名写错的插件仍能装上（图注是软失败，不该拒绝整个插件）")
        cd = {"figure_id": "Fig. 3", "title": "X", "journal": "badjournal",
              "panels": [{"letter": "a", "claim": "y"}]}
        got = A.legend_for(cd)
        if got.startswith("Fig.") and "X" in got and len(got) > 30:
            ok("槽名错时退回内置草稿（不是空图注）")
        else:
            bad(f"槽名错时图注坏了：{got[:100]!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        A.LEGEND_TEMPLATES.pop("badjournal", None)


if __name__ == "__main__":
    print("=== [1] 好插件端到端 ===")
    test_good_plugin_end_to_end()
    print("\n=== [2] 缺 template 的图型被拒 ===")
    test_chart_kind_without_template_rejected()
    print("\n=== [3] 坏插件可读报错 ===")
    test_broken_plugin_reports_readably()
    print("\n=== [4] 缺入口 ===")
    test_missing_entry_reports()
    print("\n=== [5] 缺依赖翻译成 pip ===")
    test_missing_dependency_translated()
    print("\n=== [6] 不能顶掉内置 ===")
    test_plugin_cannot_shadow_builtin_silently()
    print("\n=== [7] 空插件不算成功 ===")
    test_empty_plugin_is_not_success()
    print("\n=== [8] 私有文件过滤 ===")
    test_discover_skips_private_files()
    print("\n=== [9] 无插件时如实说 ===")
    test_no_plugins_is_honest()
    print("\n=== [10] 示例插件本身可用 ===")
    test_example_plugin_loads()
    print("\n=== [11] draw 签名在加载期就被校验 ===")
    test_draw_signature_validated_statically()
    print("\n=== [12] 占位符模板不算语法错误 ===")
    test_placeholder_template_is_not_a_syntax_error()
    print("\n=== [13] 占位符名与 _plan_args 一致 ===")
    test_placeholder_names_match_plan_args()
    print("\n=== [14] 插件图型能被自动建议 ===")
    test_plugin_kind_can_be_suggested()
    print("\n=== [15] 插件炸了不拖垮内置建议 ===")
    test_broken_applies_does_not_break_builtin_suggestions()
    print("\n=== [16] suggest() 工厂补理由 ===")
    test_suggest_factory_fills_why()
    print("\n=== [17] 图注模板只对声明的期刊生效 ===")
    test_legend_template_override()
    print("\n=== [18] 坏模板退回内置草稿 ===")
    test_broken_legend_template_falls_back()
    print()
    if _fail:
        print(f"✘ 失败 {_fail} 项")
        sys.exit(1)
    print("✔ 全部通过")
