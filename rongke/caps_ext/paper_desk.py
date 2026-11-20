# -*- coding: utf-8 -*-
"""常驻插件：论文台 —— 把「写/画/排/讲/投」串成一张可核对的交付态（2026-11-11）。

## 为什么需要它（读回事实，不是构想）

前面每个工具都各有一份状态文件、各自一台 HTML 视图，互不读对方：
稿件台（`稿件台.json/.html`）、图表编排台（正文里的题注）、机制图/数据图
（`*.svg` + `*.layout.json` + `figure-qa.json`）、排版台（`排版台.json/.html`）、
幻灯片台（`deck_spec.json` / `画布.json` / `deck.pptx`）。
而 `writing.lint()` / `manuscript_desk.check()` / `visual_desk.scan_visuals()` /
`svgq` / `FigureAuditor` 全部 `advisory=True`、都不拦交付 ——
**没有任何一处把「这篇稿现在能不能投」汇总出来**。

本插件只读扫描工作区，把产物与质量结论索引成一份 `论文台.json`
和一张自包含 `论文台.html`：每个环节有什么、缺什么、下一步调哪个工具。

## 边界

  · **纯只读 + 只写台面**：不碰正文、不碰 pptx、不拦交付、不加硬门。
  · 「可投稿」是 `advisory` 的**信息汇总**，不是第 10 条交付门。
  · 拿不到的判据进 `not_auditable`（**审不了 ≠ 通过**），不静默降级。
  · 常驻（`ALWAYS_ON`），不进预设、不改 SYSTEM。
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from rongke.caps import register
from rongke.state_store import atomic_write_text
from rongke.tool_sdk import DeskCapability, register_tool_specs, safe_workspace_path

ALWAYS_ON = True
_SECTION = "external-paper-desk"

JSON_NAME = "论文台.json"
HTML_NAME = "论文台.html"

#: 生态环节单源：名字 / 该认哪些产物 / 该调哪个工具 / 缺了怎么办。
CHAIN = (
    {"stage": "写作", "tool": "write_file / manuscript_fix",
     "files": ("*.md", "稿件台.json"),
     "action": "先把正文与大纲落盘；体例问题调 manuscript_fix"},
    {"stage": "出图", "tool": "diagram_svg / plot_svg",
     "files": ("*.svg", "*.layout.json", "figure-qa.json"),
     "action": "数据图用 plot_svg，机制图用 diagram_svg；投稿级仍走 plot_data + audit_figure"},
    {"stage": "图表落位", "tool": "visual_plan / visual_place",
     "files": ("figure*.png", "figure*.pdf", "figure*.tif"),
     "action": "图/表要进正文对应章节，别堆在参考文献后；题注与正文呼应交给工具"},
    {"stage": "格式排版", "tool": "typeset / typeset_export",
     "files": ("排版台.json", "排版台.html", "论文.docx", "论文.tex"),
     "action": "调 typeset 出打印级版式，typeset_export 出 docx / tex"},
    {"stage": "汇报", "tool": "deck_make / canvas_act",
     "files": ("deck_spec.json", "幻灯片台.html", "画布.json", "deck.pptx"),
     "action": "快速起稿用 deck_make，图元级精修用 canvas_act；两者别共用坐标系"},
    # ⚠️ 别把「论文台.json」列成自己的产物：第一次 scan 时它还没生成，
    #    自指会把本环节永远判成缺产物。这个环节的证据是**质量汇总跑没跑成**。
    {"stage": "交付态", "tool": "paper_status",
     "files": (),
     "action": "本台只读汇总；交付门仍照旧由 audit_deck 等各自把关"},
)


def _glob_files(root: Path, patterns, limit: int = 8) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        for p in sorted(root.glob(pat)):
            if p.is_file() and p.name not in out:
                out.append(p.name)
            if len(out) >= limit:
                return out
    return out


#: 正文**源稿**的文本后缀。`.docx/.doc` 是导出成品（二进制），
#: shape.PROSE_SUFFIXES 把它们也收进去，按 mtime 取第一个会把刚导出的
#: `论文.docx` 当正文读进 lint() —— 3.5 万字节乱码，质量门全红（实测）。
TEXT_SOURCE_SUFFIXES = (".md", ".txt", ".rst", ".tex")
#: 源稿优先于导出件：一次排版会写出 `论文.tex`（比正文长），
#: 只按字符数选就会把导出件当正文（实测 1053 字 > 309 字）。
SOURCE_PRIORITY = {".md": 0, ".txt": 1, ".rst": 2, ".tex": 3}


def _prose(root: Path) -> tuple:
    """正文源稿：先按后缀优先级（.md 优先），同级再按字符数；选择理由可读回。

    既不按 mtime（导出件更新），也不只按字符数（导出件更长）。
    `.docx/.doc` 是二进制成品，压根不进 TEXT_SOURCE_SUFFIXES。
    """
    cands = []
    for base in (root, root / "data"):
        try:
            for p in base.iterdir():
                if (p.is_file() and p.suffix.lower() in TEXT_SOURCE_SUFFIXES
                        and not any(k in p.name for k in
                                    ("outline", "大纲", "提纲", "说明", "计划"))):
                    try:
                        n = len(p.read_text(encoding="utf-8", errors="replace"))
                    except Exception:                        # noqa: BLE001
                        n = 0
                    cands.append((n, p.name, p))
        except Exception:                                    # noqa: BLE001
            continue
    cands.sort(key=lambda x: (SOURCE_PRIORITY.get(
        Path(x[1]).suffix.lower(), 9), -x[0]))
    ranked = [{"name": n, "chars": c,
               "priority": SOURCE_PRIORITY.get(Path(n).suffix.lower(), 9)}
              for c, n, _ in cands[:6]]
    return ((cands[0][2], cands[0][1], ranked) if cands
            else (None, "", ranked))


def scan(root: "Path | str") -> dict:
    """只读汇总。返回结构化交付态；不抛异常，缺什么如实写进 `unknown`。"""
    root = Path(root)
    rep: dict = {"ok": True, "advisory": True, "root": str(root),
                 "chain": [], "gaps": [], "not_auditable": [],
                 "verdict": "unknown", "evidence": {}}
    pf, pname, ranked = _prose(root)
    rep["evidence"]["prose"] = pname or ""
    rep["evidence"]["prose_candidates"] = ranked
    text = ""
    if pf is not None:
        try:
            raw = pf.read_text(encoding="utf-8", errors="replace")
            # 防御性输入归一：`Path.read_text` 在文本模式下已做 universal
            # newline（实测 eol_norm=False，这一行是 no-op）。留着是防止
            # 将来换成 bytes / 别的读取路径时行尾正则失守。
            # ⚠️ 那次 hard 缺口的**真因不是 CRLF**，是 `_prose` 按 mtime /
            #    字符数选中了导出的 `论文.docx` / `论文.tex` 而非源稿；
            #    归因别记错，见 _prose 的 SOURCE_PRIORITY。
            text = raw.replace("\r\n", "\n").replace("\r", "\n")
            rep["evidence"]["prose_chars"] = len(text)
            rep["evidence"]["prose_eol_normalized"] = ("\r\n" in raw)
        except Exception as e:                                # noqa: BLE001
            rep["not_auditable"].append(f"正文读不了：{type(e).__name__}: {e}")
    else:
        rep["gaps"].append({"code": "NO_PROSE", "level": "hard", "stage": "写作",
                            "detail": "工作区里没有正文 .md",
                            "action": "先调 write_file 落盘正文"})

    for c in CHAIN:
        found = _glob_files(root, c["files"])
        present = bool(found) if c["files"] else bool(rep.get("quality"))
        rep["chain"].append({"stage": c["stage"], "tool": c["tool"],
                             "files": found, "present": present,
                             "action": c["action"]})
    rep["evidence"]["files"] = {c["stage"]: c["files"] for c in rep["chain"]}

    # 图表身份层：优先读登记单源，不再只靠扫盘数 *.svg。
    # 缺席 / 读不回来都**明说**，但只报 soft —— 登记是 advisory 汇总，
    # 不是第 10 条交付门（「目标函数不是门更严」）。
    try:
        from rongke import figure_registry as FR
        reg, st = FR.load(root)
        if st == "ok" and reg["entries"]:
            vr = FR.verify(root, text, reg)
            tiers = {t: sum(1 for e in reg["entries"] if e["tier"] == t)
                     for t in FR.TIERS}
            rep["evidence"]["figure_registry"] = {
                "status": "ok", "n_entries": len(reg["entries"]),
                "tiers": tiers, "numbering": vr["numbering"],
                "verify_ok": vr["ok"], "gaps": vr["gaps"]}
            if not vr["ok"]:
                rep["gaps"].append({
                    "code": "FIGURE_REGISTRY_GAPS", "level": "soft",
                    "stage": "图表落位",
                    "detail": "登记五连读回有缺口："
                              + "、".join(sorted({g["code"] for g in vr["gaps"]})),
                    "action": "按 gaps 的 action 修稿/修登记，再调 visual_renumber"})
        else:
            rep["evidence"]["figure_registry"] = {
                "status": st, "n_entries": 0,
                "note": "图表身份层缺席（unknown，未纳入终态判定）；"
                        "盘上 SVG 数量不等于「图表已进论证链」"}
            rep["gaps"].append({
                "code": "NO_FIGURE_REGISTRY", "level": "soft",
                "stage": "图表落位",
                "detail": "工作区没有可用的 图表登记.json，编号重编与"
                          "产物/锚点/引用互证审不了",
                "action": "给 visual_place 传 anchor_id 建登记；"
                          "删图或重排章节后调 visual_renumber"})
    except Exception as ex:                                   # noqa: BLE001
        rep["evidence"]["figure_registry"] = {
            "status": f"error:{type(ex).__name__}", "n_entries": 0,
            "note": "登记层接不上，已明说而非静默退回扫盘"}
        rep["not_auditable"].append(
            f"图表登记层不可用：{type(ex).__name__}: {ex}")

    # 质量结论：复用 typeset.audit_quality 那三套判据的**同一份**汇总，
    # 不在这里重写第二套；拿不到就进 not_auditable。
    if text:
        try:
            from rongke.typeset import audit_quality
            q = audit_quality(text, root)
            rep["quality"] = {"gates": q["gates"], "details": q["details"],
                              "blocking": q["blocking"],
                              "not_auditable": q["not_auditable"]}
            rep["not_auditable"].extend(q["not_auditable"])
            for g in q["blocking"]:
                rep["gaps"].append({"code": f"QUALITY_{g.upper()}",
                                    "level": "hard", "stage": "交付态",
                                    "detail": f"质量门 {g} 有必修项",
                                    "action": "按该门的 fix 回写作链修，再排一次版"})
        except Exception as e:                                # noqa: BLE001
            rep["not_auditable"].append(
                f"质量汇总不可用：{type(e).__name__}: {e}")

    # SVG 质量门：最多量 6 张，避免一次扫全仓拖垮；失败单独记，不拖累其它环节。
    svgs = [p for p in sorted(root.glob("*.svg"))][:6]
    svg_reports = []
    if svgs:
        try:
            from rongke.svgq import audit_svg
        except Exception as e:                                # noqa: BLE001
            audit_svg = None
            rep["not_auditable"].append(
                f"svgq 不可用：{type(e).__name__}: {e}")
        if audit_svg is not None:
            for p in svgs:
                try:
                    layout = p.with_suffix(".layout.json")
                    q = audit_svg(p, layout=layout if layout.is_file() else None)
                    svg_reports.append({"file": p.name, "tier": q["tier"],
                                        "gates": {g["name"]: g["verdict"]
                                                  for g in q["gates"]},
                                        "fix": q["fix_before_delivery"]})
                    if q["fix_before_delivery"]:
                        rep["gaps"].append({
                            "code": "SVG_QUALITY", "level": "hard", "stage": "出图",
                            "detail": f"{p.name} 必修：{'、'.join(q['fix_before_delivery'])}",
                            "action": "改规格重画，不要手改 SVG 字节"})
                except Exception as e:                        # noqa: BLE001
                    rep["not_auditable"].append(
                        f"{p.name} 审不了：{type(e).__name__}: {e}")
    rep["evidence"]["svg_reports"] = svg_reports
    rep["evidence"]["n_svg"] = len(list(root.glob("*.svg")))
    # 排版台把质量读回写进了 sidecar ⇒ 论文台直接呈现，不另算第二套。
    tf = root / "排版台.json"
    if tf.is_file():
        try:
            td = json.loads(tf.read_text(encoding="utf-8"))
            rep["evidence"]["typeset_checks"] = td.get("quality_checks") or {}
            rep["evidence"]["typeset_summary"] = td.get("quality_summary") or {}
        except (ValueError, OSError) as e:
            rep["not_auditable"].append(
                f"排版质量读不回：{type(e).__name__}: {e}")
    # 交付态环节的证据是「质量汇总跑没跑成」，在质量块之后回填（不自指）。
    for c in rep["chain"]:
        if c["stage"] == "交付态":
            c["present"] = bool(rep.get("quality"))

    # 环节缺失只作 soft 提示（不是每篇论文都要做 PPT / 每种都要图）
    for c in rep["chain"]:
        if not c["present"] and c["stage"] in ("格式排版", "汇报"):
            rep["gaps"].append({"code": f"NO_{c['stage']}_ARTIFACT",
                                "level": "soft", "stage": c["stage"],
                                "detail": "该环节还没有产物",
                                "action": c["action"]})
    hard = [g for g in rep["gaps"] if g["level"] == "hard"]
    rep["verdict"] = ("gaps" if hard else
                      "unknown" if rep["not_auditable"] else "ready")
    rep["summary"] = {
        "stages_present": sum(1 for c in rep["chain"] if c["present"]),
        "stages_total": len(rep["chain"]),
        "hard": len(hard), "soft": len(rep["gaps"]) - len(hard),
        "not_auditable": len(rep["not_auditable"]),
        "prose_chars": rep["evidence"].get("prose_chars", 0),
        "n_svg": rep["evidence"].get("n_svg", 0),
    }
    return rep


def render_html(rep: dict) -> str:
    """自包含「论文台」总览：流水线 + 质量尺子 + 缺口。零外部资源。

    绢青陈墨的色值与中文字体栈复用 `svgplot` 的**唯一单源**，不另抄一套；
    拿不到时退回同一组常量并如实降级（台面是给人看的，不是承重判据）。
    """
    e = html.escape
    try:
        from rongke.svgplot import (FONT_STACK as _FS, _AXIS, _GRID, _INK,
                                    _PALETTE, _PAPER)
        C = {"ink": _INK, "paper": _PAPER, "rule": _GRID, "axis": _AXIS,
             "accent": _PALETTE[1], "ok": _PALETTE[0], "warn": _PALETTE[2]}
        fs = _FS
    except Exception:                                         # noqa: BLE001
        C = {"ink": "#2B2723", "paper": "#FBF8F1", "rule": "#D9D2C4",
             "axis": "#5A534A", "accent": "#B23A2E", "ok": "#2E5A63",
             "warn": "#C08A3E"}
        fs = "Microsoft YaHei, SimHei, Arial, sans-serif"
    s = rep["summary"]
    cards = []
    for c in rep["chain"]:
        cls = "on" if c["present"] else "off"
        fl = "、".join(e(x) for x in c["files"][:4]) or "—"
        cards.append(f"""<article class="card {cls}">
          <div class="hd"><b>{e(c['stage'])}</b>
            <span>{'有产物' if c['present'] else '暂无产物'}</span></div>
          <div class="tool">{e(c['tool'])}</div>
          <div class="files">{fl}</div>
          <div class="act">{e(c['action'])}</div>
        </article>""")
    gaps = "".join(
        f"<li class='{g['level']}'><b>{e(g['code'])}</b> · {e(g['stage'])}："
        f"{e(g['detail'])}<br><i>{e(g['action'])}</i></li>"
        for g in rep["gaps"]) or "<li class='clean'>当前没有缺口</li>"
    unknown = "".join(f"<li>{e(x)}</li>" for x in rep["not_auditable"]) or \
              "<li class='clean'>没有审不了的项</li>"
    verdict = {"ready": "可投稿（advisory）", "gaps": "还有必修项",
               "unknown": "有审不了的项，不算通过"}[rep["verdict"]]
    # 质量尺子：把三套判据结论与排版台的逐项 check 直接摆上桌面，
    # 不让人（也不让 AI）靠一句「质量没问题」自述。
    gates = (rep.get("quality") or {}).get("gates") or {}

    def _gc(v):
        return "ok" if v == "PASS" else ("warn" if v == "WARN" else "bad")

    gate_rows = "".join(
        f"<div class='gate {_gc(v)}'><b>{e(k)}</b><span>{e(v)}</span></div>"
        for k, v in gates.items()) or "<div class='empty'>质量门还没跑</div>"
    tchecks = rep["evidence"].get("typeset_checks") or {}
    chips = "".join(
        f"<span class='chip {'ok' if v else 'bad'}' title='{e(k)}'>{e(k)}</span>"
        for k, v in tchecks.items()) or "<span class='empty'>排版台还没出稿</span>"
    tsum = rep["evidence"].get("typeset_summary") or {}
    nums = " · ".join(e(str(x)) for x in (tsum.get("numbering") or [])[:8])
    cap = tsum.get("caption_rule") or {}
    tmeta = " · ".join(x for x in (
        f"章节编号 {nums}" if nums else "",
        f"图题{cap.get('figure', '?')} / 表题{cap.get('table', '?')}" if cap else "",
        f"字体 {e(str(tsum.get('font_stack', ''))[:34])}"
        if tsum.get("font_stack") else "",
    ) if x) or "—"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>论文台 · {e(rep['root'])}</title>
<style>
:root{{--paper:{C['paper']};--ink:{C['ink']};--ink2:{C['axis']};--rule:{C['rule']};
       --accent:{C['accent']};--ok:{C['ok']};--warn:{C['warn']}}}
*{{box-sizing:border-box}}body{{margin:0;background:#e9e4d8;color:var(--ink);
 font:14px/1.7 {fs}}}
header{{padding:28px 4vw 20px;background:var(--paper);
 border-bottom:3px double var(--accent)}}
h1{{margin:0;font-size:25px;letter-spacing:.12em}}
.sub{{color:var(--ink2);font-size:12px;margin-top:6px;letter-spacing:.04em}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}}
.stat{{background:#fff;border:1px solid var(--rule);padding:7px 13px;font-size:12px}}
.stat b{{color:var(--accent);font-size:18px;margin-right:5px;font-family:Georgia,serif}}
.wrap{{padding:22px 4vw 60px}}
h2{{font-size:15px;margin:26px 0 10px;color:var(--accent);letter-spacing:.06em;
 border-left:3px solid var(--accent);padding-left:9px}}
.chain{{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}}
.card{{background:var(--paper);border:1px solid var(--rule);padding:15px;
 border-left:4px solid var(--rule);box-shadow:0 1px 3px rgba(43,39,35,.07)}}
.card.on{{border-left-color:var(--ok)}}.card.off{{border-left-color:#c9bda5}}
.hd{{display:flex;justify-content:space-between;font-size:15px;letter-spacing:.05em}}
.hd span{{font-size:11px;color:var(--ink2)}}
.tool{{margin:8px 0 3px;font-family:Consolas,monospace;font-size:11px;color:var(--accent)}}
.files{{font-size:11px;color:var(--ink2);word-break:break-all}}
.act{{margin-top:9px;padding-top:8px;border-top:1px dotted var(--rule);font-size:11px}}
.quality{{background:var(--paper);border:1px solid var(--rule);padding:14px}}
.gate{{display:flex;justify-content:space-between;padding:7px 10px;margin-bottom:6px;
 border-left:3px solid var(--rule);background:#fff;font-size:12px}}
.gate.ok{{border-left-color:var(--ok)}}.gate.warn{{border-left-color:var(--warn)}}
.gate.bad{{border-left-color:var(--accent)}}
.gate span{{font-family:Consolas,monospace;color:var(--ink2)}}
.meta{{font-size:11px;color:var(--ink2);margin:10px 0 12px;line-height:1.8}}
.chip{{display:inline-block;font-family:Consolas,monospace;font-size:10px;
 padding:3px 7px;margin:0 5px 5px 0;border:1px solid var(--rule);background:#fff}}
.chip.ok{{border-left:3px solid var(--ok);color:var(--ok)}}
.chip.bad{{border-left:3px solid var(--accent);color:var(--accent)}}
.empty{{color:var(--ink2);font-size:11px}}
ul{{padding-left:0;list-style:none;background:var(--paper);border:1px solid var(--rule)}}
li{{padding:9px 14px;border-top:1px solid var(--rule);font-size:12px}}
li:first-child{{border-top:0}}li.hard b{{color:var(--accent)}}
li.soft b{{color:var(--warn)}}li.clean{{color:var(--ok)}}
li i{{color:var(--ink2)}}footer{{padding:16px 4vw 36px;font-size:11px;color:var(--ink2)}}
</style></head><body>
<header><h1>论文台 · 交付态总览</h1>
<div class="sub">只读汇总 · 不拦交付 · 审不了 ≠ 通过 · 根 {e(rep['root'])}</div>
<div class="stats">
 <div class="stat"><b>{s['stages_present']}/{s['stages_total']}</b>环节有产物</div>
 <div class="stat"><b>{s['prose_chars']}</b>正文字</div>
 <div class="stat"><b>{s['n_svg']}</b>张 SVG</div>
 <div class="stat"><b>{s['hard']}</b>必修 / {s['soft']} 建议</div>
 <div class="stat"><b>{s['not_auditable']}</b>审不了</div>
 <div class="stat"><b>{e(verdict)}</b></div>
</div></header>
<div class="wrap">
<h2>写 / 画 / 排 / 讲 / 投 —— 每个环节由哪个 AI 工具操作</h2>
<div class="chain">{''.join(cards)}</div>
<h2>质量尺子（复用三套已有判据，不另写一套）</h2>
<div class="quality">{gate_rows}</div>
<h2>排版质量读回 · {len(tchecks)} 项 checks</h2>
<div class="quality"><div class="meta">{tmeta}</div>{chips}</div>
<h2>还差什么（advisory）</h2><ul>{gaps}</ul>
<h2>审不了的项（不等于通过）</h2><ul>{unknown}</ul>
</div>
<footer>荣珂 · 判据单源 / 每步读回 / 工具替 AI 看状态 · 本台只读</footer>
</body></html>"""


