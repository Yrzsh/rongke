# -*- coding: utf-8 -*-
"""导师视角：把「真导师」做成**判断标准**，而不是语气规定。

为什么单独开一层（2026-11-14）
------------------------------
`loop.mentor_mode_text()` 的 docstring 自己承认了：现在的导师模式「是一份工具
用法说明，不是一份人格设定」。它管的是**说得出处**（`mentor_trace_audit` 三条判据）
和**方法论技能索引**，但不管「这份东西本身站不站得住」。

2026-11-03 的裁决是明确删掉人格：用户原话「本质上还是个 agent……只是多加了
一些工具和 skill 调用」，实测故障是「你好」被答成一段组会点名。今天用户要
「像真的导师一样，以独特视角分析评价、指出潜在错误」——这是方向反转，但
**不能把那 3424 字加回来**：翻车的是「怎么说话」（语气/行数/结尾必须给下一步），
不是「按什么标准判断」。所以本模块只做三件与语气无关的事：

1. **立场**：默认 `NOT_READY`，写的人不评自己的稿，不许表演式附和。
2. **四个审查镜头**（这就是「独特视角」的可判定形式）：论断—证据 /
   方法学自洽 / 数字可复算 / 反例与边界。
3. **证据纪律**：每条 finding 必须能指到位置、给得出可核验证据；
   拿不准写 `UNVERIFIED`，**不许把「没查到」写成「错了」**。

借鉴 `ma-nucho-pro/supervisor-claude-plugin` 的 `skills/supervisor/SKILL.md`
（Iron law / judge output contract / DEGRADED_MODE），但**不照搬**它的多智能体
编排。需要说准的是**范围**：荣珂内核本身有真子代理（`rongke/subagent.py`，
隔离上下文、真跑工具），所以不是「没有多智能体能力」；但**导师评审这条链**
只跑一次性模型调用、单一视角 —— 一次调用就如实说一次，不许自称
「评审团已跑过」「多位独立评审一致认为」。口径见 `review_audit` 的 R8。

同构范式（与 svgplot / typeset / pathway 一致）：**内核算可复算的部分，
模型只做判断**。疑点由 `material_issues()` 先从原表和原文算出来，再交给导师，
而不是让模型凭印象找错。
"""
from __future__ import annotations

import csv
import io
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

STANCE_VERSION = 1
STANCE_NAME = "审稿席上的导师"
DEFAULT_VERDICT = "NOT_READY"

IRON_LAW = (
    "初稿不是终稿：先假设这份东西里有还没被发现的错，任务是把它们找出来，不是让用户开心。",
    "写的人不评自己：你只评、只指路，不在评审里顺手把稿子改了。",
    "没有能指到位置的证据，就不许下结论：拿不准写 UNVERIFIED，不许把「没查到」写成「错了」。",
    "只报高信号问题：宁可少报也不误报，凑数的意见会让人不再信你。",
    "不许表演式附和：不用「总体不错，但是…」开场，也不给「建议进一步完善」这种没有信息量的话。",
)

#: 四个审查镜头 = 「独特视角」的可判定落地。key 单源，提示词与判据共用。
LENSES: tuple[tuple[str, str, str], ...] = (
    ("claim_evidence", "论断—证据对齐",
     "每个实质断言能否指到材料/文献/原表？指不到的写 UNVERIFIED，别用套话把洞填上。"),
    ("methodology", "方法学自洽",
     "结论有没有超出数据撑得住的范围？相关不等于因果、有没有重复与对照、外推到哪一步就站不住。"),
    ("numbers", "数字与图表可复算",
     "正文数字能否由原表重算？图题/轴/单位/变量是否与正文说的是同一件事。"),
    ("counterexample", "反例与边界",
     "什么情况下这个结论会错？如果只能改一件事，改哪一件 —— 给最致命的那条，不按章节顺序罗列。"),
)

#: 两个**预演**镜头（2026-11-14 补，用户点名的那两个真实场景）。
#:
#: 用户原话：「组会汇报前总会先找人评价一下自己的汇报，又或者加深自己对课题的
#: 理解，因为开题需要你对自己的课题理解的非常清楚，包括一些细枝末节，防止被
#: 答辩老师问住」。这句话点的是**两个不同的东西**，不能挤进同一个镜头：
#:
#:   · `group_meeting` 是**会前的质控**：明天上午要讲，现在还有哪里会被人当面
#:     问住 —— 交付物是一份「会被问到什么、你答得怎么样」的清单。
#:   · `defence` 是**理解的验收**：不问「你讲得好不好」，问「你自己到底懂不懂」。
#:     开题/答辩要的不是话术，是细枝末节都清楚，所以它以**追问**为主，允许一路
#:     往下挖到你答不出来为止，并把「你答不出的那一层」本身当成产出。
#:
#: 两个镜头都遵守同一条证据纪律：**不能凭空编问题**。预判的问题要能指到
#: 这份材料的某一行/某一句，或者指到「本材料该有而没有」的那一块。
REHEARSAL_LENSES: tuple[tuple[str, str, str], ...] = (
    ("group_meeting", "组会预演：会被当面问住的地方",
     "站在台下那个想挑你毛病的人的位置看这份汇报：他会先问哪三句？"
     "每一句都要能指到你的某一页/某一句，并说明你现在的材料里有没有答案。"),
    ("defence", "开题/答辩预演：有没有你真答不上来的",
     "不要评措辞、不要评讲得好不好，只查「你到底懂不懂」：为什么是这个物种/方法/对照，"
     "关键参数怎么定的，与最接近的工作差在哪，哪一步失败过、为什么。"
     "一路往下追问，直到问出你答不出来的那一层 —— 那一层才是这次的产出，别替它圆场。"),
)


#: 问句形态：句末问号，或以「什么/为什么/怎么/哪/是否/有没有…」开头。
#: 判「这条是不是一个‘会被问到的问题’」用它，而不是看 `question` 字段 ——
#: 字段在 `[P0]` 写法下天然为空，拿它当判据会永远判绿。
_QUESTION_OPENERS = ("为什么", "怎么", "如何", "什么", "哪", "是否", "有没有",
                     "能否", "是不是", "谁", "多少", "凭什么", "如果", "假如")
_RE_HOW_MANY = re.compile(r"^[（(【\[]?\s*(Q\d+|P[0-4]|\d+[.、])\s*[）)】\]]?\s*")


def _looks_like_question(s: str) -> bool:
    t = _RE_HOW_MANY.sub("", str(s or "")).strip()
    if not t:
        return False
    return t.rstrip().endswith(("?", "？")) or t.startswith(_QUESTION_OPENERS)


def is_rehearsal(lenses: "set[str] | None") -> bool:
    """这一轮是不是「预演」（会前质控 / 理解验收），而不是交稿前终审。

    为什么要区分：两者的 PASS 门槛不同 —— 预演的产物本来就是「问题清单」，
    0 条会让人以为「没问题」，那是最坏的一种假安心；而终审的 PASS 是真结论。
    """
    return bool(set(lenses or set()) & {k for k, _, _ in REHEARSAL_LENSES})


