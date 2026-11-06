# -*- coding: utf-8 -*-
"""溢出判据的**语料体检** —— 常驻守卫（2026-09-21）。

## 它回答什么

判据 `text_overflows_box` 上线时，手里的误报证据是：
**11 份 deck、0 份命中、最高比值 0.973**（门槛 1.30）。
但那份证据有个**会随模板漂移**的前提：

  ★ **这条判据只对「没有自适应」的形状说话。**
    模板若在 master 上声明 `normAutofit`（字多了自己收），占位符**全部豁免** ——
    实测那份成片 26 个形状里只有 **2 个**能说话。

⇒ 换一套模板（去掉 `normAutofit`）或换个上游（不再加文本框），
  **判据能说话的范围会剧烈变化，而没有任何人会察觉。**
  这个脚本就是盯着这件事的那只眼。

## 退出码

    0 = 至少能说一句话，且没有命中（**注意**：这不等于"误报率已确立"，看下面那行）
    1 = **有 deck 越过门槛** —— 要么真有溢出，要么门槛/估算该复核
    2 = **一个字都说不出来**（真实产物可测形状 = 0）⇒ 这是"没查"，不是"合规"

⚠️ 为什么 `MIN_MEASURED` **不当退出码**：真实语料现在只有 **4 个**可测形状，
   永远达不到"足以谈误报率"的量。把它做成退出码 ⇒ 这个脚本**恒返回 2**
   ⇒ 守卫天天喊"狼来了" ⇒ 人学会无视它 ⇒ 比没有守卫更坏（定律 10 的兄弟）。
   ⇒ 于是：`MIN_MEASURED` 只用来决定**要不要打那句"分母不足"的警告**，
     退出码只回答"有没有越线"和"说不说得出一句话"。
     **分母不足这件事每次都打印出来** —— 不能说，但也不许藏。

⚠️ 分母口径：**真实任务目录** `runs/<日期>-<时间>/`（`_` 开头一律是内部目录）。
   上一轮的教训：按 mtime 取"最近 N 份"会采到 `runs/_trash/` 里刚被挪走的测试夹具
   ⇒ **拿回收站当"真实产物"，量出来的是判据对自己夹具的命中率。**
   本脚本把两类**分开统计**，夹具只做参考、不进判据结论。

    $PY overflow_corpus_check.py
"""
from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import rongke.deck as D  # noqa: E402

MIN_MEASURED = 30       # 足以谈"误报率"的量（只影响打印的警告，**不是退出码**）


def _is_real_task_dir(p: Path) -> bool:
    try:
        rel = p.relative_to(ROOT / "runs")
    except ValueError:
        return False
    if len(rel.parts) < 2:
        return False
    top = rel.parts[0]
    if top.startswith("_"):
        return False
    return bool(re.fullmatch(r"\d{8}-\d{6}", top))


def _scan() -> list[tuple[Path, int, float | None, dict]]:
    out = []
    for p in sorted((ROOT / "runs").rglob("*.pptx")):
        if "_trash" in p.parts:
            continue
        try:
            f = D.probe_pptx(p)
        except Exception:
            continue
        out.append((p, f.overflow_measured, f.overflow_worst, f.overflow_skipped))
    return out


def main(argv: list[str]) -> int:
    rows = _scan()
    real = [r for r in rows if _is_real_task_dir(r[0])]
    other = [r for r in rows if not _is_real_task_dir(r[0])]

    agg: dict[str, int] = {}
    for grp in (real, other):
        for r in grp:
            for k, v in (r[3] or {}).items():
                agg[k] = agg.get(k, 0) + v

    print("═" * 66)
    print("溢出判据语料体检（门槛 OVERFLOW_WARN_RATIO = %.2f）" % D.OVERFLOW_WARN_RATIO)
    print("═" * 66)
    for label, grp in (("真实任务目录", real), ("其它（归档/夹具，只作参考）", other)):
        mes = sum((r[1] or 0) for r in grp)
        ratios = sorted(r[2] for r in grp if r[2] is not None)
        print("【%s】%d 份 / 可测形状 %d" % (label, len(grp), mes))
        if ratios:
            print("        比值 n=%d  min=%.3f  中位=%.3f  max=%.3f"
                  % (len(ratios), ratios[0], statistics.median(ratios), ratios[-1]))
        hits = [(r[0], r[2]) for r in grp
                if r[2] is not None and r[2] >= D.OVERFLOW_WARN_RATIO]
        for p, w in hits:
            print("        ★ 越过门槛 %.3f  %s" % (w, p.relative_to(ROOT)))

    mes_real = sum((r[1] or 0) for r in real)
    tot = sum(agg.values()) or 1
    print("-" * 66)
    print("未出数分类（全量）：%s" % dict(sorted(agg.items(), key=lambda x: -x[1])))
    print("  覆盖率 = %d/%d = %.1f%%  ⇒ `autofit` 那一格就是**这条判据够不着的范围**："
          % (agg.get("ok", 0), tot, 100.0 * agg.get("ok", 0) / tot))
    print("  模板在 master 上声明了 normAutofit，占位符全部豁免 —— "
          "**别把它读成「占位符不会溢出」，它只是不说话。**")

    hits_all = [(r[0], r[2]) for r in real
                if r[2] is not None and r[2] >= D.OVERFLOW_WARN_RATIO]
    if hits_all:
        print("★ 真实产物里有 %d 份越线 ⇒ 要么真有溢出，要么门槛/估算该复核"
              % len(hits_all))
        return 1
    if mes_real == 0:
        print("★ 真实产物里**一个形状都量不到** ⇒ 这是「没查」，不是「合规」")
        return 2
    print("✔ 真实产物无越线（可测 %d 个形状）" % mes_real)
    if mes_real < MIN_MEASURED:
        print("⚠️ **但分母不足（%d < %d）⇒「误报率 0%%」这个说法在这里不成立。**"
              % (mes_real, MIN_MEASURED))
        print("   现在能说的是：**没有观测到越线**；判据能不能响，靠 §22 的**自造正控**证明。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
