# -*- coding: utf-8 -*-
"""make_demo_data.py —— 演示用合成数据生成器（**不在绘图脚本里**）

为什么单独一个文件：nature-figure 的源码预检有一条 `DEMO-DATA`，
只要绘图源码里出现 `np.random.*` / `default_rng(` 就**永远**是 WARN ——
它没有 PASS 通道。这不是洁癖，是把「绘图脚本」和「造数据脚本」的职责分开：

  · 绘图脚本必须是**纯渲染器**：读源数据 → 出图。数据从哪来不归它管。
  · 造数据/清洗归数据准备这一步，产物落成 CSV，可被独立复核。

好处很实在：源数据 CSV 一旦落盘，图就能被任何人从同一份数据重画出来，
而"图里的数是怎么来的"这个问题有了一个**能点开的答案**（就是那个 CSV）。
原来那种「rng 在绘图脚本里现造」的写法，换个 matplotlib 版本都可能画出不同的图。

⚠️ 本文件产出的是**合成数据**，只用于演示这套制图标准。
真做论文时把 `conversion_source_data.csv` 换成实测数据即可，绘图脚本一个字不用改。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent
TARGET = OUT / "conversion_source_data.csv"

# 固定种子 = 可复现。图上任何一点的数值都能被重跑出来。
SEED = 20260918


def build() -> dict:
    """3 个独立批次 × 6 个时间点。

    刻意让「每小时增益」随时间变大（隐式二次增长），并给批次一个固定偏移 ——
    这样面板 (b) 的配对增量才真的能排除「某一个批次把结论带出来的」。
    """
    rng = np.random.default_rng(SEED)
    times = np.array([0, 1, 2, 3, 4, 5], dtype=float)
    base = 0.35 * times + 0.55 * times ** 2
    batch_offsets = np.array([-1.1, 0.2, 0.9])
    conv = np.vstack([base + off + rng.normal(0, 0.32, times.size)
                      for off in batch_offsets])
    return {"time_h": times, "conv_pct": conv,
            "batch_names": [f"Batch {i + 1}" for i in range(conv.shape[0])]}


def write(data: dict, path: Path = TARGET) -> Path:
    t = data["time_h"]
    conv = data["conv_pct"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_h", *data["batch_names"]])
        for i, ti in enumerate(t):
            w.writerow([f"{ti:g}", *[f"{v:.4f}" for v in conv[:, i]]])
    return path


def main() -> int:
    d = build()
    p = write(d)
    conv = d["conv_pct"]
    print(f"合成数据已写入：{p}")
    print(f"  形状：{conv.shape[0]} 批次 × {conv.shape[1]} 时间点，种子 {SEED}")
    print(f"  数值范围：{conv.min():.2f} ~ {conv.max():.2f} %")
    print("  ⚠️ 这是**合成数据**，仅为演示制图标准；投产前请替换为实测数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