#: 「我马上要讲/要答辩」的话语信号。**窄**：只收会前/答辩前那一刻的说法，
#: 不收「写了一篇论文」「汇报一下进展」这种普通任务 —— 那些不该进预演。
#: 与 `_DECK_WORDS` 那次教训同源（词表要窄，否则「汇报一下」被当成要幻灯片）。
#:
#: ★★ 2026-11-15 真用户跑出来的一条：旧表里有一个**裸的 `组会`**，于是
#:   「把我导师的批注和**组会记录**存下来」——一个整理资料的任务 ——
#:   被当成了「我要上讲台」，于是给了一段讲台立场段，回包还被按
#:   P0/R1-R12 的**评审格式**去盘，并给用户附上一段「这一份现在还交不出去」。
#:   用户要的是一份档案，不是评语 —— 那段尾巴对他是纯噪声，而且让人以为自己交的东西没过。
#:
#:   修法与本仓一贯的宽度纪律一致（同 `_DECK_WORDS`）：**不是只删一个词，是把「组会」
#:   从「出现就算」降为「必须和『上台 / 被问 / 时间』同时出现」**。
#:   因为「组会记录」「组会发言」「上次组会说的」都是名词，而真正的信号是
#:   「明天组会**要讲**」「组会**轮到我**」「组会**被问**」。
_RE_REHEARSAL = re.compile(
    # 组会类：必须带「要讲 / 轮到我 / 被问 / 汇报 / 预演 / 时间」之一，裸词不算。
    r"组会\s{0,4}(要|要向|向|轮到|发言|预演|被问|汇报完|今天|明天|后天|下周|周三|周四|周五)|"
    r"(今天|明天|后天|下周|周一|周二|周三|周四|周五|明早)\s{0,3}组会|"
    r"预讲|试讲|讲稿|讲一遍|要讲|明(天|早).{0,6}(讲|汇报)|"
    r"答辩|预答辩|盲审前|评委|专家会|"
    # ★★ 2026-11-15：裸的「开题」也要降宽 —— 「我明年开题」是**时间点**，
    #   「帮我把大纲定下来」是一件事；把两者合成“你要上讲台”就会把
    #   建大纲的任务当成开题预演。要么是“开题”直接带着被问/预演/帮我看/快到了，
    #   要么是“开题答辩”这种已经在讲台上的说法。
    r"开题\s{0,4}(答辩|预演|被问|问什么|会问|"
    r"帮我看|快到|在即|前夕|当天|明天|下周|这周|下月)|"
    r"(还没|没)\s{0,2}开题|开题在即|"
    r"被(问|老师问|评委问|问到)|问住|问倒|答不(上来|出)|"
    r"(模拟|预演)(一下|答辩|提问|组会)|挑(挑)?刺|提前(问|想)")


#: 「理解课题 / 开题准备」的话语信号（2026-11-16 补，用户点名的主用途）。
#:
#: 实测缺口：用户说「深入理解课题，应付开题」「帮我吃透这个课题，开题老师会问什么」
#: 「开题准备：帮我过一遍细枝末节」时，旧词表一个都不认 —— 拿到的是「先要材料」
#: 短立场段，方向错了：他不是要递稿子被评，是要**被追问课题本身**。这正是
#: `defence` 镜头的日常形态（它的描述本来就是「你到底懂不懂」）。
#:
#: 宽度纪律与 `_RE_REHEARSAL` 同源，每条都配负控：
#:   · 「理解」必须带**课题宾语** —— 「理解一下这篇文献/这段话」不算；
#:     「课题组」是研究组不是课题，`(?!组)` 挡掉。
#:   · 「开题准备」是准备动作，「开题准备**材料**」是名词（同「组会记录」那次），
#:      lookahead 挡掉材料/清单/文档/资料。
#:   · 「明年开题」是时间点，仍然不算（旧负控钉着）。
_RE_TOPIC_STUDY = re.compile(
    r"(理解|吃透|搞懂|弄懂|梳理|拆解).{0,6}(课题(?!组)|本题|这个题|研究方向|实验方案|核心逻辑|研究脉络)|"
    r"(应付|备战|迎接|准备)开题|"
    r"开题(准备|备战)(?![材清文资])|"
    r"为开题|"
    r"(开题|答辩)(老师|评委|专家|委员会)?(会问|问什么|问哪些|可能问|常问)|"
    r"(追问|考考)我.{0,12}(课题|方案|实验|开题|原理)")


def is_topic_study_text(user_text: str) -> bool:
    """用户这句话是不是在说「我要吃透课题 / 我在为开题做准备」。"""
    return bool(_RE_TOPIC_STUDY.search(str(user_text or "")))


_RE_PROJECT_GUIDE = re.compile(
    r"(指导|推进|诊断|梳理|把脉|把关).{0,6}(课题(?!组)|项目|实验进展|下一步)|"
    r"(课题|项目).{0,6}(怎么推进|下一步|瓶颈|卡住|进度)")


def is_project_guide_text(user_text: str) -> bool:
    """用户这句话是不是在请导师指导/推进课题项目。"""
    return bool(_RE_PROJECT_GUIDE.search(str(user_text or "")))


def is_rehearsal_text(user_text: str) -> bool:
    """用户这句话是不是在说「我要上讲台了 / 我要被问了 / 我要吃透课题」。"""
    g = str(user_text or "")
    return bool(_RE_REHEARSAL.search(g) or _RE_TOPIC_STUDY.search(g))


#: 「计划/排后续实验」的话语信号。**窄**：动词必须带着实验宾语，
#: 「写一篇论文」「画一张图」「汇报进展」都不算。
_RE_EXP_PLAN = re.compile(
    r"(计划|规划|安排|设计).{0,6}(实验|试验)|"
    r"排(?!版).{0,2}(实验|试验)|"
    r"(后续|接下来|下一步|后面).{0,6}(实验|做什么|干什么)|"
    r"(差|缺|少).{0,4}(什么|哪些|多少).{0,2}数据|"
    r"数据.{0,4}(缺口|还差|够不够)")


def is_experiment_plan_text(user_text: str) -> bool:
    """用户这句话是不是在说「帮我规划实验 / 看看还差什么数据」。"""
    return bool(_RE_EXP_PLAN.search(str(user_text or "")))


#: 课题工作的两条路（**单源**：提示词从这里取，判据也钉这里）。
#: 为什么必须写进提示词而不是只靠工具描述：2026-11-15 实测 ——
#: 「工具说明再详细也没用，模型走的是提示词指向的地方」。outline 三件套
#: 造好之后，导师模式提示词里一个字都没提它们，于是「计划后续实验」
#: 拿到的还是泛泛建议。这两段只在命中对应意图时注入（不稀释别的问话）。
_PLAN_ROUTE = "\n".join([
    "═══ 本轮走「规划实验」这条路（先对账，再开口）═══",
    "① 先 `outline_status` 对账：`starved` 的节就是**现在写不出来**的节，"
    "`unverified` 是自称有数据但台账核不上。这些结论由盘上两份东西复算，转述它，别另推一套。",
    "② 还没有大纲 ⇒ 先 `outline_save` 把「章节 → 每节要什么数据 → 哪个实验供」钉下来"
    "（不要替用户编实验，说不清要测什么就问）。",
    "③ 缺口用 `outline_to_lab` 排成实验 todo —— 它**只排 todo，不造数据**；"
    "真数据必须由 `lab_record(kind=\"measure\")` 记。",
    "④ 设计细节（对照 / 重复 / 样本量 / 混杂）先 `load_skill(\"experimental-design\")` / "
    "`load_skill(\"statistical-power\")` 再开口；参数只从 `lab_method` / 库原文取，不凭常识补。",
])
_STUDY_ROUTE = "\n".join([
    "═══ 本轮走「理解课题」这条路（开题/答辩前的理解验收）═══",
    "① 先把课题现状读全：`topic_status`（PLAN / 卡点 / 下一步）、"
    "`outline_status`（哪节缺数据）、`lab_report`（真做了什么、有没有测量）。",
    "② 然后按 `defence` 镜头往下追问，**不是讲解一遍课题**："
    "为什么是这个物种/方法/对照、关键参数怎么定的、与最接近的工作差在哪、哪一步失败过为什么。",
    "③ 每条追问指到课题材料的真实位置（PLAN/OUTLINE 的某一行、对账里的某条缺口、"
    "台账里「0 条测量」这个事实），并写清他现在能不能答。",
    "④ 答不上的那一层就是开题前要补的洞 —— 如实列出来，别替他圆场，也别顺手把答案写了。",
])


