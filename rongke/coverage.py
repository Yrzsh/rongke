# -*- coding: utf-8 -*-
"""文献够用性判定 —— 回答「这个题目，我库里的文献够不够写」这一句。

## 为什么必须有这一步

用户的流程第一条是「先检索目前知识库中的文献是否够用，不够才去检索入库」。
在此之前，荣珂里**根本没有这个判定**：全仓 grep `够用|够不够|suffic|coverage`
在文献语料上是空集。于是模型接到「写一篇综述」就去检索、入库、开写，
一路烧到 200 轮才发现某几章其实没有文献支撑 —— 那就是返工的根源。

## 判据是什么（可机械复算，不看感觉）

把题目拆成若干**主题词**（由模型给，本模块只算账），对每个主题词在库里检索，
数三样：

| 量 | 含义 |
|---|---|
| `hits` | 命中段落数 |
| `docs` | 命中的**不同文献**数（去重 doc_id） |
| `pages` | 命中的页码集合大小 |

然后按阈值判：

- `docs == 0` ⇒ **硬缺口**：这一章在库里一个字都没有，必须补文献
- `0 < docs < MIN_DOCS_PER_TOPIC` ⇒ **薄**：有，但撑不起一节
- `docs >= MIN_DOCS_PER_TOPIC` ⇒ **够**

## ★ 两个必须避开的坑（都是本机实测过的）

### ① 两字词会被 trigram 索引静默丢弃

litkb 的索引对**两字词**（融合 / 温度 / 酶解）会整条查询不执行、静默回空。
这条已复现过，并且被误归因成「多词 AND 恒空」了很多次。

⇒ 本模块**不收两字主题词**：`_usable_terms()` 会把它们挑出来，单列在
`dropped_short_terms` 里如实回报 —— 而不是让它们变成假的"0 命中"。
模型看到这个字段就知道该换成长词或英文，而不是得出"库里没有"的结论。

### ② 检索用的是 `kb_search`，不是 `kb_list`

`kb_list` 只告诉你"库里有哪些篇"，不回答"这些篇覆盖不覆盖这个题目"。
覆盖必须落到**段落级命中**。这与 `kb_rank` 的分工也清楚：`kb_rank` 给篇级排序，
本模块给话题级覆盖账。
"""
from __future__ import annotations

import re

#: 撑起一节至少要有的不同文献数。3 是照"一节至少三段、每段能引不同文献"估的。
MIN_DOCS_PER_TOPIC = 3

#: 撑满一篇综述，覆盖达标的章节占比。低于它就整体判「不够」。
MIN_OK_RATIO = 0.6

_CJK = re.compile(r"[\u3400-\u9fff]")


def _usable_terms(terms: list[str]) -> tuple[list[str], list[str]]:
    """把主题词分成「能检索的」与「会被索引丢掉的」。

    两字中文词走 trigram 必然静默回空（见模块文档 ①），所以挑出来单列。
    纯英文另算：英文按词切，不受中文 trigram 的两字限制。
    """
    ok: list[str] = []
    short: list[str] = []
    for t in terms or []:
        s = str(t or "").strip()
        if not s:
            continue
        if _CJK.search(s) and not re.search(r"[A-Za-z]", s) and len(s) <= 2:
            short.append(s)
        else:
            ok.append(s)
    return ok, short


def _doc_ids_of(hits: list) -> set[str]:
    """从 `kb_search` 的 `hits[]` 里取 doc_id。**只认真的出现过 doc_id 的条目。**"""
    out: set[str] = set()
    for h in hits or []:
        if isinstance(h, dict):
            d = str(h.get("doc_id") or "").strip()
            if d:
                out.add(d)
    return out


def _dropped_note(topic_rows: list[dict]) -> dict:
    """把输入里被索引丢掉的两字词挑出来单列。

    ★ 必须在 `assess` 里报出来，而不是悄悄过滤：被丢掉的词回的是空结果，
    看上去和"库里真的没有"一模一样。不报，模型就会得出"这章零文献"的假结论
    并去补一圈本来不需要补的文献。
    """
    short: list[str] = []
    for it in topic_rows or []:
        raw = it.get("term") or ""
        # 原样再判一次：调用方可能已经筛过，这里只补一条如实记录
        for t in ([raw] if isinstance(raw, str) else list(raw)):
            s = str(t or "").strip()
            if s and _CJK.search(s) and not re.search(r"[A-Za-z]", s) and len(s) <= 2:
                if s not in short:
                    short.append(s)
    return {"terms": short,
            "why": ("中文两字词在 litkb 的 trigram 索引里会**静默返回空**。"
                    "这些词的结果不能当作「库里没有」——请换三字以上或英文再判。")}


