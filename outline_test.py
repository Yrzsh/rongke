# -*- coding: utf-8 -*-
"""开题大纲（outline）判据。

★ 本文件的重点不是「字段能存进去」，而是**连线**：
  大纲存在的唯一理由是「要写什么」与「要做什么」之间那条线断了会报出来。
  所以每一条核心判据都成对写：正控（线在）＋负控（线断，必须报）。
  负控不过的判据等于没有 —— 本仓老债。
"""
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import outline as O        # noqa: E402
from rongke import topic as T          # noqa: E402

PASS = FAIL = 0
FAILS: list = []


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name}\n     期望 {want!r}\n     实得 {got!r}")
        print(f"  [FAIL] {name}\n     \u671f\u671b {want!r}\n     \u5b9e\u5f97 {got!r}")


def ok(name, cond, why=""):
    check(name + (f"（{why}）" if why else ""), bool(cond), True)


SCRATCH = Path(tempfile.mkdtemp(prefix="_outline_t_"))


def fresh(tag: str) -> str:
    """一个干净的课题，返回 slug。"""
    slug = f"t-{tag}"
    d = SCRATCH / tag
    d.mkdir(parents=True, exist_ok=True)
    T.ensure_topic(slug, title=f"课题 {tag}", question="问什么", root=d)
    return slug, d


print("\n\u2500\u2500 1. \u57fa\u672c\uff1a\u5199\u8fdb\u53bb\u3001\u8bfb\u5f97\u56de\u3001\u6587\u4ef6\u90fd\u5728 \u2500\u2500")
s1, R1 = fresh("basic")
ol1 = O.save(s1, title="蛹虫草原生质体融合", question="融合子稳定性怎么鉴定",
             thesis="融合子在传代 10 次内保持核型稳定",
             sections=[{"id": "s1", "title": "引言", "kind": "intro",
                        "intent": "为什么融合是可行路线",
                        "needs": [{"kind": "text", "what": "两条亲本路线的文献支撑"}]},
                       {"id": "s2", "title": "结果一：融合率", "kind": "result",
                        "needs": [{"kind": "table", "what": "各处理组融合率", "unit": "%"}]}],
             experiments=[{"id": "e1", "title": "PEG 介导融合", "feeds": ["s1", "s2"],
                           "method": "PEG 4000 30%", "controls": "两亲本单独再生",
                           "n": "3", "days": 14}],
             root=R1)
ok("save 回 ok", ol1["ok"])
ok("save 回 verified（读回校验过）", ol1["verified"])
check("落的是 2 节 1 实验", (ol1["sections"], ol1["experiments"]), (2, 1))
check("数据需求数对得上", ol1["needs"], 2)
ok("outline.json 真的在盘上", (R1 / s1 / O.OUTLINE_FILE).exists(), str(R1 / s1))
ok("OUTLINE.md 真的在盘上（给人看的视图）", (R1 / s1 / O.OUTLINE_MD).exists())
ok("OUTLINE.md 头一行写明「手改会被覆盖」", "手改会被下一次保存覆盖" in
   (R1 / s1 / O.OUTLINE_MD).read_text(encoding="utf-8"))
back = O.load(s1, root=R1)
check("读回来的一字不差", (back["sections"][1]["needs"][0]["what"]), "各处理组融合率")

print("\n\u2500\u2500 2. \u8bfb\u4e0d\u5230\u8981\u8bf4\u4e3a\u4ec0\u4e48\uff08\u4e0d\u5141\u8bb8\u9759\u9ed8\u5f53\u6210\u201c\u6ca1\u5199\u8fc7\u201d\uff09 \u2500\u2500")
# ★ 这一对要分开写：“课题不存在”是错误路径，“课题在、没写过大纲”是正常空缺。
ok("负控：课题本身不存在 ⇒ diagnose 要说出来",
   "课题不存在" in O.diagnose("t-nothing", root=R1))
T.ensure_topic("t-empty", title="x", root=R1)
check("★ 课题在、只是没写过大纲 ⇒ diagnose 是空串（不能合成一句话）",
      O.diagnose("t-empty", root=R1), "")
check("且 load 回 None", O.load("t-empty", root=R1), None)
(R1 / "t-broken").mkdir(parents=True, exist_ok=True)
T.ensure_topic("t-broken", title="x", root=R1)
(R1 / "t-broken" / O.OUTLINE_FILE).write_text("{不是 JSON", encoding="utf-8")
check("文件坏了 ⇒ load 仍回 None", O.load("t-broken", root=R1), None)
ok("但 diagnose 必须说出来（\"坏\"与\"空\"是两件事）",
   "读不成 JSON" in O.diagnose("t-broken", root=R1), O.diagnose("t-broken", root=R1))
