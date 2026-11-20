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
import json
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

#: 数据图的「成稿默认」视觉。纸感米色留给排版稿与机制图，数据图用白底。
#: 单系列一律主色：按**类别序号**轮转 6 色会给出一张没有语义的假色标
#: （实测 12 根柱子 6 色循环，读者会以为颜色在编码什么，其实什么都没有）。
_DATA_PAPER = "#FFFFFF"
_GRID_SOFT = "#E9E6DE"
_SERIES = _PALETTE[0]
_MAX_LEGEND_GROUPS = 6
#: 与 analysis.FULL_WIDTH_MM 同值（单源在 analysis）。刻意不在模块顶层
#: import analysis 以免循环；oneshot.py 有判据钉住两处相等，防枚举漂移。
_WIDE_WIDTH_MM = 183.0
#: 与 analysis.COL_WIDTH_MM 同值；oneshot.py 有判据钉住防漂移。
_DEFAULT_WIDTH_MM = 89.0

#: 2026-11-11 P0 扩充 5 种（Vega-Lite BSD-3-Clause / Plotly MIT 的**种类名称与
#: 语义事实**，几何与统计全部自己用标准库算，未搬上游一行代码）：
#:   area 面积 · stacked_bar 堆叠柱 · errorbar 误差棒 · errorband 置信带 · dotplot 点图
SUPPORTED_KINDS = (
    "line", "time_series", "scatter", "bar", "barh", "hist", "box",
    "heatmap", "matrix",
    "area", "stacked_bar", "errorbar", "errorband", "dotplot",
)
#: 误差类图型的数据契约：不编造误差，全部由给出的观测值算出来。
ERROR_KINDS = ("errorbar", "errorband")
NEW_KINDS = ("area", "stacked_bar", "errorbar", "errorband", "dotplot")


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
    """收集 SVG 元素 + 记账（文本几何意图与标记框），供质量门消费。

    ★ 每个 `<text>` 都发一个稳定 `id`，并把**声明意图**（字号/锚点/旋转/
      所属容器）记进 `text_specs`。这个 id 是两层尺子能对话的唯一桥梁：
      浏览器层量回来的真实 bbox 靠它 join 回声明值，否则实测与声明各说各话。
    """

    def __init__(self, w: float, h: float, paper: str = _PAPER,
                 transparent: bool = False):
        self.w, self.h = w, h
        self.paper = paper
        self.transparent = transparent
        self.body: list[str] = []
        self.defs: list[str] = []
        self.n_text = 0
        self.n_mark = 0
        self.text_specs: list[dict] = []
        self.mark_boxes: list[tuple[float, float, float, float]] = []

    def add_def(self, fragment: str) -> None:
        """往 `<defs>` 里塞一段（渐变 / 图案）。生物图元的球体渐变靠它。"""
        if fragment and fragment not in self.defs:
            self.defs.append(fragment)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, fill: str, *,
                stroke: str = "", cls: str = "frame", tip: str = "",
                stroke_width: float = 0.8, opacity: float = 1.0) -> None:
        """椭圆。区室画成细胞膜轮廓、细胞器都要它（rect 画不出膜）。"""
        if cls == "mark":
            self.n_mark += 1
            self.mark_boxes.append((cx - rx, cy - ry, cx + rx, cy + ry))
        sw = (f' stroke="{_esc(stroke)}" stroke-width="{stroke_width:.2f}"'
              if stroke else "")
        op = f' fill-opacity="{opacity:.2f}"' if opacity < 1.0 else ""
        t = f"<title>{_esc(tip)}</title>" if tip else ""
        base = (f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" '
                f'fill="{_esc(fill)}" class="{cls}"{sw}{op}')
        self.body.append(base + f'>{t}</ellipse>' if t else base + '/>')

    def path(self, d: str, fill: str, *, stroke: str = "", cls: str = "frame",
             box: tuple | None = None, tip: str = "", stroke_width: float = 0.8,
             opacity: float = 1.0) -> None:
        """任意路径（细胞器、蛋白团、抗体）。`box` 是它的包围框，供保真门对齐。

        机制图插图模式的图元全是 path/ellipse：流程图那套 rect+circle 表达不了
        「这是一个细胞」这种语义。
        """
        if cls == "mark":
            self.n_mark += 1
            if box:
                self.mark_boxes.append(tuple(box))
        sw = (f' stroke="{_esc(stroke)}" stroke-width="{stroke_width:.2f}"'
              if stroke else "")
        op = f' fill-opacity="{opacity:.2f}"' if opacity < 1.0 else ""
        t = f"<title>{_esc(tip)}</title>" if tip else ""
        base = (f'<path d="{d}" fill="{_esc(fill)}" class="{cls}"{sw}{op}')
        self.body.append(base + f'>{t}</path>' if t else base + '/>')

    def fragment(self, svg: str) -> None:
        """塞一段已经拼好的 SVG（图元库的输出）。记账由调用方自己负责。"""
        if svg:
            self.body.append(svg)

    def text(self, x: float, y: float, s: Any, *, size: float = 10,
             anchor: str = "middle", fill: str = _INK,
             weight: str = "", rotate: str = "",
             box: tuple | None = None) -> None:
        """画一段文字。`box` 是它**应当被装进去**的容器框 (x0,y0,x1,y1)；
        给了它，文字容纳门才有框可比（机制图的节点标签就靠这个）。"""
        tid = f"t{self.n_text}"
        self.n_text += 1
        self.text_specs.append({
            "id": tid, "x": float(x), "y": float(y), "size": float(size),
            "anchor": anchor, "rotate": float(rotate) if rotate else 0.0,
            "fill": fill, "text": str(s),
            "box": [float(v) for v in box] if box else None,
        })
        # rotate 只接受**角度**；旋转中心固定为文字自己的 (x, y)。
        # 早先这里把中心点也塞进 rotate 参数，拼出 5 个数的
        # `rotate(-90 13 150 13 150)` —— SVG 只认 3 个，整个文本不显示。
        extra = (f' font-weight="{weight}"' if weight else "") \
            + (f' transform="rotate({rotate} {x:.1f} {y:.1f})"' if rotate else "")
        self.body.append(
            f'<text id="{tid}" x="{x:.1f}" y="{y:.1f}" font-size="{size:.1f}" '
            f'text-anchor="{anchor}" fill="{fill}"{extra}>{_esc(s)}</text>')

    def rect(self, x: float, y: float, w: float, h: float, fill: str,
             *, stroke: str = "", cls: str = "mark", tip: str = "",
             opacity: float = 1.0, rx: float = 0.0,
             stroke_width: float = 0.8, dash: str = "") -> None:
        if cls == "mark":
            self.n_mark += 1
            self.mark_boxes.append((x, y, x + w, y + h))
        sw = (f' stroke="{_esc(stroke)}" stroke-width="{stroke_width:.2f}"'
              if stroke else "")
        da = f' stroke-dasharray="{dash}"' if dash else ""
        op = f' fill-opacity="{opacity:.2f}"' if opacity < 1.0 else ""
        rxa = f' rx="{rx:.2f}"' if rx > 0 else ""
        t = f"<title>{_esc(tip)}</title>" if tip else ""
        base = (f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(w, 0):.2f}" '
                f'height="{max(h, 0):.2f}" fill="{_esc(fill)}" '
                f'class="{cls}"{sw}{da}{op}{rxa}')
        self.body.append(base + f'>{t}</rect>' if t else base + '/>')

    def polygon(self, pts: Iterable[tuple[float, float]], fill: str, *,
                stroke: str = "", cls: str = "mark", tip: str = "",
                stroke_width: float = 0.8, opacity: float = 1.0) -> None:
        """闭合填充多边形。机制图的菱形判定框与箭头都靠它。

        ★ 为什么不用 `<path>` + `<marker>`：marker 的朝向与缩放由查看端算，
          工具这边量不到它的实际占位 —— 等于把一块几何交给「但愿它渲染对」。
          显式多边形每个顶点都在自己手里，门才量得到。
        """
        p = list(pts)
        if len(p) < 3:
            return
        xs = [q[0] for q in p]
        ys = [q[1] for q in p]
        # mark_boxes 与 n_mark 必须一一对应：清单里 marks 的条数就是声明的
        # n_marks。箭头走 class="arrow"，不是标记，两边都不记。
        if cls == "mark":
            self.n_mark += 1
            self.mark_boxes.append((min(xs), min(ys), max(xs), max(ys)))
        sw = (f' stroke="{_esc(stroke)}" stroke-width="{stroke_width:.2f}"'
              if stroke else "")
        op = f' fill-opacity="{opacity:.2f}"' if opacity < 1.0 else ""
        t = f"<title>{_esc(tip)}</title>" if tip else ""
        d = " ".join(f"{x:.2f},{y:.2f}" for x, y in p)
        base = (f'<polygon points="{d}" fill="{_esc(fill)}" '
                f'class="{cls}"{sw}{op}')
        self.body.append(base + f'>{t}</polygon>' if t else base + '/>')

    def circle(self, cx: float, cy: float, r: float, fill: str, *, tip: str = "",
               cls: str = "mark") -> None:
        # 组合形状（圆 + 内填、圆柱 + 端盖）里只有**主形状**算 mark，
        # 装饰图元走 cls="frame"：否则「一个节点 = 一个标记」的保真门会
        # 把一个组合节点误数成三个节点。记账口径与 rect/polygon 一致。
        if cls == "mark":
            self.n_mark += 1
            self.mark_boxes.append((cx - r, cy - r, cx + r, cy + r))
        t = f"<title>{_esc(tip)}</title>" if tip else ""
        if t:
            self.body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" '
                             f'fill="{_esc(fill)}" class="{cls}">{t}</circle>')
        else:
            self.body.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" '
                             f'fill="{_esc(fill)}" class="{cls}"/>')

    def line(self, x1: float, y1: float, x2: float, y2: float,
             stroke: str, width: float = 1.0, *, cls: str = "",
             dash: str = "") -> None:
        c = f' class="{cls}"' if cls else ""
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.body.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" '
                         f'y2="{y2:.2f}" stroke="{_esc(stroke)}" '
                         f'stroke-width="{width:.2f}"{c}{da}/>')

    def polyline(self, pts: Iterable[tuple[float, float]], stroke: str,
                 width: float = 1.8, *, dash: str = "") -> None:
        p = list(pts)
        if len(p) < 2:
            return
        self.n_mark += 1
        # 一条折线记一个包围框（与 read_svg 的口径一致），不是每个折点记一个：
        # 逐点记会让清单里 marks 的条数与 n_marks 对不上，保真门就失去意义。
        xs = [q[0] for q in p]
        ys = [q[1] for q in p]
        self.mark_boxes.append((min(xs), min(ys), max(xs), max(ys)))
        d = " ".join(f"{x:.2f},{y:.2f}" for x, y in p)
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.body.append(f'<polyline points="{d}" fill="none" '
                         f'stroke="{_esc(stroke)}" stroke-width="{width:.2f}" '
                         f'stroke-linejoin="round" class="mark"{da}/>')

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
            + (f'  <defs>{"".join(self.defs)}</defs>\n' if self.defs else "")
            + f'  <g id="chart" font-family="{_esc(FONT_STACK)}">\n'
            # 背景 rect 保留 `fill` 供质量门算对比度，但数据图设为
            # 完全透明：插进米色排版稿 / PPT 时不该浮出一块白框。
            f'    <rect x="0" y="0" width="{self.w:.0f}" height="{self.h:.0f}" '
            f'fill="{self.paper}" class="paper"'
            + (' fill-opacity="0"/>\n' if self.transparent else '/>\n')
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


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """把刻度取整到 1 / 2 / 2.5 / 5 × 10^k，并保证落在轴范围内。

    均分 6 段会吐出 44.3 / 40.5 / 36.7 / 32.8 这种既不好读、
    也不像期刊插图的怪值。这是**刻度生成**的事，与数据无关。
    """
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = next((m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag),
                10 * mag)
    start = math.floor(lo / step) * step
    out: list[float] = []
    v = start
    while v <= hi + 1e-9 and len(out) <= 12:
        if v >= lo - 1e-9:
            out.append(round(v, 10))
        v += step
    if len(out) < 2:
        return [lo + (hi - lo) * i / 5 for i in range(6)]
    return out


