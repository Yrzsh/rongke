# -*- coding: utf-8 -*-
"""常驻插件：给 AI 用的格式排版台（2026-11-11）。

## 与 `svgdiagram` 同一条纪律

模型只声明 `blocks`（哪段是标题、哪段是图注、哪条是文献），或干脆给一份
现成 .md 让 `parse_markdown()` 确定性拆块；**页边距、字号阶梯、行距、章节编号、
题注位置、参考文献样式、分页全由 `typeset.STYLES` 算**。算完立刻读回
（`checks` 全 bool、每个 block 的文字都必须在产物里），再把三套已有判据
汇成 `quality` + `next`。**不改 `ok` / `scope` / `checks` 语义，不接交付门。**

不该让人先点一个胶囊才生效 ⇒ 常驻（`ALWAYS_ON`）。
"""
from __future__ import annotations

import json
from pathlib import Path

from rongke.caps import register
from rongke import typeset as TS
from rongke.tool_sdk import DeskCapability, register_tool_specs, safe_workspace_path

ALWAYS_ON = True

_SECTION = "external-typeset-desk"
#: 规格口径单源：工具描述、提示段、零参数模板共用 `typeset.SPEC_GUIDE`。
SPEC_GUIDE = TS.SPEC_GUIDE

_TEMPLATE = {
    "style": "journal-cn",
    "meta": {"title": "论文标题", "authors": "作者", "affiliation": "单位",
             "abstract": "摘要…", "keywords": "关键词1；关键词2"},
    "blocks": [
        {"role": "title", "text": "论文标题"},
        {"role": "authors", "text": "作者"},
        {"role": "heading", "text": "引言", "level": 1},
        {"role": "paragraph", "text": "正文段落…〔1〕"},
        {"role": "figure", "text": "图 1 题名", "src": "figure.png"},
        {"role": "caption", "text": "图 1　题名"},
        {"role": "ref_entry", "text": "张三. 文献题名[J]. 刊, 2020.", "n": 1},
    ],
}


