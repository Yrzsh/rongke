# PPT Skill 生态调研 · Part 2：深水区与落地方案（2026-09-21）

> ## ⛔ 读本文前必看：§2.1 有一处**撤回**（2026-09-21）
>
> 本文初版在 §2.1 提议「逐页：正文含数字且该页无图 ⇒ WARN」，code = `page_has_numbers_but_no_visual`。
> **该提案已实测证否，切勿实现。** 实测打红面 **99.6%（1544/1550）** —— 它会打红封面页、
> 目录页、结论页，也就是**这类任务的常态**。
> 已改为**把「缺图」从判据降级为声明**（详见 §2.1）。可复算脚本：
> `evidence/ppt-skill-survey-20260921/measure_page_visuals.py`。
>
> **通用教训（比这一条本身更重要）**：
> **任何新增判据，落地前必须先在真实语料上量「打红面」——而且两个方向都要量。**
> 只知道"它会不会漏报"不够，还要知道"它一天要喊几次"。
> 一条打红面 99.6% 的判据和一条打红面 0% 的判据，**是同一种失败**。

> Part 1（`ppt-skill-survey-20260921.md`）是**广度**：四家的定位、星数、许可、判据五层矩阵。
> Part 2 是**深度**：把上一轮明确留档的缺口回填，挖出四家里**只有 PPT Master 有**的那几套机制，
> 并给出**精确到函数与判据公式**的落地方案。
>
> 新增证据：`evidence/ppt-skill-survey-20260921/pm/`（`failure-recovery.md` / `strategist.md` / `executor-base.md`）。
> 抓取脚本 `_fetch_pm.py` 一并留档（只用 contents API + jsDelivr 镜像两道，见 Part 1 §通道那节）。

---

## 0. 上一轮的缺口，回填情况

| Part 1 说"没拿到" | 现状 |
|---|---|
| `governance/failure-recovery.md` | ✅ **已读**（12KB），本 Part §1-② ③ 就是它 |
| `references/strategist.md` | ✅ **已读**（38KB），§1-⑥ |
| `references/executor-base.md` | ✅ **已读**（52KB），§1-① ④ ⑤ ⑦ |
| `references/semantic-svg.md` / `artifact-ownership.md` | 📥 **已下载**（5.6KB / 20KB），未细读 |
| `docs/technical-design.md` / `why-ppt-master.md` | 📥 已下载（136KB / 13KB），**刻意不读**（讲"为什么用 SVG"，与荣珂 python-pptx 链路不同源） |
| 四份技能真跑出图 | ❌ 仍未跑（不变） |

> 抓取最终战绩：7 份文档全部到手（`evidence/ppt-skill-survey-20260921/pm/`，共约 291KB），
> **全部走 jsDelivr 镜像**（`via=jsdelivr` 6 份 / `via=api` 1 份）。
> 有趣的是 `governance/failure-recovery.md` 那行显示 `FAIL` —— 那是**路径候选表里的第一条**
> （`skills/ppt-master/governance/...`）没命中，下一条 `.../workflows/governance/...` 就成功了。
> ⇒ **"路径错"和"网络坏"必须分开判**（这也正是抓取脚本里两个候选都试的原因）。

---

## 1. 七条深水区机制（PPT Master 独有，且**零引擎依赖**可直接移植）

### ★★★ ① "Absence needs a reason."（缺席需要理由）—— 本次最有价值的一条

这是对 **`pictures=0` 那个死结**的正面解。荣珂现在的处境是（见 MEMORY §4）：

> "只差'配图'一环 —— `pictures=0` 要靠模型自己插，**门不拦也不催**。"

PPT Master 的答案是：**不逼它必须有，但强迫它为"没有"写一行理由。** 原文：

> **Absence needs a reason.** Each of these receipt facts needs one written line — what carries that job
> instead, and why that serves the reader better: a deck-wide `Presets: (none)`; `inline emphasis 0`,
> `gradients 0`, or `filters 0`; **fewer pages carrying a preset or connector than pages whose
> relationship line names `order`/`link`/`parent`/`membership`**; `icons: 0` while an icon pool was registered.

而且它**预先堵死了所有偷懒借口**，这句是全文最狠的：

> **The style, speed, restraint, "text was enough", or "it is editable anyway" are not reasons.**
> Choosing not to use a device is valid; **only an unstated reason is not.**

**为什么这条对荣珂是完美形状**（对照我们自己的第三问"分母是什么"）：

