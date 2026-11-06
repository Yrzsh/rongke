# -*- coding: utf-8 -*-
"""荣珂桌面应用 —— 双击开一个自己的窗口，不露终端。

    python -m rongke.app                    # 起控制台 + 开独立窗口，关窗即收摊
    python -m rongke.app --check            # 只体检：解释器/浏览器/端口/桌面在哪
    python -m rongke.app --install-shortcut # 在桌面建「荣珂」快捷方式（带图标）
    python -m rongke.app --no-window        # 只起服务，不开窗口（当后台用）

## 窗口宿主：两代，按可用性自动选

第一代是 Edge `--app`：零新增依赖，但任务栏里是浏览器、进程是 `msedge.exe`、
`~/.rongke/app-profile` 下多一份浏览器数据 —— 用户能看出"这是个网页"。

第二代是内嵌 WebView2（pywebview）：`rongke.exe` 自己就是窗口宿主，任务栏图标
是荣珂，没有浏览器进程和 profile 目录。**它是唯一需要运行时第三方依赖的地方**，
所以只在装得上时启用：pywebview 可导入 **且** WebView2 Runtime 已安装才走它，
否则自动回退 Edge `--app`（`shell_kind()`）。`RONGKE_SHELL=edge` 可强制回退，
用于对照排查。源码版不装 pywebview 也照常跑 —— 零依赖仍是源码安装的默认路径。

**回退必须发生在开窗之前**：pywebview 在 `start()` 里才发现运行时缺失，
那时服务已起、窗口没有，用户看到的是"双击没反应"。

## 四件必须做对的事（每一件都有实测理由）

* **就绪不能靠"端口能连"**。`/api/stream` 是 SSE，未 done 就 `wait(20s)` 挂着
  （`web/server.py:_stream`），TCP 握手通 ≠ 服务能答话。所以探 `/api/build`：
  它不碰内核、返回代码指纹，答得上来才算起来了。
* **绝不能起第二个实例**。`Server.allow_reuse_address = True`，而 Windows 上
  `SO_REUSEADDR` 的语义是**允许**第二个 socket 绑同一端口 —— 重复起不会报错，
  只会静默把请求接到另一个内核上。所以先看有没有活的，有就复用。
* **pythonw 下失败必须看得见**。快捷方式指向 `pythonw.exe`，那里 `print` 无处可去，
  崩了就是"双击没反应"。所以：日志一律写 `~/.rongke/app.log`，
  致命错误额外弹一个 `MessageBoxW`（ctypes 是标准库）。
* **桌面路径不能猜**。桌面目录是可能被系统重定向到别的盘的，
  用 `~/Desktop` 建出来的快捷方式会落在一个不存在的目录里。
  所以问 `Shell COM`/`[Environment]::GetFolderPath('Desktop')` 要真实路径。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                       # 仓库根：`-m rongke.app` 的工作目录

#: PyInstaller 冻结后 __file__ 落在 _MEIPASS 里，sys.executable 才是 exe 自身。
#: 两者的区别决定了「再起一个进程」到底该用什么命令行。
FROZEN = bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """代码/工作根：源码树是仓库根，冻结版是 exe 所在目录。"""
    return Path(sys.executable).resolve().parent if FROZEN else HERE.parent

#: 控制台默认端口。真正的单源是 `web/server.py:DEFAULT_PORT`；这里没有 import 它，
#: 因为启动器不该为了拿一个数字就把整个服务端（连带内核装载）拖进来。
#: 于是这条重复由 `app_test.py` **钉住**：两个值一旦分叉，回归当场红。
DEFAULT_PORT = 8765
STATE_DIR = Path.home() / ".rongke"      # 与配置文件同一个家
LOG = STATE_DIR / "app.log"
SERVER_LOG = STATE_DIR / "app-server.log"
PROFILE = STATE_DIR / "app-profile"      # Chromium 的 user-data-dir：独立身份靠它
WEBVIEW_STATE = STATE_DIR / "webview"    # 内嵌窗口的 localStorage / 缓存

#: WebView2 Runtime 的注册表客户端 ID（微软固定值，per-machine 与 per-user 各一份）。
_WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── 探测：把"活着"定义成一个可判定的问题 ────────────────────────────────────
def probe(port: int = DEFAULT_PORT, timeout: float = 1.5) -> dict | None:
    """问 `/api/build` 要一份代码指纹。答得上来 ⇒ 有一个控制台活着；否则 None。

    刻意不用 `socket.connect`：SSE 那条长连接会让"端口通"和"服务就绪"两件事
    在观测上长得一模一样，而它们的治法相反。
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{int(port)}/api/build", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def listening(port: int, timeout: float = 0.6) -> bool:
    """端口上**有没有人**在听。

    和 `probe()` 分工不同，缺一不可：`probe` 回答"是不是荣珂"，`listening` 回答
    "有没有别人"。本机实测：8765 上跑着一个**不是荣珂**的服务，它对 `/api/build`
    回 404 ⇒ `probe()` 判 None ⇒ 只看 probe 就会去第二个 bind，
    而 `allow_reuse_address=True` 会让它**静默成功** —— 两个内核抢同一个端口，
    请求随机分流，是查起来最贵的那种错。
    """
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def wait_ready(port: int, deadline: float = 25.0) -> dict | None:
    t0 = time.time()
    while time.time() - t0 < deadline:
        got = probe(port, timeout=1.0)
        if got:
            return got
        time.sleep(0.15)
    return None