ok("对账在这种情形下也说出来，而不是给一份空",
   "读不成 JSON" in O.reconcile("t-broken", root=R1).get("why", ""))

print("\n\u2500\u2500 3. \u2605\u6838\u5fc3\uff1a\u7ebf\u65ad\u4e86\u5fc5\u987b\u62a5\u51fa\u6765\uff08\u6b63\u63a7 + \u8d1f\u63a7\uff09 \u2500\u2500")
s3, R3 = fresh("gap")
O.save(s3, title="t", sections=[
    {"id": "s1", "title": "结果一", "kind": "result",
     "needs": [{"kind": "table", "what": "有实验供的数据"}]},
    {"id": "s2", "title": "结果二", "kind": "result",
     "needs": [{"kind": "table", "what": "没人供的数据"}]},
], experiments=[
    {"id": "e1", "title": "有去处的实验", "feeds": ["s1"]},
    {"id": "e2", "title": "孤儿实验", "feeds": []},
], root=R3)
rep = O.reconcile(s3, root=R3)
ok("\u2605 有实验供的那一节**不**进 starved（正控：不是全报）",
   all(x["section"] != "s1" for x in rep["starved"]))
check("\u2605 没人供的那一节进 starved（负控会红：线断了确实报了）",
      [x["section"] for x in rep["starved"]], ["s2"])
check("\u2605 孤儿实验被单独列出来（做了不知写进哪）",
      [x["id"] for x in rep["orphan_experiments"]], ["e2"])
ok("\u2605 有星点的节 ⇒ ok 为 False（对账不是永远绿）", rep["ok"] is False)
# 负控的反面：把线接上，必须转绿
O.save(s3, title="t", sections=[
    {"id": "s1", "title": "结果一", "kind": "result",
     "needs": [{"kind": "table", "what": "有实验供的数据"}]},
    {"id": "s2", "title": "结果二", "kind": "result",
     "needs": [{"kind": "table", "what": "现在有人供了"}]},
], experiments=[
    {"id": "e1", "title": "有去处的实验", "feeds": ["s1"]},
    {"id": "e2", "title": "孤儿实验", "feeds": ["s2"]},
], root=R3)
rep2 = O.reconcile(s3, root=R3)
check("\u2605 \u63a5\u4e0a\u7ebf\u4e4b\u540e starved \u5fc5\u987b\u6e05\u96f6", rep2["starved"], [])
check("\u2605 \u63a5\u4e0a\u7ebf\u4e4b\u540e\u5b64\u513f\u5fc5\u987b\u6e05\u96f6", rep2["orphan_experiments"], [])
ok("\u2605 \u63a5\u4e0a\u4e4b\u540e ok \u8981\u53d8 True（说明上面那个 False 不是恒假）", rep2["ok"])

print("\n\u2500\u2500 4. \u2605 \u81ea\u79f0\u5df2\u6709\u6570\u636e\uff0c\u53bb\u5b9e\u9a8c\u53f0\u8d26\u6838 \u2500\u2500")
from rongke import lab as L             # noqa: E402
s4, R4 = fresh("verify")
O.save(s4, title="t", sections=[
    {"id": "s1", "title": "结果", "kind": "result",
     "needs": [{"kind": "stat", "what": "转化率", "unit": "%", "status": "have"}]},
], experiments=[{"id": "e1", "title": "测", "feeds": ["s1"]}], root=R4)
r_a = O.reconcile(s4, root=R4)
check("\u2605 台账里没有这条测量 ⇒ 必须降为 unverified（不许沿用自称）",
      [x["section"] for x in r_a["unverified"]], ["s1"])
