# -*- coding: utf-8 -*-
"""《岸记》—— 荣珂实验手记 / 规划看板的**单源台账**与渲染器。

文体不是给报告加日期，是把工程纪律写成一种日记体：
    起（为什么动手） → 验（问 / 读回 / 判词） → 朱批（只记翻案与坑）
    → 收（结论＋不宣称） → 明日（自动长成规划） → 锚（原始证据路径）

四条硬纪律（与 rongke 内核一致，别改软）：
  1. `entries.jsonl` 是**唯一事实源**；`YYYY-MM-DD.md` / `规划.md` / `index.html`
     全是它的视图，随时可重渲染，**不手改、不第二处存真相**。
  2. append-only：同日 `open` 只允许一次，更正一律走新事件，不回改旧行。
  3. 证据只认**读回**：每次写盘后立刻读回比对，不一致就抛错，不许「写成功」冒充。
  4. 文体是压缩，不是省略：判词、`不宣称`、锚三件不许被省掉；
     「读不到」≠「不符合」，朱批不放大路消息。

纯标准库，零外部依赖；被外部能力 `@external/shore-journal` 包装成工具。
"""
from __future__ import annotations

import html
import json
import re
from datetime import date as _date
from pathlib import Path
from time import strftime

#: 水位四态。不是装饰，是当日实验状态的一眼判据。
WATERS: dict[str, str] = {
    "潮": "推进 · 全绿",
    "平": "稳态 · 整理",
    "雾": "读不到 · 待核",
    "礁": "打红 · 翻案",
}

#: 两种皮，同一份台账。手记=松弛的日常本子；宋版=原来那套碑刻/古籍感。
#: 2026-11-07 用户反馈「别弄得这么严肃」⇒ 默认改手记，宋版留作可切换。
THEMES: tuple[str, ...] = ("手记", "宋版")

#: 六段固定顺序。渲染与校验共用这一份，别在别处再写一遍。
KINDS: tuple[str, ...] = ("记", "验", "朱批", "收", "明日", "锚")

RULE = "岸律　证据只认读回 · 读不到 ≠ 不符合 · 判词是压缩，不是省略"
TITLE = "岸记"
LEDGER = "entries.jsonl"
PLAN = "规划.md"
BOOK = "index.html"


class JournalError(ValueError):
    """体例被破坏时抛出。宁可报错，也不许静默写出一篇不合规的日记。"""


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=True)


def _cell(s: str) -> str:
    """Markdown 表格单元格：竖线会破表，换行会破行。"""
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()


