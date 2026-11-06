# -*- coding: utf-8 -*-
"""知识库**链路层**端到端：上传 PDF → 解析入库 → 检索 → 面板读得到（2026-09-22）。

★ 为什么单开一套，而不是并进 `runs/_regress/kb_wire_probe.py`：

  · `kb_wire_probe.py` 量的是**工具层** —— 反射进来没有、围栏登记没有、
    真调一次回包对不对。进程内直调，不需要控制台。
  · 本套量的是**链路层** —— HTTP 路由 → `Manager.kb_upload` → 工具注册表 →
    litkb → 再回到面板读的那几个端点。

  两者**不许互相替代**，与 `deck_test.py` / `deck_live_test.py` 同一分工：
  一个管判据，一个管产出路径。**工具层全绿不代表链路上接对了** ——
  本项目有过"单元里对、链路上错"（119 字事件）。面板更是重灾区：
  它的数据全来自注册表，只要有一环没接上，界面会以"0 篇文献"的样子**假绿**。

★ 本套自己起控制台（端口 0，**不启真内核**），不依赖你在外面开着 8791：
  依赖外部状态的红，看起来和"代码坏了"一模一样，没人分得清。

承重的四条（每条都配了能让它当场失守的负控）：
  1. **`library` 只有一个语义**。曾经：存 PDF 按"库名"、切引擎按"路径" ⇒
     同一个值在两处被解释成两样东西（H 型），两个"库"各写各的。
  2. **重传同一篇不许在库里长出第二份**。荣珂"同名不覆盖"会把它改名成 `-2`，
     而 `kb_ingest` 的路径去重键**只认原路径** ⇒ 不补内容比对这一步，
     工具层精心加的去重就被这一手悄悄绕过。
  3. **同名但内容不同 ⇒ 改名，绝不覆盖**。库里每条都记着自己的 `local_path`，
     覆盖 = 静默换掉一篇文献，还不报错。
  4. **「库是空的」/「接线断了」/「没命中」要说成三句不同的话** ——
     都渲染成"0 篇"，用户会去重新导入一个已经建好的库。

跑：python312 kb_e2e_test.py     退出码 0 全过 / 1 有失败
"""
from __future__ import annotations

import json
import sys
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具一律走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费。
import _tkit as T                          # noqa: E402
import rongke.web.server as S              # noqa: E402
from rongke.bridge import install_workbench  # noqa: E402
from rongke.config import Config           # noqa: E402
from rongke.tools import ToolRegistry      # noqa: E402

ok_all = True
warns: list[str] = []


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def truthy(label, got):
    check(label, bool(got), True)


def dump(label, obj, keys=None):
    """把关键字段摊开打一行。

    ★ 为什么断言之外还要这个：一条只说"期望 1、实际 0"的失败**没法查** ——
      上一次真出事时（新库没建表），回包里明明写着 `failed=1` +
      `no such table: fts_doc`，可我的断言一个字节都没往外带，
      于是还得回头去手工重跑一遍才知道根因。
      **失败信息不够用，等于这条判据白写。**
    """
    if keys:
        obj = {k: obj.get(k) for k in keys if k in obj}
    print(f"  [FYI ] {label} = {json.dumps(obj, ensure_ascii=False)[:700]}")


# ══════════════════════ 夹具 ══════════════════════
WS = S.ws_dir()
LIB = "_kb_e2e_probe"                  # **库名**（不是路径）—— 本套测的就是这条契约
LIB_DIR = WS / "_kb" / LIB
FIX = ROOT / "runs" / "_regress" / "_kb_e2e_fixture.pdf"

# 检索用的种子词：**只在夹具里出现**，于是"命中"只可能来自这份夹具。
SEED_P1 = "Harmonic resonance of mycelial protoplast fusion in Hirsutella sinensis"
SEED_P2 = "BRIGHTQUARTZ marker appears only on the second page."
MISS_WORD = "zzqqxx_no_such_word_in_any_library_zzqqxx"

