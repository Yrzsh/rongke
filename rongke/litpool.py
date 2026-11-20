# -*- coding: utf-8 -*-
"""文献池单源：给每篇文献一个**稳定身份**，把检索/精读/大纲/引注接到同一份台账。

## 为什么不是「再做一个检索工具」

检索侧工具早就有，而且 11-09 的六环节综述工作流已端到端跑通：
`kb_coverage`（`coverage.py` 判库够不够）、`literature_search`（外部四源）、
`literature_fetch`（EuropePMC→Unpaywall→doi.org）、`kb_add` / `convert_pdf`、
`kb_search` / `kb_read`（段落级 / 按页）、`verify_citation`、`source_alignment`。
缺的不是工具数量。实测缺口是两处：

  ① **「不全面 / 相关性低」没有任何可判定基准**。`coverage.py:109` 判的是
     「库里覆盖不覆盖题目」，不是「检索全不全、相不相关」；
     `bridge.py:484 _kb_rank` 直接拿上游回包的 `score`，无独立相关性口径；
     全仓没有纳入/排除准则、没有逐篇判定留痕、没有结果级去重。
     `docs/综述流程-设计与改造方案.md:93`：新增 15 篇只拿到 3 篇全文（20%）。
  ② **五处事实互不互证**：元数据/全文在外部 litkb；精读在 `READING.md` /
     `EVIDENCE.md` 自由文本（`docs/mentor-mode-20260922.md:161` 证据表「只能手抄」）；
     大纲是自由文本 `outline.md`；章-文献映射只是提示词
     （`docs/综述流程-设计与改造方案.md:414` 待办明写「门只核有没有，不核对不对」）；
     正文引注在稿件里。它们谁也不读谁。

所以这里做的是 `图表登记.json` / `entries.jsonl` 那一层的东西：
**一个唯一事实源**，让已有工具都往里写、都从里读。

## 一条必须说清的边界（不冒充领域判断）

「这篇文献相不相关」是**领域判断**，工具不替人判，也不造一个假分数。
工具管的是可判定的那一半：

  · 准则**先声明**（`declare_criteria`），之后每篇都得按同一套准则判；
  · 判定必须**逐条留痕**（准则命中 + 理由 + 谁判的），没判 / 判了不说理由
    ⇒ `verify()` 点名 `UNSCREENED` / `SCREEN_NO_REASON`，不静默放行；
  · 精读卡必须带 `claim / page / quote`，且能回溯到有全文的条目；
  · 章-文献映射里的 `doc_id` 必须在池里、已纳入、已精读。

⇒ 判断留给人，「判没判、按什么判、留没留痕」交给工具。这与
`writing.py:19-20`「不评判论证/文献恰当/结论创新」是同一条纪律。

## 边界

  · 纯标准库；**不进 `/api/tools`**、不改 SYSTEM、不加硬门、不改现有枚举。
  · `verify()` 是 advisory 汇总：缺哪一环就点名哪一环，**不拦交付、不改门语义**。
  · `outline_view()` 是**视图**，不冒充大纲正文；章号是渲染产物，不持久化。
  · `summary()` 里的召回/全文率是**过程读数**，不是「检索质量结论」；
    与 `coverage.py` 的 `distinct_docs_seen 是下界` 同一种诚实口径。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

STATE_NAME = "文献池.json"
SCHEMA_VERSION = 1
VERDICTS = ("unscreened", "include", "exclude")

#: 与 `gate.py:_RE_DOCID` 同形的 doc_id / DOI 形状（DOI / kbNNN / hex / 期刊号）。
#: 不 import gate：那是 84KB 的门禁模块，本资产要坚持零依赖、可单测打穿。
DOCID_RE = re.compile(
    r"(?i)(?:10\.\d{4,9}/[^\s,，、;；)）\]】\"']+"
    r"|\bkb[0-9a-z]{6,}\b"
    r"|\b[0-9a-f]{8,}\b"
    r"|\b[a-z]{2,}[0-9]{3,}\b)")

#: `〔库·doc_id p.N〕` 形态的正文引注锚点；普通 `〔n〕` / `[n]` 编号引注
#: **不**在这里认 —— 那是编号，不是身份，混进来会把老式稿件全判成孤儿。
POOL_CITE_RE = re.compile(r"〔\s*库[·:：]\s*([^〕\s]+?)\s*p\.?\s*(\d+)\s*〕")

SPEC_GUIDE = (
    "文献池：queries=[{q, source, n_returned, n_dup, n_new}]、"
    "criteria=[{id, text}]、entries=[{doc_id, doi, title, year, found_by, "
    "fulltext, screening, cards, chapter}]。"
    "doc_id 稳定、与编号和章序无关；准则先声明，再逐篇留痕判定；"
    "精读卡带 claim/page/quote；大纲与正文只引用池里已纳入且已精读的条目。"
    "正文身份锚点写 〔库·doc_id p.N〕，普通〔n〕编号引注不认，避免误伤老式稿。"
)


def empty() -> dict:
    return {"schema": SCHEMA_VERSION, "queries": [], "criteria": [],
            "entries": []}


def _index(state: dict) -> dict[str, dict]:
    return {str(e.get("doc_id") or ""): e for e in state.get("entries", [])}


def _norm_id(s) -> str:
    return str(s or "").strip().lower()


def _result(ok: bool, **kw) -> dict:
    return dict({"ok": ok, "advisory": True}, **kw)


# ── 落盘：写后逐字节读回 ──────────────────────────────────────
def save(state: dict, path: str | Path = STATE_NAME) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    payload = json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True)
    try:
        p.write_text(payload, encoding="utf-8")
        back = p.read_text(encoding="utf-8")
    except OSError as ex:
        return _result(False, error=f"写/读回失败：{type(ex).__name__}: {ex}",
                       path=str(p))
    return _result(back == payload, path=str(p),
                   readback_equal=(back == payload), chars=len(payload))


def load(path: str | Path = STATE_NAME) -> dict:
    p = Path(path)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA_VERSION:
        return empty()
    for k in ("queries", "criteria", "entries"):
        if not isinstance(obj.get(k), list):
            obj[k] = []
    return obj


# ── 检索台账：召回 / 去重 / 全文率（过程读数，不是质量结论）────────
def declare_criteria(state: dict, criteria: list[dict]) -> dict:
    """准则单源。先声明，后判定；不允许边判边改准则而不重判。"""
    rows = []
    for c in criteria or []:
        cid = str(c.get("id") or "").strip()
        text = str(c.get("text") or "").strip()
        if cid and text:
            rows.append({"id": cid, "text": text})
    state["criteria"] = rows
    # 准则变了，旧判定不再算数：全部退回未判，逼逐篇重判。
    touched = 0
    for e in state.get("entries", []):
        if e.get("screening", {}).get("verdict") != "unscreened":
            e["screening"] = {"verdict": "unscreened", "criteria": {},
                              "reason": "", "by": ""}
            touched += 1
    return _result(True, criteria=len(rows), reset=touched,
                   next="准则变更已有判定全部退回未判，逐篇重判后再进大纲。")


def record_query(state: dict, q: str, source: str,
                 hits: list[dict]) -> dict:
    """把一次检索的回包入池：按 doc_id/DOI 去重，新条目默认 `unscreened`。

    `hits` 形状沿用 `kb_search` 回包：`[{doc_id, doi, title, year, ...}]`。
    不联网、不重排、不替调用方决定相关性。
    """
    q = str(q or "").strip()
    if not q:
        return _result(False, error="query 为空，不记台账")
    idx = _index(state)
    n_new = n_dup = 0
    for h in hits or []:
        if not isinstance(h, dict):
            continue
        did = _norm_id(h.get("doc_id") or h.get("doi") or h.get("id"))
        if not did:
            continue
        if did in idx:
            n_dup += 1
            continue
        e = {
            "doc_id": did,
            "doi": str(h.get("doi") or "").strip(),
            "title": str(h.get("title") or "").strip(),
            "year": h.get("year") or "",
            "found_by": q,
            "fulltext": {"ok": False, "chars": 0,
                         "text": str(h.get("text") or "")},
            "screening": {"verdict": "unscreened", "criteria": {},
                          "reason": "", "by": ""},
            "cards": [], "chapter": "",
        }
        state.setdefault("entries", []).append(e)
        idx[did] = e
        n_new += 1
    row = {"q": q, "source": str(source or ""),
           "n_returned": len(hits or []), "n_dup": n_dup, "n_new": n_new}
    state.setdefault("queries", []).append(row)
    return _result(True, **row, pool_size=len(state.get("entries", [])))


def mark_fulltext(state: dict, doc_id: str, text: str) -> dict:
    """全文落池。空正文不算拿到全文（对齐 `litfetch.MIN_BODY_CHARS=1200`
    的精神：不把「文件存在」当「正文可用」，但不硬编第三方阈值）。"""
    e = _index(state).get(_norm_id(doc_id))
    if e is None:
        return _result(False, error=f"池里没有 {doc_id}；先 record_query")
    body = str(text or "")
    e["fulltext"] = {"ok": bool(body.strip()), "chars": len(body),
                     "text": body}
    return _result(True, doc_id=_norm_id(doc_id), chars=len(body),
                   ok_fulltext=bool(body.strip()))


def note_fulltext(state: dict, doc_id: str, chars: int = 0,
                  ok: bool = False) -> dict:
    """只登记「取全文」这件事的元数据，不持有正文。

    给所有权层用：`literature_fetch` / `kb_add` 的回包只有 `ok/chars/file`，
    没有全文文本。**不编正文、不拿空串覆盖已有全文**；`ok` 必须由回包说
    了算（调用方诚实），本函数不替它判断「算不算取到了」。
    缺正文时 `add_card()` 的 `quote_in_fulltext` 会如实为 False。
    """
    e = _index(state).get(_norm_id(doc_id))
    if e is None:
        return _result(False, error=f"池里没有 {doc_id}；先 record_query")
    ft = e.setdefault("fulltext", {"ok": False, "chars": 0, "text": ""})
    had = str(ft.get("text") or "")
    ft["chars"] = max(int(ft.get("chars") or 0), int(chars or 0))
    ft["ok"] = bool(ok or had.strip() or ft["chars"] > 0)
    ft.setdefault("text", had)
    return _result(True, doc_id=_norm_id(doc_id), ok_fulltext=ft["ok"],
                   chars=ft["chars"], has_text=bool(had.strip()))


# ── 逐篇判定留痕：判断归人，留痕归工具 ────────────────────────
def screen(state: dict, doc_id: str, verdict: str, reason: str,
           criteria_hits: dict | None = None, by: str = "") -> dict:
    if verdict not in VERDICTS:
        return _result(False, error=f"verdict 只能是 {VERDICTS}")
    e = _index(state).get(_norm_id(doc_id))
    if e is None:
        return _result(False, error=f"池里没有 {doc_id}；先 record_query")
    declared = [c["id"] for c in state.get("criteria", [])]
    hits = {str(k): str(v) for k, v in (criteria_hits or {}).items()}
    missing = [c for c in declared if c not in hits]
    reason = str(reason or "").strip()
    if verdict != "unscreened" and (not reason or missing):
        return _result(False,
                       error="判定必须给理由且覆盖全部已声明准则",
                       missing_criteria=missing, reason=bool(reason),
                       next="补齐 criteria_hits 与 reason 再判一次；"
                            "这里不替你填，也不静默放行。")
    e["screening"] = {"verdict": verdict, "criteria": hits,
                      "reason": reason, "by": str(by or "")}
    return _result(True, doc_id=_norm_id(doc_id), verdict=verdict,
                   criteria_covered=len(declared))


def add_card(state: dict, doc_id: str, claim: str, page: int,
             quote: str) -> dict:
    """精读卡：`claim / page / quote` 三件缺一不可，且必须能回溯到全文。"""
    e = _index(state).get(_norm_id(doc_id))
    if e is None:
        return _result(False, error=f"池里没有 {doc_id}")
    claim, quote = str(claim or "").strip(), str(quote or "").strip()
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    if not claim or not quote or page <= 0:
        return _result(False, error="精读卡必须给 claim / page>0 / quote",
                       claim=bool(claim), page=page, quote=bool(quote))
    ft = e.get("fulltext") or {}
    if not ft.get("ok"):
        return _result(False, error="条目没有全文，不能凭空写精读卡",
                       next="先 mark_fulltext 或 literature_fetch 取全文。")
    body = str(ft.get("text") or "")
    quote_in_fulltext = bool(body) and quote in body
    card = {"doc_id": _norm_id(doc_id), "claim": claim, "page": page,
            "quote": quote, "quote_in_fulltext": quote_in_fulltext}
    e.setdefault("cards", []).append(card)
    return _result(True, **card,
                   next=("证据句未在全文里逐字读到，如实标记，不等于通过"
                         if not quote_in_fulltext else "证据句可回溯"))


def upsert(state: dict, entry: dict) -> dict:
    """按 doc_id 幂等合并：不覆盖已有的判定与精读卡。"""
    did = _norm_id(entry.get("doc_id"))
    if not did:
        return _result(False, error="doc_id 不能为空")
    idx = _index(state)
    if did in idx:
        cur = idx[did]
        for k, v in entry.items():
            if k in ("screening", "cards") and cur.get(k):
                continue
            cur[k] = v
        return _result(True, doc_id=did, created=False)
    row = dict(entry, doc_id=did)
    row.setdefault("screening", {"verdict": "unscreened", "criteria": {},
                                 "reason": "", "by": ""})
    row.setdefault("cards", [])
    row.setdefault("fulltext", {"ok": False, "chars": 0, "text": ""})
    state.setdefault("entries", []).append(row)
    return _result(True, doc_id=did, created=True)


# ── 章-文献映射：从池敲定大纲的可判定形态 ──────────────────────
def _classify(state: dict, chapter_map: dict[str, list[str]]):
    """章-文献绑定的**唯一分类口径**：写盘与只读核对共用，不许两处各写一份。"""
    idx = _index(state)
    out_of_pool, unscreened, unread, accepted = [], [], [], {}
    for ch, ids in (chapter_map or {}).items():
        good = []
        for raw in ids or []:
            did = _norm_id(raw)
            e = idx.get(did)
            if e is None:
                out_of_pool.append({"chapter": ch, "doc_id": did})
                continue
            if e.get("screening", {}).get("verdict") != "include":
                unscreened.append({"chapter": ch, "doc_id": did})
                continue
            if not e.get("cards"):
                unread.append({"chapter": ch, "doc_id": did})
                continue
            good.append(did)
        accepted[str(ch)] = good
    return accepted, out_of_pool, unscreened, unread


def assign_chapters(state: dict, chapter_map: dict[str, list[str]]) -> dict:
    """每章绑哪些 doc_id。池外 / 未纳入 / 未精读的都点名，不静默剔除。"""
    accepted, o, u, r = _classify(state, chapter_map)
    for e in state.get("entries", []):
        did = _norm_id(e.get("doc_id"))
        for ch, good in accepted.items():
            if did in good:
                e["chapter"] = ch
    return _result(not (o or u or r), chapter_map=accepted, out_of_pool=o,
                   unscreened=u, unread=r, read_only=False,
                   next="池外 / 未纳入 / 未精读的条目不得进章-文献映射；"
                        "补检索、补判定或补精读卡后重跑。")


def check_bindings(state: dict, chapter_map: dict[str, list[str]]) -> dict:
    """只读版：把「每章引用 ⊆ 已纳入且已精读的池」变成可判定反馈。

    不动 state、不改任何门；给所有权层写后简报用（advisory）。
    """
    accepted, o, u, r = _classify(state, chapter_map)
    return _result(not (o or u or r), chapter_map=accepted, out_of_pool=o,
                   unscreened=u, unread=r, read_only=True,
                   next="章-文献映射越界（advisory，不拦交付）："
                        "补检索 / 补逐篇判定 / 补精读卡后再写正文。")


def outline_view(state: dict) -> str:
    """从池生成章-文献映射**视图**；不冒充大纲正文，章号不持久化。"""
    grouped: dict[str, list[dict]] = {}
    for e in state.get("entries", []):
        ch = str(e.get("chapter") or "未分章")
        grouped.setdefault(ch, []).append(e)
    lines = ["# 章-文献映射（文献池视图，不是大纲正文）", ""]
    for i, ch in enumerate(sorted(grouped, key=lambda x: (x == "未分章", x)), 1):
        lines.append(f"## 第 {i} 节 · {ch}")
        for e in grouped[ch]:
            cards = e.get("cards") or []
            claim = cards[0].get("claim", "") if cards else "（未精读）"
            lines.append(f"- `{e.get('doc_id')}` {e.get('title', '')} "
                         f"— {claim}")
        lines.append("")
    return "\n".join(lines)


# ── 五连读回 ─────────────────────────────────────────────────
def verify(state: dict, outline_text: str = "",
           manuscript_text: str = "") -> dict:
    """登记→检索台账→判定留痕→精读卡→大纲/正文引用，缺哪环点名哪环。

    advisory：**不改 ok/scope 的放行语义、不加门**。`ok` 只表示
    「五连结构自检通过」，与「这篇综述写得好不好」无关。
    """
    gaps: list[dict] = []
    entries = state.get("entries", [])
    if not entries:
        gaps.append({"code": "POOL_EMPTY", "where": "pool",
                     "detail": "文献池为空，后面四环无从核对"})
    qs = [q["q"] for q in state.get("queries", [])]
    declared = [c["id"] for c in state.get("criteria", [])]
    idx = _index(state)

    # ① 池 ↔ 检索台账
    for e in entries:
        if str(e.get("found_by") or "") not in qs:
            gaps.append({"code": "FOUND_BY_UNKNOWN", "where": "queries",
                         "detail": f"{e.get('doc_id')} 的 found_by 不在检索台账里"})
    # ② 池 ↔ 判定留痕
    if not declared:
        gaps.append({"code": "CRITERIA_UNDECLARED", "where": "criteria",
                     "detail": "没声明纳入/排除准则，相关性判定没有基准"})
    for e in entries:
        s = e.get("screening") or {}
        if s.get("verdict") == "unscreened":
            gaps.append({"code": "UNSCREENED", "where": "screening",
                         "detail": f"{e.get('doc_id')} 未逐篇判定"})
        elif not str(s.get("reason") or "").strip():
            gaps.append({"code": "SCREEN_NO_REASON", "where": "screening",
                         "detail": f"{e.get('doc_id')} 判了但没留理由"})
        elif declared and any(c not in (s.get("criteria") or {})
                              for c in declared):
            gaps.append({"code": "CRITERIA_NOT_COVERED", "where": "screening",
                         "detail": f"{e.get('doc_id')} 判定未覆盖全部准则"})
    # ③ 池 ↔ 精读卡（只对纳入的条目要求）
    for e in entries:
        if (e.get("screening") or {}).get("verdict") != "include":
            continue
        ft = e.get("fulltext") or {}
        if not ft.get("ok"):
            gaps.append({"code": "INCLUDED_NO_FULLTEXT", "where": "fulltext",
                         "detail": f"{e.get('doc_id')} 纳入了但没有全文"})
        cards = e.get("cards") or []
        if not cards:
            gaps.append({"code": "INCLUDED_UNREAD", "where": "cards",
                         "detail": f"{e.get('doc_id')} 纳入了但没有精读卡"})
        for c in cards:
            if not (c.get("claim") and c.get("quote")) or not c.get("page"):
                gaps.append({"code": "CARD_INCOMPLETE", "where": "cards",
                             "detail": f"{e.get('doc_id')} 精读卡缺 claim/page/quote"})
    # ④ 池 ↔ 大纲（doc_id 必须在池且已纳入）
    if outline_text:
        for did in set(_norm_id(m) for m in DOCID_RE.findall(outline_text)):
            e = idx.get(did)
            if e is None:
                gaps.append({"code": "OUTLINE_OUT_OF_POOL", "where": "outline",
                             "detail": f"大纲引用了池外 {did}"})
            elif (e.get("screening") or {}).get("verdict") != "include":
                gaps.append({"code": "OUTLINE_NOT_INCLUDED", "where": "outline",
                             "detail": f"大纲引用了未纳入的 {did}"})
    # ⑤ 池 ↔ 正文引用（只认 `〔库·id p.N〕` 锚点；编号引注与裸 DOI 不认，
    #    否则「正文里提了一句 DOI」会被误判成引用孤儿）
    if manuscript_text:
        for raw in POOL_CITE_RE.findall(manuscript_text):
            did = _norm_id(raw[0] if isinstance(raw, tuple) else raw)
            if did not in idx:
                gaps.append({"code": "MANUSCRIPT_ORPHAN", "where": "manuscript",
                             "detail": f"正文引用了池外 {did}"})
    codes = sorted({g["code"] for g in gaps})
    return {"ok": not gaps, "advisory": True, "scope": "literature-pool",
            "gaps": gaps, "gap_codes": codes,
            "n_entries": len(entries), "n_included": sum(
                1 for e in entries
                if (e.get("screening") or {}).get("verdict") == "include"),
            "n_cards": sum(len(e.get("cards") or []) for e in entries),
            "next": _next(codes)}


def _next(codes: list[str]) -> str:
    if not codes:
        return "五连读回通过：检索台账 / 判定留痕 / 精读卡 / 大纲 / 正文引用同源。"
    head = {
        "POOL_EMPTY": "先用 record_query 把检索回包入池。",
        "CRITERIA_UNDECLARED": "先 declare_criteria 声明纳入/排除准则，再逐篇判定。",
        "UNSCREENED": "逐篇 screen() 留痕；工具不替你判相关性。",
        "SCREEN_NO_REASON": "每条判定补上理由。",
        "CRITERIA_NOT_COVERED": "每条判定覆盖全部已声明准则。",
        "INCLUDED_NO_FULLTEXT": "纳入条目补取全文（literature_fetch）。",
        "INCLUDED_UNREAD": "纳入条目补精读卡（claim/page/quote）。",
        "CARD_INCOMPLETE": "精读卡补齐全三件。",
        "OUTLINE_OUT_OF_POOL": "大纲里池外的 doc_id：补检索入池，或从大纲删掉。",
        "OUTLINE_NOT_INCLUDED": "大纲只引用已纳入的条目。",
        "MANUSCRIPT_ORPHAN": "正文引用补回池，或改成编号引注的老式路径。",
        "FOUND_BY_UNKNOWN": "补检索台账，别让条目来路不明。",
    }
    lines = ["⚠️ 五连读回缺口（advisory，不拦交付）："]
    for c in codes:
        lines.append(f"- {c}：{head.get(c, '按 detail 逐项修')}")
    return "\n".join(lines)


def summary(state: dict) -> dict:
    """过程读数。**不是检索质量结论**：召回率只覆盖喂进来的回包。"""
    entries = state.get("entries", [])
    qs = state.get("queries", [])
    ft = sum(1 for e in entries if (e.get("fulltext") or {}).get("ok"))
    screened = sum(1 for e in entries
                   if (e.get("screening") or {}).get("verdict") != "unscreened")
    return {
        "queries": len(qs),
        "n_returned": sum(int(q.get("n_returned") or 0) for q in qs),
        "n_duplicate": sum(int(q.get("n_dup") or 0) for q in qs),
        "entries": len(entries),
        "fulltext_ok": ft,
        "fulltext_ratio": round(ft / len(entries), 3) if entries else 0.0,
        "screened": screened,
        "included": sum(1 for e in entries
                        if (e.get("screening") or {}).get("verdict") == "include"),
        "cards": sum(len(e.get("cards") or []) for e in entries),
        "chapters": len({e.get("chapter") for e in entries if e.get("chapter")}),
        "caveat": "召回/全文率只覆盖喂进 record_query 的回包，是下界；"
                  "不回答「检索全不全、相不相关」。",
    }


def status_line(state: dict) -> str:
    s = summary(state)
    return (f"文献池 {s['entries']} 篇 / 全文 {s['fulltext_ok']} 篇 "
            f"({s['fulltext_ratio']:.0%}) / 已判定 {s['screened']} 篇 / "
            f"精读卡 {s['cards']} 张 / 检索 {s['queries']} 次"
            "（过程读数，非质量结论）")
