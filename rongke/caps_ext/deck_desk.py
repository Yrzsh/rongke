# -*- coding: utf-8 -*-
"""幻灯片编排台 —— 给 agent 用的「一次成稿」原子能力（2026-11-10）。

## 为什么需要它（不是「再加几个工具」，是补一条断掉的链）

只读核过现网：`contract.DECK_TOOLCHAIN_RECIPE` 与 `_make_deck_reference()`
的「接下来怎么用」仍然点名 `create_presentation` / `add_slide` /
`manage_text` / `manage_image` / `save_presentation` —— 那是**已经退休的外部
PPT MCP 工具**（`workspace.js` 里连中文名都删了）。留下的 7 个 `deck_*`
工具没有一个能从零写出带正文的 .pptx：

  · `compose_deck`  只搬形状、套几何，**内核一个字都不写正文**；
  · `deck_from_template` 在既有品牌模板上填字，模板是本机 50MB 私有资产，
    不进仓库也不进发行件；
  · `deck_reference` 只给文本示范；`audit_deck` 只体检；`set_slide_notes`
    只写备注；`style_deck` 只换色换字。

⇒ 配方让模型「先建页再填字」，而建页工具**不在注册表里**。这不是模型笨，
是动作指向了一个不存在的工具。

## 落法：补「源稿」这一层，其余全部复用单源

`deck_reference_build.py::build_source()` 早就证明过：用 python-pptx 造一份
粗糙源稿（几何无所谓），再交给 `compose_deck` 精排，六种页型全过门。
所以这里**不抄几何、不抄字号、不抄精排**：

    结构化 spec（模型只写内容）
      → `_BUILD_SCRIPT` 造源稿（3.12 子进程 + python-pptx）
      → `deck.compose_deck` 精排（design.py 是几何/阶梯的唯一单源）
      → `deck.write_slide_notes` 写讲稿
      → 纯标准库读回 .pptx（页数 / 逐页文字 / 备注）
      → 自包含「幻灯片台.html」

## 边界（与 11-09 / 11-10 两次裁决一致）

  · **常驻加载**（`ALWAYS_ON`），不进预设、不加胶囊、删 .py 自动退回。
  · **不改 SYSTEM、不加硬门**：要求走工具回包，baseline 逐字节等价。
  · **能力不可用要明说**：找不到装了 python-pptx 的解释器时回
    `DECK_BACKEND_UNAVAILABLE`，不静默降级成「已完成」（11-10 D 场漏报同型）。
  · **写后读回才算数**：全流程在临时件上跑，全部通过才 copy 到交付件；
    任何一步失败，原 `deck.pptx` 一个字节都不动。
  · `advisory=True` 恒成立：本工具只给信息和产物，不接 CompletionGate。
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from rongke.caps import register
from rongke.state_store import atomic_write_text
from rongke.tool_sdk import DeskCapability, register_tool_specs, safe_workspace_path

ALWAYS_ON = True

_ID = "deck-desk"
_SECTION = "external-deck-desk"

#: 只在工具回包里出现，**刻意不进 SYSTEM**（baseline 逐字节等价判据钉着）。
VOICE = """【幻灯片编排台 · 常驻】
做 PPT 时你只写内容，几何/字号/主题由内核查表：
① 调 deck_plan() 拿 spec 模板与六种页型；
② 调 deck_make(spec=...) 一次成稿（造源稿→精排→讲稿备注→写后读回→幻灯片台）；
③ 改某一页调 deck_slide(index=..., ...)，别重写整份 spec；
④ 交付前照旧调 audit_deck。
不要再去调 create_presentation / add_slide / manage_text —— 那套外部 MCP 已退休，
现网注册表里没有它们。"""

#: 页型的中文名（给 HTML 台面和回包看，不参与判据）。
KIND_LABEL = {
    "cover": "封面", "section": "章节", "content": "内容",
    "two-col": "双栏", "chart": "图表", "closing": "收束",
}
#: 每型该写的字段（deck_plan 的模板从这里出，不另写一份）。
KIND_FIELDS = {
    "cover": ("title", "subtitle", "signature"),
    "section": ("title", "subtitle", "anchor"),
    "content": ("title", "bullets", "note"),
    "two-col": ("title", "left", "right", "note"),
    "chart": ("title", "bullets", "chart", "note"),
    "closing": ("title", "bullets", "note"),
}

MAX_SLIDES = 60
#: spec sidecar / 默认交付件名。
SPEC_NAME = "deck_spec.json"
DECK_NAME = "deck.pptx"
DESK_NAME = "幻灯片台.html"
WORK_NAME = ".deck_desk_work"

_A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
_RE_SLIDE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
_RE_NOTE = re.compile(r"ppt/notesSlides/notesSlide(\d+)\.xml$")


# ══════════════════════════════════════════════════════════════════════
# ① 造源稿脚本：跑在**装了 python-pptx 的独立解释器**里
# ══════════════════════════════════════════════════════════════════════
#
# 与 `deck.py` 的 6 个 r''' 脚本同一手法：内核零依赖，写操作外包给 3.12。
# 形状只用「真占位符 + 普通文本框」两种，因为 `_COMPOSE_SCRIPT.kind_of()`
# 只认形状/占位符类型、**不猜内容**；`deck_reference_build.py::build_source()`
# 已经用这份形状组合把六种页型全跑过门，不另发明一套。
_BUILD_SCRIPT = r'''
import json, sys
from pptx import Presentation
from pptx.util import Inches
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE


def set_ph(slide, idx, text):
    for ph in slide.placeholders:
        if int(ph.placeholder_format.idx) == idx:
            ph.text = text
            return True
    return False


def textbox(slide, text, w=4.0, h=2.0):
    # 源稿几何故意粗糙：精排会把它搬到 design.py 的网格上。
    tb = slide.shapes.add_textbox(Inches(0.6), Inches(3.0), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    lines = [x for x in str(text).split("\n") if x.strip()]
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = ln
    return tb


payload = json.loads(sys.stdin.read())
pages, out = payload["pages"], payload["out"]
prs = Presentation()
kinds = []
for n, pg in enumerate(pages, 1):
    kind = pg["kind"]
    lines = [str(x) for x in (pg.get("lines") or []) if str(x).strip()]
    title = str(pg.get("title") or "")

    if kind == "cover":
        # layout 0：idx0=ctrTitle，idx1=subTitle；其余文本框落 signature(other)
        sl = prs.slides.add_slide(prs.slide_layouts[0])
        sl.shapes.title.text = title
        if lines and not set_ph(sl, 1, lines[0]):
            raise RuntimeError("第 %d 页 cover 没有 subTitle 占位符" % n)
        if len(lines) > 1:
            textbox(sl, lines[1], 4.0, 0.6)
    elif kind in ("section", "content", "closing"):
        # layout 1：idx0=title，idx1=**真 body 占位符**（subtitle/body slot 只认它）
        sl = prs.slides.add_slide(prs.slide_layouts[1])
        sl.shapes.title.text = title
        body = "\n".join(lines[:1] if kind == "section" else lines)
        if body and not set_ph(sl, 1, body):
            raise RuntimeError("第 %d 页 %s 没有 body 占位符" % (n, kind))
        if kind == "section" and len(lines) > 1:
            textbox(sl, lines[1], 2.0, 1.6)   # 巨型编号 → anchor(other)
    else:
        # layout 5：仅标题。two-col 两个文本框 → body_left / body_right；
        # chart 一个真图表 → visual，一个文本框 → body。
        sl = prs.slides.add_slide(prs.slide_layouts[5])
        sl.shapes.title.text = title
        if kind == "chart":
            cs = pg.get("chart") or {}
            cd = CategoryChartData()
            cd.categories = [str(x) for x in (cs.get("categories") or [])]
            for s in (cs.get("series") or []):
                cd.add_series(str(s.get("name") or ""),
                              tuple(float(v) for v in (s.get("values") or [])))
            gf = sl.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,
                                     Inches(0.6), Inches(1.2),
                                     Inches(5.0), Inches(3.4), cd)
            gf.chart.has_title = True
            gf.chart.chart_title.text_frame.text = str(cs.get("title") or title)
            if lines:
                textbox(sl, "\n".join(lines), 3.0, 3.4)
        else:
            for ln in lines:
                textbox(sl, ln)
    kinds.append(kind)

prs.save(out)
print(json.dumps({"ok": True, "slides": len(kinds), "kinds": kinds},
                 ensure_ascii=False))
'''


# ══════════════════════════════════════════════════════════════════════
# ② spec 归一：模型只写内容，页型/几何一律由内核定
# ══════════════════════════════════════════════════════════════════════

def _lines(v) -> list[str]:
    """字符串（换行或中文分号分隔）/ 列表 → 非空行列表。"""
    if v is None:
        return []
    if isinstance(v, str):
        raw = re.split(r"[\n；;]", v)
    elif isinstance(v, (list, tuple)):
        raw = []
        for x in v:
            raw.extend(_lines(x))
    else:
        raw = [str(v)]
    return [str(x).strip() for x in raw if str(x).strip()]


def _kind_of(raw) -> str | None:
    """页型单源 = `design.resolve_kind()`，不在这里另写别名表。"""
    try:
        from rongke.design import resolve_kind
        return resolve_kind(str(raw or ""))
    except Exception:                                        # noqa: BLE001
        return None


def _norm_page(pg: dict, idx: int) -> tuple[dict | None, list[str]]:
    errs: list[str] = []
    if not isinstance(pg, dict):
        return None, [f"第 {idx} 页不是对象"]
    kind = _kind_of(pg.get("kind") or "content")
    if kind is None:
        return None, [f"第 {idx} 页页型 {pg.get('kind')!r} 认不出（用 cover/section/content/two-col/chart/closing）"]
    out = {"kind": kind, "title": str(pg.get("title") or "").strip()}
    if not out["title"]:
        errs.append(f"第 {idx} 页缺 title")

    if kind == "cover":
        out["subtitle"] = "\n".join(_lines(pg.get("subtitle")))
        out["signature"] = "\n".join(_lines(pg.get("signature")))
    elif kind == "section":
        out["subtitle"] = "\n".join(_lines(pg.get("subtitle")))
        out["anchor"] = "\n".join(_lines(pg.get("anchor") or str(idx)))
    elif kind == "two-col":
        out["left"] = _lines(pg.get("left"))
        out["right"] = _lines(pg.get("right"))
        if not out["left"] and not out["right"]:
            errs.append(f"第 {idx} 页 two-col 的 left/right 都空")
    else:
        out["bullets"] = _lines(pg.get("bullets") or pg.get("lines"))
        if not out["bullets"]:
            errs.append(f"第 {idx} 页 {kind} 缺 bullets（正文要点）")
        if kind == "chart":
            ch = pg.get("chart")
            if not isinstance(ch, dict) or not ch.get("categories") or not ch.get("series"):
                errs.append(f"第 {idx} 页 chart 缺 chart.categories/series（不编数据）")
            else:
                out["chart"] = {
                    "title": str(ch.get("title") or out["title"]),
                    "categories": [str(x) for x in ch["categories"]],
                    "series": [{"name": str(s.get("name") or ""),
                                "values": [float(v) for v in (s.get("values") or [])]}
                               for s in ch["series"] if isinstance(s, dict)],
                }

    out["note"] = " ".join(_lines(pg.get("note") or pg.get("notes")))
    return out, errs


def _pages_for_build(spec: dict) -> list[dict]:
    """spec → 造源稿用的 lines 序列（形状与 slot 一一对应）。"""
    pages = []
    for i, pg in enumerate(spec["slides"], 1):
        k = pg["kind"]
        b = {"kind": k, "title": pg["title"]}
        if k == "cover":
            b["lines"] = [pg.get("subtitle", ""), pg.get("signature", "")]
        elif k == "section":
            b["lines"] = [pg.get("subtitle", ""), pg.get("anchor", "") or str(i)]
        elif k == "two-col":
            b["lines"] = ["\n".join(pg.get("left") or []),
                          "\n".join(pg.get("right") or [])]
        elif k == "chart":
            b["lines"] = ["\n".join(pg.get("bullets") or [])]
            b["chart"] = pg.get("chart") or {}
        else:
            b["lines"] = list(pg.get("bullets") or [])
        pages.append(b)
    return pages


def _expected_texts(pg: dict) -> list[str]:
    """写后读回要比对的文字（每个非空字段都算，不给自己留假绿口子）。"""
    out = [pg["title"]]
    for key in ("subtitle", "signature", "anchor", "note"):
        v = pg.get(key)
        if v and key != "note":
            out.extend(_lines(v))
    for key in ("bullets", "left", "right"):
        out.extend(_lines(pg.get(key)))
    return [x for x in out if x]


def parse_lines(text: str) -> dict | None:
    """极简行格式（比 JSON 省 token）：`页型 | 标题 | 要点（；分隔） | 讲稿`。

    首行 `# 总标题`、次行 `> 副标题/署名` 可选。认不出就返回 None，
    由调用方回「spec 格式不对」，**不猜**。
    """
    lines = [l.strip() for l in str(text or "").splitlines() if l.strip()]
    if not lines:
        return None
    spec: dict = {"title": "", "subtitle": "", "footer": "", "slides": []}
    for ln in lines:
        if ln.startswith("# ") and not spec["slides"]:
            spec["title"] = ln[2:].strip()
            continue
        if ln.startswith("> ") and not spec["slides"]:
            spec["subtitle"] = ln[2:].strip()
            continue
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 2:
            return None
        kind_raw = parts[0]
        # 没写页型时，第一段当标题、整行当 content。
        if _kind_of(kind_raw) is None:
            parts = ["content"] + parts
        pg: dict = {"kind": parts[0], "title": parts[1]}
        if len(parts) > 2:
            pg["bullets"] = parts[2]
        if len(parts) > 3:
            pg["note"] = parts[3]
        if pg["kind"] in ("cover", "封面"):
            pg["subtitle"] = pg.pop("bullets", "")
            pg["signature"] = parts[4] if len(parts) > 4 else ""
        spec["slides"].append(pg)
    if not spec["slides"]:
        return None
    if not spec["title"]:
        spec["title"] = spec["slides"][0]["title"]
    # 第一页没写封面时不替模型加页（「不编内容」），只把总标题当页脚。
    spec["footer"] = spec.get("footer") or spec["title"]
    return spec


def resolve_spec(raw) -> tuple[dict | None, list[str]]:
    """JSON 字符串 / dict / 极简行格式 → 归一 spec。返回 (spec, errors)。"""
    obj = raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None, ["spec 为空"]
        if s.startswith(("{", "[")):
            try:
                obj = json.loads(s)
            except ValueError as e:
                return None, [f"spec 不是合法 JSON：{e}"]
        else:
            obj = parse_lines(s)
            if obj is None:
                return None, ["spec 既不是 JSON 也不是 `页型 | 标题 | 要点 | 讲稿` 行格式"]
    if isinstance(obj, list):
        obj = {"slides": obj}
    if not isinstance(obj, dict):
        return None, ["spec 必须是对象或数组"]
    slides = obj.get("slides")
    if not isinstance(slides, list) or not slides:
        return None, ["spec.slides 必须是非空数组"]
    if len(slides) > MAX_SLIDES:
        return None, [f"页数 {len(slides)} 超过上限 {MAX_SLIDES}"]

    norm, errs = [], []
    for i, pg in enumerate(slides, 1):
        p, e = _norm_page(pg, i)
        errs.extend(e)
        if p is not None:
            norm.append(p)
    if errs:
        return None, errs
    return {
        "title": str(obj.get("title") or "").strip(),
        "subtitle": str(obj.get("subtitle") or "").strip(),
        "footer": str(obj.get("footer") or obj.get("title") or "").strip(),
        "theme": str(obj.get("theme") or "juanqing").strip(),
        "slides": norm,
    }, []


# ══════════════════════════════════════════════════════════════════════
# ③ 纯标准库读回（zipfile + OOXML）——「写成功」不算数
# ══════════════════════════════════════════════════════════════════════

def read_deck_facts(path: "Path | str") -> dict:
    """只靠标准库量 .pptx：页数、逐页文字、备注份数与是否为空。

    刻意不 import python-pptx —— 这一层要能在**任何**解释器上跑，
    跟 `deck.probe_pptx` 同一个口径。
    """
    p = Path(path)
    rep: dict = {"ok": False, "path": str(p), "slides": [], "notes": 0,
                 "notes_nonempty": 0, "note_texts": [], "problem": ""}
    if not p.is_file():
        rep["problem"] = "文件不存在"
        return rep
    try:
        zf = zipfile.ZipFile(p)
    except Exception as e:                                    # noqa: BLE001
        rep["problem"] = f"打不开 pptx：{type(e).__name__}: {e}"
        return rep

    def _text(name: str) -> str:
        try:
            root = ET.fromstring(zf.read(name))
            return "".join((t.text or "") for t in root.iter(_A_T))
        except Exception:                                    # noqa: BLE001
            return ""

    sxml = sorted((n for n in zf.namelist() if _RE_SLIDE.search(n)),
                  key=lambda n: int(_RE_SLIDE.search(n).group(1)))
    nxml = [n for n in zf.namelist() if _RE_NOTE.search(n)]
    for n in sxml:
        rep["slides"].append({"index": len(rep["slides"]) + 1,
                              "text": _text(n), "chars": len(_text(n))})
    for n in sorted(nxml, key=lambda x: int(_RE_NOTE.search(x).group(1))):
        t = _text(n).strip()
        rep["notes"] += 1
        # ⚠️ 别用「>8 字符」猜有没有实质讲稿：实测「指着图说趋势。」「收尾。」
        #    这类 7 字 / 3 字讲稿会被判成空，于是 4 页写好的备注读回只剩 1 页。
        #    判据要量**语义**（这段 note 在不在某份 notesSlide 里），不是量长度。
        rep["note_texts"].append(t)
        if t:
            rep["notes_nonempty"] += 1
    zf.close()
    rep["ok"] = bool(sxml)
    if not sxml:
        rep["problem"] = "pptx 里没有 slide XML"
    return rep


def verify_readback(spec: dict, facts: dict) -> dict:
    """逐页比对 spec 的文字是否真写进了 .pptx，并量备注覆盖。"""
    gaps: list[dict] = []
    slides = facts.get("slides") or []
    if len(slides) != len(spec["slides"]):
        gaps.append({"code": "DECK_SLIDE_COUNT",
                     "detail": f"页数 {len(slides)} ≠ spec 的 {len(spec['slides'])}"})
    per_page: list[dict] = []
    for i, pg in enumerate(spec["slides"]):
        got = slides[i]["text"] if i < len(slides) else ""
        missing = [t for t in _expected_texts(pg) if t not in got]
        per_page.append({"index": i + 1, "kind": pg["kind"],
                         "title": pg["title"], "missing": missing})
        if missing:
            gaps.append({"code": "DECK_TEXT_MISSING",
                         "detail": f"第 {i+1} 页有 {len(missing)} 段文字没进 pptx：{missing[:2]}"})
    notes = facts.get("notes_nonempty", 0)
    note_texts = facts.get("note_texts") or []
    want = [pg["note"] for pg in spec["slides"] if pg.get("note")]
    missing_notes = [t for t in want
                     if not any(t in nt for nt in note_texts)]
    if missing_notes:
        gaps.append({"code": "DECK_NOTE_MISMATCH",
                     "detail": f"{len(want)} 页写了 note，读回有 {len(missing_notes)} 段讲稿没进备注："
                               + "、".join(x[:20] for x in missing_notes[:2])})
    return {"ok": not gaps, "pages": per_page, "gaps": gaps,
            "slides": len(slides), "notes_nonempty": notes,
            "notes_expected": len(want), "note_texts_checked": len(note_texts),
            "chars_total": sum(s["chars"] for s in slides)}


# ══════════════════════════════════════════════════════════════════════
# ④ 一次成稿：全流程在临时件上跑，成功才替换交付件
# ══════════════════════════════════════════════════════════════════════

def _safe_rel(root: Path, p: "Path | str") -> Path:
    return safe_workspace_path(p, root, allow_abs=False)


def _backend_python(explicit: str = "") -> str | None:
    """复用 `deck.find_notes_python()` 的**同一个**判据，不另写探针。"""
    try:
        from rongke import deck as _deck
        return _deck.find_notes_python(explicit or None)
    except Exception:                                        # noqa: BLE001
        return None


def build_deck(spec: dict, *, root: "Path | str", out: str = DECK_NAME,
               python_exe: str = "", desk: bool = True) -> dict:
    """spec → .pptx（造源稿 → 精排 → 备注 → 读回 → 台面）。

    任何一步失败：返回 `ok=False`，**交付件一字不动**。
    """
    root = Path(root)
    rep: dict = {"ok": False, "scope": "full", "advisory": True}
    try:
        target = _safe_rel(root, out or DECK_NAME)
    except ValueError as e:
        rep["error"] = str(e)
        return rep
    if str(target.suffix).lower() != ".pptx":
        rep["error"] = "out 必须是 .pptx"
        return rep

    py = _backend_python(python_exe)
    if py is None:
        # 11-10 D 场漏报同型：**能力不可用必须说出口**，不许静默降级。
        rep["error"] = ("DECK_BACKEND_UNAVAILABLE：找不到装了 python-pptx 的解释器，"
                        "无法生成 .pptx。别反复重试；请用 3.12 启动荣珂，或配置 deck.python。")
        return rep

    work = _safe_rel(root, WORK_NAME)
    try:
        work.mkdir(parents=True, exist_ok=True)
    except Exception as e:                                    # noqa: BLE001
        rep["error"] = f"建工作目录失败：{type(e).__name__}: {e}"
        return rep
    src = work / "source.pptx"
    tmp = work / "staged.pptx"

    # ① 造源稿
    payload = json.dumps({"pages": _pages_for_build(spec), "out": str(src)},
                         ensure_ascii=False)
    try:
        r = subprocess.run([py, "-c", _BUILD_SCRIPT], input=payload.encode("utf-8"),
                           capture_output=True, timeout=180)
    except Exception as e:                                    # noqa: BLE001
        rep["error"] = f"造源稿子进程没跑起来：{type(e).__name__}: {e}"
        return rep
    if r.returncode != 0:
        rep["error"] = "造源稿失败：" + (r.stderr.decode("utf-8", "replace")[-600:]
                                    or r.stdout.decode("utf-8", "replace")[-300:])
        return rep
    rep["source_slides"] = len(spec["slides"])

    # ② 精排（在临时副本上，就地语义不碰交付件）
    try:
        shutil.copy2(src, tmp)
        from rongke import deck as _deck
        kinds = [{"kind": pg["kind"]} for pg in spec["slides"]]
        cr = _deck.compose_deck(
            tmp, kinds, theme=spec.get("theme") or "juanqing", footer=True,
            footer_label=spec.get("footer") or spec.get("title") or "",
            workdir=root, python_exe=py, cfg=None, timeout=240)
    except Exception as e:                                    # noqa: BLE001
        rep["error"] = f"精排出错：{type(e).__name__}: {e}"
        return rep
    rep["compose"] = {"ok": bool(cr.ok), "kinds": list(cr.kinds),
                      "visuals_before": cr.visuals_before,
                      "visuals_after": cr.visuals_after,
                      "align_text": cr.align_text,
                      "problems": list(cr.problems)[:3]}
    if not cr.ok:
        rep["error"] = "精排未过：" + " / ".join(str(x)[:160] for x in (cr.problems or [])[:2])
        return rep
    if cr.visuals_after < cr.visuals_before:
        rep["error"] = "精排丢了视觉元素（图/表/图表）"
        return rep

    # ③ 讲稿备注（必须在精排之后：精排会重写整包）
    notes = [pg.get("note") or "" for pg in spec["slides"]]
    try:
        nr = _deck.write_slide_notes(tmp, notes, workdir=root,
                                     python_exe=py, cfg=None, timeout=180)
        # 字段名照 `NotesWrite` 的量测结果取，不用想当然的 "filled"
        # （取不到就变 None，是 11-10「静默降级」同型的假信息）。
        rep["notes"] = {"ok": bool(nr.ok),
                        "requested": nr.requested, "slides": nr.slides,
                        "wrote": nr.wrote, "notes_ratio": nr.notes_ratio,
                        "no_notes_slides": list(nr.no_notes_slides or []),
                        "failed_slides": list(nr.failed_slides or [])[:3],
                        "mismatches": list(nr.mismatches or [])[:3],
                        "unverified": nr.unverified}
        if not nr.ok:
            rep["notes"]["problems"] = list(nr.problems or [])[:2]
    except Exception as e:                                    # noqa: BLE001
        rep["notes"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ④ 写后读回（唯一算数的证据）
    facts = read_deck_facts(tmp)
    rb = verify_readback(spec, facts)
    rep["readback"] = rb
    if not rb["ok"]:
        rep["error"] = "写后读回不通过：" + " / ".join(g["detail"][:120] for g in rb["gaps"][:2])
        return rep

    # ⑤ 全部通过才替换交付件；同时落 spec sidecar（deck_slide 的增量单源）
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp, target)
        atomic_write_text(
            _safe_rel(root, SPEC_NAME),
            json.dumps(spec, ensure_ascii=False, indent=2),
        )
    except Exception as e:                                    # noqa: BLE001
        rep["error"] = f"落盘失败：{type(e).__name__}: {e}"
        return rep
    rep["pptx"] = str(target)
    rep["spec"] = str(_safe_rel(root, SPEC_NAME))
    rep["readback_equal"] = (read_deck_facts(target)["slides"] == facts["slides"])

    if desk:
        try:
            page = _safe_rel(root, DESK_NAME)
            atomic_write_text(page, render_desk_html(spec, facts, target.name))
            rep["desk"] = str(page)
        except Exception as e:                                # noqa: BLE001
            rep["desk_error"] = f"{type(e).__name__}: {e}"

    rep["ok"] = True
    rep["gaps"] = []
    rep["next"] = ("稿已成形。交付前照旧调 audit_deck(pptx=...) 体检；"
                   "要改某一页用 deck_slide(index=N, ...)，别重写整份 spec。")
    return rep


# ══════════════════════════════════════════════════════════════════════
# ⑤ 幻灯片台：自包含 HTML（产物列表点开即页面）
# ══════════════════════════════════════════════════════════════════════

def render_desk_html(spec: dict, facts: dict, deck_name: str = DECK_NAME) -> str:
    """绢青陈墨 · 逐页台面。纯字符串拼装，不引任何外部资源。"""
    e = html.escape
    slides = spec.get("slides") or []
    got = facts.get("slides") or []
    notes = facts.get("notes_nonempty", 0)
    cards = []
    for i, pg in enumerate(slides, 1):
        k = pg.get("kind", "content")
        items: list[str] = []
        for key in ("subtitle", "signature", "anchor", "bullets", "left", "right"):
            v = pg.get(key)
            if not v:
                continue
            tag = {"left": "左栏", "right": "右栏", "subtitle": "副标题",
                   "signature": "署名", "anchor": "编号"}.get(key, "")
            prefix = f"<i>{e(tag)}</i> " if tag else ""
            for x in _lines(v):
                items.append(f"<li>{prefix}{e(x)}</li>")
        missing = [t for t in _expected_texts(pg)
                   if t not in (got[i - 1]["text"] if i <= len(got) else "")]
        warn = (f'<div class="warn">读回缺 {len(missing)} 段：{e("、".join(missing[:2]))}</div>'
                if missing else "")
        note = pg.get("note") or ""
        cards.append(f"""
        <article class="card">
          <div class="num">{i:02d}</div>
          <div class="body">
            <div class="meta"><span class="kind">{e(KIND_LABEL.get(k, k))}</span>
              <span class="chars">{(got[i-1]['chars'] if i <= len(got) else 0)} 字</span></div>
            <h3>{e(pg.get('title') or '(无标题)')}</h3>
            <ul>{''.join(items)}</ul>
            {warn}
            <div class="note"><b>讲稿</b> {e(note) if note else '<span class="muted">（本页没写 note）</span>'}</div>
          </div>
        </article>""")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>幻灯片台 · {e(spec.get('title') or '未命名')}</title>
<style>
:root{{--paper:#f6f3ec;--ink:#22303a;--ink2:#5a6a72;--rule:#d8d2c4;--accent:#a8442f}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);
 font:15px/1.7 "Microsoft YaHei","PingFang SC",serif}}
header{{padding:30px 6vw 22px;border-bottom:2px solid var(--accent)}}
h1{{margin:0 0 6px;font-size:26px;letter-spacing:.06em}}
.sub{{color:var(--ink2);font-size:13px}}
.stats{{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}}
.stat{{background:#fff;border:1px solid var(--rule);padding:7px 13px;font-size:13px}}
.stat b{{color:var(--accent);font-size:17px;margin-right:4px}}
main{{padding:26px 6vw 60px;display:grid;gap:16px;
 grid-template-columns:repeat(auto-fill,minmax(360px,1fr))}}
.card{{display:flex;gap:14px;background:#fff;border:1px solid var(--rule);
 padding:16px 18px;box-shadow:0 1px 0 rgba(0,0,0,.03)}}
.num{{font:600 22px/1 Georgia,serif;color:var(--accent);min-width:34px}}
.body{{flex:1;min-width:0}}.meta{{display:flex;gap:10px;align-items:center;margin-bottom:5px}}
.kind{{background:#eae4d6;color:#6c5b3e;font-size:11px;padding:2px 8px}}
.chars{{color:var(--ink2);font-size:11px}}
h3{{margin:2px 0 8px;font-size:17px}}ul{{margin:0;padding-left:18px;color:var(--ink2);font-size:13px}}
li{{margin:2px 0}}li i{{font-style:normal;color:var(--accent);font-size:11px}}
.note{{margin-top:10px;padding-top:8px;border-top:1px dashed var(--rule);
 font-size:12px;color:var(--ink2)}}.note b{{color:var(--ink)}}
.muted{{color:#aaa}}.warn{{margin-top:8px;color:#a8442f;font-size:12px}}
footer{{padding:18px 6vw 40px;color:var(--ink2);font-size:12px;
 border-top:1px solid var(--rule)}}
</style></head><body>
<header><h1>幻灯片台 · {e(spec.get('title') or '未命名')}</h1>
<div class="sub">交付件 {e(deck_name)} · 几何/字号由 design.py 单源精排 · 本页是文字台面，不是渲染预览</div>
<div class="stats">
 <div class="stat"><b>{len(slides)}</b>页</div>
 <div class="stat"><b>{len({pg.get('kind') for pg in slides})}</b>种页型</div>
 <div class="stat"><b>{notes}</b>页有讲稿</div>
 <div class="stat"><b>{(facts.get('chars_total') or sum(x['chars'] for x in got))}</b>字读回</div>
</div></header>
<main>{''.join(cards)}</main>
<footer>只读读回 · 写成功不算数 · 交付前请调 audit_deck 体检</footer>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════
# ⑥ 四个常驻工具
# ══════════════════════════════════════════════════════════════════════

def _register_tools(ctx: dict, cfg: dict) -> list[str]:
    registry = ctx.get("registry")
    if registry is None:
        return []

    def _root() -> Path:
        return Path.cwd()

    def _read_spec(raw: str) -> tuple[dict | None, list[str], str]:
        root = _root()
        s = str(raw or "").strip()
        src = "调用参数"
        if not s:
            f = root / SPEC_NAME
            if f.is_file():
                s, src = f.read_text(encoding="utf-8"), str(f)
            else:
                return None, [f"没给 spec，工作区里也没有 {SPEC_NAME}；先调 deck_plan() 拿模板"], ""
        elif not s.startswith(("{", "[", "#")) and len(s) < 260:
            # 可能是 .json 路径
            try:
                q = _safe_rel(root, s)
                if q.is_file():
                    s, src = q.read_text(encoding="utf-8"), str(q)
            except (ValueError, OSError):
                pass
        spec, errs = resolve_spec(s)
        return spec, errs, src

    def _fn_plan() -> str:
        root = _root()
        template = {
            "title": "汇报总标题", "subtitle": "副标题", "footer": "组会 / 落款",
            "slides": [
                {"kind": "cover", "title": "汇报总标题", "subtitle": "一句话说明", "signature": "汇报人 · 日期"},
                {"kind": "section", "title": "第一部分", "subtitle": "这部分讲什么", "anchor": "01"},
                {"kind": "content", "title": "研究背景", "bullets": ["要点一", "要点二", "要点三"], "note": "这一页要讲的口语稿"},
                {"kind": "chart", "title": "关键结果",
                 "bullets": ["图里最该被记住的一句结论"],
                 "chart": {"categories": ["A", "B", "C"],
                           "series": [{"name": "处理组", "values": [1.2, 2.4, 3.1]}]},
                 "note": "指着图说趋势"},
                {"kind": "closing", "title": "结论与下一步", "bullets": ["结论一", "下一步"], "note": "收尾"},
            ],
        }
        state = {n: (root / n).exists() for n in (SPEC_NAME, DECK_NAME, DESK_NAME)}
        backend = _backend_python()
        return json.dumps({
            "ok": True, "advisory": True, "source": "deck_plan",
            "kinds": {k: list(KIND_FIELDS[k]) for k in KIND_LABEL},
            "template": template,
            "template_json": json.dumps(template, ensure_ascii=False),
            "line_format": "也可每行一页：cover | 标题 | 副标题 | 署名 ／ content | 标题 | 要点一；要点二 | 讲稿",
            "reference_summary": _reference_summary(),
            "workspace": state,
            "backend": {"python_pptx": bool(backend), "python_exe": backend or ""},
            "next": ("把上面的 template 改成你的内容，调 deck_make(spec=<JSON 或行格式>)；"
                     "几何/字号/主题由内核查表，你只写文字。"
                     "改某一页用 deck_slide(index=N, ...)。"),
            "rules": VOICE,
        }, ensure_ascii=False)

    def _fn_make(spec: str = "", out: str = DECK_NAME, desk: bool = True) -> str:
        obj, errs, src = _read_spec(spec)
        if obj is None:
            return json.dumps({"ok": False, "error": "；".join(errs[:4]),
                               "hint": "先调 deck_plan() 拿模板"}, ensure_ascii=False)
        rep = build_deck(obj, root=_root(), out=out, desk=bool(desk))
        rep["source"] = src
        return json.dumps(rep, ensure_ascii=False)

    def _fn_slide(index: int = 0, op: str = "set", title: str = "",
                  bullets: str = "", note: str = "", kind: str = "",
                  page: str = "") -> str:
        root = _root()
        f = root / SPEC_NAME
        if not f.is_file():
            return json.dumps({"ok": False, "error": f"没有 {SPEC_NAME}；先调 deck_make 出一次稿"},
                              ensure_ascii=False)
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            return json.dumps({"ok": False, "error": f"spec sidecar 坏了：{e}"}, ensure_ascii=False)
        slides = obj.get("slides") or []
        op = str(op or "set").strip().lower()
        n = int(index or 0)

        if op == "insert":
            raw = str(page or "").strip()
            if not raw:
                raw = json.dumps({"kind": kind or "content", "title": title,
                                  "bullets": bullets, "note": note}, ensure_ascii=False)
            pg, errs = _norm_page(json.loads(raw) if raw.startswith("{") else
                                  {"kind": kind or "content", "title": title,
                                   "bullets": bullets, "note": note}, len(slides) + 1)
            if errs or pg is None:
                return json.dumps({"ok": False, "error": "；".join(errs[:3])}, ensure_ascii=False)
            at = max(1, min(n, len(slides) + 1)) - 1
            slides.insert(at, pg)
        elif op == "delete":
            if not (1 <= n <= len(slides)):
                return json.dumps({"ok": False, "error": f"index {n} 越界（共 {len(slides)} 页）"},
                                  ensure_ascii=False)
            slides.pop(n - 1)
        else:
            if not (1 <= n <= len(slides)):
                return json.dumps({"ok": False, "error": f"index {n} 越界（共 {len(slides)} 页）"},
                                  ensure_ascii=False)
            pg = dict(slides[n - 1])
            if title:
                pg["title"] = str(title)
            if bullets:
                pg["bullets"] = _lines(bullets)
                pg.pop("left", None)
                pg.pop("right", None)
            if note:
                pg["note"] = " ".join(_lines(note))
            if kind:
                k = _kind_of(kind)
                if k is None:
                    return json.dumps({"ok": False, "error": f"页型 {kind!r} 认不出"}, ensure_ascii=False)
                pg["kind"] = k
            norm, errs = _norm_page(pg, n)
            if errs or norm is None:
                return json.dumps({"ok": False, "error": "；".join(errs[:3])}, ensure_ascii=False)
            slides[n - 1] = norm

        obj["slides"] = slides
        # 全量重出，但**模型不必重写内容**：spec sidecar 是唯一单源。
        rep = build_deck(obj, root=root, out=DECK_NAME, desk=True)
        rep["op"], rep["index"] = op, n
        return json.dumps(rep, ensure_ascii=False)

    def _fn_desk(pptx: str = DECK_NAME) -> str:
        root = _root()
        try:
            p = _safe_rel(root, pptx or DECK_NAME)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        facts = read_deck_facts(p)
        if not facts["ok"]:
            return json.dumps({"ok": False, "error": facts["problem"],
                               "hint": "先调 deck_make 出稿"}, ensure_ascii=False)
        sf = root / SPEC_NAME
        spec = None
        if sf.is_file():
            try:
                spec = json.loads(sf.read_text(encoding="utf-8"))
            except ValueError:
                spec = None
        if spec is None or len(spec.get("slides") or []) != len(facts["slides"]):
            spec = {"title": p.stem, "subtitle": "", "footer": "", "slides": [
                {"kind": "content", "title": f"第 {s['index']} 页",
                 "bullets": [], "note": ""} for s in facts["slides"]]}
        gaps = []
        if facts["notes_nonempty"] < len(facts["slides"]):
            gaps.append({"code": "DECK_NOTES_PARTIAL", "level": "soft",
                         "detail": f"{len(facts['slides'])} 页里只有 {facts['notes_nonempty']} 页备注有内容",
                         "action": "用 deck_slide(index=N, note=...) 补讲稿"})
        empties = [s["index"] for s in facts["slides"] if s["chars"] < 6]
        if empties:
            gaps.append({"code": "DECK_EMPTY_SLIDE", "level": "hard",
                         "detail": f"第 {empties[:5]} 页几乎没有文字",
                         "action": "用 deck_slide 补内容或删掉该页"})
        page = _safe_rel(root, DESK_NAME)
        atomic_write_text(page, render_desk_html(spec, facts, p.name))
        return json.dumps({"ok": True, "page": str(page), "pptx": str(p),
                           "slides": len(facts["slides"]),
                           "notes_nonempty": facts["notes_nonempty"],
                           "gaps": gaps, "advisory": True,
                           "next": "交付前照旧调 audit_deck(pptx=...) 体检；本台面只给信息，不加门"},
                          ensure_ascii=False)

    specs = [
        ("deck_plan",
         "做 PPT 之前先调一次（零参数）：拿到可直接改的 spec 模板、六种页型各自的字段、"
         "随库参考样例摘要、工作区现状，以及下一步该调哪个工具。只读。",
         {}, [], _fn_plan, False),
        ("deck_make",
         "一次成稿：给一份只含内容的 spec（JSON 或每行一页的极简格式），"
         "自动造源稿 → 按 design.py 精排 → 写讲稿备注 → 写后读回逐页核对文字 → "
         "生成幻灯片台.html。几何/字号/主题由内核查表，你不写绘图代码、不记页型坐标。"
         "spec 留空时自动读工作区 deck_spec.json。失败时原 deck.pptx 一字不动。",
         {"spec": {"type": "string", "description": "spec JSON 字符串、.json 路径，或 `cover | 标题 | 副标题 | 署名` 行格式；留空自动读 deck_spec.json"},
          "out": {"type": "string", "description": "输出 .pptx 路径，默认 deck.pptx"},
          "desk": {"type": "boolean", "description": "是否同时生成 幻灯片台.html，默认 true"}},
         [], _fn_make, True),
        ("deck_slide",
         "改某一页而不用重写整份 spec：set 改标题/要点/讲稿/页型，insert 插一页，delete 删一页。"
         "改完自动重出 .pptx 并写后读回。index 从 1 开始。",
         {"index": {"type": "integer", "description": "页序，从 1 开始"},
          "op": {"type": "string", "enum": ["set", "insert", "delete"], "description": "默认 set"},
          "title": {"type": "string", "description": "新标题"},
          "bullets": {"type": "string", "description": "新要点，换行或中文分号分隔"},
          "note": {"type": "string", "description": "新讲稿备注"},
          "kind": {"type": "string", "description": "新页型（cover/section/content/two-col/chart/closing）"},
          "page": {"type": "string", "description": "op=insert 时整页的 JSON"}},
         [], _fn_slide, True),
        ("deck_desk",
         "从现有 .pptx 生成自包含「幻灯片台.html」逐页文字台面，并报出空页 / 讲稿缺口"
         "（只读 + 只写台面，不碰 pptx）。纯标准库，不需要 python-pptx。",
         {"pptx": {"type": "string", "description": "要读的 .pptx，默认 deck.pptx"}},
         [], _fn_desk, True),
    ]
    return register_tool_specs(registry, specs)


def _reference_summary(limit: int = 900) -> str:
    """只读复用 `deckref.reference_text()` 的摘要，不另写一份示范。"""
    try:
        from rongke import deckref
        t = deckref.reference_text(summary=True)
        return t[:limit]
    except Exception as e:                                    # noqa: BLE001
        return f"（参考摘要不可用：{type(e).__name__}: {e}）"


@register("@external/deck-desk")
class DeckDesk(DeskCapability):
    ALWAYS_ON = True
    id = _ID
    kind = "tool"
    version = "0.1.0"
    config_schema = {}
    ALWAYS_CONFIG = {}
    SECTION_TEXTS = {_SECTION: VOICE}
    ESSENTIAL_TOOLS = ("deck_plan", "deck_make", "deck_slide", "deck_desk")

    def mount(self, cfg, ctx):
        self.cfg = dict(cfg or {})
        self.added = _register_tools(ctx, self.cfg)
        return [f"always-on:{self.id}:{n}" for n in self.added]

    def provides(self):
        return [f"always-on:{self.id}:{n}" for n in getattr(self, "added", ())]