# ── 内嵌窗口：荣珂自己当宿主 ────────────────────────────────────────────────
def _version_key(name: str) -> tuple:
    return tuple(int(x) for x in name.split(".") if x.isdigit())


def webview2_version() -> str:
    """WebView2 Runtime 的版本号；没装返回空串。

    先查注册表（权威），再查安装目录（注册表被清理过时兜底）。
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE,
             rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
            (winreg.HKEY_LOCAL_MACHINE,
             rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
            (winreg.HKEY_CURRENT_USER,
             rf"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}")):
        try:
            with winreg.OpenKey(root, sub) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if str(pv).strip() not in ("", "0.0.0.0"):
                    return str(pv)
        except OSError:
            continue
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not base:
            continue
        d = Path(base) / "Microsoft" / "EdgeWebView" / "Application"
        if not d.is_dir():
            continue
        vers = [p.name for p in d.iterdir() if p.is_dir() and _version_key(p.name)]
        if vers:
            return max(vers, key=_version_key)
    return ""


def import_webview():
    """有 pywebview 就返回模块，没有就 None —— 源码版不装它不许报错。"""
    try:
        import webview
        return webview
    except Exception:                              # noqa: BLE001
        return None


#: 窗口标题：任务栏显示的名字，也是"找已有窗口"的唯一锚点。
WINDOW_TITLE = "荣珂"


def _find_window(title: str) -> int:
    """按标题找顶层窗口；找不到或非 Windows 返回 0。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowW.restype = wintypes.HWND
        return int(u.FindWindowW(None, title) or 0)
    except Exception:                              # noqa: BLE001
        return 0


