# -*- coding: utf-8 -*-
"""画布的「给人看」与「交付」两端：SVG 渲染台 + .pptx 导出。

  · `render_html()`：把每页图元直译成 SVG，拼成自包含 HTML。
    AI 看的是 `query()` 的结构化状态，**人看的是这张台面** ——
    这正是 Origin 的画布窗口在这个工具里的对应物。
  · `export_pptx()`：图元坐标即最终坐标（px × 9525 = EMU），
    外包给装了 python-pptx 的 3.12 子进程；内核仍零依赖。
    不调 `compose_deck` —— 版面是 AI 自己摆的，工具不替它重排。
  · `audit_rendered()`：可选把渲染出的 SVG 交给 `rongke/svgq.audit_svg()`
    跑字号/对比度/碰撞/数据保真等门（复用当天做的资产，不另写一套）。
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

from .canvas import (CANVAS_PX, DECK_NAME, HTML_NAME, PALETTE, PX_EMU,
                     Canvas, _plain)

_FONT = "Microsoft YaHei, PingFang SC, SimHei, sans-serif"


def _svg_page(doc: dict, page: dict) -> str:
    w, h = doc["canvas"]["w"], doc["canvas"]["h"]
    parts = [f'<rect width="{w}" height="{h}" fill="{PALETTE["paper"]}"/>']
    for e in page["elements"]:
        x, y, ew, eh = e["box"]
        t = e["type"]
        col = e.get("color") or PALETTE["ink"]
        if t == "rect":
            parts.append(f'<rect x="{x}" y="{y}" width="{ew}" height="{eh}" '
                         f'fill="{e.get("fill") or PALETTE["accent"]}"/>')
        elif t == "line":
            parts.append(f'<rect x="{x}" y="{y}" width="{ew}" '
                         f'height="{max(1, eh)}" fill="{e.get("fill") or col}"/>')
        elif t in ("text", "bullets"):
            size = int(e.get("size") or 26)
            lines = ([str(e.get("text") or "")] if t == "text"
                     else [str(v) for v in (e.get("items") or [])])
            bold = ' font-weight="600"' if t == "text" and size >= 36 else ""
            lh = size * 1.45
            for i, ln in enumerate(lines):
                ty = y + size + i * lh
                if ty > y + eh + size:
                    break
                bullet = "• " if t == "bullets" else ""
                parts.append(
                    f'<text x="{x}" y="{ty:.0f}" font-family="{_FONT}" '
                    f'font-size="{size}" fill="{col}"{bold}>'
                    f'{html.escape(bullet + ln)}</text>')
        elif t == "chart":
            ch = e.get("chart") or {}
            cats = [str(c) for c in (ch.get("categories") or [])]
            series = ch.get("series") or []
            parts.append(f'<rect x="{x}" y="{y}" width="{ew}" height="{eh}" '
                         f'fill="#ffffff" stroke="{PALETTE["rule"]}"/>')
            vals = []
            for s in series:
                for v in (s.get("values") or []):
                    try:
                        vals.append(float(v))
                    except (TypeError, ValueError):
                        pass
            top = max(vals, default=1.0) or 1.0
            n = max(1, len(cats))
            slot = ew / n
            bw = slot / max(1, len(series)) * 0.72
            for si, s in enumerate(series):
                for ci, v in enumerate(s.get("values") or []):
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    bh = (fv / top) * (eh - 56)
                    bx = x + ci * slot + si * bw + 6
                    by = y + eh - 34 - bh
                    color = PALETTE["accent"] if si == 0 else PALETTE["accent2"]
                    parts.append(f'<rect x="{bx:.1f}" y="{by:.1f}" '
                                 f'width="{bw:.1f}" height="{bh:.1f}" fill="{color}"/>')
            for ci, c in enumerate(cats):
                parts.append(f'<text x="{x + ci * slot + 4:.1f}" y="{y + eh - 10}" '
                             f'font-family="{_FONT}" font-size="14" '
                             f'fill="{PALETTE["ink2"]}">{html.escape(c)}</text>')
        elif t == "table":
            rows = e.get("rows") or []
            rh = eh / max(1, len(rows))
            for ri, row in enumerate(rows):
                cells = row if isinstance(row, (list, tuple)) else [row]
                cw = ew / max(1, len(cells))
                for ci, cell in enumerate(cells):
                    parts.append(f'<rect x="{x + ci * cw:.1f}" y="{y + ri * rh:.1f}" '
                                 f'width="{cw:.1f}" height="{rh:.1f}" fill="none" '
                                 f'stroke="{PALETTE["rule"]}"/>')
                    parts.append(f'<text x="{x + ci * cw + 6:.1f}" '
                                 f'y="{y + ri * rh + rh * 0.65:.1f}" '
                                 f'font-family="{_FONT}" font-size="{int(e.get("size") or 18)}" '
                                 f'fill="{col}">{html.escape(str(cell))}</text>')
        elif t == "image":
            src = str(e.get("src") or "")
            safe = html.escape(src, quote=True)
            parts.append(f'<image x="{x}" y="{y}" width="{ew}" height="{eh}" '
                         f'href="{safe}" preserveAspectRatio="xMidYMid meet"/>')
            parts.append(f'<rect x="{x}" y="{y}" width="{ew}" height="{eh}" '
                         f'fill="none" stroke="{PALETTE["rule"]}" stroke-dasharray="4 3"/>')
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" '
            f'xmlns="http://www.w3.org/2000/svg" role="img">'
            + "".join(parts) + "</svg>")


def render_html(c: Canvas, out: str = HTML_NAME) -> dict:
    """生成自包含画布台：左边逐页 SVG，右边质检与图元清单。"""
    doc = c.doc
    a = c.audit()
    pages = []
    for i, p in enumerate(doc["pages"], 1):
        els = "".join(
            f"<li><code>{html.escape(e['id'])}</code> "
            f"{html.escape(e['type'])} "
            f"<span class='box'>{html.escape(' '.join(str(v) for v in e['box']))}</span> "
            f"{html.escape(_plain(e)[:44])}</li>"
            for e in p["elements"])
        pages.append(f"""<section class="sheet">
          <div class="cap"><b>{i:02d}</b> {html.escape(p.get('title') or p['kind'])}
            <span>{html.escape(p['kind'])} · {len(p['elements'])} 图元</span></div>
          <div class="canvas">{_svg_page(doc, p)}</div>
          <ul class="els">{els}</ul>
        </section>""")
    gaps = "".join(
        f"<li class='{g['level']}'><b>{html.escape(g['code'])}</b> "
        f"第 {g.get('page', '?')} 页 · {html.escape(g['detail'])}"
        f"<br><i>{html.escape(g['action'])}</i></li>"
        for g in a["gaps"]) or "<li class='clean'>当前无缺口</li>"
    page = c.root / out
    page.write_text(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 画布 · {html.escape(doc['title'])}</title>
<style>
:root{{--paper:#f6f3ec;--ink:#22303a;--ink2:#5a6a72;--rule:#d8d2c4;--accent:#a8442f}}
*{{box-sizing:border-box}}body{{margin:0;background:#eae6dc;color:var(--ink);
 font:14px/1.6 "Microsoft YaHei","PingFang SC",sans-serif}}
header{{padding:22px 3vw 16px;background:var(--paper);border-bottom:2px solid var(--accent)}}
h1{{margin:0;font-size:22px;letter-spacing:.05em}}
.sub{{color:var(--ink2);font-size:12px;margin-top:4px}}
.wrap{{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:18px;padding:20px 3vw 60px}}
main{{display:grid;gap:20px;align-content:start}}
.sheet{{background:var(--paper);border:1px solid var(--rule);padding:14px}}
.cap{{display:flex;gap:10px;align-items:center;font-size:14px;margin-bottom:10px}}
.cap b{{font:600 20px/1 Georgia,serif;color:var(--accent)}}
.cap span{{margin-left:auto;color:var(--ink2);font-size:11px}}
.canvas{{border:1px solid var(--rule);background:#fff;aspect-ratio:16/9}}
.canvas svg{{display:block}}
.els{{margin:10px 0 0;padding-left:0;list-style:none;font-size:11px;color:var(--ink2)}}
.els li{{padding:2px 0;border-top:1px dotted var(--rule)}}
.els code{{color:var(--accent)}}.box{{color:#8a7a5e}}
aside{{background:var(--paper);border:1px solid var(--rule);padding:16px;
 align-self:start;position:sticky;top:16px}}
aside h2{{font-size:14px;margin:0 0 10px;color:var(--accent)}}
.stat{{display:flex;justify-content:space-between;font-size:12px;
 padding:4px 0;border-bottom:1px dotted var(--rule)}}
aside ul{{padding-left:0;list-style:none;margin:12px 0 0;font-size:11px}}
aside li{{padding:7px 0;border-top:1px solid var(--rule)}}
aside li.hard b{{color:var(--accent)}}aside li.soft b{{color:#8a7a5e}}
aside li.clean{{color:#3f6b6a}}aside i{{color:var(--ink2)}}
footer{{padding:14px 3vw 30px;color:var(--ink2);font-size:11px}}
</style></head><body>
<header><h1>AI 画布 · {html.escape(doc['title'])}</h1>
<div class="sub">图元坐标即最终坐标 · 每次动作写后读回 · 本页是画布窗口，不是渲染成品的替身</div></header>
<div class="wrap"><main>{''.join(pages) or '<section class="sheet">画布还是空的：用 new_page() 开一页</section>'}</main>
<aside><h2>画布现状（query / audit 的同一份事实）</h2>
<div class="stat"><span>页数</span><b>{a['counts']['pages']}</b></div>
<div class="stat"><span>图元</span><b>{a['counts']['elements']}</b></div>
<div class="stat"><span>视觉件</span><b>{a['counts']['visuals']}</b></div>
<div class="stat"><span>越界 / 重叠</span><b>{a['counts']['out_of_bounds']} / {a['counts']['overlaps']}</b></div>
<div class="stat"><span>文字字符</span><b>{a['counts']['chars']}</b></div>
<h2 style="margin-top:16px">缺口与下一步动作</h2><ul>{gaps}</ul></aside></div>
<footer>AI 操作图元 · 人看画布 · 状态落在 {html.escape(Path(c.state_path).name)}</footer>
</body></html>""", encoding="utf-8")
    return {"ok": True, "page": str(page), "pages": len(doc["pages"]),
            "gaps": a["gaps"], "bytes": page.stat().st_size}


