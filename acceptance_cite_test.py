# -*- coding: utf-8 -*-
"""验收标准里「要求带引注」≠「要求零引注」（W36）。

两次同类事故中的第二次（第一次是 2026-10-15 的「不含参考文献表」）：
run 59f35cb2b623 交了一篇 288 字、8-10 处引注、写作体检 100/100 的稿子，
却被判「内核实测未达标：10 处引用标记」，连改两版过不去，14 步 0 交付。

根因：_ACC_CITE_NEG 匹配到「不出现 无引注」，于是第 ④ 条要求 citations == 0。
可那句标准的意思是「不要出现**没有引注支撑**的断言」—— 它在要求**有**引注。

负控（必须保留）：真正「全文不含任何文献引用」那种标准，仍然要按零引注判。
"""
import sys

from rongke.contract import TaskContract          # noqa: F401  (保持导入面稳定)
from rongke.gate import CompletionGate

FAILS: list[str] = []


def ok(m):
    print("[ok]  ", m)


def bad(m):
    FAILS.append(m)
    print("[FAIL]", m)


def verdict(acc_text, citations):
    """造一份带 citations 的体检报告，让 _kernel_verdict 判这一条。"""
    rep = type("R", (), {})()
    rep.metrics = {"citations": citations, "has_refs": citations > 0,
                   "chars": 288, "paragraphs": 1, "sentences": 7,
                   "chars_delta_pct": 4.0}
    rep.target_chars = 300
    rep.format_for_model = lambda: "正文"
    g = CompletionGate.__new__(CompletionGate)
    return g._kernel_verdict(acc_text, rep)


print("=== 要求带引注的标准：不该参与「有没有」的判定 ===")
for a in ("文中不出现无引注支撑的具体数值或结论性断言",   # 本轮事故原句
          "全文不得引用未经核实的文献",
          "正文不出现无出处的数值",
          "文中不得有缺少引注的论断"):
    state, why = verdict(a, 8)
    if state == "unknown":
        ok(f"{a[:28]} → 不参与判定")
    else:
        bad(f"★ {a[:28]} → 被判成 {state}（{why[:50]}）—— 好稿会被打回")

print()
print("=== 负控：真正要求零引用的标准仍要拦 ===")
NEG = ("全文不含任何文献引用", "文中不得出现引用标记")
for a in NEG:
    state, why = verdict(a, 8)
    if state == "fail":
        ok(f"{a[:20]} → 8 处引注仍判 fail（刚性要求没被削弱）")
    else:
        bad(f"★ {a[:20]} → {state}，这条要求被误放了")

print()
print("=== 负控：同上，零引用时要通过（证明判据不是恒 fail）===")
for a in NEG:
    state, why = verdict(a, 0)
    if state == "pass":
        ok(f"{a[:20]} → 0 处引注判 pass")
    else:
        bad(f"★ {a[:20]} → {state}")

print()
if FAILS:
    print(f"✘ {len(FAILS)} 项失败")
    sys.exit(1)
print("✔ 「要求带引注」与「要求零引注」被分开了")
