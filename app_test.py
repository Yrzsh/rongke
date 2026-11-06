# -*- coding: utf-8 -*-
"""桌面应用（`rongke/app.py` + `app_icon.py`）单测 —— 不开浏览器、不弹窗、不联网。

## 这套测试在防的四种翻车

| 判据 | 防的是 |
|---|---|
| `probe()` 对"只 accept 不回答"的 socket 必须返回 None | 拿 TCP 连通性冒充"服务就绪" —— 控制台有 SSE 长连接，端口通 ≠ 答得上话 |
| 已有实例时**不得**再起一个 server | Windows 上 `allow_reuse_address=True` 让第二个 bind 静默成功，两个内核抢同一个端口 |
| `stop_server` 只收自己起的那个 | 复用别人服务时顺手把人家杀了 |
| 图标三配色必须能在 `index.html` 的令牌里找到 | 图标和界面各画各的蓝，"同一个产品"这件事悄悄散架 |

承重自检照旧：每条正控都配一条负控，且关键分支单独短路一次证明它在承重。
"""
from __future__ import annotations

import http.server
import json
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import app_icon as AI                                       # noqa: E402
from rongke import app as A                                 # noqa: E402
from rongke.web import server as S                          # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"        期望 {want!r}，实际 {got!r}")
    return good


def section(t: str) -> None:
    print(f"\n── {t} ──")


# ── 1. 端口默认值：两处必须同值 ─────────────────────────────────────────────
section("1. 端口默认值的单源被钉住")
check("app.DEFAULT_PORT 与 server.DEFAULT_PORT 同值",
      A.DEFAULT_PORT, S.DEFAULT_PORT)
check("★ 负控：故意改一个就会红（证明上一条不是恒真）",
      A.DEFAULT_PORT == 8765 and S.DEFAULT_PORT != 1, True)
_orig_port = A.DEFAULT_PORT
A.DEFAULT_PORT = 9999
try:
    check("★ 承重自检：把 app 侧改掉 ⇒ 同值断言当场失守",
          A.DEFAULT_PORT == S.DEFAULT_PORT, False)
finally:
    A.DEFAULT_PORT = _orig_port
check("★ 还原后仍然同值", A.DEFAULT_PORT == S.DEFAULT_PORT, True)

# ── 1b. 冻结模式：同一个 exe 重入，不再 `-m` ────────────────────────────────
section("1b. 冻结模式：命令行由 exe 自己重入")
_real_frozen = A.FROZEN
try:
    A.FROZEN = True
    check("冻结版走 --serve，不再 -m",
          A.server_argv(8788)[1:], ["--serve", "--port", "8788"])
    check("★ 冻结版 pythonw 就是 exe 自己", A.pythonw(), sys.executable)
    A.FROZEN = False
    check("源码版仍走 -m rongke.web.server",
          A.server_argv(8788)[1:3], ["-m", "rongke.web.server"])
finally:
    A.FROZEN = _real_frozen

# ── 1c. --check 是只读的 ────────────────────────────────────────────────────
section("1c. --check 不落任何本机状态")
_tmp_home = Path(tempfile.mkdtemp())
_real_state, _real_report = A.STATE_DIR, A.report
try:
    A.STATE_DIR = _tmp_home / ".rongke"
    A.report = lambda port, force_edge=False: 0
    rc = A.main(["--check"])
    check("★ --check 不创建状态目录", (rc, A.STATE_DIR.exists()), (0, False))
finally:
    A.STATE_DIR, A.report = _real_state, _real_report
    shutil.rmtree(_tmp_home, ignore_errors=True)


# ── 2. 图标：结构合法 + 可重算 + 配色有源 ──────────────────────────────────
section("2. 桌面图标（派生物必须能重算）")
blob = AI.build()
sizes = [f"{w}x{h}" for w, h, _ in AI._read_sizes(blob)]
check("ICO 头合法且五档尺寸齐", sizes, ["16x16", "24x24", "32x32", "48x48", "256x256"])
check("★ 同一段代码画两次逐字节相同（不确定就没法当派生物）",
      AI.build() == blob, True)
disk = A.HERE / "web" / "app.ico"
check("★ 磁盘上那份 == 代码重算（不一致就跑 `python app_icon.py`）",
      disk.read_bytes() == blob, True)
