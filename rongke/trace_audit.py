# -*- coding: utf-8 -*-
"""收尾出处复核与交付正文采集（Wave 1 单源模块）。

收拢原先内联在 `rongke/loop.py` 的出处抽取、台账命中统计、全库 doc_id
本地复算、三态 `mentor_trace_audit`、来源级标注 `_run_trace_audit`、
以及交付正文采集与未完成兜底答复。

纪律：
  · 全部从 `ToolCall` 台账复算，不采信模型自述；
  · 只挂事实（`advisory=True`），不改 `success` / `final` 判定；
  · 三态 `pass / fail / unknown`：「读不到」绝不渲染成「不符合」；
  · 零第三方依赖、零删除调用、纯 LF；不 import `loop.py`。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .shape import prose_files as _shape_prose_files
from .workflow_prompts import (
    MENTOR_KB_TOOLS,
    MENTOR_NET_TOOLS,
    MENTOR_READ_TOOL,
)

# 库内出处标记：〔库·kb0688fb3f p.9〕 / (库 kb0688fb3f p9) / [库·kb0688fb3f·p.9]
_MENTOR_CITE_RE = re.compile(
    r"库\s*[·・.、]?\s*([A-Za-z][A-Za-z0-9_\-]{2,39})\s*[·、,]?\s*[pP]\.?\s*(\d{1,5})")
# 联网补的标记：〔外网·OpenAlex〕 / （外网 · web_search）
_MENTOR_EXT_RE = re.compile(r"外\s*网\s*[·・]\s*([A-Za-z0-9_\-\.]{1,40}|[^\s，。；）)]{1,20})")

_DOC_ID_RE = re.compile(r"\b(kb[0-9a-f]{6,})\b")
_PAGE_RE = re.compile(r"(?<![A-Za-z0-9])[pP]\.?\s*(\d{1,5})\b")

SOURCE_LEVELS = {
    "read": "读",      # 本次 kb_read 真的把那几页读出来了
    "hit": "检",       # 本次检索命中过这一页，但没读原文
    "recall": "忆",    # 本次既没命中也没读，来自历史/方法卡/记忆
}

#: 这些后缀算「正文」（委托 `shape.prose_files` 单源）。
_PROSE_SUFFIXES = {".md", ".txt", ".tex", ".rst", ".docx", ".doc"}


def extract_citations(text: str) -> list[dict]:
    """出处抽取的**唯一实现**：规范标记 + 散文式标注，按 (doc_id,page) 去重。"""
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


def mentor_kb_hits(calls: list) -> dict:
    """从台账里算出**本次检索真正命中**的 (doc_id, 页码) 集合（含 `kb_read`）。"""
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
            except Exception:                                 # noqa: BLE001
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
        except Exception:                                     # noqa: BLE001
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
    """从注册表里的 `kb_list` 拿**全库 doc_id 清单**（本地可复算锚点）。"""
    tool = None
    try:
        tool = registry.get("kb_list") if registry is not None else None
    except Exception:                                         # noqa: BLE001
        return None
    if tool is None:
        return None
    try:
        raw = tool.fn(limit=1000)
        payload = json.loads(str(raw or ""))
    except Exception:                                         # noqa: BLE001
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    ids = {str(it.get("id") or "").strip() for it in items
           if isinstance(it, dict) and it.get("id")}
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
    """出处标注的三条判据，全部从调用台账算，不采信模型的自我陈述。"""
    texts = [str(answer or "")] + [str(t or "") for t in (extra_texts or [])]
    text = "\n".join(texts)
    hits = mentor_kb_hits(calls)
    net_calls = [getattr(c, "name", "") for c in (calls or [])
                 if getattr(c, "name", "") in MENTOR_NET_TOOLS and getattr(c, "ok", False)]
    cites = extract_citations(text)
    exts = [{"source": m.group(1)} for m in _MENTOR_EXT_RE.finditer(text)]

    pair_set = set(hits["pairs"]) | set(hits.get("read_pairs") or [])
    doc_set = set(hits["docs"])
    lib_ids = set(library_ids) if library_ids is not None else None
    for c in cites:
        if (c["doc_id"], c["page"]) in pair_set:
            c["verdict"] = "grounded"
        elif c["doc_id"] in doc_set:
            c["verdict"] = "page_not_hit"
        elif lib_ids is not None and c["doc_id"] not in lib_ids:
            c["verdict"] = "doc_not_in_library"
        else:
            c["verdict"] = "doc_not_in_hits"

    bad = [c for c in cites if c["verdict"] != "grounded"]
    reasons: list[str] = []
    unknown: list[str] = []
    judgments: dict = {}

    if require_library:
        judgments["must_query_library"] = hits["kb_ok_calls"] > 0
        if not judgments["must_query_library"]:
            reasons.append("开着导师模式却一次库都没查"
                           f"（{' / '.join(MENTOR_KB_TOOLS)} 均无成功调用）")
    else:
        judgments["must_query_library"] = None

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

    judgments["external_marked"] = True
    if net_calls and not exts:
        judgments["external_marked"] = False
        reasons.append(f"联网补了 {len(net_calls)} 次（{net_calls[0]}…）却没标一处〔外网·来源〕")
    if exts and not net_calls:
        judgments["external_marked"] = False
        reasons.append(f"正文出现 {len(exts)} 处〔外网·…〕标记，但台账里没有任何联网调用")

    note = ""
    if hits["unparsed"]:
        note = (f"有 {hits['unparsed']} 次库检索的回包不是 JSON，内核没能从里面取出命中集 —— "
                "②的判定依据不全，标了出处对不上不等于就是编的")
    if hits["kb_ok_calls"] and not lib_hit and not hits["unparsed"]:
        note = (note + "\n" if note else "") + \
            "本次库里没有命中任何段落（查过 " + str(hits["kb_ok_calls"]) + \
            " 次，命中 0 篇）—— 按纪律这里**不该**出现库内出处；" + \
            "补来的每一句都得带〔外网·…〕。**不等于**世上没有这类文献。"
    if library_ids is None:
        note = (note + "\n" if note else "") + \
            "本次复核没有取得全库 doc_id 清单（kb_list 不可用 / 回包形状不符 / 调用方未接）—— " \
            "「库里查无此 id」这一层**未测**，出处只按本次命中集核。"
        unknown.append("全库 doc_id 清单未取得 ⇒ 「doc_not_in_library」这一层未测。")
    if "proposal" in set(stages or []):
        note = (note + "\n" if note else "") + \
            "本任务是**开题/研究计划**（proposal 阶段）：证据表里每一条都要锚到" \
            "〔库·doc_id p.页码〕或〔外网·来源〕，且库内锚必须通过上面②的比对 —— " \
            "库里没有的引用**不许写**（要引用就先 `kb_add` 真的入库）。"

    _fails = [k for k, v in judgments.items() if v is False]
    _unknowns = [k for k, v in judgments.items()
                 if v is None and k != "must_query_library"]
    verdict = ("fail" if (reasons or _fails)
               else ("unknown" if (unknown or _unknowns) else "pass"))
    return {
        "kb_only": require_library,
        "verdict": verdict,
        "unknown_reasons": unknown,
        "advisory": True,
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
        "pass": verdict == "pass",
        "answer_chars": len(str(answer or "")),
        "audited_chars": len(text),
        "sources": ["聊天答复"] + [f"落盘正文 #{i + 1}"
                               for i, _ in enumerate(extra_texts or [])],
        "note": note,
        "tools": {"kb": list(MENTOR_KB_TOOLS), "net": list(MENTOR_NET_TOOLS)},
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
    """收尾复核的**唯一入口**：算事实、不判决。"""
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
    aud["advisory"] = True
    return aud


def _prose_files(root: "Path | str | None",
                 limit: int = 12) -> list[tuple[str, Path]]:
    """委托给 `shape.prose_files` —— 工作区正文读回的**唯一**实现。"""
    return _shape_prose_files(root, limit)


def collect_delivery_text(final: str, root: "Path | str | None" = None,
                          per_file: int = 20_000,
                          total_cap: int = 60_000) -> tuple[list[str], list[str]]:
    """复核/答复的**唯一正文来源**：`(正文段列表, 来源说明列表)`。"""
    first = str(final or "")
    texts = [first]
    sources = [f"聊天答复（{len(first.strip())} 字）"]
    budget = max(0, total_cap - len(first))
    for name, p in _prose_files(root):
        if budget <= 0:
            break
        try:
            t = p.read_text(encoding="utf-8", errors="replace")[:min(per_file, budget)].strip()
        except Exception:                                     # noqa: BLE001
            continue
        if not t or t == first.strip():
            continue
        texts.append(t)
        sources.append(f"{name}（{len(t)} 字）")
        budget -= len(t)
    return texts, sources


def should_audit(kb_only: bool, calls: list, texts: list[str]) -> bool:
    """**行为触发**，不看开关：查过库 / 正文出现 doc_id / 导师模式，任一成立就复核。"""
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
    """收尾复核的**唯一出口**：收集正文 → 判是否复核 → 接回全库清单 → 出三态。"""
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
    """所有**未完成**分支的答复单源：断在哪、产出了什么、已落盘正文前 N 字。"""
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
