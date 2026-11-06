# QA 记录 · Fig. 1（`fig_conversion`）

> 交付包的一部分，不是可选附件。nature-figure 的 QA 合同要求：
> 每个 WARN 都要**说明理由或改掉**，且报告必须与这一版图对应。
> 本文件由 `figures/figure-qa.json` 的实测数据抄写，非人工声称。

生成时刻：见 `fig_conversion.pdf` 的 mtime 与 `figure-qa.json:audited_at`。
审计入口：`rongke.figure.FigureAuditor.audit()`（`python -m rongke --figure-audit`）。

---

## 1. 逐门结果

| 门 | 判定 | 实测 |
|---|---|---|
| source（源码预检） | **PASS** | 21 pass / 0 warn / 0 fail |
| glyphs（PDF 字形下限） | **PASS** | 最小 5.6 pt，低于 5 pt 下限 **0** 处，文本段 39 个 |
| collision（渲染后碰撞） | **PASS** | 0 fail / 0 warn；6 处「文字整体落在填充内」= 信息性，非缺陷 |
| alignment（多分面对齐） | **PASS** | 2 组比较 / 0 失败 / 0 警告，tolerance 1.5 pt |
| legend（图注体检） | **PASS** | 0 条发现 |

**尺寸实测**：PDF 页 = **183.00 × 52.00 mm**（脚本内用 PyMuPDF 实测核对，
与契约声明的 183 mm 相符，容差 0.5 mm）。
—— 声明写在源码里只是声明，这一步是把它变成**文件事实**。

---

## 2. 两条 WARN 的处置（都已改掉，不是记录在案放行）

初版源码预检是 **19 pass / 2 warn**。两条都改了根因，没有一条是靠调阈值或写豁免：

### `FINAL-WIDTH`｜"No static final width detected"

- **根因**：预检用 `(?:fig_)?width_mm\s*[:=]` 这个正则去找最终宽度，
  而初版把常量命名成 `W_MM, H_MM = 183.0, 52.0` —— 名字对不上，它认不出来。
- **处置**：常量改名成 `FIG_WIDTH_MM / FIG_HEIGHT_MM`。
  这不是迁就正则：这两个名字本来就比 `W_MM` 自解释，改名是**顺手的正确做法**。
- **附带改掉的真问题**：初版开了 `savefig.bbox="tight"`。
  它会把图按内容裁掉空白，于是**页宽变成一个不确定值** ——
  源码里写着 183 mm、文件里却不是。现在显式不开 tight，
  并加了一个实测断言：PDF 页宽偏离 183 mm 超过 0.5 mm 就直接报错。

### `DEMO-DATA`｜"Simulated data appears in the plotting source without an explicit demo boundary"

- **根因**：`np.random.default_rng()` / `rng.normal()` 长在**绘图脚本**里。
  这条检查没有 PASS 通道 —— 只要绘图源码里有随机数发生器就永远是 WARN。
- **处置**：把职责拆开。
  - `make_demo_data.py` —— 造数据，产物落成 `conversion_source_data.csv`；
  - `plot_conversion.py` —— **纯渲染器**：读 CSV → 出图，一个随机数都没有。
  - 顺带把面板 (c) 的**随机抖动**换成**黄金比低差异序列**的确定性抖动。
    这是真改进：点的位置是图的一部分，用随机数意味着换台机器就可能画出不同的图，
    审稿人复现不出来。现在同一份 CSV 在任何机器上都出同一张图。
- **为什么这不是"绕过检查"**：检查想表达的正是「绘图脚本不该负责造数据」。
  拆完之后那句话对**绘图脚本**成立，而数据来源有了一个能点开的答案（那个 CSV）。
  合成数据这件事本身**没有隐瞒** —— 见下面第 4 节。

---

## 3. 逐分面自查（规范要求，不采信整体一瞥）

