# -*- coding: utf-8 -*-
"""订阅 v2 授权链判据：老内核兼容 + 起算点 + 幂等 + 续期 + 对账。

判据不依赖本机发行私钥：临时生成一对 Ed25519 密钥，把内嵌公钥临时换成
测试公钥，`try/finally` 还原 —— 干净 clone 上也能跑。

**正负控都要量**：只证明"能签出来"不算数，还要证明"改一个字验不过"、
"额度字段塞不进去"、"永久码不能续期"、"坏行会被数出来"。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from rongke import license as L                       # noqa: E402
import issuing_lib as IL                             # noqa: E402

_fail = 0


def ok(m):
    print(f"[PASS] {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"[FAIL] {m}")


def eq(got, want, label):
    if got == want:
        ok(label)
    else:
        bad(f"{label}：得到 {got!r}，期望 {want!r}")


def rejects(fn, label):
    """负控：调用必须抛 ValueError，不能静默放行。"""
    try:
        fn()
    except ValueError:
        ok(label)
    except Exception as e:                            # noqa: BLE001
        bad(f"{label}：抛了别的异常 {type(e).__name__}: {e}")
    else:
        bad(f"{label}：居然放行了")


def ns(**kw):
    base = dict(plan_id="", order_id="", channel="paid", starts_at=None,
                duration_days=None, seats=None, renew="", ledger="",
                days=365, plan="pro")
    base.update(kw)
    return argparse.Namespace(**base)


# ── 测试密钥：不动本机发行私钥，也不动仓库里的 EMBEDDED_PUBKEY ──
sk = os.urandom(32)
test_pub = L._b64e(L.public_key(sk))
orig_pub = L.EMBEDDED_PUBKEY
L.EMBEDDED_PUBKEY = test_pub

try:
    mid = L.machine_id()
    st = time.time() + 14 * 86400        # 模拟"交付日比签发日晚两周"

    # ── [1] 凭证形状与起算点 ──
    p = IL.build_payload(customer="测试客户", plan_id="personal_annual",
                         mid=mid, order_id="RK-ORD-T1", starts_at=st,
                         duration_days=365, issued=time.time())
    eq(p["expires"], st + 365 * 86400,
       "expires 由 starts_at + duration_days 算出，与 issued 无关")
    eq(p["starts_at"], st, "起点可由发行方按交付日指定")
    eq(p["plan"], IL.PLANS["personal_annual"]["label"], "plan 是中文可读档位")
    eq(p["plan_id"], "personal_annual", "plan_id 供对账用")
    eq(p["mid"], mid, "机器码被规范化并写进凭证")
    eq(any(k in p for k in IL.FORBIDDEN_FIELDS), False,
       "凭证里没有任何额度/余额/价格字段")

    # ── [2] ★ 老内核零改动认得 v2 码 ──
    code = L.make_code(p, sk)
    with tempfile.TemporaryDirectory() as d:
        lic = L.License(Path(d))
        lic.save_code(code)               # save_code 内部先验签
        state = lic.state()
        eq(state.state, "licensed", "0.1.8 内核把 v2 码判为已授权")
        eq(state.plan, "个人·年费", "授权页直接显示中文档位")
        eq(state.days_left >= 365, True, "晚交付不会吃掉期限（仍 ≥365 天）")
        eq(state.machine_id, mid, "一码一机判定仍然生效")

    # ── [3] 负控：篡改凭证 ──
    parts = code.split(".")
    body = L._b64d(parts[1])
    forged_body = body.replace(b'"duration_days":365', b'"duration_days":366')
    eq(forged_body != body, True, "负控前提：确实改到了 duration_days 字段")
    forged = f"{parts[0]}.{L._b64e(forged_body)}.{parts[2]}"
    try:
        L.parse_code(forged)
        bad("改了凭证字段还验得过")
    except ValueError:
        ok("负控：改一个字段 ⇒ 验签失败")

    # ── [4] 负控：口径与形状 ──
    rejects(lambda: IL.build_payload(customer="甲", plan_id="personal_annual",
                                     mid=mid, order_id="X",
                                     extra={"quota_usd": 30}),
            "负控：额度字段塞不进凭证")
    rejects(lambda: IL.build_payload(customer="甲", plan_id="personal_annual",
                                     mid=mid, order_id="X",
                                     extra={"expires": 0}),
            "负控：extra 不许覆盖 expires")
    rejects(lambda: IL.plan_meta("nosuchplan"), "负控：不认识的档位报错")
    rejects(lambda: IL.build_payload(customer="甲", plan_id="personal_annual",
                                     mid="RK-M-BAD", order_id="X"),
            "负控：坏机器码被拒")
    rejects(lambda: IL.build_payload(customer="甲", plan_id="personal_annual",
                                     mid=mid, order_id=""),
            "负控：空 order_id 被拒（幂等键不能缺）")
    rejects(lambda: IL.build_payload(customer="甲", plan_id="personal_annual",
                                     mid=mid, order_id="X", channel="hacker"),
            "负控：不认识的渠道被拒")
    rejects(lambda: IL.build_payload(customer="甲", plan_id="personal_annual",
                                     mid=mid, order_id="X", duration_days=-1),
            "负控：负天数被拒")

    # ── [5] 永久码 ──
    p0 = IL.build_payload(customer="测试客户", plan_id="personal_perpetual",
                          mid=mid, order_id="RK-ORD-T2")
    eq(p0["expires"], 0.0, "买断档 expires=0（永久）")
    eq(p0["duration_days"], 0, "买断档期限记为 0 天")

    # ── [6] ★ 幂等：同一 order_id 只签一张码 ──
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "issued.jsonl"
        signer = lambda q: L.make_code(q, sk)
        c1, reused1, _ = IL.issue_once(led, p, signer)
        c2, reused2, _ = IL.issue_once(led, p, signer)
        eq(reused1, False, "首次签发不是复用")
        eq(reused2, True, "同一订单重投时被识别为复用")
        eq(c2, c1, "重投返回同一张码")
        rows, badlines = IL.read_jsonl(led)
        eq(len(rows), 1, "台账恰好 1 行（没有重复发码）")
        eq(badlines, 0, "台账没有坏行")

    # ── [7] ★ 续期无空窗 ──
    p2 = IL.renew_payload(p, order_id="RK-ORD-T3", duration_days=365)
    eq(p2["starts_at"], p["expires"], "续期起点接旧码到期日")
    eq(p2["expires"], p["expires"] + 365 * 86400, "续期到期 = 旧到期 + 365 天")
    eq(p2["channel"], "renewal", "续期渠道标为 renewal")
    eq(p2["mid"], p["mid"], "续期沿用同一台机器")
    rejects(lambda: IL.renew_payload(p0, order_id="RK-ORD-T4"),
            "负控：永久码拒绝续期（没有可接的起点）")
    rejects(lambda: IL.renew_payload({**p, "expires": time.time() - 86400},
                                     order_id="RK-ORD-T-EXPIRED"),
            "负控：已过期旧码拒绝续期（不会签出「出生即过期」的码）")

    # ── [8] ★ 对账：干净 / 缺码 / 孤儿码 / 坏行 ──
    with tempfile.TemporaryDirectory() as d:
        orders = Path(d) / "orders.jsonl"
        led = Path(d) / "issued.jsonl"
        IL.append_order(orders, order_id="A", customer="甲", plan_id="gift",
                        amount=0, channel="gift")
        IL.append_payment(orders, order_id="A", amount=0, txn_id="TXN-A")
        IL.append_jsonl(led, {"order_id": "A", "code": "RK1.fake"})
        r = IL.reconcile(orders, led)
        eq(r["clean"], True, "干净账本判定 clean=True")
        eq(r["unissued"], [], "已收款订单都有码")

        IL.append_payment(orders, order_id="B", amount=199, txn_id="TXN-B")
        r2 = IL.reconcile(orders, led)
        eq(r2["unissued"], ["B"], "抓出「已收款但没发码」")
        eq(r2["clean"], False, "缺码时 clean=False")

        IL.append_jsonl(led, {"order_id": "ZZ", "code": "RK1.orphan"})
        r3 = IL.reconcile(orders, led)
        eq(r3["orphan_codes"], ["ZZ"], "抓出孤儿码")
        eq(r3["payment_without_order"], ["B"], "抓出收款找不到订单")

        IL.append_jsonl(orders, "{broken json")
        r4 = IL.reconcile(orders, led)
        eq(r4["bad_order_lines"], 1, "坏行被数出来，不是静默跳过")
        eq(r4["clean"], False, "有坏行时不判干净")

        # 重复交易号（回调重投）也要能抓
        IL.append_payment(orders, order_id="A", amount=0, txn_id="TXN-A")
        r5 = IL.reconcile(orders, led)
        eq(r5["duplicate_payments"], ["TXN-A"], "抓出重复交易号")

    # ── [9] ★ 发行工具真跑 _issue_v2（不依赖本机私钥）──
    spec = importlib.util.spec_from_file_location(
        "rk_issue_code", ROOT / "tools" / "issue_code.py")
    IC = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(IC)
    with tempfile.TemporaryDirectory() as d:
        led = Path(d) / "cli_issued.jsonl"
        a = ns(plan_id="personal_annual", order_id="RK-ORD-T-CLI",
               ledger=str(led), starts_at=st, duration_days=365)
        c, payload, reused, _ = IC._issue_v2(a, sk, "测试客户", mid, {})
        eq(reused, False, "工具首次签发")
        eq(L.parse_code(c)["order_id"], "RK-ORD-T-CLI", "工具签出的码可读回订单号")
        c2, _p, reused2, _ = IC._issue_v2(a, sk, "测试客户", mid, {})
        eq(reused2, True, "工具重跑同一订单命中幂等")
        eq(c2, c, "工具重跑返回同一张码")
        rows, _ = IL.read_jsonl(led)
        eq(len(rows), 1, "工具台账只有 1 行")

        # 续期路径也走一遍工具
        ar = ns(renew=c, order_id="RK-ORD-T-RENEW", ledger=str(led),
                duration_days=365)
        _c3, p3, _r3, _ = IC._issue_v2(ar, sk, "", "", L.parse_code(c))
        eq(p3["starts_at"], payload["expires"], "工具的续期起点接旧到期")

    # ── [10] CLI 子进程端到端（只在有配对私钥的本机跑，缺了如实说没测到）──
    keyfile = Path.home() / ".rongke" / "issuing_key.txt"
    paired = False
    if keyfile.is_file():
        try:
            real_sk = L._b64d(keyfile.read_text("utf-8").strip().splitlines()[0])
            paired = (L._b64e(L.public_key(real_sk)) == orig_pub)
        except Exception:                             # noqa: BLE001
            paired = False
    if not paired:
        print("· SKIP CLI 子进程端到端：本机没有与内嵌公钥配对的发行私钥"
              "（干净 clone 上属正常，此项未被测到，不计入通过数）")
    else:
        with tempfile.TemporaryDirectory() as d:
            led = Path(d) / "real_issued.jsonl"
            cp = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "issue_code.py"),
                 "--plan-id", "gift", "--order-id", "RK-ORD-T-REAL",
                 "--customer", "判据", "--mid", mid,
                 "--duration-days", "90", "--channel", "gift",
                 "--ledger", str(led), "--key-file", str(keyfile)],
                capture_output=True, text=True, cwd=str(ROOT), timeout=60)
            if cp.returncode != 0:
                bad(f"CLI 端到端退出码 {cp.returncode}：{cp.stderr[-300:]}")
            else:
                first = cp.stdout.strip().splitlines()[0]
                got = L.parse_code(first, pk=L._b64d(orig_pub))
                eq(got["plan_id"], "gift", "CLI 真签出 gift 档凭证")
                eq(got["duration_days"], 90, "CLI 真签出 90 天期限")
                eq(got["starts_at"] + 90 * 86400, got["expires"],
                   "CLI 码的 expires 与起点/期限自洽")
                _rows, _b = IL.read_jsonl(led)
                eq(len(_rows), 1, "CLI 把码写进签发台账")
    # ── [11] ★ sdist 清单自洽：tools/ 内部依赖必须都在 MANIFEST.in ──
    # 用 AST 扫顶层 import，不扫注释（扫源码文本会把注释里的名字也算进去）。
    import ast
    manifest = (ROOT / "MANIFEST.in").read_text("utf-8")
    tool_files = ("tools/issue_code.py", "tools/order_new.py",
                  "tools/reconcile.py", "tools/issuing_lib.py")
    for f in tool_files:
        eq(f in manifest, True, f"MANIFEST.in 收录了 {f}")
    local_deps = set()
    for f in tool_files:
        for node in ast.parse((ROOT / f).read_text("utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module:
                if (ROOT / "tools" / (node.module + ".py")).is_file():
                    local_deps.add(f"tools/{node.module}.py")
    eq(bool(local_deps), True,
       "AST 真的扫到了 tools/ 内部依赖（不是空扫冒充全绿）")
    eq(sorted(d for d in local_deps if d not in manifest), [],
       "sdist 清单覆盖所有 tools/ 内部依赖")
finally:
    L.EMBEDDED_PUBKEY = orig_pub

eq(L.EMBEDDED_PUBKEY, orig_pub, "测试公钥已还原（没污染进程内的原始公钥）")

print()
if _fail:
    print(f"[FAIL] 失败 {_fail} 项")
    sys.exit(1)
print("[DONE] 订阅 v2 授权链判据全部通过")
