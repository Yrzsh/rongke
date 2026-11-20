"""图表**所有权层**：登记在场时，由工具维护图块、身份与编号。

为什么要这一层（2026-11-11 真任务 R1~R5 实证）
------------------------------------------------
`figure_registry.py` 把编号从「内容」降级成「渲染产物」，但它仍要求模型
配合：插图时传 `anchor_id`、重排后调 `visual_renumber`、整篇重写时别删掉
隐藏注释。三场真任务给出的答案是——**模型不会稳定配合**：

  · R1 默认话术：`anchor_id` 0 次、`visual_renumber` 0 次；
  · R4 写了简报提醒后：`success=true`，但 8 次 `write_file` 的 text
    全无 `<!--rk:`，最终五连读回 FAIL；
  · R3 `paper_status` 连查 3 次被 stall 误判，白等 300 s。

所以「调用时机」的正解不是继续提醒，而是**让工具在该时刻自己动手**：
每次写正文后、每次交付前，用登记的可见内容（图片路径 / 题注文本）定位
图块，缺的按登记补回、编号按锚点顺序重算，落盘标准 Markdown。
模型只写它该写的东西（正文与论证），一个编号都不用维护。

纪律：
  · **不编内容**。补回块的呼应句搬登记里模型自己写过的 `claim`，
    图注搬 `caption`，图片搬 `source`；表格没有内容字段 ⇒ 不补、明说。
  · **不改 SYSTEM、不加硬门、不碰 `self.gate`**。本层是确定性文本整理，
    与 `_ensure_visuals_for_delivery` 同一性质。
  · **每次动作都读回**：写盘后逐字节比对，五连读回的 gaps 如实进回包。
  · 纯标准库；`reconcile()` 是纯函数（不写盘），便于判据正负控。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import figure_registry as FR

#: 补回块用的临时编号。取一个正文里不可能出现的大数，避免两条同时补回时
#: `trans[(figure, n)]` 撞车导致链式冲突；renumber 之后还有一次兜底替换。
_TMP_BASE = 900000
_HEAD_RE = re.compile(r"^\s{0,3}#{1,4}[ \t]+(.+?)[ \t]*$")
_TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
_SKIP_NAMES = ("outline", "大纲", "提纲", "说明", "计划", "论文.", "排版台", "稿件台")


def prose_md_files(root: "Path | str") -> list[Path]:
    """工作区里的**正文源稿**（只认文本后缀，导出件永远排在后面）。

    ⚠️ 11-11 踩过：按 mtime / 字符数取第一个会把 `论文.docx`（二进制）
    或 `论文.tex`（导出件）当正文，质量门随即全红。这里按后缀白名单 +
    排除导出件名，且按字符数排（大的更像主稿）。
    """
    base = Path(root)
    out: list[tuple[int, str, Path]] = []
    try:
        for p in base.iterdir():
            if (not p.is_file() or p.suffix.lower() not in _TEXT_SUFFIXES
                    or any(k in p.name for k in _SKIP_NAMES)):
                continue
            try:
                n = len(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:                                   # noqa: BLE001
                n = 0
            out.append((n, p.name, p))
    except Exception:                                           # noqa: BLE001
        return []
    out.sort(key=lambda x: (-x[0], x[1]))
    return [x[2] for x in out]


def _head_titles(lines: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, ln in enumerate(lines):
        m = _HEAD_RE.match(ln)
        if m:
            out.append((i, m.group(1)))
    return out


def _chapter_match(chapter: str, title: str) -> bool:
    """登记章 ↔ 正文标题的**同一份**匹配口径（插入与回报共用）。

    除文字互含外还认编号前缀：登记常写「2 方法」/「2 章」，正文可能写
    「2 实验设计」，只要章号一致就落回那一章，不猜、不硬塞到文末。
    """
    key = re.sub(r"\s+", "", str(chapter or ""))
    t = re.sub(r"\s+", "", str(title or ""))
    if not key:
        return False
    if key in t or t in key:
        return True
    kn = re.match(r"^(\d+)(?![\d.])", key)
    tn = re.match(r"^(\d+)(?![\d.])", t)
    return bool(kn and tn and kn.group(1) == tn.group(1))


def _chapter_insert_at(lines: list[str], chapter: str) -> int:
    """补回块该插在哪一行：登记章末尾（下一个同级标题之前）。

    找不到章就退到「参考文献」之前，再找不到就文末。**不猜位置**：
    回包里带 `insert_at` 与 `chapter_matched`，方便核对。
    """
    heads = _head_titles(lines)
    key = str(chapter or "")
    target = None
    for idx, title in heads:
        if _chapter_match(key, title):
            target = idx
            break
    if target is not None:
        for idx, _title in heads:
            if idx > target:
                return idx
        return len(lines)
    for idx, title in heads:
        if "参考文献" in title:
            return idx
    return len(lines)


def _block(e: dict, tmp: int) -> list[str]:
    """一条图表登记的补回块。**全部内容来自登记**，工具只写编号与语法。

    图：图在上、题注在下；表：题注在上、表格在下（与 `visual_desk` 的
    体例一致）。表格正文来自登记的 `body`，不是工具编出来的。
    """
    z = FR.ZH.get(e["kind"], "图")
    out: list[str] = []
    claim = str(e.get("claim") or "").strip()
    if claim:
        # 呼应句搬模型自己写过的论证；编号由工具算，不由模型记。
        out.append(f"（见{z} {tmp}）{claim}")
    if e["kind"] == "table":
        out.append(f"{z} {tmp}　{e['caption']}<!--rk:{e['id']}-->")
        out.extend(str(e.get("body") or "").splitlines())
    else:
        out.append(f"![{e['id']}]({e['source']})")
        out.append(f"{z} {tmp}　{e['caption']}<!--rk:{e['id']}-->")
    return out


_IMG_LINE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_CAP_LINE_RE = re.compile(
    r"^\s{0,3}(?:#{1,4}[ \t]*)?(?:\*\*)?(图|表)[ \t]*(\d+)"
    r"[ \t]*[　\s:：、.．]+(.+?)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
_LOCAL_IMG = (".svg", ".png", ".pdf", ".tif", ".tiff", ".jpg", ".jpeg")


def _strip_comment(s: str) -> str:
    return re.sub(r"<!--\s*rk:[^>]*-->\s*$", "", str(s or "")).strip()


def _stable_id(kind: str, src: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(str(src)).stem).strip("-")
    return ("fig:" if kind == "figure" else "tab:") + (stem or "inline")


def ensure_registry(text: str, root: "Path | str") -> dict:
    """★ 默认路径的入口：从**可见图块**自动建登记，模型一个参数都不用给。

    R7 干净场实证：`visual_place` 13 次只有 1 次带 `anchor_id`，最终稿有
    2 图 2 题注却没有 `图表登记.json` —— 所有权层因此全程空转。
    「调用时机」的最后一环不是再提醒模型，而是工具把模型已经写进正文的
    事实（图片路径、题注、章标题、呼应句）**登记下来**。

    纪律：
      · 只搬可见内容，不编图注、不编论证、不编 id 语义（id 来自文件名）；
      · 已有登记按 `source` / `caption` 去重，绝不覆盖模型给的语义 id；
      · 没有图块或没有题注就不建（不猜），文件不在盘上也不建；
      · 写盘后 `FR.load` 读回，逐条确认真的在。
    """
    base = Path(root)
    rep: dict = {"ok": False, "created": [], "skipped": [],
                 "registry_status": "", "written": False,
                 "readback_equal": False, "scope": "figure-guard-bootstrap",
                 "advisory": True}
    try:
        reg, status = FR.load(base)
    except Exception as ex:                                    # noqa: BLE001
        rep["error"] = f"登记表读不回来：{type(ex).__name__}: {ex}"
        return rep
    rep["registry_status"] = status
    if status.startswith("error:"):
        rep["error"] = "现有登记表损坏，不自动覆盖（先修它）"
        return rep

    entries = list(reg["entries"])
    known_src = {str(e.get("source") or "") for e in entries}
    known_cap = {str(e.get("caption") or "") for e in entries}
    known_id = {str(e.get("id") or "") for e in entries}
    lines = str(text or "").splitlines()
    head_at = [(i, m.group(1)) for i, ln in enumerate(lines)
               if (m := _HEAD_RE.match(ln))]

    def chapter_of(i: int) -> str:
        prev = [t for j, t in head_at if j < i]
        return str(prev[-1]) if prev else ""

    def claim_near(i: int, n: int) -> str:
        """搬模型自己写过的呼应句；找不到就空着（不编论证）。"""
        for j in range(max(0, i - 6), min(len(lines), i + 4)):
            ln = lines[j]
            if (_CAP_LINE_RE.match(ln) or _IMG_LINE_RE.search(ln)
                    or _TABLE_ROW_RE.match(ln)):
                continue
            if re.search(r"(?:见|如|由|据)\s*[图表]\s*" + str(n) + r"(?!\d)", ln) \
                    or re.search(r"[图表]\s*" + str(n) + r"\s*(?:显示|表明|给出)", ln):
                return ln.strip()[:160]
        return ""

    def add(e: dict) -> None:
        entries.append(e)
        known_src.add(e["source"])
        known_cap.add(e["caption"])
        known_id.add(e["id"])
        rep["created"].append(e["id"])

    # ── 图：图片行 + 后面 4 行内的「图 N」题注 ──────────────────
    for i, ln in enumerate(lines):
        m = _IMG_LINE_RE.search(ln)
        if not m:
            continue
        src = FR._clean_src(m.group(1))
        if not src or src in known_src or src.startswith(("http://", "https://", "data:")):
            rep["skipped"].append(src or "(空图片)")
            continue
        if not src.lower().endswith(_LOCAL_IMG) or not (base / src).is_file():
            rep["skipped"].append(src + "（非本地产物或不在盘上）")
            continue
        cap, num = "", ""
        for j in range(i + 1, min(len(lines), i + 5)):
            cm = _CAP_LINE_RE.match(lines[j])
            if cm and cm.group(1) == "图":
                cap, num = _strip_comment(cm.group(3)), cm.group(2)
                break
        if not cap or cap in known_cap:
            rep["skipped"].append(src + "（缺题注或题注已登记）")
            continue
        eid = _stable_id("figure", src)
        if eid in known_id:
            rep["skipped"].append(src + "（id 已占用）")
            continue
        add({"id": eid, "kind": "figure", "caption": cap,
             "chapter": chapter_of(i), "claim": claim_near(i, num),
             "source": src, "body": "", "tier": "preview",
             "qa": {"backend": "auto-bootstrap", "ok": True}})

    # ── 表：题注在前，紧跟 markdown 表格行 ──────────────────────
    for i, ln in enumerate(lines):
        cm = _CAP_LINE_RE.match(ln)
        if not cm or cm.group(1) != "表":
            continue
        cap = _strip_comment(cm.group(3))
        if not cap or cap in known_cap:
            continue
        rows = []
        for j in range(i + 1, len(lines)):
            if _TABLE_ROW_RE.match(lines[j]):
                rows.append(lines[j].strip())
            elif rows:
                break
        if not rows:
            continue
        eid = "tab:" + re.sub(r"[^A-Za-z0-9_.-]+", "-", cap)[:24].strip("-")
        if eid in known_id:
            continue
        add({"id": eid, "kind": "table", "caption": cap,
             "chapter": chapter_of(i), "claim": claim_near(i, cm.group(2)),
             "source": "(inline)", "body": "\n".join(rows),
             "tier": "preview", "qa": {"backend": "auto-bootstrap", "ok": True}})

    if not rep["created"]:
        rep["ok"] = True
        rep["note"] = "正文里没有可登记的新图表（或已登记过），盘上一个字节没动"
        return rep
    try:
        saved = FR.save(base, {"entries": entries})
        back, st = FR.load(base)
    except Exception as ex:                                    # noqa: BLE001
        rep["error"] = f"自动登记写盘/读回失败：{type(ex).__name__}: {ex}"
        return rep
    rep["written"] = True
    rep["readback_equal"] = bool(saved.get("readback_equal")) and all(
        any(e["id"] == x for e in back["entries"]) for x in rep["created"])
    rep["ok"] = bool(saved.get("ok")) and st == "ok" and rep["readback_equal"]
    rep["n_entries"] = len(back["entries"])
    if not rep["ok"]:
        rep["error"] = "自动登记写后读回不一致，见 FR.save 回包"
    return rep


def reconcile(text: str, root: "Path | str") -> tuple[str, dict]:
    """登记在场时：补回丢失图块 → 重编全篇编号 → 渲染成标准 Markdown。

    返回 `(clean_text, report)`。`clean_text` 可直接落盘（人/模型都看得懂）：
    图片 alt 是 `图 N`、题注无隐藏注释，身份靠**可见内容**继续定位。
    登记缺席时原样返回且 `applied=False`，盘上零副作用。
    """
    src = str(text or "")
    rep: dict = {"applied": False, "restored": [], "unrestorable": [],
                 "registry": "absent", "text_changed": False, "ok": True,
                 "gaps": [], "numbering": {}, "insert_at": None,
                 "chapter_matched": False, "scope": "figure-guard",
                 "advisory": True}
    try:
        reg, status = FR.load(root)
    except Exception as ex:                                    # noqa: BLE001
        rep.update({"ok": False, "registry": f"error:{type(ex).__name__}:{ex}",
                    "gaps": [{"code": "GUARD_REGISTRY_UNREADABLE",
                              "detail": str(ex)}]})
        return src, rep
    rep["registry"] = status
    if status != "ok" or not reg["entries"]:
        rep["note"] = "没有可用的图表登记：本层不动正文（不造空表冒充通过）"
        return src, rep

    locs, how = FR.figure_locs(src, reg)
    missing = [e for e in reg["entries"] if e["id"] not in locs]
    restore = [e for e in missing
               if e["kind"] == "figure" or str(e.get("body") or "").strip()]
    rep["unrestorable"] = [
        {"id": e["id"], "reason": "表格登记里没有正文 body，不编造单元格"}
        for e in missing if e not in restore]
    rep["locators"] = how

    t = src
    tmp_of: dict[str, int] = {}
    if restore:
        lines = t.splitlines()
        trailing = "\n" if t.endswith(("\n", "\r")) else ""
        at = _chapter_insert_at(lines, restore[0].get("chapter") or "")
        rep["insert_at"] = at
        rep["chapter_matched"] = any(
            _chapter_match(restore[0].get("chapter") or "", title)
            for _i, title in _head_titles(lines))
        chunks: list[str] = []
        for j, e in enumerate(restore):
            tmp = _TMP_BASE + j
            tmp_of[e["id"]] = tmp
            chunks.extend([""] + _block(e, tmp) + [""])
        lines[at:at] = "".join(x + "\n" for x in chunks).splitlines()
        t = "\n".join(lines) + trailing
        rep["restored"] = list(tmp_of)

    try:
        numbered, ren = FR.renumber(t, reg)
    except Exception as ex:                                    # noqa: BLE001
        rep.update({"ok": False, "gaps": [{"code": "GUARD_RENUMBER_FAILED",
                                           "detail": f"{type(ex).__name__}: {ex}"}]})
        return src, rep
    # 混合稿下 renumber 只改带锚点题注行 ⇒ 呼应句里的临时编号可能没被换。
    # 这里按登记算出来的编号兜底替换，两条路径结果一致（幂等）。
    nums = ren.get("numbering") or {}
    for eid, tmp in tmp_of.items():
        want = nums.get(eid)
        if want is not None:
            numbered = numbered.replace(f"图 {tmp}", f"图 {want}")
            numbered = numbered.replace(f"表 {tmp}", f"表 {want}")
    try:
        clean, rr = FR.render_markdown(numbered, reg)
        v = FR.verify(root, numbered, reg, rendered=clean)
    except Exception as ex:                                    # noqa: BLE001
        rep.update({"ok": False, "gaps": [{"code": "GUARD_READBACK_FAILED",
                                           "detail": f"{type(ex).__name__}: {ex}"}]})
        return src, rep

    rep.update({
        "applied": True,
        "text_changed": clean != src,
        "renumber": {k: ren.get(k) for k in
                     ("ok", "changed", "drift", "orphan_ids", "mixed_mode", "note")},
        "numbering": nums,
        "render": {k: rr.get(k) for k in ("ok", "unknown_ids", "note")},
        "verify_ok": bool(v.get("ok")),
        "gaps": [{"code": g["code"], "level": g["level"], "detail": g["detail"],
                  "action": g["action"]} for g in v.get("gaps") or []],
        "ok": bool(v.get("ok")) and bool(rr.get("ok")) and bool(ren.get("ok")),
        "note": ("；".join(x for x in (ren.get("note"), rr.get("note"),
                                     v.get("note")) if x)),
    })
    return clean, rep


def reconcile_file(path: "Path | str", root: "Path | str" | None = None
                   ) -> dict:
    """写正文文件之后调：读盘 → reconcile → 有变化才写回 → **写后读回**。"""
    p = Path(path)
    base = Path(root) if root is not None else p.parent
    try:
        original = p.read_text(encoding="utf-8")
    except Exception as ex:                                    # noqa: BLE001
        return {"ok": False, "applied": False,
                "error": f"读不回刚写的正文：{type(ex).__name__}: {ex}"}
    # ★ 默认路径最后一环：模型不传 anchor_id 也照样有登记 —— 从盘上
    #   可见图块自动建。这是写盘动作，所以放在 reconcile_file 而不是纯函数里。
    boot = ensure_registry(original, base)
    rep_boot = {k: boot.get(k) for k in
                ("ok", "created", "skipped", "registry_status", "error")}
    clean, rep = reconcile(original, base)
    rep["bootstrap"] = rep_boot
    if not boot.get("ok"):
        rep["ok"] = False
        rep.setdefault("gaps", []).append({
            "code": "GUARD_BOOTSTRAP_FAILED", "level": "hard",
            "detail": str(boot.get("error") or "自动登记读回不一致"),
            "action": "检查工作区里的图片路径与题注行"})
    rep["file"] = p.name
    if not rep.get("text_changed"):
        rep["written"] = False
        return rep
    try:
        p.write_text(clean, encoding="utf-8", newline="\n")
        back = p.read_text(encoding="utf-8")
    except Exception as ex:                                    # noqa: BLE001
        rep.update({"ok": False, "written": False,
                    "error": f"补回写盘/读回失败：{type(ex).__name__}: {ex}"})
        return rep
    rep["written"] = True
    rep["readback_equal"] = (back == clean)
    if not rep["readback_equal"]:
        rep["ok"] = False
    return rep


def figure_status(root: "Path | str", text: str = "") -> dict:
    """交付前只读汇总：登记在不在、五连读回过不过、缺哪几环。

    给 `deliver_attempt` 与 `final` 用 —— **advisory**，不改任何门的判定，
    但让「success=true 却静默带缺口」在架构上不再可能。
    """
    base = Path(root)
    reg, status = FR.load(base)
    if status != "ok" or not reg["entries"]:
        return {"status": status, "ok": False, "n_entries": 0, "gaps": [],
                "note": "没有图表登记（advisory：不算硬门，但如实说出口）",
                "advisory": True}
    body = str(text or "")
    if not body:
        for p in prose_md_files(base):
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except Exception:                                   # noqa: BLE001
                continue
            if body.strip():
                break
    try:
        v = FR.verify(base, body, reg,
                      rendered=FR.render_markdown(body, reg)[0])
    except Exception as ex:                                    # noqa: BLE001
        return {"status": status, "ok": False, "n_entries": len(reg["entries"]),
                "gaps": [{"code": "GUARD_STATUS_UNREADABLE",
                          "detail": f"{type(ex).__name__}: {ex}"}],
                "advisory": True}
    return {"status": status, "ok": bool(v.get("ok")),
            "n_entries": len(reg["entries"]),
            "numbering": v.get("numbering") or {},
            "gaps": [{"code": g["code"], "level": g["level"],
                      "detail": g["detail"]} for g in v.get("gaps") or []],
            "note": v.get("note") or "", "advisory": True}


def status_line(st: dict) -> str:
    """给 `final` / 工具回执的一句话。**不加门**，只把事实说出来。"""
    if not st:
        # 「读不回来」不等于「没问题」：明说 unknown，不静默当通过。
        return "图表登记：状态读不回来（unknown，不算通过）"
    if st.get("status") != "ok" or not st.get("n_entries"):
        return "图表登记：本场未建立（编号写死在正文里，删图/重排不会自动跟着变）"
    if st.get("ok"):
        return f"图表登记：{st['n_entries']} 张图，五连读回通过，编号与产物一致"
    codes = "、".join(sorted({g["code"] for g in st.get("gaps") or []}))
    return (f"图表登记：{st['n_entries']} 张图，五连读回有 {len(st.get('gaps') or [])} "
            f"项缺口（{codes}）—— 交付门未据此拦下，下面是缺口明细")