| 分面 | 独家论据（别的分面答不了） | 中心/离散 | 重复单位 | 对齐组 | 碰撞 | 通过 |
|---|---|---|---|---|---|---|
| a | 主效应存在：转化率随停留时间单调上升 | mean ± s.d. | 3 批次 | 第 1 行 3 等宽面板组 | 0 fail | ✔ |
| b | 排除"某一个批次把结论带出来的"：逐批次配对增量 | 原始值（不聚合） | 3 批次 | 同上 | 0 fail | ✔ |
| c | 二阶说法站得住：增量**本身**在变大，不是噪声 | mean ± s.d. | 6 / 9 个区间 | 同上 | 0 fail | ✔ |

三个分面**互为补角**，去掉任一个论证就有洞：
(a) 只有趋势 → 可能是单批次假象；(b) 只有配对 → 看不出增量在变大；
(c) 只有分布 → 丢掉了随时间的形状。

**关于 (c) 用 Welch 检验而不是配对检验**：0–2 h 与 3–5 h 两组区间来自不同的时间段，
不构成逐点配对，所以用双样本 Welch t（不假设方差齐）。若硬套配对检验，
等于凭空多假设一层对应关系。

---

## 4. 数据来源与披露（数据完整性）

| 项 | 值 |
|---|---|
| 源数据文件 | `conversion_source_data.csv`（3 批次 × 6 时间点，长宽表：`time_h` + 每批次一列） |
| 来源 | **合成数据**，由 `make_demo_data.py` 生成，种子固定 `20260918` |
| 被排除的行/列 | **无**。全量使用，无任何子集筛选 |
| 排除规则 / 理由 | 不适用（没有排除） |

⚠️ **明确披露**：本图的数值是**合成数据**，用来演示这套制图标准，
**不是实测结果**，不可用于任何科学结论。投产时把
`conversion_source_data.csv` 换成实测数据即可，`plot_conversion.py` 一个字不用改 ——
这正是"绘图脚本必须是纯渲染器"带来的好处。

---

## 5. 导出包（`figures/`）

| 文件 | 角色 |
|---|---|
| `plot_conversion.py` | 绘图源码（纯渲染器） |
| `make_demo_data.py` | 数据准备（**仅演示用**，投产替换） |
| `conversion_source_data.csv` | **源数据**，定量面板可逐点追溯 |
| `fig_conversion.svg` | 矢量，文字可编辑（`svg.fonttype=none`） |
| `fig_conversion.pdf` | 矢量，文字可编辑（`pdf.fonttype=42`），精确 183×52 mm |
| `fig_conversion.tiff` | 600 dpi 位图 |
| `fig_conversion.png` | 300 dpi 预览（**不是**投稿格式，勿误投） |
| `fig_conversion.legend.md` | 图注 |
| `fig_conversion.layout.json` | 后端中立的**分面几何清单**（对齐门的输入） |
| `fig_conversion.alignment.json` / `.svg` | 对齐门报告 / 叠加图 |
| `fig_conversion.collision-audit.json` / `.pdf` | 碰撞审计报告 / 标记图 |
| `figure-qa.json` | 本图面的总审计报告（机器可读，含各产物 mtime） |
| `fig_conversion.qa.md` | 就是本文件 |

**QA 专用产物不是投稿资产**：`*.alignment.svg` 画的是量出来的分面矩形、
`*.collision-audit.pdf` 画的是碰撞标记 —— 它们只用于审计，
**不能**当作预览图或替代导出交给期刊。

---

## 6. 复现方式

```bash
cd figures
python make_demo_data.py        # ① 造源数据（仅演示）
python plot_conversion.py      # ② 出图 + 对齐门 + 字形守卫 + 尺寸实测
cd ..
python -m rongke --figure figures/figure-qa.contract.json   # ③ 四道渲染后审计 + 图注 + 新鲜度
python -m rongke --figure                                  # 装置自检（解释器/技能脚本在不在）
```

退出码：`0` = 全过 / `1` = 有阻断 / `2` = 装置不全或契约不成立。
任一步的非零退出码都意味着**这一版不算交付**，不许复用上一版的绿报告：
审计报告里记着每个产物的 mtime，改过图而没重跑审计会被判 `QA-STALE`。
