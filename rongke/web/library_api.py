# -*- coding: utf-8 -*-
"""文献库的两条只读接口：列表（服务端翻页/筛选/排序）与按页读取。

## 这一层**不做**什么

面板不实现任何查询逻辑（见 `server.py` 里 KB 段那段注释）。「库里有哪几篇」
「这一篇有多少页」「某一页的正文是什么」这些事实的唯一来源，是内核工具注册表
里那几个 `kb_*` 函数（`kb_index` / `kb_list` / `kb_bibtex` / `kb_read`）。本模块
**不**读 litkb 的 `library.json`、不查它的 SQLite、不自己数条目 —— 那会是第二套
实现，而两套实现早晚给出两个答案（本项目反复吃的 H 型）。所有数据都来自注入的
`fetch(tool, **args)`：服务端传 `Manager.kb_fetch`，测试传假实现。

## 这一层**做**四件事

1. **入参解析与拒绝**：`page` / `page_size` / `sort` / `order` / `source` 各有
   合法域，非法值给 400 + 能照着改的说明（而不是 500、也不是静默降级）。
   旧参数 `limit` / `status` **保持旧脾气**：坏值降级不报错 —— 见 `parse_list_params`。
2. **在服务端真正翻页**：`items` 回的是**一页**，`total` 是全量命中数。
   前端拿到的不再是「500 条里自己切」。
3. **补一件工具不给的盘上事实**：`has_pdf`（原件 PDF 还在不在），
   与 `/api/kb/stat` 的 `n_pdf/n_text_only/n_meta` 同一口径（`<库根>/pdfs/<id>.pdf`）。
4. **把每篇的完整元数据补齐**（`authors` / `abstract` / `journal` / `doi` / `tags`），
   供详情面板展示。这些字段 `kb_list` 只给一部分，`kb_bibtex` 给另一部分，
   两处都问一次再合 —— 仍然一次都不碰数据库。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterable

from .request import RequestError

# `fetch(tool, **args) -> dict`：与服务端 `Manager.kb_fetch` 同一个形状。
Fetch = Callable[..., dict]

# ══════════════════ 契约常量（前端照着这些写，报错文案也照着这些说）══════════════════
SOURCES = ("all", "pdf", "text", "meta")
SORTS = ("year", "title")
ORDERS = ("asc", "desc")

# 一页默认 40：与 `kb_index` 的默认页大小一致（那边是标定出来的，见 bridge.py）
KB_LIST_DEFAULT_PAGE_SIZE = 40
# 一页上限 500：`kb_list` 一次能取回的天花板（与 `kb_stat` 的 cap 同口径）。
# 旧参数 `?limit=500` 就是靠这条保持原样的。
KB_LIST_MAX_PAGE_SIZE = 500
# 旧参数 `limit` 不可解析时的降级值（旧实现就是 200，别改）
KB_LIST_LEGACY_LIMIT = 200
# `kb_index` 单页硬上限（它内部 clamp 到 50）与最多盘多少页。
# 20 × 50 = 1000 条：够覆盖实测的库（200 上下），又不至于为一个请求打上百次工具。
KB_INDEX_PAGE_N = 50
KB_INDEX_MAX_PAGES = 20
# 一次最多读多少页：`kb_read` 按页拼正文，跨度不设界时一个坏参数就能把
# 几百页的正文一次性拉进 JSON（几十万字符），前端解析不了、日志也读不完。
KB_READ_MAX_SPAN = 20
# 页窗口与字数窗口是两个维度：20 页论文仍可能很长。每次取一页可避免上游
# 6000 字的 cap 把剩余正文截掉；Web 仍限制一次最多 20 页，跨页正文在客户端衔接。

_INT = re.compile(r"[0-9]+\Z")
_YEAR = re.compile(r"[0-9]{4}")
_HINT_LIST = ("参数：limit（旧）/ status / page / page_size / q / sort=year|title / "
              "order=asc|desc / source=all|pdf|text|meta")

# id 里出现这些字符时**不拿它去拼盘上路径**：id 来自库文件（本该可信），
# 但"本该"不是"一定"—— 一道便宜的闸，坏了也只丢一个 has_pdf。
_BAD_ID = ("/", "\\", "..", "\x00")

# BibTeX 解析（`kb_bibtex` 的输出形态，见 litkb.export_bibtex）
_BIB_HEAD = re.compile(r"(?m)^@(\w+)\{([^,\s]+),")
_BIB_FIELD = re.compile(r"(?m)^  ([A-Za-z]+) = \{(.*)\}$")
_BIB_ABS = re.compile(r"(?m)^  abstract = \{(.*)\}$")
_BIB_END = re.compile(r"(?m)^\}$")


# ══════════════════ 入参归一 ══════════════════
def _first(qs: Any, name: str) -> str:
    """取第一个值。`qs` 既可以是 `parse_qs` 的 `{name: [v]}`，也可以是普通字典。"""
    v = qs.get(name) if hasattr(qs, "get") else None
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else ""
    return str(v)


def _int(value: Any, default: int = 0) -> int:
    """工具回包里的数：拿不到就给默认值，**绝不因此抛异常**（回包是外部输入）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_list_params(qs: Any) -> dict:
    """把 query 归一成一份参数。非法值抛 `RequestError(400, 说明)`（由 Handler 落成 400）。

    两条不同的脾气，是有意的：

    · **新参数严格**（`page`/`page_size`/`sort`/`order`/`source`）：写错了就 400，
      因为静默纠正会让调用方以为"我传的那个生效了"。
    · **旧参数宽容**（`limit`/`status`）：`?limit=abc` 必须**不断连、不 5xx**
      （`routes_test.py` 钉着这一条，`/api/runs?n=abc` 是同一个坑的历史），
      所以坏值降级成旧默认值。`limit` 的语义就是"一页取这么多"，与 `page_size` 等价。
    """
    p: dict[str, Any] = {}

    raw_size = _first(qs, "page_size").strip()
    raw_limit = _first(qs, "limit").strip()
    if raw_size:
        if not _INT.fullmatch(raw_size) or not (1 <= int(raw_size) <= KB_LIST_MAX_PAGE_SIZE):
            raise RequestError(400, f"page_size 必须是 1..{KB_LIST_MAX_PAGE_SIZE} 的整数，"
                                    f"收到 {raw_size!r}")
        size = int(raw_size)
    elif raw_limit:
        try:
            size = int(raw_limit)          # 负号/空格也照旧：旧实现就是 int() 后 clamp
        except (TypeError, ValueError):
            size = KB_LIST_LEGACY_LIMIT
        size = max(1, min(size, KB_LIST_MAX_PAGE_SIZE))
    else:
        size = KB_LIST_DEFAULT_PAGE_SIZE
    p["page_size"] = size

    raw_page = _first(qs, "page").strip()
    if raw_page:
        if not _INT.fullmatch(raw_page) or int(raw_page) < 1:
            raise RequestError(400, f"page 必须是 ≥1 的整数，收到 {raw_page!r}")
        p["page"] = int(raw_page)
    else:
        p["page"] = 1

    sort = _first(qs, "sort").strip().lower() or "year"
    if sort not in SORTS:
        raise RequestError(400, f"sort 只能是 {'/'.join(SORTS)}，收到 {sort!r}")
    p["sort"] = sort

    order = _first(qs, "order").strip().lower()
    if order and order not in ORDERS:
        raise RequestError(400, f"order 只能是 {'/'.join(ORDERS)}，收到 {order!r}")
    # 默认顺序按"人想先看什么"给：年份倒序（新的在前）、标题正序（A→Z）。
    p["order"] = order or ("desc" if sort == "year" else "asc")

    source = _first(qs, "source").strip().lower() or "all"
    if source not in SOURCES:
        raise RequestError(400, f"source 只能是 {'/'.join(SOURCES)}，收到 {source!r}")
    p["source"] = source

    # `q` 只匹配**元数据**（题名 / 年份 / id），**不是**段落检索 ——
    # 段落检索是 `/api/kb/search`（走 kb_search 的 FTS）。两者不能混：
    # 混了以后"库里没有这篇"和"这篇里没有这段"就分不开了。
    p["q"] = _first(qs, "q").strip()
    p["status"] = _first(qs, "status").strip()
    return p


