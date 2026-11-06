# -*- coding: utf-8 -*-
"""荣珂**原生** SVG 出图后端 —— 纯标准库，零第三方依赖。

## 为什么需要它

原来的出图有两条路，都建立在「别人机器上恰好装了什么」之上：

  · `plot_data` → matplotlib / pandas / PyMuPDF 子进程（可选依赖，安装包里没带）；
  · `figure_render` → workbench MCP → Origin COM（商业软件，仅 Windows，多数人没有）。

于是「开源框架发给别人」这件事有个很难看的后果：**装上了、界面能开、
工具列表也在，但一张图都出不来**。这不是配置问题，是产品缺件。

## 这条后端是什么 / 不是什么

✅ 是：**预览级 / 网页 / PPT 插图**的矢量出图。读一张 CSV/TSV/JSON 表，
   复用 `analysis.plan_from_shape()` 量出来的选图型判据，用 `<rect>/<circle>/
   <polyline>/<text>` 拼 SVG，中文按字体栈回退，产物可被浏览器直接打开。

❌ 不是：投稿级 PDF 渲染，**也不冒充过了 `figure.py` 的六道渲染后审计门**。
   那套判据要求 600 dpi 位图、PDF 字形 ≥5 pt、碰撞与对齐几何清单 ——
   纯标准库诚实做不到。所以本模块的返回值里 `scope="preview"`、
   `audit_scope` 明写「未跑 PDF 六门」，绝不返回一张「合格证」。
   要投稿请用 `plot_data` + `audit_figure`（装 `rongke[figure]`）。

## 判据：不认「写文件成功」

`render_svg()` 写完**读回文件**、用 `xml.etree` 真解析一遍，逐项跑结构自检
（`inspect_svg()`）：viewBox、文本节点、数据标记、坐标不出界、中文字体栈、
无外部图片/脚本、中文字符确实留在文件里。任何一项不过就 `ok=False` 并写明，
文件**保留**在盘上供人查 —— 这是「豆腐块假成功」那次教训的直接落地：
`ok` 必须说实话。
"""
from __future__ import annotations

import html
import math
import re
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

#: 中文优先、英文保底的字体栈。单写 Arial 会让中文全部渲染成豆腐块，
#: 而且 matplotlib 当年那段「退出码 0、PNG 正常、字是方框」的假成功
#: 正是从这里来的。SVG 不栅格化字形，所以能做的诚实判据是：
#: **字体栈里必须有中文字体**，并在报告里说明「字形由查看端字体决定」。
FONT_STACK = "Microsoft YaHei, SimHei, Noto Sans CJK SC, WenQuanYi Micro Hei, Arial, sans-serif"
_CJK = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")
_MM_TO_PX = 96.0 / 25.4

#: 绢青陈墨体系：陈墨底、绢青主色、朱砂强调。
_INK = "#2B2723"
_PAPER = "#FBF8F1"
_GRID = "#D9D2C4"
_AXIS = "#5A534A"
_PALETTE = ("#2E5A63", "#B23A2E", "#C08A3E", "#4C6B52", "#7A5A8E", "#3E6C8C")

SUPPORTED_KINDS = (
    "line", "time_series", "scatter", "bar", "barh", "hist", "box",
    "heatmap", "matrix",
)


class SVGPlotError(ValueError):
    """这张表 / 这个图型画不了。消息要直接给人看，不许静默画空图。"""


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and not math.isfinite(v)) else float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _fmt(v: float) -> str:
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1000 or a < 0.01:
        return f"{v:.2e}"
    return f"{v:.3g}"