_GUIDE_ROUTE = "\n".join([
    "═══ 本轮走「导师指导项目」这条路（先看课题实况，再定推进路线）═══",
    "① 先调 `topic_status`、`outline_status`、`lab_report` 读出当前阶段、实验进度/逾期排期、未闭环卡点与数据缺口。",
    "② 结合课题现状给出可执行的推进路线：最堵的瓶颈（引用真实卡点/逾期项/缺口）、"
    "本周最优先的 1-2 个动作（用 `lab_record` 记进待办或排期）、以及风险兜底方案。",
    "③ 若用户刚提供了新实验计划，直接用 `lab_plan` 或 `lab_record` 结构化落盘并排好后续跟进日程。",
])


def topic_route_text(goal: str) -> str:
    """按意图给「本轮走哪条路」的指路段。认不出 ⇒ 空串（不注入）。"""
    g = str(goal or "")
    parts = []
    if is_experiment_plan_text(g):
        parts.append(_PLAN_ROUTE)
    if is_topic_study_text(g):
        parts.append(_STUDY_ROUTE)
    if is_project_guide_text(g):
        parts.append(_GUIDE_ROUTE)
    return "\n\n".join(parts)

#: 「独特视角」不靠形容词，靠这三问（借 supervisor 的 Devil's Advocate 五问，
#: 取与科研文稿最贴的三条）。写进提示词，也由判据钉住，防止又退化成通用套话。
DEVIL_QUESTIONS = (
    "所有人（包括作者和我）共享了哪个没被说出口的假设？",
    "交付之后最先被审稿人/用户发现的会是什么？",
    "要出什么事，才能证明这份东西现在还不该交？",
)

#: 只禁「无信息量」与「假证据」，**不管语气、行数、结尾格式**（11-03 的教训）。
VAGUE_RE = re.compile(
    r"总体(不错|可以|挺好)|建议(进一步)?(完善|优化|改进)|看起来没(问题|大碍)|"
    r"整体(尚可|良好)|needs polish|looks good|需要再打磨", re.I)
FAKE_EVIDENCE_RE = re.compile(
    r"(多个|多位|独立)评审|评审团|已经?(验证|测试|复核)过|"
    r"should work|probably works|fully verified", re.I)
HEDGE_RE = re.compile(r"应该(是|能|会)|大概是|可能已经|估计|也许是")

_RE_NUM = re.compile(r"(?<![A-Za-z_.])\d+(?:\.\d+)?")
# 真 LLM A/B（2026-11-14）暴露：只认 `[P1]` 会把模型自然写出的 `【P0】`
# 整批丢掉 —— 明明指出了「温度列根本不存在」却判成 0 条 finding。
# **修的是解析器能吃什么形式，不是放宽判据**（七字段/证据/位置一条没松）。
#
# ★ 2026-11-14 补：预演回包用 `[Q1] / 【问1】 / 【追问1】`。group 1-3 = P 标号，
#   4-6 = Q 标号，7 = 正文。P 与 Q **分开取**，因为它们的语义完全不同：
#   P 是「多严重」，Q 是「这是他会问的第几句」。混进同一个 group 会让 R6
#   （PASS 不许挂 P0/P1）在预演回包上误判。
_RE_FINDING = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?"
    r"(?:\[(P[0-4])\]|【(P[0-4])】|\((P[0-4])\))"
    r"(?:\*\*)?\s*[:：]?\s*(.+?)\s*$")
_RE_REHEARSAL_ITEM = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?"
    r"(?:\[(Q\d+)\]|【(Q\d+|问\s*\d+|追问\s*\d+|追问[一二三四五六七八九十]+)】|"
    r"\((Q\d+)\))"
    r"(?:\*\*)?\s*[:：]?\s*(.+?)\s*$")
_RE_FIELD = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?"
    r"(镜头|位置|证据|为什么重要|怎么改|怎么答|复核办法|未核实|问题)\s*[:：]\s*(.*)$")
_RE_LOC_QUOTE = re.compile(r"[「\"“']([^」\"”']{2,60})[」\"”']")
# 「第 5–6 行」「第 5、6 行」也是可定位的位置，别因为区间写法判成不可定位。
_RE_LOC_LINE = re.compile(r"第\s*(\d+)\s*(?:[–\-—~至、,，]\s*\d+\s*)?行")
_RE_STRONG = re.compile(
    r"证明|证实|确立了|必然(导致|是)|机制(就)?是|显著优于|因果(关系|结论)|"
    r"可以断定|毫无疑问")
_RE_SOFT = re.compile(r"可能|提示|推测|或许|有待|尚需|相关|在.*条件下|初步")
_RE_CITE = re.compile(r"〔库·[^〕]+〕|\[\d+\]")

FIELDS = ("镜头", "位置", "证据", "为什么重要", "怎么改", "复核办法", "未核实")


# ══════════════════ 一、什么时候才启动「导师视角」 ══════════════════

#: 「成篇材料」的门槛。**按字数，不按有没有换行**。
#:
#: ★ 2026-11-14 补修（组会/开题场景实测）：原判据是
#:   `len(d) >= 120 or "\n" in d or "|" in d`，而 `loop.initial_messages()`
#:   传进来的第一件 artifact 就是 **goal 本身**（`[_goal] + _brief["sources"]`）。
#:   ⇒ `"\n" in goal` 恒真 ⇒「在吗\n问个问题」也被判成「递了成篇材料」，
#:   立场段被注入，11-03 的「你好 → 组会点名」以另一种形状长回来。
#:   换行是**排版**，不是篇幅；一件 200 字的稿子和 200 行的一句话请求
#:   在「有没有东西可评」这件事上完全不是一回事。
_ARTIFACT_MIN_CHARS = 60


#: 「用户喜不喜欢写长句子」不是成篇材料。
#: ★★ 2026-11-15 真实使用跑出来的：`_artifact_like` 只看长度 ≥ 120 字，而用户
#:   一句话交代课题就能写到 65 字 —— “我要做 X 课题、明年开题、帮我把大纲定下来”
#:   被当成了“递了成篇材料”，于是通常要评审的立场段下去，回包被按 P0/R1-R12
#:   的格式盘一遍，末尾附上一段「这一份现在还交不出去」。用户要的是一份大纲。
#:   长度是个很差的代理：**成篇材料是多行的，一句话交代需求是一行的。**
#:   所以长度与多行都要满足。实测：真课题目标 65 字 / 1 行 ⇒ 不算材料；
#:   一段 300 字的摘要、一张表、一份 PDF 转的 Markdown ⇒ 算。
_ARTIFACT_MIN_LINES = 2


