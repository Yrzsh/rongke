# -*- coding: utf-8 -*-
"""订阅 v2 的发行侧单源：凭证形状、幂等台账、续期、对账。

════════════════════════════════════════════════════════════════════════
为什么这一层放在 tools/，而不是改 rongke/license.py
═══════════════════════════════════════════════════════════════════════
**新码让 0.1.8 的老内核一行不改就能认。** 凭证里始终带 `expires`
（= starts_at + duration_days × 86400），而 starts_at / duration_days /
order_id / channel / seats / plan_id / entitlements 只是附加的审计与对账字段。
老内核只读 `expires / plan / mid / customer`，不认识的新字段解出来自然忽略。

`plan` 写的是**中文可读档位**（"个人·年费"），所以授权页那句
`if (d.plan) 显示「套餐：X」` 不用改前端就直接说人话。

⇒ 本轮不动 `rongke/*.py`、不动 `workspace.js`，也就不需要重出
   已公开发布的 0.1.8 三件发行件。这是范围控制，不是偷懒。

═══════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════
1. **不替模型厂商收钱**：凭证里不许出现任何额度 / 余额 / 价格字段。
   软件授权与 token 成本是两回事，混在一起就会出现"买 $29 得 $30 额度"
   那种自己都算不出的东西（价格表默认空 ⇒ 成本是 UNKNOWN）。
2. **幂等键是 order_id**：支付方会重投回调，同一订单只签一张码。
3. **台账只追加、坏行要报数**：悄悄跳过坏行会让人以为账本是完整的。
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rongke import license as L                       # noqa: E402

__all__ = [
    "PLANS", "CHANNELS", "FORBIDDEN_FIELDS",
    "plan_meta", "build_payload", "renew_payload",
    "new_order_id", "append_order", "append_payment", "read_jsonl",
    "append_jsonl", "issue_once", "reconcile",
]

#: plan_id → 中文档位、默认期限、席位、权益。days=0 表示永久。
PLANS: dict[str, dict[str, Any]] = {
    "personal_annual": {
        "label": "个人·年费", "days": 365, "seats": 1,
        "entitlements": ["桌面版使用权", "一年内更新", "预设与模板资产", "邮件支持"],
    },
    "personal_perpetual": {
        "label": "个人·买断", "days": 0, "seats": 1,
        "entitlements": ["桌面版使用权", "首年更新", "不限期使用"],
    },
    "lab": {
        "label": "课题组", "days": 365, "seats": 5,
        "entitlements": ["5 台机器", "共享课题库", "期刊模板", "优先支持"],
    },
    "institution": {
        "label": "机构", "days": 365, "seats": None,
        "entitlements": ["私有部署", "自建模型网关", "定制插件", "培训"],
    },
    "gift": {
        "label": "朋友/内测", "days": 90, "seats": 1,
        "entitlements": ["与正式版同功能", "限时赠送"],
    },
}

CHANNELS = ("paid", "gift", "internal", "renewal")

#: 凭证里禁止出现的字段。**不是黑名单式安全边界，是口径边界**：
#: 一旦有人往凭证里塞额度，就回到了"替厂商收钱"那条错路上。
FORBIDDEN_FIELDS = (
    "quota_usd", "balance", "remaining", "token_quota", "credits",
    "price_usd", "price", "amount_usd", "top_up",
)

#: 由凭证自己管理的字段，不允许 extra 覆盖（覆盖会伪造到期日）。
_OWNED_FIELDS = frozenset({
    "customer", "plan", "plan_id", "expires", "issued", "mid",
    "starts_at", "duration_days", "seats", "entitlements",
    "order_id", "channel",
})

DEFAULT_DIR = Path.home() / ".rongke" / "issuing"
DEFAULT_ORDERS = DEFAULT_DIR / "orders.jsonl"
DEFAULT_LEDGER = DEFAULT_DIR / "issued.jsonl"


def plan_meta(plan_id: str) -> dict[str, Any]:
    """取档位定义。不认识的档位**抛错**，不悄悄退化成默认档。"""
    pid = str(plan_id or "").strip().lower()
    if pid not in PLANS:
        raise ValueError(f"没有这个档位：{pid!r}（可选：{' / '.join(PLANS)}）")
    return PLANS[pid]


def build_payload(*, customer: str, plan_id: str, mid: str, order_id: str,
                  channel: str = "paid", starts_at: float | None = None,
                  duration_days: int | None = None, seats: int | None = None,
                  entitlements: list[str] | None = None,
                  issued: float | None = None,
                  extra: dict | None = None) -> dict:
    """构造 v2 凭证。**始终带 expires**，所以 0.1.8 内核直接认。

    `starts_at` 把有效期起点与**签发时刻**解耦：批量预签的码不会因为
    在发行方手里放了两周就开始计费，发行方可以按实际交付日指定起点。
    ⚠️ 离线码做不到「从客户首次激活算起」—— 那需要联网回传首次启动时间。
    客户收到后拖着不装仍然会消耗期限，这件事要在购买页说清，别假装解决了。
    `expires` 由 starts_at 推算，老内核读的正是这个值。
    """
    meta = plan_meta(plan_id)
    name = str(customer or "").strip()
    if not name:
        raise ValueError("客户名不能为空")
    machine = L.normalize_mid(mid)
    if not L.valid_mid(machine):
        raise ValueError(f"机器码形状不对：{machine!r}（应为 RK-M- 加 16 位 Base32）")
    ch = str(channel or "").strip()
    if ch not in CHANNELS:
        raise ValueError(f"渠道不对：{ch!r}（可选：{' / '.join(CHANNELS)}）")
    oid = str(order_id or "").strip()
    if not oid:
        raise ValueError("order_id 不能为空：它是幂等键，没有它无法防止重复发码")

    extra = dict(extra or {})
    overlap = sorted((set(extra) & _OWNED_FIELDS)
                     | (set(extra) & set(FORBIDDEN_FIELDS)))
    if overlap:
        raise ValueError(f"extra 不许覆盖凭证字段或塞入额度/价格：{overlap}")

    st = float(time.time() if starts_at is None else starts_at)
    days = int(meta["days"] if duration_days is None else duration_days)
    if days < 0:
        raise ValueError(f"duration_days 不能为负：{days}")
    expires = 0.0 if days == 0 else st + days * 86400

    p: dict[str, Any] = {
        "customer": name,
        "plan": meta["label"],          # 老内核直接显示这个
        "plan_id": str(plan_id).strip().lower(),
        "expires": expires,             # 老内核唯一读的到期字段
        "issued": float(time.time() if issued is None else issued),
        "mid": machine,
        "starts_at": st,
        "duration_days": days,
        "seats": meta["seats"] if seats is None else int(seats),
        "entitlements": list(meta["entitlements"] if entitlements is None
                             else entitlements),
        "order_id": oid,
        "channel": ch,
    }
    p.update(extra)
    leaked = [k for k in FORBIDDEN_FIELDS if k in p]
    if leaked:
        raise ValueError(f"凭证里出现了额度/价格字段：{leaked}")
    return p


def renew_payload(old: dict, *, order_id: str, duration_days: int = 365,
                  channel: str = "renewal", plan_id: str = "",
                  issued: float | None = None) -> dict:
    """续期：新码的 `starts_at` 接旧码的 `expires`，**不留空窗期**。

    永久码（expires=0）没有可续的起点，直接拒绝而不是猜一个。

    ⚠️ **旧码已过期时直接拒绝**：接着过期日续会签出一张「出生即过期」的码，
    那等于收了钱给一张废纸。过期客户应重新下单（起点 = 下单日），不走续期。
    """
    src = dict(old or {})
    old_exp = float(src.get("expires") or 0)
    if old_exp <= 0:
        raise ValueError("永久授权不需要续期（expires=0）")
    if old_exp <= time.time():
        raise ValueError(
            "旧码已过期：请重新下单（起点为下单日），不要接着过期的到期日续期")
    oid = str(order_id or "").strip()
    if not oid:
        raise ValueError("续期也要新 order_id（它是幂等键）")
    days = int(duration_days)
    if days <= 0:
        raise ValueError(f"续期天数必须为正：{days}")
    pid = str(plan_id or src.get("plan_id") or "").strip().lower()
    if not pid:
        raise ValueError("旧码没有 plan_id；请显式传 plan_id 再续期")
    meta = plan_meta(pid)
    machine = L.normalize_mid(src.get("mid") or "")
    if not L.valid_mid(machine):
        raise ValueError("旧码的机器码不对，续期拒绝")
    ch = str(channel or "renewal").strip()
    if ch not in CHANNELS:
        raise ValueError(f"渠道不对：{ch!r}")
    p = {
        "customer": str(src.get("customer") or ""),
        "plan": meta["label"],
        "plan_id": pid,
        "expires": old_exp + days * 86400,
        "issued": float(time.time() if issued is None else issued),
        "mid": machine,
        "starts_at": old_exp,
        "duration_days": days,
        "seats": src.get("seats", meta["seats"]),
        "entitlements": list(src.get("entitlements") or meta["entitlements"]),
        "order_id": oid,
        "channel": ch,
    }
    leaked = [k for k in FORBIDDEN_FIELDS if k in p]
    if leaked:
        raise ValueError(f"续期凭证里出现了额度/价格字段：{leaked}")
    return p


def new_order_id() -> str:
    return "RK-ORD-" + time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper()


def read_jsonl(path: str | Path) -> tuple[list[dict], int]:
    """读 jsonl，返回 `(行, 坏行数)`。坏行数必须返回 —— 静默跳过是假完整。"""
    p = Path(path)
    if not p.is_file():
        return [], 0
    out: list[dict] = []
    bad = 0
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("不是对象")
            out.append(row)
        except (ValueError, TypeError):
            bad += 1
    return out, bad


def append_jsonl(path: str | Path, row: dict) -> None:
    """只追加。发行侧是单机人工工具，并发由「同一 order_id 只签一次」兜住。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_order(path: str | Path, *, order_id: str, customer: str,
                 email: str = "", mid: str = "", plan_id: str = "",
                 amount: float | None = None, channel: str = "paid",
                 note: str = "") -> dict:
    """记一笔**待付款订单**。只记单，不收款、不代表已入账。

    台账只追加、绝不改行：收款确认是**另追加一行** `kind=payment`
    （见 `append_payment`），不是把这一行的 paid 改掉 —— 改行会让
    「这笔钱什么时候确认的」这条信息消失，对账就没得查了。
    """
    oid = str(order_id or "").strip()
    if not oid:
        raise ValueError("订单号不能为空")
    row = {
        "ts": time.time(), "kind": "order", "order_id": oid,
        "customer": str(customer or "").strip(),
        "email": str(email or "").strip(),
        "mid": L.normalize_mid(mid),
        "plan_id": str(plan_id or "").strip().lower(),
        "amount": amount,
        "channel": str(channel or "paid"),
        "note": str(note or ""),
    }
    append_jsonl(path, row)
    return row


