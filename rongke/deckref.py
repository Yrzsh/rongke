"""参考样例 —— 一份**完整且可复现**的组会 deck，作为 `compose_deck` 的示范。

## 为什么要有这个模块（以及为什么它不是一张图）

用户的方向纠正里有一句关键的话：

    人家同样是用 agent，只加了个 skill 就能做出好看的图了，
    而你在这里做了半天，依旧不好看。

那份"skill"里真正起作用的，是**参考样例**：不是形容词（"简洁大气"），
而是**一个可以照着比对的成品**。

### ⚠️ 但这个内核的模型**看不见图**（2026-09-21 实测）

`llm.py` 的请求体把 `content` 硬编码成**字符串**；`loop.py` 的
`_messages_chars()` 只处理 `isinstance(c, str)`；全仓 `image_url` /
`input_image` / `data:image` **零命中**。工具结果一律经 `tools.py` 的
`json.dumps` 字符串化回灌。

⇒ **给模型一张 PNG 等于没给。** 参考样例要起作用，必须是**文本的、
完整的、可抄的**：页型序列 + 每页标题 + 每页正文行。

    这正是 `docs/ppt-skill-survey-*.md` 里那些"好 skill"真正的形态 ——
    它们看上去在讲设计原则，实际起作用的是一整段**照着填的例子**。

### 所以这里装的是**数据**，不是散文

`REFERENCE_DECK` 是一份 12 页 deck 的**完整内容**（页型 + 标题 + 正文行）。
它同时是三样东西的**唯一来源**：

  ① 构建脚本 `deck_reference_build.py` 照着它造源稿；
  ② `reference_text()` 把它渲染成**给模型读的示范**（文本，模型读得懂）；
  ③ `deck_test` §24 用它核对**随库发布的那份 .pptx 没漂**。

⇒ 内容改一处，构建脚本与工具说明**同时**跟着变。
   在别处再抄一份正文 = H 型（改一处忘一处，静默漂移）。

### 三条纪律（`design.py` 与判据都不管的那一层）

`design.py` 管**几何**（版面网格、字号阶梯、色彩角色），判据管**下限**
（不越界、不超字号、标题不是名词短语堆）。中间还有一层谁也管不着的：

  · **一页一个论点**。要点 3 条为佳，最多 4 条 —— 超过就拆页，
    而不是把字号调小（字号是阶梯上的定值，不许为了塞内容而破格）。
  · **标题写判断句**，不写「结果」「讨论」「分析」这类**标签**。
    判据 `titles_are_noun_phrases` 会 WARN 的正是"满页名词短语"。
  · **图表页的标题就是结论**（「原基出现率：温度不够可以用时间补」），
    不是「结果图 1」。读者只看标题就该知道这页在说什么。

⚠️ 这三条**没法写成判据**（"是不是判断句"判不准），所以只能靠**样例**传。
   这就是"参考样例"不可替代的那部分价值。
"""

from __future__ import annotations

from pathlib import Path


# ══════════════════════════════════════════════════════════════════════
# ① 随库发布的产物路径
# ══════════════════════════════════════════════════════════════════════
#
# 目录放在 `docs/` 下是**有意的**：`.gitignore` 开篇写明
# 「进库的只有源码 + 测试 + 文档 + **可再生的参考产物**」——
# 这份 deck 恰好是"可再生的参考产物"，进库是照章办事，不是误入库。
#: 仓库根（本文件在 <root>/rongke/deckref.py）。
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

#: 参考产物目录。
REFERENCE_DIR: Path = REPO_ROOT / "docs" / "deck-reference"

#: 成品 deck（16:9 + 绢青陈墨 + 全页型）。
REFERENCE_PPTX: Path = REFERENCE_DIR / "reference_v1.pptx"

#: 成品的分页预览图（**给人看的**；模型看不见图，见模块 docstring）。
REFERENCE_SHEET_PNG: Path = REFERENCE_DIR / "reference_v1.sheet.png"

#: 前后对照图（上=未排版源稿、下=精排成品）。回答「那能做的这么丑」的那张图。
REFERENCE_COMPARE_PNG: Path = REFERENCE_DIR / "reference_v1.compare.png"

#: 未排版的源稿（4:3 + Office 默认主题）——留档用，用来演示"前后差在哪"。
REFERENCE_SRC_PPTX: Path = REFERENCE_DIR / "reference_v1.src.pptx"