check("\u964d\u7ea7\u540e\u72b6\u6001\u4e5f\u8981\u5bf9", r_a["lines"][0]["status"], "unverified")
# 真记一条测量，再核。★ 台账的硬规矩：指标先注册（kind=metric）、测量要带菌株编号。
L.record(s4, "metric", key="转化率", label="转化率", unit="%", root=R4)
L.record(s4, "strain", code="S01", role="fusant", root=R4)
L.record(s4, "measure", code="S01", metric="转化率", value=12.5, unit="%", n=3, root=R4)
r_b = O.reconcile(s4, root=R4)
check("\u2605 \u53f0\u8d26\u91cc\u771f\u6709\u4e86 ⇒ \u5347\u4e3a done", r_b["lines"][0]["status"], "done")
check("\u2605 \u4e14\u4e0d\u518d\u8ba1\u5165 unverified", r_b["unverified"], [])
# 负控：名字对不上不许也算 done
O.save(s4, title="t", sections=[
    {"id": "s1", "title": "结果", "kind": "result",
     "needs": [{"kind": "stat", "what": "拮抗反应半径", "unit": "mm", "status": "have"}]},
], experiments=[{"id": "e1", "title": "测", "feeds": ["s1"]}], root=R4)
r_c = O.reconcile(s4, root=R4)
check("\u2605 \u8d1f\u63a7\uff1a\u6307\u6807\u540d\u5bf9\u4e0d\u4e0a ⇒ \u4ecd\u7136 unverified（不是\u201c\u53f0\u8d26\u975e\u7a7a\u5c31\u7b97\u6838\u4e0a\u201d）",
      [x["section"] for x in r_c["unverified"]], ["s1"])
# 定性观察不算数（那正是「老师说我没数据」的形态）
s4b, R4b = fresh("qualonly")
O.save(s4b, title="t", sections=[
    {"id": "s1", "title": "结果", "kind": "result",
     "needs": [{"kind": "stat", "what": "融合率", "status": "have"}]},
], experiments=[{"id": "e1", "title": "测", "feeds": ["s1"]}], root=R4b)
L.record(s4b, "strain", code="S01", role="fusant", root=R4b)
L.record(s4b, "observe", code="S01", text="三株长得慢，其余长得快", root=R4b)
r_d = O.reconcile(s4b, root=R4b)
check("\u2605 \u53ea\u6709\u5b9a\u6027\u89c2\u5bdf \u21d2 \u4ecd\u7136 unverified\uff08\u89c2\u5bdf\u4e0d\u80fd\u5192\u5145\u6570\u636e\uff09",
      [x["section"] for x in r_d["unverified"]], ["s1"])

print("\n── 4b. ★ 一条数据需求都没写的节，也是写不出来（真实跑出来的漏） ──")
s4c, R4c = fresh("emptysec")
O.save(s4c, title="t", sections=[
    {"id": "s1", "title": "结果（有数据）", "kind": "result",
     "needs": [{"kind": "table", "what": "w"}]},
    {"id": "s2", "title": "引言（一条都没写）", "kind": "intro", "needs": []},
], experiments=[{"id": "e1", "title": "e", "feeds": ["s1"]}], root=R4c)
r_e = O.reconcile(s4c, root=R4c)
check("★ 空节必须被点名", [x["id"] for x in r_e["empty_sections"]], ["s2"])
ok("★ 而且它把整份大纲拉成未通过（不能因为其它节都对就放行）", r_e["ok"] is False)
O.save(s4c, title="t", sections=[
    {"id": "s1", "title": "结果（有数据）", "kind": "result",
     "needs": [{"kind": "table", "what": "w"}]},
    {"id": "s2", "title": "引言（补上了文献）", "kind": "intro",
     "needs": [{"kind": "text", "what": "两条支撑这个研究空白的文献"}]},
], experiments=[{"id": "e1", "title": "e", "feeds": ["s1", "s2"]}], root=R4c)
r_f = O.reconcile(s4c, root=R4c)
check("★ 负控：补上之后空节清零", r_f["empty_sections"], [])
ok("★ 负控：补上之后 ok 转真（说明上面那个 False 不是恒假）", r_f["ok"])

print("\n\u2500\u2500 5. \u5f62\u72b6\u9519\u8bef\u5fc5\u987b\u629b\uff08\u4e0d\u8fdb\u76d8\uff09 \u2500\u2500")
s5, R5 = fresh("shape")


def raises(fn, label):
    try:
        fn()
    except (ValueError, KeyError) as e:
        ok(label, True, str(e)[:40])
        return
    ok(label, False, "\u7adf\u7136\u6ca1\u629b")


raises(lambda: O.build(sections=[{"id": "s 1", "title": "x", "kind": "result"}]),
       "id \u91cc\u6709\u7a7a\u683c ⇒ \u629b")
raises(lambda: O.build(sections=[{"id": "s1", "title": "x", "kind": "nope"}]),
       "\u672a\u77e5\u7ae0\u8282\u7c7b\u578b ⇒ \u629b")