# ⚠️ 刻意**不启动真内核**：`Rongke()` 默认 `install_mcp_servers=True`，
#    一构造就会 spawn 外部子进程。本套要验的是知识库这条链，不是内核装配 ——
#    所以换掉 boot，再把**真注册表**手工装进去。
#    ★ 但注册表必须是**真的**（`install_workbench` 装的那份，与内核用的同一个函数），
#      否则测的就只是"我造的假注册表能跑"，与 agent 实际拿到的没关系。
S.Manager.boot = lambda self: None          # type: ignore[method-assign]

WB = Config.load().runtime.workbench


def make_fixture(p: Path, vary: bool = False) -> bytes:
    """造一份**自给自足**的 PDF，返回它的字节。`vary=True` 造"修订版"。

    刻意**不用用户盘上任何现成 PDF**：测试去依赖用户的文件 ⇒ 他一挪，测试就红，
    而红的样子与"代码坏了"一模一样（这就是"读不到 ≠ 不符合"那条纪律的日常版）。
    `fitz` 是 litkb 抽文本的硬依赖 —— 被测链路上**本来就必须有它**，
    所以用它造夹具不引入任何新依赖，也不多一个会过期的假设。

    ⚠️ 造"内容不同"的那份**不能**拿原始字节去 `replace` 那个种子词：
      `doc.save()` 默认压流，词在**解压后**才存在，原始字节里根本没有它 ——
      于是 replace 静默不生效，两份字节其实一模一样，
      而断言会以"改名没生效"的样子红，指向一个根本不存在的问题。
      （我第一版就这么错的。）**要造差异，就老老实实再生成一份。**
    """
    import fitz
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 120), SEED_P1, fontsize=13)
    p1.insert_text((72, 150), "Second line for paragraph splitting checks.", fontsize=13)
    p2 = doc.new_page()
    p2.insert_text((72, 120), SEED_P2, fontsize=13)
    if vary:
        p2.insert_text((72, 180), "A revised paragraph only the newer file has.",
                       fontsize=13)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(p))
    doc.close()
    return p.read_bytes()


# 开工前清场：把上一轮可能留下的探针库挪走 ⇒ 本轮一定是**空库**起步。
# 不这么做的话，"新库 total=0" 那条会因为上一轮的残留而恒真/恒假，都验不出东西。
T.stash(LIB_DIR, "kb-e2e-probe-prev")

DATA = make_fixture(FIX)
# 同名的**修订版**：老老实实再生成一份，别去 replace 原始字节（见 `make_fixture`）。
DATA2 = make_fixture(FIX, vary=True)
if DATA2 == DATA:                     # 夹具自身负控：两份必须真的不同
    warns.append("两份夹具字节相同 ⇒ 「同名不同文」那一节本轮验不出东西")

# ══════════════════════ 装真注册表 ══════════════════════
M = S.Manager()
reg = ToolRegistry()
installed = install_workbench(reg, WB)
M.kernel = types.SimpleNamespace(registry=reg, cfg=Config.load())
KB_NAMES = (M.KB_READ_TOOLS + M.KB_MORE_TOOLS + M.KB_WRITE_TOOLS)


def build_server():
    httpd = S.Server(("127.0.0.1", 0), S.Handler)
    httpd.mgr = M                                # type: ignore[attr-defined]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return httpd, th, f"http://127.0.0.1:{httpd.server_address[1]}"


