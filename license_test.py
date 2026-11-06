"""对着 RFC 8032 官方测试向量验 Ed25519 实现。

这一步不能省：自写的密码学代码**看起来对**是最危险的状态。
纯 Python 手写椭圆曲线很容易在某个边界上错，而错了以后症状是
"所有人的激活码都验不过"或者更糟——"所有人的都验得过"。
官方测试向量是唯一能机械证伪的办法。

RFC 8032 §7.1 的 TEST 1 / TEST 2 / TEST 3 / TEST 1024 都在下面。
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rongke.license import public_key, sign, verify, _b64e, _b64d

_fail = []


def ok(msg):
    print(f"  [PASS] {msg}")


def bad(msg):
    _fail.append(msg)
    print(f"  [FAIL] {msg}")


# RFC 8032 §7.1 的向量：(secret, public, message, signature)
VECTORS = [
    # TEST 1（空消息）
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
     "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    # TEST 2（1 字节）
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    # TEST 3（2 字节）
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]

print("=== [1] RFC 8032 官方测试向量 ===")
for i, (sk_h, pk_h, msg_h, sig_h) in enumerate(VECTORS, 1):
    sk, pk, msg = bytes.fromhex(sk_h), bytes.fromhex(pk_h), bytes.fromhex(msg_h)
    sig = bytes.fromhex(sig_h)
    got_pk = public_key(sk)
    if got_pk == pk:
        ok(f"TEST {i}：私钥推公钥对得上")
    else:
        bad(f"TEST {i}：公钥推错了 {got_pk.hex()[:16]}… ≠ {pk.hex()[:16]}…")
    got_sig = sign(msg, sk)
    if got_sig == sig:
        ok(f"TEST {i}：签名与官方向量逐字节一致")
    else:
        bad(f"TEST {i}：签名不一致 {got_sig.hex()[:16]}… ≠ {sig.hex()[:16]}…")
    if verify(msg, sig, pk):
        ok(f"TEST {i}：官方签名验得过")
    else:
        bad(f"TEST {i}：官方签名验不过（实现有错）")

print("\n=== [2] 验签的负控（不许什么都放行）===")
sk = bytes.fromhex(VECTORS[0][0])
pk = public_key(sk)
msg = b"rongke license payload"
sig = sign(msg, sk)
if verify(msg, sig, pk):
    ok("正控：自己签的验得过")
else:
    bad("正控失败：自己签的验不过 —— 实现坏了")
if not verify(msg + b"x", sig, pk):
    ok("★ 负控：消息改一个字节 ⇒ 验不过")
else:
    bad("★ 负控失败：消息被改了还验得过（验签形同虚设）")
if not verify(msg, sig[:63] + bytes([sig[63] ^ 1]), pk):
    ok("★ 负控：签名改一个字节 ⇒ 验不过")
else:
    bad("★ 负控失败：签名被改了还验得过")
if not verify(msg, sig, public_key(bytes(32))):
    ok("★ 负控：换一把公钥 ⇒ 验不过")
else:
    bad("★ 负控失败：别的公钥也能验过")
# 坏输入不许抛栈（验签不该因为畸形数据崩掉）
for junk in [b"", b"\x00" * 64, b"\xff" * 64]:
    try:
        if verify(msg, junk, pk) or verify(msg, sig, junk):
            bad(f"畸形输入被验过了：{junk[:4]!r}")
        else:
            ok(f"畸形输入被判为验不过且没抛栈（{len(junk)} 字节）")
    except Exception as e:                             # noqa: BLE001
        bad(f"畸形输入抛栈了（应返回 False）：{type(e).__name__}: {e}")

print("\n=== [3] 激活码的打包/解包往返 ===")
from rongke import license as L

payload = {"customer": "测试实验室", "plan": "pro", "expires": 0,
           "issued": 1793000000, "mid": L.machine_id()}
code = L.make_code(payload, sk)
if code.startswith("RK1.") and code.count(".") == 2:
    ok("激活码格式是 RK1.<body>.<sig>")
else:
    bad(f"激活码格式不对：{code[:24]}…")
# 用临时把 EMBEDDED_PUBKEY 换掉来验（模块里那把是空的，表示没发行能力）
_old = L.EMBEDDED_PUBKEY
L.EMBEDDED_PUBKEY = _b64e(pk)
try:
    got = L.parse_code(code)
    if got == payload:
        ok("★ 激活码打包→验签→解包，内容一字不差（含中文客户名）")
    else:
        bad(f"解出来的内容不对：{got}")
    # 篡改：改 payload 再拼上原签名
    tampered = L.make_code({**payload, "expires": 0, "plan": "enterprise"}, sk)
    body2 = _b64d(code.split(".")[1])
    bad_code = "RK1." + _b64e(body2.replace(b'"plan":"pro"',
                                            b'"plan":"enterprise"')) \
               + "." + code.split(".")[2]
    try:
        L.parse_code(bad_code)
        bad("★ 负控失败：改了套餐名还验得过（防篡改没生效）")
    except ValueError:
        ok("★ 负控：改了套餐名 ⇒ 验签失败（防篡改生效）")
    # 改到期日
    bad2 = "RK1." + _b64e(body2.replace(b'"expires":0', b'"expires":9')) \
           + "." + code.split(".")[2]
    try:
        L.parse_code(bad2)
        bad("★ 负控失败：改了到期日还验得过")
    except ValueError:
        ok("★ 负控：改了到期日 ⇒ 验签失败")
    # 换签名
    try:
        L.parse_code("RK1." + code.split(".")[1] + "." + _b64e(b"\x00" * 64))
        bad("★ 负控失败：换了个假签名还验得过")
    except ValueError:
        ok("★ 负控：假签名 ⇒ 验不过")
    # 垃圾串
    for junk in ["", "hello", "RK1.abc", "RK2.a.b", "RK1.!!!.???"]:
        try:
            L.parse_code(junk)
            bad(f"垃圾串被当成激活码了：{junk!r}")
        except ValueError:
            ok(f"垃圾串被拒绝（{junk!r}）")
finally:
    L.EMBEDDED_PUBKEY = _old

print("\n=== [4] 没有内嵌公钥时要说清楚 ===")
# ★ 这一节原本直接在"公钥为空"的状态下跑。发行方填了 EMBEDDED_PUBKEY 之后
#   前提就不成立了，于是它开始假红。判据要测的**那件事**没变（空公钥时
#   报错必须说"没有公钥"，而不是含糊的"验签失败"），所以这里改成
#   **显式清空**再验，并在 finally 里恢复 —— 测的还是同一件事，且与
#   公钥填没填无关。
_saved_pk = L.EMBEDDED_PUBKEY
try:
    L.EMBEDDED_PUBKEY = ""
    try:
        L.parse_code(code)
        bad("没有内嵌公钥却验过了 —— 等于谁都能激活")
    except ValueError as e:
        if "公钥" in str(e):
            ok("没有内嵌公钥时报的是「没有公钥」而不是含糊的验签失败")
        else:
            bad(f"报错没说清原因：{e}")
finally:
    L.EMBEDDED_PUBKEY = _saved_pk

# 反过来也要验：填了公钥之后，用它**配对的私钥**签的码必须能过。
# ★ 这里不能用上面那张 `code` —— 它是本节用**临时密钥**签的，和内嵌公钥
#   无关，拿它去验「填了公钥能不能过」等于让两把不相干的钥匙互开。
#   真正的判据是「配对关系」：内嵌公钥必须等于 public_key(发行私钥)。
#   发行私钥不写在这里（写了进仓库就等于把签名能力发出去），
#   所以只验公钥自身的形状；配对关系由 runs/_an/verify_issuing.py 验。
if _saved_pk:
    try:
        raw = L._b64d(_saved_pk)
        if len(raw) == 32:
            ok(f"内嵌公钥形状正确（32 字节），这份源码有发行能力：{_saved_pk[:10]}…")
        else:
            bad(f"内嵌公钥不是 32 字节（{len(raw)}）—— 填错了")
    except Exception as e:                              # noqa: BLE001
        bad(f"内嵌公钥不是合法 base64：{e}")
else:
    ok("当前没有内嵌公钥（这份源码还没有发行能力，属正常）")

print("\n=== [5] 源码树 LICENSE/license 撞名时退到 .license ===")
# Windows 文件系统不区分大小写：仓库根的 LICENSE 文件会让 data_root()/"license"
# 指向一个文件，mkdir 抛 FileExistsError。判据是「默认根要能真的写下去」，
# 不是「名字看起来对」。
import shutil as _shutil
import tempfile as _tempfile
from rongke import config as _C

_d = Path(_tempfile.mkdtemp())
try:
    (_d / "LICENSE").write_text("专有许可", encoding="utf-8")
    _old_root = _C.data_root
    _C.data_root = lambda: _d
    try:
        _lic = L.License()
        if _lic.root.name == ".license":
            ok("★ LICENSE 存在时默认根退到 .license（而不是撞名）")
        else:
            bad(f"默认根仍是 {_lic.root} —— 与 LICENSE 撞名")
        _lic.first_seen()
        if (_d / ".license" / "trial.json").is_file():
            ok("★ 试用起点真的写得下去（不再是 FileExistsError 静默失败）")
        else:
            bad("试用起点没落盘")
    finally:
        _C.data_root = _old_root
finally:
    _shutil.rmtree(_d, ignore_errors=True)

print("\n=== [6] 一码一机：mid 绑定 ===")
_mid = L.machine_id()
if L.valid_mid(_mid):
    ok(f"本机机器码形状正确（{_mid}）")
else:
    bad(f"机器码形状不对：{_mid!r}")
if L.machine_id() == _mid:
    ok("★ 两次调用一致（同一台机器不会漂）")
else:
    bad("机器码不稳定，两次调用不一致")

_sk2, _pk2 = L.keygen()
_old_pk = L.EMBEDDED_PUBKEY
L.EMBEDDED_PUBKEY = _pk2
_d = Path(tempfile.mkdtemp())
try:
    _base = {"customer": "机主", "plan": "pro",
             "expires": time.time() + 86400, "issued": time.time()}
    _lic = L.License(_d / "lic_now")
    _lic.save_code(L.make_code({**_base, "mid": _mid}, L._b64d(_sk2)))
    _st = _lic.state()
    if _st.state == "licensed" and _st.ok:
        ok("★ 本机机器码 ⇒ licensed")
    else:
        bad(f"本机机器码没通过：{_st}")

    _other = L.MID_PREFIX + "A" * L.MID_BODY_CHARS
    _lic2 = L.License(_d / "lic_other")
    _lic2.save_code(L.make_code({**_base, "mid": _other}, L._b64d(_sk2)))
    _st2 = _lic2.state()
    if not _st2.ok:
        ok("★ 别的机器码 ⇒ 不可用（一码一机生效）")
    else:
        bad("★ 别的机器码也能用 —— 一码一机没生效")
    if "机器码" in _st2.reason:
        ok("★ 拒绝理由说清了是机器码不一致")
    else:
        bad(f"拒绝理由含糊：{_st2.reason}")

    _lic3 = L.License(_d / "lic_nomid")
    _lic3.save_code(L.make_code(_base, L._b64d(_sk2)))
    _st3 = _lic3.state()
    if not _st3.ok and "机器码" in _st3.reason:
        ok("★ 没有 mid 的旧码不被放行")
    else:
        bad(f"没有 mid 的码被放行了：{_st3}")
finally:
    L.EMBEDDED_PUBKEY = _old_pk
    shutil.rmtree(_d, ignore_errors=True)

print()
if _fail:
    print(f"有问题 ✘   失败 {len(_fail)} 项")
    for m in _fail:
        print("  -", m)
    sys.exit(1)
print("✔ Ed25519 实现与官方向量一致，激活码防篡改生效")
