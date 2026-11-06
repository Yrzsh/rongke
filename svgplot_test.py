# -*- coding: utf-8 -*-
"""原生 SVG 出图 + 图面门「预览路线」分支的正负控。

为什么单独一份：这次加的不是又一个渲染器，而是**第三条判据口径** ——
投稿路线量 PDF 六门、预览路线量 SVG 结构自检、以及门在什么条件下允许
把硬门降成警告。三件事任何一件退化成「自称成功」，都不会被旧套件发现。

判据纪律（沿用本仓）：
  · 逐条 `[PASS]` / `[FAIL]`，结尾 `[DONE]`（`_RE_MARK` 不认 ✓）；
  · 两个方向都量：正控要绿、负控要红，光绿不算判据有效；
  · 只量语义与集合，不量某个整数或某句中文措辞。
"""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import svgplot                                     # noqa: E402
from rongke.contract import TaskContract                       # noqa: E402
from rongke.gate import CompletionClaim, CompletionGate, EvidenceRef  # noqa: E402
from rongke.tools import Tool, ToolRegistry                    # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def ck(cond: bool, label: str, extra: str = "") -> None:
    if cond:
        PASS.append(label)
        print(f"[PASS] {label}")
    else:
        FAIL.append(label)
        print(f"[FAIL] {label} {extra}")


ROWS = [
    {"批次": f"第{i}批", "组别": "对照组" if i % 2 else "处理组",
     "转化率": 20 + i * 3 + (7 if i % 2 else 0),
     "温度": 25 + i * 1.5, "产量": 100 + i * 7}
    for i in range(1, 13)
]
BASE = {
    "x": "批次", "y": "转化率", "group": "组别",
    "title": "各批次转化率对比", "xlabel": "批次", "ylabel": "转化率（%）",
    "width_mm": 89, "height_mm": 58, "table": "demo.csv", "stem": "figure",
}
WORK = Path(tempfile.mkdtemp(prefix="rk_svgplot_test_"))


# ══════════════ 1 · 九种图型正控 ══════════════
for kind in svgplot.SUPPORTED_KINDS:
    plan = dict(BASE, kind=kind)
    out = WORK / f"{kind}.svg"
    rep = svgplot.render_svg(ROWS, plan, out)
    ck(rep["ok"] is True and all(rep["checks"].values()),
       f"1.{kind} 出图且 14 项结构自检全绿", str(rep["checks"]))
    raw = out.read_text(encoding="utf-8")
    ck("各批次转化率对比" in raw and svgplot.FONT_STACK in raw
       and "荣珂原生 SVG 后端" in raw,
       f"1.{kind} 读回含中文标题/字体栈/后端声明")
    ck(rep["scope"] == "preview" and "audit_scope" in rep
       and "投稿级" in rep["publish_note"],
       f"1.{kind} 如实标注预览级与未审范围")

# 直方图 y 轴量纲不能和柱高打架（首版真踩过）
hist = (WORK / "hist.svg").read_text(encoding="utf-8")
ck("频数（count）" in hist, "1.hist y 轴标签是频数（不是原始观测值）")
box = (WORK / "box.svg").read_text(encoding="utf-8")
ck("中位数" in box, "1.box 五数概括写进 tooltip")

# ★ 肉眼截图才发现的缺陷：分类轴刻度用 i/(n-1)、柱子用 (i+0.5)/n，
#   两套坐标系 ⇒ 柱顶数值标签逐根右漂，最后一根飘到图外沿。
#   结构自检量不出「标签没对准自己的柱子」，所以这里单独钉一条几何判据。
import xml.etree.ElementTree as ET                            # noqa: E402
_NS = "{http://www.w3.org/2000/svg}"
_root = ET.parse(WORK / "bar.svg").getroot()
_rects = [e for e in _root.iter()
          if e.tag.replace(_NS, "") == "rect" and e.get("class") == "mark"]
_texts = [(float(e.get("x", 0)), (e.text or ""))
          for e in _root.iter() if e.tag.replace(_NS, "") == "text"]
_centers = [float(e.get("x", 0)) + float(e.get("width", 0)) / 2 for e in _rects]
_aligned = all(
    any(abs(tx - cx) <= 1.0 and t.strip().replace(".", "", 1).isdigit()
        for tx, t in _texts)
    for cx in _centers)
