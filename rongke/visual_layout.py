# -*- coding: utf-8 -*-
"""图表编排核心算法：图表扫描、编号计算与正文落位（纯领域模块）。

本模块承载原 `caps_ext/visual_desk.py` 中的纯计算逻辑，
供 `caps_ext/visual_desk.py`、`typeset.py`、`readiness.py` 等直接导入，
不再经由 `sys.modules` 扫描外部常驻插件。

只给信息、不接硬门；预览图自检口径为 `scope="preview"`，不冒充投稿级 PDF。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from rongke.tool_sdk import safe_workspace_path

VOICE = """【图表编排台 · 常驻】
正文里的图/表是论证的一部分，不是文末附件。写完大纲、动笔之前先调
`visual_plan` 看逐章该有几处；要放图/表就调 `visual_place`，一次给
章节号 + 一句图注 + 数据（或已有图文件），工具负责编号、落盘、插进
该章、写题注和正文交叉引用，并读回验证。别自己手写 `![图N](...)`，
更不要把 `## 图 1` 排到 `## 参考文献` 后面。"""

#: 章节标题（复用 shape 的章节单源，不另写一套分母）。
#: ⚠️ 行首空白只写 `[ \t]`，**不写 `\s`** —— `\s` 含换行，`m.start()`
#: 会落在前一行的换行处，题注就截成空串（11-09 大纲门同型坑）。
_RE_HEAD = re.compile(r"^[ \t]{0,3}#{1,4}[ \t]+(.+?)[ \t]*$", re.M)
_RE_REFS = re.compile(r"^[ \t]{0,3}#{1,4}[ \t]*参考文献[ \t]*$", re.M)
#: 题注：`图 1　标题` / `表 1 标题` / `**图 1 …**` / `## 表 1 …`。
#: 实测 D2 稿把图表写成二级标题 `## 图 1` / `## 表 1`（当附录），
#: 只认裸行首会漏掉一整类 —— 那正是要抓的误置形态。
_RE_CAPTION = re.compile(
    r"^[ \t]{0,3}(?:#{1,4}[ \t]*)?(?:\*\*)?(图|表)[ \t]*(\d+)[ \t]*[　\s:：、.．]", re.M)
#: markdown 图片：`![图 1](figure_1.svg)`
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)", re.M)
#: 正文交叉引用（「图 1 所示」「见表 2」「如图 3」），题注行不算。
_RE_XREF = re.compile(r"(?:见|如|由|参|按)\s*[图表]\s*(\d+)|[图表]\s*(\d+)\s*(?:所示|汇总|给出|列出|对比|说明)")
#: 不配图的显式理由（大纲允许写这个，不判缺）。
_RE_NO_VISUAL = re.compile(r"(?:本章|本节|该章).{0,12}(?:不配|无需|不用).{0,8}(?:图|表)|"
                           r"(?:不配图|无图表).{0,12}理由")


def _chapter_heads():
    """延迟取章节单源，避免常驻能力装配期把 shape 拉进依赖环。"""
    try:
        from rongke.shape import is_chapter_head
        return is_chapter_head
    except Exception:                                     # noqa: BLE001
        return None


def chapter_ranges(text: str) -> list[dict]:
    """正文（参考文献之前）的逐章范围。**附录区一律不算章**。"""
    t = str(text or "")
    m_refs = _RE_REFS.search(t)
    body_end = m_refs.start() if m_refs else len(t)
    heads = [m for m in _RE_HEAD.finditer(t) if m.start() < body_end]
    is_head = _chapter_heads()
    out: list[dict] = []
    for m in heads:
        line = m.group(0)
        title = m.group(1).strip()
        # 图表题注 / 附注 / 文档大标题不算章。
        if _RE_CAPTION.match(line) or title.startswith(("附注", "说明", "附录")):
            continue
        if is_head is not None and not is_head(line):
            continue
        out.append({"title": title, "start": m.start(), "head_end": m.end()})
    for i, c in enumerate(out):
        c["end"] = out[i + 1]["start"] if i + 1 < len(out) else body_end
    return out


def _caption_entries(text: str, kind: str) -> list[dict]:
    t = str(text or "")
    ref_at = _RE_REFS.search(t)
    refs_start = ref_at.start() if ref_at else len(t)
    out = []
    for m in _RE_CAPTION.finditer(t):
        if m.group(1) != kind:
            continue
        # 按行首/行尾重截一次，不用 m.start() 当行首（见正则上方注释）。
        lo = t.rfind("\n", 0, m.start()) + 1
        hi = t.find("\n", m.start())
        line = t[lo: hi if hi >= 0 else len(t)]
        out.append({"n": int(m.group(2)), "line": t.count("\n", 0, lo) + 1,
                    "caption": line.strip()[:160], "pos": lo,
                    "in_appendix": lo > refs_start})
    return out


def _xref_numbers(text: str, kind: str) -> set[int]:
    """正文（参考文献之前）的交叉引用编号；题注行不算。"""
    t = str(text or "")
    ref_at = _RE_REFS.search(t)
    body = t[: ref_at.start()] if ref_at else t
    body = "\n".join(l for l in body.splitlines()
                     if not _RE_CAPTION.match(l))
    nums: set[int] = set()
    for m in _RE_XREF.finditer(body):
        nums.add(int(m.group(1) or m.group(2)))
    return nums


def scan_visuals(text: str, root: "Path | str | None" = None,
                 *, expect_visual: bool = False) -> dict:
    """纯只读：图表锚点 ↔ 题注 ↔ 落盘文件 ↔ 正文引用 ↔ 附录误置。

    给 `visual_plan` / `visual_place` 的读回 / `readiness` 简报 / 判据
    共用**同一份**判据，不许各写一套。
    """
    t = str(text or "")
    figs = _caption_entries(t, "图")
    tabs = _caption_entries(t, "表")
    xrefs_f, xrefs_t = _xref_numbers(t, "图"), _xref_numbers(t, "表")
    chapters = chapter_ranges(t)
    covered: set[int] = set()
    for idx, c in enumerate(chapters):
        block = t[c["start"]:c["end"]]
        if _RE_CAPTION.search(block) or _RE_IMAGE.search(block) or _RE_NO_VISUAL.search(block):
            covered.add(idx)

    gaps: list[dict] = []
    # F 场实证（2026-11-10）：稿件 0 图 0 表时，上面所有判据都成立，
    # `ok=True / gaps=[]` 全绿放行 —— 「没有」混成了「没问题」。
    # 契约要配图时这必须是硬缺口，否则覆盖率等于没判据。
    if expect_visual and not (figs or tabs):
        gaps.append({"code": "VISUAL_EMPTY", "level": "hard",
                     "detail": "契约要求配图，但稿件里 0 张图、0 张表（图可能画在了工作区却没进正文）",
                     "action": "调 visual_place 把已画好的图落进对应章节，或由交付前整理自动落位"})
    for label, entries, xr in (("图", figs, xrefs_f), ("表", tabs, xrefs_t)):
        for e in entries:
            if e["in_appendix"]:
                gaps.append({"code": "VISUAL_APPENDIX", "level": "hard",
                             "detail": f"{label} {e['n']} 被排在「参考文献」之后（第 {e['line']} 行），没进正文",
                             "action": f"用 visual_place 把它落到对应章节，别当附录"})
            elif e["n"] not in xr:
                gaps.append({"code": "VISUAL_NO_BODY_REF", "level": "hard",
                             "detail": f"{label} {e['n']} 有题注，但正文里没有一处「见{label} {e['n']}」的呼应",
                             "action": "补一句这处图表支撑了什么论证，或让 visual_place 自动带一句"})
        # 同一张图写成「## 图 1」+「**图 1 说明…**」是常见写法，
        # 同号多行不算「编号不连续」；按 set 去重后再判。
        nums = sorted({e["n"] for e in entries})
        if nums and nums != list(range(1, len(nums) + 1)):
            gaps.append({"code": "VISUAL_NUMBER", "level": "soft",
                         "detail": f"{label}题注编号不连续：{nums}",
                         "action": "编号交给 visual_place 自动分配"})
        if len(entries) > len(nums):
            gaps.append({"code": "VISUAL_DUPLICATE_CAPTION", "level": "soft",
                         "detail": f"{label}题注有 {len(entries) - len(nums)} 处同号重复行",
                         "action": "同一张图只留一处题注（表上 / 图下）"})

    # 图片链接指向的文件在不在盘上（root omitted ⇒ 只报不查）
    files_missing: list[str] = []
    if root is not None:
        base = Path(root)
        for m in _RE_IMAGE.finditer(t):
            rel = str(m.group(1) or "").strip().split("#")[0]
            if not rel or rel.startswith(("http://", "https://", "data:")):
                continue
            q = (base / rel).resolve()
            if base.resolve() not in q.parents and q != base.resolve():
                continue
            if not q.is_file():
                files_missing.append(rel)
    if files_missing:
        gaps.append({"code": "VISUAL_MISSING_FILE", "level": "hard",
                     "detail": f"正文引用的图文件不存在：{files_missing[:5]}",
                     "action": "用 visual_place 重新生成，或修正链接"})

    counts = {
        "figures": len(figs), "tables": len(tabs),
        "chapters": len(chapters), "chapters_with_visual": len(covered),
        "figure_xrefs": len(xrefs_f), "table_xrefs": len(xrefs_t),
        "appendix_figures": sum(1 for e in figs if e["in_appendix"]),
        "appendix_tables": sum(1 for e in tabs if e["in_appendix"]),
        "files_missing": len(files_missing),
    }
    return {"ok": not any(g["level"] == "hard" for g in gaps),
            "counts": counts, "gaps": gaps,
            "figures": figs, "tables": tabs,
            "chapters": [c["title"] for c in chapters],
            "chapters_covered": sorted(covered),
            "advisory": True}


def next_number(text: str, kind: str) -> int:
    used = [e["n"] for e in _caption_entries(str(text or ""), kind)]
    return max(used, default=0) + 1


def _rows_from_data(data: str) -> tuple[list[dict], str]:
    """极简输入：JSON 数组或 markdown 表 → svgplot 的 rows + 表原文。"""
    raw = str(data or "").strip()
    if not raw:
        return [], ""
    rows: list[dict] = []
    if raw.startswith("["):
        try:
            obj = json.loads(raw)
            for item in obj if isinstance(obj, list) else []:
                if isinstance(item, dict):
                    label = str(item.get("label") or item.get("x") or item.get("name") or "")
                    val = item.get("value", item.get("y", item.get("n")))
                    group = str(item.get("group") or "")
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    label, val, group = str(item[0]), item[1], ""
                else:
                    continue
                rows.append({"x": label, "y": val, "group": group})
        except (ValueError, TypeError):
            rows = []
    if not rows and "|" in raw:
        lines = [l.strip() for l in raw.splitlines() if l.strip().startswith("|")]
        cells = [[c.strip() for c in l.strip("|").split("|")] for l in lines]
        cells = [c for c in cells if not all(re.fullmatch(r":?-{2,}:?", x or "") for x in c)]
        for c in cells[1:]:
            if len(c) >= 2:
                num = re.sub(r"[^\d.\-]", "", c[-1])
                try:
                    rows.append({"x": c[0], "y": float(num), "group": ""})
                except ValueError:
                    rows.append({"x": c[0], "y": 0.0, "group": ""})
    return rows, raw if "|" in raw else ""


def _safe_rel(root: Path, p: "Path | str") -> Path:
    return safe_workspace_path(p, root, allow_abs=False)


def _find_chapter(text: str, key: str) -> dict | None:
    ch = chapter_ranges(text)
    k = str(key or "").strip()
    if not ch or not k:
        return None
    for c in ch:
        if c["title"] == k or c["title"].startswith(k) or k in c["title"]:
            return c
    # 纯数字章号
    if k.isdigit():
        for c in ch:
            m = re.match(r"^\s*(\d+)", c["title"])
            if m and int(m.group(1)) == int(k):
                return c
    return None


def place_visual(text: str, *, chapter: str, kind: str, caption: str,
                 claim: str = "", data: str = "", file: str = "",
                 root: "Path | str | None" = None,
                 anchor_id: str = "") -> tuple[str, dict]:
    """原子动作：编号 → 生成/引用产物 → 插进指定章节 → 题注 + 正文呼应 → 读回。

    返回 `(新全文, 报告)`；任何一步失败都返回原文 + `ok=False`，绝不半改。
    """
    t = str(text or "")
    kind = "表" if str(kind).strip().lower() in ("table", "tab", "表") else "图"
    cap = str(caption or "").strip()
    root_p = Path(root) if root is not None else Path.cwd()
    rep: dict = {"ok": False, "kind": kind, "chapter": chapter,
                 "scope": "preview", "changed": False}

    if not cap:
        rep["error"] = "caption 不能为空（图注是论证的一部分）"
        return t, rep
    # 幂等：同章同图注已存在就不重复插。
    for e in _caption_entries(t, kind):
        if cap in e["caption"] and not e["in_appendix"]:
            rep.update({"ok": True, "reused": True,
                        "number": e["n"], "note": "该图注已在正文，未重复插入"})
            return t, rep
    c = _find_chapter(t, chapter)
    if c is None:
        rep["error"] = (f"找不到章节 {chapter!r}；可用："
                        + " / ".join(x["title"][:24] for x in chapter_ranges(t)[:12]))
        return t, rep

    n = next_number(t, kind)
    anchor = f"{kind} {n}"
    # 给了 anchor_id 就走「登记单源」路径：正文只放稳定锚点，编号由工具算。
    # 不给则**完全保持旧文本路径**（逐字节等价，现有判据一字不动）。
    rid = str(anchor_id or "").strip()
    if rid:
        prefix = "fig:" if kind == "图" else "tab:"
        rid = re.sub(r"[^A-Za-z0-9_.:-]", "-",
                     rid if rid.startswith(prefix) else prefix + rid)
    claim = str(claim or "").strip()
    claim_auto = not claim
    if claim_auto:
        claim = f"上述{('参数与证据汇总' if kind == '表' else '流程与证据关系')}见{anchor}。"

    rows, md_table = _rows_from_data(data)
    image = str(file or "").strip()
    made_file = ""
    if kind == "图":
        if not image:
            if not rows:
                rep["error"] = "figure 需要 data（JSON 数组或 markdown 表）或 file（已有图）"
                return t, rep
            image = f"figure_{n}.svg"
            try:
                from rongke.svgplot import render_svg
                out = _safe_rel(root_p, image)
                plan = {"kind": "bar", "x": "x", "y": "y", "title": cap,
                        "xlabel": "", "ylabel": "", "width_mm": 89.0,
                        "height_mm": 58.0}
                rr = render_svg(rows, plan, out)
                if not rr.get("ok"):
                    rep["error"] = f"预览图自检未过：{rr.get('errors') or rr}"
                    return t, rep
                made_file = image
            except Exception as e:                         # noqa: BLE001
                rep["error"] = f"生成预览图失败：{type(e).__name__}: {e}"
                return t, rep
        # 引用已有产物时校验路径，但不强制存在（可能由别的工具稍后生成）。
        try:
            _safe_rel(root_p, image)
        except ValueError as e:
            rep["error"] = str(e)
            return t, rep
        visual = f"![{rid or anchor}]({image})"
        caption_line = f"{anchor}　{cap}" + (f"<!--rk:{rid}-->" if rid else "")
        block = f"\n{claim}\n\n{visual}\n\n{caption_line}\n"
    else:
        if not md_table:
            if rows:
                md_table = ("| 项目 | 数值 |\n|---|---|\n"
                            + "\n".join(f"| {r['x']} | {r['y']} |" for r in rows))
            else:
                rep["error"] = "table 需要 data（markdown 表或 JSON 数组）"
                return t, rep
        caption_line = f"{anchor}　{cap}" + (f"<!--rk:{rid}-->" if rid else "")
        block = f"\n{claim}\n\n{caption_line}\n\n{md_table}\n"

    # 插到该章末尾（下一章标题之前），绝不在参考文献之后。
    insert_at = c["end"]
    while insert_at > c["head_end"] and t[insert_at - 1:insert_at] in ("\n", " ", "\t"):
        insert_at -= 1
    new = t[:insert_at].rstrip() + "\n" + block + "\n" + t[insert_at:].lstrip("\n")

    # ── 登记单源路径（只在给了 anchor_id 时）──────────────────────
    # 先在内存里把编号重算、五连读回全跑过，**最后才落盘**：任何一步
    # 不过就返回原文，盘上的登记一个字节都不动。
    reg_rep: dict = {}
    if rid:
        from rongke import figure_registry as FR
        cur, st = FR.load(root_p)
        if st.startswith("error:"):
            return t, dict(rep, error=f"现有登记表读不回来：{st}")
        entry = {"id": rid,
                 "kind": "figure" if kind == "图" else "table",
                 "caption": cap, "chapter": c["title"], "claim": claim,
                 "source": image if kind == "图" else "(inline)",
                 # 表格正文随登记一起存：整篇 write_file 覆盖后由
                 # figure_guard 照原样补回，不需要模型再敲一遍单元格。
                 "body": "" if kind == "图" else md_table,
                 "tier": "preview",
                 "qa": {"backend": "visual_desk", "ok": True}}
        trial = {"entries": [e for e in cur["entries"] if e["id"] != rid]
                 + [entry]}
        reg, errs = FR.validate(trial)
        if errs:
            return t, dict(rep, error="登记校验未过：" + "；".join(errs[:3]))
        new, rn = FR.renumber(new, reg)
        if not rn["ok"]:
            return t, dict(rep, error="登记重编号未过："
                           + (rn["note"] or str(rn["drift"])))
        clean, rr = FR.render_markdown(new, reg)
        vr = FR.verify(root_p, new, reg, rendered=clean)
        if not vr["ok"]:
            return t, dict(rep, error="五连读回未过：" +
                           "；".join(g["code"] for g in vr["gaps"][:4]))
        saved = FR.save(root_p, reg)
        if not saved["ok"]:
            return t, dict(rep, error="登记落盘失败：" + str(saved.get("error")))
        reg_rep = {"id": rid, "numbering": rn["numbering"],
                   "mixed_mode": rn["mixed_mode"],
                   "registry": saved["path"],
                   "readback_equal": saved["readback_equal"],
                   "verify_ok": vr["ok"], "render_ok": rr["ok"],
                   "renumber_note": rn["note"]}

    # 写后读回：编号、题注、呼应、位置、文件，缺一样就不算成功。
    back = scan_visuals(new, root_p)
    entries = back["figures"] if kind == "图" else back["tables"]
    # 登记路径下编号可能被 renumber 重算过，读回要用**实际交付的号**。
    want_n = reg_rep.get("numbering", {}).get(rid, n) if rid else n
    hit = next((e for e in entries if e["n"] == want_n
                and not e["in_appendix"]), None)
    if hit is None:
        rep["error"] = "写后读回失败：新题注没落进正文（或落到了参考文献之后）"
        return t, rep
    if want_n not in _xref_numbers(new, kind):
        rep["error"] = "写后读回失败：正文交叉引用没写进去"
        return t, rep
    # 只校验**本次**生成的图；原稿里其它缺失链接属于 scan_visuals 的报告，
    # 不该让一次合法插入被历史欠账挡住。
    if made_file and not _safe_rel(root_p, made_file).is_file():
        rep["error"] = "写后读回失败：刚生成的预览图不在盘上"
        return t, rep

    rep.update({"ok": True, "changed": True, "number": want_n,
                "anchor": f"{kind} {want_n}",
                "caption": f"{kind} {want_n}　{cap}"
                           + (f"<!--rk:{rid}-->" if rid else ""),
                "claim": claim, "claim_auto": claim_auto,
                "insert_at_line": hit["line"], "chapter_title": c["title"],
                "file": image, "made_file": made_file,
                "counts_after": back["counts"],
                "note": "预览级 SVG（svgplot 写后自检），不冒充投稿级；"
                        "投稿图请用 plot_data + audit_figure，再用本工具落位"})
    if reg_rep:
        rep["registry"] = reg_rep
        rep["note"] += ("；已登记稳定 id，删图/重排章节后调 visual_renumber "
                        "自动重编全篇引用")
    return new, rep
