# -*- coding: utf-8 -*-
"""`rongke/web/library_api.py` 的独立单测：**不起服务、不碰真库**。

判什么：列表的默认值 / 筛选 / 服务端翻页边界 / source 三档 / 排序 / 非法入参，
读取的窗口收窄 / 越界 / 缺全文 / 缺 PDF / 工具失败。

**为什么要独立一套、而不是只靠 `kb_e2e_test.py`**：那一套要真起服务、真入库 PDF，
跑一次要几十秒且依赖 PDF 抽取器；而这里验的是**边界条件**（页号 0、跨度 21、
sort=abc、"工具回 ok:false"），这些在真库上根本造不出来 —— 造一个"工具返回
ok:false"的真库需要先把库弄坏。所以这一套用 `fetch` 假实现直接喂形状，
断言的是**这一层自己的逻辑**，与上游工具对不对无关。

判据一律行首 `✔/✘`（`regress.py::_RE_MARK` 认这个），汇总行不算数。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 cp936，行首那个 ✔ 会直接 UnicodeEncodeError ⇒ 先把流换成
# UTF-8。`regress.py` 自己会设 PYTHONIOENCODING，但单跑这一份时没人替它设。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                            # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke.web import library_api as L          # noqa: E402

ok_all = True
n_pass = 0


def check(label: str, got, want) -> None:
    """一条判据。行首 ✔/✘ 才会被 regress 数进去。"""
    global ok_all, n_pass
    if got == want:
        n_pass += 1
        print(f"✔ {label} → {got!r}")
    else:
        ok_all = False
        print(f"✘ {label} → 实际 {got!r}，期望 {want!r}")


def truthy(label: str, got) -> None:
    global ok_all, n_pass
    if got:
        n_pass += 1
        print(f"✔ {label} → {got!r}")
    else:
        ok_all = False
        print(f"✘ {label} → 实际 {got!r}，期望「真值」")


# ══════════════════════ 假注册表：按形状喂回包 ══════════════════════
#: 一个真的小库：5 篇，年份/标题刻意错开，三档 source 都有。
LIB = [
    {"id": "kb0001", "title": "Alpha 论文", "year": "2024", "status": "read",
     "fulltext": True, "journal": "J1", "tags": ["t1"]},
    {"id": "kb0002", "title": "beta paper", "year": "2022", "status": "unread",
     "fulltext": False, "abstract": "beta 的摘要"},
    {"id": "kb0003", "title": "Gamma 2020", "year": "2020", "status": "unread",
     "fulltext": True},
    {"id": "kb0004", "title": "delta", "year": "", "status": "read",
     "fulltext": False},
    {"id": "kb0005", "title": "Epsilon", "year": "2024", "status": "unread",
     "fulltext": True},
]


def _brief(it: dict) -> dict:
    """照着 `workbench_mcp._kb_brief` 投影：**故意不带 authors / abstract**。

    这是关键的一步：真实 `kb_list` 就不给这两个字段（省长度），而补齐它们正是
    被测逻辑要干的事。假件若把 abstract 原样吐出来，那一条判据就成了自证 ——
    第一版正是这样，于是"abstract 有没有补上"这条从来没被真正验过。
    """
    out = {"id": it.get("id"), "year": str(it.get("year") or ""),
           "title": str(it.get("title") or "")[:120],
           "status": it.get("status") or "unread",
           "fulltext": bool(it.get("fulltext"))}
    for k in ("journal", "doi", "pages"):
        if it.get(k):
            out[k] = it[k]
    if it.get("tags"):
        out["tags"] = list(it["tags"])[:8]
    return out


def make_fetch(lib=None, *, pdf_ids=(), fail=None, index=True, read=None,
               bibtex=None, root=None):
    """造一个 `fetch(tool, **args)` 假实现。记录每次调用，供"真的转发了吗"这类判据。

    `root` 是 `kb_list` 自报的库根 —— 默认指向 `ROOT_DIR`（那个真有一个 `pdfs/`
    的临时目录），否则 `has_pdf` 会全 False，而那是假件的错、不是被测逻辑的错。
    """
    items = [dict(x) for x in (lib if lib is not None else LIB)]
    calls: list[tuple[str, dict]] = []
    pdf = set(pdf_ids)
    libroot = root if root is not None else ROOT_DIR

    def fetch(tool: str, **args) -> dict:
        calls.append((tool, dict(args)))
        if fail and tool in fail:
            return fail[tool]() if callable(fail[tool]) else fail[tool]
        if tool == "kb_index":
            q = str(args.get("query") or "").lower()
            n = max(1, min(int(args.get("n") or 40), 50))
            rows = [x for x in items
                    if not q or q in f"{x['title']} {x['year']} {x['id']}".lower()]
            page = max(1, int(args.get("page") or 1))
            n_pages = max(1, (len(rows) + n - 1) // n)
            chunk = rows[(page - 1) * n:(page - 1) * n + n]
            return {"ok": True, "query": q, "total": len(rows), "page": page,
                    "n_pages": n_pages, "n": len(chunk),
                    "has_more": page < n_pages, "library_total": len(items),
                    "items": [{"doc_id": x["id"], "title": x["title"],
                               "year": x["year"], "fulltext": x["fulltext"]}
                              for x in chunk]}
        if tool == "kb_list":
            st = str(args.get("status") or "")
            sel = [x for x in items if not st or x["status"] == st]
            sel = sorted(sel, key=lambda x: str(x.get("year") or ""), reverse=True)
            lim = max(1, int(args.get("limit") or 50))
            shown = sel[:lim]
            out = {"ok": True, "library": root, "total": len(items),
                   "matched": len(sel), "showing": len(shown),
                   "fulltext": sum(1 for x in sel if x["fulltext"]),
                   "items": [_brief(x) for x in shown]}
            if len(sel) > len(shown):
                out["note"] = f"还有 {len(sel) - len(shown)} 篇没列出来"
            return out
        if tool == "kb_bibtex":
            if bibtex is not None:
                return bibtex
            ids = [x for x in str(args.get("ids") or "").split(",") if x]
            by = {x["id"]: x for x in items}
            parts = []
            for i, iid in enumerate(ids):
                it = by.get(iid) or {}
                # 与 litkb.export_bibtex 同形：abstract 在**头之前**、字段无逗号结尾。
                # ★ 作者名里嵌 **doc id** 而不是序号：这样"谁的作者落到了谁头上"
                #   一眼可判 —— 判据不必知道请求顺序，就能验出错行（第一版嵌的是
                #   序号，判据于是只能假设"序号即入参顺序"，而那正是被测的东西）。
                if it.get("abstract"):
                    parts.append(f"  abstract = {{{it['abstract']}}}")
                parts.append(f"@article{{litkb{i + 1:02d},\n"
                             f"  title = {{{it.get('title') or 'Untitled'}}},\n"
                             f"  author = {{AU_{iid} One and AU_{iid} Two}}\n"
                             f"  year = {{{it.get('year') or ''}}}\n}}")
            return {"ok": True, "library": root, "n": len(ids),
                    "entries": len(ids), "bibtex": "\n\n".join(parts),
                    "note": "fake"}
        if tool == "kb_read":
            if read is not None:
                return read
            return {"ok": False, "error": "fake 没实现 kb_read"}
        return {"ok": False, "error": f"fake 不认识 {tool}"}

    fetch.calls = calls          # type: ignore[attr-defined]
    return fetch


def root_of(ids=("kb0001", "kb0003")) -> str:
    """一个真有 `pdfs/` 的临时库根，让 `has_pdf` 能验出真假两档。

    `ids` 决定哪几篇有原件。传空元组 ⇒ 目录在、一份 PDF 都没有（负控用）。
    """
    d = Path(tempfile.mkdtemp(prefix="kbapi-root-"))
    (d / "pdfs").mkdir()
    for i in ids:
        (d / "pdfs" / f"{i}.pdf").write_bytes(b"%PDF-1.4 fake")
    return str(d)


ROOT_DIR = root_of()
TOOLS_ALL = {"kb_index", "kb_list", "kb_read", "kb_bibtex"}
TOOLS_NO_INDEX = {"kb_list", "kb_read", "kb_bibtex"}


def lst(fetch, qs=None, library="", tools=TOOLS_ALL, root=ROOT_DIR):
    return L.list_payload(fetch, qs=qs or {}, library=library, tools=tools,
                          library_root=lambda: root)


def rdp(fetch, **kw):
    kw.setdefault("tools", TOOLS_ALL)
    return L.read_payload(fetch, **kw)


print("=" * 78)
print("A · 入参归一：默认值 / 旧参数宽容 / 新参数严格")
print("=" * 78)
p = L.parse_list_params({})
check("空 query ⇒ 默认 sort=year", p["sort"], "year")
check("空 query ⇒ year 默认倒序（新的在前）", p["order"], "desc")
check("空 query ⇒ source=all", p["source"], "all")
check("空 query ⇒ page=1", p["page"], 1)
check("空 query ⇒ page_size=40（与 kb_index 默认页同大）", p["page_size"], 40)
check("空 query ⇒ q 为空串（不是 None）", p["q"], "")
check("sort=title 时 order 默认正序（A→Z）",
      L.parse_list_params({"sort": "title"})["order"], "asc")

# 旧参数 `limit`：坏值必须降级，绝不抛 —— routes_test 钉着"坏数值不许断连/5xx"。
check("★ 旧参数 limit=abc ⇒ 降级 200（不抛异常）",
      L.parse_list_params({"limit": "abc"})["page_size"], 200)
check("★ 旧参数 limit=-1 ⇒ clamp 到 1",
      L.parse_list_params({"limit": "-1"})["page_size"], 1)
check("★ 旧参数 limit=0 ⇒ clamp 到 1",
      L.parse_list_params({"limit": "0"})["page_size"], 1)
check("旧参数 limit=500 ⇒ 500（旧口径保持）",
      L.parse_list_params({"limit": "500"})["page_size"], 500)
check("旧参数 limit=9999 ⇒ clamp 到 500",
      L.parse_list_params({"limit": "9999"})["page_size"], 500)
check("page_size 优先于 limit（新参数赢）",
      L.parse_list_params({"page_size": "7", "limit": "500"})["page_size"], 7)

# 新参数：非法必须 400，且错误文案带上合法域。
for qs, why in (({"page": "0"}, "page=0"),
                ({"page": "-2"}, "page=-2"),
                ({"page": "abc"}, "page=abc"),
                ({"page_size": "0"}, "page_size=0"),
                ({"page_size": "501"}, "page_size=501"),
                ({"page_size": "x"}, "page_size=x"),
                ({"sort": "author"}, "sort=author"),
                ({"order": "up"}, "order=up"),
                ({"source": "video"}, "source=video")):
    try:
        L.parse_list_params(qs)
        check(f"★ 非法 {why} ⇒ 400", "没抛", "抛 RequestError")
    except L.RequestError as e:
        check(f"★ 非法 {why} ⇒ 400", e.status, 400)

print()
print("=" * 78)
print("B · 列表默认：kb_index 优先，字段补齐，has_pdf 按盘上事实")
print("=" * 78)
f = make_fetch(pdf_ids=["kb0001", "kb0003"])
code, d = lst(f)
check("默认列表 → 200", code, 200)
check("默认列表 ok", d.get("ok"), True)
check("★ 走了 kb_index（能真翻页的那条路）", d.get("via"), "kb_index")
check("total = 全库 5 篇", d.get("total"), 5)
check("showing = 本页 5 篇", d.get("showing"), 5)
check("has_more：5 篇 < 40 ⇒ 没有下一页", d.get("has_more"), False)
check("page / page_size 如实回", (d.get("page"), d.get("page_size")), (1, 40))
check("库根取工具自报的那个", d.get("library"), ROOT_DIR)
check("★ 年份默认倒序（2024 在前）",
      [i["id"] for i in d["items"]], ["kb0001", "kb0005", "kb0002", "kb0003", "kb0004"])
by = {i["id"]: i for i in d["items"]}
check("★ has_pdf 认盘上真有的那两篇", (by["kb0001"]["has_pdf"], by["kb0003"]["has_pdf"]),
      (True, True))
check("★ 盘上没有 PDF 的 ⇒ has_pdf=False（fulltext=True 也不冒充满）",
      (by["kb0002"]["has_pdf"], by["kb0005"]["has_pdf"]), (False, False))
check("明细字段（status）从 kb_list 补上了", by["kb0001"]["status"], "read")
check("明细字段（journal）补上了", by["kb0001"].get("journal"), "J1")
check("明细字段（tags）补上了", by["kb0001"].get("tags"), ["t1"])
check("★ 每篇的 authors 从 kb_bibtex 补上了",
      by["kb0001"].get("authors"), ["AU_kb0001 One", "AU_kb0001 Two"])
check("★ 作者的归属没有错行（每篇拿到的都是**自己**的作者）",
      [by[k].get("authors", [""])[0] for k in ("kb0001", "kb0005", "kb0002", "kb0003",
                                               "kb0004")],
      ["AU_kb0001 One", "AU_kb0005 One", "AU_kb0002 One", "AU_kb0003 One",
       "AU_kb0004 One"])
check("★ 每篇的 abstract 也补上了", by["kb0002"].get("abstract"), "beta 的摘要")
truthy("没有 meta_note（全补齐了就不该有抱怨）", "meta_note" not in d)
check("fulltext 计数 = 命中集里有正文的 3 篇", d.get("fulltext"), 3)

# 负控：换一个**没有 PDF** 的库根 ⇒ 同一批条目 has_pdf 必须全 False。
# ⚠️ 必须换根，而不是只把 `pdf_ids` 清空：假件的 PDF 列表根本没被读（`_has_pdf`
#    只看盘），清它等于什么都没改 —— 第一版就是这么写的，判据看着红、其实是空转。
EMPTY_ROOT = root_of(())
code, d2 = lst(make_fetch(), root=EMPTY_ROOT)
check("★ 负控：盘上一份 PDF 都没有 ⇒ has_pdf 全 False",
      [i["has_pdf"] for i in d2["items"]], [False] * 5)
check("★ 负控：但 fulltext 事实不受影响（3 篇有正文）", d2.get("fulltext"), 3)
# 库根根本拿不到时（老内核的回包可能不带 library），照样不许猜：全 False + 说明。
code, d3 = lst(make_fetch(), root="")
check("★ 库根为空 ⇒ has_pdf 一律 False（不猜盘上约定）",
      [i["has_pdf"] for i in d3["items"]], [False] * 5)
truthy("★ 且 note 里说清 has_pdf 是按 false 给的",
       "has_pdf" in str(d3.get("note")))

print()
print("=" * 78)
print("C · 服务端翻页：page/page_size 真的切片，total 是全集")
print("=" * 78)
f = make_fetch(pdf_ids=["kb0001", "kb0003"])
code, d = lst(f, {"page": "2", "page_size": "2"})
check("page=2&page_size=2 → 200", code, 200)
check("★ 本页 2 条", d.get("showing"), 2)
check("★ total 仍是 5（不是本页 2）", d.get("total"), 5)
check("★ has_more=True（第 3 页还有）", d.get("has_more"), True)
check("★ 第 2 页是年份倒序的第 3、4 条",
      [i["id"] for i in d["items"]], ["kb0002", "kb0003"])
# 翻页不许漏也不许重：把三页拼起来必须正好是全库，且无重复。
allids = []
for pg in (1, 2, 3):
    _, dp = lst(make_fetch(pdf_ids=[]), {"page": str(pg), "page_size": "2"})
    allids.extend(i["id"] for i in dp["items"])
check("★ 三页拼起来 = 全库 5 篇", len(allids), 5)
check("★ 且没有重复（翻页不重不漏）", len(set(allids)), 5)
_, dl = lst(make_fetch(pdf_ids=[]), {"page": "3", "page_size": "2"})
check("最后一页 has_more=False", dl.get("has_more"), False)
check("最后一页 showing=1", dl.get("showing"), 1)
_, d0 = lst(make_fetch(pdf_ids=[]), {"page": "99", "page_size": "2"})
check("★ 越界页 ⇒ 200 + 空 items（不是 400/500：翻过头是正常操作）", d0.get("items"), [])
check("★ 越界页仍如实回 total", d0.get("total"), 5)
check("越界页 has_more=False", d0.get("has_more"), False)

# 兼容路（没有 kb_index）：必须在服务端切片，不能把 500 条交给前端。
fk = make_fetch(pdf_ids=[], index=False)
check("★ 没有 kb_index ⇒ 退回 kb_list", lst(fk, {}, tools=TOOLS_NO_INDEX)[1].get("via"),
      "kb_list")
code, d = lst(fk, {"page": "2", "page_size": "2"}, tools=TOOLS_NO_INDEX)
check("兼容路 page=2&page_size=2 ⇒ 本页 2 条", d.get("showing"), 2)
check("★ 兼容路也在服务端切片（total 是全集 5，不是 2）", d.get("total"), 5)
check("★ 兼容路第 2 页内容与 kb_index 路一致（两条路不许给出两个答案）",
      [i["id"] for i in d["items"]], ["kb0002", "kb0003"])

print()
print("=" * 78)
print("D · 筛选：status / q（只匹配元数据）/ source 三档")
print("=" * 78)
fk = make_fetch(pdf_ids=["kb0001", "kb0003"], index=False)
code, d = lst(fk, {"status": "read"}, tools=TOOLS_NO_INDEX)
check("status=read ⇒ 2 篇", d.get("total"), 2)
check("status=read ⇒ 都是 read",
      {i["status"] for i in d["items"]}, {"read"})
check("★ 带 status 时走 kb_list（kb_index 不支持 status）", d.get("via"), "kb_list")

f = make_fetch(pdf_ids=["kb0001", "kb0003"])
code, d = lst(f, {"q": "Gamma"})
check("q=Gamma ⇒ 命中 1 篇（题名子串）", d.get("total"), 1)
check("q=Gamma ⇒ 命中的是 kb0003", [i["id"] for i in d["items"]], ["kb0003"])
code, d = lst(make_fetch(pdf_ids=[]), {"q": "2024"})
check("★ q=2024 ⇒ 按**年份**匹配到 2 篇", d.get("total"), 2)
code, d = lst(make_fetch(pdf_ids=[]), {"q": "kb0005"})
check("★ q=kb0005 ⇒ 按 **id** 匹配到 1 篇", d.get("total"), 1)
code, d = lst(make_fetch(pdf_ids=[]), {"q": "绝不存在的词"})
check("★ q 无命中 ⇒ ok=true + total=0（「没有」与「出错」分开）", d.get("ok"), True)
check("★ 且 total=0（不是报错）", d.get("total"), 0)
code, d = lst(make_fetch(pdf_ids=[]), {"q": "ALPHA"})
check("q 大小写不敏感", d.get("total"), 1)

f = make_fetch(pdf_ids=["kb0001", "kb0003"])
_, d = lst(f, {"source": "pdf"})
check("★ source=pdf ⇒ 只有盘上有 PDF 的 2 篇", d.get("total"), 2)
check("source=pdf ⇒ 全是 has_pdf",
      all(i["has_pdf"] for i in d["items"]), True)
_, d = lst(make_fetch(pdf_ids=["kb0001", "kb0003"]), {"source": "text"})
check("★ source=text ⇒ 有全文但盘上没 PDF 的 1 篇", d.get("total"), 1)
check("source=text ⇒ 命中的是 kb0005", [i["id"] for i in d["items"]], ["kb0005"])
_, d = lst(make_fetch(pdf_ids=["kb0001", "kb0003"]), {"source": "meta"})
check("★ source=meta ⇒ 既没 PDF 也没全文的 2 篇", d.get("total"), 2)
check("source=meta ⇒ 是 kb0002/kb0004",
      sorted(i["id"] for i in d["items"]), ["kb0002", "kb0004"])
check("★ 三档加起来必须等于全库（不重不漏）", 2 + 1 + 2, 5)
_, d = lst(make_fetch(pdf_ids=[]), {"source": "all"})
check("source=all ⇒ 全库 5 篇", d.get("total"), 5)

print()
print("=" * 78)
print("E · 排序：year / title，稳定且与 order 一致")
print("=" * 78)
_, d = lst(make_fetch(pdf_ids=[]), {"sort": "year", "order": "desc"})
check("sort=year&order=desc ⇒ 2024,2024,2022,2020,无年份垫底",
      [i["id"] for i in d["items"]], ["kb0001", "kb0005", "kb0002", "kb0003", "kb0004"])
_, d = lst(make_fetch(pdf_ids=[]), {"sort": "year", "order": "asc"})
check("★ sort=year&order=asc ⇒ 倒过来（无年份的最前，按 -1 排）",
      [i["id"] for i in d["items"]], ["kb0004", "kb0003", "kb0002", "kb0001", "kb0005"])
_, d = lst(make_fetch(pdf_ids=[]), {"sort": "title", "order": "asc"})
check("★ sort=title&order=asc ⇒ 按标题小写字典序",
      [i["id"] for i in d["items"]], ["kb0001", "kb0002", "kb0004", "kb0005", "kb0003"])
# 稳定性：同一年份的两条，翻页时次序必须恒等于 id 序（不许因分页而抖动）。
_, d = lst(make_fetch(pdf_ids=[]), {"sort": "year", "order": "desc",
                                    "page": "1", "page_size": "1"})
check("★ 稳定性：2024 那一年先出 id 小的那篇", d["items"][0]["id"], "kb0001")
_, d = lst(make_fetch(pdf_ids=[]), {"sort": "year", "order": "desc",
                                    "page": "2", "page_size": "1"})
check("★ 稳定性：下一页接的是同年的另一篇", d["items"][0]["id"], "kb0005")

print()
print("=" * 78)
print("F · 工具失败**不冒充空库**")
print("=" * 78)
code, d = lst(make_fetch(fail={"kb_list": {"ok": False, "error": "库文件坏了"},
                               "kb_index": {"ok": False, "error": "库文件坏了"}}),
              tools=TOOLS_NO_INDEX)
check("★ 工具 ok:false ⇒ 列表也 ok:false", d.get("ok"), False)
truthy("★ 且把工具的原话带出来（不是只说「空」）", "库文件坏了" in str(d.get("error")))
check("★ 且没有 items 这个键（空列表会被读成「库里没有」）", "items" in d, False)

fk = make_fetch(fail={"kb_index": {"ok": False, "error": "索引炸了"}}, pdf_ids=["kb0001"])
code, d = lst(fk)
check("★ kb_index 挂了 ⇒ 自动退回 kb_list 且仍能出结果", d.get("ok"), True)
check("★ 退回后 total 仍是 5（不是 0）", d.get("total"), 5)
check("★ 回包里如实说是退回的", d.get("via"), "kb_list")
truthy("★ 并在 note 里点名 kb_index 挂了", "索引炸了" in str(d.get("note")))
# 两个工具都挂 ⇒ 不许自己编一个空库出来（也不许 5xx）。
code, d = lst(make_fetch(fail={"kb_list": {"ok": False, "error": "坏"},
                               "kb_index": {"ok": False, "error": "坏"}}))
check("★ 两条路都挂 ⇒ 200 + ok:false（不 5xx）", code, 200)
check("★ 且 total 不给（给 0 就成了「库里没有」）", "total" in d, False)
truthy("★ 抱怨里两个工具都点名了", "kb_index" in str(d.get("note")))

print()
print("=" * 78)
print("G · 读取：窗口真的收窄 / 越界 / 缺全文 / 工具失败")
print("=" * 78)
CALLS = {}


def read_fetch(pages_total=200):
    """假的 `kb_read`：回包按**本次实参**的窗口生成，正文里带窗口标记。

    ⚠️ 回包必须在 `fetch()` **内部**按 `args` 现算（第一版在外层只算了一次，
    于是无论传什么页码，回包都写着 `[1, 200]` —— 判据全红，查下来是假件
    把"窗口"这个被测对象自己写死了，等于没测）。
    """
    def fetch(tool: str, **args) -> dict:
        CALLS.setdefault(tool, []).append(dict(args))
        if tool != "kb_read":
            return make_fetch()(tool, **args)
        lo = int(args.get("page_from") or 1)
        hi = int(args.get("page_to") or pages_total)
        hi = min(hi, pages_total)
        if lo > pages_total:
            return {"ok": False, "empty_page": True, "pages_total": pages_total,
                    "page_max": pages_total, "doc_id": str(args.get("doc_id") or "")}
        return {"ok": True, "doc_id": str(args.get("doc_id") or ""),
                "title": "Alpha", "library": ROOT_DIR,
                "pages_total": pages_total, "page_range": [lo, hi],
                "chars": 12, "char_total": 9999, "truncated": False,
                "text": f"[p.{lo}-{hi}] 正文"}
    return fetch


f = read_fetch()
code, d = rdp(f, doc_id="kb0001")
check("空页码 ⇒ 200", code, 200)
check("★ 都不给 ⇒ 窗口是 1..20（不是 1..末页）",
      (d.get("page_from"), d.get("page_to")), (1, 20))
check("★ 导航窗 1..20 逐页取回（末次是第 20 页，不整窗截字）",
      (CALLS["kb_read"][0]["page_from"], CALLS["kb_read"][-1]["page_to"]), (1, 20))
check("pages_total 如实回", d.get("pages_total"), 200)
check("★ has_more_pages=True（后面还有 180 页没读）", d.get("has_more_pages"), True)
check("truncated 用工具的字符口径", d.get("truncated"), False)
check("page_range 用工具实际交回的", d.get("page_range"), [1, 20])
truthy("正文原样带回", "正文" in str(d.get("text")))

CALLS.clear()
f = read_fetch()
code, d = rdp(f, doc_id="kb0001", page_from="5")
check("★ 只给 page_from=5 ⇒ 200", code, 200)
check("★ 窗口 5..24（page_to 真的补上了）",
      (d.get("page_from"), d.get("page_to")), (5, 24))
check("★ 转发给工具的最后一页就是导航窗末页",
      CALLS["kb_read"][-1]["page_to"], 24)
check("★ 20 页导航窗拆成 20 次一页读取",
      len(CALLS["kb_read"]), 20)

CALLS.clear()
code, d = rdp(read_fetch(), doc_id="kb0001", page_from="1", page_to="20")
check("★ 跨度正好 20 ⇒ 200", code, 200)
code, d = rdp(read_fetch(), doc_id="kb0001", page_from="1", page_to="21")
check("★ 跨度 21 ⇒ 400", code, 400)
truthy("★ 400 的说明里带上限", "20" in str(d.get("error")))
code, d = rdp(read_fetch(), doc_id="kb0001", page_to="50")
check("★ 只给 page_to=50 ⇒ 400（1..50 超跨度）", code, 400)

# 非法数字：一律 400，且不许把坏值转成 0 悄悄放行。
for bad, why in (({"page_from": "abc"}, "page_from=abc"),
                 ({"page_to": "-1"}, "page_to=-1"),
                 ({"page_from": "1.5"}, "page_from=1.5"),
                 ({"page_to": " "}, "page_to=空白→不限（不算非法）")):
    code, d = rdp(read_fetch(), doc_id="kb0001", **bad)
    want = 200 if "空白" in why else 400
    check(f"★ {why} ⇒ {want}", code, want)

code, d = rdp(read_fetch(), doc_id="kb0001", page_from="9", page_to="4")
check("★ page_from > page_to ⇒ 400", code, 400)
code, d = rdp(read_fetch(), doc_id="")
check("★ 空 doc_id ⇒ 400", code, 400)
truthy("★ 空 doc_id 的说明说了去 list 拿 id", "list" in str(d.get("hint")))
code, d = rdp(read_fetch(), doc_id="   ")
check("★ 全是空格的 doc_id 也算空 ⇒ 400", code, 400)

# 越界：总页数只有工具知道 ⇒ 靠回包判。
CALLS.clear()
f = read_fetch()
code, d = rdp(f, doc_id="kb0001", page_from="201", page_to="205")
check("★ 越过最后正文页 ⇒ 明确空窗，不回退整篇", code, 200)
check("空窗回包里带 pages_total 供前端纠正", d.get("pages_total"), 200)
truthy("空窗说明指出没有抽出的正文", "没有抽出的正文" in str(d.get("hint")))
code, d = rdp(read_fetch(), doc_id="kb0001", page_from="190", page_to="200")
check("★ 190..200 在范围内 ⇒ 200", code, 200)

# 缺全文 / 认不出 id / 工具失败：三句话必须不同。
code, d = rdp(make_fetch(fail={"kb_read": {"ok": False, "doc_id": "kb0002",
                                           "error": "这篇没有全文缓存"}}),
              doc_id="kb0002")
check("★ 认得出这篇但没全文 ⇒ 409（不是 404、也不是空文档）", code, 409)
truthy("★ 且把「没有全文缓存」的原话带出来",
       "全文" in str(d.get("error")))
code, d = rdp(make_fetch(fail={"kb_read": {"ok": False,
                                           "error": "库里没有这个 id: kb9999"}}),
              doc_id="kb9999")
check("★ 认不出这个 id ⇒ 404", code, 404)
code, d = rdp(make_fetch(fail={"kb_read": {"ok": True, "doc_id": "kb0001",
                                           "pages_total": 3, "page_range": [1, 1],
                                           "text": ""}}),
              doc_id="kb0001")
check("★ 工具说成功但正文为空 ⇒ 502（绝不冒充空文档）", code, 502)
truthy("★ 502 的说明里点明「不把它当空文档」",
       "空文档" in str(d.get("error")))
code, d = rdp(make_fetch(fail={"kb_read": "这不是字典"}), doc_id="kb0001")
check("★ 回包不成形 ⇒ 502", code, 502)
code, d = rdp(make_fetch(), doc_id="kb0001", tools={"kb_list"})
check("★ 注册表里没有 kb_read ⇒ 503（接线断了，不是这篇没正文）", code, 503)

# truncated：工具的字符截断必须**如实**传上来，不许当成"就这么多"。
def truncated_read(tool, **args):
    p = int(args.get("page_from") or 1)
    return {"ok": True, "doc_id": "kb0001", "pages_total": 3, "page_max": 3,
            "page_range": [p, p], "chars": 500, "char_total": 9000,
            "truncated": True, "text": "短正文"}
f = truncated_read
code, d = rdp(f, doc_id="kb0001", page_from="1", page_to="1")
check("单页被截断时不冒充全文", code, 502)
check("★ 截断错误说明保留处理建议", "上限" in str(d), True)
check("★ 不交付不完整正文", "text" in d, False)

print()
print("=" * 78)
print("H · 元数据补齐的两种坏情况：不许填错行")
print("=" * 78)
# 认不出的 id ⇒ kb_bibtex 回 missing ⇒ **整批不填**，而不是错位填。
code, d = lst(make_fetch(pdf_ids=[], bibtex={"ok": True, "n": 5, "entries": 4,
                                            "bibtex": "@article{litkb01,\n"
                                                      "  author = {X Y}\n}",
                                            "missing": ["kb0004"]}))
truthy("★ 有条目认不出 ⇒ 回包里如实抱怨", "meta_note" in d)
truthy("★ 且抱怨里点名认不出哪个", "kb0004" in str(d.get("meta_note")))
check("★ 且这一批一条都不填（填错行比空着糟）",
      all("authors" not in i for i in d["items"]), True)

# 条数对不上（上游悄悄截断）⇒ 同样不填，且说实话。
code, d = lst(make_fetch(pdf_ids=[], bibtex={"ok": True, "n": 3, "entries": 3,
                                            "bibtex": "@article{litkb01,\n"
                                                      "  author = {X Y}\n}"}))
truthy("★ 请求 5 篇、导出 3 条 ⇒ 抱怨条数对不上", "条数" in str(d.get("meta_note")))
check("★ 且一条都不填", all("authors" not in i for i in d["items"]), True)

# 正控：条数对得上就真的填进去（证明上面两条不是"永远不填"）。
code, d = lst(make_fetch(pdf_ids=[]))
check("★ 正控：条数对得上的正常批次确实填了 authors",
      d["items"][0].get("authors"), ["AU_kb0001 One", "AU_kb0001 Two"])
truthy("正控：且没有 meta_note", "meta_note" not in d)

print()
print("=" * 78)
print("I · 契约键（前端照着这些写，缺一个就是接线断了）")
print("=" * 78)
code, d = lst(make_fetch(pdf_ids=["kb0001"]))
for k in ("ok", "library", "total", "matched", "showing", "fulltext", "items",
          "page", "page_size", "has_more"):
    truthy(f"list 回包有 {k}", k in d)
for k in ("id", "title", "year", "status", "fulltext", "has_pdf"):
    truthy(f"list 条目有 {k}", k in d["items"][0])
code, d = rdp(read_fetch(), doc_id="kb0001")
for k in ("ok", "doc_id", "title", "pages_total", "page_from", "page_to",
          "page_range", "truncated", "text"):
    truthy(f"read 回包有 {k}", k in d)

print()
print("=" * 78)
if ok_all:
    print(f"[DONE] 文献库接口单测全部通过（{n_pass} 条断言）")
else:
    print("[DONE] 文献库接口单测有失败项 —— 上面每行 ✘ 都带了期望与实际")
sys.exit(0 if ok_all else 1)
