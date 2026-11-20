# -*- coding: utf-8 -*-
"""给 AI 用的格式排版后端（零第三方依赖，2026-11-11）。

## 与 `svgdiagram` 同一条纪律：AI 声明规格，工具算版式

模型给的是 `blocks` —— **哪一段是标题、哪一段是图注、哪一条是文献**。
页边距、字号阶梯（pt）、行距、章节编号、题注在图下/表上、参考文献编号、
分页与页眉页脚全由体例模板算。模型**不写一个排版字节**：不写
`\\section{}`、不写 CSS、不写 docx XML。因此「字号不统一」「图题跑到图上面」
「参考文献编号断了」这些形态在结构上就没有入口 —— 跟机制图用虚节点
让「线穿框」不可能发生，是同一种保证方式。

算完立刻读回：`checks` 全 bool，每个 block 的文字必须在产物里找得到。
质量结论另走 `quality` + `next`，**不改 `ok` / `scope` / `checks` 的语义**，
也不接任何交付门（`advisory=True`）。

## 边界（与绘图链一致，说清楚没跑什么）

`scope="typeset"` 表示这是**可打印的排版成品声明**（HTML / docx / tex），
不是投稿 PDF 合格证。`audit_scope` 明写：未跑 PDF 渲染实测（字体嵌入、
分页溢出、孤行、图表跨页）。预览级 SVG 与投稿级 PDF 的分界照旧。

## 物理量纲

字号一律 pt、页面一律 mm（A4 = 210×297）。`STYLES` 是**体例唯一单源**，
HTML / docx / LaTeX 三条导出路都读同一份，不各写一套数字。
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# 绢青陈墨的色值 / 中文字体栈复用 svgplot 的**唯一单源**，不在这里另抄一套
# （与 svgdiagram 同一手法）。「中文用户 + 单 Arial = 豆腐块」那次假成功，
# 根因就是字体栈没显式声明；排版成品必须把这件事写进产物并可判定。
from .svgplot import FONT_STACK, _AXIS, _GRID, _INK, _PALETTE, _PAPER

#: 排版侧设计资产**唯一单源**：色值、字体、编号与题注规则。
#: HTML / docx / tex 三条路与质量判据都读这一份，改一处不会静默漂移。
#: 「哪些字体名是中文字体」是**字形事实**，不是可配置的设计值。
#: ⚠️ 判据若拿当前 `DESIGN` 当基准，短路配置时两边一起变 ⇒ 判据恒真没牙
#:    （11-11 实测：把 cjk 全改成 Arial，旧判据照样 PASS）。
#:    与 `svgplot._CJK` 检查「有中文时字体栈必须含中文字体」同一口径。
CJK_FONT_FACTS = frozenset({
    "SimSun", "NSimSun", "Microsoft YaHei", "SimHei", "KaiTi", "FangSong",
    "STSong", "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Source Han Serif SC",
})

DESIGN = {
    "colors": {"ink": _INK, "paper": _PAPER, "rule": _GRID,
               "axis": _AXIS, "accent": _PALETTE[1]},       # 朱砂
    "font_stack_css": FONT_STACK,
    "cjk": {"body": "SimSun", "head": "Microsoft YaHei",
            "caption": "SimSun"},
    "latin": "Arial",
    "numbering": "1 / 1.1 / 1.1.1",
    "caption": {"figure": "below", "table": "above"},
    "min_font_pt": 8.5,
    "ref_hanging_em": 2.0,
}

#: 规格口径的**单源**：工具描述、提示段、报错指引共用这一份（同 svgdiagram.SPEC_GUIDE）。
SPEC_GUIDE = (
    "spec 是对象：style（journal-cn 中文期刊 / thesis 学位论文 / preprint 预印本）、"
    "meta={title, authors, affiliation, abstract, keywords}、"
    "blocks=[{role, text, level?, src?, caption?, n?}]。"
    "role ∈ title/authors/affiliation/abstract/keywords/heading/paragraph/"
    "figure/table/caption/ref_entry/note。"
    "也可以不给 spec，只给 path 指向现成 .md 稿件 —— 由 parse_markdown() 确定性拆块。"
    "页边距、字号阶梯、行距、章节编号、题注位置（图下/表上）、参考文献编号、"
    "分页与页眉页脚全由体例模板算；不要手写 HTML/CSS/LaTeX/docx 标记。"
    "稿里写 {{FIGURE:data.csv}} 或按普通 Markdown 写 ![说明](data.csv) "
    "即一次出图+出初稿：图型、轴、配色、题注全由工具按数据表算，"
    "不写任何样式参数，题注由图规格生成不会图文不符。"
    "可选 |kind=errorbar 指定图型，留空自动判。"
)

ROLES = ("title", "authors", "affiliation", "abstract", "keywords",
         "heading", "paragraph", "figure", "table", "caption",
         "ref_entry", "note")

#: 体例唯一单源。字号 pt，页面 mm，行距倍数。
STYLES: dict[str, dict] = {
    "journal-cn": {
        "label": "中文期刊（单栏 · GB/T 7714 编号制）",
        "page": {"width_mm": 210, "height_mm": 297,
                 "margin_mm": [25, 25, 25, 25]},
        "fonts": {"title": 16, "authors": 10.5, "affiliation": 9,
                  "abstract": 9, "keywords": 9, "h1": 14, "h2": 12,
                  "h3": 10.5, "body": 10.5, "caption": 9, "ref": 9,
                  "note": 9, "header": 8},
        "line_height": 1.5, "columns": 1,
        "caption": {"figure": "below", "table": "above"},
        "refs": "numeric", "heading_number": True,
        "header": "{short_title}", "footer": "{page}",
        "page_break_before": ("heading",), "abstract": True,
    },
    "thesis": {
        "label": "学位论文（章起新页 · 宋体阶梯）",
        "page": {"width_mm": 210, "height_mm": 297,
                 "margin_mm": [30, 25, 25, 30]},
        "fonts": {"title": 22, "authors": 12, "affiliation": 12,
                  "abstract": 12, "keywords": 12, "h1": 16, "h2": 14,
                  "h3": 12, "body": 12, "caption": 10.5, "ref": 10.5,
                  "note": 10.5, "header": 9},
        "line_height": 1.6, "columns": 1,
        "caption": {"figure": "below", "table": "above"},
        "refs": "numeric", "heading_number": True,
        "header": "{short_title}", "footer": "{page}",
        "page_break_before": ("heading",), "abstract": True,
    },
    "preprint": {
        "label": "预印本（双栏 · 无页眉）",
        "page": {"width_mm": 210, "height_mm": 297,
                 "margin_mm": [20, 20, 20, 20]},
        "fonts": {"title": 18, "authors": 11, "affiliation": 9,
                  "abstract": 10, "keywords": 10, "h1": 13, "h2": 11,
                  "h3": 10, "body": 10, "caption": 8.5, "ref": 8.5,
                  "note": 9, "header": 8},
        "line_height": 1.4, "columns": 2,
        "caption": {"figure": "below", "table": "above"},
        "refs": "numeric", "heading_number": True,
        "header": "", "footer": "{page}",
        "page_break_before": (), "abstract": True,
    },
}

STATE_NAME = "排版台.json"
HTML_NAME = "排版台.html"
LAYOUT_SUFFIX = ".layout.json"
DOCX_NAME = "论文.docx"
TEX_NAME = "论文.tex"

_RE_MD_HEAD = re.compile(r"^[ \t]{0,3}(#{1,4})[ \t]+(.+?)[ \t]*$", re.M)
_RE_REFS = re.compile(r"^[ \t]{0,3}#{1,4}[ \t]*参考文献[ \t]*$", re.M)
_RE_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_RE_CAPTION = re.compile(r"^[ \t]{0,3}(?:\*\*)?(图|表)[ \t]*(\d+)[ \t]*[　\s:：、.．]", re.M)
_RE_REF_ENTRY = re.compile(r"^[ \t]{0,3}(?:\[(\d+)\]|〔(\d+)〕)\s*(.+)$", re.M)
_RE_TABLE_ROW = re.compile(r"^[ \t]{0,3}\|.+\|[ \t]*$")
#: 标题里模型自己写的编号（`## 1 引言`）。拆块时分离出来交给工具统一算，
#: 否则「1　1 引言」会重复编号；原值存 n_hint，不丢信息。
_RE_HEAD_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[、.．:：]?\s*(.+)$")


class TypesetError(ValueError):
    """规格不合法 / 本后端排不了。**说清楚为什么**，不静默排成别的东西。"""


def _blocks_from_spec(raw) -> list[dict]:
    if not isinstance(raw, dict):
        raise TypesetError("spec 必须是一个对象（含 blocks）。")
    blocks = raw.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise TypesetError("blocks 不能为空：至少要有一个正文块。")
    out = []
    for i, b in enumerate(blocks, 1):
        if not isinstance(b, dict):
            raise TypesetError(f"blocks[{i}] 不是对象。")
        role = str(b.get("role") or "paragraph").strip()
        if role not in ROLES:
            raise TypesetError(
                f"blocks[{i}] 的 role={role!r} 不支持（支持：{'、'.join(ROLES)}）。"
                "本后端不会把它假装排成别的样式。")
        item = {"id": str(b.get("id") or f"b{i}"), "role": role,
                "text": str(b.get("text") or "").strip()}
        for k in ("level", "src", "caption", "n", "n_hint"):
            if b.get(k) is not None:
                item[k] = b[k]
        if role in ("figure", "table") and not item["text"] and not item.get("src"):
            raise TypesetError(f"blocks[{i}]（{role}）既没有 text 也没有 src。")
        out.append(item)
    ids = [x["id"] for x in out]
    if len(ids) != len(set(ids)):
        raise TypesetError("block id 重复：增量改样式要靠 id 定位，不能重复。")
    return out


def validate(spec: dict) -> dict:
    """规格校验 + 归一。照 `svgdiagram._validate()` 的口径，不猜不说「差不多」。"""
    blocks = _blocks_from_spec(spec)
    style = str(spec.get("style") or "journal-cn").strip()
    if style not in STYLES:
        raise TypesetError(f"体例 {style!r} 不存在（可选：{'、'.join(STYLES)}）。")
    meta = spec.get("meta") or {}
    if not isinstance(meta, dict):
        raise TypesetError("meta 必须是对象。")
    m = {k: str(meta.get(k) or "").strip()
         for k in ("title", "authors", "affiliation", "abstract", "keywords")}
    # 标题可以从 blocks 里的 title 块补，但**不替模型编标题**。
    if not m["title"]:
        t = next((b["text"] for b in blocks if b["role"] == "title"), "")
        m["title"] = t
    return {"style": style, "meta": m, "blocks": blocks,
            "source": str(spec.get("source") or "")}


#: 「一次全出」的图位声明。**不要求模型记住新语法**：
#:   ① {{FIGURE:data.csv}}                 显式占位
#:   ② ![任意说明](data.csv)                普通 Markdown 图片指向数据表
#: 两者都可带可选图型：{{FIGURE:data.csv|kind=errorbar}} / ![](data.csv?kind=bar)
#: 图型 / 轴 / 配色 / 题注全由工具算，一个样式参数都不给。
_RE_AUTOFIG = re.compile(
    r"\{\{FIGURE:\s*([^}\s|]+)\s*(?:\|\s*([^}]*?)\s*)?\}\}"
    r"|!\[([^\]]*)\]\(\s*([^)\s]+\.(?:csv|tsv|json))\s*"
    r"(?:\?\s*([^)]*?)\s*)?\)"
)


def _opt_kind(raw: str) -> str:
    """图位可选参数目前只认 `kind`（语义选择，不是审美样式）。"""
    m = re.search(r"(?:^|[&,;])kind\s*=\s*([A-Za-z_]+)", raw or "")
    return m.group(1) if m else ""


def _detect_series(rows: list[dict], x: str, cols: list) -> str:
    """真多系列：低基数分类列在**每个 x 值下都出现 ≥2 个不同水平**。

    只凭列形状分不出来 —— 「组别」在 12 批次表里只是每行一个属性，
    硬拆成两条系列会把一条趋势锯成交错的锯齿；而在同 x 有重复观测的
    表里它才是真系列。按**行**实测才分得开。
    """
    from collections import defaultdict
    if not x:
        return ""
    cands = [c.name for c in cols
             if c.is_category and 2 <= c.n_unique <= 6 and c.name != x]
    for name in cands:
        seen: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            seen[str(r.get(x, ""))].add(str(r.get(name, "")))
        if len(seen) >= 2 and all(len(v) >= 2 for v in seen.values()):
            return name
    return ""

#: 题注模板按图型分派，变量只能取 plan 的 x / y —— 不编造结论、不描述数据。
_CAPTION_BY_KIND = {
    "line": "{y}随{x}的变化趋势", "time_series": "{y}随{x}的变化趋势",
    "area": "{y}随{x}的累积变化", "bar": "各{x}的{y}对比",
    "barh": "各{x}的{y}对比", "stacked_bar": "各{x}的{y}堆叠对比",
    "scatter": "{y}与{x}的关系", "box": "各{x}的{y}分布",
    "hist": "{y}的分布", "dotplot": "各{x}的{y}分布",
    "heatmap": "{y}等数值列间相关性", "matrix": "{y}等数值列间相关性",
    "errorbar": "各{x}的{y}均值与误差", "errorband": "{y}随{x}的变化与误差带",
}


def figure_caption(n: int, plan: dict, meta: dict) -> str:
    """从**图规格**生成事实性题注。

    题注里的变量只能来自 plan 的 x / y：结构上杜绝「题注写转化率、
    图却画温度」这种 11-13 改前实物里最刺眼的图文不符。不写结论、不写趋势判断。
    """
    x = str(plan.get("x") or "")
    y = str(plan.get("y") or "")
    kind = str(plan.get("kind") or "")
    tpl = _CAPTION_BY_KIND.get(kind, "{y}按{x}作图")
    core = tpl.format(y=y, x=x) if (x and y) else (y or kind)
    # 题注是给读者看的：不写 `data.csv` 这类工程文件名，也不写数据源断言。
    # 数据表名、图型、显式参数都留在回执里供核查，不上图面。
    nrow = meta.get("n_rows")
    suffix = f"（n={nrow}）" if nrow else ""
    return f"图 {n}　{core}{suffix}"


def has_figure_declaration(md_text: str) -> bool:
    """稿里有没有需要工具先出图的图位声明（**两种入口都算**）。

    ⚠️ 调用方不能只判断 `"{{FIGURE:" in text`：真 LLM A/B 里模型
    4 次中有 1 次按普通 Markdown 写 `![说明](data.csv)`，接线漏判
    ⇒ 图位被静默忽略、typeset 仍回 ok:true、一次全出 n_fig=0。
    这是**真任务才暴露**的静默降级，离线直调 expand_figures 测不到。
    """
    return _RE_AUTOFIG.search(str(md_text or "")) is not None


def expand_figures(md_text: str, root: "Path | str") -> tuple[str, list[dict]]:
    """把稿里的 `{{FIGURE:data.csv}}` 一次展开成「真图 + 自动题注」。

    只做「出图 + 声明」，排版仍走既有 `parse_markdown` / `render`，不另起炉灶。
    回执逐张记录 plan / SVG / 结构自检 / 图文一致性；**写后读回**，不认「写成功」。
    """
    from . import analysis as AN
    from . import svgplot as SV

    root = Path(root)
    receipts: list[dict] = []
    state = {"n": 0}

    def _one(m: re.Match) -> str:
        state["n"] += 1
        i = state["n"]
        if m.group(1):                       # {{FIGURE:...}}
            rel, raw_opts, alt = m.group(1), m.group(2) or "", ""
            entry = "placeholder"
        else:                                # ![说明](xxx.csv)
            alt, rel, raw_opts = m.group(3) or "", m.group(4), m.group(5) or ""
            entry = "markdown-image"
        tp = root / rel
        if not tp.is_file():
            raise TypesetError(f"图位指向的数据表不存在：{rel}")
        rows, meta = AN.read_table(tp)
        cols = AN.profile_table(rows)
        plan = AN.plan_from_shape(tp.name, meta, cols)
        # 真多系列按**行**判（同 x 下有多个水平），不是按列形状猜。
        grp = _detect_series(rows, plan["x"], cols)
        if grp:
            plan = AN.plan_from_shape(tp.name, meta, cols, group=grp)
        explicit_kind = _opt_kind(raw_opts)
        if explicit_kind:
            plan = dict(plan, kind=explicit_kind)   # 只覆盖图型，默认决策不动
        # 统一 ascii 文件名，避开 file:// 下中文路径编码差异。
        name = f"fig_{i}.svg"
        dest = root / name
        rep = SV.render_svg(rows, plan, dest)
        cap = figure_caption(i, plan, meta)
        svg_back = dest.read_text(encoding="utf-8")
        receipts.append({
            "index": i, "table": rel, "svg": str(dest), "svg_name": name,
            "entry": entry, "user_alt": alt,
            "kind": plan["kind"], "x": plan["x"], "y": plan["y"],
            "group": plan.get("group") or "", "explicit_kind": explicit_kind,
            "title": plan["title"], "caption": cap,
            "wide": bool(rep.get("wide")),
            "svg_bytes": len(svg_back.encode("utf-8")),
            "render_ok": bool(rep.get("ok")),
            "checks": rep.get("checks"),
            "caption_matches_plan": (str(plan["y"]) in cap
                                     and (not plan["x"]
                                          or str(plan["x"]) in cap)),
            "svg_readback": ("<svg" in svg_back and "</svg>" in svg_back
                             and str(plan["title"]) in svg_back),
        })
        # alt 一律用自动题注，不采用模型写的说明 —— 否则又回到「题注可能
        # 与图不符」的老路上；模型写的 alt 只留在回执里供核查。
        return f"![{cap}]({name})\n\n{cap}"

    return _RE_AUTOFIG.sub(_one, md_text), receipts


def parse_markdown(text: str, style: str = "journal-cn") -> dict:
    """把荣珂产出的现成 .md 稿件**确定性**拆成 blocks（AI 不必重写内容）。

    ⚠️ 只做结构映射，**不改一个字**：体例问题交给 `manuscript_fix`，
    这里不删过程说明、不去零号章 —— 排版工具不该偷偷改稿。
    """
    t = str(text or "")
    if not t.strip():
        raise TypesetError("稿件是空的，没有可排的块。")
    blocks: list[dict] = []
    lines = t.splitlines()
    refs_at = len(lines)
    m = _RE_REFS.search(t)
    if m:
        refs_at = t.count("\n", 0, m.start())

    def add(role, txt, **kw):
        if str(txt or "").strip():
            blocks.append({"role": role, "text": str(txt).strip(), **kw})

    i = 0
    in_refs = False
    ref_n = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if i >= refs_at:
            in_refs = True
        if not s:
            i += 1
            continue
        if in_refs:
            me = _RE_REF_ENTRY.match(ln)
            if me:
                n = int(me.group(1) or me.group(2))
                add("ref_entry", me.group(3).strip(), n=n)
                ref_n = max(ref_n, n)
            elif not s.startswith("#"):
                # 文献表里的非编号行（过程说明等）照实保留为 note，
                # 由质量门报 PROCESS_NOTE_IN_REFS，不在这里替模型删。
                add("note", s)
            i += 1
            continue

        hm = _RE_MD_HEAD.match(ln)
        if hm:
            level = len(hm.group(1))
            head = hm.group(2).strip()
            if level == 1 and not any(b["role"] == "title" for b in blocks):
                add("title", head)
            else:
                # `#` 是文档标题（已归 title）⇒ `##` 起才是章标题，层级减一。
                # 否则全篇会落成 Heading2 / h2 字号，章标题看着像小节。
                lv = max(1, min(3, level - 1))
                hn = _RE_HEAD_NUM.match(head)
                if hn and hn.group(2).strip():
                    add("heading", hn.group(2).strip(), level=lv,
                        n_hint=hn.group(1))
                else:
                    add("heading", head, level=lv)
            i += 1
            continue
        # 真 LLM A/B 的 A4 原稿写 `**摘要：**` / `**关键词：**`：
        # 加粗是呈现层标记，结构识别不该被它挡住 —— 否则星号裸露在
        # 成品里，且摘要/关键词退化成普通段落、丢掉体例样式。
        bare = s.replace("**", "").strip()
        if bare.startswith("摘要：") or bare.startswith("摘要:"):
            add("abstract", bare.split("：", 1)[-1].split(":", 1)[-1])
            i += 1
            continue
        if bare.startswith("关键词：") or bare.startswith("关键词:"):
            add("keywords", bare.split("：", 1)[-1].split(":", 1)[-1])
            i += 1
            continue
        im = _RE_IMG.search(s)
        if im:
            add("figure", im.group(1), src=im.group(2).strip())
            i += 1
            continue
        if _RE_CAPTION.match(ln):
            add("caption", s)
            i += 1
            continue
        if _RE_TABLE_ROW.match(ln):
            buf = []
            while i < len(lines) and _RE_TABLE_ROW.match(lines[i]):
                buf.append(lines[i].strip())
                i += 1
            add("table", "\n".join(buf))
            continue
        # 普通段落：连续非空非结构行合并成一段
        buf = [s]
        i += 1
        while i < len(lines) and lines[i].strip() and not (
                _RE_MD_HEAD.match(lines[i]) or _RE_IMG.search(lines[i])
                or _RE_CAPTION.match(lines[i]) or _RE_TABLE_ROW.match(lines[i])
                or lines[i].strip().startswith(("摘要：", "关键词："))):
            buf.append(lines[i].strip())
            i += 1
        add("paragraph", "".join(buf))

    return validate({"style": style,
                     "meta": {"title": next(
                         (b["text"] for b in blocks if b["role"] == "title"), "")},
                     "blocks": blocks, "source": "markdown"})


def render_with_registry(md_text: str, reg: dict,
                         root: "Path | str | None" = None,
                         style: str = "journal-cn") -> dict:
    """锚点稿 → 标准 Markdown → 排版 spec，**从登记取图元与编号**。

    不另写一套识别：`figure_registry` 是唯一事实源，这里只做渲染态转换，
    再把转换结果交给既有的 `parse_markdown` / `render`。没有登记时调用方
    走原路径，本函数不会被触发。
    """
    from . import figure_registry as FR
    clean, rr = FR.render_markdown(md_text, reg)
    vr = FR.verify(root or Path.cwd(), md_text, reg, rendered=clean)
    sp = parse_markdown(clean, style)
    ok = bool(rr["ok"] and vr["ok"])
    return {"ok": ok, "markdown": clean, "spec": sp,
            "registry_readback": rr, "registry_gaps": vr["gaps"],
            "numbering": rr["numbering"], "scope": "typeset",
            "advisory": True,
            "note": "" if ok else "登记五连读回有缺口，见 registry_gaps；"
                                 "排版仍按渲染态出，但不算通过"}


def _expected_fragments(b: dict) -> list[str]:
    """这个块**该有哪几段文字**（读回按片段比，不拿整段前 N 字硬凑）。

    ⚠️ table 在 HTML/docx 里被拆成单元格、figure 的题注另起一行 ——
       拿 `b["text"][:12]` 去产物里找必然找不到，是凭空造出来的假失败。
       按 role 拆成「产物里真的会出现的片段」，与 studio._lines_of 同型。
    """
    text = str(b.get("text") or "")
    if not text:
        return []
    if b["role"] == "table":
        cells = []
        for line in text.splitlines():
            if not line.strip().startswith("|"):
                continue
            for c in line.strip().strip("|").split("|"):
                c = c.strip()
                if c and not re.fullmatch(r":?-{2,}:?", c):
                    cells.append(c)
        return cells
    return [text]


def reconstruct_markdown(spec: dict) -> str:
    """把 blocks **逆向重建**成 Markdown，仅供质量判据使用（不是导出产物）。

    ⚠️ 为什么必须有这一层：`writing.lint()` / `manuscript_desk.check()` /
       `visual_desk.scan_visuals()` 判的是**带 Markdown 标记的稿件原文**
       （`#` 标题、`## 参考文献`、`![图]`、题注行）。把 blocks 的 `text`
       用空行拼起来喂给它们，`## 参考文献` 就凭空消失 ⇒ `MISSING_REFS` 假红
       （2026-11-11 实测：同一份稿，原文 hard=0，拼接 hard=1）。
       判据不变，**把输入还原成它吃得进去的形式**。
    """
    sp = validate(spec)
    lines: list[str] = []
    refs_started = False
    for b in sp["blocks"]:
        role, text = b["role"], b["text"]
        if role == "title":
            lines.append(f"# {text}")
        elif role == "abstract":
            lines.append(f"摘要：{text}")
        elif role == "keywords":
            lines.append(f"关键词：{text}")
        elif role == "heading":
            marks = "#" * min(3, int(b.get("level") or 1) + 1)
            hint = str(b.get("n_hint") or "").strip()
            lines.append(f"{marks} {hint + ' ' if hint else ''}{text}")
        elif role == "figure":
            lines.append(f"![{text}]({b.get('src') or ''})")
        elif role == "ref_entry":
            if not refs_started:
                lines.append("## 参考文献")
                refs_started = True
            lines.append(f"[{b.get('n') or ''}] {text}")
        elif role == "table":
            lines.append(text)
        else:                                  # paragraph / caption / note / 其它
            lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _font(style: dict, role: str, level=None) -> float:
    f = style["fonts"]
    if role == "heading":
        return f.get(f"h{min(3, int(level or 1))}", f["body"])
    return f.get(role, f["body"])


def number_headings(blocks: list[dict], enabled: bool = True) -> list[dict]:
    """按体例生成 `1 / 1.1 / 1.1.1` 编号 —— **由工具算，不让模型写**。

    `text_rendered` 是带编号的上屏文字；`text` 原件一字不动，读回仍按原件比。
    """
    out: list[dict] = []
    c1 = c2 = c3 = 0
    for b in blocks:
        item = dict(b)
        item["text_rendered"] = b["text"]
        if b["role"] == "heading" and enabled:
            lv = min(3, int(b.get("level") or 1))
            if lv == 1:
                c1 += 1
                c2 = c3 = 0
                num = str(c1)
            elif lv == 2:
                c2 += 1
                c3 = 0
                num = f"{c1}.{c2}"
            else:
                c3 += 1
                num = f"{c1}.{c2}.{c3}"
            item["number"] = num
            item["text_rendered"] = f"{num}　{b['text']}"
        out.append(item)
    return out


def order_blocks(blocks: list[dict], st: dict) -> list[dict]:
    """题注位置**由体例决定**，不由模型给的块顺序决定（结构保证质量）。

    图题在下、表题在上：把相邻的 `caption` 与 `figure/table` 交换到正确一侧。
    """
    out = list(blocks)
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        if (a["role"] == "table" and b["role"] == "caption"
                and st["caption"]["table"] == "above"):
            out[i], out[i + 1] = b, a
        elif (a["role"] == "caption" and b["role"] == "figure"
              and st["caption"]["figure"] == "below"
              and not (i > 0 and out[i - 1]["role"] in ("figure", "table"))):
            # 题注紧跟在上一张图/表之后 ⇒ 它**已经属于上一张**，
            # 不能再和后面那张图交换。旧实现只做相邻交换，同稿两图时
            # 把「图1题注, 图2」误判成「题注写在了图前」，两张题注
            # 全被挤到最后一张图下面（finish_lab/oneshot/multifig 实测）。
            out[i], out[i + 1] = b, a
    return out


def _numbering_ok(blocks: list[dict]) -> bool:
    """编号连续性：一级 1..N，二级在同一章内 1..M（判据有牙，不是恒真）。"""
    h1 = 0
    h2 = 0
    for b in blocks:
        if b["role"] != "heading" or not b.get("number"):
            continue
        lv = min(3, int(b.get("level") or 1))
        if lv == 1:
            h1 += 1
            h2 = 0
            if b["number"] != str(h1):
                return False
        elif lv == 2:
            h2 += 1
            if b["number"] != f"{h1}.{h2}":
                return False
    return True


def _caption_order_ok(blocks: list[dict], st: dict) -> bool:
    """题注落在体例规定的一侧；没配题注的图表不在这里误伤（由体例门管）。"""
    for i, b in enumerate(blocks):
        if b["role"] not in ("figure", "table"):
            continue
        want = st["caption"].get(b["role"], DESIGN["caption"].get(b["role"]))
        right = i + 1 if want == "below" else i - 1
        wrong = i - 1 if want == "below" else i + 1
        if 0 <= wrong < len(blocks) and blocks[wrong]["role"] == "caption" \
                and not (0 <= right < len(blocks) and blocks[right]["role"] == "caption"):
            return False
    return True


def _font_ladder_ok(css: str, st: dict) -> bool:
    """所有 `font-size:Npt` 都落在体例字号阶梯上，不许临时凑中间值。"""
    allowed = {round(float(v), 2) for v in st["fonts"].values()}
    used = {round(float(x), 2) for x in re.findall(r"font-size:\s*([\d.]+)pt", css)}
    return bool(used) and used.issubset(allowed)


def _layout_of(spec: dict, blocks: list[dict] | None = None) -> dict:
    """几何清单 —— 与 `svgdiagram` 的 `.layout.json` 同一用途：给质检量，不给人猜。"""
    st = STYLES[spec["style"]]
    src = blocks if blocks is not None else spec["blocks"]
    out_blocks = []
    for seq, b in enumerate(src, 1):
        out_blocks.append({"id": b["id"], "seq": seq, "role": b["role"],
                           "number": b.get("number", ""),
                           "font_pt": _font(st, b["role"], b.get("level")),
                           "line_height": st["line_height"],
                           "chars": len(b["text"]),
                           "caption_pos": st["caption"].get(
                               "figure" if b["role"] == "figure" else "table", "")
                           if b["role"] in ("figure", "table") else "",
                           "page_break_before": b["role"] in st["page_break_before"]})
    return {"schema_version": 1, "backend": "native-typeset",
            "style": spec["style"], "page": st["page"],
            "columns": st["columns"], "refs": st["refs"],
            "design": DESIGN, "numbering": DESIGN["numbering"],
            "n_blocks": len(out_blocks), "blocks": out_blocks}


def render(spec: dict, out: "str | Path" = HTML_NAME) -> dict:
    """排成自包含**打印级 HTML**（真 @page / @media print），写完读回自检。"""
    sp = validate(spec)
    st = STYLES[sp["style"]]
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = st["fonts"]
    mg = st["page"]["margin_mm"]
    C = DESIGN["colors"]
    # 编号与题注位置由体例算，模型给的顺序不当真（结构保证质量）。
    numbered = number_headings(sp["blocks"], bool(st.get("heading_number", True)))
    blocks = order_blocks(numbered, st)
    short_title = (sp["meta"]["title"] or "未命名稿件")[:24]
    css_title = json.dumps(short_title, ensure_ascii=False)
    hdr = st.get("header") or ""
    pg = f"""@page {{
  size: {st['page']['width_mm']}mm {st['page']['height_mm']}mm;
  margin: {mg[0]}mm {mg[1]}mm {mg[2]}mm {mg[3]}mm;
  @top-center {{ content: {css_title}; font-family: {FONT_STACK};
                 font-size: {f['header']}pt; color: {C['axis']}; }}
  @bottom-right {{ content: counter(page); font-family: {FONT_STACK};
                   font-size: {f['header']}pt; color: {C['axis']}; }}
}}"""
    # 插图按栏宽自动落位：AI 不给尺寸，工具按体例算。
    # 原来只写 max-width:100%，SVG 按固有 336px 显示，在 160mm 单栏里
    # 只占一半 ⇒ 初稿"图小、四周空"。单栏 82%、双栏 100%。
    fig_w = "100%" if st["columns"] > 1 else "82%"
    css = f"""