def _maximize_hwnd(hwnd: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow(hwnd, 3)                      # SW_MAXIMIZE
        return True
    except Exception:                              # noqa: BLE001
        return False


def _is_zoomed(hwnd: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.IsZoomed.restype = wintypes.BOOL
        return bool(u.IsZoomed(hwnd))
    except Exception:                              # noqa: BLE001
        return False


def ensure_maximized(hwnd: int, tries: int = 4, gap: float = 0.4,
                     settle: float = 1.2) -> bool:
    """把窗口点成最大化，并确认过一会儿它还在最大化。

    ★ 发一次不够：安装版首启时窗口刚 loaded，随后 WebView2/pywebview 的一次
      布局会把 SW_MAXIMIZE 抹掉 —— 日志写着"已最大化"，窗口却回到 1280x860。
      所以每次发完先看 IsZoomed，再等 settle 秒复验一次，被还原就重来。
    """
    for _ in range(tries):
        _maximize_hwnd(hwnd)
        time.sleep(gap)
        if _is_zoomed(hwnd):
            time.sleep(settle)
            if _is_zoomed(hwnd):
                return True
    return False


def _enum_own_window(title: str) -> int:
    """枚举**本进程**的可见窗口，返回标题匹配的第一个句柄。

    ★ 不能用 FindWindowW 按标题找：多实例时它会返回别人的窗口，
      PID 校验必然失败，本实例就永远找不到自己的窗口（最大化失败）。
    """
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        found: list[int] = []
        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _):
            if not u.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != os.getpid():
                return True
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                found.append(int(hwnd))
                return False
            return True

        u.EnumWindows(proc_type(_cb), 0)
        return found[0] if found else 0
    except Exception:                              # noqa: BLE001
        return 0


def _find_own_window(tries: int = 30, gap: float = 0.5) -> int:
    """等本进程的窗口出现并返回句柄；等不到返回 0。

    ★ 首次 loaded 时窗口不一定已经创建（WebView2 在后台先导航），
      实测源码版要 13 秒才出现 —— 轮询上限给到 15 秒。
    """
    for _ in range(tries):
        hwnd = _enum_own_window(WINDOW_TITLE)
        if hwnd:
            return hwnd
        time.sleep(gap)
    return 0


def _activate(hwnd: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        # ★ 一次 ShowWindow 都不发：SW_RESTORE 会把最大化窗口还原、
        #   SW_SHOW 在真实激活流程里也观察到过还原。SetForegroundWindow
        #   自己就能把最小化窗口恢复并置前，尺寸不受影响。
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:                              # noqa: BLE001
        return False


def focus_existing_window(wait: float = 0.0) -> bool:
    """已有荣珂窗口就拉到前台并返回 True。

    wait > 0 时轮询等待：另一个实例可能服务已起、窗口还没创建完
    （实测窗口要 13 秒才出现），这时不该自己再开一个。
    """
    deadline = time.time() + max(0.0, wait)
    while True:
        hwnd = _find_window(WINDOW_TITLE)
        if hwnd and _hwnd_pid(hwnd) != os.getpid():
            return _activate(hwnd)
        if time.time() >= deadline:
            return False
        time.sleep(0.4)


def _hwnd_pid(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                           ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _try_serve_lock():
    """拿「起服务」的独占锁；拿不到返回 None（另一个实例正在起）。

    ★ 两个实例同时启动时，probe 可能都在对方 bind 之前失败，于是都想起服务。
      Windows 的 SO_REUSEADDR 会让第二个 bind 静默成功 —— 两个内核抢一个端口，
      请求随机分流。这把文件锁把"起服务"变成单入口。
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = None
    try:
        f = open(STATE_DIR / "serve.lock", "a+b")
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        return None


def _release_serve_lock(f) -> None:
    if f is None:
        return
    try:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        f.close()
    except OSError:
        pass


def shell_kind(force_edge: bool = False) -> str:
    """选窗口宿主：'webview'（自有窗口）/ 'edge'（Edge --app）/ 'browser'（默认浏览器）。

    判定放在开窗**之前**，因为 pywebview 探不到运行时是在 `start()` 里抛 ——
    那时服务已起、窗口没有，就是"双击没反应"。
    """
    want = os.environ.get("RONGKE_SHELL", "").strip().lower()
    if not force_edge and want != "edge":
        if import_webview() is not None and webview2_version():
            return "webview"
    return "edge" if find_browser() else "browser"


def open_native_window(url: str) -> int:
    """起内嵌 WebView2 窗口，阻塞到关窗。返回 0。

    `private_mode=False` + `storage_path` 是必须的：默认私有模式会把 localStorage
    清掉，前端存的侧栏开合、主题这些偏好每次重启都归零。
    """
    wv = import_webview()
    if wv is None:
        raise RuntimeError("pywebview 不可用")
    WEBVIEW_STATE.mkdir(parents=True, exist_ok=True)
    icon = HERE / "web" / "app.ico"
    WIN_W, WIN_H = 1280, 860
    win = wv.create_window(WINDOW_TITLE, url, width=WIN_W, height=WIN_H,
                           min_size=(960, 600), background_color="#FFFFFF")

    def _maximize() -> None:
        # pywebview 6.2.1 的 maximize() 在 WinForms 后端只设 WindowState：
        # 实测 IsZoomed=True 但窗口矩形仍是 1280x860（随后一次布局把它重置了）。
        # 直接给窗口发 SW_MAXIMIZE，并按 IsZoomed 复验重试。
        hwnd = _find_own_window()
        if hwnd and ensure_maximized(hwnd):
            _log_line("内嵌窗口：已最大化")
            return
        _log_line("内嵌窗口：未找到自己的窗口或最大化未确认，退回 pywebview maximize")
        try:
            win.maximize()
        except Exception as e:                     # noqa: BLE001
            _log_line(f"内嵌窗口：最大化失败 {type(e).__name__}: {e}")

    def _arm() -> None:
        _log_line("内嵌窗口：loaded 事件到达")
        # 2 秒后再点：刚 loaded 就点会被随后的一次布局还原（安装版实测）。
        threading.Timer(2.0, _maximize).start()

    win.events.loaded += _arm
    wv.start(icon=str(icon) if icon.exists() else None,
             private_mode=False, storage_path=str(WEBVIEW_STATE))
    return 0


# ── 浏览器：app 模式的窗口来源 ──────────────────────────────────────────────
def find_browser() -> str:
    """按 环境变量 → 标准安装路径 → PATH 的顺序找一个能 `--app` 的浏览器。

    只认 Edge / Chrome 这一族：`--app=` 是它们的私有开关，换成别的就没有独立窗口。
    """
    env = os.environ.get("RONGKE_BROWSER", "").strip()
    if env and Path(env).exists():
        return env
    pf = [os.environ.get(k, "") for k in
          ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
    cands = [Path(b).joinpath(*sub) for b in pf if b for sub in (
        ("Microsoft", "Edge", "Application", "msedge.exe"),
        ("Google", "Chrome", "Application", "chrome.exe"))]
    for c in cands:
        if c.exists():
            return str(c)
    for name in ("msedge", "chrome", "microsoft-edge"):
        w = shutil.which(name)
        if w:
            return w
    return ""


def _profile_pids(profile: Path) -> list[int]:
    """正在用我们这个 profile 的浏览器**主进程**。

    为什么不按进程名杀：所有 Edge 窗口都叫 msedge.exe，按名字杀会连用户自己的
    浏览器一起杀掉。只有命令行里带着我们这个 profile 目录的才是荣珂的窗口。
    """
    ps = ("$p='" + str(profile) + "';"
          "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
          "Where-Object { $_.CommandLine -like ('*' + $p + '*') "
          "-and $_.CommandLine -notlike '*--type=*' } | "
          "ForEach-Object { $_.ProcessId }")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, creationflags=_NO_WINDOW)
    return [int(x) for x in (r.stdout or "").split() if x.isdigit()]


def close_stale_window(profile: Path, timeout: float = 8.0) -> bool:
    """收掉上一轮的浏览器进程，返回"是否真的收了东西"。

    ★ 这是**必须**的，不是清理癖：Chromium 发现同一个 user-data-dir 已经有进程在跑时，
      新的 `msedge --app=... --start-fullscreen` 只是把"开个窗口"这个命令交给老进程，
      自己立刻退出 —— 而 **--start-fullscreen 被丢掉**，开出来就是一个带系统标题栏
      （右上角 − ⤢ ✕）的普通窗口。实测：先用 --no-window 起了服务、再双击快捷方式，
      或者窗口还在任务栏里时再开一次，都会踩到。
      只有让新进程当"第一个"，全屏参数才生效，窗口才是无边框的。
    """
    pids = _profile_pids(profile)
    if not pids:
        return False
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=_NO_WINDOW)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not _profile_pids(profile):
            return True
        time.sleep(0.2)
    return True


def open_window(url: str, browser: str) -> int:
    """开一个 app 模式窗口并等它关掉，返回浏览器进程的退出码。

    ★ 这里**不能**带 `CREATE_NO_WINDOW`。实测：带上之后窗口以**最小化**出现 ——
      那个标志会给子进程的 STARTUPINFO 塞 `SW_HIDE`，Chromium 拿它当首窗显示状态，
      于是"双击没反应"（用户只会再双击一次，就又多一个实例在等端口）。
      浏览器本来就是 GUI 程序，没有控制台可藏，不需要这个标志。
      需要它的是服务端的 `python.exe`（控制台子系统），见 `start_server`。
    """
    PROFILE.mkdir(parents=True, exist_ok=True)
    close_stale_window(PROFILE)
    return subprocess.Popen(
        [browser, f"--app={url}", f"--user-data-dir={PROFILE}",
         "--start-fullscreen", "--no-first-run", "--no-default-browser-check"],
    ).wait()


# ── 控制台服务：复用现成入口，不在这里另装一套 Manager ──────────────────────
def server_argv(port: int) -> list[str]:
    """起控制台的命令行。

    冻结版**不能**再 `-m rongke.web.server`：那时 sys.executable 是 exe 自己，
    `-m` 会变成"再启动一次自己"并回到桌面分支。改成隐藏开关 `--serve`，
    由同一个 exe 在子进程里前台跑 web server。
    """
    if FROZEN:
        return [sys.executable, "--serve", "--port", str(int(port))]
    return [sys.executable, "-m", "rongke.web.server", "--port", str(int(port))]


def start_server(port: int) -> subprocess.Popen:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        server_argv(port), cwd=str(app_root()), stdout=open(SERVER_LOG, "ab"),
        stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)


def stop_server(proc: subprocess.Popen) -> None:
    """只收**自己起的那个**。复用来的实例一律留着 —— 那是别人的进程。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ── 桌面快捷方式 ────────────────────────────────────────────────────────────
def pythonw() -> str:
    """无控制台的解释器。没有它就只能退回 `python.exe`，会带一个黑框。

    冻结版本身就是 GUI 子系统的 exe，不需要（也不存在）pythonw.exe。
    """
    if FROZEN:
        return sys.executable
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w) if w.exists() else sys.executable


def desktop_dir() -> Path:
    """问系统要桌面，别拼路径 —— 桌面目录可能被系统重定向到别的盘。"""
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, creationflags=_NO_WINDOW)
    got = (out.stdout or "").strip()
    return Path(got) if out.returncode == 0 and got and Path(got).is_dir() \
        else Path.home()


def install_shortcut(port: int) -> tuple[Path, bool]:
    """建（或修）桌面快捷方式。返回 (路径, 是否成功)。

    五个值一律走**环境变量**传给 PowerShell：命令行里拼中文路径 + 带空格的
    参数，转义迟早出错；`$env:` 没有引号问题，也不会有注入面。
    """
    lnk = desktop_dir() / "荣珂.lnk"
    if FROZEN:
        target, args, icon = sys.executable, f"--port {int(port)}", Path(sys.executable)
        cwd = app_root()
    else:
        target = pythonw()
        args = f"-m rongke.app --port {int(port)}"
        icon = HERE / "web" / "app.ico"
        cwd = app_root()
    env = os.environ.copy()
    env.update({
        "RK_LNK": str(lnk),
        "RK_EXE": str(target),
        "RK_ARGS": args,
        "RK_CWD": str(cwd),
        "RK_ICON": str(icon) if icon.exists() else str(icon.parent / "app.ico"),
        "RK_DESC": "荣珂 · 科研 agent 控制台",
    })
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:RK_LNK);"
          "$s.TargetPath=$env:RK_EXE;$s.Arguments=$env:RK_ARGS;"
          "$s.WorkingDirectory=$env:RK_CWD;$s.IconLocation=$env:RK_ICON;"
          "$s.Description=$env:RK_DESC;$s.WindowStyle=1;$s.Save()")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       env=env, capture_output=True, text=True,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip()[:300]
                           or f"PowerShell 退出码 {r.returncode}")
    return lnk, lnk.exists()


