# -*- coding: utf-8 -*-
"""荣珂 ↔ workbench 领域库接入层。

只做两件事：
  1. 把 workbench 目录挂进 sys.path —— 252KB 工具资产零拷贝复用，不搬不改。
  2. 用 FastMCP 的 _tool_manager 反射出全部工具的 name / description /
     JSON Schema / 原函数，注册进荣珂的工具注册表。
     （唯一例外：`kb_ingest` 反射时包一层荣珂侧预检，见 `_wrap_kb_ingest_precheck`。）

**为什么不走 MCP 协议**：同进程直调，省掉子进程启动与 JSON 序列化开销；
已实测 workbench_mcp.py 的 @mcp.tool() 装饰器返回的仍是原函数（模块级
`getattr(workbench_mcp, name)` 是 `<class 'function'>`，可直接调）。

workbench 的 21 个工具是荣珂的**领域资产**：工具本身、门禁、台账都长在那里。
荣珂只负责调度它们，不重写它们。
"""
from __future__ import annotations

import functools
import inspect
import json
import re
import sys
import warnings
from pathlib import Path

from .config import Config
from .tools import Tool, ToolRegistry

# 会改动外部世界的工具（写盘 / 落图 / 派发）——门禁与审批要用
MUTATING = {
    "mechanical_fix",        # 改稿件文件
    "export_docx",           # 写 docx
    "build_references",      # 写 frame/bib
    "import_document",       # 落 .md
    "record_skill_use",      # 写台账
    "dispatch_task",         # 派发任务（高风险）
    "figure_render",         # 落图
    "figure_audit_extract",  # 抽取 PDF 图像落盘
    # 文献知识库（2026-09-22）：这两个写 library.json / pdfs / texts / index.db。
    # 如实登记成 mutating，围栏才会对它们生效 —— 于是「src / library 用相对路径」
    # 这条纪律是被**机制**保证的，不是靠模型记得。
    # 代价（已知且可接受）：绝对路径指向工作区外会被拒；而用户递进材料区的 PDF
    # 本来就在本次工作区里，相对路径即可 —— 这恰好是主路径。
    "kb_add",                # 入库文献（写库）
    "kb_note",               # 写文献笔记
    # 2026-09-22 补：批量入库（写库）与综述落盘。
    # kb_review **只在给了 out 时才写**，但它"能写"这件事是确定的 ⇒ 按写类登记。
    # 本项目的治理口令是「未知工具一律按'会改'」，那么**已知会改的**更不该漏。
    "kb_ingest",             # 批量入库（写库）
    "kb_review",             # 综述草稿；给了 out 就落盘
}


class BridgeError(RuntimeError):
    pass


# ─────────────────────────── kb_ingest 荣珂侧预检（W3 · R1） ───────────────────────────

# 上游 `_kb_expand` 收**任何存在的文件**进候选，而真正入库那一层（litkb.add_pdf_file）
# 只认 `.pdf` ⇒ 两处判据不一致，表现为 `dry_run` 对 .md 回 `would_add:1`、真跑才报
# 「不是 PDF 文件」。这不是 kb 库的缺陷 —— **kb 库按设计只吃 PDF**（litkb 的全文抽取
# 与段落索引都建在 PDF 上；litkb 入库时自己会抽全文，转换出的 Markdown 是给人读 /
# 给 Obsidian 的另一套文本）。要修的只是**干跑不许给假承诺**。
_KB_PDF_ONLY_NOTE = (
    "kb 库（kb_search/kb_ask 查的那套索引）**只收 PDF 原文** —— 这是有意的分工，"
    "不是缺这一格式：litkb 入库时会自己从 PDF 抽全文并建段落级索引，"
    "同一篇文献没必要两边都存正文。这些文件不是 PDF（Markdown 是 convert_pdf "
    "的产物，用途是**给人读、写进 Obsidian**，不是入库格式）。"
    "要让这篇进知识库，请把它的 **PDF 原文**交给 kb_ingest / kb_add。")


def _not_pdf_plan_entries(raw_paths: list) -> list:
    return [{"file": Path(x).name, "will_add": False, "not_pdf": True}
            for x in raw_paths]


