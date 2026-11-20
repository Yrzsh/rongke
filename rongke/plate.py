# -*- coding: utf-8 -*-
"""板内质控：把一块读板机的读数变成「这张板的数据能不能用」。

## 这份实现从哪来

源自 GitHub 开源项目 `sriranga13/plateqc`（MIT，Zero dependencies，stdlib only）。
**照抄的是算法与口径，不是文件**——它那套 Z' / SSMD / 信号窗 / 边沿与漂移诊断
是Zhang et al. (J Biomol Screen 1999) 的标准算法，我们的 MIVA 方案正缺这一层：
方案里写了「复孔 CV ≤ 15%」「ΔA 落 20%–80% 线性段」，
却没有任何东西**在数据进来时真的去算**。判据写在文档里，没人执行。

## 为什么没有直接 `pip install plateqc`

① 本仓是零依赖的，多一个依赖就多一条供应链；
② 它的 `_parse_number` 有个静默污染缺陷（见下）。照抄会一起抄进来。

## 我们改了什么（两处，都是实测发现的）

### ① `nan` / `inf` 会被当成正常数值

它的 `_parse_number`：`_MISSING_TOKENS = {"", "na", "n/a", "null", "none", "-", "--"}`
之后 `float(text)`。问题在后半句：`float("nan")` 与 `float("inf")` **不抛异常**，
于是这两个值一路进入 `mean/stdev`，把 Z' 变成 `nan`，而 `warnings` 是**空的**。

实测（本机，2026-11-16，导入其源码 `src/plateqc`）：

    CLEAN  zprime=1.0    meanHigh=1.0    warnings=[]
    NAN    zprime=nan    meanHigh=nan    warnings=[]
    INF    zprime=nan    meanHigh=inf    warnings=[]

`nan` 的 Z' 在任何比较里都是 False，于是「Z' > 0.5 判合格」会**静默判不合格**，
而「Z' < 0.5 告警」也**永远不会触发**。一块被 nan 污染的板会安静地过审。
这里把 `nan`/`inf`/`-inf` 归入缺失，并**计入 warnings**（不静默）。

### ② README 声称支持 `ND`/`BQL`/`TNTC` 等删失标记，代码里没有

原文 README 写 "censored tokens (`ND`, `BQL`, `<`, `>`, `TNTC`) parse as missing"，
但 `qc.py` 里搜不到这些串（`ND in qc.py → False`，`BQL → False`，`TNTC → False`）。
它们只是恰好被 `float()` 抛 ValueError 兜住，`<0.05` / `>4.0` 这种**带比较符的**
同样靠异常兜底。语义上「小于检出限」与「缺失」**不是一回事**，
用同一个桶装会让「<0.05 的样」和「没测的样」无法区分。

这里把删失值**单独记一个 `censored` 桶**并写进回包，
因为做定量筛选时「这个样低于检出限」和「这个孔漏加了」是两种完全不同的结论。

## 纪律

★ 本模块**只做判定，不改数据**。任何「把不合格的孔剔掉再算一遍」的行为都会
让 Z' 变好看但失真。不合格就报不合格。
"""
from __future__ import annotations

import math
import re

#: 读板机导出里表示「没有这个值」的写法
MISSING_TOKENS = {"", "na", "n/a", "null", "none", "nan", "-", "--"}

#: 表示「测了，但落在检出/定量限之外」的写法 —— **不与缺失共用桶**（见模块头 ②）
CENSORED_TOKENS = {"nd", "bql", "tnc", "tntc", "n.d.", "n/a*"}

#: 带比较符的删失写法：`<0.05`、`>4.0`、`<=1e-3`
_CENSORED_RE = re.compile(r"^(<=|>=|<|>)\s*-?\d+(\.\d+)?([eE][-+]?\d+)?$")

_WELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


# ── 数值解析（本模块最要命的一段，见模块头 ①）────────────────
def parse_value(text):
    """把一个格子里的文字解析成 `(值, 状态)`。

    状态只有三种：`"ok"` / `"missing"` / `"censored"`。
    **`nan`/`inf` 归 missing，不归 ok** —— 这是与原项目唯一的实质分歧。
    """
    s = str(text if text is not None else "").strip()
    low = s.lower()
    if low in MISSING_TOKENS:
        return None, "missing"
    if low in CENSORED_TOKENS or _CENSORED_RE.match(s):
        return None, "censored"
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return None, "missing"
    # ★ 关键三行：float("nan") 不抛异常，原项目就是在这里漏掉的
    if math.isnan(v) or math.isinf(v):
        return None, "missing"
    return v, "ok"


