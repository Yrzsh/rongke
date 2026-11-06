"""登录门禁的常驻守卫（服务端 + 真浏览器两层）。

为什么要有两套：
- **服务端层**（不需要 playwright）：证明"设了密码就不再放行"，判据打在
  401 上。它跑得快，且在没装浏览器的机器上也能跑。
- **浏览器层**（需要 playwright，没有就跳过）：证明"门真的出现在屏幕上、
  输对能进、令牌能留住"。这一层抓到过一个只有真浏览器才看得见的 bug：
  服务端认令牌，但 `checkLoginGate` 用裸 fetch 没带令牌，于是**每次都要求
  重新登录**。静态检查与纯服务端测试都看不见它。

单独跑：python login_test.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_fail = 0
_n = 0


def ok(m):
    global _n
    _n += 1
    print(f"  [PASS] {m}")


def bad(m):
    global _n, _fail
    _n += 1
    _fail += 1
    print(f"  [FAIL] {m}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ══════════════════ 第一层：服务端门禁 ══════════════════
def test_server_gate():
    import rongke.web.server as S
    from rongke.billing import Billing

    tmp = Path(tempfile.mkdtemp(prefix="rklogin_"))
    mgr = S.Manager.__new__(S.Manager)
    mgr.runs = {}
    mgr.active = None
    mgr._lock = threading.Lock()
    mgr.kernel = None
    mgr.kernel_error = "（本套件不需要真内核）"
    mgr.inbox = None
    mgr.removed = None
    mgr.inbox_leftover = []
    mgr.billing = Billing(tmp / "billing")
    mgr.billing_error = ""

    httpd = S.Server(("127.0.0.1", 0), S.Handler)
    httpd.mgr = mgr
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, method="GET", body=None, token=""):
        req = urllib.request.Request(base + path, method=method)
        if token:
            req.add_header("X-Rongke-Token", token)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}

    try:
        # ── 没设密码：本地免登录是默认，不能把用户关在自己工具外面 ──
        st, j = call("/api/auth")
        if st == 200 and j.get("login_required") is False:
            ok("没设密码 ⇒ login_required=false")
        else:
            bad(f"没设密码时 /api/auth 不对：{st} {j}")
        if call("/api/status")[0] == 200:
            ok("没设密码 ⇒ /api/status 直接 200（免登录）")
        else:
            bad("没设密码却把控制台锁了")

        mgr.billing.set_password("guard-pw")

        # ── 门必须关上 ──
        st, j = call("/api/auth")
        if st == 200:
            ok("/api/auth 在门前（前端才能问出要不要登录）")
        else:
            bad(f"/api/auth 被门挡了：{st}")

        for path in ("/api/status", "/api/tools", "/api/billing", "/api/runs"):
            st = call(path)[0]
            if st == 401:
                ok(f"★ 设了密码 + 无令牌 ⇒ {path} 是 401")
            else:
                bad(f"★ 设了密码但 {path} = {st} —— 门没关")

        # 激活/注销也是写操作：注释说它在门禁内，就必须真的在。
        st = call("/api/license", "POST", {"action": "deactivate"})[0]
        if st == 401:
            ok("★ 设了密码 + 无令牌 ⇒ POST /api/license 是 401")
        else:
            bad(f"★ POST /api/license = {st} —— 授权端点没进门禁")

        # ── 错密码不发令牌（关键：不能"错了也给个能用的令牌"）──
        st, j = call("/api/login", "POST", {"password": "nope"})
        if st == 401 and not j.get("token"):
            ok("★ 错密码 401 且不发令牌")
        else:
            bad(f"错密码返回 {st}，token={j.get('token')!r}")

        # ── 对密码 → 令牌 → 门开 ──
        st, j = call("/api/login", "POST", {"password": "guard-pw"})
        tok = j.get("token") or ""
        if st == 200 and tok:
            ok("对密码 200 并发令牌")
        else:
            bad(f"对密码失败：{st}")
        if tok and call("/api/status", token=tok)[0] == 200:
            ok("★ 带令牌 ⇒ 门真的开了")
        else:
            bad("带令牌仍然进不去")

        # ── 令牌是不可猜的随机串 ──
        _, ja = call("/api/login", "POST", {"password": "guard-pw"})
        _, jb = call("/api/login", "POST", {"password": "guard-pw"})
        if ja.get("token") and jb.get("token") and ja["token"] != jb["token"]:
            ok("两次登录拿到不同令牌（不是固定串）")
        else:
            bad("令牌不是随机的")
        if call("/api/status", token="guess-guess")[0] == 401:
            ok("猜的令牌 401")
        else:
            bad("猜的令牌居然被接受")

        # ── 登出即撤销 ──
        call("/api/logout", "POST", {}, token=tok)
        if call("/api/status", token=tok)[0] == 401:
            ok("★ 登出后同一令牌 401（撤销是真的）")
        else:
            bad("登出后令牌仍可用")

        # ── 同一个连接上连发多条请求（keep-alive 复现）──
        # 这一条是**回归判据**：`/api/logout` 原先不读 body 就返回，于是
        # 未读的 body 被当成下一条请求的开头，同一连接上的后续请求串味，
        # 客户端只看到 `WinError 10053`。单跑整套件时往往侥幸通过 ——
        # 因为 urllib 每条请求都新开连接，问题不出现。
        # 所以必须**显式复用一条连接**才测得出来。
        import http.client
        host, port_s = base.split("//", 1)[1].split(":")
        conn2 = http.client.HTTPConnection(host, int(port_s), timeout=20)
        try:
            _, jl = call("/api/login", "POST", {"password": "guard-pw"})
            tl = jl.get("token") or ""
            seq_ok = True
            try:
                # 1) 发一条**带 body** 的 logout（原先就是它把连接搞坏的）
                conn2.request("POST", "/api/logout", body=b'{"x":1}',
                              headers={"Content-Type": "application/json",
                                       "X-Rongke-Token": tl})
                r1 = conn2.getresponse()
                r1.read()
                # 2) 同一条连接上再发一条 —— 串味会在这里炸
                conn2.request("GET", "/api/auth",
                              headers={"X-Rongke-Token": tl})
                r2 = conn2.getresponse()
                body2 = r2.read().decode("utf-8", "replace")
                # 3) 再来一条，确认不是碰巧
                conn2.request("GET", "/api/status",
                              headers={"X-Rongke-Token": tl})
                r3 = conn2.getresponse()
                r3.read()
                if r1.status == 200 and r2.status == 200 and r3.status == 401:
                    ok("★ 同一连接连发 3 条请求都对（keep-alive 不串味）")
                else:
                    seq_ok = False
                    bad(f"★ 同一连接上串味了：{r1.status}/{r2.status}/{r3.status}")
            except Exception as e:                    # noqa: BLE001
                seq_ok = False
                bad(f"★ 同一连接上连发请求炸了：{type(e).__name__}: {e}")
            if seq_ok and '"login_required"' not in body2:
                bad(f"第二条的响应不是预期 JSON：{body2[:120]!r}")
        finally:
            try:
                conn2.close()
            except Exception:
                pass

        # ── 令牌表不许无限增长 ──
        # `token_valid` 的过期清理是惰性的（只在拿旧令牌来时清），
        # 所以"反复登录但不再用旧令牌"会让表无限涨。这不是理论问题：
        # 前端每次 `location.reload()` 之后都会重新登录一次。
        try:
            before_n = len(mgr.billing._tokens)
            toks = [mgr.billing.issue_token() for _ in range(200)]
            n = len(mgr.billing._tokens)
            if n <= mgr.billing.MAX_TOKENS:
                ok(f"★ 连发 200 个令牌后表里只留 {n} 个（上限 "
                   f"{mgr.billing.MAX_TOKENS}，不会无限涨）")
            else:
                bad(f"★ 令牌表涨到 {n} 条（上限 {mgr.billing.MAX_TOKENS}）—— 内存泄漏")
            if mgr.billing.token_valid(toks[-1]):
                ok("最新签发的令牌仍然有效")
            else:
                bad("★ 最新令牌被误删 —— 正常用户会被自己的登录踢掉")
            if not mgr.billing.token_valid(toks[0]):
                ok("最老的令牌已失效（丢的是最该丢的那些）")
            else:
                bad("最老的令牌仍然有效 —— 上限没真正生效")
        except Exception as e:                        # noqa: BLE001
            bad(f"令牌表上限检查异常：{type(e).__name__}: {e}")

        # ── 凭据不许下发给前端 ──
        _, j2 = call("/api/login", "POST", {"password": "guard-pw"})
        t2 = j2.get("token") or ""
        st, rep = call("/api/billing", token=t2)
        blob = json.dumps(rep, ensure_ascii=False)
        if st == 200 and not any(k in blob for k in ("salt", "pwd_hash", "pwd_iter")):
            ok("★ /api/billing 不泄露 salt / pwd_hash / pwd_iter")
        else:
            bad("★ /api/billing 泄露了凭据材料")

        # ── 页码路径不能绕过门 ──
        # 首页必须放行：登录界面本体就在里面，挡了它用户没法登录。
        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
        if r.status == 200 and "loginGate" in html:
            ok("首页放行，且里面带着登录层")
        else:
            bad(f"首页取不到或缺登录层（{r.status}）")
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════ 第二层：真浏览器 ══════════════════
def test_browser_gate():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [SKIP] 没装 playwright，跳过浏览器层")
        return

    tmp = Path(tempfile.mkdtemp(prefix="rkloginui_"))
    data = tmp / "data"
    data.mkdir()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    # 这个子进程是一个**独立控制台**（不是夹具），给它自己的数据根正合适：
    # 我们绝不能在用户的真实台账上设密码。
    env["RONGKE_DATA"] = str(data)
    env["PYTHONIOENCODING"] = "utf-8"
    exe = sys.executable

    r = subprocess.run(
        [exe, "-c",
         "import sys; sys.path.insert(0, r'%s')\n"
         "from rongke.billing import Billing\n"
         "Billing().set_password('ui-pw')\n" % str(ROOT)],
        env=env, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        bad(f"备场失败（设密码）：{r.stderr[:200]}")
        shutil.rmtree(tmp, ignore_errors=True)
        return

    proc = subprocess.Popen(
        [exe, "-m", "rongke", "--web", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))

    up = False
    for _ in range(200):
        try:
            with socket.create_connection(("127.0.0.1", port), 1):
                up = True
                break
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.3)
    if not up:
        bad("控制台没起来（浏览器层无法验证）")
        proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
        return

    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(base + "/", wait_until="domcontentloaded")
            pg.wait_for_timeout(1800)

            if pg.evaluate("() => { const g = document.querySelector('#loginGate');"
                           " return g && !g.hidden; }"):
                ok("★ 设了密码 ⇒ 登录层真的弹出来了（不是 hidden 空壳）")
            else:
                bad("★ 登录层没弹 —— 门没关")

            pg.fill("#loginPw", "wrong")
            pg.click("#loginBtn")
            pg.wait_for_timeout(1200)
            err = pg.evaluate("() => { const e = document.querySelector('#loginErr');"
                              " return e && !e.hidden ? e.innerText.trim() : ''; }")
            if err and pg.evaluate("() => { const g = document.querySelector('#loginGate');"
                                   " return g && !g.hidden; }"):
                ok(f"★ 错密码留在门外并给了原因（{err[:16]}）")
            else:
                bad(f"错密码处理不对：err={err!r}")

            pg.fill("#loginPw", "ui-pw")
            pg.click("#loginBtn")
            try:
                pg.wait_for_load_state("load", timeout=8000)
            except Exception:
                pass
            pg.wait_for_timeout(2500)
            if pg.evaluate("() => { const g = document.querySelector('#loginGate');"
                           " return !g || g.hidden; }"):
                ok("★ 对密码 ⇒ 登录层消失")
            else:
                bad("★ 对密码之后仍进不去")

            if pg.evaluate("() => document.body.innerText.trim().length") > 150:
                ok("进去之后页面真的有内容（不是空壳）")
            else:
                bad("进去之后页面几乎是空的")

            if pg.evaluate("() => localStorage.getItem('rongke_token') || ''"):
                ok("令牌已存进 localStorage")
            else:
                bad("令牌没存下")

            # ★ 刷新这条是那个真 bug 的判据：服务端认、浏览器却不带令牌时，
            #   这里会红。没有它，登录层能过但每次刷新都要重登。
            pg.reload(wait_until="domcontentloaded")
            pg.wait_for_timeout(2000)
            if not pg.evaluate("() => { const g = document.querySelector('#loginGate');"
                               " return g && !g.hidden; }"):
                ok("★ 刷新后仍在门内（令牌被受理，不是每次重登）")
            else:
                bad("★ 刷新后又要求登录 —— 浏览器没带令牌")

            if errs:
                bad("页面有 JS 异常：" + " | ".join(errs[:3]))
            else:
                ok("整个登录流程零 JS 异常")
            br.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("=== [1] 服务端门禁（不需要浏览器）===")
    test_server_gate()
    print("\n=== [2] 真浏览器：门在屏幕上真的关上又打开 ===")
    test_browser_gate()
    print()
    print(f"合计 {_n} 项：{_n - _fail} 通过，{_fail} 失败")
    if _fail:
        sys.exit(1)
    print("[DONE] 登录守卫全部通过")