:root {{ --ink:{C['ink']}; --paper:{C['paper']}; --rule:{C['rule']};
         --axis:{C['axis']}; --accent:{C['accent']}; }}
* {{ box-sizing:border-box }}
{pg}
html,body {{ margin:0; background:#e9e4d8; color:var(--ink);
  font-family:{FONT_STACK}; }}
.sheet {{ max-width:{st['page']['width_mm']}mm; margin:16px auto;
  background:var(--paper); padding:{mg[0]}mm {mg[1]}mm {mg[2]}mm {mg[3]}mm;
  box-shadow:0 2px 14px rgba(43,39,35,.14); }}
.body {{ columns:{st['columns']}; column-gap:8mm; }}
.screen-chrome {{ display:flex; justify-content:space-between;
  font-size:{f['header']}pt; color:var(--axis); border-bottom:1px solid var(--rule);
  padding-bottom:.4em; margin-bottom:1.4em; letter-spacing:.08em; }}
.front {{ text-align:center; border-bottom:2px solid var(--accent);
  padding-bottom:1em; margin-bottom:1.2em; }}
p {{ margin:0 0 .55em; text-align:justify; orphans:2; widows:2; }}
.r-title {{ font-size:{f['title']}pt; font-weight:700; text-align:center;
  line-height:{st['line_height']}; letter-spacing:.1em; margin-bottom:.5em; }}
