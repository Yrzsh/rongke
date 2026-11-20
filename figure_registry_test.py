# -*- coding: utf-8 -*-
"""图表登记单源判据：证明「编号是渲染产物」而不是又一层装饰。

量的是三个改造前真跑出来的错位有没有被修掉：
  A 删图 / 重排章节 ⇒ 编号与交叉引用必须自动跟着变；
  B 图与题注之间夹多少段都不影响配对（按 id 不按相邻块）；
  C 登记 / 盘上产物 / 正文锚点 / 排版成品 / 引用编号必须互证。

外加：向后兼容（不给 anchor_id 时产物与旧路径一致）、五连读回每种缺口
单独报红、混合稿不误伤老式题注、承重自检（短路判据当场翻红）。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import caps as C                                 # noqa: E402
from rongke import figure_registry as R                      # noqa: E402
from rongke import typeset as TS                             # noqa: E402
from rongke.tools import ToolRegistry                        # noqa: E402

# ⚠️ 别直接 `from rongke.caps_ext import visual_desk`：那会用包内名字先
#    register 一份，随后 `ensure_external_loaded()` 又用 _rongke_ext_* 名字
#    注册第二份，常驻挂载可能取到旧那份 ⇒ 工具表里没有新工具。
#    一律从**当前注册表**反查模块对象（同 canvas_desk_test 的踩坑）。
C.ensure_external_loaded()
V = sys.modules[C._REGISTRY["@external/visual-desk"].__module__]
P = sys.modules[C._REGISTRY["@external/paper-desk"].__module__]

_fail = 0
PASS: list[str] = []


def ok(m):
    PASS.append(m)
    print(f"[PASS] {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"[FAIL] {m}")


def eq(got, want, label):
    ok(label) if got == want else bad(f"{label}：期望 {want!r}，实际 {got!r}")


def truth(got, label, extra=""):
    ok(label) if got else bad(f"{label} {extra}")


def codes(rep):
    return sorted({g["code"] for g in rep["gaps"]})


def workspace(n=3, with_refs=True):
    """造一份带锚点的稿 + n 个真图文件 + n 条登记。"""
    td = pathlib.Path(tempfile.mkdtemp(prefix="rk_figreg_"))
    reg = {"entries": []}
    chunks = ["# 温度对转化率的影响\n"]
    for i, (ch, cap) in enumerate((
            ("1 引言", "温度梯度设置"),
            ("2 方法", "各批次对照流程"),
            ("3 结果", "转化率随温度变化"))[:n], 1):
        src = f"figure_{i}.svg"
        (td / src).write_text('<svg xmlns="http://www.w3.org/2000/svg"/>',
                              encoding="utf-8")
        eid = f"fig:part-{i}"
        reg["entries"].append({
            "id": eid, "kind": "figure", "caption": cap,
            "chapter": ch, "claim": f"见{eid}所示",
            "source": src, "tier": "preview", "qa": {"ok": True}})
        chunks.append(
            f"\n## {ch}\n"
            + (f"该章结论见图 {i}。\n" if with_refs else "该章有结论。\n")
            + f"\n![{eid}]({src})\n"
            + f"图 {i}　{cap}<!--rk:{eid}-->\n")
    chunks.append("\n## 参考文献\n〔1〕某文献\n")
    md = "".join(chunks)
    (td / "draft.md").write_text(md, encoding="utf-8", newline="\n")
    R.save(td, reg)
    return td, md, R.load(td)[0]


print("── 1. 登记读写：写后读回、幂等、失败不改盘 ──")
td, md, reg = workspace()
eq(len(reg["entries"]), 3, "登记三条")
eq(R.load(td)[1], "ok", "登记读回状态 ok")
one = R.upsert(td, dict(reg["entries"][0], caption="改名后的题注"))
eq(one["readback_equal"], True, "upsert 写后读回一致")
eq(len(R.load(td)[0]["entries"]), 3, "同 id upsert 幂等（不重复追加）")
eq(R.load(td)[0]["entries"][-1]["caption"], "改名后的题注", "覆盖生效")
bad_entry = dict(reg["entries"][0], id="tab:wrong-prefix")
eq(R.upsert(td, bad_entry)["ok"], False, "id 前缀与 kind 不符 ⇒ 拒绝")
eq(len(R.load(td)[0]["entries"]), 3, "失败后盘上登记没变")
eq(R.validate({"entries": [{"id": "fig:a", "kind": "figure"}]})[1] != [],
   True, "缺必填字段被拒")
eq(R.load(pathlib.Path(tempfile.mkdtemp()))[1], "absent",
   "登记缺席如实报 absent，不静默造空表")

print("\n── 2. ★ A：编号按锚点顺序算，重排章节自动跟着变 ──")
eq(R.numbering(md, reg), {"fig:part-1": 1, "fig:part-2": 2, "fig:part-3": 3},
   "原顺序 1/2/3")
parts = re.split(r"(?=^## )", md, flags=re.M)
head = parts[0]
chunks = parts[1:]
body = [c for c in chunks if not c.startswith("## 参考文献")]
refs = [c for c in chunks if c.startswith("## 参考文献")]
reordered = head + body[-1] + "".join(body[:-1]) + "".join(refs)
nums = R.numbering(reordered, reg)
eq(nums, {"fig:part-3": 1, "fig:part-1": 2, "fig:part-2": 3},
   "★ 把「结果」章挪到最前 ⇒ 编号按新顺序重算（旧链会停在 1/2/3）")
new, rep = R.renumber(reordered, reg)
eq(rep["ok"] and rep["changed"], True, "renumber 成功且真的改了文本")
seq = [int(m.group(1)) for m in re.finditer(r"见图 (\d+)", new)]
eq(seq, [1, 2, 3], "★ 全篇交叉引用出现顺序变成 1,2,3（旧链是 [3,1,2]）")

print("\n── 3. ★ A′：删一张图，编号不留洞、引用不悬空 ──")
td2, md2, reg2 = workspace()
# 正确删法：删登记条目 + 从正文拿掉整个第 2 章块
p = re.split(r"(?=^## )", md2, flags=re.M)
kept = "".join(x for x in p if not x.startswith("## 2 方法"))
R.delete(td2, "fig:part-2")
reg3 = R.load(td2)[0]
new2, rn = R.renumber(kept, reg3)
eq(rn["ok"] and not rn["orphan_ids"], True, "删图后重编号无孤儿")
eq(rn["numbering"], {"fig:part-1": 1, "fig:part-3": 2},
   "★ 剩下两张是 1/2，不再留 1/3 的洞")
eq(V.next_number(new2, "图"), 3, "新插入会从 3 开始（不再跳到 4）")
v = R.verify(td2, new2, reg3, rendered=R.render_markdown(new2, reg3)[0])
eq(v["ok"], True, "删图后五连读回全过")
# 错误删法：只删登记，正文没动 ⇒ 必须当场说出口
v_bad = R.verify(td2, md2, reg3)
eq("REGISTRY_ORPHAN_ANCHOR" in codes(v_bad), True,
   "★ 只删登记不删正文 ⇒ 孤儿锚点当场报红")

print("\n── 4. ★ B：图与题注之间夹段落，按 id 配对不受影响 ──")
gap_md = md.replace(
    "![fig:part-1](figure_1.svg)",
    "![fig:part-1](figure_1.svg)\n\n这里插了一段对图的解读，把图和题注隔开了。")
clean, rr = R.render_markdown(gap_md, reg)
sp = TS.parse_markdown(clean)
roles = [b["role"] for b in sp["blocks"]]
truth("figure" in roles and "caption" in roles and "paragraph" in roles,
      "夹段落的稿子仍拆出 figure / caption / paragraph")
vr = R.verify(td, gap_md, reg, rendered=clean)
eq(vr["ok"], True,
   "★ 按 id 配对，图与题注不相邻也不误判（旧链的位置门会假绿）")
spec_rep = TS.render_with_registry(gap_md, reg, td)
eq(spec_rep["ok"], True, "render_with_registry 从登记取图元与编号")
figs = [b for b in spec_rep["spec"]["blocks"] if b["role"] == "figure"]
eq(len(figs), 3, "排版 spec 里有三张图")
truth(all("figure_" in str(b.get("src")) for b in figs),
      "每张图的 src 来自登记，不是另猜一遍")

print("\n── 5. ★ C：五连读回，每一环单独打红 ──")
td3, md3, reg4 = workspace()
clean3, _ = R.render_markdown(md3, reg4)
eq(R.verify(td3, md3, reg4, rendered=clean3)["ok"], True, "完整稿五连读回全过")
(td3 / "figure_1.svg").unlink()
eq("REGISTRY_ASSET_MISSING" in codes(R.verify(td3, md3, reg4, rendered=clean3)),
   True, "① 产物缺失 ⇒ ASSET_MISSING")
(td3 / "figure_1.svg").write_text("<svg/>", encoding="utf-8")
# 2026-11-11 R4 真任务后的**真语义变更**：身份不再依赖模型维护的隐藏注释。
# 判据前提随之改，并补一条能真正打红的正控，不是把断言改软。
no_comment = md3.replace("<!--rk:fig:part-1-->", "")
eq("REGISTRY_ANCHOR_MISSING" in codes(
    R.verify(td3, no_comment, reg4, rendered=clean3)), False,
   "②a ★ 整篇重写删掉隐藏注释 ⇒ 靠图片路径/题注文本仍定位，不误报")
lost_block = "\n".join(
    ln for ln in md3.splitlines()
    if "fig:part-1" not in ln and "figure_1.svg" not in ln
    and "温度梯度设置" not in ln)
eq("REGISTRY_ANCHOR_MISSING" in codes(
    R.verify(td3, lost_block, reg4, rendered=clean3)), True,
   "②b ★ 图块与题注整块都没了 ⇒ ANCHOR_MISSING 当场报红")
eq("REGISTRY_RENDER_MISSING" in codes(
    R.verify(td3, md3, reg4, rendered="一份完全没有图的排版成品")), True,
   "③ 排版成品漏图 ⇒ RENDER_MISSING")
tampered = md3.replace("图 1　温度梯度设置", "图 7　温度梯度设置", 1)
eq("REGISTRY_NUMBER_DRIFT" in codes(
    R.verify(td3, tampered, reg4, rendered=clean3)), True,
   "④ 手改编号 ⇒ NUMBER_DRIFT")
no_ref = md3.replace("该章结论见图 1。", "该章有结论。", 1)
eq("REGISTRY_NO_BODY_REF" in codes(
    R.verify(td3, no_ref, reg4, rendered=clean3)), True,
   "⑤ 正文没有呼应 ⇒ NO_BODY_REF（结构化版，不靠扫已有题注）")

print("\n── 5b. ★ R4 真形态：整篇重写删光隐藏标记、alt 变回「图 N」──")
td_r4, md_r4, reg_r4 = workspace()
num_r4 = R.numbering(md_r4, reg_r4)
rewritten = R.ANCHOR_RE.sub("", md_r4)
for e in reg_r4["entries"]:
    rewritten = rewritten.replace(f"![{e['id']}]",
                                  f"![图 {num_r4[e['id']]}]")
locs, how = R.figure_locs(rewritten, reg_r4)
eq(len(locs), 3, "★ 靠可见内容仍定位到全部 3 张图")
truth(all(v in ("img_src", "caption") for v in how.values()),
      "定位依据是图片路径 / 题注文本，不是隐藏注释", str(how))
clean_r4, rr4 = R.render_markdown(rewritten, reg_r4)
eq(rr4["ok"], True, "普通 alt 的图片也能渲染成编号态")
v_r4 = R.verify(td_r4, rewritten, reg_r4, rendered=clean_r4)
eq(v_r4["ok"], True,
   "★ 覆盖稿五连读回仍全过（R4 的 broken 登记在这里被根治）")
p_r4 = re.split(r"(?=^## )", rewritten, flags=re.M)
c_r4 = p_r4[1:]
b_r4 = [c for c in c_r4 if not c.startswith("## 参考文献")]
f_r4 = [c for c in c_r4 if c.startswith("## 参考文献")]
reordered_r4 = p_r4[0] + b_r4[-1] + "".join(b_r4[:-1]) + "".join(f_r4)
new_r4, rn4 = R.renumber(reordered_r4, reg_r4)
eq(rn4["ok"] and not rn4["mixed_mode"], True,
   "无注释稿不被误判为混合稿（不会保守地跳过重编）")
eq([int(m.group(1)) for m in re.finditer(r"见图 (\d+)", new_r4)],
   [1, 2, 3], "★ 无注释稿重排后引用仍重编成 1,2,3")

print("\n── 6. 混合稿保护：不误伤老式题注，且明说 ──")
mixed = md3 + "\n## 附录\n图 9　老式写法留档\n"
_, rm = R.renumber(mixed, reg4)
eq(rm["mixed_mode"], True, "识别出混合稿")
truth("混合稿" in rm["note"] and "未改" in rm["note"],
      "★ 混合模式明说「老式引用未改」，不冒充全篇重编",
      rm["note"])
newmix = R.renumber(mixed, reg4)[0]
truth("图 9　老式写法留档" in newmix,
      "★ 老式题注没被误改（一次性查表替换只动登记条目）")

print("\n── 7. 向后兼容：不给 anchor_id 完全是旧路径 ──")
td4 = pathlib.Path(tempfile.mkdtemp(prefix="rk_figreg_old_"))
D = json.dumps([{"x": f"B{i}", "y": 20 + i * 7} for i in range(1, 6)],
               ensure_ascii=False)
old_md = ("# 稿\n\n## 1 引言\n温度与转化率。\n\n"
          "## 2 方法\n三批对照。\n\n## 参考文献\n〔1〕某文献\n")
new_old, rep_old = V.place_visual(
    old_md, chapter="1 引言", kind="figure", caption="温度趋势",
    data=D, root=td4)
eq(rep_old["ok"], True, "旧路径仍能插入")
eq("registry" in rep_old, False, "旧路径回包不含 registry 字段")
eq((td4 / R.STATE_NAME).exists(), False,
   "★ 旧路径不产生 图表登记.json（盘上零副作用）")
eq("<!--rk:" not in new_old and "![fig:" not in new_old, True,
   "旧路径正文不含锚点（逐字节等价的形态）")
new_reg, rep_reg = V.place_visual(
    old_md, chapter="1 引言", kind="figure", caption="温度趋势",
    data=D, root=td4, anchor_id="temp-trend")
eq(rep_reg["ok"] and bool(rep_reg.get("registry")), True,
   "给了 anchor_id 才走登记路径")
eq((td4 / R.STATE_NAME).is_file(), True, "登记真落盘")
eq(rep_reg["registry"]["verify_ok"], True, "登记路径五连读回通过")

print("\n── 8. 常驻工具 visual_renumber 真调 ──")
r = ToolRegistry()
C.load_capability_preset("baseline", ctx={"registry": r})
tool = r.get("visual_renumber")
truth(tool is not None and getattr(tool, "fn", None) is not None,
      "visual_renumber 已挂在 baseline 常驻工具表")
td5, md5, _ = workspace()
# 重排稿子但正文编号还是旧的 ⇒ 工具应当重编并落盘
p5 = re.split(r"(?=^## )", md5, flags=re.M)
c5 = p5[1:]
b5 = [c for c in c5 if not c.startswith("## 参考文献")]
r5 = [c for c in c5 if c.startswith("## 参考文献")]
(td5 / "draft.md").write_text(
    p5[0] + b5[-1] + "".join(b5[:-1]) + "".join(r5),
    encoding="utf-8", newline="\n")
cwd = os.getcwd()
try:
    os.chdir(td5)
    out = json.loads(tool.fn(path="draft.md", write=True))
finally:
    os.chdir(cwd)
eq(out.get("ok"), True, "工具真调成功（不是只注册了名字）")
eq(out.get("registry_status"), "ok", "工具读到登记")
eq(out.get("readback_equal"), True, "工具落盘后读回一致")
back = (td5 / "draft.md").read_text(encoding="utf-8")
eq([int(m.group(1)) for m in re.finditer(r"见图 (\d+)", back)],
   [1, 2, 3], "★ 工具真把全篇引用重编成 1,2,3")
# 登记缺席时明说 unknown，不静默造空表
empty = pathlib.Path(tempfile.mkdtemp(prefix="rk_figreg_none_"))
(empty / "draft.md").write_text("# 稿\n", encoding="utf-8")
cwd = os.getcwd()
try:
    os.chdir(empty)
    out2 = json.loads(tool.fn(path="draft.md", write=True))
finally:
    os.chdir(cwd)
eq(out2.get("ok"), False, "登记缺席时工具如实失败")
eq(out2.get("registry_status"), "absent", "★ 明说 absent，不静默降级")

print("\n── 9. 论文台优先读登记，缺席只报 soft ──")
rep_pd = P.scan(td3)
eq(rep_pd["evidence"]["figure_registry"]["status"], "ok",
   "论文台从登记汇总，不再只数 *.svg")
eq(rep_pd["evidence"]["figure_registry"]["n_entries"], 3, "登记条目数读回一致")
rep_abs = P.scan(empty)
eq(rep_abs["evidence"]["figure_registry"]["status"], "absent",
   "缺席时 evidence 明说 absent")
eq(all(g["level"] == "soft" for g in rep_abs["gaps"]
       if g["code"] == "NO_FIGURE_REGISTRY"), True,
   "登记缺席只报 soft，不加交付门（不加严）")

print("\n── 10. 承重自检：短路判据当场翻红 ──")
_o = R._asset_ok
try:
    R._asset_ok = lambda root, rel: True
    (td3 / "figure_2.svg").unlink()
    eq("REGISTRY_ASSET_MISSING" in codes(
        R.verify(td3, md3, reg4, rendered=clean3)), False,
       "★ 短路产物检查 ⇒ 缺件不再被抓（证明这条门原本在承重）")
finally:
    R._asset_ok = _o
    (td3 / "figure_2.svg").write_text("<svg/>", encoding="utf-8")
_o_num = R.numbering
try:
    R.numbering = lambda t, g: {}
    eq(R.verify(td3, md3, reg4, rendered=clean3)["ok"], False,
       "★ 短路编号 ⇒ 五连读回当场翻红（不是恒绿）")
finally:
    R.numbering = _o_num

print()
if _fail:
    print(f"[FAIL] 失败 {_fail} 项")
    raise SystemExit(1)
print(f"[DONE] 图表登记单源：{len(PASS)} PASS / 0 FAIL")
