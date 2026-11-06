# -*- coding: utf-8 -*-
"""写作体检 —— 稿面问题的**程序化**核验。

## 为什么要有这一层

原来的写作纪律全靠 `load_skill` 把规则写在技能全文里，指望模型"读了就照做"。
证据门只核对**流程**（技能装载了吗、证据可追溯吗），**从不看文本本身**。
后果：一段 118 字的引言，只要技能装过、证据链完整，门就放行 ——
哪怕它是「随着冬虫夏草产业的飞速发展，众所周知，该领域具有重要的意义」
这种空转句子。

荣珂的两道硬门（完成证据门 / 写作技能门）都只拦"流程造假"，
这一层补的是**"流程合规但文本不合格"**—— 那是最容易蒙混过关的一类。

## 边界（很重要，别指望它替代判断）

它**只查可程序化核实的事实**：体量、段落、句长、标点、套话密度、引注标记、重复、
占位符残留、引注首现顺序、工具链行话泄漏、段落收尾同构、绝对化表述、英文句长。
它**不评判论证是否成立、文献是否恰当、结论是否创新**——那需要领域判断，
强行用正则去猜只会造出一堆假报警，反而让模型学会无视体检报告。

所以硬门只留**客观错误**（空稿 / 字数严重偏离 / 正文里出现点列 / 重复标点 / 超长句骗分），
风格类一律 soft。这与项目已有的教训一致：*硬门只留可核对的验收标准，字面匹配当硬门必误伤。*

## 一个刻意的设计

`target_chars` **不由模型自报**，而是从用户原话里正则抽出来的（`约120字` / `5000 字左右`）。
模型自报的字数目标会被它自己" interpretive" 地放宽；用户写的数字不会。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field

# ── 字符类 ──────────────────────────────────────────────────
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
# 中文句末标点 + 英文句号（要求后接空格且下一词首字母大写，避免劈开 "3.5" "et al."）。
# 不做英文断句的话，一篇纯英文稿会被算成「1 句、均句长 800 字」—— 全文性假报警。
_SENT_END = re.compile(r"(?<=[。！？；…!?;])\s*|(?<=\.)\s+(?=[A-Z\u4e00-\u9fff])")

# ── 套话表：(正则, 标签, 改法) ──────────────────────────────
# 入选标准：**在任何严肃中文论文里都是空转词**，删掉不损失信息。
# 故意不收的：学术上正常的连接词（"然而""因此""此外"）—— 收了就成噪音。
_CLICHES: tuple[tuple[str, str, str], ...] = (
    (r"随着[^，。；]{0,24}(的|之)(不断)?(发展|进步|推进|深入|普及|提高)",
     "「随着……的发展」起手式",
     "直接给事实与缺口，例：「近十年 X 产量由 A 增至 B，但 Y 仍未解决」"),
    (r"众所周知", "「众所周知」", "删掉。学术写作不靠共识铺垫，只靠引证"),
    (r"不言而喻|毋庸(置疑|讳言)|无可(否认|厚非)", "「毋庸置疑」类",
     "删掉，或改成一条可引证的事实"),
    (r"显而易见", "「显而易见」", "删掉。「显然」是审稿人最不买账的词"),
    (r"(具有|有着)(重要|重大|深远|一定)的?(意义|价值|影响)", "「具有重要意义」",
     "改成具体后果：对谁、在哪一环、量化到什么程度"),
    (r"(发挥|起到)(了)?(重要|关键|巨大|积极)?的?(作用)", "「发挥……作用」",
     "写清它具体改变了哪个变量，否则句子没有信息量"),
    (r"(扮演|担(负|当))(了)?(重要|关键)?的?(角色|重任)", "「扮演……角色」",
     "同「发挥作用」，换成具体机制描述"),
    (r"极大地?(提高|提升|增强|改善|促进|推动|丰富|拓展)", "「极大地……」",
     "删掉「极大地」；要么给出量级，要么删掉整个程度状语"),
    (r"(取得|获得了?)(了)?(丰硕|显著|重要|突破性)?的?(成果|进展|突破|成就)",
     "「取得……成果」", "列具体结论，不要总结'成果感'"),
    (r"(综上(所述)?|总而言之|总的来说|总体(而言|来看)|一言以蔽之)",
     "「综上所述」", "删掉，直接给结论句。综述结尾尤其忌"),
    (r"在当今(社会|时代|世界|中国)|随着(时代|社会)的?(发展|进步)", "「在当今……」",
     "写具体时间窗与范围，如「2020–2025 年间」"),
    (r"(飞速|迅猛|快速)发?展|日新月异", "「飞速发展」", "给增长率或规模数字"),
    (r"越来(越|愈)", "「越来越……」", "给趋势的量化表述：「年均增长 X%」"),
    (r"(需要|应当|值得)指出的是", "「值得注意的是」",
     "全篇最多留一处，其余直接陈述"),
    (r"在一定程度上|某种程度上", "「在一定程度上」", "给出具体条件，或直接删掉"),
    (r"相关的?(研究|工作|问题|内容|领域)", "「相关的研究」",
     "点名具体研究（作者+年份）或删掉定语"),
    (r"(备受|广受)(关注|瞩目)|(引起|受到|得到)(了)?(广泛|高度|极大|普遍)?的?(关注|重视|兴趣|青睐)",
     "「备受关注」", "换成谁、在哪一年、提出了什么问题"),
    (r"(值得|需要)(我们)?(深入|进一步)?(探讨|研究|思考|重视)|有待(进一步|深入)(研究|探讨)",
     "「值得深入探讨」结尾式", "结尾必须给结论或给具体未解问题，不要用'值得研究'收尾"),
    (r"提供了?(有力|重要|坚实)?的?(理论)?(支撑|依据|参考|借鉴)", "「提供支撑」",
     "写清支撑了哪一条具体论断"),
    (r"(为|给)[^，。]{0,12}(提供|带来)(了)?(新|重要)的?(思路|契机|机遇|方向)",
     "「提供新思路」", "写清这条新思路具体新在哪一步"),
)

# ── 冗余动词框架：「进行观察」→「观察」 ─────────────────────
# 只收**能独立作动词**的双字词，避免「进行实验」这类惯用搭配被误报。
_REDUNDANT_FRAME = re.compile(
    r"(进行|作出|加以|予以|给予)(了)?("
    r"观察|判断|分析|研究|比较|测量|评价|说明|处理|讨论|检验|验证|"
    r"统计|建模|计算|预测|优化|改进|设计|实现|考察|调查|评估|论证)"
)

# ── 引注标记 ────────────────────────────────────────────────
# 拆成「数字制」与「著者-出版年制」两套，因为**首现顺序**这条硬要求只对数字制成成立，
# 而两套混用本身就是错（期刊排版会直接退回）。
# 号段限 1–3 位数字：`[2020-2025]` 是年份区间，不是引注 —— 让它落进来会造出假报警。
_CITE_NUM = re.compile(r"\[(\d{1,3}(?:\s*[-,，～~至]\s*\d{1,3})*)\]")   # [1] [1,2] [1-3]
_AY_PAT = (
    r"（[^）]{0,40}?(?:et al\.?|等)[，,]\s*\d{4}[a-z]?）"                  # （Zhang et al., 2020）
    r"|\([A-Z][A-Za-z\-]+(?:\s+(?:et al\.?|and\s+[A-Z][A-Za-z\-]+))?,\s*\d{4}[a-z]?\)"
    r"|（[\u4e00-\u9fff]{2,3}(?:等)?[，,]\s*\d{4}[a-z]?）"                 # （张三，2020）
)
_CITE_AY = re.compile(_AY_PAT)
_CITE = re.compile(r"\[\d{1,3}(?:\s*[-,，～~至]\s*\d{1,3})*\]" + "|" + _AY_PAT)


def _expand_cite_group(g: str) -> list[int]:
    """把 `[1,3-5]` 的括号内容展平成 [1, 3, 4, 5]。

    段长上限 60：`[1-3]` 是号段，`[1-999]` 是写错了或不是引注 ——
    与其展开出 999 个数把顺序判定彻底搅乱，不如只取起点。
    """
    out: list[int] = []
    for part in re.split(r"[,，]", g):
        part = part.strip()
        m = re.fullmatch(r"(\d{1,3})\s*[-–~～至]\s*(\d{1,3})", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and b - a <= 60:
                out.extend(range(a, b + 1))
            else:
                out.append(a)
        elif part.isdigit():
            out.append(int(part))
    return out

# ── 占位符 / 模板残留 ───────────────────────────────────────
# 只收**任何严肃稿子里都不该出现**的痕迹，宁可窄：
# 漏掉一个 TODO 只是少一条提示，误报一次「某某」会让模型学会无视整份体检报告。
_PLACEHOLDER = re.compile(
    r"TODO|TBD|FIXME|PLACEHOLDER|lorem ipsum"
    r"|待(?:补充|填写|补|定|核实|确认|细化|完善|给出)"
    r"|此处省略|以下省略|略去不表"
    r"|某某|某氏|××+"
    r"|[×Xx]{2,}(?=\s*(?:%|％|元|万|人|个|次|年|月|日|倍|条|篇|名|所|家))"
    r"|【\s*】|\[\s*\]|（\s*）|\(\s*\)"
    r"|\\[1-9](?!\d)"                                    # 没回填的正则捕获组 \1
    r"|\{\{[^}\n]{0,30}\}\}"
    r"|X{3,}",
    re.I)

# ── 工具链内行话（软）───────────────────────────────────────
# 只收**工具名 / 内部字段名**这类不会自然出现在稿件正文里的词。
# 刻意不收 JSON / markdown / prompt / token / pipeline ——
# 研究 LLM 的论文里它们是正当术语，收了就会在正确的稿子上报错，比漏报更糟。
_JARGON = re.compile(
    r"workbench|wb\.py|staged_gen|quality_rules|"
    r"load_skill|read_skill|draft_text|draft_path|"
    r"report_blocker|deliver_task|call_seq|job_id|run_id|"
    r"tool_registry|\.bak\b|--force|--dir\b|python\s+-u|"
    r"report/[A-Za-z0-9_\-]+\.(?:md|json|csv)",
    re.I)

# ── 绝对化表述（软）─────────────────────────────────────────
# 分两档：强档命中一处就提（这些词在学术论断里基本站不住）；
# 弱档攒够 3 处才提 —— 「完全一致」有时是正当的，出现一次不算病。
_ABS_STRONG = re.compile(
    r"毫无疑问|毋庸置疑|无可辩驳|千真万确|毫无例外|无一例外|"
    r"决不可能|绝不可能|从未(?:出现|发生|观察到|见诸)|从来没有|"
    r"100%|百分之百")
_ABS_WEAK = re.compile(
    r"完全(?:不|没|未|一致|相同|可以|能够|避免|消除|解决|杜绝)|"
    r"绝对(?:不|是|正确|可靠|安全|成立)|"
    r"必然(?:导致|造成|引起|是|会)|"
    r"(?:所有|全部)(?:的)?(?:研究|文献|学者|病例|样本|数据)(?:都|均)|"
    r"彻底(?:解决|消除|杜绝|改变)|"
    r"一切(?:都|问题|困难)")

# ── 英文句长 ────────────────────────────────────────────────
# 按**词**算，不按字符 —— 35 个英文词 ≈ 60 个汉字的信息量，
# 拿中文字数阈值去量英文必然误判（这正是把纯英文稿算成「1 句 800 字」的同源错误）。
_EN_LONG_SOFT = 35       # 期刊语言检查常见的零容忍线
_EN_LONG_HARD = 70

# ── 参考文献表 / 图注表注（字数口径用）──────────────────────
# 踩坑清单第 19 条：**期刊字数上限是按「含表图、剔除文末文献表」核的，而这条没有机验。**
# 内核若把文献表算进正文字数，就会让模型为了"压到上限"去砍正文 —— 砍错地方。
_REFS_LINE = re.compile(
    r"^(?:#{1,6}\s*)?(?:参考文献|參考文獻|引用文献|文献列表|References?|Bibliography|"
    r"Literature\s+Cited|Works\s+Cited)\s*[：:]?\s*(?:（[^）]{0,12}）|\([\w\s]{0,12}\))?$",
    re.I)
# 只认**行首的图注/表注**。正文里的「见表 2」是交叉引用 —— 数它会重复计数
# （同一张表被引三次就成了三张表），那比不数更糟：**宁可不出数，不出假数。**
_FIG_CAP = re.compile(r"^[ \t>*\-]*(?:图|Figure|Fig\.?)\s*(\d{1,2})[ \t:：.、]?", re.I | re.M)
_TAB_CAP = re.compile(r"^[ \t>*\-]*(?:表|Table|Tab\.?)\s*(\d{1,2})[ \t:：.、]?", re.I | re.M)


def _refs_offset(text: str) -> int:
    """参考文献表的起始字符位置；找不到返回 -1。

    只在**后 80% 的正文**里找，且要求该行短（≤24 字符）——
    否则「参考文献的著录格式需统一」这种正文句子会被误判成文献表标题，
    于是一整篇稿子的字数口径被静默改掉（而它不会报错）。
    """
    off = 0
    for line in text.split("\n"):
        s = line.strip()
        if 2 <= len(s) <= 24 and off > len(text) * 0.2 and _REFS_LINE.match(s):
            return off
        off += len(line) + 1
    return -1


# 文献表里一条**条目**的起头。只认这几种真实存在的著录形态：
#   ① `[1]` / `［1］`（GB/T 7714 与中文期刊最常见）
#   ② `1.` / `1)` / `1、` 后接非数字（防把年份「2019.」当条目）
#   ③ `- ` 项目符号（Markdown 列表式文献表）
#   ④ `Author, A. (2019).` 这类无编号的 APA 行 —— 靠"年份在括号里"认
# ★ 为什么不数正文里的 `[1]` 引注：同一篇文献被引三次会数成三篇，
#   **那比数不出来更糟** —— 用户会以为"50 篇"达标了，而实际只有 18 篇。
_REF_ENTRY = re.compile(
    r"^(?:"
    r"[\[［]\s*\d{1,3}\s*[\]］]"                       # [1] / ［1］
    r"|\d{1,3}\s*[.)）、]\s*(?!\d)"                   # 1. 1) 1、 (后面不是数字)
    r"|[-*+]\s+\S"                                    # - 项目符号
    r"|[^\s].{0,80}?[\(（]\s*(?:19|20)\d{2}\s*[\)）]"  # APA: Author (2019).
    r")")


def _count_ref_entries(refs_block: str) -> int:
    """数文献表里有多少条。只数**行首**，不做跨行续行合并。

    ★ 有意保守：GB/T 7714 的长条目会折行，折行行不以编号开头 ⇒ 不计入。
      这是一个"只会少数、不会多数"的方向 —— 数少了会让门要求更多文献，
      数多了会让不达标的稿子过关。用户要的是「最少 50 篇」，
      在两个误差方向里，宁可让他多补两篇，也不能让 30 篇过 50 篇的关。

    ★ 另一种误判要排除：文献表里常见「收稿日期」「基金项目」这类行，
      它们不以编号开头、也不含括号年份 ⇒ 自然不被计入，无需额外排除。
    """
    if not refs_block:
        return 0
    n = 0
    for line in refs_block.split("\n"):
        # 跳过文献表标题行本身与空行
        s = line.strip()
        if not s or _REFS_LINE.match(s):
            continue
        if _REF_ENTRY.match(s):
            n += 1
    return n


# ── 中文语境里的半角标点 ────────────────────────────────────
_HALF_PUNCT = re.compile(r"[\u4e00-\u9fff][,.;:?!]|[,.;:?!][\u4e00-\u9fff]")
_DOUBLE_PUNCT = re.compile(r"[，。；：！？]{2,}")
# ★ 叠字笔误。**不能只写 `的的`** —— 2026-10-15 实跑里它误报了一次：
#   正文「……服务于非融合**目的的**高通量筛选」被判成叠字，可那其实是
#   「目的」+「的」的正常接缝。模型被告知"必修：叠字笔误"，于是花步数去
#   找一处并不存在的错，改不掉、只能反复交。
#   判据要打在**真笔误**上：叠字两侧不能构成词。用否定环视排掉常见合法搭配。
#   合法接缝的完整清单不可能穷举，所以这里只挡**实测出现过**的与明显成词的几种；
#   宁可漏报也不误报 —— 误报会教模型无视整份体检报告（设计定律 10）。
_TYPO_DOUBLE = re.compile(
    r"(?<!目)的的|(?<!就)了了|(?<!总)是是|(?<!存)在在|(?<!你)和和|(?<!相)与与"
)

# ── 点列行首 ────────────────────────────────────────────────
_BULLET = re.compile(
    r"^\s*(?:[-*•·▪]|"
    r"\d{1,2}\s*[.)、]|"
    r"[（(]\s*\d{1,2}\s*[）)]|"
    r"[（(]\s*[一二三四五六七八九十]\s*[）)]|"
    r"[一二三四五六七八九十]\s*[、.])"
)

# 硬门阈值（**只此一处** —— 曾经重复定义过一遍，那正是"两处真相"的形状）
_MAX_SENTENCE_HARD = 120       # 单句超过它 → 硬拦（中文论文最常见病）
_MAX_SENTENCE_SOFT = 80


@dataclass
class Issue:
    """一条稿面问题。`fix` 必须**可执行**——"提高质量"不是 fix。"""
    level: str          # hard | soft
    code: str           # 机器可读代号
    msg: str            # 出了什么事（含定位）
    fix: str            # 具体怎么改

    def to_dict(self) -> dict:
        return asdict(self)

    def line(self) -> str:
        tag = "必修" if self.level == "hard" else "建议"
        return f"  [{tag}] {self.msg}\n         改法：{self.fix}"


@dataclass
class Report:
    metrics: dict = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    score: int = 100
    target_chars: int | None = None
    # ★ 「字数目标」那一行拿哪个量纲去显示。默认 `chars` = 旧行为；
    #   预设把它设成 `chars_eq` 后，「体量」与「当前」就同源（Q3）。
    #   放在末尾并给默认值，手工构造 Report 的老代码不必改。
    display_unit: str = "chars"

    # ── 查询 ────────────────────────────────────────────────
    def hard(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "hard"]

    def soft(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "soft"]

    @property
    def needs_revision(self) -> bool:
        """是否值得打回一次让模型改稿。

        阈值刻意保守：**宁少打一次，不误伤一遍**。硬问题必打回；
        软问题要攒够 4 条或评分跌破 70 才惊动它 —— 因为每一次打回
        都是一轮完整重写，代价是分钟级。

        注：评分已改为两段式（有硬问题 → <70，无硬问题 → ≥70），
        所以 `score < 70` 现在与 `bool(self.hard())` 等价。留着不是为了逻辑，
        而是为了**语义自明**：读到这里就知道"分低"和"必须改"是同一件事。
        """
        return bool(self.hard()) or len(self.soft()) >= 4 or self.score < 70

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "target_chars": self.target_chars,
            "metrics": self.metrics,
            "issues": [i.to_dict() for i in self.issues],
            "hard": len(self.hard()),
            "soft": len(self.soft()),
        }

    # ── 渲染 ────────────────────────────────────────────────
    def brief(self) -> str:
        """一行摘要，给 UI 与 CLI 状态行。"""
        m = self.metrics
        # ★ 英文稿按**词**报，中文稿按**字**报 —— 两种语言各自的自然单位。
        #   混着报会让英文稿显示「0 字」，读者以为稿子空了。
        if m.get("lang") == "en":
            head = (f"{m.get('en_words', 0)} words · {m.get('paragraphs', 0)} 段 · "
                    f"{m.get('sentences', 0)} 句 · 均句 {m.get('avg_sentence', 0)} 字 · "
                    f"{self.score}/100")
            if self.target_chars:
                head += f"（目标约 {self.target_chars} 字 ≈ {int(self.target_chars / 1.6)} 词）"
            return head
        head = (f"{m.get('chars_eq', m.get('chars', 0))} 字 · "
                f"{m.get('paragraphs', 0)} 段 · "
                f"{m.get('sentences', 0)} 句 · 均句 {m.get('avg_sentence', 0)} 字 · "
                f"{self.score}/100")
        if self.target_chars:
            head += f"（目标约 {self.target_chars} 字）"
        return head

    def format_for_model(self) -> str:
        """回灌给模型的体检报告 —— 逐条可改，不是泛泛而谈。"""
        m = self.metrics
        _is_en = m.get("lang") == "en"
        _vol = (f"{m.get('en_words', 0)} words"
                if _is_en else f"{m.get('chars_eq', m.get('chars', 0))} 字")
        lines = [
            "【写作体检 · 内核程序化核验】",
            f"体量：{_vol} / {m.get('paragraphs', 0)} 段 / "
            f"{m.get('sentences', 0)} 句 · 均句长 {m.get('avg_sentence', 0)} 字 · "
            f"最长句 {m.get('max_sentence', 0)} 字",
            f"评分：{self.score}/100",
        ]
        # 字数口径与图表计数：这两项**不给 issue**，只把数摆出来 ——
        # 期刊上限不是内核能知道的，但"该用哪个数去比"是内核能算清的。
        if m.get("has_refs"):
            lines.append(
                f"字数口径：本稿含参考文献表 —— 剔除后正文 {m.get('chars_excl_refs', 0)} 字 / "
                f"全文 {m.get('chars', 0)} 字。期刊上限多按「含表图、**剔除文末文献表**」核算，"
                "自校验请用前者")
        if m.get("figures") or m.get("tables"):
            lines.append(
                f"图表：图 {m.get('figures', 0)} 张 · 表 {m.get('tables', 0)} 个"
                "（按行首图注/表注计数；加图加表会挤占字数上限，改完必须重算，"
                "不能靠记忆）")
        if self.target_chars:
            # ★ 与上面「体量」同源与否由预设的 display_unit 决定。
            #   baseline 取 chars（= 旧行为，两行数字不同 = Q3 的漂移）；
            #   strict 取 chars_eq（两行同一个数）。
            _cur_key = getattr(self, "display_unit", "chars") or "chars"
            lines.append(f"字数目标：约 {self.target_chars} 字"
                         f"（当前 {m.get(_cur_key, m.get('chars', 0))} 字，"
                         f"偏差 {m.get('chars_delta_pct', 0):+.0f}%）")
        hard, soft = self.hard(), self.soft()
        if hard:
            lines += ["", f"⛔ 必修（{len(hard)}）—— 不解决无法通过："]
            lines += [i.line() for i in hard]
        if soft:
            lines += ["", f"◇ 建议（{len(soft)}）—— 不改也能过，但改完更像人写的："]
            lines += [i.line() for i in soft]
        if not hard and not soft:
            lines += ["", "稿面未发现程序化可查的问题。"]
        lines += [
            "",
            "说明：以上只核对**可程序化核实**的稿面事实（体量/结构/句法/套话/标点/引注），",
            "不评判论证是否成立。按技能全文重写后，把**新稿全文**重新放进 draft_text 再交付。",
        ]
        return "\n".join(lines)


# ══════════════════ 文本切分 ══════════════════
def count_chars(text: str) -> int:
    """"字数"：中文字符数 + 西文单词数。中文作者报字数的习惯口径。"""
    return len(_CJK.findall(text)) + len(_LATIN_WORD.findall(text))


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n+", text or "") if p.strip()]


def _sentences(paras: list[str]) -> list[tuple[str, int, int]]:
    """→ [(句子, 段号(从1), 句号(从1))]"""
    out: list[tuple[str, int, int]] = []
    for pi, p in enumerate(paras, 1):
        si = 0
        for raw in _SENT_END.split(p):
            s = raw.strip()
            if not s:
                continue
            si += 1
            out.append((s, pi, si))
    return out


def extract_target_chars(*texts: str) -> int | None:
    """从用户原话里抽字数目标。**不采信模型自报** —— 它会自己放宽。

    认这些写法：约120字 / 120字左右 / 约 5000 字 / 300–500字（取下界附近）/
    120 words / about 120 words
    """
    blob = " ".join(t or "" for t in texts)
    if not blob:
        return None
    # 区间：取中值，审稿人心理上的"约"就是这个
    m = re.search(r"(\d{2,6})\s*[-–~至到]\s*(\d{2,6})\s*(?:字|words?)", blob, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a < b:
            return (a + b) // 2
    # 单值：约/大致/左右/上下 等修饰词可有可无
    m = re.search(r"(?:约|大约|大致|将近|不少于|不超过|至少)?\s*(\d{2,6})\s*(?:字|words?)\s*(?:左右|上下|以内|以上)?",
                  blob, re.I)
    if m:
        n = int(m.group(1))
        if 20 <= n <= 200000:
            return n
    # 中文数字（万字级）：约一万字 / 三千字 / 两万字
    # 字符类必须含「万」与「两」—— 漏任何一个都会静默返回 None（首轮两次踩到）。
    m = re.search(r"([一二两三四五六七八九十百千万]+)\s*字", blob)
    if m:
        n = _cn_number(m.group(1))
        if n and n >= 100:
            return n
    return None


_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def _cn_number(s: str) -> int | None:
    """中文数字解析（正向）。支持 一万 / 三千五百 / 十五 / 二百 / 两万。

    换成正向是因为逆序解析在「一万」上会退化成 1（首轮实测踩到）——
    逆序读到的第一个字是「万」，此时低位累加器还是 0，
    `0 × 10000` 把整个万位吞掉了。正向解析没有这个陷阱。
    """
    total = section = digit = 0
    for ch in s:
        if ch in _CN_DIGIT:
            digit = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            section += (digit or 1) * _CN_UNIT[ch]
            digit = 0
        elif ch == "万":
            total += (section + digit) * 10000
            section = digit = 0
        else:
            return None
    return total + section + digit or None


# ══════════════════ 主检 ══════════════════
def lint(text: str, target_chars: int | None = None,
         allow_lists: bool = False, caps=None) -> Report:
    """体检一段中文（或中英混排）稿子。

    allow_lists=True 时不再把点列当问题（用于契约明确要求清单/表格的任务）。

    `caps` 是能力预设的只读视图（`rongke.caps.Caps`）。`None` 或 `Caps.empty()`
    ⇒ 引注只认半角 `[n]`、字数展示取 `chars`，**与加这个形参之前逐字节等价**。
    预设把 `bracket_pairs` 加上 `〔n〕` 后，正文里那 13 处全角引注才数得出来（Q4）。
    """
    # ★ 括号对由预设决定，但只编译**一个捕获组**：`_CITE_NUM.findall()`
    #   在下面要的是数字串本身，多组会返回 tuple 把 `_expand_cite_group` 打崩。
    pairs = list(caps.bracket_pairs()) if caps is not None else ["[n]"]
    if pairs == ["[n]"]:
        # ★ baseline / `caps=None` **直接复用模块级正则对象**，不另编译。
        #   `writing_lint` 有一条承重自检：把 `_CITE_NUM` 换掉后 cite_order
        #   必须不再报。若这里另起一份正则，那条自检就成了装饰 —— 判据的
        #   覆盖面会被悄悄削掉。旧行为也由此逐字保住。
        cite_num_re, cite_re = _CITE_NUM, _CITE
    else:
        lefts = "".join(sorted({p[0] for p in pairs if len(p) >= 2}))
        rights = "".join(sorted({p[-1] for p in pairs if len(p) >= 2}))
        _L = "[" + re.escape(lefts) + "]" if lefts else r"\["
        _R = "[" + re.escape(rights) + "]" if rights else r"\]"
        _body = r"\d{1,3}(?:\s*[-,，～~至]\s*\d{1,3})*"
        cite_num_re = re.compile(_L + "(" + _body + ")" + _R)
        cite_re = re.compile(_L + _body + _R + "|" + _AY_PAT)
    disp_unit = (str(caps.flag("chars", "display_unit", "chars"))
                 if caps is not None else "chars")
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    paras = _paragraphs(text)
    sents = _sentences(paras)
    issues: list[Issue] = []

    cjk = len(_CJK.findall(text))
    latin = len(_LATIN_WORD.findall(text))
    # ★★ 2026-10-26 修一个**量错了对象**的字数口径缺陷：
    #
    #   原写法 `chars = cjk + latin` 把**英文词数**直接加到**中文字数**上。
    #   两者单位根本不同：`chars` 是「字」，`latin` 是「词」。
    #   后果（实测）：一篇 143 词的英文摘要，`count_chars()`（按非空白字符
    #   算，约 900 字符）与 `chars`（143）差 6 倍，于是 `length_far` 这条
    #   硬门把一份完全正常的英文摘要判成「字数严重不足」。
    #   中文稿因为 CJK 占绝大多数，误差被稀释，所以一直没暴露。
    #
    #   正确口径拆成两个互不混算的量：
    #     · `chars`       —— **只数中日韩字符**（中文稿的字数就是它）
    #     · `en_words`    —— 英文**词数**（英文稿 / 中英混排的英文部分）
    #   `chars` 不再吸收 `latin`。这样 `chars` 与 `count_chars()` 的口径
    #   在同一量纲上（都是"字符"），字数门才有意义。
    #
    #   ★ 英文稿（cjk == 0 且 en_words > 0）走**词数门**，不用中文字数门：
    #     期刊对英文摘要的限制本来就是「words」（150/200/250 words），
    #     拿字数去卡英文稿必然误判。
    chars = cjk
    en_words = latin
    sent_lens = [count_chars(s) for s, _, _ in sents]
    avg_sent = round(sum(sent_lens) / len(sent_lens), 1) if sent_lens else 0
    max_idx = max(range(len(sent_lens)), key=lambda i: sent_lens[i]) if sent_lens else -1
    max_sent = sent_lens[max_idx] if max_idx >= 0 else 0
    de_ratio = round(text.count("的") / cjk, 3) if cjk else 0.0

    # ── 字数口径：剔除文末文献表（踩坑清单第 19 条）──────────
    # 期刊上限按「含表图、剔除文献表」核。若把文献表算进正文，
    # 一份正常长度的稿子会被判「超限」，模型于是去砍正文 —— 砍错地方，且不报错。
    refs_at = _refs_offset(text)
    chars_excl_refs = count_chars(text[:refs_at]) if refs_at > 0 else chars
    # ── 文献条目数（2026-10-24）──────────────────────────────
    # 用户要求：**综述的参考文献最少 50 篇**。
    # `has_refs` 只回答"有没有文献表"，回答不了"有几篇" —— 一篇综述
    # 列 3 条参考文献也满足 `has_refs=1`。要核"最少 50"，必须先能数。
    # 数法只认**文献表里以编号或 [编号] 开头的条目行**，以及 DOI/年份形态的行，
    # 不去数正文里的 [1] 引注 —— 二者会重复计数（同一篇被引三次成三篇），
    # 那比不数更糟。宁可少数，不出假数。
    refs_n = _count_ref_entries(text[refs_at:]) if refs_at > 0 else 0
    fig_nums = {int(n) for n in _FIG_CAP.findall(text)}
    tab_nums = {int(n) for n in _TAB_CAP.findall(text)}
    # 口径提示**只挂在字数问题上**，不单独成一条 issue ——
    # 否则它会平白给一份合规稿凑软问题，把人推进无谓的返工。
    _ref_note = ""
    if refs_at > 0:
        _ref_note = (f"；注意口径：本稿含参考文献表，剔除后正文 {chars_excl_refs} 字 —— "
                     "期刊上限通常按「含表图、剔除文末文献表」核算，若你的目标是上限，以正文数为准")

    # ── A. 体量 ─────────────────────────────────────────────
    # ★ 英文稿（没有中日韩字符、但有英文词）走**词数门**，不走中文字数门。
    #   判定依据是"这篇稿子用什么语言写的"，不是"字数够不够"。
    _is_english = (cjk == 0 and en_words > 0)

    if _is_english:
        # 英文稿：一篇正经摘要至少 20 词（等价于中文侧"不足一段话"的下限）
        if en_words < 20:
            issues.append(Issue(
                "hard", "empty",
                f"交付正文只有 {en_words} 个英文词，不足一段话",
                "把正文全文放进 draft_text（或写进工作区文件后用 draft_path 指过来）再交付"))
    elif chars < 20 and (chars + int(round(en_words * 1.6))) < 20:
        # ★ 混排稿要按**折算后**的篇幅判空，否则一份中文少、英文多的
        #   正常稿（例如图注体、方法摘要）会被判「不足一段话」。
        issues.append(Issue(
            "hard", "empty",
            f"交付正文只有 {chars + int(round(en_words * 1.6))} 字"
            f"（中文 {chars} 字 + {en_words} 个英文词折算），不足一段话",
            "把正文全文放进 draft_text（或写进工作区文件后用 draft_path 指过来）再交付"))

    chars_delta_pct = 0.0
    if target_chars:
        # ★ 英文稿与中文稿的「目标」单位不同：target_chars 是从用户原话里
        #   抽出来的**中文字数**（`约120字`）。英文稿不能拿它比 ——
        #   一比就必然「严重偏离」，模型于是去灌水凑字数。
        #
        # ★★ 2026-10-26 补一处**中英混排**的口径：
        #   中文稿里常夹着 AFLP / PEG / consolidated bioprocessing 这类英文，
        #   只数 CJK 会把它们**整个不算**（实测混排稿 chars=15 而实际内容不少），
        #   于是 length_far 判它「严重不足」，还建议「压缩到 255–345 字」——
        #   对一篇混排稿是纯粹的误导。
        #   按中文期刊的通行折算：**英文词 × 1.6 = 折算中文字数**，据此算总篇幅。
        _eq = chars + int(round(en_words * 1.6))   # 折算中文字数
        if _is_english:
            _measure, _unit = en_words, "词"
            _target_in_unit = int(target_chars / 1.6)
        else:
            _measure, _unit = _eq, "字"
            _target_in_unit = target_chars
        if _target_in_unit > 0:
            chars_delta_pct = (_measure - _target_in_unit) / _target_in_unit * 100
        d = abs(chars_delta_pct)
        _conv = ("（目标按 1 英文词 ≈ 1.6 中文字换算）" if _is_english else "")
        if not _is_english and en_words:
            # 混排稿：把折算过程写清楚，否则读者看到「15 字」会以为算错了
            _conv = (f"（本稿含 {en_words} 个英文词，按 ×1.6 折算为 "
                     f"{int(round(en_words * 1.6))} 字；中文 {chars} 字 + 折算 = {_measure} 字）")
        if d > 40:
            issues.append(Issue(
                "hard", "length_far",
                f"篇幅严重偏离目标：{_measure} {_unit} vs 目标约 "
                f"{_target_in_unit} {_unit}（{chars_delta_pct:+.0f}%）{_conv}",
                (f"扩写到 {int(_target_in_unit * 0.85)}–{int(_target_in_unit * 1.15)} "
                 f"{_unit}之间"
                 "（补证据、补机制、补限制，不要靠形容词灌水）"
                 if _measure < _target_in_unit else
                 f"压缩到 {int(_target_in_unit * 0.85)}–{int(_target_in_unit * 1.15)} "
                 f"{_unit}之间"
                 "（删套话与重复修饰，合并同义句）") + _ref_note))
        elif d > 15:
            issues.append(Issue(
                "soft", "length_off",
                f"篇幅偏离目标：{_measure} {_unit} vs 约 {_target_in_unit} {_unit}"
                f"（{chars_delta_pct:+.0f}%）{_conv}",
                f"微调到 {int(_target_in_unit * 0.9)}–{int(_target_in_unit * 1.1)} "
                f"{_unit}" + _ref_note))

    # ── B. 结构 ─────────────────────────────────────────────
    if not allow_lists:
        bullet_paras = [i for i, p in enumerate(paras, 1) if _BULLET.match(p)]
        if bullet_paras:
            issues.append(Issue(
                "hard", "bullet_prose",
                f"第 {'、'.join(map(str, bullet_paras[:6]))} 段起手是点列标记 —— "
                f"这是**成段论述**的稿子，不是清单",
                "把点列改写为连贯段落：把每个条目用「其一…其二…」或"
                "「首先…其次…」串进句子，或把并列内容压成一句带顿号的列举"))

    long_paras = [i for i, p in enumerate(paras, 1) if count_chars(p) > 800]
    if long_paras and len(paras) < 3:
        issues.append(Issue(
            "soft", "para_unbroken",
            f"全文仅 {len(paras)} 段，最长一段 {max(count_chars(p) for p in paras)} 字，"
            "读者无法喘息",
            "按论证层次切段，每段一个意思（中文论文单段 150–300 字较常见）"))

    # ── C. 句法 ─────────────────────────────────────────────
    if max_sent > _MAX_SENTENCE_HARD:
        s, pi, si = sents[max_idx]
        issues.append(Issue(
            "hard", "sentence_overlong",
            f"第 {pi} 段第 {si} 句长达 {max_sent} 字（上限 {_MAX_SENTENCE_HARD}）",
            f"拆成 2–3 句。原文开头：「{s[:34]}…」；"
            "在「其中」「该」「这一」「由此」处断开，每个新句只承载一个判断"))
    elif max_sent > _MAX_SENTENCE_SOFT:
        s, pi, si = sents[max_idx]
        issues.append(Issue(
            "soft", "sentence_long",
            f"第 {pi} 段第 {si} 句 {max_sent} 字偏长（建议 ≤{_MAX_SENTENCE_SOFT}）",
            "在第二个逗号处断句，或把从句提为独立句"))

    if len(sent_lens) >= 6 and max(sent_lens) - min(sent_lens) <= 8 and avg_sent > 0:
        issues.append(Issue(
            "soft", "rhythm_flat",
            f"全篇句长几乎一样（{min(sent_lens)}–{max(sent_lens)} 字），节奏机械",
            "长短句交替：关键判断单独短句成句，背景与方法用长句铺陈"))
    if de_ratio > 0.07:
        issues.append(Issue(
            "soft", "de_density",
            f"「的」占全文字数 {de_ratio:.1%}，定语层层套叠",
            "拆开「A 的 B 的 C」；能删的「的」直接删，把定语改成谓语小句"))
    if _TYPO_DOUBLE.search(text):
        hits = sorted(set(_TYPO_DOUBLE.findall(text)))
        issues.append(Issue(
            "hard", "typo_double",
            f"出现叠字笔误：{'、'.join(hits)}",
            "删掉多余的一个字（这是排版级错误，必须在交付前清掉）"))

    m = _REDUNDANT_FRAME.search(text)
    if m:
        all_hits = _REDUNDANT_FRAME.findall(text)
        issues.append(Issue(
            "soft", "redundant_frame",
            f"冗余动词框架 {len(all_hits)} 处，如「{m.group(0)}」",
            f"删掉框架词直接用作动词：「{m.group(0)}」→「{m.group(3)}」"
            "（同理：进行观察→观察、作出判断→判断、加以分析→分析）"))

    # ── D. 套话 ─────────────────────────────────────────────
    # 判定**看密度不看条数**：这是首轮实测暴露的失真 ——
    # 一段 135 字里塞 6 处套话（4.4/百字）本该是"通篇没说事"，
    # 但按条数计只算一条 soft，评分 96/100。而同样 6 处撒在 5000 字综述里
    # 只是行文习惯，不该重罚。同一个数字，两种性质，只能用密度区分。
    hits: list[tuple[str, str, int]] = []
    for pat, label, fix in _CLICHES:
        n = len(re.findall(pat, text))
        if n:
            hits.append((label, fix, n))
    cliche_total = sum(n for _, _, n in hits)
    cliche_density = round(cliche_total / max(chars / 100, 1), 2)
    if hits:
        listed = "、".join(f"{lab}×{n}" if n > 1 else lab for lab, _, n in hits[:6])
        more = f" 等 {len(hits)} 类" if len(hits) > 6 else ""
        dense = cliche_total >= 3 and cliche_density >= 2.0
        if dense:
            msg = (f"学术套话 {cliche_total} 处、密度 {cliche_density}/百字 —— "
                   f"全篇几乎由空转句构成：{listed}{more}")
            fix = (f"先处理{hits[0][0]}：{hits[0][1]}。"
                   f"这 {cliche_total} 处套话位必须换成具体事实、具体数字或可核验的引证；"
                   "删掉套话后字数若不够，用**证据与机制**补，而不是用形容词补")
        else:
            msg = f"学术套话 {cliche_total} 处（密度 {cliche_density}/百字）：{listed}{more}"
            fix = f"可优先处理{hits[0][0]}：{hits[0][1]}"
        issues.append(Issue("hard" if dense else "soft", "cliche", msg, fix))

    # ── E. 标点 ─────────────────────────────────────────────
    if _DOUBLE_PUNCT.search(text):
        issues.append(Issue(
            "hard", "punct_double",
            "出现连续标点（如「。。」「，，」）",
            "删掉重复标点；省略号统一用「……」"))
    half = _HALF_PUNCT.findall(text)
    if half:
        issues.append(Issue(
            "soft", "punct_halfwidth",
            f"中文语境里混用半角标点 {len(half)} 处（如「{half[0]}」）",
            "中文句内标点统一用全角：，。；：！？"))
    for a, b, name in (("“", "”", "弯引号"), ("「", "」", "中文角引号"),
                       ("『", "』", "中文双角引号"), ("（", "）", "全角括号")):
        if text.count(a) != text.count(b):
            issues.append(Issue(
                "soft", "punct_unpaired",
                f"{name}不配对：左 {text.count(a)} 个 / 右 {text.count(b)} 个",
                "补齐或删除多余的那一半"))

    # ── F. 句间重复 ─────────────────────────────────────────
    dup = _adjacent_repeat(sents)
    if dup:
        issues.append(Issue(
            "soft", "repeat",
            f"相邻句措辞重复：{dup}",
            "同一意思只说一次；后一句改成推进（给证据、给机制、给限制）"))

    # ── G. 引注 ─────────────────────────────────────────────
    # 引注写进正文，就必须可核验 —— 那一半由 gate 联动台账判定（见 gate.py 第 7 条）。
    # 这里管的是另一半：**编号顺序**。工作台的门禁只校验「[n] 的 n == 参考文献表条目序号」，
    # 那是自洽，不是期刊要求；于是逆序编号能一路全绿地交付出去。
    cites = cite_re.findall(text)
    num_cites = cite_num_re.findall(text)
    ay_cites = _CITE_AY.findall(text)
    cite_seq: list[int] = []
    if num_cites:
        seen: set[int] = set()
        for m in cite_num_re.finditer(text):
            for n in _expand_cite_group(m.group(1)):
                if n not in seen:
                    seen.add(n)
                    cite_seq.append(n)
        bad = [(cite_seq[i - 1], cite_seq[i]) for i in range(1, len(cite_seq))
               if cite_seq[i] < max(cite_seq[:i])]
        if bad:
            # 「这份正文是全文还是节选」决定它凭什么级别报警：
            # 从 [1] 起编 ⇒ 第一篇引注就在本文里，全局顺序**可判** → 硬门；
            # 最小号 >1 ⇒ 多半只是论文的某一节，编号从中间开始属正常 → 只提示。
            # 不许不分青红皂白地硬拦：那会逼模型去"修"一份本来正确的节选。
            starts_at_one = cite_seq[0] == 1
            a, b = bad[0]
            shown = "→".join(str(n) for n in cite_seq[:12])
            issues.append(Issue(
                "hard" if starts_at_one else "soft", "cite_order",
                f"引文编号未按正文首现顺序：先引 [{a}]，之后才引 [{b}]"
                f"（首现序 {shown}{'…' if len(cite_seq) > 12 else ''}）"
                + ("" if starts_at_one else
                   "。若这份正文只是节选，编号从中间开始属正常，可忽略"),
                "按纪律三件事**一起**做，缺一即错：① 重排参考文献表 ② 改号全部分节源稿 "
                "③ 改号表格里的 ref 列；改完重跑判定器 + 重建契约 + 重渲表格，"
                "并断言「新表第 i 条 == 旧表第 order[i] 条」"))
    if num_cites and ay_cites:
        issues.append(Issue(
            "soft", "cite_mixed",
            f"同一稿里混用两套引注制：数字制 {len(num_cites)} 处、"
            f"著者-出版年制 {len(ay_cites)} 处",
            "选定一套并统一。数字制 = 正文 [n] + 文末顺序编码表；"
            "著者-出版年制 = 正文（作者, 年份）+ 文末著者字母序表。混用会被期刊排版直接退回"))

    # ── H. 占位符 / 模板残留（硬）──────────────────────────
    # 唯一一类「看一眼就报废」的错误：审稿人看到 TODO 不会写"建议补充"，它会直接退稿。
    # 而它偏偏不会让任何一步失败 —— 导出、门禁、字数统计全都照样通过。
    ph = _PLACEHOLDER.findall(text)
    if ph:
        uniq = sorted(set(ph))
        where = [i for i, p in enumerate(paras, 1) if _PLACEHOLDER.search(p)]
        issues.append(Issue(
            "hard", "placeholder",
            f"正文残留占位符/模板痕迹 {len(ph)} 处："
            + "、".join(f"「{u}」" for u in uniq[:5])
            + (f"（最先出现在第 {where[0]} 段）" if where else ""),
            "交付前逐处清掉。若某处是「查不到、还没做」，**不要留空、不要留 TODO** —— "
            "按纪律如实写成 undetermined_requires_fulltext 或「由标题推导（未经核验）」，"
            "并说明缺什么。填得好看比留白更糟"))

    # ── I. 工具链内行话泄漏进正文（软）──────────────────────
    jar = _JARGON.findall(text)
    if jar:
        uniq = sorted({j.lower() for j in jar})
        issues.append(Issue(
            "soft", "jargon",
            f"正文出现工具链/内部字段名 {len(jar)} 处："
            + "、".join(f"「{u}」" for u in uniq[:5]),
            "这些词属于**流水线内部**，不该出现在稿件里。逐处改写成读者能懂的说法"
            "（draft_path → 稿件文件、call_seq → 调用编号）。"
            "若这篇稿子本身就在研究 agent 工具链，忽略本条"))

    # ── J. 多段结尾同构（软）────────────────────────────────
    # 加章节、补图表时最容易长出来的病：每段都用同一个尾巴收束，读起来像填表。
    # 它不是错句 —— 单看每段都通顺，只有**横向比**才看得出来。
    tail4, idx4 = _tail_repeat(paras, 4, 3)
    tail2, idx2 = _tail_repeat(paras, 2, 4)
    if tail4:
        issues.append(Issue(
            "soft", "tail_same",
            f"第 {'、'.join(map(str, idx4))} 段结尾同构，都以「{tail4}」收束",
            "让并列段各自落在不同的落点上（一段给数字、一段给机制、一段给限制），"
            "或把这几个尾巴砍掉，改成一段带层次的论述"))
    elif tail2:
        issues.append(Issue(
            "soft", "tail_same",
            f"第 {'、'.join(map(str, idx2))} 段以同样的「{tail2}」两字收束",
            "若是有意的排比可忽略；若不是，把其中几段的落点换掉"))

    # ── K. 绝对化表述（软）──────────────────────────────────
    abs_strong = _ABS_STRONG.findall(text)
    abs_weak = _ABS_WEAK.findall(text)
    if abs_strong or len(abs_weak) >= 3:
        seen_abs: list[str] = []
        for h in abs_strong + abs_weak:
            if h not in seen_abs:
                seen_abs.append(h)
        n_abs = len(abs_strong) + len(abs_weak)
        issues.append(Issue(
            "soft", "absolute",
            "绝对化表述：" + "、".join(f"「{h}」" for h in seen_abs[:5])
            + (f"（共 {n_abs} 处）" if n_abs >= 2 else ""),
            "学术论断要留出反例空间：把「完全/必然/从未/毫无疑问」换成带条件的表述"
            "（「在 X 条件下」「就本次样本而言」），或明确给出证据覆盖的范围"))

    # ── L. 英文句长（软 35 词 / 硬 70 词）───────────────────
    # 中文稿里的英文摘要、以及纯英文稿都走这里。先筛出「一句里没有一个汉字」的句子，
    # 再用**词数**判长 —— 用字符数判英文，等于把 35 词的阈值挪到 200 字符上去。
    en = [(len(_LATIN_WORD.findall(s)), pi, si)
          for s, pi, si in sents if not _CJK.search(s)]
    over = sorted([x for x in en if x[0] > _EN_LONG_SOFT], reverse=True)
    if over:
        w, pi, si = over[0]
        issues.append(Issue(
            "hard" if w > _EN_LONG_HARD else "soft", "en_sentence_long",
            f"第 {pi} 段第 {si} 句英文长达 {w} 词"
            f"（软线 {_EN_LONG_SOFT}、硬线 {_EN_LONG_HARD}）"
            + (f"，另有 {len(over) - 1} 句超线" if len(over) > 1 else ""),
            "在分号、which/that 从句、and 并列处断句，一句只承载一个判断。"
            "期刊语言检查常按 35 词零容忍处理"))

    # ── 防御性写作（2026-10-26，乙派口径）──────────────────────
    #
    # 与上面所有检查的分工：上面的查的是「**空的**」话（套话、冗余框架），
    # 这一段查的是「**不空但在跟假想审稿人吵架**」的话。
    #
    # 一律 soft，且**每类最多报 2 处**。理由：
    #   · 防御性写作有大量正当用法（真局限、伦理声明、必要免责），
    #     当硬门会逼模型删掉本该留的东西；
    #   · 乙派铁律是「只删姿态、不删事实」——含数字/合规/局限的句子，
    #     检出里会带 protected 标记，改法自动换成「只改姿态，不许整句删」。
    # 详见 rongke/defensive.py 的模块注释。
    defensive_hits: list[dict] = []
    try:
        from . import defensive as _defensive
        defensive_hits = _defensive.scan(text)
    except Exception:            # 辅助层不许把主流程带崩
        defensive_hits = []
    for h in defensive_hits:
        # 只把「典型」的那几条升级成建议条目；弱信号只进 metrics 计数，
        # 避免一张稿子刷出十几条把真问题淹没。
        if not h["strong"]:
            continue
        issues.append(Issue(
            "soft", h["code"],
            f"{h['label']}：「{h['sentence']}」",
            h["fix"]))

    metrics = {
        # ★ `chars` 现在**只数中日韩字符**（与 count_chars 同为"字符"量纲）；
        #   英文词数单独放在 `en_words`。原先 `chars` 把词加到字上，量纲混算，
        #   会让一篇 143 词的英文摘要被判「字数严重不足」。
        # ★ `chars_eq` 是**折算后的总篇幅**（中文 + 英文词×1.6），
        #   字数门与"体量"展示都用它，这样混排稿不会被少算。
        "chars": chars, "cjk": cjk, "en_words": en_words,
        "chars_eq": chars + int(round(en_words * 1.6)),
        "latin_words": latin,
        "lang": "en" if _is_english else "zh",
        "paragraphs": len(paras), "sentences": len(sents),
        "avg_sentence": avg_sent, "max_sentence": max_sent,
        "min_sentence": min(sent_lens) if sent_lens else 0,
        "de_ratio": de_ratio,
        "cliche_hits": cliche_total, "cliche_density": cliche_density,
        "halfwidth_punct": len(half), "citations": len(cites),
        "chars_delta_pct": round(chars_delta_pct, 1),
        "bullet_paras": len(bullet_paras) if not allow_lists else 0,
        # 本轮新增：可机查的那几条踩坑记录
        "placeholders": len(ph),
        "cite_numbers": len(num_cites), "cite_author_year": len(ay_cites),
        "cite_first_order": cite_seq,
        "jargon_hits": len(jar),
        "tail_repeat": max(len(idx4), len(idx2)),
        "absolutes": len(abs_strong) + len(abs_weak),
        "max_en_words": max((x[0] for x in en), default=0),
        # 防御性写作：总命中 / 典型命中 / 含保护信号（禁删事实）的条数
        "defensive_hits": len(defensive_hits),
        "defensive_strong": len([h for h in defensive_hits if h["strong"]]),
        "defensive_protected": len([h for h in defensive_hits if h["protected"]]),
        # 字数口径（踩坑清单第 19 条）：期刊上限按「含表图、剔除文献表」核
        "chars_excl_refs": chars_excl_refs,
        "has_refs": 1 if refs_at > 0 else 0,
        "refs_n": refs_n,
        "figures": len(fig_nums), "tables": len(tab_nums),
    }
    # 评分是**两段式**的：有硬问题必落 <70 区，无硬问题落 ≥70 区。
    # 原先是 `100 - 15×硬 - 4×软` 的连续扣分，于是「必修：全篇套话」会和
    # 81/100 一起显示 —— 判定说「必须改」、分数说「挺好」，自相矛盾。
    # 分数在 UI 上就是一个信号，信号必须与判定同向，否则人会先信那个漂亮的数字。
    hard_n = len([i for i in issues if i.level == "hard"])
    soft_n = len([i for i in issues if i.level == "soft"])
    score = (max(20, 60 - 10 * (hard_n - 1)) if hard_n
             else max(70, 100 - 4 * soft_n))
    return Report(metrics=metrics, issues=issues, score=score,
                  target_chars=target_chars, display_unit=disp_unit)


def _adjacent_repeat(sents: list[tuple[str, int, int]], n: int = 4) -> str:
    """相邻两句有 ≥6 字的连续重合片段 → 判定重复。返回重合片段。"""
    prev = ""
    for s, _, _ in sents:
        clean = re.sub(r"[\s，。；：！？、（）()「」“”\"']", "", s)
        if len(prev) >= n and len(clean) >= n:
            for i in range(len(clean) - 5):
                frag = clean[i:i + 6]
                if frag in prev:
                    return f"「{frag}」"
        if len(clean) >= n:
            prev = clean
    return ""


def _tail_repeat(paras: list[str], n: int, need: int) -> tuple[str, list[int]]:
    """段落收尾是否同构 —— 取每段末尾 n 个字符（先去尾标点），
    返回出现次数 ≥`need` 的尾巴及其段号；没有就返回 ("", [])。

    为什么用「末尾 n 字」而不是「末尾分句」：末尾分句长短不一，
    但模板化的段落**恰恰在每个分句的不同处对不上、在最后几个字上一致**。
    n=4 抓「具有重要意义 / 提供了参考」，n=2 抓「……作用 / ……意义」这种更松的同构。
    """
    tails: list[tuple[int, str]] = []
    for i, p in enumerate(paras, 1):
        s = re.sub(r"[\s。！？；：…，、,.!?;:\"'）)」』】\]]+$", "", p)
        s = s.rstrip("-*#")
        if len(s) >= n:
            tails.append((i, s[-n:]))
    for t, c in Counter(t for _, t in tails).most_common():
        if c >= need:
            return t, [i for i, tt in tails if tt == t]
    return "", []


def lint_file(path, target_chars: int | None = None,
              allow_lists: bool = False) -> Report:
    """从文件读稿体检（draft_path 用）。读不到就返回一条 hard。"""
    from pathlib import Path
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        r = lint("", target_chars, allow_lists)
        r.issues.insert(0, Issue("hard", "draft_unreadable",
                                 f"读不到稿件文件 {p}：{type(e).__name__}: {e}",
                                 "确认路径相对于本次任务的工作区，且文件真的写进去了"))
        return r
    return lint(text, target_chars, allow_lists)