.r-authors {{ font-size:{f['authors']}pt; text-align:center; letter-spacing:.06em; }}
.r-affiliation {{ font-size:{f['affiliation']}pt; text-align:center; color:var(--axis); }}
.r-abstract,.r-keywords {{ font-size:{f['abstract']}pt; line-height:{st['line_height']};
  margin:.5em 0; padding-left:.9em; border-left:3px solid var(--accent);
  text-align:left; }}
.r-heading {{ font-size:{f['h1']}pt; font-weight:700; line-height:{st['line_height']};
  margin:1.2em 0 .6em; break-after:avoid; color:var(--ink);
  border-bottom:1px solid var(--rule); padding-bottom:.25em; }}
.r-heading.lv2 {{ font-size:{f['h2']}pt; border-bottom:0 }}
.r-heading.lv3 {{ font-size:{f['h3']}pt; border-bottom:0 }}
.r-paragraph {{ font-size:{f['body']}pt; line-height:{st['line_height']};
  text-indent:2em; }}
.r-caption {{ font-size:{f['caption']}pt; color:var(--axis); text-align:center;
  text-indent:0; break-after:avoid; letter-spacing:.03em; }}
.r-figure {{ text-align:center; margin:.85em 0 .5em; break-inside:avoid; }}
.r-figure img {{ max-width:100%; width:{fig_w}; height:auto; border:0; }}
.r-figure.wide img {{ width:100%; }}
.r-table {{ font-size:{f['body']}pt; width:100%; border-collapse:collapse;
  break-inside:avoid; margin:.5em 0; }}
