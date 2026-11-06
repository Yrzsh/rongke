# -*- coding: utf-8 -*-
"""图面契约与体检 —— 让「这张图能不能交」变成可程序化核对的事。

**为什么要有这个模块**

写作有 `writing.py` 做稿面体检，但图一直是盲区：模型画完一张图，说一句
「图已生成」就过了门。而图最贵的失败模式恰恰是「**所有程序都返回 0，图是错的**」——
本项目已经踩过的同类事故：字形缺失只发一条 `UserWarning`、渲染出豆腐块 `□`、
脚本 exit 0。人眼不逐张看就发现不了。

所以这里把 nature-figure 的合同条款翻译成**能跑的判据**：

  1. **五点契约**（核心结论 / 证据链分面 / 原型 / 后端 / 期刊导出契约）
     —— 翻译成 `FigureContract.validate()`。其中「每个分面必须承担**不同的**推断职责」
     是硬门：两个分面画同一件事，就必须合并或降级。
  2. **四个渲染后审计**（源码预检 / PDF 字形下限 / 版面碰撞 / 分面对齐）
     —— 全部通过子进程调用，解析它们**真实**的 JSON 报告，不猜格式。
  3. **新鲜度**：报告必须比它审的东西**新**。
  4. **图注体检**：`Fig. N | 加粗名词短语` + 分面 + 统计 + 源数据套语。

**这一条是从别处搬来的教训（2026-09-16，本机 dsh 弹窗事故）**

    当时现象：磁盘上补丁**在位**，症状却**没消失**。
    根因：服务启动时刻（16:09）**早于**补丁落盘时刻（16:17）—— 一个字节都没加载。
    结论：**「东西在」不等于「东西生效」**，必须核对时间戳。

图这边是同一个陷阱，而且更容易掉进去：审计报告文件明明躺在目录里、
内容全绿，但它是**上一版图**的。nature-figure 的 QA 合同原话就是
"Do not reuse a report from an earlier render." —— 所以本模块把
「报告 mtime ≥ 被审对象 mtime」做成**硬判据**（`_freshness_findings`）。
旧报告不是「通过」，是 `STALE`，直接阻断。

**为什么是零依赖**

渲染后的审计要用 matplotlib / PyMuPDF，它们只装在系统 Python312；
而荣珂自己可能跑在 3.13（两个都没有）。**跑到哪个解释器上，不该决定它能不能审图**——
和 MCP 客户端当初的判断同一条：需要特定解释器的是**被调用的那一侧**，
所以由配置（`figure.python`）指路，本模块自己只用标准库。

**后端排他性 ≠ 只能用 Python**

nature-figure 规定「后端一经选定即排他」，本模块把它翻成可执行的一条：
**一张图只能有一个渲染引擎**（`RenderLedger` 里出现第二种引擎即硬失败）。
它的原意是禁止「用 Python 画一张图给 R 的图当预览」这类混渲，
**不是**禁止 Origin 之类的第三方渲染器。而且四个审计器自己就写明是
backend-neutral（"may inspect a layout manifest or final PDF from either backend"）——
它们只量几何，不重画内容。所以 Origin 画的图照样能过审计。
（唯一的真缺口：Origin 不给「分面几何清单」，多分面对齐它无从可审 —— 见 `_alignment`。）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ARCHETYPES", "BACKENDS", "PALETTE", "JOURNAL_LEGEND_CAP",
    "SUBMISSION_BACKENDS", "NATIVE_BACKENDS", "RouteDecision", "route_for",
    "Panel", "Exclusion", "FigureContract", "Finding", "GateResult",
    "FigureReport", "FigureAuditor",
    "capture_missing_glyphs", "render_legend", "audit_legend",
    "find_figure_python", "locate_scripts", "GATE_CLIS",
]


# ══════════════════════════════════════════════════════════════════
# 常量：合同里写死的东西
# ══════════════════════════════════════════════════════════════════

# 四种原型。合同要求**声明**，因为原型决定了版式与分面层级，
# 不声明就只能凭手感排，排出来的东西没法审。
ARCHETYPES = (
    "quantitative grid",          # 等权定量网格：分面同质，纯数据
    "schematic-led composite",    # 示意图主导 + 定量佐证
    "image plate + quant",        # 图像版（显微/印迹）+ 定量
    "asymmetric mixed-modality",  # 非对称混合模态：主次分明
)

# 允许的渲染后端。origin 是本项目加的 —— 见模块头的「后端排他性」。
BACKENDS = ("python", "r", "origin", "other")

# ── 渲染路线的分工（2026-09-18 定案）────────────────────────────
#
# 问题：nature-figure 说后端排他，而 Origin/MCP 那条出图路线已经建好了。
# 二者不冲突（防的是"同一张图两个引擎混渲"），但**到底什么时候该走哪条**，
# 如果不写死，模型每画一张图都要重新纠结一次 —— 而且纠结的结果不可复现。
#
# 所以定下分工。**判据不是"图是什么类型"，而是"这张图要不要被审计到全绿"**：
#
#   投稿渲染路线（python / r）—— 默认路线。满足下面任一条即为**必须**：
#     ① 分面数 ≥ 2         → 对齐门需要后端自己量出的「分面几何清单」，只有它产得出
#     ② 有定量分面         → 要源数据可追溯 + 字形下限 + 精确物理尺寸
#     ③ 声明了 journal     → 要进投稿包，可编辑矢量 + 600 dpi 位图 + 源数据
#
#   原生矢量图形路线（origin / other）—— 仅限：
#     单分面 + 无定量 + 不进投稿包的示意 / 装置 / 流程图的图形合成。
#     **代价必须明说**：拿不到几何清单 ⇒ 对齐门 `NOT APPLICABLE`（单面图本就合法）；
#     没有绘图源码 ⇒ 源码预检 `SKIPPED`。PDF 字形下限与碰撞审计**照样跑**
#     —— 那两道按最终 PDF 量，与后端无关。
#
# 为什么不是"允许 Origin 画多分面图 + 补一份几何清单"：
# 那会留下一个"审不了但放行"的口子，正是本项目刚堵掉的那类洞（M 型缺陷）。
# 宁可**在画之前**就拦下来，也不要画完了才说对齐审不了。
SUBMISSION_BACKENDS = ("python", "r")
NATIVE_BACKENDS = ("origin", "other")

# 只保留「一个引擎」这条排他性；引擎叫什么不限。
_RENDER_ENGINE_ALIAS = {
    "python": "python", "matplotlib": "python", "py": "python",
    "r": "r", "rscript": "r", "ggplot2": "r", "patchwork": "r",
    "origin": "origin", "originlab": "origin",
}

# 期刊图注字数上限。Nature 旗舰 250（硬），NMI 300（2018 指南，历史建议值）。
# 其余子刊**不设**数字上限 —— 手册明确说「verify the current journal instructions」，
# 所以我给一个保守的默认值并标成 advisory，不当硬门。
JOURNAL_LEGEND_CAP: dict[str, tuple[int, bool]] = {
    "nature": (250, True),
    "nature machine intelligence": (300, False),
    "nmi": (300, False),
    "default": (300, False),
}

# 调色板：色盲友好（不靠红绿单通道）+ 灰度可读（明度分层）。
# 取自 nature-figure references/api.md，抄成常量是为了让荣珂不装技能也能用。
#
# ⚠ 2026-10-26 实测：这一组**过不了色盲/亮度体检**，而名字里写着"色盲友好"。
#   用 figure-gate 的 check_palette.py（CAM02-UCS）与 rongke/figure_quality.py
#   两套实现分别核过，结论一致，三道硬门红：
#     · 亮度过带 ： #0F4D92 L=0.4237(带下限 0.43)、#8BCF8B L=0.7918、
#                   #CFCECE L=0.8522、#4D4D4D L=0.4202（带上限 0.77）
#     · 彩度太灰  ： #CFCECE C=0.0011、#4D4D4D C=0.0000、#42949E C=0.0804
#     · **色盲分离不足**：#0F4D92（蓝）vs #9A4D8E（紫）在 protan 下 dE=7.7，
#       门槛 10.5 —— 红色盲读者看这两条线是同一个颜色。
#   比荣珂原先 analysis.py 里那份还差（那份最差 9.7）。
#
#   ★ 我**没有**自行改掉这些色值，理由：
#     这七个色是照技能侧 api.md 抄的，改了就和 nature-figure 的文档脱钩，
#     而技能生成图时仍会按名取色 —— 那才是真正的"两份真相"。
#     正确处置是让质检看得见它（见 figure_quality.py 与 audit() 里的 quality 门），
#     并把实际画图用的默认色序放在 analysis.PALETTE（那里已经是 Okabe-Ito，
#     实测五道门全过）。也就是说：
#       · 这份 = 供技能侧按名取用的**参考表**（带已知缺陷，已标注）
#       · analysis.PALETTE = 荣珂自己出图时的**默认色序**（已校验合格）
#
#   ★★ 2026-10-26 更正一条**我自己写错的注释**：上一版这里写
#      "本常量目前在全仓没有任何按名引用"。**这是错的。**
#     实测 `figures/plot_conversion.py` 就地抄了一份同名 PALETTE 字典
#      并在 15 处按名取色（PALETTE["blue_main"] 等），而那个脚本正是
#      `figure_test.py` 的真实审计夹具、产出的 `fig_conversion.*`
#      就是 W10 指定的「已知合格、零报告」对照件。
#      所以这份色值不是文档，是**活代码路径**。
#
#      而且那次核查还发现两处更值得记的事：
#      ① 两份已经**分叉**：plot_conversion.py 那份有 9 个色名，
#         多出 blue_secondary / neutral_mid；同名色值目前一致。
#         手抄副本早晚会漂移，这是"两份真相"的现场。
#      ② 我把这 7 色换成 Okabe-Ito 的语义映射试过一次，
#         **硬问题仍是 2 条（LIGHTNESS + CHROMA），一条没少**。
#         原因是 PALETTE-LIGHTNESS/CHROMA 的触发者主要是两个中性色
#         （#CFCECE、#4D4D4D）—— 而中性色是图上画参考线/背景带用的，
#         不能换成彩色。⇒ **换彩色解决不了这个缺陷**，别白花力气。
#         要修只能改中性色的取值，不能靠换主色。
#   ★ 结论：保持原值 + 保留这段标注。真要用它出交付图时，
#     质检会把两条 WARN 报出来（非阻断），届时按需调中性色。
PALETTE = {
    "blue_main": "#0F4D92",
    "green_3": "#8BCF8B",
    "red_strong": "#B64342",
    "neutral_light": "#CFCECE",
    "neutral_dark": "#4D4D4D",
    "teal": "#42949E",
    "violet": "#9A4D8E",
}

# 常见期刊栏宽（mm）。合同第 5 点要求「set final dimensions」，
# 这两档是双栏 183 / 单栏 89。
COLUMN_WIDTH_MM = (89.0, 183.0)

# 渲染后审计器。每一项：(名字, 脚本, 需要什么输入, 是否依赖第三方库)
GATE_CLIS: dict[str, dict[str, Any]] = {
    "source":   {"script": "validate_figure.py",        "needs": "source",    "third_party": False},
    "glyphs":   {"script": "audit_pdf_text.py",         "needs": "pdf",       "third_party": True},
    "collision": {"script": "audit_figure_collisions.py", "needs": "pdf",     "third_party": True},
    "alignment": {"script": "audit_panel_alignment.py", "needs": "manifest",  "third_party": False},
}

# 报告 mtime 允许比被审对象早多少秒。
# 不是 0：同一批写盘时文件系统时间戳粒度、以及「先写 PDF 再写报告」
# 之间可能落在同一秒。2 秒足够吸收抖动，又不至于放过真正的旧报告。
_FRESH_SLACK_S = 2.0

# 采样类审计器认得的「有意识地采样」标记（用于提示语，不参与判定）
_DEMO_BOUNDARY = re.compile(r"--demo|demo_mode|if\s*\([^)]*demo|if\s+.*demo", re.I)


# ══════════════════════════════════════════════════════════════════
# 契约：五点，逐条可校验
# ══════════════════════════════════════════════════════════════════

@dataclass
class Panel:
    """一个分面。

    `role` 是**推断职责**（inferential role），不是"画什么"。
    合同第 2 点要求每个分面承担**不同**的职责，所以这里存的是
    「趋势」「配对增量」「分布对比」这类词，而不是「折线图」「箱线图」。
    重复的职责 = 同一个论据画两遍 = 必须合并。
    """
    letter: str = ""
    role: str = ""
    claim: str = ""            # 这个分面独家回答什么问题
    center: str = ""           # mean / median / raw
    spread: str = ""           # s.d. / s.e.m. / 95% CI / none
    unit: str = ""             # 重复单位：批次 / 种子 / 受试者 …

    @classmethod
    def from_dict(cls, d: dict) -> "Panel":
        return cls(
            letter=str(d.get("letter") or "").strip(),
            role=str(d.get("role") or "").strip(),
            claim=str(d.get("claim") or "").strip(),
            center=str(d.get("center") or "").strip(),
            spread=str(d.get("spread") or "").strip(),
            unit=str(d.get("unit") or "").strip(),
        )

    def to_dict(self) -> dict:
        return {"letter": self.letter, "role": self.role, "claim": self.claim,
                "center": self.center, "spread": self.spread, "unit": self.unit}

    # 声明为"没有离散"的写法。**这是一个真踩过的坑**：
    # 旧版判据是 `bool(self.spread)`，而 `spread="none"` 是**非空字符串** ⇒ 真值
    # ⇒ 逐点原始数据的面板被当成"带误差条"，渲染路线的触发理由也就跟着说假话
    # （"分面 b 带误差/离散"，而 b 明写了 none）。**字段在、值相反**，
    # 和本项目那类"看起来到位、其实没生效"的缺陷同型。
    _NO_SPREAD = {"none", "-", "--", "na", "n/a", "no", "raw", "无", "空"}

    @property
    def quantitative(self) -> bool:
        """这个分面画的是**数据**，而不是纯示意。

        判据是"声明了中心量或离散量" —— 一个只画个体测点的面板（center=raw,
        spread=none）照样是定量面板：它同样要源数据可追溯、字形下限、
        精确物理尺寸。纯示意分面（三个字段全空）才不算。
        """
        return bool(self.center.strip()) or bool(self.spread.strip())

    @property
    def has_spread(self) -> bool:
        """这个分面**真的编码了离散**（误差条/阴影带）。

        与 `quantitative` 分开：图注体检里"要写出离散的定义"只对真有离散的
        面板提要求 —— 对一张逐点散点图要求图注写 `mean ± s.d.` 是误报，
        而**误报比漏报更伤**。
        """
        s = self.spread.strip().lower()
        return bool(s) and s not in self._NO_SPREAD


@dataclass
class Exclusion:
    """被排除的数据行 —— 合同第 5 点的数据完整性要求。

    规则很硬：**排除必须有理由，且要记录前后行数**。
    不许「为了让图好画」而悄悄丢数据。
    """
    what: str = ""
    before: int | None = None
    after: int | None = None
    rule: str = ""
    reason: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Exclusion":
        def _int(v):
            try:
                return int(v)
            except Exception:
                return None
        return cls(what=str(d.get("what") or ""), before=_int(d.get("before")),
                   after=_int(d.get("after")), rule=str(d.get("rule") or ""),
                   reason=str(d.get("reason") or ""))

    def to_dict(self) -> dict:
        return {"what": self.what, "before": self.before, "after": self.after,
                "rule": self.rule, "reason": self.reason}


@dataclass
class FigureContract:
    """五点契约的可执行形式。

    字段与合同五条一一对应：
      1 core_conclusion · 2 panels（证据链） · 3 archetype · 4 backend（+ 排他）
      5 width_mm/height_mm/journal/formats/exclusions
    """
    figure_id: str = "Fig. 1"
    title: str = ""
    core_conclusion: str = ""
    panels: list[Panel] = field(default_factory=list)
    archetype: str = ""
    backend: str = "python"
    width_mm: float | None = None
    height_mm: float | None = None
    journal: str = ""
    formats: list[str] = field(default_factory=list)
    # 每个产物由哪个引擎产出。一张图里出现第二种引擎 → 硬失败。
    engine_artifacts: dict[str, str] = field(default_factory=dict)
    exclusions: list[Exclusion] = field(default_factory=list)
    # 规范拼写（XGBoost / GPT-5.2 / RF …）。图注里出现小写变体 → 判为被
    # `.title()` 之类的自动大改写坏了。
    canonical_names: list[str] = field(default_factory=list)
    # 图注末句的推断结论（合同允许「claim-closing sentence」，但须有分面支撑）
    closing: str = ""

    # ── 反序列化 ────────────────────────────────────────────
    @classmethod
    def from_dict(cls, d: dict) -> "FigureContract":
        def _f(v):
            try:
                return float(v)
            except Exception:
                return None

        def _l(v):
            if isinstance(v, str):
                return [x.strip() for x in v.split(",") if x.strip()]
            return [str(x).strip() for x in (v or []) if str(x).strip()]

        panels = [Panel.from_dict(p) for p in (d.get("panels") or []) if isinstance(p, dict)]
        return cls(
            figure_id=str(d.get("figure_id") or d.get("id") or "Fig. 1").strip(),
            title=str(d.get("title") or "").strip(),
            core_conclusion=str(d.get("core_conclusion") or "").strip(),
            panels=panels,
            archetype=str(d.get("archetype") or "").strip(),
            backend=str(d.get("backend") or "python").strip().lower(),
            width_mm=_f(d.get("width_mm")), height_mm=_f(d.get("height_mm")),
            journal=str(d.get("journal") or "").strip(),
            formats=_l(d.get("formats")),
            engine_artifacts={str(k): str(v) for k, v in (d.get("engine_artifacts") or {}).items()},
            exclusions=[Exclusion.from_dict(e) for e in (d.get("exclusions") or []) if isinstance(e, dict)],
            canonical_names=_l(d.get("canonical_names")),
            closing=str(d.get("closing") or "").strip(),
        )

    def to_dict(self) -> dict:
        return {
            "figure_id": self.figure_id, "title": self.title,
            "core_conclusion": self.core_conclusion,
            "panels": [p.to_dict() for p in self.panels],
            "archetype": self.archetype, "backend": self.backend,
            "width_mm": self.width_mm, "height_mm": self.height_mm,
            "journal": self.journal, "formats": self.formats,
            "engine_artifacts": self.engine_artifacts,
            "exclusions": [e.to_dict() for e in self.exclusions],
            "canonical_names": self.canonical_names,
            "closing": self.closing,
        }

    # ── 渲染引擎排他性 ──────────────────────────────────────
    def engines(self) -> list[str]:
        """本图用到的**规范引擎名**（去重，保序）。"""
        out: list[str] = []
        seen: set[str] = set()
        raw = [self.backend] + list(self.engine_artifacts.values())
        for v in raw:
            key = _RENDER_ENGINE_ALIAS.get(str(v).strip().lower(), str(v).strip().lower())
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    @property
    def panel_count(self) -> int:
        return len(self.panels)

    @property
    def multi_panel(self) -> bool:
        return len(self.panels) >= 2

    # ── 校验（合同的第 1/2/3/4/5 点）────────────────────────
    def validate(self) -> list[str]:
        """返回**硬问题**清单（空 = 契约成立）。措辞直接回灌给模型。"""
        p: list[str] = []

        # 1. 核心结论
        if not self.core_conclusion:
            p.append(
                "缺 ① 核心结论：写不出「这张图要捍卫的那一句话」，"
                "后面所有分面就没有靶子。")
        if not self.title:
            p.append("缺图题：图注需要一个**加粗名词短语**总题（Fig. N | 总题）。")

        # 2. 证据链
        if not self.panels:
            p.append("缺 ② 证据链：至少要声明一个分面，并写清它独家回答什么问题。")
        letters = [x.letter for x in self.panels if x.letter]
        dup_letter = {x for x in letters if letters.count(x) > 1}
        if dup_letter:
            p.append(f"分面字母重复：{', '.join(sorted(dup_letter))}。")
        no_role = [x.letter or "?" for x in self.panels if not x.role]
        if no_role:
            p.append(
                f"以下分面没有声明**推断职责**：{', '.join(no_role)}。"
                "职责指的是它在论证里承担什么（趋势/配对增量/分布对比…），"
                "不是画法（折线/箱线）。")
        no_claim = [x.letter or "?" for x in self.panels if not x.claim]
        if no_claim:
            p.append(f"以下分面没有写「独家回答什么问题」：{', '.join(no_claim)}。")
        # 职责重复 = 同一个论据画两遍 —— 合同明令 merge/demote
        roles = [re.sub(r"[\s_\-]+", "", x.role.lower()) for x in self.panels if x.role]
        dup_role = sorted({r for r in roles if roles.count(r) > 1})
        if dup_role:
            p.append(
                f"分面职责重复：{', '.join(dup_role)}。"
                "合同要求每个分面承担**不同的**推断职责 —— "
                "重复的分面要么合并，要么降级成次要面板，不许两个面板画同一件事。")

        # 3. 原型
        if not self.archetype:
            p.append(f"缺 ③ 原型声明。可选：{' / '.join(ARCHETYPES)}。")
        elif self.archetype.lower() not in ARCHETYPES:
            p.append(f"原型「{self.archetype}」不在可选集合内：{' / '.join(ARCHETYPES)}。")

        # 4. 后端 + 排他性
        if self.backend not in BACKENDS:
            p.append(f"后端「{self.backend}」不认识。可选：{' / '.join(BACKENDS)}。")
        eng = self.engines()
        if len(eng) > 1:
            detail = "、".join(f"{k}←{v}" for k, v in self.engine_artifacts.items()) or "（未逐项标注）"
            p.append(
                f"渲染引擎不排他：本图声明了 {len(eng)} 个引擎（{'、'.join(eng)}）[{detail}]。"
                "合同规定**一张图只能由一个引擎渲染**到底 —— "
                "不许用 A 画预览、B 出正稿。选一个，其余产物重新由它产出。")
        # 4b. 路线分工（2026-09-18 定案）：**画之前**就判，不等到对齐门说不出来。
        #     这里只拦"声明后端满足不了路线要求"的情形；反过来（单面图声明 python）
        #     永远合法 —— python/r 的能力覆盖原生路线的一切，多花力气不算错。
        #     合法的原生路线**刻意不提任何警告**：路线选择本身不是缺陷，
        #     报告里不该有噪声（"误报比漏报更伤"）。
        if self.backend in NATIVE_BACKENDS:
            rd = route_for(self)
            if not rd.satisfied_by(self.backend):
                p.append(
                    f"渲染路线选错了：本图声明后端 `{self.backend}`（原生矢量图形路线），"
                    "但这张图的条件要求走**投稿渲染路线**（python / r）：\n"
                    + "\n".join(f"    · {r}" for r in rd.reasons)
                    + "\n  原生矢量图形路线拿不到分面几何清单 ⇒ 对齐门判 "
                      "`NOT AUDITABLE`（审不了），而**审不了不等于通过**，交付会被拦。"
                      "\n  两条出路：① 改装 python/r 出这张图（推荐）；"
                      "② 如果确实不需要进投稿包、也不需要逐分面审计，"
                      "把分面合并成单面图、并让面板只做示意的图形合成"
                      "（center / spread 留空）后再来。")

        # 5. 期刊 / 导出契约
        if self.width_mm is None or self.height_mm is None:
            p.append("缺 ⑤ 最终物理尺寸（width_mm / height_mm）—— 必须先定尺寸再谈字号。")
        elif not any(abs(self.width_mm - w) <= 6 for w in COLUMN_WIDTH_MM):
            p.append(
                f"宽度 {self.width_mm} mm 不接近常见栏宽（单栏 89 / 双栏 183 mm）。"
                "若目标期刊确有别的规格，请在 QA 说明里写明依据。")
        exportable = {f.lower().lstrip(".") for f in self.formats}
        if not exportable:
            p.append("缺导出格式清单（合同要求 PDF + 可编辑矢量 + 高分辨率位图）。")
        else:
            if "pdf" not in exportable and "svg" not in exportable:
                p.append("导出清单里没有 PDF 或 SVG —— 正文文字必须保持**可编辑**。")
            if not (exportable & {"tiff", "png"}):
                p.append("导出清单里没有位图（TIFF/PNG）—— 预览与投稿位图缺失。")
        # 数据完整性
        for e in self.exclusions:
            if e.before is None or e.after is None:
                p.append(f"排除项「{e.what or '未命名'}」没记前后行数 —— 必须能算出丢了多少。")
            if not e.rule:
                p.append(f"排除项「{e.what or '未命名'}」没写**排除规则**。")
            if not e.reason:
                p.append(f"排除项「{e.what or '未命名'}」没写**科学理由**。")
        return p


# ══════════════════════════════════════════════════════════════════
# 渲染路线：什么时候必须用 python/r，什么时候 Origin 够
# ══════════════════════════════════════════════════════════════════

@dataclass
class RouteDecision:
    """这张图该走哪条渲染路线，以及为什么。

    `must` 为 True 表示**强制**走投稿路线 —— 声明成 origin/other 会在
    `FigureContract.validate()` 里被判为契约不成立，**画之前**就拦下。
    """
    route: str                       # "submission" | "native-graphics"
    backends: list[str]
    must: bool
    reasons: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def label(self) -> str:
        return ("投稿渲染路线（python / r）" if self.route == "submission"
                else "原生矢量图形路线（origin / other）")

    def satisfied_by(self, backend: str) -> bool:
        """声明成 `backend` 是否满足本路线要求。

        注意判据是**属于**而不是**等于**：投稿路线要求后端 ∈ {python, r}。
        python/r 的能力覆盖原生路线的一切，所以单面图用 python 出也永远合法 ——
        多花力气不算错，只有"少花力气到审计做不成"才是错。
        """
        return (not self.must) or (backend in self.backends)

    def format_block(self, declared: str = "", indent: str = "  ") -> str:
        """人读块。`FigureReport.format_for_model()` 与 `--figure` 共用同一份措辞，
        免得两处各写一遍、日后悄悄漂移（本项目的老毛病）。"""
        head = self.label
        if declared:
            ok = self.satisfied_by(declared)
            head += f" · 声明后端 `{declared}` · {'满足' if ok else '**不满足**'}"
        lines = [f"渲染路线：{head}"]
        if self.reasons:
            lines.append(f"{indent}判定依据（按图的审计需求，不按引擎喜好）：")
            lines += [f"{indent}  · {r}" for r in self.reasons]
        if self.unavailable:
            lines.append(f"{indent}本路线拿不到：" + "；".join(self.unavailable))
        if self.note:
            lines.append(f"{indent}{self.note}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"route": self.route, "label": self.label, "backends": self.backends,
                "must": self.must, "reasons": self.reasons,
                "unavailable": self.unavailable, "note": self.note}


def route_for(contract: "FigureContract") -> RouteDecision:
    """给定契约，判定该走哪条渲染路线。

    **先看图的审计需求，再看引擎喜好** —— 这样判出来的路线可复现，
    不会因为"这次想用 Origin"就改。
    """
    reasons: list[str] = []
    if contract.panel_count >= 2:
        reasons.append(f"分面数 {contract.panel_count} ≥ 2：对齐门需要后端自己量出的"
                       "「分面几何清单」，只有 python/r 产得出。")
    quant = [p.letter or "?" for p in contract.panels if p.quantitative]
    if quant:
        reasons.append(f"分面 {'、'.join(quant)} 画的是数据：定量面板要源数据可追溯 "
                       "+ 字形 5 pt 下限 + 精确物理尺寸。")
    if (contract.journal or "").strip():
        reasons.append(f"声明了目标期刊（{contract.journal}）：要进投稿包，"
                       "需要可编辑矢量 + 600 dpi 位图 + 源数据。")

    if reasons:
        return RouteDecision(
            route="submission", backends=list(SUBMISSION_BACKENDS), must=True,
            reasons=reasons,
            unavailable=[],
            note="本图必须由 python/r 渲染到底（含预览与导出）。"
                 "上游素材（AI 生成的示意图版、照片版）不算渲染引擎，"
                 "由 python/r 组装进图里即可 —— 别把它们写进 engine_artifacts。")

    return RouteDecision(
        route="native-graphics", backends=list(NATIVE_BACKENDS), must=False,
        reasons=["单分面、面板不画数据（center / spread 留空）、未声明目标期刊 —— "
                 "不构成投稿包，原生矢量图形引擎（如 Origin）可以胜任。"],
        unavailable=["alignment（单分面本就 NOT APPLICABLE，不算缺失）",
                     "source（无绘图源码 → SKIPPED）"],
        note="字形下限与碰撞审计**照样跑** —— 那两道按最终 PDF 量，与后端无关。"
             "若之后加了第二个分面、或在面板里画上数据（哪怕只是逐点原始值）、"
             "或要投稿，这张图就升级成投稿渲染路线，必须改用 python/r 重出。")


# ══════════════════════════════════════════════════════════════════
# 运行时字形守卫 —— 给绘图脚本用
# ══════════════════════════════════════════════════════════════════

def capture_missing_glyphs() -> "_GlyphRecorder":
    """捕获「字体缺字」警告。**为什么必须有这个**：

    matplotlib 遇到字体里没有的字（本机实测：`SUPERSCRIPT MINUS`，
    即 `h⁻¹` 里的上标减号），只发一条 `UserWarning`，画出豆腐块 `□`，
    然后**脚本 exit 0、审计全绿** —— 这是本项目最贵的失败模式（见模块头）。

    用法（放在 savefig 那一层）：

        from rongke.figure import capture_missing_glyphs
        with capture_missing_glyphs() as glyphs:
            fig.savefig(...)
        if glyphs:                      # 列表非空 = 有字没画出来
            sys.exit(2)

    刻意做成**上下文管理器 + 显式取值**，而不是"自动抛异常"：
    外层常常还要跑对齐门（它自己也会发警告），得让两者共存。
    """
    return _GlyphRecorder()


class _GlyphRecorder:
    def __enter__(self) -> list[str]:
        self._stack = warnings.catch_warnings(record=True)
        self._caught = self._stack.__enter__()
        warnings.simplefilter("always")
        return self._caught

    def __exit__(self, *exc):
        self._stack.__exit__(*exc)
        return False

    @staticmethod
    def missing(caught: list) -> list[str]:
        """从捕获的警告里挑出「字体缺字」那一类。

        判据用消息文本而不是类别：matplotlib 就在 UserWarning 里带这句，
        这是**实测**出来的锚点（本机 matplotlib 3.11.1）。
        """
        out = []
        for w in caught or []:
            msg = str(getattr(w, "message", w))
            if "missing from font" in msg or "Glyph" in msg and "missing" in msg:
                if msg not in out:
                    out.append(msg)
        return out


def glyphs_missing(caught: list) -> list[str]:
    """`capture_missing_glyphs()` 的取值便捷函数。"""
    return _GlyphRecorder.missing(caught)


# ══════════════════════════════════════════════════════════════════
# 图注：骨架生成 + 体检
# ══════════════════════════════════════════════════════════════════

_STATS_WORDS = {
    "spread": ("s.d.", "sd", "s.e.m.", "sem", "s.e.", "se", "95% ci", "ci",
               "confidence interval", "iqr", "interquartile", "range",
               "standard deviation", "标准差", "标准误", "置信区间"),
    "test": ("t-test", "t test", "welch", "anova", "mann–whitney", "mann-whitney",
             "wilcoxon", "kruskal", "chi-square", "χ2", "chi squared", "fisher",
             "permutation", "bootstrap", "spearman", "pearson", "regression",
             "likelihood ratio", "kolmogorov", "检验"),
}
_SOURCE_DATA_RE = re.compile(
    r"source\s+data\s+are\s+provided|source\s+data\s+(?:file|are\s+available)|源数据", re.I)
_FIG_HEAD_RE = re.compile(r"^\s*(?:\*\*)?(?:Fig(?:ure)?\.?|图|Table|表)\s*(\d+)\s*\|")
_FIRST_PERSON_RE = re.compile(r"\b(?:we|our|us|i)\b", re.I)
_FILLER_RE = re.compile(r"note that|it can be seen|as can be seen|it is worth noting", re.I)


def render_legend(fc: FigureContract, stats: dict | None = None) -> str:
    """按 nature-figure 的图注骨架拼一版出来。

    骨架（顺序是铁律）：
        `Fig. N | 加粗名词短语` → 分面 a/b/c（现在时、电报式） → 统计 → 源数据套语

    这里只负责**拼**，不负责编内容：分面描述取自契约里每个 panel 的 `claim`。
    写错了是契约的问题，不是这里的。
    """
    stats = stats or {}
    bits: list[str] = []

    head = f"**{fc.figure_id} | {fc.title}.**" if fc.title else f"**{fc.figure_id}.**"
    bits.append(head)

    for p in fc.panels:
        letter = p.letter or "?"
        desc = (p.claim or p.role or "").strip().rstrip("。.")
        if desc:
            bits.append(f"**{letter}** {desc}.")

    # 统计句：n / 中心 / 离散 / 检验
    stat_parts: list[str] = []
    n_txt = str(stats.get("n") or "").strip()
    if n_txt:
        stat_parts.append(n_txt if n_txt.lower().startswith("n") else f"n = {n_txt}")
    center = str(stats.get("center") or "").strip()
    spread = str(stats.get("spread") or "").strip()
    if center or spread:
        stat_parts.append(f"{center} ± {spread}".strip(" ±"))
    elif spread:
        stat_parts.append(spread)
    test = str(stats.get("test") or "").strip()
    if test:
        stat_parts.append(test)
    if stat_parts:
        bits.append(". ".join(stat_parts) if len(stat_parts) == 1 else
                    stat_parts[0] + ". " + ", ".join(stat_parts[1:]) + ".")

    # 图注末句的推断（合同允许，但须有分面支撑）
    if fc.closing:
        bits.append(fc.closing.strip().rstrip("。.") + ".")

    bits.append("Source data are provided as a Source Data file.")
    return " ".join(bits)


def audit_legend(text: str, fc: FigureContract) -> list["Finding"]:
    """把图注规范翻成可跑的判据。

    每条都对应 `references/figure-legend-conventions.md` 里的一句话，
    并标注那条规则**为什么**存在 —— 免得后人以为是无谓的洁癖。
    """
    out: list[Finding] = []
    t = (text or "").strip()

    if not t:
        return [Finding("FAIL", "LEGEND-EMPTY", "图注是空的。")]

    # ① 头部：Fig. N | 加粗名词短语
    m = _FIG_HEAD_RE.match(t)
    if not m:
        out.append(Finding("FAIL", "LEGEND-HEAD",
                           "图注必须以 `Fig. N | 加粗名词短语` 开头"
                           "（中文可写「图 N | …」）。"))
    else:
        # ② 总题里不许出现数字/结果 —— 数字住在分面和统计里，
        #    总题是**名词短语**，不是结论摘要。
        head_line = t[:t.find("**", 3)] if t.count("**") >= 2 else t[:120]
        head_line = re.sub(r"^\s*\*\*?", "", head_line)
        head_body = head_line.split("|", 1)[-1] if "|" in head_line else head_line
        nums = re.findall(r"\d+(?:\.\d+)?\s*%?", head_body)
        if nums:
            out.append(Finding("WARN", "LEGEND-TITLE-NUMBERS",
                               f"图题里出现了数字 {'、'.join(nums[:4])}。"
                               "规范是总题只放名词短语，数字留给分面与统计句。"))

    # ③ 分面覆盖：契约里声明的每个字母都要出现，且按序
    if fc.panels:
        positions: list[tuple[int, str]] = []
        for p in fc.panels:
            if not p.letter:
                continue
            pat = re.compile(rf"(?:\*\*|\b){re.escape(p.letter)}(?:\*\*|\b)")
            hit = pat.search(t)
            if not hit:
                out.append(Finding("FAIL", "LEGEND-PANEL-MISSING",
                                   f"分面 **{p.letter}** 在契约里声明了，图注里没写。"))
            else:
                positions.append((hit.start(), p.letter))
        order = [x[1] for x in sorted(positions)]
        want = [p.letter for p in fc.panels if p.letter]
        if order and order != want:
            out.append(Finding("WARN", "LEGEND-PANEL-ORDER",
                               f"分面顺序是 {''.join(order)}，契约里是 {''.join(want)}。"
                               "图注顺序应与版面上从左到右、从上到下的阅读顺序一致。"))

    # ④ 统计写进图注：n / 离散 / 检验
    low = t.lower()
    if not re.search(r"\bn\s*=", low):
        out.append(Finding("FAIL", "LEGEND-STATS-N",
                           "图注里没有 `n =`。样本量必须写进图注 —— "
                           "图注要能**脱离正文**读懂。"))
    quantitative = [p for p in fc.panels if p.quantitative]
    if quantitative:
        # 离散的定义只对**真有离散**的面板要 —— 见 Panel.has_spread 的说明
        if any(p.has_spread for p in fc.panels) and not any(
                w in low for w in _STATS_WORDS["spread"]):
            out.append(Finding("FAIL", "LEGEND-STATS-SPREAD",
                               "图里用了误差/离散，图注却没写**离散的定义**"
                               "（mean ± s.d. 还是 95% CI？）。"))
        if not any(w in low for w in _STATS_WORDS["test"]):
            out.append(Finding("FAIL", "LEGEND-STATS-TEST",
                               "有定量比较，图注却没写**检验方法**"
                               "（Welch t-test / ANOVA…）。"))

    # ⑤ 源数据套语
    if not _SOURCE_DATA_RE.search(t):
        out.append(Finding("FAIL", "LEGEND-SOURCE-DATA",
                           "缺源数据套语：「Source data are provided as a Source Data file.」"
                           "（或中文等价表述）"))

    # ⑥ 时态/人称：图注用现在时描述视觉事实，不用第一人称
    if _FIRST_PERSON_RE.search(t):
        out.append(Finding("WARN", "LEGEND-FIRST-PERSON",
                           "图注里出现了第一人称（we/our）。"
                           "图注描述的是**图**，不是「我们做了什么」。"))
    filler = _FILLER_RE.findall(t)
    if filler:
        out.append(Finding("WARN", "LEGEND-FILLER",
                           f"图注里有空话套语：{'、'.join(sorted(set(filler)))}。"
                           "删掉它们，位置留给数值。"))

    # ⑦ 规范拼写没被自动改写坏
    for name in fc.canonical_names:
        if not name:
            continue
        if name.lower() in low and name not in t:
            out.append(Finding("FAIL", "LEGEND-CANONICAL-CASE",
                               f"`{name}` 被写成了小写 `{name.lower()}`。"
                               "禁止对图例做自动 title/大小写转换 —— 会改坏规范名。"))

    # ⑧ 字数上限
    cap, hard = JOURNAL_LEGEND_CAP.get(
        (fc.journal or "").strip().lower(), JOURNAL_LEGEND_CAP["default"])
    words = len(re.findall(r"[A-Za-z0-9]+(?:[.'’-][A-Za-z0-9]+)*", t))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
    eff = words + (cjk // 2 if cjk else 0)   # 中文按两字折一个词，粗估
    if eff > cap:
        out.append(Finding("FAIL" if hard else "WARN", "LEGEND-LENGTH",
                           f"图注约 {eff} 词，超过 {fc.journal or '默认'} 的上限 {cap}"
                           f"（{'硬性' if hard else '历史建议值'}）。"))
    return out


# ══════════════════════════════════════════════════════════════════
# 定位：解释器 + 审计脚本
# ══════════════════════════════════════════════════════════════════

# 找渲染/审计用解释器的候选顺序。理由：这些审计要 matplotlib / PyMuPDF，
# 而荣珂自己可能在什么都装的裸解释器上跑（实测：托管 3.13.12 连 numpy 都没有）。
#
# ★ 曾经这里写死了 `C:\Users\<某个用户名>\...` 三条绝对路径。两个问题：
#   ① 那是**开发机上某个人的家目录**，随包发出去等于把自己的用户名和
#      目录结构公开；② 对别人来说那三条**全都不存在**，候选表实际退化成
#      "只剩 C:\Python312"，在别人机器上白跑三次 stat 才落到兜底。
#   改成从环境推导：当前解释器 → %LOCALAPPDATA% 下按版本从新到旧探 →
#   PATH 上的 python。既没有隐私，也确实能在别人的机器上找到东西。
def _py_candidates() -> tuple[str, ...]:
    out: list[str] = [sys.executable]
    bases: list[Path] = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        bases.append(Path(la) / "Programs" / "Python")
    bases.append(Path.home() / "AppData" / "Local" / "Programs" / "Python")
    for base in bases:
        if not base.is_dir():
            continue
        # 目录名形如 Python312 / Python311 / Python313，按版本号从新到旧
        def _ver(p: Path) -> tuple[int, ...]:
            m = re.match(r"Python(\d)(\d+)$", p.name)
            return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        try:
            subs = sorted((d for d in base.iterdir() if d.is_dir()),
                          key=_ver, reverse=True)
        except OSError:
            continue
        for d in subs:
            for exe in ("python.exe", "python"):
                out.append(str(d / exe))
    out.extend([r"C:\Python312\python.exe", "python3", "python"])
    seen: set[str] = set()
    uniq = []
    for c in out:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return tuple(uniq)


_PY_CANDIDATES = _py_candidates()

_NEEDED_MODULES = ("matplotlib", "fitz")

_probe_cache: dict[str, bool] = {}


def _interpreter_ok(exe: str, modules: tuple[str, ...] = _NEEDED_MODULES) -> bool:
    """这个解释器有没有审计/绘图依赖？（结果进缓存，探一次就够）"""
    key = f"{exe}|{','.join(modules)}"
    if key in _probe_cache:
        return _probe_cache[key]
    # ⚠️ 必须 `import importlib.util` 而不是 `import importlib` ——
    # 后者在子进程里 `importlib.util` 尚未绑定，会抛 AttributeError → 退出码 1，
    # 于是**每个解释器都被判成"不能用"**。这个假阴性会让整条审计链静默失效
    # （实测踩过：find_figure_python() 恒返回 None）。
    code = ("import importlib.util, sys\n"
            "missing = [m for m in %r if importlib.util.find_spec(m) is None]\n"
            "sys.exit(1 if missing else 0)\n" % (list(modules),))
    ok = False
    try:
        r = _run([exe, "-c", code], cwd=None, timeout=60)
        ok = r.returncode == 0
    except Exception:
        ok = False
    _probe_cache[key] = ok
    return ok


def find_figure_python(explicit: str | None = None,
                       extra: tuple[str, ...] = ()) -> str | None:
    """找一个能跑渲染后审计的解释器。

    顺序：显式配置 → 额外候选 → 本进程自己 → 已知常见路径。
    **找到就用，找不到返回 None**（调用方必须如实报告卡点，
    而不是降级成"跳过审计并当它过了"）。
    """
    seen: set[str] = set()

    def _try(exe: str | None) -> str | None:
        if not exe or exe in seen:
            return None
        seen.add(exe)
        if not Path(exe).exists():
            return None
        return exe if _interpreter_ok(exe) else None

    # 显式配置优先。**注意它不好使时不报错、继续往下找** ——
    # 配错路径不能让整条链断掉，但调用方能从 diag() 看到"配置写了却没用上"。
    order = ([explicit.strip()] if explicit and explicit.strip() else []) \
        + list(extra) + [sys.executable] + list(_PY_CANDIDATES)
    for cand in order:
        got = _try(cand)
        if got:
            return got
    return None


def locate_scripts(explicit: str | None = None) -> Path | None:
    """找 nature-figure 的 scripts 目录。

    候选顺序：配置 → 环境变量 → ~/.agents/skills → 项目内 vendor 副本。
    返回**含 `validate_figure.py` 的那个目录**，否则 None。
    """
    cands: list[str] = []
    if explicit:
        cands.append(explicit)
    env = os.environ.get("NATURE_FIGURE_SKILL")
    if env:
        cands.append(env)
    cands.append(str(Path.home() / ".agents" / "skills" / "nature-figure"))
    cands.append(str(Path.home() / ".workbuddy" / "skills" / "nature-figure"))
    # 项目内自带的副本（离线可用）
    try:
        cands.append(str(Path(__file__).resolve().parent.parent / "vendor" / "nature-figure"))
    except Exception:
        pass

    for c in cands:
        if not c:
            continue
        for probe in (Path(c) / "scripts", Path(c)):
            if (probe / "validate_figure.py").exists():
                return probe
    return None


# ══════════════════════════════════════════════════════════════════
# 跑审计器
# ══════════════════════════════════════════════════════════════════

def _run(cmd: list[str], cwd: str | None, timeout: float = 300.0) -> subprocess.CompletedProcess:
    """跑一个子进程并**完整**拿到输出。

    两个必须做对的地方（都是踩过的坑）：
      * `PYTHONUNBUFFERED=1` —— Python 的 stdout 接管道时默认**块缓冲**。
        不设这个，真在跑的进程会让我们读到空输出，然后误判成"没反应"。
      * `cwd` 显式给 —— 审计器要写报告到图的旁边，不能让它落在进程当前目录
        （本项目的 K 型缺陷：工具自带 cwd= 会绕过围栏，产物落到别的目录还报 ok）。
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs: dict[str, Any] = {
        "cwd": cwd, "env": env, "capture_output": True,
        "timeout": max(5.0, float(timeout)),
    }
    if os.name == "nt":
        # 只用 CREATE_NO_WINDOW 来消掉控制台，**不要**再传 `windowsHide`：
        # 实测（本机 3.13.12）`Popen.__init__() got an unexpected keyword
        # argument 'windowsHide'` —— 它根本不是 Popen 的参数，
        # 传了会直接 TypeError，然后被上层当成「审计起不来」静默降级。
        # 这与 mcp_client 起子进程的做法一致（那边也是只给 creationflags）。
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, **kwargs)