#: 工具说明的落盘副本，由 `reference_text()` 生成。
#: **不手写** —— 手写就是第二处来源，必然会和代码漂开。
REFERENCE_MD: Path = REFERENCE_DIR / "reference_v1.md"

#: 主题。
REFERENCE_THEME: str = "juanqing"

#: 页脚落款。
REFERENCE_FOOTER_LABEL: str = "低温诱导冬虫夏草子实体分化 ｜ 第三次组会"


# ══════════════════════════════════════════════════════════════════════
# ② 参考 deck 的完整内容（**唯一来源**）
# ══════════════════════════════════════════════════════════════════════
#
# 每条：
#   kind   —— `design.PAGE_KINDS` 的键
#   title  —— 页面主标题（含封面/章节/收束页）
#   note   —— **讲稿备注**（`set_slide_notes` 写进备注栏的那一段）
#   lines  —— 该页其余的文本块，**按落位顺序**：
#               cover    → [副标题, 署名]
#               section  → [说明, 巨型编号]
#               content  → [正文（多行，一条一段）]
#               two-col  → [左栏正文, 右栏正文]
#               chart    → [右侧读数要点]      ← 另有 chart=True
#               closing  → [正文]
#   chart  —— True ⇒ 这一页要放一个**原生图表**（不是图片）。
#             ★ 故意用原生图表：`compose_deck` 的承重承诺之一就是
#               "重排不丢图表"。图片与图表走的是两条不同的形状路径
#               （`PICTURE` vs `graphicFrame`），只测图片证明不了图表。
#
# ⚠️ `note` 不是可选装饰。判据 `notes_coverage` 要 ≥60%，
#    而**一份没有讲稿的组会 deck 本来就是半成品**：备注栏里放的
#    （来源、口径、会被问到的问题）正是汇报时真正要说的东西。
#    没有它，deck 只是给人看的摘要。
#
# 正文用的行首符号：**不加 `·`**。项目符号由版面/版式负责，
# 写进正文会让"换一份版式就多一层点"（同一份内容两种呈现）。
REFERENCE_DECK: list[dict] = [
    # ── 1 ────────────────────────────────────────────────────────────
    {
        "kind": "cover",
        "title": "低温诱导冬虫夏草子实体分化：阈值与时长之争",
        "note": "开场三句话：① 这是第三次组会；② 前两次已确认低温是启动信号，但条件没被定量；"
                "③ 本次要回答的是「多低、多久」。题目里的「之争」是刻意的 —— 现在的文献确实矛盾，"
                "不是我漏读了。",
        "lines": [
            "第三次组会汇报 ｜ 资源化利用方向",
            "汇报人：— ｜ 2026-09-21",
        ],
    },
    # ── 2 ────────────────────────────────────────────────────────────
    {
        "kind": "section",
        "title": "卡点：启动条件没被定量",
        "note": "立卡点：真正的卡点不是「低温有没有用」，而是「到底多低、多久」从来没被定量。"
                "不定量 ⇒ 换个实验室就复现不了。本页只做一件事："
                "让所有人同意问题出在「定量」上。",
        "lines": ["启动条件不定量，诱导就无法标准化", "01"],
    },
    # ── 3 ────────────────────────────────────────────────────────────
    {
        "kind": "content",
        "title": "适宜温度报道分散在 4–15℃",
        "note": "依据：汇总了 12 篇文献给出的适宜温度，区间 4–15℃。"
                "会被问：是不是培养基或菌株口径不同？答：口径差异正是要排除的变量之一，"
                "见研究设计那一页。",
        "lines": [
            "汇总 12 篇文献，报道的适宜诱导温度从 4℃ 到 15℃，跨度 11℃\n"
            "各家只报自家有效的温度点，没有一份在同一菌株上做跨温度对比\n"
            "⇒ 阈值区间始终不明确，同一套参数换个实验室就复现不了\n"
            "差异可能出在培养基配方与菌株口径 —— 这正是要先排除的变量（见设计页）",
        ],
    },
    # ── 4 ────────────────────────────────────────────────────────────
    {
        "kind": "two-col",
        "title": "同一现象，两个相反结论",
        "note": "这一页的作用是把矛盾**摆在台面上**，不是当场解释掉它。"
                "两组结论都出自可重复的实验 ⇒ 分歧不在操作手法，在实验条件。",
        "lines": [
            "一组：4℃ 下稳定诱导出原基\n"
            "14d 出现率 78%，三次独立重复趋势一致\n"
            "→ 低温是必要条件",
            "另一组：8℃ 下几乎无响应\n"
            "14d 出现率 61%，延长到 21d 才升到 74%\n"
            "→ 低温只是加速因子",
        ],
        # 反例派生源：DENSITY_NEGATIVE_EXAMPLE 取本页每侧第一条（见 ③ 节）。
        # 改动本页时保留「每侧至少两条」，否则反例与正例退化成同一页。
    },
    # ── 5 ────────────────────────────────────────────────────────────
    {
        "kind": "section",
        "title": "假设与设计",
        "note": "过渡页：把「温度之争」翻译成一个**可检验的预测**。"
                "翻页前停一下，确认大家接受这个翻译，再往下讲设计。",
        "lines": ["把「温度之争」转成一个可检验的预测", "02"],
    },
    # ── 6 ────────────────────────────────────────────────────────────
    {
        "kind": "content",
        "title": "我们的判断：差异来自诱导时长不受控",
        "note": "本次汇报的核心假设。会被问：怎么排除菌株差异？"
                "答：同一保藏菌株 QZ-11、同一批斜面活化两代，三次重复错开培养基批次。",
        "lines": [
            "两组差异可能不在温度本身，而在诱导时长没有被控制\n"
            "若成立：温度不足可以用时间补 —— 8℃/21d 的 74% 已接近 4℃/14d 的 78%\n"
            "可检验预测：低温度 + 长时长 ≈ 高温度 + 短时长，两条路径的出现率应收敛\n"
            "菌株口径先排除在外：同一保藏菌株、同一批斜面活化两代，重复错开培养基批次",
        ],
    },
    # ── 7 ────────────────────────────────────────────────────────────
    {
        "kind": "content",
        "title": "研究设计：3 温度 × 3 时长，每组 120",
        "note": "样本量 120/组是按 78% vs 61% 的差异、α=0.05、power=0.8 反推的，"
                "现场可以给计算过程。会被问：为什么不做 10℃？"
                "答：先把 4/8/12 三点定下来，下一轮再把阈值收窄到 2℃ 以内。",
        "lines": [
            "菌株：实验室保藏 QZ-11，同一批斜面活化两代后接种，排除菌种口径差异\n"
            "处理：4℃/8℃/12℃ × 7d/14d/21d，共 9 组，温度与时长全交叉\n"
            "样本量：每组 120，按 78% vs 61% 的差异、α=0.05、power=0.8 反推\n"
            "重复：三次独立重复、错开培养基批次；10℃ 留到下一轮收窄阈值时再补",
        ],
    },
    # ── 8 ────────────────────────────────────────────────────────────
    {
        "kind": "chart",
        "title": "原基出现率：温度不够可以用时间补",
        "note": "读图顺序：先看 12℃ 全零（⇒ 低温必要），再看 8℃ 从 61% 升到 74%（⇒ 时间能部分补偿）。"
                "会被问：78% 与 74% 差 4 个点算差异吗？答：不显著 —— "
                "这正是措辞用「接近」而不是「追平」的依据。",
        "chart": True,
        "lines": [
            "4℃ 与 8℃ 组 14d 后出现原基，12℃ 组全程未出现\n"
            "出现率：4℃ 78%、8℃ 61%、12℃ 0%\n"
            "8℃ 延长到 21d：出现率升至 74%，接近 4℃ 的 14d 水平",
        ],
    },
    # ── 9 ────────────────────────────────────────────────────────────
    {
        "kind": "chart",
        "title": "转化率随诱导时长单调上升",
        "note": "这页用转化率而不是出现率：出现率只说明「启动了」，转化率才说明「能收获多少」。"
                "三次重复趋势方向一致，绝对值有批次波动 —— 图上的误差棒是三次重复的标准差。",
        "chart": True,
        "lines": [
            "三个温度下均随时长上升，趋势一致\n"
            "4℃ 21d 最高，12℃ 始终接近零\n"
            "三次独立重复的趋势方向一致",
        ],
    },
    # ── 10 ───────────────────────────────────────────────────────────
    {
        "kind": "two-col",
        "title": "延长诱导有代价，但方向是清楚的",
        "note": "代价是真实的，不回避：同步度下降意味着收获要分批做。"
                "推测部分**明说是推测**，不做过度解读。会被问：同步度怎么算的？"
                "答：同批次内原基出现天数的标准差取倒数。",
        "lines": [
            "代价：分化同步度下降\n14d 的 0.81 → 21d 的 0.62\n"
            "收获要分批做：从一次收到两到三批，人工成本上升",
            "推测：低温作用于早期开关\n12℃ 组菌丝生长最快，"
            "却始终停在营养生长阶段\n与文献「低温启动」的定性描述一致",
        ],
    },
    # ── 11 ───────────────────────────────────────────────────────────
    {
        "kind": "content",
        "title": "现有证据只到表型层面",
        "note": "这一页是**主动**划边界，不是等被问才承认。会被问：那你怎么排除底物供给变化？"
                "答：排不除，所以下一轮加代谢组。",
        "lines": [
            "以上都是表型证据：出现率、转化率、同步度，尚未鉴定到转录因子或调控节点\n"
            "措辞边界：只能说观测结果「与低温启动一致」，不能说「已经证明开关存在」\n"
            "替代解释：低温可能只是改变底物供给（碳氮比变化），这一轮的设计排除不掉它\n"
            "对应的补法：下一轮代谢组查底物供给，转录组找调控节点，两条各管一头",
        ],
    },
    # ── 12 ───────────────────────────────────────────────────────────
    {
        "kind": "closing",
        "title": "下一步：收窄阈值，锁定开关基因",
        "note": "收尾给明确的下一步与时间点。会被问：转录组什么时候送样？"
                "答：阈值实验（4/8/10/12℃ × 14d）出结果后两周内，预计下月中旬。",
        "lines": [
            "局限：单一菌株；控温精度 ±0.5℃；未测代谢产物；成本口径未纳入\n"
            "局限：证据只到表型层面，底物供给的替代解释这一轮排不除\n"
            "计划一：加做 10℃，4/8/10/12℃ × 14d，把阈值收窄到 2℃ 以内\n"
            "计划二：阈值结果出来后两周内转录组送样，锁定候选开关基因\n"
            "口径：出现率看启动、转化率看收获、同步度看分批成本",
        ],
    },
]