raises(lambda: O.build(sections=[{"id": "s1", "title": "x", "kind": "result",
                                  "needs": [{"kind": "bogus", "what": "w"}]}]),
       "\u672a\u77e5\u6570\u636e\u5f62\u6001 ⇒ \u629b")
raises(lambda: O.build(sections=[{"id": "s1", "title": "x", "kind": "result",
                                  "needs": [{"kind": "table", "what": ""}]}]),
       "\u6570\u636e\u9700\u6c42\u6ca1\u5199\u8981\u4ec0\u4e48 ⇒ \u629b\uff08\u7a7a\u884c\u4e0d\u53eb\u9700\u6c42\uff09")
raises(lambda: O.build(sections=[{"id": "s1", "title": "x", "kind": "result"},
                                 {"id": "s1", "title": "y", "kind": "result"}]),
       "\u8282 id \u91cd\u590d ⇒ \u629b\uff08\u5bf9\u8d26\u9760 id \u8fde\u7ebf\uff0c\u91cd\u4e86\u5c31\u8fde\u9519\uff09")
raises(lambda: O.build(sections=[{"id": "s1", "title": "x", "kind": "result"}],
                       experiments=[{"id": "e1", "title": "y", "feeds": ["ghost"]}]),
       "\u2605 feeds \u6307\u5411\u4e0d\u5b58\u5728\u7684\u8282 ⇒ \u629b\uff08\u4e0d\u8bb8\u9759\u9ed8\u5f53\u6210\u5b64\u513f\uff09")
raises(lambda: O.build(sections=[{"id": "s1", "title": "x", "kind": "result"}],
                       experiments=[{"id": "e1", "title": "y", "feeds": ["s1"],
                                     "depends_on": ["ghost"]}]),
       "depends_on \u6307\u5411\u4e0d\u5b58\u5728\u7684\u5b9e\u9a8c ⇒ \u629b")
raises(lambda: O.save("s-nope-topic", title="t", sections=[], root=R5),
       "\u8bfe\u9898\u4e0d\u5b58\u5728 ⇒ \u629b")
raises(lambda: O.build(sections=[{"id": f"s{i}", "title": "x", "kind": "result"}
                                 for i in range(O.MAX_SECTIONS + 1)]),
       "\u8d85\u8fc7\u8282\u6570\u4e0a\u9650 ⇒ \u629b\uff08\u518d\u591a\u5c31\u6ca1\u4eba\u8bfb\u4e86\uff09")

print("\n\u2500\u2500 6. \u5b64\u513f\u4fdd\u62a4\uff1a\u5220\u4e00\u8282\uff0c\u6302\u5728\u4e0b\u9762\u7684\u6570\u636e\u9700\u6c42\u4e0d\u80fd\u51ed\u7a7a\u6d88\u5931 \u2500\u2500")
s6, R6 = fresh("orphan")
O.save(s6, title="t", sections=[
    {"id": "s1", "title": "\u5f15\u8a00", "kind": "intro", "needs": []},
    {"id": "s2", "title": "\u7ed3\u679c\u4e00", "kind": "result",
     "needs": [{"kind": "table", "what": "\u5f88\u8d35\u7684\u90a3\u5f20\u8868"}]},
], experiments=[{"id": "e1", "title": "e", "feeds": ["s2"]}], root=R6)
r6 = O.save(s6, title="t", sections=[
    {"id": "s1", "title": "\u5f15\u8a00", "kind": "intro", "needs": []},
], experiments=[{"id": "e1", "title": "e", "feeds": ["s2"]}], root=R6)
ok("\u2605 \u88ab\u5220\u90a3\u8282\u88ab\u4fdd\u7559\u4e0b\u6765\u4e86\uff08\u56de\u5305\u91cc\u660e\u8bf4\uff09",
   r6["carried_orphan_sections"] == ["s2"], str(r6["carried_orphan_sections"]))
ol6 = O.load(s6, root=R6)
ok("\u2605 \u5b83\u6807\u4e86 orphan=True\uff08\u4e0d\u662f\u5077\u5077\u7559\u4e0b\u800c\u4e0d\u544a\u8bc9\uff09",
   [s for s in ol6["sections"] if s["id"] == "s2"][0]["orphan"] is True)
ok("\u2605 \u5b83\u90a3\u6761\u6570\u636e\u9700\u6c42\u8fd8\u5728\uff08\u6ca1\u88ab\u5220\u6389\uff09",
   [s for s in ol6["sections"] if s["id"] == "s2"][0]["needs"][0]["what"] == "\u5f88\u8d35\u7684\u90a3\u5f20\u8868")