def _draw_frame(c: _Canvas, *, x0, y0, x1, y1, xlabel, ylabel, title,
                labels, numeric_x, label_pos, sx_lo, sx_hi, y_lo, y_hi,
                horizontal: bool = False, x_ticks: bool = True,
                show_title: bool = True) -> tuple:
    """画边框/网格/刻度。返回 (sx, sy) 两个映射函数。

    `horizontal=True`（横向柱状图）：数值轴横过来躺在底部，类别轴竖在左侧。
    ★ 为什么必须分这一支：barh 的柱长由 `(m-y_lo)/(y_hi-y_lo)` 算，数值轴是 x；
      而默认框把数值刻度画在 y 轴、类别标签画在 x 轴 —— 两套轴同时上屏，
      左侧的数值刻度还与 barh 自己画的类别标签撞在一起（实测重叠 18×8.6px）。
      轴标题也跟着换：底部是数值变量（ylabel），左侧是类别变量（xlabel）。
    `x_ticks=False`：类别标签由调用方自己画（箱线图按**分组**而非原始 x 列
    分类，位置与默认框算出来的不是一回事）。默认框再画一遍就是两套类别轴。
    """
    def sx(v: float) -> float:
        t = 0.5 if sx_hi <= sx_lo else (v - sx_lo) / (sx_hi - sx_lo)
        return x0 + max(0.0, min(1.0, t)) * (x1 - x0)

    def sy(v: float) -> float:
        t = 0.5 if y_hi <= y_lo else (v - y_lo) / (y_hi - y_lo)
        return y1 - max(0.0, min(1.0, t)) * (y1 - y0)

    if show_title:
        c.text((x0 + x1) / 2, 20, title, size=10.5, weight="bold")
    # 数值轴：整数刻度 + 极淡网格。horizontal 时它是 x 轴，否则是 y 轴。
    for v in _nice_ticks(y_lo, y_hi):
        if horizontal:
            t = 0.5 if y_hi <= y_lo else (v - y_lo) / (y_hi - y_lo)
            xx = x0 + max(0.0, min(1.0, t)) * (x1 - x0)
            c.line(xx, y0, xx, y1, _GRID_SOFT, 0.6)
            c.line(xx, y1, xx, y1 + 4, _AXIS, 0.9)
            c.text(xx, y1 + 16, _fmt(v), size=8, fill=_AXIS)
        else:
            yy = sy(v)
            c.line(x0, yy, x1, yy, _GRID_SOFT, 0.6)
            c.line(x0 - 4, yy, x0, yy, _AXIS, 0.9)
            c.text(x0 - 8, yy + 3.5, _fmt(v), size=8, anchor="end", fill=_AXIS)
    c.line(x0, y0, x0, y1, _AXIS, 0.9)
    c.line(x0, y1, x1, y1, _AXIS, 0.9)
    # 类别轴刻度：按**标签真实宽度**决定抽稀与截断，不按「标签个数 / 8」
    # 拍脑袋。实测（finish_lab/oneshot/calendar）：12 个 `2026-01-01`
    # 各约 45px，按个数抽到 6 个仍互相压字，collision 门实测 5 处、
    # 肉眼看连成一串。89mm 单栏里能放几个完整标签是**几何**决定的。
    def _text_px(s: str, size: float = 8.0) -> float:
        return sum(size * (1.0 if ord(ch) > 0x2E80 else 0.56) for ch in s)

    slot = (x1 - x0) / max(len(labels), 1)
    widest = max((_text_px(str(lb)) for lb in labels), default=0.0)
    step = max(1, math.ceil((widest + 6.0) / max(slot, 1.0)))
    avail = slot * step - 6.0
    # 上面按「均匀槽宽」估的 step 对**数值轴不成立**：直方图的观测值、
    # 不等距时间点挤在一起时，标签的真实 x 位置远比槽宽近（实测
    # single 组 collision 1 处）。再按真实位置贪心过一遍，两道保险。
    placed: list[tuple[float, float]] = []
    for i, lb in enumerate(labels):
        # horizontal 的类别标签由 barh 自己画（它知道自己的槽位），这里不插手。
        if horizontal or not x_ticks or i % step:
            continue
        xv = _num(lb) if numeric_x else label_pos.get(lb, (i + 0.5) / max(len(labels), 1))
        xx = sx(float(xv) if xv is not None else float(i))
        s = str(lb)
        if _text_px(s) > avail:
            clipped = ""
            for ch in s:
                if _text_px(clipped + ch) > max(8.0, avail - 8.0):
                    break
                clipped += ch
            s = (clipped + "…") if clipped else s[:1]
        # 缓冲 2px：`_text_px` 对 ASCII 按 0.56em 估，比真实字形略宽；
        # 早先用 4px 会把通栏下明明放得下的相邻日期误判成重叠
        # （calendar 实测只标 6/12）。取 2px，再由七道门实测兜底。
        half = _text_px(s) / 2.0 + 2.0
        if any(abs(xx - px) < half + ph for px, ph in placed):
            continue
        placed.append((xx, half))
        c.line(xx, y1, xx, y1 + 4, _AXIS, 0.9)
        c.text(xx, y1 + 16, s, size=8, fill=_AXIS)
    if horizontal:
        c.text((x0 + x1) / 2, c.h - 6, ylabel, size=9.5, fill=_INK)
        c.text(13, (y0 + y1) / 2, xlabel, size=9.5, fill=_INK, rotate="-90")
    else:
        c.text((x0 + x1) / 2, c.h - 6, xlabel, size=9.5, fill=_INK)
        c.text(13, (y0 + y1) / 2, ylabel, size=9.5, fill=_INK, rotate="-90")
    return sx, sy


