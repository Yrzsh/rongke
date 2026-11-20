# -*- coding: utf-8 -*-
"""科研通路插图（native-pathway）—— 机制图的**另一种品类**。

## 为什么不是 `svgdiagram`

`svgdiagram` 画的是「节点-边自动分层框图」：圆角矩形/菱形/圆 + 直线箭头。
用户 2026-11-12 给的参考图（论文机制通路插图）完全不是这个品类 —— 那里是
磷脂双层膜、细胞核虚线轮廓、蛋白胶囊、波浪线 mRNA、m6A/磷酸化修饰点、
按细胞区室摆位、弧形转运线、T 形抑制、虚线间接、A/B 低饱和分区。
**继续往自动分层里加形状或布线，只会越跑越偏。**

## 本模块的纪律（与项目既有产出面同构）

- AI 只声明**构图意图**：图元种类、所属区室（panel/region）、相对位置 `at`、
  语义关系；像素坐标、图元尺寸、避让、连线路由、色板全由工具算。
- 不写输出格式字节；`SPEC_GUIDE` / `GLYPHS` / `PALETTE` 单源。
- 写后逐字节读回 + 结构自检；产出 `.layout.json` 让 `svgq` 七道门可审。
- `Scene` 提供单图元增删改 + `affected`/`diff`/`revision`/`summary` 回执，
  保持「给 AI 的操作面」而不是一次性打印机。

纯标准库，零第三方依赖。
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from .svgplot import FONT_STACK, _esc

# ══════════════════════════════════════════════════════════════════
# 单源：图元 / 关系 / 论文级低饱和配色
# ══════════════════════════════════════════════════════════════════

#: 色值取自用户给的「论文原图配色拆解」色卡（低饱和、可分区、可叠印）。
PALETTE = {
    "sand": "#F6F4D7",
    "blush": "#EAD1D0",
    "blue": "#A9B9E6",
    "clay": "#DACBC5",
    "cyan": "#8ED3EC",
    "violet": "#B899CA",
}
_INK = "#2E3A45"
_AXIS = "#7A8894"
_PAPER = "#FFFFFF"
_AMBER = "#E8B23A"
_M6A = "#4C7FD1"

GLYPHS = ("panel", "membrane", "organelle", "protein", "mrna",
          "mod", "molecule", "label")
RELATIONS = ("activate", "inhibit", "indirect", "transport")
_MM_TO_PX = 96.0 / 25.4

#: 图元默认像素尺寸（工具算几何，AI 不给 w/h）。
_SIZE = {
    "membrane": (420, 26), "organelle": (190, 130), "protein": (116, 32),
    "mrna": (170, 18), "mod": (14, 14), "molecule": (44, 26),
    "label": (110, 18),
}

SPEC_GUIDE = """科研通路插图规格（不是节点-边框图）：
panels=[{id,title,tone}] —— A/B 淡色分区，tone ∈ sand/blush/blue/clay/cyan/violet。
elements=[{id,kind,label?,tone?,panel,region?,at:[u,v],variant?}]：
  kind ∈ membrane(磷脂双层) / organelle(细胞器虚线轮廓) / protein(蛋白胶囊,
    variant=receptor 可跨膜) / mrna(波浪线) / mod(修饰点：m6A/磷酸化) /
    molecule(小分子点簇) / label(文字标注)。
  at 是**区室内的相对位置** [0..1, 0..1]，不是像素坐标；尺寸、避让、色板由工具算。
relations=[{from,to,kind,label?,bend?}]：
  kind ∈ activate(实线箭头) / inhibit(T 形横杠) / indirect(虚线) / transport(弧线)。
