"""离线授权：签名激活码。**不需要服务器，不需要联网。**

## 授权管的是什么（先分清这件事）

这里管的是**软件本身的授权**：你能不能运行这个程序。
它**不管**模型 API 的开销 —— 用户自己填 API key，token 的钱是直接付给
模型厂商的，不经过荣珂，也不经过这套授权。所以：

- 激活码里**没有** token 额度，也不该有。有的话就等于我们替厂商收钱。
- 用户换了更大的 key、跑得更狠，与授权状态无关。
- 账单模块（`billing.py`）里那张余额表是**成本估算**，给用户自己看的
  花费计，不是收款凭据。两者不该混在一个界面概念里。

## 为什么要自己做，而不是引个库

内核的 `dependencies = []` 是设计决定，不是遗漏：荣珂要能在断网机器上
`pip install` 完就跑。为了授权拉一个第三方库进来，等于为了卖软件破坏它
最大的卖点。所以 Ed25519 在这里是**纯标准库实现**（RFC 8032 的
参考算法直译），并用官方测试向量自证正确性 —— 见 `license_test.py`。

## 形状

- 私钥留在**你手里**，签一张凭证发给买家。
- 公钥**内嵌在代码里**（`EMBEDDED_PUBKEY`）。买家改不动它 —— 改了就验不过。
- 凭证是一段 base64url 的 JSON + 签名，买家粘贴到
  `rongke --license <激活码>` 即可，**全程离线**。
- 凭证明文可读（客户名、套餐、到期日）。这不是加密，是**防篡改**：
  买家能看到自己买了什么，但改一个字就验不过。

## 能防什么，不能防什么

**防**：改到期日、改套餐、伪造凭证、把别人的凭证换个名字用。

**不防**：懂 Python 的人删掉校验那一行。这是解释型语言的固有边界，
所有不加密狗的商业软件都一样。所以这里的目标是**让诚实的人走正道**，
不是拦住铁了心要破解的人 —— 后者只能靠法务，不是靠代码。

## 三态，不是两态

`state()` 返回 `licensed` / `trial` / `expired`，而不是一个布尔。
"没用过"和"用到期了"必须分开：前者要给完整的试用期，后者要拦，
把两者混成一个 `False` 会让新装的人也立刻被拦。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, NamedTuple

# ══════════════════════════════════════════════════════════════════
#  Ed25519（RFC 8032 参考算法，纯标准库）
# ══════════════════════════════════════════════════════════════════
#
# 实现说明：这是 RFC 8032 第 6 节的直译，用 Python 大整数做域运算。
# 它不快（验一个签名约几毫秒），但**正确性可以对着官方向量验**，
# 而授权校验一辈子只跑几次，性能从来不是问题。

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _sha512_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _xrecover(y: int) -> int:
    """从 y 坐标恢复 x（RFC 8032 §5.1.3）。"""
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_By = (4 * pow(5, _P - 2, _P)) % _P
_Bx = _xrecover(_By)
_B = (_Bx % _P, _By % _P, 1, (_Bx * _By) % _P)

# 预计算表：授权校验只做验签，基线点的倍点不需要，但签名工具要。
# 这里保持最朴素的实现，把"能读懂"放在"跑得快"前面。


def _edwards_add(p: tuple, q: tuple) -> tuple:
    """Extended coordinates 上的点加（RFC 8032 §5.1.4）。"""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (t1 * 2 * _D * t2) % _P
    d = (z1 * 2 * z2) % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalarmult(p: tuple, e: int) -> tuple:
    if e == 0:
        return (0, 1, 1, 0)
    q = _scalarmult(p, e // 2)
    q = _edwards_add(q, q)
    if e & 1:
        q = _edwards_add(q, p)
    return q


def _encode_point(p: tuple) -> bytes:
    x, y, z, _t = p
    zi = pow(z, _P - 2, _P)
    x, y = (x * zi) % _P, (y * zi) % _P
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8))
                 for i in range(32))


def _decode_point(s: bytes) -> tuple:
    if len(s) != 32:
        raise ValueError("点长度不是 32 字节")
    y = sum((s[i] >> j & 1) << (8 * i + j) for i in range(32) for j in range(8))
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    x = _xrecover(y)
    if (x & 1) != sign:
        x = _P - x
    if (-x * x + y * y - 1 - _D * x * x * y * y) % _P != 0:
        raise ValueError("这个点不在曲线上")
    return (x, y, 1, (x * y) % _P)


def public_key(sk: bytes) -> bytes:
    """从 32 字节私钥推出公钥。"""
    if len(sk) != 32:
        raise ValueError("私钥必须是 32 字节")
    h = hashlib.sha512(sk).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= (1 << 254)
    return _encode_point(_scalarmult(_B, a))


def sign(msg: bytes, sk: bytes) -> bytes:
    """签一条消息，返回 64 字节签名。"""
    if len(sk) != 32:
        raise ValueError("私钥必须是 32 字节")
    h = hashlib.sha512(sk).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= (1 << 254)
    pk = _encode_point(_scalarmult(_B, a))
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % _L
    big_r = _encode_point(_scalarmult(_B, r))
    k = int.from_bytes(hashlib.sha512(big_r + pk + msg).digest(),
                       "little") % _L
    s = (r + k * a) % _L
    return big_r + s.to_bytes(32, "little")


def verify(msg: bytes, sig: bytes, pk: bytes) -> bool:
    """验签。**任何异常都当失败** —— 验签不该因为坏输入抛栈。"""
    try:
        if len(sig) != 64 or len(pk) != 32:
            return False
        big_r, s = sig[:32], int.from_bytes(sig[32:], "little")
        if s >= _L:
            return False
        a = _decode_point(pk)
        k = int.from_bytes(hashlib.sha512(big_r + pk + msg).digest(),
                           "little") % _L
        left = _scalarmult(_B, s)
        right = _edwards_add(_decode_point(big_r), _scalarmult(a, k))
        return _encode_point(left) == _encode_point(right)
    except Exception:                                  # noqa: BLE001
        return False


# ══════════════════════════════════════════════════════════════════
#  授权凭证
# ══════════════════════════════════════════════════════════════════

#: 内嵌公钥（base64url）。换发行私钥时必须同步换这里。
#: 留空 = 这份源码没有发行能力，只认已经发出去的凭证。
#: ★ 用 `rongke --license keygen` 生成一对密钥，把**公钥**填这里，
#:   私钥留在你手上（别提交进仓库）。
EMBEDDED_PUBKEY = "hv139BqkN5K7Y-O9Z5DEWBp72nvI9rJFH_5IbdLTxJk"

#: 激活码前缀。有这个前缀才能和别的 base64 串区分开，
#: 用户在命令行粘错东西时能立刻看出"这不像激活码"。
CODE_PREFIX = "RK1"

#: 试用期（天）。从**首次启动**算，不是从安装算。
TRIAL_DAYS = 30

#: 机器码前缀与长度。一码一机：客户把这一行发来，发行方签进凭证的 `mid`。
#: 机器码是 MachineGuid（或 fallback）的 SHA-256 截断 + Base32，
#: 不可反推出原始 GUID，也不随重启/重装荣珂变化；换机/重装系统会变。
MID_PREFIX = "RK-M-"
MID_BODY_CHARS = 16


def normalize_mid(value: str) -> str:
    """统一机器码的写法：去掉空白、转大写。"""
    return "".join(str(value or "").split()).upper()


def valid_mid(value: str) -> bool:
    """形状校验：`RK-M-` + 16 个 Base32 字符。"""
    s = normalize_mid(value)
    if not s.startswith(MID_PREFIX):
        return False
    body = s[len(MID_PREFIX):]
    return len(body) == MID_BODY_CHARS and all(
        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in body)


def machine_id() -> str:
    """本机机器码（稳定、无 PII、不可反推）。

    Windows 用 `HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid`；
    其它平台用 `/etc/machine-id` 或 MAC 地址兜底。最终值只暴露哈希，
    不把 MachineGuid 原样交给发行方或写进日志。
    """
    raw = ""
    if os.name == "nt":
        try:
            import winreg
            flags = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography", 0,
                                flags) as k:
                raw = str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip()
        except Exception:                                  # noqa: BLE001
            raw = ""
    if not raw:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                raw = Path(p).read_text(encoding="utf-8").strip()
            except OSError:
                raw = ""
            if raw:
                break
    if not raw:
        raw = f"node:{uuid.getnode():012x}"
    digest = hashlib.sha256(("rongke-machine:" + raw).encode("utf-8")).digest()
    body = base64.b32encode(digest[:10]).decode("ascii").rstrip("=")
    return MID_PREFIX + body[:MID_BODY_CHARS]


_UNSET = object()


class LicenseState(NamedTuple):
    """授权状态。**三态**，不是布尔 —— 见模块 docstring。"""

    state: str          # "licensed" | "trial" | "expired"
    ok: bool            # state 是 licensed 或 trial
    customer: str       # 客户名（凭证里的）
    plan: str           # 套餐名（凭证里的）
    expires: float      # 到期 unix 时间；0 = 永久
    days_left: int      # 剩余天数；永久为 -1
    reason: str         # 给人看的一句话
    machine_id: str = ""  # 本机机器码（一码一机），总是给界面看

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "ok": self.ok, "customer": self.customer,
                "plan": self.plan, "expires": self.expires,
                "days_left": self.days_left, "reason": self.reason,
                "machine_id": self.machine_id}


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_code(payload: dict, sk: bytes) -> str:
    """把 payload 签成激活码。**发行方工具**，买家不该有私钥。"""
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    sig = sign(body, sk)
    return f"{CODE_PREFIX}.{_b64e(body)}.{_b64e(sig)}"


def parse_code(code: str, pk: bytes | None = _UNSET) -> dict:
    """验一个激活码，返回 payload。**验不过就抛 ValueError**。

    这里刻意抛异常而不是返回 None：调用方必须明确处理"验不过"，
    静默返回 None 会让人写出 `if payload:` 而漏掉报错。
    """
    code = (code or "").strip()
    parts = code.split(".")
    if len(parts) != 3 or parts[0] != CODE_PREFIX:
        raise ValueError(f"这不像激活码（应以 {CODE_PREFIX}. 开头，共三段）")
    try:
        body = _b64d(parts[1])
        sig = _b64d(parts[2])
    except Exception as e:                             # noqa: BLE001
        raise ValueError(f"激活码不是合法的 base64：{e}")
    key = EMBEDDED_PUBKEY if pk is _UNSET else (pk or b"")
    if not key:
        raise ValueError(
            "这份代码里没有内嵌公钥，验不了激活码。"
            "（发行方需要用 `rongke --license keygen` 生成密钥并填 "
            "`rongke/license.py` 的 EMBEDDED_PUBKEY）")
    pk_bytes = _b64d(key) if isinstance(key, str) else key
    if not verify(body, sig, pk_bytes):
        raise ValueError("激活码验签失败 —— 要么是伪造的，要么被改过")
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as e:                             # noqa: BLE001
        raise ValueError(f"激活码内容不是合法 JSON：{e}")
    if not isinstance(payload, dict):
        raise ValueError("激活码内容必须是一个对象")
    return payload


class License:
    """授权管理器。凭证与试用起点都存在数据根下的 `license/`。"""

    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            from .config import data_root
            base = data_root()
            # ★ Windows 不区分大小写：源码树根目录有一个 LICENSE 文件，
            #   base/"license" 会指向它，mkdir 直接 FileExistsError ——
            #   试用起点写不下去、激活码也存不了。遇到这种撞名就退到 .license。
            cand = base / "license"
            if cand.exists() and not cand.is_dir():
                cand = base / ".license"
            root = cand
        self.root = Path(root)
        self._code_path = self.root / "code.txt"
        self._trial_path = self.root / "trial.json"

    # ── 读写（都不删文件，改名为 .abandoned）────────────────────

    def _read(self, p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _write(self, p: Path, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, p)

    def code(self) -> str:
        return self._read(self._code_path).strip()

    def save_code(self, code: str) -> None:
        """存激活码。**先验再存** —— 存进去一个验不过的东西只会让人困惑。"""
        parse_code(code)          # 验不过就抛，不落盘
        self._write(self._code_path, code.strip() + "\n")

    def clear_code(self) -> None:
        """清掉激活码。按本仓零删除纪律，是改名而不是删。"""
        p = self._code_path
        if p.exists():
            os.replace(p, p.with_name(p.name + ".abandoned"))

    # ── 试用期 ───────────────────────────────────────────────

    def first_seen(self) -> float:
        """首次启动时间。**第一次问就写下来**，之后只读。"""
        raw = self._read(self._trial_path)
        if raw.strip():
            try:
                d = json.loads(raw)
                ts = float(d.get("first_seen") or 0)
                if ts > 0:
                    return ts
            except Exception:                          # noqa: BLE001
                pass
        ts = time.time()
        try:
            self._write(self._trial_path,
                        json.dumps({"first_seen": ts}, ensure_ascii=False))
        except OSError:
            # 写不下去（只读盘、权限不足）就**不让它变成硬错误**：
            # 返回当前时间，这一次仍按"试用第 0 天"走。
            # 把"记不下"升级成"打不开"是可用性事故。
            pass
        return ts

    # ── 状态 ─────────────────────────────────────────────────

    def state(self, now: float | None = None) -> LicenseState:
        """算出当前授权状态。这是**唯一**的判定入口。"""
        now = time.time() if now is None else now
        code = self.code()
        if code:
            try:
                payload = parse_code(code)
            except ValueError as e:
                # 激活码坏了：不静默当"没授权"，要说清楚为什么，
                # 否则用户只会看到"要激活"而不知道是自己的码坏了。
                return self._trial_state(now, extra=f"激活码无效：{e}")
            expires = float(payload.get("expires") or 0)
            customer = str(payload.get("customer") or "")
            plan = str(payload.get("plan") or "")
            this_mid = machine_id()
            code_mid = normalize_mid(payload.get("mid") or "")
            # 一码一机：没有 mid 的旧码不再放行（0.1.1 起签的码都带 mid），
            # 否则「一码一机」只对新码成立、旧码仍是全网通用。
            if not code_mid:
                return LicenseState(
                    state="expired", ok=False, customer=customer, plan=plan,
                    expires=expires, days_left=0,
                    reason="激活码没有绑定机器码，请向发行方索取本机激活码",
                    machine_id=this_mid)
            if code_mid != this_mid:
                return LicenseState(
                    state="expired", ok=False, customer=customer, plan=plan,
                    expires=expires, days_left=0,
                    reason="激活码绑定的机器码与本机不一致 —— 请把本机机器码发给发行方换码",
                    machine_id=this_mid)
            if expires and now > expires:
                return LicenseState(
                    state="expired", ok=False, customer=customer,
                    plan=plan,
                    expires=expires, days_left=0,
                    reason=f"授权已于 {_ymd(expires)} 到期",
                    machine_id=this_mid)
            return LicenseState(
                state="licensed", ok=True,
                customer=customer, plan=plan,
                expires=expires,
                days_left=(-1 if not expires
                           else max(0, int((expires - now) // 86400))),
                reason=("永久授权" if not expires
                        else f"授权到 {_ymd(expires)}"),
                machine_id=this_mid)
        return self._trial_state(now)

    def _trial_state(self, now: float, extra: str = "") -> LicenseState:
        start = self.first_seen()
        used = max(0.0, now - start)
        left = TRIAL_DAYS * 86400 - used
        days = int(left // 86400) if left > 0 else 0
        base = ""
        if extra:
            base = extra + "；"
        this_mid = machine_id()
        if left > 0:
            return LicenseState(
                state="trial", ok=True, customer="", plan="",
                expires=start + TRIAL_DAYS * 86400, days_left=days,
                # 用"还剩 N 天"而不是 days+1：刚刚开始时 left≈30 天、days=30，
                # days+1 会打出"还剩 31 天"这种比实际更长的数。
                reason=f"{base}试用中，还剩 {max(1, days)} 天",
                machine_id=this_mid)
        return LicenseState(
            state="expired", ok=False, customer="", plan="",
            expires=start + TRIAL_DAYS * 86400, days_left=0,
            reason=f"{base}试用期已结束（{TRIAL_DAYS} 天）",
            machine_id=this_mid)

    # ── 拦不拦 ───────────────────────────────────────────────

    def allows_run(self, now: float | None = None
                   ) -> "tuple[bool, LicenseState]":
        """能不能**发起新任务**。这是唯一的拦截点。

        为什么拦"发起新任务"而不是拦全部：控制台照常打开、历史照常读写，
        用户能看见自己之前做的东西 —— 被锁在门外看自己的文件会让人愤怒，
        而且那对"提醒你该买了"没有任何帮助。
        """
        st = self.state(now)
        return st.ok, st


def _ymd(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# ══════════════════════════════════════════════════════════════════
#  发行方工具（生成密钥 / 签激活码）—— 买家不用这些
# ══════════════════════════════════════════════════════════════════

def keygen() -> tuple[str, str]:
    """生成一对密钥，返回 `(私钥_b64, 公钥_b64)`。

    ★ 私钥**不要提交进仓库**。它一旦泄露，任何人都能签出有效凭证。
    """
    sk = os.urandom(32)
    return _b64e(sk), _b64e(public_key(sk))
