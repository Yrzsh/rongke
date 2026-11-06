# -*- coding: utf-8 -*-
"""实验设计三件套的回归测试。

★ 为什么必须测数字而不测「文件在不在」：
  一个算样本量的技能如果算错，比没有更糟 —— 用户会拿这个数去写基金、
  报伦理审查。所以判据是「和教科书答案对不对得上」，
  不是「SKILL.md 有没有 20KB」。

★ 为什么从 vendor/ 路径加载而不是 pip：
  这三个技能是**随产品分发**的（stages.SKILL_ROOTS 第一项）。
  如果只测「本机某处能 import」，那证明了不「装进 vendor 之后还能用」。
  所以 sys.path 一律指向 vendor/skills/<name>/scripts。

5 个算例的答案都可以在统计教材或 G*Power 里复现：
  ① 两独立样本 t 检验 d=0.5, α=0.05, power=0.80  → 每组 64
  ② 相关 r=0.3, 同上                              → n≈84
  ③ 全因子 3 因子 2 水平                          → 8 run
  ④ Box-Behnken 3 因子                            → 12 角点 + 中心点
  ⑤ u_c(x·y), x=2.0±0.1, y=3.0±0.2 独立           → √(0.09+0.16)=0.5
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor" / "skills"

_fails: list[str] = []
_total = 0


def check(label: str, good: bool, detail: str = "") -> None:
    global _total
    _total += 1
    print(f"  [{'PASS' if good else 'FAIL'}] {label}"
          + (f" —— {detail}" if detail else ""))
    if not good:
        _fails.append(label)


def near(got, want, tol: float) -> bool:
    try:
        return abs(float(got) - float(want)) <= tol
    except (TypeError, ValueError):
        return False


# ── 0. 三个技能目录齐不齐（分发的前提）─────────────────────────────
print("\n[0] vendor/skills 落地检查")
for name in ("experimental-design", "statistical-power", "uncertainty-and-units"):
    d = VENDOR / name
    check(f"{name}/SKILL.md 存在", (d / "SKILL.md").is_file())
    check(f"{name}/scripts 存在", (d / "scripts").is_dir())

# ── 把三个 scripts 目录挂上 sys.path ────────────────────────────────
for name in ("experimental-design", "statistical-power", "uncertainty-and-units"):
    p = str(VENDOR / name / "scripts")
    if p not in sys.path:
        sys.path.insert(0, p)

# ★ 包名 ≠ 模块名的补丁：脚本里 `from pydoe import ...`，
#   但 PyPI 包名是 pyDOE3、模块名也是 pyDOE3。
#   不做这一步，doe_designs 会直接 ImportError。
try:
    import pyDOE3
    sys.modules.setdefault("pydoe", pyDOE3)
    check("pyDOE3 可用（已别名成 pydoe）", True)
except ImportError as e:
    check("pyDOE3 可用", False, f"缺依赖：{e}")

try:
    import statsmodels  # noqa: F401
    check("statsmodels 可用", True, statsmodels.__version__)
except ImportError as e:
    check("statsmodels 可用", False, f"缺依赖：{e}")

# ── 1. 样本量：两独立样本 t 检验 ────────────────────────────────────
print("\n[1] 两独立样本 t 检验 d=0.5, power=0.80 → 每组 64")
try:
    import power as P
    r = P.sample_size("t_ind", effect_size=0.5, alpha=0.05, power=0.80)
    n = r.get("nobs1") if isinstance(r, dict) else r
    check("每组 n = 64 ± 2", near(n, 64, 2), f"得 {n}")
except Exception as e:
    check("t_ind 样本量", False, f"{type(e).__name__}: {e}")

# ── 2. 样本量：相关 ────────────────────────────────────────────────
print("\n[2] 相关 r=0.3, power=0.80 → n≈84")
try:
    r = P.sample_size("correlation", effect_size=0.3, alpha=0.05, power=0.80)
    n = r.get("nobs") if isinstance(r, dict) else r
    check("n = 84 ± 4", near(n, 84, 4), f"得 {n}")
except Exception as e:
    check("correlation 样本量", False, f"{type(e).__name__}: {e}")

# ── 3. DOE：全因子 ────────────────────────────────────────────────
print("\n[3] 全因子 3 因子 2 水平 → 必然 8 run")
try:
    import doe_designs as D
    df = D.full_factorial({"A": [0, 1], "B": [0, 1], "C": [0, 1]})
    check("run 数 = 8", len(df) == 8, f"得 {len(df)}")
    check("列名含 3 个因子",
          all(c in list(df.columns) for c in "ABC"), f"列 {list(df.columns)}")
    # 负控：不能只画 8 行却全是同一个点
    uniq = df[["A", "B", "C"]].drop_duplicates()
    check("8 run 互不相同（负控）", len(uniq) == 8, f"唯一组合 {len(uniq)}")
except Exception as e:
    check("full_factorial", False, f"{type(e).__name__}: {e}")

# ── 4. DOE：Box-Behnken ──────────────────────────────────────────
print("\n[4] Box-Behnken 3 因子 → 12 角点 + 中心点（12~21 合理）")
try:
    df2 = D.box_behnken({"A": [-1, 1], "B": [-1, 1], "C": [-1, 1]})
    check("run 数在 12~21", 12 <= len(df2) <= 21, f"得 {len(df2)}")
except Exception as e:
    check("box_behnken", False, f"{type(e).__name__}: {e}")

# ── 5. 不确定度传播 ───────────────────────────────────────────────
print("\n[5] u_c(x·y), x=2.0±0.1, y=3.0±0.2 独立 → 0.5")
try:
    import propagate_uncertainty as U
    tree = ast.parse("x*y", mode="eval").body
    vs = [{"name": "x", "value": 2.0, "standard_uncertainty": 0.1,
           "distribution": "normal"},
          {"name": "y", "value": 3.0, "standard_uncertainty": 0.2,
           "distribution": "normal"}]
    r = U.monte_carlo(tree, vs, {}, 0.95, 200000, 7)
    sd = r.get("standard_uncertainty")
    check("u_c = 0.5 ± 0.02", near(sd, 0.5, 0.02), f"得 {sd}")
    check("mean = 6.0 ± 0.02", near(r.get("mean"), 6.0, 0.02),
          f"得 {r.get('mean')}")
except Exception as e:
    check("monte_carlo", False, f"{type(e).__name__}: {e}")

# ── 汇总 ─────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
if _fails:
    print(f"有失败 ✘：{_total - len(_fails)}/{_total} 通过")
    for f in _fails:
        print(f"  · {f}")
    sys.exit(1)
print(f"全部通过 ✔（{_total} 项）")
sys.exit(0)