| 判据形态 | 分母 | 会不会误报 |
|---|---|---|
| 「必须 ≥1 张图」 | 所有 deck | ❌ **会误伤**纯文字 deck（诚信报告、纯论述型汇报） |
| 「零图 ⇒ FAIL」 | 所有 deck | ❌ 同上，且逼模型硬塞无意义的图 |
| **「零图 ⇒ 必须有一行理由」** | **只有"零图"的那批** | ✅ **不会** —— 理由成立就放过，没写就拦 |

⇒ **这是一个"不接受沉默"而不是"强制动作"的判据。** 它对荣珂现存的 `Finding` 机制几乎零改动：
`Finding(level, code, message, evidence)` 里的 `evidence` 字段天然就是这个"理由位"。

### ② 失败恢复矩阵：**"必需产物" vs "便利面"** 的二分

`failure-recovery.md` 的第一条硬规则：

> **a failed required artifact blocks the next gate.
> A failed convenience surface falls back to the canonical channel and does not block the route.**

然后是一张 **5 列 27 行**的矩阵：`失败点 | 是否阻断 | 自动恢复 | 需用户介入 | 恢复入口`。
从"Confirm UI 启动失败"到"`svg_to_pptx.py` 失败"逐条写死，并且明确
**"Derived artifact stale ⇒ 从它的拥有源重新生成"**、**"Validation failure ⇒ 只重跑失败的与受影响的下游"**。

**对荣珂的意义**：荣珂的失败处理散在 docstring 与返回值里（`StyleDeck.problems`、`NotesWrite.problems`、
`DeckReport.blockers/warnings` —— 其实**已经有"阻断 vs 警告"的二分了**），
缺的是这张**把每个失败点、恢复动作、恢复入口一次列全**的表。它同时还是一份**可审计的文档**：
用户问"你这个会不会卡死"时，答案是那张表，不是一句"有处理"。

### ③ **"Forbidden — silent downgrade"（禁止静默降级）**

> never skip a required gate because a downstream command might tolerate the missing file,
> and never change a confirmed execution value to keep the route moving.

**对荣珂的意义**：这条正是 `style_deck` 里那段注释的**通用化**：
"复测做不成 ⇒ `ok=False` 并说明**核不了**，绝不渲染成'套好了'"。
荣珂已经**实践**了这条，但没把它写成**纪律**。写成纪律 = 让它约束以后新增的每一条门。

### ④ **合并修复纪律：一次跑完、一次验、不许逐个改逐个查**

> run the checker unfiltered, review the complete issue set, fix every error plus the selected warnings
> **in one consolidated pass, verify once. Never check between individual fixes**,
> never `cat` a passing report, never defer errors past `finalize_svg.py`.

配套的是早期门那句：**"a still-failing verification is the next batch"**。

**对荣珂的意义**：荣珂踩过 `max_identical_calls=3` **拦不住"参数各不同"的重复**（MEMORY §4 未修）。
"不许在两次修复之间跑检查"是一条**行为约束**，而 `max_identical_calls` 是一条**计数约束** ——
**约束行为形状 > 约束次数**。这是修那条未修项的一个新角度。

### ⑤ 宽度估算：**校准一次、按角色算、永不为了估算删词**

> Width is estimated, **never measured per page**. The route calibrates every role once before P01 …
> size each zone as `characters ÷ that rate × 100`; a line mixing CJK with Latin **adds the two parts**
> (`CJK chars ÷ CJK rate + other chars ÷ Latin rate`; acronyms/uppercase use the CAPS rate, numbers the DIGITS rate);
> the rates are **sample averages while the checker measures real glyphs**, so keep about **5% below the bounds width**.
> **Never trim wording to satisfy an estimate.**

而且给了**明确的修复优先级**（不是简单判 FAIL）：

> When text does not fit, **expand a zone with unused space first, then reflow or switch texture
> (prose → points) before dropping a qualifier**; **larger bounds never repair off-canvas text.**

**对荣珂的意义**：这是 P0-2"文字溢出判据"的**完整答案**——不只给量纲，还给了
①**校准口径**（CJK/Latin/CAPS/DIGITS 四种字宽率，各角色一张表）
②**5% 余量**这个防止"估算本身误报"的缓冲
③**修复优先级**（扩框 > 改版式 > 删限定语），而不是让模型自己瞎猜。
荣珂已有 `_run_sizes()`（逐 run 字号 ＋ 字数），**离这条只差一张字宽率表和一段几何求交**。

### ⑥ 双闸 + `(binding)` + **"禁止项必须是用户原话"**

`strategist.md` 的三条：

