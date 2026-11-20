# -*- coding: utf-8 -*-
"""常驻插件：中文期刊综述稿件体例 + 稿件台可视化（2026-11-09）。

## 为什么是「常驻」而不是预设

稿件台本来就是前端，格式体例是每场写稿都该在场的基础约束 —— 不该让人
先点一个胶囊才生效。本文件声明 `ALWAYS_ON = True`，由 `caps.mount_preset()`
无条件装载；**任何预设 JSON 都不点名它**，前端也没有对应胶囊。删掉这个
`.py`，整条能力自动退回原样（注册表里没有 ⇒ 不挂载、不报错）。

纯计算与视图渲染逻辑已下沉至 `rongke/manuscript.py`，本文件只保留常驻能力
装配与三个 AI 工具薄封装，并按原名再导出全部领域函数以保持兼容。
"""
from __future__ import annotations

import json
from pathlib import Path

from rongke.caps import register
from rongke.manuscript import (
    VOICE,
    _CSS,
    _RE_BRACKET_HEAD,
    _RE_CHAPTER,
    _RE_CITE,
    _RE_CN_CHAPTER,
    _RE_CN_NUM,
    _RE_INTERNAL_DOCID,
    _RE_MD_HEAD,
    _RE_MULTI_NUM,
    _RE_PLAIN_NUM,
    _RE_PROCESS_NOTE,
    _RE_REF_ENTRY,
    _RE_ZERO_HEAD,
    _lint_metrics,
    _write_back,
    check,
    desk_data,
    normalize,
    render_html,
    toc_of,
)
from rongke.tool_sdk import DeskCapability, register_tool_specs, safe_workspace_path

#: 常驻标记：`caps.always_on_capabilities()` 只认这个。
ALWAYS_ON = True

_SECTION = "external-manuscript-desk"

__all__ = [
    "ALWAYS_ON", "VOICE", "ManuscriptDesk",
    "toc_of", "_lint_metrics", "check", "normalize",
    "desk_data", "render_html", "_write_back",
    "_RE_MD_HEAD", "_RE_BRACKET_HEAD", "_RE_CN_CHAPTER", "_RE_CN_NUM",
    "_RE_MULTI_NUM", "_RE_PLAIN_NUM", "_RE_CHAPTER", "_RE_CITE",
    "_RE_REF_ENTRY", "_RE_INTERNAL_DOCID", "_RE_PROCESS_NOTE", "_RE_ZERO_HEAD",
    "_CSS",
]


