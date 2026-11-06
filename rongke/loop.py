# -*- coding: utf-8 -*-
"""荣珂主循环：Plan → Act → Observe → Verify。

与「对话循环」（`for _ in range(6)`）的四个本质区别：

  1. **目标不漂移** —— 契约常驻 system，并周期性重申；不得自行加码
  2. **证据链** —— 每一步都落结构化 ToolCall（seq / ok / ms / 指纹），可追溯
  3. **收敛检测** —— 同一工具同参数重复调用即判原地打转，主动注入干预
  4. **完成要过门** —— deliver_task 必须附真实证据，通不过就打回并告诉它缺什么

外加两条护栏：
  * **工作区围栏** —— 每次任务一个独立 workspace，进程 cwd 切进去，
    mutating 工具的越界绝对路径被拒（首跑实测：模型会把项目建在 cwd）
  * **诚实退出通道** —— report_blocker。干不成不许假装干成。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .bridge import install_workbench
from .citation import VERIFY_SCHEMA, verify_citation
from .config import Config
from . import context
from .contract import TaskContract, deck_recipe_lines
from .gate import CompletionClaim, CompletionGate, Verdict
from .hooks import WorkspaceFence
from .llm import ContextOverflow, LLM, LLMError, Usage
from .mcp_client import MCPHub, install_mcp
from .memory import Memory, default_root
from . import pricing as _pricing
from .stages import read_skill_text
from .subagent import SUBAGENT_SCHEMA, SubagentRunner
from . import topic
from .telemetry import TraceRecorder
from .tools import Tool, ToolCall, ToolRegistry, scope_fallback
from . import system_sections

# 做 PPT 的生产动作序列：**渲染一次，system 里引用**。
# 原文定义在 `contract.DECK_TOOLCHAIN_RECIPE`（唯一来源），契约提示词也用同一份。
# 不在 system 里另写一遍，是为了不制造"两句话要一起改"的 H 型。
# 预先算好（而不是在 f-string 里调函数）也避免嵌套引号对解释器版本的依赖。
_DECK_RECIPE_LINES = deck_recipe_lines(indent="    ")

SYSTEM = f"""你是荣珂 —— 一个科研任务的自主执行体。

工作方式：
- 你会收到一份「目标契约」。它是本轮任务的唯一准则：不得偏离，也不得自行加码。
- 你可以调用工具来获取真实信息、执行真实操作。
- **不要凭记忆编造事实**：文献、DOI、数值、结论都必须来自工具返回。
- 宣布完成前先自查：契约里每一条验收标准，我有没有对应的工具证据？
- 完成时调用 deliver_task 并附上证据（工具的 call_seq）。证据门会核验，编造会被打回。
- 用户明令「不要调用任何工具」的任务：遵守它 —— 不检索、不读写文件、不装载技能；
  交付仍走 deliver_task，evidence 留空即可（证据门对此类任务不适用）。
  **绝不许编造 call_seq 来凑证据** —— 编造在任何任务里都会被当场抓出。
- 确实做不成时调用 report_blocker 诚实说明卡点 —— 这比假装完成有价值得多。
- **用户只是在问你**（问下一步该做什么、问为什么、问要不要、问有什么区别）：
  契约里会标「本轮是提问」。这时**不要去找证据凑交付**，
  直接把回答写出来，再用 deliver_task 的 summary 交上来即可。
  门对这一类核的是「有没有真的答」，不是「有没有产物」。
  仍然照旧：不许凭记忆编造文献与数值；查过库就照旧标出处。
- **核验参考文献时逐条调用 verify_citation**：它一次给出存在性、是否已被撤稿、
  以及元数据是否与稿件写的一致。只调 fetch_doi 不够 —— 它不查撤稿。

写作任务：
- 契约里若带「阶段专用技能」清单，进入某阶段前先 `load_skill("<技能名>")`
  装载它的**全文**，然后按它做。缺了不会被打回，但**活会做糙** ——
  阶段纪律只写在技能全文里，凭记忆模仿不算数。
- 每个阶段有**唯一**的专属技能，不能用别的技能替代。
- 顺序即流水线：**润色排在起草之后** —— 全稿拼接完成才轮到 nature-polishing，
  不要边写边润色。找不到技能时如实报告，不要假装装载。
- **查到的文献要用在稿子里。** 实测踩坑（2026-11-01）：一篇 203 字引言，
  检索了 3 次、核实了 2 个 DOI，正文里却有 **0 处引注** —— 检索和核实全白做，
  「已有研究在人工培养基上获得子实体」这种话没有任何出处。
  稿子里每一条「已有研究表明 / 有研究报道」都必须指向真核验过的文献。
  **交付前自查一遍：我引用的每一句，正文里都看得见对应的引注吗？**
- **写稿是正事，检索核实是它的配料 —— 别把配料做成主菜。**
  实测踩坑（同一天，run e595cea278dc）：14 步里第 1–8 步全在 `kb_search` /
  `literature_search` / `verify_citation` / `kb_add`，**第 9 步才动笔**；
  写完体检 60/100，唯一的必修项是「引文编号未按首现顺序」——
  **只差重排一遍号就能过，但步数已经用完了，交不出来。**

  所以：**先有计划，再动手。** 开工时就想清楚这件事大约要几步、写稿占几步，
  按那个分配走，而不是"还能再做一件就再做一件"。
  检索到**够用**就停：一篇引言要 3–6 篇可靠文献，不是 20 篇；
  核实到"这条论断有出处"就够，不必把每个 DOI 的卷期页码都查一遍。
  **至少留三分之一的路程给动笔和改稿** —— 因为体检几乎总会挑出点东西，
  而改那些东西是要花步数的。
  真正写不完时，宁可交一版能过体检的短稿，也不要交一版没改完的长稿。
- **凡是要产出「真稿件」的工作（投稿 / 期刊 / 稿件 / 导出 PDF·Word / 动工作台流水线），
  开工前先 `load_skill("workbench-paper-pitfalls")`。** 那是 30 条实测踩坑清单：
  **没有一条会报错** —— 退出码 0、日志干净、门禁全绿，而产物是错的。
  最典型的三个：headless 浏览器把请求转交给已运行实例导致**根本没出文件**；
  导出图片传相对路径被 COM 静默拒绝；以及「门禁过了 ≠ 规则合规」

配图任务：
- **图的失败模式和文字相反：它不报错。** 字体缺字只发一条 warning 就画成豆腐块（□），
  退出码 0；面板差 0.8 pt 肉眼看不出来；字号在 PDF 里被缩到 4.9 pt；盘上躺着一份
  上一版图的绿报告。这些全都**静默**，所以只能靠程序化审计发现。
- 要产出图的任务，**交付前必须调用 `audit_figure`**，并附上五点图面契约
  （核心结论 / 分面各自的推断职责 / 原型 / 后端 / 导出契约）。
  图面门会拦下没有体检、体检不过、或**拿了过期报告**的交付。
- 一张图**只许一个渲染引擎**出图、出预览、出导出。混用两个就是硬失败。
- 改了图就必须**重跑** `audit_figure` —— 旧报告不认。
  （编号顺序、字数上限、图表张数这些**门禁根本不查**）。

做演示文稿（组会 PPT / 汇报幻灯片）任务：
- **产物是 .pptx，只能由 `ppt` 域的工具建出来。** 把材料转成 .docx / .html
  **不等于**做好了 PPT —— 拿它们去 `open_presentation` 会直接报
  「… is not a PowerPoint file」。别在那儿停下、也别去 `audit_deck` 它们。
- 造稿就走这条链，一条到底（第一步别迟疑）：
{_DECK_RECIPE_LINES}
- **「能生成 .pptx」和「能讲」是两件事。** 上游 MCP 只保证文件是合法 OOXML；
  把论文整段粘上一页、没有讲稿、正文小到后排看不见 —— 这些全都**静默**。
- 要产出 .pptx 的任务，**交付前必须调用 `audit_deck`**（传最终 .pptx 的路径）。
  deck 门会拦下没有体检、体检不过、或**拿了过期报告**的交付。
- ⚠️ 上游那个坑：`create_presentation` / `open_presentation` **不会**把文稿
  设为「当前」 —— 必须紧接着调 `switch_presentation(presentation_id=…)`，
  否则后续调用会报「没有已加载的文稿」，看起来像创建失败。
  **看到那句报错不要再 create 一遍**（会得到第二份空文稿）。
- 讲稿备注：**上游 37 个工具一个都没有这个能力**（`grep notes` 零命中）——
  用内核提供的 `set_slide_notes(pptx, notes=[每页一段])` 补。**不要自己写
  python-pptx 代码**：你没有执行代码的工具，写出来也跑不了（实测踩过，
  一次任务为此烧掉 19 次调用、最后零交付）。
- 外观：**内容齐不等于拿得出手**；**换色换字也不等于版面好看**。
  ★★ **有品牌模板时一律走上面那串编号动作里的「路线甲」**（学校/学院给了模板就是这种情形）：
  它**取代**建页那一整段，出来的就是学校的样子 —— 品牌装饰一个字节都不重绘。
  下面这套 `compose_deck` / `style_deck` 只是**近似品**，**只在没有模板时才用**。
  （动作细节与注意事项全在**路线甲那一条**里，这里是强调、不是另写一份 ——
   同一句话抄两处就是 H 型。）
  **动手之前先调一次 `deck_reference()`**：它给一份随库发布、五门全绿的
  参考样例（12 页、六种页型全用到、逐页正文与讲稿备注），
  照着它的**页型序列**与**每页论点密度**做，比任何形容词（「简洁大气」）都管用。
  存盘之后，在下面两条里**挑一条**（不要两条都调 —— 两套坐标系会打架）：
  · **要交付上组会 ⇒ `compose_deck(pptx=\"deck.pptx\", pages=[逐页页型])`（精排）**。
    你只交页型（cover/section/content/two-col/chart/closing），内核按设计规范
    把整份版面重排：三区网格、字号阶梯、朱砂强调线、页脚落款与页码。
    **这是「能上组会」该走的最后一步。**
  · 只想快速起手打扮 ⇒ `style_deck(pptx=\"deck.pptx\")`（快套）。
    它只换主题色与字体、**不动版面骨架** —— 所以光靠它，版面还是那副版面。
  不套的后果是**硬门会拦** —— 体检把「4:3 画布」与「纯默认外观」判成 FAIL。
  ⚠️ 别用 `apply_professional_design(operation=\"theme\")`：那是**空壳**，
  回 `success: true` 而文件一个字节都不变（实测 theme1.xml 的 md5 没变）。
- 改了 deck 就必须**重跑** `audit_deck`。
  契约里若已列出「作业纪律」阶段，装载它是硬性要求，门会拦。
- **交稿时必须附上正文全文**：短稿（≤2000 字）填 `draft_text`；
  长稿写进工作区文件、再用 `draft_path` 指相对路径。
  内核会读正文做**稿面体检**（字数/段落/句长/套话密度/点列/标点/引注/写作姿态），
  硬问题会打回并逐条给出改法。所以**交稿前自己先读一遍**：
  字数对不对得上目标、有没有把论点写成 bullet、有没有用「随着…的发展」这类空转句。
  体检报告说哪句太长就拆哪句，说哪处是套话就换成具体事实 —— 不要重写一遍了事。
- **写作姿态也是体检项**（`defensive_*`）。稿子里出现「需要指出的是」「为了保证公平」
  「初步结果表明」「不应被理解为」「我们未开展 X，因为…」这类句子时，体检会以
  **建议**（不是必修）列出来。它们的问题不是空，而是**在跟一个不在场的审稿人吵架**：
  先立一个没人提过的靶，再宣布自己不打。
  改法是**只改姿态、不删事实**：
  · 判断某句该不该删，只有一条标准——删掉后重读，读者有没有丢失
    **事实、数字、条件、逻辑链**？丢了就不许删。
  · 「需要指出的是，X」→「X」；「为了保证公平，所有方法用同一主干」→「所有方法用同一主干」。
  · 含数字 / 伦理批准 / 数据可用性 / 利益冲突 / 真实局限的句子，**只许换说法，不许删**。
    体检会标出这类句子（`defensive_protected`），标了就别删。
  · **绝不许**为了让话听起来更强而删掉数字、条件、阴性结果，
    也不许把「可能」改成「确定」、把「有界个案」改成「普遍规律」。
    主张强度不得超过证据 —— 这条优先于上面所有改法。

工作区纪律：
- 你当前的工作目录就是本次任务的 workspace。**把产物写在这里**（用相对路径）。
- 不要往 workspace 之外写任何东西，越界的绝对路径会被围栏拒绝。

工具使用：
- 参数必须符合 schema，写错会直接失败（失败信息会告诉你哪里错）。
- **读输入要认对门**（走错门**不会**告诉你"此路不通"，只会回一个看不懂的错，
  于是你会以为是自己路径写错了，然后一直猜下去 —— 实测踩过）：
  · 工作区里的**文本**文件（.md / .txt / .json / .csv…）⇒ `read_file`
  · **pdf / docx** ⇒ `import_document`（它**只吃这两种**；喂 `.md` 会回
    「不支持的格式: md（仅 pdf/docx）」）
  · 网址 ⇒ `web_extract`（喂本地路径会回 `invalid_extract_url`）
- 同一个工具用同样参数反复调用是无效的；发现打转就换策略。
"""

# 统计审查专属提示段（唯一来源）。只由 `stats-review` 预设点名时追加进 system：
# 其余任务一个字都不带 —— 对应 SkillsInjector 那条「按任务分配技能上下文」，
# 也避免把审查清单塞进一场只是写引言的任务里制造噪音。
# 与预设的 4 个门禁指标（absolutes / placeholders / defensive_hits /
# tail_repeat）和 stats、units 两道工序逐条对得上，不是一句空泛的「注意统计规范」。
STATS_CHECKLIST = """统计审查清单（本轮启用）：
- 主张强度不得超过证据：出现「一定 / 必然 / 证明 / 首次 / 完全一致」时，逐条回查它的数据来源与样本量，改不动就删主张、别删限定。
- 不许留占位符：XXX / TODO / 待补 / [数字] / <待填> 出现即视为未完稿，交付前全部清掉。
- 量纲与不确定度：每个数值带单位；写清 n、误差棒是什么（SD/SEM/CI）、用了哪种检验、有没有做多重比较校正；p 值别只写「显著」。
- 不在场的审稿人：「需要指出的是」「为了保证公平」「不应被理解为」这类防御性句式只换说法不删事实；含数字、伦理批准、数据可用性、真实局限的句子标为 protected，一个字不许删。
- 结尾不复述：结论段不得把结果段的原句再抄一遍，要给推断边界与下一步。"""

NUDGE = """你上一轮没有调用任何工具，也没有提交完成声明。

请二选一：
  1. 继续工作 —— 调用工具推进；
  2. 调用 deliver_task 提交完成（附证据），或调用 report_blocker 说明阻塞。

不要只是描述你打算做什么。"""

# 收尾提示。为什么必须存在：一场任务此前只有 `remind_every=8` 重申目标，
# 那句话**与剩余量无关** —— 于是"还剩 3 步"和"还剩 30 步"收到的是同一个提示，
# 等于没有提示。实测代价：四场 PPT 任务分别烧到 1,142,001 / 1,012,234 /
# 737,357 / 355,386 输入 token，全程被人工取消，内核一次都没提醒过该收尾。
WRAPUP = """【预算告急 —— 请开始收尾】

剩余步数不多了。从现在起请：
  1. **停止开启新的检索/读取**（那些不会变成交付物）；
  2. 用你已经拿到的材料把交付物**做完整**，哪怕粗糙；
  3. 尽快调用 deliver_task 提交（附证据）。

宁可交一份朴素但完整的产物，也不要留一堆开了一半的口子。"""

# prompt 压力提示。与 WRAPUP 是两件事：一个说"步数不够"，一个说"上下文不够"。
# 合并成一条会让模型不知道省哪 —— 少发提示省不了任何字节，只会让它猜错方向。
PRESSURE = """【上下文额度吃紧】