# 256 那档必须是 PNG 内嵌（老壳读 BMP，新壳读 PNG，两者都得在）。
# 目录项布局：0..3=B,B,B,B  4=planes(H)  6=bitcount(H)  8=size(I)  12=offset(I)
_ent = 6 + 16 * 4
_size256 = struct.unpack_from("<I", blob, _ent + 8)[0]
_off256 = struct.unpack_from("<I", blob, _ent + 12)[0]
check("★ 256 档是 PNG 内嵌", blob[_off256:_off256 + 8], b"\x89PNG\r\n\x1a\n")
check("★ 且目录声明的字节数正好收到文件尾", _off256 + _size256, len(blob))
check("★ 每档都是 1 平面 32 位",
      {struct.unpack_from("<HH", blob, 6 + 16 * i + 4) for i in range(5)}, {(1, 32)})
# 2026-10-29 拆分后，设计令牌搬进了 static/tokens.css —— 只看 index.html
# 会让这条判据静默假红。两处都读，判据测的仍是「图标与界面同源」。
html = (A.HERE / "web" / "index.html").read_text(encoding="utf-8")
tokens = (A.HERE / "web" / "static" / "tokens.css").read_text(encoding="utf-8")
ui_tokens = html + "\n" + tokens
for name, rgb in (("ink 底", AI.INK), ("biz 主体", AI.BIZ), ("on-ink 高光", AI.WHITE)):
    hexv = "#" + "".join(f"{c:02x}" for c in rgb)
    check(f"  配色有源：{name} {hexv} 确实在控制台令牌里", hexv in ui_tokens, True)


# ── 3. probe()：就绪必须是"答得上话"，不是"端口通" ─────────────────────────
section("3. 就绪判据（这条最容易偷工）")


class _OnlyBuild(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802
        if self.path == "/api/build":
            body = json.dumps({"started": 123.5, "n_files": 7}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):                          # 别把测试输出弄脏
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OnlyBuild)
port = srv.server_address[1]
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()
check("正控：/api/build 答得上来 ⇒ 判为活着并带回指纹",
      A.probe(port, timeout=2.0), {"started": 123.5, "n_files": 7})
srv.shutdown()
srv.server_close()
check("★ 负控：服务停了 ⇒ 必须判为不活着（不许抛）", A.probe(port, timeout=1.0), None)

# 关键负控：只 accept 不回答 —— 这正是 SSE 长连接伪装出来的"端口通"
dark = socket.socket()
dark.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
dark.bind(("127.0.0.1", 0))
dark.listen(5)
dport = dark.getsockname()[1]
def _accept_forever(sk: socket.socket) -> None:
    """只 accept 不回答，模拟"端口通但答不上话"。关掉时安静退出，不许甩 traceback。"""
    try:
        while True:
            sk.accept()
    except OSError:
        pass


drain = threading.Thread(target=_accept_forever, args=(dark,), daemon=True)
drain.start()
t0 = time.time()
check("★ 只握手不答话（SSE 那种假就绪）⇒ probe 必须判 None",
      A.probe(dport, timeout=1.0), None)
check("★ 且是在超时窗内判出来的，不是等到的", time.time() - t0 < 3.0, True)
# 同一条 socket，probe 说"不是荣珂"，listening 说"有人" —— 两问合起来才拦得住抢端口
check("★ 但 listening 必须看得见它（本机实测：8765 被工作台的 backend 占着，"
      "probe 判 None、listening 判 True）", A.listening(dport), True)
check("★ 负控：没人听的端口 listening 判 False（不许恒真）",
      A.listening(1), False)
dark.close()

section("3b. wait_ready 的两种结局")
srv2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OnlyBuild)
p2 = srv2.server_address[1]
threading.Thread(target=srv2.serve_forever, daemon=True).start()
got = A.wait_ready(p2, deadline=5.0)
check("正控：真就绪时 wait_ready 返回那份指纹", got, {"started": 123.5, "n_files": 7})
srv2.shutdown()
srv2.server_close()
t0 = time.time()
check("★ 负控：没人应答时它必须**到期返回 None**，不许无限等",
      A.wait_ready(p2, deadline=0.6), None)
