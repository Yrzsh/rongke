# -*- coding: utf-8 -*-
"""交付前管线与写作工作流共享单源（Wave 1 收敛）。

把原先散在 `loop.py`、`gate.py`、`readiness.py`、`process_feedback.py`、
`litpool.py`、`litpool_guard.py` 里的共享常量、文件发现规则与**写盘型**
交付前整理收敛到本模块：

  · 单源常量：`CHARS_TOLERANCE`、`FIGURE_AUDIT_TOOLS`、`DOCID_RE`、
    `PROSE_SUFFIXES`、`GUARD_SUFFIXES`；
  · 单源发现：`DeliveryDraft`、`resolve_prose_files()`、`find_outline_file()`、
    `find_desk_func()`；
  · 写盘型交付整理（与纯只读的 `readiness.py` 彻底分离）：
    `ensure_visuals()`、`ensure_visuals_for_delivery()`、
    `normalize_prose_file()`、`run_pre_delivery_pipeline()`。

纪律：
  · 纯标准库，零第三方依赖；
  · 不 import `gate`（避免循环依赖，也保证只读层不碰门状态）；
  · 写盘一律采用临时文件 + `os.replace`，内核包零删除调用；
  · 所有交付前整理都是确定性搬运/重编/规范化，不编内容、不加硬门，
    并保持盘上文件与 `args["draft_text"]` 同源同步。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 字数容差（±15%）：`gate.py` 与 `process_feedback.py` 共用单源。
CHARS_TOLERANCE: float = 15.0

#: 算「跑过图面体检」的工具集合：`gate.py` 与 `readiness.py` 共用单源。
FIGURE_AUDIT_TOOLS: frozenset[str] = frozenset({"audit_figure"})

#: 正文类后缀（用于写后稿面/体例只读简报）。
PROSE_SUFFIXES: frozenset[str] = frozenset({".md", ".txt", ".tex", ".rst"})

#: 所有权层会整理的正文源稿后缀（不碰导出件或二进制）。
GUARD_SUFFIXES: tuple[str, ...] = (".md", ".markdown", ".txt", ".rst")

#: doc_id / DOI 形态（`gate.py` 大纲门与 `litpool.py` 文献池共用单源）。
DOCID_RE = re.compile(
    r"(?i)(?:10\.\d{4,9}/[^\s,，、;；)）\]】\"']+"
    r"|\bkb[0-9a-z]{6,}\b"
    r"|\b[0-9a-f]{8,}\b"
    r"|\b[a-z]{2,}[0-9]{3,}\b)")

_OUTLINE_NAME_MARKS: tuple[str, ...] = ("outline", "大纲", "提纲")

#: 常驻操作面已下沉到内核纯模块的映射（优先直导，无加载竞态）。
_DESK_PURE_MODULES: dict[str, str] = {
    "manuscript_desk.py": "manuscript",
    "visual_desk.py": "visual_layout",
}


@dataclass
class DeliveryDraft:
    """一次交付前整理与核验共同指向的稿件对象（盘路径 + 当前正文）。"""
    path: Path | None = None
    text: str = ""
    source: str = ""


def _norm_compare(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[，。、；：！？,.;:!?\"'`（）()\[\]【】]", "", s)
    return s


def find_desk_func(filename: str, funcname: str):
    """取常驻操作面的纯函数：优先内核下沉模块，再复用 `caps` 已加载模块。

    优先顺序：
      1. 下沉后的纯模块（如 `rongke.manuscript` / `rongke.visual_layout`）；
      2. `sys.modules.values()` 按 `__file__` 反查 `caps_ext/<filename>`（不重跑 register）；
      3. 直接按包路径导入 `rongke.caps_ext.<stem>`。
    """
    pure = _DESK_PURE_MODULES.get(filename)
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
        fn = getattr(mod, funcname, None)
        return fn if callable(fn) else None
    except Exception:                                         # noqa: BLE001
        return None


def resolve_prose_files(root: Path | str | None = None, *,
                        include_exports: bool = False,
                        limit: int = 12) -> list[tuple[str, Path]]:
    """工作区正文文件发现的统一入口。

    - `include_exports=True`：走 `shape.prose_files`（含 `.docx`/`.doc`/`.tex` 与 `data/`，按 mtime 降序），
      供交付清单与未完成兜底读回；
    - `include_exports=False`：走 `figure_guard.prose_md_files`（只认正文源稿、排除大纲与导出件，按字数降序），
      供图表/体例整理与视觉扫描。
    """
    if include_exports:
        from .shape import prose_files as _shape_prose_files
        return _shape_prose_files(root, limit)
    from . import figure_guard as _fg
    base = Path(root) if root is not None else Path.cwd()
    return [(p.name, p) for p in _fg.prose_md_files(base)[:limit]]


def find_outline_file(root: Path | str | None = None, *,
                      recursive: bool = False) -> Path | None:
    """在工作区里查找大纲 Markdown（同时认 `outline.md` / `大纲.md` / `提纲.md`）。

    多份并存时取最新修改的一份（与 `gate._check_outline` 口径一致）。
    找不到返回 None，不造假路径。
    """
    try:
        base = Path(root) if root is not None else Path.cwd()
    except Exception:                                         # noqa: BLE001
        return None
    cands: list[Path] = []
    try:
        it = base.rglob("*.md") if recursive else base.glob("*.md")
        for p in it:
            if not p.is_file() or p.name.startswith("."):
                continue
            low = p.name.lower()
            if any(k in low or k in p.name for k in _OUTLINE_NAME_MARKS):
                cands.append(p)
    except Exception:                                         # noqa: BLE001
        cands = []
    if not cands:
        return None
    try:
        return max(cands, key=lambda x: x.stat().st_mtime)
    except Exception:                                         # noqa: BLE001
        return cands[0]


def _inside_workspace(path: Path, root: Path) -> bool:
    try:
        q = path.resolve()
        b = root.resolve()
        return q == b or b in q.parents
    except Exception:                                         # noqa: BLE001
        return False


def _resolve_draft_path(raw: str, root: Path) -> Path | None:
    s = str(raw or "").strip()
    if not s:
        return None
    p = Path(s)
    q = p if p.is_absolute() else root / p
    try:
        q = q.resolve()
    except Exception:                                         # noqa: BLE001
        return None
    if not _inside_workspace(q, root):
        return None
    return q


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写回：同目录 `.tmp` + `os.replace`，内核零删除调用。"""
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _chapter_from_outline(outline: str) -> str:
    """从大纲的「④ 图表：本章配图 N」反查它属于第几章。"""
    t = str(outline or "")
    m = re.search(r"本章配图\s*\d+|配图\s*1|Fig\.?\s*1", t, re.I)
    if not m:
        return ""
    heads = list(re.finditer(r"^[ \t]{0,3}#{1,4}[ \t]+(.+?)[ \t]*$",
                             t[:m.start()], re.M))
    if not heads:
        return ""
    title = heads[-1].group(1).strip()
    num = re.match(r"^\s*(\d+)", title)
    return num.group(1) if num else title[:20]