# ── 体检：把"我凭什么说能用"摊开 ────────────────────────────────────────────
def report(port: int, force_edge: bool = False) -> int:
    b = find_browser()
    live = probe(port)
    if FROZEN:
        print("  形态      ", "安装版（内置解释器，不需要本机 Python）")
    kind = shell_kind(force_edge)
    if kind == "webview":
        host = f"内嵌 WebView2 {webview2_version()}（荣珂自己的窗口）"
    elif force_edge or os.environ.get("RONGKE_SHELL", "").strip().lower() == "edge":
        host = "Edge --app（被 RONGKE_SHELL / --edge-shell 强制）"
    else:
        host = "Edge --app（pywebview 或 WebView2 运行时不可用）"
    print("  窗口宿主  ", host)
    print("  解释器    ", sys.executable)
    print("  pythonw   ", pythonw(), "" if Path(pythonw()).name == "pythonw.exe"
          else "  ⚠ 没找到，快捷方式会带黑框")
    print("  浏览器    ", b or "  ⚠ 没找到 Edge/Chrome ⇒ 只能退回普通标签页")
    state = ("荣珂在跑（会复用，不再起第二个）" if live
             else "被别的服务占用（会拒绝启动，不起第二个抢它）" if listening(port)
             else "空闲（将新起一个）")
    print("  端口      ", f"{port} →", state)
    if live:
        print("  在跑的代码", json.dumps({k: live[k] for k in sorted(live)[:4]},
                                         ensure_ascii=False)[:160])
    print("  桌面      ", desktop_dir())
    print("  日志      ", LOG)
    return 0


