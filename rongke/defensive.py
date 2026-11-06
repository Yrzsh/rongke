# -*- coding: utf-8 -*-
"""稿件「防御性写作」检出层（乙派 / 证据边界口径）。

## 这一层是什么

`writing.py` 查的是**套话**（「随着…的发展」「综上所述」）——那些是**空的**。
这一层查的是另一种毛病：**话不空，但写的时候在跟一个不在场的审稿人吵架**。

例：
  「我们强调，上述结果不应被理解为普适鲁棒性的证据。」
  这句没有一个套话词，字数也正常；但它做了两件不必要的事：
  先立一个没人提过的靶（"普适鲁棒性"），再宣布自己不打。

## ★ 口径：只删姿态，不删事实（这是与另一个流派的根本分歧）

GitHub 上同名的方案有两派。星数最高的那份（4k）主张
「打不过的维度不设为比赛 / 可以重构整个故事」——那是**改研究问题**，不采纳。

本实现站在**证据边界**这一派（125★/276★ 那两个 + Nature 全链那份）。

**铁律：判断"该不该删"只有一条标准 ——**

    把这句删掉，重读。读者有没有丢失**事实、数字、条件、逻辑链**？
      丢了 ⇒ 不许删。改成"把同一件事正面说出来"，或挪到 Methods/讨论。
      没丢 ⇒ 可以删。

所以本层**绝不**：
  · 删除数字、样本量、置信区间、p 值、条件限定
  · 把「可能」改成「确定」、把「报告」改成「观察」、把「有界个案」改成「普遍规律」
  · 因为某句"不占优"就建议换评价维度或重写结论

**不确定时保留，并提示"人工确认"** —— 这是乙派与甲派最关键的行为差异。

## 为什么是 soft

本层所有检出都是 `soft`。理由写在 `writing.py` 开头：
「硬门只留可核对的验收标准，字面匹配当硬门必误伤」。
防御性写作有大量**正当用法**（真实的局限、必要的免责、伦理声明），
当成硬门会逼模型删掉本该留的东西 —— 那比漏报严重得多。

## 出处

分类框架参考 `Worigin0314/academic-defensive-writing-auditor`（MIT，9 类 D1–D9）
与 `lensback940701` 的证据约束版（MIT，"不确定时保留并登记 query"）。
模式本身是自写的，没有逐字复制。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── 中文与英文两套触发面 ──────────────────────────────────────
#
# 每条 = (代号, 人话标签, 正则, 改法)
# 改法必须**可执行**，且默认偏向"保留信息、只换姿态"。


@dataclass(frozen=True)
class Tell:
    code: str
    label: str
    pat: re.Pattern
    fix: str
    #: 这条检出是否"高置信"——高置信的才在汇总里单独点名
    strong: bool = False
    #: 一句里需要命中几次才算数。默认 1；限定语堆叠那类要 ≥3
    need: int = 1


def _p(*alts: str) -> re.Pattern:
    """把若干**独立分支**拼成一条正则。

    用 `_p("a", "b")` 而不是手写 `"a|b"`，是因为我第一版手写时
    **漏过一个 `|`**：英文分支 `for a fair comparison` 忘了前导竖线，
    结果它被粘到前一个中文分支的末尾，变成「必须先出现『所限』」才可能命中——
    英文那一路整批静默失效，而负控照样全绿（不报错就没信号）。
    传多个参数时由这里机械 join，漏不了。

    IGNORECASE 同样是必须的：触发面几乎都按小写写，而正文句首字母大写
    （`We do not claim…`）。第一版只有 re.M，4 条英文正控全漏才暴露。
    """
    return re.compile("|".join(f"(?:{a})" for a in alts), re.M | re.I)


# D2 / 反复声明「不主张」 ────────────────────────────────────
NON_CLAIM = Tell(
    "defensive_non_claim",
    "反复声明「本文不主张……」",
    _p(r"(我们|本文|本研究|该)(并不|并不试图|无意|不)(声称|主张|断言|宣称)",
       r"(不应|不能|不可)(被)?(理解为|视为|解读为)",
       r"(并非|不是)(旨在|为了)(证明|说明)",
       r"we\s+(do\s+not|make\s+no|are\s+not)\s+(claim|argue|assert|suggest)",
       r"should\s+not\s+be\s+(read|interpreted|taken)\s+as",
       r"this\s+does\s+not\s+(establish|imply|prove|mean)"),
    "这句在预告「我不主张什么」。若读者本没这样理解，直接删；"
    "若确有限制，改成正面的范围陈述（「本结论适用于 X 条件」），只写一次。",
    strong=True,
)

# D1 / 面向审稿人的预防性辩护 ──────────────────────────────
PREBUTTAL = Tell(
    "defensive_prebuttal",
    "面向假想审稿人的预防性辩护",
    _p(r"(需要|应当|必须|值得)(特别)?指出(的是)?",
       r"(为|为了)(避免|防止)(误解|误读|混淆)",
       r"(有人|读者|审稿人)(可能|或许|也许)(会)?(认为|质疑|提出|问)",
       r"(可能|或许)有人(会)?(问|质疑)",
       r"(在此|这里)(需要|有必要)(说明|澄清|强调)",
       r"it\s+should\s+be\s+(noted|emphasized|mentioned)\s+that",
       r"we\s+(emphasize|stress|note)\s+that",
       r"one\s+might\s+(argue|object|ask)",
       r"to\s+avoid\s+(misunderstanding|confusion)"),
    "删掉这层开场，直接陈述技术事实。"
    "「需要指出的是，X」→「X」。若 X 本身是限制，见上一条的正面写法。",
    strong=True,
)

# D3 / 限定语堆叠 ─────────────────────────────────────────
#
# 做法：不去写一个「三个弱化词各来一遍」的巨型正则（第一版就是这么写的，
# 结果漏报——因为它要求三段之间不能跨过句号，而真实里弱化词挨得很近却形态各异）。
# 改成：先按句切，再在**同一句内**数弱化词出现次数，≥3 才算堆叠。
_HEDGE = re.compile(
    r"在一定程度上|某种程度上|较为|相对地|可能|或许|大概|似乎|"
    r"在某种意义上|基本上|大致|大体上|不无|未尝|或可|未必|"
    r"potentially|arguably|somewhat|relatively|presumably|"
    r"\bmay\b|\bmight\b|\bcould\b|\bpossibly\b|\bperhaps\b",
    re.I,
)

CAVEAT_STACK = Tell(
    "defensive_caveat_stack",
    "同一句里限定语堆叠",
    _HEDGE,   # 命中次数由 need 控制
    "保留**最强的那个**限定语，其余删掉；"
    "真正的范围限制挪到方法/讨论里写一次。⚠ 逐句人工确认，别整段删。",
    need=3,
)

# D4 / 为不理想的结果提前开脱 ──────────────────────────────
EXCUSING = Tell(
    "defensive_excusing",
    "为不理想结果预先开脱",
    # 「是(由于|…)」与英文 `is due to` 必须**分成两支**：
    # 第一版把 `lower score … due to` 和中文分支拼在一行里，漏了中间的 `|`，
    # 于是英文那支被并进「（受…所限）」的分支尾部，永远不可能单独命中。
    _p(r"(低于|不如|落后于|略逊)[^。\n]{0,24}(是|属于|主要)?(由于|因为|源于)",
       r"(性能|精度|效果|表现)(下降|变差|降低)(是|主要|可能)?(由于|因为|源于)",
       r"(结果)?(仍然|依然)(令人|较为)?(鼓舞|乐观|可接受)",
       r"(受|限于)(客观)?(条件|篇幅|数据|设备)(所限|限制)",
       r"(the\s+lower|the\s+degrad|\bthis\s+drop)[^.\n]{0,40}"
       r"(is\s+(likely\s+)?due\s+to|because)",
       r"remains?\s+encouraging\s+despite"),
    "先报结果本身，再给解释；且解释必须有证据支撑。"
    "没做归因实验就删掉归因；做了就写清是哪个诊断支持的。",
    strong=True,
)

# D5 / 为省略的实验辩护 ──────────────────────────────────
OMISSION = Tell(
    "defensive_omission",
    "为省略的实验做辩护",
    _p(r"(我们|本文)(未|没有|未予)(进行|开展|纳入|包含|考虑)[^。\n]{0,30}(因为|由于|考虑到)",
       r"(不|未)(纳入|包含|比较)[^。\n]{0,24}(是|主要)?(因为|由于)",
       r"(这样|如此)(比较|设置)(不公平|不合理)",
       r"we\s+(did\s+not|omit|exclude)[^.\n]{0,40}(because|since)",
       r"such\s+a\s+comparison\s+would\s+(be\s+)?unfair"),
    "如果这项确实影响结论，写成一条局限（放讨论）；"
    "如果只影响观感，改成直接描述评估方案，不要跟假想审稿人辩论。",
)

# D6 / 公平性自我辩护 ────────────────────────────────────
FAIRNESS = Tell(
    "defensive_fairness",
    "「为了保证公平」式自我辩护",
    # 「公平性」「公正性」这类**带性字的名词**第一版没覆盖，
    # 而它恰恰是中文里最常见的写法（「为了保证比较的公平性」）。
    _p(r"(为了|为保证|以确保|确保)[^。\n]{0,8}?(公平|公正|公允)(性)?",
       r"(公平|公正|公允)(性)?起见",
       r"(该|此)(设置|对比|条件)(是)?(公平|合理|恰当)的",
       r"for\s+(a\s+)?fair\s+comparison",
       r"to\s+ensure\s+fairness",
       r"we\s+believe\s+(this\s+is\s+)?(sufficient|fair)"),
    "直接描述受控变量，不要宣称公平。"
    "「为保证公平，所有方法使用同一主干」→「所有方法使用同一主干」。",
    strong=True,
)

# D7 / 反复贴「初步/探索性」标签 ──────────────────────────
PRELIM = Tell(
    "defensive_preliminary",
    "反复贴「初步/探索性」标签",
    _p(r"(初步|探索性|试验性|试探性|前期的?|阶段性的?)(研究|结果|分析|结论|工作)",
       r"(仅|只是)(作为|属于)(初步|探索)",
       r"(preliminary|exploratory|suggestive|proof-of-concept|indicative)\s+"
       r"(result|finding|study|analysis)"),
    "这类标签全篇最多留必要的一两处；反复贴会让读者低估整篇的分量，"
    "也会自相矛盾。拿不准时保留**第一次**出现的那处。",
)

# D8 / 法条式免责清单 ────────────────────────────────────
LEGALISTIC = Tell(
    "defensive_legalistic",
    "法条式免责清单",
    _p(r"(本文|本研究|上述|该)(结论|结果|数据|分析)(不|既不)[^。\n]{0,40}"
       r"(也|亦)(不|未)[^。\n]{0,40}[。；]"),
    "把连续的「既不…也不…」清单压成一句正面的适用范围陈述。"
    "若清单里含伦理/合规/安全条款，**保留**——那属正当边界。",
)

# D9 / 无关的防御性披露 ──────────────────────────────────
IRRELEVANT = Tell(
    "defensive_irrelevant",
    "与论证无关的过程披露",
    _p(r"(首先|最初|一开始)[^。\n]{0,30}(尝试|考虑|设想)"
       r"[^。\n]{0,40}(但|然而|不过)[^。\n]{0,30}(失败|无效|不可行|放弃|改用)",
       r"(后来|之后|随后)(我们)?(改|换|转向)",
       r"we\s+(first\s+)?(tried|attempted)[^.\n]{0,40}(but|however)"
       r"[^.\n]{0,40}(failed|not\s+work)"),
    "读者不关心试错过程。写成「问题→缺口→本文做法→证据」；"
    "若失败的尝试本身有信息量（排除了一条路径），单独成段并说明它排除了什么。",
)

ALL_TELLS: tuple[Tell, ...] = (
    NON_CLAIM, PREBUTTAL, CAVEAT_STACK, EXCUSING, OMISSION,
    FAIRNESS, PRELIM, LEGALISTIC, IRRELEVANT,
)

# ── 必须保留的信号：命中这些的句子，不参与「整句可删」建议 ──────
#
# 这一条是本层与「甲派」的分界线：只要句子里有**事实载体**，
# 就只允许换姿态，绝不允许整句删。
PROTECTED = re.compile(
    r"\d"                                  # 任何数字：样本量、年份、p 值、CI、百分比
    r"|[pP]\s*[<>=]\s*0?\.\d+"             # p 值
    r"|\d+\s*[%‰]"                         # 百分比
    r"|置信区间|confidence\s+interval"
    r"|伦理|知情同意|审批|批号|伦理审查"        # 伦理合规
    r"|ethic|consent|approv"
    r"|数据可用|代码可用|data\s+availab|code\s+availab"
    r"|利益冲突|资助|基金|conflict\s+of\s+interest|funding"
    r"|局限|limitation"                     # 显式局限段里的内容
)

# ── 检出 ──────────────────────────────────────────────────


def scan(text: str, max_per: int = 2) -> list[dict]:
    """扫一段正文，返回命中列表。每类最多报 `max_per` 处，避免刷屏。

    返回的每条都是**候选**，不是判决 —— 调用方按 soft 处理。
    """
    out: list[dict] = []
    # 先切句，位置信息对模型改稿才有用
    sents = re.split(r"(?<=[。！？；\n])", text)
    for tell in ALL_TELLS:
        hits = 0
        for s in sents:
            if not s.strip() or hits >= max_per:
                continue
            found = list(tell.pat.finditer(s))
            # need>1 的规则（限定语堆叠）要求**同一句内**够次数才算，
            # 且用不同片段计数——同一个词重复三次也算堆叠。
            if len(found) < tell.need:
                continue
            hits += 1
            snippet = s.strip()
            if len(snippet) > 90:
                snippet = snippet[:44] + "…" + snippet[-40:]
            protected = bool(PROTECTED.search(s))
            out.append({
                "code": tell.code,
                "label": tell.label,
                "strong": tell.strong,
                "sentence": snippet,
                "protected": protected,
                "fix": (
                    tell.fix + " ⚠ 这句含数字/合规/局限信息，只改姿态，不许整句删。"
                    if protected else tell.fix
                ),
            })
    return out


def summarize(hits: list[dict]) -> str:
    """把检出压成给模型看的一段话。**只列强置信 + 含保护信号的**，其余计数。"""
    if not hits:
        return ""
    strong = [h for h in hits if h["strong"]]
    guarded = [h for h in hits if h["protected"]]
    lines = []
    if strong:
        lines.append(f"防御性写作 {len(hits)} 处（其中 {len(strong)} 处为典型）：")
        for h in strong[:4]:
            lines.append(f"  · {h['label']}：「{h['sentence'][:60]}」")
    if guarded:
        lines.append(f"  ⚠ 其中 {len(guarded)} 处含数字/合规/局限内容 —— 只改姿态，禁删事实")
    return "\n".join(lines)
