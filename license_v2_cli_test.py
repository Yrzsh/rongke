# -*- coding: utf-8 -*-
"""订阅 v2 的 CLI 层常驻判据：命令行 → 子进程 → 台账 → 另一个工具读回。

`license_v2_test.py` 测的是库函数与 `_issue_v2`；这一层专测**参数解析、
退出码、跨工具交接**。只用临时目录，不碰用户数据、不占端口。

⚠️ 签发 / 续期那几项需要本机有与内嵌公钥配对的发行私钥。干净 clone 上
   没有时**明确打印 SKIP 且不计入通过数** —— 不把「没测到」冒充成绿。
   订单 / 收款 / 对账 / --help 这几项不依赖私钥，任何机器上都真的跑到。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
KEY = Path.home() / ".rongke" / "issuing_key.txt"
sys.path.insert(0, str(ROOT))
from rongke import license as L                       # noqa: E402

MID = L.machine_id()
_fail = 0


def ok(m):
    print(f"[PASS] {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"[FAIL] {m}")


def run(*args, expect=0):
    cp = subprocess.run([sys.executable, str(TOOLS / args[0]), *args[1:]],
                        capture_output=True, text=True, cwd=str(ROOT), timeout=60)
    if cp.returncode == expect:
        ok(f"{args[0]} 退出码 {cp.returncode} 符合预期")
    else:
        bad(f"{args[0]} 退出码 {cp.returncode}（期望 {expect}）：{cp.stderr[-300:]}")
    return cp


def key_paired() -> bool:
    if not KEY.is_file():
        return False
    try:
        raw = KEY.read_text("utf-8").strip().splitlines()[0].strip()
        sk = L._b64d(raw)
        return L._b64e(L.public_key(sk)) == L.EMBEDDED_PUBKEY
    except Exception:                                 # noqa: BLE001
        return False


with tempfile.TemporaryDirectory() as d:
    orders = Path(d) / "orders.jsonl"
    ledger = Path(d) / "issued.jsonl"
    oid = "RK-ORD-CLI-TEST"

    # ── [1] 登记待付款订单 ──
    cp = run("order_new.py", "--orders", str(orders), "--order-id", oid,
             "--customer", "判据", "--plan-id", "gift", "--amount", "0",
             "--mid", MID)
    if oid in cp.stdout:
        ok("订单工具把订单号打出来了")
    else:
        bad("订单工具没输出订单号")

    # ── [2] 回调确认收款 ──
    run("order_new.py", "--orders", str(orders), "--confirm-payment",
        "--order-id", oid, "--amount", "0", "--txn-id", "TXN-CLI-1")
    rows = [json.loads(x) for x in orders.read_text("utf-8").splitlines()]
    kinds = sorted(r.get("kind") for r in rows)
    if kinds == ["order", "payment"]:
        ok("订单台账只追加 order + payment 两行（没改旧行）")
    else:
        bad(f"订单台账形状不对：{kinds}")

    # ── [3] 缺码 ⇒ 对账必须退出 1 并点名订单 ──
    cp = run("reconcile.py", "--orders", str(orders), "--ledger", str(ledger),
             expect=1)
    if "已收款未签发" in cp.stdout and oid in cp.stdout:
        ok("对账抓出「已收款但没发码」并列出订单号")
    else:
        bad("对账没抓出缺码订单")

    # ── [4] 补上签发记录 ⇒ 对账干净、退出 0 ──
    ledger.write_text(json.dumps({"order_id": oid, "code": "RK1.test",
                                  "plan_id": "gift", "channel": "gift"},
                                 ensure_ascii=False) + "\n", encoding="utf-8")
    cp = run("reconcile.py", "--orders", str(orders), "--ledger", str(ledger))
    if "账本干净" in cp.stdout:
        ok("补发后对账判定账本干净")
    else:
        bad("补发后对账没判干净")

    # ── [5] --help 里 v2 参数都在（参数解析没被改丢）──
    cp = subprocess.run([sys.executable, str(TOOLS / "issue_code.py"), "--help"],
                        capture_output=True, text=True, cwd=str(ROOT), timeout=60)
    need = ("--plan-id", "--order-id", "--channel", "--starts-at",
            "--duration-days", "--seats", "--renew", "--ledger")
    missing = [a for a in need if a not in cp.stdout]
    if cp.returncode == 0 and not missing:
        ok("签发工具 --help 列出全部 8 个 v2 参数")
    else:
        bad(f"--help 缺参数：{missing}（rc={cp.returncode}）")

    # ── [6] 真签发 / 幂等 / 续期（需要本机配对私钥）──
    if not key_paired():
        print("· SKIP 签发 CLI 真跑 / 幂等 / 续期：本机没有与内嵌公钥配对的"
              "发行私钥（干净 clone 上属正常；这几项未被测到，不计入通过数）")
    else:
        real_ledger = Path(d) / "real_issued.jsonl"
        real_oid = "RK-ORD-CLI-REAL"
        base = ("issue_code.py", "--plan-id", "gift", "--channel", "gift",
                "--duration-days", "90", "--order-id", real_oid,
                "--customer", "判据", "--mid", MID,
                "--ledger", str(real_ledger), "--key-file", str(KEY))
        cp = run(*base)
        code = cp.stdout.strip().splitlines()[0] if cp.stdout.strip() else ""
        if code.startswith("RK1."):
            got = L.parse_code(code)
            if got.get("plan_id") == "gift" and got.get("duration_days") == 90:
                ok("CLI 真签出的码读回档位与期限")
            else:
                bad(f"CLI 码字段不对：{got.get('plan_id')} / {got.get('duration_days')}")
        else:
            bad("CLI 没输出激活码")
            code = ""
        cp2 = run(*base)
        code2 = cp2.stdout.strip().splitlines()[0] if cp2.stdout.strip() else ""
        if code and code2 == code:
            ok("同一 order_id 重跑返回同一张码（CLI 层幂等）")
        else:
            bad("CLI 层幂等失效")
        if "复用了台账里已有的码" in cp2.stdout:
            ok("CLI 明确标出这次是复用旧码（不是静默重签）")
        else:
            bad("CLI 复用时没说明，容易让人以为重新签了一张")
        lines = [x for x in real_ledger.read_text("utf-8").splitlines() if x.strip()]
        if len(lines) == 1:
            ok("CLI 台账只有 1 行（没有重复发码）")
        else:
            bad(f"CLI 台账有 {len(lines)} 行")
        if code:
            cp3 = run("issue_code.py", "--renew", code, "--order-id",
                      "RK-ORD-CLI-RENEW", "--duration-days", "90",
                      "--ledger", str(real_ledger), "--key-file", str(KEY))
            renewed = cp3.stdout.strip().splitlines()[0] if cp3.stdout.strip() else ""
            if renewed.startswith("RK1."):
                old, new = L.parse_code(code), L.parse_code(renewed)
                if (new.get("starts_at") == old.get("expires")
                        and new.get("expires") == old.get("expires") + 90 * 86400):
                    ok("CLI 续期无空窗（起点接旧到期、到期顺延 90 天）")
                else:
                    bad("CLI 续期日期不对")
            else:
                bad("CLI 续期没输出激活码")

print()
if _fail:
    print(f"[FAIL] 失败 {_fail} 项")
    sys.exit(1)
print("[DONE] 订阅 v2 CLI 层判据全部通过")