def _wrap_kb_ingest_precheck(inner):
    """包一层**入库前自查**：候选里有非 `.pdf` 的文件时，如实标出来，
    不让 `dry_run` 替上游兑现「会入库」的假承诺。

    ★ 判据与入库层**同一条**：litkb.add_pdf_file 用的是
    `p.suffix.lower() != ".pdf"`（实测上游报「不是 PDF 文件」那一支），这里照用，
    不另立标准 —— 两处判据不一致正是本工单要修的病根。
    ★ 全是 PDF（或只有目录/不存在的路径）⇒ **原样透传**，真 PDF 的行为一个字不变。
    ★ 这里只做**更早、更诚实的拒绝**，不放宽任何校验：非 PDF 依旧进不了库。
    """
    sig = inspect.signature(inner)

    @functools.wraps(inner)
    def kb_ingest(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        spec = str(bound.arguments.get("paths") or "")
        keep: list[str] = []
        not_pdf: list[str] = []
        for raw in [x.strip() for x in spec.split(",") if x.strip()]:
            try:
                p = Path(raw)
                is_file = p.is_file()
            except OSError:
                p, is_file = None, False
            if is_file and p.suffix.lower() != ".pdf":
                not_pdf.append(raw)
            else:
                keep.append(raw)
        if not not_pdf:
            return inner(*args, **kwargs)          # 纯 PDF 批次：一个字都不碰

        bound.arguments["paths"] = ",".join(keep)
        base = None
        if keep:
            raw = inner(*bound.args, **bound.kwargs)
            try:
                base = json.loads(raw)
            except Exception:                       # noqa: BLE001
                # 上游回包解析不了：不硬凑，把拦下的非 PDF 项如实附在前面。
                return json.dumps({
                    "ok": False,
                    "error": "kb_ingest 上游回包不是合法 JSON，荣珂预检已拦下的非 PDF 项见 not_pdf",
                    "not_pdf": _not_pdf_plan_entries(not_pdf),
                    "not_pdf_note": _KB_PDF_ONLY_NOTE,
                    "raw": str(raw)[:800]}, ensure_ascii=False)
        dry = bool(bound.arguments.get("dry_run"))
        if base is None:                            # 整批都不是 PDF：不调上游（空 paths 只会得到「paths 为空」）
            base = ({"ok": True, "dry_run": True, "candidates": 0, "would_add": 0,
                     "would_skip": 0, "plan": []} if dry else
                    {"ok": False, "added": 0, "skipped": 0, "failed": 0,
                     "items": [], "skipped_items": [],
                     "error": "这批里没有一份 PDF，没有入库任何文件（见 not_pdf）"})
        base["candidates"] = int(base.get("candidates") or 0) + len(not_pdf)
        base.setdefault("plan", []).extend(_not_pdf_plan_entries(not_pdf))
        base["not_pdf"] = _not_pdf_plan_entries(not_pdf)
        base["not_pdf_note"] = _KB_PDF_ONLY_NOTE
        if not keep:
            base["note"] = ("dry_run 没有动库；这批没有可入库的 PDF（见 not_pdf）"
                            if dry else
                            "本次**没有入库任何文件**，库没有被改动")
        elif not dry and base.get("ok"):
            # 混批真跑：PDF 部分照常处理，但别让调用方以为"ok 就是全都入了"。
            base["note"] = str(base.get("note") or "") + \
                f"\n⚠️ 另有 {len(not_pdf)} 份不是 PDF，**没有入库**（见 not_pdf）。"
        return json.dumps(base, ensure_ascii=False)

    return kb_ingest


# ─────────────────────── kb_ask 荣珂侧兜底（W27） ───────────────────────
#
# 实测症状：`kb_ask` 回 `{"ok": false, "error": "RuntimeError: AI 未配置完整…"}`，
# 或（库里只有题录、没全文时）`没有可用的文献内容（既无全文也无摘要）`。
#
# ★ 第一层原因：litkb.ask() 要一个**它自己的** ai_client（workbench 的「AI 助手」
#   配置 / PAPER_WORKBENCH_API_KEY）。而荣珂这边早就有一个配好的模型
#   （荣珂的 provider，现在是 CodeBuddy）。**同一个进程里有两套模型配置，
#   而其中一套从没被配置过** —— 于是"库里 AI 问答"这个能力对用户是**静默死的**：
#   它返回 ok:false，而人在导师模式里看到的只是"模型没答上来"，会误读成
#   "库里真没有"。
#
# ★ 第二层原因（更伤）：上游 `ask()` 在抛错之前，**已经把片段取出来了**
#   （`frags` 非空才走到 `_ai_chat`；"没有可用内容"那一支说明 frags 为空是对的）。
#   也就是说：检索成果是好的，**死的只是那一步生成**。
#
# 所以荣珂侧的正确修法**不是**去改 litkb（领域资产，纪律上不动），
# 而是：**用荣珂自己的模型替它做那一步生成**。片段由 litkb 取，生成由荣珂做。
# 这和 `_wrap_kb_ingest_precheck` 是同一条纪律 —— 接缝在荣珂侧，上游一个字不改。
#
# 三条不许违反（否则就是把一个可用性问题升级成数据问题）：
#   ① **能跑通上游时一个字都不插手**（上游有自己的模型就让它答，行为不变）；
#   ② 兜底只在上游**明确报错**时启用，且**必须如实标注**这份答案是谁生成的；
#   ③ 片段里没有的东西不许编 —— 沿用上游那段 system 的铁律，并**只喂检索原文**。
_KB_ASK_FALLBACK_NOTE = (
    "本答案由**荣珂自己的模型**基于 litkb 检索出的片段生成"
    "（litkb 自带的 AI 未配置，荣珂侧兜底接管了生成这一步；检索仍由 litkb 完成）。")


# ★ 第三层原因（**这一层才是实操中必然踩到的**）：litkb 的检索对**短词**是瞎的。
#   两张 fts 表都是 `tokenize='trigram'`，而 `_sanitize_match` 的正则是
#       r"[A-Za-z0-9][A-Za-z0-9\-_.]*|[\u4e00-\u9fff]{3,}"
#   再过一道 `len(t) >= 3` —— **少于 3 个汉字的词直接被丢掉，而且不报错**。
#   实测（见 `00_总控/_w28/w28d_floor.py`）：
#       MATCH "温度" -> 0 行     ← 2 字，trigram 索引里压根没有
#       MATCH "融合" -> 0 行
#       MATCH "原生质" -> 166 行  ← 3 字起才有
#   更糟的是 `_match_expr` 在词表被过滤空后返回 `None`，`search_paras` 于是
#   **整个跳过 MATCH 分支**：`_sanitize_match("酶解 温度")` → `[]`
#   ⇒ 这个查询**从未执行过**（不是"搜了没搜到"，是"没搜"）。
#   `融合`/`温度`/`酶解`/`菌株`/`选育` 恰是中文学术检索最常用的词。
#
#   ⚠️ 我此前在这里写的是「litkb 的 FTS 是**全部词都要命中**的 AND 语义 ⇒
#      多词必然 0 命中」。**那个判断是错的** —— 它是从一个失败样本
#      （43 字整句）推广出来的假规律。实测 `原生质体 融合`、`protoplast fusion`、
#      `蝙蝠蛾拟青霉 原生质体` 都正常返回 8 命中。真正的分界线是**词长**，不是词数。
#      更坏的是这个假规律**被模型学去了**：它在开题报告里自己写下
#      「多词组合查询在本库一律返回 0，故用单词项」——错误的能力认知固化成了工作习惯。
#
#   修法（两条，都在荣珂侧，litkb 一个字不改）：
#     ① `kb_ask`：把**问句**拆成检索词再喂给 litkb（`_kb_ask_terms`）；
#     ② `kb_search`：命中的两字词另走 LIKE 补回（`_wrap_kb_search`）。
_KB_ASK_STOP = {
    "什么", "怎么", "如何", "是否", "哪些", "哪个", "为什么", "请问", "告诉",
    "我", "的", "了", "吗", "呢", "和", "与", "及", "在", "是", "有", "请",
    "最", "会", "能", "可以", "多少", "介绍", "说明", "解释", "关于",
    "the", "a", "an", "is", "are", "of", "to", "and", "or", "in", "on",
    "what", "how", "which", "why", "does", "do", "for", "with", "that",
}


# ══════════════ 领域概念表：同义词 + 领域锚（**单源**） ══════════════
# 为什么需要这一层（2026-11-02 实测，用户问「核质染色怎么做」）：
#   全库 19,283 段里 `核质染色` **0 次**；`DAPI` **50 段/16 篇**。
#   文献里的叫法是 DAPI / 荧光染色 / 细胞核染色 —— 用户用的是教科书词，
#   文献用的是试剂名。词对不上 ⇒ 0 命中 ⇒ 被读成「这件事没有文献」，
#   而 `kb478857a9 p.2 §1.4.2` 就写着全套浓度、时长与激发波长。
#   **这不是检索的毛病，是术语的地图缺了一页。**
#
# ★ 一条记录同时装三样：aliases（用户怎么说）、terms（文献怎么写）、
#   anchors（这条检索必须落进哪个领域语境）。三者分家就必然漏一处。
# ★ 锚不是打分，是**约束**：单查 `DAPI` 全库 50 段，前 5 段全是巨噬细胞/
#   结核分枝杆菌染色；`DAPI 原生质体` 走 litkb 的真 AND，噪声段里没有
#   「原生质体」这个词，直接被滤掉 —— 用的是文献自己的词，不是相似度。
# ★ 不许拿泛词当 alias：`融合` 会把「原生质体融合」误吞进「融合子鉴定」，
#   反而把最该命中的查询带偏。alias 必须具体到不会误撞。
_KB_CONCEPTS: dict[str, dict[str, tuple]] = {
    "核质染色": {
        "aliases": ("核质染色", "核染色", "细胞核染色", "染核", "核荧光",
                    "nuclear staining", "nucleus staining"),
        "terms": ("DAPI", "Hoechst", "细胞核染色", "核荧光", "荧光染色"),
        "anchors": ("原生质体", "菌丝", "孢子", "虫草", "羊肚菌", "融合子"),
    },
    "细胞壁染色": {
        "aliases": ("细胞壁染色", "荧光增白剂", "荧光白", "calcofluor", "cfw", "fb28"),
        "terms": ("荧光增白剂", "Calcofluor", "CFW", "刚果红"),
        "anchors": ("原生质体", "菌丝", "酶解", "再生"),
    },
    "原生质体制备": {
        "aliases": ("原生质体制备", "原生质体分离", "酶解条件", "溶壁酶", "裂解酶"),
        "terms": ("原生质体", "溶壁酶", "酶解", "渗透压稳定剂"),
        "anchors": ("真菌", "菌丝", "孢子", "得率"),
    },
    "原生质体再生": {
        "aliases": ("原生质体再生", "再生率", "再生培养基", "再生条件"),
        "terms": ("原生质体", "再生培养基", "再生率", "高渗"),
        "anchors": ("菌丝", "菌落", "甘露醇", "蔗糖"),
    },
    "融合子鉴定": {
        "aliases": ("融合子", "融合株", "fusant", "异核体", "杂种"),
        "terms": ("融合子", "异核体", "双核", "拮抗", "锁状联合", "ISSR"),
        "anchors": ("原生质体", "亲本", "菌丝", "融合"),
    },
    "紫外灭活": {
        "aliases": ("紫外灭活", "紫外致死", "紫外诱变"),
        "terms": ("紫外", "灭活", "致死"),
        "anchors": ("原生质体", "再生", "融合"),
    },
    "发酵指标": {
        "aliases": ("发酵指标", "菌粉得率", "生物量", "腺苷", "虫草素",
                    "甘露醇", "多糖含量"),
        "terms": ("菌粉得率", "生物量", "腺苷", "虫草素", "甘露醇", "发酵周期"),
        "anchors": ("发酵", "菌丝体", "摇瓶", "培养"),
    },
}
_KB_CONCEPT_KEYS = tuple(sorted(
    _KB_CONCEPTS, key=lambda k: -max(len(a) for a in _KB_CONCEPTS[k]["aliases"])))

# 问句外壳。**只从串首/串尾剥**，不做任何改写 —— 中段出现「怎么做」多半
# 是正文的一部分，剥掉反而伤查询。
_KB_ASK_QWORDS = tuple(sorted({
    "怎么做", "怎么办", "怎样做", "如何做", "怎么", "怎样", "如何", "是什么",
    "有哪些", "为什么", "请问", "想知道", "方法", "步骤", "流程", "条件", "参数",
    "protocol",
}, key=len, reverse=True))

# 串首的问句前缀：**必须按长度从长到短试**。
# 实测教训：先试「怎么」会把「怎么做原生质体融合」剥成「做原生质体融合」——
# 留下一个悬空的「做」，检索词就脏了。
_KB_ASK_PREFIX = tuple(sorted({
    "想知道", "请问", "如何做", "怎样做", "怎么做", "怎么办",
    "如何", "怎样", "怎么",
}, key=len, reverse=True))

_KB_TERM_BUDGET = 8       # 一次检索最多试几个词（与旧口径同值）
_KB_ANCHOR_SHOW = 2       # 每个词最多试几个锚（AND 查询次数 = 词数 × 锚数）
_KB_ANCHOR_BUDGET = 4     # 一句问最多带几个锚
_KB_PASSAGE_CHARS = 600   # 单段正文回给模型的字数上限
_KB_PASSAGE_MAX = 6       # 最多回几段
_KB_PASSAGE_TOTAL = 4200  # 全部段落的总字数预算（远低于 ToolRegistry 的 12000 截断）


# 明显是虚词/句法成分的字。含这些字的长串**不许切片**。
# 实测教训（2026-11-02）：问句「库里有关于量子色动力学的文献吗」被滑窗切成
# `库里有 / 里有关 / 有关于`，其中 `有关于` 撞进了三篇诱变育种文献 ——
# 于是「库里没有这个主题」被它答成了「从这些片段看未提及」，免责句整个消失。
# **碎片命中比 0 命中更坏**：0 命中会让人去换词，碎片命中会让人以为查过了。
_KB_FUNC_CHARS = frozenset("有是的了么吗呢吧啊在和我你他她它这那请什哪怎")


def _kb_sliceable(segment: str) -> bool:
    """像不像一个**术语**（而不是一句话）。只有术语样长串才值得切三字去撞库。"""
    if len(segment) < 5 or not re.fullmatch(r"[\u4e00-\u9fff]+", segment):
        return False
    return not any(ch in _KB_FUNC_CHARS for ch in segment)


def _kb_needs_like(term: str) -> bool:
    """两字中文词必须走 LIKE —— trigram 索引里根本没有它们（见 `_kb_short_terms`）。"""
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2}", term or ""))