def _artifact_like(d: str) -> bool:
    """一件东西算不算「成篇材料」。

    ★ 两个条件**与**：够长 且 多行。只看长度会把用户的一句话当成稿子（见上面那条真实故障）。
    表格行（`|` ≥ 2）仍然单独认：一张表本身就可评，不管多短。
    """
    lines = [x for x in str(d or "").splitlines() if x.strip()]
    if any(line.count("|") >= 2 for line in lines):
        return True
    return len(d) >= _ARTIFACT_MIN_CHARS and len(lines) >= _ARTIFACT_MIN_LINES


def should_review(user_text: str, artifacts: list[str] | None = None) -> dict:
    """**行为触发**，别让「你好」又被答成组会点名（11-03 实测故障）。

    判据是「有没有可评审的对象」，不是「开关开没开」。只有两种情形启动：
      · 用户明确要评价/指导/挑错，且递了东西；
      · 用户没明说，但递来了成篇的稿子/数据/图（递东西过来就是要你看）。
    纯寒暄、纯概念提问、纯任务下达 ⇒ 正常作答，不注入评审视角。
    """
    goal = str(user_text or "").strip()
    docs = [str(a or "") for a in (artifacts or [])]
    has_artifact = any(_artifact_like(d) for d in docs)
    asks = bool(re.search(
        r"评价|评审|评一评|看看|帮我看|指导|指点|挑错|找(找)?(问题|错)|"
        r"哪里(有问题|不对|错了)|有没有(问题|错)|把关|审稿|导师|该怎么改|"
        r"站得住|靠不靠谱|有没有漏洞|挑(刺|毛病)|预演|模拟(答辩|提问|组会)|"
        r"会问|被问|怕(被)?问|答辩|开题", goal))
    # 「看一下 X 这个概念」不是评审；只有明确指向作品/产物才算
    concept_only = bool(re.search(r"是什么|什么意思|解释一下|介绍一下|怎么用", goal)) \
        and not has_artifact
    greeting = len(goal) <= 12 and not asks and not has_artifact
    # ★ 只递来**一句话的请求**（没有稿子）：不算可评审对象，但若他明确要评价，
    #   由 `build_stance_text(review=False)` 给一段更短的「先要材料」立场段 ——
    #   既不凭空开评审，也不至于把「帮我看看这段行不行」答成一句空话。
    review = has_artifact and not concept_only and not greeting
    return {
        "review": review,
        "sole_request": bool(asks) and not has_artifact and not concept_only,
        "reason": ("有可评审对象" if has_artifact else "没有可评审对象",
                   "明确要求评价" if asks else "未明确要求评价",
                   "寒暄/概念提问" if (greeting or concept_only) else "非寒暄"),
        "signals": {"has_artifact": has_artifact, "asks": asks,
                    "concept_only": concept_only, "greeting": greeting},
    }


# ══════════════════ 二、内核先算可复算的疑点 ══════════════════

def _r(v: float) -> float:
    return round(float(v), 1)


def _allowed_numbers(csv_text: str) -> set[float]:
    """从 CSV 原表独立重算「哪些数字说得通」，判据基准不依赖被测模型。"""
    try:
        rows = list(csv.DictReader(io.StringIO(csv_text)))
    except (csv.Error, OSError):
        return {0.0, 1.0, 2.0, 3.0, 4.0, 100.0}
    cols: dict[str, list[float]] = {}
    for row in rows:
        for k, v in row.items():
            try:
                cols.setdefault(k or "", []).append(float(str(v).strip()))
            except (TypeError, ValueError):
                continue
    return _allow_from_cols(cols)


#: Markdown 表的分隔行：`| --- | :---: |` / `---|---`
_RE_MD_SEP = re.compile(r"^\s*\|?[\s:|-]*-{3,}[\s:|-]*\|?\s*$")


def _is_md_table_line(ln: str) -> bool:
    return ln.count("|") >= 2 or bool(_RE_MD_SEP.match(ln))