#: 导出脚本跑在装了 python-pptx 的独立解释器里。图元 → 形状，一一对应。
_EXPORT_SCRIPT = r'''
import json, sys
from pptx import Presentation
from pptx.util import Emu, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

payload = json.loads(sys.stdin.read())
doc, out = payload["doc"], payload["out"]
E = 9525
prs = Presentation()
prs.slide_width = Emu(doc["canvas"]["w"] * E)
prs.slide_height = Emu(doc["canvas"]["h"] * E)
blank = prs.slide_layouts[6]

def rgb(v):
    s = str(v or "#22303a").lstrip("#")
    try: return RGBColor.from_string(s.upper())
    except Exception: return RGBColor(0x22, 0x30, 0x3A)

def place(sh, box):
    sh.left, sh.top = Emu(int(box[0]) * E), Emu(int(box[1]) * E)
    sh.width, sh.height = Emu(int(box[2]) * E), Emu(int(box[3]) * E)

def textbox(sl, e, lines):
    tb = sl.shapes.add_textbox(0, 0, Emu(100), Emu(100))
    place(tb, e["box"])
    tf = tb.text_frame; tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = str(ln)
        p.alignment = PP_ALIGN.LEFT
        for r in p.runs:
            r.font.size = Pt(float(e.get("size") or 26) * 0.75)
            r.font.color.rgb = rgb(e.get("color"))
    return tb

for pg in doc["pages"]:
    sl = prs.slides.add_slide(blank)
    for e in pg["elements"]:
        t, box = e["type"], e["box"]
        if t == "text":
            textbox(sl, e, [str(e.get("text") or "")])
        elif t == "bullets":
            textbox(sl, e, ["• " + str(x) for x in (e.get("items") or [])])
        elif t in ("rect", "line"):
            sh = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Emu(100), Emu(100))
            place(sh, box)
            sh.fill.solid(); sh.fill.fore_color.rgb = rgb(e.get("fill"))
            sh.line.fill.background()
        elif t == "chart":
            ch = e.get("chart") or {}
            cd = CategoryChartData()
            cd.categories = [str(x) for x in (ch.get("categories") or [])]
            for s in (ch.get("series") or []):
                cd.add_series(str(s.get("name") or ""),
                              tuple(float(v) for v in (s.get("values") or [])))
            gf = sl.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,
                                     Emu(int(box[0])*E), Emu(int(box[1])*E),
                                     Emu(int(box[2])*E), Emu(int(box[3])*E), cd)
            gf.chart.has_title = bool(ch.get("title"))
            if ch.get("title"):
                gf.chart.chart_title.text_frame.text = str(ch["title"])
        elif t == "table":
            rows = e.get("rows") or []
            nr, nc = max(1, len(rows)), max(1, max(len(r) for r in rows if isinstance(r,(list,tuple))) if rows else 1)
            gf = sl.shapes.add_table(nr, nc, Emu(int(box[0])*E), Emu(int(box[1])*E),
                                     Emu(int(box[2])*E), Emu(int(box[3])*E))
            for ri, row in enumerate(rows):
                cells = row if isinstance(row, (list,tuple)) else [row]
                for ci, val in enumerate(cells[:nc]):
                    gf.table.cell(ri, ci).text = str(val)
        elif t == "image":
            src = str(e.get("src") or "")
            import os
            if os.path.isfile(src):
                pic = sl.shapes.add_picture(src, Emu(int(box[0])*E), Emu(int(box[1])*E),
                                            Emu(int(box[2])*E), Emu(int(box[3])*E))
prs.save(out)
print(json.dumps({"ok": True, "slides": len(doc["pages"])}, ensure_ascii=False))
'''