def _kb_is_question(text: str) -> bool:
    """像不像一句人话问句（而不是一串检索词）。纯函数。"""
    s = (text or "").strip()
    if not s:
        return False
    if s.endswith(("？", "?")):
        return True
    return any(w in s for w in _KB_ASK_QWORDS)


def _kb_question_core(question: str) -> str:
    """剥掉问句外壳，只留「问的是什么」。纯函数。

    这是 `核质染色怎么做` 现在 0 命中的直接原因：旧实现只在**切完的整段**
    上比 `_KB_ASK_STOP`，而「怎么做」嵌在串里，整词比对永远命不中。
    """
    s = (question or "").strip()
    if not s:
        return ""
    s = re.sub(r"[\s　]+", " ", s).strip()
    s = re.sub(r"[？?！!。；;，,、]+$", "", s).strip()
    for _ in range(3):
        hit = False
        for w in _KB_ASK_QWORDS:                      # 长的在前（表已按长度排好）
            if len(s) > len(w) and s.endswith(w):
                s = s[: -len(w)].strip(" 　的")
                hit = True
                break
        if hit:
            continue
        for w in _KB_ASK_PREFIX:                  # 长的在前（表已按长度排好）
            if len(s) > len(w) and s.startswith(w):
                s = s[len(w):].lstrip(" 　，,")
                hit = True
                break
        if not hit:
            break
    return s.strip()


def _kb_concept_hit(segment: str) -> str:
    """一个切段命中哪条概念（alias 子串匹配，长 alias 优先）。纯函数。"""
    s = (segment or "").strip().lower()
    if not s:
        return ""
    for key in _KB_CONCEPT_KEYS:
        for alias in _KB_CONCEPTS[key]["aliases"]:
            if alias.lower() in s:
                return key
    return ""


def _kb_expand_terms(question: str, max_terms: int = _KB_TERM_BUDGET) -> dict:
    """问句 → 检索计划。**纯函数：不碰库、不联网、不读配置。**

    返回 `{core, concepts, terms:[{q,how,src}], anchors, dropped, sliced}`：
      - `how` ∈ `{"fts","like"}` —— 两字中文词必走 LIKE（trigram 存不下）
      - `src` ∈ `{"synonym","exact","slide"}` —— 排在前面先试；同义词优先，
        原词保留但靠后（原词多半是问句里的说法，命中率低于文献用词）
      - `dropped` / `sliced` **如实带回**：没有哪个词是被悄悄丢掉的
        （「静默丢词」正是这一层原有的病，不许换个地方重犯）。
    """
    q = (question or "").strip()
    out = {"core": "", "concepts": [], "terms": [], "anchors": [],
           "dropped": [], "sliced": []}
    if not q:
        return out
    core = _kb_question_core(q) or q
    out["core"] = core

    subs: list[str] = []
    for part in re.split(r"[\s,，。；;：:、！!？?（）()\[\]【】\"'“”‘’/\\|]+", core):
        part = part.strip()
        if not part:
            continue
        # 中文没有空格，长段再按「的/和/与」这类连接词切一刀（旧口径，不改）
        for sub in re.split(r"[的之和与及或]", part):
            sub = sub.strip()
            if not sub:
                continue
            if len(sub) < 2 or sub.lower() in _KB_ASK_STOP:
                out["dropped"].append(sub)
                continue
            subs.append(sub)
    subs.sort(key=lambda s: -len(s))                  # 长词优先（旧口径）

    syn: list[str] = []
    exact: list[str] = []
    slide: list[str] = []
    for sub in subs:
        key = _kb_concept_hit(sub)
        if key:
            if key not in out["concepts"]:
                out["concepts"].append(key)
            syn.extend(_KB_CONCEPTS[key]["terms"])
            for a in _KB_CONCEPTS[key]["anchors"]:
                if a not in out["anchors"]:
                    out["anchors"].append(a)
            exact.append(sub)                          # 原词保留，排同义词后面
        elif _kb_sliceable(sub):
            # 术语样长串：**原串照旧先试**（≥3 字，trigram 支持），
            # 再补滑窗切三字兜底 —— 整串常因为多了修饰语而 0 命中
            # （「原生质体融合时最适」这种），切片才有可能撞到。
            exact.append(sub)
            out["sliced"].append(sub)
            for i in range(min(3, len(sub) - 2)):
                slide.append(sub[i:i + 3])
        elif len(sub) >= 5:
            # 长句/问句：整串去查必 0（trigram 把整个 CJK 串当一个词），
            # 切片又会撞出「有关于」那种碎片命中 ⇒ **两个都不做，如实记账**。
            out["dropped"].append(sub)
        elif any(ch in _KB_FUNC_CHARS for ch in sub):
            # 短句里带虚词的（「文献吗」「怎么选」）：不是术语，别占检索预算。
            out["dropped"].append(sub)
        else:
            exact.append(sub)

    seen: set[str] = set()
    ordered: list[dict] = []

    def push(term: str, src: str) -> None:
        term = (term or "").strip()
        if not term or term.lower() in seen:
            return
        seen.add(term.lower())
        ordered.append({"q": term,
                        "how": "like" if _kb_needs_like(term) else "fts",
                        "src": src})

    for term in syn:
        push(term, "synonym")
    for term in exact:
        push(term, "exact")
    for term in slide:
        push(term, "slide")

    out["terms"] = ordered[:max_terms]
    out["anchors"] = out["anchors"][:_KB_ANCHOR_BUDGET]
    return out


def _kb_ask_terms(question: str, max_terms: int = 8) -> list[str]:
    """把**问句**拆成 litkb 能用的**检索词**。

    保留这个名字是因为它是既有调用点（`_kb_ask_fragments`、零命中回包的
    `searched_terms`）的入口；真正的拆法已经搬到 `_kb_expand_terms`
    （多了问句剥壳、同义词展开、领域锚与两字词的 LIKE 分流）。
    这层壳只保证**旧签名与旧语义**不变，免得两处各算一遍。
    """
    return [t["q"] for t in _kb_expand_terms(question)["terms"]][:max_terms]


def _kb_search_term(lib, term: str, how: str, anchor: str = "", doc_ids=None,
                    n: int = 14) -> list[dict]:
    """用**一个词**（可选带一个锚）检索段落。任何异常都吞掉返回空。

    - `how=="like"`（两字中文词）：走 litkb 的 `_like_fallback`；
      带锚时**在结果里筛锚**（LIKE 表达不了 AND，只能筛）。
    - `how=="fts"`：`"词 锚"` 直接交给 litkb —— 它的 `_match_expr` 是**真 AND**，
      所以不用在这里另写 SQL，也就不会出现「两处各写一遍 AND」的 H 型缺陷。
    """
    if how == "like":
        rows = _kb_like_paras(lib, term, doc_ids=doc_ids, n=n)
        if anchor:
            rows = [r for r in rows if anchor in str(r.get("text") or "")]
        return rows
    q = f"{term} {anchor}".strip() if anchor else term
    try:
        return list(lib.search_paras(q, n=n, doc_ids=doc_ids) or [])
    except Exception:                                    # noqa: BLE001
        return []


def _kb_rank(paras: list[dict], anchors) -> list[dict]:
    """段落排序：**带锚的在前**，其次分数，其次长度。纯函数。

    为什么锚要排在最前：单查 `DAPI` 时，巨噬细胞/结核那批段落也会命中，
    而且分数不低。领域锚是唯一能把「同一个试剂名、不同学科」分开的信号，
    所以它必须在分数之前 —— 否则 `DAPI` 的前 5 段全是哺乳动物细胞。
    锚为空时排序自然退化为纯分数（负控要能看见这个差异，测试里成对断言）。
    """
    pool = [a for a in (anchors or []) if a]

    def has_anchor(p: dict) -> bool:
        text = str(p.get("text") or "")
        return bool(pool) and any(a in text for a in pool)

    return sorted(paras, key=lambda p: (
        0 if has_anchor(p) else 1,
        -float(p.get("score") or 0),
        -len(str(p.get("text") or "")),
    ))


