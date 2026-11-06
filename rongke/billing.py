# -*- coding: utf-8 -*-
"""账单骨架 —— 套餐、配额、用量台账、余额。

═══════════════════════════════════════════════════════════════════════
这一层是什么，不是什么
═══════════════════════════════════════════════════════════════════════
**是**：一个能本地验证到底的计量与额度系统。它记下每次任务花了多少钱、
扣了哪个套餐的多少额度、还剩多少、什么时候用完。

**不是**：支付系统。这里没有一行代码会扣真实银行卡。
"充值"只是往本地台账写一条记录 —— 它是**占位**，不是入账。

为什么这样做：支付需要真实商户号、webhook 回调、PCI 合规，
这些在这台机器上无法验证。写一个假装能收钱但从不验证的模块，
比不写更危险 —— 它会让人以为"付费链路已经通了"。

═══════════════════════════════════════════════════════════════════════
三条从 `pricing.py` 继承的纪律
═══════════════════════════════════════════════════════════════════════
一、**未知不是零。** 成本算不出来时是 `UNKNOWN`，不是 `0.0`。
    `0` 断言"这次不要钱"，`UNKNOWN` 陈述"我不知道"。把前者当后者用，
    余额会静默地永不减少 —— 那是最坏的一种账单缺陷：它看起来正常。

二、**假设要写在结果旁边。** 返回的每份账单都带 `assumptions`，
    说明用了哪张价格表、缓存怎么算。不让人去翻代码猜。

三、**扣不动就说扣不动。** 额度不足时抛 `InsufficientBalance`，
    不静默放行、也不静默失败。是否阻断由调用方决定（默认阻断）。

═══════════════════════════════════════════════════════════════════════
存储
═══════════════════════════════════════════════════════════════════════
`<data_root>/billing/`
  · `account.json`  —— 账户与当前套餐
  · `ledger.jsonl`  —— 只追加的流水（每次扣费/充值一行）

为什么用 jsonl 追加而不是就地改一个总数：
  · 追加不会因为进程被杀而写出半个文件（改总数会）；
  · 流水本身是审计材料 —— 能重放出余额，"余额对不上"才有得查；
  · `balance()` 从流水**算**出来，而不是读一个可能早就漂了的字段。
    这与本项目"能算的就不许靠记"的定律一致。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pricing import UNKNOWN, add_cost, format_cost

__all__ = [
    "PLANS", "InsufficientBalance", "Account", "LedgerEntry",
    "Billing", "PLAN_ORDER",
]


# ════════════════════════════════════════════════════════════════════
# 套餐
# ════════════════════════════════════════════════════════════════════

#: 套餐表。`quota_usd` 是该套餐**总额度**（美元），`None` 表示不限。
#: `price_usd` 是标价 —— 注意：**这里不收费**，它只用于展示与对账。
#:
#: ★ 这些数字是**占位**，不是定价决策。真实定价要看成本结构、竞品、
#:   目标用户支付意愿 —— 那三件事这里一件都没有。写成占位而不是
#:   编一组看起来专业的数字，是为了不让人误以为它们经过测算。
PLANS: dict[str, dict[str, Any]] = {
    "free": {
        "name": "免费",
        "price_usd": 0.0,
        "quota_usd": 2.0,
        "seats": 1,
        "features": ["对话与写作", "文献检索（受限）", "每日本地任务 20 次"],
        "note": "额度足够试完主要功能。用完可以换套餐，不会自动扣费。",
    },
    "pro": {
        "name": "专业版",
        "price_usd": 29.0,
        "quota_usd": 30.0,
        "seats": 1,
        "features": ["全部内置工具", "配图与 PPT", "插件系统", "不限任务次数"],
        "note": "单人研究者。额度按成本转售，不做超额溢价。",
    },
    "lab": {
        "name": "实验室版",
        "price_usd": 99.0,
        "quota_usd": 120.0,
        "seats": 5,
        "features": ["专业版全部", "5 个席位", "共享课题库", "期刊模板定制"],
        "note": "一个课题组共用一套课题库与模板。",
    },
    "enterprise": {
        "name": "机构版",
        "price_usd": None,     # None = 面议
        "quota_usd": None,     # None = 不限
        "seats": None,
        "features": ["不限席位", "私有部署", "自建模型网关", "定制插件与图型"],
        "note": "需要私有部署与模型网关对接，定价面议。",
    },
}

#: 套餐的展示顺序（从低到高）。列表顺序就是"升级"方向。
PLAN_ORDER = ["free", "pro", "lab", "enterprise"]


class InsufficientBalance(RuntimeError):
    """额度不足。消息要能直接印给人看。"""

    def __init__(self, msg: str, *, balance: Any = None, needed: Any = None):
        super().__init__(msg)
        self.balance = balance
        self.needed = needed


# ════════════════════════════════════════════════════════════════════
# 跨进程文件锁
# ════════════════════════════════════════════════════════════════════

import contextlib


def _replace_with_retry(src: Path, dst: Path, tries: int = 8) -> None:
    """把 `src` 原子替换成 `dst`，Windows 上带重试与降级。

    背景：Windows 上 `os.replace` 在**目标文件正被另一个句柄读**时会抛
    `[WinError 5] 拒绝访问`（Linux 允许 rename 一个正被读的文件）。
    并发跑任务时这就不是偶发：一个线程在读 account.json 判断有没有账户，
    另一个线程同时在 replace 它 —— 实测 20 线程里稳定挂账。

    策略：
      1. 先重试 `os.replace`（每次退避）—— 大多数情况一次就好；
      2. 仍失败则**降级为就地覆盖写**（open 'w' + write + flush）。
         这条路不是原子的，读者可能读到半个文件；但
         `_read_account_file` 有 JSON 解析失败重试，能吃下这个窗口。
      3. 走到降级时打一行 stderr —— 让"这次用了弱路径"留痕，
         而不是悄悄发生。
    """
    import time as _t
    last: Exception | None = None
    for i in range(max(1, tries)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:      # WinError 5 / 32 都落这里
            last = e
            _t.sleep(0.005 * (i + 1))
        except OSError as e:
            last = e
            _t.sleep(0.005 * (i + 1))

    # 降级：就地写。内容先在内存里定好，减少"半个文件"的窗口。
    try:
        data = src.read_bytes()
        with open(dst, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            # ★ 不删，改名（`os.replace` 不走删除路径）。
            #   仓库纪律是「内核包里零删除调用」，这条纪律不分"自己的临时文件"
            #   与"用户数据" —— 一旦开口子，下一个删错的就会是用户数据。
            #   固定废弃名 ⇒ 同名覆盖 ⇒ 最多留一份，不会随运行次数累积。
            os.replace(src, dst.with_name(dst.name + ".abandoned"))
        except OSError:
            pass
        print(f"[billing] os.replace 重试 {tries} 次仍失败，已降级为就地写入"
              f"（目标：{dst.name}；原因：{last}）",
              file=__import__("sys").stderr)
    except OSError:
        raise RuntimeError(
            f"账户文件写不进去：{dst}\n"
            f"最后一次错误是 {last}。检查目录权限，或别处是否有进程锁着它。")


def _lock_path(path: Path) -> Path:
    """把"要保护的文件"映射到**系统临时目录**里的锁文件。

    为什么不把锁放在数据目录里：本仓有条硬纪律「内核包里零删除调用」，
    所以锁释放只能**改名**（`os.replace`）而不是 `unlink`。改名会留下
    `.lock.done` 这样的残留 —— 每个锁各一个、几个字节，不累积，但它们
    出现在**用户的数据目录**里，而用户不该在自己的账单文件夹里看到
    `.account.lock.done` 这种东西。

    放到临时目录后：语义不变（同一台机器上所有进程算出同一个路径），
    残留落在 `%TEMP%` 里由系统清理，用户的 `billing/` 只剩
    `account.json` 与 `ledger.jsonl` 两个文件。

    名字用**绝对路径的 sha256 前 16 位**：不能直接用文件名，因为
    `data_root` 可以被 `RONGKE_DATA` 指到别处，两台不同数据根的
    `account.json` 不能共用同一把锁。
    """
    import hashlib
    import tempfile
    try:
        key = str(Path(str(path)).resolve()).lower().encode("utf-8", "replace")
    except OSError:
        key = str(path).lower().encode("utf-8", "replace")
    h = hashlib.sha256(key).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "rongke-locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{Path(str(path)).name}.{h}.lock"


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = 10.0):
    """一把基于"独占创建锁文件"的跨进程互斥。

    为什么不用 `msvcrt.locking` / `fcntl.flock`：那两个都是**平台专属**的，
    而荣珂两边都要跑。独占创建（`O_CREAT | O_EXCL`）在两边语义一致，
    而且失败时只是"再等一会儿"，不会把进程搞挂。

    超时后**照常放行**并打一行警告 —— 宁可偶发一次竞争，也不要因为
    锁文件残留把一个能算的账永远卡死。这是有意的取舍：
    账本写坏可以靠流水重放发现，进程卡死用户只会以为软件坏了。
    """
    import time as _t

    lock = _lock_path(path)
    got = False
    deadline = _t.time() + timeout
    delay = 0.002
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii", "replace"))
            finally:
                os.close(fd)
            got = True
            break
        except (FileExistsError, PermissionError):
            if _t.time() >= deadline:
                break
            _t.sleep(delay)
            delay = min(delay * 1.5, 0.05)
        except OSError:
            break
    if not got:
        print(f"[billing] 锁文件等待超时（{lock.name}），继续写入 —— "
              f"可能出现一次竞争，流水可用重放校验", file=__import__("sys").stderr)
    try:
        yield got
    finally:
        if got:
            # 锁文件同样"改名不删"：改成固定废弃名后，**原名**就空出来了，
            # 下一个进程的 `O_CREAT|O_EXCL` 照样能创建（它要的是那个名字，
            # 不是"这个名字在磁盘上不存在"）。同名覆盖 ⇒ 不留垃圾。
            for _ in range(5):
                try:
                    os.replace(lock, lock.with_name(lock.name + ".done"))
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    _t.sleep(0.005)


# ════════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════════

@dataclass
class Account:
    """账户。`id` 一旦生成就不变 —— 流水靠它归属。

    ## 密码存的是什么

    **只存 `pbkdf2_hmac` 的盐与摘要，绝不存明文，也绝不存它的弱哈希。**
    `salt` 与 `pwd_hash` 都是 hex 字符串；校验用 `hmac.compare_digest`
    做定长比较（普通 `==` 会在第一个不同字节就返回，泄露时序信息）。

    ## 这不是一个多用户认证系统

    荣珂是**单机工具**，`account.json` 在用户自己的数据目录里，能读到这个
    文件的人本来就能改台账、改套餐、改密码。所以这里的密码**只用于
    "防止别人路过你的电脑顺手打开控制台"**，不是用来防有磁盘权限的攻击者。

    把这一点写清楚，是因为一个看起来像登录框的东西很容易让人以为
    它提供了它并不提供的保证。真要多人共用一台机器，得靠操作系统的
    用户隔离，不是靠这个文件。
    """
    id: str = ""
    email: str = ""
    plan: str = "free"
    created: float = 0.0
    #: 套餐生效的起点。额度从这一刻开始算（不是从账户创建算）。
    plan_since: float = 0.0
    #: pbkdf2 的盐（hex）。空 = 没设密码（本地免登录）。
    salt: str = ""
    #: pbkdf2-HMAC-SHA256 的摘要（hex）。空 = 没设密码。
    pwd_hash: str = ""
    #: 迭代次数。**跟着账户走**，不写死在代码里 ——
    #: 否则以后调高迭代数，老账户的密码就再也验不过了。
    pwd_iter: int = 0

    def has_password(self) -> bool:
        return bool(self.salt and self.pwd_hash)

    def to_dict(self) -> dict:
        d = {"id": self.id, "email": self.email, "plan": self.plan,
             "created": self.created, "plan_since": self.plan_since}
        # 只在设了密码时才写出这两个字段 —— 不让没设密码的账户
        # 在文件里躺两个空字符串，那会让人以为"有个空密码能过"。
        if self.has_password():
            d["salt"] = self.salt
            d["pwd_hash"] = self.pwd_hash
            d["pwd_iter"] = self.pwd_iter
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        return cls(
            id=str(d.get("id") or ""),
            email=str(d.get("email") or ""),
            plan=str(d.get("plan") or "free"),
            created=float(d.get("created") or 0.0),
            plan_since=float(d.get("plan_since") or 0.0),
            salt=str(d.get("salt") or ""),
            pwd_hash=str(d.get("pwd_hash") or ""),
            pwd_iter=int(d.get("pwd_iter") or 0),
        )


@dataclass
class LedgerEntry:
    """一条流水。**只追加，永不修改。**

    `kind` 只有三种：
      `charge`  —— 一次任务消耗（`amount` 是正数的花费）
      `grant`   —— 额度授予（套餐生效/充值占位）。`amount` 是正数的额度
      `adjust`  —— 人工校正（对账用），`amount` 可正可负，必须带 `note`
    """
    ts: float
    kind: str
    amount: Any               # float 或 UNKNOWN
    plan: str = ""
    note: str = ""
    run_id: str = ""
    model: str = ""

    def to_dict(self) -> dict:
        return {"ts": self.ts, "kind": self.kind, "amount": self.amount,
                "plan": self.plan, "note": self.note,
                "run_id": self.run_id, "model": self.model}

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerEntry":
        amt = d.get("amount")
        # UNKNOWN 在 JSON 里是字符串，要原样保留 —— 不能 float() 成 0
        if isinstance(amt, str) and amt == UNKNOWN:
            amt = UNKNOWN
        elif amt is not None:
            try:
                amt = float(amt)
            except (TypeError, ValueError):
                amt = UNKNOWN
        return cls(ts=float(d.get("ts") or 0.0),
                   kind=str(d.get("kind") or ""),
                   amount=amt if amt is not None else UNKNOWN,
                   plan=str(d.get("plan") or ""),
                   note=str(d.get("note") or ""),
                   run_id=str(d.get("run_id") or ""),
                   model=str(d.get("model") or ""))


# ════════════════════════════════════════════════════════════════════
# 账单本体
# ════════════════════════════════════════════════════════════════════

class Billing:
    """本地账单。所有状态都在 `root` 下的两个文件里，没有隐藏状态。

    `root` 默认 `<data_root>/billing`。测试里传临时目录即可完全隔离。
    """

    #: 控制台会话令牌的有效期（秒）。8 小时 = 一个工作日。
    #: 单机控制台不需要更长的窗口，也不该更短到一天里反复登。
    TOKEN_TTL = 8 * 3600

    #: 同时存在的令牌上限。单机控制台够用了：一台机器不会同时开 32 个
    #: 已登录的浏览器会话。到上限就丢最老的（见 `issue_token`）。
    MAX_TOKENS = 32

    def __init__(self, root: str | Path | None = None):
        if root is None:
            from .config import data_root
            root = data_root() / "billing"
        self.root = Path(root)
        self.account_path = self.root / "account.json"
        self.ledger_path = self.root / "ledger.jsonl"
        # 会话令牌**只活在内存里**（见 `issue_token`）：
        # 每个 `Billing` 实例各有一份 —— 控制台进程重启就都失效，
        # 这正是我们要的语义，不需要额外的撤销逻辑。
        self._tokens: dict[str, float] = {}
        self._tokens_lock = threading.Lock()

    # ── 账户 ────────────────────────────────────────────────

    def account(self) -> Account:
        """读账户。不存在就**建一个 free 账户**并落盘。"""
        a = self._read_account_file()
        if a is not None:
            return a
        return self._create_account()

    def _read_account_file(self, tries: int = 5) -> Account | None:
        """读账户文件。不存在返回 None；坏了抛错。

        ★ **必须带重试**：`_save_account` 用 `os.replace` 原子替换，
          而在 Windows 上，恰好撞上替换那一瞬间的读会发生
          `PermissionError` / 读到半个文件。并发跑任务时这不是偶发，
          是每 20 次里就有几次 —— 实测 20 线程里挂过账。
          重试几次即可；重试完还不行才认为是真坏了。
        """
        import time as _t
        for i in range(max(1, tries)):
            if not self.account_path.is_file():
                return None
            try:
                raw = self.account_path.read_text(encoding="utf-8")
            except PermissionError:
                # 正在被替换，等一下再来
                _t.sleep(0.005 * (i + 1))
                continue
            except OSError:
                _t.sleep(0.005 * (i + 1))
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                # 空文件/半个文件也是替换瞬间的典型形态，重试
                if i + 1 < tries:
                    _t.sleep(0.005 * (i + 1))
                    continue
                raise RuntimeError(
                    f"账户文件读不出来：{self.account_path}\n"
                    f"它是 JSON 坏了吗？修好它，或改名备份后重试"
                    f"（改名会让你之前的流水再也对不上账）。")
            a = Account.from_dict(d)
            if a.id:
                return a
            if i + 1 < tries:
                _t.sleep(0.005 * (i + 1))
                continue
            raise RuntimeError(
                f"账户文件里没有 id：{self.account_path}\n"
                f"这不像荣珂写的。改名备份后重试。")
        return None

    def _create_account(self, email: str = "") -> Account:
        # ★ 建户必须是**幂等**的：并发时多个进程可能同时发现"没有账户"，
        #   各自生成一个 id 并落盘 —— 于是账户 id 被最后写的那个覆盖，
        #   而先写的流水全部挂到一个不存在的 id 上（孤儿账）。
        #   所以落盘前再读一次：别人已经建好了就用它的。
        #   ★ 这里用 try 包住：坏文件应该由**上层 `account()`** 报错，
        #     建户路径内部不该因为"读到一个坏文件"而抛（那会把
        #     "文件坏了"和"建户失败"混成同一个错误，定位变难）。
        try:
            a0 = self._read_account_file()
        except RuntimeError:
            a0 = None
        if a0 is not None:
            return a0

        now = time.time()
        a = Account(id="acct_" + uuid.uuid4().hex[:16], email=email,
                    plan="free", created=now, plan_since=now)
        self._save_account(a)

        # 落盘之后再确认一次：如果这期间别人抢先写了另一个 id，
        # 以磁盘上的为准 —— 并且**不要**再追加 grant，否则会重复授予额度。
        try:
            a2 = self._read_account_file()
        except RuntimeError:
            a2 = None
        if a2 is not None and a2.id != a.id:
            return a2

        # 建户即授予 free 套餐额度 —— 否则新用户余额是 0，什么都做不了
        self._append(LedgerEntry(ts=now, kind="grant",
                                 amount=float(PLANS["free"]["quota_usd"] or 0),
                                 plan="free", note="开通免费套餐"))
        return a

    def _save_account(self, a: Account) -> None:
        """写账户文件。**带跨进程互斥**。

        ★ 为什么必须加锁，而不是靠 `os.replace` 的原子性：
          在 Windows 上，多个进程同时 `os.replace` 到**同一个目标**会抛
          `[WinError 5] 拒绝访问` —— replace 的原子性是"要么旧的要么新的"，
          但它不保证并发调用不报错。实测 20 线程下稳定挂账。
          所以用一把文件锁把"写临时文件 + 替换"这段串起来。
          锁粒度是整台机器（锁文件在同一个目录），够用且不依赖第三方包。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".account.lock"
        with _file_lock(lock):
            tmp = self.account_path.with_name(
                f".account.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            try:
                tmp.write_text(
                    json.dumps(a.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8", newline="\n")
                # ★ Windows 上 `os.replace` 会在**目标正被别的进程读**时
                #   抛 `[WinError 5] 拒绝访问`。锁只能挡住我们自己人，
                #   挡不住"另一处正在 read_text" —— 而 read 恰恰发生在
                #   `_read_account_file` 里，并发时必然撞上。
                #   所以这里重试几次；仍失败就退化成"就地写"（open 'w'），
                #   那条路的语义是"内容可能短暂读到半个文件"，
                #   而 `_read_account_file` 的重试能吃掉这个窗口。
                _replace_with_retry(tmp, self.account_path)
            finally:
                try:
                    if tmp.exists():
                        # 同上：改名不删。临时名是唯一的（`.account.<pid>.<hex>.tmp`），
                        # 所以这里废弃名也跟着唯一 —— 只有"替换没成功"时才会留一份，
                        # 正常路径上 tmp 已经被 `os.replace` 移走了，这里不执行。
                        os.replace(tmp, tmp.with_name(tmp.name + ".abandoned"))
                except OSError:
                    pass

    def set_email(self, email: str) -> Account:
        a = self.account()
        a.email = str(email or "").strip()
        self._save_account(a)
        return a

    # ── 登录（本地，非多用户） ──────────────────────────────

    #: pbkdf2 迭代数。16 万次在普通机器上约 60–120ms ——
    #: 足够让人不能"每秒试几万个密码"，又不至于让登录卡顿。
    PBKDF2_ITER = 160_000

    def set_password(self, password: str) -> Account:
        """设/改密码。空字符串 = **取消密码**（回到本地免登录）。"""
        a = self.account()
        if not password:
            a.salt = a.pwd_hash = ""
            a.pwd_iter = 0
            self._save_account(a)
            return a
        salt = os.urandom(16).hex()
        it = self.PBKDF2_ITER
        a.salt, a.pwd_iter = salt, it
        a.pwd_hash = self._hash(password, salt, it)
        self._save_account(a)
        return a

    @staticmethod
    def _hash(password: str, salt_hex: str, iterations: int) -> str:
        import hashlib
        # errors="surrogatepass"：密码里可能有非 UTF-8 能表示的字符，
        # 默认 encode 会抛 UnicodeEncodeError —— 那会让用户"设不了密码"
        # 而不知道为什么。surrogatepass 能编码任意 str。
        raw = str(password).encode("utf-8", "surrogatepass")
        return hashlib.pbkdf2_hmac(
            "sha256", raw, bytes.fromhex(salt_hex), int(iterations)).hex()

    def verify_password(self, password: str) -> bool:
        """校验密码。没设密码时**恒为 True**（本地免登录语义）。

        ★ 没设密码就返回 True 是有意的：荣珂是单机工具，默认不需要登录。
          把"没设密码"解释成"谁都进不来"会让软件在首次使用时锁死自己。
          但这意味着**设了密码才叫上了锁** —— 界面上必须说清这一点。
        """
        import hmac
        a = self.account()
        if not a.has_password():
            return True
        try:
            got = self._hash(password, a.salt, a.pwd_iter or self.PBKDF2_ITER)
        except (ValueError, TypeError):
            return False
        # 定长比较：普通 == 会在第一个不同字节返回，泄露"前几位对了"
        return hmac.compare_digest(got, a.pwd_hash)

    # ── 控制台会话 ──────────────────────────────────────────
    # 为什么需要"会话令牌"而不是每个请求都带密码：
    # 浏览器每刷新一次、前端每调一个接口都要重跑一次 pbkdf2（16 万次迭代），
    # 那是几百毫秒 × 几十个请求。令牌把"证明过身份"这件事从密码派生里
    # 解耦出来，密码只在那一次登录时被校验。

    def issue_token(self) -> str:
        """登录成功后发一个会话令牌，**存在内存里**，不落盘。

        不落盘的理由：这是个单机控制台，进程一停就该重新登录。
        把令牌写进文件反而多一个要保护的东西。

        ★ 令牌表有上限。`token_valid` 的过期清理是**惰性**的（只在有人拿
          旧令牌来时清），所以"反复登录但都不再用旧令牌"会让表无限增长
          —— 每登录一次多一条，永不释放。这不是理论问题：前端每次
          `location.reload()` 之后都会重新登录一次。
          到上限时按签发时间丢掉最老的那些（最老的本来也最该过期）。
        """
        import secrets
        tok = secrets.token_urlsafe(32)
        now = time.time()
        with self._tokens_lock:
            self._tokens[tok] = now
            if len(self._tokens) > self.MAX_TOKENS:
                # 先扔掉所有已过期的（便宜且精确），不够再按年龄丢最老的
                self._tokens = {
                    k: t for k, t in self._tokens.items()
                    if now - t <= self.TOKEN_TTL}
                while len(self._tokens) > self.MAX_TOKENS:
                    oldest = min(self._tokens, key=lambda k: self._tokens[k])
                    self._tokens.pop(oldest, None)
        return tok

    def token_valid(self, tok: str) -> bool:
        """令牌是否仍然有效。没设密码时**一律有效**（本地免登录）。"""
        if not self.account().has_password():
            return True
        if not tok:
            return False
        with self._tokens_lock:
            born = self._tokens.get(str(tok))
            if born is None:
                return False
            if time.time() - born > self.TOKEN_TTL:
                self._tokens.pop(str(tok), None)
                return False
            return True

    def drop_token(self, tok: str) -> None:
        with self._tokens_lock:
            self._tokens.pop(str(tok), None)

    def login_state(self) -> dict:
        """登录状态，给界面用。"""
        a = self.account()
        return {
            "account_id": a.id,
            "email": a.email,
            "password_set": a.has_password(),
            # 明确告诉界面"现在需不需要登录" —— 让界面不用自己推断
            "login_required": a.has_password(),
            "note": ("已设本地密码：打开控制台需要它。"
                     if a.has_password() else
                     "未设密码：本机打开控制台无需登录"
                     "（这是单机工具的默认，不是漏洞）。"),
        }

    # ── 套餐 ────────────────────────────────────────────────

    def change_plan(self, plan: str, *, note: str = "") -> Account:
        """换套餐。**授予新额度，不退还旧额度**（那是财务决策，不是代码决定）。

        这里**不收费** —— 它只写一条 grant 流水。真实扣款需要接支付网关，
        而那件事没有做过，所以刻意留空。
        """
        plan = str(plan or "").strip().lower()
        if plan not in PLANS:
            raise ValueError(f"没有这个套餐：{plan}（可选：{' / '.join(PLAN_ORDER)}）")
        a = self.account()
        old = a.plan
        now = time.time()
        a.plan = plan
        a.plan_since = now
        self._save_account(a)
        quota = PLANS[plan]["quota_usd"]
        if quota is not None:
            self._append(LedgerEntry(
                ts=now, kind="grant", amount=float(quota), plan=plan,
                note=note or f"切换套餐 {old} → {plan}（本地记账，未实际收费）"))
        else:
            self._append(LedgerEntry(
                ts=now, kind="grant", amount=0.0, plan=plan,
                note=note or f"切换套餐 {old} → {plan}（不限额度，金额记 0）"))
        return a

    def top_up(self, amount_usd: float, *, note: str = "",
               run_id: str = "") -> LedgerEntry:
        """充值**占位**：只往本地台账写一条 grant 流水。

        ★ 它不会联系任何支付服务。这个函数的 docstring 之所以这么啰嗦，
          是因为一个叫 `top_up` 的函数什么都不收钱，很容易在后续开发中
          被误当成真的入账入口接线上去。**接支付时必须替换整个函数**，
          而不是在里面补两行。

        `run_id`：**幂等键**。支付方会重投回调（网络抖动、超时重试、
        人工补发），同一笔交易重复入账等于凭空给钱。传了 `run_id` 且台账里
        已有同一 `run_id` 的 grant 时，**不再写第二条**，直接返回那条旧的。
        真接支付时把它设成支付方的交易号。

        为什么去重放在这里而不是让调用方自己查：调用方是回调处理器，
        它忘了查的后果是白送钱；而这里查一次的代价是一次台账读。
        """
        try:
            amt = float(amount_usd)
        except (TypeError, ValueError):
            raise ValueError(f"充值金额不是数字：{amount_usd!r}")
        if amt <= 0:
            raise ValueError(f"充值金额必须为正：{amt}")
        rid = str(run_id or "").strip()
        if rid:
            # `ledger()` 返回 `(entries, bad_lines)` —— 只用前者。
            # 坏行不阻断幂等检查：读不出来就当没有，宁可多记一条，
            # 也不要因为一行坏数据把整个充值入口打死。
            olds, _bad = self.ledger()
            for old in olds:
                if (old.kind == "grant" and str(old.run_id or "") == rid):
                    return old
        e = LedgerEntry(ts=time.time(), kind="grant", amount=round(amt, 8),
                        plan=self.account().plan,
                        run_id=rid,
                        note=note or "本地充值占位（未接支付网关，未实际收款）")
        self._append(e)
        return e

    # ── 流水与余额 ──────────────────────────────────────────

    def _append(self, e: LedgerEntry) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(e.to_dict(), ensure_ascii=False) + "\n"
        # 追加写：即使进程半路被杀，最坏也只丢最后一行。
        # ★ 仍然加锁 —— 不是防"半行"，而是防**交错写**：
        #   两个进程同时 append 时，Python 的写是单次 write 调用，
        #   通常不会交错，但 Windows 上多个句柄同时写同一文件时
        #   顺序不受控。实测并发下出现过一次坏行。锁很便宜，值得。
        with _file_lock(self.root / ".ledger.lock"):
            with self.ledger_path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line)

    def ledger(self) -> tuple[list[LedgerEntry], int]:
        """读全部流水。返回 `(条目, 坏行数)`。

        ★ 坏行计数必须返回：悄悄跳过坏行会让人以为账本是完整的。
        """
        if not self.ledger_path.is_file():
            return [], 0
        out: list[LedgerEntry] = []
        bad = 0
        try:
            text = self.ledger_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return [], 0
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(LedgerEntry.from_dict(json.loads(ln)))
            except (ValueError, TypeError, KeyError):
                bad += 1
        return out, bad

    def balance(self) -> dict:
        """从流水**算出**余额。不读缓存字段 —— 那是漂移的来源。

        返回：
          `granted`  —— 累计授予（float 或 UNKNOWN）
          `spent`    —— 累计消耗（float 或 UNKNOWN）
          `remaining`—— 剩余（float 或 UNKNOWN）
          `entries`  —— 流水条数
          `bad_lines`—— 坏行数（>0 说明账本被破坏过）
          `unknown_charges` —— 有多少笔消耗算不出金额。
              **这个数 >0 时，`remaining` 只是上限，不是真值。**
              必须让人看到这一点，否则"余额还很足"是假安慰。
        """
        entries, bad = self.ledger()
        granted: Any = 0.0
        spent: Any = 0.0
        unknown_charges = 0
        for e in entries:
            if e.kind == "grant":
                granted = add_cost(granted, e.amount)
            elif e.kind == "charge":
                if e.amount == UNKNOWN:
                    unknown_charges += 1
                    # UNKNOWN 的消耗无法从余额里扣 —— 那余额就只能是上限。
                    # 不把它当 0 加，而是让 spent 变 UNKNOWN ——
                    # 这样 remaining 自然也是 UNKNOWN，语义才对。
                    spent = add_cost(spent, UNKNOWN)
                else:
                    spent = add_cost(spent, e.amount)
            elif e.kind == "adjust":
                # 校正：正数加到授予，负数加到消耗
                if e.amount == UNKNOWN:
                    continue
                if float(e.amount) >= 0:
                    granted = add_cost(granted, e.amount)
                else:
                    spent = add_cost(spent, abs(float(e.amount)))
        if granted == UNKNOWN or spent == UNKNOWN:
            remaining: Any = UNKNOWN
        else:
            remaining = round(float(granted) - float(spent), 8)
        return {
            "granted": granted, "spent": spent, "remaining": remaining,
            "entries": len(entries), "bad_lines": bad,
            "unknown_charges": unknown_charges,
            "exact": remaining != UNKNOWN,
        }

    # ── 扣费 ────────────────────────────────────────────────

    def charge(self, cost: Any, *, run_id: str = "", model: str = "",
               enforce: bool = True, note: str = "") -> LedgerEntry:
        """记一笔消耗。`cost` 可以是 `UNKNOWN`。

        **UNKNOWN 也照记**：不知道花了多少钱这件事本身就是账本的一部分。
        把它当 0 跳过，账本就会越用越"充裕"——那是伪造。

        `enforce=True` 时，余额不足会抛 `InsufficientBalance`。
        余额为 UNKNOWN 时**不阻断**（无法证明不足），但会在返回值里
        留下 `unknown_charges` 让上层知道这是估算。
        """
        amt = cost
        if amt != UNKNOWN:
            try:
                amt = float(amt)
            except (TypeError, ValueError):
                amt = UNKNOWN

        # ★ 先确保账户与初始额度存在，再做余额检查。
        #   不这么做的话，**新用户第一次任务必然被拦**：`balance()` 只读流水，
        #   而"建户授予 free 额度"这件事发生在 `account()` 里 —— 检查余额时
        #   账户还没被创建，余额算出来是 0，于是第一次调用就报"额度不足"。
        #   实测踩到：free 套餐 2.0 额度，第一笔 0.5 就被拒。
        account = self.account()

        if enforce and amt != UNKNOWN:
            bal = self.balance()
            rem = bal["remaining"]
            if rem != UNKNOWN and float(rem) < float(amt):
                raise InsufficientBalance(
                    f"额度不足：本次需要 {format_cost(amt)}，"
                    f"余额只有 {format_cost(rem)}。\n"
                    f"换个套餐或充值（`rongke --billing topup 10`）后重试。",
                    balance=rem, needed=amt)

        e = LedgerEntry(ts=time.time(), kind="charge", amount=amt,
                        plan=account.plan, run_id=str(run_id or ""),
                        model=str(model or ""), note=str(note or ""))
        self._append(e)
        return e

    # ── 报告 ────────────────────────────────────────────────

    def report(self) -> dict:
        """一份完整快照，给命令行与界面用。

        `assumptions` 是**强制**的：账单不能只给数字，得说清这个数字
        建立在什么之上（哪张价格表、缓存怎么算、支付有没有接）。
        """
        a = self.account()
        bal = self.balance()
        plan_meta = PLANS.get(a.plan, {})
        entries, bad = self.ledger()
        charges = [e for e in entries if e.kind == "charge"]

        # ★ 给界面/命令行的账户视图**脱敏**：`to_dict()` 是落盘用的，
        #   它带着 `salt` / `pwd_hash` / `pwd_iter`。这些是派生出密码摘要的
        #   原料，没有任何理由送到浏览器里 —— 一个只读的 GET 端点
        #   （`/api/billing`）不该回传凭据材料，哪怕 PBKDF2 很难逆推。
        #   这里是白名单，不是黑名单：将来 `Account` 加了新字段，
        #   默认不会跟着泄露出去。
        acct = {
            "id": a.id,
            "email": a.email,
            "plan": a.plan,
            "created": a.created,
            "plan_since": a.plan_since,
            "password_set": a.has_password(),
            # 台账位置由后端给出，前端不该自己拼一条（两边就会漂）
            "ledger": str(self.ledger_path),
            "account_file": str(self.account_path),
        }

        return {
            "account": acct,
            "plan": {
                "id": a.plan,
                "name": plan_meta.get("name", a.plan),
                "price_usd": plan_meta.get("price_usd"),
                "quota_usd": plan_meta.get("quota_usd"),
                "seats": plan_meta.get("seats"),
                "features": plan_meta.get("features", []),
                "note": plan_meta.get("note", ""),
            },
            "balance": bal,
            "usage": {
                "charges": len(charges),
                "unknown_charges": bal["unknown_charges"],
            },
            "assumptions": [
                "金额来自本地价格表（`rongke --pricing` 查看/填写）。"
                "价格表为空时所有消耗记作 UNKNOWN，余额剩余量也只是上限。",
                "**本模块不接支付网关**：`top_up` 只写本地流水，不会真的收款。",
                "余额由流水累加算出，不读缓存字段。",
            ],
            "payment_connected": False,
        }


# ════════════════════════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════════════════════════

def plan_diff(current: str) -> list[dict]:
    """列出所有套餐，标出当前用的是哪个、哪些是升级。

    给界面用：用户最想知道的是"我在哪、往上走要多少"。
    """
    cur = str(current or "free").strip().lower()
    try:
        ci = PLAN_ORDER.index(cur)
    except ValueError:
        ci = 0
    out = []
    for i, k in enumerate(PLAN_ORDER):
        m = PLANS[k]
        out.append({
            "id": k, "name": m["name"], "price_usd": m["price_usd"],
            "quota_usd": m["quota_usd"], "seats": m["seats"],
            "features": list(m["features"]), "note": m["note"],
            "current": k == cur,
            "is_upgrade": i > ci,
        })
    return out
