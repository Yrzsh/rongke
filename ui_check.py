# -*- coding: utf-8 -*-
"""Web 控制台的静态一致性检查。

## 为什么需要它

`index.html` 是一整块 66KB 的 HTML+CSS+JS，**没有构建步骤、没有前端测试**。
它最危险的失效方式全部是**静默**的：

  * JS 里 `$("#histFilt")` 而 HTML 里 id 拼成了 `histFilter` → 运行到那行才报错，
    而它可能只在你点"历史"时才执行 —— 于是"平时看着好好的"
  * `onclick="loadHist()"` 里的函数被改名 → 点了没反应，控制台一句红字都没有
  * CSS 里 `var(--ok-fg)` 而变量定义时手滑写成 `--ok-fg2` → **样式静默失效**，
    元素退化成继承色。暗色模式下就是一块白斑
  * 语法错误（少个括号）→ 整页白屏

这些都不能靠"读一遍"发现。所以把它们变成可执行的断言。

## 检查项

  1. **JS 语法**：把每个 `<script>` 抽出来交给 `node --check` 真解析
  2. **DOM id**：JS 里 `$("#x")` / `getElementById("x")` 的每个 x 都要在 HTML 里存在
  3. **id 唯一**：重复 id 会让 `$()` 只拿到第一个，是隐蔽的错绑
  4. **全局函数**：HTML 里 `onclick="fn(…)"` 引用的 fn 必须在 JS 里有定义
  5. **CSS 变量**：每个 `var(--x)` 都要有 `--x:` 的定义（含 `[data-theme="dark"]`）
  6. **视图路由**：每个 `data-v="x"` 都要有对应的 `id="v-x"` 区块
  7. **体检卡渲染**：把 `lintCard` 的**真实源码**抽出来交给 node 执行，喂合成 metrics ——
     静态检查看不出「内核加了指标、界面没显示」（缺陷形 D：检查存在但不覆盖）。
     抽不到源码就 fail-loud，**绝不静默跳过**。

用法：python ui_check.py            # 检查 rongke/web/index.html
      python ui_check.py --file x   # 检查指定文件
退出码：0 全过 / 1 有问题 / 2 文件或 node 不可用
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
import _tkit as T                                             # noqa: E402

DEFAULT = ROOT / "rongke" / "web" / "index.html"

RE_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)
RE_STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
RE_ID_DEF = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")
# 只收**静态** id 引用：闭合引号后必须紧跟 `)`。
# 否则 `$("#v-" + v)` 这种拼接会被误判成"引用了 id=v-"（首轮实测踩到，
# 检查器自己报了假警 —— 会报假警的检查器比没有检查器更糟，很快就被无视）。
RE_ID_USE = re.compile(
    r"""\$\(\s*["']#([A-Za-z0-9_\-]+)["']\s*\)"""
    r"""|getElementById\(\s*["']([^"']+)["']\s*\)""")
# 动态拼接的选择器：单独统计，只作提示，不作断言
RE_ID_DYN = re.compile(r"""\$\(\s*["']#([A-Za-z0-9_\-]*)["']\s*\+""")
RE_ONCLICK = re.compile(r"""on(?:click|input|change)\s*=\s*["']\s*([A-Za-z_$][\w$]*)\s*\(""")
RE_FUNC_DEF = re.compile(r"""(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|(?:^|\n)\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function)""")
RE_VAR_USE = re.compile(r"var\(\s*(--[A-Za-z0-9_\-]+)")
RE_VAR_DEF = re.compile(r"(--[A-Za-z0-9_\-]+)\s*:")
RE_DATAV = re.compile(r"""data-v\s*=\s*["']([A-Za-z0-9_\-]+)["']""")
RE_VSECTION = re.compile(r"""id\s*=\s*["']v-([A-Za-z0-9_\-]+)["']""")
# W3：侧栏"收起后真的变窄"这条 CSS 规则。允许选择器带 class 前缀
# （`.side[data-fold="1"]`），但**必须真的存在一条带宽度的收窄规则** ——
# 只查 `data-fold` 字样是不够的：状态翻了而 CSS 没动，界面看着毫无变化。
RE_FOLD_CSS = re.compile(r"""\[data-fold\s*=\s*["']1["']\][^{]*\{[^}]*width""")

# ── 体检卡渲染探针（交给 node 执行；用 argv[2] 拿 index.html 路径）──
# 为什么要**抽真实源码**而不是在探针里重写一份 lintCard：
# 重写的那份会随着界面改动而漂移，于是这个检查测的是探针自己，
# 恒为通过 —— 就是纪律里那个「检查自我闭环」的事故。抽不到必须退出码 2。
PROBE_JS = r"""
const fs = require("fs");
const file = process.argv[2];
const src = fs.readFileSync(file, "utf8");


function sliceFrom(mark) {                    // 花括号配平，取出整段函数
  const i = src.indexOf(mark);
  if (i < 0) throw new Error("找不到 " + mark);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") { depth++; started = true; }
    else if (src[j] === "}") { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error("括号不平衡: " + mark);
}

let fn;
try {
  const cd = sliceFrom("function citeDisorder(m) {");
  const lc = sliceFrom("function lintCard(lint, open) {");
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  fn = new Function("esc", cd + "\n" + lc + "\nreturn {citeDisorder, lintCard};")(esc);
} catch (e) {
  console.error(e.message);
  process.exit(2);                            // fail-loud：抽不到就别报"通过"
}

let fail = 0;
function check(label, got, want) {
  const okv = got === want;
  if (!okv) fail++;
  console.log(`  [${okv ? "PASS" : "FAIL"}] ${label}` + (okv ? "" : `  期望 ${want} 实际 ${got}`));
}

// ① 缺数据时必须沉默 —— 宁可不出数，不出假数
let h = fn.lintCard({ metrics: { chars: 100, paragraphs: 2, sentences: 3,
  avg_sentence: 33, max_sentence: 40 }, issues: [], score: 100 }, false);
check("无文献表 → 不显示剔除口径", h.includes("剔文献表"), false);
check("无图表 → 不显示图/表计数", /图 \d+ 张|表 \d+ 个/.test(h), false);

// ② 有数据时必须渲染（新增指标一个都不许漏）
h = fn.lintCard({ metrics: { chars: 100, paragraphs: 2, sentences: 3, avg_sentence: 33,
  max_sentence: 40, has_refs: 1, chars_excl_refs: 72, figures: 3, tables: 2,
  placeholders: 2, jargon_hits: 1, tail_repeat: 3, absolutes: 4, max_en_words: 41,
  citations: 5, cite_first_order: [1, 3, 2], cliche_hits: 6 }, issues: [], score: 88 }, false);
const pills = [["字数口径", "正文 72 字（剔文献表）"], ["图", "图 3 张"], ["表", "表 2 个"],
  ["占位符", "占位符 2 处"], ["行话", "行话 1 处"], ["同尾", "同尾 3 段"],
  ["绝对化", "绝对化 4 处"], ["英文句长", "英文最长 41 词"],
  ["引注首现序", "引注未按首现序"], ["套话", "套话 6 处"]];
for (const [label, needle] of pills) check("渲染 " + label, h.includes(needle), true);

// ③ citeDisorder 的边界
check("升序 [1,2,3] 不报", fn.citeDisorder({ cite_first_order: [1, 2, 3] }), false);
check("逆序 [1,3,2] 报", fn.citeDisorder({ cite_first_order: [1, 3, 2] }), true);
check("跳号 [1,2,5] 不报", fn.citeDisorder({ cite_first_order: [1, 2, 5] }), false);
check("缺字段不报", fn.citeDisorder({}), false);

// ④ 问题逐条渲染 + 展开态
h = fn.lintCard({ metrics: {}, issues: [
  { level: "hard", msg: "残留 TODO", fix: "删掉" },
  { level: "soft", msg: "行话", fix: "改写" }], score: 40 }, true);
check("必修标签", h.includes("必修 · "), true);
check("建议标签", h.includes("建议 · "), true);
check("展开态标记", h.includes('data-open="1"'), true);

process.exit(fail ? 1 : 0);
"""

# ── 稿件台探针（同样抽真实源码；抽不到退 2）──
# 这一探针存在的理由，是一条**实测**出来的形状：归档里的稿是"整段无换行"的连续中文
# （最长 176 字 / 1 行 / 无 # 标题）。只认 markdown 的话，大纲会是一个空壳 ——
# 而空壳大纲比没有大纲更糟：它承诺了导航却什么都不给。
# 所以这里最要紧的一条断言不是"能渲染"，而是**"单行长稿必须被切成多块"**：
# 谁把句群降级删了，这条立刻红。
PROBE_DESK_JS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");

/* esc 是 `const esc = s => …` 一行式：sliceFrom 靠大括号配平，对它无效
   （会一路吞到下一个函数体的大括号）。按「到第一个分号」切，切出来的就是真 esc。 */
function sliceToSemi(mark) {
  const i = src.indexOf(mark);
  if (i < 0) throw new Error("找不到 " + mark);
  /* 切到「行尾分号」，不是第一个分号 —— esc 里写着 "&amp;" 这种 HTML 实体，
     那个分号在行中间，按第一个分号切会切进字符串里，拼出来是坏 JS。 */
  const rest = src.slice(i);
  /* 切到「行尾分号」，不是第一个分号 —— esc 里写着 "&amp;" 这种 HTML 实体，
     那个分号在行中间，按第一个分号切会切进字符串里，拼出来是坏 JS。
     本探针写在 r-string 里，一律避开反斜杠转义：换行用 fromCharCode(10)。 */
  const tail = src.slice(i).split(String.fromCharCode(10));
  let n = 0;
  while (n < tail.length && !tail[n].trimEnd().endsWith(";")) n++;
  if (n >= tail.length) throw new Error("没有行尾分号: " + mark);
  return tail.slice(0, n + 1).join(String.fromCharCode(10));
}

function sliceFrom(mark) {
  const i = src.indexOf(mark);
  if (i < 0) throw new Error("找不到 " + mark);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") { depth++; started = true; }
    else if (src[j] === "}") { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error("括号不平衡: " + mark);
}

let mk;
try {
  // 依赖（fetch / openDesk / fmtSize）从参数注入 —— 这样"读产出物"这条异步路径
  // 也能在 node 里跑，且**不会碰真网络**。抽不到照样退 2。
  const MARKS = ["function deskTocOf(line) {", "function deskSentences(t) {",
    "function deskChunks(t, per) {", "function deskBlocks(text) {",
    "function deskRefs(text) {", "function deskHi(html, kw) {",
    // deskCite 是正文引注角标（[n] → sup.dcite，点了跳参考文献）的**纯函数部分**；
    // 它与 deskHi 同一条「标签外替换」纪律，判据落在切片里才测得到。
    "function deskCite(html) {",
    // 期刊解剖学判据一族（_desk_ruler.py 拿真期刊论文对拍量出来的那一层）：
    // deskPClass 调 deskIsAbs/Kw/Ref/Cap，deskHead 调 mdInline + esc ——
    // 切片按调用链全量登记，缺一个就是沙箱里 ReferenceError（W26B 那条纪律）。
    "function deskIsAbs(s) {", "function deskIsKw(s) {", "function deskIsRef(s) {",
    "function deskIsCap(s) {", "function deskIsTcap(s) {", "function deskFrontier(blocks) {",
    "function deskPClass(s, i, frontier) {", "function deskHead(t) {",
    "function deskLead(html, kind) {", "function deskLatin(t) {",
    "function deskFrontKind(s) {", "function deskMark(html) {",
    "function mdInline(s) {",
    // deskFigPick 是图注→资产的**严格**匹配器（文件名自己承认是图 N 才认）；
    // deskPos 一族是续读位置记忆（比例存取 + 20 条挤旧）。都是纯函数，切片可测 ——
    // 它们互相调用（deskPosOf→deskPosLoad→deskStore），少切一个就是 ReferenceError。
    "function deskFigPick(n, files, kind) {",
    "function deskStore() {", "function deskPosKey() {", "function deskPosCap() {",
    "function deskPosLoad() {", "function deskPosSave(rid, ratio) {",
    "function deskPosOf(rid) {", "function deskPosPrune(all, cap) {",
    // deskTitleOf 是 deskPayload 用来定标题的（"稿子叫什么"）；它**调 deskTocOf**，
    // 所以两个都得在切片里 —— 少一个就是 ReferenceError，不是"通过"。
    "function deskTitleOf(text, r) {",
    "function deskPayload(r) {", "async function deskReadFile(runId, f) {",
    "function canReplay(r) {", "function deskMissText(rid, raw) {",
    // deskRunWhen 是 deskLoadRun 用来把运行号读成时刻的（"我读的是哪一次"）；
    // 它**必须**跟着切片，否则沙箱里是 ReferenceError、deskLoadRun 静默走 catch，
    // 表现成"读不出正文"（假红）—— 切一片函数就得把它调的也算上。
    "function deskRunWhen(rid) {",
    "async function deskLoadRun(rid) {"];
  const body = sliceToSemi("const esc = s => String(s == null") + "\n"
    + MARKS.map(sliceFrom).join("\n");
  const RET = "\nreturn {deskTocOf,deskSentences,deskChunks,deskBlocks,deskRefs,deskHi,"
            + "deskCite,deskFigPick,deskStore,deskPosKey,deskPosCap,deskPosLoad,"
            + "deskPosSave,deskPosOf,deskPosPrune,"
            + "deskIsAbs,deskIsKw,deskIsRef,deskIsCap,deskIsTcap,deskFrontier,deskPClass,deskHead,"
            + "deskLead,deskLatin,mdInline,deskFrontKind,deskMark,"
            + "deskTitleOf,deskPayload,deskReadFile,canReplay,deskMissText,"
            + "deskRunWhen,deskLoadRun};";
  mk = (deps) => {
    deps = deps || {};
    const NO = () => {};
    // paintArts 是 deskLoadRun 末尾的选中态回写（成品行跟随稿件台）—— 沙箱里给空桩
    const paintArts = deps.paintArts || NO;
    const NOFETCH = async () => { throw new Error("本段不该联网"); };
    return new Function("CUR", "fetch", "openDesk", "fmtSize", "api", "paintArts", body + RET)(
      { goal: "" }, deps.fetch || NOFETCH, deps.openDesk || NO,
      deps.fmtSize || (n => n + "B"), deps.api || NOFETCH, paintArts);
  };
} catch (e) {
  console.error(e.message);
  process.exit(2);            // fail-loud：抽不到就别报"通过"
}

let fail = 0;
function check(label, got, want) {
  const okv = got === want;
  if (!okv) fail++;
  console.log(`  [${okv ? "PASS" : "FAIL"}] ${label}` + (okv ? "" : `  期望 ${JSON.stringify(want)} 实际 ${JSON.stringify(got)}`));
}
const fn = mk();

// ① 真实稿形：单行、无换行、无标题标记、>140 字 —— 必须降级成多块
const ONE = "低温诱导下冬虫夏草菌丝体中虫草素含量显著上升。".repeat(8);
check("夹具本身是单行（这才测到了降级）", ONE.indexOf("\n"), -1);
check("夹具本身超过 140 字", ONE.length > 140, true);
const b1 = fn.deskBlocks(ONE);
check("单行长稿被切成多块（句群降级生效）", b1.length > 1, true);
check("每块不超过 4 句（切块参数没被悄悄改掉）", fn.deskSentences(b1[0].t).length <= 4, true);
const tocN1 = b1.some(b => b.k === "h") ? b1.filter(b => b.k === "h").length : b1.length;
check("大纲不会是空壳（块导航兜底）", tocN1 >= 1, true);
check("句群切分拼回去等于原文（没丢字）", b1.map(b => b.t).join(""), ONE);

// ② 有 markdown 标题的稿：认出来，且不再走块导航
const MDT = "# 总论\n\n## 方法\n\n正文一。\n\n## 结论\n\n正文二。";
const b2 = fn.deskBlocks(MDT);
check("三个标题都被认出来", b2.filter(b => b.k === "h").length, 3);
check("有标题 ⇒ hasH 为真（改用大纲导航）", b2.some(b => b.k === "h"), true);

// ③ 标题识别的各种长相（含两条**边界**：短的真、长的假）
check("【】小标题 lv=1", (fn.deskTocOf("【研究方法】") || {}).lv, 1);
check("第X章 认", !!fn.deskTocOf("第三章 材料与方法"), true);
check("一、认", !!fn.deskTocOf("一、总论"), true);
check("1.2.3 分节 lv=3", (fn.deskTocOf("1.2.3 数据处理") || {}).lv, 3);
// 真期刊论文的编号有三格深（1. / 2.1 / 2.3.1），加上题目就是四个 rank。
// 上一版把 lv 封顶在 3，#### 与 ### 撞成同一档，2.3.1 这类小节在预览里塌回上一级。
check("#### 四级标题 lv=4", (fn.deskTocOf("#### 2.3.1 基因组分析") || {}).lv, 4);
check("##### 封顶在 lv=4", (fn.deskTocOf("##### 更深的编号") || {}).lv, 4);
check("### 三级仍是 lv=3", (fn.deskTocOf("### 2.1 材料") || {}).lv, 3);
check("1.2.3.4 四级编号 lv=4", (fn.deskTocOf("1.2.3.4 更深的分节") || {}).lv, 4);
check("1.2 仍是 lv=2（旧语义不许动）", (fn.deskTocOf("1.2 数据处理") || {}).lv, 2);
check("普通正文不算标题", fn.deskTocOf("这一节讨论了低温诱导对虫草素的影响。"), null);
// 这条是**实测逼出来的**：把「1.2.3 数据处理」和「2021 年的实验显示…」分开，
// 靠的是"数字编号分两式写"。谁把两式合回一个宽松正则，这里立刻红。
check("数字开头的正文不算标题（年份）", fn.deskTocOf("2021 年的实验显示，低温诱导组的含量显著高于对照组。"), null);
check("短编号行算标题", !!fn.deskTocOf("1. 材料"), true);
check("超过 50 字的编号行不算标题", fn.deskTocOf("1. " + "很长的说明文字".repeat(9)), null);

// ④ 出处：只认正文里真有的；doi.org 链接与裸 DOI 合并成一条
const REFT = "见 DOI 10.1038/s41586-021-03819-2。另见 https://doi.org/10.1000/xyz123，"
           + "重复引用 10.1038/s41586-021-03819-2。";
const rf = fn.deskRefs(REFT);
check("doi.org 链接与裸 DOI 合并（不列两遍）", rf.length, 2);
check("DOI 末尾标点被剥掉", rf[0].t.indexOf("。"), -1);
check("裸 DOI 补成可点的 doi.org 链接", /^https:\/\/doi\.org\//.test(rf[0].href), true);
check("纯正文不凭空造出处", fn.deskRefs("这一节讨论了低温诱导对虫草素的影响。").length, 0);

// ⑤ 高亮不许改坏 HTML（标签名/属性里也有 "p" 这种关键字）
const hi = fn.deskHi('<p class="abc">p 值小于 0.05</p>', "p");
check("标签名没被高亮污染", hi.indexOf('<p class="abc">') === 0, true);
check("属性值没被高亮污染", hi.indexOf('class="abc"') > 0, true);
check("正文里的关键字确实被高亮", hi.indexOf("<mark") > 0, true);
check("空关键字原样返回", fn.deskHi("<p>x</p>", ""), "<p>x</p>");

// ⑤′ 结构块：表格/列表/代码必须**整块带换行**交给渲染器（k:"raw"），prose 仍无缝拼接。
// 2026-10-07 实测的缺陷形状：块内所有行曾被 join("") 压成一段 ——
// 表格变一行竖线乱码、无序列表变一根横杠长句、有序列表被 deskTocOf 认成三个标题
// （大纲凭空多三节）。这三条正控就是钉住那三种坏法，谁改回去谁红。
const bT = fn.deskBlocks("见表。\n\n表 1：引物\n\n| 基因 | 序列 |\n| --- | --- |\n| CHS1 | ACG |\n| PKS4 | TGG |\n\n完。");
check("表格整体成块（带换行、| 开头）", bT.some(b => b.k === "raw" && b.t.indexOf("| 基因 |") === 0 && b.t.indexOf("\n") > 0), true);
check("表格 4 行一行不少", (bT.find(b => b.k === "raw") || {}).t.split("\n").length, 4);
check("表格不再压成竖线乱码（|| 连体）", bT.filter(b => b.k === "p" && b.t.indexOf("||") >= 0).length, 0);
const bL = fn.deskBlocks("步骤：\n\n- 备料\n- 低温诱导\n- 测序\n\n完成。");
check("无序列表整体成块（3 行）", bL.some(b => b.k === "raw" && b.t.split("\n").length === 3), true);
check("无序列表不再压成一根横杠长句", bL.filter(b => b.k === "p" && /备料- 低温/.test(b.t)).length, 0);
const bO = fn.deskBlocks("流程：\n\n1. 提取 RNA\n2. 建库测序\n3. 差异分析\n\n结束。");
check("有序列表整体成块（3 行）", bO.some(b => b.k === "raw" && b.t.split("\n").length === 3), true);
check("有序列表不再被认成标题（大纲不污染）", bO.filter(b => b.k === "h").length, 0);
// 与既有判据的边界：**单个**编号行仍按标题走（「1. 材料」是小节，不是列表项）
const bS = fn.deskBlocks("前言一句。\n\n1. 材料\n\n方法见下一段。");
check("单个编号行仍是标题（既有判据不回退）", bS.some(b => b.k === "h"), true);
const bF = fn.deskBlocks("代码如下：\n\n```python\nx = 1\nprint(x)\n```\n\n以上。");
check("代码围栏整体成块（含首尾围栏）", bF.some(b => b.k === "raw" && b.t.startsWith("```python") && b.t.endsWith("```")), true);
check("单块降级不碰结构块（大代码块不许被按句切碎）",
      fn.deskBlocks("```\n" + "x++;".repeat(60) + "\n```").length, 1);
// prose 回归：中文硬换行仍无缝拼接（这条是原有行为的正控，改结构不能伤它）
const bJ = fn.deskBlocks("段一第一行\n段一第二行\n\n段二。");
check("prose 硬换行仍无缝拼接（不插空格）", bJ[0].t, "段一第一行段一第二行");

// ⑤‴ 参考文献**一条一段**：连排的 [n] 行曾被 join("") 拼成一大坨，
// 悬挂缩进只挂第一行、引注锚点（data-ref）也只能锚住第一条 —— 都是这条坏法。
const bR = fn.deskBlocks("正文一句。\n\n[1] 张三. 甲刊, 2020.\n[2] Wang X. Mycology, 2021.\n[3] 刘五. 菌物学报, 2019.\n\n下一段。");
check("参考文献每条一段（不拼成一大坨）", bR.filter(b => b.k === "p" && /^\[\d{1,3}\]/.test(b.t)).length, 3);
const bW = fn.deskBlocks("[1] 张三. 很长的题名，刊物名，2020，\n45(3): 481-490.\n[2] Wang X. 2021.\n\n正文。");
const wRefs = bW.filter(b => b.k === "p" && /^\[\d{1,3}\]/.test(b.t));
check("文献的折页续行接回上一条（不另起一段）", wRefs.length, 2);
check("续行内容真的在第一条文献里", wRefs[0].t.indexOf("481-490") >= 0, true);

// ⑤″ 引注角标：正文 [n] 变可点角标；年份不认；链接残段不认；属性不被污染
check("正文 [n] 包成角标", fn.deskCite("低温显著诱导表达[1]。").indexOf('<sup class="dcite" data-n="1">[1]</sup>') >= 0, true);
check("两/三位序号都认", (fn.deskCite("见[12]与[123]文").match(/data-n="\d+"/g) || []).length, 2);
check("四位不认（[2020] 是年份不是引注）", fn.deskCite("成稿于[2020]年。").indexOf("dcite"), -1);
check("链接残段 [n]( 不认", fn.deskCite("[1](http://x)").indexOf("dcite"), -1);
check("标签属性里的 [1] 不被污染", fn.deskCite('<a title="文献[1]">x</a>'), '<a title="文献[1]">x</a>');
check("无引注原样返回", fn.deskCite("<p>正文没有引注</p>"), "<p>正文没有引注</p>");

// 从 PDF 抽出来的稿子常带 U+0001 这类破码（ACS 的长破折号就落在自定义编码的空位上）：
// 留在页面上是一块豆腐，读者看见的就是坏掉的版式。
const bC = fn.deskBlocks("产量 547 mg/L" + String.fromCharCode(1) + "an impressive 436% improvement");
check("控制字符不许排给读者（PDF 破码 U+0001）", bC[0].t.indexOf(String.fromCharCode(1)), -1);
check("丢掉控制字符不吞正文", bC[0].t.indexOf("436%") > 0, true);
// 期刊稿里的斜体是排印规矩（*物种名* / *P* < 0.05 / *spnA*），不认就是把一串星号留给读者。
// 但作者行的通讯标记「Rang,*」与算式「2 * 3 * 4」都不许被配成一对 —— 两侧贴边才算。
check("*物种名* 渲成斜体", fn.mdInline("如 *S. spinosa* 所示").indexOf("<em>S. spinosa</em>") > 0, true);
check("括号紧跟的开星号也认（*P* < 0.05）", fn.mdInline("差异显著（*P* < 0.05）").indexOf("<em>P</em>") > 0, true);
// 署名区三档（真论文实测作者 13pt / 单位 9pt / 日期 8pt）：四行一样大就是"不像"
check("作者行 = p-front（不带子档）", fn.deskPClass("Ziyuan Xia,§ Jun Li,§ Xirong Liu", 1, 4), " p-front");
check("括号包着的单位块 = p-front p-affil", fn.deskPClass("(Hunan Provincial Key Laboratory for Microbial Molecular Biology, Changsha 410081 Hunan, China)", 2, 4), " p-front p-affil");
check("日期 / 引用行 = p-front p-meta", fn.deskPClass("Received: August 23, 2024 Revised: December 18, 2024", 3, 4), " p-front p-meta");
check("中文收稿日期行也算 p-meta", fn.deskPClass("收稿日期：2024-08-23", 3, 4), " p-front p-meta");
// 署名标记（* 通讯 / § 同等贡献）在期刊里是上标；算式里的星号不许被抬起来
check("Rang,* 的星号排成上标", fn.deskMark("Jie Rang,* Liqiu Xia").indexOf('<sup class="dmark">*</sup>') > 0, true);
check("串尾的通讯星号也上标", fn.deskMark("and Zirong Zhu*").indexOf("<sup"), 14);
check("§ 同等贡献标记上标", fn.deskMark("Ziyuan Xia,§ Jun Li").indexOf('dmark">§<') > 0, true);
check("算式里的星号不上标（后面跟数字）", fn.deskMark("5 * 3 = 15").indexOf("dmark"), -1);
check("标签属性里的星号不被污染", fn.deskMark("<img alt=\"a*b\">"), "<img alt=\"a*b\">");
check("作者行的通讯星号不配成斜体", fn.mdInline("Jie Rang,* Liqiu Xia, and Zirong Zhu*").indexOf("<em>"), -1);
check("算式 2 * 3 * 4 不动", fn.mdInline("换算 2 * 3 * 4 与 5 * 10^2 mg/L").indexOf("<em>"), -1);
check("加粗仍优先于斜体", fn.mdInline("**加粗**仍优先").indexOf("<strong>加粗</strong>"), 0);
check("一段里两处斜体都认", (fn.mdInline("*spnA* 与 *chsII*").match(/<em>/g) || []).length, 2);
// 真论文里出现过 *S. spinosa*’s —— 闭星号后面是**排版右引号**（所有格），
// 边界类里没有它就把一串星号留给读者了。
check("斜体后接排版右引号（所有格）也认",
      fn.mdInline("surpassing *S. spinosa*\u2019s biomass").indexOf("<em>S. spinosa</em>\u2019s") > 0, true);
// ⑤⁗ 期刊解剖学（_desk_ruler.py 拿 ACS 真论文对拍量出来的那一层）：
// 一篇正常期刊论文的部件是固定的 —— 前置件 / 摘要 / 关键词 / 编号小节 / 图注表注 /
// 悬挂缩进的参考文献。改前那把尺子量出来 117 块里 104 块全被判成正文，
// 因为判据只写了中文那一半。下面每条都打在真源码切片上。
check("(1) 式文献各自成段", fn.deskBlocks("(1) Passos LC. Biocontrol Sci Technol, 2016.\n(2) Li X, Guo R. Pest Manag Sci, 2019.").filter(b => fn.deskIsRef(b.t)).length, 2);
check("(n) 短行不算文献（正文里「(2) 千斤」这类开头）", fn.deskIsRef("(2) 千斤重"), false);
check("[1] 式中文文献照旧认（不回退）", fn.deskIsRef("[1] 张三. 菌物学报, 2019."), true);
check("(2020) 年份开头不认作文献", fn.deskIsRef("(2020) 年的报告指出产量提升明显"), false);
check("图 1 中文图注认", fn.deskIsCap("图 1 火山图"), true);
check("图 3：带冒号的也认", fn.deskIsCap("图 3：蛋白表达谱"), true);
check("Figure 1. 英文图注认", fn.deskIsCap("Figure 1. Volcano plot of DEGs"), true);
check("Fig. 2 缩写式认", fn.deskIsCap("Fig. 2 Heatmap of metabolites"), true);
check("Table 1. 表注认", fn.deskIsCap("Table 1. Strains and plasmids"), true);
check("Figure 1 shows… 是正文句子不是图注", fn.deskIsCap("Figure 1 shows the yield change"), false);
check("「如图 1 所示」正文段不认作图注", fn.deskIsCap("如图 1 所示，产量上升"), false);
// 第二把尺子（Frontiers in Microbiology 15）的图注**不带界点**，靠全大写标签区分；
// 同一篇里正文引用图表写的是 mixed case —— 大小写就是刊物自己给的区分符。
check("全大写 FIGURE 1 无界点也算图注", fn.deskIsCap("FIGURE 1 The mechanisms of cordycepin biosynthesis"), true);
check("NBSP + en-space 的 TABLE 也算图注",
      fn.deskIsCap("TABLE 1\u00a0\u2002Microbial fermentation and mutation breeding"), true);
check("mixed case 无界点仍算正文（不许因为大写形就放宽）",
      fn.deskIsCap("Table 2 ATMT has been widely used for transformation"), false);
// 注属于表还是图：号段（t1/f1）、注在表上方还是图下方、英文表注整行加粗，三处同一个判断。
check("Table 1. 是表注", fn.deskIsTcap("Table 1. Strains and plasmids"), true);
check("表 2：是表注", fn.deskIsTcap("表 2：菌株与质粒"), true);
check("Figure 1. 不是表注", fn.deskIsTcap("Figure 1. Volcano plot of DEGs"), false);
check("图 1 不是表注", fn.deskIsTcap("图 1 火山图"), false);
check("复数 Tables 1 and 2 是正文不是表注", fn.deskIsTcap("Tables 1 and 2 summarize the strains"), false);
// 前置件（作者行 / 单位 / 日期）：题名之后、摘要之前的短段落
const fB = [{ k: "h", lv: 1, t: "一种高产方法" }, { k: "p", t: "张三，李四*，王五" }, { k: "p", t: "摘要：本文提出…" }];
check("前置件边界 = 摘要所在块下标", fn.deskFrontier(fB), 2);
check("作者行判为 p-front", fn.deskPClass("张三，李四*，王五", 1, 2), " p-front");
check("题名后一大段引言不当前置件", fn.deskPClass("本".repeat(261), 1, 3), " p-body");
check("前置件不许是句子（120 字且以句号收尾的引言）",
      fn.deskPClass("本研究以棘孢糖为对象，" .repeat(8) + "。", 1, 3), " p-body");
check("长作者串（150 字无句末标点）仍是前置件",
      fn.deskPClass("张三, ".repeat(25) + "李四", 1, 3), " p-front");
check("无摘要时以第一个小标题为界", fn.deskFrontier([{ k: "h", t: "题名" }, { k: "p", t: "作者" }, { k: "h", t: "1. 引言" }]), 2);
check("摘要优先于前置件（边界内也不抢）", fn.deskPClass("摘要：本文提出…", 1, 3), " p-abs");
// 版式标签：ABSTRACT: / 图 3： 是排出来的部件，正文里同形句子不许被加粗
check("ABSTRACT: 包成标签", fn.deskLead("ABSTRACT: Spinosyns are…", "p-abs").indexOf('<b class="dlead">ABSTRACT:</b>'), 0);
check("图 3：标签加粗（全角冒号在标签内）", fn.deskLead("图 3：蛋白表达谱", "p-cap").indexOf('<b class="dlead">图 3：</b>'), 0);
check("正文里的 Table 3 不加粗", fn.deskLead("Table 3 of the survey shows…", "p-body"), "Table 3 of the survey shows…");
check("Authors of this paper… 不当标签", fn.deskLead("Authors of this paper thank…", "p-front"), "Authors of this paper thank…");
// 中英文是两套排版惯例，判错就把中文稿的首行缩进关掉
check("纯英文长稿判为英文稿", fn.deskLatin("The spinosyn gene cluster ".repeat(20)), true);
check("中文稿夹基因名不判成英文稿", fn.deskLatin("低温诱导 CHS1 与 CHS2 的表达量上升，".repeat(20)), false);
check("零星英文短句不误判", fn.deskLatin("Abstract of a paper"), false);
// 标题不许被块级 markdown 吃掉编号：页面与大纲必须是同一件事
check("标题里的编号不被当列表标记", fn.deskHead("1. INTRODUCTION"), "1. INTRODUCTION");
check("标题仍走行内加粗", fn.deskHead("2. **结果**").indexOf("<strong>结果</strong>") > 0, true);
check("标题里的尖括号被转义", fn.deskHead("<b>x</b>").indexOf("<b>"), -1);
// 摘要段里 run-in 的关键词是另一个部件，粘在一起读不出边界
const bK = fn.deskBlocks("ABSTRACT: " + "本文研究棘孢糖。".repeat(12) + " KEYWORDS: spinosyn, genome shuffling");
check("run-in 的 KEYWORDS 拆成两段", bK.length, 2);
check("拆出的关键词段判为 p-kw", fn.deskPClass(bK[1].t, 1, 9), " p-kw");

// ⑤⁴ 图注→资产严格匹配：文件名**自己承认**是图 N 才嵌。
// 为什么这么严：文本的「图 1」和盘上的资产之间没有可靠锚点，猜配会配错图 ——
// 配错（把别人的结果当这篇的）比不配更糟。这里把允许与拒绝的形状都钉死。
const F = n => ({ name: n, ext: n.split(".").pop().toLowerCase(), path: n });
check("fig1_x.png 认作图 1", (fn.deskFigPick(1, [F("fig1_volcano.png")]) || {}).name, "fig1_volcano.png");
check("fig_1 / fig-1 / figure1 / 图1 都认",
      !!fn.deskFigPick(1, [F("fig_1.png")]) && !!fn.deskFigPick(1, [F("fig-1.png")]) &&
      !!fn.deskFigPick(1, [F("figure1_x.png")]) && !!fn.deskFigPick(1, [F("图 1.png")]), true);
check("fig12 不算图 1（数字后不许再跟数字）", fn.deskFigPick(1, [F("fig12_a.png")]), null);
check("fig_result1 不是图 1（诱饵形状）", fn.deskFigPick(1, [F("fig_result1_x.png")]), null);
check("fig_conversion 无号不算", fn.deskFigPick(1, [F("fig_conversion.png")]), null);
check("两份不同名都像 ⇒ 含糊，全不嵌", fn.deskFigPick(1, [F("fig1_a.png"), F("fig1_b.png")]), null);
check("同名多格式 ⇒ 挑渲得动的 png（弃 pdf）", (fn.deskFigPick(1, [F("fig1_x.pdf"), F("fig1_x.png")]) || {}).ext, "png");
check("只剩渲不动的 pdf ⇒ 不嵌", fn.deskFigPick(1, [F("fig1_x.pdf")]), null);
check("图 2 认自己的号（不串号）", (fn.deskFigPick(2, [F("fig1_a.png"), F("fig2_b.png")]) || {}).name, "fig2_b.png");
check("空清单不嵌", fn.deskFigPick(1, []), null);
// 表也要能嵌：真期刊稿的表常整张出图（table1_x.png 就是表 1），判据同一条 —— 只认文件名自己承认的号
check("table1_x.png 认作表 1（kind=t）", (fn.deskFigPick(1, [F("table1_results.png")], "t") || {}).name, "table1_results.png");
check("表2 认自己的号（中文表号同一条判据）", (fn.deskFigPick(2, [F("table1_a.png"), F("表2_b.png")], "t") || {}).name, "表2_b.png");
check("table12 不算表 1（数字后不许再跟数字）", fn.deskFigPick(1, [F("table12_a.png")], "t"), null);
check("tbl_1_x.png 不自认表 1 ⇒ 不嵌", fn.deskFigPick(1, [F("tbl_1_x.png")], "t"), null);
// 号段：图 1 与表 1 各指各的东西。共用一个号就会把图 1 的配图挂到表 1 的注下面
//（真论文对拍当场抓到：8 张图嵌出了 9 个 figure）。
check("表 1 不抢图 1 的资产", fn.deskFigPick(1, [F("fig1_a.png")], "t"), null);
check("图 1 不抢表 1 的资产", fn.deskFigPick(1, [F("table1_a.png")]), null);
check("表 1 认自己的资产", (fn.deskFigPick(1, [F("fig1_a.png"), F("table1_b.png")], "t") || {}).name, "table1_b.png");
check("图 2 仍认图族（号段不干扰既有判据）", (fn.deskFigPick(2, [F("table2_a.png"), F("fig2_b.png")]) || {}).name, "fig2_b.png");

// ⑤⁵ 续读位置：node 沙箱没有 localStorage ⇒ 存取是空转、deskPosOf 恒 0 ——
// 可测的是**纯逻辑**：挤旧留新、未知稿号/无稿号回 0。
check("无稿号 ⇒ 位置 0", fn.deskPosOf(""), 0);
check("未知稿号 ⇒ 位置 0", fn.deskPosOf("nope"), 0);
const pruned = fn.deskPosPrune({ a: { r: 0.5, t: 1 }, b: { r: 0.4, t: 3 }, c: { r: 0.3, t: 2 } }, 2);
check("挤旧留新（t 最大的两条留下）", Object.keys(pruned).sort().join(","), "b,c");
check("不超上限时原样返回", Object.keys(fn.deskPosPrune({ a: { r: 0.5, t: 1 } }, 20)).length, 1);

// ⑥ 稿件台拿到的是**全稿**，不是成果卡里那个 460px 滚动框里的截断版
const pl = fn.deskPayload({ draft: ONE, steps: 5, run_id: "20260919-120000",
  lint: { score: 88, metrics: { chars: ONE.length } } });
check("正文与原文逐字节相同（一字不少）", pl.text === ONE, true);
check("meta 报字数", pl.meta.indexOf(ONE.length + " 字") >= 0, true);
/* ★ 2026-10-01 用户裁定「直接抄 DSH」后**反转**的判据。
   原来这里是「meta 报运行号（知道读的是哪一次）」期望 true —— 它把 run id 钉在副题上。
   但 DSH 的 ui-sidebar-right 明文「面板没有标题行」，14 位 run id 更是没有任何位置；
   用户的原话是「为什么非得给用户看他不需要看的呢？用户看得懂吗？」。
   所以这条判据原来**判的方向就是错的**，现在反过来判：副题里**不许**出现 run id。
   （run id 没丢：deskReadFile 那条路仍照常，稿件台的 runId 字段也还在，历史回放靠它。） */
check("meta 不许摊 run id（用户看不懂的内部号）", pl.meta.indexOf("20260919-120000") < 0, true);
check("meta 不许摊步数这类内部量", /步/.test(pl.meta), false);
/* 标题要是**稿子自己的名字**，不是任务原文。
   pl 这段夹具既无标题块、又没给 final ⇒ 如实退回占位名「交付正文」，
   不许编造、也不许把任务原文塞进来当标题。 */
check("无标题块且无摘要：标题如实退回占位名", pl.title, "交付正文");
check("标题里不许混进 run id", pl.title.indexOf("20260919-120000") < 0, true);
/* 有标题块时，标题必须取**正文里的标题**（与大纲第一行同源），而不是任务原文 */
const ph = fn.deskPayload({ draft: "# 冬虫夏草人工培育\n\n正文若干。", run_id: "20260101-000000",
  lint: { metrics: { chars: 20 } } });
check("有标题块的稿子：标题取正文标题，不取任务原文", ph.title, "冬虫夏草人工培育");
/* 没有标题块时退回一句话摘要，且摘要**不许**是空、不许带 run id */
const pt = fn.deskPayload({ draft: "就一段没有标题的正文。", steps: 3, run_id: "20260101-000000",
  final: "它写完了一句话" });
check("无标题块时退回摘要当标题", pt.title, "它写完了一句话");
check("摘要里也不许混进 run id", pt.title.indexOf("20260101-000000") < 0, true);
const pe = fn.deskPayload(null);
check("没稿时正文为空", pe.text, "");
check("没稿时说清为什么", pe.meta.indexOf("还没有正文") >= 0, true);
check("没稿时不编造 0 字这种假数", /0 字/.test(pe.meta), false);

// ⑦ 产出物 → 稿件台：**不截断**，失败了不撒谎
// 内联「预览」在 2 万字符处就截断（长稿只能下载了再看），稿件台必须没有这个上限。
// 这条断言就是那个上限的负控：谁把 slice(0,20000) 加回来，这里立刻红。
(async () => {
  const BIG = "长".repeat(30000);
  let calls = [];
  let drf = mk({ fetch: async () => ({ ok: true, text: async () => BIG }),
                 openDesk: o => calls.push(o) }).deskReadFile;
  await drf("r1", { path: "draft.md", size: 90000, ext: "md" });
  check("先开面板再说读取中（不是点了没反应）", calls.length >= 2 && calls[0].meta.indexOf("读取中") >= 0, true);
  check("拿到的正文是全稿（30000 字一个不少）", (calls[calls.length - 1] || {}).text === BIG, true);
  check("meta 报的是全稿字符数", (calls[calls.length - 1] || {}).meta.indexOf("30000") >= 0, true);

  calls = [];
  drf = mk({ fetch: async () => ({ ok: false, status: 404 }),
             openDesk: o => calls.push(o) }).deskReadFile;
  await drf("r2", { path: "x.md", size: 10, ext: "md" });
  const bad = calls[calls.length - 1] || {};
  check("读失败时不假装有内容", bad.text, "");
  check("读失败时如实说失败", /读取失败/.test(bad.meta), true);

  // ⑧ 历次稿子：**点哪次就报哪次** —— 页码与正文必须同源。
  // 若两者不同源，你读的是 A 稿、标的是 B 次，而界面上看不出任何异常。
  let opened = [];
  let dlr = mk({ api: async () => ({ goal: "旧的一次", result: { draft: "旧稿正文。", steps: 3 } }),
                 openDesk: o => opened.push(o) }).deskLoadRun;
  await dlr("20260101-000001");
  const o1 = opened[opened.length - 1] || {};
  check("历次：正文来自那一次", o1.text, "旧稿正文。");
  check("历次：标题用那一次的目标", o1.title, "旧的一次");
  // ★ 判据打在"meta 有没有点名是哪一次"上，但**不认运行号这个形状**：
  //   运行号（20260101-000001）是机器主键，摊给用户看是行话；
  //   用户要的是"我读的是哪一次"，而运行号前 15 位本来就是时刻。
  //   所以这里断"是那个时刻"，同一条 313 也断"不是原始运行号"——
  //   只断前者会放过"两个都写"（时刻 + 运行号）那种没清干净的写法。
  check("历次：meta 点名是哪一次（用时刻，不是运行号）", o1.meta.indexOf("01-01 00:00") >= 0, true);
  check("历次：meta 里不再摊原始运行号", o1.meta.indexOf("20260101-000001"), -1);
  check("历次：runId 回填（下拉才能标出当前项）", o1.runId, "20260101-000001");

  opened = [];
  dlr = mk({ api: async () => ({ goal: "没稿的一次", result: {} }),
             openDesk: o => opened.push(o) }).deskLoadRun;
  await dlr("20260101-000002");
  const o2 = opened[opened.length - 1] || {};
  check("历次：那次没稿就说没稿", o2.text, "");
  check("历次：没稿时不编造「N 字」", /\d+ 字/.test(o2.meta), false);

  opened = [];
  dlr = mk({ api: async () => { throw new Error("HTTP 500"); },
             openDesk: o => opened.push(o) }).deskLoadRun;
  await dlr("20260101-000003");
  const o3 = (opened[opened.length - 1] || {});
  // 后端原文是「任务不存在」= 那条没归档 ⇒ 必须说"没存"，不许说"出错了/读取失败"（归因不清）。
  opened = [];
  dlr = mk({ api: async () => { throw new Error("任务不存在"); },
             openDesk: o => opened.push(o) }).deskLoadRun;
  await dlr("20260101-000004");
  const o4 = (opened[opened.length - 1] || {});
  check("历次：没归档时说清是「没存」", /没有落归档快照/.test(o4.meta), true);
  check("历次：没归档时不许写成「读取失败/出错了」", /读取失败|出错了/.test(o4.meta), false);
  check("历次：没归档时给下一步（会话列表看数 · 历次换一条）", /侧栏会话列表/.test(o4.meta) && /换一条/.test(o4.meta), true);
  check("历次：后端原文留着（技术细节不丢）", o4.meta.indexOf("任务不存在") >= 0, true);
  check("历次：请求真没成时不甩锅给「没归档」", /没读到正文/.test(o3.meta) && !/没有落归档/.test(o3.meta), true);
  // 判据只许有一处来源：下拉与历史表各写一份，将来必漂（本缺陷的成因）。
  const nPred = (src.match(/replay !== false/g) || []).length;
  check("replay 判据只写了一处（canReplay）", nPred, 1);
  check("下拉走的是 canReplay", /filter\(canReplay\)/.test(src), true);
  /* 「历史表」已并入侧栏会话列表（openSession → replay）——
     canReplay 的消费方只剩稿件台下拉这一处，上一条判据管的就是它。 */
  check("历史不再有第二张表（旧 paintHist 已随会话列表退役）",
        /paintHist|histReplay|histDetail/.test(src), false);

  process.exit(fail ? 1 : 0);
})();
"""

fails: list[str] = []
warns: list[str] = []
counts: dict[str, int] = {}

def ok(label: str, n: int = 0) -> None:
    counts[label] = counts.get(label, 0) + n
    print(f"  [PASS] {label}" + (f"（{n} 项）" if n else ""))


def bad(label: str, detail: list[str]) -> None:
    print(f"  [FAIL] {label}")
    for d in detail[:12]:
        print(f"         {d}")
    if len(detail) > 12:
        print(f"         …还有 {len(detail) - 12} 条")
    fails.extend(detail or [label])


def find_npm_node() -> str | None:
    for c in (str(Path.home() / ".workbuddy" / "binaries" / "node" / "versions" / "22.22.2-2" / "node.exe"),
              r"C:\Program Files\nodejs\node.exe", "node"):
        if shutil.which(c) or Path(c).exists():
            return c
    return None


SELF_TMP = ROOT / "runs" / "_uicheck"


def _run_on(path: Path) -> int:
    """以子进程跑一遍本脚本（走真实 CLI，而不是内部函数 ——
    否则自检测的是"我以为的调用方式"，而不是用户真正会跑的那条路）。"""
    r = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--file", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return r.returncode


def _injections(src: str) -> list[tuple[str, str, str]]:
    """从**文件本身**派生注入点，而不是写死字符串 ——
    写死的话，界面一改名，注入点就失效；而失效的注入点会静默变成"没检查"。"""
    out: list[tuple[str, str, str]] = [
        ("新任务：发送按钮绕过任务单", '$("#btnSend").onclick = submitComposer;',
         '$("#btnSend").onclick = () => send();'),
        ("体检卡函数被改名", "function lintCard(", "function lintCardX("),
        ("新增指标药丸被删", '(m.figures ? "<span>图 "', '(false ? "<span>图 "'),
        # 稿件台的两条负控 —— 一条打行为（句群降级），一条打接线。
        # 「能判得出坏」比「说它好」重要：恒过检查器能完美通过一切正控。
        # ★ 2026-10-07 随 deskBlocks 重写同步：句群降级判据改打新形状
        #   （k:"p" 守卫 —— 结构块不许被按句切碎的那一行），
        #   注入 `out.length === 1` → `false` 让降级永远不触发，单行长稿不再切块。
        ("稿件台：句群降级被删",
         'if (out.length === 1 && out[0].k === "p" && out[0].t.length > 140)',
         'if (false && out.length === 1 && out[0].k === "p" && out[0].t.length > 140)'),
        ("稿件台：不再记录最近一次出稿", "LAST_RESULT = res;", ""),
        ("稿件台：读产出物又被截断回 2 万字", "openDesk({ text: t, title: f.path,",
         "openDesk({ text: t.slice(0, 20000), title: f.path,"),
        # ★ 2026-09-27 修（这条负控此前是**失效**的，表现为自检「漏报」）：
        #   检查里第 595/597 两条断言是 `'data-desk="' not in js` **或**
        #   `"[data-desk]" not in js`，而文件里 `data-desk="` 只出现在**按钮生成处**
        #   （2451/2843）、`[data-desk]` 只出现在**选择器**（2462/2854）。
        #   原来只把 `[data-desk]`→`[data-deskX]`，于是 595 那条照样为假、检查照样过；
        #   改只打生成处则 597 那条照样为假 —— **打一处永远打不穿"或"**。
        #   注入点必须同时覆盖**判据依赖的每一处**。
        #   （教训：负控失效时自检报的是「漏报」，看着像检查器不灵，
        #     其实是**探针没打中**。先怀疑探针。）
        ("稿件台：产出物的读全稿按钮被摘", 'data-desk="', 'data-deskX="'),
        ("稿件台：产出物的读全稿按钮失联（选择器被改名）",
         'querySelectorAll("[data-desk]")', 'querySelectorAll("[data-deskX]")'),
        # 点了"历次"里某一次，却不再回报那是哪一次 ——
        # 正文与身份分了家，你读的是 A 稿、标的是 B 次，而界面上看不出任何异常。
        ("稿件台：历次不回报是哪一次", "text: n, runId: rid,", 'text: n, runId: "",'),
        # ── W26F 稿件台表格（三线表）：五条线各自打一个负控 ──
        # 这组的判据是"按权重压一遍看生效值"，所以负控打的正是**生效层**：
        # 基线 `.mdtbl th,td{border:…}` 是四边 shorthand，覆写只要漏说一边就赢回来。
        ("稿件台表格：td 竖线又画回来了",
         ".dtext .dc.journal .draw .mdtbl td{border-width:0}",
         ".dtext .dc.journal .draw .mdtbl td{border-width:0 0 0 1px}"),
        ("稿件台表格：行间线又画回来了（真表只有三条横线）",
         ".dtext .dc.journal .draw .mdtbl td{border-width:0}",
         ".dtext .dc.journal .draw .mdtbl td{border-width:0 0 1px}"),
        ("稿件台表格：顶线没了（只剩两条横线）",
         "border-top:1.5px solid var(--fg);\n  border-bottom:1px solid var(--fg)",
         "border-bottom:1px solid var(--fg)"),
        ("稿件台表格：底线被统一成发丝灰线（三条线不再分轻重）",
         "tbody tr:last-child td{border-bottom:1.5px solid var(--fg)}",
         "tbody tr:last-child td{border-bottom:var(--hair) solid var(--line-2)}"),
        ("稿件台表格：英文表体字号写进了中文作用域（中英又共用一把尺）",
         ".dtext .dc.journal.latin .draw .mdtbl{font-size:calc(12.8px",
         ".dtext .dc.journal .draw .mdtbl{font-size:calc(12.8px"),
        # ── W26F·B 字号阶梯档位 ──
        ("稿件台阶梯：题名又被拽回 2.06 那一档（量错分母的那个数）",
         "font-size:calc(27px * var(--desk-scale,1));line-height:calc(34px * var(--desk-scale,1))}",
         "font-size:calc(33px * var(--desk-scale,1));line-height:calc(40px * var(--desk-scale,1))}"),
        ("稿件台阶梯：中文题名被英文尺子带跑了",
         "font-size:calc(22px * var(--desk-scale,1));line-height:calc(34px * var(--desk-scale,1));",
         "font-size:calc(27px * var(--desk-scale,1));line-height:calc(34px * var(--desk-scale,1));"),
        ("稿件台阶梯：正文基准不再是 16px（每一档的比值都不再是那个意思）",
         "--fs-base:16px;", "--fs-base:15px;"),
        ("稿件台字族：图表注又落回 UI 无衬线",
         "  font-family:var(--font-serif)}\n/* 标签只加粗、不换族",
         "}\n/* 标签只加粗、不换族"),
        ("稿件台字族：图表注的标签换成了无衬线（加粗之外还换了族）",
         ".dtext .dc.journal p.p-cap .dlead{font-family:var(--font-serif)}",
         ".dtext .dc.journal p.p-cap .dlead{font-family:var(--font)}"),
        ("稿件台断字：英文参考文献的断字规则失联（两端对齐就出河道）",
         ".dtext .dc.journal.latin p.p-ref{hyphens:auto}",
         ".dtext .dc.journal.latin p.p-refX{hyphens:auto}"),
        # ── W3 新增检查的负控 ────────────────────────────────────
        # 负控必须打中**判据依赖的每一处**。侧栏那条判据是"或"型
        # （控件 / 状态 / localStorage / 收窄规则 四条任一断掉即报），
        # 所以只打一处**永远打不穿** —— 上一轮就在这上面栽过（见上面那段注）。
        #
        # ★ 这里连打两处（`SIDE_KEY` 赋值的引号 + 注释里的那处引用）：
        #   我第一版只替换字符串本身，可是它在文件里出现 2 次
        #   （1320 注释 + 1323 常量），`replace` 全换之后判据
        #   `"rongke-side-fold" not in js` 仍然为**假** ⇒ 负控漏报。
        #   这和上面那次「打一处打不穿'或'」是**同一类**错误，
        #   换了个面：判据查的是"字符串在不在"，那就要么全打掉，要么改判据。
        #
        #   ★★ 而且**替换值不能是原串的超串**：我先前用 `...-foldX`，
        #   于是 `"rongke-side-fold" in js` 照样为真（它是 `...-foldX` 的子串），
        #   负控又一次漏报。判据是 `in`，替换就必须把原串**彻底打散**，
        #   否则换了个写法、意思没变、注入等于没做。
        #   这和「打一处打不穿'或'」是同一个坑的第三个面：**注入的语义要对准判据的语义**。
        #   ★ W4：这条原来打「持久化键 rongke-side-fold」—— 但 DSH 要求刷新即重置，
        #     那个键**已被撤**（不存在了），注入点会失效。改成打**自动收起阈值**：
        #     把 isNarrow 的 `< 1024` 改成别的数，DSH 折叠判据必须立刻红。
        ("侧栏：自动收起阈值不再是 DSH 的 1024",
         "innerWidth < 1024", "innerWidth < 700"),
        # 设置页那条打"接线"：区块在、导航项在，但接的接口被换掉。
        # 同理，`/api/settings` 出现 3 次（读、错误提示、写），全打；
        # 替换值同样不能含原串（不能用 `/api/settingsX`）。
        ("设置页：不再接 /api/settings", "/api/settings", "/api/settingz"),
        # W4 右栏几何：三个 DSH 数值各打一处，判据查的是"数值在不在"，
        # 所以替换值同样不能含原串（不能 `0.450`、`- 4000`），否则 `in` 又为真、负控漏报。
        ("右栏：首开不再是 DSH 的 45%", "innerWidth * 0.45", "innerWidth * 0.30"),
        ("右栏：上限不再是 70% 视口", "vw * 0.70", "vw * 0.55"),
        ("右栏：中间不再保底 400", "deskSideW() - 400", "deskSideW() - 200"),
        # W4 设置页信息架构：分节标题与通用节的真控件。
        ("设置页：「通用」分节标题被摘", ">通用<span", ">外观A<span"),
        ("设置页：通用节外观控件失联", "setTheme(v, true)", "setThemeQ(v, true)"),
        # ★ 用户报「侧边栏也打不开啊」那条判据的负控。
        #   判据是"抽屉层存在(z-index:70) 时，#btnFold 有没有被抬到 >70"。
        #   注入 = 把抬起规则的 z-index 压到抽屉之下（模拟缺陷复现）。
        #   ★ 注意超串坑：判据查的是 `#btnFold {... z-index: N}`，
        #     注入值不能是 `80` 的超串/子串混淆 —— 这里用 `z-index:60`（小于 70），
        #     语义正好是"没抬起来"，且不含原串 `z-index:80`。
        ("侧栏：☰ 没被抬到窄屏抽屉之上（关不掉）",
         "z-index:80", "z-index:60"),
        # ── W14-B 原子件层：每件一个负控（工单判据 B12 要求 ≥11）──────────
        # ★ 老坑在这里同样成立：注入值**不能是被打串的超串**（判据查 "36px"，
        #   把 36px 换成 361px 等于没打）；每条替换都语义明确地把真值挪走。
        ("W14B·B0 别名层：--label-primary 不再指荣珂 --fg",
         "--label-primary:var(--fg);", "--label-primary:var(--fgQ);"),
        ("W14B·B1 Button：disabled 透明度不再是 .4",
         ".btn:disabled{cursor:not-allowed;opacity:.4}",
         ".btn:disabled{cursor:not-allowed;opacity:.35}"),
        ("W14B·B2 Input：外壳高度不再是 32px",
         "gap:6px;height:32px;padding:0 8px", "gap:6px;height:34px;padding:0 8px"),
        ("W14B·B3 Switch：轨道不再是 36×20",
         "flex:0 0 auto;width:36px;height:20px;padding:2px",
         "flex:0 0 auto;width:40px;height:20px;padding:2px"),
        ("W14B·B4 Checkbox：勾选色不再走品牌色",
         "accent-color:var(--brand-primary)", "accent-color:var(--label-dimmed)"),
        ("W14B·B5 Segmented：指示器退回写死宽度（不再用 --seg-n 算术）",
         "width:calc((100% - 8px - 2px * (var(--seg-n) - 1)) / var(--seg-n))",
         "width:calc((100% - 8px - 2px * 1) / 2)"),
        ("W14B·B6 Tag：warning 的 12% 被统一成 10%（DSH 明写这是故意的）",
         "var(--warn) 12%", "var(--warn) 10%"),
        ("W14B·B7 Pill：active 描边从 inset 阴影漂成真边",
         "box-shadow:inset 0 0 0 1px var(--ghost-active-border)",
         "box-shadow:0 0 0 1px var(--ghost-active-border)"),
        ("W14B·B8 StateDot：芯 inset 不再是 20%（10px 槽画不出 6px 芯）",
         "inset:20%;border-radius:50%", "inset:25%;border-radius:50%"),
        ("W14B·B8 StateDot：两个动画不再是共用 1.5s 周期",
         "animation:dot-spin 1.5s linear", "animation:dot-spin 2s linear"),
        ("W14B·B9 DisclosureRow：指头槽不再是 16px",
         "flex:none;width:16px;height:16px;display:inline-flex",
         "flex:none;width:18px;height:18px;display:inline-flex"),
        ("W14B·B10 Modal：卡片不再是 min(380px,100%)",
         "width:min(380px,100%)", "width:min(420px,100%)"),
        ("W14B·B11 焦点环：退回硬编码且在指针模态清立面",
         "outline:var(--focus-ring-width) solid var(--focus-ring-color);outline-offset:2px",
         "outline:2px solid var(--focus-ring-color);outline-offset:2px;box-shadow:none"),
        # ── W14-E 接线落地（E5）：每件一个负控 = 把那一件的 class 从**标记**里摘掉 ──
        # ★ 为什么这组负控特别要紧：W14-B 的判据全绿而页面毫无变化（CSS 有、
        #   标记没有）。摘掉标记里的那处 class，E5 必须变红；摘不动就说明判据
        #   其实是在查 CSS —— 那是恒真判据的一种，正是这一单要堵的洞。
        #   注入一律只动 class 名，不动 id / data-* / 事件（工单 §1.1 的红线）。
        ("W14E·segctl：设置页外观主题没穿分段控件",
         'class="segctl" role="tablist"', 'class="segctlQ" role="tablist"'),
        ("W14E·switch：设置页过程开关没穿 .switch",
         'class="switch" role="switch"', 'class="switchQ" role="switch"'),
        ("W14E·ipt：过滤输入的外壳被摘掉（3 处全摘）",
         'class="ipt"', 'class="iptQ"'),
        ("W14E·tag：列表来源标签被摘掉（3 处全摘）",
         'class="tag"', 'class="tagQ"'),
        ("W14E·apill：场景胶囊的 .apill 被摘掉（保留 .chip，它才是承重的）",
         'class="chip apill int"', 'class="chip int"'),
        ("W14E·disclosure：过程折叠行的 .disclosure 被摘掉",
         ' disclosure"', '"'),
        ("W14E·toast：复制反馈的 .toast 被摘掉",
         'class="toast" role="status"', 'class="toastQ" role="status"'),
        ("W14E·dot：状态点的 .dot 被摘掉",
         'class="dot"', 'class="dotQ"'),
        ("W14E·btn：按钮的 .btn 被摘掉（4 处全摘）",
         'class="btn ', 'class="btnQ '),
        ("W14E·card：卡片的 .card 被摘掉（20 处全摘）",
         'class="card', 'class="cardQ'),
    ]
    m = RE_ID_USE.search(src)                       # ① JS 引用的 id 在 HTML 里被改名
    if m:
        v = m.group(1) or m.group(2)
        out.append((f"id={v} 在 HTML 里被改名", f'id="{v}"', f'id="{v}_broken"'))
    m = RE_ID_DEF.search(src)                       # ② id 重复
    if m:
        out.append((f"id={m.group(1)} 重复", f'id="{m.group(1)}"',
                    f'id="{m.group(1)}" id="{m.group(1)}"'))
    used = set(RE_VAR_USE.findall(src))             # ③ 被 var() 用到的变量定义被拼错
    for v in RE_VAR_DEF.findall(src):
        if v in used:
            out.append((f"CSS 变量 {v} 定义被拼错", f"{v}:", f"{v}X:"))
            break
    m = RE_VSECTION.search(src)                     # ④ 视图区块缺失
    if m:
        out.append((f"视图 id=v-{m.group(1)} 缺失", f'id="v-{m.group(1)}"',
                    f'id="v-{m.group(1)}_gone"'))
    return out


def _copy_ui_tree(dst: Path) -> Path:
    """把 index.html + static/ 真镜像到临时目录，返回临时 index.html。

    2026-10-29 拆分后，CSS/JS 不再内联在 index.html 里。自检若只拷一个
    HTML 文件，main() 找不到 /static/ 资产，注入点也会大面积"失效" ——
    那是夹具不完整，不是判据变松。镜像整棵树才是真实运行形态。
    """
    T.stash(dst, "uicheck-selftmp")
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DEFAULT, dst / "index.html")
    static = DEFAULT.parent / "static"
    if static.is_dir():
        shutil.copytree(static, dst / "static", dirs_exist_ok=True)
    ico = DEFAULT.parent / "app.ico"
    if ico.is_file():
        shutil.copy2(ico, dst / "app.ico")
    return dst / "index.html"


def _apply_injection(dst: Path, old: str, new: str) -> int:
    """在整棵临时 UI 树里全量替换，返回命中次数。

    注入点现在可能住在任何静态文件里；只搜 index.html 会把"文件搬过家"
    误报成"注入点失效"。命中 0 仍然必须报错 —— 那是判据真的失联了。
    """
    hits = 0
    for p in sorted(dst.rglob("*")):
        if p.suffix not in (".html", ".css", ".js"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        n = text.count(old)
        if n:
            hits += n
            p.write_text(text.replace(old, new), encoding="utf-8")
    return hits


def self_test() -> int:
    """故障注入自检：**先证明它判得出坏**，再看它说好。

    为什么不能只跑一遍干净文件就放心：一个**恒返回 0** 的检查器能完美通过那种自检。
    本项目实测过同类事故 —— 修完一个误报后检查变成"永不报"，
    因为它自我闭环、恒为通过。**只有负控的自检等于没测。**
    """
    index_src = DEFAULT.read_text(encoding="utf-8")
    # 注入点要从"界面真实加载的全部源码"里派生，而不是只看 index.html。
    ui_src = index_src
    static = DEFAULT.parent / "static"
    if static.is_dir():
        for f in sorted(static.glob("*")):
            if f.suffix in (".css", ".js"):
                ui_src += "\n" + f.read_text(encoding="utf-8", errors="replace")
    # ★ `parents=True, exist_ok=True` 不够：上一轮收尾可能把整棵目录**挪走**了
    #   （见本函数末尾的 stash）。这里先确保存在，再靠下面每个文件"用到前重新写"
    #   来摆脱对残留状态的依赖 —— 自检必须自给自足。
    SELF_TMP.mkdir(parents=True, exist_ok=True)
    for _stale in list(SELF_TMP.glob("inject_*")) + list(SELF_TMP.glob("clean")):
        # 零删除纪律：挪，不删。挪完目录里干净，下一轮的注入副本不会和旧的混。
        T.stash(_stale, "uicheck-selftmp")
    fails: list[str] = []

    clean = _copy_ui_tree(SELF_TMP / "clean")
    rc = _run_on(clean)
    print(f"  [{'PASS' if rc == 0 else 'FAIL'}] 正控：原文件应通过（exit={rc}）")
    if rc != 0:
        fails.append("正控失败：原文件都过不了，说明检查器本身坏了")

    inj = _injections(ui_src)
    # ── 判据 B12：W14-B 每件原子件必须各有一个负控（工单第 3 节，要求 ≥11）──
    # 恒过判据就是这么混进来的：判据写了一堆、一个也打不穿。数量单独报。
    n_b = sum(1 for name, _, _ in inj if name.startswith("W14B"))
    b12_ok = n_b >= 11
    print(f"  [{'PASS' if b12_ok else 'FAIL'}] 判据 B12：W14-B 原子件负控 {n_b} 个（要求 ≥11，每件一个）")
    if not b12_ok:
        fails.append(f"B12：原子件负控只有 {n_b} 个（<11）—— 有件没负控，它的判据可能是恒真的")

    # ── 判据 E5b（工单 §3）：本单每件**接上的**原子件都要有负控，一个都不能少 ──
    # 工单原话："为每一件你接上的原子件加一条判据……每一条都要配负控"。
    # 判据数量与负控数量分不开时，"加了一条判据"和"加了一条会红的判据"是两回事。
    n_e = sum(1 for name, _, _ in inj if name.startswith("W14E"))
    e5b_ok = n_e >= 7
    print(f"  [{'PASS' if e5b_ok else 'FAIL'}] 判据 E5b：W14-E 接线负控 {n_e} 个（要求 ≥7，本单接的每件一个）")
    if not e5b_ok:
        fails.append(f"E5b：接线负控只有 {n_e} 个（<7）—— 有件的落地判据打不穿，可能是恒真的")

    for i, (name, old, new) in enumerate(inj, 1):
        page = _copy_ui_tree(SELF_TMP / f"inject_{i}")
        hits = _apply_injection(page.parent, old, new)
        if hits == 0:
            # 注入点失效必须**报错**，不能跳过 —— 跳过就是把"没检查"说成"没问题"
            print(f"  [FAIL] 注入点失效：{name}（找不到 {old!r}）")
            fails.append(f"注入点失效：{name} —— 界面改过了，请更新 _injections()")
            continue
        # ★ 2026-09-27 修：原来写 `src.replace(old, new, 1)` —— **只换第一处**。
        #   而 `data-desk="` / `[data-desk]` 在文件里各有 2 处（按钮生成处 + 选择器），
        #   于是"摘掉按钮"这个注入只摘了一半：检查里 `'data-desk="' not in js`
        #   照样为假、照样通过 ⇒ **负控变成恒过，自检报「漏报」**。
        #   一个"或"型判据（`A not in js or B not in js`）**必须两处都打穿**，
        #   所以这里改成全量替换，并把命中数打出来（一处也说明判据是单点承重的）。
        if hits > 1:
            print(f"         （注入点命中 {hits} 处，已全部替换）")
        rc = _run_on(page)
        good = rc == 1
        print(f"  [{'PASS' if good else 'FAIL'}] 负控：{name} 应被判出（exit={rc}）")
        if not good:
            fails.append(f"漏报：{name}（exit={rc}，期望 1）")

    # ★ 只挪走本次生成的注入副本，**不挪整个 SELF_TMP**。
    #   原来的写法是 `T.stash(SELF_TMP, ...)` —— 把 `runs/_uicheck` 整棵挪走，
    #   可这个目录在**本函数后面还要用**（chk_*.js / probe.js / probe_desk.js
    #   都落在这里）。后果是一个**顺序依赖缺陷**：第一次跑能过（目录里已经有
    #   文件），跑过一轮之后再单跑就 `FileNotFoundError: inject_3.html` ——
    #   而我恰恰是在"全量回归之后单跑复验"这条最常用的探查路径上撞到它的。
    #   零删除纪律要的是"不删"，不是"必须连目录一起挪走"。
    T.stash_many(sorted(SELF_TMP.glob("inject_*")) + sorted(SELF_TMP.glob("clean")),
                 "uicheck-selftmp")
    print()
    if fails:
        print(f"自检未通过 ✘   失败 {len(fails)} 项")
        for x in fails:
            print(f"  · {x}")
        return 1
    print("自检通过 ✔   （正控 1 项 + 负控若干项，全部符合预期）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Web 控制台静态一致性检查")
    ap.add_argument("--file", default=str(DEFAULT))
    ap.add_argument("--self-test", action="store_true",
                    help="故障注入自检：证明这个检查器判得出坏")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    p = Path(args.file)
    if not p.is_file():
        print(f"找不到文件：{p}", file=sys.stderr)
        return 2

    html = p.read_text(encoding="utf-8", errors="replace")
    scripts = RE_SCRIPT.findall(html)
    styles = RE_STYLE.findall(html)
    # 无构建静态模块与内联代码使用同一套判据；不复制函数实现。
    for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I):
        if src.startswith('/static/'):
            f = p.parent / 'static' / Path(src).name
            if not f.is_file():
                bad('静态脚本存在', src)
                continue
            code = f.read_text(encoding='utf-8', errors='replace')
            scripts.append(code)
            html += '\n' + code
    for href in re.findall(r'<link[^>]+href=["\']([^"\']+\.css)["\']', html, re.I):
        if href.startswith('/static/'):
            f = p.parent / 'static' / Path(href).name
            if not f.is_file():
                bad('静态样式存在', href)
                continue
            code = f.read_text(encoding='utf-8', errors='replace')
            styles.append(code)
            html += '\n' + code
    probe_source = p.parent / 'static' / 'workspace.js'
    probe_path = probe_source if probe_source.is_file() else p
    print(f"检查 {p.name}  （{len(html):,} 字符 / script {len(scripts)} 块 / style {len(styles)} 块）\n")

    # ── 1. JS 语法：交给 node 真解析 ────────────────────────
    node = find_npm_node()
    if node is None:
        warns.append("找不到 node，跳过 JS 语法检查（这一项最重要，建议装 node 后重跑）")
        print("  [SKIP] JS 语法（本机无 node）")
    else:
        syn: list[str] = []
        for i, code in enumerate(scripts, 1):
            if not code.strip():
                continue
            # ★ 落点从系统临时目录改成 `SELF_TMP`（仓库内），**同名覆盖、不删**。
            #   原版是 `NamedTemporaryFile(delete=False)` + `finally: unlink()`：
            #   每块 script 花 1 点，而且落点在系统盘（跨盘时挪都挪不走）。
            tmp = SELF_TMP / f"chk_{i}.js"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("(function(){\n" + code + "\n})();\n", encoding="utf-8")
            r = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                msg = (r.stderr or "").strip().splitlines()
                syn.append(f"script#{i}: " + (msg[0] if msg else "语法错误"))
        if syn:
            bad("JS 语法", syn)
        else:
            ok("JS 语法（node --check 全过）", len(scripts))

    # ── 2. DOM id 引用 ──────────────────────────────────────
    ids = set(RE_ID_DEF.findall(html))
    used: set[str] = set()
    for m in RE_ID_USE.finditer(html):
        used.add(m.group(1) or m.group(2))
    missing = sorted(x for x in used if x and x not in ids)
    if missing:
        bad("JS 引用的元素都存在", [f'$("#{x}") 在 HTML 里没有 id="{x}"' for x in missing])
    else:
        ok("JS 引用的元素都存在", len(used))
    dyn = sorted(set(m.group(1) for m in RE_ID_DYN.finditer(html)))
    if dyn:
        # 拼接触发/拼接 id 无法静态核对 —— 但任何**前缀**都该真的存在，
        # 否则说明 v- 这类区块被改名了而 JS 没跟上。
        stale = [x for x in dyn if x and not any(i.startswith(x) for i in ids)]
        if stale:
            bad("动态选择器前缀存在", [f'$("#{x}" + …) 没有任何 id 以此开头' for x in stale])
        else:
            print(f"  [PASS] 动态选择器前缀存在（{', '.join('#%s' % x for x in dyn)}）")

    # ── 3. id 唯一 ─────────────────────────────────────────
    all_ids = RE_ID_DEF.findall(html)
    dup = sorted({x for x in all_ids if all_ids.count(x) > 1})
    if dup:
        bad("id 唯一", [f'id="{x}" 出现 {all_ids.count(x)} 次' for x in dup])
    else:
        ok("id 唯一", len(all_ids))

    # ── 4. HTML 里的函数引用有定义 ──────────────────────────
    defined: set[str] = set()
    for m in RE_FUNC_DEF.finditer("\n".join(scripts)):
        defined.add(m.group(1) or m.group(2))
    called = set(RE_ONCLICK.findall(html))
    # 也扫 JS 内部对顶层的裸调用点（形如 `foo(` 但不是方法调用）
    undefined = sorted(c for c in called if c not in defined)
    if undefined:
        bad("内联事件引用的函数有定义", [f"{c}() 未在 JS 中定义" for c in undefined])
    else:
        ok("内联事件引用的函数有定义", len(called))

    # ── 5. CSS 变量 ────────────────────────────────────────
    vdef = set(RE_VAR_DEF.findall(html))
    vuse = set(RE_VAR_USE.findall(html))
    vundef = sorted(x for x in vuse if x not in vdef)
    if vundef:
        bad("CSS 变量都有定义", [f"var({x}) 无定义 —— 样式会静默失效" for x in vundef])
    else:
        ok("CSS 变量都有定义", len(vuse))

    # ── 6. 视图路由成对 ────────────────────────────────────
    dv = set(RE_DATAV.findall(html))
    vs = set(RE_VSECTION.findall(html))
    miss_v = sorted(x for x in dv if x not in vs)
    if miss_v:
        bad("导航与视图区块成对", [f'data-v="{x}" 没有对应的 id="v-{x}"' for x in miss_v])
    else:
        ok("导航与视图区块成对", len(dv))

    # ── 6B. 新手主链：静态证明默认入口没有绕过任务单 ─────────
    ux_checks = {
        "发送按钮接统一入口": '$("#btnSend").onclick = submitComposer;' in html,
        "Enter 接统一入口": 'e.preventDefault(); submitComposer();' in html,
        "新任务默认预审": 'function submitComposer()' in html and 'return previewContract();' in html,
        "确认后才执行": 'const confirmed = DRAFT; DRAFT = null;' in html and 'send(confirmed);' in html,
        "空输入有反馈": 'if (!goal) return flagGoalMissing();' in html,
        "场景使用稳定程序键": 'pillFormOpen(c.dataset.form || "", c)' in html,
        "更多场景默认收起": 'id="morePills" hidden' in html,
        "高级诊断默认收起": 'id="advancedNav" hidden' in html,
        "最近执行记录复用 canReplay": 'SESS.find(canReplay)' in html,
        "最近 200 条不冒充总数": 'wbCell(3, rnList.length, "最近任务"' in html,
    }
    ux_bad = [name for name, passed in ux_checks.items() if not passed]
    if ux_bad:
        bad("新手任务主链与信息层级", ux_bad)
    else:
        ok("新手任务主链与信息层级", len(ux_checks))

    # ── 7. 体检卡渲染：**真跑一次**，不是读源码 ──────────────
    # 上面 6 项全是静态的，看不出「内核加了指标、界面没显示」——
    # 那正是缺陷形 D（检查存在但不覆盖）：名字对、跑得动，就是漏了那一类。
    # 做法：把 `citeDisorder` / `lintCard` 的**真实源码**抽出来交给 node 执行，
    # 喂合成的 metrics 看输出里有没有该有的东西、有没有不该有的东西。
    # 抽不到 → fail-loud（**绝不静默跳过**：跳过会把"没检查"渲染成"没问题"）。
    if node is None:
        warns.append("找不到 node，跳过体检卡渲染检查（建议装 node 后重跑）")
        print("  [SKIP] 体检卡渲染（本机无 node）")
    else:
        # 同上：落 `SELF_TMP`（仓库内）、同名覆盖、不删。
        tmp = SELF_TMP / "probe.js"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(PROBE_JS, encoding="utf-8")
        r = subprocess.run([node, str(tmp), str(probe_path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 2:
            last = (out.strip().splitlines() or ["未知错误"])[-1]
            bad("体检卡渲染", [f"源码抽取失败：{last}"])
        elif r.returncode != 0:
            detail = [ln for ln in out.splitlines() if "[FAIL]" in ln or "rror" in ln]
            bad("体检卡渲染", detail[:8] or ["node 退出码非 0"])
        else:
            ok("体检卡渲染（抽取真实源码执行）", out.count("[PASS]"))

    # ── 6.5 稿件台：先说"接线在不在"，再说"它算得对不对" ──────
    # 断言的是**三处接线**各自都在：少一处就是那个 H 型缺陷
    # （功能落地要改三处，改两处 = 界面上那个按钮永远打不开）。
    js = "\n".join(scripts)
    wire: list[str] = []
    if "LAST_RESULT = res;" not in js:
        wire.append("renderResult 没有记录最近一次出稿（LAST_RESULT = res;）—— 侧栏按钮永远只能看到空态")
    i_rr = js.find("function renderResult(")
    if i_rr < 0:
        wire.append("找不到 renderResult —— 界面改过了，请更新本检查")
    else:
        i_nx = js.find("\nfunction ", i_rr + 1)
        body = js[i_rr:i_nx if i_nx > 0 else len(js)]
        if "deskSync()" not in body:
            wire.append("renderResult 没有调 deskSync() —— 面板开着时不会换上新稿")
        if 'data-r="deskRead"' not in body and 'id="deskRead"' not in body:
            wire.append("成果卡里没有「读全稿」入口（data-r=\"deskRead\"）")
    if "deskInit();" not in js:
        wire.append("启动段没有 deskInit() —— 面板既不接线也不恢复宽度/字号")
    # 只看"调用过 deskReadFile"是不够的：把选择器改名，函数照样被调用，
    # 而按钮已经点不动了。负控实测就是这么漏过去的 —— 所以按钮与接线分开核。
    if 'data-desk="' not in js:
        wire.append("产出物清单里没有「读全稿」按钮（data-desk=\"…\"）")
    if "[data-desk]" not in js or "deskReadFile(" not in js:
        wire.append("产出物清单的「读全稿」按钮没接线（缺 [data-desk] → deskReadFile 这条路）")
    # 下拉的**标签**在 HTML 里，**接线**在 script 里 —— 两处分开核
    # （我第一版两样都在 js 里找，于是正控就红了：查错了容器）。
    if 'id="deskRun"' not in html:
        wire.append("稿件台头部没有「历次」下拉（id=\"deskRun\"）")
    if "deskLoadRun(" not in js:
        wire.append("「历次」下拉没接线（缺 deskLoadRun 这条路）")
    if wire:
        bad("稿件台接线", wire)
    else:
        ok("稿件台接线（最近一次出稿 · 随新稿刷新 · 成果卡/产出物/历次三个入口 · 启动接线）", 8)

    # ── W26F：期刊表格是三线表（"只有三条横线"要一条条量）────────────────
    # 栽过的样子：注释自称"三线表 · 无竖线"，基线 `.mdtbl th,.mdtbl td{border:…}` 却是四边
    # shorthand，journal 覆写只补了上/下 —— 竖线一直被基线画着；再往深挖又发现每个 td 还各带
    # 一条行分隔线，那是**第四条**线：四篇真论文可数的 10 张表里 8 张没有竖线（Elsevier 可数 7/7）、
    # 横线也只有 3 条或整表行底纹（`_tbl_ruler.py` 量的）。
    # 判据不能只问"文件里有没有 border-left:0"：写在选择器打不赢的规则里等于没写。
    # 所以按 CSS 权重把命中该单元格的声明依次压一遍，查**最终生效值**。
    # （祖先链只比**类名集合**：ctx 里没有的类出现在选择器里就判该规则不生效。
    #   这里要防的是"覆写没清干净"和"中英作用域写串"，不是元素级的祖先匹配。）
    css_all = re.sub(r"/\*.*?\*/", "", "\n".join(styles), flags=re.S)

    def css_weight(sel: str) -> int:
        s = re.sub(r"::?[\w-]+(\([^()]*\))?", "", sel)
        return (100 * len(re.findall(r"\.\w[\w-]*", s))
                + 10 * len(re.findall(r"#\w[\w-]*", s))
                + len(re.findall(r"(?<![\w.#-])[a-z][\w-]*", s)))

    def cell_props(tail: str, ctx: str, skip: str = "", needle: str = ".mdtbl") -> dict:
        """压在「末段 == tail、且选择器用到的类名都在 ctx 里」的那些规则上，按权重生效。
        ctx = 该元素所在祖先链上的**类名集合** —— 必须写全才能问出"中文期刊（latin 不在场）"：
        只给 'journal' 的话 `.journal.latin` 也算命中，权重更高还压过来，中英就分不开了。
        skip = 选择器含此串的规则不算（问"中间那几行"时要排除 `tr:last-child` 那条底线）。
        needle = 只看含这个片段的规则（默认表格，字号阶梯那组传 '.dc'）。"""
        hit = []
        want = set(ctx.split())
        for raw_sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css_all):
            for part in raw_sel.split(","):
                toks = [t for t in re.split(r"[>\s]+", part.strip()) if t]
                if not toks or not any(needle in t for t in toks):
                    continue
                if re.sub(r"::?[\w-]+(\([^()]*\))?", "", toks[-1]) != tail:
                    continue
                if skip and skip in part:
                    continue
                # 祖先链上的类名要比，但**末段自己的类名不算祖先**：
                # 查 .doc-title 时把 'doc-title' 当作用链要求，就永远匹配不到它自己。
                if not all(c in want for t in toks[:-1]
                           for c in re.findall(r"\.([\w][\w-]*)", t)):
                    continue
                decls = []
                for piece in body.split(";"):
                    if ":" in piece:
                        prop, val = piece.split(":", 1)
                        decls.append((prop.strip().lower(), val.strip()))
                hit.append((css_weight(part), decls))
        out = {}
        for _, decls in sorted(hit, key=lambda x: x[0]):
            for prop, val in decls:
                sides = expand_width(prop, val)
                if sides is None:
                    out[prop] = val
                else:
                    out.update(sides)
        return out

    SIDES = ("top", "right", "bottom", "left")

    def expand_width(prop: str, val: str):
        """只摊 shorthand（border / border-width）成四条边；长写 border-top 等原样留着
        —— 留着整串是因为判据要看**颜色**（`var(--fg)` 才算墨色线），只留宽度就把信息丢了。"""
        p = val.split()
        if not p:
            return None
        if prop == "border-width":
            if len(p) == 1:
                t, r, b, l = p * 4
            elif len(p) == 2:
                t, b, r, l = p[0], p[0], p[1], p[1]
            elif len(p) == 3:
                t, r, b, l = p[0], p[1], p[2], p[1]
            else:
                t, r, b, l = p[:4]
            return {"border-" + s: v for s, v in zip(SIDES, (t, r, b, l))}
        if prop == "border":       # 整串铺满四边：`border:… solid …` 是四边都画，正是要防的
            return {"border-" + s: val for s in SIDES}
        return None

    def ruled(val: str) -> bool:
        """这条边画不画得出线：看第一个 token（宽度）—— 0/none 就没线，
        `0 solid X` 那种"有样式没宽度"也不算线；`var(--hair)` 量不出就按有线算。"""
        head = (val or "").split()[0] if (val or "").strip() else ""
        if head in ("", "none"):
            return False
        if head in ("thin", "medium", "thick"):
            return True
        m = re.match(r"^([\d.]+)(?:px|em|rem|%)?$", head)
        return not (m and float(m.group(1)) == 0)

    CN, EN = "dtext dc journal draw mdtbl", "dtext dc journal latin draw mdtbl"
    tb = []
    th_b = cell_props("th", CN)
    td_b = cell_props("td", CN, skip="last-child")   # 中间那几行（末行底线另算）
    for el, b in (("th", th_b), ("td", td_b)):
        bad_sides = [s for s in ("left", "right") if ruled(b.get("border-" + s, ""))]
        if bad_sides:
            tb.append(f"{el} 有竖线（{'/'.join(bad_sides)}）—— 三线表要求左右都清空，"
                      f"实际 L={b.get('border-left', '-')} R={b.get('border-right', '-')}")
    if ruled(td_b.get("border-top", "")) or ruled(td_b.get("border-bottom", "")):
        tb.append("行间画了线 —— 真表只有三条横线（四篇真论文可数的 10 张表里 8 张无竖线），"
                  f"实际中间行 T={td_b.get('border-top', '-')} B={td_b.get('border-bottom', '-')}")
    if "1.5px" not in th_b.get("border-top", ""):
        tb.append(f"顶线不是加重线（实际生效 {th_b.get('border-top', '-') or '无'} —— "
                  "被基线那条 hair shorthand 顶掉了也不算数）")
    if "var(--fg)" not in th_b.get("border-bottom", ""):
        tb.append(f"表头下那条线不是墨色（实际 {th_b.get('border-bottom', '-') or '无'}）")
    # 第三条：末行底线。同一套权重压一遍、**不**排除 last-child ⇒ 那条 503 的规则赢，
    # 读出来的就是"最后一行下面到底画了什么"（比在 CSS 文本里找一个整串可靠）。
    last_b = cell_props("td", CN)
    if "1.5px" not in last_b.get("border-bottom", "") or "var(--fg)" not in last_b.get("border-bottom", ""):
        tb.append(f"底线不是 1.5px 墨色加重线（实际 {last_b.get('border-bottom', '-') or '无'}）")
    # 表体字号分中英：中文 13.5，英文跟着英文阶梯走（正文 16 的 0.80×）
    if "13.5px" not in cell_props(".mdtbl", CN).get("font-size", ""):
        tb.append("中文期刊表体不再是 13.5px")
    if "12.8px" not in cell_props(".mdtbl", EN).get("font-size", ""):
        tb.append("英文期刊表体没走 0.80× 的 12.8px（缺 .journal.latin 作用域的规则）")
    if tb:
        bad("稿件台表格 · 三线表（只三条横线 · 竖线行间线都被赢家规则清零 · 表体字号分中英）", tb)
    else:
        ok("稿件台表格 · 三线表（只三条横线 · 竖线行间线都被赢家规则清零 · 表体字号分中英）", 6)

    # ── W26F·B：英文阶梯的档位要钉住（值来自三家英文期刊的实测）────────────
    # 为什么值得钉：上一版把题名从 1.69 改成 2.06，起因是第二家 Elsevier 的题名**除错了分母**
    # （拿图注 6.4pt 当正文 8.0pt）—— 数值是量出来的就一定会量错，而量错时没有东西会响。
    # 这一组查的是"生效值"（同一套权重压一遍），卡尺 _desk_ruler_shot.py 再在浏览器里复核渲染。
    # 正文基准 --fs-base 必须一起钉：比值是按它算的，它一动整条阶梯的话就都不算了。
    ld = []
    if "--fs-base:16px" not in re.sub(r"\s+", "", css_all):
        ld.append("--fs-base 不再是 16px（下面每一档的比值都是按它算的）")
    CN_DC, EN_DC = "dtext dc journal", "dtext dc journal latin"
    WANT = (
        (".doc-title", EN_DC, "font-size", "27px", "题名 1.69（ACS 1.70 + Elsevier 两篇 1.68 同带）"),
        ("p.p-front", EN_DC, "font-size", "20.5px", "作者行 1.28（ACS 1.30 · Frontiers 1.22）"),
        ('h3[data-lv="2"]', EN_DC, "font-size", "20px", "一级节标题 1.25（Elsevier 1.25）"),
        ('h3[data-lv="3"]', EN_DC, "font-size", "16px", "二级节标题 1.00（ACS/Elsevier 都是 1.00）"),
        ('h3[data-lv="4"]', EN_DC, "font-size", "14.5px", "三级 0.91（只有 ACS 量到 0.90 斜体）"),
        ('h3[data-lv="4"]', EN_DC, "font-style", "italic", "三级靠斜体分级，不靠放大"),
        (".doc-title", CN_DC, "font-size", "22px", "中文题名 1.38 —— 没有中文期刊样本，不许被英文尺子带跑"),
        # 字族：图表注必须跟正文同族（ACS ArnoPro / Elsevier AdvOT+CharisSIL 三篇都是衬线，
        # 标签是**衬线加粗**不是无衬线；Frontiers 整条无衬线是单家特例）
        ("p.p-cap", CN_DC, "font-family", "var(--font-serif)", "图注表注正文跟正文同族（三家三篇都是衬线）"),
        ("p.p-cap", EN_DC, "font-family", "var(--font-serif)", "同上（英文分支）"),
        (".dlead", CN_DC + " p-cap", "font-family", "var(--font-serif)",
         "图表注的标签只加粗、不换字族（`.dlead` 基线那条的无衬线要在注里被压掉）"),
        # 断字：英文凡是两端对齐的角色都得开 —— 真 ACS 正文 22/517 行（4%）、
        # 文献体 2/105 行（2%，长菌名逼出来的 `Saccharopoly-`）都在断字。
        ("p.p-body", EN_DC, "hyphens", "auto", "英文正文两端对齐必须开断字（ACS 4% 的行在断字）"),
        ("p.p-ref", EN_DC, "hyphens", "auto", "英文参考文献同样两端对齐 —— 不开就是河道"),
    )
    for tail, ctx, prop, want, why in WANT:
        got = cell_props(tail, ctx, needle=".dc").get(prop, "")
        if want not in got:
            ld.append(f"{why}：{tail} 的 {prop} 实际生效 {got or '（没有这条规则）'}，不是 {want}")
    # 中文不许跟着开：中文没有断字符这回事
    for tail in ("p.p-body", "p.p-ref"):
        hv = cell_props(tail, CN_DC, needle=".dc").get("hyphens", "")
        if hv and "auto" in hv:
            ld.append(f"中文 {tail} 被开了断字（hyphens:auto 只属于拉丁稿）")
    if ld:
        bad("稿件台字号 · 字族 · 断字 档位（英文按三家实测 · 中文那把尺子还没找到）", ld)
    else:
        ok("稿件台字号 · 字族 · 断字 档位（英文按三家实测 · 中文那把尺子还没找到）", len(WANT) + 1)

    # ── W3：侧栏可收（用户原话「想隐藏都不行」）─────────────────
    # 这条是**新增的静态接线检查** —— W3 的执行体主动报了缺口：
    # 折叠行为当时只有它自己的一次性脚本兜着，标准套件里一条都不响。
    # ⇒ 下一个人把折叠功能拆了，全套回归仍然全绿。那是 G 型失效（守卫在、不覆盖）。
    # 判据打在**三处独立的接线上**，任一处断掉都报：
    #   ① 控件在（#btnFold）② 状态挂在 #side[data-fold] ③ 状态持久化到 localStorage。
    # 只查其中一处是不够的：把 localStorage 那行删掉，按钮照样在、CSS 照样有，
    # 而"刷新后回到展开"正是用户抱怨的那半句。
    # ── W4：侧栏收起照 DSH 语义（刷新即重置 · 280/56 · <1024 自动收起）──
    # DSH 的布局状态**不持久化**（reload restores the sidebar default），所以
    # 上一版那条「必须有 localStorage 键 rongke-side-fold」判据被**反转**：
    # 现在要证明的是「收起状态不再进 localStorage」—— 键在＝违背 DSH，报。
    # 可注入的承重项有三条，各配一个负控：① 控件在 ② 收窄规则在 ③ 阈值 1024 在。
    fold = []
    if 'id="btnFold"' not in html:
        fold.append("侧栏没有收起控件（id=\"btnFold\"）")
    if ".dataset.fold" not in js and "data-fold" not in js:
        fold.append("收起状态没接线（JS 里既不设 dataset.fold，也没有 data-fold）")
    if not RE_FOLD_CSS.search(html):
        fold.append("没有 [data-fold=\"1\"] 的收窄规则（状态翻了而界面不动）")
    if "rongke-side-fold" in js:
        fold.append("收起状态仍被持久化（localStorage 键 rongke-side-fold）—— DSH 要求刷新即重置")
    if "width:280px" not in html:
        fold.append("侧栏默认宽不是 DSH 的 280px")
    if "width:56px" not in html:
        fold.append("侧栏收起 rail 不是 DSH 的 56px")
    if "innerWidth < 1024" not in js:
        fold.append("自动收起阈值不是 DSH 的 <1024（isNarrow 用 innerWidth < 1024）")
    # ★★ 用户报「侧边栏也打不开啊」的根因就在这里 —— 上面 6 条**全都查不出来**。
    # 窄屏（<=880px）时侧栏展开是**覆盖层抽屉**（fixed + z-index:70），
    # 而 ☰ 在文档流里排在 .side **之后** ⇒ 抽屉一铺开就把 ☰ 压在自己下面，
    # 点下去命中的是抽屉里的元素。表现：侧栏能打开，但**再也关不掉**。
    # 上面 6 条只证明"控件在、规则在、阈值在"，证明不了"这个控件点得到"。
    # 判据：抽屉规则生效时，必须有一条把 ☰ 抬到抽屉之上的规则。
    if "z-index:70" in html or "z-index: 70" in html:
        # 有抽屉层（z-index:70）就必须有抬起 ☰ 的规则
        lift = re.search(
            r"#btnFold\s*\{[^}]*z-index\s*:\s*(\d+)", html)
        if not lift or int(lift.group(1)) <= 70:
            fold.append(
                "窄屏抽屉(z-index:70)会把 #btnFold 压住 —— 侧栏打开后关不掉"
                "（需要一条把 #btnFold 抬到 z-index>70 的规则）")
    if fold:
        bad("侧栏可收 · 照 DSH（控件 · 状态 · 收窄规则 · 刷新重置 · 280/56 · 1024 · 抽屉下☰可点）", fold)
    else:
        ok("侧栏可收 · 照 DSH（控件 · 状态 · 收窄规则 · 刷新重置 · 280/56 · 1024 · 抽屉下☰可点）", 7)

    # ── W4：右栏轨道几何照 DSH（首开 45% · 上限 70% · 中间保底 400 · 关闭不渲染把手）──
    # 事实清单第 62–64 行：右栏「首次开在视口 45%，此后记住像素值，上限 70%」；
    # 「保住中间 400px：先把右栏压到 300px，再报'空间不够'让它自己关」；「右把手在关闭/全屏时不渲染」。
    # 这四条是**几何承重**，写在 deskCapW/deskFirstW/deskRoomOk + 一条 CSS 里。
    # 只查 CSS/只查 JS 都不够：把 45% 悄悄改成 30%，页面照样能开、把手照样能拖 —— 数值漂了没人响。
    dg = []
    if "innerWidth * 0.45" not in js:
        dg.append("右栏首开宽度不再是 DSH 的视口 45%（deskFirstW 缺 window.innerWidth * 0.45）")
    if "vw * 0.70" not in js:
        dg.append("右栏上限不再是 70% 视口（deskCapW 缺 vw * 0.70）")
    if "deskSideW() - 400" not in js:
        dg.append("右栏不再给中间保底 400px（deskCapW 缺 deskSideW() - 400）")
    if "deskCapW() >= 300" not in js:
        dg.append("右栏少了'装不下就不开'的判据（deskRoomOk 缺 deskCapW() >= 300）")
    if ".desk[data-open=\"0\"].dghost{display:none}" not in re.sub(r"\s+", "", html):
        dg.append("右把手在关闭态没被摘掉（缺 .desk[data-open=\"0\"] .dghost{display:none}）")
    if dg:
        bad("右栏几何 · 照 DSH（45% 首开 · 70% 上限 · 中间保底 400 · 塞不下不开 · 关闭无把手）", dg)
    else:
        ok("右栏几何 · 照 DSH（45% 首开 · 70% 上限 · 中间保底 400 · 塞不下不开 · 关闭无把手）", 5)

    # ── W3：设置页接线（用户原话「连设置都没有，换模型都不行」）─────
    # 同上：新页落地而套件不覆盖 = 下次被拆没人知道。
    st = []
    if 'id="v-set"' not in html:
        st.append("设置页区块缺失（id=\"v-set\"）")
    if 'data-v="set"' not in html:
        st.append("导航里没有「设置」项（data-v=\"set\"）")
    if "/api/settings" not in js:
        st.append("设置页没接 /api/settings（读了也是死的）")
    if st:
        bad("设置页接线", st)
    else:
        ok("设置页接线（区块 · 导航项 · /api/settings）", 3)

    # ── W4：设置页信息架构照 DSH（分节卡片：通用 / 模型 / 配置层级 · 通用不许空壳）──
    # 本轮只重排信息架构：三个分节标题必须在，且「通用」那节得有**真控件**
    # （外观/过程默认走现成的 setTheme / PROCOPEN），否则"分了三节"只是三张空标题。
    si = []
    if ">通用<span" not in js:
        si.append("设置页缺「通用」分节（DSH 信息架构第一节）")
    if ">模型与服务端<span" not in js:
        si.append("设置页缺「模型与服务端」分节")
    if ">配置从哪一层来" not in js and "配置从哪一层来" not in js:
        si.append("设置页缺「配置从哪一层来」分节")
    if 'id="setTheme"' not in js or "setTheme(v, true)" not in js:
        si.append("「通用」节缺外观控件或没接线（setTheme）—— 分节成了空壳")
    if 'id="setModel"' not in js:
        si.append("「模型」节的模型名字段丢了（W3 功能不许丢）")
    if si:
        bad("设置页信息架构 · 照 DSH（通用 / 模型 / 配置层级 · 通用非空壳）", si)
    else:
        ok("设置页信息架构 · 照 DSH（通用 / 模型 / 配置层级 · 通用非空壳）", 5)

    # ── W14-B：DSH 原子件层（B0–B11 逐件判据；每件负控在 --self-test）──
    # 判据打法：**按选择器抽出规则体再查真值数字**，不是全文 find("36px") ——
    # 全文匹配会串件：Switch 的 36 没了、Button 里还有一句 36px，判据照样绿，
    # 那就是恒真判据。工单第 3 节的话：没有负控的判据等于没判据。
    afail: list[str] = []
    apass: list[str] = []

    def rule_body(sel: str) -> str:
        sel = sel.rstrip("{")          # 调用点习惯写成 ".btn{"，这里统一剥掉
        # ★ 行首锚定：不带 ^ 时 ".sp-arc{" 会先命中组选择器 ".sp-track,.sp-arc{"
        #   的尾巴，抽出来的规则体就不是那条规则的（实测红在 B8 呼吸周期上）。
        m = re.search(r"(?m)^" + re.escape(sel) + r"\s*\{([^}]*)\}", html)
        return m.group(1) if m else ""

    def need(label: str, sel: str, *frags: str) -> None:
        body = rule_body(sel)
        if not body:
            afail.append(f"{label}：找不到规则 {sel}{{…}}（件被拆了？）")
            return
        miss = [f for f in frags if f not in body]
        if miss:
            afail.append(f"{label}：{sel} 里缺 {' · '.join(miss)}")
        else:
            apass.append(label)

    # ── B0 别名层：工单映射表逐条在 :root，右侧只指荣珂原有变量，层内零 hex ──
    WANT_ALIAS = {
        "--label-primary": "var(--fg)", "--label-secondary": "var(--fg-2)",
        "--label-tertiary": "var(--fg-3)", "--label-primary-foreground": "var(--on-ink)",
        "--label-dimmed": "var(--b-400)", "--border-l2": "var(--line-2)",
        "--border-l3": "var(--line-3)", "--border-l4": "var(--line-4)",
        "--bg-layer-1": "var(--bg)", "--bg-layer-2": "var(--bg-platform)",
        "--bg-layer-3": "var(--bg)", "--bg-layer-4": "var(--bg-overlay)",
        "--bg-module-platform": "var(--bg-platform)", "--interactive-bg-hover": "var(--hover)",
        "--interactive-bg-active": "var(--active)", "--state-success-primary": "var(--ok)",
        "--state-warn-primary": "var(--warn)", "--state-error-primary": "var(--err)",
        "--state-business-primary": "var(--biz)", "--state-idle-primary": "var(--b-400)",
        # 2026-09-29 设计对齐 ZCode（zai-org/ZCode，Apache-2.0）：primary 从品牌蓝
        # 改为黑白镜像的 --ink（明 #0d0d0d/白字，暗 #f8f8f8/黑字）；蓝只留给链接与图标。
        "--brand-primary": "var(--ink)", "--markdown-code-block": "var(--bg-code)",
        "--focus-ring-width": "2px", "--focus-ring-color": "var(--biz)",
    }
    m = re.search(r"W14-B·alias-begin(.*?)W14-B·alias-end", html, re.S)
    if not m:
        afail.append("B0：别名层标记丢了（index.html 里必须保留 W14-B·alias-begin/end 这一对注释）")
    else:
        block = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
        base = set(RE_VAR_DEF.findall(
            re.sub(r"W14-B·alias-begin.*?W14-B·alias-end", "", html, flags=re.S)))
        defs = {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)}
        for name, rhs in WANT_ALIAS.items():
            got = defs.get(name)
            if got is None:
                afail.append(f"B0：工单映射表的 {name} 不在别名层里")
            elif got.replace(" ", "") != rhs.replace(" ", ""):
                afail.append(f"B0：{name} 指向漂了（期望 {rhs} 实际 {got}）")
        for name, rhs in defs.items():
            if "#" in rhs:
                afail.append(f"B0：别名 {name} 右侧出现 hex —— 映射表只许指荣珂已有变量，不许新造原始色")
            for ref in re.findall(r"var\(\s*(--[\w-]+)", rhs):
                if ref not in base:
                    afail.append(f"B0：别名 {name} 指向 {ref}，它不是荣珂原有变量（不在标记层之外定义）")
        if not afail:
            apass.append(f"B0：别名层 {len(defs)} 条（工单 24 条逐条核过，右侧零 hex）")

    # ── B1–B11 逐件 ──
    need("B1 Button 基类", ".btn{", "display:inline-flex", "justify-content:center",
         "gap:4px", "border:none", "border-radius:var(--r-md)", "font-size:14px",
         "line-height:22px", "padding:0 14px")
    need("B1 Button md=36", ".btn.md{", "height:36px")
    need("B1 Button sm=28", ".btn.sm{", "height:28px", "font-size:12px", "line-height:18px",
         "padding:0 10px", "border-radius:var(--r-sm)")
    need("B1 Button primary 品牌底+反色", ".btn.primary{", "background:var(--brand-primary)",
         "color:var(--label-primary-foreground)")
    need("B1 Button ghost hover", ".btn.ghost:hover:not(:disabled){",
         "background:var(--interactive-bg-hover)")
    need("B1 Button outline", ".btn.outline{", "border:.5px solid var(--border-l3)",
         "background:transparent")
    need("B1 Button disabled=0.4", ".btn:disabled{", "opacity:.4", "cursor:not-allowed")
    need("B1 Button icon 槽 16×16", ".btn .i16{", "width:16px", "height:16px",
         "inline-flex", "justify-content:center")

    need("B2 Input 外壳 32/.5px l4", ".ipt{", "gap:6px", "height:32px", "padding:0 8px",
         "border:.5px solid var(--border-l4)", "border-radius:var(--r-md)",
         "background:var(--bg-layer-1)")
    need("B2 Input focus-within 变品牌色", ".ipt:focus-within{", "border-color:var(--biz)")
    need("B2 Input 内壳 14/22", ".ipt input{", "flex:1", "min-width:0", "border:none",
         "outline:none", "background:transparent", "font-size:14px", "line-height:22px",
         "color:var(--label-primary)")
    need("B2 Input placeholder", ".ipt input::placeholder{", "color:var(--label-dimmed)")

    need("B3 Switch 轨道 36×20 pad2", ".switch{", "width:36px", "height:20px", "padding:2px",
         "border:0", "border-radius:999px", "corner-shape:round", "background:var(--border-l3)")
    need("B3 Switch 由 aria-checked 驱动", ".switch[aria-checked='true']{",
         "background:var(--brand-primary)")
    need("B3 Switch 滑块 16×16 120ms", ".switch .thumb{", "width:16px", "height:16px",
         "border-radius:50%", "corner-shape:round",
         "background:var(--label-primary-foreground)", "transition:transform 120ms ease")
    need("B3 Switch 位移 16px", ".switch[aria-checked='true'] .thumb{", "transform:translateX(16px)")
    need("B3 Switch disabled=.5", ".switch:disabled{", "opacity:.5")
    need("B3 Switch 焦点环走令牌", ".switch:focus-visible{",
         "outline:var(--focus-ring-width) solid var(--focus-ring-color)", "outline-offset:2px")
    if re.search(r"\.switch\.(on|active|checked)\b", html):
        afail.append("B3：Switch 出现了平行状态 class —— DSH 的设计意图是外观只键在 aria-checked")

    need("B4 Checkbox 基类", ".checkbox{", "gap:6px", "font-size:14px", "line-height:20px",
         "color:var(--label-primary)")
    need("B4 Checkbox 原生 input 16×16 品牌色", ".checkbox input{", "width:16px", "height:16px",
         "accent-color:var(--brand-primary)")
    need("B4 Checkbox 禁用 .5", ".checkbox:has(input:disabled){", "opacity:.5")

    need("B5 Segmented 轨道 gap2/pad4 + 容器挂 --seg-n/--seg-i", ".segctl{",
         "grid-auto-flow:column", "grid-auto-columns:1fr", "gap:2px", "padding:4px",
         "border-radius:var(--r-md)", "background:var(--interactive-bg-hover)",
         "--seg-n:2", "--seg-i:0")
    need("B5 Segmented 指示器算术定位（不量 DOM）", ".segctl .segind{", "top:4px", "left:4px",
         "width:calc((100% - 8px - 2px * (var(--seg-n) - 1)) / var(--seg-n))",
         "height:calc(100% - 8px)", "border:0", "border-radius:var(--r-sm)",
         "background:var(--bg-layer-1)", "box-shadow:var(--elevation-soft)",
         "transform:translateX(calc(var(--seg-i) * (100% + 2px)))",
         "transition:transform 160ms ease", "pointer-events:none")
    need("B5 Segmented 段 28/16 13|20 w500", ".segctl .seg{", "height:28px", "padding:0 16px",
         "border:0", "border-radius:var(--r-sm)", "background:transparent",
         "color:var(--label-secondary)", "font-size:13px", "line-height:20px", "font-weight:500")
    need("B5 Segmented 选中/hover 提亮",
         ".segctl .seg:hover:not(:disabled),.segctl .seg[aria-selected='true']{",
         "color:var(--label-primary)")
    if "@media (prefers-reduced-motion:reduce){.segctl .segind,.segctl .seg{transition:none}" not in html:
        afail.append("B5：SegmentedControl 少了 prefers-reduced-motion 分支（transition 不许在减弱动效下还跑）")

    need("B6 Tag 胶囊几何定死", ".tag{", "border-radius:999px", "corner-shape:round",
         "padding:1px 8px", "font-size:11px", "line-height:17px", "font-weight:500",
         "white-space:nowrap")
    need("B6 Tag outline", ".tag[data-tone='outline']{", "border:.5px solid var(--border-l4)",
         "color:var(--label-tertiary)")
    need("B6 Tag solid", ".tag[data-tone='solid']{", "background:var(--label-primary)",
         "color:var(--bg-layer-3)")
    need("B6 Tag neutral", ".tag[data-tone='neutral']{", "background:var(--bg-module-platform)",
         "color:var(--label-secondary)")
    need("B6 Tag quiet", ".tag[data-tone='quiet']{", "color:var(--label-tertiary)")
    need("B6 Tag success=10%", ".tag[data-tone='success']{",
         "background:color-mix(in srgb,var(--ok) 10%,transparent)", "color:var(--ok)")
    need("B6 Tag info=10%", ".tag[data-tone='info']{",
         "background:color-mix(in srgb,var(--biz) 10%,transparent)", "color:var(--biz)")
    need("B6 Tag warning=12%（DSH 注释：故意的，别统一）", ".tag[data-tone='warning']{",
         "background:color-mix(in srgb,var(--warn) 12%,transparent)", "color:var(--warn)")
    need("B6 Tag danger=10%", ".tag[data-tone='danger']{",
         "background:color-mix(in srgb,var(--err) 10%,transparent)", "color:var(--err)")

    need("B7 Pill 高 24 胶囊", ".apill{", "gap:4px", "height:24px", "padding:0 8px",
         "border:none", "border-radius:999px", "corner-shape:round", "font-size:12px",
         "line-height:18px", "color:var(--label-secondary)", "background:var(--bg-layer-2)")
    need("B7 Pill interactive hover", ".apill.int:hover{", "background:var(--interactive-bg-hover)")
    _act = rule_body(".apill.act")
    if not _act:
        afail.append("B7 Pill active：找不到 .apill.act 规则")
    elif "box-shadow:inset 0 0 0 1px" not in _act or "border:" in _act:
        afail.append("B7 Pill active：描边必须由 inset box-shadow 画（DSH 真值 inset 0 0 0 1px，不许真 border）")
    else:
        apass.append("B7 Pill active（inset 描边）")

    need("B8 StateDot 槽", ".dot{", "position:relative", "display:inline-block", "flex:none")
    need("B8 StateDot 芯 inset 20%（10px 槽画 6px 芯）", ".dot::after{", "inset:20%",
         "border-radius:50%", "corner-shape:round", "background:currentColor")
    need("B8 StateDot done→ok", ".dot[data-state='done']{", "color:var(--state-success-primary)")
    need("B8 StateDot warning→warn", ".dot[data-state='warning']{", "color:var(--state-warn-primary)")
    need("B8 StateDot error→err", ".dot[data-state='error']{", "color:var(--state-error-primary)")
    need("B8 StateDot idle→b-400", ".dot[data-state='idle']{", "color:var(--state-idle-primary)")
    need("B8 StateDot 转 1.5s linear", ".sp-motion{", "transform-origin:center",
         "animation:dot-spin 1.5s linear infinite")
    need("B8 StateDot 环/弧共 stroke 真值", ".sp-track,.sp-arc{", "fill:none",
         "stroke:currentColor", "stroke-width:2", "stroke-linecap:round")
    need("B8 StateDot track .25", ".sp-track{", "opacity:.25")
    need("B8 StateDot 呼吸 1.5s（与转同周期，别改）", ".sp-arc{",
         "stroke-dasharray:12 150", "animation:dot-dash 1.5s ease-in-out infinite")
    if "@keyframes dot-spin{to{transform:rotate(360deg)}}" not in html:
        afail.append("B8：dot-spin keyframes 丢了或被改（真值 to{rotate(360deg)}）")
    if "50%{stroke-dasharray:24 150;stroke-dashoffset:-6}" not in html:
        afail.append("B8：dot-dash keyframes 50% 帧不是 DSH 的 24 150 / -6")
    if ("@media (prefers-reduced-motion:reduce){.sp-motion,.sp-arc{animation:none}"
            ".sp-arc{stroke-dasharray:18 150;stroke-dashoffset:-3}}") not in html:
        afail.append("B8：StateDot 的 reduced-motion 分支缺（静态弧应停在 18 150 / -3）")

    need("B9 DisclosureRow 行 24", ".disclosure .row{", "position:relative", "overflow:hidden",
         "height:24px")
    need("B9 DisclosureRow 指头槽 16 / gap 6", ".disclosure .leading{", "flex:none",
         "width:16px", "height:16px", "margin-right:6px", "border:none", "background:none",
         "color:var(--label-tertiary)")
    need("B9 DisclosureRow 槽内 svg 14（StateDot 除外）", ".disclosure .leading svg:not([data-state]){",
         "width:14px", "height:14px")
    need("B9 DisclosureRow 静图标", ".disclosure .iconIdle{", "opacity:1", "transition:opacity 100ms ease")
    need("B9 DisclosureRow 悬停图标", ".disclosure .chevronHover{", "inset:0", "margin:auto",
         "opacity:0", "transition:opacity 100ms ease")
    need("B9 DisclosureRow 交叉淡出（100ms 交换）", ".disclosure .row:hover .iconIdle{", "opacity:0")
    need("B9 DisclosureRow 交叉淡入", ".disclosure .row:hover .chevronHover{", "opacity:1")
    need("B9 DisclosureRow 标题 13/24", ".disclosure .title{", "flex:none", "font-size:13px",
         "line-height:24px", "color:var(--label-secondary)")

    need("B10 Tooltip 气泡", ".tooltip{", "position:fixed", "z-index:100", "width:max-content",
         "max-width:50vw", "padding:3px 7px", "border-radius:var(--r-sm)",
         "background:var(--tooltip-bg)", "font-size:13px", "line-height:20px",
         "pointer-events:none", "animation:tip-in 150ms ease-in-out")
    need("B10 Menu 卡片立面（stroke 走 l1 + prominent，不用实线边框）", ".menu .mlist,.menu .msub{",
         "padding:4px", "border:0", "--elevation-stroke-color:var(--line-1)",
         "box-shadow:var(--elevation-prominent)")
    need("B10 Menu 定位与宽度", ".menu .mlist{", "top:calc(100% + 4px)", "left:0",
         "z-index:100", "min-width:144px", "max-width:360px")
    need("B10 Menu 行几何", ".menu .mitem{", "min-height:34px", "padding:6px 8px",
         "border-radius:var(--r-md)", "font-size:13px", "line-height:20px")
    need("B10 Menu 分隔线 l2 发丝", ".menu .msep{", "height:.5px", "margin:3px 2px",
         "background:var(--border-l2)")
    need("B10 MenuSurface 毛玻璃只滤 material 层", ".msurf .material{", "inset:0", "z-index:-1",
         "border-radius:inherit", "background:var(--menu-fill)",
         "backdrop-filter:blur(40px) saturate(150%)", "pointer-events:none")
    need("B10 Modal 卡", ".modal .dialog{", "gap:20px", "width:min(380px,100%)",
         "padding:0 0 24px", "border:0", "border-radius:var(--r-panel)",
         "background:var(--bg-layer-2)", "box-shadow:var(--elevation-prominent)")
    need("B10 Modal 遮罩", ".modal .mask::after{", "background:var(--mask-fill)")
    need("B10 Toast", ".toast{", "top:40px", "left:50%", "z-index:1100", "pointer-events:none",
         "gap:10px", "max-width:min(640px,calc(100vw - 48px))", "padding:12px 16px",
         "border-radius:var(--r-lg)", "background:var(--toast-bg)", "font-size:14px",
         "line-height:22px", "--toast-hold:3000ms")

    for _sel in (".menu .mlist,.menu .msub", ".msurf .material", ".modal .dialog", ".tooltip", ".toast"):
        _body = rule_body(_sel)
        if _body and "solid" in _body:
            afail.append(f"B10：{_sel} 的立面用了实线（规矩是 --elevation-*/shadow 画边，不是 border:solid）")

    _fv = re.search(r"^:focus-visible\s*\{([^}]*)\}", html, re.M)
    if not _fv:
        afail.append("B11：全局 :focus-visible 规则没了")
    elif "outline:var(--focus-ring-width) solid var(--focus-ring-color)" not in _fv.group(1):
        afail.append("B11：:focus-visible 没统一走 --focus-ring-width/--focus-ring-color（还硬编码？）")
    else:
        apass.append("B11 全站焦点环走令牌")
    if "html[data-input-modality='pointer'] :focus-visible:not(:read-write){outline-color:transparent}" not in html:
        afail.append("B11：指针模态的透明环规则没了（DSH 规矩：透明的是环，不是立面）")
    for mm in re.finditer(r"([^{}]+)\{([^}]*)\}", html):
        if ":focus-visible" in mm.group(1) and "box-shadow:none" in mm.group(2):
            afail.append("B11：有 :focus-visible 规则在清 box-shadow —— 指针模态下塌的是立面，不许")
            break

    if afail:
        bad("W14-B 原子件层 · 照 DSH（B0 别名层 · B1–B11 逐件真值）", afail)
    else:
        ok("W14-B 原子件层（别名层 · Button · Input · Switch · Checkbox · Segmented · Tag · Pill · StateDot · DisclosureRow · 弹出 5 件 · 焦点环）", len(apass))

    # ── W14-E：接线落地判据（E5）—— 「CSS 里有」≠「页面上有」──────────────
    # 这一节存在的理由就是 W14-B 的事故：那一单 9/9 判据全绿、13 个负控全打穿，
    # 而**页面上一个原子件都没用上**（主控从真浏览器量到 DOM 实例全 0）。
    # 只查"CSS 里有没有定义"的判据对这种工单会一路绿灯。所以 E5 数的是
    # **标记里的 class 引用**，口径与主控的 w14_wiring_check.py 一致：
    # 去掉 <style> 块，在剩下的 HTML 标记 + JS 字符串里数 class="…"。
    rest_markup = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S)

    def markup_refs(cls: str) -> int:
        return len(re.findall(r'class="[^"]*(?<![\w-])' + re.escape(cls) + r'(?![\w-])', rest_markup))

    def css_defs(cls: str) -> int:
        # 只在 <style> 里数：拿全文数会被 JS 字符串里的 `.toast` 之类混过去。
        return len(re.findall(r"\." + re.escape(cls) + r"(?=[\s,{:>.\[])", "\n".join(styles)))

    WIRED_E = {
        "segctl": "E1 设置页外观主题（#setTheme 走分段控件）",
        "switch": "E1 设置页过程默认展开（#setProc 走开关）",
        "ipt": "E3 过滤输入的外壳（#toolsFilt / #kbQ）",
        "tag": "E3 工具与知识库列表的来源标签",
        "apill": "E2 成果页 8 个场景胶囊",
        "disclosure": "E2 过程折叠行（#procWrap）",
        "toast": "E4 复制反馈",
        "btn": "W14-C 已接（按钮）",
        "dot": "W14-C 已接（状态点）",
        "card": "W14-A 起就在用（卡片）",
        # W25：W23 的「让 AI 找文献」候选表带来了页面第一个真勾选框，
        # 当时 ui_check 属禁改文件，勾选框只能借色不挂类（index.html 里留了话）。
        # 本刀兑现：候选表勾选框挂上 `.checkbox` 原子件，从备用皮转在册。
        "checkbox": "W25 候选表勾选框（AI 找文献的 pick 表）接上",
    }
    # 主控在 E4 亲口留下的"备用皮"：不许为了消零硬造触发器（那会引入新行为，
    # 违反 §1.1）。这份白名单是**带理由的例外**，不是漏网：白名单外的件一律算红。
    BACKUP_E = {
        "tooltip": "DSH 的 .tooltip 是 position:fixed 浮层、要 JS 定位；现成悬浮提示是原生 title",
        "menu": "页面没有自定义下拉（唯一的下拉是原生 <select id=\"deskRun\">）",
        "msurf": ".menu 的毛玻璃壳，没有菜单可穿",
        "modal": "页面没有遮罩式对话框；#dropmask 是拖放提示、有自己的显隐判据",
    }
    ATOMS_E = list(WIRED_E) + list(BACKUP_E)

    efail: list[str] = []
    epass: list[str] = []
    for cls, why in WIRED_E.items():
        n = markup_refs(cls)
        if n == 0:
            efail.append(f"{cls} 在标记里 0 处引用 —— 皮被拆了（{why}）")
        elif css_defs(cls) == 0:
            efail.append(f"{cls} 标记里用了 {n} 处但 CSS 没定义（穿了件不存在的衣服）")
        else:
            epass.append(f"{cls}×{n}")
    dangling = [c for c in ATOMS_E if markup_refs(c) == 0]
    unannounced = [c for c in dangling if c not in BACKUP_E]
    if unannounced:
        efail.append("悬空且没写理由的原子件：" + "、".join(unannounced)
                     + "（要么接上，要么进备用皮白名单并写明为什么不接）")
    for c in dangling:
        if c in BACKUP_E:
            epass.append(f"{c} 备用皮：{BACKUP_E[c]}")
    # ★ 白名单不许只进不出：备用皮一旦真被接上，就得从这份名单里删掉，
    #   否则"有理由的例外"会变成永久的免检区。
    stale_backup = [c for c in BACKUP_E if markup_refs(c) > 0]
    if stale_backup:
        efail.append("白名单里的件其实已经接上了，把它从 BACKUP_E 删掉："
                     + "、".join(f"{c}×{markup_refs(c)}" for c in stale_backup))
    if efail:
        bad("W14-E 接线落地（E5：标记里真有 · 无未申报悬空 · 备用皮不过期）", efail)
    else:
        ok("W14-E 接线落地（" + " · ".join(epass) + f"，共 {len(ATOMS_E)} 件在册）", len(epass))


    if node is None:
        warns.append("找不到 node，跳过稿件台行为检查（建议装 node 后重跑）")
        print("  [SKIP] 稿件台行为（本机无 node）")
    else:
        # 同上：落 `SELF_TMP`（仓库内）、同名覆盖、不删。
        tmp = SELF_TMP / "probe_desk.js"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(PROBE_DESK_JS, encoding="utf-8")
        desk_probe = SELF_TMP / 'desk_probe_source.js'
        reader = p.parent / 'static' / 'reader.js'
        common = re.search(r'const esc =.*?^/\*', scripts[1] if len(scripts) > 1 else '', re.S | re.M)
        # 与稿件台探针一致：直接读真实 esc 到分号，不能猜一份简化实现。
        prefix = ''
        main = p.parent / 'static' / 'workspace.js'
        main_text = main.read_text(encoding='utf-8') if main.is_file() else ''
        esc_start = main_text.find('const esc = s => String(s == null')
        esc_end = main_text.find('\n\n', esc_start)
        if esc_start >= 0 and esc_end >= 0: prefix = main_text[esc_start:esc_end + 1]
        if not prefix: raise RuntimeError('找不到真实 esc 函数')
        main = p.parent / 'static' / 'workspace.js'
        md = re.search(r'^function mdInline\(s\) \{.*?^\}', main.read_text(encoding='utf-8') if main.is_file() else '', re.S | re.M)
        common_deps = re.findall(r'^function (?:canReplay\(r\)|deskMissText\(rid, raw\)) \{.*?^\}', main.read_text(encoding='utf-8') if main.is_file() else '', re.S | re.M)
        # 非贪婪匹配遇函数内 switch 的闭合括号会截断；补齐真实依赖链。
        common_deps = []
        for mark in ('function canReplay(r) {', 'function deskMissText(rid, raw) {'):
            start = main.read_text(encoding='utf-8').find(mark) if main.is_file() else -1
            end = main.read_text(encoding='utf-8').find('\nfunction ', start + len(mark)) if start >= 0 else -1
            if start >= 0: common_deps.append(main.read_text(encoding='utf-8')[start:end if end >= 0 else None])
        desk_probe.write_text(prefix + '\n' + (md.group(0) if md else '') + '\n' + '\n'.join(common_deps) + '\n' + (reader.read_text(encoding='utf-8') if reader.is_file() else scripts[-1]), encoding='utf-8')
        r = subprocess.run([node, str(tmp), str(desk_probe)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 2:
            last = (out.strip().splitlines() or ["未知错误"])[-1]
            bad("稿件台行为", [f"源码抽取失败：{last}"])
        elif r.returncode != 0:
            detail = [ln for ln in out.splitlines() if "[FAIL]" in ln or "rror" in ln]
            bad("稿件台行为", detail[:8] or ["node 退出码非 0"])
        else:
            ok("稿件台行为（抽取真实源码执行）", out.count("[PASS]"))

    # ── W18：主视图别说行话 ────────────────────────────────────────────
    # ① TOOLHUMAN 必须覆盖**内核真正注册的每一个**工具名。
    #    判据从产出方机械导出（内核的 /api/tools），**不手抄** ——
    #    实测改前 56 个里 32 个没覆盖，用户在主视图上看到的是
    #    `deck_from_template`、`set_slide_notes` 这种裸标识符。
    #    另：表里写着 export_document 而内核注册的是 export_docx ——
    #    过时名字留在表里，那条永远命中不了。
    # 工具名清单的来源：/api/tools（内核自己报的注册表）。
    # ⚠️ 两条我踩过的坑：
    #   ① 第一版在"取不到"时**判失败** ⇒ 服务没起时 ui_check 就红，
    #      而 ui_check 本来是不需要起服务的静态检查 —— 等于把一个离线检查
    #      绑死在"服务开着"上（regress 里当场红了一次）。
    #   ② 第二版想从 `rongke/tools.py` 的 `@tool("…")` 装饰器离线抽，
    #      实测该仓库**没有这个装饰器**、工具名也不在 rongke/ 里
    #      （它们由 workbench 侧提供）⇒ 那个兜底会静默抽到 0 个，
    #      然后"0 个未覆盖"照样绿 —— 比不检查更坏。
    # 正确处置：**取不到就明说跳过**（warn），既不放假绿也不算失败。
    real_tools = None
    try:
        import json as _j
        import urllib.request as _u
        base = os.environ.get("RONGKE_WEB", "http://127.0.0.1:8821")
        with _u.urlopen(base.rstrip("/") + "/api/tools", timeout=3) as _r:
            _d = _j.load(_r)
        real_tools = [x["name"] if isinstance(x, dict) else x
                      for x in _d.get("tools", _d)]
    except Exception:
        real_tools = None
        warns.append("W18：没连上 /api/tools —— 工具名覆盖这一项**本次没测**"
                     "（不是通过）。起服务后重跑 ui_check 会测。")

    m_th = re.search(r"const TOOLHUMAN = \{(.*?)\n\};", html, re.S)
    if not m_th:
        bad("W18 工具名覆盖", ["找不到 TOOLHUMAN 映射表"])
    else:
        covered = set(re.findall(r"(\w+)\s*:", m_th.group(1)))
        # ★ 2026-11-04 插件化预设：能力注册的工具**只在本次 run 存在**，
        #   中文名放在 PRESET_TOOLHUMAN（与主表分开，主表的 stale 分母仍是
        #   开机常驻注册表）。W18 判的是「用户看不看得懂」，两张表都得算覆盖：
        #   否则预设一跑，/api/tools 多出 writing_metrics，就会报一条假的
        #   「没有中文名」；反过来把它塞进主表，空闲时又会被判成旧名。
        m_pt = re.search(r"const PRESET_TOOLHUMAN = \{(.*?)\};", html, re.S)
        preset_covered = (set(re.findall(r"(\w+)\s*:", m_pt.group(1)))
                          if m_pt else set())
        covered |= preset_covered
        w18 = []
        # ① a 表里不许有内核已经不认的旧名字（预设工具确实会注册，不算旧名）
        known = set(real_tools or []) | preset_covered
        stale = sorted(covered - known) if real_tools else []
        if stale:
            w18.append("TOOLHUMAN 里有内核没注册的旧名（永远命中不了）："
                       + "、".join(stale))
        # ① b 内核注册的每个名字都要有人话
        if real_tools:
            miss = sorted(set(real_tools) - covered)
            if miss:
                w18.append(f"{len(miss)}/{len(real_tools)} 个工具没有中文名，"
                           f"主视图会显示裸英文标识符：" + "、".join(miss[:10])
                           + ("…" if len(miss) > 10 else ""))
        if w18:
            bad("W18 工具名覆盖", w18)
        elif real_tools:
            ok(f"W18 工具名全覆盖（{len(real_tools)} 个）")
        else:
            # 没拿到真实清单 ⇒ 既没通过也没失败，只留上面那条 warn。
            # 这里**绝不能**打 PASS —— 那会把"没测"说成"绿了"。
            pass

    # ② 那一行渲染出来不许含原始串（DOI / URL / 绝对路径）。
    #    只扫函数**体内**，不扫注释 —— 注释里我写明了自己在防什么，
    #    直接全文搜关键词会把注释当成命中（这个坑上一轮踩过）。
    m_ts = re.search(r"function toolSummary\(t\) \{(.*?)\n\}", html, re.S)
    if not m_ts:
        bad("W18 主视图按类型改写", ["找不到 toolSummary()"])
    else:
        body_ts = m_ts.group(1)
        need = ["baseName", "argKind", "humanArgKey"]
        lack = [n for n in need if n not in body_ts]
        if lack:
            bad("W18 主视图按类型改写",
                ["toolSummary() 没有按类型改写，缺：" + "、".join(lack)
                 + " —— 路径/DOI/URL 会原样摊在主视图上"])
        else:
            ok("W18 主视图按类型改写（路径取文件名 / DOI·URL 换描述 / 键名说人话）")

    print()
    if fails:
        print(f"有问题 ✘   失败 {len(fails)} 项")
        return 1
    if warns:
        for w in warns:
            print(f"⚠ {w}")
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