# ══════════════════ 取数（一律经注册表工具）══════════════════
def _collect_index(fetch: Fetch, q: str) -> dict:
    """用 `kb_index` 一页页盘出全库清单。返回 `{rows, total, library_total, note}`。

    为什么翻页而不是一把抓：`kb_index` 的回包**恒在长度上限内**是它存在的
    全部理由（见 `bridge.py::_kb_index` 那段标定），一把 n=1000 会拿到被砍断的坏 JSON。
    """
    rows: list[dict] = []
    total = library_total = 0
    note = ""
    for pg in range(1, KB_INDEX_MAX_PAGES + 1):
        d = fetch("kb_index", query=q, page=pg, n=KB_INDEX_PAGE_N)
        if not isinstance(d, dict):
            return {"error": "回包不是字典", "raw": str(d)[:400]}
        if not d.get("ok"):
            return {"error": str(d.get("error") or "kb_index 说没有成功")}
        chunk = d.get("items") or []
        total = _int(d.get("total"), total)
        library_total = _int(d.get("library_total"), total)
        rows.extend(chunk)
        if not d.get("has_more") or not chunk:
            break
    else:
        # 盘到硬上限还没到底：**如实说只盘了一部分**，不把部分当全部。
        note = (f"库比能盘点的更多：只盘了前 {len(rows)} 条"
                f"（kb_index 自报命中 {total} 条、全库 {library_total} 条）"
                f"—— 排序/筛选/翻页只在这个窗口里成立")
    return {"rows": rows, "total": total, "library_total": library_total, "note": note}


