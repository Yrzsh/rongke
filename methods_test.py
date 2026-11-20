# -*- coding: utf-8 -*-
"""方法台账 + 创新点（`rongke/methods.py`）的判据。

每条核心断言都配负控 —— 只证明「该红的时候红」，不证明「什么都红」。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rongke import methods as M          # noqa: E402
from rongke import topic as T            # noqa: E402

N = 0
BAD = 0


def ok(label, cond):
    global N, BAD
    N += 1
    if not cond:
        BAD += 1
        print(f"  [FAIL] {label}")
    else:
        print(f"  [PASS] {label}")


def eq(label, got, want):
    global N, BAD
    N += 1
    if got != want:
        BAD += 1
        print(f"  [FAIL] {label}：得到 {got!r}，应为 {want!r}")
    else:
        print(f"  [PASS] {label}")


def raises(label, fn, needle=""):
    global N, BAD
    N += 1
    try:
        fn()
    except ValueError as e:
        if needle and needle not in str(e):
            BAD += 1
            print(f"  [FAIL] {label}：抛了但话不对 —— {e}")
        else:
            print(f"  [PASS] {label}")
        return
    except Exception as e:                              # noqa: BLE001
        BAD += 1
        print(f"  [FAIL] {label}：抛的不是 ValueError —— {type(e).__name__}: {e}")
        return
    BAD += 1
    print(f"  [FAIL] {label}：**没抛**（静默进了盘）")


def fresh(name="m"):
    root = Path(tempfile.mkdtemp())
    T.ensure_topic(name, title="方法台账用例", root=root)
    return name, root


def P(name, value, src):
    return {"name": name, "value": value, "src": src}


# ── 1. 基本读写 ─────────────────────────────────────────────
print("\n── 1. 基本读写：记一个方法、落盘、读回一致 ──")
s1, R1 = fresh("m1")
r = M.record(s1, "method", root=R1, id="peg", title="PEG 促融",
             origin="adapted",
             params=[P("溶壁酶", "2.06%", "kb:kb478857a9 p.2"), P("温度", "26.1 ℃", "user")],
             steps=["取 6 天菌丝 1 g", "酶解 2 h"],
             pitfalls=["酶解超 2.5 h 后形态完整者几乎无再生力"])
ok("★ 写完读回（verified 是读回来的，不是自称）", r["verified"] is True)
st = M.state(s1, root=R1)
ok("★ 方法在盘上", "peg" in st["methods"])
eq("★ 首建即 v1（参数不会因为不写 version 就丢）",
   len(st["methods"]["peg"]["versions"]), 1)
eq("★ 而且 v1 里真的有那两条参数",
   len(st["methods"]["peg"]["latest"]["params"]), 2)
ok("METHODS.jsonl 真的存在", (R1 / "m1" / "METHODS.jsonl").exists())
ok("METHODS.md 由 write_md 渲染", M.write_md(s1, root=R1)["verified"] is True)

# ── 2. ★ 参数必须带出处（整件事的支点）──────────────────────
print("\n── 2. ★ 每个参数都必须写出处 ──")
s2, R2 = fresh("m2")
raises("缺 src ⇒ 抛（不许静默进盘）",
       lambda: M.record(s2, "method", root=R2, id="a", title="t", origin="own",
                        params=[{"name": "浓度", "value": "2%"}]),
       "缺 src")
raises("★ src 写成「我记忆中」这种自由文本 ⇒ 抛（收得越宽，编造越容易藏）",
       lambda: M.record(s2, "method", root=R2, id="b", title="t", origin="own",
                        params=[P("浓度", "2%", "我记得文献里是这个")]),
       "src 写法不认")
raises("缺 value ⇒ 抛（要写清什么值、带单位）",
       lambda: M.record(s2, "method", root=R2, id="c", title="t", origin="own",
                        params=[{"name": "浓度", "src": "user"}]),
       "缺 value")
# 负控：四种合法写法都必须能进
for i, src in enumerate(["kb:kb478857a9 p.2", "user", "design", "std:GB/T 1234-2020"]):
    M.record(s2, "method", root=R2, id=f"ok{i}", title="t", origin="own",
             params=[P("p", "1", src)])
    ok(f"负控：合法 src {src!r} 能进", f"ok{i}" in M.state(s2, root=R2)["methods"])

# ── 3. ★ 版本不可覆盖（复现失败时唯一线索）──────────────────
print("\n── 3. ★ 版本独立成条、版本号不可复用 ──")
s3, R3 = fresh("m3")
M.record(s3, "method", root=R3, id="digest", title="酶解", origin="own",
         params=[P("时长", "1.5 h", "design")])
raises("同 id 再用 kind=method ⇒ 抛（覆盖之后上一版就没了）",
       lambda: M.record(s3, "method", root=R3, id="digest", title="酶解 v2", origin="own"),
       "已存在")
raises("version 指向不存在的方法 ⇒ 抛",
       lambda: M.record(s3, "version", root=R3, id="nope", version=1, params=[]),
       "还不存在")
# 首建即 v1；所以下一版从 v2 起数
M.record(s3, "version", root=R3, id="digest", version=2,
         params=[P("时长", "2 h", "design")], what_changed="时长 1.5h → 2h")
M.record(s3, "version", root=R3, id="digest", version=3,
         params=[P("时长", "2.5 h", "design")], what_changed="时长 2h → 2.5h")
raises("★ 版本号复用 ⇒ 抛（上一版的样子要能追回去）",
       lambda: M.record(s3, "version", root=R3, id="digest", version=2, params=[]),
       "版本号不能复用")
st3 = M.state(s3, root=R3)
eq("三版都在（首建 v1 + v2 + v3）", len(st3["methods"]["digest"]["versions"]), 3)
eq("最新一版是 v3", st3["methods"]["digest"]["latest"]["version"], 3)
ok("★ 手改不进：METHODS.md 里 v1/v2/v3 三段都在",
   all(f"### v{i}" in M.render_md(s3, root=R3) for i in (1, 2, 3)))

# ── 4. ★ 说「不一样」就必须说清哪不一样 ──────────────────────
print("\n── 4. ★ differs_from 非空 ⇒ what_changed 必填 ──")
s4, R4 = fresh("m4")
raises("说区别于文献却不说改在哪 ⇒ 抛",
       lambda: M.record(s4, "method", root=R4, id="x", title="t", origin="adapted",
                        differs_from=["kb:kb478857a9"]),
       "what_changed")
M.record(s4, "method", root=R4, id="y", title="t", origin="adapted",
         differs_from=["kb:kb478857a9"], what_changed="溶壁酶 1% → 2.06%")
ok("负控：说清改在哪就放行", "y" in M.state(s4, root=R4)["methods"])

# ── 5. origin 必填（决定创新点能不能算）─────────────────────
print("\n── 5. origin 必填且只能四种 ──")
s5, R5 = fresh("m5")
raises("origin 缺失 ⇒ 抛",
       lambda: M.record(s5, "method", root=R5, id="a", title="t"),
       "origin")
raises("origin 写成「自己想的」这种自由说法 ⇒ 抛",
       lambda: M.record(s5, "method", root=R5, id="b", title="t", origin="自己想的"),
       "origin")

# ── 6. ★ 专利线索：只算三件可复算的事，不下新颖性结论 ─────────
print("\n── 6. ★ 创新点：自己的参数 / 差异写没写 / 公开日期 ──")
s6, R6 = fresh("m6")
M.record(s6, "method", root=R6, id="pure", title="纯抄文献", origin="literature",
         params=[P("a", "1", "kb:kb478857a9 p.2"), P("b", "2", "kb:kb478857a9 p.3")])
v = M.patent_view(s6, "pure", root=R6)
eq("★ 全是文献参数 ⇒ weak", v["verdict"], "weak")
eq("自己的参数 0 条", len(v["own_params"]), 0)
ok("★ 而且缺口里明确说了「没有一条参数是你自己的」",
   any("没有任何一条参数是你自己的" in g for g in v["gaps"]))
ok("★ 它明确声明自己不是新颖性结论",
   "不是" in v["note"] and "代理人" in v["note"])

M.record(s6, "method", root=R6, id="mine", title="自己设计的",
         origin="adapted", differs_from=["kb:kb478857a9"],
         what_changed="酶解改两步，26.1 ℃ 先 30 min 再 18 ℃ 1 h",
         params=[P("第一步温度", "26.1 ℃", "design"),
                 P("第二步温度", "18 ℃", "design"),
                 P("基础酶系", "Driselase 3 mg/mL", "kb:kb478857a9 p.2")])
eq("★ 首建时就带参数 ⇒ 直接能算自己的参数",
   len(M.patent_view(s6, "mine", root=R6)["own_params"]), 2)
M.record(s6, "patent", root=R6, id="mine", disclosed_at="2026-11-15",
         claims_basis=["两步变温酶解，第一步 26.1 ℃ 破壁、第二步 18 ℃ 保护再生能力"])
v2 = M.patent_view(s6, "mine", root=R6)
eq("★ 有自己的参数 + 写了差异 ⇒ 待补公开日期前是 check_first",
   v2["verdict"], "check_first")
eq("自己的参数 2 条", len(v2["own_params"]), 2)
eq("文献参数 1 条", len(v2["kb_params"]), 1)
ok("★ 没填 first_public 会被点名",
   any("first_public" in g for g in v2["gaps"]))

M.record(s6, "patent", root=R6, id="mine", disclosed_at="2026-11-15",
         first_public="2027-06-01",
         claims_basis=["两步变温酶解"])
v3 = M.patent_view(s6, "mine", root=R6)
eq("★ 填了公开日期 ⇒ published（新颖性可能受影响，必须最先说）",
   v3["verdict"], "published")
ok("★ 而且话术里保留了「务务必先说这一条」",
   "公开" in v3["verdict_cn"])

# 负控：不公开 + 参数齐 ⇒ worth_asking
M.record(s6, "method", root=R6, id="clean", title="完整创新",
         origin="own", params=[P("新参数", "X", "design")],
         what_changed="全新方法")
M.record(s6, "patent", root=R6, id="clean", disclosed_at="2026-11-15",
         first_public="", claims_basis=["全新方法"])
v4 = M.patent_view(s6, "clean", root=R6)
eq("★ 负控：自己设计 + 未公开，但线索里没填 first_public ⇒ check_first（不能直接给绿）",
   v4["verdict"], "check_first")
ok("★ 但 gaps 里仍然点名「first_public 没填」",
   any("first_public" in g for g in v4["gaps"]))

# ── 6b. ★ 一句话摘要不能与 gaps 列表自相矛盾 ──────────
print("\\n── 6b. ★ check_first 的一句话不能写死缺口类型 ──")
# 真实使用里碰到的：自己的参数 7 条、差异写了、只差 first_public，
# 但回包的一句话里仍写着「缺自己的参数」，与 gaps 对不上。
s6b, R6b = fresh("m6b")
M.record(s6b, "method", root=R6b, id="only", title="只差公开日期", origin="own",
         params=[P("a", "1", "design"), P("b", "2", "design")],
         what_changed="全新。")
M.record(s6b, "patent", root=R6b, id="only", disclosed_at="2026-11-16")
vb = M.patent_view(s6b, "only", root=R6b)
eq("★ 只差 first_public ⇒ check_first", vb["verdict"], "check_first")
ok("★ gaps 里只有 first_public 这一条（没有自己参数的缺口）",
   len(vb["gaps"]) == 1 and "first_public" in vb["gaps"][0])
ok("★ 而且一句话里**不得**出现「缺自己的参数」（与 gaps 矛盾）",
   "缺自己的参数" not in vb["verdict_cn"])

# ── 7. 作废：不改旧行，只追加一条 ───────────────────────────
print("\n── 7. 作废走 retract 事件，不改旧行 ──")
before = len(M.events(s6, root=R6))
M.record(s6, "observe", root=R6, id="pure", text="酶解 2h 后全死", failed=True)
n_obs_before = len(M.state(s6, root=R6)["methods"]["pure"]["observes"])
seq_last = M.events(s6, root=R6)[-1]["seq"]
M.record(s6, "retract", root=R6, seq=seq_last, why="那批菌丝本来就污染了，不算数")
st7 = M.state(s6, root=R6)
eq("★ 作废后不再出现在 observe 里",
   len(st7["methods"]["pure"]["observes"]), n_obs_before - 1)
ok("★ 但旧行还在盘上（append-only）",
   len(M.events(s6, root=R6)) == before + 2)
raises("retract 不给理由 ⇒ 抛（没有理由的作废等于篡改）",
       lambda: M.record(s6, "retract", root=R6, seq=1),
       "why")
raises("retract 指向不存在的 seq ⇒ 抛",
       lambda: M.record(s6, "retract", root=R6, seq=9999, why="x"),
       "没有 seq")

# ── 8. 形状错误必抛 ─────────────────────────────────────────
print("\n── 8. 形状错误必抛（不静默进盘）──")
s8, R8 = fresh("m8")
raises("非法 kind", lambda: M.record(s8, "bogus", root=R8, id="a"), "kind 只能是")
raises("非法 id（中文）",
       lambda: M.record(s8, "method", root=R8, id="酶解方法", title="t", origin="own"),
       "只能用字母")
raises("缺 title", lambda: M.record(s8, "method", root=R8, id="a", origin="own"), "title")
raises("params 不是数组",
       lambda: M.record(s8, "method", root=R8, id="b", title="t", origin="own",
                        params={"a": 1}), "必须是数组")
raises("steps 不是数组",
       lambda: M.record(s8, "method", root=R8, id="c", title="t", origin="own",
                        steps="一步"), "必须是字符串数组")
M.record(s8, "method", root=R8, id="real", title="t", origin="own")
raises("observe 的 text 为空",
       lambda: M.record(s8, "observe", root=R8, id="real", text="  "), "text")
raises("version=0 ⇒ 抛（不是 ≥1）",
       lambda: M.record(s8, "version", root=R8, id="real", version=0, params=[]),
       "≥1 的整数")
raises("★ 首建已占用 v1 ⇒ 再报 v1 要报「版本号不能复用」（而不是默默写两层）",
       lambda: M.record(s8, "version", root=R8, id="real", version=1, params=[]),
       "版本号不能复用")

# ── 9. 不存在的课题必须抛，不凭空造 ─────────────────────────
print("\n── 9. ★ 课题不存在 ⇒ 抛（不许凭空造目录）──")
R9 = Path(tempfile.mkdtemp())
raises("不存在的课题 ⇒ record 抛",
       lambda: M.record("ghost", "method", root=R9, id="a", title="t", origin="own"),
       "课题不存在")
ok("★ 而且真的没造出目录", not (R9 / "ghost").exists())
raises("不存在的课题 ⇒ events 抛",
       lambda: M.events("ghost", root=R9), "课题不存在")

# ── 10. 渲染确定性 + 与 lab_method 的分工 ───────────────────
print("\n── 10. 渲染确定性、单源 ──")
a = M.render_md(s3, root=R3)
b = M.render_md(s3, root=R3)
ok("★ 同一事件流渲两次逐字节相同", a == b)
ok("★ 渲染里能看到参数的出处（没有出处的参数等于没记）",
   "`user`" in a or "`design`" in a)
ok("★ 渲染里写明了「别手改」", "别手改" in a)
ok("★ 方法台账与实验台账是两个文件（LAB.jsonl 不被碰）",
   (R3 / "m3" / "METHODS.jsonl").exists()
   and not (R3 / "m3" / "LAB.jsonl").exists())

# ── 11. 安全边界 ───────────────────────────────────────────
print("\n── 11. 安全边界 ──")
ok("★ methods.py 里没有 os.unlink / os.remove / shutil.rmtree",
   all(t not in open(Path(__file__).parent / "rongke" / "methods.py",
                     encoding="utf-8").read()
       for t in ("os.unlink", "os.remove", "shutil.rmtree")))
ok("★ write_md 用固定停车位 + os.replace",
   "os.replace" in open(Path(__file__).parent / "rongke" / "methods.py",
                        encoding="utf-8").read())

print(f"\n合计 {N} 项 / {BAD} 失败")
if BAD:
    print("有失败 ✘")
    sys.exit(1)
print("全部通过 ✔")
