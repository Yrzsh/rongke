# -*- coding: utf-8 -*-
"""荣珂主循环：Plan → Act → Observe → Verify。

与「对话循环」（`for _ in range(6)`）的四个本质区别：

  1. **目标不漂移** —— 契约常驻 system，并周期性重申；不得自行加码
  2. **证据链** —— 每一步都落结构化 ToolCall（seq / ok / ms / 指纹），可追溯
  3. **收敛检测** —— 同一工具同参数重复调用即判原地打转，主动注入干预
  4. **完成要过门** —— deliver_task 必须附真实证据，通不过就打回并告诉它缺什么

外加两条护栏：
  * **工作区围栏** —— 每次任务一个独立 workspace，进程 cwd 切进去，
    mutating 工具的越界绝对路径被拒（首跑实测：模型会把项目建在 cwd）
  * **诚实退出通道** —— report_blocker。干不成不许假装干成。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .bridge import install_workbench
from .citation import VERIFY_SCHEMA, verify_citation
from .config import Config
from . import context
from .contract import TaskContract, deck_recipe_lines
from .gate import CompletionClaim, CompletionGate, Verdict
from .hooks import WorkspaceFence
from .llm import ContextOverflow, LLM, LLMError, Usage
from .mcp_client import MCPHub, install_mcp
from .memory import Memory, default_root
from . import pricing as _pricing
# 2026-11-09 结构单源：本场形状决策 + 工作区正文读回，别处不许再各算一遍。
from .shape import (AUTO_EXTEND_STEPS, MAX_AUTO_EXTENDS,
                    prose_files as _shape_prose_files, resolve_shape,
                    should_auto_extend)
from .readiness import build_readiness as _build_readiness
from .readiness import compact_event as _readiness_event
from .readiness import ensure_visuals_for_delivery as _ensure_visuals_for_delivery
from .readiness import readiness_after_write
from . import figure_guard as _figure_guard
from . import litpool_guard as _litpool_guard
from .stages import read_skill_text
from .subagent import SUBAGENT_SCHEMA, SubagentRunner
from . import topic
from .telemetry import TraceRecorder
from .tools import Tool, ToolCall, ToolRegistry, scope_fallback
from . import builtin_tools as _bt
from . import delivery_pipeline as _dp
from . import system_sections
from . import trace_audit as _ta
from . import workflow_prompts as _wp
from .builtin_tools import (
    ADVISOR_READ_SCHEMA,
    ADVISOR_SAVE_SCHEMA,
    ADVISOR_STATUS_SCHEMA,
    AUDIT_DECK_SCHEMA,
    AUDIT_FIGURE_SCHEMA,
    CONVERT_PDF_SCHEMA,
    COMPOSE_DECK_SCHEMA,
    DECK_REFERENCE_SCHEMA,
    KB_COVERAGE_SCHEMA,
    KB_TO_OBSIDIAN_SCHEMA,
    LAB_METHOD_SCHEMA,
    LAB_PLAN_SCHEMA,
    LAB_RECORD_SCHEMA,
    LAB_REPORT_SCHEMA,
    LITERATURE_FETCH_SCHEMA,
    METHOD_RECORD_SCHEMA,
    METHOD_STATUS_SCHEMA,
    OUTLINE_SAVE_SCHEMA,
    OUTLINE_STATUS_SCHEMA,
    PATENT_LEADS_SCHEMA,
    PLATE_QC_SCHEMA,
    PLOT_DATA_SCHEMA,
    PLOT_SVG_SCHEMA,
    RAISE_BLOCKER_SCHEMA,
    READ_FILE_SCHEMA,
    RESOLVE_BLOCKER_SCHEMA,
    SET_NOTES_SCHEMA,
    STYLE_DECK_SCHEMA,
    TEMPLATE_DECK_SCHEMA,
    TOPIC_LINK_SCHEMA,
    TOPIC_STATUS_SCHEMA,
    TOPIC_UPDATE_SCHEMA,
    TOPIC_USE_SCHEMA,
    TOPIC_WRITE_SCHEMA,
    WRITE_FILE_SCHEMA,
    _advisor_fns,
    _load_skill,
    _make_audit_figure,
    _make_kb_convert_tools,
    _make_kb_coverage,
    _make_literature_fetch,
    _make_plot_data,
    _make_plot_svg,
    _make_read_file,
    _make_topic_tools,
    _make_write_file,
    _plot_kind_list,
)
from .trace_audit import (
    SOURCE_LEVELS,
    _DOC_ID_RE,
    _MENTOR_CITE_RE,
    _MENTOR_EXT_RE,
    _PAGE_RE,
    _PROSE_SUFFIXES,
    _prose_files,
    _run_trace_audit,
    _source_level,
    audit_delivery,
    collect_delivery_text,
    extract_citations,
    interrupted_final,
    kb_library_ids,
    mentor_kb_hits,
    mentor_trace_audit,
    should_audit,
)
from .workflow_prompts import (
    ADVISOR_METHOD_MARK,
    ADVISOR_METHOD_SKILL,
    ADVISOR_METHOD_TEXT,
    BLOCKER_SCHEMA,
    DELIVER_SCHEMA,
    MENTOR_KB_TOOLS,
    MENTOR_MARK,
    MENTOR_METHOD_MARK,
    MENTOR_METHOD_SKILLS,
    MENTOR_METHOD_TEXT,
    MENTOR_NET_TOOLS,
    MENTOR_OFF_MARK,
    MENTOR_READ_TOOL,
    NUDGE,
    PRESSURE,
    STATS_CHECKLIST,
    SYSTEM,
    WRAPUP,
    answer_shape_text,
    mentor_exit_text,
    mentor_mode_text,
    workspace_listing_text,
)

# 做 PPT 的生产动作序列：单源定义在 `contract.DECK_TOOLCHAIN_RECIPE`。
_DECK_RECIPE_LINES = deck_recipe_lines(indent="    ")


# run() 会切换进程 cwd（chdir 是进程级状态），并发调用会互相踩。
# 内核用这把锁保证同一时刻只跑一个任务 —— Web 控制台因此可以放心用线程。
_WORKSPACE_LOCK = threading.RLock()

# 事件归档上限：防一次超长任务把 session.json 写成几十兆。
_MAX_ARCHIVE_EVENTS = 600

# ── 对话快照的文件名（**单源**，2026-09-21） ──────────────────────────────
# 为什么要单源：这套名字原来散在三个地方 —— 写快照的 `_snapshot`、成功收尾的
# 改名、以及 `web/server.py` 里那份"界面上不该露出来的内部账本"白名单。
# 三处各写一遍就是 H 型（改一处忘一处，必然静默漂移）：改名的目标名一旦和
# 白名单不一致，快照就会**伪装成用户交付的文件**出现在产出物清单里。
# 所以白名单从这份元组**算出来**，不手抄。
#
# 语义：成功 ⇒ 规范名消失（"能不能接着跑"的判据自动失效）、内容改名留下。
SNAPSHOT_RENAMES: tuple[tuple[str, str], ...] = (
    ("conversation.json", "conversation.done.json"),
    ("conversation.json.tmp", "conversation.done.json.tmp"),
)
# 界面上要藏起来的内部分件 = 改名前后两套名字的并集。
# 写成**函数**而不是一句推导式：`hygiene_test.py` 要能拿一对假名字喂进来、
# 证明"它确实随输入变" —— 否则那条"单源"断言只是在复述我自己的写法。
def _internal_names(pairs) -> frozenset[str]:
    return frozenset(_n for _pair in pairs for _n in _pair)


SNAPSHOT_INTERNAL: frozenset[str] = _internal_names(SNAPSHOT_RENAMES)


def _unpack(tc: dict) -> tuple[str, dict, str, str]:
    """拆一次工具调用 → `(name, args, tcid, parse_error)`。

    `parse_error` 非空 = **模型的 arguments 根本没能解析成对象**。

    为什么非要把它带出来（原实现只是 `except Exception: args = {}`）：
    那条静默兜底会把「**我没能读懂你的参数**」说成「**你没给参数**」。
    实测（runs/20260921-113729）：模型某次 `import_document` 的 arguments
    流到 `"kind": "pdf</` 就断了（未闭合），解析失败 ⇒ `args={}` ⇒
    报回「缺少必填参数：file / 你传的是：（一个都没传）」。模型**明明传了**，
    于是它收到一条**自相矛盾**的反馈，无从下手。

    这两种情况的下一步动作**完全不同**：参数坏掉 ⇒「重发一次完整 JSON」；
    真的没传 ⇒「补上这个字段」。把它们压成同一个口径就是 M 型（口径塌缩）。
    """
    fn = tc.get("function") or {}
    name = str(fn.get("name") or "")
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        args, err = raw, ""
    elif raw is None or (isinstance(raw, str) and not raw.strip()):
        args, err = {}, ""                      # 真的没传 —— 交给必填校验去说
    elif isinstance(raw, str):
        try:
            args, err = json.loads(raw), ""
        except Exception as e:
            args = {}
            # 诊断在这里成形：**只有这里同时握着原文和异常**。
            # 给出长度 + 尾部原文，是因为实测的坏法就是"流到一半断了"，
            # 尾部那几十个字符才是模型唯一能据以自纠的证据。
            err = (f"{type(e).__name__}: {e}\n"
                   f"   你给的原文共 {len(raw)} 字符，尾部 160 字符：…{raw[-160:]}")
    else:
        args, err = {}, f"arguments 既不是字符串也不是对象（是 {type(raw).__name__}）"
    if not isinstance(args, dict):
        args, err = {}, (err or f"arguments 解析出来不是 JSON 对象"
                                f"（是 {type(args).__name__}）")
    return name, args, str(tc.get("id") or ""), err


#: 检索类工具：回包是"一串命中"，硬切会切掉「有哪些篇」这段索引。
_INDEX_TOOLS = {"kb_search", "kb_list", "kb_rank", "kb_bibtex"}
#: 索引里每条命中最多占多少字（`- kb123 p.4 《标题》` 这种一行）。
_INDEX_LINE_CHARS = 90


def _index_head(body: str, limit: int) -> str:
    """把「命中清单」提到截断之前，再让摘要吃剩下的字数。

    为什么必须这么做（2026-11-03 实测，`docs/导师模式实测-2026-11-03.md` 第十节）：
      一次 `kb_search` 完整回包 9924 字、10 条命中；旧实现**从头硬切** 6000 字，
      模型看到的前 800 字里只落进 2 条命中的 doc_id/page ——
      剩下 8 篇它**连编号都不知道**，提示词却还在教它"看 snippet 对不对题"。
      这是 M 型缺陷：**没看见**被渲染成了**不存在**。

    新形状（顺序即优先级）：
      ```
      【命中清单 n=N】   ← 全部 N 条，一行一条，绝不被摘要挤掉
      1. kb12345678  p.4  《标题》
      …
      【摘要节选】       ← 只放得下的前几条 snippet
      ```
    清单行如果自己就超预算，按行数比例截并**明说截了几条** ——
    「没列出来」与「不存在」是两件事，这是本仓库的老债。
    """
    try:
        payload = json.loads(body)
    except Exception:                      # noqa: BLE001  不是 JSON：退回原来的硬切
        return ""
    if not isinstance(payload, dict):
        return ""
    rows = list(payload.get("hits") or []) + list(payload.get("refs") or [])
    if not rows:
        return ""
    lines: list[str] = []
    for i, h in enumerate(rows, 1):
        if not isinstance(h, dict):
            continue
        did = str(h.get("doc_id") or h.get("id") or "").strip()
        if not did:
            continue
        pg = h.get("page")
        # 页码缺失要**写出来**，不能省 —— 省了会被读成"这篇没有页码"
        where = f"p.{pg}" if pg not in (None, "") else "p.?"
        title = str(h.get("title") or "").replace("\n", " ")[:52]
        year = str(h.get("year") or "")
        line = f"{i}. {did}  {where}  《{title}》"
        if year:
            line += f" {year}"
        lines.append(line[:_INDEX_LINE_CHARS])
    if not lines:
        return ""
    head = (f"【命中清单 n={len(rows)}，共 {len(lines)} 条可列】\n"
            + "\n".join(lines))
    if len(head) > limit:
        # 只列得下一部分：**必须自报**截了几条
        keep, used = [], len(f"【命中清单 n={len(rows)}】\n")
        for ln in lines:
            if used + len(ln) + 1 > limit - 40:
                break
            keep.append(ln)
            used += len(ln) + 1
        head = (f"【命中清单 n={len(rows)}，这里只列了前 {len(keep)} 条 —— "
                f"其余 {len(rows) - len(keep)} 条请用 kb_search 收窄条件或直接 kb_read】\n"
                + "\n".join(keep))
    return head


def _observation(call: ToolCall, limit: int) -> str:
    body = call.result if call.ok else f"[工具执行失败] {call.error}"
    total = len(call.result or "")
    if len(body) > limit:
        head = _index_head(body, int(limit * 0.45)) if call.name in _INDEX_TOOLS else ""
        if head:
            # 清单 + 摘要：清单已经占掉一部分，剩下的给 snippet
            rest = limit - len(head) - 60
            if rest > 200:
                body = head + "\n\n【摘要节选】\n" + body[len(head):][:rest].lstrip("\n")
            else:
                body = head
            tail = (f"\n…（已截断，原始 {total} 字符；上面的【命中清单】是完整的）"
                    if len(head) + 200 < limit else
                    f"\n…（已截断，原始 {total} 字符）")
        else:
            tail = (f"\n…（已截断，原始 {total} 字符）\n"
                    "要接着看这段的后半，请换更小的范围再调一次"
                    "（检索加 `n` 收窄、读文件用 `offset/limit`、读文献用 `page_from/page_to`）")
        body = body[:limit] + tail if not head else body + tail
    return f"[seq={call.seq} {call.name} {'ok' if call.ok else 'ERR'} {call.ms}ms]\n{body}"


def _messages_chars(messages: list[dict]) -> int:
    """消息体的字符数 —— 上下文占用的代理指标。

    ★ 口径**单源**在 `context.messages_chars`（那里逐字解释了为什么用字符数而不是
      token）。这个壳只保留旧名字：`replay_deck` / 控制台 / 一堆测试都在调它。
      留两份同口径的实现迟早漂移 —— 而"压到多少字符"这件事的全部意义，就是压缩前后
      两个数说的是同一种东西。
    """
    return context.messages_chars(messages)


def _reminder(contract: TaskContract) -> str:
    acc = "\n".join(f"  {i}. {a}" for i, a in enumerate(contract.acceptance, 1)) or "  （无）"
    return (
        "【目标重申 —— 防止漂移】\n"
        f"原始目标：{contract.raw_goal}\n"
        f"验收标准：\n{acc}\n"
        "请确认你当前的工作仍然指向这些目标。"
    )


def initial_messages(contract: TaskContract,
                     ws: "Path | str | None" = None,
                     topics_root: "Path | str | None" = None,
                     kb_only: bool = False,
                     topic_slug: str = "",
                     advisor_slug: str = "",
                     caps=None,
                     assembly_out: "dict | None" = None) -> list[dict]:
    """开跑时的两条消息：system + 第一条 user（正文模板收在 `workflow_prompts`）。"""
    text = contract.to_prompt()
    if ws is not None:
        text += "\n\n" + workspace_listing_text(
            ws, goal=getattr(contract, "raw_goal", "") or "")
    tctx = topic.context_text(root=topics_root,
                              goal=getattr(contract, "raw_goal", "") or "",
                              slug=topic_slug)
    text += "\n\n" + answer_shape_text()
    if tctx:
        text += "\n\n" + tctx
    if kb_only and topic_slug:
        meta = topic.read_topic(topic_slug, root=topics_root) or {}
        ids = meta.get("library", [])
        if ids:
            text += ("\n\n本课题已关联的共享库文献ID：" + "、".join(ids) +
                     "\n优先检索这些文献，必要时仍检索全库补足证据。"
                     "若关联ID已不在共享库，如实说关联失效；不要把它说成整个库为空。")
    if kb_only:
        from . import mentor_review as _MR            # noqa: PLC0415
        _goal = getattr(contract, "raw_goal", "") or ""
        _reh = _MR.is_rehearsal_text(_goal)
        _tslug = str(topic_slug or "").strip() or topic.current(root=topics_root)
        _brief = _MR.mentor_brief(ws, _goal, topic_slug=_tslug if _reh else "",
                                  root=topics_root)
        _artifacts = [_goal] + _brief["sources"] + _brief.get("topic_sources", [])
        text += "\n\n" + mentor_mode_text()
        _route = _MR.topic_route_text(_goal)
        if _route:
            text += "\n\n" + _route
        _trig = _MR.should_review(_goal, _artifacts)
        if _trig["review"] or _reh:
            text += "\n\n" + _MR.build_stance_text(
                _brief["issues"] or None, rehearsal=_reh, review=True,
                inline=True)
            if _reh and _brief.get("topic_digest"):
                text += "\n\n" + _brief["topic_digest"]
        elif _trig.get("sole_request"):
            text += "\n\n" + _MR.build_stance_text(review=False)
        if advisor_slug:
            from . import advisor as _AD             # noqa: PLC0415
            _meta = topic.read_topic(topic_slug, root=topics_root) if topic_slug else None
            _atext = _AD.context_text(
                advisor_slug, goal=_goal,
                root=topics_root,
                topic_stage=str((_meta or {}).get("stage") or ""))
            if _atext:
                text += "\n\n" + _atext
            elif assembly_out is not None:
                assembly_out["advisor_missing"] = advisor_slug

    _needed: list[str] = []
    _collect = getattr(caps, "system_sections", None)
    if callable(_collect):
        _needed = list(_collect() or [])
    _complete_text = ""
    _ct = getattr(caps, "system_complete", None)
    if callable(_ct):
        _complete_text = str(_ct() or "")
    _cf = getattr(caps, "complete_flag", None)
    _complete = bool(_cf()) if callable(_cf) else False
    _keep: list[str] = []
    _kf = getattr(caps, "keep_sections", None)
    if callable(_kf):
        _keep = list(_kf() or [])
    if _complete and not _complete_text:
        for _s in _keep:
            if _s not in _needed:
                _needed.append(_s)
        if not _needed:
            _complete_text = system_sections.minimal_system(SYSTEM)
    try:
        _asm = system_sections.assemble(SYSTEM, needed=_needed,
                                        complete_text=_complete_text)
    except (ValueError, AssertionError) as e:
        _asm = system_sections.assemble(SYSTEM)
        if assembly_out is not None:
            assembly_out["degraded"] = True
            assembly_out["error"] = str(e)
    system_text = _asm.text
    if assembly_out is not None:
        assembly_out.update(_asm.to_dict())
        assembly_out["structural_override"] = bool(_complete or _complete_text)
        assembly_out["kept_sections"] = list(_needed)
        assembly_out["preset"] = str(getattr(caps, "preset_id", "") or "")
    _flag = getattr(caps, "flag", None)
    _sections = (list(_flag("prompt", "sections", []) or [])
                 if callable(_flag) else [])
    _builtin = {"stats-checklist": STATS_CHECKLIST}
    _peek = getattr(caps, "prompt_texts", None)
    _provided = _peek(_sections) if callable(_peek) else {}
    for _name in _sections:
        _t = _provided.get(_name) or _builtin.get(_name)
        if _t:
            system_text += "\n\n" + _t

    _brief_text = ""
    _brief_fact = {}
    _contrib = getattr(caps, "contributions", None)
    if callable(_contrib):
        try:
            for _rid, _v in _contrib("STAGE_BRIEF"):
                if not isinstance(_v, dict):
                    continue
                if _v.get("text"):
                    _brief_text += (("\n\n" if _brief_text else "")
                                    + str(_v["text"]))
                _brief_fact = {
                    "injected": list(_v.get("injected") or []),
                    "missing": list(_v.get("missing") or []),
                    "chars": int(_v.get("chars") or 0),
                }
        except Exception as e:                        # noqa: BLE001
            _brief_fact = {"error": f"{type(e).__name__}: {e}"}
    if _brief_text:
        system_text += "\n\n" + _brief_text
    if _brief_fact and assembly_out is not None:
        assembly_out["stage_brief"] = _brief_fact
        assembly_out["stage_brief_text_len"] = len(_brief_text)
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": text},
    ]


STALL_ASK_EXTRA = 3

#: 正文类后缀（所有权层只碰这些，导出件 / 二进制一律不碰）。
_GUARD_SUFFIXES = _dp.GUARD_SUFFIXES


def _written_prose_path(raw: str):
    """把 write_file 的 path 解析成工作区内的正文文件；不是正文就返回 None。"""
    p = Path(str(raw or ""))
    if p.suffix.lower() not in _GUARD_SUFFIXES:
        return None
    return _dp._resolve_draft_path(raw, Path.cwd().resolve())


def _reconcile_written_file(raw: str, emit) -> dict:
    """★ 每次写正文后：工具自己维护图块、身份与编号（模型不用记）。"""
    q = _written_prose_path(raw)
    if q is None:
        return {"applied": False, "reason": "非正文文件"}
    rep = _figure_guard.reconcile_file(q, q.parent)
    boot = rep.get("bootstrap") or {}
    if (rep.get("applied") and (rep.get("restored") or rep.get("written"))) \
            or boot.get("created"):
        emit("figure_reconciled", {
            "file": q.name, "restored": rep.get("restored") or [],
            "bootstrap": boot.get("created") or [],
            "written": bool(rep.get("written")),
            "numbering": rep.get("numbering") or {},
            "verify_ok": bool(rep.get("verify_ok")),
            "gaps": [g["code"] for g in rep.get("gaps") or []]})
    return rep


def _normalize_prose_before_delivery(emit, target: "Path | None" = None) -> None:
    """交付前对目标正文 .md 跑一次确定性体例规范化（委托 `delivery_pipeline`）。"""
    p = target if (target is not None and target.is_file()) else _dp.resolve_delivery_target({})
    if p is not None and p.is_file():
        _dp.normalize_prose_file(p, emit)


def _reconcile_before_delivery(emit, target: "Path | None" = None) -> dict:
    """★ 交付前最后一次确定性整理 + 只读登记结论（advisory，不加门）。"""
    root = Path.cwd().resolve()
    p = target if (target is not None and target.is_file()) else _dp.resolve_delivery_target({}, root)
    if p is None:
        return _figure_guard.figure_status(root)
    rep = _figure_guard.reconcile_file(p, root)
    if (rep.get("applied") and (rep.get("restored") or rep.get("written"))) \
            or (rep.get("bootstrap") or {}).get("created"):
        emit("figure_reconciled", {
            "file": p.name, "phase": "delivery",
            "restored": rep.get("restored") or [],
            "bootstrap": (rep.get("bootstrap") or {}).get("created") or [],
            "written": bool(rep.get("written")),
            "verify_ok": bool(rep.get("verify_ok"))})
    try:
        body = p.read_text(encoding="utf-8", errors="replace")
    except Exception:                                        # noqa: BLE001
        body = ""
    return _figure_guard.figure_status(root, text=body)


def _litpool_ingest(name: str, args: dict, call, emit) -> dict:
    """★ 工具回包统一收口：检索/取全文的事实自动进池，模型不必记得调工具。"""
    if not getattr(call, "ok", False):
        return {"applied": False, "reason": "tool_failed", "advisory": True}
    try:
        rep = _litpool_guard.ingest_call(
            name, args, getattr(call, "result", ""), Path.cwd())
    except Exception as e:                                  # noqa: BLE001
        rep = {"applied": False,
               "reason": f"ingest_error:{type(e).__name__}: {e}",
               "advisory": True}
    if rep.get("applied") or rep.get("reason") in ("parse_failed",
                                                   "shape_unknown",
                                                   "readback_failed"):
        emit("litpool_ingested", {
            "tool": name, "applied": bool(rep.get("applied")),
            "reason": rep.get("reason") or "",
            "n_new": rep.get("n_new") or 0, "n_dup": rep.get("n_dup") or 0,
            "n_fulltext": rep.get("n_fulltext") or 0,
            "readback_equal": bool(rep.get("readback_equal"))})
    return rep


def _litpool_after_write(emit) -> dict:
    """★ 写正文后：从可见 outline.md / 大纲.md 引导建池。"""
    rep = _litpool_guard.reconcile_after_write(Path.cwd())
    boot = rep.get("bootstrap") or {}
    if boot.get("created"):
        emit("litpool_bootstrap", {"created": boot["created"],
                                   "pool": boot.get("pool") or 0})
    return rep


def _litpool_before_delivery(emit) -> dict:
    """★ 交付前：五连读回结论进 deliver_attempt 与 final（advisory）。"""
    rep = _litpool_guard.reconcile_before_delivery(Path.cwd())
    st = rep.get("status") or {}
    emit("litpool_status", {"status": st.get("status") or "unknown",
                            "gap_codes": st.get("gap_codes") or [],
                            "advisory": True})
    return st


def _guard_line(rep: dict) -> str:
    """给模型看的一句话：说明工具刚做了什么，别再重复插图/手改编号。"""
    if not rep or not rep.get("applied"):
        return ""
    bits = []
    boot = (rep.get("bootstrap") or {}).get("created") or []
    if boot:
        bits.append(f"已按正文里的可见图块自动建立图表登记（{len(boot)} 条："
                    + "、".join(boot[:3]) + "），不必再传 anchor_id")
    if rep.get("restored"):
        bits.append(f"已按图表登记自动补回 {len(rep['restored'])} 个图块（图注与呼应句来自登记）")
    if rep.get("written"):
        bits.append("已按首次出现顺序重编全篇题注与「见图 N」，正文已回写并读回")
    if not bits:
        return ""
    tail = ""
    if rep.get("gaps"):
        tail = "；仍有缺口：" + "、".join(sorted({g["code"] for g in rep["gaps"]}))
    return ("【图表所有权层】" + "；".join(bits)
            + "。你不必再调 visual_place 重插，也不要手改编号" + tail)


def _compose_final(claim, draft: str, files: "list[str] | None" = None,
                   figure_status: dict | None = None,
                   litpool_status: dict | None = None) -> str:
    """把「用户要看到的答复」拼出来 —— **正文优先，概述退居其后**。"""
    summary = str(getattr(claim, "summary", "") or "").strip()
    body = str(draft or "").strip()
    if not body:
        out = ((summary + "\n\n（本轮产物已落盘：" + "、".join(files[:6]) + "）").strip()
               if files else summary)
    elif len(body) < 120 and len(summary) > len(body):
        out = summary + "\n\n【已落盘的正文】\n" + body
    else:
        tail = ("\n\n———\n（本轮交付说明：" + summary + "）"
                if summary else "")
        if files:
            tail += "\n（产物落盘：" + "、".join(files[:6]) + "）"
        out = body + tail
    line = _figure_guard.status_line(figure_status or {})
    tail = (out + ("\n\n" + line if line else "")).rstrip()
    if litpool_status is not None:
        lp = _litpool_guard.status_line(litpool_status)
        if lp:
            tail = (tail + "\n\n" + lp).rstrip()
    return tail


# ── 幻灯片工具工厂薄包装（保持 `deck_test.py` AST 行号与 `deck_desk_test.py` 引用检查）──
def _make_audit_deck(cfg) -> "Callable[..., str]":
    return _bt._make_audit_deck(cfg)


def _make_set_slide_notes(cfg) -> "Callable[..., str]":
    return _bt._make_set_slide_notes(cfg)


def _make_style_deck(cfg) -> "Callable[..., str]":
    return _bt._make_style_deck(cfg)


def _make_deck_from_template(cfg) -> "Callable[..., str]":
    return _bt._make_deck_from_template(cfg)


def _make_compose_deck(cfg) -> "Callable[..., str]":
    return _bt._make_compose_deck(cfg)


def _make_deck_reference(cfg) -> "Callable[..., str]":
    """返回组会 deck 参考样例（引导 `deck_plan` → `deck_make` → `audit_deck`；旧 MCP 工具已退休、不要调）。"""
    return _bt._make_deck_reference(cfg)




_MOUNT_ALWAYS: tuple[str, ...] = ("core", "research", "figure")
_DECK_DOMAIN = "ppt"
# ★ 导师工具组（W25，2026-10-06）：kb（workbench 的 kb_* 文献库）与
#   topic（课题台账五件套）两个域。**一起进退**（设计文档 §2.4 的原话：
#   「G2/G3 没有 G1 就是空壳；G1 没有 G2/G3 就是一次性检索」），
#   由 `mounting_decision` 按 `contract.needs_kb` / 导师开关 / 文献类阶段挂载。
_KB_DOMAIN = "kb"
_TOPIC_DOMAIN = "topic"
# 文献类阶段：契约的阶段清单沾上任何一个 ⇒ 挂 kb/topic 域
# （照 deck 的「阶段含 slides ⇒ 挂 ppt 域」超集先例）。
_KB_STAGES = frozenset({"plan", "search", "fulltext", "read", "translate",
                        "cite", "verify", "topic", "proposal"})


def mounting_decision(contract: "TaskContract | None", names: list[str] | None = None,
                      domain_of=None, kb_only: bool = False,
                      has_topic: bool = False,
                      caps=None) -> dict:
    """算出"本任务给模型看哪些工具"，并**带上理由**。

    返回 `{"allow": [...] | None, "domains": [...], "reason": str, "saved": int}`。
    `allow=None` 表示不裁（全给）—— 只在拿不到领域信息时这样。

    理由必须返回：一个悄悄发生的裁剪会让人对着"工具明明注册了却调不到"发懵
    （这就是本项目反复记的"静默失败"）。

    ★ `kb_only`（导师模式开关，W25 起进挂载判据）：开着 ⇒ **强制**挂 kb/topic
      两个域。这是设计文档 §3.2 的安全不变量「needs_kb ⇒ kb_* 必在 allow 里」
      的机器形态 —— 开关承诺了"基于你的文献库回答"，挂载却不给库工具，
      那个开关就是装饰。它与 `mentor_mode_text` 里点名的工具名**单源**：
      那段提示词提到 `kb_search`/`kb_ask`，所以这里必须保证它们在场。
    """
    want = set(_MOUNT_ALWAYS)
    reason = "core/research/figure 常驻（与加 PPT 之前一致）"
    if (caps is not None
            and getattr(caps, "preset_id", "baseline") not in ("", "baseline")
            and callable(getattr(caps, "initial_domains", None))):
        _preset_doms = [str(d).strip() for d in (caps.initial_domains() or []) if str(d).strip()]
        if _preset_doms:
            want.update(_preset_doms)
            reason += f"；预设初始域={_preset_doms}"
    need_deck = bool(getattr(contract, "needs_deck", False))
    stages = list(getattr(contract, "stages", None) or [])
    if need_deck:
        want.add(_DECK_DOMAIN)
        reason += "；契约判定要产出 deck ⇒ 挂 ppt 域"
    elif "slides" in stages:
        want.add(_DECK_DOMAIN)
        reason += "；阶段含 slides（汇报 PPT）⇒ 挂 ppt 域"
    else:
        reason += "；本任务不像要做 deck ⇒ **不挂 ppt 域**"

    # ── 导师工具组（kb + topic 一起进退）───────────────────────
    # 四条挂载判据，任一命中即挂：
    #   ①导师 **会话带着课题身份**（`has_topic`，2026-11-02 新增）——
    #     这一条的由来是真实的不一致：`initial_messages` 在开场就把
    #     「你在课题 X 上」注入了提示词（`topic_slug` 为空时退回 `current()`），
    #     而挂载判据此前**只看契约与阶段**。于是普通对话里发生的是：
    #     **提示词说你有课题，工具却不给管课题的手** ——
    #     模型只能答「我改不了课题 / 关联不了文献」，而能力全在。
    #     判据改成「提示词注入的那份身份，工具也要给」—— 两处读同一个来源。
    #   ② 导师开关 kb_only（硬：开关承诺了库，不给库工具 = 假开关）；
    #   ③ 契约字段 needs_kb（硬：与 needs_deck ⇒ ppt 同一条不变量）；
    #   ④ 阶段清单沾上文献类阶段（超集，照 deck 的「slides ⇒ ppt」先例）——
    #     阶段是目标文本推断出来的，检索/精读/核验/开题天然要碰库。
    # kb 与 topic **不分家**：课题台账没有库就是空壳，库没有台账就是一次性检索
    # （设计文档 §2.4）。挂一个 = 挂两个。
    need_kb = (bool(kb_only)
               or bool(has_topic)
               or bool(getattr(contract, "needs_kb", False))
               or bool(set(stages) & _KB_STAGES))
    if need_kb:
        want.add(_KB_DOMAIN)
        want.add(_TOPIC_DOMAIN)
        why = ("导师模式开着" if kb_only else
               "本场会话带着课题身份" if has_topic else
               "契约判定要用文献库/课题（needs_kb）" if getattr(contract, "needs_kb", False) else
               "阶段含文献类工作")
        reason += f"；{why} ⇒ 挂 kb/topic 域"
    else:
        reason += "；本任务不沾文献库/课题 ⇒ **不挂 kb/topic 域**"

    if names is None or domain_of is None:
        return {"allow": None, "domains": sorted(want), "reason": reason}

    # 把领域展开成名字。**量不出来就不裁**：若某个想保留的领域名一个工具都
    # 对不上，说明领域命名变了或注册失败 —— 此时按名单裁等于静默地把工具拿走。
    available_domains: set[str] = set()
    for n in names:
        d = domain_of(n)
        if d:
            available_domains.add(d)
    if "mcp" in available_domains and "mcp" not in want:
        want.add("mcp")
        reason += "；检测到 mcp 域工具 ⇒ 挂 mcp 域"
    got: set[str] = set()
    hit: set[str] = set()
    for n in names:
        d = domain_of(n)
        if d in want:
            got.add(n)
            hit.add(d)
    missing = want - hit
    # 纯内核安装（install_domain=False）不注册 research 域工具：只要已有域命中，
    # 不因缺少外部 workbench 的 research 域而整表退回 allow=None。
    if "research" in missing and "research" not in available_domains and hit:
        missing.discard("research")
        reason += "（research 域未安装，跳过该域）"
    if missing:
        return {"allow": None, "domains": sorted(want),
                "reason": reason + f"；⚠️ 领域 {sorted(missing)} 一个工具都没匹配到 ⇒ "
                                   f"退回不裁（宁可多给，也别静默拿走工具）"}
    return {"allow": sorted(got), "domains": sorted(want), "reason": reason}


class LicenseError(RuntimeError):
    """授权不允许发起新任务。

    ★ 它不是"任务失败"，是"任务**没开始**"。刻意做成独立异常类型，
      让上层（CLI / Web）能把它和真的运行失败分开呈现 —— 混在
      `RunResult(success=False)` 里会让遥测、账单、归档都记上一笔
      根本没发生的运行。

    继承 `RuntimeError` 而不是 `Exception`：这是运行时环境问题，不是
    调用方写错了参数，不该和 `ValueError`/`TypeError` 混作一类被
    `except Exception` 顺手吞掉。
    """


@dataclass
class RunResult:
    success: bool
    reason: str
    steps: int
    usage: Usage
    trace: list[ToolCall] = field(default_factory=list)
    final: str = ""
    contract: TaskContract | None = None
    verdict: Verdict | None = None
    workspace: str = ""
    run_id: str = ""                 # 工作区名，也是归档 id（可直接用来回放）
    draft: str = ""                  # 写作任务交付的正文全文（给界面展示）
    lint: dict | None = None         # 写作体检摘要
    # 成本：float（价格表命中）或字面 "UNKNOWN"（未命中）。
    # **不给 0 当默认值** —— 0 会把"没算出来"渲染成"免费的"。
    cost: object = _pricing.UNKNOWN
    telemetry: dict | None = None    # span 时间账摘要（见 telemetry.timing_report）
    # 最后一次上下文出清的**事实**（级别 / 省了多少字符 / 第几步 / 为什么）。
    # 为什么要把事件里已有的东西再放进结果：归档的 `events` 有 600 条上限
    # （`MAX_EVENTS`），一场长任务的出清事件可能被挤出；而 `ctx_econ.py` 的跨源核对
    # 需要第二个来源，否则"emit 了但没真出清"与"根本没 emit"在归档里长得一模一样。
    compacted: dict | None = None
    # 用量是不是全量。`False` ⇒ `usage` 是**中断前的下界**，不是总账
    # （在途那次调用的 usage 网关没来得及回）。
    # 为什么要显式带这个布尔：光看 `input_tokens: 0` 分不出"真没花"与"没测到"，
    # 而下游一律会把 0 读成"没花钱"。`pricing` 用字面 `UNKNOWN` 治同一个病，
    # 这边用量是结构体、没法塞字符串，所以用一个字段声明可置信度。
    usage_complete: bool = True
    #: 这一轮的记账结果（`_charge_billing` 的自述）。
    #: `None` = 还没记（如构造出来但没 `run()`）；`{"ok": False, ...}` = 记账失败，
    #: 但**任务本身可能已经成功** —— 这两件事必须分开看。
    billing: dict | None = None
    #: 收尾的**出处复核事实**（`mentor_trace_audit` 的输出，只挂事实不下判决）。
    #: `None` = 没跑（非导师模式）；`{"error": …}` = 跑了但异常。
    #: 为什么要放进结果：这套判据能分辨真假（PASS / doc_not_in_hits / page_not_hit），
    #: 但只在 run 结尾算一次、只进内存的话，界面和归档都拿不到 —— 那就等于没算。
    trace_audit: dict | None = None
    #: 导师评审回包的**读回事实**（`mentor_review.review_audit` 的输出）。
    #: 同样只挂事实不下判决：不改 success / final，advisory=True。
    #: 必须在这里声明 —— `to_dict()` 是手写字段表，动态挂属性进不了归档与
    #: `/api/…`，那就又是「算过了但谁也看不见」（11-14 接线升级的同型教训）。
    mentor_review: dict | None = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "reason": self.reason,
            "steps": self.steps,
            "usage": self.usage.to_dict(),
            "usage_complete": self.usage_complete,
            "cache_ratio": self.usage.cache_ratio,
            "final": self.final,
            "workspace": self.workspace,
            "run_id": self.run_id,
            "draft": self.draft,
            "lint": self.lint,
            "cost_usd": self.cost,
            "cost_display": _pricing.format_cost(self.cost),
            "billing": self.billing,
            "telemetry": self.telemetry,
            "compacted": self.compacted,
            "trace_audit": self.trace_audit,
            "mentor_review": self.mentor_review,
            "contract": self.contract.to_dict() if self.contract else None,
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "trace": [c.to_dict() for c in self.trace],
        }


class Rongke:
    """荣珂内核。一次实例化，可跑多个任务（每个任务一个独立工作区）。"""

    def __init__(self, cfg: Config | None = None, install_domain: bool = True,
                 use_memory: bool = True, install_mcp_servers: bool = True,
                 install_plugins: bool = True, billing_root=None,
                 license_root=None, preset: str = ""):
        self.cfg = cfg or Config.load()
        # 账单落点的显式覆盖。`None` = 走 `<数据根>/billing`（正常情况）。
        # 为什么留这个口子：测试要能记账**但不碰用户真台账**，
        # 而"把整个数据根改道"会连带改掉工作区根，测试现场就分家了。
        self.billing_root = billing_root
        # 授权落点，同上。测试要能**造一个过期授权**而不碰用户真凭证。
        self.license_root = license_root
        self.llm = LLM(self.cfg.provider, timeout=self.cfg.loop.request_timeout,
                       default_temperature=self.cfg.loop.temperature)
        self.registry = ToolRegistry()
        self._installed: list[str] = []
        if install_domain:
            self._installed = install_workbench(self.registry, self.cfg.runtime.workbench,
                                                kb_domain=_KB_DOMAIN)
            # 如实留下 workbench 为什么没挂上（冻结版缺 mcp/fitz 时就是这里）。
            # 不把它翻成"库里没有"——那是两件事。
            self.workbench_error = getattr(install_workbench, "last_error", "")
        self._install_builtins()

        # ── MCP：荣珂作为**客户端**去连外部 MCP 服务端 ──────────
        # 和上面的 bridge 反射是两条不同的路：
        #   bridge  —— 别人（workbench）的工具，同进程直调，方向朝内
        #   MCP     —— 外部服务端，子进程 + JSON-RPC，方向朝外
        # 后者才是"荣珂是客户端"。enabled 已在 Config.load 判过，这里不重复判。
        self.mcp = MCPHub(self.cfg.mcp.servers if install_mcp_servers else [])
        self.mcp_report: dict = {"installed": [], "skipped": [], "servers": {}}
        if self.mcp.specs:
            try:
                self.mcp_report = install_mcp(self.registry, self.mcp)
            except Exception as e:
                # 装配 MCP 失败不该让内核起不来，但必须**报到明面**
                self.mcp_report = {
                    "installed": [], "servers": {},
                    "skipped": [{"server": "", "reason":
                                 f"装配 MCP 整体失败：{type(e).__name__}: {e}"}],
                }

        # ── 插件：让别人不用改内核就能加工具与图型 ──────────────
        # ★ 位置必须在 CompletionGate **之前**：gate 构造时会抓一份工具清单
        #   用来判"该调的工具调了没有"，插件工具晚装上去它就不认。
        # ★ 加载失败绝不让内核起不来（插件是第三方代码，坏是常态），
        #   但也绝不静默跳过 —— 结果留在 `self.plugins_report` 上，
        #   由 `--plugins` 与界面报到明面。
        self.plugins_report: dict = {"scanned": 0, "ok": 0, "failed": 0, "items": []}
        if install_plugins:
            try:
                from .plugins import load_plugins
                rep = load_plugins(self.registry, cfg=self.cfg)
                self.plugins_report = {
                    "scanned": rep.scanned,
                    "ok": len(rep.ok),
                    "failed": len(rep.failed),
                    "summary": rep.summary(),
                    "items": [{
                        "path": r.path, "ok": r.ok, "name": r.name,
                        "version": r.version, "description": r.description,
                        "tools": r.tools, "chart_kinds": r.chart_kinds,
                        "skipped": r.skipped, "error": r.error, "hint": r.hint,
                    } for r in rep.results],
                }
            except Exception as e:                             # noqa: BLE001
                self.plugins_report = {
                    "scanned": 0, "ok": 0, "failed": 1, "items": [],
                    "summary": f"插件系统整体没跑成：{type(e).__name__}: {e}",
                }
            self._refresh_plot_schema()

        # ── 能力预设：把「这一场用哪套判据/门/工具」收成一份挂载结果 ──
        # ★ 位置在 load_plugins 之后、CompletionGate 之前：P2 的预设可以注册
        #   `writing_metrics` 工具，gate 构造时才能把它抓进工具清单。
        # ★ 装配失败绝不让内核起不来，但也绝不静默：`presets_report` 里带上
        #   理由，事件流与 `--presets` 台账都读得到。判健康只认 `broken`。
        from .caps import Caps, MountResult, load_capability_preset
        self._caps_ctx: dict = {"registry": self.registry}
        try:
            self.caps: Caps = load_capability_preset(
                str(preset or "").strip() or "baseline", ctx=self._caps_ctx)
        except Exception as e:                             # noqa: BLE001
            self.caps = Caps.empty()
            self.caps.report.results.append(
                MountResult("<装配>", "", False,
                            error=f"能力预设整体没跑成：{type(e).__name__}: {e}"))
        self._caps_ctx["caps"] = self.caps
        self._baseline_caps = self.caps
        self.presets_report: dict = self.caps.report.to_dict()

        self.gate = CompletionGate(self.registry, caps=self.caps)
        self.fence: WorkspaceFence | None = None
        self.telemetry: TraceRecorder | None = None
        self.memory = Memory(default_root(self.cfg)) if use_memory else None
        # MCP 开工前自检（只读；**不阻断启动**，见 mcp_doctor 的模块说明）。
        # 存在的理由是一个真踩过的坑：AnySearch 的 key 早就配好、工具也反射进来了，
        # 但装配期没人验过"这些工具到底能不能用"，于是我去手搓爬虫撞了一路 403。
        # 代价极低（纯静态检查，不起子进程），所以默认跑；失败也只是少一条提示。
        self.mcp_health: dict = {}
        if use_memory is not False:
            try:
                from .mcp_doctor import diagnose_mcp
                self.mcp_health = diagnose_mcp(self.cfg, install=False)
            except Exception as e:                             # noqa: BLE001
                self.mcp_health = {"verdict": "unknown", "summary_line":
                                   f"MCP 自检没跑成：{type(e).__name__}: {e}"}

    # ── 装配 ────────────────────────────────────────────────
    def _refresh_plot_schema(self) -> None:
        """把插件注册的图型补进 `plot_data` 参数说明（委托 `builtin_tools.refresh_plot_schema`）。"""
        _bt.refresh_plot_schema(self.registry)

    def _install_builtins(self) -> None:
        """装配内置工具包（含课题与台账 `doc=plan|reading|evidence`、`status=open|resolved|all`）。"""
        _bt.install_builtins(self, kb_domain=_KB_DOMAIN, topic_domain=_TOPIC_DOMAIN)

    @property
    def tools(self) -> list[str]:
        return self.registry.names()

    def describe(self) -> str:
        mcp = ""
        if self.mcp.specs:
            got = len(self.mcp_report.get("installed", []))
            mcp = f" | MCP {len(self.mcp.specs)} 服务端/{got} 工具"
        elif self.mcp_report.get("skipped"):
            mcp = f" | MCP 无可用服务端（{len(self.mcp_report['skipped'])} 条原因）"
        # MCP 自检结论只报**有问题**的（ready 是常态，不值得占一行）
        health = ""
        v = (self.mcp_health or {}).get("verdict")
        if v and v != "ready":
            health = f" | ⚠ {(self.mcp_health or {}).get('summary_line', '自检异常')}"
        return (f"荣珂 v{self._version()} | {self.llm.describe()} | "
                f"工具 {len(self.registry)} 个（领域 {len(self._installed)}）{mcp}{health}")

    def mcp_diag(self) -> dict:
        """MCP 的全量诊断。给 CLI / Web 控制台用。

        把三处"没成"的原因合到一起，且**都留着**：
          * hub.skipped        —— 服务端级（disabled / 重名）
          * report.skipped     —— 工具级（起不来 / 重名 / deny）
          * cfg.mcp.rejected   —— 配置级（v1 不支持远程传输 / 配置读不动）
        静默丢掉任何一类，用户就会对着"明明配了却没有"发懵。
        """
        d = self.mcp.diag()
        d["installed"] = list(self.mcp_report.get("installed", []))
        d["skipped"] = ([{"level": "server", **s} for s in self.mcp.skipped]
                        + [{"level": "tool", **s} for s in self.mcp_report.get("skipped", [])])
        d["rejected"] = list(self.cfg.mcp.rejected)
        d["sources"] = list(self.cfg.mcp.sources)
        d["enabled"] = self.cfg.mcp.enabled
        d["errors"] = dict(self.mcp.errors)
        return d

    def _charge_billing(self, result: "RunResult") -> dict:
        """把一轮任务的成本记进本地台账。返回一份自述（永不为 None）。

        **任何异常都不许冒出去。** 任务已经跑完了 —— 因为记账出错而把
        一次成功的任务报成失败，是主次颠倒。失败信息写进返回字典，
        由界面/命令行决定怎么显示。

        `enforce=False`：这里是**事后记账**，不是事前拦截。拦截该发生在
        开始一个任务之前（那时用户还没等十分钟）；跑到一半因为余额不足
        而中断，只会把人晾在那儿。所以超额也如实记下，让余额变负 ——
        负数余额是一个**准确的信号**，比静默中断好。
        """
        try:
            from .billing import Billing
            b = Billing(self.billing_root)
        except Exception as e:                                 # noqa: BLE001
            return {"ok": False, "error": f"账单系统没起来：{type(e).__name__}: {e}",
                    "hint": "检查数据目录是否可写（`rongke --billing` 可查看）"}

        if not getattr(self.cfg, "billing", None) or \
                not getattr(self.cfg.billing, "enabled", True):
            return {"ok": True, "skipped": True, "reason": "账单在配置里关掉了"}

        try:
            e = b.charge(result.cost, run_id=result.run_id,
                         model=self.cfg.provider.model, enforce=False)
            bal = b.balance()
            out = {
                "ok": True,
                "charged": e.amount,
                "charged_display": _pricing.format_cost(e.amount),
                "remaining": bal["remaining"],
                "remaining_display": _pricing.format_cost(bal["remaining"]),
                "exact": bal["exact"],
                "unknown_charges": bal["unknown_charges"],
            }
            # 余额是 UNKNOWN 时必须说出来：否则"$UNKNOWN 还剩"会被
            # 眼睛读成"没问题"。这不是显示细节，是这套账本的核心语义。
            if not bal["exact"]:
                out["note"] = ("本次成本或历史消耗里有算不出金额的"
                               "（价格表没填全），余额只是上限，不是真值。")
            if bal["remaining"] not in (_pricing.UNKNOWN,) and \
                    float(bal["remaining"]) < 0:
                out["overdrawn"] = True
                out["note"] = (out.get("note", "") +
                               " 余额已为负，请换套餐或充值后继续。").strip()
            return out
        except Exception as e:                                 # noqa: BLE001
            return {"ok": False,
                    "error": f"这一轮没记进账：{type(e).__name__}: {e}",
                    "hint": "任务本身已经完成，账单可事后用 `rongke --billing` 补记"}

    def close(self) -> None:
        """收摊：关掉 MCP 子进程。

        内核本身没有别的常驻资源；MCP 服务端是**子进程**，
        留着不管就是泄漏（尤其像 Origin 那种背后还挂着 COM 实例的）。
        """
        try:
            self.mcp.stop_all()
        except Exception:
            pass

    @staticmethod
    def _version() -> str:
        try:
            from . import __version__
            return __version__
        except Exception:
            return "?"

    # ── 工作区 ──────────────────────────────────────────────
    def make_workspace(self, tag: str = "") -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{tag}" if tag else stamp
        ws = (self.cfg.runtime.workspace_dir / name).resolve()
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    # ── 能力预设：本次运行用哪一套 ────────────────────────────
    def _caps_for(self, preset: str) -> tuple:
        """取本次 run 的能力集。`broken` 非空 ⇒ **回退 baseline 并说出口**，绝不静默。

        预设绑定的是**这一次 run()**，不是全局重建：`Manager.boot()` 是长命
        单例，重建会撞 `busy()` 409，也会让追问串味。`run()` 的模块级串行锁
        保证切换 `self.caps` 时不会有两个任务互相踩。
        """
        from .caps import load_capability_preset

        pid = str(preset or "").strip() or "baseline"
        if pid == self.caps.preset_id and self.caps.healthy:
            return self.caps, ""
        ctx = {"registry": self.registry}
        try:
            c = load_capability_preset(pid, ctx=ctx)
        except Exception as e:                             # noqa: BLE001
            return self._baseline_caps, f"装载抛异常：{type(e).__name__}: {e}"
        if c.broken:
            return self._baseline_caps, "；".join(r.error for r in c.broken)
        ctx["caps"] = c
        return c, ""

    # ── 任务入口 ────────────────────────────────────────────
    def run(
        self,
        goal: str,
        contract: TaskContract | None = None,
        max_steps: int | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        workspace: Path | None = None,
        cancel: threading.Event | None = None,
        intervene: Callable[[str, dict], dict | None] | None = None,
        resume_from: dict | None = None,
        kb_only: bool = False,
        mode: str = "",
        preset: str = "",
        recall: bool = True,
        unattended: bool = False,
        advisor_slug: str = "",
    ) -> RunResult:
        """跑一个任务。

        **串行化**：run() 会切换进程 cwd（chdir 是进程级状态），并发调用会互相踩。
        内核用模块级锁保证同一时刻只跑一个任务，所以 Web 控制台可以放心用线程。

        `cancel` 是 threading.Event：置位后主循环在**下一步开头**退出，
        并如实报告"已取消"——不假装完成。正在跑的那次工具调用不会被中断。

        `intervene` 是**运行中问人**的通道（评审第①条：消灭"整轮重来"）。
        主循环在三种"再跑也是白跑"的时刻回调它并**阻塞等待**：

        | kind | 触发时机 |
        |---|---|
        | `gate_reject` | 交付被证据门**第 2 次**打回（第 1 次留给模型自己修，不吵人） |
        | `stall` | 检测到原地打转（同工具同参数重复） |
        | `step_limit` | 步数耗尽，**退出前**给一次"加步数/补约束"的机会 |

        回调返回 `{"action": ..., "text": ...}`：

        * `action="continue"` —— 什么都不改，接着跑
        * `action="steer"` —— `text` 作为一条用户消息**插进当前消息栈**并继续。
          因为断点就在消息栈里，**这是从断点续跑，不是重来**
        * `action="extend"` —— 加步数（`steps` 字段，默认 20），仅 `step_limit` 有意义
        * `action="stop"` —— 如实结束，理由写明是用户叫停

        **三条不许违反**（都是"没人应答"这一种情况的不同侧面）：
        ① `intervene=None`（CLI 走这条）⇒ 行为与加这个参数之前**一字不差**；
        ② 回调**抛异常**⇒ 当成没答，降级为原行为；
        ③ 回调**超时返回 None** ⇒ 同上。
        **绝不允许"没人应答"把任务卡死或弄成失败** —— 那是把可用性问题升级成可用性事故。

        `resume_from` 是**跨进程**的断点续跑：把上一次留下的对话快照（见
        `conversation.json`）原样交回来，主循环就**跳过装配、直接接着跑**。
        快照里带 `follow_up`（一句用户的话）时就是**会话内追问**：接回前情后
        把这句话追加为一条 user 消息 —— 一场会话聊到底，而不是一问一跑一清零。

        为什么必须落在磁盘上才算数：`intervene` 的续跑只活在**同一个进程**里 ——
        服务一重启、笔记本一合盖、任务一崩，内存里的消息栈就没了，
        用户看到的是"跑到第 7 步的东西全没了，只能重来"。那正是评审第 2 条骂的
        「7 个中断任务没有归档」。**快照落盘 + 崩溃也归档，才让续跑跨得过进程边界。**
        """
        emit = on_event or (lambda kind, data: None)
        # ── 授权闸门（离线激活码）──────────────────────────────
        # 拦在**发起新任务**这一处，且只拦这一处：控制台照常打开、历史
        # 照常读写、已归档的结果照常看。用户被锁在自己文件外面是最糟的
        # 形态 —— 既帮不上"提醒你该买了"，又让人愤怒。
        #
        # ★ 用自定义异常而不是返回一个失败 RunResult：失败结果会被当成
        #   "任务跑了但没成功"，而这里根本**没开始**。两者混在一起会让
        #   遥测、账单、归档都记上一笔不存在的运行。
        try:
            from .license import License
            _ok, _st = License(getattr(self, "license_root", None)).allows_run()
        except Exception:                              # noqa: BLE001
            # 授权子系统自己坏了，**不许因此拦住用户**。
            # 把"我算不出授权状态"升级成"你用不了"是不可接受的降级。
            _ok, _st = True, None
        if not _ok:
            raise LicenseError(_st.reason if _st else "授权无效")
        with _WORKSPACE_LOCK:
            # ★ 预设局部覆盖放在**任务入口**（不在主循环里）：一是这里才是
            #   「这一场用哪把尺子」该定的地方，二是 `resume_test` /
            #   `mentor_workspace_test` 的夹具自检会扫 `_run_serial` 源码里
            #   所有 `self.X` 读取，主循环多一个依赖就得改两处夹具。
            #   `preset=""` 与 `"baseline"` 一个事件都不发 ⇒ 旧路径等价。
            #
            # ★ 每场结束必须**复原**。`Manager.boot()` 是长命单例：registry、
            #   self.caps、gate.caps 全是进程级状态。2026-11-04 真 HTTP 实测
            #   抓到过：跑完 paper-writing 后工具 66→67 不回落，下一场不带
            #   预设的 baseline 任务照样背着 writing_metrics（还沿用上一场的
            #   14 项门禁）。「预设绑定本次 run」不能只在入口成立。
            chosen = self._baseline_caps
            cleanup_tools: list[str] = []
            # ★ 2026-11-09 形状单源：形状决策必须发生在**契约起草之后**
            #   （`_loop:5009`），而预设装配/撤销的记账在 `run()` 这一层。
            #   把「本次 run 的撤销清单」（同一个 list 对象）与「用户显式
            #   指定的 preset」挂到实例上，供 `_loop` 回填/读取；
            #   `_WORKSPACE_LOCK` 保证同一时刻只有一场 run，不会串场。
            self._shape_cleanup_tools = cleanup_tools
            self._run_explicit_preset = str(preset or "").strip()
            try:
                if preset:
                    chosen, caps_err = self._caps_for(preset)
                    if caps_err:
                        emit("preset_error", {"preset": str(preset), "error": caps_err,
                                              "fallback": self._baseline_caps.preset_id})
                    elif str(preset).strip() != "baseline":
                        emit("preset_mounted", chosen.to_dict())
                # 传不传 preset 都显式落到「这一场的尺子」：不传就是开机那套，
                #   绝不留着上一场的残留。
                self.caps = chosen
                self.gate.caps = chosen
                # 开机预设自己注册的工具是长命的，不在撤销清单里。
                boot_tools = set(getattr(self._baseline_caps, "registered_tools", []) or [])
                # ★ 必须 extend，不许重新绑定成新 list：`_loop` 里形状路由
                #   往 `self._shape_cleanup_tools`（同一个对象）追加了新注册
                #   的工具，这里一重新赋值，finally 遍历的就是另一张空表，
                #   跑完一场长综述后 writing_metrics / source_alignment 会
                #   永久留在注册表里（11-09 C 场 /api/tools 读回 69 个坐实）。
                cleanup_tools.extend(
                    t for t in (getattr(chosen, "registered_tools", []) or [])
                    if t not in boot_tools and t not in cleanup_tools)
                return self._run_serial(goal, contract, max_steps, emit, workspace,
                                        cancel, intervene, resume_from, kb_only=kb_only,
                                        mode=mode, recall=recall,
                                        unattended=unattended,
                                        advisor_slug=advisor_slug)
            finally:
                for _t in cleanup_tools:
                    try:
                        self.registry.unregister(_t)
                    except Exception:                      # noqa: BLE001
                        # 清理失败不许把已经跑完的任务改判成失败 —— 但也不
                        # 假装它成功：事件流里说出口，下一场仍能看见真相。
                        emit("preset_cleanup_error",
                             {"tool": _t, "preset": chosen.preset_id})
                self.caps = self._baseline_caps
                self.gate.caps = self._baseline_caps
                # registry 变了，refresh=once 缓存的 schema 必须跟着作废，
                # 否则「工具撤了、模型还看得到」是另一种泄漏。
                self._baseline_caps.invalidate_mount_cache()

    def _run_serial(
        self,
        goal: str,
        contract: TaskContract | None,
        max_steps: int | None,
        emit: Callable[[str, dict], None],
        workspace: Path | None,
        cancel: threading.Event | None = None,
        intervene: Callable[[str, dict], dict | None] | None = None,
        resume_from: dict | None = None,
        kb_only: bool = False,
        mode: str = "",
        recall: bool = True,
        unattended: bool = False,
        advisor_slug: str = "",
    ) -> RunResult:
        self.registry.start_task()
        self.gate.reset()          # 写作体检的打回额度按任务归零

        # 工作区：进程 cwd 切进去，围栏也只放行这里。
        # 这样模型的相对路径产物不会污染项目目录（首跑实测：它把
        # ophiocordyceps_ref_verification/ 建在了 cwd）。
        ws = Path(workspace).resolve() if workspace else self.make_workspace()
        ws.mkdir(parents=True, exist_ok=True)
        self.fence = WorkspaceFence(roots=[ws], base=ws)
        # ── 遥测 ────────────────────────────────────────────
        # 把 hooks.py 里那个**从未被实例化**的 `AuditLog` 的位置转正（见 telemetry.py）。
        # 顺序有讲究：**观察者排在策略之后** —— 这样它记下的是"经过全部拦截之后"
        # 的最终事实（比如围栏改写过参数、或直接拒掉了这次调用）。
        # 反过来放，记下的就是"模型原本想做什么"，与真正执行的事不符。
        self.telemetry = TraceRecorder(
            agent_name="rongke",
            model=self.cfg.provider.model,
            provider=self.cfg.provider.name,
        )
        self.registry.hooks = [self.fence, self.telemetry]
        self.telemetry.start_run()

        # MCP 子进程的 cwd 也必须跟着换工作区。
        # 若沿用上一个任务留下的连接，它的 cwd 还钉在**旧**工作区里 ——
        # 那服务端按相对路径写出来的产物就会落进上一个任务，且毫无报错。
        # 所以：改 workspace + 关掉旧连接，下次调用时按新 cwd 重新起。
        self.mcp.workspace = ws
        self.mcp.stop_all()

        # 对话快照写进本任务的工作区 —— 它是「跨进程续跑」的锚点（见 `_snapshot`）。
        # 放在工作区而不是别处：工作区已经是"这一次任务"的天然边界，
        # 归档、产物、围栏都以它为准，快照没有理由另起一套。
        self._snap_dir = ws
        self._last_step = 0
        # 崩溃时要把"已经烧掉多少"如实带走。累加器只活在 `_loop` 的局部里，
        # 外层兜崩溃时取不到 ⇒ 原实现写死 `usage=Usage()`，于是整条中断路径
        # **必然**报 0（实测一例：真烧 51,241、报 0）。
        # 这里挂的是一个**别名**，不是第二个累加器：`_loop` 每次累加后让它重新
        # 指向同一个对象，全仓仍只有一处真相。`None` = 一次完整响应都没拿到。
        self._run_usage: Usage | None = None

        # 事件双写：一路给界面，一路攒着准备落盘。
        # 落盘是为了**跨进程回放** —— Run 对象只活在服务端内存里，
        # 服务一重启，历史页的记录就成了点不进去的死链。
        events: list[dict] = []

        def tee(kind: str, data: dict) -> None:
            if len(events) < _MAX_ARCHIVE_EVENTS:
                events.append({"kind": kind, "data": data,
                               "t": time.strftime("%H:%M:%S")})
            emit(kind, data)

        emit("workspace", {"path": str(ws)})
        # ★ 导师模式的**状态事实**也进事件（不只进提示词）。
        #   理由：「回放旧会话要得出来当时开没开」—— 归档里那份 `events` 就是唯一真相，
        #   界面读它，而不是另存一份"上次开关是什么"的前端内存（两处真相必然漂一处）。
        #   用 `tee` 不用 `emit`：上面那条 `workspace` 只给界面、不落盘，
        #   而这条**必须落盘**，否则服务一重启，历史里的开关状态就全成了"没开过"。
        #
        # ★ W28：带上**本场会话开局时**的课题 slug。三个理由，缺一个这条字段就没有意义：
        #   ① 界面要能"接着看这个课题的台账"，而课题靠 `CURRENT` 是**跨会话**的残留，
        #      等界面读到的那一刻，别人可能已经切走了（`topic_write` 那段的实测事故）；
        #   ② 追问接的是同一场会话 —— 它的课题身份必须是**开局那一刻**的那一个，
        #      否则「会话内追问」会悄悄写进另一个课题的台账；
        #   ③ 归档里的 `events` 是回放的唯一真相：把话题 id 记在这里，
        #      历史页才**复原得出来当时那个课题**，而不是让前端去猜（猜就是两处真相）。
        #   续跑时**先认快照里那份**（`resume_from["topic"]`），再退回 `CURRENT()`：
        #   CURRENT 是"上次用过的"，快照里那份才是"这一场用的"。
        _sess_topic = str((resume_from or {}).get("topic") or "").strip()
        if not _sess_topic:
            try:
                _sess_topic = topic.current()
            except Exception:
                _sess_topic = ""      # 台账读不动不许拦住任务本身（它只是元信息）
        # ★ 导师模型身份（advisor profile，2026-11-14 续）：与 topic 同一条纪律 ——
        #   「这一场用谁的判断方式」是**开局那一刻**的事实，必须由调用方给，
        #   不许在这里读某个「上次选中」的残留。续跑则先认快照里那份。
        #   为什么要发事件：界面上那个「已选：张老师」的标签必须能回放出来，
        #   否则历史页只能显示「导师模式已开启」，看不出用的是谁的判准。
        _sess_advisor = str((resume_from or {}).get("advisor") or "").strip() \
            or str(advisor_slug or "").strip()
        tee("mentor_mode", {"on": bool(kb_only), "topic": _sess_topic,
                            "advisor": _sess_advisor})

        prev_cwd = os.getcwd()
        t0 = time.time()
        result: RunResult | None = None
        self._compact_log = {}          # 与 `self._last_step` 同一手法：跨层取现场
        try:
            os.chdir(ws)
            result = self._loop(goal, contract, max_steps, tee, cancel,
                                intervene, resume_from, kb_only=kb_only,
                                topic_slug=_sess_topic, mode=mode,
                                recall=recall, unattended=unattended,
                                advisor_slug=_sess_advisor)
            # 出清事实**在收尾处统一挂**，而不是往 7 个 `return RunResult(` 里各塞
            # 一遍：写 7 遍就会有第 8 个返回点漏掉，而"漏掉"在归档里看不出来 ——
            # 只会让 `ctx_econ` 的跨源核对误报。别名对象由 `_loop` 指向同一个 dict。
            result.compacted = dict(getattr(self, "_compact_log", {}) or {}) or None
        except BaseException as e:                       # noqa: BLE001
            # ── 崩溃也要留下归档（评审第 2 条的直接缺口）──
            # 原实现只在 `result is not None` 时归档 ⇒ 任务中途崩掉时，
            # runs/ 里只留一个**空目录**：事后既查不出它跑到哪、也看不出为什么停。
            # 「7 个中断任务没有归档」就是这么来的。
            # 这里**不重新抛**：把崩溃翻成一个如实的 RunResult，让它和
            # 「跑完了但没成功」走同一条收尾路径（成本 / 遥测 / 归档 / 台账都齐）。
            crash = f"{type(e).__name__}: {e}"
            try:
                tee("crash", {"error": crash,
                              "traceback": traceback.format_exc(limit=8),
                              "step": int(getattr(self, "_last_step", 0) or 0)})
            except Exception:
                pass
            # ★ 用量按现场带走，不再写死 `Usage()`。
            #   `_acc is None` = 一次完整响应都没拿到，此时 0 是**下界**不是总额；
            #   非 None = 拿得到已完成的累计，但崩掉那次在途调用的 usage 网关没来得及回。
            #   两种情况都不许被下游读成"这场没花钱" —— 同 `pricing.py` 的定律：
            #   0 是一条断言，"我不知道"是事实，不许把前者渲染成后者。
            _acc = getattr(self, "_run_usage", None)
            _crash_reason = f"执行中断（{crash}）—— 是未完成，不是失败；可以接着跑"
            _crash_step = int(getattr(self, "_last_step", 0) or 0)
            result = RunResult(
                success=False,
                reason=_crash_reason,
                steps=_crash_step,
                usage=(_acc if _acc is not None else Usage()),
                trace=list(self.registry.trace),
                usage_complete=False,
                final=interrupted_final(_crash_reason, _crash_step, ws),
                contract=contract,
                # 崩溃时更要留这份事实：溢出正是从这条路走的，事后要能看出
                # "它出清过、还是压根没来得及出清"。
                compacted=dict(getattr(self, "_compact_log", {}) or {}) or None)
        finally:
            os.chdir(prev_cwd)
            if result is not None:
                result.workspace = str(ws)
                result.run_id = ws.name
        if result is not None:
            # ── 收尾复核：接回 `mentor_trace_audit`（2026-11-03，**分级**，不一票否决）──
            #
            # W36（2026-10-31）把它从收尾处摘掉了，理由是「挂载知识库不该做成审查」。
            # 那个顾虑是**对的**，所以这里不恢复成「跑完再判一次合不合格」，
            # 而是改成一件只产生**事实**、不产生**判决**的事：
            #
            #   · 它**不拦交付**、不改 `success`、不改 `final` 的判定；
            #   · 它只回答一个问题：正文里每个〔库·doc p.N〕，本次到底读到没有；
            #   · 结果挂到 `result.trace_audit`，GUI 读得到、归档存得下。
            #
            # 为什么值得接回来（实测依据，见 docs/导师模式实测-2026-11-03.md 第十六节）：
            # 这套判据**能分辨真假** —— 全部真实落地时 PASS，编造一篇报
            # `doc_not_in_hits`，真篇编造页报 `page_not_hit`，负控两档都拦得住。
            # 而它被摘掉之后，25 处引注里有 11 处本次根本没检索到对应的篇，
            # 学生和老师都看不出来（`__`第十三节）。
            #
            # 为什么现在只「挂事实」不「下判决」：判据②只认本次命中集，
            # 模型靠记忆/方法卡引真文献时也会被判 `doc_not_in_hits`。把它当门会误伤，
            # 当成"来源级标注"才是它真正的用处（同 Hyra 的『观察/已验证/约定』三级）。
            # 2026-11-04 接线收敛：入口只剩 `audit_delivery()` 一个。
            #   · 触发按**行为**（查过库 / 正文有 doc_id / 导师模式），不再只看 kb_only；
            #   · 正文 = 聊天答复 ∪ 本次落盘正文，不再只量 `final`；
            #   · 全库 doc_id 清单在这里接回 `kb_library_ids()`，编造 id 那层不再免检；
            #   · 输出三态 `pass / fail / unknown`，「读不到」不再冒充「不符合」。
            try:
                if not getattr(result, "_audited", False):
                    result.trace_audit = audit_delivery(
                        self.registry.trace, result.final, ws,
                        kb_only=kb_only, registry=self.registry,
                        stages=list(getattr(result.contract, "stages", None) or []))
            except Exception as e:                       # noqa: BLE001
                # 复核是附赠品，出错**不许**影响已经跑完的任务（同 `_archive`）。
                # 但「复核没跑成」必须写成未测，不许留成 None 让界面当通过。
                result.trace_audit = {
                    "error": f"{type(e).__name__}: {e}",
                    "verdict": "unknown", "advisory": True,
                    "unknown_reasons": ["出处复核器异常 ⇒ 本层**未测**"],
                    "summary": "出处复核没跑成（未测，不等于没有违规）。",
                }
            # ★ 接线（2026-11-04）：`renderMentorAudit` 一直挂在前端的
            #   `case "mentor_audit"`，可全仓**没有一处 emit 这个事件** ——
            #   复核结果只躺在 `result.to_dict()` 里，界面永远看不到。
            #   事件名保留旧名：W22/W27 的回放判据按这个 kind 认「审计在场」。
            if result.trace_audit is not None:
                try:
                    tee("mentor_audit", result.trace_audit)
                except Exception as e:                   # noqa: BLE001
                    # `tee` 是「先落盘、再 emit」。`emit` 挂掉（SSE 断了 /
                    # 前端回调抛错）时归档那份**已经**进了 `events`，丢掉的
                    # 只是这一次的界面推送。原来这里是 `pass` —— 界面永远
                    # 不知道这条没送到，回放也看不出「送达失败」，等于把
                    # 一次传输故障伪装成「没有审计」（11-14 同型：静默降级
                    # 比报错更伤）。不抛错（跑完的 run 不该被推送失败打死），
                    # 但把事实写进 `trace_audit` 本身 —— 它进 `to_dict()`，
                    # 归档与 /api 都读得到。
                    if isinstance(result.trace_audit, dict):
                        result.trace_audit["undelivered"] = \
                            f"{type(e).__name__}: {e}"
            # ── 导师评审回包读回（2026-11-14，**输出侧**接线）──
            # 输入侧已在 initial_messages 注入立场段与可复算疑点；但评语回来后
            # 有没有挂位置/证据、有没有冒充「多位独立评审」，原先内核一次都没核
            # —— 判据写了却没人调，等于装饰（同本轮早前修的 force_brief 接线）。
            # 与 trace_audit 同纪律：只挂事实、不下判决，不改 success / final；
            # 异常走 unknown 并留痕，不许吞成 None 让界面当通过。
            #
            # ★ 2026-11-14 同日补（组会/开题场景）：**读回不合格时，把缺什么补进
            #   最终答案**。原来 `next_action` 只进了 `result.mentor_review` 与事件，
            #   界面能看到、**模型与用户都看不到** —— 一份「没挂位置、没挂证据、
            #   0 条问题」的预演会原样交到用户手上，而他明天就要上讲台。
            #   追加的只是「补齐清单」，**不改写、不代评**；且因为 R1-R12 是
            #   纯函数判据，同一个回包重跑必然是同一结论，不会追加出第二份。
            if kb_only:
                try:
                    from . import mentor_review as _MR3        # noqa: PLC0415
                    _g = str(goal or "")
                    # 与输入侧同源：预演时课题台账也是材料（R3 核位置要用它）。
                    _reh3 = _MR3.is_rehearsal_text(_g)
                    _mb = _MR3.mentor_brief(ws, goal,
                                            topic_slug=_sess_topic if _reh3 else "")
                    # ★ 出口**不要**再 `should_review` 现算一遍：那次调用里只有
                    #   goal 与源文本，`mentor_brief` 读到的文件材料在这里拼不回来，
                    #   一份 60 字以内的短稿会被现算成「没有可评审对象」，于是
                    #   R10 把一份已交付的正确预演判成违规。判据与输入侧同源：
                    #   说得出「他要评/他在预演」，就评审。
                    # ★★ 2026-11-15 真实使用修：`or _mb["n_files"]` 不能是否定。
                    #   它的意思是「本轮落盘了材料，就当作要评审」，而建任何丌西
                    #   都会落盘材料 —— 于是「帮我把开题大纲定下来」这类任务
                    #   一定会让模型按 P0/R1-R12 写一份评审，尾巴再附「这一份现在还
                    #   交不出去」（另一个方向的任务）。立场要由**返回类型与请求**
                    #   决定，不能由“有没有产生文件”决定。
                    #   `should_review` 已经把 sources 作为材料看了（返回类型一到，它就启动），
                    #   所以只要裁掉这个课件条件，该评的仍然评。
                    # ★★ 2026-11-15 第三次修：**工作区里落下的材料不能替用户表达“我要评审”**。
                    #   上一版要么 `or _mb["n_files"]`、要么把 sources 塞进
                    #   `should_review` 的 artifacts，效果都是同一件事：建任何产物（开题大纲、
                    #   一张图、一份课题计划）都会附上一段「这一份现在还交不出去」。
                    #   声明式的进步地銷了一个工具供给的边界：**这个出口的职责是独立复核一份已交付的回答**，
                    #   而不是自己发明一份材料再去评它。用户要求评价（asks）与真的在预演（rehearsal）两条才算。
                    _trig = _MR3.should_review(_g, [_g])
                    _want = bool((_trig["signals"]["asks"] and _trig["review"])
                                 or _MR3.is_rehearsal_text(_g))
                    if _want:
                        _src = "\n".join(_mb["sources"]
                                         + _mb.get("topic_sources", [])) or _g
                        _rev = _MR3.review_audit(
                            str(result.final or ""), source_text=_src,
                            user_text=_g, known_issues=_mb["issues"])
                        _rev["advisory"] = True
                        _addendum = ""
                        if not _rev["ok"] and result.final:
                            _addendum = "\n\n" + _MR3.audit_addendum(_rev)
                            result.final = str(result.final) + _addendum
                            _rev["appended_to_final"] = True
                        result.mentor_review = _rev
                        tee("mentor_review", {
                            "ok": _rev["ok"], "verdict": _rev["verdict"],
                            "n_findings": _rev["n_findings"],
                            "n_questions": _rev.get("n_questions", 0),
                            "n_lenses": _rev["n_lenses"],
                            "rehearsal": _rev.get("rehearsal", False),
                            "gaps": _rev["gaps"], "mode": _rev["mode"],
                            "findings": _rev.get("findings") or [],
                            "next_action": _rev.get("next_action", ""),
                            "appended_to_final": bool(
                                _rev.get("appended_to_final")),
                            "advisory": True})
                except Exception as e:                       # noqa: BLE001
                    result.mentor_review = {
                        "ok": False, "verdict": "unknown", "advisory": True,
                        "error": f"{type(e).__name__}: {e}",
                        "summary": "导师评审复核没跑成（未测，不等于评语合格）。",
                    }
            # ── 收尾：闭合根 span，把时间账与成本挂上去 ──
            # 成本由**总用量算出**，而不是逐步累加 ——
            # 逐步累加会多一条"累加器"这条独立真相（B 型缺陷：两处各算一遍，
            # 靠约定同步）。总用量已经是唯一真相，计价就该是它的纯函数。
            elapsed = time.time() - t0
            cost = _pricing.estimate_cost(result.usage, self.cfg.provider.model,
                                         getattr(self.cfg, "pricing", None))
            result.cost = cost
            # ── 账单：把这一轮消耗记进本地台账 ──
            # ★ 为什么要在这里扣，而不是在调用 `run()` 的外面扣：
            #   成本是**总用量的纯函数**，而总用量只有这里知道。放到外面
            #   就意味着调用方各自去算一遍，那又多了一处真相（B 型缺陷）。
            # ★ 为什么扣费失败**不许**影响任务结果：任务已经跑完了，
            #   图已经画出来了。因为记账出错就把成功报成失败是颠倒轻重。
            #   所以这里捕获一切异常，只写进 `result.billing` 让人看得见。
            result.billing = self._charge_billing(result)
            if self.telemetry is not None:
                self.telemetry.finish(
                    cost=cost, iteration_count=result.steps,
                    status="ok" if result.success else "error")
                result.telemetry = self.telemetry.summary(
                    elapsed, cost=cost, llm_calls=result.steps)
                emit("metrics", {"telemetry": result.telemetry,
                                 "cost_display": _pricing.format_cost(cost)})
            # 归档的 goal 用**会话标题**（契约里的用户原话）而不是这一轮的输入：
            # 会话内追问时，这一轮 goal 是最后一句追问 —— 拿它当标题，
            # 历史里同一个会话就会一遍换一个名字。原话才是这场会话的身份。
            # 契约从 result 上取：自动起草发生在 `_loop` 里，外层这个局部
            # 变量在"没传冻结契约"时是 None（那正是默认路径）。
            _title_c = result.contract or contract
            self._archive(ws, result, events, t0,
                          str(getattr(_title_c, "raw_goal", "") or goal),
                          spans=(self.telemetry.spans if self.telemetry else []))
            if result.success:
                # 跑成功了就不需要「接着跑」的快照 —— 留着只是占盘。
                # **失败 / 中断的一律保留**：那正是用户要续跑的场合。
                #
                # ★ 为什么是「改名」而不是「删掉」（2026-09-21 改）：
                #   本机**按轮次**收删除费（额度 50 点/轮，一个文件 1 点），
                #   而这一步在**每一次成功的任务**末尾都会跑 —— 于是"跑通一个任务"
                #   会静默花掉一点额度。额度吃穿之后守卫**直接杀进程**：
                #   主循环会在 `_archive` 之后、`memory.save_run` 之前被杀，
                #   用户看到的是"一次莫名其妙崩掉的、其实已经成功的任务"。
                #   ⚠️ 外面那个 try/except **兜不住"被杀"** —— 它只拦异常，拦不住信号。
                #   改名（`os.replace`）**不计费**，而且"能不能接着跑"的判据
                #   （`web/server.py` 里 `run.resumable` 找 `conversation.json`）
                #   会自动失效 —— 语义与原实现**一模一样**。
                #   代价只是几十 KB 盘，而这个目录本来就是**永久归档**，盘不是瓶颈。
                for _f, _t in SNAPSHOT_RENAMES:
                    try:
                        _src = ws / _f
                        if _src.is_file():
                            os.replace(_src, ws / _t)
                    except Exception:
                        pass
        if self.memory and result is not None:
            try:
                self.memory.save_run(goal, result, time.time() - t0)
                emit("memory_saved", {"runs": self.memory.stats()["runs"]})
            except Exception as e:                     # noqa: BLE001
                # 同 billing 的纪律：记账失败不许把已经跑完的任务报成失败；
                # 但 `except: pass` 会让人以为写进去了 —— 如实发一条事件留痕。
                emit("memory_save_error", {"error": f"{type(e).__name__}: {e}"})
        return result

    # ── 归档 ────────────────────────────────────────────────
    @staticmethod
    def _archive(ws: Path, result: RunResult, events: list[dict],
                 started: float, goal: str, spans: list | None = None) -> None:
        """把整场任务落盘，供跨进程回放。

        写失败**不打断任务** —— 归档是附赠品，不是交付的一部分。
        `result` 走 to_dict()：trace 里每个调用带 `args` + `digest` +
        `result_preview`（≤800 字）+ `result_truncated` + `anchors`（DOI/URL/数值），
        **仍不含结果原文**（一次长检索就能让归档膨胀到几十 MB）。

        为什么是"预览 + 锚点"而不是"要么全存、要么只存哈希"：
        只存哈希 ⇒ 证据门第 4 条**离线不可复算**、界面只能显示一串哈希（Q 型缺陷）；
        全存 ⇒ 归档不可分发。锚点让「这份稿子里的每个数字 / DOI 能不能追到一次真实调用」
        变成可离线重算的**字符串比对** —— 这是审计导出的第一块砖。

        `spans` 单独写成 `trace.jsonl`（一 span 一行）而不是塞进 session.json：
        span 条数随步数线性增长，混进 JSON 会让整个归档越来越难解析；
        分行则能流式读、能 tail、坏一行不影响其余。
        """
        try:
            payload = {
                "id": ws.name,
                "goal": goal,
                "workspace": str(ws),
                "started": started,
                "finished": time.time(),
                "elapsed": round(time.time() - started, 1),
                "events": events,
                "result": result.to_dict(),
                "error": "",
            }
            (ws / "session.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        # trace.jsonl 独立写：它失败不该连累 session.json（已先写完）。
        if spans:
            try:
                from .telemetry import MAX_SPANS
                rows = spans[:MAX_SPANS]
                text = "\n".join(
                    json.dumps(s.to_dict() if hasattr(s, "to_dict") else s,
                               ensure_ascii=False, default=str)
                    for s in rows)
                (ws / "trace.jsonl").write_text(text + "\n", encoding="utf-8")
            except Exception:
                pass

    @staticmethod
    def _written_files(root: "Path", limit: int = 12) -> list[str]:
        """本次工作区里**成文的产物**文件名（给 `final` 用）。

        只列正文类后缀、只列一层 + `data/` 一层，按修改时间从新到旧。
        拿不到就返回空表 —— 这里任何失败都不许影响已经跑完的任务。
        """
        # ★ 原先这里把 `_prose_files` 逐行抄了一遍（第三套真相）。
        #   收尾答复与成功答复现在读同一份工作区清单。
        return [n for n, _ in _prose_files(root, limit)]

    @staticmethod
    def _draft_for_display(claim: CompletionClaim, limit: int = 200_000) -> str:
        """把交付正文取出来给界面看（盘上全文更完整/较新时优先读盘，与 `gate._draft_text` 同口径）。"""
        t = (claim.draft_text or "").strip()
        p = (claim.draft_path or "").strip()
        file_text = ""
        if p:
            try:
                path = Path(p)
                if path.is_file():
                    file_text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                file_text = ""
        if not file_text and t:
            cand = _dp.resolve_delivery_target({"draft_path": ""}, Path.cwd())
            if cand is not None and cand.is_file():
                try:
                    cand_text = cand.read_text(encoding="utf-8", errors="replace")
                    if _dp._norm_compare(t) in _dp._norm_compare(cand_text):
                        file_text = cand_text
                except Exception:
                    pass
        if file_text and (not t or _dp._norm_compare(t) in _dp._norm_compare(file_text)):
            return file_text.strip()[:limit]
        if file_text and p and len(file_text.strip()) > len(t) * 1.2:
            return file_text.strip()[:limit]
        if t:
            return t[:limit]
        return file_text.strip()[:limit] if file_text else ""

    # ── 主循环 ──────────────────────────────────────────────
    def _loop(
        self,
        goal: str,
        contract: TaskContract | None,
        max_steps: int | None,
        emit: Callable[[str, dict], None],
        cancel: threading.Event | None = None,
        intervene: Callable[[str, dict], dict | None] | None = None,
        resume: dict | None = None,
        kb_only: bool = False,
        topic_slug: str = "",
        mode: str = "",
        recall: bool = True,
        unattended: bool = False,
        advisor_slug: str = "",
    ) -> RunResult:
        cfg = self.cfg
        # 保留用户显式给的步数：形状决策在契约冻结之后才跑，那时才分得清
        # 「40 步够不够」。覆盖之后就再也拿不回原值了。
        _explicit_steps = max_steps
        max_steps = max_steps or cfg.loop.max_steps

        # ── 续跑：把上一次留下的对话**原样接回来** ──
        # 续跑时**绝不重新起草契约**：契约已在快照里，重新起草会得到另一份标准，
        # 于是"接着跑"其实换了一把尺子 —— 那正是评测口径里"评分标准漂移"的老问题，
        # 在这里会变成"同一份稿子前后两段按不同要求写"。
        resumed = list((resume or {}).get("messages") or [])
        # ★ W28 会话的**课题身份**：优先认快照里那份（`resume["topic"]`），
        #   其次认调用方给的（`_run_serial` 开局时读的 `CURRENT`），**不**在这里
        #   再读一次 `CURRENT` —— 那是"上一次用过的课题"，中途被切走就串了。
        #   它只有一个去处：`_snapshot` 落盘时带上，供下一次续跑/追问认回同一个课题。
        _sess_topic = (str(topic_slug or "").strip()
                       or str((resume or {}).get("topic") or "").strip())
        # ★ 导师模型身份（2026-11-14 续）：与课题身份同一条纪律 ——
        #   续跑先认快照里那条，再认调用方给的。**不读任何「上次选中」的残留**：
        #   回落到残留会造成「前半段按张老师评、后半段按通用视角评」，
        #   而两条消息长得一样，事后翻历史也发现不了。
        _sess_advisor = (str((resume or {}).get("advisor") or "").strip()
                         or str(advisor_slug or "").strip())
        if resumed and contract is None and (resume or {}).get("contract"):
            try:
                contract = TaskContract.from_dict(resume["contract"])
            except Exception as e:
                emit("resume_note", {"text": f"快照里的契约用不了，回退到自动起草：{e}"})
        if contract is None:
            emit("contract_drafting", {"goal": goal})
            contract = TaskContract.draft(goal, self.llm)        # ★ W36「任务模式 / 仅问答」：用户亲手按下的开关**优先**，盖过起草器与词表。
        #   用户原话：「直接加个任务模式和仅问答不就行了」—— 对，这件事该是**他说的**，
        #   不是我们猜的。`answer_only=True` 的契约在门那边换一种判定（要真答，
        #   不要产物，但**编造证据照样抓**，见 gate.check 的第 1 条）。
        #   只改这一个字段，不动 deliverables/acceptance —— 用户要是真要东西，
        #   把模式切回任务即可，而"我替你加一条验收标准"是绝不添加用户没说过的东西。
        if (mode or "").strip() == "answer" and not contract.answer_only:
            contract.answer_only = True
            emit("mode", {"mode": "answer", "by": "user",
                          "text": "仅问答：这一轮按「回答 + 自查」走，不要求产物与证据"})
        elif (mode or "").strip() == "task" and contract.answer_only:
            contract.answer_only = False
            emit("mode", {"mode": "task", "by": "user",
                          "text": "任务模式：这一轮要交东西，按契约与证据门走"})

        # ── 本场结构单源（2026-11-09 根因收敛）────────────────────────
        # 契约此刻才冻结，这里才第一次能回答「这一场是什么形状、给多少步、
        # 挂哪把尺子」。原先步数在入口用全局常数 40 定死、预设在 `run()`
        # 里只认前端胶囊，两者都不读契约 ⇒ 长综述两场都在第 40 步弹窗、
        # 默认路径一个新能力都挂不上。显式参数永远优先；非长综述的事件流
        # 与 SYSTEM 一字不变（baseline 等价裁决不动）。
        _shape = resolve_shape(
            contract,
            explicit_preset=getattr(self, "_run_explicit_preset", ""),
            explicit_max_steps=_explicit_steps,
            default_max_steps=cfg.loop.max_steps,
        )
        max_steps = _shape.max_steps
        if _shape.kind == "long_review":
            emit("run_shape", _shape.to_event())

        # 自动路由只发生在「用户没点胶囊 + 长综述 + 非续跑」这一种情况。
        # 续跑绝不换尺子（快照里的契约与上一场的判据必须原样沿用）。
        # ★ 新注册的工具必须回填 `run()` 的撤销清单，否则会精确重现
        #   11-04 那次事故：跑完一场后工具不回落、下一场仍背着别场的门禁。
        # ★ 全部走 getattr 降级（本仓 `_loop` 的既有约定）：续跑 / 轻量夹具
        #   里可能没有这些协作对象，形状路由是可选路径，取不到就安静跳过，
        #   不许让一场本来能跑完的续跑任务崩在夹具上。
        _route_box = getattr(self, "_shape_cleanup_tools", None)
        _caps_for = getattr(self, "_caps_for", None)
        _baseline = getattr(self, "_baseline_caps", None)
        if (_shape.routed and not resumed and isinstance(_route_box, list)
                and callable(_caps_for) and _baseline is not None):
            _routed_caps, _route_err = _caps_for(_shape.preset)
            if _route_err:
                emit("shape_route_error",
                     {"preset": _shape.preset, "error": str(_route_err)})
            else:
                self.caps = _routed_caps
                self.gate.caps = _routed_caps
                emit("preset_mounted", _routed_caps.to_dict())
                _boot = set(getattr(_baseline, "registered_tools", []) or [])
                for _t in (getattr(_routed_caps, "registered_tools", []) or []):
                    if _t not in _boot and _t not in _route_box:
                        _route_box.append(_t)

        # ── 预设声明的工序 → 契约（2026-11-04，接活 Preset.stages）──────────
        # 每道工序对应 stages.py 里一个**专属 skill**。用户在胶囊里选了「统计
        # 审查」，就该真按统计那几道走，而不是仍由话术关键词碰运气 —— 关键词
        # 只认用户恰好用的那个字眼，换种问法就漏。
        #
        # ★ 只加不删：原话推出来的工序一律保留，预设只补它点名的那几道；
        #   再按 STAGE_ORDER 归位（plan_text 逐条编号念给模型，顺序即指令）。
        # ★ 这份清单同时是**模型看得见**的：contract.to_prompt() 里 plan_text()
        #   会把每道工序和它的 skill 名念出来，门那边才按同一份清单核装载。
        #   工具给了、判据也给了，才不会变成 D 场那种「交不出去的死门」。
        # ★ 取只读视图用 getattr 降级：续跑 / 轻量夹具里可能没有这个属性，
        #   算不出就安静跳过，别把一场本来能跑完的任务打崩。注释里同理，
        #   夹具自检扫的是源码文本，别在这里写点号形式的属性名。
        _view = getattr(self, "caps", None)
        _declared = list(getattr(_view, "preset_stages", []) or [])
        if _declared:
            from .caps import merge_preset_stages
            _merge = merge_preset_stages(contract, _declared)
            if _merge["changed"]:
                emit("preset_stages", {
                    "preset": getattr(_view, "preset_id", "") or "",
                    "declared": _merge["declared"],
                    "added": _merge["added"],
                    "stages": _merge["stages"],
                    "skills": _merge["skills"],
                })
        # ★ W36（2026-10-31 定稿，用户原话「普通对话不该先跑一遍整理任务单」）：
        #   起草那一次调用**留着**（它是判断"这算不算写稿"的唯一来源，省不掉），
        #   但**非写作任务不再把任务单发到界面**。
        #
        #   区别要说准：用户讨厌的不是"多花一次模型调用"（他看不见这个），
        #   而是"我就问一句话，它先给我列一张单子"——那是把对话变成了填表。
        #   所以这里按 `needs_writing` 分流：
        #     · 写稿 ⇒ 照旧发 `contract`（那张单子真的是尺子，该看得见）；
        #     · 其余 ⇒ 只发一句 `mode` 说明"这是一次对话，不是任务"，
        #       界面不会长出任务单，模型那边照样带着契约干活。
        # ★ 用 getattr 兜底：`contract` 不一定是真 `TaskContract`（续跑替身、
        #   测试夹具都会传轻量对象）。这里只是**分流发不发任务单**，
        #   拿不到这个字段就当"不是写稿"——不列单子比无谓崩掉好。
        if getattr(contract, "needs_writing", False):
            emit("contract", contract.to_dict())
        else:
            emit("mode", {"mode": "chat", "by": "auto",
                          "text": "对话式请求：直接办，不列任务单、不要验收标准"})

        if resumed:
            # ★ E2「追问先脱水」（默认关闭）：追问背着父轮完整历史开跑，
            #   到第 3 步才压，前两步白背 145,277 tok。预设打开时，在
            #   **接回消息栈之前**先裁成「开场 system 段 + 最近 keep_live 条」。
            #   用 getattr 取而非直接读属性：内核在 resume/mentor 的轻量夹具里
            #   没有这个属性，算不出就该降级而不是崩。
            #   ⚠️ 注释里**不许写属性名的点号形式**：resume_test 的夹具自检是
            #   正则扫源码字面量的，写在注释里也会被当成真实读取（2026-11-04 实踩）。
            _caps0 = getattr(self, "caps", None)
            _cc0 = _caps0.inst("context") if _caps0 is not None else None
            if (_cc0 is not None and _cc0.cfg["compact_before_followup"]
                    and str((resume or {}).get("follow_up") or "").strip()):
                keep = int(_cc0.cfg.get("keep_live", 6))
                tail = resumed[-keep:] if keep > 0 else list(resumed)
                tail_ids = {id(m) for m in tail}
                head = [m for m in resumed
                        if m.get("role") == "system" and id(m) not in tail_ids]
                trimmed = head + tail
                if len(trimmed) < len(resumed):
                    emit("followup_precompact",
                         {"before": len(resumed), "after": len(trimmed),
                          "kept_live": keep})
                    resumed = trimmed
            messages: list[dict[str, Any]] = [dict(m) for m in resumed]
            # ── 会话内追问（连续会话的全部机制就这几行）──
            # 与 `_steer` 同一条原理：断点本来就在消息栈里，追加一条 user 消息，
            # 下一轮模型就带着**完整前情 + 新的话**接着干。区别只在时机：
            # `_steer` 是运行中插话，这里是一轮结束后接着说。
            # 契约**原样沿用**（不重新起草、不改 raw_goal）—— 它是这一场会话
            # 的验收尺子；追问只是往同一把尺子下加一条新指令。
            _fu = str((resume or {}).get("follow_up") or "").strip()
            if _fu:
                messages.append({"role": "user", "content":
                                 f"【会话内追问 —— 就地接着说，不必重来】\n{_fu}"})
                # 历史回放要能显示"你追问了什么"：只记长度会让用户
                # 打开历史时只看到一段无头的过程（实测：输入 1 的那次）。
                emit("follow_up", {"chars": len(_fu), "text": _fu})
                # ★ Q5「追问的新指令要进验收」（默认关闭）。不重起草契约、
                #   不改 raw_goal、父轮条款一条不删 —— 只把这句追问里的
                #   祈使/否定句追加成新的 acceptance。C 场那三条要求就是
                #   这么丢的；baseline 下这条完全不执行。
                _caps1 = getattr(self, "caps", None)
                _fc1 = _caps1.inst("followup") if _caps1 is not None else None
                if _fc1 is not None and _fc1.cfg["merge_new_constraints"]:
                    _parent = list(getattr(contract, "acceptance", None) or [])
                    _new = _fc1.split_constraints(_fu, _parent)
                    if _new:
                        try:
                            contract.acceptance = _parent + _new
                            emit("followup_constraints",
                                 {"n": len(_new), "items": _new,
                                  "parent_kept": len(_parent)})
                        except Exception as e:               # noqa: BLE001
                            emit("followup_constraints",
                                 {"n": 0, "items": [], "error": f"{type(e).__name__}: {e}"})
            # ★ 导师模式在**同一场会话里**的状态延续（W22-B §3.2）。
            #   追问接回来的是同一份消息栈，于是有两种对不上：
            #   · 这次开着、栈里没有那段（上一轮没开）⇒ **补上**，否则开关是假的；
            #   · 这次关了、栈里还有那段（上一轮开着）⇒ **撤销**，否则界面已经关了
            #     而模型还在按导师模式答题，用户看不见这个分差。
            #   ★ 只测"打得开"不测"关得掉"，正是本项目记过的单向测试缺口 —— 两个方向
            #     在这里都成立，也都进 `resumed` 的计数（补了几条要说得出几条）。
            _had = any(MENTOR_MARK in str(m.get("content") or "") for m in messages)
            if kb_only and not _had:
                messages.append({"role": "user", "content": mentor_mode_text()})
                emit("mentor_mode_injected", {"action": "on", "reason": "会话中途开启"})
            elif _had and not kb_only:
                messages.append({"role": "user", "content": mentor_exit_text()})
                emit("mentor_mode_injected", {"action": "off", "reason": "会话中途关闭"})
            emit("resumed", {"from_step": int((resume or {}).get("step") or 0),
                             "messages": len(messages),
                             "chars": _messages_chars(messages),
                             # 接回来的这份栈**是否已经脱过水**，必须在续跑那一刻说清：
                             # 否则下一次出清的"省了多少"会拿一份已经缩过水的历史当基线，
                             # 于是 ctx_econ 里 freed_chars 偏低、看着像"没起作用"。
                             "prior_compaction": (resume or {}).get("compacted") or {}})
        else:
            # `Path.cwd()` 就是本次工作区：`_run_serial` 在调 `_loop` 之前
            # 已经 `os.chdir(ws)`（同一个前提 `_make_audit_deck` 也在用）。
            # ⚠️ 顺带一条纪律：**cwd 是被测语义的一部分** —— 谁在别的 cwd 下
            # 调这条路径，谁就会拿到错的清单（测试里必须自己 chdir）。
            # ★ 课题上下文（导师模式）由 `initial_messages` 内部注入，这里**不传
            #   `topics_root`**：默认落点由 `topic.topics_root()` **单源**定义 =
            #   `config.project_root()/topics`（即 `<项目根>/topics`）。
            #   **这是有意的** —— 课题要跨 run 活着，不能住在某个 run 的工作区里；
            #   而 `config.project_root()` 由**代码位置**推导，不受 `_run_serial` 的
            #   `os.chdir(ws)` 影响，所以「工作区」与「课题台账」两条路径不会互相污染。
            _asm_fact: dict = {}
            messages = initial_messages(contract, Path.cwd(), kb_only=kb_only,
                                        topic_slug=_sess_topic,
                                        advisor_slug=_sess_advisor,
                                        caps=getattr(self, "caps", None),
                                        assembly_out=_asm_fact)
            # 装配事实只在**开场**发一次 —— 这就是「整场冻结」的可见证据。
            # baseline 的 saved=0 / complete=False ⇒ 一个事件都不发，
            # 旧事件流一字不变；续跑路径的消息栈是接回来的，也不重发。
            # ★ 2026-11-06：写作前技能简报也要发事件。paper-writing 不声明
            #   SYSTEM 裁剪 ⇒ saved/complete/degraded 全假，原先这条不发，
            #   于是「简报注入了哪些技能、缺了哪些」只躺在 assembly_out 里，
            #   线上事件流一个字都看不到 —— 配置在场冒充已生效的同族沉默。
            if (_asm_fact.get("saved") or _asm_fact.get("complete")
                    or _asm_fact.get("degraded")
                    or _asm_fact.get("stage_brief")):
                emit("system_assembled", dict(_asm_fact))
            # `recall=False` 是**干净对照开关**：真任务 A/B 里关掉历史参考，
            # 不然上一场的工具名会被注入本场上下文，对照结论不成立（R4/R5）。
            if self.memory and recall:
                hist = self.memory.inject_text(goal)
                if hist:
                    messages.append({"role": "user", "content": hist})
                    emit("memory_recall", {"chars": len(hist)})
            else:
                emit("memory_recall", {"chars": 0, "disabled": True})
        # ── 按领域按需挂载（理由见 `mounting_decision`）──────────
        # 默认关掉裁剪只有一个开关：`loop.domain_mount=false`。
        # 留这个开关是**诚实**的一部分：一个悄悄生效的性能改动，
        # 出问题时没有人能一键退回去。
        _mount = {"allow": None, "domains": [], "reason": "裁剪被配置关掉（loop.domain_mount=false）"}
        if getattr(cfg.loop, "domain_mount", True):
            _dom = {n: (self.registry.get(n).domain if self.registry.get(n) else "")
                    for n in self.registry.names()}
            # ★ kb_only（导师开关）必须进挂载判据：开关承诺了"基于文献库回答"，
            #   挂载就得把 kb/topic 域给足 —— 否则前端那个开关是装饰（W25 不变量）。
            _mount = mounting_decision(contract, list(_dom), lambda n: _dom.get(n, ""),
                                       kb_only=kb_only,
                                       # ★ 与 `initial_messages` 读**同一个来源**：
                                       #   本轮会话身份优先；没有就用 `current()` ——
                                       #   因为开场注入用的正是这个兜底。两处读同一个
                                       #   东西，才不会出现"提示词说在课题里、工具却在外面"。
                                       has_topic=bool(_sess_topic or topic.current()),
                                       caps=getattr(self, "caps", None))
        # ★ E1「可刷新接口，但不把 schemas 整段移进循环」：baseline 的
        #   `refresh="once"` 每步返回同一个缓存对象，零额外开销、字符数不变；
        #   `per_stage` 才按档重算。整段替换会污染事件流顺序，这里只换取值来源。
        _caps2 = getattr(self, "caps", None)
        _mc2 = _caps2.inst("mount") if _caps2 is not None else None
        # ★ 必留工具面由**本次挂载的能力**贡献（检索插件保住整条检索链）。
        #   baseline 没有贡献 ⇒ 空元组 ⇒ 与旧版逐字等价。用 getattr 降级：
        #   续跑 / 测试夹具可能塞轻量替身，读不到就当没有，不把任务打崩。
        _ess = (list(_caps2.essential_tools())
                if _caps2 is not None and hasattr(_caps2, "essential_tools")
                else [])
        schemas = (_mc2.schemas_for(self.registry, _mount["allow"], 0, "open",
                                    essential=_ess)
                   if _mc2 is not None
                   else self.registry.openai_schemas(_mount["allow"]))
        # ★ 工具可见性硬预算：预设写了 `schema_chars_budget` 才裁（default=0 不限），
        #   裁掉的**每一个**工具都要报出来 —— 悄悄缩小模型的能力边界，出了事
        #   没人能从事件流里看出来。开场算一次、整场冻结，保住前缀缓存稳定前缀。
        _bf = getattr(_mc2, "budget_report", None) if _mc2 is not None else None
        if _bf:
            emit("tools_budget_applied", dict(_bf))
        if _mc2 is not None and _mc2.cfg["refresh"] != "once":
            emit("schema_refresh",
                 {"refresh": _mc2.cfg["refresh"],
                  "schema_chars": len(json.dumps(schemas, ensure_ascii=False, default=str))})
        if _mount["allow"] is not None:
            emit("tools_mounted", {
                "allow": len(_mount["allow"]), "total": len(self.registry),
                "domains": _mount["domains"], "reason": _mount["reason"],
                "schema_chars": len(json.dumps(schemas, ensure_ascii=False, default=str)),
            })
        # 稳定段的体量：工具 schema（外加每轮原样重发的 SYSTEM）。
        # 这是**前缀缓存最值钱的对象** —— 一个装了 20+ 工具的内核，
        # 每次调用都要重发这坨 schema。量出它，才能回答"网关到底有没有在缓存"
        # （`cache_ratio` 恒为 0 就是没有，那是纯浪费，不是"本来就这样"）。
        tools_schema_chars = len(json.dumps(schemas, ensure_ascii=False, default=str))
        warn_chars = int(getattr(cfg.loop, "context_warn_chars", 0) or 0)
        warned_context = False
        # ── 上下文经济与预算闸的状态 ────────────────────────────
        # `compact_target=0` 表示这项关着（与 `context_warn_chars` 同一个约定）。
        compact_target = int(getattr(cfg.loop, "compact_target_chars", 0) or 0)
        compact_keep_live = int(getattr(cfg.loop, "compact_keep_live", 6) or 0)
        _caps3 = getattr(self, "caps", None)
        if _caps3 is not None and callable(getattr(_caps3, "effective_compact_target", None)):
            compact_target = _caps3.effective_compact_target(compact_target)
        else:
            _cc3 = _caps3.inst("context") if _caps3 is not None else None
            if _cc3 is not None:
                compact_target = int(_cc3.cfg["target_chars"])
        if _caps3 is not None and callable(getattr(_caps3, "effective_compact_keep_live", None)):
            compact_keep_live = _caps3.effective_compact_keep_live(compact_keep_live)
        # 最后一次出清的**事实**。写进快照是给续跑者看的：它有权知道自己接手的
        # 是不是半份历史 —— 不写，就是本项目最反感的那种静默。
        # 续跑时**接上**上一次的事实，而不是从零开始：快照里那份 `compacted`
        # 说的是"你接手的这个栈已经脱过水"。丢掉它就等于让续跑者以为拿到全量历史。
        # 顺带把计数也接上 —— 否则"每场最多出清 1 次"这条验收可以靠续跑绕过去。
        _inherited = dict((resume or {}).get("compacted") or {})
        compact_log: dict[str, Any] = dict(_inherited)
        # 同一个 dict 对象挂到实例上，让 `_run_serial` 收尾时能取到（含崩溃路径）。
        self._compact_log = compact_log
        # 迟滞线：出清过一次就把触发线抬到这里，否则"压不到目标以下"会演变成
        # **每步都压** —— 而每压一次要付一次前缀缓存 miss。0 = 本场还没压过。
        next_compact_at = 0
        compacted_this_task = int(_inherited.get("n") or 0)
        prompt_tokens_last = 0         # 网关**实测**回的上一步 prompt token
        budget_max_prompt = int(getattr(cfg.loop, "budget_max_prompt_tokens", 0) or 0)
        budget_hint_steps = int(getattr(cfg.loop, "budget_hint_steps", 0) or 0)
        hinted_wrapup = False          # 收尾提示每场最多一次
        hinted_pressure = False        # token 压力提示每场最多一次
        warned_degraded = False        # "闸在降级"这句自言自语，每场最多说一次
        total = Usage()
        self._run_usage = total          # 别名跟着走（见 `run()` 里的说明）
        empty_rounds = 0
        remind_every = 8
        gate_rejects = 0
        asked_limit = False
        # 2026-11-10：长综述「一次请求」兜底 —— 步数将尽时自动续一段，
        # 不弹窗等人。硬上限写在 shape.py，绝不无限续；如实发事件可复核。
        auto_extends = 0
        # 过程简报的缺口签名：只在缺口集合变化 / 预算告急时展开细项。
        _readiness_sig = ""
        # 预算里程碑：**按步号**，不是按剩余量。2026-11-10 第一轮 D 场实测
        # 第 12 步仍是 `write_file=0 / kb_read=13` —— 若按「还剩 30/20/10 步」
        # 触发，第 40 步才第一次开口，前 30 步照样全花在读库上（C 场病根）。
        # 第 1 步就给一次总预算与必做项，之后每 10 步一次；自动续步后
        # `step` 单调递增，里程碑不会像 `left` 那样被续步冲掉。
        _milestone_every = 10
        _hinted_marks: set[int] = set()
        # 已问过的打转点。键必须是**指纹**（工具名 + 参数），不是 `seq` ——
        # `seq` 是"这一次调用"的身份，每调一次就换一个；拿它当键，守卫恒真，
        # 于是每次重复都问一遍（实测被问了 4 次）。
        # 「同一个坑」的身份 = 同一工具同一参数 = `fingerprint`，与打转检测同源。
        asked_stalls: set[str] = set()
        _fig_status: dict = {}          # 交付前只读登记结论（advisory）

        def ask(kind: str, data: dict) -> dict | None:
            """把问题抛给人并**阻塞等**回答。没人应答一律返回 None（降级为原行为）。

            为什么"发射事件"由内核做、而不是回调自己发：
            `intervene` 是业务回调，界面该看到的是**内核承认的介入点**。
            回调只管取答案 —— 否则 CLI / Web / 测试各广播一遍，必然漂移（H 型）。
            """
            if intervene is None:
                return None
            # 无人值守（自动化/批量验证）：不弹窗白等 300 s，按原路继续。
            # `_ack(None)` 本来就按「继续」处理，所以这条只是省掉空等，
            # **不改变任何判定**，也不是把打转/打回放过去。
            if unattended:
                emit("intervene_note", {
                    "kind": kind, "outcome": "skipped_unattended",
                    "text": "无人值守：不弹窗等人，按原路继续"})
                return None
            payload = {"kind": kind, "step": step, **data}
            emit("intervene_request", payload)
            t0 = time.time()
            try:
                ans = intervene(kind, payload)
            except Exception as e:
                emit("intervene_note", {
                    "kind": kind, "outcome": "error",
                    "text": f"介入通道出错，按原路继续：{type(e).__name__}: {e}"})
                return None
            if ans is None:
                emit("intervene_note", {
                    "kind": kind, "outcome": "timeout",
                    "waited": round(time.time() - t0, 1),
                    "text": "没有人应答，按原路继续"})
                return None
            return ans

        def _steer(text: str, why: str) -> int:
            """把用户的补充**插进当前消息栈**再继续。

            这是"从断点续跑"的全部秘密：断点本来就在 `messages` 里，
            所以只要追加一条 user 消息，下一轮模型就带着**完整前情 + 新约束**接着干。
            反过来，若把它做成"结束本轮、开新任务"，前情全丢 ⇒ 那才是整轮重来。
            """
            t = (text or "").strip()
            if not t:
                return 0
            messages.append({"role": "user", "content":
                             f"【运行中补充（因{why}暂停问你）—— 就地按它继续，不必重来】\n{t}"})
            return len(t)

        def _ack(kind: str, ans: dict | None) -> bool:
            """处理一次介入答复。True = 就地继续；False = 用户叫停。

            `ans is None`（没人答/超时/通道坏）**一律当"继续"** ——
            降级路径必须是原行为，不许把可用性问题升级成可用性事故。
            """
            if not ans:
                return True
            act = str(ans.get("action") or "continue").strip()
            if act == "stop":
                return False
            if act == "steer":
                n = _steer(ans.get("text") or "", kind)
                emit("intervene_applied", {"kind": kind, "action": "steer",
                                           "chars": n, "step": step})
            return True

        def _stopped(kind: str) -> RunResult:
            _reason = f"用户叫停（{kind} 时选择停止）—— 是未完成，不是失败"
            return RunResult(
                success=False,
                reason=_reason,
                steps=step, usage=total, trace=list(self.registry.trace),
                final=interrupted_final(_reason, step), contract=contract)

        def _snapshot(n: int) -> None:
            """把对话快照**原子写盘** —— 这是"跨进程续跑"的全部本钱。

            为什么必须原子：写到一半被 kill 会留下半个 JSON，而续跑读到坏文件
            比读不到更糟（要么静默丢掉半截对话，要么直接崩）。所以先写 `.tmp`
            再 `os.replace` —— 同一文件系统上的 replace 是原子的。

            为什么目录是实例属性而不是 `Path.cwd()`：`_loop` 也直接被单测调用，
            那时 cwd 是项目根 —— 往那儿写快照会污染仓库。没有 `_snap_dir` 就不落盘。
            """
            d = getattr(self, "_snap_dir", None)
            if not d:
                return
            try:
                tmp = d / "conversation.json.tmp"
                tmp.write_text(json.dumps({
                    "goal": goal, "step": n,
                    "contract": contract.to_dict() if contract else None,
                    "messages": messages, "t": time.time(),
                    # 出清过就必须留痕：续跑者看到的消息栈是**脱水后的**。
                    # 不写这一项，接着跑的人（和下一份归档的评测）会以为那是全量历史
                    # —— 那是把"没看全"渲染成"看过了"（M 型）。
                    "compacted": dict(compact_log),
                    # ★ W28：这一场的**课题身份**也进快照。续跑/追问接回来时，
                    #   "这轮在哪个课题里"必须从**快照**取，不能现场读 `CURRENT`
                    #   —— 后者是跨会话残留，中途被切走就换了课题（`topic_write`
                    #   那段实测事故的同一条根因）。空串 = 本场没有课题。
                    "topic": str(_sess_topic or ""),
                    "advisor": str(_sess_advisor or advisor_slug or ""),
                    "preset": str(getattr(getattr(self, "caps", None), "preset_id", "") or ""),
                    "mode": str(mode or ""),
                }, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, d / "conversation.json")
            except Exception:
                pass          # 快照是附赠品：写不了也不许打断任务

        # ── 为什么不是 `for step in range(1, max_steps + 1)` ──
        # 因为 `range` 在循环开始那一刻就把上界**固化**了：人在中途说"再给 20 步"
        # 也改不动它。而"步数耗尽"恰恰是三个介入点之一 ——
        # 用 for 就等于声明"这个介入点不可能实现"。
        # 换成 while 后 `max_steps` 始终是可协商的活值。
        step = 0
        while True:
            if step >= max_steps:
                # ── 2026-11-10：长综述「一次请求」兜底 ──────────────
                # 实测两场默认请求都在第 40/70 步弹窗等人点「继续」，没人
                # 应答就白耗 300 s 再 `success=false`，盘上却已有可用初稿。
                # 一次请求要能自己走完：步数将尽时自动续一段并如实记账，
                # **不弹窗、不等待**；硬上限 MAX_AUTO_EXTENDS 防失控。
                # 短任务 / 问答（kind != long_review）完全沿用下面的原路径。
                # 11-11 真任务 R2/R3：writing 短文同样白等 300 s 后失败。
                # 资格判定收在 shape.should_auto_extend() 纯函数里（可判据测），
                # 两道闸是「写过正文」+「还没试过交付」，不是无条件加步数。
                _tnames = [str(getattr(c, "name", "")
                               or (c.get("name", "") if isinstance(c, dict) else ""))
                           for c in (self.registry.trace or [])]
                if should_auto_extend(_shape.kind, _tnames, auto_extends):
                    auto_extends += 1
                    max_steps += AUTO_EXTEND_STEPS
                    emit("step_limit_auto_extend", {
                        "step": step, "add": AUTO_EXTEND_STEPS,
                        "max_steps": max_steps, "times": auto_extends,
                        "max_times": MAX_AUTO_EXTENDS,
                        "shape": _shape.kind,
                        "reason": ("无人值守：已写过正文且尚未交付，不弹窗等人，"
                                   "自动续一段并如实记账")})
                    # 续完之后 left 重新变大，收尾提示要允许再发一次。
                    hinted_wrapup = False
                    continue
                # 步数耗尽**不等于**任务没救：退出前给人一次机会。
                # 长综述已自动续到上限 ⇒ 不再弹窗（无人值守白等是实测坑），
                # 事件里已有两次续步记录，收尾会如实列出盘上产物。
                _auto_exhausted = (_shape.kind in ("long_review", "writing")
                                   and MAX_AUTO_EXTENDS > 0
                                   and auto_extends >= MAX_AUTO_EXTENDS)
                if not asked_limit and not _auto_exhausted:
                    asked_limit = True
                    ans = ask("step_limit", {"max_steps": max_steps})
                    act = str((ans or {}).get("action") or "").strip()
                    if act == "extend":
                        add = int(ans.get("steps") or 20)
                        add = max(1, min(add, 200))     # 防手滑填个 100000
                        max_steps += add
                        emit("intervene_applied", {
                            "kind": "step_limit", "action": "extend",
                            "add": add, "max_steps": max_steps})
                        continue
                    if act == "stop":
                        return _stopped("步数耗尽")
                    if act == "steer":
                        # "补一句"在步数耗尽时**必须附带步数**：
                        # 只收下意见却不给执行机会 = 收下意见、然后照样结束 ——
                        # 那是个假动作（M 型：把"没采纳"渲染成"采纳了"）。
                        _steer(ans.get("text") or "", "步数耗尽")
                        max_steps += 10
                        emit("intervene_applied", {
                            "kind": "step_limit", "action": "steer",
                            "max_steps": max_steps})
                        continue
                break
            step += 1
            self._last_step = step      # 崩溃时用它如实报"跑到第几步"
            _snapshot(step)             # 每步落盘一次：崩了最多丢一步
            # 取消：在每步开头检查（不打断正在跑的那次工具调用）
            if cancel is not None and cancel.is_set():
                emit("cancelled", {"step": step})
                _reason = "任务被用户取消（未完成）"
                return RunResult(
                    success=False, reason=_reason,
                    steps=step - 1, usage=total, trace=list(self.registry.trace),
                    final=interrupted_final(_reason, step - 1), contract=contract,
                )
            messages_chars = _messages_chars(messages)
            if warn_chars and messages_chars > warn_chars and not warned_context:
                # **一次任务最多告警一次**：每步都叫会把人训练成无视告警，
                # 那比不告警更糟（"误报比漏报更伤"）。
                warned_context = True
                emit("context_warn", {"chars": messages_chars, "limit": warn_chars,
                                      "step": step})

            def _compact(why: str, target: int, *,
                         keep_live_steps: int | None = None) -> dict:
                """出清一次历史，并把这件事**记成事实**。返回那次的统计。

                为什么是个内嵌函数而不是三处 inline：三个触发点（超阈值、token 压力、
                网关真报溢出）必须走同一条路、发同一种事件，否则三处的口径迟早漂移。

                ★ `compact_log` **只记真省到字节的那一次**。以前是无条件写，于是
                  "只空跑过"的一场任务会留下记录却没有对应事件 —— `ctx_econ` 的跨源
                  核对（R6）正好把这形状判成红，一个假警报。
                """
                nonlocal messages, messages_chars, compacted_this_task
                before = _messages_chars(messages)
                # ★ 必须**按名**传（2026-10-15 修）：`context.apply` 的形参是
                #   `(messages, trace, target_chars, keep_live, keep_live_steps, only_levels)`。
                #   这里原来是位置传 `compact_keep_live`（一个**消息条数**，默认 6）——
                #   加了 `keep_live_steps` 之后它落进了**第五个**位置，
                #   于是"保留 6 条"被读成"保留 6 步"，
                #   保护窗口一下放宽 6 倍，出清几乎压不动
                #   （实测：溢出重试那条 25,572 → 25,572，一条没省）。
                #   位置传参遇上形参表变长就是这样安静地错位的 —— 按名传，让它错不了。
                if keep_live_steps is None:
                    # 超阈值 / token 压力两个触发点：逐字沿用原来的调用，
                    # 不传 keep_live_steps ⇒ apply 用默认 KEEP_LIVE_STEPS 步数保护。
                    new, stat = context.apply(messages, self.registry.trace, target,
                                              keep_live=compact_keep_live)
                else:
                    # 溢出兜底专用（why="overflow"）：把步数保护**放开到 0**。
                    # plan 里 `_step_cutoff` 返回 len(messages) ⇒ 可动区覆盖全栈，
                    # 2026-10-15 实测那 25,572 字符才压得动；否则 eff=0、整场作废。
                    # ★ 仍按名传 keep_live=compact_keep_live：条数下限不许丢。
                    #   为什么不改调 context.force_brief：它不收 keep_live，
                    #   会退回默认 KEEP_LIVE，且 _compact 这套记账/emit 得复制一遍 ——
                    #   改动面更大且违反「条数下限仍生效」。只加一个按名形参，最小且等价。
                    new, stat = context.apply(messages, self.registry.trace, target,
                                              keep_live=compact_keep_live,
                                              keep_live_steps=keep_live_steps)
                compacted_this_task += 1
                stat.update({"step": step, "why": why, "n": compacted_this_task,
                             "target": int(target), "before_chars": before})
                if stat["freed_chars"] <= 0:
                    # 一次没省下来的出清＝白付一次前缀缓存 miss。**不发假的成功事件**，
                    # 也不写进 compact_log，而是如实说明为什么没动：
                    # 可动区已被 keep_live 保护到底了。
                    emit("context_compact_skipped", stat)
                    return stat
                messages = new
                messages_chars = _messages_chars(messages)
                stat["after_chars"] = messages_chars
                compact_log.clear()
                compact_log.update(stat)
                emit("context_compact", stat)
                return stat

            # ── 出手，而不只是看见 ────────────────────────────
            # `context_warn` 那 20 行此前是**唯一**的响应：发一条事件、什么都不做，
            # 于是历史一路涨到网关回 400、整场判「执行中断」。
            # ★ 迟滞：出清过一次之后，要把触发线抬到"当时体量 + 半个阈值"以上才再动手。
            #   否则只要压不到 target 以下（`keep_live` 护着最近那一批，长任务里这是
            #   常态），**每一步都会再压一次** —— 每压一次付一次前缀缓存 miss，
            #   那是把急刹车当成例行打扫，成本上明确是亏的（实测命中率中位 94.4%）。
            if compact_target and messages_chars > max(compact_target, next_compact_at):
                _st = _compact("over_target", compact_target)
                # **一次决定，两个出口共用**：不管这次省没省到都要抬起触发线。
                # 省不到说明"能动的都动过了"，留着不抬就会每步空跑一遍、每步刷一条
                # `context_compact_skipped` —— 那正是"误报会把人训练成无视告警"那条老账。
                next_compact_at = (int(_st.get("after_chars") or messages_chars)
                                   + max(compact_target // 2, 20_000))

            # token 压力：用网关**实测**回的 input_tokens，不做字数→token 的换算。
            # ★ `step > 1` 是必需的：第 1 步还没有任何一次 chat 发生过，
            #   `prompt_tokens_last` 恒为 0 —— 那不是"网关不回这个字段"，是"还没轮到"。
            #   不挡掉的话每场任务开头都会冒一条假的"闸在降级"（实测就是这么来的）。
            if budget_max_prompt and step > 1:
                if prompt_tokens_last:
                    share = prompt_tokens_last / budget_max_prompt
                    degraded = ""
                else:
                    # 降级口径：拿字符数当分母比"什么都不做"诚实，但必须自报降级。
                    # ★ 一次任务最多说一次：实测漏过这个守卫，于是每一步都发一条
                    #   "闸在降级"的自言自语 —— 噪声会把人训练成无视提示
                    #   （与"误报比漏报更伤"同一条纪律）。
                    share = messages_chars / budget_max_prompt
                    degraded = "no_prompt_tokens"
                    if not warned_degraded:
                        warned_degraded = True
                        emit("budget_note", {
                            "step": step, "degraded": degraded,
                            "text": "网关未回 input_tokens，token 闸降级为字符闸"})
                if share >= cfg.loop.budget_force_ratio:
                    _st = _compact("token_pressure", int(compact_target or 0))
                    # 迟滞线照抬，**三个出口共用一条规则**：不抬的话这一级会在
                    # 每一步都重跑一遍出清 —— 那正是"把急刹车当成例行打扫"。
                    next_compact_at = (int(_st.get("after_chars") or messages_chars)
                                       + max(compact_target // 2, 20_000))
                elif share >= cfg.loop.budget_warn_ratio and not hinted_pressure:
                    hinted_pressure = True
                    messages.append({"role": "user", "content": PRESSURE})
                    emit("budget_note", {"step": step, "share": round(share, 4),
                                         "prompt_tokens": prompt_tokens_last,
                                         "degraded": degraded, "kind": "pressure"})
            left = max_steps - step
            # ── 2026-11-10：预算里程碑（不依赖写文件）────────────────
            # 实测 C 场前 30 步全在逐篇 kb_read，模型既不知道还剩几步，
            # 也不知道图面体检一次都没跑。只在长综述上启用，短任务零影响。
            if (_shape.kind == "long_review"
                    and (step == 1 or step % _milestone_every == 0)
                    and step not in _hinted_marks):
                _hinted_marks.add(step)
                try:
                    _rm = _build_readiness(
                        trace=list(self.registry.trace), contract=contract,
                        step=step, max_steps=max_steps, text="",
                        prev_signature=_readiness_sig)
                    messages.append({"role": "user", "content": str(_rm.get("text") or "")})
                    emit("readiness_milestone", _readiness_event(_rm))
                    _readiness_sig = _rm.get("signature") or ""
                except Exception as e:                     # noqa: BLE001
                    emit("readiness_error", {"error": f"{type(e).__name__}: {e}"})
            if budget_hint_steps and 0 < left <= budget_hint_steps and not hinted_wrapup:
                hinted_wrapup = True
                messages.append({"role": "user", "content": WRAPUP})
                emit("wrapup_hint", {"step": step, "steps_left": left,
                                     "max_steps": max_steps})
            # 模型调用前先广播，消灭"屏幕上一片死寂"。
            # 网关实测会间歇性长挂起（1.5s 与 247s 交替），没有这个事件时
            # 用户/前端在整段等待里看不到任何东西，会以为它挂了。
            emit("llm_call", {"n": step, "tools_available": len(schemas),
                              "messages": len(messages),
                              "messages_chars": messages_chars,
                              "tools_schema_chars": tools_schema_chars})
            meta: dict = {}
            t_chat = time.time()
            try:
                msg, usage, reasoning = self.llm.chat(messages, tools=schemas, meta=meta)
            except ContextOverflow as e:
                # ── 溢出兜底：此前这是全仓唯一的硬崩路径 ──────────────
                # 400 → LLMError → 主循环不接 → `_run_serial` 兜成「执行中断」，
                # 跑了 N 步的进度当场作废。现在先走满出清阶梯重试**一次**；
                # 仍不行就如实收场，并保住快照（它是可续跑的，别谎称失败）。
                emit("context_overflow", {"step": step, "code": e.code,
                                           "body": str(e.body)[:600]})
                _st = _compact("overflow", 0, keep_live_steps=0)
                # 迟滞线照抬（三个出口共用一条规则）。不抬的话：重试成功后下一步
                # `messages_chars` 依旧 > 阈值 ⇒ 又压一次 ⇒ 每一步付一次前缀缓存 miss，
                # 那是把急刹车当成例行打扫 —— 实测归档的命中率中位是 94.4%。
                next_compact_at = (int(_st.get("after_chars") or messages_chars)
                                   + max(compact_target // 2, 20_000))
                try:
                    msg, usage, reasoning = self.llm.chat(messages, tools=schemas,
                                                          meta=meta)
                    emit("context_overflow_recovered", {"step": step})
                except LLMError as e2:
                    _snapshot(step)
                    emit("context_overflow_failed", {"step": step,
                                                     "error": f"{type(e2).__name__}: {e2}"})
                    _reason = (f"上下文溢出，出清后重试仍不可用（{e2}）。"
                               f"**这不是失败**：进度已落快照，可续跑。"
                               f"上游原文：{str(e.body)[:200]}")
                    return RunResult(
                        success=False, reason=_reason,
                        steps=step, usage=total, trace=list(self.registry.trace),
                        final=interrupted_final(_reason, step), contract=contract)
            t_done = time.time()
            # 耗时只算一次，给两个消费者用（事件 + span）——
            # 各算一遍就是"两处真相"，迟早会对不上。
            chat_ms = round((t_done - t_chat) * 1000, 3)
            total = total + usage
            self._run_usage = total      # 唯一的**累加**写点，别名跟着它走
            prompt_tokens_last = int(usage.input_tokens or 0)
            # 调用**结束**了 —— 原实现到这里就断了，模型耗时因此完全不可见。
            # finish_reason 尤其重要：它是唯一能区分"模型说完了"与
            # "被 max_tokens 截断"的字段，缺了它这种静默失败永远查不出来。
            emit("llm_done", {"n": step, "ms": chat_ms, "usage": usage.to_dict(),
                              "cache_ratio": usage.cache_ratio,
                              "finish_reason": meta.get("finish_reason", ""),
                              "response_model": meta.get("response_model", ""),
                              "attempts": meta.get("attempts", 1)})
            if self.telemetry is not None:
                self.telemetry.record_chat(
                    step, t_chat, t_done, usage,
                    finish_reason=meta.get("finish_reason", ""),
                    messages_chars=messages_chars,
                    tools_schema_chars=tools_schema_chars)
            content = (msg.get("content") or "").strip()
            # 思维链单独发一条：界面拿它渲染"思考"折叠块。
            # 它是模型的自述，**不是证据**——证据只认真实的工具调用（gate.py 把关）。
            if reasoning:
                emit("thinking", {"n": step, "text": reasoning[:4000],
                                  "text_chars": len(reasoning), "truncated": len(reasoning) > 4000,
                                  "tokens": usage.reasoning_tokens})
            emit("step", {"n": step, "usage": usage.to_dict(),
                          "content": content[:4000], "tools_available": len(schemas)})

            tool_calls = msg.get("tool_calls") or []

            # ── 没有工具调用：不是完成就是空转 ──
            if not tool_calls:
                messages.append({"role": "assistant", "content": content})
                empty_rounds += 1
                if empty_rounds >= 2:
                    messages.append({"role": "user", "content": NUDGE})
                    emit("nudge", {"after_rounds": empty_rounds})
                    empty_rounds = 0
                continue

            empty_rounds = 0
            messages.append(msg)  # 必须原样保留（含 tool_calls），否则下一轮 API 报错

            delivered: tuple[CompletionClaim, Verdict] | None = None
            blocked: dict | None = None

            for tc in tool_calls:
                name, args, tcid, parse_err = _unpack(tc)
                # ★ 置信度门控回退：模型点名了本轮未暴露、但注册表里真实
                #   存在的工具 ⇒ 放开一次、记事件，之后整场保持放开。
                #   这不是「每步变形」：只在真的点名时扩张一次，前缀缓存
                #   只 miss 一次。真查无此工具的名字仍由 registry 如实拒绝。
                _expanded = scope_fallback(schemas, self.registry, name)
                if _expanded is not None and hasattr(schemas, "append"):
                    schemas.append(_expanded)
                    emit("tool_scope_expanded", {
                        "tool": name, "tools_available": len(schemas),
                        "frozen": True,
                        "reason": "模型点名了本轮未暴露、但注册表里真实存在的工具",
                    })

                if parse_err:
                    # ★ 参数没能解析 ⇒ **这一次调用整体作废**，`deliver_task` /
                    #   `report_blocker` 也不例外。两个理由：
                    #   ① 一致性：四个分支里只有这一个能让"参数坏了"**穿透**到
                    #      后面 —— 空 args 交给 `CompletionClaim.from_args()`
                    #      会变成一份"没有证据的交付声明"，报回的是
                    #      「证据不足」，而那又是一句**假话**（模型可能写了证据，
                    #      只是 JSON 断了）。口径不许在各分支各说一套（H 型）。
                    #   ② 安全：不要求必填参数的工具会**真的拿 `{}` 跑起来**。
                    #   照样过一遍 `registry.call`，是为了让它**进台账**。
                    emit("tool_start", {"seq": self.registry.next_seq, "name": name,
                                        "args": args})
                    call = self.registry.call(name, args, parse_error=parse_err,
                                              tcid=tcid)
                    emit("tool", call.to_dict())
                    messages.append({
                        "role": "tool", "tool_call_id": tcid,
                        "content": _observation(call, cfg.loop.max_observation_chars),
                    })
                    continue

                if name == "deliver_task":
                    try:
                        args = _ensure_visuals_for_delivery(args, contract, emit)
                    except Exception as e:                 # noqa: BLE001
                        emit("visual_autofix_error",
                             {"error": f"{type(e).__name__}: {e}"})
                    _delivery_target = _dp.resolve_delivery_target(args)
                    _orig_disk = ""
                    if _delivery_target is not None and _delivery_target.is_file():
                        try:
                            _orig_disk = _delivery_target.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            _orig_disk = ""
                    try:
                        _fig_status = _reconcile_before_delivery(emit, target=_delivery_target)
                    except Exception as e:                 # noqa: BLE001
                        _fig_status = {"ok": False, "status": "error",
                                       "gaps": [{"code": "GUARD_DELIVERY_FAILED",
                                                 "detail": f"{type(e).__name__}: {e}"}],
                                       "advisory": True}
                        emit("figure_reconcile_error", dict(_fig_status))
                    try:
                        _lit_status = _litpool_before_delivery(emit)
                    except Exception as e:                 # noqa: BLE001
                        _lit_status = {"ok": False, "status": "error",
                                       "gaps": [{"code": "LITPOOL_GUARD_FAILED",
                                                 "detail": f"{type(e).__name__}: {e}"}],
                                       "advisory": True}
                        emit("litpool_error", dict(_lit_status))
                    try:
                        _normalize_prose_before_delivery(emit, target=_delivery_target)
                    except Exception as e:                 # noqa: BLE001
                        emit("manuscript_normalize_error",
                             {"error": f"{type(e).__name__}: {e}"})
                    if _delivery_target is not None and _delivery_target.is_file():
                        try:
                            _cur_disk = _delivery_target.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            _cur_disk = ""
                        _raw_dt = str(args.get("draft_text") or "").strip()
                        if (_cur_disk and _cur_disk != _orig_disk and _raw_dt
                                and _dp._norm_compare(_raw_dt) in _dp._norm_compare(_orig_disk)):
                            args = dict(args)
                            args["draft_text"] = _cur_disk
                    claim = CompletionClaim.from_args(args)
                    verdict = self.gate.check(claim, contract)
                    emit("deliver_attempt", {
                        "passed": verdict.passed,
                        "reasons": verdict.reasons,
                        "warnings": verdict.warnings,
                        "writing": verdict.writing,
                        "lint": verdict.lint,
                        "summary": claim.summary,
                        "evidence": [e.__dict__ for e in claim.evidence],
                        "figure_registry": _fig_status,
                        "litpool": _lit_status,
                    })
                    messages.append({"role": "tool", "tool_call_id": tcid,
                                     "content": verdict.feedback})
                    if verdict.passed and delivered is None:
                        delivered = (claim, verdict)
                    elif not verdict.passed:
                        # **第 1 次打回不问人** —— 留给模型自己修。
                        # 实测里同一任务会被连打回 3 次，第 2 次才值得问：
                        # 第一次就问等于"它还没试过就先怀疑它"，只会制造噪音，
                        # 而噪音训练人无视提示（"误报比漏报更伤"）。
                        gate_rejects += 1
                        if gate_rejects >= 2:
                            ans = ask("gate_reject", {
                                "rejects": gate_rejects,
                                "reasons": list(verdict.reasons or []),
                                "summary": claim.summary,
                            })
                            if not _ack("gate_reject", ans):
                                return _stopped("交付被证据门反复打回")

                elif name == "report_blocker":
                    blocked = args
                    emit("blocker", args)
                    messages.append({"role": "tool", "tool_call_id": tcid,
                                     "content": "已记录该阻塞报告，任务将以「阻塞」结束。"})

                else:
                    # 先广播"开始"，再真正调用 —— 前端才有**真实的运行态**。
                    # 原实现只在调用**结束之后**发一次 tool 事件，于是前端
                    # data-st="run" 的扫光动画成了永不触发的死代码，
                    # 用户也看不到"它正在检索"这个过程，只看到结果逐条冒出来。
                    emit("tool_start", {"seq": self.registry.next_seq, "name": name,
                                        "args": args})
                    call = self.registry.call(name, args, tcid=tcid)
                    emit("tool", call.to_dict())
                    obs = _observation(call, cfg.loop.max_observation_chars)
                    # ── 文献池所有权层（2026-11-12）──────────────────
                    # R1/R4/R8/R9 实证：靠模型主动调工具，默认路径 = 0 次。
                    # 检索/入库回包一到统一收口就自动入池；解析不了也 emit
                    # 明说，不静默跳过、不造空池冒充核对过。
                    try:
                        _lp = _litpool_ingest(name, args, call, emit)
                        _lp_line = _litpool_guard.guard_line(_lp)
                        if _lp_line:
                            obs += "\n\n" + _lp_line
                    except Exception as e:                 # noqa: BLE001
                        emit("litpool_error",
                             {"error": f"{type(e).__name__}: {e}"})
                    if self.registry.repeated_call(call, cfg.loop.max_identical_calls):
                        n_same = self.registry.identical_calls(call)
                        obs += (
                            f"\n\n⚠️ 收敛告警：同一工具+同一参数已重复 "
                            f"{n_same} 次，判定为原地打转。\n"
                            "请立刻换策略；若确实无法推进，调用 report_blocker 说明卡点。"
                        )
                        emit("stall", {"seq": call.seq, "tool": call.name,
                                       "repeat": n_same,
                                       "ask_at": cfg.loop.max_identical_calls
                                                 + STALL_ASK_EXTRA})
                        # 两段式：先只在工具回包里告警（模型还有机会自己换策略），
                        # 累计到 ask_at 次才问人 —— R3 的「3 次查询白等 300 s」
                        # 到此为止，而真打转（6 次）照样拦。
                        # **同一个 fingerprint 只问一次** —— 否则反复问是骚扰。
                        if (n_same >= cfg.loop.max_identical_calls + STALL_ASK_EXTRA
                                and call.fingerprint not in asked_stalls):
                            asked_stalls.add(call.fingerprint)
                            ans = ask("stall", {"tool": call.name, "seq": call.seq,
                                                "repeat": n_same})
                            if not _ack("stall", ans):
                                return _stopped("检测到原地打转")
                    # ── 2026-11-10：把「交付还差什么 + 还剩几步」前移 ──────
                    # 只在 write_file 成功后附加。纯只读、零 LLM 轮次、
                    # **绝不碰 self.gate** —— 过程里调 check() 会烧掉唯一一次
                    # 打回额度，等于把交付门放水（见 readiness.py 模块说明）。
                    # 简报是运行时工具回包，SYSTEM 一字不动、任何门都不拦。
                    if name == "write_file" and call.ok:
                        # ── 图表所有权层（2026-11-11 第二轮根治）──────────
                        # R4 实证：模型整篇 write_file 会删掉工具写的隐藏锚点，
                        # 8 次覆盖后最终稿五连读回 FAIL。提醒它「别删」没用，
                        # 正解是让工具在**写后这一刻**自己按可见内容补回图块、
                        # 重编编号。纯确定性文本整理，不改任何门、不调 gate。
                        try:
                            _gr = _reconcile_written_file(
                                str(args.get("path") or ""), emit)
                            _gl = _guard_line(_gr)
                            if _gl:
                                obs += "\n\n" + _gl
                        except Exception as e:                # noqa: BLE001
                            emit("figure_reconcile_error",
                                 {"error": f"{type(e).__name__}: {e}"})
                        # 文献池：先引导建池，再算简报 —— 顺序反了简报会读不到刚落的池。
                        try:
                            _lpw = _litpool_after_write(emit)
                            _lpwl = str(_lpw.get("line") or "")
                            if _lpwl:
                                obs += "\n\n" + _lpwl
                        except Exception as e:                # noqa: BLE001
                            emit("litpool_error",
                                 {"error": f"{type(e).__name__}: {e}"})
                        try:
                            _rd = readiness_after_write(
                                path=str(args.get("path") or ""),
                                trace=list(self.registry.trace),
                                contract=contract, step=step,
                                max_steps=max_steps,
                                caps=getattr(self, "caps", None),
                                prev_signature=_readiness_sig)
                            if _rd is not None:
                                _readiness_sig = _rd.get("signature") or ""
                                obs += "\n\n" + str(_rd.get("text") or "")
                                emit("readiness", _readiness_event(_rd))
                        except Exception as e:                 # noqa: BLE001
                            # 简报是可选层：它炸了不能把写文件的结果也弄丢。
                            emit("readiness_error",
                                 {"error": f"{type(e).__name__}: {e}"})
                    messages.append({"role": "tool", "tool_call_id": tcid, "content": obs})

            if blocked is not None:
                _reason = f"模型报告阻塞：{blocked.get('reason', '')}"
                return RunResult(
                    success=False, reason=_reason,
                    steps=step, usage=total, trace=list(self.registry.trace),
                    final=interrupted_final(_reason, step), contract=contract,
                )
            if delivered is not None:
                claim, verdict = delivered
                draft = self._draft_for_display(claim)
                return RunResult(
                    success=True, reason=verdict.feedback, steps=step, usage=total,
                    trace=list(self.registry.trace),
                    final=_compose_final(claim, draft,
                                         files=self._written_files(Path.cwd()),
                                         figure_status=_fig_status,
                                         litpool_status=_lit_status),
                    contract=contract, verdict=verdict,
                    draft=draft, lint=verdict.lint,
                )

            if step % remind_every == 0:
                messages.append({"role": "user", "content": _reminder(contract)})
                emit("remind", {"step": step})

        _reason = (f"达到最大步数 {max_steps} 仍未通过证据门"
                   f"（是未完成，不是失败——可加大步数重跑）")
        return RunResult(
            success=False, reason=_reason,
            steps=max_steps, usage=total, trace=list(self.registry.trace),
            final=interrupted_final(_reason, max_steps), contract=contract,
        )