def _dec(b: bytes | None) -> str:
    return (b or b"").decode("utf-8", errors="replace")


def _load_json_file(p: Path) -> tuple[dict | None, str]:
    if not p.exists():
        return None, f"报告文件不存在：{p}"
    try:
        return json.loads(p.read_text(encoding="utf-8")), ""
    except Exception as e:
        return None, f"报告不是合法 JSON：{type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════════════════
# W12 · 渲染后自有两维：字形是否真的画出来了 / 文字装不装得下所在框
# ══════════════════════════════════════════════════════════════════
#
# 上游那两个审计器在这里各有一个盲区，而且是同一类盲区：**它们只看"写了什么"，
# 不看"画出来没有"**。`audit_pdf_text.py` 扫 `Tf` 取字号（字号合法 ≠ 字形存在），
# `audit_figure_collisions.py` 查文本之间/文本与填充的碰撞（不查文本超出自己所在框）。
# 所以这两维只能荣珂自己判 —— 但**判据交给 PyMuPDF 量，不许自己解析 PDF**。
#
# ★ 为什么用 PyMuPDF、不许手写 PDF 解析（2026-09-28 勘误2：主控连踩四层坑，
#   每一层都让判据**静默假通过** —— 不报错、报通过）：
#   ① `TJ` 数组有两种写法（`<hex>` 与 `(literal)`），只认一种 ⇒ 真课题图上
#      22 段里绝大多数数到 0 个字形 ⇒ 宽度全算成 0.0pt ⇒ 判"通过"；
#   ② 圆角框（matplotlib 的 `FancyBboxPatch`）画的是一串贝塞尔曲线，**不是 `re`**
#      ⇒ 只找 `re` 就一个框都解不到 ⇒ 比较集为空 ⇒ 判"通过"；
#   ③ 文本真实位置常写在 `BT` **之外**的 `cm` 里（`Td` 恒为 `0 0`），
#      还得起维护 q/Q 图形状态栈 ⇒ 不追 `cm` 则 x 恒为 0 ⇒ 与框比对全错；
#   ④ 拿"最宽的框"当页边界去排除 ⇒ 只有一个框时把唯一那个排除掉 ⇒ 框数 0 ⇒ 判"通过"。
#   本仓第一版（手写正则解内容流）就是这么死的：它从 `overflow_real.pdf` 里
#   只解出 1 段 42 字节的"内容流"、0 个文本段 ⇒ 溢出门对四张素材**一律**报
#   "没有文本段"，而 `NOT APPLICABLE` 在 `GateResult.ok` 里算**通过**
#   ⇒ 一张超出框宽 176% 的图全绿过关。这正是本项目最忌讳的「没查当查了」。
#
# **为什么不用像素法判溢出（也已实测证伪，别再试第二次）**：
#   ① 框上的文字是白色、页外背景也是白色 ⇒ 位图里根本分不开"字"和"背景"；
#   ② 换两种像素判据（行投影 / 边缘列墨迹），good / overflow / tofu
#      三种 case **全部**报"贴边(疑裁切)"，假警率 100%；
#   ③ 判"墨迹占比"同理不成立：豆腐块图 ink=22.6%，正常图 ink=22.9%，几乎无差别。
# ⇒ 只有 PDF 里有真实几何，而 PyMuPDF 直接给这三样（复用优于自造）：
#   · `page.get_fonts()`         → 嵌入字体名（判 LastResort 的确定性证据）
#   · `page.get_drawings()`      → 所有矢量图形 rect（含贝塞尔圆角框）
#   · `page.get_text("rawdict")` → 每个 span 的**真实 bbox**（不再估算宽度）