def _register_tools(ctx: dict, cfg: dict) -> list[str]:
    registry = ctx.get("registry")
    if registry is None:
        return []

    def _fn_status(root: str = "") -> str:
        base = Path.cwd().resolve()
        if root:
            try:
                base = safe_workspace_path(root, base, allow_abs=False)
            except ValueError:
                return json.dumps({"ok": False, "error": f"root 越出工作区：{root}"},
                                  ensure_ascii=False)
        rep = scan(base)
        try:
            jf, hf = base / JSON_NAME, base / HTML_NAME
            atomic_write_text(jf, json.dumps(rep, ensure_ascii=False, indent=2))
            atomic_write_text(hf, render_html(rep))
            rep["page"], rep["state"] = str(hf), str(jf)
            rep["readback_equal"] = (
                json.loads(jf.read_text(encoding="utf-8")).get("verdict")
                == rep["verdict"])
            rep["next"] = ("按 gaps[].action 补；改完再调一次 paper_status 复核。"
                           "本台只给信息，不拦交付、不加门")
        except Exception as e:                                # noqa: BLE001
            rep["ok"] = False
            rep["error"] = f"台面落盘失败：{type(e).__name__}: {e}"
        return json.dumps(rep, ensure_ascii=False, default=str)

    specs = [
        ("paper_status",
         "论文台：只读扫描工作区，把正文 / 图 / 表 / 排版 / 幻灯片 / "
         "质检结论索引成一份交付态（论文台.json + 自包含 论文台.html），"
         "给出「可投稿 / 还差什么 / 审不了」的汇总。不碰正文、不拦交付、不加门。",
         {"root": {"type": "string", "description": "工作区；留空用当前工作区"}},
         [], _fn_status, True),
    ]
    return register_tool_specs(registry, specs)


@register("@external/paper-desk")
class PaperDesk(DeskCapability):
    """常驻能力：论文生态交付态总线（只读汇总）。"""

    ALWAYS_ON = True
    id = "paper-desk"
    kind = "tool"
    version = "1.0.0"
    config_schema: dict[str, dict] = {}
    ALWAYS_CONFIG: dict = {}
    SECTION_TEXTS = {_SECTION: "论文台只读汇总；审不了不等于通过；不拦交付。"}
    ESSENTIAL_TOOLS = ("paper_status",)

    def mount(self, cfg, ctx):
        self.cfg = dict(cfg or {})
        self.added = _register_tools(ctx, self.cfg)
        return [f"always-on:{self.id}:{n}" for n in self.added]

    def provides(self):
        return [f"always-on:{self.id}:{n}" for n in getattr(self, "added", ())]