def _legend(c: _Canvas, groups: list[str], x0: float, x1: float, y: float) -> None:
    """图例横排在**绘图区外**的顶栏里（y 是文字基线）。

    ★ 为什么不画在绘图区右上角：那里正是数据最高的地方 —— 柱状图的数值标签、
      散点图的点都在那儿。实测它压住最高柱的数值标签 8.7×2.8px。图例挤进绘图区
      等于赌数据不会长到那儿，而「保证质量」不能靠赌：把它挪出去，重叠在结构上
      就不可能发生。标签按槽宽截断，组名全文另见 tooltip / 数据表。
    """
    gs = groups[:6]
    if not gs:
        return
    slot = (x1 - x0) / len(gs)
    budget = max(2, int((slot - 29.0) / 8.0))
    for i, g in enumerate(gs):
        xx = x0 + i * slot
        # 图例色必须与系列色**同源**：早先这里按 i 轮转、折线按
        # groups.index() 轮转，两套路子一对齐就给出假映射。
        c.rect(xx, y - 8, 9, 9, _color_of(groups, g), cls="legend")
        c.text(xx + 13, y, g if len(g) <= budget else g[:budget] + "…",
               size=8, anchor="start", fill=_INK)


def _color_of(groups: list[str], g: str) -> str:
    if not groups or len(groups) < 2:
        return _PALETTE[0]
    return _PALETTE[groups.index(g) % len(_PALETTE)] if g in groups else _PALETTE[0]


