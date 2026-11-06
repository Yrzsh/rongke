# -*- coding: utf-8 -*-
"""plot_conversion.py —— 出版级配图（按 nature-figure v2.8.0 的契约执行）

**本脚本是纯渲染器**：只读源数据 CSV → 出图。不造数据（造数据在
`make_demo_data.py`，为什么分开见那个文件的头注释）。

════════════════════════ FIGURE CONTRACT ════════════════════════
（nature-figure 硬要求：**先写契约，后写代码**）

1. 核心结论（这张图要捍卫的那一句话）
   "Increasing residence time raises conversion monotonically, the gain per
    hour itself grows with time, and this pattern reproduces across three
    independent batches rather than being carried by a single batch."

2. 证据链（每个面板承担**不同的推断角色** —— 不是为了好看多画两个）
   (a) 趋势 + 不确定性   → 主效应存在（一阶）
   (b) 每批次的配对增量  → 排除"某一个批次把结论带出来的"
   (c) 增量的分布对比     → "增量本身在变大"这个**二阶**说法站得住，不是噪声
   三个角色互补，缺一个都有漏洞。**没有任何两个面板在画同一件事。**

3. 原型：quantitative grid（1 行 3 个等宽面板）

4. 后端：python
   （`scripts/nature_figure_backend.py get` → `python`，是**已保存的偏好**）
   ⚠️ 后端**独占**：本图全部由 matplotlib 出图 / 预览 / 导出 / 目视 QA。
   不许拿另一个后端（或别的绘图软件）出预览或替代导出。

5. 期刊/导出契约
   · 尺寸 **精确 183 mm（双栏）× 52 mm** —— 不用 `bbox_inches="tight"`，
     因为它会按内容裁掉空白、把物理宽度变成不确定值；期刊要的是精确栏宽。
   · 正文字号 7 pt；PDF 里**每个字形**下限 5 pt（含上下标）
   · 字体 Arial（sans-serif 回退链）
   · 文字可编辑：pdf.fonttype=42 / svg.fonttype=none
   · 导出 SVG + PDF + TIFF(600 dpi) + PNG(300 dpi 预览)
   · 多面板对齐门 tolerance 1.5 pt（strict，不过就拦交付）
   · 颜色：不用彩虹色图；红绿不是唯一编码；灰度打印仍可读

数据：`conversion_source_data.csv`（**合成演示数据**，见 `make_demo_data.py`）。
真做论文时替换那个 CSV 即可，本脚本一个字都不用改。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

# ── 技能自带的强制对齐门 ──────────────────────────────────────
# 单面板记 NOT APPLICABLE；多面板几何不过就 raise（strict=True）。
_SKILL_SCRIPTS = Path.home() / ".agents" / "skills" / "nature-figure" / "scripts"
sys.path.insert(0, str(_SKILL_SCRIPTS))
from audit_panel_alignment import (  # noqa: E402
    matplotlib_layout_manifest,
    require_matplotlib_panel_alignment,
)

# ── 荣珂内核的字形守卫 ────────────────────────────────────────
# 复用内核实现而不是各写一份：这套判据（"missing from font" 是唯一的锚点）
# 一旦分叉，两边迟早不一致。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rongke.figure import glyphs_missing  # noqa: E402

OUT = Path(__file__).resolve().parent
FIG_STEM = OUT / "fig_conversion"
DATA_CSV = OUT / "conversion_source_data.csv"

MM = 1.0 / 25.4
# 这两个名字是**约定的名字**：nature-figure 的源码预检靠
# `(?:fig_)?width_mm\s*[:=]` 正则来认最终宽度，叫 `W_MM` 它认不出来，
# 于是会给一条 "No static final width detected" 的警告。
FIG_WIDTH_MM = 183.0
FIG_HEIGHT_MM = 52.0
# 预期页宽（mm），保存后用 PyMuPDF 实测核对 —— 把"我声明了 183"
# 升级成"文件确实是 183"。差超过 0.5 mm 就报错。
EXPECT_PDF_WIDTH_MM = 183.0
EXPECT_PDF_HEIGHT_MM = 52.0

# ── 调色板：取自技能的 PALETTE（不要自己调色，色盲友好与灰度可读是设计过的）──
PALETTE = {
    "blue_main": "#0F4D92", "blue_secondary": "#3775BA",
    "green_3": "#8BCF8B", "red_strong": "#B64342",
    "neutral_light": "#CFCECE", "neutral_mid": "#767676",
    "neutral_dark": "#4D4D4D", "teal": "#42949E", "violet": "#9A4D8E",
}

# ── rcParams：技能 static/fragments/backend/python.md 的 quick-start ──
mpl.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans", "sans-serif"],
    "svg.fonttype": "none",          # SVG 里文字保持可编辑
    "pdf.fonttype": 42,              # PDF 里 TrueType 文字可编辑（不是轮廓）
    "font.size": 7,                  # 期刊密集图的正文档位
    "axes.spines.right": False,      # Nature 惯例：去掉上/右框
    "axes.spines.top": False,
    "axes.linewidth": 0.8,
    "legend.frameon": False,         # 图例不套框
    "xtick.direction": "out",
    "ytick.direction": "out",
    "xtick.major.size": 2.5,
    "ytick.major.size": 2.5,
    "xtick.major.width": 0.8,
    "ytick.major.width": 0.8,
    "lines.linewidth": 1.1,
    "lines.markersize": 3.4,
    "figure.dpi": 200,
    # 刻意**不设** savefig.bbox = "tight"：见契约第 5 点。
})


# ══════════════════ 数据（只读，不造）══════════════════
def load_data(path: Path = DATA_CSV) -> dict:
    """从源数据 CSV 读入。宽表：一列 time_h + 每个批次一列。"""
    if not path.exists():
        raise SystemExit(
            f"⛔ 找不到源数据 {path.name}。\n"
            f"   本脚本是纯渲染器，不自己造数据。先跑：\n"
            f"     python {OUT / 'make_demo_data.py'}\n"
            f"   或者把你的实测数据按同样的列结构写进 {path.name}。")
    times: list[float] = []
    cols: list[list[float]] = []
    names: list[str] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        header = next(rd)
        names = [h.strip() for h in header[1:]]
        cols = [[] for _ in names]
        for row in rd:
            if not row or not row[0].strip():
                continue
            times.append(float(row[0]))
            for j in range(len(names)):
                cols[j].append(float(row[j + 1]))
    conv = np.array(cols, dtype=float)          # shape = (批次, 时间点)
    return {"time_h": np.array(times, dtype=float), "conv_pct": conv,
            "batch_names": names, "source": str(path)}


def _det_jitter(n: int, width: float) -> np.ndarray:
    """**确定性**抖动（黄金比低差异序列）。

    为什么不用 `np.random`：图上的点在物理上落在哪儿，是**图的一部分**。
    用随机数意味着同一份数据在不同机器/不同 numpy 版本上会画出不同的图 ——
    审稿人复现不出来。低差异序列既均匀分散又完全可复现。
    （顺带也让绘图脚本彻底不含随机数发生器。）
    """
    i = np.arange(n, dtype=float)
    return ((i * 0.6180339887498949) % 1.0 - 0.5) * width


# ══════════════════ 绘图 ══════════════════
def draw(data: dict):
    t = data["time_h"]
    conv = data["conv_pct"]
    mean = conv.mean(axis=0)
    sd = conv.std(axis=0, ddof=1)                 # 样本标准差（n-1）
    inc = np.diff(conv, axis=1)                   # 每小时增量，逐批次
    n_batch, n_tp = conv.shape

    # 3 个等宽面板：用 subplots 保证宽度/高度/间距天然相等（对齐门要求）
    fig, axes = plt.subplots(1, 3, figsize=(FIG_WIDTH_MM * MM, FIG_HEIGHT_MM * MM))
    fig.subplots_adjust(left=0.075, right=0.985, bottom=0.235, top=0.855,
                        wspace=0.42)

    # ── (a) 趋势 + 不确定性：主效应（一阶）─────────────────
    ax = axes[0]
    for i in range(n_batch):
        ax.plot(t, conv[i], color=PALETTE["neutral_light"], linewidth=0.7,
                marker="o", markersize=2.1, markerfacecolor="white",
                markeredgewidth=0.6, zorder=1)          # 个体批次（浅色垫底）
    ax.errorbar(t, mean, yerr=sd, color=PALETTE["blue_main"], linewidth=1.3,
                marker="o", markersize=3.6, markerfacecolor=PALETTE["blue_main"],
                markeredgecolor="white", markeredgewidth=0.5,
                capsize=2.0, elinewidth=0.8, capthick=0.8, zorder=3)
    ax.set_xlabel("Residence time (h)")
    ax.set_ylabel("Conversion (%)")
    ax.set_xlim(-0.25, 5.25)
    ax.set_ylim(-1.5, 17.5)
    ax.text(0.5, 0.94, "mean ± s.d., n = 3 batches",
            transform=ax.transAxes, ha="center", va="top", fontsize=5.6,
            color=PALETTE["neutral_dark"])

    # ── (b) 每批次的配对增量：排除单批次假象 ─────────────────
    ax = axes[1]
    mid = (t[:-1] + t[1:]) / 2                    # 增量落在区间中点
    batch_styles = [
        (PALETTE["blue_main"], "-", "o"),
        (PALETTE["green_3"], "--", "s"),
        (PALETTE["red_strong"], ":", "^"),
    ]
    for i in range(n_batch):
        c, ls, mk = batch_styles[i]
        ax.plot(mid, inc[i], color=c, linestyle=ls, marker=mk,
                markersize=3.0, markerfacecolor="white", markeredgewidth=0.8,
                markeredgecolor=c, label=data["batch_names"][i])
    ax.axhline(0, color=PALETTE["neutral_mid"], linewidth=0.6, zorder=0)
    ax.set_xlabel("Time interval midpoint (h)")
    # 不用 "h⁻¹"：Arial 缺 U+207B（上标减号）→ 渲染成豆腐块且**不报错**。
    # "per hour" 已经写在标签里了，单位就是 %-points。
    ax.set_ylabel("Increment per hour (%-points)")
    ax.set_xlim(0.4, 4.6)
    ax.set_ylim(0, 6.8)
    ax.legend(loc="upper left", fontsize=5.6, handlelength=2.1,
              borderaxespad=0.15, labelspacing=0.28)

    # ── (c) 增量的分布对比：加速这个"二阶"说法站不站得住 ──────
    ax = axes[2]
    early = inc[:, :2].ravel()                    # 0–2 h 的区间
    late = inc[:, 2:].ravel()                     # 3–5 h 的区间
    groups = [("0–2 h", early, PALETTE["blue_secondary"], "o"),
              ("3–5 h", late, PALETTE["red_strong"], "D")]
    for k, (lbl, vals, c, mk) in enumerate(groups):
        x = np.full(vals.size, k) + _det_jitter(vals.size, 0.24)
        ax.scatter(x, vals, s=9, facecolor="white", edgecolor=c,
                   linewidth=0.7, marker=mk, zorder=3)
        m, s = vals.mean(), vals.std(ddof=1)
        ax.errorbar(k, m, yerr=s, color=PALETTE["neutral_dark"],
                    linewidth=0, elinewidth=0.9, capsize=2.2, capthick=0.9,
                    marker="_", markersize=7.5, zorder=4)
    ax.set_xticks([0, 1], [g[0] for g in groups])
    ax.set_xlim(-0.55, 1.55)
    ax.set_ylim(0, 6.8)
    ax.set_ylabel("Increment per hour (%-points)")

    # 统计：Welch 双样本 t（两组增量来自不同时间段，不构成配对）
    tv, pv = stats.ttest_ind(late, early, equal_var=False)
    ax.text(0.5, 0.95,
            f"Welch t = {tv:.2f}, P = {pv:.3f}\nn = {early.size} / {late.size} intervals",
            transform=ax.transAxes, ha="center", va="top", fontsize=5.6,
            color=PALETTE["neutral_dark"])

    # ── 面板标签：小写粗体、左上、8 pt（三处位置**完全一致**，对齐门要看）──
    for ax_, lbl in zip(axes, "abc"):
        ax_.text(-0.235, 1.045, lbl, transform=ax_.transAxes, fontsize=8,
                 fontweight="bold", ha="left", va="top")

    return fig, {"times": t, "mean": mean, "sd": sd,
                 "batch_names": data["batch_names"],
                 "early": early, "late": late, "t": tv, "p": pv}


def check_pdf_page(pdf: Path) -> str:
    """实测导出 PDF 的页尺寸，核对"精确 183 mm"这句话是不是真的。

    为什么值得单独一步：源码里写 `FIG_WIDTH_MM = 183` 只是**声明**。
    一旦 `savefig` 带了 `bbox_inches="tight"`（技能模板里的默认写法），
    文件会被按内容裁掉空白，页宽变成一个不确定值 —— 声明和产物就此脱钩，
    而没有任何程序会报错。这里把声明变成**实测**。
    """
    import fitz  # PyMuPDF：和 scipy 同属本脚本的解释器依赖
    doc = fitz.open(str(pdf))
    try:
        r = doc[0].rect
        w_mm, h_mm = r.width * 25.4 / 72.0, r.height * 25.4 / 72.0
    finally:
        doc.close()
    bad = []
    if abs(w_mm - EXPECT_PDF_WIDTH_MM) > 0.5:
        bad.append(f"宽 {w_mm:.2f} mm，期望 {EXPECT_PDF_WIDTH_MM:.1f} mm")
    if abs(h_mm - EXPECT_PDF_HEIGHT_MM) > 0.6:
        bad.append(f"高 {h_mm:.2f} mm，期望 {EXPECT_PDF_HEIGHT_MM:.1f} mm")
    if bad:
        raise SystemExit(
            "⛔ 导出 PDF 的物理尺寸与契约不符：" + "；".join(bad) + "\n"
            "   常见原因：savefig 带了 bbox_inches=\"tight\"（会把空白裁掉）。")
    return f"{w_mm:.2f} × {h_mm:.2f} mm"


def main() -> int:
    data = load_data()

    # 出图 + 对齐门 + 导出，全程录音，事后查有没有字形缺失
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fig, st = draw(data)

        # ① 先把**分面几何清单**落盘 —— 后端中立的对齐审计器（CLI）要它。
        #    require_matplotlib_panel_alignment 内部也会算一遍，但它写出的
        #    是**报告**（图尺寸嵌在 "layout" 里），CLI 直接吃会判 NOT AUDITABLE。
        manifest = matplotlib_layout_manifest(fig)
        (OUT / "fig_conversion.layout.json").write_text(
            __import__("json").dumps(manifest, indent=2), encoding="utf-8")

        # ② 强制对齐门：**最终布局画完之后**、导出之前调用（strict → 不过就抛）
        require_matplotlib_panel_alignment(
            fig,
            json_out=f"{FIG_STEM}.alignment.json",
            overlay_svg=f"{FIG_STEM}.alignment.svg",
            tolerance_pt=1.5,
            gutter_tolerance_pt=1.5,
            strict=True,
        )

        # ③ 导出（文字可编辑 + 精确尺寸 + 600 dpi 位图）
        fig.savefig(f"{FIG_STEM}.svg")
        fig.savefig(f"{FIG_STEM}.pdf")
        fig.savefig(f"{FIG_STEM}.tiff", dpi=600)
        fig.savefig(f"{FIG_STEM}.png", dpi=300)
        plt.close(fig)

    # ④ 字形守卫：matplotlib 缺字只打 warning、画豆腐块、exit 0 —— 升级成硬失败
    glyphs = glyphs_missing(caught)
    if glyphs:
        print("⛔ 字体缺字形 —— 图上会出现豆腐块（□），已拦下，不算交付：",
              file=sys.stderr)
        for g in dict.fromkeys(glyphs):
            print(f"   · {g}", file=sys.stderr)
        print("   处理：换掉那个字符（Arial 缺上标减号 U+207B 这类），"
              "或显式换成含它的字体；不要把'没报错'当成没事。", file=sys.stderr)
        return 2

    # ⑤ 尺寸实测：把"我声明了 183 mm"变成"文件确实是 183 mm"
    size = check_pdf_page(Path(f"{FIG_STEM}.pdf"))

    print(f"核心结论：Conversion rises with residence time, the per-hour gain "
          f"itself grows, and all {len(st['batch_names'])} batches agree.")
    print(f"统计：Welch t = {st['t']:.3f}, P = {st['p']:.4f} "
          f"(n = {st['early'].size} / {st['late'].size} intervals)")
    print(f"尺寸：PDF 实测 {size}")
    print(f"导出：{FIG_STEM.name}.svg/.pdf/.tiff(600dpi)/.png")
    print("字形检查：无缺失 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