def _kb_zero_verdict(meta: dict, question: str = "") -> dict:
    """0 命中时**归因**：到底是哪一层断的。纯函数。

    ★ 这是本仓最忌讳的一件事：把「读不到」渲染成「不符合」。
      旧回答只有一句「你的文献库里没有可用于回答这个问题的材料」，
      用户会读成「世上没人做过」—— 而实测库里有全套 protocol，
      只是没用他那个词写。三种成因的**下一步动作完全不同**：

        word    主题在、词不对  → 换更接近原文的叫法（能救）
        topic   主题也没有      → 换题或补文献（救不了，但要明说边界）
        unknown 认不出主题      → **分不清**，不许冒充前两者
    """
    cov = (meta or {}).get("coverage") or {}
    plan = (meta or {}).get("term_plan") or {}
    anchors = list(plan.get("anchors") or [])
    terms = list(plan.get("terms") or [])[:6]
    hits_map = cov.get("anchor_hits") or {}
    best, best_n = "", 0
    for a, n in hits_map.items():
        try:
            n = int(n or 0)
        except Exception:                                # noqa: BLE001
            n = 0
        if n > best_n:
            best, best_n = str(a), n

    if not anchors:
        why = "unknown"
    elif best_n > 0:
        why = "word"
    else:
        why = "topic"

    terms_txt = "、".join(terms) or "（没有可用的检索词）"
    if why == "word":
        text = (f"这个主题在库里是**有文献的**（锚词「{best}」至少命中 {best_n} 段），"
                f"但**没有**哪一段是用我试过的这几个词写的：{terms_txt}。\n"
                "换更接近文献原文的叫法再问一次（本库常用：DAPI / 荧光染色 / "
                "细胞核染色 / 荧光增白剂）。")
    elif why == "topic":
        text = ("库里**没有**这个主题的段落 —— 领域锚词"
                f"（{'、'.join(anchors)}）检索都是 0。\n"
                "注意：这**不等于**世上没人做过，只说明你已入库的这批文献里没有。")
    else:
        text = ("这次**没能归因**：问句里没有我认得的领域锚（物种名/方法名/试剂名），"
                "所以分不清是『库里没有这个主题』还是『库里只是没用这个词』。"
                "这两种情况我没有证据区分，**不会**把它们说成同一件事。\n"
                f"已经试过的词：{terms_txt}，全部 0 命中。\n"
                "把物种名/方法名/试剂名说进来再问一次，我就能给你确定的答案。\n"
                "无论如何，「库里没有」**不等于**「世上没人做过」——"
                "库里只有你已入库的那几篇。")
    return {"why": why, "anchor": best, "anchor_hits": best_n,
            "terms": terms, "text": text}


def _kb_ask_fragments(question: str, ids=None, n_docs: int = 3,
                      library: str = ""):
    """直接用 litkb 取片段 —— **不经过它的生成那一步**。

    返回 `(frags, refs, meta)`。`meta` 里有 `term_plan` / `coverage` /
    `passages`，调用方据此才能在零命中时**说清是哪一层断的**
    （见 `_kb_zero_verdict`）—— 旧版本只能回一句「库里没有」。

    ★ 两趟的顺序是刻意的：**先量主题层，再查词层**。
      反过来 0 命中时无法区分「库里没这个主题」与「库里没用这个词」，
      而这两种情况要给的下一步完全相反（一个换题、一个换词）。

    ★ 取模块走 `workbench_mcp._kb_module(library)` —— 那是 workbench 自己
      用的入口（它顺手做了 `set_data_dir`，支持「一人/一课题一份库」）。
      第一版我写的 `w.litkb` 会得到 `module 'workbench_mcp' has no attribute
      'litkb'`：litkb 是**懒导入**的，从来不是 workbench_mcp 的属性。
    """
    w = _import_workbench(Path.home() / ".dsh" / "papers" / "workbench")
    manager = getattr(w, "_kb_module", None)
    if manager is None:
        raise BridgeError("workbench_mcp 上没有 _kb_module（上游结构可能变了）")
    lib, _root = manager(library)
    items = lib._load_library()
    by_id = {it["id"]: it for it in items}
    if not items:
        raise BridgeError("文献库为空，请先入库文献")

    plan = _kb_expand_terms(question)
    terms = plan["terms"] or [{"q": (question or "").strip(), "how": "fts",
                               "src": "exact"}]
    anchors = plan["anchors"]

    doc_ids = None
    if ids:
        sel = [by_id.get(i) for i in ids if by_id.get(i)]
        if not sel:
            raise BridgeError("指定的文献 id 不存在")
        doc_ids = [it["id"] for it in sel]

    # ① 主题层下界：每个锚单独试一次，**只数条数不取含义**。
    #    n 给 50 不是 1：n=1 也能证明"有"，但回给用户的"至少 1 段"读起来像
    #    几乎没有 —— 而实测 `原生质体` 有 1,160 段。报一个像样的下界，
    #    用户才知道"换词"值得试。仍然标注为**下界**（命中超过 50 时就是 50）。
    anchor_hits: dict[str, int] = {}
    for a in anchors:
        try:
            anchor_hits[a] = len(lib.search_paras(a, n=50, doc_ids=doc_ids) or [])
        except Exception:                                # noqa: BLE001
            anchor_hits[a] = 0
    topic_hits = max(anchor_hits.values()) if anchor_hits else 0

    # ② 词层：先带锚（AND 约束），再裸词兜底。哪个词先命中就用哪个。
    paras: list[dict] = []
    used, used_src, constrained, tried = "", "", False, 0
    for t in terms:
        tried += 1
        got: list[dict] = []
        hit_constrained = False
        for a in anchors[:_KB_ANCHOR_SHOW]:
            got = _kb_search_term(lib, t["q"], t["how"], anchor=a,
                                  doc_ids=doc_ids)
            if got:
                hit_constrained = True
                break
        if not got:
            got = _kb_search_term(lib, t["q"], t["how"], doc_ids=doc_ids)
        if got:
            paras, used, used_src, constrained = got, t["q"], t.get("src", ""), hit_constrained
            break

    groups: dict[str, list[dict]] = {}
    if paras:
        for p in _kb_rank(paras, anchors):
            pid = p.get("doc_id")
            if pid:
                groups.setdefault(pid, []).append(p)
        sel = [by_id[k] for k in groups if k in by_id][:n_docs]
        groups = {it["id"]: groups[it["id"]] for it in sel}
    else:
        # 段落没中：退回文献级 + 摘要（旧行为保留，别把摘要兜底一起丢掉）
        sel, picked = [], set()
        for t in terms[:4]:
            try:
                rs = lib.search_docs(t["q"], n=n_docs) or []
            except Exception:                            # noqa: BLE001
                rs = []
            for r in rs:
                if r.get("id") in by_id and r["id"] not in picked:
                    picked.add(r["id"])
                    sel.append(by_id[r["id"]])
            if len(sel) >= n_docs:
                break
        if doc_ids:
            sel = [by_id[i] for i in doc_ids if i in by_id]
        sel = sel[:n_docs]

    frags: list[str] = []
    refs: list[dict] = []
    passages: list[dict] = []
    budget = _KB_PASSAGE_TOTAL
    for it in sel:
        pid = it["id"]
        label = (it.get("title") or pid)[:70]
        got = (groups.get(pid) or [])[:3]
        if got:
            for p in got:
                text = str(p.get("text") or "")
                if not text.strip():
                    continue
                frags.append(f"[文献: {label} | 页码 p.{p['page_no']}]\n{text}\n")
                refs.append({"id": pid, "title": label, "page": p["page_no"]})
                if len(passages) < _KB_PASSAGE_MAX and budget > 0:
                    take = min(_KB_PASSAGE_CHARS, budget)
                    cut = text[:take]
                    passages.append({
                        "doc_id": pid, "page": p["page_no"], "title": label,
                        "text": cut, "chars": len(text),
                        "truncated": len(text) > len(cut),
                        "term": used, "constrained": constrained,
                    })
                    budget -= len(cut)
        elif it.get("abstract"):
            frags.append(f"[文献: {label} | 摘要]\n{it['abstract'][:900]}\n")
            refs.append({"id": pid, "title": label, "page": 0})

    meta = {
        "term_plan": {"core": plan["core"], "concepts": plan["concepts"],
                      "terms": [t["q"] for t in terms], "anchors": anchors,
                      "dropped": plan["dropped"], "sliced": plan["sliced"]},
        "coverage": {"terms_tried": tried,
                     "term_hits": 1 if paras else 0,
                     "docs_hit": len({r["id"] for r in refs}),
                     "anchor": anchors[0] if anchors else "",
                     "anchor_hits": anchor_hits,
                     "topic_hits": topic_hits,
                     "constrained": constrained,
                     # 命中的是哪个词、它是怎么来的：synonym(同义扩展) /
                     # exact(原样) / slide(长串切出来的三字片段)。落在 slide
                     # 上说明这是一次"碎片命中"，模型据此才敢降低置信度。
                     "matched_term": used,
                     "matched_by": used_src,
                     "passage_count": len(passages)},
        "passages": passages,
    }
    return frags, refs, meta