check("★ 且确实只等了约一个 deadline（不是碰运气）",
      0.5 <= time.time() - t0 < 2.5, True)


# ── 4. 浏览器与解释器定位 ───────────────────────────────────────────────────
section("4. 浏览器 / pythonw / 桌面路径")
import os                                                              # noqa: E402
_old_env = os.environ.get("RONGKE_BROWSER")
try:
    os.environ["RONGKE_BROWSER"] = str(Path(sys.executable))
    check("环境变量指哪就用哪（存在性检查通过）", A.find_browser(), sys.executable)
    os.environ["RONGKE_BROWSER"] = r"D:\不存在\msedge.exe"
    check("★ 负控：指向不存在的文件就不许用（继续往下找）",
          A.find_browser() == r"D:\不存在\msedge.exe", False)
    check("★ 本机确实找得到可用的 Edge/Chrome", bool(A.find_browser()), True)
finally:
    if _old_env is None:
        os.environ.pop("RONGKE_BROWSER", None)
    else:
        os.environ["RONGKE_BROWSER"] = _old_env

_real_exe = A.sys.executable
try:
    A.sys.executable = str(Path(r"C:\不存在的目录\python.exe"))
    check("★ 没有 pythonw 时退回自己（而不是返回一个不存在的路径）",
          A.pythonw(), str(Path(r"C:\不存在的目录\python.exe")))
finally:
    A.sys.executable = _real_exe
check("正控：本机 pythonw 存在 ⇒ 快捷方式不会带黑框",
      Path(A.pythonw()).name, "pythonw.exe")

d = A.desktop_dir()
check("桌面必须问系统要（本机桌面被重定向到 D:\\桌面，拼 `~/Desktop` 会落空）",
      d.is_dir(), True)
_real_run = A.subprocess.run
try:
    A.subprocess.run = lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "boom")
    check("★ 负控：问不到就退回 home，不返回一个不存在的路径",
          A.desktop_dir(), Path.home())
finally:
    A.subprocess.run = _real_run


# ── 4b. 窗口宿主：装得上就自己开窗，装不上必须回退 ─────────────────────────
section("4b. 窗口宿主：WebView2 可用才走内嵌，否则回退")
check("正控：本机 WebView2 Runtime 版本可读", bool(A.webview2_version()), True)
check("正控：本机 pywebview 可导入", A.import_webview() is not None, True)

_old_shell_env = os.environ.get("RONGKE_SHELL")
_real_imp, _real_ver, _real_find2 = A.import_webview, A.webview2_version, A.find_browser
try:
    check("两样都齐 ⇒ 内嵌窗口", A.shell_kind(), "webview")
    A.webview2_version = lambda: ""
    check("★ 负控：WebView2 运行时缺失 ⇒ 回退 Edge（不是硬开、也不是报错）",
          A.shell_kind(), "edge")
    A.webview2_version = lambda: "156.0"
    A.import_webview = lambda: None
    check("★ 负控：没有 pywebview ⇒ 回退 Edge", A.shell_kind(), "edge")
    A.import_webview = _real_imp
    os.environ["RONGKE_SHELL"] = "edge"
    check("环境变量可强制回退（排障用）", A.shell_kind(), "edge")
    os.environ.pop("RONGKE_SHELL", None)
    check("--edge-shell 参数可强制回退", A.shell_kind(force_edge=True), "edge")
    A.find_browser = lambda: ""
    check("★ 负控：连 Edge/Chrome 都没有 ⇒ 默认浏览器",
          A.shell_kind(force_edge=True), "browser")
finally:
    A.import_webview, A.webview2_version, A.find_browser = _real_imp, _real_ver, _real_find2
    if _old_shell_env is None:
        os.environ.pop("RONGKE_SHELL", None)
    else:
        os.environ["RONGKE_SHELL"] = _old_shell_env

section("4c. 内嵌窗口：私有模式必须关掉，否则前端偏好每次归零")
_seen_wv: dict = {}
_real_imp2, _old_wv_state = A.import_webview, A.WEBVIEW_STATE
_real_timer = A.threading.Timer
_real_find3 = A._find_window
_real_findown = A._find_own_window


