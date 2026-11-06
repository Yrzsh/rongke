# -*- coding: utf-8 -*-
"""目标契约 —— 让目标不漂移。

长任务里模型会不知不觉偏离原始目标：越跑越远，最后交付一个"看起来很努力但
不是你要的"东西。契约做三件事：

  1. **结构化**：把一句话目标拆成 交付物 / 验收标准 / 约束 / 禁止 四类
  2. **常驻注入**：每一轮循环都把契约重新放回模型眼前，让它始终看得见原点
  3. **可程序化核对**：验收标准是 gate.py 判定"完成"的唯一依据

契约一旦建立即冻结：`raw_goal` 是用户原话，永不被改写。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

from .stages import (_DECK_WORDS, STAGE_BY_ID, Stage, available,  # noqa: F401
                     infer_stages, plan_text, _only_negated)
from .writing import extract_target_chars

# 交付物里出现这些词 → 说明本来就要清单/表格形态，正文里出现点列不算毛病
_LISTY = ("表", "清单", "列表", "提纲", "逐条", "要点", "bullet", "table", "checklist")

DRAFT_SYSTEM = """你是目标契约起草器。把用户的目标拆成结构化契约。

只输出 JSON，不要任何解释文字、不要 markdown 代码块：
{
  "goal": "用一句话重述目标",
  "deliverables": ["要交付的具体产物"],
  "acceptance": ["可核对的验收标准"],
  "constraints": ["必须遵守的约束"],
  "forbidden": ["明确不能做的事"],
  "needs_writing": false,
  "needs_figure": false,
  "needs_deck": false,
  "needs_kb": false,
  "answer_only": false
}

铁律：
- 只能从用户目标里提取，**绝不添加用户没说过的要求**
- acceptance 要具体到可检验（写"每条参考文献的 DOI 都已核验"，
  不要写"质量好"）
- 用户没提到的类别给空数组 []，不要自己发挥
- needs_writing：本任务是否**要产出一份成篇的文字稿件**。这是**唯一**决定
  「要不要过证据门」的字段，所以判准它比你直觉里更重要：
  是（论文/综述/稿件的正文、摘要、引言、章节，或对已有文字润色、翻译、改写、
  扩写、重写成稿）⇒ true。这时 deliverables 与 acceptance 必须写实、可检验。
  不是（问我一个问题、让我检索文献、把文献入库、修改课题内容、挂载文献、
  查资料、汇报进度、报告工具调用、看看这篇讲了什么）⇒ false。
  这时 deliverables 与 acceptance 都给 []，**这是正常状态、不是缺陷** ——
  不要为了让契约"看起来完整"而编造验收标准。
- needs_figure：本任务是否要求**产出图**（画图 / 配图 / 出图 / 复现某张图 /
  做投稿图表，或对图做重排、改版）。只是"读图/看图里写了什么/审一段图注"
  **不算**，填 false。
- needs_deck：本任务是否要求**产出演示文稿（.pptx）**（做组会 PPT / 汇报幻灯片 /
  答辩 PPT / 把论文做成 PPT / 改一版已有的 PPT）。只是"整理组会记录""汇报一下
  检索结果"**不算**，填 false。
  ★ 用户明说「不用/不要/不做 PPT、演示文稿、幻灯片」，或你把「做 PPT」写进
  `forbidden` 时，`needs_deck` **必须填 false**。否定句里出现 "PPT" 不等于要
  产出 deck；契约不许一边禁止做、一边又声明需要产出（那会让交付门变成不可能
  通过的门）。
- needs_kb：本任务是否要**用文献知识库或课题台账**（查/读本地文献库、把文献入库、
  记卡点、汇报课题进度）。只是"联网随便搜搜"**不算**，填 false。
- answer_only：用户是**在问你**，还是在**要你交一件东西**？
  在问（要一句回答/一段建议/一次解释/一个判断/下一步该做什么/为什么/是不是/
  有什么区别）⇒ 填 true。这时 deliverables 与 acceptance 都给 []——
  **"把答案说清楚"本身就是交付**，不要去编验收标准，也不要假设用户要文件。
  要东西（写稿/出图/做 PPT/整理表/导 PDF/入库/跑一遍流程）⇒ 填 false，
  并按上面的规则把 deliverables 与 acceptance 写实。
