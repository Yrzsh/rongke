# -*- coding: utf-8 -*-
"""SVG 质量门 —— 把「结构自检」升成「可量的质量」。

## 为什么需要它

`svgplot.inspect_svg()` 的十二项全是**结构**检查：XML 能不能解析、有没有
viewBox、字体栈里有没有中文名。它量不出质量。两个具体的洞：

  · `texts_in_viewbox` 只量文本的**锚点** (x,y)。标签从框内起笔、一路画出
    框外，照样通过。
  · `cjk_font_stack_ok` 只查字体**声明字符串**，豆腐块没被排除。

而这类假成功真实发生过：柱按 (i+0.5)/n 画、刻度按 i/(n-1) 放，两套坐标系
让最后一根柱的数值标签飘到图外，**所有程序都 ok:true**，靠肉眼才发现
（见 `svgplot._xscale` 的注释与 `svgplot_test.py` 的 1.align）。本模块把那条
一次性测试断言提成常驻门。

## 两层尺子

第一层（本模块）：纯标准库，无条件跑。SVG 是文本树，font-size / fill /
坐标都是属性，所以字号下限、对比度、数据保真在 SVG 上反而比在 PDF 上更容易
判 —— PDF 那几道要靠 PyMuPDF 才能把同样的事实挖出来。文本**实际占位**是
估算（口径单源借 `deck.text_advance_em`），落在边界带时判 `NOT AUDITABLE`，
交第二层。

第二层（`svgmeasure`）：浏览器 getBBox() / getComputedTextLength() 量真值、
document.fonts.check() 查字形覆盖。缺件时判 `NOT AUDITABLE`，**绝不判 PASS**。

## 判据纪律

· verdict 词汇借 `figure.GateResult`，不压成布尔：`NOT AUDITABLE`（审不了）
  与 `PASS`（审过了）是两件事，合并就是造假证据。
· 本模块**不改 `ok` / `scope` / `checks`**：那三个是预览路线的声明，
  `gate.py:379` 与 `svgplot_test.py:66` 都钉死了它们的语义。门的结果走
  `gates` 字段，靠工具回包让模型看见并修，不参与完工判定。
· 被审对象是**读回来的产物**，不是内存里的意图。几何清单只补两件产物里
  没有的事：声明的物理宽度（px→pt 换算）与文本的所属容器。
"""
from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .deck import text_advance_em
from .figure import Finding, GateResult, _TEXT_FIT_FAIL_PCT, _TEXT_FIT_WARN_PCT
from .svgplot import inspect_svg

#: 字形下限。与 `figure.py:_gate_pdf_text` 的 `--min-pt 5` 同口径（那边是
#: 字面量、没有常量可借）。单位是**物理 pt**，不是 SVG 的 px。
MIN_FONT_PT = 5.0
_PT_PER_MM = 72.0 / 25.4

#: WCAG 2.x 对比度档位。小字（<18pt）AA 要 4.5；低于 3.0 连大字都不合格。
_CONTRAST_FAIL = 3.0
_CONTRAST_WARN = 4.5

#: 估算误差缓冲（px）。0.55/1.0 的 em 口径对真实字体的偏差在这个量级，
#: 所以只有**双方都超过它**的碰撞才算估算也能定案的硬伤。
_EST_MARGIN_PX = 2.0

#: 同一串文字在这么近的距离内重绘 = 描边光晕（先画白描边再画填色），不是碰撞。
#: 不给这条豁免，凡是给标签加了 halo 的图都会被碰撞门判红 —— 纯误报。
_HALO_PX = 2.5

#: 文本 em 盒的 ascent / descent 系数。取偏保守的上界，宁可多报边界带
#: 交第二层实测，也不要把真溢出估成刚好装得下。
_ASCENT_EM = 0.88
_DESCENT_EM = 0.22

_NS = "{http://www.w3.org/2000/svg}"


def _tag(e: ET.Element) -> str:
    return e.tag.replace(_NS, "") if isinstance(e.tag, str) else ""


def _f(e: ET.Element, key: str, default: float = 0.0) -> float:
    try:
        return float(e.get(key, default))
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════
# 读回产物
# ══════════════════════════════════════════════════════════════════

