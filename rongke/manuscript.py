# -*- coding: utf-8 -*-
"""中文期刊综述稿件体例检查、确定性规范化与稿件台视图渲染（纯领域模块）。

本模块承载原 `caps_ext/manuscript_desk.py` 中的纯计算与视图渲染逻辑，
供 `caps_ext/manuscript_desk.py`、`typeset.py`、`readiness.py` 等直接导入，
不再经由 `sys.modules` 扫描外部常驻插件。

三条不许破：
  1. 格式判据只有一份：章节识别复刻 `web/static/reader.js` 的 `deskTocOf`，
     稿面指标取 `writing.lint()` 的实测值。
  2. 规范化是确定性的：只改无争议的格式（零号章、内部标注、过程说明），
     不重排编号、不删正文、不改引注。
  3. 只给信息，不加门：体例检查是过程反馈，不进交付硬门。
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from rongke.state_store import atomic_write_text

VOICE = """【中文期刊综述 · 稿件体例（常驻插件 · 稿件台）】
交稿前按这份体例自查，改不动的如实说明，不要为了过检查改坏内容：
1. 标题层级：稿名用一级标题；各章用二级标题；小节用三级。不跳级。
2. **引言不编号**：写「## 引言」，不要写「## 0 引言」；正文各章从 1 起连续编号
   （1、2、3…），章号与大纲一致。零号章、跳号、重复号都算体例错误。
3. 稿名之下依次给「摘要：」「关键词：」；关键词用中文分号分隔，5–8 个。
4. 表题在表上方、图题在图下方，统一写「表 1　题名」「图 1　题名」；
   正文里必须各有一次呼应。图表编号连续，不重号。
5. 引注用 [n]，首现顺序递增、编号连续；参考文献表条目与正文编号一一对应，
   没有表外条目、没有漏列条目。
6. **参考文献表是给审稿人看的**：不写「以下 N 条取自本地文献库」这类过程说明，
   不在条目末尾留〔库·kb…〕等内部标注，不写检索日志；DOI 只在核验一致时填。
7. 检索范围、DOI 核验说明等过程材料另放附录文件或文末「附录」，不与正文各章同级。
8. 写完调 `manuscript_check` 看体例报告；要落盘用 `manuscript_fix` 做确定性规范化，
   用 `manuscript_desk` 生成稿件台视图。这三者只给信息，不是交付门。