"""

_JSON_RE = re.compile(r"\{.*\}", re.S)

#: **场景键的单源**（`rongke/preset.py` 用它校验预设文件里的 `scenes`）。
#: 今天真在生效的只有三个位：`needs_writing` / `needs_figure` / `needs_deck`。
#: 预设要声明第四个场景（例如知识库）就必须**先在这里加名、再在消费侧接上** ——
#: 不许让预设文件自己造一个名字：那会让「预设声明了、内核没人认」静默生效成
#: 「什么都没挂、什么都没拦」，而现场看起来反倒像预设起过作用。
SCENE_KEYS: tuple[str, ...] = ("writing", "figure", "deck")
# ── 写作意图判定 ────────────────────────────────────────────
# ★ W36（2026-10-31）：这张表**不再负责判定**。判准是**模型在起草时给出的
#   `needs_writing`**（见 `DRAFT_SYSTEM`）——用户原话：「问题直接让ai判断不就行了」。
#
#   为什么把主判据从词表换成模型：
#     · 词表实测漏判「把引言重写一遍」「写个框架」「写摘要」「出一版稿子」；
#     · 而这一位现在决定「要不要过证据门」（用户原话「如果不是涉及写作的
#       就别搞证据门」）。漏判 = 放过一次没交稿的完成声明，代价由词表承担不起。
#     · 起草本来就有一次模型调用，把这个问题搭在那次调用上**不增加成本**。
#
#   这张表退居**兜底**，只在两种情况下用（两种都是"模型的答案拿不到"）：
#     ① `fallback()` —— 起草失败（模型没回 / 回了坏 JSON），没有答案可采信；
#     ② `from_llm()` 的 payload 里**没有** `needs_writing` 这个键（老模型/截断）。
#   兜底宁可宽（漏判代价大于误判），但不要指望它准 —— 它只是"总比没有强"。
_WRITING_STRONG = (
    "论文", "综述", "稿件", "文稿", "成稿", "正文", "章节", "撰写", "起草",
    "润色", "翻译成", "投稿", "开题报告", "研究计划", "标书", "写一篇", "写一段",
    "manuscript", "paper", "survey", "polish", "submission", "write a", "draft a",
)


def _looks_like_writing(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts).lower()
    return any(k in blob for k in _WRITING_STRONG)


# ── 配图意图判定 ────────────────────────────────────────────
# 与写作那套同构，但有**一条重要的不同**：这里的词要窄。
# 写作漏判的代价是"模型凭记忆写稿"（用户明令禁止，且无法事后补救）；
# 配图漏判的代价是"图绕过了图面体检"——同样严重。
# 但**误判**的代价在这边高得多：图面门是硬门，一旦误触发，
# 一个只是"读一下这张图"的任务会被要求交一份图面体检，卡在那里。
# 所以刻意**不收**光秃秃的「图」「图表」「图片」——它们太常见，
# 大量出现在"看这张图""图里说了什么"这类**读**图的任务里。
#: **长综述**的字数下限。低于它的"综述"按一段文字处理，不套全流程。
#:
#: 为什么定在 5000：一篇真正的文献综述（哪怕只是中文核心期刊的研究进展）
#: 起步就是 6–8 千字；这个数之上"分章 + 大纲 + 配图"才成立。
#: 反向证据：`figure_test.py` / `deck_test.py` 都把「写一篇 1200 字综述」
#: 当作"不该触发重流程"的对照样本 —— 那组断言表达的是常识判断，
#: 不该被这一轮改动推翻。
_REVIEW_MIN_CHARS = 5000

#: 一篇综述的**参考文献下限**（用户明确要求：最少 50 篇）。
#: ★ 这个数在三处使用，必须是**同一个常量**：
#:   ① `contract.outline_kickoff_text()` —— 大纲阶段就摊开，让模型按章分摊；
#:   ② `gate.py` 第 7.5 条 —— 交付时核 `writing.metrics["refs_n"]`；
#:   ③ 单测的反向对照（49 拦、50 过）。
#: ★ 为什么是模块级常量而不是散在各处的字面量 50：这个仓已经因为
#:   "同一个判据写在两个地方、改一处漏一处"出过事故（见 `_RE_FIGURE_VETO`
#:   那次当场抓到的回归）。一个口径只许有一份定义。
_REVIEW_MIN_REFS = 50

_FIGURE_STRONG = (
    "配图", "配一张", "插图", "画图", "绘图", "出图", "制图", "画一张", "画个图",
    "画成", "作图", "绘制", "示意图", "图注", "矢量图", "投稿图", "排版图", "出版级图",
    "make a figure", "make the figure", "plot the data", "plotting script",
    "figure legend", "publication figure", "vector figure", "schematic",
)


def _looks_like_figure(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts).lower()
    return any(k in blob for k in _FIGURE_STRONG)


# ── 演示文稿（deck）意图判定 ────────────────────────────────
# 这一条同时被**两处**用，所以它的宽窄是被两边夹出来的：
#   ① `loop.py` 的领域按需挂载 —— 命中才把 ppt 域的 37 个工具给模型；
#   ② `gate.py` 第 9 条硬门 —— 命中就必须有当轮体检台账。
# 两处的代价方向**相反**：
#   · 挂载惜给 ⇒ 模型够不到工具，会去瞎猜工具名（挂载惜给是"够不到"）
#   · 门惜给   ⇒ 合格 deck 被硬拦、白跑一轮（误报比漏报更伤，设计定律 10）
# 取法：本表**窄**（只收明确做出演示文稿的说法），而挂载那边再加一条
# 「阶段里有 slides」作为**超集**（见 loop.py::_deck_mount_reason）。
# 方向保证是安全的：**门会拦的情形，工具一定已经挂上了**（有断言钉住这条不变量）。
#
# 刻意**不收**光秃秃的「组会」「汇报」—— 它们太常见：
# 「整理组会记录」「汇报一下检索结果」都会被误伤成硬门。
#
# ★ 2026-10-21：词表搬到 `stages._DECK_WORDS` 了，这里改为**引用**。
#   起因是同一张表在本仓库存在**第二份手抄副本**（stages.py 的 slides 阶段触发词），
#   而那份收了裸词「汇报」「组会」—— 口径与这里写的正好相反。
#   后果是真的：一次「写这个月的报告，汇报一下菌株的照片」被判出 slides 阶段，
#   `loop.py` 按「阶段含 slides ⇒ 挂 ppt 域」把 37 个 ppt 工具挂给模型，
#   模型就转去做 PPT，用户要的《月度报告.md》写盘了却记成失败。
#   现在只有一份词表，三处用途（slides 阶段 / 挂载超集 / 第 9 条硬门）读同一个元组。
_DECK_STRONG = _DECK_WORDS


def _looks_like_deck(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts).lower()
    # ★ 2026-11-05：复用 `stages._only_negated`（`infer_stages` 用的同一台否定
    #   机器），**不另抄一份**。上一版注释写「刻意不处理否定语境」，理由是怕
    #   引入第二份拷贝 —— 但代价是「不用做 PPT」里的 `ppt` 照样把
    #   `needs_deck` 抬起：契约同时写着 `forbidden=["做 PPT"]` 和
    #   `needs_deck=true`，第 9 条硬门于是反复要求 `audit_deck`，
    #   而契约又禁止产出 .pptx ⇒ 一个**不可能通过的门**，真写作任务被打回
    #   4 次、132 秒后报阻塞（归档 `runs/20261105-193745`）。
    #   单源复用既修了这个死门，又不会造成「同一判据两处各写一遍」。
    #   ⚠️ 仍读 `_DECK_STRONG`（`= _DECK_WORDS` 的别名）：`deck_test.py:9.1`
    #   靠替换这个模块级名字做**承重自检**，改成直接读 `_DECK_WORDS` 会让
    #   那颗钉子失效（实测 534/1）。
    return any(k in blob and not _only_negated(blob, k) for k in _DECK_STRONG)


# ── 文献库 / 课题（导师工具组）意图判定 ──────────────────────
# 与 deck 同构：**自报 或 关键词命中**，命中后把 kb/topic 两个域的工具挂给模型。
# 两种错的代价不对称，方向与 deck 相反：
#   · 挂载惜给 ⇒ 模型够不到本地文献库，只能联网瞎搜或凭记忆背
#     （「read_file 事故」的同型：外部能力在、本地资产够不到）；
#   · 误判 ⇒ 每场任务多付 ~12k 字符的 schema —— 只是贵，不是错。
# 所以本表**宽**（宁滥勿缺），与 _DECK_STRONG 的窄正好相反。
# 信号词单源：docs/mentor-mode-20260922.md §3.2 约束 2 那张清单。
# 刻意**不处理否定语境**（与 _looks_like_deck 同一取舍）：
# 「不需要查文献」误挂的代价只是 schema 字符，处理否定反而引入 stages.py 那套
# 否定机器的第二份拷贝（H 型）。
_KB_STRONG = (
    "文献", "文献库", "知识库", "库里", "读过的", "读书", "精读",
    "之前那篇", "上次那篇", "那篇论文", "课题", "研究方向", "卡点",
    "参考文献", "全文翻译", "查文献", "找文献", "文献综述",
    "kb_search", "kb_ask", "kb_add", "kb_read", "kb_list",
)


def _looks_like_kb(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts).lower()
    return any(k in blob for k in _KB_STRONG)


# ── 「他在问我，还是要我交东西」 ────────────────────────────────
#
# 这条判据的存在理由见 `TaskContract.answer_only` 的注释（一句话：问话没有
# 验收标准，而门对空验收标准一律打回，于是问话永远过不了门）。
#
# ★ 为什么是**词表而不是模型自报为主**：起草器被要求"绝不添加用户没说过的要求"，
#   它在"这是不是任务"上的判断没那么可靠；而这层的误判代价不对称 ——
#   把问话误判成任务 ⇒ 打回、空转、用户叫停（本次事故）；
#   把任务误判成问话 ⇒ 少一道证据核对（`answer_only` 分支仍然要求真答 + 不伪造证据）。
#   所以这里取"宽进"：只要像问句就往问那边靠，再由 `from_llm` 用
#   "用户/模型给了 deliverables 或 acceptance" 这条硬保险把它拉回来。
_QUESTION_CUES = (
    "?", "？", "该做什么", "该做什么", "该怎么做", "下一步", "为什么", "为何",
    "怎么", "怎样", "如何", "是不是", "要不要", "能不能", "可不可以", "行不行",
    "有什么区别", "区别是", "啥意思", "什么意思", "是什么意思", "是否",
    "值不值得", "靠不靠谱", "有什么建议", "你觉得", "你看", "怎么办", "选哪个",
)

# 「一看就是要东西」的动作词：出现**任意一个**，就问句也算任务。
# 与 `_WRITING_STRONG` 等词表分开维护：那几张表回答"要不要写作/出图/做 PPT"，
# 这张表回答"用户到底要不要一件东西"，两者不是同一件事（问句里也可能提到"论文"）。
_WORK_CUES = (
    "帮我写", "替我写", "写一版", "写个", "写一篇", "写成", "起草", "润色", "翻译",
    "做一版", "出一版", "生成", "导出", "整理成", "列个表", "做成表", "汇总成",
    "画一张", "画个", "画一下", "出图", "做一份", "做一份", "做一版PPT", "ppt",
    "入库", "建档", "导出 PDF", "导出pdf", "转成", "跑一遍", "执行", "部署",
)


def _looks_like_question(*texts: str) -> bool:
    """用户是在问，还是在要东西？

    判据 = 有问句线索 **且** 没有任何"动手做事"的动作词。
    刻意的"且"：`帮我写一篇，你看该从哪开头？` 这种既有问句又有动作词的，
    应当按任务走（用户要的是那篇稿子），而不是按问话走。
    """
    blob = " ".join(t or "" for t in texts).strip()
    if not blob:
        return False
    if any(k in blob.lower() for k in _WORK_CUES):
        return False
    return any(k in blob for k in _QUESTION_CUES)


# 用户**明确要了讲稿**才算。与 `_looks_like_deck` 同一族，但用途不同：
# 它决定"备注覆盖率不足要不要**阻断**"（见 gate.py 第 9 条）。
#
# 为什么必须住在契约层、而不是让模型在 `audit_deck(require_notes=…)` 里自报：
# 那是 **R 型**（判据的输入由被审者提供）—— 被审者给自己定标准，
# 只要填个 `false` 就能把"缺讲稿"变成"合格"。
# 放在这里，判据的输入就来自**用户原话**这个独立锚点。
_NOTES_ASK = ("讲稿", "备注", "演讲词", "演讲稿", "speaker notes", "口播", "逐字稿")


def contract_wants_notes(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts).lower()
    return any(k in blob for k in _NOTES_ASK)


# ── 做 PPT 的**生产动作序列** —— 全项目只此一处 ──────────────────
#
# 为什么要有它（2026-09-21 实测 `runs/20260921-113729`）：
#   原提示词把「组会汇报纪律」整段写成了**核验**（`audit_deck`、"上游那个坑"、三条
#   最容易犯的），而**生产序列**只在其中以一行 `✅ 正确顺序：…` 的旁注形式出现。
#   结果模型**照着它读到的东西做**：
#     · `audit_deck` 在提示词里出现 2 次（30% 与 77% 位置）⇒ 模型**调了** 1 次
#       （而且是对着一个 `.docx` 调的）
#     · `create_presentation` / `add_slide` 埋在 39–52% 的一行旁注里
#       ⇒ **零次**。它把 `import_document`→`export_docx` 出来的 `.docx` 当成了产物。
#   ⇒ 教训：**模型执行的是"它读到的动作"，不是"我们以为它知道的动作"。**
#      纪律段落可以通篇讲核验，但**生产动作必须单独、靠前、以动作命名地出现**。
#
# 为什么做成常量而不是在两处各写一遍：这句话要出现在**两个渠道**
#   （`loop.SYSTEM` 与契约提示词），而"同一句话抄两遍"就是 H 型
#   （改一处忘一处）。所以这里不是"两处保持一致"，是**一处定义、两处引用**。
#: ★★ 出稿**路线选择** —— 渲染在编号动作**之前**（2026-09-22）。
#:
#: 为什么它是**单独一段**、而不是"第 1 步"：
#:   它决定的是「**走哪一串步骤**」，不是「先做哪个动作」。
#:   塞进编号序列会同时坏两件事：① 步骤序号被搅乱；
#:   ② 现有三条「步骤先后」断言按 `"audit_deck" in s` 找步骤 —— 而路线说明里
#:      也点了这个名字 ⇒ `audit=0`，三条当场失守（实测）。
#:   ⇒ **不是测试太脆，是抽象层放错了**（A 型：拿"选路线"当"一个步骤"）。
#:
#: 为什么必须进**提示词正文**（而不是只写在 `loop.SYSTEM` 的散文里）：
#:   本文件上面那笔血债写着 —— **「模型执行的是它读到的动作」**。
#:   动作藏在散文里 = 模型看不见，它会照编号序列走 ⇒ 出来的是
#:   `create_presentation` + 套主题的稿，**不是学校的样子**。
DECK_ROUTE_GUIDE: tuple[str, ...] = (
    # ★★ 2026-09-22：**先选路线**，而且把它放在**最前**。
    #
    # 为什么必须进配方、而不是只在 `loop.SYSTEM` 的散文里提一句：
    #   本文件上面那条血债写着 —— **「模型执行的是它读到的动作」**。
    #   当时模型照着提示词里**出现过的核验动作**做（`audit_deck` 出现 2 次就调了
    #   1 次），而埋在旁注里的**生产动作**零次，于是它把 `.docx` 当成了产物。
    #   ⇒ 品牌模板这条路若只写成散文，模型会照下面那串编号动作走
    #     （`create_presentation` → `add_slide` → 套主题），出来的**不是学校的样子**。
    #   ⇒ 所以它必须**单列一条、排在 1**、且**以动作命名**。
    #
    # 为什么写"取代第 2–N 步"而不是"再加一步"：同一个理由 ——
    #   配方是**编号动作序列**，写成并列的额外一步，模型会两步都做
    #   （已有的先例：`style_deck` 与 `compose_deck` 必须写"二选一"，
    #    否则两套坐标系打架）。品牌模板路线**自己建页、自己填字**，
    #   所以它**取代**建页那一段，而不是接在后面。
    "**先选出稿路线（二选一，别都走）** —— 学校/学院给了模板（`config.json` 里配了 "
    "`deck.template`）时**一律走甲**："
    "**路线甲：按品牌模板出稿**。① `deck_template_plan()` 拿到页型与**每槽字数上限**"
    "（槽位有**硬容量**：页标题只装 5 字、章节标题 6 字；不看上限去写必然被拒）；"
    "② `deck_from_template(out=\"deck.pptx\", pages=[…])` —— 它保留模板的**全部装饰**"
    "（满版蓝底、章节巨型数字、校徽、出血照片），**只在文字槽上填字**，"
    "品牌装饰一个字节都不重绘。**它取代下面第 2 步到外观那一步**"
    "（不用 `create_presentation`/`add_slide`/`manage_text`，**也不用再套主题**），"
    "做完**直接跳到 `audit_deck`**。"
    "⚠️ 每个槽**都必须填**（不填就会把模板里的「请输入文本」交出去）；"
    "⚠️ 同一个模板页只能用一次（要两个章节页用 `section-1`/`section-2`）；"
    "⚠️ 若 `deck_template_plan()` 说槽位表读不到 ⇒ 改走路线乙。"
    "**路线乙：从零建**（没有模板时）。走下面那串 `create_presentation` → … → 外观二选一。",
)


DECK_TOOLCHAIN_RECIPE: tuple[str, ...] = (
    # 2026-09-21 新增，且**排在最前**：动手之前先看"什么样算好"。
    # 用户那句方向纠正（「人家同样是用 agent，只加个 skill 就能做出好看的图了」）
    # 里，那个 skill 真正起作用的就是**一份可以照着比对的成品**。
    # 参考样例存在 `docs/deck-reference/`（随库发布、可重跑重建），
    # 由本工具以**文本**形态交给模型 —— 因为内核的模型通道读不了图片
    # （`llm.py` 的 content 是字符串、全仓 `image_url` 零命中）。
    # ⇒ 给模型一张 PNG 等于没给；给一段**逐页可抄的文本**才起作用。
    "deck_reference() —— **动手之前先调一次**：取一份随库发布的参考样例"
    "（12 页、六种页型全用到、逐页正文与讲稿备注，五门全绿、零提示），"
    "把「什么样算好」变成照着比的结构；顺带告诉你每页该装多少内容",
    "create_presentation —— 建一份新文稿",
    "switch_presentation(presentation_id=…) —— **必须紧跟**，不许跳过",
    "add_slide / manage_text / … —— 逐页填内容",
    # 2026-09-21 新增：配图。起因是成片 `pictures=0` —— 一份 13 页组会 PPT
    # 一张图都没有，而 `no_visual_elements` 只在"≥10 页且图/表/图表**全 0**"时
    # 才 WARN（它有两个图表就绕过去了）。⇒ 目标函数里没有的东西不会被做出来。
    #
    # 2026-11-05 **改工具名**：原先点名 `figure_render`（workbench MCP →
    # Origin COM / matplotlib）。框架开源后发行件里既没有 Origin 也没有绘图栈
    # ⇒ 配方指向一个不存在的工具，模型必然调不到，`pictures=0` 重演。
    # 现在优先点名荣珂**内置**的 `plot_data`（有绘图栈 → 真 PNG）与
    # `plot_svg`（零依赖保底 → SVG），两者都在 figure 域随包挂载。
    # ⚠️ SVG **不能直接插 pptx**：`manage_image` 吃的是位图。没有 PNG 时，
    #    SVG 只作为**随稿附图**交付，不要调 manage_image 去插一个盘上
    #    不存在的 PNG —— 那是最典型的「工具退出 0、图根本没进去」。
    # ⚠️ 顺序硬约束：`manage_image` 改的是 MCP 会话内存中的文稿 ⇒
    #    必须在 `save_presentation` **之前**，否则存完盘再插就是白插。
    "plot_data(table=…) —— 凡有数字的页**别用文字复述**，出一张图；"
    "装了 rongke[figure] 时产物里有 run.png，把它交给 manage_image 插图",
    "plot_svg(table=…) —— **零依赖保底**：没装绘图栈也能出带中文的 SVG 预览图；"
    "⚠️ SVG 不能直接插 pptx，此时作为随稿附图交付，"
    "不要调 manage_image 去插不存在的 PNG",
    "manage_image(slide_index=N, operation=\"add\", image_source=\"fig1.png\")"
    " —— 仅当盘上**真有**这张 PNG 时才调（必须在 `save_presentation` 之前）",
    # 旧配方里那段 `figure_render.out` 绝对路径陷阱（相对路径落到工作台根、
    # 回传的 file 只是把 out 抄回来）随外部工具一起退休；保留外部后端时
    # 再查历史记录，不再占用每轮都要重发的提示词字符。
    "save_presentation(file_path=\"deck.pptx\") —— 存盘",
    "set_slide_notes(pptx=\"deck.pptx\", notes=[每页一段]) —— 补讲稿备注"
    "（上游 37 个工具都没有这个能力，由内核提供；长度必须与页数一一对应）",
    # 2026-09-21 新增：起因是用户看完一份**结构全绿**的 deck 只说了一句
    # 「那能做的这么丑」。丑的第三条原因是**契约与门里一个字都没提外观** ——
    # 目标函数里没有的东西永远不会被优化出来。这一步必须进配方，而且
    # **必须有一条判据兜底**（见 deck.DeckAuditor 的 visual 一节），否则加了也是空转。
    #
    # 2026-09-21 **再补**：上面这条补完、`style_deck` 也跑过了，用户看完那份
    # 13 页 deck 仍然是那句「**太差了，不能直接上组会**」。量下来根因是
    # `style_deck` **只换色换字、不动骨架** —— 而"好看"这件事，版面本身才是大头。
    # ⇒ 新增 `compose_deck`：按 `rongke/design.py`（设计资产**单源**）的三区网格、
    #    8 档字号阶梯、色彩角色与六种页型几何，把整份版面**确定性重排**一遍。
    # ⚠️ 这里**故意写成"二选一"而不是"再加一步"**：配方是**编号动作序列**，
    #    而实测教训是「**模型执行的是它读到的动作**」（`runs/20260921-113729`
    #    把 .docx 当产物）。写成两步，模型就会两步都调 ⇒ 两套坐标系打架
    #    （一个不动骨架、一个重排骨架），后调的覆盖先调的。
    "外观这一步**二选一**："
    "① 快套 → `style_deck(pptx=\"deck.pptx\", theme=\"juanqing\")`"
    "（只换色槽/换字体/加页脚，**不动版面骨架**）；"
    "② 精排 → `compose_deck(pptx=\"deck.pptx\", pages=[逐页页型])`"
    "（按设计规范**重排整份版面**：三区网格、字号阶梯、强调线、页脚落款；"
    "你只交页型，几何由内核查表）"
    "。**要交付上组会就用 ②**；两者**不要都调**（双重坐标系）",
    "audit_deck(pptx=\"deck.pptx\") —— 交付前必调，改了 deck 要重跑",
)


def deck_recipe_lines(indent: str = "  ") -> str:
    """渲染成一段：**路线选择在前，编号动作在后**。

    **两处渠道都走它**（`loop.SYSTEM` 与契约提示词），所以格式也只有一处。
    路线说明**不参与编号** —— 它决定走哪串步骤，不是步骤之一（见 `DECK_ROUTE_GUIDE`）。
    """
    head = "\n".join(f"{indent}{s}" for s in DECK_ROUTE_GUIDE)
    body = "\n".join(f"{indent}{i}. {s}" for i, s in enumerate(DECK_TOOLCHAIN_RECIPE, 1))
    return f"{head}\n{body}" if head else body



def _ensure_review_stages(ids: list[str], want_figure: bool = True) -> list[str]:
    """给综述类任务补齐**流程上必然要经过**的阶段，并保持流水线顺序。

    ## 补什么、插在哪

    | 阶段 | 何时补 | 插在哪 |
    |---|---|---|
    | `fulltext` | 总是（综述必然要落到"有没有全文"这一关） | `search` 之后 |
    | `outline`  | 总是 | `draft` **之前** |
    | `figure`   | `want_figure` 为真时 | `outline` 之后、`draft` **之前** |

    ## 为什么是"插入"而不是"追加"

    `stages.plan_text()` 把阶段按元组顺序**逐条编号念给模型**（"顺序即流水线顺序"）。
    追加到末尾 ⇒ 模型读到的是"先写完稿，最后再定大纲、最后再画图" ——
    那正是这一整套改动要消灭的返工形态。

    ## 为什么只补缺的

    用户可能在契约里显式指定了阶段（`preset` / `--stages`）。已经点名的阶段
    一律保留原位，这里只保证"该有而没有"的那几个被补上，**不重排用户的显式序列**
    —— 重排会覆盖掉用户的明确意图，那比漏补更糟。

    ## `want_figure` 为什么由调用方传

    用户明写「不用配图」时 `needs_figure` 已经是假，此时再补一个 figure 阶段
    会既注入图面纪律、又开图面门 —— 那是**跟用户的明令对着干**，
    而且交付时会被自己的硬门拦下（实测过这族"不可能通过的门"）。
    """
    out = list(ids)

    def _insert_before(anchor: str, sid: str) -> None:
        if sid in out:
            return
        if anchor in out:
            out.insert(out.index(anchor), sid)
        else:
            out.append(sid)

    # ① fulltext：要写作/要检索的成篇任务，必然要落到"有没有全文"这一关。
    #    没被关键词推出来时，插在 search 之后；没有 search 就插在 draft 之前。
    if "fulltext" not in out:
        if "search" in out:
            out.insert(out.index("search") + 1, "fulltext")
        else:
            _insert_before("draft", "fulltext")
    # ② outline：结构必须先冻结
    _insert_before("draft", "outline")
    # ③ figure：图必须先于正文（插在 outline 之后，保证"先定结构、再出图、再写"）
    if want_figure and "figure" not in out:
        if "outline" in out:
            out.insert(out.index("outline") + 1, "figure")
        else:
            _insert_before("draft", "figure")
    return out


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON（容忍 ```json 包裹与前后废话）。"""
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