# ⚠️ 本机设着 HTTP_PROXY，它会连 127.0.0.1 一起拦（吃过 502）——
#    显式清空代理，而不是去信环境变量。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(url: str, method: str = "GET", body: bytes | None = None,
         headers: dict | None = None):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    try:
        with _opener.open(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jbody(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:                            # noqa: BLE001
        return {"_unparsable": raw[:300].decode("utf-8", "replace")}


print("=" * 78)
print("A · 契约层：`library` 只有一个语义（库名 → 一条路径 → 两处共用）")
print("=" * 78)
# ★ 承重判据：空库名必须回空字符串。litkb 把空解释成"切回默认库"，
#   也就是用户真正在用的那个；这里若顺手 resolve() 一下，就会把默认库
#   换成一个刚建出来的空目录 —— 用户已有的文献**一篇都看不见，且不报错**。
check("空库名 ⇒ 空字符串（绝不凭空 resolve 成新目录）", M.kb_root(""), "")
root_lib = M.kb_root(LIB)
check("库名 ⇒ 落在 runs/_kb/<库名> 下",
      Path(root_lib).parent.name, "_kb")
check("库名 ⇒ 末段就是库名本身", Path(root_lib).name, LIB)
check("★ 一个库名 → 同一条路径（存 PDF 与切引擎不再各说各话）",
      Path(M.kb_dir(LIB)), Path(root_lib))
# 负控：库名来自 query 字符串，是可写路径的输入 ⇒ 必须挡住穿越。
# 当前实现是**直接拒绝**（400），而不是取 basename 后继续 —— 后者会把
# 一个明显的攻击输入静默解释成合法库名，用户看不出自己发错了。
try:
    M.kb_root("../../evil")
    bad("★ 负控：`../../evil` 被接受了 —— 库名被当成路径")
except Exception as e:                                # noqa: BLE001
    check("★ 负控：`../../evil` 被拒绝（库名不能是路径）",
          type(e).__name__, "RequestError")
# 正控配负控：确认上面两条不是因为"函数直接返回空"而恒真。
truthy("正控：正常库名拿到的是非空路径", bool(root_lib))

print()
print("=" * 78)
print("B · 端点接线：真起服务、真发请求（面板读的就是这几个端点）")
print("=" * 78)
print(f"  注册表里的 kb_* = {len([n for n in reg._tools if n.startswith('kb_')])} 个"
      f"（install_workbench 装了 {len(installed)} 个工具）")
httpd, th, BASE = build_server()
q_lib = urllib.parse.urlencode({"library": LIB})


def get_json(path: str):
    st, raw = http(BASE + path)
    return st, jbody(raw)


def post_pdf(name: str, data: bytes, lib: str = LIB, no_enrich: bool = True):
    q = urllib.parse.urlencode({"name": name, "library": lib,
                                "no_enrich": "1" if no_enrich else "0"})
    st, raw = http(BASE + f"/api/kb/upload?{q}", "POST", data,
                   {"Content-Type": "application/pdf"})
    return st, jbody(raw)

try:
    st, stat0 = get_json(f"/api/kb/stat?{q_lib}")
    check("GET /api/kb/stat → 200", st, 200)
    check("stat.ok", stat0.get("ok"), True)
    check(f"★ {len(KB_NAMES)} 个 kb_* 工具一个不缺（缺哪个就写进 tools_missing）",
          stat0.get("tools_missing"), [])
    truthy("stat 报了 AI 凭证的**事实**（configured/missing/unknown，不猜）",
           stat0.get("ai", {}).get("state") in ("configured", "missing", "unknown"))
    check("★ 新库 ⇒ total=0（「库是空的」这句话说得出来）", stat0.get("total"), 0)

    # ── 空查询：必须 400，不许回空列表 ──
    # 空列表会被读成"库里没有这段内容"，而事实是"你什么都没问"。
    st, body = get_json(f"/api/kb/search?{q_lib}")
    check("★ 负控：空 q ⇒ 400（不是 200 + 空列表）", st, 400)

    # ── 非 PDF 拒掉 ──
    st, body = post_pdf("nota.pdf", b"this is plain text, not a pdf")
    check("★ 拒收伪装成 .pdf 的非 PDF（抽不出文本 ⇒ ok=False）",
          (st == 400) or (body.get("ok") is False), True)

    print()
    print("  ── 入库第一次：应当真的进库 ──")
    st, up1 = post_pdf("_kb_e2e_fixture.pdf", DATA)
    dump("第一次上传回包", up1, ("ok", "candidates", "added", "skipped", "failed",
                                "failures", "unmatched", "in_library", "error"))
    check("POST /api/kb/upload → 200", st, 200)
    check("第一次上传：added=1", up1.get("added"), 1)
    check("第一次上传：failed=0", up1.get("failed"), 0)
    check("第一次上传：库里条目数", up1.get("in_library"), 0)   # 入库**前**的库量
    one = (up1.get("items") or [{}])[0]
    probe_id = one.get("id")
    truthy("入库条目拿到了 id", probe_id)
    check("★ 解析出了全文（不是只登记了文件名）", one.get("fulltext"), True)
    check("★ 抽出 2 页", one.get("pages"), 2)

    st, stat1 = get_json(f"/api/kb/stat?{q_lib}")
    check("入库后 stat：matched=1", stat1.get("matched"), 1)
    check("入库后 stat：有全文的 1 篇", stat1.get("fulltext"), 1)

    # ── 段落级检索：连页码一起验 ──
    st, hit2 = get_json(f"/api/kb/search?{q_lib}&q=BRIGHTQUARTZ")
    dump("检索回包", hit2, ("ok", "n", "error", "hint", "raw"))
    check("检索 → 200", st, 200)
    check("检索 ok", hit2.get("ok"), True)
    check("★ 命中数 > 0（种子词只在第 2 页）", (hit2.get("n") or 0) > 0, True)
    if hit2.get("hits"):
        h = hit2["hits"][0]
        check("★ 命中页码 = 2（段落级定位真通了）", h.get("page"), 2)
        check("命中指向刚入库那篇", str(h.get("doc_id")), str(probe_id))
    else:
        check("★ 命中页码 = 2（段落级定位真通了）", None, 2)

    # ── 「没命中」≠「出错」：两句不同的话 ──
    st, miss = get_json(f"/api/kb/search?{q_lib}&q={MISS_WORD}")
    check("★ 负控：查一个绝不存在的词 ⇒ ok=True（不是错误）", miss.get("ok"), True)
    check("★ 负控：且命中数为 0（「没命中」与「接线断了」分开说）",
          miss.get("n"), 0)

    print()
    print("  ── 入库第二次：同一篇重传，**不许**在库里长出第二份 ──")
    st, up2 = post_pdf("_kb_e2e_fixture.pdf", DATA)
    check("重传 → 200", st, 200)
    check("★ 重传：added=0（工具层去重拦住了）", up2.get("added"), 0)
    check("★ 重传：skipped=1（如实说「跳过」，不是静默）", up2.get("skipped"), 1)
    check("★ 重传：认出是同一份文件（same_file）", up2.get("same_file"), True)
    st, stat2 = get_json(f"/api/kb/stat?{q_lib}")
    # ★ 只断言 added 字段是不够的 —— 要量**最终行为**：库量有没有变。
    #   本项目的老教训：判据要量最终行为，别量中间量。
    check("★★ 最终行为：库里仍然只有 1 篇", stat2.get("matched"), 1)

    print()
    print("  ── 入库第三次：同名但内容不同 ⇒ 改名，绝不覆盖，并把话说全 ──")
    st, up3 = post_pdf("_kb_e2e_fixture.pdf", DATA2)
    dump("同名不同文回包", up3, ("ok", "added", "skipped", "failed", "failures",
                                 "stored_name", "same_file", "same_name_new_content",
                                 "skipped_by", "renamed_note", "note"))
    check("同名不同文 → 200", st, 200)
    check("★ 没有误判成同一份", up3.get("same_file"), None)
    check("★ 存成了新名字（没有覆盖原来那一份）",
          str(up3.get("stored_name")) != "_kb_e2e_fixture.pdf", True)
    truthy("★ 回包里说清了改名这件事", up3.get("renamed_note"))
    # ★ 承重：added 是多少都行，但**回包的话必须与实际发生的那件对得上**。
    #   第一版这里我按假设写死了措辞（"要让新版进库请改名再传"），
    #   而实际行为是"新版已经单独入库了" ⇒ 回包在撒谎，测试当场逮住。
    #   所以判据不写成"必须是 A"或"必须是 B"，而写成**一致性**：
    #   话与结果不许互相矛盾。
    check("★ 标出了「同名新内容」这个状态", up3.get("same_name_new_content"), True)
    n3 = int(up3.get("added") or 0)
    note3 = str(up3.get("note") or "")
    if n3:
        truthy("★★ 话与结果一致：说了「新版已单独入库」", "单独入库" in note3)
    else:
        truthy("★★ 话与结果一致：说了「索引里仍是旧版」", "旧版" in note3)
    truthy("★★ 并提醒了「库里现在同时有两条、要自己核对」",
           ("同时有" in note3) or ("旧版" in note3))
    st, stat3 = get_json(f"/api/kb/stat?{q_lib}")
    check("★ 最终行为：库里条目数 = 1 + added", stat3.get("matched"), 1 + n3)
    if n3:
        st, l3 = get_json(f"/api/kb/list?{q_lib}&limit=500")
        as_of = {it.get("id") for it in (l3.get("items") or [])}
        check("★ 最终行为：两条是**不同**的条目（不是同一 id 被骗两次）",
              len(as_of) >= 2, True)
    # 量**最终行为**，别只看字段：两个文件都要在盘上，且内容各自是各自那份。
    old_f = LIB_DIR / "_kb_e2e_fixture.pdf"
    new_f = LIB_DIR / str(up3.get("stored_name"))
    truthy("★ 原件仍在盘上（改名而不是替换）", old_f.exists())
    check("★ 新文件也在盘上", new_f.exists(), True)
    check("★ 原件内容**没有被换掉**", old_f.read_bytes(), DATA)
    check("★ 新文件确实是新内容", new_f.read_bytes(), DATA2)

    print()
    print("  ── 库隔离：临时库的东西**不许**出现在默认库里 ──")
    # 这一对是"两个方向都量"：只看负控会恒真（比如 list 恒回空列表），
    # 只看正控又验不出隔离。两条一起才有信息量。
    st, l_probe = get_json(f"/api/kb/list?{q_lib}&limit=500")
    ids_probe = [it.get("id") for it in (l_probe.get("items") or [])]
    check("★ 正控：带 library 的列表里**有**探针那篇", probe_id in ids_probe, True)
    st, l_def = get_json("/api/kb/list?limit=500")
    ids_def = [it.get("id") for it in (l_def.get("items") or [])]
    check("★ 负控：默认库的列表里**没有**它（库真的分开了）",
          probe_id in ids_def, False)
    print(f"  默认库 {len(ids_def)} 篇 / 探针库 {len(ids_probe)} 篇")

    st, stat_def = get_json("/api/kb/stat")
    check("默认库仍可读（切库没有把它弄丢）", stat_def.get("ok"), True)
finally:
    httpd.shutdown()
    httpd.server_close()
    th.join(timeout=5)

# ══════════════════════ 收尾 ══════════════════════
# 探针库与夹具都挪走（**不删**）。留着的话下一轮的"空库起步"就要靠运气。
T.stash(LIB_DIR, "kb-e2e-probe")
T.stash(FIX, "kb-e2e-fixture")

print()
if warns:
    for w in warns:
        print(f"  [WARN] {w}")
if ok_all:
    print("[DONE] 知识库端到端全部通过")
else:
    print("[DONE] 知识库端到端有失败项 —— 上面每行 FAIL 都带了期望与实际")
sys.exit(0 if ok_all else 1)