def _rotation(e: ET.Element) -> tuple[float, float, float]:
    """从 transform 里取 (角度, 中心x, 中心y)。只认 rotate，别的变换如实忽略。"""
    tr = e.get("transform") or ""
    if "rotate(" not in tr:
        return 0.0, 0.0, 0.0
    inner = tr.split("rotate(", 1)[1].split(")", 1)[0]
    nums = [float(v) for v in inner.replace(",", " ").split() if v]
    if not nums:
        return 0.0, 0.0, 0.0
    ang = nums[0]
    cx = nums[1] if len(nums) > 1 else 0.0
    cy = nums[2] if len(nums) > 2 else 0.0
    return ang, cx, cy


def read_svg(path: str | Path, layout: str | Path | dict | None = None) -> dict:
    """读回 SVG + 几何清单，解析成门能消费的形状。

    清单缺失**不是错误**：它只让依赖物理宽度的门判 `NOT AUDITABLE`，
    其余门照跑。这样手工写的 / 插件产的 SVG 也能审，不必先有清单。
    """
    p = Path(path)
    out: dict[str, Any] = {"svg": str(p), "exists": p.is_file(),
                           "manifest": None, "texts": [], "marks": [],
                           "viewbox": (0.0, 0.0, 0.0, 0.0), "paper": "#FFFFFF",
                           "error": ""}
    if not p.is_file():
        out["error"] = f"SVG 不存在：{p}"
        return out
    try:
        root = ET.fromstring(p.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"SVG 解析不了：{type(e).__name__}: {e}"
        return out
    if _tag(root) != "svg":
        out["error"] = "根元素不是 <svg>"
        return out

    vb = (root.get("viewBox") or "").split()
    if len(vb) == 4:
        out["viewbox"] = tuple(float(v) for v in vb)
    else:
        out["viewbox"] = (0.0, 0.0, _f(root, "width"), _f(root, "height"))

    # 几何清单：显式给了就用，否则找同名的 .layout.json
    man: Any = layout
    if man is None:
        cand = p.with_suffix(".layout.json")
        man = cand if cand.is_file() else None
    if isinstance(man, (str, Path)):
        try:
            man = json.loads(Path(man).read_text(encoding="utf-8"))
        except Exception as e:                               # noqa: BLE001
            out["error"] = f"几何清单读不进来：{type(e).__name__}: {e}"
            man = None
    out["manifest"] = man if isinstance(man, dict) else None
    specs = {str(t.get("id")): t for t in (out["manifest"] or {}).get("texts", [])
             if isinstance(t, dict)}

    for e in root.iter():
        t = _tag(e)
        if t == "text":
            ang, cx, cy = _rotation(e)
            tid = e.get("id") or ""
            spec = specs.get(tid) or {}
            out["texts"].append({
                "id": tid, "x": _f(e, "x"), "y": _f(e, "y"),
                "size": _f(e, "font-size", 10.0),
                "anchor": e.get("text-anchor") or "start",
                "fill": e.get("fill") or "#000000",
                "rotate": ang, "rcx": cx, "rcy": cy,
                "text": "".join(e.itertext()),
                "box": spec.get("box"),
            })
        elif t == "rect" and e.get("class") == "paper":
            out["paper"] = e.get("fill") or "#FFFFFF"
        elif e.get("class") == "mark":
            fill = e.get("fill") or "none"
            op = _f(e, "fill-opacity", 1.0)
            if t == "rect":
                x, y = _f(e, "x"), _f(e, "y")
                w, h = _f(e, "width"), _f(e, "height")
                box = (x, y, x + w, y + h)
            elif t == "circle":
                cx, cy, r = _f(e, "cx"), _f(e, "cy"), _f(e, "r")
                box = (cx - r, cy - r, cx + r, cy + r)
            elif t in ("polyline", "polygon"):
                pts = [tuple(map(float, pr.split(",")))
                       for pr in (e.get("points") or "").split() if "," in pr]
                if not pts:
                    continue
                box = (min(q[0] for q in pts), min(q[1] for q in pts),
                       max(q[0] for q in pts), max(q[1] for q in pts))
            else:
                continue
            out["marks"].append({"tag": t, "box": box, "fill": fill,
                                 "opacity": op,
                                 "tip": "".join(e.itertext())})
    return out


# ══════════════════════════════════════════════════════════════════
# 纯算术原语
# ══════════════════════════════════════════════════════════════════

def _rgb(color: str) -> tuple[float, float, float] | None:
    s = (color or "").strip()
    if s.startswith("#"):
        s = s[1:]
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) != 6:
            return None
        try:
            return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return None
    return None