def render_svg(rows: list[dict], plan: dict, out: str | Path) -> dict:
    """按 plan 把一张表画成 SVG。**写完读回并自检**。

    返回紧凑 dict；`ok=False` 时产物仍留在盘上供人查，绝不说「成功」。
    """
    kind, x, y, g, recs = _records(rows, plan)
    labels, numeric_x, label_pos, sx_lo, sx_hi = _xscale(recs)
    width_mm = float(plan.get("width_mm") or 89.0)
    height_mm = float(plan.get("height_mm") or 58.0)
    # 类别多到 89mm 单栏装不下完整标签 ⇒ 工具自动升到通栏，一次全标。
    # w = width_mm * 96/25.4 ⇒ mm/px 恒定，字号 px 不变则**物理字号不变**；
    # 排版侧按宽图 100% 落位（见 typeset.render），不把字压到 5pt 以下。
    # ★ 只接管**默认单栏宽度**：调用方显式给了别的宽度（尤其 <84.7mm
    #   触发 320px 钳位的窄图）必须完全尊重 —— 否则 svgq 的钳位判据
    #   「声明 60mm / 实际 320px」前提被改掉，有效判据会平白打红。
    wide = (kind not in ("heatmap", "matrix", "barh", "box")
            and len(labels) > 8
            and abs(width_mm - _DEFAULT_WIDTH_MM) < 1e-6
            and width_mm < _WIDE_WIDTH_MM)
    if wide:
        width_mm = _WIDE_WIDTH_MM
        height_mm = max(height_mm, 78.0)
    w = max(320.0, width_mm * _MM_TO_PX)
    h = max(220.0, height_mm * _MM_TO_PX)
    c = _Canvas(w, h, paper=_DATA_PAPER, transparent=True)
    x0, x1, y0, y1 = 54.0, w - 16.0, 34.0, h - 46.0
    cat_budget = 12
    if kind == "barh":
        # 横向柱的类别标签躺在**左侧**，默认的 54px 边距装不下几个字。
        # 按最长标签放宽左边距（上限吃掉画布宽的 38%，不然没地方画柱了），
        # 再按边距反推能放几个字 —— 截断的全文进 tooltip，不静默丢信息。
        longest = max((len(str(r[0])) for r in recs), default=1)
        x0 = min(w * 0.38, 54.0 + max(0.0, longest - 3) * 8.0)
        cat_budget = max(2, int((x0 - 28.0) / 8.0) - 1)

    yvals = [r[1] for r in recs]
    groups = sorted({r[2] for r in recs if r[2]})
    # ★ 图例只挂在**真按组着色**的图型上。bar / barh / errorbar 的颜色是
    #   `_PALETTE[i % …]`，i 是**类别**序号，与组别无关 —— 挂一张组别图例
    #   等于给读者一个假映射。hist 只有一色，更不需要。
    has_legend = (1 < len(groups) <= _MAX_LEGEND_GROUPS
                  and kind in ("line", "time_series", "scatter", "box",
                               "area", "stacked_bar", "errorband"))
    if has_legend:
        y0 = 46.0          # 顶栏让给图例，绘图区从它下面开始
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
        # ── 新图型的轴范围必须跟它真正画出来的量纲一致 ──────────────
        #   堆叠柱量的是「累计」，误差类量的是「mean ± sd」；若沿用原始观测
        #   的 min/max，柱会戳出画框、误差棒会画到画布外（结构上就错了）。
        stack_acc: dict[str, dict[str, float]] = {}
        err_stats: dict[str, tuple[float, float]] = {}
        if kind == "stacked_bar":
            for xv, yv, gv in recs:
                stack_acc.setdefault(xv, {}).setdefault(gv or "全体", 0.0)
                stack_acc[xv][gv or "全体"] += max(yv, 0.0)
            totals = [sum(d.values()) for d in stack_acc.values()]
            if totals:
                y_lo, y_hi = 0.0, max(totals) * 1.08
                ylabel = f"{ylabel}（堆叠合计）"
        if kind in ERROR_KINDS:
            bucket: dict[str, list[float]] = {}
            for xv, yv, _ in recs:
                bucket.setdefault(xv, []).append(yv)
            for k, v in bucket.items():
                err_stats[k] = (statistics.fmean(v),
                                statistics.stdev(v) if len(v) > 1 else 0.0)
            if err_stats:
                y_lo = min(y_lo, min(m - sd for m, sd in err_stats.values()) - pad)
                y_hi = max(y_hi, max(m + sd for m, sd in err_stats.values()) + pad)
        if kind == "errorband":
            # 滑动窗口 sd 可能大于按 x 聚合的 sd ⇒ 用全局窗口上界再兜一层。
            ws = [yvals[max(0, j - 1):j + 2] for j in range(len(yvals))]
            wsd = max((statistics.stdev(v) for v in ws if len(v) > 1),
                      default=0.0)
            y_lo = min(y_lo, min(yvals) - wsd - pad)
            y_hi = max(y_hi, max(yvals) + wsd + pad)
        # 柱 / 面积类从 0 起：截断 y 轴会把 30→56 的差距画成"翻好几倍"，
        # 既误导读者也不是期刊插图。负值数据不强制（否则柱要画到框外）。
        if (kind in ("bar", "barh", "stacked_bar", "area", "hist")
                and min(yvals) >= 0):
            y_lo = 0.0
        sx, sy = _draw_frame(c, x0=x0, y0=y0, x1=x1, y1=y1,
                             xlabel=xlabel, ylabel=ylabel, title=title,
                             labels=labels, numeric_x=numeric_x,
                             label_pos=label_pos,
                             sx_lo=sx_lo, sx_hi=sx_hi, y_lo=y_lo, y_hi=y_hi,
                             horizontal=(kind == "barh"),
                             # 箱线图按**分组**（有 group 时）或 x 列分类，
                             # 槽位由它自己算；默认框再画一遍就是两套类别轴。
                             x_ticks=(kind != "box"),
                             # 工具兜底的图题只进 <title>/desc（无障碍），
                             # 不占图面；期刊插图的标题由排版题注给。
                             show_title=not bool(plan.get("title_auto")))

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
                col = _SERIES
                if kind == "barh":
                    yy = y0 + (i + 0.15) * (y1 - y0) / n
                    bh = (y1 - y0) / n * 0.7
                    bw = (x1 - x0) * (m - y_lo) / (y_hi - y_lo)
                    c.rect(x0, yy, bw, bh, col, tip=f"{k}: {_fmt(m)} (n={len(bucket[k])})")
                    lab = k if len(k) <= cat_budget else k[:cat_budget] + "…"
                    c.text(x0 - 6, yy + bh / 2 + 3, lab, size=8,
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
                col = _color_of(groups, k) if groups else _SERIES
                c.line(cx, sy(hi), cx, sy(q3), _AXIS)
                c.line(cx, sy(q1), cx, sy(lo), _AXIS)
                c.line(cx - bw / 4, sy(hi), cx + bw / 4, sy(hi), _AXIS)
                c.line(cx - bw / 4, sy(lo), cx + bw / 4, sy(lo), _AXIS)
                c.rect(cx - bw / 2, sy(q3), bw, max(sy(q1) - sy(q3), 1.0), col,
                       tip=f"{k}: 样本数 n={len(v)}, 中位数={_fmt(med)}")
                c.line(cx - bw / 2, sy(med), cx + bw / 2, sy(med), _DATA_PAPER, 1.6)
                # 类别轴归箱线图自己画（默认框那边 x_ticks=False）。
                # 标签按槽宽截断，全文在 tooltip 里 —— 不静默丢信息。
                c.line(cx, y1, cx, y1 + 4, _AXIS, 1.0)
                budget = max(2, int(slot / 8.0) - 1)
                c.text(cx, y1 + 16, k if len(k) <= budget else k[:budget] + "…",
                       size=8, fill=_AXIS)
        elif kind in ("line", "time_series"):
            series: dict[str, list[tuple[float, float, str]]] = {}
            for xv, yv, gv in recs:
                series.setdefault(gv, []).append((xpos(xv), yv, xv))
            for i, (gv, pts) in enumerate(series.items()):
                col = _color_of(groups, gv) if groups else _SERIES
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
        # ══ P0 新增 5 种图型：同一份 recs，误差只由给出的观测值算 ══
        if kind == "area":
            series: dict[str, list[tuple[float, float]]] = {}
            for xv, yv, gv in recs:
                series.setdefault(gv, []).append((xpos(xv), yv))
            for i, (gv, pts) in enumerate(series.items()):
                col = _color_of(groups, gv) if groups else _SERIES
                pts.sort(key=lambda p: p[0])
                if len(pts) < 2:
                    c.circle(pts[0][0], sy(pts[0][1]), 3.0, col,
                             tip=f"{gv or '全体'}: {_fmt(pts[0][1])}")
                    continue
                poly = [(px, sy(py)) for px, py in pts]
                poly += [(pts[-1][0], y1), (pts[0][0], y1)]
                c.polygon(poly, col, opacity=0.30,
                          tip=f"{gv or '全体'} 面积（基线 = 绘图区底边）")
                c.polyline(((px, sy(py)) for px, py in pts), col)
        elif kind == "stacked_bar":
            keys = sorted(stack_acc)
            series_names = sorted(groups) if groups else ["全体"]
            slot = (x1 - x0) / max(len(keys), 1)
            bw = slot * 0.62
            for i, k in enumerate(keys):
                base = 0.0
                xx = x0 + i * slot + (slot - bw) / 2
                for j, gn in enumerate(series_names):
                    v = stack_acc[k].get(gn, 0.0)
                    if v <= 0:
                        continue
                    top, bottom = sy(base + v), sy(base)
                    c.rect(xx, top, bw, max(bottom - top, 1.0),
                           _color_of(groups, gn) if groups else _SERIES,
                           tip=f"{k} / {gn}: {_fmt(v)}（累计 {_fmt(base + v)}）")
                    base += v
                c.text(xx + bw / 2, sy(base) - 5, _fmt(base), size=8, fill=_INK)
        elif kind == "errorband":
            series = {}
            for xv, yv, gv in recs:
                series.setdefault(gv, []).append((xpos(xv), yv))
            clamp = lambda v: max(y0, min(y1, v))           # noqa: E731
            for i, (gv, raw) in enumerate(series.items()):
                col = _color_of(groups, gv) if groups else _SERIES
                pts = sorted(raw, key=lambda p: p[0])
                vals = [p[1] for p in pts]
                up, dn = [], []
                for j in range(len(pts)):
                    win = vals[max(0, j - 1):j + 2]
                    sd = statistics.stdev(win) if len(win) > 1 else 0.0
                    up.append((pts[j][0], clamp(sy(pts[j][1] + sd))))
                    dn.append((pts[j][0], clamp(sy(pts[j][1] - sd))))
                if len(pts) >= 2:
                    c.polygon(up + list(reversed(dn)), col, opacity=0.22,
                              tip=f"{gv or '全体'} ±1 SD（相邻 3 点滑动窗口）")
                c.polyline(((p[0], sy(p[1])) for p in pts), col)
        elif kind == "dotplot":
            keys = sorted({r[0] for r in recs})
            slot = (x1 - x0) / max(len(keys), 1)
            slot_i = {k: i for i, k in enumerate(keys)}
            per: dict[str, int] = {}
            for xv, yv, gv in recs:
                n_seen = per.get(xv, 0)
                per[xv] = n_seen + 1
                same = sum(1 for r in recs if r[0] == xv)
                # 确定性错位：同类点按序号均分槽宽，不用随机抖动（可复现）。
                off = (n_seen + 1) / (same + 1) - 0.5
                px = x0 + (slot_i[xv] + 0.5) * slot + off * slot * 0.5
                c.circle(px, sy(yv), 3.2,
                         _color_of(groups, gv) if groups else _PALETTE[0],
                         tip=f"{xv} #{n_seen + 1}: {_fmt(yv)}")
        if kind == "errorbar":
            # 误差棒叠在均值柱上：mean ± 样本标准差，端帽是量得到的线段。
            n = max(len(err_stats), 1)
            slot = (x1 - x0) / n
            for i, (k, (m, sd)) in enumerate(err_stats.items()):
                cx = x0 + (i + 0.5) * slot
                c.line(cx, sy(m - sd), cx, sy(m + sd), _AXIS, 1.3)
                cap = slot * 0.16
                c.line(cx - cap, sy(m + sd), cx + cap, sy(m + sd), _AXIS, 1.3)
                c.line(cx - cap, sy(m - sd), cx + cap, sy(m - sd), _AXIS, 1.3)
                c.circle(cx, sy(m), 2.4, _SERIES,
                         tip=f"{k}: mean={_fmt(m)}, sd={_fmt(sd)}")
        if has_legend:
            _legend(c, groups, x0, x1, 34.0)
        n_marks = c.n_mark

    desc = (f"荣珂原生 SVG 后端（零第三方依赖）| 图型 {kind} | "
            f"数据点 {len(recs)} | 数据源 {plan.get('table', '')} | "
            "预览级产物，未跑 PDF 六道渲染后审计门")
    svg = c.to_svg(title, desc)
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(svg, encoding="utf-8", newline="\n")

    # ── 几何清单：两层尺子共用的被审对象 ────────────────────────
    # ★ `mm_per_px` 必须由**声明的 width_mm 与实际画布宽**算，不能直接用
    #   25.4/96。上面 `w = max(320.0, width_mm * _MM_TO_PX)` 那个钳位意味着：
    #   width_mm < 84.7 时画布被撑到 320px，而产物仍声称自己是 60mm 宽 ——
    #   摆进版面按 60mm 缩放后，8.5px 的字实际只有 4.5pt，低于 5pt 下限。
    #   把这两个数都记下来，字号门才算得对，钳位本身也才看得见。
    manifest = {
        "schema_version": 1,
        "backend": "native-svg",
        "figure": {
            "width_mm": width_mm, "height_mm": height_mm,
            "width_px": w, "height_px": h,
            "mm_per_px": width_mm / w,
            "intrinsic_width_mm": w * 25.4 / 96.0,
            "clamped": abs(w - width_mm * _MM_TO_PX) > 0.01,
        },
        "kind": kind, "n_points": len(recs),
        "n_marks": n_marks, "n_text": c.n_text,
        "texts": c.text_specs,
        "marks": [list(b) for b in c.mark_boxes],
    }
    mp = p.with_suffix(".layout.json")
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                  encoding="utf-8", newline="\n")

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
        "layout": str(mp),
        "bytes": p.stat().st_size,
        "backend": "native-svg",
        "scope": "preview",
        "kind": kind,
        "wide": wide,
        "width_mm_used": width_mm,
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