def _collect_list(fetch: Fetch, library: str, status: str) -> dict:
    """用 `kb_list` 取候选集（兼容路：老内核没有 `kb_index`，或要按 status/库过滤）。"""
    d = fetch("kb_list", library=library, status=status, limit=KB_LIST_MAX_PAGE_SIZE)
    if not isinstance(d, dict):
        return {"fail": {"ok": False, "error": "kb_list 回包不是字典",
                         "raw": str(d)[:400]}}
    if not d.get("ok"):
        # ★ 工具失败**不冒充空库**：原样把 error/hint 交上去（旧口径也是 200 + ok:false，
        #   前端 kbListAll 就是这么读的，所以这里不动 HTTP 码，只保证不丢错因）。
        return {"fail": d}
    rows = list(d.get("items") or [])
    matched = _int(d.get("matched"), len(rows))
    library_total = _int(d.get("total"), matched)
    note = str(d.get("note") or "")
    if matched > len(rows):
        note = (f"满足条件的有 {matched} 篇，本次只取回前 {len(rows)} 篇"
                f"（kb_list 一次上限 {KB_LIST_MAX_PAGE_SIZE}）—— 翻页只能在这个窗口里进行").strip()
    return {"rows": rows, "matched": matched, "library_total": library_total,
            "root": str(d.get("library") or ""), "via": "kb_list", "note": note}


def _enrich(fetch: Fetch, library: str, got: dict) -> dict:
    """给 `kb_index` 的清单补上明细字段（状态 / 期刊 / DOI / 页码 / 标签 / 库根）。

    `kb_index` 只给 doc_id + 标题 + 年份 + 有无全文 —— 那是它**故意**的（省长度）。
    而界面那几列要 status/journal/tags，`has_pdf` 还要库根路径。所以再问一次
    `kb_list`：**以 kb_index 为准的是"有哪些篇"，以 kb_list 为准的是"每篇的字段"**。
    """
    note = str(got.get("note") or "")
    brief = fetch("kb_list", library=library, status="", limit=KB_LIST_MAX_PAGE_SIZE)
    if isinstance(brief, dict) and brief.get("ok"):
        by_id = {str(it.get("id") or ""): it for it in (brief.get("items") or [])}
        if brief.get("note"):
            note = (note + f"（{brief['note']}）").strip()
        rows = []
        for row in got.get("rows") or []:
            key = str(row.get("doc_id") or row.get("id") or "")
            merged = dict(row)
            for k, v in (by_id.get(key) or {}).items():
                merged.setdefault(k, v)      # 清单里已有的（标题/年份/全文）不覆盖
            rows.append(merged)
        got["rows"] = rows
        got["root"] = str(brief.get("library") or "")
        # `matched` 的旧语义是"只按 status 过滤后的条数"。这条路只在 status 为空时
        # 走，且 `q` 已经交给 kb_index 过滤过了（`total` 会再算一次），
        # 所以这里如实回**全库条数**——正是旧口径在"无 status"时给的那个数。
        got["matched"] = _int(got.get("library_total"), 0)
    else:
        why = (brief or {}).get("error") if isinstance(brief, dict) else "回包不是字典"
        note = (note + f"（kb_list 没给出条目明细：{why} —— 状态/期刊/标签这几列会缺，"
                       f"原件在不在也判不了）").strip()
    got["via"] = "kb_index"
    got["note"] = note
    return got


def _collect(fetch: Fetch, library: str, status: str, q: str,
             tools: set[str]) -> dict:
    """取候选集。返回 `{rows, matched, library_total, via, note[, root]}` 或 `{fail: ...}`。

    **优先 `kb_index`**（它能真翻页、上限是"库有多大"而不是"回包多长"），
    但它有两个限制，所以只在满足条件时用：
      · 它的签名里**没有** library（只读默认库）⇒ 指定了库名的请求走 `kb_list`；
      · 它不返回 status ⇒ 带 status 过滤的请求走 `kb_list`（那边原生支持）。
    """
    if not status and not library and "kb_index" in tools:
        got = _collect_index(fetch, q)
        if "rows" in got:
            return _enrich(fetch, library, got)
        # kb_index 挂了不意味着库空了：退回 kb_list，并把这件事**写进回包**。
        fallback = _collect_list(fetch, library, status)
        why = f"（kb_index 不可用：{got.get('error') or '未说明'}"
        if "fail" not in fallback:
            fallback["note"] = (str(fallback.get("note") or "") +
                                why + " —— 已退回 kb_list）").strip()
            return fallback
        # 两条路都挂了：把 kb_index 的错也补进回包 —— 只报 kb_list 那个错，
        # 排查的人会以为"换个工具就好了"，而实际上两条路都断着。
        fail = dict(fallback["fail"]) if isinstance(fallback.get("fail"), dict) else {}
        fail["note"] = (str(fail.get("note") or "") +
                        why + "；kb_list 也失败了）").strip()
        return {"fail": fail}
    return _collect_list(fetch, library, status)


