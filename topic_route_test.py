# -*- coding: utf-8 -*-
"""导师工作台的 **GET 路由** 契约：`/api/topic/doc` 与 `/api/topic/blockers`。

## 为什么补这一套

`mentor_workspace_test.py` 覆盖的是**台账层**（`topic.read_doc` / `blocker_page`），
`mentor_ui_test.py` 只用字符串匹配确认 `mentor.js` 里写了 `/api/topic/blockers`
—— **没有任何一条断言真的把 HTTP 请求打到那两条路由上**。
于是路由层的 400/404 映射、参数校验、`limit` 上界、以及"GET 不许写盘"
这四类东西全都**没有判据**：把它们改成 200 空壳，全套测试照样绿。

## 纪律

· **不碰真实 `topics/CURRENT`**：用 `RONGKE_DATA` 把 `data_root()` 指到临时目录，
  跑完核对真实台账指纹未变（本条本身也是断言）。
· **不启动内核**：与 `routes_test.py` 同手法屏蔽 `Manager.boot`。
· 每条判据都写"它该抓什么"，并带负控 —— 只测"打得开"的测试测不出任何东西。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def tree_fingerprint(base: Path) -> str:
    h = hashlib.sha256()
    if not base.exists():
        return "absent"
    for p in sorted(base.rglob("*")):
        rel = str(p.relative_to(base))
        if p.is_dir():
            h.update(("D " + rel + "\n").encode("utf-8"))
        else:
            st = p.stat()
            h.update(("F %s %d %d\n" % (rel, st.st_size, int(st.st_mtime))).encode("utf-8"))
            try:
                h.update(p.read_bytes())
            except OSError:
                h.update(b"<unreadable>")
    return h.hexdigest()


REAL_TOPICS = ROOT / "topics"
REAL_BEFORE = tree_fingerprint(REAL_TOPICS)

TMP = Path(tempfile.mkdtemp(prefix="_topicroute_"))
os.environ["RONGKE_DATA"] = str(TMP)

import rongke.web.server as S                            # noqa: E402
from rongke import topic as T                            # noqa: E402

TR = T.topics_root()
# 硬前置：临时根没生效就**立刻停**，绝不带着真实台账往下跑。
if not str(TR).startswith(str(TMP)) or TR.resolve() == REAL_TOPICS.resolve():
    raise SystemExit(f"[FATAL] 临时台账根没生效（{TR}）—— 拒绝在真实台账上跑")

FAILED: list[str] = []


def check(label, got, want):
    good = got == want
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")
        FAILED.append(label)


def truthy(label, got):
    check(label, bool(got), True)


# ── 夹具 ────────────────────────────────────────────────
T.ensure_topic("rt-a", title="路由探针 A", question="要回答什么？", root=TR)
T.write_doc("rt-a", "plan", "P" * 30000, mode="replace", root=TR)   # 3 窗
T.write_doc("rt-a", "reading", "正文。", mode="replace", root=TR)
# evidence **故意不写** ⇒ 正常空文档（不是工具失败）
T.ensure_topic("rt-b", title="路由探针 B", root=TR)
T.write_doc("rt-b", "plan", "B 的正文", mode="replace", root=TR)
for i in range(2):
    T.add_blocker("rt-a", f"开着 {i}", tried=[f"t{i}"], root=TR)
for i in range(2):
    r = T.add_blocker("rt-a", f"关掉 {i}", root=TR)
    T.resolve_blocker("rt-a", r["id"], how=f"办法 {i}", root=TR)

LEDGER_BEFORE = tree_fingerprint(TR)

S.Manager.boot = lambda self: None                       # type: ignore[method-assign]
M = S.Manager()
httpd = S.Server(("127.0.0.1", 0), S.Handler)
httpd.mgr = M                                            # type: ignore[attr-defined]
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = f"http://127.0.0.1:{PORT}"


def get(path):
    try:
        with OPENER.open(BASE + path, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jget(path):
    st, body = get(path)
    try:
        return st, json.loads(body.decode("utf-8", "replace"))
    except Exception:                                    # noqa: BLE001
        return st, {}


try:
    # ── 0. 对照：证明 200 不是"什么都回 200" ──────────────
    print("\n── 0. 对照：未注册路由 / 静态白名单 ──")
    st, _ = get("/api/topic/definitely-not-a-route")
    check("未知 /api/ 路由 → 404", st, 404)
    st, _ = get("/static/not-listed.js")
    check("静态白名单：没列出的资产 → 404", st, 404)
    st, _ = get("/static/../server.py")
    check("静态白名单：穿越名 → 404（路由不许变成磁盘浏览器）", st, 404)
    for a in ("tokens.css", "workspace.css", "reader.css", "layout.css",
              "library.css", "mentor.css", "core.js", "workspace.js",
              "library.js", "mentor.js", "reader.js"):
        st, _ = get("/static/" + a)
        check(f"静态白名单：{a} → 200", st, 200)

    # ── 1. doc：字段契约（mentor.js 逐字读这些键）──────────
    print("\n── 1. /api/topic/doc 的字段契约（与 mentor.js 同一组键）──")
    st, d = jget("/api/topic/doc?slug=rt-a&doc=reading")
    check("正控：→ 200", st, 200)
    check("text 原样回来", d.get("text"), "正文。")
    check("doc 键回显", d.get("doc"), "reading")
    check("file 键回显", d.get("file"), "READING.md")
    check("slug 键回显", d.get("slug"), "rt-a")
    check("offset 回显", d.get("offset"), 0)
    # "正文。" 是 3 个字符（正/文/。），别按字数直觉写 2。
    check("★ total_chars 在（mentor.js 的页码分母）", d.get("total_chars"), 3)
    check("total 与 total_chars 同值（两个名字都得给）",
          d.get("total"), d.get("total_chars"))
    check("★ chars 在（mentor.js 的页码分子）", d.get("chars"), 3)
    check("没截断 ⇒ truncated=False", d.get("truncated"), False)
    check("非空 ⇒ empty=False", d.get("empty"), False)

    # ── 2. doc：字符窗口 ────────────────────────────────
    print("\n── 2. /api/topic/doc 的字符窗口 ──")
    st, d = jget("/api/topic/doc?slug=rt-a&doc=plan")
    check("默认窗 → 200", st, 200)
    check("默认 limit = topic.DOC_WINDOW", d.get("limit"), T.DOC_WINDOW)
    check("首窗字数 = DOC_WINDOW", d.get("chars"), T.DOC_WINDOW)
    check("★ 超过一窗 ⇒ truncated=True（别把『没给全』说成『就这么多』）",
          d.get("truncated"), True)
    st, d = jget("/api/topic/doc?slug=rt-a&doc=plan&offset=24000&limit=12000")
    check("末窗字数", d.get("chars"), 6000)
    check("★ 末窗 truncated=False（前端据此禁用『下一段』）", d.get("truncated"), False)
    st, d = jget("/api/topic/doc?slug=rt-a&doc=plan&offset=99999")
    check("越界 offset → 200（不是 5xx）", st, 200)
    check("★ 越界如实说 out_of_range=True", d.get("out_of_range"), True)
    check("★ 越界不伪装成『文档是空的』：empty=False", d.get("empty"), False)

    # ── 3. doc：正常空文档 ≠ 工具失败 ────────────────────
    print("\n── 3. 正常空文档不是工具失败 ──")
    st, d = jget("/api/topic/doc?slug=rt-a&doc=evidence")
    check("★ 从没写过的文档 → 200", st, 200)
    check("★ ok=True", d.get("ok"), True)
    check("not_found=False（课题在）", d.get("not_found"), False)
    check("reason=no_file", d.get("reason"), "no_file")
    check("empty=True（界面显示『还是空的』）", d.get("empty"), True)

    # ── 4. doc：非法入参 ────────────────────────────────
    print("\n── 4. /api/topic/doc 的拒绝路径 ──")
    for path, why in (
        ("/api/topic/doc?slug=rt-a&doc=../../config.json", "doc 穿越"),
        ("/api/topic/doc?slug=rt-a&doc=secret", "未知 doc"),
        ("/api/topic/doc?slug=rt-a", "缺 doc"),
        ("/api/topic/doc?doc=plan", "缺 slug"),
        ("/api/topic/doc?slug=../../&doc=plan", "slug 穿越"),
        ("/api/topic/doc?slug=" + "a" * 60 + "&doc=plan", "slug 超长"),
    ):
        st, d = jget(path)
        check(f"★ {why} → 400", st, 400)
        truthy(f"{why}：理由是句人话", d.get("error"))
    st, d = jget("/api/topic/doc?slug=no-such-topic&doc=plan")
    check("★ 课题不存在 → 404（不是 200 空壳）", st, 404)

    # ── 5. doc：limit 有界 ──────────────────────────────
    print("\n── 5. /api/topic/doc 的 limit 有界 ──")
    st, _ = jget(f"/api/topic/doc?slug=rt-a&doc=plan&limit={T.DOC_WINDOW_MAX + 1}")
    check(f"★ limit > {T.DOC_WINDOW_MAX} → 400", st, 400)
    st, d = jget(f"/api/topic/doc?slug=rt-a&doc=plan&limit={T.DOC_WINDOW_MAX}")
    check("limit = 上限 → 200", st, 200)
    for bad in ("abc", "0", "-5"):
        st, _ = jget(f"/api/topic/doc?slug=rt-a&doc=plan&limit={bad}")
        check(f"★ limit={bad} → 400（不 5xx、不断连）", st, 400)
    st, _ = jget("/api/topic/doc?slug=rt-a&doc=plan&offset=-1")
    check("offset=-1 → 400", st, 400)

    # ── 6. blockers：状态筛选 ───────────────────────────
    print("\n── 6. /api/topic/blockers 的状态筛选 ──")
    st, d = jget("/api/topic/blockers?slug=rt-a&status=open")
    check("正控：→ 200", st, 200)
    check("★ open 只回未闭环 2 条", d.get("total"), 2)
    check("全是 open", sorted(b["status"] for b in d.get("items", [])),
          ["open", "open"])
    _, d = jget("/api/topic/blockers?slug=rt-a&status=resolved")
    check("★ resolved 只回已闭环 2 条", d.get("total"), 2)
    check("全是 resolved", sorted(b["status"] for b in d.get("items", [])),
          ["resolved", "resolved"])
    _, d = jget("/api/topic/blockers?slug=rt-a&status=all")
    check("★ all 回全部 4 条", d.get("total"), 4)

    print("\n── 6b. open_total 与筛选无关（界面红色角标）──")
    _, o = jget("/api/topic/blockers?slug=rt-a&status=open")
    _, r = jget("/api/topic/blockers?slug=rt-a&status=resolved")
    _, a = jget("/api/topic/blockers?slug=rt-a&status=all")
    check("open 视图 open_total", o.get("open_total"), 2)
    check("★ resolved 视图 open_total 不跟着变", r.get("open_total"), 2)
    check("all 视图 open_total", a.get("open_total"), 2)

    print("\n── 6c. 折叠与字段（append-only + 按 id 折叠）──")
    ids = [b["id"] for b in a.get("items", [])]
    check("★ 同一 id 不出现两次", len(ids), len(set(ids)))
    check("★ 新→旧", ids, sorted(ids, reverse=True))
    sample = a["items"][0] if a.get("items") else {}
    for k in ("id", "status", "opened_at", "what", "tried", "resolved_how"):
        truthy(f"★ 字段 {k} 在（mentor.js 读它）", k in sample)

    # ── 7. blockers：越界与拒绝路径 ─────────────────────
    print("\n── 7. /api/topic/blockers 的越界与拒绝 ──")
    st, d = jget("/api/topic/blockers?slug=rt-a&status=open&offset=99")
    check("越界 offset → 200", st, 200)
    check("★ 越界：items 空但 total 仍说 2（不伪装成『没有卡点』）",
          (d.get("items"), d.get("total")), ([], 2))
    check("★ 越界：open_total 仍是全量事实", d.get("open_total"), 2)
    st, d = jget("/api/topic/blockers?slug=rt-a&status=bogus")
    check("★ 非法 status → 400（不许静默降级）", st, 400)
    st, _ = jget("/api/topic/blockers?slug=rt-a&status=open&limit=201")
    check("★ limit > 200 → 400（有界）", st, 400)
    st, _ = jget("/api/topic/blockers?slug=rt-a&status=open&limit=abc")
    check("limit=abc → 400（不 5xx）", st, 400)
    st, _ = jget("/api/topic/blockers?status=open")
    check("缺 slug → 400", st, 400)
    st, _ = jget("/api/topic/blockers?slug=../../&status=open")
    check("★ slug 穿越 → 400", st, 400)

    # ── 7b. 已知缺陷：两条路由对「课题不存在」给了不同的码 ────────
    # ★ 这一条**故意留着红**，它不是本套件写错，而是产品代码的口径不一致。
    #   两条路由共用同一段收尾（`server.py` 那次 `if not body.get("ok")`
    #   → `404 if body.get("not_found") else 400`），而 doc 分支由台账层
    #   带回 `not_found=True`（→404），blockers 分支在这里手搓的
    #   `{"ok": False, "error": …}` **没有 `not_found` 键** ⇒ 落到 400。
    #   前端 `api()` 把非 2xx 一律折成 `e.message`，于是同一件事
    #   （"这个课题没了"）在「计划」页是"读取失败与404"、在「卡点」页是
    #   "读取失败与400" —— 两个页面给出两种说法，正是本仓反复记的 H 型。
    #   修法见评审报告的 Suggestion；修好后本条自动转绿。
    print("\n── 7b. 已知缺陷：『课题不存在』的码在两条路由上不一致 ──")
    _sd, _db = jget("/api/topic/doc?slug=ghost-topic&doc=plan")
    _sb, _bb = jget("/api/topic/blockers?slug=ghost-topic&status=open")
    print(f"         doc      → {_sd}  {json.dumps(_db, ensure_ascii=False)[:90]}")
    print(f"         blockers → {_sb}  {json.dumps(_bb, ensure_ascii=False)[:90]}")
    check("★ [已知缺陷] 两条路由对同一个『课题不存在』必须同一个码",
          _sb, _sd)
    check("★ [已知缺陷] blockers 的路由也必须回 404", _sb, 404)

    # ── 8. 不串台账 ─────────────────────────────────────
    print("\n── 8. 两个课题互不串味儿 ──")
    st, d = jget("/api/topic/doc?slug=rt-b&doc=plan")
    check("rt-b 的正文", d.get("text"), "B 的正文")
    st, d = jget("/api/topic/doc?slug=rt-a&doc=plan")
    truthy("★ rt-a 的窗里没有 B 的正文", "B 的正文" not in str(d.get("text")))

    # ── 8b. 承重自检：把机制拆掉，上面的断言必须当场失守 ──
    # ★ 这一步是这一套件唯一有意义的部分。只断言"现在回 200"，改坏了也回 200
    #   就一条都抓不住（G 型：判据不承重 = 装饰品）。所以这里**在进程内**
    #   把台账层的实现换成缺陷版（**磁盘上的实现一个字都不动**），
    #   再打同一个请求，确认结果**确实变了** —— 变了才说明判据在承重。
    print("\n── 8b. 承重自检：拆掉机制，前面的断言必须失守 ──")

    # 8b-① 让 read_doc 变成"成功但永远空" —— 「正常空 ≠ 工具失败」那条的反面
    _orig_read = T.read_doc

    def _always_empty(slug, doc, offset=0, limit=T.DOC_WINDOW, root=None):
        return {"ok": True, "not_found": False, "reason": "", "slug": slug,
                "doc": doc, "file": "X", "offset": 0, "limit": limit,
                "text": "", "chars": 0, "total": 0, "total_chars": 0,
                "truncated": False, "empty": True}

    T.read_doc = _always_empty                       # type: ignore[assignment]
    try:
        st, d = jget("/api/topic/doc?slug=rt-a&doc=reading")
        check("★ 8b-① 换成『永远空』之后，正文断言确实失守（证明它测的是真读）",
              d.get("text") == "正文。", False)
        st, _ = jget("/api/topic/doc?slug=no-such-topic&doc=plan")
        check("★ 8b-① 且『课题不存在→404』也失守（变成 200 空壳）", st, 200)
    finally:
        T.read_doc = _orig_read                      # type: ignore[assignment]
    st, d = jget("/api/topic/doc?slug=rt-a&doc=reading")
    check("★ 8b-① read_doc 已还原（正文回来了）", d.get("text"), "正文。")

    # 8b-② 让 blocker_page 忽略 status —— 状态筛选那三条必须失守
    _orig_page = T.blocker_page

    def _no_filter(slug, status="", offset=0, limit=T.BLOCKER_PAGE, root=None):
        allb = T.blockers(slug, root=root)
        off, lim = max(0, int(offset or 0)), max(1, int(limit or T.BLOCKER_PAGE))
        return {"ok": True, "slug": slug, "status": "all",
                "items": allb[off:off + lim], "blockers": allb[off:off + lim],
                "total": len(allb), "offset": off, "limit": lim,
                "truncated": off + lim < len(allb),
                "open_total": sum(1 for b in allb if b["status"] == "open")}

    T.blocker_page = _no_filter                      # type: ignore[assignment]
    try:
        st, d = jget("/api/topic/blockers?slug=rt-a&status=open")
        # ★ 断言写成「好值 == 2 是否仍成立」，期望 **False** —— 失守才算通过。
        check("★ 8b-② 不过滤之后，open 视图的 total 确实失守（好值 2 不再成立）",
              d.get("total") == 2, False)
        st, d = jget("/api/topic/blockers?slug=rt-a&status=resolved")
        check("★ 8b-② resolved 视图也失守（拿到的是全部 4 条）",
              sorted(b["status"] for b in d.get("items", [])) == ["resolved", "resolved"],
              False)
    finally:
        T.blocker_page = _orig_page                  # type: ignore[assignment]
    st, d = jget("/api/topic/blockers?slug=rt-a&status=open")
    check("★ 8b-② blocker_page 已还原（open 回到 2 条）", d.get("total"), 2)

    # 8b-③ 让 read_doc 忽略 offset —— 越界/窗口那几条必须失守
    def _no_window(slug, doc, offset=0, limit=T.DOC_WINDOW, root=None):
        return _orig_read(slug, doc, offset=0, limit=limit, root=root)

    T.read_doc = _no_window                          # type: ignore[assignment]
    try:
        st, d = jget("/api/topic/doc?slug=rt-a&doc=plan&offset=99999")
        check("★ 8b-③ 忽略 offset 之后，out_of_range 不再为真（越界断言失守）",
              d.get("out_of_range") is True, False)
        st, d = jget("/api/topic/doc?slug=rt-a&doc=plan&offset=24000&limit=12000")
        check("★ 8b-③ 末窗字数也失守（拿到首窗 12000，不是 6000）",
              d.get("chars") == 6000, False)
    finally:
        T.read_doc = _orig_read                      # type: ignore[assignment]
    st, d = jget("/api/topic/doc?slug=rt-a&doc=plan&offset=24000&limit=12000")
    check("★ 8b-③ read_doc 已还原（末窗回到 6000）", d.get("chars"), 6000)

    # 8b-④ 让 DOC_WINDOW_MAX 变成很大 —— limit 有界那条必须失守
    _orig_max = T.DOC_WINDOW_MAX
    T.DOC_WINDOW_MAX = 10 ** 9                       # type: ignore[assignment]
    try:
        st, _ = jget("/api/topic/doc?slug=rt-a&doc=plan&limit=50000")
        check("★ 8b-④ 上限被放大后，超限请求不再 400（有界断言失守）",
              st == 400, False)
    finally:
        T.DOC_WINDOW_MAX = _orig_max                 # type: ignore[assignment]
    st, _ = jget("/api/topic/doc?slug=rt-a&doc=plan&limit=50000")
    check("★ 8b-④ DOC_WINDOW_MAX 已还原（超限重新 400）", st, 400)

    # ── 9. GET 只读 ─────────────────────────────────────
    print("\n── 9. GET 只读：整轮请求之后台账一字未变 ──")
    check("★ 临时台账指纹未变（GET 不写盘）", tree_fingerprint(TR), LEDGER_BEFORE)
finally:
    httpd.shutdown()
    httpd.server_close()

print("\n── 10. 真实 topics/ 未被本测试碰过 ──")
check("★ 真实 topics/ 指纹与开工前一致", tree_fingerprint(REAL_TOPICS), REAL_BEFORE)
shutil.rmtree(TMP, ignore_errors=True)

print("\n═══ 汇总 ═══")
if FAILED:
    print(f"  ✘ {len(FAILED)} 项失败")
    for x in FAILED:
        print(f"    - {x}")
    sys.exit(1)
print("  全部通过 ✔")
sys.exit(0)