def _register_tools(ctx: dict, cfg: dict) -> list[str]:
    registry = ctx.get("registry")
    if registry is None:
        return []
    keep_backup = bool(cfg.get("keep_backup", True))
    page = str(cfg.get("page") or "稿件台.html")
    data_name = str(cfg.get("data") or "稿件台.json")

    def _read(text: str, path: str) -> tuple[str, str]:
        if str(text or "").strip():
            return str(text), "(传入全文)"
        raw = str(path or "").strip() or "综述.md"
        try:
            p = safe_workspace_path(raw)
            return p.read_text(encoding="utf-8", errors="replace"), str(raw)
        except (ValueError, OSError) as ex:
            return "", f"读不到 {raw}：{type(ex).__name__}"

    def _fn_check(text: str = "", path: str = "") -> str:
        body, src = _read(text, path)
        if not body:
            return json.dumps({"ok": False, "error": src}, ensure_ascii=False)
        r = check(body)
        # 体例要求走工具回包（过程反馈），不塞进 SYSTEM：常驻插件在场 ≠
        # 每场都改系统提示。模型调一次检查就拿到同一份单源正文。
        return json.dumps({"ok": True, "source": src, "hard": r["hard"],
                           "soft": r["soft"], "issues": r["issues"],
                           "metrics": r["metrics"], "rules": VOICE},
                          ensure_ascii=False)

    def _fn_fix(text: str = "", path: str = "", out: str = "") -> str:
        body, src = _read(text, path)
        if not body:
            return json.dumps({"ok": False, "error": src}, ensure_ascii=False)
        new, changes = normalize(body)
        raw_out = str(out or "").strip() or (str(path or "").strip() if path else "综述_规范.md")
        try:
            target = safe_workspace_path(raw_out)
        except ValueError as ex:
            return json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False)
        if str(path or "").strip() and not str(out or "").strip() and keep_backup:
            try:
                srcp = safe_workspace_path(str(path))
                srcp.with_suffix(srcp.suffix + ".bak").write_text(
                    srcp.read_text(encoding="utf-8"), encoding="utf-8")
            except (ValueError, OSError):
                pass
        ok = _write_back(target, new)
        after = check(new)
        return json.dumps({"ok": ok, "output": str(raw_out), "changes": changes,
                           "before": check(body)["hard"], "after": after["hard"],
                           "issues_after": after["issues"]}, ensure_ascii=False)

    def _fn_desk(text: str = "", path: str = "") -> str:
        body, src = _read(text, path)
        if not body:
            return json.dumps({"ok": False, "error": src}, ensure_ascii=False)
        d = desk_data(body)
        try:
            data_dest = safe_workspace_path(data_name)
            page_dest = safe_workspace_path(page)
        except ValueError as ex:
            return json.dumps({"ok": False, "error": str(ex)}, ensure_ascii=False)
        ok_json = _write_back(data_dest,
                              json.dumps(d, ensure_ascii=False, indent=1))
        ok_html = _write_back(page_dest, render_html(d))
        return json.dumps({
            "ok": ok_json and ok_html, "page": page, "data": data_name,
            "source": src, "chapters": len(d["blocks"]),
            "hard": d["report"]["hard"], "soft": d["report"]["soft"],
            "hint": f"前端产物列表里点开 {page} 即可查看（/api/artifact 不带 dl 时按 text/html 内联）",
        }, ensure_ascii=False)

    specs = [
        ("manuscript_check", "读回稿件体例报告：零号章、章号连续、层级、摘要关键词、"
         "引注与文献表对应、内部标注。只给信息，不拦交付。",
         {"text": {"type": "string", "description": "稿件全文；留空则从 path 读"},
          "path": {"type": "string", "description": "工作区内稿件相对路径"}},
         [], _fn_check, False),
        ("manuscript_fix", "确定性规范化稿件格式并落盘：引言去零号、前置字段统一、"
         "文献表去掉过程说明与内部库标注、清理行尾空白。不改编号、正文与引注；"
         "写后读回校验，原件留 .bak。",
         {"text": {"type": "string", "description": "稿件全文；留空则从 path 读"},
          "path": {"type": "string", "description": "待修稿件相对路径"},
          "out": {"type": "string", "description": "输出路径；留空写 综述_规范.md"}},
         [], _fn_fix, True),
        ("manuscript_desk", "生成稿件台视图：结构化数据 + 自包含 HTML（章节树、"
         "阅读进度、稿面指标、体例检查、引注覆盖），落盘并写后读回。",
         {"text": {"type": "string", "description": "稿件全文；留空则从 path 读"},
          "path": {"type": "string", "description": "工作区内稿件相对路径"}},
         [], _fn_desk, True),
    ]
    return register_tool_specs(registry, specs)


@register("@external/manuscript-desk")
class ManuscriptDesk(DeskCapability):
    """常驻能力：稿件体例（上游约束）+ 三个工具 + 稿件台视图数据。"""

    ALWAYS_ON = True
    id = "manuscript-desk"
    kind = "tool"
    version = "1.0.0"
    config_schema: dict[str, dict] = {}
    ALWAYS_CONFIG = {"page": "稿件台.html", "data": "稿件台.json",
                     "keep_backup": True}
    SECTION_TEXTS = {_SECTION: VOICE}
    ESSENTIAL_TOOLS = ("manuscript_check", "manuscript_fix", "manuscript_desk")

    def mount(self, cfg, ctx):
        self.cfg = dict(cfg or {})
        self.added = _register_tools(ctx, self.cfg)
        return [f"体例段 {_SECTION}（{len(VOICE)} 字符）",
                f"注册工具 {len(self.added)} 个：{'、'.join(self.added)}",
                "格式检查只进过程反馈，不加门"]

    def provides(self) -> list[str]:
        return ["always-on:manuscript-desk", f"prompt:{_SECTION}",
                f"tools:{len(getattr(self, 'added', []))}/3",
                "desk:稿件台.json + 稿件台.html"]