@dataclass
class TaskContract:
    raw_goal: str                                  # 用户原话，不可变
    goal: str = ""                                 # 一句话重述（留空则等于原话）
    deliverables: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    # 是否写作任务：由模型自报或关键词命中
    needs_writing: bool = False
    # 是否要产出**图**：同样"自报 或 关键词命中"。
    # 命中后：① 事前注入图面纪律；② 交付时图面门要求一份新鲜的图面体检。
    needs_figure: bool = False
    # 是否要产出**演示文稿（.pptx）**：同样"自报 或 关键词命中"。
    # 命中后：① 把 ppt 域的工具挂给模型（否则 37 个工具的 schema 会加在
    # 每一场任务上）；② 事前注入组会纪律；③ 交付时 deck 门要求一份新鲜的 deck 体检。
    needs_deck: bool = False
    # 是否要用**文献知识库 / 课题台账**（导师工具组）：同样"自报 或 关键词命中"。
    # 命中后：把 kb 与 topic 两个域的工具挂给模型（W25，照 needs_deck ⇒ ppt 域
    # 的同一条不变量：needs_kb ⇒ kb_* 必在 allow 里）。这两个域**一起进退**：
    # 课题台账没有库就是空壳，库没有台账就是一次性检索 —— 设计文档 §2.4 的原话。
    needs_kb: bool = False
    # ── 「他是在问我」这一层（2026-10-31，W36）────────────────────────
    # ★ 症状（用户原话）：「我只是问他我课题下一步该做什么，他跟我说交付没过检查」。
    #   归档 `runs/20261031-085105` 的现场：31 条 assistant 消息**全都是空正文 +
    #   tool_calls**，一个字都没答；两次 `deliver_task` 都被证据门打回，理由都是
    #   「契约里没有可核对的验收标准（起草时未能提取），无法判定完成」。
    # ★ 根因是**两处各自的"正确"叠成了一个死角**：
    #   ① 起草器的铁律写着「用户没提到的类别给空数组 []」⇒ 对一句问话，
    #      `acceptance` 老老实实是 `[]`；
    #   ② `gate.py` 对空 `acceptance` 一律判「无法判定完成」⇒ 于是**无论它答得多好
    #      都过不了门**，只能一直再加工具调用，直到用户叫停。
    # ★ 所以要有这一层：问话不按"任务契约 + 证据门"走，按"回答 + 自查"走。
    #   它不是把门关掉（那样任何回答都算过），而是**换一种判定**——
    #   见 `gate.py` 里 `answer_only` 那条分支：要求真有一份回答，且不许伪造证据。
    #
    # ★ 用户看过这个设计后的话：「直接加个任务模式和仅问答不就行了」。
    #   对 —— 所以输入框上有一个**直接开关**（`#modeSwitch`，事件 `mode:set`），
    #   按下去就把这一轮定死成哪个模式，不必等起草器去猜。
    #   开关**优先**：用户亲手按的，永远盖过模型自报与关键词。
    #   `answer_only` 仍然常驻：没按开关时由分类器兜底（见 `_looks_like_question`）。
    answer_only: bool = False
    # 本任务包含哪些阶段（stage id）。**每个阶段有唯一的专属技能**，
    # gate 会逐一核对是否装载。由目标文本推断，契约冻结时定死。
    stages: list[str] = field(default_factory=list)
    # 目标字数：**从用户原话正则抽取**，不采信模型自报 ——
    # 模型会"interpretive"地把 120 字放宽，用户写的数字不会。
    target_chars: int | None = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.goal:
            # goal 只是 raw_goal 的复述，不该逼调用方重复一遍；
            # 留空即以原话为准（raw_goal 永远是唯一权威）。
            self.goal = self.raw_goal
        # ── 契约自洽：禁止做的事，不许同时是「要产出的东西」（2026-11-05）──
        # ★ 真事故：`from_llm` 的 `needs_deck = 模型自报 or 词表命中` 里，
        #   模型把 `needs_deck` 报成 true，同时老实把 `forbidden` 抽成
        #   `["做 PPT"]`。两处各自都「照规则办」，叠起来就成了互斥契约：
        #   第 9 条硬门按 needs_deck 要 `audit_deck`（必须先有 .pptx），
        #   契约又禁止产出 .pptx ⇒ 死门，模型只能报告阻塞。
        # ★ 这个校正放在 `__post_init__` 而不是 `from_llm`：三条构造路径
        #   （from_llm / fallback / 调用方直接构造）全都要守同一条不变量，
        #   写在一处才不会「改一处漏一处」（本项目最反复的那类缺陷）。
        # ★ 且必须排在下面所有读 `needs_deck` 的派生之前。
        if self.needs_deck and self._user_vetoed_deck:
            self.needs_deck = False
        # ── 图意图派生（2026-10-24，修一个**真 bug**）────────────────────
        # ★ 症状：`TaskContract(raw_goal="画一张融合机制示意图")` 出来
        #   `needs_figure=False`、`stages=[]` —— 一个**明确的画图任务被判成不要图**。
        #   后果是连环的：① figure 域工具不挂载（模型手里根本没有绘图工具）；
        #   ② 图面纪律段不注入；③ 交付时图面门不启用；④ 阶段推不出 figure。
        # ★ 根因：`needs_figure` 只是 dataclass 的默认 `False`，`__post_init__`
        #   过去**从不用 `_looks_like_figure` 填它** —— 只有综述那条分支会置位。
        #   而 `_looks_like_figure()` 本身一直是对的（"画一张/示意图"都在词表里）。
        #   也就是说：判定函数存在、却没人调用它。这是本仓最反复的一类缺陷 ——
        #   **"定义了却没人接线"**，表现是"看起来什么都在，就是不起作用"。
        # ★ 为什么放在 `_derive_review_shape()` **之前**：
        #   补阶段要用到 `self.needs_figure`（`_ensure_review_stages(want_figure=…)`）。
        #   先算出"用户/形态上到底要不要图"，再让综述分支在此基础上叠加。
        # ★ 只在**没人显式给值**时才派生：调用方传进来的 `True` 是权威，
        #   不许被关键词判据覆盖成 False。
        if not self.needs_figure and not self.needs_deck:
            if not self._user_vetoed_figure and _looks_like_figure(
                    self.raw_goal, self.goal, " ".join(self.deliverables)):
                self.needs_figure = True
        # ── 综述/成篇稿件的**形态派生**（2026-10-22）─────────────────────
        # 为什么放在 stages 推断**之前**：下面那条 fulltext/outline 的补阶段
        # 要看 `needs_writing` 与目标文本，先把它判对再推阶段。
        self._derive_review_shape()
        if not self.stages:
            found = infer_stages(self.raw_goal, self.goal, " ".join(self.deliverables))
            if not found and self.needs_writing:
                found = [STAGE_BY_ID["draft"]]   # 判为写作却没识别出阶段 → 至少是起草
            ids = [s.id for s in found]
            # ── 综述的**全流程补阶段**（2026-10-22，用户点名的流程顺序）─────
            # 起因：上一轮综述真跑（runs/20261015-092001）契约只推出来
            # `search, verify, draft` —— **没有 fulltext、没有 outline、没有 figure**。
            # 于是：nature-downloader 从没被要求装载（全文率 20%）、
            # 大纲从没被冻结（写到第三节回头改第二节）、**一张图都没出**。
            #
            # 为什么不靠加关键词，而是按**任务形态**补：
            #   `infer_stages` 是纯关键词命中，"写一篇……的文献综述"里
            #   凑不出「下载全文」「大纲」这些字面 —— 加词只会得到"用户恰好这么写
            #   才生效"的脆弱行为。而"这是一篇综述"是**形态**，形态决定它
            #   必然要经过取全文、定结构、出图这几道工序。
            #
            # ★ 只补**缺的**、且**不重排**已推断出来的阶段：`outline` 插在
            #   draft 之前、`figure` 插在 draft 之前、`fulltext` 插在 search 之后。
            #   插入位置错了就等于顺序错了（`plan_text` 是逐条编号念给模型的）。
            if self._is_review_like and self._is_long_review:
                ids = _ensure_review_stages(ids, want_figure=self.needs_figure)
            self.stages = ids
        if self.target_chars is None:
            self.target_chars = extract_target_chars(
                self.raw_goal, self.goal, " ".join(self.deliverables))

    # ── 综述形态 ─────────────────────────────────────────────
    #: 综述类任务的**形态判据**词表。刻意比 `_WRITING_STRONG` 窄：
    #: 只认"这是一篇综述/文献总结"的说法，不认「论文」「稿件」——
    #: 后者多半是原创研究稿，不该被强制走"先定章节大纲 + 先出图"这套。
    #: ⚠️ 词表**只此一份**。`stages.py` 的 draft 触发词里也有「综述」，
    #: 但那是"要写作"的信号，不是"这是综述形态"的信号，两者不能合并：
    #: 合并会让「综述一下这个课题的文献」也被要求先出大纲和图。
    _REVIEW_WORDS = (
        "文献综述", "综述", "review article", "literature review",
        "研究进展", "国内外进展", "研究现状", "文献总结", "文献梳理",
    )

    def _derive_review_shape(self) -> None:
        """判「这是一篇综述」并据此派生 `needs_figure` / `needs_kb`。

        ★ 为什么综述要**默认带图**（`needs_figure=True`）：
           实测依据是上一轮真跑 —— `needs_figure=False` 的直接后果是
           ① 图面纪律段不注入；② 交付时图面门不启用；③ 最后 0 张图。
           而一篇综述没有一张机制图/汇总表，本来就不成立。
           用户的原话更明确：「首先需要先根据知识库进行图表绘制，防止后面遗漏」。
        ★ 为什么不改 `_looks_like_figure`：那个函数还被
           `figure_test.py` 的反向对照（「写一篇 1200 字综述」**不该**判成配图任务）
           钉着，改它会把那组断言打红。而且语义上它是"用户要一张图"的判据，
           这里要判的是"综述这种体裁需要图"，**是两个不同的命题**。
        """
        blob = " ".join([self.raw_goal, self.goal,
                         " ".join(self.deliverables)]).lower()
        # ★ 两个标志**先各自给默认值**，再按判据覆盖。
        #   最初只在命中时才赋值，于是 `to_prompt()` 里那句
        #   `if self._is_long_review` 对**任何非综述任务**都抛 AttributeError ——
        #   实测直接把「把材料区那份做成 PPT」这类无关任务打成崩溃。
        #   这类"只在一条分支上赋值的标志"是本仓反复踩的形状：
        #   同一个属性在有的实例上存在、有的不存在，读它的地方就必须知道调用上下文。
        self._is_review_like = False
        self._is_long_review = False
        self._is_review_like = (bool(self.needs_writing or _looks_like_writing(blob))
                                and any(k in blob for k in self._REVIEW_WORDS))
        if not self._is_review_like:
            return
        # ★ 短文不是综述。用户原话里字面给定的字数（`extract_target_chars` 抽的）
        #   小于 `_REVIEW_MIN_CHARS` 时，它是一段"综述性文字"，不是一篇综述 ——
        #   不该被要求先冻结大纲、先出图、再分章写。
        #   依据：反向对照句「写一篇 1200 字综述」在 `figure_test.py` / `deck_test.py`
        #   里被当作"不该触发重流程"的样本；把全流程套上去会把这些断言打红，
        #   而那组断言表达的是用户的常识判断（1200 字要什么大纲）。
        n = self.target_chars
        if n is None:
            n = extract_target_chars(self.raw_goal, self.goal,
                                     " ".join(self.deliverables))
        self._is_long_review = n is None or n >= _REVIEW_MIN_CHARS
        if not self._is_long_review:
            return
        # 综述必然要用库（要引文献），也必然要图（要机制图/汇总表）
        self.needs_kb = True
        # ⚠️ 只在用户**没有明确说不要图**时才置位 —— 尊重「不用配图」这类明令。
        if not self._user_vetoed_figure:
            self.needs_figure = True

    #: 用户是否**明说不要图**。这个判据现在有**两处**要用，所以抽成一处：
    #:   ① `__post_init__` 开头的关键词派生（「画一张图，但不用配图说 …
    #:      实心是：说了要画就别再纠结"不用配图"这种组合，但**单独说不要图**必须听）；
    #:   ② 综述分支里"综述默认配图、除非用户明说不要"。
    #: ★ 抽出来的动因是一个**当场抓到的回归**：把 `needs_figure` 的派生提到
    #:   综述分支之前以后，`needs_figure` 先被关键词置成 True，于是综述分支里那句
    #:   `if not re.search(...): self.needs_figure = True` 就再也不会执行 ——
    #:   「写一篇……文献综述，不用配图」被判成要图。**同一份判据写在两个地方，
    #:   加一个前置条件就会漏掉一个地方。**
    _RE_FIGURE_VETO = re.compile(r"不(?:用|要|需|需要)(?:画|配|出)?图")

    @property
    def _user_vetoed_figure(self) -> bool:
        return bool(self._RE_FIGURE_VETO.search(self.raw_goal))

    @property
    def _user_vetoed_deck(self) -> bool:
        """用户是否**明令不要演示文稿**（决定 `needs_deck` 不许为真）。

        两条口径，都读契约自己的字段，不另造一套否定词表：
          ① `forbidden` 是「明确不能做的事」清单，语义自带否定 ——
             里面出现 deck 词就算否决（不需要再找「不」字）；
          ② 原话 / 目标 / 约束里，若**所有**命中的 deck 词都只出现在否定语境
             （复用 `stages._only_negated`），也算否决。
        用 `all` 而不是 `any`：一句「不用做 PPT，另外做一份组会汇报」里
        两处 hit 一否一肯，不该被一刀切判成「不要 deck」。
        """
        f = " ".join(self.forbidden).lower()
        if any(k in f for k in _DECK_STRONG):
            return True
        blob = " ".join([self.raw_goal, self.goal] + list(self.constraints)).lower()
        hits = [k for k in _DECK_STRONG if k in blob]
        return bool(hits) and all(_only_negated(blob, k) for k in hits)

    @property
    def allow_lists(self) -> bool:
        """交付物本身就是清单/表格形态时，正文出现点列不算毛病。"""
        blob = " ".join(self.deliverables + [self.raw_goal]).lower()
        return any(k in blob for k in _LISTY)

    # 「明令禁用工具」的措辞族。**刻意收窄**（与 stages.py 的否定语境同一条哲学）：
    # 只认"工具"被点名且被否定的说法；「不需要检索文献」只否定了检索这一类，
    # 用户没禁别的工具 —— 收进来会把 read_file / fetch_doi 的正常任务误判成零工具。
    # 宁可漏（漏了模型顶多被门打回一次，协议文本会兜底），别把有工具的任务错标成零工具。
    _RE_NO_TOOLS = re.compile(
        r"不(?:要|需|需要|许|得|用)(?:调用|使用|用)?任何?工具"
        r"|不调用任何?工具"
        r"|(?:无需|零)工具")

    @property
    def no_tools(self) -> bool:
        """用户原话是否**明令禁用工具**（2026-09-29 实测逼出来的判定）。

        一次真实任务：「请只回一句话……不要调用任何工具，不要检索」——
        模型如实遵守了契约，但证据门第 1 条要求"交付必须附真实调用"，
        零工具任务永远凑不出这条证据：**契约与门禁互斥**，模型只能报告阻塞。
        纯对话（问答、确认、改写一句话）是普通人最常用的场景，这条路必须通。
        判定从 raw_goal 正则派生（与 target_chars 同一手法：用户写的算数），
        图 / deck 任务不适用 —— 它们逻辑上必须动工具。
        """
        if self.needs_figure or self.needs_deck:
            return False
        return bool(self._RE_NO_TOOLS.search(self.raw_goal))

    # 本任务要求装载的技能（按流水线顺序，去重）
    @property
    def required_skills(self) -> list[str]:
        out: list[str] = []
        for sid in self.stages:
            st = STAGE_BY_ID.get(sid)
            if st and st.skill not in out:
                out.append(st.skill)
        return out

    # ── 注入模型 ────────────────────────────────────────────
    def to_prompt(self) -> str:
        def block(title: str, items: list[str], bullet: str = "-") -> str:
            if not items:
                return ""
            body = "\n".join(f"  {bullet} {x}" for x in items)
            return f"{title}\n{body}\n"

        parts = [
            "═══ 目标契约（本轮任务的唯一准则，不得偏离）═══",
            f"原始目标（用户原话，不可改写）：{self.raw_goal}",
            f"目标重述：{self.goal}",
            "",
            # ★ 问话这一类必须在**最前面**说清，而且要说"交付怎么走" ——
            #   否则模型看到"必须交付：（空）/ 验收标准：（空）"只会更慌，
            #   于是继续调工具找证据（归档里 31 条空正文就是这么来的）。
            ("本轮是提问，不是任务：用户在等你一句回答。\n"
             "  · 直接回答，别去找产物、别去凑证据；\n"
             "  · 答完用 deliver_task 交上来，**把回答正文写进 summary**；\n"
             "  · evidence 可以留空（真查过库想留台账也可以附）；\n"
             "  · 不许因为「没有验收标准」就报告阻塞 —— 问话本来就没有验收标准。\n"
             if self.answer_only else ""),
            block("必须交付：", self.deliverables),
            block("验收标准（宣布完成时，每一条都必须有证据）：", self.acceptance),
            block("约束：", self.constraints),
            block("禁止：", self.forbidden, "✗"),
            "═══════════════════════════════════════════",
        ]
        text = "\n".join(p for p in parts if p is not None)
        # 禁用工具的任务：把「交付协议怎么走」**事前**讲清楚，别等它被门打回才明白。
        # （2026-09-29 实测：模型如实遵守了"不要调用任何工具"，却在交付时
        #   凑不出证据门要的 call_seq，两头不是，最后只能报告阻塞。）
        if self.no_tools:
            text += (
                "\n\n═══ 本任务禁用工具：交付协议 ═══\n"
                "用户明令不调用工具 —— **遵守它**：不要检索、不要读写文件、不要装载技能。\n"
                "「不要工具」指的这些**实际操作**；交付协议 deliver_task 照常调用（它不是工具操作）。\n"
                "deliver_task 的 evidence **留空数组**即可 —— 证据门对本任务不适用，"
                "**绝不许编造 call_seq 来凑证据**。\n"
                "内核能实测的验收标准（如字数）仍然生效，答案要自己达标。\n")
        st = [STAGE_BY_ID[s] for s in self.stages if s in STAGE_BY_ID]
        plan = plan_text(st, available())
        if plan:
            text += "\n\n" + plan
        # ★ 「开工动作」**紧贴目标之后、纪律之前** —— 位置是实测出来的
        #   （见 DECK_TOOLCHAIN_RECIPE 的注释）：生产动作一旦混进一段以"核验"
        #   为名的长文，模型就只执行核验动作。
        #   ⚠️ 但必须排在**阶段技能之后**：第 6 条技能门要求"动手前先 load_skill"，
        #   把"第一步就 create_presentation"排在装载之前，等于**诱导它跳门**。
        #   顺序：目标 → 先装技能 → 再动手 → 最后才是纪律。
        if self.needs_deck:
            text += "\n\n" + self.deck_kickoff_text()
        # ★ 综述的取全文开工动作 —— 必须在写作纪律**之前**：
        #   它回答的是"动笔之前先把文献备齐"，排在写作纪律之后就变成
        #   "先写，写不动再去找文献"，那正是要消灭的顺序。
        if self._is_long_review:
            text += "\n\n" + self.fulltext_kickoff_text()
        if self.needs_writing:
            text += "\n\n" + self.writing_discipline_text()
        if self.needs_figure:
            text += "\n\n" + self.figure_discipline_text()
        if self.needs_deck:
            text += "\n\n" + self.deck_discipline_text()
        if self.needs_kb:
            text += "\n\n" + self.kb_kickoff_text()
        # ★ 大纲与章-文献映射的开工动作排在**最后**（它要求"前面几件都做完才开工"，
        #   排在最前会被读成"第一步就写大纲"，而大纲必须建立在库够用之上）。
        if self._is_long_review:
            text += "\n\n" + self.outline_kickoff_text()
        return text

    # ── 综述：全文获取开工动作 ──────────────────────────────
    def _refs_budget_line(self) -> str:
        """把 {_REVIEW_MIN_REFS} 篇摊到章上，给模型一条可执行的预算线。

        ★ 为什么要摊：只给一个"总数 50"，模型会在大纲里每章写 8–10 篇、
          合计不到 50，等到交付时才被发现 —— 那是一次完整返工。
          给出每章下限，它在**冻结结构的那一步**就能把缺口看出来。

        ★ 章数从哪来：契约里 `needs_review` 的章数**此刻还不存在**（大纲还没写），
          所以这里按常见综述的 5–7 章给一个区间，而不是精确值。
          区间比精确值更诚实：不知道的事不假装知道。
        """
        n = _REVIEW_MIN_REFS
        lo, hi = 5, 7               # 综述常见的章节区间
        return (f"{n} 篇摊到 {lo}–{hi} 章，每章至少 "
                f"{-(-n // hi)}–{-(-n // lo)} 篇（向上取整）")

    def fulltext_kickoff_text(self) -> str:
        """综述的**取全文**动作。这是「20% 全文率」那条坑的正面对策。

        ★ 为什么要点名 `literature_fetch` 而不是只写「去下载全文」：
          本仓的血债是「模型执行的是它读到的动作」（见 DECK_TOOLCHAIN_RECIPE
          上方那段 2026-09-21 的复盘）。写"去拿到全文"它就会去 `kb_add(src="doi:…")`，
          而那条路**没有 Europe PMC 这条路**（实测整份 litkb.py `europepmc` 命中 0），
          于是 12 篇里只落下 3 篇、而它以为自己都拿到了。

        ★ 为什么强调"下不到 ≠ 世上没有"：
          实测的失败回包被模型读成"这篇文献不存在"，于是从综述里删掉了一个
          本来有开放版的论点。这句提醒必须写在动作旁边，不能只写在纪律段里。
        """
        return "\n".join([
            "═══ 全文获取开工动作（综述必须先备齐全文，再动笔）═══",
            "",
            "**顺序不许颠倒**：先把要引的文献的**全文**备齐入库，再写正文。",
            "只凭摘要写综述，写出的是别人的摘要拼贴 —— 段落里没有方法、没有数据、",
            "没有边界条件，一问就露。",
            "",
            "做法：",
            "1. `kb_coverage(topics=[…])` —— **先判库里够不够**（把题目拆成 5–8 个",
            "   **三字以上或英文**的主题词；中文两字词会被索引静默丢弃，回包会点名它们）。",
            "   - `need_more_literature=false` ⇒ **库够用，跳过下面的检索入库**，",
            "     直接进入大纲；缺哪一篇就只补那一篇，**不要重开一轮全量检索**。",
            "   - `need_more_literature=true` ⇒ 只针对它点出的 `gaps` 去补。",
            "2. `literature_search(query=…)` —— 找候选（OpenAlex/arXiv/Crossref/PubMed）。",
            "3. `literature_fetch(dois=[…])` —— **取全文**。它按三条路依次试：",
            "   Europe PMC 全文 → Unpaywall OA 直链 → doi.org 内容协商。",
            "   拿到什么就如实报什么（`route` 字段写着走的是哪条路）。",
            "4. `kb_add` / `kb_ingest` —— 把 `literature_fetch` 回的 `file` 路径入库。",
            "5. 回到第 1 步**用同一批主题词再判一次**，直到够用。",
            "",
            "⚠️ `literature_fetch` 说没取到时，**那是「这几条路这次没成」，",
            "   不是「世上没有这篇」**。如实写进检索死区，并写明缺的是哪条路",
            "   （机构订阅 / CNKI 中文库 / 出版商账号）。不许据此删掉一个论点。",
            "⚠️ 入库成功的判据是**磁盘上真有正文**（`texts/` 与 `pdfs/` 里那两样），",
            "   不是回包里的 `fulltext=true` —— 后者只代表抽过文本，原件可能早没了。",
            "════════════════════════════════════════════",
        ])

    # ── 综述：大纲与章-文献映射开工动作 ─────────────────────
    def outline_kickoff_text(self) -> str:
        """冻结结构。这是「不要返工」那条诉求的机器落点。

        返工的机制成因是**结构与内容在同一遍里来回改**：写到第三节发现第二节
        的论点要改，回头改第二节，第三节又得重写——来回两轮，稿子就烂了。
        把大纲、章-文献映射、章-图表清单**冻结在动笔之前**，写作阶段就只剩
        "按已定结构填段落"这一件事。

        ★ 为什么把"章-文献映射"写死成必需项：没有它，写作时模型会临时抓文献，
          抓到的往往是它刚检索过、上下文里最眼熟的那几篇，而不是最适合这一章的。
          有了它，每章引谁在动笔前就定了，写的时候只需填充。
        """
        n = self.target_chars
        budget = (f"总预算约 {n} 字，按节分配、每节预算写进大纲" if n
                  else "总预算由你按任务性质定，并把每节预算写进大纲")
        return "\n".join([
            "═══ 大纲与章-文献映射（**动笔之前冻结，之后只许填充**）═══",
            "",
            f"{budget}。",
            "",
            "装载 `paper-spine` 技能并按其协议执行，产出 `outline.md` 落盘。",
            "**每一章都必须写清这四样**，缺一样不算完成：",
            "  ① 一句话论证（这一章要向读者证明什么，不是「这一章讲什么」）",
            "  ② 要引的 `doc_id` 清单（**从 kb_coverage / kb_search 的回包里照抄**，",
            "     不许凭记忆写；这一章写的时候只能引这里列出的）",
            "  ③ 本章目标字数（加起来要对得上总预算）",
            "  ④ 本章图表（要画什么；**不要图也要写「本章不配图，理由：…」**）",
            "",
            # ★ 文献下限必须**在大纲阶段就摊开**，不能在交付时才算账。
            #   交付时才说"少 20 篇"= 一次返工；大纲阶段就知道每章该有几篇，
            #   补文献和改结构是同一件事，只做一遍。这是"不要返工"的正解。
            f"★ **参考文献总数不得少于 {_REVIEW_MIN_REFS} 篇**（硬要求，交付时会核）。",
            f"  按章分摊：{self._refs_budget_line()}",
            "  大纲里每章的 ② 清单加起来必须够这个数；**不够就先补文献，再往下走**。",
            "  ⚠️ 不许为凑数把没读过的文献塞进清单 —— 后面 `verify_citation` 会逐条核，",
            "     塞进去的那几篇会当场露馅，而且要连带重写引用它们的段落。",
            "",
            "★ 大纲落盘之后，**结构就冻结了**。写作阶段：",
            "  · 只许填充，不许重排章序、不许增删章、不许改每章的论点方向；",
            "  · 真要改，先改回 `outline.md` 并说明理由，再继续写 —— ",
            "    不许在正文里悄悄改结构（那样前文和后文会对不上）。",
            "  · 每章引用的 doc_id **必须 ⊆ 该章映射清单**。写着写着发现缺一篇，",
            "    走「补一篇 → 加进该章清单 → 再引」这条路，不要直接塞进来。",
            "",
            "为什么这么严：写到第三节才发现第二节要改，会连带重写后文 —— ",
            "**来回改两轮，稿子就烂了**。结构一次性定死，是唯一能一次成稿的做法。",
            "════════════════════════════════════════════",
        ])

    # ── 文献库开工动作（needs_kb 时的**事前**告知）─────────────
    def kb_kickoff_text(self) -> str:
        """要用文献库的任务，开跑前把「库里有什么、怎么用」以**动作**讲清。

        为什么照 deck_kickoff_text 的形状再做一份：同一条款律 ——
        **模型执行的是它读到的动作**。kb_* 工具的 schema 虽然挂上了，
        但「先查库再说话」「doc_id 照抄不许编」这些动作不点名地写出来，
        模型就会按自己的默认习惯走（联网搜或凭记忆）。导师模式另有
        `mentor_mode_text` 那段更严的纪律（标注审计），那段与这段**互补不重复**：
        这里讲工具面（怎么用），那里讲诚信面（怎么标注、怎么被核）。
        """
        return "\n".join([
            "═══ 文献库开工动作（要用库的任务，先做这几步）═══",
            "",
            "本任务要用**文献知识库** —— 那是你的**私人收藏**（就库里这几篇 PDF），",
            "不是全量文献：库里没有 ≠ 世上没有。",
            "",
            "1. `kb_search(query=…)` —— **先查库再说话**。段落级检索，回包带",
            "   `hits[].doc_id` 与**页码**；这两样是后面标出处的唯一凭据。",
            "2. `kb_read(doc_id=…, page_from=…)` —— 按页读全文；回包会如实报全文总长，",
            "   截断了就明说截断了。",
            "3. 结论标出处：〔库·<doc_id> p.<页码>〕—— doc_id 与页码**照抄回包**，",
            "   不许凭记忆写、不许编。",
            "4. 要把新文献留下来：`kb_add(src=\"doi:…\")` / `kb_ingest(paths=…)` ——",
            "   **只收 PDF 原文**（Markdown 是 `convert_pdf` 的产物，给人读、写 Obsidian，",
            "   进不了库）。",
            "5. 读了有价值的东西，顺手用 `topic_write(doc=\"reading\", …)` 记进课题台账：",
            "   doc_id + 从它拿到什么 + 和课题什么关系 —— 下次会话才记得住。",
            "",
            "库里没有就先明说「库里没有」，再问要不要联网补（`literature_search` 等）。",
            "════════════════════════════════════════════",
        ])

    # ── 交稿纪律（写作任务专用的**事前**告知）────────────────
    def writing_discipline_text(self) -> str:
        """把事情在**开跑前**讲清楚，而不是等它写完了打回。

        与证据门的写作体检构成一前一后两道 —— 这是项目里已经验证有效的模式
        （写作技能门就是这么做的：契约注入协议 + deliver 时硬拦）。
        """
        tgt = (f"约 {self.target_chars} 字（由用户原话抽取，"
               f"不是估计值，±15% 以内）" if self.target_chars
               else "用户未指定，按任务性质自定并在交付时说明")
        lines = [
            "═══ 交稿纪律（内核**程序化核验**，不是写作建议）═══",
            f"目标字数：{tgt}",
            "",
            "交付方式：调用 deliver_task 时必须附上**正文全文**——",
            "  · 短稿（≤2000 字）：直接填 draft_text",
            "  · 长稿：写进工作区文件，用 draft_path 指相对路径",
            "  没有正文的写作交付会被直接打回：内核要拿它做稿面体检。",
            "",
            "硬性红线（触发即打回，无一例外）：",
            "  ✗ 正文里出现点列（- / 1. / ① / 一、）—— 成段论述就用成段散文"
            if not self.allow_lists else "  · 本任务允许清单形态，点列不作为红线",
            "  ✗ 字数偏离目标 40% 以上",
            "  ✗ 单个句子超过 120 字（拆句，不要用逗号串成长链）",
            "  ✗ 连续标点（。。/，，）、叠字笔误（的的/了了）",
            "  ✗ 空转套话密度过高（「随着…的发展」「众所周知」「具有重要意义」",
            "    「综上所述」「极大地」「备受关注」这类；它们占的是本该放证据的位置）",
            "",
            "内核会把体检报告原样回灌给你，逐条列出位置与改法。",
            "改稿时按报告改，不要重写一遍了事。",
            "════════════════════════════════════════════════",
        ]
        return "\n".join(lines)

    # ── 图面纪律（配图任务专用的**事前**告知）────────────────
    def figure_discipline_text(self) -> str:
        """画之前就把规矩讲清楚，而不是等它画完了打回。

        为什么值得单独写一段：图的失败模式和文字**不一样**。
        文字写歪了肉眼一眼看得出；图最常见的失败是
        **每个程序都退出 0、日志干净、图是错的** ——
        字体缺字渲染成豆腐块、面板差 0.8 pt 肉眼看不出来、
        字号在 PDF 里被缩到 4.9 pt、报告是上一版图的却拿来当合格证。
        这些都不报错，所以只能事前说清、事后程序化核。
        """
        n = len(self.deliverables)
        lines = [
            "═══ 图面纪律（内核**程序化核验**，不是审美建议）═══",
            "",
            "**图的失败模式与文字相反：它不报错。**",
            "字体缺字只发一条 warning 就画出豆腐块（□），退出码 0；",
            "面板差 0.8 pt 肉眼看不出来；字号在 PDF 里被缩到 4.9 pt；",
            "盘上躺着一份上一版图的绿报告。以上全部**静默**。",
            "",
            "所以本轮要产出图的话，交付前必须调用 `audit_figure`，它会跑四道审计：",
            "  ① 源码预检（字体/字号/可编辑文字/颜色/导出/数据完整性）",
            "  ② PDF 字形下限  —— 每个字形 ≥ 5 pt，含上下标（上标常被缩到 0.7 倍）",
            "  ③ 渲染后碰撞    —— 文字压文字 / 线穿字 / 出界，按最终 PDF 的几何量",
            "  ④ 多分面对齐    —— 同行面板的边、宽、高、间距，容差 1.5 pt",
            "外加：图注体检（结构/统计/套语）+ 报告新鲜度（旧报告不算通过）。",
            "",
            "**开画之前先把契约定死**（调 audit_figure 时一并提交），五点缺一不可：",
            "  1. 核心结论：这张图要捍卫的那**一句话**",
            "  2. 证据链：每个分面承担一个**不同的推断职责**",
            "     ⚠️ 两个分面画同一件事 = 必须合并或降级，不许留着凑数",
            "  3. 原型：quantitative grid / schematic-led composite /",
            "     image plate + quant / asymmetric mixed-modality",
            "  4. 后端：python / r / origin / other，**一经选定即独占**",
            "     一张图只许一个渲染引擎出图、出预览、出导出、做目视 QA。",
            "     混用两个引擎（哪怕只是拿另一个出预览）就是硬失败。",
            "  5. 导出契约：最终物理尺寸（单栏 89 / 双栏 183 mm）、可编辑文字、",
            "     位图分辨率、源数据、排除项的前后行数与理由",
            "",
            "**后端怎么选：看图的审计需求，不看引擎喜好。**（已定案，不要再逐图纠结）",
            "  · 投稿渲染路线（python / r）—— 满足任一条即**必须**：",
            "      ① 分面数 ≥ 2：对齐门要「分面几何清单」，只有 python/r 量得出来",
            "      ② 面板里画了数据（声明了 center / spread，含逐点原始值）：",
            "         要源数据可追溯 + 字形下限 + 精确物理尺寸",
            "      ③ 声明了目标期刊：要进投稿包（可编辑矢量 + 600 dpi 位图 + 源数据）",
            "  · 原生矢量图形路线（origin / other）—— 仅限：",
            "      单分面 + 面板不画数据（center / spread 留空）+ 不进投稿包 的"
            "示意 / 装置 / 流程图的图形合成。",
            "      代价明说：拿不到几何清单 ⇒ 对齐门 `NOT APPLICABLE`（单面图本就合法）；",
            "      没有绘图源码 ⇒ 源码预检 `SKIPPED`。字形下限与碰撞审计**照样跑**"
            "（那两道按最终 PDF 量，与后端无关）。",
            "      若之后加了第二个分面、或在面板里画上数据、或要投稿，"
            "这张图就升级成投稿路线，必须改用 python/r 重出。",
            "  ⚠️ 后端选错 = 契约不成立：`audit_figure` 会在**画之前**把契约打回，"
            "不用等画完才发现。也别指望「先用 Origin 画着、不行再补一份几何清单」——"
            "那会留下一个「审不了但放行」的口子，本项目不留这种口子。",
            "",
            "四条容易踩的（都是实测）",
            "  ✗ `h⁻¹` 这类上标：Arial 缺 U+207B，**渲染成豆腐块但不报错**。",
            "    改用 `per hour` 或换字体 —— 别把「没报错」当没事。",
            "  ✗ 改完图**不重跑审计**就直接交付：那是拿旧报告当合格证。",
            "    图一改，上一份报告立刻作废。",
            "  ✗ 多分面图不给分面几何清单 → 对齐门判 `NOT AUDITABLE`。",
            "    **审不了不等于通过**，会被拦下。",
            "  ✗ 为了让图好画而悄悄丢数据。排除必须记前后行数 + 规则 + 科学理由。",
            "",
            "图注（也进体检）：`Fig. N | 加粗名词短语` → 分面 a/b/c（现在时、电报式）",
            "→ 统计（n = ／误差定义／检验方法）→ 「Source data are provided as a Source Data file.」",
            f"  可交付物共 {n} 项；图的源数据 CSV 与可编辑矢量文件都要在交付包里。",
            "════════════════════════════════════════════════",
        ]
        return "\n".join(lines)

    # ── 开工动作（needs_deck 时**紧贴目标**的那一段）─────────────
    def deck_kickoff_text(self) -> str:
        """第一步做什么 —— 以**动作**命名，而不是以"纪律"命名。

        这段存在的唯一理由：模型执行它读到的动作。所以要说的不是"你要注意
        什么"，而是"**现在就调这个工具**"，并且**点名那条走错的路**。
        """
        return ("═══ 开工动作（第一步就做这个，不要先写别的文件）═══\n"
                "本任务的产物是 **.pptx**，它只能由 `ppt` 域的工具**建**出来。\n"
                "**不存在**\"把材料转成某个文档就等于做好了 PPT\"这条路：\n"
                "`import_document` / `export_docx` 产出的是 .docx / .html，\n"
                "它们**不是**演示文稿 —— 拿它们去 `open_presentation` 会直接报\n"
                "\"… is not a PowerPoint file\"。所以别在那儿停下。\n"
                "\n"
                "造稿就走这条链，一条到底：\n"
                + deck_recipe_lines() + "\n"
                "\n"
                # ⚠️ 这里原来写的是「第 4 步之前…第 6 步不过…」—— **按序号引用是错的**：
                #    配方一加步，序号就漂（写这句话时配方只有 7 步，现在是 9 步），
                #    而漂了的序号会指向一个**完全不相干的动作**。
                #    ⇒ 纪律：**按工具名引用，永不按序号引用**（名字是稳定的）。
                "在 `save_presentation` 之前把内容填完，别转去写别的文档；\n"
                "`audit_deck` 不过就不要宣布完成。\n"
                "════════════════════════════════════════════")

    # ── 组会汇报纪律（做 PPT 任务专用的**事前**告知）──────────
    def deck_discipline_text(self) -> str:
        """做 PPT 之前就把规矩讲清楚。

        为什么值得单独写一段：**"能生成 .pptx" 和"能讲"是两件事。**
        上游 MCP 服务端给你 37 个操作，它只保证文件是合法 OOXML，
        不保证这份 deck 拿去组会不丢人 —— 把论文段落整段粘上一页、
        没有讲稿备注、正文小到最后一排看不见、拿上一版的体检报告当合格证，
        这些全都**不报错**。所以只能事前说清、事后程序化核。
        """
        n = len(self.deliverables)
        lines = [
            "═══ 组会汇报纪律（内核**程序化核验**，不是排版建议）═══",
            "",
            "**能生成 .pptx ≠ 能拿去讲。** 上游 MCP 只保证文件是合法 OOXML；",
            "把论文段落整段粘上一页、没有讲稿备注、正文小到后排看不见 ——",
            "以上全部**静默**，退出码 0。",
            "",
            "交付前必须调用 `audit_deck`（传入最终 .pptx 的路径）。它会：",
            "  ① 跑技能自带的 XML 审计（越界形状 / 文字密度 / AI 套话 / 裁图 / 小图 / 对齐差一点）",
            "  ② 核**讲稿备注覆盖率** —— 组会的备注就是你的讲稿，不是装饰",
            "  ③ 核字号下限、页面总字数、标题长度、页数区间",
            "  ④ 核**报告新鲜度** —— 旧报告不算通过（改过 deck 就必须重跑）",
            "",
            "── 上游那个坑（**第一枪就会踩**，实测）──────────────────",
            "`create_presentation` / `open_presentation` **不会**把新建/打开的文稿设为",
            "「当前文稿」—— 上游那两个负责同步状态的包装函数是死代码，从未被调用。",
            "于是：create 成功之后紧接着调 `get_presentation_info`，会得到",
            "`No presentation is currently loaded` —— 看起来像「创建没生效」，",
            "其实只是**没切过去**。",
            "",
            "  ✅ 正确顺序**见本文档开头的「开工动作」**（同一份，不重抄 ——",
            "     重抄一遍就是 H 型：两处会各自漂移）",
            "  ✅ 或者给每个后续工具**显式传** presentation_id（等价，二选一）",
            "  ✗ 看到「没有已加载的文稿」就**再 create 一遍** —— 那会得到第二份",
            "     空文稿，两份都在内存里，最后存下去的是哪一份取决于你之后切没切。",
            "",
            "── 每次任务一个工作区 ────────────────────────────────",
            "MCP 子进程的 cwd 已钉在本任务工作区，所以 `save_presentation(file_path=\"deck.pptx\")`",
            "这种相对路径会落到工作区里；内核也会把已知的路径参数解析成绝对路径。",
            "**唯一例外是 `image_source`**：它收「路径**或 base64**」，内核刻意不改写它 ——",
            "所以传图时请自己给绝对路径，或传 base64 并把 source_type 设成 \"base64\"。",
            "",
            "── 三条实测最容易犯的 ──────────────────────────────",
            "  ✗ 一整段论文正文粘进一个文本框：XML 审计判 `text_overload`，",
            "    而且组会上没人会读。一页一个主张，证据用图/表/数字。",
            "  ✗ 备注栏留空：来源、口径、可能被问到的问题都该放那儿。",
            "    覆盖率不够会被拦（阈值在 config.json 的 deck.min_notes_ratio）。",
            "    ⇒ 补法是用 `set_slide_notes`（**内核提供的工具**，见开头「开工动作」），",
            "      **不是自己写 python-pptx 代码** —— 内核没有执行代码的工具。",
            "  ✗ 改完 deck 不重跑 `audit_deck`：拿旧报告当合格证。",
            "    deck 一动，上一份报告立刻作废。",
            "",
            "── 内容诚信（与四条硬门同源）────────────────────────",
            "不许编造数字、结论、图注。deck 里出现的每个数值都应能追到证据；",
            "引用文献必须真实存在。做不出来就用 `report_blocker` 如实说。",
            f"  可交付物共 {n} 项；.pptx 与它的体检报告都要在交付包里。",
            "════════════════════════════════════════════════",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskContract":
        """从冻结的 JSON 还原契约（`--contract-in` / Web 预审后开跑）。

        只取认得的字段：多余的键直接丢掉，避免旧版本导出的契约把构造器噎住。
        """
        known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        payload = {k: v for k, v in (d or {}).items() if k in known}
        payload.pop("created_at", None)
        raw = str(payload.pop("raw_goal", "") or payload.get("goal") or "")
        if not raw:
            raise ValueError("契约缺少 raw_goal（用户原话），无法还原")
        payload.setdefault("goal", raw)      # 冻结文件里缺 goal 时，用原话兜底
        for k in ("deliverables", "acceptance", "constraints", "forbidden", "stages"):
            if k in payload:
                payload[k] = _as_list(payload[k])
        if payload.get("target_chars") is not None:
            try:
                payload["target_chars"] = int(payload["target_chars"]) or None
            except Exception:
                payload["target_chars"] = None
        return cls(raw_goal=raw, **payload)

    # ── 构造 ────────────────────────────────────────────────
    @classmethod
    def from_llm(cls, raw_goal: str, payload: dict) -> "TaskContract":
        deliverables = _as_list(payload.get("deliverables"))
        goal = str(payload.get("goal") or raw_goal).strip()
        # ★ W36：**模型说了算**。它给了这个键就采信它（哪怕它说 false），
        #   只在键**不存在**时退回词表兜底 —— 那是"模型没回答"，不是"模型说不是"。
        #   这里必须用 `in` 而不是 `.get()` 的真值判断：`{"needs_writing": false}`
        #   与"没有这个键"是两件不同的事，混成一个就会让模型的否定被词表推翻。
        if "needs_writing" in payload:
            needs_writing = bool(payload.get("needs_writing"))
        else:
            needs_writing = _looks_like_writing(raw_goal, goal, " ".join(deliverables))
        needs_figure = bool(payload.get("needs_figure")) or _looks_like_figure(
            raw_goal, goal, " ".join(deliverables)
        )
        needs_deck = bool(payload.get("needs_deck")) or _looks_like_deck(
            raw_goal, goal, " ".join(deliverables)
        )
        needs_kb = bool(payload.get("needs_kb")) or _looks_like_kb(
            raw_goal, goal, " ".join(deliverables)
        )
        # ★ "在问我" 取「模型自报 **或** 文本像问句」，理由与上面四个 needs_* 一致，
        #   但这里的代价更不对称：漏判 = 一句问话被当成任务契约去要证据（就是本次
        #   用户撞上的那个死角）；误判 = 一份真任务的交付少了一道核对。
        #   所以**再**加一条硬保险：模型自报了 deliverables/acceptance 的，
        #   一律不算问话——用户要的是东西，那就还按任务走。
        explicit_work = bool(deliverables) or bool(_as_list(payload.get("acceptance")))
        answer_only = (not explicit_work) and (
            bool(payload.get("answer_only"))
            or _looks_like_question(raw_goal, goal)
        )
        return cls(
            raw_goal=raw_goal,
            goal=goal,
            deliverables=deliverables,
            acceptance=_as_list(payload.get("acceptance")),
            constraints=_as_list(payload.get("constraints")),
            forbidden=_as_list(payload.get("forbidden")),
            needs_writing=needs_writing,
            needs_figure=needs_figure,
            needs_deck=needs_deck,
            needs_kb=needs_kb,
            answer_only=answer_only,
        )

    @classmethod
    def fallback(cls, raw_goal: str) -> "TaskContract":
        """起草失败时的退化契约：目标即契约，不阻塞流程，但标注需要人工补验收标准。"""
        return cls(raw_goal=raw_goal, goal=raw_goal, acceptance=[],
                   needs_writing=_looks_like_writing(raw_goal),
                   needs_figure=_looks_like_figure(raw_goal),
                   needs_deck=_looks_like_deck(raw_goal),
                   needs_kb=_looks_like_kb(raw_goal),
                   # 起草失败（模型没回/回了坏 JSON）时也要判"是不是在问我" ——
                   # 否则一次网络抖动又能把一句问话推回"验收标准为空 ⇒ 打回"那条路。
                   answer_only=_looks_like_question(raw_goal))

    @classmethod
    def draft(cls, raw_goal: str, llm, temperature: float = 0.0) -> "TaskContract":
        """让模型把一句话目标拆成契约。起草失败不阻塞——退化为目标即契约。"""
        try:
            msg, _, _ = llm.chat(
                [
                    {"role": "system", "content": DRAFT_SYSTEM},
                    {"role": "user", "content": raw_goal},
                ],
                temperature=temperature,
            )
        except Exception:
            return cls.fallback(raw_goal)
        payload = _extract_json(msg.get("content") or "")
        if not payload:
            return cls.fallback(raw_goal)
        return cls.from_llm(raw_goal, payload)
