# -*- coding: utf-8 -*-
"""订单台账 × 签发台账对账（订阅 v2 发行侧）。

    python tools/reconcile.py                 # 用默认台账
    python tools/reconcile.py --json         # 给脚本读的结构化输出

退出码 0 = 账本干净；1 = 有未签发订单 / 孤儿码 / 重复 / 坏行。
**坏行必须报数**：悄悄跳过会让人以为账本是完整的。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from issuing_lib import DEFAULT_LEDGER, DEFAULT_ORDERS, reconcile  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="荣珂订阅 v2 双台账对账")
    ap.add_argument("--orders", default=str(DEFAULT_ORDERS),
                    help=f"订单台账（默认 {DEFAULT_ORDERS}）")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                    help=f"签发台账（默认 {DEFAULT_LEDGER}）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    a = ap.parse_args(argv)

    r = reconcile(a.orders, a.ledger)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(f"  订单 {r['orders']} 笔 · 收款确认 {r['payments']} 笔 · "
              f"签发 {r['issued']} 张 · 匹配 {r['matched']} 笔")
        print(f"  已收款未签发：{len(r['unissued'])}    "
              f"孤儿码：{len(r['orphan_codes'])}    "
              f"收款找不到订单：{len(r['payment_without_order'])}")
        print(f"  重复订单 {len(r['duplicate_orders'])} · "
              f"重复发码 {len(r['duplicate_codes'])} · "
              f"重复交易号 {len(r['duplicate_payments'])}")
        print(f"  坏行：订单台账 {r['bad_order_lines']} / "
              f"签发台账 {r['bad_issued_lines']}")
        for label, key in (("已收款未签发", "unissued"),
                           ("孤儿码", "orphan_codes"),
                           ("收款找不到订单", "payment_without_order"),
                           ("重复订单", "duplicate_orders"),
                           ("重复发码", "duplicate_codes"),
                           ("重复交易号", "duplicate_payments")):
            if r[key]:
                print(f"  ✘ {label}：{r[key]}")
        print("  ✔ 账本干净" if r["clean"] else "  ✘ 账本有问题，见上")
    return 0 if r["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