def _lines_of(e: dict) -> list[str]:
    """这个图元**该有哪几行文字**（读回按行比，不拿整段前 N 字硬凑）。

    ⚠️ 曾踩：bullets 导成 `• 行一 / • 行二` 后，把 `_plain()` 拼起来的
       整段前 12 字拿去 slide 文本里找，必然找不到 ⇒ 凭空一次假失败。
       文字图元按**行**比，视觉件按 **part 数**比，两码事。
    """
    t = e["type"]
    if t == "text":
        return [str(e.get("text") or "")]
    if t == "bullets":
        return [str(x) for x in (e.get("items") or [])]
    if t == "table":
        out = []
        for r in (e.get("rows") or []):
            cells = r if isinstance(r, (list, tuple)) else [r]
            out.extend(str(c) for c in cells)
        return out
    return []


def backend_python(explicit: str = "") -> str | None:
    """找装了 python-pptx 的解释器。找不到就返回 None，**能力不可用要明说**。"""
    cands = ([explicit] if explicit else []) + [sys.executable]
    for exe in cands:
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "-c", "import pptx"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                return exe
        except Exception:                                     # noqa: BLE001
            continue
    return None


def export_pptx(c: Canvas, out: str = DECK_NAME, python_exe: str = "") -> dict:
    """把当前画布导出为 .pptx（图元坐标直接落位，不重排）。"""
    py = backend_python(python_exe)
    if py is None:
        return {"ok": False, "error": "EXPORT_BACKEND_UNAVAILABLE：找不到装了 python-pptx 的解释器",
                "hint": "请用 3.12 启动，或显式给 python_exe；别反复重试"}
    target = c.root / out
    payload = json.dumps({"doc": c.doc, "out": str(target)}, ensure_ascii=False)
    try:
        r = subprocess.run([py, "-c", _EXPORT_SCRIPT], input=payload.encode("utf-8"),
                           capture_output=True, timeout=180)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"导出子进程没跑起来：{type(e).__name__}: {e}"}
    if r.returncode != 0:
        return {"ok": False, "error": "导出失败：" +
                r.stderr.decode("utf-8", "replace")[-600:]}
    # 写后读回：页数与逐页文字都要在盘上的 .pptx 里找得到。
    import zipfile
    from xml.etree import ElementTree as ET
    A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
    try:
        with zipfile.ZipFile(target) as z:
            names = sorted((n for n in z.namelist()
                            if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                           key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)))
            texts = ["".join((t.text or "") for t in ET.fromstring(z.read(n)).iter(A_T))
                     for n in names]
            charts = [n for n in z.namelist()
                      if re.fullmatch(r"ppt/charts/chart\d+\.xml", n)]
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"导出后读不回 .pptx：{type(e).__name__}: {e}"}
    missing = []
    want_charts = 0
    for i, pg in enumerate(c.doc["pages"]):
        if i >= len(texts):
            missing.append(f"第 {i+1} 页不存在")
            continue
        for e in pg["elements"]:
            # ⚠️ chart 的标题不在 slide XML 里，在 ppt/charts/chartN.xml；
            #    拿 slide 文本去比 chart 的 _plain() 会凭空造一次假失败。
            #    「文字图元比文字、视觉件比 part」——判据按对象种类分开量。
            if e["type"] == "chart":
                want_charts += 1
                continue
            if e["type"] == "image":
                continue
            for line in _lines_of(e):
                if line.strip() and line.strip() not in texts[i]:
                    missing.append(f"第 {i+1} 页 {e['id']} 少了一行文字：{line[:16]}")
                    break
    if len(charts) < want_charts:
        missing.append(f"画布有 {want_charts} 个 chart 图元，pptx 里只有 {len(charts)} 个 chart part")
    render_html(c)
    return {"ok": not missing and len(texts) == len(c.doc["pages"]),
            "pptx": str(target), "slides": len(texts),
            "bytes": target.stat().st_size, "readback_missing": missing,
            "canvas_html": str(c.root / HTML_NAME),
            "note": "图元坐标即最终坐标；导出不重排、不替 AI 改版面"}


def audit_rendered(c: Canvas) -> dict:
    """可选：把渲染出的 SVG 交给 `rongke/svgq.audit_svg()` 复用七门质检。

    `svgq` 不在场 / 读不进 SVG 时如实回报，**不假装审过**。
    """
    try:
        from rongke.svgq import audit_svg
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "skipped": True,
                "reason": f"svgq 不可用：{type(e).__name__}: {e}"}
    out = []
    for i, p in enumerate(c.doc["pages"], 1):
        svg = c.root / f"_page_{i:02d}.svg"
        svg.write_text(_svg_page(c.doc, p), encoding="utf-8")
        try:
            rep = audit_svg(svg)
            out.append({"page": i, "gates": rep.get("gates") or rep})
        except Exception as e:                                # noqa: BLE001
            out.append({"page": i, "error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "skipped": False, "pages": len(out), "reports": out,
            "note": "复用 rongke/svgq 的门；与 canvas.audit() 的几何门互补"}
