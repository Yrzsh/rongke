"""空态聚焦输入框时，hero 标题不许被「跟随底部」推出视口（真浏览器判据）。

背景（2026-11-06 用户实报）：「我在输入框里输入字符，『你想让荣珂帮你完成什么』
会一直往上跑」。实测机制：空态 `#v-chat` 占满 852px，sticky 的 `#composer`
又占 248px ⇒ `#scroll` 可滚 248px；而 `FOLLOWING` 初值 true，一聚焦输入框
（pointerdown → rAF）`followBottom()` 就把 `scrollTop` 0→248，配合
`scroll-behavior:smooth`，标题看起来就是「慢慢往上飘」。

跟随底部只在**有对话流**时成立；空态没有对话，滚到底纯属误触发。
判据打在**渲染后的几何**上，且两个方向都量：
  ① 负控（真缺陷）：空态点击 + 逐字输入 + 删字，`scrollTop` 保持 0、h2.top 不动
  ② 正控（别把跟随功能一刀切废）：把 `#empty` 藏掉、塞一段高内容后
     `followBottom(true)` 仍应真的滚到底
静态源码检查量不出这件事 —— 页面在浏览器里到底滚没滚，只有量 DOM 才知道。

用法：`python hero_scroll_test.py`（自己起控制台 / 复用 RONGKE_WEB 热台）
      `python hero_scroll_test.py --port 8912`（连已启动的）
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHROME = Path(os.path.expanduser(r"~\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"))
_fail = 0
_n = 0


def ok(m: str) -> None:
    global _n
    _n += 1
    print(f"  [PASS] {m}")


def bad(m: str) -> None:
    global _fail, _n
    _fail += 1
    _n += 1
    print(f"  [FAIL] {m}")


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _wait_http(port: int, timeout: float = 60.0) -> bool:
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


SNAP = """() => {
  const s = document.querySelector('#scroll');
  const h = document.querySelector('.herohead h2');
  const e = document.querySelector('#empty');
  return { st: Math.round(s ? s.scrollTop : -1),
           sh: Math.round(s ? s.scrollHeight : -1),
           ch: Math.round(s ? s.clientHeight : -1),
           h2: Math.round(h ? h.getBoundingClientRect().top : -1),
           emptyHidden: !!(e && e.hidden) };
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0,
                    help="连已启动的控制台；不给就复用热台 / 自己起一个")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("没有 playwright，跳过（这条判据只在本机装了 playwright 时才有意义）")
        return 0

    proc = None
    port = args.port
    if not port:
        _m = re.search(r":(\d+)\s*$", os.environ.get("RONGKE_WEB", ""))
        if _m:
            port = int(_m.group(1))
            print(f"  info: 复用热控制台 RONGKE_WEB → port {port}")
    if not port:
        print("  info: 自起冷控制台（RONGKE_WEB 没给）")
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "rongke", "--web", "--port", str(port)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_http(port):
            bad(f"控制台没起来（端口 {port}）")
            proc.kill()
            return 1

    base = f"http://127.0.0.1:{port}/"
    errs: list[str] = []
    try:
        with sync_playwright() as p:
            kw = {"headless": True, "args": ["--no-sandbox"]}
            if CHROME.exists():
                kw["executable_path"] = str(CHROME)
            b = p.chromium.launch(**kw)
            pg = b.new_page(viewport={"width": 1440, "height": 900})
            pg.on("pageerror", lambda e: errs.append(str(e)))
            # 环境里的 HTTP_PROXY 会拦 127.0.0.1；绕开
            pg.goto(base, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(1800)

            before = pg.evaluate(SNAP)
            if before["h2"] > 0 and before["st"] == 0:
                ok(f"空态初始：h2.top={before['h2']} / scrollTop=0（标题在视口内）")
            else:
                bad(f"空态初始状态不对：{before}")

            # ① 真输入：点输入框 + 逐字打 + 删字，标题都不许跑
            goal = pg.locator("#goal")
            if not goal.count():
                bad("页面上没有 #goal 输入框")
                b.close()
                return 1
            goal.click()
            pg.keyboard.type("帮我写一篇关于冬虫夏草人工培育的综述论文", delay=0)
            pg.wait_for_timeout(700)          # 等 smooth 滚动动画走完
            typed = pg.evaluate(SNAP)
            if typed["st"] == 0 and abs(typed["h2"] - before["h2"]) <= 2:
                ok(f"输入 {len('帮我写一篇关于冬虫夏草人工培育的综述论文')} 字后："
                   f"scrollTop=0 / h2.top={typed['h2']}（没上移）")
            else:
                bad(f"输入后标题被推走：scrollTop {before['st']}→{typed['st']} / "
                    f"h2.top {before['h2']}→{typed['h2']}")

            for _ in range(6):
                pg.keyboard.press("Backspace")
            pg.wait_for_timeout(500)
            deleted = pg.evaluate(SNAP)
            if deleted["st"] == 0 and abs(deleted["h2"] - before["h2"]) <= 2:
                ok("删字后同样不上移（与 11-01 修过的同族跳动一起守住）")
            else:
                bad(f"删字后标题又动了：{deleted}")

            # ② 正控：会话态的「跟随底部」必须仍然能滚 —— 别把功能一刀切废
            pg.evaluate("""() => {
              const e = document.querySelector('#empty'); if (e) e.hidden = true;
              const t = document.querySelector('#thread');
              if (t) { t.hidden = false; t.innerHTML = '<div style="height:2000px"></div>'; }
              const s = document.querySelector('#scroll'); if (s) s.scrollTop = 0;
            }""")
            pg.wait_for_timeout(250)
            live_before = pg.evaluate(SNAP)
            try:
                pg.evaluate("() => followBottom(true)")
            except Exception as e:                            # noqa: BLE001
                bad(f"会话态 followBottom 调不动：{type(e).__name__}: {e}")
                live_after = live_before
            else:
                pg.wait_for_timeout(500)
                live_after = pg.evaluate(SNAP)
            if live_after["st"] > 0:
                ok(f"会话态 followBottom(true) 仍滚到底："
                   f"scrollTop {live_before['st']}→{live_after['st']}（功能没被废）")
            else:
                bad(f"会话态跟随底部被误伤：{live_after}")

            if not errs:
                ok("整个输入过程 0 个 pageerror")
            else:
                bad(f"页面有 JS 异常：{errs[:3]}")
            b.close()
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:                                # noqa: BLE001
                try:
                    proc.kill()
                except Exception:
                    pass

    print(f"  {_n} 项 / 失败 {_fail}")
    print("[DONE]")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
