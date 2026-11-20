# -*- coding: utf-8 -*-
"""文献池所有权层：工具回包自动入池、写后/交付前自动核对（2026-11-12）。

## 为什么是「所有权层」而不是「再给模型一个工具」

11-11 真任务 R1/R4/R8/R9 反复坐实：靠模型主动调工具，默认路径 = **0 次**；
`figure_guard` 的正解是**把身份与编号的所有权从模型手里收回来** ——
`write_file` 成功后、交付前，主循环自己跑确定性整理。文献池照抄这个范式：

  · 检索 / 入库 / 取全文的工具回包一进 `loop.py` 的统一收口，就自动
    `record_query` / `mark_fulltext`，模型不必记得调 `litpool`；
  · 每次写正文后从 `outline.md` 的可见 doc_id 引导建池（bootstrap）——
    **默认路径的分母必须包含「模型没用工具」**（R7 教训）；
  · 交付前出五连读回结论，进 `deliver_attempt` 与 `final`，advisory 不加门；
  · 写后简报（`readiness.py`）带 `LITPOOL_*` 缺口，让静默丢失可见。

## 边界（不粉饰）

  · **不改 SYSTEM、不加硬门、不碰 `self.gate.check(`、不动请求级参数链**
    （`recall` / `unattended` 六处签名一律不碰，同 figure_guard）。
  · 回包形状未知 / JSON 被截断 / 解析失败 ⇒ 回包里**明说**，不静默跳过，
    更不造一个空池冒充通过（「没测到 ≠ 不符合」）。
  · 从 `outline.md` 引导出来的条目 `screening=unscreened`、无精读卡、无编造
    标题 —— 它们会在 `verify()` 里如实报 `UNSCREENED` / `INCLUDED_UNREAD`。
  · 相关性的**语义判断仍归人**：本层只搬事实，不替谁判「这篇相不相关」。
  · 纯标准库；不进 `/api/tools`。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import litpool as LP
from .delivery_pipeline import find_outline_file
from .shape import is_chapter_head as _is_chapter_head

STATE_NAME = LP.STATE_NAME
BOOTSTRAP_Q = "outline-bootstrap"
BOOTSTRAP_SOURCE = "outline.md"

#: 检索类：回包里是一批候选文献 → 记召回台账 + 去重入池。
QUERY_TOOLS = frozenset({
    "literature_search", "kb_search", "kb_list", "kb_rank", "kb_bibtex",
    "web_search",
})
#: 取全文 / 入库类：回包里是若干条「拿到没拿到」→ 只登记事实。
FULLTEXT_TOOLS = frozenset({
    "literature_fetch", "convert_pdf", "kb_add", "kb_ingest", "kb_read",
    "fetch_doi",
})
ALL_TOOLS = QUERY_TOOLS | FULLTEXT_TOOLS

#: 回包里可能装候选列表的键。**不猜**：一个都不认识就明说 `shape_unknown`。
_HITS_KEYS = ("hits", "results", "papers", "docs", "items", "references",
              "records", "data", "files", "entries")
_ID_KEYS = ("doc_id", "doi", "id", "key", "uid", "identifier")
_TEXT_KEYS = ("text", "body", "markdown", "content", "fulltext")

#: 与 `gate.py:_RE_OUTLINE_HEAD` 同形的章标题；排除附录，避免 11-09 P1
#: 「附录节被算成第 9 章」那种分母假红。这里只是 advisory，仍不误报为好。
_CH_HEAD = re.compile(
    r"^\s{0,3}(?:#{1,4}\s+\S|第[一二三四五六七八九十百\d]+[章节部分]\s*\S"
    r"|[一二三四五六七八九十]+\s*[、.．]\s*\S)", re.M)
_SKIP_CH = re.compile(r"附录|参考文献|致谢|声明|补充材料|元数据|大纲|提纲|核算|处理规则|写作计划|字数预算|图清单|图表清单")
_DEFAULT_SKIP_CH = _SKIP_CH


def _norm(s) -> str:
    return str(s or "").strip().lower()


def _parse(result) -> tuple[dict | None, str, str]:
    """回包统一解析。`ToolCall.result` 恒为字符串，且可能被 truncate 截断。"""
    if isinstance(result, dict):
        return result, "dict", ""
    if isinstance(result, list):
        return {"hits": result}, "list", ""
    if not isinstance(result, str) or not result.strip():
        return None, "empty", "回包为空"
    try:
        obj = json.loads(result)
    except ValueError as ex:
        return None, "parse_failed", f"{type(ex).__name__}: {ex}"
    if isinstance(obj, dict):
        return obj, "dict", ""
    if isinstance(obj, list):
        return {"hits": obj}, "list", ""
    return None, f"unknown:{type(obj).__name__}", "回包不是对象也不是数组"


def _rows_of(obj: dict) -> tuple[list, str]:
    """取候选行；找不到数组键时，若回包本身就是一条文献也算一行。"""
    for k in _HITS_KEYS:
        v = obj.get(k)
        if isinstance(v, list):
            return v, k
    if any(k in obj for k in _ID_KEYS):
        return [obj], "self"
    return [], "shape_unknown"


def _doc_of(hit) -> dict | None:
    if isinstance(hit, str):
        return {"doc_id": hit} if hit.strip() else None
    if not isinstance(hit, dict):
        return None
    did = ""
    for k in _ID_KEYS:
        if str(hit.get(k) or "").strip():
            did = str(hit[k]).strip()
            break
    if not did:
        return None
    return {"doc_id": did, "doi": str(hit.get("doi") or "").strip(),
            "title": str(hit.get("title") or hit.get("name") or "").strip(),
            "year": hit.get("year") or "",
            "text": next((str(hit[k]) for k in _TEXT_KEYS
                          if str(hit.get(k) or "").strip()), ""),
            "chars": int(hit.get("chars") or 0) if str(
                hit.get("chars") or "").isdigit() else 0,
            "ok": hit.get("ok", True)}


def _root_of(root) -> Path:
    p = Path(root) if root is not None else Path.cwd()
    return p.resolve()


def ingest_call(name: str, args: dict, result, root=None) -> dict:
    """一次工具调用结束后：把回包里的事实搬进 `文献池.json`，写后读回。

    不改回包、不改工具、不拦调用。失败一律在回包里说清 `reason`，
    由调用方决定要不要 emit，绝不用空池冒充「已入池」。
    """
    name = str(name or "")
    if name not in ALL_TOOLS:
        return {"applied": False, "reason": "not_lit_tool", "tool": name,
                "advisory": True}
    obj, shape, err = _parse(result)
    if obj is None:
        return {"applied": False, "reason": "parse_failed" if err else "empty",
                "tool": name, "shape": shape, "advisory": True,
                "detail": f"回包解析不了（{err or '空'}，可能被 truncate 截断）："
                          "本次不入池，也不冒充已核对"}
    rows, key = _rows_of(obj)
    if key == "shape_unknown":
        return {"applied": False, "reason": "shape_unknown", "tool": name,
                "shape": shape, "keys": sorted(obj)[:12], "advisory": True,
                "detail": "回包里找不到候选列表，也不知道哪个键是 doc_id；"
                          "明说没测到，不静默跳过"}
    docs = [d for d in (_doc_of(h) for h in rows) if d]
    root_p = _root_of(root)
    path = root_p / STATE_NAME
    state = LP.load(path)
    q = str((args or {}).get("query") or (args or {}).get("q")
            or (args or {}).get("term") or name)
    n_new = n_dup = n_fulltext = 0
    errors: list[str] = []

    if name in QUERY_TOOLS:
        rep = LP.record_query(state, q, name, docs)
        n_new, n_dup = int(rep.get("n_new") or 0), int(rep.get("n_dup") or 0)
        if not rows:
            errors.append("回包候选列表为空：本次检索 0 条，如实记台账")
    else:
        # 取全文 / 入库：先按同一份台账建条目，再登记「有没有拿到全文」。
        # record_query 自带去重，已有条目不会被覆盖判定或精读卡。
        rep = LP.record_query(state, q, name, docs)
        n_new, n_dup = int(rep.get("n_new") or 0), int(rep.get("n_dup") or 0)
        for d in docs:
            did = _norm(d["doc_id"])
            if d.get("text"):
                r = LP.mark_fulltext(state, did, d["text"])
            else:
                r = LP.note_fulltext(
                    state, did, d.get("chars") or 0,
                    ok=bool(d.get("ok", True)) and
                    bool(d.get("chars") or 0))
            if r.get("ok"):
                n_fulltext += 1
            else:
                errors.append(f"{did}: {r.get('error', '登记失败')}")

    if not (n_new or n_dup or n_fulltext):
        return {"applied": False, "reason": "no_entries", "tool": name,
                "shape": shape, "list_key": key, "rows": len(rows),
                "errors": errors, "advisory": True}
    saved = LP.save(state, path)
    if not saved.get("ok") or not saved.get("readback_equal"):
        return {"applied": False, "reason": "readback_failed", "tool": name,
                "saved": saved, "errors": errors, "advisory": True}
    return {"applied": True, "tool": name, "shape": shape, "list_key": key,
            "rows": len(rows), "n_new": n_new, "n_dup": n_dup,
            "n_fulltext": n_fulltext, "pool": len(state.get("entries", [])),
            "readback_equal": True, "path": str(path), "errors": errors,
            "advisory": True}


def outline_bindings(root=None) -> dict[str, list[str]]:
    """从大纲文件（`outline.md` / `大纲.md` / `提纲.md`）解析「章 → doc_id」，不编、不猜。"""
    base = _root_of(root)
    p = find_outline_file(base, recursive=False)
    if p is None:
        return {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    # 逐行判整行：只拿正则命中的那一小段（`## 附`）会让「附录」跳过判断失效
    # —— 11-09 P1 的章分母假红就是这么来的，这里宁可窄。
    lines = text.splitlines(keepends=True)
    heads = [i for i, line in enumerate(lines) if _CH_HEAD.match(line)]
    # 有编号章且 `_SKIP_CH` 未被测试覆写时，复用 `shape.is_chapter_head` 同源滤除说明节。
    use_shape = (_SKIP_CH is _DEFAULT_SKIP_CH
                 and any(_is_chapter_head(lines[i]) for i in heads))
    out: dict[str, list[str]] = {}
    ch_no = 0
    for j, i in enumerate(heads):
        end = heads[j + 1] if j + 1 < len(heads) else len(lines)
        # ⚠️ 被跳过的标题也必须是**边界**：否则「## 附录」下面的 doc_id
        # 会并进上一章，正是 11-09 P1「附录被算成第 9 章」的同型假红。
        if _SKIP_CH.search(lines[i]):
            continue
        if use_shape and not _is_chapter_head(lines[i]):
            continue
        seg = "".join(lines[i:end])
        ids = sorted({_norm(x) for x in LP.DOCID_RE.findall(seg)})
        if ids:
            ch_no += 1
            out[str(ch_no)] = ids
    return out


def bootstrap_from_outline(root=None) -> dict:
    """写后引导：模型没调任何检索工具时，也从大纲的可见 doc_id 建池。

    只建条目，**不编标题、不判相关性、不写精读卡** —— 缺的会在
    `verify()` 里如实报 `CRITERIA_UNDECLARED` / `UNSCREENED`。
    """
    base = _root_of(root)
    p = find_outline_file(base, recursive=False)
    if p is None or not p.exists():
        return {"applied": False, "reason": "no_outline", "advisory": True}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as ex:
        return {"applied": False, "reason": f"outline_read_failed:{ex}",
                "advisory": True}
    ids = sorted({_norm(x) for x in LP.DOCID_RE.findall(text)})
    if not ids:
        return {"applied": False, "reason": "no_docid_in_outline",
                "advisory": True}
    path = base / STATE_NAME
    state = LP.load(path)
    known = {_norm(e.get("doc_id")) for e in state.get("entries", [])}
    fresh = [{"doc_id": d} for d in ids if d not in known]
    if not fresh:
        return {"applied": False, "reason": "already_in_pool",
                "pool": len(state.get("entries", [])), "advisory": True}
    src_name = BOOTSTRAP_SOURCE if p.name == "outline.md" else p.name
    LP.record_query(state, BOOTSTRAP_Q, src_name, fresh)
    saved = LP.save(state, path)
    return {"applied": bool(saved.get("ok")), "created": [d["doc_id"] for d in fresh],
            "readback_equal": bool(saved.get("readback_equal")),
            "pool": len(state.get("entries", [])), "advisory": True,
            "note": "引导条目未判相关性、无精读卡，五连读回会如实点名"}


def check_outline(root=None) -> dict:
    """「每章引用 ⊆ 已纳入且已精读的池」的**只读**可判定反馈。"""
    base = _root_of(root)
    path = base / STATE_NAME
    if not path.exists():
        return {"ok": False, "status": "absent", "read_only": True,
                "gaps": [{"code": "LITPOOL_ABSENT"}], "advisory": True}
    state = LP.load(path)
    rep = LP.check_bindings(state, outline_bindings(base))
    rep["status"] = "ok" if rep["ok"] else "gaps"
    return rep


def status(root=None, manuscript_text: str = "") -> dict:
    """交付态结论。池不存在要明说 `absent`，不静默退回扫盘。"""
    base = _root_of(root)
    path = base / STATE_NAME
    if not path.exists():
        return {"ok": False, "status": "absent", "advisory": True,
                "scope": "literature-pool",
                "gaps": [{"code": "LITPOOL_ABSENT",
                          "where": "pool",
                          "detail": "没有 文献池.json：本轮检索/精读/"
                                    "章-文献映射无从核对"}],
                "gap_codes": ["LITPOOL_ABSENT"],
                "next": "文献池缺席（不等于通过）：本轮没有可互证的文献事实源。"}
    state = LP.load(path)
    outline_text = ""
    op = find_outline_file(base, recursive=False)
    if op is not None:
        try:
            outline_text = op.read_text(encoding="utf-8", errors="replace")
        except OSError:
            outline_text = ""
    v = LP.verify(state, outline_text, manuscript_text)
    v.update({"status": "ok" if v["ok"] else "gaps",
              "summary": LP.summary(state),
              "bindings": LP.check_bindings(state, outline_bindings(base)),
              "line": LP.status_line(state)})
    return v


def status_line(rep: dict | None) -> str:
    """给 `final` 的一句话：缺席/缺口都说出口，advisory 不拦交付。"""
    if not rep:
        return "【文献池】本轮没有拿到交付态结论（unknown，不算通过）。"
    if rep.get("status") == "absent":
        return "【文献池】缺席：本轮没有 文献池.json，检索与章-文献映射无从互证（advisory）。"
    s = rep.get("summary") or {}
    line = (f"【文献池】{s.get('entries', 0)} 篇 / 全文 {s.get('fulltext_ok', 0)} 篇"
            f" / 已判定 {s.get('screened', 0)} 篇 / 精读卡 {s.get('cards', 0)} 张")
    if rep.get("gap_codes"):
        line += "；五连读回缺口：" + "、".join(rep["gap_codes"][:6])
    b = rep.get("bindings") or {}
    n_bad = sum(len(b.get(k) or []) for k in
                ("out_of_pool", "unscreened", "unread"))
    if n_bad:
        line += f"；章-文献映射越界 {n_bad} 处"
    return line + "（advisory，不拦交付）"


def guard_line(rep: dict | None) -> str:
    """给模型看的短句：说明工具刚替它做了什么，别再重复检索/手抄证据表。"""
    if not rep or not rep.get("applied"):
        return ""
    bits = []
    if rep.get("n_new"):
        bits.append(f"已把本次回包的 {rep['n_new']} 条候选自动记进文献池")
    if rep.get("n_dup"):
        bits.append(f"去重 {rep['n_dup']} 条")
    if rep.get("n_fulltext"):
        bits.append(f"登记 {rep['n_fulltext']} 条全文状态")
    boot = (rep.get("bootstrap") or {}).get("created") or []
    if boot:
        bits.append(f"按 outline.md 的可见 doc_id 引导建池 {len(boot)} 条"
                    "（未判相关性、未写精读卡）")
    if not bits:
        return ""
    return ("【文献池所有权层】" + "；".join(bits)
            + "。你不必再手抄证据表，也不要自己编 doc_id；"
              "相关性与精读卡仍需按准则逐篇留痕（advisory）。")


def reconcile_after_write(root=None) -> dict:
    """写后这一刻：引导建池 + 只读核对。确定性、可幂等、不调 gate。"""
    base = _root_of(root)
    try:
        boot = bootstrap_from_outline(base)
    except Exception as ex:                                  # noqa: BLE001
        boot = {"applied": False, "reason": f"bootstrap_error:{ex}",
                "advisory": True}
    try:
        st = status(base)
    except Exception as ex:                                  # noqa: BLE001
        st = {"ok": False, "status": "error", "advisory": True,
              "gaps": [{"code": "LITPOOL_GUARD_FAILED",
                        "detail": f"{type(ex).__name__}: {ex}"}]}
    return {"applied": True, "bootstrap": boot, "status": st,
            "line": guard_line({"applied": True, "bootstrap": boot}),
            "advisory": True}


def reconcile_before_delivery(root=None, manuscript_text: str = "") -> dict:
    """交付前最后一次：整理 + 只读结论，进 `deliver_attempt` 与 `final`。"""
    base = _root_of(root)
    rec = reconcile_after_write(base)
    try:
        st = status(base, manuscript_text)
    except Exception as ex:                                  # noqa: BLE001
        st = {"ok": False, "status": "error", "advisory": True,
              "gaps": [{"code": "LITPOOL_STATUS_FAILED",
                        "detail": f"{type(ex).__name__}: {ex}"}]}
    return dict(rec, status=st)