def _quantile(vals: list[float], q: float) -> float:
    s = sorted(vals)
    if not s:
        return 0.0
    pos = (len(s) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (pos - lo)


class _Canvas:
    """收集 SVG 元素 + 记账（文本/标记数量与坐标），供结构自检复用。"""

    def __init__(self, w: float, h: float):
        self.w, self.h = w, h
        self.body: list[str] = []
        self.n_text = 0
        self.n_mark = 0
        self.text_pos: list[tuple[float, float]] = []
        self.mark_boxes: list[tuple[float, float, float, float]] = []

    def text(self, x: float, y: float, s: Any, *, size: float = 10,
             anchor: str = "middle", fill: str = _INK,
             weight: str = "", rotate: str = "") -> None:
        self.n_text += 1
        self.text_pos.append((float(x), float(y)))
        # rotate 只接受**角度**；旋转中心固定为文字自己的 (x, y)。
        # 早先这里把中心点也塞进 rotate 参数，拼出 5 个数的
        # `rotate(-90 13 150 13 150)` —— SVG 只认 3 个，整个文本不显示。
        extra = (f' font-weight="{weight}"' if weight else "") \
            + (f' transform="rotate({rotate} {x:.1f} {y:.1f})"' if rotate else "")
        self.body.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size:.1f}" '
            f'text-anchor="{anchor}" fill="{fill}"{extra}>{_esc(s)}</text>')

    def rect(self, x: float, y: float, w: float, h: float, fill: str,
             *, stroke: str = "", cls: str = "mark", tip: str = "",
             opacity: float = 1.0) -> None:
        if cls == "mark":
            self.n_mark += 1
            self.mark_boxes.append((x, y, x + w, y + h))
        sw = f' stroke="{_esc(stroke)}" stroke-width="0.8"' if stroke else ""
        op = f' fill-opacity="{opacity:.2f}"' if opacity < 1.0 else ""
        t = f"<title>{_esc(tip)}</title>" if tip else ""
        base = (f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(w, 0):.2f}" '
                f'height="{max(h, 0):.2f}" fill="{_esc(fill)}" '
                f'class="{cls}"{sw}{op}')
        self.body.append(base + f'>{t}</rect>' if t else base + '/>')

    def circle(self, cx: float, cy: float, r: float, fill: str, *, tip: str = "") -> None:
        self.n_mark += 1
        self.mark_boxes.append((cx - r, cy - r, cx + r, cy + r))
        t = f"<title>{_esc(tip)}</title>" if tip else ""
        if t:
            self.body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" '
                             f'fill="{_esc(fill)}" class="mark">{t}</circle>')
        else:
            self.body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" '
                             f'fill="{_esc(fill)}" class="mark"/>')

    def line(self, x1: float, y1: float, x2: float, y2: float,
             stroke: str, width: float = 1.0, *, cls: str = "") -> None:
        c = f' class="{cls}"' if cls else ""
        self.body.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" '
                         f'y2="{y2:.2f}" stroke="{_esc(stroke)}" '
                         f'stroke-width="{width:.2f}"{c}/>')

    def polyline(self, pts: Iterable[tuple[float, float]], stroke: str,
                 width: float = 1.8) -> None:
        p = list(pts)
        if len(p) < 2:
            return
        self.n_mark += 1
        for x, y in p:
            self.mark_boxes.append((x, y, x, y))
        d = " ".join(f"{x:.2f},{y:.2f}" for x, y in p)
        self.body.append(f'<polyline points="{d}" fill="none" '
                         f'stroke="{_esc(stroke)}" stroke-width="{width:.2f}" '
                         f'stroke-linejoin="round" class="mark"/>')

    def to_svg(self, title: str, desc: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{self.w:.0f}px" height="{self.h:.0f}px" '
            f'viewBox="0 0 {self.w:.0f} {self.h:.0f}" role="img" '
            f'aria-labelledby="rk-title rk-desc">\n'
            f'  <title id="rk-title">{_esc(title)}</title>\n'
            f'  <desc id="rk-desc">{_esc(desc)}</desc>\n'
            f'  <style>text {{ font-family: {FONT_STACK}; }}</style>\n'
            f'  <g id="chart" font-family="{_esc(FONT_STACK)}">\n'
            f'    <rect x="0" y="0" width="{self.w:.0f}" height="{self.h:.0f}" '
            f'fill="{_PAPER}" class="paper"/>\n'
            + "\n".join("    " + b for b in self.body)
            + "\n  </g>\n</svg>\n")


