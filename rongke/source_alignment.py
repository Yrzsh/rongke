# -*- coding: utf-8 -*-
"""引文—原文对齐：把「这条引注有没有出处」从事后审计变成内核可实测读回。

═══════════════════════════════════════════════════════════════════════
为什么要收编 `_realp/audit_live.py`
═══════════════════════════════════════════════════════════════════════
11-04 真人实测里那句「4 处〔库·doc p.N〕锚点全部落在本次 `kb_read` 覆盖页」，
来自一份**一次性探针**：它只把锚点里的 `doc_id` 与 `kb_read` 的参数比对，
算出「读 / 检 / 忆」三档，**从来没有把稿件里那句话与库内原文那句话对起来**。
探针不在内核、不在常驻判据、跑完归档 —— 于是「没编结论」这件事
实际上仍是「这次恰好没踩」，不是「结构上踩不了」。

本模块把这件事做成内核单源，并补上第三级：

  L1 来源  `read` / `searched` / `unrecorded` —— 本次调用台账有没有读过它
  L2 页本  `page` / `doc` / `missing` / `unknown` —— 拿不拿得到这一页的文字
  L3 支撑  `aligned` / `unsupported` / `unknown` —— 稿件句能否在原文里找到支撑句

═══════════════════════════════════════════════════════════════════════
三条不许越过的线
═══════════════════════════════════════════════════════════════════════
1. **三态**。`unknown` 绝不等于 `unsupported`：没给原文、原文太短、
   文献表没标 `doc_id`，一律判「判不了」，不判「你编了」。
   项目铁律：读不到 ≠ 不符合。
2. **对齐 ≠ 结论正确**。这里只证明「有文本可支撑」，不判科学结论对错、
   不判转述是否过度、不判引用是否恰当。输出里必须原样写给人看。
3. **不阻断**。这一层只把事实送进过程反馈，不进硬门、不增加打回。
   门更严 = 多打回 = 更费力，与「一次写对 + 省力」的目标函数相反。

正文引注口径与 `writing.py` 同源（编号展开直接用 `_expand_cite_group`），
不另造一套正则语义。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from . import writing as W

# 〔库·kbXXXX p.N〕：带页码锚点，实测 A / E2 用这个形态。
# ★ 真实产物里页码是范围（`p.4–5`）；非捕获组吃掉后缀，group(2) 仍是起始页。
ANCHOR_RE = re.compile(
    r"〔\s*库[·・]\s*(kb[0-9a-fA-F]+)\s*p\.?\s*(\d+)"
    r"(?:\s*[-–—~～至]\s*\d+)?\s*〕")
# 文献表里的 〔库·kbXXXX〕：D2 实测不带页码
DOC_RE = re.compile(r"〔\s*库[·・]\s*(kb[0-9a-fA-F]+)\s*〕")
_ANY_DOC = re.compile(r"\bkb[0-9a-fA-F]{4,}\b")
# `writing.py` 的 `_CITE_NUM` 只认半角 `[n]`；预设可把括号对换成 `〔n〕`。
# 这里只多认一种括号形状，展平仍走 `_expand_cite_group` —— 口径单源。
_CITE_ANY = re.compile(
    r"[\[［〔]\s*(\d{1,3}(?:\s*[-,，～~至]\s*\d{1,3})*)\s*[\]］〕]")
_SENT_SPLIT = re.compile(r"[。！？；!?;\n]+")
_NUM_TOK = re.compile(r"\d+(?:\.\d+)?")
_WORD_TOK = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")

TITLE = "【引文溯源读回 · 内核实测】"
ALIGNED, UNSUPPORTED, UNKNOWN = "aligned", "unsupported", "unknown"

#: 原文短于此不判 unsupported —— 拿三行字当「全文」就断言人家没支撑，是误伤。
MIN_SOURCE_CHARS = 40
#: 稿件句短于此、或事实 token 少于此，转述再自由也判不了 ⇒ unknown 而非 unsupported。
#: 实测事故：「结果表明融合率为 12.4%[1]」明明有出处，却因句子太短被判没支撑。
#: 误报会教模型无视整份读回，比漏报更伤（writing.py 设计定律 10）。
MIN_CLAIM_CHARS = 24
MIN_CLAIM_TOKENS = 2
#: 默认相似度阈值。标定方式见 `source_alignment_test.py` 的正负控。
DEFAULT_MIN_SCORE = 0.55
#: 一次最多核对多少条引注，防止超长稿把回包撑爆。
MAX_ITEMS = 40


def _norm(s: str) -> str:
    """去标点空白、小写；`\\W` 对 str 是 Unicode 感知的，中文不会被抹掉。"""
    return re.sub(r"[\W_]+", "", (s or "").lower())


def _grams(s: str, n: int = 3) -> set[str]:
    t = _norm(s)
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _strong(s: str) -> set[str]:
    """事实 token：数值 + 拉丁词。中文停用词太多，不拿汉字凑覆盖率。"""
    return set(_NUM_TOK.findall(s or "")) | {
        w.lower() for w in _WORD_TOK.findall(s or "")}


def _similarity(claim: str, src: str) -> float:
    """确定性相似度：字符 3-gram Jaccard 与事实 token 覆盖率加权。"""
    a, b = _grams(claim), _grams(src)
    jac = len(a & b) / len(a | b) if a and b else 0.0
    sa = _strong(claim)
    cov = (len(sa & _strong(src)) / len(sa)) if sa else 0.0
    return round(0.6 * jac + 0.4 * cov, 3) if sa else round(jac, 3)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "")
            if len(s.strip()) >= 6]


def _claim_at(text: str, start: int, end: int) -> str:
    """引注所在句子：向左右扩到最近的分句边界，最多 220 字。"""
    left = max((text.rfind(c, max(0, start - 220), start)
                for c in "。！？；!?;\n"), default=-1)
    right_candidates = [p for c in "。！？；!?;\n"
                        if (p := text.find(c, end, min(len(text), end + 220))) >= 0]
    right = min(right_candidates) if right_candidates else min(len(text), end + 220)
    s = re.sub(r"\s+", " ", text[left + 1:right]).strip()
    # ★ 引注标记本身不是「论断内容」。不剥掉的话，`〔库·kbXXXX p.2〕`
    #   会把字符 3-gram 稀释掉：实测一句 0.46 的转述被压到 0.152，
    #   于是短稿被误判成「没支撑」。比较只比人写的那句话。
    s = ANCHOR_RE.sub("", s)
    s = _CITE_ANY.sub("", s)
    return s.strip(" ，,;；")


def _coerce_sources(sources: Any) -> dict[str, dict[int | None, str]]:
    """接受三种形状，都是为了「模型把 kb_read 回包直接贴进来就能用」。

    · `{doc_id: "全文"}`                → {doc: {None: 全文}}
    · `{doc_id: {"2": "第2页", ...}}`    → {doc: {2: ...}}
    · `[{"doc_id", "page_no", "text"}]` → 累加到对应页（kb_search 的 hits 形状）
    """
    out: dict[str, dict[int | None, str]] = {}
    if not sources:
        return out
    if isinstance(sources, str):
        # 工具入参里模型常直接贴 kb_read 的回包文本；解析失败就如实不给原文。
        try:
            sources = json.loads(sources)
        except (ValueError, TypeError):
            return out

    def put(doc: str, page: int | None, txt: str) -> None:
        doc = (doc or "").strip().lower()
        if not doc or not isinstance(txt, str) or not txt.strip():
            return
        slot = out.setdefault(doc, {})
        slot[page] = (slot.get(page, "") + "\n" + txt).strip()

    if isinstance(sources, dict):
        for doc, val in sources.items():
            if isinstance(val, str):
                put(str(doc), None, val)
            elif isinstance(val, dict):
                for p, t in val.items():
                    try:
                        pg = int(str(p).lstrip("pP"))
                    except (TypeError, ValueError):
                        pg = None
                    put(str(doc), pg, str(t) if t is not None else "")
    elif isinstance(sources, Iterable):
        for row in sources:
            if not isinstance(row, dict):
                continue
            doc = str(row.get("doc_id") or row.get("id") or "")
            p = row.get("page_no", row.get("page"))
            try:
                pg = int(p) if p not in (None, "") else None
            except (TypeError, ValueError):
                pg = None
            put(doc, pg, str(row.get("text") or row.get("snippet") or ""))
    return out


def _page_text(src: dict[str, dict[int | None, str]], doc: str,
               page: int | None) -> tuple[str, str]:
    """→ (原文, L2 状态)。整篇可用的话照样给内容，但页码要如实标未核。"""
    if not src or doc not in src:
        return "", UNKNOWN if not src else "missing"
    pages = src[doc]
    if page is not None and page in pages:
        return pages[page], "page"
    if page is not None and None in pages:
        return pages[None], "doc"
    if page is None and pages:
        return "\n".join(pages.values()), "doc"
    if page is not None and pages:
        # 有分页但没这一页 ⇒ 明确 missing，别拿别的页冒充
        return "", "missing"
    return "", UNKNOWN


# 全角 `〔1〕` / `［1］` 条目起头。11-04 的真实文献表用的就是 `〔1〕`，
# 而 `writing._REF_ENTRY` 只认 `[1]` / `1.` / `- ` / APA —— 不补这一条，
# 真实稿件里 4 篇文献一条都映射不上（`_realp/out/A_artifacts/参考文献.md`）。
_FULL_NUM_ENTRY = re.compile(r"^[〔［\[]\s*(\d{1,3})\s*[〕］\]]")


def _ref_entries(text: str) -> tuple[dict[int, dict], list[tuple[int, int]]]:
    """→ (`编号 → {doc, page}`, 文献表条目行的字符区间)。

    ★ 两个真实形状，缺一不可：
      · 编号起头可能是 `〔1〕` 而非 `[1]`；
      · `doc_id` 常写在**条目的下一行**（「用于：…〔库·kbXXXX p.4–5〕」），
        所以映射必须按「条目 + 后续行」扫描，不能只看单行。
    另返回条目行 offset：`_refs_offset()` 在多文件拼接产物里可能找不到
    文献表标题，此时正文与著录行会混在一起，靠这份集合把著录行排掉。
    """
    out: dict[int, dict] = {}
    entry_spans: list[tuple[int, int]] = []
    cur: int | None = None
    off = 0
    for line in text.split("\n"):
        s = line.strip()
        m = _FULL_NUM_ENTRY.match(s) or W._REF_ENTRY.match(s)
        if m:
            num = m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
            if num and str(num).isdigit():
                cur = int(num)
                out[cur] = {"doc": "", "page": None}
                entry_spans.append((off, off + len(line)))
        if cur is not None:
            row = out[cur]
            a = ANCHOR_RE.search(line)
            if a and not row["doc"]:
                row["doc"] = a.group(1).lower()
                try:
                    row["page"] = int(a.group(2))
                except (TypeError, ValueError):
                    pass
            elif not row["doc"]:
                d = DOC_RE.search(line)
                doc = d.group(1) if d else ""
                if not doc:
                    d2 = _ANY_DOC.search(line)
                    doc = d2.group(0) if d2 else ""
                if doc:
                    row["doc"] = doc.lower()
        off += len(line) + 1
    return out, entry_spans


def _specs(text: str) -> list[dict]:
    """把稿件里的引注抽成待核对项。带页码锚点优先；编号制靠文献表映射。"""
    off = W._refs_offset(text)
    body = text[:off] if off > 0 else text
    rmap, entry_spans = _ref_entries(text)
    specs: list[dict] = []
    for m in ANCHOR_RE.finditer(text):
        specs.append({"pos": m.start(), "end": m.end(), "label": m.group(0),
                      "doc": m.group(1).lower(), "page": int(m.group(2))})
    # 编号制：映射不到也列出来判 unknown。著录行本身不是正文引注 ——
    # 多文件拼接时 `_refs_offset` 可能失效，靠条目行 offset 把它们排掉。
    for m in _CITE_ANY.finditer(body):
        if any(a <= m.start() < b for a, b in entry_spans):
            continue
        for n in W._expand_cite_group(m.group(1)):
            if any(s["pos"] == m.start() for s in specs):
                continue
            info = rmap.get(n) or {}
            specs.append({"pos": m.start(), "end": m.end(),
                          "label": m.group(0),
                          "doc": info.get("doc", ""),
                          "page": info.get("page"), "num": n})
    specs.sort(key=lambda s: s["pos"])
    return specs[:MAX_ITEMS]


def sources_from_calls(calls: Any) -> dict:
    """从工具台账抽 L1 与原文 —— 给「写完自动在读回回包里附一份」留的接缝。

    只读 `ok=True` 的调用；`result` 可能是被截断的字符串，解析不出来就
    如实不给原文（unknown），绝不拿半截 JSON 猜。
    """
    read: set[str] = set()
    searched: set[str] = set()
    rows: list[dict] = []
    for c in calls or []:
        if isinstance(c, dict):
            name, args, ok = c.get("name", ""), c.get("args") or {}, c.get("ok", True)
            result = c.get("result", "") or ""
        else:
            name = getattr(c, "name", "") or ""
            args = getattr(c, "args", None) or {}
            ok = bool(getattr(c, "ok", True))
            result = getattr(c, "result", "") or ""
        if not ok:
            continue
        did = str(args.get("doc_id") or "").strip().lower()
        if name == "kb_read" and did:
            read.add(did)
        if name == "kb_read" or "kb" in name:
            try:
                data = json.loads(result) if isinstance(result, str) else result
            except (ValueError, TypeError):
                data = None
            hits = (data or {}).get("hits", []) if isinstance(data, dict) else []
            for h in hits:
                if not isinstance(h, dict):
                    continue
                hd = str(h.get("doc_id") or "").strip().lower()
                if not hd:
                    continue
                if name == "kb_read":
                    read.add(hd)
                else:
                    searched.add(hd)
                rows.append({"doc_id": hd,
                             "page_no": h.get("page_no", h.get("page")),
                             "text": h.get("text") or h.get("snippet") or ""})
    return {"read_docs": read, "searched_docs": searched - read, "sources": rows}


def align(text: str, *, sources: Any = None,
          read_docs: Iterable[str] | None = None,
          searched_docs: Iterable[str] | None = None,
          min_score: float = DEFAULT_MIN_SCORE) -> dict[str, Any]:
    """对齐主入口。返回 `{items, summary, text, has_alignment}`。

    `sources` 为空 ⇒ 只做 L1/L2，L3 一律 `unknown`（不误判）。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    src = _coerce_sources(sources)
    rd = {str(d).lower() for d in (read_docs or [])}
    sd = {str(d).lower() for d in (searched_docs or [])}
    specs = _specs(text)

    items: list[dict] = []
    for s in specs:
        doc, page = s.get("doc", ""), s.get("page")
        claim = _claim_at(text, s["pos"], s["end"])
        origin = ("read" if doc and doc in rd else
                  "searched" if doc and doc in sd else "unrecorded")
        raw, lvl = _page_text(src, doc, page) if doc else ("", UNKNOWN)
        shallow = (len(_norm(claim)) < MIN_CLAIM_CHARS
                   or len(_strong(claim)) < MIN_CLAIM_TOKENS)
        if not raw or len(_norm(raw)) < MIN_SOURCE_CHARS:
            state, best, score = UNKNOWN, "", 0.0
        else:
            cands = _sentences(raw) or ([raw] if raw.strip() else [])
            scored = [(_similarity(claim, c), c) for c in cands]
            score, best = max(scored, key=lambda x: x[0]) if scored else (0.0, "")
            if score >= min_score:
                state = ALIGNED
            elif shallow:
                state, best = UNKNOWN, ""     # 判不了 ≠ 不符合
            else:
                state = UNSUPPORTED
        items.append({
            "label": s["label"], "doc": doc, "page": page,
            "claim": claim[:200], "origin": origin, "page_text": lvl,
            "state": state, "score": score, "shallow": shallow,
            "support": best[:160],
        })

    def count(k, v):
        return len([i for i in items if i[k] == v])

    summary = {
        "total": len(items),
        "aligned": count("state", ALIGNED),
        "unsupported": count("state", UNSUPPORTED),
        "unknown": count("state", UNKNOWN),
        "by_origin": {k: count("origin", k)
                      for k in ("read", "searched", "unrecorded")},
        "by_page_text": {k: count("page_text", k)
                         for k in ("page", "doc", "missing", "unknown")},
        "sources_given": bool(src),
    }

    lines = [TITLE]
    if not items:
        lines.append("稿件里没有可核对的引注锚点（既无 〔库·kb… p.N〕，"
                     "也无带 doc_id 的编号文献表）。这不等于「没有引文」，"
                     "只是内核拿不到出处，判不了。")
    else:
        lines.append(f"共 {len(items)} 条引注：找到原文支撑 {summary['aligned']}，"
                     f"未找到 {summary['unsupported']}，判不了 {summary['unknown']}。"
                     f"（来源：读过 {summary['by_origin']['read']} / "
                     f"仅检索 {summary['by_origin']['searched']} / "
                     f"无调用记录 {summary['by_origin']['unrecorded']}）")
        for i in items[:12]:
            if i["state"] == ALIGNED:
                lines.append(f"  · {i['label']} 已对齐（{i['score']:.2f}）"
                             f"→ {i['doc'] or '?'} p.{i['page'] or '?'}"
                             f"「{i['support'][:60]}」")
            elif i["state"] == UNSUPPORTED:
                lines.append(f"  · {i['label']} 未找到支撑（最高 {i['score']:.2f}）"
                             f"→ 稿件句「{i['claim'][:60]}」；请核对是否转述过度或引错页。")
            else:
                why = ("未提供库内原文" if not summary["sources_given"] else
                       "文献表未标 doc_id" if not i["doc"] else
                       "该页原文不可得" if i["page_text"] in ("missing", UNKNOWN)
                       else "转述过短 / 事实 token 太少，内核判不了" if i["shallow"]
                       else "原文过短，不足以判定")
                lines.append(f"  · {i['label']} 判不了 → {why}（读不到 ≠ 不符合）")
        if len(items) > 12:
            lines.append(f"  · 其余 {len(items) - 12} 条见结构化台账。")
    if not summary["sources_given"]:
        lines.append("⚠️ 本次没有提供库内原文，只核了「出处有没有读过」，"
                     "没做内容级比对。把 kb_read 的回包贴进 sources 即可补全。")
    lines.append("⚠️ 对齐只证明「有文本可支撑」，不判结论对错；本读回不阻断交付。")

    return {"items": items, "summary": summary,
            "text": "\n".join(lines), "has_alignment": bool(items)}