# ══════════════════ 每篇的完整元数据（authors / abstract）══════════════════
def _parse_bibtex(txt: str) -> dict[str, dict]:
    """把 `kb_bibtex` 的输出解析成 `{key: {字段}}`。只认**完整**的条目。

    三处必须照它实际的形态写（都是 `litkb.export_bibtex` 的既有行为，不是发明）：

    · 条目以 `@article{litkb01,` 开头、以**独占一行**的 `}` 收尾。所以每条的字
      段区只取到它自己的收尾行为止 —— 不收在这里切，下一条前面的 `abstract`
      行就会落进上一条的字段区，摘要整体错行一位（实测过，确实会串）。
    · `abstract = {...}` 那一行印在**它自己那条的头之前** ⇒ 归属规则是
      「它之后的第一个 `@` 头」。
    · 超长时 `kb_bibtex` 会把输出砍在 20000 字符处。被砍的可能是最后一条的尾巴
      —— 所以**没有收尾行的条目一律不要**：半截字段填进去，比不填更糟。
    """
    heads = list(_BIB_HEAD.finditer(txt))
    out: dict[str, dict] = {}
    for i, m in enumerate(heads):
        stop = heads[i + 1].start() if i + 1 < len(heads) else len(txt)
        body = txt[m.end():stop]
        end = _BIB_END.search(body)
        if not end:
            continue                    # 被砍断的这一条：宁缺，不填半截
        out[m.group(2)] = {f.group(1).lower(): f.group(2)
                           for f in _BIB_FIELD.finditer(body[:end.start()])}
    starts = [h.start() for h in heads]
    for am in _BIB_ABS.finditer(txt):
        for i, pos in enumerate(starts):
            if pos > am.start():
                out.setdefault(heads[i].group(2), {})["abstract"] = am.group(1)
                break
    return out


#: 一次问多少篇的参考文献元数据。**必须分批**：`kb_bibtex` 的输出上限是
#: 20000 字符，而实测一篇的自然科学摘要就能占 2000+ 字符 —— 12 篇就到顶被砍。
#: 8 篇是留足余量的经验值（实测约 15k），超了会自动对半再分。
_KB_META_BATCH = 8


def _meta_batch(fetch: Fetch, library: str, ids: list[str],
                notes: list[str]) -> dict[str, dict]:
    """问一批 `kb_bibtex`，返回 `{litkbNN: 字段}`（只含解析完整、序号对得上的）。

    截断时说清了怎么办：`kb_bibtex` 的 `bibtex` 字段硬上限 20000 字符，
    所以一次问太多篇必然砍尾。这里的对策是**对半再分**，而不是把砍掉的那几篇
    填成空字段 —— 填空的后果是"这篇在详情里看着没作者"，而真相是"没问全"。
    """
    d = fetch("kb_bibtex", library=library, ids=",".join(ids))
    if not isinstance(d, dict):
        notes.append("kb_bibtex 回包不是字典")
        return {}
    if not d.get("ok"):
        notes.append(str(d.get("error") or "kb_bibtex 说没有成功"))
        return {}
    if d.get("missing"):
        # 认不出的 id 会被跳过 ⇒ 序号整体错位 ⇒ **整批不填**（填错行比空着糟得多）。
        notes.append("kb_bibtex 认不出 " + ", ".join(str(x) for x in d["missing"])[:120])
        return {}
    if _int(d.get("n"), 0) != len(ids):
        notes.append(f"请求 {len(ids)} 篇、导出 {d.get('n')} 条，条数对不上")
        return {}
    parsed = _parse_bibtex(str(d.get("bibtex") or ""))
    keys = [f"litkb{i + 1:02d}" for i in range(len(ids))]
    got = {k: parsed[k] for k in keys if k in parsed}
    if len(got) == len(keys):
        return got
    if len(ids) == 1:
        notes.append("kb_bibtex 的输出解析不出这一个条目")
        return {}
    half = len(ids) // 2
    left = _meta_batch(fetch, library, ids[:half], notes)
    right = _meta_batch(fetch, library, ids[half:], notes)
    # 右半的序号又是从 litkb01 起的 ⇒ 平移回来，才与调用方的下标对齐。
    right = {f"litkb{_int(k[5:]) + half:02d}": v for k, v in right.items()}
    return {**left, **right}