def _records(rows: list[dict], plan: dict) -> tuple[str, str, str, str, list[tuple]]:
    kind = str(plan.get("kind") or "")
    if kind not in SUPPORTED_KINDS:
        raise SVGPlotError(
            f"原生 SVG 后端不支持图型 `{kind}`（支持：{'、'.join(SUPPORTED_KINDS)}）。"
            "需要这个图型的投稿级产物请改装 rongke[figure] 后走 plot_data；"
            "本模块不会假装画成别的图型。")
    x, y = str(plan.get("x") or ""), str(plan.get("y") or "")
    g = str(plan.get("group") or "")
    recs: list[tuple] = []
    for r in rows:
        yv = _num(r.get(y, ""))
        if yv is None:
            continue
        recs.append((str(r.get(x, "") if x else ""), yv,
                     str(r.get(g, "") or "") if g else ""))
    if not recs:
        raise SVGPlotError(
            f"列 `{y}` 里一个数值都没读到 —— 没有数据可画，不产出空图。"
            "检查列名、分隔符与缺失值。")
    return kind, x, y, g, recs


def _xscale(recs: list[tuple]) -> tuple[list[str], bool, dict[str, float], float, float]:
    """x 轴标签 / 是否数值轴 / 标签→位置 / 最小最大值。"""
    labels: list[str] = []
    for xv, _, _ in recs:
        if xv not in labels:
            labels.append(xv)
    nums = [_num(v) for v in labels]
    if nums and all(v is not None for v in nums):
        vals = [float(v) for v in nums]          # type: ignore[arg-type]
        return labels, True, {}, min(vals), max(vals)
    # ★ 分类轴的位置必须是**柱中心** `(i+0.5)/n`，不是 `i/(n-1)`。
    #   首版用后者：柱按 (i+0.5)/n 画、刻度与数值标签按 i/(n-1) 放，
    #   两套坐标系 ⇒ 最后一根柱的数字飘到图右外沿，而所有程序都 ok:true。
    #   这是肉眼看图才发现的，结构自检（只量出界/字体栈）量不出来。
    n = max(len(labels), 1)
    return labels, False, {lb: (i + 0.5) / n for i, lb in enumerate(labels)}, 0.0, 1.0


def _draw_frame(c: _Canvas, *, x0, y0, x1, y1, xlabel, ylabel, title,
                labels, numeric_x, label_pos, sx_lo, sx_hi, y_lo, y_hi) -> tuple:
    """画边框/网格/刻度。返回 (sx, sy) 两个映射函数。"""
    def sx(v: float) -> float:
        t = 0.5 if sx_hi <= sx_lo else (v - sx_lo) / (sx_hi - sx_lo)
        return x0 + max(0.0, min(1.0, t)) * (x1 - x0)

    def sy(v: float) -> float:
        t = 0.5 if y_hi <= y_lo else (v - y_lo) / (y_hi - y_lo)
        return y1 - max(0.0, min(1.0, t)) * (y1 - y0)

    c.text((x0 + x1) / 2, 20, title, size=12, weight="bold")
    # y 轴：5 条网格线 + 刻度值
    for i in range(6):
        v = y_lo + (y_hi - y_lo) * i / 5
        yy = sy(v)
        c.line(x0, yy, x1, yy, _GRID, 0.7)
        c.line(x0 - 4, yy, x0, yy, _AXIS, 1.0)
        c.text(x0 - 8, yy + 3.5, _fmt(v), size=8.5, anchor="end", fill=_AXIS)
    c.line(x0, y0, x0, y1, _AXIS, 1.1)
    c.line(x0, y1, x1, y1, _AXIS, 1.1)
    # x 轴刻度：标签太多时抽稀，不然文字必然重叠
    step = max(1, math.ceil(len(labels) / 8))
    for i, lb in enumerate(labels):
        if i % step:
            continue
        xv = _num(lb) if numeric_x else label_pos.get(lb, (i + 0.5) / max(len(labels), 1))
        xx = sx(float(xv) if xv is not None else float(i))
        c.line(xx, y1, xx, y1 + 4, _AXIS, 1.0)
        c.text(xx, y1 + 16, lb if len(lb) <= 12 else lb[:11] + "…",
               size=8.5, fill=_AXIS)
    c.text((x0 + x1) / 2, c.h - 6, xlabel, size=9.5, fill=_INK)
    c.text(13, (y0 + y1) / 2, ylabel, size=9.5, fill=_INK, rotate="-90")
    return sx, sy


