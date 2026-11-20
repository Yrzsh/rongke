# -*- coding: utf-8 -*-
"""交付就绪度：把「还差什么」和「还剩几步」前移到写作过程中（2026-11-10）。

**为什么有这个文件**：2026-11-09 三场真跑把同一个结构缺口量得很死 ——
交付判据（图面体检 / 50 篇文献 / 证据覆盖 / 稿面 / 大纲）**只在
`deliver_task` 那一刻第一次开口**，而步数事实只发到前端事件流，模型
全程看不见。于是模型把 70 步几乎全花在逐篇 `kb_read`，到第 67 步才
听到「一次图面体检都没跑」，那时已无步数补正 ⇒ `success=false`，
盘上却躺着一份比正式交付稿更厚的初稿。

开场 SYSTEM 里其实写了「交付前必须调 audit_figure」「图先于正文」，
但那是一次性注入，会被 70 步读库冲到上下文极远处。**问题不在提示词
写得不够，在于反馈回路的时间结构**：判据与预算都是终点量、不是过程量。

    trace + contract + 盘上稿件 ──> build_readiness() ──> 只读简报
                                                          └─> 拼进 write_file 回包

纪律（与 11-04~11-09 的既有裁决逐条对齐）：
  · **绝不调 `CompletionGate.check()`**。它不是只读函数：会消耗
    `_writing_used`（唯一一次打回额度）⇒ 真正交付时直接落进「为免无限
    拉锯本次放行」，等于**放水**；还会污染 `last_report` /
    `last_draft_source` / `_warn_seen`，并吐「完成声明未通过」的交付口吻。
    本模块只复用**只读数据源**：`writing.lint()`（经 `build_feedback`）、
    `source_alignment.sources_from_calls()` / `align()`、稿件台 `check()`。
  · **不新增/加严任何门**：`advisory=True` 恒成立，只给信息、不拦人、
    不改 `messages` 以外的任何状态。判据定下限，上限来自资产。
  · **SYSTEM 一字不动**：简报是运行时工具回包，不是提示段。
  · **三态纪律**：引文对齐 `unknown ≠ unsupported`，只进提示不进缺口。
  · **异常隔离**：任一数据源炸了只影响那一层，绝不把异常抛回工具回包。
  · **零 LLM 轮次**：纯 CPU 正则，写文件后顺带算，不多花一次模型调用。
"""
from __future__ import annotations

import re
import sys
from typing import Any

from .delivery_pipeline import (
    FIGURE_AUDIT_TOOLS,
    PROSE_SUFFIXES,
    _chapter_from_outline as _dp_chapter_from_outline,
    ensure_visuals as _dp_ensure_visuals,
    find_outline_file,
    prepare_visuals_for_delivery as _dp_prepare_visuals_for_delivery,
)

TITLE = "【交付就绪度 · 读回】"

