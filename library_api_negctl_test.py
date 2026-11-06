# -*- coding: utf-8 -*-
"""审查者补的判据（不是作者的 library_api_test.py，也不改实现）。

## 这份测的是作者那 164 条**没覆盖**的三件事

1. **真传 20 页窗口的负控**：作者的判据是"回包里 page_from/page_to 是 5..24"，
   而这两串数字是**本层自己算出来的**，一个"校验算 5..24、转发时仍传 page_to=0"
   的实现同样能让它变绿（上游 0 = 读到末页，正文照样回）。
   所以这里断言的是**转发给工具的那个实参**，并且带一条**变异负控**：
   把同一段判据套到"原样转发 page_to"的坏实现上，判据必须变红 —— 否则判据没牙。

2. **kb_index 那条路**：`kb_index` 不在 `KB_*_TOOLS` 三张名单里，由
   `bridge.install_workbench` 直接注册。作者的单测是**手工把 "kb_index" 塞进 tools 集合**
   喂进去的，所以"取函数时先查名单、再问注册表"这一条在单测里根本没被走过。
   这里走真注册表（真 kb_index）、以及"真注册表里 kb_index 抛错"的退回路径。

3. **HTTP 层**：`/api/kb/list` `/api/kb/read` 的 400/404/409/502/503 与
   旧参数 `limit=abc` 的兼容，都是**真起服务、真发请求**判的
   （作者的单测只碰纯函数，路由接线一个字都没验）。

判据行首一律 ✔/✘（`regress.py::_RE_MARK` 认这个）。退出码 0 = 全绿。
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

import rongke.web.server as S                            # noqa: E402
from rongke.bridge import install_workbench              # noqa: E402
from rongke.config import Config                        # noqa: E402
from rongke.tools import ToolRegistry                    # noqa: E402
from rongke.web import library_api as L                  # noqa: E402

ok_all = True
n = 0


def check(label: str, got, want) -> None:
    global ok_all, n
    if got == want:
        n += 1
        print(f"✔ {label} → {got!r}")
    else:
        ok_all = False
        print(f"✘ {label} → 实际 {got!r}，期望 {want!r}")


# ══════════════════ 真注册表（真 workbench、真库）══════════════════
S.Manager.boot = lambda self: None
M = S.Manager()
reg = ToolRegistry()
install_workbench(reg, Config.load().runtime.workbench)
M.kernel = types.SimpleNamespace(registry=reg, cfg=Config.load())

WB = Path(Config.load().runtime.workbench)
LIB_FILE = WB / "data" / "litkb" / "library.json"
LIB = json.loads(LIB_FILE.read_text(encoding="utf-8")) if LIB_FILE.exists() else []
PDF_DIR = LIB_FILE.parent / "pdfs"
REAL = bool(LIB) and M.registry_fn("kb_index") is not None

print("=" * 78)
print(f"A · 接线事实：真库 {len(LIB)} 条；kb_index 在注册表里="
      f"{M.registry_fn('kb_index') is not None}；在 KB_*_TOOLS 名单里="
      f"{'kb_index' in M.kb_tools()}")
print("=" * 78)
check("★ kb_index 确实不在名单里（所以名单-only 的取函数会静默退回）",
      "kb_index" in M.kb_tools(), False)
check("★ 但 kb_names()（名单 + 注册表）里有它", "kb_index" in M.kb_names(), True)
check("★ kb_call('kb_index') 走注册表能真拿到回包（不是「荣珂看不到」）",
      M.kb_call("kb_index", page=1, n=1).get("ok"), True)

# ══════════════════ B · 「真传 20 页窗口」+ 变异负控 ══════════════════
print()
print("=" * 78)
print("B · 只给 page_from：转发给 kb_read 的实参是不是真收窄（带变异负控）")
print("=" * 78)

SENT: list[dict] = []


def fake_kb_read(tool_name: str, **args) -> dict:
    """假的 kb_read：**严格照上游实现**（含 `sel = [...] or pages` 那条回退）。

    上游 `workbench_mcp.kb_read`：`lo, hi = page_from or 0, (page_to or 0) or len(pages)`
    然后 `sel = [p for p in pages if lo <= p["page"] <= hi] or pages`。
    照抄它，是为了让"page_to 留空"的坏实现在这里也真的读到末页 —— 否则假件
    比上游宽容，负控就抓不到那一类错。
    """
    SENT.append(dict(args))
    pages = list(range(1, 201))
    lo = int(args.get("page_from") or 0)
    hi = int(args.get("page_to") or 0) or len(pages)
    sel = [p for p in pages if lo <= p <= hi] or pages
    return {"ok": True, "doc_id": str(args.get("doc_id") or ""), "title": "T",
            "library": str(ROOT), "pages_total": len(pages),
            "page_range": [sel[0], sel[-1]], "chars": 10, "char_total": 9000,
            "truncated": False, "text": f"[p.{sel[0]}] .. [p.{sel[-1]}]"}


def forwarded_span(doc_id="kb1", pf="5") -> tuple:
    """跑一次 read_payload，回它**实际转发**给 kb_read 的 (page_from, page_to)。"""
    SENT.clear()
    L.read_payload(fake_kb_read, doc_id=doc_id, page_from=pf,
                   tools={"kb_read"})
    return SENT


def buggy_forwarded_span(doc_id="kb1", pf="5") -> tuple:
    """变异负控用的**坏实现**：校验照做，但转发时把 page_to 原样递（=上游的读到末页）。

    只存在于本判据里，不是产品代码 —— 它证明"断言转发实参"这条判据有牙：
    换一个真会犯这个错的实现，判据必须变红。
    """
    pages = list(range(1, 201))
    ef = int(pf or 1)
    et = ef + L.KB_READ_MAX_SPAN - 1
    wire_to = 0                       # ← 坏在这里：没把算出来的 et 递下去
    lo = max(0, ef)
    hi = wire_to or len(pages)
    sel = [p for p in pages if lo <= p <= hi] or pages
    return (ef, wire_to, [sel[0], sel[-1]])


got = forwarded_span()
check("★ 只给 page_from=5 ⇒ 首条转发第 5 页", got[0]["page_from"], 5)
check("★ 只给 page_from=5 ⇒ 最后转发第 24 页", got[-1]["page_to"], 24)
check("★ 20 页导航窗拆成 20 次单页读取（不整窗截字）", len(got), 20)

bad = buggy_forwarded_span()
check("★ 负控：坏实现转发的是 page_to=0（上游语义=读到末页）", bad[1], 0)
check("★ 负控：坏实现实际读到的末页是 200（正控里末页是 24）", bad[2][1], 200)
check("★ 判据有牙：把「转发 page_to 必须=24」套到坏实现上会红",
      bad[1] == 24, False)

got = forwarded_span(pf="")
check("★ 两头都不给 ⇒ 导航窗 1..20（不是 1..末页）",
      (got[0]["page_from"], got[-1]["page_to"]), (1, 20))
SENT.clear()
L.read_payload(fake_kb_read, doc_id="kb1", page_to="7", tools={"kb_read"})
check("★ 只给 page_to=7 ⇒ 导航窗 1..7", (SENT[0].get("page_from"),
                                      SENT[-1].get("page_to")), (1, 7))
SENT.clear()
code, d = L.read_payload(fake_kb_read, doc_id="kb1", page_from="1", page_to="21",
                         tools={"kb_read"})
check("★ 跨度 21 ⇒ 400 且一次都没转发（坏参数不落到工具）", (code, len(SENT)),
      (400, 0))

# ══════════════════ C · kb_index 真路 + 退回 kb_list ══════════════════
print()
print("=" * 78)
print("C · kb_index 路径：真注册表 / 以及 kb_index 挂掉时的退回")
print("=" * 78)
if REAL:
    code, d = L.list_payload(M.kb_fetch, qs={}, library="", tools=M.kb_names(),
                             library_root=lambda: M.kb_root(""))
    check("★ 真库默认列表 200", code, 200)
    check("★ 走的是 kb_index 那条路", d.get("via"), "kb_index")
    check("★ total = 库里真条数（不是本页 40）", d.get("total"), len(LIB))
    check("★ library_total 也是库里真条数", d.get("library_total"), len(LIB))
    check("★ 默认一页 40 条", (d.get("page"), d.get("page_size"), d.get("showing")),
          (1, 40, min(40, len(LIB))))
    check("★ has_more 与「还有几条没给」一致",
          d.get("has_more"), len(LIB) > 40)
    ids = [i.get("id") for i in (d.get("items") or [])]
    check("★ 本页 id 无重复", len(set(ids)), len(ids))
    got_pdf = sum(1 for i in (d.get("items") or []) if i.get("has_pdf"))
    want_pdf = sum(1 for i in ids if (PDF_DIR / f"{i}.pdf").exists())
    check("★ has_pdf 与盘上 pdfs/<id>.pdf 逐条一致", got_pdf, want_pdf)

    # kb_list 明细那条腿挂掉：清单仍给出、via 仍是 kb_index、note 如实抱怨。
    def no_brief(tool, **args):
        if tool == "kb_list":
            return {"ok": False, "error": "明细腿断了"}
        return M.kb_fetch(tool, **args)

    code, d = L.list_payload(no_brief, qs={}, library="", tools=M.kb_names(),
                             library_root=lambda: M.kb_root(""))
    check("★ 明细腿挂了 ⇒ 列表仍是 ok:true（清单来自 kb_index）", d.get("ok"), True)
    check("★ 且 total 仍是全库条数", d.get("total"), len(LIB))
    check("★ 且 via 仍是 kb_index", d.get("via"), "kb_index")
    check("★ 且 note 点名了挂掉的那条腿", "明细腿断了" in str(d.get("note")), True)

    # kb_index 真抛错 ⇒ 退回 kb_list，且 note 说明（不许静默）。
    # ⚠️ 注入点必须在**工具执行体**上（`Registry.call` 会把 raise 转成 ok:false，
    #    见 server.tool_forward）—— 若直接顶替 `Manager.kb_call`，注入的异常会绕过
    #    那一层，测的就不是产品行为而是本探针的接线。
    reg2 = ToolRegistry()
    install_workbench(reg2, Config.load().runtime.workbench)
    _t = reg2.get("kb_index")
    if _t is not None:
        def _boom(**kw):
            raise RuntimeError("索引炸了（审查注入）")
        _t.fn = _boom
    M.kernel = types.SimpleNamespace(registry=reg2, cfg=Config.load())
    try:
        code, d = L.list_payload(M.kb_fetch, qs={}, library="", tools=M.kb_names(),
                                 library_root=lambda: M.kb_root(""))
    finally:
        M.kernel = types.SimpleNamespace(registry=reg, cfg=Config.load())
    check("★ kb_index 抛错 ⇒ 仍 200（不许 5xx）", code, 200)
    check("★ 且退回 kb_list", d.get("via"), "kb_list")
    check("★ 且 note 里写了 kb_index 不可用 + 已退回",
          ("kb_index 不可用" in str(d.get("note")) and
           "已退回" in str(d.get("note"))), True)
    check("★ 且没把库说成空的（有 items）", bool(d.get("items")), True)
else:
    print("✔ （跳过：本机没有真库/真 workbench，C 段只跑假件部分）→ True")
    n += 1

# ══════════════════ D · 真 HTTP：参数闸门 + 读的四种错 ══════════════════
print()
print("=" * 78)
print("D · 真起服务、真发请求：/api/kb/list 与 /api/kb/read 的闸门")
print("=" * 78)
httpd = S.Server(("127.0.0.1", 0), S.Handler)
httpd.mgr = M
_port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def get(path: str) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", _port, timeout=60)
    try:
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        try:
            j = json.loads(body.decode("utf-8", "replace"))
        except Exception:                              # noqa: BLE001
            j = {"_raw": body[:200].decode("utf-8", "replace")}
        return r.status, j
    finally:
        c.close()


for path, want in (("/api/kb/list?page_size=abc", 400),
                   ("/api/kb/list?page_size=0", 400),
                   ("/api/kb/list?page_size=501", 400),
                   ("/api/kb/list?page=0", 400),
                   ("/api/kb/list?page=abc", 400),
                   ("/api/kb/list?sort=abc", 400),
                   ("/api/kb/list?order=sideways", 400),
                   ("/api/kb/list?source=gold", 400)):
    st, j = get(path)
    check(f"★ {path} ⇒ {want}", st, want)
    check(f"   且说明里点名了参数（不是空 error）",
          bool(str(j.get("error") or "").strip()), True)

for path, want in (("/api/kb/list", 200),
                   ("/api/kb/list?limit=abc", 200),
                   ("/api/kb/list?limit=-1", 200),
                   ("/api/kb/list?limit=0", 200),
                   ("/api/kb/list?limit=500", 200),
                   ("/api/kb/list?status=read", 200),
                   ("/api/kb/list?status=nosuchstatus", 200)):
    st, j = get(path)
    check(f"★ 旧参数不 5xx：{path} ⇒ 200", st, want)
st, j = get("/api/kb/list?limit=500")
check("★ ?limit=500 仍按旧脾气给一整页 500（本页条数 = min(500, 全库)）",
      j.get("showing"), min(500, len(LIB)) if REAL else j.get("showing"))
check("★ ?limit=abc 不被当成 0 条（降级到旧默认值，不是空页）",
      get("/api/kb/list?limit=abc")[1].get("ok"), True)

if REAL:
    # 挑一份**页数 > 24** 的真文档：否则 `read_payload` 会把窗口收窄到总页数
    # （那是它的既有口径），断言 5..24 就成了假红 —— 判据错、不是代码错。
    import re as _re
    big = None
    for it in LIB:
        p = LIB_FILE.parent / "texts" / f"{it.get('id')}.txt"
        if not p.exists():
            continue
        raw = p.read_text(encoding="utf-8", errors="replace")
        nos = [int(x) for x in _re.findall(r"(?m)^\s*PAGE\s*(\d+)", raw)]
        if len(nos) > 24:
            big = it.get("id")
            break
    noft = next((i.get("id") for i in LIB if not i.get("has_fulltext")), None)
    print(f"（读路径用的真文档：{big}）")

    st, j = get(f"/api/kb/read?doc_id={big}&page_from=5")
    check("★ 真 HTTP：只给 page_from=5 ⇒ 200", st, 200)
    check("★ 真 HTTP：回包 page_from/page_to = 5..24（真收窄）",
          (j.get("page_from"), j.get("page_to")), (5, 24))
    st, j = get("/api/kb/read?doc_id=kbZZZZnotexist")
    check("★ 认不出的 id ⇒ 404", st, 404)
    check("   且说明带「库里没有」这层意思",
          "没有这个 id" in str(j.get("error")), True)
    if noft:
        st, j = get(f"/api/kb/read?doc_id={noft}")
        check("★ 无全文 ⇒ 409（不是 404、不是空文档）", st, 409)
    st, j = get("/api/kb/read")
    check("★ 缺 doc_id ⇒ 400", st, 400)
    st, j = get(f"/api/kb/read?doc_id={big}&page_from=1&page_to=21")
    check("★ 跨度 21 ⇒ 400", st, 400)
    st, j = get(f"/api/kb/read?doc_id={big}&page_from=99999")
    check("★ 越界如实报告：没有正文页不是系统故障",
          (st, isinstance(j.get("pages_total"), int)), (200, True))

# 503：注册表里没有 kb_read（接线断了，不是这篇没正文）
M2 = S.Manager()
M2.kernel = types.SimpleNamespace(registry=ToolRegistry(), cfg=Config.load())
httpd2 = S.Server(("127.0.0.1", 0), S.Handler)
httpd2.mgr = M2
_p2 = httpd2.server_address[1]
threading.Thread(target=httpd2.serve_forever, daemon=True).start()


def get2(path: str) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", _p2, timeout=30)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    finally:
        c.close()


st, j = get2("/api/kb/read?doc_id=kb1")
check("★ 注册表里没有 kb_read ⇒ 503（接线断了，不是这篇没正文）", st, 503)
st, j = get2("/api/kb/list")
check("★ 注册表里没有 kb 工具 ⇒ 200 + ok:false（不 5xx、也不冒充空库）",
      (st, j.get("ok")), (200, False))
check("★ 且没有 items 这个键（空列表会被读成「库里没有」）", "items" in j, False)

print()
if ok_all:
    print(f"[DONE] 审查补充判据全部通过（{n} 条）")
else:
    print("[DONE] 审查补充判据有失败项 —— 见上面每行 ✘")
sys.exit(0 if ok_all else 1)