**a. `(binding)` 的精确语义**（比 Part 1 更准）：

> **Reference**：a starting sketch: Executor adjusts or replaces it freely … **It carries no binding semantics;
> label it `(binding)` only when the user, a template, or a resource contract requires that property.**

**b. GATE 1 — active-decision fidelity 的判据，与荣珂那条血债同形**：

> Missing or substituted values, unapplied revisions, or **silently changed semantic types**
> block **despite schema validity**; bounded Reference adaptation and unused Permission remain valid.

注意后半句：**"受约束的 Reference 自适应"和"未被使用的许可"仍然合法** ——
**这是分母正确性**：闸门不许把"我允许你自由发挥的地方"判成违规。

**c. ★ "Hard rule — a lock prohibition is the user's"**：

> `forbidden` takes the technical baseline plus **prohibitions the user stated in their own words,
> each quoted verbatim and tagged `(user)`**. … **A confirmed direction's behavior stays identity prose
> and is never projected into a prohibition.**

**对荣珂的意义**：`TaskContract` 有 `forbidden: list[str]` 字段。
这条规则说：**禁止项里只许放用户原话（逐字引用）＋技术基线，不许由系统"推导/投影"出来。**
这直接治 H 型漂移的一个来源 —— **系统自己脑补出来的禁令，会随系统演进而漂移，而用户原话不会。**

还有一条极诚实的自白，值得单独抄下来：

> **Tools enforce presence, not quality.**（工具只能强制"存在"，不能强制"质量"。）

### ⑦ 事实溯源：`Fact ID` / `Data class: scenario` / **情景数据必须可见标注**

> resolve each §IX `Fact ID` from `sources/*.facts.json` and **keep the value unchanged**.
> Render a compact source footnote (name + short URL/domain) …
> For `Data class: scenario`, place a **visible localized `Scenario data` / `情景数据` label** beside the KPI/chart
> and say in notes that it is illustrative. **Never attach a fact ID to scenario data or let an
> unlabeled invented KPI look factual.**
> An organizing framework, grouping, or label the source does not state is the deck's reading:
> **say so on the page** (e.g. `整理` / `our reading`) or in enabled notes, **never presented as the source's structure.**

**对荣珂的意义**：荣珂是科研 agent，**编造数据是头号罪**。这条给了两个可直接搬的构件：
- **`Data class: scenario` 标记** ＋ 页面上**可见的"情景数据"字样**（不是只在备注里说）。
- **"源材料没说的分组/框架 = 本 deck 的解读，必须在页面上标注"** ——
  这条比"不许编数据"更细一层：**编数据之外，"我把作者的结论重新归了类"也是一种失真，也必须标。**

---

## 2. 对荣珂的精确落地方案

> **施工前置（项目纪律，不许跳）**：先 `snapshot.py save` 目标文件；
> 改完 `py_compile` **3.12 与 3.13 各一遍**；回跑 `regress.py` 并现测计数；
> **动过 `rongke/` 里任何 `.py` ⇒ 重启控制台**（否则 `ui_shot` 0.4 秒 rc=2 拒拍）。

### ⚠️ 2.0 现状校正（本节原结论已部分作废，必须看这段）

**本 Part 起草时读的是 15:00 前后的代码快照。写完后才发现：并行的另一条线已经读过 Part 1，
并把建议落地了** —— `rongke/deck.py` 的论证层注释里**直接引用了 `docs/ppt-skill-survey-20260921.md` §3
那张"判据铺到哪一层"的表**。所以本节改成"现状 + 剩余缺口"，不再假装它们是待办。

| Part 1/2 原建议 | 实况（已现场核实） | 判定 |
|---|---|---|
| P0-1「图表存在 ⇒ 加判据」 | **已实现** `no_visual_elements`，但阈值是 **≥10 页且全零**（不是 6） | ✅ 已做，**阈值比我的更对** |
| P0-3-1「标题是话题标签 ⇒ WARN」 | **已实现** `titles_are_noun_phrases`（短 ＋ 无标点 ＋ 无判断词 ≥70%） | ✅ 已做，且**刻意不做中文分词**，比我的方案更克制 |
| P0-3-2「末页 Thank You / 空白」 | **已实现** `ends_with_thanks` ＋ `last_slide_blank` | ✅ 已做 |
| P0-3-3「无 References 页 ⇒ WARN」 | **被明确否决**，理由：会**在本项目真实产物上天天误报**（组会汇报本来不放参考文献页；实测那份 13 页成片就没有） | ❌ **我的建议是错的** |
| P0-2「文字溢出」 | **未做** | ⏳ 仍待办（§2.2） |