check("\u6b64\u523b\u5f52\u6863\u90a3\u4e2a\u5b9e\u9a8c\u7684\u8fde\u7ebf\u4ecd\u5728\uff08\u56e0\u4e3a\u8282\u8fd8\u5728\uff09",
      [s for s in ol6["sections"] if s["id"] == "s2"][0] is not None, True)
ok("\u5bf9\u8d26\u628a\u5b64\u513f\u8282\u5355\u72ec\u5217\u51fa\u6765", O.reconcile(s6, root=R6)["orphan_sections"] == ["s2"])

print("\n\u2500\u2500 7. \u56db\u7c7b\u7ae0\u8282\u67e5\u6f0f\uff08\u53ea\u67e5\u6f0f\uff0c\u4e0d\u5f3a\u5236\u586b\u5185\u5bb9\uff09 \u2500\u2500")
s7, R7 = fresh("kinds")
O.save(s7, title="t", sections=[
    {"id": "s1", "title": "引言", "kind": "intro", "needs": []},
    {"id": "s2", "title": "结果", "kind": "result", "needs": []},
], experiments=[], root=R7)
mk = [k["kind"] for k in O.reconcile(s7, root=R7)["missing_kinds"]]
check("\u2605 \u53ea\u5199\u4e86\u5f15\u8a00\u4e0e\u7ed3\u679c ⇒ \u62a5\u51fa\u7f3a\u65b9\u6cd5\u4e0e\u8ba8\u8bba", sorted(mk), ["discussion", "method"])
s7b, R7b = fresh("kinds2")
O.save(s7b, title="t", sections=[{"id": f"s{i}", "title": "x", "kind": k, "needs": []}
                                 for i, k in enumerate(O.SECTION_KINDS)], root=R7b)
check("\u56db\u7c7b\u9f50\u5168 ⇒ \u4e0d\u62a5\u7f3a\uff08\u8bf4\u660e\u4e0a\u9762\u4e0d\u662f\u6052\u62a5\uff09",
      O.reconcile(s7b, root=R7b)["missing_kinds"], [])

print("\n\u2500\u2500 8. \u7f3a\u6570\u636e\u6392\u6210\u5b9e\u9a8c\u4efb\u52a1\uff1a\u53ea\u5199 todo\uff0c\u4e0d\u9020\u6d4b\u91cf \u2500\u2500")
s8, R8 = fresh("tolab")
O.save(s8, title="t", sections=[
    {"id": "s1", "title": "结果一", "kind": "result",
     "needs": [{"kind": "table", "what": "融合率"}]},
    {"id": "s2", "title": "结果二", "kind": "result",
     "needs": [{"kind": "table", "what": "已有数据的那个"}]},
], experiments=[{"id": "e1", "title": "e", "feeds": ["s2"]}], root=R8)
before = len(L.events(s8, root=R8))
pl = O.plan_to_lab(s8, root=R8)
check("\u2605 \u53ea\u7ed9\u63a5\u4e0d\u4e0a\u7ebf\u7684\u90a3\u4e00\u6761\u6392 todo", len(pl["added"]), 1)
ok("\u6392\u51fa\u6765\u7684 todo \u91cc\u5199\u4e86\u54ea\u4e00\u8282", "结果一" in pl["added"][0], pl["added"][0])
ev8 = L.events(s8, root=R8)
check("\u2605 \u53f0\u8d26\u91cc\u591a\u4e86 1 \u6761\u4e8b\u4ef6", len(ev8) - before, 1)
check("\u2605 \u4e14\u90a3\u4e00\u6761\u662f todo\uff0c**\u4e0d\u662f measure**",
      [e["kind"] for e in ev8 if e["kind"] == "todo"].count("todo") >= 1
      and len([e for e in ev8 if e["kind"] == "measure"]) == 0, True)
pl2 = O.plan_to_lab(s8, root=R8)
check("\u2605 \u5e42\u7b49\uff1a\u518d\u6392\u4e00\u6b21\u4e0d\u91cd\u590d\u5199", (pl2["added"], len(pl2["skipped"])), ([], 1))
check("\u2605 \u6392\u5b8c\u4e4b\u540e\u53f0\u8d26\u91cc\u4ecd\u7136 0 \u6761\u6d4b\u91cf\uff08\u6ca1\u6709\u51ed\u7a7a\u9020\u6570\u636e\uff09",
      len([e for e in L.events(s8, root=R8) if e["kind"] == "measure"]), 0)