ck(bool(_centers) and _aligned,
   "1.align 每根柱的数值标签都落在自己的柱中心（不是两套坐标系）",
   f"centers={[round(c,1) for c in _centers]} texts={[t for _,t in _texts[:20]]}")


# ══════════════ 2 · 负控：画不了就如实报错 ══════════════
try:
    svgplot.render_svg(ROWS, dict(BASE, kind="violin"), WORK / "nope.svg")
    ck(False, "2.1 不支持的图型必须抛错")
except svgplot.SVGPlotError as e:
    ck("不支持" in str(e) and "plot_data" in str(e),
       "2.1 不支持的图型抛错且指路投稿路线")
try:
    svgplot.render_svg([{"批次": "A", "转化率": "n/a"}],
                       dict(BASE, kind="bar"), WORK / "nope2.svg")
    ck(False, "2.2 零数值必须抛错（不画空图）")
except svgplot.SVGPlotError as e:
    ck("没有数据" in str(e) or "一个数值" in str(e),
       "2.2 零数值抛错且说明原因")


# ══════════════ 3 · 负控：结构自检真的会打红 ══════════════
good = (WORK / "bar.svg").read_text(encoding="utf-8")
bad_font = WORK / "bad_font.svg"
bad_font.write_text(good.replace(svgplot.FONT_STACK, "Arial"), encoding="utf-8")
c1 = svgplot.inspect_svg(bad_font)
ck(c1.get("cjk_font_stack_ok") is False,
   "3.1 有中文却只有 Arial ⇒ 字体栈判据打红（豆腐块假成功不会重演）")

bad_ref = WORK / "bad_ref.svg"
bad_ref.write_text(good.replace("</svg>", '<image href="http://x/y.png"/></svg>'),
                   encoding="utf-8")
c2 = svgplot.inspect_svg(bad_ref)
ck(c2.get("xml_parsable") is True and c2.get("no_external_refs") is False,
   "3.2 塞进外链图片 ⇒ 外部引用判据打红")

bad_box = WORK / "bad_box.svg"
bad_box.write_text(re.sub(r'(<rect x=")[0-9.]+(" y="[0-9.]+"[^>]*class="mark")',
                          r'\g<1>99999\g<2>', good, count=1), encoding="utf-8")
c3 = svgplot.inspect_svg(bad_box)
ck(c3.get("marks_in_viewbox") is False,
   "3.3 标记坐标出界 ⇒ 出界判据打红")

ck(svgplot.inspect_svg(WORK / "not_here.svg").get("file_exists") is False,
   "3.4 文件不存在 ⇒ 自检如实报缺件")


# ══════════════ 4 · 图面门：预览路线的三个前提 ══════════════
PREVIEW_PAYLOAD = {"ok": True, "scope": "preview", "svg": "figure.svg",
                   "backend": "native-svg", "n_marks": 8}
PDF_PAYLOAD = {"ok": True, "ran": True,
               "run": {"ok": True, "pdf": "figure.pdf", "png": "figure.png"}}
AUDIT_PAYLOAD = {"ok": True, "verdict": "PASS", "blockers": [],
                 "warnings": [], "artifact_mtimes": {}, "gates": {}}