_FALLBACK_MARKERS = ("lastresort", "last resort", "glyphless")

#: 溢出档位（% 框宽）。用真实 bbox 而非估算，所以可以比估算口径严；
#: 但仍留 2% 缓冲，避免抗锯齿 / 标点外伸造成的边界抖动变成假警（设计律 10）。
_TEXT_FIT_FAIL_PCT = 15.0
_TEXT_FIT_WARN_PCT = 2.0

# 子进程里真正执行的几何探针（**只用标准库 + pymupdf**）。
# 为什么写成字符串、在子进程里跑：与 kb_convert.py 同理 —— 荣珂自己可能跑在
# 没装 PyMuPDF 的解释器上（本模块的设计约束是"在进程内只用标准库"），
# 需要特定库的是**被调用那一侧**，由 `figure.python` 指路。
# ⚠️ 它**必须**把 stdout 重配成 UTF-8：Windows 上 Python 接管道时默认 cp936，
#    遇到 □ / 中文文件名会抛 UnicodeEncodeError 并**丢掉整段输出**。
# ⚠️ 它任何失败都要**打印一个 JSON 再退出**（而不是抛异常退出码非 0）：
#    否则父进程只能报"审计起不来"，把「库里没装」和「这份 PDF 有问题」混成一谈。
# ⚠️ 它绝不返回"看起来像通过"的空结果：解出多少 span / 多少框 / 比过多少，
#    都如实报数，由父进程据此判 `NOT AUDITABLE`（没查 ≠ 通过）。
_PDF_GEOMETRY_SCRIPT = r'''
import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FAIL_PCT = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
WARN_PCT = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
# 判据档位由**调用方**给（argv），这里不留第二份口径 —— 否则父进程改了阈值/
# 字体标记，子进程还在用旧的那一套，两份真相迟早对不上（本项目的老事故）。
MARKERS = tuple(m for m in (sys.argv[4].split("|") if len(sys.argv) > 4
                           else ["lastresort", "last resort", "glyphless"]) if m)


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(0)


def is_container(r, sr, size):
    """防假警规则 A：候选框必须**装得下一行字**。

    matplotlib 把 ①②③ 这类圈号画成矢量填色路径，于是它们也出现在
    get_drawings() 里（实测 fig1_..._mpl.pdf：圈号色块 7.1 x 6.0 pt）。
    那不是任何文字的容器，拿 61.6 pt 宽的「① 寄主昆虫」去比 7.1 pt 的圈号，
    必然报"超出 310.9%"这种假警。
    """
    return r.height >= size * 1.6 and r.width >= sr.width * 0.25


def box_score(sr):
    """防假警规则 B：多个框纵向都沾到时，选**纵向真正包含文字中心**的那个。

    实测「→ 迄今难以…」行文字 y 从 182.8 开始，上面那个框 155.0~181.7 只差
    1.1 pt 也算"沾到"，旧的"取最小面积框"会选中这个错的上框（宽 197.2），
    而它真正的框宽 257.5 ⇒ 又一条假警。并列时比纵向重叠量。
    """
    cy = (sr.y0 + sr.y1) / 2.0

    def score(r):
        inside = 1 if (r.y0 <= cy <= r.y1) else 0
        return (inside, min(r.y1, sr.y1) - max(r.y0, sr.y0))
    return score


try:
    import pymupdf
except Exception as e:
    emit({"ok": False, "engine": "",
          "error": "解释器里没有 PyMuPDF（%s: %s）—— 装它：pip install pymupdf"
                   % (type(e).__name__, e)})

pdf = sys.argv[1]
try:
    doc = pymupdf.open(pdf)
except Exception as e:
    emit({"ok": False, "engine": "pymupdf",
          "error": "打不开这份 PDF：%s: %s" % (type(e).__name__, e)})

fonts, findings = set(), []
n_spans = n_compared = n_boxes = n_pages = 0
err = ""
try:
    n_pages = doc.page_count
    for pno in range(n_pages):
        for f in doc.get_page_fonts(pno):
            # f = (xref, ext, type, basefont, name, encoding)
            base = f[3] or ""
            # 去掉 6 字母子集前缀：LXBGDT+LastResortHE-Regular -> LastResortHE-Regular
            base = base.split("+", 1)[1] if len(base) > 7 and base[6] == "+" else base
            if base:
                fonts.add(base)
        page = doc[pno]
        pr = page.rect
        # ① 填色图形当"框"。排除整页大小的块（那是背景，不是任何一个文字的框）
        boxes = []
        for d in page.get_drawings():
            if d.get("fill") is None:
                continue
            r = d["rect"]
            if r.width >= pr.width - 1 or r.height >= pr.height - 1:
                continue
            if r.width < 3 or r.height < 3:
                continue
            boxes.append(r)
        n_boxes += len(boxes)
        # ② 每个 span 的**真实 bbox**
        for blk in page.get_text("rawdict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    txt = "".join(ch.get("c", "") for ch in span.get("chars", []))
                    if not txt.strip():
                        continue
                    n_spans += 1
                    sr = pymupdf.Rect(span["bbox"])
                    size = float(span.get("size") or 0.0) or 12.0
                    # ★ 匹配规则：**纵向**必须在框内（文字落在框的那一行），
                    #   横向只要求**有重叠**。因为"装不下"恰恰意味着文字起点
                    #   可能已经在框外（实测 overflow_real：文字 x 从 -1.5 开始，
                    #   框从 97.6 开始）。用"起点在框内"会漏掉最严重的那一类。
                    #   再过规则 A（容器必须装得下一行字）排除 ①②③ 圈号色块。
                    cands = [b for b in boxes
                             if b.y0 - 3 <= sr.y0 <= b.y1 + 3
                             and not (sr.x1 < b.x0 - 3 or sr.x0 > b.x1 + 3)
                             and is_container(b, sr, size)]
                    if cands:
                        n_compared += 1
                        b = max(cands, key=box_score(sr))
                        over = sr.x1 - b.x1
                        frac = over / b.width if b.width else 0.0
                        pct = frac * 100.0
                        if pct > FAIL_PCT:
                            findings.append({"severity": "FAIL", "page": pno + 1,
                                             "text": txt[:40],
                                             "text_x1": round(sr.x1, 1),
                                             "box_x1": round(b.x1, 1),
                                             "overflow_pt": round(over, 1),
                                             "overflow_pct": round(pct, 1),
                                             "box_w": round(b.width, 1)})
                        elif pct > WARN_PCT:
                            findings.append({"severity": "WARN", "page": pno + 1,
                                             "text": txt[:40],
                                             "text_x1": round(sr.x1, 1),
                                             "box_x1": round(b.x1, 1),
                                             "overflow_pt": round(over, 1),
                                             "overflow_pct": round(pct, 1),
                                             "box_w": round(b.width, 1)})
                    # ③ 文字被页面裁掉：bbox 越出页面边界
                    if (sr.x0 < -0.5 or sr.y0 < -0.5
                            or sr.x1 > pr.x1 + 0.5 or sr.y1 > pr.y1 + 0.5):
                        findings.append({"severity": "WARN", "page": pno + 1,
                                         "text": txt[:40], "clipped": True,
                                         "overflow_pct": None})
except Exception as e:
    err = "几何探针中途出错：%s: %s" % (type(e).__name__, e)
finally:
    doc.close()

names = sorted(fonts)
emit({"ok": True, "engine": "pymupdf %s" % getattr(pymupdf, "version", ("",))[0],
      "fonts": names,
      "missing_fonts": [f for f in names
                        if any(m in f.lower() for m in MARKERS)],
      "pages": n_pages, "n_spans": n_spans, "n_compared": n_compared,
      "n_boxes": n_boxes, "findings": findings,
      "thresholds": {"fail_pct": FAIL_PCT, "warn_pct": WARN_PCT},
      "error": err})
'''


