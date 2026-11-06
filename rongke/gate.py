# -*- coding: utf-8 -*-
"""完成证据门 —— "说完成"必须交出可追溯的证据。

这是荣珂与「对话循环」最本质的区别：模型说"我做完了"不算数。

判定全部**程序化**，不采信模型的自我陈述：
  1. 证据列表非空（契约明令禁用工具的任务**不适用**——它逻辑上不可能有调用证据）
  2. 每条证据引用的 call_seq 必须真实存在于工具调用记录里（防编造；
     禁用工具的任务如果**编了**证据，这条照拦 —— 不适用 ≠ 不设防）
  3. 被引用的调用必须成功（ok=True）
  4. 契约里每条验收标准都要被至少一条证据覆盖（禁用工具时降为警告：
     内核能实测的标准仍终审；实测不了的标准无从「被证据覆盖」）
  5. deliverable 每一项都要被证据提及（软检查，只警告）
  6. **阶段专属技能**必须装载：本任务包含的每个阶段，其唯一专属技能都要装载过
     （禁用工具的任务不适用 —— load_skill 本身就是一次工具调用）
  7. **写作体检**：写作任务必须附正文，正文逐项核稿面
     （体量/结构/句长/套话/标点/引注）——**前 6 条只核流程，这条才看文本本身**
  8. **图面门**：要产出图就必须有一份**新鲜**的图面体检报告
  9. **组会 PPT 门**：要产出演示文稿就必须有一份**新鲜**的 deck 体检报告
  10. **大纲门**：长综述必须有落盘的 `outline.md`，且每章要素齐全
      （一句话论证 / doc_id 清单 / 字数 / 图表），章-文献映射不得为空

第 6 条与契约里的「写作任务强制协议」构成两道：事前把协议塞到模型眼前，
事后没做就打回。工作台原本只有 process_audit 的事后审计，靠 agent 自觉。

第 7 条同理，但**刻意限制打回次数为 1**：体检是启发式的，一定会有误报，
无限打回会把一篇稿子烧成十几轮。第一遍逼它真改，第二遍认账放行。

不通过时产出的 feedback 会原样回灌给模型——它必须知道"缺什么"才能补。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .contract import TaskContract, contract_wants_notes
from .stages import STAGE_BY_ID, available
from .tools import ToolCall, ToolRegistry
from .writing import Report, lint


def _norm(s: str) -> str:
    """规范化：去空白/标点/全角，便于宽松比对。"""
    s = (s or "").lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[，。、；：！？,.;:!?\"'`（）()\[\]【】]", "", s)
    return s


def _covers(needle: str, hay: str) -> bool:
    """needle 是否被 hay 覆盖（双向子串，规范化后）。"""
    a, b = _norm(needle), _norm(hay)
    if not a or not b:
        return False
    return a in b or b in a


def _echo_of(acc: str, text: str) -> bool:
    """这条「证据」是不是把**验收标准原文抄了一遍**。

    2026-11-04 A 场实测：交付被打回后，模型把被拦的那条标准一字不改填进
    `evidence[].acceptance`，`_covers()` 精确命中 ⇒ 门 92→96 放行。
    判据要抓的是「复述」，不是「提到」：所以要求规范化后相等，或者是原文
    的长子串（≥85% 长度）。普通证据里恰好包含标准里的几个词不会被误伤。
    """
    a, b = _norm(acc), _norm(text)
    return bool(a and b) and (b == a or (b in a and len(b) >= 0.85 * len(a)))


def _evidence_is_echo(acc: str, ev, tc) -> bool:
    """整条证据是不是**只有**「把验收标准复述一遍」，没有别的实质内容。

    2026-11-04 D 场真跑（paper-writing）撞出来的死门：`EvidenceRef.acceptance`
    是**绑定声明** —— 协议本来就要求模型在这里写它对应契约里的哪条标准，
    于是它天然等于标准原文。上一版 Q1 拿这个字段判「抄原文」，再把它与
    `shows` / 工具回包的匹配一起短路掉，结果 7 条语义验收全判「无证据支撑」，
    3 次交付恒定拒绝、跑满 652 s 也交不出稿。

    ★ 要判「复述」的是**证据内容**（`shows` 与 `tc.result`），不是绑定字段。
      两个内容字段里只要有一个不是标准原文的复述，就算有实质证据。
    """
    parts = (str(getattr(tc, "result", "") or "")[:2000],
             str(getattr(ev, "shows", "") or ""))
    return not any(_norm(t) and not _echo_of(acc, t) for t in parts)


# ★ 预设点名后才启用的**额外内核判据**（Q2）。baseline 的 measured_metrics
#   只有 6 项，全部落在 `_kernel_verdict` 原有四段里 ⇒ 这张表一条都不进，
#   旧路径逐字等价。规则一律保守：只认「命中数 == 0」或「必须有」这类
#   能由内核直接量出来的事实，绝不靠措辞猜。
_EXTRA_RULES = (
    ("cliche_hits", re.compile(r"套话|空话|填充句|无信息量"), "eq0", "套话命中"),
    ("absolutes", re.compile(r"绝对化|一刀切|断言式"), "eq0", "绝对化表述"),
    ("placeholders", re.compile(r"占位符|模板残留|TODO"), "eq0", "占位符"),
    ("tail_repeat", re.compile(r"结尾复述|末尾重复|收尾重复"), "eq0", "结尾重复"),
    ("defensive_hits", re.compile(r"防御性写作|免责式"), "eq0", "防御性写作"),
    ("cite_numbers", re.compile(r"引注编号|数字制引注|引用标记"), "gt0", "数字引注"),
    ("refs_n", re.compile(r"参考文献表条目|文献表条数"), "gt0", "文献表条目"),
)
_EXTRA_ORDER = ("cite_first_order", re.compile(r"首现顺序|编号顺序"),
                "ordered", "引注首现顺序")


# 验收标准里出现这些词 → 该条**由内核自己测量判定**，不去比对模型措辞。
# 动因（2026-09-18 实测）：一次 119 字、偏差 -1%、稿面体检 100/100 的正确交付，
# 因为模型证据的措辞里没出现「字数约120字」这几个字，被判「验收标准无证据支撑」
# 并打回，逼它多跑一轮去"证明自己确实写了 120 字"。
# 内核明明已经数出来了 —— 这是 README 记过的「字面匹配当硬门必误伤」
# 从新的一扇门又进来一次。**能算的就不许靠匹配。**
_ACC_CHARS = re.compile(r"字数|字符数|字左右|字以内|字以上|words?\b|word\s*count", re.I)
_ACC_PARA = re.compile(r"段落|分段|成段")
_ACC_SENT = re.compile(r"句数|句子数|多少句")

# ── 引注类验收标准（2026-09-19 新增）────────────────────────
# 动因：一次真实运行里，验收标准「全文不含任何文献引用（无引用标记、无参考文献列表）」
# 被证据门判成「没有证据支撑」并打回 —— 而**草稿里一个引用标记都没有**，
# 体检报告上白纸黑字写着 `citations: 0`。内核明明已经数出来了，却仍然去比对措辞，
# 于是模型为了"证明自己没引用"又跑了 4 步、多烧 57,876 输入 token
# （68,974 → 126,850），最后靠**把这条标准的原文抄进 evidence.acceptance** 才放行。
# 这是「字面匹配当硬门必误伤」第三次从新门进来，见 _ACC_CHARS 上方同一段教训。
#
# 判据只认**「有没有引用」**，不认格式/条数/页码 —— 后者内核不管，交回措辞匹配。
_ACC_CITE_NOUN = r"(?:参考文献|文献列表|文献|引用|引注|引文|引证|citations?)"
# 必须**全称否定**才算：出现「任何 / 无任何 / 全文 / 通篇 / 全篇 / 一律」这类
# 覆盖全篇的量词，才说明它要求的是"整篇一个引用都没有"。
# 反例（必须判为 unknown，见 writing_gate_test 的负控）：
#   「不得引用未经核实的文献」—— 它管的是引用的*质量*，不是"不许引用"；
#   若被判成否定式，一篇带规范引用的好稿会被硬门打回（误报比漏报更伤）。
_ACC_CITE_CATEGORICAL = re.compile(r"任何|全文|全篇|通篇|文中|稿中|正文|一律|完全|绝对")
_ACC_CITE_NEG = re.compile(
    r"(?:不含|不出现|不带有|不使用|不得出现|不得有|不要|不得|没有|未含|未出现|无|零)\s*"
    r"(?:任何|出现|包含|使用|添加|引用)?\s*" + _ACC_CITE_NOUN
)
# 标准在谈「格式/著录/顺序/编号/页码/条数」时，谈的就不是「有没有」。
_ACC_CITE_META = re.compile(r"格式|著录|规范|统一|顺序|编号|页码|条数|数量|篇数|标点")
# 标准在谈「引用得对不对」时，谈的也不是「有没有」。
# 反例（必须判 unknown）：「全文不得引用未经核实的文献」—— 它同时命中了
# 全称量词「全文」和否定式「不得引用」，但要求的是引用的**质量**，
# 一篇带规范引用的好稿会被硬门误打回。这是 _ACC_CITE_CATEGORICAL 单独挡不住的一类。
# ★ 2026-11-01 补：下面第二行那一类是「要求每句话都带引注」，不是「不许有引注」。
#   实测事故（run 59f35cb2b623）：验收标准「文中不出现无引注支撑的具体数值或结论性断言」
#   命中 _ACC_CITE_NEG（不出现）+ _ACC_CITE_CATEGORICAL（文中），被判成「必须零引用」。
#   而模型交的稿子 288 字、7 句、8–10 处引注、写作体检 **100/100** —— 好稿被硬门打回，
#   它连改两版都过不去，14 步跑完仍然 0 交付。
#   这与 2026-10-15 那次「不含参考文献表」是同一类错误：否定词修饰的是**质量**，
#   不是**存在**。判据要能分开这两件事。
#
#   ⚠️ 第一版我写成 `无引注|无引用|无出处|…` 一口气全加，结果把
#      「全文不含任何文献引用（无引用标记、无参考文献列表）」
#      也判成"要求带引注" —— 那条是真的要零引用，writing_gate_test 当场变红。
#      区别在**否定词后面跟的是什么**：
#        「无引注**支撑**」「无**文献**支撑」= 要求带引注（质量问题）
#        「无引注**标记**」「无**任何**引用」   = 要求零引用（存在问题）
#      所以只认「无…支撑 / 缺…」这几种，不认裸的「无引用」「无引注」。
_ACC_CITE_QUALITY = re.compile(
    r"未经核实|未核实|不实|虚假|错误|失效|撤稿|不当|未标注|应标注|需标注"
    r"|无\s*(?:引注|引用|引文|文献|证据)?\s*支撑"
    r"|缺\s*(?:引注|引用|出处|文献)")
# 「不含参考文献表」这类**篇幅限定语**：否定语紧跟的是 参考文献/文献表，
# 而标准本身在谈字数/篇幅 —— 此时 ④ 不判（详见 _kernel_verdict ④ 处的事故注释）。
# 2026-10-15 加：此前缺这一条，「字数 12000–15000（不含参考文献表）」被误判成
# 「必须零引用」，成为一道不可能通过的门。
_ACC_CITE_SCOPE_ONLY = re.compile(
    r"(?:不含|不计入|不计算|除外|另计|另算|剔除)\s*"
    r"(?:文末|末尾|篇末)?\s*(?:参考文献表?|文献表|文献列表|引用列表)",
)
# 标准明确提到**文末的参考文献表**时，才连带核 `has_refs`（否则只核文中标记）。
# 分开的理由：`citations` 只数文中标记（[1] / 作者-年份），
# **一份只有文末文献表、正文一个标记都没有的稿子，citations 也是 0**
# —— 拿它去证明「无参考文献列表」就是超范围断言（L 型：声明 ≠ 事实）。
_ACC_REF_WORD = re.compile(r"参考文献|文献列表|references?|bibliography", re.I)

# 字数容差：与 contract 里承诺的 ±15% 保持一致，别再引入第二套阈值
_CHARS_TOLERANCE = 15.0

# 内核判据的三态。**必须三态**，理由见 _kernel_verdict 的长注释。
KERNEL_PASS = "pass"        # 内核实测：已达标
KERNEL_FAIL = "fail"        # 内核实测：未达标（终审，不许上诉到措辞）
KERNEL_UNKNOWN = "unknown"  # 内核判不了（才轮到措辞匹配）


@dataclass
class EvidenceRef:
    tool: str              # 哪个工具产生的
    call_seq: int          # 台账里的第几次调用（唯一可验证的锚点）
    shows: str = ""        # 它证明了什么
    acceptance: str = ""   # 对应契约里的哪条验收标准

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceRef":
        def _int(v) -> int:
            try:
                return int(v)
            except Exception:
                return -1
        return cls(
            tool=str(d.get("tool") or ""),
            call_seq=_int(d.get("call_seq", d.get("seq", -1))),
            shows=str(d.get("shows") or ""),
            acceptance=str(d.get("acceptance") or ""),
        )


@dataclass
class CompletionClaim:
    deliverables: list[str] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    summary: str = ""
    # 写作任务专用：正文全文（短稿内联）或稿件文件（长稿，相对工作区）
    draft_text: str = ""
    draft_path: str = ""

    @classmethod
    def from_args(cls, args: dict) -> "CompletionClaim":
        ev = args.get("evidence") or []
        if isinstance(ev, dict):
            ev = [ev]
        return cls(
            deliverables=[str(x) for x in (args.get("deliverables") or [])],
            evidence=[EvidenceRef.from_dict(e) for e in ev if isinstance(e, dict)],
            summary=str(args.get("summary") or ""),
            draft_text=str(args.get("draft_text") or ""),
            draft_path=str(args.get("draft_path") or ""),
        )


@dataclass
class Verdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)   # 硬门：阻断完成
    warnings: list[str] = field(default_factory=list)  # 软检查：只提示，不阻断
    feedback: str = ""
    writing: str = ""                                  # 写作体检报告（回灌 + 上界面）
    lint: dict | None = None                           # 体检摘要（结构化，给 UI 用）

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reasons": self.reasons,
                "warnings": self.warnings, "writing": self.writing,
                "lint": self.lint}


class CompletionGate:
    # 能算"装载过技能"的工具。荣珂自带 load_skill；工作台 read_skill 也认（兼容旧路径）。
    LOADER_TOOLS = ("load_skill", "read_skill")
    # 能算"核验过引文"的工具（写作体检的引注联动用）
    CITE_TOOLS = ("verify_citation", "fetch_doi", "build_references")
    # 能算"体检过图面"的工具（第 8 条图面门用）
    FIGURE_TOOLS = ("audit_figure",)
    # 能算「显式走了预览路线」的工具（2026-11-05）。
    # ★ 它不是合格证：只有台账里**真出现过一次成功的 plot_svg**，
    #   才允许把第 8 条硬门降成警告。没调工具却自称「我出了预览图」不算。
    PREVIEW_TOOLS = ("plot_svg",)
    # 能算"体检过演示文稿"的工具（第 9 条 deck 门用）
    DECK_TOOLS = ("audit_deck",)

    def __init__(self, registry: ToolRegistry, strict_coverage: bool = True,
                 writing_passes: int = 1, caps=None):
        """`writing_passes`：允许写作体检**打回几次**。默认 1。

        为什么不是无限：体检是启发式的，一定存在误报。允许无限打回会陷入
        「模型改一遍 → 换一条 soft 冒出来 → 再打回」的消耗战，把一篇稿子烧成十轮。
        一次是刻意的：**第一遍逼它真改，第二遍认账放行**。
        这与项目既有教训一致（字面匹配当硬门必误伤 → 硬门只留可核对的）。
        """
        self.registry = registry
        self.strict_coverage = strict_coverage
        self.writing_passes = writing_passes
        # 能力预设的只读视图。`None` / `Caps.empty()` ⇒ 全部判据取旧常量，
        # 与加这个形参之前逐字节等价（见 `caps.py` 的 `flag()` 约定）。
        self.caps = caps
        self._writing_used = 0
        self.last_report: Report | None = None
        self._warn_seen: set[str] = set()

    def reset(self) -> None:
        """每个任务开始时归零 —— 否则上一轮的打回额度会漏到下一轮。"""
        self._writing_used = 0
        self.last_report = None
        self._warn_seen = set()

    def _loaded_skills(self) -> set[str]:
        """台账里**成功**装载过的技能名。

        ⚠️ 只认**入口文档**的装载：`load_skill(name=..., file="references/x.md")`
        读的是参考文件，**不算"装载过这个技能"**。

        为什么（2026-09-18）：`load_skill` 新加了 `file` 参数用来按需读技能目录里的
        参考文件 —— 而技能门原先只看 `name`、不看 `file` ⇒ **新参数会悄悄把硬门开大**：
        模型只读一份 `references/introduction.md` 就能凑齐"装载过 nature-writing"，
        从来没读过入口文档里那份**协议**。而这道门存在的全部理由就是"不许凭记忆写稿"，
        它守的正是"读没读过协议"。

        这是"加一个参数把既有的门开大"这一类缺陷 —— 加功能时要回头看门。
        向后兼容：工作台的 `read_skill` 没有 `file` 参数 ⇒ 取到空串 ⇒ 照常计数。
        """
        out: set[str] = set()
        for c in self.registry.trace:
            if not c.ok or c.name not in self.LOADER_TOOLS:
                continue
            args = c.args or {}
            nm = args.get("name") or args.get("skill")
            # 有 file（非空白）= 读参考文件，不是读协议 ⇒ 不算装载
            if nm and not str(args.get("file") or "").strip():
                out.add(str(nm).strip())
        return out

    # ── 库够用性台账 ────────────────────────────────────────
    def _coverage_sufficient(self) -> tuple[bool, str]:
        """台账里有没有一次**判「库够用」**的 `kb_coverage`。

        为什么这条要影响阶段技能门（见第 6 条对 `fulltext` 的豁免）：
          「库够用就别去检索入库」是用户流程的**第一条**。如果门还硬性要求装载
          `nature-downloader`（取全文阶段的专属技能），模型就被自己的证据门
          逼着去走一遍它本来不该走的路 —— 那与本仓记过的
          「不需要检索文献 → 契约硬推 search 阶段」是同一类矛盾：
          **契约/门逼模型做一件流程上明确不该做的事。**

        ★ 只认 `need_more_literature is False` 这一种值。
          拿不到、解析不了、字段缺失 ⇒ **不算够用**（保守方向：照常要求装载技能）。
          「没法证明够用」与「够用」必须分开，否则这个豁免会变成跳过检索的后门。
        """
        for c in self.registry.trace:
            if not c.ok or c.name != "kb_coverage":
                continue
            try:
                d = json.loads(c.result or "{}")
            except Exception:
                continue
            if isinstance(d, dict) and d.get("need_more_literature") is False:
                return True, str(d.get("why") or "")
        return False, ""

    # ── 图面体检台账 ────────────────────────────────────────
    def _figure_audits(self) -> list[tuple[ToolCall, dict]]:
        """台账里**成功**跑过的图面体检，附解析出来的报告。

        判据只看台账（与证据门、技能门同一套规矩）：模型嘴上说"图我审过了"不算。
        审计工具的返回值是一段**紧凑 JSON**（全文报告写盘），所以这里直接
        `json.loads`。**解析失败当失败处理**，不静默当通过 ——
        一个解析不了的"合格证"不是合格证。
        """
        out: list[tuple[ToolCall, dict]] = []
        for c in self.registry.trace:
            if not c.ok or c.name not in self.FIGURE_TOOLS:
                continue
            try:
                d = json.loads(c.result or "{}")
            except Exception:
                continue
            if isinstance(d, dict) and d:
                out.append((c, d))
        return out

    # ── 预览路线 / 投稿路线：两条图链路的台账判据（2026-11-05）──
    def _preview_svg_runs(self) -> list[dict]:
        """台账里**真的出过**预览级 SVG 的调用。

        覆盖两种入口：显式 `plot_svg`，以及 `plot_data` 在缺绘图栈时
        自动降级挂出来的 `native_svg`。判据只看工具返回 JSON 里的
        `ok=true` 且 `scope="preview"`，不采信模型正文里的自述。
        """
        out: list[dict] = []
        for c in self.registry.trace:
            if not c.ok:
                continue
            try:
                d = json.loads(c.result or "{}")
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if c.name in self.PREVIEW_TOOLS and d.get("ok") is True \
                    and d.get("scope") == "preview":
                out.append(d)
            elif c.name == "plot_data":
                sv = d.get("native_svg")
                if isinstance(sv, dict) and sv.get("ok") is True \
                        and sv.get("scope") == "preview":
                    out.append(sv)
        return out

    def _submission_figure_runs(self) -> list[dict]:
        """台账里真跑出了 PDF 的 `plot_data` 调用。

        有它就不该按「预览路线」放行 —— 那说明本次确实走了投稿链路，
        第 8 条门必须照旧要求 `audit_figure`。
        """
        out: list[dict] = []
        for c in self.registry.trace:
            if not c.ok or c.name != "plot_data":
                continue
            try:
                d = json.loads(c.result or "{}")
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            run = d.get("run") or {}
            if isinstance(run, dict) and run.get("ok") and (run.get("pdf") or ""):
                out.append(d)
        return out

    # ── deck 体检台账 ──────────────────────────────────────
    def _deck_audits(self) -> list[tuple[ToolCall, dict]]:
        """台账里**成功**跑过的 deck 体检，附解析出来的报告。

        与 `_figure_audits` 同一条规矩：判据只看台账（模型嘴上说"我审过了"不算），
        返回值是紧凑 JSON，`json.loads` 不了就当失败 ——
        **一个解析不了的"合格证"不是合格证**。
        """
        out: list[tuple[ToolCall, dict]] = []
        for c in self.registry.trace:
            if not c.ok or c.name not in self.DECK_TOOLS:
                continue
            try:
                d = json.loads(c.result or "{}")
            except Exception:
                continue
            if isinstance(d, dict) and d:
                out.append((c, d))
        return out

    # ── 内核自己的测量，充当验收证据 ────────────────────────
    def _kernel_verdict(self, acc: str, rep: Report | None) -> tuple[str, str]:
        """这条验收标准，**内核自己**能不能判定？返回 `(三态, 说明文字)`。

        三态：`pass`（实测已达标）/ `fail`（实测未达标）/ `unknown`（判不了）。

        为什么必须是三态（2026-09-19 实测）：
        原实现只有「达标 → 给文字；不达标 → 给空串」，于是
        **实测不达标**与**判不了**长得一模一样 —— 两者都掉进措辞匹配。
        后果是可复现的：草稿里 9 处引用标记、体检报告白纸黑字写着 `citations: 9`，
        验收标准是「全文不含任何文献引用」，而模型只要把**这条标准的原文抄进
        `evidence.acceptance`**，`_covers()` 精确命中，门就放行了 ——
        **内核量出来的事实，被被审者的一句话推翻了。**

        同一族的第二个实证：目标 150 字、实测 350 字（容差 ±15%），
        模型声明「字数约150字」照样放行。

        规矩一句话：**内核量得出来的，内核说了算；量不出来的才交回措辞。**
        这正是 `_ACC_CHARS` 上方那条「能算的就不许靠匹配」——
        原实现只让肯定侧成了终审，否定侧还能上诉，这里把否定侧补上。

        刻意保守（与原来的口径一致）：
        - 「字数够不够」只在**有目标字数**时可判，没有就不判；
        - 引注只判**全称否定式**标准（全文/任何…不含引用），且一旦标准在谈
          格式/条数/页码，就交回措辞匹配（内核不管格式）；
        - 一个标准里能判的判据全判，**任一不达标即 fail**，与判据书写顺序无关。
        """
        if rep is None:
            return KERNEL_UNKNOWN, ""
        m = rep.metrics or {}
        a = _norm(acc)

        ok_notes: list[str] = []
        bad_notes: list[str] = []
        judged = 0

        # ① 字数
        if _ACC_CHARS.search(a) and rep.target_chars:
            judged += 1
            d = abs(m.get("chars_delta_pct") or 0.0)
            _du = (str(self.caps.flag("chars", "display_unit", "chars"))
                   if self.caps is not None else "chars")
            txt = (f"{m.get(_du, m.get('chars', 0))} 字 vs 目标 {rep.target_chars} 字"
                   f"（偏差 {d:.0f}%，容差 ±{_CHARS_TOLERANCE:.0f}%）")
            (ok_notes if d <= _CHARS_TOLERANCE else bad_notes).append(txt)

        # ② 段落数
        if _ACC_PARA.search(a) and m.get("paragraphs") is not None:
            judged += 1
            n = m.get("paragraphs")
            (ok_notes if n else bad_notes).append(f"{n} 段")

        # ③ 句数
        if _ACC_SENT.search(a) and m.get("sentences") is not None:
            judged += 1
            n = m.get("sentences")
            (ok_notes if n else bad_notes).append(f"{n} 句")

        # ④ 引注：只判「有没有」，且只判全称否定式标准
        #    ★ 2026-10-15 修：必须先排除「这条标准其实在谈字数」。
        #    实测事故（run 20261015-092001）：验收标准是
        #      「正文汉字数在 12000–15000 之间（不含参考文献表）」
        #    其中「不含参考文献表」同时命中 _ACC_CITE_NEG（不含+文献），
        #    于是这条**字数**标准被 ④ 判到了**引注**上，要求 cites == 0。
        #    后果：稿子只要有任何引用标记就恒 fail，模型永远无法达标 ——
        #    它为此空转 205 步 / 6928 秒，日志里 5 次打回，每次「内核实测」
        #    都只印出「N 处引用标记」而从不印字数。这是**不可能通过的门**。
        #    判据：标准里若出现字数词，且否定语紧跟的是 参考文献/文献表
        #    这类**篇幅限定语**，则 ④ 让位给 ①，不参与判定。
        if (_ACC_CITE_NEG.search(a) and _ACC_CITE_CATEGORICAL.search(a)
                and not _ACC_CITE_META.search(a) and not _ACC_CITE_QUALITY.search(a)
                and not _ACC_CITE_SCOPE_ONLY.search(a)
                and m.get("citations") is not None):
            judged += 1
            cites = m.get("citations") or 0
            wants_refs = bool(_ACC_REF_WORD.search(a))
            refs = m.get("has_refs")
            txt = f"{cites} 处引用标记"
            if wants_refs and refs is not None:
                txt += "、" + ("有参考文献表" if refs else "无参考文献表")
            clean = cites == 0 and (not wants_refs or refs is None or not refs)
            (ok_notes if clean else bad_notes).append(txt)

        # ⑤ 预设点名的额外指标（Q2）。只在 `gate-kernel` 能力实例的
        #    `measured_metrics` 超出 baseline 6 项时才进来，且**每条规则
        #    都要验收标准里出现对应触发词** —— 不许凭空给自己加标准。
        gate_cap = self.caps.inst("gate") if self.caps is not None else None
        extra = list(gate_cap.extra_metrics()) if gate_cap is not None else []
        wanted = set(extra)
        # ★ 2026-11-05 内核优化：额外判据不再只有内核里那 7 条硬编码表。
        #   已挂载能力可贡献同形状的规则 (key, 正则, 模式, 标签)，于是
        #   「加一条新判据 = 加能力 + 预设点名」，不必再改 gate.py。
        #   贡献即点名：否则会出现「能力挂上了却没生效」这种假成功。
        #   baseline 没有任何贡献 ⇒ 合并集与旧表逐字相等。
        _contrib = getattr(self.caps, "extra_rules", None)
        _contributed = list(_contrib()) if callable(_contrib) else []
        wanted |= {r[0] for r in _contributed}
        for key, pat, mode, label in list(_EXTRA_RULES) + _contributed:
            _pat = re.compile(pat) if isinstance(pat, str) else pat
            if key not in wanted or not _pat.search(a) or key not in m:
                continue
            judged += 1
            v = m.get(key) or 0
            good = (v == 0) if mode == "eq0" else (v > 0)
            txt = f"{label} {v} 处（要求 {'0' if mode == 'eq0' else '≥1'}）"
            (ok_notes if good else bad_notes).append(txt)
        key, pat, _mode, label = _EXTRA_ORDER
        if key in wanted and pat.search(a) and key in m:
            judged += 1
            seq = list(m.get(key) or [])
            good = seq == sorted(seq)
            txt = f"{label} {seq[:12]}（{'符合' if good else '逆序'}）"
            (ok_notes if good else bad_notes).append(txt)

        if bad_notes:
            return KERNEL_FAIL, "；".join(bad_notes)
        if judged:
            return KERNEL_PASS, "内核体检已达标：" + "；".join(ok_notes)
        return KERNEL_UNKNOWN, ""

    # ── 主判定 ──────────────────────────────────────────────
    def check(self, claim: CompletionClaim, contract: TaskContract) -> Verdict:
        reasons: list[str] = []
        warnings_extra: list[str] = []
        by_seq: dict[int, ToolCall] = {c.seq: c for c in self.registry.trace}
        # 契约明令禁用工具 ⇒ 证据类硬门 1/4/6 **不适用**（详见模块 docstring）。
        # 「不适用」有精确的边界：2/3（不许编造、引用必须真实）**照常生效** ——
        # 零工具任务若敢在证据里编 call_seq，台账是空的，第 2 条当场抓。
        no_tools = bool(getattr(contract, "no_tools", False))
        # 问话契约（见 `TaskContract.answer_only`）。
        answer_only = bool(getattr(contract, "answer_only", False))

        # 1. 非空
        #
        # ★ 问话这一类，"非空"要核的是**回答本身**。原实现只核证据链，
        #   于是对一句问话会出现最荒谬的一种"通过"：答 0 字、附了几条检索调用，
        #   门判它完成 —— 用户那边屏幕上什么都没有。归档里那次是反过来的一面
        #   （0 字 + 打回），两面同一个根：**门只看证据、不看答没答**。
        if answer_only and not (claim.summary or "").strip():
            reasons.append(
                "这是提问，不是任务 —— 请在 deliver_task 的 summary 里写上你的回答正文"
                "（可以直接是完整的回答，不必压缩成一句）。证据可以留空。"
            )
        if not claim.evidence:
            if no_tools or answer_only or not contract.needs_writing:
                # ★ W36：**非写作任务**不要求证据链 —— 与"没有验收标准"是同一个理由。
                #   门核的是稿子，稿子之外的活（检索、入库、挂载、改台账）的成功
                #   信号工具自己就回了（ok/verified），再要一份 call_seq 清单只是
                #   让它把已经做过的事再证明一遍，做不到就卡死。
                #   注意这里放松的**只有"有没有附证据"**：附了但编造的，
                #   第 2/3 条照样抓（`by_seq` 是空台账，编不出来）。
                pass
            else:
                reasons.append("没有提供任何证据。宣布完成必须附上产生该结果的工具调用（call_seq）。")

        real: list[tuple[EvidenceRef, ToolCall]] = []
        # 2 & 3. 可追溯 + 成功
        for i, ev in enumerate(claim.evidence, 1):
            tc = by_seq.get(ev.call_seq)
            if tc is None:
                reasons.append(
                    f"证据 #{i} 引用了 call_seq={ev.call_seq}，但台账里没有这次调用"
                    f"（可能是编造的）。台账现有 seq: {sorted(by_seq) or '空'}"
                )
                continue
            if ev.tool and not _covers(ev.tool, tc.name):
                reasons.append(
                    f"证据 #{i} 声称来自 {ev.tool}，但 call_seq={ev.call_seq} 实际是 {tc.name}。"
                )
                continue
            if not tc.ok:
                reasons.append(
                    f"证据 #{i}（call_seq={ev.call_seq} {tc.name}）是一次**失败**调用：{tc.error[:120]}"
                )
                continue
            real.append((ev, tc))

        # ── 写作体检：**先算，再判**（顺序很重要）──
        # 必须排在第 4 条「验收覆盖」之前：像「字数约120字」这种标准，
        # 内核能自己数出来，就不该退化成比对模型措辞（见上方 _ACC_CHARS 的注释）。
        rep: Report | None = None
        draft_err = ""
        if contract.needs_writing:
            draft, draft_err = self._draft_text(claim)
            if not draft_err:
                rep = lint(draft, target_chars=contract.target_chars,
                           allow_lists=contract.allow_lists, caps=self.caps)
                self.last_report = rep

        # 4. 验收覆盖
        uncovered: list[str] = []
        measured_fail: list[str] = []
        if contract.acceptance:
            for acc in contract.acceptance:
                state, why = self._kernel_verdict(acc, rep)

                # 4a. 内核实测不达标 ⇒ **终审**，直接 continue，不许上诉到措辞。
                #     理由（2026-09-19 实测）：允许上诉时，模型把验收标准原文抄进
                #     `evidence.acceptance` 就能推翻 `citations: 9` / `chars: 350`
                #     这类实测事实。报告里也**分开写**：实测不达标要改的是稿子，
                #     「无证据支撑」要补的是证据 —— 原来两句混成一句，
                #     模型收到「无证据支撑」就真的去找证据了（实测多跑 4 步、多烧 57,876 输入 token）。
                if state == KERNEL_FAIL:
                    measured_fail.append(f"{acc}（内核实测：{why}）")
                    continue

                # 4b. 措辞匹配（独立锚点 `tc.result` 与模型自述 `acceptance`/`shows` 并列，
                #     这条回退路径只服务「内核判不了」的标准）。
                # ★ Q1：`evidence_fallback=false` 的预设下，只挡「整条证据
                #   除了复述标准什么都没有」。绑定字段填标准原文是**正确用法**，
                #   不能拿它判复述（D 场死门的根因）；实质内容看 shows 与回包。
                allow_echo = (self.caps.flag("gate", "evidence_fallback", True)
                              if self.caps is not None else True)
                hit = state == KERNEL_PASS or any(
                    (allow_echo or not _evidence_is_echo(acc, ev, tc))
                    and (_covers(acc, ev.acceptance) or _covers(acc, ev.shows)
                         or _covers(acc, tc.result[:2000]))
                    for ev, tc in real
                )
                if not hit:
                    uncovered.append(acc)

            if measured_fail:
                reasons.append(
                    "以下验收标准**内核实测未达标**（改稿，不是补证据）：\n"
                    + "\n".join(f"  - {u}" for u in measured_fail)
                )
            if uncovered:
                if no_tools:
                    # 禁用工具 ⇒ "补证据"这条路不存在，降为警告；
                    # 内核实测得了的标准已经走 4a 终审，不在这里放水。
                    warnings_extra.append(
                        "以下验收标准无法被证据覆盖（本任务禁用工具，只作提醒）：\n"
                        + "\n".join(f"  - {u}" for u in uncovered)
                    )
                else:
                    reasons.append(
                        "以下验收标准没有任何证据支撑：\n"
                        + "\n".join(f"  - {u}" for u in uncovered)
                    )
        elif self.strict_coverage and not no_tools:
            # 禁用工具的任务没有验收标准时，"补可检验的标准"这条路也不通 ——
            # 它的完成本来就只能由内核实测与契约禁止项约束。
            #
            # ★ 2026-10-31（W36 二改，用户原话「除非是写作任务」）：
            #   **只有写作任务**才因为"没有验收标准"被打回。其余一概不判。
            #
            #   改动前的两次错误，记在这里免得再犯：
            #   ① 最初这里**无条件**打回。一句问话被起草器按铁律判成 `acceptance=[]`，
            #      于是答得再好也过不了门。归档 `runs/20261031-085105` 的现场：
            #      它其实干了活（kb_search 19 次、topic_write 4 次、三份台账都写了），
            #      31 条 assistant 却全是空正文 —— 每次都被这一条打回，直到用户叫停。
            #   ② 我第一次修成"问句不走这条"（`answer_only`）。不够：用户说的是
            #      「帮我检索文献入库」「修改课题内容」，那不是问句，仍然被打回。
            #   真判据是**这件事是不是写作**，不是"它是不是问句"。
            #
            #   为什么不给非写作任务补一套"工具台账判定"：那是**又加一层限制**。
            #   对话式动作（检索、入库、挂载、改台账）的成功信号工具自己就有
            #   （回包 ok、文件真写了），不需要证据门替它判。门的职责是核稿子，
            #   稿子之外的活去核，只会把它判成失败。
            if contract.needs_writing:
                reasons.append(
                    "契约里没有可核对的验收标准（起草时未能提取），无法判定完成。"
                    "请先用可检验的语言补充验收标准，或改用 report_blocker 说明为什么无法完成。"
                )

        # 5. 交付物覆盖 —— **软检查，只警告不阻断**
        #    理由：deliverables 是模型自己组织的措辞，自由度大，字面匹配天然脆弱。
        #    实测（2026-09-18 首跑）把它当硬门会误伤正确交付：模型本已干对，
        #    却连续 3 轮被打回，逼它多跑一次 113s 的 build_references，
        #    多烧一倍 token 却没有任何信息增益。硬门留给可核对的 acceptance。
        warnings: list[str] = []
        if claim.deliverables and real:
            pool = " ".join(
                f"{ev.shows} {ev.acceptance} {tc.name} {tc.result[:1500]}" for ev, tc in real
            )
            missing = [d for d in claim.deliverables if _norm(d) not in _norm(pool)]
            if missing and len(missing) == len(claim.deliverables):
                # ★ Q7 告警去重（默认关闭）：这条在 5 次交付里亮了 5 次，
                #   恒亮 = 没有告警。预设打开时**每场只说一次**，baseline 逐字不变。
                _dedupe = (self.caps.flag("stage", "dedupe_warnings", False)
                           if self.caps is not None else False)
                show = True
                if _dedupe:
                    if "no_overlap" in self._warn_seen:
                        show = False
                    else:
                        self._warn_seen.add("no_overlap")
                if show:
                    warnings.append(
                        "交付物描述与证据措辞没有交集（不影响判定，仅供自查）：\n"
                        + "\n".join(f"  - {d}" for d in missing)
                    )

        # 6. 阶段技能门 —— **硬门**（契约禁用工具时**不适用**：load_skill 本身就是一次
        #    工具调用 —— 要求它调用，等于逼它违反用户明令，与证据门同一条互斥逻辑。）
        #    用户要求：各阶段必须调用 skill，且**每个阶段有唯一的专属技能**。
        #    工作台原本只有 process_audit 的事后审计（把"写作期间零技能登记"标为可疑），
        #    这里升级为交付前硬门。与契约里的「阶段专用技能」协议构成事前+事后两道。
        #    动因：整合版技能让五个技能抢"写论文"，模型一口气读了 4 个（README 有实测）。
        #
        #    ★ W36（2026-10-31 定稿）：这道门**降级为提醒，不再判失败**。
        #      用户原话：「要装载技能调用工具，但不用那么复杂的证据门，比如限制多少步」。
        #
        #      这两件事被我一直绑在一起，其实是两回事：
        #        · **装技能**是「该读的读一遍再动手」—— 属于**提示词里的行为要求**
        #          （见 loop.py 第 72 行：进阶段前必须 load_skill）。它该由模型照做，
        #          做不到时体现为"活干得糙"，而不是"交付被拒"。
        #        · **过门**是"没做就不许交差"—— 那才是用户反对的限制层。
        #      检索入库要前者，不要后者。
        #
        #      实测代价：一句「把这篇挂到课题上」，阶段推出文献类，一次 write_doc
        #      就能办成，却被判"专属技能未装载"；「帮我检索文献入库」检索都做完了，
        #      还因为"没先读检索方法论"被判失败。**它已经干完了活，门在后面罚它。**
        #
        #      所以现在：缺技能 → 写进 warnings（人看得见、模型也看得见，可以补），
        #      但不进 reasons（不阻断交付）。
        if contract.stages and not no_tools:
            loaded = self._loaded_skills()
            have = available()
            missing: list[str] = []
            missing_sids: list[str] = []   # 与 missing 逐项对齐：区分「预设点名」与「话术带出来」
            unavailable: list[str] = []
            # ★ 库够用 ⇒ 豁免 fulltext 阶段（2026-10-22）。
            #   用户流程第一条就是「库够用就不去检索入库」。若门仍硬要求装载
            #   nature-downloader，模型会被自己的门逼着走一遍不该走的路 ——
            #   与「不需要检索文献 → 契约硬推 search 阶段」是同一类矛盾。
            #   豁免条件写得**严**：必须有当轮一次成功的 `kb_coverage` 且它明确回
            #   `need_more_literature=false`；拿不到就不豁免（保守方向）。
            _skip = set()
            _cov_ok, _cov_why = self._coverage_sufficient()
            if _cov_ok:
                _skip.add("fulltext")
            for sid in contract.stages:
                st = STAGE_BY_ID.get(sid)
                if st is None or st.skill in loaded:
                    continue
                if sid in _skip:
                    warnings.append(
                        f"[阶段] 已跳过「{st.name}」—— 本轮的 `kb_coverage` 判定库够用"
                        f"（{_cov_why or '见回包'}），按流程**不该**再去检索入库，"
                        f"故不要求装载 {st.skill}。缺哪一篇就只补那一篇。")
                    continue
                if st.skill not in have:
                    unavailable.append(f"{st.name} → {st.skill}")
                else:
                    missing.append(f'  - {st.name} → load_skill("{st.skill}")')
                    missing_sids.append(sid)
            if missing:
                # ★ 从 reasons 改到 warnings（W36 定稿）：不再阻断交付。
                #   措辞也跟着改 —— 原来那句「凭记忆模仿不算数」是**罚**的语气，
                #   现在它是提醒：还来得及补，补了活更好，不补也能交。
                # ★ Q7 预设可把这道门改回硬门；baseline 的文案**一字不动**。
                _hard = (self.caps.flag("stage", "hard", False)
                         if self.caps is not None else False)
                # ★ 2026-11-04：硬门只约束**预设点名的那几道工序**。
                #   理由：预设声明的工序在契约计划里逐条念给模型看过（模型知道
                #   该 load_skill 哪几个），没装就是没做，拦得有理有据；而话术
                #   关键词顺带带出来的工序模型未必预期得到，一律硬拦就会复现
                #   D 场那种「稿子写好了却永远交不出去」的死门。
                #   W36 的软门原义在这条分支里**完整保留**。
                _declared = set(getattr(self.caps, "preset_stages", []) or []) \
                    if self.caps is not None else set()
                if _hard:
                    _pairs = list(zip(missing_sids, missing))
                    # ★ 2026-11-04 真 HTTP 抓到的误伤（run 03a69c03c26a）：
                    #   选了 stats-review 胶囊，用户只问一句「为什么核算字数上限
                    #   要剔除文献表」。契约判成 answer_only / mode:"chat"，
                    #   内核自己说着「直接办，不列任务单、不要验收标准」，
                    #   回答已经写好，硬门却因没 load_skill("nature-statistics")
                    #   把它拒了 —— 与 D 场「稿子写好了永远交不出去」同族：
                    #   **判据落在了根本不存在流水线的那一类任务上**。
                    #   问话没有要走工序的产物，装载技能全文不改变它答得对不对。
                    #   所以这里把硬门降级为**提醒**：拦沉默，不拦选择；
                    #   真任务（有交付物 / needs_writing）仍按下面原样硬拦。
                    if answer_only:
                        _must, _soft = [], [t for _, t in _pairs]
                    elif _declared:
                        _must = [t for sid, t in _pairs if sid in _declared]
                        _soft = [t for sid, t in _pairs if sid not in _declared]
                    else:
                        _must, _soft = [t for _, t in _pairs], []
                    if _must:
                        reasons.append(
                            "[阶段技能·硬门] 以下阶段的专属技能还没装载，本轮不放行：\n"
                            + "\n".join(_must)
                        )
                    if _soft:
                        warnings.append(
                            "[阶段技能] 以下阶段的专属技能这一步还没装载（不阻断交付，"
                            "能补就补 —— 阶段纪律写在技能全文里，读了再做更稳）：\n"
                            + "\n".join(_soft)
                        )
                else:
                    warnings.append(
                        "[阶段技能] 以下阶段的专属技能这一步还没装载（不阻断交付，"
                        "能补就补 —— 阶段纪律写在技能全文里，读了再做更稳）：\n"
                        + "\n".join(missing)
                    )
            if unavailable:
                warnings.append(
                    "以下阶段的专属技能在本机找不到，无法装载"
                    "（如实报告即可，不算你的错）：\n"
                    + "\n".join(f"  - {u}" for u in unavailable)
                )

        # 7. 写作体检 —— **终于看一眼文本本身**（硬门，但最多打回一次）        #    前 6 条核的全是「流程」：证据可不可追溯、技能装没装。
        #    这一条补的是最容易蒙混过关的一类瑕疵 —— **流程全合规，稿子却没法看**。
        #    动因：一段 118 字的引言，只要技能装过、证据链完整，门原本直接放行；
        #    哪怕它通篇是「随着…的飞速发展，众所周知，具有十分重要的意义」。
        writing_text = ""
        if contract.needs_writing:
            if rep is None:
                # 拿不到正文（既没 draft_text 也没 draft_path，或路径越界/不存在）。
                # 注意：这时第 4 条的字数验收也失去了内核兜底，会一并打回 —— 合理。
                reasons.append(draft_err or "写作任务拿不到正文，无法完成稿面体检。")
            else:
                writing_text = rep.format_for_model()
                # 引注联动：正文写了引注，台账里却一次核验类调用都没有
                cites = rep.metrics.get("citations") or 0
                if cites and not any(c.ok and c.name in self.CITE_TOOLS
                                     for c in self.registry.trace):
                    warnings.append(
                        f"正文含 {cites} 处引注标记，但台账里没有一次引注核验调用"
                        f"（{'/'.join(self.CITE_TOOLS)}）。引注若来自本次工具检索可忽略；"
                        "若是凭记忆写的，必须补核验 —— 这条不进硬门，但审稿人会查。")
                if rep.needs_revision:
                    if self._writing_used < self.writing_passes:
                        self._writing_used += 1
                        reasons.append(
                            f"写作体检未通过（评分 {rep.score}/100："
                            f"必修 {len(rep.hard())} 项、建议 {len(rep.soft())} 项）——"
                            "稿面问题见下方体检报告。**按报告逐条改**，"
                            "把改后的**全文**重新放进 draft_text（或覆盖 draft_path 的文件）再交付。"
                        )
                    else:
                        warnings.append(
                            f"写作体检仍不达标（评分 {rep.score}/100），但本轮已给过修订机会，"
                            "为免无限拉锯本次放行 —— 请你自行判断是否还要再改一轮。"
                        )

        # 7.5 综述文献量门 —— **综述类任务的参考文献不得少于 50 篇**
        #
        #     动因：用户明确要求「写综述文献最少 50 篇」。在这之前内核只有
        #     `has_refs`（0/1），它回答"有没有文献表"，回答不了"有几篇" ——
        #     一篇综述列 3 条参考文献也算 `has_refs=1` 合格。**有判据和没有判据
        #     在这里是一回事**，所以先给 `writing.py` 补了 `refs_n`。
        #
        #     ⚠️ 判据挂在「这是综述（`_is_review_like`）」上，不是挂在
        #     「`needs_writing`」上：一篇 2000 字的原创研究稿引 30 篇是正常的，
        #     套 50 篇会逼模型堆无关引文 —— 那正是"为了过门而变差"。
        #     这也与 `_derive_review_shape` 的既有口径一致（综述词表刻意窄）。
        #
        #     ⚠️ 只对**长综述**（`_is_long_review`）生效，理由同上：短稿
        #     「写一段综述性文字」不该被要求 50 篇文献。
        #     ★ 阈值从 `contract` 导入，**不在这里写第二个 50** ——
        #       同一口径只许有一份定义（本仓已经因为手抄两份吃过亏）。
        from .contract import _REVIEW_MIN_REFS
        if (rep is not None and getattr(contract, "_is_review_like", False)
                and getattr(contract, "_is_long_review", False)):
            _n = rep.metrics.get("refs_n")
            if _n is None:
                # 拿不到数就不拦 —— 宁可不管，也不许因为"我数不出来"罚模型。
                # 这条纪律在这个仓反复用过：**不出数，不出假数。**
                warnings.append(
                    "本轮没能数出参考文献条目数（文献表格式不被识别），"
                    f"因此未能核验「综述最少 {_REVIEW_MIN_REFS} 篇」这条要求。"
                    "请自行确认文献表用的是 `[1]`/`1.`/`- ` 这类可数的著录格式。")
            elif _n < _REVIEW_MIN_REFS:
                reasons.append(
                    f"综述的参考文献只有 {_n} 篇，低于要求的 **{_REVIEW_MIN_REFS} 篇**。\n"
                    "⇒ 补足的做法：用 `kb_coverage` 找出覆盖薄弱的章，"
                    "再对缺的章补检索（`kb_search` / `literature_fetch`）并把新文献入库，"
                    "然后**逐章把它们真正引到正文里**再补齐文献表。\n"
                    "⚠️ 不许为了凑数把没读过的文献塞进文献表 —— 那种稿子过得了这道门，"
                    "过不了 `verify_citation`，也过不了审稿人。")

        # 8. 图面门 —— 图必须**真的被程序看过了**，而且报告不是旧的
        #
        #    动因：图有一类独有的失败 —— **所有程序都退出 0，图是错的**。
        #    字体缺字只发一条 warning 就画成豆腐块；面板差 0.8 pt 肉眼看不出；
        #    字号在 PDF 里被缩到 4.9 pt。第 1~7 条门全都拦不住这一类，
        #    因为它们看的都是"流程有没有走"，而图的流程走完了照样出坏图。
        #
        #    **新鲜度这一条是从别处搬来的教训**（2026-09-16，本机 dsh 事故）：
        #    当时补丁在磁盘上「在位」，症状却没消失 —— 因为服务启动**早于**
        #    补丁落盘，一个字节都没加载。结论：**「东西在」不等于「东西生效」**。
        #    图这边同型：一份上一版图的绿报告躺在目录里，内容全绿、路径也对，
        #    但它审的不是这一版。所以这里比对**审计时记下的 mtime vs 磁盘现状**，
        #    对不上就判过期 —— 不许拿旧报告当合格证。
        figure_note = ""
        # ★ 三个台账各量一次，判定顺序有讲究（2026-11-05 实测修过一次）：
        #   预览分支**必须排在「audit_figure 没装」之前**。首版把它放在
        #   `elif contract.needs_figure:` 里，结果零依赖装配（本来就没装
        #   audit_figure）会被旧分支先吃掉，模型只收到「体检没装上」，
        #   拿不到「本次是预览路线、六门没跑」这条更准确的警告。
        #   ⇒ 判据的**分支顺序**也是覆盖面的一部分，不是收纳偏好。
        audits = self._figure_audits() if contract.needs_figure else []
        preview = self._preview_svg_runs() if contract.needs_figure else []
        submission = self._submission_figure_runs() if contract.needs_figure else []
        if not contract.needs_figure:
            pass
        elif preview and not submission and not audits:
            # 三条同时成立才降成警告：① 台账里**真有**一次成功的 plot_svg
            # （或 plot_data 的自动降级）；② 没有跑出 PDF 的投稿链路；
            # ③ 没有 audit_figure 台账。缺件、没调工具、自称出了图一律照旧拦。
            # 拦的是沉默，不拦**显式选择**的路线。
            warnings.append(
                "【预览级出图 —— 不是通过】台账里只有荣珂原生 SVG 后端出的预览图"
                "（scope=preview，零第三方依赖），本次**没有**跑 PDF 六道渲染后审计门："
                "源码预检 / 字形 ≥5 pt / 渲染后碰撞 / 多分面对齐 / 图注体检 / "
                "报告新鲜度。SVG 是矢量文本，字形由查看端字体栈解析，未做像素级验证；"
                "也没有 600 dpi 位图与投稿级物理尺寸。\n"
                "这是**显式选择**预览路线（台账里有成功的 plot_svg），门不把"
                "「没审」记成「审过了」，只如实降级为警告。"
                '要进投稿包请 pip install "rongke[figure]" 后走 '
                "plot_data + audit_figure 重出。")
            figure_note = f"（预览级 SVG {len(preview)} 张，未审）"
        elif self.registry.get("audit_figure") is None:
            # 图面体检被配置关掉了（figure.enabled=false）或没装上。
            # 这时**不能**照样硬拦 —— 否则一个关掉的开关会让所有配图任务
            # 永远交付不了。但也不能装没事：降为警告，如实说明没做体检。
            warnings.append(
                "本任务要产出图，但 `audit_figure` 没装上（配置里 figure.enabled=false，"
                "或装配失败）。**本次没有做图面体检** —— 字形缺字、面板错位、"
                "字号过低这类问题不会被发现，请自行目视核对最终 PDF。")
        elif not audits:
                failed = [c for c in self.registry.trace
                          if c.name in self.FIGURE_TOOLS and not c.ok]
                if failed:
                    reasons.append(
                        "本任务要产出图，图面体检**跑了但失败了**："
                        f"{failed[-1].error[:300]}\n"
                        "按上面的报错修掉再重跑 audit_figure。"
                        "（注意：`NOT AUDITABLE`（审不了）**不等于**通过 —— "
                        "比如多分面图没给分面几何清单、或找不到能跑审计的解释器。）")
                else:
                    reasons.append(
                        "本任务要产出图，但台账里**一次图面体检都没有**。"
                        "宣布完成前必须调用 `audit_figure`：它会跑源码预检 / "
                        "PDF 字形下限 / 渲染后碰撞 / 多分面对齐四道审计，"
                        "外加图注体检。\n"
                        "理由：图最常见的失败是**每个程序都退出 0、图是错的**"
                        "（字体缺字画成豆腐块、面板差零点几 pt、字号被缩到 5 pt 以下）——"
                        "这些都不报错，只能靠程序化审计发现。")
        else:
                tc, d = audits[-1]
                figure_note = f"（call_seq={tc.seq}，{len(audits)} 次体检）"
                if not d.get("ok"):
                    bad = d.get("blockers") or []
                    reasons.append(
                        f"图面体检**未通过**（判定 {d.get('verdict')}）"
                        f"{figure_note}。必须解决以下阻断项：\n"
                        + "\n".join(f"  - {x}" for x in bad[:6])
                        + "\n（改完图之后**重跑** audit_figure —— 报告不会自己变新。）")
                # 新鲜度：审计时记下的 mtime 必须与磁盘现状一致
                stale: list[str] = []
                for path_s, recorded in (d.get("artifact_mtimes") or {}).items():
                    try:
                        cur = Path(path_s).stat().st_mtime
                    except Exception:
                        stale.append(f"{Path(path_s).name}（审计后已被删除或不可读）")
                        continue
                    if abs(cur - float(recorded)) > 1e-6:
                        stale.append(
                            f"{Path(path_s).name}（审计时 {time.strftime('%H:%M:%S', time.localtime(float(recorded)))}"
                            f" → 现在 {time.strftime('%H:%M:%S', time.localtime(cur))}）")
                if stale:
                    reasons.append(
                        "**图面体检报告已过期** —— 以下文件在体检**之后**又被改过：\n"
                        + "\n".join(f"  - {x}" for x in stale)
                        + "\n这份报告审的是上一版图，不能当合格证。"
                        "改过图就必须**重跑** audit_figure。")
                for w in (d.get("warnings") or [])[:8]:
                    warnings.append(f"[图面] {w}")

        # 9. 组会 PPT 门 —— deck 必须**真的被程序看过了**，而且报告不是旧的
        #
        #    与第 8 条同源：**「能生成 .pptx」和「能讲」是两件事。**
        #    上游 MCP 只保证文件是合法 OOXML；把论文整段粘上一页、没有讲稿、
        #    正文小到后排看不见 —— 这些全都不报错，第 1~8 条门也拦不住
        #    （它们核的是"流程走没走"）。
        #
        #    ⚠️ **讲稿那一条的判据刻意不从报告里拿**：报告里的 `strict_notes`
        #    来自模型传的参数，而"被审者不该给自己定标准"（R 型）。
        #    这里改成：**由契约独立推出"用户是否要了讲稿"**，再读报告里
        #    `gates.notes` 这个**由配置阈值算出的事实**。两样都不是模型说了算。
        if contract.needs_deck and self.registry.get("audit_deck") is None:
            # 与图面门同一条纪律：开关被关掉时**不能照样硬拦**，
            # 否则一个 config 开关会让所有做 PPT 的任务永远交付不了。
            warnings.append(
                "本任务要产出演示文稿，但 `audit_deck` 没装上（配置里 deck.enabled=false，"
                "或装配失败）。**本次没有做 deck 体检** —— 形状越界、文字过密、"
                "字号过小、讲稿缺失这类问题不会被发现，请自行打开 pptx 核对。")
        elif contract.needs_deck:
            audits = self._deck_audits()
            if not audits:
                failed = [c for c in self.registry.trace
                          if c.name in self.DECK_TOOLS and not c.ok]
                if failed:
                    reasons.append(
                        "本任务要产出演示文稿，deck 体检**跑了但失败了**："
                        f"{failed[-1].error[:300]}\n"
                        "按上面的报错修掉再重跑 audit_deck。"
                        "（注意：`NOT AUDITABLE`（审不了）**不等于**通过 —— "
                        "比如 pptx 打不开、或找不到审计脚本。）")
                else:
                    reasons.append(
                        "本任务要产出演示文稿，但台账里**一次 deck 体检都没有**。"
                        "宣布完成前必须调用 `audit_deck`（传最终 .pptx 的路径）："
                        "它会跑上游 XML 审计（形状越界 / 文字密度 / AI 套话 / 裁图）、"
                        "荣珂收紧的版式判据（正文字号下限 / 页数 / 单页字数）、"
                        "讲稿备注覆盖率，以及报告新鲜度。\n"
                        "理由：**能生成 .pptx ≠ 能拿去讲** —— 这些失败全都不报错。")
            else:
                tc, d = audits[-1]
                deck_note = f"（call_seq={tc.seq}，{len(audits)} 次体检）"
                if not d.get("ok"):
                    bad = d.get("blockers") or []
                    reasons.append(
                        f"deck 体检**未通过**（判定 {d.get('verdict')}）{deck_note}。"
                        "必须解决以下阻断项：\n"
                        + "\n".join(f"  - {x}" for x in bad[:6])
                        + "\n（改完 deck 之后**重跑** audit_deck —— 报告不会自己变新。）")
                # 讲稿：判据来自契约（独立锚点），阈值判定来自报告里由配置算出的
                # `gates.notes`。**不读模型传的 require_notes。**
                wants_notes = contract_wants_notes(
                    contract.raw_goal, contract.goal,
                    " ".join(contract.deliverables or []))
                if wants_notes:
                    nv = (d.get("gates") or {}).get("notes")
                    if nv not in ("PASS", "NOT APPLICABLE"):
                        reasons.append(
                            f"本轮契约**明确要了讲稿/备注**，但 deck 的讲稿备注不合格"
                            f"（notes 门判定 {nv}，覆盖率 {d.get('notes_ratio')}）{deck_note}。\n"
                            "组会 PPT 的备注栏就是讲稿：来源、口径、会被问到的问题都在那里。\n"
                            "补的办法：调 `set_slide_notes(pptx=\"deck.pptx\", "
                            "notes=[每页一段])` —— 上游 37 个 MCP 工具都没有写备注的"
                            "能力，这个能力由内核直接提供（模型不需要自己写代码）。"
                            "补完**重跑** audit_deck：改过 deck，旧报告就作废了。")
                # 新鲜度：体检记下的 pptx mtime 必须与磁盘现状一致。
                # 与图面门同一条纪律：**「报告在位」≠「报告对得上这一版」**。
                rec = d.get("pptx_mtime")
                pp = str(d.get("pptx") or "")
                if rec and pp:
                    try:
                        cur = Path(pp).stat().st_mtime
                    except Exception:
                        reasons.append(
                            f"**deck 体检报告指向的 pptx 现在读不到了**：{pp}\n"
                            "产物必须在交付时还在，且体检报告要对得上它。")
                    else:
                        if abs(cur - float(rec)) > 1e-6:
                            reasons.append(
                                "**deck 体检报告已过期** —— 被审的 pptx 在体检**之后**"
                                f"又被改过（体检时 {time.strftime('%H:%M:%S', time.localtime(float(rec)))}"
                                f" → 现在 {time.strftime('%H:%M:%S', time.localtime(cur))}）。\n"
                                "这份报告审的是上一版 deck，不能当合格证。改过就必须**重跑** "
                                "audit_deck。")
                for w in (d.get("warnings") or [])[:8]:
                    warnings.append(f"[deck] {w}")

        # 10. 大纲门 —— 长综述的结构必须**在盘上冻结过**
        #
        # 为什么它是硬门而不是建议：用户的原话是「最好不用要进行多次返工，
        # 因为事实证明，多次返工出来的一般很垃圾」。返工的机制成因是
        # **结构与内容在同一遍里来回改** —— 写到第三节改第二节，后文连带重写。
        # 只把"先定大纲"写进提示词是不够的（本仓的定律：模型执行的是它读到的
        # 动作，而**没有门的动作只是建议**）。所以这里设一道盘上的判据。
        #
        # 判据取**最不容易造假的三样**：
        #   ① 文件在不在（工作区里有 outline/大纲 字样的 .md）
        #   ② 里面有没有章标题（Markdown 的 `#`/`##` 行）
        #   ③ 每章附近有没有 doc_id 形态的串（章-文献映射不是空的）
        # 不判"论点写得好不好" —— 那是语义判断，机器判不了，判了就是误报。
        # ★ 判据取「契约的阶段里**有** outline」，而不是只看「这是不是长综述」。
        #   两者的差别是真实的：用户可以用 `--stages` / 预设**显式固定**阶段
        #   （写作门那个测试就是这么做的）。显式不给 outline 阶段，却仍按
        #   大纲门去拦，等于**无视用户的明确指定** —— 而且会造出一条
        #   「用户根本没被要求写大纲、交付却永远过不去」的死路。
        #   形态推断出来的综述本来就带 outline 阶段（见 contract._ensure_review_stages），
        #   所以这么写不会漏掉真正的综述任务。
        if "outline" in (contract.stages or []):
            ok_o, note, info = self._check_outline()
            if not ok_o:
                reasons.append(note)
            elif note:
                warnings.append(f"[大纲] {note}")

        passed = not reasons
        warnings = warnings_extra + warnings
        return Verdict(passed=passed, reasons=reasons, warnings=warnings,
                       writing=writing_text,
                       lint=self.last_report.to_dict() if self.last_report else None,
                       feedback=self._feedback(passed, reasons, warnings, claim,
                                               contract=contract,
                                               writing=writing_text))
    # ── 大纲门 ──────────────────────────────────────────────
    #: 章标题行：Markdown 的 `#`/`##`/`###`，或「第X章」「一、」这类编号行。
    _RE_OUTLINE_HEAD = re.compile(
        r"^\s{0,3}(?:#{1,4}\s+\S|第[一二三四五六七八九十百\d]+[章节部分]\s*\S"
        r"|[一二三四五六七八九十]+\s*[、.．]\s*\S)", re.M)
    #: doc_id / DOI 形态。这条判据要回答「这一章有没有绑定文献」，不是「ID 对不对」，
    #: 所以取**宽**：判格式是 `verify_citation` 的活，不是这里的。
    #:
    #: ⚠️ 最初写成"`doi:` 前缀 + 后面整串"，实测漏掉了最常写的两种形态：
    #:   `doc_id: 10.1038/abc`（前缀是 doc_id 不是 doi）与裸 DOI。
    #:   而 `\b[0-9a-f]{8,}\b` 这类会**误吃** DOI 里的片段（`s41598-020-68112`），
    #:   看着命中了、其实没对上整条 DOI。⇒ 改成先找 DOI 的**真实形状**
    #:   （`10.\d{4,9}/<可见字符>`），再找 litkb 的 doc_id 形状。
    _RE_DOCID = re.compile(
        r"(?i)(?:10\.\d{4,9}/[^\s,，、;；)）\]】\"']+"
        r"|\bkb[0-9a-z]{6,}\b"
        r"|\b[0-9a-f]{8,}\b"
        r"|\b[a-z]{2,}[0-9]{3,}\b)")
    #: 每章至少要绑几篇文献。1 是这个判据的下限 —— 要求更多会误伤
    #: "这一章只做概念界定、引一篇综述即可"这种合法写法。
    _OUTLINE_MIN_REFS = 1

    def _check_outline(self) -> tuple[bool, str, dict]:
        """查工作区里的大纲。返回 `(是否过门, 问题说明, 统计)`。

        ★ 为什么按文件名模糊找而不是要求固定叫 `outline.md`：
          契约里让模型"产出 outline.md"，但它完全可能叫 `大纲.md`、
          `03-大纲.md`。**要求精确文件名会把干对了的事判成没干**（这仓反复
          付过这种代价）。所以按「文件名里带 outline 或 大纲」找。
        ★ 为什么"多份"只取最新那份：中途改过大纲是正常的，
          旧版留在盘上不该被当成问题。
        """
        try:
            root = Path.cwd()
        except Exception:
            return True, "", {}
        cands = []
        try:
            for p in root.rglob("*.md"):
                n = p.name.lower()
                if "outline" in n or "大纲" in p.name or "提纲" in p.name:
                    if p.is_file():
                        cands.append(p)
        except Exception:
            cands = []
        if not cands:
            return False, (
                "**没有找到大纲文件**。长综述必须先把结构冻结成文件再动笔 —— "
                "这是本任务契约要求的（见「大纲与章-文献映射」那段）。\n"
                "补的办法：装载 `paper-spine` 技能，按它的协议产出 `outline.md` 落盘。"
                "每章必须写清四样：① 一句话论证 ② 要引的 `doc_id` 清单 "
                "③ 本章目标字数 ④ 本章图表（不要图也要写理由）。\n"
                "为什么不能跳：写到第三节才发现第二节要改，会连带重写后文 ——"
                "来回改两轮，稿子就烂了。"), {}

        p = max(cands, key=lambda x: x.stat().st_mtime)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:                              # noqa: BLE001
            return True, f"大纲文件读不到（{type(e).__name__}），跳过内容核验：{p}", {}

        # 按章切开，逐章数 doc_id。
        # ★ 切片的**起点取标题行的行首、终点取下一标题的行首**（不是匹配的 end）——
        #   最初用 `m.start()` 让匹配吃掉了 `## ` 与标题文字，于是「第一章」
        #   那一段里装的是第一章正文 **加上第二章的标题**，数出来的是第二章的
        #   doc_id。实测症状：两章都写了 doc_id，却报「2 章没有绑定文献」。
        # ★ 另外要**把文档标题摘出去**：`# 综述大纲` 这种一级标题是文件名，
        #   不是一章。最初没摘，于是每一份规范写法的大纲都稳定多出一个"0 篇文献
        #   的章"，门永远打不开 —— 那是"不可能通过的门"，比没有门更糟。
        spans = [(m.start(), m.end(), m.group(0)) for m
                 in self._RE_OUTLINE_HEAD.finditer(text)]
        # 摘掉"文档标题"：`# 综述大纲` 是一级标题，通常是**文件名**而不是一章。
        # 判据：一级标题（`# ` 而非 `## `）**且**拿掉它之后还剩 ≥2 个标题 ——
        # 那说明真正分章的是别的层级，这一条只是文档名。
        # ⚠️ 两个必须同时成立的原因：一份只写了 `# 绪论\n# 机制` 的大纲
        #    也是合法写法（有人不用 `##`），那种情况下一级标题**就是**章，
        #    摘掉就把整份大纲判成空的了。
        _h1 = [sp for sp in spans
               if sp[2].lstrip().startswith("# ")
               and not sp[2].lstrip().startswith("##")]
        if _h1 and len(spans) - len(_h1) >= 2:
            spans = [sp for sp in spans if sp not in _h1]
        idx = [a for a, _, _ in spans]
        per_chapter: list[dict] = []
        for i, (s, e_head, _raw) in enumerate(spans):
            e = idx[i + 1] if i + 1 < len(idx) else len(text)
            body = text[s:e]
            refs = set(m.group(0) for m in self._RE_DOCID.finditer(body))
            title = body.splitlines()[0].strip()[:60] if body.strip() else ""
            per_chapter.append({"title": title, "refs": len(refs)})
        info = {
            "path": str(p),
            "chars": len(text),
            "chapters": len(idx),
            "chapters_with_refs": sum(1 for c in per_chapter
                                      if c["refs"] >= self._OUTLINE_MIN_REFS),
            "chapter_refs": per_chapter[:40],
        }

        if len(idx) < 2:
            return False, (
                f"大纲文件 `{p.name}` 里**读不出章标题**（只找到 {len(idx)} 个）。\n"
                "大纲要能用它来分章写 —— 没有章标题，后面就无从「按大纲逐章填充」。\n"
                "请把它写成 Markdown 标题结构（`## 第一章 …` 这种），"
                "每章下面写清 论证 / doc_id 清单 / 目标字数 / 图表。"), info

        bad = [c for c in per_chapter if c["refs"] < self._OUTLINE_MIN_REFS]
        if bad:
            names = "、".join(c["title"] or "(无标题)" for c in bad[:6])
            return False, (
                f"大纲里有 {len(bad)} 章**没有绑定任何文献**（{names}）。\n"
                "每一章都要写清「这一章引哪些 doc_id」—— 这是**动笔前**定的，"
                "不许留到写作时临时抓（临时抓到的往往是上下文里最眼熟的那几篇，"
                "不是最适合这一章的）。\n"
                "doc_id 从 `kb_coverage` / `kb_search` 的回包里**照抄**，不许凭记忆写。\n"
                "确实不引文献的章（如「本文结构安排」）也请写明该章不引文献的理由，"
                "并把理由写在该章标题下 —— 内核只核「有没有内容」，不核措辞。"), info

        return True, "", info

    # ── 取正文 ──────────────────────────────────────────────
    def _draft_text(self, claim: CompletionClaim) -> tuple[str, str]:
        """返回 (正文, 错误说明)。二者必有其一为空。"""
        t = (claim.draft_text or "").strip()
        if t:
            return t, ""
        p = (claim.draft_path or "").strip()
        if not p:
            return "", (
                "这是**写作任务**，但交付里既没 draft_text 也没 draft_path —— "
                "内核拿不到正文就做不了稿面体检（字数/结构/句长/套话/标点）。"
                "短稿（≤2000 字）直接填 draft_text；长稿写进工作区文件，"
                "再用 draft_path 指**相对本工作区**的路径。")
        path = Path(p)
        if path.is_absolute():
            try:
                path.resolve().relative_to(Path.cwd().resolve())
            except Exception:
                return "", (f"draft_path 指向工作区之外（{p}）。请改用相对路径，"
                            "把稿件写进本次任务的工作区里。")
        if not path.exists():
            return "", (f"draft_path={p} 指向的文件不存在。确认它是**相对本次任务工作区**"
                        "的路径，且确实写进去了（不要用绝对路径或 ../ 绕出去）。")
        try:
            return path.read_text(encoding="utf-8", errors="replace"), ""
        except Exception as e:
            return "", f"读取 draft_path={p} 失败：{type(e).__name__}: {e}"

    # ── 打回时对模型说的话 ──────────────────────────────────
    def _feedback(self, passed: bool, reasons: list[str], warnings: list[str],
                  claim: CompletionClaim, contract: TaskContract | None = None,
                  writing: str = "") -> str:
        if passed:
            base = ("证据门通过。证据链："
                    + "; ".join(f"#{e.call_seq} {e.tool} → {e.shows[:60]}"
                                for e in claim.evidence))
            if self.last_report is not None:
                base += "\n写作体检：" + self.last_report.brief()
            if warnings:
                base += ("\n\n（以下不影响判定，仅供你自己复核）\n"
                         + "\n".join(f"  · {w}" for w in warnings))
            return base
        lines = [
            "❌ 完成声明**未通过证据门**，不能算完成。",
            "",
            "问题：",
        ]
        lines += [f"  {i}. {r}" for i, r in enumerate(reasons, 1)]
        if writing:
            lines += ["", writing]
        if warnings:
            lines += ["", "另外（不是打回原因，仅供自查）："]
            lines += [f"  · {w}" for w in warnings]
        if contract is not None and contract.stages:
            lines += ["",
                      "⚠️ 本任务有**阶段专属技能**必须装载（内核强制，一个阶段只有一个）："]
            for sid in contract.stages:
                st = STAGE_BY_ID.get(sid)
                if st:
                    lines.append(f'  · {st.name:<10} → load_skill("{st.skill}")')
            lines += ["  凭记忆模仿不算数。装载完再重做该阶段，然后交付。"]
        lines += [
            "",
            "请按以下方式补正后再次调用 deliver_task：",
            "  · 证据必须是你**真实调用过**的工具，并带上它的 call_seq（见下方台账）",
            "  · evidence[].acceptance 要写清它对应契约里的哪一条验收标准",
            "  · 如果确实做不到，请诚实调用 report_blocker 说明卡在哪里，不要编造证据",
            "",
            "当前台账（可引用的调用）：",
            self.trace_digest(),
        ]
        return "\n".join(lines)

    def trace_digest(self, limit: int = 25) -> str:
        rows = self.registry.trace[-limit:]
        if not rows:
            return "  （本次任务还没有任何工具调用）"
        return "\n".join(
            f"  seq={c.seq:<3} {c.name:<22} {'ok ' if c.ok else 'ERR'} "
            f"{c.result[:90].replace(chr(10), ' ')}"
            for c in rows
        )