只声明图元与关系，不要写 SVG 字节，也不要写 x/y 像素。"""

OPS = ("new", "add_element", "add_relation", "set_element", "remove",
       "query", "describe", "render")


class PathwayError(ValueError):
    """规格不合法。说清楚为什么，不静默画个别的东西。"""


# ══════════════════════════════════════════════════════════════════
# 极简 SVG 累加器（自己收 texts/marks，供 manifest 与七道门读回）
# ══════════════════════════════════════════════════════════════════

class _C:
    def __init__(self, w: float, h: float):
        self.w, self.h = w, h
        self.body: list[str] = []
        self.texts: list[dict] = []
        self.marks: list[list[float]] = []

    def _a(self, s: str) -> None:
        self.body.append(s)

    def rect(self, x, y, w, h, fill="none", stroke="", sw=0.8,
             rx=0, dash="", cls="", mark=False):
        if mark:
            self.marks.append([x, y, x + w, y + h])
        sa = f' stroke="{stroke}" stroke-width="{sw:.2f}"' if stroke else ""
        da = f' stroke-dasharray="{dash}"' if dash else ""
        ra = f' rx="{rx:.1f}"' if rx else ""
        ca = f' class="{cls}"' if cls else ""
        self._a(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w,0):.1f}" '
                f'height="{max(h,0):.1f}" fill="{fill}"{sa}{da}{ra}{ca}/>')

    def ellipse(self, cx, cy, rx, ry, fill="none", stroke="", sw=0.8,
                dash="", mark=False):
        if mark:
            self.marks.append([cx - rx, cy - ry, cx + rx, cy + ry])
        sa = f' stroke="{stroke}" stroke-width="{sw:.2f}"' if stroke else ""
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self._a(f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{rx:.1f}" '
                f'ry="{ry:.1f}" fill="{fill}"{sa}{da}/>')

    def circle(self, cx, cy, r, fill, stroke="", sw=0.8):
        sa = f' stroke="{stroke}" stroke-width="{sw:.2f}"' if stroke else ""
        self._a(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
                f'fill="{fill}"{sa}/>')

    def line(self, x1, y1, x2, y2, stroke, sw=1.0, dash=""):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self._a(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" '
                f'y2="{y2:.1f}" stroke="{stroke}" stroke-width="{sw:.2f}"{da}/>')

    def poly(self, pts, fill="none", stroke="", sw=1.0, dash="", closed=False):
        tag = "polygon" if closed else "polyline"
        p = " ".join(f"{a:.1f},{b:.1f}" for a, b in pts)
        sa = f' stroke="{stroke}" stroke-width="{sw:.2f}"' if stroke else ""
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self._a(f'<{tag} points="{p}" fill="{fill}"{sa}{da}/>')

    def path(self, d: str, stroke, sw=1.0, dash="", fill="none"):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self._a(f'<path d="{d}" fill="{fill}" stroke="{stroke}" '
                f'stroke-width="{sw:.2f}"{da}/>')

    def text(self, tid, x, y, s, size=10, anchor="middle", fill=_INK,
             box=None, cls=""):
        bw = max(10.0, sum(size * (1.0 if ord(ch) > 0x2E80 else 0.62)
                           for ch in str(s)))
        bx0 = x - bw / 2 if anchor == "middle" else x
        bb = box or [bx0, y - size * 0.9, bx0 + bw, y + size * 0.3]
        self.texts.append({"id": tid, "text": str(s), "box": bb,
                           "size": size, "anchor": anchor})
        ca = f' class="{cls}"' if cls else ""
        self._a(f'<text id="{tid}" x="{x:.1f}" y="{y:.1f}" font-size="{size:.1f}" '
                f'text-anchor="{anchor}" fill="{fill}"{ca}>{_esc(str(s))}</text>')

    def to_svg(self, title: str) -> str:
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
                f'width="{self.w:.0f}px" height="{self.h:.0f}px" '
                f'viewBox="0 0 {self.w:.0f} {self.h:.0f}" role="img" '
                f'aria-labelledby="rk-title rk-desc">\n'
                f'<title id="rk-title">{_esc(title)}</title>\n'
                f'<desc id="rk-desc">科研通路原生 SVG 后端</desc>\n'
                f'<style>text {{ font-family: {FONT_STACK}; }}</style>\n'
                f'<g id="pathway" font-family="{_esc(FONT_STACK)}">\n')
        return head + "\n".join(self.body) + "\n</g>\n</svg>\n"


# ══════════════════════════════════════════════════════════════════
# 规格校验 + 几何（AI 给意图，工具算像素）
# ══════════════════════════════════════════════════════════════════

def validate(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise PathwayError("规格必须是一个对象。")
    panels = spec.get("panels")
    if not isinstance(panels, list) or not panels:
        raise PathwayError("panels 不能为空：至少要一个区室面板。")
    pids = set()
    for i, p in enumerate(panels):
        if not isinstance(p, dict) or not str(p.get("id") or "").strip():
            raise PathwayError(f"panels[{i}] 缺 id。")
        pids.add(str(p["id"]))
    elems = spec.get("elements")
    if not isinstance(elems, list):
        raise PathwayError("elements 必须是数组。")
    seen = set()
    for i, e in enumerate(elems):
        if not isinstance(e, dict):
            raise PathwayError(f"elements[{i}] 不是对象。")
        eid = str(e.get("id") or "").strip()
        if not eid or eid in seen:
            raise PathwayError(f"elements[{i}] 的 id 缺失或重复：`{eid}`。")
        seen.add(eid)
        kind = str(e.get("kind") or "")
        if kind not in GLYPHS or kind == "panel":
            raise PathwayError(
                f"元素 {eid} 的 kind=`{kind}` 不支持（支持："
                f"{'、'.join(k for k in GLYPHS if k != 'panel')}）。")
        if str(e.get("panel")) not in pids:
            raise PathwayError(f"元素 {eid} 指向不存在的面板 {e.get('panel')}。")
        at = e.get("at")
        if (not isinstance(at, list) or len(at) != 2
                or any(not isinstance(v, (int, float)) or not 0 <= v <= 1
                       for v in at)):
            raise PathwayError(f"元素 {eid} 的 at 要给两个 0..1 的相对位置。")
        if e.get("tone") and e["tone"] not in PALETTE:
            raise PathwayError(f"元素 {eid} 的 tone=`{e['tone']}` 不在色板里。")
    rels = spec.get("relations") or []
    for i, r in enumerate(rels):
        if not isinstance(r, dict):
            raise PathwayError(f"relations[{i}] 不是对象。")
        if str(r.get("from")) not in seen or str(r.get("to")) not in seen:
            raise PathwayError(f"relations[{i}] 指向不存在的元素。")
        if str(r.get("kind") or "activate") not in RELATIONS:
            raise PathwayError(f"relations[{i}] 的 kind 不支持。")
    return {"panels": panels, "elements": elems, "relations": rels,
            "title": str(spec.get("title") or "机制通路示意"),
            "width_mm": float(spec.get("width_mm") or 260.0)}


def layout(spec: dict) -> dict:
    """把相对位置编译成像素框，并做基本避让（几何全在工具侧）。"""
    v = validate(spec)
    W, H = 1240.0, 760.0
    title_h = 54.0
    gap = 18.0
    pw = (W - gap * (len(v["panels"]) + 1)) / len(v["panels"])
    ph = H - title_h - gap * 2
    boxes: dict[str, list[float]] = {}
    panel_box: dict[str, list[float]] = {}
    for k, p in enumerate(v["panels"]):
        px = gap + k * (pw + gap)
        py = title_h + gap
        # 统一存 [x0,y0,x1,y1]（早期存成 [x,y,w,h] 让第二个面板宽=0）
        panel_box[str(p["id"])] = [px, py, px + pw, py + ph]
    placed: list[list[float]] = []
    for e in v["elements"]:
        eid = str(e["id"])
        kind = e["kind"]
        w, h = _SIZE.get(kind, (100, 24))
        pb = panel_box[str(e["panel"])]
        if kind == "membrane":
            w = (pb[2] - pb[0]) * 0.84
        if kind == "organelle":
            w, h = 210.0, 150.0
        u, vv = float(e["at"][0]), float(e["at"][1])
        cx = pb[0] + 26 + u * ((pb[2] - pb[0]) - 52)
        cy = pb[1] + 44 + vv * ((pb[3] - pb[1]) - 88)
        b = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
        # 同层图元互相压住时沿区室垂直方向挪开（不让 AI 手调坐标）；
        # 膜 / 细胞器是背景层，不参与挤位，否则核内蛋白会被推到核外。
        if kind not in ("membrane", "organelle"):
            for _ in range(8):
                hit = next((o for o in placed
                            if min(b[2], o[2]) - max(b[0], o[0]) > 8
                            and min(b[3], o[3]) - max(b[1], o[1]) > 6), None)
                if hit is None:
                    break
                shift = hit[3] - b[1] + 8
                b = [b[0], b[1] + shift, b[2], b[3] + shift]
        b = [max(pb[0] + 4, b[0]), max(pb[1] + 4, b[1]),
             min(pb[2] - 4, b[2]), min(pb[3] - 4, b[3])]
        boxes[eid] = b
        if kind not in ("membrane", "organelle"):
            placed.append(b)
    return {"W": W, "H": H, "title_h": title_h, "gap": gap,
            "panel_box": panel_box, "boxes": boxes, "validated": v}


# ══════════════════════════════════════════════════════════════════
# 图元绘制
# ══════════════════════════════════════════════════════════════════

def _draw_panel(c: _C, p: dict, b: list[float]) -> None:
    tone = PALETTE.get(str(p.get("tone") or "sand"), PALETTE["sand"])
    c.rect(b[0], b[1], b[2] - b[0], b[3] - b[1], fill=tone,
           stroke=_AXIS, sw=1.0, rx=12, cls="panel")
    c.text(f"pn-{p['id']}", b[0] + 22, b[1] + 28, str(p["id"]), size=17,
           anchor="start", box=[b[0] + 18, b[1] + 10, b[0] + 44, b[1] + 32])
    c.text(f"pt-{p['id']}", b[0] + 48, b[1] + 27,
           str(p.get("title") or ""), size=14, anchor="start",
           box=[b[0] + 46, b[1] + 10, b[0] + 260, b[1] + 32])


def _draw_element(c: _C, e: dict, b: list[float], tid: str) -> None:
    kind, x0, y0, x1, y1 = e["kind"], *b
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tone = PALETTE.get(str(e.get("tone") or "blue"), PALETTE["blue"])
    label = str(e.get("label") or "")
    if kind == "membrane":
        # 磷脂双层：上下两排圆头 + 两条尾线，中间留双层间隙
        n = max(10, int((x1 - x0) / 15))
        for row, yy in enumerate((y0 + 5, y1 - 5)):
            for i in range(n):
                xx = x0 + (i + 0.5) * (x1 - x0) / n
                c.circle(xx, yy, 3.0, fill=tone, stroke=_AXIS, sw=0.5)
                d = 6.5 if row == 0 else -6.5
                c.line(xx - 1.3, yy + d * 0.35, xx - 1.3, yy + d, _AXIS, 0.8)
                c.line(xx + 1.3, yy + d * 0.35, xx + 1.3, yy + d, _AXIS, 0.8)
        c.rect(x0, y0 + 9, x1 - x0, y1 - y0 - 18,
               fill=_PAPER, stroke="", cls="mark", mark=True)
        if label:
            c.text(tid, cx, y0 - 5, label, size=9,
                   box=[x0, y0 - 18, x1, y0 - 2])
    elif kind == "organelle":
        c.ellipse(cx, cy, (x1 - x0) / 2, (y1 - y0) / 2,
                  fill=_PAPER, stroke=_AXIS, sw=1.4, dash="7,5")
        c.ellipse(cx, cy, (x1 - x0) / 2 - 7, (y1 - y0) / 2 - 7,
                  fill="none", stroke=_AXIS, sw=0.8)
        if label:
            c.text(tid, cx, y0 + 18, label, size=10,
                   box=[x0 + 10, y0 + 5, x1 - 10, y0 + 22])
    elif kind == "protein":
        h = y1 - y0
        c.rect(x0, y0, x1 - x0, h, fill=tone, stroke=_INK, sw=0.9,
               rx=min(h / 2, 14), cls="mark", mark=True)
        if str(e.get("variant")) == "receptor":
            c.line(cx - 10, y1, cx - 10, y1 + 16, _INK, 1.2)
            c.line(cx + 10, y1, cx + 10, y1 + 16, _INK, 1.2)
        if label:
            c.text(tid, cx, cy + 3.5, label, size=10,
                   box=[x0 - 12, y0, x1 + 12, y1])
    elif kind == "mrna":
        pts = [(x0 + (x1 - x0) * i / 36,
                cy + 4.2 * math.sin(i * math.pi / 6)) for i in range(37)]
        c.poly(pts, stroke=_INK, sw=1.7, closed=False)
        c.rect(x0, y0, x1 - x0, y1 - y0, fill="none", stroke="", cls="mark",
               mark=True)
        if label:
            c.text(tid, cx, y0 - 4, label, size=9,
                   box=[x0, y0 - 16, x1, y0])
    elif kind == "mod":
        col = _M6A if "m6A" in label else _AMBER
        c.circle(cx, cy, 6.0, fill=col, stroke=_INK, sw=0.7)
        if label:
            c.text(tid, cx + 12, cy + 3, label, size=9, anchor="start",
                   box=[cx + 10, cy - 10, cx + 70, cy + 8])
    elif kind == "molecule":
        for i in range(7):
            a = i * 2 * math.pi / 7
            c.circle(cx + 12 * math.cos(a), cy + 8 * math.sin(a), 3.2,
                     fill=tone, stroke=_AXIS, sw=0.5)
        if label:
            c.text(tid, cx, y1 + 12, label, size=9,
                   box=[x0 - 20, y1, x1 + 20, y1 + 20])
    elif kind == "label":
        c.text(tid, cx, cy + 3, label, size=10,
               box=[x0, y0, x1, y1])


def _edge_point(b: list[float], toward: list[float]) -> tuple[float, float]:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    tx, ty = (toward[0] + toward[2]) / 2, (toward[1] + toward[3]) / 2
    dx, dy = tx - cx, ty - cy
    if abs(dx) * (b[3] - b[1]) > abs(dy) * (b[2] - b[0]):
        return (b[2] if dx > 0 else b[0], cy)
    return (cx, b[3] if dy > 0 else b[1])


def _arrow(c: _C, tip, prev, color, sw=1.4) -> None:
    dx, dy = tip[0] - prev[0], tip[1] - prev[1]
    n = math.hypot(dx, dy) or 1.0
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    c.poly([tip, (tip[0] - 9 * ux + 4 * px, tip[1] - 9 * uy + 4 * py),
            (tip[0] - 9 * ux - 4 * px, tip[1] - 9 * uy - 4 * py)],
           fill=color, closed=True)


def _draw_relation(c: _C, r: dict, fb: list[float], tb: list[float],
                   tid: str) -> None:
    kind = str(r.get("kind") or "activate")
    p1 = _edge_point(fb, tb)
    p2 = _edge_point(tb, fb)
    color = _INK if kind != "inhibit" else "#B4524D"
    dash = "5,4" if kind == "indirect" else ""
    if kind == "transport":
        bend = float(r.get("bend") or -70)
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2 + bend
        c.path(f"M{p1[0]:.1f},{p1[1]:.1f} Q{mx:.1f},{my:.1f} "
               f"{p2[0]:.1f},{p2[1]:.1f}", color, 1.4, dash)
        _arrow(c, p2, (mx, my), color)
    else:
        c.line(p1[0], p1[1], p2[0], p2[1], color, 1.4, dash)
        if kind == "inhibit":
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            n = math.hypot(dx, dy) or 1.0
            px, py = -dy / n, dx / n
            c.line(p2[0] + 7 * px, p2[1] + 7 * py,
                   p2[0] - 7 * px, p2[1] - 7 * py, color, 2.2)
        else:
            _arrow(c, p2, p1, color)
    lab = str(r.get("label") or "")
    if lab:
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2 - 6
        c.text(tid, mx, my, lab, size=9,
               box=[mx - 48, my - 11, mx + 48, my + 3], cls="edge-label")


# ══════════════════════════════════════════════════════════════════
# 出图 + 写后读回
# ══════════════════════════════════════════════════════════════════

def render_pathway(spec: dict, out: str | Path) -> dict:
    """把构图意图画成科研通路插图，写完读回自检。"""
    L = layout(spec)
    v = L["validated"]
    c = _C(L["W"], L["H"])
    c.rect(0, 0, L["W"], L["H"], fill=_PAPER, cls="paper")
    c.text("title", L["W"] / 2, 32, v["title"], size=18,
           box=[L["W"] / 2 - 260, 12, L["W"] / 2 + 260, 40])
    for p in v["panels"]:
        _draw_panel(c, p, L["panel_box"][str(p["id"])])
    # 层次：区室/膜 → mRNA/小分子 → 蛋白 → 修饰点 → 文字
    rank = {"organelle": 0, "membrane": 0, "mrna": 1, "molecule": 1,
            "protein": 2, "mod": 3, "label": 4}
    elems = sorted(v["elements"], key=lambda e: rank.get(e["kind"], 2))
    by_id = {str(e["id"]): e for e in v["elements"]}
    for e in elems:
        _draw_element(c, e, L["boxes"][str(e["id"])], f"t-{e['id']}")
    for k, r in enumerate(v["relations"]):
        _draw_relation(c, r, L["boxes"][str(r["from"])],
                       L["boxes"][str(r["to"])], f"r-{k}")

    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    svg = c.to_svg(v["title"])
    p.write_text(svg, encoding="utf-8", newline="\n")
    mp = p.with_suffix(".layout.json")
    manifest = {
        "schema_version": 1, "backend": "native-pathway",
        "kind": "pathway", "n_marks": len(c.marks),
        "figure": {"width_mm": v["width_mm"],
                   "height_mm": L["H"] * v["width_mm"] / L["W"],
                   "width_px": L["W"], "height_px": L["H"],
                   "mm_per_px": v["width_mm"] / L["W"],
                   "intrinsic_width_mm": L["W"] * 25.4 / 96.0,
                   "clamped": False},
        "texts": c.texts, "marks": c.marks,
        "elements": [{"id": str(e["id"]), "kind": e["kind"],
                      "box": L["boxes"][str(e["id"])]} for e in v["elements"]],
        "relations": [{"from": str(r["from"]), "to": str(r["to"]),
                       "kind": r.get("kind", "activate")}
                      for r in v["relations"]],
        "palette": PALETTE,
    }
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                  encoding="utf-8", newline="\n")
    back = p.read_text(encoding="utf-8")
    checks = {
        "readback_equal": back == svg,
        "cjk_preserved": "科研通路原生 SVG 后端" in back,
        "font_stack": FONT_STACK in back,
        "every_element_rendered": all(f'id="t-{e["id"]}"' in back
                                      for e in v["elements"]
                                      if e.get("label")),
        "every_relation_drawn": all(f'id="r-{k}"' in back
                                    for k, r in enumerate(v["relations"])
                                    if r.get("label")),
        "no_nan": "nan" not in back.lower(),
        "panels_drawn": back.count('class="panel"') == len(v["panels"]),
    }
    ok = all(bool(x) for x in checks.values())
    return {
        "ok": ok, "svg": str(p), "layout": str(mp), "bytes": p.stat().st_size,
        "backend": "native-pathway", "scope": "preview", "kind": "pathway",
        "width_px": L["W"], "height_px": L["H"], "width_mm": v["width_mm"],
        "n_elements": len(v["elements"]), "n_relations": len(v["relations"]),
        "n_texts": len(c.texts), "checks": checks,
        "spec_guide": SPEC_GUIDE,
        "note": "" if ok else "结构自检有未通过项（见 checks）—— 文件留在盘上供核查。",
    }


# ══════════════════════════════════════════════════════════════════
# 给 AI 的操作面：单图元增删改 + 显式回执（不是一次性打印机）
# ══════════════════════════════════════════════════════════════════

def demo_spec() -> dict:
    """示例规格单源：给判据、演示图、新用户看「什么是对的品类」共用一份。

    内容是自造的示意通路，不抄任何论文；图元语汇按参考图建。
    """
    return {
        "title": "脂质代谢与 m6A 修饰的交叉调控（示意）",
        "width_mm": 260.0,
        "panels": [
            {"id": "A", "title": "A · 脂质代谢 / SREBP 加工", "tone": "sand"},
            {"id": "B", "title": "B · m6A 与转录后调控", "tone": "blush"},
        ],
        "elements": [
            {"id": "mem", "kind": "membrane", "panel": "A", "at": [0.50, 0.22],
             "tone": "cyan", "label": "磷脂双层膜"},
            {"id": "rtk", "kind": "protein", "panel": "A", "at": [0.18, 0.22],
             "tone": "blue", "label": "RTK 受体", "variant": "receptor"},
            {"id": "srebp", "kind": "protein", "panel": "A", "at": [0.36, 0.44],
             "tone": "cyan", "label": "SREBP"},
            {"id": "scap", "kind": "protein", "panel": "A", "at": [0.60, 0.44],
             "tone": "violet", "label": "SCAP"},
            {"id": "s1p", "kind": "protein", "panel": "A", "at": [0.84, 0.30],
             "tone": "clay", "label": "S1P"},
            {"id": "lncarsr", "kind": "mrna", "panel": "A", "at": [0.27, 0.64],
             "label": "LNCARSR"},
            {"id": "linc00858", "kind": "mrna", "panel": "A", "at": [0.72, 0.64],
             "label": "LINC00858"},
            {"id": "phos", "kind": "mod", "panel": "A", "at": [0.48, 0.32],
             "label": "P"},
            {"id": "chol", "kind": "molecule", "panel": "A", "at": [0.48, 0.84],
             "tone": "sand", "label": "胆固醇"},
            {"id": "mettl3", "kind": "protein", "panel": "A", "at": [0.84, 0.82],
             "tone": "violet", "label": "METTL3"},
            {"id": "nuc", "kind": "organelle", "panel": "B", "at": [0.78, 0.30],
             "tone": "blush", "label": "细胞核"},
            {"id": "igf", "kind": "protein", "panel": "B", "at": [0.22, 0.24],
             "tone": "blue", "label": "IGF2BPs"},
            {"id": "slc", "kind": "mrna", "panel": "B", "at": [0.43, 0.58],
             "label": "SLC7A11 mRNA"},
            {"id": "m6a1", "kind": "mod", "panel": "B", "at": [0.30, 0.49],
             "label": "m6A"},
            {"id": "m6a2", "kind": "mod", "panel": "B", "at": [0.58, 0.49],
             "label": "m6A"},
            {"id": "ythdc", "kind": "protein", "panel": "B", "at": [0.28, 0.80],
             "tone": "cyan", "label": "YTHDC"},
            {"id": "parp1", "kind": "protein", "panel": "B", "at": [0.80, 0.30],
             "tone": "violet", "label": "PARP1"},
            {"id": "wtap", "kind": "protein", "panel": "B", "at": [0.78, 0.80],
             "tone": "clay", "label": "WTAP / METTL14"},
            {"id": "metab", "kind": "molecule", "panel": "B", "at": [0.52, 0.82],
             "tone": "cyan", "label": "代谢物"},
        ],
        "relations": [
            {"from": "rtk", "to": "srebp", "kind": "activate"},
            {"from": "srebp", "to": "scap", "kind": "activate"},
            {"from": "scap", "to": "s1p", "kind": "activate"},
            {"from": "s1p", "to": "lncarsr", "kind": "inhibit"},
            {"from": "lncarsr", "to": "mettl3", "kind": "activate"},
            {"from": "linc00858", "to": "mettl3", "kind": "activate"},
            {"from": "igf", "to": "slc", "kind": "activate", "label": "稳定"},
            {"from": "m6a1", "to": "ythdc", "kind": "indirect"},
            {"from": "wtap", "to": "m6a1", "kind": "activate"},
            {"from": "ythdc", "to": "parp1", "kind": "transport", "bend": -95},
            {"from": "parp1", "to": "slc", "kind": "inhibit"},
        ],
    }


class Scene:
    """图元级场景。AI 一次只动一个图元，拿 affected/diff/revision 接着改。"""

    def __init__(self, title: str = "机制通路示意"):
        self.state = {"schema": 1, "revision": 0, "title": title,
                      "width_mm": 260.0, "panels": [], "elements": [],
                      "relations": []}

    def _spec(self) -> dict:
        return dict(self.state)

    @staticmethod
    def _diff(before, after, eid, op):
        return [{"id": eid, "change": op,
                 "before": before, "after": after}]

    def apply(self, action: dict) -> dict:
        a = dict(action or {})
        op = str(a.pop("op") or "")
        if op not in OPS:
            return {"ok": False, "op": op, "error": f"未知动作（支持 {'、'.join(OPS)}）"}
        st = self.state
        snapshot = copy.deepcopy(st)   # 写动作前留快照，失败整笔回滚
        if op == "query":
            return {"ok": True, "op": op, "revision": st["revision"],
                    "state": json.loads(json.dumps(st)),
                    "summary": self.describe(), "next": "改一个图元发 set_element；出图发 render。"}
        if op == "describe":
            return {"ok": True, "op": op, "revision": st["revision"],
                    "summary": self.describe(), "glyphs": list(GLYPHS),
                    "palette": PALETTE, "guide": SPEC_GUIDE}
        if op == "render":
            out = a.get("out") or "机制通路.svg"
            rep = render_pathway(self._spec(), out)
            return {"ok": rep["ok"], "op": op, "revision": st["revision"],
                    "svg": rep["svg"], "checks": rep["checks"],
                    "n_elements": rep["n_elements"],
                    "n_relations": rep["n_relations"],
                    "next": "出图完成；要改一个图元发 set_element，别重写整份规格。"}
        diff_before = None
        diff_after = a
        diff_op = op
        try:
            if op == "new":
                before = dict(st)
                st.update({"revision": st["revision"] + 1,
                           "title": a.get("title", st["title"]),
                           "width_mm": a.get("width_mm", st["width_mm"]),
                           "panels": list(a.get("panels") or []),
                           "elements": [], "relations": []})
                return {"ok": True, "op": op, "revision": st["revision"],
                        "affected": ["scene"], "diff": self._diff(before, st, "scene", "new"),
                        "summary": self.describe()}
            if op == "add_element":
                e = dict(a); e.pop("out", None)
                eid = str(e.get("id") or "")
                if any(str(x.get("id")) == eid for x in st["elements"]):
                    raise PathwayError(f"图元 id 重复：`{eid}`")
                st["elements"].append(e)
            elif op == "add_relation":
                r = dict(a); r.pop("out", None)
                st["relations"].append(r)
                eid = f"rel-{len(st['relations'])}"
            elif op == "set_element":
                eid = str(a.get("id") or "")
                hit = next((x for x in st["elements"]
                            if str(x.get("id")) == eid), None)
                if hit is None:
                    raise PathwayError(f"找不到图元 `{eid}`")
                before = dict(hit)
                for k, vv in a.items():
                    if k not in ("id", "out"):
                        hit[k] = vv
                diff_before = before
                diff_after = dict(hit)
                diff_op = "set"
            elif op == "remove":
                eid = str(a.get("id") or "")
                n = len(st["elements"])
                st["elements"] = [x for x in st["elements"]
                                  if str(x.get("id")) != eid]
                if len(st["elements"]) == n:
                    raise PathwayError(f"找不到图元 `{eid}`")
                st["relations"] = [r for r in st["relations"]
                                   if str(r.get("from")) != eid
                                   and str(r.get("to")) != eid]
            else:
                raise PathwayError(f"动作 `{op}` 没有落点")
            # 事务性校验：不合法就整笔退回，不留半截状态（含 set_element）
            validate(self._spec())
        except PathwayError as ex:
            self.state = snapshot      # 整笔回滚：半截状态一个字节都不留
            return {"ok": False, "op": op, "error": str(ex),
                    "revision": snapshot["revision"],
                    "next": "按 SPEC_GUIDE 改动作再发一次。"}
        st["revision"] += 1
        return {"ok": True, "op": op, "revision": st["revision"],
                "affected": [eid],
                "diff": self._diff(diff_before, diff_after, eid, diff_op),
                "summary": self.describe(),
                "next": "继续加图元 / 发 query 看现状 / 发 render 出图。"}

    def describe(self) -> str:
        st = self.state
        kinds: dict[str, int] = {}
        for e in st["elements"]:
            kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
        mix = "、".join(f"{k}×{n}" for k, n in sorted(kinds.items()))
        return (f"{st['title']} · rev {st['revision']} · "
                f"{len(st['panels'])} 区室 / {len(st['elements'])} 图元 / "
                f"{len(st['relations'])} 关系"
                + (f"（{mix}）" if mix else ""))