#: 交给 `compose_deck` 的 `pages` 参数（由 `REFERENCE_DECK` 派生 ⇒ 单源）。
REFERENCE_PAGES: list[dict] = [{"kind": x["kind"]} for x in REFERENCE_DECK]


# ══════════════════════════════════════════════════════════════════════
# ③ 页型 → 适合装什么内容（**文本模型真正缺的那张表**）
# ══════════════════════════════════════════════════════════════════════
#
# `design.PAGE_KINDS` 说的是"这一页几何长什么样"；
# 这张表说的是"**什么内容该往哪一页放**"。模型缺的是后者。
KIND_CONTENT_SHAPE: dict[str, str] = {
    "cover": "标题（≤ 20 字，压成判断句或「X：Y 之争」式）"
             " + 一行副标题（第几次组会 · 方向）+ 一行署名。**不要放要点。**",
    "section": "一个章节名（≤ 14 字）+ 一行说明 + 一个巨型编号（01 / 02）。"
               "用来切 2–3 个大段。**一页只放一件事。**",
    "content": "一个论点当标题（判断句，≤ 18 字）+ 3–4 条支撑要点，"
               "每条写满一到两行、至少带一个数或口径。**一页一个论点**；"
               "要点到 4 条以上就拆页。",
    "two-col": "一个对比式标题（「X 与 Y」/「同一现象，两个相反结论」）"
               "+ 左右各 2–3 条。**只用于真对比**（两组结论、代价 vs 收益、"
               "现状 vs 计划）。左右条数尽量相等。",
    "chart": "一个**结论式**标题（「原基出现率：温度不够可以用时间补」）"
             "+ 主视觉放左 2/3（图 / 表 / 原生图表）+ 右侧 1–3 条读数要点。"
             "**标题写结论，不写「结果图 1」。**",
    "closing": "一句收束（「下一步：X」）+ 局限与计划各若干条。"
               "**不要写「谢谢」** —— 判据 `ends_with_thanks` 会 WARN，"
               "而且组会汇报的末页应当留给下一步。",
}

