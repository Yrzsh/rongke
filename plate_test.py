# -*- coding: utf-8 -*-
"""plate_qc 判据。每条都对应一个真实出现过或差点出现的错。"""
import sys

sys.path.insert(0, r"D:\桌面\荣珂")
from rongke import plate as P

PASS = FAIL = 0


def ok(name, cond, extra=""):
    """标记必须**行首**写 [PASS] / [FAIL] —— regress.py 的 _RE_MARK 只认行首，
    否则整套被当成「空跑」（rc=0 但一条断言都没扫到）。
    我第一版写成 FAIL name 就是这个下场。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print("[PASS] " + name)
    else:
        FAIL += 1
        print("[FAIL] " + name + ("  " + str(extra) if extra else ""))


def eq(name, got, want):
    ok(name, got == want, "got=%r want=%r" % (got, want))


def lay(n=8):
    return {"high": ["A%d" % i for i in range(1, n + 1)],
            "low": ["H%d" % i for i in range(1, n + 1)]}


def clean():
    v = {"A%d" % i: str(1.20 + 0.005 * i) for i in range(1, 9)}
    v.update({"H%d" % i: str(0.10 + 0.001 * i) for i in range(1, 9)})
    return v


print("── 1. nan/inf 不许静默进均值 ──")
# 这是上游 plateqc 的真缺陷：float("nan") 不抛异常，Z' 变 nan，warnings 为空，
# 于是「Z'>0.5 合格」静默变 False、「Z'<0.5 告警」永不触发。
for bad in ("nan", "NaN", "inf", "-inf", "INF"):
    v = clean()
    v["A1"] = bad
    r = P.run_wells(v, lay())
    ok("nan/inf %r: Z' 仍可算" % bad, r["assay"]["zprime"] is not None)
    ok("nan/inf %r: 被记入 missing" % bad, "A1" in r["missing"])
    ok("nan/inf %r: 有告警" % bad, len(r["warnings"]) > 0)

print("── 2. 删失值与缺失值必须分桶 ──")
for tok in ("ND", "nd", "<0.05", ">4.0", "BQL", "TNTC", "<=1e-3"):
    v = clean()
    v["H1"] = tok
    r = P.run_wells(v, lay())
    ok("删失 %r 进 censored" % tok, "H1" in r["censored"], r["censored"])
    ok("删失 %r 不进 missing" % tok, "H1" not in r["missing"])
for tok in ("", "NA", "n/a", "null", "none", "-", "--", "xyz"):
    v = clean()
    v["H1"] = tok
    r = P.run_wells(v, lay())
    ok("缺失 %r 进 missing" % tok, "H1" in r["missing"], r["missing"])

print("── 3. 对照组不足时不许说「通过」──")
# 真实使用撞到的自相矛盾：开头「板质控通过 Z'=0.93」，
# 结尾「对照组只有 3 个有效孔 —— Z' 不可信」。同一段回包前后打架。
v = clean()
v["A1"] = "nan"
v["A2"] = "nan"
v["A3"] = "nan"
v["A4"] = "nan"
v["A5"] = "nan"
v["H1"] = "ND"
v["H2"] = "ND"
v["H3"] = "ND"
v["H4"] = "ND"
v["H5"] = "ND"
r = P.run_wells(v, lay())
s = P.summary("x", r)
ok("对照不足时 summary 不写「板质控通过」", "板质控通过" not in s, s[:80])
ok("对照不足时 summary 写「没过」", "没过" in s)

print("── 4. 空白对照全挂 ⇒ Z' 不可算（不是 0）──")
r = P.run_wells({"A1": "1.0"}, {"high": ["A1"], "low": ["H1"]})
eq("算不出来时 zprime 是 None", r["assay"]["zprime"], None)
ok("不是 0", r["assay"]["zprime"] != 0)

print("── 5. 只判不改 ──")
# 把难看的孔剔掉重算会让 Z' 变好看但失真。这里确认工具不会那么做：
# 传入脏数据后，报告里的 n 必须反映真实有效孔数，而不是被补齐。
v = clean()
v["A2"] = "nan"
r = P.run_wells(v, lay())
eq("high 组有效孔数按实际计（8 孔有 1 个 nan ⇒ 7）", r["groups"]["high"]["n"], 7)

print("── 6. 数字要给到孔号 ──")
v = clean()
v["B5"] = "nan"
r = P.run_wells(v, lay())
ok("告警里带孔号 B5", any("B5" in w for w in r["warnings"]), r["warnings"])

print("── 7. CV 超 15% 要点名 ──")
v = clean()
v["A1"] = "2.0"
v["A2"] = "0.9"
r = P.run_wells(v, lay())
s = P.summary("x", r)
ok("CV 超限时说「复孔 CV 超 15%」", "复孔 CV 超 15%" in s, s)

print("── 8. 真跑那次的数要能复算 ──")
# 2026-11-16 真实运行 run/20261116-191957，模型报 Z'=0.94、SSMD=56.7、
# 高值均值 1.2159 CV 1.6%、低值均值 0.1008 CV 2.5%。这里复算同样输入。
vals = {"A1": "1.234", "A2": "1.198", "A3": "nan", "A4": "1.241",
        "A5": "1.205", "A6": "1.219", "A7": "1.188", "A8": "1.226",
        "H1": "0.102", "H2": "<0.05", "H3": "0.099", "H4": "ND",
        "H5": "0.105", "H6": "0.098", "H7": "0.101", "H8": "0.100",
        "B1": "0.412", "B2": "0.988", "B3": "0.135", "B4": "0.764",
        "B5": "nan", "B6": "0.221"}
r = P.run_wells(vals, lay())
eq("Z' 复算到 0.94", round(r["assay"]["zprime"], 2), 0.94)
eq("SSMD 复算到 56.7", round(r["assay"]["ssmd"], 1), 56.7)
eq("high n=7", r["groups"]["high"]["n"], 7)
eq("low n=6", r["groups"]["low"]["n"], 6)
eq("high 均值 1.2159", round(r["groups"]["high"]["mean"], 4), 1.2159)
eq("low 均值 0.1008", round(r["groups"]["low"]["mean"], 4), 0.1008)
eq("missing 是 A3 和 B5", sorted(r["missing"]), ["A3", "B5"])
eq("censored 是 H2 和 H4", sorted(r["censored"]), ["H2", "H4"])
# 负控：这块板被判定为「可以用」，所以 summary 必须写通过 —— 否则上面的判据
# 只是在检验「它会不会永远说不合格」。
s = P.summary("x", r)
ok("这块板 8 个对照都在 ⇒ summary 写「通过」", "板质控通过" in s, s[:80])

print()
print("合计 %d 项 / %d 失败" % (PASS + FAIL, FAIL))
print("全部通过 ✔" if FAIL == 0 else "有失败 ✘")
sys.exit(1 if FAIL else 0)
