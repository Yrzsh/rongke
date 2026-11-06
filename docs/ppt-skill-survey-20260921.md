# PPT Skill 生态调研（2026-09-21）

> 调研对象：`ppt-master`（PPT Master）、`cyber-ppt`（CyberPPT）、`gordensuper-ppt-skill`（GordenSuperPPTSkills），
> 外加用户指定的优先级方向 **科研 PPT** 所对应的两个专门技能。
>
> **星数/许可/维护状态全部取自 `api.github.com` 实测**（2026-09-21），不取二手文章口径 ——
> 实测发现二手文章的数字互相打架，且普遍严重滞后（CyberPPT 有文章写 743★/890★，实测 1723★）。
>
> 原始抓取件归档在 `evidence/ppt-skill-survey-20260921/`，本报告的每条结论都能回到那份原文。

---

## 0. 一句话结论

**这四家不是四个竞品，是一个"谁把判据铺得更深"的梯度。而荣珂现在的 PPT 判据只有两条硬 FAIL（非 16:9、纯默认外观）—— 恰好卡在这个梯度最浅的一档。**

按"判据覆盖面"排序，从深到浅：

```
PPT Master (55,485★)  ─ 论证 → 设计 → 页面 → 视觉 → 导出，五层各有可跑的检查器
      ↑
CyberPPT   ( 1,723★)  ─ 咨询风 18 道门，但要 PptxGenJS，与荣珂引擎直接冲突
      ↑
academic-pptx-skill (1,023★) ─ 只管"论证层"，纯规则、零工具，最便宜可移植
      ↑
GordenSuperPPTSkills (1,991★) ─ 只解决"好看 vs 可编辑"，无任何质量门
      ↑
荣珂现状              ─ 结构 4 条 + 视觉 2 条硬拦
```

---

## 1. 真实数据（API 实测，2026-09-21）

| 项目 | 仓库 | ★ | fork | 许可 | 建仓 | 最后推码 | 一句话 |
|---|---|---:|---:|---|---|---|---|
| **PPT Master** | `hugohe3/ppt-master` | **55,485** | 4,401 | MIT | 2025-12-10 | **2026-09-20** | 赛道霸主，原生 DrawingML，日更 |
| GordenSuperPPTSkills | `GordenSun/GordenSuperPPTSkills` | 1,991 | 169 | ⚠️ **无 LICENSE 文件** | 2026-06-07 | 2026-06-07 | 先出豪华图，再逆向拆回可编辑 |
| CyberPPT | `crazyykhllc-bit/CyberPPT` | 1,723 | 175 | MIT | 2026-06-24 | 2026-07-20 | 咨询风，18 道门，证据链 |
| academic-pptx-skill | `Gabberflast/academic-pptx-skill` | 1,023 | 110 | ⚠️ **自相矛盾** | 2026-02-28 | 2026-07-14 | 科研论证质量层 |
| academic-ppt-master | `M1n-n9/academic-ppt-master` | 210 | 7 | MIT | 2026-06-18 | 2026-06-18 | PPT Master 的科研落地分支 |
| ppt-master-plus | `gnuhpc/ppt-master-plus` | 54 | 7 | MIT | 2026-06-27 | 2026-09-06 | 加备注/实时预览/模板 |
| ppt-master-zit | `xiaoranlingfeng/ppt-master-zit` | 51 | 4 | MIT | 2026-09-05 | 2026-09-11 | 本机 ComfyUI 配图，免 Key |
| ppt-master-sci-fork | `hongyi652/ppt-master-sci-fork` | 19 | 5 | MIT | 2026-05-23 | 2026-06-11 | **MinerU 解析 + SVG 公式** |
| guizang-ppt-skill | `op7418/guizang-ppt-skill` | 未实核 ⚠️ | 1,632 | AGPL-3.0 | — | — | 单文件 HTML 演讲 deck |

### 两个必须先说清的风险

**① `GordenSuperPPTSkills` 没有 LICENSE 文件。** API 返回 `"license": null`，仓库根目录也没有 `LICENSE`。
README 里写的是"可以商用，**必须标明 Github 出处**，或标记出作者 @Gorden Sun" —— 这是**作者的一段话，不是一份授权文本**。
⇒ 里面的代码/提示词**不要直接抄进荣珂**。要借鉴就借鉴**思路**，不要复制实现。

**② `academic-pptx-skill` 的许可自相矛盾。** SKILL.md 结尾写 `License: Proprietary. LICENSE.txt has complete terms.`，
而 GitHub API 报 `"spdx_id": "MIT"`，README 也写 `MIT — free to use, adapt, and share`。
⇒ 落地前必须先看仓库里 `LICENSE.txt` 的**正文**，不能只看徽章。