def _kb_short_terms(query: str, max_terms: int = 6) -> list[str]:
    """挑出**litkb 的 FTS 根本搜不到**的两字中文词。

    ★ 为什么需要这个：litkb 的 fts_para / fts_doc 都用 `tokenize='trigram'`，
      而 `litkb._sanitize_match` 的正则是
          r"[A-Za-z0-9][A-Za-z0-9\\-_.]*|[\\u4e00-\\u9fff]{3,}"
      且再过一道 `len(t) >= 3`。**少于 3 个汉字的词会被直接丢掉，而且不报错。**
      实测（w28d_floor.py）：
          MATCH "温度" -> 0 行     ← 2 字，trigram 索引里压根没有
          MATCH "融合" -> 0 行
          MATCH "原生质" -> 166 行  ← 3 字起才有
      更要命的是 `_match_expr` 在词表被过滤空之后返回 `None`，
      `search_paras` 于是**整个跳过 MATCH 分支**：
          _sanitize_match("酶解 温度") -> []   ⇒ 这个查询**从未执行过**
      而 `融合` / `温度` / `酶解` / `菌株` / `选育` 恰是中文学术检索最常用的词。

    ★ 为什么不在荣珂侧"放宽到 2 字就去 MATCH"：trigram 索引本身就存不下 2 字词，
      放宽只会换来**静默空结果**，比现在更糟。真正的退路是 `LIKE`
      （实测 `LIKE %温度%` → 43 行）。litkb 自己有 `_like_fallback`，
      但它只在 FTS 返回空时触发、且拿**整串**去 LIKE
      （`%原生质体 融合%` 这种带空格的整串 → 0 行），所以救不了这个场景。

    ★ 纪律：**litkb 一个字不动**（领域资产）。这一层只读、只补，不改上游。
    """
    q = (query or "").strip()
    if not q:
        return []
    # 与 _kb_ask_terms 同口径切词，但这里只留**两字中文词**（三字以上归 FTS）
    parts = re.split(r"[\s,，。；;：:、！!？?（）()\[\]【】\"'“”‘’/\\|]+", q)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if len(p) == 2 and re.fullmatch(r"[\u4e00-\u9fff]{2}", p) and p not in out:
            out.append(p)
    return out[:max_terms]