def append_payment(path: str | Path, *, order_id: str, amount: float,
                   txn_id: str, note: str = "") -> dict:
    """支付回调**验签通过之后**追加一行收款确认。

    ★ 这个函数的调用方必须是回调处理器，不能是界面。金额取回调里的
      实际到账，不取前端传来的数（`docs/产品说明.md` 早就把这条接缝写清了）。
    ★ 同一笔交易重投回调时由 `txn_id` 去重，见 `reconcile` 的
      `duplicate_payments`。
    """
    oid = str(order_id or "").strip()
    tid = str(txn_id or "").strip()
    if not oid or not tid:
        raise ValueError("order_id 与 txn_id 都不能为空（txn_id 是幂等键）")
    row = {
        "ts": time.time(), "kind": "payment", "order_id": oid,
        "amount": float(amount), "txn_id": tid, "note": str(note or ""),
    }
    append_jsonl(path, row)
    return row


def issue_once(ledger_path: str | Path, payload: dict,
               signer: Callable[[dict], str], *,
               order_id: str = "") -> tuple[str, bool, dict]:
    """按 order_id 幂等签发，返回 `(激活码, 是否复用了旧记录, 台账记录)`。

    `signer` 是注入的签发函数：发行工具传真实的 Ed25519 签名器，
    判据传自生成的临时密钥 —— 测试不需要本机的发行私钥。
    """
    oid = str(order_id or payload.get("order_id") or "").strip()
    if not oid:
        raise ValueError("order_id 不能为空")
    rows, bad = read_jsonl(ledger_path)
    for old in rows:
        if str(old.get("order_id") or "") == oid:
            code = str(old.get("code") or "")
            if not code:
                raise ValueError(f"台账里订单 {oid} 的记录没有激活码，账本坏了")
            return code, True, old
    code = signer(payload)
    if not isinstance(code, str) or not code.strip():
        raise ValueError("签发器没有返回激活码")
    rec = {
        "ts": time.time(), "order_id": oid, "code": code.strip(),
        "customer": payload.get("customer", ""),
        "mid": payload.get("mid", ""),
        "plan_id": payload.get("plan_id", ""),
        "plan": payload.get("plan", ""),
        "channel": payload.get("channel", ""),
        "starts_at": payload.get("starts_at"),
        "duration_days": payload.get("duration_days"),
        "expires": payload.get("expires"),
    }
    append_jsonl(ledger_path, rec)
    return code.strip(), False, rec


