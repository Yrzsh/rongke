# -*- coding: utf-8 -*-
"""AI 画布接入层 —— 把 `aideck/` 那个独立工具挂成荣珂主循环能直接调的工具。

## 为什么是薄薄一层

用户要的是「一个专门给 AI 用的工具」（`aideck/` 独立包 + 画布窗口 + HTTP 服务），
不是荣珂流程里的又一层封装。所以这里**不重写任何画布逻辑**：
`canvas_act` 只是把动作转交给 `aideck.Canvas.apply()`，再把读回原样交回模型。

## 两条纪律（都是踩过的坑）

  · **`mount()` 绝不 import aideck**：健康只认 `MountReport.broken`，
    能力放在发行件里没打进来时若让 mount 抛异常，会把**所有预设**判成 broken。
    ⇒ 延迟到工具 fn 里导入；拿不到就回 `CANVAS_BACKEND_UNAVAILABLE`，
    **明说能力不可用**（11-10 D 场「静默降级比报错更伤」）。
  · **不依赖 cwd**：内核会 `os.chdir(工作区)`，直接 `import aideck` 会飘。
    按本文件 `__file__` 反查项目根，必要时补进 `sys.path`，并读回确认
    拿到的确实是 `aideck` 包（不是同名模块）。

  · 常驻（`ALWAYS_ON`）、不进预设、不改 SYSTEM、不加硬门。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from rongke.caps import register
from rongke.tool_sdk import DeskCapability, register_tool_specs, safe_workspace_path

ALWAYS_ON = True

_ID = "canvas-desk"
_SECTION = "external-canvas-desk"

VOICE = """【AI 画布 · 常驻】
要像在 Origin 里摆弄图元那样做 PPT，就调 canvas_act：
一次一个动作（new_page / add / set / move / remove / duplicate / align / query / audit），
工具自己维护画布、每步写后读回，并用 query() 回答「现在这页有什么」、
用 audit() 报越界/重叠/离网格。改一个框只发一个 move，不用重写整份稿。
做完调 canvas_export 导出 deck.pptx（会逐行读回文字、按 part 读回图表）。
画布窗口是 画布.html，人可以在浏览器里实时看。"""

_TEMPLATE = {
    "new_page": {"op": "new_page", "kind": "content", "title": "第一页"},
    "add": {"op": "add", "type": "text", "box": [72, 36, 1136, 72],
            "text": "这一页的标题", "size": 40},
    "bullets": {"op": "add", "type": "bullets", "box": [72, 140, 1136, 380],
                "items": ["要点一", "要点二"], "size": 26},
    "chart": {"op": "add", "type": "chart", "box": [656, 140, 552, 380],
              "chart": {"title": "三组对照", "categories": ["A", "B", "C"],
                        "series": [{"name": "处理组", "values": [1.2, 2.4, 3.1]}]}},
    "query": {"op": "query"},
    "audit": {"op": "audit"},
    "move": {"op": "move", "eid": "<query 里查到的 id>", "box": [72, 300, 500, 80]},
    "set": {"op": "set", "eid": "<id>", "text": "改过的文字"},
}


def _load_aideck():
    """按 `__file__` 反查项目根 + 读回确认，返回 aideck 模块或 None。"""
    root = Path(__file__).resolve().parents[2]
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)
    try:
        import aideck
    except Exception:                                        # noqa: BLE001
        return None
    # 读回：拿到的得真是那个包（有 Canvas / render_html / export_pptx）。
    if not all(hasattr(aideck, n) for n in ("Canvas", "render_html", "export_pptx")):
        return None
    return aideck


def _root_of(raw: str):
    """工作区围栏：root 必须是 cwd 之内（或就是 cwd）的相对路径。"""
    base = Path.cwd().resolve()
    s = str(raw or "").strip()
    if not s:
        return base
    try:
        return safe_workspace_path(s, base, allow_abs=False)
    except ValueError:
        raise ValueError(f"root 越出工作区：{s}") from None


def _next_for(op: str, rep: dict) -> str:
    hard = [g for g in (rep.get("gaps") or []) if g.get("level") == "hard"]
    if hard:
        return ("画布有 hard 缺口，按 gaps[].action 逐个修（一般是 canvas_act 发一个 move）；"
                "修完再 audit 一次，然后 canvas_export")
    if op in ("new_page", "add", "set", "move", "duplicate", "align"):
        return "调 canvas_act({\"op\":\"query\"}) 看当前页，或 {\"op\":\"audit\"} 看缺口；交付用 canvas_export"
    if op == "query":
        return "拿到 id 后可用 move/set/remove/duplicate 只改一个图元；不用重写整份稿"
    if op == "audit":
        return "缺口为空就可以 canvas_export 了"
    if op == "remove":
        return "删完建议 audit 一次确认没有空页/重叠"
    return "继续用 canvas_act 发下一个动作；模板见 canvas_act() 空参数回包"


def _register_tools(ctx: dict, cfg: dict) -> list[str]:
    registry = ctx.get("registry")
    if registry is None:
        return []

    def _unavailable() -> str:
        return json.dumps({
            "ok": False, "error": "CANVAS_BACKEND_UNAVAILABLE：找不到 aideck 画布引擎"
                                  "（源码态在项目根 aideck/；发行件需把它打进包）",
            "hint": "别反复重试；检查项目根是否有 aideck/canvas.py、aideck/studio.py",
            "advisory": True,
        }, ensure_ascii=False)

    def _fn_act(action: str = "", root: str = "") -> str:
        mod = _load_aideck()
        if mod is None:
            return _unavailable()
        try:
            base = _root_of(root)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        s = str(action or "").strip()
        if not s:
            return json.dumps({
                "ok": True, "source": "canvas_act", "advisory": True,
                "what": "AI 画布：一次一个图元级动作，工具维护状态并每步写后读回",
                "actions": list(_TEMPLATE), "template": _TEMPLATE,
                "canvas": "1280×720，1px=9525EMU；图元坐标即最终坐标",
                "next": "先发 {\"op\":\"new_page\"} 开一页，再发 {\"op\":\"add\", ...} 放图元；"
                        "随时用 {\"op\":\"query\"} 看现状",
                "rules": VOICE,
            }, ensure_ascii=False)
        try:
            obj = json.loads(s) if s.startswith(("{", "[")) else None
        except ValueError as e:
            return json.dumps({"ok": False, "error": f"action 不是合法 JSON：{e}",
                               "hint": "动作要写成 JSON 对象字符串，或留空看模板"},
                              ensure_ascii=False)
        if obj is None:
            return json.dumps({"ok": False, "error": "action 必须是 JSON 对象字符串",
                               "hint": "留空可看全部动作模板"}, ensure_ascii=False)
        try:
            c = mod.Canvas(base)
            rep = c.apply(obj)
            op = str(obj.get("op") or "").lower()
            # 每次动作都重画布台面 —— 人这边的窗口不该停在旧状态。
            try:
                h = mod.render_html(c)
                rep["view"] = h.get("page")
                rep["view_pages"] = h.get("pages")
            except Exception as e:                            # noqa: BLE001
                # 台面失败不能吞掉动作结果，但**必须说出口**（不静默降级）。
                rep["view_error"] = f"{type(e).__name__}: {e}"
            rep["source"] = str(base / "画布.json")
            rep["next"] = _next_for(op, rep)
            rep["advisory"] = True
            return json.dumps(rep, ensure_ascii=False)
        except Exception as e:                                # noqa: BLE001
            return json.dumps({"ok": False,
                               "error": f"画布动作出错：{type(e).__name__}: {e}"},
                              ensure_ascii=False)

    def _fn_export(root: str = "", out: str = "deck.pptx") -> str:
        mod = _load_aideck()
        if mod is None:
            return _unavailable()
        try:
            base = _root_of(root)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        try:
            c = mod.Canvas(base)
            rep = mod.export_pptx(c, out=out)
            rep["advisory"] = True
            rep["next"] = ("交付前照旧调 audit_deck 做五门体检；"
                           "本画布工具只保证图元与文字都读得回，不替代体检")
            return json.dumps(rep, ensure_ascii=False)
        except Exception as e:                                # noqa: BLE001
            return json.dumps({"ok": False,
                               "error": f"导出出错：{type(e).__name__}: {e}"},
                              ensure_ascii=False)

    specs = [
        ("canvas_act",
         "AI 画布：一次一个**图元级**动作。工具维护持久化画布，每步写后读回，"
         "并用 query 回答「现在这页有什么」、用 audit 报越界/重叠/离网格。"
         "改一个框只发一个 move，不用重写整份稿。留空调用可拿到全部动作模板。"
         "动作：new_page / add / set / move / remove / duplicate / align / query / audit。",
         {"action": {"type": "string", "description": "动作 JSON 对象字符串，如 {\"op\":\"add\",\"type\":\"text\",\"box\":[72,36,1136,72],\"text\":\"标题\"}；留空给模板"},
          "root": {"type": "string", "description": "画布工作目录；留空用当前工作区"}},
         [], _fn_act, True),
        ("canvas_export",
         "把当前 AI 画布导出为 .pptx（图元坐标即最终坐标，不重排），并逐行读回文字、"
         "按 part 读回图表；同时刷新 画布.html 窗口。",
         {"root": {"type": "string", "description": "画布工作目录；留空用当前工作区"},
          "out": {"type": "string", "description": "输出文件名，默认 deck.pptx"}},
         [], _fn_export, True),
    ]
    return register_tool_specs(registry, specs)


@register("@external/canvas-desk")
class CanvasDesk(DeskCapability):
    ALWAYS_ON = True
    id = _ID
    kind = "tool"
    version = "0.1.0"
    config_schema = {}
    ALWAYS_CONFIG = {}
    SECTION_TEXTS = {_SECTION: VOICE}
    ESSENTIAL_TOOLS = ("canvas_act", "canvas_export")

    def mount(self, cfg, ctx):
        self.cfg = dict(cfg or {})
        # 刻意不在这里 import aideck：挂载失败会把所有预设判成 broken。
        self.added = _register_tools(ctx, self.cfg)
        return [f"always-on:{self.id}:{n}" for n in self.added]

    def provides(self):
        return [f"always-on:{self.id}:{n}" for n in getattr(self, "added", ())]
