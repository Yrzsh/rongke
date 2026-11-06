# -*- coding: utf-8 -*-
"""组会汇报 PPT 的体检门：**编排 + 收紧 + 接进硬门**。

## 为什么长这个样子

和 `figure.py` 是同一个形状，理由也同源：

| 这件事 | 谁做 |
|---|---|
| **编排** | 荣珂调技能 `nature-paper2ppt` 自带的 `scripts/audit_pptx_quality.py`（纯标准库） |
| **收紧** | 荣珂补上"上游量得出来、却没有任何判定"的那几项（见下） |
| **硬门** | `gate.py` 第 9 条读这份报告，并要求它**新鲜** |

**荣珂不重写那个审计器。** 重写一遍就有两份实现、两套阈值、两个结论，
而"两处真相"迟早会漂成"两副面孔"。

## 上游审计器量得出来、却没有判定的三件事（实测）

1. **讲稿备注**。`audit_pptx` 返回里有 `notes_count`，但**没有任何检查读它**。
   而组会 PPT 的备注就是**讲稿本身** —— 数出来了却不判，等于没查。
2. **字号**。整个审计器不看 `sz` 属性。图面那边有"每个字形 ≥ 5 pt"的硬下限
   （`nature-figure` 的 glyph 门），deck 这边一个字都没有。
3. **新鲜度**。`figure.py` 有 `freshness` 门（报告 mtime vs 磁盘现状），
   deck 这边没有 ⇒ 拿上一版 deck 的报告当合格证，谁也拦不住。

## ★ 一条影响设计的实测：**上游没有备注工具**

`grep -rniE "notes|speaker" vendor/ppt-mcp` → 唯一命中是
`slide_layout_templates.json` 里一句无关的 `implementation_notes`。
38 个（37 上游 + 1 内核）ppt 域工具里，**没有一个是写 speaker notes 的**。
后果必须被设计吸收：

> 如果"备注覆盖率"做成**无条件硬门**，那么"纯走 MCP 建出来的 deck"**永远无法通过** ——
> 那不是严格，那是个死结：模型会一直重试、烧步数，最后要么伪造，要么放弃。

**实测的血债（2026-09-21）**：这个死结真的发生了，而且代价是**整场任务**——
deck 建好之后 67 次调用里约 19 次全耗在找"怎么写备注"上，最后
`audit_deck`=0、`deliver_task`=0，25 分钟跑完零交付。
根因不是模型不努力，是**底座没给工具**：技能里写的兜底路径
"另跑一小段 python-pptx"需要一个执行代码的能力，而内核没有。

⇒ 修法就是本模块的 `write_slide_notes`（模型给**文本**，内核写代码）。
所以现在"补备注"**有工具可走**了；但这**不改变门槛**：
判据仍由契约独立推出（见下），工具的存废只是让合格变得**可能**，
不是让合格变得**自动**。

所以本模块的分级是：

* 备注不足 → **默认 WARN**（进 `warnings`，醒目但不阻断），
  并**点名工具**：说清"调 `set_slide_notes` 补，补完重跑 audit_deck"；
* **只有当契约明确要了讲稿**（用户原话里有"讲稿/备注/演讲词/speaker notes"）
  → 才升成 **FAIL**。

这条判据由 **`gate.py` 从契约独立推出**，不采信模型在 `audit_deck` 里传的
`require_notes`（那是 R 型：被审者给自己定标准）。工具报事实，门下判决。
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# 设计资产（版面网格 / 字号阶梯 / 色彩角色 / 页型几何）——**全项目唯一单源**。
# 见 `design.py` 开头的诊断：产出上限来自资产本身，判据只能划定下限。
from . import design
# 槽位容量模型（em 宽度 / 真实内边距 / 作者原文作存在性证明）——**全项目唯一单源**。
# 抽取器、填充子脚本、本模块都从这里取；见 `slotfit.py` 开头的五条量测。
from . import slotfit

# ── 常量 ────────────────────────────────────────────────────
EMU_PER_POINT = 12700

# 客观不可读的下限。**刻意不做成配置项**：没人会想把它调低，
# 而一个"可以调低到 4pt 的硬下限"等于没有下限（它只是把责任推给配置的人）。
HARD_MIN_FONT_PT = 8.0

# 骨架几何的对称门槛（2026-09-21）。左右边距之比超过它 ⇒ 骨架是按**另一个画布**排的。
# 为什么取 3：正常版式的左右边距比落在 1~2.5（居中、或略偏），而
# 「4:3 骨架套在 16:9 画布上」实测是 **7.7**（ml=457200 / mr=3505200）。
# 门槛放在断层中间，两侧都留足余量 —— 误报比漏报更伤（设计定律 10）。
GEO_ASYM_MAX = 3.0

# **内容骨架**占位符的类型白名单（单源）。
# ⚠️ 三处必须一致：判据 `placeholder_geometry_stale`、修复 `remap_skeleton`
#   （住在 `_STYLE_SCRIPT` 子进程字符串里，**引用不到这个常量**，只能重写一份）、
#   以及 `deck_test` 里那条"两处一致"的断言（防 H 型：改一处忘一处必静默漂移）。
# 判读口径：
#   · 白名单**只回答"哪些是骨架"**，不回答"哪些该拉伸" —— 后者的规则在 remap 里。
#   · 为什么排除 `dt`/`ftr`/`sldNum`：它们是**页脚/日期/页码**，坐标是**锚点语义**
#     （靠左/居中/靠右）。拿它们判"不对称"是纯误报（实测 master 的 `sldNum`
#     左右边距比是 **14.3**，不排除的话每一份 deck 都被报）。
#   · `subTitle` 必须在内：封面副标题也是骨架。实测漏掉它 ⇒ 标题拉宽了、
#     副标题没拉，同一页上 94.4% 与 63.7% 并排 —— 比全都不拉更难看。
CONTENT_PH_TYPES = ("title", "ctrTitle", "body", "subTitle")

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# 上游脚本的 finding 等级 → 我们的处置。**只在这一处映射**（别处不要再判一遍）。
_UPSTREAM_HIGH_IS_BLOCKING = True

# 判"这段文字算不算正文体量"的门槛：低于它就不拿字号去判 ——
# 图表里的轴标签、页码、角标常常是有意做小的，拿它们去硬拦就是误报，
# 而误报会教模型学会无视整份报告（设计定律 10）。
# 取 12 而不是更大：**此处只挡住 1~3 字的标记**，真正的正文（哪怕只有一句话）
# 都要被量到 —— 否则稀疏的 deck 会整篇报"字号：没查"，那条检查就等于不存在。
# （实测教训：原取 40，三页演示里有两页的正文只有 27~33 字 ⇒ 全程量不到字号。）
_BODY_TEXT_MIN_CHARS = 12


# ══════════════════════ 结构 ══════════════════════
@dataclass
class Finding:
    level: str          # PASS / WARN / FAIL / SKIP
    check_id: str
    message: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"level": self.level, "check_id": self.check_id,
                "message": self.message, "evidence": self.evidence}


@dataclass
class GateResult:
    """一道门的结果。

    **verdict 词汇保住原样，不许压成布尔** —— `NOT AUDITABLE`（审不了）
    和 `PASS`（审过了）是完全不同的两件事，合成一个 `ok` 就是造假的证据
    （本项目 M 型 / I 型缺陷）。
    """
    name: str
    verdict: str = "SKIPPED"
    detail: str = ""
    findings: list[Finding] = field(default_factory=list)
    report_path: str = ""
    raw: str = ""
    # 门可以不只给"过/不过"，还给**数字**（视觉一节就是这么用的）。
    # 抽成 `extra` 之后任何门都能挂自己的一组数字，不必为每道门新加字段 ——
    # 加字段就得同步改 `to_dict`，改漏一处就是 H 型（两处真相）。
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict in ("PASS", "NOT APPLICABLE")

    @property
    def blocking(self) -> bool:
        return self.verdict in ("FIX BEFORE DELIVERY", "NOT AUDITABLE",
                                "STALE", "SKIPPED")

    def to_dict(self) -> dict:
        return {"name": self.name, "verdict": self.verdict, "detail": self.detail,
                "report_path": self.report_path,
                "findings": [f.to_dict() for f in self.findings],
                "extra": self.extra}


@dataclass
class DeckFacts:
    """从 .pptx 里量出来的**事实**（"当时值"，不是现值）。

    只放量得出来的东西。量不出来的一律 `None` —— **宁可不出数，不出假数**
    （设计定律 12）。例如运行没显式给 `sz` 的文本，字号记为 `None` 而不是
    拿默认值填一个好看的数：`sz` 缺省是"继承母版"，真实值我们**不知道**。
    """
    path: str = ""
    slide_count: int = 0
    notes_slides: int = 0
    slides_with_notes: int = 0
    notes_ratio: float = 0.0
    media_count: int = 0
    slide_cx: int = 0
    slide_cy: int = 0
    min_font_pt: float | None = None       # 全篇最小显式字号
    min_body_font_pt: float | None = None  # 只算"正文体量"形状里的最小字号
    slides: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    # ── 视觉事实（2026-09-21 新增）。全部"量得出来才记"，量不出记 None / 空 ──
    aspect: float | None = None            # 宽/高
    is_widescreen: bool | None = None      # None = 宽高读不到 ⇒ 判不了，不猜
    theme_colors: dict = field(default_factory=dict)
    theme_fonts: dict = field(default_factory=dict)
    layout_kinds: int = 0                  # 全篇用了几种不同的版式
    layout_top_share: float = 0.0          # 最常用那一种版式占的比例
    pictures: int = 0
    charts: int = 0
    tables: int = 0
    runs_total: int = 0
    runs_with_font: int = 0
    # ── 文字溢出（2026-09-21）。⚠️ `overflow_measured == 0` 是**没查**，不是"没溢出" ──
    overflow_measured: int = 0            # 量到几个「既有框、又有显式字号」的形状
    overflow_worst: float | None = None   # 全篇最大的「估高 / 可用高」
    # 「为什么没出数」的分类计数：`autofit`（被自适应豁免）/ `nogeom`（框量不到）/
    #   `nofont`（字号推不出）/ `empty`（无文字）/ `ok`（出了数）。
    #   ⚠️ 没有它就分不清「没查」和「没有」——`overflow_measured == 0`
    #   可能是"读错了层"，也可能是"真的都合规"，**含混不是诚实**。
    overflow_skipped: dict = field(default_factory=dict)
    # 继承链解析时哪一步没读出来。**如实记**：`[]` 才是"读全了"，
    #   `["master: ..."]` 说明这一项是**没查**，不是"没有"（定律 12 的亲戚）。
    inherit_degraded: list[str] = field(default_factory=list)
    # ── 骨架几何（2026-09-21）：master 里带 xfrm 的占位符，空 = 没查到 ──
    master_geo: list[dict] = field(default_factory=list)

    @property
    def fonts_set_ratio(self) -> float | None:
        """显式设过字体的 run 占比。`None` = 一个 run 都没有（判不了，不猜 0）。"""
        if not self.runs_total:
            return None
        return self.runs_with_font / self.runs_total

    def theme_name(self) -> str:
        """这份 deck 用的是哪个**已知**主题；都不是则 `"other"`，读不到则 `""`。

        ⚠️ 与 `DECK_THEMES` **同源**比对（只比我们真正写进去的那几个色槽）。
        比的是"是不是我们认识的主题"，**不是**"好不好看" —— 一个自定义的好
        配色在这里也会被判 `"other"`，所以判据**不能单靠它**（见 `DeckAuditor`）。
        """
        if not self.theme_colors:
            return ""
        for name, th in DECK_THEMES.items():
            cols = th["colors"]
            if all(str(self.theme_colors.get(k, "")).upper() == str(v).upper()
                   for k, v in cols.items()):
                return name
        return "other"

    def to_dict(self) -> dict:
        return {
            "path": self.path, "slide_count": self.slide_count,
            "notes_slides": self.notes_slides,
            "slides_with_notes": self.slides_with_notes,
            "notes_ratio": round(self.notes_ratio, 4),
            "media_count": self.media_count,
            "slide_cx": self.slide_cx, "slide_cy": self.slide_cy,
            "min_font_pt": self.min_font_pt,
            "min_body_font_pt": self.min_body_font_pt,
            "slides": self.slides, "problems": self.problems,
            "aspect": round(self.aspect, 4) if self.aspect is not None else None,
            "is_widescreen": self.is_widescreen,
            "theme": self.theme_name(),
            "theme_colors": self.theme_colors,
            "theme_fonts": self.theme_fonts,
            "layout_kinds": self.layout_kinds,
            "layout_top_share": round(self.layout_top_share, 4),
            "pictures": self.pictures, "charts": self.charts, "tables": self.tables,
            "runs_total": self.runs_total, "runs_with_font": self.runs_with_font,
            "fonts_set_ratio": (round(self.fonts_set_ratio, 4)
                                if self.fonts_set_ratio is not None else None),
            "master_geo": self.master_geo,
            "overflow_measured": self.overflow_measured,
            "overflow_worst": (round(self.overflow_worst, 4)
                               if self.overflow_worst is not None else None),
            "inherit_degraded": list(self.inherit_degraded),
            "overflow_skipped": dict(self.overflow_skipped),
        }


# ══════════════════════ 直接量 pptx（标准库）══════════════════════
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _slide_no(name: str) -> int:
    m = re.search(r"slide(\d+)\.xml$", name)
    return int(m.group(1)) if m else 0


def _ph_type(node: ET.Element) -> str:
    """这个形状是不是占位符、是哪一种（title / ctrTitle / body / sldNum …）。"""
    for ph in node.iter():
        if _local(ph.tag) == "ph":
            return str(ph.get("type") or "body")
    return ""


def _shape_text(node: ET.Element) -> str:
    return "".join(t.text or "" for t in node.iter() if _local(t.tag) == "t").strip()


def _run_sizes(node: ET.Element) -> list[tuple[float, int]]:
    """形状里每个"有文字的 run"的显式字号（pt）与该 run 的字符数。

    `a:rPr/@sz` 是**百分之一磅**（1800 = 18 pt）。缺这个属性的 run 表示
    "继承母版" —— 我们记不下来，也就不记（不猜）。
    `a:endParaRPr` 是段末标记，它也算行高的一部分，但不含文字 ⇒ 不计入
    "正文体量"，只在全篇最小值里参考（见 `probe_pptx`）。
    """
    out: list[tuple[float, int]] = []
    for r in node.iter():
        if _local(r.tag) != "r":
            continue
        txt = "".join(t.text or "" for t in r.iter() if _local(t.tag) == "t")
        rpr = None
        for ch in r:
            if _local(ch.tag) == "rPr":
                rpr = ch
                break
        if rpr is None:
            continue
        sz = rpr.get("sz")
        if not sz:
            continue
        try:
            out.append((int(sz) / 100.0, len(txt.strip())))
        except ValueError:
            continue
    return out


# ══════════════════ 文字溢出估算（2026-09-21，纯标准库）══════════════════
# 为什么单独开一层：调研报告（`docs/ppt-skill-survey-20260921.md` §4 的 P0-2）说
# 「文字塞不下」是荣珂产出「丑」的最常见形态之一，而它**完全可复算** ——
# 形状的框（`a:ext`）与字号（`a:rPr/@sz`）都在 XML 里，不需要排版引擎。
#
# ⚠️ 这是**估算，不是排版**。真排版要字体度量、断行、字距、中西混排规则 —— 一概没有。
#    所以这层的定位是「抓**明显**塞不下的」：门槛必须放到估算误差之外
#    （见 `OVERFLOW_WARN_RATIO`），而且**只报 WARN**。
#    ★ 误报比漏报更伤（设计定律 10）：一份好稿子被说「文字溢出」，会教人整体无视体检。
_EMU_PER_PT = 12700
# ★ 这三个数是**行高与内边距的物理量**，`design.py` 也要用同一组值去算
#   「一个框至少要有多高才装得下一行字」（`design.min_box_h_px`）。
#   ⇒ **单源在 `design`**，这里取对象、不另抄字面量。
#   反过来（各写一个 1.2）就是 H 型：改了这里的估算器，资产的框高约束
#   会**继续按旧行距**成立，而没有任何东西会响。
_LINE_SPACING = design.LINE_SPACING
_INS_LR = design.TEXT_INSET_LR_EMU
_INS_TB = design.TEXT_INSET_TB_EMU
OVERFLOW_WARN_RATIO = 1.30   # 估高 / 可用高 超过它才报（给估算误差留足余量）


# ── 版心利用率（2026-09-27 · W5）─────────────────────────────
# 定义：利用率 = 该页所有「可见内容」的包围盒并集面积 / 页面可用版面面积。
# 「可见内容」的口径（每一档都是防误报，见 `_slide_content_rects` 注释）：
#   · 有文字的形状 —— 取**墨迹包围盒**（文字真正占的那块），不是它那个空框；
#   · 图片 / 图表 / 表格 —— 取整个框（它们是实心内容，框即内容）；
#   · 页脚装饰线、页码、页脚落款、页型标记、装饰短线 —— **排除**。
# ⚠️ 为什么文字用墨迹而不是形状的 `a:xfrm` 框：本库的 `compose_deck`
#   把正文框一律铺满内容带（472px 高），于是"3 条短句"和"8 条要点"
#   的**框一样大**。按框量，每一页都 ≈88% ⇒ 这条判据永远不会响，
#   而它要抓的正是「框铺满了、墨迹只占上面两行」那种空页。
#   对照数字见 `facts.slides[*]["util_box"]`（按框量的那份），写进执行报告 §五。
# 并集算法：矩形并集没有现成公式，用工单允许的**投影法近似** ——
#   把可用版面按每轴 50 格（= 2%）打格，标被任一矩形盖住的格，数格子。
_DENSITY_GRID = 50
# 严重度档位（工单拍板）。**全部 WARN，不 FAIL** —— 误报比漏报更伤（设计定律 10），
# 而且不是每一页都该塞满。阈值的四个档位未做本库实测校准，见执行报告 §五。
DENSITY_HIGH_MAX = 0.15      # < 15% ⇒ high
DENSITY_MEDIUM_MAX = 0.25    # 15~25% ⇒ medium
DENSITY_LOW_MAX = 0.40       # 25~40% ⇒ low；≥ 40% 通过（不报）
# ⚠️ **页型豁免**：`cover` 与 `section` **本就该留白**（一页一件事 / 巨型编号占位）。
#   参考样例第 2、5 页 section 实测墨迹利用率 10~15%，那是**刻意的空**，不是没写完；
#   对它们判"过低"是纯误报。其余页型（content / two-col / chart / closing）按上表判。
#   —— 这条豁免的理由必须留在代码里，删之前先回看 W5 工单 §一。
DENSITY_EXEMPT_KINDS = ("cover", "section")
# 排除的"非内容"形状名前缀（页脚线/页码/落款、页型标记、装饰短线）。
# 光靠"有没有文字"滤不掉**页码与页脚落款**（它们有字），所以按名/按占位符类型硬排。
_DENSITY_CHROME_PREFIX = ("rk-", "juanqing-footer")
_DENSITY_CHROME_PH = ("sldNum", "dt", "ftr")


def _text_width_emu(txt: str, pt: float) -> float:
    """粗略文本宽度（EMU）：**全角 1.0 em、ASCII 0.55 em**，其余按全角算。

    为什么不一律按全角：那会把**英文稿**的每行容量低估近一半，
    于是英文 deck 集体报「溢出」—— 纯误报。反之一律按 0.55 又会漏掉中文。
    """
    em = 0.0
    for ch in txt:
        em += 0.55 if ord(ch) < 0x2E80 else 1.0
    return em * pt * _EMU_PER_PT


def _sp_overflow(node: ET.Element, chain=None, part: int = 0,
                 stats: dict | None = None) -> tuple[float, float] | None:
    """估一个形状里「文字需要的高度」与「可用的高度」（EMU）。**走继承链**。

    ⚠️ `part` = **分片号**（`ppt/slides/slideN.xml` 里的 N），**不是演示顺序** ——
    `PhChain` 的 `layout_of` 是按**部件名**建的（见 `levels`）。
    **2026-09-22 修**：此前 `probe_pptx` 传的是页码（演示顺序）⇒ 路线甲的产物里
    两者不等（成员名是 `1,2,3,4,7,13,…`）⇒ **查到不存在的 layout、一路退回 master**
    ⇒ 字号/几何/自适应全部按错的层解析。实测同一个形状：真值 24pt 被算成 28pt，
    「估高比」从 2.32 虚高到 2.71。**两个方向都会错**（误报与漏报同时可能）。

    返回 `(need, avail)`；**判不了一律 `None`**（宁可不出数，不出假数 —— 定律 12）。
    判不了的四种情形，每一条都是防误报：

    ① **有效自适应**（`a:normAutofit` / `a:spAutoFit`）—— 作者已经告诉渲染器
       「字多了自己收」，实际不会溢出 ⇒ 报它就是误报。**这条必须走在最前面**，
       否则后面算得再准也是白算。
       ⚠️ 要看**继承后的有效值**：这份模板的 slide 层是空 `<a:bodyPr/>`，
       两个标记都只在 **master** 上 —— 只看形状自己那层，这条豁免等于不存在。
    ② 有效框量不到（三层都没有 `a:xfrm`）或尺寸非正 —— 不知道框多大。
    ③ 一个字号都推不出来（连 master 的 `txStyles` 也没有）—— 不知道字多大。
       ★ **与上一版的区别**：上一版要求「run 必须有显式 `@sz`」，
       而真实产物的 run **一个显式字号都没有** ⇒ 恒返回 `None`。
       现在沿链回退到 `txStyles`；**推不出来才算判不了**，不是"没显式写就判不了"。
    ④ 一个字符都没有 —— 不需要高度。

    `stats` 非空时，把"为什么没出数"分类计数写进去（键 `autofit` / `nogeom` /
    `nofont` / `empty` / `ok`）。★ **`overflow_measured == 0` 有四种互不相同的原因**，
    分不清就只能说"量不到"，那是**含混**，不是诚实。
    """
    def _bump(k: str) -> None:
        if stats is not None:
            stats[k] = stats.get(k, 0) + 1

    # ① 自适应豁免（**走链**取有效值）。
    if chain is not None:
        if chain.autofit(part, node):
            _bump("autofit")
            return None
    else:
        if _sp_autofit(node):
            _bump("autofit")
            return None

    # ② 框：走链。`chain is None` 时退化成"只看自己"（合成夹具/单元测试用）。
    geom = chain.geom(part, node) if chain is not None else _sp_geom(node)
    if not geom:
        _bump("nogeom")
        return None
    cx, cy = geom

    def _emu(attr: str, default: float) -> float:
        """内边距取**链上第一个声明了的**，都没有才用 DrawingML 默认值。"""
        raw = chain.inset(part, node, attr) if chain is not None else None
        if raw is None:
            bp = _sp_bodypr(node)
            raw = bp.get(attr) if bp is not None else None
        try:
            return float(raw) if raw else default
        except (TypeError, ValueError):
            return default

    avail_w = cx - _emu("lIns", _INS_LR) - _emu("rIns", _INS_LR)
    avail_h = cy - _emu("tIns", _INS_TB) - _emu("bIns", _INS_TB)
    if avail_w <= 0 or avail_h <= 0:
        _bump("nogeom")
        return None

    need = 0.0
    measured = False
    for para in node.iter():                    # 逐段估行数
        if _local(para.tag) != "p":
            continue
        ppr = _direct(para, "pPr")
        try:
            lvl = int(ppr.get("lvl")) + 1 if ppr is not None and ppr.get("lvl") else 1
        except (TypeError, ValueError):
            lvl = 1
        # 段级默认字号（`pPr/defRPr@sz`）—— 比 run 低一档、比形状 lstStyle 高一档
        dr = _direct(ppr, "defRPr")
        para_pt = _sz_pt(dr.get("sz")) if dr is not None else None
        chain_pt = chain.pt_of(part, node, lvl) if chain is not None else None

        seg_w = 0.0
        max_pt = 0.0
        for r in para.iter():
            if _local(r.tag) != "r":
                continue
            txt = "".join(t.text or "" for t in r.iter() if _local(t.tag) == "t")
            if not txt:
                continue
            pt = _sz_pt((_direct(r, "rPr")).get("sz")) if _direct(r, "rPr") is not None else None
            if not pt:
                pt = para_pt or chain_pt          # ★ 回退：段 → 形状/版式/master
            if not pt:
                continue                          # ③ 推不出来 ⇒ 这个 run 不算
            measured = True
            seg_w += _text_width_emu(txt, pt)
            max_pt = max(max_pt, pt)
        if seg_w <= 0 or max_pt <= 0:           # ④ 空段
            continue
        # **有文字的段至少占一行** —— 否则一堆短段会被算成一行而漏报
        need += max(1, math.ceil(seg_w / avail_w)) * max_pt * _EMU_PER_PT * _LINE_SPACING

    if need <= 0:
        _bump("empty")
        return None
    if not measured:
        _bump("nofont")
        return None
    _bump("ok")
    return need, avail_h


# ══════════════ 版心利用率（2026-09-27 · W5，纯标准库）══════════════
def _sp_off_ext(el) -> tuple[float, float, float, float] | None:
    """形状**自己的绝对框** `(x, y, cx, cy)`（EMU）。

    `sp`/`pic` 走 `spPr/a:xfrm`，`graphicFrame` 走 `p:xfrm`（off/ext 仍是 `a:`）。
    取元素内**第一个** `xfrm` —— 调用方只把它用在叶子形状上（`sp`/`pic`/
    `graphicFrame`），不进 `grpSp`，所以不会误抓子形状的变换。
    量不到（无 `xfrm` / 尺寸非正）返回 `None` —— 宁可不出数，不出假数（定律 12）。
    """
    for xf in el.iter():
        if _local(xf.tag) != "xfrm":
            continue
        off = ext = None
        for ch in xf:
            n = _local(ch.tag)
            if n == "off":
                off = ch
            elif n == "ext":
                ext = ch
        if off is None or ext is None:
            return None
        try:
            x = float(off.get("x") or 0)
            y = float(off.get("y") or 0)
            cx = float(ext.get("cx") or 0)
            cy = float(ext.get("cy") or 0)
        except (TypeError, ValueError):
            return None
        return (x, y, cx, cy) if cx > 0 and cy > 0 else None
    return None


def _shape_name(el) -> str:
    """`p:nvSpPr/p:nvPr/p:cNvPr/@name`（页脚线/页码/页型标记靠它认）。"""
    for e in el.iter():
        if _local(e.tag) == "cNvPr":
            return str(e.get("name") or "")
    return ""


def _slide_kind(root) -> str:
    """这一页的页型（读 `rk-kind-<kind>` 标记，与 `_kind_marks` 同一口径）。

    读不到返回 `""` —— 那是"**不知道页型**"，不能读成"content"，
    所以豁免逻辑遇到 `""` 一律**不豁免**（宁可照判，也不放过真·空页）。
    """
    for e in root.iter():
        if _local(e.tag) == "cNvPr":
            nm = str(e.get("name") or "")
            if nm.startswith("rk-kind-"):
                return nm[len("rk-kind-"):]
    return ""


def _sp_anchor(el) -> str:
    """文本在框内的**垂直锚点**（`bodyPr/@anchor`）：`t`/`ctr`/`b`，缺省 `t`。

    墨迹要摆在它真正出现的那一头，才量得准"下半页是不是空的"。
    `compose_deck` 对双栏/图表正文用 `middle`、对内容页正文用 `top`（见 `design`）。
    """
    bp = _sp_bodypr(el)
    a = bp.get("anchor") if bp is not None else None
    return str(a) if a else "t"


def _text_ink_rect(node, chain=None, part: int = 0):
    """一个**有文字的形状**里，文字真正占的那块 `(x, y, w, h)`（墨迹包围盒）。

    复用溢出估算器同一套原语（`_text_width_emu` / `_LINE_SPACING` / 内边距 /
    `chain.pt_of`），**不另发明一种字尺寸**（H 型）。

    量不出字号（连继承链都推不出）时**回退用整个框**：这条判据宁可**少报空页**，
    也不因估算器读不动而**误报**（误报比漏报更伤，设计定律 10）。
    """
    r = chain.rect(part, node) if chain is not None else _sp_off_ext(node)
    if not r:
        return None
    x, y, cx, cy = r

    def _emu(attr: str, default: float) -> float:
        raw = chain.inset(part, node, attr) if chain is not None else None
        if raw is None:
            bp = _sp_bodypr(node)
            raw = bp.get(attr) if bp is not None else None
        try:
            return float(raw) if raw else default
        except (TypeError, ValueError):
            return default

    lins = _emu("lIns", _INS_LR)
    rins = _emu("rIns", _INS_LR)
    tins = _emu("tIns", _INS_TB)
    bins = _emu("bIns", _INS_TB)
    avail_w = cx - lins - rins
    if avail_w <= 0:
        lins = rins = 0.0
        avail_w = cx

    need = 0.0
    maxw = 0.0
    any_text = False
    any_glyph = False                     # 有字，但可能一个 run 的字号都推不出
    for para in node.iter():                    # 逐段估行数（与 `_sp_overflow` 同口径）
        if _local(para.tag) != "p":
            continue
        ppr = _direct(para, "pPr")
        try:
            lvl = int(ppr.get("lvl")) + 1 if ppr is not None and ppr.get("lvl") else 1
        except (TypeError, ValueError):
            lvl = 1
        dr = _direct(ppr, "defRPr")
        para_pt = _sz_pt(dr.get("sz")) if dr is not None else None
        seg_w = 0.0
        max_pt = 0.0
        for run in para.iter():
            if _local(run.tag) != "r":
                continue
            txt = "".join(t.text or "" for t in run.iter() if _local(t.tag) == "t")
            if not txt:
                continue
            any_glyph = True
            pt = _sz_pt(_direct(run, "rPr").get("sz")) if _direct(run, "rPr") is not None else None
            if not pt:
                pt = para_pt or (chain.pt_of(part, node, lvl) if chain is not None else None)
            if not pt:
                continue
            seg_w += _text_width_emu(txt, pt)
            max_pt = max(max_pt, pt)
        if seg_w <= 0:
            continue                            # 空段
        any_text = True
        need += max(1, math.ceil(seg_w / avail_w)) * max_pt * EMU_PER_POINT * _LINE_SPACING
        maxw = max(maxw, min(avail_w, seg_w))

    if not any_text:
        # 有字、却连一个 run 的字号都推不出（继承链也读不动）⇒ 保守按**整个框**算。
        # ⚠️ 这里不能返回 None：那等于宣称"这个形状不存在"，一页会**看着更空**，
        #   估算器读不动就变成了对作者的误报（设计定律 10）。
        return (x, y, cx, cy) if any_glyph else None
    ink_h = min(cy, need + tins + bins)
    ink_w = min(cx, maxw + lins + rins)
    if ink_h <= 0 or ink_w <= 0:
        return (x, y, cx, cy)
    anchor = _sp_anchor(node)
    if anchor == "ctr":
        top = y + (cy - ink_h) / 2.0
    elif anchor == "b":
        top = y + (cy - ink_h)
    else:
        top = y
    return (x, top, ink_w, ink_h)


def _slide_content_rects(root, chain=None, part: int = 0):
    """一页的三样事实：`(墨迹矩形列表, 形状框列表, 正文段数)`。

    只走 `spTree` 的**顶层子形状**（`sp`/`pic`/`graphicFrame`）：
      · 无文字的 `sp`（装饰矩形）与页脚/页码/落款/页型标记 —— 排除，不算"可见内容"；
      · 有文字的 `sp` —— 墨迹进 `ink`、形状框进 `box`；
      · `pic`/`graphicFrame`（图/表/图表）—— 实心，墨迹=框=其形状框。
    ⚠️ **组合形状（`grpSp`）不递归**：子形状坐标在组的缩放空间里，直接取会算错
    位置。本库产物不用组合形状，遇到就整组跳过并如实少计（见执行报告 §五局限）。
    """
    cSld = root.find(f"{{{NS['p']}}}cSld")
    if cSld is None:
        return [], [], 0
    tree = cSld.find(f"{{{NS['p']}}}spTree")
    if tree is None:
        return [], [], 0
    ink: list[tuple[float, float, float, float]] = []
    box: list[tuple[float, float, float, float]] = []
    n_bullets = 0
    for ch in list(tree):
        t = _local(ch.tag)
        if t not in ("sp", "pic", "graphicFrame"):
            continue                            # grpSp 等：整组跳过（见 docstring）
        nm = _shape_name(ch)
        if nm.startswith(_DENSITY_CHROME_PREFIX) or _ph_type(ch) in _DENSITY_CHROME_PH:
            continue                            # 页脚/页码/落款/标记/装饰短线：不是内容
        if t == "sp":
            if not _shape_text(ch):
                continue                        # 无文字的形状：装饰，不算可见内容
            r = _text_ink_rect(ch, chain, part)
            b = chain.rect(part, ch) if chain is not None else _sp_off_ext(ch)
            if r is not None:
                ink.append(r)
            # 正文段数：非空 `a:p` 计数，给修法文案用（"只有 1 条要点"）。
            # ⚠️ 标题（`title`/`ctrTitle`）**不算要点** —— 它是论点、不是支撑点，
            #    数进去会让每条修法都多报一条"要点"。
            if _ph_type(ch) not in ("title", "ctrTitle"):
                for para in ch.iter():
                    if _local(para.tag) == "p" and _shape_text_para(para):
                        n_bullets += 1
        else:                                   # pic / graphicFrame（图 / 表 / 图表）
            b = _sp_off_ext(ch)
            if b is not None:
                ink.append(b)
        if b is not None:
            box.append(b)
    return ink, box, n_bullets


def _shape_text_para(para) -> bool:
    """一个段落里有没有非空文字（用于数"几条要点"）。"""
    return any((t.text or "").strip() for t in para.iter() if _local(t.tag) == "t")


def _usable_region(slide_cx: int, slide_cy: int):
    """可用版面 `(x0, y0, x1, y1)`（EMU）：页面减去边距带。

    ⚠️ 数字**全部来自 `design.py`**（`PAD_PX` / `BANDS_PX` / `CANVAS_PX`），
    按页面尺寸成比例缩放 —— 不另设第二套边距（两套边距就是 H 型）。
    纵向取到内容带底（`BANDS_PX["B"][1]`），把页脚带 C 排除在分母外。
    """
    if not slide_cx or not slide_cy:
        return None
    W, H = float(slide_cx), float(slide_cy)
    cw, chh = float(design.CANVAS_PX[0]), float(design.CANVAS_PX[1])
    pad = design.PAD_PX / cw * W
    foot_top = design.BANDS_PX["B"][1] / chh * H
    if foot_top <= 0 or W - 2 * pad <= 0:
        return None
    return (pad, 0.0, W - pad, foot_top)


def _util_from_rects(rects, usable) -> float | None:
    """矩形并集 / 可用版面（**投影法近似**：每轴 50 格 = 2%，数被盖住的格）。

    返回 0..1；`usable` 为 None 或没量到任何矩形时返回 None（"没查"，不是"0%"）。
    并集而非面积求和：重叠只数一次（各形状面积之和会把叠放重复计）。
    """
    if not usable or not rects:
        return None
    ux0, uy0, ux1, uy1 = usable
    uw = ux1 - ux0
    uh = uy1 - uy0
    if uw <= 0 or uh <= 0:
        return None
    n = _DENSITY_GRID
    cells: set[int] = set()
    for (x, y, w, h) in rects:
        x0 = max(x, ux0)
        y0 = max(y, uy0)
        x1 = min(x + w, ux1)
        y1 = min(y + h, uy1)
        if x1 <= x0 or y1 <= y0:
            continue
        c0 = int((x0 - ux0) / uw * n)
        c1 = int((x1 - ux0) / uw * n)
        r0 = int((y0 - uy0) / uh * n)
        r1 = int((y1 - uy0) / uh * n)
        for rr in range(max(0, r0), min(r1, n - 1) + 1):
            base = rr * n
            for cc in range(max(0, c0), min(c1, n - 1) + 1):
                cells.add(base + cc)
    return len(cells) / (n * n) if cells else 0.0


def _density_severity(util: float) -> str:
    """把利用率映射到档位（工单的四档，未做本库校准 —— 见执行报告 §五）。"""
    if util < DENSITY_HIGH_MAX:
        return "high"
    if util < DENSITY_MEDIUM_MAX:
        return "medium"
    if util < DENSITY_LOW_MAX:
        return "low"
    return ""


def _density_fix(kind: str, bullets: int) -> str:
    """一条**可执行**的修法（工单 §一 要求每条 finding 都带一句怎么改）。

    只说动作、不堆形容词。拿实测的正文段数 `bullets` 把话说实
    （"本页只有 1 条要点"比"内容偏少"更能让人动起手来）。
    """
    if kind == "two-col":
        return ("左右栏都补到 2–3 条、条数尽量相等；"
                "若只有一侧有料，改用 content 页，别硬撑双栏")
    if kind == "chart":
        return "把图放大吃满左侧主视觉位，右侧读数要点补到 2–3 条"
    if kind == "closing":
        return "下半页补上：局限与计划各 2–3 条（收束页停在下一步，不是停在半句）"
    if kind == "content":
        return (f"本页只有 {bullets} 条要点，按参考样例补到 3 条"
                f"（每条一行），或把散点合成一张图；再不然改用 section 页型"
                if bullets else "本页没有正文要点，补到 3 条或改用 section 页型")
    return f"补到该页型应有的要点密度（当前约 {bullets} 条），或改用 section 页型"


# ── 文字-背景对比度（2026-09-27 · W8）─────────────────────────────
# 现有各门全部只读 XML 的**结构事实**，没有任何一维在问"这东西看得见吗"。
# 这一维补的是 WCAG 2.1 AA 的文字/背景对比度。
# ★ **纯 XML 的天花板**（本维的诚实底线）：拿不到照片的亮度 ⇒ "文字压在 `pic` 上"
#   一律记 `contrast_unmeasurable` 并**跳过**。绝不因为算不出来就默认它合格 ——
#   "没查"被读成"通过"是本项目最贵的那类缺陷（M 型）。
# 阈值是 WCAG 2.1 AA 的原文数字，**不是本库校准出来的**，也不许调 —— 调它等于换标准。
CONTRAST_BODY_AA = 4.5        # 正文门槛：< 18pt（且不满足 ≥14pt 粗体）
CONTRAST_LARGE_AA = 3.0       # 大字门槛：≥ 18pt，或 ≥ 14pt 且粗体
CONTRAST_LARGE_PT = 18.0
CONTRAST_BOLD_LARGE_PT = 14.0
# 背景候选形状至少要盖住文字框这么多，才算"字压在它上面"。
# 为什么要有这一条：一根几 px 高的装饰短线也会和文字框**擦边重叠**，按"重叠面积
# 最大"挑它当背景 ⇒ 深色短线配深色字 = 假警（定律 10：误报会教人无视整份报告）。
_CONTRAST_MIN_COVER = 1.0 / 3.0
# `schemeClr` 的槽位别名（ECMA-376 固定映射，不是猜色）。
_SCHEME_ALIAS = {"tx1": "dk1", "bg1": "lt1", "tx2": "dk2", "bg2": "lt2"}
# 这两类填充**没有单一色值**可言，用哨兵记下来（它们不是颜色，别拿去算对比度）。
CONTRAST_FILL_IMAGE = "__image__"
CONTRAST_FILL_GRADIENT = "grad"


def _hex6(val) -> str | None:
    """只认 6 位十六进制色值。别的（`windowText` / `phClr` / 空）⇒ `None` = 量不到。"""
    s = str(val or "").strip().upper()
    return s if re.fullmatch(r"[0-9A-F]{6}", s) else None


def _theme_slot_hex(theme: dict, slot: str) -> str | None:
    """主题槽位 ⇒ 真实 RGB。`bg1`/`tx1` 这类别名先翻译成 `lt1`/`dk1` 再查。

    ⚠️ 查不到、或查到的是 `read_theme_colors` 原样吐出来的**名字**
    （默认 Office 主题的 `dk1` 是 `sysClr val="windowText"` ⇒ 返回 `"WINDOWTEXT"`）
    ⇒ 一律 `None`。这是"量不到"，不是"白色"。
    """
    return _hex6(theme.get(_SCHEME_ALIAS.get(slot, slot)))


def _color_of(node, theme: dict) -> str | None:
    """颜色节点（`srgbClr`/`schemeClr`/`sysClr`）⇒ `RRGGBB`；解不出 ⇒ `None`。

    三种"解不出"都**不猜**：① 带颜色变换（`a:shade`/`a:tint`/`a:lumMod`… 那是
    在 base 上再合成一步的色，拿 base 冒充实测值就是假数）；② `schemeClr` 指向
    主题里没有的槽、或指向 `phClr`（模板占位色，脱离上下文根本不知道是什么）；
    ③ `sysClr` 没写 `lastClr`。
    """
    if node is None:
        return None
    if len(list(node)):                       # 有子节点 ⇒ 带变换，合成色算不出来
        return None
    t = _local(node.tag)
    if t == "srgbClr":
        return _hex6(node.get("val"))
    if t == "sysClr":
        return _hex6(node.get("lastClr"))
    if t == "schemeClr":
        return _theme_slot_hex(theme, str(node.get("val") or ""))
    return None


def _fill_color_of(el, theme: dict) -> str | None:
    """形状的**填充** ⇒ `RRGGBB` / `"grad"` / `None`（没填、填充方式不是纯色、读不出）。

    只看 `spPr` 下第一个 `solidFill`/`gradFill`/`noFill`/`pattFill`。`noFill` 返回
    `None` 的意思是"这一层不是背景"，调用方据此往下一层找（页面底色）。
    """
    sppr = _direct(el, "spPr")
    if sppr is None:
        return None
    for ch in sppr:
        n = _local(ch.tag)
        if n == "solidFill":
            for c in ch:
                got = _color_of(c, theme)
                if got:
                    return got
            return None                       # 有色节点在，但解不出 ⇒ 量不到
        if n == "gradFill":
            return CONTRAST_FILL_GRADIENT
        if n in ("noFill", "pattFill"):
            return None
    return None


def _srgb_lin(v: float) -> float:
    v = v / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _rel_luminance(hex6: str) -> float:
    """WCAG 相对亮度 `L = 0.2126R' + 0.7152G' + 0.0722B'`（分量已线性化）。"""
    return (0.2126 * _srgb_lin(int(hex6[0:2], 16))
            + 0.7152 * _srgb_lin(int(hex6[2:4], 16))
            + 0.0722 * _srgb_lin(int(hex6[4:6], 16)))


def _contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.1 对比度（`1.0 .. 21.0`）。两个入参都是 `RRGGBB`。"""
    l1, l2 = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _rect_overlap(a, b) -> float:
    """两个 `(x, y, cx, cy)` 的重叠面积（0 = 不相交）。"""
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _contrast_threshold(pt: float, bold: bool) -> tuple[float, bool]:
    """(门槛, 是否算大字)。WCAG 2.1 AA：≥18pt，或 ≥14pt 且粗体 ⇒ 大字 3:1，否则正文 4.5:1。"""
    big = pt >= CONTRAST_LARGE_PT or (pt >= CONTRAST_BOLD_LARGE_PT and bold)
    return (CONTRAST_LARGE_AA if big else CONTRAST_BODY_AA), big


def _contrast_backdrop(trect, own_fill, fills, page_bg):
    """文字真正的底色 ⇒ `(RRGGBB 或 None, 它是什么)`；`None` = **量不到**（不是"合格"）。

    三层回落，与本库"就近能查到什么算什么"的口径一致：
      ① 同页**非文字**实心形状里与文字框重叠面积最大、且至少盖住
         `_CONTRAST_MIN_COVER` 的那个（擦边的装饰短线不算背景）；
      ② 文字形状**自己**的 `solidFill` —— 深色圆角矩形里写白字是排版日常，
         拿页面底色去比它 = 假警（定律 10），所以自己涂的色必须先算；
      ③ 都没有 ⇒ 页面底色（主题 `bg1` → `lt1`）。
    图片 / 渐变 / 解不出的 schemeClr 一律 `None`：**纯 XML 到这就是天花板**。
    """
    best = None
    if trect and trect[2] * trect[3] > 0:
        area = trect[2] * trect[3]
        for b in fills or []:
            br = b.get("rect")
            if not br:
                continue
            ov = _rect_overlap(trect, br)
            if ov <= 0 or ov < _CONTRAST_MIN_COVER * area:
                continue                            # 只擦到边 ⇒ 那不是"字压在上面"
            if best is None or ov > best[0]:
                best = (ov, b)
    if best is not None:
        b = best[1]
        nm = str(b.get("name") or "未命名形状")
        f = b.get("fill")
        if f == CONTRAST_FILL_IMAGE:
            return None, f"压在图片「{nm}」上（照片亮度量不到）"
        if f == CONTRAST_FILL_GRADIENT:
            return None, f"压在渐变「{nm}」上（渐变没有单一底色）"
        if not f:
            return None, f"下层形状「{nm}」的填充色解不出"
        return f, f"下层填充「{nm}」"
    if own_fill == CONTRAST_FILL_IMAGE:
        return None, "形状自身是图片"
    if own_fill == CONTRAST_FILL_GRADIENT:
        return None, "形状自身是渐变填充"
    if own_fill:
        return own_fill, "形状自身的填充"
    if not trect:
        return None, "形状没有几何（`a:xfrm` 读不到），无法确定它压在什么上面"
    if page_bg:
        return page_bg, "页面底色（主题 `bg1`）"
    return None, "既没有下层填充，主题页面底色（`bg1`）也解不出"


def _slide_color_facts(root, chain=None, part: int = 0, theme=None,
                       with_elements: bool = False):
    """一页里「谁的字压在谁上面」的两样事实 —— 对比度门（W8）的输入。

    `with_elements=True`（W9 可读性修正的子进程用）：每个 run 的 dict 里**追加**
    一个 `"_el"` 键 = 该 run 的 XML 元素（stdlib ET 或 lxml 都原样带着），
    改色时直接在那棵树上动 `rPr/solidFill` 的色节点，不必另写一套定位。
    默认 `False` ⇒ 事实层（进 JSON 的那份）一个键都没多。

    返回 `(text_shapes, fill_shapes)`，两者都是**只走 `spTree` 顶层子形状**
    （与 `_slide_content_rects` 同一口径：`grpSp` 不递归，组合空间的坐标会算错）：

      · `text_shapes` = 有文字的非装饰 `sp`
        `{name, ph, rect, runs:[{text, pt, rgb, bold}]}`；
      · `fill_shapes` = **没有文字**的实心形状（色块 / 装饰矩形 / 图片）
        `{name, rect, fill}`，`fill` 是 `RRGGBB` / `"grad"` / `"__image__"`。

    ★ 页脚 / 页码 / 落款 / 页型标记**不进 `text_shapes`** —— 它们是本库**刻意弱化**
      的装饰（`juanqing-footer-label` 的 12pt 米棕字与绢底实测 3.63:1，
      按正文门槛必报），而 density 门早就是同一口径（`_DENSITY_CHROME_*`），
      这里**复用那一份**，不另设第二套"什么算装饰"。
    ★ 字号 / 字色**继承母版**的 run 记成 `None`：不猜（定律 12），判据层据此报"量不到"。
    """
    theme = theme or {}
    out_t: list[dict] = []
    out_f: list[dict] = []
    cSld = root.find(f"{{{NS['p']}}}cSld")
    if cSld is None:
        return out_t, out_f
    tree = cSld.find(f"{{{NS['p']}}}spTree")
    if tree is None:
        return out_t, out_f
    for ch in list(tree):
        t = _local(ch.tag)
        if t not in ("sp", "pic"):
            continue                            # graphicFrame（表 / 图表）不当事：见 docstring
        rect = chain.rect(part, ch) if chain is not None else _sp_off_ext(ch)
        nm = _shape_name(ch)
        if t == "sp" and _shape_text(ch):
            if nm.startswith(_DENSITY_CHROME_PREFIX) or _ph_type(ch) in _DENSITY_CHROME_PH:
                continue
            runs = []
            for r in ch.iter():
                if _local(r.tag) != "r":
                    continue
                txt = "".join(x.text or "" for x in r.iter() if _local(x.tag) == "t")
                if not txt.strip():
                    continue
                rpr = None
                for rc in r:
                    if _local(rc.tag) == "rPr":
                        rpr = rc
                        break
                sf = _direct(rpr, "solidFill") if rpr is not None else None
                rgb = None
                if sf is not None:
                    for c in sf:
                        rgb = _color_of(c, theme)
                        if rgb:
                            break
                rd = {
                    "text": txt.strip(),
                    "pt": (_sz_pt(rpr.get("sz")) if rpr is not None else None),
                    "bold": (str(rpr.get("b") or "0") in ("1", "true")
                             if rpr is not None else False),
                    "rgb": rgb,
                }
                if with_elements:
                    rd["_el"] = r
                runs.append(rd)
            if not runs:
                continue                        # 只剩空白 run
            out_t.append({"name": nm, "ph": _ph_type(ch), "rect": rect, "runs": runs,
                          "own_fill": _fill_color_of(ch, theme)})
            continue
        if rect is None:
            continue
        fill = CONTRAST_FILL_IMAGE if t == "pic" else _fill_color_of(ch, theme)
        if not fill:
            continue                            # 没填充 / 读不出 ⇒ 它不是任何人的背景
        out_f.append({"name": nm, "rect": rect, "fill": fill})
    return out_t, out_f


# ── 出稿后的可读性修正（2026-09-27 · W9）────────────────────────────
# W8 的门在 zjut 模板第一稿上抓到真缺陷（p4 两个按钮 1.64:1：硬编码填充
# 4F9FDB × 字色 accent1=4472C4）。模板是用户的资产不许动 ⇒ 在**出稿之后**
# 对成品做一次修正：**只改字色**，几何/字号/位置/填充一律不碰
# （"品牌装饰一字不动"的纪律）。
# ★ 判定**全部复用 W8 的检测层**（`_slide_color_facts` / `_contrast_ratio` /
#   `_contrast_threshold` / `_contrast_backdrop`）—— 在这里再写一套
#   "什么算不足、该配什么色"就是两处真相（H 型，本项目明令禁止）。
# ★ 选色按 **W9 工单勘误**：不按亮度分档，而是**在候选集里取对比度最大的**
#   且要求 ≥ 门槛。分档规则的洞主控已实测：底色 4F9FDB 亮度 0.3157 判"深底"
#   ⇒ 选白字，而白字在它上面只有 2.87:1，**修了还是不合格**。
# ★ **量不到的不猜**（压在图片/渐变上、字色字号继承母版）⇒ 原样不动 +
#   如实报；候选全不达标 ⇒ 原样不动 + 如实报"修不了"。
CONTRAST_FIX_CANDIDATES = ("1A1A1A", "FFFFFF")


def _contrast_fix_candidates(theme: dict) -> list[str]:
    """候选色板：两个基准色 + 本主题 `dk1`/`lt1`（**解得出十六进制才进**）。

    默认 Office 主题的 `dk1` 是 `sysClr val="windowText"` ⇒ `_theme_slot_hex`
    判"解不出"（返回 None），**不拿一个好看的数顶上**（定律 12）。
    """
    out = list(CONTRAST_FIX_CANDIDATES)
    for slot in ("dk1", "lt1"):
        h = _theme_slot_hex(theme or {}, slot)
        if h and h not in out:
            out.append(h)
    return out


def _plan_contrast_fixes(facts: DeckFacts) -> dict:
    """扫一份 deck，产出「对比度不足且**量得到**」的文字 run 改色计划（纯读，无副作用）。

    返回 `{"fixes": [...], "unfixable": [...], "unmeasurable": [...]}`。
    每条都带 `slide`（演示页码）与 `part`（分片号）；`fixes`/`unfixable` 里的
    `(shape_idx, run_idx)` 是 `_slide_color_facts` 那两个列表里的**下标** ——
    子进程用同一份检测层重跑一遍就能按同一坐标找到 run 元素，
    不依赖形状名在页内唯一（模板里恰恰常见重名）。
    """
    theme = facts.theme_colors or {}
    page_bg = _theme_slot_hex(theme, "bg1")
    cands = _contrast_fix_candidates(theme)
    fixes: list[dict] = []
    unfixable: list[dict] = []
    unmeasurable: list[dict] = []
    for s in facts.slides:
        no = s["slide"]
        fills = s.get("fill_shapes") or []
        for si, t in enumerate(s.get("text_shapes") or []):
            nm = str(t.get("name") or "未命名形状")
            bg, src = _contrast_backdrop(t.get("rect"), t.get("own_fill"),
                                         fills, page_bg)
            if bg is None:
                # 图片 / 渐变 / 解不出的填充 ⇒ **不猜、不改**，只如实记
                unmeasurable.append({"slide": no, "shape": nm, "why": src})
                continue
            for ri, run in enumerate(t.get("runs") or []):
                pt, fg = run.get("pt"), run.get("rgb")
                if fg is None or pt is None:
                    continue        # 继承母版读不出 ⇒ 门那边已报"量不到"，这里同样不猜
                thr, _big = _contrast_threshold(pt, bool(run.get("bold")))
                ratio = _contrast_ratio(fg, bg)
                if ratio + 1e-9 >= thr:
                    continue        # 合格 ⇒ 一个字都不许碰（负控纪律）
                best = max(cands, key=lambda c: _contrast_ratio(c, bg))
                r_best = _contrast_ratio(best, bg)
                item = {"slide": no, "part": s["part"], "shape": nm,
                        "shape_idx": si, "run_idx": ri,
                        "fg": fg, "bg": bg, "pt": pt, "threshold": thr,
                        "before": round(ratio, 2)}
                if r_best + 1e-9 < thr:
                    item["best_candidate"] = best
                    item["best_ratio"] = round(r_best, 2)
                    unfixable.append(item)      # 修不了 ⇒ 说出口，不许静默
                else:
                    item["new"] = best
                    item["after"] = round(r_best, 2)
                    fixes.append(item)
    return {"fixes": fixes, "unfixable": unfixable, "unmeasurable": unmeasurable}


def _recolor_run(r_el, hex6: str) -> bool:
    """只改一个 `a:r` 的**字色**：把它 `rPr/solidFill` 里的色节点整体换成 `srgbClr`。

    同时适用于 stdlib `ElementTree` 与 `lxml`：新元素用 `makeelement` 造
    （混库元素会炸）。改色节点之外的任何东西（`latin`/`sym` 字体、`hlink`…）
    一概不碰。以下情形返回 `False` 并且**不动树**：
    没有 `rPr`、没有 `solidFill`（那是继承来的字色 —— 不归本函数管）、
    或 `solidFill` 里一个色节点都没有（读不出 ⇒ 不猜）。
    """
    rpr = _direct(r_el, "rPr")
    if rpr is None:
        return False
    sf = _direct(rpr, "solidFill")
    if sf is None or not list(sf):
        return False
    new = sf.makeelement("{%s}srgbClr" % NS["a"], {"val": hex6})
    for ch in list(sf):
        sf.remove(ch)
    sf.append(new)
    return True


#: 子进程脚本：改色要在**装了 python-pptx 的解释器**里用 lxml 动那棵树
#: （stdlib ET 重新序列化会把 `p14:` 这类前缀改写掉 ⇒ `mc:Ignorable` 会指到
#:   不存在的声明上，PowerPoint 可能弹修复。lxml 原样保留前缀，零副作用）。
_CONTRAST_FIX_SCRIPT = r'''
import json, sys, zipfile
res = {"applied": [], "errors": []}
try:
    pl = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    sys.path.insert(0, pl["root"])
    from rongke import deck as D
    from pptx import Presentation
    with zipfile.ZipFile(pl["out"]) as zf:
        chain = D.PhChain(zf)
    theme = D.read_theme_colors(pl["out"])
    prs = Presentation(pl["out"])
    by_slide = {}
    for fx in pl["fixes"]:
        by_slide.setdefault(int(fx["slide"]), []).append(fx)
    for snum in sorted(by_slide):
        fxs = by_slide[snum]
        slide = prs.slides[snum - 1]
        tshp, _f = D._slide_color_facts(slide._element, chain,
                                        int(fxs[0]["part"]), theme,
                                        with_elements=True)
        for fx in fxs:
            try:
                run_el = tshp[int(fx["shape_idx"])]["runs"][int(fx["run_idx"])]["_el"]
            except (IndexError, KeyError, TypeError):
                res["errors"].append(
                    "p%d 形状#%d run#%d 在重跑的检测层里对不上号（页/形状变了？）"
                    % (fx["slide"], fx["shape_idx"], fx["run_idx"]))
                continue
            if not D._recolor_run(run_el, fx["new"]):
                res["errors"].append("p%d「%s」run#%d 改色失败（rPr/solidFill 不在）"
                                     % (fx["slide"], fx["shape"], fx["run_idx"]))
                continue
            res["applied"].append({"slide": int(fx["slide"]),
                                   "shape_idx": int(fx["shape_idx"]),
                                   "run_idx": int(fx["run_idx"])})
    if res["applied"]:
        prs.save(pl["out"])
except Exception as e:
    import traceback
    res["errors"].append("修正子进程崩了：%s: %s" % (type(e).__name__, e))
    res["errors"].append(traceback.format_exc()[-600:])
sys.stdout.write(json.dumps(res, ensure_ascii=False))
'''


def apply_readability_fix(pptx_path: str | Path, *, python_exe: str | None = None,
                          timeout: float = 300.0) -> dict:
    """出稿后的可读性修正（W9 工单 §二·任务 1）。**只在有缺陷时才碰文件。**

    流程（三步都走 W8 那一份检测层，不另立判据）：
      ① `probe_pptx` + `_plan_contrast_fixes` 找"量得到且不足"的 run；
      ② 一条都不欠 ⇒ **直接返回，文件一个字节不动**（负控：合格稿零 diff）；
      ③ 有 ⇒ 子进程（python-pptx/lxml）只改字色，保存后**从磁盘重开复测**：
         逐条读回真实比值，`verified` 只认读回来的数，不认子进程自述。

    返回 `{"fixed": [...], "unfixable": [...], "unmeasurable": [...],
           "problems": [...], "touched": bool}`。
    """
    p = Path(pptx_path)
    facts = probe_pptx(p)
    plan = _plan_contrast_fixes(facts)
    out: dict = {"fixed": [], "unfixable": plan["unfixable"],
                 "unmeasurable": plan["unmeasurable"], "problems": [],
                 "touched": False}
    if not plan["fixes"]:
        return out
    if not python_exe:
        out["problems"].append("找不到装 python-pptx 的解释器 ⇒ 可读性修正**没跑**。")
        return out
    payload = json.dumps({"out": str(p.resolve()), "root": str(ROOT),
                          "fixes": plan["fixes"]}, ensure_ascii=False)
    try:
        r = subprocess.run([python_exe, "-c", _CONTRAST_FIX_SCRIPT],
                           input=payload.encode("utf-8"),
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        out["problems"].append(f"修正子进程超时（{timeout:.0f}s），文件**可能只改了一半**"
                               f"⇒ 已停止复测，请重新读一遍这份 deck。")
        return out
    except Exception as e:
        out["problems"].append(f"修正子进程起不来（{python_exe}）：{type(e).__name__}: {e}")
        return out
    try:
        data = json.loads((r.stdout or b"").decode("utf-8", "replace") or "{}")
    except Exception:
        out["problems"].append("修正子进程的返回不是 JSON（rc=%d）⇒ 修正结果**没查**。"
                               % int(r.returncode))
        return out
    out["problems"] += list(data.get("errors") or [])
    applied = {(int(a["slide"]), int(a["shape_idx"]), int(a["run_idx"]))
               for a in data.get("applied") or []}
    out["touched"] = bool(applied)

    # ★ 复测：**从磁盘重开**，用同一份检测层量改回来的真实比值
    #   （子进程自报"改了"不算数 —— 见 §18.C 那条"空转脚本"的血债）。
    facts2 = probe_pptx(p)
    by_slide = {s["slide"]: s for s in facts2.slides}
    page_bg2 = _theme_slot_hex(facts2.theme_colors or {}, "bg1")
    for fx in plan["fixes"]:
        v = {"slide": fx["slide"], "shape": fx["shape"], "old": fx["fg"],
             "new": fx["new"], "bg": fx["bg"], "threshold": fx["threshold"],
             "before": fx["before"], "after": None, "why": ""}
        key = (fx["slide"], fx["shape_idx"], fx["run_idx"])
        if key not in applied:
            v["why"] = "子进程报告没改到这一处"
            out["fixed"].append(v)
            continue
        s2 = by_slide.get(fx["slide"]) or {}
        try:
            t2 = (s2.get("text_shapes") or [])[fx["shape_idx"]]
            run2 = (t2.get("runs") or [])[fx["run_idx"]]
        except IndexError:
            v["why"] = "复测时形状/段落下标对不上（页结构变了？）"
            out["fixed"].append(v)
            continue
        bg2, _src = _contrast_backdrop(t2.get("rect"), t2.get("own_fill"),
                                       s2.get("fill_shapes") or [], page_bg2)
        rgb2, pt2 = run2.get("rgb"), run2.get("pt")
        if bg2 is None or rgb2 is None or pt2 is None:
            v["why"] = "复测读不回可比值（底色/字色/字号有一项量不到）"
        else:
            after = _contrast_ratio(rgb2, bg2)
            v["after"] = round(after, 2)
            thr2, _b = _contrast_threshold(pt2, bool(run2.get("bold")))
            if after + 1e-9 >= thr2 and rgb2 == fx["new"]:
                v["verified"] = True
            else:
                v["why"] = "复测仍不达标（读回 %s:%s）" % (rgb2, round(after, 2))
        v.setdefault("verified", False)
        out["fixed"].append(v)
    return out


def readability_fix_lines(fix: dict) -> tuple[list[str], list[str]]:
    """把 `apply_readability_fix` 的结果翻成出稿报告的行。

    返回 `(checks, warnings)`：改成功且复测达标的进 `checks`（复测段），
    其余三类 —— 复测不过、候选修不了、量不到 —— 全部进 `warnings`
    （**必须说出口**，且一条都不许被读成"过了"）。
    """
    checks: list[str] = []
    warns: list[str] = []
    groups: dict[tuple, dict] = {}
    for v in fix.get("fixed") or []:
        if v.get("verified"):
            gk = (v["slide"], v["old"], v["new"], bool(v.get("verified")))
        else:
            gk = (v["slide"], v["old"], v.get("new") or "?", False)
        g = groups.setdefault(gk, {"names": [], "before": [], "after": [],
                                   "why": []})
        g["names"].append(str(v["shape"]))
        g["before"].append(v.get("before"))
        if v.get("after") is not None:
            g["after"].append(v["after"])
        if v.get("why"):
            g["why"].append(str(v["why"]))
    for (no, old, new, ok), g in sorted(groups.items()):
        names = "、".join(dict.fromkeys(g["names"]))
        b = min(x for x in g["before"] if x is not None)
        aft = ("%.2f:1" % min(g["after"])) if g["after"] else "量不到"
        line = (f"· 可读性修正 {len(g['names'])} 处：p{no}「{names}」"
                f"字色 {old} → {new}（{b:.2f}:1 → {aft}）")
        if ok:
            checks.append(line)
        else:
            warns.append(line + f" ⚠️ **复测未达标**（{('；'.join(dict.fromkeys(g['why'])))[:120]}）"
                                " —— 这一处**没修好**，不要当成交付理由")
    uf = fix.get("unfixable") or []
    if uf:
        warns.append(
            f"· 可读性：{len(uf)} 处对比度不足但候选集里**没有一个色**能到达标"
            f" ⇒ 未改动（改底色越界了）："
            + "；".join(f"p{x['slide']}「{x['shape']}」（门槛{x['threshold']}，"
                        f"最优候选也只有{x['best_ratio']}:1）"
                        for x in uf[:6])
            + ("……" if len(uf) > 6 else ""))
    um = fix.get("unmeasurable") or []
    if um:
        warns.append(
            f"· 可读性：{len(um)} 处文字对比度**量不到**（压在图片/渐变上等）"
            f" ⇒ 未改动、也**没猜**一个颜色上去，请人工看渲染图确认："
            + "；".join(f"p{x['slide']}「{x['shape']}」{x['why']}" for x in um[:6])
            + ("……" if len(um) > 6 else ""))
    return checks, warns


# ── 任务 2（W9）：可选的渲染像素校验 —— **默认必须关闭** ──────────────
# 纯 XML 的天花板：字压在照片上时拿不到那张照片的亮度（W8 把这类记成
# `contrast_unmeasurable` 并如实说"没查"）。这一维把天花板顶开一条缝：
# 真渲染 ⇒ 在文字矩形内**按分布口径**估前景/背景。
# ★ 为什么默认关闭（主控实测）：本机走 COM 渲染一份 5 页 deck = **15.1 秒**，
#   而现有各门是秒级 ⇒ 设成默认会让每次体检慢一个数量级，
#   而绝大多数稿子并不压照片。关闭时 `contrast` 门的输出**逐字不变**
#   （`deck_test` 28.4 把这条钉死）。
# ★ 分布口径（工单 §二·任务2 的"关键算法警告"，主控踩过；**W10 又修了一次**）：
#   ① **不许**取 p5..p95 两个极端 —— 那会把明亮天空当背景、深色字当前景，
#      量出 5.23:1"达标"而肉眼明明看不清。
#   ② 也**不许**靠"哪一端更极端"判谁是文字。W9 的写法（文字=离中位数最远的
#      5% 尾）在**深底浅字**上必然塌：那份 deck 96% 的像素都在同一档里，
#      "最暗 5%"取到的**正是背景本身** ⇒ text_luma == bg_luma ⇒ 报出 1.00:1，
#      而 1.00:1 的定义是前景与背景完全同色 —— 那样的区域根本没有字，
#      渲染图上明明有字 ⇒ 物理上不可能，必然是假警（W10 §一）。
#   ⇒ W10 的口径：**判"谁是文字"靠"哪一撮是少数"，不靠哪端更极端。**
#      背景 = 占比最大的那一档；文字 = 离主体档最远、且占比 < 20% 的第二峰。
#      **度量与肉眼冲突时，先怀疑度量。**

#: 渲染子进程：在装了 win32com 的解释器里走 `deck_preview.render_pngs`
#: （那四条纪律 —— 绝对路径 / 不抢用户 PPT / 读回验尺寸 / 失败要响 ——
#:   的现成实现，**不在这里重写**）。
_RENDER_DECK_SCRIPT = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from deck_preview import render_pngs
pngs, err = render_pngs(sys.argv[2], sys.argv[3])
print(json.dumps({"pngs": [str(x) for x in pngs], "err": err}, ensure_ascii=False))
'''


def _render_deck_pngs(pptx_path, python_exe, outdir, timeout: float = 120.0):
    """渲染每页为 PNG（`slide-01.png` 起，演示顺序）。返回 `(pngs, err)`。

    err 非空 = **没跑成** —— 调用方必须把它说出口，不许当成通过（M 型）。
    这是任务 2 降级测试（28.5）的**换点**：门通过模块全局查名调它，
    `swapped(D, "_render_deck_pngs", …)` 就能模拟渲染失败。
    """
    cands: list[str] = []
    for c in (python_exe, find_deck_python(), find_notes_python()):
        if c and c not in cands:
            cands.append(c)
    py = next((c for c in cands if _exe_ok(c, "import win32com.client")), None)
    if py is None:
        return [], ("找不到能渲染的解释器（win32com 不可用，试过 "
                    f"{len(cands)} 个候选）")
    try:
        r = subprocess.run(
            [py, "-c", _RENDER_DECK_SCRIPT, str(ROOT),
             str(Path(pptx_path).resolve()), str(Path(outdir).resolve())],
            capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], f"渲染超时（{timeout:.0f}s）"
    except Exception as e:
        return [], f"渲染子进程起不来（{py}）：{type(e).__name__}: {e}"
    try:
        d = json.loads((r.stdout or b"").decode("utf-8", "replace") or "{}")
    except Exception:
        return [], "渲染子进程返回的不是 JSON"
    err = str(d.get("err") or "")
    pngs = [Path(x) for x in d.get("pngs") or []]
    if err:
        return [], err
    if not pngs:
        return [], "渲染返回 0 页且没报错 —— 按没跑成处理"
    return pngs, ""


def _png_grey(data: bytes):
    """PNG bytes ⇒ `(w, h, 逐像素亮度表)`。**纯标准库**（struct+zlib）。

    只认 8 bit、非隔行、颜色类型 0/2/3/4/6（PowerPoint COM 导出的 PNG 就是
    这一类）；其余一律抛 `ValueError` ⇒ 调用方如实报"这一页量不了"，
    **不猜**。亮度口径与主控实测脚本一致：Rec.601 `L = 0.299R+0.587G+0.114B`。
    """
    import struct
    import zlib
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    i = 8
    ihdr = None
    plte = b""
    idat = bytearray()
    while i + 8 <= len(data):
        ln = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + ln]
        if typ == b"IHDR":
            ihdr = body
        elif typ == b"PLTE":
            plte = body
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        i += 12 + ln
    if ihdr is None:
        raise ValueError("PNG 没有 IHDR")
    w, h, depth, ctype, _c, _f, inter = struct.unpack(">IIBBBBB", ihdr[:13])
    if depth != 8 or inter != 0:
        raise ValueError(f"只支持 8 bit 非隔行（读到 depth={depth} interlace={inter}）")
    chan = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if chan is None:
        raise ValueError(f"不支持的颜色类型 {ctype}")
    raw = zlib.decompress(bytes(idat))
    stride = w * chan
    if len(raw) < (stride + 1) * h:
        raise ValueError("IDAT 解出来的长度与画布对不上")
    grey: list[int] = []
    prev = bytearray(stride)
    ap = grey.append
    for y in range(h):
        base = y * (stride + 1)
        f = raw[base]
        line = bytearray(raw[base + 1:base + 1 + stride])
        if f == 1:
            for x in range(chan, stride):
                line[x] = (line[x] + line[x - chan]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                a = line[x - chan] if x >= chan else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - chan] if x >= chan else 0
                b = prev[x]
                c = prev[x - chan] if x >= chan else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        elif f != 0:
            raise ValueError(f"未知 PNG 行过滤器 {f}")
        if ctype == 3:
            for x in range(w):
                o = line[x] * 3
                ap((299 * plte[o] + 587 * plte[o + 1] + 114 * plte[o + 2]) // 1000)
        elif ctype in (0, 4):
            for x in range(w):
                ap(line[x * chan])
        else:
            for x in range(w):
                o = x * chan
                ap((299 * line[o] + 587 * line[o + 1] + 114 * line[o + 2]) // 1000)
        prev = line
    return w, h, grey


#: ── W10 像素取样三口径（改这三个数就等于改判据，测试按名字钉着）──────────
#: 背景主体档的占比下限：低于此 ⇒ 这块区域**不是**「字压一种底色」（照片、
#: 渐变、双色拼贴都长这样），拿"少数撮"去当文字只会编出数 ⇒ 记量不到。
PIXEL_BG_MIN_SHARE = 0.5
#: 文字那一撮的占比上限：**一行字在它的框里只占个位数百分比**。
#: 超过 20% 的那一撮是第二块背景，不是笔画（`0.20` 也是"两撮找不出"的判据）。
PIXEL_TEXT_MAX_SHARE = 0.20
#: 一撮"文字"的最小体量（占比 + 绝对像素数）：一两个噪点/镜面高光不算一撮。
#: ⚠️ 这一条是**本工单没要求、我自己加的地板**，见 W10 报告 §六 的自认局限。
PIXEL_TEXT_MIN_SHARE = 0.002
PIXEL_TEXT_MIN_PX = 24
#: 文字档的取样分位：把那一撮**由背景往极端**排序后取此分位 = 笔画芯。
#: ⚠️ 取"这一撮的中位数"会被抗锯齿斜坡拖向背景（实测能把 11:1 稀释成 2:1 假警），
#:    所以取靠极端的一端；95% 位而不是最值 ⇒ 一两个孤立亮点顶不起整条判据。
PIXEL_TEXT_TAIL_Q = 0.95
#: 一档的灰阶宽度（16 级一档的亮度直方图，与 `overlap` 同一把尺）。
PIXEL_HIST_BINS = 16
PIXEL_HIST_BIN_W = 256 // PIXEL_HIST_BINS


def _pixel_stats_unmeasurable(reason: str, n: int, mode: int, share: float) -> dict:
    """「量不到」的返回值：**不带 ratio 键**（结构性地报不出一个数字）。

    所有失败分支都必须从这里走 —— 工单 §一 第③条：找不出两撮就记量不到，
    **绝不报一个数字**。想要新加一条失败分支，就得多写一个这个函数的调用，
    而它给不出比值，所以"顺手报个数"这条路是堵死的。
    """
    return {"measurable": False, "reason": reason, "n_px": n,
            "bg_bucket": mode, "bg_share": round(share, 4)}


def _pixel_contrast_stats(vals: list[int]):
    """分布口径的像素对比度 ⇒ dict（**W10 口径：谁少谁是文字**）。

    `None` = 像素太少（< 400），分布无从谈起。否则一定带 `measurable`：

      True  ⇒ 另带 `ratio / text_luma / text_side / bg_luma / overlap /
              bg_share / text_share`；
      False ⇒ 只有 `reason`（+ 取样元信息），**没有 `ratio`** —— 报不出数字。

    算法（工单 §一 修法四条，逐条对应）：
      ① **占比最大的那一档 = 背景**（16 级一档的直方图取众数档，不看明暗端）；
         该档占比 < `PIXEL_BG_MIN_SHARE` ⇒ 这块区域压根不是"字压底色" ⇒ 量不到。
      ② **文字 = 离主体档最远、且占比 < `PIXEL_TEXT_MAX_SHARE` 的那一撮**。
         找法：只看主体档之外（至少差一档）的**局部峰**档（自己的像素数不小于
         左右邻档）—— 抗锯齿斜坡单调衰减、成不了峰，所以斜坡不会被当成笔画;
         两侧都合格时，取线性亮度离背景**更远**的那侧（⇒ 深底浅字与浅底深字
         **同一条代码路径**，方向不是特例）。
      ③ 两侧都找不出合格的第二峰 ⇒ **量不到**，绝不报数字。
      ④ 硬断言：算出来 `text_luma == bg_luma` ⇒ 判**取样失败**（1.00:1 的定义
         是前景与背景完全同色，那种区域没有字；报出来必然是假的）。
    """
    n = len(vals)
    if n < 400:
        return None
    hist: dict[int, int] = {}
    for v in vals:
        hist[v // PIXEL_HIST_BIN_W] = hist.get(v // PIXEL_HIST_BIN_W, 0) + 1
    # ① 背景 = 占比最大的那一档（平票偏向靠背景中位那一侧，保证可复现）
    mid_v = sorted(vals)[n // 2]
    mode = max(range(PIXEL_HIST_BINS),
               key=lambda b: (hist.get(b, 0), -abs(b - mid_v // PIXEL_HIST_BIN_W)))
    bg_share = hist.get(mode, 0) / n
    if bg_share < PIXEL_BG_MIN_SHARE:
        return _pixel_stats_unmeasurable(
            f"占比最大的亮度档（{mode * PIXEL_HIST_BIN_W}-"
            f"{mode * PIXEL_HIST_BIN_W + PIXEL_HIST_BIN_W - 1}）只占 "
            f"{bg_share:.0%} < {PIXEL_BG_MIN_SHARE:.0%} ⇒ 这块区域不是"
            f"「字压在同一种底色上」（照片/渐变/双色拼贴都长这样），"
            f"没有'背景'可比", n, mode, bg_share)
    bg_luma = sorted(v for v in vals if v // PIXEL_HIST_BIN_W == mode)[
        hist[mode] // 2]
    lm_bg = _srgb_lin(bg_luma)
    #: 候选文字峰：档索引 → (占比, 由背景往极端排序后的取样位)
    cands: list[tuple[float, int, str, float]] = []
    for b, c in sorted(hist.items()):
        if b == mode or abs(b - mode) < 1:
            continue                          # 与主体同一档/没有档 ⇒ 不是"第二撮"
        share = c / n
        if share >= PIXEL_TEXT_MAX_SHARE:     # 太大 ⇒ 是第二块背景，不是笔画
            continue
        if c < PIXEL_TEXT_MIN_PX or share < PIXEL_TEXT_MIN_SHARE:
            continue                          # 一两个噪点不成一撮
        if c < hist.get(b - 1, 0) or c < hist.get(b + 1, 0):
            continue                          # 不是局部峰 ⇒ 只是主体档的肩部/斜坡
        px = sorted(v for v in vals if v // PIXEL_HIST_BIN_W == b)
        # 由**背景**往**极端**排序：浅底深字要反着排，否则取样取到斜坡那头
        if b < mode:
            px = list(reversed(px))
        take = min(len(px) - 1, int(PIXEL_TEXT_TAIL_Q * (len(px) - 1) + 0.5))
        cands.append((b, px[take], "暗侧" if b < mode else "亮侧", share))
    if not cands:
        return _pixel_stats_unmeasurable(
            f"主体档（{mode * PIXEL_HIST_BIN_W}-"
            f"{mode * PIXEL_HIST_BIN_W + PIXEL_HIST_BIN_W - 1}，占 {bg_share:.0%}）"
            f"之外找不出「占比 < {PIXEL_TEXT_MAX_SHARE:.0%} 的局部峰」那一撮"
            f" ⇒ 分布上分不出谁是文字（要么是一片纯色、要么底色本身就在动）",
            n, mode, bg_share)
    # ② 两侧都合格时取**离背景更远**的那撮（线性亮度比，不看档位远近）
    b_fg, fg, side, fg_share = max(cands, key=lambda c: abs(_srgb_lin(c[1]) - lm_bg))
    # ④ 硬断言：前景与背景同亮度 ⇒ 取样失败，这个数**不许**往外报
    if fg == bg_luma or abs(_srgb_lin(fg) - lm_bg) < 1e-9:
        return _pixel_stats_unmeasurable(
            f"取样失败：文字档亮度 {fg} 与背景档亮度 {bg_luma} **相等**"
            f" ⇒ 1.00:1 的定义是前景与背景完全同色，那种区域根本没有字"
            f"（{side}峰档 {b_fg * PIXEL_HIST_BIN_W}-"
            f"{b_fg * PIXEL_HIST_BIN_W + PIXEL_HIST_BIN_W - 1}）",
            n, mode, bg_share)
    hi, lo = max(lm_bg, _srgb_lin(fg)), min(lm_bg, _srgb_lin(fg))
    return {"measurable": True,
            "ratio": round((hi + 0.05) / (lo + 0.05), 2),
            "text_luma": fg, "text_side": f"少数撮·{side}峰", "text_bucket": b_fg,
            "text_share": round(fg_share, 4),
            "bg_luma": bg_luma, "bg_bucket": mode, "bg_share": round(bg_share, 4),
            "overlap": abs(b_fg - mode) <= 1, "n_px": n}


def _rect_px_crop(grey, w, h, rect, slide_cx, slide_cy):
    """形状矩形（EMU）→ 渲染图像素区域里的亮度列表；越界裁剪、空区域 ⇒ None。"""
    if not rect or not slide_cx or not slide_cy:
        return None
    x0 = max(0, int(rect[0] * w / slide_cx))
    x1 = min(w, int((rect[0] + rect[2]) * w / slide_cx))
    y0 = max(0, int(rect[1] * h / slide_cy))
    y1 = min(h, int((rect[1] + rect[3]) * h / slide_cy))
    if x1 <= x0 or y1 <= y0:
        return None
    return [grey[y * w + x] for y in range(y0, y1) for x in range(x0, x1)]


def _contrast_pixel_pass(facts: DeckFacts, targets: list[dict], pptx_path,
                         python_exe, timeout: float = 120.0):
    """对"XML 量不到"的文字形状跑一遍渲染像素校验。返回 `(warns, infos, summary)`。

    全程**不阻断**（与 W8 的口径一致），且**渲染没跑成 ⇒ 一条 SKIP 说清楚**，
    那几处仍然是"没查"，绝不读成通过。
    """
    import tempfile
    summary: dict = {"attempted": len(targets), "rendered": False,
                     "err": "", "measured": 0, "unmeasurable": 0, "per_shape": []}
    if not targets:
        summary["err"] = "没有'量不到'的文字形状，无需渲染"
        summary["rendered"] = True
        return [], [], summary
    if not pptx_path:
        summary["err"] = "调用方没给 deck 路径"
        return [], [Finding(
            "SKIP", "contrast_pixel_unavailable",
            f"像素校验**没跑成**（{summary['err']}）—— {len(targets)} 处"
            f"「量不到」的文字仍然**没查**，不要当成通过。")], summary
    try:
        with tempfile.TemporaryDirectory(prefix="rk-deep-") as td:
            pngs, err = _render_deck_pngs(pptx_path, python_exe, td,
                                          timeout=min(120.0, float(timeout)))
            if err:
                summary["err"] = err
                return [], [Finding(
                    "SKIP", "contrast_pixel_unavailable",
                    f"像素校验**没跑成**（{err}）—— {len(targets)} 处纯 XML"
                    f"「量不到」的文字仍然**没查**，不要当成通过。",
                    [f"reason={err[:80]}"])], summary
            summary["rendered"] = True
            warns: list[Finding] = []
            infos: list[Finding] = []
            cache: dict[int, tuple] = {}
            for tg in targets:
                no = int(tg["slide"])
                nm = str(tg["shape"])
                pi = no - 1
                if pi >= len(pngs):
                    infos.append(Finding("INFO", "contrast_pixel_unavailable",
                                         f"第 {no} 页「{nm}」：渲染页数不够（{len(pngs)} 张），"
                                         f"像素校验**没查到这一处**", [f"slide={no}", f"shape={nm}"]))
                    continue
                try:
                    if pi not in cache:
                        with open(pngs[pi], "rb") as fh:
                            cache[pi] = _png_grey(fh.read())
                    w, h, grey = cache[pi]
                    vals = _rect_px_crop(grey, w, h, tg.get("rect"),
                                         facts.slide_cx, facts.slide_cy)
                    st = _pixel_contrast_stats(vals or [])
                except Exception as e:
                    infos.append(Finding(
                        "INFO", "contrast_pixel_unavailable",
                        f"第 {no} 页「{nm}」：渲染图像素读不出（{type(e).__name__}: {e}）"
                        f" ⇒ 这一处**没查**", [f"slide={no}", f"shape={nm}"]))
                    continue
                if st is None:
                    infos.append(Finding(
                        "INFO", "contrast_pixel_unavailable",
                        f"第 {no} 页「{nm}」：矩形内像素太少"
                        f"（{len(vals or [])} < 400），分布口径分不出来 ⇒ 这一处**没查**",
                        [f"slide={no}", f"shape={nm}"]))
                    continue
                # ★★ W10 §一第③④条：**量不到就记量不到，绝不报一个数字**。
                #   两道都拦：一是取样函数自己判不合格（measurable=False，它压根
                #   不带 ratio 键）；二是**这里独立核一遍** text_luma == bg_luma ——
                #   就算哪天有人把取样改回"取最暗 5%"、重新造出 1.00:1，
                #   这条也拦在出口，不许它变成 WARN。
                un = ("" if st.get("measurable") else
                      str(st.get("reason") or "分布里找不出'文字'那一撮"))
                if not un and st["text_luma"] == st["bg_luma"]:
                    un = (f"取样失败：文字亮度 {st['text_luma']} 与背景亮度 "
                          f"{st['bg_luma']} 相等 ⇒ 1.00:1 是'前景与背景完全同色'，"
                          f"而 1.00:1 的区域根本没有字")
                if un:
                    summary["unmeasurable"] += 1
                    infos.append(Finding(
                        "INFO", "contrast_pixel_unmeasurable",
                        f"第 {no} 页「{nm}」：像素口径**量不到**（{un}）"
                        f" ⇒ 这一处**没查**，不报比值（渲染图上看得见这一处，"
                        f"要确认得人工看一眼）",
                        [f"slide={no}", f"shape={nm}", "method=pixel"]))
                    continue
                summary["measured"] += 1
                summary["per_shape"].append({"slide": no, "shape": nm, **st})
                ratio = st["ratio"]
                pts = [p for p, _b in tg["runs"] if p is not None]
                if not pts:
                    infos.append(Finding(
                        "INFO", "contrast_pixel_unmeasurable",
                        f"第 {no} 页「{nm}」：像素侧量到估计比值 {ratio}:1，但该形状所有 run "
                        f"**字号都继承母版** ⇒ 选不出该用哪档门槛，**仍算没查**"
                        f"（比值只报数、不判合格）",
                        [f"slide={no}", f"shape={nm}", f"ratio={ratio}"]))
                    continue
                worst_thr = max(_contrast_threshold(p, b)[0] for p, b in tg["runs"]
                                if p is not None)
                thr_note = "/".join(sorted({str(_contrast_threshold(p, b)[0])
                                            for p, b in tg["runs"] if p is not None}))
                if ratio + 1e-9 < worst_thr:
                    warns.append(Finding(
                        "WARN", "text_low_contrast_pixel",
                        f"第 {no} 页（渲染像素口径）形状「{nm}」文字对比度约 "
                        f"{ratio}:1，低于门槛 {thr_note}（{st['text_side']} "
                        f"亮度{st['text_luma']}、占像素 {st['text_share']:.0%} vs "
                        f"背景主体档亮度{st['bg_luma']}、占 {st['bg_share']:.0%}）"
                        f" —— 纯 XML 量不到它的背景（多为照片），这一处**只有渲染能看见**。"
                        + ("；直方图上文字与背景主体同档：**看得见但读不出**"
                           if st["overlap"] else ""),
                        [f"slide={no}", f"shape={nm}", f"ratio={ratio}",
                         f"threshold={worst_thr}", "method=pixel"]))
                elif st["overlap"]:
                    infos.append(Finding(
                        "INFO", "contrast_pixel_overlap",
                        f"第 {no} 页「{nm}」：像素比值 {ratio}:1 在门槛上，但直方图上"
                        f"文字那一撮与背景主体**落在同一亮度区间**"
                        f"（文字{st['text_side']}={st['text_luma']} vs 众数档 {st['bg_luma']//16*16}"
                        f"±16）⇒「看得见但读不出」，建议人工看一眼",
                        [f"slide={no}", f"shape={nm}", f"ratio={ratio}"]))
            return warns, infos, summary
    except Exception as e:                       # 像素校验是加分项：天塌下来也不许
        summary["err"] = f"像素校验自身出错（{type(e).__name__}: {e}）"  # 崩掉整份体检
        return [], [Finding(
            "SKIP", "contrast_pixel_unavailable",
            f"像素校验**没跑成**（{summary['err']}）—— 「量不到」的那几处仍然**没查**。"
        )], summary


def _master_placeholder_geo(zf: zipfile.ZipFile) -> list[dict]:
    """读 `slideMaster1.xml` 里**带 `a:xfrm` 的占位符**的框（EMU）。

    为什么非要读 master：占位符在 slide / layout **两层通常根本没有 `a:xfrm`**
    —— 位置尺寸一路继承到 master。实测那份 13 页成片：slide 层 **0** 个 xfrm、
    layout 层 **0** 个、master 层 title/body 各一个。
    ⇒ **只读 slide 会得出"量不到"，而病根就在 master。**

    ⚠️ 我第一版做的正是「只读 slide」：溢出估算一份都没量到，先去怀疑算法，
       打印出 XML 才发现是**查错了层**（数据不在那一层）。
       ★ 通用问法：**「我断言'量不到'之前，先证明我查的是那个容器吗？」**
    """
    out: list[dict] = []
    try:
        root = ET.fromstring(zf.read("ppt/slideMasters/slideMaster1.xml"))
    except Exception:
        return out
    for node in root.iter():
        if _local(node.tag) != "sp":
            continue
        ph = None
        for t in node.iter():
            if _local(t.tag) == "ph":
                ph = t
                break
        if ph is None:
            continue
        # 几何只在 `spPr/a:xfrm` 下找（全树找 `xfrm` 会被别的容器骗）
        xfrm = None
        for t in node.iter():
            if _local(t.tag) == "xfrm":
                xfrm = t
                break
        if xfrm is None:
            continue
        off = ext = None
        for ch in xfrm:
            n = _local(ch.tag)
            if n == "off":
                off = ch
            elif n == "ext":
                ext = ch
        if off is None or ext is None:
            continue
        try:
            out.append({
                "ph": str(ph.get("type") or "body"),
                "idx": str(ph.get("idx") or ""),
                "x": int(off.get("x") or 0),
                "cx": int(ext.get("cx") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


# ══════════════════ 占位符继承链（2026-09-21，纯标准库）══════════════════
# 为什么非要有这一层：**占位符在 slide 层几乎什么都不声明。**
# 实测 `runs/20260921-124950/deck.pptx`（13 页）逐页解剖：
#   · slide 层 `a:xfrm` —— **0 个**；显式字号 `a:rPr/@sz` —— **0 个**；
#     `a:bodyPr` 在，但是**空的** `<a:bodyPr/>`
#   · 几何在 **master**（title 与 body 都是 cx=8229600）
#   · 字号在 **master 的 `p:txStyles`**（titleStyle lvl1 **44pt**、bodyStyle lvl1 **32pt**）
#   · `a:normAutofit` 也在 master
# ⇒ 任何"只读 slide 一层"的估算都必然得出「量不到」。
#
# ⚠️ **我是先怀疑算法、后打印 XML 才发现查错了层。**
#    ★ 通用问法：**我断言「量不到」之前，先证明我查的是那个容器吗？**
#
# 回退顺序（ECMA-376 的占位符继承语义）：
#   几何 / 内边距 / 自适应：slide → 所属 layout → master（逐项取第一个**声明了的**）
#   字号：run `rPr@sz` → 段 `pPr/defRPr@sz` → 形状 `lstStyle/lvlN` →
#         layout 同名占位符 `lstStyle` → master 同名占位符 `lstStyle` →
#         master `p:txStyles/{titleStyle|bodyStyle|otherStyle}/lvlN`
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

# 占位符类型 → master `p:txStyles` 里该走哪套默认字号。
# ⚠️ `subTitle` 归 **bodyStyle**（不是 otherStyle）：Office 主题里副标题跟着正文层级走。
_TXSTYLE_OF = {"title": "titleStyle", "ctrTitle": "titleStyle",
               "body": "bodyStyle", "subTitle": "bodyStyle"}
_INSET_KEYS = ("lIns", "rIns", "tIns", "bIns")


def _sz_pt(raw) -> float | None:
    """`a:rPr/@sz` 是**百分之一磅**（1800 = 18pt）⇒ 换算成 pt。非法值返回 None。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v / 100.0 if v > 0 else None


def _direct(el, local_name: str):
    """**直接子元素**里第一个叫这个名字的，不递归。"""
    if el is None:
        return None
    for ch in el:
        if _local(ch.tag) == local_name:
            return ch
    return None


def _lvl_sizes(container) -> dict[int, float]:
    """容器下逐级的 `defRPr@sz` ⇒ `{层级: pt}`（层级从 1 起）。"""
    out: dict[int, float] = {}
    if container is None:
        return out
    for lvlp in container:
        m = re.match(r"lvl(\d+)pPr$", _local(lvlp.tag))
        if not m:
            continue
        pt = _sz_pt(_direct(lvlp, "defRPr").get("sz")) if _direct(lvlp, "defRPr") is not None else None
        if pt:
            out[int(m.group(1))] = pt
    return out


def _ph_key(el) -> tuple[str, str] | None:
    """占位符身份 `(type, idx)`；非占位符返回 None。

    ⚠️ `type` 缺省是 **`body`**（ECMA：省略 type 即正文）—— 这条口径与
    `remap_skeleton` / `_master_placeholder_geo` 三处必须一致。
    """
    ph = _direct(_direct(el, "nvSpPr"), "nvPr")
    ph = _direct(ph, "ph")
    if ph is None:
        return None
    return (str(ph.get("type") or "body"), str(ph.get("idx") or ""))


def _sp_geom(el) -> tuple[float, float] | None:
    """形状自己的框 `(cx, cy)`，**只认 `spPr/a:xfrm/a:ext`**。"""
    ext = _direct(_direct(_direct(el, "spPr"), "xfrm"), "ext")
    if ext is None:
        return None
    try:
        cx, cy = float(ext.get("cx") or 0), float(ext.get("cy") or 0)
    except (TypeError, ValueError):
        return None
    return (cx, cy) if cx > 0 and cy > 0 else None


def _sp_bodypr(el):
    """`txBody/a:bodyPr`。⚠️ 不递归 —— `a:bodyPr` 别处也有。"""
    return _direct(_direct(el, "txBody"), "bodyPr")


def _sp_autofit(el) -> str | None:
    bp = _sp_bodypr(el)
    if bp is None:
        return None
    for ch in bp:
        n = _local(ch.tag)
        if n in ("normAutofit", "spAutoFit"):
            return n
    return None


class PhChain:
    """一份 deck 的占位符继承链。**构建一次、逐页复用**。

    读不出来不抛异常：判不了就让调用方走 `SKIP`（定律 12：宁可不出数，不出假数）。
    """

    def __init__(self, zf: zipfile.ZipFile):
        self.master: dict[tuple[str, str], object] = {}
        self.master_tx: dict[str, dict[int, float]] = {}
        self.layout_of: dict[int, str] = {}       # 页号 → layout 部件名
        self.layouts: dict[str, dict[tuple[str, str], object]] = {}
        self.degraded: list[str] = []             # 哪一步没读出来（如实记，不静默）
        self._load(zf)

    # ── 构建 ──
    def _load(self, zf: zipfile.ZipFile) -> None:
        names = set(zf.namelist())
        mname = "ppt/slideMasters/slideMaster1.xml"
        if mname in names:
            try:
                mroot = ET.fromstring(zf.read(mname))
                for sp in mroot.iter():
                    if _local(sp.tag) != "sp":
                        continue
                    k = _ph_key(sp)
                    if k and k not in self.master:
                        self.master[k] = sp
                for ts in mroot.iter():
                    if _local(ts.tag) != "txStyles":
                        continue
                    for sty in ts:
                        self.master_tx[_local(sty.tag)] = _lvl_sizes(sty)
            except Exception as e:
                self.degraded.append("master: %s: %s" % (type(e).__name__, e))
        else:
            self.degraded.append("包里没有 slideMaster1.xml")

        # 页号 → layout 部件名（走 slide 自己的 rels，不走猜测）
        for n in sorted(names):
            m = re.match(r"ppt/slides/_rels/slide(\d+)\.xml\.rels$", n)
            if not m:
                continue
            try:
                rr = ET.fromstring(zf.read(n))
            except Exception:
                continue
            for rel in rr:
                tgt = str(rel.get("Target") or "")
                if "slideLayout" in tgt:
                    self.layout_of[int(m.group(1))] = tgt.rsplit("/", 1)[-1]
                    break

        for lay in sorted(x for x in names
                          if re.match(r"ppt/slideLayouts/slideLayout\d+\.xml$", x)):
            short = lay.rsplit("/", 1)[-1]
            if short not in set(self.layout_of.values()):
                continue                       # 只在用得到的版式上花时间
            try:
                lroot = ET.fromstring(zf.read(lay))
            except Exception as e:
                self.degraded.append("%s: %s: %s" % (short, type(e).__name__, e))
                continue
            d: dict[tuple[str, str], object] = {}
            for sp in lroot.iter():
                if _local(sp.tag) != "sp":
                    continue
                k = _ph_key(sp)
                if k and k not in d:
                    d[k] = sp
            self.layouts[short] = d

    # ── 查询 ──
    def levels(self, no: int, el) -> list:
        """这个形状要查的三层，**就近在前**：slide 自己 → layout → master。"""
        out = [el]
        lay = self.layouts.get(self.layout_of.get(no, ""), {})
        k = _ph_key(el)
        if k:
            if k in lay:
                out.append(lay[k])
            if k in self.master:
                out.append(self.master[k])
        return out

    def geom(self, no: int, el) -> tuple[float, float] | None:
        """有效框：逐层取第一个**声明了** `a:xfrm` 的。"""
        for lv in self.levels(no, el):
            g = _sp_geom(lv)
            if g:
                return g
        return None

    def rect(self, no: int, el) -> tuple[float, float, float, float] | None:
        """有效**绝对**框 `(x, y, cx, cy)`：逐层取第一个声明了完整 `xfrm` 的。

        与 `geom` 的区别：`geom` 只回尺寸（溢出估算用不到位置），
        利用率要把矩形摆到页面上求并集，**必须有 off**。占位符在 slide/layout
        层常常什么都没有 ⇒ 一路继承到 master 才拿到坐标（见 `_master_placeholder_geo`）。
        """
        for lv in self.levels(no, el):
            g = _sp_off_ext(lv)
            if g:
                return g
        return None

    def inset(self, no: int, el, key: str) -> str | None:
        """有效内边距：逐层取第一个**声明了**该属性的。"""
        for lv in self.levels(no, el):
            bp = _sp_bodypr(lv)
            if bp is not None and bp.get(key):
                return str(bp.get(key))
        return None

    def autofit(self, no: int, el) -> str | None:
        """有效自适应标记：逐层取第一个**声明了** `normAutofit`/`spAutoFit` 的。

        ⚠️ **不是"就近覆盖"**：slide 上那个空 `<a:bodyPr/>` **不会**取消
        master 的 `normAutofit` —— 缺省子元素照样继承。把这条读成"就近覆盖"
        会让豁免失效，于是**这份模板上每一个正文框都会被误报溢出**。
        """
        for lv in self.levels(no, el):
            af = _sp_autofit(lv)
            if af:
                return af
        return None

    def pt_of(self, no: int, el, lvl: int) -> float | None:
        """某一层的**有效字号**：形状 lstStyle → layout lstStyle → master lstStyle
        → master `txStyles` 里对应的那套（按占位符类型选）。"""
        layers = self.levels(no, el)
        for lv in layers:
            pt = _lvl_sizes(_direct(_direct(lv, "txBody"), "lstStyle")).get(lvl)
            if pt:
                return pt
        k = _ph_key(el)
        style = _TXSTYLE_OF.get(k[0] if k else "body", "otherStyle")
        return self.master_tx.get(style, {}).get(lvl)


# ══════════════════════ 视觉主题（**单源**）════════════════════════
# 2026-09-21 新增。存在的理由（实测，不是审美偏好）：
#   ① 上游 `create_presentation` 起手就是 python-pptx 的**默认空白模板** ——
#      4:3、Calibri、白底。实测一份 13 页 deck：字体/字号/颜色**全部"继承"**，
#      11 页是同一个 "Title and Content"，全篇 0 张图。
#   ② 看着唯一能救场的 `apply_professional_design(operation="theme")` 是**空壳**：
#      它自述 `{"success": true, "slides_affected": 13}`，而实测 theme1.xml 的
#      md5 **一个字节都没变**、尺寸没变、part 数没变。⇒ 「以为改好了」这条路
#      本身是假的（**假成功** / phantom success）。
#   ③ 契约与门里**没有一个字**提视觉 ⇒ 目标函数里没有"好看"，产出就永远不好看。
# ⇒ 治法不是再加一句纪律（纪律没有判据兜底就是空转），而是：
#    给一个**真改 XML** 的工具（`style_deck`）＋ 一条**为视觉承重**的判据。
#
# ⚠️ 这一份定义**同时**喂给两处：`style_deck` 写进 theme1.xml、
#    `DeckAuditor` 拿它当合格线。**必须同源** —— 分开放就是 H 型（改一处忘一处）。
DECK_THEMES: dict[str, dict] = {
    "juanqing": {
        "label": "绢青陈墨",
        "note": "宋版书：绢底墨字，朱砂点睛；青为次，褐为佐",
        "colors": {
            "dk1": "23201C",      # 墨 —— 正文与深底
            "lt1": "F4EFE4",      # 绢 —— 页面底色
            "dk2": "47606B",      # 青 —— 次级文字
            "lt2": "E8DFCC",      # 浅绢 —— 区块底
            "accent1": "B03A2E",  # 朱砂 —— 强调
            "accent2": "8C7A5B",  # 褐
            "accent3": "6E7F6B",  # 灰绿
            "accent4": "C89B3C",  # 暖黄
            "accent5": "34515E",  # 深青
            "accent6": "7A5C4B",  # 檀
            "hlink": "B03A2E",
            "folHlink": "8C7A5B",
        },
        # 字体挑**本机必定存在**的中文字体：宋体标题（宋版书的骨）、
        # 微软雅黑正文（投影上笔画不糊）。装"思源宋体"更好看，但一旦目标机
        # 没有就打回原形 —— 缺字渲染成豆腐块，比丑更难救。
        "fonts": {
            "major_ea": "宋体", "major_latin": "SimSun",
            "minor_ea": "微软雅黑", "minor_latin": "Microsoft YaHei",
        },
    },
    # ── 学校品牌主题（浙江工业大学，2026-09-22）────────────────────────
    #
    # ★ 这是**兜底**，不是主路。主路是 `deck_from_template`（在模板页上填字，
    #   100% 保真）。这一条的价值在于：当模板文件不可用 / 页型不够用时，
    #   仍能出一份"学校的蓝"的稿子 —— 而不是回落到与学校无关的绢青。
    #
    # ⚠️ 色值**全部逐字取自对模板 17 页的实测统计**，一个都不许编：
    #   `#064178` 出现 99 次（绝对主色）、`#6B9CC9` 6 次、`#B4C7E7` 4 次、
    #   `#244C89` 4 次、`#2864A0` 3 次、`#3471A1` 3 次、`#F2F2F2` 5 次、
    #   `#313D51` 2 次、`#4F9FDB` 1 次、`#0563C1`/`#954F72`（Office 链接色）。
    #
    # ⚠️ 字体**刻意不照抄模板**：模板标题用的是「阿里巴巴普惠体」（190 处），
    #   而**本机真缺**（`C:\Windows\Fonts` 与用户字体目录都查过）。
    #   照抄它 ⇒ 换一台机器就是豆腐块。⇒ 用**本机必有**的微软雅黑一以贯之。
    "zjut": {
        "label": "浙江工业大学",
        "note": "学校蓝：深蓝压场，浅蓝分层；白底为主，墨字为文",
        "colors": {
            "dk1": "313D51",      # 墨 —— 正文（实测模板正文灰蓝）
            "lt1": "FFFFFF",      # 白 —— 页面底色
            "dk2": "2864A0",      # 中深蓝 —— 次级文字
            "lt2": "F2F2F2",      # 浅灰 —— 区块底（实测）
            "accent1": "064178",  # ★ 学校主色
            "accent2": "6B9CC9",  # 浅蓝 —— 分隔线 / 页脚
            "accent3": "244C89",  # 深蓝变体
            "accent4": "4F9FDB",  # 亮蓝 —— 数据锚点
            "accent5": "3471A1",
            "accent6": "B4C7E7",  # 淡蓝
            "hlink": "0563C1",
            "folHlink": "954F72",
        },
        "fonts": {
            "major_ea": "微软雅黑", "major_latin": "Microsoft YaHei",
            "minor_ea": "微软雅黑", "minor_latin": "Microsoft YaHei",
        },
    },
}
DEFAULT_DECK_THEME = "juanqing"

# PowerPoint 标准 16:9（EMU）。**不要**用 `Inches(13.333)` 现算 —— 取整后是
# 12191352，和标准值差 648 EMU。肉眼看不出，但判据要拿它做等值比较。
SLIDE_16_9 = (12192000, 6858000)


def _run_fonts(node: ET.Element) -> tuple[int, int]:
    """(写了显式字体的 run 数, run 总数)。用来量"字体到底设了没有"。

    ⚠️ 只数 `a:rPr` 下**真的写了** `a:latin`/`a:ea` 的 run。缺省即"继承母版"，
    继承得对不对我们**不知道** —— 所以这一项量的是"设了没有"，不是"好不好看"。
    """
    total = with_font = 0
    for r in node.iter():
        if _local(r.tag) != "r":
            continue
        total += 1
        for ch in r:
            if _local(ch.tag) != "rPr":
                continue
            for pr in ch:
                if _local(pr.tag) in ("latin", "ea") and (pr.get("typeface") or "").strip():
                    with_font += 1
                    break
            break
    return with_font, total


def read_theme_colors(path: str | Path) -> dict:
    """读 `ppt/theme/theme1.xml` 的色槽原值（**不做任何解读**）。

    `dk1`/`lt1` 在默认模板里常是 `a:sysClr`（`windowText` / `window`），
    此时记它的 `lastClr` —— 那是渲染时真正落下来的值。

    读不出来返回 `{}`。`{}` 的意思是**"没读到"**，与"读到了但是空"是两件事
    （本项目 M 型：口径塌缩）。
    """
    out: dict = {}
    try:
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read("ppt/theme/theme1.xml"))
    except Exception:
        return out
    for scheme in root.iter():
        if _local(scheme.tag) != "clrScheme":
            continue
        for slot in scheme:
            name = _local(slot.tag)
            for c in slot:
                v = c.get("val")
                if v:
                    out[name] = str(v).upper()
                    break
            else:
                for c in slot:
                    lc = c.get("lastClr")
                    if lc:
                        out[name] = str(lc).upper()
                        break
        break
    return out


def _presentation_order(zf: zipfile.ZipFile) -> list[int]:
    """**演示顺序**下每页的**分片号**（第 1 页的分片号在最前）。读不动就返回 `[]`。

    ## 为什么非要有这个函数（2026-09-22，两个口径撞车）

    这份文件里同时住着两个"第几页"，它们**在普通 deck 里恰好相等** ——
    这正是这个 bug 能藏这么久的原因：

      · **分片号** = `ppt/slides/slideN.xml` 里的 N。
        上游审计器（`slide_number_from_path`）、`_notes_map`、以及路线甲的
        侧车 `<pptx>.origin.json` 的 `map`，用的都是它。
      · **页码** = `p:sldIdLst` 里的位置，也就是**人在 PowerPoint 里数到的第几页**。
        "第 7 页的讲稿"、"第 7 页没有备注"说的都是它。

    **路线甲里它们不相等**：产物是模板页的子集且**保留模板原编号**
    ⇒ 分片号是 `[1,2,3,5,…,17]` 这种带洞的序列，而页码是 `1..16`。
    把两者当成一回事，讲稿就会**写对页、验错页**（症状：逐页比对报一堆
    "发出去 248 字、读回来 0 字"，而字数在页与页之间**互相串位** ——
    那是排列错乱，不是内容丢失）。
    """
    names = set(zf.namelist())
    if "ppt/presentation.xml" not in names:
        return []
    by_rid: dict[str, int] = {}
    rel_name = "ppt/_rels/presentation.xml.rels"
    if rel_name in names:
        try:
            for rel in ET.fromstring(zf.read(rel_name)):
                if str(rel.get("Type") or "").endswith("/slide"):
                    by_rid[str(rel.get("Id"))] = _slide_no(str(rel.get("Target") or ""))
        except Exception:
            pass
    try:
        root = ET.fromstring(zf.read("ppt/presentation.xml"))
    except Exception:
        return []
    out: list[int] = []
    for node in root.iter():
        if _local(node.tag) != "sldId":
            continue
        n = by_rid.get(str(node.get("{%s}id" % NS["r"]) or ""), 0)
        if n:
            out.append(n)
    return out


def _notes_map(zf: zipfile.ZipFile) -> dict[int, str]:
    """备注页 → 它挂在第几张幻灯片上，以及备注正文。

    ⚠️ **不要把 rel 文件名揉成一个字符串再去正则猜**（踩过）：
    第一版写的是
    `_slide_no(rel_name.replace("/_rels/", "/").replace(".rels", ""))`，
    而 `_slide_no` 的正则是**小写** `slide(\\d+)\\.xml$` ——
    匹配不到 `notesSlide1.xml` 里的大写 S ⇒ 恒返回 0 ⇒ 去开不存在的
    `notesSlide0.xml` ⇒ 备注正文恒为空。**症状极具迷惑性**：
    `notes_slides` 计数是对的（那是按前缀数的），只有正文是空的，
    于是"备注明明加了 3 页，体检却报覆盖率 0%"。
    ⇒ 修法是**结构化解析**（名字里本来就有组），不是把门槛放宽。
    """
    out: dict[int, str] = {}
    names = set(zf.namelist())
    rel_names = [n for n in names
                 if re.match(r"ppt/notesSlides/_rels/notesSlide\d+\.xml\.rels$", n)]
    for rel_name in rel_names:
        m = re.match(r"(ppt/notesSlides/)_rels/(notesSlide\d+)\.xml\.rels$", rel_name)
        if not m:
            continue
        note_name = f"{m.group(1)}{m.group(2)}.xml"
        try:
            root = ET.fromstring(zf.read(rel_name))
        except Exception:
            continue
        target = ""
        for rel in root:
            if str(rel.get("Type") or "").endswith("/slide"):
                target = str(rel.get("Target") or "")
                break
        if not target:
            continue
        idx = _slide_no(target)
        if not idx:
            continue
        # 备注正文住在 type="body" 的那个占位符里；sldNum / sldImg / hdr 要排掉，
        # 否则"备注覆盖率"会把页码也算成讲稿 —— 那是个恒真的假绿。
        body = ""
        if note_name in names:
            try:
                nroot = ET.fromstring(zf.read(note_name))
            except Exception:
                nroot = None
            if nroot is not None:
                for sp in nroot.iter():
                    if _local(sp.tag) != "sp":
                        continue
                    if _ph_type(sp) == "body":
                        body = _shape_text(sp)
                        break
        out[idx] = body
    return out


def probe_pptx(path: str | Path) -> DeckFacts:
    """把 .pptx 当 zip 读，量出体检要用的**事实**。

    为什么不用 python-pptx：这一层要能在**任何**解释器上跑。
    审计器已经选了纯标准库（上游脚本自己的选择），量事实这层跟着同一口径，
    否则"能不能审"又变成一个依赖问题。
    """
    p = Path(path)
    f = DeckFacts(path=str(p))
    if not p.exists():
        f.problems.append(f"文件不存在：{p}")
        return f
    try:
        zf = zipfile.ZipFile(p)
    except Exception as e:
        f.problems.append(f"不是合法的 .pptx（zip 打不开）：{type(e).__name__}: {e}")
        return f

    with zf:
        names = zf.namelist()
        f.media_count = len([n for n in names if n.startswith("ppt/media/")])
        # 幻灯片尺寸
        try:
            proot = ET.fromstring(zf.read("ppt/presentation.xml"))
            sz = None
            for el in proot.iter():
                if _local(el.tag) == "sldSz":
                    sz = el
                    break
            if sz is not None:
                f.slide_cx = int(sz.get("cx") or 0)
                f.slide_cy = int(sz.get("cy") or 0)
            if f.slide_cx and f.slide_cy:
                f.aspect = f.slide_cx / f.slide_cy
                # 16:9 = 1.7778，4:3 = 1.3333 —— 两者相距很远，0.02 的容差
                # 只是为了让 12191352 这类取整误差不至于误判。
                f.is_widescreen = abs(f.aspect - 16 / 9) <= 0.02
        except Exception as e:
            f.problems.append(f"读不到 ppt/presentation.xml：{type(e).__name__}: {e}")

        # ── 骨架几何：master 里的占位符框。判据层要拿它和画布比 ──
        f.master_geo = _master_placeholder_geo(zf)
        # ── 占位符继承链：几何/内边距/自适应/字号都要靠它才读得到 ──
        #   构建一次、逐页复用（每页现建会把 13 页的版式重复解析 13 遍）。
        chain = PhChain(zf)
        f.inherit_degraded = list(chain.degraded)

        # ── 主题色 / 主题字体：只读**原值**，解读放在判据层 ──
        f.theme_colors = read_theme_colors(p)
        try:
            troot = ET.fromstring(zf.read("ppt/theme/theme1.xml"))
            for node in troot.iter():
                if _local(node.tag) != "fontScheme":
                    continue
                for fam in node:
                    key = "major" if _local(fam.tag) == "majorFont" else "minor"
                    for slot in fam:
                        s = _local(slot.tag)
                        # 只收 latin / ea / cs 三个槽。`a:font script="…"` 是
                        # **逐脚本兜底**（一份 fontScheme 里有几十个），全收会互相
                        # 覆盖成同一个键，读出来的东西看着像事实、其实是噪声。
                        if s not in ("latin", "ea", "cs"):
                            continue
                        tf = slot.get("typeface")
                        if tf:
                            f.theme_fonts[f"{key}_{s}"] = tf
                break
        except Exception:
            pass

        notes = _notes_map(zf)
        # ★ 枚举顺序 = **演示顺序**，标签用**页码**（1..N），分片号另存 `part`。
        #   `slide` 字段从"分片号"改成"页码"是**有意的**：报告里写「第 7 页」，
        #   人就该能翻到第 7 页。路线甲里两者不等（分片号 `[1,2,3,5,…]`），
        #   见 `_presentation_order`。上游 findings 那种**必须**用分片号的地方，
        #   走的是 `data["slides"]`（原样透传的上游 JSON），不经过这里。
        _order = _presentation_order(zf)
        if not _order:
            # 兜底：presentation.xml 读不动 ⇒ 退回"按分片号排序"（旧口径）。
            # 这条路会和真实演示顺序不一致，所以只在读不动时走 —— 宁可退成
            # "可能错序"，也不退成"0 页"（后者会让整份体检变成空壳）。
            _order = sorted(_slide_no(n) for n in names
                            if re.match(r"ppt/slides/slide\d+\.xml$", n))
        slides = ["ppt/slides/slide%d.xml" % _n for _n in _order]
        # "为什么没出数"的分类计数（全篇累加）。见 `_sp_overflow` 的 `stats`。
        skip: dict[str, int] = {}
        for page, name in enumerate(slides, 1):
            no = page                       # 「第几页」= 演示顺序（见 _presentation_order）
            part = _slide_no(name)          # 分片号：备注正文按它取，上游 findings 也用它
            try:
                root = ET.fromstring(zf.read(name))
            except Exception as e:
                f.problems.append(f"第 {no} 页解析失败：{type(e).__name__}: {e}")
                continue
            chars = 0
            has_title = False
            title_text = ""
            body_chars = 0
            body_min: float | None = None
            all_min: float | None = None
            all_max: float | None = None
            # ^ 每页最大显式字号：品牌模板的"标题"常常是**普通文本框**而不是
            #   `title` 占位符 ⇒ 只认占位符会把它判成"这一页没有标题"，
            #   于是每一页都亮一条警告。**一条永远亮着的警告等于没有警告**（定律 10）。
            #   判据改成"占位符 **或** 一处 ≥24pt 的大字"，既不再误报，
            #   也仍能抓住真的没标题（只有 12pt 正文的那种页）。
            slide_ov: float | None = None     # 本页最大的「估高 / 可用高」
            for node in root.iter():
                # 视觉元素计数（`pic` 与 `tbl`/`chart` 常一起出现在 graphicFrame 内，
                # 按标签各自数一次即可 —— 只求"有没有"，不求精确归属）
                _t = _local(node.tag)
                if _t == "pic":
                    f.pictures += 1
                elif _t == "tbl":
                    f.tables += 1
                elif _t == "chart":
                    f.charts += 1
                if _t not in ("sp", "graphicFrame"):
                    continue
                if _t == "sp":
                    # ★ 传 **`part`（分片号）**，不是 `no`（演示顺序）—— `PhChain` 按部件名建索引。
                    #   路线甲产物里两者不等（成员名是模板源页号），传错就是"拿甲页的
                    #   layout 去解析乙页"，字号/几何/自适应全错。**判据在两个方向都会失守。**
                    ov = _sp_overflow(node, chain, part, skip)
                    if ov is not None:
                        need, avail = ov
                        f.overflow_measured += 1
                        ratio = need / avail
                        if slide_ov is None or ratio > slide_ov:
                            slide_ov = ratio
                        if f.overflow_worst is None or ratio > f.overflow_worst:
                            f.overflow_worst = ratio
                wf, tot = _run_fonts(node)
                f.runs_with_font += wf
                f.runs_total += tot
                txt = _shape_text(node)
                chars += len(txt)
                ph = _ph_type(node)
                if ph in ("title", "ctrTitle"):
                    has_title = True
                    # 标题文字要真的取出来 —— 否则"标题过长"那条检查
                    # 拿不到输入，会变成一条**永远不报**的检查（G 型）。
                    if txt and not title_text:
                        title_text = txt
                for pt, run_chars in _run_sizes(node):
                    all_min = pt if all_min is None else min(all_min, pt)
                    all_max = pt if all_max is None else max(all_max, pt)
                    if run_chars >= _BODY_TEXT_MIN_CHARS:
                        body_chars += run_chars
                        body_min = pt if body_min is None else min(body_min, pt)
            ntext = notes.get(part, "")
            # ── 版心利用率（W5）：墨迹并集 + 形状框并集（对照）+ 页型 + 正文段数 ──
            _ink, _box, _bullets = _slide_content_rects(root, chain, part)
            # ── 谁的字压在谁上面（W8）：字色 + 下层填充色，对比度门的输入 ──
            _tshp, _fshp = _slide_color_facts(root, chain, part, f.theme_colors)
            _usable = _usable_region(f.slide_cx, f.slide_cy)
            f.slides.append({
                "slide": no, "part": part,
                "text_chars": chars, "has_title": has_title,
                "max_font_pt": all_max,
                "title_text": title_text,
                "max_run_chars": body_chars,
                "min_font_pt": body_min,
                "notes_chars": len(ntext),
                "has_notes": bool(ntext.strip()),
                "overflow_ratio": slide_ov,
                "kind": _slide_kind(root),
                "util": _util_from_rects(_ink, _usable),
                "util_box": _util_from_rects(_box, _usable),
                "bullets": _bullets,
                # 对比度门（W8）的逐页事实。**只追加**，不动上面任何已有键。
                "text_shapes": _tshp,
                "fill_shapes": _fshp,
            })
            if all_min is not None:
                f.min_font_pt = all_min if f.min_font_pt is None else min(f.min_font_pt, all_min)
            if body_min is not None:
                f.min_body_font_pt = (body_min if f.min_body_font_pt is None
                                      else min(f.min_body_font_pt, body_min))

    # 版式多样性：全篇一共用了几种不同的 slideLayout。
    # 13 页里 11 页是同一个 "Title and Content" ⇒ 通篇"标题+项目符号"，
    # 那是 Word 文档贴在幻灯片上，不是幻灯片（实测那份丑 deck 的指纹之一）。
    try:
        with zipfile.ZipFile(p) as zf2:
            lays: dict[str, int] = {}
            for n in zf2.namelist():
                if not re.match(r"ppt/slides/_rels/slide\d+\.xml\.rels$", n):
                    continue
                try:
                    rr = ET.fromstring(zf2.read(n))
                except Exception:
                    continue
                for rel in rr:
                    tgt = str(rel.get("Target") or "")
                    if "slideLayout" in tgt:
                        k = tgt.rsplit("/", 1)[-1]
                        lays[k] = lays.get(k, 0) + 1
            f.layout_kinds = len(lays)
            f.layout_top_share = (max(lays.values()) / sum(lays.values())
                                  if lays else 0.0)
    except Exception:
        pass

    f.slide_count = len(f.slides)
    f.overflow_skipped = dict(skip)
    f.notes_slides = len(notes)
    f.slides_with_notes = sum(1 for s in f.slides if s["has_notes"])
    f.notes_ratio = (f.slides_with_notes / f.slide_count) if f.slide_count else 0.0
    return f


def read_notes(path: str | Path) -> dict[int, str] | None:
    """读出"**第几页** → 备注正文"。**只给写完备注之后的独立复测用**。

    返回 `None` = **读不了**（文件不在 / zip 坏了）。**不返回 `{}`** ——
    `{}` 的意思是"读出来了，一页备注都没有"，和"没查成"是两件事
    （本项目 M 型：口径塌缩。`{}` 会把"没查"渲染成"查出来是空的"）。

    ⚠️ 这里**故意复用 `_notes_map` + `_presentation_order`**（体检用的
    同一套解析器 + 同一个排序器），不另写一份。
    理由：写完之后的复测，要核的正是"**第 9 条门会看到的那个数字**"。
    换个解析器去复测，就变成"我用自己的尺子量我的作品"—— 两边一旦不一致，
    还得再判一次"以谁为准"。用同一把尺子，子进程的自述才真的被外部事实替代。

    ★ 键是**页码**（演示顺序），**不是分片号**（2026-09-22 修）：
    这个函数的第一行一直写着"第几页"，但实现直接返回了 `_notes_map` 的
    分片号键 —— 在普通 deck 里两者相等，所以这个谎**看不出来**；
    路线甲里分片号带洞（`[1,2,3,5,…]`），它立刻显形。现在按演示顺序重排，
    一句话才真的等于它做的事。
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with zipfile.ZipFile(p) as zf:
            by_part = _notes_map(zf)
            order = _presentation_order(zf)
            if not order:
                order = sorted(by_part)     # 兜底：退回旧口径（按分片号排序）
            if not order:
                return by_part              # 读得动、但一页备注都没有
            # 每页都给一个键：没有备注的页映射到空串。于是"第 5 页缺讲稿"
            # 会以 `""` 的形态出现在调用方面前，而不是"这个键不存在"。
            return {i: by_part.get(part, "") for i, part in enumerate(order, 1)}
    except Exception:
        return None


def _nows(s: str) -> str:
    """去掉**所有**空白后比较用的形式。

    ⚠️ 为什么必须忽略空白：读取端 `_shape_text` 把形状里所有 `<a:t>` 直接
    拼接，**不加分隔符** ⇒ 三行备注读回来是 `第一行第二行第三行`。
    这是**读取端**的表现，不是写进去的内容有问题（python-pptx 那边确实是
    三个独立段落）。拿带空白的原文去比，就会得到一个必然发生的假告警 ——
    而假告警教人学会无视告警（设计定律 10）。
    去掉空白后仍然能抓真正的失败：少写一页、文字被截、退化成空。
    """
    return "".join(str(s or "").split())


# ══════════════════════ 资源解析 ══════════════════════
# 「这个解释器里有没有 python-pptx」的探测结果缓存。
# 起一次子进程要几百毫秒，而一个任务里会问很多次（诊断 + 每次写备注）。
# 缓存的是**事实**（装了/没装），不是"上次行不行"。
_PPTX_OK: dict[str, bool] = {}


def _exe_ok(exe: str, code: str = "import zipfile, json, sys") -> bool:
    """这个解释器起得来吗（且能跑通 `code`）？

    `code` 参数是 2026-09-21 加的：写讲稿备注要的不是"能起子进程"，
    而是"装了 python-pptx" —— **两个不同的要求**。同一件事只留这一个探针
    函数，把要探的代码当参数传进来，比另写一个 `_exe_has_pptx` 好
    （平行机制：两处探针迟早给出两个答案）。
    """
    try:
        r = subprocess.run([exe, "-c", code], capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def has_python_pptx(exe: str) -> bool:
    """这个解释器里能 `import pptx` 吗？**只探一次**。"""
    if exe not in _PPTX_OK:
        _PPTX_OK[exe] = _exe_ok(exe, "import pptx, json, sys")
    return _PPTX_OK[exe]


def find_deck_python(explicit: str | None = None) -> str | None:
    """跑审计的解释器。

    审计器是纯标准库 ⇒ 要求比 `figure.py` 那边低得多（那边要 matplotlib+PyMuPDF）。
    所以这里**允许退到"当前解释器"**：只要它能起子进程就够了。
    """
    if explicit:
        return explicit if _exe_ok(explicit) else None
    import sys
    if _exe_ok(sys.executable):
        return sys.executable
    return None


def find_notes_python(explicit: str | None = None) -> str | None:
    """**写讲稿备注**的解释器 —— 必须是装了 python-pptx 的那个。

    ⚠️ 刻意与 `find_deck_python` 分开，不合并。两者的门槛不同：
    审计器纯标准库（谁都能跑），写备注要第三方库。
    合成一个函数，就会长出「审计跑得动 ⇒ 以为备注也写得动」这种错觉，
    而它的症状是模型拿到一句 `ModuleNotFoundError` —— 模型既看不懂
    也修不了（它没有装包的能力），只会换个参数重试。
    **能力不可用时要说"能力不可用"，不要让它去撞。**
    """
    import sys
    cands = ([explicit] if explicit else []) + [sys.executable]
    for exe in cands:
        if exe and has_python_pptx(exe):
            return exe
    return None


def find_audit_script(skills_dir: str | None = None) -> Path | None:
    """找 nature-paper2ppt 自带的审计脚本。

    依次试：配置给的技能根 → `~/.agents/skills`（本机真库）→ 环境变量。
    **找不到就返回 None**，由上层如实报 `SKIPPED`（SKIPPED 不算通过）。
    """
    import os
    roots: list[Path] = []
    if skills_dir:
        roots.append(Path(skills_dir))
    env = os.environ.get("RONGKE_SKILLS_DIR")
    if env:
        roots.append(Path(env))
    roots.append(Path.home() / ".agents" / "skills")
    roots.append(Path.home() / ".dsh" / "skills")
    for root in roots:
        cand = root / "nature-paper2ppt" / "scripts" / "audit_pptx_quality.py"
        if cand.is_file():
            return cand
    return None


# ══════════════════════ 写讲稿备注 ══════════════════════
# 由 `write_slide_notes` 生成并交给子进程执行的脚本。**参数从 stdin 进来**
# （UTF-8 编码的 JSON），于是：① 没有命令行引号转义问题（Windows 上那段
# 转义规则能把人耗死）；② 不需要临时文件 —— 而临时文件是要还"删除额度"的。
#
# 三个刻意的写法，都是为了让**编码**这件事不再是一个变量：
#   · `sys.stdin.buffer.read().decode("utf-8")` —— 显式解码，不依赖
#     `PYTHONIOENCODING` 之类的环境变量是否生效（Windows 上默认是 cp936）。
#   · `print(json.dumps(..., ensure_ascii=True))` —— stdout 纯 ASCII，
#     于是它是什么编码都无所谓；中文路径会变成 \uXXXX，`json.loads` 会还原。
#   · **取页一律走 `_slides_by_rid`，绝不写 `prs.slides`** —— 见下面那段血债。
#
# ══════════════════ 血债（2026-09-22 实测，不是推想）══════════════════════
# `Presentation.slides` 是个 **`@lazyproperty`，而且带副作用**：python-pptx
# 1.0.2 的 `pptx/presentation.py:108-113` 里，光**读一下** `prs.slides` 就会调
# `PresentationPart.rename_slide_parts(...)`，把每一页的分片名就地改成
# `slide1..N`（按 `sldIdLst` 顺序，即演示顺序）。改的**不是** `save()` ——
# `save()` 只是把已经被改过的名字写出去。⇒ **任何** python-pptx 代码只要
# 碰过 `.slides`，这份 deck 的分片编号就已经不再是它出厂时的样子了。
#
# 为什么这条对**路线甲**是致命的：`deck_from_template` 会把「模板源页号」
# 写进侧车 `<pptx>.origin.json` 的 `map`（产物页 → 模板源页），而
# `_template_baseline` 正是拿上游审计器报的**分片号**去对这套编号做模板基线
# 扣除的（上游 `slide_number_from_path()` 取的是 `ppt/slides/slideN.xml` 里
# 那个 N，口径本身就是分片号 —— 上游没错）。于是本工具写一次备注，
# `map` 就从 `[1,2,9,17]` 变成 `[1,2,3,4]`，扣除当场作废：
# 症状是体检门报一堆**模板自带的**装饰越界，理由写着
# 「产物页码集合与声明的模板源页不符」—— 报得对，但根因在这里。
# 隔离实验（`runs/_regress/_zjut/`）：同一份 4 页产物，`_iso4.pptx` 分片
# `[1,2,9,17]`；仅 `list(prs.slides)` + `save()` 之后变成 `[1,2,3,4]`；
# 换成按 rId 取页（本文件的写法）则原样保住 `[1,2,9,17]`。
#
# ⇒ 纪律：**写备注不许动分片名。** 而且这话不能只靠"我觉得应该没动"——
# 所以 `main()` 收尾会把落盘后的分片名**再数一遍**（标准库 zip 解析，与
# 第 9 条硬门同一把尺子），`parts_preserved` 如实回给调用方。哪天真被改坏了，
# 它会自己说出口，而不是等体检门去猜。
_NOTES_SCRIPT = r'''
import json
import sys
import traceback


def _by_num(names):
    import re
    return sorted(names, key=lambda x: int(re.findall(r"\d+", x)[0]))


def _slides_by_rid(prs):
    """按 `sldIdLst` 顺序取页 + 记下当前分片名。**刻意不读 `prs.slides`。**

    读了就会触发 `rename_slide_parts`（见本文件外面那段血债），
    分片名会被就地重编号，路线甲的模板基线扣除随之失效。
    """
    part = prs.part
    sld_id_lst = part._element.get_or_add_sldIdLst()
    slides = []
    names = []
    for sld_id in sld_id_lst:
        slides.append(part.related_slide(sld_id.rId))
        names.append(str(part.related_part(sld_id.rId).partname).split("/")[-1])
    return slides, _by_num(names)


def _slide_part_names(path):
    """标准库读盘数一遍 `ppt/slides/slideN.xml`。与第 9 条硬门同一把尺子。"""
    import re
    import zipfile
    with zipfile.ZipFile(path) as z:
        got = [n.split("/")[-1] for n in z.namelist()
               if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    return _by_num(got)


def _notes_body_frame(sl):
    """取该页备注文本框；**notesSlide 缺 `type=body` 占位符时先补一个再取**。

    返回 `None` = 补不出来（notesMaster 里也没有 body 占位符），
    交调用方把这一页**点名**成失败页。

    🪤 为什么这里必须有防御（2026-09-28 W13-C 实测）：
      `notes_slide.notes_text_frame` 在 notesSlide 没有 body 占位符时返回
      **`None`** —— 那是 python-pptx 的**正常返回**（`pptx/slide.py`
      `notes_text_frame` 的 docstring 自己写了 "|None| if there is no notes
      placeholder"），不是异常。原代码拿到就 `.text`
      ⇒ `AttributeError: 'NoneType' object has no attribute 'text'`。
      真模板 58 个 notesSlide 里恰好有 1 个零占位符（`notesSlide3.xml`，
      页型「章节页-1」）⇒ 用一次章节页，全篇讲稿 0 写入。

    ★ 补占位符**只动 notes 侧那一个 part**：拿 `notes_master` 的 body 占位符
      走 `shapes.clone_placeholder()`，落款是 `ppt/notesSlides/notesSlideN.xml`
      里多出一个 `<p:ph type="body"/>`。**绝不碰 `ppt/slides/*.xml`** ——
      第 9 条硬门的同构基线比的是产物页与模板页，动 slide 本体等于把基线凿穿
      （与本文件上面那段"分片名"血债同一类事故）。
    """
    from pptx.enum.shapes import PP_PLACEHOLDER
    ns = sl.notes_slide
    tf = ns.notes_text_frame
    if tf is not None:
        return tf
    body = [p for p in ns.part.notes_master.placeholders
            if p.element.ph_type == PP_PLACEHOLDER.BODY]
    if not body:
        return None
    ns.shapes.clone_placeholder(body[0])
    return ns.notes_text_frame


def main():
    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    from pptx import Presentation
    prs = Presentation(req["pptx"])
    notes = req["notes"]
    slides, parts_before = _slides_by_rid(prs)
    if len(slides) != len(notes):
        return {"ok": False,
                "error": "写完之前页数变了：文件里 %d 页，收到 %d 段备注"
                         % (len(slides), len(notes))}
    out = {"ok": True, "slides": len(slides), "wrote": 0, "empty": [],
           "replaced": [], "chars": [], "failed": [],
           "slide_parts_before": parts_before}
    for i, sl in enumerate(slides):
        txt = str(notes[i])
        # ★ **逐页各自成败**：一页坏只点名这一页，好页照常写入、照常落盘。
        #   原来是"整份一个循环 + 末尾一次 save" ⇒ 一处坏牵连全篇 0 写入，
        #   而且报错里看不出是第几页坏的 —— 静默全篇失败最坏的地方就在于它
        #   长得像"这份 deck 有问题"，其实是内核没防护。
        #   这里吞异常**不是**掩盖失败：失败页进 `failed`，且那一页读不回
        #   讲稿 ⇒ 调用方独立复测时落进 `mismatches` ⇒ `ok=False`。
        #   判据仍是量出来的事实，不是子进程自称的。
        try:
            tf = _notes_body_frame(sl)
            if tf is None:
                raise ValueError("notesSlide 缺 body 占位符，且 notesMaster "
                                 "里也没有可补的 body 占位符")
            had_text = bool((tf.text or "").strip())
            tf.clear()
            lines = txt.split("\n")
            tf.paragraphs[0].text = lines[0]
            for extra in lines[1:]:
                tf.add_paragraph().text = extra
        except Exception as e:
            out["failed"].append({"page": i + 1,
                                  "error": "%s: %s" % (type(e).__name__, e)})
            out["chars"].append(0)
            continue
        if had_text:
            out["replaced"].append(i + 1)
        out["chars"].append(len(txt))
        if txt.strip():
            out["wrote"] += 1
        else:
            out["empty"].append(i + 1)
    prs.save(req["pptx"])
    out["slide_parts_after"] = _slide_part_names(req["pptx"])
    out["parts_preserved"] = out["slide_parts_after"] == parts_before
    return out


try:
    result = main()
except Exception as e:
    result = {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
              "tb": traceback.format_exc()[-1500:]}
print(json.dumps(result, ensure_ascii=True))
'''


@dataclass
class NotesWrite:
    """写备注的**结果**，也是它自己的证据。字段全是量出来的事实。"""
    ok: bool = False
    pptx: str = ""
    requested: int = 0                 # 收到几段
    slides: int = 0                    # 文件里几页
    wrote: int = 0                     # **复测**出的有备注页数（不是子进程自称的）
    notes_ratio: float = 0.0           # 复测出的覆盖率
    empty_slides: list[int] = field(default_factory=list)      # 你给了空串的页
    no_notes_slides: list[int] = field(default_factory=list)   # 复测仍无备注的页
    replaced_slides: list[int] = field(default_factory=list)   # 原有备注被覆盖的页
    failed_slides: list[dict] = field(default_factory=list)    # 子进程点名的失败页
    char_counts: list[int] = field(default_factory=list)
    slide_parts_before: list[str] = field(default_factory=list)
    slide_parts_after: list[str] = field(default_factory=list)
    parts_preserved: bool | None = None   # None = 没量到（不是"没动过"）
    bytes_before: int = 0
    bytes_after: int = 0
    python_exe: str = ""
    rc: int = 0
    stderr_tail: str = ""
    problems: list[str] = field(default_factory=list)   # 写之前的拒绝 / 失败原因
    unverified: str = ""                                # 写完了但**核不了**的原因
    mismatches: list[str] = field(default_factory=list)  # 逐页比对不一致

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "pptx": self.pptx, "requested": self.requested,
            "slides": self.slides, "wrote": self.wrote,
            "notes_ratio": round(self.notes_ratio, 4),
            "empty_slides": self.empty_slides,
            "no_notes_slides": self.no_notes_slides,
            "replaced_slides": self.replaced_slides,
            "failed_slides": self.failed_slides,
            "char_counts": self.char_counts,
            "slide_parts_before": self.slide_parts_before,
            "slide_parts_after": self.slide_parts_after,
            "parts_preserved": self.parts_preserved,
            "bytes_before": self.bytes_before, "bytes_after": self.bytes_after,
            "python_exe": self.python_exe, "rc": self.rc,
            "stderr_tail": self.stderr_tail, "problems": self.problems,
            "unverified": self.unverified, "mismatches": self.mismatches,
        }

    def format_for_model(self) -> str:
        name = Path(self.pptx).name if self.pptx else "(未指定)"
        if self.ok:
            head = "✔ 讲稿备注已写入（独立复测通过）"
        elif self.problems:
            head = "✘ 没有写入"
        else:
            head = "⚠ 写入动作跑了，但**复测没通过**"
        lines = [f"═══ 讲稿备注 · {name} ═══", f"结果：{head}"]
        if self.slides:
            lines.append(f"页数：{self.slides}   有讲稿的页：{self.wrote}/{self.slides}"
                         f"（{self.notes_ratio:.0%}）")
        if self.bytes_before and self.bytes_after:
            lines.append(f"体积：{self.bytes_before:,} → {self.bytes_after:,} 字节"
                         f"（{self.bytes_after - self.bytes_before:+,}）")
        if self.parts_preserved is True:
            lines.append(f"分片编号：**原样保住** {self.slide_parts_after[:8]}"
                         f"{'…' if len(self.slide_parts_after) > 8 else ''}"
                         f"（共 {len(self.slide_parts_after)} 页）—— 路线甲靠"
                         f"这套编号做模板基线扣除，保住了才扣得动。")
        elif self.parts_preserved is False:
            lines.append(
                f"⚠️ 分片编号被改了：{self.slide_parts_before[:8]} → "
                f"{self.slide_parts_after[:8]}\n"
                f"   ⇒ 这份 deck 的 `<pptx>.origin.json` 侧车（模板源页映射）"
                f"**已经对不上**，体检门的模板基线扣除会失效，把模板自带的装饰"
                f"越界报成你的问题。请把这条当成内核 bug 报上去，"
                f"**不要**用 `--save` 去压体检基线。")
        if self.replaced_slides:
            lines.append(f"原有备注被覆盖的页：{self.replaced_slides[:20]}"
                         f"（共 {len(self.replaced_slides)} 页）")
        if self.empty_slides:
            lines.append(f"⚠️ 你给的是**空串**的页：{self.empty_slides[:20]}"
                         f"（共 {len(self.empty_slides)} 页）—— 空串不算讲稿，"
                         f"覆盖率按**非空**算。")
        if self.failed_slides:
            lines.append(f"⚠️ 内核**写不进去**的页（共 {len(self.failed_slides)} 页）"
                         f"—— 其余页照常写入了，不是整份作废：")
            for _f in self.failed_slides[:20]:
                lines.append(f"  - 第 {_f.get('page')} 页：{_f.get('error')}")
        if self.python_exe:
            lines.append(f"解释器：{self.python_exe}（rc={self.rc}）")
        if self.problems:
            lines += ["", "原因："] + [f"  - {x}" for x in self.problems]
        if self.unverified:
            lines += ["", "复测没做成（所以**不能**说写好了）：", f"  - {self.unverified}"]
        if self.mismatches:
            lines += ["", f"逐页比对不一致（{len(self.mismatches)} 页）："
                      ] + [f"  - {x}" for x in self.mismatches[:12]]
        if self.stderr_tail.strip():
            lines += ["", "子进程 stderr 尾部：", "  " + self.stderr_tail.strip()[-500:]]
        if self.char_counts:
            lines += ["", "逐页讲稿字数：",
                      "  | 页 | 讲稿字数 |", "  |---:|---:|"]
            for i, c in enumerate(self.char_counts, 1):
                flag = " ⚠️空" if not str(c).strip() or c == 0 else ""
                lines.append(f"  | {i} | {c}{flag} |")
        if self.ok:
            lines += ["",
                      "⚠️ 你**改过 deck** ⇒ 之前那份 deck 体检报告已经作废。",
                      "交付前必须**重跑** `audit_deck`（传同一个路径），否则第 9 条硬门"
                      "会按「报告过期」拦下交付。",
                      "⚠️ **之后别再调 `save_presentation` 存同一份 deck** —— "
                      "MCP 服务端内存里那份是**没有备注的旧版本**，它一存盘就会把刚写的"
                      "备注**整片抹掉**（本工具改的是磁盘文件，服务端并不知道）。"
                      "要改内容就先改完、最后一步再补备注；补完只做 `audit_deck`。"]
        else:
            lines += ["",
                      "接下来：按上面的原因改掉再重试。**别重复同样的调用** —— "
                      "同一个参数再来一次结果不会变。"]
        lines.append("═" * 28)
        return "\n".join(lines)


def write_slide_notes(
    pptx: str | Path,
    notes,
    workdir: str | Path | None = None,
    python_exe: str | None = None,
    cfg=None,
    timeout: float | None = None,
) -> NotesWrite:
    """给已有 .pptx 的**每一页**写讲稿备注（备注栏就是讲稿本身）。

    ## 为什么非要有这个方法（2026-09-21 实测，不是推想）

    上游 37 个 MCP 工具**没有**写备注的能力（`grep -rniE "notes|speaker"`
    在 `vendor/ppt-mcp` 只命中一句无关的 `implementation_notes`）。
    技能里给的兜底路径是「另跑一小段 python-pptx」，而**内核没有执行代码
    的工具** ⇒ 那条路在荣珂里走不通。代价实测（runs/20260921-121201，
    25 分钟 / 67 次调用）：deck 建好之后，约 **19 次调用（28%）**全耗在
    找「怎么写备注」上 —— 6 次幻觉 `apply_professional_design(
    operation="add_speaker_notes")`、4 次 `dispatch_task`（4 个执行器全
    `available: false`）、9 次 `load_skill` 翻文档；最后 `audit_deck`=0、
    `deliver_task`=0 —— **整场跑完，零交付。**

    ## 为什么是「数据进、结果出」，而不是给模型一个 `run_python`

    模型给**文本**，代码由内核生成。三条理由：
      ① **确定性**：同一份输入必然同一份结果，不取决于它这次代码写得对不对；
      ② **可测**：负控清清楚楚（页数不匹配 / 路径越界 / 不是 pptx / 是二进制），
         而「模型自己写的代码跑挂了」根本没法当负控 —— 那是它的题目，不是我的；
      ③ **不把它推到够不着的地方**：它没有装包、改环境的能力。让它面对
         `ModuleNotFoundError`，它只会换个参数继续试（上面那 19 次就是这么烧掉的）。
    ⚠️ 这**不是** `run_python`。要开一个能跑任意代码的基元，围栏 / 超时 /
    进程回收 / 输出上限每件都要单独设计、单独测 —— 那是另一件事，别在这里顺手做。

    ## 判据

    `notes` 是**按页序一一对应**的字符串列表，长度必须**恰好等于**页数。
    不匹配直接拒绝并报出真实页数 —— 短了或长了都意味着"哪段配哪页"要靠猜，
    而猜出来的讲稿没有意义（这份 deck 是给人上台讲的）。
    某页确实不要讲稿就占位给空串 `""`。

    ## 写完不自我采信

    子进程 `rc=0` **不等于**写成了。所以写完用 `probe_pptx` + `read_notes`
    **独立复测**（标准库 zip 解析，与第 9 条硬门**同一把尺子**），再逐页把
    "发出去的文本"与"读回来的文本"比一遍。复测做不成 ⇒ `ok=False`
    并说明**核不了** —— 绝不渲染成"写好了"。

    ## 写备注**不许动分片名**（2026-09-22 血债，实测）

    `Presentation.slides` 带副作用：python-pptx 1.0.2 的
    `pptx/presentation.py:112` 在**读**这个 `@lazyproperty` 时就调
    `PresentationPart.rename_slide_parts()`，把每页分片名改成 `slide1..N`。
    ⇒ 只要写过一次备注，**路线甲**那份 `<pptx>.origin.json` 侧车里的
    「产物页 → 模板源页」映射就跟磁盘上的分片号对不上了，`_template_baseline`
    的扣除随之失效（症状：体检门把**模板自带的**装饰越界报成缺陷）。
    所以本方法的子进程按 `sldIdLst` 的 rId 直取页，并把落盘后的分片名
    **重新数一遍**回给调用方（`parts_preserved`）—— 它坏了会自己说出口。
    """
    res = NotesWrite(pptx=str(pptx or ""))

    # ── ① 参数形状：先在最便宜的地方拦住 ────────────────────
    if isinstance(notes, (str, bytes)) or not isinstance(notes, (list, tuple)):
        extra = ("  你给的是一整段文本 —— 但本工具要**逐页**给："
                 "请按页序拆成与页数等长的若干段。"
                 if isinstance(notes, (str, bytes)) else "")
        res.problems.append(
            f"`notes` 必须是**字符串列表**（每页一段），收到的是 "
            f"{type(notes).__name__}。{extra}")
        return res
    texts = ["" if x is None else (x if isinstance(x, str)
                                   else json.dumps(x, ensure_ascii=False))
             for x in notes]
    res.requested = len(texts)

    # ── ② 路径：**先解析成绝对路径，再判围栏** ───────────────
    #    ⚠️ 注册表那道工作区围栏只检查**绝对路径**的参数（相对路径它一律放行，
    #    理由是"cwd 已在工作区内"）；而 `../..` 恰恰也是相对的。
    #    ⇒ 这道围栏必须由工具自己关，且必须**先 `resolve()` 再判**，
    #    否则 `ws/../..` 这种写法判的时候还是"在里头"。
    root = Path(workdir).resolve() if workdir else Path.cwd().resolve()
    raw = str(pptx or "").strip()
    if not raw:
        res.problems.append("`pptx` 不能为空：给工作区里 .pptx 的路径（如 \"deck.pptx\"）。")
        return res
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    try:
        p = p.resolve()
    except Exception as e:
        res.problems.append(f"路径无法解析：{raw}（{type(e).__name__}: {e}）")
        return res
    if p != root and root not in p.parents:
        res.problems.append(
            f"⛔ 越界：{p}\n   本任务工作区是 {root}，只能改它里面的文件。\n"
            f"   （注册表那道围栏只看绝对路径，`../` 这类本工具自己拦。）")
        return res
    res.pptx = str(p)

    if not p.is_file():
        near: list[str] = []
        try:
            near = sorted(x.name + ("/" if x.is_dir() else "")
                          for x in root.iterdir())[:40]
        except Exception:
            pass
        res.problems.append(
            f"文件不存在：{raw}\n   工作区 {root} 里真实有的是：\n    "
            + ("\n    ".join(near) if near else "（空）")
            + "\n   ⚠️ 请**照抄**上面的名字 —— 路径写错时不会替你找相近的。")
        return res

    facts = probe_pptx(p)
    if facts.problems or facts.slide_count == 0:
        res.problems.append("这**不是**一份能改的 .pptx："
                            + "；".join(facts.problems or ["0 页"]))
        return res
    res.slides = facts.slide_count
    try:
        res.bytes_before = p.stat().st_size
    except Exception:
        res.bytes_before = 0

    # ── ③ 页数必须一一对应：不匹配就拒绝，**不猜** ───────────
    if res.requested != res.slides:
        res.problems.append(
            f"备注段数与页数不等：这份 deck 有 **{res.slides} 页**，你给了 "
            f"**{res.requested} 段**备注。\n"
            f"   `notes` 是**按页序一一对应**的（第 1 段配第 1 页），"
            f"长度必须恰好 {res.slides}。\n"
            f"   本工具**不会**替你猜哪段配哪页 —— 猜出来的讲稿等于没有。\n"
            f"   某页确实不需要讲稿也要占位给空串 \"\"，"
            f"但覆盖率按**非空**备注算，别靠空串凑数。")
        return res

    # ── ④ 解释器：必须装了 python-pptx ───────────────────────
    explicit = python_exe or (getattr(cfg, "python", None) if cfg is not None else None)
    py = find_notes_python(explicit)
    res.python_exe = py or str(explicit or "(未指定)")
    if py is None:
        res.problems.append(
            f"**写备注的能力不可用**：找不到装了 python-pptx 的解释器。\n"
            f"   试过：{explicit or '(未指定)'}（config.json 的 deck.python）"
            f"、以及当前解释器。\n"
            f"   ⇒ 本任务**写不了**这份 deck 的讲稿备注。**不要反复重试** —— "
            f"请在交付时如实说明「这份 deck 不带讲稿，以及为什么」。")
        return res

    # ── ⑤ 跑 ────────────────────────────────────────────────
    to = float(timeout or getattr(cfg, "timeout", None) or 180.0)
    payload = json.dumps({"pptx": str(p), "notes": texts}, ensure_ascii=False)
    try:
        # **不设 `cwd`**：payload 里给的是绝对路径，让 cwd 保持现状。
        # 踩过 K 型：另起 cwd 之后子进程按相对路径找不到文件，症状却长得
        # 像"deck 有问题"。凡是自己起子进程的工具，一律显式钉绝对路径。
        r = subprocess.run([py, "-c", _NOTES_SCRIPT], input=payload.encode("utf-8"),
                           capture_output=True, timeout=to)
    except subprocess.TimeoutExpired:
        res.problems.append(
            f"写备注超时（{to:.0f}s）—— 脚本被中断，文件**可能只写了一半**。"
            f"请先重新读一遍这份 deck 再决定下一步。")
        return res
    except Exception as e:
        res.problems.append(f"起不了子进程（{py}）：{type(e).__name__}: {e}")
        return res
    res.rc = int(r.returncode)
    res.stderr_tail = (r.stderr or b"").decode("utf-8", "replace")[-800:]

    data = None
    for line in reversed((r.stdout or b"").decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except Exception:
                continue
    if not isinstance(data, dict):
        # ⚠️ 这里**不是**"写了 0 页备注"，是"根本不知道写没写成"。
        #    rc 只当背景信息 —— 判据是"有没有一份可读的结果"。
        res.problems.append(
            f"写备注的脚本**没有回一份可读的结果**（rc={res.rc}）⇒ 写成没写成"
            f"**无法确定**。\n   stdout 尾部："
            f"{(r.stdout or b'').decode('utf-8', 'replace')[-300:] or '（空）'}\n"
            f"   stderr 尾部：{res.stderr_tail or '（空）'}")
        return res
    if not data.get("ok"):
        res.problems.append(
            (f"写备注失败：{data.get('error')}\n{data.get('tb') or ''}").strip())
        return res
    res.empty_slides = [int(x) for x in (data.get("empty") or [])]
    res.replaced_slides = [int(x) for x in (data.get("replaced") or [])]
    res.failed_slides = [dict(x) for x in (data.get("failed") or [])
                         if isinstance(x, dict)]
    res.char_counts = [int(x) for x in (data.get("chars") or [])]
    res.slide_parts_before = [str(x) for x in (data.get("slide_parts_before") or [])]
    res.slide_parts_after = [str(x) for x in (data.get("slide_parts_after") or [])]
    if "parts_preserved" in data:
        # 子进程**数过盘**才敢说这话（`_slide_part_names`），不是它的自述。
        res.parts_preserved = bool(data.get("parts_preserved"))

    # ── ⑥ 独立复测：不采信子进程的自述 ──────────────────────
    after = probe_pptx(p)
    if after.problems or after.slide_count == 0:
        res.unverified = ("写完复测打不开 ⇒ 这份文件现在**状态不明**："
                          + "；".join(after.problems or ["0 页"]))
        return res
    got = read_notes(p)
    if got is None:
        res.unverified = "写完复测读不出备注页（zip 解析失败）⇒ 写成没写成**无法确认**。"
        return res
    res.slides = after.slide_count
    res.wrote = after.slides_with_notes
    res.notes_ratio = after.notes_ratio
    try:
        res.bytes_after = p.stat().st_size
    except Exception:
        res.bytes_after = 0
    res.no_notes_slides = [s["slide"] for s in after.slides if not s["has_notes"]]

    want = {i: t for i, t in enumerate(texts, 1)}
    for i in sorted(want):
        back = _nows(got.get(i, ""))
        sent = _nows(want[i])
        if back != sent:
            res.mismatches.append(
                f"第 {i} 页：发出去 {len(sent)} 字（去空白），读回来 {len(back)} 字"
                + ("（读回来是空的）" if not back else ""))
    missing = [i for i, t in want.items() if t.strip() and i in res.no_notes_slides]
    res.mismatches += [f"第 {i} 页：复测仍读不到非空备注" for i in missing]
    res.ok = not res.mismatches
    return res


# ══════════════════════ 审计器 ══════════════════════
_STYLE_SCRIPT = r'''
# `io` 是"内存完工"要用的（见 main() 收尾）；`os`/`shutil` 已随"删临时文件"那一步
# 一起退场 —— 留着是两个没人用的名字，会让人以为这里还会落盘。
import json, sys, zipfile, io
from lxml import etree
from pptx import Presentation
from pptx.util import Emu, Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
def q(t):
    return "{%s}%s" % (A, t)

def patch_theme(blob, colors, fonts):
    """改 theme1.xml：12 个色槽 + majorFont/minorFont 的 latin/ea。返回新 blob。"""
    root = etree.fromstring(blob)
    hit = {"colors": [], "fonts": []}
    for scheme in root.iter(q("clrScheme")):
        for slot in scheme:
            name = etree.QName(slot).localname
            if name not in colors:
                continue
            for ch in list(slot):
                slot.remove(ch)
            el = etree.SubElement(slot, q("srgbClr"))
            el.set("val", colors[name].upper())
            hit["colors"].append(name)
        break
    for scheme in root.iter(q("fontScheme")):
        for fam in scheme:
            key = "major" if etree.QName(fam).localname == "majorFont" else "minor"
            for slot in fam:
                s = etree.QName(slot).localname
                kk = key + "_" + s
                if kk in fonts:
                    slot.set("typeface", fonts[kk])
                    hit["fonts"].append(kk)
        break
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=True), hit

def remap_skeleton(prs, ocx, ocy, ncx, ncy, asym_max=0.0):
    """把 master / layout 里**内容占位符**的框从旧画布映射到新画布。返回改了几个。

    映射规则：**保持四周边距、拉伸宽高**。
      · 为什么不等比缩放：4:3 → 16:9 是**只加宽**（高 6858000 不变）。等比缩放会把
        内容整体放大、纵向溢出，还得给下方空白找补。
      · 为什么"保持边距"是对的：画布加宽 33% 而边距不变 ⇒ 内容区跟着变宽，
        右侧那 28.8% 的空当就补上了。这就是 PowerPoint「最大化」的语义。

    `asym_max` = 左右边距比的容忍门槛（由调用方从 `GEO_ASYM_MAX` 注入）。
    ⚠️ **为什么走参数而不是在这里再写一个字面量**：本函数住在 `_STYLE_SCRIPT`
      字符串里、跑在**独立解释器**中，引用不到模块常量；抄一份就是 H 型
      （判据那边 3.0、修复这边 3.0，改一处忘一处即静默漂移）。
      传 0 / None ⇒ **关上硬化**（退化成"只按 ocx 算"的原始行为）。

    ★ **天然幂等**：形状若已在目标画布上，按新画布算出的 ml/mr 就是原值
      ⇒ 变换恒等。连套两次主题**不会**累积漂移（等比缩放会）。

    ⚠️ **只动内容占位符**（`CONTENT_PH_TYPES`：title / ctrTitle / body / subTitle）：
      · 页脚/日期/页码占位符的坐标是**锚点语义**（靠左/居中/靠右），
        按"保持左边距"处理会把靠右的页码留在左边 ⇒ 那才是弄坏设计。
      · `subTitle` 必须在内：只拉标题不拉副标题，同一页会出现 94.4% 与 63.7%
        并排 —— **比全都不拉更难看**。
      · 上游/作者摆的普通文本框常是刻意定位的（右上角标签、图注），一律不碰。
    """
    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    # ⚠️ 与主进程的 `CONTENT_PH_TYPES` **必须一致** —— 子进程是独立解释器，
    #    **引用不到那个常量**，只能重写一份。`deck_test` 有一条断言扫这两处源码盯着它。
    CONTENT_PH = ("title", "ctrTitle", "body", "subTitle")

    def pq(t):
        return "{%s}%s" % (P, t)

    def one(el):
        ph = None
        for t in el.iter():
            if etree.QName(t).localname == "ph":
                ph = t
                break
        if ph is None:
            return False
        if str(ph.get("type") or "body") not in CONTENT_PH:
            return False
        spPr = el.find(pq("spPr"))
        if spPr is None:
            return False
        xfrm = None
        for ch in spPr:
            if etree.QName(ch).localname == "xfrm":
                xfrm = ch
                break
        if xfrm is None:
            return False
        off = ext = None
        for ch in xfrm:
            n = etree.QName(ch).localname
            if n == "off":
                off = ch
            elif n == "ext":
                ext = ch
        if off is None or ext is None:
            return False
        try:
            x, y = int(off.get("x") or 0), int(off.get("y") or 0)
            w, h = int(ext.get("cx") or 0), int(ext.get("cy") or 0)
        except (TypeError, ValueError):
            return False
        if w <= 0 or h <= 0:
            return False
        ml, mr = x, ocx - (x + w)
        mt, mb = y, ocy - (y + h)
        # ★ 硬化（2026-09-21 补）：**左右不对称 = 画布已经被人改过、骨架停在旧画布上**。
        #   场景：一份稿子被旧版 `style_deck` 处理过（只改画布、没改骨架），于是 `ocx`
        #   已经是 16:9、骨架还是按 4:3 摆的 —— 这时按 `ocx` 算出的 ml/mr 里，
        #   **大的一侧是"改画布时凭空多出来的空白"，小的一侧才是设计边距**
        #   （实测那份成片 ml=457200 / mr=3505200，7.7 倍）。
        #   取小的一侧为基准、两侧对称铺开 ⇒ 存量污染也修得回来。
        #   ⚠️ 只在**水平**方向做：纵向边距天然不对称（标题带下方留白、页脚占位），
        #      那是设计不是故障。判据 `placeholder_geometry_stale` 同样只看水平 ——
        #      判据与修复必须是同一把尺子。
        #   ⚠️ `lo > 0` 是硬条件：`lo < 0` 意味着骨架**比画布还宽**（溢出一侧），
        #      那归 `placeholder_offcanvas` 管。不是修不了，是**不该静默修** ——
        #      溢出往往指向真的设计问题，应当先报红让人看一眼。
        lo, hi = (ml, mr) if ml <= mr else (mr, ml)
        if asym_max and lo > 0 and hi > lo * asym_max:
            ml = mr = lo
        nw, nh = ncx - ml - mr, ncy - mt - mb
        if nw <= 0 or nh <= 0:          # 旧几何比新画布还大 ⇒ **不动**（别把能看的改坏）
            return False
        if (nw, nh) == (w, h):
            return False                # 已在目标几何上 ⇒ 恒等，不改
        off.set("x", str(ml))
        off.set("y", str(mt))
        ext.set("cx", str(nw))
        ext.set("cy", str(nh))
        return True

    n = 0
    parts = []
    for m in prs.slide_masters:
        parts.append(m)
        parts.extend(m.slide_layouts)
    parts.extend(prs.slides)
    for part in parts:
        try:
            shapes = list(part.shapes)
        except Exception:
            continue
        for shp in shapes:
            try:
                if one(shp._element):
                    n += 1
            except Exception:
                continue
    return n


def main():
    pl = json.loads(sys.stdin.read())
    src = pl["pptx"]
    colors, fonts = pl["colors"], pl["fonts"]
    cx, cy = pl["size"]
    bg = pl.get("bg") or ""
    foot = bool(pl.get("footer"))
    # 骨架硬化的门槛**由主进程注入**（单源 `GEO_ASYM_MAX`）—— 见 `remap_skeleton`
    # 文档里那段"为什么走参数"。缺省 0 = 关上硬化（老行为的等价回退）。
    asym_max = float(pl.get("geo_asym_max") or 0.0)
    out = {}
    try:
        prs = Presentation(src)
        old_cx, old_cy = prs.slide_width, prs.slide_height
        prs.slide_width, prs.slide_height = Emu(int(cx)), Emu(int(cy))
        # ★ 画布改了，**骨架也得跟着改**（2026-09-21 补）。
        #   原实现只改 `sldSz`，而占位符的几何住在 master 里、还是按 4:3 摆的
        #   ⇒ 16:9 页面上内容挤在左边、右侧空 28.8%。实测**同一页自带对照**：
        #   本函数自己加的页脚铺到 95.4%，继承来的 Title/Content 只到 71.2%。
        out["geometry_remapped"] = remap_skeleton(prs, old_cx, old_cy,
                                                  int(cx), int(cy), asym_max)
        if bg:
            for sl in prs.slides:
                try:
                    sl.background.fill.solid()
                    sl.background.fill.fore_color.rgb = RGBColor.from_string(bg)
                except Exception:
                    pass
        # **幂等**：先清掉上一次套主题留下的页脚 —— 否则连调两次会叠出两层页码。
        # 只认自己命名的形状（`juanqing-footer-*`），绝不碰别人的东西。
        for sl in prs.slides:
            for sh in list(sl.shapes):
                # ⚠️ 名单与 `_COMPOSE_SCRIPT` 的 `del_named` **同源**（两个工具
                #    都认得这套页脚命名）。少一个就会出现"我画的你清不掉"的叠加。
                if sh.name in ("juanqing-footer-line", "juanqing-footer-page",
                               "juanqing-footer-label"):
                    sh._element.getparent().remove(sh._element)
        if foot:
            for i, sl in enumerate(prs.slides, 1):
                try:
                    ln = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                             Inches(0.62), Inches(6.86),
                                             Inches(12.10), Emu(9525))
                    ln.name = "juanqing-footer-line"
                    ln.fill.solid()
                    ln.fill.fore_color.rgb = RGBColor.from_string(colors.get("accent2", "8C7A5B"))
                    ln.line.fill.background()
                    ln.shadow.inherit = False
                    tb = sl.shapes.add_textbox(Inches(11.30), Inches(6.94),
                                               Inches(1.45), Inches(0.34))
                    tb.name = "juanqing-footer-page"
                    tf = tb.text_frame
                    tf.word_wrap = False
                    p = tf.paragraphs[0]
                    p.alignment = PP_ALIGN.RIGHT
                    r = p.add_run()
                    r.text = str(i)
                    r.font.size = Pt(12.0)
                    r.font.name = fonts.get("minor_latin", "")
                    r.font.color.rgb = RGBColor.from_string(colors.get("accent2", "8C7A5B"))
                except Exception:
                    pass
        # ★ **在内存里完工，不落临时文件**（2026-09-21 改）。
        #   为什么：本机按**轮次**收删除费（额度 50 点/轮），而"删一个文件"就是 1 点。
        #   旧实现走 `prs.save(tmp)` → 重写 zip → `finally:` 里删掉那个 tmp，
        #   于是**每一次 `style_deck` 都花 1 点**，而 `deck_test` 一趟要调它二十多次
        #   ⇒ 一轮跑两遍就把额度吃穿。吃穿之后的表现**极难归因**：
        #     子进程在 `os.remove` 处被守卫直接杀掉（rc=1、**不打印结果行、
        #     也没有 traceback**），`finally` 兜不住"被杀"
        #     ⇒ 调用方只看到 `ok=False「套主题未完成」`，
        #     而**只读的那些断言照旧全过** —— 看起来像"代码坏了"，其实是环境额度没了。
        #   实测证据（那一轮的 stderr 尾巴）：
        #     `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":61,"threshold":50}`
        #   ★ 而且同一份代码**时而放行、时而被杀**（几分钟内两种结果都见到了）
        #     —— 不稳定比稳定失败更坏：它让"重跑一遍"变成一个假答案。
        #   治法不是"把删除挪个地方"，是**根本不需要删**：
        #     `python-pptx` 的 `save()` 本来就收文件对象，直接写进 `BytesIO`。
        #   ⚠️ 这条也顺带符合本项目那条底线：**清场一律「挪」不「删」**
        #     （`_tkit.py` 开头就写着"测试里不许再写 unlink / rmtree / os.remove"）
        #     —— 改完这一处，`rongke/` 包里**一个 `os.remove` 都不剩**。
        buf = io.BytesIO()
        prs.save(buf)
        out["pres_saved"] = True
    except Exception as e:
        out["error"] = "python-pptx 阶段失败：%s: %s" % (type(e).__name__, e)
        print(json.dumps(out, ensure_ascii=False)); return

    try:
        with zipfile.ZipFile(buf) as zin, zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "ppt/theme/theme1.xml":
                    data, hit = patch_theme(data, colors, fonts)
                    out["theme_hit"] = hit
                zout.writestr(item, data)
        out["zip_rewritten"] = True
    except Exception as e:
        out["error"] = "重写 zip 阶段失败：%s: %s" % (type(e).__name__, e)
    print(json.dumps(out, ensure_ascii=False))

main()
'''


# ══════════════════════════════════════════════════════════════════════
# `compose_deck` 的子进程脚本 —— **按设计资产排版**
# ══════════════════════════════════════════════════════════════════════
#
# 与 `_STYLE_SCRIPT` 的关系：两者**共享同一套页脚命名**与同一个 `patch_theme`
#   （这里必须重写一份 —— 子进程是独立解释器，引用不到模块里的那个函数）。
#   ⚠️ 所以 `patch_theme` 与 `del_named` 的名单**改动时必须两处一起改**；
#      `deck_test` 有一条断言扫这两段源码盯着它（防 H 型）。
#
# 它与 `style_deck` 的分工：
#   · `style_deck`   —— 快：换主题 + 页脚，**不碰版面结构**。
#   · `compose_deck` —— 精：按 `design.PAGE_KINDS` 把每页**重排到版面网格上**，
#                      并补装饰。**只改几何与字号，绝不删内容**。
#   两者**取其一**（同时跑是双重坐标系，`remap_skeleton` 的增量语义会和这里的
#   绝对定位打架）。配方里写明"二选一"。
_COMPOSE_SCRIPT = r'''
import io
import json
import sys
import zipfile

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def q(t):
    return "{%s}%s" % (A, t)


def patch_theme(blob, colors, fonts):
    """改 theme1.xml：12 个色槽 + majorFont/minorFont 的 latin/ea。

    ⚠️ 与 `_STYLE_SCRIPT` 里那份是同一段逻辑（子进程引用不到模块函数，
       只能各写一份）⇒ **改这里必须同时改那边**。
    """
    root = etree.fromstring(blob)
    hit = {"colors": [], "fonts": []}
    for scheme in root.iter(q("clrScheme")):
        for slot in scheme:
            name = etree.QName(slot).localname
            if name not in colors:
                continue
            for ch in list(slot):
                slot.remove(ch)
            el = etree.SubElement(slot, q("srgbClr"))
            el.set("val", colors[name].upper())
            hit["colors"].append(name)
        break
    for scheme in root.iter(q("fontScheme")):
        for fam in scheme:
            key = "major" if etree.QName(fam).localname == "majorFont" else "minor"
            for slot in fam:
                s = etree.QName(slot).localname
                kk = key + "_" + s
                if kk in fonts:
                    slot.set("typeface", fonts[kk])
                    hit["fonts"].append(kk)
        break
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=True), hit


# 视觉类形状枚举。⚠️ 用 `getattr` 兜底而不是硬写成员名 ——
# `python-pptx` 各版本的成员名会变（实测这一版**没有** `OLE_OBJECT`），
# 硬写会让脚本在 `kind_of` 里 AttributeError 崩掉（本轮第一次实跑就是这么崩的）。
_VISUAL_TYPES = set()
for _n in ("PICTURE", "CHART", "TABLE", "MEDIA", "OLE_OBJECT",
           "EMBEDDED_OLE_OBJECT", "LINKED_OLE_OBJECT"):
    _v = getattr(MSO_SHAPE_TYPE, _n, None)
    if _v is not None:
        _VISUAL_TYPES.add(_v)


def kind_of(shape):
    """把形状归类到 slot 的 `from` 词表：title / subTitle / body / visual / other。

    ★ 判据只认**形状类型**与**占位符类型**，不去猜内容 —— 猜内容（「这段看着
      像副标题」）会在边界上抖，而抖动的判据比没有判据更坏。
    """
    try:
        st = shape.shape_type
    except Exception:
        st = None
    if st is not None and st in _VISUAL_TYPES:
        return "visual"
    try:
        el = shape._element
    except Exception:
        return "other"
    for t in el.iter():
        if etree.QName(t).localname != "ph":
            continue
        ty = str(t.get("type") or "body")
        if ty in ("title", "ctrTitle"):
            return "title"
        if ty == "subTitle":
            return "subTitle"
        if ty in ("pic", "chart", "tbl", "media", "clipArt"):
            return "visual"
        if ty in ("body", "obj"):
            return "body"
        return "other"
    return "other"


def place(shape, box, E):
    """把形状搬到目标几何。**只改 off/ext，不重建** ⇒ 内容与图表都还在。"""
    l, t, w, h = box
    shape.left = Emu(E(l))
    shape.top = Emu(E(t))
    shape.width = Emu(E(w))
    shape.height = Emu(E(h))


def style_runs(shape, pt, hexc, bold):
    """给形状里**每一个 run** 设阶梯字号（可选字色/加粗）。返回设了几个。"""
    try:
        tf = shape.text_frame
    except Exception:
        return 0
    if tf is None:
        return 0
    n = 0
    try:
        paras = list(tf.paragraphs)
    except Exception:
        return 0
    for para in paras:
        for run in getattr(para, "runs", []):
            try:
                run.font.size = Pt(pt)
                if hexc:
                    run.font.color.rgb = RGBColor.from_string(hexc)
                run.font.bold = bool(bold)
                n += 1
            except Exception:
                continue
    return n


def set_anchor(shape, anchor):
    """给文本框设垂直对齐。

    ★ 为什么需要：内容带的框是**固定高度**（472px），而正文常常只占上半截
      ⇒ 全挤在顶部、下面一大片空，页面看着"头重脚轻"。
      设成 `middle` 让内容块在带内居中 —— 这是**渲染出来才看得见**的问题
      （第一版没设，13 页里有 11 页都是"内容吊在顶上"）。
    """
    if not anchor:
        return
    try:
        tf = shape.text_frame
    except Exception:
        return
    if tf is None:
        return
    try:
        tf.vertical_anchor = {"middle": MSO_ANCHOR.MIDDLE,
                              "bottom": MSO_ANCHOR.BOTTOM}.get(
                                  anchor, MSO_ANCHOR.TOP)
    except Exception:
        pass


def set_bullet(shape, want_bullet):
    """给文本框里**每一段**显式写明"要不要项目符号"。返回设了几段。

    ★ 为什么必须有（2026-09-21 **渲染出来才看见**）：`a:pPr` 里不写
      `a:buNone`/`a:buChar` 时，项目符号去**版式**的 `lstStyle` 取值 ⇒
      "这行前面有没有点"由**这份 deck 的来路**决定，不由我们决定。

      实测症状：参考样例的**章节页副标题**被套上了项目符号 ——
      因为那一页的副标题槽吃的是一个 `body` 占位符，
      而 `body` 占位符在版式里是带点的列表。
      同一份内容、换成"没有列表样式的版式"来建，**点就没了**。

      ⇒ 和 `set_align` 同一条纪律：**版面规范必须显式拥有它。**
        判据也查不出这一类（多一个点不越界、不超字号）。

    口径是**按 slot 的 role** 决定，不是按形状的出身 —— 这正是设计资产的意思：
    同一个 `body` 占位符，放进 `content` 的正文槽就该有点，
    放进 `section` 的副标题槽就不该有。
    """
    try:
        tf = shape.text_frame
    except Exception:
        return 0
    if tf is None:
        return 0
    n = 0
    try:
        paras = list(tf.paragraphs)
    except Exception:
        return 0
    for p in paras:
        try:
            pPr = p._pPr if p._pPr is not None else p._p.get_or_add_pPr()
            # 先清掉可能残留的符号声明，再按需要写死
            for tag in ("buNone", "buChar", "buAutoNum"):
                for el in pPr.findall(q(tag)):
                    pPr.remove(el)
            if want_bullet:
                # 用**字符**符号而不是自动编号：编号会随段落数变，
                # 而设计稿要的是稳定的圆点。
                bu = pPr.makeelement(q("buChar"), {"char": "•"})
            else:
                bu = pPr.makeelement(q("buNone"), {})
            # ⚠️ DrawingML 的 `a:pPr` 子元素**有序**：符号声明必须排在
            #    `a:tabLst`/`a:defRPr`/`a:extLst` **之前**。
            #    直接 append 到末尾会让 schema 顺序出错（PowerPoint 可能整个
            #    忽略这一项 ⇒ 又变回"继承说了算"，而我们是查不出来的）。
            pPr.insert_element_before(bu, "a:tabLst", "a:defRPr", "a:extLst")
            n += 1
        except Exception:
            continue
    return n


def set_align(shape, align):
    """给文本框里**每一段**设显式水平对齐。返回设了几段。

    ★ 为什么必须有（2026-09-21 实测，不是推想）：`a:pPr/@algn` **缺省**时
      PowerPoint 去**母版/版式**取值 ⇒ 正文左对齐还是居中由「这份 deck 的
      来路」决定。实测那 13 页 deck 的 `algn` **全是 None**，收束页的正文
      因此被版式居中，与左侧 72px 强调整线打架。
      ⇒ 版面规范必须**显式拥有**它，不能让继承替我们决定。
    """
    if not align:
        return 0
    try:
        tf = shape.text_frame
    except Exception:
        return 0
    if tf is None:
        return 0
    tgt = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER,
           "right": PP_ALIGN.RIGHT}.get(str(align))
    if tgt is None:
        return 0
    n = 0
    try:
        for p in tf.paragraphs:
            p.alignment = tgt
            n += 1
    except Exception:
        return n
    return n


def del_named(sl, prefixes):
    """按名字前缀清掉自己上一次留下的装饰/标记 —— **幂等**就靠这一步。"""
    k = 0
    for sh in list(sl.shapes):
        try:
            nm = sh.name or ""
        except Exception:
            continue
        if any(nm.startswith(p) for p in prefixes):
            sh._element.getparent().remove(sh._element)
            k += 1
    return k


def add_rect(sl, box, hexc, name, E):
    l, t, w, h = box
    sh = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                             Emu(E(l)), Emu(E(t)),
                             Emu(max(E(w), 1)), Emu(max(E(h), 1)))
    sh.name = name
    try:
        sh.fill.solid()
        sh.fill.fore_color.rgb = RGBColor.from_string(hexc or "000000")
        sh.line.fill.background()
        sh.shadow.inherit = False
    except Exception:
        pass
    return sh


def add_text(sl, box, text, pt, hexc, name, align, font_latin, E):
    l, t, w, h = box
    tb = sl.shapes.add_textbox(Emu(E(l)), Emu(E(t)), Emu(E(w)), Emu(E(h)))
    tb.name = name
    try:
        tf = tb.text_frame
        tf.word_wrap = True
        tf.clear()                      # 幂等：清掉上一次写的
        p = tf.paragraphs[0]
        # ⚠️ 这里原来只判 right/center —— **`left` 掉进 else 什么都不写**，
        #    于是"左对齐"退化成"不设值"，而 python-pptx 不写 `algn` 就等于
        #    交给版式决定。**症状是"我明明让它左对齐，它却报了未设对齐"。**
        #    这正是 `set_align` 要给每个文字形状显式写 `algn="l"` 的原因。
        _tgt = {"right": PP_ALIGN.RIGHT, "center": PP_ALIGN.CENTER,
                "left": PP_ALIGN.LEFT}.get(str(align))
        if _tgt is not None:
            p.alignment = _tgt
        r = p.add_run()
        r.text = str(text)
        r.font.size = Pt(pt)
        if hexc:
            r.font.color.rgb = RGBColor.from_string(hexc)
        if font_latin:
            r.font.name = font_latin
        # 页脚/落款**不要项目符号** —— 同样要**显式写死**，不能交给版式
        # （理由见 `set_bullet`；渲染出来才看得见的那一类）。
        set_bullet(tb, False)
    except Exception:
        pass
    return tb


def main():
    pl = json.loads(sys.stdin.read())
    src = pl["pptx"]
    pages = pl["pages"]
    D = pl["design"]
    colors = pl["colors"]
    fonts = pl["fonts"]
    bg = pl.get("bg") or ""
    foot = bool(pl.get("footer"))
    foot_label = str(pl.get("footer_label") or "")
    target = pl.get("out") or src

    EMU = int(D["px_emu"])
    # 缺省水平对齐：**故意用 [] 而不是 .get()** —— 少这个键说明 payload 结构变了，
    # 当场 KeyError ⇒ rc≠0 ⇒ `compose_deck` 报"核不了"。**不许静默兜一个默认值**
    # （兜底就等于第二个单源：改了 design.ALIGN_DEFAULT、这里还是硬编码的旧值）。
    align_default = D["align_default"]
    # 哪些 role 是"列表"（要项目符号）。同一条纪律：**故意用 [] 而不是 .get()**。
    bullet_roles = D["bullet_roles"]

    def E(px):
        return int(round(float(px) * EMU))

    def hex_of(role):
        slot = D["color_roles"].get(role) if role else None
        if not isinstance(slot, str):
            return None
        return colors.get(slot)

    out = {}
    try:
        prs = Presentation(src)
    except Exception as e:
        out["error"] = "打不开这份 pptx：%s: %s" % (type(e).__name__, e)
        print(json.dumps(out, ensure_ascii=False))
        return

    slides = list(prs.slides)
    if len(pages) != len(slides):
        out["error"] = ("pages 有 %d 项，而 deck 有 %d 页 —— **必须一一对应**"
                        "（第 i 项配第 i 页）。"
                        % (len(pages), len(slides)))
        print(json.dumps(out, ensure_ascii=False))
        return

    canvas = D["canvas_px"]
    prs.slide_width = Emu(E(canvas[0]))
    prs.slide_height = Emu(E(canvas[1]))

    F = D["footer"]
    ladder_pt = D["ladder_pt"]
    bold_keys = set(D["bold_keys"])
    latin = fonts.get("minor_latin", "")
    page_report = []

    for i, sl in enumerate(slides, 1):
        spec = pages[i - 1]
        kind = spec["kind"]
        pk = D["page_kinds"][kind]

        # ① 幂等：先清掉自己上一次留下的东西（**只认自己的命名**，不碰别人的）
        del_named(sl, ("rk-kind-", "rk-decor-",
                       "juanqing-footer-line", "juanqing-footer-page",
                       "juanqing-footer-label"))

        # ② 背景
        if bg:
            try:
                sl.background.fill.solid()
                sl.background.fill.fore_color.rgb = RGBColor.from_string(bg)
            except Exception:
                pass

        # ③ 分发：把页面上的形状按 `from` 词表分派到 slot，逐个搬到设计几何上
        pool = list(sl.shapes)
        used = set()
        placed = {}
        aligned = 0
        bulleting = 0
        for slot in pk["slots"]:
            pick = None
            for want in (slot.get("from") or ()):
                for sh in pool:
                    if id(sh) in used:
                        continue
                    if kind_of(sh) == want:
                        pick = sh
                        break
                if pick is not None:
                    break
            if pick is None:
                continue
            used.add(id(pick))
            try:
                place(pick, slot["box"], E)
            except Exception:
                continue
            sz = slot.get("size")
            if sz:
                style_runs(pick, ladder_pt[sz], hex_of(slot.get("color")),
                           sz in bold_keys)
            set_anchor(pick, slot.get("anchor"))
            # ★ 显式水平对齐：**不让版式替我们决定**（见 set_align 的注释）。
            #   注意这里设的是"段落级 algn"，与上面尺寸/颜色一样，
            #   都是**版面规范的一部分**，不是可选的润色。
            aligned += set_align(pick, slot.get("align") or align_default)
            # ★ 显式项目符号：**按 slot 的 role 决定**，不按形状出身。
            #   同一个 `body` 占位符，在 content 的正文槽里该有点，
            #   在 section 的副标题槽里不该有（见 `set_bullet` 的注释）。
            bulleting += set_bullet(pick, str(slot["role"]) in bullet_roles)
            placed[slot["role"]] = kind_of(pick)

        # ④ 装饰（先按名清过 ⇒ 幂等）
        for dec in pk.get("decor", []):
            if dec.get("shape") == "rect":
                add_rect(sl, dec["box"], hex_of(dec.get("color")), dec["name"], E)

        # ⑤ 页型标记：1px 透明矩形。复测靠它认页型（**不靠位置反推**）
        mark = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                   Emu(0), Emu(0), Emu(EMU), Emu(EMU))
        mark.name = "rk-kind-" + kind
        try:
            mark.fill.background()
            mark.line.fill.background()
            mark.shadow.inherit = False
        except Exception:
            pass

        # ⑥ 通用页脚（封面/章节/收束页不带 —— 那三页带页脚很业余）
        if foot and pk.get("chrome"):
            add_rect(sl, F["rule"]["box"], hex_of(F["rule"].get("color")),
                     F["rule"]["name"], E)
            if foot_label:
                add_text(sl, F["label"]["box"], foot_label,
                         ladder_pt[F["label"]["size"]],
                         hex_of(F["label"].get("color")),
                         F["label"]["name"],
                         F["label"].get("align") or align_default, latin, E)
            add_text(sl, F["page"]["box"], str(i),
                     ladder_pt[F["page"]["size"]],
                     hex_of(F["page"].get("color")),
                     F["page"]["name"], "right", latin, E)

        page_report.append({"page": i, "kind": kind,
                            "placed": placed,
                            "aligned": aligned,
                            "bulleted": bulleting,
                            "slots_total": len(pk["slots"])})

    out["pages"] = page_report
    out["canvas"] = [int(canvas[0]), int(canvas[1])]

    # ★ 在内存里完工，不落临时文件（与 `_STYLE_SCRIPT` 同一条血债：
    #   本机按轮次收删除费，额度 50 点/轮；"删一个临时文件"就是 1 点。
    #   根本不需要删 —— `save()` 本来就收文件对象。）
    try:
        buf = io.BytesIO()
        prs.save(buf)
        out["pres_saved"] = True
    except Exception as e:
        out["error"] = "python-pptx 阶段失败：%s: %s" % (type(e).__name__, e)
        print(json.dumps(out, ensure_ascii=False))
        return

    try:
        with zipfile.ZipFile(buf) as zin, \
                zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "ppt/theme/theme1.xml":
                    data, hit = patch_theme(data, colors, fonts)
                    out["theme_hit"] = hit
                zout.writestr(item, data)
        out["zip_rewritten"] = True
    except Exception as e:
        out["error"] = "重写 zip 阶段失败：%s: %s" % (type(e).__name__, e)
    print(json.dumps(out, ensure_ascii=False))


main()
'''


@dataclass
class StyleDeck:
    """`style_deck` 的结果。**自称成功不算数** —— 见 `style_deck` 的复测一节。"""
    ok: bool = False
    pptx: str = ""
    theme: str = ""
    problems: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    bytes_before: int = 0
    bytes_after: int = 0
    python_exe: str = ""
    rc: int = -1
    stderr_tail: str = ""
    aspect_before: float | None = None
    aspect_after: float | None = None
    theme_before: str = ""
    theme_after: str = ""
    fonts_ratio_before: float | None = None
    fonts_ratio_after: float | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "pptx": self.pptx, "theme": self.theme,
            "problems": self.problems, "changed": self.changed,
            "bytes_before": self.bytes_before, "bytes_after": self.bytes_after,
            "python_exe": self.python_exe, "rc": self.rc,
            "stderr_tail": self.stderr_tail,
            "aspect_before": self.aspect_before, "aspect_after": self.aspect_after,
            "theme_before": self.theme_before, "theme_after": self.theme_after,
            "fonts_set_ratio_before": self.fonts_ratio_before,
            "fonts_set_ratio_after": self.fonts_ratio_after,
        }


    def format_for_model(self) -> str:
        """给模型看的一段话。

        ⚠️ **成功也要把复测数字摆出来**，而且只说"复测读回来的"。
        本次的教训正是"工具自称成功"完全不可信 —— 上游那个空壳回的是
        `{"success": true, "slides_affected": 13}`。所以这里**不写**"已应用主题"，
        只写字节数、比例、主题名这些**别人能复算**的量。
        """
        def _n(x, nd: int = 3) -> str:
            return "?" if x is None else f"{x:.{nd}f}"

        ok = "成功" if self.ok else "**未完成**"
        L = [f"【套主题 {ok}】{self.pptx}（主题 {self.theme}）"]
        if self.problems:
            L += ["", "⛔ 问题："] + [f"  · {x}" for x in self.problems]
        if self.ok:
            L += ["", "改动（**复测读回来的**，不是脚本的自述）："]
            L += [f"  · {c}" for c in self.changed]
            L += ["",
                  f"  文件 {self.bytes_before} → {self.bytes_after} 字节"
                  f"（若两者相同 ⇒ 其实没改成功）",
                  f"  复测：画布比例 {_n(self.aspect_before)} → {_n(self.aspect_after)}；"
                  f"主题 {self.theme_before or '(读不到)'} → {self.theme_after or '(读不到)'}；"
                  f"显式字体 run 占比 {_n(self.fonts_ratio_before)} → {_n(self.fonts_ratio_after)}"]
            L += ["",
                  "⚠️ 改过 deck ⇒ 之前的体检报告**作废**，交付前必须**重跑** `audit_deck`。",
                  "⚠️ 套完**不要**再调 `save_presentation` 存同一份 deck —— 服务端内存里"
                  "那份没套主题，一存盘会把主题整片抹回去（与写备注同一条禁忌）。"]
        return "\n".join(L)


def style_deck(
    pptx: str | Path,
    theme: str = DEFAULT_DECK_THEME,
    footer: bool = True,
    workdir: str | Path | None = None,
    python_exe: str | None = None,
    cfg=None,
    timeout: float | None = None,
) -> StyleDeck:
    """给已有 .pptx 套上视觉主题（**真改 XML**，不是改几个形状的属性）。

    ## 为什么非要有这个方法（2026-09-21 实测，不是推想）

    用户看完一份跑通的 13 页 deck 只说了一句"**那能做的这么丑**"。
    量下来，丑不是审美问题，是三件事同时缺位：

      ① **起手就是空白默认模板**：`create_presentation` 出的是 python-pptx
         默认模板 —— **4:3**、Calibri、白底。那份 deck 的字体/字号/颜色
         **全部"继承"**，11 页是同一个 "Title and Content"，全篇 0 张图。
      ② **契约与门一个字都没提视觉**：第 9 条门读的 `audit_deck` 报告只覆盖
         结构（页数/讲稿/越界/密度）⇒ **"丑"在这个系统里是合格的**。
      ③ **想救也没救**：看着最对口的 `apply_professional_design(operation="theme")`
         是**空壳** —— 它回 `{"success": true, "slides_affected": 13}`，
         而实测 `theme1.xml` 的 md5、尺寸、part 数**一个都没变**。
         ⇒ 「我以为我改好了」这条路本身是假的。

    ⇒ 结论：**加一句"要好看"的纪律没有用**（纪律没有判据兜底就是空转）。
       必须同时给 **一个真干活的工具** 和 **一条为视觉承重的判据**。
       本函数是前者；后者见 `DeckAuditor` 的 `visual` 一节。

    ## 它到底改了什么（可复算）

    · 画布尺寸 → **16:9**（12192000×6858000 EMU，PowerPoint 标准值）
    · `ppt/theme/theme1.xml` → 12 个色槽换成主题色、major/minor 字体换掉
      （图表、SmartArt、占位符的默认色都会跟着变 —— 这是"改主题"相对
      "逐个形状涂色"的全部意义）
    · 每页 → 绢色背景 + 页脚细线 + 右下页码

    ## 写完不自我采信

    子进程 `rc=0` **不等于**改成了 —— 本次那个空壳工具就是活证据。
    所以写完用 `probe_pptx` **独立复测**（标准库逐字节读回，与第 9 条门
    **同一把尺子**），核对：尺寸真的变了？主题真的换上了？复测做不成 ⇒
    `ok=False` 并说明**核不了**，绝不渲染成"套好了"。
    """
    res = StyleDeck(pptx=str(pptx or ""), theme=str(theme or ""))
    th = DECK_THEMES.get(str(theme or ""))
    if th is None:
        res.problems.append(
            f"不认识的主题 `{theme}`。可选：{', '.join(sorted(DECK_THEMES))}。\n"
            f"   （每套主题的色值与字体见 `DECK_THEMES`，不要自己编色号 ——"
            f"判据按同一份定义核。）")
        return res

    # ── ① 路径：**先 resolve 再判围栏**（与 write_slide_notes 同一道关）──
    root = Path(workdir).resolve() if workdir else Path.cwd().resolve()
    raw = str(pptx or "").strip()
    if not raw:
        res.problems.append("`pptx` 不能为空：给工作区里 .pptx 的路径（如 \"deck.pptx\"）。")
        return res
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    try:
        p = p.resolve()
    except Exception as e:
        res.problems.append(f"路径无法解析：{raw}（{type(e).__name__}: {e}）")
        return res
    if p != root and root not in p.parents:
        res.problems.append(
            f"⛔ 越界：{p}\n   本任务工作区是 {root}，只能改它里面的文件。")
        return res
    res.pptx = str(p)
    if not p.is_file():
        res.problems.append(f"文件不存在：{raw}")
        return res

    before = probe_pptx(p)
    if before.problems or before.slide_count == 0:
        res.problems.append("这**不是**一份能改的 .pptx："
                            + "；".join(before.problems or ["0 页"]))
        return res
    res.aspect_before = before.aspect
    res.theme_before = before.theme_name()
    res.fonts_ratio_before = before.fonts_set_ratio
    try:
        res.bytes_before = p.stat().st_size
    except Exception:
        res.bytes_before = 0

    # ── ② 解释器 ─────────────────────────────────────────────
    explicit = python_exe or (getattr(cfg, "python", None) if cfg is not None else None)
    py = find_notes_python(explicit)
    res.python_exe = py or str(explicit or "(未指定)")
    if py is None:
        res.problems.append(
            "**套主题的能力不可用**：找不到装了 python-pptx 的解释器。\n"
            "   ⇒ 本任务这份 deck 只能保持默认外观。**不要反复重试** ——"
            "请在交付时如实说明。")
        return res

    # ── ③ 跑 ────────────────────────────────────────────────
    to = float(timeout or getattr(cfg, "timeout", None) or 180.0)
    payload = json.dumps({
        "pptx": str(p), "colors": th["colors"], "fonts": th["fonts"],
        "size": list(SLIDE_16_9), "bg": th["colors"].get("lt1", ""),
        "footer": bool(footer),
        # 骨架硬化门槛：**从 `GEO_ASYM_MAX` 注入**给子进程（那边引用不到模块常量）。
        # 判据 / 复测 / 修复三处因此共用同一个数，改一处即三处同变（防 H 型）。
        "geo_asym_max": GEO_ASYM_MAX,
    }, ensure_ascii=False)
    try:
        r = subprocess.run([py, "-c", _STYLE_SCRIPT], input=payload.encode("utf-8"),
                           capture_output=True, timeout=to)
    except subprocess.TimeoutExpired:
        res.problems.append(
            f"套主题超时（{to:.0f}s）—— 脚本被中断，文件**可能只改了一半**。"
            f"请先重新读一遍这份 deck 再决定下一步。")
        return res
    except Exception as e:
        res.problems.append(f"起不了子进程（{py}）：{type(e).__name__}: {e}")
        return res
    res.rc = int(r.returncode)
    res.stderr_tail = (r.stderr or b"").decode("utf-8", "replace")[-800:]

    data = None
    for line in reversed((r.stdout or b"").decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except Exception:
                continue
    if not isinstance(data, dict):
        res.problems.append("子进程没给出可解析的结果行 ⇒ 改没改**核不了**。"
                            f"（rc={res.rc}）stderr 尾部：{res.stderr_tail or '(空)'}")
        return res
    if data.get("error"):
        res.problems.append(f"套主题失败：{data['error']}")
        return res

    # ── ④ 独立复测：**与第 9 条门同一把尺子** ─────────────────
    after = probe_pptx(p)
    if after.problems or after.slide_count == 0:
        res.problems.append("改完读不回来了 ⇒ 文件可能已损坏："
                            + "；".join(after.problems or ["0 页"]))
        return res
    res.aspect_after = after.aspect
    res.theme_after = after.theme_name()
    res.fonts_ratio_after = after.fonts_set_ratio
    try:
        res.bytes_after = p.stat().st_size
    except Exception:
        res.bytes_after = 0

    if after.is_widescreen is not True:
        res.problems.append(
            f"复测不过：画布**仍是 {before.slide_cx}×{before.slide_cy} EMU**"
            f"（aspect={after.aspect}），没有变成 16:9。")
        return res
    if after.theme_name() != str(theme):
        res.problems.append(
            f"复测不过：主题色**没有落到 theme1.xml**（读回来是 "
            f"`{after.theme_name() or '读不到'}`，期望 `{theme}`）。\n"
            f"   ⚠️ 这正是 `apply_professional_design` 那种**假成功**的形状："
            f"自称改了、文件没动。本工具必须在**这一关**上被抓住。")
        return res

    # ★ 骨架几何复测（2026-09-21 补）：**画布改了、骨架必须同源**。
    #   为什么单列一关：原实现恰恰只改了画布而没改骨架，而且 `res.changed`
    #   里写的是"画布 4:3 → 16:9"，读起来像全干完了 —— **干了一半却报全功**，
    #   比"假成功"更隐蔽（假成功是没动，这个是动了但没动全）。
    #   ⇒ 与判据 `placeholder_geometry_stale` **同一把尺子**（两处不同源即 H 型）。
    if after.master_geo and after.slide_cx:
        W = after.slide_cx
        bad: list[str] = []
        for geo in after.master_geo:
            if geo.get("ph") not in CONTENT_PH_TYPES:
                continue
            ml = geo["x"]
            mr = W - (geo["x"] + geo["cx"])
            lo, hi = min(ml, mr), max(ml, mr)
            if lo < 0 or (lo > 0 and hi > lo * GEO_ASYM_MAX):
                bad.append(str(geo.get("ph")))
        if bad:
            res.problems.append(
                f"复测不过：骨架几何仍与画布**不同源**（{bad}）—— 画布已经是 16:9，"
                f"而占位符还按另一个尺寸摆着，内容会挤在一侧、另一边空一大块。")
            return res

    n_geo = int(data.get("geometry_remapped") or 0)
    res.changed = [
        f"画布 4:3 → 16:9（{before.slide_cx}×{before.slide_cy} → "
        f"{SLIDE_16_9[0]}×{SLIDE_16_9[1]} EMU）",
        (f"**骨架跟着画布重排**（{n_geo} 个内容占位符：保持边距、拉伸宽度）"
         if n_geo else "骨架几何已在目标画布上，无需重排"),
        f"主题色 {len(th['colors'])} 槽写入 theme1.xml（{after.theme_name()}）",
        "主题字体 major/minor 换为 宋体/微软雅黑",
        f"每页绢色背景 + 页脚页码（{after.slide_count} 页）",
    ]
    res.ok = True
    return res


# ══════════════════════════════════════════════════════════════════════
# `compose_deck` —— 按设计资产**排版**
# ══════════════════════════════════════════════════════════════════════

def _kind_marks(path: str | Path) -> dict[int, str]:
    """读回每页的页型标记（`rk-kind-<kind>`）。**复测用，不信脚本自述。**

    ★ 为什么要有标记这回事：复测若靠"按位置反推页型"，就会在"标题恰好也
      落在 36px"这种巧合上认错。标记是**工具自己钉下的承重证据** ——
      读完能认出它，才说明 `_COMPOSE_SCRIPT` 真的走到了那一页。
    """
    marks: dict[int, str] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                m = re.match(r"ppt/slides/slide(\d+)\.xml$", name)
                if not m:
                    continue
                no = int(m.group(1))
                try:
                    xml = zf.read(name).decode("utf-8", "replace")
                except Exception:
                    continue
                for mm in re.finditer(r'name="rk-kind-([a-zA-Z][a-zA-Z\-]*)"', xml):
                    marks[no] = mm.group(1)
    except Exception:
        return {}
    return marks


_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_NS_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _align_report(path: str | Path) -> tuple[dict[int, dict], str]:
    """读回每页**有文字的形状**是否都带**显式**水平对齐。复测用，不信脚本自述。

    返回 `({页号: {"text": 有字形状数, "loose": 没设显式的个数, "grp": 组合数}}, "")`。

    ★ 为什么要复测这一项：`a:pPr/@algn` **缺省**时 PowerPoint 去**母版/版式**
      取值 ⇒ "正文左对齐还是居中"由**这份 deck 的来路**决定，不由版面规范决定。
      实测那份 13 页 deck 的 `algn` **全是 None**，收束页的正文因此被版式居中。

    三条口径（每条都为了**不误报**）：

      · **「有字」的定义**：`p:txBody` 里至少有一个非空 `a:t`。
        只数形状数会把装饰矩形与 1px 页型标记也算进去 —— 它们**本来就没文字**，
        数进来就是误报（"读不到 ≠ 不符合"）。
      · **只数顶层 `p:sp`**：与 `sl.shapes` 的覆盖面一致（脚本能搬到的就是这些）。
        **组合形状内部的文字不在覆盖面内** —— 所以单独计数、如实上报，
        **不假装查过了**。
      · **不做位置反推**：只问"有没有显式值"，不猜哪个形状"应该"左对齐。
    """
    rep: dict[int, dict] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist()
                     if re.match(r"ppt/slides/slide\d+\.xml$", n)]
            for name in names:
                no = int(re.match(r"ppt/slides/slide(\d+)\.xml$", name).group(1))
                try:
                    root = ET.fromstring(zf.read(name))
                except Exception:
                    continue
                tree = root.find(f"{_NS_P}cSld/{_NS_P}spTree")
                if tree is None:
                    continue
                n_text = n_loose = 0
                for sp in tree.findall(f"{_NS_P}sp"):
                    body = sp.find(f"{_NS_P}txBody")
                    if body is None:
                        continue
                    txt = "".join(t.text or "" for t in body.iter(f"{_NS_A}t"))
                    if not txt.strip():
                        continue
                    n_text += 1
                    for para in body.findall(f"{_NS_A}p"):
                        pr = para.find(f"{_NS_A}pPr")
                        if pr is None or not pr.get("algn"):
                            n_loose += 1
                            break
                n_grp = len(tree.findall(f"{_NS_P}grpSp"))
                if n_text or n_grp:
                    rep[no] = {"text": n_text, "loose": n_loose, "grp": n_grp}
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"
    return rep, ""


def _bullet_report(path: str | Path) -> tuple[dict[int, dict], str]:
    """读回每页**有文字的段落**是否都带**显式**项目符号声明。复测用。

    返回 `({页号: {"paras": 有字段落数, "loose": 没写符号声明的段落数}}, "")`。

    ★ 为什么与 `_align_report` 成对存在：`a:pPr` 里不写
      `a:buNone`/`a:buChar`/`a:buAutoNum` 时，项目符号与水平对齐**一样**会去
      **版式**的 `lstStyle` 取值 ⇒ 症状是"章节页副标题上冒出一个孤零零的「•」"。

    ★ 为什么是**段落级**而不是形状级（与 `_align_report` 的差别，也是它的教训）：
      一个形状里可能有的段带点、有的不带。按形状只判"第一个段"会漏掉后面所有段
      （`_align_report` 用的是 `break` —— 那是有意的：对齐是形状级属性）。
      **项目符号是段落级属性**，所以这里必须逐段数。
      同一条通用问法：**「这个属性的定义域，是形状还是段落？」**

    口径同 `_align_report`：只数顶层 `p:sp`、只要有非空 `a:t` 的段落；
    组合形状内部的文字不在覆盖面内，不假装查过。
    """
    rep: dict[int, dict] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist()
                     if re.match(r"ppt/slides/slide\d+\.xml$", n)]
            for name in names:
                no = int(re.match(r"ppt/slides/slide(\d+)\.xml$", name).group(1))
                try:
                    root = ET.fromstring(zf.read(name))
                except Exception:
                    continue
                tree = root.find(f"{_NS_P}cSld/{_NS_P}spTree")
                if tree is None:
                    continue
                n_par = n_loose = 0
                for sp in tree.findall(f"{_NS_P}sp"):
                    body = sp.find(f"{_NS_P}txBody")
                    if body is None:
                        continue
                    for para in body.findall(f"{_NS_A}p"):
                        txt = "".join(t.text or "" for t in para.iter(f"{_NS_A}t"))
                        if not txt.strip():
                            continue
                        n_par += 1
                        pr = para.find(f"{_NS_A}pPr")
                        if pr is None or not any(
                                pr.find(f"{_NS_A}{t}") is not None
                                for t in ("buNone", "buChar", "buAutoNum")):
                            n_loose += 1
                if n_par:
                    rep[no] = {"paras": n_par, "loose": n_loose}
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"
    return rep, ""


def _norm_page_kinds(raw) -> tuple[list[dict] | None, str | None]:
    """把模型给的 `pages` 归一成 `[{"kind": <标准名>}, …]`。

    容忍两种写法：`["cover", "content", …]` 与 `[{"kind": "cover"}, …]`。
    认不出的页型**直接报错、绝不猜** —— 猜错等于把内容排到错误的版式上，
    比明确失败坏得多。
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None, ("`pages` 必须是**逐页一个页型**的列表，"
                      "长度等于 deck 页数（如 "
                      '`[{"kind":"cover"}, {"kind":"content"}, …]`）。')
    out: list[dict] = []
    for idx, item in enumerate(raw, 1):
        if isinstance(item, str):
            kind = item
        elif isinstance(item, dict):
            kind = str(item.get("kind") or "")
        else:
            return None, (f"`pages` 第 {idx} 项既不是字符串也不是对象"
                          f"（{type(item).__name__}）。")
        k = design.resolve_kind(kind)
        if k is None:
            return None, (f"`pages` 第 {idx} 项的页型 `{kind}` 不认识。\n"
                          f"   可选：{', '.join(design.KIND_NAMES)}。"
                          f"（别自己造页型名 —— 版式几何按名字查表，"
                          f"查不到就没有版式可排。）")
        out.append({"kind": k})
    return out, None


@dataclass
class ComposeDeck:
    """`compose_deck` 的结果。**自称成功不算数** —— 见 `compose_deck` 的复测一节。"""
    ok: bool = False
    pptx: str = ""
    theme: str = ""
    kinds: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    marks: dict = field(default_factory=dict)
    align_text: int = 0
    align_groups: int = 0
    bullet_paras: int = 0
    style_changed: list[str] = field(default_factory=list)
    visuals_before: int = 0
    visuals_after: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    python_exe: str = ""
    rc: int = -1
    stderr_tail: str = ""
    aspect_before: float | None = None
    aspect_after: float | None = None
    theme_before: str = ""
    theme_after: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "pptx": self.pptx, "theme": self.theme,
            "kinds": self.kinds, "problems": self.problems,
            "changed": self.changed, "marks": self.marks,
            "style_changed": self.style_changed,
            "align_text": self.align_text,
            "align_groups": self.align_groups,
            "bullet_paras": self.bullet_paras,
            "visuals_before": self.visuals_before,
            "visuals_after": self.visuals_after,
            "bytes_before": self.bytes_before, "bytes_after": self.bytes_after,
            "python_exe": self.python_exe, "rc": self.rc,
            "stderr_tail": self.stderr_tail,
            "aspect_before": self.aspect_before, "aspect_after": self.aspect_after,
            "theme_before": self.theme_before, "theme_after": self.theme_after,
        }

    def format_for_model(self) -> str:
        """给模型看的一段话。**只说复测读回来的量**，不说"已排版"。"""
        def _n(x, nd: int = 3) -> str:
            return "?" if x is None else f"{x:.{nd}f}"

        head = "成功" if self.ok else "**未完成**"
        L = [f"【按设计规范排版 {head}】{self.pptx}（主题 {self.theme}）"]
        if self.problems:
            L += ["", "⛔ 问题："] + [f"  · {x}" for x in self.problems]
        if self.ok:
            dist: dict[str, int] = {}
            for k in self.kinds:
                dist[k] = dist.get(k, 0) + 1
            L += ["", "页型分布：" + "、".join(f"{k}×{v}" for k, v in dist.items())]
            L += ["", "改动（**复测读回来的**，不是脚本的自述）："]
            L += [f"  · {c}" for c in self.changed]
            L += ["",
                  f"  文件 {self.bytes_before} → {self.bytes_after} 字节",
                  f"  复测：画布比例 {_n(self.aspect_before)} → {_n(self.aspect_after)}；"
                  f"主题 {self.theme_before or '(读不到)'} → {self.theme_after or '(读不到)'}",
                  f"  视觉资产（图+表+图表）{self.visuals_before} → {self.visuals_after}"
                  f"（**相等才说明没丢图**）",
                  f"  显式水平对齐：{self.align_text} 个文字形状"
                  f"（**读回来**核过，不留继承）",
                  f"  页型标记 {len(self.marks)} 处：" +
                  "、".join(f"p{k}={v}" for k, v in sorted(self.marks.items()))]
            L += ["",
                  "⚠️ 改过 deck ⇒ 之前的体检报告**作废**，交付前必须**重跑** `audit_deck`。",
                  "⚠️ 排完**不要**再调 `save_presentation` 存同一份 deck —— 服务端内存里"
                  "那份没排过版，一存盘会把版面整片抹回去。"]
        return "\n".join(L)


def compose_deck(
    pptx: str | Path,
    pages,
    theme: str = DEFAULT_DECK_THEME,
    footer: bool = True,
    footer_label: str = "",
    out: str | Path | None = None,
    workdir: str | Path | None = None,
    python_exe: str | None = None,
    cfg=None,
    timeout: float | None = None,
) -> ComposeDeck:
    """按**设计资产**把每一页排到版面网格上（`design.PAGE_KINDS`）。

    ## 它解决的是什么

    用户看完一份五门全绿的 13 页组会 deck，只说了一句「**太差了，不能直接上组会**」。
    量下来根因不在模型、也不在判据 —— 是**整条链路上没有任何一处存着设计资产**：
    起手是 python-pptx 的空白 4:3 默认母版，`style_deck` 的全部打扮是
    「换 12 个色槽 + 换字体 + 一条 0.01 英寸的页脚线」。

    ⇒ **判据只能划定下限；产出上限来自资产本身。**

    所以本函数把设计师的隐性判断（三区网格、字号阶梯、色彩角色、页型几何）
    **固化成 `design.py` 里的数字**，然后**由内核照着排** ——
    模型不需要有"坐标感"（它本来也没有，见 `config.py` 的模型名）。

    ## 模型交什么

    `pages` = **逐页一个页型**，长度必须等于页数（与 `set_slide_notes` 同范式）：

        [{"kind": "cover"}, {"kind": "content"}, {"kind": "chart"}, …]

    可选页型：`cover` / `section` / `content` / `two-col` / `chart` / `closing`
    （别名见 `design.KIND_ALIASES`）。
    **内核一个字都不写正文** —— 它只搬已经在那儿的形状、给它们套设计几何。

    ## 它做什么、不做什么

    · **做**：把每页的标题/正文/图表搬到设计网格上；套字号阶梯、字色与
      **显式水平对齐**（不让版式替我们决定对齐）；补装饰（强调线、页脚）；
      把画布与主题统一。
    · **不做**：不改正文内容、不删形状、不重画图表 ——
      `place()` 只改 `a:off`/`a:ext`，**图和文字因此都还在**。
    · 与 `style_deck` **二选一**：那边是"快套主题"（不动版面结构），
      这边是"精排"（连版面一起动）。两个都调 = 双重坐标系。

    ## 写完不自我采信

    子进程 `rc=0` **不等于**排好了。所以写完用 `probe_pptx` **独立复测**
    （与第 9 条门同一把尺子）：画布真是 16:9？主题真落进 theme1.xml？
    **视觉资产前后相等（没丢图）**？每页都有页型标记（脚本真的走到了那一页）？
    **每个有文字的形状都带显式水平对齐**（不留继承）？
    复测做不成 ⇒ `ok=False` 并说明**核不了**，绝不渲染成"排好了"。
    """
    res = ComposeDeck(pptx=str(pptx or ""), theme=str(theme or ""))
    th = DECK_THEMES.get(str(theme or ""))
    if th is None:
        res.problems.append(
            f"不认识的主题 `{theme}`。可选：{', '.join(sorted(DECK_THEMES))}。")
        return res

    # ── ① 页型表（**先校验**：页型认不出就别去动文件）────────────
    norm, err = _norm_page_kinds(pages)
    if err:
        res.problems.append(err)
        return res
    res.kinds = [x["kind"] for x in norm]

    # ── ② 路径：先 resolve 再判围栏（与 style_deck / write_slide_notes 同关）──
    root = Path(workdir).resolve() if workdir else Path.cwd().resolve()
    raw = str(pptx or "").strip()
    if not raw:
        res.problems.append('`pptx` 不能为空：给工作区里 .pptx 的路径（如 "deck.pptx"）。')
        return res
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    try:
        p = p.resolve()
    except Exception as e:
        res.problems.append(f"路径无法解析：{raw}（{type(e).__name__}: {e}）")
        return res
    if p != root and root not in p.parents:
        res.problems.append(
            f"⛔ 越界：{p}\n   本任务工作区是 {root}，只能改它里面的文件。")
        return res
    res.pptx = str(p)
    if not p.is_file():
        res.problems.append(f"文件不存在：{raw}")
        return res

    target = p
    if out:
        op = Path(str(out))
        if not op.is_absolute():
            op = root / op
        try:
            op = op.resolve()
        except Exception as e:
            res.problems.append(f"`out` 路径无法解析：{out}（{type(e).__name__}）")
            return res
        if op != root and root not in op.parents:
            res.problems.append(f"⛔ `out` 越界：{op}\n   只能写到工作区 {root} 里。")
            return res
        target = op

    before = probe_pptx(p)
    if before.problems or before.slide_count == 0:
        res.problems.append("这**不是**一份能排版的 .pptx："
                            + "；".join(before.problems or ["0 页"]))
        return res
    if len(norm) != before.slide_count:
        res.problems.append(
            f"`pages` 有 {len(norm)} 项，而这份 deck 有 {before.slide_count} 页 ——"
            f" **必须一一对应**（第 i 项配第 i 页）。\n"
            f"   请先 `list_slide_templates` / 读一遍 deck 确认页数，再重调。")
        return res
    res.aspect_before = before.aspect
    res.theme_before = before.theme_name()
    res.visuals_before = before.pictures + before.charts + before.tables
    try:
        res.bytes_before = p.stat().st_size
    except Exception:
        res.bytes_before = 0

    # ── ③ 解释器 ─────────────────────────────────────────────
    explicit = python_exe or (getattr(cfg, "python", None) if cfg is not None else None)
    py = find_notes_python(explicit)
    res.python_exe = py or str(explicit or "(未指定)")
    if py is None:
        res.problems.append(
            "**排版能力不可用**：找不到装了 python-pptx 的解释器。\n"
            "   ⇒ 这份 deck 只能保持默认外观。**不要反复重试** ——"
            "请在交付时如实说明。")
        return res

    # ── ④ 第一步：**快套**（画布 → 16:9 ＋ 主题 ＋ **母版骨架重排**）────
    #   ★ 为什么不把 `remap_skeleton` 抄一份进 `_COMPOSE_SCRIPT`：
    #     它住在 `_STYLE_SCRIPT` 那个字符串里、跑在**独立解释器**中。抄一份就是
    #     H 型（判据那边一份、修复这边一份，改一处忘一处即静默漂移），
    #     而且 `deck_test` 有一条断言专门扫这两个脚本的源码盯着它们一致。
    #   ⇒ 直接**复用**同一个函数：本工具的语义本来就是「精排 = 快套 ＋ 版面」。
    #     少了这一步，母版占位符还按 4:3 摆着 ⇒ 判据 `placeholder_geometry_stale`
    #     照样响。**这是本工具第一版真漏掉的一步，被自己的判据当场逮住。**
    #   ⚠️ `style_deck` 是**就地改写**、没有 `out` 语义 ⇒ 传了 `out` 时必须先
    #     把原件**复制**到目标，否则"想留原件"的语义会被这一步毁掉。
    if str(target) != str(p):
        try:
            shutil.copy2(p, target)
        except Exception as e:
            res.problems.append(
                f"复制到 `out` 失败：{type(e).__name__}: {e}")
            return res
    styled = style_deck(target, theme=str(theme), footer=True,
                        workdir=root, python_exe=py, cfg=cfg)
    if not styled.ok:
        res.problems.append(
            "**第一步（快套：画布/主题/骨架）没成功**，所以没有继续排版：\n    "
            + "\n    ".join(styled.problems or ["(它没给原因)"]))
        return res
    res.style_changed = list(styled.changed)

    # ── ⑤ 第二步：按设计资产**排版** ─────────────────────────
    to = float(timeout or getattr(cfg, "timeout", None) or 240.0)
    payload = json.dumps({
        "pptx": str(target), "pages": norm,
        # 设计资产**整体注入**（子进程是独立解释器，import 不到 design 模块）。
        # 注入的是活对象的快照 ⇒ 单源，不是抄本。
        "design": design.as_payload(),
        "colors": th["colors"], "fonts": th["fonts"],
        "size": list(SLIDE_16_9), "bg": th["colors"].get("lt1", ""),
        "footer": bool(footer), "footer_label": str(footer_label or ""),
        "out": str(target) if str(target) != str(p) else "",
    }, ensure_ascii=False)
    try:
        r = subprocess.run([py, "-c", _COMPOSE_SCRIPT], input=payload.encode("utf-8"),
                           capture_output=True, timeout=to)
    except subprocess.TimeoutExpired:
        res.problems.append(
            f"排版超时（{to:.0f}s）—— 脚本被中断，文件**可能只改了一半**。"
            f"请先重新读一遍这份 deck 再决定下一步。")
        return res
    except Exception as e:
        res.problems.append(f"起不了子进程（{py}）：{type(e).__name__}: {e}")
        return res
    res.rc = int(r.returncode)
    res.stderr_tail = (r.stderr or b"").decode("utf-8", "replace")[-800:]

    data = None
    for line in reversed((r.stdout or b"").decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except Exception:
                continue
    if not isinstance(data, dict):
        res.problems.append("子进程没给出可解析的结果行 ⇒ 排没排**核不了**。"
                            f"（rc={res.rc}）stderr 尾部：{res.stderr_tail or '(空)'}")
        return res
    if data.get("error"):
        res.problems.append(f"排版失败：{data['error']}")
        return res

    # ── ⑤ 独立复测：与第 9 条门同一把尺子 ─────────────────────
    after = probe_pptx(target)
    if after.problems or after.slide_count == 0:
        res.problems.append("改完读不回来了 ⇒ 文件可能已损坏："
                            + "；".join(after.problems or ["0 页"]))
        return res
    res.aspect_after = after.aspect
    res.theme_after = after.theme_name()
    res.visuals_after = after.pictures + after.charts + after.tables
    try:
        res.bytes_after = target.stat().st_size if target.is_file() else 0
    except Exception:
        res.bytes_after = 0

    if after.is_widescreen is not True:
        res.problems.append(
            f"复测不过：画布**不是 16:9**（读回来 {after.slide_cx}×{after.slide_cy} EMU、"
            f"aspect={after.aspect}）。")
        return res
    if after.theme_name() != str(theme):
        res.problems.append(
            f"复测不过：主题色**没有落到 theme1.xml**（读回来是 "
            f"`{after.theme_name() or '读不到'}`，期望 `{theme}`）。\n"
            f"   ⚠️ 这正是 `apply_professional_design` 那种**假成功**的形状。")
        return res
    # ★ **丢图检查**：排版绝不许把模型插好的图表/图片弄丢。
    if res.visuals_after < res.visuals_before:
        res.problems.append(
            f"复测不过：**视觉资产变少了**（{res.visuals_before} → "
            f"{res.visuals_after}）—— 排版把图/表弄丢了。这是不可接受的，"
            f"请从原件重来（先 `shutil.copy` 后悔药）。")
        return res

    # ★ **页型标记检查**：脚本真的走到了每一页吗？
    marks = _kind_marks(target)
    res.marks = marks
    missing = [i for i in range(1, after.slide_count + 1) if i not in marks]
    if missing:
        res.problems.append(
            f"复测不过：第 {missing} 页没有页型标记 —— 说明脚本**没走到那些页**"
            f"（这与「改了但没改全」同形：不查就等于没改）。")
        return res
    wrong = [(i, marks[i], res.kinds[i - 1])
             for i in sorted(marks)
             if i <= len(res.kinds) and marks[i] != res.kinds[i - 1]]
    if wrong:
        res.problems.append(
            f"复测不过：页型标记与请求**对不上**：{wrong[:5]}。")
        return res

    # ★ **显式对齐复测**（2026-09-21 补）：版面规范必须**显式拥有**水平对齐。
    #   为什么单列一关：`algn` 缺省 ⇒ PowerPoint 去版式取值 ⇒ "左对齐还是居中"
    #   由这份 deck 的来路决定。脚本里 `set_align` 说设了不算数，**读回来才算**。
    align_rep, align_err = _align_report(target)
    if align_err:
        res.problems.append(
            f"复测不过：**水平对齐核不了**（{align_err}）——"
            f" 不查就等于没设（「改了但没改全」的同一形状）。")
        return res
    loose = {i: v["loose"] for i, v in align_rep.items() if v["loose"]}
    if loose:
        res.problems.append(
            f"复测不过：{len(loose)} 页上**仍有文字没设显式水平对齐**（{loose}）——"
            f" `algn` 缺省时 PowerPoint 会去**版式**取值，正文会被版式居中/右靠，"
            f"版面规范说了不算。")
        return res
    res.align_text = sum(v["text"] for v in align_rep.values())
    res.align_groups = sum(v["grp"] for v in align_rep.values())

    # ★ **显式项目符号复测**（2026-09-21 补，与对齐同一形状的病）。
    #   症状是渲染出来才看见的：章节页副标题顶上冒出一个孤零零的「•」。
    bul_rep, bul_err = _bullet_report(target)
    if bul_err:
        res.problems.append(
            f"复测不过：**项目符号核不了**（{bul_err}）——"
            f" 不查就等于没设。")
        return res
    bul_loose = {i: v["loose"] for i, v in bul_rep.items() if v["loose"]}
    if bul_loose:
        res.problems.append(
            f"复测不过：{len(bul_loose)} 页上**仍有段落没写显式项目符号声明**"
            f"（{bul_loose}）—— 不写就会去**版式**的列表样式取值，"
            f"同一份内容换个版式就会平白多出（或丢掉）圆点。")
        return res
    res.bullet_paras = sum(v["paras"] for v in bul_rep.values())

    n_kind = len(set(res.kinds))
    res.changed = [
        f"**第一步 快套**（复用 `style_deck`，{len(res.style_changed)} 项）："
        f"画布 → 16:9、主题写入 theme1.xml、**母版骨架重排**（骨架与画布同源，"
        f"判据 `placeholder_geometry_stale` 因此不响）",
        f"**第二步 精排**：{after.slide_count} 页按 {n_kind} 种页型重排到设计网格"
        f"（左右边距 {design.PAD_PX}px、标题带 0–120px、内容带 120–660px）",
        f"字号套阶梯（封面 {design.LADDER_PT['cover_title']}pt / 主标题 "
        f"{design.LADDER_PT['page_title']}pt / 正文 {design.LADDER_PT['body']}pt）",
        f"**{res.align_text} 个文字形状套显式水平对齐**"
        f"（{design.ALIGN_DEFAULT}，不留继承 —— 读回来核过）",
        f"**{res.bullet_paras} 个文字段落套显式项目符号**"
        f"（按 role 决定：{', '.join(sorted(design.BULLET_ROLES))} 带点、其余无点）",
        f"主题色 {len(th['colors'])} 槽写入 theme1.xml（{after.theme_name()}）",
        f"装饰：强调线 + 页脚（{sum(1 for k in res.kinds if k in ('content','two-col','chart'))} 页）",
        f"视觉资产保全：图+表+图表 {res.visuals_before} → {res.visuals_after}",
    ]
    if res.align_groups:
        # 如实上报**覆盖面**：组合形状内部的文字这套对齐管不到。
        # 说"都设了"而实际有盲区，就是"干了一半却报全功"。
        res.changed.append(
            f"⚠️ 覆盖边界：另有 **{res.align_groups} 个组合形状**，"
            f"其内部文字的对齐**不在本工具的覆盖面内**"
            f"（未核查，不等于「已通过」）。")
    res.ok = True
    return res


# ══════════════════════════════════════════════════════════════════════
# 品牌模板：**在模板页上填字**（保真路线，2026-09-22）
# ══════════════════════════════════════════════════════════════════════
#
# ## 为什么不是"把设计反推成主题槽"
#
# 一份学校品牌模板的价值 = 它的**全部装饰**：满版蓝底、450pt 的章节巨型数字、
# 校徽、刻意出血的照片、装饰条。把设计反推成"12 个色槽 + 六组几何"只能得到
# **近似品**，而品牌的要求是"就是它"。
# ⇒ **人怎么用模板，就让 agent 怎么用**：保留模板页、在槽上填字、多余的页删掉。
#   一个字都不重绘。
#
# ## 槽位表是**抽出来的**，不是写出来的（`deck_template_extract.py`）
#
# "页型 → 源页号"是判断（12 行，手工）；"每页有哪些槽、每槽能装多少字"是事实
# （自动抽）。⇒ 手写面从 ~120 条降到 12 条，改模板只要重跑一次抽取器。
#
# ## ★★ 模板槽位不是"版式"，是一组**带硬容量的槽**
#
# 实测这份模板：页标题 28pt/2.17in ⇒ **5 字**、章节标题 ⇒ **6 字**、
# 四栏栏标题 ⇒ **4 字**。**而且模板自己就有 2 处已溢出**（封面日期、结尾署名）。
# ⇒ 容量必须**从几何＋字号现算**（`slot_capacity`），并在**写文案之前**告诉模型。
#   第一版 PoC 就是因为没给这个判据，封面标题/章节副标题/四栏正文当场顶出去 ——
#   **判据的覆盖面就是产出的上限**（第三问）。

#: 仓库根（`rongke/deck.py` 的上两级）。槽位表默认从这里找。
ROOT = Path(__file__).resolve().parent.parent
#: 抽出来的槽位表放哪。`docs/deck-templates/<名>/slots.json`。
TEMPLATE_DIR = "docs/deck-templates"
#: 没配置时用哪份槽位表（`config.json` 里 `deck.template_name` 空着时兜底）。
DEFAULT_TEMPLATE_NAME = "zjut"


def template_name_of(cfg) -> str:
    """**「用哪几份槽位表」的唯一裁决处** —— 读 `config.json` 的 `deck.template_name`。

    ★ 为什么要有这一个函数（而不是在调用点各写一次默认值）：2026-09-22 之前
      `load_template("zjut")` 在 `loop.py` 里**写死了两处**（出稿与开工清单各一处），
      而 `deck_from_template` 内部又有第三处默认值。第二份模板（一份 72 页实测模板）
      一到，这就变成"三处真相"——换模板要记得改三个地方，漏一处就是
      「清单说的是 A 模板的页型、出稿却拿 B 模板的页号去填」这种**最难查的错**。
      ⇒ 统一从这里取；`deck.template`（.pptx 路径）与 `deck.template_name`
        （槽位表名）是**一对**，改就一起改。
    """
    name = ""
    if cfg is not None:
        name = str(getattr(cfg, "template_name", "") or "").strip()
    return name or DEFAULT_TEMPLATE_NAME
#: 缩字号的下限。低于它就宁可报错，也不交一份小到看不清的稿。
TEMPLATE_SZ_FLOOR = 9.0


def slot_capacity(w_in: float, h_in: float, pt: float | None, **kw
                  ) -> tuple[float | None, int | None, float | None]:
    """(每行 **em** 容量, 行数, 总 **em** 容量)。`pt` 为空 ⇒ 三个都 None
    （**"读不到"不是"不符合"**）。

    ★ 实现在 `rongke/slotfit.py` —— **全链唯一一份**。这里只是把它接到
      `rongke.deck` 这个名字下（只读工具、开工清单、测试都从这儿取）。
      ⚠️ **别在这里写回公式**：2026-09-22 之前这里有一份"抄来的"，
        而抄的那份与抽取器、与填充子脚本各差一点，实测漂出：
        实测模板 703 槽里 156 槽连**模板自己的原文**都判"装不下"。

    `**kw` 直接透传（`ins_emu` / `wrap` / `vert` / `min_lines`），
    与抽取器和填充子脚本是**同一个签名**。
    """
    return slotfit.slot_capacity(w_in, h_in, pt, **kw)


def load_template(name: str, root: str | Path | None = None) -> dict:
    """读一份抽好的槽位表。找不到 / 读不动 ⇒ 抛，"读不到"绝不悄悄降级成空表。"""
    base = Path(root) if root else (ROOT / TEMPLATE_DIR)
    f = base / name / "slots.json"
    if not f.is_file():
        raise FileNotFoundError(
            f"槽位表不存在：{f}\n"
            f"（它由 `python deck_template_extract.py <模板.pptx> -o {f}` 生成；"
            f"缺了它就无法在模板页上填字。）")
    return json.loads(f.read_text(encoding="utf-8"))


def template_pages(tpl: dict) -> dict[str, dict]:
    """展开成 `页id → {src, label, slots}`（`section-1..4` 各自是一条）。"""
    out: dict[str, dict] = {}
    for kind, blk in (tpl.get("pages") or {}).items():
        for e in blk.get("entries", []):
            out[e["id"]] = {"src": e["src"], "label": blk.get("label", ""),
                            "assets": e.get("assets") or {},
                            "slots": e.get("slots", [])}
    return out


def fillable_slots(slots: list[dict]) -> list[dict]:
    """可填槽 = 不是装饰锚点/固定件的那些。**判定只看 role，不看名字。**"""
    return [s for s in slots if s.get("role") not in ("anchor", "fixed", "empty")]


def _sid(slot: dict) -> str:
    """槽位在清单里的**显示名**（右对齐）。

    ⚠️ **必须走 `str()`，不能写 `:>2d`。** 槽键有两种形态：形状用整数索引、
    SmartArt 用 `d<i>`。写成 `:>2d` 会在 SmartArt 槽上抛
    `ValueError: Unknown format code 'd' for object of type 'str'`。

    ★ 实测踩过，而且代价极大：崩的是 `template_plan_text()` ——
      模型**动手写文案之前必须先调**的那个只读工具。它一死，整条品牌模板路线
      就走不通，而**函数级测试全绿**（当时的合成夹具里没有 SmartArt 槽，
      这条格式化路径压根没被走到）。⇒ `template_test` 的夹具里现在有一个
      diagram 槽专门钉住它。
    """
    return f"{str(slot.get('i', '?')):>4}"


#: ★ 清单「一次读完」的页型数上限。
#:
#: 实测（2026-09-22）：浙工大 17 页 ⇒ 全量清单 **9,708 字符**（能一次读完）；
#: 一份 72 页的实测版式库 ⇒ **36,290 字符 ≈ 18k tokens** —— 比全部 69 个工具的 schema
#: 加起来还大，而它一旦被读进上下文就是**整场任务都付**的钱。
#: ⇒ 超过这个数就只给**页型级概览**，详表（槽位 + 字数上限）**按需索取**。
#: ⚠️ 概览里那行「可填 N 槽，字数上限 a–b」不是装饰：它让模型**在选页时**
#:    就知道这一页装多少字，不至于选完才发现写不下。
PLAN_INLINE_MAX = 24


def _page_head_line(pid: str, info: dict) -> list[str]:
    """页型那一行（含资产清点与**自带示例图表**的警告）。

    ★ 详表与概览**共用这两个函数**，不许各写一遍 —— 图表那条警告是最不能丢的一句
    （只说"能填"、不说"换不了图表数据"，模型会以为这页填完字就齐了，
    然后交出一页带着模板示例数据「销售额 20:80」的图）。
    """
    fl = fillable_slots(info["slots"])
    caps = [s["cap_chars"] for s in fl if s.get("cap_chars")]
    a = info.get("assets") or {}
    own = []
    if a.get("pics"):
        own.append(f"{a['pics']} 张自带照片")
    if a.get("smartart"):
        own.append(f"{a['smartart']} 个 SmartArt（文字可填）")
    if a.get("tables"):
        own.append(f"{a['tables']} 张自带表格")
    if a.get("charts"):
        own.append(f"{a['charts']} 个⚠️自带示例图表")
    line = (f"  · {pid}　{info['label']}　可填 {len(fl)} 槽"
            + (f"，容量 {min(caps)}–{max(caps)} 字（中文）" if caps else ""))
    if own:
        line += "　〔" + "；".join(own) + "〕"
    out = [line]
    if a.get("charts"):
        # ★ 一条**会亮的**警告（只在用这一页时出现，不是常亮）
        out.append(
            "      ⚠️ 这一页带着模板**自带的示例图表** —— 本工具**只填字，"
            "换不了图表数据**（数据在嵌入的 xlsx 里）。用它就会交出**假数字**。"
            "要放真实数据图 ⇒ 这一页别用，或把整份稿改走路线乙。")
    return out


def template_plan_text(tpl: dict, only: "list[str] | None" = None) -> str:
    """给模型看的**开工清单**：页型 / 槽位索引 / 原文样例 / **容量（字数上限）**。

    ★ 这一段就是"判据的覆盖面"：不把容量摆到它眼前，它写多长都是猜
    （实测代价：封面标题、章节副标题、四栏正文三处当场顶出去）。

    `only` 给了 ⇒ 只列这几页（页型库很大时按需索取；见 `PLAN_INLINE_MAX`）。
    不认识的 id **点名报出来**，不许静默跳过 —— 静默跳过等于给一份缺页清单。
    """
    pages = template_pages(tpl)
    ids = list(pages) if not only else [str(x).strip() for x in only if str(x).strip()]
    unknown = [x for x in ids if x not in pages]
    ids = [x for x in ids if x in pages]
    lines = [f"【可用页型】共 {len(pages)} 页"
             f"（画布 {tpl.get('aspect')}；缺省字号 {tpl.get('default_pt')}pt）",
             "每槽后面是**容量**，写成 `N字/M字符`：**最多 N 个汉字**，"
             "**或 M 个英文字母/数字**（超了会被拒）。",
             "⚠️ 汉字按 1 个字算，英文数字只占 0.56 个字 —— "
             "**别拿字符数去猜汉字数**，两者差 1.8 倍。"]
    if only:
        lines.append(f"（本次只列你点名的 {len(ids)} 页）")
    if unknown:
        lines.append(f"⚠️ 你点名了 {len(unknown)} 个**不存在的页型**："
                     f"{', '.join(unknown)} —— 它们不在下面（别照抄）。")
    for pid in ids:
        info = pages[pid]
        lines += _page_head_line(pid, info)
        for s in fillable_slots(info["slots"]):
            cap = s.get("cap_chars")
            lines.append(f"      [{_sid(s)}] {s.get('role',''):8s}"
                         f" 上限{cap if cap is not None else '?'}字"
                         f"/{int(cap * slotfit.ASCII_RATIO) if cap is not None else '?'}字符"
                         f"  原文样例 {s.get('sample','')!r}")
    return "\n".join(lines)


def template_overview_text(tpl: dict) -> str:
    """**页型级概览**：一行一页 + 下一步该调什么（页型库大时用；见 `PLAN_INLINE_MAX`）。

    ⚠️ 概览**不给槽位索引** —— 那正是"照抄就错"的地方：槽位索引是随机数一样的
      数字，模型凭印象写必然填错槽。所以概览末尾必须**明说**去调详表，
      并把调用方式写出来（只说"请查看详情"等于没说）。
    """
    pages = template_pages(tpl)
    lines = [f"【可用页型 · 概览】共 {len(pages)} 页（画布 {tpl.get('aspect')}；"
             f"缺省字号 {tpl.get('default_pt')}pt）",
             "—— **这里只有页型，没有槽位索引与逐槽上限。**",
             "写文案之前，对你**要用到的每一页**再调一次 "
             "`deck_template_plan(page=\"页型1,页型2\")` 拿它的槽位索引、"
             "每个槽的字数上限和原文样例；",
             "⚠️ 槽位索引**不要凭印象写**（填错槽会把内容塞进装饰件里）。"]
    for pid, info in pages.items():
        lines += _page_head_line(pid, info)
    return "\n".join(lines)


#: 子进程脚本：python-pptx 在**目标解释器**里跑（与 `style_deck` 同一条纪律）。
_TEMPLATE_SCRIPT = r'''
import json, sys, shutil
from copy import deepcopy
from pptx.util import Pt

def _set(tf, lines, shrink):
    if isinstance(lines, str):
        lines = [lines]
    while len(tf.paragraphs) < len(lines):
        last = tf.paragraphs[-1]._p
        last.addnext(deepcopy(last))
    for i, ln in enumerate(lines):
        p = tf.paragraphs[i]
        if p.runs:
            p.runs[0].text = ln
            for r in p.runs[1:]:
                r._r.getparent().remove(r._r)
        else:
            p.add_run().text = ln
        if shrink != 1.0:
            for r in p.runs:
                if r.font.size:
                    r.font.size = Pt(round(r.font.size.pt * shrink, 1))
    for p in list(tf.paragraphs)[len(lines):]:
        p._p.getparent().remove(p._p)

# ★ 容量模型**不在这里** —— 它是 `rongke/slotfit.py`（全链唯一一份，本子脚本
#   `import` 它）。这里原来那一份是"三份互相抄的公式"之一，且四条假设全是错的
#   （整段按 1em/字、内边距固定 0.2in、每行容量取整、行数不看作者写了多少）
#   ⇒ 实测实测模板 703 槽里 156 槽连模板自己的原文都判"装不下"。
#   **别在这里复活一份**；要改容量就改 `slotfit.py`，改完重抽两份槽位表。

def _flat(shapes):
    """**深度优先摊平**：组合里的子形状也要拿出来。

    ★ 为什么必须有它：`SlideShapes`（`slide.shapes`）**只给顶层**。实测浙工大模板
      有 **14 处文字住在 `p:grpSp` 里** —— 其中 8 处是四个章节页上各两个
      「请输入文本」。摸不到它们 ⇒ 页面上就明晃晃露着两个「请输入文本」，
      而**所有判据都是绿的**（不越界、不超字号）。

    ⚠️ **顺序必须与 `deck_template_extract.py` 的 `flat_shapes` 完全一致** ——
      槽位索引就是这个 DFS 序号。两边都用 python-pptx 的 `shapes` 迭代，
      顺序由同一个库保证；`template_test` 有一条带组合的合成模板端到端钉住它。
    """
    out = []
    for sh in shapes:
        out.append(sh)
        sub = getattr(sh, "shapes", None)
        if sub is not None:
            try:
                out.extend(_flat(list(sub)))
            except Exception:
                pass
    return out


def _rewrite_diagram(path, edits):
    """改 SmartArt 数据部件里的文字（`ppt/diagrams/dataN.xml`）。

    ⚠️ **为什么必须单独做一遍**：SmartArt 的文字**不在 slide 里** ——
    `slideN.xml` 只有一个 `graphicFrame`，正文住在 `dataN.xml` 的 `<a:t>` 里。
    只改 slide 的话，那一页会**原样留着**「请输入文本」×N，而渲染出来才发现。
    做法：按**文档顺序**数 `<a:t>`（抽取器用的是同一个顺序，所以 `d<i>` 对得上），
    逐 zip 条目重写 —— `python-pptx` 保存后会原样保留未识别的部件，所以这一遍在它之后跑。

    `edits` = `{部件名: {第几个 a:t: 新文字}}`。
    """
    import zipfile, os, re
    from xml.sax.saxutils import escape
    tmp = path + ".dia.tmp"
    with zipfile.ZipFile(path) as zin, \
         zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in zin.infolist():
            data = zin.read(it.filename)
            want = edits.get(it.filename)
            if want:
                s = data.decode("utf-8")
                seen = [-1]

                def repl(m, _w=want, _n=seen):
                    _n[0] += 1
                    if _n[0] in _w:
                        return "<a:t>" + escape(_w[_n[0]]) + "</a:t>"
                    return m.group(0)

                s = re.sub(r"<a:t>([^<]*)</a:t>", repl, s)
                data = s.encode("utf-8")
            zout.writestr(it, data)
    os.replace(tmp, path)

def _main():
    from pptx import Presentation
    pl = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    # ★ 容量模型只有一份，住在 `rongke/slotfit.py`（零依赖）。这里 import 它，
    #   而不是抄一份公式 —— 抄的那一份实测会漂，而"表里说能装、执行体说装不下"
    #   正是本项目最贵的那类失败。
    sys.path.insert(0, pl["root"])
    from rongke.slotfit import em_width as _em, fits as _fits, slot_capacity
    tpl, out = pl["template"], pl["out"]
    shutil.copyfile(tpl, out)
    prs = Presentation(out)
    sldIdLst = prs.slides._sldIdLst
    ids = list(sldIdLst)
    RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

    result = {"filled": [], "shrunk": [], "problems": [],
              "diagram_filled": []}

    # ★ 先把"结构上不可能"的两件事判掉，**在动手之前**。
    #   踩过：重复页那一支原先在收尾才报，结果**读回时索引越界**、
    #   子进程当场崩 ⇒ 主进程只看到"页数不符"，**真正的原因（重复用页）丢了**。
    srcs = [pg["src"] for pg in pl["pages"]]
    dup = sorted({x for x in srcs if srcs.count(x) > 1})
    if dup:
        result["problems"].append(
            "同一个模板页被用了两次（%s）—— **一份模板页只能出现一次**："
            "要两个章节页请用 section-1 / section-2（模板本来就给了 4 个章节页）。"
            % "、".join(f"p{x}" for x in dup))
        return result
    bad = [x for x in srcs if x < 1 or x > len(ids)]
    if bad:
        result["problems"].append(
            "源页号越界：%s（这份模板只有 %d 页）" % (bad, len(ids)))
        return result

    dia_edits = {}          # {SmartArt 部件名: {第几个 <a:t>: 新文字}}
    for pg in pl["pages"]:
        src = pg["src"]
        if src < 1 or src > len(ids):
            result["problems"].append(f"源页号 {src} 越界（模板 {len(ids)} 页）")
            continue
        s = prs.slides[src - 1]
        shapes = _flat(list(s.shapes))      # ★ 含组合里的子形状（见 `_flat`）
        for key, val in (pg.get("slots") or {}).items():
            lines0 = val if isinstance(val, list) else [val]
            # ── SmartArt 槽（键是 `d<i>`）：文字不在 slide 里，走 zip 重写 ──
            if str(key).startswith("d"):
                d = (pg.get("dia") or {}).get(str(key)) or {}
                part, ddx = d.get("part"), d.get("idx")
                if part is None or ddx is None:
                    result["problems"].append(
                        "p%d 槽 %s：缺 SmartArt 部件信息（槽位表旧了？"
                        "重跑 `deck_template_extract.py`）" % (src, key))
                    continue
                dia_edits.setdefault(part, {})[int(ddx)] = "".join(lines0)
                result["filled"].append({"src": src, "i": key,
                                         "chars": sum(len(x) for x in lines0),
                                         "how": "填(SmartArt)"})
                result["diagram_filled"].append({"part": part, "idx": int(ddx)})
                continue
            i = int(key)
            if i >= len(shapes):
                result["problems"].append(f"p{src} 槽 {i} 越界（本页 {len(shapes)} 形状）")
                continue
            sh = shapes[i]
            nm = pg.get("names", {}).get(key) or ""
            if nm and sh.name != nm:
                result["problems"].append(
                    f"p{src} 槽 {i} 名字不符：表={nm!r} 实={sh.name!r}（模板换了？重跑抽取器）")
                continue
            if not sh.has_text_frame:
                result["problems"].append(f"p{src} 槽 {i}（{sh.name}）不是文本框")
                continue
            geo = pg.get("geo", {}).get(key) or {}
            lines = val if isinstance(val, list) else [val]
            # 容量模型的五个输入**全在 `geo` 里**（抽取时读出来的事实）：
            # 几何 / 真实内边距 / 换行方式 / 竖排 / 作者原文需要几行。
            _kw = dict(ins_emu=tuple(geo.get("ins") or (91440, 91440, 45720, 45720)),
                       wrap=geo.get("wrap") or "square",
                       vert=geo.get("vert") or "",
                       min_lines=geo.get("min_lines") or 0)
            w_in = float(geo.get("w") or 0)
            h_in = float(geo.get("h") or 0)
            pt0 = float(geo.get("pt") or 0)
            if _fits(lines, w_in, h_in, pt0, **_kw):
                _set(sh.text_frame, lines, 1.0)
                result["filled"].append({"src": src, "i": i,
                                         "chars": sum(len(x) for x in lines),
                                         "how": "填"})
                continue
            done = False
            for step in range(1, 15):
                sc = 1.0 - step * 0.06
                if pt0 * sc < float(pl.get("sz_floor") or 9.0):
                    break
                if _fits(lines, w_in, h_in, pt0 * sc, **_kw):
                    _set(sh.text_frame, lines, sc)
                    result["filled"].append(
                        {"src": src, "i": i, "chars": sum(len(x) for x in lines),
                         "how": "缩%0.0fpt" % (pt0 * sc)})
                    result["shrunk"].append({"src": src, "i": i,
                                             "from": pt0, "to": round(pt0 * sc, 1)})
                    done = True
                    break
            if not done:
                cpl, mx, cap = slot_capacity(w_in, h_in, pt0, **_kw)
                result["problems"].append(
                    "p%d 槽 %d（%s）**装不下**：上限 %d em／%d 行"
                    "（一个汉字算 1、一个英文数字算 0.56），你给了 %d em／%d 字。"
                    "缩到 %.0fpt 仍放不下 ⇒ 请改写短一些。"
                    % (src, i, sh.name, int(cap or 0), mx or 0,
                       int(sum(_em(x) for x in lines)),
                       sum(len(x) for x in lines),
                       float(pl.get("sz_floor") or 9.0)))

    # 删不用的页 + 按请求顺序重排
    keep = list(srcs)
    for i in range(len(ids) - 1, -1, -1):
        if (i + 1) not in keep:
            prs.part.drop_rel(ids[i].get(RID))
            sldIdLst.remove(ids[i])
    for src in keep:
        sldIdLst.append(ids[src - 1])           # lxml：append 已存在的子节点 = 移到末尾
    prs.save(out)

    # SmartArt 的文字**不在 slide 里** —— 必须在 `prs.save` **之后**单独重写部件
    # （python-pptx 会把未识别的部件原样带过去，所以改写要放在它之后）。
    if dia_edits:
        try:
            _rewrite_diagram(out, dia_edits)
        except Exception as e:
            result["problems"].append(
                "SmartArt 部件改写失败（%s: %s）—— 这一页的图示文字**没改上**。"
                % (type(e).__name__, e))
            return result

    # ★ 复测：**从磁盘重开**，逐槽读回来（不是回读内存里那份对象）
    chk = Presentation(out)
    nslides = len(chk.slides._sldIdLst)
    rb = {}
    for n, pg in enumerate(pl["pages"], 1):
        if n - 1 >= nslides:                 # 防御：页少了也不许把整个脚本带走
            rb[str(n)] = {}
            continue
        s = chk.slides[n - 1]
        shapes = _flat(list(s.shapes))
        d = {}
        for key in (pg.get("slots") or {}):
            if str(key).startswith("d"):
                continue            # SmartArt 的文字从 zip 里读回，见下
            i = int(key)
            if i < len(shapes) and shapes[i].has_text_frame:
                d[key] = shapes[i].text_frame.text
        rb[str(n)] = d
    # SmartArt 的复测：**重新打开磁盘上那份 zip** 读 `dataN.xml` 的第 idx 个 <a:t>
    if dia_edits:
        import zipfile, re
        with zipfile.ZipFile(out) as _z:
            for n, pg in enumerate(pl["pages"], 1):
                for key, want in (pg.get("slots") or {}).items():
                    if not str(key).startswith("d"):
                        continue
                    d = (pg.get("dia") or {}).get(str(key)) or {}
                    part, ddx = d.get("part"), d.get("idx")
                    if part is None or ddx is None:
                        continue
                    try:
                        x = _z.read(part).decode("utf-8")
                    except KeyError:
                        continue
                    ts = re.findall(r"<a:t>([^<]*)</a:t>", x)
                    if int(ddx) < len(ts):
                        rb.setdefault(str(n), {})[str(key)] = ts[int(ddx)]
    result["readback"] = rb
    result["slide_count"] = nslides
    return result


def main():
    # ★ **永远回一份 JSON**。子进程崩掉而只留 stderr 的后果是：
    #   主进程拿到 `{}`、于是把"重复用页"报成"页数不符" —— **真正的原因丢了**。
    #   实测踩过。所以最外层兜住一切异常，翻成一条 problems。
    try:
        result = _main()
    except Exception as e:
        import traceback
        result = {"filled": [], "shrunk": [],
                  "problems": ["填充脚本崩了：%s: %s" % (type(e).__name__, e),
                               traceback.format_exc()[-1200:]],
                  "readback": {}, "slide_count": 0}
    result["ok"] = not result.get("problems")
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


main()
'''


@dataclass
class TemplateDeck:
    """`deck_from_template` 的结果。**自称成功不算数** —— 见 `deck_from_template` 的复测。"""
    ok: bool = False
    pptx: str = ""
    template: str = ""
    #: 用的是哪份槽位表（`docs/deck-templates/<名>/slots.json`）。
    #: ★ 它必须**跟到 sidecar 里去**：体检门做模板基线扣除时要按它核对，
    #:   而基线扣除一旦对错模板，扣出来的就是另一份产物的 findings。
    template_name: str = ""
    problems: list[str] = field(default_factory=list)
    filled: list[dict] = field(default_factory=list)
    shrunk: list[dict] = field(default_factory=list)
    #: 不阻断、但**必须说出口**的事（如"用了自带示例图表的页"）。
    #: 与 `problems` 分开：problems 是"没做到"，warnings 是"做到了但你要知道"。
    warnings: list[str] = field(default_factory=list)
    map: list[list[int]] = field(default_factory=list)   # [输出页序号, 模板源页号]
    origin_path: str = ""
    python_exe: str = ""
    rc: int = -1
    stderr_tail: str = ""
    slides_before: int = 0
    slides_after: int = 0
    bytes_after: int = 0
    checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "pptx": self.pptx, "template": self.template,
            "template_name": self.template_name,
            "problems": self.problems, "filled": self.filled,
            "shrunk": self.shrunk, "pages": len(self.map), "map": self.map,
            "warnings": self.warnings,
            "origin_path": self.origin_path, "python_exe": self.python_exe,
            "rc": self.rc, "stderr_tail": self.stderr_tail,
            "slides_before": self.slides_before, "slides_after": self.slides_after,
            "bytes_after": self.bytes_after, "checks": self.checks,
        }

    def format_for_model(self) -> str:
        head = "成功" if self.ok else "**未完成**"
        L = [f"【按模板出稿 {head}】{self.pptx}（模板 {self.template}）"]
        if self.problems:
            L += ["", "⛔ 必须修掉："] + [f"  · {x}" for x in self.problems]
        if self.ok:
            L += ["", f"  {len(self.map)} 页："
                      + " → ".join(f"p{a}←p{b}" for a, b in self.map)]
            L += [f"  填了 {len(self.filled)} 个槽"
                  + (f"，其中缩号 {len(self.shrunk)} 处："
                     + "；".join(f"p{x['src']} 槽{x['i']} {x['from']:.0f}→{x['to']:.0f}pt"
                                 for x in self.shrunk) if self.shrunk else "（无缩号）")]
            if self.warnings:
                L += ["", "⚠️ 必须知道："] + [f"  · {x}" for x in self.warnings]
            L += ["", "  复测（**从磁盘重开读回来的**，不是脚本的自述）："]
            L += [f"    · {c}" for c in self.checks]
            L += ["",
                  "  ⚠️ 还没做体检 —— 交付前调 `audit_deck(pptx=…)`（本工具**不代替门**）。"]
        return "\n".join(L)


def deck_from_template(out: str, pages: list[dict], *, template_pptx: str = "",
                       template_slots: dict | None = None, template_name: str = "",
                       workdir: str | Path | None = None,
                       python_exe: str | None = None, cfg=None,
                       timeout: float | None = None) -> TemplateDeck:
    """按品牌模板出稿：**保留模板页、在槽上填字、多余的页删掉**（保真路线）。

    `pages` = `[{"page": "cover", "slots": {"2": "标题第一行\\n标题第二行", ...}}, ...]`
    · `page` 取 `template_plan_text()` 里列出的页 id（`section-1..4` 是四个章节页）。
    · `slots` 的键是**槽位索引**（清单里的 `[ 2]`），值是文字（列表 = 多段）。
    · ⚠️ **每个槽都必须填** —— 不填就会把模板的「请输入文本」交出去。
      缺哪个会在 `problems` 里点名（**拦沉默，不拦选择**）。
    · ⚠️ 同一个模板页只能用一次（要两个章节页请用 `section-1`/`section-2`）。

    ★ 这里**不做体检**：门第 9 条仍要求 `audit_deck`。本工具只负责"产出一份
      品牌保真的稿"，**不代替门** —— 否则又是一条平行判定（H 型）。
    """
    res = TemplateDeck(template=template_name or "", pptx=str(out or ""))

    # ── ① 路径：先 resolve 再判围栏（与 style_deck 同一道关）──
    root = Path(workdir).resolve() if workdir else Path.cwd().resolve()
    raw = str(out or "").strip()
    if not raw:
        res.problems.append("`out` 不能为空（要写到工作区里的哪个 .pptx）。")
        return res
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    try:
        p = p.resolve()
    except Exception as e:
        res.problems.append(f"路径无法解析：{raw}（{type(e).__name__}: {e}）")
        return res
    if p != root and root not in p.parents:
        res.problems.append(f"⛔ 越界：{p}\n   本任务工作区是 {root}，只能写它里面的文件。")
        return res
    res.pptx = str(p)

    # ── ② 模板 + 槽位表 ──────────────────────────────────────
    tpl_pptx = Path(template_pptx) if template_pptx else None
    if tpl_pptx is None:
        cfg_path = getattr(cfg, "template", None) if cfg is not None else None
        if cfg_path:
            tpl_pptx = Path(str(cfg_path))
    if tpl_pptx is None or not Path(tpl_pptx).is_file():
        res.problems.append(
            "**模板文件不可用**：请在 `config.json` 的 `deck.template` 里填模板 .pptx 的绝对路径"
            f"（现在取到的是 {tpl_pptx!r}）。\n"
            "   ⇒ 没有模板就**无法出品牌稿**。不要反复重试 —— 交付时如实说明，"
            "或改用 `style_deck`/`compose_deck`（但那只是近似品）。")
        return res
    tpl_pptx = Path(tpl_pptx).resolve()
    res.template = str(tpl_pptx)
    if template_slots is None:
        try:
            template_slots = load_template(template_name or template_name_of(cfg))
        except FileNotFoundError as e:
            res.problems.append(str(e))
            return res
    res.template_name = template_name or template_name_of(cfg)
    tpl_pages = template_pages(template_slots)

    # ── ③ 先判"页/槽填全了没有"（**在起子进程之前**，省得白跑）──
    plan: list[dict] = []
    for n, pg in enumerate(pages or [], 1):
        pid = str((pg or {}).get("page") or "").strip()
        info = tpl_pages.get(pid)
        if info is None:
            near = [k for k in tpl_pages if k.startswith(pid[:6])] if pid else []
            res.problems.append(
                f"第 {n} 页：不认识的页型 `{pid}`。可选：{', '.join(tpl_pages)}"
                + (f"（你是不是想用 {', '.join(near)}？）" if near else ""))
            continue
        want = {str(k): v for k, v in ((pg or {}).get("slots") or {}).items()}
        fl = fillable_slots(info["slots"])
        miss = [s for s in fl if str(s["i"]) not in want]
        if miss:
            res.problems.append(
                f"第 {n} 页（{pid}）**有 {len(miss)} 个槽没填** —— 不填就会把模板的"
                f"「请输入文本」交出去：\n"
                + "\n".join(f"      [{_sid(s)}] {s.get('role','')}"
                            f" 上限{s.get('cap_chars')}字"
                            f"／{int((s.get('cap_chars') or 0) * slotfit.ASCII_RATIO)}字符"
                            f" 原文 {s.get('sample','')!r}" for s in miss))
            continue
        bad = [k for k in want if k not in {str(s["i"]) for s in info["slots"]}]
        if bad:
            res.problems.append(
                f"第 {n} 页（{pid}）给了不存在的槽索引 {sorted(bad)}；"
                f"本页可填的是 {[s['i'] for s in fl]}。")
            continue
        plan.append({
            "src": info["src"], "page": pid,
            "names": {str(s["i"]): s["name"] for s in info["slots"]},
            # ★ 容量模型的输入**必须原样带过去**（内边距/换行/竖排/原文行数）：
            #   子进程 import 的是同一个 `slotfit`，但它得拿到同一组事实，
            #   否则"表里的数"与"执行体算的数"又分家了。
            "geo": {str(s["i"]): {"w": s["w"], "h": s["h"], "pt": s["pt"],
                                  "ins": s.get("ins_emu"),
                                  "wrap": s.get("wrap") or "square",
                                  "vert": s.get("vert") or "",
                                  "min_lines": s.get("min_lines") or 0}
                    for s in info["slots"]},
            # SmartArt 槽（键 `d<i>`）要带上"改哪个部件的第几段" —— 子进程
            # 靠它做 zip 重写。**没有它就必须报错**，不许跳过。
            "dia": {str(s["i"]): {"part": s.get("part"),
                                  "idx": s.get("part_index")}
                    for s in info["slots"] if s.get("role") == "diagram"},
            "slots": want,
        })
    if res.problems:
        return res
    if not plan:
        res.problems.append("`pages` 是空的：至少要一页。")
        return res

    # ── ④ 解释器（要 python-pptx）────────────────────────────
    explicit = python_exe or (getattr(cfg, "python", None) if cfg is not None else None)
    py = find_notes_python(explicit)
    res.python_exe = py or str(explicit or "(未指定)")
    if py is None:
        res.problems.append(
            "**按模板出稿的能力不可用**：找不到装了 python-pptx 的解释器。\n"
            "   ⇒ 不要反复重试；交付时如实说明。")
        return res

    # ★ 用到"自带示例图表"的页 ⇒ 记一条 warning（**不阻断，但必须说出口**）。
    #   理由：那种图表的数据在嵌入的 xlsx 里，本工具换不了 —— 交出去就是**假数字**，
    #   而文字槽还填得像模像样，用户看不出哪里不对。**假数字比缺一张图严重得多。**
    for _n, pg in enumerate(plan, 1):
        a = (tpl_pages.get(pg["page"]) or {}).get("assets") or {}
        if a.get("charts"):
            # ⚠️ 页号必须用**产物里的位次**（`_n`），不能拿 `len(res.warnings)+1` 顶替
            #    ——那是"第几条警告"，只在恰好一条时看着像页号。2026-09-22 实测：
            #    12 页稿里唯一一条警告印成「第 1 页」，而那一页实际排在第 11 位。
            #    说出口的话必须是准的：错页号会把人送到错的地方去改。
            res.warnings.append(
                f"第 {_n} 页用的 `{pg['page']}` 带着模板**自带的示例图表**"
                f"（数据在嵌入 xlsx 里，本工具**换不了**）。这一页交出去会带着**假数字** ——"
                f"请改用别的页型，或在正文里明确说明该图是模板占位、不是本研究数据。")

    before = probe_pptx(tpl_pptx)
    res.slides_before = before.slide_count

    # ── ⑤ 跑 ────────────────────────────────────────────────
    to = float(timeout or getattr(cfg, "timeout", None) or 300.0)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "template": str(tpl_pptx), "out": str(p), "pages": plan,
        "sz_floor": TEMPLATE_SZ_FLOOR,
        # ★ 子进程要 `import rongke.slotfit`（容量模型的唯一一份）。
        #   传绝对路径而不是靠 cwd —— cwd 随调用方变，靠它会把"找不到模块"
        #   变成"每次出稿都失败"。
        "root": str(ROOT),
    }, ensure_ascii=False)
    try:
        r = subprocess.run([py, "-c", _TEMPLATE_SCRIPT], input=payload.encode("utf-8"),
                           capture_output=True, timeout=to)
    except subprocess.TimeoutExpired:
        res.problems.append(
            f"按模板出稿超时（{to:.0f}s）—— 脚本被中断，文件**可能只写了一半**。"
            f"先重新读一遍这份 deck 再决定下一步。")
        return res
    except Exception as e:
        res.problems.append(f"起不了子进程（{py}）：{type(e).__name__}: {e}")
        return res
    res.rc = int(r.returncode)
    res.stderr_tail = (r.stderr or b"").decode("utf-8", "replace")[-800:]
    data = None
    try:
        data = json.loads((r.stdout or b"").decode("utf-8", "replace") or "{}")
    except Exception as e:
        res.problems.append(
            f"子进程的返回不是 JSON（rc={res.rc}）：{type(e).__name__}: {e}\n"
            f"stderr 末尾：{res.stderr_tail[-400:]}")
        return res
    res.filled = data.get("filled") or []
    res.shrunk = data.get("shrunk") or []
    res.problems += list(data.get("problems") or [])
    if res.problems:
        return res

    # ── ⑥ 独立复测：**用门那把尺子**（`probe_pptx`，标准库读回来）──
    after = probe_pptx(p)
    res.slides_after = after.slide_count
    res.bytes_after = p.stat().st_size if p.is_file() else 0
    if after.problems or after.slide_count == 0:
        res.problems.append("产物读不回来（不是合法 .pptx）："
                            + "；".join(after.problems or ["0 页"]))
        return res
    if after.slide_count != len(plan):
        res.problems.append(
            f"复测页数不符：要做 {len(plan)} 页，读回来 {after.slide_count} 页。")
        return res
    # ① 页数 ✔ ② 逐页**真从磁盘读回**的文字必须与发出去的一致（忽略空白差异）
    rb = data.get("readback") or {}
    mismatch = []
    for n, pg in enumerate(plan, 1):
        got = rb.get(str(n)) or {}
        for k, want in (pg["slots"] or {}).items():
            wl = want if isinstance(want, list) else [want]
            # ⚠️ **两侧用同一把尺子去空白**。踩过：只对读回来那侧 `.split()`、
            #    对发出去那侧只 `strip()` ⇒ 「01 研究背景」被判成对不上
            #    （读回来是「01研究背景」）。那是个**必然发生的假告警**，
            #    而假告警会教人学会无视整份报告（定律 10）。
            #    这与 `set_slide_notes` 那条纪律同源：比对必须忽略空白。
            w_flat = "".join("".join(x for x in wl).split())
            g_flat = "".join((got.get(k) or "").split())
            if w_flat != g_flat:
                mismatch.append(f"第 {n} 页槽 {k}：发出去 {w_flat[:30]!r}… "
                                f"读回来 {g_flat[:30]!r}…")
    if mismatch:
        res.problems.append("**复测对不上**（写进去的和读回来的不是同一份）：\n"
                            + "\n".join(f"    · {x}" for x in mismatch[:6]))
        return res
    # ③ 每页必须有字（挡住"页被清空/写错页"这类静默失败）
    thin = [i for i, f in enumerate(after.slides, 1) if not f.get("text_chars")]
    if thin:
        res.problems.append(f"复测发现第 {thin} 页**一个字都没有**（页被写空或写错了页）。")
        return res

    # ── ⑥+ 可读性修正（W9）：只改字色，改完**从磁盘重开复测**再写进报告 ──
    #   合格稿 ⇒ `apply_readability_fix` 连文件都不打开写（零 diff）；
    #   没修好 / 修不了 / 量不到 ⇒ 全部进 `warnings`，不许被读成"过了"。
    fix = apply_readability_fix(p, python_exe=py, timeout=to)
    fix_checks, fix_warnings = readability_fix_lines(fix)
    if fix.get("problems"):
        fix_warnings.append(
            "· 可读性修正**没跑成**：" + "；".join(fix["problems"][:3])
            + " —— 门抓到的对比度缺陷可能仍在稿上，交付前请人工确认。")
    if fix["touched"]:
        res.bytes_after = p.stat().st_size if p.is_file() else res.bytes_after

    # ── ⑦ origin sidecar：门要用它做**基线扣除**（见 `DeckAuditor`）──
    res.map = [[n, pg["src"]] for n, pg in enumerate(plan, 1)]
    side = p.with_suffix(p.suffix + ".origin.json")
    side.write_text(json.dumps({
        "template": str(tpl_pptx), "template_name": res.template_name,
        "map": res.map, "pages": [pg["page"] for pg in plan],
    }, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    res.origin_path = str(side)

    res.ok = True
    res.warnings += fix_warnings
    res.checks = [
        f"页数 {res.slides_before}（模板）→ {res.slides_after}（产物），与请求一致",
        f"{len(res.filled)} 个槽**逐槽从磁盘读回**、与发出去的文字逐字相符",
        f"每页都有文字（无空页）；文件 {res.bytes_after} 字节",
    ] + fix_checks
    return res


class DeckAuditor:
    """把上游 XML 审计 + 荣珂收紧的那几项串起来。

    用法：

        au = DeckAuditor(python_exe=..., skills_dir=..., cfg=cfg.deck)
        rep = au.audit("deck.pptx", workdir="/abs/ws")
        if not rep.ok:            # rep.blockers 里是逐条原因
            ...
    """

    def __init__(self, python_exe: str | None = None, skills_dir: str | None = None,
                 cfg=None, timeout: float = 180.0):
        self._python_exe = python_exe
        self._skills_dir = skills_dir
        self.cfg = cfg
        self.timeout = float(getattr(cfg, "timeout", None) or timeout)
        self.notes: list[str] = []
        self._py: str | None = None
        self._script: Path | None = None
        self._script_resolved = False

    # ── 阈值（配置没有就给默认，且**默认写在配置里**，这里只是兜底）──
    def _th(self, name: str, default):
        return getattr(self.cfg, name, default) if self.cfg is not None else default

    @property
    def python_exe(self) -> str | None:
        if self._py is None:
            self._py = find_deck_python(self._python_exe)
            if self._py is None:
                self.notes.append(
                    f"找不到能跑审计的解释器（指定的 `{self._python_exe or '(未指定)'}` 起不来）。")
        return self._py

    @property
    def audit_script(self) -> Path | None:
        if not self._script_resolved:
            self._script = find_audit_script(self._skills_dir)
            self._script_resolved = True
            if self._script is None:
                self.notes.append(
                    "找不到 nature-paper2ppt 的 scripts/audit_pptx_quality.py。"
                    "请在 config.json 的 deck.skills_dir 里指定技能根。")
        return self._script

    # ── 门 1：上游 XML 审计 ─────────────────────────────────
    def _gate_xml(self, pptx: Path, raw_json: Path) -> GateResult:
        g = GateResult(name="xml", verdict="SKIPPED")
        if not self.cfg_is_enabled():
            g.detail = "配置里 deck.enabled=false"
            return g
        if self.audit_script is None or self.python_exe is None:
            g.detail = "；".join(self.notes) or "审计器或解释器不可用"
            return g
        cmd = [self.python_exe, str(self.audit_script), str(pptx),
               "--json", str(raw_json), "--fail-on", "none"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=self.timeout,
                               cwd=str(pptx.parent))
        except subprocess.TimeoutExpired:
            g.detail = f"审计脚本超时（{self.timeout:.0f}s）"
            return g
        except Exception as e:
            g.detail = f"审计脚本起不来：{type(e).__name__}: {e}"
            return g
        g.raw = (r.stdout or b"").decode("utf-8", "replace")[:4000]

        data = None
        if raw_json.is_file():
            try:
                data = json.loads(raw_json.read_text(encoding="utf-8"))
            except Exception as e:
                g.detail = f"审计 JSON 读不动：{type(e).__name__}: {e}"
        if not isinstance(data, dict):
            tail = (r.stderr or b"").decode("utf-8", "replace")[-600:]
            if not g.detail:
                g.detail = (f"审计脚本没有产出 JSON（rc={r.returncode}）。"
                            f"stderr 末尾：{tail or '（空）'}")
            return g
        g.report_path = str(raw_json)

        # ── 模板基线扣除（品牌模板的装饰不是缺陷）────────────────
        #
        # ★ 为什么是"算出来的基线"而不是"手写的豁免名单"：
        #   豁免名单会烂，烂的方式是"新缺陷被顺手加进名单里"。而基线 =
        #   **同一套审计器原样跑在模板身上**得到的发现 ⇒ 模板改一次它自动跟着变。
        # ★ 为什么需要它：实测浙工大模板自身 11 条 high 全是**品牌装饰**
        #   （450pt 章节数字刻意出血 / 装饰照片裁切），而保真稿会**继承**它们
        #   ⇒ 不扣掉，用品牌模板出稿**恒不通过**。
        # ★ 为什么不担心被洗白：map 不是自述，是**可校验的**——
        #   产物页与模板页的 `shape_count`/`image_count` 必须逐页相等，
        #   且 map 必须是 1..N 的置换；任何一条对不上 ⇒ **一条都不扣**。
        findings_raw = list(data.get("findings") or [])
        base_note = ""
        base, binfo, why = self._template_baseline(pptx, raw_json, data)
        if base is None:
            if why:
                base_note = f"　（模板基线**没能用上**：{why} —— 按「宁可多报」处理，未扣除。）"
        else:
            kept_raw, removed = [], []
            for it in findings_raw:
                sl = it.get("slide")
                key = (str(it.get("code")), int(sl)) if sl is not None else None
                (removed if (key and key in base) else kept_raw).append(it)
            if removed:
                findings_raw = kept_raw
                sev = {}
                for it in removed:
                    k = str(it.get("severity") or "low")
                    sev[k] = sev.get(k, 0) + 1
                base_note = (
                    f"　（已扣掉模板自带的 {len(removed)} 条品牌装饰："
                    + "、".join(f"{k} {v}" for k, v in sorted(sev.items()))
                    + " —— 那不是缺陷，是模板的设计：刻意出血的巨型章节数字、"
                      "装饰照片的裁切。基线由**同一套审计器跑在模板上**算得，"
                      "且已校验产物与模板逐页同构。）")
                g.extra["template_baseline"] = {
                    "template": binfo.get("template", ""), "subtracted": len(removed),
                    "by_severity": sev, "pages": len(binfo.get("map") or []),
                }

        counts = {}
        for it in findings_raw:
            k = str(it.get("severity") or "low")
            counts[k] = counts.get(k, 0) + 1
        high = int(counts.get("high") or 0)
        med = int(counts.get("medium") or 0)
        low = int(counts.get("low") or 0)
        for item in findings_raw:
            sev = str(item.get("severity") or "low")
            slide = item.get("slide")
            g.findings.append(Finding(
                level="FAIL" if sev == "high" else "WARN",
                check_id=f"upstream/{item.get('code') or '?'}",
                message=str(item.get("message") or ""),
                evidence=[f"slide={slide}"] if slide is not None else [],
            ))
        if high and _UPSTREAM_HIGH_IS_BLOCKING:
            g.verdict = "FIX BEFORE DELIVERY"
            g.detail = (f"上游 XML 审计：high={high}（形状越界 / 大幅裁图），"
                        f"这些必须改{base_note}")
        elif med or low:
            g.verdict = "REVIEW REQUIRED"
            g.detail = (f"上游 XML 审计：medium={med}, low={low}"
                        f"（逐条复核并说明，或改掉）{base_note}")
        else:
            g.verdict = "PASS"
            g.detail = ("上游 XML 审计无发现（越界/密度/套话/裁图/小图/对齐）"
                        + base_note)
        return g

    def _template_baseline(self, pptx: Path, raw_json: Path, data: dict
                           ) -> tuple[set | None, dict, str]:
        """读 `<pptx>.origin.json`，把模板自己带的发现算成基线。

        返回 `(基线集合, sidecar 的 map, 未生效的原因)`。
        **任何一步对不上就返回 `(None, map, 原因)`** —— 方向是"宁可多报"，
        绝不因为校验不了就大方地全扣掉。

        ★★ 一条**上游约定**（读 `audit_pptx_quality.py` 定的，不是猜的）：
           findings 的 `slide` = **`ppt/slides/slideN.xml` 的 N**（`slide_number_from_path`），
           **不是**演示顺序里的位置。
           而本引擎**保留模板页、从不克隆** ⇒ 产物的 N **就是**模板的源页号
           ⇒ **基线匹配不需要任何"输出位置→模板页"的翻译**（我第一版就错在
             多加了这层翻译，结果校验全对不上）。
        ★ 这条约定也是上面那条"对不上就不扣"挡着的东西：**万一上游哪天改成
           按演示顺序编号**，产物侧的键集会变成 `{1..N}`、与 sidecar 声明的
           源页号集合不符 ⇒ 校验失败 ⇒ **退回不扣**（多报，安全的方向）。
        """
        origin = Path(str(pptx) + ".origin.json")
        if not origin.is_file():
            return None, {}, ""            # 不是模板产出的稿 ⇒ 本来就该全报
        try:
            o = json.loads(origin.read_text(encoding="utf-8"))
        except Exception as e:
            return None, {}, f"origin.json 读不动（{type(e).__name__}）"
        tpl = Path(str(o.get("template") or ""))
        mp = o.get("map") or []
        if not tpl.is_file():
            return None, {}, f"模板文件不在盘上了：{tpl}"
        if not mp or self.audit_script is None or self.python_exe is None:
            return None, {}, "缺少 map，或审计器/解释器不可用"
        try:                                # map 必须是 1..N 的置换（防伪造/防漏页）
            outs = sorted(int(a) for a, _ in mp)
            srcs = {int(b) for _, b in mp}
        except Exception:
            return None, {}, "map 里的页号不是整数"
        if outs != list(range(1, len(mp) + 1)):
            return None, {}, "map 不是 1..N 的置换"
        tjson = raw_json.parent / (raw_json.stem + ".template.json")
        try:
            subprocess.run([self.python_exe, str(self.audit_script), str(tpl),
                            "--json", str(tjson), "--fail-on", "none"],
                           capture_output=True, timeout=self.timeout,
                           cwd=str(tpl.parent))
            td = json.loads(tjson.read_text(encoding="utf-8"))
        except Exception as e:
            return None, {}, f"跑模板基线失败（{type(e).__name__}: {e}）"
        tsl = {int(s["slide"]): s for s in (td.get("slides") or [])}
        psl = {int(s["slide"]): s for s in (data.get("slides") or [])}
        if set(psl) != srcs:
            return None, {}, (f"产物页码集合 {sorted(psl)} 与声明的模板源页 "
                              f"{sorted(srcs)} 不符（上游编号口径变了？）")
        for no in sorted(srcs):
            pa, tb = psl.get(no), tsl.get(no)
            if not pa or not tb:
                return None, {}, f"第 {no} 页在两侧对不上"
            if (pa.get("shape_count"), pa.get("image_count")) != \
               (tb.get("shape_count"), tb.get("image_count")):
                return None, {}, (f"第 {no} 页形状/图数不符：产物 "
                                  f"{(pa.get('shape_count'), pa.get('image_count'))} vs "
                                  f"模板 {(tb.get('shape_count'), tb.get('image_count'))}"
                                  f"（形状被加过/换过 ⇒ 不扣）")
        base = {(str(f.get("code")), int(f["slide"]))
                for f in (td.get("findings") or []) if f.get("slide") is not None}
        return base, {"template": str(tpl), "map": [[int(a), int(b)] for a, b in mp]}, ""

    def cfg_is_enabled(self) -> bool:
        if self.cfg is None:
            return True
        return getattr(self.cfg, "enabled", True) is not False

    # ── 门 2：荣珂收紧的版式判据 ────────────────────────────
    def _gate_type(self, facts: DeckFacts) -> GateResult:
        g = GateResult(name="type", verdict="PASS")
        th_min = int(self._th("min_slides", 6))
        th_max = int(self._th("max_slides", 40))
        th_title = int(self._th("max_title_chars", 40))
        th_body = int(self._th("max_body_chars_per_slide", 600))
        th_font = float(self._th("min_font_pt", 12.0))

        if facts.problems:
            g.verdict = "NOT AUDITABLE"
            g.detail = "；".join(facts.problems[:3])
            return g
        if facts.slide_count == 0:
            g.verdict = "FIX BEFORE DELIVERY"
            g.detail = "这份 pptx 一页都没有"
            g.findings.append(Finding("FAIL", "empty_deck", "0 页，不是一份可交付的 deck"))
            return g

        fails: list[Finding] = []
        warns: list[Finding] = []
        # ⚠️ `skips` 必须单开一个篮子。
        # 踩过（本轮首跑就被 `deck_test` 的 3.6/3.7 抓住）：原先只留了
        # `fails` / `warns` 两个篮子，末尾 `g.findings = fails + warns`
        # 会把前面 append 进 `g.findings` 的那条 `font_unmeasurable`
        # **静默冲掉** —— 于是"字号量不出来"这条 M 型防线根本不在报告里，
        # 而它正是为了阻止"没查"被读成"通过"才存在的。
        # **一个被后续赋值覆盖掉的守卫，等于没有守卫**（G 型）。
        skips: list[Finding] = []

        # ① 字号：客观不可读是 FAIL；低于期望但肉眼尚可是 WARN。
        #    只在"正文体量"的形状上判 —— 图表轴标签/页码做小是有意的。
        if facts.min_body_font_pt is not None:
            if facts.min_body_font_pt < HARD_MIN_FONT_PT:
                fails.append(Finding(
                    "FAIL", "font_too_small",
                    f"正文字号最小 {facts.min_body_font_pt:.1f} pt，低于客观可读下限 "
                    f"{HARD_MIN_FONT_PT:.0f} pt（投影/打印都读不了）",
                    [f"min_body_font_pt={facts.min_body_font_pt}"]))
            elif facts.min_body_font_pt < th_font:
                warns.append(Finding(
                    "WARN", "font_small",
                    f"正文字号最小 {facts.min_body_font_pt:.1f} pt，低于本场景期望的 "
                    f"{th_font:.0f} pt（后排可能看不清）"))
        else:
            # 量不出来就说量不出来 —— 不许渲染成"通过"（M 型）。
            skips.append(Finding(
                "SKIP", "font_unmeasurable",
                "没有一处在正文体量上显式声明字号（都继承母版）⇒ 字号这一项**没查**"))

        # ② 页数区间：**只 WARN**。3 页的进展速报是合法的，
        #    把它硬拦掉是误报，而误报比漏报更伤（设计定律 10）。
        if facts.slide_count < th_min:
            warns.append(Finding("WARN", "slide_count_small",
                                 f"只有 {facts.slide_count} 页（本场景期望 ≥ {th_min}）"))
        elif facts.slide_count > th_max:
            warns.append(Finding("WARN", "slide_count_large",
                                 f"{facts.slide_count} 页（本场景期望 ≤ {th_max}）；"
                                 f"组会通常讲不完这么多页"))

        # ③ 每页字数：搬整段论文上来的典型症状
        for s in facts.slides:
            if s["text_chars"] > th_body:
                warns.append(Finding(
                    "WARN", "slide_text_overload",
                    f"第 {s['slide']} 页有 {s['text_chars']} 字（期望 ≤ {th_body}）——"
                    f"一页一个主张，剩下交给图/表/数字和讲稿",
                    [f"slide={s['slide']}"]))
                if len(warns) >= 12:
                    break

        # ④ 标题：一页没有标题 = 讲的时候听众失去锚点
        # ⚠️ 「有标题」= `title`/`ctrTitle` **占位符**，**或**页面上有一处 ≥24pt 的大字。
        #    为什么放宽这一条：品牌模板（以及大量手工做的 deck）用**普通文本框**
        #    当标题 ⇒ 只认占位符会让**每一页**都亮警告，而"永远亮着的警告"
        #    会教人整体无视体检报告（定律 10，本项目已为 `layout_monotone` 修过一次）。
        #    放宽的方向是安全的：真的没标题（只有 12pt 正文的页）照样被抓
        #    —— `deck_test` 里有一条正控钉住它。
        untitled = [s["slide"] for s in facts.slides
                    if not s["has_title"] and (s.get("max_font_pt") or 0) < 24]
        if untitled:
            warns.append(Finding(
                "WARN", "slide_without_title",
                f"{len(untitled)} 页没有标题占位符（第 {untitled[:6]} 页）",
                [f"untitled={len(untitled)}"]))
        long_titles = []
        for s in facts.slides:
            if len(str(s.get("title_text") or "")) > th_title:
                long_titles.append(s["slide"])
        if long_titles:
            warns.append(Finding("WARN", "title_too_long",
                                 f"标题超过 {th_title} 字的有第 {long_titles[:6]} 页"))

        # ⑤ 视觉（2026-09-21 新增）。
        #    为什么值得单开一节：用户看完一份**结构全绿**的 deck 只说了一句
        #    「那能做的这么丑」。上面四条检查一条都没红 —— 因为它们量的都是
        #    "能不能讲"，而"丑不丑"**不在它们的量程里**。
        #    ⇒ **判据的覆盖面就是产出的上限**：门只量结构，产出就只保证结构。
        #
        #    ⚠️ 这一节只硬拦两条**不可能误伤**的形状，其余全 WARN
        #    （误报比漏报更伤，设计定律 10）：
        #      a. 4:3 —— 投影和屏幕都是 16:9，交上去必然留黑。客观、可复算。
        #      b. 纯默认外观 —— 没套已知主题 + 没设过字体，两条同时成立才判。
        #         这是"起手空白模板、一次都没打扮"的指纹。
        #         （原是三连，第三连「版式单一」2026-09-21 实测为**噪音项**已删 ——
        #          版式分工是上游模型自己会做的事，不是主题工具该管的。）
        if facts.is_widescreen is False:
            fails.append(Finding(
                "FAIL", "aspect_not_16_9",
                f"画布 {facts.slide_cx}×{facts.slide_cy} EMU（比例 "
                f"{facts.aspect:.3f}，即 4:3）—— 投影和屏幕都是 16:9，"
                f"这样交上去必然两侧留黑。改法：`style_deck(pptx=\"{Path(facts.path).name}\", "
                f"theme=\"{DEFAULT_DECK_THEME}\")`，之后**重跑**体检。",
                [f"aspect={facts.aspect}", f"slide_cx={facts.slide_cx}"]))
        elif facts.is_widescreen is None:
            skips.append(Finding("SKIP", "aspect_unmeasurable",
                                 "读不到画布宽高 ⇒ 比例这一项**没查**"))

        theme_name = facts.theme_name()
        theme_known = theme_name in DECK_THEMES
        fonts_set = facts.fonts_set_ratio or 0.0
        # ★ 判据修正（2026-09-21）：原式为三连，第三连是 `layout_top_share >= 0.7`。
        #   实测它是**噪音项** —— 那份被吐槽"丑"的真 deck 本来就有 3 种版式
        #   （封面 Title Slide / 内容 Title and Content / 图表 Title Only，
        #   正文页占 77%），**版式分工是上游模型主动做的**；它丑在两处：
        #   `4:3` 与 `Office 默认主题 ＋ 零显式字体`。
        #   ⇒ 留下真正指向"起手空白模板、一次没打扮"的两条，删掉版式那一条。
        #
        #   ⚠️ 收窄成 `theme_name == "other"`，**不是** `not theme_known`：
        #     `""` = 包里没有 theme1.xml ⇒ **没查**（真实 .pptx 必然带它，
        #            读到空只可能是极简合成夹具）⇒ 读成"没打扮"就是**误报**。
        #            第一版我把 `not theme_known` 写进来，当场打红 5 项既有断言
        #            （3.1/3.4/3.8/3.9/6.3 全是无 theme 的合成夹具）。
        #     `"other"` = 读到了，但不在内核那几套里 ⇒ 这才是证据。
        #   ⇒ 与下面 `theme_unknown` 那条 WARN **同一把尺子**（两处不同源即 H 型）。
        #
        #   ⚠️ 删第三连**不降低覆盖面**：那两份真产物（合成 §18 夹具走不到这里，
        #   python-pptx 真文件走得到）仍被判 FAIL —— 由 `deck_test` 19.4 钉住。
        default_look = (theme_name == "other" and fonts_set < 0.05)
        g.extra["visual"] = {
            "aspect": round(facts.aspect, 4) if facts.aspect is not None else None,
            "is_widescreen": facts.is_widescreen,
            "theme": facts.theme_name() or "读不到",
            "theme_known": theme_known,
            "fonts_set_ratio": (round(facts.fonts_set_ratio, 4)
                                if facts.fonts_set_ratio is not None else None),
            "layout_kinds": facts.layout_kinds,
            "layout_top_share": round(facts.layout_top_share, 4),
            "pictures": facts.pictures, "charts": facts.charts,
            "tables": facts.tables,
            "default_look": default_look,
        }
        if default_look:
            fails.append(Finding(
                "FAIL", "look_is_default",
                f"外观是**纯默认**的：主题不在内核已知主题里（{facts.theme_name() or '读不到'}）、"
                f"显式设过字体的 run 只占 {fonts_set:.0%} —— "
                f"典型的「起手空白模板、一次没打扮」。这份**能讲，但拿不出手**。"
                f"改法：`style_deck(pptx=\"deck.pptx\", theme=\"{DEFAULT_DECK_THEME}\")`。",
                [f"theme={facts.theme_name()}", f"fonts_set_ratio={fonts_set:.3f}"]))
        else:
            # 只在**读到了**主题、而它不是内核认识的那几套时才提示。
            # `theme_name() == ""` 是"读不到主题"（包里没有 theme1.xml）——
            # 真实 .pptx **必然**带 theme1.xml，所以那只可能是极简夹具 ⇒ 不吵人。
            if facts.theme_name() == "other":
                warns.append(Finding(
                    "WARN", "theme_unknown",
                    f"主题不是内核已知的（{facts.theme_name() or '读不到'}）——"
                    f"如果你自己配过色，忽略这条；否则调 `style_deck` 一键套。",
                    [f"theme_colors={facts.theme_colors}"]))
            # ★ 判据修正（2026-09-21）：原判据是 `layout_top_share > 0.7`，
            #   实测会在**任何一份正常的学术汇报**上永久报 WARN —— 一份 13 页
            #   组会 PPT 的分布是「封面 1 ＋ 内容 10 ＋ 图表 2」（正文页占 77%），
            #   这是**合理的版式分工**，不是单调。而它会把门永久钉在
            #   REVIEW REQUIRED、**永远到不了 PASS** ⇒ 一条永远亮着的警告
            #   等于没有警告（设计定律 10：误报比漏报更伤信任）。
            #   「单调」的真义是**通篇没有版式分工**，所以判 `layout_kinds == 1`。
            #   ⚠️ 用 `== 1` 不用 `<= 1`：`layout_kinds == 0` 是"**量不到**版式"
            #   （包里没有 `slides/_rels`），那是"没查"，不能读成"只有一种"。
            if facts.layout_kinds == 1 and facts.slide_count >= 5:
                warns.append(Finding(
                    "WARN", "layout_monotone",
                    f"全篇 {facts.slide_count} 页**只用了一种版式** —— "
                    f"通篇「标题＋项目符号」读起来像 Word 文档贴上来的。"
                    f"标题页/单点页/图表页/收束页值得换版式。",
                    [f"layout_kinds={facts.layout_kinds}",
                     f"layout_top_share={facts.layout_top_share:.3f}"]))
            # 门槛放到 10 页：6 页纯文字的**概念讲解**是合法的，
            # 对它报"没有图"是误报（误报比漏报更伤，设计定律 10）。
            if facts.slide_count >= 10 and (facts.pictures + facts.charts
                                            + facts.tables) == 0:
                warns.append(Finding(
                    "WARN", "no_visual_elements",
                    f"{facts.slide_count} 页里一张图/表/图表都没有 ——"
                    f"凡是有数字的页，用图说比用字说快，也更像幻灯片。",
                    ["pictures=0, charts=0, tables=0"]))

        # ★ 骨架几何与画布**同源**吗（2026-09-21 新增；纯坐标比较、零估算）。
        #   症状：`style_deck` 把画布从 4:3 改成 16:9，**骨架没跟着改** ——
        #   master 里的占位符还是按 4:3 摆的 ⇒ 每页内容挤在左边、右侧空一大块。
        #   实测那份成片**自带对照**：同一页上，`style_deck` 自己加的页脚铺到
        #   **95.4%**，而继承来的 Title/Content 只到 **71.2%** —— 两套坐标系。
        #   ⇒ 判据**只读 master**：占位符的几何最终都落在那里，
        #     不必解析 slide→layout→master 那条继承链（那条链上 slide 和 layout
        #     两层实测**一个 `a:xfrm` 都没有**）。
        #   ⚠️ 位置放在 `if default_look / else` **之外**：几何错配与"打没打扮"
        #     是两件事，掉进 else 里会让"没打扮的稿子"永远查不到几何。
        if facts.master_geo and facts.slide_cx:
            W = facts.slide_cx
            offc: list[str] = []
            stale: list[str] = []
            for geo in facts.master_geo:
                # ⚠️ 页脚/日期/页码**本来就该靠边** —— 拿它们判不对称是纯误报。
                #    实测 master 的 `sldNum`：ml=6553200 / mr=457200（14.3 倍），
                #    不排除的话**每一份** deck 都会被报。
                if geo.get("ph") not in CONTENT_PH_TYPES:
                    continue
                ml = geo["x"]
                mr = W - (geo["x"] + geo["cx"])
                lo, hi = min(ml, mr), max(ml, mr)
                tag = str(geo.get("ph") or geo.get("idx"))
                if lo < 0:
                    offc.append(tag)
                elif lo > 0 and hi > lo * GEO_ASYM_MAX:
                    stale.append(f"{tag} {hi / lo:.1f}×")
            if offc:
                warns.append(Finding(
                    "WARN", "placeholder_offcanvas",
                    f"骨架**超出画布**：{offc[:4]} —— 占位符的框比页面还大，"
                    f"内容会被截掉。多半是画布改过、而占位符几何没跟着改。",
                    [f"canvas={W}", f"offcanvas={offc[:4]}"]))
            elif stale:
                warns.append(Finding(
                    "WARN", "placeholder_geometry_stale",
                    f"骨架几何与画布**不同源**：{stale[:4]} 的左右边距严重不对称 —— "
                    f"占位符是按**另一个**画布尺寸摆的，于是内容挤在一侧、"
                    f"另一边空出一大块。`style_deck` 会把画布与骨架一起对齐。",
                    [f"canvas={W}", f"asym={stale[:4]}"]))
        else:
            skips.append(Finding(
                "SKIP", "geometry_unmeasurable",
                "读不到 master 的内容占位符几何（没有 `a:xfrm`）⇒ 骨架这一项**没查**"))

        # ── 文字溢出（2026-09-21 接上；此前**只有估算、没有判据**）──────────
        #  调研报告 §4 的 P0-2 说「文字塞不下」是荣珂产出「丑」的最常见形态之一，
        #  而且完全可复算。落地分了两步，**分界点正是"这条判据该不该接"**：
        #    第一步（同日早先）：只做估算。因为**一个形状都量不到** ——
        #      根因是几何在 master、字号在 master 的 `txStyles`（见 `PhChain`）。
        #    第二步（现在）：继承链解析出来之后，量到了，才谈得上定门槛。
        #  ⚠️ 这是**估算，不是排版**（没有字体度量/断行/字距）⇒ 门槛必须放到
        #    估算误差**之外**，而且只报 WARN。**误报比漏报更伤**（定律 10）。
        #  ⚠️ **这条判据只对「没有自适应」的形状说话**：模板若在 master 上声明了
        #    `normAutofit`，占位符会被渲染器自动缩字、本就不会溢出，于是**全部豁免**。
        #    实测那份成片：24 个形状里 **22 个**落在豁免里，能说话的只有作者自己加的文本框。
        #    ⇒ **别把它读成"占位符不会溢出"**，它只是**不说话**。
        #  ⚠️ **`overflow_measured == 0` 是「没查」，不是「合规」**——必须走 SKIP。
        #    这条本项目栽过四次了，第五次不许再栽。
        if facts.overflow_measured > 0:
            _r = facts.overflow_worst
            # ★ 页号**早就在手里**：`facts.slides[i]["overflow_ratio"]` 就是每页的页内最大比值。
            #   此前这条只说"有形状……请打开**那一页**看一眼再说"，**却从不说哪一页** ——
            #   判据在要求人做一个它没给坐标的动作（与隔壁 `slide_without_title` 的写法不对称）。
            #   ⚠️ 口径同 `slide_without_title`：`s["slide"]` 是**页码**（演示顺序，人能翻到那一页），
            #   不是分片号 —— 见 `probe_pptx` 里那句"枚举顺序 = 演示顺序，标签用页码"。
            _over = [s["slide"] for s in facts.slides
                     if (s.get("overflow_ratio") or 0) >= OVERFLOW_WARN_RATIO]
            if _r is not None and _r >= OVERFLOW_WARN_RATIO:
                warns.append(Finding(
                    "WARN", "text_overflows_box",
                    f"第 {_over[:6]} 页有形状的文字**估高超过可用高 {_r:.2f} 倍**（门槛 "
                    f"{OVERFLOW_WARN_RATIO:.2f}，共 {len(_over)} 页）—— "
                    f"文字很可能被挤出边框或被裁掉。"
                    f"⚠️ 这是**估算**（无字体度量），所以只报 WARN：请打开那几页看一眼再说。",
                    [f"worst={_r:.3f}", f"measured={facts.overflow_measured}",
                     f"slides={_over[:8]}"]))
        else:
            skips.append(Finding(
                "SKIP", "overflow_unmeasurable",
                f"一个形状都量不到文字溢出（分类计数 {facts.overflow_skipped or '空'}）"
                f"⇒ 这一项**没查**，不是「合规」。常见原因：模板在 master 上声明了 "
                f"`normAutofit`（字多了自己收，于是本就不会溢出）—— 那批豁免就是它。"))

        # ── 论证层（2026-09-21 新增）──────────────────────────────
        #  依据：`docs/ppt-skill-survey-20260921.md` 的 §3。那张"判据铺到哪一层"
        #  的表里，荣珂在**① 论证层**的判据数是 **0**，而四家外部实践
        #  （PPT Master / CyberPPT / academic-pptx-skill）在这一层都有可跑的检查。
        #  它也是最便宜的一层：**数据早就躺在 `facts.slides` 里**
        #  （`title_text` / `text_chars`），只是没进判据 —— 和 `pictures` 同一个道理。
        #  ⚠️ 两条都刻意做成 **WARN ＋ 高门槛**：论证层最容易误伤（**章节页的标题
        #  本来就该是名词短语**），刚在 §19 吃过"判据过度收紧"的亏。
        #
        #  ⛔ **刻意不做**的一条（调研报告 P0-3 的第三项）："没有 References 页而
        #  含图表 ⇒ WARN"。理由是它会在**本项目的真实产物上天天误报** ——
        #  组会汇报本来就不放参考文献页（实测那份 13 页成片就没有）。
        #  ⇒ 判据必须长在**这类任务的真实形态**上，而不是长在别人的最佳实践上。
        titled = [s for s in facts.slides if (s.get("title_text") or "").strip()]
        if len(titled) >= 5:
            # 名词短语的**粗**判据：短 ＋ 无标点 ＋ 无判断/连接词。
            # 刻意不做中文分词（做不到可靠），只回答一个粗问题：
            #   "读者光看标题，读不读得出你的结论？"
            # ⚠️ 标点表里**一个横线都不放** —— 标题里 `4–15℃` 这类连接号很多，
            #    若把横线算作标点，它就会被读成"句子"，整条判据悄悄失效（G 型）。
            punct = "，。：；、！？,.:;!?"
            verb = re.compile("是|为|需|要|应|能|会|不|使|导致|来自|说明|表明|"
                              "支持|超过|低于|高于|随|由|在于|取决|显示|发现|"
                              "证明|意味着|并非|才是")
            noun = 0
            for s in titled:
                t = s["title_text"]
                if (len(t.strip()) < 12 and not any(c in t for c in punct)
                        and not verb.search(t)):
                    noun += 1
            if noun / len(titled) >= 0.7:
                warns.append(Finding(
                    "WARN", "titles_are_noun_phrases",
                    f"{noun}/{len(titled)} 页的标题是**名词短语**（短、无结论）—— "
                    f"读者只读标题读不出你的判断。学术汇报的标题该是**一句话结论**"
                    f"（「低温必要，尚不充分」，而不是「温度的影响」）。",
                    [f"noun_titles={noun}", f"titled={len(titled)}"]))

        # 末页：学术汇报该**停在结论或下一步**上。
        #  依据同源（academic-pptx-skill 的最小设计标准）：`Thank You` / 纯空白
        #  在学术场合是收尾不合格的典型形态。只查**末页标题**，不查正文里
        #  偶然出现的"感谢"，避免误伤。
        if len(facts.slides) >= 4:
            last = facts.slides[-1]
            lt = (last.get("title_text") or "").strip().lower()
            if any(k in lt for k in ("thank you", "thanks", "谢谢", "感谢")):
                warns.append(Finding(
                    "WARN", "ends_with_thanks",
                    "末页是致谢/道别页 —— 学术场合通常该**停在结论或下一步**，"
                    "而不是「谢谢聆听」。",
                    [f"last_title={last.get('title_text') or ''}"]))
            elif (last.get("text_chars") or 0) == 0:
                warns.append(Finding(
                    "WARN", "last_slide_blank",
                    "末页**一个字的正文都没有** —— 收尾页该给出结论或下一步，"
                    "空白页投出来很难看。", ["text_chars=0"]))

        g.findings = fails + warns + skips
        if fails:
            g.verdict = "FIX BEFORE DELIVERY"
            g.detail = "；".join(f.message for f in fails[:2])
        elif warns:
            g.verdict = "REVIEW REQUIRED"
            g.detail = f"{len(warns)} 条版式提示（不阻断，但请逐条看）"
        return g

    # ── 门 2b：版心利用率（2026-09-27 · W5）───────────────────
    def _gate_density(self, facts: DeckFacts) -> GateResult:
        """量每一页的版心利用率，过低就报。**全部 WARN，不阻断。**

        为什么默认只 WARN、不 FAIL（设计定律 10）：
          · 不是每一页都该塞满 —— `section`/`cover` 的留白是**刻意的**（已豁免，见下）；
          · 一条会在合格 deck 上报错的检查，会教模型学会无视整份体检报告。
        ⚠️ 阈值是工单拍的四档、未做本库校准；本门**照它报**，不擅自调低把问题压掉
          （调阈值要单独说，见执行报告 §五）。
        """
        g = GateResult(name="density", verdict="PASS")
        if facts.slide_count == 0:
            g.verdict = "NOT APPLICABLE"
            g.detail = "没有页可量"
            return g

        per: list[dict] = []
        warns: list[Finding] = []
        measured = exempted = unmeasured = 0
        for s in facts.slides:
            kind = str(s.get("kind") or "")
            util = s.get("util")
            per.append({
                "slide": s["slide"], "kind": kind,
                "util": (round(util, 4) if util is not None else None),
                "util_box": (round(s["util_box"], 4)
                             if s.get("util_box") is not None else None),
            })
            if kind in DENSITY_EXEMPT_KINDS:
                exempted += 1                 # cover/section：留白是有意的，跳过
                continue
            if util is None:
                unmeasured += 1               # 量不到几何 ⇒ "没查"，不许读成"合规"
                continue
            measured += 1
            sev = _density_severity(util)
            if not sev:
                continue                      # ≥ 40% ⇒ 通过
            warns.append(Finding(
                "WARN", "slide_low_fill",
                f"第 {s['slide']} 页（{kind or '未知页型'}）版心利用率 {util * 100:.1f}%，"
                f"偏低（{sev}）—— {_density_fix(kind, s.get('bullets') or 0)}",
                [f"slide={s['slide']}", f"kind={kind or 'unknown'}",
                 f"util={util:.3f}", "util_box="
                 + (f"{s['util_box']:.3f}" if s.get("util_box") is not None else "NA"),
                 f"severity={sev}"]))

        g.extra["density"] = {
            "per_slide": per, "measured": measured,
            "exempt": exempted, "unmeasured": unmeasured,
            "thresholds": {"high_lt": DENSITY_HIGH_MAX, "medium_lt": DENSITY_MEDIUM_MAX,
                           "low_lt": DENSITY_LOW_MAX},
        }

        if measured == 0 and exempted == 0:
            # 一页都没量到、也没页可豁免 ⇒ 这一项**没查**（M 型：别说成"通过"）。
            g.findings = [Finding(
                "SKIP", "density_unmeasurable",
                "量不到任何一页的形状几何（多为无 `a:xfrm` 的合成夹具）⇒ 版心利用率**没查**")]
        else:
            g.findings = warns
            if warns:
                g.verdict = "REVIEW REQUIRED"
                g.detail = (f"{len(warns)} 页版心利用率偏低（不阻断，但请逐页补密度）："
                            + "、".join(f"第 {f.evidence[0].split('=')[1]} 页"
                                       for f in warns[:6]))
            else:
                g.detail = f"已量 {measured} 页均在阈值上"
        return g

    # ── 门 2c：文字-背景对比度（2026-09-27 · W8）─────────────────
    def _gate_contrast(self, facts: DeckFacts, deep_contrast: bool = False,
                       pptx_path: str | Path | None = None) -> GateResult:
        """文字与它下方填充色的对比度。**全部 WARN，不阻断**（设计定律 10）。

        `deep_contrast=True`（W9 任务 2，**默认关闭**）：对纯 XML"量不到"的
        文字形状追加一条**渲染像素校验**（分布口径，见 `_contrast_pixel_pass`）。
        关闭时本门的 verdict / detail / findings / extra **与 W8 逐字相同**
        （`deck_test` 28.4 钉死）；开启也不改上面任何一条既有判定，只**追加**。

        口径（就按工单 §三② 那套算法，不自行发明）：
          · 背景 = 同一页里与文字框**重叠面积最大**的非文字实心形状（且至少盖住
            文字框 `_CONTRAST_MIN_COVER`，否则那只是一根擦边的装饰短线）；
            找不到 ⇒ 退到形状自己的 `solidFill`；再没有 ⇒ **页面底色**（主题 `bg1`）。
          · 门槛 = WCAG 2.1 AA：正文 4.5:1；大字（≥18pt，或 ≥14pt 粗体）3:1。
          · 压在**图片 / 渐变**上、或字色字号继承母版读不出 ⇒ 记 `INFO
            contrast_unmeasurable` 并**跳过**。★ 这一处是"没查"，不是"合格"；
            一条都没量到 ⇒ 整门 **不是 PASS**（`NOT APPLICABLE` + 一条 `SKIP`
            finding，理由见函数末尾那段"为什么不用 SKIPPED"）。
        ⚠️ 为什么只 WARN：纯 XML 拿不到照片亮度，本维天生会漏、也会把"量不到"
          说得比实际严重。让它阻断，就是拿一个不完整的事实去拦交付（定律 10）。
        """
        g = GateResult(name="contrast", verdict="PASS")
        if facts.slide_count == 0:
            g.verdict = "NOT APPLICABLE"
            g.detail = "没有页可量"
            return g
        page_bg = _theme_slot_hex(facts.theme_colors or {}, "bg1")
        per: list[dict] = []
        warns: list[Finding] = []
        infos: list[Finding] = []
        measured = 0
        unmeasured = 0
        seen: set[tuple] = set()            # 同一形状里同样式的 run 只判一次（20 条同色要点 ≠ 20 条告警）
        pix_targets: list[dict] = []        # deep_contrast=True 时的像素校验目标（只在这一个开关下收集）
        for s in facts.slides:
            no = s["slide"]
            kind = str(s.get("kind") or "")
            fills = s.get("fill_shapes") or []
            page_un: list[str] = []
            for t in s.get("text_shapes") or []:
                nm = str(t.get("name") or "未命名形状")
                bg, src = _contrast_backdrop(t.get("rect"), t.get("own_fill"),
                                             fills, page_bg)
                pix_runs: list[tuple] = []
                for run in t.get("runs") or []:
                    pt, fg = run.get("pt"), run.get("rgb")
                    why = None
                    if bg is None:
                        why = src
                    elif fg is None:
                        why = "字色继承母版、XML 里没写"
                    elif pt is None:
                        why = "字号继承母版、XML 里没写（选不出该用哪档门槛）"
                    if why:
                        unmeasured += 1
                        tag = f"「{nm}」{why}"
                        if tag not in page_un:
                            page_un.append(tag)
                        per.append({"slide": no, "shape": nm, "status": "unmeasurable",
                                    "reason": why})
                        if deep_contrast:
                            pix_runs.append((pt, bool(run.get("bold"))))
                        continue
                    thr, big = _contrast_threshold(pt, bool(run.get("bold")))
                    ratio = _contrast_ratio(fg, bg)
                    key = (no, nm, fg, pt, bg)
                    if key in seen:
                        continue
                    seen.add(key)
                    measured += 1
                    row = {"slide": no, "shape": nm, "pt": pt, "fg": fg, "bg": bg,
                           "ratio": round(ratio, 2), "threshold": thr,
                           "status": "low" if ratio + 1e-9 < thr else "ok"}
                    per.append(row)
                    if row["status"] == "ok":
                        continue
                    warns.append(Finding(
                        "WARN", "text_low_contrast",
                        f"第 {no} 页（{kind or '未知页型'}）形状「{nm}」文字对比度 "
                        f"{ratio:.2f}:1，低于{'大字' if big else '正文'}门槛 {thr}:1"
                        f"（{pt:g}pt）—— 字色 {fg} 压在{src} {bg} 上。"
                        f"改法：换成与底色反差大的字色（深底配 FFFFFF、浅底配 1A1A1A），"
                        f"或给文字加一层不透明衬底"
                        + ("；若文字实际压在**照片**上，纯 XML 量不到那张照片的亮度，"
                           "请人工看渲染图确认" if "页面底色" in src else ""),
                        [f"slide={no}", f"shape={nm}", f"ratio={ratio:.2f}",
                         f"fg={fg}", f"bg={bg}", f"pt={pt:g}", f"threshold={thr}"]))
                if deep_contrast and pix_runs and t.get("rect"):
                    pix_targets.append({"slide": no, "shape": nm,
                                        "rect": t.get("rect"), "runs": pix_runs})
            if page_un:
                infos.append(Finding(
                    "INFO", "contrast_unmeasurable",
                    f"第 {no} 页有 {len(page_un)} 处文字对比度**量不到**："
                    + "；".join(page_un[:6])
                    + ("……" if len(page_un) > 6 else "")
                    + " —— 这些**没查**（纯 XML 拿不到照片/渐变的亮度），"
                      "请人工看渲染图确认，不要当成通过。",
                    [f"slide={no}", f"count={len(page_un)}",
                     f"shapes={'；'.join(page_un[:12])}"]))
        pix = None
        if deep_contrast:
            pw, pinf, pix = _contrast_pixel_pass(facts, pix_targets, pptx_path,
                                                 self.python_exe, self.timeout)
            warns += pw
            infos += pinf
        g.extra["contrast"] = {
            "per_slide": per, "measured": measured, "unmeasured": unmeasured,
            "thresholds": {"body": CONTRAST_BODY_AA, "large": CONTRAST_LARGE_AA,
                           "large_pt": CONTRAST_LARGE_PT,
                           "bold_large_pt": CONTRAST_BOLD_LARGE_PT},
            "page_bg": page_bg,
        }
        if deep_contrast:
            # ★ 只在开关打开时**追加**这一个键 —— 关闭时 extra 与 W8 逐字相同。
            g.extra["contrast"]["pixel"] = pix
        g.findings = warns + infos
        if measured == 0:
            # ★ 一处都没量到 ⇒ 这一维**没查**。绝不许读成"合格"（M 型）。
            #   ⚠️ 这里**刻意不用 `SKIPPED`**（工单 §三②.5 写的是 SKIPPED）：
            #   `GateResult.blocking` 把 `SKIPPED` 算成阻断，而"字色继承母版、
            #   XML 里没写"是 **python-pptx 产物的常态** ⇒ 一旦 SKIPPED，
            #   凡是没逐 run 写死颜色的 deck 都会永久 `FIX BEFORE DELIVERY`，
            #   模型无论怎么改都过不去 —— 那正是本库给 notes 门写下的那条死结
            #   （`deck_test` 3.10："否则纯 MCP 路径永远过不了 = 死结"）。
            #   所以取 `NOT APPLICABLE`（**不是 PASS**）+ 一条 `SKIP` finding：
            #   "没查"这件事照样进 `warnings` 与逐门明细，只是不拿它拦交付。
            #   ★ 与工单的这一处偏离是**有意的**，执行报告 §六 当面记账。
            g.verdict = "NOT APPLICABLE"
            g.detail = (f"量不到任何文字/背景配色（未量化 {unmeasured} 处，"
                        f"多为无填充、无几何的合成夹具）⇒ 对比度**没查**")
            g.findings = [Finding(
                "SKIP", "contrast_not_checked",
                f"对比度**没查**：一处文字/背景配色都没能定量"
                f"（{unmeasured} 处形状量不到，通常是字色或字号继承母版、"
                f"或整页没有 `a:xfrm`/`solidFill`）。**这不代表配色合规** —— "
                f"要让它响，出稿时逐 run 写死字色（本库 `compose_deck` 就是这么做的）。"
                + ("逐页的量不到清单见 `gate_details` 里 contrast 的明细。"
                   if infos else ""))] + warns + infos
        elif warns:
            g.verdict = "REVIEW REQUIRED"
            g.detail = (f"{len(warns)} 处文字对比度低于 WCAG 2.1 AA（不阻断，"
                        f"但投出来后排看不清）："
                        + "、".join(f"第 {f.evidence[0].split('=')[1]} 页"
                                   for f in warns[:6]))
        elif infos:
            g.verdict = "REVIEW REQUIRED"
            g.detail = (f"已量 {measured} 处均在门槛上，但有 {unmeasured} 处对比度"
                        f"**量不到**（压在图片/渐变上，或字色字号继承母版）—— "
                        f"那几处是**没查**，请人工看渲染图")
        else:
            g.detail = f"已量 {measured} 处文字/背景配色均在 AA 门槛上"
        if deep_contrast and pix and pix.get("attempted"):
            # 开关打开才走这一段：关闭时上面的 verdict/detail 与 W8 逐字相同。
            if not pix.get("rendered"):
                g.detail += (f"；像素校验**没跑成**（{pix.get('err') or '原因未明'}）"
                             f"—— {pix['attempted']} 处「量不到」的仍然**没查**")
                if g.verdict == "PASS":      # 兜底：渲染失败绝不被读成通过
                    g.verdict = "REVIEW REQUIRED"
            else:
                g.detail += (f"；像素校验量了 {pix.get('measured', 0)}/{pix['attempted']} 处"
                             f"「量不到」（分布口径：背景=占比最大的亮度档，"
                             f"文字=离它最远且占比<{PIXEL_TEXT_MAX_SHARE:.0%} 的那一撮"
                             + (f"；另有 {pix['unmeasurable']} 处**分布上找不出两撮**、"
                                f"记量不到不报数" if pix.get("unmeasurable") else "")
                             + "）")
        return g

    # ── 门 3：讲稿备注 ────────────────────────────────────
    def _gate_notes(self, facts: DeckFacts) -> GateResult:
        """覆盖率。

        ⚠️ **这里只报事实，不下最终判决。** 是否"必须"有讲稿由 `gate.py`
        从**契约**独立推出 —— 不采信模型自己传的参数（R 型：被审者定标准）。
        本门给出的 `FAIL` 只是"按默认期望不合格"，最终拦不拦看门的输入。
        """
        g = GateResult(name="notes", verdict="PASS")
        want = float(self._th("min_notes_ratio", 0.6))
        if facts.slide_count == 0:
            g.verdict = "NOT APPLICABLE"
            g.detail = "没有幻灯片"
            return g
        ratio = facts.notes_ratio
        ev = [f"slides_with_notes={facts.slides_with_notes}",
              f"slide_count={facts.slide_count}", f"ratio={ratio:.2f}"]
        if ratio + 1e-9 >= want:
            g.detail = (f"讲稿备注覆盖 {facts.slides_with_notes}/{facts.slide_count}"
                        f"（{ratio:.0%} ≥ 期望 {want:.0%}）")
            return g
        g.verdict = "REVIEW REQUIRED"   # 是否升级成阻断，由门按契约决定
        g.detail = (f"讲稿备注只覆盖 {facts.slides_with_notes}/{facts.slide_count}"
                    f"（{ratio:.0%} < 期望 {want:.0%}）")
        g.findings.append(Finding(
            "WARN", "notes_coverage",
            "组会 PPT 的备注栏就是讲稿（来源、口径、会被问到的问题）。"
            "补的办法：调 `set_slide_notes(pptx=\"deck.pptx\", notes=[每页一段])` —— "
            "上游 37 个 MCP 工具都**没有**写备注的能力，这个能力由内核直接提供"
            "（它内部用 python-pptx 写，模型不需要、也不应该自己写代码）。"
            "补完必须**重跑** `audit_deck`：改过 deck，旧报告就作废了。",
            ev))
        return g

    # ── 门 4：新鲜度 ──────────────────────────────────────
    def _gate_freshness(self, pptx: Path, started_at: float) -> GateResult:
        """报告是不是**这一版** deck 的？

        「报告在位」≠「报告对得上这一版」。盘上躺着一份全绿的旧报告
        是最容易蒙混过关的假证据，所以必须比 mtime。
        """
        g = GateResult(name="freshness", verdict="PASS")
        if not pptx.is_file():
            g.verdict = "NOT AUDITABLE"
            g.detail = "找不到被审的 .pptx"
            return g
        m = pptx.stat().st_mtime
        # 容差 2 秒：写盘与取时间里外差一点点不该算"过期"。
        if m > started_at + 2.0:
            g.verdict = "STALE"
            g.detail = ("被审的 .pptx 比这次审计还新 —— 审完又改过它。"
                        "**改过就得重跑**，旧报告不算合格证。")
            g.findings.append(Finding(
                "FAIL", "stale_artifact",
                f"pptx mtime={time.strftime('%H:%M:%S', time.localtime(m))} "
                f"晚于审计开始 {time.strftime('%H:%M:%S', time.localtime(started_at))}"))
        return g

    # ── 总入口 ────────────────────────────────────────────
    def audit(self, pptx: str | Path, workdir: str | Path | None = None,
              report_path: str | Path | None = None,
              deep_contrast: bool | None = None) -> "DeckReport":
        started_at = time.time()
        # 像素校验开关（W9 任务 2）：显式参数优先，其次配置项，**默认关闭**。
        # 为什么默认关：本机实测 COM 渲染 5 页 = 15.1s，而各门本是秒级 ——
        # 设成默认会让每次体检慢一个数量级（`_gate_contrast` 关闭时逐字不变）。
        dc = bool(self._th("deep_contrast", False)) if deep_contrast is None \
            else bool(deep_contrast)
        # ★ **在入口处一次性解析成绝对路径**，之后处处只用 `p`。
        # 踩过（K 型）：下面跑审计脚本时 `cwd=str(pptx.parent)`，
        # 而喂给子进程的还是那个**相对**路径 ⇒ 子进程在新 cwd 下找不到文件，
        # 于是报 "PPTX not found"，xml 门判 SKIPPED ⇒ 整份报告变成"阻断"，
        # 看起来像 deck 有问题，其实是路径没解析。
        # 本项目纪律：**凡是自己起子进程的工具，一律显式钉绝对路径。**
        p = Path(pptx)
        if not p.is_absolute() and workdir:
            cand = Path(workdir) / p
            if cand.exists():
                p = cand
        p = p.resolve()
        raw_json = Path(report_path) if report_path else (p.parent / "deck_audit.json")
        raw_json = raw_json.resolve()
        rep = DeckReport(
            pptx=str(p),
            workdir=str(workdir or p.parent),
            started_at=started_at,
            pptx_mtime=(p.stat().st_mtime if p.is_file() else 0.0),
        )
        facts = probe_pptx(p)
        rep.facts = facts
        if facts.problems and facts.slide_count == 0:
            g = GateResult(name="open", verdict="NOT AUDITABLE",
                           detail="；".join(facts.problems[:3]))
            rep.gates.append(g)
            rep.gates += [GateResult(name="xml", verdict="SKIPPED",
                                     detail="文件打不开，谈不上审计"),
                          GateResult(name="type", verdict="SKIPPED", detail="同上"),
                          GateResult(name="density", verdict="SKIPPED", detail="同上"),
                          GateResult(name="contrast", verdict="SKIPPED", detail="同上"),
                          GateResult(name="notes", verdict="SKIPPED", detail="同上")]
        else:
            rep.gates.append(GateResult(name="open", verdict="PASS",
                                        detail=f"{facts.slide_count} 页 / "
                                               f"{facts.media_count} 个媒体文件"))
            rep.gates.append(self._gate_xml(p, raw_json))
            rep.gates.append(self._gate_type(facts))
            rep.gates.append(self._gate_density(facts))
            rep.gates.append(self._gate_contrast(facts, deep_contrast=dc,
                                                 pptx_path=p))
            rep.gates.append(self._gate_notes(facts))
        rep.gates.append(self._gate_freshness(p, started_at))
        rep.notes += self.notes
        return rep


# ══════════════════════ 总报告 ══════════════════════
@dataclass
class DeckReport:
    pptx: str = ""
    workdir: str = ""
    facts: DeckFacts = field(default_factory=DeckFacts)
    gates: list[GateResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # `strict_notes=True` 时（由 gate 按契约推出）备注不足升级成阻断。
    # **字段在报告里，但它不是报告的自我判定** —— 它是"门当时拿的是哪把尺子"
    # 的记录（P 型：现值 vs 当时值，这份是当时值）。
    strict_notes: bool = False
    started_at: float = 0.0
    pptx_mtime: float = 0.0
    report_path: str = ""

    @property
    def blockers(self) -> list[str]:
        out: list[str] = []
        for g in self.gates:
            if g.verdict in ("FIX BEFORE DELIVERY", "NOT AUDITABLE", "STALE", "SKIPPED"):
                bad = [f for f in g.findings if f.level in ("FAIL", "WARN")]
                extra = "\n" + "\n".join(
                    f"    · [{f.level}] {f.check_id} {f.message}" for f in bad[:6]) if bad else ""
                out.append(f"【{g.name} 门：{g.verdict}】{g.detail}{extra}")
            elif g.name == "notes" and self.strict_notes and not g.ok:
                out.append(f"【notes 门：FIX BEFORE DELIVERY】{g.detail}\n"
                           "    · 本轮契约**明确要了讲稿/备注** ⇒ 备注不足按阻断处理")
        return out

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        for g in self.gates:
            if g.verdict == "REVIEW REQUIRED" and not (g.name == "notes" and self.strict_notes):
                out.append(f"[{g.name}] {g.detail}")
                out += [f"    · {f.check_id} {f.message}" for f in g.findings[:6]]
            else:
                # `SKIP` 也进 warnings：它是**「这一项没查」**的如实交代。
                # 不放进来的话，"字号量不出来"就只活在 `report_text` 里，
                # 机器读的那份紧凑载荷会只字不提 —— 那又是一次
                # "没检查"被压成"没问题"（M 型）。
                out += [f"[{g.name}/{f.level}] {f.check_id} {f.message}"
                        for f in g.findings if f.level in ("WARN", "SKIP")
                        and not (g.name == "notes" and self.strict_notes)]
        # ⚠️ `facts.problems` 是 **`list[str]`**（见 DeckFacts 的字段声明，三处 append
        #    全都在塞纯字符串），**不是 Finding 列表** —— 这里曾经写成
        #    `f"[{f.check_id}] {f.message}"`，于是 `format_for_model()` 在
        #    **"文件不存在" / "不是合法 .pptx"** 这两条路径上必然 `AttributeError`。
        #    实测（runs/20260921-113729）：模型把 `.docx` 交给 `audit_deck`，
        #    拿回来的是 `'str' object has no attribute 'check_id'`，
        #    而不是「这不是 pptx」——**报告恰好在最需要它的那一刻崩掉**。
        #    为什么 146 条断言的 `deck_test.py` 没抓到：第 2 节只验到 **facts 层**
        #    （`slide_count == 0 and problems`），没人把这两条输入喂进
        #    `audit()` + `format_for_model()`。「记下了」≠「显示得出来」。
        out += [f"[problem] {s}" for s in self.facts.problems]
        out += self.notes
        return out

    @property
    def verdict(self) -> str:
        if self.blockers:
            return "FIX BEFORE DELIVERY"
        for g in self.gates:
            if g.verdict == "REVIEW REQUIRED":
                return "REVIEW REQUIRED"
        return "PASS WITH WARNINGS" if self.warnings else "PASS"

    @property
    def ok(self) -> bool:
        return not self.blockers

    def summary_json(self) -> dict:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "pptx": self.pptx,
            "slide_count": self.facts.slide_count,
            "notes_ratio": round(self.facts.notes_ratio, 4),
            "min_body_font_pt": self.facts.min_body_font_pt,
            "strict_notes": self.strict_notes,
            "gates": {g.name: g.verdict for g in self.gates},
            "blockers": self.blockers,
            "warnings": self.warnings[:12],
            "report_path": self.report_path,
            "audited_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)),
            "audited_at_epoch": float(self.started_at or 0.0),
            "pptx_mtime": float(self.pptx_mtime or 0.0),
        }

    def to_dict(self) -> dict:
        d = self.summary_json()
        d.update({
            "workdir": self.workdir,
            "facts": self.facts.to_dict(),
            "gate_details": [g.to_dict() for g in self.gates],
            "notes": self.notes,
        })
        return d

    def save(self, path: str | Path) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # **先记路径再序列化**：反过来报告里 report_path 会是空串 ——
        # 报告不知道自己住哪（字段在、值为空，比字段不在更难发现）。
        self.report_path = str(p)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return str(p)

    def format_for_model(self) -> str:
        f = self.facts
        lines = [f"═══ 组会 PPT 体检报告 · {Path(self.pptx).name} ═══",
                 f"判定：{self.verdict}",
                 f"页数：{f.slide_count}   媒体：{f.media_count}   "
                 f"讲稿备注：{f.slides_with_notes}/{f.slide_count}"
                 f"（{f.notes_ratio:.0%}）",
                 "正文字号下限："
                 + (f"{f.min_body_font_pt:.1f} pt" if f.min_body_font_pt is not None
                    else "未显式声明（继承母版）⇒ 这一项没查"),
                 "",
                 "逐门："]
        for g in self.gates:
            mark = {"PASS": "✔", "NOT APPLICABLE": "—", "REVIEW REQUIRED": "△",
                    "FIX BEFORE DELIVERY": "✘", "NOT AUDITABLE": "?",
                    "SKIPPED": "·", "STALE": "⏱"}.get(g.verdict, "?")
            lines.append(f"  {mark} {g.name:<10} {g.verdict:<20} {g.detail}")
        if self.blockers:
            lines += ["", "阻断项（必须解决才能交付）："] + [f"  {b}" for b in self.blockers]
        if self.warnings:
            lines += ["", "警告（逐条复核并说明，或改掉）："] + [f"  {w}" for w in self.warnings[:20]]
        lines += ["", "交付前自查（别只瞟一眼整份 deck）：",
                  "  | 页 | 字数 | 有标题 | 正文最小字号 | 讲稿字数 |",
                  "  |---:|---:|:---:|---:|---:|"]
        for s in f.slides[:30]:
            fs = f"{s['min_font_pt']:.0f}" if s.get("min_font_pt") is not None else "继承"
            lines.append(f"  | {s['slide']} | {s['text_chars']} | "
                         f"{'✔' if s['has_title'] else '✗'} | {fs} | {s['notes_chars']} |")
        lines.append("═══════════════════════════════════════")
        return "\n".join(lines)