**③ 一个依赖地雷（PPT Master）：** 项目是 MIT，但 PDF 转换可选依赖 **PyMuPDF = AGPL-3.0**。
纯 MIT 项目里混进 AGPL 依赖，若整包再分发要注意 —— 它自己的 README 也专门提了这一条。

**④ 维护状态信号不一致的地方，如实标注：** GordenSuperPPTSkills 的 API `pushed_at` 是 2026-06-07，
但 GitHub 页面的提交列表显示 README 是"2 天前"更新 —— 两处对不上。**落地前实际 clone 一次核对**，不要信任何一边。

---

## 2. 逐个解剖

### 2.1 PPT Master（`hugohe3/ppt-master`，v6.6.0）—— 唯一值得整套读的

作者 Hugo He，金融从业者（CPA/CPV/咨询工程师投资），动机很朴素：**"我要 AI 生成的幻灯片在 PowerPoint 里还能改，不要被压成图片。"**

它的核心主张，原文一句，值得贴在这里：

> **Editable is already table stakes — what sets PPT Master apart is native depth.**
> （"可编辑"已经是入场券了，真正的门槛是你到底拿到了多少 PowerPoint 本身。）

**它交付的不是"能改的文件"，是 PowerPoint 的原生对象模型本身：** 原生形状与连接线（带可拖的调整手柄）、
数据驱动图表与表格（带 `Edit Data`）、完整文本/图片/填充/效果模型、以及走模板路线时的真
`p:sldMaster` / `p:sldLayout` 继承。公式编译成**可编辑 OMML**（PowerPoint 2010+）。
SmartArt 是**故意不做**的，不是漏了。

#### 架构：路由 + 阶段框 + 全局纪律

最外层不是"一套流程"，而是**一个路由分派器**：

```
Mandatory Load Order
  1. 读 SKILL.md
  2. 跑 attribution_guard.py  ← 非零立刻停，不许检查、不许修、不许绕
  3. 读 routing.md
  4. 从 routing 权威里选**恰好一条**顶层路由
  5. 只读那条路由的运行时权威 + 它显式触发的支撑文档
```

六条顶层路由：`Generate PPTX`（含 Image-to-PPTX / Beautify 两个保真 profile）、`Create Template`、
`Edit Native PPTX`。配一套 **Plan → Do·Check·Act** 阶段框，和 7 条全局纪律：

1. 串行执行
2. **Blocking 就是停**（每个 `⛔ BLOCKING` 门必须等用户明确确认，不许替用户决定）
3. 不许跨阶段打包
4. **进门前验前置**
5. **不许投机执行**（不在归属步骤之前预制后续阶段的产物）
6. 路由确定性（routing.md 能判的就不许再问用户）
7. **★ Act at the owning layer** —— 失败时在**拥有该缺陷的最浅那一层**修

#### 值得逐条偷的六个机制

**① 词汇表（Vocabulary）—— 一个术语全局只有一个意思。**

开头一张表钉死 28 个词，并明说："Where a word is used in more than one sense, the sense is named here
and the files say which one."（一词多义时，在这里命名，各文件各自指明用的是哪个义。）

> 这一条**直接对症荣珂的 H 型漂移**。荣珂现在的"单源"是**配方**级别的（`DECK_TOOLCHAIN_RECIPE`），
> 但**术语**级别没有单源表 —— 同一个词在两处意思不同，两处都"自洽"，改一处忘一处。

**② `(binding)` 标记 —— 显式声明"这个字段谁有权改"。**

> **Reference**（默认）：一个起手草图，执行角色可以自由调整或替换，无需上游修复、无需说明理由；
> 字段标签后面加 `(binding)` **就移除这份自由**。

这是把"自由度"做成了**字段级的一等公民**。荣珂的契约里"约束/禁令"是一堆散句，没有这种**逐字段的绑定位**。

**③ "Schema validity never proves fidelity."（schema 合法永远不证明保真。）**

原文还有更狠的一句：*"Schema validity never proves fidelity: a final-state → Design Spec mismatch,
an approved Design Spec → lock mismatch, or an unapplied revision blocks."*

> 与荣珂那条血债**完全同频** —— `apply_professional_design` 回 `success: true` 而文件一个字节没动。
> 荣珂已经把这条写成了设计定律（"自称成功不能当证据"），PPT Master 把它做成了**三处交叉比对**。

**④ 双件分离：`design_spec.md`（人读）+ `spec_lock.md`（机读执行契约）。**