#### ★ 我从这条否决里学到的东西：我的 P0-3-3 违反了荣珂自己的第二问

我写「没有 References 页而含图表 ⇒ WARN」时，**把别人（academic-pptx-skill）的最佳实践直接当成了判据源**，
**而没有先问第二问**：

> **"这条判据的分母是什么？它在*荣珂自己那份正确的稿子*上会不会报错？"**

答案：**会，而且天天会**。⇒ **判据必须长在这类任务的真实形态上，不能长在别人的最佳实践上。**

这与 §3 的第四问合成一条更完整的准入纪律：

> **别人的最佳实践只是"候选判据"，不是"判据"。过第二问（分母）才准入。**

⇒ **本节之后，我只提一条 Part 1/2 都没提过、由这次校正才浮出来的缺口。**

---

### ★★ 2.1 真正剩下的那个洞：`no_visual_elements` 是**整份总量**判据，会被绕过

代码里作者自己留了这句（原文）：

> `no_visual_elements` 只在"≥10 页且图/表/图表**全 0**"时才 WARN（**它有两个图表就绕过去了**）。

形态问题很清楚：它是 **deck 总量**判据（`facts.pictures + charts + tables == 0`）。
⇒ **11 页纯文字 ＋ 2 个图表** 完美绕过 —— 而那份被吐槽的 13 页组会 PPT 恰恰是这个形状。

---

#### ⛔ 撤回：我上一版提的"逐页含数字无图 ⇒ WARN"**已被实测证否，不要实现它**

> **撤回声明（2026-09-21，本节初版作废）**
> 初版补丁：「该页正文含 `\d` 且该页 `visuals == 0` ⇒ WARN，code=`page_has_numbers_but_no_visual`」。
> **这条判据是错的。** 证否它的是一场可复算的语料实测 —— 脚本：
> `evidence/ppt-skill-survey-20260921/measure_page_visuals.py`
> （扫到 1688 个 pptx；读得动 1550；读不动 138 记作**没查**、不进任何分母）。

**三种候选形态，全部在语料上量了打红面：**

| 版本 | 判据形态 | 打红面 | 判定 |
|---|---|---|---|
| **v1**（我的提案） | 逐页：含数字且无视觉元素 ⇒ WARN | **1544 / 1550 = 99.6%** | **噪音器 ⇒ 证否** |
| v2 | 整份：无视觉页占比 ≥ 85%（且 ≥8 页） | 184 / 1550 = 11.9% | **误报**（打红内核自己的示例） |
| v3 | 长正文页（>120 字）无视觉占比 ≥ 80% | **0 / 1550** | **零触发 🔴 装饰品** |

**v1 为什么 99.6%？** 因为**"纯文字页"在这个项目里就是常态，不是异常**。
真实产物逐份摊开（去重后只有 **5 种形态**，n=5 ⇒ 结论是**方向性**的，不是统计性的）：

| 真实产物 | 页数 | 视觉元素 | 无视觉页 |
|---|---|---|---|
| `runs/20260921-124950/deck_juanqing.pptx`（被吐槽的那份 13 页组会） | 13 | 2 | **11/13 = 85%** |
| `runs/_style_probe/ugly.pptx` / `probe.pptx` / `raw_ugly.pptx`（同内容变体） | 13 | 2 | 85% |
| `runs/_regress/_probe_notes/deck.pptx` | 13 | 3 | 10/13 = 77% |
| `runs/20260921-121201/deck.pptx`（同上变体） | 13 | 3 | 77% |
| `runs/_deck_demo/组会汇报_示例.pptx`（**内核自己的回归示例**） | 8 | **0** | **8/8 = 100%** |
| `runs/_regress/_img_keep/with_image.pptx` | 5 | 1 | 4/5 = 80% |
| `runs/_regress/deck_e2e/group_meeting.pptx` | 3 | 0 | 3/3 = 100% |

封面页写 `2026-09-21`、目录页写 `1 背景 2 方法`、结论页写 `准确率 92%` ——
**都含数字、都没图、全都正确**。v1 不是在抓缺陷，它是在抓**这类任务的常态**。

**v2 为什么也错**：它唯一打中的真实产物是 `组会汇报_示例.pptx` ——
那是 `runs/_regress/deck_demo.py` 生成的**回归示例**，**它的存在意义就是"一个正确用例"**。
⇒ 打红它 = 误报坐实。

