# -*- coding: utf-8 -*-
"""阶段 → 专属技能（一对一，不再整合）。

## 为什么要改

工作台原来的技能是"把 GitHub 上好几套整合成一套"。后果是**五个技能抢同一件事**：

    nature-writing / paper-spine / scientific-writing /
    write-scientific-manuscript / paper-staged-gen

全都声称"写论文"。实测代价（README「写作技能门实测」）：模型不知道该听谁，
为一个 120 字的引言段落**一口气读了 4 个技能**，输入 token 从 4.9k 撑到 11k。

现在改成：**每个阶段有且只有一个专属技能**。
全稿拼接完 → `nature-polishing` 润色；引用核验 → `nature-ref-verifier`；
配图 → `nature-figure`。不再存在"通用写作技能"这种谁都管、谁都不精的东西。

## 另外修一个真问题

工作台的 `SKILLS_ROOTS` 只扫三处（`~/.zcode/skills` 的 8 个旧副本、`~/.dsh/skills`、
引擎自带 skills/），**扫不到 `~/.agents/skills` —— 那里才是 21 个真库**。
所以荣珂自带技能索引与读取，不依赖工作台那三个根。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .pitfalls import DESCRIPTION as _PITFALL_DESC
from .pitfalls import DOCTRINE as _PITFALL_DOCTRINE
from .pitfalls import SKILL_NAME as PITFALL_SKILL
from .pitfalls import PRODUCTION_SIGNALS

# 技能根，按优先级排列（先命中者胜）。
# `~/.agents/skills` 排第一：那是分阶段的真库，其余是旧副本或引擎自带。
#
# ★ 第一项 `_BUNDLED_SKILLS`（2026-10-22 加，2026-11-05 改口径）：
#   **开发机本地的第三方技能副本，不随开源发行件分发**。
#
#   原设计是「随产品分发的技能」，理由成立：product 的能力不能建立在
#   「用户机器上恰好装了某个 GitHub 项目」之上 —— 那种设计在开发机上永远
#   看起来是绿的，在客户机上从来跑不起来。
#
#   但 2026-11-05 框架改用 Apache-2.0 开源后，这份副本不能再随包走：
#   它是 CC BY-NC-SA 4.0（禁止商用 + 相同方式共享），与「开源框架 +
#   专有商业软件」双轨冲突。于是撤出版本库索引（`git rm --cached`，
#   本地文件保留仅供回归），改由 `_PACKAGED_SKILLS` 提供**自研**的
#   Apache-2.0 最小技能随 wheel 分发。本机有第三方副本时仍优先用它，
#   clone 下来的干净机器自然落到自研那份。
#
#   所以 PaperSpine 这类"流程骨架"技能的落点是**仓内** `vendor/skills/`，
#   随包一起发。用户自己 `~/.agents/skills` 里若有同名目录，那是他自己
#   要覆盖 —— 但他得先删掉仓内那份才排得上，**默认以产品自带的为准**。
#   这条取舍是有意的：产品的能力基线不许被客户机的残留状态改写。
_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_SKILLS: Path = _ROOT / "vendor" / "skills"
#: 随 **开源发行件** 分发的自研技能（Apache-2.0）。落在包内而不是仓库根，
#: 这样 `pip install` 之后 `stages.available()` 一定扫得到 —— 不依赖
#: 「clone 完整仓库」或「本机装了某个技能库」。见 `pyproject.package-data`。
_PACKAGED_SKILLS: Path = Path(__file__).resolve().parent / "skills"
_EXTERNAL_SKILLS: tuple[Path, ...] = (
    _ROOT / "vendor" / "Supervisor.skill",
    # ★ 2026-10-31：`tavotto-figure` 已**内化进仓内** `vendor/skills/`（随包分发），
    #   不再从 `vendor/Tavotto/codex-plugin/skills` 取。
    #   为什么要内化：上游 `vendor/Tavotto` 是整份第三方源码 —— 111MB / 5139 个文件，
    #   而荣珂真正用到的只有 `codex-plugin/skills/tavotto-figure` 这 141KB。
    #   按本文件开头的原则（产品能力不能建立在「用户机器上恰好装了某个 GitHub 项目」
    #   之上），它本来就该走 `_BUNDLED_SKILLS`，而不是外部技能。
    #   交接仍走官方脚本 `scripts/handoff.py`，它只依赖标准库与同目录 `update_check`，
    #   找的是**用户机上已安装的 Tavotto 桌面版**，与这份源码树无关。
)

SKILL_ROOTS: tuple[Path, ...] = (
    _BUNDLED_SKILLS,
    _PACKAGED_SKILLS,
    Path.home() / ".agents" / "skills",
    Path.home() / ".zcode" / "skills",
    Path.home() / ".dsh" / "skills",
    Path.home() / ".dsh" / "papers" / "workbench" / "skills",
)

# ── 内核原生技能 ────────────────────────────────────────────
# 不落磁盘、不进用户的技能库：内容跟着代码走版本，不会因为目录被清理而消失。
# 见 `pitfalls.py` 的开头说明。
KERNEL_SKILLS: dict[str, dict] = {
    PITFALL_SKILL: {
        "name": PITFALL_SKILL,
        "dir": PITFALL_SKILL,
        "path": "",                       # 内核原生，没有磁盘路径
        "source": "kernel",
        "description": _PITFALL_DESC,
        "bytes": len(_PITFALL_DOCTRINE),
    },
}


# ═════════════ 演示文稿（deck）意图的唯一词表 ═════════════
# ★ 2026-10-21：这张表原来在 `contract.py` 里叫 `_DECK_STRONG`，而本文件
#   slides 阶段另有一份自己的触发词 —— **两份手抄副本，口径相反**：
#   contract 那份写明了「刻意不收光秃秃的『组会』『汇报』，它们太常见」，
#   stages 这份却收了裸词「汇报」「组会」。
#   后果不是学术性的：一次「写这个月的报告，汇报一下菌株的照片」被判出 slides 阶段，
#   `loop.py` 按「阶段含 slides ⇒ 挂 ppt 域」把 37 个 ppt 工具挂给模型，
#   模型看到 `deck_template_plan` 就去做 PPT（原话："Also PPT stage in skills list"），
#   而它要的《月度报告.md》已写盘 5426 字节、却被记成失败。
#   ⇒ 词表只留**一份**，住在这里（stages 是更底层的模块，contract 已经 import 它）。
#   三处用途（挂载 ppt 域 / gate 第 9 条硬门 / slides 阶段）都读它。
#   `contract._DECK_STRONG` 保留为别名，老引用不断。
#   取舍与 contract 那一份原文一致：**窄**。只收"要一份演示文稿"的明确说法，
#   不收"向谁交代这件事"的说法。
_DECK_WORDS: tuple[str, ...] = (
    "ppt", "pptx", "幻灯片", "演示文稿", "幻灯", "slides", "slide deck",
    # ★ 2026-10-21 补「deck」裸词：它本身就是一个**交付形态**的专名
    #   （「出一份汇报 deck」「能上组会的 deck」），不是"汇报"那种通用动词。
    #   实测漏判过一句「给课题出一份能上组会的汇报 deck」。与「汇报」的区别：
    #   deck 是名词、指那份东西；汇报是动词、只说明意图。词表窄的口径保的是
    #   "别把'汇报一下进展'判成要 PPT"，而不是"别认 deck"。
    "deck", "keynote", "prezi", "宣讲片", "路演材料", "做图汇报",
    "组会汇报", "汇报幻灯片", "汇报ppt", "组会ppt", "答辩ppt", "论文汇报",
    "make slides", "make a deck", "presentation deck",
)


@dataclass(frozen=True)
class Stage:
    """一个阶段。skill 是该阶段的**唯一**专属技能。"""
    id: str
    name: str
    skill: str
    triggers: tuple[str, ...]
    purpose: str

    @property
    def order(self) -> int:
        return STAGE_ORDER[self.id]


# 流水线顺序 = 列表顺序。润色排在起草之后，因为**全稿拼接完才轮到它**。
STAGES: tuple[Stage, ...] = (
    # ← 排第一：它是"开工前"的动作，不是流水线上的一道工序。
    #    为什么做成 Stage 而不是另一个概念：这样**门会自动硬拦没装载**（既有的阶段技能门
    #    逐条核对 contract.stages），不用再写一套并行机制。触发词只收成篇稿件生产的信号，
    #    见 pitfalls.py 里"刻意不收论文/综述"的理由。
    Stage("discipline", "作业纪律", PITFALL_SKILL,
          PRODUCTION_SIGNALS,
          "开工前过一遍实测踩坑清单 —— 30 条坑没有一条会报错："
          "退出码 0、门禁全绿，而产物是错的"),
    Stage("ideate", "想法评估", "idea-evaluator",
          ("值不值得做", "这个想法", "idea 评估", "评估一下这个思路",
           "五维", "更高更快更强", "致命伤", "颠覆式创新", "范式转移",
           "能做成一篇文章吗", "有没有搞头", "值不值一个顶会"),
          "动手之前先评这个想法：五维打分 + 致命伤审查 + 范式转移探针"),
    Stage("plan", "选题与方案", "evidence-graded-research-protocol",
          ("技术路线", "选题", "研究缺口", "有依据的方案", "research gap"),
          "把课题拆成有文献依据的技术路线"),
    Stage("search", "文献检索", "nature-academic-search",
          ("检索", "查文献", "找文献", "文献调研", "搜集文献", "文献综述"),
          "多源检索与文献管理"),
    Stage("fulltext", "全文获取", "nature-downloader",
          ("下载全文", "获取全文", "知网", "全文获取", "下载文献", "取全文",
           "下全文", "补文献", "全文没拿到"),
          "合法拿到全文与附件"),
    Stage("read", "精读", "nature-paper-card",
          ("精读", "读论文", "方法拆解", "证据链分析"),
          "把一篇论文拆成方法/证据/局限"),
    Stage("translate", "中英对照精读", "nature-reader",
          ("中英对照", "全文翻译", "对照阅读"),
          "带图表公式对齐的双语精读"),
    Stage("cite", "补引用", "nature-citation",
          ("补引用", "支撑文献", "配文献", "claim 对接"),
          "给每一句断言配上可核验的来源"),
    Stage("verify", "引用核验", "nature-ref-verifier",
          ("核验", "验证", "查核", "撤稿", "参考文献", "引用是否", "doi"),
          "逐字段核验参考文献真伪"),
    # ── 大纲（2026-10-22 新增，用户点名的流程顺序）─────────────────────
    # 位置：**起草之前**、核验之后。为什么必须是独立一道工序而不是塞进 draft：
    #   「先定每章写什么、引哪些文献」与「把段落写出来」是两种不同的活。
    #   混在一步里做，模型会在写第三节时改第二节的论点 —— 那正是用户说的
    #   「多次返工出来的一般很垃圾」的成因：结构与内容在同一遍里来回改。
    #   冻结大纲 + 章-文献映射之后，写作阶段只剩"按已定结构填段落"。
    #
    # 技能用 `paper-spine`（PaperSpine 的宿主投影），落点是**仓内** `vendor/skills/`，
    # 随产品分发 —— 见 `SKILL_ROOTS` 上方那段。
    #
    # 触发词口径：只收"要定结构"的说法。**刻意不收裸「综述」** ——
    #   裸「综述」归 draft（写一篇综述的主体动作）。把裸词也收进来会让
    #   每一篇要写综述的任务都被硬性要求先跑一遍大纲门，包括那种
    #   用户已经给好提纲、只想让它填字的情形。
    Stage("outline", "大纲与章-文献映射", "paper-spine",
          ("大纲", "提纲", "章节安排", "每一章写什么", "论文骨架", "贡献闸门",
           "outline", "paper spine", "paperspine"),
          "冻结每章论证、要引的文献与图表清单，之后只许填充不许改结构"),
    # ★ 2026-10-22：**figure 提到 draft 之前**（原来是 draft → polish → stats → figure）。
    #   用户的原话是「首先需要先根据知识库进行图表绘制，防止后面遗漏，再根据知识库
    #   挂载各个阶段 skill 进行分段写作」。理由很实际：写完再补图，必然漏。
    #   实测依据：上一轮综述真跑（runs/20261015-092001）契约推断出
    #   `search, verify, draft`，`needs_figure=False`，最后**一张图都没有**。
    #   顺序即指令（`plan_text()` 逐条编号念给模型），排在后面就等于没让它做。
    Stage("figure", "配图", "nature-figure",
          ("配图", "绘图", "图表", "多面板图", "figure"),
          "出版级科研配图 —— **先于正文**，防止写完再补图时漏掉"),
    Stage("benchmark", "Benchmark 论文", "benchmark-paper-template",
          ("benchmark 论文", "评测论文", "数据集论文", "构建一个 benchmark",
           "评测框架", "五柱"),
          "Benchmark/Evaluation 类论文的五柱骨架与六段 Introduction 逻辑链"),
    Stage("draft", "起草写作", "nature-writing",
          ("写一篇", "写一段", "撰写", "起草", "改写", "综述", "引言", "讨论",
           "正文", "章节", "投稿材料", "cover letter"),
          "起草与重构章节论证（按已冻结的大纲分章写，不重排结构）"),
    # ← 用户点名的那个：全稿拼接完，用它润色
    Stage("polish", "全稿润色", "nature-polishing",
          ("润色", "精简", "语言", "翻译", "polish"),
          "全稿拼接完成后统一润色/翻译"),
    # ★ 2026-10-26：**辅助层**，不是主技能。
    #   用户的原话是「乙派，目前最好的还是 nature skill 和 paperspine，
    #   以他们为主，别的为辅助」。
    #   所以这一格的触发词**刻意只收"姿态"说法**，不收任何起草/润色词
    #   （「写一篇」「润色」「精简」都不在这里）——那些归 draft / polish。
    #   它只在「稿子已经写出来了，但读起来在跟假想审稿人吵架」时接活。
    #   排在 polish 之后：因为它的改动**只该发生在语言已经通顺之后**，
    #   否则会出现"删了防御句但句子还不通"的半成品。
    #
    #   判据见 `_w34_route.py`：5 条起草/结构/润色/PPT/检索任务里
    #   本阶段必须**一次都不出现**（负控），否则就是把主技能的活抢走了。
    Stage("posture", "写作姿态", "anti-defensive-writing",
          ("防御性写作", "心虚", "语气太虚", "太虚", "老在解释",
           "自我削弱", "不适当地示弱", "审稿人还没问",
           "防御腔", "审计腔", "过度免责", "免责腔"),
          "去掉防御性姿态：只改姿态、不删事实；主张强度不得超过证据"),
    # ★ 2026-10-26：新增「实验设计」——补的是**实验开始之前**这一整段空白。
    #   为什么必须有这一格：原来的流水线从「选题」直接跳到「统计审查」，
    #   而 stats 管的是**已收集数据**的事后审查（实验单位、重复、不确定度）。
    #   「样本量该多大、怎么随机化、怎么区组、几个重复才不算伪重复」
    #   落在没人管的真空里 —— 对生物医药课题这是最要命的一类缺失：
    #   样本量算错，后面整条链路都白做。
    #
    #   排在 stats **之前**：设计定下来才谈得上审查设计是否合规。
    #
    #   ⚠ 触发词纪律：`样本量` 原来挂在 stats 上，本轮**移到这里**。
    #     stats 收的是「已经做完了、看统计方法对不对」的说法，
    #     design 收的是「还没做、问要多少样本/怎么分组」的说法。
    #     两个阶段抢同一个词，模型就会把"要多少个样本"路由去写 Data
    #     Availability 声明 —— 见 `_w35c_route.py` 的三条负控。
    Stage("design", "实验设计", "experimental-design",
          ("实验设计", "试验设计", "分组方案", "怎么分组", "分到各组",
           "怎么分到", "随机化", "区组", "析因", "因子设计", "因子想筛",
           "正交试验", "响应面", "用什么设计", "怎么设计这个实验",
           "怎么避免混杂", "混杂", "对照组", "对照组怎么设", "空白对照"),
          "实验开始之前：随机化/区组/对照/因子设计/重复与伪重复"),
    # ★ 与 design 分开的理由：design 定「怎么摆这个实验」，
    #   power 定「要多少样本才看得见效应」。两件事的输入完全不同 ——
    #   前者要因子与水平，后者要效应量与容许的 α/β。
    #   合用一格会让模型拿"分组方案"去回答"多少样本"，反之亦然。
    Stage("power", "样本量与功效", "statistical-power",
          ("样本量", "需要多少样本", "要多少样本", "多少个样本", "多少只",
           "多少例", "多少重复", "几个重复", "伪重复", "功效分析",
           "统计功效", "检验效能", "power analysis", "最小可检测", "mde",
           "样本量依据"),
          "先验样本量与功效：闭式解或 Monte Carlo，可反算最小可检测效应"),
    Stage("units", "量纲与不确定度", "uncertainty-and-units",
          ("不确定度", "误差范围", "有效数字", "量纲", "单位换算",
           "数量级合理", "±", "标准差怎么报", "扩展不确定度",
           "gum", "误差传播"),
          "量纲检查与测量不确定度传播（GUM / Monte Carlo）"),
    Stage("stats", "统计审查", "nature-statistics",
          ("统计审查", "显著性", "p 值", "统计方法", "统计报告",
           "检验方法选对了吗", "多重比较", "校正"),
          "实验单位/重复/不确定度审查"),
    Stage("data", "数据可用性", "nature-data",
          ("数据可用性", "数据共享", "数据仓库", "fair"),
          "Data/Code Availability 声明"),
    Stage("review", "模拟审稿", "nature-reviewer",
          ("模拟审稿", "预审", "审稿人视角", "投稿前自审"),
          "投稿前的证据本位自审"),
    Stage("response", "审稿回复", "nature-response",
          ("审稿意见", "返修", "rebuttal", "逐点回复", "回复审稿"),
          "逐点回复审稿意见"),
    Stage("proposal", "研究计划", "nature-proposal-writer",
          ("研究计划", "开题报告", "申请书", "标书"),
          "开题/申请书的论证与预算"),
    Stage("patent", "专利", "nature-paper-to-patent",
          ("专利", "交底书", "发明"),
          "论文转专利交底书"),
    Stage("slides", "汇报 PPT", "nature-paper2ppt",
          # ★ 2026-10-21：触发词改为**引用**唯一词表 `_DECK_WORDS`，不再另抄一份。
          #   撤掉的裸词「汇报」「组会」说的是"向谁交代这件事"，不是"要一份演示文稿"：
          #   实测 12 句常见说法里 slides 阶段误判 6 句，全部来自这两个词
          #   （「汇报一下检索结果」「整理一下组会记录」「汇报一下菌株的照片」…）。
          #   完整事故链见 `_DECK_WORDS` 上方那段。
          _DECK_WORDS,
          "论文转学术汇报"),
    Stage("log", "实验日志", "nature-experiment-log",
          ("实验日志", "实验记录"),
          "标准化实验记录"),
    # ══ 2026-10-26：接 Supervisor-Skills（鸭博士）补齐三处真空 ═══════════
    #
    # 用户要「用鸭博士 skill 升级模块」。12 个技能不是全接 —— **只接那些
    # 现有流水线里真没有对应工序的**，其余留在导师模式的方法论层按需装载。
    # 判据是：接进来的每一格必须填一个"原来没人管"的洞；已经有主技能管的
    # 领域（检索=draft/polish、写作=paper-spine、配图=nature-figure…）
    # **一格都不许加**，否则就是抢主技能的活（见 posture 那格上方那段纪律）。
    #
    # ⚠️ 位置纪律：`Stage.order` 取自 `STAGE_ORDER`（= 列表下标），
    #    而 `plan_text` 是**按顺序逐条编号念给模型**的 —— 顺序即指令，
    #    排错了模型就会先写稿再想这个想法值不值得做。所以每一格必须
    #    插在它**语义上该在的位置**，不许图省事追加到列表末尾。
    #    这条我自己踩过一次：初版把三格全追加在 `log` 之后，
    #    结果 `ideate` 排到了第 24 位、在 `plan` 之后，与它"发生在选题之前"
    #    的定义直接矛盾（`_w39_accept.py` 的顺序判据当场抓出）。
    #
    # ── 三处真空的判据 ────────────────────────────────────────────
    # ① `ideate`：原来的第一格是 `plan`（选题与方案），但 plan 的触发词是
    #    「技术路线/选题/研究缺口」—— 它假设**你已经有课题了**，只是要把它拆成
    #    路线。而"这个想法本身值不值得做"发生在它**之前**，全仓没人管。
    #    实测口径：问「这个想法能不能做成一篇顶会」不会命中任何 stage。
    # ② `benchmark`：`tech-paper-template` 的活其实由 `paper-spine` 覆盖了
    #    （章节骨架），所以**不接 tech-paper-template**。但 benchmark/evaluation
    #    类论文有它自己的五柱结构（研究缺口/构建流水线/评测框架/实证/…），
    #    与"技术贡献型"论文的骨架不通用，paper-spine 的通用骨架答不了它。
    # ③ `submit`：`review` 收的是「模拟审稿/审稿人视角」，是**证据本位**的
    #    内容审查；投稿前的**形式面**（宏观逻辑串不串、LaTeX 格式、图面、
    #    引注完整性、语法）没有任何一格覆盖。两者不合并：内容审查说"这个结论
    #    证据不足"，形式审查说"第 3 节的图在第 5 节才被提到"——不同工种。
    #
    # ★ 触发词纪律（这条最容易踩）：**只收现有 stage 没有的说法**。
    #   我把新增词逐个对着已有 STAGES 查过一遍，"不能出现第二个 stage 也命中"
    #   由 `_w39_route.py` 的正/负控守着。
    Stage("submit", "投稿前形式自审", "pre-submission-reviewer",
          ("投稿前检查", "投稿前过一遍", "形式审查", "格式检查", "latex 格式",
           "引注完整性", "提交前自查", "camera ready", "预投稿检查"),
          "交出去之前的最后一遍：宏观逻辑/写作细节/语法/格式/图面/引注完整性"),
)

STAGE_ORDER: dict[str, int] = {s.id: i for i, s in enumerate(STAGES)}
STAGE_BY_ID: dict[str, Stage] = {s.id: s for s in STAGES}
STAGE_BY_SKILL: dict[str, Stage] = {s.skill: s for s in STAGES}


# ── 阶段推断 ────────────────────────────────────────────────
# 否定语境标记。**必须处理「不」**：用户明写「不需要检索文献」时，
# 关键词「检索」照样命中，契约就会硬性要求装载检索技能 ——
# 于是模型被自己的证据门逼着去做一件用户明令禁止的事。
# 实测（2026-09-18 写作引言任务）：`不需要检索文献` → 误判出 search 阶段。
_NEGATIONS = ("不需要", "不须要", "无需", "无须", "不必", "不用", "不做", "不进行",
              "不要求", "不要", "禁止", "严禁", "勿", "免于", "没有要求", "不作")
# 紧贴触发词之前的单个否定字。必须收 —— 「不下载全文」里，触发词「下载全文」
# 前面只有一个「不」，多字标记表匹配不到（实测：fulltext 阶段仍然被误判出来）。
# 只判**紧邻**是为了不误伤「不断检索」「不仅检索」这类。
_NEG_TAIL = ("不", "未", "没", "勿", "免", "非", "无")


def _only_negated(blob: str, kw: str, window: int = 10) -> bool:
    """kw 是否**只**出现在否定语境里（任何一处非否定出现即返回 False）。"""
    idx = blob.find(kw)
    if idx < 0:
        return False
    while idx >= 0:
        before = blob[max(0, idx - window):idx]
        if not (before.endswith(_NEG_TAIL) or any(n in before for n in _NEGATIONS)):
            return False
        idx = blob.find(kw, idx + 1)
    return True


def infer_stages(*texts: str) -> list[Stage]:
    """从目标文本推断本任务包含哪些阶段。按流水线顺序返回。

    只认**明确的**触发词，不做联想——宁可少判（那就少装一个技能），
    不滥判（滥判会让模型装一堆用不上的技能，正是要修的老毛病）。
    触发词若只出现在否定语境（"不需要检索"），一律不算命中。
    """
    blob = " ".join(t or "" for t in texts).lower()
    hit = [s for s in STAGES
           if any(k in blob and not _only_negated(blob, k) for k in s.triggers)]
    return hit


def plan_text(stages: list[Stage], available: set[str] | None = None) -> str:
    """给模型看的阶段技能协议。"""
    if not stages:
        return ""
    lines = [
        "═══ 阶段专用技能（内核强制，一对一）═══",
        "本任务包含以下阶段。**每个阶段有且只有一个专属技能**，进入该阶段前必须用",
        "`load_skill` 装载它的全文，然后按它的协议做。凭记忆模仿不算数。",
        "",
    ]
    for i, s in enumerate(stages, 1):
        warn = ""
        if available is not None and s.skill not in available:
            warn = "   ⚠️ 该技能未在本机找到，请如实报告而不是假装装载"
        lines.append(f"  {i}. {s.name:<10} → load_skill(\"{s.skill}\")")
        lines.append(f"     用途：{s.purpose}{warn}")
    lines += [
        "",
        "顺序即流水线顺序：**润色排在起草之后**——全稿拼接完成才轮到它，",
        "不要边写边润色，也不要跳过起草直接润色。",
    ]
    # ── 综述/成篇稿件的**结构冻结**纪律（2026-10-22 加）─────────────────
    # 为什么这一条必须单独写：用户的原始诉求是「不要多次返工，多次返工出来的一般
    # 很垃圾」。返工的机制成因是**结构与内容在同一遍里来回改** —— 写到第三节发现
    # 第二节的论点不对，回头改第二节，第三节又得重写。把大纲与章-文献映射**冻结在
    # 起草之前**，写作阶段就只剩"按已定结构填段落"。
    _ids = {s.id for s in stages}
    if "outline" in _ids:
        lines += [
            "",
            "★ 结构与内容**分两遍**做，不许混：",
            "  ① 先冻结大纲：落盘 `outline.md`，每章写清——一句话论证 / 要引哪些",
            "     `doc_id` / 本章目标字数 / 本章图表。**章-文献映射没写全就不要动笔**。",
            "  ② 再按章填段落。写作阶段**只许填充，不许重排结构**；",
            "     确实要改章序或增删章，先改回 `outline.md` 并说明理由，再继续。",
            "  这样做的理由：写到第三节才发现第二节论点要改，会连带重写后文 ——",
            "  这正是「多次返工产出很垃圾」的机制成因。",
        ]
    # ── 先图后文（2026-10-22，用户点名的顺序）──────────────────────────
    # 顺序即指令：`plan_text` 是逐条编号念给模型的，排在后面就等于没让它做。
    # 实测依据：改成这个顺序之前，上一轮综述真跑（runs/20261015-092001）
    # 契约里 `needs_figure=False`，最后一张图都没有，而"图该在哪一节"从没被规划过。
    if "figure" in _ids and "draft" in _ids:
        lines += [
            "",
            "★ **图先于正文**（这条顺序是刻意的，不要颠倒）：",
            "  在写正文之前，先按大纲把每一章该有的图/表**画出来并落盘**。",
            "  理由：写完再补图必然漏 —— 写作时脑子里装的是论证，不是版面。",
            "  做法：逐章确认「本章要不要图/表」，要就当场画 + 跑 `audit_figure`；",
            "       不要的**也要显式写明理由**（写进大纲或成稿的说明里）。",
            "       静默地一章都没有图，就是这一条没执行。",
        ]
    # ── 长文阶段的**分节交付**纪律（2026-10-10 W28 加）────────────────────
    # 起因是一次真实开题报告：一次成文的跑法在 70 步耗尽时只写完前三节，
    # 加预算到 110 步才补上四、五节 —— 报告是一半一半写出来的。
    # 根因不是模型懒，是**任务形态**：让它一次检索 + 一次成文，
    # 「查证」与「写作」在同一个 go 里争步数，必然有一头被截断。
    # 正确形态是把长文切成**可独立完成的节**，每节查证→落盘→再进下一节，
    # 这样任何一步被截断，已完成的节**已经在盘上**（而不是只在上下文里）。
    _LONG = {"proposal", "draft", "review", "response", "outline"}
    if any(s.id in _LONG for s in stages):
        lines += [
            "",
            "★ 长文按**节**推进，不要憋一稿：",
            "  这类任务（开题/起草/审稿/回复）篇幅大，一次成文会在步数耗尽时",
            "  只写出一半，而且没写的那半**只存在于上下文里、不在盘上**。",
            "  做法：**每完成一节就落盘**——契约指定了产物文件名（如 `综述.md`）时，",
            "       用 `write_file(path=..., append=true)` 追加这一节；",
            "       没指定文件名时用 `topic_write(doc=..., mode=append, slug=...)`。",
            "       再进下一节。任何时刻被截断，已完成的节都已落盘、可续写。",
            "  续写时先 `read_file` / `topic_status` 读回已写了什么，",
            "       **从断点接着写，不要重写前文**。",
            "  每节的查证也要克制：库存文献是已知的，需要哪篇直接 `kb_read` 那几页，",
            "       不要为了「查全」把全库扫一遍 —— 检索本身会吃掉写作用掉的步数。",
        ]
    lines += [
        "════════════════════════════════════════════",
    ]
    return "\n".join(lines)


# ── 技能索引与读取（荣珂自带，不依赖工作台的三个根）──────
_FM = re.compile(r"^---\s*$(.*?)^---\s*$", re.S | re.M)


def _frontmatter(md: str) -> tuple[str, str]:
    """取 (name, description)。兼容 `description: >-` / `|` 块标量。"""
    m = _FM.search(md)
    block = (m.group(1) if m else md[:4000])
    name = ""
    nm = re.search(r"^name:\s*(.+)$", block, re.M)
    if nm:
        name = nm.group(1).strip().strip("\"'")
    desc = ""
    dm = re.search(r"^description:\s*([>|]-?)\s*\n((?:[ \t]+.*\n?)*)", block, re.M)
    if dm:                                    # 块标量：合并后续缩进行
        desc = " ".join(x.strip() for x in dm.group(2).splitlines() if x.strip())
    else:
        dl = re.search(r"^description:\s*(.+)$", block, re.M)
        if dl:
            desc = dl.group(1).strip().strip("\"'")
    return name, desc


@lru_cache(maxsize=1)
def index() -> dict[str, dict]:
    """扫全部技能根建索引。同名技能先命中的根胜出（`.agents/skills` 优先）。

    内核原生技能在最前，且**不可被磁盘上的同名技能顶掉** ——
    它是纪律，不是可替换的第三方内容。
    """
    found: dict[str, dict] = {k: dict(v) for k, v in KERNEL_SKILLS.items()}
    for root in (*SKILL_ROOTS, *_EXTERNAL_SKILLS):
        # Supervisor.skill 本身就是单技能库根，不是「装技能的目录」。
        if root.name == "Supervisor.skill" and (root / "SKILL.md").is_file():
            head = (root / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            name, desc = _frontmatter(head)
            found["create-supervisor"] = {"name": name or "create-supervisor", "dir": root.name,
                "path": str(root / "SKILL.md"), "source": root.name,
                "description": desc, "bytes": len(head)}
        if root.is_file() or not root.exists():
            continue
        for d in sorted(root.iterdir()):
            md = d / "SKILL.md"
            if not d.is_dir() or not md.exists():
                continue
            key = d.name
            if key in found:
                continue                      # 高优先级根已提供，跳过旧副本
            try:
                head = md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            name, desc = _frontmatter(head)
            found[key] = {"name": name or key, "dir": key,
                          "path": str(md), "source": root.name,
                          "description": desc, "bytes": len(head)}
    return found


def available() -> set[str]:
    return set(index())


def describe(name: str) -> str:
    return (index().get(name) or {}).get("description", "")


def resolve(name: str) -> Path | None:
    """返回技能文件的磁盘路径。内核原生技能没有路径 → None。"""
    e = index().get(name)
    if not e or not e.get("path"):
        return None
    return Path(e["path"])


def read_skill_text(name: str, limit: int = 60000, file: str = "") -> str:
    """读技能文本。找不到时给出**可操作**的提示，而不是一句"失败"。

    `file` 为空 ⇒ 读入口文档（`SKILL.md`）；给了相对路径 ⇒ 读该技能目录下的那个文件。

    **为什么需要 `file`**（2026-09-18 实测挖出）：`SKILL.md` 自己写着
    「`references/` 下的文件是按需深读、照 manifest 的表打开」，里面还有
    `../nature-shared/core/nature-introduction.md` 这类路径指引 ——
    但原来**没有任何工具能读它们**，于是模型去猜 `nature-writing/core/stance.md`
    并连着报「找不到技能」。**技能在指示模型做一件内核做不到的事。**
    真实规模：nature-writing 一个技能就有 71 个 .md / 195,720 字节，
    全部内联不现实 ⇒ 只能"入口文档 + 按需取参考"。
    """
    if name in KERNEL_SKILLS:                 # 内核原生：直接给文本，不碰磁盘
        if file:
            return f"⛔ 技能 {name!r} 是**内核原生技能**，没有参考文件可读。"
        return _PITFALL_DOCTRINE
    p = resolve(name)
    if p is None:
        near = [k for k in available() if name.lower() in k.lower()]
        hint = ("你是不是想找：" + ", ".join(near[:5])) if near else \
               ("本机可用技能：" + ", ".join(sorted(available())[:12]) + " …")
        return f"⛔ 找不到技能 {name!r}。{hint}"

    if file:
        return _read_skill_file(name, p.parent, file, limit)

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"⛔ 读取 {name!r} 失败：{type(e).__name__}: {e}"
    if len(text) > limit:
        text = text[:limit] + f"\n…（技能过长，已截断，原长 {len(text)} 字符）"
    return text


# 能当"技能参考"读的文本后缀。刻意**不列二进制**（图、字体、压缩包）：
# 读进来是一堆乱码，还会白占一大段上下文。
_SKILL_TEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".csv"}


def skill_files(name: str) -> list[str]:
    """技能目录下**可读**的文件（相对路径）。给模型"该读哪个"一个精确答案。

    存在的理由：原实现在路径猜错时只会把整机技能名列表倒出来 ——
    模型想问的是"nature-writing 里有哪些文件"，得到的却是"本机有哪些技能"。
    答非所问也是一种失败，而且它让模型继续猜。
    """
    p = resolve(name)
    if p is None:
        return []
    root = p.parent
    out: list[str] = []
    try:
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix.lower() in _SKILL_TEXT_SUFFIXES:
                out.append(f.relative_to(root).as_posix())
    except Exception:
        return []
    return out


def _inside(root: Path, target: Path) -> bool:
    """`target` 是否落在 `root` 之内。**抽成具名函数是为了让承重自检能替换它** ——
    把谓词换成恒真，"目录穿越必须被拒"的正控必须当场失守。
    不这样做，那条安全断言可能其实是别的原因在过（G 型：有正控但不承重）。

    比的是**解析后的真路径**，不是字符串里有没有 `..`：
    `a/../../b` 与 `..\\..\\b` 都绕得过朴素的字符串检查。
    """
    try:
        real_root = str(root.resolve())
        real_target = str(target.resolve())
    except Exception:
        return False
    return real_target == real_root or real_target.startswith(real_root + os.sep)


def _sibling_skill(target: Path, root: Path) -> tuple[str, Path] | None:
    """`target` 是否落在**同库另一个已注册技能**的目录内？是则返回 (技能名, 该技能根)。

    ## 为什么要开这道门

    `nature-*` 这套技能是「一个技能 + 一个共享层」的结构：各自的 `SKILL.md` 和
    `manifest.yaml` 里，共享层写作 `../nature-shared/core/xxx.md`（实测 nature-writing
    里有 13 处这种写法）。原来的守卫一律拒绝 —— 于是 `always_load` 声明必读的
    4 个共享文件**一个都读不到**，而那 4 个文件在磁盘上好好躺着。

    ## 安全边界（比原来宽，但仍不是任意穿越）

    放行的条件**同时**满足：
      ① `target` 落在 `root` 的父目录（技能库根）之内；
      ② 库根下的第一段（如 `nature-shared`）**自己带 SKILL.md**，即它是已注册技能。
    因此 `../../../etc/passwd`（出库根）和 `../不存在的东西/x.md`（不是技能）仍然被拒。

    共享层是**独立注册的技能**（`load_skill("nature-shared", file="core/xxx.md")`
    本来就读得到），这里只是让文档里已经写好的 `../` 写法也能走通，
    不必去改第三方技能的正文。
    """
    try:
        real = target.resolve()
        lib = root.resolve().parent
    except Exception:
        return None
    if not _inside(lib, real):
        return None
    try:
        rel = real.relative_to(lib)
    except Exception:
        return None
    if not rel.parts:
        return None
    owner = rel.parts[0]
    skill_root = lib / owner
    if not (skill_root / "SKILL.md").is_file():
        return None
    if owner == root.resolve().name:
        return None                       # 落回自己，交给常规分支
    return owner, skill_root


def _read_skill_file(name: str, root: Path, file: str, limit: int) -> str:
    """读技能目录内的一个文件。**必须挡住目录穿越** —— `file` 是模型给的字符串。

    允许一种例外：指向**同库另一个已注册技能**（见 `_sibling_skill`）——
    `nature-*` 的共享层 `../nature-shared/…` 就是这么写的。
    """
    rel = (file or "").strip().lstrip("/\\").replace("\\", "/")
    if not rel:
        return "⛔ 文件名为空。可用文件：\n" + _file_list_block(name)
    target = root / rel
    if not _inside(root, target):
        sib = _sibling_skill(target, root)
        if sib is None:
            return (f"⛔ {file!r} 指向技能目录之外 —— 技能参考只能是 "
                    f"`{name}` 目录内的文件，或同库另一个技能（如 "
                    f"`load_skill(\"nature-shared\", file=\"core/…\")`）。\n"
                    f"可用文件：\n" + _file_list_block(name))
        owner, sib_root = sib
        sub = target.resolve().relative_to(sib_root.resolve()).as_posix()
        body = _read_skill_file(owner, sib_root, sub, limit)
        if body.startswith("⛔"):
            return body                  # 递归里的失败必须原样透出，不许被前缀包成"成功"
        return f"（经共享层 `{owner}` 解析 {file!r}）\n{body}"
    try:
        real = target.resolve()
    except Exception as e:
        return f"⛔ 解析 {name!r} 的 {file!r} 失败：{type(e).__name__}: {e}"
    if not real.is_file():
        return (f"⛔ `{name}` 里没有 {file!r}。\n可用文件：\n" + _file_list_block(name))
    try:
        text = real.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"⛔ 读 {name}/{file} 失败：{type(e).__name__}: {e}"
    if len(text) > limit:
        text = text[:limit] + f"\n…（文件过长，已截断，原长 {len(text)} 字符）"
    return f"（{name}/{rel}）\n{text}"


def _file_list_block(name: str, cap: int = 60) -> str:
    files = skill_files(name)
    if not files:
        return "  （这个技能没有可读的参考文件）"
    shown = files[:cap]
    body = "\n".join(f"  - {f}" for f in shown)
    if len(files) > cap:
        body += f"\n  …（共 {len(files)} 个）"
    return body