"""

#: 与 reader.js:deskTocOf 同一套标题识别（本项目稿件台的唯一读法）。
_RE_MD_HEAD = re.compile(r"^(#{1,5})\s+(.+?)\s*#*$")
_RE_BRACKET_HEAD = re.compile(r"^【(.{1,28})】\s*$")
_RE_CN_CHAPTER = re.compile(r"^第[一二三四五六七八九十百]+[章节部编]")
_RE_CN_NUM = re.compile(r"^[一二三四五六七八九十]{1,3}\s*[、.．]\s*\S")
_RE_MULTI_NUM = re.compile(r"^(\d+(?:\.\d+){1,4})[\s、.．]")
_RE_PLAIN_NUM = re.compile(r"^\d{1,3}\s*[、.．]\s*\S")

_RE_CHAPTER = re.compile(r"^\s{0,3}(#{2,4})\s+(\d+)\s*[、.．]?\s*(\S.*)$")
_RE_CITE = re.compile(r"[〔\[]\s*(\d+)\s*[〕\]]")
_RE_REF_ENTRY = re.compile(r"^\s*[\[〔]\s*(\d+)\s*[\]〕]\s+\S", re.M)
_RE_INTERNAL_DOCID = re.compile(r"\s*〔库·[A-Za-z0-9_.-]+〕\s*$")
_RE_PROCESS_NOTE = re.compile(
    r"^\s*说明[:：].{0,40}(?:文献库|litkb|核验|检索).*$\n?", re.M | re.I)
_RE_ZERO_HEAD = re.compile(r"^(\s{0,3}#{2,4}\s+)0\s*(引言|绪论|前言|导言)\s*$", re.M)


def toc_of(line: str):
    """复刻 reader.js 的 deskTocOf：返回 (层级, 标题) 或 None。"""
    s = str(line or "").strip()
    m = _RE_MD_HEAD.match(s)
    if m:
        return min(len(m.group(1)), 4), m.group(2).strip()
    m = _RE_BRACKET_HEAD.match(s)
    if m:
        return 1, m.group(1)
    if len(s) <= 40 and _RE_CN_CHAPTER.match(s):
        return 1, s
    if len(s) <= 40 and _RE_CN_NUM.match(s):
        return 1, s
    if len(s) <= 50:
        m = _RE_MULTI_NUM.match(s)
        if m:
            return min(len(m.group(1).split(".")), 4), s
    if len(s) <= 50 and _RE_PLAIN_NUM.match(s):
        return 1, s
    return None


def _lint_metrics(text: str) -> dict:
    """稿面指标只取 writing.lint() —— 与交付门同一份数据源。"""
    try:
        from rongke.writing import lint
        rep = lint(text)
        m = rep.to_dict().get("metrics") or {}
        return {k: m.get(k) for k in
                ("chars", "chars_eq", "paragraphs", "sentences", "citations",
                 "cite_numbers", "refs_n", "has_refs", "cliche_hits")
                if k in m}
    except Exception:  # noqa: BLE001 - 拿不到就如实留空，不假装算过
        return {}


def check(text: str) -> dict:
    """读回一份体例报告。**只判格式，不判科学结论、不改稿**。"""
    t = str(text or "")
    lines = t.splitlines()
    issues: list[dict] = []

    def add(code, level, line, detail):
        issues.append({"code": code, "level": level, "line": line,
                       "detail": detail})

    chapters: list[dict] = []
    for i, line in enumerate(lines, 1):
        h = toc_of(line)
        if h:
            chapters.append({"line": i, "level": h[0], "title": h[1]})
        m = _RE_ZERO_HEAD.match(line)
        if m:
            add("ZERO_CHAPTER", "hard", i,
                f"引言不该编零号：{line.strip()!r} → 改成「{m.group(1).strip()} {m.group(2)}」")

    if not re.search(r"^\s*(?:\*\*)?摘要(?:\*\*)?\s*[:：]", t, re.M):
        add("MISSING_ABSTRACT", "soft", 0, "缺「摘要：」")
    if not re.search(r"^\s*(?:\*\*)?关键词(?:\*\*)?\s*[:：]", t, re.M):
        add("MISSING_KEYWORDS", "soft", 0, "缺「关键词：」")

    heads = [c for c in chapters if c["level"] >= 2]
    nums = [int(m.group(2)) for c in heads
            for m in [_RE_CHAPTER.match(lines[c["line"] - 1])] if m]
    if nums:
        expect = list(range(1, len(nums) + 1))
        if nums != expect:
            add("CHAPTER_NUMBER", "hard", 0,
                f"正文章号应从 1 起连续，实测 {nums} → 期望 {expect}")
    for i in range(1, len(chapters)):
        if chapters[i]["level"] - chapters[i - 1]["level"] > 1:
            add("LEVEL_JUMP", "soft", chapters[i]["line"],
                f"标题层级从 {chapters[i-1]['level']} 跳到 {chapters[i]['level']}")

    ref_at = next((i for i, l in enumerate(lines)
                   if re.match(r"^\s*#{1,4}\s*参考文献\s*$", l)), None)
    if ref_at is None:
        add("MISSING_REFS", "hard", 0, "缺「## 参考文献」表")
        ref_text = ""
    else:
        ref_text = "\n".join(lines[ref_at + 1:])
        if _RE_PROCESS_NOTE.search(ref_text):
            add("PROCESS_NOTE_IN_REFS", "hard", ref_at + 1,
                "参考文献表里混入了「说明：…文献库/核验」过程说明，不该给审稿人看")
        if "〔库·" in ref_text:
            add("INTERNAL_DOCID", "hard", ref_at + 1,
                "文献条目末尾带〔库·kb…〕内部标注，投稿前应去掉")
        entries = [int(x) for x in _RE_REF_ENTRY.findall(ref_text)]
        if entries and entries != list(range(1, len(entries) + 1)):
            add("REF_NUMBER", "hard", ref_at + 1,
                f"参考文献表编号不连续：{entries[:12]}…")
        body_text = "\n".join(lines[:ref_at])
        used = [int(x) for x in _RE_CITE.findall(body_text)]
        uniq = sorted(set(used))
        if entries:
            out_of_range = [n for n in uniq if n > len(entries)]
            if out_of_range:
                add("CITE_OUT_OF_RANGE", "hard", 0,
                    f"正文引注编号 {out_of_range[:8]} 超出文献表 {len(entries)} 条")
            if uniq and uniq != list(range(1, max(uniq) + 1)):
                add("CITE_GAP", "soft", 0, f"正文引注编号有断档：{uniq[:12]}…")
            unused = [n for n in range(1, len(entries) + 1) if n not in uniq]
            if unused:
                add("UNUSED_REF", "soft", 0, f"文献表里正文未引用：{unused[:8]}…")

    cap_tables = re.findall(r"^\s*(?:\*\*)?(表|图)\s*(\d+)\s*[　\s]", t, re.M)
    for kind, group in (("表", [int(n) for k, n in cap_tables if k == "表"]),
                        ("图", [int(n) for k, n in cap_tables if k == "图"])):
        if group and group != list(range(1, len(group) + 1)):
            add("CAPTION_NUMBER", "soft", 0, f"{kind}题注编号不连续：{group}")
    if re.search(r"^\s*\*\*(?:表|图)\s*\d+", t, re.M):
        add("CAPTION_BOLD", "soft", 0, "图表题注用粗体包裹，建议统一为「表 1　题名」")

    return {
        "issues": issues,
        "chapters": chapters,
        "metrics": _lint_metrics(t),
        "citation_numbers": sorted(set(int(x) for x in _RE_CITE.findall(t))),
        "ref_entries": len(_RE_REF_ENTRY.findall(ref_text)),
        "captions": {"table": sum(1 for k, _ in cap_tables if k == "表"),
                     "figure": sum(1 for k, _ in cap_tables if k == "图")},
        "hard": sum(1 for x in issues if x["level"] == "hard"),
        "soft": sum(1 for x in issues if x["level"] == "soft"),
    }


def normalize(text: str) -> tuple[str, list[str]]:
    """确定性规范化。**只改无争议的格式**，不动编号、正文与引注。"""
    t = str(text or "")
    changes: list[str] = []

    def sub(pattern, repl, label, flags=0):
        nonlocal t
        # 编译过的 pattern 不能再接 flags（re.subn 会当场抛 ValueError）；
        # 编译时的 re.M 已生效，直接走 pattern.subn。
        if hasattr(pattern, "subn"):
            new, n = pattern.subn(repl, t)
        else:
            new, n = re.subn(pattern, repl, t, flags=flags)
        if n:
            t = new
            changes.append(f"{label} ×{n}")

    # ① 用户点名的「引言前面不用写零」
    sub(_RE_ZERO_HEAD, lambda m: f"{m.group(1)}{m.group(2)}", "引言去零号", re.M)
    # ② 摘要/关键词去掉粗体包裹与多余空格
    sub(r"^\s*\*\*(摘要|关键词)\*\*\s*[:：]\s*", lambda m: f"{m.group(1)}：",
        "前置字段统一", re.M)
    # ③ 参考文献表内混入的过程说明（只在文献表之后生效）
    idx = next((m.start() for m in re.finditer(r"^\s*#{1,4}\s*参考文献\s*$",
                                               t, re.M)), None)
    if idx is not None:
        head, tail = t[:idx], t[idx:]
        new_tail, n = _RE_PROCESS_NOTE.subn("", tail)
        if n:
            t = head + new_tail
            changes.append(f"移除文献表过程说明 ×{n}")
    # ④ 文献条目末尾的内部库标注
    lines = t.splitlines(keepends=True)
    out, n = [], 0
    for line in lines:
        new, k = _RE_INTERNAL_DOCID.subn("", line.rstrip("\r\n"))
        if k:
            n += k
            out.append(new + ("\n" if line.endswith("\n") else ""))
        else:
            out.append(line)
    if n:
        t = "".join(out)
        changes.append(f"移除文献条目内部标注 ×{n}")
    # ⑤ 行尾空白
    sub(r"[ \t]+$", "", "清理行尾空白", re.M)
    return t, changes


def desk_data(text: str, title: str = "") -> dict:
    """稿件台视图的**唯一数据源**：体例报告 + 章节树 + 正文分块。"""
    report = check(text)
    if not title:
        for line in str(text or "").splitlines():
            h = _RE_MD_HEAD.match(line.strip())
            if h and len(h.group(1)) == 1:
                title = h.group(2).strip()
                break
    blocks, cur = [], None
    for line in str(text or "").splitlines():
        h = toc_of(line)
        if h and h[0] <= 2:
            cur = {"level": h[0], "title": h[1], "lines": []}
            blocks.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
    for b in blocks:
        b["text"] = "\n".join(b["lines"]).strip()
        b["chars"] = len(b["text"])
        del b["lines"]
    return {"title": title or "未命名稿件", "report": report,
            "blocks": blocks, "source": "writing.lint + manuscript_desk.check"}


_CSS = """
:root{--paper:#fdfaf3;--ink:#3c484a;--mute:#8b9490;--qing:#5b8890;
--qing-l:#dceae8;--zhu:#c0705a;--line:#ece5d8;--amber:#b08a4a;}
*{box-sizing:border-box}
body{margin:0;background:#f4f0e7;color:var(--ink);line-height:1.7;
font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif;}
#bar{position:fixed;top:0;left:0;height:3px;background:var(--zhu);width:0;z-index:9}
.wrap{max-width:1180px;margin:0 auto;padding:26px 20px 60px;}
header.top{border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:18px;
display:flex;justify-content:space-between;align-items:flex-end;gap:14px;flex-wrap:wrap}
h1{font-size:23px;font-weight:500;letter-spacing:2px;margin:0}
.sub{color:var(--mute);font-size:12px;margin-top:4px}
.stats{display:flex;gap:18px;font-size:11px;color:var(--mute)}
.stats b{display:block;font-size:19px;color:var(--qing);font-weight:500;text-align:center}
.grid{display:grid;grid-template-columns:250px minmax(0,1fr);gap:18px}
.card{background:var(--paper);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;margin-bottom:16px}
.card h2{font-size:13px;letter-spacing:1px;font-weight:500;margin:0 0 10px;
color:var(--qing);border-bottom:1px solid var(--line);padding-bottom:7px}
.toc a{display:block;color:var(--ink);text-decoration:none;font-size:12px;
padding:4px 6px;border-left:2px solid transparent;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.toc a:hover{border-left-color:var(--zhu);background:#faf5ec}
.toc a.lv3{padding-left:20px;color:var(--mute)}
.issue{border-left:3px solid var(--zhu);background:#fbeee9;border-radius:0 8px 8px 0;
padding:7px 11px;margin-bottom:7px;font-size:12px}
.issue.soft{border-left-color:var(--amber);background:#faf1e2}
.issue code{color:var(--zhu);font-size:11px}
.ok{color:var(--qing);font-size:12px;padding:6px 0}
.kv{display:flex;justify-content:space-between;font-size:12px;padding:4px 0;
border-bottom:1px dashed var(--line)}
.kv b{font-weight:500;color:var(--qing)}
.chap{margin-bottom:22px;scroll-margin-top:16px}
.chap h3{font-size:15px;font-weight:500;letter-spacing:1px;margin:0 0 8px;
color:var(--ink);border-bottom:1px solid var(--line);padding-bottom:5px}
.chap .body{font-size:13px;white-space:pre-wrap;word-break:break-word}
.chap .len{font-size:11px;color:var(--mute);float:right}
"""


def render_html(data: dict) -> str:
    """自包含稿件台视图（绢青陈墨 + 朱砂）。零外部依赖，可直接内联打开。"""
    e = html.escape
    rep = data.get("report") or {}
    blocks = data.get("blocks") or []
    m = rep.get("metrics") or {}
    toc = "".join(
        f'<a class="lv{min(b.get("level", 2), 3)}" href="#c{i}">{e(b.get("title", ""))}</a>'
        for i, b in enumerate(blocks))
    issues = "".join(
        f'<div class="issue {e(x.get("level", "soft"))}"><code>{e(x.get("code", ""))}</code>'
        f'{" · 第 " + str(x["line"]) + " 行" if x.get("line") else ""}<br>{e(x.get("detail", ""))}</div>'
        for x in (rep.get("issues") or [])) or '<div class="ok">体例检查通过，没有发现格式问题。</div>'
    kvs = [("字数（chars_eq）", m.get("chars_eq")), ("段落", m.get("paragraphs")),
           ("句子", m.get("sentences")), ("引注数", m.get("cite_numbers")),
           ("文献表条数", m.get("refs_n")), ("章节数", len(blocks)),
           ("表 / 图", f'{(rep.get("captions") or {}).get("table", 0)} / '
                       f'{(rep.get("captions") or {}).get("figure", 0)}'),
           ("体例硬问题 / 提示", f'{rep.get("hard", 0)} / {rep.get("soft", 0)}')]
    kv = "".join(f'<div class="kv"><span>{e(str(k))}</span><b>{e(str(v if v is not None else "—"))}</b></div>'
                 for k, v in kvs)
    chaps = "".join(
        f'<section class="chap" id="c{i}"><h3>{e(b.get("title", ""))}'
        f'<span class="len">{b.get("chars", 0)} 字</span></h3>'
        f'<div class="body">{e(b.get("text", ""))}</div></section>'
        for i, b in enumerate(blocks))
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(data.get("title", "稿件台"))} · 稿件台</title><style>{_CSS}</style></head>
<body><div id="bar"></div><div class="wrap">
<header class="top"><div><h1>{e(data.get("title", "稿件台"))}</h1>
<div class="sub">荣珂 · 稿件台视图 · 数据源 writing.lint + manuscript_desk.check</div></div>
<div class="stats"><div><b>{len(blocks)}</b>章节</div><div><b>{m.get("cite_numbers", "—")}</b>引注</div>
<div><b>{m.get("refs_n", "—")}</b>文献</div><div><b>{rep.get("hard", 0)}</b>体例硬问题</div></div>
</header><div class="grid"><aside>
<div class="card toc"><h2>章节</h2>{toc or '<div class="ok">未识别到章节标题</div>'}</div>
<div class="card"><h2>稿面</h2>{kv}</div></aside><main>
<div class="card"><h2>体例检查</h2>{issues}</div>{chaps}</main></div></div>
<script>addEventListener('scroll',function(){{var h=document.documentElement;
document.getElementById('bar').style.width=
(100*h.scrollTop/(h.scrollHeight-h.clientHeight||1))+'%';}});</script></body></html>"""


def _write_back(path: Path, text: str) -> bool:
    """写后读回：原子写盘，写成功不算数，读回一致才算数。"""
    try:
        return bool(atomic_write_text(path, text)["ok"])
    except OSError:
        return False