def _enrich_meta(fetch: Fetch, library: str, items: list[dict]) -> str:
    """给**这一页**的条目补 `authors` / `abstract`（工具没给的字段才补，绝不覆盖）。

    返回一句"没补成/只补了一半"的说明（全补齐就回空串）。补不齐**不算错误** ——
    这些字段是详情展示用的附加信息，缺了只是少显示一块，不该让整页列表失败。
    """
    ids = [str(it.get("id") or "") for it in items if str(it.get("id") or "")]
    if not ids:
        return ""
    by_id = {str(it.get("id") or ""): it for it in items}
    notes: list[str] = []
    filled = 0
    for start in range(0, len(ids), _KB_META_BATCH):
        batch = ids[start:start + _KB_META_BATCH]
        got = _meta_batch(fetch, library, batch, notes)
        for i, iid in enumerate(batch):
            f = got.get(f"litkb{i + 1:02d}") or {}
            it = by_id.get(iid)
            if not it:
                continue
            au = [a.strip() for a in str(f.get("author") or "").split(" and ") if a.strip()]
            if au and not it.get("authors"):
                it["authors"] = au
                filled += 1
            ab = str(f.get("abstract") or "").strip()
            if ab and not it.get("abstract"):
                it["abstract"] = ab
    if filled or not notes:
        return ""
    return "authors/abstract 没补上：" + "；".join(notes[:2])


# ══════════════════ 过滤 / 排序 / 投影 ══════════════════
def _has_pdf(root: str, doc_id: str, row: dict | None = None) -> bool:
    """原件 PDF 在不在。

    两个来源都算：litkb 的约定落点 `<库根>/pdfs/<id>.pdf`，以及条目自己记的
    `local_path`（手工上传/从别的目录入库的 PDF 就在原处，litkb 只记路径、
    不复制文件）。只看第一个会让**本地 PDF 全部显示「只有文本」**，
    「看原页」按钮也跟着消失 —— 2026-10-29 实测就是这个。
    """
    if not doc_id:
        return False
    if any(bad in doc_id for bad in _BAD_ID):
        return False
    cands: list[Path] = []
    if root:
        cands.append(Path(root) / "pdfs" / (doc_id + ".pdf"))
    for key in ("local_path", "path", "pdf_path"):
        v = (row or {}).get(key)
        if v:
            try:
                cands.append(Path(str(v)))
            except OSError:
                pass
    for p in cands:
        try:
            if p.is_file():
                return True
        except OSError:
            continue
    return False


def _item(row: dict, root: str) -> dict:
    """一条对外的条目。**不编造任何字段**：工具给了什么就带什么。

    工具没给的（比如某个内核版本的 `kb_list` 不带 abstract）就**没有这个键** ——
    补一个空串会让人以为"这篇没作者"，而真相是"这一层拿不到"。
    """
    did = str(row.get("id") or row.get("doc_id") or "")
    it = {k: v for k, v in row.items() if k != "doc_id"}
    it["id"] = did
    it["title"] = str(row.get("title") or "")
    it["year"] = str(row.get("year") or "")
    it["status"] = str(row.get("status") or "unread")
    it["fulltext"] = bool(row.get("fulltext"))
    it["has_pdf"] = _has_pdf(root, did, row)
    return it


def _match_q(it: dict, q: str) -> bool:
    """`q` 的匹配面：题名 / 年份 / id 三个元数据字段（与 `kb_index` 的 query 同口径）。

    **不碰正文**：正文检索是 `kb_search` 的活。把两者混起来，用户就分不清
    "库里没有这篇"和"这篇里没有这段"——而那是两个完全不同的结论。
    """
    if not q:
        return True
    hay = f"{it.get('title') or ''} {it.get('year') or ''} {it.get('id') or ''}"
    return q.lower() in hay.lower()


def _source_of(it: dict) -> str:
    """三档盘上事实：有原文 / 只有文本 / 仅元数据（与 `kb_stat` 同一口径）。"""
    if it.get("has_pdf"):
        return "pdf"
    return "text" if it.get("fulltext") else "meta"


def _year_key(it: dict) -> int:
    y = str(it.get("year") or "")
    m = _YEAR.search(y)
    return int(m.group(0)) if m else -1      # 没年份的排最后（倒序时）


def _title_key(it: dict) -> str:
    return str(it.get("title") or "").lower()


