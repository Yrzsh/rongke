# -*- coding: utf-8 -*-
"""拿荣珂**自己的** `_gate_type` 去体检整份真实语料 —— 量每条判据的「打红面」。

## 为什么要有这个脚本

`measure_page_visuals.py` 刚刚证否了我提的那条新判据（打红面 99.6%）。
它顺带逼出一个更该问的问题：

> 既然"新判据会不会变噪音"这么容易翻车，那**已经在门里的那几条**呢？

`rongke/deck.py` 的 `_gate_type` 在 2026-09-21 一天里被加了 4 条新判据
（`no_visual_elements` / `titles_are_noun_phrases` / `ends_with_thanks` /
`last_slide_blank`）。它们**一条都没有在真实语料上量过打红面** ——
而本项目刚刚在 `layout_top_share > 0.7` 上吃过一模一样的亏：
它会在**任何一份正常的学术汇报**上永久报 WARN，把门永久钉在 REVIEW REQUIRED、
**永远到不了 PASS**（见 `rongke/deck.py` 1712–1720 行的判据修正注释）。

**一条永远亮着的警告等于没有警告。**

## 本脚本怎么量（关键：用真尺子，不重写口径）

不镜像 `_gate_type` 的逻辑，而是**直接 import 它来跑**：

    facts = probe_pptx(p)
    g = DeckAuditor(cfg=None)._gate_type(facts)

⇒ 量的就是内核**真正会用的那把尺子**，不存在"镜像实现静默漂移"（H 型）的风险。
⇒ 绕开 `_gate_xml`（它要上游 `audit_pptx_quality.py`，与本节问题无关）。

## 两个分母，都必须分开报

- **真实产物**（`runs/` 下非 `_trash` 的可能被当产品看的那批）——
  判据该长在它们身上。在这里天天喊 = 噪音项。
- **合成夹具**（`_trash/deck-type.*` 等）—— 它们是**既有断言的分母**。
  判据在它们身上**不触发**，往往意味着"这条判据测不出夹具想测的形状"。

⚠️ 真实产物去重后只有个位数（同内容的不同主题变体）⇒ 结论是**方向性**的，不是统计性的。
   本脚本因此**逐份摊开**，而不是只给一个百分比。
"""
from __future__ import annotations

import hashlib
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根
RUNS = ROOT / "runs"
sys.path.insert(0, str(ROOT))

from rongke.deck import DeckAuditor, probe_pptx  # noqa: E402


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if not RUNS.exists():
        print(f"[FAIL] 找不到语料目录：{RUNS}")
        return 2

    au = DeckAuditor(cfg=None)          # cfg=None ⇒ 走 _th 的内置兜底默认
    files = sorted(RUNS.rglob("*.pptx"))

    seen: dict[str, str] = {}
    real: list[dict] = []
    synth: list[dict] = []
    unreadable = 0
    dup = 0

    for f in files:
        try:
            facts = probe_pptx(f)
        except Exception:
            unreadable += 1
            continue
        if facts is None or getattr(facts, "slide_count", 0) == 0:
            unreadable += 1
            continue

        h = _md5(f)
        if h in seen:
            dup += 1
            continue
        seen[h] = str(f)

        try:
            g = au._gate_type(facts)
        except Exception as e:
            print(f"  [WARN] _gate_type 在 {f} 上抛了 {type(e).__name__}: {e}")
            continue

        rec = {
            "path": str(f.relative_to(ROOT)),
            "size": f.stat().st_size,
            "slides": facts.slide_count,
            "verdict": g.verdict,
            "codes": [(fd.level, fd.check_id) for fd in g.findings],
        }
        (synth if "_trash" in f.parts else real).append(rec)

    print(f"语料：{len(files)} 个 pptx；去重后体检 {len(seen)} 份"
          f"（重复 {dup} / 读不动 {unreadable}，**没查**不计入分母）")
    print(f"    其中 真实产物 {len(real)} 份 · 合成夹具 {len(synth)} 份")
    print()

    # ── ① 判决分布（真实产物 vs 夹具）─────────────────────────────
    print("=" * 78)
    print("① 判决分布 —— 真实产物能不能到 PASS？")
    print("=" * 78)
    for label, group in (("真实产物", real), ("合成夹具", synth)):
        c = Counter(r["verdict"] for r in group)
        tot = max(1, len(group))
        parts = "  ".join(f"{k}={v}({v/tot:.0%})" for k, v in c.most_common())
        print(f"  {label:<10} n={len(group):<5} {parts}")
    print()

    # ── ② 逐判据打红面 ───────────────────────────────────────────
    print("=" * 78)
    print("② 逐判据「打红面」—— 真实产物列在前，这是判据该长的地方")
    print("=" * 78)
    real_hit: Counter = Counter()
    synth_hit: Counter = Counter()
    levels: dict[str, str] = {}
    for r in real:
        for lv, code in set(r["codes"]):
            real_hit[code] += 1
            levels.setdefault(code, lv)
    for r in synth:
        for lv, code in set(r["codes"]):
            synth_hit[code] += 1
            levels.setdefault(code, lv)

    nreal, nsynth = max(1, len(real)), max(1, len(synth))
    print(f"{'判据 code':<34}{'级别':<6}{'真实产物':>12}{'夹具':>14}")
    print("-" * 78)
    allcodes = sorted(set(real_hit) | set(synth_hit),
                      key=lambda c: (-real_hit.get(c, 0), -synth_hit.get(c, 0)))
    for code in allcodes:
        r, s = real_hit.get(code, 0), synth_hit.get(code, 0)
        flag = "  ⚠️ 噪音项" if r / nreal >= 0.8 else ""
        print(f"{code:<34}{levels.get(code,''):<6}"
              f"{r:>5} ({r/nreal:>4.0%}){s:>7} ({s/nsynth:>4.0%}){flag}")
    print()
    print("  级别说明：FAIL/WARN 会进判决；SKIP 只报告事实（「没查」）。")
    print("  ⚠️ 打红面 ≥80% ⇒ 这条判据在真实形态上永远亮着 ⇒ 等于没有判据。")
    print()

    # ── ③ 真实产物逐份摊开（去重后）──────────────────────────────
    print("=" * 78)
    print("③ 真实产物逐份摊开（去重后）—— 判决 + 触发了哪些判据")
    print("=" * 78)
    for r in sorted(real, key=lambda r: -r["size"]):
        codes = " ".join(f"{lv}:{c}" for lv, c in r["codes"]) or "（无 finding）"
        print(f"\n  [{r['verdict']}]  {r['path']}")
        print(f"    {r['slides']} 页 · {r['size']/1024:.0f} KB")
        print(f"    {codes}")
    print()

    # ── ④ 夹具上从不触发的判据（潜在 G 型）───────────────────────
    print("=" * 78)
    print("④ 只在真实产物上触发、在夹具上一次都不触发的判据")
    print("=" * 78)
    only_real = [c for c in allcodes if real_hit.get(c, 0) and not synth_hit.get(c, 0)]
    if only_real:
        for c in only_real:
            print(f"  · {c}  —— 真实 {real_hit[c]} / 夹具 0"
                  f"  ⇒ **既有断言覆盖不到它**（改了它也测不出来）")
    else:
        print("  （无）")
    print()
    never = [c for c in allcodes if not real_hit.get(c, 0)]
    if never:
        print("  反之，在真实产物上一次都不触发的判据（不一定是坏事，但要知道）：")
        for c in never:
            print(f"  · {c}  —— 夹具 {synth_hit.get(c,0)} 次 / 真实 0 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