def _kb_relax_terms(query: str, max_terms: int = 6) -> list[str]:
    """多词 AND 查询 → 逐词召回的候选词。**只收 ≥3 字中文词 / 英文词**。

    litkb 的 MATCH 是**真 AND**：`菌丝生长速率 菌落直径`、
    `protoplast regeneration frequency mannitol` 这类查询里每个词单独都搜得到，
    但没有任何一个段落同时含全部词 ⇒ 整串 0 命中（11-03 实测至今仍在）。

    ⚠️ 为什么**不收两字中文词**：trigram 索引本来就存不下它们（见
       `_kb_short_terms`），逐词召回对它们必然也是 0；而 `kb_recall_test.py` §6
       的负控是「关掉短词救援后『酶解 温度』必须回 0 命中」—— 把两字词放进
       这一层就会打红它。两字词归 LIKE 救援，两条路各管各的、互不重叠。
    """
    q = (query or "").strip()
    if not q:
        return []
    parts = [p.strip() for p in re.split(
        r"[\s,，。；;：:、！!？?（）()\[\]【】\"'“”‘’/\\|]+", q) if p.strip()]
    if len(parts) < 2:
        return []
    out: list[str] = []
    for p in parts:
        if (len(p) >= 3 or re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{1,}", p)) and p not in out:
            out.append(p)
    # 只有一个可召回词 ⇒ 交给上游/短词救援，别把「一个词」冒充「多词拆词」
    return out[:max_terms] if len(out) >= 2 else []


def _kb_finalize(obj: dict) -> str:
    """回包的**唯一出口**：自报的 `n` 恒等于读回来的 `len(hits)`。

    实测事故：短词救援把 `extra` 追加到 `hits` 后没更新 `n` ⇒
    请求 `n=10`、回包 `n=10`、实际 `hits=20`。模型与审计按 `n` 读条数就错。
    所有救援路径都从这里出，谁也不许自己 `json.dumps` 一个改过 hits 的包。
    """
    o = dict(obj)
    o["n"] = len(o.get("hits") or [])
    return json.dumps(o, ensure_ascii=False)


def _kb_like_paras(lib, term: str, doc_ids=None, n: int = 10) -> list[dict]:
    """对**单个两字词**做 LIKE 段落检索，返回 dict。

    直接打 litkb 自己的 `_like_fallback`（它已经是 LIKE，只是上游拿整串去调）。
    这里逐个短词调，并显式带上 doc_ids 限定 —— 上游的 extra_sql 拼接同样支持。
    任何异常都吞掉返回空：补丁层坏了不该让整个检索失败。
    """
    try:
        fb = getattr(lib, "_like_fallback", None)
        if fb is None:
            return []
        extra_sql, extra_args = "", ()
        if doc_ids:
            ph = ",".join("?" for _ in doc_ids)
            extra_sql = f" AND doc_id IN ({ph})"
            extra_args = tuple(doc_ids)
        rows = fb("fts_para", term, extra_sql=extra_sql, extra_args=extra_args,
                  limit=n * 2)
    except Exception:                                          # noqa: BLE001
        return []
    return list(rows or [])


def _wrap_kb_search(inner, workbench_dir: Path):
    """给 `kb_search` 补一层**短词救援**：两字词走 LIKE，与 FTS 结果合并。

    上游行为**一个字不改**：先按原样调 `inner`，拿到它的结果；
    只有当查询里含 litkb 搜不到的两字词时，才额外补 LIKE 命中的段落。

    返回体保持上游形状（`{ok, query, hits:[{doc_id,page_no,text,score}], ...}`），
    补进来的条目多一个 `via: "like"` 便于追溯 —— 不新增顶层字段，
    免得下游（含导师模式的审计）见到没见过的形状。
    """
    sig = inspect.signature(inner)

    @functools.wraps(inner)
    def kb_search(*args, **kwargs):
        raw = inner(*args, **kwargs)
        try:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            a = bound.arguments
            query = str(a.get("query") or "")
            n = int(a.get("n") or 10)
            doc_ids = a.get("doc_ids")
            if isinstance(doc_ids, str):
                doc_ids = [doc_ids] if doc_ids else None
            o = json.loads(raw)
        except Exception:                                      # noqa: BLE001
            return raw
        if not isinstance(o, dict):
            return raw

        # ① 问句改写（**窄路**：零命中 + 像人话问句 + 改写后确实不同，三条同时成立）。
        #    为什么必须窄：这一层不许把「确实没有」改写成「好像有」。
        #    `kb_recall_test.py` §6 的负控（关掉短词救援后 `酶解 温度` 必须回 0）
        #    依赖「不是问句就不改写」这条边界 —— 别把它放宽。
        if not (o.get("hits") or []) and _kb_is_question(query):
            try:
                plan = _kb_expand_terms(query)
            except Exception:                              # noqa: BLE001
                plan = {"terms": [], "anchors": []}
            # 候选串**一个一个试**，不要拼成一句：litkb 是真 AND，
            # 把 DAPI / Hoechst / 细胞核染色 拼一起 ⇒ 没有任何段落同时含三者 ⇒ 0。
            # 带锚的单字最干净（「DAPI 原生质体」7 段，全对题）。
            cands: list[str] = []
            for t in plan.get("terms") or []:
                # ⚠️ 这里**不能**把循环变量叫 `a` —— 外层 `a` 是参数字典，
                #    被覆盖后 `inner(**{**a, …})` 会抛 TypeError，而异常被下面的
                #    `continue` 吞掉 ⇒ 改写静默失效（第一版就是这么写的）。
                for anc in (plan.get("anchors") or [])[:1]:
                    cands.append(f"{t['q']} {anc}")
            for t in plan.get("terms") or []:
                cands.append(t["q"])
            seen_q = {(query or "").strip().lower()}
            for cand in cands:
                key = cand.strip().lower()
                if not key or key in seen_q:
                    continue
                seen_q.add(key)
                try:
                    o2 = json.loads(inner(**{**a, "query": cand}))
                except Exception:                          # noqa: BLE001
                    continue
                if not isinstance(o2, dict) or not (o2.get("hits") or []):
                    continue
                o2 = dict(o2)
                o2["query_rewritten"] = {
                    "from": query, "to": cand,
                    "why": "问句已剥壳，并换成文献里的叫法（同义扩展）"}
                o2["note"] = (f"这次是零命中后改写的：原串『{query}』"
                              f"→ 『{cand}』。改写只发生在"
                              "**零命中且像人话问句**的情况，检索词串不会被改写。")
                return _kb_finalize(o2)

        # ② 多词 AND 救援（2026-11-04）：**只在原串零命中**时启动。
        #    litkb 是真 AND，`菌丝生长速率 菌落直径` / 英文四词串这类查询
        #    每个词单独都搜得到、整串却没有同段共现 ⇒ 0 命中。逐词召回后
        #    按「命中了几个词」排序，并显式标注这是**较宽的候选**，不是原串成立。
        if not (o.get("hits") or []):
            terms = _kb_relax_terms(query)
            if terms:
                buckets: dict[tuple, dict] = {}
                for t in terms:
                    try:
                        ot = json.loads(inner(**{**a, "query": t,
                                                 "n": max(n, 10)}))
                    except Exception:                          # noqa: BLE001
                        continue
                    if not isinstance(ot, dict):
                        continue
                    for h in (ot.get("hits") or []):
                        if not isinstance(h, dict):
                            continue
                        key = (h.get("doc_id"), h.get("page") or h.get("page_no"))
                        slot = buckets.setdefault(key, {"hit": dict(h), "terms": []})
                        if t not in slot["terms"]:
                            slot["terms"].append(t)
                ranked = sorted(buckets.values(),
                                key=lambda e: (-len(e["terms"]),
                                               str(e["hit"].get("doc_id") or "")))[:n]
                if ranked:
                    merged: list[dict] = []
                    for slot in ranked:
                        h = dict(slot["hit"])
                        h["via"] = "per_term"
                        h["matched_terms"] = slot["terms"]
                        merged.append(h)
                    o = dict(o)
                    o["hits"] = merged
                    o["query_relaxed"] = {
                        "from": query, "terms": terms,
                        "why": "原串是 AND 且零命中；逐词召回后按共现词数排序"}
                    o["note"] = (
                        f"原串『{query}』按 AND 检索零命中，已拆成 {'、'.join(terms)} "
                        f"分别召回、按共现词数排序，补回 {len(merged)} 条。"
                        "**这是较宽的候选**：只保证命中过其中某个词，"
                        "不保证同一段落同时满足全部条件，引用前请自己核页。")
                    return _kb_finalize(o)

        # ③ 两字词 LIKE 救援（原有能力，边界一字未动）
        shorts = _kb_short_terms(query)
        if not shorts:
            return _kb_finalize(o)           # 没有两字词 ⇒ 上游行为已经完整

        try:
            from warnings import catch_warnings, simplefilter
            w = _import_workbench(workbench_dir)
            manager = getattr(w, "_kb_module", None)
            if manager is None:
                return _kb_finalize(o)
            with catch_warnings():
                simplefilter("ignore")
                lib, _root = manager("")
            # 上游 hits 的条目形状是 {doc_id,page,year,title,snippet} ——
            # **不是** search_paras 的 {page_no,text}。第一版我照后者写，
            # 结果补进来的条目字段名对不上、下游读 snippet 会拿到空。
            # 这里按上游形状补齐：page/snippet 是必需项，title/year 尽量给。
            items = lib._load_library()
            by_id = {it["id"]: it for it in items}
            extra: list[dict] = []
            seen = {(h.get("doc_id"), h.get("page")) for h in (o.get("hits") or [])}
            for t in shorts:
                for p in _kb_like_paras(lib, t, doc_ids=doc_ids, n=n):
                    key = (p.get("doc_id"), p.get("page_no"))
                    if key in seen or not (p.get("text") or "").strip():
                        continue
                    seen.add(key)
                    meta = by_id.get(p.get("doc_id")) or {}
                    extra.append({
                        "doc_id": p.get("doc_id"),
                        "page": p.get("page_no"),
                        "year": meta.get("year", ""),
                        "title": (meta.get("title") or "")[:70],
                        "snippet": (p.get("text") or "")[:400],
                        "via": "like",
                    })
                    if len(extra) >= n:
                        break
                if len(extra) >= n:
                    break
        except Exception:                                      # noqa: BLE001
            return raw                       # 补丁层出错 ⇒ 退回上游原结果

        if not extra:
            return _kb_finalize(o)
        o = dict(o)
        o["hits"] = (o.get("hits") or []) + extra
        # 如实标注这一步做了什么，供模型与审计追溯（不是错误，是能力边界）
        o["short_terms_rescued"] = shorts
        o["note"] = (
            f"查询里的 {'、'.join(shorts)} 是两字词，本库索引（trigram）存不下，"
            f"FTS 搜不到；已用子串匹配补回 {len(extra)} 条。"
            "要更精确可用三字以上的词。"
        )
        return _kb_finalize(o)

    return kb_search


# ══════════════════ kb_index —— 全库清单，分页，永不被截断 ═══════════════════
# 为什么必须另加一个工具，而不是让模型多调几次 kb_list：
#
#   实测（2026-10-15，课题 hirsutella-sinensis-breeding）：库 216 条，
#   「doc_id + 标题」的紧凑清单共 **25,081 字符**，而注册表对单次工具结果的
#   上限是 `ToolRegistry.call(truncate=12000)`（见 tools.py）。
#   ⇒ **一次 kb_list 在架构上就不可能展示全库**，模型只能看到前一半。
#
#   模型对此的反应是**正确的**：它发现回包不全，于是改用小批量 kb_search
#   一轮轮往回捞。但它无法知道"还差多少"，于是候选池计数来回反复
#   （实测轨迹：70 → 38 → 50 → 65 → 70），**35 步全耗在盘库上**，
#   契约里排在第二位的「补检索」一步都没开始。
#
#   这不是模型的错，是**工具面缺一件东西**：一个能说清"全库有什么、你看到第几页、
#   还剩几页"的入口。缺了它，模型只能靠试错拼图，而试错在 12k 截断下永远收敛不了。
#
#   所以 `kb_index` 只做一件事：**按页给全库的 doc_id + 标题 + 年份 + 是否有全文**，
#   每页有界，并在回包里明说 `total` / `page` / `n_pages` / `has_more` —— 让模型
#   **一眼知道边界**。再配一个 `query` 做子串过滤（不是 FTS，就是朴素的包含匹配），
#   这样"库里跟 X 有关的都有哪些"变成**一次调用**，而不是几十次 kb_search。
#
# ★ 页大小 40 是**标定出来的**，不是拍的（`_calib_kbindex.py`，全 216 条实测）：
#      n=20→最坏页 4813 字   n=30→6964   n=40→9096   n=50→11139   n=60→12024 ✗
#   截断的后果比"清单不全"严重得多 —— JSON 会被**拦腰砍断**，
#   模型拿到的是坏 JSON（`Invalid control character at char 12000`），
#   它只会看到"解析失败"，根本不知道是页开大了。
#   n=50 余量只剩 861 字符，而标题长度是会变的；40 留 2904 字符余量，稳。
_KB_INDEX_DEFAULT_N = 40


def _kb_index(workbench_dir: Path):
    """生成 `kb_index` 的执行体。独立于 workbench 的反射路 —— 这是荣珂自己的工具。"""

    def kb_index(query: str = "", page: int = 1, n: int = _KB_INDEX_DEFAULT_N) -> str:
        w = _import_workbench(workbench_dir)
        mgr = getattr(w, "_kb_module", None)
        if mgr is None:
            raise BridgeError("workbench 上没有 _kb_module（上游结构可能变了）")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            lib, _root = mgr("")
        items = lib._load_library()

        q = (query or "").strip().lower()
        rows = []
        for it in items:
            did = it.get("id") or it.get("doc_id") or ""
            title = (it.get("title") or "").replace("\n", " ").strip()
            year = str(it.get("year") or "")
            if q and q not in f"{title} {year} {did}".lower():
                continue
            rows.append({
                "doc_id": did,
                "title": title,
                "year": year,
                # 如实标"抽出了文本"，不是"原件还在"（本仓记过这个区别）
                "fulltext": bool(it.get("has_fulltext")),
            })

        total = len(rows)
        # ★ 上限 50 同样是标定值，不是拍的：n=100 时最坏页 12,024 字符 > 12,000，
        #   回包被截断成坏 JSON（我的负控就是这么抓到的）。50 是实测仍在上限内
        #   的最大值（11,139），留 861 字符余量；默认取 40 再留一层。
        per = max(1, min(int(n or _KB_INDEX_DEFAULT_N), 50))
        n_pages = max(1, (total + per - 1) // per)
        pg = max(1, min(int(page or 1), n_pages))
        start = (pg - 1) * per
        chunk = rows[start:start + per]

        return json.dumps({
            "ok": True,
            "query": query,
            "total": total,            # 命中的**全部**条数
            "page": pg,
            "n_pages": n_pages,
            "n": len(chunk),
            "has_more": pg < n_pages,
            "library_total": len(items),   # 库里一共多少条（不受 query 影响）
            "items": chunk,
            "note": ("这是**全库清单**（doc_id + 标题 + 年份），按页给，"
                     "回包恒在长度上限内，所以 `total`/`n_pages` 是可信的边界 —— "
                     "不必再用多次 kb_search 去拼全库。"
                     "要看某条的内容用 kb_read；要按主题找段落用 kb_search。"
                     "要给参考文献取权威元数据用 kb_bibtex。"),
        }, ensure_ascii=False)

    return kb_index


KB_INDEX_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "可选。子串过滤（匹配标题/年份/doc_id），留空=全库。"
                           "例：『被毛孢』『原生质体』『冬虫夏草』。",
        },
        "page": {"type": "integer", "description": "第几页，从 1 起。默认 1。"},
        "n": {"type": "integer",
              "description": "每页几条，默认 40（上限 50）。"
                             "★ 不要为了省次数把 n 开很大：单次结果有长度上限，"
                             "开过头（实测 >50）会把 JSON 截断，你拿到的是坏数据而不是全清单。"
                             "要多看就翻页，不要加大 n。"},
    },
}