**v3 为什么更糟**：零触发。**一条永远不报的检查比没有这条检查更坏**（G 型）——
它会在报告里冒充"这一项查过了"。

---

#### ★ 三条为什么全死：**判据拦错了东西**

`rongke/deck.py` 1748–1751 行自己写着这条规矩：

> 判据必须长在**这类任务的真实形态**上，而不是长在别人的最佳实践上。

实测给出的"真实形态"是：**本项目的组会/学术汇报，天然就是 77%–100% 的纯文字页。**
⇒ 「你没配图」在这个形态上**不是异常，是常态**。
⇒ 任何以「缺图」为信号的判据：**灵敏到能触发就必然误报，不误报就永远不触发**。
v1 撞在前一头，v3 撞在后一头，v2 卡在中间 —— 三个位置都试过了，**这条路是死的**。

有一条更锐利的说法值得单收（**设计定律 11 的候选**）：

> **判据该拦「沉默」，不该拦「选择」。**

「封面页不放图」是一个**选择**；「这份 deck 全程没想过配图」才是**沉默**。
v1 分不开这两者 —— 它把选择当沉默打，于是 99.6%。

---

#### ★★ 顺带的实测发现：`no_visual_elements` 在真实产物上触发率 **0 / 10**

```
放过  with_image.pptx                5 页 / 视觉 1 个
放过  deck_juanqing.pptx            13 页 / 视觉 2 个   ← 就是它没拦住的那份
放过  ugly.pptx                     13 页 / 视觉 2 个
放过  …（共 10 份，无一次触发）
```

它**从未在这类任务的真实形态上开过火**（只在合成夹具上开火）。而且卡在一个**刀尖**上：
内核自己的示例是 **8 页、全文字、0 视觉元素**，**恰好差 2 页踩在阈值（10）下面**，被放过。

⇒ **但这不是"阈值调错了"，不要把它改成 8。** 改成 8 就会打红那份
**故意做简单的回归示例**（`deck_demo.py` 生成，它被当作正确用例存在）。
⇒ 这个现象说明的是**判据的选型不对，不是它的松紧不对**。
一句话：`no_visual_elements` 与"逐页判据"**不是分工关系** —— 它们是**同一个错误假设的两条支路**。

#### ★ 正确的落点：把「缺图」从**判据**降级为**声明**

既然"缺图"在本形态上是**常态**，那它就不该被当成**错误信号**去拦；
它该被当成**一个必须被回答的问题**去问 —— 这正是 PPT Master 的
"Absence needs a reason" 的**正确用法**（§1① 那条机制本身没变，**用法变了**）：

```
不是：  你的 deck 没图      ⇒ WARN「你错了」
而是：  这份 deck 的配图策略，声明过吗？
          · 声明过（如「本次是概念讲解，不配图」）⇒ 按声明放行，并把声明写进报告
          · 没声明                                ⇒ SKIP
```

关键在最后一行是 **SKIP 而不是 WARN**：

- **SKIP 的语义是「没查」，不是「不合格」** —— 与本项目已有的 M 型纪律同源
  （`font_unmeasurable` / `aspect_unmeasurable` 都是 SKIP）。
- 它顺手补掉一个**当前真实存在的报告盲区**：现在 `no_visual_elements` 不触发时，
  报告里**一个字都没有**。读者分不清「查了、合格」与「**压根没查**」。
  ⇒ 改成声明制之后，**报告里总有一行**。这是 G 型（"没查"被读成"通过"）的正解。

**为什么这条能过第二问（分母考）**：它的分母**不是**"所有 deck"，
而是"**没声明配图策略**的 deck"。一份正常的组会汇报**只要声明一句**就永远不被扰 ——
⇒ 漏报是可能的（模型可能懒得声明），但**误报面被压到接近零**。
这与 v1 恰好相反（v1 是误报面 99.6%、漏报面 0）。

⚠️ **但这条不该现在实现**：它要新增一个声明入口（跨 `contract.py` 的工具契约
＋ `loop.py` 的配图阶段），属于**改公共模块**，成本远超一条 WARN。
⇒ 记为 **P1**，等 `deck.py` 的门这一轮收口之后再动。

#### P0-1（修订）：`probe_pptx` 的逐页视觉计数 —— **仍然值得加，但理由变了**

原理由（"给逐页判据喂输入"）**已随 v1 一起作废**。保留它的新理由：