print("\n\u2500\u2500 9. \u4e0e\u8bfe\u9898\u53f0\u8d26\u5171\u5b58\uff1a\u4e0d\u52a8 PLAN/READING/EVIDENCE \u2500\u2500")
s9, R9 = fresh("coexist")
T.write_doc(s9, "plan", "# 我原来的计划\n- 保留我手写的东西\n", mode="replace", root=R9)
O.save(s9, title="t", sections=[{"id": "s1", "title": "x", "kind": "result"}],
       experiments=[], root=R9)
ok("\u2605 \u5199\u4e86\u5927\u7eb2\u4e4b\u540e\uff0c\u624b\u5199\u7684 PLAN.md \u4e00\u5b57\u4e0d\u53d8",
   "\u4fdd\u7559\u6211\u624b\u5199\u7684\u4e1c\u897f" in T.read_doc(s9, "plan", root=R9)["text"])
md = T.read_topic(s9, root=R9)
ok("\u8bfe\u9898\u5143\u6570\u636e\u6ca1\u88ab\u52a8", md["title"] == "\u8bfe\u9898 coexist")
ok("topic.json \u91cc\u6ca1\u88ab\u585e\u8fdb\u4e00\u6574\u4efd\u5927\u7eb2\uff08\u5b83\u81ea\u5df1\u4e00\u4efd\u6587\u4ef6\uff09",
   "sections" not in md)

print("\n\u2500\u2500 10. \u5355\u6e90\uff1a\u5b57\u5178\u4e0e\u6e32\u67d3\u7528\u7684\u662f\u540c\u4e00\u6279\u5e38\u91cf \u2500\u2500")
check("\u8282\u7c7b\u578b 4 \u7c7b", len(O.SECTION_KINDS), 4)
check("\u6570\u636e\u5f62\u6001 5 \u79cd", len(O.DATA_KINDS), 5)
check("MARKDOWN \u6e32\u67d3\u91cc\u7684\u8282\u7c7b\u578b\u540d\u4e0e\u5b57\u5178\u4e00\u81f4",
      "结果" in O.render_md(s7, O.load(s7, root=R7)), True)
s10, R10 = fresh("render")
O.save(s10, title="t", sections=[
    {"id": "s1", "title": "结果", "kind": "result",
     "needs": [{"kind": "table", "what": "没人供的数据"}]}],
    experiments=[], root=R10)
ok("渲染出来的表里能看见断线信号（人看的那份里也要有）",
   "无实验供应" in O.render_md(s10, O.load(s10, root=R10)), "断线要写成醒目的一小格")
check("\u6e32\u67d3\u662f\u786e\u5b9a\u7684\uff08\u540c\u4e00\u4efd\u5b57\u5178\u6e32\u4e24\u6b21\u4e00\u6837\uff09",
      O.render_md(s3, O.load(s3, root=R3)).replace(
          O.load(s3, root=R3).get("updated", ""), ""),
      O.render_md(s3, O.load(s3, root=R3)).replace(
          O.load(s3, root=R3).get("updated", ""), ""))

print("\n\u2500\u2500 11. \u5b89\u5168\u8fb9\u754c\uff1a\u8def\u5f84\u4e0d\u8d8a\u754c\u3001\u5185\u6838\u5305\u96f6\u5220\u9664\u8c03\u7528 \u2500\u2500")
check("\u975e\u6cd5 slug \u21d2 \u8def\u5f84 None", O.path_of("../../etc", root=R1), None)
check("\u4e0d\u5b58\u5728\u7684\u8bfe\u9898 \u21d2 \u8def\u5f84 None", O.path_of("t-zzz", root=R1), None)
src = (ROOT / "rongke" / "outline.py").read_text(encoding="utf-8")
ok("\u2605 outline.py \u91cc\u6ca1\u6709 os.unlink / os.remove\uff08hygiene \u90a3\u6761\u5224\u636e\u540c\u53e3\u5f84\uff09",
   "os.unlink" not in src and "os.remove" not in src and "shutil.rmtree" not in src)
ok("\u2605 \u7528\u7684\u662f\u56fa\u5b9a\u505c\u8f66\u4f4d + os.replace", "os.replace" in src)

shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n\u5408\u8ba1 {PASS + FAIL} \u9879 / {FAIL} \u5931\u8d25")
if FAIL:
    print("\u5931\u8d25\u6e05\u5355\uff1a")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("\u5168\u90e8\u901a\u8fc7 \u2714")