def _sort_rows(rows: list[dict], sort: str, order: str) -> None:
    """稳定排序：先按 id 兜底，再按主键。

    两趟而不是一趟复合键，是因为降序时复合键会把 id 也一起倒过来 ——
    那样"同一年份内的顺序"就取决于 id 的倒序，翻页时同一篇可能在两页都出现
    （或哪页都不出现）。先 id 正序、再按主键稳定排，同键的次序就恒等于 id 序。
    """
    rows.sort(key=lambda it: str(it.get("id") or ""))
    rows.sort(key=_year_key if sort == "year" else _title_key,
              reverse=(order == "desc"))


# ══════════════════ 对外入口：列表 ══════════════════
def list_payload(fetch: Fetch, *, qs: Any = None, library: str = "",
                 tools: Iterable[str] = (), library_root: Callable[[], str] = str
                 ) -> tuple[int, dict]:
    """`GET /api/kb/list` 的全部逻辑。返回 `(HTTP 码, 载荷)` —— Handler 只负责发出去。

    载荷的键（前端按这些写）：

        ok, library, total, matched, showing, fulltext, items,
        page, page_size, has_more, library_total, via[, note][, meta_note]

    · `total`     满足**全部**条件（status + q + source）的条数 —— 拿它算页数
    · `matched`   只按 status 过滤后的条数（旧口径，`kb_list.matched` 的语义，前端在用）
    · `showing`   本页条数 = `len(items)`
    · `has_more`  还有下一页吗（`(page-1)*page_size + showing < total`）
    · `fulltext`  命中集里"有正文"的条数（不是本页的）

    条目上的键：`id, title, year, status, fulltext, has_pdf` +
    `journal, doi, pages, tags`（工具给了就有）+ `authors, abstract`（补到了就有）。
    """
    try:
        p = parse_list_params(qs or {})
    except RequestError as e:
        return e.status, {"ok": False, "error": e.detail, "hint": _HINT_LIST}

    got = _collect(fetch, library, p["status"], p["q"], set(tools or ()))
    if "fail" in got:
        return 200, got["fail"]

    # 库根以工具自报的为准（与 kb_stat 同一口径）；工具不报（kb_index）才回退到
    # Manager 的"库名 → 库根"换算 —— 那条换算只有一处，绝不在这里重写。
    root = str(got.get("root") or "").strip() or str(library_root() or "").strip()

    rows = [_item(r, root) for r in (got.get("rows") or [])]
    rows = [it for it in rows if _match_q(it, p["q"])]
    if p["source"] != "all":
        rows = [it for it in rows if _source_of(it) == p["source"]]
    total = len(rows)
    _sort_rows(rows, p["sort"], p["order"])

    start = (p["page"] - 1) * p["page_size"]
    page_rows = rows[start:start + p["page_size"]]
    meta_note = _enrich_meta(fetch, library, page_rows)

    body: dict = {
        "ok": True,
        "library": root,
        "total": total,
        "matched": _int(got.get("matched"), total),
        "showing": len(page_rows),
        "fulltext": sum(1 for it in rows if it["fulltext"]),
        "items": page_rows,
        "page": p["page"],
        "page_size": p["page_size"],
        "has_more": start + len(page_rows) < total,
        "library_total": _int(got.get("library_total"), total),
        "via": str(got.get("via") or "kb_list"),
    }
    note = str(got.get("note") or "")
    if not root and rows:
        note = (note + "（库根没拿到 ⇒ has_pdf 一律按 false 给）").strip()
    if note:
        body["note"] = note
    if meta_note:
        body["meta_note"] = meta_note
    return 200, body


# ══════════════════ 对外入口：按页读取 ══════════════════
def _opt_page(raw: Any, name: str) -> tuple[int, bool]:
    """解析页码。返回 `(值, 调用方是否显式给了)`；非法抛 400。

    空 / `0` = "不限"（`kb_read` 自己的语义：from 从开头、to 到结尾）。
    """
    s = str(raw or "").strip()
    if not s:
        return 0, False
    if not _INT.fullmatch(s):
        raise RequestError(400, f"{name} 必须是非负整数（页从 1 起；0 或留空 = 不限），"
                                f"收到 {s!r}")
    return int(s), True


