# -*- coding: utf-8 -*-
"""订单台账登记（订阅 v2 发行侧）。**只记单，不收款。**

    # 下单页 / 人工：登记一笔待付款订单
    python tools/order_new.py --customer "某大学" --email a@b.cn \
        --plan-id personal_annual --amount 199 --mid RK-M-XXXXXXXXXXXXXX

    # 支付回调验签通过之后（只能由回调处理器调用，不能由界面调用）
    python tools/order_new.py --confirm-payment \
        --order-id RK-ORD-20261106-XXXXXXXX --amount 199 --txn-id <支付方交易号>

⚠️ 这个工具不会联系任何支付服务，`--amount` 只是台账里的一个数字。
   **收款确认必须由回调验签后追加**，金额取回调里的实际到账，
   不取前端传来的数。`--confirm-payment` 也一样：调用方是谁，决定了
   这条记录有没有证明力 —— 工具本身证明不了。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from issuing_lib import (                             # noqa: E402
    DEFAULT_ORDERS, PLANS, append_order, append_payment, new_order_id,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="荣珂订阅 v2 订单台账（只记单，不收款）")
    ap.add_argument("--orders", default=str(DEFAULT_ORDERS),
                    help=f"订单台账 jsonl（默认 {DEFAULT_ORDERS}）")
    ap.add_argument("--order-id", default="", help="订单号；不给就自动生成")
    ap.add_argument("--confirm-payment", action="store_true",
                    help="追加一行收款确认（支付回调验签通过后调用）")
    ap.add_argument("--customer", default="", help="客户名")
    ap.add_argument("--email", default="", help="联系邮箱（只进本地台账）")
    ap.add_argument("--mid", default="", help="机器码 RK-M-...（授权页可见）")
    ap.add_argument("--plan-id", default="", choices=sorted(PLANS),
                    help="档位")
    ap.add_argument("--amount", type=float, default=None, help="金额（元，仅台账记录）")
    ap.add_argument("--txn-id", default="", help="支付方交易号（收款确认的幂等键）")
    ap.add_argument("--channel", default="paid", help="渠道标记")
    ap.add_argument("--note", default="", help="备注")
    a = ap.parse_args(argv)

    if a.confirm_payment:
        if not a.order_id.strip():
            print("✘ --confirm-payment 需要 --order-id", file=sys.stderr)
            return 2
        if not a.txn_id.strip():
            print("✘ --confirm-payment 需要 --txn-id（幂等键）", file=sys.stderr)
            return 2
        if a.amount is None:
            print("✘ --confirm-payment 需要 --amount（回调里的实际到账）",
                  file=sys.stderr)
            return 2
        row = append_payment(a.orders, order_id=a.order_id,
                             amount=a.amount, txn_id=a.txn_id, note=a.note)
        print(f"  已追加收款确认：{row['order_id']}  ¥{row['amount']}  "
              f"txn={row['txn_id']}")
        print("  ⚠️ 调用方必须是验过签的支付回调；工具本身不证明钱到账了。")
        return 0

    if not a.customer.strip():
        print("✘ 客户名不能为空", file=sys.stderr)
        return 2
    oid = a.order_id.strip() or new_order_id()
    row = append_order(a.orders, order_id=oid, customer=a.customer,
                       email=a.email, mid=a.mid, plan_id=a.plan_id,
                       amount=a.amount, channel=a.channel, note=a.note)
    print(f"  订单号：{row['order_id']}")
    print(f"  客户：{row['customer']}    档位：{row['plan_id'] or '(未指定)'}"
          f"    金额：{row['amount'] if row['amount'] is not None else '(未填)'}")
    print(f"  台账：{a.orders}")
    print("  ⚠️ 这一步只登记待付款订单：**没收款、没签发激活码**。")
    print("     支付回调验签通过后：① --confirm-payment 追加收款确认 "
          "② tools/issue_code.py --plan-id ... --order-id <上面这行> 签码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
