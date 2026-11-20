# -*- coding: utf-8 -*-
"""写作前技能简报判据：装配真的生效，且 baseline 逐字节等价。

两个方向都量（本项目硬纪律）：
  · 正控：挂了 @rongke/stage-brief 的预设 ⇒ 简报真进 system，事实能读回；
  · 负控：不带预设 / baseline / 轻量替身 ⇒ 一个字都不加；
  · 缺件：技能读不到 ⇒ 如实列 missing、不抛错、不判坏件、不假装注入；
  · 预算：超预算截断后总长仍 ≤ max_chars（截断提示自己也占预算）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from rongke import caps
from rongke.contract import TaskContract
from rongke.loop import SYSTEM, initial_messages
from rongke.stage_brief import BRIEF_TITLE, build_brief

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
    try:
        fn()
    except ValueError:
        ok(label)
    except Exception as e:                            # noqa: BLE001
        bad(f"{label}：抛了别的异常 {type(e).__name__}: {e}")
    else:
        bad(f"{label}：居然放行了")


# ── [1] build_brief 正控：随包技能在干净环境也能读到 ──
r = build_brief(["uncertainty-and-units"], 1200)
eq("uncertainty-and-units" in r["injected"], True,
   "随包技能被注入（不依赖本机 ~/.agents/skills）")
eq(bool(r["text"]), True, "生成了简报正文")
eq(r["text"].startswith(BRIEF_TITLE), True, "简报带确定性标题")
eq(r["missing"], [], "正控没有缺件")

# ── [2] 预算是硬的（含截断提示） ──
for budget in (400, 600, 1200, 4000):
    rr = build_brief(["uncertainty-and-units", "nature-writing"], budget)
    if rr["chars"] <= budget:
        ok(f"预算 {budget}：简报 {rr['chars']} 字符未超")
    else:
        bad(f"预算 {budget}：简报 {rr['chars']} 字符超了（截断提示没算进预算）")
    eq(len(rr["text"]), rr["chars"], f"预算 {budget}：chars 与正文长度同源")

# ── [3] 缺件 ≠ 坏件 ──
rm = build_brief(["__no_such_skill_zzz__"], 600)
eq(rm["text"], "", "全缺件 ⇒ 不注入任何文本")
eq(rm["missing"], ["__no_such_skill_zzz__"], "全缺件如实列出")
rmix = build_brief(["uncertainty-and-units", "__no_such_skill_zzz__"], 1200)
eq("uncertainty-and-units" in rmix["injected"], True, "混合场景仍注入可用技能")
eq("__no_such_skill_zzz__" in rmix["missing"], True, "混合场景列出缺件")
eq("本机不可用" in rmix["text"], True, "缺件写进简报（拦沉默）")

# ── [4] 形状与非法输入 ──
eq(build_brief([], 600)["text"], "", "空 skills ⇒ 不注入")
rejects(lambda: build_brief(["x"], 10), "负控：预算过小被拒")
rejects(lambda: build_brief(["x"], "abc"), "负控：预算非整数被拒")

# ── [5] ★ 装配正负控：baseline 逐字节等价，paper-writing 真注入 ──
c = TaskContract(raw_goal="写一篇初稿", needs_writing=False,
                 acceptance=[], deliverables=["x.md"], stages=[])
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    m0 = initial_messages(c, topics_root=root)
    eq(m0[0]["content"], SYSTEM, "不带预设 ⇒ system 就是 SYSTEM 原文本")

    mb = initial_messages(c, topics_root=root,
                          caps=caps.load_capability_preset("baseline"))
    eq(mb[0]["content"], SYSTEM, "baseline ⇒ 逐字节等价（一个字都不加）")

    mb_out: dict = {}
    initial_messages(c, topics_root=root,
                     caps=caps.load_capability_preset("baseline"),
                     assembly_out=mb_out)
    eq("stage_brief" in mb_out, False, "baseline 不写简报事实（不虚报）")

    # 轻量替身：测试夹具常传 SimpleNamespace，读不到 contributions 不许崩。
    ms = initial_messages(c, topics_root=root,
                          caps=SimpleNamespace(flag=lambda *a, **k: []))
    eq(ms[0]["content"], SYSTEM, "轻量替身 caps ⇒ 不加字、不崩")

    mp_out: dict = {}
    mp = initial_messages(c, topics_root=root,
                          caps=caps.load_capability_preset("paper-writing"),
                          assembly_out=mp_out)
    system = mp[0]["content"]
    eq(BRIEF_TITLE in system, True, "paper-writing ⇒ 简报真进了 system")
    eq(system.count(BRIEF_TITLE), 1, "简报只注入一次（开场装配、整场冻结）")
    eq(system.startswith(SYSTEM), True, "注入是追加，不改动原 SYSTEM 前缀")
    eq("stage_brief" in mp_out, True, "装配事实被带出来（可读回）")
    injected = (mp_out.get("stage_brief") or {}).get("injected") or []
    eq(bool(injected), True, "事实里列出了真注入的技能（非空扫）")
    eq(mp_out.get("stage_brief_text_len", 0) > 0, True,
       "事实里的简报长度 > 0")
    eq(len(system), len(SYSTEM) + 2 + mp_out["stage_brief_text_len"],
       "system 增量与事实里的长度同源（两处真相）")
    eq("uncertainty-and-units" in system, True,
       "随包技能正文真的到了 system（不是只在 JSON 里挂名）")

# ── [6] ★ 线上事件接线：装配事实不能只躺在 assembly_out 里 ──
import inspect                                        # noqa: E402
from rongke.loop import Rongke                        # noqa: E402
_loop_src = inspect.getsource(Rongke._loop)
eq("stage_brief" in _loop_src, True,
   "_loop 的 system_assembled 条件认得 stage_brief（不是只在装配函数里写）")
eq('emit("system_assembled"' in _loop_src, True,
   "装配事件仍然存在（没被改丢）")
# 负控：baseline 不写这个键 ⇒ 事件条件仍不触发（旧事件流一字不变）。
eq(bool(mb_out.get("stage_brief")), False,
   "baseline 没有简报事实 ⇒ 不会平白发装配事件")

print()
if _fail:
    print(f"[FAIL] 失败 {_fail} 项")
    raise SystemExit(1)
print("[DONE] 写作前技能简报判据全部通过")
