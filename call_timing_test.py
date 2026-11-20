# -*- coding: utf-8 -*-
"""「调用时机」与无人值守续步的判据（2026-11-11 真任务暴露的三处）。

三场真 AI 任务把三个「工具在册但没接上」的缺口量死了：
  R1 默认话术 ⇒ anchor_id 0 次；R1/R2/R3 ⇒ 整篇 write_file 冲掉图块；
  R2/R3 ⇒ writing 短文弹 step_limit 白等 300 s 后 success=false。

这里钉住修法确实生效、且**没有改成更严的门**：
  ① 写后简报在正确时刻点名工具（没图不早报、有图必报）；
  ② 锚点被覆盖掉当场说出口；
  ③ 自动续步资格是纯函数且两道闸都在；
  ④ 简报仍是 advisory、不碰 gate.check、SYSTEM 不动。
"""
from __future__ import annotations

import ast
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import figure_registry as FR                    # noqa: E402
from rongke import readiness as RD                           # noqa: E402
from rongke import shape as SH                               # noqa: E402

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


def brief(text: str, root: pathlib.Path, *, step: int = 8,
          max_steps: int = 40) -> dict:
    return RD.build_readiness(
        trace=[], contract=SimpleNamespace(
            needs_figure=False, acceptance=[], _is_long_review=False),
        step=step, max_steps=max_steps, text=text, root=root)


def codes(rep):
    return [g["code"] for g in rep["gaps"]]


def ws() -> pathlib.Path:
    import tempfile
    return pathlib.Path(tempfile.mkdtemp(prefix="rk_timing_"))


print("── 1. 没图不早报（成熟度分诊：早报会训练模型无视反馈）──")
d1 = ws()
r = brief("# 稿\n\n## 1 引言\n正文还没开始配图。\n", d1)
eq("FIGURE_REGISTRY_MISSING" in codes(r), False,
   "纯文本半截稿不报登记缺口")
eq(r["advisory"], True, "简报恒为 advisory（不加门）")

print("\n── 2. ★ 有图无登记 ⇒ 在写后简报里点名工具 ──")
d2 = ws()
(d2 / "figure_1.svg").write_text("<svg/>", encoding="utf-8")
md_fig = ("# 稿\n\n## 1 引言\n温度趋势见图 1。\n\n"
          "![图 1](figure_1.svg)\n图 1　温度趋势\n")
r = brief(md_fig, d2)
eq("FIGURE_REGISTRY_MISSING" in codes(r), True,
   "★ 有图无登记 ⇒ 报 FIGURE_REGISTRY_MISSING")
truth("anchor_id" in r["text"] and "visual_renumber" in r["text"],
      "★ 简报正文里点名 anchor_id 与 visual_renumber（模型真看得见）",
      r["text"][:200])
eq(r["metrics"].get("figure_registry"), "absent", "状态读回 absent")

print("\n── 2b. 整篇覆盖把图块冲掉，文本里没图也照样报 ──")
d2b = ws()
(d2b / "figure_1.svg").write_text("<svg/>", encoding="utf-8")
r = brief("# 稿\n\n## 1 引言\n刚被整篇覆盖，图块没了。\n", d2b)
eq("FIGURE_REGISTRY_MISSING" in codes(r), True,
   "★ 靠工作区图产物兜住：覆盖那一刻不漏报（R1 的盲点）")

print("\n── 3. ★ 登记在场但锚点被覆盖 ⇒ FIGURE_ANCHORS_LOST ──")
d3 = ws()
(d3 / "f_a.svg").write_text("<svg/>", encoding="utf-8")
FR.upsert(d3, {"id": "fig:a", "kind": "figure", "caption": "温度趋势",
               "chapter": "1 引言", "source": "f_a.svg", "tier": "preview"})
md_ok = ("# 稿\n\n## 1 引言\n温度趋势见图 1。\n\n"
         "![fig:a](f_a.svg)\n图 1　温度趋势<!--rk:fig:a-->\n")