#: `design.py` 与判据都管不着、只能靠样例传的三条。（判据判不准"是不是判断句"。）
LAYOUT_RULES: tuple[str, ...] = (
    "一页一个论点。要点 3 条为佳、最多 4 条 —— 超过就拆页，"
    "**不要为了塞内容去调小字号**（字号是阶梯上的定值）。",
    "标题写判断句，不写「结果」「讨论」「分析」这类标签 —— "
    "判据 `titles_are_noun_phrases` 抓的正是「满页名词短语」。",
    "图表页的标题就是结论。读者只看标题就该知道这页在说什么。",
)


def density_bands() -> tuple[int, int, int]:
    """门限的三个百分数（15 / 25 / 40）。

    ★ **从 `deck.py` 现读**，不在这儿另抄一份 —— 抄一份就是第二处真相：
      哪天主控把阈值挪了，这份说明会安静地变成假话（H 型）。
    """
    from . import deck as _D                      # noqa: PLC0415（延迟导入防成环）
    return (round(_D.DENSITY_HIGH_MAX * 100),
            round(_D.DENSITY_MEDIUM_MAX * 100),
            round(_D.DENSITY_LOW_MAX * 100))


#: 逐页型密度落点的**第三列**（"要往上抬该动什么"）。
#: 这一列是**人工建议**、不是事实，所以手写没有第二处真相的问题；
#: 第二列（实测区间）**绝不手写** —— 见 `density_per_kind()`。
_DENSITY_RAISE_HOW: dict[str, str] = {
    "content": "每条写满一到两行，带上数字、口径或边界条件，别停在半行；"
               "散点太多就把其中一条换成一张图。",
    "two-col": "左右各三条、条数相等、每条带一个具体数字；只有一侧有料就改用 content。",
    "chart": "图吃满左侧主视觉位才有这个数。图缩小一半，这一页直接掉回 40% 以下。",
    "closing": "局限与计划各两到三条、每行写满。只写一句「谢谢」会掉到 10% 以下。",
    "cover / section": "内核**豁免**这两种页型：一页一件事、留白是有意的。",
}