设计叙事给人看，执行契约给机器看；**两者必须比对一致**，不一致就阻断。
而且锁是**从已批准的 spec 派生**的，不许反过来。执行角色**每页都要重读一次锁**。

**⑤ 早期门 + 终门，而且门检查的是"方法"不只是"页面"。**

```
P01–P05 → 早期门 → 剩余页 → 终门
```

早期门（第 5 页后、第 6 页前）强制输出一行：

```
gate-signal: method=<rule resolved, or none> | page-local=<count> | not-exercised=<list>
```

判读规则极漂亮：**同一类别同方向出现 ≥2 个问题，或某个问题出现在"每页都重复的写法"里 ⇒ 这是方法级偏差**，
必须先改**权威规则**（比如重跑 `text_measure.py calibrate` 修正每个角色的估算表），
而不是缝缝补补修这一个样本 —— 因为"长 deck 会在终门前把它复制到每一页"。

> 这一条荣珂**完全没有**：荣珂是"交付前必调 audit"，13 页全做完才发现问题，返工成本 = 全量。
> 而且**修样本 vs 修规则**这个区分，正好是荣珂"治法"思路的下一页。

**⑥ token-safe 报告处理（原文是 Hard rule）：**
"on success use the exit status and terminal summary; **never `cat` the complete JSON into context**."
只在失败调查、显式审计、或某字段 stdout 没有时才读 JSON。

> 荣珂"用量只有一个来源/别把大 JSON 灌进上下文"的同一家族，值得写进纪律。

**另外两个细节：**
- **五页锁重读**：P05/P10/P15 之后，下一张之前把 `spec_lock.md` **完整重读一次**——纯重锚色板/字体/图标风格/页面节奏，**不跑检查、不输出、不暂停**。这是对抗长上下文的漂移的。
- **精确 roster**：`design_spec.md §IX` 是**有序队列**，一页对一条，**不许增、删、并、拆、改序**。

#### 科研相关性：它自己就有一个"技术蓝图"样例