class _NowTimer:
    def __init__(self, interval, fn, *a, **kw):
        self.fn = fn

    def start(self):
        self.fn()


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


class _FakeWin:
    def __init__(self):
        self.events = type("E", (), {})()
        self.events.shown = _FakeEvent()
        self.events.loaded = _FakeEvent()
        self.maximized = 0

    def maximize(self):
        self.maximized += 1


class _FakeWV:
    @staticmethod
    def create_window(title, url, **kw):
        _seen_wv.update(title=title, url=url, **kw)
        _seen_wv["win"] = _FakeWin()
        return _seen_wv["win"]

    @staticmethod
    def start(func=None, **kw):
        _seen_wv.update(start=kw, func=func)


with tempfile.TemporaryDirectory() as _td:
    try:
        A.import_webview = lambda: _FakeWV
        A.threading.Timer = _NowTimer
        A._find_own_window = lambda: 0
        A.WEBVIEW_STATE = Path(_td) / "webview"
        rc = A.open_native_window("http://127.0.0.1:9/")
        check("开窗返回 0", rc, 0)
        check("★ private_mode=False（默认 True 会把 localStorage 每次清空）",
              _seen_wv["start"].get("private_mode"), False)
        check("storage_path 落在 ~/.rongke 下（不往别处写）",
              Path(_seen_wv["start"]["storage_path"]), A.WEBVIEW_STATE)
        check("标题是产品名", _seen_wv["title"], "荣珂")
        _w = _seen_wv["win"]
        check("★ 最大化挂在 loaded 事件上（shown 时布局还没完，会被重置）",
              len(_w.events.loaded.handlers), 1)
        _w.events.loaded.handlers[0]()
        check("loaded 触发后确实点了一次最大化", _w.maximized, 1)

        # 窗口可能晚于 loaded 出现：要等，不能立刻放弃
        _real_enum, _real_pid4 = A._enum_own_window, A._hwnd_pid
        try:
            seq = [0, 0, 123]
            A._enum_own_window = lambda t: seq.pop(0) if seq else 0
            check("窗口晚出现也能等到", _real_findown(tries=4, gap=0), 123)
            A._enum_own_window = lambda t: 0
            check("★ 负控：一直不出现 ⇒ 如实返回 0",
                  _real_findown(tries=2, gap=0), 0)
        finally:
            A._enum_own_window, A._hwnd_pid = _real_enum, _real_pid4

        # 最大化要复验：发一次会被随后的布局重置（安装版首启实测）
        _real_zoom, _real_maxh = A._is_zoomed, A._maximize_hwnd
        try:
            seq = [False, False, True, True]
            A._is_zoomed = lambda h: seq.pop(0) if seq else True
            A._maximize_hwnd = lambda h: True
            check("前两次没成、第三次成 ⇒ True",
                  A.ensure_maximized(1, gap=0, settle=0), True)

            # 一次成功但随后被布局还原 ⇒ 必须重来，不能第一次 True 就收工
            seq = [True, False, True, True]
            A._is_zoomed = lambda h: seq.pop(0) if seq else True
            check("★ 最大化后被还原 ⇒ 重试并最终确认",
                  A.ensure_maximized(1, gap=0, settle=0), True)

            A._is_zoomed = lambda h: False
            check("★ 负控：一直没成 ⇒ 如实返回 False（不许谎报成功）",
                  A.ensure_maximized(1, tries=3, gap=0, settle=0), False)
        finally:
            A._is_zoomed, A._maximize_hwnd = _real_zoom, _real_maxh
    finally:
        A.import_webview, A.WEBVIEW_STATE = _real_imp2, _old_wv_state
        A.threading.Timer = _real_timer
        A._find_window = _real_find3
        A._find_own_window = _real_findown


# ── 4d. 第二次双击：切到已有窗口，不叠第二个 ───────────────────────────────
section("4d. 第二次双击：找已有窗口，不叠第二个")
_real_findw, _real_pidw, _real_act = A._find_window, A._hwnd_pid, A._activate
try:
    A._find_window = lambda title: 0
    check("没有窗口 ⇒ 不动手", A.focus_existing_window(), False)
    A._find_window = lambda title: 4242
    A._hwnd_pid = lambda hwnd: os.getpid()
    check("★ 负控：本进程自己的窗口不算「已有」", A.focus_existing_window(), False)
    A._hwnd_pid = lambda hwnd: 1
    A._activate = lambda hwnd: True
    check("★ 别的进程的荣珂窗口 ⇒ 切过去", A.focus_existing_window(), True)