def measured_density() -> dict[str, list[float]] | None:
    """**从成品 .pptx 现算**的逐页型实测利用率（`{页型: [util, …]}`）。

    ★ 为什么必须现算（W6 的 H 型教训）：这一列数字原来手抄在
      `DENSITY_PER_KIND` 里，样例一改它就变成假话，而且**没有任何东西会红**。
      现算走 `deck.probe_pptx`（纯标准库读 zip）—— 与 `density_bands()`
      从内核现读阈值是同一种做法。
    成品不在 / 量不到几何 ⇒ 返回 None（"没量"，调用方要如实说，不许编数）。
    """
    from . import deck as _D                      # noqa: PLC0415（延迟导入防成环）
    if not REFERENCE_PPTX.is_file():
        return None
    facts = _D.probe_pptx(REFERENCE_PPTX)
    out: dict[str, list[float]] = {}
    for s in facts.slides:
        kind = str(s.get("kind") or "")
        util = s.get("util")
        if not kind or kind in _D.DENSITY_EXEMPT_KINDS or util is None:
            continue
        out.setdefault(kind, []).append(float(util))
    return out or None


def density_per_kind() -> tuple[tuple[str, str, str], ...]:
    """`KIND_CONTENT_SHAPE` 那一节的密度表：`(页型, 实测落点, 怎么往上抬)`。

    第二列**由 `measured_density()` 现算**（如 `31–38%（4 页）`），
    成品不在时明说"未实测"，绝不留一个手抄的旧数在这里。
    """
    meas = measured_density() or {}
    rows: list[tuple[str, str, str]] = []
    for kind in ("content", "two-col", "chart", "closing"):
        vals = meas.get(kind) or []
        if not vals:
            band = "未实测（先跑 `deck_reference_build.py`）"
        else:
            lo, hi = min(vals) * 100, max(vals) * 100
            one = round(lo) == round(hi)
            band = (f"{hi:.0f}%" if one else f"{lo:.0f}–{hi:.0f}%") + f"（{len(vals)} 页）"
        rows.append((kind, band, _DENSITY_RAISE_HOW[kind]))
    rows.append(("cover / section", "不量", _DENSITY_RAISE_HOW["cover / section"]))
    return tuple(rows)