1. 它是上面那条**声明制判据**的输入（要问"没声明 ⇒ 该页本来该不该有图"，得先知道有没有图）；
2. 它是任何未来**逐页版式**判据（`figure_render` 用了几次、配图落在哪几页）的输入；
3. **它是纯新增字段、零既有断言面** —— 这一条是**"工具 vs 判据"两条腿的第一条**：
   **先把事实备好，判据可以后长。** 现在报告里连"每页有几张图"这个**事实**都拿不到，
   这本身就是个缺口（`probe_pptx` 逐页循环已在数 `pic`/`tbl`/`chart`，只是没分开记）。

**改动量（精确到行）**：`rongke/deck.py` 逐页循环（约 505–527 行）现在**已经在数**
`pic` / `tbl` / `chart`，但**只累加到 deck 总量**（`f.pictures += 1`），**没有逐页计数**。
⇒ 循环里加 `page_visuals = 0`，在那三个分支里同时 `page_visuals += 1`，
再在约 548 行的逐页 dict 里加 `"visuals": page_visuals`。**只新增一个字段，不动任何既有字段。**

⚠️ **配套测试仍要三条（正控 / 负控 / 边界），但断言的对象变了**：
不是断言"判据出现"，而是断言**这个字段的数字是真的**（第 N 页确实是 1、其余是 0），
外加一条**负控：一份只有 `tbl` 的页，`visuals` 必须 ≥1**
（防止只数 `pic` 就交差 —— 那会让"有表没图"的页被读成"没有视觉元素"，**又一个 G 型**）。

⚠️ **`no_visual_elements` 的阈值（10）一个字都不要动。** 理由见上：它的问题不是松紧，
是选型。**加一条与它同源的新判据 = 把同一个错误再犯一遍。**

### ★★ 2.2 本轮实测新发现（2026-09-21 下午，全部可复算）

这一节不是"方案"，是**在真语料与真工具链上量出来的事实**。每条都给了复算路径。

#### ①⛔ `figure_render` 的 `out` 是**相对工作台**解析的 —— 会静默打崩配图那两步

**已写进单源配方**（`contract.DECK_TOOLCHAIN_RECIPE` 第 4–5 步之间）。

```
机制：figure_render 内部 _run_cli(..., cwd=工作台目录)，
      且 figure_router.py 用 os.path.abspath(out) 解析相对路径
      ⇒ 相对 out 落在**工作台根目录**，不落在调用方的工作区。
实测：out="fig_bare_probe.png"
      → ~\.dsh\papers\workbench\fig_bare_probe.png
更坑：它回的 {"file": …} **只是把 out 原样抄回来**，不是核实过的路径
      ⇒ 它不能告诉你图到底在哪（字段说 ok:true，同名文件却在工作台）。
```

**后果**：配方若按字面写 `out="fig1.png"` 再 `manage_image(image_source="fig1.png")`，
图在工作台根、而 MCP 服务端的 cwd 是工作区 ⇒ **插图必然找不到那张图**，
且错误要等到很后面才浮出来。
**治法**：`figure_render.out` 给**绝对路径**；`manage_image.image_source` 用工作区内相对名。

#### ② 示例脚本是配方的**平行实现**，且已经漂移成"反面教材"

`runs/_regress/deck_demo.py` 停在 `save_presentation`（旧 7 步配方的样子），
不走 `figure_render` / `manage_image` / `set_slide_notes` / `style_deck` / `audit_deck`。
而它是**唯一一份人能打开**的产物 ⇒ 谁打开它，学到的就是"这条链路产 4:3 默认丑底"。

| | 配图 | 讲稿 | 主题 | 体检判定 |
|---|---|---|---|---|
| 修前 | **0** | 0/8（python-pptx 直写） | 无（4:3 默认） | **FIX BEFORE DELIVERY**　FAIL: `aspect_not_16_9` `look_is_default` |
| 修后 | **2** | **8/8 = 100%** | juanqing（16:9） | **PASS WITH WARNINGS**　五道门全 PASS |

修法不是"照着配方补工具名"，而是**让它真的走一遍**；并且加了三条纪律：
**出图拿不到就非零退出**（禁止静默降级）、**payload 必须原文照印**（不许只报"不在盘上"）、
**版式逐页分派**（8 页全 `Title and Content` 会被 `layout_monotone` 正确点名为"Word 贴上来的"）。

#### ③ `deck_test.py` §21 现有 **4 条红**（`21.8–21.11`），**与本报告方案无关**

```
日志原文：合计 273 项：269 通过，4 失败     （runs/_regress/deck.log）
✘ 21.8  套完主题后骨架对称了      — ok=False 比值=[7.67, 7.67]
✘ 21.9  且判据随之消失            — WARN 里 placeholder_geometry_stale 还在
✘ 21.10 `changed` 里如实写了骨架被重排 — []
✘ 21.11 幂等：再套一次 ⇒ 骨架一字不变 — ok=False
```