def reconcile(orders_path: str | Path,
              issued_path: str | Path) -> dict:
    """订单台账 × 签发台账对账。

    按 `kind` 区分：`order` 是待付款订单，`payment` 是回调验签通过的
    收款确认（只追加，不改行）。**已收款但没发码**是最该抓的那件事。

    `clean=True` 要求：每笔已收款订单恰好一张码、没有孤儿码、
    没有重复订单号 / 重复发码 / 重复交易号、收款能对应到订单、
    两个台账都没有坏行。
    """
    orders, bad_o = read_jsonl(orders_path)
    issued, bad_i = read_jsonl(issued_path)

    def _key(row):
        return str(row.get("order_id") or "").strip()

    def _dups(xs):
        seen, out = set(), []
        for x in xs:
            if x in seen and x not in out:
                out.append(x)
            seen.add(x)
        return out

    # 没有 kind 的历史行按订单处理（旧台账也能对）。
    order_rows = [r for r in orders
                  if r.get("kind", "order") == "order" and _key(r)]
    pay_rows = [r for r in orders if r.get("kind") == "payment" and _key(r)]
    order_ids = [_key(r) for r in order_rows]
    paid_ids = [_key(r) for r in pay_rows]
    issued_ids = [_key(r) for r in issued if _key(r)]
    txn_ids = [str(r.get("txn_id") or "").strip() for r in pay_rows]

    oset, iset = set(order_ids), set(issued_ids)
    unissued = [o for o in dict.fromkeys(paid_ids) if o not in iset]
    orphan = [o for o in dict.fromkeys(issued_ids) if o not in oset]
    payment_without_order = [o for o in dict.fromkeys(paid_ids)
                             if o not in oset]
    dup_o = _dups(order_ids)
    dup_i = _dups(issued_ids)
    dup_p = [t for t in _dups(txn_ids) if t]

    report = {
        "orders": len(order_rows), "payments": len(pay_rows),
        "issued": len(issued),
        "matched": len([o for o in dict.fromkeys(paid_ids) if o in iset]),
        "unissued": unissued,
        "payment_without_order": payment_without_order,
        "orphan_codes": orphan,
        "duplicate_orders": dup_o,
        "duplicate_codes": dup_i,
        "duplicate_payments": dup_p,
        "bad_order_lines": bad_o,
        "bad_issued_lines": bad_i,
    }
    report["clean"] = not (unissued or payment_without_order or orphan
                           or dup_o or dup_i or dup_p or bad_o or bad_i)
    return report