def _luminance(rgb: tuple[float, float, float]) -> float:
    def chan(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float | None:
    """WCAG 相对亮度比。任一颜色解析不了就返回 None（判不了不出假数）。"""
    a, b = _rgb(fg), _rgb(bg)
    if a is None or b is None:
        return None
    l1, l2 = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _composite(fg: str, bg: str, opacity: float) -> str:
    """半透明填色压在纸色上的**有效色**。热图格子就是这种，直接拿原色算会虚高。"""
    a, b = _rgb(fg), _rgb(bg)
    if a is None or b is None:
        return fg
    op = max(0.0, min(1.0, opacity))
    return "#%02X%02X%02X" % tuple(
        round(op * a[i] + (1.0 - op) * b[i]) for i in range(3))


def est_text_box(t: dict) -> tuple[float, float, float, float]:
    """估算文本 bbox（canvas px）。**这是估算，不是实测**。

    宽度口径单源借 `deck.text_advance_em`；anchor 决定左右展开方向；
    rotate 绕文字自己的中心转（与 `_Canvas.text` 发出的 transform 一致）。
    """
    size = float(t.get("size") or 10.0)
    w = text_advance_em(str(t.get("text") or "")) * size
    x, y = float(t.get("x") or 0.0), float(t.get("y") or 0.0)
    anchor = str(t.get("anchor") or "start")
    x0 = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
    y0, y1 = y - _ASCENT_EM * size, y + _DESCENT_EM * size
    box = (x0, y0, x0 + w, y1)
    ang = float(t.get("rotate") or 0.0)
    if abs(ang) < 1e-9:
        return box
    cx, cy = float(t.get("rcx") or x), float(t.get("rcy") or y)
    rad = math.radians(ang)
    cos, sin = math.cos(rad), math.sin(rad)
    pts = []
    for px, py in ((box[0], box[1]), (box[2], box[1]),
                   (box[0], box[3]), (box[2], box[3])):
        dx, dy = px - cx, py - cy
        pts.append((cx + dx * cos - dy * sin, cy + dx * sin + dy * cos))
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


def _overlap(a: tuple, b: tuple) -> tuple[float, float]:
    """两个框的重叠深度 (横向, 纵向)。不相交返回 (0, 0) 或负数。"""
    return (min(a[2], b[2]) - max(a[0], b[0]),
            min(a[3], b[3]) - max(a[1], b[1]))


def _bg_under(t: dict, doc: dict) -> str:
    """文字锚点落在哪个标记里，就取那个标记的有效填色；否则取纸色。"""
    x, y = float(t.get("x") or 0.0), float(t.get("y") or 0.0)
    paper = doc.get("paper") or "#FFFFFF"
    best = None
    for m in doc.get("marks") or []:
        x0, y0, x1, y1 = m["box"]
        if x0 <= x <= x1 and y0 <= y <= y1 and m["fill"] != "none":
            area = (x1 - x0) * (y1 - y0)
            if best is None or area < best[0]:
                best = (area, m)
    if best is None:
        return paper
    m = best[1]
    return _composite(m["fill"], paper, m["opacity"])


# ══════════════════════════════════════════════════════════════════
# 第一层：六道门
# ══════════════════════════════════════════════════════════════════

def _gate_font_size(doc: dict) -> GateResult:
    texts = doc["texts"]
    if not texts:
        return GateResult("font_size_floor", "NOT AUDITABLE", "产物里没有文本节点。")
    fig = (doc.get("manifest") or {}).get("figure") or {}
    mm_per_px = fig.get("mm_per_px")
    if not mm_per_px:
        return GateResult(
            "font_size_floor", "NOT AUDITABLE",
            "没有几何清单里的声明物理宽度，px 换算不成 pt。"
            "字号下限是物理量（≥5 pt），拿 px 判会放过被钳位缩小的图。")
    pts = [(t, float(t["size"]) * float(mm_per_px) * _PT_PER_MM) for t in texts]
    below = [(t, v) for t, v in pts if v < MIN_FONT_PT]
    mn = min(v for _, v in pts)
    findings = [Finding("PASS", "SVG-FONT-FLOOR",
                        f"最小 {mn:.2f} pt ≥ 下限 {MIN_FONT_PT:.1f} pt"
                        f"（{len(texts)} 段文本，按声明 {fig.get('width_mm')} mm 宽换算）")]
    if fig.get("clamped"):
        findings.append(Finding(
            "WARN", "SVG-CANVAS-CLAMP",
            f"画布宽被钳到 {fig.get('width_px')} px，而产物声明 {fig.get('width_mm')} mm"
            f"（自然宽其实是 {fig.get('intrinsic_width_mm', 0):.1f} mm）。"
            "按声明宽度摆进版面时所有字都会再缩一档 —— 这就是字号不足的根因。"))
    if below:
        for t, v in below[:12]:
            findings.append(Finding(
                "FAIL", "SVG-FONT-FLOOR",
                f"文本 {t['id'] or '?'}「{str(t['text'])[:24]}」"
                f" {t['size']:g}px → {v:.2f} pt < {MIN_FONT_PT:.1f} pt"))
        return GateResult("font_size_floor", "FIX BEFORE DELIVERY",
                          f"{len(below)} 段文本低于 {MIN_FONT_PT:.0f} pt（最小 {mn:.2f} pt）",
                          findings)
    return GateResult("font_size_floor", "PASS",
                      f"最小 {mn:.2f} pt，{len(texts)} 段文本全部 ≥ {MIN_FONT_PT:.0f} pt",
                      findings)


def _gate_contrast(doc: dict) -> GateResult:
    texts = doc["texts"]
    if not texts:
        return GateResult("contrast", "NOT AUDITABLE", "产物里没有文本节点。")
    findings: list[Finding] = []
    n_ok = n_unknown = 0
    worst = None
    for t in texts:
        bg = _bg_under(t, doc)
        r = contrast_ratio(str(t["fill"]), bg)
        if r is None:
            n_unknown += 1
            continue
        if worst is None or r < worst[0]:
            worst = (r, t, bg)
        if r < _CONTRAST_WARN:
            findings.append(Finding(
                "FAIL" if r < _CONTRAST_FAIL else "WARN", "SVG-CONTRAST",
                f"文本 {t['id'] or '?'}「{str(t['text'])[:24]}」"
                f" {t['fill']} 压 {bg} 对比度 {r:.2f}:1"))
        else:
            n_ok += 1
    if n_unknown and not n_ok and not findings:
        return GateResult("contrast", "NOT AUDITABLE",
                          f"{n_unknown} 段文本的颜色解析不了（非 #hex），判不了不出假数。")
    fails = [f for f in findings if f.level == "FAIL"]
    if fails:
        return GateResult("contrast", "FIX BEFORE DELIVERY",
                          f"{len(fails)} 段文本对比度 < {_CONTRAST_FAIL}:1"
                          f"（最低 {worst[0]:.2f}:1）", findings)
    if findings:
        return GateResult("contrast", "REVIEW REQUIRED",
                          f"{len(findings)} 段文本对比度落在 "
                          f"{_CONTRAST_FAIL}~{_CONTRAST_WARN}:1，不阻断", findings)
    return GateResult("contrast", "PASS",
                      f"{n_ok} 段文本对比度 ≥ {_CONTRAST_WARN}:1"
                      + (f"，最低 {worst[0]:.2f}:1" if worst else "")
                      + (f"；{n_unknown} 段颜色解析不了" if n_unknown else ""),
                      [Finding("PASS", "SVG-CONTRAST",
                               f"最低 {worst[0]:.2f}:1" if worst else "无文本")]
                      if worst else [])


def _gate_text_extent(doc: dict, measured: dict | None) -> GateResult:
    """文字出界 / 装不进容器。第一层估算，边界带交第二层。"""
    texts = doc["texts"]
    if not texts:
        return GateResult("text_containment", "NOT AUDITABLE", "产物里没有文本节点。")
    real = {str(b.get("id")): b for b in (measured or {}).get("boxes", [])
            if isinstance(b, dict)} if measured and measured.get("ok") else {}
    vb = doc["viewbox"]
    findings: list[Finding] = []
    n_comp = n_real = 0
    for t in texts:
        rb = real.get(str(t["id"]))
        if rb:
            box = (float(rb["x"]), float(rb["y"]),
                   float(rb["x"]) + float(rb["width"]),
                   float(rb["y"]) + float(rb["height"]))
            n_real += 1
        else:
            box = est_text_box(t)
        # ① 声明容器（机制图节点标签）；② 没有容器就比 viewBox
        container = t.get("box")
        n_comp += 1
        if container and len(container) == 4:
            cx0, cy0, cx1, cy1 = (float(v) for v in container)
            cw = cx1 - cx0
            over = max(box[2] - cx1, cx0 - box[0], box[3] - cy1, cy0 - box[1])
            pct = (over / cw * 100.0) if cw > 0 else 0.0
            if over > 0:
                sev = ("FAIL" if pct > _TEXT_FIT_FAIL_PCT else "WARN")
                findings.append(Finding(
                    sev, "SVG-TEXT-FIT",
                    f"文本 {t['id'] or '?'}「{str(t['text'])[:24]}」"
                    f" 越出容器 {over:.1f}px（{pct:.1f}% 框宽）",
                    [f"bbox={[round(v, 1) for v in box]}",
                     f"box={[round(v, 1) for v in container]}",
                     "实测" if rb else "估算"]))
        elif (box[0] < vb[0] - 0.5 or box[1] < vb[1] - 0.5
              or box[2] > vb[2] + 0.5 or box[3] > vb[3] + 0.5):
            findings.append(Finding(
                "FAIL", "SVG-TEXT-OUT",
                f"文本 {t['id'] or '?'}「{str(t['text'])[:24]}」画出 viewBox："
                f" bbox={[round(v, 1) for v in box]} vb={list(vb)}",
                ["实测" if rb else "估算"]))
    attempted = bool(measured and measured.get("ok"))
    tier = ("实测" if attempted and n_real == len(texts)
            else "部分实测" if attempted and n_real else "估算")
    fails = [f for f in findings if f.level == "FAIL"]
    warns = [f for f in findings if f.level == "WARN"]
    if fails:
        return GateResult("text_containment", "FIX BEFORE DELIVERY",
                          f"{len(fails)} 处文字出界/装不进容器（{tier}）", findings)
    if warns:
        measured_all = attempted and n_real == len(texts)
        if measured_all:
            # 浏览器把**每一段**都量到了 ⇒ 实测数据足以定案，不能再退回
            # 「审不了」。越界是真的（落在 WARN 带也是真越界），必须给改法：
            # 否则 blocking 有值而 fix_before_delivery 为空，工具 next 里
            # 一句「怎么修」都没有 —— 反馈闭环当场断掉（2026-11-12 实测）。
            return GateResult(
                "text_containment", "FIX BEFORE DELIVERY",
                f"{len(warns)} 处文字**实测**越界（"
                f"{_TEXT_FIT_WARN_PCT:.0f}~{_TEXT_FIT_FAIL_PCT:.0f}% 框宽），"
                f"浏览器量到 {n_real}/{len(texts)} 段：这不是估算精度问题，"
                f"是真的装不下。按 findings 缩短标签 / 改节点形状 / 拆图后重画。",
                findings)
        # ★ 估算落在 2~15% 边界带时**不判 PASS**：估算精度不足以定案。
        #   这正是「审不了 ≠ 审过了」那条纪律的落地。
        return GateResult("text_containment", "NOT AUDITABLE",
                          f"{len(warns)} 处文字越界落在 "
                          f"{_TEXT_FIT_WARN_PCT:.0f}~{_TEXT_FIT_FAIL_PCT:.0f}% 边界带，"
                          f"当前是{tier}，估算精度不足以定案 —— 需浏览器 getBBox() 实测。",
                          findings)
    if attempted and n_real < len(texts):
        return GateResult(
            "text_containment", "NOT AUDITABLE",
            f"浏览器跑起来了，但 {len(texts) - n_real}/{len(texts)} 段文字量不到；"
            f"已实测的 {n_real} 段无越界。**未量到的部分不得当成通过。**",
            findings + [Finding("WARN", "SVG-TEXT-UNMEASURED", u)
                        for u in (measured or {}).get("texts_unchecked", [])][:12])
    return GateResult("text_containment", "PASS",
                      f"{len(texts)} 段文字，{n_comp} 段与容器/viewBox 比过，"
                      f"无越界（{tier}"
                      + (f"：浏览器量到 {n_real}/{len(texts)} 段" if n_real else "")
                      + "）",
                      [Finding("PASS", "SVG-TEXT-FIT",
                               f"{tier}口径下 {n_comp} 段全部装得下")])


def _gate_collision(doc: dict, measured: dict | None) -> GateResult:
    """文字压字。第一层只判 text-vs-text；线穿字 / 字压标记要第二层的真 bbox。"""
    texts = doc["texts"]
    if len(texts) < 2:
        return GateResult("collision", "NOT APPLICABLE",
                          f"只有 {len(texts)} 段文本，无字可压。")
    real = {str(b.get("id")): b for b in (measured or {}).get("boxes", [])
            if isinstance(b, dict)} if measured and measured.get("ok") else {}

    def box_of(t: dict) -> tuple:
        rb = real.get(str(t["id"]))
        if rb:
            return (float(rb["x"]), float(rb["y"]),
                    float(rb["x"]) + float(rb["width"]),
                    float(rb["y"]) + float(rb["height"]))
        return est_text_box(t)

    boxes = [(t, box_of(t)) for t in texts]
    findings: list[Finding] = []
    n_pair = 0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            ta, a = boxes[i]
            tb, b = boxes[j]
            # 描边光晕豁免：同串、近距重绘不是碰撞
            if (str(ta["text"]) == str(tb["text"])
                    and abs(float(ta["x"]) - float(tb["x"])) <= _HALO_PX
                    and abs(float(ta["y"]) - float(tb["y"])) <= _HALO_PX):
                continue
            ox, oy = _overlap(a, b)
            if ox <= 0 or oy <= 0:
                continue
            n_pair += 1
            hard = ox > _EST_MARGIN_PX and oy > _EST_MARGIN_PX
            findings.append(Finding(
                "FAIL" if hard else "WARN", "SVG-TEXT-COLLISION",
                f"「{str(ta['text'])[:18]}」({ta['id']}) 压 "
                f"「{str(tb['text'])[:18]}」({tb['id']})："
                f"重叠 {ox:.1f}×{oy:.1f}px",
                ["实测" if str(ta["id"]) in real else "估算"]))
    n_real = sum(1 for t in texts if str(t["id"]) in real)
    attempted = bool(measured and measured.get("ok"))
    tier = ("实测" if attempted and n_real == len(texts)
            else "部分实测" if attempted and n_real else "估算")
    fails = [f for f in findings if f.level == "FAIL"]
    if fails:
        return GateResult("collision", "FIX BEFORE DELIVERY",
                          f"{len(fails)} 处文字重叠超过估算误差缓冲 "
                          f"{_EST_MARGIN_PX:g}px（{tier}）", findings)
    if findings:
        return GateResult("collision", "NOT AUDITABLE",
                          f"{len(findings)} 处轻微重叠（≤{_EST_MARGIN_PX:g}px）落在"
                          f"估算误差量级，{tier}定不了案 —— 需浏览器实测。", findings)
    if attempted and n_real < len(texts):
        return GateResult(
            "collision", "NOT AUDITABLE",
            f"{len(texts) - n_real}/{len(texts)} 段文字浏览器量不到，"
            f"已实测的 {n_real} 段之间无重叠。未量到的部分不得当成通过。",
            [Finding("WARN", "SVG-TEXT-UNMEASURED", u)
             for u in (measured or {}).get("texts_unchecked", [])][:12])
    return GateResult("collision", "PASS",
                      f"{len(texts)} 段文本两两比过（{len(texts) * (len(texts) - 1) // 2} 对），"
                      f"无重叠（{tier}）")


def _gate_data_fidelity(doc: dict) -> GateResult:
    """产物有没有忠实画出数据。把 svgplot_test 的 1.align 提成常驻门。"""
    man = doc.get("manifest") or {}
    marks = doc["marks"]
    kind = str(man.get("kind") or "")
    findings: list[Finding] = []
    n_comp = 0

    # ① 标记元素数 vs 声明数（写盘途中丢元素会在这里露出来）
    if man.get("n_marks") is not None:
        n_comp += 1
        if len(marks) != int(man["n_marks"]):
            findings.append(Finding(
                "FAIL", "SVG-MARK-COUNT",
                f"产物里 {len(marks)} 个标记元素，几何清单声明 {man['n_marks']} 个"))

    # ② 逐点图：点数必须等于数据行数
    if kind in ("scatter", "line", "time_series") and man.get("n_points"):
        n_comp += 1
        n_circ = sum(1 for m in marks if m["tag"] == "circle")
        if n_circ != int(man["n_points"]):
            findings.append(Finding(
                "FAIL", "SVG-POINT-COUNT",
                f"{kind} 画了 {n_circ} 个点，数据有 {man['n_points']} 行"))

    # ③ 数值标签必须对准自己的柱子 —— 两套坐标系那次真 bug 的常驻判据
    rects = [m for m in marks if m["tag"] == "rect"]
    numeric = [t for t in doc["texts"]
               if str(t["text"]).strip().replace(".", "", 1).replace("-", "", 1).isdigit()]
    if kind in ("bar", "heatmap", "matrix") and rects:
        n_comp += 1
        bad = []
        for m in rects:
            x0, y0, x1, y1 = m["box"]
            cx = (x0 + x1) / 2
            if not any(abs(float(t["x"]) - cx) <= 1.0 for t in numeric):
                bad.append(round(cx, 1))
        if bad:
            findings.append(Finding(
                "FAIL", "SVG-LABEL-ALIGN",
                f"{len(bad)}/{len(rects)} 个柱/格子的数值标签没落在自己中心"
                f"（中心 x={bad[:8]}）—— 刻度与标记可能用了两套坐标系"))
    elif kind == "barh" and rects:
        n_comp += 1
        bad = []
        for m in rects:
            x0, y0, x1, y1 = m["box"]
            cy = (y0 + y1) / 2
            if not any(abs(float(t["y"]) - cy) <= 4.0 and float(t["x"]) >= x1 - 1.0
                       for t in numeric):
                bad.append(round(cy, 1))
        if bad:
            findings.append(Finding(
                "FAIL", "SVG-LABEL-ALIGN",
                f"{len(bad)}/{len(rects)} 根横条的数值标签没贴在条端"))

    # ④ 机制图：规格里声明的每个节点、每条边都必须真画出来。
    #    节点框是 rect/polygon，连线是 polyline；箭头用 class="arrow"，
    #    与连线同一段代码画出，不单独计数（免得一个箭头把保真判据搞成两套口径）。
    if kind == "diagram" and man.get("n_nodes") is not None:
        n_comp += 1
        # 2026-11-11：节点形状不只矩形/多边形了，正圆节点是 <circle>。
        # 覆盖面跟着种类扩，但「一个节点恰好一个主形状」这条语义不变 ——
        # 组合形状（圆柱 / 跑道 / 双框）的装饰图元走 cls="frame"，不进 marks。
        n_shape = sum(1 for m in marks
                      if m["tag"] in ("rect", "polygon", "circle"))
        n_line = sum(1 for m in marks if m["tag"] == "polyline")
        want_e = int(man.get("n_edges") or 0)
        if n_shape != int(man["n_nodes"]) or n_line != want_e:
            findings.append(Finding(
                "FAIL", "SVG-DIAGRAM-COUNT",
                f"机制图画了 {n_shape} 个节点框 / {n_line} 条连线，"
                f"规格声明 {man['n_nodes']} 个节点 / {want_e} 条边"))

    if not n_comp:
        return GateResult("data_fidelity", "NOT AUDITABLE",
                          "没有几何清单（不知道图型与声明点数），无法核对数据保真。")
    if findings:
        return GateResult("data_fidelity", "FIX BEFORE DELIVERY",
                          f"{len(findings)} 项数据保真判据不成立", findings)
    return GateResult("data_fidelity", "PASS",
                      f"{n_comp} 项保真判据成立：{len(marks)} 个标记、"
                      f"{len(doc['texts'])} 段文本，图型 {kind or '未知'}",
                      [Finding("PASS", "SVG-FIDELITY", f"{n_comp} 项核对通过")])


def _gate_font_stack(path: Path) -> GateResult:
    """字体栈**声明**检查。单源借 inspect_svg，不在这里重写一遍判据。"""
    try:
        ck = inspect_svg(path)
    except Exception as e:                                   # noqa: BLE001
        return GateResult("font_stack_declared", "NOT AUDITABLE",
                          f"结构自检跑不起来：{type(e).__name__}: {e}")
    if ck.get("cjk_font_stack_ok"):
        return GateResult("font_stack_declared", "PASS",
                          "有中文时字体栈里确实声明了中文字体（只是声明，字形见 glyph_coverage）")
    return GateResult("font_stack_declared", "FIX BEFORE DELIVERY",
                      "产物里有中文，font-family 却没声明中文字体 —— 会渲染成豆腐块",
                      [Finding("FAIL", "SVG-FONT-STACK",
                               "检查 <style> 与 font-family 属性，别只在 <desc> 里提字体名")])


def _gate_glyph(doc: dict, measured: dict | None) -> GateResult:
    """字形覆盖。第一层**一律审不了** —— SVG 不栅格化字形，字体栈只是声明。"""
    if measured and measured.get("ok"):
        missing = measured.get("missing_fonts") or []
        unchecked = measured.get("texts_unchecked") or []
        if missing:
            return GateResult("glyph_coverage", "FIX BEFORE DELIVERY",
                              f"浏览器实测：{len(missing)} 个字体回退到了缺字字体",
                              [Finding("FAIL", "SVG-GLYPH", f"{m}") for m in missing[:12]])
        if unchecked:
            return GateResult("glyph_coverage", "NOT AUDITABLE",
                              f"浏览器实测跑起来了，但 {len(unchecked)} 段文本的字形查不了",
                              [Finding("WARN", "SVG-GLYPH", str(u)) for u in unchecked[:12]])
        fonts = measured.get("fonts") or []
        basis = measured.get("glyph_basis") or "判的是字体可用性，不是逐字字形覆盖"
        return GateResult("glyph_coverage", "PASS",
                          f"浏览器实测：字体栈里 {'、'.join(fonts) or '(未名)'} 在本机可用。"
                          f"⚠️ 本项{basis}。")
    return GateResult(
        "glyph_coverage", "NOT AUDITABLE",
        "SVG 不栅格化字形，字体栈只是声明 —— 第一层判不了有没有豆腐块。"
        "需第二层浏览器 document.fonts.check() 实测；未跑实测前本项不得当成通过。")


# ══════════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════════

GATE_ORDER = ("font_size_floor", "contrast", "text_containment", "collision",
              "data_fidelity", "font_stack_declared", "glyph_coverage")


def audit_svg(svg: str | Path, *, layout: str | Path | dict | None = None,
              measured: dict | None = None) -> dict:
    """跑第一层七道门；给了 `measured`（浏览器报告）就把能升级的升到实测。

    返回 dict，**不抛异常**：读不进来 / 解析不了都变成如实的 NOT AUDITABLE，
    绝不让调用方拿到一个空报告还以为审过了。
    """
    doc = read_svg(svg, layout)
    p = Path(svg)
    if doc["error"] or not doc["exists"]:
        gates = [GateResult(n, "NOT AUDITABLE", doc["error"] or f"SVG 不存在：{p}")
                 for n in GATE_ORDER]
    else:
        gates = [
            _gate_font_size(doc),
            _gate_contrast(doc),
            _gate_text_extent(doc, measured),
            _gate_collision(doc, measured),
            _gate_data_fidelity(doc),
            _gate_font_stack(p),
            _gate_glyph(doc, measured),
        ]
    # tier 由**真的量到了几段**决定，不由「浏览器说 ok」决定：
    # 浏览器返回 ok 却一个框都没量到，仍然只算估算级。
    mok = bool(measured and measured.get("ok"))
    n_real = 0
    if mok:
        ids = {str(b.get("id")) for b in (measured or {}).get("boxes", [])
               if isinstance(b, dict)}
        n_real = sum(1 for t in doc["texts"] if str(t["id"]) in ids)
    if not mok or not doc["texts"] or n_real == 0:
        tier = "estimated"
    elif n_real == len(doc["texts"]):
        tier = "measured"
    else:
        tier = "partial"
    blocking = [g.name for g in gates if g.blocking]
    fix = [g.name for g in gates if g.verdict == "FIX BEFORE DELIVERY"]
    na = [g.name for g in gates if g.verdict == "NOT AUDITABLE"]
    return {
        "gates": [g.to_dict() for g in gates],
        "tier": tier,
        "blocking": blocking,
        "fix_before_delivery": fix,
        "not_auditable": na,
        "n_texts": len(doc["texts"]),
        "n_marks": len(doc["marks"]),
        "manifest": bool(doc["manifest"]),
        "summary": format_for_model(gates, tier),
    }


def format_for_model(gates: list[GateResult], tier: str = "estimated") -> str:
    """人读 / 模型读块。**措辞单源**：工具回包与 CLI 共用这一份，免得两处漂移。"""
    label = {"measured": "浏览器实测级", "partial": "浏览器部分实测级"}.get(
        tier, "标准库估算级")
    lines = [f"SVG 质量门（{label}）："]
    for g in gates:
        lines.append(f"  · {g.name}: {g.verdict} —— {g.detail}")
        for f in g.findings:
            if f.level in ("FAIL", "WARN"):
                lines.append(f"      [{f.level}] {f.message}")
    fix = [g.name for g in gates if g.verdict == "FIX BEFORE DELIVERY"]
    na = [g.name for g in gates if g.verdict == "NOT AUDITABLE"]
    if fix:
        lines.append(f"  ⛔ 交付前必须修：{'、'.join(fix)}")
    if na:
        lines.append(f"  ⚠️ 审不了（**不等于通过**）：{'、'.join(na)}")
    if not fix and not na:
        lines.append("  七道门全部通过或本就不适用。")
    return "\n".join(lines)