finally:
    A._find_window, A._hwnd_pid, A._activate = _real_findw, _real_pidw, _real_act


# ── 5. 单实例决策：main() 到底起不起第二个 ─────────────────────────────────
section("5. 单实例：复用不起新的，起新的要负责收掉")
calls: dict = {"start": 0, "stop": 0, "window": 0, "die": 0}
_fake_proc = object()                 # 假的"Popen 句柄"，只用来让 finally 认得出"是我起的"
_real_start, _real_stop, _real_window = A.start_server, A.stop_server, A.open_window
_real_probe_main, _real_die = A.probe, A._die
_real_wait_ready, _real_listening = A.wait_ready, A.listening
_real_shell = A.shell_kind
_real_focus = A.focus_existing_window
_fake_lock = object()
_real_trylock, _real_unlock = A._try_serve_lock, A._release_serve_lock
try:
    A.start_server = (lambda port: (calls.__setitem__(
        "start", calls["start"] + 1), _fake_proc)[1])
    A.stop_server = lambda proc: calls.__setitem__("stop", calls["stop"] + 1)
    A.open_window = lambda url, b: calls.__setitem__("window", calls["window"] + 1) or 0
    A.find_browser = lambda: r"C:\fake\msedge.exe"
    # 这节测的是单实例/收摊，不是窗口宿主；固定走 Edge 分支，别真弹窗。
    A.shell_kind = lambda force_edge=False: "edge"
    A.focus_existing_window = lambda wait=0.0: False
    A._try_serve_lock = lambda: _fake_lock
    A._release_serve_lock = lambda f: None
    # `_die` 真实现会弹 MessageBoxW —— 测试里弹窗会把整套卡住，所以换成计数器。
    A._die = lambda what: calls.__setitem__("die", calls["die"] + 1)
    A.wait_ready = lambda port, deadline=25.0: {"started": 1.0}

    A.probe = lambda port, timeout=1.5: {"started": 1.0}          # 已有活的
    rc = A.main(["--port", "8799"])
    check("端口有人 ⇒ 不起第二个 server（Windows 会静默让第二个 bind 成功）",
          (rc, calls["start"], calls["window"]), (0, 0, 1))
    check("★ 且复用时不许替别人收摊", calls["stop"], 0)

    calls.update(start=0, stop=0, window=0, die=0)
    A.focus_existing_window = lambda wait=0.0: True
    rc = A.main(["--port", "8799"])
    check("★ 第二次双击 ⇒ 切到已有窗口，不叠第二个、不起服务、不收别人的摊",
          (rc, calls["start"], calls["window"], calls["stop"]), (0, 0, 0, 0))
    A.focus_existing_window = lambda wait=0.0: False

    calls.update(start=0, stop=0, window=0, die=0)
    A.probe = lambda port, timeout=1.5: None                      # 没人
    rc = A.main(["--port", "8799"])
    check("端口空闲 ⇒ 自己起一个，起完开窗", (rc, calls["start"], calls["window"]),
          (0, 1, 1))
    check("★ 自己起的就必须自己收干净", calls["stop"], 1)

    # 竞态：两个实例同时启动，都以为端口空闲。起服务必须是单入口。
    calls.update(start=0, stop=0, window=0, die=0)
    A.probe = lambda port, timeout=1.5: None
    A.listening = lambda port, timeout=0.6: False
    A._try_serve_lock = lambda: None                              # 别人拿着锁
    A.wait_ready = lambda port, deadline=30.0: {"started": 1.0}
    rc = A.main(["--port", "8799"])
    check("★ 另一个实例正在起服务 ⇒ 不跟着起第二个，等它就绪后复用",
          (rc, calls["start"], calls["window"], calls["stop"]), (0, 0, 1, 0))
    A._try_serve_lock = lambda: _fake_lock
    A.wait_ready = lambda port, deadline=25.0: {"started": 1.0}

    calls.update(start=0, stop=0, window=0, die=0)
    A.probe = lambda port, timeout=1.5: None
    A.wait_ready = lambda port, deadline=25.0: None               # 起不来
    rc = A.main(["--port", "8799"])
    check("★ 负控：起了但没就绪 ⇒ 退出码非 0、不开窗口、且**喊一声**（pythonw 下没人看得见 print）",
          (rc, calls["window"], calls["die"]), (1, 0, 1))
    check("★ 起了却没就绪 ⇒ 那个半死的子进程也要被收掉，不许留孤儿", calls["stop"], 1)

    # 启动窗口期：端口已 bind、/api/build 还没答 —— 那是另一个荣珂在启动，
    # 不是"别人占着"。安装器刚拉起一个、用户又双击时必踩。
    calls.update(start=0, stop=0, window=0, die=0)
    A.probe = lambda port, timeout=1.5: None
    A.listening = lambda port, timeout=0.6: True
    A.wait_ready = lambda port, deadline=25.0: {"started": 1.0}
    rc = A.main(["--port", "8799"])
    check("★ 端口先 bind、后 ready ⇒ 等它就绪并复用，不误判成「不是荣珂」",
          (rc, calls["start"], calls["window"], calls["die"]), (0, 0, 1, 0))
    check("★ 且等到的那个也不是自己起的 ⇒ 不收摊", calls["stop"], 0)

    # 第四态（本机实测踩出来的）：端口有人、但不是荣珂 —— 必须拒绝，不能"也起一个"
    calls.update(start=0, stop=0, window=0, die=0)
    A.probe = lambda port, timeout=1.5: None
    A.listening = lambda port, timeout=0.6: True
    A.wait_ready = lambda port, deadline=25.0: None      # 等了也不答 ⇒ 确实不是荣珂
    rc = A.main(["--port", "8765"])
    check("★ 端口被别的服务占着 ⇒ 拒绝启动、不起服务、不开窗口、且把话说清楚",
          (rc, calls["start"], calls["window"], calls["die"]), (1, 0, 0, 1))