def _md_table_cells(text: str) -> list[list[str]]:
    """把 Markdown 表拆成「每个数据行 → 单元格列表」（表头与分隔行剔除）。

    存在的理由（组会/开题场景实测）：讲稿、汇报大纲、开题报告里那张「原表」
    几乎总是 Markdown 表，而不是 CSV。`material_issues` 原来只吃
    `csv_text`，于是**表里的数一个都照不进来** —— 正文写「中段抬升至 45%」
    不会被判查无出处，而立场段却照样宣称「内核已按原表算出疑点」。那张
    「已算出」的空头支票比不说更糟：模型会据此跳过真正的核数。
    """
    rows: list[list[str]] = []
    lines = str(text or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.count("|") < 2 or _RE_MD_SEP.match(ln):
            continue
        # 下一行是分隔行 ⇒ 本行是表头，不进数据
        if i + 1 < len(lines) and _RE_MD_SEP.match(lines[i + 1]):
            continue
        rows.append([c.strip() for c in ln.strip().strip("|").split("|")])
    return rows


def _allow_from_cols(cols: dict) -> set[float]:
    """**口径单源**：CSV 与 Markdown 表共用这一份描述统计白名单。

    两处各写一遍就是 H 型缺陷（改一处忘一处），而这份白名单是承重判据
    （R9 靠它认「内核算出的真疑点」），两份副本迟早给出两个答案。
    """
    allow: set[float] = {0.0, 1.0, 2.0, 3.0, 4.0, 100.0}
    for vals in cols.values():
        if len(vals) < 2:
            continue
        for v in vals:
            allow.add(_r(v))
        mean, sd = statistics.fmean(vals), statistics.stdev(vals)
        steps = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
        k = max(1, len(vals) // 3)
        for v in (min(vals), max(vals), mean, sd, vals[0], vals[-1],
                  vals[-1] - vals[0],
                  (vals[-1] - vals[0]) / vals[0] * 100 if vals[0] else 0,
                  len(vals), len(vals) - 1, k,
                  sum(1 for d in steps if d > 0), sum(1 for d in steps if d < 0)):
            allow.add(_r(v))
        for d in steps:
            allow.add(_r(abs(d)))
        for seg in (vals[:k], vals[k:2 * k], vals[2 * k:]):
            if seg:
                allow.add(_r(statistics.fmean(seg)))
    return allow


def _allowed_numbers_md(text: str) -> set[float]:
    """Markdown 表版本的可复算白名单，逐列重算（口径同上）。"""
    cols: dict[int, list[float]] = {}
    for cells in _md_table_cells(text):
        for j, cell in enumerate(cells):
            try:
                cols.setdefault(j, []).append(float(cell))
            except (TypeError, ValueError):
                continue
    return _allow_from_cols(cols)


def material_issues(text: str, *, csv_text: str = "",
                    known_lines: int = 0) -> list[dict]:
    """内核能**自己算出来**的疑点 —— 交给导师当证据锚，不靠它凭印象找错。

    三类：数字查无出处（硬）/ 强断言越界（软，标 unverified）/ 引注无出处表（软）。
    每条都带 `line` 与 `evidence`，所以后面能判「导师有没有真的看这一处」。

    ★ 复算锚有**两个来源**：独立的 CSV，以及**正文里的 Markdown 表**
      （2026-11-14 补）。组会讲稿 / 汇报大纲 / 开题报告里的「原表」多半是
      Markdown 表，只认 CSV 等于对这类材料一个数都复算不了。
      表格行本身**不参与断言核验** —— 表里的数是定义，不是结论。
    """
    src = str(text or "")
    lines = src.splitlines() or [""]
    out: list[dict] = []

    md_allow = _allowed_numbers_md(src)
    has_md_table = bool(_md_table_cells(src))
    csv_allow = _allowed_numbers(csv_text) if csv_text.strip() else None
    allow = (md_allow | csv_allow) if csv_allow is not None else md_allow
    if has_md_table or csv_allow is not None:
        for i, ln in enumerate(lines, 1):
            # 参考文献/年份/编号行不进数字核验的分母
            if re.match(r"^\s*(\[|参考文献|#)", ln) or _is_md_table_line(ln):
                continue
            for tok in _RE_NUM.findall(ln):
                if _r(float(tok)) not in allow:
                    out.append({
                        "kind": "number_unverified", "lens": "numbers",
                        "severity": "P1", "line": i, "token": tok,
                        "evidence": f"第 {i} 行「{ln.strip()[:70]}」里的 {tok} "
                                    f"无法由原表或描述统计复算",
                        "unverified": True,
                    })

    for i, ln in enumerate(lines, 1):
        if _RE_STRONG.search(ln) and not _RE_SOFT.search(ln):
            out.append({
                "kind": "overreach", "lens": "methodology",
                "severity": "P2", "line": i, "token": "",
                "evidence": f"第 {i} 行「{ln.strip()[:70]}」用了确定性因果/机制措辞，"
                            f"但同句没有限定条件",
                "unverified": True,
            })

    cites = _RE_CITE.findall(src)
    if cites and not re.search(r"^\s*##\s*参考文献|^\s*\[1\]\s", src, re.M):
        out.append({
            "kind": "citation_without_list", "lens": "claim_evidence",
            "severity": "P2", "line": 0, "token": "",
            "evidence": f"正文出现 {len(cites)} 处引注，但全文没有文献表",
            "unverified": True,
        })
    if known_lines:
        out = [o for o in out if not o["line"] or o["line"] <= known_lines]
    return out


# ══════════════════ 三、注入 SYSTEM 的立场段（短，且不管语气） ══════════════════

def build_stance_text(known_issues: list[dict] | None = None,
                      *,
                      rehearsal: bool = False,
                      review: bool = True,
                      inline: bool = False) -> str:
    """生成导师视角段。**不写「怎么说话」、不规定行数与结尾**。

    三种形态，都只改**判断标准**，不改语气：

    | 形态 | 何时 | 内容 |
    |---|---|---|
    | 常规评审（默认） | 有稿子/数据，没说要预演 | 四镜头 + 三问 + finding 七字段 |
    | 预演（`rehearsal=True`） | 组会/开题/答辩前 | 四镜头 **+ 两镜头预演**，回包用 Q 标签 |
    | 只说了句请求（`review=False`） | 「帮我看看这段行不行」但没有材料 | 短：先要材料，不许凭空开评审 |

    `inline=True` 时内核算出的疑点写进提示词（初始注入用）；
    出口回调（追加到 `result.final`）必须传 `inline=False` —— 那里再列一遍
    就会把同一段疑点复制进答案正文，用户看到的是重复内容。
    """
    if not review:
        return "\n".join([
            f"═══ {STANCE_NAME}：他要你评，但**还没把东西递过来** ═══",
            "默认判定 NOT_READY。别凭空开评审，也别先夸一句再问。",
            "直接说清楚要他递什么（稿子/大纲/数据表/汇报的哪一页），",
            "以及拿到之后你会按哪几条看 —— 只报高信号的问题，不凑数。",
            "一次模型调用就是一次评审：不许自称「多位独立评审/评审团已跑过」。",
        ]) + "\n"

    head = [
        f"═══ {STANCE_NAME}（只在真有东西要评时生效）═══",
        "你坐在审稿席上，不是代笔。默认判定是 NOT_READY：",
        "证据齐了才放行，别先替这份东西辩护。",
        "铁律（是判断标准，不是说话方式）：",
        *[f"  · {x}" for x in IRON_LAW],
        "",
        "按这四个镜头看，别按章节顺序流水账：",
        *[f"  · `{k}` {name}：{ask}" for k, name, ask in LENSES],
        "",
    ]
    if rehearsal:
        head += [
            "★ 这一轮是**预演**（他要么明天要讲、要么马上要开题/答辩），"
            "所以再加两个镜头，而且**这两个是主角**：",
            *[f"  · `{k}` {name}：{ask}" for k, name, ask in REHEARSAL_LENSES],
            "",
            "预演的硬要求：",
            "  · 每条预判问题都要指到这份材料的**某一页/某一句**，"
            "不能是「老师可能会问你的创新点」这种放之四海皆准的话；",
            "  · 要写清他**现在能不能答**（材料里有答案 / 只有半句 / 根本没写）—— "
            "这正是他找你的原因；",
            "  · 开题/答辩那一侧，允许一路往下追问到**他答不出来**为止。"
            "把答不出的那一层如实说出来（判定 NOT_READY），别替他圆场、"
            "也别顺手替他把答案写了 —— 那正是他必须自己补的那部分。",
            "",
        ]
    head += [
        "最后一个镜头不许省，至少把这三问在心里过一遍：",
        *[f"  · {q}" for q in DEVIL_QUESTIONS],
        "",
    ]
    if known_issues and inline:
        head += ["内核已按原表/原文算出下面这些疑点，先核它们，再补你自己的判断："]
        for it in known_issues[:8]:
            head.append(f"  · [{it.get('severity', 'P2')}] {it.get('evidence', '')}"
                        + ("（未核实，别当成已定错误）" if it.get("unverified") else ""))
        head.append("")
    head += [
        f"回包用这个结构（每条都要能独立复核，不要只给「看起来不错」）：",
        "{【开题预演】/【组会预演】 或 【导师评审】}",
        f"判定：{DEFAULT_VERDICT} / PASS（有 P0/P1 就不许写 PASS）",
        ("审查面：<至少两个看过的镜头 key，逗号分隔>"
         + ("；预演时两个预演镜头必须都写" if rehearsal else "；PASS 也要写，证明真看过")),
        "最要紧的一条：<如果只能改一件事，改哪一件>",
    ]
    if rehearsal:
        head += [
            "[Q1] <会被当面问到的那一句，用他自己的口吻问出来>",
            "  镜头：<group_meeting / defence>",
            "  位置：<这份材料的第 N 行 / 原文短引文>",
            "  证据：<材料里现在有没有答案；有就引，没有就写「全文未出现」>",
            "  为什么重要：<答不上来会怎样>",
            "  怎么答：<一句话方向 + 要到哪份材料/哪篇文献找支撑>",
            "  复核办法：<补完之后怎么验 —— 搜什么词、看哪一节>",
            "  未核实：<没有就写 无>",
            "（问题可以多于一条；**只报真的会被问住的**，凑数的提问比不报更糟。）",
        ]
        head += [
            "  · `问题/怎么答/怎么改` 任何一种叫法都行，但**位置与证据必须有**。",
            "  · 「他会问创新点」这种没有位置的话不算，删掉。",
        ]
    else:
        head += [
            "[P0|P1|P2] <一句话说清问题>（判定 PASS 且确实无可指摘时可以不写 finding）",
            "  镜头：<key>",
            "  位置：<第 N 行 / 原文短引文>",
            "  证据：<可核验原文或表值；拿不准写 UNVERIFIED>",
            "  为什么重要：<影响>",
            "  怎么改：<具体动作>",
            "  复核办法：<独立可验的办法>",
            "  未核实：<没有就写 无>",
        ]
    head += [
        "  · `证据` 写可核验的原文/表值/行号；拿不准就写 UNVERIFIED。",
        "  · `位置` 写「第 N 行」或原文里的短引文，必须是这份稿子里真有的。",
        "  · `未核实` 没东西就写「无」。不许把没查到写成「编造/错误」。",
        "  · 一次模型调用就是一次评审：不许自称「多位独立评审/评审团已跑过」。",
    ]
    return "\n".join(head) + "\n"


# ══════════════════ 四、结构化 finding 与读回校验 ══════════════════

@dataclass
class Finding:
    severity: str = ""
    issue: str = ""
    lens: str = ""
    location: str = ""
    evidence: str = ""
    why_it_matters: str = ""
    recommended_fix: str = ""
    acceptance_test: str = ""
    unverified: list[str] = field(default_factory=list)
    #: 预演条目的标号（`Q1` / `追问2`）。**不是严重度** —— 预演里没有「多严重」
    #: 这一维，只有「这是他会问的第几句」。留空表示这是一条常规 finding。
    question: str = ""

    @property
    def is_question(self) -> bool:
        return bool(self.question.strip())

    def missing(self) -> list[str]:
        pairs = (("镜头", self.lens), ("位置", self.location),
                 ("证据", self.evidence), ("为什么重要", self.why_it_matters),
                 ("怎么改", self.recommended_fix), ("复核办法", self.acceptance_test))
        return [k for k, v in pairs if not str(v or "").strip()]


def parse_findings(text: str) -> tuple[str, list[Finding]]:
    """零依赖解析回包。**结构化对象**，不是字符串包含判据。

    ★ 2026-11-14 补：预演回包用 `[Q1] / 【问1】 / 【追问1】` 标号（「这是他会
      问的那句」），字段叫「问题 / 怎么答」。**只扩解析器能吃什么形式，
      七字段、位置、证据这三条判据一条没松** —— 同 11-14 早前 `【P0】` 那次更正。
    """
    verdict = ""
    items: list[Finding] = []
    cur: Finding | None = None
    for raw in str(text or "").splitlines():
        m = re.search(r"判定\s*[:：]\s*(\S+)", raw)
        if m and not verdict:
            verdict = m.group(1).strip().upper()
        fm = _RE_FINDING.match(raw)
        if fm:
            sev = next((g for g in fm.groups()[:3] if g), "")
            cur = Finding(severity=sev, issue=fm.group(4))
            items.append(cur)
            continue
        qm = _RE_REHEARSAL_ITEM.match(raw)
        if qm:
            idx = next((g for g in qm.groups()[:3] if g), "")
            # 预演的标号不是严重度（它是「第几句追问」），所以 severity 留空；
            # 标号放进 `question`，写回提示与界面都用它，别和 P0 混成一档。
            cur = Finding(question=idx, issue=qm.group(4))
            items.append(cur)
            continue
        if cur is None:
            continue
        fd = _RE_FIELD.match(raw)
        if not fd:
            continue
        key, val = fd.group(1), fd.group(2).strip()
        if key == "问题" and val and not cur.issue:
            cur.issue = val
        elif key == "镜头":
            cur.lens = val
        elif key == "位置":
            cur.location = val
        elif key == "证据":
            cur.evidence = val
        elif key == "为什么重要":
            cur.why_it_matters = val
        elif key in ("怎么改", "怎么答"):
            cur.recommended_fix = val
        elif key == "复核办法":
            cur.acceptance_test = val
        elif key in ("未核实", "拿不准") and val and val not in ("无", "无。", "-", "—"):
            cur.unverified.append(val)
    return verdict, items


def _location_ok(loc: str, source_text: str) -> bool:
    """位置必须能在这份稿子里定位：行号在范围内，或引号片段真的出现。"""
    m = _RE_LOC_LINE.search(loc)
    if m:
        n = int(m.group(1))
        return 1 <= n <= max(1, len(source_text.splitlines()))
    q = _RE_LOC_QUOTE.search(loc)
    if q:
        return q.group(1) in source_text
    return any(k in loc for k in ("§", "节", "图", "表", "摘要", "全文")) \
        and bool(source_text.strip())


def validate_findings(findings: list[Finding], *, source_text: str = "",
                      known_issues: list[dict] | None = None,
                      independent: bool = False, verdict: str = "",
                      lenses_seen: set[str] | None = None,
                      rehearsal: bool | None = None) -> dict:
    """读回校验：没有证据的意见不算意见。

    PASS 允许 0 条 finding（不许逼着导师编问题），但仍要写「审查面」证明真看过。

    ★ 预演（`rehearsal=True`）改的是 **R12 一条**，不是松判据：
      预演的产物本来就是问题清单，PASS / 0 条会让人误以为「没问题」——
      那是这类任务里最坏的一种假安心。所以预演必须有至少一条**答案形态**的问题；
      位置、证据、为什么重要、怎么答、复核办法一条都不少。
    """
    seen = set(lenses_seen or set()) | {f.lens for f in findings if f.lens}
    reh = is_rehearsal(seen) if rehearsal is None else bool(rehearsal)
    checks = {
        "R1_有结构化 finding 或明确 PASS": bool(findings) or verdict == "PASS",
        "R2_每条七字段齐全": all(not f.missing() for f in findings),
        "R3_每条位置可在这份稿子里定位":
            all(_location_ok(f.location, source_text) for f in findings),
        # R2 管「有没有」，R4 只管「是不是猜测」—— 缺证据只该翻 R2，
        # 否则一条负控同时翻两条，就没有分辨率了。
        "R4_证据不是猜测或假验证":
            all(not f.evidence.strip()
                or (not HEDGE_RE.search(f.evidence)
                    and not FAKE_EVIDENCE_RE.search(f.evidence))
                for f in findings),
        "R5_没有无信息量套话":
            all(not VAGUE_RE.search(f.issue) for f in findings),
        "R6_判定与严重度一致": True,   # 由 review_audit 结合 verdict 填
        "R7_至少覆盖两个审查镜头": len(seen) >= 2,
        "R8_不冒充独立评审": True,     # 由 review_audit 结合全文填
        "R9_内核算出的真疑点被指到": True,  # 有效性判据，下面单独量
        # R11 管的是「**以问句形式**写出来的条目不许挂 P 档」：`[P0] 中段的 45% 是
        # 哪几批的均值？` 把「多严重」和「这是第几句追问」混成一档，读的人没法
        # 判预演的产出到底有几问。判「是不是问题」看**句子本身**（问号 / 问句
        # 开头），不看 `question` 字段 —— 那个字段在 `[P0]` 写法下天然是空的，
        # 拿它当判据等于永远判绿（我第一版就是这么写的，负控立刻打红）。
        "R11_问题条目用 Q 标号而不是 P 档": all(
            not (f.severity and _looks_like_question(f.issue)) for f in findings),
        # R12 管「有没有产出问答形态」—— 问句形态的条目也算，不等 `question` 字段。
        # 这样一条回包只会在 R11（标号用错）或 R12（根本没问）里翻**一条**，
        # 定向负控才有分辨率（两条一起翻等于宣布「标号错了就当没写问题」）。
        "R12_预演必须真的给出会被问住的问题":
            (not reh) or (bool(findings) and all(
                f.is_question or _looks_like_question(f.issue) for f in findings)),
    }
    hard = [it for it in (known_issues or [])
            if it.get("kind") == "number_unverified"]
    if hard:
        covered = 0
        for it in hard:
            tok = str(it.get("token") or "")
            line = int(it.get("line") or 0)
            if any((tok and tok in (f.evidence + f.location + f.issue))
                   or (line and f"第 {line} 行" in f.location
                       or f"第{line}行" in f.location)
                   for f in findings):
                covered += 1
        checks["R9_内核算出的真疑点被指到"] = covered >= 1
    return {"checks": checks, "n_findings": len(findings),
            "n_lenses": len({f.lens for f in findings if f.lens}),
            "n_questions": sum(1 for f in findings if f.is_question),
            "missing_by_item": [f.missing() for f in findings],
            "rehearsal": reh, "independent": independent}


def review_audit(reply: str, *, source_text: str = "", user_text: str = "",
                 known_issues: list[dict] | None = None,
                 independent: bool = False) -> dict:
    """一次评审回包的完整读回。缺件一律记红，不静默跳过。"""
    trigger = should_review(user_text, [source_text] if source_text else [])
    verdict, findings = parse_findings(reply)
    lens_line = re.search(r"审查面\s*[:：]\s*(.+)", reply)
    _all_lens = [k for k, _, _ in LENSES] + [k for k, _, _ in REHEARSAL_LENSES]
    lenses_seen = {k for k in _all_lens
                   if re.search(r"\b" + k + r"\b", lens_line.group(1))} \
        if lens_line else set()
    res = validate_findings(findings, source_text=source_text,
                            known_issues=known_issues, independent=independent,
                            verdict=verdict, lenses_seen=lenses_seen)
    checks = dict(res["checks"])
    checks["R6_判定与严重度一致"] = (
        verdict == "PASS" and not any(f.severity in ("P0", "P1") for f in findings)
    ) or (verdict in ("NOT_READY", "REVISE", "FAIL")
          and (bool(findings) or verdict != "PASS"))
    checks["R8_不冒充独立评审"] = independent or not FAKE_EVIDENCE_RE.search(reply)
    # ★ R10 的语义是「**没有可评审对象**时不许开评审」，所以它只看：
    #   ① 输入侧判定过要评（`trigger["review"]`，即真有材料），或
    #   ② 回包里自己声明了预演镜头 —— 预演本来就是「会前质控」，材料可以只有
    #      一段大纲，判定与评审同源，不靠现算。
    #   ❌ 不能用 `or bool(findings)`：那会让「对一句『你好』开了一场评审」
    #      因为「确实写了几条」而自动合规 —— 判据被自己的结论满足，等于没有判据。
    #   （这条负控在 11-14 加 R10 时就钉着，别为了让新用例变绿而放宽它。）
    checks["R10_只在有可评审对象时启动"] = (
        bool(trigger["review"]) or res["rehearsal"])
    gaps = [k for k, v in checks.items() if not v]
    # ★ 预演级的东西（"我明天要讲"）本身就构成「有人在上台前请你审过」，
    #   哪怕工作区里一份材料都没读进来。所以 `user_text` 的预演信号也算合格启动。
    #   ⚠️ **不拿 `bool(findings)` 兜**：那等于「判据被自己的结论满足」，
    #   对一句「你好」硬开一场评审也会因为写了几条而合规 —— 负控专门钉着这一条。
    if not checks["R10_只在有可评审对象时启动"]:
        checks["R10_只在有可评审对象时启动"] = is_rehearsal_text(user_text)
    gaps = [k for k, v in checks.items() if not v]
    # 反馈闭环：不合格不是只记个红，要给出「补什么」—— 指导本来就是导师的活。
    next_action = "" if not gaps else ("本次评语未通过读回校验，请补齐："
                                       + "；".join(gaps))
    mode = "独立多视角" if independent else \
        "单视角内核评审（DEGRADED_MODE，不冒充多智能体）"
    return {"ok": not gaps, "verdict": verdict, "trigger": trigger,
            "rehearsal": res["rehearsal"],
            "next_action": next_action,
            "checks": checks, "gaps": gaps, "findings": [vars(f) for f in findings],
            "n_findings": res["n_findings"],
            "n_questions": res["n_questions"],
            "n_lenses": len(lenses_seen | {f.lens for f in findings if f.lens}),
            "independent": independent, "mode": mode}


def audit_addendum(rep: dict) -> str:
    """把「这次评语缺什么」写成**能直接读的一段**，追加到最终答案末尾。

    为什么必须追加到 `final`，而不是只挂进 `result.mentor_review`：
    读回事实挂在 result 里只有界面与归档看得见，**用户和模型都看不到**。
    一份 0 条问题、没挂位置、自称「多位评审一致认为」的预演，
    会原样交到明天就要上讲台的人手上 —— 那正是本项目一直在防的
    「算过了但谁也看不见」（同 11-14 早前 `tee("mentor_audit")` 从未 emit 那条）。

    措辞纪律（与 R 判据同源，不吓人也不粉饰）：
      · 只说**缺什么**，不重写、不替模型下结论；
      · 明确写「这不是说内容错，是说这一份没法逐条复核」——
        把「不合格」与「内容错」分开，是本仓 11-14 A/B 里已经定过的口径；
      · 一次模型调用就是一次评审，不在这里暗示「已有多轮复核」。
    """
    gaps = list(rep.get("gaps") or [])
    n = int(rep.get("n_findings") or 0)
    nq = int(rep.get("n_questions") or 0)
    reh = bool(rep.get("rehearsal"))
    head = "═══ 导师视角读回：这一份现在还交不出去 ═══"
    lines = [head]
    lines.append(
        "上面那份评语没有通过内核的读回校验。**这说的是「没法逐条复核」，"
        "不是「内容一定错」** —— 两者别混。")
    lines.append(f"本次读出：{n} 条带位置的问题"
                 + (f"（其中 {nq} 条是「会被问到」的问答形态）" if reh else "")
                 + "。")
    if reh:
        lines.append(
            "预演的产物就是问题清单：给一份 PASS / 0 条问题，读的人会以为「没问题」，"
            "那他明天上台时才发现没有 —— 所以这条按不合格算。")
    if gaps:
        lines.append("缺的是：")
        lines += [f"  · {g}" for g in gaps]
    act = str(rep.get("next_action") or "").strip()
    if act:
        lines.append(act)
    lines.append("（这是内核按规则算出来的，不是又一次模型评价；"
                 "重新按上面的结构回一份即可覆盖。）")
    return "\n".join(lines)


def _topic_digest(slug: str, root=None) -> str:
    """课题现状的**内核复算摘要**（预演时注入，给追问当证据锚）。

    全部来自盘上两份东西（outline.json + LAB.jsonl）与卡点台账，不含模型判断。
    条数按构造有界（各列前几条），不靠事后截断 —— 截断会把一条缺口切成半句。
    """
    from . import topic as _T                            # noqa: PLC0415
    from . import outline as _O                          # noqa: PLC0415
    lines = ["═══ 课题现状对账（内核复算，不是模型判断）═══"]
    rep = _O.reconcile(slug, root=root)
    if rep.get("error"):
        lines.append(f"大纲：{rep['error']} —— 要定大纲用 outline_save"
                     "（章节 → 数据需求 → 实验，断线当场报）。")
    else:
        lines.append(f"大纲：{rep['sections']} 节 / {rep['needs']} 条数据需求 / "
                     f"{rep['experiments']} 个实验。")
        for key, label in (
                ("starved", "要数据但没实验供（这些节现在写不出来）"),
                ("unverified", "自称已有、台账核不上"),
                ("empty_sections", "一条数据需求都没写"),
                ("orphan_experiments", "做了不知写进哪")):
            items = rep.get(key) or []
            if items:
                names = "；".join(
                    str(i.get("title") or i.get("what") or i.get("id"))[:24]
                    for i in items[:4])
                lines.append(f"  · {label}：{len(items)} 条（{names}"
                             + ("…" if len(items) > 4 else "") + "）")
        if rep.get("ok"):
            lines.append("  · 对账全绿：每条数据需求都有实验供。")
    lc = _T.lab_counts(slug, root=root)
    lines.append(f"实验台账：{lc['events']} 条事件 / {lc['measures']} 条测量 / "
                 f"{lc['observes']} 条定性观察 / 未闭环 todo {lc['todos_open']} 条。")
    # 待办与卡点都要给**原文**而不只是条数：追问的位置会引「T01 那句话」，
    # 出口 R3 拿这份对账核位置 —— 只给条数时，真引用反而被判成不可定位。
    try:
        from . import lab as _L                            # noqa: PLC0415
        _st = _L.state(slug, root=root)
        _ot = [(tid, t) for tid, t in sorted((_st.get("todos") or {}).items())
               if t.get("status") != "closed"]
        if _ot:
            lines.append(f"未闭环待办 {len(_ot)} 条：" + "；".join(
                f"{tid}「{str(t.get('what') or '')[:30]}」" for tid, t in _ot[:3]))
    except Exception:                                   # noqa: BLE001
        pass
    ob = _T.open_blockers(slug, root=root)
    if ob:
        lines.append(f"未闭环卡点 {len(ob)} 条："
                     + "；".join(b["what"][:30] for b in ob[:3]))
    lines.append("（追问的位置可以指到上面这些事实、PLAN.md / OUTLINE.md 的某一行；"
                 "台账里没有的测量不许当成已有。）")
    return "\n".join(lines)


def mentor_brief(ws, goal: str = "", *, max_files: int = 8,
                 max_chars: int = 20000, topic_slug: str = "",
                 root=None) -> dict:
    """只读工作区材料，把**内核能自己算出来的疑点**交给导师当证据锚。

    存在的理由：不让「真导师」退化成「请模型自由发挥地挑错」。数字查无出处、
    强断言越界这类是能复算的，就该由内核先算好写进提示词，模型只做判断
    （与 svgplot / typeset / pathway 同一范式）。

    ⚠️ 读不到材料要**明说**（`n_files=0` + note），不许返回空表冒充「没有疑点」——
    本仓为「空集被当成读过」付过学费（`caps.kb_library_ids` 那段同型）。

    ★ `topic_slug`（2026-11-16）：**预演时**课题台账就是材料的一部分 ——
    「理解课题/开题准备」没有讲稿可递，被追问的对象就是课题本身。
    给了 slug 且课题存在时，PLAN.md / OUTLINE.md 与内核复算的对账摘要进
    `topic_sources`（供出口 R3 核位置 —— 追问引的卡点/待办原文就在对账里），
    摘要同时单独放在 `topic_digest`（供提示词注入）。
    **课题材料不进 `issues`**：疑点核验是对「稿子」的，台账不是稿子；
    旧字段（sources/files/n_files/issues）语义一字不变，新字段纯附加。
    """
    root_path = Path(ws) if ws else None
    sources: list[str] = []
    names: list[str] = []
    csv_text = ""
    if root_path is not None and root_path.is_dir():
        for p in sorted(root_path.iterdir()):
            if (p.suffix.lower() not in (".md", ".txt", ".csv")
                    or p.name.startswith(".")):
                continue
            try:
                s = p.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeError):
                continue
            if not s.strip():
                continue
            sources.append(s[:max_chars])
            names.append(p.name)
            if p.suffix.lower() == ".csv" and not csv_text:
                csv_text = s[:max_chars]
            if len(names) >= max_files:
                break
    issues: list[dict] = []
    for name, s in zip(names, sources):
        for it in material_issues(s, csv_text=csv_text):
            it["file"] = name
            it["evidence"] = f"{name} {it['evidence']}"
            issues.append(it)
    if not sources and str(goal or "").strip():
        # 用户直接把稿子贴在话里、工作区没有材料时，也别让导师空着手上场
        for it in material_issues(str(goal), csv_text=csv_text):
            it["file"] = "本轮输入"
            it["evidence"] = f"本轮输入 {it['evidence']}"
            issues.append(it)
    # ── 课题材料（只预演时由调用方传 slug；读不动不许拦住评审本身）──
    topic_sources: list[str] = []
    topic_files: list[str] = []
    topic_digest = ""
    _ts = str(topic_slug or "").strip()
    if _ts:
        try:
            from . import topic as _T                    # noqa: PLC0415
            if _T.read_topic(_ts, root=root) is not None:
                d = _T.topic_dir(_ts, root=root)
                for fname in ("PLAN.md", "OUTLINE.md"):
                    p = d / fname
                    if p.is_file():
                        txt = p.read_text(encoding="utf-8",
                                          errors="replace").strip()
                        if txt:
                            topic_sources.append(txt[:max_chars])
                            topic_files.append(f"课题·{fname}")
                topic_digest = _topic_digest(_ts, root=root)
                # 对账摘要本身也是材料：追问会引「卡点那句话」「待办那句话」，
                # 出口 R3 的 source_text 必须含它，否则真引用被判成不可定位。
                if topic_digest:
                    topic_sources.append(topic_digest)
                    topic_files.append("课题·现状对账（内核复算）")
        except Exception:                               # noqa: BLE001
            topic_sources, topic_files, topic_digest = [], [], ""
    return {"issues": issues, "sources": sources, "files": names,
            "n_files": len(names), "csv_loaded": bool(csv_text),
            "topic_sources": topic_sources, "topic_files": topic_files,
            "topic_digest": topic_digest,
            "note": "" if (names or issues) else
                    "工作区没有可读的 md/txt/csv 材料 —— 内核未提供可复算疑点，"
                    "导师须自行声明证据不足"}
