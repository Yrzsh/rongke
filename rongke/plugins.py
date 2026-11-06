# -*- coding: utf-8 -*-
"""插件系统 —— 让别人能往荣珂上长东西，而不用改内核源码。

═══════════════════════════════════════════════════════════════════════
为什么要有这一层
═══════════════════════════════════════════════════════════════════════
在此之前，加一个工具、一种图型，唯一的路是**改 `loop.py`**。这对自用够了，
但「生态成熟」的含义是**别人不用碰内核也能扩展**。所以这里定义三件事：

  1. 插件长什么样（`Plugin` 协议）
  2. 从哪里加载（`~/.rongke/plugins/*.py` + 配置里的路径）
  3. 加载失败时怎么说实话（**不许静默跳过**）

═══════════════════════════════════════════════════════════════════════
三条设计纪律
═══════════════════════════════════════════════════════════════════════
一、**插件不许静默失败。**
   加载一个插件报错时，如果只是 `except: pass`，用户会以为"我装了插件"
   而实际什么也没发生 —— 这比没有插件系统更糟（它伪造了一个能力）。
   所以每个插件都有一份 `LoadResult`，失败原因原样保留给人看。

二、**插件不许覆盖内置。**
   `ToolRegistry.register()` 默认对重名抛错。插件走同一条路：想顶掉
   `plot_data` 这种内置工具必须显式声明 `override=True`，且只有插件
   自己的名字空间能这么做。理由：内置工具带审计语义，被一个同名插件
   悄悄替掉，图面门就会审一个空壳。

三、**插件声明的依赖缺失要说人话。**
   一个插件可能 `import matplotlib`。没装时不该让用户看见 `ModuleNotFoundError`
   回溯，而应该说「插件 X 需要 matplotlib，请 pip install rongke[figure]」。

═══════════════════════════════════════════════════════════════════════
插件怎么写（最小例子，存成 ~/.rongke/plugins/my_plugin.py）
═══════════════════════════════════════════════════════════════════════
    from rongke.plugins import Plugin
    from rongke.tools import Tool

    class MyPlugin(Plugin):
        name = "my-plugin"
        version = "0.1.0"
        description = "把温度从华氏换到摄氏"

        def tools(self):
            yield Tool(
                name="f_to_c",
                description="华氏温度转摄氏温度",
                parameters={
                    "type": "object",
                    "properties": {"f": {"type": "number"}},
                    "required": ["f"],
                },
                fn=lambda f: f"{(f - 32) * 5 / 9:.2f} °C",
                domain="core",
                source="plugin",
            )

    PLUGIN = MyPlugin          # 模块级入口，加载器认这个

完整的可跑示例见 `examples/plugins/word_count.py` 与
`docs/插件开发.md`。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "Plugin", "PluginError", "LoadResult", "LoadReport",
    "plugin_dirs", "load_plugins", "discover",
]


class PluginError(RuntimeError):
    """插件本身有问题。消息要能直接印给人看。"""


# ════════════════════════════════════════════════════════════════════
# 插件接口
# ════════════════════════════════════════════════════════════════════

class Plugin:
    """插件基类。**继承它或只实现同名属性都行** —— 我们按鸭子类型取用。

    为什么不用 `abc.ABC`：第三方写插件时被强制继承一个类，会让
    "只想加一个工具"这种小事变得比改内核还麻烦。鸭子类型够了。
    """

    #: 唯一标识。重名会让后加载的被跳过（并如实记录原因）。
    name: str = ""
    #: 语义化版本。**必填** —— 出问题时人得知道是哪个版本。
    version: str = "0.0.0"
    #: 一句话说明。会出现在 `rongke --plugins` 的台账里。
    description: str = ""
    #: 声明需要的第三方模块。缺了这里会把 ModuleNotFoundError
    #: 翻译成「请 pip install rongke[...]」而不是甩回溯。
    requires: tuple[str, ...] = ()

    # ── 扩展点（都返回可迭代对象，返回 None 等价于空） ──────────

    def tools(self) -> Iterable[Any]:
        """产出 `Tool` 实例。"""
        return ()

    def chart_kinds(self) -> dict[str, dict[str, Any]]:
        """产出图型登记项：`{kind: {"family":…, "name":…, "needs":…, "template": …}}`。

        `template` 是该图型的绘图函数源码（字符串），会被注入到生成脚本里。
        **只登记元数据而不给 template 是不允许的** —— 那样
        `recommend()` 会推荐一个画不出来的图型。加载时会检查并拒绝。
        """
        return {}

    def legend_templates(self) -> dict[str, str]:
        """产出图注模板：`{journal_id: template}`，可覆盖内置期刊的措辞。"""
        return {}

    def on_load(self, ctx: dict) -> None:
        """加载成功后的回调。`ctx` 至少含 `data_root` 与 `registry`。"""
        return None


# ════════════════════════════════════════════════════════════════════
# 加载结果
# ════════════════════════════════════════════════════════════════════

@dataclass
class LoadResult:
    """一个插件的加载结局。**失败也保留，不许吞掉。**"""
    path: str
    ok: bool
    name: str = ""
    version: str = ""
    description: str = ""
    tools: list[str] = field(default_factory=list)
    chart_kinds: list[str] = field(default_factory=list)
    legend_templates: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # 被拒的条目及原因
    #: 不构成失败但**值得让人知道**的事（如"图型没声明 applies，
    #: 不会被自动建议"）。与 `skipped` 的区别是它不影响 `ok`。
    #: 分这两个字段的理由：把"提示"混进"跳过"会让完全合规的插件
    #: 被判成失败 —— 判据就不可信了。
    notes: list[str] = field(default_factory=list)
    error: str = ""
    hint: str = ""                                     # 给用户的修法

    def line(self) -> str:
        if not self.ok:
            s = f"✘ {self.name or Path(self.path).stem} 加载失败：{self.error}"
            if self.hint:
                s += f"\n   修法：{self.hint}"
            for sk in self.skipped:
                s += f"\n   ⚠ 跳过：{sk}"
            return s
        bits = [f"✔ {self.name} v{self.version}"]
        if self.description:
            bits.append(f"— {self.description}")
        detail = []
        if self.tools:
            detail.append(f"工具 {len(self.tools)}：{'/'.join(self.tools)}")
        if self.chart_kinds:
            detail.append(f"图型 {len(self.chart_kinds)}：{'/'.join(self.chart_kinds)}")
        if self.legend_templates:
            detail.append(f"图注模板：{'/'.join(self.legend_templates)}")
        s = " ".join(bits)
        if detail:
            s += "\n   " + "  |  ".join(detail)
        for sk in self.skipped:
            s += f"\n   ⚠ 跳过：{sk}"
        for n in self.notes:
            s += f"\n   · {n}"
        return s


@dataclass
class LoadReport:
    """一次加载的全部结果。`rongke --plugins` 直接印它。"""
    results: list[LoadResult] = field(default_factory=list)
    scanned: int = 0

    @property
    def ok(self) -> list[LoadResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[LoadResult]:
        return [r for r in self.results if not r.ok]

    def summary(self) -> str:
        n_ok, n_bad = len(self.ok), len(self.failed)
        tools = sum(len(r.tools) for r in self.ok)
        kinds = sum(len(r.chart_kinds) for r in self.ok)
        legs = sum(len(r.legend_templates) for r in self.ok)
        if not self.scanned:
            return "没有找到任何插件（在 ~/.rongke/plugins/ 放 .py 文件即可）"
        s = f"扫了 {self.scanned} 个，成功 {n_ok}，失败 {n_bad}"
        if n_ok:
            bits = []
            if tools:
                bits.append(f"工具 {tools} 个")
            if kinds:
                bits.append(f"图型 {kinds} 种")
            if legs:
                bits.append(f"图注模板 {legs} 个")
            if bits:
                s += "；新增" + "、".join(bits)
        return s


# ════════════════════════════════════════════════════════════════════
# 从哪儿找插件
# ════════════════════════════════════════════════════════════════════

def plugin_dirs(cfg=None) -> list[Path]:
    """插件搜索路径，按优先级排列。

    1. 环境变量 `RONGKE_PLUGINS`（`os.pathsep` 分隔，测试/部署用）
    2. 配置 `plugins.dirs`（列表）
    3. `~/.rongke/plugins`  —— 用户的个人插件
    4. `<data_root>/plugins` —— 项目内的插件（跟着数据走）

    ★ 只搜这些目录，**不搜 site-packages 里任意包** —— 那会把第三方
      库的顶层模块当成插件去 exec，是个安全问题。
    """
    out: list[Path] = []

    env = os.environ.get("RONGKE_PLUGINS", "").strip()
    if env:
        out.extend(Path(p).expanduser() for p in env.split(os.pathsep) if p.strip())

    if cfg is not None:
        pl = getattr(cfg, "plugins", None)
        dirs = getattr(pl, "dirs", None) if pl is not None else None
        if isinstance(dirs, (list, tuple)):
            out.extend(Path(d).expanduser() for d in dirs if str(d).strip())

    out.append(Path.home() / ".rongke" / "plugins")
    try:
        from .config import data_root
        out.append(data_root() / "plugins")
    except Exception:
        pass

    # 去重且保序
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def discover(dirs: list[Path] | None = None) -> list[Path]:
    """列出候选插件文件（`*.py`，跳过 `_` 开头的）。"""
    files: list[Path] = []
    for d in (dirs if dirs is not None else plugin_dirs()):
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            files.append(f)
    return files


# ════════════════════════════════════════════════════════════════════
# 加载
# ════════════════════════════════════════════════════════════════════

def _missing_modules(mods: tuple[str, ...]) -> list[str]:
    import importlib.util as iu
    out = []
    for m in mods:
        try:
            if iu.find_spec(m.split(".")[0]) is None:
                out.append(m)
        except (ImportError, ValueError):
            out.append(m)
    return out


def _hint_for_missing(mods: list[str]) -> str:
    """把缺模块翻译成可执行的命令，而不是让人去猜。"""
    if not mods:
        return ""
    m = set(mods)
    if m & {"matplotlib", "pandas", "openpyxl", "fitz"}:
        return "pip install \"rongke[figure]\""
    if m & {"win32com", "pythoncom"}:
        return "pip install \"rongke[com]\""
    if "pytest" in m:
        return "pip install \"rongke[dev]\""
    return "pip install " + " ".join(sorted(m))


def _fill_placeholders_for_ast(template: str) -> str:
    """把模板里的 `{...}` 占位符换成合法字面量，好让 AST 能解析。

    只用于**签名校验**，绝不用于执行。占位符形如：
        {x!r}  {title!r}  {width_mm!r}  {out_dir!r}  {stem!r}
    替换成一个字符串字面量 `'X'` 就够了 —— 语法骨架不变。

    ★ 不能直接把整段 `{...}` 删掉：在 `ax.set_xlabel({x_label!r})` 里
      删掉会变成 `ax.set_xlabel()`，那是**另一种**语法（合法但语义不同），
      而在 `figsize=({w!r} / 25.4, ...)` 里删掉会变成 `figsize=( / 25.4)`
      直接语法错。替换成字面量两种情形都对。
    """
    import re
    # 只换花括号占位符；`{{` `}}` 是转义后的真花括号，先保护起来。
    s = template.replace("{{", "\x00L\x00").replace("}}", "\x00R\x00")
    s = re.sub(r"\{[^{}]*\}", "'X'", s)
    s = s.replace("\x00L\x00", "{{").replace("\x00R\x00", "}}")
    return s


def _check_draw_signature(template: str) -> str:
    """检查绘图模板里 `draw` 的签名是否是 `draw(df)` 单参数。

    返回空串表示没问题，否则返回一句可直接印给人看的拒绝理由。

    ★ 为什么值得单独做这个静态检查：`_EPILOGUE` 里调用的是
      `fig = draw(load(table))`。签名写错时：
        加载成功 → 图型进了 CHART_KINDS → recommend() 会推荐它 →
        用户点"画这张" → **那时才炸** TypeError。
      错误被推迟到用户操作那一刻，且错误信息是解释器的措辞
      （`missing 1 required positional argument`），插件作者未必能定位到
      是签名约定错了。所以在加载期就拦住，并把正确写法说出来。

    为什么不直接跑一遍：加载插件时不该起 matplotlib 子进程（慢、还要
    绘图后端）。静态读签名足够——这是 AST 能确定性回答的问题。
    """
    import ast
    # ★ 模板里的占位符（`{x_label!r}` 这类）让模板**本身不是合法 Python**，
    #   直接 ast.parse 会报 `invalid syntax` —— 实测踩过：一个完全正确的
    #   插件因为这条被判成"语法错误"，而错误信息还指着占位符的位置。
    #   校验签名只需要语法骨架，所以先把占位符换成无副作用的字面量。
    probe = _fill_placeholders_for_ast(template)
    try:
        tree = ast.parse(probe)
    except SyntaxError as e:
        return f"template 有语法错误（{e.msg}，第 {e.lineno} 行）"

    fn = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "draw":
            fn = node
            break
    if fn is None:
        return "template 里没有定义 draw(df)"

    args = fn.args
    n_pos = len(args.posonlyargs) + len(args.args)
    # `self` 之类不会出现在模板里；但允许一个可选的 spec 参数，
    # 只要调用时能只传一个参数过去（有默认值）。
    required = n_pos - len(args.defaults)
    if required != 1:
        return (f"draw 的签名必须是 draw(df) 单参数，"
                f"现在是 {n_pos} 个位置参数（其中 {required} 个必填）")
    # 关键字-only 参数带默认值无妨，必填的关键字参数会让调用失败
    if any(d is None for d in args.kw_defaults):
        return "draw 不许有必填的关键字参数（_EPILOGUE 只会传 df 一个）"
    return ""


def _load_one(path: Path, ctx: dict) -> LoadResult:
    """加载单个插件文件，返回结局（绝不抛给调用方）。"""
    res = LoadResult(path=str(path), ok=False)

    # 模块名要唯一：两个插件都叫 `plugin.py` 不能互相顶掉
    modname = f"rongke_plugin_{path.stem}_{abs(hash(str(path.resolve()))) % 10**8}"
    try:
        spec = importlib.util.spec_from_file_location(modname, str(path))
        if spec is None or spec.loader is None:
            res.error = "不是可导入的 Python 文件"
            return res
        mod = importlib.util.module_from_spec(spec)
        # 先注册进 sys.modules：插件内部 `from __future__` 之外的自引用
        # 与 dataclass 装饰器都要求模块在 sys.modules 里，否则会炸。
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
    except Exception as e:                                   # noqa: BLE001
        res.error = f"{type(e).__name__}: {e}"
        res.hint = "插件文件 import 阶段就报错了；单独 python 跑一下它看完整回溯"
        # 详细回溯留在 stderr 里，方便开发者自查，但不污染人看的台账
        res.description = traceback.format_exc(limit=3)
        sys.modules.pop(modname, None)
        return res

    # 取模块级入口：优先 PLUGIN，其次一个名叫 plugin 的实例
    obj = getattr(mod, "PLUGIN", None)
    if obj is None:
        cand = getattr(mod, "plugin", None)
        if cand is not None and not isinstance(cand, type):
            obj = cand
    if obj is None:
        res.error = "找不到入口 —— 需要模块级的 PLUGIN 变量"
        res.hint = ("在插件文件末尾写 `PLUGIN = MyPlugin()`，"
                    "或直接 `PLUGIN = MyPlugin`（类也行，会自动实例化）")
        return res
    if isinstance(obj, type):
        try:
            obj = obj()
        except Exception as e:                              # noqa: BLE001
            res.error = f"插件类实例化失败：{type(e).__name__}: {e}"
            res.hint = "检查 __init__ 是否需要参数（插件必须能无参构造）"
            return res

    # ── 身份 ──
    res.name = str(getattr(obj, "name", "") or path.stem)
    res.version = str(getattr(obj, "version", "") or "0.0.0")
    res.description = str(getattr(obj, "description", "") or "")

    # ── 依赖 ──
    req = getattr(obj, "requires", ()) or ()
    if isinstance(req, str):
        req = (req,)
    missing = _missing_modules(tuple(req))
    if missing:
        res.error = "缺依赖：" + ", ".join(missing)
        res.hint = _hint_for_missing(missing)
        return res

    # ── 工具 ──
    try:
        got = list(obj.tools() or ())
    except Exception as e:                                  # noqa: BLE001
        res.error = f"tools() 抛错：{type(e).__name__}: {e}"
        res.hint = "tools() 必须返回 Tool 列表，不要在生成过程中做重活"
        return res

    reg = ctx.get("registry")
    for t in got:
        for attr in ("name", "description", "parameters", "fn"):
            if not hasattr(t, attr):
                res.skipped.append(f"工具 {getattr(t, 'name', '?')} 缺字段 {attr}")
                break
        else:
            if not str(t.description or "").strip():
                res.skipped.append(f"工具 {t.name} 没有 description（模型看不见它会干什么）")
                continue
            # 工具来源统一盖章：插件给自己标了别的 source 也纠正过来，
            # 否则 `by_source("plugin")` 的审计会漏掉它。
            try:
                t.source = "plugin"
            except Exception:
                pass
            if reg is not None:
                try:
                    reg.register(t, overwrite=bool(getattr(obj, "override", False)))
                except ValueError as e:
                    res.skipped.append(f"工具 {t.name} 没注册上：{e}")
                    continue
            res.tools.append(t.name)

    # ── 图型 ──
    try:
        kinds = dict(obj.chart_kinds() or {})
    except Exception as e:                                  # noqa: BLE001
        res.skipped.append(f"chart_kinds() 抛错：{type(e).__name__}: {e}")
        kinds = {}

    for k, meta in kinds.items():
        # ★ 必须带 template，否则 recommend() 会推荐一个画不出来的图型。
        #   这是本层最容易造成的"看起来扩展了、实际是空壳"的坑。
        if not isinstance(meta, dict) or not str(meta.get("template") or "").strip():
            res.skipped.append(f"图型 {k} 没有 template（只登记元数据会让建议画不出图）")
            continue
        for need in ("family", "name", "needs"):
            if not str(meta.get(need, "")).strip():
                res.skipped.append(f"图型 {k} 缺 {need}")
                break
        else:
            # ★ 静态校验 draw 的签名：必须是 `draw(df)` 单参数。
            #   `_EPILOGUE` 里写的是 `fig = draw(load(table))` —— 多一个参数
            #   会在**真跑**时报 `TypeError: missing 1 required positional
            #   argument`。那个错误直到出图那一刻才出现，而在此之前
            #   图型已经注册成功、已经被 recommend() 推荐、脚本也生成好了。
            #   实测踩过：一个插件图型"装上了"，用户点画才炸。
            bad_sig = _check_draw_signature(str(meta["template"]))
            if bad_sig:
                res.skipped.append(f"图型 {k} 的 {bad_sig}")
                continue
            res.chart_kinds.append(k)

    # ── 回调 ──
    # ★ 图注模板先接线（在 on_load 之前），因为 on_load 可能想覆盖它。
    try:
        lts = dict(obj.legend_templates() or {})
    except Exception as e:                                  # noqa: BLE001
        res.skipped.append(f"legend_templates() 抛错：{type(e).__name__}: {e}")
        lts = {}
    if lts:
        try:
            from . import analysis as _A
            for jid, tpl in lts.items():
                if not str(jid or "").strip():
                    res.skipped.append("图注模板缺期刊名（键不能为空）")
                    continue
                if not str(tpl or "").strip():
                    res.skipped.append(f"图注模板 {jid} 是空的")
                    continue
                _A.LEGEND_TEMPLATES[str(jid).strip().lower()] = str(tpl)
            res.legend_templates = sorted(
                str(j).strip().lower() for j in lts if str(j).strip())
        except Exception as e:                              # noqa: BLE001
            res.skipped.append(f"图注模板没接进 analysis：{type(e).__name__}: {e}")

    try:
        obj.on_load(ctx)
    except Exception as e:                                  # noqa: BLE001
        res.skipped.append(f"on_load 抛错：{type(e).__name__}: {e}")

    # 什么都没产出时也要说清楚 —— 否则"装了但没用"会被误认为成功
    if not res.tools and not res.chart_kinds and not res.legend_templates:
        res.ok = False
        res.error = "插件加载了但什么都没产出（没有工具、没有图型、没有图注模板）"
        res.hint = ("确认 tools() / chart_kinds() / legend_templates() "
                    "至少有一个返回了内容")
        return res

    # ★ 部分失败不算成功。插件同时声明了工具和图型时，图型因为缺 template
    #   被拒，上面那条"至少产出了一样"会给它开绿灯 —— 用户看到一个 ✔，
    #   以为图型也装上了，实际 recommend() 永远不会推荐它。
    #   这正是"看起来扩展了、实际是空壳"的那类坑，必须在状态上就说实话。
    if res.skipped:
        res.ok = False
        res.error = f"部分条目被拒（{len(res.skipped)} 条），插件未完全生效"
        res.hint = "逐条看下面的 ⚠ 跳过原因；改好后重跑 rongke --plugins"
        return res

    res.ok = True
    return res


def load_plugins(registry=None, *, dirs: list[Path] | None = None,
                 cfg=None, apply_chart_kinds: bool = True) -> LoadReport:
    """扫描并加载全部插件。

    `registry` 给了就把工具注册进去；不给就只做校验（dry-run）。
    `apply_chart_kinds` 为真时把图型登记进 `analysis.CHART_KINDS`/`_BODY`。

    **不抛异常**：任何单个插件的失败都变成一条 `LoadResult`。
    整个函数只在参数彻底不对时才抛。
    """
    report = LoadReport()
    from .config import data_root as _dr
    ctx: dict[str, Any] = {"registry": registry, "data_root": _dr()}

    files = discover(dirs if dirs is not None else plugin_dirs(cfg))
    report.scanned = len(files)

    seen_names: dict[str, str] = {}
    for f in files:
        res = _load_one(f, ctx)
        # 插件重名：后加载的跳过（不让一个插件悄悄顶掉另一个）
        if res.ok and res.name in seen_names:
            res.ok = False
            res.error = f"插件名重名：{res.name} 已被 {seen_names[res.name]} 占用"
            res.hint = "改一个插件的 name 属性"
        elif res.ok:
            seen_names[res.name] = str(f)

        if res.ok and apply_chart_kinds and res.chart_kinds:
            _apply_kinds(f, res)
        report.results.append(res)

    return report


def _apply_kinds(path: Path, res: LoadResult) -> None:
    """把插件图型真正接进 analysis 的登记表与模板表。

    ★ 两处必须同时写：`CHART_KINDS`（建议器读它）与 `_BODY`（生成脚本读它）。
      只写前者 ⇒ recommend() 会建议一个 build_script() 找不到模板的图型，
      结果是"建议了但画不出来"。
    """
    try:
        from . import analysis as A
    except Exception as e:                                  # noqa: BLE001
        res.skipped.append(f"图型没能接进 analysis：{e}")
        return

    # 重新加载这个插件拿到 meta（_load_one 只留了键名）
    # 避免二次 exec：从已注册的模块里取。模块名规则与 _load_one 一致。
    meta_map: dict[str, dict] = {}
    for modname, m in list(sys.modules.items()):
        if not modname.startswith("rongke_plugin_"):
            continue
        obj = getattr(m, "PLUGIN", None)
        if obj is None:
            continue
        if isinstance(obj, type):
            try:
                obj = obj()
            except Exception:
                continue
        try:
            got = dict(obj.chart_kinds() or {})
        except Exception:
            continue
        if str(getattr(obj, "name", "") or "") == res.name:
            meta_map = got
            break

    for k in res.chart_kinds:
        meta = meta_map.get(k) or {}
        if not meta:
            res.skipped.append(f"图型 {k} 的元数据在接入时丢了")
            continue
        if k in A.CHART_KINDS:
            res.skipped.append(f"图型 {k} 与内置重名，跳过")
            continue
        A.CHART_KINDS[k] = {
            "family": str(meta["family"]),
            "name": str(meta["name"]),
            "needs": str(meta["needs"]),
        }
        A._BODY[k] = str(meta["template"])

        # ★ 适用条件（可选）：没有它，这个图型**永远不会被自动建议** ——
        #   recommend() 的内置判据是硬编码的，不认识插件图型。
        #   登记了 applies 的插件才真正"长进了建议器"。
        #
        #   ★ 注意这里用 `notes` 而不是 `skipped`：缺 applies 不是**错误**，
        #     图型照样能通过 `kind=` 显式指定来用。把它算成"跳过"会让
        #     一个完全合规的插件被判成失败 —— 我第一版就这么做了，
        #     结果 [1] 号测试立刻翻红，是个正确的拦下。
        applies = meta.get("applies")
        if callable(applies):
            A.PLUGIN_SCOPERS[k] = applies
        else:
            res.notes.append(
                f"图型 {k} 没声明 applies —— 不会被自动建议（可显式 kind={k}）")
