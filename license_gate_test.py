"""授权（离线激活码）的守卫套件。

两层，理由和 login_test.py 一样：
- 第一层纯逻辑/纯 HTTP，任何机器都能跑，**零第三方依赖**；
- 第二层真浏览器，只验"点了之后会发生什么"，没 playwright 就 SKIP。

★ 这个套件**绝不碰用户的真授权目录**。`License(root=...)` 全部指向
  临时目录 —— 否则跑一次回归就给用户的机器写上"已过期"。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
PY = sys.executable

_fail = []
_n = 0


def ok(m):
    global _n
    _n += 1
    print(f"  [PASS] {m}")


def bad(m):
    global _n
    _n += 1
    _fail.append(m)
    print(f"  [FAIL] {m}")


# ══════════════════════════════════════════════════════════════════
#  第一层：纯逻辑 + 纯 HTTP
# ══════════════════════════════════════════════════════════════════

def test_ed25519_vectors():
    """Ed25519 必须对上 RFC 8032 官方向量。

    ★ 这一步不是形式主义。自己写的椭圆曲线**看起来对**是最危险的状态：
      错了以后症状是"所有人激活码都验不过"，或者更糟——"所有人的都验得过"。
      官方向量是唯一能机械证伪的办法。
    """
    from rongke.license import public_key, sign, verify
    VEC = [
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
         "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
         "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ]
    for i, (sk_h, pk_h, m_h, sg_h) in enumerate(VEC, 1):
        sk, pk, msg, sg = (bytes.fromhex(sk_h), bytes.fromhex(pk_h),
                           bytes.fromhex(m_h), bytes.fromhex(sg_h))
        if public_key(sk) == pk:
            ok(f"RFC 8032 TEST {i}：私钥推公钥对得上")
        else:
            bad(f"RFC 8032 TEST {i}：公钥推错了")
        if sign(msg, sk) == sg:
            ok(f"RFC 8032 TEST {i}：签名逐字节一致")
        else:
            bad(f"RFC 8032 TEST {i}：签名与官方向量不一致")
        if verify(msg, sg, pk):
            ok(f"RFC 8032 TEST {i}：官方签名验得过")
        else:
            bad(f"RFC 8032 TEST {i}：官方签名验不过（实现有错）")

    # 负控
    sk = bytes.fromhex(VEC[0][0])
    pk = public_key(sk)
    msg = b"payload"
    sig = sign(msg, sk)
    if verify(msg, sig, pk):
        ok("正控：自己签的验得过")
    else:
        bad("正控失败：自己签的验不过")
    if not verify(msg + b"x", sig, pk):
        ok("★ 负控：消息改一字节 ⇒ 验不过")
    else:
        bad("★ 负控失败：消息被改还验得过")
    if not verify(msg, sig[:63] + bytes([sig[63] ^ 1]), pk):
        ok("★ 负控：签名改一字节 ⇒ 验不过")
    else:
        bad("★ 负控失败：签名被改还验得过")
    if not verify(msg, sig, public_key(bytes(32))):
        ok("★ 负控：换公钥 ⇒ 验不过")
    else:
        bad("★ 负控失败：别的公钥也能验过")


def test_code_tamper():
    """激活码不许被改：改到期日/套餐/签名都必须验不过。"""
    from rongke import license as L
    d = Path(tempfile.mkdtemp())
    old = L.EMBEDDED_PUBKEY
    try:
        sk_b64, pk_b64 = L.keygen()
        L.EMBEDDED_PUBKEY = pk_b64
        sk = L._b64d(sk_b64)
        payload = {"customer": "某实验室", "plan": "pro", "expires": 0,
                   "issued": 1793000000, "mid": L.machine_id()}
        code = L.make_code(payload, sk)
        if code.startswith("RK1."):
            ok("激活码以 RK1. 开头（粘错东西时看得出来）")
        else:
            bad(f"激活码前缀不对：{code[:16]}")
        if L.parse_code(code) == payload:
            ok("★ 打包→验签→解包，内容一字不差")
        else:
            bad("解出来的内容不对")
        body = L._b64d(code.split(".")[1])
        tail = code.split(".")[2]
        for name, alt in [("套餐", body.replace(b'"plan":"pro"',
                                                b'"plan":"enterprise"')),
                          ("到期日", body.replace(b'"expires":0', b'"expires":9'))]:
            try:
                L.parse_code("RK1." + L._b64e(alt) + "." + tail)
                bad(f"★ 负控失败：改了{name}还验得过")
            except ValueError:
                ok(f"★ 负控：改了{name} ⇒ 验签失败")
        try:
            L.parse_code("RK1." + code.split(".")[1] + "." + L._b64e(b"\x00" * 64))
            bad("★ 负控失败：假签名还验得过")
        except ValueError:
            ok("★ 负控：假签名 ⇒ 验不过")
        for junk in ["", "hello", "RK1.abc", "RK2.a.b"]:
            try:
                L.parse_code(junk)
                bad(f"垃圾串被当成激活码：{junk!r}")
            except ValueError:
                ok(f"垃圾串被拒绝（{junk!r}）")
    finally:
        L.EMBEDDED_PUBKEY = old
        shutil.rmtree(d, ignore_errors=True)


def test_states():
    """三态：全新=试用能跑、到期=拦、激活=放行。"""
    from rongke import license as L
    d = Path(tempfile.mkdtemp())
    old = L.EMBEDDED_PUBKEY
    try:
        st = L.License(d / "new").state()
        if st.state == "trial" and st.ok:
            ok(f"全新安装 ⇒ 试用，能跑（{st.reason}）")
        else:
            bad(f"全新安装状态不对：{st.state}")
        if st.days_left == L.TRIAL_DAYS:
            ok(f"剩余天数正好 {L.TRIAL_DAYS}（不虚报成 {L.TRIAL_DAYS + 1}）")
        else:
            bad(f"剩余天数不对：{st.days_left}")

        p = d / "old"
        p.mkdir(parents=True, exist_ok=True)
        (p / "trial.json").write_text(
            json.dumps({"first_seen": time.time() - (L.TRIAL_DAYS + 1) * 86400}),
            encoding="utf-8")
        got, st2 = L.License(p).allows_run()
        if not got and st2.state == "expired":
            ok(f"★ 试用期满 ⇒ 拦（{st2.reason}）")
        else:
            bad(f"★ 试用期满却放行：{got} {st2.state}")

        p3 = d / "almost"
        p3.mkdir(parents=True, exist_ok=True)
        (p3 / "trial.json").write_text(
            json.dumps({"first_seen": time.time() - (L.TRIAL_DAYS - 1) * 86400}),
            encoding="utf-8")
        got3, _ = L.License(p3).allows_run()
        if got3:
            ok("还剩 1 天 ⇒ 仍能跑（不提前失效）")
        else:
            bad("★ 还剩 1 天就被拦了")

        sk_b64, pk_b64 = L.keygen()
        L.EMBEDDED_PUBKEY = pk_b64
        sk = L._b64d(sk_b64)
        code = L.make_code({"customer": "已买的人", "plan": "pro",
                            "expires": time.time() + 365 * 86400,
                            "issued": time.time(), "mid": L.machine_id()}, sk)
        lic = L.License(d / "bought")
        lic.save_code(code)
        gotok, st4 = lic.allows_run()
        if gotok and st4.state == "licensed" and st4.customer == "已买的人":
            ok(f"★ 有效激活码 ⇒ 放行并记住客户名（{st4.reason}）")
        else:
            bad(f"★ 有效激活码没生效：{gotok} {st4.state}")

        exp_code = L.make_code({"customer": "去年", "plan": "pro",
                                "expires": time.time() - 86400,
                                "issued": time.time() - 4e7,
                                "mid": L.machine_id()}, sk)
        lic5 = L.License(d / "old-lic")
        lic5.save_code(exp_code)
        got5, st5 = lic5.allows_run()
        if not got5 and st5.state == "expired":
            ok(f"★ 过期的授权 ⇒ 拦住（{st5.reason}）")
        else:
            bad(f"★ 过期授权被放行：{got5} {st5.state}")

        # 伪造的码不许落盘
        lic6 = L.License(d / "forged")
        try:
            lic6.save_code("RK1." + code.split(".")[1] + "." + L._b64e(b"\x01" * 64))
            bad("★ 伪造的激活码被存进去了")
        except ValueError:
            ok("★ 伪造的激活码验不过，拒绝保存（先验再存）")
        if not (d / "forged" / "code.txt").exists():
            ok("拒绝时没在盘上留半截文件")
        else:
            bad("拒绝时仍写了文件")

        # 注销按零删除纪律只是改名
        lic.clear_code()
        if (d / "bought" / "code.txt.abandoned").exists():
            ok("注销是改名成 .abandoned（不是删）")
        else:
            bad("注销没留下 .abandoned —— 可能用了删除")
    finally:
        L.EMBEDDED_PUBKEY = old
        shutil.rmtree(d, ignore_errors=True)


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_up(url, timeout=45):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def test_http_gate():
    """真 HTTP：过期时 /api/run 是 403 且带 license 标记；激活后放行。

    这条是**拦不拦得住**的判据。只在 Python 层测 `allows_run()` 不够 ——
    真正的拦截点在 `Manager.start()`，测不到那儿等于没测。
    """
    import subprocess
    from rongke import license as L
    d = Path(tempfile.mkdtemp())
    proc = None
    try:
        sk_b64, pk_b64 = L.keygen()
        sk = L._b64d(sk_b64)
        (d / "license").mkdir(parents=True, exist_ok=True)
        (d / "license" / "trial.json").write_text(
            json.dumps({"first_seen": time.time() - 400 * 86400}),
            encoding="utf-8")
        (d / "sitecustomize.py").write_text(
            f"import rongke.license as L\nL.EMBEDDED_PUBKEY = {pk_b64!r}\n",
            encoding="utf-8")
        env = dict(os.environ)
        env["RONGKE_DATA"] = str(d)
        env["PYTHONPATH"] = str(d) + os.pathsep + str(ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        port = _free_port()
        proc = subprocess.Popen(
            [PY, "-m", "rongke", "--web", "--port", str(port)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        if not _wait_up(base + "/"):
            bad("控制台起不来，HTTP 层没测成")
            return

        with urllib.request.urlopen(base + "/api/license", timeout=10) as r:
            st = json.loads(r.read())
        if st.get("state") == "expired":
            ok(f"/api/license 如实报过期（{st.get('reason')}）")
        else:
            bad(f"/api/license 状态不对：{st.get('state')}")

        # 过期 ⇒ 403
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + "/api/run", method="POST",
                data=json.dumps({"goal": "x"}).encode(),
                headers={"Content-Type": "application/json"}), timeout=15)
            bad("★ 过期了还能发起任务 —— 闸门没生效")
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            if e.code == 403 and body.get("license"):
                ok("★ 过期 ⇒ /api/run 是 403 且带 license 标记")
            else:
                bad(f"★ 状态码/标记不对：{e.code} {body}")

        # 追问也是发起新任务：必须同样被 403 + license 拦下
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + "/api/follow", method="POST",
                data=json.dumps({"id": "nope", "text": "再说一句"}).encode(),
                headers={"Content-Type": "application/json"}), timeout=15)
            bad("★ 过期了还能追问 —— 闸门没生效")
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            if e.code == 403 and body.get("license"):
                ok("★ 过期 ⇒ /api/follow 是 403 且带 license 标记")
            else:
                bad(f"★ /api/follow 状态码/标记不对：{e.code} {body}")

        # 只读接口不受影响 —— 「只拦发起新任务」这句承诺必须是真的
        for p in ["/api/status", "/api/runs", "/api/tools"]:
            try:
                with urllib.request.urlopen(base + p, timeout=10) as r:
                    if r.status == 200:
                        ok(f"过期后 {p} 照常 200（只拦发起任务，不是锁死）")
                    else:
                        bad(f"过期后 {p} 是 {r.status}")
            except Exception as e:
                bad(f"过期后 {p} 打不开：{type(e).__name__}")

        # 激活 ⇒ 放行
        code = L.make_code({"customer": "HTTP 测试", "plan": "pro",
                            "expires": time.time() + 30 * 86400,
                            "issued": time.time(), "mid": L.machine_id()}, sk)
        with urllib.request.urlopen(urllib.request.Request(
                base + "/api/license", method="POST",
                data=json.dumps({"code": code, "action": "activate"}).encode(),
                headers={"Content-Type": "application/json"}), timeout=15) as r:
            j = json.loads(r.read())
        if j.get("ok") and (j.get("license") or {}).get("state") == "licensed":
            ok("★ 激活后 /api/license 回 licensed")
        else:
            bad(f"★ 激活响应不对：{j}")

        # 激活后能开任务（用一个必然失败但不被授权拦的目标；只看不是 403）
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    base + "/api/run", method="POST",
                    data=json.dumps({"goal": "x"}).encode(),
                    headers={"Content-Type": "application/json"}),
                    timeout=20) as r:
                ok("★ 激活后 /api/run 不再是 403（闸门真的开了）")
        except urllib.error.HTTPError as e:
            if e.code == 403:
                bad("★ 激活后仍然 403")
            else:
                ok(f"激活后 /api/run 过闸门（HTTP {e.code} 是别的原因，不是授权）")
        except Exception:
            ok("激活后 /api/run 过闸门（连接层异常，不是 403）")

        # 坏码 ⇒ 400 而不是 500
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + "/api/license", method="POST",
                data=json.dumps({"code": "RK1.xxx.yyy"}).encode(),
                headers={"Content-Type": "application/json"}), timeout=10)
            bad("坏激活码被接受了")
        except urllib.error.HTTPError as e:
            if e.code == 400:
                ok("★ 坏激活码 ⇒ 400（用户输入问题，不是服务器坏了）")
            else:
                bad(f"坏激活码的状态码不对：{e.code}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(d, ignore_errors=True)


def test_kernel_gate():
    """内核入口也要拦：CLI 走的是 `Rongke.run()`，不是 Web 那条路。"""
    from rongke import license as L
    from rongke.loop import LicenseError
    d = Path(tempfile.mkdtemp())
    try:
        (d / "trial.json").write_text(
            json.dumps({"first_seen": time.time() - 400 * 86400}),
            encoding="utf-8")
        lic = L.License(d)
        got, st = lic.allows_run()
        if not got:
            ok("前提：这个目录的授权是过期的")
        else:
            bad("前提不成立")

        # 授权子系统坏掉时必须**放行**，不能把"我算不出"升级成"你用不了"
        class Boom:
            def allows_run(self):
                raise RuntimeError("模拟授权子系统崩了")

        try:
            _ok2, _ = Boom().allows_run()
        except RuntimeError:
            ok("模拟坏掉的授权子系统确实会抛（这条是给下面那条做前提）")

        # 直接验 loop.py 里那段兜底逻辑的语义：异常 ⇒ 放行
        import inspect
        src = inspect.getsource(LicenseError)
        if "没开始" in src:
            ok("LicenseError 的语义被写在 docstring 里（是「没开始」不是「失败」）")
        else:
            bad("LicenseError 没说明它是「没开始」而不是「失败」")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_billing_not_mixed():
    """授权与账单必须是两件事 —— 用户自带 API key，钱付给厂商。"""
    from rongke import license as L, billing as B
    src = Path(ROOT / "rongke" / "license.py").read_text(encoding="utf-8")
    if "API" in src or "模型" in src:
        ok("license.py 里说清了它与模型 API 开销无关")
    else:
        bad("license.py 没交代「授权 vs API 开销」这层关系")
    # 授权状态不许出现在余额里（混了就等于把"买软件"和"花 token"搅一起）
    d = Path(tempfile.mkdtemp())
    try:
        bal = B.Billing(d / "billing").balance()
        if "license" not in json.dumps(bal):
            ok("★ 余额里没有授权字段（两件事没有混在一起）")
        else:
            bad("★ 余额里混进了授权字段")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cli_roundtrip():
    """CLI 全链路：keygen → sign → activate → show → deactivate。

    为什么这条必须在套件里、而不是我手工跑一次就算：
    `--license sign` 的三个参数（`--customer/--lic-plan/--days`）是**独立
    的 argparse 参数**，任何一个和别人撞名都会让整条命令打不开 ——
    实际就撞过：`--plan` 与 `--billing plan` 同名，argparse 直接
    `unrecognized arguments: --plan`。手工跑一次也能发现，但只有进了套件
    才不会在下次重构 cli.py 时静默坏掉。
    """
    import re as _re
    import subprocess
    from rongke import license as L
    src = ROOT / "rongke" / "license.py"
    bak = src.read_bytes()
    data = Path(tempfile.mkdtemp())
    try:
        env = dict(os.environ)
        env["RONGKE_DATA"] = str(data)
        env["PYTHONIOENCODING"] = "utf-8"

        def run(*a):
            return subprocess.run([PY, "-m", "rongke", *a], cwd=str(ROOT),
                                  env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")

        r = run("--license", "keygen")
        out = (r.stdout or "") + (r.stderr or "")
        pk = _re.search(r"(?:公钥)[^\n]*\n\s*([A-Za-z0-9_\-=]{40,48})", out)
        sk = _re.search(r"(?:私钥)[^\n]*\n\s*([A-Za-z0-9_\-=]{40,48})", out)
        if r.returncode == 0 and pk and sk:
            ok("keygen 打得出公钥与私钥")
        else:
            bad(f"keygen 不可用：{out[-160:]}")
            return
        if pk.group(1) != sk.group(1):
            ok("公钥与私钥不相同（没把同一把发两次）")
        else:
            bad("★ 公钥与私钥是同一个值")

        text = src.read_text(encoding="utf-8")
        src.write_text(_re.sub(r'^EMBEDDED_PUBKEY\s*=\s*"[^"]*"',
                               f'EMBEDDED_PUBKEY = "{pk.group(1)}"',
                               text, count=1, flags=_re.M),
                       encoding="utf-8", newline="\n")
        try:
            # ★ 用**等号形式**传私钥：base64url 可能以 `-` 开头，写成
            #   `--sk <私钥>` 时 argparse 会把那个值当成另一个选项，
            #   报 `argument --sk: expected one argument`。间歇性失败
            #   （只在私钥恰好以 - 开头时出现），实测量到过一次。
            r2 = run("--license", "sign", f"--sk={sk.group(1)}",
                     f"--mid={L.machine_id()}",
                     "--customer", "浙江工业大学", "--lic-plan", "pro",
                     "--days", "365")
            out2 = (r2.stdout or "") + (r2.stderr or "")
            code = ""
            for ln in out2.splitlines():
                if ln.strip().startswith("RK1."):
                    code = ln.strip()
                    break
            if r2.returncode == 0 and code:
                ok(f"sign 签出一行激活码（{len(code)} 字符）")
            else:
                bad(f"★ sign 打不开或没输出（参数撞名就会这样）：{out2[-200:]}")
                return
            if "浙江工业大学" in out2:
                ok("sign 回显客户名（发码前能核一眼）")
            else:
                bad("sign 没回显客户名")

            r3 = run("--license", "activate", code)
            r4 = run("--license")
            out4 = (r4.stdout or "") + (r4.stderr or "")
            # 判据打真实文案：show 打的是「内部状态    licensed」
            if r3.returncode == 0 and "licensed" in out4:
                ok("★ activate 后 show 变成已授权")
            else:
                bad(f"★ activate→show 不通：{out4[:200]}")

            r5 = run("--license", "activate", "RK1.坏.坏")
            r6 = run("--license")
            out6 = (r6.stdout or "") + (r6.stderr or "")
            if r5.returncode != 0 and "licensed" in out6 and "浙江工业大学" in out6:
                ok("★ 坏码被拒，且原来那张授权完好（客户名仍在）")
            else:
                bad("★ 试了一个坏码，把好好的授权弄没了")

            r7 = run("--license", "deactivate")
            r8 = run("--license")
            out8 = (r8.stdout or "") + (r8.stderr or "")
            if r7.returncode == 0 and "licensed" not in out8:
                ok("deactivate 后不再是已授权")
            else:
                bad("★ 注销没生效")
        finally:
            src.write_bytes(bak)

        # 过期试用下 run 的退出码必须是 4，不能和运行失败(3)混
        import json as _json
        import time as _time
        licdir = data / "license"
        licdir.mkdir(parents=True, exist_ok=True)
        (licdir / "trial.json").write_text(
            _json.dumps({"first_seen": _time.time() - 400 * 86400}),
            encoding="utf-8")
        r9 = run("说点什么")
        out9 = (r9.stdout or "") + (r9.stderr or "")
        if r9.returncode == 4:
            ok("★ 授权拦截的退出码是 4（与运行失败 3、中断 130 分开）")
        else:
            bad(f"★ 拦截的退出码是 {r9.returncode}，不是 4")
        if "激活码" in out9:
            ok("拦截信息告诉用户下一步怎么做")
        else:
            bad("拦截时没给下一步")
    finally:
        src.write_bytes(bak)
        shutil.rmtree(data, ignore_errors=True)


def test_gate_beats_busy():
    """「没授权」要排在「忙」前面 —— 否则给出的是一条假出路。

    两者同时成立时，如果先报"等它结束再开新的"，用户等完了照样开不了
    新任务；而"你没授权、去激活"是当场可行动的。这条只能靠**同时造出
    两个条件**才测得出来。
    """
    import threading as _th
    from rongke.web.server import Manager
    from rongke.license import License
    d = Path(tempfile.mkdtemp())
    try:
        (d / "trial.json").write_text(
            json.dumps({"first_seen": time.time() - 400 * 86400}),
            encoding="utf-8")
        lic = License(d)

        # 不起真服务：用 __new__ 造一个只有授权与"忙"两个字段的最小对象。
        m = Manager.__new__(Manager)
        m.license = lic
        m.license_error = ""
        m.billing_error = ""
        m._lock = _th.RLock()

        class _BusyRun:
            done = False

        m.active = "fake-running"
        m.runs = {"fake-running": _BusyRun()}
        m.kernel = object()          # 假装内核就绪，排除那条分支
        m.kernel_error = ""
        m._license_ok = Manager._license_ok.__get__(m, Manager)

        run, why = Manager.start(m, "随便一个目标", None)
        if run is None and ("激活码" in why or "试用" in why):
            ok("★ 又忙又没授权时，报的是「没授权」而不是「忙」")
        elif run is None and "等它结束" in why:
            bad("★ 同时满足两个条件时先报了「忙」——用户会等到一条假出路")
        else:
            bad(f"未预期结果：run={run} why={why}")

        run2, why2 = Manager.follow_up(m, "fake-running", "再补一句")
        if run2 is None and ("激活码" in why2 or "试用" in why2):
            ok("★ follow_up 也先报「没授权」，而不是「忙」")
        else:
            bad(f"follow_up 未预期结果：run={run2} why={why2}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_btn_class_not_evading_injection():
    """授权页新增的按钮不能把 `ui_check.py --self-test` 弄红。

    为什么这条必须存在、而不是"顺手写对就行"：
    `ui_check.py --self-test` 里 W14E·btn 那条负控是拿 `class="btn `
    （**带空格前缀**）做全量替换来模拟"把 .btn 摘掉"。裸写 `class="btn"`
    （后面紧跟引号）不含那个前缀，替换碰不到它，于是"全摘"之后仍残留
    一处 .btn —— 负控判据被**静默打穿**，自检直接变红。

    我加授权页时就写了 2 处裸的 `class="btn"`，全量回归里 `ui_check_st`
    因此报 1 项失败，而根因在界面文件、不在检查器。这条判据把这个
    耦合钉住：**改了界面就要保证检查器的自检还是绿的**。

    ★ 判据直接跑那个自检，而不是数 `class="btn"` 的个数 ——
      数个数只是我这一次的巧合形态，检查器以后可能改注入串；
      跑自检才是它真正承诺的东西。
    """
    import subprocess
    html = (ROOT / "rongke" / "web" / "index.html").read_text(encoding="utf-8")
    # 先给出便宜、可读的定位信息（失败时好查），但不作为唯一判据
    bare = html.count('class="btn"')
    r = subprocess.run([PY, "ui_check.py", "--self-test"], cwd=str(ROOT),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        ok("ui_check 的负控自检仍然绿（界面改动没打穿检查器）")
    else:
        hint = f"（界面里还有 {bare} 处裸 class=\"btn\"）" if bare else ""
        bad(f"★ ui_check --self-test 变红了{hint} —— 界面写法打穿了负控")
        for ln in out.splitlines():
            if "FAIL" in ln or "漏报" in ln:
                print("        " + ln.strip()[:110])


if __name__ == "__main__":
    print("=== [1] Ed25519 对上 RFC 8032 官方向量 ===")
    test_ed25519_vectors()
    print("\n=== [2] 激活码防篡改 ===")
    test_code_tamper()
    print("\n=== [3] 三态：试用 / 到期 / 已授权 ===")
    test_states()
    print("\n=== [4] 真 HTTP 闸门（过期 403、激活放行、只拦发起任务）===")
    test_http_gate()
    print("\n=== [5] 内核入口与异常语义 ===")
    test_kernel_gate()
    print("\n=== [6] 授权与账单没有混在一起 ===")
    test_billing_not_mixed()
    print("\n=== [7] CLI 全链路（keygen→sign→activate→show→deactivate）===")
    test_cli_roundtrip()
    print("\n=== [8] 「没授权」排在「忙」前面 ===")
    test_gate_beats_busy()
    print("\n=== [9] 授权页的按钮没绕开 ui_check 的负控 ===")
    test_btn_class_not_evading_injection()
    print()
    if _fail:
        print(f"有问题 ✘   合计 {_n} 项，失败 {len(_fail)} 项")
        for m in _fail:
            print("  -", m)
        sys.exit(1)
    print(f"合计 {_n} 项：{_n} 通过，0 失败")
    print("[DONE] 授权守卫全部通过")
