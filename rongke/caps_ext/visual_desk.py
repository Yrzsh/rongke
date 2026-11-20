# -*- coding: utf-8 -*-
"""图表编排台 —— 给 agent 用的「图表进正文」原子能力（2026-11-10）。

纯计算与落位算法已下沉至 `rongke/visual_layout.py`，本文件只保留常驻能力装配与
三个 AI 工具薄封装，并按原名再导出全部领域函数与模式常量。

边界（与既有裁决一致）：
  · 常驻加载，不进预设、不加胶囊（`ALWAYS_ON`，删 .py 自动退回）。
  · 不改 SYSTEM：体例要求走工具回包，baseline 逐字节等价。
  · 不加硬门：只给过程反馈（`"advisory": True`），不接任何硬交付门。
  · 预览级，不冒充投稿级：`figure` 走 `svgplot.render_svg()`，`scope="preview"`。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
from pathlib import Path

from rongke.caps import register
from rongke import visual_layout as _VL
from rongke.tool_sdk import DeskCapability, register_tool_specs

ALWAYS_ON = True

_ID = "visual-desk"
_SECTION = "external-visual-desk"

VOICE = _VL.VOICE
_RE_HEAD = _VL._RE_HEAD
_RE_REFS = _VL._RE_REFS
_RE_CAPTION = _VL._RE_CAPTION
_RE_IMAGE = _VL._RE_IMAGE
_RE_XREF = _VL._RE_XREF
_RE_NO_VISUAL = _VL._RE_NO_VISUAL
_chapter_heads = _VL._chapter_heads
_caption_entries = _VL._caption_entries
_xref_numbers = _VL._xref_numbers
_rows_from_data = _VL._rows_from_data
_safe_rel = _VL._safe_rel
_find_chapter = _VL._find_chapter


@contextmanager
def _sync_vl_hooks():
    """若调用方/承重测试在插件模块上短路 `_RE_CAPTION` 或 `_caption_entries`，临时同步到 `visual_layout`。"""
    old_re = _VL._RE_CAPTION
    old_ce = _VL._caption_entries
    if _RE_CAPTION is not old_re:
        _VL._RE_CAPTION = _RE_CAPTION
    if _caption_entries is not old_ce:
        _VL._caption_entries = _caption_entries
    try:
        yield
    finally:
        _VL._RE_CAPTION = old_re
        _VL._caption_entries = old_ce


def chapter_ranges(text: str) -> list[dict]:
    with _sync_vl_hooks():
        return _VL.chapter_ranges(text)


def scan_visuals(text: str, root: "Path | str | None" = None,
                 *, expect_visual: bool = False) -> dict:
    with _sync_vl_hooks():
        return _VL.scan_visuals(text, root, expect_visual=expect_visual)


def next_number(text: str, kind: str) -> int:
    with _sync_vl_hooks():
        return _VL.next_number(text, kind)


def place_visual(text: str, *, chapter: str, kind: str, caption: str,
                 claim: str = "", data: str = "", file: str = "",
                 root: "Path | str | None" = None,
                 anchor_id: str = "") -> tuple[str, dict]:
    with _sync_vl_hooks():
        return _VL.place_visual(
            text, chapter=chapter, kind=kind, caption=caption,
            claim=claim, data=data, file=file, root=root,
            anchor_id=anchor_id,
        )


def _register_tools(ctx: dict, cfg: dict) -> list[str]:
    registry = ctx.get("registry")
    if registry is None:
        return []

    def _read(path: str) -> tuple[str, Path | None, str]:
        root = Path.cwd()
        p = Path(str(path or "").strip()) if str(path or "").strip() else None
        if p is None:
            cands = sorted(root.glob("*.md"), key=lambda x: x.stat().st_size, reverse=True)
            cands = [x for x in cands if not re.search(
                r"outline|大纲|提纲|说明|计划|检索|fix|note", x.name, re.I)]
            if not cands:
                return "", None, "工作区里没找到稿件 .md"
            p = cands[0]
        try:
            q = _safe_rel(root, p)
            return q.read_text(encoding="utf-8", errors="replace"), q, str(q)
        except Exception as e:                             # noqa: BLE001
            return "", None, f"读不到 {p}：{type(e).__name__}: {e}"

    def _fn_plan(path: str = "") -> str:
        body, q, src = _read(path)
        if not body:
            return json.dumps({"ok": False, "error": src}, ensure_ascii=False)
        r = scan_visuals(body, q.parent if q else Path.cwd())
        c = r["counts"]
        todo = [{"chapter": ch, "status": "已有图表/已说明理由" if i in r["chapters_covered"]
                 else "待补：调 visual_place 放一处图或表，或写明本章不配图的理由"}
                for i, ch in enumerate(r["chapters"])]
        return json.dumps({
            "ok": True, "source": src, "counts": c, "gaps": r["gaps"],
            "chapters": todo,
            "next": (f"逐章覆盖 {c['chapters_with_visual']}/{c['chapters']}；"
                     f"先调 visual_place(chapter=章号, kind='table'|'figure', "
                     f"caption=一句图注, data=数据) 把图表落进对应章节"),
            "rules": VOICE, "advisory": True,
        }, ensure_ascii=False)

    def _fn_place(chapter: str = "", kind: str = "table", caption: str = "",
                  claim: str = "", data: str = "", file: str = "",
                  path: str = "", anchor_id: str = "") -> str:
        body, q, src = _read(path)
        if not body:
            return json.dumps({"ok": False, "error": src}, ensure_ascii=False)
        root = q.parent if q else Path.cwd()
        new, rep = place_visual(body, chapter=chapter, kind=kind, caption=caption,
                                claim=claim, data=data, file=file, root=root,
                                anchor_id=anchor_id)
        if rep.get("ok") and rep.get("changed") and q is not None:
            try:
                q.write_text(new, encoding="utf-8")
                rep["written"] = str(q)
                rep["readback_equal"] = q.read_text(encoding="utf-8") == new
                if not rep["readback_equal"]:
                    rep["ok"] = False
                    rep["error"] = "落盘后读回不一致"
            except Exception as e:                         # noqa: BLE001
                rep["ok"] = False
                rep["error"] = f"落盘失败：{type(e).__name__}: {e}"
        rep["source"] = src
        return json.dumps(rep, ensure_ascii=False)

    def _fn_renumber(path: str = "", write: bool = True) -> str:
        """删图 / 重排章节之后，按登记重编全篇编号并五连读回。"""
        from rongke import figure_registry as FR
        body, q, src = _read(path)
        if not body:
            return json.dumps({"ok": False, "error": src,
                               "scope": "figure-registry", "advisory": True},
                              ensure_ascii=False)
        root = q.parent if q else Path.cwd()
        reg, st = FR.load(root)
        if st == "absent":
            return json.dumps(
                {"ok": False, "scope": "figure-registry", "advisory": True,
                 "registry_status": "absent",
                 "error": "工作区没有 图表登记.json（unknown，不算通过）："
                          "先给 visual_place 传 anchor_id 建登记"},
                ensure_ascii=False)
        if st.startswith("error:"):
            return json.dumps({"ok": False, "scope": "figure-registry",
                               "advisory": True, "registry_status": st,
                               "error": f"登记表读不回来：{st}"},
                              ensure_ascii=False)
        new, rn = FR.renumber(body, reg)
        clean, rr = FR.render_markdown(new, reg)
        vr = FR.verify(root, new, reg, rendered=clean)
        rep = {"ok": bool(rn["ok"] and vr["ok"] and rr["ok"]),
               "source": src, "scope": "figure-registry", "advisory": True,
               "registry_status": "ok", "numbering": rn["numbering"],
               "changed": rn["changed"], "mixed_mode": rn["mixed_mode"],
               "note": rn["note"], "gaps": vr["gaps"],
               "renumber": {"drift": rn["drift"],
                            "orphan_ids": rn["orphan_ids"]},
               "next": ("编号已重编并落盘" if (rn["ok"] and vr["ok"] and rr["ok"])
                        else "按 gaps 的 action 修稿/修登记，再调一次")}
        if rep["ok"] and write and rn["changed"] and q is not None:
            try:
                q.write_text(new, encoding="utf-8")
                rep["written"] = str(q)
                rep["readback_equal"] = q.read_text(encoding="utf-8") == new
                if not rep["readback_equal"]:
                    rep["ok"] = False
                    rep["error"] = "落盘后读回不一致"
            except Exception as e:                         # noqa: BLE001
                rep["ok"] = False
                rep["error"] = f"落盘失败：{type(e).__name__}: {e}"
        return json.dumps(rep, ensure_ascii=False)

    specs = [
        ("visual_plan",
         "看稿件逐章的图表覆盖现状与待补清单（只读）。写完大纲、动笔前先调一次；"
         "返回每章有没有图表、图表有没有插进正文、正文有没有呼应、编号与文件是否齐。",
         {"path": {"type": "string", "description": "稿件 .md 路径；留空自动找工作区里最大的正文 .md"}},
         [], _fn_plan, False),
        ("visual_place",
         "把一处图/表放进**指定章节的正文里**（不是文末附录）。一次调用自动完成："
         "分配编号 → 用 data 生成预览级 SVG 或引用已有 file → 插到该章末尾 → "
         "写题注（表上图下）→ 写一句正文交叉引用 → 写后读回验证。幂等，同图注不重复插。"
         "给了 anchor_id 就同时写 图表登记.json：正文只放稳定锚点，编号由工具算，"
         "之后删图/重排章节调 visual_renumber 自动重编全篇引用。",
         {"chapter": {"type": "string", "description": "章号或标题片段，如 '5'、'原生质体'"},
          "kind": {"type": "string", "enum": ["table", "figure"],
                   "description": "table=正文内 markdown 表（轻）；figure=预览级 SVG 图"},
          "caption": {"type": "string", "description": "一句图注/表题，如 '不同酶系的原生质体产量对照'"},
          "claim": {"type": "string", "description": "可选：这处图表支撑哪句论证；不填会生成保守占位句并在回包标出"},
          "data": {"type": "string", "description": "markdown 表文本或 JSON 数组 [{\"label\":\"A\",\"value\":3.2}]"},
          "file": {"type": "string", "description": "可选：已有图文件路径（如投稿级 figure.png），给它就只做落位不出新图"},
          "anchor_id": {"type": "string", "description": "可选稳定 id（fig:xxx / tab:xxx）。"
                        "给了就写 图表登记.json 并只放锚点，编号由工具算；"
                        "不给则完全保持旧文本路径（产物逐字节不变）"},
          "path": {"type": "string", "description": "稿件 .md 路径；留空自动找"}},
         ["chapter", "kind", "caption"], _fn_place, True),
        ("visual_renumber",
         "删图、局部重写或重排章节之后，按 图表登记.json 里锚点的新顺序"
         "重编全篇题注与「见图 N / 见表 N」，并读回编号漂移、孤儿锚点、"
         "缺产物、缺呼应。只改编号与登记读回，不动图元、不加交付门。",
         {"path": {"type": "string", "description": "稿件 .md 路径；留空自动找"},
          "write": {"type": "boolean", "description": "默认 true 落盘；false 只回报不写"}},
         [], _fn_renumber, True),
    ]
    return register_tool_specs(registry, specs)


@register("@external/visual-desk")
class VisualDesk(DeskCapability):
    ALWAYS_ON = True
    id = _ID
    kind = "tool"
    version = "0.1.0"
    config_schema = {}
    ALWAYS_CONFIG = {}
    SECTION_TEXTS = {_SECTION: VOICE}
    ESSENTIAL_TOOLS = ("visual_plan", "visual_place", "visual_renumber")

    def mount(self, cfg, ctx):
        self.cfg = dict(cfg or {})
        self.added = _register_tools(ctx, self.cfg)
        return [f"always-on:{self.id}:{n}" for n in self.added]

    def provides(self):
        return [f"always-on:{self.id}:{n}" for n in getattr(self, "added", ())]
