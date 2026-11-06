const DESK_KEY = "rongke-desk";
/* 能走「原页」的原件类型 —— PDF 直渲，Office 文档先转 PDF。
   与 rongke/web/orig.py 的 _DIRECT_PDF | _VIA_PDF 同一份清单：
   前端拿它决定"这个入口该不该出现/该走哪条路"，服务端拿它决定"收不收"。
   两侧都必须有（前端不许拿它去断言后端一定成功，后端也不许因为前端有就放宽）。
   定义在这里（顶层常量区）是因为先声明的先能用：deskIsOrigDoc 在它之前的
   函数体里被引用，放在后面会踩 TDZ。 */
const ORIG_DOC_EXT = [".pdf", ".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp", ".ods"];
let DESK = { text: "", title: "交付正文", meta: "", runId: "", blocks: [], nodes: [], hasH: false, hits: [], hit: -1, kw: "", figs: null, posRestore: 0, docAbs: "", docName: "" };
let LAST_RESULT = null;   // 最近一次成果 —— 侧栏的「稿件台」按钮随时能打开它

/* 标题识别：中文稿的标题不止 markdown 一种长相 */
function deskTocOf(line) {
  let m;
  if ((m = /^(#{1,5})\s+(.+?)\s*#*$/.exec(line))) return { lv: Math.min(m[1].length, 4), t: m[2].trim() };
  if ((m = /^【(.{1,28})】\s*$/.exec(line))) return { lv: 1, t: m[1] };
  if (line.length <= 40 && /^第[一二三四五六七八九十百]+[章节部编]/.test(line)) return { lv: 1, t: line };
  if (line.length <= 40 && /^[一二三四五六七八九十]{1,3}\s*[、.．]\s*\S/.test(line)) return { lv: 1, t: line };
  /* 数字编号**分两式**写，为的是把「1.2.3 数据处理」（标题）和
     「2021 年的实验显示…」（正文）分开 —— 用 (?:\.\d+){0,3} 配一个宽松分隔符时，
     贪婪回溯会把 "1.2.3" 截成 "1.2"，层级少一级、大纲缩进就平了。
     这个 bug 是探针第一次跑就抓出来的，见 ui_check.py 的 PROBE_DESK_JS。 */
  if (line.length <= 50 && (m = /^(\d+(?:\.\d+){1,4})[\s、.．]/.exec(line)))
    return { lv: Math.min(m[1].split(".").length, 4), t: line };   // 带点的多级号：1.2 / 1.2.3
  if (line.length <= 50 && /^\d{1,3}\s*[、.．]\s*\S/.test(line))
    return { lv: 1, t: line };                                     // 裸序号：1. / 2、 / 3．
  return null;
}
function deskSentences(t) { return t.match(/[^。！？!?；;\n]+[。！？!?；;]?/g) || [t]; }
function deskChunks(t, per) {
  const ss = deskSentences(t), out = [];
  for (let i = 0; i < ss.length; i += (per || 4)) out.push({ k: "p", t: ss.slice(i, i + (per || 4)).join("") });
  return out;
}
function deskBlocks(text) {
  const raw = String(text || "").replace(/\r\n?/g, "\n")
    /* 控制字符（换行 / 制表除外）直接丢掉：从 PDF 抽出来的稿子常带 U+0001 这类破码，
       留在页面上就是一块豆腐 —— 豆腐块永远不是正确答案。 */
    .replace(/[\x00-\x08\x0b-\x1f]/g, "").trim();
  if (!raw) return [];
  const lines = raw.split("\n"), blank = /\n[ \t]*\n/.test(raw), out = [];
  let buf = [];
  const flush = () => { if (buf.length) { out.push({ k: "p", t: buf.join("") }); buf = []; } };
  /* ★ 结构块（表格 / 列表 / 代码围栏）必须**整块带换行**交给 md() —— 它们靠换行
     分行分列。这里曾经只有一条路：块内所有行 join("") 压成一段，于是
     表格压成一行竖线乱码、无序列表压成一根横杠长句、有序列表更冤——
     「1. 提取 RNA」被 deskTocOf 认成标题，大纲里凭空多出三节。
     结构块走 k:"raw"（deskPaint 用 <div> 承接，md() 现渲），prose 仍 join("")：
     中文硬换行要拼回一段，这两条路不冲突。
     ★ 有序列表与「1. 材料」式小标题的边界：**连续 ≥2 行**列表项才算列表块
     （单行仍按 deskTocOf 的既有判据走 —— 那条判据有 ui_check 探针守着）。
     围栏代码块由 ``` 显式定界，两种分段模式下都认。 */
  const isFence = s => /^```/.test(s);
  const isRow = s => /^\s*\|.*\|\s*$/.test(s);
  const isSep = s => /^\s*\|[\s:|-]+\|?\s*$/.test(s);
  const isLi = s => /^\s*([-*+]|\d+[.)])\s+\S/.test(s);
  const grabWhile = (pred, keep) => {
    while (x < lines.length && pred(lines[x].trim())) { keep.push(lines[x]); x++; }
    x--;                       // while 多吃的一行还回去（外层 for 还要 ++）
  };
  let x = 0, refAt = -1;       // refAt：最后一条参考文献块的下标（-1 = 不在文献区）
  for (; x < lines.length; x++) {
    const ln = lines[x], s = ln.trim();
    if (isFence(s)) {
      flush();
      const keep = [ln];
      x++;
      while (x < lines.length && !isFence(lines[x].trim())) { keep.push(lines[x]); x++; }
      if (x < lines.length) keep.push(lines[x]);   // 收尾围栏
      out.push({ k: "raw", t: keep.join("\n") });
      continue;
    }
    if (isRow(s) && x + 1 < lines.length && isSep(lines[x + 1])) {
      flush();
      const keep = [ln];
      x++;
      grabWhile(isRow, keep);
      out.push({ k: "raw", t: keep.join("\n") });
      continue;
    }
    if (blank && isLi(s) && x + 1 < lines.length && isLi(lines[x + 1].trim())) {
      flush();
      const keep = [ln];
      x++;
      grabWhile(isLi, keep);
      out.push({ k: "raw", t: keep.join("\n") });
      continue;
    }
    /* 参考文献条目：**每条一段** —— [n] 行若被 join("") 拼进下一条，
       悬挂缩进只挂在整坨的第一行上，5 条文献挤成一大段没法读。
       （[n] 行本来就被 pclass 认成 p-ref；这里只是让它别跟邻居粘住。）
       条目的**续行**（折页、不以 [n] 开头）接回上一条文献 —— 一条文献不许被拆成两段；
       空行或标题出现时 refAt 作废，后面的普通段落照常拼接。
       ★ ACS / RSC / 多数英文期刊的编号是 **(1)** 而不是 [1]：真论文对拍（_desk_ruler.py
       抽 ACS J. Agric. Food Chem. 那篇）量出 20 条文献全被当正文 —— 同一件事不能只认一中括号式。
       但 (1) 比 [1] 容易撞车（正文里 "(2) 千" 这类），所以多要一道**长度门槛**：
       一条真文献不会短于 24 字。 */
    if (deskIsRef(s)) {
      flush();
      out.push({ k: "p", t: s });
      refAt = out.length - 1;
      continue;
    }
    if (!s) { flush(); refAt = -1; continue; }
    const h = deskTocOf(s);
    if (h) { flush(); out.push({ k: "h", lv: h.lv, t: h.t }); refAt = -1; continue; }
    if (blank) {
      if (refAt >= 0 && !buf.length) { out[refAt].t += s; continue; }   // 文献的续行
      buf.push(s);                        // 原文自己分过段：空行是边界
    }
    else { flush(); out.push({ k: "p", t: s }); } // 原文没分过段：行即段
  }
  flush();
  /* 摘要与关键词在期刊里是**两个部件**，ACS 这类刊把它们排成同一段的 run-in
     （"…in actinomyces. KEYWORDS: Saccharopolyspora spinosa, …"），
     读的人分不出边界 —— 在标签处拆成两段，关键词自己走 p-kw 那套缩排小一号。 */
  for (let i = 0; i < out.length; i++) {
    const b = out[i];
    if (b.k !== "p") continue;
    const m = /^([\s\S]{40,}?)\s((?:KEYWORDS?|Key\s*words|关\s*键\s*词)\s*[:：].+)$/i.exec(b.t);
    if (!m) continue;
    out.splice(i, 1, { k: "p", t: m[1].trim() }, { k: "p", t: m[2].trim() });
    i--;                       // 拆剩的前半段再过一次：一段里可能粘了两个标签
  }
  /* 句群降级只对**孤零零一段 prose**生效 —— 一整份代码块/表格（k:"raw"）被
     deskChunks 按句切开就是灾难（围栏碎一地），这里必须挡住。 */
  if (out.length === 1 && out[0].k === "p" && out[0].t.length > 140) return deskChunks(out[0].t);
  return out;
}
/* 出处抽取：DOI 与 URL 从正文里直接认出来 —— 不依赖后端再送一份字段，
   因为"能不能核"这件事只该由正文本身决定（Q 型缺陷的第一块砖就是锚点）。 */
function deskRefs(text) {
  const s = String(text || ""), seen = {}, out = [];
  const add = (key, t, href) => { if (!seen[key]) { seen[key] = 1; out.push({ t: t, href: href }); } };
  (s.match(/\b10\.\d{4,9}\/[^\s，。；）)】"'<>]+/g) || []).forEach(x => {
    const d = x.replace(/[.,;)\]]+$/, "");
    add("d" + d, d, "https://doi.org/" + d);
  });
  (s.match(/https?:\/\/[^\s，。；）)】"'<>]+/g) || []).forEach(x => {
    const u = x.replace(/[.,;)\]]+$/, "");
    /* doi.org 链接与裸 DOI 是**同一篇文献** —— 按 DOI 建同一个 key 合并，
       否则出处栏会把一篇文献列两遍。 */
    const dm = /^https?:\/\/(?:dx\.)?doi\.org\/(.+)$/i.exec(u);
    if (dm) add("d" + dm[1], dm[1], "https://doi.org/" + dm[1]);
    else add("u" + u, u.replace(/^https?:\/\//, ""), u);
  });
  return out.slice(0, 60);
}
/* 高亮：先按标签把 HTML 切开，只替换**标签之外**的文本片段。
   直接对整串 HTML 做 replace 会命中标签名与属性值。 */
function deskHi(html, kw) {
  if (!kw) return html;
  const re = new RegExp("(" + kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
  return html.split(/(<[^>]*>)/).map((s, i) =>
    i % 2 ? s : s.replace(re, '<mark class="dm">$1</mark>')).join("");
}
/* 引注标记 [n] → 可点的角标：学术稿里「这句的出处是哪条」是最高频的阅读动作，
   点一下就该跳到参考文献里那条，而不是让读者拿肉眼去参考文献里扫。
   ★ 输入是**已渲染的 HTML**（md() 之后）：md 已把 [文字](url) 变成 <a>，
     角标只认剩下的裸 [n]；
   ★ 先按标签切开再替换（与 deskHi 同一条纪律），防止误伤属性值里的 [1]；
   ★ 序号上限 3 位 ——「[2020]」是年份不是引注；后面紧跟 ( 的不认，那是链接残段。 */
function deskCite(html) {
  return html.split(/(<[^>]*>)/).map((s, i) => i % 2 ? s :
    s.replace(/\[(\d{1,3})\](?!\()/g, '<sup class="dcite" data-n="$1">[$1]</sup>')).join("");
}
/* ═══ 期刊解剖学判据（顶层：ui_check 探针按函数切片可量）═══
   一篇"正常期刊论文"由固定部件拼成 —— 题名、前置件（作者/单位/日期）、摘要、
   关键词、编号小节、图注表注、悬挂缩进的参考文献。每条判据只认**段首的可见事实**，
   不猜内容语义。★ 中英两套写法共用一条判据：中文「图 1：」「[1]」，
   英文 "Figure 1." "(1)" —— 真论文对拍（_desk_ruler.py 抽 ACS 那篇）量出
   117 块里 104 块全被判成正文，就是因为原来只写了中文那一半。 */
function deskIsAbs(s) {
  s = (s || "").trim();
  return /^(摘\s*要|Abstract)\s*[（(:：]/i.test(s) || /^摘\s*要$/.test(s);
}
function deskIsKw(s) {
  return /^(关键词|Keywords?|Key\s*words)\s*[（(:：]/i.test((s || "").trim());
}
/* 文献条目：中文 [1] 无门槛（W26 起就这样），英文 (1) 多一道长度门槛 ——
   正文里 "(2) 千斤" 这种开头不该被拆成一条文献；真文献不会短于 24 字。 */
function deskIsRef(s) {
  s = (s || "").trim();
  return /^\[\d{1,3}\]/.test(s) || (/^\(\d{1,3}\)\s+\S/.test(s) && s.length >= 24);
}
function deskIsCap(s) {
  s = (s || "").trim();
  // 中文反过来：「图 1 火山图」是图注，正文引用会写「如图 1 所示」——所以中文允许空格收尾
  if (/^[图表]\s*\d{1,3}\s*[：:.、\s]/.test(s)) return true;
  /* 英文必须带界点：期刊里 "Figure 1. 火山图" 是图注，而 "Figure 1 shows…" 是正文句子。
     缩写式 "Fig. 2 Heatmap" 的界点长在缩写自己尾巴上，后面跟空格也算。 */
  const m = /^(Figure|Fig\.|Fig|Table)\s*(\d{1,3})\s*([.．:：、]?)/i.exec(s);
  if (m && (m[1].toLowerCase() === "fig." || m[3] !== "")) return true;
  /* 第二把尺子（Frontiers in Microbiology 15）的图注表注**不带界点**，靠全大写标签区分：
     'FIGURE 1 The mechanisms of…'、'TABLE 1␀␀Microbial fermentation…'（NBSP + en-space）。
     同一篇里正文引用图表写的是 mixed case（'…in Table 2.'）—— 大小写就是刊物自己给的区分符，
     所以这一支只认全大写。 */
  return /^(?:FIGURE|TABLE)\s*\d{1,3}(?=$|\s)/.test(s);
}
/* 这条注属于表还是图：号段（t1/f1）、注在表的上方还是图的下方、
   以及英文期刊里表注要不要整行加粗，三处都问这同一个问题 —— 只留一个答案。 */
function deskIsTcap(s) { return /^(?:表|Table)\s*\d{1,3}/i.test((s || "").trim()); }
/* 前置件区 = 题名之后、摘要（没有摘要就第一个小标题）之前的那段。返回边界下标。 */
function deskFrontier(blocks) {
  const bs = blocks || [];
  let n = bs.findIndex((b, i) => i > 0 && b.k === "p" && (deskIsAbs(b.t) || deskIsKw(b.t)));
  if (n < 0) n = bs.findIndex((b, i) => i > 0 && b.k === "h");
  return n < 0 ? 0 : n;
}
/* 段落角色：顺序即优先级（摘要/关键词/文献/图表注都在前置件之前判，
   否则一段以「图 3」开头的长正文会被前置件规则误收）。
   长度门槛挡住"题名后紧跟一大段引言"被整段当作者行。 */
function deskPClass(s, i, frontier) {
  s = (s || "").trim();
  if (deskIsAbs(s)) return " p-abs";
  if (deskIsKw(s)) return " p-kw";
  if (deskIsRef(s)) return " p-ref";
  if (deskIsCap(s)) return " p-cap";
  /* 前置件长什么样：作者行 / 单位行 / 日期行 / 引用行都**不是句子** ——
     短（≤220 字：真稿一行挂二十个作者、或写全两个单位的长地址）且不以句末标点收尾。
     另一种形状整段被括号包着（中文期刊的单位块就是「（湖南农业大学…，长沙 410205）」），
     那种可以到 360 字。少了这些门槛，"题名之后直接一段引言"会被整段居中排成灰色作者行；
     误判成正文只是"跟以前一样"，误判成前置件是**版面坏了** —— 所以宁可紧。 */
  if (i > 0 && i < (frontier || 0) &&
      ((s.length <= 220 && !/[。．.;；]$/.test(s)) || /^\(.{40,360}\)$/.test(s))) return deskFrontKind(s);
  if (/^[-*+>]\s|^\d+[.)]\s|^\|/.test(s)) return "";   // 列表/引用/表格块不吃首行缩进
  return " p-body";
}
/* 标题只走行内语法：md() 是块级渲染器，会把「1. INTRODUCTION」的编号
   当有序列表标记吃掉 —— 页面上丢了号，大纲里却还留着，同一件事两处真相。 */
function deskHead(t) { return mdInline(esc(String(t == null ? "" : t))); }
/* 前置件内部还有层级：作者行最大、单位行小一号更淡、日期与引用行最小最淡 ——
   期刊的署名区就是这三档排下来的（真论文实测：作者 13pt / 单位 9pt / 日期 8pt）。
   判据还是段首可见事实：整段被括号包着的是单位块，以 Received:/Cite this:/收稿日期: 起头的是日期引用行。 */
function deskFrontKind(s) {
  s = (s || "").trim();
  if (/^[（(]/.test(s)) return " p-front p-affil";
  if (/^(Received|Revised|Accepted|Published|Cite this|收稿日期|修回日期|网络首发|DOI)\s*[:：]/i.test(s)) return " p-front p-meta";
  return " p-front";
}
/* 署名行里的标记（* 通讯作者、§ 同等贡献、† ‡ 其它注）在期刊里是**上标**，
   排在基线上就不像署名行。只对前置件做 —— 正文里的 * 是斜体记号，不是署名标记。
   ★ 两侧的贴边要求缺一不可：标记必须**紧跟前一个非空白字符**，且后面是分隔符或收尾。
     这样 `Rang,*`（逗号后紧跟）与串尾的 `Zhu*` 都算，
     而算式 "5 * 3" 里星号前面是空格 ⇒ 不抬（只看"后面跟什么"会被它骗过，探针当场抓到）。 */
function deskMark(html) {
  return html.split(/(<[^>]*>)/).map((s, i) => i % 2 ? s :
    s.replace(/(\S)([*§†‡])(?=$|[,，;；.)）\s])/g, '$1<sup class="dmark">$2</sup>')).join("");
}
/* 版式标签：期刊里的「ABSTRACT:」「KEYWORDS:」「Figure 1.」「表 2：」是**排出来**的部件
   （加粗 / 小型大写），不是句子的普通开头。渲染时单独包出来，读者一眼找到部件边界。
   ★ 只对已判定角色的段落做（p-abs / p-kw / p-cap / p-front）：正文里一句
     "Table 3 of the survey shows…" 不该被加粗 —— 角色没判出来就不许动版式。
   ★ 词标签必须带冒号才认（"Authors of this paper…" 不是标签）；
     图表标签的界点是句点/冒号，两种都收。 */
function deskLead(html, kind) {
  const re = kind === "p-cap"
    ? /^((?:[图表]\s*\d{1,3}|(?:Figure|Fig\.?|Table)\s*\d{1,3})\s*[.．:：、]?)/i
    : /^((?:摘\s*要|关\s*键\s*词|Keywords?|Key\s*words|Abstract|Authors?(?:\s*[Cc]ontributions)?|收稿日期|Received|Cite\s*This)\s*[:：])/i;
  const m = re.exec(html);
  if (!m) return html;
  return '<b class="dlead">' + m[1] + "</b>" + html.slice(m[0].length);
}
/* 这份稿子是中文还是英文？看字符本身，不看有没有 DOI。
   阈值取「汉字不到西文字母 1/4」—— 中文稿里夹几个基因名/期刊缩写是常态，
   不该把整篇判成英文稿（那会关掉首行缩进这条中文惯例）。 */
function deskLatin(t) {
  const s = String(t || "");
  const cjk = (s.match(/[㐀-䶿一-鿿豈-﫿]/g) || []).length;
  const lat = (s.match(/[A-Za-z]/g) || []).length;
  return lat > 200 && cjk * 4 < lat;
}
function deskPaint() {
  const inner = $("#deskInner"), d = DESK;
  /* 锚点用 data-i 而不是拼出来的 id：`id="dsk"+i` 运行时唯一、静态看却像重复 id，
     体检器会据此报假警（而报假警的检查器很快就被无视）。
     何况这些锚点只给自己人用，data-* 本来就比 id 合适。 */
  /* 期刊解剖学的判据在顶层（deskIsAbs / deskIsKw / deskIsRef / deskIsCap /
     deskFrontier / deskPClass），ui_check 探针按函数切片直接量；
     这里只负责"判出角色之后怎么排"。 */
  const frontier = deskFrontier(d.blocks);
  /* 引注角标交给 deskCite（顶层函数，ui_check 探针切片可测）；这里只管**给谁做**：
     只对正文段做 —— 参考文献自己就是 [n] 开头，再套一遍 sup 就是套娃。 */
  const html = d.blocks.map((b, i) => {
    if (b.k === "h") {
      const doc = (i === 0 && (b.lv || 1) === 1) ? " doc-title" : "";
      return '<h3 class="' + doc.trim() + '"' + (b.lv ? ' data-lv="' + b.lv + '"' : "") +
        ' data-i="' + i + '">' + deskHi(deskHead(b.t), d.kw) + "</h3>";
    }
    if (b.k === "raw")   // 表格/列表/代码：md() 现渲，<div> 承接（<p> 里放 <table> 是坏 HTML）
      return '<div class="draw" data-i="' + i + '">' + deskHi(md(b.t), d.kw) + "</div>";
    const cls = deskPClass(b.t, i, frontier).trim();
    const ref = cls === "p-ref" ? (/^[\[(](\d{1,3})[\])]/.exec(b.t) || [])[1] : "";
    /* 图注 → 内嵌资产：deskFillFigs 严格匹配到的图（DESK.figs），跟在图注后面。
       没匹配到就不出图（保持无图原样），**不许**放一个占位框充数。 */
    const figNo = cls === "p-cap" ? (/^(?:图|表|Figure|Fig\.?|Table)\s*(\d{1,3})/i.exec(b.t) || [])[1] : "";
    // 号段分开：表注只查 t 键、图注只查 f 键（否则表 1 会挂上图 1 的配图）
    const isTab = deskIsTcap(b.t);
    const fig = figNo ? (isTab ? "t" + figNo : "f" + figNo) : "";
    const figEl = (fig && d.figs && d.figs[fig])
      ? '<figure class="dfig" data-abs="' + esc(d.figs[fig].abs) + '" data-name="' + esc(d.figs[fig].name) +
        '" title="点开看原页">' +
        '<img src="/api/artifact?id=' + encodeURIComponent(d.runId) + "&path=" +
        encodeURIComponent(d.figs[fig].path) + '" alt="' + esc(d.figs[fig].name) + '"></figure>'
      : "";
    const body = cls === "p-body" ? deskCite(md(b.t)) : md(b.t);
    const lead = deskLead(body, cls);
    const para = '<p class="' + cls + (isTab ? " p-tcap" : "") + '"' +
      (ref ? ' data-ref="' + ref + '"' : "") +
      ' data-i="' + i + '">' +
      deskHi(cls.indexOf("p-front") >= 0 ? deskMark(lead) : lead, d.kw) + "</p>";
    /* 图在注**上方**、表在注下方 —— 期刊的图注表注分居两侧是排版规矩，
       不是可选项（读者先看图再读注，反过来是先读注再看表）。 */
    return isTab ? para + figEl : figEl + para;
  }).join("");
  inner.innerHTML = html ||
    '<div class="dempty"><b>这次没有落盘正文</b>产出若是文件，去成果卡的「产出物」里取。</div>';
  inner.classList.toggle("journal", !!d.journal);
  /* 中英文是两套排版惯例：中文期刊首行缩进两字符，英文期刊不缩进、靠段间距分行。
     一把尺子量两种稿子，必然有一种是错的 —— 所以按稿面文字本身决定。 */
  const lat = deskLatin(d.text);
  inner.classList.toggle("latin", lat);
  inner.setAttribute("lang", lat ? "en" : "zh-CN");
  /* 块 ↔ 节点一一对应存成表：跳转和高亮都查这张表，不拼选择器。
     空态时没有块，表就是空的（别让"空壳"和 blocks 长度对不上）。 */
  d.nodes = html ? d.blocks.map((b, i) => inner.querySelector('[data-i="' + i + '"]')) : [];

  const toc = [];
  d.blocks.forEach((b, i) => {
    if (b.k === "h") toc.push({ i: i, lv: b.lv, t: b.t });
    /* 无标题稿的"按块导航"只列 prose 段 —— 表格/代码块的原文当导航名是一行竖线乱码 */
    else if (!d.hasH && b.k === "p") toc.push({ i: i, lv: 1, t: b.t.slice(0, 18) + (b.t.length > 18 ? "…" : "") });
  });
  let th = '<div class="th">' + (d.hasH ? "大纲 · " + toc.length + " 节"
    : (toc.length ? "导航 · " + toc.length + " 段（没有标题标记，按块导航）" : "大纲")) + "</div>";
  toc.forEach(t => {
    th += '<button data-lv="' + (t.lv || 1) + '" data-go="' + t.i + '" title="' + esc(t.t) + '">' +
      esc(t.t) + "</button>";
  });
  const refs = deskRefs(d.text);
  if (refs.length) {
    th += '<hr><div class="th">出处 · ' + refs.length + " 条（正文里认出来的）</div>" +
      refs.map(r => '<a class="ref" href="' + esc(r.href) + '" target="_blank" rel="noopener" title="' +
        esc(r.href) + '">' + esc(r.t) + "</a>").join("");
  }
  $("#deskToc").innerHTML = th;
  /* 大纲栏的入场线：≥2 节（值得导航）或有多条出处才占位 ——
     一段短稿配一根 184px 的空栏杆，就是把阅读宽度砍掉三分之一 */
  $("#deskToc").hidden = !(toc.length >= 2 || refs.length);
  $("#deskProg").style.width = "0%";
  /* 稿面排完才知道"这份稿子有没有可开原件的图" —— 开关的可见性只能在这里定。
     放在 deskPaint 末尾（不是 openDesk 里）：重新渲染（改字号、查找）之后
     开关也必须跟着最新内容走，否则会出现"稿里已经没有图了、开关还挂着"。 */
  deskModePaint();
}
function deskHit(i) {
  const ms = DESK.hits;
  if (!ms.length) return;
  i = ((i % ms.length) + ms.length) % ms.length;
  ms.forEach(m => m.classList.remove("cur"));
  ms[i].classList.add("cur");
  ms[i].scrollIntoView({ block: "center", behavior: "smooth" });
  DESK.hit = i;
  $("#deskFindCnt").textContent = (i + 1) + " / " + ms.length;
}
function deskFind(kw) {
  DESK.kw = kw || "";
  deskPaint();
  DESK.hits = Array.prototype.slice.call($("#deskInner").querySelectorAll("mark.dm"));
  DESK.hit = -1;
  $("#deskFindCnt").textContent = !kw ? "" : (DESK.hits.length ? "0 / " + DESK.hits.length : "没有");
  if (DESK.hits.length) deskHit(0);
}
function deskScale(d) {
  const cur = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--desk-scale")) || 1;
  const sc = Math.max(0.85, Math.min(1.7, Math.round((cur + d) * 100) / 100));
  document.documentElement.style.setProperty("--desk-scale", String(sc));
  try { localStorage.setItem(DESK_KEY + "-scale", String(sc)); } catch (e) {}
}
/* ── 续读位置：长稿读到一半关掉，重开回原处 ──
   记的是**比例**不是像素：字号、面板宽度、窗口大小变了，像素位置就是错的。
   只记「真读进去的」（比例 > 0.5% —— 顶部不算位置），每稿一条，上限 20 条按时间挤旧。
   原页阅读（PDF）不走这套：那边的翻页有它自己的页码状态。 */
function deskStore() {
  try { return window.localStorage; } catch (e) { return null; }
}
/* 键名与上限写成函数而不是块级 const：ui_check 的探针按**函数**切片，
   引用块外常量的函数在沙箱里就是 ReferenceError —— 自含才测得到。 */
function deskPosKey() { return "rongke-desk-pos"; }
function deskPosCap() { return 20; }
function deskPosLoad() {
  const st = deskStore();
  if (!st) return {};
  try { return JSON.parse(st.getItem(deskPosKey()) || "{}") || {}; } catch (e) { return {}; }
}
function deskPosPrune(all, cap) {
  const keys = Object.keys(all).sort((a, b) => (all[a].t || 0) - (all[b].t || 0));
  while (keys.length > (cap || deskPosCap())) delete all[keys.shift()];
  return all;
}
function deskPosSave(rid, ratio) {
  if (!rid || !(ratio > 0.005)) return;          // 没稿号 / 还在顶部：都不算"读到过哪"
  const all = deskPosLoad();
  all[rid] = { r: Math.min(1, Math.max(0, ratio)), t: Date.now() };
  const st = deskStore();
  if (!st) return;
  try { st.setItem(deskPosKey(), JSON.stringify(deskPosPrune(all))); } catch (e) {}
}
function deskPosOf(rid) {
  if (!rid) return 0;
  const e = deskPosLoad()[rid];
  return (e && typeof e.r === "number" && e.r > 0 && e.r <= 1) ? e.r : 0;
}
/* 把比例落成滚动位置。**不能量了就赋**：右栏刚 data-open="1" 的那一帧，
   width 过渡（.22s）进行中，`.dc` 窄到一字一行，scrollHeight 实测冲到 6 万像素
   （整稿才 2900）—— 按 0.4 × 6 万落位就是一屁股坐到底。
   所以这里按 origApplyWidth 同一条纪律：等量度**稳定**（连续 3 帧同高）再落，
   30 帧兜底。`.dtext` 是 scroll-behavior:smooth，落位前临时关掉 —— 续读要瞬移。 */
function deskPosApply(pr) {
  const el = $("#deskText");
  if (!el) return;
  let lastH = -1, stable = 0, tries = 0;
  const step = () => {
    const h = el.scrollHeight;
    if (h === lastH) stable++; else stable = 0;
    lastH = h;
    if (stable >= 2 || tries++ > 30) {
      el.style.scrollBehavior = "auto";
      el.scrollTop = Math.round(pr * Math.max(0, el.scrollHeight - el.clientHeight));
      el.style.scrollBehavior = "";
      return;
    }
    requestAnimationFrame(step);
  };
  step();
}
/* 运行结果 → 稿件台载荷。全站**只有一个**地方算这件事：侧栏开关、成果卡的「读全稿」、
   以及面板开着时随新稿刷新，三处都走这里 —— 散成三份就是 H 型（改一处忘一处，迟早两副面孔）。
   （另一种内容另有来源：产出物文件走 deskReadFile，那是"文件"不是"运行结果"。）

   ★ 标题与副题的口径（2026-10-01 用户裁定「直接抄 DSH」后重写）：
   DSH 的 ui-sidebar-right 包在 README 里明文规定「**面板没有标题行**」，
   整个面板只有 2 个控件、不产生任何标题行布局盒。荣珂保留一行标题是为了
   让用户知道"右边这面墙是什么"，但**不许再往上摊机器的话**：
     · 标题 = **稿子自己的名字**，不是任务原文（任务原文是用户的输入，不是稿子的身份）；
     · 副题 = 只留**人能读的量**（多少字）。run id / 步数 / 内部分数一律不上屏
       —— run id 尤其：那是 14 位时间戳，用户既看不懂也用不上，
       真要它去「技术细节」里看。 */
function deskPayload(r) {
  const m = (r && r.lint && r.lint.metrics) || {};
  if (!r || !r.draft) {
    return { text: "", title: "交付正文", meta: "还没有正文 —— 跑完一次任务后在这里读全稿" };
  }
  const full = String(r.draft);
  return {
    text: full,
    runId: r.run_id || "",
    // 稿子名：优先正文里第一个标题块（稿件台自己认得出来），退回一句话摘要
    title: deskTitleOf(full, r),
    meta: (m.chars || full.length) + " 字",
  };
}
/* 从正文里取"稿子叫什么"：用同一套标题识别（deskTocOf），保证标题与大纲页第一行是同一个东西。
   一条都没有时才退回一句话摘要 —— 摘要要压到 42 字，且不许把 run id 混进去。 */
function deskTitleOf(text, r) {
  const lines = String(text || "").split("\n");
  for (const ln of lines) {
    const s = ln.trim();
    if (!s) continue;
    const h = deskTocOf(s);
    if (h && h.t) return h.t.replace(/\s+/g, " ").slice(0, 42);
    break;   // 只在开头找：第一个非空行不是标题就不当标题看了
  }
  const one = String((r && r.final) || "").replace(/\s+/g, " ").trim();
  return (one || "交付正文").slice(0, 42);
}
/* 产出物文件 → 稿件台载荷（另一种内容的唯一来源）。
   先把面板打开再说"读取中…"：点了没反应最容易被当成坏了，
   而这里读的可能是几十万字符的文件。 */
async function deskReadFile(runId, f) {
  const base = { title: f.path, meta: fmtSize(f.size) + " · 读取中…" };
  openDesk({ text: "", title: base.title, meta: base.meta });
  try {
    const r = await fetch("/api/artifact?id=" + encodeURIComponent(runId) +
                          "&path=" + encodeURIComponent(f.path));
    if (!r.ok) throw new Error("HTTP " + r.status);
    const t = await r.text();
    openDesk({ text: t, title: f.path, meta: fmtSize(f.size) + " · " + t.length + " 字符 · " + (f.ext || "") });
  } catch (e) {
    openDesk({ text: "", title: f.path, meta: "读取失败：" + (e && e.message ? e.message : e) });
  }
}
/* ═══ 「打开本地论文」═══
   为什么要有这条路：用户手上真正要读的论文，大多**不在**荣珂的任务工作区、
   素材区或文献库之内（实测：桌面上的那篇卤化酶论文被 /api/orig 的根围栏如实挡下，
   回「这个文件不在本任务的工作区、素材区或文献库之内」）。没有这一步，
   "看原页"就只对库里那几篇成立 —— 而用户要读的恰恰是他自己那篇。

   走的是**既有的**上传通路（POST /api/upload?name=…，裸字节体）：
   它把文件落进素材区 runs/_inbox/，而素材区本来就在 /api/orig 的允许根之内。
   ⇒ 不需要放宽任何围栏，也不需要新增一条"服务端按路径打开本地文件"的口子
     （那种口子等于把整台盘交给一个 URL，正是上一轮刚补掉的那个洞）。

   纪律：**只挑不读**。浏览器给的是 File 对象，我们把它读成字节传上去；
   本地路径从头到尾没有离开浏览器。 */
/* 「打开本地论文」收哪几种：与"能开原页"是**同一件事**，所以直接用那一个判据。
   另立一份清单就会漂 —— 两处清单不一致时，界面收下的文件后端不认。 */
function deskLocalOk(name) {
  return deskIsOrigDoc(name);
}
async function deskOpenLocalFile(file) {
  if (!file) return;
  if (!deskLocalOk(file.name)) {
    openOrig({ title: "打不开这份文件" });   // 走同一面板如实说，不弹 alert
    origShow('<b>这份文件看不了原页</b>'
      + '原页只支持论文/文档类：' + ORIG_DOC_EXT.join("、")
      + '<br>你选的是 ' + esc(file.name || "（无名字）"));
    return false;
  }
  const buf = await file.arrayBuffer();
  const r = await fetch("/api/upload?name=" + encodeURIComponent(file.name), {
    method: "POST", body: buf,
    headers: { "Content-Type": "application/octet-stream" },
  });
  let j = null;
  try { j = await r.json(); } catch (e) { j = null; }
  if (!r.ok || !j || !j.ok || !j.item) {
    const why = (j && j.error) || ("HTTP " + r.status);
    openOrig({ title: "打不开这份文件" });
    origShow('<b>没能把这份文件递进来</b>' + esc(why)
      + '<br>原件没有被改动，也没有离开你的电脑。');
    return false;
  }
  /* 递进来的名字可能与原件同名 —— 服务端**绝不覆盖**同名文件，会自己改成 `-2`。
     所以取"落盘后扫描出来的那一项"的路径（j.item.path），不自己拼路径。 */
  const it = j.item;
  return openOrig({ file: it.path, title: it.name, from: "list" });
}
/* 历次稿子 —— 稿件台默认只显示"最近一次出稿"，可写作真正的工作方式是**翻回去比**。
   三条纪律：① 列表只取一次（打开面板不能发十几个请求）；
   ② **点哪次就如实报哪次** —— 页码与正文必须同源，否则你读的是 A 稿、标的是 B 次；
   ③ 这里只列**能打开**的那几条 —— 判据走 canReplay()，与历史表同一条，不许在这里再写一遍。 */
let DESK_RUNS_P = null;
const DESK_RUNS_NEED = 12;      // 下拉要摆几条
/* 补问时的上限，与内核 recent_runs(200)（server.py）同源 —— 台账那一路
   最多给 200 条，问得再大也不会多出台账记录，所以不往上堆魔数。 */
const DESK_RUNS_CAP = 200;
async function deskFillRuns() {
  const sel = $("#deskRun");
  if (!sel) return;
  if (!DESK_RUNS_P) {
    // 「要几条」与「一次请求几条」解耦：先按要的条数问（常见情况一问就满，
    // 不必每次都为最坏情况拉满上限）；过滤掉不可回放的之后不足，再按上限补问一次。
    // 只问 12 条而不补，就会在过滤之后凑不满、下拉莫名少几条。
    DESK_RUNS_P = (async () => {
      const grab = n => api("/api/runs?n=" + n)
        .then(d => (d.runs || []).filter(canReplay)).catch(() => []);
      let rs = await grab(DESK_RUNS_NEED);
      if (rs.length < DESK_RUNS_NEED) {
        const more = await grab(DESK_RUNS_CAP);
        if (more.length > rs.length) rs = more;   // 补问失败时别把已有的几条丢掉
      }
      return rs.slice(0, DESK_RUNS_NEED);
    })();
  }
  let rs = [];
  try { rs = await DESK_RUNS_P; } catch (e) { rs = []; }
  if (!rs.length) { sel.disabled = true; sel.title = "还没有历史任务"; return; }
  const cur = DESK.runId || "";
  sel.innerHTML = '<option value="">历次…</option>' + rs.map(r => {
    const t = String(r.ts_iso || "").slice(5, 16);                   // 09-19 10:07
    const g = String(r.goal || r.id || "").replace(/\s+/g, " ").slice(0, 22);
    return '<option value="' + esc(r.id) + '"' + (r.id === cur ? " selected" : "") + ">" +
      esc(t + " " + g) + "</option>";
  }).join("");
}
/* 运行号 → 人读得懂的时刻。`20260919-124950` 就是 09-19 12:49。
   为什么不在 meta 里摊原始运行号：用户要的是"我读的是哪一次"，
   而运行号是**机器的主键**；它前 15 位本来就是时间，读出来给人看更直接。
   读不出形状（不是那个格式）时**原样回**，不猜、也不编一个时刻。 */
function deskRunWhen(rid) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(String(rid || ""));
  return m ? m[2] + "-" + m[3] + " " + m[4] + ":" + m[5] : String(rid || "");
}
/* 点开某一次：先把面板切到"读取中"，免得看起来像没反应（/api/run 一份快照几万字节）。 */
async function deskLoadRun(rid) {
  if (!rid) return;
  openDesk({ text: "", title: "读取中…", meta: "", runId: rid });
  try {
    const d = await api("/api/run?id=" + encodeURIComponent(rid));
    const r = d.result || {}, n = String(r.draft || "");
    /* 原来这里分两岔、其中一岔说"这类任务产出的是 .pptx，正文不落稿件台"——
       那句话**现在是错的**：.pptx 可以从成果卡的「产出物」拿来看**原页**，
       正文一样不落稿件台，但用户不必再被指去别处。空态由 deskPaint 统一说
       （"这次没有落盘正文 · 产出若是文件，去成果卡「产出物」里取"），这里不再重复。
       ★ meta 保留"读的是哪次"（历次下拉必须有回指，否则你读的是 A 稿、标的是 B 次），
       但用**时刻**而不是运行号 —— 见 deskRunWhen。 */
    openDesk({
      text: n, runId: rid, restore: true,
      title: String(d.goal || "").slice(0, 42) || "未命名任务",
      meta: (n ? n.length + " 字 · " : "") + deskRunWhen(rid),
    });
  } catch (e) {
    openDesk({ text: "", runId: rid, title: "这次读不出来",
      meta: deskMissText(rid, e && e.message ? e.message : e) });
  }
  paintArts();     // 历史行的选中态跟着稿件台当前内容走（见 paintArts 的别名说明）
}
/* ★ 这个名字是历史遗留：原来它重绘的是独立的「成品区」，那个区已撤。
   现在它只是 paintSessions 的别名，存在的理由是 **ui_check.py 的沙箱**按
   `paintArts` 这个标识符给 deskLoadRun 注桩（ui_check.py:234-239）——
   改名会让那段切片测试 ReferenceError，看起来像"稿件台坏了"。
   所以保留这个入口，但让它指向唯一那份真相。 */
function paintArts() { paintSessions(); }
/* ── 图表内嵌：稿子写「图 N」时，把工作区里**文件名自己承认是图 N** 的资产嵌进来 ──
   为什么是严格匹配：文本里的「图 1」和盘上的 fig_result1_x.png 之间**没有可靠锚点**，
   猜配会配错图 —— 配错比不配更糟（读者会把别人的结果当成这篇的）。
   所以只认文件名明说的：fig1 / fig_1 / fig-1 / figure1 / 图1 开头（fig12 不算图 1，
   数字后面不许再跟数字）。含糊（多份不同名都像）⇒ 全都不嵌；
   同名多格式（fig1_x.png + fig1_x.pdf）⇒ 挑 <img> 渲得动的那个（png/jpg/svg…，pdf 渲不动）。 */
function deskFigPick(n, files, kind) {
  const stemOf = f => String(f && f.name || "").replace(/\.[^.]+$/, "").toLowerCase().replace(/\s+/g, "");
  /* fig 后允许一个分隔符（fig_1 / fig-1 / fig.1）；数字后不许再跟数字或字母 ——
     fig12 不算图 1，fig1x 这种含糊形状也不算（没明说就不嵌）。
     ★ 图与表是**两个号段**：图 1 与表 1 各指各的东西，共用一个号就会把图 1 的配图
       挂到表 1 的注下面（真论文对拍当场抓到：8 张图嵌出了 9 个 figure）。
       kind="t" 只认表族前缀，图族资产一律不给。 */
  const pre = kind === "t" ? "table|表" : "fig(?:ure)?|图";
  const re = new RegExp("^(?:" + pre + ")[_\\-\\.]?(\\d{1,3})(?![0-9a-z])");
  const want = String(n);
  const hits = {};
  for (const f of (files || [])) {
    const st = stemOf(f);
    const m = re.exec(st);
    if (!m || m[1] !== want) continue;
    (hits[st] = hits[st] || []).push(f);
  }
  const stems = Object.keys(hits);
  if (stems.length !== 1) return null;          // 没有或含糊：都不嵌
  const IMG = { png: 1, jpg: 1, jpeg: 1, gif: 1, webp: 1, svg: 1 };
  const ok = hits[stems[0]].filter(f => IMG[String(f.ext || "").toLowerCase()]);
  return ok.length ? ok[0] : null;              // 只剩渲不动(pdf)的：也不嵌
}
const DESK_FIG_EXTS = ["png", "jpg", "jpeg", "svg", "gif", "webp"];
/* 拉本任务的产出物清单 → 算图号映射 → 重渲。失败**不吭声**：图是加餐，拉不到
   就该是无图的原样，不许为此弹一条错打断阅读。 */
async function deskFillFigs() {
  const rid = DESK.runId;
  if (!rid) return;
  try {
    const d = await api("/api/artifacts?id=" + encodeURIComponent(rid));
    const all = d.files || [];
    /* ★ 顺带记下**本任务的文档类产出**（PDF/Word/PPT）—— 它是「原页」那一档的进门钥匙。
       原来这里只留图片扩展名，于是任务产出的 PDF 在右栏根本无处可去：
       图号映射用不上它，原页的判据又只认内嵌图。实测有个任务工作区里
       就躺着 fig1_volcano.pdf，界面上却连"原页"这个开关都不出现。
       只记第一份（一份稿子对应一份原件），按路径短的优先 —— 通常那是主文件。 */
    const docs = all.filter(f => deskIsOrigDoc(f.path || f.name))
                    .sort((a, b) => String(a.path).length - String(b.path).length);
    if (docs.length && rid === DESK.runId) {
      DESK.docAbs = joinAbs(d.workspace || "", docs[0].path);
      DESK.docName = docs[0].name || docs[0].path;
    }
    const files = all.filter(f => DESK_FIG_EXTS.indexOf(String(f.ext || "").toLowerCase()) >= 0);
    const figs = {};
    for (let n = 1; n <= 30; n++) {
      ["", "t"].forEach(kind => {          // 图与表各一个号段，键是 f1 / t1
        const pick = deskFigPick(n, files, kind);
        if (pick) figs[(kind ? "t" : "f") + n] =
          { path: pick.path, name: pick.name, abs: joinAbs(d.workspace || "", pick.path) };
      });
    }
    /* 回来时可能用户已切到别的稿（rid 对不上就丢）—— 内嵌错稿的图比不嵌严重 */
    const gotFigs = Object.keys(figs).length > 0;
    if (gotFigs) DESK.figs = figs;
    /* ★ 重渲的条件不能只看"有没有图"：一份只产出 PDF、没有图片的任务，
       稿面本身不用重排，但**「原页」这个开关要亮起来** —— 那也是一次界面变化。
       （原来这里只判 gotFigs，于是纯 PDF 的任务里开关永远不出现。） */
    if ((gotFigs || DESK.docAbs) && rid === DESK.runId) {
      deskPaint();
      /* 图到位后版面高了 ~一张图 —— 续读位置按**最终布局**再校一次。
         但只在该用户没自己滚过的时候：校正值（deskPosOf，滚动防抖存的就是当前位置）
         和恢复值差得远 = 人已经读走了，尊重人，别拽回来。 */
      if (DESK.posRestore > 0) {
        const cur = deskPosOf(DESK.runId);
        if (Math.abs(cur - DESK.posRestore) < 0.02) deskPosApply(cur);
        DESK.posRestore = 0;
      }
    }
  } catch (e) { /* 保持无图 */ }
}
/* ── 右栏几何（照 DSH ui-layout/README）：首次开在视口 45%，此后记住像素值，
   上限 70%；给中间保底 400px ⇒ 上限 = min(70% 视口, 视口 − 侧栏 − 400)。
   装不下（上限 < 300）时 openDesk 不开，右把手在关闭态本就不渲染。 ── */
let DESK_W_SET = false;              // 本会话是否已有明确像素宽（拖过 / 首开算过）
let DESK_W = 0;                      // 本会话记住的右栏像素宽（DSH：刷新即重置，不落 localStorage）
function deskSideW() {
  const s = document.getElementById("side");
  if (!s) return 280;
  return s.dataset.fold === "1" ? 56 : (parseInt(getComputedStyle(s).width, 10) || 280);
}
function deskCapW() {                // 上限：70% 视口，且保住中间 400
  const vw = window.innerWidth;
  return Math.min(Math.floor(vw * 0.70), vw - deskSideW() - 400);
}
function deskClampW(w) { return Math.max(300, Math.min(deskCapW(), w)); }
function deskFirstW() { return deskClampW(Math.floor(window.innerWidth * 0.45)); }
function deskRoomOk() { return deskCapW() >= 300; }
function deskSetW(w) {               // 写入并夹取；DESK_W 是本会话记忆
  DESK_W = deskClampW(w);
  DESK_W_SET = true;
  document.documentElement.style.setProperty("--desk-w", DESK_W + "px");
  return DESK_W;
}
function openDesk(o) {
  o = o || {};
  // 中间保底 400：连右栏最小 300 都塞不下 -> 不开，让位给主区（DSH：报"空间不够"让它自己关）
  if (window.innerWidth > 880 && !deskRoomOk()) return false;
  deskExitOrig();                    // 从原页模式切回正文：清掉原页的滚动/缩放残留
  DESK.text = String(o.text || "");
  DESK.title = o.title || "交付正文";
  DESK.meta = o.meta || "";
  DESK.runId = o.runId || "";
  /* 图表内嵌的映射必须**每稿现拉** —— 上一稿的 figs 留着，就会把别人的图嵌进这一稿。
     同一句话对 docAbs 也成立：上一稿的 PDF 留着，这一稿的「原页」会打开**别人的原件**，
     而且界面上完全看不出来（开关亮着、点开是另一篇）—— 那是最坏的一类错。 */
  DESK.figs = null;
  DESK.docAbs = ""; DESK.docName = "";
  /* 期刊版式的开关：run 交付的**成品文章**按期刊排版显示；
     文件预览（deskReadFile 递进来的代码/表格）保持通用样式 —— 对代码做首行缩进是灾难 */
  DESK.journal = !!o.runId;
  DESK.blocks = deskBlocks(DESK.text);
  DESK.hasH = DESK.blocks.some(b => b.k === "h");
  DESK.kw = "";
  // 首次开：视口 45%；本会话已定过宽（拖过）：沿用像素值，但按当前侧栏/视口重新夹上限
  if (window.innerWidth > 880) deskSetW(DESK_W_SET ? DESK_W : deskFirstW());
  $("#deskTitle").textContent = DESK.title;
  $("#deskMeta").textContent = DESK.meta;
  $("#desk").dataset.open = "1";
  $("#btnDesk").classList.add("on");
  deskPaint();
  $("#deskText").scrollTop = 0;
  /* 续读位置：只对**重开同一篇**的两条路生效（读全稿 / 历次，o.restore）——
     新稿到达（deskSync）是**内容变了**，跳回旧位置是骗人；
     文件预览没有"上次读到哪"的语义（无稿号，deskPosOf 本来就是 0）。
     有记录就把比例换算回像素，并在副题里**如实说**一句 —— 静默跳位置会让人
     怀疑自己是不是漏看了开头。 */
  const pr = o.restore ? deskPosOf(DESK.runId) : 0;
  DESK.posRestore = pr;              // 图异步到位后（deskFillFigs）还要按最终布局再校一次
  if (pr > 0) {
    const mm = $("#deskMeta");
    if (mm.textContent) mm.textContent += " · 已回到上次读到的地方";
    deskPosApply(pr);
  }
  deskFillRuns();      // 异步补"历次"下拉；它失败也只是没有下拉，不影响正在读的稿子
  deskFillFigs();      // 异步拉图号映射；拉不到就是无图原样（deskFillFigs 自己兜）
  return true;
}
/* ═════════════ 原页阅读 ═════════════
   为什么单独一条路：正文那条路是「把 markdown 重排成页面」，而 PDF/PPT 的原页
   **不能重排** —— 论文的意思有一半在版面里（双栏、图、公式上下标）。
   所以这里只翻页和缩放，一个字都不动。

   三条纪律：
   ① 渲染慢，**不自动渲**。先问服务端"几页、要不要渲"，让用户自己按下开始。
   ② 同一份文件按 sha256 缓存，翻回来第二次是即时的。
   ③ 面板宽度会变（可拖），所以「适宽」要在 resize 时重算，不能算一次就钉死。 */
const ORIG = { on: false, from: "", src: "", kb: "", title: "", n: 1, total: 0,
               zoom: 0, rendered: false, busy: false, natW: 0, whyNot: "" };
const ORIG_FIT = 0;                 // zoom=0 表示"适宽"（宽度铺满面板）

function origEls() {
  return { box: $("#deskOrig"), stage: $("#origStage"), blank: $("#origBlank"),
           img: $("#origImg"), bar: $("#origBar") };
}
/* 适宽：把图宽算成"面板可用宽"。放大时给固定像素宽，由 stage 滚动看。

   ★ 一个踩过的坑：`clientWidth` 在元素**还没布局出来**时读到的是中间态。
   实测：面板刚 .dataset.open="1" 的那一帧，stage 量到 224px（面板正在撑开），
   下一帧才稳定在 674px —— 于是图被钉成 224px，界面上看不出任何报错。
   一次 rAF 不够（它就在那一帧里跑）。所以：**量两次，宽度变了就再算**，
   稳定为止（最多 8 次）。origApplyWidth 是幂等的，多算几次没有代价。 */
let _origFitTries = 0;
function origApplyWidth() {
  const st = $("#origStage"); if (!st) return;
  const img = $("#origImg");
  const avail = st.clientWidth - 28;
  if (avail < 80) {                  // 还没布局：下一帧再来
    if (_origFitTries++ < 8) requestAnimationFrame(origApplyWidth);
    return;
  }
  let w;
  if (!ORIG.zoom) { w = avail; $("#origZoomTxt").textContent = "适宽"; }
  else {
    /* 放大倍率以**原图宽**为基准。没量到原图宽时先按当前显示宽兜一下，
       img.onload 里会补算一次 —— 否则第一次点 + 会以小宽度为基准，怎么点都大不了。 */
    const base = ORIG.natW || avail;
    w = Math.round(base * ORIG.zoom);
    $("#origZoomTxt").textContent = Math.round(ORIG.zoom * 100) + "%";
  }
  if (img) img.style.width = w + "px";
  /* 宽度还在变（面板正撑开）⇒ 下一帧再算一次，直到稳定。
     判据用"这次算的 == 上次算的"，而不是"算了几次" —— 次数是猜的，相等是事实。 */
  if (w !== _origFitW && _origFitTries++ < 8) {
    _origFitW = w;
    requestAnimationFrame(origApplyWidth);
  } else {
    _origFitW = w;
  }
}
let _origFitW = -1;
function origShow(html) {           // 舞台中央那块（加载中 / 错误 / 开始渲染）
  const b = $("#origBlank");
  b.innerHTML = html;
  b.hidden = false;
  const img = $("#origImg"); if (img) img.hidden = true;
}
function origHideBlank() { $("#origBlank").hidden = true; }
function origPaint() {
  const img = $("#origImg");
  if (!img || !ORIG.rendered) return;
  const url = "/api/orig/page?n=" + ORIG.n
    + (ORIG.kb ? "&kb=" + encodeURIComponent(ORIG.kb) : "&file=" + encodeURIComponent(ORIG.src));
  img.src = url;
  img.hidden = false;
  origHideBlank();
  $("#origPage").value = String(ORIG.n);
  $("#origTotal").textContent = "/ " + ORIG.total;
  $("#origPrev").disabled = ORIG.n <= 1;
  $("#origNext").disabled = ORIG.n >= ORIG.total;
  /* ★ 顺序要紧：容器必须**先 visible** 再量宽。
     容器还 hidden 时 clientWidth=0，量出来是 0 ⇒ 图被钉成最小宽。
     origApplyWidth 自己也会在量不到时排到下一帧重试，这里是第一道保险。 */
  origApplyWidth();
}
function origGo(n) {
  if (!ORIG.rendered) return;
  ORIG.n = Math.max(1, Math.min(ORIG.total, n | 0));
  origPaint();
  $("#origStage").scrollTop = 0;
}
/* 问服务端：这份文件能不能渲、几页。**不触发渲染**，所以很快。 */
async function origProbe() {
  const q = ORIG.kb ? "kb=" + encodeURIComponent(ORIG.kb)
                    : "file=" + encodeURIComponent(ORIG.src);
  const d = await api("/api/orig/open?" + q);
  if (!d.ok) { origShow('<b>打不开原件</b>' + esc(d.error || "")); return null; }
  ORIG.title = d.title || ORIG.title;
  $("#deskTitle").textContent = ORIG.title;
  ORIG.total = d.pageCount || 0;
  ORIG.n = ORIG.total ? Math.max(1, Math.min(ORIG.total, ORIG.n)) : 1;
  ORIG.rendered = !!d.rendered;
  /* ★ 服务端已经知道"渲不了、为什么"（`why_not`），就**别再把"渲染原页"这个按钮递给用户**。
     实测：夹具里放着一份 43 字节的假 PDF（内容写着 "fixture decoy (not renderable)"），
     /api/orig/open 如实回 why_not="PDFium could not open the PDF"，
     而这里原来把它丢掉 ⇒ 界面照旧弹一个「渲染原页」，点下去必然失败。
     **知道会失败还给按钮，是在骗用户点。** */
  ORIG.whyNot = d.why_not || "";
  $("#origTotal").textContent = "/ " + (ORIG.total || "?");
  return d;
}
/* 真渲染。几十页要十几秒，所以显式给一个按钮 + 说清大概要多久。 */
async function origDoRender() {
  if (ORIG.busy) return;
  ORIG.busy = true;
  origShow('<b>正在渲染原页…</b>论文通常十几秒。<br>渲染结果会缓存，下次打开是即时的。');
  const q = (ORIG.kb ? "kb=" + encodeURIComponent(ORIG.kb)
                     : "file=" + encodeURIComponent(ORIG.src))
          + "&dpi=" + ORIG.dpi;
  try {
    const d = await api("/api/orig/render?" + q);
    if (!d.ok) { origShow('<b>渲染失败</b>' + esc(d.error || "")); ORIG.busy = false; return; }
    ORIG.total = d.pageCount;
    ORIG.rendered = true;
    ORIG.n = Math.max(1, Math.min(ORIG.total || 1, ORIG.n));
    ORIG.busy = false;
    origPaint();
    // 页面正文里那句"字数"对原页没意义，换成真实信息：几页 + 走没走 pdf 中转
    $("#deskMeta").textContent = d.pageCount + " 页";
    deskBuildFilm();
  } catch (e) {
    origShow('<b>渲染失败</b>' + esc(e.message));
    ORIG.busy = false;
  }
}
/* 缩略图带：只在渲染好之后建，且**懒加载**（一份 19 页的论文就是 19 张图）。 */
async function deskBuildFilm() {
  const f = $("#origFilm");
  if (!f || !ORIG.rendered || ORIG.total < 2) return;
  f.hidden = false;
  const q = ORIG.kb ? "&kb=" + encodeURIComponent(ORIG.kb)
                    : "&file=" + encodeURIComponent(ORIG.src);
  f.innerHTML = Array.from({ length: ORIG.total }, (_, i) =>
    '<img loading="lazy" data-n="' + (i + 1) + '" src="/api/orig/page?n=' + (i + 1) + q
    + '" title="第 ' + (i + 1) + ' 页">').join("");
  f.querySelectorAll("img").forEach(im => {
    im.onclick = () => origGo(parseInt(im.dataset.n, 10));
  });
  $("#origFilm").hidden = false;
}
function origMarkFilm() {
  const f = $("#origFilm"); if (!f) return;
  f.querySelectorAll("img").forEach(im => {
    im.classList.toggle("on", parseInt(im.dataset.n, 10) === ORIG.n);
  });
}
/* 进入原页模式。o = {kb} 或 {file}，可选 title。
   `o.from === "doc"` 表示这是从**版面**那档切过来的 —— 那时"版面"这一档仍然成立
   （稿子还在内存里），所以开关要显示出来、且能把人切回去。
   从文献库/产出物直接开的那条路没有"版面"可回，开关就不该显示。 */
async function openOrig(o) {
  o = o || {};
  if (window.innerWidth > 880 && !deskRoomOk()) return false;
  ORIG.on = true; ORIG.from = o.from || "list";
  ORIG.kb = o.kb || ""; ORIG.src = o.file || "";
  ORIG.title = o.title || "原页"; ORIG.dpi = o.dpi || 110;
  ORIG.n = Math.max(1, parseInt(o.n == null ? "1" : o.n, 10) || 1);
  ORIG.total = 0; ORIG.zoom = ORIG_FIT; ORIG.rendered = false; ORIG.natW = 0;
  ORIG.whyNot = "";            // 上一份的"打不开原因"绝不许泄漏到下一份上
  $("#deskOrig").hidden = false;
  $("#deskText").hidden = true;
  $("#deskToc").hidden = true;
  $("#origFilm").hidden = true; $("#origFilm").innerHTML = "";
  $("#deskTitle").textContent = ORIG.title;
  $("#deskMeta").textContent = "";
  if (window.innerWidth > 880) deskSetW(DESK_W_SET ? DESK_W : deskFirstW());
  /* ★ 先开面板、再量宽：面板关着时里头的 stage clientWidth=0。
     原来我把 openDesk 那套顺序照抄过来，结果图被钉成 120px（还看不出来是 bug）。 */
  $("#desk").dataset.open = "1";
  $("#btnDesk").classList.add("on");
  origShow('<b>正在读取…</b>');
  let d = null;
  try { d = await origProbe(); }
  catch (e) { origShow('<b>读取失败</b>' + esc(e.message)); return true; }
  if (!d) return true;
  if (d.rendered) {                 // 缓存命中：直接显示，不问
    ORIG.rendered = true;
    $("#deskMeta").textContent = d.pageCount + " 页";
    origPaint(); deskBuildFilm();
  } else if (ORIG.whyNot) {
    /* 服务端已判定这份原件**渲不出来**，并给了原因 —— 如实转述，不给死按钮。
       两条出路都要给：换一份能看的文件，或者回版面读（稿面本来就在）。 */
    origShow('<b>这份原件打不开</b>'
      + '它不是一份能渲染的文档，所以看不了原页。<br>'
      + '<span class="owhy">' + esc(String(ORIG.whyNot).replace(/\s+/g, " ").trim()) + '</span><br>'
      + '换一份 PDF / Word / PPT 再试（⋯ → 打开本地论文…），'
      + (ORIG.from === "doc" ? '或者切回<b>版面</b>读这份稿子。' : '或者到文献库里换一篇。'));
  } else {
    origShow('<b>' + esc(ORIG.title) + '</b>'
      + '这份原件要渲染成图才能看原页。<br>'
      + '渲染后版面与原件一致（双栏、图、公式都保留）。'
      + '<div class="obtn"><button class="mini" id="origStart">渲染原页</button></div>');
    const b = $("#origStart"); if (b) b.onclick = origDoRender;
  }
  deskModePaint();
  return true;
}
/* 退出原页模式，回到正文 */
function deskExitOrig() {
  ORIG.on = false; ORIG.from = ""; ORIG.rendered = false; ORIG.total = 0; ORIG.n = 1;
  const box = $("#deskOrig"); if (box) box.hidden = true;
  const film = $("#origFilm"); if (film) { film.hidden = true; film.innerHTML = ""; }
  const img = $("#origImg");
  if (img) { img.hidden = true; img.removeAttribute("src"); }
  const t = $("#deskText"); if (t) t.hidden = false;
  deskModePaint();
}
/* ═══ 版面 / 原页 两档开关 ═══
   两档是**同一份东西的两种看法**：版面 = 荣珂按期刊规矩重排过的稿面；
   原页 = 论文原件的页面，一个字都不改（图、公式、上下标、双栏全在）。

   ★ 「切得过去」这件事必须先量清楚再显示开关。这里踩过一次，记下来：
     我起初把判据写成"稿子里有没有内嵌图"，于是这份夹具的 **8 张图全是 .png** ——
     开关亮着，一点下去得到的是「这个接口只开 PDF 与能转 PDF 的文档…收到的是 .png」。
     **一张插图不是"一页原件"**：插图本来就是图，放大看它就是尽头；
     而"原页"要的是一份**能分页渲染的文档**（PDF / Word / PPT）。
     两者混为一谈，用户点到的就是一个永远打不开的开关 —— 比不挂更糟。

   所以判据只有一个：这份稿子有没有一份**可渲染的原件**（PDF/Office 文档）。
   `deskOrigAbs()` 是它唯一的出处，开关与点击都走这里。 */
function deskIsOrigDoc(p) {
  const s = String(p || "").toLowerCase().split(/[?#]/)[0];
  return ORIG_DOC_EXT.some(x => s.endsWith(x));
}
/* 当前稿子里可开"原页"的那份原件（没有就返回空串）。
   两条来源，按可信度排：
     ① 这份稿子**自己产出的文档**（DESK.docAbs，来自 /api/artifacts）——
        那是"这次工作的原件"，最贴合"读这份稿子的原页"这个意思；
     ② 稿面里内嵌的、指向文档的图（少见，但产出物里带 PDF 图时就是它）。 */
function deskOrigAbs() {
  if (ORIG.on && ORIG.src && deskIsOrigDoc(ORIG.src)) return ORIG.src;
  if (DESK.docAbs && deskIsOrigDoc(DESK.docAbs)) return DESK.docAbs;
  const figs = ($("#deskInner") || document).querySelectorAll("figure.dfig[data-abs]");
  for (const f of figs) if (deskIsOrigDoc(f.dataset.abs)) return f.dataset.abs;
  return "";
}
function deskModeAvail() {
  /* 原页里时，"版面"这一档**只在当初是从版面切过来**才成立。
     从文献库点开一篇 PDF 时，右边根本没有"重排过的稿面"这回事 ——
     那时挂一个切不过去的「版面」，点下去就是空白，比不挂更糟。 */
  if (ORIG.on) return ORIG.from === "doc";
  return !!deskOrigAbs();
}
function deskModePaint() {
  const box = $("#deskMode");
  if (!box) return;
  box.hidden = !deskModeAvail();
  const cur = ORIG.on ? "orig" : "doc";
  ["doc", "orig"].forEach(m => {
    const b = $("#deskMode" + (m === "doc" ? "Doc" : "Orig"));
    if (b) b.classList.toggle("on", m === cur);
  });
}
/* 切到原页：用这份稿子里那可渲染的原件（deskOrigAbs 是唯一判据）。 */
function deskSwitchOrig() {
  const abs = deskOrigAbs();
  if (!abs) return;
  /* 标题优先用产出物清单里的**名字**（那是文件的真名），取不到才从路径里刨。
     路径刨出来的可能只是文件名，但对"我正在看哪一份原件"这个问题已经够用了。 */
  const nm = DESK.docName || (abs.split(/[\\/]/).pop() || "原页");
  openOrig({ file: abs, title: nm, from: "doc" });
}
/* 切回版面。**必须把原页那一路的状态留着**（ORIG.rendered / n / zoom）：
   切回去再切回来还要重渲一遍，用户会以为原页坏了。deskExitOrig 清状态是给
   "换一份全新的稿子"用的，这里只做显示切换。 */
function deskSwitchDoc() {
  const box = $("#deskOrig"); if (box) box.hidden = true;
  const t = $("#deskText"); if (t) t.hidden = false;
  const toc = $("#deskToc");
  if (toc) toc.hidden = !(toc.querySelector("button[data-go]") || toc.querySelector("a.ref"));
  ORIG.on = false;                                 // 「当前看的是哪一档」归位
  $("#deskTitle").textContent = DESK.title || "交付正文";
  $("#deskMeta").textContent = DESK.meta || "";
  deskModePaint();
  $("#desk").dataset.view = "doc";
}
function closeDesk() {  $("#desk").dataset.open = "0";
  $("#btnDesk").classList.remove("on");
  $("#deskFind").classList.remove("on");
  $("#deskFindBtn").classList.remove("on");
  const mp = $("#deskMorePanel");
  if (mp && !mp.hidden) { mp.hidden = true; $("#deskMore").setAttribute("aria-expanded", "false"); $("#deskMore").classList.remove("on"); }
}
/* 新稿到了，但面板正开着 —— 换上；面板没开就什么都不做。
   稿子没变（比如这次跑挂了）也不重画：正在读的人不该被弹回第一行。 */
function deskSync() {
  const d = $("#desk");
  if (!d || d.dataset.open !== "1") return;
  const p = deskPayload(LAST_RESULT);
  if (p.text === DESK.text) return;
  openDesk(p);
}
function deskInit() {
  const desk = $("#desk"), text = $("#deskText");
  /* 字号是阅读偏好，留在本地记住；右栏的**几何**（宽度、开合）按 DSH「布局刷新即重置」
     —— 不落 localStorage，每次进来都从「未定宽」开始，首次开算 45%。 */
  let sc = 1;
  try { sc = parseFloat(localStorage.getItem(DESK_KEY + "-scale") || "1") || 1; } catch (e) {}
  document.documentElement.style.setProperty("--desk-scale", String(sc));

  $("#deskClose").onclick = closeDesk;
  $("#deskFocus").onclick = () => {
    const on = $("#desk").dataset.focus !== "1";
    $("#desk").dataset.focus = on ? "1" : "0";
    $("#deskFocus").setAttribute("aria-pressed", on ? "true" : "false");
    $("#deskFocus").textContent = on ? "分栏" : "专注";
    $("#deskFocus").setAttribute("aria-label", on ? "返回分栏" : "专注阅读");
  };
  $("#deskRun").onchange = e => deskLoadRun(e.target.value);
  /* 版面 / 原页 两档。切回版面走 deskSwitchDoc（只切显示、保留原页状态）；
     切到原页用稿子里那张内嵌图指向的原件。 */
  $("#deskModeDoc").onclick = () => { if (ORIG.on) deskSwitchDoc(); };
  $("#deskModeOrig").onclick = () => { if (!ORIG.on) deskSwitchOrig(); };
  /* 「打开本地论文」：点菜单项 → 唤起系统取文件框 → 选中的那份递进素材区再开原页。
     取文件框被取消时 change 不触发，所以不需要额外的"取消"分支。 */
  $("#deskOpenLocal").onclick = () => {
    const inp = $("#deskLocalFile");
    if (inp) { inp.value = ""; inp.click(); }      // 清空：连选同一个文件两次也要触发
  };
  const lf = $("#deskLocalFile");
  if (lf) lf.onchange = e => {
    const f = e.target.files && e.target.files[0];
    if (f) deskOpenLocalFile(f);
  };
  $("#deskFontUp").onclick = () => deskScale(0.08);
  $("#deskFontDown").onclick = () => deskScale(-0.08);
  $("#deskCopy").onclick = () => copyText(DESK.text || "");
  $("#deskDl").onclick = () => downloadText(DESK.text || "", (DESK.title || "draft").replace(/[\\/:*?"<>|]/g, "_") + ".md");
  /* 「⋯」菜单：点开/点关；点里面任意一项之后收起来（不然它一直盖在正文上）；
     点别处也收。关面板时一并收 —— 否则下次开面板它还开着。 */
  const more = $("#deskMorePanel");
  const moreBtn = $("#deskMore");
  const setMore = on => {
    more.hidden = !on;
    moreBtn.setAttribute("aria-expanded", on ? "true" : "false");
    moreBtn.classList.toggle("on", on);
  };
  moreBtn.onclick = e => { e.stopPropagation(); setMore(more.hidden); };
  more.onclick = e => { if (e.target.closest("button,select")) setMore(false); };
  document.addEventListener("click", e => {
    if (!more.hidden && !more.contains(e.target) && e.target !== moreBtn) setMore(false);
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") setMore(false); });
  $("#btnDesk").onclick = () => {
    if (desk.dataset.open === "1") return closeDesk();
    openDesk(Object.assign(deskPayload(LAST_RESULT), { restore: true }));
  };

  /* ══ 收件箱的入口与关闭 ══
     打开时才请求系统通知权限（见 notifySystem 的三条克制）——
     请求放在"用户明确点了收件箱"之后，而不是进页面就弹。
     被拒也照常用：面板本身不依赖权限。 */
  $("#btnInbox").onclick = async () => {
    const p = $("#inbox");
    if (p && p.dataset.open === "1") return closeInbox();
    openInbox();
    try {
      if (typeof Notification !== "undefined" && Notification.permission === "default") {
        await Notification.requestPermission();
      }
    } catch (e) {}
  };
  $("#btnInboxClose").onclick = closeInbox;
  /* Esc 关面板：与稿件台等其它浮层保持同一个手感。
     ★ 只在面板开着时才吃这个键 —— 否则会把 Esc 从别处抢走。 */
  document.addEventListener("keydown", ev => {
    if (ev.key !== "Escape") return;
    const p = $("#inbox");
    if (p && p.dataset.open === "1") { closeInbox(); ev.stopPropagation(); }
  }, true);
  // 首屏刷新一次：刷新页面之后，上一轮留下的未读仍然要在。
  paintInbox();

  // 拖拽调宽
  let drag = null;
  $("#deskGrip").onmousedown = e => {
    drag = { x: e.clientX, w: desk.getBoundingClientRect().width };
    desk.classList.add("dragging");
    document.body.style.userSelect = "none";
    e.preventDefault();
  };
  window.addEventListener("mousemove", e => {
    if (!drag) return;
    // 无过渡延迟：deskSetW 直接写 --desk-w，.dragging 期间 CSS 已关动画；
    // 每次移动都按当前侧栏/视口重新夹上限，中间保底 400 始终成立。
    deskSetW(drag.w - (e.clientX - drag.x));
  });
  window.addEventListener("mouseup", () => {
    if (!drag) return;
    drag = null;
    desk.classList.remove("dragging");
    document.body.style.userSelect = "";
  });

  // 查找
  $("#deskFindBtn").onclick = () => {
    const f = $("#deskFind"), on = !f.classList.contains("on");
    f.classList.toggle("on", on);
    $("#deskFindBtn").classList.toggle("on", on);
    if (on) $("#deskFindInput").focus(); else deskFind("");
  };
  let ft = null;
  $("#deskFindInput").oninput = e => {
    const v = e.target.value.trim();
    clearTimeout(ft);
    ft = setTimeout(() => deskFind(v), 160);
  };
  $("#deskFindInput").onkeydown = e => {
    if (e.key === "Enter") { e.preventDefault(); deskHit(DESK.hit + (e.shiftKey ? -1 : 1)); }
    if (e.key === "Escape") { e.preventDefault(); $("#deskFindBtn").click(); }
  };
  $("#deskFindPrev").onclick = () => deskHit(DESK.hit - 1);
  $("#deskFindNext").onclick = () => deskHit(DESK.hit + 1);

  // 大纲跳转（事件委托 —— 大纲是每次渲染重建的）
  $("#deskToc").onclick = e => {
    const b = e.target.closest("button[data-go]");
    if (!b) return;
    const el = DESK.nodes[+b.dataset.go];
    if (el) el.scrollIntoView({ block: "start", behavior: "smooth" });
  };

  // 稿件台正文的两类可点物（事件委托 —— 正文每次渲染重建，钩子只能挂容器上）：
  // ① 引注角标 [n] → 跳到参考文献里那条。找不到那条文献时**原样不动**：
  //    稿子可能引了正文里没列的号，跳不了就别乱跳，更不能弹一个"找不到"去打断
  //    阅读 —— 角标本身还在，语义没丢。
  // ② 内嵌图（figure.dfig）：**图就是图，放大看它就是尽头。**
  //    ★ 这里原来无条件调 openOrig —— 而 /api/orig 只开 PDF 与能转 PDF 的文档，
  //      内嵌图实测是 .png ⇒ 每一次点图都得到「只开 PDF…收到的是 .png」。
  //      活页器不该被拿来当图片查看器：分页、缩放、缩略图带对一张插图全是噪音。
  //      所以：**可渲染的文档**才走原页；图片类在原地放大（再点一下还原）。
  $("#deskInner").onclick = e => {
    const c = e.target.closest && e.target.closest("sup.dcite");
    if (c) {
      const ref = document.querySelector('#deskInner p.p-ref[data-ref="' + c.dataset.n + '"]');
      if (ref) {
        ref.scrollIntoView({ block: "center", behavior: "smooth" });
        ref.classList.remove("drefhit");
        void ref.offsetWidth;            // 重启动画：连点两个不同角标时各闪各的
        ref.classList.add("drefhit");
      }
      return;                            // 点的是角标就到此为止，别让同一下落进图块分支
    }
    const fg = e.target.closest && e.target.closest("figure.dfig");
    if (!fg || !fg.dataset.abs) return;
    if (deskIsOrigDoc(fg.dataset.abs)) {
      openOrig({ file: fg.dataset.abs, title: fg.dataset.name || "原页", from: "doc" });
      return;
    }
    /* 图片：原地放大/还原。整块 figure 上开关，图与注一起放大不影响正文重排
       （改的是这一块的宽度上限，不碰字号与行距）。 */
    fg.classList.toggle("zoom");
    const im = fg.querySelector("img");
    if (im) im.title = fg.classList.contains("zoom") ? "点一下还原" : "点一下放大";
  };

  // 阅读进度 + 大纲高亮（"我读到哪了"是长稿的刚需）
  // 续读位置顺手在这里记（防抖 400ms —— 滚动是连续事件，逐条写 localStorage 是浪费）。
  let posT = null;
  text.addEventListener("scroll", () => {
    const max = text.scrollHeight - text.clientHeight;
    $("#deskProg").style.width = (max > 0 ? Math.min(100, Math.round(text.scrollTop / max * 100)) : 100) + "%";
    if (posT) clearTimeout(posT);
    posT = setTimeout(() => deskPosSave(DESK.runId, max > 0 ? text.scrollTop / max : 0), 400);
    if (!DESK.hasH) return;
    const top0 = text.getBoundingClientRect().top;
    let cur = -1;
    for (let i = 0; i < DESK.nodes.length; i++) {
      const n = DESK.nodes[i];
      if (n && DESK.blocks[i] && DESK.blocks[i].k === "h" &&
          n.getBoundingClientRect().top - top0 <= 72) cur = i;
    }
    $("#deskToc").querySelectorAll("button[data-go]").forEach(b => {
      b.classList.toggle("on", cur >= 0 && +b.dataset.go === cur);
    });
  });

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && desk.dataset.open === "1") closeDesk();
  });

  // 桌面窗口变窄时重新夹一次；全屏浮层只重算原页适宽，不改桌面记忆。
  window.addEventListener("resize", () => {
    if (desk.dataset.open !== "1") return;
    if (window.innerWidth <= 880) {
      if (ORIG.on) origApplyWidth();
      return;
    }
    if (!deskRoomOk()) { closeDesk(); return; }
    if (DESK_W_SET) deskSetW(DESK_W);
  });
  // DSH「布局刷新即重置」：不再按 localStorage 自动展开；首屏右栏收起，
  // 用户点「读全稿 / 稿件台」或某次跑完时按需打开 —— 打开时算 45% 首开宽。
  origWire();
}
/* ── 原页阅读的控件接线（与正文那套并存，只在原页模式下生效） ── */
function origWire() {
  const st = $("#origStage");
  $("#origPrev").onclick = () => origGo(ORIG.n - 1);
  $("#origNext").onclick = () => origGo(ORIG.n + 1);
  const jump = () => {
    const v = parseInt($("#origPage").value, 10);
    if (!isNaN(v)) origGo(v); else $("#origPage").value = String(ORIG.n);
  };
  $("#origPage").onchange = jump;
  $("#origPage").onkeydown = e => { if (e.key === "Enter") { jump(); e.preventDefault(); } };
  /* 缩放基准 = 原图宽（ORIG.natW）。当前显示宽只有在原图宽还没量到时才兜底，
     而且**兜底值要先折算成倍率**，否则 ORIG.zoom 会被写成一个像素数，
     下次按倍率乘就飞了。 */
  const zoomBase = () => {
    if (ORIG.natW) return { base: ORIG.natW, zoom: ORIG.zoom || 1 };
    const shown = $("#origImg").getBoundingClientRect().width || 0;
    const nat = $("#origImg").naturalWidth || shown || 1;
    return { base: nat, zoom: shown ? shown / nat : 1 };
  };
  $("#origZoomIn").onclick = () => {
    if (!ORIG.rendered) return;
    const b = zoomBase();
    ORIG.zoom = Math.min(4, Math.max(0.1, (ORIG.zoom || b.zoom) * 1.25));
    origApplyWidth();
  };
  $("#origZoomOut").onclick = () => {
    if (!ORIG.rendered) return;
    const b = zoomBase();
    ORIG.zoom = Math.max(0.15, Math.max(0.1, (ORIG.zoom || b.zoom) * 0.8));
    origApplyWidth();
  };
  $("#origFit").onclick = () => { ORIG.zoom = ORIG_FIT; origApplyWidth(); };
  $("#origRaw").onclick = () => {
    const q = ORIG.kb ? "kb=" + encodeURIComponent(ORIG.kb)
                      : "file=" + encodeURIComponent(ORIG.src);
    window.open("/api/orig/raw?" + q, "_blank");
  };
  /* 图一加载出来就记下它的原始像素宽 —— 放大要按原图算，不能按当前显示宽算
     （否则每点一次 + 都会把上一次的显示宽当基准，几下就飘到天上去）。 */
  $("#origImg").onload = () => {
    const im = $("#origImg");
    if (im.naturalWidth && im.naturalWidth !== ORIG.natW) {
      ORIG.natW = im.naturalWidth;
      if (ORIG.zoom) origApplyWidth();
    }
    origMarkFilm();
  };
  /* 方向键翻页：只在原页模式且焦点不在输入框里时生效，
     否则用户在页码框里按左右键会被抢走。 */
  document.addEventListener("keydown", e => {
    if (!ORIG.on || !ORIG.rendered) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
    if (e.key === "ArrowRight" || e.key === "PageDown") { origGo(ORIG.n + 1); e.preventDefault(); }
    else if (e.key === "ArrowLeft" || e.key === "PageUp") { origGo(ORIG.n - 1); e.preventDefault(); }
  });
  /* 面板宽度变了，"适宽"必须重算 —— 拖动右栏时会一直触发。 */
  window.addEventListener("resize", () => { if (ORIG.on) origApplyWidth(); });
}

/* ═════════════ 登录：提交与启动自检 ═════════════
   判据全在服务端（GET /api/auth）。前端**不自己推断**这个控制台该不该锁 ——
   "账户有没有设密码"这件事前端够不到，猜出来的结果只会在两种情况下错：
   设了密码却没锁（漏），或者没设密码却弹窗（把用户关在自己工具外面）。 */
async function submitLogin(e) {
  if (e) e.preventDefault();
  const pw = $("#loginPw");
  const err = $("#loginErr");
  const btn = $("#loginBtn");
  const val = pw ? pw.value : "";
  if (err) { err.hidden = true; err.textContent = ""; }
  if (!val) {
    /* 空密码是**合法值**（有人就这么设），所以不能在前端拦。
       真正的判据在服务端：它会告诉我们"密码不对"。 */
  }
  if (btn) { btn.disabled = true; btn.textContent = "登录中…"; }
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: val }),
    });
    const t = await r.text();
    let j; try { j = JSON.parse(t); } catch (e2) { j = { error: t.slice(0, 200) }; }
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    setToken(j.token || "");
    NEED_LOGIN = false;
    hideLoginGate();
    /* 登录成功之后把这一页该加载的重跑一遍 —— 之前那些请求全是 401，
       各个面板都还是空的。先 `location.reload()` 更稳妥：省得去追
       "哪几个 loadXxx 需要在登录后再调一次"，那清单一定会漂。 */
    location.reload();
  } catch (e2) {
    if (err) {
      err.hidden = false;
      err.textContent = e2.message || "登录失败";
    }
    if (pw) { try { pw.select(); } catch (e3) { pw.focus(); } }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "登录"; }
  }
}

async function checkLoginGate() {
  let j = null;
  try {
    /* ★ 这里必须用 `_rawFetch`（绕开 401 自动弹层，否则会递归），
       所以也**必须自己带令牌** —— 否则刷新后这一问永远匿名，
       结果是"令牌明明存着，却每次刷新都被要求重新登录"。
       这是实测抓到的：服务端认令牌，浏览器却不带它。 */
    const h = {};
    const t = getToken();
    if (t) h["X-Rongke-Token"] = t;
    const r = await _rawFetch("/api/auth", { cache: "no-store", headers: h });
    if (!r.ok) return;                      /* 问不出来就不锁 —— 别把用户关在外面 */
    j = await r.json();
  } catch (e) {
    return;                                  /* 服务端还没起来之类的，交给别的提示 */
  }
  NEED_LOGIN = !!j.login_required && !j.signed_in;
  if (NEED_LOGIN) showLoginGate("这个控制台设了本地密码。");
  else hideLoginGate();
}