def _ensure_stdio() -> None:
    """冻结版的 GUI 子系统默认把 sys.stdout/stderr 设成 None。

    有父进程给的句柄就接上（`--check` 被重定向时仍能输出），
    没有就丢进 devnull —— 绝不让一次 print 把启动流程打成退出码 1。
    """
    for name, fd in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name) is None:
            try:
                stream = open(fd, "w", encoding="utf-8", errors="replace",
                              closefd=False)
            except OSError:
                stream = open(os.devnull, "w", encoding="utf-8")
            setattr(sys, name, stream)
        # 冻结版可能沿用系统 GBK 控制台；`⚠`/`→` 这类字符编不进去会把
        # 一次无害的体检变成退出码 1。换成替换字符，别让输出编码决定成败。
        try:
            getattr(sys, name).reconfigure(errors="replace")
        except Exception:                              # noqa: BLE001
            pass


def _log_line(msg: str) -> None:
    """启动期的一次性事件写进 app.log。不建目录以外的副作用，失败也不许抛。"""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except OSError:
        pass


def _die(what: str) -> None:
    """pythonw 下 print 没人看得见，所以致命错误必须弹窗。"""
    msg = f"荣珂启动失败：{what}\n\n详见 {LOG}"
    try:
        print(msg)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "荣珂", 0x10)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _ensure_stdio()
    ap = argparse.ArgumentParser(prog="rongke.app", description="荣珂桌面应用")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-window", action="store_true", help="只起服务，不开窗口")
    ap.add_argument("--edge-shell", action="store_true",
                    help="强制用 Edge --app 当窗口（对照排查用）")
    ap.add_argument("--new-window", action="store_true",
                    help="已有实例在跑时也再开一个窗口（默认切到已有窗口）")
    ap.add_argument("--check", action="store_true", help="只体检，不启动")
    ap.add_argument("--install-shortcut", action="store_true",
                    help="在桌面建「荣珂」快捷方式")
    # 内部开关：冻结版由父进程用同一个 exe 重入，前台跑 web server。
    ap.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"rongke {__version__}")
    a = ap.parse_args(argv)
    url = f"http://127.0.0.1:{a.port}/?side=open"

    if a.check:
        # ★ 只读体检：在 --check 分支之前不许建状态目录。
        return report(a.port, a.edge_shell)
    if a.install_shortcut:
        lnk, ok = install_shortcut(a.port)
        print(f"  [{'PASS' if ok else 'FAIL'}] 桌面快捷方式 {lnk}")
        return 0 if ok else 1
    if a.serve:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # 服务子进程的输出去 app-server.log（不管父进程有没有给句柄）。
        try:
            _log = open(SERVER_LOG, "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = _log
        except OSError:
            pass
        from .web.server import main as web_main
        try:
            return web_main(["--port", str(a.port)])
        except BaseException:                          # noqa: BLE001
            # 子进程弹 MessageBox 会藏在窗口后面；把栈写进日志，退出码交给父进程。
            try:
                traceback.print_exc()
            except Exception:                          # noqa: BLE001
                pass
            return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    proc = None
    serve_lock = None
    try:
        live = probe(a.port)
        if not live and listening(a.port):
            # ★ 端口有人但还拿不到指纹：另一个荣珂可能正在启动（服务先 bind，
            #   过几秒 /api/build 才答得上）。安装器刚拉起一个、用户又双击时必踩。
            #   先等它就绪，等不到才判"不是荣珂"。
            live = wait_ready(a.port, deadline=20.0)
        if live:
            if not a.new_window and focus_existing_window(8.0):
                print("  已有荣珂窗口，已切到前台")
                return 0
            print(f"  复用在跑的控制台：{url}")
        elif listening(a.port):
            _die(f"端口 {a.port} 上有人应答，但那**不是荣珂**（拿不到 /api/build 指纹）。\n\n"
                 f"Windows 上重复 bind 会静默成功，两个内核会抢同一个端口的请求 ——\n"
                 f"所以这里拒绝启动，而不是「也起一个」。\n"
                 f"换一个端口试试：python -m rongke.app --port 8788")
            return 1
        else:
            # ★ 起服务是单入口：另一个实例正在起时，这里绝不跟着起第二个。
            serve_lock = _try_serve_lock()
            if serve_lock is None:
                live = wait_ready(a.port, deadline=30.0)
                if live is None:
                    _die("另一个荣珂实例正在启动，但 30 秒内没就绪。\n\n"
                         "可能是它启动失败，稍后再试；或换端口：--port 8788")
                    return 1
                if not a.new_window and focus_existing_window(15.0):
                    print("  已有荣珂窗口，已切到前台")
                    return 0
                print(f"  复用在跑的控制台：{url}")
            else:
                proc = start_server(a.port)
                if not wait_ready(a.port):
                    tail = ""
                    try:
                        tail = Path(SERVER_LOG).read_text(
                            encoding="utf-8", errors="replace")[-600:]
                    except OSError:
                        pass
                    _die(f"控制台在 {a.port} 端口没起来。\n\n服务日志尾部：\n{tail}")
                    return 1
                print(f"  控制台已就绪：{url}")
        if a.no_window:
            print("  --no-window：服务留在前台，Ctrl+C 结束。")
            while proc is not None and proc.poll() is None:
                time.sleep(1.0)
            return 0
        shell = shell_kind(a.edge_shell)
        if shell == "webview":
            print("  窗口：内嵌 WebView2（荣珂自己的窗口，关掉即收摊）")
            try:
                return open_native_window(url)
            except Exception as e:                 # noqa: BLE001
                _log_line(f"内嵌窗口失败，回退 Edge：{type(e).__name__}: {e}")
                print(f"  ⚠ 内嵌窗口起不来（{type(e).__name__}），回退 Edge --app")
                shell = "edge" if find_browser() else "browser"
        if shell == "browser":
            import webbrowser
            webbrowser.open(url)
            _die("没找到 Edge/Chrome，已用默认浏览器打开标签页"
                 "（不是独立窗口，没有自己的图标）")
            return 1
        browser = find_browser()
        print(f"  窗口：{Path(browser).name} --app  （关掉窗口即收摊）")
        open_window(url, browser)
        return 0
    except BaseException as e:                       # noqa: BLE001
        _die(f"{type(e).__name__}: {e}")
        try:
            LOG.write_text(f"{time.strftime('%F %T')} {type(e).__name__}: {e}\n",
                           encoding="utf-8")
        except OSError:
            pass
        return 1
    finally:
        if proc is not None:
            stop_server(proc)
        _release_serve_lock(serve_lock)


if __name__ == "__main__":
    sys.exit(main())