def _legend(c: _Canvas, groups: list[str], x1: float, y0: float) -> None:
    for i, g in enumerate(groups[:6]):
        yy = y0 + 6 + i * 13
        c.rect(x1 - 96, yy - 7, 9, 9, _PALETTE[i % len(_PALETTE)], cls="legend")
        c.text(x1 - 82, yy + 1, g[:12], size=8, anchor="start", fill=_INK)


def _color_of(groups: list[str], g: str) -> str:
    if not groups or len(groups) < 2:
        return _PALETTE[0]
    return _PALETTE[groups.index(g) % len(_PALETTE)] if g in groups else _PALETTE[0]


def render_svg(rows: list[dict], plan: dict, out: str | Path) -> dict:
    """按 plan 把一张表画成 SVG。**写完读回并自检**。

    返回紧凑 dict；`ok=False` 时产物仍留在盘上供人查，绝不说「成功」。
    """
    kind, x, y, g, recs = _records(rows, plan)
    width_mm = float(plan.get("width_mm") or 89.0)
    height_mm = float(plan.get("height_mm") or 58.0)
    w = max(320.0, width_mm * _MM_TO_PX)
    h = max(220.0, height_mm * _MM_TO_PX)
    c = _Canvas(w, h)
    x0, x1, y0, y1 = 54.0, w - 16.0, 34.0, h - 46.0

    yvals = [r[1] for r in recs]
    groups = sorted({r[2] for r in recs if r[2]})
    title = str(plan.get("title") or f"{y} by {x}")
    xlabel = str(plan.get("xlabel") or x)
    ylabel = str(plan.get("ylabel") or y)

    if kind in ("heatmap", "matrix"):
        # 数值列两两 Pearson 相关矩阵：与 CHART_KINDS 里 heatmap 的语义一致。
        numcols = [k for k in (rows[0] if rows else {})
                   if sum(1 for r in rows if _num(r.get(k)) is not None) >= max(2, len(rows) // 2)]
        numcols = numcols[:8]
        if len(numcols) < 2:
            raise SVGPlotError("热图至少需要 2 个数值列，这张表不够 —— 不画空矩阵。")
        mat: list[list[float]] = []
        for a in numcols:
            row: list[float] = []
            av = [_num(r.get(a)) or 0.0 for r in rows]
            for b in numcols:
                bv = [_num(r.get(b)) or 0.0 for r in rows]
                ma, mb = statistics.fmean(av), statistics.fmean(bv)
                num = sum((p - ma) * (q - mb) for p, q in zip(av, bv))
                den = math.sqrt(sum((p - ma) ** 2 for p in av)
                                * sum((q - mb) ** 2 for q in bv))
                row.append(num / den if den else 0.0)
            mat.append(row)
        cw, ch = (x1 - x0) / len(numcols), (y1 - y0) / len(numcols)
        for i, a in enumerate(numcols):
            for j, b in enumerate(numcols):
                v = mat[i][j]
                tint = "#B23A2E" if v >= 0 else "#2E5A63"
                c.rect(x0 + j * cw + 1, y0 + i * ch + 1, cw - 2, ch - 2,
                       tint, cls="mark", tip=f"{a} × {b}: r={v:.3f}",
                       opacity=0.18 + 0.62 * min(abs(v), 1.0))
                c.text(x0 + j * cw + cw / 2, y0 + i * ch + ch / 2 + 3,
                       f"{v:.2f}", size=8, fill=_INK)
            c.text(x0 - 6, y0 + i * ch + ch / 2 + 3, a[:10], size=8,
                   anchor="end", fill=_AXIS)
        for j, b in enumerate(numcols):
            c.text(x0 + j * cw + cw / 2, y1 + 14, b[:10], size=8, fill=_AXIS)
        c.text((x0 + x1) / 2, 20, title, size=12, weight="bold")
        n_marks = len(numcols) ** 2

    else:
        labels, numeric_x, label_pos, sx_lo, sx_hi = _xscale(recs)
        pad = (max(yvals) - min(yvals)) * 0.08 or (abs(max(yvals)) * 0.05 or 1.0)
        y_lo, y_hi = min(yvals) - pad, max(yvals) + pad
        # ★ 直方图的 y 轴量的是**频数**，不是原始观测值。
        #   首版拿原始值的 min/max 当轴范围、却按 cnt/top 画柱高，
        #   刻度与柱高是两套量纲 —— 图能打开、程序退出 0、结论却是错的。
        #   所以先把分箱算出来，再决定轴范围与轴标签。
        hist_bins: list[int] = []
        hist_top, hist_nb = 1.0, 1
        hist_lo_v, hist_hi_v = 0.0, 1.0
        if kind == "hist":
            hist_nb = max(3, min(14, math.ceil(math.log2(len(yvals)) + 1)))
            hist_lo_v, hist_hi_v = min(yvals), max(yvals)
            span = (hist_hi_v - hist_lo_v) or 1.0
            hist_bins = [0] * hist_nb
            for v in yvals:
                hist_bins[min(hist_nb - 1, int((v - hist_lo_v) / span * hist_nb))] += 1
            hist_top = max(hist_bins) * 1.15
            y_lo, y_hi = 0.0, hist_top
            ylabel = "频数（count）"
        sx, sy = _draw_frame(c, x0=x0, y0=y0, x1=x1, y1=y1,
                             xlabel=xlabel, ylabel=ylabel, title=title,
                             labels=labels, numeric_x=numeric_x,
                             label_pos=label_pos,
                             sx_lo=sx_lo, sx_hi=sx_hi, y_lo=y_lo, y_hi=y_hi)

        def xpos(xv: str) -> float:
            v = _num(xv) if numeric_x else label_pos.get(xv, 0.0)
            return sx(float(v) if v is not None else 0.0)

        if kind in ("bar", "barh", "errorbar"):
            bucket: dict[str, list[float]] = {}
            for xv, yv, _ in recs:
                bucket.setdefault(xv, []).append(yv)
            means = {k: statistics.fmean(v) for k, v in bucket.items()}
            n = max(len(means), 1)
            for i, (k, m) in enumerate(means.items()):
                col = _PALETTE[i % len(_PALETTE)]
                if kind == "barh":
                    yy = y0 + (i + 0.15) * (y1 - y0) / n
                    bh = (y1 - y0) / n * 0.7
                    bw = (x1 - x0) * (m - y_lo) / (y_hi - y_lo)
                    c.rect(x0, yy, bw, bh, col, tip=f"{k}: {_fmt(m)} (n={len(bucket[k])})")
                    c.text(x0 - 6, yy + bh / 2 + 3, k[:12], size=8,
                           anchor="end", fill=_AXIS)
                    c.text(min(x0 + bw + 5, x1 - 14), yy + bh / 2 + 3,
                           _fmt(m), size=8, anchor="start", fill=_INK)
                else:
                    slot = (x1 - x0) / n
                    bw = slot * 0.62
                    xx = x0 + i * slot + (slot - bw) / 2
                    top = sy(m)
                    c.rect(xx, top, bw, y1 - top, col,
                           tip=f"{k}: {_fmt(m)} (n={len(bucket[k])})")
                    # 数值标签落在**柱中心**，不是 x 轴刻度位置。
                    c.text(xx + bw / 2, top - 5, _fmt(m), size=8, fill=_INK)
        elif kind == "box":
            buckets: dict[str, list[float]] = {}
            key = (lambda r: r[2]) if groups else (lambda r: r[0])
            for r in recs:
                buckets.setdefault(key(r), []).append(r[1])
            keys = sorted(buckets)
            slot = (x1 - x0) / max(len(keys), 1)
            for i, k in enumerate(keys):
                v = buckets[k]
                cx = x0 + (i + 0.5) * slot
                bw = slot * 0.5
                q1, med, q3 = (_quantile(v, q) for q in (0.25, 0.5, 0.75))
                lo, hi = min(v), max(v)
                col = _color_of(groups, k) if groups else _PALETTE[i % len(_PALETTE)]
                c.line(cx, sy(hi), cx, sy(q3), _AXIS)
                c.line(cx, sy(q1), cx, sy(lo), _AXIS)
                c.line(cx - bw / 4, sy(hi), cx + bw / 4, sy(hi), _AXIS)
                c.line(cx - bw / 4, sy(lo), cx + bw / 4, sy(lo), _AXIS)
                c.rect(cx - bw / 2, sy(q3), bw, max(sy(q1) - sy(q3), 1.0), col,
                       tip=f"{k}: 样本数 n={len(v)}, 中位数={_fmt(med)}")
                c.line(cx - bw / 2, sy(med), cx + bw / 2, sy(med), _PAPER, 1.6)
                c.text(cx, y1 + 16, k[:12], size=8, fill=_AXIS)
        elif kind in ("line", "time_series"):
            series: dict[str, list[tuple[float, float, str]]] = {}
            for xv, yv, gv in recs:
                series.setdefault(gv, []).append((xpos(xv), yv, xv))
            for i, (gv, pts) in enumerate(series.items()):
                col = _color_of(groups, gv) if groups else _PALETTE[i % len(_PALETTE)]
                pts.sort(key=lambda p: p[0])
                c.polyline(((p[0], sy(p[1])) for p in pts), col)
                for px, py, xv in pts:
                    c.circle(px, sy(py), 2.6, col, tip=f"{xv}: {_fmt(py)}")
        elif kind == "scatter":
            for xv, yv, gv in recs:
                c.circle(xpos(xv), sy(yv), 3.0, _color_of(groups, gv),
                         tip=f"{xv}: {_fmt(yv)}")
        elif kind == "hist":
            span = (hist_hi_v - hist_lo_v) or 1.0
            for i, cnt in enumerate(hist_bins):
                bw = (x1 - x0) / hist_nb
                bh = (y1 - y0) * cnt / hist_top
                c.rect(x0 + i * bw + 1, y1 - bh, bw - 2, bh, _PALETTE[0],
                       tip=f"[{_fmt(hist_lo_v + span * i / hist_nb)}, "
                           f"{_fmt(hist_lo_v + span * (i + 1) / hist_nb)}): {cnt}")
        if groups and len(groups) > 1:
            _legend(c, groups, x1, y0)
        n_marks = c.n_mark

    desc = (f"荣珂原生 SVG 后端（零第三方依赖）| 图型 {kind} | "
            f"数据点 {len(recs)} | 数据源 {plan.get('table', '')} | "
            "预览级产物，未跑 PDF 六道渲染后审计门")
    svg = c.to_svg(title, desc)
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(svg, encoding="utf-8", newline="\n")

    # ── 读回 + 结构自检：不认「写文件成功」 ──────────────────────┐
    readback = p.read_text(encoding="utf-8")
    checks = inspect_svg(p)
    checks["readback_nonempty"] = len(readback) > 400
    checks["cjk_preserved"] = "荣珂原生 SVG 后端" in readback
    checks["marks_rendered"] = n_marks > 0 and c.n_mark > 0
    checks["n_text_nodes"] = c.n_text >= 5
    ok = all(bool(v) for v in checks.values())
    return {
        "ok": ok,
        "svg": str(p),
        "bytes": p.stat().st_size,
        "backend": "native-svg",
        "scope": "preview",
        "kind": kind,
        "n_points": len(recs),
        "n_marks": c.n_mark,
        "n_text": c.n_text,
        "font_stack": FONT_STACK,
        "checks": checks,
        "audit_scope": ("未跑 PDF 六道渲染后审计门（字形 ≥5 pt / 碰撞 / 对齐 / 位图）。"
                        "SVG 是矢量文本，字形由查看端字体栈解析，未做像素级验证。"),
        "publish_note": ("投稿级请改装 pip install \"rongke[figure]\" 后走 "
                         "plot_data + audit_figure；本产物只作预览 / 网页 / 插图。"),
        "note": "" if ok else "结构自检有未通过项（见 checks）—— 文件保留在盘上供核查，不算成功。",
    }


def inspect_svg(path: str | Path) -> dict[str, bool]:
    """纯标准库解析刚写出的 SVG，逐项跑结构自检。

    这些检查是**换被审对象之后的等价检查**，不是「跳过当通过」：
    投稿路线量 PDF 的字形/碰撞，预览路线量 SVG 的结构/字体栈/坐标。
    """
    p = Path(path)
    out: dict[str, bool] = {"file_exists": p.is_file()}
    if not p.is_file():
        return out
    try:
        raw = p.read_text(encoding="utf-8")
        root = ET.fromstring(raw)
    except Exception:
        out.update({"xml_parsable": False, "has_viewbox": False,
                    "has_title": False, "has_text": False, "has_marks": False,
                    "marks_in_viewbox": False, "cjk_font_stack_ok": False,
                    "no_external_refs": False})
        return out
    ns = "{http://www.w3.org/2000/svg}"
    tag = lambda e: e.tag.replace(ns, "")                    # noqa: E731
    out["xml_parsable"] = tag(root) == "svg"
    vb = (root.get("viewBox") or "").split()
    out["has_viewbox"] = len(vb) == 4 and float(vb[2]) > 0 and float(vb[3]) > 0
    W, H = (float(vb[2]), float(vb[3])) if out["has_viewbox"] else (0.0, 0.0)
    texts = [e for e in root.iter() if tag(e) == "text"]
    out["has_title"] = any(tag(e) == "title" and (e.text or "") for e in root.iter())
    out["has_text"] = len(texts) >= 5
    marks = [e for e in root.iter() if e.get("class") == "mark"]
    out["has_marks"] = len(marks) > 0

    def inside(x: float, yy: float) -> bool:
        return -1 <= x <= W + 1 and -1 <= yy <= H + 1

    def rect_inside(e) -> bool:
        x, yy = float(e.get("x", 0)), float(e.get("y", 0))
        # 只看左上角会放过「柱子从框内一直画到框外」；右下角也要量。
        return (inside(x, yy) and
                inside(x + float(e.get("width", 0)), yy + float(e.get("height", 0))))

    out["marks_in_viewbox"] = bool(marks) and all(
        rect_inside(e) if tag(e) == "rect" else
        inside(float(e.get("cx", 0)), float(e.get("cy", 0)))
        if tag(e) == "circle" else
        all(inside(*map(float, pair.split(",")))
            for pair in (e.get("points") or "").split() if "," in pair)
        for e in marks)
    out["texts_in_viewbox"] = bool(texts) and all(
        inside(float(e.get("x", 0)), float(e.get("y", 0))) for e in texts)
    has_cjk = any(_CJK.search("".join(e.itertext())) for e in root.iter())
    # ★ 分母只能是**字体声明本身**。早先这里拿整个 SVG 原文去搜字体名，
    #   `<desc>`/注释里出现一次就恒真 —— 判据等于没写。
    #   真正的失败形态是「有中文、font-family 却只有 Arial」⇒ 必须打红。
    fam_parts = [e.get("font-family") or "" for e in root.iter()]
    fam_parts += ["".join(e.itertext()) for e in root.iter() if tag(e) == "style"]
    fam = " ".join(fam_parts)
    out["cjk_font_stack_ok"] = (not has_cjk) or (
        "Microsoft YaHei" in fam and "SimHei" in fam and FONT_STACK in fam)
    out["no_external_refs"] = not any(
        tag(e) in ("image", "script", "foreignObject") or
        any(k.endswith("href") for k in e.attrib)
        for e in root.iter())
    return out