当前这轮请求的 prompt 已接近上限。请：
  1. 不要再调用会返回**大段正文**的工具；
  2. 需要旧内容时，凭台账里的 seq 精确取，不要重新全量检索；
  3. 优先把已有材料整理成交付物并提交。"""

DELIVER_SCHEMA = {
    "type": "object",
    "properties": {
        "deliverables": {
            "type": "array", "items": {"type": "string"},
            "description": "你实际交付了哪些产物",
        },
        "evidence": {
            "type": "array",
            "description": "支撑完成的工具调用证据。每一条都必须指向你**真实调用过**的工具",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "description": "产生该证据的工具名"},
                    "call_seq": {"type": "integer", "description": "该次调用的台账序号 seq"},
                    "shows": {"type": "string", "description": "它证明了什么"},
                    "acceptance": {"type": "string", "description": "对应契约里的哪条验收标准（原文）"},
                },
                "required": ["tool", "call_seq"],
            },
        },
        "summary": {"type": "string", "description": "一句话总结你做了什么"},
        "draft_text": {
            "type": "string",
            "description": ("**写作任务必填**：交付的正文全文（≤2000 字直接粘贴在这）。"
                            "内核会对它逐项做稿面体检（字数/结构/句长/套话/标点/引注），"
                            "体检不过会被打回。非写作任务留空。"),
        },
        "draft_path": {
            "type": "string",
            "description": ("长稿（>2000 字）的替代方案：用**相对本工作区**的路径指向稿件文件，"
                            "内核会读它做体检。draft_text 与 draft_path 至少给一个。"),
        },
    },
    "required": ["deliverables", "evidence", "summary"],
}

BLOCKER_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "卡在哪里（具体）"},
        "tried": {"type": "array", "items": {"type": "string"}, "description": "已尝试过什么"},
        "need": {"type": "string", "description": "需要什么才能继续"},
    },
    "required": ["reason"],
}


# run() 会切换进程 cwd（chdir 是进程级状态），并发调用会互相踩。
# 内核用这把锁保证同一时刻只跑一个任务 —— Web 控制台因此可以放心用线程。
_WORKSPACE_LOCK = threading.RLock()

# 事件归档上限：防一次超长任务把 session.json 写成几十兆。
_MAX_ARCHIVE_EVENTS = 600

# ── 对话快照的文件名（**单源**，2026-09-21） ──────────────────────────────
# 为什么要单源：这套名字原来散在三个地方 —— 写快照的 `_snapshot`、成功收尾的
# 改名、以及 `web/server.py` 里那份"界面上不该露出来的内部账本"白名单。
# 三处各写一遍就是 H 型（改一处忘一处，必然静默漂移）：改名的目标名一旦和
# 白名单不一致，快照就会**伪装成用户交付的文件**出现在产出物清单里。
# 所以白名单从这份元组**算出来**，不手抄。
#
# 语义：成功 ⇒ 规范名消失（"能不能接着跑"的判据自动失效）、内容改名留下。
SNAPSHOT_RENAMES: tuple[tuple[str, str], ...] = (
    ("conversation.json", "conversation.done.json"),
    ("conversation.json.tmp", "conversation.done.json.tmp"),
)
# 界面上要藏起来的内部分件 = 改名前后两套名字的并集。
# 写成**函数**而不是一句推导式：`hygiene_test.py` 要能拿一对假名字喂进来、
# 证明"它确实随输入变" —— 否则那条"单源"断言只是在复述我自己的写法。
def _internal_names(pairs) -> frozenset[str]:
    return frozenset(_n for _pair in pairs for _n in _pair)


SNAPSHOT_INTERNAL: frozenset[str] = _internal_names(SNAPSHOT_RENAMES)


def _unpack(tc: dict) -> tuple[str, dict, str, str]:
    """拆一次工具调用 → `(name, args, tcid, parse_error)`。

    `parse_error` 非空 = **模型的 arguments 根本没能解析成对象**。

    为什么非要把它带出来（原实现只是 `except Exception: args = {}`）：
    那条静默兜底会把「**我没能读懂你的参数**」说成「**你没给参数**」。
    实测（runs/20260921-113729）：模型某次 `import_document` 的 arguments
    流到 `"kind": "pdf</` 就断了（未闭合），解析失败 ⇒ `args={}` ⇒
    报回「缺少必填参数：file / 你传的是：（一个都没传）」。模型**明明传了**，
    于是它收到一条**自相矛盾**的反馈，无从下手。

    这两种情况的下一步动作**完全不同**：参数坏掉 ⇒「重发一次完整 JSON」；
    真的没传 ⇒「补上这个字段」。把它们压成同一个口径就是 M 型（口径塌缩）。
    """
    fn = tc.get("function") or {}
    name = str(fn.get("name") or "")
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        args, err = raw, ""
    elif raw is None or (isinstance(raw, str) and not raw.strip()):
        args, err = {}, ""                      # 真的没传 —— 交给必填校验去说
    elif isinstance(raw, str):
        try:
            args, err = json.loads(raw), ""
        except Exception as e:
            args = {}
            # 诊断在这里成形：**只有这里同时握着原文和异常**。
            # 给出长度 + 尾部原文，是因为实测的坏法就是"流到一半断了"，
            # 尾部那几十个字符才是模型唯一能据以自纠的证据。
            err = (f"{type(e).__name__}: {e}\n"
                   f"   你给的原文共 {len(raw)} 字符，尾部 160 字符：…{raw[-160:]}")
    else:
        args, err = {}, f"arguments 既不是字符串也不是对象（是 {type(raw).__name__}）"
    if not isinstance(args, dict):
        args, err = {}, (err or f"arguments 解析出来不是 JSON 对象"
                                f"（是 {type(args).__name__}）")
    return name, args, str(tc.get("id") or ""), err


#: 检索类工具：回包是"一串命中"，硬切会切掉「有哪些篇」这段索引。
_INDEX_TOOLS = {"kb_search", "kb_list", "kb_rank", "kb_bibtex"}
#: 索引里每条命中最多占多少字（`- kb123 p.4 《标题》` 这种一行）。
_INDEX_LINE_CHARS = 90


def _index_head(body: str, limit: int) -> str:
    """把「命中清单」提到截断之前，再让摘要吃剩下的字数。

    为什么必须这么做（2026-11-03 实测，`docs/导师模式实测-2026-11-03.md` 第十节）：
      一次 `kb_search` 完整回包 9924 字、10 条命中；旧实现**从头硬切** 6000 字，
      模型看到的前 800 字里只落进 2 条命中的 doc_id/page ——
      剩下 8 篇它**连编号都不知道**，提示词却还在教它"看 snippet 对不对题"。
      这是 M 型缺陷：**没看见**被渲染成了**不存在**。

    新形状（顺序即优先级）：
      ```
      【命中清单 n=N】   ← 全部 N 条，一行一条，绝不被摘要挤掉
      1. kb12345678  p.4  《标题》
      …
      【摘要节选】       ← 只放得下的前几条 snippet
      ```
    清单行如果自己就超预算，按行数比例截并**明说截了几条** ——
    「没列出来」与「不存在」是两件事，这是本仓库的老债。
    """
    try:
        payload = json.loads(body)
    except Exception:                      # noqa: BLE001  不是 JSON：退回原来的硬切
        return ""
    if not isinstance(payload, dict):
        return ""
    rows = list(payload.get("hits") or []) + list(payload.get("refs") or [])
    if not rows:
        return ""
    lines: list[str] = []
    for i, h in enumerate(rows, 1):
        if not isinstance(h, dict):
            continue
        did = str(h.get("doc_id") or h.get("id") or "").strip()
        if not did:
            continue
        pg = h.get("page")
        # 页码缺失要**写出来**，不能省 —— 省了会被读成"这篇没有页码"
        where = f"p.{pg}" if pg not in (None, "") else "p.?"
        title = str(h.get("title") or "").replace("\n", " ")[:52]
        year = str(h.get("year") or "")
        line = f"{i}. {did}  {where}  《{title}》"
        if year:
            line += f" {year}"
        lines.append(line[:_INDEX_LINE_CHARS])
    if not lines:
        return ""
    head = (f"【命中清单 n={len(rows)}，共 {len(lines)} 条可列】\n"
            + "\n".join(lines))
    if len(head) > limit:
        # 只列得下一部分：**必须自报**截了几条
        keep, used = [], len(f"【命中清单 n={len(rows)}】\n")
        for ln in lines:
            if used + len(ln) + 1 > limit - 40:
                break
            keep.append(ln)
            used += len(ln) + 1
        head = (f"【命中清单 n={len(rows)}，这里只列了前 {len(keep)} 条 —— "
                f"其余 {len(rows) - len(keep)} 条请用 kb_search 收窄条件或直接 kb_read】\n"
                + "\n".join(keep))
    return head


def _observation(call: ToolCall, limit: int) -> str:
    body = call.result if call.ok else f"[工具执行失败] {call.error}"
    total = len(call.result or "")
    if len(body) > limit:
        head = _index_head(body, int(limit * 0.45)) if call.name in _INDEX_TOOLS else ""
        if head:
            # 清单 + 摘要：清单已经占掉一部分，剩下的给 snippet
            rest = limit - len(head) - 60
            if rest > 200:
                body = head + "\n\n【摘要节选】\n" + body[len(head):][:rest].lstrip("\n")
            else:
                body = head
            tail = (f"\n…（已截断，原始 {total} 字符；上面的【命中清单】是完整的）"
                    if len(head) + 200 < limit else
                    f"\n…（已截断，原始 {total} 字符）")
        else:
            tail = (f"\n…（已截断，原始 {total} 字符）\n"
                    "要接着看这段的后半，请换更小的范围再调一次"
                    "（检索加 `n` 收窄、读文件用 `offset/limit`、读文献用 `page_from/page_to`）")
        body = body[:limit] + tail if not head else body + tail
    return f"[seq={call.seq} {call.name} {'ok' if call.ok else 'ERR'} {call.ms}ms]\n{body}"


def _messages_chars(messages: list[dict]) -> int:
    """消息体的字符数 —— 上下文占用的代理指标。

    ★ 口径**单源**在 `context.messages_chars`（那里逐字解释了为什么用字符数而不是
      token）。这个壳只保留旧名字：`replay_deck` / 控制台 / 一堆测试都在调它。
      留两份同口径的实现迟早漂移 —— 而"压到多少字符"这件事的全部意义，就是压缩前后
      两个数说的是同一种东西。
    """
    return context.messages_chars(messages)


def _reminder(contract: TaskContract) -> str:
    acc = "\n".join(f"  {i}. {a}" for i, a in enumerate(contract.acceptance, 1)) or "  （无）"
    return (
        "【目标重申 —— 防止漂移】\n"
        f"原始目标：{contract.raw_goal}\n"
        f"验收标准：\n{acc}\n"
        "请确认你当前的工作仍然指向这些目标。"
    )


def workspace_listing_text(ws: "Path | str", goal: str = "",
                           limit: int = 40) -> str:
    """把工作区**现有文件**如实列给模型（含目录、含大小、超限如实说）。

    为什么这一条必须存在（2026-09-21 实测的 40 步空转，血债）：
      用户在控制台上说「**材料区**那份《组会汇报素材》整理成 PPT」。
      契约**忠实地**把这句话原样重述（这是对的，不许改写用户原话）。
      但控制台在提交时已经把材料区 `_inbox` **改名**成了本次工作区（零拷贝）
      ⇒ **`材料区/` 这个路径在任务期根本不存在**。
      模型于是开始猜文件名：`材料区/X.md` → `.docx` → `.pdf` → `materials/` →
      `input/` → `../材料区` → `../../..` → `C:/Users/…/workbench` ……
      实测 **58 次 `import_document` 全部 `文件不存在`**，40 步里一步没用在正事上，
      连一个 `add_slide` 都没发出去。

    ⇒ 结论：**「让模型看见目录」，而不是「指望它猜对」。**
      模型没有 ls 能力（它连 `read_file` 之前都得先知道文件名），
      所以"工作区里有什么"是**内核必须提供的事实**，不是模型该自己发现的东西。
      代价几十个字节，换掉的是整类「找不到文件」空转。

    ★ 两条纪律写在这里：
      ① **列表里没有的文件就是不存在** —— 明说不许靠换后缀/换目录去猜；
      ② **截断必须如实说**（"只列了前 N 个"），否则模型会把"没列出来"读成"不存在"。
         —— **「没显示」与「不存在」是两件事**，这正是 M 型缺陷的老家。

    `goal` 只用来决定要不要多说一句「材料区」的澄清：用户口中那个词是
    **提交前**的叫法，任务期已经不存在这个路径了。
    """
    p = Path(ws)
    try:
        entries: list[str] = []
        for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
            try:
                if c.is_dir():
                    n = sum(1 for _ in c.iterdir())
                    entries.append(f"  {c.name}/         （目录，{n} 项）")
                else:
                    entries.append(f"  {c.name}         {c.stat().st_size} B")
            except Exception as e:                       # 权限/竞态：如实说读不到
                entries.append(f"  {c.name}         （读属性失败：{type(e).__name__}）")
    except Exception as e:
        return (f"═══ 本任务工作区 ═══\n路径：{p}\n"
                f"⚠️ 列目录失败（{type(e).__name__}: {e}）—— 请先用 read_file 之类的工具自己确认真实路径。\n")

    shown, more = entries[:limit], max(0, len(entries) - limit)
    body = "\n".join(shown) if shown else "  （空 —— 工作区里一个文件都没有）"
    tail = (f"\n  …… 还有 {more} 项**没列出来**（不是不存在；要看全请用列目录的工具）"
            if more else "")
    lines = [
        "═══ 本任务工作区（你的 cwd，也是围栏边界）═══",
        f"路径：{p}",
        f"现有条目（{len(entries)} 项）：",
        body + tail,
        "",
        "⚠️ **上面没列出来的文件就是不存在** —— 不要靠换后缀（.md→.docx→.pdf）"
        "或换目录（input/ materials/ ../）去猜路径；猜出来的调用只会得到「文件不存在」，"
        "白耗步数。需要什么就直接 `read_file` 读上面这些名字"
        "（pdf/docx 才用 `import_document`）。",
    ]
    if "材料区" in (goal or ""):
        lines += [
            "",
            "※ 你看到的「材料区」是**提交之前**的说法：材料在提交时已被**移进本工作区**"
            "（零拷贝改名）。所以**工作区里没有 `材料区/` 这个路径** —— "
            "材料就是清单里那些文件本身，用它们的**实际名字**直接读。",
        ]
    return "\n".join(lines) + "\n"


def initial_messages(contract: TaskContract,
                     ws: "Path | str | None" = None,
                     topics_root: "Path | str | None" = None,
                     kb_only: bool = False,
                     topic_slug: str = "",
                     caps=None,
                     assembly_out: "dict | None" = None) -> list[dict]:
    """开跑时的两条消息：system + 第一条 user。

    `assembly_out` 只用于把**这一次装配的事实**带出去发事件（不改提示词）：
    system 里放不下给界面看的元数据，而「这一场到底裁了哪些段、省了多少字」
    必须能被读回来，否则又变成「配置在场冒充已生效」。传 None 时行为不变。

    单开一个函数（而不是在 `_loop` 里内联两个字典）是为了**可测**：
    提示词里到底注入了什么，是本项目最容易悄悄漂移、又最难被发现的地方 ——
    它没有异常、没有日志、跑起来一切正常，只是模型从此开始瞎猜。
    抽出来之后，测试可以直接调它、断言"清单里有没有真文件名"，
    以及"换成真跑时 `_loop` 用的是不是它"。

    `topics_root` 只为**可测**而存在：课程台账住在 `<项目根>/topics/`，
    单测要能指向临时目录，否则它就得往真账上写。

    ★ `kb_only`（W22-B 导师模式）走的是**同一条定律**：「这个事实，模型有办法
    自己得到吗？」—— 「现在这个会话开着导师模式」模型**无从得知**（它没有开关的
    读数，界面上那个按钮是人的，不是它的）。得不到的就是内核必须提供的，
    所以这个状态**必须**注入首条消息，否则前端那个开关就是一个装饰。
    默认 `False` ⇒ **一个字都不注入**：没开导师模式的会话不许被牵连（工单第 1 节
    第 1 条「每会话一个开关」+「默认关」）。

    ★ `topic_slug`（W28）：这一场会话的课题身份。为空 ⇒ 退回 `topic.current()`
    （老行为一字不差）。非空时**只认它** —— 它与 `mentor_mode` 事件、快照里
    `topic` 是**同一个值**（同一个 `_sess_topic`），于是"首条上下文说的课题"
    与"历史回放复原的课题"永远是同一个。两处各读一次 `CURRENT` 就会出现
    提示词说 A、事件记 B 的静默分叉。
    """
    text = contract.to_prompt()
    if ws is not None:
        # `raw_goal` 走 `getattr`：真契约**一定有**这个字段（`to_prompt()` 里无条件用它），
        # 但这条助手也可能被喂进"契约形状"的替身。提示词组装**不该**因为一处可选读取
        # 而把整趟任务打崩 —— 那种误伤的代价，远大于少一句「材料区」澄清的代价。
        text += "\n\n" + workspace_listing_text(
            ws, goal=getattr(contract, "raw_goal", "") or "")
    # ★ 课题上下文（导师模式 P1/P2）：**跨会话**的事实，模型自己拿不到 ⇒ 必须内核给。
    #   与 `workspace_listing_text` 同一条定律：**「这个事实，模型有办法自己得到吗？」**
    #   得不到的，就是内核必须提供的，而不是模型该自己发现的。
    #   没有当前课题时 `context_text` 返回空串 —— **不注入空壳**：
    #   空壳承诺了"这里有内容"，却什么都给不出来，比不给更糟（稿件台那处踩过同一个坑）。
    tctx = topic.context_text(root=topics_root,
                              goal=getattr(contract, "raw_goal", "") or "",
                              slug=topic_slug)
    # 形态先于内容：课题块再长，回答也不该变成一张表。
    text += "\n\n" + answer_shape_text()
    if tctx:
        text += "\n\n" + tctx
    if kb_only and topic_slug:
        meta = topic.read_topic(topic_slug, root=topics_root) or {}
        ids = meta.get("library", [])
        if ids:
            text += ("\n\n本课题已关联的共享库文献ID：" + "、".join(ids) +
                     "\n优先检索这些文献，必要时仍检索全库补足证据。"
                     "若关联ID已不在共享库，如实说关联失效；不要把它说成整个库为空。")
    # ★ 导师模式（W22-B）：开关是**会话级**的，事实由内核说给模型听。
    #   关掉时这里一个字都不加 —— 判据是"提示词里不许出现那段"，两个方向都要成立。
    if kb_only:
        text += "\n\n" + mentor_mode_text()
    # ★ 按任务追加提示段：只有预设点名了「有文本」的段才拼进 system。
    #   baseline / 不带预设时 sections 里没有它 ⇒ 返回的就是 SYSTEM 原对象，
    #   逐字节等价，旧行为一字不动。这里用 getattr 降级：续跑与测试夹具会
    #   传轻量替身，读不到 flag 就当没有段，绝不让提示词组装把任务打崩。
    # ── 按任务装配 SYSTEM（2026-11-05 内核优化）──────────────────────
    # 学自 Anthropic 的渐进披露：**开场一次**就把本任务用不上的段丢掉，
    # 之后整场冻结 —— 不学 ITR 的「每步重算」，因为 prompt caching 要求
    # 前缀逐字节稳定（本文件 4960 行自己记着「SYSTEM + 工具 schema 是
    # 前缀缓存最值钱的对象」）。省的是「根本用不着的段」，不是「每步重算」。
    #
    # baseline / 不带预设 ⇒ caps 是空的 ⇒ needed=[]、complete 全 False ⇒
    # assemble() 返回 SYSTEM **原对象**，连拼接都不做 ⇒ 逐字节等价。
    _needed: list[str] = []
    _collect = getattr(caps, "system_sections", None)
    if callable(_collect):
        _needed = list(_collect() or [])
    _complete_text = ""
    _ct = getattr(caps, "system_complete", None)
    if callable(_ct):
        _complete_text = str(_ct() or "")
    _cf = getattr(caps, "complete_flag", None)
    _complete = bool(_cf()) if callable(_cf) else False
    _keep: list[str] = []
    _kf = getattr(caps, "keep_sections", None)
    if callable(_kf):
        _keep = list(_kf() or [])
    if _complete and not _complete_text:
        # dsh complete:true 的荣珂形态：结构性覆盖 = 只发常驻节。
        # ★ 2026-11-05 真 HTTP 读回的退化：strict-v2 自己声明 scenes=["writing"]，
        #   接活后 system 6749→1893，`load_skill` 那段写作流水线整个消失。
        #   所以允许预设显式点名「哪几节不许丢」，由通用贡献点提供。
        #   走 `needed` 分支而不是把拼好的文本塞进 `complete_text`：装配出来的
        #   sections/dropped/saved 才是**真实事实**，事件不会把「保留了 1 节」
        #   谎报成「所有节都丢了」。能力自带 SYSTEM_COMPLETE 全文时则不覆盖它。
        for _s in _keep:
            if _s not in _needed:
                _needed.append(_s)
        if not _needed:
            # 一节都没点名 ⇒ 用 minimal_system 强制只发骨架；
            # 否则 assemble(needed=[]) 会返回全量 SYSTEM，覆盖就不成立了。
            _complete_text = system_sections.minimal_system(SYSTEM)
    try:
        _asm = system_sections.assemble(SYSTEM, needed=_needed,
                                        complete_text=_complete_text)
    except (ValueError, AssertionError) as e:
        # 节名拼错 / 锚点漂移：**不许把任务打崩**，也不许静默降级。
        # 退回全量，并把理由写进装配事实，由事件说出口（拦沉默）。
        _asm = system_sections.assemble(SYSTEM)
        if assembly_out is not None:
            assembly_out["degraded"] = True
            assembly_out["error"] = str(e)
    system_text = _asm.text
    if assembly_out is not None:
        assembly_out.update(_asm.to_dict())
        # `to_dict()["complete"]` 只表示「由外部全文整体替换」；结构性覆盖还有
        # 「内核骨架 + 点名保留」这一路，单独记一个字段，界面/判据才不会把
        # True 读成 False（拦沉默，不拦选择）。
        assembly_out["structural_override"] = bool(_complete or _complete_text)
        assembly_out["kept_sections"] = list(_needed)
        assembly_out["preset"] = str(getattr(caps, "preset_id", "") or "")
    _flag = getattr(caps, "flag", None)
    _sections = (list(_flag("prompt", "sections", []) or [])
                 if callable(_flag) else [])
    # 通用消费点：段正文可以来自内核单源，也可以来自**本次挂载的外部能力**。
    # 预设没点名就返回 SYSTEM 原对象 ⇒ 逐字节等价，旧行为一字不动。
    # 原来这里只有一个 `if "stats-checklist"`，等于「提示段槽写死成一个」，
    # 别的 agent 的方法论文本根本进不来 —— 这就是「吸收长处」的真实缺口。
    _builtin = {"stats-checklist": STATS_CHECKLIST}
    _peek = getattr(caps, "prompt_texts", None)
    _provided = _peek(_sections) if callable(_peek) else {}
    for _name in _sections:
        _t = _provided.get(_name) or _builtin.get(_name)
        if _t:
            system_text += "\n\n" + _t
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": text},
    ]


# ── 导师模式（W22-B，2026-10-01）───────────────────────────────────
# 用户定死的三条纪律：**每个会话一个开关**（不是全局模式）·
# **优先用库 → 库里不够允许联网补 → 必须标注哪句来自库、哪句来自外网** ·
# **库里没有就明说**，再联网补并标注。
#
# ★★ 为什么这里除了提示词还跟了一整套"从调用台账算"的判据：
#   只把纪律写进提示词 = **靠模型自觉**。本项目记过两次同型失败 ——
#   「打红面 99.6% 与 0% 是同一种失败」，以及 `gate.py` 的既有纪律
#   「判定全部**程序化**，不采信模型的自我陈述」。
#   所以"标没标出处、标的是不是本次真命中的那一页"必须能**不看自然语言**、
#   只从 `ToolCall` 台账里算出来（见 `mentor_trace_audit`）。
#   分工：提示词负责**人看得懂**，台账负责**判它做没做**。
MENTOR_MARK = "═══ 导师模式（本次会话已开启）═══"
MENTOR_OFF_MARK = "═══ 导师模式已关闭（本条之后恢复正常）═══"

# 判据用的两组工具名。**单源**：提示词里的名字和判据里的名字从同一份元组来，
# 两处各写一遍就是 H 型（改一处忘一处 —— 比如日后把 web_search 换名，
# 判据还按旧名找，于是"标注了外网却查不到联网调用"会被误判成违规）。
MENTOR_KB_TOOLS = ("kb_search", "kb_ask")
# ★ W27：`kb_read` 单列一支。它**不是检索**（不进 `MENTOR_KB_TOOLS`，
#   否则"查库次数"会把"读某一页"也算成检索），但它是**页码级依据** ——
#   读了第 4–6 页就可以引第 5 页。判据见 `mentor_kb_hits` 的 `read_pairs`。
MENTOR_READ_TOOL = "kb_read"
MENTOR_NET_TOOLS = ("literature_search", "web_search", "web_extract",
                    "fetch_doi", "build_references")

# 库内出处标记：〔库·kb0688fb3f p.9〕 / (库 kb0688fb3f p9) / [库·kb0688fb3f·p.9]
_MENTOR_CITE_RE = re.compile(
    r"库\s*[·・.、]?\s*([A-Za-z][A-Za-z0-9_\-]{2,39})\s*[·、,]?\s*[pP]\.?\s*(\d{1,5})")
# 联网补的标记：〔外网·OpenAlex〕 / （外网 · web_search）
_MENTOR_EXT_RE = re.compile(r"外\s*网\s*[·・]\s*([A-Za-z0-9_\-\.]{1,40}|[^\s，。；）)]{1,20})")

# ── 出处抽取（2026-11-04 结构收敛）────────────────────────────────────
# 实测 B 场：模型把 doc_id 写在条目标题里、页码另起一行写「· p.1」，
# 规范正则一处都读不到，旧判据却把它写成「用了不说，等于没依据」——
# 那是「读不到」被渲染成了「不符合」。这里把「出处」变成一件可解释的事：
# 要么显式标记，要么按 doc_id 辖区认页码，两种都带 form 供追溯。
_DOC_ID_RE = re.compile(r"\b(kb[0-9a-f]{6,})\b")
_PAGE_RE = re.compile(r"(?<![A-Za-z0-9])[pP]\.?\s*(\d{1,5})\b")


def extract_citations(text: str) -> list[dict]:
    """出处抽取的**唯一实现**：规范标记 + 散文式标注，按 (doc_id,page) 去重。

    · `tight` —— 〔库·doc p.N〕这类显式标记，最可信；
    · `prose` —— doc_id 写在句子里、页码写在它后面的辖区里（到下一个 doc_id 为止）。
      辖区上限 800 字、只认 id 之后不认之前，不许跨条目乱配。

    ⚠️ 已用 `tight` 标过的 id 不再走辖区法，否则同一段里别的页码会被
       误配成这个 id 的引注（判据在正确的稿子上报错，比读不到更坏）。
    """
    s = str(text or "")
    out: list[dict] = []
    tight_ids: set[str] = set()
    for m in _MENTOR_CITE_RE.finditer(s):
        tight_ids.add(m.group(1))
        out.append({"doc_id": m.group(1), "page": int(m.group(2)), "form": "tight"})
    spans = list(_DOC_ID_RE.finditer(s))
    for i, m in enumerate(spans):
        if m.group(1) in tight_ids:
            continue
        end = spans[i + 1].start() if i + 1 < len(spans) else len(s)
        seg = s[m.end():min(end, m.end() + 800)]
        for pm in _PAGE_RE.finditer(seg):
            out.append({"doc_id": m.group(1), "page": int(pm.group(1)),
                        "form": "prose"})
    seen: set[tuple[str, int]] = set()
    uniq: list[dict] = []
    for c in out:
        key = (c["doc_id"], c["page"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


# ── 导师模式的第二层：科研方法论（2026-10-26）────────────────────
# 用户要「用鸭博士 skill 优化导师模式」，但**原来那层是文献库陪读**（先查库、
# 每句标出处、内核按台账审计），那是荣珂自建的能力，方法论并不覆盖它；
# 而方法论那层（Idea 怎么评、Introduction 怎么写、投稿前怎么自审）原来的
# 导师模式一个字都没有。
#
# 所以是**叠加**而不是替换：下面这段接在三条纪律之后，管的是
# 「这个课题该不该做 / 稿子该怎么搭 / 交出去之前自己怎么挑刺」。
# 出处审计（`mentor_trace_audit`）一个字未动 —— 它管的是"说得出处"，
# 方法论管的是"想得清不清楚"，两件事。
#
# ★ 内容是对 HKUSTDial/Supervisor-Skills（CC BY-NC-SA 4.0，骆昱宇，
#   香港科技大学（广州））方法论的**宿主投影**：只写"什么时候该翻哪一份"，
#   不抄它的正文 —— 正文在 `vendor/skills/<name>/` 下原样随包分发，
#   按需 `load_skill(name, file=…)` 取。这样提示词保持短，正文可核对。
#   署名与改动说明见 `vendor/skills/SOURCE-Supervisor-Skills.md`。
MENTOR_METHOD_MARK = "═══ 科研方法论（导师模式的第二层）═══"

# 方法论技能 → 一句话用途。**单源**：提示词里的名字与文件名从这里生成，
# 两处各写一遍就是 H 型（改一处忘一处，名字打错时模型只会一直报"找不到技能"）。
MENTOR_METHOD_SKILLS: tuple[tuple[str, str], ...] = (
    ("idea-evaluator", "评估一个想法值不值得做（更高/更快/更强/更省/更广 五维 + 致命伤审查）"),
    ("deep-research", "综述级文献调研（多视角检索 + 逐条核引 + MECE 归纳）"),
    ("paper-spine", "选题到成稿的整条骨架（章节-文献映射）"),
    ("tech-paper-template", "技术类论文的思考模板与四条自洽性检查"),
    ("benchmark-paper-template", "Benchmark / Evaluation 类论文的五柱骨架"),
    ("intro-drafter", "Introduction 六段流程图的正文写法"),
    ("paper-writer", "证据门控的正文起草（每个断言都要能追到材料或已核检索）"),
    ("paper-polish", "保义润色、去 AI 腔、措辞校准"),
    ("figure-designer", "科研配图的范式选择与质量审查"),
    ("drawio-reconstruction", "把参考图/截图重建成可编辑的 drawio"),
    ("pre-submission-reviewer", "投稿前自审（宏观逻辑 / 写作细节 / 语法 / 格式 / 图面）"),
    ("rebuttal-guidance", "审稿意见回复（逐条应答、分类、模板）"),
)

MENTOR_METHOD_TEXT = "\n".join(
    [MENTOR_METHOD_MARK,
     "除了查库与标出处，这个会话还有**方法论**这一层：我可能要你帮我判断",
     "「这个想法值不值得做」「这篇稿子该怎么搭」「交出去之前哪里会被挑」。",
     "下面这些技能随包在本机，需要时用 `load_skill(name)` 读入口，",
     "入口里点名的 `references/…` 用 `load_skill(name, file=…)` 按需深读。",
     "",
     "★ 用法纪律：**不要凭记忆复述方法论。** 要按某套骨架写/评之前，先装载它。",
     "  你脑子里那套多半是它的粗略印象，而这类骨架的价值恰在细节",
     "  （比如 Introduction 六段里每一段只承担一件事，记岔一段整段逻辑就塌）。",
     "",
     *[f"  · `{n}` —— {d}" for n, d in MENTOR_METHOD_SKILLS],
     "",
     "★★ **方法论不能用来覆盖证据。** 这两层冲突时，永远是出处优先：",
     "  · 五维评估说这个想法「更强」，但库里查不到支撑 → 就如实说查不到，"
     "不许让评估框架代替证据；",
     "  · 骨架要求某一段该写「研究空白」，而你的库里没有能证明存在这个空白的文献",
     "  → 明说这一点缺依据，别用套话把洞填上；",
     "  · 自审清单只说「这一节通常该有 X」，不代表你的稿子里 X 的说法是对的。",
     "  一句话：**方法论管「怎么组织」，库与台账管「凭什么这么说」。**",
     "",
     "※ 只是随口问一句概念、不需要按某套骨架产出时，不必装载 —— 过度装载会挤占上下文。",
     ]) + "\n"


def answer_shape_text() -> str:
    """答复形态。**恒注入**，不看导师模式开关。

    ★ 2026-11-03 大幅删减（同 `mentor_mode_text` 的那次改动）。
      上一版是四条「先给动作、≤8 行、不列表格、不许用反问开头」——
      实测：那正是「你好」被答成一段组会点名的直接原因。
      长度约束会逼着模型**先挑一件事说**，于是它挑了个不该挑的。

      留下的只有两条，都是**用户明确抱怨过的具体故障**，不是风格偏好：
      · 附了文件却说「已生成 X.md」—— 用户要的是能读的东西，不是文件名；
      · 反问「你的株号是多少」把用户挡回去 —— 这条是真的发生过。
      其余（行数、表格、反问开头）属于让 agent 自己判断的范围。
    """
    return "\n".join([
        "═══ 两件用户抱怨过的事（其余照常回答即可）═══",
        "- 交付了文件就把内容说出来，别只回一句「已生成 xxx.md」—— 用户要读的是正文。",
        "- 信息不全时按现有数据先答完，缺什么放最后说；不要用反问把用户挡回去。",
    ]) + "\n"


def mentor_mode_text() -> str:
    """导师模式那段提示词 —— **它是一份工具用法说明，不是一份人格设定**。

    ★ 2026-11-03 改（用户原话：「不要写那么多用户不需要看的，本质上还是个 agent，
      跟其他的 agent 对话应该是一样的，只是多加了一些工具和 skill 调用」）。

    上一版 3424 字，按「怎么跟我说话 / 依据从哪来 / 查库怎么查 / 什么时候不要动手 /
    关于实验进展与数据 / 对着我的课题说」六节写成 —— 那不是工具说明，
    那是在给模型规定一套人格和一套流程。两个后果都是实测出来的：

      ① **错的是语气**：「你好」两个字换回来一段「你这轮真正撑不住的是定量数据」
         的组会点名。它没调工具，但它在替人开会。
      ② **对的地方被稀释**：真正只有这里才说得出的事实（`lab_method` 的 `say`
         字段可以原样念、库里的词是文献原话不是我的说法）埋在第 900 字之后，
         前面 800 字全是"要像导师那样"。

    这一版只保留**别处拿不到的东西**，其余删掉：
      · 留：工具有哪几类、各自容易怎么用错（查库的术语问题、库里没有 ≠ 世上没有、
            标出处、台账与文献是两回事、`lab_method` 的 `say` 可以直接用）。
      · 删：怎么说话、什么语气、必须先给动作、结尾必须给下一步 ——
            那些是**意图**，不是工具知识；而且 agent 本来就会判断。
      · 删：打招呼那段。原来专门写了一节教它「你好别调工具」——
            那是治症状。正常 agent 收到「你好」本来就不会去查文献。
            如果它真去查了，该修的是工具描述里"什么时候用我"那一半，不是加规矩。
    """
    kb = " / ".join(f"`{n}`" for n in MENTOR_KB_TOOLS)
    net = " / ".join(f"`{n}`" for n in MENTOR_NET_TOOLS)
    return "\n".join([
        MENTOR_MARK,
        f"这个会话挂着用户的文献库：{kb} 检索，`{MENTOR_READ_TOOL}` 读原文；"
        f"要用库里没有的东西，还有 {net}。",
        "",
        "以下是这些工具**容易用错**的地方（不是规矩，是踩过的坑）：",
        f"- 库里存的是**文献原话**，不是用户的说法。查「可行性闸门」命中的是羊肚菌，"
        "一篇都不相干；查「原生质体融合」「sgRNA 递送」才命中。先拆成文献里会出现的术语"
        "（方法名、物种名、基因名、试剂名、指标名）。",
        "- 一次两到三个术语最稳（实测：`光控 dCas9` 4 条，`光控 dCas9 丝状真菌` 只剩 1 条）。"
        "但这是倾向不是铁律，命中数不等于对题。",
        "- **零命中说明这个词库里没用，不等于库里没有。** 换两种叫法（同义词 / 英文 / 上下位词）"
        "再下结论。「用户的库里没有」和「世上没人做过」是两个判断。",
        "- 命中先看 snippet 对不对题，对题的用 "
        f"`{MENTOR_READ_TOOL}` 读进去。引具体结论时要能说出 `doc_id` 和页码"
        "（记成〔库·doc p.N〕），不要凭记忆写。",
        "- 参数（浓度/时间/温度/波长）只从原文取。两篇冲突就把两个都摆出来说差在哪 ——"
        "实测库里两条染色方案 DAPI 浓度差 1000 倍，都是正式发表物。",
        "- 实验台账（`lab_report` / `lab_record`）是**进度状态**，文献库是**依据来源**，"
        "别拿台账代替库去回答科研问题。台账记错了用 `lab_record(kind=\"retract\")` 作废并写理由。",
        "- `lab_method` 回来的 `say` 字段是已经写好的那段话，可以直接用。",
        "",
        "方法论那层（怎么评估想法、怎么搭稿子骨架、投稿前怎么自审）在下面的技能里，"
        "要按某套骨架做事之前先 `load_skill` 读它 —— 别凭记忆复述。",
        "冲突时依据优先：评估框架说「更有说服力」，但库里查不到支撑，就说查不到。",
        "",
    ]) + MENTOR_METHOD_TEXT


def mentor_exit_text() -> str:
    """会话内**关掉**开关时补的一句话。

    存在的全部理由：续跑/追问接回来的是**同一份消息栈**，那段"开着导师模式"
    还钉在历史里。不撤销就等于——界面开关已经关了，模型却还在按导师模式答题，
    而用户看不见这个分差（本项目定律：「没显示」与「不存在」是两件事）。
    """
    return "\n".join([
        MENTOR_OFF_MARK,
        "前面那段导师模式的纪律**对本条及之后的回答不再生效**：",
        "不再要求先查库、也不再要求〔库·doc p.N〕/〔外网·来源〕标注。",
        "按正常任务继续即可。",
    ]) + "\n"


def mentor_kb_hits(calls: list) -> dict:
    """从台账里算出**本次检索真正命中**的 (doc_id, 页码) 集合。

    两代工具的载荷形状都认（`kb_search` 回 `hits[{doc_id,page}]`，
    `kb_ask` 回 `refs[{id,page}]`）—— 只认一种的话，另一种工具的出处
    会被判成"库里没有"，那是**误伤**，而误伤的代价是模型从此学会不标出处。

    ★★ W27 修正：**`kb_read` 的页也算命中**（`read_pairs`）。
      原判据只认 `kb_search`/`kb_ask` 的**检索命中页**，于是"检索到第 4 页、
      再 `kb_read` 读 4–6 页、然后引第 5 页"会被判成 `page_not_hit`。
      实测（W27 第二轮真实运行）：模型读了 43 次 `kb_read`，产出 112 处规范
      标注，内核却判「11/39 处对不上」—— **被罚的恰是读得最认真的那些页**。
      这条判据的方向反了：它想防"编造没读过的页"，结果把"读过但没被检索
      直接命中的页"一起罚了。

      所以页码级依据有**两种**，都算数：
        · `hits/refs` —— 检索把这一页摆到模型面前了（`search_pairs`）
        · `kb_read` 的 `page_range` —— 模型把这一页的正文取出来看了（`read_pairs`）
      两者取并集作为"本次可依据的页"。这不是放宽：编一个**从没出现在任何
      检索结果、也没被 kb_read 覆盖**的页码，依旧判 `page_not_hit`。
      `read_pairs` 从回包的 `page_range` 与 `text` 里的 `[p.N]` 标记取
      （两者都取，任一缺失都不至于漏判）。
    """
    pairs: set[tuple[str, int]] = set()
    read_pairs: set[tuple[str, int]] = set()
    docs: dict[str, str] = {}
    kb_calls = 0
    kb_ok_calls = 0
    read_calls = 0
    unparsed = 0
    for c in calls or []:
        name = getattr(c, "name", "")
        if name not in MENTOR_KB_TOOLS and name != MENTOR_READ_TOOL:
            continue
        if name == MENTOR_READ_TOOL:
            if not getattr(c, "ok", False):
                continue
            read_calls += 1
            try:
                p = json.loads(str(getattr(c, "result", "") or ""))
            except Exception:                    # noqa: BLE001
                unparsed += 1
                continue
            if not isinstance(p, dict):
                unparsed += 1
                continue
            did = str(p.get("doc_id") or "").strip()
            if not did:
                continue
            docs.setdefault(did, str(p.get("title") or "")[:80])
            rng = p.get("page_range")
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                try:
                    a, b = int(rng[0]), int(rng[1])
                except (TypeError, ValueError):
                    a = b = None
                if a is not None and b is not None:
                    for pg in range(min(a, b), max(a, b) + 1):
                        read_pairs.add((did, pg))
            # 正文里的 [p.N] 标记：page_range 没给全时的第二来源
            for m in re.finditer(r"\[p\.(\d{1,5})\]", str(p.get("text") or "")):
                try:
                    read_pairs.add((did, int(m.group(1))))
                except (TypeError, ValueError):
                    continue
            continue
        kb_calls += 1
        if not getattr(c, "ok", False):
            continue
        kb_ok_calls += 1
        try:
            payload = json.loads(str(getattr(c, "result", "") or ""))
        except Exception:                       # noqa: BLE001  载荷不是 JSON：如实记一笔
            unparsed += 1
            continue
        if not isinstance(payload, dict):
            unparsed += 1
            continue
        rows = list(payload.get("hits") or []) + list(payload.get("refs") or [])
        for h in rows:
            if not isinstance(h, dict):
                continue
            did = str(h.get("doc_id") or h.get("id") or "").strip()
            if not did:
                continue
            docs.setdefault(did, str(h.get("title") or "")[:80])
            try:
                pg = int(h.get("page"))
            except (TypeError, ValueError):
                continue
            pairs.add((did, pg))
    return {"kb_calls": kb_calls, "kb_ok_calls": kb_ok_calls,
            "read_calls": read_calls,
            "docs": sorted(docs), "pairs": sorted(pairs),
            "read_pairs": sorted(read_pairs),
            "search_pairs": sorted(pairs),
            "unparsed": unparsed}


def kb_library_ids(registry) -> "set[str] | None":
    """从注册表里的 `kb_list` 拿**全库 doc_id 清单**（P3「本地可复算」的锚）。

    为什么需要它：`mentor_trace_audit` 原先只能判「这篇**本次**命没命中」——
    编造一个 id 和"查过但这次没命中"在台账上是同一种东西。有了全库清单，
    「全库里根本没有这个 id」就能**不依赖网络、随时重算**地钉死 ——
    这正是设计文档 §3.5 那条「从『联网核验真伪』升级为『本地可复算』」。

    拿不到（workbench 没装 / 回包不是 JSON / 形状变了）返回 **None**：
    调用方据此跳过这一层，退回只按本次命中判。**拿不全就明说拿不全**，
    不许拿 None 冒充空集 —— 空集会把每一处出处都误判成"库里没有"。
    """
    tool = None
    try:
        tool = registry.get("kb_list") if registry is not None else None
    except Exception:                       # noqa: BLE001
        return None
    if tool is None:
        return None
    try:
        raw = tool.fn(limit=1000)
        payload = json.loads(str(raw or ""))
    except Exception:                       # noqa: BLE001
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    ids = {str(it.get("id") or "").strip() for it in items
           if isinstance(it, dict) and it.get("id")}
    # 清单被 limit 截断时仍是"拿不全"—— 用超大 limit 拿，还截断就如实放弃
    try:
        if int(payload.get("matched") or 0) > len(ids):
            return None
    except (TypeError, ValueError):
        return None
    return ids or set()


def mentor_trace_audit(calls: list, answer: str = "",
                       library_ids: "set[str] | None" = None,
                       stages: "list | None" = None,
                       extra_texts: "list[str] | None" = None,
                       require_library: bool = True) -> dict:
    """★ 出处标注的**三条判据**，全部从调用台账算，不采信模型的自我陈述。

    | # | 判据 | 怎么算（都是行为，不是措辞） |
    |---|---|---|
    | ① | 导师模式下必须真查过库 | 台账里有没有成功的 `kb_search`/`kb_ask` |
    | ② | 标的〔库·doc p.N〕必须真在本次命中里 | 标记 vs `mentor_kb_hits()` 算出的命中集 |
    | ③ | 联网补的内容必须能被指出 | 台账里的联网调用数 ↔ 正文里的〔外网·…〕标记数 |

    ②③ 都是**双向**的：用了不标 = 违规，标了却没在用 = 也是违规。
    只做单向就成了"背一句咒就过关"—— 那正是本条要防的自觉式纪律。

    ★ `library_ids`（P3 本地复算层，W25）：给全库 doc_id 清单时，
      对不上命中集的出处**再分一档** —— doc_id 连库里都没有 ⇒
      `doc_not_in_library`（编造的铁证，本地可复算，不依赖网络）；
      库里有但本次没命中 ⇒ `doc_not_in_hits`（可能是真读过没查）。
      两档措辞不同：前者比后者严重得多。`None` = 清单拿不到，
      这一层如实跳过，全部按 `doc_not_in_hits` 判（老行为）。

    `calls` 是 `ToolCall` 列表（用**全量 result**，不是界面那 800 字预览：
    按预览判会把"截断没看见"误判成"库里没有"，那是 M 型缺陷）。
    """
    # 复核对象 = 聊天答复 ∪ 本次落盘正文。只量 `final` 会把「稿子写好了、
    # 答复却是空的」这场判成「一处出处都没有」（实测 C 场）。
    texts = [str(answer or "")] + [str(t or "") for t in (extra_texts or [])]
    text = "\n".join(texts)
    hits = mentor_kb_hits(calls)
    net_calls = [getattr(c, "name", "") for c in (calls or [])
                 if getattr(c, "name", "") in MENTOR_NET_TOOLS and getattr(c, "ok", False)]
    cites = extract_citations(text)
    exts = [{"source": m.group(1)} for m in _MENTOR_EXT_RE.finditer(text)]

    # ★ 页码级依据有**两种**：检索命中（search_pairs）与 kb_read 读过（read_pairs）。
    #   取并集。这不是放宽 —— 见 `mentor_kb_hits` 的 W27 修正说明：
    #   只认检索命中，会把"读了原文再引"这条**最该鼓励**的路径判成违规。
    pair_set = set(hits["pairs"]) | set(hits.get("read_pairs") or [])
    doc_set = set(hits["docs"])
    lib_ids = set(library_ids) if library_ids is not None else None
    for c in cites:
        if (c["doc_id"], c["page"]) in pair_set:
            c["verdict"] = "grounded"           # 本篇这一页，本次确实命中/读到过
        elif c["doc_id"] in doc_set:
            c["verdict"] = "page_not_hit"       # 篇在命中里，**这一页**没有
        elif lib_ids is not None and c["doc_id"] not in lib_ids:
            c["verdict"] = "doc_not_in_library"  # ★ 全库里就没有这个 id（P3 本地复算）
        else:
            c["verdict"] = "doc_not_in_hits"    # 这篇本次没命中过（可能是编的）

    bad = [c for c in cites if c["verdict"] != "grounded"]
    reasons: list[str] = []       # 只装**确定不符合**；不确定绝不写进来
    unknown: list[str] = []       # 「读不到 / 依据不全」在这里，一律判「未测」
    #: 单一真相：三条判据各是 True(符合) / False(不符合) / None(未测或不适用)。
    #: `verdict` / `pass` / 界面文案**全部**由这一张表派生，不许别处再算一遍。
    judgments: dict = {}

    # ① 导师模式专属：开着就必须真查库。普通知识库任务只是「查过库」，
    #    不适用这条 ⇒ None（不是 False，否则会把正确答案诬成违规）。
    if require_library:
        judgments["must_query_library"] = hits["kb_ok_calls"] > 0
        if not judgments["must_query_library"]:
            reasons.append("开着导师模式却一次库都没查"
                           f"（{' / '.join(MENTOR_KB_TOOLS)} 均无成功调用）")
    else:
        judgments["must_query_library"] = None

    # ② 标了出处必须在本次命中里；**有命中却真的一处都没标**才算"悄悄用了库"。
    #   ⚠️ 库没命中时没标 = 正确行为，判违规等于逼模型从空库里编一篇。
    #   ⚠️ 正文有 doc_id 却读不出页码 = **读不到**，不是「没有依据」（实测 B 场）。
    lib_hit = bool(pair_set)
    doc_tokens = len(_DOC_ID_RE.findall(text))
    format_unreadable = bool(doc_tokens) and not cites
    if format_unreadable:
        judgments["citations_ground"] = None
        unknown.append(
            f"正文里出现 {doc_tokens} 个 doc_id，但一处都没能读成可复核的页码标注 —— "
            "标注格式读不到，无法判定（**未测**，不是「没有依据」）。")
    else:
        judgments["citations_ground"] = (not bad) and (bool(cites) if lib_hit else True)
        if cites and bad:
            reasons.append(f"{len(bad)}/{len(cites)} 处库内出处对不上本次命中集"
                           f"（例：{bad[0]['doc_id']} p.{bad[0]['page']} → {bad[0]['verdict']}）")
            # ★ P3 本地复算层给的**加重情节**：doc_id 连全库清单里都没有。
            _fab = [c for c in bad if c["verdict"] == "doc_not_in_library"]
            if _fab:
                reasons.append(
                    f"其中 {len(_fab)} 处的 doc_id **全库查无此 id**（库里共 {len(lib_ids)} 篇，"
                    f"如 {_fab[0]['doc_id']}）—— 这不是「本次没命中」，是**本地可复算的编造**。")
        elif cites and not hits["kb_ok_calls"]:
            reasons.append("标了库内出处，但台账里没有任何检索命中可依据")
        if not cites and lib_hit and hits["kb_ok_calls"] > 0:
            reasons.append("查了库、库里也命中了，却没标一处可复核的库内出处 "
                           "—— 用了不说，等于没依据")

    # ③ 联网补的内容必须能被指出（双向）
    judgments["external_marked"] = True
    if net_calls and not exts:
        judgments["external_marked"] = False
        reasons.append(f"联网补了 {len(net_calls)} 次（{net_calls[0]}…）却没标一处〔外网·来源〕")
    if exts and not net_calls:
        judgments["external_marked"] = False
        reasons.append(f"正文出现 {len(exts)} 处〔外网·…〕标记，但台账里没有任何联网调用")

    # 命中集**不完整**时不许装作判全了（M 型：把"没看全"说成"不存在"）
    note = ""
    if hits["unparsed"]:
        note = (f"有 {hits['unparsed']} 次库检索的回包不是 JSON，内核没能从里面取出命中集 —— "
                "②的判定依据不全，标了出处对不上不等于就是编的")
    if hits["kb_ok_calls"] and not lib_hit and not hits["unparsed"]:
        # 「查过、确实没命中」是一句要**摆到界面上**的事实（§3.4）：
        # 它既给模型那句「库里没有」兜底，也拦住接下来任何一处〔库·…〕标记。
        note = (note + "\n" if note else "") + \
            "本次库里没有命中任何段落（查过 " + str(hits["kb_ok_calls"]) + \
            " 次，命中 0 篇）—— 按纪律这里**不该**出现库内出处；" + \
            "补来的每一句都得带〔外网·…〕。**不等于**世上没有这类文献。"
    if library_ids is None:
        # 旧文案把它归因成「workbench 未装」，而真实原因是收尾复核**根本没传**
        # 这个参数（死代码）—— 错的原因会让正确的修复方向被带偏。如实说未取得。
        note = (note + "\n" if note else "") + \
            "本次复核没有取得全库 doc_id 清单（kb_list 不可用 / 回包形状不符 / 调用方未接）—— " \
            "「库里查无此 id」这一层**未测**，出处只按本次命中集核。"
        unknown.append("全库 doc_id 清单未取得 ⇒ 「doc_not_in_library」这一层未测。")
    # P3（proposal 阶段的证据表纪律）：审计**不设门**（W22 定的分工），
    # 但要把这一阶段的硬要求摆在人面前 —— 证据表每行都得有库内锚。
    if "proposal" in set(stages or []):
        note = (note + "\n" if note else "") + \
            "本任务是**开题/研究计划**（proposal 阶段）：证据表里每一条都要锚到" \
            "〔库·doc_id p.页码〕或〔外网·来源〕，且库内锚必须通过上面②的比对 —— " \
            "库里没有的引用**不许写**（要引用就先 `kb_add` 真的入库）。"
    # ── 单一真相 → 派生字段。界面只读 `verdict`，不许自己拼「通过/未通过」──
    # `must_query_library=None` 是「本条不适用」，不是「没测」，不进未测集合。
    _fails = [k for k, v in judgments.items() if v is False]
    _unknowns = [k for k, v in judgments.items()
                 if v is None and k != "must_query_library"]
    verdict = ("fail" if (reasons or _fails)
               else ("unknown" if (unknown or _unknowns) else "pass"))
    return {
        "kb_only": require_library,
        "verdict": verdict,
        "unknown_reasons": unknown,
        "advisory": True,        # 只挂事实，不是门（界面必须读它）
        "library_hit": lib_hit,
        "library_ids_known": library_ids is not None,
        "library_size": len(lib_ids) if lib_ids is not None else None,
        "judgments": judgments,
        "kb_calls": hits["kb_calls"], "kb_ok_calls": hits["kb_ok_calls"],
        "kb_read_calls": hits.get("read_calls", 0),
        "net_calls": net_calls,
        "hit_docs": hits["docs"], "hit_pairs": len(pair_set),
        "search_pairs": len(hits["pairs"]),
        "read_pairs": len(hits.get("read_pairs") or []),
        "citations": cites, "external_marks": exts,
        "violations": reasons,
        "pass": verdict == "pass",     # 兼容旧消费点；新界面一律读 verdict
        "answer_chars": len(str(answer or "")),
        "audited_chars": len(text),
        "sources": ["聊天答复"] + [f"落盘正文 #{i + 1}"
                               for i, _ in enumerate(extra_texts or [])],
        "note": note,
        "tools": {"kb": list(MENTOR_KB_TOOLS), "net": list(MENTOR_NET_TOOLS)},
    }


#: 来源级：把「这次到底读没读到原文」分成三档，挂在每处引注旁边。
#:
#: 为什么需要这一层（实测依据，`docs/导师模式实测-2026-11-03.md` 第十七节）：
#: 一次 `kb_search` 的完整回包 9924 字、10 条命中，而**模型只看得见前 800 字**，
#: 于是 25 处引注里 14 处的页它根本没看见、11 处的篇本次压根没检索到 ——
#: 这两类在旧形态下长得一模一样，学生分不出哪句能直接抄进论文。
#: 腾讯混元 Hyra 2.0 的 Books 用「观察/已验证/约定」三级治同一个病，这里取前两级的思路。
SOURCE_LEVELS = {
    "read": "读",      # 本次 kb_read 真的把那几页读出来了
    "hit": "检",       # 本次检索命中过这一页，但没读原文
    "recall": "忆",    # 本次既没命中也没读，来自历史/方法卡/记忆
}


def _source_level(cite: dict, hits: dict) -> str:
    """给一处引注定「读 / 检 / 忆」。不采信任何自述，只看台账。"""
    pair = (cite.get("doc_id"), cite.get("page"))
    if pair in set(hits.get("read_pairs") or []):
        return "read"
    if pair in set(hits.get("pairs") or []):
        return "hit"
    return "recall"


def _run_trace_audit(calls: list, answer: str,
                     extra_texts: "list[str] | None" = None,
                     library_ids: "set[str] | None" = None,
                     stages: "list | None" = None,
                     require_library: bool = True) -> dict:
    """收尾复核的**唯一入口**：算事实、不判决。

    相对 `mentor_trace_audit` 多做两件事，都是纯附加：
      ① 给每处引注挂 `level`（读/检/忆）；
      ② 产出一行 `summary`，给界面直接用（人话，不是布尔）。

    **绝不**在这里改 `success` / `final` / `verdict` —— 它只往 `result.trace_audit` 里写。
    判据②只认本次命中集，靠记忆引真实文献也会被判 `doc_not_in_hits`；
    把它当门会误伤，当成来源级标注才是它的用处。
    """
    aud = mentor_trace_audit(calls, answer=answer, extra_texts=extra_texts,
                             library_ids=library_ids, stages=stages,
                             require_library=require_library)
    hits = mentor_kb_hits(calls)
    counts = {"read": 0, "hit": 0, "recall": 0}
    for c in aud.get("citations") or []:
        lv = _source_level(c, hits)
        c["level"] = lv
        c["level_label"] = SOURCE_LEVELS[lv]
        counts[lv] += 1
    aud["level_counts"] = counts
    total = sum(counts.values())
    if aud.get("verdict") == "unknown":
        # 「读不到」不许被写成「一处出处都没有」—— B 场正文里明明有 9 个 doc_id。
        aud["summary"] = ("本轮出处复核**未测**：" +
                          "；".join(aud.get("unknown_reasons") or ["判据依据不全"]))
    elif total == 0:
        aud["summary"] = "本轮没有出现库内出处标注。"
    else:
        aud["summary"] = (
            f"本轮 {total} 处库内出处：读原文 {counts['read']}、只见检索命中 {counts['hit']}、"
            f"来自历史记忆 {counts['recall']}。"
            + ("「忆」档的出处**没有本次依据**，写进论文前必须自己核一遍。"
               if counts["recall"] else ""))
    aud["advisory"] = True     # 显式声明：这是提示，不是门
    return aud


#: 这些后缀算「正文」。写作任务交付它们时，正文必须进 `final`，不能只给一句概述。
_PROSE_SUFFIXES = {".md", ".txt", ".tex", ".rst", ".docx", ".doc"}


# ── 交付正文的单源（2026-11-04 结构收敛）──────────────────────────────
# 之前「复核读什么」写在收尾处一行、「失败答复读什么」压根没写、
# 「成功答复读什么」又在 `_compose_final` 里另算一遍 —— 三处真相，
# 于是同一份落盘稿在三个界面里长得不一样。这里收敛成一条：
#     正文 = 聊天答复 ∪ 本次落盘正文（附来源清单，供界面如实交代读过什么）
def _prose_files(root: "Path | str | None") -> list[tuple[str, Path]]:
    """本次工作区里成文的产物，按修改时间从新到旧。任何异常都只影响这一层。"""
    if root is None:
        return []
    r = Path(root)
    found: list[tuple[float, str, Path]] = []
    for base in (r, r / "data"):
        try:
            for p in base.iterdir():
                if p.is_file() and p.suffix.lower() in _PROSE_SUFFIXES:
                    found.append((p.stat().st_mtime, p.name, p))
        except Exception:                            # noqa: BLE001
            continue
    found.sort(key=lambda x: x[0], reverse=True)
    return [(n, p) for _, n, p in found[:12]]


def collect_delivery_text(final: str, root: "Path | str | None" = None,
                          per_file: int = 20_000,
                          total_cap: int = 60_000) -> tuple[list[str], list[str]]:
    """复核/答复的**唯一正文来源**：`(正文段列表, 来源说明列表)`。

    同一段文字只收一次（与 `final` 全同的落盘稿不重复收），
    引注在 `extract_citations()` 里再按 `(doc_id,page)` 去重 ——
    两道去重保证「答复里写了、盘上也存了」不会被算成两处出处。
    """
    first = str(final or "")
    texts = [first]
    sources = [f"聊天答复（{len(first.strip())} 字）"]
    budget = max(0, total_cap - len(first))
    for name, p in _prose_files(root):
        if budget <= 0:
            break
        try:
            t = p.read_text(encoding="utf-8", errors="replace")[:min(per_file, budget)].strip()
        except Exception:                            # noqa: BLE001
            continue
        if not t or t == first.strip():
            continue
        texts.append(t)
        sources.append(f"{name}（{len(t)} 字）")
        budget -= len(t)
    return texts, sources


def should_audit(kb_only: bool, calls: list, texts: list[str]) -> bool:
    """**行为触发**，不看开关：查过库 / 正文出现 doc_id / 导师模式，任一成立就复核。

    旧版只在 `kb_only` 下复核，于是普通任务查了 6 次库、写了 8 处规范引注，
    却一次复核都没跑（实测 A 场）。开关描述的是「要不要导师」，不是「要不要诚实」。
    """
    if kb_only:
        return True
    kb_tools = MENTOR_KB_TOOLS + (MENTOR_READ_TOOL,)
    if any(getattr(c, "name", "") in kb_tools and getattr(c, "ok", False)
           for c in (calls or [])):
        return True
    return any(_DOC_ID_RE.search(str(t or "")) for t in texts)


def audit_delivery(calls: list, final: str, root: "Path | str | None" = None,
                   *, kb_only: bool = False, registry=None,
                   stages: "list | None" = None) -> "dict | None":
    """收尾复核的**唯一出口**：收集正文 → 判是否复核 → 接回全库清单 → 出三态。

    `kb_library_ids()` 曾是一段「单独跑能取 252 篇、产品里零消费点」的死代码：
    编造 doc_id 那层判据永久免检，note 还把原因错写成「workbench 未装」。
    接线放在这一个出口里，别处不许再自己拼一遍复核参数。
    """
    texts, sources = collect_delivery_text(final, root)
    if not should_audit(kb_only, calls, texts):
        return None
    lib = kb_library_ids(registry) if registry is not None else None
    aud = _run_trace_audit(calls, texts[0], extra_texts=texts[1:],
                           library_ids=lib, stages=stages,
                           require_library=kb_only)
    aud["sources"] = sources
    return aud


def interrupted_final(reason: str, steps: int,
                      root: "Path | str | None" = None,
                      head: int = 600) -> str:
    """所有**未完成**分支的答复单源：断在哪、产出了什么、已落盘正文前 N 字。

    实测 C 场：盘上 `kb_summary.md` 227 字 / 4 处引注，学生看到的却是一片空白。
    六处 `final=""` 各写各的，修一处漏五处 —— 现在只有这一处可以写。
    """
    texts, sources = collect_delivery_text("", root, per_file=2000,
                                           total_cap=6000)
    files = [s.split("（")[0] for s in sources[1:]]
    body = "\n".join(t for t in texts[1:] if t)[:head].strip()
    out = [f"【本轮未完成】{reason}",
           f"断在第 {steps} 步。",
           "已产出：" + ("、".join(files) if files else "（无正文产物）"),
           (f"【已落盘正文（前 {len(body)} 字）】\n{body}"
            if body else "（本轮没有正文落盘）")]
    return "\n\n".join(out)


def _compose_final(claim, draft: str, files: "list[str] | None" = None) -> str:
    """把「用户要看到的答复」拼出来 —— **正文优先，概述退居其后**。

    为什么必须改（2026-11-03 实测，`docs/导师模式实测-2026-11-03.md` 第二节）：
      开题报告那一轮跑 38 步、688 秒、把 10271 字写进
      `workspace/虫草菌原生质体融合育种_开题报告_final.md`，证据门通过，
      `success=True` —— 而 `result.final` 是 **空字符串**。300 字摘要那一轮
      `final` 只有一句 78 字的元描述。**学生在聊天窗口里看到的是空白。**

    这不是模型没写（思考链显示它改了四版字数），是交付通道只把
    `deliver_task.summary` 当答复，而 summary 按契约是"给门看的描述"，
    不是给学生的正文。两者职责不同，此前被当成了同一个东西。

    新口径：
      · 有正文（`draft` 非空）且是成文的散文 → **正文在前**，概述附在末尾一行；
      · 正文很短（< 120 字，多半是占位符）→ 仍以 summary 为主；
      · 没有任何正文 → 原样返回 summary，并**如实说明**产物落在哪个文件。
    """
    summary = str(getattr(claim, "summary", "") or "").strip()
    body = str(draft or "").strip()
    if not body:
        if files:
            return (summary + "\n\n（本轮产物已落盘：" + "、".join(files[:6]) + "）").strip()
        return summary
    if len(body) < 120 and len(summary) > len(body):
        head = summary
        tail = "\n\n【已落盘的正文】\n" + body
    else:
        head = body
        # 概述里常带"多少字/什么结构"这类**正文本身看不出来的事实**，不许丢
        tail = ("\n\n———\n（本轮交付说明：" + summary + "）"
                if summary else "")
        if files:
            tail += "\n（产物落盘：" + "、".join(files[:6]) + "）"
    return head + tail


def _load_skill(name: str, file: str = "") -> str:
    """load_skill 的执行体：装载失败要**抛错**，而不是返回一句提示。

    因为注册表把"返回字符串"记为成功调用（ok=True），门就会误以为技能装载过了 ——
    一个打错名字的 load_skill 不该让证据门放行。抛错的提示文字一样会回灌给模型
    （走 _observation 的 [工具执行失败] 分支），不损失可读性。

    `file` 为空 ⇒ 入口文档（`SKILL.md`）；给了相对路径 ⇒ 该技能目录下的参考文件。
    技能自己的 `SKILL.md` 写着"`references/` 按需深读"，原来却没有工具能读 ——
    模型于是去猜路径并连着报错。这个参数把那句指示**变成可执行的**。
    """
    text = read_skill_text(name, file=file)
    if text.startswith("⛔"):
        raise RuntimeError(text)
    return text


AUDIT_FIGURE_SCHEMA = {
    "type": "object",
    "properties": {
        "contract": {
            "type": "object",
            "description": (
                "五点图面契约（**开画之前就要定死**，这里一并提交）：\n"
                "  figure_id: 'Fig. 1'\n"
                "  title: 图注总题（加粗名词短语，不含数字）\n"
                "  core_conclusion: 这张图要捍卫的那一句话\n"
                "  archetype: quantitative grid | schematic-led composite | "
                "image plate + quant | asymmetric mixed-modality\n"
                "  backend: python | r | origin | other（**一经选定即独占**）\n"
                "    ⚠️ **怎么选：看审计需求，不看引擎喜好**（已定案，别逐图纠结）——\n"
                "    满足任一条即**必须** python/r：① 分面数 ≥ 2（对齐门要「分面几何"
                "清单」，只有 python/r 量得出）② 面板里画了数据（center/spread 非空，"
                "含逐点原始值）③ 声明了目标期刊。\n"
                "    仅「单分面 + 面板不画数据 + 不进投稿包」的示意/装置/流程图"
                "可走 origin/other。\n"
                "    选错 = 契约不成立，本工具会在**画之前**把契约打回。\n"
                "  width_mm / height_mm: 最终物理尺寸（单栏 89 / 双栏 183 mm）\n"
                "  journal / formats: 如 'nature' / ['svg','pdf','tiff','png']\n"
                "  panels: [{letter:'a', role:'推断职责（不是画法）', claim:'独家回答什么问题', "
                "center:'mean', spread:'s.d.', unit:'batches'}] —— "
                "**每个分面的 role 必须互不相同**，重复即失败\n"
                "  engine_artifacts: 每个产物由哪个引擎产出（一种以上即失败）\n"
                "  exclusions: [{what, before, after, rule, reason}] 被排除的数据\n"
                "  canonical_names: 需保留原拼写的名字（XGBoost / GPT-5.2 …）"),
        },
        "pdf": {"type": "string",
                "description": "导出的 PDF（相对工作区）。字形下限与碰撞审计都按它量。"},
        "source": {"type": "string",
                   "description": "绘图源码（相对工作区）。后端是 python/r 时应当给；"
                                  "第三方渲染器出图时可以不给。"},
        "layout_manifest": {"type": "string",
                            "description": "分面几何清单（相对工作区）。多分面图必给，"
                                           "否则对齐门判 NOT AUDITABLE（**不等于通过**）。"},
        "legend": {"type": "string", "description": "图注文本（直接给全文）。"},
        "legend_path": {"type": "string",
                        "description": "或给图注文件路径（相对工作区）。"},
    },
    "required": ["contract"],
}


def _plot_kind_list() -> str:
    """列出当前可用的图型 —— **含插件注册的**。

    ★ 为什么必须动态取：这段文字是模型唯一能看到的"可以画哪些图"。
      `PLOT_DATA_SCHEMA` 是模块级常量，在 import 时就冻住了；
      而插件在 `Rongke.__init__` 里才加载，比 import 晚。
      如果描述在这里写死内置那 9 种，插件图型就**对模型不可见** ——
      能注册、能画、能过审计，但模型从来不知道它存在，永远不会填。
      那正是"扩展了但等于没扩展"的形态。
    """
    try:
        from . import analysis as _an
        kinds = sorted(_an.CHART_KINDS)
    except Exception:                                          # noqa: BLE001
        kinds = []
    if not kinds:
        kinds = ["time_series", "line", "scatter", "box", "violin",
                 "bar", "paired", "heatmap", "hist"]
    return " / ".join(kinds)


PLOT_DATA_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {
            "type": "string",
            "description": "数据表路径（相对工作区或绝对）。支持 .csv / .tsv / .xlsx / .json",
        },
        "kind": {
            "type": "string",
            "description": ("图型。**留空是最常见也最正确的选择** —— 模块会按列的形状"
                            "自动选，并把「为什么选它、为什么不选别的」写进 suggestions。"
                            "只有你已经确定要画什么时才填。可用图型见调用时的返回"
                            "（含已装插件注册的）。内置的有："
                            "time_series / line / scatter / box / violin / bar / "
                            "paired / heatmap / hist"),
        },
        "x": {"type": "string", "description": "x 轴列名（留空则用建议里的）"},
        "y": {"type": "string", "description": "y 轴列名（留空则用建议里的）"},
        "group": {"type": "string",
                  "description": ("分组列名（分组的柱/箱/小提琴，以及时间序列的分线）。"
                                  "留空时若有分类列会自动用第一个 —— 时间序列**务必**"
                                  "按组分层，否则各组会被串成一条来回折返的线。")},
        "title": {"type": "string", "description": "图题（留空则自动生成为「y by x」）"},
        "width_mm": {"type": "number",
                     "description": "最终物理宽度 mm。默认 89（单栏），双栏用 183"},
        "journal": {"type": "string", "description": "目标期刊（如 nature），影响图注字数上限"},
    },
    "required": ["table"],
}


#: 原生 SVG 出图（2026-11-05 加）。参数刻意比 `plot_data` **少**：
#: 这条路线不产投稿级产物，也就不该有 journal / 物理精度这些假装要送审的旋钮。
PLOT_SVG_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {
            "type": "string",
            "description": ("数据表路径（相对工作区或绝对）。CSV / TSV / JSON 用标准库直读；"
                            ".xlsx 需要 pandas，没装会如实报错而不是画空图。"),
        },
        "kind": {
            "type": "string",
            "description": ("图型，留空自动按列形状选。原生后端支持："
                            "line / time_series / scatter / bar / barh / hist / box / "
                            "heatmap / matrix。不支持的图型会**明确报错**，不会偷换成别的图。"),
        },
        "x": {"type": "string", "description": "x 轴列名（留空用建议的）"},
        "y": {"type": "string", "description": "数值列（留空用建议的）"},
        "group": {"type": "string", "description": "分组列名（多系列折线 / 分组着色）"},
        "title": {"type": "string", "description": "图题（中文也可以，字体栈已按中文回退）"},
        "width_mm": {"type": "number",
                     "description": "图宽 mm，默认 89（单栏）；只决定 viewBox，不承诺印刷尺寸"},
        "out": {"type": "string",
                "description": "输出 .svg 路径（相对工作区或绝对）。留空 = 工作区下 figure.svg"},
    },
    "required": ["table"],
}


AUDIT_DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "pptx": {
            "type": "string",
            "description": "要体检的 .pptx 路径（相对工作区或绝对路径都行）。",
        },
        "require_notes": {
            "type": "boolean",
            "description": (
                "**用户是否明确要了讲稿/备注**（原话里有『讲稿』『备注』『演讲词』"
                "『逐字稿』才填 true；只是『做个组会 PPT』填 false）。\n"
                "填 true 时，备注覆盖率不足会被**阻断**。\n"
                "⚠️ 这一项只是给你自己看的提示：内核在交付时会**从契约独立重判一次**，"
                "不采信这里的值（被审者不该给自己定标准）。"),
        },
        "deep_contrast": {
            "type": "boolean",
            "description": (
                "**是否额外跑一遍渲染像素级对比度校验**（默认 false）。\n"
                "⚠️ **慢**：本机实测 ~15 秒（一份 5 页 deck），而且**会调用 PowerPoint "
                "渲染**每一页。默认关闭时本工具仍是秒级。\n"
                "只在需要检查**『文字压在照片/渐变上』**这一类情形时开启 —— "
                "纯 XML 拿不到照片的亮度，那几处会被记成『量不到』(INFO "
                "contrast_unmeasurable)，只有渲染能看见它们。\n"
                "开启**不改变**任何既有判据与阈值，只追加一条像素口径的 "
                "WARN/INFO（**不阻断**交付）。\n"
                "留空或 false = 沿用配置 `deck.deep_contrast`（不是强制关掉）。"),
        },
    },
    "required": ["pptx"],
}


READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string",
                 "description": "工作区内的文件路径（相对工作区即可，例如「素材.md」）"},
        "max_chars": {"type": "integer",
                      "description": "最多返回多少字符（默认 20000）。超长会截断并如实说明。"},
    },
    "required": ["path"],
}


WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string",
                 "description": "工作区内的落点路径（相对工作区即可，例如「综述.md」"
                                "或「out/综述.md」）。中间目录会自动建。"},
        "text": {"type": "string",
                 "description": "要写入的**完整正文**。append=false 时它是全文（覆盖旧内容）；"
                                "append=true 时它是追加的一段。"},
        "append": {"type": "boolean",
                   "description": "true=追加到文件末尾（分段落盘用这个，避免一次要吐几万字）；"
                                  "false（默认）=整篇覆盖。"},
    },
    "required": ["path", "text"],
}


CONVERT_PDF_SCHEMA = {
    "type": "object",
    "properties": {
        "src": {"type": "string",
                "description": "要转的 PDF 路径（相对工作区即可，如 \"paper.pdf\"）。"},
        "out": {"type": "string",
                "description": "Markdown 落点（相对工作区，如 \"knowledge/paper.md\"）。"
                               "留空则只转不落盘（用于探路/看结构统计）。"},
        "use_ocr": {"type": "boolean",
                    "description": "是否开 OCR。**默认 false**。"
                                   "实测：21 页双栏论文关 OCR 24.4s、开 OCR 53.2s（慢 2.2 倍），"
                                   "而正文只多约 1.3%。⇒ 数字文本的期刊件**不要开**；"
                                   "只有扫描件（没有文字层）才开。"
                                   "拿不准就先关着转一次 —— 返回里会按每页字符数告诉你像不像扫描件。"},
        "images": {"type": "boolean",
                   "description": "是否导出图片（默认 false）。这份 Markdown 是给人读 / 给 Obsidian 的，"
                                  "默认不需要图；要把 PDF 里的图也落盘（Obsidian 里看图）才开"
                                  "（会慢约 7s/篇。kb 库只收 PDF，开不开与入库无关）。"},
    },
    "required": ["src"],
}

KB_TO_OBSIDIAN_SCHEMA = {
    "type": "object",
    "properties": {
        "src": {"type": "string", "description": "要转并写进 vault 的 PDF 路径。"},
        "title": {"type": "string",
                  "description": "笔记标题（也用作文件名）。留空则用 PDF 文件名。"},
        "authors": {"type": "array", "items": {"type": "string"},
                    "description": "作者列表（可留空 —— 空字段不会写进 frontmatter）。"},
        "year": {"type": "integer", "description": "年份（可留空）。"},
        "journal": {"type": "string", "description": "期刊名（可留空）。"},
        "doi": {"type": "string", "description": "DOI（可留空）。"},
        "tags": {"type": "array", "items": {"type": "string"},
                 "description": "Obsidian 标签（可留空）。"},
        "folder": {"type": "string",
                   "description": "vault 内的子目录（如 \"文献\"）。留空则落在库根。"},
        "use_ocr": {"type": "boolean", "description": "同 convert_pdf，默认 false。"},
        "images": {"type": "boolean", "description": "同 convert_pdf，默认 false。"},
    },
    "required": ["src"],
}


LITERATURE_FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "dois": {"type": "array", "items": {"type": "string"},
                 "description": "要取全文的 DOI 列表（可带 `doi:` 前缀或 doi.org 外壳，会自动剥）。"
                                "一次别超过 20 个：这几个 API 都对突发请求限流。"},
        "out_dir": {"type": "string",
                    "description": "落盘目录（**相对工作区**，如 \"fulltext\"）。留空落在 "
                                   "`fulltext/`。落在这里的文件可直接喂给 kb_add/kb_ingest。"},
    },
    "required": ["dois"],
}

KB_COVERAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {"type": "array", "items": {"type": "string"},
                   "description": "主题词列表（建议 5–8 个，对应大纲的章节或核心论点）。"
                                  "**中文请给三字以上的词** —— 两字词会被库的 trigram 索引"
                                  "静默丢弃、返回空，那不等于库里没有；本工具会把它们点名出来。"},
        "min_docs": {"type": "integer",
                     "description": "每个主题词至少要有几篇不同文献才算「够」。默认 3。"},
        "min_ok_ratio": {"type": "number",
                         "description": "达标主题词占比阈值（0–1）。默认 0.6。"
                                        "低于它就整体判「不够」。有任何硬缺口时一律判不够。"},
        "per_topic": {"type": "integer",
                      "description": "每个主题词检索几段。默认 8。调大更准但更慢。"},
    },
    "required": ["topics"],
}


def _make_literature_fetch(cfg) -> "Callable[..., dict]":
    """取全文的工具实现。落盘只在工作区内（围栏与自判双保险）。

    ★ 为什么这是内核自带的、而不是调用外部 skill 的脚本：
      荣珂没有 shell（实测 59 个工具里没有 exec/bash/子进程类），
      所以「让模型去跑 nature-downloader 的 batch_download.mjs」这条路
      **根本不存在**。要实现它就得给模型任意命令执行权 —— 那与这个仓
      的围栏设计方向相反，而且是把它做成产品时最不该开的口子。
      ⇒ 正确做法：把「合法取全文」这个动作本身做成一个**参数受限**的工具。
        能传的只有 DOI 列表与落点目录，**拼不出一条任意命令**。

    ★ 为什么 `mutating=True`：它往工作区写 pdf/xml 文件。
      围栏只查 mutating 工具的绝对路径参数，所以这里再自己判一遍
      （两处各写一遍是刻意的，见 hooks.py 的说明）。
    """
    def literature_fetch(dois, out_dir: str = "", **_ignored) -> dict:
        from . import litfetch

        if isinstance(dois, str):
            dois = [dois]
        if not isinstance(dois, (list, tuple)) or not dois:
            raise RuntimeError("dois 不能为空：给一个 DOI 列表，如 [\"10.1038/…\"]。")

        root = Path.cwd().resolve()
        raw = str(out_dir or "").strip() or "fulltext"
        p = Path(raw)
        if p.is_absolute():
            raise RuntimeError(
                f"⛔ out_dir 必须用**相对工作区**的路径（收到绝对路径 {raw}）。"
                f"\n   本任务工作区是 {root}。")
        dest = (root / p).resolve()
        if dest != root and root not in dest.parents:
            raise RuntimeError(f"⛔ out_dir 越过工作区：{raw}")

        rep = litfetch.fetch_many([str(d) for d in dois], dest)
        rep["out_dir"] = str(dest)
        if rep.get("ok"):
            rep["note"] = ("取全文**成功不等于已入库** —— 把每条的 `file` 交给 "
                           "kb_add / kb_ingest 才算进库。")
        return rep

    return literature_fetch


def _make_kb_coverage(root=None) -> "Callable[..., dict]":
    """库够用性判定的工具实现：**发检索 + 算账**合在一个动作里。

    为什么不让模型自己循环调 `kb_search` 再自己数：
      让模型数"命中了几篇不同文献"，它数不准（会重复计、会漏计），
      而这一步的结论直接决定"去不去检索入库" —— 数错了就是白跑一轮或带着缺口开写。
      ⇒ 内核发请求、内核记账、内核下判语，模型只负责给主题词。

    实现要点：`kb_search` 是 workbench 反射进来的工具，本函数通过
    `_KB_CFG["registry"]` 拿到它（**同一进程内直调**，不起子进程、不走 MCP）。
    """
    def kb_coverage(topics, min_docs: int = 3, min_ok_ratio: float = 0.6,
                    per_topic: int = 8, **_ignored) -> dict:
        from . import coverage

        if isinstance(topics, str):
            topics = [topics]
        if not isinstance(topics, (list, tuple)) or not topics:
            raise RuntimeError("topics 不能为空：给 5–8 个主题词。")

        reg = _KB_CFG.get("registry")
        # ★ `_KB_CFG["registry"]` 挂的是 **`ToolRegistry` 对象**，不是 dict。
        #   第一版写成 `reg.get("kb_search")`/`reg["kb_search"]` —— ToolRegistry 上没有
        #   `get(key)` 这个方法签名（它的 `.get()` 收的是工具名并返回 `Tool`，
        #   但 `reg["..."]` 会抛 `TypeError: 'ToolRegistry' object is not iterable`），
        #   于是**每一次检索都在 except 里落地成"检索失败"**，底下好好的库一条也查不到。
        #   实测症状：五个主题词全报"检索失败"，而 `lib.search_paras` 直接调能返回 5 条。
        #   教训：反射拿到的东西先**打印它的类型**，别照着名字猜形状。
        _kb = reg.get("kb_search") if reg is not None else None
        if _kb is None:
            raise RuntimeError(
                "本任务没有挂载 kb_search（文献库不可用）。\n"
                "⇒ 这种情况**不能**判「库够用」：如实说明库里查不了，"
                "再问用户要不要换一条路（先建库 / 只按已给材料写）。")

        usable, short = coverage._usable_terms([str(t) for t in topics])
        rows: list[dict] = []
        for t in usable:
            try:
                # ★ 参数名是 `n`，**不是** `top_k`。
                #   workbench 的 kb_search 签名是 (query, n=8, doc_ids="", library="")。
                #   写成 top_k 会抛 TypeError，而它被上面的 kb_search 包装层
                #   catch 成"返回空串" —— 又是一个静默失败。
                #   **参数名必须照抄产出方。**
                raw = _kb.fn(query=t, n=int(per_topic))
                hits = []
                if isinstance(raw, dict):
                    hits = raw.get("hits") or raw.get("results") or []
                elif isinstance(raw, str):
                    # workbench 有时把 JSON 序列化成字符串返回；解析不了就如实记失败
                    import json as _json
                    try:
                        obj = _json.loads(raw)
                        hits = (obj.get("hits") or obj.get("results") or []
                                if isinstance(obj, dict) else [])
                    except Exception:
                        hits = []
                rows.append({"term": t, "ok": True, "hits": hits})
            except Exception as e:                      # noqa: BLE001
                rows.append({"term": t, "ok": False, "hits": [],
                             "error": f"{type(e).__name__}: {e}"})
        # 两字词没发检索，但必须出现在报告里 —— 静默丢弃会让模型
        # 得出"这章零文献"的假结论（见 coverage 模块文档 ①）。
        for t in short:
            rows.append({"term": t, "ok": False, "hits": [],
                         "error": ("该主题词是**中文两字词**，库的 trigram 索引会静默回空，"
                                   "本工具未发检索。换成三字以上或英文再判 —— "
                                   "它现在既不算「有」也不算「没有」。")})

        rep = coverage.assess(rows, min_docs=int(min_docs),
                              min_ok_ratio=float(min_ok_ratio))
        rep["judged_terms"] = len(usable)
        rep["next"] = coverage.next_action(rep)
        return rep

    return kb_coverage


def _make_kb_convert_tools() -> dict:
    """PDF → Markdown 的两个工具（`convert_pdf` / `kb_to_obsidian`）。

    ★ 为什么这两个必须挂 `core` 常驻，而不照 `_DECK_DOMAIN` 做成按需域：
    与 `_make_topic_tools` 同一条理由 —— 按需裁剪需要一个**硬判据**，
    而"这次要不要转 PDF"从契约里看不出来（任务是"帮我建知识库"还是
    "综述这篇稿子"都可能要转）。拿模糊判据裁工具，就是本仓记的
    「不打红面的判据」。**量不出来就不裁**。

    ⚠️ 与 `read_file` 同一条安全边界：围栏只管 `mutating` 工具，
    而这两个工具**要读工作区外的 PDF**（用户的 Zotero 库/下载目录）。
    ⇒ 入参 PDF 路径**允许绝对路径**（源文件本来就在工作区外，这是主路径）；
    而**输出**一律限制在工作区内（`out` 必须相对路径），
    理由同 kb_add：产物的落点必须可解释，不能让模型往任意位置写文件。
    """
    def _out_path(out: str) -> Path:
        """把 `out` 解析成工作区内的绝对路径。**只收相对路径。**"""
        raw = str(out or "").strip()
        if not raw:
            raise RuntimeError("out 不能为空")
        p = Path(raw)
        if p.is_absolute():
            raise RuntimeError(
                f"⛔ out 必须用**相对工作区**的路径（收到绝对路径 {raw}）。"
                f"\n   理由：产物落点必须可解释 —— 本任务工作区是 {Path.cwd()}。")
        root = Path.cwd().resolve()
        full = (root / p).resolve()
        if full != root and root not in full.parents:
            raise RuntimeError(f"⛔ out 越过工作区：{raw}")
        return full

    def convert_pdf(src: str, out: str = "", use_ocr: bool = False,
                    images: bool = False) -> str:
        from .kb_convert import convert_pdf as _conv
        cfg = _KB_CFG.get("cfg")
        py = getattr(getattr(cfg, "kb", None), "python", "") or "" if cfg else ""
        to = float(getattr(getattr(cfg, "kb", None), "timeout", 600) or 600) if cfg else 600.0

        raw = str(src or "").strip()
        if not raw:
            raise RuntimeError("src 不能为空：给要转的 PDF 路径。")
        # 源 PDF **允许绝对路径**（Zotero 库/下载目录本来就在工作区外），
        # 但相对路径按工作区解析，与 read_file 一致。
        sp = Path(raw)
        if not sp.is_absolute():
            sp = Path.cwd() / sp

        op = ""
        if str(out or "").strip():
            op = str(_out_path(out))

        r = _conv(str(sp), out=op, use_ocr=bool(use_ocr), images=bool(images),
                  python_exe=py, timeout=to)
        # 给模型的一句人话（回包里 verdict 已经是 SKIPPED/FAILED/OK，这里只补后果）
        if r.get("verdict") == "SKIPPED":
            r["next"] = ("**这次没有转换，产物不存在**。"
                         "要么在 config.json 的 kb.python 里指一个装了 pymupdf4llm 的解释器，"
                         "要么换一条不用转换的路（把 PDF 原文直接 `kb_add`/`kb_ingest` 入库 —— "
                         "litkb 入库时自己会抽全文）。"
                         "⚠️ 不要把 SKIPPED 当成「这篇 PDF 没内容」。")
        elif r.get("verdict") == "OK":
            # ★ 分工三句话（W3·R2）：转完这一刻正是模型最容易误以为
            #   "转出来的 md 就是入库格式"的位置，所以话要说在产物旁边。
            r["next"] = (f"结构化 Markdown 已产出：{r.get('out')}\n"
                         "★ 两套文本、两个去处：① **kb 库**（`kb_search`/`kb_ask` 查的那套索引）"
                         "**只收 PDF 原文**，入库把 PDF 交给 `kb_add`/`kb_ingest` ——"
                         "**litkb 入库时自己会抽全文**（实测同一篇：库内 148,087 字符），"
                         "同一篇没必要两边都存正文；② 这份 Markdown 是**给人读、给 Obsidian 精读**用的，"
                         "价值在标题层级 / 表格 / 阅读顺序 —— 它**不是**入库格式，"
                         "别把它喂给 `kb_ingest`。")
        return json.dumps(r, ensure_ascii=False, default=str)

    def kb_to_obsidian(src: str, title: str = "", authors=None, year=None,
                       journal: str = "", doi: str = "", tags=None,
                       folder: str = "", use_ocr: bool = False,
                       images: bool = False) -> str:
        from .kb_convert import PdfConverter, to_obsidian
        cfg = _KB_CFG.get("cfg")
        kcfg = getattr(cfg, "kb", None) if cfg else None
        vault = (getattr(kcfg, "obsidian_vault", "") or "").strip() if kcfg else ""

        # ★ vault 没配就**如实报错**，不猜路径。
        # 猜一个"大概是这里"然后往那儿写笔记，是本仓最忌的静默行为：
        # 用户会在一个自己不知道的目录里发现一堆文件。
        if not vault:
            return json.dumps({
                "ok": False,
                "error": "没有配置 Obsidian 库目录（config.json 的 kb.obsidian_vault 是空的）",
                "hint": ("Obsidian 库就是**一个装了 .md 的目录** —— 在 Obsidian 里"
                         "「打开库」时选的那个文件夹。把它的绝对路径填进 "
                         "config.json 的 kb.obsidian_vault，本工具就会把笔记写进去。"
                         "⚠️ 本工具**不替你猜库在哪**，也不替你新建一个库。"),
                "note": "本次**没有写任何文件**。",
            }, ensure_ascii=False)

        raw = str(src or "").strip()
        sp = Path(raw)
        if not sp.is_absolute():
            sp = Path.cwd() / sp

        cv = PdfConverter(python_exe=(getattr(kcfg, "python", "") or None),
                          timeout=float(getattr(kcfg, "timeout", 600) or 600),
                          use_ocr=bool(use_ocr), images=bool(images))
        # ★ 这里**不落临时文件**，直接把 md 拿在内存里交给 to_obsidian。
        #   第一版是"先落一个临时 md、再删掉"，写出来当场撞了两条纪律：
        #   ① `hygiene_test` 的「内核包里零删除调用」—— 删临时文件需要一个
        #      删除调用，而本仓对这个包的要求是**一个都不许有**
        #      （删除一律走 `_tkit` 的重命名路线）；
        #   ② 白落一份文件再删，本身就给"删失败 / 删错对象"留了口子。
        #   既然 `to_obsidian` 收的就是**字符串**，那就没有理由经过磁盘 ——
        #   少一次写、少一次删、少一类失败。
        #   ⚠️ 注释里**不要原样写出删除调用的语法**：hygiene_test 有一条
        #   "误报对照"专门要求注释里引述调用语法的行**不许命中**，
        #   写了那个字样就会把那条对照判红（实测踩过）。
        conv = cv.convert(sp, out=None, use_ocr=bool(use_ocr),
                          images=bool(images), need_text=True)
        if not conv.ok:
            return json.dumps({
                "ok": False, "error": conv.error, "hint": conv.hint,
                "verdict": conv.verdict,
                "note": "转换没成功 ⇒ **vault 里没有写入任何东西**。",
            }, ensure_ascii=False)
        md = conv.text
        if not md:
            return json.dumps({
                "ok": False,
                "error": "转换报告成功，但**正文没有回传**（conv.text 为空）",
                "hint": "这是内核的缺陷，不是 PDF 的问题。请报告这一条。",
                "note": "本次**没有写任何文件**。",
            }, ensure_ascii=False)

        meta = {"title": title or sp.stem, "authors": authors or [],
                "year": year, "journal": journal, "doi": doi,
                "tags": tags or [], "_images": bool(images)}
        r = to_obsidian(md, meta, vault, folder=folder, name=title or sp.stem)
        r["convert"] = {"pages": conv.pages, "chars": conv.chars,
                        "seconds": conv.seconds, "notes": conv.notes}
        if r.get("ok"):
            r["next"] = (f"笔记已落进 Obsidian 库：{r.get('path')}\n"
                         "写进 vault **不等于**进了 kb 库 —— 两者是分开的两套："
                         "kb 库（`kb_search`/`kb_ask`）**只收 PDF**，"
                         "入库是把那份 **PDF 原文**交给 `kb_add`/`kb_ingest`，"
                         "**litkb 入库时自己会抽全文**（实测同一篇：库内 148,087 字符、"
                         "段落级索引带页码出处）；这份 Markdown 是**给人读、给 Obsidian 精读**用的，"
                         "不是入库格式，同一篇没必要两边都存正文。")
        return json.dumps(r, ensure_ascii=False, default=str)

    return {"convert_pdf": convert_pdf, "kb_to_obsidian": kb_to_obsidian}


#: 让工具函数拿到 cfg（由 Rongke 在装配时填）。用模块级而不是闭包，
#: 是因为注册表只存函数对象，而这两个工具需要读配置（python/vault/timeout）。
_KB_CFG: dict = {"cfg": None, "registry": None}


SET_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "pptx": {
            "type": "string",
            "description": "要写备注的 .pptx 路径（相对工作区即可，如 \"deck.pptx\"）。",
        },
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "**按页序一一对应**的讲稿，每页一段，长度必须**恰好等于页数**"
                "（第 1 段配第 1 页）。某页确实不要讲稿也要占位给空串 \"\"。\n"
                "每段建议写清：这一页要讲什么、数据的来源与口径、"
                "听众最可能追问的那个问题。备注栏就是讲稿本身。"),
        },
    },
    "required": ["pptx", "notes"],
}


#: `deck_from_template`：在**品牌模板**的页上填字出稿（保真路线）。
TEMPLATE_DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "out": {
            "type": "string",
            "description": "产出到工作区里的哪个 .pptx（相对路径即可，如 \"deck.pptx\"）。",
        },
        "pages": {
            "type": "array",
            "description": (
                "逐页：`[{\"page\": \"cover\", \"slots\": {\"2\": \"标题\", \"3\": \"汇报人：…\"}}, …]`。\n"
                "`page` 只能取 `deck_template_plan()` 列出的页 id（section-1..4 是四个章节页）。\n"
                "`slots` 的键是**槽位索引**（清单里的 `[ 2]`），值是文字（字符串列表 = 多段）。\n"
                "⚠️ **每个槽都必须填**：不填就会把模板里的「请输入文本」原样交出去，工具会拒绝并点名缺哪些。\n"
                "⚠️ **字数必须 ≤ 清单里标的上限**（那是从几何与字号算出来的，超了必被拒）。\n"
                "⚠️ 同一个模板页只能用一次（要两个章节页请用 section-1 / section-2）。"),
        },
        "template": {
            "type": "string",
            "description": "模板 .pptx 的绝对路径；留空则用 `config.json` 里配的那个。",
        },
    },
    "required": ["out", "pages"],
}


STYLE_DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "pptx": {
            "type": "string",
            "description": "要套视觉主题的 .pptx 路径（相对工作区即可，如 \"deck.pptx\"）。",
        },
        "theme": {
            "type": "string",
            "description": (
                "主题名。目前只有 \"juanqing\"（绢青陈墨：绢底墨字、朱砂点睛、"
                "宋体标题＋雅黑正文）。**不要自己编色号或字体名** —— "
                "体检判据按同一份主题定义核，编出来的不算数。"),
        },
        "footer": {
            "type": "boolean",
            "description": "是否加页脚细线与页码（默认 true）。",
        },
    },
    "required": ["pptx"],
}


DECK_REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "detail": {
            "type": "boolean",
            "description": (
                "false（默认）⇒ 给**逐页标题 + 页型序列**的紧凑版；"
                "true ⇒ 连同**每页正文与讲稿备注**一起给（照着填的完整示范）。"
                "没有把握时先用默认。"),
        },
    },
}


COMPOSE_DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "pptx": {
            "type": "string",
            "description": "要排版的 .pptx 路径（相对工作区即可，如 \"deck.pptx\"）。",
        },
        "pages": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "**逐页一个页型**，长度必须**恰好等于页数**（第 1 项配第 1 页）。\n"
                "可选值：cover（封面）/ section（章节）/ content（内容）/ "
                "two-col（双栏）/ chart（图表）/ closing（收束）。\n"
                "你**只报页型**，版面几何、字号、对齐全部由内核按设计规范落。\n"
                "判断页型的口径：第一页 cover，最后一页 closing，"
                "页面主体是一张图/表/图表就 chart，其余 content。"
                "**没把握就先调一次 `deck_reference`** —— 它给出一份六种页型"
                "全用到的参考样例，照着比对比猜准。"),
        },
        "theme": {
            "type": "string",
            "description": "主题名，目前只有 \"juanqing\"（绢青陈墨）。不要自己编色号。",
        },
        "footer": {
            "type": "boolean",
            "description": "是否加页脚细线、落款与页码（默认 true；封面/章节/收束页本就不带）。",
        },
        "footer_label": {
            "type": "string",
            "description": "页脚左侧的落款文字，如 \"课题名 ｜ 第 N 次组会汇报\"。留空则只画线不写字。",
        },
        "out": {
            "type": "string",
            "description": (
                "另存到新文件（相对工作区）。**建议留空** —— 留空即原地重排。\n"
                "⚠️ 本工具是**就地改写**：留空会覆盖 `pptx` 指向的那份。"
                "想保留未排版的原件，请先自己复制一份（内核无法替你回滚）。"),
        },
    },
    "required": ["pptx", "pages"],
}


def _make_read_file() -> "Callable[..., str]":
    """读**本任务工作区里的文本文件**。

    ★ 这是内核一直缺的一个**基元**，2026-09-21 实跑才抓到。
    当时挂载的 67 个工具里**没有任何一个能读本地文本文件**：
      · `import_document` 只吃 pdf/docx —— 喂 `.md` 直接回
        「不支持的格式: md（仅 pdf/docx）」。**它能"输出"规范化 Markdown，
        却读不进 Markdown**，这是最讽刺的一处；
      · `web_extract` 只吃 URL，给相对路径回 `invalid_extract_url`
        （而且它以 `ok: true` 返回，连"这条路不通"都不说）；
      · `read_skill` / `load_skill` 只能读**技能目录**里的文件，
        拿材料路径去调会被"指向技能目录之外"拒掉。
    于是模型拿到一份 `.md` 材料后**无路可走**：实测它把
    `export_docx`（想把 md 转成 docx 再 import）、`load_skill(file=材料)`、
    `web_extract` 三条歪路各撞一遍，**18 次调用零进展**，PPT 一个工具没碰。

    ⇒ 结论不是"教它绕路"，是**补上缺掉的基元**。
    通用问法：**「这件事，我给了它工具吗？」**
    —— 不是"我告诉它该怎么做了吗"。没有工具的动作，写多少纪律都是空转。

    安全边界：注册表的工作区围栏只管 `mutating` 工具，读工具默认豁免；
    所以**围栏必须写在这里**，否则 `read_file("/etc/passwd")` 就是一条读取通道。
    """
    def read_file(path: str, max_chars: int = 20000) -> str:
        root = Path.cwd().resolve()
        raw = str(path or "").strip()
        if not raw:
            raise RuntimeError("path 不能为空：给工作区里的文件路径（如「素材.md」）。")
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        try:
            p = p.resolve()
        except Exception as e:
            raise RuntimeError(f"路径无法解析：{raw}（{type(e).__name__}: {e}）") from None

        # ── 围栏：解析后必须落在工作区内（**先解析再判**，防 `../` 绕出去）──
        if p != root and root not in p.parents:
            raise RuntimeError(
                f"⛔ 越界：{p}\n   本任务工作区是 {root}，只能读它里面的文件。")

        if p.is_dir():
            names = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
            return (f"（{raw} 是**目录**，不是文件）里面有 {len(names)} 项：\n  "
                    + "\n  ".join(names[:60]))
        if not p.is_file():
            # ★ 找不到时**列出真实邻居** —— 这一条直接治"猜路径"：
            #   实测模型会连猜 58 次不同的名字（.md→.docx→.pdf→materials/…），
            #   而它缺的从来不是"更努力的猜"，是**一份真实清单**。
            try:
                near = sorted(x.name + ("/" if x.is_dir() else "")
                              for x in root.iterdir())[:60]
            except Exception:
                near = []
            raise RuntimeError(
                f"文件不存在：{raw}\n   工作区 {root} 里真实有的是：\n    "
                + ("\n    ".join(near) if near else "（空）")
                + "\n   ⚠️ 请**照抄**上面某个名字，不要再换后缀或换目录去猜。")

        blob = p.read_bytes()
        text, enc, err = "", "", ""
        for _e in ("utf-8", "utf-8-sig", "gbk"):
            try:
                text, enc = blob.decode(_e), _e
                break
            except UnicodeDecodeError as e:
                err = f"{_e}: {e}"
        if not enc:
            raise RuntimeError(
                f"读不出文本：{p.name}（{len(blob)} 字节）不是 UTF-8/GBK 文本 —— "
                f"{err}。二进制文件（pdf/docx/图片）请用 `import_document` "
                f"或 `figure_audit_extract`，别用本工具。")

        cap = int(max_chars) if str(max_chars).strip() else 20000
        cap = max(200, min(cap, 200000))
        head = (f"═══ {p.name} ═══（{len(blob)} 字节 · 编码 {enc} · "
                f"{text.count(chr(10)) + 1} 行）\n")
        if len(text) > cap:
            return (head + text[:cap]
                    + f"\n…（已截断：全文 {len(text)} 字符，这里给了前 {cap} 字符。"
                      f"要读后面用 max_chars 调大，或分段读。）")
        return head + text

    return read_file


def _make_write_file() -> "Callable[..., str]":
    """把文本写进**本任务工作区里的文件**。`read_file` 的对偶基元。

    ★ 这是内核缺的**第三个基元**，2026-10-14 实跑抓到（第一个 `read_file`
    2026-09-21、第二个 `set_slide_notes`）。

    当时的情形：契约要求产出两份**指定路径**的文本文件（综述 + 写作说明），
    而挂载的 56 个工具里**没有任何一个能写任意文本**：
      · `topic_write` 只能写课题的 plan/reading/evidence **三个固定文件名**
        （`topic.py:write_doc` 的 doc 参数只认这三个 key），写不到 `综述.md`；
      · `kb_note` / `record_skill_use` 写的是库内记录与台账，不是文件；
      · `export_docx` 只吃 md 转 docx，且实测在路径不存在时直接抛 Traceback；
      · `mechanical_fix` 只能**就地修改已存在**的文件，不能创建 —— 实测回
        「文件不存在」；
      · `convert_pdf(out=…)` 要有上游 PDF 才谈得上落盘。
    模型因此**无路可走**：它老老实实把 9 条路各撞一遍（含派子代理、派
    workbuddy 执行者），拿到 9 份"此路不通"的证据，然后**如实报告阻塞**。
    它没编、没假装完成 —— 但一份能写文件的活干不成，缺的是工具。

    ⇒ 通用问法还是那一句：**「这件事，我给了它工具吗？」**
    这次的教训更硬：**"能读"不等于"能写"**。上一个基元补的是读，
    于是"读得到材料"这件事被解决了，而"把成果放回盘上"一直悬空 ——
    两次都是同一个盲区：把「模型会说话」当成「模型能落盘」。

    安全边界：与 `read_file` **完全同一条**（先 resolve 再判、必须落在
    工作区内）。写工具比读工具更需要它，所以这里再抄一遍而不复用 ——
    围栏这种东西，两处各写一遍才看得见；抽成函数会让"哪些工具受管"变模糊。
    注册时标 `mutating=True`，让注册表的工作区围栏也管一道（双保险）。
    """
    def write_file(path: str, text: str, append: bool = False) -> str:
        root = Path.cwd().resolve()
        raw = str(path or "").strip()
        if not raw:
            raise RuntimeError("path 不能为空：给工作区里的落点路径（如「综述.md」）。")
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        try:
            p = p.resolve()
        except Exception as e:
            raise RuntimeError(f"路径无法解析：{raw}（{type(e).__name__}: {e}）") from None

        # ── 围栏：与 read_file 同一条 —— 先解析再判，防 `../` 绕出去 ──
        if p != root and root not in p.parents:
            raise RuntimeError(
                f"⛔ 越界：{p}\n   本任务工作区是 {root}，只能写它里面的文件。")

        if p.is_dir():
            raise RuntimeError(
                f"⛔ 落点是个**目录**：{p}\n   请给一个文件名（如 `{p.name}/综述.md`）。")

        body = text if isinstance(text, str) else str(text or "")
        if not body.strip():
            raise RuntimeError("text 为空 —— 空写没有意义。要建空文件请写明用途再调。")

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"建不了上级目录 {p.parent}：{type(e).__name__}: {e}") from None

        mode = "a" if append else "w"
        existed = p.is_file()
        before = p.stat().st_size if existed else 0
        try:
            with p.open(mode, encoding="utf-8", newline="\n") as f:
                if append and existed:
                    f.write("\n")
                f.write(body)
        except Exception as e:
            raise RuntimeError(f"写入失败 {p}：{type(e).__name__}: {e}") from None

        # ── 回读核验（照 `write_doc` 的做法）：写这类工具必须自己证明写进去了 ──
        try:
            back = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise RuntimeError(f"写后回读失败 {p}：{type(e).__name__}: {e}") from None
        tail = body.strip()[-200:].strip()
        ok = bool(tail) and back.strip().endswith(tail)
        if not ok:
            raise RuntimeError(
                f"⚠️ 写后回读**对不上**：{p}（{len(back)} 字符）末尾不是本次写入的内容。"
                f"文件可能没写成功，请用 read_file 核一遍再往下走。")

        after = p.stat().st_size
        act = "追加" if (append and existed) else ("覆盖" if existed else "新建")
        # ★ 回执里**必须带绝对路径**：`read_file`/`write_file` 的根都是 `Path.cwd()`，
        #   而内核在跑任务前会 `os.chdir(工作区)`（`loop.py:2817`）。
        #   于是模型写的相对路径「综述.md」实际落在 `<工作区>/综述.md` ——
        #   2026-10-14 实测：模型连写 3 次都报 ok，交付物却不在用户要的位置，
        #   因为回执只说 `综述.md`，模型**没有任何办法知道** cwd 不是它以为的那个。
        #   同型的教训见 `read_file`（找不到时列真实邻居）。这里同理：**把真相摊开**。
        return (f"✔ 已{act}：{p.name}（{before} → {after} 字节，全文 {len(back)} 字符）\n"
                f"   绝对路径：{p}\n"
                f"   ⚠️ 本任务的工作区根是 {root}；相对路径按它解析。"
                f"要落到别处，请在 path 里给**绝对路径**（也必须在工作区内）。\n"
                f"   末 80 字：…{back.strip()[-80:]}")

    return write_file


def _make_audit_deck(cfg) -> "Callable[..., str]":
    """造 `audit_deck` 工具的执行体。

    与 `audit_figure` 同构：返回**紧凑 JSON**，第 9 条 deck 门要
    `json.loads` 它来判定。全文报告写盘，路径一并返回。

    超长保护也照抄：注册表会把超长结果截断，一截断 JSON 就废了 ——
    宁可让模型少读一段散文，也不能让门拿到半截 JSON。
    """
    def audit_deck(pptx: str, require_notes: bool = False,
                   deep_contrast: bool = False) -> str:
        import json as _json
        from .deck import DeckAuditor

        if not str(pptx or "").strip():
            raise RuntimeError("pptx 不能为空：给最终 .pptx 的路径（相对工作区即可）。")
        # 相对路径按**进程 cwd** 解析 —— 任务期间它就是本次 workspace
        # （loop.run() 会 chdir 进去），所以报告只会落在围栏内。
        wd = Path.cwd()
        au = DeckAuditor(python_exe=getattr(cfg, "python", "") or None,
                         skills_dir=getattr(cfg, "skills_dir", "") or None,
                         cfg=cfg)
        # ★ W10：`deep_contrast` 不传/false ⇒ 传 **None** 而不是 False —— 让 `audit()`
        #   回落到配置项 `config.deck.deep_contrast` ⇒ **不传时与今天逐字相同**
        #   （工单 §二）。只有显式 true 才点名开启那条 ~15 秒的渲染像素校验。
        rep = au.audit(pptx, workdir=wd,
                       deep_contrast=None if not deep_contrast else True)
        # 只影响**给模型看的那份判定**；门会从契约独立重判（见 gate.py 第 9 条）。
        rep.strict_notes = bool(require_notes)
        try:
            rep.save(Path(wd) / "deck_report.json")
        except Exception:
            pass

        payload = rep.summary_json()
        payload["report_text"] = rep.format_for_model()
        payload["facts"] = rep.facts.to_dict()
        blob = _json.dumps(payload, ensure_ascii=False, default=str)
        if len(blob) > 9000:
            payload.pop("report_text", None)
            payload.pop("facts", None)
            payload["note"] = "报告已过长，人读版见 report_path。"
            blob = _json.dumps(payload, ensure_ascii=False, default=str)
        return blob

    return audit_deck


def _make_set_slide_notes(cfg) -> "Callable[..., str]":
    """造 `set_slide_notes` 工具的执行体。**与 `audit_deck` 成对：一个量，一个改。**

    ★ 这是内核缺的**第二个基元**（第一个是 `read_file`），同样是实跑抓到的，
    同样**一条基元抵得过若干轮提示词改造**。

    为什么不是 `run_python`：见 `deck.write_slide_notes` 的 docstring
    （确定性 / 可测 / 不把模型推到它够不着的地方）。这里只说工具层的取舍：

    * 失败一律走**异常**通道（`ok=False`），不渲染成"成功文本里的一个警告" ——
      参数不对、路径越界、页数不匹配都不是"结果的一种"，是**这次调用没成**。
      渲染成 ok 的成功文本，模型就会以为可以往下走了（G 型的经典制造法）。
    * `mutating=True`：它真的改盘上的 .pptx，该受工作区围栏管。
      ⚠️ 但围栏只看**绝对路径**参数，相对路径（含 `../`）它一律放行 ⇒
      越界判定由 `write_slide_notes` 自己**先 resolve 再判**地关掉。
    """
    def set_slide_notes(pptx: str, notes) -> str:
        from .deck import write_slide_notes

        r = write_slide_notes(pptx, notes, workdir=Path.cwd(), cfg=cfg)
        text = r.format_for_model()
        if not r.ok:
            raise RuntimeError(text)
        return text

    return set_slide_notes


def _make_style_deck(cfg) -> "Callable[..., str]":
    """造 `style_deck` 工具的执行体。**与 `audit_deck` 成对：一个量，一个扮。**

    为什么不是 `run_python`：同 `write_slide_notes` —— 模型给**主题名**，
    代码由内核生成。它没有装包、改环境的能力，把它推到 `lxml` 的报错面前，
    换来的只会是"换个参数再试一次"。

    ⚠️ 这个工具的存在本身就是一条纪律的产物：**"我给了它工具吗"**。
    用户看完一份结构全绿的 deck 只说了一句"那能做的这么丑" —— 丑的原因之一
    就是**没有任何一个真能改外观的工具**（看着对口的那个是空壳）。
    """

    def style_deck(pptx: str, theme: "str | None" = None,
                   footer: bool = True) -> str:
        from .deck import style_deck as _style, DEFAULT_DECK_THEME

        r = _style(pptx, theme=(theme or DEFAULT_DECK_THEME),
                   footer=bool(footer), workdir=Path.cwd(), cfg=cfg)
        text = r.format_for_model()
        if not r.ok:
            raise RuntimeError(text)
        return text

    return style_deck


def _make_deck_from_template(cfg) -> "Callable[..., str]":
    """造 `deck_from_template` 的执行体 —— **品牌保真**路线。

    ★ 为什么它是**第一条**该想到的路（2026-09-22，用户递来学校模板）：
      用户看完一份五门全绿的 deck 只说「太差了，不能直接上组会」，根因是
      **全链没有任何一处存着设计资产**。而"学校的模板"就是一份现成的、
      发过版的资产 —— 它的价值在**全部装饰**里（满版蓝底、450pt 章节数字、
      校徽、出血照片）。把设计反推成"12 个色槽 + 六组几何"只能得到**近似品**，
      品牌的要求是"就是它"。
      ⇒ **人怎么用模板，就让 agent 怎么用**：保留模板页、在槽上填字、多余页删掉。
        一个字都不重绘。

    ⚠️ 它**不代替门**：产出之后仍须 `audit_deck`（否则第 9 条硬门拦交付）。
       "能按模板出稿"和"这份稿能讲"是两件事。
    """

    def deck_from_template(out: str, pages, template: "str | None" = None) -> str:
        # ★ 主控勘误（2026-09-27）：`template_name_of` 必须在这儿 import。
        #   原先只 import 了 `deck_from_template` + `load_template`，而下面
        #   第 4 行就调 `template_name_of(cfg)` ⇒ **本工具每次调用都抛 NameError**，
        #   100% 不可用。而 `regress.py --only deck` 396 项全绿 ——
        #   因为套件直接测 `deck.py` 的函数，**从不经过这个薄包装层**
        #   （"测了库、没测接缝"）。
        #   讽刺的是同一文件 `loop.py:1799` 的 `deck_template_plan` 那条路
        #   **import 对了**，所以只有"真正出稿"这条路是断的。
        #   ⇒ 修这一行只是治标；`deck_test.py` 里补了"每个工具工厂都不得抛
        #     NameError"的冒烟守卫，那才是治本（这类错会再犯）。
        from .deck import deck_from_template as _tpl, load_template, template_name_of

        # ★ 槽位表由 `config.json` 的 `deck.template_name` **单源**指定。
        #   写死 "zjut" 就是"两处真相"——换模板要记得改两个地方，早晚漏一处。
        _name = template_name_of(cfg)
        r = _tpl(out, pages, template_pptx=(template or ""), workdir=Path.cwd(),
                 cfg=cfg, template_slots=load_template(_name), template_name=_name)
        text = r.format_for_model()
        if not r.ok:
            raise RuntimeError(text)
        return text

    return deck_from_template


def _make_compose_deck(cfg) -> "Callable[..., str]":
    """造 `compose_deck` 工具的执行体。**与 `style_deck` 二选一：一个精排，一个快套。**

    ★ 为什么这个工具非有不可（2026-09-21，用户原话）：

      用户看完一份**五门全绿**的 13 页组会 deck，只说了一句
      「**太差了，不能直接上组会**」。而 `style_deck` 已经跑过了。
      量下来根因不在模型、也不在判据 —— 是**整条链路上没有任何一处存着设计资产**：
      起手是 python-pptx 的空白 4:3 默认母版，`style_deck` 的全部打扮 =
      「换 12 个色槽 + 换字体 + 一条 0.01 英寸的页脚线」。
      全链最"具体"的设计数值，就是那条页脚线。

      ⇒ **判据只能划定下限；产出上限来自资产本身。**
        别人的 agent「加个 skill 就好看」，加的其实就是这套东西：把设计师的
        隐性判断（版式比例、色彩克制、字号对比、留白）**固化成可判定的数字**。
        我们把这份资产放进 `rongke/design.py`，再由本工具**确定性地**落到版面上。

    ★ 为什么不能只改 `style_deck`：那边的契约是"**不动版面结构**"（快套）。
      而「好看」这件事，**版面本身才是大头** —— 只换色换字，骨架还是那副骨架。
      两个工具的目标不同，所以**并存**：赶时间/只想起手打扮 → `style_deck`；
      要交付级 → `compose_deck`。**两个都调 = 双重坐标系**（见各自 docstring）。

    ★ 为什么模型只报**页型**、不报坐标：它本来就没有"坐标感"
      （见 `config.py` 的模型名）。让它算 px 只会得到一串貌似合理的数字。
      它擅长的是**判断这一页是干什么的** —— 所以只要它交页型，
      几何由内核查表。**这是把"模型会做的"和"资产说了算的"分开。**
    """

    def compose_deck(pptx: str, pages, theme: "str | None" = None,
                     footer: bool = True, footer_label: str = "",
                     out: "str | None" = None) -> str:
        from .deck import compose_deck as _compose, DEFAULT_DECK_THEME

        r = _compose(pptx, pages, theme=(theme or DEFAULT_DECK_THEME),
                     footer=bool(footer), footer_label=str(footer_label or ""),
                     out=(out or None), workdir=Path.cwd(), cfg=cfg)
        text = r.format_for_model()
        if not r.ok:
            raise RuntimeError(text)
        return text

    return compose_deck


def _make_deck_reference(cfg) -> "Callable[..., str]":
    """造 `deck_reference` 工具的执行体。**只读**，不碰任何文件。

    ★★ 为什么要有这个工具（2026-09-21，用户那句方向纠正的直接产物）：

      用户说：**「人家同样是用 agent，只加了个 skill 就能做出好看的图了，
      而你在这里做了半天，依旧不好看。」**

      那份 skill 里真正起作用的不是形容词（「简洁大气」「留白充足」），
      而是**一个可以照着比对的成品**。所以我们把一份 12 页、六种页型全用到的
      组会 deck 固化进仓库（`docs/deck-reference/`），由本工具交给模型。

    ★★ 所以这个工具**返回文本，不返回图** —— 这不是偷懒，是模型看不见图：

      `llm.py` 把 `content` 硬编码成字符串、`loop.py::_messages_chars` 只处理
      `isinstance(c, str)`、全仓 `image_url`/`input_image` **零命中**。
      ⇒ 给模型一张 PNG 等于没给。**参考样例要起作用，必须是文本的、完整的、
        可抄的**：页型序列 + 逐页标题 + 逐页正文 + 讲稿备注。

      路径里的 `.pptx` / `.sheet.png` / `.compare.png` 是**给人看的**，
      工具会把路径报给模型，但**不假装模型看过**。

    ★ 为什么也把「笔记要写什么」放进示范：备注栏（来源、口径、会被问到的问题）
      才是汇报时真正要说的话。没有它，deck 只是给人看的摘要 ——
      而 `notes_coverage` 判据要 60%，光靠一句"记得写备注"是补不上的。
    """

    def deck_reference(detail: bool = False) -> str:
        from . import deckref

        head = [
            "【参考样例 · 组会 deck 长得对是什么样】",
            "",
            "★ 这是**文本示范**，直接照抄结构即可。"
            "（内核的模型通道是纯文本、读不了图片 —— 所以示范给的是文字，不是图。）",
            "",
        ]
        body = deckref.reference_text(summary=not bool(detail))
        tail = [
            "",
            "── 随库发布的成品（**给人看的**，你读不了图，别假装读过）──",
            f"  成品 deck ：{deckref.REFERENCE_PPTX}",
            f"  分页预览图：{deckref.REFERENCE_SHEET_PNG}",
            f"  前后对照图：{deckref.REFERENCE_COMPARE_PNG}",
            "",
            "── 接下来怎么用 ──",
            "  1) 先按上面的**页型序列**与每页论点密度，把你的内容写成逐页文本；",
            "  2) 用上游 MCP 建 deck（`create_presentation` / 加页 / 写文字），"
            "**页数与页型序列一一对应**；",
            "  3) 调 `compose_deck(pptx, pages=[逐页页型])` 精排；",
            "  4) 调 `set_slide_notes(pptx, notes=[逐页一段])` 补讲稿"
            "（长度必须等于页数）；",
            "  5) 最后调 `audit_deck` —— 不过就不要宣布完成。",
            "",
            "⚠️ 参考样例**自己也是这么过门的**（五门全绿、零提示）："
            "它就是标准的可执行版本，不是理想化的描述。",
        ]
        return "\n".join(head) + body + "\n".join(tail)

    return deck_reference


def _make_plot_svg(cfg) -> "Callable[..., str]":
    """造 `plot_svg` 工具的执行体 —— **零第三方依赖**的原生出图。

    ## 为什么单独一个工具，而不是把 `plot_data` 改成「没装库就用 SVG」

    两条路线的**判据不同**，混在一个返回值里会长出最坏的那种假成功：
    模型拿到 `ok:true` 就以为图可以送审，而 `figure.py` 的六道门其实一道没跑。
    分开之后，工具名本身就是路线声明 —— 门（见 `gate.py` 的预览分支）
    能只看台账里出现过哪个工具，就知道本次有没有资格走预览路线。

    ## 不冒充过门
    返回值里 `scope="preview"` 与 `audit_scope` 都是固定文案，不随数据变化；
    `ok` 只表示「SVG 结构自检通过」，不表示「出版级审计通过」。
    """
    def plot_svg(table: str, kind: str = "", x: str = "", y: str = "",
                 group: str = "", title: str = "", width_mm: float = None,
                 out: str = "") -> str:
        import json as _json
        from . import analysis as _an
        from . import svgplot as _sv

        wd = Path.cwd()
        tp = Path(table) if Path(table).is_absolute() else (wd / table)
        if not tp.exists():
            raise RuntimeError(f"数据表不存在：{tp}。先把数据放进工作区。")
        try:
            rows, meta = _an.read_table(tp)
        except _an.TableError as e:
            raise RuntimeError(f"表读不进来：{e}")
        cols = _an.profile_table(rows)
        if not cols:
            raise RuntimeError(f"{tp.name} 里一列都没测出来，不画空图。")
        try:
            plan = _an.plan_from_shape(
                tp.name, meta, cols, kind=kind, x=x, y=y, group=group,
                title=title,
                width_mm=float(width_mm) if width_mm else _an.COL_WIDTH_MM)
        except ValueError as e:
            raise RuntimeError(f"选图型失败：{e}")

        dest = Path(out) if out else (wd / f"{plan.get('stem') or 'figure'}.svg")
        if not dest.is_absolute():
            dest = wd / dest
        if dest.suffix.lower() != ".svg":
            dest = dest.with_suffix(".svg")
        try:
            rep = _sv.render_svg(rows, plan, dest)
        except _sv.SVGPlotError as e:
            raise RuntimeError(f"原生 SVG 后端画不了这张图：{e}")
        except Exception as e:                              # noqa: BLE001
            raise RuntimeError(f"原生 SVG 后端出错：{type(e).__name__}: {e}")

        payload = dict(rep)
        payload["columns"] = [c.to_dict() for c in cols]
        payload["suggestions"] = [s.to_dict() for s in _an.recommend(cols)]
        payload["next"] = (
            "这是**预览级** SVG：可直接插网页 / PPT / 给用户看。"
            "⚠️ 不要把它当投稿级 PDF 交给 audit_figure —— 那六道门审的是 PDF，"
            "本次一道都没跑。若任务要求出版级配图，装 "
            'pip install "rongke[figure]" 后改用 plot_data + audit_figure。')
        return _json.dumps(payload, ensure_ascii=False, default=str)

    return plot_svg


def _make_plot_data(cfg) -> "Callable[..., str]":
    """造 `plot_data` 工具的执行体 —— 数据分析成图。

    ## 为什么这是**一个**工具而不是三个

    读表、选图型、写脚本、跑图，这四步之间没有**需要模型决策**的地方：
    选图型的判据是测量出来的（列类型、基数、有没有时间轴），不是靠灵感。
    拆成四个工具只会让模型在中间四次要一遍遍重述上下文，
    还多出四次填错参数的机会。所以合成一个，输出里把中间结论
    （`columns` / `suggestions` / `plan`）全部摊开给它看。

    ## 它**不**做审计

    图面审计仍由 `audit_figure` 负责（第 8 条硬门点名要它）。
    这里只把 `pdf` + `script` + **契约** + **图注草稿**一并返回，
    模型原样转手给 `audit_figure` 即可 —— 契约和图注都由模块按图型生成，
    不让模型手填，因为那两处最容易填错（`role` 填成"折线图"、
    `formats` 漏掉位图），而填错的后果是"门全绿但总判不过"。
    """
    def plot_data(table: str, kind: str = "", x: str = "", y: str = "",
                  group: str = "", title: str = "", width_mm: float = None,
                  journal: str = "") -> str:
        import json as _json
        from . import analysis as _an

        wd = Path.cwd()
        # 相对路径按**进程 cwd** 解析 —— 任务期间它就是本次 workspace
        # （`loop.run()` 会 chdir 进去），所以产物只会落在围栏内。
        tp = Path(table) if Path(table).is_absolute() else (wd / table)
        if not tp.exists():
            raise RuntimeError(f"数据表不存在：{tp}。先把数据放进工作区。")
        width = float(width_mm) if width_mm else _an.COL_WIDTH_MM
        try:
            rep = _an.analyze(tp, kind=kind, x=x, y=y, group=group,
                              title=title, width_mm=width,
                              out_dir=wd, python_exe=(cfg.python or None),
                              timeout=float(getattr(cfg, "timeout", 300)))
        except _an.TableError as e:
            raise RuntimeError(str(e))

        payload: dict = {
            "table": tp.name,
            "columns": rep.get("columns"),
            "suggestions": rep.get("suggestions"),
            "plan": rep.get("plan"),
            "ran": rep.get("ran"),
            "run": rep.get("run"),
            "error": rep.get("error"),
        }
        if rep.get("plan"):
            cd = _an.contract_for(rep["plan"], journal=journal)
            lg = _an.legend_for(cd, meta=rep.get("meta"),
                                center=str((cd["panels"] or [{}])[0].get("center") or ""),
                                spread=str((cd["panels"] or [{}])[0].get("spread") or ""))
            payload["contract"] = cd
            payload["legend_draft"] = lg
            payload["next"] = (
                "把 contract 与 legend_draft 原样交给 audit_figure（pdf 用 run.pdf、"
                "source 用 script 的文件名），图注里的 ___ 与结论句必须由你补全后再审。")

        # ── 2026-11-05：缺绘图栈时自动降级到荣珂原生 SVG 后端 ──────────
        # 起因：安装包把 matplotlib/pandas/PyMuPDF 全排除了（`rongke.spec`
        # 的 excludes），所以「别人装完」的 `plot_data` 只会回一句装置不全。
        # 那种状态下界面能开、工具在册、一张图都出不来 —— 是产品缺件，
        # 不是用户配置错。这里补一条零依赖路线，并且**如实标注**它是预览级。
        if rep.get("error") and not rep.get("ran"):
            try:
                from . import svgplot as _sv
                rows, _meta = _an.read_table(tp)
                svg_path = wd / f"{(rep.get('plan') or {}).get('stem') or 'figure'}.svg"
                payload["native_svg"] = _sv.render_svg(
                    rows, rep.get("plan") or {}, svg_path)
                payload["fallback"] = (
                    "本机缺 matplotlib/pandas/PyMuPDF：已**自动降级**为荣珂原生 SVG 后端，"
                    "产出预览图（见 native_svg.svg）。⚠️ 这不是投稿级 PDF，"
                    "PDF 六道渲染后审计门一道都没跑，不要把下面的 contract 交给 audit_figure。"
                    '要出版级请 pip install "rongke[figure]" 后重跑 plot_data。')
                if payload["native_svg"].get("ok"):
                    payload["next"] = (
                        "本次是**预览级 SVG 路线**：把 native_svg.svg 给用户看即可，"
                        "不要提交 audit_figure（没有 PDF 可审）。"
                        "contract / legend_draft 是投稿路线的模板，本次不适用。")
            except Exception as e:                          # noqa: BLE001
                payload["native_svg_error"] = (
                    f"原生 SVG 后端也没出成：{type(e).__name__}: {e}。"
                    "如实报告缺件，不要假装已有图。")
        blob = _json.dumps(payload, ensure_ascii=False, default=str)
        if len(blob) > 12000:
            # 与 audit_figure 同一条纪律：宁少给一段散文，不给半截 JSON。
            payload.pop("columns", None)
            payload["note"] = "列测量结果已省略（表太宽），plan 里已包含选定的列。"
            blob = _json.dumps(payload, ensure_ascii=False, default=str)
        return blob

    return plot_data


def _make_audit_figure(cfg) -> "Callable[..., str]":
    """造 `audit_figure` 工具的执行体。

    返回值刻意是**紧凑 JSON**：第 8 条图面门要 `json.loads` 它来判定
    （和证据门只认台账同一个路子 —— 不采信模型的复述）。
    全文报告写盘，路径一并返回，人要细看时去看文件。

    **超长保护**：注册表会把超过 `truncate` 的结果截断，一旦截断 JSON 就解析不了。
    所以这里先量一下长度，超了就丢掉人读的那一段、只留机器读的字段 ——
    宁可让模型少看一段散文，也不能让门拿到半截 JSON（那会变成"审计跑了却判不了"）。
    """
    def audit_figure(contract: dict, pdf: str = None, source: str = None,
                     layout_manifest: str = None, legend: str = None,
                     legend_path: str = None) -> str:
        if not isinstance(contract, dict):
            raise RuntimeError("contract 必须是一个对象（见工具说明里的字段清单）。")
        import json as _json
        from .figure import FigureAuditor, FigureContract

        fc = FigureContract.from_dict(contract)
        # 相对路径按**进程 cwd** 解析 —— 任务期间它就是本次 workspace
        # （loop.run() 会 chdir 进去），所以产物只会落在围栏内。
        wd = Path.cwd()
        au = FigureAuditor(python_exe=getattr(cfg, "python", "") or None,
                           skills_dir=getattr(cfg, "skills_dir", "") or None,
                           timeout=float(getattr(cfg, "timeout", 300)))
        rep = au.audit(fc, workdir=wd, source=source, pdf=pdf,
                       layout_manifest=layout_manifest,
                       legend_text=legend or "", legend_path=legend_path)

        payload = rep.summary_json()
        payload["report_text"] = rep.format_for_model()
        payload["auditor"] = au.diag()
        blob = _json.dumps(payload, ensure_ascii=False, default=str)
        if len(blob) > 9000:
            payload.pop("report_text", None)
            payload["note"] = "报告已过长，人读版见 report_path。"
            blob = _json.dumps(payload, ensure_ascii=False, default=str)
        return blob

    return audit_figure


# ══════════════════════ 工具按领域按需挂载 ══════════════════════
# ## 为什么必须做
#
# 加进 PPT 服务端那一刻，工具数从 29 涨到 66、schema 从 15,562 字符涨到
# **50,244**（实测 2026-09-21，`runs/_regress/_probe_domains.py`）。拆开看：
#
#   ppt       37 个  34,461 字符  ← 占全部 **68.6%**
#   research  21 个   9,767 字符
#   figure     4 个   3,422 字符
#   core       4 个   2,594 字符
#
# 而 `_loop` 每次调用都要把这坨 schema 原样重发（它是前缀缓存里最大的稳定段）。
# 给一场「写一段引言」的任务塞 37 个 PPT 工具是纯噪声：既撑大输入，
# 又给模型更多点错的机会。
#
# ## 判据只此一处
#
# 挂载的输入是 `TaskContract.needs_deck`（与第 9 条硬门**同源**），
# 外加一条超集：阶段里有 `slides` 也算。超集方向是**安全**的 ——
# 保证「门会拦的情形，工具一定已经挂上了」。有不变量断言钉住这条
# （`deck_test.py` 的「needs_deck ⇒ ppt 域必在 allow 里」）。
#
# ## 刻意不动别的领域
#
# `figure` 域只有 3,422 字符（6.8%），且 `audit_figure` 是第 8 条硬门**点名要用**
# 的工具 —— 裁它风险大、收益小。所以本次只裁 ppt 一个域：除 ppt 外的行为
# 与加 PPT 之前**一字不差**（可回退、可测量）。
# ── 课题上下文（导师模式 P1/P2，2026-09-22）──────────────────
# 详见 `topic.py` 模块头：为什么「课题」必须跨会话存在、为什么卡点台账是 append-only、
# 以及**为什么第一版的「相似度过滤」被推翻** —— 阈值要靠真实语料标定，而今天
# 一条卡点语料都还没有 ⇒ 任何阈值都是猜（「打红面 99.6% 与 0% 是同一种失败」）。
# 现在是无阈值的**全量浮现**：拦沉默，不拦选择。
TOPIC_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "课题名。留空 = 当前课题（topics/CURRENT 指向的那个）"},
        # ── 以下是**加**的（W28）：一份有界文档索引 + 一窗正文 + 卡点按状态筛。
        #    全部可省，省略时行为与旧版一字不差（旧调用方不受影响）。
        "doc": {"type": "string", "enum": ["plan", "reading", "evidence"],
                "description": "（可选）顺带取这份活文档的正文。省略 = 只给三份文档的"
                               "有界索引（有没有、多少字、截断没截断）。"},
        "offset": {"type": "integer",
                   "description": "（可选）取 `doc` 正文的起始**字符**位置，默认 0"},
        "limit": {"type": "integer",
                  "description": "（可选）卡点最多列几条，默认 10；配 `doc` 时是正文窗口字数"},
        "status": {"type": "string", "enum": ["open", "resolved", "all"],
                   "description": "（可选）卡点按状态筛，默认 all（与旧版一致）"},
    },
}

TOPIC_USE_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "课题名（字母/数字/下划线/短横/点/中文，≤48 字）。已存在则切过去"},
        "title": {"type": "string",
                  "description": "人类可读的标题（只在新建时生效；已存在则原样保留，绝不覆盖）"},
        "question": {"type": "string",
                     "description": "这个课题要回答的科学问题（只在新建时生效）"},
    },
    "required": ["slug"],
}

TOPIC_LINK_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "课题名。**必须显式传** —— 关联是改盘操作，不许靠「上次用的那个」"},
        "doc_id": {"type": "string",
                   "description": "文献 id（kbXXXXXXX）。必须来自 kb_search / kb_list 的真实返回，不要自己拼"},
        "linked": {"type": "boolean",
                   "description": "true = 关联到本课题（默认）；false = 解除关联"},
    },
    "required": ["slug", "doc_id"],
}

TOPIC_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "要改的课题名。**必须显式传**"},
        "title": {"type": "string", "description": "课题名称（≤160 字）。留空 = 不改，**不会被清掉**"},
        "question": {"type": "string", "description": "研究问题（≤1200 字）。留空 = 不改"},
        "stage": {"type": "string",
                  "description": "工作阶段：ideate（构思评估）/ design（研究设计）/ "
                                 "evidence（文献与证据）/ execute（实验与写作）/ submission（投稿与回复）"},
        "next_step": {"type": "string",
                      "description": "用户记下的下一步（≤240 字）。留空字符串 = 清空这一步"},
    },
    "required": ["slug"],
}

RAISE_BLOCKER_SCHEMA = {
    "type": "object",
    "properties": {
        "what": {"type": "string",
                 "description": "卡在哪 —— 一句话说清**具体的卡点**，"
                                "不要写「做了很多尝试」这种等于没写的话"},
        "tried": {"type": "array", "items": {"type": "string"},
                  "description": "已经试过什么（每条一句）。**这是下次最值钱的信息**，别省"},
        "where": {"type": "string",
                  "description": "（可选）卡在哪一步 / 哪个文件 / 哪条命令"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
    "required": ["what"],
}

RESOLVE_BLOCKER_SCHEMA = {
    "type": "object",
    "properties": {
        "blocker_id": {"type": "string", "description": "卡点 id，如 B003（从 topic_status 里拿）"},
        "how": {"type": "string", "description": "是怎么解决的 —— 写清手法，下次撞上同一个坑能直接照抄"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
    "required": ["blocker_id"],
}

TOPIC_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "doc": {"type": "string", "enum": ["plan", "reading", "evidence"],
                "description": "plan = 计划与技术路线 / reading = 读过的文献及从它拿到什么 / "
                               "evidence = 证据表（哪条结论由哪篇支撑）"},
        "text": {"type": "string", "description": "要写的正文"},
        "mode": {"type": "string", "enum": ["append", "replace"],
                 "description": "append（默认，追加）或 replace（整篇覆盖）"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
    "required": ["doc", "text"],
}

# ★ 实验台账（2026-11-02）。为什么单独立一张 schema 而不是复用 topic_write：
#   `topic_write` 写的是**自由文本**（PLAN/READING/EVIDENCE 都是散文），
#   而"没数据"这件事只有**结构化**才判得出来 —— 定性观察与定量测量必须是
#   两个不同的 kind，代码才能算出 `measures==0 and observes>0`。
#   一旦允许它们混在一个自由文本字段里，这个式子永远为假，工具就会一直说
#   "你已经有数据了"，而导师那句"没数据"还在那儿。
#   扁平字段是刻意的：模型填嵌套 object 的准确率明显更低。
LAB_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "enum": ["strain", "batch", "measure", "observe", "metric", "todo",
                          "update", "close", "retract"],
                 "description":
                     "记什么：\n"
                     "strain=建株（code 必填，role=parent/fusant/candidate）\n"
                     "observe=**定性**观察（「长得慢」「看起来快」，不算数据）\n"
                     "measure=**定量**测量（metric+value+unit+n；value 必须是数字）\n"
                     "batch=发酵批次（id/started/expect_days/temp_c）\n"
                     "metric=注册新指标（key/label/unit，之后才允许 measure 用它）\n"
                     "todo=待办（id/what/due）\n"
                     "update=改已有记录（id + fields）\n"
                     "close=闭环（id + how）\n"
                     "retract=**作废**一条记错/记重的事件（seq 或 target + reason）"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
        "code": {"type": "string", "description": "菌株编号，如 S01（strain/observe/measure 必填）"},
        "species": {"type": "string", "description": "（可选）物种，如 Hirsutella sinensis"},
        "role": {"type": "string", "enum": ["parent", "fusant", "candidate"],
                 "description": "（可选）parent=亲本（快慢的参照系）/ fusant=融合子 / candidate=候选"},
        "id": {"type": "string", "description": "批次号 B01 / 待办号 T01 / update、close 要改的那条 id"},
        "metric": {"type": "string",
                   "description": "指标 key：growth_rate / biomass / mannitol / adenosine / "
                                  "ferment_period（或先用 kind=metric 注册的 key）"},
        "value": {"type": "number", "description": "测量值，**必须是数字**（定性判断用 observe）"},
        "unit": {"type": "string", "description": "单位，须与指标要求一致（g/100mL、%、mg/g、h、mm/d）"},
        "n": {"type": "integer", "description": "重复数。n=1 是观察，不是重复"},
        "sd": {"type": "number", "description": "（可选）标准差"},
        "method": {"type": "string", "description": "（可选）测量方法一句话"},
        "batch": {"type": "string", "description": "（可选）这条测量属于哪个批次"},
        "started": {"type": "string", "description": "（batch）起始日 YYYY-MM-DD，用于算已发酵几天"},
        "expect_days": {"type": "integer",
                        "description": "（batch）预计发酵天数；到点会进「下一步」第一条（样品有时效）"},
        "vessel": {"type": "string", "description": "（batch）容器，如 250mL 摇瓶 / 5L 罐"},
        "medium": {"type": "string", "description": "（batch）培养基，如 PDB"},
        "rpm": {"type": "integer", "description": "（batch）转速 r/min"},
        "temp_c": {"type": "number", "description": "（可选）培养温度 ℃"},
        "growth": {"type": "string", "description": "（可选）observe 的定性判断：slow / fast / normal"},
        "text": {"type": "string", "description": "observe 的原话（用户怎么描述的，照抄）"},
        "evidence": {"type": "string", "description": "（可选）这条观察的依据：照片/日期/平板号"},
        "what": {"type": "string", "description": "todo 的内容"},
        "due": {"type": "string", "description": "（可选）todo 截止日 YYYY-MM-DD"},
        "how": {"type": "string", "description": "close 时：是怎么完成的/怎么解决的"},
        "note": {"type": "string", "description": "（可选）备注"},
        "fields": {"type": "object",
                   "description": "（仅 update）要改的字段字典，如 {\"note\": \"已补生长速率\"}"},
        "placeholder": {"type": "boolean",
                        "description": "（strain，可选）这条株号是不是**占位**（你还没给真实株号时用 "
                                       "S01/S02 这类临时名）。标了之后汇报页会加 `*` 并注明，"
                                       "免得把临时编号当成真株号念给导师听"},
        "seq": {"type": "integer",
                "description": "（仅 retract）要作废第几条事件（#1 起，按追加顺序）。"
                               "不知道序号就先给 target，报错里会把候选的 #seq 列出来"},
        "target": {"type": "object",
                   "description": "（仅 retract）按字段匹配要作废的那条，如 "
                                  "{\"kind\":\"observe\",\"code\":\"S01\"}。"
                                  "**必须恰好匹配一条** —— 匹配到多条会报错并列出来，"
                                  "因为作废不能「顺手」批量"},
        "reason": {"type": "string",
                   "description": "（retract 必填）为什么作废，如「脚本重复写入，同一条记了 5 遍」"},
    },
    "required": ["kind"],
}

LAB_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
}

LAB_METHOD_SCHEMA = {
    "type": "object",
    "properties": {
        "need": {"type": "string",
                 "description": "要哪张方法卡。三选一（也可以直接传用户原话，例如「核质染色怎么做」）："
                                "staining = 核质染色 / DAPI / 荧光 / 核相观察的参数与失败分诊；"
                                "identity = 怎么证明这几株是融合子（身份证据链，按成本排）；"
                                "quant = 该测哪些定量指标、单位、n、参照值（「没数据」缺哪个数字）"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
    "required": ["need"],
}


def _make_topic_tools(root: "Path | str | None" = None) -> dict:
    """返回 10 个课题工具的执行体。`root` 显式传入 ⇒ 单测可指向临时目录，不碰真账。

    ★ **失败一律 raise，不返回 ok=false 的 JSON。**
      理由本仓记过：注册表把「返回字符串」记成**成功调用**（ok=True），
      于是 payload 里那个 ok=false 会滑过证据门 —— `_load_skill` 当初就是为此改成抛错的。
      抛错会走 `[工具执行失败]` 分支回灌给模型，可读性一点不损失。
    """
    from . import topic as T

    root = T.topics_root(root)

    def _cur(slug: str) -> str:
        s = (slug or "").strip() or T.current(root=root)
        if not s:
            raise RuntimeError(
                "还没有当前课题（topics/CURRENT 是空的）。"
                "先用 topic_use(slug=…, title=…, question=…) 建一个，或显式传 slug。")
        return s

    def topic_status(slug: str = "", doc: str = "", offset: int = 0,
                     limit: int = 0, status: str = "") -> str:
        # ★ W28：折叠逻辑搬到 `topic.status_view()` —— 模型工具与 Web 面板
        #   现在读的是**同一份状态**。此前这套"过滤 / 截断 / 计数"长在这个闭包里，
        #   面板要同一件事的另一半（文档正文、按状态筛）就只能再实现一遍；
        #   两份实现对"还有几条没列出来"迟早会各说一套（H 型）。
        #   这里只留**塑形**（把台账原始键映射成工具契约里的键名）。
        s = (slug or "").strip() or T.current(root=root)
        # `offset`/`limit` 是**一个窗口**,落在"这一趟最想要的东西"上：
        # 传了 `doc` 就是文档正文的字符窗口，否则是卡点分页。
        # 不让它同时管两件事 —— 一个参数一处语义，否则调用方永远猜不准。
        want_doc = bool((doc or "").strip())
        off = max(0, int(offset or 0))
        lim = max(0, int(limit or 0))
        try:
            st = T.status_view(
                s, root=root, status=status,
                offset=0 if want_doc else off,
                limit=(lim or T.BLOCKER_PAGE) if not want_doc else T.BLOCKER_PAGE,
                doc=doc, doc_offset=off,
                doc_limit=(lim or T.DOC_WINDOW) if want_doc else T.DOC_WINDOW)
        except ValueError as e:
            raise RuntimeError(str(e)) from None
        if not st["slug"]:
            return json.dumps(st, ensure_ascii=False)
        st["blockers"] = [{"id": b["id"], "status": b["status"], "at": b["opened_at"],
                           "what": b["what"], "tried": b["tried"],
                           "resolved_how": b["resolved_how"]} for b in st["blockers"]]
        st.pop("items", None)     # 工具契约里历史上只有 `blockers`；面板用 `blocker_page`
        return json.dumps(st, ensure_ascii=False)

    def topic_use(slug: str, title: str = "", question: str = "") -> str:
        s = (slug or "").strip()
        if not s:
            raise RuntimeError("slug 为空")
        if not T.valid_slug(s):
            raise RuntimeError(f"非法课题名: {s!r}（只收字母/数字/下划线/短横/点/中文，≤48 字）")
        existed = T.read_topic(s, root=root) is not None
        m = T.ensure_topic(s, title=title, question=question, root=root)
        T.set_current(s, root=root)
        return json.dumps({"ok": True, "slug": s, "created": not existed,
                           "title": m.get("title"), "question": m.get("question"),
                           "current": T.current(root=root),
                           "note": "已切到本课题。它的未闭环卡点会出现在每个新任务的第一条消息里。"},
                          ensure_ascii=False)

    def raise_blocker(what: str, tried=None, where: str = "", slug: str = "") -> str:
        s = _cur(slug)
        where = (where or "").strip()
        r = T.add_blocker(s, what, tried or [], where=where, root=root)
        return json.dumps(
            {"ok": r["ok"], "verified": r["verified"], "id": r["id"], "slug": s,
             "open_total": r["open_total"],
             "note": "已记进卡点台账。**这是记账、不是收工** —— 继续推进；"
                     "确实做不成才用 report_blocker。"},
            ensure_ascii=False)

    def resolve_blocker(blocker_id: str, how: str = "", slug: str = "") -> str:
        s = _cur(slug)
        r = T.resolve_blocker(s, blocker_id, how=how, root=root)
        return json.dumps({"ok": r["ok"], "verified": r["verified"], "id": r["id"],
                           "slug": s, "already": bool(r.get("already")),
                           "open_total": r["open_total"]}, ensure_ascii=False)

    def topic_write(doc: str, text: str, mode: str = "append", slug: str = "") -> str:
        # ★ W27 修正：**没传 slug 时不再静默兜到 current()**。
        #   实测事故（W27 第二轮真实运行）：CURRENT 还停在上一轮的
        #   `cordyceps-cultivation`（主题是「冬虫夏草人工培育难点」），
        #   而本轮课题是原生质体融合选育 —— 模型连调 6 次 `topic_write`
        #   全部不传 slug，于是**一整轮的原生质体融合证据表被写进了另一个课题的
        #   EVIDENCE.md**，而每次回包都是 `ok:true, slug:cordyceps-cultivation`，
        #   看着完全正常。它是本次运行里唯一一个**静默毁数据**的缺陷。
        #
        #   为什么不能只靠"模型该自己传 slug"：`current()` 的语义是
        #   「**上次**用的课题」，跨会话残留；而"这轮在写哪个课题"是**本轮**的
        #   事实。拿跨会话的残留当本轮的默认值，就是在允许它们悄悄不一致。
        #
        #   改法：写台账是**改盘**操作，意图必须**明示**。没传 slug 就报错，
        #   并把当前课题与全部课题列出来（模型据此自己决定传哪个）。
        #   `topic_status` / `kb_*` 这些**读**操作不受影响 —— 读错地方代价小，
        #   写错地方代价是数据被搅在一起。
        s = (slug or "").strip()
        if not s:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(
                "topic_write 必须显式传 slug —— 台账是改盘操作，"
                f"不许默默写进「上次用过的课题」。当前 current()={T.current(root=root)!r}；"
                f"已知课题：{known}。"
                "如果本轮课题不在其中，先 topic_use(slug=…, title=…, question=…) 建/切过去。")
        if T.read_topic(s, root=root) is None:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(f"课题不存在: {s!r}。已知课题：{known}。")
        r = T.write_doc(s, doc, text, mode=mode, root=root)
        return json.dumps({"ok": r["ok"], "verified": r["verified"],
                           "doc": r["doc"], "chars": r["chars"], "slug": s},
                          ensure_ascii=False)

    def topic_link(slug: str = "", doc_id: str = "", linked: bool = True) -> str:
        # ★ 与 `topic_write` 同一条纪律：**改盘操作必须显式传 slug**。
        #   理由不是形式主义：`current()` 是「**上次**用的课题」，跨会话残留；
        #   「这篇文献属于这轮哪个课题」是本轮的事实。拿残留当默认值，
        #   就是允许两者悄悄不一致 —— W27 那次一整轮证据表写进另一个课题，
        #   根因正是这个。关联比写台账轻，但错挂的代价同样要事后人工翻查。
        s = (slug or "").strip()
        if not s:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(
                "topic_link 必须显式传 slug —— 关联是改盘操作，"
                f"不许默默挂到「上次用过的课题」。当前 current()={T.current(root=root)!r}；"
                f"已知课题：{known}。")
        try:
            r = T.set_library(s, doc_id, bool(linked), root=root)
        except KeyError as e:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(f"{e}。已知课题：{known}。") from None
        # ★ 只回 `count` 不回全量 id 列表：某个课题挂着 172 篇时，全量回包会
        #   把一次"挂一篇"变成几 KB 噪音，而模型真正要确认的只有"现在一共几篇"。
        #   要看全量有 `topic_status`（它的 `library` 是完整列表）。
        return json.dumps({"ok": r["ok"], "slug": s, "doc_id": r["doc_id"],
                           "linked": r["linked"], "count": len(r["library"])},
                          ensure_ascii=False)

    def topic_update(slug: str = "", title=None, question=None,
                     stage=None, next_step=None) -> str:
        """改课题元数据（名称/研究问题/阶段/下一步）。

        ★ 为什么要这一层包装，而不是直接把 `T.update_topic` 给模型：
          它的 `title` / `question` 是**必填位置参数**，内部无条件
          `old.update(title=title.strip(), question=question.strip())`。
          于是"我只想把阶段改成 evidence"这句，模型按 schema 传空串，
          就会把**课题标题和研究问题一起清空** —— 又是一次静默毁数据
          （与 W27 那次写错课题同型：回包 ok、内容没了）。
          所以这里的口径是：**空 = 没打算改，不是清空**。

        ★ `next_step` 是例外，故意允许清空：它是"用户记下的下一步"，
          「这一步做完了，清掉」是真实意图。传 `""` = 清空；不传（None）= 不改。
        """
        s = (slug or "").strip()
        if not s:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(
                "topic_update 必须显式传 slug —— 改盘操作不许靠「上次用过的课题」。"
                f"当前 current()={T.current(root=root)!r}；已知课题：{known}。")
        cur = T.read_topic(s, root=root)
        if cur is None:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(f"课题不存在: {s!r}。已知课题：{known}。")

        # 空串一律按"不改"处理；只有 next_step 的显式 "" 表示清空。
        nt = (title or "").strip() or (cur.get("title") or "")
        nq = (question or "").strip() or (cur.get("question") or "")
        st = (stage or "").strip() or None
        if next_step is None:
            nx = None
        else:
            nx = str(next_step).strip()      # "" ⇒ 清空，属显式意图
        if not any([(title or "").strip(), (question or "").strip(), st,
                    next_step is not None]):
            raise RuntimeError("什么都没给：title / question / stage / next_step "
                              "至少给一个要改的，否则这一趟是空转。")
        try:
            out = T.update_topic(s, nt, nq, root=root, stage=st, next_step=nx)
        except ValueError as e:
            raise RuntimeError(str(e)) from None
        return json.dumps({"ok": True, "slug": s,
                           "title": out.get("title"), "question": out.get("question"),
                           "stage": out.get("stage"), "next_step": out.get("next_step")},
                          ensure_ascii=False)

    def lab_record(kind: str, slug: str = "", code: str = "", id: str = "",
                   metric: str = "", value=None, unit: str = "", n=None, sd=None,
                   species: str = "", role: str = "", method: str = "", batch: str = "",
                   started: str = "", expect_days=None, vessel: str = "",
                   medium: str = "", rpm=None,
                   temp_c=None, growth: str = "", text: str = "", evidence: str = "",
                   what: str = "", due: str = "", how: str = "", note: str = "",
                   fields=None, placeholder=None, seq=None, target=None,
                   reason: str = "") -> str:
        """记一条实验事实。**这是"下一步"唯一的依据来源**。

        ★ 为什么 observe 与 measure 是两个 kind，而不是一个 kind 加布尔位：
          「老师说我没数据」这句话必须在代码里有一个形态。那个形态就是
          `len(measures)==0 and len(observes)>0` —— 它要求定性观察
          **写不进**定量字段。合成一个 kind，这个式子永远为假，
          工具就会一直安慰"你已经有数据了"。
          所以 `measure.value` 非数字一律**抛错**，而不是存成字符串。

        ★ 为什么失败抛错而不是回 ok:false：本仓老债 —— 注册表把"返回字符串"
          记成成功调用，payload 里的 ok:false 会滑过证据门。
        """
        from . import lab as L
        s = _cur(slug)
        kw = dict(code=code, species=species, role=role, id=id, metric=metric,
                  value=value, unit=unit, n=n, sd=sd, method=method, batch=batch,
                  started=started, expect_days=expect_days, vessel=vessel,
                  medium=medium, rpm=rpm,
                  temp_c=temp_c, growth=growth, text=text, evidence=evidence,
                  what=what, due=due, how=how, note=note, reason=reason)
        if fields:
            kw["fields"] = fields
        if target:
            kw["target"] = target
        if seq is not None:
            kw["target_seq"] = seq
        if placeholder is not None:
            kw["placeholder"] = bool(placeholder)
        try:
            r = L.record(s, kind, root=root, **{k: v for k, v in kw.items() if v not in (None, "")})
        except (ValueError, KeyError) as e:
            # 把"现在该怎么做"一起说出来 —— 只说"未注册的指标"模型会开始瞎猜。
            raise RuntimeError(f"{e}") from None
        out = {"ok": r["ok"], "verified": r["verified"], "slug": s, "kind": r["kind"],
               "total_events": r["total_events"], "event": r["event"],
               # 作废之后"还剩几条有效"是模型判断后续动作的直接依据 ——
               # 只回原始条数会让它以为账里凭空多了两条（作废记录 + 被作废那条）。
               "live_events": r.get("live_events", r["total_events"]),
               "retracted": r.get("retracted", 0)}
        # 记完立刻回一份**处境摘要**：不这样的话，模型要再调一次 lab_report
        # 才知道自己刚改了什么（多一次往返，且中间那步容易忘）。
        try:
            out["digest"] = L.digest(s, root=root)
            out["gaps"] = len(L.gaps(s, root=root))
        except Exception:                                  # noqa: BLE001
            pass
        return json.dumps(out, ensure_ascii=False)

    def lab_report(slug: str = "") -> str:
        """出「组会一页」：`LAB.md`（菌株×指标矩阵 + 缺口 + 下一步 + 方法出处）
        与 `LAB.csv`（tidy 长表，含定性行）。

        两个文件**都写进课题目录**（持久），同时把正文 `md_text` / `csv_text`
        一并返回 —— 这样模型可以再 `write_file("实验台账-<日期>.md", md_text)`
        落到本次 run 的工作区，用户在面板里就能直接看见、下载带去组会。
        为什么要两份：课题目录里那份是**台账的视图**（随时可重生成），
        run 工作区那份是**这次交付的凭据**（不会因为台账更新而变）。
        """
        from . import lab as L
        s = _cur(slug)
        try:
            r = L.write_report(s, root=root)
        except (ValueError, KeyError) as e:
            raise RuntimeError(f"{e}") from None
        acts = L.next_actions(s, limit=5, root=root)
        return json.dumps({
            "ok": r["ok"], "verified": r["verified"], "slug": s,
            "say": r.get("say", ""),
            "md_file": r["md_file"], "csv_file": r["csv_file"], "dir": r["dir"],
            "md_chars": r["md_chars"], "csv_rows": r["csv_rows"],
            "next_actions": acts,
            "md_text": r["md_text"], "csv_text": r["csv_text"],
            "hint": "★ `say` 是**直接给人看的那一段**，贴进回答里（≤3 条动作），"
                    "不要只回一句「已生成 LAB.md」。"
                    "要放进本次交付：write_file(name='实验台账.md', content=md_text)；"
                    "要出图：write_file(name='lab.csv', content=csv_text) 后调 "
                    "plot_data(table='lab.csv')。"
                    "**CSV 里的定性行（evidence_type=qual）不要删**，"
                    "删了图上就看不见那几株。",
        }, ensure_ascii=False)

    def lab_method(need: str, slug: str = "") -> str:
        """出**方法卡**：染色参数 / 身份证据链 / 定量指标标尺。

        ★ 它存在的理由：这三类知识本来就在 `lab_assets` 里，每条都挂着
          `doc_id` + 页码 —— 但**没有任何工具读它，模型永远看不见**。
          资产不进工具面，等于没写（本仓那条定律的第二次复发）。
          所以这个工具只做一件事：**把资产确定性渲染成模型能直接用、能直接念给用户的文本**。
          参数不许模型自己补；它只负责解释、排序、对着台账说「你先做这一步」。

        ★ 为什么把「染色」和「身份证据」「定量指标」放同一个工具：
          三者回答的是同一个问题的三个层次 ——
          「我下一步干什么」（台账排出动作）→「怎么证明这几株是融合子」（身份链）
          →「缺哪个数字」（标尺）→「具体那个方法怎么做」（染色参数）。
          分成三个工具会让模型在「我该调哪个」上多花一次判断，而它们共用同一份资产。
        """
        from . import lab as L
        s = _cur(slug)
        try:
            r = L.write_method_card(s, need, root=root)
        except ValueError as e:
            # 映射不到就说清可用说法 —— 静默给一张不相干的卡比报错坏得多。
            raise RuntimeError(f"{e}") from None
        return json.dumps({
            "ok": r["ok"], "verified": r["verified"], "slug": s,
            "need": r["need"], "also_matched": r["also_matched"],
            "title": r["title"], "file": r["file"], "dir": r["dir"],
            "md_chars": r["md_chars"], "md_text": r["md"],
            "say": r.get("say", ""),
            "hint": "★ `say` 是**直接念给用户的那一段**（3-5 行动作），原样贴进回答，"
                    "不要改写成表格，也不要只回一句「已生成方法卡」。"
                    "卡是资产渲染的，参数照抄、别改写数字。"
                    "用户想留存：write_file(name='方法卡.md', content=md_text)。"
                    "卡里没有的，去 `kb_search` / `kb_ask` 查原文，不要凭空补。",
        }, ensure_ascii=False)

    return {"topic_status": topic_status, "topic_use": topic_use,
            "raise_blocker": raise_blocker, "resolve_blocker": resolve_blocker,
            "topic_write": topic_write, "topic_link": topic_link,
            "topic_update": topic_update,
            "lab_record": lab_record, "lab_report": lab_report,
            "lab_method": lab_method}


_MOUNT_ALWAYS: tuple[str, ...] = ("core", "research", "figure")
_DECK_DOMAIN = "ppt"
# ★ 导师工具组（W25，2026-10-06）：kb（workbench 的 kb_* 文献库）与
#   topic（课题台账五件套）两个域。**一起进退**（设计文档 §2.4 的原话：
#   「G2/G3 没有 G1 就是空壳；G1 没有 G2/G3 就是一次性检索」），
#   由 `mounting_decision` 按 `contract.needs_kb` / 导师开关 / 文献类阶段挂载。
_KB_DOMAIN = "kb"
_TOPIC_DOMAIN = "topic"
# 文献类阶段：契约的阶段清单沾上任何一个 ⇒ 挂 kb/topic 域
# （照 deck 的「阶段含 slides ⇒ 挂 ppt 域」超集先例）。
_KB_STAGES = frozenset({"plan", "search", "fulltext", "read", "translate",
                        "cite", "verify", "topic", "proposal"})


def mounting_decision(contract: "TaskContract | None", names: list[str] | None = None,
                      domain_of=None, kb_only: bool = False,
                      has_topic: bool = False) -> dict:
    """算出"本任务给模型看哪些工具"，并**带上理由**。

    返回 `{"allow": [...] | None, "domains": [...], "reason": str, "saved": int}`。
    `allow=None` 表示不裁（全给）—— 只在拿不到领域信息时这样。

    理由必须返回：一个悄悄发生的裁剪会让人对着"工具明明注册了却调不到"发懵
    （这就是本项目反复记的"静默失败"）。

    ★ `kb_only`（导师模式开关，W25 起进挂载判据）：开着 ⇒ **强制**挂 kb/topic
      两个域。这是设计文档 §3.2 的安全不变量「needs_kb ⇒ kb_* 必在 allow 里」
      的机器形态 —— 开关承诺了"基于你的文献库回答"，挂载却不给库工具，
      那个开关就是装饰。它与 `mentor_mode_text` 里点名的工具名**单源**：
      那段提示词提到 `kb_search`/`kb_ask`，所以这里必须保证它们在场。
    """
    want = set(_MOUNT_ALWAYS)
    reason = "core/research/figure 常驻（与加 PPT 之前一致）"
    need_deck = bool(getattr(contract, "needs_deck", False))
    stages = list(getattr(contract, "stages", None) or [])
    if need_deck:
        want.add(_DECK_DOMAIN)
        reason += "；契约判定要产出 deck ⇒ 挂 ppt 域"
    elif "slides" in stages:
        want.add(_DECK_DOMAIN)
        reason += "；阶段含 slides（汇报 PPT）⇒ 挂 ppt 域"
    else:
        reason += "；本任务不像要做 deck ⇒ **不挂 ppt 域**"

    # ── 导师工具组（kb + topic 一起进退）───────────────────────
    # 四条挂载判据，任一命中即挂：
    #   ①导师 **会话带着课题身份**（`has_topic`，2026-11-02 新增）——
    #     这一条的由来是真实的不一致：`initial_messages` 在开场就把
    #     「你在课题 X 上」注入了提示词（`topic_slug` 为空时退回 `current()`），
    #     而挂载判据此前**只看契约与阶段**。于是普通对话里发生的是：
    #     **提示词说你有课题，工具却不给管课题的手** ——
    #     模型只能答「我改不了课题 / 关联不了文献」，而能力全在。
    #     判据改成「提示词注入的那份身份，工具也要给」—— 两处读同一个来源。
    #   ② 导师开关 kb_only（硬：开关承诺了库，不给库工具 = 假开关）；
    #   ③ 契约字段 needs_kb（硬：与 needs_deck ⇒ ppt 同一条不变量）；
    #   ④ 阶段清单沾上文献类阶段（超集，照 deck 的「slides ⇒ ppt」先例）——
    #     阶段是目标文本推断出来的，检索/精读/核验/开题天然要碰库。
    # kb 与 topic **不分家**：课题台账没有库就是空壳，库没有台账就是一次性检索
    # （设计文档 §2.4）。挂一个 = 挂两个。
    need_kb = (bool(kb_only)
               or bool(has_topic)
               or bool(getattr(contract, "needs_kb", False))
               or bool(set(stages) & _KB_STAGES))
    if need_kb:
        want.add(_KB_DOMAIN)
        want.add(_TOPIC_DOMAIN)
        why = ("导师模式开着" if kb_only else
               "本场会话带着课题身份" if has_topic else
               "契约判定要用文献库/课题（needs_kb）" if getattr(contract, "needs_kb", False) else
               "阶段含文献类工作")
        reason += f"；{why} ⇒ 挂 kb/topic 域"
    else:
        reason += "；本任务不沾文献库/课题 ⇒ **不挂 kb/topic 域**"

    if names is None or domain_of is None:
        return {"allow": None, "domains": sorted(want), "reason": reason}

    # 把领域展开成名字。**量不出来就不裁**：若某个想保留的领域名一个工具都
    # 对不上，说明领域命名变了或注册失败 —— 此时按名单裁等于静默地把工具拿走。
    got: set[str] = set()
    hit: set[str] = set()
    for n in names:
        d = domain_of(n)
        if d in want:
            got.add(n)
            hit.add(d)
    missing = want - hit
    if missing:
        return {"allow": None, "domains": sorted(want),
                "reason": reason + f"；⚠️ 领域 {sorted(missing)} 一个工具都没匹配到 ⇒ "
                                   f"退回不裁（宁可多给，也别静默拿走工具）"}
    return {"allow": sorted(got), "domains": sorted(want), "reason": reason}


class LicenseError(RuntimeError):
    """授权不允许发起新任务。

    ★ 它不是"任务失败"，是"任务**没开始**"。刻意做成独立异常类型，
      让上层（CLI / Web）能把它和真的运行失败分开呈现 —— 混在
      `RunResult(success=False)` 里会让遥测、账单、归档都记上一笔
      根本没发生的运行。

    继承 `RuntimeError` 而不是 `Exception`：这是运行时环境问题，不是
    调用方写错了参数，不该和 `ValueError`/`TypeError` 混作一类被
    `except Exception` 顺手吞掉。
    """


@dataclass
class RunResult:
    success: bool
    reason: str
    steps: int
    usage: Usage
    trace: list[ToolCall] = field(default_factory=list)
    final: str = ""
    contract: TaskContract | None = None
    verdict: Verdict | None = None
    workspace: str = ""
    run_id: str = ""                 # 工作区名，也是归档 id（可直接用来回放）
    draft: str = ""                  # 写作任务交付的正文全文（给界面展示）
    lint: dict | None = None         # 写作体检摘要
    # 成本：float（价格表命中）或字面 "UNKNOWN"（未命中）。
    # **不给 0 当默认值** —— 0 会把"没算出来"渲染成"免费的"。
    cost: object = _pricing.UNKNOWN
    telemetry: dict | None = None    # span 时间账摘要（见 telemetry.timing_report）
    # 最后一次上下文出清的**事实**（级别 / 省了多少字符 / 第几步 / 为什么）。
    # 为什么要把事件里已有的东西再放进结果：归档的 `events` 有 600 条上限
    # （`MAX_EVENTS`），一场长任务的出清事件可能被挤出；而 `ctx_econ.py` 的跨源核对
    # 需要第二个来源，否则"emit 了但没真出清"与"根本没 emit"在归档里长得一模一样。
    compacted: dict | None = None
    # 用量是不是全量。`False` ⇒ `usage` 是**中断前的下界**，不是总账
    # （在途那次调用的 usage 网关没来得及回）。
    # 为什么要显式带这个布尔：光看 `input_tokens: 0` 分不出"真没花"与"没测到"，
    # 而下游一律会把 0 读成"没花钱"。`pricing` 用字面 `UNKNOWN` 治同一个病，
    # 这边用量是结构体、没法塞字符串，所以用一个字段声明可置信度。
    usage_complete: bool = True
    #: 这一轮的记账结果（`_charge_billing` 的自述）。
    #: `None` = 还没记（如构造出来但没 `run()`）；`{"ok": False, ...}` = 记账失败，
    #: 但**任务本身可能已经成功** —— 这两件事必须分开看。
    billing: dict | None = None
    #: 收尾的**出处复核事实**（`mentor_trace_audit` 的输出，只挂事实不下判决）。
    #: `None` = 没跑（非导师模式）；`{"error": …}` = 跑了但异常。
    #: 为什么要放进结果：这套判据能分辨真假（PASS / doc_not_in_hits / page_not_hit），
    #: 但只在 run 结尾算一次、只进内存的话，界面和归档都拿不到 —— 那就等于没算。
    trace_audit: dict | None = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "reason": self.reason,
            "steps": self.steps,
            "usage": self.usage.to_dict(),
            "usage_complete": self.usage_complete,
            "cache_ratio": self.usage.cache_ratio,
            "final": self.final,
            "workspace": self.workspace,
            "run_id": self.run_id,
            "draft": self.draft,
            "lint": self.lint,
            "cost_usd": self.cost,
            "cost_display": _pricing.format_cost(self.cost),
            "billing": self.billing,
            "telemetry": self.telemetry,
            "compacted": self.compacted,
            "trace_audit": self.trace_audit,
            "contract": self.contract.to_dict() if self.contract else None,
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "trace": [c.to_dict() for c in self.trace],
        }


class Rongke:
    """荣珂内核。一次实例化，可跑多个任务（每个任务一个独立工作区）。"""

    def __init__(self, cfg: Config | None = None, install_domain: bool = True,
                 use_memory: bool = True, install_mcp_servers: bool = True,
                 install_plugins: bool = True, billing_root=None,
                 license_root=None, preset: str = ""):
        self.cfg = cfg or Config.load()
        # 账单落点的显式覆盖。`None` = 走 `<数据根>/billing`（正常情况）。
        # 为什么留这个口子：测试要能记账**但不碰用户真台账**，
        # 而"把整个数据根改道"会连带改掉工作区根，测试现场就分家了。
        self.billing_root = billing_root
        # 授权落点，同上。测试要能**造一个过期授权**而不碰用户真凭证。
        self.license_root = license_root
        self.llm = LLM(self.cfg.provider, timeout=self.cfg.loop.request_timeout,
                       default_temperature=self.cfg.loop.temperature)
        self.registry = ToolRegistry()
        self._installed: list[str] = []
        if install_domain:
            self._installed = install_workbench(self.registry, self.cfg.runtime.workbench,
                                                kb_domain=_KB_DOMAIN)
            # 如实留下 workbench 为什么没挂上（冻结版缺 mcp/fitz 时就是这里）。
            # 不把它翻成"库里没有"——那是两件事。
            self.workbench_error = getattr(install_workbench, "last_error", "")
        self._install_builtins()

        # ── MCP：荣珂作为**客户端**去连外部 MCP 服务端 ──────────
        # 和上面的 bridge 反射是两条不同的路：
        #   bridge  —— 别人（workbench）的工具，同进程直调，方向朝内
        #   MCP     —— 外部服务端，子进程 + JSON-RPC，方向朝外
        # 后者才是"荣珂是客户端"。enabled 已在 Config.load 判过，这里不重复判。
        self.mcp = MCPHub(self.cfg.mcp.servers if install_mcp_servers else [])
        self.mcp_report: dict = {"installed": [], "skipped": [], "servers": {}}
        if self.mcp.specs:
            try:
                self.mcp_report = install_mcp(self.registry, self.mcp)
            except Exception as e:
                # 装配 MCP 失败不该让内核起不来，但必须**报到明面**
                self.mcp_report = {
                    "installed": [], "servers": {},
                    "skipped": [{"server": "", "reason":
                                 f"装配 MCP 整体失败：{type(e).__name__}: {e}"}],
                }

        # ── 插件：让别人不用改内核就能加工具与图型 ──────────────
        # ★ 位置必须在 CompletionGate **之前**：gate 构造时会抓一份工具清单
        #   用来判"该调的工具调了没有"，插件工具晚装上去它就不认。
        # ★ 加载失败绝不让内核起不来（插件是第三方代码，坏是常态），
        #   但也绝不静默跳过 —— 结果留在 `self.plugins_report` 上，
        #   由 `--plugins` 与界面报到明面。
        self.plugins_report: dict = {"scanned": 0, "ok": 0, "failed": 0, "items": []}
        if install_plugins:
            try:
                from .plugins import load_plugins
                rep = load_plugins(self.registry, cfg=self.cfg)
                self.plugins_report = {
                    "scanned": rep.scanned,
                    "ok": len(rep.ok),
                    "failed": len(rep.failed),
                    "summary": rep.summary(),
                    "items": [{
                        "path": r.path, "ok": r.ok, "name": r.name,
                        "version": r.version, "description": r.description,
                        "tools": r.tools, "chart_kinds": r.chart_kinds,
                        "skipped": r.skipped, "error": r.error, "hint": r.hint,
                    } for r in rep.results],
                }
            except Exception as e:                             # noqa: BLE001
                self.plugins_report = {
                    "scanned": 0, "ok": 0, "failed": 1, "items": [],
                    "summary": f"插件系统整体没跑成：{type(e).__name__}: {e}",
                }
            self._refresh_plot_schema()

        # ── 能力预设：把「这一场用哪套判据/门/工具」收成一份挂载结果 ──
        # ★ 位置在 load_plugins 之后、CompletionGate 之前：P2 的预设可以注册
        #   `writing_metrics` 工具，gate 构造时才能把它抓进工具清单。
        # ★ 装配失败绝不让内核起不来，但也绝不静默：`presets_report` 里带上
        #   理由，事件流与 `--presets` 台账都读得到。判健康只认 `broken`。
        from .caps import Caps, MountResult, load_capability_preset
        self._caps_ctx: dict = {"registry": self.registry}
        try:
            self.caps: Caps = load_capability_preset(
                str(preset or "").strip() or "baseline", ctx=self._caps_ctx)
        except Exception as e:                             # noqa: BLE001
            self.caps = Caps.empty()
            self.caps.report.results.append(
                MountResult("<装配>", "", False,
                            error=f"能力预设整体没跑成：{type(e).__name__}: {e}"))
        self._caps_ctx["caps"] = self.caps
        self._baseline_caps = self.caps
        self.presets_report: dict = self.caps.report.to_dict()

        self.gate = CompletionGate(self.registry, caps=self.caps)
        self.fence: WorkspaceFence | None = None
        self.telemetry: TraceRecorder | None = None
        self.memory = Memory(default_root(self.cfg)) if use_memory else None
        # MCP 开工前自检（只读；**不阻断启动**，见 mcp_doctor 的模块说明）。
        # 存在的理由是一个真踩过的坑：AnySearch 的 key 早就配好、工具也反射进来了，
        # 但装配期没人验过"这些工具到底能不能用"，于是我去手搓爬虫撞了一路 403。
        # 代价极低（纯静态检查，不起子进程），所以默认跑；失败也只是少一条提示。
        self.mcp_health: dict = {}
        if use_memory is not False:
            try:
                from .mcp_doctor import diagnose_mcp
                self.mcp_health = diagnose_mcp(self.cfg, install=False)
            except Exception as e:                             # noqa: BLE001
                self.mcp_health = {"verdict": "unknown", "summary_line":
                                   f"MCP 自检没跑成：{type(e).__name__}: {e}"}

    # ── 装配 ────────────────────────────────────────────────
    def _refresh_plot_schema(self) -> None:
        """把插件注册的图型补进 `plot_data` 的参数说明。

        为什么需要这一步：`PLOT_DATA_SCHEMA` 是模块级常量，在 import 时
        就写死了内置图型；而插件在 `__init__` 里才加载。不刷新的话，
        插件图型对模型**不可见** —— 它能注册、能画、能被 suggest 推荐，
        但模型读到的"可用图型"清单里没有它，于是永远只会填内置那几种。

        ★ 只改 `kind` 那一条描述，别重建整个 schema —— 重建会把别人
          （工作台的、MCP 的）对它的改动抹掉。
        """
        t = self.registry.get("plot_data")
        if t is None:
            return
        try:
            kinds = _plot_kind_list()
            props = (t.parameters or {}).get("properties") or {}
            kd = props.get("kind")
            if not isinstance(kd, dict):
                return
            kd["description"] = (
                "图型。**留空是最常见也最正确的选择** —— 模块会按列的形状"
                "自动选，并把「为什么选它、为什么不选别的」写进 suggestions。"
                "只有你已经确定要画什么时才填。当前可用："
                f"{kinds}")
        except Exception:                                      # noqa: BLE001
            pass      # 刷新失败不该让内核起不来；模型至少还能看到内置那几种

    def _install_builtins(self) -> None:
        # 这两个不由 registry 执行，而是主循环特判（因为它们要触发门禁 / 退出）
        self.registry.register(Tool(
            name="deliver_task",
            description=("宣布任务完成。必须附上真实工具调用的证据（call_seq）。"
                         "证据门会核验，编造或证据不足会被打回并告知缺什么。"),
            parameters=DELIVER_SCHEMA,
            fn=lambda **kw: "（deliver_task 由主循环处理）",
            domain="core",
        ))
        self.registry.register(Tool(
            name="report_blocker",
            description=("诚实报告任务无法完成。当你确实推进不了时用它，"
                         "而不是假装完成。这是被鼓励的行为。"),
            parameters=BLOCKER_SCHEMA,
            fn=lambda **kw: "（report_blocker 由主循环处理）",
            domain="core",
        ))
        # ★ 读工作区里的文本文件。**这是内核一直缺的基元**（2026-09-21 实跑抓到）：
        #   在此之前 67 个工具里没有一个能读本地文本文件，于是材料是 `.md` 的
        #   任务**根本无法开工** —— 详见 `_make_read_file` 的 docstring。
        #   放 `core` ⇒ 任何任务都挂，因为"读输入"不是某个领域的能力。
        self.registry.register(Tool(
            name="read_file",
            description=("读**本任务工作区里的文本文件**（.md/.txt/.json/.csv…，UTF-8 或 GBK），"
                         "返回正文，超长会截断并如实说明。**找不到文件时会列出工作区里"
                         "真实有的名字**，直接照抄即可。注意：pdf/docx 用 `import_document`，"
                         "网页用 `web_extract` —— 本工具只读工作区内的纯文本。"),
            parameters=READ_FILE_SCHEMA,
            fn=_make_read_file(),
            domain="core",
        ))
        # ★ 写工作区里的文本文件。**`read_file` 的对偶基元**（2026-10-14 实跑抓到）：
        #   补了"读"之后，"写"一直悬空 —— 契约要求产出指定路径的文本文件时
        #   模型无路可走，只能如实报阻塞。详见 `_make_write_file` 的 docstring。
        #   标 `mutating=True`：注册表的工作区围栏再管一道（工具内已自判一次）。
        self.registry.register(Tool(
            name="write_file",
            description=("把文本写入**本任务工作区里的文件**（utf-8，自动建上级目录）。"
                         "**要产出任何 .md/.txt/.csv 正文文件，用这个。**"
                         "`append=true` 追加（长文分段写用这个，别一次吐几万字）；"
                         "默认整篇覆盖。**写完会回读核验**并回报字节与末段文字。"
                         "只能写工作区内；pdf/docx 用 `import_document`/`export_docx`。"),
            parameters=WRITE_FILE_SCHEMA,
            fn=_make_write_file(),
            domain="core",
            mutating=True,
        ))
        # ── 子代理（W20-B）────────────────────────────────────
        # 把一件事派给一个**隔离子智能体**：它在自己的工作区、自己的 registry /
        # gate / fence / telemetry 里干完，**只把结果交回来**。
        #
        # ★ 注册成**一个普通工具**，`_loop` 里没有为它加任何特判分支 ——
        #   特判才是风险所在：主循环一旦认识"子代理"这个概念，
        #   上下文隔离就要靠人记住（本项目反复付过这种债）。
        #
        # ★ 它**不是** mutating：子代理写的是**它自己的工作区**，
        #   父级工作区一个字节都不动。把它标成 mutating 会让围栏去检查
        #   `task` 这段自然语言 —— 而 `looks_like_path()` 对含 `/` 的中文句子
        #   返回 True，于是**正常的任务描述会被围栏当越界路径拒掉**。
        #   围栏管的是"参数里写的路径"，这里没有路径参数。
        #
        # `enabled=False` 时**不注册**（而不是注册了再拒绝）：
        # 看得见却调不通的工具，模型会反复试、反复失败、把步数烧光。
        if getattr(getattr(self.cfg, "subagent", None), "enabled", True):
            self.subagents = SubagentRunner(self.cfg, host=self)
            self.registry.register(Tool(
                name="subagent",
                description=("把**一件边界清楚的独立任务**派给一个隔离子智能体去做。"
                             "它有自己的上下文与工具面，干完只交回结果"
                             "（最终交付文本 + 用了多少步 + 各工具调用次数）——"
                             "**你看不到它的中间过程，它也看不到你的**。"
                             "适合大范围检索 / 翻材料 / 读代码这类"
                             "**会产生大量原文**的活：派出去，父级上下文就不被撑爆。"
                             "写清楚要什么结论、什么范围、什么算做完。"
                             "它**不能**再派子代理（派发工具不在它的清单里）。"),
                parameters=SUBAGENT_SCHEMA,
                fn=self.subagents.delegate,
                domain="core",
            ))
        # 只读工具（网络 GET），不参与工作区围栏检查。
        # 存在理由：领域工具 fetch_doi 只**取元数据**，不判真伪也不查撤稿；
        # 而 lit-review 技能的 retract 指令需要这个能力才能落地。
        self.registry.register(Tool(
            name="verify_citation",
            description=("核验单条引文：① DOI 是否真实存在 ② 是否**已被撤稿** "
                         "③ 元数据是否与稿件里写的一致（标题/年份/作者）。"
                         "核验参考文献时逐条调用它，不要只看 fetch_doi 的元数据 —— "
                         "fetch_doi 不会告诉你一篇论文已经被撤了。"),
            parameters=VERIFY_SCHEMA,
            fn=verify_citation,
            domain="core",
        ))
        # 荣珂自带的技能装载（只读）。
        # 为什么不用工作台的 read_skill：它的技能根只有三处（~/.zcode/skills 的 8 个
        # 旧副本、~/.dsh/skills、引擎自带），**扫不到 ~/.agents/skills 的 21 个真库**。
        # 而「每个阶段一个专属技能」要求能读到那 21 个。
        self.registry.register(Tool(
            name="load_skill",
            description=("装载一个阶段专属技能的**入口文档**（只读，自动记进过程记录）。"
                         "契约的「阶段专用技能」清单里，每个阶段有且只有一个技能，"
                         "进入该阶段前必须先用本工具装载它，再按它的协议执行。"
                         "凭记忆模仿不算数。找不到会明确告诉你有哪些可用技能。\n"
                         "技能的入口文档里若让你「按需打开 references/ 下的文件」，"
                         "用本工具的 `file` 参数读它们（例如 "
                         "file='references/introduction.md'）—— 别自己拼路径猜文件名，"
                         "路径写错时会把该技能下**所有可用文件**列给你。"),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "技能名，如 nature-writing / nature-polishing"},
                    "file": {"type": "string",
                             "description": "（可选）技能目录内的相对路径，读它的参考文件；"
                                            "留空则读入口文档 SKILL.md"},
                },
                "required": ["name"],
            },
            fn=_load_skill,
            domain="core",
        ))
        # ★★ 课题台账（导师模式 P1/P2，2026-09-22；挂载形状 W25，2026-10-06）。
        #   存在理由：内核原来只有**一条时间尺度 —— 一次 run**。`report_blocker` 是
        #   **终局**动作（卡点被说出来一次就永远消失），`TaskContract` 跑完就归档。
        #   于是「导师」最核心的两句话**没有对象可查**：
        #     ·「上次到哪了？」 —— 课题不是一等公民
        #     ·「这个坑你两周前卡过。」 —— 没有台账，同一个坑换个说法会再踩一遍
        #   完整设计与三条纪律见 `rongke/topic.py` 模块头。
        #
        #   ⚠️ 挂载形状的演变：这 5 个原先**全挂 `core` 常驻**，理由是"按需裁剪需要
        #   一个硬判据，而当时量不出来 ⇒ 量不出来就不裁"。W25 把判据补上了 ——
        #   契约的 `needs_kb` 字段（照 `needs_deck` ⇒ ppt 域的同构先例）＋导师开关
        #   ＋文献类阶段（见 `mounting_decision`），于是照 `_DECK_DOMAIN` 的形状
        #   拆出 `_TOPIC_DOMAIN`，与 `_KB_DOMAIN` 一起按需挂。
        #   开关不变量（有断言钉着）：kb_only=True ⇒ `kb_search` 与 `topic_use`
        #   **必在 allow 里** —— 导师模式那段提示词点名了这两类工具，
        #   挂载不给就是假开关。
        #
        #   注：这 5 个的参数里**没有路径**，所以标 `mutating` 对 `WorkspaceFence`
        #   没有任何效果（围栏只查 mutating 工具的**绝对路径参数**）。越界防护由
        #   `topic._topic_dir()` 自己负责（非法 slug 与 `..` 一律返回 None，不抛异常）。
        # ★★ PDF → Markdown 转换层（2026-09-25）。
        #   补的是**知识库的入库前那一环**：litkb 自己抽的是纯文本，
        #   丢掉了标题层级/表格/阅读顺序；而"方便 AI 读"要有产物可查。
        #   详见 `rongke/kb_convert.py` 模块头（含实测数字）。
        _kbcfg = getattr(self.cfg, "kb", None)
        if _kbcfg is None or getattr(_kbcfg, "enabled", True):
            _KB_CFG["cfg"] = self.cfg
            # `kb_coverage` 要**同一进程内直调** kb_search（它是 workbench 反射进来的
            # 工具，不是内核函数）。把 registry 交出去，让它能按名字取。
            # ★ 不给就走不通 —— 而"判不了够不够"正是这一轮要修的第一件事。
            _KB_CFG["registry"] = self.registry
            # ── 库够用性判定（2026-10-22，用户流程的第一条）───────────────
            # 为什么必须有它：在此之前整个内核**没有**这个判定（全仓 grep
            # 够用/够不够/suffic/coverage 在文献语料上是空集）。于是模型接到
            # 「写一篇综述」→ 先检索 → 再入库 → 一路烧到 200 轮，
            # 才发现某几章其实没有文献支撑。判在动笔之前，返工才不会被触发。
            #
            # 挂 `core` 的理由与 `convert_pdf` 同源：**量不出一个硬判据来裁它**。
            # 「这次要不要判够不够」从契约里看不出来 —— 看起来所有要引文献的任务都该判。
            self.registry.register(Tool(
                name="kb_coverage",
                description=(
                    "**判「库里的文献够不够写这个题目」** —— 综述/开题/长文动笔前的第一件事。\n"
                    "给一组主题词（建议 5–8 个，对应大纲各章或核心论点），它对每个词做段落级"
                    "检索，数出**命中了多少篇不同文献**，然后下判语：\n"
                    "  · `need_more_literature=false` ⇒ **库够用，不要再去全量检索入库**，"
                    "直接进入大纲阶段；缺哪一篇只补那一篇。\n"
                    "  · `need_more_literature=true` ⇒ 回包里 `gaps` 点名了缺哪几章，"
                    "只针对它们补（literature_search → literature_fetch → kb_add）。\n"
                    "判据是机械的（默认每个主题词 ≥3 篇不同文献、达标占比 ≥60%，"
                    "可用 min_docs / min_ok_ratio 调），不是感觉。\n"
                    "⚠️ **中文主题词请给三字以上**：两字词会被库的 trigram 索引"
                    "静默回空，本工具会把它们单列在 `dropped_short_terms` 里 ——"
                    "那既不算「有」也不算「没有」，别据此判定库里缺。\n"
                    "⚠️ `distinct_docs_seen` 是**下界**（每个词只取前若干段），"
                    "它回答「至少能引几篇」，不是「库里一共几篇」。"),
                parameters=KB_COVERAGE_SCHEMA,
                fn=_make_kb_coverage(),
                domain="core",
            ))
            # ── 取全文（2026-10-22）──────────────────────────────────
            # 这是「20% 全文率」那条坑的正面对策。详见 litfetch.py 模块头。
            # ★ 为什么它必须由内核自带：荣珂**没有 shell**（59 个工具里没有
            #   exec/bash/子进程类），所以"让模型去跑外部下载器脚本"这条路
            #   根本不存在；要开它就得给任意命令执行权，与围栏设计相反。
            self.registry.register(Tool(
                name="literature_fetch",
                description=(
                    "**按 DOI 取全文**（综述/长文备料的核心动作）。按三条路依次试：\n"
                    "  ① Europe PMC `fullTextXML` —— 官方 API，回的是 **JATS 全文 XML**，"
                    "稳定、无需 key，**多数开放获取的生物医学文献走这条**\n"
                    "  ② Unpaywall 判 OA → 出版方 PDF 直链\n"
                    "  ③ doi.org 内容协商\n"
                    "回包逐条给 `ok` / `route`（走的哪条路）/ `file`（落盘路径）/ `chars`。\n"
                    "取到的 `file` **直接喂给 kb_add / kb_ingest 入库**（本工具不写库）。\n"
                    "⚠️ 回 `ok=false` 说的是「**这三条路这次没成**」，"
                    "**不是「世上没有这篇」** —— 剩下的可能是机构订阅 / CNKI 中文库 / "
                    "出版商账号。要如实写进检索死区，不许据此删掉一个论点。\n"
                    "⚠️ 落盘只在工作区内，`out_dir` 只收相对路径。"),
                parameters=LITERATURE_FETCH_SCHEMA,
                fn=_make_literature_fetch(self.cfg),
                domain="core",
                mutating=True,     # 往工作区写 pdf/xml → 交给围栏管
            ))
            _kfn = _make_kb_convert_tools()
            self.registry.register(Tool(
                name="convert_pdf",
                description=(
                    "把一份 PDF 转成**结构化 Markdown**（标题层级 / 表格 / 阅读顺序保留），"
                    "落进工作区。**产物是给人读、给 Obsidian 精读用的** —— "
                    "kb 库只收 PDF 原文（入库走 `kb_add`/`kb_ingest`，"
                    "litkb 入库时自己会抽全文），别拿这份 md 去入库。\n"
                    "什么时候用它：用户给的是 PDF，而你要的是**能读的结构化文本**时"
                    "（做综述、精读、建库、抽证据）。它比 `import_document` 更适合"
                    "**学术论文**——后者是通用格式转换，不做版面结构还原。\n"
                    "★ 默认 `use_ocr=false`，**别乱开**：实测 21 页双栏论文关 OCR 24 秒、"
                    "开 OCR 53 秒（慢 2.2 倍），正文只多约 1.3%。数字文本的期刊件不要开；"
                    "只有扫描件才开。返回里会按每页字符数提示像不像扫描件。\n"
                    "⚠️ 返回 `verdict=\"SKIPPED\"` 表示**解释器里没装 pymupdf4llm，"
                    "这次根本没转**（产物不存在）—— 不要把它当成「这篇 PDF 是空的」。"),
                parameters=CONVERT_PDF_SCHEMA,
                fn=_kfn["convert_pdf"],
                domain="core",
                mutating=False,   # out 只许相对路径，且只写工作区 ⇒ 不动外部世界
            ))
            self.registry.register(Tool(
                name="kb_to_obsidian",
                description=(
                    "把一份 PDF 转成 Markdown **并写进 Obsidian 库**（带 YAML frontmatter："
                    "title/authors/year/journal/doi/tags）。\n"
                    "Obsidian 库就是一个**装了 .md 的目录** —— 本工具直接写文件，"
                    "**不依赖任何 Obsidian 插件**（那些插件在不在、还维不维护都不该决定"
                    "这条链能不能用）。\n"
                    "⚠️ 两条纪律：① 同名笔记**绝不覆盖** —— 内容不同就存成新名字，"
                    "原件一个字不动；② 它写的是 **Obsidian 的库**，"
                    "**不等于**进了荣珂的 kb 库（`kb_search`/`kb_ask` 查的是另一套索引）。"
                    "两个都要，就对同一份 PDF 再 `kb_ingest` 一次。"),
                parameters=KB_TO_OBSIDIAN_SCHEMA,
                fn=_kfn["kb_to_obsidian"],
                domain="core",
                mutating=True,    # 往用户 vault 写文件 ⇒ 按写类登记（保守）
            ))
        _tfn = _make_topic_tools()
        # ① 入口：立课题 / 切课题 —— 导师模式的总开关。
        self.registry.register(Tool(
            name="topic_use",
            description=(
                "开启或切换一个**课题**（长生命的科研任务）—— 导师模式的总开关。\n"
                "**要带着学生做科研之前先调它一次**：把「这次做的大方向」立成一个课题，"
                "之后每个新任务的第一条消息里都会自动带上这个课题的进度与未闭环卡点。\n"
                "已存在的课题**原样切过去，绝不覆盖**（title/question 只在新建时生效）。\n"
                "不立课题也能干活，但每次任务都是孤立的 —— 上次卡在哪、读过什么，下次没人记得。"),
            parameters=TOPIC_USE_SCHEMA,
            fn=_tfn["topic_use"],
            domain=_TOPIC_DOMAIN,
        ))
        # ② 看进度 + 卡点全貌。卡点 id 只能从这里拿。
        self.registry.register(Tool(
            name="topic_status",
            description=(
                "看**课题到哪了**：计划/读书/证据三份活文档的**有界索引**"
                "（有没有、多少字、截断没截断），外加卡点条目（每条都带「当时试过什么」）。\n"
                "**新任务开工前、或觉得在重复踩坑时调它**。留空 slug = 当前课题。\n"
                "要看某份台账的**正文**就带 `doc=plan|reading|evidence`（可用 offset/limit "
                "取一窗，默认 12000 字）；卡点可用 `status=open|resolved|all` 筛。\n"
                "卡点 id（B001 这类）**只能从这里拿** —— 不要猜 id，猜错的会被拒并列出真实 id。"),
            parameters=TOPIC_STATUS_SCHEMA,
            fn=_tfn["topic_status"],
            domain=_TOPIC_DOMAIN,
        ))
        # ③ 记卡点。**与 report_blocker 分工明确**：一个是记账，一个是终局。
        self.registry.register(Tool(
            name="raise_blocker",
            description=(
                "把推进中遇到的卡点**记账**：卡在哪、已经试过什么。\n"
                "**这是记账，不是收工** —— 记完继续推进；确实做不成才用 `report_blocker`（那是终局）。\n"
                "为什么必须记：不记的卡点会随着这次 run 一起消失，下次换个说法再踩一遍。\n"
                "⚠️ `tried` 是**下次最值钱的信息** —— 别写「做了很多尝试」这种等于没写的话。"),
            parameters=RAISE_BLOCKER_SCHEMA,
            fn=_tfn["raise_blocker"],
            domain=_TOPIC_DOMAIN,
        ))
        # ④ 闭环卡点。
        self.registry.register(Tool(
            name="resolve_blocker",
            description=(
                "把一个卡点**闭环**（写明是怎么解决的）。闭环后它不再出现在路径提醒里。\n"
                "`blocker_id` 从 `topic_status` 拿。同一个 id 重复闭环是**幂等**的，不会报错。\n"
                "⚠️ 别把没解决的标成已解决 —— 那是把台账变成废纸，比不记更糟。"),
            parameters=RESOLVE_BLOCKER_SCHEMA,
            fn=_tfn["resolve_blocker"],
            domain=_TOPIC_DOMAIN,
        ))
        # ⑤ 写课题活文档。**这是内核唯一能写任意文本的基元**（补另一条真缺口）。
        self.registry.register(Tool(
            name="topic_write",
            description=(
                "写课题的**活文档**：`plan`（计划与技术路线）/ `reading`（读过什么、从它拿到什么）/ "
                "`evidence`（哪条结论由哪篇文献支撑）。\n"
                "为什么必须有：内核此前**没有任何「写任意文本文件」的基元** —— "
                "`read_file` 只读、`import_document` 只转格式，于是「把这一路读到的攒成一份台账」"
                "这件事根本没有工具可做。\n"
                "`mode=append`（默认，追加）或 `replace`（整篇覆盖）。写完内核会**读回来核字符数**。"),
            parameters=TOPIC_WRITE_SCHEMA,
            fn=_tfn["topic_write"],
            domain=_TOPIC_DOMAIN,
            # ★ 2026-10-01 主控勘误：这里原先**漏了** `mutating=True`。
            #   它是**会写盘**的（`T.write_doc` 往课题活文档写正文；
            #   紧邻的 `audit_figure` 注册处就明写"会写盘…所以标 mutating 让围栏管"）。
            #   漏标的后果很具体：W20-B 里标称"只读"的 `explore` 子代理，
            #   靠 `<mutating>` 元规格过滤时**挡不住它** ——
            #   一个只该查文献的子代理于是拿到了写台账的能力（实测已复现）。
            #   本项目定律：能算的就不许靠匹配。`mutating` 是元数据，
            #   补它比在每份 profile 的名字清单里逐个列举可靠得多。
            mutating=True,
        ))
        # ⑥ 把文献挂到课题上。**这是在补一只原来根本不存在的手。**
        #   实测缺口（2026-11-02，用户原话：「我让他…寻找文献关联在这个课题下，
        #   他做不到」）：`set_library()` 早就存在，Web 面板的「添加到课题 /
        #   解除关联」按钮就在调它；但模型侧只注册了 5 个 topic 工具，
        #   **没有任何一个能把"搜到的这篇"挂到课题上**。
        #   ⇒ 模型读得到 library（topic_status）、搜得到全库（kb_search），
        #     唯独走不完最后一步。这是本仓「没给工具的动作，写多少纪律都空转」
        #     的正面命中：能力全在，只给了 GUI、没给模型。
        self.registry.register(Tool(
            name="topic_link",
            description=(
                "把共享库里的一篇文献**关联到课题**（或解除关联）。\n"
                "共享库只存一份；这里只加/删那条关联 —— **不复制、不删除文献本身**。\n"
                "**「在这个课题下找文献关联」的落子一步就是它**：先用 `kb_search` 在共享库里搜、"
                "或用 `topic_status` 的 `library` 看本课题已挂哪些，再把选中的 id 传进来。\n"
                "`doc_id` 必须是 kb_search / kb_list **真实返回**的 id —— 不要自己拼"
                "（拼错的 id 会挂上去而查不到内容，面板会把它列为「共享库中已找不到」）。\n"
                "同一篇重复关联是幂等的，不会重复计入。"),
            parameters=TOPIC_LINK_SCHEMA,
            fn=_tfn["topic_link"],
            domain=_TOPIC_DOMAIN,
            # 改 `topic.json` ⇒ 按写类登记（同 `topic_write` 2026-10-01 那次勘误）。
            mutating=True,
        ))
        # ⑦ 改课题元数据。原来"管理课题"在对话里只能做一半：
        #   建（topic_use）/ 写台账（topic_write）/ 记卡点，但**改不了**
        #   名称、研究问题、工作阶段、下一步 —— 这四项只存在于 Web 面板的
        #   `update` 分支里。于是用户说"这个课题的研究问题改成 X""阶段推到
        #   证据了"，模型只能说"请到面板上改"。
        self.registry.register(Tool(
            name="topic_update",
            description=(
                "改课题的**元数据**：名称 / 研究问题 / 工作阶段 / 用户记下的下一步。\n"
                "「这个课题的问题改成……」「阶段推到证据了」「下一步记成……」都在这里做，"
                "不必让用户去面板上点。\n"
                "⚠️ 只传要改的那几项即可：**留空 = 不改**（不会把标题或研究问题清掉）。\n"
                "唯一例外是 `next_step`：传空字符串 `\"\"` 表示「这一步做完了，清掉」。\n"
                "阶段只收 ideate / design / evidence / execute / submission 五个值。"),
            parameters=TOPIC_UPDATE_SCHEMA,
            fn=_tfn["topic_update"],
            domain=_TOPIC_DOMAIN,
            mutating=True,      # 改 topic.json ⇒ 按写类登记
        ))
        # ⑧ 记实验台账 / ⑨ 出汇报页（2026-11-02）
        #    ★ 这两个是本轮唯一"用户点名的能力"：他说得很具体 ——
        #      「老师说我没数据，在催我，我也很迷茫」「我需要他指导我下一步该干什么」。
        #      核完之后发现他不是没做实验，是**做的全是定性观察**：
        #      「三株 23℃ 适应但长得慢」「其他几株看起来长得快，但还没测定指标」——
        #      一句数字都没有。所以缺的不是"更聪明的建议"，是**结构**：
        #      把定性与定量分成两个 kind，"没数据"才能被算出来，
        #      "下一步"才能从台账里机械排出来，而不是靠模型发挥。
        self.registry.register(Tool(
            name="lab_record",
            description=(
                "往**实验台账**里记一条事实（菌株 / 观察 / 测量 / 批次 / 待办 / 指标）。\n"
                "为什么要有它：课题的 PLAN/READING/EVIDENCE 都是**散文**，散文里算不出"
                "「你哪几格还没数」；而导师催的正是这个。台账是结构化的，所以能算。\n"
                "★ **定性用 observe，定量用 measure，两者不许混**：\n"
                "  · `observe`（code + text + growth）＝「长得慢」「看起来快」，**不算数据**；\n"
                "  · `measure`（code + metric + value + unit + n）＝ 1.2 mm/d (n=3)，**才算**。\n"
                "  `measure.value` 传「慢」会**直接报错** —— 这是刻意的，不是刁难：\n"
                "  混进去之后「你有没有数据」就永远答不准了。\n"
                "★ 用户随口说的处境（「三株 23℃ 长慢，其他几株快还没测，发酵第 5 天」）\n"
                "  应当被拆成：三条 observe + 一条 batch + 若干 strain，**不要只记一句话**。\n"
                "★ 记完回包里带 `digest`（当前处境摘要）与 `gaps`（缺口格数），\n"
                "  不用再调一次报告就能知道刚才那笔改变了什么。\n"
                "★ 指标 key 只能从已注册里选（growth_rate/biomass/mannitol/adenosine/"
                "ferment_period）；要加新指标先用 `kind=metric` 注册（key+label+unit），"
                "否则报错会列出已知的。\n"
                "★ **记错/记重了用 `kind=retract` 作废，不要手工去改 LAB.jsonl**："
                "作废是一次追加（seq 或 target 指定那一条 + reason 说明为什么），"
                "被作废的那条不再进矩阵/缺口/汇报，但原文仍留在盘上可查。"
                "台账 append-only 的全部意义就是「改过什么能看出来」，手改会把这段历史抹掉。\n"
                "★ 株号还没定（用户没给真实编号，我在用 S01/S02 这类临时名）时，"
                "建株要带 `placeholder=true` —— 汇报页会给它加 `*` 并注明，"
                "否则那几个临时编号在纸上和真株号无法分辨，会被当成事实念出去。"),
            parameters=LAB_RECORD_SCHEMA,
            fn=_tfn["lab_record"],
            domain=_TOPIC_DOMAIN,
            mutating=True,      # 追加写 LAB.jsonl
        ))
        self.registry.register(Tool(
            name="lab_report",
            description=(
                "把实验台账出成**能放到组会/导师桌上的一页**：\n"
                "`LAB.md`（菌株×指标矩阵，空格显示 —；缺口逐条；下一步带来源；方法带文献出处）"
                "+ `LAB.csv`（tidy 长表，可直接喂 `plot_data` 画图）。\n"
                "两个文件写进课题目录，同时把正文一并返回。\n"
                "★ 用户的原话是「没有可以拿出来汇报的东西」，所以这个工具交付的是**文件**，"
                "不是一段建议。拿到 `md_text` 后应当 `write_file` 落到本次工作区，"
                "`csv_text` 存成 csv 再 `plot_data` 出一张图 —— 一页字 + 一张图才叫能汇报。\n"
                "★ 表里的 `—` 是「还没测」，不是「没这项」。汇报时**不要**把 — 换成 0。"),
            parameters=LAB_REPORT_SCHEMA,
            fn=_tfn["lab_report"],
            domain=_TOPIC_DOMAIN,
            mutating=True,      # 写 LAB.md / LAB.csv
        ))
        self.registry.register(Tool(
            name="lab_method",
            description=(
                "**方法卡**：把库里的方法与参数，按课题渲染成一张能照着做的卡。\n"
                "三张卡：`staining`（核质染色 / DAPI / 荧光 / 核相：两套已发表方案的逐项参数、"
                "两篇浓度差 1000 倍的冲突、失败三分诊、第一版下手顺序）、"
                "`identity`（怎么证明这几株是融合子：四环证据链，按成本排，标注台账里哪一环还没记录）、"
                "`quant`（该测哪些指标：单位、n、参照值、你现在的缺口）。\n"
                "★ **问到「怎么做 X」「参数是多少」「下一步做什么实验」，先调这个** ——"
                "卡里的数字全部来自库里文献的 `doc_id` + 页码，是资产渲染的，不是模型编的。\n"
                "★ 卡里没有的方法，再用 `kb_search` / `kb_ask` 去库里查原文。"
                "**不许凭常识补参数**：浓度、时长、波长这三类数字编错一个，实验就白做一天。\n"
                "★ 卡里会给「第一步做什么」的顺序（如阳性对照先于调浓度），"
                "转述这个顺序时不要打乱 —— 打乱了用户就会同时改两个变量。"),
            parameters=LAB_METHOD_SCHEMA,
            fn=_tfn["lab_method"],
            domain=_TOPIC_DOMAIN,
            mutating=True,      # 写 topics/<slug>/方法卡-<key>.md
        ))
        # 图面体检（**会写盘**：审计报告落在工作区里，所以标 mutating 让围栏管）。
        # 存在理由：图有一类独有的失败 —— **所有程序都退出 0，图是错的**。
        # 没有这道工具，第 1~7 条门全都拦不住它（它们核的是"流程走没走"）。
        if getattr(self.cfg.figure, "enabled", True):
            # 数据分析成图（**会写盘**：脚本与 PNG/PDF/SVG/TIFF 都落在工作区）。
            # 它补的是「拿到一份表」到「有一张可以送审的图」之间那一段断链：
            # 过去模型凭手感选图型（两组连续变量画折线、时间序列画箱线），
            # 程序全都退出 0 而图是错的。现在选图型的判据由被测出的列形状给出。
            self.registry.register(Tool(
                name="plot_data",
                description=(
                    "**数据分析成图**：给一张数据表（CSV/TSV/Excel/JSON），"
                    "自动测列形状 → 选图型 → 生成绘图脚本 → 跑出图。\n"
                    "产出：单独一套绘图脚本 `plot.py` + PDF/SVG/PNG(600dpi)/TIFF，"
                    "外带一份**可直接送审的五点契约**与**图注草稿**。\n"
                    "为什么用它而不是自己写绘图代码：它会先把「每列是什么」量出来"
                    "（连续/分类/时间、缺失、基数），再按这个选图型，并把"
                    "「为什么选它 / 为什么不选那几个」写成理由。"
                    "自己凭手感选，最常见的错是把时间序列画成箱线、把两个组的点"
                    "串成一条折线 —— **程序退出码是 0，图是错的**。\n"
                    "★ `kind` 留空即可（自动选）。有分组就填 group，"
                    "时间序列**必须**按组分层。\n"
                    "★ 跑完必须接着调 `audit_figure`：把返回的 contract 与 "
                    "legend_draft 原样传过去（图注里的 ___ 和结论句要你先补全）。"
                    "本工具只管画，不管审。\n"
                    "★ `kind` 可选值不止内置那几种 —— 已装插件注册的图型"
                    "也会出现在 `suggestions` 里，带它自己的适用理由。"),
                parameters=PLOT_DATA_SCHEMA,
                fn=_make_plot_data(self.cfg.figure),
                domain="figure",
                mutating=True,        # 写脚本与产物 → 交给工作区围栏管
            ))
            # 原生 SVG 出图（2026-11-05）：**零第三方依赖**，装完就能画。
            # 与 plot_data 并列而不是替掉它 —— 两者判据不同：
            # plot_data 走投稿路线（PDF + 六道审计门，需要 rongke[figure]）；
            # plot_svg 走预览路线（SVG + 结构自检，只需要标准库）。
            self.registry.register(Tool(
                name="plot_svg",
                description=(
                    "**原生出图（零依赖）**：给一张表，荣珂自己用标准库画出 SVG 矢量图。"
                    "不需要 Origin、不需要 workbench MCP、不需要 matplotlib / pandas / PyMuPDF，"
                    "装完框架就能用；中文按 `Microsoft YaHei → SimHei → Arial` 字体栈回退。\n"
                    "选图型仍然复用 `analysis.plan_from_shape()` 的量出来的列形状判据，"
                    "不是凭手感；出图后**读回文件**跑结构自检（viewBox / 文本节点 / "
                    "数据标记 / 坐标出界 / 中文字体栈 / 外部引用），"
                    "任一不过就 `ok:false` 并保留文件供核查，不假装成功。\n"
                    "⚠️ 这是**预览级**产物（网页 / PPT 插图 / 给用户看）："
                    "PDF 六道渲染后审计门（字形 ≥5pt / 碰撞 / 对齐 / 位图）**一道都没跑**，"
                    "不许把它当投稿级图宣布完成。要出版级请改装 "
                    '`pip install "rongke[figure]"` 后走 `plot_data` + `audit_figure`。\n'
                    "★ `kind` 留空自动选；不支持的图型会明确报错，不会偷换成别的图。"),
                parameters=PLOT_SVG_SCHEMA,
                fn=_make_plot_svg(self.cfg.figure),
                domain="figure",
                mutating=True,        # 写 SVG 到工作区 → 交给围栏管
            ))
            self.registry.register(Tool(
                name="audit_figure",
                description=(
                    "对**产出图**的任务做图面体检（出版级标准，nature-figure 合同）。"
                    "跑四道程序化审计：① 源码预检 ② PDF 字形下限（每字形 ≥5 pt，含上下标）"
                    "③ 渲染后碰撞（文字压字/线穿字/出界）④ 多分面对齐（容差 1.5 pt）；"
                    "外加图注体检与**报告新鲜度**。\n"
                    "同时判定**渲染路线**：要不要走 python/r 由这张图的审计需求决定"
                    "（≥2 分面 / 有定量分面 / 声明了期刊 ⇒ 必须），"
                    "声明成 origin 而条件不满足时，本工具会在**画之前**把契约打回。\n"
                    "为什么必须跑：图最常见的失败是**每个程序都退出 0、图是错的** ——"
                    "字体缺字渲染成豆腐块、面板差零点几 pt、字号被缩到 5 pt 以下，"
                    "这些都不报错。\n"
                    "要产出图的任务，**交付前必须调用本工具**，否则图面门会拦下交付。"
                    "改过图之后必须**重跑** —— 旧报告不算合格证。"),
                parameters=AUDIT_FIGURE_SCHEMA,
                fn=_make_audit_figure(self.cfg.figure),
                domain="figure",
                mutating=True,        # 写审计报告 → 交给工作区围栏管
            ))
        # 组会 PPT 体检（同样**会写盘**）。存在理由与图面门同源：
        # **「能生成 .pptx」和「能讲」是两件事** —— 上游 MCP 只保证文件是合法
        # OOXML，不保证这份 deck 拿去组会不丢人。没有这道工具，
        # 第 1~8 条门全都拦不住它（它们核的是"流程走没走"）。
        if getattr(getattr(self.cfg, "deck", None), "enabled", True):
            self.registry.register(Tool(
                name="audit_deck",
                description=(
                    "对**产出演示文稿**的任务做组会 PPT 体检。跑四道："
                    "① 上游 XML 审计（形状越界 / 文字密度 / AI 套话 / 裁图 / "
                    "小图 / 对齐差一点）② 荣珂收紧的版式判据（正文字号下限、"
                    "页数区间、单页字数、标题）③ **讲稿备注覆盖率**"
                    "（上游只数不判，而组会的备注就是讲稿）④ **报告新鲜度**。\n"
                    "为什么必须跑：**能生成 .pptx ≠ 能拿去讲**。把论文整段粘上"
                    "一页、没有讲稿、正文小到后排看不见 —— 这些全都不报错。\n"
                    "⚠️ 上游 37 个 MCP 工具**没有**写备注的能力；补讲稿备注请调"
                    " `set_slide_notes`（内核提供，你别自己写代码）。\n"
                    "★ `deep_contrast=true` 会**额外调 PowerPoint 渲染**逐页看像素"
                    "（本机实测 ~15 秒，慢一个数量级），**仅在需要检查"
                    "'文字压在照片上'时开启**；默认关闭、不开启时输出与今天逐字相同。\n"
                    "要产出 .pptx 的任务，**交付前必须调用本工具**，否则 deck 门会"
                    "拦下交付。改过 deck 之后必须**重跑** —— 旧报告不算合格证。"),
                parameters=AUDIT_DECK_SCHEMA,
                fn=_make_audit_deck(self.cfg.deck),
                domain="ppt",
                mutating=True,        # 写体检报告 → 交给工作区围栏管
            ))
            # ★★ 品牌模板：**保真路线**（2026-09-22）。
            #   成对注册：一个**只读的清单**（模型得先知道有哪些页型、每槽能装多少字）
            #   + 一个**出稿工具**。清单独立成一个工具而不是塞进 schema 描述里，
            #   理由与 `deck_reference` 相同：它是**可变长的资产**，塞进 schema
            #   就是每场任务都付的固定开销，而只有真要做品牌稿的任务才需要它。
            #
            # ★ 清单**分两段读**（同日补，接一份 72 页的实测版式库时量出来的）：
            #   小模板（≤ `PLAN_INLINE_MAX` 页）一次给全；
            #   大模板只给**页型级概览**，详表按 `page=` 索要。
            #   依据：72 页版式库的全量清单 **36,290 字符 ≈ 18k tokens**，
            #   比全部 69 个工具的 schema 加起来还大，而它一旦读进上下文
            #   就是**整场任务都付**的钱。
            try:
                from .deck import (PLAN_INLINE_MAX, load_template,
                                   template_overview_text, template_pages,
                                   template_plan_text, template_name_of)
                _tname = template_name_of(self.cfg.deck)
                _tpl = load_template(_tname)
                _n_pages = len(template_pages(_tpl))

                def _plan(page: str = "") -> str:
                    _ids = [x for x in re.split(r"[,，;；\s]+", str(page or "")) if x]
                    if _ids:
                        return template_plan_text(_tpl, only=_ids)
                    return (template_plan_text(_tpl) if _n_pages <= PLAN_INLINE_MAX
                            else template_overview_text(_tpl))
            except Exception as e:            # 槽位表不在 / 读不动 ⇒ **如实说**
                _tname, _n_pages = "(读不到)", 0
                # ★ except 块结束时 Python 会把 e 删掉，而 _plan 是嵌套函数、
                #   真正调用发生在块外 —— 直接闭包引用 e 时单元格已被清空
                #   （实测：NameError: cannot access free variable 'e'），
                #   模型收到纯噪声而不是「改走路线乙」的路由信号。必须先落常量。
                _err = f"{type(e).__name__}: {e}"

                def _plan(page: str = "") -> str:
                    return (f"（品牌模板的槽位表读不到：{_err}）\n"
                            f"⇒ 本次**不能**按模板出稿；改用 compose_deck/style_deck 兜底，"
                            f"并在交付时说明这件事。")
            _plan_schema = {
                "type": "object",
                "properties": {
                    "page": {
                        "type": "string",
                        "description": (
                            "想要**哪几页**的槽位与字数上限（逗号分隔的页型 id，"
                            "如 `\"封面,章节页-1\"`）。\n"
                            "留空则：页型少的模板直接给全量清单；"
                            f"页型多的模板（超过 {PLAN_INLINE_MAX} 页）只给**页型级概览**，"
                            "这时你要**按它列出的 id 再调一次**本工具拿详表。"),
                    },
                },
            }
            self.registry.register(Tool(
                name="deck_template_plan",
                description=(
                    "列出**学校品牌模板**有哪些页型、每一页有哪些槽位、"
                    "每个槽能装多少字（上限从几何与字号**算出来**，不是估的）。\n"
                    "**动手写文案之前先调它** —— 模板的槽位是有硬容量的"
                    "（页标题只装 5 字、章节标题 6 字），不看上限去写必然被拒。\n"
                    "只在概览模式下，用 `page=\"页型1,页型2\"` 再调一次拿详表；"
                    "**槽位索引不要凭印象写**。\n"
                    "只读，不产生任何文件。"),
                parameters=_plan_schema,
                fn=_plan,
                domain="ppt",
                mutating=False,
            ))
            self.registry.register(Tool(
                name="deck_from_template",
                description=(
                    "**按学校品牌模板出稿**（保真路线）：保留模板页的原样装饰"
                    "（满版蓝底、章节巨型数字、校徽、出血照片），只在文字槽上填字，"
                    "多余的页删掉。品牌装饰**一个字节都不重绘**。\n"
                    "这是**第一选择** —— 只要这份稿是要交给学校/组会看的，"
                    "外观就该是学校的样子；`compose_deck` / `style_deck` 是**兜底**"
                    "（模板不可用时才用，它们只是近似品）。\n"
                    "用法：① 先调 `deck_template_plan()` 拿到页型与**每槽字数上限**；"
                    "② 再调本工具，`pages` 按顺序给 `[{\"page\": \"cover\", "
                    "\"slots\": {\"2\": \"标题\", …}}, …]`。\n"
                    "内核会：逐槽检查字数上限（超了先缩字号、缩不动就拒绝并告诉你上限是多少）、"
                    "**从磁盘重开文件逐槽读回来复测**，并写一份 `.origin.json` "
                    "让体检能扣掉'模板自带的品牌装饰'（那些不是缺陷）。\n"
                    "⚠️ **每个槽都必须填**；⚠️ 同一个模板页只能用一次；"
                    "⚠️ 出稿后仍须调 `audit_deck`（本工具**不代替门**）。"),
                parameters=TEMPLATE_DECK_SCHEMA,
                fn=_make_deck_from_template(self.cfg.deck),
                domain="ppt",
                mutating=True,
            ))
            # ★ 写讲稿备注（第二个基元缺口的补法）。
            #   放在**同一个 `if` 里、同一个域**：只有要产出 deck 的任务才挂它。
            #   不挂到 `core` 域的理由是**每场任务都要付它的 schema 体积** ——
            #   而 schema 体积正是首步输入的最大一笔开销（实测口径见 skill）。
            self.registry.register(Tool(
                name="set_slide_notes",
                description=(
                    "给已有 .pptx 的**每一页**写讲稿备注（备注栏就是讲稿本身）。\n"
                    "为什么必须有这一步：**上游 37 个 MCP 工具没有写备注的能力** ——"
                    "不调本工具，备注覆盖率就恒为 0，而本轮契约要了讲稿时"
                    "第 9 条硬门必然拦下交付（实测：这一条曾让整场 25 分钟零交付）。\n"
                    "用法：`notes` 是**按页序一一对应**的字符串列表，"
                    "长度必须**恰好等于页数**（第 1 段配第 1 页）；"
                    "某页确实不要讲稿也要占位给空串。页数不匹配会被拒绝，"
                    "并告诉你这份 deck 真实有几页。\n"
                    "写入由内核在子进程里用 python-pptx 完成 —— **你不需要、也不应该"
                    "自己写代码**（你没有装包和改环境的能力）。写完内核会"
                    "**独立复测**（重新读回文件逐页比对），把「写了几页 / 覆盖率 /"
                    "哪几页还没有讲稿」如实报给你。\n"
                    "⚠️ 补完**不要再调 `save_presentation`** 存同一份 deck —— "
                    "MCP 服务端内存里那份没有备注，一存盘就会把它整片抹掉。\n"
                    "⚠️ 改过 deck 会让之前的体检报告作废 ⇒ 写完必须**重跑** `audit_deck`。"),
                parameters=SET_NOTES_SCHEMA,
                fn=_make_set_slide_notes(self.cfg.deck),
                domain="ppt",
                mutating=True,        # 真的改盘上的 .pptx → 交给工作区围栏管
            ))
            # ★ 套视觉主题（2026-09-21 补的第三个缺口：**能生成 ≠ 拿得出手**）。
            #   起因是用户看完一份**结构全绿**的 deck 只说了一句「那能做的这么丑」。
            #   与「能生成 .pptx ≠ 能讲」同源，但量的是另一件事：**外观**。
            #   放在同一个 `if`、同一个域 —— 只有要产出 deck 的任务才挂它。
            self.registry.register(Tool(
                name="style_deck",
                description=(
                    "给已有 .pptx 套上视觉主题。**真改 XML**：画布改 16:9（标准 "
                    "12192000×6858000 EMU）、重写 `theme1.xml` 的 12 个色槽与 "
                    "major/minor 字体、每页加绢色背景与页脚页码。\n"
                    "主题 `juanqing` = 绢青陈墨（绢底 #F4EFE4、墨字 #23201C、"
                    "朱砂点睛 #B03A2E、宋体标题＋雅黑正文）。\n"
                    "为什么必须走这一步：上游 `create_presentation` 起手出的是 "
                    "python-pptx 的**默认空白模板**（4:3、白底、Calibri），"
                    "而体检会把「4:3 画布」和「纯默认外观」判成 **FAIL 并拦下交付**。\n"
                    "⚠️ **不要**再指望 `apply_professional_design(operation=\"theme\")` ——"
                    "那是**空壳**：它回 `{\"success\": true, \"slides_affected\": N}`，"
                    "但实测 `theme1.xml` 的 md5、画布尺寸、part 数**一个都没变**"
                    "（这类「自称改了、文件没动」的**假成功**是本项目点名的缺陷形状）。\n"
                    "用法：保存 deck 之后调一次即可，内核会**独立复测**并把读回来的"
                    "数字报给你。改完必须**重跑** `audit_deck`（旧报告作废）。\n"
                    "⚠️ 套完**不要再调 `save_presentation`** 存同一份 deck —— "
                    "服务端内存里那份没套主题，一存盘就会把主题整片抹回去"
                    "（与写讲稿备注同一条禁忌）。"),
                parameters=STYLE_DECK_SCHEMA,
                fn=_make_style_deck(self.cfg.deck),
                domain="ppt",
                mutating=True,        # 真改盘上的 .pptx
            ))
            # ★ 参考样例（2026-09-21）。**只读**，排在 `compose_deck` **之前**
            #   —— 模型应当先看"长得对是什么样"，再动手排版。
            #   它是用户那句「人家加个 skill 就好看」的直接产物：
            #   参考样例要起作用就必须是**文本的**（模型读不了图，见执行体 docstring）。
            self.registry.register(Tool(
                name="deck_reference",
                description=(
                    "取一份**随库发布的组会 deck 参考样例**（文本形态）："
                    "12 页、六种页型全用到的完整示范 —— 逐页页型、标题、正文，"
                    "外加每页的**讲稿备注**（写来源/口径/会被问到的问题）。"
                    "还给出「页型 → 适合装什么内容」的对照表。\n"
                    "**动手做 deck 之前先调它一次**：它把「什么样算好」变成"
                    "可以照抄的结构，比任何形容词（「简洁大气」）都有用。\n"
                    "⚠️ 这是**文本**，不是图：内核的模型通道读不了图片，"
                    "所以示范以文字给出。样例另附成品 .pptx 与预览图路径 ——"
                    "那是**给人看的**，你读不了，不要假装读过。\n"
                    "⚠️ 参考样例自己的体检是 **PASS（五门全绿、零提示）**："
                    "照着它的结构做，不会在门那里翻车。\n"
                    "只读工具，不改任何文件，不占工作区围栏。"),
                parameters=DECK_REFERENCE_SCHEMA,
                fn=_make_deck_reference(self.cfg.deck),
                domain="ppt",
                mutating=False,       # **只读** —— 不碰盘上任何东西
            ))
            # ★ 按设计资产**精排**（2026-09-21 补的第四个缺口：**排版本身**）。
            #   上面三个缺口（读输入 / 写备注 / 外观）都补完之后，用户仍然说
            #   「太差了，不能直接上组会」。这一条补的是**版面**：
            #   三区网格、字号阶梯、色彩角色、页型几何 —— 全部来自
            #   `rongke/design.py`（单源），由内核确定性地落到每一页。
            #   放在同一个 `if`、同一个域：只有要产出 deck 的任务才挂它。
            self.registry.register(Tool(
                name="compose_deck",
                description=(
                    "**按设计规范给整份 .pptx 精排**（比 `style_deck` 多管版面本身）。\n"
                    "你只交**逐页页型**（cover/section/content/two-col/chart/closing），"
                    "内核按 `design.py` 的三区网格（标题带 0–120px / 内容带 120–660px / "
                    "页脚带 660–720px、左右边距 72px）、8 档字号阶梯、色彩角色"
                    "与页型几何把每页排好，并补朱砂强调线 + 页脚落款与页码。\n"
                    "**这是要交付组会时该走的最后一步**：`style_deck` 只换色换字、"
                    "不动骨架，所以光靠它「版面还是那副版面」。\n"
                    "⚠️ 动手之前建议先调一次 `deck_reference` —— 它给出一份"
                    "六种页型全用到、五门全绿的参考样例，照着它定页型序列最稳。\n"
                    "⚠️ 与 `style_deck` **二选一**：两个都调 = 双重坐标系。"
                    "要精排就**只调本工具**（它内部已把画布与主题一并统一）。\n"
                    "⚠️ 本工具**就地改写**：不传 `out` 会覆盖 `pptx` 指向的那份。"
                    "想留未排版的原件，先自己复制一份 —— 内核无法替你回滚。\n"
                    "⚠️ **不要**再指望 `add_chart` / `add_table` 之外的外形工具；"
                    "排完**不要再调 `save_presentation`**（服务端内存那份没排过版，"
                    "一存盘会把版面整片抹回去）。\n"
                    "改完必须**重跑** `audit_deck` —— 旧报告作废。\n"
                    "内核会**独立复测**并回报读回来的量：画布比例、主题槽、"
                    "视觉资产前后相等（没丢图）、每页页型标记、"
                    "以及「每个有文字的形状是否都带显式对齐」。"),
                parameters=COMPOSE_DECK_SCHEMA,
                fn=_make_compose_deck(self.cfg.deck),
                domain="ppt",
                mutating=True,        # 真改盘上的 .pptx
            ))

    @property
    def tools(self) -> list[str]:
        return self.registry.names()

    def describe(self) -> str:
        mcp = ""
        if self.mcp.specs:
            got = len(self.mcp_report.get("installed", []))
            mcp = f" | MCP {len(self.mcp.specs)} 服务端/{got} 工具"
        elif self.mcp_report.get("skipped"):
            mcp = f" | MCP 无可用服务端（{len(self.mcp_report['skipped'])} 条原因）"
        # MCP 自检结论只报**有问题**的（ready 是常态，不值得占一行）
        health = ""
        v = (self.mcp_health or {}).get("verdict")
        if v and v != "ready":
            health = f" | ⚠ {(self.mcp_health or {}).get('summary_line', '自检异常')}"
        return (f"荣珂 v{self._version()} | {self.llm.describe()} | "
                f"工具 {len(self.registry)} 个（领域 {len(self._installed)}）{mcp}{health}")

    def mcp_diag(self) -> dict:
        """MCP 的全量诊断。给 CLI / Web 控制台用。

        把三处"没成"的原因合到一起，且**都留着**：
          * hub.skipped        —— 服务端级（disabled / 重名）
          * report.skipped     —— 工具级（起不来 / 重名 / deny）
          * cfg.mcp.rejected   —— 配置级（v1 不支持远程传输 / 配置读不动）
        静默丢掉任何一类，用户就会对着"明明配了却没有"发懵。
        """
        d = self.mcp.diag()
        d["installed"] = list(self.mcp_report.get("installed", []))
        d["skipped"] = ([{"level": "server", **s} for s in self.mcp.skipped]
                        + [{"level": "tool", **s} for s in self.mcp_report.get("skipped", [])])
        d["rejected"] = list(self.cfg.mcp.rejected)
        d["sources"] = list(self.cfg.mcp.sources)
        d["enabled"] = self.cfg.mcp.enabled
        d["errors"] = dict(self.mcp.errors)
        return d

    def _charge_billing(self, result: "RunResult") -> dict:
        """把一轮任务的成本记进本地台账。返回一份自述（永不为 None）。

        **任何异常都不许冒出去。** 任务已经跑完了 —— 因为记账出错而把
        一次成功的任务报成失败，是主次颠倒。失败信息写进返回字典，
        由界面/命令行决定怎么显示。

        `enforce=False`：这里是**事后记账**，不是事前拦截。拦截该发生在
        开始一个任务之前（那时用户还没等十分钟）；跑到一半因为余额不足
        而中断，只会把人晾在那儿。所以超额也如实记下，让余额变负 ——
        负数余额是一个**准确的信号**，比静默中断好。
        """
        try:
            from .billing import Billing
            b = Billing(self.billing_root)
        except Exception as e:                                 # noqa: BLE001
            return {"ok": False, "error": f"账单系统没起来：{type(e).__name__}: {e}",
                    "hint": "检查数据目录是否可写（`rongke --billing` 可查看）"}

        if not getattr(self.cfg, "billing", None) or \
                not getattr(self.cfg.billing, "enabled", True):
            return {"ok": True, "skipped": True, "reason": "账单在配置里关掉了"}

        try:
            e = b.charge(result.cost, run_id=result.run_id,
                         model=self.cfg.provider.model, enforce=False)
            bal = b.balance()
            out = {
                "ok": True,
                "charged": e.amount,
                "charged_display": _pricing.format_cost(e.amount),
                "remaining": bal["remaining"],
                "remaining_display": _pricing.format_cost(bal["remaining"]),
                "exact": bal["exact"],
                "unknown_charges": bal["unknown_charges"],
            }
            # 余额是 UNKNOWN 时必须说出来：否则"$UNKNOWN 还剩"会被
            # 眼睛读成"没问题"。这不是显示细节，是这套账本的核心语义。
            if not bal["exact"]:
                out["note"] = ("本次成本或历史消耗里有算不出金额的"
                               "（价格表没填全），余额只是上限，不是真值。")
            if bal["remaining"] not in (_pricing.UNKNOWN,) and \
                    float(bal["remaining"]) < 0:
                out["overdrawn"] = True
                out["note"] = (out.get("note", "") +
                               " 余额已为负，请换套餐或充值后继续。").strip()
            return out
        except Exception as e:                                 # noqa: BLE001
            return {"ok": False,
                    "error": f"这一轮没记进账：{type(e).__name__}: {e}",
                    "hint": "任务本身已经完成，账单可事后用 `rongke --billing` 补记"}

    def close(self) -> None:
        """收摊：关掉 MCP 子进程。

        内核本身没有别的常驻资源；MCP 服务端是**子进程**，
        留着不管就是泄漏（尤其像 Origin 那种背后还挂着 COM 实例的）。
        """
        try:
            self.mcp.stop_all()
        except Exception:
            pass

    @staticmethod
    def _version() -> str:
        try:
            from . import __version__
            return __version__
        except Exception:
            return "?"

    # ── 工作区 ──────────────────────────────────────────────
    def make_workspace(self, tag: str = "") -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{tag}" if tag else stamp
        ws = (self.cfg.runtime.workspace_dir / name).resolve()
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    # ── 能力预设：本次运行用哪一套 ────────────────────────────
    def _caps_for(self, preset: str) -> tuple:
        """取本次 run 的能力集。`broken` 非空 ⇒ **回退 baseline 并说出口**，绝不静默。

        预设绑定的是**这一次 run()**，不是全局重建：`Manager.boot()` 是长命
        单例，重建会撞 `busy()` 409，也会让追问串味。`run()` 的模块级串行锁
        保证切换 `self.caps` 时不会有两个任务互相踩。
        """
        from .caps import load_capability_preset

        pid = str(preset or "").strip() or "baseline"
        if pid == self.caps.preset_id and self.caps.healthy:
            return self.caps, ""
        ctx = {"registry": self.registry}
        try:
            c = load_capability_preset(pid, ctx=ctx)
        except Exception as e:                             # noqa: BLE001
            return self._baseline_caps, f"装载抛异常：{type(e).__name__}: {e}"
        if c.broken:
            return self._baseline_caps, "；".join(r.error for r in c.broken)
        ctx["caps"] = c
        return c, ""

    # ── 任务入口 ────────────────────────────────────────────
    def run(
        self,
        goal: str,
        contract: TaskContract | None = None,
        max_steps: int | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        workspace: Path | None = None,
        cancel: threading.Event | None = None,
        intervene: Callable[[str, dict], dict | None] | None = None,
        resume_from: dict | None = None,
        kb_only: bool = False,
        mode: str = "",
        preset: str = "",
    ) -> RunResult:
        """跑一个任务。

        **串行化**：run() 会切换进程 cwd（chdir 是进程级状态），并发调用会互相踩。
        内核用模块级锁保证同一时刻只跑一个任务，所以 Web 控制台可以放心用线程。

        `cancel` 是 threading.Event：置位后主循环在**下一步开头**退出，
        并如实报告"已取消"——不假装完成。正在跑的那次工具调用不会被中断。

        `intervene` 是**运行中问人**的通道（评审第①条：消灭"整轮重来"）。
        主循环在三种"再跑也是白跑"的时刻回调它并**阻塞等待**：

        | kind | 触发时机 |
        |---|---|
        | `gate_reject` | 交付被证据门**第 2 次**打回（第 1 次留给模型自己修，不吵人） |
        | `stall` | 检测到原地打转（同工具同参数重复） |
        | `step_limit` | 步数耗尽，**退出前**给一次"加步数/补约束"的机会 |

        回调返回 `{"action": ..., "text": ...}`：

        * `action="continue"` —— 什么都不改，接着跑
        * `action="steer"` —— `text` 作为一条用户消息**插进当前消息栈**并继续。
          因为断点就在消息栈里，**这是从断点续跑，不是重来**
        * `action="extend"` —— 加步数（`steps` 字段，默认 20），仅 `step_limit` 有意义
        * `action="stop"` —— 如实结束，理由写明是用户叫停

        **三条不许违反**（都是"没人应答"这一种情况的不同侧面）：
        ① `intervene=None`（CLI 走这条）⇒ 行为与加这个参数之前**一字不差**；
        ② 回调**抛异常**⇒ 当成没答，降级为原行为；
        ③ 回调**超时返回 None** ⇒ 同上。
        **绝不允许"没人应答"把任务卡死或弄成失败** —— 那是把可用性问题升级成可用性事故。

        `resume_from` 是**跨进程**的断点续跑：把上一次留下的对话快照（见
        `conversation.json`）原样交回来，主循环就**跳过装配、直接接着跑**。
        快照里带 `follow_up`（一句用户的话）时就是**会话内追问**：接回前情后
        把这句话追加为一条 user 消息 —— 一场会话聊到底，而不是一问一跑一清零。

        为什么必须落在磁盘上才算数：`intervene` 的续跑只活在**同一个进程**里 ——
        服务一重启、笔记本一合盖、任务一崩，内存里的消息栈就没了，
        用户看到的是"跑到第 7 步的东西全没了，只能重来"。那正是评审第 2 条骂的
        「7 个中断任务没有归档」。**快照落盘 + 崩溃也归档，才让续跑跨得过进程边界。**
        """
        emit = on_event or (lambda kind, data: None)
        # ── 授权闸门（离线激活码）──────────────────────────────
        # 拦在**发起新任务**这一处，且只拦这一处：控制台照常打开、历史
        # 照常读写、已归档的结果照常看。用户被锁在自己文件外面是最糟的
        # 形态 —— 既帮不上"提醒你该买了"，又让人愤怒。
        #
        # ★ 用自定义异常而不是返回一个失败 RunResult：失败结果会被当成
        #   "任务跑了但没成功"，而这里根本**没开始**。两者混在一起会让
        #   遥测、账单、归档都记上一笔不存在的运行。
        try:
            from .license import License
            _ok, _st = License(getattr(self, "license_root", None)).allows_run()
        except Exception:                              # noqa: BLE001
            # 授权子系统自己坏了，**不许因此拦住用户**。
            # 把"我算不出授权状态"升级成"你用不了"是不可接受的降级。
            _ok, _st = True, None
        if not _ok:
            raise LicenseError(_st.reason if _st else "授权无效")
        with _WORKSPACE_LOCK:
            # ★ 预设局部覆盖放在**任务入口**（不在主循环里）：一是这里才是
            #   「这一场用哪把尺子」该定的地方，二是 `resume_test` /
            #   `mentor_workspace_test` 的夹具自检会扫 `_run_serial` 源码里
            #   所有 `self.X` 读取，主循环多一个依赖就得改两处夹具。
            #   `preset=""` 与 `"baseline"` 一个事件都不发 ⇒ 旧路径等价。
            #
            # ★ 每场结束必须**复原**。`Manager.boot()` 是长命单例：registry、
            #   self.caps、gate.caps 全是进程级状态。2026-11-04 真 HTTP 实测
            #   抓到过：跑完 paper-writing 后工具 66→67 不回落，下一场不带
            #   预设的 baseline 任务照样背着 writing_metrics（还沿用上一场的
            #   14 项门禁）。「预设绑定本次 run」不能只在入口成立。
            chosen = self._baseline_caps
            cleanup_tools: list[str] = []
            try:
                if preset:
                    chosen, caps_err = self._caps_for(preset)
                    if caps_err:
                        emit("preset_error", {"preset": str(preset), "error": caps_err,
                                              "fallback": self._baseline_caps.preset_id})
                    elif str(preset).strip() != "baseline":
                        emit("preset_mounted", chosen.to_dict())
                # 传不传 preset 都显式落到「这一场的尺子」：不传就是开机那套，
                #   绝不留着上一场的残留。
                self.caps = chosen
                self.gate.caps = chosen
                # 开机预设自己注册的工具是长命的，不在撤销清单里。
                boot_tools = set(getattr(self._baseline_caps, "registered_tools", []) or [])
                cleanup_tools = [t for t in (getattr(chosen, "registered_tools", []) or [])
                                 if t not in boot_tools]
                return self._run_serial(goal, contract, max_steps, emit, workspace,
                                        cancel, intervene, resume_from, kb_only=kb_only,
                                        mode=mode)
            finally:
                for _t in cleanup_tools:
                    try:
                        self.registry.unregister(_t)
                    except Exception:                      # noqa: BLE001
                        # 清理失败不许把已经跑完的任务改判成失败 —— 但也不
                        # 假装它成功：事件流里说出口，下一场仍能看见真相。
                        emit("preset_cleanup_error",
                             {"tool": _t, "preset": chosen.preset_id})
                self.caps = self._baseline_caps
                self.gate.caps = self._baseline_caps
                # registry 变了，refresh=once 缓存的 schema 必须跟着作废，
                # 否则「工具撤了、模型还看得到」是另一种泄漏。
                self._baseline_caps.invalidate_mount_cache()

    def _run_serial(
        self,
        goal: str,
        contract: TaskContract | None,
        max_steps: int | None,
        emit: Callable[[str, dict], None],
        workspace: Path | None,
        cancel: threading.Event | None = None,
        intervene: Callable[[str, dict], dict | None] | None = None,
        resume_from: dict | None = None,
        kb_only: bool = False,
        mode: str = "",
    ) -> RunResult:
        self.registry.start_task()
        self.gate.reset()          # 写作体检的打回额度按任务归零

        # 工作区：进程 cwd 切进去，围栏也只放行这里。
        # 这样模型的相对路径产物不会污染项目目录（首跑实测：它把
        # ophiocordyceps_ref_verification/ 建在了 cwd）。
        ws = Path(workspace).resolve() if workspace else self.make_workspace()
        ws.mkdir(parents=True, exist_ok=True)
        self.fence = WorkspaceFence(roots=[ws], base=ws)
        # ── 遥测 ────────────────────────────────────────────
        # 把 hooks.py 里那个**从未被实例化**的 `AuditLog` 的位置转正（见 telemetry.py）。
        # 顺序有讲究：**观察者排在策略之后** —— 这样它记下的是"经过全部拦截之后"
        # 的最终事实（比如围栏改写过参数、或直接拒掉了这次调用）。
        # 反过来放，记下的就是"模型原本想做什么"，与真正执行的事不符。
        self.telemetry = TraceRecorder(
            agent_name="rongke",
            model=self.cfg.provider.model,
            provider=self.cfg.provider.name,
        )
        self.registry.hooks = [self.fence, self.telemetry]
        self.telemetry.start_run()

        # MCP 子进程的 cwd 也必须跟着换工作区。
        # 若沿用上一个任务留下的连接，它的 cwd 还钉在**旧**工作区里 ——
        # 那服务端按相对路径写出来的产物就会落进上一个任务，且毫无报错。
        # 所以：改 workspace + 关掉旧连接，下次调用时按新 cwd 重新起。
        self.mcp.workspace = ws
        self.mcp.stop_all()

        # 对话快照写进本任务的工作区 —— 它是「跨进程续跑」的锚点（见 `_snapshot`）。
        # 放在工作区而不是别处：工作区已经是"这一次任务"的天然边界，
        # 归档、产物、围栏都以它为准，快照没有理由另起一套。
        self._snap_dir = ws
        self._last_step = 0
        # 崩溃时要把"已经烧掉多少"如实带走。累加器只活在 `_loop` 的局部里，
        # 外层兜崩溃时取不到 ⇒ 原实现写死 `usage=Usage()`，于是整条中断路径
        # **必然**报 0（实测一例：真烧 51,241、报 0）。
        # 这里挂的是一个**别名**，不是第二个累加器：`_loop` 每次累加后让它重新
        # 指向同一个对象，全仓仍只有一处真相。`None` = 一次完整响应都没拿到。
        self._run_usage: Usage | None = None

        # 事件双写：一路给界面，一路攒着准备落盘。
        # 落盘是为了**跨进程回放** —— Run 对象只活在服务端内存里，
        # 服务一重启，历史页的记录就成了点不进去的死链。
        events: list[dict] = []

        def tee(kind: str, data: dict) -> None:
            if len(events) < _MAX_ARCHIVE_EVENTS:
                events.append({"kind": kind, "data": data,
                               "t": time.strftime("%H:%M:%S")})
            emit(kind, data)

        emit("workspace", {"path": str(ws)})
        # ★ 导师模式的**状态事实**也进事件（不只进提示词）。
        #   理由：「回放旧会话要得出来当时开没开」—— 归档里那份 `events` 就是唯一真相，
        #   界面读它，而不是另存一份"上次开关是什么"的前端内存（两处真相必然漂一处）。
        #   用 `tee` 不用 `emit`：上面那条 `workspace` 只给界面、不落盘，
        #   而这条**必须落盘**，否则服务一重启，历史里的开关状态就全成了"没开过"。
        #
        # ★ W28：带上**本场会话开局时**的课题 slug。三个理由，缺一个这条字段就没有意义：
        #   ① 界面要能"接着看这个课题的台账"，而课题靠 `CURRENT` 是**跨会话**的残留，
        #      等界面读到的那一刻，别人可能已经切走了（`topic_write` 那段的实测事故）；
        #   ② 追问接的是同一场会话 —— 它的课题身份必须是**开局那一刻**的那一个，
        #      否则「会话内追问」会悄悄写进另一个课题的台账；
        #   ③ 归档里的 `events` 是回放的唯一真相：把话题 id 记在这里，
        #      历史页才**复原得出来当时那个课题**，而不是让前端去猜（猜就是两处真相）。
        #   续跑时**先认快照里那份**（`resume_from["topic"]`），再退回 `CURRENT()`：
        #   CURRENT 是"上次用过的"，快照里那份才是"这一场用的"。
        _sess_topic = str((resume_from or {}).get("topic") or "").strip()
        if not _sess_topic:
            try:
                _sess_topic = topic.current()
            except Exception:
                _sess_topic = ""      # 台账读不动不许拦住任务本身（它只是元信息）
        tee("mentor_mode", {"on": bool(kb_only), "topic": _sess_topic})

        prev_cwd = os.getcwd()
        t0 = time.time()
        result: RunResult | None = None
        self._compact_log = {}          # 与 `self._last_step` 同一手法：跨层取现场
        try:
            os.chdir(ws)
            result = self._loop(goal, contract, max_steps, tee, cancel,
                                intervene, resume_from, kb_only=kb_only,
                                topic_slug=_sess_topic, mode=mode)
            # 出清事实**在收尾处统一挂**，而不是往 7 个 `return RunResult(` 里各塞
            # 一遍：写 7 遍就会有第 8 个返回点漏掉，而"漏掉"在归档里看不出来 ——
            # 只会让 `ctx_econ` 的跨源核对误报。别名对象由 `_loop` 指向同一个 dict。
            result.compacted = dict(getattr(self, "_compact_log", {}) or {}) or None
        except BaseException as e:                       # noqa: BLE001
            # ── 崩溃也要留下归档（评审第 2 条的直接缺口）──
            # 原实现只在 `result is not None` 时归档 ⇒ 任务中途崩掉时，
            # runs/ 里只留一个**空目录**：事后既查不出它跑到哪、也看不出为什么停。
            # 「7 个中断任务没有归档」就是这么来的。
            # 这里**不重新抛**：把崩溃翻成一个如实的 RunResult，让它和
            # 「跑完了但没成功」走同一条收尾路径（成本 / 遥测 / 归档 / 台账都齐）。
            crash = f"{type(e).__name__}: {e}"
            try:
                tee("crash", {"error": crash,
                              "traceback": traceback.format_exc(limit=8),
                              "step": int(getattr(self, "_last_step", 0) or 0)})
            except Exception:
                pass
            # ★ 用量按现场带走，不再写死 `Usage()`。
            #   `_acc is None` = 一次完整响应都没拿到，此时 0 是**下界**不是总额；
            #   非 None = 拿得到已完成的累计，但崩掉那次在途调用的 usage 网关没来得及回。
            #   两种情况都不许被下游读成"这场没花钱" —— 同 `pricing.py` 的定律：
            #   0 是一条断言，"我不知道"是事实，不许把前者渲染成后者。
            _acc = getattr(self, "_run_usage", None)
            _crash_reason = f"执行中断（{crash}）—— 是未完成，不是失败；可以接着跑"
            _crash_step = int(getattr(self, "_last_step", 0) or 0)
            result = RunResult(
                success=False,
                reason=_crash_reason,
                steps=_crash_step,
                usage=(_acc if _acc is not None else Usage()),
                trace=list(self.registry.trace),
                usage_complete=False,
                final=interrupted_final(_crash_reason, _crash_step, ws),
                contract=contract,
                # 崩溃时更要留这份事实：溢出正是从这条路走的，事后要能看出
                # "它出清过、还是压根没来得及出清"。
                compacted=dict(getattr(self, "_compact_log", {}) or {}) or None)
        finally:
            os.chdir(prev_cwd)
            if result is not None:
                result.workspace = str(ws)
                result.run_id = ws.name
        if result is not None:
            # ── 收尾复核：接回 `mentor_trace_audit`（2026-11-03，**分级**，不一票否决）──
            #
            # W36（2026-10-31）把它从收尾处摘掉了，理由是「挂载知识库不该做成审查」。
            # 那个顾虑是**对的**，所以这里不恢复成「跑完再判一次合不合格」，
            # 而是改成一件只产生**事实**、不产生**判决**的事：
            #
            #   · 它**不拦交付**、不改 `success`、不改 `final` 的判定；
            #   · 它只回答一个问题：正文里每个〔库·doc p.N〕，本次到底读到没有；
            #   · 结果挂到 `result.trace_audit`，GUI 读得到、归档存得下。
            #
            # 为什么值得接回来（实测依据，见 docs/导师模式实测-2026-11-03.md 第十六节）：
            # 这套判据**能分辨真假** —— 全部真实落地时 PASS，编造一篇报
            # `doc_not_in_hits`，真篇编造页报 `page_not_hit`，负控两档都拦得住。
            # 而它被摘掉之后，25 处引注里有 11 处本次根本没检索到对应的篇，
            # 学生和老师都看不出来（`__`第十三节）。
            #
            # 为什么现在只「挂事实」不「下判决」：判据②只认本次命中集，
            # 模型靠记忆/方法卡引真文献时也会被判 `doc_not_in_hits`。把它当门会误伤，
            # 当成"来源级标注"才是它真正的用处（同 Hyra 的『观察/已验证/约定』三级）。
            # 2026-11-04 接线收敛：入口只剩 `audit_delivery()` 一个。
            #   · 触发按**行为**（查过库 / 正文有 doc_id / 导师模式），不再只看 kb_only；
            #   · 正文 = 聊天答复 ∪ 本次落盘正文，不再只量 `final`；
            #   · 全库 doc_id 清单在这里接回 `kb_library_ids()`，编造 id 那层不再免检；
            #   · 输出三态 `pass / fail / unknown`，「读不到」不再冒充「不符合」。
            try:
                if not getattr(result, "_audited", False):
                    result.trace_audit = audit_delivery(
                        self.registry.trace, result.final, ws,
                        kb_only=kb_only, registry=self.registry,
                        stages=list(getattr(result.contract, "stages", None) or []))
            except Exception as e:                       # noqa: BLE001
                # 复核是附赠品，出错**不许**影响已经跑完的任务（同 `_archive`）。
                # 但「复核没跑成」必须写成未测，不许留成 None 让界面当通过。
                result.trace_audit = {
                    "error": f"{type(e).__name__}: {e}",
                    "verdict": "unknown", "advisory": True,
                    "unknown_reasons": ["出处复核器异常 ⇒ 本层**未测**"],
                    "summary": "出处复核没跑成（未测，不等于没有违规）。",
                }
            # ★ 接线（2026-11-04）：`renderMentorAudit` 一直挂在前端的
            #   `case "mentor_audit"`，可全仓**没有一处 emit 这个事件** ——
            #   复核结果只躺在 `result.to_dict()` 里，界面永远看不到。
            #   事件名保留旧名：W22/W27 的回放判据按这个 kind 认「审计在场」。
            if result.trace_audit is not None:
                try:
                    tee("mentor_audit", result.trace_audit)
                except Exception:                    # noqa: BLE001
                    pass
            # ── 收尾：闭合根 span，把时间账与成本挂上去 ──
            # 成本由**总用量算出**，而不是逐步累加 ——
            # 逐步累加会多一条"累加器"这条独立真相（B 型缺陷：两处各算一遍，
            # 靠约定同步）。总用量已经是唯一真相，计价就该是它的纯函数。
            elapsed = time.time() - t0
            cost = _pricing.estimate_cost(result.usage, self.cfg.provider.model,
                                         getattr(self.cfg, "pricing", None))
            result.cost = cost
            # ── 账单：把这一轮消耗记进本地台账 ──
            # ★ 为什么要在这里扣，而不是在调用 `run()` 的外面扣：
            #   成本是**总用量的纯函数**，而总用量只有这里知道。放到外面
            #   就意味着调用方各自去算一遍，那又多了一处真相（B 型缺陷）。
            # ★ 为什么扣费失败**不许**影响任务结果：任务已经跑完了，
            #   图已经画出来了。因为记账出错就把成功报成失败是颠倒轻重。
            #   所以这里捕获一切异常，只写进 `result.billing` 让人看得见。
            result.billing = self._charge_billing(result)
            if self.telemetry is not None:
                self.telemetry.finish(
                    cost=cost, iteration_count=result.steps,
                    status="ok" if result.success else "error")
                result.telemetry = self.telemetry.summary(
                    elapsed, cost=cost, llm_calls=result.steps)
                emit("metrics", {"telemetry": result.telemetry,
                                 "cost_display": _pricing.format_cost(cost)})
            # 归档的 goal 用**会话标题**（契约里的用户原话）而不是这一轮的输入：
            # 会话内追问时，这一轮 goal 是最后一句追问 —— 拿它当标题，
            # 历史里同一个会话就会一遍换一个名字。原话才是这场会话的身份。
            # 契约从 result 上取：自动起草发生在 `_loop` 里，外层这个局部
            # 变量在"没传冻结契约"时是 None（那正是默认路径）。
            _title_c = result.contract or contract
            self._archive(ws, result, events, t0,
                          str(getattr(_title_c, "raw_goal", "") or goal),
                          spans=(self.telemetry.spans if self.telemetry else []))
            if result.success:
                # 跑成功了就不需要「接着跑」的快照 —— 留着只是占盘。
                # **失败 / 中断的一律保留**：那正是用户要续跑的场合。
                #
                # ★ 为什么是「改名」而不是「删掉」（2026-09-21 改）：
                #   本机**按轮次**收删除费（额度 50 点/轮，一个文件 1 点），
                #   而这一步在**每一次成功的任务**末尾都会跑 —— 于是"跑通一个任务"
                #   会静默花掉一点额度。额度吃穿之后守卫**直接杀进程**：
                #   主循环会在 `_archive` 之后、`memory.save_run` 之前被杀，
                #   用户看到的是"一次莫名其妙崩掉的、其实已经成功的任务"。
                #   ⚠️ 外面那个 try/except **兜不住"被杀"** —— 它只拦异常，拦不住信号。
                #   改名（`os.replace`）**不计费**，而且"能不能接着跑"的判据
                #   （`web/server.py` 里 `run.resumable` 找 `conversation.json`）
                #   会自动失效 —— 语义与原实现**一模一样**。
                #   代价只是几十 KB 盘，而这个目录本来就是**永久归档**，盘不是瓶颈。
                for _f, _t in SNAPSHOT_RENAMES:
                    try:
                        _src = ws / _f
                        if _src.is_file():
                            os.replace(_src, ws / _t)
                    except Exception:
                        pass
        if self.memory and result is not None:
            try:
                self.memory.save_run(goal, result, time.time() - t0)
                emit("memory_saved", {"runs": self.memory.stats()["runs"]})
            except Exception:
                pass
        return result

    # ── 归档 ────────────────────────────────────────────────
    @staticmethod
    def _archive(ws: Path, result: RunResult, events: list[dict],
                 started: float, goal: str, spans: list | None = None) -> None:
        """把整场任务落盘，供跨进程回放。

        写失败**不打断任务** —— 归档是附赠品，不是交付的一部分。
        `result` 走 to_dict()：trace 里每个调用带 `args` + `digest` +
        `result_preview`（≤800 字）+ `result_truncated` + `anchors`（DOI/URL/数值），
        **仍不含结果原文**（一次长检索就能让归档膨胀到几十 MB）。

        为什么是"预览 + 锚点"而不是"要么全存、要么只存哈希"：
        只存哈希 ⇒ 证据门第 4 条**离线不可复算**、界面只能显示一串哈希（Q 型缺陷）；
        全存 ⇒ 归档不可分发。锚点让「这份稿子里的每个数字 / DOI 能不能追到一次真实调用」
        变成可离线重算的**字符串比对** —— 这是审计导出的第一块砖。

        `spans` 单独写成 `trace.jsonl`（一 span 一行）而不是塞进 session.json：
        span 条数随步数线性增长，混进 JSON 会让整个归档越来越难解析；
        分行则能流式读、能 tail、坏一行不影响其余。
        """
        try:
            payload = {
                "id": ws.name,
                "goal": goal,
                "workspace": str(ws),
                "started": started,
                "finished": time.time(),
                "elapsed": round(time.time() - started, 1),
                "events": events,
                "result": result.to_dict(),
                "error": "",
            }
            (ws / "session.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        # trace.jsonl 独立写：它失败不该连累 session.json（已先写完）。
        if spans:
            try:
                from .telemetry import MAX_SPANS
                rows = spans[:MAX_SPANS]
                text = "\n".join(
                    json.dumps(s.to_dict() if hasattr(s, "to_dict") else s,
                               ensure_ascii=False, default=str)
                    for s in rows)
                (ws / "trace.jsonl").write_text(text + "\n", encoding="utf-8")
            except Exception:
                pass

    @staticmethod
    def _written_files(root: "Path", limit: int = 12) -> list[str]:
        """本次工作区里**成文的产物**文件名（给 `final` 用）。

        只列正文类后缀、只列一层 + `data/` 一层，按修改时间从新到旧。
        拿不到就返回空表 —— 这里任何失败都不许影响已经跑完的任务。
        """
        out: list[tuple[float, str]] = []
        for base in (root, root / "data"):
            try:
                for p in base.iterdir():
                    if p.is_file() and p.suffix.lower() in _PROSE_SUFFIXES:
                        out.append((p.stat().st_mtime, p.name))
            except Exception:                            # noqa: BLE001
                continue
        out.sort(reverse=True)
        return [n for _, n in out[:limit]]

    @staticmethod
    def _draft_for_display(claim: CompletionClaim, limit: int = 200_000) -> str:
        """把交付正文取出来给界面看（draft_text 优先，否则读 draft_path）。"""
        t = (claim.draft_text or "").strip()
        if t:
            return t[:limit]
        p = (claim.draft_path or "").strip()
        if p:
            try:
                return Path(p).read_text(encoding="utf-8", errors="replace")[:limit]
            except Exception:
                return ""
        return ""

    # ── 主循环 ──────────────────────────────────────────────
    def _loop(
        self,
        goal: str,
        contract: TaskContract | None,
        max_steps: int | None,
        emit: Callable[[str, dict], None],
        cancel: threading.Event | None = None,
        intervene: Callable[[str, dict], dict | None] | None = None,
        resume: dict | None = None,
        kb_only: bool = False,
        topic_slug: str = "",
        mode: str = "",
    ) -> RunResult:
        cfg = self.cfg
        max_steps = max_steps or cfg.loop.max_steps

        # ── 续跑：把上一次留下的对话**原样接回来** ──
        # 续跑时**绝不重新起草契约**：契约已在快照里，重新起草会得到另一份标准，
        # 于是"接着跑"其实换了一把尺子 —— 那正是评测口径里"评分标准漂移"的老问题，
        # 在这里会变成"同一份稿子前后两段按不同要求写"。
        resumed = list((resume or {}).get("messages") or [])
        # ★ W28 会话的**课题身份**：优先认快照里那份（`resume["topic"]`），
        #   其次认调用方给的（`_run_serial` 开局时读的 `CURRENT`），**不**在这里
        #   再读一次 `CURRENT` —— 那是"上一次用过的课题"，中途被切走就串了。
        #   它只有一个去处：`_snapshot` 落盘时带上，供下一次续跑/追问认回同一个课题。
        _sess_topic = (str(topic_slug or "").strip()
                       or str((resume or {}).get("topic") or "").strip())
        if resumed and contract is None and (resume or {}).get("contract"):
            try:
                contract = TaskContract.from_dict(resume["contract"])
            except Exception as e:
                emit("resume_note", {"text": f"快照里的契约用不了，回退到自动起草：{e}"})
        if contract is None:
            emit("contract_drafting", {"goal": goal})
            contract = TaskContract.draft(goal, self.llm)        # ★ W36「任务模式 / 仅问答」：用户亲手按下的开关**优先**，盖过起草器与词表。
        #   用户原话：「直接加个任务模式和仅问答不就行了」—— 对，这件事该是**他说的**，
        #   不是我们猜的。`answer_only=True` 的契约在门那边换一种判定（要真答，
        #   不要产物，但**编造证据照样抓**，见 gate.check 的第 1 条）。
        #   只改这一个字段，不动 deliverables/acceptance —— 用户要是真要东西，
        #   把模式切回任务即可，而"我替你加一条验收标准"是绝不添加用户没说过的东西。
        if (mode or "").strip() == "answer" and not contract.answer_only:
            contract.answer_only = True
            emit("mode", {"mode": "answer", "by": "user",
                          "text": "仅问答：这一轮按「回答 + 自查」走，不要求产物与证据"})
        elif (mode or "").strip() == "task" and contract.answer_only:
            contract.answer_only = False
            emit("mode", {"mode": "task", "by": "user",
                          "text": "任务模式：这一轮要交东西，按契约与证据门走"})
        # ── 预设声明的工序 → 契约（2026-11-04，接活 Preset.stages）──────────
        # 每道工序对应 stages.py 里一个**专属 skill**。用户在胶囊里选了「统计
        # 审查」，就该真按统计那几道走，而不是仍由话术关键词碰运气 —— 关键词
        # 只认用户恰好用的那个字眼，换种问法就漏。
        #
        # ★ 只加不删：原话推出来的工序一律保留，预设只补它点名的那几道；
        #   再按 STAGE_ORDER 归位（plan_text 逐条编号念给模型，顺序即指令）。
        # ★ 这份清单同时是**模型看得见**的：contract.to_prompt() 里 plan_text()
        #   会把每道工序和它的 skill 名念出来，门那边才按同一份清单核装载。
        #   工具给了、判据也给了，才不会变成 D 场那种「交不出去的死门」。
        # ★ 取只读视图用 getattr 降级：续跑 / 轻量夹具里可能没有这个属性，
        #   算不出就安静跳过，别把一场本来能跑完的任务打崩。注释里同理，
        #   夹具自检扫的是源码文本，别在这里写点号形式的属性名。
        _view = getattr(self, "caps", None)
        _declared = list(getattr(_view, "preset_stages", []) or [])
        if _declared:
            from .caps import merge_preset_stages
            _merge = merge_preset_stages(contract, _declared)
            if _merge["changed"]:
                emit("preset_stages", {
                    "preset": getattr(_view, "preset_id", "") or "",
                    "declared": _merge["declared"],
                    "added": _merge["added"],
                    "stages": _merge["stages"],
                    "skills": _merge["skills"],
                })
        # ★ W36（2026-10-31 定稿，用户原话「普通对话不该先跑一遍整理任务单」）：
        #   起草那一次调用**留着**（它是判断"这算不算写稿"的唯一来源，省不掉），
        #   但**非写作任务不再把任务单发到界面**。
        #
        #   区别要说准：用户讨厌的不是"多花一次模型调用"（他看不见这个），
        #   而是"我就问一句话，它先给我列一张单子"——那是把对话变成了填表。
        #   所以这里按 `needs_writing` 分流：
        #     · 写稿 ⇒ 照旧发 `contract`（那张单子真的是尺子，该看得见）；
        #     · 其余 ⇒ 只发一句 `mode` 说明"这是一次对话，不是任务"，
        #       界面不会长出任务单，模型那边照样带着契约干活。
        # ★ 用 getattr 兜底：`contract` 不一定是真 `TaskContract`（续跑替身、
        #   测试夹具都会传轻量对象）。这里只是**分流发不发任务单**，
        #   拿不到这个字段就当"不是写稿"——不列单子比无谓崩掉好。
        if getattr(contract, "needs_writing", False):
            emit("contract", contract.to_dict())
        else:
            emit("mode", {"mode": "chat", "by": "auto",
                          "text": "对话式请求：直接办，不列任务单、不要验收标准"})

        if resumed:
            # ★ E2「追问先脱水」（默认关闭）：追问背着父轮完整历史开跑，
            #   到第 3 步才压，前两步白背 145,277 tok。预设打开时，在
            #   **接回消息栈之前**先裁成「开场 system 段 + 最近 keep_live 条」。
            #   用 getattr 取而非直接读属性：内核在 resume/mentor 的轻量夹具里
            #   没有这个属性，算不出就该降级而不是崩。
            #   ⚠️ 注释里**不许写属性名的点号形式**：resume_test 的夹具自检是
            #   正则扫源码字面量的，写在注释里也会被当成真实读取（2026-11-04 实踩）。
            _caps0 = getattr(self, "caps", None)
            _cc0 = _caps0.inst("context") if _caps0 is not None else None
            if (_cc0 is not None and _cc0.cfg["compact_before_followup"]
                    and str((resume or {}).get("follow_up") or "").strip()):
                keep = int(_cc0.cfg.get("keep_live", 6))
                tail = resumed[-keep:] if keep > 0 else list(resumed)
                tail_ids = {id(m) for m in tail}
                head = [m for m in resumed
                        if m.get("role") == "system" and id(m) not in tail_ids]
                trimmed = head + tail
                if len(trimmed) < len(resumed):
                    emit("followup_precompact",
                         {"before": len(resumed), "after": len(trimmed),
                          "kept_live": keep})
                    resumed = trimmed
            messages: list[dict[str, Any]] = [dict(m) for m in resumed]
            # ── 会话内追问（连续会话的全部机制就这几行）──
            # 与 `_steer` 同一条原理：断点本来就在消息栈里，追加一条 user 消息，
            # 下一轮模型就带着**完整前情 + 新的话**接着干。区别只在时机：
            # `_steer` 是运行中插话，这里是一轮结束后接着说。
            # 契约**原样沿用**（不重新起草、不改 raw_goal）—— 它是这一场会话
            # 的验收尺子；追问只是往同一把尺子下加一条新指令。
            _fu = str((resume or {}).get("follow_up") or "").strip()
            if _fu:
                messages.append({"role": "user", "content":
                                 f"【会话内追问 —— 就地接着说，不必重来】\n{_fu}"})
                # 历史回放要能显示"你追问了什么"：只记长度会让用户
                # 打开历史时只看到一段无头的过程（实测：输入 1 的那次）。
                emit("follow_up", {"chars": len(_fu), "text": _fu})
                # ★ Q5「追问的新指令要进验收」（默认关闭）。不重起草契约、
                #   不改 raw_goal、父轮条款一条不删 —— 只把这句追问里的
                #   祈使/否定句追加成新的 acceptance。C 场那三条要求就是
                #   这么丢的；baseline 下这条完全不执行。
                _caps1 = getattr(self, "caps", None)
                _fc1 = _caps1.inst("followup") if _caps1 is not None else None
                if _fc1 is not None and _fc1.cfg["merge_new_constraints"]:
                    _parent = list(getattr(contract, "acceptance", None) or [])
                    _new = _fc1.split_constraints(_fu, _parent)
                    if _new:
                        try:
                            contract.acceptance = _parent + _new
                            emit("followup_constraints",
                                 {"n": len(_new), "items": _new,
                                  "parent_kept": len(_parent)})
                        except Exception as e:               # noqa: BLE001
                            emit("followup_constraints",
                                 {"n": 0, "items": [], "error": f"{type(e).__name__}: {e}"})
            # ★ 导师模式在**同一场会话里**的状态延续（W22-B §3.2）。
            #   追问接回来的是同一份消息栈，于是有两种对不上：
            #   · 这次开着、栈里没有那段（上一轮没开）⇒ **补上**，否则开关是假的；
            #   · 这次关了、栈里还有那段（上一轮开着）⇒ **撤销**，否则界面已经关了
            #     而模型还在按导师模式答题，用户看不见这个分差。
            #   ★ 只测"打得开"不测"关得掉"，正是本项目记过的单向测试缺口 —— 两个方向
            #     在这里都成立，也都进 `resumed` 的计数（补了几条要说得出几条）。
            _had = any(MENTOR_MARK in str(m.get("content") or "") for m in messages)
            if kb_only and not _had:
                messages.append({"role": "user", "content": mentor_mode_text()})
                emit("mentor_mode_injected", {"action": "on", "reason": "会话中途开启"})
            elif _had and not kb_only:
                messages.append({"role": "user", "content": mentor_exit_text()})
                emit("mentor_mode_injected", {"action": "off", "reason": "会话中途关闭"})
            emit("resumed", {"from_step": int((resume or {}).get("step") or 0),
                             "messages": len(messages),
                             "chars": _messages_chars(messages),
                             # 接回来的这份栈**是否已经脱过水**，必须在续跑那一刻说清：
                             # 否则下一次出清的"省了多少"会拿一份已经缩过水的历史当基线，
                             # 于是 ctx_econ 里 freed_chars 偏低、看着像"没起作用"。
                             "prior_compaction": (resume or {}).get("compacted") or {}})
        else:
            # `Path.cwd()` 就是本次工作区：`_run_serial` 在调 `_loop` 之前
            # 已经 `os.chdir(ws)`（同一个前提 `_make_audit_deck` 也在用）。
            # ⚠️ 顺带一条纪律：**cwd 是被测语义的一部分** —— 谁在别的 cwd 下
            # 调这条路径，谁就会拿到错的清单（测试里必须自己 chdir）。
            # ★ 课题上下文（导师模式）由 `initial_messages` 内部注入，这里**不传
            #   `topics_root`**：默认落点由 `topic.topics_root()` **单源**定义 =
            #   `config.project_root()/topics`（即 `<项目根>/topics`）。
            #   **这是有意的** —— 课题要跨 run 活着，不能住在某个 run 的工作区里；
            #   而 `config.project_root()` 由**代码位置**推导，不受 `_run_serial` 的
            #   `os.chdir(ws)` 影响，所以「工作区」与「课题台账」两条路径不会互相污染。
            _asm_fact: dict = {}
            messages = initial_messages(contract, Path.cwd(), kb_only=kb_only,
                                        topic_slug=_sess_topic,
                                        caps=getattr(self, "caps", None),
                                        assembly_out=_asm_fact)
            # 装配事实只在**开场**发一次 —— 这就是「整场冻结」的可见证据。
            # baseline 的 saved=0 / complete=False ⇒ 一个事件都不发，
            # 旧事件流一字不变；续跑路径的消息栈是接回来的，也不重发。
            if (_asm_fact.get("saved") or _asm_fact.get("complete")
                    or _asm_fact.get("degraded")):
                emit("system_assembled", dict(_asm_fact))
            if self.memory:
                hist = self.memory.inject_text(goal)
                if hist:
                    messages.append({"role": "user", "content": hist})
                    emit("memory_recall", {"chars": len(hist)})
        # ── 按领域按需挂载（理由见 `mounting_decision`）──────────
        # 默认关掉裁剪只有一个开关：`loop.domain_mount=false`。
        # 留这个开关是**诚实**的一部分：一个悄悄生效的性能改动，
        # 出问题时没有人能一键退回去。
        _mount = {"allow": None, "domains": [], "reason": "裁剪被配置关掉（loop.domain_mount=false）"}
        if getattr(cfg.loop, "domain_mount", True):
            _dom = {n: (self.registry.get(n).domain if self.registry.get(n) else "")
                    for n in self.registry.names()}
            # ★ kb_only（导师开关）必须进挂载判据：开关承诺了"基于文献库回答"，
            #   挂载就得把 kb/topic 域给足 —— 否则前端那个开关是装饰（W25 不变量）。
            _mount = mounting_decision(contract, list(_dom), lambda n: _dom.get(n, ""),
                                       kb_only=kb_only,
                                       # ★ 与 `initial_messages` 读**同一个来源**：
                                       #   本轮会话身份优先；没有就用 `current()` ——
                                       #   因为开场注入用的正是这个兜底。两处读同一个
                                       #   东西，才不会出现"提示词说在课题里、工具却在外面"。
                                       has_topic=bool(_sess_topic or topic.current()))
        # ★ E1「可刷新接口，但不把 schemas 整段移进循环」：baseline 的
        #   `refresh="once"` 每步返回同一个缓存对象，零额外开销、字符数不变；
        #   `per_stage` 才按档重算。整段替换会污染事件流顺序，这里只换取值来源。
        _caps2 = getattr(self, "caps", None)
        _mc2 = _caps2.inst("mount") if _caps2 is not None else None
        # ★ 必留工具面由**本次挂载的能力**贡献（检索插件保住整条检索链）。
        #   baseline 没有贡献 ⇒ 空元组 ⇒ 与旧版逐字等价。用 getattr 降级：
        #   续跑 / 测试夹具可能塞轻量替身，读不到就当没有，不把任务打崩。
        _ess = (list(_caps2.essential_tools())
                if _caps2 is not None and hasattr(_caps2, "essential_tools")
                else [])
        schemas = (_mc2.schemas_for(self.registry, _mount["allow"], 0, "open",
                                    essential=_ess)
                   if _mc2 is not None
                   else self.registry.openai_schemas(_mount["allow"]))
        # ★ 工具可见性硬预算：预设写了 `schema_chars_budget` 才裁（default=0 不限），
        #   裁掉的**每一个**工具都要报出来 —— 悄悄缩小模型的能力边界，出了事
        #   没人能从事件流里看出来。开场算一次、整场冻结，保住前缀缓存稳定前缀。
        _bf = getattr(_mc2, "budget_report", None) if _mc2 is not None else None
        if _bf:
            emit("tools_budget_applied", dict(_bf))
        if _mc2 is not None and _mc2.cfg["refresh"] != "once":
            emit("schema_refresh",
                 {"refresh": _mc2.cfg["refresh"],
                  "schema_chars": len(json.dumps(schemas, ensure_ascii=False, default=str))})
        if _mount["allow"] is not None:
            emit("tools_mounted", {
                "allow": len(_mount["allow"]), "total": len(self.registry),
                "domains": _mount["domains"], "reason": _mount["reason"],
                "schema_chars": len(json.dumps(schemas, ensure_ascii=False, default=str)),
            })
        # 稳定段的体量：工具 schema（外加每轮原样重发的 SYSTEM）。
        # 这是**前缀缓存最值钱的对象** —— 一个装了 20+ 工具的内核，
        # 每次调用都要重发这坨 schema。量出它，才能回答"网关到底有没有在缓存"
        # （`cache_ratio` 恒为 0 就是没有，那是纯浪费，不是"本来就这样"）。
        tools_schema_chars = len(json.dumps(schemas, ensure_ascii=False, default=str))
        warn_chars = int(getattr(cfg.loop, "context_warn_chars", 0) or 0)
        warned_context = False
        # ── 上下文经济与预算闸的状态 ────────────────────────────
        # `compact_target=0` 表示这项关着（与 `context_warn_chars` 同一个约定）。
        compact_target = int(getattr(cfg.loop, "compact_target_chars", 0) or 0)
        # 预设可以改压缩阈值，但 **不动 cfg.loop 的默认值**：baseline 写的
        # 120000 与现值相同 ⇒ 旧路径逐字等价；strict/任务预设才给新值。
        _caps3 = getattr(self, "caps", None)
        _cc3 = _caps3.inst("context") if _caps3 is not None else None
        if _cc3 is not None:
            compact_target = int(_cc3.cfg["target_chars"])
        compact_keep_live = int(getattr(cfg.loop, "compact_keep_live", 6) or 0)
        # 最后一次出清的**事实**。写进快照是给续跑者看的：它有权知道自己接手的
        # 是不是半份历史 —— 不写，就是本项目最反感的那种静默。
        # 续跑时**接上**上一次的事实，而不是从零开始：快照里那份 `compacted`
        # 说的是"你接手的这个栈已经脱过水"。丢掉它就等于让续跑者以为拿到全量历史。
        # 顺带把计数也接上 —— 否则"每场最多出清 1 次"这条验收可以靠续跑绕过去。
        _inherited = dict((resume or {}).get("compacted") or {})
        compact_log: dict[str, Any] = dict(_inherited)
        # 同一个 dict 对象挂到实例上，让 `_run_serial` 收尾时能取到（含崩溃路径）。
        self._compact_log = compact_log
        # 迟滞线：出清过一次就把触发线抬到这里，否则"压不到目标以下"会演变成
        # **每步都压** —— 而每压一次要付一次前缀缓存 miss。0 = 本场还没压过。
        next_compact_at = 0
        compacted_this_task = int(_inherited.get("n") or 0)
        prompt_tokens_last = 0         # 网关**实测**回的上一步 prompt token
        budget_max_prompt = int(getattr(cfg.loop, "budget_max_prompt_tokens", 0) or 0)
        budget_hint_steps = int(getattr(cfg.loop, "budget_hint_steps", 0) or 0)
        hinted_wrapup = False          # 收尾提示每场最多一次
        hinted_pressure = False        # token 压力提示每场最多一次
        warned_degraded = False        # "闸在降级"这句自言自语，每场最多说一次
        total = Usage()
        self._run_usage = total          # 别名跟着走（见 `run()` 里的说明）
        empty_rounds = 0
        remind_every = 8
        gate_rejects = 0
        asked_limit = False
        # 已问过的打转点。键必须是**指纹**（工具名 + 参数），不是 `seq` ——
        # `seq` 是"这一次调用"的身份，每调一次就换一个；拿它当键，守卫恒真，
        # 于是每次重复都问一遍（实测被问了 4 次）。
        # 「同一个坑」的身份 = 同一工具同一参数 = `fingerprint`，与打转检测同源。
        asked_stalls: set[str] = set()

        def ask(kind: str, data: dict) -> dict | None:
            """把问题抛给人并**阻塞等**回答。没人应答一律返回 None（降级为原行为）。

            为什么"发射事件"由内核做、而不是回调自己发：
            `intervene` 是业务回调，界面该看到的是**内核承认的介入点**。
            回调只管取答案 —— 否则 CLI / Web / 测试各广播一遍，必然漂移（H 型）。
            """
            if intervene is None:
                return None
            payload = {"kind": kind, "step": step, **data}
            emit("intervene_request", payload)
            t0 = time.time()
            try:
                ans = intervene(kind, payload)
            except Exception as e:
                emit("intervene_note", {
                    "kind": kind, "outcome": "error",
                    "text": f"介入通道出错，按原路继续：{type(e).__name__}: {e}"})
                return None
            if ans is None:
                emit("intervene_note", {
                    "kind": kind, "outcome": "timeout",
                    "waited": round(time.time() - t0, 1),
                    "text": "没有人应答，按原路继续"})
                return None
            return ans

        def _steer(text: str, why: str) -> int:
            """把用户的补充**插进当前消息栈**再继续。

            这是"从断点续跑"的全部秘密：断点本来就在 `messages` 里，
            所以只要追加一条 user 消息，下一轮模型就带着**完整前情 + 新约束**接着干。
            反过来，若把它做成"结束本轮、开新任务"，前情全丢 ⇒ 那才是整轮重来。
            """
            t = (text or "").strip()
            if not t:
                return 0
            messages.append({"role": "user", "content":
                             f"【运行中补充（因{why}暂停问你）—— 就地按它继续，不必重来】\n{t}"})
            return len(t)

        def _ack(kind: str, ans: dict | None) -> bool:
            """处理一次介入答复。True = 就地继续；False = 用户叫停。

            `ans is None`（没人答/超时/通道坏）**一律当"继续"** ——
            降级路径必须是原行为，不许把可用性问题升级成可用性事故。
            """
            if not ans:
                return True
            act = str(ans.get("action") or "continue").strip()
            if act == "stop":
                return False
            if act == "steer":
                n = _steer(ans.get("text") or "", kind)
                emit("intervene_applied", {"kind": kind, "action": "steer",
                                           "chars": n, "step": step})
            return True

        def _stopped(kind: str) -> RunResult:
            _reason = f"用户叫停（{kind} 时选择停止）—— 是未完成，不是失败"
            return RunResult(
                success=False,
                reason=_reason,
                steps=step, usage=total, trace=list(self.registry.trace),
                final=interrupted_final(_reason, step), contract=contract)

        def _snapshot(n: int) -> None:
            """把对话快照**原子写盘** —— 这是"跨进程续跑"的全部本钱。

            为什么必须原子：写到一半被 kill 会留下半个 JSON，而续跑读到坏文件
            比读不到更糟（要么静默丢掉半截对话，要么直接崩）。所以先写 `.tmp`
            再 `os.replace` —— 同一文件系统上的 replace 是原子的。

            为什么目录是实例属性而不是 `Path.cwd()`：`_loop` 也直接被单测调用，
            那时 cwd 是项目根 —— 往那儿写快照会污染仓库。没有 `_snap_dir` 就不落盘。
            """
            d = getattr(self, "_snap_dir", None)
            if not d:
                return
            try:
                tmp = d / "conversation.json.tmp"
                tmp.write_text(json.dumps({
                    "goal": goal, "step": n,
                    "contract": contract.to_dict() if contract else None,
                    "messages": messages, "t": time.time(),
                    # 出清过就必须留痕：续跑者看到的消息栈是**脱水后的**。
                    # 不写这一项，接着跑的人（和下一份归档的评测）会以为那是全量历史
                    # —— 那是把"没看全"渲染成"看过了"（M 型）。
                    "compacted": dict(compact_log),
                    # ★ W28：这一场的**课题身份**也进快照。续跑/追问接回来时，
                    #   "这轮在哪个课题里"必须从**快照**取，不能现场读 `CURRENT`
                    #   —— 后者是跨会话残留，中途被切走就换了课题（`topic_write`
                    #   那段实测事故的同一条根因）。空串 = 本场没有课题。
                    "topic": str(_sess_topic or ""),
                }, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, d / "conversation.json")
            except Exception:
                pass          # 快照是附赠品：写不了也不许打断任务

        # ── 为什么不是 `for step in range(1, max_steps + 1)` ──
        # 因为 `range` 在循环开始那一刻就把上界**固化**了：人在中途说"再给 20 步"
        # 也改不动它。而"步数耗尽"恰恰是三个介入点之一 ——
        # 用 for 就等于声明"这个介入点不可能实现"。
        # 换成 while 后 `max_steps` 始终是可协商的活值。
        step = 0
        while True:
            if step >= max_steps:
                # 步数耗尽**不等于**任务没救：退出前给人一次机会。
                if not asked_limit:
                    asked_limit = True
                    ans = ask("step_limit", {"max_steps": max_steps})
                    act = str((ans or {}).get("action") or "").strip()
                    if act == "extend":
                        add = int(ans.get("steps") or 20)
                        add = max(1, min(add, 200))     # 防手滑填个 100000
                        max_steps += add
                        emit("intervene_applied", {
                            "kind": "step_limit", "action": "extend",
                            "add": add, "max_steps": max_steps})
                        continue
                    if act == "stop":
                        return _stopped("步数耗尽")
                    if act == "steer":
                        # "补一句"在步数耗尽时**必须附带步数**：
                        # 只收下意见却不给执行机会 = 收下意见、然后照样结束 ——
                        # 那是个假动作（M 型：把"没采纳"渲染成"采纳了"）。
                        _steer(ans.get("text") or "", "步数耗尽")
                        max_steps += 10
                        emit("intervene_applied", {
                            "kind": "step_limit", "action": "steer",
                            "max_steps": max_steps})
                        continue
                break
            step += 1
            self._last_step = step      # 崩溃时用它如实报"跑到第几步"
            _snapshot(step)             # 每步落盘一次：崩了最多丢一步
            # 取消：在每步开头检查（不打断正在跑的那次工具调用）
            if cancel is not None and cancel.is_set():
                emit("cancelled", {"step": step})
                _reason = "任务被用户取消（未完成）"
                return RunResult(
                    success=False, reason=_reason,
                    steps=step - 1, usage=total, trace=list(self.registry.trace),
                    final=interrupted_final(_reason, step - 1), contract=contract,
                )
            messages_chars = _messages_chars(messages)
            if warn_chars and messages_chars > warn_chars and not warned_context:
                # **一次任务最多告警一次**：每步都叫会把人训练成无视告警，
                # 那比不告警更糟（"误报比漏报更伤"）。
                warned_context = True
                emit("context_warn", {"chars": messages_chars, "limit": warn_chars,
                                      "step": step})

            def _compact(why: str, target: int) -> dict:
                """出清一次历史，并把这件事**记成事实**。返回那次的统计。

                为什么是个内嵌函数而不是三处 inline：三个触发点（超阈值、token 压力、
                网关真报溢出）必须走同一条路、发同一种事件，否则三处的口径迟早漂移。

                ★ `compact_log` **只记真省到字节的那一次**。以前是无条件写，于是
                  "只空跑过"的一场任务会留下记录却没有对应事件 —— `ctx_econ` 的跨源
                  核对（R6）正好把这形状判成红，一个假警报。
                """
                nonlocal messages, messages_chars, compacted_this_task
                before = _messages_chars(messages)
                # ★ 必须**按名**传（2026-10-15 修）：`context.apply` 的形参是
                #   `(messages, trace, target_chars, keep_live, keep_live_steps, only_levels)`。
                #   这里原来是位置传 `compact_keep_live`（一个**消息条数**，默认 6）——
                #   加了 `keep_live_steps` 之后它落进了**第五个**位置，
                #   于是"保留 6 条"被读成"保留 6 步"，
                #   保护窗口一下放宽 6 倍，出清几乎压不动
                #   （实测：溢出重试那条 25,572 → 25,572，一条没省）。
                #   位置传参遇上形参表变长就是这样安静地错位的 —— 按名传，让它错不了。
                new, stat = context.apply(messages, self.registry.trace, target,
                                          keep_live=compact_keep_live)
                compacted_this_task += 1
                stat.update({"step": step, "why": why, "n": compacted_this_task,
                             "target": int(target), "before_chars": before})
                if stat["freed_chars"] <= 0:
                    # 一次没省下来的出清＝白付一次前缀缓存 miss。**不发假的成功事件**，
                    # 也不写进 compact_log，而是如实说明为什么没动：
                    # 可动区已被 keep_live 保护到底了。
                    emit("context_compact_skipped", stat)
                    return stat
                messages = new
                messages_chars = _messages_chars(messages)
                stat["after_chars"] = messages_chars
                compact_log.clear()
                compact_log.update(stat)
                emit("context_compact", stat)
                return stat

            # ── 出手，而不只是看见 ────────────────────────────
            # `context_warn` 那 20 行此前是**唯一**的响应：发一条事件、什么都不做，
            # 于是历史一路涨到网关回 400、整场判「执行中断」。
            # ★ 迟滞：出清过一次之后，要把触发线抬到"当时体量 + 半个阈值"以上才再动手。
            #   否则只要压不到 target 以下（`keep_live` 护着最近那一批，长任务里这是
            #   常态），**每一步都会再压一次** —— 每压一次付一次前缀缓存 miss，
            #   那是把急刹车当成例行打扫，成本上明确是亏的（实测命中率中位 94.4%）。
            if compact_target and messages_chars > max(compact_target, next_compact_at):
                _st = _compact("over_target", compact_target)
                # **一次决定，两个出口共用**：不管这次省没省到都要抬起触发线。
                # 省不到说明"能动的都动过了"，留着不抬就会每步空跑一遍、每步刷一条
                # `context_compact_skipped` —— 那正是"误报会把人训练成无视告警"那条老账。
                next_compact_at = (int(_st.get("after_chars") or messages_chars)
                                   + max(compact_target // 2, 20_000))

            # token 压力：用网关**实测**回的 input_tokens，不做字数→token 的换算。
            # ★ `step > 1` 是必需的：第 1 步还没有任何一次 chat 发生过，
            #   `prompt_tokens_last` 恒为 0 —— 那不是"网关不回这个字段"，是"还没轮到"。
            #   不挡掉的话每场任务开头都会冒一条假的"闸在降级"（实测就是这么来的）。
            if budget_max_prompt and step > 1:
                if prompt_tokens_last:
                    share = prompt_tokens_last / budget_max_prompt
                    degraded = ""
                else:
                    # 降级口径：拿字符数当分母比"什么都不做"诚实，但必须自报降级。
                    # ★ 一次任务最多说一次：实测漏过这个守卫，于是每一步都发一条
                    #   "闸在降级"的自言自语 —— 噪声会把人训练成无视提示
                    #   （与"误报比漏报更伤"同一条纪律）。
                    share = messages_chars / budget_max_prompt
                    degraded = "no_prompt_tokens"
                    if not warned_degraded:
                        warned_degraded = True
                        emit("budget_note", {
                            "step": step, "degraded": degraded,
                            "text": "网关未回 input_tokens，token 闸降级为字符闸"})
                if share >= cfg.loop.budget_force_ratio:
                    _st = _compact("token_pressure", int(compact_target or 0))
                    # 迟滞线照抬，**三个出口共用一条规则**：不抬的话这一级会在
                    # 每一步都重跑一遍出清 —— 那正是"把急刹车当成例行打扫"。
                    next_compact_at = (int(_st.get("after_chars") or messages_chars)
                                       + max(compact_target // 2, 20_000))
                elif share >= cfg.loop.budget_warn_ratio and not hinted_pressure:
                    hinted_pressure = True
                    messages.append({"role": "user", "content": PRESSURE})
                    emit("budget_note", {"step": step, "share": round(share, 4),
                                         "prompt_tokens": prompt_tokens_last,
                                         "degraded": degraded, "kind": "pressure"})
            left = max_steps - step
            if budget_hint_steps and 0 < left <= budget_hint_steps and not hinted_wrapup:
                hinted_wrapup = True
                messages.append({"role": "user", "content": WRAPUP})
                emit("wrapup_hint", {"step": step, "steps_left": left,
                                     "max_steps": max_steps})
            # 模型调用前先广播，消灭"屏幕上一片死寂"。
            # 网关实测会间歇性长挂起（1.5s 与 247s 交替），没有这个事件时
            # 用户/前端在整段等待里看不到任何东西，会以为它挂了。
            emit("llm_call", {"n": step, "tools_available": len(schemas),
                              "messages": len(messages),
                              "messages_chars": messages_chars,
                              "tools_schema_chars": tools_schema_chars})
            meta: dict = {}
            t_chat = time.time()
            try:
                msg, usage, reasoning = self.llm.chat(messages, tools=schemas, meta=meta)
            except ContextOverflow as e:
                # ── 溢出兜底：此前这是全仓唯一的硬崩路径 ──────────────
                # 400 → LLMError → 主循环不接 → `_run_serial` 兜成「执行中断」，
                # 跑了 N 步的进度当场作废。现在先走满出清阶梯重试**一次**；
                # 仍不行就如实收场，并保住快照（它是可续跑的，别谎称失败）。
                emit("context_overflow", {"step": step, "code": e.code,
                                           "body": str(e.body)[:600]})
                _st = _compact("overflow", 0)
                # 迟滞线照抬（三个出口共用一条规则）。不抬的话：重试成功后下一步
                # `messages_chars` 依旧 > 阈值 ⇒ 又压一次 ⇒ 每一步付一次前缀缓存 miss，
                # 那是把急刹车当成例行打扫 —— 实测归档的命中率中位是 94.4%。
                next_compact_at = (int(_st.get("after_chars") or messages_chars)
                                   + max(compact_target // 2, 20_000))
                try:
                    msg, usage, reasoning = self.llm.chat(messages, tools=schemas,
                                                          meta=meta)
                    emit("context_overflow_recovered", {"step": step})
                except LLMError as e2:
                    _snapshot(step)
                    emit("context_overflow_failed", {"step": step,
                                                     "error": f"{type(e2).__name__}: {e2}"})
                    _reason = (f"上下文溢出，出清后重试仍不可用（{e2}）。"
                               f"**这不是失败**：进度已落快照，可续跑。"
                               f"上游原文：{str(e.body)[:200]}")
                    return RunResult(
                        success=False, reason=_reason,
                        steps=step, usage=total, trace=list(self.registry.trace),
                        final=interrupted_final(_reason, step), contract=contract)
            t_done = time.time()
            # 耗时只算一次，给两个消费者用（事件 + span）——
            # 各算一遍就是"两处真相"，迟早会对不上。
            chat_ms = round((t_done - t_chat) * 1000, 3)
            total = total + usage
            self._run_usage = total      # 唯一的**累加**写点，别名跟着它走
            prompt_tokens_last = int(usage.input_tokens or 0)
            # 调用**结束**了 —— 原实现到这里就断了，模型耗时因此完全不可见。
            # finish_reason 尤其重要：它是唯一能区分"模型说完了"与
            # "被 max_tokens 截断"的字段，缺了它这种静默失败永远查不出来。
            emit("llm_done", {"n": step, "ms": chat_ms, "usage": usage.to_dict(),
                              "cache_ratio": usage.cache_ratio,
                              "finish_reason": meta.get("finish_reason", ""),
                              "response_model": meta.get("response_model", ""),
                              "attempts": meta.get("attempts", 1)})
            if self.telemetry is not None:
                self.telemetry.record_chat(
                    step, t_chat, t_done, usage,
                    finish_reason=meta.get("finish_reason", ""),
                    messages_chars=messages_chars,
                    tools_schema_chars=tools_schema_chars)
            content = (msg.get("content") or "").strip()
            # 思维链单独发一条：界面拿它渲染"思考"折叠块。
            # 它是模型的自述，**不是证据**——证据只认真实的工具调用（gate.py 把关）。
            if reasoning:
                emit("thinking", {"n": step, "text": reasoning[:4000],
                                  "text_chars": len(reasoning), "truncated": len(reasoning) > 4000,
                                  "tokens": usage.reasoning_tokens})
            emit("step", {"n": step, "usage": usage.to_dict(),
                          "content": content[:4000], "tools_available": len(schemas)})

            tool_calls = msg.get("tool_calls") or []

            # ── 没有工具调用：不是完成就是空转 ──
            if not tool_calls:
                messages.append({"role": "assistant", "content": content})
                empty_rounds += 1
                if empty_rounds >= 2:
                    messages.append({"role": "user", "content": NUDGE})
                    emit("nudge", {"after_rounds": empty_rounds})
                    empty_rounds = 0
                continue

            empty_rounds = 0
            messages.append(msg)  # 必须原样保留（含 tool_calls），否则下一轮 API 报错

            delivered: tuple[CompletionClaim, Verdict] | None = None
            blocked: dict | None = None

            for tc in tool_calls:
                name, args, tcid, parse_err = _unpack(tc)
                # ★ 置信度门控回退：模型点名了本轮未暴露、但注册表里真实
                #   存在的工具 ⇒ 放开一次、记事件，之后整场保持放开。
                #   这不是「每步变形」：只在真的点名时扩张一次，前缀缓存
                #   只 miss 一次。真查无此工具的名字仍由 registry 如实拒绝。
                _expanded = scope_fallback(schemas, self.registry, name)
                if _expanded is not None and hasattr(schemas, "append"):
                    schemas.append(_expanded)
                    emit("tool_scope_expanded", {
                        "tool": name, "tools_available": len(schemas),
                        "frozen": True,
                        "reason": "模型点名了本轮未暴露、但注册表里真实存在的工具",
                    })

                if parse_err:
                    # ★ 参数没能解析 ⇒ **这一次调用整体作废**，`deliver_task` /
                    #   `report_blocker` 也不例外。两个理由：
                    #   ① 一致性：四个分支里只有这一个能让"参数坏了"**穿透**到
                    #      后面 —— 空 args 交给 `CompletionClaim.from_args()`
                    #      会变成一份"没有证据的交付声明"，报回的是
                    #      「证据不足」，而那又是一句**假话**（模型可能写了证据，
                    #      只是 JSON 断了）。口径不许在各分支各说一套（H 型）。
                    #   ② 安全：不要求必填参数的工具会**真的拿 `{}` 跑起来**。
                    #   照样过一遍 `registry.call`，是为了让它**进台账**。
                    emit("tool_start", {"seq": self.registry.next_seq, "name": name,
                                        "args": args})
                    call = self.registry.call(name, args, parse_error=parse_err,
                                              tcid=tcid)
                    emit("tool", call.to_dict())
                    messages.append({
                        "role": "tool", "tool_call_id": tcid,
                        "content": _observation(call, cfg.loop.max_observation_chars),
                    })
                    continue

                if name == "deliver_task":
                    claim = CompletionClaim.from_args(args)
                    verdict = self.gate.check(claim, contract)
                    emit("deliver_attempt", {
                        "passed": verdict.passed,
                        "reasons": verdict.reasons,
                        "warnings": verdict.warnings,
                        "writing": verdict.writing,
                        "lint": verdict.lint,
                        "summary": claim.summary,
                        "evidence": [e.__dict__ for e in claim.evidence],
                    })
                    messages.append({"role": "tool", "tool_call_id": tcid,
                                     "content": verdict.feedback})
                    if verdict.passed and delivered is None:
                        delivered = (claim, verdict)
                    elif not verdict.passed:
                        # **第 1 次打回不问人** —— 留给模型自己修。
                        # 实测里同一任务会被连打回 3 次，第 2 次才值得问：
                        # 第一次就问等于"它还没试过就先怀疑它"，只会制造噪音，
                        # 而噪音训练人无视提示（"误报比漏报更伤"）。
                        gate_rejects += 1
                        if gate_rejects >= 2:
                            ans = ask("gate_reject", {
                                "rejects": gate_rejects,
                                "reasons": list(verdict.reasons or []),
                                "summary": claim.summary,
                            })
                            if not _ack("gate_reject", ans):
                                return _stopped("交付被证据门反复打回")

                elif name == "report_blocker":
                    blocked = args
                    emit("blocker", args)
                    messages.append({"role": "tool", "tool_call_id": tcid,
                                     "content": "已记录该阻塞报告，任务将以「阻塞」结束。"})

                else:
                    # 先广播"开始"，再真正调用 —— 前端才有**真实的运行态**。
                    # 原实现只在调用**结束之后**发一次 tool 事件，于是前端
                    # data-st="run" 的扫光动画成了永不触发的死代码，
                    # 用户也看不到"它正在检索"这个过程，只看到结果逐条冒出来。
                    emit("tool_start", {"seq": self.registry.next_seq, "name": name,
                                        "args": args})
                    call = self.registry.call(name, args, tcid=tcid)
                    emit("tool", call.to_dict())
                    obs = _observation(call, cfg.loop.max_observation_chars)
                    if self.registry.repeated_call(call, cfg.loop.max_identical_calls):
                        obs += (
                            f"\n\n⚠️ 收敛告警：同一工具+同一参数已重复 "
                            f"{cfg.loop.max_identical_calls} 次，判定为原地打转。\n"
                            "请立刻换策略；若确实无法推进，调用 report_blocker 说明卡点。"
                        )
                        emit("stall", {"seq": call.seq, "tool": call.name})
                        # 打转是"再跑也是白跑"的典型：同一参数已经敲了 N 次门。
                        # **同一个 seq 只问一次** —— 否则它继续打转、我们继续问，
                        # 就把"帮人"做成了"烦人"。
                        if call.fingerprint not in asked_stalls:
                            asked_stalls.add(call.fingerprint)
                            ans = ask("stall", {"tool": call.name, "seq": call.seq,
                                                "repeat": cfg.loop.max_identical_calls})
                            if not _ack("stall", ans):
                                return _stopped("检测到原地打转")
                    messages.append({"role": "tool", "tool_call_id": tcid, "content": obs})

            if blocked is not None:
                _reason = f"模型报告阻塞：{blocked.get('reason', '')}"
                return RunResult(
                    success=False, reason=_reason,
                    steps=step, usage=total, trace=list(self.registry.trace),
                    final=interrupted_final(_reason, step), contract=contract,
                )
            if delivered is not None:
                claim, verdict = delivered
                draft = self._draft_for_display(claim)
                return RunResult(
                    success=True, reason=verdict.feedback, steps=step, usage=total,
                    trace=list(self.registry.trace),
                    final=_compose_final(claim, draft,
                                         files=self._written_files(Path.cwd())),
                    contract=contract, verdict=verdict,
                    draft=draft, lint=verdict.lint,
                )

            if step % remind_every == 0:
                messages.append({"role": "user", "content": _reminder(contract)})
                emit("remind", {"step": step})

        _reason = (f"达到最大步数 {max_steps} 仍未通过证据门"
                   f"（是未完成，不是失败——可加大步数重跑）")
        return RunResult(
            success=False, reason=_reason,
            steps=max_steps, usage=total, trace=list(self.registry.trace),
            final=interrupted_final(_reason, max_steps), contract=contract,
        )