**形态**：`style_deck` 把画布从 4:3 改成 16:9，**却没有把母版骨架一起重排**
⇒ 新判据 `placeholder_geometry_stale` 响；修复路径（重排骨架）返回 `ok=False`、`changed=[]`。
**非归属证明**（不是"我认为"）：`DECK_TOOLCHAIN_RECIPE` 的引用者只有
`contract.py` 与 `loop.py`（`deck.py` 从不读它，已用 grep 核过导入图），
而 `21.8–21.11` 的输入是 `D.style_deck(...)` 与 `probe_pptx(...).master_geo` ——
**断言函数的输入面**里没有配方。⇒ 本节所有改动与这 4 条红无因果关系。

#### ④ 🪤 `regress.py` 把「失败项」汇总块**重复计为断言** ⇒ 失败数虚高一倍

同一次运行，`deck_test.py` 自报 `4 失败`，`regress.py` 报 `8 失败`。
原因：失败会**打印两次**（节内一次、末尾「失败项」汇总一次），
而 `regress.py` 的解析器把行首（允许缩进）的 `✘` 一律计数
⇒ 4 → 8。基线只记 `✔` 数，所以**基线没被污染，被人读的报告被污染了**。
⚠️ 这条会让人**以为改动坏了更多东西**，值得单独修（`regress.py` 已有
「汇总行不算断言」的用例，但它只覆盖 `全部通过 ✔` 那种形状，没覆盖逐条汇总）。

#### ⑤ 基线尚未收录 §21

`runs/_regress/baseline.json`（15:23:44）`deck = 260`；现测通过数 **269**，差 **9**。
**9 恰好等于 §21 里「通过」的那 9 条**（`21.1–21.7`、`21.12–21.13`）
⇒ 与"基线是在 §21 落地之前采的"**算术上一致**。
（⚠️ 这是**算术吻合**，不是逐条 diff —— `git diff` 里 §20 与 §21 的改动混在一起，
未逐条拆开核对；如需坐实，应把 `deck_test.py` 回到 pre-§21 版本数一次通过数。）
⇒ 结论仍然成立：那 4 条红**不在任何基线比较的视野里**，`regress.py` 现在**看不见 §21 的失败**。
⇒ 处置：等 §21 修绿之后 `regress.py --save`，**不要现在就覆盖**（会把红一起"洗白"）。

### P0-2 文字溢出判据：**用 PPT Master 的估算口径 + CyberPPT 的容差**

```
① 每角色字宽率表（校准一次，落在 deck.py，与 DECK_THEMES 同为单源）
   cjk_rate / latin_rate / caps_rate / digits_rate  ← 每 100px 可容纳字符数
② 估算：(cjk字数 ÷ cjk_rate + 其他字数 ÷ latin_rate) × 100，混合行两段相加
③ 安全余量：估算宽 × 1.05 与容器实测宽比较（5% 是估算 vs 真实字形的缓冲）
④ 容差（借用 CyberPPT）：P0 默认 3px / 最大 6px；P1 4/8；P2 6/12
```

**关键纪律（照抄原文，写进工具描述）**：
- **"Never trim wording to satisfy an estimate."** —— 不许为了迁就估算去删词。
- **修复优先级**：扩框（先用没用上的空间）→ 改版式（整段 → 分点）→ **最后**才删限定语。
- **"larger bounds never repair off-canvas text."** —— 把框改大到画布外不算修好。

### P0-3 末页 / 引用（来自 academic-pptx-skill，规则非自编）

| 判据 | 触发 | 级别 |
|---|---|---|
| 末页是 "Thank You" / "谢谢" / 空白 | 学术类 deck | WARN |
| 全 deck 含图表但零 "References / 参考文献" | 所有 deck | WARN |

两条都能**只读 XML 文本**判出（`_shape_text()` 已有现成读法）。

### P1 结构化改进（对应 Part 1 §4，这里给精确落点）

| 项 | 落点 | 形态 |
|---|---|---|
| **禁止项必须是用户原话** | `contract.py` 的 `TaskContract.forbidden` | 每条标 `(user)` 或 `(baseline)`；**禁止由系统"投影"出禁令** |
| **`(binding)` 字段语义** | `TaskContract` 的 `deliverables/acceptance/constraints` | 默认"可自由调整"，加绑定标记才锁死 |
| **早期门** | `DeckAuditor.audit(..., stage="early")` | 前 5 页就触发；**只报方法级**（同类同向 ≥2） |
| **合并修复纪律** | 工具描述 ＋ 纪律块 | "跑一次全量 → 一次改完 → 验一次；**不许在两次修复之间跑检查**" |
| **禁止静默降级** | 纪律块 | 已在 `style_deck` 实践，落成文字 |