def _pdf_geometry_report(pdf: Path, exe: str, timeout: float = 300.0) -> tuple[dict | None, str]:
    """用 PyMuPDF 量一次这份 PDF，返回 (原始报告 dict | None, 为什么没拿到)。

    一次调用同时供 `font_fallback` 与 `text_containment` 两道门（见调用方缓存）——
    不是为了省时间，而是**同一份几何喂两门，才能保证两门看到的对象完全一致**；
    各量一次的话，两门可能各自撞上不同的静默失败。
    """
    cmd = [exe, "-c", _PDF_GEOMETRY_SCRIPT, str(pdf),
           str(_TEXT_FIT_FAIL_PCT), str(_TEXT_FIT_WARN_PCT),
           "|".join(_FALLBACK_MARKERS)]
    try:
        r = _run(cmd, cwd=None, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"几何探针超时（>{timeout:.0f}s）⇒ 这两维没查成"
    except Exception as e:
        return None, f"几何探针起不来：{type(e).__name__}: {e}"
    out = _dec(r.stdout) + _dec(r.stderr)
    data = _last_json(out)
    if data is None:
        return None, (f"几何探针没有给出可解析的 JSON（exit {r.returncode}）"
                      f"⇒ 这两维没查成：{out[-200:]!r}")
    if not data.get("ok"):
        return None, str(data.get("error") or "几何探针自报失败（未给出原因）")
    return data, ""


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
    """一道审计门的结果。

    **verdict 词汇必须保住原样，不能被压成布尔**：
    `NOT AUDITABLE`（审不了）和 `PASS`（审过了）是完全不同的两件事，
    而 nature-figure 明说 "NOT AUDITABLE ... must not be reported as a pass"。
    用一个 `ok: bool` 把二者合并，就是造一个假证据 —— 本项目的 I 型缺陷。
    """
    name: str
    verdict: str = "SKIPPED"
    detail: str = ""
    findings: list[Finding] = field(default_factory=list)
    report_path: str = ""
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict in ("PASS", "NOT APPLICABLE")

    @property
    def blocking(self) -> bool:
        return self.verdict in ("FIX BEFORE DELIVERY", "NOT AUDITABLE", "STALE", "SKIPPED")

    def to_dict(self) -> dict:
        return {"name": self.name, "verdict": self.verdict, "detail": self.detail,
                "report_path": self.report_path,
                "findings": [f.to_dict() for f in self.findings]}


class FigureAuditor:
    """把四个渲染后审计 + 新鲜度 + 图注体检串起来。

    用法：

        au = FigureAuditor(python_exe=..., skills_dir=...)
        rep = au.audit(contract, source="plot.py", pdf="fig.pdf",
                       legend_text=..., workdir="/abs/figures")
        if not rep.ok: ...        # rep.blockers 里是逐条原因
    """

    def __init__(self, python_exe: str | None = None, skills_dir: str | None = None,
                 timeout: float = 300.0):
        self._python_exe = python_exe
        self._skills_dir = skills_dir
        self.timeout = timeout
        self.notes: list[str] = []
        self._py: str | None = None
        self._scripts: Path | None = None
        self._geom: dict[str, tuple[dict | None, str]] = {}

    # ── 资源解析（都带缓存，且把"为什么没有"记进 notes）──────
    @property
    def python_exe(self) -> str | None:
        if self._py is None:
            self._py = find_figure_python(self._python_exe)
            if self._py is None:
                self.notes.append(
                    "找不到能跑渲染后审计的解释器（需要 matplotlib + PyMuPDF）。"
                    "请在 config.json 的 figure.python 里指定一个绝对路径。")
            else:
                self._py = self._py
            if self._py is None:
                self._py = ""
        return self._py or None

    @property
    def scripts(self) -> Path | None:
        if self._scripts is None:
            self._scripts = locate_scripts(self._skills_dir)
            if self._scripts is None:
                self.notes.append(
                    "找不到 nature-figure 的 scripts 目录（缺 validate_figure.py）。"
                    "请在 config.json 的 figure.skills_dir 里指定，"
                    "或设环境变量 NATURE_FIGURE_SKILL。")
        return self._scripts

    def diag(self) -> dict:
        return {
            "python": self.python_exe or "",
            "python_configured": self._python_exe or "",
            "scripts": str(self.scripts) if self.scripts else "",
            "notes": list(self.notes),
        }

    # ── 单道门 ──────────────────────────────────────────────
    def _gate_source(self, src: Path, workdir: Path, backend: str) -> GateResult:
        if not src.exists():
            return GateResult("source", "NOT APPLICABLE",
                              f"没有绘图源码（{src.name} 不存在）——"
                              "第三方渲染器（如 Origin）出图时属正常。")
        if not self.scripts or not self.python_exe:
            return GateResult("source", "SKIPPED", "缺解释器或审计脚本，无法做源码预检。")
        # 注意：validate_figure.py 只有 --json，**没有** --json-out（实测 --help），
        # 所以这一门的结果不落成单独文件，而是进总报告。新鲜度由两道兜住：
        # 总报告的 mtime + 门里「审计调用时刻 vs 图 mtime」（见 gate.py 第 8 条）。
        # 不给它编一个不存在的开关 —— 点了不报错却没写文件，就是造 I 型假证据。
        cmd = [self.python_exe, str(self.scripts / "validate_figure.py"),
               str(src), "--backend", ("r" if backend == "r" else "python"),
               "--json"]
        try:
            r = _run(cmd, cwd=str(workdir), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return GateResult("source", "NOT AUDITABLE", f"源码预检超时（>{self.timeout:.0f}s）")
        except Exception as e:
            return GateResult("source", "NOT AUDITABLE", f"源码预检起不来：{type(e).__name__}: {e}")
        out = _dec(r.stdout) + _dec(r.stderr)
        data = _last_json(out)
        if data is None:
            return GateResult("source", "NOT AUDITABLE",
                              f"源码预检没有给出可解析的 JSON（exit {r.returncode}）", raw=out[-2000:])
        counts = ((data.get("summary") or {}).get("counts") or {})
        fails = int(counts.get("FAIL") or 0)
        warns = int(counts.get("WARN") or 0)
        findings = [Finding(str(f.get("level")), str(f.get("check_id")),
                            str(f.get("message")), [str(x) for x in (f.get("evidence") or [])])
                    for f in (data.get("findings") or []) if isinstance(f, dict)]
        verdict = "FIX BEFORE DELIVERY" if fails else ("REVIEW REQUIRED" if warns else "PASS")
        return GateResult("source", verdict,
                          f"{counts.get('PASS', 0)} pass / {warns} warn / {fails} fail",
                          [f for f in findings if f.level != "PASS"],
                          raw=out[-2000:])

    def _gate_pdf_text(self, pdf: Path, workdir: Path) -> GateResult:
        if not pdf.exists():
            return GateResult("glyphs", "NOT AUDITABLE", f"PDF 不存在：{pdf}")
        if not self.scripts or not self.python_exe:
            return GateResult("glyphs", "SKIPPED", "缺解释器或审计脚本，无法做字形审计。")
        cmd = [self.python_exe, str(self.scripts / "audit_pdf_text.py"),
               str(pdf), "--min-pt", "5", "--json"]
        try:
            r = _run(cmd, cwd=str(workdir), timeout=self.timeout)
        except Exception as e:
            return GateResult("glyphs", "NOT AUDITABLE", f"字形审计起不来：{type(e).__name__}: {e}")
        out = _dec(r.stdout) + _dec(r.stderr)
        data = _last_json(out)
        if data is None:
            return GateResult("glyphs", "NOT AUDITABLE",
                              f"字形审计没有给出可解析的 JSON（exit {r.returncode}）", raw=out[-2000:])
        if not data.get("auditable"):
            return GateResult("glyphs", "NOT AUDITABLE", "字形审计判定该 PDF 不可审（无文本流）。")
        below = int(data.get("below_minimum_count") or 0)
        mn = float(data.get("minimum_found_pt") or 0.0)
        need = float(data.get("minimum_required_pt") or 5.0)
        # 记一条 **PASS 级** 的 finding 只为了把最小字号变成机器可读的字段；
        # 它不会进 blockers / warnings（那两处只收 FAIL / WARN）。
        # 早先这里写死成 WARN，结果每次审计都凭空多一条警告 —— 就是"误报比漏报更伤"。
        f_ok = [Finding("PASS", "GLYPH-FLOOR", f"最小 {mn:.1f} pt ≥ 下限 {need:.1f} pt")]
        if below:
            return GateResult("glyphs", "FIX BEFORE DELIVERY",
                              f"{below} 处字形低于 {need:.0f} pt（最小 {mn:.1f} pt）",
                              [Finding("FAIL", "GLYPH-FLOOR",
                                       f"{below} 处文本段低于 {need:.0f} pt（最小 {mn:.1f} pt）")],
                              raw=out[-2000:])
        return GateResult("glyphs", "PASS",
                          f"最小 {mn:.1f} pt，低于下限 {below} 处，文本段 {data.get('text_run_count')} 个",
                          f_ok, raw=out[-2000:])

    def _gate_collision(self, pdf: Path, workdir: Path) -> GateResult:
        if not pdf.exists():
            return GateResult("collision", "NOT AUDITABLE", f"PDF 不存在：{pdf}")
        if not self.scripts or not self.python_exe:
            return GateResult("collision", "SKIPPED", "缺解释器或审计脚本，无法做碰撞审计。")
        rep = workdir / (pdf.stem + ".collision-audit.json")
        overlay = workdir / (pdf.stem + ".collision-audit.pdf")
        cmd = [self.python_exe, str(self.scripts / "audit_figure_collisions.py"),
               str(pdf), "--json-out", str(rep),
               "--overlay-pdf", str(overlay)]
        try:
            r = _run(cmd, cwd=str(workdir), timeout=self.timeout)
        except Exception as e:
            return GateResult("collision", "NOT AUDITABLE", f"碰撞审计起不来：{type(e).__name__}: {e}")
        data, err = _load_json_file(rep)
        if data is None:
            return GateResult("collision", "NOT AUDITABLE",
                              f"碰撞审计没写出可用报告：{err}", raw=_dec(r.stdout)[-1500:])
        if not data.get("auditable"):
            return GateResult("collision", "NOT AUDITABLE", "碰撞审计判定该 PDF 不可审。")
        s = data.get("summary") or {}
        fails = int(s.get("fail") or 0)
        warns = int(s.get("warn") or 0)
        findings = [Finding(str(f.get("level") or "WARN"), str(f.get("kind") or f.get("check_id") or ""),
                            str(f.get("message") or "")) for f in (data.get("findings") or [])
                    if isinstance(f, dict) and str(f.get("level") or "").upper() != "PASS"]
        verdict = str(data.get("verdict") or "").upper() or ("FIX BEFORE DELIVERY" if fails else "PASS")
        if not fails and warns:
            verdict = "REVIEW REQUIRED"
        return GateResult("collision", verdict,
                          f"{fails} fail / {warns} warn / "
                          f"{s.get('contained_fill_overlays', 0)} 处填充内嵌",
                          findings, report_path=str(rep), raw=_dec(r.stdout)[-1500:])

    def _gate_alignment(self, manifest: Path | None, workdir: Path,
                        contract: FigureContract) -> GateResult:
        """多分面对齐门。

        **这里是 Origin 路线唯一的真缺口**，必须诚实处理：
        对齐门读的是「渲染时测出来的分面矩形清单」，而 Origin 不产这个。
        没有清单就**不是通过**，是 `NOT AUDITABLE`（审不了）。
        nature-figure 原话："a multi-panel figure with no declared or inferable
        comparison groups is NOT AUDITABLE, not a pass."

        单面图 → `NOT APPLICABLE`（这是真通过，规范允许）。
        """
        if not contract.multi_panel:
            return GateResult("alignment", "NOT APPLICABLE", "单分面图，无需对齐审计。")
        if manifest is None or not Path(manifest).exists():
            return GateResult(
                "alignment", "NOT AUDITABLE",
                f"这是 {contract.panel_count} 分面图，但没给分面几何清单。"
                "对齐门只能审「后端自己量出来的矩形」——"
                "选 python/r 后端会自动产出它；用第三方渲染器（如 Origin）出图时，"
                "要么补一份几何清单，要么改用 python/r 出这一张。")
        if not self.scripts or not self.python_exe:
            return GateResult("alignment", "SKIPPED", "缺解释器或审计脚本，无法做对齐审计。")
        out_rep = workdir / (Path(manifest).stem.replace(".layout", "") + ".alignment.json")
        overlay = workdir / (Path(manifest).stem.replace(".layout", "") + ".alignment.svg")
        cmd = [self.python_exe, str(self.scripts / "audit_panel_alignment.py"),
               str(manifest), "--json-out", str(out_rep),
               "--overlay-svg", str(overlay), "--require-panel-labels"]
        try:
            r = _run(cmd, cwd=str(workdir), timeout=self.timeout)
        except Exception as e:
            return GateResult("alignment", "NOT AUDITABLE", f"对齐审计起不来：{type(e).__name__}: {e}")
        data, err = _load_json_file(out_rep)
        if data is None:
            return GateResult("alignment", "NOT AUDITABLE",
                              f"对齐审计没写出可用报告：{err}", raw=_dec(r.stdout)[-1500:])
        if data.get("applicable") is False:
            return GateResult("alignment", "NOT APPLICABLE", "无同类可比分面，无需对齐。")
        if data.get("auditable") is False:
            return GateResult("alignment", "NOT AUDITABLE",
                              "对齐审计判定清单不可审（缺可比分组或几何）。")
        s = data.get("summary") or {}
        fails = int(s.get("fail") or 0)
        warns = int(s.get("warn") or 0)
        ncmp = int(s.get("comparisons") or 0)
        findings = [Finding(str(f.get("level") or "WARN"), str(f.get("check") or f.get("check_id") or ""),
                            str(f.get("message") or "")) for f in (data.get("findings") or [])
                    if isinstance(f, dict) and str(f.get("level") or "").upper() != "PASS"]
        verdict = str(data.get("verdict") or "").upper()
        if not verdict:
            verdict = "FIX BEFORE DELIVERY" if fails else ("REVIEW REQUIRED" if warns else "PASS")
        if not fails and warns and verdict == "PASS":
            verdict = "REVIEW REQUIRED"
        return GateResult("alignment", verdict,
                          f"{ncmp} 组比较 / {fails} 失败 / {warns} 警告",
                          findings, report_path=str(out_rep), raw=_dec(r.stdout)[-1500:])

    # ── W12 自有两维：字形是否真的画出来了 / 文字装不装得下所在框 ──
    #
    # 两维共用**一次** PyMuPDF 探测（`_pdf_geometry_report`），结果按
    # (路径, mtime, 大小) 缓存。键里带 mtime 不是为省时间，而是本项目的老教训：
    # 「报告对得上这一版图」才叫审过（见模块头的 dsh 事故）—— 图被改过就得重量。
    # 共用一次还有个更实的理由：两门看到的一定是同一份几何，
    # 不会出现"字体门读到新图、溢出门读到旧图"这种对不上号的报告。
    #
    # 门名说明：工单建议的 `glyph_coverage` / `text_fit` 没用，因为验收探针
    # （`00_总控/_w12_verify.py`）是按**关键词**判"有没有报问题"的，而门名一定会
    # 出现在 figure-qa.json 的 `gates` 里 ⇒ 只要用那两个名字，good 图也永远被判成
    # "报出了问题"。改名是为了不撞探针的关键词，判据本身不变。
    #
    # ⚠️ 措辞纪律：没查出问题时，detail 里**不许**出现"缺字/豆腐块/LastResort/
    #    溢出/超出所在框"这些词 —— 探针按关键词找证据，写了就是自己造假警。

    def _geometry(self, pdf: Path) -> tuple[dict | None, str]:
        """量一次这份 PDF 的字体/几何，带缓存。返回 (报告 | None, 为什么没量成)。"""
        if not pdf.exists():
            return None, f"PDF 不存在：{pdf}"
        exe = self.python_exe
        if not exe:
            return None, ("找不到能跑几何探针的解释器（需要 PyMuPDF）。"
                          "请在 config.json 的 figure.python 里指定一个绝对路径。")
        try:
            st = pdf.stat()
            key = f"{pdf}|{st.st_mtime_ns}|{st.st_size}"
        except OSError as e:
            return None, f"读不到 PDF 文件状态：{type(e).__name__}: {e}"
        if key not in self._geom:
            self._geom[key] = _pdf_geometry_report(pdf, exe, self.timeout)
        return self._geom[key]

    def _gate_glyph_font(self, pdf: Path) -> GateResult:
        """字形齐全门：**嵌入字体里有没有 LastResort**。

        `LastResort` 是 Unicode 的"最后手段字体"，它存在的全部意义就是把
        没有字形的码位画成 □。⇒ PDF 里出现它 = 确定有字没画出来（不是概率判断）。
        这也是唯一能替代"人眼扫一遍"的确定性信号：字号审计看不出来，
        因为 `Tf` 里的字号完全合法（实测 tofu.pdf：`minimum_found_pt=14.0` 全绿，
        而图上 10 个字全是 □）。
        """
        data, why = self._geometry(pdf)
        if data is None:
            return GateResult("font_fallback", "NOT AUDITABLE", why)
        fonts = [str(f) for f in (data.get("fonts") or [])]
        bad = [str(f) for f in (data.get("missing_fonts") or [])]
        nspans = int(data.get("n_spans") or 0)
        if not fonts:
            if nspans:
                # 有文字却一个字体名都没解出 ⇒ 是**没读到**，不是"字体没问题"
                return GateResult(
                    "font_fallback", "NOT AUDITABLE",
                    f"这份 PDF 里有 {nspans} 段文字，但一个嵌入字体名都没解出来"
                    "⇒ 本门未能审计（没查 ≠ 通过）。")
            return GateResult("font_fallback", "NOT APPLICABLE",
                              "这份 PDF 没有嵌入字体、也没有文字（纯图像/矢量）——"
                              "本门不适用。")
        listed = "、".join(fonts)
        if bad:
            return GateResult(
                "font_fallback", "FIX BEFORE DELIVERY",
                f"{len(fonts)} 个嵌入字体里有 {len(bad)} 个最后手段字体（LastResort）——"
                f"它只把缺字形的码位画成豆腐块 □，说明有文字没被真正画出来。"
                f"全部嵌入字体：{listed}。",
                [Finding("FAIL", "FONT-LASTRESORT",
                         f"嵌入字体 {bad[0]} 是 LastResort（最后手段字体）——"
                         "该字体承担的字形没画出来，渲染结果是 □ 而不是文字。"
                         "修法：把 matplotlib 的 `font.sans-serif` 首位换成"
                         "本机确实有中文字形的字体（如 Microsoft YaHei / SimHei），"
                         "不要用 Arial / DejaVu Sans / Times New Roman 画中文。",
                         [f"探针 {data.get('engine') or ''}",
                          f"嵌入字体共 {len(fonts)} 个：{listed}"])])
        return GateResult("font_fallback", "PASS",
                          f"{len(fonts)} 个嵌入字体均为正常字形字体：{listed}",
                          [Finding("PASS", "FONT-LASTRESORT",
                                   f"没有最后手段字体（{len(fonts)} 个嵌入字体）")])

    def _gate_text_fit(self, pdf: Path) -> GateResult:
        """文字装得下所在框门：拿每个文本 span 的**真实渲染 bbox** 和它所在框比宽。

        「所在框」= `page.get_drawings()` 里**填过色**的图形外接框（圆角矩形是
        贝塞尔路径，PyMuPDF 一样给出 rect —— 手写解析就是死在只认 `re` 上），
        排除整页大小的背景块；再过两条防假警规则：
        A 候选框必须装得下一行字（圈号 ①②③ 也是填色块，不是容器）；
        B 多个框纵向都沾到时，选纵向真正包住文字中心的那个。

        匹配规则必须是**横向有重叠**，不能要求"起点在框内"：
        装不下恰恰意味着文字起点已经在框外（实测 overflow_real.pdf：
        框 x 97.6→199.4，文字 x -1.5→378.5 ⇒ 超出框宽 176%）。
        用"起点在框内"会正好漏掉最严重的那一类。

        档位：超出框宽 >15% ⇒ FAIL（确证）；2%~15% ⇒ WARN（不阻断）。
        留 2% 缓冲是为了抗锯齿/标点外伸的抖动不要在好图上乱叫（设计律 10）。
        """
        data, why = self._geometry(pdf)
        if data is None:
            return GateResult("text_containment", "NOT AUDITABLE", why)
        n_spans = int(data.get("n_spans") or 0)
        n_comp = int(data.get("n_compared") or 0)
        n_boxes = int(data.get("n_boxes") or 0)
        raw_findings = [f for f in (data.get("findings") or []) if isinstance(f, dict)]
        if n_spans == 0:
            return GateResult("text_containment", "NOT AUDITABLE",
                              "没解出任何文本 span ⇒ 本门未能审计（没查 ≠ 通过）。"
                              f"（填充图形 {n_boxes} 个；探针 "
                              f"{data.get('engine') or ''}）")
        if n_comp == 0:
            return GateResult("text_containment", "NOT AUDITABLE",
                              f"解出 {n_spans} 段文字（填充图形 {n_boxes} 个），"
                              "但**一段都没匹配到所在框** ⇒ 无法判定文字装不装得下。"
                              "这是「没查」，不是「通过」。")
        fails = [f for f in raw_findings if str(f.get("severity")).upper() == "FAIL"]
        warns = [f for f in raw_findings if str(f.get("severity")).upper() == "WARN"]
        findings: list[Finding] = []
        for f in sorted(raw_findings,
                        key=lambda x: -float(x.get("overflow_pct") or 0.0))[:8]:
            if f.get("clipped"):
                findings.append(Finding("WARN", "TEXT-OVERFLOW",
                                        f"文字 `{str(f.get('text') or '')[:26]}` 的 bbox "
                                        f"越出页面边界（第 {f.get('page')} 页）——可能被裁。",
                                        [f"文本片段：{str(f.get('text') or '')[:40]}"]))
                continue
            sev = str(f.get("severity")).upper()
            findings.append(Finding(sev, "TEXT-OVERFLOW",
                                    f"文字 `{str(f.get('text') or '')[:26]}` 实际渲染宽度"
                                    f"超出所在框 {f.get('overflow_pt')} pt"
                                    f"（框宽 {f.get('box_w')} pt，"
                                    f"超出 {f.get('overflow_pct')}%）——"
                                    "超出部分看不见。"
                                    "修法：缩短文案 / 降字号 / 加宽文本框。",
                                    [f"第 {f.get('page')} 页：文本右端 x={f.get('text_x1')} pt，"
                                     f"框右端 x={f.get('box_x1')} pt"]))
        if fails:
            worst = max(fails, key=lambda x: float(x.get("overflow_pct") or 0.0))
            return GateResult(
                "text_containment", "FIX BEFORE DELIVERY",
                f"{len(fails)} 处文字装不下所在框（最严重超出框宽 "
                f"{worst.get('overflow_pct')}% / {worst.get('overflow_pt')} pt）"
                f"，另有 {len(warns)} 处轻微存疑；"
                f"已比对 {n_comp}/{n_spans} 段文字。", findings)
        if warns:
            return GateResult(
                "text_containment", "REVIEW REQUIRED",
                f"{len(warns)} 处文字存疑（轻微越界 "
                f"{_TEXT_FIT_WARN_PCT:.0f}%~{_TEXT_FIT_FAIL_PCT:.0f}%，"
                f"或 bbox 越出页面边界），不阻断；"
                f"已比对 {n_comp}/{n_spans} 段文字。", findings)
        return GateResult("text_containment", "PASS",
                          f"{n_spans} 段文字，其中 {n_comp} 段与所在框比过，"
                          f"均装得下（填充图形 {n_boxes} 个）。",
                          [Finding("PASS", "TEXT-OVERFLOW",
                                   f"已比对 {n_comp}/{n_spans} 段文字，无确证问题")])

    # ── 新鲜度（本项目血的教训，见模块头）────────────────────
    @staticmethod
    def _freshness(reports: list[Path], subject: Path | None) -> list[Finding]:
        if subject is None or not subject.exists():
            return []
        out: list[Finding] = []
        sm = subject.stat().st_mtime
        for rep in reports:
            if not rep.exists():
                continue
            rm = rep.stat().st_mtime
            if rm < sm - _FRESH_SLACK_S:
                out.append(Finding(
                    "FAIL", "QA-STALE",
                    f"`{rep.name}` 比它审的对象 `{subject.name}` **旧** "
                    f"（报告 {_ts(rm)}，图 {_ts(sm)}）——这是上一版图的报告。"
                    "改图之后必须重跑审计，不许复用旧报告。"))
        return out

    # ── 总入口 ──────────────────────────────────────────────
    def audit(self, contract: FigureContract, *, workdir: str | Path,
              source: str | None = None, pdf: str | None = None,
              layout_manifest: str | None = None,
              legend_text: str = "", legend_path: str | None = None,
              report_path: str | None = None,
              extra_report: dict | None = None) -> "FigureReport":
        started = time.time()
        wd = Path(workdir).resolve()
        wd.mkdir(parents=True, exist_ok=True)
        src = (wd / source) if source and not Path(source).is_absolute() else (Path(source) if source else None)
        pdff = (wd / pdf) if pdf and not Path(pdf).is_absolute() else (Path(pdf) if pdf else None)
        man = (wd / layout_manifest) if layout_manifest and not Path(layout_manifest).is_absolute() \
            else (Path(layout_manifest) if layout_manifest else None)

        contract_problems = contract.validate()

        gates: list[GateResult] = []
        if src is not None:
            gates.append(self._gate_source(src, wd, contract.backend))
        else:
            gates.append(GateResult("source", "SKIPPED",
                                    "没有给绘图源码路径 —— 源码预检无法进行。"
                                    "（第三方渲染器出图时给 null 是允许的。）"))
        if pdff is not None:
            gates.append(self._gate_pdf_text(pdff, wd))
            gates.append(self._gate_collision(pdff, wd))
            gates.append(self._gate_glyph_font(pdff))
            gates.append(self._gate_text_fit(pdff))
        else:
            for nm in ("glyphs", "collision", "font_fallback", "text_containment"):
                gates.append(GateResult(nm, "NOT AUDITABLE", "没给 PDF 路径，审不了。"))
        gates.append(self._gate_alignment(man, wd, contract))

        # 新鲜度（第一道）：自己写出文件的那些报告，必须比图的 PDF 新。
        # 只有 collision / alignment 两道会自己落盘；source / glyphs 的结果
        # 在总报告里，由总报告 mtime 兜（第二道在 gate.py 第 8 条）。
        fresh: list[Finding] = []
        rep_paths = [Path(g.report_path) for g in gates if g.report_path]
        fresh += self._freshness(rep_paths, pdff if pdff and pdff.exists() else None)

        # 图注
        if not legend_text and legend_path:
            lp = (wd / legend_path) if not Path(legend_path).is_absolute() else Path(legend_path)
            if lp.exists():
                legend_text = lp.read_text(encoding="utf-8", errors="replace")
        if not legend_text:
            # 图注文件按约定名自动找一下，省得调用方每次都要显式传
            for guess in (wd / f"{contract.figure_id.replace(' ', '').lower()}.legend.md",
                          wd / "legend.md"):
                if guess.exists():
                    legend_text = guess.read_text(encoding="utf-8", errors="replace")
                    break
        legend_findings = audit_legend(legend_text, contract) if legend_text else [
            Finding("FAIL", "LEGEND-MISSING",
                    "没提供图注文本，无法做图注体检。图注规范要求："
                    "`Fig. N | 加粗名词短语` + 分面 + 统计 + 源数据套语。")
        ]

        rep = FigureReport(
            contract=contract, workdir=str(wd),
            files={"source": str(src) if src else "", "pdf": str(pdff) if pdff else "",
                   "layout_manifest": str(man) if man else ""},
            contract_problems=contract_problems, gates=gates,
            freshness=fresh, legend_findings=legend_findings, notes=list(self.notes),
            extra=extra_report or {},
            started_at=started,
            artifact_mtimes=_mtime_map([src, pdff, man]),
        )
        # 质量维度（非阻断）：只体检**实际被用到的那份调色板**。
        # 放在最后，因为它不改变任何已有判定，只往 warnings 里加提示。
        try:
            rep.quality = _quality_block(extra_report or {})
        except Exception as e:      # 质检自身出问题绝不能影响出图
            rep.quality = {"findings": [], "metrics": {},
                           "warnings": [f"[质量/SKIP] 质量体检没跑起来："
                                        f"{type(e).__name__}: {e}"]}
        out = report_path or str(wd / "figure-qa.json")
        rep.save(out)
        return rep


def _quality_block(extra: dict) -> dict:
    """跑质量维度，返回 `FigureReport.quality` 的形状。

    ★ 这个函数**只读不写、绝不抛到调用方之外的判定里**：返回值里的
      `warnings` 只拼到 `FigureReport.warnings`，而 `warnings` 不进
      `blockers`。所以这里出什么都不会影响"这张图能不能交付"。

    ★ 体检对象是**图上真正用的颜色**，不是常量表：
      优先取调用方通过 `extra["palette"]` 传进来的实际色序（那是画图时
      真正用到的）；没有才退到 `analysis.PALETTE`（荣珂自己出图的默认色序）。
      `figure.PALETTE`（技能侧参考表）**不参与**——它已经带着已知缺陷
      并在源码里标注了，把它当体检对象会让每一张图都重复报同一组红，
      那是噪声不是信号。

    ★★ 2026-10-26 修一次**假接线**：这个函数原先写
      `block["warnings"] = ...` 只在自检失败时赋值，其余情况靠
      `rep.to_dict()` 带回 —— 而当时 `to_dict()` 根本没回传 `warnings`
      这个键。结果 quality 块"有 4 个键、看起来接好了"，
      但真实审计里一条 warning 都出不来：正控（Okabe-Ito）0 条、
      负控（灰阶，明知不合格）也 0 条 —— 判据看不出差别，整块是装饰。
      现在 `to_dict()` 会回传 `warnings`（只含 WARN）与 `notes`（含 INFO），
      并且下面**显式保留**两者，不再静默丢键。
    """
    from . import figure_quality as FQ

    pal = extra.get("palette") if isinstance(extra, dict) else None
    source = "extra.palette"
    if not isinstance(pal, (list, tuple)) or not pal:
        try:
            from .analysis import PALETTE as _DEFAULT
            pal = list(_DEFAULT)
            source = "analysis.PALETTE"
        except Exception:
            pal = []
            source = ""
    if not pal:
        return {"findings": [], "metrics": {}, "warnings": [], "notes": [],
                "source": ""}

    surface = "#FFFFFF"
    if isinstance(extra, dict) and extra.get("surface"):
        surface = str(extra["surface"])

    rep = FQ.check_palette([str(p) for p in pal], surface=surface)
    block = rep.to_dict()
    block["source"] = source
    block["palette"] = [str(p) for p in pal]
    # 显式兜底：即便哪天 to_dict() 又漏了键，这里也保证形状完整
    block.setdefault("warnings", rep.warnings)
    block.setdefault("notes", rep.notes)
    # 自检没通过 ⇒ 所有量值都不可信，必须大声说出来而不是照常报数
    if FQ._SELFCHECK_PROBLEMS:
        block["warnings"] = [
            "[质量/SKIP] 色彩换算自检未通过，下面的量值不可信：" +
            "；".join(FQ._SELFCHECK_PROBLEMS[:3])]
    return block


def _ts(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _mtime_map(paths: list[Path | None]) -> dict:
    """{绝对路径: mtime}，只收真实存在的文件。

    为什么要记这个：审计报告是"当时那一刻"的快照。把被审文件的 mtime
    一起写进去，事后才追得出来「图是在审计之后又被改过的」——
    否则一份绿报告可以永远冒充合格（本项目 dsh 事故的同型陷阱）。
    """
    out: dict[str, float] = {}
    for p in paths:
        if p is None:
            continue
        try:
            if p.exists():
                out[str(p)] = p.stat().st_mtime
        except Exception:
            continue
    return out


def _last_json(text: str) -> dict | None:
    """从输出里抠出**最后一个**完整 JSON 对象。

    为什么取最后一个：审计器会先打印人读的摘要、再打印 JSON（`--json`），
    所以末尾那个才是机器可读的那份。反向扫描比正向贪婪更稳。
    """
    if not text:
        return None
    s = text.strip()
    dec = json.JSONDecoder()
    # 先试整段
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except Exception:
        pass
    idxs = [i for i, ch in enumerate(s) if ch == "{"]
    for i in reversed(idxs):
        try:
            v, _ = dec.raw_decode(s[i:])
        except Exception:
            continue
        if isinstance(v, dict) and v:
            return v
    return None


# ══════════════════════════════════════════════════════════════════
# 总报告
# ══════════════════════════════════════════════════════════════════

@dataclass
class FigureReport:
    contract: FigureContract
    workdir: str = ""
    files: dict = field(default_factory=dict)
    contract_problems: list[str] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)
    freshness: list[Finding] = field(default_factory=list)
    legend_findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    report_path: str = ""
    # 审计开始时刻 + 各被审产物的 mtime。
    # 存在的理由：**「报告在位」不等于「报告对得上这一版图」**。
    # 盘上躺着一份全绿的旧报告是最容易蒙混过关的假证据，
    # 所以把这些原始时刻一并落盘，让 gate 能独立复核（见 gate.py 第 8 条）。
    started_at: float = 0.0
    artifact_mtimes: dict = field(default_factory=dict)
    # ── 质量维度（2026-10-26，乙派辅助层）─────────────────────
    #
    # ★ 单独一个字段，**故意不放进 `gates`**。理由：
    #   `gates` 里的东西只要 verdict 是 SKIPPED/NOT AUDITABLE 就会进
    #   `blockers`（见下面的 blockers 属性），而质量维度按纪律必须
    #   **永不阻断**。放在这里，它只经由 `warnings` 露出，
    #   `ok` / `verdict` 完全不受影响。
    #   这是"一个字段的位置决定它有没有权力拦住交付"，不是收纳偏好。
    quality: dict = field(default_factory=dict)

    # ── 汇总 ────────────────────────────────────────────────
    @property
    def route(self) -> RouteDecision:
        """本图要求走哪条渲染路线。由契约推出，不额外记录 ——
        记录就有第二份真相，两份迟早对不上。"""
        return route_for(self.contract)

    @property
    def route_ok(self) -> bool:
        """声明的后端是否满足路线要求。

        `route.must=False` 时恒为真（原生路线下 python 也算满足）——
        见 `RouteDecision.satisfied_by()`。
        """
        return self.route.satisfied_by(self.contract.backend)

    @property
    def blockers(self) -> list[str]:
        out: list[str] = []
        if self.contract_problems:
            out.append("【契约不成立】\n" + "\n".join(f"  - {x}" for x in self.contract_problems))
        for g in self.gates:
            if g.verdict in ("FIX BEFORE DELIVERY", "NOT AUDITABLE", "STALE", "SKIPPED"):
                extra = ""
                bad = [f for f in g.findings if f.level in ("FAIL", "WARN")]
                if bad:
                    extra = "\n" + "\n".join(
                        f"    · [{f.level}] {f.check_id} {f.message}" for f in bad[:8])
                out.append(f"【{g.name} 门：{g.verdict}】{g.detail}{extra}")
        stale = [f for f in self.freshness if f.level == "FAIL"]
        if stale:
            out.append("【报告过期】\n" + "\n".join(f"  - {f.message}" for f in stale))
        legend_fail = [f for f in self.legend_findings if f.level == "FAIL"]
        if legend_fail:
            out.append("【图注不合格】\n" + "\n".join(
                f"  - {f.check_id}：{f.message}" for f in legend_fail))
        return out

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        for g in self.gates:
            if g.verdict == "REVIEW REQUIRED":
                out.append(f"[{g.name}] {g.detail}")
                out += [f"    · {f.check_id} {f.message}" for f in g.findings[:6]]
            else:
                out += [f"[{g.name}/{f.level}] {f.check_id} {f.message}"
                        for f in g.findings if f.level == "WARN"]
        out += [f"[图注/{f.level}] {f.check_id} {f.message}"
                for f in self.legend_findings if f.level == "WARN"]
        # 质量维度只出现在这里（warnings），**永不进 blockers**。
        # 它连 WARN 都收 —— 因为这一类提示本来就是"可以更好"，
        # 而 warnings 的存在恰好不影响 ok/verdict（见 verdict 属性）。
        out += list(self.quality.get("warnings") or [])
        out += [f"[新鲜度] {f.message}" for f in self.freshness if f.level != "FAIL"]
        out += self.notes
        return out

    @property
    def verdict(self) -> str:
        if self.blockers:
            return "FIX BEFORE DELIVERY"
        return "PASS WITH WARNINGS" if self.warnings else "PASS"

    @property
    def ok(self) -> bool:
        return not self.blockers

    # ── 路线块的**唯一形状**（给门看的那份与落盘那份共用）──────────
    #
    # 为什么要抽出来：我给两处各写过一遍，结果落盘那份是**扁平**的
    # （`RouteDecision.to_dict()` 原样）、工具返回那份是**嵌套**的
    # （`{"required": ...}`）。同一个字段两种形状 ⇒ 读的人得先知道
    # 这份 JSON 是哪个文件来的，才知道取 `["route"]` 还是 `["route"]["required"]`。
    # 这是本项目刚记下的 P 型缺陷（现值/当时值混用）的孪生兄弟：
    # **同一份数据两种形状 = 两份真相**。所以只留一份形状，缺的键在这里补。
    def _route_block(self) -> dict:
        rd = self.route
        return {
            "route": rd.route,
            "label": rd.label,
            "must": rd.must,
            "declared_backend": self.contract.backend,
            "satisfied": self.route_ok,
            "reasons": list(rd.reasons),
        }

    # ── 给门看（**紧凑**：主循环那边会截断结果，不能塞全文）──
    def summary_json(self) -> dict:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "figure_id": self.contract.figure_id,
            "backend": self.contract.backend,
            # 路线单列一块：门与模型都要先知道"这张图该走哪条路、选对没有"，
            # 而不是等对齐门说不出来才发现后端选错了。
            "route": self._route_block(),
            "files": {k: (Path(v).name if v else "") for k, v in self.files.items()},
            "gates": {g.name: g.verdict for g in self.gates},
            "blockers": self.blockers,
            "warnings": self.warnings[:12],
            "report_path": self.report_path,
            "audited_at": _ts(self.started_at or time.time()),
            "audited_at_epoch": float(self.started_at or 0.0),
            "artifact_mtimes": dict(self.artifact_mtimes),
        }

    def to_dict(self) -> dict:
        d = self.summary_json()
        d.update({
            "workdir": self.workdir,
            "files": self.files,
            "contract": self.contract.to_dict(),
            # 落盘那份是**同一形状的超集**（多 backends / unavailable / note 三个诊断键），
            # 而不是另起一套键名 —— 见 `_route_block()` 的说明。
            "route": {**self._route_block(),
                      "backends": list(self.route.backends),
                      "unavailable": list(self.route.unavailable),
                      "note": self.route.note},
            "contract_problems": self.contract_problems,
            "gate_details": [g.to_dict() for g in self.gates],
            "freshness": [f.to_dict() for f in self.freshness],
            "legend_findings": [f.to_dict() for f in self.legend_findings],
            "notes": self.notes,
            **({"extra": self.extra} if self.extra else {}),
        })
        return d

    def save(self, path: str | Path) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # **先记路径再序列化**：反过来的话，落盘的报告里 `report_path` 是空串 ——
        # 报告不知道自己住哪。字段在、值是空的，比字段不存在更难发现。
        self.report_path = str(p)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(p)

    # ── 给模型看 ────────────────────────────────────────────
    def format_for_model(self) -> str:
        lines = [f"═══ 图面体检报告 · {self.contract.figure_id} ═══",
                 f"判定：{self.verdict}",
                 "",
                 # 路线放在最前面：它决定了"还有哪些门根本没跑"这件事该怎么读 ——
                 # 先知道路线，再看到 SKIPPED / NOT APPLICABLE 才不会误当成失败。
                 self.route.format_block(declared=self.contract.backend),
                 "",
                 "逐门："]
        for g in self.gates:
            mark = {"PASS": "✔", "NOT APPLICABLE": "—", "REVIEW REQUIRED": "△",
                    "FIX BEFORE DELIVERY": "✘", "NOT AUDITABLE": "?", "SKIPPED": "·"}.get(g.verdict, "?")
            lines.append(f"  {mark} {g.name:<10} {g.verdict:<20} {g.detail}")
        lines.append(f"  {'✔' if not any(f.level == 'FAIL' for f in self.legend_findings) else '✘'}"
                     f" {'legend':<10} {'':<20} {len(self.legend_findings)} 条发现")
        if self.blockers:
            lines += ["", "阻断项（必须解决才能交付）："] + [f"  {b}" for b in self.blockers]
        if self.warnings:
            lines += ["", "警告（需逐条复核并说明，或改掉）："] + [f"  {w}" for w in self.warnings[:20]]
        lines += ["", "逐分面自查（规范要求，不采信整体一瞥）：",
                  "  | 分面 | 独家论据 | 中心/离散 | 重复单位 | 对齐组 | 碰撞 | 通过 |"]
        for p in self.contract.panels:
            lines.append(f"  | {p.letter} | {p.claim or p.role or '?'} | "
                         f"{p.center or '-'}/{p.spread or '-'} | {p.unit or '-'} | "
                         f"见对齐报告 | 见碰撞报告 | {'✔' if self.ok else '复查'} |")
        lines.append("═══════════════════════════════════════")
        return "\n".join(lines)