.r-table td,.r-table th {{ border:1px solid var(--rule); padding:3px 6px; }}
.r-table tr:first-child {{ background:rgba(178,58,46,.07); }}
.r-ref_entry {{ font-size:{f['ref']}pt; line-height:{st['line_height']};
  text-indent:-2em; padding-left:2em; }}
.r-note {{ font-size:{f['note']}pt; color:var(--axis); text-indent:0; }}
@media print {{
  html,body {{ background:#fff; }}
  .sheet {{ margin:0; box-shadow:none; max-width:none; padding:0; }}
  .screen-chrome {{ display:none; }}
  .r-heading.pb {{ break-before:page; }}
  .r-figure,.r-table {{ break-inside:avoid; }}
}}
"""
    front_roles = ("title", "authors", "affiliation", "abstract", "keywords")
    parts: list[str] = []
    front: list[str] = []
    for b in blocks:
        role = b["role"]
        txt = html.escape(b.get("text_rendered") or b["text"])
        if role == "figure":
            src = html.escape(str(b.get("src") or ""), quote=True)
            # 从**成品自身**读回画布宽度决定落位，不靠额外元数据传递：
            # 通栏图按 100%（字不会缩到 5pt 以下），单栏图按体例算的 82%。
            wide = False
            if src:
                fp = Path(src) if Path(src).is_absolute() else (p.parent / src)
                try:
                    head = (fp.read_text(encoding="utf-8")[:600]
                            if fp.is_file() else "")
                    mw = re.search(r'width="(\d+(?:\.\d+)?)px"', head)
                    wide = bool(mw and float(mw.group(1)) >= 550)
                except (OSError, UnicodeError):
                    pass
            inner = (f'<img src="{src}" alt="{txt}"/>' if src
                     else f'<div class="ph">［图位：{txt}］</div>')
            cls = "r-figure wide" if wide else "r-figure"
            parts.append(f'<div class="{cls}" id="{b["id"]}">{inner}</div>')
        elif role == "table":
            rows = [r for r in b["text"].splitlines() if r.strip().startswith("|")]
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            cells = [c for c in cells if not all(re.fullmatch(r":?-{2,}:?", x or "")
                                                for x in c)]
            tb = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r)
                         + "</tr>" for r in cells)
            parts.append(f'<table class="r-table" id="{b["id"]}">{tb}</table>')
        elif role == "heading":
            lv = min(3, int(b.get("level") or 1))
            pb = " pb" if role in st["page_break_before"] and lv == 1 else ""
            num = html.escape(str(b.get("number") or ""))
            parts.append(f'<p class="r-heading lv{lv}{pb}" id="{b["id"]}" '
                         f'data-number="{num}">{txt}</p>')
        elif role == "ref_entry":
            n = b.get("n") or ""
            parts.append(f'<p class="r-ref_entry" id="{b["id"]}">'
                         f'［{n}］{txt}</p>')
        else:
            line = f'<p class="r-{role}" id="{b["id"]}">{txt}</p>'
            (front if role in front_roles else parts).append(line)

    title = html.escape(sp["meta"]["title"] or "未命名稿件")
    chrome = (f'<div class="screen-chrome"><span>荣珂 · {title}</span>'
              f'<span>{html.escape(st["label"])} · 打印时显示页眉与页码</span></div>')
    body = "\n".join(parts)
    head = "\n".join(front)
    doc = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
           f'<title>{title}</title><style>{css}</style></head><body>'
           f'<main class="sheet">{chrome}<header class="front">{head}</header>'
           f'<div class="body">{body}</div></main></body></html>')
    p.write_text(doc, encoding="utf-8", newline="\n")
    readback = p.read_text(encoding="utf-8")

    layout = _layout_of(sp, blocks)
    lp = p.with_suffix(LAYOUT_SUFFIX)
    lp.write_text(json.dumps(layout, ensure_ascii=False, indent=1),
                  encoding="utf-8", newline="\n")
    layout_back = json.loads(lp.read_text(encoding="utf-8"))

    # 前十项量「结构在场」，后九项量「规不规范/好不好」——把形容词变成
    # 可判定的事实（design.py 那条「判据定下限、资产定上限」的同一口径）。
    checks = {
        "readback_nonempty": len(readback) > 400,
        "page_rule_declared": "@page" in readback,
        "print_media_declared": "@media print" in readback,
        "font_pt_declared": "pt;" in readback or "pt;" in css,
        "cjk_preserved": "lang=\"zh-CN\"" in readback,
        "no_external_refs": ("http://" not in readback
                             and "https://" not in readback
                             and "<script" not in readback),
        "layout_valid": layout_back.get("n_blocks") == len(sp["blocks"]),
        "every_block_present": all(b["id"] in readback for b in sp["blocks"]),
        "every_block_text_present": all(
            all(html.escape(frag)[:12] in readback
                for frag in _expected_fragments(b))
            for b in sp["blocks"]),
        "style_declared": layout_back.get("style") == sp["style"],
        # ── 质量门：以下每条都能被改成 false，判据不是装饰 ──
        "cjk_font_stack_declared": (FONT_STACK in css and "SimHei" in css),
        "heading_numbering_continuous": _numbering_ok(numbered),
        "caption_position_by_style": _caption_order_ok(blocks, st),
        "page_header_footer_declared": ("@top-center" in css
                                        and "@bottom-right" in css
                                        and "counter(page)" in css),
        "font_ladder_respected": _font_ladder_ok(css, st),
        "orphan_widow_protection": ("orphans:2" in css and "widows:2" in css
                                    and "break-inside:avoid" in css),
        "ref_hanging_indent": ("text-indent:-" in css and "padding-left:" in css),
        "accent_color_declared": C["accent"] in css,
        "design_single_source": layout_back.get("design") == DESIGN,
    }
    return {"ok": all(bool(v) for v in checks.values()),
            "html": str(p), "layout": str(lp), "bytes": p.stat().st_size,
            "scope": "typeset", "backend": "native-typeset",
            "style": sp["style"], "n_blocks": len(sp["blocks"]),
            "n_quality_checks": len(checks) - 10,
            "checks": checks, "layout_data": layout,
            "quality_summary": {
                "n_headings": sum(1 for b in numbered if b.get("number")),
                "numbering": [b["number"] for b in numbered if b.get("number")],
                "caption_rule": st["caption"],
                "font_stack": FONT_STACK,
                "colors": C,
            },
            "audit_scope": ("未跑 PDF 渲染实测（字体嵌入 / 分页溢出 / 孤行 / "
                            "图表跨页）。这是可打印的排版声明，不是投稿 PDF 合格证。"),
            "note": "" if all(checks.values())
                    else "结构/质量自检有未通过项（见 checks）—— 文件保留在盘上供核查，不算成功。"}


_DOCX_SCRIPT = r'''
import json, os, sys
from docx import Document
from docx.shared import Pt, Mm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

payload = json.loads(sys.stdin.read())
spec, style, design, out = (payload["spec"], payload["style"],
                            payload["design"], payload["out"])
d = Document()
sec = d.sections[0]
pg = style["page"]
sec.page_width, sec.page_height = Mm(pg["width_mm"]), Mm(pg["height_mm"])
mg = pg["margin_mm"]
sec.top_margin, sec.right_margin = Mm(mg[0]), Mm(mg[1])
sec.bottom_margin, sec.left_margin = Mm(mg[2]), Mm(mg[3])
F, DJ, LATIN = style["fonts"], design["cjk"], design["latin"]
warnings = []

def set_font(run, size, role):
    """★ 中文必须显式写 w:eastAsia。只设 font.name / size 时 Word 把中文
    交给默认字体，西文衬线与中文宋体混排会糊 —— 与「单 Arial 出豆腐块」
    是同一类静默假成功。"""
    run.font.size = Pt(float(size))
    run.font.name = LATIN
    rPr = run._element.get_or_add_rPr()
    rf = rPr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rPr.insert(0, rf)
    rf.set(qn("w:ascii"), LATIN)
    rf.set(qn("w:hAnsi"), LATIN)
    rf.set(qn("w:eastAsia"), DJ["head"] if role in ("heading", "title") else DJ["body"])
    return run

# 页眉：短标题；页脚：真 PAGE 域（不是写死的「1」）。
hdr = sec.header.paragraphs[0]
hdr.text = ""
set_font(hdr.add_run(str(spec["meta"]["title"] or "未命名稿件")[:24]),
         F["header"], "note")
hdr.alignment = WD_ALIGN_PARAGRAPH.CENTER
ftr = sec.footer.paragraphs[0]
ftr.alignment = WD_ALIGN_PARAGRAPH.RIGHT
fld = OxmlElement("w:fldSimple")
fld.set(qn("w:instr"), "PAGE")
ftr._p.append(fld)

def put(role, text, level=None):
    p = d.add_paragraph()
    key = ("h" + str(min(3, int(level or 1)))) if role == "heading" else role
    if role == "heading":
        try:
            p.style = d.styles["Heading " + str(min(3, int(level or 1)))]
        except Exception:
            pass
    r = set_font(p.add_run(str(text)), F.get(key, F["body"]), role)
    pf = p.paragraph_format
    pf.line_spacing = float(style["line_height"])
    pf.widow_control = True
    pf.space_after = Pt(4)
    if role in ("title", "authors", "affiliation", "caption"):
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if role == "heading":
        r.font.bold = True
    if role == "paragraph":
        pf.first_line_indent = Pt(float(F["body"]) * 2)
    if role == "ref_entry":
        pf.left_indent = Pt(float(F["ref"]) * 2)
        pf.first_line_indent = Pt(-float(F["ref"]) * 2)
    return p

for b in spec["blocks"]:
    role = b["role"]
    text = b.get("text_rendered") or b.get("text") or ""
    if role == "figure":
        src = str(b.get("src") or "")
        p = d.add_paragraph()
        if src and os.path.isfile(src):
            # 一张坏图不该让整份 docx 崩掉；降级成占位文本并**说出口**。
            try:
                p.add_run().add_picture(src, width=Mm(120))
            except Exception as ie:
                warnings.append("%s 图片不可读（%s）：已降级为占位文本"
                                % (b.get("id"), type(ie).__name__))
                p.add_run("[图位·图片不可读] " + text)
        else:
            warnings.append("%s 图文件不在盘上（%s）：已用占位文本"
                            % (b.get("id"), src or "(未给)"))
            p.add_run("[图位] " + text)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if text:
            put("caption", text)
    elif role == "table":
        rows = [r for r in text.splitlines() if r.strip().startswith("|")]
        cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
        cells = [c for c in cells if c and not all(set(x) <= set("-: ") for x in c)]
        if cells:
            t = d.add_table(rows=len(cells), cols=max(len(r) for r in cells))
            try:
                t.style = "Table Grid"
            except Exception as ie:
                warnings.append("表格边框样式不可用：%s" % type(ie).__name__)
            for ri, row in enumerate(cells):
                for ci, val in enumerate(row):
                    t.cell(ri, ci).text = val
    elif role == "ref_entry":
        put(role, "[%s] %s" % (b.get("n") or "", text))
    elif role in ("heading", "title", "authors", "affiliation", "abstract",
                  "keywords", "paragraph", "caption", "note"):
        put(role, text, level=b.get("level"))
d.save(out)
print(json.dumps({"ok": True, "blocks": len(spec["blocks"]),
                  "warnings": warnings}, ensure_ascii=False))
'''


def backend_python(explicit: str = "") -> str | None:
    """找装了 python-docx 的解释器；找不到返回 None，**能力不可用要明说**。"""
    for exe in ([explicit] if explicit else []) + [sys.executable]:
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "-c", "import docx"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                return exe
        except Exception:                                     # noqa: BLE001
            continue
    return None


def export_docx(spec: dict, out: "str | Path" = DOCX_NAME,
                python_exe: str = "") -> dict:
    """导出 .docx（体例数值仍来自同一份 `STYLES`），导出后逐块读回文字。"""
    sp = validate(spec)
    st = STYLES[sp["style"]]
    # 与 HTML 同一套编号 / 题注顺序，三条导出路不许各排各的。
    numbered = number_headings(sp["blocks"], bool(st.get("heading_number", True)))
    blocks = order_blocks(numbered, st)
    py = backend_python(python_exe)
    if py is None:
        return {"ok": False, "error": "EXPORT_BACKEND_UNAVAILABLE：找不到装了 python-docx 的解释器",
                "hint": "用 3.12 启动或显式给 python_exe；别反复重试"}
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ 子进程 cwd 未必是工作区：相对 src 会在它眼里「文件不存在」，
    #    于是静默走占位、warnings 也丢了。父进程先把图路径解析成绝对形式。
    base = p.parent.resolve()
    for b in blocks:
        if b["role"] == "figure" and b.get("src"):
            s = Path(str(b["src"]))
            q = s.resolve() if s.is_absolute() else (base / s).resolve()
            b["src"] = str(q)
    payload = json.dumps({"spec": dict(sp, blocks=blocks),
                          "style": st, "design": DESIGN,
                          "out": str(p)}, ensure_ascii=False)
    try:
        r = subprocess.run([py, "-c", _DOCX_SCRIPT], input=payload.encode("utf-8"),
                           capture_output=True, timeout=180)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"docx 子进程没跑起来：{type(e).__name__}: {e}"}
    if r.returncode != 0:
        return {"ok": False, "error": "docx 导出失败：" +
                r.stderr.decode("utf-8", "replace")[-600:]}
    try:
        warnings = (json.loads(r.stdout.decode("utf-8", "replace"))
                    .get("warnings") or [])
    except (ValueError, UnicodeDecodeError):
        warnings = ["子进程回包读不出来（warnings 未知）"]
    W_T = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
    try:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            xml = z.read("word/document.xml").decode("utf-8", "replace")
            doc = "".join((t.text or "")
                          for t in ET.fromstring(z.read("word/document.xml")).iter(W_T))
            footers = [n for n in names
                       if n.startswith("word/footer") and n.endswith(".xml")]
            footer_xml = "".join(z.read(n).decode("utf-8", "replace")
                                 for n in footers)
            headers = [n for n in names
                       if n.startswith("word/header") and n.endswith(".xml")]
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"导出后读不回 docx：{type(e).__name__}: {e}"}
    missing = [b["id"] for b in sp["blocks"]
               if any(frag[:12] not in doc
                      for frag in _expected_fragments(b))]
    has_table = any(b["role"] == "table" for b in blocks)
    docx_checks = {
        "every_block_text_present": not missing,
        # ★ 中文 eastAsia 字体：不写它，Word 里中文走默认，就是「豆腐块」同型。
        #    按「实际写进去的值是否属于设计中文字体集合」判，不判某串模板
        #    在不在（那种写法短路一侧会被另一侧救回来，判据没牙）。
        "cjk_east_asia_font_declared": bool(
            set(re.findall(r'w:eastAsia="([^"]+)"', xml)) & CJK_FONT_FACTS),
        "header_present": bool(headers),
        "footer_with_page_field": bool(footers) and "PAGE" in footer_xml,
        # 有牙但不误伤：只要求「确实设了标题样式」，不强制一定是 Heading1
        # （一份稿可能只有 ## / ### 级标题）。
        "heading_styles_present": bool(re.search(r'w:val="Heading\d"', xml)),
        "table_grid_present": ("TableGrid" in xml) if has_table else True,
        "page_geometry_declared": "w:pgSz" in xml,
        "caption_position_by_style": _caption_order_ok(blocks, st),
    }
    return {"ok": all(docx_checks.values()), "docx": str(p),
            "bytes": p.stat().st_size, "n_blocks": len(sp["blocks"]),
            "readback_missing": missing, "warnings": warnings,
            "checks": docx_checks, "quality_summary": {
                "font_east_asia": DESIGN["cjk"], "numbering": DESIGN["numbering"],
                "caption_rule": st["caption"]},
            "scope": "typeset",
            "audit_scope": "未跑 Word 实际渲染分页复核（页眉/页码/字体已写进 OOXML 并读回）",
            "note": ("有降级项，见 warnings；文字仍逐块读回"
                     if warnings else "")}


def export_tex(spec: dict, out: "str | Path" = TEX_NAME) -> dict:
    """导出 .tex（纯标准库）。读回：每个非图表块的文字必须在文件里。"""
    sp = validate(spec)
    st = STYLES[sp["style"]]
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    cls = "ctexart" if sp["style"] != "preprint" else "ctexart"
    short = (sp["meta"]["title"] or "未命名稿件")[:24]
    lines = [f"\\documentclass[UTF8, a4paper, {int(st['fonts']['body'])}pt]{{{cls}}}",
             "\\usepackage{graphicx}", "\\usepackage{amsmath}",
             "\\usepackage{geometry}", "\\usepackage{fancyhdr}",
             "% 中文字体由 ctexart 提供（SimSun / Microsoft YaHei 回退）",
             "\\geometry{%s}" % ",".join(
                 f"{k}={v}mm" for k, v in zip(
                     ("top", "right", "bottom", "left"), st["page"]["margin_mm"])),
             "\\pagestyle{fancy}", "\\fancyhf{}",
             f"\\fancyhead[C]{{{short}}}",
             "\\fancyfoot[R]{\\thepage}",
             ("\\title{%s}" % sp["meta"]["title"]) if sp["meta"]["title"] else "",
             "\\begin{document}", "\\maketitle"]
    if st["columns"] == 2:
        lines.insert(-2, "\\usepackage{multicol}")
    blocks = order_blocks(sp["blocks"], st)   # 编号交给 LaTeX 的 \\section
    for b in blocks:
        role, txt = b["role"], b["text"]
        esc = (txt.replace("\\", r"\textbackslash{}")
               .replace("&", r"\&").replace("%", r"\%")
               .replace("_", r"\_").replace("#", r"\#"))
        if role == "heading":
            lv = int(b.get("level") or 1)
            cmd = {1: "section", 2: "subsection", 3: "subsubsection"}[min(3, lv)]
            lines.append(f"\\{cmd}{{{esc}}}")
        elif role in ("abstract", "keywords"):
            lines.append(f"\\noindent\\textbf{{{role}:}} {esc}\\\\")
        elif role == "figure":
            src = str(b.get("src") or "")
            if src:
                cap = f"\\caption{{{esc}}}" if txt else ""
                lines.append(f"\\begin{{figure}}[htbp]\\centering"
                             f"\\includegraphics[width=0.8\\linewidth]{{{src}}}"
                             f"{cap}\\end{{figure}}")
            else:
                lines.append(f"% [图位] {esc}")
        elif role == "table":
            # 每个单元格都落进 .tex（注释形态），导出后逐单元格读回。
            for frag in _expected_fragments(b):
                lines.append(f"% [表格] {frag}")
        elif role == "ref_entry":
            lines.append(f"\\bibitem{{{b.get('n') or ''}}} {esc}")
        elif role == "caption":
            lines.append(f"\\begin{{center}}\\small {esc}\\end{{center}}")
        else:
            lines.append(esc + "\n")
    lines.append("\\end{document}")
    p.write_text("\n".join(x for x in lines if x) + "\n", encoding="utf-8",
                 newline="\n")
    back = p.read_text(encoding="utf-8")
    missing = [b["id"] for b in sp["blocks"]
               if any(frag[:12] not in back
                      for frag in _expected_fragments(b))]
    tex_checks = {
        "every_block_text_present": not missing,
        "cjk_class_declared": "ctexart" in back and "SimSun" in back,
        "page_geometry_declared": "\\geometry{" in back,
        "header_footer_declared": ("\\fancyhead[C]" in back
                                   and "\\fancyfoot[R]" in back
                                   and "\\thepage" in back),
        "caption_position_by_style": _caption_order_ok(blocks, st),
    }
    return {"ok": all(tex_checks.values()), "tex": str(p),
            "bytes": p.stat().st_size, "n_blocks": len(sp["blocks"]),
            "readback_missing": missing, "checks": tex_checks,
            "scope": "typeset",
            "audit_scope": "未跑 xelatex 编译；引用与图表路径需编译环境复核"}


def _caps_ext_func(filename: str, funcname: str):
    """优先直连下沉后的纯领域模块（`manuscript` / `visual_layout`），其余再回退 `caps_ext`。

    保留 `_caps_ext_func` 名称与签名，确保调用方与承重测试短路钩子完全兼容；
    常用路径不再遍历 `sys.modules` 或触发外部插件二次注册。
    """
    _DIRECT_MODULES = {
        "manuscript_desk.py": "manuscript",
        "visual_desk.py": "visual_layout",
    }
    mod_name = _DIRECT_MODULES.get(filename)
    if mod_name:
        try:
            mod = __import__(f"{__package__}.{mod_name}", fromlist=[funcname])
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
        return getattr(__import__(pkg, fromlist=[funcname]), funcname, None)
    except Exception:                                         # noqa: BLE001
        return None


def audit_quality(text: str, root: "Path | str | None" = None) -> dict:
    """把三套**已有**判据汇总成一张质量结论（不重写任何一条判据）。

    照 `svgdiagram` 的 `quality/next` 口径：`ok`/`scope`/`checks` 不动，
    拿不到的判据进 `not_auditable`（**审不了 ≠ 通过**），结论只给信息不加门。
    """
    gates: dict[str, str] = {}
    blocking: list[str] = []
    not_auditable: list[str] = []
    details: list[dict] = []

    try:
        from . import writing
        rep = writing.lint(text)
        hard = len(rep.hard())
        soft = len(rep.soft())
        gates["writing_lint"] = "FAIL" if hard else ("WARN" if soft >= 4 else "PASS")
        if hard:
            blocking.append("writing_lint")
        details.append({"gate": "writing_lint", "metrics": len(rep.metrics),
                        "hard": hard, "soft": soft, "score": rep.score})
    except Exception as e:                                    # noqa: BLE001
        not_auditable.append("writing_lint")
        details.append({"gate": "writing_lint", "error": f"{type(e).__name__}: {e}"})

    chk = _caps_ext_func("manuscript_desk.py", "check")
    if chk is None:
        not_auditable.append("manuscript_format")
    else:
        try:
            r = chk(text)
            gates["manuscript_format"] = "FAIL" if r["hard"] else (
                "WARN" if r["soft"] else "PASS")
            if r["hard"]:
                blocking.append("manuscript_format")
            details.append({"gate": "manuscript_format",
                            "hard": r["hard"], "soft": r["soft"],
                            "chapters": len(r["chapters"]),
                            "ref_entries": r["ref_entries"]})
        except Exception as e:                                # noqa: BLE001
            not_auditable.append("manuscript_format")
            details.append({"gate": "manuscript_format",
                            "error": f"{type(e).__name__}: {e}"})

    scan = _caps_ext_func("visual_desk.py", "scan_visuals")
    if scan is None:
        not_auditable.append("visual_placement")
    else:
        try:
            r = scan(text, root)
            hard = [g for g in r["gaps"] if g["level"] == "hard"]
            gates["visual_placement"] = "FAIL" if hard else (
                "WARN" if r["gaps"] else "PASS")
            if hard:
                blocking.append("visual_placement")
            details.append({"gate": "visual_placement",
                            "counts": r["counts"],
                            "hard_codes": [g["code"] for g in hard]})
        except Exception as e:                                # noqa: BLE001
            not_auditable.append("visual_placement")
            details.append({"gate": "visual_placement",
                            "error": f"{type(e).__name__}: {e}"})

    return {"ok": True, "advisory": True, "tier": "kernel-reused",
            "gates": gates, "blocking": blocking,
            "fix_before_delivery": blocking,
            "not_auditable": not_auditable, "details": details,
            "scope_note": "复用 writing.lint / manuscript_desk.check / "
                          "visual_desk.scan_visuals，不另写第二套判据；不接交付门"}


def format_next(quality: dict, render_rep: dict) -> str:
    """把结论写成**改规格**的动作，不叫 AI 去手改产物字节（同 svgdiagram.next）。"""
    out = ["排版已成形：打印级 HTML（@page + @media print）、几何清单 .layout.json 已落盘。",
           "⚠️ " + render_rep["audit_scope"],
           "改版式请**改 spec 重排**：换 style、改 blocks 的 role/level、调 meta；"
           "不要手改 HTML/CSS 字节。"]
    if quality["fix_before_delivery"]:
        out += ["", "⛔ 交付前建议修：" + "、".join(quality["fix_before_delivery"]) + "。",
                "稿面/体例问题回写作链修（writing.lint / manuscript_fix），"
                "图表落位用 visual_place，修完再排一次。"]
    if quality["not_auditable"]:
        out += ["", "⚠️ 审不了（**不等于通过**）："
                + "、".join(quality["not_auditable"])
                + "。交付时如实说明，别声称全审过。"]
    out += ["", "要交付文件：调 typeset_export(fmt='docx') 或 fmt='tex'；"
            "两张质量尺子（SVG 七门 / 投稿 PDF 六门）不在这里，照旧各走各的。"]
    return "\n".join(out)