def _wrap_kb_ask(inner, workbench_dir: Path):
    """给 `kb_ask` 挂荣珂侧兜底：上游因为**没配模型**答不了时，用荣珂的模型答。"""
    sig = inspect.signature(inner)

    @functools.wraps(inner)
    def kb_ask(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        raw = inner(*args, **kwargs)

        # ① 上游答出来了（有正文）⇒ 原样返回，一个字都不碰。
        try:
            o = json.loads(raw)
        except Exception:                            # noqa: BLE001
            return raw
        if not isinstance(o, dict):
            return raw
        if str(o.get("answer") or "").strip():
            return raw

        err = str(o.get("error") or "")
        # ② 只在"上游自己答不了"时才兜底。别的错（id 不存在 / 库为空）
        #    是**真错误**，兜底会把它盖掉 —— 那才是把可用性问题变成数据问题。
        dead_ai = ("AI 未配置" in err) or ("没有可用的文献内容" in err)
        if not dead_ai:
            return raw

        question = str(bound.arguments.get("question") or "")
        ids = bound.arguments.get("ids")
        if isinstance(ids, str):
            ids = [x.strip() for x in ids.split(",") if x.strip()] or None
        n_docs = int(bound.arguments.get("n_docs") or 3)
        library = str(bound.arguments.get("library") or "")

        try:
            frags, refs, meta = _kb_ask_fragments(question, ids=ids,
                                                  n_docs=n_docs, library=library)
        except Exception as e:                       # noqa: BLE001
            o["rongke_fallback"] = f"取片段失败：{type(e).__name__}: {e}"
            return json.dumps(o, ensure_ascii=False)

        # 回包的**公共骨架**：旧键全留（refs / note / searched_terms /
        # upstream_error），新键只增不改 —— 下游有断言在读旧键。
        base = {
            "ok": True,
            "refs": refs[:12],
            "rongke_fallback": True,
            "term_plan": meta["term_plan"],
            "coverage": meta["coverage"],
            "passages": meta["passages"],
            "note": _KB_ASK_FALLBACK_NOTE,
            "searched_terms": _kb_ask_terms(question),
            "upstream_error": err,
        }

        if not frags:
            # 真的没有材料 —— 照实说，但**必须说清是哪一层没有**。
            # ★ 这里返回的是**答案**，不是报错：用户问的是问题，答案就是
            #   "查过了，这批库里没有"。做成 ok:false 会让人以为工具坏了，
            #   而事实是工具查过了 —— 两者必须分得开。
            # ★ 旧版本只有一句「你的文献库里没有可用于回答这个问题的材料」，
            #   用户读成「世上没人做过」。实测：`核质染色` 全库 0 次，
            #   而 DAPI 有 50 段 —— 词层断的，不是主题断的。
            verdict = _kb_zero_verdict(meta, question)
            base["answer"] = verdict["text"]
            base["zero_why"] = verdict["why"]
            return json.dumps(base, ensure_ascii=False)

        from .llm import LLM                          # 局部导入避免循环依赖
        cfg = Config.load()
        system = ("你是科研文献助教。**严格基于**提供的文献片段回答问题；"
                  "回答必须标注引用（如「[标题, p.3]」）；"
                  "片段没有的信息明确说「文献中未提及」，**禁止编造**；"
                  "禁止引入片段之外的任何知识。"
                  "涉及方法参数（浓度/时长/温度/波长）时，**原样抄数字与单位**，"
                  "不同文献给的数不一致就都列出来，不要择一。")
        user = ("## 文献片段\n\n" + "\n".join(frags) + "\n## 问题\n" + question +
                "\n\n请用中文回答（保留原文术语），结论给引用出处。")
        answer, gen_err = "", ""
        try:
            llm = LLM(cfg.provider)
            # ★ chat() 回的是**三元组** `(msg, usage, reasoning)`，不是正文本身。
            #   第一版我写成 `resp.get("content")`，答案里直接印出了整个
            #   message dict 的 repr —— 既有隐忧也不可读。
            msg, _usage, _reasoning = llm.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}])
            answer = str((msg or {}).get("content") or "").strip()
        except Exception as e:                       # noqa: BLE001
            gen_err = f"{type(e).__name__}: {e}"

        if not answer:
            # 归纳这一步失败**不该把原文一起丢掉**：passages 已经在手，
            # 主模型可以直接引用。旧版本这里返回上游 o（里头没有任何片段），
            # 于是"生成失败"被读成了"库里没有"——一次典型的静默假失败。
            base["answer"] = (
                "（归纳这一步没成功："
                + (gen_err or "模型返回空正文")
                + "。命中的**原始段落**已在 `passages` 里，"
                  "可直接按 doc_id + 页码引用。）")
            base["note"] = "荣珂模型未产出归纳，已把命中原文回传到 passages。"
            return json.dumps(base, ensure_ascii=False)

        base["answer"] = answer
        return json.dumps(base, ensure_ascii=False)

    return kb_ask


def _import_workbench(workbench_dir: Path):
    """把 workbench 挂进 sys.path 并导入 workbench_mcp（幂等）。"""
    if not workbench_dir.exists():
        raise BridgeError(f"workbench 目录不存在: {workbench_dir}")
    entry = workbench_dir / "workbench_mcp.py"
    if not entry.exists():
        raise BridgeError(f"找不到 {entry}")

    sdir = str(workbench_dir)
    if sdir not in sys.path:
        sys.path.insert(0, sdir)

    # 导入 workbench_mcp 会触发 pydantic_settings 的一条无关告警（lifespan 前向引用），
    # 它只污染 stderr，不影响功能——这里压掉，免得混进荣珂的诊断输出。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import workbench_mcp  # type: ignore

    _apply_kb_download_switch(workbench_dir)
    return workbench_mcp


def _apply_kb_download_switch(workbench_dir: Path) -> None:
    """把荣珂配置里的 `kb.scihub_fallback` 交给 schubdl 的运行时开关。

    ★ 为什么要在这里做：schubdl 自己有**环境变量**开关（`SCHUBDL_ENABLE`），
      但用户是**在 config.json 里配**的。两套开关各自为政的话，
      "我明明配了 True 却没生效"就成了必然 —— 而这正是本仓最防的那种
      **静默失效**（不报错，只是不按你配的来）。
      ⇒ 统一在唯一入口（workbench 只从这里被导入）把配置**推**下去。

    取不到配置时**什么都不做**（保持 schubdl 自己的默认：关）。
    这是有意的：拿不到依据时不该把它打开。
    """
    try:
        from .config import Config
        kb = Config.load().kb
        on = bool(getattr(kb, "scihub_fallback", False)) if kb is not None else False
    except Exception:                                   # noqa: BLE001
        return
    try:
        import schubdl  # type: ignore
        schubdl.enable(on)
    except Exception:                                   # noqa: BLE001
        # schubdl 不在（老工作台）⇒ 少一条兜底路，不是故障。
        pass