def _register_tools(ctx: dict, cfg: dict) -> list[str]:
    registry = ctx.get("registry")
    if registry is None:
        return []

    def _wd() -> Path:
        return Path.cwd()

    def _safe(root: Path, p: "str | Path") -> Path:
        return safe_workspace_path(p, root, allow_abs=False)

    def _build_spec(spec, path: str, style: str,
                    sink: dict | None = None) -> tuple[dict | None, str, str]:
        root = _wd()
        raw = spec
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError as e:
                return None, "", f"spec 不是合法 JSON：{e}"
        if raw is None and path:
            try:
                q = _safe(root, path)
                text = q.read_text(encoding="utf-8")
                # 一次全出：稿里 {{FIGURE:data.csv}} ⇒ 工具先出图、按图规格
                # 生成题注，再把展开态交给既有拆块链路（不另起一套排版）。
                if TS.has_figure_declaration(text):
                    text, figs = TS.expand_figures(text, q.parent)
                    if sink is not None:
                        sink["markdown"] = text
                        sink["figures"] = figs
                raw = TS.parse_markdown(text, style)
                return raw, str(q), ""
            except (ValueError, OSError) as e:
                return None, "", f"读不了 {path}：{type(e).__name__}: {e}"
        if raw is None:
            # 自动找工作区里最大的正文 .md（复用 shape.prose_files 的口径）
            try:
                from rongke.shape import prose_files
                cand = prose_files(root, limit=1)
            except Exception:                                # noqa: BLE001
                cand = []
            if not cand:
                return None, "", "没给 spec / path，工作区里也没找到正文 .md"
            q = cand[0][1]
            try:
                text = q.read_text(encoding="utf-8")
                if TS.has_figure_declaration(text):
                    text, figs = TS.expand_figures(text, q.parent)
                    if sink is not None:
                        sink["markdown"] = text
                        sink["figures"] = figs
                raw = TS.parse_markdown(text, style)
                return raw, str(q), ""
            except Exception as e:                            # noqa: BLE001
                return None, "", f"自动拆块失败：{type(e).__name__}: {e}"
        try:
            if "style" not in raw:
                raw = dict(raw, style=style)
            return TS.validate(raw), "", ""
        except TS.TypesetError as e:
            return None, "", str(e)

    def _fn_typeset(spec=None, path: str = "", style: str = "journal-cn",
                    out: str = "") -> str:
        if spec is None and not path:
            # 「零参数拿模板」：降低调用阻力，同 deck_plan / diagram_svg 的规格单源。
            return json.dumps({
                "ok": True, "source": "typeset", "advisory": True,
                "what": "AI 声明 blocks，工具算版式；不写任何排版标记",
                "styles": {k: v["label"] for k, v in TS.STYLES.items()},
                "roles": list(TS.ROLES), "template": _TEMPLATE,
                "template_json": json.dumps(_TEMPLATE, ensure_ascii=False),
                "guide": SPEC_GUIDE,
                "also": "也可只给 path=.md 稿件，或留空自动找工作区最大的正文 .md",
                "next": "把 template 改成你的内容调 typeset(spec=...)；"
                        "要成品文件再调 typeset_export(fmt='docx'|'tex')",
            }, ensure_ascii=False)
        auto: dict = {}
        sp, src, err = _build_spec(spec, path, style, sink=auto)
        if sp is None:
            return json.dumps({"ok": False, "error": err,
                               "next": "零参数可调 typeset() 拿规格模板"},
                              ensure_ascii=False)
        root = _wd()
        # 图表登记在场 ⇒ 从登记取图元与编号，不再从正文另猜一遍。
        # 没登记时**完全保持原路径**（演示稿与既有判据逐字节不受影响）。
        registry_rep = None
        registry_markdown = None
        # 一次全出是独立新路径：图与题注都由本轮生成，不再让所有权层
        # 拿未展开的原稿覆盖回来（否则图会凭空消失）。
        if spec is None and path and not auto.get("figures"):
            try:
                from rongke import figure_registry as FR
                pth = _safe(root, path)
                reg, st = FR.load(pth.parent)
                if st == "ok" and reg["entries"]:
                    rr = TS.render_with_registry(
                        pth.read_text(encoding="utf-8"), reg,
                        pth.parent, sp["style"])
                    if rr["ok"]:
                        sp = rr["spec"]
                        # 质量门要吃**渲染态** Markdown（已去掉身份注释、
                        # 编号已注入），不能吃带 <!--rk:...--> 的锚点源稿。
                        registry_markdown = rr["markdown"]
                    registry_rep = {
                        "status": "ok", "ok": rr["ok"],
                        "n_entries": len(reg["entries"]),
                        "numbering": rr["numbering"],
                        "gaps": rr["registry_gaps"],
                        "note": rr["note"]}
                elif st != "absent":
                    registry_rep = {"status": st, "ok": False,
                                    "note": "登记表读不回来；已按原稿排版（明说，不静默降级）"}
            except Exception as ex:                           # noqa: BLE001
                registry_rep = {"status": f"error:{type(ex).__name__}",
                                "ok": False,
                                "note": f"登记接入失败：{type(ex).__name__}: {ex}；"
                                        "已按原稿排版"}
        try:
            dest = _safe(root, out or TS.HTML_NAME)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        try:
            rep = TS.render(sp, dest)
        except TS.TypesetError as e:
            return json.dumps({"ok": False, "error": str(e),
                               "next": "这是规格不合法，改 blocks 重排一次"},
                              ensure_ascii=False)
        except Exception as e:                                # noqa: BLE001
            return json.dumps({"ok": False,
                               "error": f"排版后端出错：{type(e).__name__}: {e}"},
                              ensure_ascii=False)
        # spec sidecar：导出工具与生态总线的读回单源。
        try:
            sidecar = _safe(root, TS.STATE_NAME)
            # 把质量读回一起落进 sidecar：论文台才能把「排版质量的 19 项」
            # 呈现在交付态总览上，而不是各自藏在工具回包里。
            state = dict(sp, quality_checks=rep.get("checks") or {},
                         quality_summary=rep.get("quality_summary") or {},
                         figure_registry=registry_rep)
            sidecar.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            rep["state"] = str(sidecar)
            if registry_rep is not None:
                rep["registry"] = registry_rep
            back_state = json.loads(sidecar.read_text(encoding="utf-8"))
            rep["state_readback_equal"] = (
                back_state.get("quality_checks") == rep.get("checks")
                and back_state.get("blocks") == sp["blocks"]
                and back_state.get("figure_registry") == registry_rep)
        except Exception as e:                                # noqa: BLE001
            rep["state_error"] = f"{type(e).__name__}: {e}"
        # 质量结论另走 quality/next；拿不到的判据进 not_auditable，不等于通过。
        # ⚠️ 判据要吃**带 Markdown 标记的稿件原文**，不能吃 blocks 拼出的纯文本
        #    （丢了 `## 参考文献` 会让 manuscript_format 凭空判 MISSING_REFS）。
        #    有原稿就读原稿；只有 spec 时按 blocks 逆向重建（见 reconstruct_markdown）。
        qsrc = "unknown"
        try:
            if registry_markdown is not None:
                body = registry_markdown
                qsrc = "registry-rendered"
            elif auto.get("markdown"):
                # 质量门要吃**展开态**：带 ![图] 与自动题注，
                # 不能拿还留着 {{FIGURE:...}} 的原稿去判「图位缺题注」。
                body = auto["markdown"]
                qsrc = "markdown-expanded"
            elif src and Path(src).is_file():
                body = Path(src).read_text(encoding="utf-8", errors="replace")
                qsrc = "markdown"
            else:
                body = TS.reconstruct_markdown(sp)
                qsrc = "reconstructed"
            q = TS.audit_quality(body, root)
        except Exception as e:                                # noqa: BLE001
            q = {"ok": False, "advisory": True,
                 "error": f"{type(e).__name__}: {e}",
                 "gates": {}, "blocking": [], "fix_before_delivery": [],
                 "not_auditable": ["all"], "details": []}
        rep.update({"source": src or "(spec)",
                    "quality_source": qsrc,
                    "quality": q, "next": TS.format_next(q, rep),
                    "advisory": True})
        if auto.get("figures"):
            figs = auto["figures"]
            rep["auto_figures"] = figs
            rep["one_shot"] = {
                "n_figures": len(figs),
                "all_render_ok": all(f["render_ok"] for f in figs),
                "all_caption_matches_plan": all(
                    f["caption_matches_plan"] for f in figs),
                "all_svg_readback": all(f["svg_readback"] for f in figs),
            }
        return json.dumps(rep, ensure_ascii=False, default=str)

    def _fn_export(fmt: str = "docx", out: str = "",
                   python_exe: str = "") -> str:
        root = _wd()
        sf = _safe(root, TS.STATE_NAME)
        if not sf.is_file():
            return json.dumps({"ok": False,
                               "error": f"没有 {TS.STATE_NAME}；先调 typeset 出一次版"},
                              ensure_ascii=False)
        try:
            sp = TS.validate(json.loads(sf.read_text(encoding="utf-8")))
        except (ValueError, OSError) as e:
            return json.dumps({"ok": False,
                               "error": f"排版状态坏了：{type(e).__name__}: {e}"},
                              ensure_ascii=False)
        fmt = str(fmt or "docx").strip().lower()
        try:
            dest = _safe(root, out or (TS.DOCX_NAME if fmt == "docx" else TS.TEX_NAME))
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        if fmt == "docx":
            rep = TS.export_docx(sp, dest, python_exe=python_exe)
        elif fmt == "tex":
            rep = TS.export_tex(sp, dest)
        else:
            return json.dumps({"ok": False,
                               "error": f"fmt={fmt!r} 认不出（docx / tex）"},
                              ensure_ascii=False)
        rep.update({"style": sp["style"], "advisory": True,
                    "next": TS.format_next(
                        {"fix_before_delivery": [], "not_auditable": []},
                        {"audit_scope": rep.get("audit_scope", "")})})
        return json.dumps(rep, ensure_ascii=False)

    specs = [
        ("typeset",
         "把稿件排成可打印的版式：AI 只声明 blocks（详口径见 spec 参数）或给现成 .md，"
         "页边距/字号阶梯/行距/章节编号/题注位置/参考文献样式/分页全由体例模板算。"
         "出打印级 HTML + 几何清单，写完逐块读回，并汇总稿面/体例/图表三套判据。"
         "零参数调用可拿到全部体例与规格模板。",
         {"spec": {"type": "object", "description": SPEC_GUIDE},
          "path": {"type": "string", "description": "现成 .md 稿件路径；给了它就不必写 blocks"},
          "style": {"type": "string", "enum": list(TS.STYLES),
                    "description": "体例，默认 journal-cn"},
          "out": {"type": "string", "description": "输出 HTML 路径，默认 排版台.html"}},
         [], _fn_typeset, True),
        ("typeset_export",
         "把当前排版状态导出成 .docx（3.12 + python-docx）或 .tex（纯标准库），"
         "导出后逐块读回文字。体例数值仍来自同一份 STYLES 单源。",
         {"fmt": {"type": "string", "enum": ["docx", "tex"], "description": "默认 docx"},
          "out": {"type": "string", "description": "输出路径"},
          "python_exe": {"type": "string", "description": "可选：装了 python-docx 的解释器"}},
         [], _fn_export, True),
    ]
    return register_tool_specs(registry, specs)


@register("@external/typeset-desk")
class TypesetDesk(DeskCapability):
    """常驻能力：格式排版台 + 规格口径单源。"""

    ALWAYS_ON = True
    id = "typeset-desk"
    kind = "tool"
    version = "1.0.0"
    config_schema: dict[str, dict] = {}
    ALWAYS_CONFIG: dict = {}
    SECTION_TEXTS = {_SECTION: SPEC_GUIDE}
    ESSENTIAL_TOOLS = ("typeset", "typeset_export")

    def mount(self, cfg, ctx):
        self.cfg = dict(cfg or {})
        self.added = _register_tools(ctx, self.cfg)
        return [f"注册工具 {len(self.added)} 个：{'、'.join(self.added)}",
                "规格口径单源 SPEC_GUIDE",
                "质量结论走 quality/next，不改 ok/scope/checks"]

    def provides(self) -> list[str]:
        return ["always-on:typeset-desk", f"prompt:{_SECTION}",
                f"tools:{len(getattr(self, 'added', []))}/2"]