finally:
    A.start_server, A.stop_server, A.open_window = _real_start, _real_stop, _real_window
    A.probe, A._die = _real_probe_main, _real_die
    A.wait_ready, A.listening = _real_wait_ready, _real_listening
    A.shell_kind = _real_shell
    A.focus_existing_window = _real_focus
    A._try_serve_lock, A._release_serve_lock = _real_trylock, _real_unlock

section("5b. stop_server 真能收掉一个子进程")
p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
A.stop_server(p)
check("正控：terminate 之后进程确实没了", p.poll() is not None, True)
A.stop_server(p)                                    # 再收一次不该炸
check("★ 负控：对已退出的进程重复调用不抛", p.poll() is not None, True)

section("5c. 开窗：先收掉上一轮的浏览器进程，再开窗")
# 为什么这条是承重的：同一个 user-data-dir 已有进程在跑时，新的
# `msedge --app=... --start-fullscreen` 只是把"开个窗口"交给老进程，自己退出，
# 而 --start-fullscreen 被丢掉 ⇒ 开出来是带系统标题栏的普通窗口（右上角 − ⤢ ✕）。
order: list[str] = []
_seen: dict = {}
_real_close, _real_popen = A.close_stale_window, A.subprocess.Popen


class _Win:
    def wait(self):
        return 0


def _fake_popen(argv, **kw):
    order.append("open")
    _seen["argv"] = argv
    return _Win()


try:
    A.close_stale_window = lambda profile, **kw: order.append("close") or False
    A.subprocess.Popen = _fake_popen
    A.open_window("http://127.0.0.1:9/", r"C:\fake\msedge.exe")
    check("先收旧进程、再开窗口", order, ["close", "open"])
    check("★ 参数里 --app 与 --start-fullscreen 都在（少一个就没有无边框窗口）",
          (any(x.startswith("--app=") for x in _seen["argv"]),
           "--start-fullscreen" in _seen["argv"]), (True, True))
finally:
    A.close_stale_window, A.subprocess.Popen = _real_close, _real_popen

check("★ 按命令行里的 profile 认进程，不按进程名（否则会误杀用户自己的浏览器）",
      A._profile_pids(Path(r"C:\不存在的profile\rk-none")), [])

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