### ⛔ 不改的

**`DECK_TOOLCHAIN_RECIPE` 不动。** Part 1 建议过"配方前加一步论证先行"，
但读完 `strategist.md` 后修正判断：**PPT Master 是先产 spec 文件再画**，那是因为它的执行器要读那份文件；
**荣珂的配方是给同一个模型看的开工顺序**，多插一步会稀释它（而且第 6 条技能门要求技能先装载，
加在生产动作之前的每一步都是"诱导跳门"的风险）。**论证层要补的是判据，不是配方步骤。**
⇒ 论证层补法：P0-3 那两条（末页/引用）＋ 一条 P1 的 WARN（**页标题若是纯话题标签**、短且不含动词短语），
判据用"标题长度 + 是否含数字/结论词"做粗筛，**宁可漏报不误报**（设计定律 10）。

---

## 3. 三问之后，第四问

荣珂已有的三问：**给了工具吗？给了判据吗？这条判据的分母是什么？**

读完 PPT Master 的缺口处理机制后，补上第四问：

> ### ★★★ 第四问：「这件事"没做"的时候，谁负责说出口？」
>
> `pictures=0` 的死结不是"没工具"也不是"没判据"——是**没人负责说明"为什么没有"**。
> **"缺席需要理由，而理由必须写在产物里"** —— 这条把"沉默的零"变成了"必须记账的零"。
>
> ⇒ **沉默不是中性，沉默是一种需要被记账的行为。**
>
> ⚠️ 但它是**"不接受沉默"**，不是"强制动作"：`Choosing not to use a device is valid;
> only an unstated reason is not.`
> —— **这正是它能免于误报的原因，也是它比"必须有图"高级的地方。**

**推论（可直接当设计定律 11 的候选）**：
**判据应当拦"沉默"，而不是拦"选择"。** 拦选择的判据必须猜测姿势对不对（会误报、会逼出垃圾动作）；
拦沉默的判据只要求一句话（不误报，且那句话本身就是审计线索）。

---

## 4. 仍未拿到（如实留档）

1. `semantic-svg.md`（5.6KB）、`artifact-ownership.md`（20KB）、`technical-design.md`（136KB）、
   `why-ppt-master.md`（13KB）—— **已全部下载，未细读**。
   **优先级判断**：`semantic-svg.md` 是 SVG↔DrawingML 的语义边界，与荣珂 python-pptx 链路**不同源** ⇒
   **可永久降级**；`technical-design.md` / `why-ppt-master.md` 讲"为什么用 SVG"、产品定位 ⇒ **永久降级**；
   `artifact-ownership.md` 讲"**谁能写哪个产物**"（与荣珂"归属层修复"直接同题）⇒ **中等价值，值得补**。
2. **四份技能一份都没真跑过图** —— 全部结论都是**文档与结构层**的。
   特别是 §2.2 的字宽率：**PPT Master 的率值是它自己（中英混排）的样本均值，
   直接搬到荣珂必须重新校准**，不能照抄 —— 字体不同，率就不同。
3. PPT Master 的 `preset_shape_vocabulary.md`（187 个 Office preset）未看：
   荣珂不走这条路线（用 python-pptx 原生形状），**永久无关**。
4. ⚠️ **本节（§2）的最大教训不是"漏拿了什么"，而是"我在起草时读的是旧快照"** ——
   见 §2.0。**调研别人的东西时，自己的仓库也在动。**

---

## 5. 一条元教训：**报告本身也要过第二问**

Part 1 的 P0-3-3 是在**没有核对本仓库真实产物形态**的情况下给出的，本仓库随后的实践把它否决了，
而且否决理由正是荣珂自己的第二问（分母）。

⇒ **把外部最佳实践抄成自家判据之前，必须先在自家真实产物上跑一遍分母检验。**
这条不只适用于写判据，也适用于**写调研报告的建议清单** ——
**建议的分母，就是"我建议的对象此刻的真实状态"。**

---

*证据：`evidence/ppt-skill-survey-20260921/`（Part 1 六份 + Part 2 七份 + 抓取脚本 `_fetch_pm.py`，共约 291KB）*