# ── 统计量（与 Zhang et al. 1999 同口径，样本标准差 n-1）──────
def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def stdev(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def cv(xs):
    m, s = mean(xs), stdev(xs)
    if m is None or s is None or m == 0:
        return None
    return s / abs(m)


def zprime(high, low):
    """Z'-factor。1 理想，>0.5 可用。任一输入不足以计算 ⇒ None（不是 0）。"""
    mh, ml = mean(high), mean(low)
    sh, sl = stdev(high), stdev(low)
    if None in (mh, ml, sh, sl):
        return None
    denom = abs(mh - ml)
    return None if denom == 0 else 1 - (3 * (sh + sl)) / denom


def ssmd(high, low):
    """严格标准化均差。>3 通常认为分离良好。"""
    mh, ml = mean(high), mean(low)
    sh, sl = stdev(high), stdev(low)
    if None in (mh, ml, sh, sl):
        return None
    pooled = math.sqrt(sh ** 2 + sl ** 2)
    return None if pooled == 0 else abs(mh - ml) / pooled


def signal_window(high, low):
    mh, ml = mean(high), mean(low)
    if None in (mh, ml):
        return None
    return mh - ml


def signal_to_background(high, low):
    mh, ml = mean(high), mean(low)
    if None in (mh, ml) or ml == 0:
        return None
    return mh / ml


# ── 空间诊断 ────────────────────────────────────────────────
def parse_well(well):
    m = _WELL_RE.match(str(well).strip())
    if not m:
        raise ValueError(f"不是孔号: {well!r}")
    row = 0
    for ch in m.group(1).upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"不是孔号: {well!r}")
        row = row * 26 + (ord(ch) - ord("A") + 1)
    col = int(m.group(2))
    if row < 1 or col < 1:
        raise ValueError(f"不是孔号: {well!r}")
    return row, col


def edge_ratio(edge_vals, inner_vals):
    """边沿孔均值 / 内部孔均值 − 1。>0.05 提示蒸发/温度边沿效应。"""
    me, mi = mean(edge_vals), mean(inner_vals)
    if me is None or mi is None or mi == 0:
        return None
    return me / mi - 1


def _linregress(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pts)
    if n < 2 or len({p[0] for p in pts}) < 2:
        return None, None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    denom = sum((p[0] - mx) ** 2 for p in pts)
    if denom == 0:
        return None, None
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / denom
    return slope, my - slope * mx


def drift(index, values):
    """跨列的相对漂移（斜率 × 跨度 / 均值）。0.10 = 全程漂 10%。"""
    m = mean(values)
    slope, _ = _linregress(index, values)
    if m is None or slope is None or m == 0:
        return None
    xs = [x for x in index if x is not None]
    if len(set(xs)) < 2:
        return None
    return slope * (max(xs) - min(xs)) / abs(m)