class JournalBook:
    """一本《岸记》。所有写操作都先追加事件，再整册重渲染。"""

    def __init__(self, root: str | Path, theme: str = "手记"):
        if theme not in THEMES:
            raise JournalError(f"主题只认 {list(THEMES)}，得到 {theme!r}")
        self.theme = theme
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = self.root / LEDGER
        self.plan_path = self.root / PLAN
        self.book_path = self.root / BOOK

    # ── 台账：唯一事实源 ──────────────────────────────────────────
    def rows(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        out: list[dict] = []
        for i, line in enumerate(self.ledger.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception as e:
                raise JournalError(f"台账第 {i} 行损坏：{e}") from e
        return out

    def _append(self, ev: dict) -> dict:
        with self.ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
        rows = self.rows()
        if not rows or rows[-1] != ev:
            raise JournalError("写盘后读回不一致：拒绝把「写成功」当成成功")
        return ev

    # ── 校验 ──────────────────────────────────────────────────────
    @staticmethod
    def _date(s: str) -> str:
        s = str(s or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            raise JournalError(f"日期要 YYYY-MM-DD，得到 {s!r}")
        try:
            _date.fromisoformat(s)
        except ValueError as e:
            raise JournalError(f"不是真实日期：{s!r}") from e
        return s

    def _opened(self, rows: list[dict] | None = None) -> dict[str, dict]:
        return {r["date"]: r for r in (rows if rows is not None else self.rows())
                if r.get("type") == "open"}

    def _last_day(self, rows: list[dict]) -> str:
        opened = self._opened(rows)
        if not opened:
            raise JournalError("还没开页：先 journal_open，别在空册上记一笔")
        return max(opened)

    # ── 写：三段式，全部 append-only ──────────────────────────────
    def open_day(self, day: str, water: str, question: str) -> dict:
        """开一页。同日已有页 ⇒ 拒绝覆盖（日记不许被重写）。"""
        day = self._date(day)
        water = str(water or "").strip()
        if water not in WATERS:
            raise JournalError(f"水位只认 {list(WATERS)}，得到 {water!r}")
        question = str(question or "").strip()
        if not question:
            raise JournalError("今日问不能空：没有问题的日记只是流水账")
        rows = self.rows()
        if day in self._opened(rows):
            raise JournalError(f"{day} 已开页，不许覆盖；用 journal_note 续写")
        ev = self._append({"type": "open", "date": day,
                           "water": water, "question": question})
        self.render_all()
        return ev

    def note(self, kind: str, text: str, evidence: str = "", verdict: str = "",
             boundary: str = "", day: str = "") -> dict:
        """续一条。kind=明日 会同时长进规划看板（与日记同源，不另立清单）。"""
        kind = str(kind or "").strip()
        if kind not in KINDS:
            raise JournalError(f"段落只认 {list(KINDS)}，得到 {kind!r}")
        text = str(text or "").strip()
        if not text:
            raise JournalError("正文不能空")
        rows = self.rows()
        day = self._date(day) if day else self._last_day(rows)
        if day not in self._opened(rows):
            raise JournalError(f"{day} 没开页，不许悬空记一笔")
        ev = {"type": "note", "date": day, "kind": kind, "text": text}
        for k, v in (("evidence", evidence), ("verdict", verdict),
                     ("boundary", boundary)):
            v = str(v or "").strip()
            if v:
                ev[k] = v
        self._append(ev)
        self.render_all()
        return ev

    def done(self, item: str, day: str = "") -> dict:
        """规划条目结案：追加一条 done 事件，**不删**原「明日」，日记原文保持可读。"""
        item = str(item or "").strip()
        if not item:
            raise JournalError("结案要给出事项原文片段")
        rows = self.rows()
        hits = [r for r in rows if r.get("type") == "note"
                and r.get("kind") == "明日" and item in r.get("text", "")]
        if not hits:
            raise JournalError(f"规划里找不到「{item}」：读不到 ≠ 已办")
        if len(hits) > 1 and not day:
            raise JournalError(f"「{item}」命中 {len(hits)} 条，给 day 说清是哪一渡")
        if day:
            day = self._date(day)
            hits = [r for r in hits if r.get("date") == day]
            if not hits:
                raise JournalError(f"{day} 没有这条「明日」")
        ev = self._append({"type": "done", "item": item,
                           "dates": sorted({r["date"] for r in hits})})
        self.render_all()
        return ev

    # ── 读回 ──────────────────────────────────────────────────────
    def days(self, rows: list[dict] | None = None) -> list[str]:
        return sorted(self._opened(rows))

    def crossing(self, day: str, rows: list[dict] | None = None) -> int:
        ds = self.days(rows)
        return ds.index(day) + 1 if day in ds else 0

    def notes_of(self, day: str, rows: list[dict] | None = None) -> list[dict]:
        return [r for r in (rows if rows is not None else self.rows())
                if r.get("type") == "note" and r.get("date") == day]

    def read_day(self, day: str) -> dict:
        """读回：结构化事件 + 渲染文本 + 落盘全文，三者必须一致。"""
        day = self._date(day)
        rows = self.rows()
        opens = self._opened(rows)
        if day not in opens:
            raise JournalError(f"{day} 没有这一渡")
        md = self.render_day(day, rows)
        path = self.root / f"{day}.md"
        disk = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"date": day, "water": opens[day]["water"],
                "question": opens[day]["question"],
                "crossing": self.crossing(day, rows),
                "notes": self.notes_of(day, rows),
                "markdown": md, "disk": disk, "same": md == disk,
                "path": str(path)}

    # ── 渲染：Markdown 日记 ───────────────────────────────────────
    def render_day(self, day: str, rows: list[dict] | None = None) -> str:
        rows = rows if rows is not None else self.rows()
        opens = self._opened(rows)
        if day not in opens:
            raise JournalError(f"{day} 没有这一渡")
        head = opens[day]
        notes = self.notes_of(day, rows)
        by = {k: [n for n in notes if n.get("kind") == k] for k in KINDS}
        L: list[str] = [
            f"# {TITLE} · 第 {self.crossing(day, rows)} 渡",
            "",
            f"> {day} · 水位 **{head['water']}**（{WATERS[head['water']]}）",
            f"> 今日问：{head['question']}",
            "",
        ]
        if by["记"]:
            L += ["## 起", ""]
            for x in by["记"]:
                L += [x["text"], ""]
        if by["验"]:
            L += ["## 验", ""]
            for i, n in enumerate(by["验"], 1):
                # 行尾双空格 = Markdown 硬换行。否则「问 / 读回 / 判」会被
                # 渲染器揉成一段，日记就没法一眼扫了（文体是给人看的）。
                seg = [f"{i}. **问**　{n['text']}"]
                if n.get("evidence"):
                    seg.append(f"**读回**　{n['evidence']}")
                if n.get("verdict"):
                    seg.append(f"**判**　〔{n['verdict']}〕")
                if n.get("boundary"):
                    seg.append(f"**不宣称**　{n['boundary']}")
                for j, s in enumerate(seg):
                    L.append(("" if j == 0 else "   ") + s
                             + ("  " if j < len(seg) - 1 else ""))
            L.append("")
        if by["朱批"]:
            L += ["## 朱批", ""] + [f"- {n['text']}" for n in by["朱批"]] + [""]
        if by["收"]:
            L += ["## 收", ""]
            for n in by["收"]:
                L += [n["text"], ""]
                if n.get("boundary"):
                    L += [f"> **不宣称**　{n['boundary']}", ""]
        if by["明日"]:
            done_items = [r.get("item", "") for r in rows if r.get("type") == "done"]
            L += ["## 明日", ""]
            for n in by["明日"]:
                mark = "x" if any(d in n["text"] for d in done_items) else " "
                tail = f"　〔验收：{n['evidence']}〕" if n.get("evidence") else ""
                L.append(f"- [{mark}] {n['text']}{tail}")
            L.append("")
        if by["锚"]:
            L += ["## 锚", ""] + [f"- `{n['text']}`" for n in by["锚"]] + [""]
        L += ["---", "", RULE, ""]
        return "\n".join(L)

    # ── 渲染：规划看板（与日记同源）───────────────────────────────
    def plan_rows(self, rows: list[dict] | None = None) -> list[dict]:
        rows = rows if rows is not None else self.rows()
        done_items = [r.get("item", "") for r in rows if r.get("type") == "done"]
        out = []
        for r in rows:
            if r.get("type") != "note" or r.get("kind") != "明日":
                continue
            out.append({"date": r["date"], "item": r.get("text", ""),
                        "accept": r.get("evidence", ""),
                        "status": "已渡" if any(d in r.get("text", "")
                                               for d in done_items) else "待渡"})
        return out

    def render_plan(self, rows: list[dict] | None = None) -> str:
        pr = self.plan_rows(rows)
        L = ["# 岸记 · 规划看板", "",
             f"> 单源：`journal/{LEDGER}` 的「明日」事件。此页由渲染器生成，勿手改。",
             f"> 待渡 {sum(1 for r in pr if r['status'] == '待渡')} 条 · "
             f"已渡 {sum(1 for r in pr if r['status'] == '已渡')} 条", ""]
        for status in ("待渡", "已渡"):
            L += [f"## {status}", "",
                  "| 出处 | 事项 | 验收判据 |", "|---|---|---|"]
            for r in reversed([x for x in pr if x["status"] == status]):
                L.append(f"| {_cell(r['date'])} | {_cell(r['item'])} | "
                         f"{_cell(r['accept']) or '（未写验收判据）'} |")
            L.append("")
        L += ["---", "", RULE, ""]
        return "\n".join(L)

    # ── 渲染：HTML 日记本（绢青陈墨 / 朱砂）───────────────────────
    def render_book(self, rows: list[dict] | None = None) -> str:
        rows = rows if rows is not None else self.rows()
        days = list(reversed(self.days(rows)))
        notes = [r for r in rows if r.get("type") == "note"]
        pr = self.plan_rows(rows)
        pending = [r for r in pr if r["status"] == "待渡"]
        cards: list[str] = []
        for day in days:
            head = self._opened(rows)[day]
            by = {k: [n for n in notes if n.get("date") == day
                      and n.get("kind") == k] for k in KINDS}
            w = head["water"]
            body: list[str] = []
            if by["记"]:
                body.append('<div class="seg"><span class="lab">起</span><div class="txt">'
                            + "".join(f"<p>{_esc(n['text'])}</p>" for n in by["记"])
                            + "</div></div>")
            if by["验"]:
                items = []
                for n in by["验"]:
                    s = f'<div class="exp"><div class="q">问　{_esc(n["text"])}</div>'
                    if n.get("evidence"):
                        s += f'<div class="rb">读回　{_esc(n["evidence"])}</div>'
                    if n.get("verdict"):
                        s += f'<div class="vd">判　{_esc(n["verdict"])}</div>'
                    if n.get("boundary"):
                        s += f'<div class="bd">不宣称　{_esc(n["boundary"])}</div>'
                    items.append(s + "</div>")
                body.append('<div class="seg"><span class="lab">验</span>'
                            '<div class="exps">' + "".join(items) + "</div></div>")
            if by["朱批"]:
                body.append('<div class="seg"><span class="lab">朱批</span>'
                            '<div class="verm">'
                            + "".join(f"<p>{_esc(n['text'])}</p>" for n in by["朱批"])
                            + "</div></div>")
            if by["收"]:
                s = "".join(f"<p>{_esc(n['text'])}</p>" for n in by["收"])
                for n in by["收"]:
                    if n.get("boundary"):
                        s += f'<p class="bd">不宣称　{_esc(n["boundary"])}</p>'
                body.append(f'<div class="seg"><span class="lab">收</span>'
                            f'<div class="txt">{s}</div></div>')
            if by["明日"]:
                body.append('<div class="seg"><span class="lab">明日</span><div class="txt">'
                            + "".join(f"<p>{_esc(n['text'])}"
                                      + (f'　<span class="acc">〔验收：{_esc(n["evidence"])}〕</span>'
                                         if n.get("evidence") else "") + "</p>"
                                      for n in by["明日"])
                            + "</div></div>")
            if by["锚"]:
                body.append('<div class="anch">锚　'
                            + " · ".join(f"<code>{_esc(n['text'])}</code>" for n in by["锚"])
                            + "</div>")
            cards.append(
                f'<article class="page" data-water="{_esc(w)}">'
                f'<header><div class="ttl">岸記 <span class="cross">· 第 '
                f'{self.crossing(day, rows)} 渡</span></div>'
                f'<div class="meta"><span>{_esc(day)}</span>'
                f'<span class="stamp w-{_esc(w)}">{_esc(w)}</span></div></header>'
                f'<div class="ask">今日问　{_esc(head["question"])}</div>'
                + "".join(body) + "</article>")
        plan_html = "".join(
            f'<li><span class="pd">{_esc(r["date"])}</span>{_esc(r["item"])}'
            + (f'<em>{_esc(r["accept"])}</em>' if r.get("accept") else "")
            + "</li>" for r in reversed(pending)) or "<li class='empty'>暂无待渡</li>"
        out = _HTML_SHELL.format(
            ndays=len(days), nexps=sum(1 for n in notes if n.get("kind") == "验"),
            nverm=sum(1 for n in notes if n.get("kind") == "朱批"),
            npending=len(pending), pages="".join(cards), plan=plan_html,
            rule=_esc(RULE))
        if self.theme == "手记":
            # 只换皮：HTML 结构、class 名、台账一律不变，判据读到的还是同一份内容。
            out = re.sub(r"<style>.*?</style>",
                         lambda _m: "<style>\n" + _CSS_CASUAL + "\n</style>",
                         out, count=1, flags=re.S)
            out = out.replace("岸記", "岸记")
        return out

    # ── 整册重渲染：每次写盘都读回比对 ────────────────────────────
    def render_all(self) -> dict:
        rows = self.rows()
        written: list[str] = []
        for day in self.days(rows):
            p = self.root / f"{day}.md"
            md = self.render_day(day, rows)
            p.write_text(md, encoding="utf-8")
            if p.read_text(encoding="utf-8") != md:
                raise JournalError(f"{p.name} 读回不一致")
            written.append(p.name)
        for path, text in ((self.plan_path, self.render_plan(rows)),
                           (self.book_path, self.render_book(rows))):
            path.write_text(text, encoding="utf-8")
            if path.read_text(encoding="utf-8") != text:
                raise JournalError(f"{path.name} 读回不一致")
            written.append(path.name)
        return {"rendered": written, "events": len(rows),
                "days": len(self.days(rows))}

    def today(self) -> str:
        return strftime("%Y-%m-%d")


#: 手记版皮肤：暖白纸、圆角、柔和陶土色、便签式朱批、水位点状标签。
#: 与宋版共用同一套 class 名，所以只换 <style>，HTML 与台账一个字不动。
_CSS_CASUAL = """
:root{--paper:#fdfaf3;--ink:#3c484a;--mute:#8b9490;--qing:#5b8890;
--qing-l:#dceae8;--zhu:#c0705a;--line:#ece5d8;--amber:#b08a4a;}
*{box-sizing:border-box}
body{margin:0;background:#f4f0e7;color:var(--ink);line-height:1.7;
font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 56px;}
header.top{border-bottom:none;padding-bottom:14px;margin-bottom:20px;
display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;}
h1{font-size:24px;font-weight:500;letter-spacing:2px;margin:0;}
.sub{color:var(--mute);font-size:12px;letter-spacing:0;margin-top:3px;}
.stats{display:flex;gap:10px;font-size:11px;color:var(--mute);}
.stats b{display:block;font-size:18px;color:var(--qing);font-weight:500;text-align:center;}
.page{background:var(--paper);border:0.5px solid var(--line);border-radius:16px;
padding:20px 22px 16px;margin-bottom:16px;}
.page header{display:flex;justify-content:space-between;align-items:center;
border-bottom:none;padding-bottom:6px;gap:10px;flex-wrap:wrap;}
.ttl{font-size:16px;letter-spacing:1px;}
.cross{font-size:11px;color:var(--mute);}
.meta{display:flex;align-items:center;gap:9px;font-size:12px;color:var(--mute);}
.stamp{border:none;border-radius:999px;padding:2px 10px;font-size:11px;
background:#fbeee9;color:#b3664f;}
.stamp::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;
background:#c9705a;margin-right:6px;vertical-align:middle;}
.stamp.w-潮{background:#eaf3f2;color:var(--qing);}
.stamp.w-潮::before{background:#8fb3ad;}
.stamp.w-平{background:#eef0ee;color:#7c8580;}
.stamp.w-平::before{background:#aab2ac;}
.stamp.w-雾{background:#faf1e2;color:var(--amber);}
.stamp.w-雾::before{background:#d7b073;}
.ask{background:#eaf3f2;border-radius:10px;padding:7px 11px;font-size:12px;
color:#4b7078;margin:10px 0 14px;}
.seg{display:grid;grid-template-columns:36px minmax(0,1fr);gap:10px;margin-bottom:12px;}
.lab{color:var(--zhu);font-size:11px;letter-spacing:1px;padding-top:3px;}
.txt p{margin:0 0 6px;font-size:13px;}
.exps{border-left:none;padding-left:0;}
.exp{font-size:13px;margin-bottom:7px;background:#f1f7f6;
border-radius:11px;padding:9px 12px;}
.q{font-size:13px;}
.rb{font-size:12px;color:var(--mute);}
.vd{color:var(--zhu);font-weight:500;}
.bd{color:var(--zhu);font-size:12px;}
.verm{border-left:none;border-radius:11px;padding:9px 12px;background:#fbeee9;color:#a15b47;}
.verm p{margin:0 0 6px;font-size:12px;}
.verm p:last-child{margin-bottom:0;}
.acc{color:var(--mute);font-size:12px;}
.anch{border-top:none;margin-top:8px;padding-top:4px;font-size:11px;
color:#9aa39e;word-break:break-all;}
code{font-family:ui-monospace,Consolas,monospace;font-size:11px;}
aside{background:var(--paper);border:0.5px solid var(--line);border-radius:16px;
padding:16px 18px 10px;position:sticky;top:20px;}
aside h2{font-size:14px;font-weight:500;letter-spacing:1px;margin:0 0 4px;}
aside .hint{font-size:11px;color:var(--mute);margin-bottom:10px;}
aside ul{list-style:none;padding:0;margin:0;}
aside li{border-top:0.5px solid #f0ebe0;padding:9px 0;font-size:12px;}
aside li .pd{display:block;color:var(--zhu);font-size:11px;}
aside li em{display:block;color:var(--mute);font-style:normal;font-size:11px;margin-top:3px;}
aside li.empty{color:var(--mute);}
.rule{margin-top:18px;border-top:none;padding-top:4px;
font-size:11px;color:#8fb3ad;letter-spacing:0;}
@media(max-width:820px){.cols{grid-template-columns:1fr;}aside{position:static;}}
"""

_HTML_SHELL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>岸记 · 荣珂实验手记</title>
<style>
:root{{--paper:#f8f4ea;--ink:#2f2c26;--mute:#7a7365;--qing:#4d6f78;
--qing-l:#b9cbc9;--zhu:#a8342a;--line:#d9cfb8;--amber:#8a6a24;}}
*{{box-sizing:border-box}}
body{{margin:0;background:#efe9dc;color:var(--ink);
font-family:"Songti SC","SimSun","Noto Serif CJK SC",Georgia,serif;line-height:1.7;}}
.wrap{{max-width:1080px;margin:0 auto;padding:32px 20px 60px;}}
header.top{{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:22px;
display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;}}
h1{{font-size:28px;font-weight:500;letter-spacing:8px;margin:0;}}
.sub{{color:var(--mute);font-size:13px;letter-spacing:1px;margin-top:4px;}}
.stats{{display:flex;gap:18px;font-size:12px;color:var(--mute);}}
.stats b{{display:block;font-size:20px;color:var(--qing);font-weight:500;}}
.cols{{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:22px;align-items:start;}}
.page{{background:var(--paper);border:1px solid var(--line);border-radius:3px;
padding:20px 24px 16px;margin-bottom:20px;}}
.page header{{display:flex;justify-content:space-between;align-items:center;
border-bottom:1px solid var(--line);padding-bottom:9px;gap:10px;flex-wrap:wrap;}}
.ttl{{font-size:17px;letter-spacing:3px;}}
.cross{{font-size:12px;color:var(--mute);letter-spacing:1px;}}
.meta{{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--mute);}}
.stamp{{border:1px solid var(--zhu);color:var(--zhu);padding:0 6px;font-size:12px;}}
.stamp.w-潮{{border-color:var(--qing);color:var(--qing);}}
.stamp.w-平{{border-color:var(--mute);color:var(--mute);}}
.stamp.w-雾{{border-color:var(--amber);color:var(--amber);}}
.ask{{font-size:13px;color:var(--qing);margin:12px 0 14px;}}
.seg{{display:grid;grid-template-columns:34px minmax(0,1fr);gap:12px;margin-bottom:12px;}}
.lab{{color:var(--zhu);font-size:12px;letter-spacing:2px;padding-top:2px;}}
.txt p{{margin:0 0 6px;font-size:13px;}}
.exps{{border-left:2px solid var(--qing-l);padding-left:12px;}}
.exp{{font-size:13px;margin-bottom:9px;}}
.rb{{font-size:12px;color:var(--mute);}}
.vd{{color:var(--zhu);font-weight:500;}}
.bd{{color:var(--zhu);font-size:12px;}}
.verm{{border-left:1px solid var(--line);padding-left:12px;color:#8c3a2f;}}
.verm p{{margin:0 0 7px;font-size:12px;}}
.acc{{color:var(--mute);font-size:12px;}}
.anch{{border-top:1px dashed var(--line);margin-top:12px;padding-top:8px;
font-size:11px;color:var(--mute);word-break:break-all;}}
code{{font-family:var(--font-monospace,monospace);font-size:11px;}}
aside{{background:var(--paper);border:1px solid var(--line);border-radius:3px;
padding:18px 18px 10px;position:sticky;top:20px;}}
aside h2{{font-size:15px;font-weight:500;letter-spacing:2px;margin:0 0 4px;}}
aside .hint{{font-size:11px;color:var(--mute);margin-bottom:12px;}}
aside ul{{list-style:none;padding:0;margin:0;}}
aside li{{border-top:1px solid var(--line);padding:9px 0;font-size:12px;}}
aside li .pd{{display:block;color:var(--zhu);font-size:11px;}}
aside li em{{display:block;color:var(--mute);font-style:normal;font-size:11px;margin-top:3px;}}
aside li.empty{{color:var(--mute);}}
.rule{{margin-top:20px;border-top:1px dashed var(--line);padding-top:9px;
font-size:11px;color:var(--qing);letter-spacing:1px;}}
@media(max-width:820px){{.cols{{grid-template-columns:1fr;}}aside{{position:static;}}}}
</style></head><body><div class="wrap">
<header class="top"><div><h1>岸記</h1>
<div class="sub">荣珂实验手记 · 规划看板　／　文体会变，证据不变</div></div>
<div class="stats"><div><b>{ndays}</b>渡</div><div><b>{nexps}</b>验</div>
<div><b>{nverm}</b>朱批</div><div><b>{npending}</b>待渡</div></div></header>
<div class="cols"><main>{pages}</main>
<aside><h2>规划看板</h2><div class="hint">单源：日记里的「明日」，结案不删原句</div>
<ul>{plan}</ul></aside></div>
<div class="rule">{rule}</div></div></body></html>
"""
