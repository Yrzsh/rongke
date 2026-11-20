# -*- coding: utf-8 -*-
"""SVG 质量门（svgq）的正负控。

判据纪律（沿用本仓）：
  · 逐条 `[PASS]` / `[FAIL]`，结尾 `[DONE]`；
  · **两个方向都量**：正控要绿、负控要红，光绿不算判据有效；
  · 只量语义与集合，不量某个整数或某句中文措辞。

本套件最重要的一条是 3.1：同一个「锚点在框内、字画出框外」的产物，
旧的 `inspect_svg()["texts_in_viewbox"]` 判**通过**，新的文字容纳门判**红**。
那正是这次升级要堵的洞 —— 如果这条不成立，门就是白加的。

5.4 / 5.8 / 5.9 这几条的来历值得记一笔：门第一次跑就抓出**渲染器三处真缺陷**
（barh 复用竖直图的坐标系框架 ⇒ 两套类别轴叠在一起；box 分组时默认框与箱线
loop 各画一套类别轴；bar 按类别着色却挂组别图例，还压住最高柱的数值标签）。
三处都已修，于是判据翻成正控认「真产物已干净」，另用合成几何复刻当初的重叠
形态当负控（5.7）—— **缺陷修好了测试不该反而红，但门的牙口必须另行证明**。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import svgplot, svgq                                  # noqa: E402
from rongke.figure import GateResult                              # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def ck(cond: bool, label: str, extra: str = "") -> None:
    if cond:
        PASS.append(label)
        print(f"[PASS] {label}")
    else:
        FAIL.append(label)
        print(f"[FAIL] {label} {extra}")


WORK = Path(tempfile.mkdtemp(prefix="rk_svgq_test_"))
FONT = svgplot.FONT_STACK
ROWS = [
    {"批次": f"第{i}批", "组别": "对照组" if i % 2 else "处理组",
     "转化率": 20 + i * 3 + (7 if i % 2 else 0),
     "温度": 25 + i * 1.5, "产量": 100 + i * 7}
    for i in range(1, 13)
]
BASE = {"x": "批次", "y": "转化率", "group": "组别", "title": "各批次转化率对比",
        "xlabel": "批次", "ylabel": "转化率（%）", "width_mm": 89, "height_mm": 58,
        "table": "demo.csv", "stem": "figure"}


def render(kind: str, name: str = "", **over) -> tuple[Path, dict]:
    out = WORK / f"{name or kind}.svg"
    rep = svgplot.render_svg(ROWS, dict(BASE, kind=kind, **over), out)
    return out, rep


def synth(name: str, texts: list[str], *, w: int = 336, h: int = 220,
          width_mm: float = 89.0, marks: str = "", style: str = "") -> Path:
    """手写一份 SVG 当被审对象 —— 门必须能审任意 SVG，不只审自己产的。"""
    p = WORK / f"{name}.svg"
    p.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}px" height="{h}px" '
        f'viewBox="0 0 {w} {h}"><title id="t">测试图</title>'
        f'<style>text {{ font-family: {style or FONT}; }}</style>'
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="{svgplot._PAPER}" class="paper"/>'
        f'{marks}<g font-family="{FONT}">' + "".join(texts) + "</g></svg>\n",
        encoding="utf-8", newline="\n")
    p.with_suffix(".layout.json").write_text(json.dumps({
        "schema_version": 1, "backend": "synthetic",
        "figure": {"width_mm": width_mm, "height_mm": round(h * width_mm / w, 2),
                   "width_px": w, "height_px": h, "mm_per_px": width_mm / w,
                   "intrinsic_width_mm": w * 25.4 / 96.0,
                   "clamped": abs(w - width_mm * 96 / 25.4) > 0.01},
        "kind": "synthetic", "n_points": 0, "n_marks": 0, "n_text": len(texts),
        "texts": [], "marks": [],
    }, ensure_ascii=False), encoding="utf-8")
    return p


def verdict(rep: dict, name: str) -> str:
    for g in rep["gates"]:
        if g["name"] == name:
            return g["verdict"]
    return "<门不存在>"


def findings(rep: dict, name: str) -> list[dict]:
    for g in rep["gates"]:
        if g["name"] == name:
            return g["findings"]
    return []


def txt(x, y, s, *, size=8.5, anchor="middle", fill=svgplot._INK, tid="") -> str:
    idattr = f' id="{tid}"' if tid else ""
    return (f'<text{idattr} x="{x}" y="{y}" font-size="{size}" '
            f'text-anchor="{anchor}" fill="{fill}">{s}</text>')


CLEAN_MARK = '<rect x="60" y="60" width="40" height="80" fill="#2E5A63" class="mark"/>'

# ══════════════ 0 · 纪律：审不了绝不等于通过 ══════════════
ck(GateResult("x", "NOT AUDITABLE").blocking is True
   and GateResult("x", "NOT AUDITABLE").ok is False,
   "0.1 NOT AUDITABLE 既不算 ok 也要阻断（词汇没被压成布尔）")
ck(GateResult("x", "PASS").ok is True and GateResult("x", "PASS").blocking is False,
   "0.2 PASS 才是通过")

miss = svgq.audit_svg(WORK / "不存在.svg")
ck(len(miss["gates"]) == len(svgq.GATE_ORDER)
   and all(g["verdict"] == "NOT AUDITABLE" for g in miss["gates"])
   and miss["tier"] == "estimated",
   "0.3 文件不存在 ⇒ 七道门全判审不了，不抛异常也不空报告", str(miss["summary"])[:120])

bad = WORK / "bad.svg"
bad.write_text("<svg><text> broken", encoding="utf-8")
rb = svgq.audit_svg(bad)
ck(all(g["verdict"] == "NOT AUDITABLE" for g in rb["gates"]),
   "0.4 XML 解析不了 ⇒ 全判审不了")

# ══════════════ 1 · 正控：干净产物七道门如实报 ══════════════
for kind in ("scatter", "line", "hist"):
    out, rep = render(kind, name=f"clean_{kind}")
    a = svgq.audit_svg(out)
    ck(verdict(a, "font_size_floor") == "PASS"
       and verdict(a, "contrast") == "PASS"
       and verdict(a, "text_containment") == "PASS"
       and verdict(a, "collision") == "PASS"
       and verdict(a, "data_fidelity") == "PASS"
       and verdict(a, "font_stack_declared") == "PASS",
       f"1.{kind} 六道可判门全绿", str(a["blocking"]))
    ck(verdict(a, "glyph_coverage") == "NOT AUDITABLE"
       and "glyph_coverage" in a["not_auditable"]
       and a["tier"] == "estimated",
       f"1.{kind} 字形覆盖第一层如实判审不了、不当成通过")
    # 兼容：这次升级绝不动预览路线的三个声明
    ck(rep["ok"] is True and all(rep["checks"].values())
       and rep["scope"] == "preview" and "audit_scope" in rep,
       f"1.{kind} ok/scope/checks 语义未被门改动（gate.py 与旧套件钉死的）")
    ck(a["n_texts"] > 0 and a["n_marks"] > 0 and a["manifest"] is True,
       f"1.{kind} 门读到了产物里的文本/标记与几何清单")

# text id 是两层尺子的桥：产物里每个 <text> 都得有 id
import xml.etree.ElementTree as ET                                # noqa: E402
_out, _ = render("scatter", name="ids")
_root = ET.parse(_out).getroot()
_ts = [e for e in _root.iter() if e.tag.endswith("}text")]
ck(bool(_ts) and all(e.get("id") for e in _ts)
   and len({e.get("id") for e in _ts}) == len(_ts),
   "1.id 每个 <text> 都有唯一 id（浏览器层靠它 join 回声明意图）")

# ══════════════ 2 · 字号下限：物理 pt，不是 px ══════════════
p = synth("font_small", [txt(160, 100, "小字", size=4, tid="s0")], marks=CLEAN_MARK)
a = svgq.audit_svg(p)
ck(verdict(a, "font_size_floor") == "FIX BEFORE DELIVERY",
   "2.1 4px 字按 89mm 宽换算不足 5pt ⇒ 红", str(findings(a, "font_size_floor"))[:160])

# ★ 真缺陷：width_mm 被 max(320,…) 钳位后，px 与声明 mm 脱钩
out60, rep60 = render("scatter", name="clamp60", width_mm=60)
man60 = json.loads(out60.with_suffix(".layout.json").read_text(encoding="utf-8"))
a60 = svgq.audit_svg(out60)
ck(man60["figure"]["clamped"] is True
   and man60["figure"]["width_px"] == 320.0
   and man60["figure"]["mm_per_px"] < 96 / 25.4 / 1.0
   and verdict(a60, "font_size_floor") == "FIX BEFORE DELIVERY",
   "2.2 声明 60mm 但画布被钳到 320px ⇒ 字号按声明宽度算不足 5pt，门抓到根因",
   f"mm_per_px={man60['figure']['mm_per_px']:.4f} v={verdict(a60, 'font_size_floor')}")
ck(any(f["check_id"] == "SVG-CANVAS-CLAMP" for f in findings(a60, "font_size_floor")),
   "2.3 钳位本身被单独记一条，不只报字号不足")

# 同一份产物，89mm 时应当绿 —— 证明红是钳位造成的，不是门一律红
out89, _ = render("scatter", name="clamp89", width_mm=89)
ck(verdict(svgq.audit_svg(out89), "font_size_floor") == "PASS",
   "2.4 同一渲染器 89mm 不钳位 ⇒ 字号门绿（红绿同源，判据有效）")

p = synth("no_manifest", [txt(160, 100, "字", tid="m0")], marks=CLEAN_MARK)
p.with_suffix(".layout.json").unlink()
ck(verdict(svgq.audit_svg(p), "font_size_floor") == "NOT AUDITABLE",
   "2.5 没有几何清单 ⇒ 字号门判审不了，不拿 px 冒充 pt")

# ══════════════ 3 · 文字容纳：旧尺子放过、新尺子抓住 ══════════════
# 锚点 x=300 在 viewBox(0..336) 内，但 20 个全角字往右展开 170px ⇒ 画出框外
long_label = "这是一个很长很长的中文坐标轴标签"
p = synth("overflow", [txt(300, 100, long_label, anchor="start", tid="o0")],
          marks=CLEAN_MARK)
old = svgplot.inspect_svg(p)
a = svgq.audit_svg(p)
ck(old.get("texts_in_viewbox") is True
   and verdict(a, "text_containment") == "FIX BEFORE DELIVERY",
   "3.1 ★ 锚点在框内、字画出框外：旧结构自检判通过，新容纳门判红",
   f"old={old.get('texts_in_viewbox')} new={verdict(a, 'text_containment')}")

# 声明容器：机制图节点标签装不进节点框
p = synth("nodefit", [txt(80, 80, "受体激活与下游通路", size=9, tid="n0")],
          marks=CLEAN_MARK)
man = json.loads(p.with_suffix(".layout.json").read_text(encoding="utf-8"))
man["texts"] = [{"id": "n0", "box": [60, 60, 100, 100]}]
p.with_suffix(".layout.json").write_text(json.dumps(man, ensure_ascii=False),
                                         encoding="utf-8")
a = svgq.audit_svg(p)
ck(verdict(a, "text_containment") == "FIX BEFORE DELIVERY"
   and any(f["check_id"] == "SVG-TEXT-FIT" for f in findings(a, "text_containment")),
   "3.2 节点标签越出声明容器 >15% ⇒ 红", str(findings(a, "text_containment"))[:160])

# 边界带：越界 2~15% 时估算定不了案，必须判审不了而不是通过
p = synth("borderline", [txt(80, 80, "abcdefghij", size=9, anchor="start", tid="b0")],
          marks=CLEAN_MARK)
man = json.loads(p.with_suffix(".layout.json").read_text(encoding="utf-8"))
# 10 个 ASCII × 0.55em × 9px = 49.5px，从 x=80 起到 129.5；框右沿 125 ⇒ 越界 4.5/45=10%
man["texts"] = [{"id": "b0", "box": [80, 60, 125, 100]}]
p.with_suffix(".layout.json").write_text(json.dumps(man, ensure_ascii=False),
                                         encoding="utf-8")
a = svgq.audit_svg(p)
ck(verdict(a, "text_containment") == "NOT AUDITABLE",
   "3.3 越界落在 2~15% 边界带 ⇒ 判审不了交第二层，不冒充通过",
   verdict(a, "text_containment"))

# 同一份边界带产物，给了浏览器实测 bbox 就该定案（这里用一份"装得下"的实测）
measured_ok = {"ok": True, "engine": "test-double",
               "boxes": [{"id": "b0", "x": 80, "y": 70, "width": 40, "height": 10}],
               "n_spans": 1, "missing_fonts": [], "texts_unchecked": []}
a2 = svgq.audit_svg(p, measured=measured_ok)
ck(verdict(a2, "text_containment") == "PASS" and a2["tier"] == "measured",
   "3.4 第二层实测装得下 ⇒ 边界带升成 PASS，tier 如实标 measured")

# ══════════════ 4 · 对比度 ══════════════
p = synth("lowcontrast", [txt(160, 100, "浅色字", fill="#E8E4DA", tid="c0")],
          marks=CLEAN_MARK)
a = svgq.audit_svg(p)
ck(verdict(a, "contrast") == "FIX BEFORE DELIVERY",
   "4.1 接近纸色的字 ⇒ 对比度 <3:1 判红", str(findings(a, "contrast"))[:140])

p = synth("goodcontrast", [txt(160, 100, "墨色字", fill=svgplot._INK, tid="c1")],
          marks=CLEAN_MARK)
ck(verdict(svgq.audit_svg(p), "contrast") == "PASS",
   "4.2 墨色压纸色 ⇒ 绿（红绿同源）")

# 半透明填色必须先合成再算，否则拿原色算会虚高
r_direct = svgq.contrast_ratio(svgplot._INK, "#B23A2E")
r_comp = svgq.contrast_ratio(svgplot._INK,
                             svgq._composite("#B23A2E", svgplot._PAPER, 0.3))
ck(r_direct is not None and r_comp is not None and r_comp > r_direct,
   "4.3 半透明标记先与纸色合成再算对比度（热图格子就是这种）",
   f"direct={r_direct:.2f} composite={r_comp:.2f}")
ck(svgq.contrast_ratio("not-a-color", "#FFF") is None,
   "4.4 颜色解析不了返回 None，不出假数")

# ══════════════ 5 · 碰撞 ══════════════
p = synth("collide", [txt(160, 100, "第一段文字", tid="a"),
                      txt(166, 102, "第二段文字", tid="b")], marks=CLEAN_MARK)
a = svgq.audit_svg(p)
ck(verdict(a, "collision") == "FIX BEFORE DELIVERY",
   "5.1 两段文明显重叠 ⇒ 红", str(findings(a, "collision"))[:160])

p = synth("apart", [txt(100, 100, "左边", tid="a"), txt(260, 100, "右边", tid="b")],
          marks=CLEAN_MARK)
ck(verdict(svgq.audit_svg(p), "collision") == "PASS",
   "5.2 两段文分开 ⇒ 绿（红绿同源）")

p = synth("one", [txt(160, 100, "只有一段", tid="a")], marks=CLEAN_MARK)
ck(verdict(svgq.audit_svg(p), "collision") == "NOT APPLICABLE",
   "5.3 只有一段文本 ⇒ 本就不适用，不算审不了也不算通过")

# 描边光晕：同串近距重绘不是碰撞；不同串同位置仍然是碰撞
p = synth("halo", [txt(160, 100, "标签", tid="h0", fill="#FFFFFF"),
                   txt(161, 101, "标签", tid="h1")], marks=CLEAN_MARK)
ck(verdict(svgq.audit_svg(p), "collision") == "PASS",
   "5.5 同串文字 1px 内重绘（描边光晕）⇒ 不误判成碰撞")
p = synth("nohalo", [txt(160, 100, "标签甲", tid="h0"),
                     txt(161, 101, "标签乙", tid="h1")], marks=CLEAN_MARK)
ck(verdict(svgq.audit_svg(p), "collision") == "FIX BEFORE DELIVERY",
   "5.6 不同串同位置 ⇒ 光晕豁免不生效，照旧判红（豁免没变成漏报口子）")

# ★ 这两处曾经是**渲染器真缺陷**：barh 复用竖直图框（数值刻度画在 y 轴、
#   类别标签画在 x 轴），box 分组时默认框与箱线 loop 各画一套类别轴。
#   门抓到之后已修（`_draw_frame` 的 horizontal / x_ticks 两支）。
#   修完就不能再拿真缺陷当负控 —— 否则「缺陷修好了测试反而红」。
#   改成：正控认真产物已干净；负控用合成几何复刻当初那套重叠（见 5.7）。
for kind in ("barh", "box"):
    out, _ = render(kind, name=f"fixed_{kind}")
    a = svgq.audit_svg(out)
    ck(verdict(a, "collision") == "PASS",
       f"5.4-{kind} 修好后真产物无文字重叠（当初那两套类别轴已拆掉）",
       str(findings(a, "collision")[:2])[:170])

# 合成负控：复刻 barh 当初的形态 —— 竖直框的数值 y 刻度（x=46, anchor=end）
# 与横向柱自己的类别标签（x=48, anchor=end）落在同一批基线上。
_ys = [60.0, 90.0, 120.0]
p = synth("two_axes",
          [txt(46, y, f"{20 + i * 5:.1f}", anchor="end", tid=f"n{i}")
           for i, y in enumerate(_ys)]
          + [txt(48, y, f"第{i + 1}批", anchor="end", tid=f"c{i}")
             for i, y in enumerate(_ys)], marks=CLEAN_MARK)
_a = svgq.audit_svg(p)
ck(verdict(_a, "collision") == "FIX BEFORE DELIVERY"
   and bool(findings(_a, "collision")),
   "5.7 合成负控：两套轴标签叠在同一基线（barh 当初的真缺陷形态）⇒ 门照旧判红",
   str(findings(_a, "collision")[:1])[:170])

# 图例的两条结构性判据（修 bar 那次一并钉住）：
#   ① 按类别着色的图型不许挂组别图例 —— 那是个假映射；
#   ② 真按组着色的图型，图例必须在绘图区**外**，不许赌数据长不到那儿。
for kind in ("bar", "barh", "hist"):
    out, _ = render(kind, name=f"nolegend_{kind}")
    _ts = [t["text"] for t in svgq.read_svg(out)["texts"]]
    ck(not any(t in ("对照组", "处理组") for t in _ts),
       f"5.8-{kind} 按类别/单色着色 ⇒ 不挂组别图例（颜色与组别无关，挂了就是假映射）",
       str([t for t in _ts if t in ("对照组", "处理组")]))
out, _ = render("scatter", name="legend_outside")
_leg = [t for t in svgq.read_svg(out)["texts"] if t["text"] in ("对照组", "处理组")]
ck(len(_leg) == 2 and all(t["y"] < 46.0 for t in _leg),
   "5.9 散点图图例横排在绘图区外的顶栏（y<46），结构上不可能压住数据",
   str(_leg)[:170])

# ══════════════ 6 · 数据保真 ══════════════
out, _ = render("bar", name="fidelity_ok")
ck(verdict(svgq.audit_svg(out), "data_fidelity") == "PASS",
   "6.1 正常柱状图：数值标签对准柱心 ⇒ 绿")

# 复刻「两套坐标系」那次真 bug：把数值标签整体右移
raw = out.read_text(encoding="utf-8")
import re                                                          # noqa: E402
shifted = re.sub(r'(<text id="t\d+" x=")([\d.]+)(" y="[\d.]+" font-size="8\.0" '
                 r'text-anchor="middle" fill="#2B2723">[\d.]+</text>)',
                 lambda m: f"{m.group(1)}{float(m.group(2)) + 20:.1f}{m.group(3)}",
                 raw)
p = WORK / "fidelity_shifted.svg"
p.write_text(shifted, encoding="utf-8")
out.with_suffix(".layout.json").replace(p.with_suffix(".layout.json"))
a = svgq.audit_svg(p)
ck(shifted != raw and verdict(a, "data_fidelity") == "FIX BEFORE DELIVERY"
   and any(f["check_id"] == "SVG-LABEL-ALIGN" for f in findings(a, "data_fidelity")),
   "6.2 ★ 数值标签整体右移 20px（两套坐标系那次的形态）⇒ 保真门红",
   f"changed={shifted != raw} v={verdict(a, 'data_fidelity')}")

# 标记数与清单不符
out2, _ = render("scatter", name="fidelity_count")
man = json.loads(out2.with_suffix(".layout.json").read_text(encoding="utf-8"))
man["n_marks"] = man["n_marks"] + 3
out2.with_suffix(".layout.json").write_text(json.dumps(man, ensure_ascii=False),
                                            encoding="utf-8")
a = svgq.audit_svg(out2)
ck(verdict(a, "data_fidelity") == "FIX BEFORE DELIVERY"
   and any(f["check_id"] == "SVG-MARK-COUNT" for f in findings(a, "data_fidelity")),
   "6.3 产物标记数与清单声明不符 ⇒ 红")

# 逐点图少画一个点
raw = out2.read_text(encoding="utf-8")
i = raw.find('<circle')
j = raw.find("</circle>", i)
k = raw.find("/>", i)
end = min(x for x in (j + 9, k + 2) if x > 0) if (j > 0 or k > 0) else -1
p3 = WORK / "fidelity_missing_point.svg"
p3.write_text(raw[:i] + raw[end:], encoding="utf-8") if end > 0 else p3.write_text(raw, encoding="utf-8")
man["n_marks"] = man["n_marks"] - 3
p3.with_suffix(".layout.json").write_text(json.dumps(man, ensure_ascii=False),
                                          encoding="utf-8")
a = svgq.audit_svg(p3)
ck(end > 0 and verdict(a, "data_fidelity") == "FIX BEFORE DELIVERY"
   and any(f["check_id"] == "SVG-POINT-COUNT" for f in findings(a, "data_fidelity")),
   "6.4 散点图少画一个点 ⇒ 点数与数据行数不符判红",
   str(findings(a, "data_fidelity"))[:160])

# ══════════════ 7 · 字体栈与字形 ══════════════
p = synth("arial_only", [txt(160, 100, "中文字", tid="f0")], marks=CLEAN_MARK,
          style="Arial, sans-serif")
# synth 的 <g> 上仍带 FONT，得一并换成 Arial 才是真负控
p.write_text(p.read_text(encoding="utf-8").replace(FONT, "Arial, sans-serif"),
             encoding="utf-8")
a = svgq.audit_svg(p)
ck(verdict(a, "font_stack_declared") == "FIX BEFORE DELIVERY",
   "7.1 有中文却只声明 Arial ⇒ 字体栈门红")
ck(verdict(a, "glyph_coverage") == "NOT AUDITABLE",
   "7.2 即便字体栈红了，字形覆盖仍单独判审不了（两件事不合并）")

clean, _ = render("scatter", name="glyph_clean")
a = svgq.audit_svg(clean)
ck(verdict(a, "glyph_coverage") == "NOT AUDITABLE"
   and "glyph_coverage" in a["blocking"],
   "7.3 干净产物 + 没跑浏览器 ⇒ 字形门仍审不了且计入阻断，绝不当成通过")

def measured_double(path: Path, **over) -> dict:
    """造一份「浏览器量到的就是估算值」的实测替身，用来验升级通路。

    不是拿它当质量证据 —— 它只证明 svgq 收到真值形状时会把门升到实测级。
    真浏览器由 svgmeasure 负责，另有两个方向的反例钉住「量不到不算通过」。
    """
    doc = svgq.read_svg(path)
    boxes = []
    for t in doc["texts"]:
        x0, y0, x1, y1 = svgq.est_text_box(t)
        boxes.append({"id": t["id"], "x": x0, "y": y0,
                      "width": x1 - x0, "height": y1 - y0,
                      "text": t["text"], "advance": x1 - x0})
    rep = {"ok": True, "engine": "test-double", "boxes": boxes,
           "fonts": ["Microsoft YaHei"], "missing_fonts": [], "texts_unchecked": [],
           "n_spans": len(boxes), "n_boxes": len(boxes), "n_compared": len(boxes),
           "glyph_basis": "判的是字体栈里声明的中文字体在本机是否可用，不是逐字字形覆盖"}
    rep.update(over)
    return rep


a = svgq.audit_svg(clean, measured=measured_double(clean))
ck(verdict(a, "glyph_coverage") == "PASS" and a["tier"] == "measured",
   "7.4 浏览器实测字体可用 ⇒ 字形门升成 PASS")
ck("不是逐字字形覆盖" in str(findings(a, "glyph_coverage"))
   or "不是逐字字形覆盖" in next(g["detail"] for g in a["gates"]
                                if g["name"] == "glyph_coverage"),
   "7.4b 字形门的 PASS 措辞不许放大成逐字保证（浏览器没有那个 API）")
a = svgq.audit_svg(clean, measured=measured_double(clean, missing_fonts=["LastResort"],
                                                 fonts=[]))
ck(verdict(a, "glyph_coverage") == "FIX BEFORE DELIVERY",
   "7.5 浏览器实测整个字体栈都不可用 ⇒ 红")
a = svgq.audit_svg(clean, measured={"ok": False, "error": "无 playwright",
                                    "boxes": []})
ck(verdict(a, "glyph_coverage") == "NOT AUDITABLE" and a["tier"] == "estimated",
   "7.6 浏览器层跑失败 ⇒ 退回审不了，不拿失败的实测当通过")

# ★ 浏览器说 ok 却一个框都没量到 ⇒ 不许算实测级
a = svgq.audit_svg(clean, measured={"ok": True, "engine": "x", "boxes": [],
                                    "fonts": [], "missing_fonts": [],
                                    "texts_unchecked": ["t0 量不到"], "n_spans": 17})
ck(a["tier"] == "estimated" and verdict(a, "text_containment") == "NOT AUDITABLE",
   "7.7 实测返回 ok 但零个框 ⇒ tier 仍是估算级、容纳门判审不了",
   f"tier={a['tier']} v={verdict(a, 'text_containment')}")

# 部分实测：量到一半，另一半不得当成通过
half = measured_double(clean)
half["boxes"] = half["boxes"][:3]
half["texts_unchecked"] = ["t9 量不到"]
a = svgq.audit_svg(clean, measured=half)
ck(a["tier"] == "partial" and verdict(a, "collision") == "NOT AUDITABLE",
   "7.8 只量到一部分 ⇒ tier=partial，碰撞门判审不了而不是通过",
   f"tier={a['tier']} v={verdict(a, 'collision')}")

# ══════════════ 8 · 汇总措辞单源 ══════════════
a = svgq.audit_svg(clean)
ck("审不了" in a["summary"] and "不等于通过" in a["summary"]
   and "glyph_coverage" in a["summary"],
   "8.1 汇总块明写「审不了 ≠ 通过」并点名是哪一道")
ck(set(a["blocking"]) >= {"glyph_coverage"} and a["fix_before_delivery"] == [],
   "8.2 干净产物的阻断项只有字形门，没有假 FAIL", str(a["blocking"]))
ck(len(a["gates"]) == len(svgq.GATE_ORDER)
   and [g["name"] for g in a["gates"]] == list(svgq.GATE_ORDER),
   "8.3 七道门齐全且顺序稳定（少一道就是静默降级）")

print()
if FAIL:
    print(f"[DONE] SVG 质量门正负控：{len(PASS)} PASS / {len(FAIL)} FAIL")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print(f"[DONE] SVG 质量门正负控：{len(PASS)} PASS / {len(FAIL)} FAIL")