#: 体例里**与写作阶段无关**的硬项：第一稿就该写对，早说才有用。
#: 其余（缺文献表 / 章号不连续 / 文献不足 50）是终稿完整性判据，
#: 半截稿上恒为真 —— 早早列出来只会把模型训练成无视反馈。
STAGE_FREE_FORMAT_CODES = frozenset({"ZERO_CHAPTER"})
#: 剩余步数低于这个值 ⇒ 不论稿件成熟度都全量报（该收尾了）。
URGENT_LEFT = 25


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """台账元素既可能是 `ToolCall`，也可能是 dict（归档/夹具）。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _find_caps_ext_func(filename: str, funcname: str):
    """拿到常驻能力里的函数 —— 优先下沉纯模块，再复用 `caps` 已加载的那份模块。

    1. 优先从下沉后的纯内核模块导入（`rongke.manuscript` / `rongke.visual_layout`），
       彻底摆脱 `caps_ext/` 动态装载顺序与 cwd 依赖；
    2. 未下沉或尚未落地时，按 `__file__` 反查 `sys.modules.values()` 复用已加载模块；
    3. 最后按 `rongke.caps_ext.<stem>` 兜底导入。
    """
    _pure_map = {
        "manuscript_desk.py": "manuscript",
        "visual_desk.py": "visual_layout",
    }
    pure = _pure_map.get(filename)
    if pure and __package__:
        try:
            mod = __import__(f"{__package__}.{pure}", fromlist=[funcname])
            fn = getattr(mod, funcname, None)
            if callable(fn):
                return fn
        except Exception:                                     # noqa: BLE001
            pass
    tail = "caps_ext/" + filename
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", "") or "").replace("\\", "/")
        if f.endswith(tail):
            fn = getattr(mod, funcname, None)
            if callable(fn):
                return fn
    try:
        pkg = f"{__package__}.caps_ext.{filename[:-3]}"
        mod = __import__(pkg, fromlist=[funcname])
        return getattr(mod, funcname, None)
    except Exception:                                         # noqa: BLE001
        return None


def _find_desk_check():
    """稿件台 `check()` 的薄封装（保留名字给判据与旧调用方）。"""
    return _find_caps_ext_func("manuscript_desk.py", "check")


def _safe_import():
    """软引用可选数据源：缺一个不许拖垮整份简报，**但必须留痕**。"""
    out: dict[str, Any] = {"feedback": None, "sources": None, "align": None,
                           "desk": None, "visual": None, "min_refs": 50,
                           "errors": []}
    errs: list[str] = out["errors"]

    def attempt(label, fn):
        try:
            return fn()
        except Exception as e:                             # noqa: BLE001
            errs.append(f"{label}: {type(e).__name__}: {e}")
            return None

    attempt("process_feedback", lambda: _load_feedback(out))
    attempt("source_alignment", lambda: _load_alignment(out))
    attempt("contract._REVIEW_MIN_REFS", lambda: _load_min_refs(out))
    out["desk"] = _find_desk_check()
    if out["desk"] is None:
        errs.append("manuscript_desk.check 不可用 ⇒ 体例检查这一路没接上")
    out["visual"] = _find_caps_ext_func("visual_desk.py", "scan_visuals")
    if out["visual"] is None:
        errs.append("visual_desk.scan_visuals 不可用 ⇒ 图表锚点检查没接上")
    return out


def _load_feedback(out):
    from .process_feedback import build_feedback
    out["feedback"] = build_feedback


def _load_alignment(out):
    from .source_alignment import align, sources_from_calls
    out["sources"], out["align"] = sources_from_calls, align


def _load_min_refs(out):
    from .contract import _REVIEW_MIN_REFS
    out["min_refs"] = int(_REVIEW_MIN_REFS)


_IMPORTED: dict[str, Any] | None = None


def _deps() -> dict[str, Any]:
    global _IMPORTED
    if _IMPORTED is None:
        _IMPORTED = _safe_import()
    # 常驻能力可能晚于第一次简报才装载 ⇒ 允许重试探一次，别把
    # 「暂时拿不到」缓存成「整场都没有」。
    for key, filename, func, label in (
            ("desk", "manuscript_desk.py", "check", "manuscript_desk"),
            ("visual", "visual_desk.py", "scan_visuals", "visual_desk")):
        if _IMPORTED.get(key) is None:
            fn = _find_caps_ext_func(filename, func)
            if fn is not None:
                _IMPORTED[key] = fn
                _IMPORTED["errors"] = [e for e in _IMPORTED.get("errors", [])
                                       if not e.startswith(label)]
    return _IMPORTED


def _prose_text_for_visual(root: Any, limit: int = 200_000) -> str:
    """里程碑（`text=""`）时只读回工作区里最大的正文 .md，专供视觉扫描。

    不这么做的话：里程碑只在「剩余步数」上说话，模型前 30 步一个文件没写
    时完全看不到图表该做什么 —— 又变成「工具注册了却没人调」。这份读回
    不跑 lint / align / 体例，只喂 `scan_visuals()`，成本是一次正则。
    """
    try:
        from pathlib import Path
        r = Path(root)
        cands = [p for p in r.iterdir()
                 if p.is_file() and p.suffix.lower() == ".md"
                 and not re.search(
                     r"outline|大纲|提纲|说明|计划|检索|fix|note|稿件台|references",
                     p.name, re.I)]
        if not cands:
            return ""
        return max(cands, key=lambda x: x.stat().st_size).read_text(
            encoding="utf-8", errors="replace")[:limit]
    except Exception:                                     # noqa: BLE001
        return ""


def build_readiness(*, trace: Any, contract: Any, step: int, max_steps: int,
                    text: str = "", caps: Any = None,
                    prev_signature: str = "", root: Any = None,
                    detail_threshold: int = 10) -> dict[str, Any]:
    """算一份**只读**的交付就绪度简报。绝不写任何外部状态。

    `prev_signature` 用于去重：缺口集合没变且步数还宽裕时只出一行短报，
    免得每次写文件都拿一大段重复文本训练模型无视反馈。缺口变化或预算
    告急时自动展开细项。
    """
    deps = _deps()
    t = str(text or "")
    try:
        st, cap = int(step), int(max_steps)
    except (TypeError, ValueError):
        st, cap = 0, 40
    left = max(0, cap - st)
    gaps: list[dict[str, str]] = []
    deferred: list[dict[str, str]] = []
    metrics: dict[str, Any] = {}
    notes: list[str] = []
    # 少了一只眼必须说出口：静默降级会让「暂无缺口」变成假绿。
    notes.extend(str(e) for e in (deps.get("errors") or []))
    # 稿件成熟度分诊：`check()` / `lint()` 是面向**完整稿**的判据。半截稿上
    # 「缺参考文献表」「章号不连续」「字数差 80%」恒为真，若每次写文件都列
    # 一遍，模型很快学会整段跳过 —— 那比不报更糟。只有终稿形态（已有文献
    # 表标题）或预算告急时才全量报；与阶段无关的项（零号引言、图面体检）
    # 任何时候都报。
    # 与稿件台 `manuscript_desk.check()` 第 148 行**同一份**精确整行判据：
    # 前缀匹配会把大纲里的「## 参考文献总数核算」误判成终稿形态
    # （11-09 大纲门假红就是同型的「分母写宽了」）。
    mature = bool(re.search(r"^\s*#{1,4}\s*参考文献\s*$", t, re.M)) if t else False
    urgent = left <= URGENT_LEFT
    full_check = bool(mature or urgent)
    metrics["mature"], metrics["full_check"] = mature, full_check

    def gap(code: str, detail: str, action: str, *,
            stage_free: bool = False) -> None:
        row = {"code": code, "severity": "must",
               "detail": detail, "action": action}
        (gaps if (stage_free or full_check) else deferred).append(row)

    # ── ① 图面体检：交付门最常到最后才说的那条 ──────────────────
    needs_figure = bool(getattr(contract, "needs_figure", False))
    audited = False
    try:
        for c in trace or []:
            if bool(_field(c, "ok", True)) and str(_field(c, "name", "")) in FIGURE_AUDIT_TOOLS:
                audited = True
                break
    except Exception:                                     # noqa: BLE001
        notes.append("台账不可读，图面体检状态 unknown")
    metrics["figure_audit_done"] = audited
    metrics["needs_figure"] = needs_figure
    if needs_figure and not audited:
        gap("FIGURE_AUDIT",
            "本任务要出图，台账里还没有一次成功的 `audit_figure` 调用",
            "先按大纲把该有的图画出来并落盘，画完立刻调 `audit_figure`"
            "（改过图要重跑）——别等交付门点名，那时已没步数补正",
            stage_free=True)

    # ── ② 稿面：复用 build_feedback 的 lint() 单源，不另写一套 ──
    fb: dict[str, Any] = {}
    if t and deps["feedback"] is not None:
        target_chars = getattr(contract, "target_chars", None)
        try:
            fb = deps["feedback"](t, target_chars=target_chars, caps=caps) or {}
        except Exception as e:                             # noqa: BLE001
            notes.append(f"稿面读回不可用：{type(e).__name__}")
            try:
                fb = deps["feedback"](t, target_chars=target_chars) or {}
            except Exception as e2:                        # noqa: BLE001
                notes.append(f"稿面读回降级仍失败：{type(e2).__name__}")
    m = dict(fb.get("metrics") or {})
    metrics["chars"] = m.get("chars", 0)
    metrics["score"] = fb.get("score")
    metrics["refs_n"] = m.get("refs_n")
    for i, g in enumerate(list(fb.get("gaps") or [])):
        gap(f"WRITING_{i + 1}", str(g), "按稿面读回逐条改；改完再写一次文件")

    # 长综述文献下限：阈值从契约单源读，不硬编码 50。
    review_like = bool(getattr(contract, "_is_review_like", False))
    long_review = bool(getattr(contract, "_is_long_review", False))
    min_refs = int(deps["min_refs"] or 50)
    metrics["min_refs"] = min_refs if (review_like and long_review) else None
    if review_like and long_review:
        n = m.get("refs_n")
        if isinstance(n, (int, float)) and int(n) < min_refs:
            gap("REFS_MIN", f"参考文献 {int(n)}/{min_refs} 篇（长综述下限）",
                "补齐文献表并在正文里按首现序引用；查不到 DOI 的条目如实留空")

    # ── ③ 体例：常驻稿件台插件的 hard 项（治「0 引言」这类事后返工）──
    if t and deps["desk"] is not None:
        try:
            rep = deps["desk"](t) or {}
            metrics["format_hard"] = int(rep.get("hard") or 0)
            metrics["format_soft"] = int(rep.get("soft") or 0)
            for it in list(rep.get("issues") or []):
                if isinstance(it, dict) and str(it.get("level")) == "hard":
                    code = str(it.get("code") or "FORMAT")
                    detail = str(it.get("detail") or "").strip()
                    gap(f"FORMAT_{code}", detail or code,
                        "按中文期刊体例就地改这一行，别重排编号、别删正文",
                        stage_free=(code in STAGE_FREE_FORMAT_CODES))
        except Exception as e:                             # noqa: BLE001
            notes.append(f"体例检查不可用：{type(e).__name__}")

    # ── ③b 图表锚点：图/表有没有**真的进正文**（2026-11-10）──────
    # D2 稿实证：13 章只有 1 图 1 表，且都排在「参考文献」之后当附录，
    # 正文 1–9 章零处呼应 —— 而所有门全绿。没有工具能定点插、也没有
    # 判据会因此开口。这里复用 visual_desk 的同一份只读判据。
    visual_counts: dict[str, Any] = {}
    if deps["visual"] is not None:
        try:
            from pathlib import Path as _Path
            # root 由调用方给（写文件读回 ⇒ 稿件所在目录）。真跑里内核已
            # chdir 到工作区，Path.cwd() 恰好对；离线复核时不是，若不给
            # root 会把「figure.png 找不到」误报成缺件。
            _root = _Path(root) if root is not None else _Path.cwd()
            visual_text = t or _prose_text_for_visual(_root)
            vr = (deps["visual"](
                visual_text, _root,
                expect_visual=bool(getattr(contract, "needs_figure", False)))
                or {}) if visual_text else {}
            visual_counts = dict(vr.get("counts") or {})
            metrics["visual"] = visual_counts
            for g in (vr.get("gaps") or []):
                code = str(g.get("code") or "")
                if code in ("VISUAL_APPENDIX", "VISUAL_NO_BODY_REF",
                            "VISUAL_MISSING_FILE", "VISUAL_EMPTY"):
                    gap(code, str(g.get("detail") or code),
                        "调 visual_place(chapter=章号, kind='table'|'figure')："
                        "自动编号 + 插进该章 + 写题注 + 写正文呼应 + 读回验证")
                elif code.startswith("VISUAL_"):
                    notes.append(f"图表体例提示：{str(g.get('detail') or '')[:90]}")
        except Exception as e:                             # noqa: BLE001
            notes.append(f"图表锚点检查不可用：{type(e).__name__}")

    # ── ④ 引文对齐：接上审计 §9 留的接缝，但只做提示、不判错 ──────
    alignment_summary: dict[str, Any] = {}
    if t and deps["sources"] is not None and deps["align"] is not None:
        try:
            src = deps["sources"](trace) or {}
            a = deps["align"](t, sources=src.get("sources") or [],
                              read_docs=src.get("read_docs") or [],
                              searched_docs=src.get("searched_docs") or []) or {}
            alignment_summary = dict(a.get("summary") or {})
            metrics["alignment"] = alignment_summary
        except Exception as e:                             # noqa: BLE001
            notes.append(f"引文对齐不可用：{type(e).__name__}")
    unsupported = int(alignment_summary.get("unsupported") or 0)
    unknown = int(alignment_summary.get("unknown") or 0)
    if unsupported:
        notes.append(f"引文对齐：{unsupported} 条判为无原文支撑；"
                     f"另有 {unknown} 条 unknown（不等于 unsupported，不据此改稿）")

    # ── ④b 图表身份层：把「调用时机」接到模型每步都看得见的写后简报 ──
    #   11-11 R1 真任务：工具在册（83）、schema 里有 anchor_id，默认话术
    #   仍 0 次调用。缺的不是工具，是**什么时候该用**的那个时刻。
    #   落点选这里而不是 SYSTEM：简报是运行时工具回包，baseline 等价判据
    #   一字不动、不加门、不烧打回额度。只在「真的已经有图」时才开口，
    #   没图就早报会把模型训练成无视反馈。
    try:
        from pathlib import Path
        from rongke import figure_registry as FR
        base = Path(root) if root is not None else Path.cwd()
        reg, reg_status = FR.load(base)
        has_caption = bool(t and re.search(
            r"^\s*(?:#{1,4}\s*)?(?:\*\*)?(?:图|表)\s*\d+", t, re.M))
        has_image = bool(t and re.search(r"!\[[^\]]*\]\([^)]+\)", t))
        # 整篇 write_file 覆盖会把图块冲掉，此时**文本里**看不出有图，
        # 但工作区里的图产物还在 —— 一并量，否则覆盖那一刻恰好不报。
        has_asset = any(p.suffix.lower() in
                        (".svg", ".png", ".pdf", ".tif", ".tiff")
                        for p in base.glob("figure*") if p.is_file())
        has_visual = bool(has_caption or has_image or has_asset)
        metrics["figure_registry"] = reg_status
        if has_visual and reg_status != "ok":
            gap("FIGURE_REGISTRY_MISSING",
                "正文/工作区已有图或表，但没有 图表登记.json：编号写死在文本里，"
                "删图或重排章节后「见图 N」不会跟着变。"
                "调 visual_place(anchor_id='fig:xxx') 建登记，"
                "重排后调 visual_renumber 自动重编全篇引用",
                "把下一次插图改成 visual_place(..., anchor_id='fig:xxx')；"
                "已有图可用 visual_plan 看缺口",
                stage_free=True)
        elif reg_status == "ok" and reg["entries"]:
            locs, _ = FR.figure_locs(t, reg)
            lost = [e["id"] for e in reg["entries"] if e["id"] not in locs]
            if lost:
                gap("FIGURE_ANCHORS_LOST",
                    f"登记里有 {len(reg['entries'])} 张图，正文却缺 "
                    f"{len(lost)} 个身份锚点（{'、'.join(lost[:3])}）——"
                    "多半是整篇 write_file 覆盖把图块冲掉了。"
                    "调 visual_place(anchor_id=...) 把丢的图补回正文，"
                    "再调 visual_renumber，别只改编号",
                    "重新插图或从上一版把锚点块补回来；别用 paper_status 反复查状态",
                    stage_free=True)
            vr = FR.verify(base, t, reg)
            if not vr["ok"]:
                gap("FIGURE_REGISTRY_GAPS",
                    "图表登记五连读回有缺口："
                    + "、".join(sorted({g["code"] for g in vr["gaps"]}))
                    + "。按 visual_renumber 回包的 gaps 逐项修",
                    "调 visual_renumber 看每项的 action，再修稿或修登记",
                    stage_free=True)
    except Exception as ex:                                  # noqa: BLE001
        # 「读不回来」不等于「没问题」：明说 unknown，不静默当通过。
        notes.append(f"图表登记层读不回来（unknown，不算通过）："
                     f"{type(ex).__name__}: {ex}")

    # ── ④c 文献池（2026-11-12）：事实源缺席 / 五连缺口 / 章映射越界 ──────
    # 只读取盘上 文献池.json（所有权层在 loop 写后已建好），本函数不写任何状态。
    # 三条都是 advisory：不改门、不改 SYSTEM，只把「读过什么不可核」说出口。
    try:
        from pathlib import Path as _LPath
        from rongke import litpool_guard as _LG
        lp_base = _LPath(root) if root is not None else _LPath.cwd()
        lp_st = _LG.status(lp_base)
        metrics["litpool"] = {
            "status": lp_st.get("status"),
            "gap_codes": lp_st.get("gap_codes") or [],
            "summary": lp_st.get("summary") or {},
        }
        if lp_st.get("status") == "absent":
            gap("LITPOOL_ABSENT",
                "工作区没有 文献池.json：本轮检索到的文献、精读卡、"
                "章-文献映射与正文引注无从互证，「读过什么」不可核。",
                "跑一次检索/入库，工具会在回包后自动建池；"
                "不必手抄证据表，也别自己编 doc_id",
                stage_free=True)
        elif lp_st.get("gap_codes"):
            gap("LITPOOL_GAPS",
                "文献池五连读回有缺口："
                + "、".join(sorted(set(lp_st["gap_codes"])))
                + "。相关性语义判断仍归人，但准则与逐篇留痕必须补齐。",
                "按 status 回包里的 gap_codes 逐项补：声明准则 → 逐篇判定"
                " → 取全文 → 写精读卡",
                stage_free=True)
        _bd = lp_st.get("bindings") or {}
        _bad = sum(len(_bd.get(k) or []) for k in
                   ("out_of_pool", "unscreened", "unread"))
        if _bad:
            gap("LITPOOL_OUTLINE_BINDINGS",
                f"章-文献映射越界 {_bad} 处（池外 / 未纳入 / 未精读）："
                "「每章引用 ⊆ 该章清单」以前只是提示词，现在能读回核对。",
                "池外的补检索入池，未纳入的按准则逐篇判，未精读的补"
                " claim/page/quote 精读卡；不直接塞进正文",
                stage_free=True)
    except Exception as ex:                                  # noqa: BLE001
        # 「读不回来」不等于「没问题」：明说 unknown，不静默当通过。
        notes.append(f"文献池层读不回来（unknown，不算通过）："
                     f"{type(ex).__name__}: {ex}")

    # ── ⑤ 验收标准：原样列出，不自行判定（避免造第二套真相）──────
    acceptance = [str(x) for x in (getattr(contract, "acceptance", None) or [])]
    metrics["acceptance_n"] = len(acceptance)

    signature = "|".join(sorted(g["code"] for g in gaps))
    expanded = bool(signature != (prev_signature or "") or left <= detail_threshold
                    or not gaps)

    lines = [TITLE,
             f"步数 {st}/{cap}，还剩 {left} 步。"
             + (f"交付前还差 {len(gaps)} 项：" if gaps
                else "按现有读回，交付判据暂无缺口。")]
    if gaps:
        for i, g in enumerate(gaps[:8], 1):
            lines.append(f"  {i}. {g['detail']}")
        if expanded:
            lines.append("  建议动作：")
            for i, g in enumerate(gaps[:8], 1):
                lines.append(f"    {i}. {g['action']}")
        if left <= detail_threshold:
            lines.append("  ⚠️ 步数已紧：停止开新的检索/读取，先把上面几项补齐再交付。")
    else:
        lines.append("  保持当前稿面，不要在无关细节上反复返工。")
    if deferred:
        lines.append(f"  终稿前还要核 {len(deferred)} 项（现在不必处理）："
                     + "、".join(str(d["code"]) for d in deferred[:5]))
    if visual_counts and int(visual_counts.get("chapters", 0) or 0) > 0:
        lines.append(
            f"  图表覆盖 {visual_counts.get('chapters_with_visual', 0)}/"
            f"{visual_counts.get('chapters', 0)} 章"
            f"（图 {visual_counts.get('figures', 0)} / 表 {visual_counts.get('tables', 0)}；"
            f"文末附录误置 "
            f"{int(visual_counts.get('appendix_figures', 0)) + int(visual_counts.get('appendix_tables', 0))}"
            f" 处）。要补就调 visual_place 落进对应章节。")
    elif bool(getattr(contract, "_is_long_review", False)):
        # 一个文件都还没写时给**动作**而不只是数字：否则模型前 30 步读库，
        # 完全不知道有 visual_plan / visual_place 可用（注册 ≠ 用上）。
        lines.append(
            "  图表编排台：写完大纲先调 visual_plan 看逐章该有几处；"
            "要放图/表就调 visual_place，它会把题注、图片和正文呼应一起"
            "插进指定章节（别堆到「参考文献」之后）。")
    if acceptance and expanded and left <= detail_threshold:
        lines.append("  交付时逐条要有证据的验收标准：")
        for i, a in enumerate(acceptance[:8], 1):
            lines.append(f"    ({i}) {a[:120]}")
    for n in notes[:3]:
        lines.append(f"  · {n}")
    if not t:
        lines.append("  （本次只核台账与预算；稿面/体例以写文件后的读回为准。）")
    lines.append("  （只给信息，不拦你；交付门仍只在 deliver_task 时跑一次。）")

    return {
        "advisory": True,
        "budget": {"step": st, "max_steps": cap, "left": left,
                   "pct": round(st / cap, 3) if cap else 0.0},
        "gaps": gaps,
        "deferred": deferred,
        "signature": signature,
        "expanded": expanded,
        "metrics": metrics,
        "acceptance": acceptance,
        "notes": notes,
        "text": "\n".join(lines),
    }


def readiness_after_write(*, path: str, trace: Any, contract: Any,
                          step: int, max_steps: int, caps: Any = None,
                          prev_signature: str = "",
                          root: Any = None) -> dict[str, Any] | None:
    """`write_file` 成功后的唯一入口：读回刚落盘的全文再算。

    只对正文类文件触发；读不到 / 后缀不对 / 任何异常 ⇒ 返回 None，
    调用方就当没这回事，**绝不改写工具回执本身**。
    """
    from pathlib import Path
    p = Path(str(path or ""))
    if p.suffix.lower() not in PROSE_SUFFIXES:
        return None
    try:
        base = Path(root) if root is not None else Path.cwd()
        q = p if p.is_absolute() else base / p
        # 工作区围栏：不许顺着路径读出去。
        q = q.resolve()
        if base.resolve() not in q.parents and q != base.resolve():
            return None
        text = q.read_text(encoding="utf-8", errors="replace")
    except Exception:                                     # noqa: BLE001
        return None
    try:
        return build_readiness(trace=trace, contract=contract, step=step,
                               max_steps=max_steps, text=text, caps=caps,
                               prev_signature=prev_signature, root=q.parent)
    except Exception:                                     # noqa: BLE001
        return None


def _chapter_from_outline(outline: str) -> str:
    """薄转接：`delivery_pipeline._chapter_from_outline`（保住只读模块边界与旧引用）。"""
    return _dp_chapter_from_outline(outline)


def ensure_visuals(text: str, *, root: Any, outline_text: str = "",
                   figure_qa: dict | None = None) -> tuple[str, dict]:
    """薄转接：写盘与图表落位已迁入 `rongke.delivery_pipeline.ensure_visuals`。"""
    return _dp_ensure_visuals(
        text, root=root, outline_text=outline_text, figure_qa=figure_qa,
        chapter_finder=_chapter_from_outline)


def ensure_visuals_for_delivery(args: dict, contract: Any,
                                emit: Any = None) -> dict:
    """薄转接：交付前图表落位已迁入 `rongke.delivery_pipeline`，本模块自身零写盘。"""
    out, kind, payload = _dp_prepare_visuals_for_delivery(
        args, contract, ensure_fn=ensure_visuals)
    if emit is not None and kind == "visual_autofix" and payload is not None:
        emit("visual_autofix", payload)
    elif emit is not None and kind and payload is not None:
        emit(kind, payload)
    return out


def compact_event(r: dict[str, Any]) -> dict[str, Any]:
    """给事件流的精简快照（不把整段简报塞进归档）。"""
    return {
        "step": (r.get("budget") or {}).get("step"),
        "max_steps": (r.get("budget") or {}).get("max_steps"),
        "left": (r.get("budget") or {}).get("left"),
        "gap_codes": [g.get("code") for g in (r.get("gaps") or [])],
        "expanded": r.get("expanded"),
        "advisory": r.get("advisory"),
        "notes": list(r.get("notes") or [])[:5],
        "metrics": {k: v for k, v in (r.get("metrics") or {}).items()
                    if k != "alignment"},
    }