README 的示例里有一个 **"Technical Blueprint — Attention Is All You Need: Transformer paper walkthrough:
diagrams, native formulas, notes and animations"** —— 论文讲解 + 原生公式 + 讲稿备注 + 动画。
**这正是荣珂要干的活。** 而且作者还是微软 [ResearchStudio](https://github.com/microsoft/ResearchStudio)
（paper → talk video / poster / blog）的参与者 —— 这条线的方向和我们高度重合。

---

### 2.2 CyberPPT（`crazyykhllc-bit/CyberPPT`）—— 门最多，但引擎与荣珂正面冲突

把 PPT 生成当成**工程流程**，不当创作。走 **SCR 叙事**（Situation → Complication → Resolution），
先建 **MBB 标准证据表**（每条事实/数字/判断都能回溯到源材料，冲突显式记录、不静默归一），
再脑暴 2–3 条故事线收敛，再出逐页大纲 + 密度目标。

**三阶段 · 三确认门**：分析（批故事线与页数）→ 蓝图（8 种固定风格，16:9 独立样张，逐页 ImageGen 蓝图）→
混合还原（复杂视觉存为图片、信息层原生重建）→ 最终确认。

**18 道门**，摘几条最有信息量的：

| 门 | 内容 |
|---|---|
| PPTX 生成工具选择门 | **正式 PPTX 必须用 PptxGenJS；禁用 `python-pptx`** |
| SVG / custom geometry 优先门 | 曲线、弧线、异形边界优先 SVG path / freeform |
| 图标库优先门 | 只从 `assets/icons/` 已验图标里选，且只选**一个**风格库 |
| preset shape 准入门 | 复杂 preset 默认禁用，须通过打开测试 + 容差验证 |
| 曲线/异形精确追踪门 | 桑基图、流线图、弯曲箭头触发 |
| 容器边界/连续文本/表格语义门 | 文字不得越过归属容器 |
| 双硬门槛 | `editable_information_layer_pass` **和** `visual_semantics_preserved` 必须同时为 true |

还有一条很清醒的自述：**"`validate_pptx.py --strict` 通过不等于视觉系统合格"** ——
它明确区分"结构校验器"和"视觉判断"，且 `pictures=0` **不是目标、也不是视觉合格证明**。

**对荣珂的价值与冲突：**

- ⛔ **第 1 条门与荣珂正面冲突**：荣珂整条 deck 链路（`create_presentation` / `style_deck` / 复测）
  就是 python-pptx。**CyberPPT 的引擎规则一条都不能照抄。**
- ✅ 可偷的是**判据形态**：容器溢出门、密度门、以及它那套**容差表**
  （P0 默认 3px 最大 6px / P1 4–8px / P2 6–12px）—— 荣珂要量文字溢出，这是现成的量纲。
- ⚠️ **高热慢更**：1723★ 但 2026-07-20 之后没推过代码，约两个月静默。

---

### 2.3 GordenSuperPPTSkills —— 思路漂亮，授权有雷

作者的口号是"AI PPT 赛道终结者"，README 情绪拉满。但拆开看，它解决的是一个**很真实**的痛点：

> AI 做的 PPT 好看但改不动；能改的 PPT 又不好看。**别让我二选一。**

做法是**把"好看"和"可编辑"拆成两个阶段**，而不是一次性同时解决：

```
GordenImagePPTGen   主题/内容 → 每页 PNG + 图片型 pptx（默认就要求"信息密度极高、排版复杂、豪华"，简单=不合格）
GordenImage2PPTX    图片 → 可编辑 pptx（背景 / 骨架 / 图标装饰 / 文本 四层拆解）
GordenSuperPPTSkill 串联 A→B，一把跑完
```

第二步是它的真功夫，而且规矩偏执到可爱：背景图、框架图、图标图**都必须来自 imagegen**，
**禁止**直接裁原图局部顶上去，**禁止**用原生 shape / PPT 色块 / 代码绘图偷懒，每页都要有
`imagegen-assets-manifest.json`。

**代价它自己也说了**：只支持 Codex（依赖 GPT 生图与视觉能力）；
**转 1 张图约消耗 Plus 订阅 5 小时额度的 10%** —— 一份 13 页 deck 光还原就是 130% 的 5 小时额度。

**对荣珂的价值：** 思路（分层 + 混合保真）可借鉴；**代码不要抄**（无 LICENSE 文件）。
且它**没有任何质量门** —— 是四家里判据最浅的，恰好证明"视觉豪华 ≠ 质量可控"。

---

### 2.4 academic-pptx-skill（`Gabberflast`）—— 科研方向的**纯规则层**，最便宜的补丁

这一个**没有工具、没有脚本、没有引擎**，只有规则。它明确说自己是**内容与结构层**，
技术实现交给 Anthropic 内置的 PPTX skill。

它的核心主张是把 Claude 默认的"设计优先"风格**覆盖**成"沟通优先"：

**★ Action Title（行动标题）** —— 每条标题必须是**陈述结论的完整句子**，不是话题标签。

| ❌ 话题标签 | ✅ 行动标题 |
|---|---|
| "实验结果" | "方法 B 在高噪声下把误差降低了 23%" |

**★ Ghost Deck Test（幽灵甲板测试）** —— **只读所有行动标题，按顺序读，必须能讲完整个论证。**
讲不完 ⇒ 在**规划阶段**就改大纲，**在画任何一页之前**。

其他规则：
- 论证结构：**一个论证，讲透**（别把整篇论文搬上来）；**一页一个洞察**；**每张结果页恰好一个 exhibit**，
  且关键发现**直接标在图上**（"别让观众自己去图上找"）。
- 引用：**借来的每张图/每个数据点都要在页内标引用**，末尾必须有 References 页。
- **结尾必须是 Conclusions 页**（Q&A 期间就停在那一页），**绝不是 "Thank You" 或空白页**。
- 最小设计：白底、**单一无衬线字体**、**最多三色**（默认深蓝 `1F4E79` + 中蓝 `2E75B6`）、
  不要装饰性图标/渐变/标题下划线、正文左对齐、正文 ≤ 20pt 起。
- **10 条验收清单**（可直接当判据抄）：行动标题齐否 / 幽灵甲板测试过否 / 每张结果页一图一 so-what /
  引用齐否 / 有 References 页 / **末页非附录页是 Conclusions** / 末页有联系方式或二维码 /
  正文字号 ≥ 20pt / 无装饰元素 / > 15 页要有章节分隔或面包屑。

> **这是四家里唯一一套"零依赖、纯可复算"的科研判据**，而且大半条**能用 XML 读出来**。
> 对荣珂是性价比最高的一块拼图。

---

### 2.5 科研专线：academic-ppt-master（PPT Master 的落地分支）

`M1n-n9/academic-ppt-master`（210★）是把 PPT Master 那套引擎**专门改成论文汇报**：

- **五模块科研结构**：背景 → 方法 → 结果 → 结论 → 展望。
- **公式是三档策略**（第 7 条确认项里）：
  | 策略 | 行为 |
  |---|---|
  | `mixed`（默认） | 复杂公式渲成 PNG，简单行内保持可编辑文本 |
  | **`render-all`** | **所有公式都渲成 PNG** |
  | `text-only` | 全不渲，保持文本 |
  > 并且明说：**学术/科学/工程/数学 deck，只要用户要公式显示正确，就推荐 `render-all`。**
  > 还有一条实战经验：PDF/DOCX 抽取常把公式糟蹋成 `*QK**T* *√**d**k*` 这种 Markdown 强调碎片 ——
  > **要把这当作"重建忠实 LaTeX 的信号"，不是当作可以贴上幻灯片的文本。** 这句话值钱。
- **公式清单**：确认后写 `images/formula_manifest.json` → 跑 `latex_render.py` →
  以 `Acquire Via: formula` 行进资源表。渲染器有 provider 回退链（codecogs/quicklatex/mathpad/wikimedia）。
- **内置 5 套学术预置**：`academic_paper`（文献汇报/组会）、`academic_defense`（答辩/开题/详细报告）、
  `academic-blue` / `academic-graphite` / `academic-navy` 三个品牌档。
- **⚡ 强制纪律：NEVER directly read / open / view image files.** 所有图片信息只从
  `analyze_images.py` 的 CSV 输出或设计规格里来。
  > 这条对荣珂直接有用 —— **别让模型"看一眼图"来决定布局**，那是最贵最不稳定的读法。

**兄弟分支 `hongyi652/ppt-master-sci-fork`（19★）**：MinerU 解析 + **SVG 公式支持**。
MinerU 是处理扫描版论文 PDF 的利器 —— 如果荣珂要啃扫描页，这条线值得单看。

---

## 3. 横向对比：**判据铺到哪一层**

这是本次调研最该带走的一张表。按"一份 deck 从想法到交付"的五层来切，
看每家在**哪一层有可跑的检查**：

| 层 | 该层要判什么 | PPT Master | CyberPPT | Gorden | academic-pptx | **荣珂现状** |
|---|---|---|---|---|---|---|
| **① 论证层** | 结论先行？标题是句子？逻辑成链？末页是结论？ | ✅ 沟通契约双阶段确认 | ✅ SCR + 证据门 + 故事线门 | ❌ | ✅ **action title + ghost deck + 10 条清单** | ❌ **完全没有** |
| **② 设计层** | 色/字/栅格/风格统一？ | ✅ spec_lock 双件 + 模板路由 | ✅ 8 风格锁定 + 词汇门 | ✅ 出图即定风格 | ✅ 白底单字三色 | 🟡 有 `DECK_THEMES`（只有 1 套默认） |
| **③ 页面层** | 一页一洞察？密度够？ | ✅ **定量表 + 校准** | ✅ 密度门 | ❌ | ✅ 一页一洞察 | 🟡 结构 4 条（页数/讲稿/越界/密度） |
| **④ 视觉层** | 溢出？对齐？曲线？可编辑？ | ✅ 早期门+终门+carrier receipt | ✅ **18 门 + 容差 px 表** | ✅ 四层拆解 | ✅ 无装饰 | 🔴 **只有 2 条硬 FAIL** |
| **⑤ 交付层** | 渲染回看？manifest？ | ✅ 检查器指纹绑定导出 | ✅ `visual_qa_gate.json` | ✅ manifest | ✅ QA 清单 | 🟡 `audit_deck` 报告 |

**读出来三件事：**

1. **① 论证层是荣珂最大的空白，也是四家里最容易补的** —— 因为 `academic-pptx-skill` 那套是**纯规则、零依赖**，
   而且大半条**能从 .pptx 的 XML 里读出来**（标题文本是不是完整句、有没有 References 页、末页是不是 Thank You、
   有没有图）。**判据的覆盖面就是产出的上限 —— 这一层现在是 0。**
2. **④ 视觉层荣珂只有 2 条硬 FAIL，是"够不够档次"的下限而非"好不好"**。
   `probe_pptx` **已经在数** `pictures / charts / tables / layout_kinds / fonts_set_ratio` 了 ——
   **数据早就在手里，只是没进判据**。这是最划算的一次改进。
3. **③ 页面层荣珂已经有一条别人都没有的东西**：`set_slide_notes` 的**逐页讲稿**。
   PPT Master 的讲稿是"信息承载组"级别的严格对齐，但荣珂这条链路是更窄、更硬的。

---

## 4. 对荣珂的可落地清单（按性价比排序）

> 原则照旧：**没有工具的动作写多少纪律都是空转；没有判据的工具加了也是空转。**
> 下面每一条都同时说清"工具"和"判据"两件事。

### P0 —— 一次改动就能测量到收益（纯可复算，几乎不可能误伤）

**P0-1 ★ 把"图表存在"从记录升级为判据。**
`probe_pptx` 已经读了 `facts.pictures / facts.charts / facts.tables`，现在只写进 `extra` 不参与判定。
科研 deck 里"13 页 0 张图"就是不合格。建议加一条 **WARN 起步**（不是 FAIL，避免误伤纯文字 deck）：
`pictures + charts + tables == 0 且 页数 ≥ 6` ⇒ WARN「整份 deck 零图表，科研汇报通常是证据驱动」。
> 这条今天是**零成本**的 —— 数据已经在 `DeckFacts` 里了。

**P0-2 ★ 加一条"文字溢出"判据，量纲抄 CyberPPT。**
CyberPPT 的 **Container Overflow Gate** + **P0 3px/6px、P1 4px/8px、P2 6px/12px** 容差表是现成的。
荣珂已有 `_run_sizes()`（读每个 run 的字号与字数），配上形状 bbox 就能估"这段文字会不会溢出让位框"。
> 这是荣珂"丑"的最常见形态之一，而且**完全可复算**。

**P0-3 视觉判据从"2 条硬拦"扩到"2 硬拦 + 3 条硬拦线"。**
按 `academic-pptx-skill` 的最小设计标准，三条**不可能误伤**的：
- 成功识别的主题里**正文色数 > 3**（科研 deck 判 FAIL/WARN）—— 荣珂 `DECK_THEMES` 是单源的，可对标。
- **末页是 "Thank You" / "谢谢" / 空白** ⇒ WARN（学术场合应该停在 Conclusions）。
- **没有任何一页出现 References / 参考文献字样** 而 deck 里含图表 ⇒ WARN。
> 这三条都能从 XML 读，且规则来自一个 1023★ 的专业技能，不是我自己编的。

### P1 —— 需要新增一步，但收益明显

**P1-1 ★ 论证先行：配方从"建文稿"前加一步。**
现在 `DECK_TOOLCHAIN_RECIPE` 第 1 条就是 `create_presentation`。
PPT Master 和 academic 两条线**都是"先定论证与结构，再画页"**。
建议在配方**最前面**插一条产出物：逐页**行动标题清单**（一行一页，必须是陈述结论的完整句）。
> 格式建议直接抄 `gate-signal` 那种**一行机器可读**的风格，方便后续判据核对。

**P1-2 ★ 早期门：`audit_deck` 支持"中途体检"。**
PPT Master 的 P05 早期门是"**把方法级偏差在第 6 页之前掐掉**"，而荣珂是**交付前一次体检**，
13 页全做完才发现 ⇒ 返工 = 全量。
建议：`audit_deck` 加 `stage="early"` 参数（前 5 页就够触发），判据只报"**方法级**"问题。

**P1-3 公式策略显式化（科研刚需）。**
`academic-ppt-master` 的 `mixed / render-all / text-only` 三档 + **"PDF 抽取的 `*QK**T*` 碎片要当重建信号"**
这条经验，应直接写进荣珂的**科研 deck 配方**。哪怕暂时没有 `latex_render.py`，
**"识别 + 明确告知模型这是重建信号"本身就能提升产出**。

### P2 —— 结构化改进，治的是"漂移"这一类老病

**P2-1 ★ 建术语单源表。** 荣珂的单源目前只到"配方"级。PPT Master 的 Vocabulary 表是为**跨 40+ 文件**准备的，
荣珂的文件规模小得多，但 `kind` / `attrs` / `data` 这类字段名陷阱已经踩过 **5 次** ——
正是"一词多义 + 没有单源"的症状。

**P2-2 契约字段加 `(binding)` 语义。** 现在"约束/禁令"是一堆散句；
改成"字段 + 是否绑定"能减少模型在自由区和锁死区的混淆。

**P2-3 把"修方法 vs 修样本"写进纪律。** 荣珂已有"治法"文化，缺的是**判据**：
"同一类别同方向 ≥2 个问题 ⇒ 先改权威规则，别修样本。"

### ⛔ 明确不要做的事

| 不要做 | 为什么 |
|---|---|
| 把 CyberPPT 的引擎规则搬进来 | 它**禁用 python-pptx**，与荣珂整条链路正面冲突 |
| 抄 GordenSuperPPTSkills 的代码 | **无 LICENSE 文件**，README 的商用声明不构成授权 |
| 引入 Gorden 的"图片还原"路线 | 转 1 张图 ≈ Plus 5 小时额度的 10%，且**它自己没有任何质量门** |
| 抄 guizang 的代码 | **AGPL-3.0**（传染性），只可远观其设计 |
| 直接依赖 PPT Master 全包 | 它的可选依赖 PyMuPDF 是 AGPL-3.0，且整包 56MB |

---

## 5. 建议的下一步（三选一，按投入排序）

| 方案 | 内容 | 成本 | 产出 |
|---|---|---|---|
| **A. 只偷判据**（推荐先做） | 把 §4 的 **P0 三条**落成 `DeckAuditor` 的新一节（论证/图表/溢出），复用现有 `probe_pptx` 数据 | 小，一个模块内 | 视觉判据从 2 条 → 5–6 条，且全部可复算 |
| **B. 拿来主义 + 对照** | 实际 clone `hugohe3/ppt-master` 与 `M1n-n9/academic-ppt-master` 到本地，只读 `references/` 与 `workflows/`（**不接引擎**），把 `design_spec + spec_lock` 双件结构与 §IX roster 思路对照荣珂的契约设计 | 中 | 一张"荣珂契约 vs PPT Master 契约"的逐条对照表 |
| **C. 全套借引擎** | 把 PPT Master 当独立后端，荣珂只做编排与门禁 | 大 | 与荣珂"自研内核"的定位冲突，**不建议** |

### 落地进度（2026-09-21 当天 · 方案 A）

**方案 A 已执行，但没照单全收。** 三条里一条本来就在、一条按误报治理拆成"做一半、明确不做一半"，
实际新增的是**从 §3 那张表反推出来的论证层判据**（这张表比 §4 清单更值钱）：

| 条目 | 状态 | 说明 |
|---|---|---|
| P0-1 图表存在 | ✅ **早就在**（`no_visual_elements`） | 门槛**保持 `≥10 页`、没降到 6**：6–9 页纯文字的概念讲解是合法的（见 `deck.py` 就地注释） |
| P0-3 末页形态 | ✅ **已落**：`ends_with_thanks` / `last_slide_blank` | WARN ＋ 高门槛 |
| P0-3 正文色数 > 3 | ⏸ **未做** | 要读 XML `solidFill`；留下一轮 |
| P0-3 无 References 页 | ❌ **明确不做** | **组会产物本来就没有参考文献页** ⇒ 会天天误报。⚠️ 不做也要写下来，否则下一个人会加回来 |
| P0-2 文字溢出 | ⏸ **未做** —— **下一轮首选** | 要形状 bbox ＋ `_run_sizes()`；报告自己说它是"『丑』的最常见形态" |
| **新增（§4 没写）** | ✅ `titles_are_noun_phrases` | 标题全是名词短语 ⇒ 读目录读不出论点。**这是从 §3 那张"判据铺到哪一层"表反推的** |

**判据上线前，先量了它在真实产物上的误报率**（`title_corpus_check.py`，常驻守卫）：

```
真实任务目录（runs/<日期>-<时间>/）26 个 / .pptx 3 份，进分母 3 份
  命中（≥0.70）：0 份 = 0.0%
  干净（<0.40）：3 份
⇒ 语料够、误报在阈值内（退出码 0）
```

⚠️ **这个 0% 是换过分母才拿到的。** 第一版按 mtime 取"最近 60 份 `runs/**/*.pptx`" ⇒ 报 **90% 命中**，
差一步就把一条好判据砍掉。真相：mtime 最新的一批**全是刚被 `sweep_probes`/`_tkit` 挪进
`runs/_trash/` 的测试夹具**（`argument/noun.pptx` 就是 §20.1 正控自己造的）
⇒ **拿回收站当"真实产物"，量出来的是判据对自己夹具的命中率。**

⇒ **通用问法（项目的第三问）**：「这条判据的**分母**是什么？它在**正确的稿子**上会不会报错？」

**测试**：`deck_test.py` 248 → **260** 项全绿（新增 §20 论证层 9 项，含**真实成片误报守卫**与**单源守卫**）。
**侧证**：同一份内容，`deck.pptx`（4:3 ＋ 纯默认）⇒ `FIX BEFORE DELIVERY`；
`deck_juanqing.pptx`（16:9 ＋ 绢青陈墨）⇒ **PASS**（五门全过、零 WARN）。

---

### 后续（同日 · 同一轮）：P0-2 之前，先挖出**更靠前**的一层 —— 骨架几何

**重新定性。** §5 说「文字溢出」是"『丑』的最常见形态"。但接着往下量，发现**在它之前**
还有一层、而且更致命：**内容区根本没铺满**。

那份成片**自带一份对照组**（同一页上）：`style_deck` 自己画的页脚铺到 **95.4%**、
继承来的 `Title`/`Content Placeholder` 只到 **71.2%** ⇒ **两套坐标系挤在一张纸上**，
每页右侧空出近三成。
根因：`style_deck` 原实现**只改 `p:sldSz`**，而占位符几何住在 **master** 里、还是 4:3 摆的
（实测 slide 层 0 个 `a:xfrm`、layout 层 0 个、**master 层才有**）。

| 判据 | 抓什么 | 性质 |
|---|---|---|
| `placeholder_geometry_stale` | 内容占位符左右边距比 > `GEO_ASYM_MAX`(3.0) | **纯坐标比较、零估算** |
| `placeholder_offcanvas` | 左右边距出现负值（骨架比画布还宽） | 同上 |

- **豁免**：`dt`/`ftr`/`sldNum` 的坐标是**锚点语义**，本来就靠边
  （实测 master 的 `sldNum` 左右比 **14.3 倍**）⇒ 不排除则**每一份 deck 都被报**。
- **修复** `remap_skeleton()`：**保持边距、拉伸宽高**（＝ PowerPoint「最大化」语义），**天然幂等**。
- **硬化**：画布若早被改过（旧版产物的存量污染），以**较小侧边距**为基准重铺 ⇒ 存量文件也修得回来。
- **单源**：门槛 `GEO_ASYM_MAX` **经 payload 注入**给子进程脚本（它引用不到模块常量），
  判据 / 复测 / 修复三处共用同一个数。

★ **唯一能证明"修对了"的是路径无关性**：① 存量污染（比值 7.67）与 ② 真实 4:3 → 16:9，
两条**不同输入路径修完撞在同一个数**（比值 1.0、占宽 **92.5%**）。
"把 7.67 拉成 1.0"有一万种办法，只有"和正常流程一致"排除掉了瞎拉。

**测试**：260 → **277** 项全绿（§21 十七项）；全套回归 **1502 → 1519 项 / 0 失败**，基线已更新。

**P0-2 的现状（降级为"下一轮"，但理由写清楚）**：溢出估算 `_sp_overflow` **已实现、未接判据**
—— 真实产物量到 **0** 个形状，而根因是**几何数据在 master 层**（与"字号继承链未解析"同源）
⇒ **没有量到，不等于不存在；只是当前这把尺子量不到。**
⇒ 顺序上先修**能量到的**（骨架几何），溢出留到解析出继承链之后再定门槛。

**⚠️ 本轮的事故与恢复**：给成片重做交付物时，我以为 `style_deck` 会产出
`deck_juanqing.pptx`，实际它是**就地改写** ⇒ `runs/20260921-124950/deck.pptx`（4:3 原件）被盖掉，
且 `runs/` 在 `.gitignore` 里、git 捞不回。
恢复路径：`session.json` 的 `events[].data.args` 存着**完整入参**（`trace.jsonl` 只有 `digest`）
⇒ 照归档重放 34 次调用，对着 `deck_report.json` **逐项校验**（13 页 / `notes_ratio` 1.0 /
`min_body_font_pt` 16.0 / 画布宽 9144000）**全部一致**。
产出工具 **`replay_deck.py`**（仓库根），细节见 skill `rongke-agent-ops`。
★ **顺带得到的判据**：`session.json` 是"这次任务到底怎么建出那个文件的"的唯一答案
⇒ **任何清理运行痕迹的守卫，都不许把它当垃圾扫掉。**

---

## 6. 留档：本次调研没能拿到的东西（如实标注）

1. **`skills/ppt-master/SKILL.md` 的更深层 references**（`strategist.md` / `executor-base.md` /
   `semantic-svg.md` / `native-shape-authoring.md` / `governance/failure-recovery.md`）—— 只读了被直接引用的片段，
   `failure-recovery.md`（失败恢复的归属层设计）**没读到**，那是荣珂最该看的之一。
2. **`guizang-ppt-skill` 的星数未实核**（各聚合站口径从 16.8k 到 26.3k 不等，未打 API）。
3. **GordenSuperPPTSkills 的实际提交时间无法确定**（API 与页面互相矛盾，见 §1 风险④）。
4. **四份技能全部没有本地实测出图** —— 本次是**文档与结构层面的调研**，没跑过任何一份。
   若要判断"效果"而非"设计"，必须真跑。

---

*证据文件：`evidence/ppt-skill-survey-20260921/`（6 份原始抓取件，共约 162KB）*