# ── 一次完整的板检 ──────────────────────────────────────────
def run_wells(wells, layout, min_controls=4):
    """`wells` 是 `{孔号: 原始值或文字}`（**未解析**，本函数负责解析）。

    返回 `groups / assay / spatial / warnings / missing / censored`。
    `warnings` 里每一条都是**人话**且带孔号 —— 不说孔号的告警等于没说。
    """
    warnings = []
    vals, missing, censored = {}, [], []
    for w, raw in wells.items():
        v, st = parse_value(raw)
        if st == "ok":
            vals[w] = v
        else:
            (missing if st == "missing" else censored).append(w)

    if missing:
        warnings.append(
            f"{len(missing)} 个孔没有可用数值（{', '.join(sorted(missing)[:6])}"
            f"{'…' if len(missing) > 6 else ''}）—— 已排除，**没有当作 0**")
    if censored:
        warnings.append(
            f"{len(censored)} 个孔是删失值（{', '.join(sorted(censored)[:6])}"
            f"{'…' if len(censored) > 6 else ''}）—— 落在检出限外，"
            "不能当数值参与均值，但**也不能当成没测**")

    def group(name):
        spec = layout.get(name) or []
        got = [vals[w] for w in spec if w in vals]
        if len(got) < min_controls:
            warnings.append(
                f"对照组 {name!r} 只有 {len(got)} 个有效孔"
                f"（要求 ≥{min_controls}）—— 这块板的 Z' 不可信")
        return got

    groups = {}
    for name in ("high", "low"):
        if name not in layout:
            continue
        g = group(name)
        groups[name] = {"n": len(g), "mean": mean(g), "stdev": stdev(g),
                        "cv": cv(g),
                        "min": min(g) if g else None,
                        "max": max(g) if g else None}

    hi = [vals[w] for w in (layout.get("high") or []) if w in vals]
    lo = [vals[w] for w in (layout.get("low") or []) if w in vals]
    assay = {
        "zprime": zprime(hi, lo),
        "ssmd": ssmd(hi, lo),
        "signal_window": signal_window(hi, lo),
        "signal_to_background": signal_to_background(hi, lo),
    }
    z = assay["zprime"]
    if z is None:
        assay["zprime_grade"] = "无法判定（对照组不足或全部相同）"
    elif z >= 0.5:
        assay["zprime_grade"] = "可用"
    else:
        assay["zprime_grade"] = f"**不可用**（Z'={z:.2f} < 0.5，这块板的数据不能用）"

    # 空间诊断：需要一个有行有列的板
    rc = [(w, parse_well(w)) for w in vals]
    edge_v, inner_v = [], []
    if rc:
        maxr = max(r for _, (r, _) in rc)
        maxc = max(c for _, (_, c) in rc)
        for w, (r, c) in rc:
            if r == 1 or r == maxr or c == 1 or c == maxc:
                edge_v.append(vals[w])
            else:
                inner_v.append(vals[w])
    col_vals, col_idx = [], []
    for w, (r, c) in rc:
        col_vals.append(vals[w])
        col_idx.append(c)
    spatial = {"edge_ratio": edge_ratio(edge_v, inner_v),
               "column_drift": drift(col_idx, col_vals)}

    return {"groups": groups, "assay": assay, "spatial": spatial,
            "warnings": warnings,
            "n_ok": len(vals), "missing": sorted(missing),
            "censored": sorted(censored)}


def summary(slug, report):
    """给人看的一段话。**先说不合格，再说细节** —— 不合格的不该埋在末尾。"""
    a = report["assay"]
    lines = []
    z = a["zprime"]
    # ★ 2026-11-16 真实使用补：原来这里只看 Z'，于是出现过
    #   「板质控通过：Z'=0.93」紧跟「对照组 high 只有 3 个有效孔 —— Z' 不可信」
    #   的自相矛盾（同一段回包，前后两句话打架）。根因是把「算得出数」当成「数可信」。
    #   Z' 在 n=2 的对照上照样算得出来，但它没有置信意义。所以**先过样本量这一关**。
    underpowered = [w for w in report["warnings"] if "有效孔" in w]
    if underpowered:
        lines.append("⚠ 这张板的质控**没过**：对照孔不足，Z' 算得出来但不可信")
    elif z is None or z < 0.5:
        lines.append(f"⚠ 这张板的质控**没过**：{a['zprime_grade']}")
    else:
        lines.append(f"板质控通过：Z'={z:.2f}，SSMD={a['ssmd']:.1f}"
                     if a["ssmd"] is not None else f"板质控通过：Z'={z:.2f}")
    sw = a["signal_window"]
    if sw is not None:
        lines.append(f"信号窗 {sw:.3g}，信噪比 "
                     f"{a['signal_to_background']:.2f}" if a["signal_to_background"]
                     else f"信号窗 {sw:.3g}")
    for name, g in report["groups"].items():
        if g["cv"] is not None:
            tag = " ✅" if g["cv"] <= 0.15 else " ⚠ 复孔 CV 超 15%"
            lines.append(f"  {name}: n={g['n']} 均值 {g['mean']:.4g} "
                         f"CV={g['cv'] * 100:.1f}%{tag}")
    if report["warnings"]:
        lines.append("提示：")
        lines += [f"  · {w}" for w in report["warnings"]]
    return "\n".join(lines)
