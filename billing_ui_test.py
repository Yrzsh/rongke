"""账单区块的**真实浏览器**判据：数字与声明真的上屏了吗。

为什么不并进 `ui_check.py`：那套是静态源码检查，判的是"页面里有没有这段
代码"。这次要验的恰恰是它看不见的那一半 —— `loadBilling()` 是异步的，
它可能 404、可能抛错、可能把区块留成空壳，而静态检查全绿。

为什么不并进 `ui_shot.py`：那套负责回放与几何，要连**已启动**的控制台，
是给人手工跑、要花 token 的（一次 225s）。

判据打在**渲染后的可见文本**上，三条：
  ① 余额上屏（数据链路真的通了，不是空壳）
  ② 「没有接支付网关」上屏 —— 这是产品诚实性的一部分，不是装饰
  ③ 没有 JS 异常（区块没被自己的脚本炸掉）

用法：`python billing_ui_test.py`（自己起控制台、自己关）
      `python billing_ui_test.py --port 8912`（连已启动的）
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
_fail = 0


def ok(m: str) -> None:
    print(f"  [PASS] {m}")


def bad(m: str) -> None:
    global _fail
    _fail += 1
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


def _warm_setup(port: int) -> tuple[bool, str]:
    """开浏览器前先把 /api/setup 打热，把「冷启动」这个无关变量摘出去。

    ★ 判据要验的是「账单区块上屏」，而 #setupBill 是 renderSetup() 动态生成的 ——
      只有 /api/setup 成功返回 groups 才会生成它。刚起的控制台首次这个接口实测
      22.6s（之后 3.5s），不预热就等于让账单判据顺便测「环境检查能不能冷启动
      跑完」，红了也分不清是账单坏了还是体检没跑完。
    """
    import json as _json
    import urllib.request as _u
    op = _u.build_opener(_u.ProxyHandler({}))
    try:
        with op.open(f"http://127.0.0.1:{port}/api/setup", timeout=100) as r:
            d = _json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(d.get("groups"), list):
            return True, f"{len(d['groups'])} 组环境检查"
        return False, f"返回里没有 groups：{str(d)[:120]}"
    except Exception as e:                                  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0,
                    help="连已启动的控制台；不给就自己起一个")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("没有 playwright，跳过（这一条只在本机装了 playwright 时才有意义）")
        return 0

    proc = None
    port = args.port
    if not port:
        # ★ 全套回归里已经有一台热控制台（RONGKE_WEB）。自己再冷起一台时，
        #   /api/setup 首次要加载整条依赖链，实测曾 45s 还没把账单区块渲染出来；
        #   复用热台既省时间，也把「测的其实是冷启动」这个无关变量摘掉。
        #   regress.py 的 _child_env() 继承 os.environ，这个变量能传进来。
        _m = re.search(r":(\d+)\s*$", os.environ.get("RONGKE_WEB", ""))
        if _m:
            port = int(_m.group(1))
            print(f"  info: 复用热控制台 RONGKE_WEB → port {port}")
    if not port:
        print("  info: 自起冷控制台（RONGKE_WEB 没给 / 没解析出端口）")
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "rongke", "--web", "--port", str(port)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_http(port):
            bad(f"控制台没起来（端口 {port}）")
            proc.kill()
            return 1

    # ★ 预热：没测到必须记账，不能让浏览器里 60s 空等冒充「没问题」。
    warm, why = _warm_setup(port)
    if warm:
        print(f"  info: /api/setup 已打热（{why}）")
    else:
        bad(f"/api/setup 预热没成功：{why}")

    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page(viewport={"width": 1440, "height": 1000})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle",
                    timeout=60000)

            pg.click('.navbtn[data-v="setup"]')
            pg.wait_for_timeout(1200)

            # ★ 等的是"区块里真的有字"，不是"元素存在" ——
            #   空壳元素一开始就在 DOM 里，等它存在等于什么都没等。
            try:
                pg.wait_for_function(
                    "() => { const b = document.querySelector('#setupBill');"
                    " return b && b.innerText.trim().length > 20; }",
                    timeout=60000)
                ok("账单区块渲染出内容（不是空壳）")
            except Exception:
                # ★ 诊断代码不许自己炸：元素压根不存在时 eval_on_selector 会抛
                #   Error，整份套件变成「0 项 / 0 失败 + rc=1」——「没测到」
                #   必须被记账，不能被异常冒充成「没问题」。
                diag = pg.evaluate(
                    "() => { const b = document.querySelector('#setupBill');"
                    " const s = document.querySelector('#setupBody');"
                    " return { bill: b ? b.innerText : '(元素不存在)',"
                    "          body: s ? s.innerText.slice(0, 300) : '(setupBody 不存在)' }; }")
                bad(f"账单区块没渲染出内容：{diag['bill'][:120]!r}"
                    f"；setup 面板显示：{diag['body'][:200]!r}")

            txt = pg.evaluate(
                "() => { const b = document.querySelector('#setupBill');"
                " return b ? b.innerText : ''; }")

            if "$" in txt and ("余额" in txt or "已用" in txt):
                ok("余额数字上屏")
            else:
                bad(f"没看到余额数字：{txt[:160]!r}")

            if "没有接支付网关" in txt:
                ok("★ 「没有接支付网关」上屏（诚实性声明不是文档里的句子）")
            else:
                bad(f"没看到「没有接支付网关」：{txt[:200]!r}")

            if "台账在" in txt and "ledger.jsonl" in txt:
                ok("台账路径上屏（后端给的那条，不是前端自己拼的）")
            else:
                bad(f"没看到台账路径：{txt[:200]!r}")

            if errs:
                bad(f"页面有 JS 异常：{errs[:3]}")
            else:
                ok("没有 JS 异常")
            br.close()
    finally:
        if proc is not None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass

    print()
    if _fail:
        print(f"✘ 失败 {_fail} 项")
        return 1
    print("✔ 账单区块浏览器判据全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