def ensure_visuals(text: str, *, root: Any, outline_text: str = "",
                   figure_qa: dict | None = None,
                   chapter_finder: Any = None) -> tuple[str, dict]:
    """**交付前结构整理**：把「已画好、大纲已规划、却没进正文」的图落位。

    纪律：只搬模型自己产出的文件与契约标题，不编图注、不编数据、
    不删任何正文；已有图就完全不干预；失败返回原文 + `ok=False`。
    """
    scan = find_desk_func("visual_desk.py", "scan_visuals")
    place = find_desk_func("visual_desk.py", "place_visual")
    if scan is None or place is None:
        return text, {"ok": False, "reason": "图表编排台不可用，跳过整理"}
    v = scan(text, root, expect_visual=True) or {}
    if int((v.get("counts") or {}).get("figures", 0) or 0) > 0:
        return text, {"ok": True, "skipped": "正文已有图，不干预"}
    r = Path(root)
    img = next((p for n in ("figure.png", "figure.svg", "figure.pdf", "figure.tiff")
                for p in [r / n] if p.is_file()), None)
    if img is None:
        return text, {"ok": False, "reason": "工作区没有已落盘的图文件"}
    qa = figure_qa
    if qa is None:
        try:
            qa = json.loads((r / "figure-qa.json").read_text(encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            qa = {}
    ct = (qa or {}).get("contract") or {}
    caption = str(ct.get("title") or "").strip() or "已核验研究数据的图示汇总"
    conclusion = str(ct.get("core_conclusion") or "").strip()
    claim = (f"如图 1 所示，{conclusion[:120]}" if conclusion else "")
    find_ch = chapter_finder if callable(chapter_finder) else _chapter_from_outline
    chapter = find_ch(outline_text) or "1"
    new, rep = place(text, chapter=chapter, kind="figure", caption=caption,
                     claim=claim, file=img.name, root=root)
    rep["source"] = {"file": img.name,
                     "caption_from": "figure-qa.json/contract.title",
                     "chapter_from": "outline.md 逐章图表规划",
                     "chapter": chapter}
    return new, rep


def prepare_visuals_for_delivery(args: dict, contract: Any, *,
                                 root: Path | str | None = None,
                                 ensure_fn: Any = None
                                 ) -> tuple[dict, str, dict | None]:
    """`deliver_task` 前图表落位的核心执行器，返回 `(new_args, event_kind, event_data)`。

    修正后的全文同时写回 `draft_text` 与落盘文件，保证门量到的与盘上一致。
    大纲统一走 `find_outline_file()`，不再只认死写 `outline.md`。
    """
    if not bool(getattr(contract, "needs_figure", False)):
        return args, "", None
    try:
        base = Path(root) if root is not None else Path.cwd()
        text = str(args.get("draft_text") or "")
        dp = str(args.get("draft_path") or "").strip()
        p = _resolve_draft_path(dp, base) if dp else None
        if not text and p is not None and p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
        if not text:
            return args, "", None
        outline = ""
        o = find_outline_file(base, recursive=False)
        if o is not None and o.is_file():
            outline = o.read_text(encoding="utf-8", errors="replace")
        fn = ensure_fn if callable(ensure_fn) else ensure_visuals
        new, rep = fn(text, root=base, outline_text=outline)
        if not rep.get("ok") or not rep.get("changed"):
            return args, "", None
        out = dict(args)
        out["draft_text"] = new
        if p is not None and p.is_file():
            _atomic_write_text(p, new)
        payload = {
            "ok": True, "chapter": rep.get("chapter_title"),
            "anchor": rep.get("anchor"), "file": rep.get("file"),
            "insert_at_line": rep.get("insert_at_line"),
            "source": rep.get("source"), "advisory": True,
            "reason": "模型画了图、大纲写了规划，但正文 0 图 ⇒ 交付前按已有素材落位",
        }
        return out, "visual_autofix", payload
    except Exception as e:                                    # noqa: BLE001
        return args, "visual_autofix_error", {"error": f"{type(e).__name__}: {e}"}


def ensure_visuals_for_delivery(args: dict, contract: Any,
                                emit: Any = None, *,
                                root: Path | str | None = None) -> dict:
    """`deliver_task` 前的接线入口：修正后的全文同时写回 `draft_text` 与落盘文件。"""
    out, kind, payload = prepare_visuals_for_delivery(args, contract, root=root)
    if emit is not None and kind and payload is not None:
        emit(kind, payload)
    return out


def resolve_delivery_target(args: dict, root: Path | str | None = None) -> Path | None:
    """确定交付前整理应作用的正文源稿文件。

    优先尊重 `args["draft_path"]`（只要在工作区内且是正文后缀），
    避免盘上另有更大文件时整理错对象；未给或不可用时退回 `resolve_prose_files`。
    """
    base = Path(root) if root is not None else Path.cwd()
    dp = str((args or {}).get("draft_path") or "").strip()
    if dp:
        q = _resolve_draft_path(dp, base)
        if q is not None and q.is_file() and q.suffix.lower() in GUARD_SUFFIXES:
            return q
    files = resolve_prose_files(base, include_exports=False, limit=1)
    return files[0][1] if files else None


def normalize_prose_file(path: Path, emit: Any = None) -> tuple[str | None, list]:
    """对正文源稿跑一次确定性体例规范化（引言去零号、文献表去内部标注等）。"""
    norm_fn = find_desk_func("manuscript_desk.py", "normalize")
    if not callable(norm_fn) or not path.is_file():
        return None, []
    try:
        orig = path.read_text(encoding="utf-8", errors="replace")
    except Exception:                                         # noqa: BLE001
        return None, []
    new_text, changes = norm_fn(orig)
    if not changes or new_text == orig:
        return None, []
    _atomic_write_text(path, new_text)
    if emit is not None:
        emit("manuscript_normalized", {"file": path.name, "changes": changes})
    return new_text, list(changes)


def run_pre_delivery_pipeline(args: dict, contract: Any,
                              emit: Any = None, *,
                              root: Path | str | None = None
                              ) -> tuple[dict, dict, dict]:
    """交付前四段确定性管线（同源同步盘上文件与 `args["draft_text"]`）。

    顺序：
      1. 图表落位兜底（`ensure_visuals_for_delivery`）
      2. 图表所有权层对账重编（`figure_guard.reconcile_file` + `figure_status`）
      3. 文献池交付态五连读回（`litpool_guard.reconcile_before_delivery`）
      4. 正文确定性体例规范化（`normalize_prose_file`）

    关键修复：第 2/4 步修改盘上目标稿件后，若 `args["draft_text"]` 原本与该
    盘文件同源（为空或被原盘文包含），同步刷新 `args["draft_text"]`，彻底消除
    「盘上已规范化/重编号，但 `gate._draft_text()` 因字面不再是子串而回退审旧稿」
    与「`_draft_for_display()` 优先展示旧 `draft_text`」的脱节。

    返回 `(updated_args, figure_status, litpool_status)`。
    """
    from . import figure_guard as _fg
    from . import litpool_guard as _lg

    base = (Path(root) if root is not None else Path.cwd()).resolve()
    out = dict(args or {})

    # 1. 图表落位兜底
    try:
        out = ensure_visuals_for_delivery(out, contract, emit, root=base)
    except Exception as e:                                    # noqa: BLE001
        if emit is not None:
            emit("visual_autofix_error", {"error": f"{type(e).__name__}: {e}"})

    target = resolve_delivery_target(out, base)
    orig_disk = ""
    if target is not None and target.is_file():
        try:
            orig_disk = target.read_text(encoding="utf-8", errors="replace")
        except Exception:                                     # noqa: BLE001
            orig_disk = ""

    raw_dt = str(out.get("draft_text") or "").strip()
    dt_synced_to_disk = bool(
        target is not None and orig_disk
        and (not raw_dt or _norm_compare(raw_dt) in _norm_compare(orig_disk))
    )

    # 2. 图表所有权层
    disk_changed = False
    body = orig_disk
    try:
        if target is None:
            fig_status = _fg.figure_status(base)
        else:
            rep = _fg.reconcile_file(target, base)
            if (rep.get("applied") and (rep.get("restored") or rep.get("written"))) \
                    or (rep.get("bootstrap") or {}).get("created"):
                if emit is not None:
                    emit("figure_reconciled", {
                        "file": target.name, "phase": "delivery",
                        "restored": rep.get("restored") or [],
                        "bootstrap": (rep.get("bootstrap") or {}).get("created") or [],
                        "written": bool(rep.get("written")),
                        "verify_ok": bool(rep.get("verify_ok")),
                    })
            if rep.get("written"):
                disk_changed = True
            try:
                body = target.read_text(encoding="utf-8", errors="replace")
            except Exception:                                 # noqa: BLE001
                body = orig_disk
            fig_status = _fg.figure_status(base, text=body)
    except Exception as e:                                    # noqa: BLE001
        fig_status = {"ok": False, "status": "error",
                      "gaps": [{"code": "GUARD_DELIVERY_FAILED",
                                "detail": f"{type(e).__name__}: {e}"}],
                      "advisory": True}
        if emit is not None:
            emit("figure_reconcile_error", dict(fig_status))

    # 3. 文献池五连读回
    try:
        lp_rep = _lg.reconcile_before_delivery(base, manuscript_text=body)
        lit_status = lp_rep.get("status") or {}
        if emit is not None:
            emit("litpool_status", {
                "status": lit_status.get("status") or "unknown",
                "gap_codes": lit_status.get("gap_codes") or [],
                "advisory": True,
            })
    except Exception as e:                                    # noqa: BLE001
        lit_status = {"ok": False, "status": "error",
                      "gaps": [{"code": "LITPOOL_GUARD_FAILED",
                                "detail": f"{type(e).__name__}: {e}"}],
                      "advisory": True}
        if emit is not None:
            emit("litpool_error", dict(lit_status))

    # 4. 正文确定性体例规范化
    if target is not None:
        try:
            new_body, changes = normalize_prose_file(target, emit)
            if changes and new_body is not None:
                body = new_body
                disk_changed = True
        except Exception as e:                                # noqa: BLE001
            if emit is not None:
                emit("manuscript_normalize_error",
                     {"error": f"{type(e).__name__}: {e}"})

    # 5. 同源同步 draft_text（只同步原本与该盘文件同源的情形，不覆盖更新的独立 draft_text）
    if disk_changed and dt_synced_to_disk and raw_dt and body:
        out["draft_text"] = body

    return out, fig_status, lit_status