r = brief(md_ok, d3)
eq("FIGURE_ANCHORS_LOST" in codes(r), False,
   "锚点齐、登记齐 ⇒ 不制造噪音")
eq("FIGURE_REGISTRY_GAPS" in codes(r), False, "五连读回干净 ⇒ 不报")
r_lost = brief("# 稿\n\n## 1 引言\n整篇覆盖把锚点冲掉了。\n", d3)
eq("FIGURE_ANCHORS_LOST" in codes(r_lost), True,
   "★ 登记 1 条、正文 0 锚点 ⇒ 当场报 FIGURE_ANCHORS_LOST")
truth("visual_place(anchor_id=" in r_lost["text"],
      "缺口里给出可自愈动作（重新插图），不是只报警")

print("\n── 3b. 产物缺失 ⇒ 五连读回缺口进简报 ──")
(d3 / "f_a.svg").unlink()
r_gap = brief(md_ok, d3)
eq("FIGURE_REGISTRY_GAPS" in codes(r_gap), True,
   "图文件被删 ⇒ FIGURE_REGISTRY_GAPS")

print("\n── 4. ★ 自动续步资格：纯函数、两道闸、不放水 ──")
eq(SH.should_auto_extend("long_review", [], 0), True,
   "长综述无人值守仍自动续（原行为保留）")
eq(SH.should_auto_extend("writing", ["write_file", "read_file"], 0), True,
   "★ writing 且写过正文、未交付 ⇒ 自动续（R2/R3 的白等被修掉）")
eq(SH.should_auto_extend("writing", ["write_file", "deliver_task"], 0), False,
   "已经试过交付 ⇒ 不再自动加步数（不把预算放水）")
eq(SH.should_auto_extend("writing", ["read_file"], 0), False,
   "只检索没写正文 ⇒ 不续")
eq(SH.should_auto_extend("other", ["write_file"], 0), False,
   "问答 / 短任务一律不续（守住原边界）")
eq(SH.should_auto_extend("writing", ["write_file"], SH.MAX_AUTO_EXTENDS),
   False, "硬上限生效，绝不无限续")
eq(SH.should_auto_extend("writing", ["write_file"], 0) and SH.AUTO_EXTEND_STEPS > 0,
   True, "续步步长是正数")

print("\n── 5. ★ 承重与例外纪律 ──")
_o = FR.load
try:
    FR.load = lambda root: (_ for _ in ()).throw(RuntimeError("短路"))
    r = brief(md_fig, d2)
    truth(any("unknown" in n and "图表登记层" in n for n in r["notes"]),
          "★ 登记层短路 ⇒ 明说 unknown（不静默降级成通过）",
          str(r["notes"])[:160])
finally:
    FR.load = _o

src = (ROOT / "rongke" / "readiness.py").read_text(encoding="utf-8")
tree = ast.parse(src)
calls = [n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute)
         and n.func.attr == "check"
         and isinstance(n.func.value, ast.Name)
         and n.func.value.id == "gate"]
eq(len(calls), 0, "readiness 里没有 gate.check()（不烧打回额度）")

print("\n── 6. write_file 真调：简报真的挂在回包路径上 ──")
d6 = ws()
(d6 / "figure_1.svg").write_text("<svg/>", encoding="utf-8")
(d6 / "draft.md").write_text(md_fig, encoding="utf-8", newline="\n")
r6 = RD.readiness_after_write(
    path="draft.md", trace=[],
    contract=SimpleNamespace(needs_figure=False, acceptance=[],
                             _is_long_review=False),
    step=8, max_steps=40, root=d6)
truth(r6 is not None, "readiness_after_write 对正文类文件返回简报")
truth(r6 is not None and "FIGURE_REGISTRY_MISSING" in codes(r6),
      "★ 真调路径上也能读到登记缺口（不是只有直接调 build_readiness 才有）")

print()
if _fail:
    print(f"[FAIL] 失败 {_fail} 项")
    raise SystemExit(1)
print(f"[DONE] 调用时机与无人值守修复：{len(PASS)} PASS / 0 FAIL")