def make_reg(payload: dict | None = None, *, name: str = "plot_svg",
             fail: bool = False, extras: dict | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    if payload is not None:
        def fn(**_kw):
            if fail:
                raise RuntimeError("故意失败")
            return json.dumps(payload, ensure_ascii=False)
        reg.register(Tool(name, "stub", {"type": "object", "properties": {}},
                          fn=fn, domain="figure"))
    for extra_name, p in (extras or {}).items():
        reg.register(Tool(extra_name, "stub", {"type": "object", "properties": {}},
                          fn=lambda _p=p, **_kw: json.dumps(_p, ensure_ascii=False),
                          domain="figure"))
    # ★ 必须模拟**真实装配**：内核里 `audit_figure` 是装上的。
    #   首版夹具没注册它，门走的是「体检没装 ⇒ 只警告」那条旧分支，
    #   于是「没调工具该打回」的负控全变成绿灯 —— 测的不是产品，是夹具。
    if reg.get("audit_figure") is None:
        reg.register(Tool("audit_figure", "stub", {"type": "object", "properties": {}},
                          fn=lambda **_kw: json.dumps(AUDIT_PAYLOAD, ensure_ascii=False),
                          domain="figure"))
    reg.register(Tool("deliver_task", "stub", {"type": "object", "properties": {}},
                      fn=lambda **_kw: ""))
    return reg


def contract_() -> TaskContract:
    return TaskContract(raw_goal="给这批数据配一张图",
                        acceptance=["出一张可用的图"], needs_figure=True)


def claim_(seq: int, tool: str = "plot_svg") -> CompletionClaim:
    return CompletionClaim(
        deliverables=["figure.svg"],
        evidence=[EvidenceRef(tool=tool, call_seq=seq,
                              shows="SVG 结构自检 14 项全绿", acceptance="出一张可用的图")],
        summary="图已交付")


def check_gate(reg: ToolRegistry, call_name: str = "plot_svg"):
    tc = reg.call(call_name, {}) if reg.get(call_name) else None
    g = CompletionGate(reg)
    v = g.check(claim_(tc.seq) if tc else CompletionClaim(deliverables=["figure.svg"],
                                                          summary="done"),
                contract_())
    return v, g


# 4.1 负控：什么都没调 ⇒ 照旧打回（预览分支不能被当成「缺件就放过」）
v0, _ = check_gate(make_reg())
ck(not v0.passed and any("audit_figure" in r for r in v0.reasons),
   "4.1 没调任何出图工具 ⇒ 第 8 条门照旧打回")

# 4.2 正控：真有一次成功的 plot_svg ⇒ 降成警告，且警告明说「不是通过」
v1, g1 = check_gate(make_reg(PREVIEW_PAYLOAD))
warn = " ".join(getattr(v1, "warnings", []) or [])
ck(g1._preview_svg_runs() and not g1._submission_figure_runs(),
   "4.2 台账判据认出预览调用、不认成投稿链路")
ck(v1.passed, "4.2 显式预览路线放行", str(v1.reasons))
# 量集合、不量某个动词：首版写死「没跑」，文案是「没有跑」就当场假红 ——
# 与本仓记过的「措辞断言 / 总数断言」是同一种失败。
ck(all(k in warn for k in ("预览级出图", "不是通过", "六道", "投稿级")),
   "4.2 警告逐条说明未审范围，不冒充通过", warn[:200])

# 4.3 负控：调用失败 ⇒ 不许按预览放行
v2, g2 = check_gate(make_reg(PREVIEW_PAYLOAD, fail=True))
ck(not g2._preview_svg_runs() and not v2.passed,
   "4.3 plot_svg 调用失败 ⇒ 仍打回")

# 4.4 负控：伪造 scope（自称 ok 但不是预览）⇒ 不许放行
v3, g3 = check_gate(make_reg({"ok": True, "scope": "submission"}))
ck(not g3._preview_svg_runs() and not v3.passed,
   "4.4 scope 不是 preview ⇒ 不被当成预览路线")

# 4.5 负控：同时真出了 PDF ⇒ 必须照旧审，不许借预览绕过
v4, g4 = check_gate(make_reg(PREVIEW_PAYLOAD, extras={"plot_data": PDF_PAYLOAD}))
reg4 = g4.registry
reg4.call("plot_data", {})
v4b = CompletionGate(reg4).check(claim_(reg4.trace[-2].seq), contract_())
ck(bool(g4._submission_figure_runs()) and not v4b.passed,
   "4.5 台账里有 PDF ⇒ 预览分支不生效，照旧要求 audit_figure",
   str(v4b.reasons)[:200])

# 4.6 已经跑过 audit_figure ⇒ 走原判定，预览分支不覆盖它
v5, g5 = check_gate(make_reg(PREVIEW_PAYLOAD))
reg5 = g5.registry
reg5.call("audit_figure", {})
ck(bool(CompletionGate(reg5)._figure_audits()),
   "4.6 已有图面体检台账时走原判定（预览分支不抢判）")


# ══════════════ 5 · 提示词不再指向不存在的外部工具 ══════════════
from rongke.contract import DECK_TOOLCHAIN_RECIPE            # noqa: E402
recipe = " ".join(DECK_TOOLCHAIN_RECIPE)
ck("plot_svg" in recipe and "plot_data" in recipe and "figure_render" not in recipe,
   "5.1 deck 配方点名内置工具、不再点名外部 figure_render")
ck("manage_image" in recipe and "save_presentation" in recipe
   and "不要调 manage_image" in recipe,
   "5.2 配方保留插图顺序约束，并禁止拿不存在的 PNG 去插")

print(f"\n[DONE] 原生 SVG 出图与预览门自检：{len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    for f in FAIL:
        print(f"  ✘ {f}")
    sys.exit(1)