def read_payload(fetch: Fetch, *, doc_id: Any = "", page_from: Any = "",
                 page_to: Any = "", library: str = "",
                 tools: Iterable[str] = ()) -> tuple[int, dict]:
    """`GET /api/kb/read` 的全部逻辑。返回 `(HTTP 码, 载荷)` —— Handler 只负责发出去。

    载荷的键（前端按这些写）：

        ok, doc_id, title, library, pages_total, page_from, page_to,
        page_range, truncated, chars, char_total, text, has_more_pages

    · `page_from` / `page_to` 是**实际转发下去的那个窗口**，不是原样回显入参 ——
      只给 `page_from` 时窗口是 `[page_from, page_from+19]`，只给 `page_to` 时是
      `[1, page_to]`，都没给时是 `[1, 20]`。回显入参会让前端以为读了 5..末页。
    · `page_range` 是工具实际交回的页码范围（当 `pages_total` 比窗口小，二者会不同）。
    · `truncated` 是**字符**层面的截断（工具按 `max_chars` 砍的）。
    · `has_more_pages` 是**页**层面的：这篇后面还有没读到的页。

    为什么窗口必须真的收窄、而不是只在入参校验时算一算：`kb_read` 的实现是
    `hi = page_to or len(pages)`，`page_to` 留空 = 读到**末页**。所以"只给
    `page_from=5`"若把 `page_to=0` 原样传下去，实际会读完 5..N 页 —— 校验说
    "跨度 20 页以内"、实际读了两百页，两边说的不是一件事。这里把窗口算出来
    再传，并且把算出来的窗口如实报回去。

    错误口径（**工具失败绝不冒充空文档**）：

    ======  ==========================================================
    400     空 doc_id / 页码不是非负整数 / from > to / 跨度超 20 页 /
            页码超出这篇的总页数（越界）
    404     `kb_read` 认不出这个 id（回包里没带 doc_id）
    409     这篇在库里，但**没有全文缓存**（`kb_read` 回包里带着 doc_id）
    502     工具没能给出正文（回包不成形 / `ok:true` 却正文为空）
    503     注册表里根本没有 `kb_read`（接线断了，不是这篇没正文）
    ======  ==========================================================
    """
    did = str(doc_id or "").strip()
    if not did:
        return 400, {"ok": False, "error": "doc_id 不能为空",
                     "hint": "先用 GET /api/kb/list 拿到条目 id（形如 kb635b861b1）"}
    try:
        pf, has_pf = _opt_page(page_from, "page_from")
        pt, has_pt = _opt_page(page_to, "page_to")
        if has_pf and has_pt and pf > pt:
            raise RequestError(400, f"page_from({pf}) 不能大于 page_to({pt})")
        # 有效窗口：缺哪头补哪头，跨度一律 ≤ KB_READ_MAX_SPAN。
        # 只给 page_from ⇒ 往后 20 页；只给 page_to ⇒ 从头到 page_to（>20 页就拒）；
        # 都不给 ⇒ 从第 1 页起 20 页（**不是**"读到末页"）。
        ef = pf or 1
        et = pt or (ef + KB_READ_MAX_SPAN - 1)
        if et - ef + 1 > KB_READ_MAX_SPAN:
            raise RequestError(400, f"一次最多读 {KB_READ_MAX_SPAN} 页（请求 {ef}..{et}，"
                                    f"{et - ef + 1} 页）—— 分段读")
    except RequestError as e:
        return e.status, {"ok": False, "error": e.detail,
                          "hint": f"page_from / page_to 为 1 起的页码，单次跨度 ≤"
                                  f"{KB_READ_MAX_SPAN} 页；留空 = 从第 1 页起读 20 页"}

    if "kb_read" not in set(tools or ()):
        return 503, {"ok": False, "error": "荣珂看不到 kb_read",
                     "hint": "workbench 未挂载，或 kb 域被裁剪 —— 这是接线断了，"
                             "不是这篇没有正文"}

    # ★ 一页一次：上游 kb_read 的 max_chars 是对整个页窗生效的；20 页一次会把
    # 长文截在 6000 字且无法续读。Web 的 20 页窗口因此是**导航窗口**，不是一次
    # 工具调用的正文拼接窗口。
    chunks = []
    d = None
    for page in range(ef, et + 1):
        d = fetch("kb_read", library=library, doc_id=did, page_from=page, page_to=page,
                  max_chars=250000)
        if not isinstance(d, dict) or d.get("ok"):
            got_max = _int(d.get("page_max"), _int(d.get("pages_total"))) if isinstance(d, dict) else 0
            if got_max and page > got_max:
                return 400, {"ok": False, "doc_id": did, "pages_total": _int(d.get("pages_total")),
                             "page_max": got_max, "error": f"页码越界：最大页号 {got_max}"}
        if not isinstance(d, dict):
            return 502, {"ok": False, "doc_id": did, "error": "kb_read 回包不是字典",
                         "raw": str(d)[:400]}
        if d.get("ok") and (len(d.get("page_range") or []) != 2
                            or d["page_range"] != [page, page]):
            return 502, {"ok": False, "doc_id": did,
                         "error": "kb_read 回包页码与请求不符，拒绝把它拼进正文"}
        if not d.get("ok"):
            if d.get("empty_page"):
                continue
            err = str(d.get("error") or "kb_read 没能读出正文")
            body = {"ok": False, "doc_id": did, "error": err}
            if d.get("hint"):
                body["hint"] = str(d["hint"])
            return (409 if d.get("doc_id") else 404), body
        if d.get("truncated"):
            return 502, {"ok": False, "doc_id": did,
                         "error": "单页正文超过安全读取上限 250000 字；没有把它截断后冒充全文",
                         "hint": "请重新抽取或分段处理这一页"}
        chunks.append(d)
        if not str(d.get("text") or ""):
            break                         # 空正文按上游异常处理，不再读余下 19 页
    if not chunks:
        return 200, {"ok": True, "doc_id": did, "pages_total": _int(d.get("pages_total")),
                     "page_max": _int(d.get("page_max"), _int(d.get("pages_total"))),
                     "page_from": ef, "page_to": et, "page_range": [], "text": "",
                     "chars": 0, "empty_window": True, "truncated": False,
                     "has_more_pages": False,
                     "hint": "这一窗没有抽出的正文；换页读或重新抽取 PDF"}
    d = dict(chunks[0])
    d["text"] = "\n\n".join(str(c.get("text") or "") for c in chunks)
    d["chars"] = len(d["text"])
    d["truncated"] = any(c.get("truncated") for c in chunks)
    d["char_total"] = _int(chunks[-1].get("char_total"), _int(d.get("char_total")))
    d["page_max"] = _int(chunks[-1].get("page_max"), _int(d.get("pages_total")))
    d["page_range"] = [int(chunks[0]["page_range"][0]), int(chunks[-1]["page_range"][-1])]
    if not isinstance(d, dict):
        return 502, {"ok": False, "doc_id": did, "error": "kb_read 回包不是字典",
                     "raw": str(d)[:400]}
    if not d.get("ok"):
        err = str(d.get("error") or "kb_read 没能读出正文")
        body = {"ok": False, "doc_id": did, "error": err}
        if d.get("hint"):
            body["hint"] = str(d["hint"])
        # 回包里带 doc_id = 工具认出了这一篇（只是没有正文）⇒ 409；
        # 不带 = 这个 id 在库里根本找不到 ⇒ 404。两种都不是"空文档"。
        return (409 if d.get("doc_id") else 404), body

    pages_total = _int(d.get("pages_total"))
    # 越界判定放在**调用之后**：总页数只有工具知道（本模块不去数库），
    # 而 `kb_read` 自己的越界行为是"退回全部页"—— 那会把一次错参数
    # 渲染成"这篇只有这些内容"，所以这里必须自己拦一次。
    # ⚠️ 只拿**调用方显式给**的那头判越界：另一头是我们自己补的，
    #    它超出去是正常的（那只是"读不满这么多页"），不该因此报错。
    page_max = _int(d.get("page_max"), pages_total)
    if page_max and ((has_pf and pf > page_max) or (has_pt and pt > page_max)):
        return 400, {"ok": False, "doc_id": did, "pages_total": pages_total,
                     "page_max": page_max,
                     "error": f"页码越界：这篇有 {pages_total} 页正文，最大页号 {page_max}",
                     "hint": f"page_from / page_to 是 1 起的页码，最大到 {page_max}"}

    text = str(d.get("text") or "")
    if d.get("truncated"):
        return 502, {"ok": False, "doc_id": did, "pages_total": pages_total,
                     "error": "单页正文超过安全读取上限 250000 字；没有把它截断后冒充全文",
                     "hint": "请重新抽取或分段处理这一页"}
    if not text:
        return 502, {"ok": False, "doc_id": did, "pages_total": pages_total,
                     "error": "kb_read 说读成功了，但正文是空的 —— 不把它当空文档",
                     "hint": "这多半是上游抽文本时出了问题；重新入库或换一篇试"}

    rng = [int(x) for x in (d.get("page_range") or [])
           if str(x).strip().isdigit()]
    # 实际窗口收窄到总页数为止（我们自己补的那头可能超出去）。
    if pages_total:
        et = min(et, pages_total)
    return 200, {
        "ok": True,
        "doc_id": str(d.get("doc_id") or did),
        "title": str(d.get("title") or ""),
        "library": str(d.get("library") or ""),
        "pages_total": pages_total,
        "page_max": page_max,
        "page_from": ef,
        "page_to": et,
        "page_range": rng,
        # 页层面还有没有没读到的：拿工具实际交回的末页比总页数，不靠推算。
        "has_more_pages": bool(page_max and et < page_max),
        "truncated": bool(d.get("truncated")),
        "chars": _int(d.get("chars"), len(text)),
        "char_total": _int(d.get("char_total")),
        "text": text,
    }