def assess(topic_rows: list[dict],
           min_docs: int = MIN_DOCS_PER_TOPIC,
           min_ok_ratio: float = MIN_OK_RATIO) -> dict:
    """算出够不够。**纯函数**：输入是「每个主题词的检索结果」，输出是判断。

    输入形状（由调用方喂进来，本模块不发请求）::

        [{"term": "原生质体融合", "ok": True, "hits": [...], "error": ""}, ...]

    为什么要这样一种"半成品"输入：检索那一步是 `kb_search`，它的回包形态
    由 workbench 决定；把解析与判断分开，这样判断这一段能被单测直接打穿，
    不需要联网、也不需要库。
    """
    rows: list[dict] = []
    for it in topic_rows or []:
        term = str(it.get("term") or "").strip()
        if not term:
            continue
        ok_flag = bool(it.get("ok", True))
        err = str(it.get("error") or "")
        hits = it.get("hits") or []
        docs = _doc_ids_of(hits)
        pages: set[tuple[str, str]] = set()
        for h in hits:
            if isinstance(h, dict) and h.get("doc_id"):
                pages.add((str(h["doc_id"]), str(h.get("page", ""))))
        if not ok_flag:
            verdict = "检索失败"
        elif not docs:
            verdict = "硬缺口"
        elif len(docs) < min_docs:
            verdict = "薄"
        else:
            verdict = "够"
        rows.append({
            "term": term,
            "verdict": verdict,
            "docs": sorted(docs),
            "docs_n": len(docs),
            "hits_n": len(hits),
            "pages_n": len(pages),
            "error": err,
        })

    total = len(rows)
    ok_rows = [r for r in rows if r["verdict"] == "够"]
    thin = [r for r in rows if r["verdict"] == "薄"]
    gap = [r for r in rows if r["verdict"] == "硬缺口"]
    err = [r for r in rows if r["verdict"] == "检索失败"]
    ratio = (len(ok_rows) / total) if total else 0.0

    # 判定口径：**有硬缺口就一定不够**（那是"这一章零文献"），
    # 否则看达标比例。这个方向是刻意的 —— 一条硬缺口不能被"其余都够"抵掉。
    if not total:
        need = False
        verdict = "无从判断"
        why = "没有可判定的主题词（全部被丢弃或为空）"
    elif gap or err:
        need = True
        verdict = "不够"
        why = f"{len(gap)} 个主题词零命中、{len(err)} 个检索失败"
    elif ratio < min_ok_ratio:
        need = True
        verdict = "不够"
        why = (f"达标章节只占 {ratio:.0%}（阈值 {min_ok_ratio:.0%}）："
               f"{len(ok_rows)}/{total} 够、{len(thin)} 薄")
    else:
        need = False
        verdict = "够用"
        why = f"{len(ok_rows)}/{total} 个主题词达标（{ratio:.0%} ≥ {min_ok_ratio:.0%}）"

    # 全库覆盖：把各主题词的 doc_id 并起来 —— 这是「这一篇综述能引多少篇」的下界，
    # 不是精确值（检索只回前 n 段），回包里必须这么写。
    union: set[str] = set()
    for r in rows:
        union |= set(r["docs"])

    return {
        "verdict": verdict,
        "need_more_literature": need,
        "why": why,
        "thresholds": {"min_docs_per_topic": min_docs,
                       "min_ok_ratio": round(min_ok_ratio, 2)},
        "topics": rows,
        "ok_ratio": round(ratio, 3),
        "distinct_docs_seen": len(union),
        "distinct_docs": sorted(union),
        "gaps": [{"term": r["term"], "docs_n": r["docs_n"]} for r in gap],
        "thin": [{"term": r["term"], "docs_n": r["docs_n"]} for r in thin],
        "failed": [{"term": r["term"], "error": r["error"]} for r in err],
        "dropped_short_terms": _dropped_note(topic_rows),
        "caveat": ("`distinct_docs_seen` 是**下界**：检索每个词只回前若干段，"
                   "被截掉的部分不会出现在这里。它回答的是「至少能引几篇」，"
                   "不是「库里一共有几篇」。"),
    }


def next_action(rep: dict) -> str:
    """把判断翻成**下一步该做什么**的一句话（写进回包，让模型照着执行）。"""
    if rep["verdict"] == "无从判断":
        return ("主题词没给出可用项。先把题目拆成 5–8 个**三字以上或英文**的主题词"
                "（两字词会被索引静默丢弃），再判一次。")
    if rep["need_more_literature"]:
        gaps = rep.get("gaps") or []
        terms = "、".join(g["term"] for g in gaps[:6]) or "（见 failed 列表）"
        return (
            f"**库里不够**：{rep['why']}。\n"
            f"要补的主题词：{terms}\n"
            "补的顺序是：`literature_search` 找候选 → `literature_fetch` 取全文 "
            "→ `kb_add`/`kb_ingest` 入库 → **用同一批主题词再判一次**（`kb_coverage`）。\n"
            "★ 补到 `need_more_literature=false` 才进入大纲阶段；不要带着硬缺口去写。"
        )
    return (
        f"**库里够用**：{rep['why']}。\n"
        "**不要再做全量检索入库** —— 直接进入大纲阶段（装载 paper-spine，"
        "落盘 outline.md，逐章写清要引哪些 doc_id）。"
        "写作时缺哪一篇就只补那一篇，别重开一轮检索。"
    )