def reflect_workbench_tools(workbench_dir: Path) -> list[tuple[str, str, dict, object]]:
    """反射出 workbench 的全部工具：[(name, description, json_schema, fn), ...]"""
    w = _import_workbench(workbench_dir)
    manager = getattr(w.mcp, "_tool_manager", None)
    if manager is None:
        raise BridgeError("FastMCP 实例上没有 _tool_manager（上游结构可能变了）")

    out: list[tuple[str, str, dict, object]] = []
    for t in manager.list_tools():
        fn = getattr(w, t.name, None) or getattr(t, "fn", None)
        if fn is None:
            continue
        # 两处例外，都在这一条路上包（agent 的工具表、面板、测试的反射路**都从
        # 这里走**；包在调用方各自包就会漂成两副面孔 —— 本项目反复吃的 H 型）。
        #
        # · kb_ingest —— 荣珂侧**预检**：别让 dry_run 对非 PDF 兑现假承诺
        #   （见 `_wrap_kb_ingest_precheck`；纯 PDF 批次原样透传）。
        # · kb_ask —— 荣珂侧**兜底**：litkb 自带的 AI 没配置时，用荣珂的模型
        #   完成它那一步生成（见 `_wrap_kb_ask`；上游能答就一个字不插手）。
        # · kb_search —— 荣珂侧**补短词**：litkb 的 trigram 索引搜不到两字词
        #   （融合/温度/酶解…），用 LIKE 补回（见 `_wrap_kb_search`；
        #   没有两字词时原样透传）。
        if t.name == "kb_ingest":
            fn = _wrap_kb_ingest_precheck(fn)
        elif t.name == "kb_ask":
            fn = _wrap_kb_ask(fn, workbench_dir)
        elif t.name == "kb_search":
            fn = _wrap_kb_search(fn, workbench_dir)

        desc = (getattr(t, "description", "") or "").strip()
        # 把**检索词长**这条能力边界写进工具描述 —— 模型看不见 bridge.py 的注释，
        # 它只看得到这段 desc。上一个版本没写，模型于是自己猜，猜出来的规则是
        # 错的（「多词一律返回 0，故用单词项」），还把它写进了开题报告正文。
        # 能力边界必须**由提供方明说**，不能让调用方靠试错去悟。
        if t.name == "kb_search":
            desc += (
                "\n\n  ★ 检索词长度有一条硬边界（本库用 trigram 索引）："
                "查询里**少于 3 个汉字的词搜不到**（如「融合」「温度」「酶解」）。"
                "两个办法：一是写成三字以上的组合词（「原生质体融合」比「融合」准得多）；"
                "二是照常写两字词 —— 荣珂会自动用子串匹配补回一批，"
                "并在回包 `short_terms_rescued` 里如实告诉你补了哪几个词。"
                "另：**多词查询是正常的**（`原生质体 融合` 有结果），"
                "不要因为一次没中就改成单词项。"
                "\n  ★ 也可以直接问整句（「核质染色怎么做」）：零命中且像人话问句时，"
                "荣珂会把问句剥壳并换成文献用词再查一次，回包 `query_rewritten` "
                "写明改了哪些词。检索词串（如 `酶解 温度`）不会被改写。"
            )
        elif t.name == "kb_read":
            # ★ 2026-10-15：内核**因为不会翻页而误判「数据不可得」**，把已经取到的
            #   真实数据当不可溯源而丢弃（run 20261015-220246 第 42 步）。
            #   实测：kb60993237 全文 6 页 12366 字，表 2 的逐格数值在 **第 4 页**
            #   （offset 8190）。内核不带页码调 kb_read，默认从第 1 页起、只回
            #   max_chars=6000 字 ⇒ **永远读不到第 4 页的表**，于是它得出
            #   「绝对数值不在可检索片段中」这个错误结论，并把图里的真数据撤回。
            #   能力边界必须由提供方明说：告诉它「要翻页」，并告诉它回包里有页码字段。
            desc += (
                "\n\n  ★ 全文是**分页**的，默认只回开头一段（`max_chars` 默认 6000）。"
                "**表格、逐格数值、方法细节通常在后半页**，所以："
                "① 回包里的 `pages_total` 告诉你这篇共几页；"
                "② `truncated: true` 表示你看到的**不是全部**；"
                "③ 要后面的内容就**带页码再读一次** —— `page_from=4, page_to=4`。"
                "**不要因为第一次没看到某个数字，就断言「该数据不在库里」** ——"
                "多半是它在你没翻到的那一页。"
                "提取表格数值时，逐页读完再用；`char_total` 是全篇总字数。"
            )
        elif t.name == "kb_ask":
            desc += (
                "\n\n  ★ 问句可以自然语言直接问（「核质染色怎么做」就能问），"
                "荣珂会剥掉问句外壳、把术语换成文献里的叫法"
                "（核质染色 → DAPI / 细胞核染色 / 荧光染色），并带领域锚做 AND 约束。"
                "回包里的 `term_plan` 告诉你试了哪些词、`coverage` 告诉你"
                "每个锚命中多少段（`anchor_hits` 是**下界**，n=1 探的）。"
                "★ 零命中时**看 `zero_why` 再决定怎么办**："
                "`word`＝这个主题库里有、只是没用这个词写（换说法再问）；"
                "`topic`＝库里确实没这个主题；"
                "`unknown`＝这次分不清是没主题还是没词 —— "
                "**不许把 `unknown` 说成「库里没有」**。"
                "★ `passages` 是命中的**原始段落**（带 doc_id + 页码，每段 ≤600 字）。"
                "讲方法参数（浓度/时长/波长）时以它为准，不要用 `answer` 里的转述。"
                "★ 注意：`kb_ask` 只在**检索到的片段**里找答案，因此它答不出某个数字"
                "**不等于库里没有这个数字**。要逐格的表格数值，用 `kb_read` 翻到对应页取，"
                "不要用 kb_ask 的「查不到」去否定 kb_read 能看到的内容。"
            )
        out.append((
            t.name,
            desc,
            getattr(t, "parameters", None) or {},
            fn,
        ))
    return out


def install_workbench(
    registry: ToolRegistry,
    workbench_dir: Path,
    domain: str = "research",
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    kb_domain: str = "",
) -> list[str]:
    """把 workbench 工具注册进荣珂。返回注册成功的工具名。

    `kb_domain` 非空时，名字以 `kb_` 开头的工具改挂到那个域（W25 导师模式
    按需挂载）：文献知识库是**课题组专用**的资产，不该让"写一段引言"也付
    这 11 个工具的 schema。其余工具（literature_search / web_search / 图 / docx…）
    照旧进 `domain` —— 它们是科研任务的通用件，留在常驻域。
    只认 `kb_` 前缀而不维护一份名单：名单是第二处真相，workbench 加一个
    kb_* 工具名单忘了跟，那个工具就会静默常驻 —— 前缀规则不会。
    """
    allow_set = set(allow) if allow else None
    deny_set = set(deny or [])
    installed: list[str] = []

    # ── 荣珂自己的 kb_index：全库分页清单 ──────────────────────────────
    # 放在反射路**之前**注册。它是荣珂侧的补件（上游没有），
    # 存在的理由见 `_kb_index` 上方那段：单次结果被截断在 12k，
    # 而全库紧凑清单 25k ⇒ 没有它，模型拼不出"库里有什麼"的全貌。
    # 它同样吃 `kb_domain` 那条前缀规则（名字以 kb_ 开头 ⇒ 随文献域按需挂载）。
    if (allow_set is None or "kb_index" in allow_set) and "kb_index" not in deny_set:
        registry.register(Tool(
            name="kb_index",
            description=(
                "列出文献库的**全库清单**（doc_id + 标题 + 年份 + 是否有全文），按页返回。\n\n"
                "★ 与 kb_list 的区别：kb_list 的回包**会在长度上限处被截断**，"
                "库大时看不到全库；kb_index 每页有界，回包恒带 total / page / n_pages / has_more，"
                "所以**边界是可信的** —— 盘库用这一个工具就够，不必用几十次 kb_search 去拼。\n\n"
                "用 query 做子串过滤（不是全文检索）：想知道『库里跟中国被毛孢有关的都有哪些』，"
                "传 query='被毛孢' 一次调用即可。\n\n"
                "它只给清单。看内容用 kb_read；找段落用 kb_search；取参考文献元数据用 kb_bibtex。"
            ),
            parameters=KB_INDEX_SCHEMA,
            fn=_kb_index(workbench_dir),
            domain=(kb_domain or domain),
            mutating=False,
            source="rongke",
        ))
        installed.append("kb_index")

    # ★ workbench 是**可选**扩展（外部包 + 它自己的第三方依赖）。它导入失败
    #   不能让整个程序起不来 —— 实测：一个刚 `pip install rongke` 的干净 venv
    #   里没有 `mcp` 这个包，于是 `rongke --tools` 直接 traceback 退出，
    #   连"哪些功能可用"都问不出来。用户看到的是"装好了但一敲就崩"。
    #   真实场景里 workbench 有三种正常的"不在"：没装、装了但缺依赖、路径配错。
    #   三种都该降级成"少这几件工具 + 在诊断里说清楚原因"，而不是炸。
    try:
        wb_tools = reflect_workbench_tools(workbench_dir)
    except Exception as e:                              # noqa: BLE001
        # 只记录原因，**不往 installed 里塞假名字** —— `installed` 是
        # "装上了哪些工具"的清单，塞标记进去会让下游把它当工具名。
        install_workbench.last_error = f"{type(e).__name__}: {e}"
        return installed
    install_workbench.last_error = ""

    for name, desc, schema, fn in wb_tools:
        if allow_set is not None and name not in allow_set:
            continue
        if name in deny_set:
            continue
        registry.register(Tool(
            name=name,
            description=desc,
            parameters=schema,
            fn=fn,
            domain=(kb_domain or domain) if name.startswith("kb_") else domain,
            mutating=name in MUTATING,
            source="workbench",
        ))
        installed.append(name)
    return installed