def _example_two_col() -> "dict | None":
    """反例/正例共用的源页：**样例里第一张 two-col**（派生，不写死第几页）。"""
    return next((x for x in REFERENCE_DECK if str(x.get("kind")) == "two-col"), None)


def _para_blocks(block: object) -> list[str]:
    """一个文本块拆成非空段（two-col 的一栏 ⇒ 该栏的 2–3 条要点）。"""
    return [p for p in str(block).split("\n") if p.strip()]


_EXAMPLE_PAGE: "dict | None" = _example_two_col()

#: 源页在 deck 里的页码（派生）。源页缺失时为 0，渲染处自行退让。
EXAMPLE_PAGE_NO: int = next((i for i, x in enumerate(REFERENCE_DECK, 1)
                             if x is _EXAMPLE_PAGE), 0)

#: 反例：**把上面那张 two-col 页（第 `EXAMPLE_PAGE_NO` 页）左右各砍剩第一条**。
#: ★ 由 `REFERENCE_DECK` **派生**（W6 前是手抄第 4 页正文 —— 第 4 页改好后
#:    那份手抄立刻变成假话，正是本仓 docstring 反复警告的 H 型）。
#:    守卫在 `deck_test` §24.29：三行必须逐行真的出现在样例正文里。
DENSITY_NEGATIVE_EXAMPLE: tuple[str, ...] = (
    (f"标题：{_EXAMPLE_PAGE['title']}",
     f"左：{_para_blocks(_EXAMPLE_PAGE['lines'][0])[0]}",
     f"右：{_para_blocks(_EXAMPLE_PAGE['lines'][1])[0]}")
    if _EXAMPLE_PAGE is not None and len(_EXAMPLE_PAGE.get("lines") or []) >= 2
    else ()
)


# ══════════════════════════════════════════════════════════════════════
# ④ 渲染成**给模型读的**文本
# ══════════════════════════════════════════════════════════════════════

def kind_sequence() -> list[str]:
    """参考 deck 的页型序列（校验用：必须六种页型都出现）。"""
    return [str(x["kind"]) for x in REFERENCE_DECK]


