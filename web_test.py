# -*- coding: utf-8 -*-
"""Web 控制台服务端（rongke/web/server.py）单测。

## 为什么单开这一套

`ui_check.py` 只管 **index.html 自己** 的一致性（语法 / id / CSS 变量 / 路由），
它一个字都不看服务端。而本轮新增的「产出物」接口有两个**只能在这里测**的风险：

  1. **围栏**：`/api/artifact` 是把磁盘上的文件发回去的接口。它必须有且只有一条通路
     （"能被列出来的才能被取回"），否则 `../` 就是一把任意文件读取的钥匙。
     这条**必须用真 HTTP 请求测** —— 直接调 Manager 测不到路由是否真的接上了。
  2. **列不出来 ≠ 没有**：`artifacts()` 对**没有归档的旧任务**也要照常回答
     （`exists:false` / 空列表），而不是报错。定律：看得见优先于能回放；
     而"什么都没有"本身就是要让人看见的事实。

## 两条实测出来的环境事实（本套件的夹具就是围绕它们设计的）

  * **`Path.symlink_to()` 在本机会静默降级。** 对文件目标调它**不抛异常**，
    但结果是一个 0 字节的**普通文件**，`is_symlink()` 返回 `False`
    （实测：`st_mode=0o100666`、`st_reparse_tag=0`、`readlink` 报 WinError 4390）。
    ⇒ 拿"软链"当夹具，测的其实是"一个空文件会不会被列出"（G 型：负控是别的原因在过）。
  * **真正的逃逸向量是目录联接（junction）**：`mklink /J` 建出来的目录
    `is_symlink()` 同样是 `False`、`is_dir()` 是 `True`，而 `rglob` **会走进去**。
    所以夹具用 junction，不用 symlink。

一条刻意的记法：判据落在**可核对的量**上（文件在不在列表里、HTTP 状态码、字节是否相等），
不落在"措辞像不像"。理由同 `tools_test.py` 开头那段。

用法：python web_test.py      退出码 0 全过 / 1 有失败
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具一律走 `_tkit`（挪，不删）—— 理由见它开头那段：本环境按**条目数**
# 收删除费，而 `rmtree` 一个目录按里面所有条目计价。这里不再 import shutil。
import _tkit as T                          # noqa: E402
import rongke.web.server as S              # noqa: E402
import rongke.memory as M_                    # noqa: E402  定向记忆根目录用

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


# ══════════════════════ 夹具 ══════════════════════
WS = S.ws_dir()
RUN = WS / "_webtest"
OUTSIDE = ROOT / "_webtest_outside"
# 影子副本（"注入过缺陷的 server.py"）的落点。**挪去，不删**：
# 它含故意缺陷、又能被 import，躺在产品包里是隐患而不是垃圾问题。
# 用固定名 ⇒ 每轮覆盖，不堆积。定义必须**早于**任何用到它的函数。
_SCRATCH = ROOT / "runs" / "_regress"

# ⚠️ 刻意**不启动真内核**：`Rongke()` 默认 `install_mcp_servers=True`，
#    一构造就会 spawn Origin 子进程（背后还挂 COM 实例）。本套件要测的是
#    产出物接口，它一行都不碰内核 —— 所以把 boot 换掉，而不是让测试去起真装置。
S.Manager.boot = lambda self: None          # type: ignore[method-assign]


def make_junction(link: Path, target: Path) -> bool:
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True, errors="replace")
    return r.returncode == 0 and link.exists()


def cleanup() -> None:
    """拆夹具。**一个删除都不做** —— 理由见 `_tkit` 开头：本环境按**条目数**收删除费。

    ★ 老写法是 `os.rmdir(联接)` → `rmtree(RUN)` → `rmtree(OUTSIDE)`，注释警告
      「顺序反了 `rmtree` 会跟进联接、删掉目标目录里的东西」。
      ⚠️ **那句警告本身是错的**（2026-09-19 实测推翻，见 `_tkit.py --verify-purge`）：
      Python 3.12 在 Windows 上**不**跟进 junction —— `os.walk` 把 junction 排除出
      dirnames（`os.py:388`），顶层还有 `_rmtree_islink()`（`shutil.py:606`）。
      我当时是拿"`entry.is_dir(follow_symlinks=False)` 对 junction 为 True"这**半截
      实测**往外推的，漏了那两道闸 ⇒ **判据要量到"最终行为"，别量中间量。**
      （同族教训：`rmtree/unlink` 关键字扫不出 `TemporaryDirectory` 的 `__exit__`。）

      但"顺序"这条**改成挪**依然是对的，理由换成了**真的那一个**：整棵树按**条目数**
      计费（实测 29 点/次，一轮才 50），而 `os.replace` 是改名、零计费。
      ⇒ 顺手也就不用再关心任何"谁会跟进链接"的版本差异了。

    ★ `OUTSIDE` 刻意**不再清掉**：留着它，第 6 节才能验
      「挪走联接有没有波及目标目录」（目标还在，才算没波及）。以前它被
      `rmtree` 清掉，那条断言于是**恒真**，验不出任何东西。
    """
    T.stash(RUN, "webtest-run")


def build_fixture() -> None:
    cleanup()
    OUTSIDE.mkdir(parents=True, exist_ok=True)
    (RUN / "sub").mkdir(parents=True, exist_ok=True)
    (RUN / ".hidden").mkdir(parents=True, exist_ok=True)
    (RUN / "a" / "b" / "c" / "d" / "e").mkdir(parents=True, exist_ok=True)

    (RUN / "draft.md").write_text("# 稿子\n\n正文一段。\n", encoding="utf-8")
    (RUN / "sub" / "table.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (RUN / "fig.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    # 内核自己的账本：**不是产出物**，一个都不许出现在列表里
    (RUN / "session.json").write_text('{"id":"_webtest"}', encoding="utf-8")
    (RUN / "trace.jsonl").write_text('{"span":1}\n', encoding="utf-8")
    # 对话快照（跨进程续跑的锚点）也是内核账本 —— 2026-09-19 新增。
    # 它最容易被误当成"用户交付的文件"：名字像文档、内容像正文，
    # 所以必须一起进"不该出现在产出物清单里"的名单。
    (RUN / "conversation.json").write_text(
        '{"goal":"夹具","step":3,"messages":[{"role":"user","content":"hi"}]}',
        encoding="utf-8")
    # 隐藏目录与超深目录：同样是噪声
    (RUN / ".hidden" / "x.md").write_text("hidden", encoding="utf-8")
    (RUN / "a" / "b" / "c" / "d" / "e" / "deep.md").write_text("deep", encoding="utf-8")
    # 通向工作区外的目录联接：它必须**既不出现、也取不回**
    (OUTSIDE / "secret.md").write_text("TOP SECRET", encoding="utf-8")
    if not make_junction(RUN / "junc", OUTSIDE):
        warns.append("建不出目录联接 —— 逃逸那一族本轮未测（不是通过，是没测）")


build_fixture()
M = S.Manager()


def paths_of(rid: str) -> list[str]:
    return sorted(f["path"] for f in M.artifacts(rid)["files"])


# ══════════════════════ 1. 列表 ══════════════════════
print("── 1. 产出物列表：该有的都在、不该有的一个都不许在 ──")
lst = M.artifacts("_webtest")
check("exists", lst["exists"], True)
got = paths_of("_webtest")
check("★ 正常产出物被列出", got, ["draft.md", "fig.png", "sub/table.csv"])
check("★ 内核账本 session.json 不出现在列表里",
      any(p.endswith("session.json") for p in got), False)
check("★ trace.jsonl 不出现在列表里",
      any(p.endswith("trace.jsonl") for p in got), False)
check("★ conversation.json（对话快照）也不出现在列表里",
      any(p.endswith("conversation.json") for p in got), False)
check("★ 隐藏目录下的文件不列出", any(p.startswith(".") for p in got), False)
check("★ 超过 MAX_DEPTH 的深文件不列出", any("deep.md" in p for p in got), False)
# ★★ 这一条是**首版没通过**才补出来的：原来的实现只挡 `is_symlink()`，
#     而目录联接的 `is_symlink()` 是 False，`rglob` 照样走进去 ⇒
#     工作区外的文件名漏进"产出物清单"。判据因此改成**结构判断**（解析后在不在根里）。
check("★ 目录联接不跟（联接里的文件不许出现在清单里）",
      any("secret.md" in p for p in got), False)
# 清单缓存只加速遍历，不改变磁盘事实：新增、修改、删除都要下一问可见。
M._artifact_index(RUN)
(RUN / "cache-new.txt").write_text("one", encoding="utf-8")
check("★ 新文件立即可见", "cache-new.txt" in paths_of("_webtest"), True)
(RUN / "cache-new.txt").write_text("changed", encoding="utf-8")
check("★ 文件大小变化立即可见",
      next(f["size"] for f in M.artifacts("_webtest")["files"] if f["path"] == "cache-new.txt"), 7)
os.replace(RUN / "cache-new.txt", OUTSIDE / "cache-old.txt")
check("★ 文件移走立即可见", "cache-new.txt" in paths_of("_webtest"), False)
_old_walk = S.Manager._walk
_walk_calls = [0]
def _count_walk(cls, base, cur, depth, out):
    _walk_calls[0] += 1
    return _old_walk.__func__(cls, base, cur, depth, out)
S.Manager._walk = classmethod(_count_walk)
try:
    M._artifact_index(RUN)
    _before_walk = _walk_calls[0]
    M._artifact_index(RUN)
    check("★ 未变化时复用清单", _walk_calls[0], _before_walk)
finally:
    S.Manager._walk = _old_walk
check("★ 目录联接本身也不列出（它不是产出物）", any(p == "junc" for p in got), False)
check("count 与 files 一致", lst["count"], len(lst["files"]))
check("total_bytes = 各文件之和",
      lst["total_bytes"], sum(f["size"] for f in lst["files"]))
truthy("每个条目带 mtime / ext", all(f.get("mtime") and "ext" in f for f in lst["files"]))

print("\n── 1b. 没有归档的旧任务：照常回答，不报错 ──")
check("不存在的目录 → exists:false（而不是 error）",
      "error" in M.artifacts("_no_such_run"), False)
check("不存在的目录 → 空列表", M.artifacts("_no_such_run")["files"], [])
check("不存在的目录 → count 0", M.artifacts("_no_such_run")["count"], 0)
check("非法 id（../）被拒", "error" in M.artifacts("../etc"), True)
check("非法 id（含斜杠）被拒", "error" in M.artifacts("a/b"), True)

# ══════════════════════ 2. 取回：围栏 ══════════════════════
print("\n── 2. 取回：能被列出来的才能被取回 ──")
check("正控：清单里的文件取得回", M.artifact_path("_webtest", "draft.md"), RUN / "draft.md")
check("正控：子目录里的也能", M.artifact_path("_webtest", "sub/table.csv"), RUN / "sub" / "table.csv")
check("★ 负控：session.json 取不回（能被取回就是任意文件读）",
      M.artifact_path("_webtest", "session.json"), None)
check("★ 负控：trace.jsonl 取不回", M.artifact_path("_webtest", "trace.jsonl"), None)
check("★ 负控：conversation.json 取不回",
      M.artifact_path("_webtest", "conversation.json"), None)
check("★ 负控：路径穿越 ../ 取不回", M.artifact_path("_webtest", "../_webtest_outside/secret.md"), None)
check("★ 负控：只有文件名不同也取不回（必须逐字在清单里）",
      M.artifact_path("_webtest", "draft.MD"), None)
check("★ 负控：经目录联接取回也不行（清单里没有它 ⇒ 取不回）",
      M.artifact_path("_webtest", "junc/secret.md"), None)
check("★ 负控：反向斜杠穿越取不回（Windows 上 \\ 是合法分隔符）",
      M.artifact_path("_webtest", "sub\\..\\..\\_webtest_outside\\secret.md"), None)
check("★ 负控：绝对路径取不回",
      M.artifact_path("_webtest", str(OUTSIDE / "secret.md")), None)
check("空 path 取不回", M.artifact_path("_webtest", ""), None)
check("非法 rid 取不回", M.artifact_path("../x", "draft.md"), None)

# ══════════════════════ 2b. 会话内追问（Manager 半边）══════════════════════
# 「接着说」是连续会话的地基。这里测 `Manager.follow_up` 的**拒绝阶梯**与
# **受理装载** —— 内核怎么消费追问（消息栈追加）由 resume_test.py 管，两边不重复。
print("\n── 2b. 会话内追问：id 校验 / 工作区定位 / 快照口径 ──")


class _NoRunKernel:
    """替身内核：`follow_up` 只在入口判 `kernel is None`，不跑它 ——
    但要测到「工作区定位 / 快照读取」就必须**有**内核。run() 一被调就抛，
    由 `_exec` 的兜底如实收尾，测试轮询 run.done 即可。"""

    def run(self, *a, **k):
        raise RuntimeError("测试不跑真任务")


_KERNEL_SAVED = M.kernel
M.kernel = _NoRunKernel()                      # type: ignore[assignment]
_r2 = None
try:
    check("空追问被拒", M.follow_up("_webtest", "   ")[0] is None, True)
    check("非法 id 被拒（含斜杠）", M.follow_up("a/b", "hi")[0] is None, True)
    check("非法 id 被拒（穿越）", M.follow_up("..", "hi")[0] is None, True)
    _r0, _e0 = M.follow_up("_no_such", "hi")
    check("不存在的会话如实拒（不糊弄成成功）", _r0 is None and "找不到" in _e0, True)
    _NS = WS / "_nosnap"
    _NS.mkdir(parents=True, exist_ok=True)
    (_NS / "draft.md").write_text("x", encoding="utf-8")
    _r1, _e1 = M.follow_up("_nosnap", "hi")
    check("没有对话快照 ⇒ 如实拒绝", _r1 is None and "没有可接续" in _e1, True)

    _r2, _e2 = M.follow_up("_webtest", "接着说一句")
    check("快照在 ⇒ 追问受理", _e2 == "" and _r2 is not None, True)
    if _r2 is not None:
        check("受理后立刻可在内存找到（active 路径的 run.workspace 有值）",
              M.runs.get(_r2.id) is _r2, True)
        check("★ 工作区回到原会话目录（追问不能另起炉灶）",
              _r2.workspace, str(RUN))
        check("★ resume 里带着追问原话（内核负责把它追加进对话）",
              (_r2.resume or {}).get("follow_up"), "接着说一句")
        check("goal = 追问原话（这一轮的输入）", _r2.goal, "接着说一句")
        for _ in range(200):                   # 最多等 10s：假内核当场抛，收尾很快
            if _r2.done:
                break
            time.sleep(0.05)
        check("替身内核的失败被如实收尾（不卡死、不静默）",
              _r2.done and "RuntimeError" in _r2.error, True)
        check("收尾后让出串行位（active 清空，下一个追问进得来）",
              M.active is None, True)
        check("快照在的会话 _followable 为真（界面「可接着说」的判据）",
              _r2._followable(), True)
finally:
    M.kernel = _KERNEL_SAVED                   # type: ignore[assignment]
    if _r2 is not None:
        M.runs.pop(_r2.id, None)               # 假内核的 run 不留给后面的 HTTP 段

# ══════════════════════ 3. 端到端 HTTP ══════════════════════
# 为什么非要走真 HTTP：`/api/artifact` 是**新增路由**，
# 直接调 Manager 测不到"GET 路由表里到底有没有接上它"。
# （本项目的 119 字事件同源：单元里对、链路上错。）
print("\n── 3. 端到端：真起服务、真发请求 ──")
httpd = S.Server(("127.0.0.1", 0), S.Handler)
httpd.mgr = M                                  # type: ignore[attr-defined]
port = httpd.server_address[1]
th = threading.Thread(target=httpd.serve_forever, daemon=True)
th.start()
# ⚠️ 本机设着 HTTP_PROXY，它会连 127.0.0.1 一起拦（吃过 502）——
#    所以这里显式把代理清空，而不是去信环境变量。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = f"http://127.0.0.1:{port}"


def get(path: str):
    try:
        with _opener.open(BASE + path, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def post_json(path: str, obj):
    """JSON 体 POST。与材料区那节的 `post(path, bytes)` 分工不同、名字也不同 ——
    两个助手撞名会让后到者静默顶掉先到者（本轮实测就是这么炸的）。"""
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


try:
    # 无构建静态模块：页面引用的资产必须经白名单可取，目录不可浏览。
    for asset in ("tokens.css", "workspace.css", "reader.css", "layout.css",
                  "library.css", "mentor.css", "core.js", "workspace.js",
                  "library.js", "mentor.js", "reader.js"): 
        st, payload, hdr = get("/static/" + asset)
        check("静态模块可取：" + asset, st, 200)
        truthy("静态模块非空：" + asset, len(payload) > 0)
    st, _, _ = get("/static/../server.py")
    check("静态目录穿越不被路由接受", st, 404)
    for wrong in ([], "text", None):
        st, payload = post_json("/api/settings", wrong)
        check("JSON 非对象统一 400：" + repr(wrong), st, 400)
        truthy("错误载荷可读", json.loads(payload).get("error"))

    st, body, _ = get("/api/artifacts?id=_webtest")
    check("GET /api/artifacts → 200", st, 200)
    check("★ 载荷能解析且列出产物",
          sorted(f["path"] for f in json.loads(body)["files"]),
          ["draft.md", "fig.png", "sub/table.csv"])

    # 逐字相等：拿**磁盘上的真实字节**当期望，而不是把字符串写死在测试里 ——
    # 本机 `write_text` 会把 \n 翻成 \r\n（首版就是这么假失败的）。
    raw = (RUN / "draft.md").read_bytes()
    st, body, hdr = get("/api/artifact?id=_webtest&path=draft.md")
    check("GET /api/artifact → 200", st, 200)
    check("★ 内容逐字相等（不是预览、不是摘要哈希）", body, raw)
    truthy("文本类带 charset=utf-8（否则中文界面乱码）",
           "charset=utf-8" in (hdr.get("Content-Type") or ""))

    st, body, hdr = get("/api/artifact?id=_webtest&path=fig.png")
    check("二进制原样发回", body, b"\x89PNG\r\n\x1a\nFAKE")
    check("二进制给的是 image/png", (hdr.get("Content-Type") or "").startswith("image/png"), True)

    st, _, hdr = get("/api/artifact?id=_webtest&path=draft.md&dl=1")
    check("下载模式带 Content-Disposition", "attachment" in (hdr.get("Content-Disposition") or ""), True)

    st, _, _ = get("/api/artifact?id=_webtest&path=session.json")
    check("★ 负控：账本文件走 HTTP 也是 404", st, 404)
    st, _, _ = get("/api/artifact?id=_webtest&path=../_webtest_outside/secret.md")
    check("★ 负控：穿越走 HTTP 也是 404", st, 404)
    st, _, _ = get("/api/artifact?id=_webtest&path=junc%2Fsecret.md")
    check("★ 负控：目录联接穿越走 HTTP 也是 404", st, 404)
    st, _, _ = get("/api/artifact?id=_webtest_outside&path=secret.md")
    check("★ 负控：换个 rid 去够工作区外的目录 → 404", st, 404)
    st, _, _ = get("/api/artifacts?id=_no_such_run")
    check("不存在的任务 → 200 + exists:false（不是 404，'没有'要看得见）", st, 200)
    st, _, _ = get("/api/artifact?id=_no_such_run&path=x.md")
    check("不存在的任务取文件 → 404", st, 404)

    # ── /api/orig/* 的 `?file=` 围栏（2026-10-12 体检补）──────────────
    # 背景：这条入口原先只判"绝对路径 + 存在"，于是
    #   GET /api/orig/raw?file=C:\Users\<你>\.rongke\secrets.json
    # 能把**荣珂自己的密钥**原样读出去（sha256 与本地直读逐字相等，byte-exact）。
    # 同文件的 /api/artifact 一直是严格围栏的 —— 两条取文件的通路必须同一套纪律。
    # 下面判据照上面 artifact 那 8 条的样式写：**每条都打在自己的判据上**。
    _orig_out = OUTSIDE / "outside.pdf"           # 工作区外的合法扩展名
    _orig_out.write_bytes(b"%PDF-1.4 fake\n")
    _orig_in = RUN / "inside.pdf"                 # 工作区内的合法扩展名（正控）
    _orig_in.write_bytes(b"%PDF-1.4 fake\n")

    st, body, _ = get("/api/orig/open?file=" + urllib.parse.quote(str(_orig_out)))
    _j = json.loads(body)
    check("★ 负控：工作区外的 .pdf 被拒（根围栏）", _j.get("ok"), False)
    check("★ 拒的理由是围栏，不是别的（要说得出话）",
          "不在本任务的工作区" in (_j.get("error") or ""), True)

    st, body, _ = get("/api/orig/open?file=" + urllib.parse.quote(str(_orig_in)))
    check("★ 正控：工作区内的 .pdf 放行（证明不是'一律拒'）",
          json.loads(body).get("ok"), True)

    st, body, _ = get("/api/orig/open?file=" + urllib.parse.quote(
        str(OUTSIDE / "secret.txt")))
    check("★ 负控：扩展名不合法即使在工作区外也拒",
          json.loads(body).get("ok"), False)

    st, _, _ = get("/api/orig/raw?file=" + urllib.parse.quote(str(_orig_out)))
    check("★ 负控：/api/orig/raw 同样拒（读字节那条最容易漏）", st, 404)

    # 穿越写法：拿工作区内的合法 pdf，把 ..\ 塞进路径。
    # ⚠️ 目标必须**真实存在** —— 第一版我写成 `..\..\..\outside.pdf`（不存在），
    #    于是它是被"文件不存在"挡下的，跟围栏无关；负控把它抓了出来
    #    （摘掉围栏它照样绿）。现在指向一个真在盘上、且真在工作区外的文件。
    _evil = str(_orig_in.parent / ".." / ".." / "_webtest_outside" / "outside.pdf")
    _j = json.loads(get("/api/orig/open?file=" + urllib.parse.quote(_evil))[1])
    check("★ 负控：..\\ 穿越写法被拒（目标是真文件，只有围栏能挡）",
          _j.get("ok"), False)
    check("★ 穿越拒的理由是围栏（不是'不存在'）",
          "不在本任务的工作区" in (_j.get("error") or ""), True)

    # /api/follow 的路由半边：这一段内核是 None（原状态）——
    # 判据只锁「路由接上了、拒绝的形状对」，不碰内核语义（2b 已管）。
    st, body = post_json("/api/follow", {"id": "_no_such_run", "text": "hi"})
    check("POST /api/follow：路由接上了（409 而不是全局 404 'not found'）", st, 409)
    check("POST /api/follow：响应带可读原因",
          "内核未就绪" in json.loads(body).get("error", ""), True)
finally:
    httpd.shutdown()
    httpd.server_close()
    th.join(timeout=5)

# ══════════════════════ 4. 承重自检 ══════════════════════
print("\n── 4. 承重自检：把两道过滤各自短路 ⇒ 对应断言必须当场失守 ──")
_src = Path(S.__file__).read_text(encoding="utf-8")


def load_patched(text: str):
    """以**真的包内模块**加载打过补丁的源码。

    不能直接 `exec(src, {})`：server.py 里有 `from ..config import …`，
    相对导入要 `__package__`（首版就是这么炸的：`KeyError: '__name__' not in globals`）。
    写成临时文件 + `spec_from_file_location("rongke.web._probe…")` 才既保住包上下文、
    又拿到**独立的一份类**（而不是本模块已导入的那个 —— tools_test 第 6 节踩过同款坑）。
    """
    # 每进程唯一 —— 写死文件名时两个并发进程会互删对方正在加载的文件，
    # 症状是"单独跑全绿、一起跑就 rc=1"（2026-09-19 被这个骗过一轮）。
    p = Path(S.__file__).with_name(f"_probe_patched_{os.getpid()}.py")
    p.write_text(text, encoding="utf-8")
    name = "rongke.web._probe_patched"
    try:
        spec = importlib.util.spec_from_file_location(name, p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        mod.Manager.boot = lambda self: None        # type: ignore[method-assign]
        return mod
    finally:
        sys.modules.pop(name, None)
        # ★ 挪到固定名（覆盖），**不删**。上一版这里是 `p.unlink(missing_ok=True)`：
        #   每调用一次花 1 点，而且脚本崩在半路时那份"含故意缺陷、可被 import 的
        #   server.py 副本"就留在产品包里了 —— 本轮开工时清出 13 个历史残留，
        #   就是这么攒出来的。
        os.replace(p, _SCRATCH / "_probe_patched.py")


# ① 去掉"账本过滤"
# 锚点取**前缀**而不是整行：INTERNAL 是个会长大的集合（2026-09-19 就加了一项），
# 写死整行的话每加一项这个短路实验就静默失效 —— 而它正是"证明过滤在承重"的那条。
_p1 = _src.replace('INTERNAL = {"session.json", "trace.jsonl"', 'INTERNAL = {""')
check("补丁①确实改到了源码", _p1 != _src, True)
_leaked = sorted(f["path"] for f in load_patched(_p1).Manager().artifacts("_webtest")["files"])
check("★ 短路后 session.json / trace.jsonl 果然漏进列表（证明账本过滤在承重）",
      ("session.json" in _leaked and "trace.jsonl" in _leaked), True)
check("★ 对照组：未短路时它们都不在",
      ("session.json" in got, "trace.jsonl" in got,
       any("conversation.json" in p for p in got)), (False, False, False))

# ② 去掉"出界"这条结构判据（合上 `_inside`），联接必须立刻漏进来
_p2 = _src.replace("                if not cls._inside(p, base):\n                    continue",
                   "                if False:\n                    continue")
check("补丁②确实改到了源码", _p2 != _src, True)
_leak2 = sorted(f["path"] for f in load_patched(_p2).Manager().artifacts("_webtest")["files"])
check("★ 短路后目录联接里的文件果然漏进列表（证明结构判据在承重）",
      any("secret.md" in p for p in _leak2), True)
check("★ 对照组：未短路时它不在", any("secret.md" in p for p in got), False)

# ══════════════════════ 5. 材料区（上传）══════════════════════
# 评审第④条「输入输出离科研工作流太远」的入口。这里要守的是**两条**：
#   ① **文件名是用户可控输入，而它会被拼进路径** —— 围栏的第一道在这里，
#      前端做了也拦不住绕过前端的请求，所以判据必须落在服务端；
#   ② **提交时材料区改名成正式工作区** —— 就地复用会让第二个任务覆盖
#      第一个的 `session.json`（归档路径 = 工作区路径），这是一次静默的事故。
#
# ⚠️ 材料区落在 `ws_dir()/_inbox`，而 `ws_dir()` 是**真实的 runs/** ——
#    里面可能正躺着用户自己递的材料。所以本节把 `ws_dir` 临时重定向到夹具目录，
#    而不是去清真实目录（**测试永远不许删用户的东西**）。
print("\n── 5. 材料区：文件名围栏 + 改名成工作区 ──")

TMPWS = ROOT / "_webtest_ws"
# 清场用 `fresh_dir`：一次挪，不是 N 次删除（见 `_tkit`）。
T.fresh_dir(TMPWS, "webtest-ws")

# 本节会往 `rongke/web/` 写"注入过缺陷的 server.py 副本"来验判据承重，而脚本
# 崩在半路（或被额度守卫杀掉 ⇒ `finally` 不跑）时它就留在产品包里了 ——
# 一个**可被 import、且含故意缺陷**的文件躺在产品包，不是垃圾问题，是隐患。
#
# ⚠️ 副本**只能**留在 `rongke/web/`：`server.py` 里有
#    `HERE = Path(__file__).resolve().parent`（找 `index.html`），位置就是语义。
#    所以这里的办法是**开工前清扫**，不是"换个落点"。
#    清扫收拢进 `_tkit.sweep_probes()` —— 原先这段是**手写的一份平行实现**，
#    它只扫 `rongke/web/`，而 `rongke/` 下（intervene/resume 的 loop.py 副本）
#    **没人扫**，于是堆了 27 个。
T.sweep_probes()
_real_ws_dir = S.ws_dir
# 节前记下**真实**材料区的样子。末尾拿它比对 —— 不写死"应该为空"：
# 用户可能真递过材料，写死 False 会在他身上假失败（假失败会教人关掉检查）。
_REAL_INBOX = WS / "_inbox"
_mat_before = (sorted(p.name for p in _REAL_INBOX.iterdir())
               if _REAL_INBOX.is_dir() else None)
# 同上，`_removed` 也记一份：它和 `_inbox` 都在 `ws_dir()` 下，重定向漏一个就漏一片。
_REAL_REMOVED = WS / "_removed"
_rm_real_before = (sorted(p.name for p in _REAL_REMOVED.iterdir())
                   if _REAL_REMOVED.is_dir() else None)
S.ws_dir = lambda: TMPWS                      # type: ignore[assignment]
# ★ 同一条纪律的第二处：`history()` 会把**记忆台账**合并进来，而台账根目录在
#   `memory/` —— 不在 `ws_dir()` 的重定向范围内。2026-09-26 实测：四臂实验往共享
#   台账追加 39 条后，本节"真任务照样列得出来"的正控被挤出 `history(50)` 窗口，
#   回归自己变红（红的是测试的环境依赖，不是产品）。记忆也必须一起定向。
TMPMEM = ROOT / "_webtest_mem"
T.fresh_dir(TMPMEM, "webtest-mem")
_real_mem_root = M_.default_root
M_.default_root = lambda cfg=None: TMPMEM        # type: ignore[assignment]
# ⚠️ 上面这行**只覆盖了 memory 模块的名字**。`server.py` 是
#    `from ..memory import Memory, default_root` —— **按名导入**，
#    server 自己持有一份绑定，改 `M_.default_root` 对它**无效**。
#    而 `history()` 合并台账那一段（server.py:1415）调的正是 server 那份
#    ⇒ 真实台账照样混进来 ⇒ 下面「真任务照样列得出来」的正控会被挤出
#    `history(50)` 窗口而假红（红的是环境依赖，不是产品）。
#    上面那段注释描述的正是这个现象，但当时只补了一半，所以现象会复发。
#    实测（2026-10-12）：`M_.default_root is S.default_root` 为 True，
#    但赋给前者只改 memory 的全局；再打 `S.default_root` 后断言立即转绿。
S.default_root = lambda cfg=None: TMPMEM         # type: ignore[assignment]

try:
    # ── 5a. 文件名消毒：纯函数，逐条打 ──
    cases = [
        ("../../evil.md", "evil.md", "路径穿越（`..` 只留 basename）"),
        ("..\\..\\evil2.md", "evil2.md", "反斜杠穿越（`os.path.basename` 在 Linux 上不认 `\\`）"),
        ("C:\\Windows\\x.md", "x.md", "绝对路径"),
        ("/etc/passwd.md", "passwd.md", "POSIX 绝对路径"),
        ("con.txt", "_con.txt", "Windows 保留名（能建却读不回）"),
        ("NUL.md", "_NUL.md", "保留名大小写不敏感"),
        ("a<b>c:d.md", "abcd.md", "Windows 非法字符"),
        ("trail.md...", "trail.md", "结尾的点（Windows 建不出来）"),
        ("trail.md  ", "trail.md", "结尾的空格（同上）"),
    ]
    for raw, want, why in cases:
        try:
            check(f"文件名消毒：{why}", S.safe_upload_name(raw), want)
        except ValueError as e:
            check(f"文件名消毒：{why}", f"拒绝({e})", want)

    for bad in ("", ".", "..", "///", "..\\.."):
        got = "接受"
        try:
            got = S.safe_upload_name(bad)
        except ValueError:
            got = "拒绝"
        check(f"负控：不可用文件名 {bad!r}", got, "拒绝")

    check("★ 点开头的文件名不被'洗干净'（`.env` 是正经名字，不是脏东西）",
          S.safe_upload_name(".env"), ".env")
    check("★ 超长名被截断（挡住超长名撑爆路径）",
          len(S.safe_upload_name("x" * 300 + ".md")), 120)

    # ── 5b. 落盘：真写文件，再逐字读回来 ──
    M2 = S.Manager()
    it = M2.add_material("稿子.md", "样本量：120\n".encode("utf-8"))
    check("落进材料区（不是别处）", it["path"], str(TMPWS / "_inbox" / "稿子.md"))
    check("★ 盘上字节与递进来的一字不差",
          (TMPWS / "_inbox" / "稿子.md").read_bytes(), "样本量：120\n".encode("utf-8"))
    check("文本类给了预览", it["preview"], "样本量：120\n")
    check("★ 预览是按字数截的（不许把整篇塞回前端）",
          len(M2.add_material("long.md", b"a" * 5000)["preview"]), 2000)
    pdf = M2.add_material("paper.pdf", b"%PDF-1.4 fake")
    check("★ 二进制明说没有预览（M 型：不许把'没看'渲染成'看过'）",
          (pdf["previewable"], pdf["preview"]), (False, ""))
    dup = M2.add_material("稿子.md", b"second")
    check("★ 同名不覆盖，改挂 -2（静默覆盖 = 静默的数据事故）", dup["name"], "稿子-2.md")
    check("★ 原文件没被动过（覆盖的第一受害者是它）",
          (TMPWS / "_inbox" / "稿子.md").read_bytes(), "样本量：120\n".encode("utf-8"))

    # ── 5b2. ★ 清单以**磁盘**为准：换成新实例（= 控制台重启）不许失忆 ──
    # 旧实现把清单存在进程内存里 ⇒ 一重启，材料还在 `_inbox` 躺着、界面显示"空"。
    # 这不是假想：本轮修 bug 必须重启服务，当场撞上了（盘上 1 份 · 清单 0 份）。
    # 这条判据在旧实现上**必然红**，所以它证明了这次重构真的在承重。
    M3 = S.Manager()                      # 全新实例：内存里什么都没继承
    _n3 = [m["name"] for m in M3.materials]
    check("★ 换个新实例也看得见盘上的材料（清单不靠内存活着）", "稿子.md" in _n3, True)
    check("★ 新实例报的落点也是真的",
          [m["path"] for m in M3.materials if m["name"] == "稿子.md"],
          [str(TMPWS / "_inbox" / "稿子.md")])
    check("★ 新实例的预览是重新扫出来的（不是缓存）",
          [m["preview"] for m in M3.materials if m["name"] == "稿子.md"], ["样本量：120\n"])
    (TMPWS / "_inbox" / "x.exe").write_bytes(b"MZ")
    check("负控：不在收件类型里的文件不算材料（不冒充）",
          "x.exe" in [m["name"] for m in M3.materials], False)

    for bad, why in (("x.exe", "不收的扩展名"), ("noext", "没有扩展名")):
        try:
            M2.add_material(bad, b"x")
            got = "接受"
        except ValueError:
            got = "拒绝"
        check(f"负控：{why} ⇒ 拒绝", got, "拒绝")
    try:
        M2.add_material("empty.md", b"")
        got = "接受"
    except ValueError:
        got = "拒绝"
    check("负控：空文件 ⇒ 拒绝（一个 0 字节的'材料'只会让模型白读一轮）", got, "拒绝")

    # ── 5c. 移除：清单移掉 + 原件**挪**进 `_removed`（不删）──
    # 这一节是上一个坑的回归测试。老版 `drop_material` 里有一句
    #   `except Exception: pass` → `return True`
    # ⇒ 环境的批量删除拦截把 `unlink` 打回来时，界面说"已移除"、盘上文件还在。
    # 所以这里要的不只是"能移除"，而是**挪不动时它会不会说实话**。
    _rm_dir = TMPWS / "_removed"
    _rm_before = set(p.name for p in _rm_dir.iterdir()) if _rm_dir.is_dir() else set()
    _res = M2.drop_material("paper.pdf")
    check("移除成功", _res.get("ok"), True)
    check("★ 清单里没有了", [m["name"] for m in M2.materials].count("paper.pdf"), 0)
    check("★ 材料区里也没有了（不是只从清单里划掉）",
          (TMPWS / "_inbox" / "paper.pdf").exists(), False)
    check("★ 原件没被删、而是挪进了 _removed"
          "（用户自己递进来的稿子，一个 × 就不可逆地没了 = 不该）",
          (TMPWS / "_removed" / "paper.pdf").is_file(), True)
    check("★ 如实报出原件去了哪（前端那一行提示就靠它，不许只回一句'成功'）",
          _res.get("kept_at"), str(TMPWS / "_removed" / "paper.pdf"))
    check("★ 挪不是重写：内容原样",
          (TMPWS / "_removed" / "paper.pdf").read_bytes(), b"%PDF-1.4 fake")
    check("负控：移除不存在的名字 → ok=False 且不抛", M2.drop_material("nope.md").get("ok"), False)
    check("负控：移除穿越名字 → ok=False（不许借这个接口够到别处）",
          M2.drop_material("../../x.md").get("ok"), False)
    check("负控：穿越名字没在 _removed 里留下任何东西",
          set(p.name for p in _rm_dir.iterdir()) - _rm_before, {"paper.pdf"})

    # ── 5c2. ★ 正控：**把"挪不动"造出来**，看它是如实报还是装死 ──
    # 「负控证明它会拦坏输入」还不够 —— 这个 bug 的形态是**失败被藏起来**，
    # 所以必须人为制造一次失败，否则修没修都看不出区别。
    class _BoomOS:
        """只有 `replace` 会炸，其余原样转给真的 os。"""
        def __getattr__(self, k):
            return getattr(os, k)

        @staticmethod
        def replace(*a, **kw):
            raise OSError("模拟：原件被别的程序占用")

    M2.add_material("busy.md", b"x")
    _real_os = S.os
    S.os = _BoomOS()                        # type: ignore[assignment]
    try:
        _bad = M2.drop_material("busy.md")
    finally:
        S.os = _real_os                     # type: ignore[assignment]
    check("★ 挪不动时 ok 仍为 True（清单那一步不该跟着失败）", _bad.get("ok"), True)
    check("★ 挪不动时 moved=False（不许状告成功）", _bad.get("moved"), False)
    check("★ 挪不动时如实说出原件还在哪",
          _bad.get("kept_at"), str(TMPWS / "_inbox" / "busy.md"))
    check("★ 说的和做的一致：原件真的还在",
          (TMPWS / "_inbox" / "busy.md").is_file(), True)
    check("★ 给用户的那一行里明说'没挪动'（不是一个'成功'糊过去）",
          "没挪动" in (_bad.get("note") or ""), True)

    # ── 5d. 提交：改名成正式工作区 ──
    took = M2.take_inbox()
    check("★ 提交后材料区改名成了工作区（同盘 rename，原件零拷贝）",
          (took is not None, (took / "稿子.md").is_file()), (True, True))
    check("★ 原材料区已交出去（不留一份会分家的副本）", M2.inbox, None)
    check("清单也清了（不能让人以为还没交）", M2.materials, [])
    check("负控：没材料时 take 返回 None，不许凭空造工作区", S.Manager().take_inbox(), None)

    # ── 5d2. 历史列表：`_` 开头的是**建材**，不是用户跑过的任务 ──
    # `runs/` 里住着 `_inbox` / `_removed` / `_regress` 和一堆夹具，它们只要带上
    # 一个 `session.json` 就会冒充成用户的历史记录。`ui_shot.latest_archive()`
    # 一直在按 `_` 筛，而 `history()` 不筛 —— 同一个目录两套口径，早晚出两副面孔。
    for _nm, _g in (("_fake_task", "夹具冒充的任务"), ("realtask", "真任务")):
        _d = TMPWS / _nm
        _d.mkdir(parents=True, exist_ok=True)
        (_d / "session.json").write_text(
            json.dumps({"id": _nm, "goal": _g, "started": 1}, ensure_ascii=False),
            encoding="utf-8")
    _hist = [r.get("id") for r in M3.history(50)]
    check("★ 历史列表不列 `_` 开头的目录（否则夹具会冒充成用户的任务）",
          "_fake_task" in _hist, False)
    check("★ 正控：真任务照样列得出来（别把筛子做成'什么都不列'）",
          "realtask" in _hist, True)

    # ── 5d3. `history()` 的行缓存：省的是解析，绝不能省掉"文件变了" ──
    # 动机（W30 实测）：`history(n)` 原先**完全不随 n 变** —— 为了给 10 条，它把
    # 48 份归档（最大 2.2 MB）全读全解析，热跑 169 ms，且随历史增长线性变慢。
    # 加了按 (mtime_ns, size) 做键的缓存后降到 7.8 ms（21×）。
    # ★ 缓存最容易出的错不是"不快"，是**读到过时结论**。所以判据落在"改了文件
    #   必须看见新值"上，而不是"第二次更快"上。
    _c1 = M3.history(50)
    _row1 = next(r for r in _c1 if r["id"] == "realtask")
    check("① 缓存前：goal 是建文件时那个", _row1["goal"], "真任务")

    (_d / "session.json").write_text(
        json.dumps({"id": "realtask", "goal": "改过的目标", "started": 1},
                   ensure_ascii=False), encoding="utf-8")
    # mtime 分辨率在部分文件系统上只到秒/百纳秒；显式推一下，避免"同一瞬写两次"
    # 让本判据变成看平台脸色的运气题。
    _mt = time.time() + 2
    os.utime(_d / "session.json", (_mt, _mt))
    _row2 = next(r for r in M3.history(50) if r["id"] == "realtask")
    check("★ ② 归档改过之后必须读到新 goal（缓存不许给出过时结论）",
          _row2["goal"], "改过的目标")

    # ③ `followable` 看的 `conversation.json` 与 session.json 是**分开写**的，
    #    所以它也必须进缓存键 —— 否则"归档没动、快照动了"时会一直报不可追问。
    check("③ 建快照前：还没有 conversation.json ⇒ 不可追问",
          next(r for r in M3.history(50) if r["id"] == "realtask")["followable"], False)
    (_d / "conversation.json").write_text("{}", encoding="utf-8")
    check("★ ④ 快照出现后 followable 必须变真（它不在 session.json 里，别漏进缓存键）",
          next(r for r in M3.history(50) if r["id"] == "realtask")["followable"], True)

    # ⑤ 负控：缓存给出的行必须是**副本**，调用方改了不能污染缓存。
    _victim = M3.history(50)
    for _r in _victim:
        if _r["id"] == "realtask":
            _r["goal"] = "被外面改脏了"
    check("★ ⑤ 缓存对外给副本（改返回值不许污染下一次的结果）",
          next(r for r in M3.history(50) if r["id"] == "realtask")["goal"], "改过的目标")

    # ── 5e. `start()` 真的把工作区接上了吗（这是"接上"还是"写了没用"的分界）──
    M2.kernel = object()                       # 骗过"内核未就绪"那道闸
    M2._exec = lambda run: None                # type: ignore[assignment]
    M2.add_material("b.md", b"hi")
    run, err = M2.start("用这份材料写摘要", None)
    check("起任务成功", (run is not None, err), (True, ""))
    # 判据落在**内容**上而不是目录名上：名字是时间戳，同一秒内连开两次必然带 `-2`
    # （我第一版就是拿"第一次的名字"去比"第二次"，报了个假失败）。
    check("★ 材料跟着进了这次任务的工作区（不是另开一个空工作区）",
          (Path(run.workspace) / "b.md").is_file() if run else False, True)
    check("★ 内容原样（改名不等于重写）",
          (Path(run.workspace) / "b.md").read_bytes() if run else b"", b"hi")
    check("★ start 真的把材料区交出去了（inbox/清单都清空 ⇒ 这条链接上了）",
          (M2.inbox, M2.materials), (None, []))

    # ── 5f. 运行中不许动材料 ──
    check("★ 有任务在跑时 busy() 报原因（上传与清空都要走它）",
          "任务在跑" in M2.busy(), True)

    # ── 5g. 端到端 HTTP：路由到底接上没有 ──
    # 直接调 Manager 测不到"do_POST 里有没有这个分支"—— 这正是本项目
    # "单元里对、链路上错"那一族。所以真起服务、真发请求。
    M3 = S.Manager()
    httpd2 = S.Server(("127.0.0.1", 0), S.Handler)
    httpd2.mgr = M3                             # type: ignore[attr-defined]
    port2 = httpd2.server_address[1]
    th2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
    th2.start()
    B2 = f"http://127.0.0.1:{port2}"

    def get2(path: str):
        # 必须另起一个：上面那个 `get` 绑的是**已经关掉**的 BASE。
        # 复用它会得到 ConnectionRefused（我第一版就这么翻的车）。
        try:
            with _opener.open(B2 + path, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw2 = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw2)
            except Exception:
                return e.code, {"error": raw2[:120]}

    def post(path: str, data: bytes, ctype: str = "application/octet-stream"):
        r = urllib.request.Request(B2 + path, data=data,
                                   headers={"Content-Type": ctype})
        try:
            with _opener.open(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw2 = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw2)
            except Exception:
                return e.code, {"error": raw2[:120]}

    try:
        st, _ = get2("/api/materials")
        check("GET /api/materials → 200", st, 200)
        st, j = post("/api/upload?name=paper.md", "样本 9\n".encode("utf-8"))
        check("POST /api/upload → 200", st, 200)
        check("★ 载荷带回清单（前端不必猜）", len(j.get("materials") or []), 1)
        st, j = post("/api/upload?name=..%2F..%2Fesc.md", b"x")
        check("★ 负控：穿越文件名走 HTTP 也只落 basename",
              j["item"]["name"] if st == 200 else f"HTTP {st}", "esc.md")
        check("★ 负控：落点仍在材料区里",
              TMPWS.resolve() in Path(j["item"]["path"]).resolve().parents
              if st == 200 else False, True)
        check("★ 负控：材料区外面没有多出文件（穿越的第一受害者就是它）",
              (ROOT / "_webtest_ws").parent.joinpath("esc.md").exists(), False)
        st, j = post("/api/upload?name=a.exe", b"x")
        check("负控：不收的扩展名 → 400", st, 400)
        st, j = post("/api/upload?name=a.md", b"")
        check("负控：空体 → 400", st, 400)
        st, j = post("/api/upload", b"x")
        check("负控：没有文件名 → 400", st, 400)
        st, j = post("/api/material_drop", json.dumps({"name": "nope.md"}).encode(),
                     "application/json")
        check("负控：删不存在的材料 → 404", st, 404)
        M3.active = "fake"
        M3.runs["fake"] = S.Run(id="fake", goal="占位")     # done=False
        st, j = post("/api/upload?name=b.md", b"y")
        check("★ 负控：任务在跑时上传 → 409（不是 400，这是'此刻不成'）", st, 409)
        M3.active = None
    finally:
        httpd2.shutdown()
        httpd2.server_close()
        th2.join(timeout=5)

    # 承重自检：把 `safe_upload_name` 的 basename 那一步短路 ⇒ 围栏必须当场失守
    _ssrc = Path(S.__file__).read_text(encoding="utf-8")
    _sp = _ssrc.replace('    s = s.replace("\\\\", "/").split("/")[-1]',
                        "    pass   # 承重自检：不切 basename")
    check("补丁锚点命中（safe_upload_name 的 basename 那一步）", _sp != _ssrc, True)
    if _sp != _ssrc:
        _P = Path(S.__file__).with_name(f"_probe_web_{os.getpid()}.py")
        _P.write_text(_sp, encoding="utf-8")
        _nm = "rongke.web._probe_web"
        try:
            _spec = importlib.util.spec_from_file_location(_nm, _P)
            _mod = importlib.util.module_from_spec(_spec)
            sys.modules[_nm] = _mod
            _spec.loader.exec_module(_mod)
            leaked = _mod.safe_upload_name("../../evil.md")
        finally:
            sys.modules.pop(_nm, None)
            # 挪走而不是删：既不留隐患（含缺陷的副本不进产品包），
            # 也不依赖删除能不能成功（环境里有批量删除确认，别把测试卡在它上面）。
            # 落点用**固定名** ⇒ 每轮覆盖，不堆积。
            os.replace(_P, _SCRATCH / "_probe_web.py")
        check("★ 短路后穿越名原样漏出（证明这条判据在承重）", leaked, "../../evil.md")

    # 承重自检 ②：把 `materials` 从"从目录算"退回"从内存取" ⇒ 新实例必然失忆。
    # 我在 5b2 的注释里写了"这条判据在旧实现上必然红" —— 写了就要验，
    # 否则那句注释只是一句自我安慰（判据不承重 = G 型）。
    _a2 = ('    @property\n    def materials(self) -> list[dict]:\n'
           '        """当前材料清单（每次现算，见 `scan_materials`）。"""\n'
           '        return self.scan_materials()')
    _b2 = _a2.replace("return self.scan_materials()",
                      "return []   # 承重自检：模拟「清单只活在内存里」")
    _src2 = Path(S.__file__).read_text(encoding="utf-8")
    _rev2 = _src2.replace(_a2, _b2)
    check("承重自检锚点命中（materials 这个 property）", _rev2 != _src2, True)
    if _rev2 != _src2:
        _P2 = Path(S.__file__).with_name(f"_probe_web2_{os.getpid()}.py")
        _P2.write_text(_rev2, encoding="utf-8")
        _nm2 = "rongke.web._probe_web2"
        try:
            _sp2 = importlib.util.spec_from_file_location(_nm2, _P2)
            _m2 = importlib.util.module_from_spec(_sp2)
            sys.modules[_nm2] = _m2
            _sp2.loader.exec_module(_m2)
            _mem_sees = "稿子.md" in [m["name"] for m in _m2.Manager().materials]
        finally:
            sys.modules.pop(_nm2, None)
            os.replace(_P2, _SCRATCH / "_probe_web2.py")
        check("★ 退回「只认内存」之后新实例什么都看不见"
              "（证明上面那条「重启不失忆」的判据在承重）", _mem_sees, False)
finally:
    S.ws_dir = _real_ws_dir                     # type: ignore[assignment]
    M_.default_root = _real_mem_root            # type: ignore[assignment]
    S.default_root = _real_mem_root             # type: ignore[assignment]  ← 与上面成对
    T.stash(TMPWS, "webtest-ws")
check("材料区夹具已挪走", TMPWS.exists(), False)
_mat_after = (sorted(p.name for p in _REAL_INBOX.iterdir())
              if _REAL_INBOX.is_dir() else None)
check("★ 真实 runs/_inbox 一字未动（测试永远不许碰用户递进来的材料）",
      _mat_after, _mat_before)
# `_removed` 也得跟着隔离 —— 它是"挪"的落点，重定向漏了它，测试就会把
# 夹具文件堆进用户真实的 runs/ 里（不删，但脏）。
_rm_real_after = (sorted(p.name for p in _REAL_REMOVED.iterdir())
                  if _REAL_REMOVED.is_dir() else None)
check("★ 真实 runs/_removed 也没被测试碰过", _rm_real_after, _rm_real_before)

# ══════════════════════ 6. 拆夹具 ══════════════════════
print("\n── 6. 拆夹具：挪走联接不许波及目标目录 ──")
# ★ 这一节换过**两次**方向，两次都是因为判据没有判别力（G 型）：
#
#   ① 老版本：`cleanup` 里 `rmtree(OUTSIDE)` 会把 `secret.md` 删掉 ⇒ 断言
#      `(True, False)` 无论"顺序对不对"都成立 —— 它测不出任何东西。
#   ② 我第一次改完，注释里写"判别力来自 `shutil.rmtree(RUN)` 会跟进联接"——
#      **这句是错的**（2026-09-19 实测 + 读源码推翻，见 `_tkit.py --verify-purge`）：
#      Python 3.12 在 Windows 上**不**跟进 junction（`os.py:388` 的
#      `not entry.is_junction()`、`shutil.py:606` 的 `_rmtree_islink`）。
#      ⇒ 真把 `cleanup` 退化回 `rmtree(RUN)`，那条断言**照样绿** —— 我等于
#      又写了一条自认为在拦人、其实拦不住的判据（只是这次没栽，是被探针逼出来的）。
#
#   所以现在**两件事分开钉**，各钉各的：
#     · 「目标目录必须活着」 → 分辨**手写递归删除**（`_tkit._naive_rmtree` 那种
#       用 `Path.is_dir()` 判目录的写法）；对 3.12 的 `rmtree` 则**如实承认分辨不了**。
#     · 「台账记的节省量 == 清场前手数的代价」 → 分辨**挪是真的挪走了**（不是空转）。
#   ⇒ 改这一节的**根本理由**（额度 29 点 → 0）本身是可量化、可核对的，不靠形容词。
_sentinel = OUTSIDE / "secret.md"
_junc = RUN / "junc"
_junc_there = _junc.exists()
_cost = T.points(RUN)                 # ★ 必须在 cleanup **之前**数：挪完就数不出来了
check("夹具就位：联接指向的目标里确实有文件（否则下面那条测的是空气）",
      _sentinel.is_file(), True)
print(f"         （清场前手数：这棵夹具用「删」要花 {_cost} 点 —— 和一轮额度 50 点比一比）")
check("★ 夹具确实有份量：老写法（删）要吃掉一大块额度 ⇒ 「挪」才值得", _cost > 5, True)
if not _junc_there:
    print("  [SKIP] 目录联接没建成 —— 逃逸那一族本轮**没测**（不是通过）")
cleanup()
check("★ 挪走整棵夹具（含目录联接）**没有**动到联接指向的目标目录",
      _sentinel.is_file(), True)
check("★ 且 `RUN` 确实被挪走了（证明上一条不是「什么都没做」）",
      RUN.exists(), False)
check("★ 台账记的节省量 == 清场前手数的代价（证明「挪」是真挪走了，不是换个说法）",
      [r.get("saved") for r in T.ledger_rows() if r.get("tag") == "webtest-run"][-1:],
      [_cost])
if _junc_there:
    check("★ 联接本体是**跟着夹具一起挪走**的，不是被摘掉/删掉",
          list(T.TRASH.glob("webtest-run.*/junc")) != [], True)

# ══════════════════════ 7. W1 只读接口 ══════════════════════
# 工单 `00_总控/qoder-orchestration/W1-服务端只读接口.md` §4 要求的 6 项覆盖。
# 必须走**真 HTTP**（同第 3 节的理由）：直接调 Manager 测不到路由表接没接上。
# 夹具把 `ws_dir` 与记忆根都定向到临时目录 —— `/api/deliverables` 与 `/api/figures`
# 遍历的是 history()，不隔离就会把**用户真实的历史任务**读进断言里（假失败/看漏都是）。
print("\n── 7. W1 只读接口：/api/capabilities /api/gates /api/deliverables /api/figures ──")

WS7 = ROOT / "_webtest_ws7"
MEM7 = ROOT / "_webtest_mem7"
T.fresh_dir(WS7, "webtest-ws7")
T.fresh_dir(MEM7, "webtest-mem7")
_real_ws7 = S.ws_dir
_real_mem7 = M_.default_root
S.ws_dir = lambda: WS7                         # type: ignore[assignment]
M_.default_root = lambda cfg=None: MEM7       # type: ignore[assignment]
# ⚠️ 上面这行**只打了 memory 模块那个名字**，而 `server.py` 是
#     `from ..memory import Memory, default_root` —— **按名导入**，
#     server 自己持有一份绑定，改 `M_.default_root` 对它无效。
#     （实测：`M_.default_root is S.default_root` 为 True，但赋给前者
#      只是覆盖 memory 模块的全局，server 那份不动。）
#     ⇒ `history()` 里「台账里那些没有归档的更早任务」那一段（server.py:1415）
#       照样读**真实台账** ⇒ 真实任务混进夹具目录 ⇒
#       `_last_gate_verdict()` 取到真实最近任务，而不是夹具 `figtask01`。
#     这一条不补，本节 3 条断言会因"用户真的跑过任务"而假红 ——
#     失败与代码无关，纯粹是夹具漏打了一个口子。
S.default_root = lambda cfg=None: MEM7        # type: ignore[assignment]
try:
    M7 = S.Manager()
    httpd7 = S.Server(("127.0.0.1", 0), S.Handler)
    httpd7.mgr = M7                            # type: ignore[attr-defined]
    th7 = threading.Thread(target=httpd7.serve_forever, daemon=True)
    th7.start()
    B7 = f"http://127.0.0.1:{httpd7.server_address[1]}"

    def get7(path: str):
        """返回 (status, JSON)。JSON 解析不了 ⇒ (status, None) —— 由各条断言当场报。"""
        try:
            with _opener.open(B7 + path, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return e.code, None

    def get_raw7(path: str) -> int:
        """取回类断言只看状态码：载荷可能是**二进制**（png/pdf），不许按 JSON 解。"""
        try:
            with _opener.open(B7 + path, timeout=10) as r:
                r.read()
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        # ── 覆盖 4（先测"空仓库"这条失败路径，再往里放夹具）──
        st, j0 = get7("/api/deliverables")
        check("① 空仓库（无任何归档）→ 200", st, 200)
        check("① 空仓库 ⇒ total:0，且不报错（'没有'要看得见）",
              (j0 or {}).get("total", -1), 0)
        st, g0 = get7("/api/gates")
        check("① 空仓库 ⇒ gates.last 是 null 并附 hint（不许编假结果）",
              (st == 200 and (g0 or {}).get("last") is None
               and bool((g0 or {}).get("hint"))), True)

        # ── 历史任务夹具：一个有图有报告有归档、一个有图**没**报告 ──
        d1 = WS7 / "figtask01"
        (d1 / "sub").mkdir(parents=True, exist_ok=True)
        (d1 / "draft.md").write_text("# 稿子\n", encoding="utf-8")
        (d1 / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        (d1 / "sub" / "chart.pdf").write_bytes(b"%PDF-1.4 fake")
        (d1 / "figure-qa.json").write_text(json.dumps({
            "ok": True, "verdict": "PASS", "gates": {"source": "PASS"},
            "audited_at": "2026-09-27 10:00:00", "artifact_mtimes": {}},
            ensure_ascii=False), encoding="utf-8")
        (d1 / "session.json").write_text(json.dumps({
            "id": "figtask01", "goal": "画图并成稿", "started": 1750000000,
            "result": {"success": True,
                       "lint": {"score": 95},
                       "verdict": {"passed": True, "reasons": [], "warnings": ["软警告一条"],
                                   "writing": "", "lint": {"score": 95}}}},
            ensure_ascii=False), encoding="utf-8")
        d2 = WS7 / "noref02"
        d2.mkdir(parents=True, exist_ok=True)
        (d2 / "loose.png").write_bytes(b"\x89PNG\r\n\x1a\nLOOSE")
        (d2 / "session.json").write_text(json.dumps(
            {"id": "noref02", "goal": "只出图", "started": 1749999999,
             "result": {"success": True}}, ensure_ascii=False), encoding="utf-8")

        # ── 覆盖 1：4 个接口都 200 且是合法 JSON ──
        payloads = {}
        for ep in ("/api/capabilities", "/api/gates", "/api/deliverables", "/api/figures"):
            st, j = get7(ep)
            check(f"② {ep} → 200 且载荷是合法 JSON", (st, isinstance(j, dict)), (200, True))
            payloads[ep] = j
        caps = payloads["/api/capabilities"] or {}
        gates_j = payloads["/api/gates"] or {}
        deliv = payloads["/api/deliverables"] or {}
        figs_j = payloads["/api/figures"] or {}

        # ── 覆盖 2：capabilities 每条 file 真的存在；条数 ≥ 14 ──
        rows = [it for g in caps.get("groups", []) for it in g.get("items", [])]
        check("② 条目数 ≥ 14（S0 §2 的表一项不许少）", len(rows) >= 14, True)
        missing = [r["file"] for r in rows if not (ROOT / r["file"]).exists()]
        check("② ★ 每条 file 用 Path.exists() 独立核实过（一个不许缺）", missing, [])
        check("② live 全为真（文件确在 ⇒ 不许有 false 冒充活着）",
              [r["live"] for r in rows], [True] * len(rows))

        # ── 覆盖 3：defs 条数 == 从 gate.py docstring **现读**的条数（不许写死 9）──
        import rongke.gate as G7
        doc_nums = set()
        for ln in (G7.__doc__ or "").splitlines():
            s7 = ln.strip()
            if s7[:1].isdigit() and ". " in s7:
                doc_nums.add(int(s7.split(".", 1)[0]))
        defs = gates_j.get("defs") or []
        check(f"③ defs 条数 == docstring 现读条数（{len(doc_nums)}，不是写死的 9）",
              len(defs), len(doc_nums))
        check("③ 条号与 docstring 完全一致",
              sorted(d["n"] for d in defs), sorted(doc_nums))
        # 逐字比对按**行剥边后拼接**归一化 —— 否则比对的是缩进空格，不是文案本身。
        doc_flat = "".join(ln.strip() for ln in (G7.__doc__ or "").splitlines())
        check("③ ★ 每条文案**逐字**来自 docstring（不许自己归纳措辞）",
              ["".join(ln.strip() for ln in d["text"].split("\n")) in doc_flat for d in defs],
              [True] * len(defs))
        check("③ 分类按 S0 §5″：第 5 条 soft、其余 hard",
              [(d["n"], d["kind"]) for d in defs if d["kind"] != ("soft" if d["n"] == 5 else "hard")],
              [])
        truthy("③ 门禁关键词原样在文案里（证据/技能/写作体检/图面门/deck）",
               all(k in doc_flat for k in ("证据", "技能", "写作体检", "图面门")))
        last = gates_j.get("last") or {}
        check("③ last 指向最近一次归档任务的门判定",
              (last.get("rid"), last.get("passed")), ("figtask01", True))
        check("③ last.warnings 原样透传（软检查不吞）",
              last.get("warnings"), ["软警告一条"])

        # ── 覆盖 5：deliverables 列出的每个 url 都真能取回（含二进制）──
        urls = [(it["rid"], f["rel"], f["url"]) for it in deliv.get("items", [])
                for f in it.get("files", [])]
        check("⑤ 两个归档任务的产出物都列出（含无归档语义的正控）", len(urls), 5)
        bad = [(rid7, rel7, get_raw7(u7)) for rid7, rel7, u7 in urls if get_raw7(u7) != 200]
        check("⑤ ★ 每个 url 打 /api/artifact 都取得回（200）", bad, [])
        check("⑤ url 走的是**既有通路**的形（id=/path=，没开第二条下载道）",
              all(u7.startswith("/api/artifact?id=") and "&path=" in u7 for _, _, u7 in urls),
              True)
        check("⑤ by_kind 计数与 total 一致", deliv.get("total"), sum(
            (deliv.get("by_kind") or {}).values()))

        # ── 覆盖 6：围栏负控 —— 列出的通路没被新接口开大 ──
        st = get_raw7("/api/artifact?id=figtask01&path=../../../etc/passwd")
        check("⑥ ★ 负控：穿越 ../../../etc/passwd → 404", st, 404)
        st = get_raw7("/api/artifact?id=figtask01&path=..%2F..%2F..%2Fetc%2Fpasswd")
        check("⑥ ★ 负控：编码穿越同样 404", st, 404)
        st = get_raw7("/api/artifact?id=figtask01&path=session.json")
        check("⑥ ★ 负控：账本文件仍取不回（新接口没把围栏开大）", st, 404)
        st = get_raw7("/api/artifact?id=..%2F..%2Fconfig.json&path=x")
        check("⑥ 负控：穿越 rid 也取不回", st, 404)

        # ── figures：图与报告并排、无报告的 hint、fresh 复用内核判据 ──
        items = {it["rid"]: it for it in figs_j.get("items", [])}
        check("④ figtask01 列出", "figtask01" in items, True)
        it1 = items.get("figtask01") or {}
        check("④ 图清单（png + pdf 都算图）",
              sorted(f["rel"] for f in it1.get("figs", [])), ["fig1.png", "sub/chart.pdf"])
        check("④ 审计报告并排列出",
              [(a["rel"], a["verdict"]) for a in it1.get("audits", [])],
              [("figure-qa.json", "PASS")])
        check("④★ fresh=true：复用 FigureAuditor._freshness（报告不旧于图）",
              it1.get("fresh"), True)
        it2 = items.get("noref02") or {}
        check("④ 没有报告的图**照样列出**：audits 空 + hint",
              (it2.get("audits"), it2.get("fresh"), bool(it2.get("hint"))),
              ([], None, True))
        # fresh 判据承重：把报告改**旧**（早于被审对象 > 容差）⇒ 必须翻成 false。
        # 这不是"我自己写了个时间戳判据再自证"——变的是磁盘事实，判据是内核的 `_freshness`。
        d3 = WS7 / "stale03"
        d3.mkdir(parents=True, exist_ok=True)
        (d3 / "late.png").write_bytes(b"\x89PNG\r\n\x1a\nLATE")
        (d3 / "figure-qa.json").write_text(json.dumps(
            {"ok": True, "verdict": "PASS", "audited_at": "x"}, ensure_ascii=False),
            encoding="utf-8")
        img_m = (d3 / "late.png").stat().st_mtime
        os.utime(d3 / "figure-qa.json", (img_m - 3600, img_m - 3600))
        (d3 / "session.json").write_text(json.dumps(
            {"id": "stale03", "goal": "图比报告新", "started": 1749999998,
             "result": {"success": True}}, ensure_ascii=False), encoding="utf-8")
        st, f3 = get7("/api/figures")
        it3 = {it["rid"]: it for it in (f3 or {}).get("items", [])}.get("stale03") or {}
        check("④★ 承重正控：报告旧于图 ⇒ fresh 翻成 false（内核 QA-STALE 在拦）",
              it3.get("fresh"), False)
        truthy("④ 翻false时给出内核原文（stale_findings 透传）",
               any("QA-STALE" in m or "旧" in m for m in it3.get("stale_findings", [])))
    finally:
        httpd7.shutdown()
        httpd7.server_close()
        th7.join(timeout=5)
finally:
    S.ws_dir = _real_ws7                       # type: ignore[assignment]
    M_.default_root = _real_mem7               # type: ignore[assignment]
    S.default_root = _real_mem7                # type: ignore[assignment]  ← 与上面那处成对
    T.stash(WS7, "webtest-ws7")
    T.stash(MEM7, "webtest-mem7")
check("★ W1 夹具已挪走，真实 runs/ 与 memory/ 没被第 7 节碰过",
      (WS7.exists(), MEM7.exists()), (False, False))

# ══════════════════════ 8. 挑选入库的人因台账（W25）══════════════════════
# W23 验收报告 §10 记的账：「谁在什么时候勾了哪几篇」这层人因记录本刀没做。
# 本节测 `_append_pick_ledger`：记什么、append-only、写不进去时**如实报**不静默。
# 走真 Manager（不碰内核/网络）+ 假 project_root（账写进临时目录，不碰用户真账）。
print("\n── 8. 挑选入库的人因台账：append-only + 失败不静默 ──")
P8 = ROOT / "_webtest_pickledger"
T.fresh_dir(P8, "webtest-pickledger")
_real_pr8 = S.project_root
_real_dr8 = S.default_root
S.project_root = lambda: P8                    # type: ignore[assignment]
# ★ 记忆根的唯一算法是 `default_root()`（cfg → 环境变量 → 数据根）。
#   只替换 `project_root` 已经隔离不住它了：那条链的末档是 `data_root()`，
#   于是台账照样写进用户真目录，而 `P8/memory/kb_picks.jsonl` 根本不存在
#   —— 正控自己先红（2026-10-20 实测）。两处一起替换才叫隔离。
S.default_root = lambda cfg=None: P8 / "memory"   # type: ignore[assignment]
try:
    M8 = S.Manager()
    _rep8 = {"counts": {"fulltext": 1, "metadata": 0, "duplicate": 1, "failed": 0},
             "rows": [{"doi": "10.1/x", "kind": "fulltext", "id": "kb111"},
                      {"doi": "10.2/y", "kind": "duplicate", "id": ""}]}
    led = M8._append_pick_ledger(_rep8, ["10.1/x", "10.2/y"])
    check("① 写账成功（正控）", led["ok"], True)
    _lp = P8 / "memory" / "kb_picks.jsonl"
    check("① 落在 memory/kb_picks.jsonl（与 runs.jsonl 同一家）", _lp.exists(), True)
    _lines = _lp.read_text(encoding="utf-8").strip().splitlines()
    check("① 一行一次勾选", len(_lines), 1)
    _j = json.loads(_lines[0])
    check("① 记了勾选的 DOI 清单", _j["dois"], ["10.1/x", "10.2/y"])
    check("① 记了四类计数（duplicate 单独算，不是失败）",
          (_j["counts"]["fulltext"], _j["counts"]["duplicate"]), (1, 1))
    check("① 行内只留 doi/kind/id（标题等冗余不进账）",
          sorted(_j["rows"][0].keys()), ["doi", "id", "kind"])
    check("① 带时间戳（谁在**什么时候**勾的）", bool(_j.get("ts")), True)

    _first = _lines[0]
    M8._append_pick_ledger(_rep8, ["10.3/z"])
    _lines2 = _lp.read_text(encoding="utf-8").strip().splitlines()
    check("② 第二次勾选追加成第二行（append-only）", len(_lines2), 2)
    check("② 旧行逐字节未动（append，不许改写历史）", _lines2[0], _first)

    # 负控：落点不可写 ⇒ ok=False 且 error 有字。台账是辅助，写不进**不许拦住
    # 已完成的入库**，但也**不许装作记上了** —— 静默成功比失败更糟。
    _blocked = P8 / "blocker"
    _blocked.write_text("x", encoding="utf-8")
    S.project_root = lambda: _blocked / "sub"  # type: ignore[assignment]
    # 负控也要打到**真正决定落点**的那一处，否则它测的是另一条路
    S.default_root = lambda cfg=None: _blocked / "sub" / "memory"  # type: ignore[assignment]
    led2 = M8._append_pick_ledger(_rep8, ["10.1/x"])
    check("③ 负控：落点不可写 ⇒ ok=False（如实报）", led2["ok"], False)
    check("③ 负控：error 有内容", bool(led2.get("error")), True)
finally:
    S.project_root = _real_pr8                 # type: ignore[assignment]
    S.default_root = _real_dr8                 # type: ignore[assignment]
    T.stash(P8, "webtest-pickledger")
check("★ 台账夹具已挪走，真实 memory/ 没被第 8 节碰过",
      P8.exists(), False)

# ── 9. 客户端断开不许把服务端日志打成 traceback ──────────────────
print()
print("── 9. 客户端断开：静默；别 OSError：照旧冒 ──")
# 动机（实测）：回归是**按日志里有没有 Traceback 判**的。客户端超时先走
# 之后，`self.wfile.write` 抛 `ConnectionAbortedError`，`socketserver`
# 把整段栈打到 stderr —— 于是一次全绿的回归被判成失败。
# 只吞"对端走了"这一类；别的 OSError（磁盘满、编码错）必须照旧冒，
# 否则守卫就从"降噪"变成了"藏真问题"。两边都要有判据。
class _FakeWFile:
    def __init__(self, exc):
        self.exc = exc

    def write(self, _data):
        raise self.exc


def _send_with(exc):
    """拿真实 handler 的 `_send` 跑一遍，换掉 wfile 让它抛指定异常。"""
    h = object.__new__(S.Handler)
    h.wfile = _FakeWFile(exc)
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda *a, **k: None
    h._send(200, "{}")
    return "returned"


try:
    check("★ 正控：ConnectionAborted（WinError 10053）被静默",
          _send_with(ConnectionAbortedError(10053, "中止")), "returned")
    check("★ 正控：BrokenPipe 被静默",
          _send_with(BrokenPipeError()), "returned")
    # 负控：一个**不是**断连的 OSError 必须照样抛出去
    try:
        _send_with(OSError(28, "磁盘满"))
        check("★ 负控：非断连的 OSError 必须照旧抛（不许被一起吞掉）",
              "没有抛", "抛了")
    except OSError:
        check("★ 负控：非断连的 OSError 必须照旧抛（不许被一起吞掉）",
              "抛了", "抛了")
except Exception as e:                                # noqa: BLE001
    check(f"第 9 节冒出异常：{type(e).__name__}: {e}", False, True)

# ── 10. `probe=1` 必须廉价：只查缓存，不许真渲 ──────────────────
print()
print("── 10. /api/orig/open?probe 只查缓存，不许真渲 ──")
# 实测缺陷：这个分支的注释写着「不自作主张开始渲」，实际调的是
# `render()` —— 缓存未命中就**整份渲染**（几十页论文十几秒）。客户端
# 10 秒超时先走，服务端渲完再写响应，`ConnectionAbortedError` 打进
# 日志，回归据此判失败。判据打在**耗时**与**副作用**上，不看注释。
try:
    from rongke.web import orig as _og
    P9 = ROOT / "_webtest_origprobe"
    P9.mkdir(parents=True, exist_ok=True)
    _src = P9 / "probe.pdf"
    _src.write_bytes(b"%PDF-1.4\n%fake\n")
    _t0 = time.time()
    _r = _og.peek(_src, dpi=110)
    _dt = time.time() - _t0
    check("① 没缓存 ⇒ ok=False（不假装有）", _r.get("ok"), False)
    check("① 没缓存 ⇒ 明确标 miss（前端据此显示「开始渲染」）",
          _r.get("miss"), True)
    # ★ 判据原来写成 any(glob("*_d110")) and not(本文件产物).exists()。
    #   前半句要求"缓存里至少还有别的产物"，全空时恒假；
    #   后半句把**目录存在**当成"渲过了"，可 peek 的命中条件是
    #   `meta.json` 存在且 pages 全在 —— 光有目录是残留，不算命中
    #   （实测 ef381f48344a471d_d110 目录就在那儿，里面没有 meta.json，
    #     peek 正确地回了 miss=True，而旧判据判它"渲过了"）。
    #   两处都是判据读错了契约，不是产品缺陷。改成直接对齐 peek 的命中条件。
    _outdir0 = _og.cache_root() / ((_og._sha256(_src)[:16]) + "_d110")
    check("①★ 且**真的没渲**：缓存里没有这个文件的 **meta.json**（命中才算渲过）",
          not (_outdir0 / "meta.json").exists(),
          True)
    # 正控：造一份**真的命中**的缓存，证明这条判据有鉴别力（不是恒真）
    import json as _json
    _outdir0.mkdir(parents=True, exist_ok=True)
    (_outdir0 / "page-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (_outdir0 / "meta.json").write_text(_json.dumps(
        {"ok": True, "pages": [{"file": "page-1.png"}]}), encoding="utf-8")
    try:
        _rh = _og.peek(_src, dpi=110)
        check("①☆ 正控：缓存**真命中**时 peek 说 cached（判据不是恒真）",
              _rh.get("ok") is True and _rh.get("cached") is True, True)
        check("①☆ 且此时 meta.json 存在（与上面那条判据互为反面）",
              (_outdir0 / "meta.json").exists(), True)
    finally:
        import shutil as _sh
        _sh.rmtree(_outdir0, ignore_errors=True)
    check("②★ 返回值里带证明：**它自己说没渲**（不是靠外面猜）",
          _r.get("ok") is False and _r.get("cached") is False, True)
    check("② 快：只查缓存，耗时 < 1 秒（真渲不可能这么快）",
          _dt < 1.0, True)
    # 正控：文件真的不存在时要说出来，而不是报 miss
    _r2 = _og.peek(P9 / "no_such.pdf")
    check("③ 正控：文件不存在 ⇒ ok=False 且有 error（不当 miss 糊过去）",
          bool(_r2.get("error")), True)
    # 负控：把 `render` 换成一个"会调用就记一笔"的探针，证明 `peek`
    # **根本没碰**它。第一版负控用计时（"换成会真渲的实现 ⇒ 耗时翻倍"），
    # 打不穿 —— 我喂的是假 PDF（`%PDF-1.4\n%fake`），`render()` 解析失败
    # 立刻返回，假实现和真实现一样快，计时判据两个方向都分开不了。
    # 计量"有没有被调用"才是这件事的直接判据。
    _calls = []
    _real_render = _og.render

    def _spy(src, dpi=110, force=False):
        _calls.append(src)
        return _real_render(src, dpi=dpi, force=force)

    _og.render = _spy                                                     # type: ignore
    try:
        _og.peek(_src, dpi=110)
    finally:
        _og.render = _real_render                                          # type: ignore
    check("★ 负控/主判据：`peek` 全程**一次都没调用** `render`", _calls, [])
    # 正控：这个间谍本身能听见（否则上面那条是"测空气"）
    _og.render = _spy                                                     # type: ignore
    try:
        _og.render(_src, dpi=110)
    finally:
        _og.render = _real_render                                          # type: ignore
    check("★ 正控：间谍确实能听见 `render` 被调用（否则上一条是测空气）",
          len(_calls) >= 1, True)
except Exception as e:                                # noqa: BLE001
    check(f"第 10 节冒出异常：{type(e).__name__}: {e}", False, True)
finally:
    try:
        T.stash(ROOT / "_webtest_origprobe", "webtest-origprobe")
    except Exception:                                 # noqa: BLE001
        pass

# ── 11. 文献库：请求头、DOI 归一、原页判定 ────────────────────────────────
# 这三条都是 2026-10-29 实测踩过的：漏 Content-Type ⇒ 415；DOI 两种写法
# ⇒「库里没有」但 kb_add 说已存在；只看 pdfs/<id>.pdf ⇒ 本地 PDF 没有原页。
print("\n── 11. 文献库请求头 / DOI / 原页 ──")
try:
    _ws = (ROOT / "rongke" / "web" / "static" / "workspace.js").read_text(encoding="utf-8")
    for _ep in ("/api/kb/find", "/api/kb/pick"):
        _i = _ws.find(f'api("{_ep}"')
        _snip = _ws[_i:_i + 700] if _i >= 0 else ""
        check(f"★ {_ep} 显式带 Content-Type: application/json",
              '"Content-Type": "application/json"' in _snip, True)
    _api_i = _ws.find("async function api(p, o)")
    check("★ api() 对字符串体兜底 JSON（未来漏写头也不会 415）",
          "typeof oo.body === \"string\"" in _ws[_api_i:_api_i + 1600], True)

    check("DOI URL 归一", S.Manager._doi_key("https://doi.org/10.1038/NRMICRO1211/"),
          "10.1038/nrmicro1211")
    check("DOI doi: 前缀归一", S.Manager._doi_key("doi:10.1038/nrmicro1211"),
          "10.1038/nrmicro1211")
    check("★ 同一个 DOI 的两种外衣得到同一个键",
          S.Manager._doi_key("https://doi.org/10.1038/nrmicro1211"),
          S.Manager._doi_key("doi:10.1038/NRMICRO1211/"))

    from rongke.web import library_api as LA
    _d = Path(tempfile.mkdtemp(prefix="rk_has_pdf_"))
    try:
        _p = _d / "uploaded.pdf"
        _p.write_bytes(b"%PDF-1.4\n%fake")
        check("★ local_path 指向真实 PDF ⇒ has_pdf",
              LA._has_pdf("", "kb0001", {"local_path": str(_p)}), True)
        check("★ local_path 指向不存在的文件 ⇒ has_pdf=False",
              LA._has_pdf("", "kb0001", {"local_path": str(_d / "missing.pdf")}), False)
        (_d / "pdfs").mkdir()
        (_d / "pdfs" / "kb0001.pdf").write_bytes(b"%PDF-1.4\n%fake")
        check("★ 约定的 pdfs/<id>.pdf 仍然认",
              LA._has_pdf(str(_d), "kb0001", {}), True)
    finally:
        T.stash(_d, "webtest-haspdf")
except Exception as e:                                # noqa: BLE001
    check(f"第 11 节冒出异常：{type(e).__name__}: {e}", False, True)

print()
print("═══ 汇总 ═══")
for w in warns:
    print(f"⚠ {w}")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
