# -*- coding: utf-8 -*-
"""发行方签发工具：一码一机（离线绑定）。

私钥只从文件或环境变量读，**不接受把它写进命令行**（shell history 会留底）：

    set RK_ISSUING_KEY=<私钥>                       # 或者用默认文件
    python tools/issue_code.py --customer "某大学" --mid RK-M-XXXXXXXXXXXXXX --days 365

默认私钥文件：`%USERPROFILE%\\.rongke\\issuing_key.txt`（不在仓库里）。
客户机器码在荣珂「试用与激活」页显示，形如 `RK-M-XXXXXXXXXXXXXX`。

输出一行 `RK1...`，把它发给客户；客户粘进同一页即可。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rongke import license as L                       # noqa: E402

# 同目录的发行侧单源（凭证形状 / 幂等台账 / 续期 / 对账）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from issuing_lib import (                             # noqa: E402
    PLANS, CHANNELS, DEFAULT_LEDGER, build_payload, renew_payload,
    issue_once, new_order_id,
)

DEFAULT_KEY = Path.home() / ".rongke" / "issuing_key.txt"


def _read_key(path: str, inline: str) -> str:
    if inline.strip():
        return inline.strip()
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(
            f"找不到私钥文件：{p}\n"
            "把它放进去（一行 base64），或用 --sk 临时传入；"
            "私钥不要提交进仓库。")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise SystemExit(f"私钥文件是空的：{p}")


def _issue_v2(a, sk: bytes, customer: str, mid: str,
              old_payload: dict) -> tuple[str, dict, bool, str]:
    """构造 v2 凭证并按 order_id 幂等签发。

    v2 码始终带 `expires`，所以 0.1.8 的老内核一行不改就能验、能显示档位。
    """
    oid = a.order_id.strip() or new_order_id()
    if a.renew.strip():
        pid = (a.plan_id.strip().lower()
               or str(old_payload.get("plan_id") or ""))
        if not pid:
            raise ValueError("旧码没有 plan_id；续期请显式给 --plan-id")
        duration = a.duration_days if a.duration_days is not None else a.days
        channel = a.channel if a.channel != "paid" else "renewal"
        payload = renew_payload(old_payload, order_id=oid,
                                duration_days=duration,
                                channel=channel, plan_id=pid)
    else:
        if not a.plan_id:
            raise ValueError("v2 签发需要 --plan-id（或用 --renew 续一张旧码）")
        payload = build_payload(customer=customer, plan_id=a.plan_id,
                                mid=mid, order_id=oid, channel=a.channel,
                                starts_at=a.starts_at,
                                duration_days=a.duration_days,
                                seats=a.seats)
    ledger = str(a.ledger or DEFAULT_LEDGER)
    code, reused, _rec = issue_once(
        ledger, payload, lambda p: L.make_code(p, sk),
        order_id=payload["order_id"])
    return code, payload, reused, ledger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="荣珂发行方签发工具（一码一机）")
    ap.add_argument("--customer", default="", help="客户名（写进凭证）")
    ap.add_argument("--mid", default="", help="客户机器码 RK-M-...（授权页可见）")
    ap.add_argument("--plan", default="pro", help="套餐名（默认 pro）")
    ap.add_argument("--days", type=int, default=365, help="有效期天数；0=永久")
    ap.add_argument("--key-file", default=str(DEFAULT_KEY),
                    help=f"私钥文件（默认 {DEFAULT_KEY}）")
    ap.add_argument("--sk", default="",
                    help="私钥（不推荐：会进 shell history）；优先用 --key-file")
    ap.add_argument("--out", default="", help="把激活码另存到一个文件")
    # ── v2 订阅凭证（不给这些参数 = 完全走 0.1.8 老路径，行为一字不变）──
    ap.add_argument("--plan-id", default="", choices=sorted(PLANS),
                    help="v2 档位；给了就签 v2 凭证")
    ap.add_argument("--order-id", default="", help="订单号（幂等键；不给就自动生成）")
    ap.add_argument("--channel", default="paid", choices=list(CHANNELS),
                    help="渠道：paid / gift / internal / renewal")
    ap.add_argument("--starts-at", type=float, default=None,
                    help="有效期起点的 unix 时间（默认下单时刻；不再是签发时刻）")
    ap.add_argument("--duration-days", type=int, default=None,
                    help="期限天数；0=永久。不给则取档位默认值")
    ap.add_argument("--seats", type=int, default=None, help="席位数；不给则取档位默认值")
    ap.add_argument("--renew", default="", help="用一张旧码续期（起点接旧码到期日）")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                    help="签发台账 jsonl（默认 ~/.rongke/issuing/issued.jsonl）")
    a = ap.parse_args(argv)

    # 续期时客户名 / 机器码从旧码里回读（旧码先验签，读不出来就不猜）。
    old_payload: dict = {}
    if a.renew.strip():
        try:
            old_payload = L.parse_code(a.renew)
        except ValueError as e:
            print(f"✘ --renew 的旧码验不过：{e}", file=sys.stderr)
            return 2

    customer = (a.customer.strip()
                or str(old_payload.get("customer") or "").strip())
    if not customer:
        try:
            customer = input("客户名: ").strip()
        except EOFError:
            customer = ""
    if not customer:
        print("✘ 客户名不能为空", file=sys.stderr)
        return 2

    mid = a.mid.strip() or str(old_payload.get("mid") or "").strip()
    if not mid:
        try:
            mid = input("客户机器码（RK-M-...）: ").strip()
        except EOFError:
            mid = ""
    mid = L.normalize_mid(mid)
    if not L.valid_mid(mid):
        print(f"✘ 机器码形状不对：{mid!r}（应为 RK-M- 加 16 位 Base32）",
              file=sys.stderr)
        return 2

    try:
        sk = L._b64d(_read_key(a.key_file, a.sk))
    except SystemExit:
        raise
    except Exception as e:                             # noqa: BLE001
        print(f"✘ 私钥不是合法 base64：{e}", file=sys.stderr)
        return 2

    derived = L._b64e(L.public_key(sk))
    if derived != L.EMBEDDED_PUBKEY:
        print("✘ 这把私钥与代码里内嵌的公钥不配对，签出来的码客户验不过。",
              file=sys.stderr)
        print(f"  私钥对应公钥：{derived[:12]}…  代码内嵌：{L.EMBEDDED_PUBKEY[:12]}…",
              file=sys.stderr)
        return 2

    # 没给任何 v2 参数 = 完全走 0.1.8 老路径（签名形状、输出、退出码都不变）。
    v2 = bool(a.renew.strip() or a.plan_id or a.order_id
              or a.starts_at is not None or a.duration_days is not None
              or a.seats is not None or a.channel != "paid")
    reused, ledger = False, ""
    if v2:
        try:
            code, payload, reused, ledger = _issue_v2(
                a, sk, customer, mid, old_payload)
        except ValueError as e:
            print(f"✘ v2 签发失败：{e}", file=sys.stderr)
            return 2
        expires = float(payload.get("expires") or 0)
    else:
        expires = 0.0 if a.days <= 0 else time.time() + a.days * 86400
        payload = {"customer": customer, "plan": a.plan,
                   "expires": expires, "issued": time.time(), "mid": mid}
        code = L.make_code(payload, sk)

    # 自检：用内嵌公钥把刚签的码验一遍，避免"签出来了但客户验不过"。
    try:
        got = L.parse_code(code)
    except ValueError as e:
        print(f"✘ 自检失败：{e}", file=sys.stderr)
        return 1
    if v2 and reused:
        # ★ 幂等命中时交付的是**台账里那张旧码**，不是本次新构造的 payload。
        #   拿本次参数去逐字段比对它，starts_at / issued 必然不同 ⇒ 真红
        #   （曾把「重投回调」误判成自检失败）。摘要与自检一律以实际交付
        #   的那张码为准；要改档位就得换订单号，不能用旧号偷偷换内容。
        payload = got
        expires = float(payload.get("expires") or 0)
    if got.get("mid") != mid or got.get("customer") != customer:
        print("✘ 自检失败：解出的客户名/机器码不一致", file=sys.stderr)
        return 1
    if v2:
        for k in ("plan_id", "order_id", "channel", "starts_at",
                  "duration_days", "expires"):
            if got.get(k) != payload.get(k):
                print(f"✘ 自检失败：v2 字段 {k} 读回不一致", file=sys.stderr)
                return 1

    print(code)
    if v2:
        print(f"\n  客户：{customer}    档位：{payload['plan']}"
              f"（{payload['plan_id']}）")
        print(f"  起点：{L._ymd(payload['starts_at'])}    "
              f"期限：{payload['duration_days'] or '永久'} 天    "
              f"到期：{'永久' if not expires else L._ymd(expires)}")
        print(f"  订单：{payload['order_id']}    渠道：{payload['channel']}"
              + ("    ★ 复用了台账里已有的码（幂等）" if reused else ""))
        print(f"  台账：{ledger}")
    else:
        print(f"\n  客户：{customer}    套餐：{a.plan}    "
              f"到期：{'永久' if not expires else L._ymd(expires)}")
    print(f"  机器码：{mid}（只在这台机器有效；换机/重装系统要新码）")
    print("  ↑ 把上面那行激活码发给客户，让他在「试用与激活」页粘贴。")

    if a.out:
        Path(a.out).write_text(code + "\n", encoding="utf-8", newline="\n")
        print(f"  已另存：{Path(a.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