def reference_text(summary: bool = False) -> str:
    """把参考样例渲染成文本。

    `summary=True` ⇒ 只给页型序列与逐页标题（模型快速比对用，短得多）；
    缺省 ⇒ 连同**每页正文**一起给（照着填的完整示范）。

    两条都从 `REFERENCE_DECK` 生成 ⇒ 与随库发布的 .pptx 天然同源。
    """
    n = len(REFERENCE_DECK)
    lines: list[str] = []
    lines.append(f"# 参考样例：一份 {n} 页的组会汇报 deck")
    lines.append("")
    lines.append("下面是**本体**（内核提供的 `compose_deck` 就是照这个标准排的）。"
                 "它六种页型全用到，可直接照抄结构。")
    lines.append("")

    if summary:
        for i, it in enumerate(REFERENCE_DECK, 1):
            lines.append(f"{i:>2}. [{it['kind']}] {it['title']}")
    else:
        for i, it in enumerate(REFERENCE_DECK, 1):
            lines.append(f"## 第 {i} 页 · 页型 `{it['kind']}`")
            lines.append(f"标题：{it['title']}")
            for ln in it.get("lines", []):
                for one in str(ln).split("\n"):
                    lines.append(f"    {one}")
            if it.get("chart"):
                lines.append("    （此页另有一个原生图表放左侧主视觉位）")
            note = str(it.get("note") or "").strip()
            if note:
                lines.append("")
                lines.append("讲稿备注（写进备注栏的那一段）：")
                for one in note.split("\n"):
                    lines.append(f"    {one}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 页型 → 适合装什么内容")
    lines.append("")
    for k, v in KIND_CONTENT_SHAPE.items():
        lines.append(f"- **`{k}`**：{v}")
    lines.append("")
    lines.append("## 三条版面纪律（判据管不着，只能靠样例传）")
    lines.append("")
    for r in LAYOUT_RULES:
        lines.append(f"- {r}")
    lines.append("")

    # ── 密度标准（W5 · 2026-09-27）─────────────────────────────
    # ★ 为什么单独一节：`audit_deck` 从 W5 起**真的会量**版心利用率并出 WARN。
    #   标准不写进模型读的那段文本，它就只能靠猜 —— 而它上一版猜出来的
    #   是每页三条短句、下半页全空（W6 之前这份样例**自己就中了这条**，
    #   见工单 §〇 事实 B；W6 干的就是把标杆本身做扎实）。
    hi, mid, low = density_bands()
    lines.append("---")
    lines.append("")
    lines.append("## 版面密度：一页该吃掉多少")
    lines.append("")
    lines.append("`audit_deck` 量的是**看得见的墨**占可用版面多少，不是形状框的大小。")
    lines.append("正文框本来就铺满内容带，按框量每一页都 ≈88% —— 那样什么都抓不到。")
    lines.append("")
    lines.append(f"档位：< {hi}% 明显空 ｜ {hi}–{mid}% 偏低 ｜ {mid}–{low}% 略空 "
                 f"｜ ≥ {low}% 通过（不报）。")
    lines.append("只 WARN，不拦交付。`cover` / `section` **不量**（留白是有意的）。")
    lines.append("")
    lines.append("逐页型的落点（左边**由成品 .pptx 现算**、不再手抄，右边是怎么再往上抬）：")
    lines.append("")
    for k, band, how in density_per_kind():
        lines.append(f"- **`{k}`** {band} —— {how}")
    lines.append("")
    lines.append("往上抬只有三条路，按优先级：")
    lines.append("")
    lines.append("1. 放视觉资产。图 / 表 / 原生图表吃满主视觉位，一页就有 60% 以上。")
    lines.append("2. 把要点写实。每条写满一到两行（数字、口径、边界条件），"
                 "四条装不下就拆页，别堆成六条。")
    lines.append("3. 换页型。只有一件事可说就改用 `section`，别用内容页装一句空话。")
    lines.append("")
    lines.append("**不要靠调大字号抬数字。** 字号是阶梯上的定值，`type` 门也盯着它。")
    lines.append("")
    lines.append(f"### 反例：把第 {EXAMPLE_PAGE_NO} 页左右各砍剩第一条，就退回这个形状")
    lines.append("")
    lines.append("```")
    for one in DENSITY_NEGATIVE_EXAMPLE:
        lines.append(one)
    lines.append("```")
    lines.append("")
    lines.append("每侧一条、结论都来不及给，几行字悬在版面中间，下半页全空 —— ")
    lines.append("W6 之前这份样例的第 4 页就是这个形状，被 density 门当场报了。")
    lines.append(f"同一页写满（就是上面第 {EXAMPLE_PAGE_NO} 页的本体）：")
    lines.append("")
    lines.append("```")
    if _EXAMPLE_PAGE is not None:
        for _side, _block in zip(("左", "右"), list(_EXAMPLE_PAGE.get("lines") or [])[:2]):
            _paras = _para_blocks(_block)
            lines.append(f"{_side}：{_paras[0]}")
            for _p in _paras[1:]:
                lines.append(f"　　{_p}")
    lines.append("```")
    lines.append("")
    lines.append("左右各三条、条数相等、每条都带一个数。")
    lines.append("字少了就是内容少了 —— 别用排版去遮。")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 怎么用")
    lines.append("")
    lines.append("页型序列（`pages` 参数）就照上面每页的 `kind` 抄；")
    lines.append("正文由你自己的内容决定，但**每页的论点密度照这个标准**：")
    lines.append("内容页 3–4 条、每条写满一到两行，对比页左右各 2–3 条且条数相等，图表页标题即结论。")
    lines.append("")
    lines.append("**备注也要写**，别只交画面。上面每页那段「讲稿备注」就是示范：")
    lines.append("写来源（这个数从哪来）、口径（怎么算的）、")
    lines.append("以及**会被问到的问题与回答**。补的办法是 `set_slide_notes`：")
    lines.append("")
    lines.append("```")
    lines.append('set_slide_notes(pptx="deck.pptx", notes=[第1页一段, 第2页一段, …])')
    lines.append("```")
    lines.append("")
    lines.append("长度**必须等于页数**（第 i 段配第 i 页）。补完必须**重跑 `audit_deck`**。")
    return "\n".join(lines)
