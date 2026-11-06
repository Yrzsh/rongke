const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");

/* ★ 2026-10-28 · title 属性的单行摘要。
   实测缺陷：会话卡的 .ttl 把用户请求**原文**当 title，最长一条 5166 字符 / 170 行。
   原生 tooltip 把 \n 当普通字符，170 行挤成一行再截断 —— 越长越读不了。
   全文去处是点开标题行的 .procb > .kv（逐字原文），不靠悬停。
   所以 title 只回答「这一行被截掉了什么」：折成单行、限 120 字。 */
const ttl = (s, max) => {
  const n = max || 120;
  const one = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  if (one.length <= n) return one;
  return one.slice(0, n) + "…（点开看全文）";
};

/* ★ 2026-10-28 · 折叠卡的标题要显示**人写的任务**，不是子代理模板的开头。
   实测缺陷：产出页 5 张折叠卡里有 4 张前 24 个字符完全相同 ——
   goal 是内核拼的模板（【你的角色】→【角色指令】→【这次的硬约束】→【任务】），
   人写的那段在末尾，被前面三段固定文字挤出了可见区（只露得下约 20 字）。
   折叠的目的是「扫一眼知道哪张是哪张」，模板开头做不到这件事。

   ★ 只影响**标题行**。展开体 `.procb > .kv` 仍是 goal 全文，一字不改 ——
   可追溯性靠那一段，不靠标题。
   非子代理任务（"子代理任务"不在 goal 里）原样返回，一个字符都不动。 */
const TASK_RE = /【任务】\s*\n?/g;
const humanTask = s => {
  const g = String(s == null ? "" : s);
  if (g.indexOf("子代理任务") < 0) return g;        // 普通任务：原样
  TASK_RE.lastIndex = 0;
  let m, last = null;
  while ((m = TASK_RE.exec(g))) last = m;           // 取**最后**一个【任务】
  if (!last) return g;                              // 没有任务段：原样
  const tail = g.slice(last.index + last[0].length).trim();
  return tail || g;
};

/* ═════════════ 登录令牌（必须在任何 fetch 之前装好）═════════════
   为什么包装原生 `fetch` 而不是改每个调用点：
   这一页有 20+ 处 `fetch`，逐个加请求头一定会漏一处，而漏掉的那处
   在"设了密码"的用户那里就是**静默 401** —— 表现为某个面板永远转圈。

   ★ 令牌存 localStorage 而不是 cookie：这一页是单文件、同源，
     localStorage 一行读写就够；cookie 要处理 Path/SameSite/端口差异，
     全与需求无关。令牌本身只活在控制台进程的内存里（见 billing.issue_token），
     浏览器这边存一份只是"带上它"。
   ★ 401 统一在这里处理：清令牌 + 弹登录层。任何一处调用都不用自己管。 */
const TOKEN_KEY = "rongke_token";
let NEED_LOGIN = false;
/* 授权状态缓存在这里，供两处用：导航徽标与「被拦下时弹层」。
   与 NEED_LOGIN 分开：登录问的是"你是谁"，授权问的是"你买没买"，
   两者是两回事，混用一个标志会让弹错层。 */
let LICD = null;
let NEED_LICENSE = false;

/* 登录层显隐。用 `function` 声明（不是 const 箭头），这样它会被提升 ——
   下面 fetch 包装器里可能在脚本还没走到定义处就调用它。 */
function showLoginGate(why) {
  const g = $("#loginGate");
  if (!g) return;
  const w = $("#loginWhy");
  if (w) w.textContent = why || "这个控制台设了本地密码。";
  g.hidden = false;
  /* 聚焦密码框：这一层出现时用户唯一能做的事就是输密码，
     不该还要再点一下输入框。`preventScroll` 防止页面跳动。 */
  const pw = $("#loginPw");
  if (pw) { try { pw.focus({ preventScroll: true }); } catch (e) { pw.focus(); } }
}
function hideLoginGate() {
  const g = $("#loginGate");
  if (g) g.hidden = true;
  const pw = $("#loginPw");
  if (pw) pw.value = "";
  const e = $("#loginErr");
  if (e) { e.hidden = true; e.textContent = ""; }
}

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}
function setToken(t) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (e) { /* 隐私模式下 localStorage 会抛，不能因此炸掉整页 */ }
}

const _rawFetch = window.fetch.bind(window);
window.fetch = async function (input, init) {
  const o = Object.assign({}, init || {});
  const t = getToken();
  if (t) {
    o.headers = Object.assign({}, o.headers || {});
    /* 用 Headers 对象时要走 set，普通对象直接赋值 —— 两种都有人传 */
    if (o.headers instanceof Headers) o.headers.set("X-Rongke-Token", t);
    else o.headers["X-Rongke-Token"] = t;
  }
  const r = await _rawFetch(input, o);
  /* 401 只对 /api/ 处理：静态资源不会 401，登录接口自己的 401 是"密码不对"，
     那个要留给调用方显示原因，不能在这弹登录层。 */
  const url = String(input && input.url ? input.url : input);
  if (r.status === 401 && url.indexOf("/api/") >= 0
      && url.indexOf("/api/login") < 0) {
    setToken("");
    NEED_LOGIN = true;
    showLoginGate("登录已失效，请重新登录");
  }
  return r;
};

async function api(p, o) {
  /* ★ 死线。这里是全站共用入口，一处改就能把所有走 `api()` 的
     路径一起护住。原来裸 `fetch` 没有超时：一条路由挂住（不报错、
     不应答）时，那块界面永远停在「读取中…」—— 看起来在努力，实际已死，刷新也一样。
     实测死在这两处：文献库的库况四格、全库清单的「展开全部」。
     ★ 仍然**抛错**：调用点已有 40+ 处 catch，抛错是既有契约；只把 name 标成
       `TimeoutError`，让想分辨的调用方能分辨。 */
  const LIMIT = Number(o && o.timeout) || 12000;
  const AC = typeof AbortController === "function" ? new AbortController() : null;
  const cut = AC ? setTimeout(() => AC.abort(), LIMIT) : 0;
  let r, t;
  try {
    const oo = Object.assign({}, o || {});
    delete oo.timeout;
    if (AC && !oo.signal) oo.signal = AC.signal;
    /* 字符串体默认按 JSON 发：浏览器对 string body 的默认 Content-Type 是
       text/plain，而服务端的 read_json_object 只认 application/json ——
       漏写头就是 415（文献库「检索候选 / 入库选中」两处都踩过）。
       统一在这里兜底，调用点仍可显式覆盖。 */
    if (typeof oo.body === "string") {
      const h = Object.assign({}, oo.headers || {});
      if (!Object.keys(h).some(k => k.toLowerCase() === "content-type"))
        h["Content-Type"] = "application/json";
      oo.headers = h;
    }
    r = await fetch(p, oo);  t = await r.text();
  } catch (e) {
    if (e && e.name === "AbortError") {
      const err = new Error("这次请求 " + Math.round(LIMIT / 1000) + " 秒内没取回来。");
      err.name = "TimeoutError";
      err.hint = "服务端还在，只是这条路由这一次没答上 —— 重进这页再试；"
               + "一直如此就看控制台的启动日志";
      throw err;
    }
    throw e;
  } finally { if (cut) clearTimeout(cut); }
  let j; try { j = JSON.parse(t); } catch (e) { j = { error: t.slice(0, 300) }; }
  if (!r.ok) {
    /* ★ 把状态码与响应体挂在 Error 上。
       调用方只读 `e.message` 的现状不变（向后兼容），但"授权拦下"(403)
       与"已有任务在跑"(409) 必须能分开 —— 混成一句 message 就没法
       决定该弹激活框还是提示等一会儿。 */
    const err = new Error(j.error || ("HTTP " + r.status));
    err.status = r.status;
    err.body = j;
    throw err;
  }
  return j;
}
/* 「这一条能不能回放」的唯一判据 —— 历史表的入口与稿件台的历次下拉都必须走这里。
   两处各写一份就会漂：曾经下拉列了打不开的条目，点进去只剩后端原文「任务不存在」。 */
function canReplay(r) { return !!(r && r.id && r.replay !== false); }
/* 取不到某一次正文时怎么说。两种情况不许混着报：
   后端说「不存在」= 那条**当时就没落归档**（没存 ≠ 坏了）；别的才是这次请求真没成。
   两条都必须给下一步，且不许写成"加载失败/出错了"这种归因不清的话。 */
function deskMissText(rid, raw) {
  const s = String(raw || "");
  return /不存在|not found/i.test(s)
    ? "「" + rid + "」当时没有落归档快照，所以读不出正文 —— 这是没存，不是出错。" +
      "想知道它做了什么：去侧栏会话列表点这条，看目标 / 步数 / 用时 / 成本；" +
      "要读正文：在「历次…」里换一条（这里只列有归档的）。" +
      "（后端原文：" + s + "）"
    : "「" + rid + "」这次没读到正文 —— 稿件台没坏，是这一趟请求没成。" +
      "在「历次…」里再点它一次；仍不成就去侧栏会话列表看这条的目标 / 步数 / 用时 / 成本。" +
      "（后端原文：" + s + "）";
}
/* 极简 markdown：只做 **粗体**、`代码`、### 小标题、``` 代码围栏 → 代码卡。
   研究文本里的 LaTeX 原样保留。 */
/* ── W14-C · 代码卡（数值照 dsh_ref CodeCard.module.css，只抄数值不 import）──
   ★ Codex 三条承重纪律在这段里的落点（styles.md 原文转写）：
     ① 多数地方用**默认前景色**：只有关键词/注释/diff 行三种 token 有色，
        其余文本吃 .ccard 的 --label-primary，不逐词着色。
     ② **禁止纯黑纯白当前景**：这里出现的每一个前景都是 var()，两档
        解析后都不是 #000/#fff（亮档默认前景 = #0f1115）。
     ③ **对比度对着真正压在其上的底色算**：这些前景压的是
        --markdown-code-block，不是页面底 —— 验收在代码块底色上逐色量。 */
function ccodeHl(line) {
  const t = esc(line);
  if (/^\s*(#|\/\/)/.test(line)) return '<span class="tc">' + t + "</span>";
  return t.replace(/\b(def|class|return|if|elif|else|for|while|import|from|as|with|try|except|finally|lambda|yield|pass|break|continue|and|or|not|in|is|None|True|False|const|let|var|function|new|await|async|export|require|print)\b/g,
    '<span class="tk">$1</span>');
}
function ccardMarkup(lang, title, code) {
  const body = String(code).replace(/\n+$/, "");
  const lines = body.length ? body.split("\n") : [""];
  const isDiff = /^diff$/i.test(lang) || (!lang && lines.some(l => /^(@@|[+-]{3}\s)/.test(l)));
  const html = lines.map(l => {
    if (isDiff && /^\+/.test(l)) return '<div class="cl add">' + esc(l) + "</div>";
    if (isDiff && /^-/.test(l)) return '<div class="cl del">' + esc(l) + "</div>";
    if (isDiff && /^@@/.test(l)) return '<div class="cl hunk">' + esc(l) + "</div>";
    return '<div class="cl">' + (isDiff ? esc(l) : ccodeHl(l)) + "</div>";
  }).join("");
  return '<div class="ccard" data-lang="' + esc(lang || "code") + '">' +
    '<div class="ccard-h"><span class="ccard-head">' +
      '<span class="ccard-lang">' + esc(lang || "code") + "</span>" +
      (title ? '<span class="ccard-title">' + esc(title) + "</span>" : "") +
    "</span>" +
    '<span class="ccard-acts">' +
      '<button class="ccard-act" data-ccopy type="button" title="复制代码">&#8980;</button>' +
    "</span></div>" +
    '<div class="ccard-b">' + html + "</div></div>";
}
/* md() v2 —— 照 ZCode 的消息渲染能力补齐：表格 / 列表 / 链接。
   既有能力一个不摘：围栏代码走 ccardMarkup（带语言标与复制键），
   粗体 / 行内代码 / 小标题照旧。行内规则在 esc 之后跑，链接的 href
   因此是已转义的串 —— 引号已是 &quot;，属性注入不成立。 */
function mdInline(s) {
  let out = s
    /* ★ 图片要先于链接处理，否则 `![](x.png)` 会被下面那条链接规则吃掉。
       实测：这条规则不存在时，稿子里的 `![](img.png)` 会原样显示成一串括号
       —— 用户要的"照片汇报"就变成了占位符。DSH 的 MarkdownText 是真渲图的。 */
    .replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,
      (m, alt, src) => '<img src="' + src + '" alt="' + alt + '" loading="lazy">')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    /* 单星号斜体：期刊稿里 *物种名* / *P* < 0.05 / *spnA* 是**排印规矩**，
       不认就是把一串星号留在正文里给读者看。
       ★ 两侧都要贴边才算一对：开括号后紧跟非空格、闭括号前不接空格 ——
         这样作者行「Jie Rang,* Liqiu Xia, and Zirong Zhu*」（通讯作者的星号紧跟在
         逗号后面）不会被误配成一段斜体，算式 "2 * 3 * 4" 也不会。 */
    .replace(/(^|[\s(（[])\*([^\s*](?:[^*\n]{0,58}[^\s*])?)\*(?=$|[\s.,;:!?)\]】》、。，；：！？\u2018\u2019\u201c\u201d])/g,
      "$1<em>$2</em>")
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>')
    /* 出处标记渲染成胶囊（导师模式那句「哪句来自库、哪句来自外网」在界面上的落点）。
       这里**只做显示**：判定全部在内核的调用台账里算（loop.mentor_trace_audit），
       前端不许因为"看着是外网"就把它藏起来 —— 藏 = 骗人。 */
    .replace(/〔库\s*[·・.、]?\s*([^\s〔〕]{2,60}?)\s*[·、,]?\s*[pP]\.?\s*(\d{1,5})\s*〕/g,
      '<span class="cite" title="文献库内的出处：这条能对上台账里的检索命中">库 · $1 p.$2</span>')
    .replace(/〔外网\s*[·・.、]?\s*([^\s〔〕]{1,80}?)\s*〕/g,
      '<span class="cite net" title="不是文献库里的，是这次联网补的">外网 · $1</span>');
  return out;
}
function md(s) {
  const boxes = [];
  const raw = String(s == null ? "" : s).replace(
    /```[ \t]*(\w*)[ \t]*([^\n]*)\n?([\s\S]*?)```[ \t]*\n?/g,
    (m, lang, title, code) => {
      boxes.push(ccardMarkup(lang, title.trim(), code));
      return "\x00B" + (boxes.length - 1) + "\x00";
    });
  const lines = esc(raw).split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    /* 代码卡占位行原样放行 */
    if (new RegExp("^" + "\x00B" + "\\d+" + "\x00" + "$").test(ln.trim())) { out.push(ln); i++; continue; }
    /* 表格：| 开头的连续块，第二行是 |---| 分隔 */
    if (/^\s*\|.*\|\s*$/.test(ln) && i + 1 < lines.length &&
        /^\s*\|[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => mdInline(c.trim()));
      const head = cells(ln);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
      out.push('<table class="mdtbl"><thead><tr>' +
        head.map(c => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>" +
        rows.map(r => "<tr>" + r.map(c => "<td>" + c + "</td>").join("") + "</tr>").join("") +
        "</tbody></table>");
      continue;
    }
    /* 列表：- / * / 1. 的连续行（有序与无序不混排） */
    const lm = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(ln);
    if (lm) {
      const ordered = /\d/.test(lm[1]);
      const items = [];
      while (i < lines.length) {
        const m2 = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
        if (!m2 || /\d/.test(m2[1]) !== ordered) break;
        items.push("<li>" + mdInline(m2[2]) + "</li>");
        i++;
      }
      out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>"));
      continue;
    }
    /* 小标题（# / ## / ### 同一档） */
    const hm = /^\s*#{1,4}\s+(.+)$/.exec(ln);
    if (hm) { out.push("<h3>" + mdInline(hm[1]) + "</h3>"); i++; continue; }
    /* 空行是块边界 */
    if (!ln.trim()) { out.push(""); i++; continue; }
    /* ★ 段落：连续的普通行收成一个 <p>，行与行之间用空格连（Markdown 的软换行
       语义就是"同一段里折行"）。这一段以前是**每行直接 push 一个裸字符串** ——
       于是段落之间的 16px 间距全靠空行里的换行符撑着，正文一进 `.manu` 就
       成了"一行一行贴在一起、段落分不出"的样子。DSH 的 MarkdownText 是标准
       段落模型（p margin:16px 0），这里补上。
       收进一个 <p> 也顺手修掉另一个隐患：裸字符串被 pre-wrap 渲染时，
       中文之间的多余空白不会被折叠，视觉上像排错了间距。 */
    const para = [];
    while (i < lines.length) {
      const cur = lines[i];
      if (!cur.trim()) break;
      if (/^\s*#{1,4}\s+/.test(cur)) break;
      if (/^\s*\|.*\|\s*$/.test(cur)) break;
      if (/^\s*([-*+]|\d+[.)])\s+/.test(cur)) break;
      if (new RegExp("^" + "\x00B" + "\\d+" + "\x00" + "$").test(cur.trim())) break;
      para.push(cur.trim());
      i++;
    }
    out.push("<p>" + mdInline(para.join(" ")) + "</p>");
  }
  /* 代码卡占位还原（占位行上面已原样放行，这里兜住行内混排的情况） */
  return out.join("\n").replace(/\x00B(\d+)\x00/g, (m, i2) => boxes[Number(i2)]);
}
/* ── 滚动跟随（DSH `useScrollFollow` · 阈值 25px）──
   以前是无条件跳底：用户往上读，下一条事件一到就被拽回去。
   现在只在自己贴底时跟随；上滑即停，给「回到最新」。 */
const FOLLOW_PX = 25;
let FOLLOWING = true;
function nearBottom() {
  const s = $("#scroll");
  if (!s) return true;
  return s.scrollHeight - s.scrollTop - s.clientHeight <= FOLLOW_PX;
}
function paintToBottom() {
  const b = $("#toBottom");
  if (!b) return;
  const flow = $("#sayFlow");
  b.hidden = FOLLOWING || !(flow && flow.children.length);
}
/* ★ 2026-11-06：空态没有对话流，不许「跟随底部」。
   空态 `#v-chat` 占满 852px，sticky 的 `#composer` 又占 248px ⇒ #scroll 可滚
   248px；而 FOLLOWING 初值 true，一聚焦输入框（pointerdown → rAF）followBottom
   就把 scrollTop 0→248，把 hero 标题整体推出视口；`scroll-behavior:smooth`
   让它看起来就是用户说的「一直往上跑」。跟随底部只在**有会话内容**时成立。
   ⚠️ 别改成「把 FOLLOWING 初值设 false」：那只把触发推迟，用户一滚到底照样跳；
     判据量的是「空态下这个函数根本不滚」，且会话态仍能滚（见 hero_scroll_test）。 */
function emptyViewVisible() {
  const e = $("#empty"), v = $("#v-chat");
  return !!(e && !e.hidden && v && v.classList.contains("on"));
}
function followBottom(force) {
  if (force) FOLLOWING = true;
  const s = $("#scroll");
  if (!s) return;
  if (emptyViewVisible()) {
    /* 不滚、也不留 data-follow（那条规则会关掉 overflow-anchor）。
       paintToBottom() 仍要跑，让「回到最新」按真实状态显隐：空态 flow 为空 ⇒ 隐藏。 */
    delete s.dataset.follow;
    paintToBottom();
    return;
  }
  if (FOLLOWING) {
    s.scrollTop = s.scrollHeight;
    s.dataset.follow = "1";
  } else {
    delete s.dataset.follow;
  }
  paintToBottom();
}
const scroll = () => followBottom(false);
$("#scroll").addEventListener("scroll", () => {
  const was = FOLLOWING;
  FOLLOWING = nearBottom();
  if (was !== FOLLOWING) followBottom(false);
}, { passive: true });
["wheel", "touchstart", "pointerdown"].forEach(ev => {
  $("#scroll").addEventListener(ev, () => {
    requestAnimationFrame(() => { FOLLOWING = nearBottom(); followBottom(false); });
  }, { passive: true });
});
$("#toBottom").onclick = () => {
  if (emptyViewVisible()) return;   // 与 followBottom 同一道门，别留后门
  followBottom(true);
  const s = $("#scroll");
  if (s) s.scrollTo({ top: s.scrollHeight, behavior: "smooth" });
};

/* ═════════════ 主题 ═════════════ */
function currentTheme() { return document.documentElement.dataset.theme || "light"; }
function setTheme(v, manual) {
  document.documentElement.dataset.theme = v;
  document.documentElement.dataset.themeSrc = manual ? "manual" : "system";
  try { manual ? localStorage.setItem("rongke-theme", v) : localStorage.removeItem("rongke-theme"); }
  catch (e) {}
  paintThemeBtn(); paintTop();
}
function paintThemeBtn() {
  const src = document.documentElement.dataset.themeSrc;
  const dark = currentTheme() === "dark";
  $("#themeIcon").textContent = dark ? "☾" : "☀";
  $("#themeLabel").textContent = src === "manual" ? (dark ? "深色" : "浅色") : "跟随系统";
}
$("#btnTheme").onclick = () => {
  setTheme(currentTheme() === "dark" ? "light" : "dark", true);
  paintTop();
};
if (window.matchMedia) {
  try {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", e => {
      if (document.documentElement.dataset.themeSrc !== "manual")
        setTheme(e.matches ? "dark" : "light", false);
    });
  } catch (e) {}
}

/* ═════════════ 状态 ═════════════ */
let STATUS = {}, STATUS_READY = false, STATUS_RETRY = 0,
    ES = null, CUR = null, T0 = 0, round = null, LIVE = false, DRAFT = null;
/* 当前对话轮（DSH 的 Turn）：过程组 / 结果卡 / 介入卡都挂在它身上。
   一个用户输入 = 一个 turn.me + 一个 turn.ai；追问追加一对，不碰旧的。 */
let TURN = null, TURN_SEQ = 0;
let REPLAY_FOLLOWUPS = [], REPLAY_FU_I = 0;
let TIMER = null, RECONN = 0;
let MAXSTEPS = null;          // 本次任务的步数上限（服务端默认 40）
let LASTGOAL = "";            // 最近一次目标 —— "改目标重跑" 用
let REJECTS = 0, STALLS = 0;  // 打回 / 打转计数（"要你定夺"靠它触发）
let ASK = null;               // 当前挂起的"问人"：内核正**停下来等**你回答
// 这份结果对应的任务，磁盘上还有没有可续跑的对话快照？
// 它决定"接着上次跑"那个按钮出不出现 —— **给了却点不通的按钮，比没有按钮更伤信任**。
let RESUMABLE = false;

/* 过程默认展开？默认**不展开** —— 界面默认要回答的是"推进到哪"，
   不是"模型说了什么"。想常看的在「设置 → 通用」里钉住（侧栏那颗开关已撤：
   与设置页 #setProc 重复，两份开关就是两个真相）。 */
let PROCOPEN = false;
try { PROCOPEN = localStorage.getItem("rongke-proc") === "1"; } catch (e) {}
function procTurn() {
  // 设置页开关在启动时就会装（那时一个 turn 都还没有）——必须判空。
  return TURN;
}
function applyProcOpen() {
  const t = procTurn();
  if (!t) return;
  const open = PROCOPEN || transcriptView() === "verbose";
  t.proc.dataset.open = open ? "1" : "0";
  t.head.setAttribute("aria-expanded", open ? "true" : "false");
}
function toggleProc() {
  const t = procTurn();
  if (!t) return;
  const nowOpen = t.proc.dataset.open !== "1";
  t.proc.dataset.open = nowOpen ? "1" : "0";
  t.head.setAttribute("aria-expanded", nowOpen ? "true" : "false");
  /* 用户**手动**关掉之后，就别再自动弹开了（一次性闩锁置位）——
     自动打开是帮忙，反复打开是较劲。 */
  if (!nowOpen) AUTOOPENED = true;
}
function procIsOpen() {
  const t = procTurn();
  return !!(t && t.proc.dataset.open === "1");
}
/* 通用折叠行：给"主行说人话、原始诊断收进详情"那一类地方用（W15-A 任务 2）。
   与 toggleProc 同一条机制（data-open + .proc[data-open="0"] .procb{display:none}），
   只是不认死 id —— 谁点就开谁那一层。 */
function toggleDetail(h) {
  const w = h.parentNode;
  if (w) w.dataset.open = w.dataset.open === "1" ? "0" : "1";
}
/* 折叠件那一行的指头槽。**一处定义**，.proch 的所有产出方共用 ——
   这样"看起来像开关"这件事只有一个地方负责，不会一页一页地漏。
   两个字符都留在 DOM 里、靠 CSS 交叉淡换（`.proch:hover .ci/.ch`）：
   收起时 ▶ 指右，悬停换成 ›（同 DSH DisclosureRow 的 iconIdle / chevronHover）。 */
function chevSlot() {
  return '<span class="chev"><span class="ci">▶</span><span class="ch">›</span></span>';
}
function detailRow(title, inner) {
  return '<div class="proc disclosure" data-open="0">' +
    '<div class="proch row" onclick="toggleDetail(this)">' +
      chevSlot() + '<span class="title">' + esc(title) + "</span></div>" +
    '<div class="procb">' + inner + "</div></div>";
}
/* 注意：**不在这里**给 #procHead 挂 onclick —— 外壳第一次开工才建（resetThread），
   启动时它还不存在。挂事件的地方紧挨着建它的地方，才不会漂移。 */

/* ═════════════ 导航 ═════════════ */
/* 「历史」已并入侧栏会话列表 —— 这里不再有 hist 视图。
   chat 是默认视图，由「新对话」与会话行进入（路由锚点按钮不可见但仍在）。 */
/* ══ 页名与副标题（2026-10-26 信息层级重排）══
   格式：[页名, 副标题]。副标题由 goView() 拼进顶栏 `标题 · 副标题`，
   也由各页自己的 <h2>/<div class="sub"> 再写一遍（页内那份更详细）。
   ★ 改的是**用词，不是路由**：左边这些键是 data-v 的值、VIEWS 的键、
     goView() 与下面 loadXxx() 分派共用的同一批 id，动一个就断一处。
   ★ 用词口径：用户对着 Paper Workbench 说「功能很不清晰」。那边每页副标题
     都是"你能拿它干什么"的人话（「图表清单 · 一键生成 · 重绘 · 上传替换」），
     荣珂这边原来是"这台机器内部怎么样了"（「它的本事清单与现在的状态」）。
     同一件事换个主语就清晰了 —— 改的全是主语，没有一条功能变化。 */
const VIEWS = { chat:["新任务",""],
                traj:["执行记录","每一步查了什么、凭什么这么说"],
                cap:["本事清单","它能干什么、哪些真跑得通"],
                gate:["质量检查","交付前它自己核过哪几道"],
                tools:["科研工具","查文献 / 核 DOI / 画图 / 排 PPT"],
                kb:["文献库","你手上的 PDF，能查到原页段落"],
                mentor:["导师与课题","管理研究问题、课题台账和本课题文献"],
                deliv:["成果文件","做出来的东西都在这，点开看全稿"],
                fig:["图片与图表","配过的图和它们的体检报告"],
                setup:["环境检查","画图和做 PPT 要用的工具配好了没有"],
                lic:["试用与激活","试用还剩多久、粘贴激活码"],
                set:["设置","外观 / 模型 / 配置来源"] };
function setAdvancedNav(open) {
  const panel = $("#advancedNav"), btn = $("#btnAdvancedNav");
  panel.hidden = !open;
  btn.setAttribute("aria-expanded", open ? "true" : "false");
}
$("#btnAdvancedNav").onclick = () => setAdvancedNav($("#advancedNav").hidden);
document.querySelectorAll(".navbtn[data-v]").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll(".navbtn[data-v]").forEach(x => {
      x.classList.remove("on"); x.removeAttribute("aria-current");
    });
    b.classList.add("on"); b.setAttribute("aria-current", "page");
    const v = b.dataset.v;
    if ($("#advancedNav").contains(b)) setAdvancedNav(true);
    document.querySelectorAll(".view").forEach(x => x.classList.remove("on"));
    $("#v-" + v).classList.add("on");
    $("#scroll").scrollTop = 0;
    $("#composer").style.display = v === "chat" ? "" : "none";
    if (v === "chat") {
      $("#title").textContent = CUR ? CUR.goal : "新任务";
      delete $("#title").dataset.quiet;
    }
    else { $("#title").textContent = VIEWS[v][0]; $("#title").dataset.quiet = "1"; }
    if (v === "tools") loadTools();
    if (v === "kb") {
      if (window.RongkeLibrary) RongkeLibrary.refresh();
      else loadKb();
    }
    if (v === "mentor" && window.RongkeMentor) RongkeMentor.openManager();
    if (v === "cap") loadCap();
    if (v === "gate") loadGate();
    if (v === "deliv") loadDeliv();
    if (v === "fig") { loadAn(); loadFig(); }
    if (v === "setup") loadSetup();
    if (v === "lic") loadLic();
    if (v === "set") loadSet();
    /* 窄屏是覆盖层：选完页就该让路，不然侧栏一直压在内容上看着像"页面没反应"。 */
    if (isNarrow()) setFold(true);
  };
});
function goView(v) { document.querySelector('.navbtn[data-v="' + v + '"]').click(); }
/* ══ 折叠组接线已撤（2026-10-26 信息层级重排）══
   原来这里有两处：给「工具与档案」那个折叠按钮挂 onclick，以及
   用 bindFold() 把「更多」按钮与它的子容器连起来。
   两个折叠组（工具与档案 / 更多）改成平铺后，按钮与子容器都不在了。
   ★ 必须**删掉**而不是留着：那两个按钮的 id 已经不存在，按 id 取元素拿到
     null，`.onclick = …` 会抛 TypeError；而这一段是脚本顶层语句 —— 抛了
     就中断后面全部初始化（会话列表、斜杠命令、状态栏都建不起来），
     页面看起来"能用一半"。
     （注释里不写「$(井号 + id)」这种字面量：ui_check 的「JS 引用的元素都存在」
      是扫源码字面量的，写在注释里也会被判成缺失引用。）
   ★ 不写成 `if (el) el.onclick = …` 兜底：那样留着一份永远不执行的死代码，
     下一个人会以为折叠还在、去调 bindFold。要恢复折叠就照侧栏那段注释重做。 */
/* ═════════════ 会话列表（侧栏）═════════════
   数据单源是 /api/runs（服务端从磁盘现算）；「正在跑」的行由前端补一条 ——
   它还没有归档，但它是此刻最重要的一行，不能等跑完才出现。 */
/* ═════════════ 历史列表（侧栏，唯一一份）═════════════
   数据单源是 /api/runs（服务端从磁盘现算）；「正在跑」的行由前端补一条 ——
   它还没有归档，但它是此刻最重要的一行，不能等跑完才出现。

   ★ 2026-10-21：原来这里只查 30 条，而合并后的列表要承担原来「成品」区的
     「让我看见我有过什么」职责（成品区已撤）。实测本机 50 个归档、
     `/api/runs?n=100` 只要 0.15s / 109KB（服务端只读 session.json 的头几项，
     不读 events），所以取 100 条 —— 历史被截在 30 条，就是又一次"记录变少了"。 */
let SESS = [];
/* 上一次拉历史失败的原因。null = 这一次读到了（哪怕读到的是空）。
   ★ 必须与 SESS 分开：SESS 空既可能是「真没有」，也可能是「没读到」，
     这两件事给用户的话**完全不同**，混成一个空列表就是在说谎。 */
let SESS_ERR = null;
/* 历史列表的显隐档位（照 DSH `ui-workspace` 的「视图选项」三选一）：
     "mine"   默认 —— 隐藏子代理归档
     "all"    全部对话（显示子代理归档）
   DSH 原文：「视图选项以一组显式三选一控制显隐：**隐藏已归档（默认项）**隐藏
   已归档 Session，**全部对话（显示已归档）**将其纳入列表，**仅显示已归档**则隐藏
   普通 Session」。
   ★ 这一档必须有：只把 19 条子代理收起而不给任何入口，等于**无声隐藏用户的记录**
     —— 那正是 DSH 明确要避免的（它的归档行虽然默认隐藏，但筛选器一直点得到，
     且显示时置灰 + 无障碍说明「取消归档后才能打开」）。 */
let SESS_MODE = "mine";
function sessFiltered(rs) {
  return SESS_MODE === "all" ? rs.slice() : rs.filter(r => r.live || !isSubRun(r));
}
async function loadSessions() {
  let rs = [];
  try { const d = await api("/api/runs?n=100"); rs = d.runs || []; SESS_ERR = null; }
  /* 读不到不打扰主流程，但**不许装作空**：记下原因，列表自己说清楚，
     并给一个重试。见 paintSessions() 的第一个分支。 */
  catch (e) { SESS_ERR = e; }
  if (STATUS.active && !rs.some(r => r.id === STATUS.active))
    rs.unshift({ id: STATUS.active, goal: (CUR && CUR.goal) || "（正在跑的任务）",
                 live: true, success: null, ts_iso: "" });
  /* ═════════════ 子代理归档不进行历史（照 DSH「我只列我的会话」）═════════
     ★ 2026-10-21 实测：82 行里 **19 行是子代理任务**（23%），标题全是同一句
       `═══ 子代理任务 · profile=explore · mode=auto ═══`。
       它们不是用户开的对话，是荣珂自己派出去的子代理留下的归档；混在
       "我的历史"里既挤掉真任务，又让整个列表看起来像重复渲染的 bug。

     DSH 的 `ui-workspace` 规定列表只列 Workspace 下的 Session，且「未保存
     标题的历史会话显示「未命名 / Untitled」，**不使用目录名兜底**」——
     即标题必须是**人写的**那一个，不能拿内部标识凑数。子代理归档没有人类
     标题，按同一条纪律它就不该占用一行。

     ★ 但**不丢**：`live`（正在跑的）一律保留；其余 19 条只是默认收起，
       侧栏「历史」小标题右边那枚筛选按钮（隐藏归档 / 全部对话）随时能把它们显出来。
       判据用 id 前缀 + 目标首行，两个信号都在（见 `isSubRun`），不靠单一前缀。 */
  SESS = sessFiltered(rs);
  paintSessions();
}
/* 这一行是不是子代理归档：id 前缀与目标首行**两个信号都命中**才算。
   只认一个信号会漏：历史上有 `subagent-coder-…` 这种 id、目标却写得很像人话的；
   也有 id 像人话、目标却是模板串的。 */
function isSubRun(r) {
  const id = String((r && r.id) || "");
  const head = goalHead((r && r.goal) || "");
  return id.startsWith("subagent-") && head.indexOf("子代理任务") >= 0;
}
/* 筛选键：在我的会话 / 全部对话之间切。**重取一次数据**而不是就地过滤 ——
   因为 SESS 已经是过滤后的数组，就地过滤切不回"全部"。
   `/api/runs?n=100` 实测 0.15s，切一下不卡。 */
function bindSessFilter() {
  const btn = $("#sessFlt");
  if (!btn) return;
  btn.onclick = () => {
    SESS_MODE = SESS_MODE === "all" ? "mine" : "all";
    btn.setAttribute("aria-pressed", SESS_MODE === "all" ? "true" : "false");
    btn.textContent = SESS_MODE === "all" ? "只看我的" : "全部对话";
    btn.title = SESS_MODE === "all"
      ? "收起子代理归档，只留你自己发起的会话"
      : "子代理归档是荣珂自己派出去的子任务留下的记录，不是你的会话";
    loadSessions();
  };
}
/* 相对时间（ZCode 的 formatTaskRelativeTime 语义：<60s 刚刚，<60min 分钟，<24h 小时，否则日期） */
function relTime(ts) {
  const t = Date.parse(String(ts || "").replace(" ", "T"));
  if (!isFinite(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "刚刚";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  if (s < 86400 * 365) return Math.floor(s / 86400) + " 天前";
  return Math.floor(s / 86400 / 365) + " 年前";
}
/* 前置状态槽：TaskListItem 的 leadingIndicator 语义 ——
   运行中=加载环、未完成=destructive 点、未读=sky 点；完成的行**不画点**。 */
function sessLead(r) {
  if (r.live) return '<span class="spin"></span>';
  if (r.success === false) return '<span class="sdot bad"></span>';
  if (r.unread) return '<span class="sdot blue"></span>';
  return "";
}
/* ═════════════ W18-B · 按任务合并（用户 2026-09-30 定）═════════════
   改前实测：会话区 30 行只有 9 件不同的事（同一个目标跑 8 次就是 8 行），
   成品区 32 行只有 10 件 —— 侧栏读起来"像一堆东西"而不是一份目录。
   规则：同一个任务合并成一行，右侧角标写 ×N；**点行 = 打开最新一次**
   （与改前同一个动作，一行都没多按），点角标才展开历次。
   角标只在 N≥2 时出现 —— 只跑过一次的任务，行与改前同构。

   ★ 分组键用**第一行**，不是整串 goal。原因是实测出来的：
     荣珂自己的界面会把模板拼成 3000 字的目标，两次运行只差封面两个槽位，
     整串比较就是两个"任务"，而界面上两行前 22 字一模一样 ——
     按整串分组在语义上对、在人眼上无用。第一行是人写的摘要，
     后面全是模板样板，用它分组才和"一件事"对齐。
     （另有一类残留：三条只差句尾的探针 prompt，第一行也互相不同，
       在 280px 里同样只截到相同前缀 —— 见交付报告的"已知残留"。） */
function goalHead(g) {
  const s = String(g == null ? '' : g);
  const first = s.split(/\r?\n/).map(x => x.trim()).filter(Boolean)[0];
  return (first || s).trim();
}
/* 悬停提示：整串 goal 可能有 3000 字，原样塞进 title 会弹出一个巨型气泡 */
function tipOf(g, n) {
  const s = String(g == null ? '' : g).replace(/\s+/g, ' ').trim();
  const lim = n || 220;
  return s.length > lim ? s.slice(0, lim) + ' …' : s;
}
/* 历次子行靠什么区分同一次任务的多次运行：时间 / 步数 / 耗时。
   ★ 老任务 replay=false，点了打不开（openSession 会如实说"只在列表里可见"）——
     所以子行先把"能不能打开"写在 title 里，而不是让人点了才发现。
   （2026-10-21 起侧栏改为平铺，这一条只剩 title 拼接在用。） */
function runMeta(r) {
  const p = [relTime(r.ts_iso)];
  if (r.steps != null) p.push(r.steps + " 步");
  if (r.elapsed != null && isFinite(+r.elapsed)) p.push(Math.round(+r.elapsed) + " 秒");
  return p.filter(Boolean).join(" · ");
}
/* 底部渐隐的开关：只在"下面还有下文"时挂 .more（滚到底就撤掉）。
   常驻渐隐会把最后一行的下半截永远啃掉，所以必须跟着滚动位置走。 */
function updateNavFade() {
  const n = document.querySelector(".nav");
  if (!n) return;
  n.classList.toggle("more", n.scrollHeight - n.clientHeight - n.scrollTop > 8);
}
function wireNavFade() {
  const n = document.querySelector(".nav");
  if (n && !n.__fadeWired) {
    n.__fadeWired = true;
    n.addEventListener("scroll", updateNavFade, { passive: true });
    window.addEventListener("resize", updateNavFade);
  }
  updateNavFade();
}
function paintSessions() {
  const host = $("#sessList");
  if (!host) return;
  /* ★ 顺序要紧：**先**判「没读到」，再判「真没有」。反过来就会把
     一次网络失败渲染成「你还没有会话」，还顺手把用户的注意力引向「再开一个」。 */
  if (SESS_ERR && !SESS.length) {
    host.innerHTML = '<div class="sessfail">'
      + '<b>历史没读到</b>'
      + '<span>你的会话还在磁盘上，是这一趟没取回来。</span>'
      + '<button class="mini" id="sessRetry">重试</button>'
      + '<em>' + esc(String(SESS_ERR.message || SESS_ERR)) + '</em>'
      + '</div>';
    const b = $("#sessRetry");
    if (b) b.onclick = () => { b.disabled = true; b.textContent = "重试中…"; loadSessions(); };
    return;
  }
  if (!SESS.length) { host.innerHTML = '<div class="sessnone">还没有会话 —— 上面开一个</div>'; return; }
  /* ═════════════ 平铺：每次运行一行（用户 2026-10-21 定）═════════════
     改前是 W18-B 的「按任务合并」：同一个任务收成一行，右侧角标 ×N，
     点行只打开最新一次、点角标才看历次。那次改的动机是"30 行只有 9 件事，
     读起来像一堆东西"。

     ★ 但它换来了一个更糟的观感：用户看到的是"点以前的任务看不到以前的
       记录"—— 实际上回放一直是好的（实测点开有 65 个事件、5 个产出物），
       只是**大多数历史被收进了角标里**，一眼望去只有 7 行。
       历史列表的第一职责是"让我看见我有过什么"，合并把这件职责压掉了。

     所以改回平铺：一次运行一行，新的在上。行比合并版窄得多（只有标题
     一行 + 时间），40 条也滚得动；点哪一行就回放哪一次，不需要先展开。 */
  host.innerHTML = SESS.map((r, i) => {
    const on = CUR && CUR.id === r.id;
    /* 这一条有没有可读的东西（有交付正文 / 有落盘文件）——
       只是**标记**，不再是入口：点行一律回放中间区，见 openSession()。 */
    const art = ART_IDX.get(r.id);
    const marks = (art ? (art.draft ? " · 有交付正文" : "") +
                          (art.files ? " · " + art.files + " 个文件" : "") : "");
    const t = tipOf(r.goal || r.id) +
      (r.replay === false ? " · 这一条只在列表里可见"
                          : (r.followable ? " · 可接着说" : "")) + marks;
    /* 同目标的第一次与后续，视觉上仍要能区分 —— 否则 8 条一模一样的标题
       看起来像重复渲染的 bug。所以：给出时间，并在标题后补一个极淡的
       序号（同目标出现第 2 次起才补）。 */
    const sameBefore = SESS.slice(0, i)
      .filter(x => goalHead(x.goal) === goalHead(r.goal)).length;
    /* 归档行（只有"全部对话"档位里才可能出现）置灰：DSH「可见的归档行置灰」 */
    const arch = isSubRun(r) ? " arch" : "";
    return '<div class="sess' + (on ? " on" : "") + arch + '" data-i="' + i +
      '" title="' + esc(t) + '" role="button" tabindex="0">' +
      '<div class="sess-main"><div class="sess-title-row">' +
      '<span class="sess-title">' + esc(goalHead(r.goal) || r.id) +
        (sameBefore ? '<span class="sess-dup">#' + (sameBefore + 1) + "</span>" : "") +
      "</span>" +
      '<span class="sess-lead">' + sessLead(r) + "</span>" +
      /* 有交付的那几条给一个极淡的稿纸字形：一眼能认出"这条跑出过东西"，
         但行的主身份仍是"一次对话"，不是"一个文件"。 */
      (art ? '<span class="sess-art" title="这次有交付">' + ART_GLYPH + "</span>" : "") +
      (r.live ? "" : '<span class="sess-time">' + esc(relTime(r.ts_iso)) + "</span>") +
      "</div></div></div>";
  }).join("");
  host.querySelectorAll(".sess").forEach(el => {
    const open = () => openSession(SESS[+el.dataset.i]);
    el.onclick = open;
    el.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
  wireNavFade();
  /* 标题溢出渐隐：TaskTitleOverflowText 的 mask 只在真溢出时生效 —— 这里量出来，
     顺手把**溢出宽度**写成 `--sx`、把滚动时长写成 `--sd`（悬停揭示用，见 CSS）。
     为什么不交给纯 CSS：`text-overflow:ellipsis` 的位移量拿不到；
     DSH 的做法也是量（它按行宽算 reveal），照它的思路来。 */
  host.querySelectorAll(".sess-title").forEach(el => {
    const over = el.scrollWidth - el.clientWidth;
    if (over > 1) {
      el.classList.add("of");
      el.style.setProperty("--sx", over + "px");
      /* 位移时长跟着距离走：字数少的别肉，长的别急。240ms~900ms。 */
      el.style.setProperty("--sd", Math.min(900, Math.max(240, over * 6)) + "ms");
    }
  });
}
/* ═════════════ 交付物的"有没有"标注（合并进历史列表用）═════════════
   ★ 2026-10-21：原来这里是**独立的成品区**（#artList）：自己的 26 行、
     自己的 DOM、点行只开右栏稿件台、中间区一动不动。那就是用户报的
     "点历史任务、右侧跳出文本、中间无变化"的直接来源 —— 侧栏两套列表
     标题几乎一样，但一套动右栏、另一套动中间。
     现在成品区已撤，只留下这一份**查表**：哪个 rid 有交付正文 / 交付文件，
     供历史行画那两个小标记（标记只说明"这一条有东西可读"，不再是入口）。
   数据单源仍是 /api/deliverables（服务端从 runs/ 现算）。
   读不到就整张表空着 —— 标记是加餐，不该为它拦住列表。 */
let ART_IDX = new Map();          // rid -> { draft: bool, files: n }
/* 历史行右侧那个"这次有交付"的小字形。定义在顶层常量区（先声明的先能用）——
   它被 paintSessions 引用，而 paintSessions 在文件里出现得更早。 */
const ART_GLYPH = '<span class="art-glyph"><svg viewBox="0 0 16 16" aria-hidden="true">' +
  '<rect x="3.5" y="2.5" width="9" height="11" rx="1"/><path d="M6 6h4M6 8.5h4M6 11h2.5"/></svg></span>';
async function loadArts() {
  let d = null;
  try { d = await api("/api/deliverables?n=100"); } catch (e) { /* 读不到就空着，不吵 */ }
  const idx = new Map();
  for (const it of ((d && d.items) || [])) {
    const nf = (it.files || []).length;
    if (it.has_draft || nf) idx.set(it.rid, { draft: !!it.has_draft, files: nf });
  }
  const changed = idx.size !== ART_IDX.size;
  ART_IDX = idx;
  if (changed) paintSessions();   // 标记后到：补一次重绘（列表本身早已画好）
}
function openSession(r) {
  if (!r || !r.id) return;
  goView("chat");
  if (r.live) { attach(r.id, r.goal); return; }
  /* 台账里的更早任务可能没有归档（服务端 replay:false）——
     点了要如实说，而不是打开一个"任务不存在"。 */
  if (r.replay === false) {
    note("这一条是很早的任务，没有留下可回放的归档 —— 只在列表里可见。");
    return;
  }
  replay(r.id);
}

/* ═════════════ 斜杠命令（ZCode 式：功能都长在输入框上）═════════════
   输入 / 唤出菜单；↑↓ 选、Enter 执行、Esc 收起。命令就是视图与动作的别名 ——
   不引入任何新状态，点了等于用手点了那颗按钮。 */
const SLASH = [
  ["/新对话", "开一个新会话", () => $("#btnNew").click()],
  ["/帮助", "怎么跟它打交道", () => showHelp()],
  ["/文献库", "手头的 PDF：入库、按段落查", () => goView("kb")],
  ["/工具", "它手上有哪些工具", () => goView("tools")],
  ["/能力", "哪些本事现在真能用", () => goView("cap")],
  ["/把关", "交付前它自己核什么", () => goView("gate")],
  ["/产出", "历次交付的文件", () => goView("deliv")],
  ["/配图", "配过的图与它们的体检", () => goView("fig")],
  ["/过程", "它每一步做了什么", () => goView("traj")],
  ["/设置", "模型 / 外观 / 配置", () => goView("set")],
  ["/稿件台", "把交付正文放到右边整面看", () => $("#btnDesk").click()],
];
let SLASH_OPEN = false, SLASH_HIT = [], SLASH_SEL = 0;
function paintSlashMenu() {
  const v = $("#goal").value;
  const host = $("#slashMenu");
  if (!v.startsWith("/")) { closeSlashMenu(); return; }
  const q = v.toLowerCase();
  SLASH_HIT = SLASH.filter(c => c[0].startsWith(q));
  if (!SLASH_HIT.length) {
    host.innerHTML = '<div class="snone">没有这个命令 —— 输入 / 看全部</div>';
    host.hidden = false; SLASH_OPEN = true; SLASH_SEL = 0;
    return;
  }
  if (SLASH_SEL >= SLASH_HIT.length) SLASH_SEL = 0;
  host.innerHTML = SLASH_HIT.map((c, i) =>
    '<div class="sitem' + (i === SLASH_SEL ? " on" : "") + '" data-i="' + i + '"><b>' +
    esc(c[0]) + "</b><span class=\"sd\">" + esc(c[1]) + "</span></div>").join("");
  host.querySelectorAll(".sitem").forEach(el => {
    el.onclick = () => { const it = SLASH_HIT[+el.dataset.i]; closeSlashMenu(); if (it) it[2](); };
  });
  host.hidden = false; SLASH_OPEN = true;
}
function closeSlashMenu() {
  const host = $("#slashMenu");
  if (host) host.hidden = true;
  SLASH_OPEN = false; SLASH_HIT = []; SLASH_SEL = 0;
}
/* /帮助：不发起任务，就地展开一张用法卡 —— 普通人第一次打开最需要的就是这个。 */
function showHelp() {
  goView("chat");
  $("#empty").hidden = true; $("#thread").hidden = false;
  if (!$("#thread").innerHTML) resetThread();
  newRound();
  addBody(
"## 怎么跟荣珂打交道\n" +
"- **第一步，直接说需求**：把想做的事写进下面的输入框，例如「帮我总结这篇论文」。\n" +
"- **第二步，确认任务单**：新任务会先整理「准备做什么、会交付什么、怎样算完成」。确认后才真正执行。\n" +
"- **第三步，拿结果**：执行时只看简明进度；做完可阅读全文、复制或下载。技术过程按需展开。\n" +
"- **接着说**：一个会话可以一直聊。看完结果接着追问（「再短一点」「换成表格」），它带着前情继续。\n" +
"- **递材料**：把 PDF / Word / 数据表拖进窗口，或点左下角「＋」，然后说要对它做什么。\n" +
"- **斜杠命令**：输 `/` 唤出菜单 —— /新对话、/文献库、/设置 这些一键直达。\n\n" +
"它不说没根据的话：结论要能追到真实操作记录；做不完就是做不完，它不会假装交付。");
}

/* ═════════════ 命令面板（Ctrl+K）═════════════
   组件规格逐项照 ZCode：command.tsx（组标题/条目/空态/快捷键右置）
   与 quickPickStyles.ts（列表 max-h min(460px,64vh)、面板 max-w-lg 居中）。
   键盘：↑↓ 循环、Enter 执行、Esc 关闭 —— 与 composer 的斜杠菜单互不抢占：
   斜杠菜单管"输入框里以 / 开头"的场景，这里管全局快捷键。 */
const CP_CMDS = [
  { g: "对话", name: "开一个新对话", k: "/新对话", run: () => $("#btnNew").click() },
  { g: "对话", name: "怎么跟荣珂打交道（帮助）", k: "/帮助", run: () => showHelp() },
  { g: "去某个页面", name: "过程 · 它每一步做了什么", k: "/过程", run: () => goView("traj") },
  { g: "去某个页面", name: "它能做什么 · 能力清单", k: "/能力", run: () => goView("cap") },
  { g: "去某个页面", name: "质量把关 · 交付前核什么", k: "/把关", run: () => goView("gate") },
  { g: "去某个页面", name: "科研工具", k: "/工具", run: () => goView("tools") },
  { g: "去某个页面", name: "我的文献库", k: "/文献库", run: () => goView("kb") },
  { g: "去某个页面", name: "产出 · 历次交付的文件", k: "/产出", run: () => goView("deliv") },
  { g: "去某个页面", name: "图与配图", k: "/配图", run: () => goView("fig") },
  { g: "去某个页面", name: "设置", k: "/设置", run: () => goView("set") },
  { g: "外观与窗口", name: "切换 深色 / 浅色 主题", k: "/主题", run: () => $("#btnTheme").click() },
  { g: "外观与窗口", name: "收起 / 展开侧栏", k: "/侧栏", run: () => $("#btnFold").click() },
  { g: "外观与窗口", name: "稿件台 · 右侧阅读栏", k: "/稿件台", run: () => $("#btnDesk").click() },
];
let CP = { open: false, sel: 0, items: [] };
function cpRender() {
  const q = ($("#cpInput").value || "").trim().toLowerCase();
  CP.items = CP_CMDS.filter(c => !q || (c.name + c.k + c.g).toLowerCase().includes(q));
  const host = $("#cpList");
  if (!CP.items.length) {
    host.innerHTML = '<div class="cpempty">没有匹配的命令</div>';
    return;
  }
  let html = "", lastG = "";
  CP.items.forEach((c, i) => {
    if (c.g !== lastG) { html += '<div class="cph">' + esc(c.g) + "</div>"; lastG = c.g; }
    html += '<div class="cpi' + (i === CP.sel ? " on" : "") + '" data-i="' + i + '">' +
      '<span class="g">•</span><span>' + esc(c.name) + '</span>' +
      (c.k ? '<span class="k">' + esc(c.k) + "</span>" : "") + "</div>";
  });
  host.innerHTML = html;
  host.querySelectorAll(".cpi").forEach(el => {
    el.onclick = () => cpRun(CP.items[+el.dataset.i]);
    el.onmousemove = () => { if (CP.sel !== +el.dataset.i) { CP.sel = +el.dataset.i; cpPaintSel(); } };
  });
  const on = host.querySelector(".cpi.on");
  if (on) on.scrollIntoView({ block: "nearest" });
}
function cpPaintSel() {
  $("#cpList").querySelectorAll(".cpi").forEach(el =>
    el.classList.toggle("on", +el.dataset.i === CP.sel));
}
function cpOpen() {
  $("#cpMask").hidden = false;
  $("#cpInput").value = ""; CP.sel = 0; CP.open = true;
  cpRender(); $("#cpInput").focus();
}
function cpClose() {
  $("#cpMask").hidden = true; CP.open = false;
}
function cpRun(c) {
  cpClose();
  if (c && c.run) c.run();
}
$("#cpMask").addEventListener("mousedown", e => { if (e.target === $("#cpMask")) cpClose(); });
$("#cpInput").addEventListener("input", () => { CP.sel = 0; cpRender(); });
$("#cpInput").addEventListener("keydown", e => {
  const n = CP.items.length;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!n) return;
    CP.sel = (CP.sel + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    cpRender();
  } else if (e.key === "Enter") {
    e.preventDefault();
    cpRun(CP.items[CP.sel] || CP.items[0]);
  }
});
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
    e.preventDefault();
    CP.open ? cpClose() : cpOpen();
    return;
  }
  if (e.key === "Escape" && CP.open) { cpClose(); return; }
  /* 右键菜单的 Esc 也在这里收（菜单打开时它还在 DOM 里） */
  if (e.key === "Escape" && !$("#ctxMenu").hidden) $("#ctxMenu").hidden = true;
});

/* ═════════════ 右键菜单（会话行 / 成品行）═════════════
   照 ZCode ContextMenu 的形状：min-w-44 rounded-lg bg-menu p-1、
   条目 min-h-7 圆角-md 悬停 bg-menu-hover。条目是两条既有通路的快捷入口：
   打开（回放 / 稿件台）与复制目标全文 —— 不新增任何后端能力。 */
function ctxOpen(x, y, items) {
  const m = $("#ctxMenu");
  m.innerHTML = items.map((it, i) =>
    it === "-" ? '<div class="sep"></div>'
      : '<div class="i" data-i="' + i + '">' + esc(it.name) + "</div>").join("");
  m.hidden = false;
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.min(x, innerWidth - w - 8) + "px";
  m.style.top = Math.min(y, innerHeight - h - 8) + "px";
  m.querySelectorAll(".i").forEach(el => {
    el.onclick = () => { m.hidden = true; items[+el.dataset.i].run(); };
  });
}
function ctxCopy(text) {
  try { navigator.clipboard.writeText(text); note("目标全文已复制到剪贴板"); }
  catch (e) { note("复制失败：" + (e && e.message ? e.message : e)); }
}
function ctxBind(host, build) {
  if (!host) return;
  host.addEventListener("contextmenu", e => {
    const row = e.target.closest(".sess");
    if (!row) return;
    e.preventDefault();
    const r = build(+row.dataset.i);
    if (r && r.length) ctxOpen(e.clientX, e.clientY, r);
  });
  /* 左键点别处 / 滚动都收起 */
  document.addEventListener("mousedown", e => {
    if (!e.target.closest("#ctxMenu")) $("#ctxMenu").hidden = true;
  }, true);
}
ctxBind($("#sessList"), i => {
  const r = SESS[i];
  if (!r) return null;
  const items = [];
  if (r.live) items.push({ name: "回到进行中的对话", run: () => openSession(r) });
  else if (canReplay(r)) items.push({ name: "打开这次回放", run: () => openSession(r) });
  if (r.followable && !r.live) items.push({ name: "接着说（在输入框输入即可）", run: () => { openSession(r); $("#goal").focus(); } });
  /* 「在稿件台读这次的正稿」只对有交付正文的条目给 —— 没正文的那些，
     给一个点了只有空态的菜单项，等于把"点开是空的"重新请回来。
     （这是原「成品区」唯一的独有能力，随成品区一起并进这一行的右键菜单。） */
  const art = ART_IDX.get(r.id);
  if (art && art.draft) items.push({ name: "在稿件台读这次的正稿", run: () => deskLoadRun(r.id) });
  items.push({ name: "复制目标全文", run: () => ctxCopy(r.goal || "") });
  return items;
});

/* ═════════════ 侧栏收起 / 展开（照 DSH：刷新即重置）═════════════
   DSH 的布局状态**不持久化**（ui-layout/README：panel geometry is transient，
   reload restores the sidebar default）。所以这里没有 localStorage —— 收起只是
   本次会话内的临时状态，刷新回到「宽屏展开 / 窄屏(<1024)自动收起」。
   侧栏还可在 264–420px 之间拖动调宽（同样不持久化，刷新回到 CSS 默认 280）。 */
const SIDE_MIN = 264, SIDE_MAX = 420;
/* 桌面 app 从 `?side=open` 进来时，侧栏默认展开（用户的原话：「你侧栏打开不就行了」）。
   浏览器里照旧跟随视口：窄屏自动收起，☰ 仍可手动切。 */
let SIDE_MANUAL = null;              // null=跟随视口；true/false=本会话内用户显式折叠/展开
try {
  const _side = new URLSearchParams(location.search).get("side");
  if (_side === "open") SIDE_MANUAL = false;
  else if (_side === "fold") SIDE_MANUAL = true;
} catch (e) {}
function isNarrow() { return window.innerWidth < 1024; }   // DSH：< 1024 自动收起
function paintSide(folded) {
  $("#side").dataset.fold = folded ? "1" : "0";
  const b = $("#btnFold");
  b.title = folded ? "展开侧栏" : "收起侧栏";
  b.setAttribute("aria-label", b.title);
  const narrow = window.innerWidth <= 880;
  const scrim = $("#sideScrim");
  if (scrim) scrim.dataset.open = narrow && !folded ? "1" : "0";
  $("#side").setAttribute("aria-hidden", narrow && folded ? "true" : "false");
  if ("inert" in $("#side")) $("#side").inert = narrow && folded;
}
function applyFold() { paintSide(SIDE_MANUAL === null ? isNarrow() : SIDE_MANUAL); }
/* 只改**当前这一眼**，不记为选择：窄屏选完页面自动让路，不该把它当成一个持久选择。 */
function setFold(v) {
  paintSide(v);
  const narrow = isNarrow();
  const scrim = $("#sideScrim");
  if (scrim) scrim.dataset.open = narrow && !v ? "1" : "0";
  $("#side").setAttribute("aria-hidden", narrow && v ? "true" : "false");
  if (narrow && "inert" in $("#side")) $("#side").inert = !!v;
}
$("#btnSideClose").onclick = () => { setFold(true); $("#btnFold").focus(); };
$("#sideScrim").onclick = () => { setFold(true); $("#btnFold").focus(); };
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && isNarrow() && $("#side").dataset.fold === "0") {
    e.preventDefault(); setFold(true); $("#btnFold").focus();
  }
});
let COMPOSER_CLEAR = 0;
/* ★ 切换会话/视图时把「见过的最大高度」清零（2026-11-01）。
   留白只增不减是为了不让内容在打字/删字时跳动；但换了内容就**该**重新量，
   否则上一场留下的高峰值会让这一场的短会话底部空出一大片。
   写回 CSS 用一个保守初值，让 `syncComposerSpace` 马上重新填。 */
function resetComposerSpace() {
  COMPOSER_CLEAR = 0;
  const c = $("#composer");
  const h = (c && getComputedStyle(c).display !== "none")
    ? Math.ceil(c.getBoundingClientRect().height) : 0;
  COMPOSER_CLEAR = h;
  document.documentElement.style.setProperty("--composer-clear", (h + 16) + "px");
}
function syncComposerSpace() {
  const c = $("#composer");
  const visible = c && getComputedStyle(c).display !== "none";
  const h = visible ? Math.ceil(c.getBoundingClientRect().height) : 0;
  /* ★ 2026-11-01：留白必须**只增不减**，否则你在输入框里删字时，
     整个会话内容会往上退一下。用户原话：
         「我在输入框删除的时候，上方那个荣珂也会向上退。」
     机制：`.thread{padding-bottom: var(--composer-clear)}`，而这里每帧把
     `--composer-clear` 设成"输入框当前高度 + 16"。于是
         打一个字撑行 → 输入框 248→274 → 留白 264→290 → 内容被顶上去 26px；
         删回一行     → 留白 290→264 → 内容又掉回来 26px。
     用户看到的就是"我一删字，上面的东西跳了一下"。
     实测（_t_jump.py，1280×800）：scrollTop 248 → 274 → 248，幅度 26px。

     正确做法：留白取**这一会话里见过的最大高度**。输入框长高时内容不动
     （它本来就该待在原地，只是被输入框盖住 —— 滚动区最后那点留白是给它让位的），
     缩回去时留白也不动。只在切换会话/视图这种"内容换了一批"的时机重置。

     为什么不用 CSS 的 `max-height` 直接写死一个够大的值：那会让短回答的
     会话底部空出一大片，滚到底时看到的是一片白。只增不减的峰值不会。 */
  if (h > COMPOSER_CLEAR) {
    COMPOSER_CLEAR = h;
    document.documentElement.style.setProperty("--composer-clear", (h + 16) + "px");
  }
}
if (window.ResizeObserver) new ResizeObserver(syncComposerSpace).observe($("#composer"));
window.addEventListener("resize", syncComposerSpace);
syncComposerSpace();
$("#btnFold").onclick = () => {
  SIDE_MANUAL = $("#side").dataset.fold !== "1";   // 与当前显示相反
  applyFold();
};
window.addEventListener("resize", () => { if (SIDE_MANUAL === null) applyFold(); });
/* 拖动调宽：264–420，拖动过程不加过渡（DSH：dragging has no transition delay）。
   收起成 56px rail 时把手 CSS 里就不渲染（.side[data-fold="1"] .sidegrip{display:none}）。 */
(function () {
  const side = $("#side"), grip = $("#sideGrip");
  if (!grip) return;
  let dragging = false;
  const clamp = w => Math.max(SIDE_MIN, Math.min(SIDE_MAX, Math.round(w)));
  grip.addEventListener("pointerdown", (e) => {
    if (side.dataset.fold === "1") return;
    dragging = true; side.classList.add("dragging");
    grip.setPointerCapture(e.pointerId); e.preventDefault();
  });
  grip.addEventListener("pointermove", (e) => {
    if (dragging) side.style.width = clamp(e.clientX) + "px";
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false; side.classList.remove("dragging");
    try { grip.releasePointerCapture(e.pointerId); } catch (x) {}
  };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
})();
/* 窄屏抽屉是覆盖层，而 ☰ 恰好被它自己盖住（截图实测）。没有这一条，
   误开抽屉的人只剩"选一个页面"这一条出路 —— 点抽屉外任意处就该让路。 */
document.addEventListener("click", (e) => {
  if (!isNarrow() || $("#side").dataset.fold === "1") return;
  const t = e.target;
  if (!t || !t.closest) return;
  /* `#btnFold` 必须一起放过：它的事件先于这里执行，展开完立刻又被这条判成
     "点了外面"，抽屉就永远打不开（实测就是这样翻的）。 */
  if (!t.closest("#side") && !t.closest("#btnFold")) setFold(true);
});
applyFold();

/* ═════════════ 知识库 ═════════════
   ★ 面板**不含任何查询逻辑**：数据全部来自 /api/kb/*，而那一头转发的就是
     agent 自己用的 kb_* 工具（荣珂工具注册表里的同一批函数）。
     于是"面板能查到、agent 却调不到"这种分家不可能发生 —— 那是本项目
     最难查的一类故障。
   ★ 失败一律**原样显示服务端的话**（含 hint）。把失败渲染成"没有结果"，
     使用者会以为"库里没这篇"，而去重新导入一遍已经存在的库。 */

/* ★ `String(Error)` 返回的是「名字: 消息」—— 于是 `TimeoutError`、`TypeError`
   这类**帮我调试的名字会被端到用户眼前**，还把真正该读的那半句挤到后面。
   全站 7 处都是这么写的。统一从这里取消息（`e.message` 本来就有 39 处在用）。 */
function errMsg(e) {
  if (e == null) return "未知错误";
  if (typeof e === "string") return e;
  if (e.message) return String(e.message);
  return String(e);
}
function kbFail(where, d) {
  return '<div class="kbwarn"><b>' + esc(String(where)) + '失败：</b>'
    + kbFailBody(d) + '</div>';
}
/* 原始诊断那一坨（服务端 error + 怎么修 + 逐份原因）单独成件：
   kbFail 把它铺在首屏，pageFail 把它折进详情 —— 共用一份，
   不在别处再写第二种"失败长什么样"（那会是第二套真相）。 */
function kbFailBody(d) {
  const e = (d && (d.error || d.raw)) || "未知错误";
  const h = (d && d.hint) ? '<br><b>怎么修：</b>' + esc(String(d.hint)) : "";
  /* 逐份失败的原因也要带出来：只说"1 个文件入库失败"等于没说，
     而 tool 层明明把每个文件的原因都写进了 `failures`。 */
  const fs = (d && d.failures) || [];
  const fd = fs.length
    ? '<br>逐份：' + fs.slice(0, 5).map(x => esc(String(x.file)) + ' → '
        + esc(String(x.error))).join('；')
    : "";
  return esc(errMsg(e).slice(0, 400)) + h + fd;
}
/* W15-A-r 任务 r2：这四页失败时主行只说人话；路由名与返回体键名是排查线索，
   一行不删，只从首屏降到折叠层 —— 形状与 gateReadFail 一致（W15-A 任务 2 立的那套）。 */
function pageFail(mainLine, humanLine, d) {
  return '<div class="kbwarn"><b>' + esc(mainLine) + "</b><br>" + humanLine +
    detailRow("详情（排查用）", kbFailBody(d)) + "</div>";
}

function paintKbAi(ai) {
  const box = $("#kbAiNote");
  if (!box) return;
  if (!ai || ai.state === "unknown") {
    box.innerHTML = '<div class="kbwarn">读不到 AI 配置状态'
      + (ai && ai.why ? '（' + esc(ai.why) + '）' : '')
      + ' —— 问答 / 对比 / 综述是否可用，只能按下去才知道。</div>';
    return;
  }
  if (ai.state === "configured") {
    box.innerHTML = '<div class="kbok">问答 / 对比 / 综述的模型已配置（'
      + esc(ai.model || "-") + '）。</div>';
    return;
  }
  /* 报"配置文件里 api_key 是空的"这个**事实**，不声称"AI 不能用"
     （直连还取决于网关与 key 是否有效）。 */
  /* 工具名一律取自 ai.ai_dependent（后端是唯一真相源）；前端写死就等于
     后端加个 AI 依赖工具时这里悄悄漏报。空清单要如实说"没给"，不渲染垃圾。 */
  const dep = (Array.isArray(ai.ai_dependent) ? ai.ai_dependent : [])
    .map(t => String(t == null ? "" : t).trim()).filter(Boolean);
  const depNote = dep.length
    ? '依赖 AI 的是这几个工具：<code>' + dep.map(esc).join('</code>、<code>')
      + '</code>。<br>'
    : '这份响应没列出哪些工具依赖 AI（上面提到的功能以工具表为准）。<br>';
  /* 变量名取自后端；清单空了就别再挂一个空的「或设环境变量 」在句尾 ——
     也不替它编名字：那串名字是给用户抄的，编一个就是假话。 */
  const envs = (Array.isArray(ai.env_names) ? ai.env_names : [])
    .map(t => String(t == null ? "" : t).trim()).filter(Boolean);
  /* base_url / model 为空就说没填，绝不拿它们冒充"已配好"。 */
  const url = String(ai.base_url == null ? "" : ai.base_url).trim();
  const mdl = String(ai.model == null ? "" : ai.model).trim();
  const both = [url, mdl].filter(Boolean).length;
  const addrNote = '另外：' + (url ? '请求地址 <code>' + esc(url) + '</code>'
    : '请求地址<b>也没填</b>') + '，'
    + (mdl ? '模型 <code>' + esc(mdl) + '</code>' : '模型名<b>也没填</b>')
    + (both === 2 ? ' —— 两项都在，<b>只差 <code>ai.api_key</code></b>。'
      : (both === 0 ? ' —— 这两项同样是空的，要和 key 一并填。'
        : ' —— 空的这项要和 <code>ai.api_key</code> 一并填。')) + '<br>';
  box.innerHTML = '<div class="kbwarn"><b>问答 / 对比 / 综述现在大概率用不了</b>'
    + ' —— 这几个功能要的接口密钥还没配。<br>'
    + '<b>不依赖它们的功能照常可用</b>：入库、段落级检索、按页码读全文。'
    + '<details class="kbhow"><summary>怎么修（技术细节）</summary>'
    + depNote
    + '根据：<code>' + esc(ai.file || "app_config.json")
    + '</code> 里 <code>ai.api_key</code> 是空的。<br>'
    + addrNote
    + (envs.length
        ? '修法二选一：填那份配置的 ai 段，或设环境变量 '
          + esc(envs.join(" / ")) + '。<br>'
        : '修法：填那份配置的 ai 段'
          + '（这份响应没列出等效的环境变量名）。<br>')
    + '<b>这是另一套凭据</b>：这些工具读的是上面那份 workbench 配置里的'
    + ' <code>ai.api_key</code>（或 <code>PAPER_WORKBENCH_*</code> 环境变量），'
    + '与荣珂主模型用的 <code>COMMANDCODE_API_KEY</code> <b>不是同一份</b>，'
    + '两边要分别配。'
    + '</details></div>';
}

async function loadKb() {
  const out = $("#kbOut");
  if (out && out.dataset.filled !== "1") kbListAll();
  $("#kbStat").innerHTML = '<div class="blank">读取中…</div>';
  let d;
  try { d = await api("/api/kb/stat"); }
  catch (e) { $("#kbStat").innerHTML = kbFail("读取库况", { error: errMsg(e) }); return; }
  if (!d || !d.ok) { $("#kbStat").innerHTML = kbFail("读取库况", d); paintKbAi(d && d.ai); return; }
  /* ★ 这里的四个格子必须与下面每行的说法**同口径**（有原文/只有文本/仅元数据）。
     原来写的是"库中文献 / 有全文 / 仅元数据"—— `有全文` 会被读成"能看原文"，
     而每行写的是"只有文本"，同一批文献在页面上自己打自己。
     现在三档直接对着盘上事实数，措辞与行内完全一致。

     ★★ 2026-10-14 用户「看不懂」返工：光有四个名词还不够 ——
     用户不知道**哪个数字高才算好**、也不知道「只有文本」要不要处理。
     所以每个格子补一句"这档能干什么"，并且**把要处理的那一档显出来**
     （见下面 kbTodo）。名词保持简短，解释放 title 与副行。 */
  /* ★ 2026-10-27 重塑：原来这里是 4 个同色灰盒子并排（用户："给个框就叫做了？"）。
     四个数字并排读不出关系 —— 三档其实是**同一个库的三个切片**，不是四个并列指标。
     改成一条按真实条数分宽的分布条 + 图例：读"占比"而不是读"四个孤立数字"。
     ★ 合计单独一行放在末尾，与切片用发丝线隔开 —— 它是分母，不该和切片平级。 */
  const _pct = n => d.total ? Math.round(100 * n / d.total) : 0;
  const _segs = [[d.n_pdf, "pdf"], [d.n_text_only, "text"], [d.n_meta, "meta"]];
  let h = '<div class="kbdist">'
    + '<div class="kbdist-bar">'
    + _segs.map(s => s[0]
        ? '<span class="kbdist-seg" data-k="' + s[1] + '" style="flex-grow:' + s[0]
          + '" title="' + esc(String(s[0])) + ' 篇"></span>'
        : "").join("")
    + '</div>'
    + '<div class="kbdist-legend">'
    + [["pdf", d.n_pdf, "有原文", "PDF 还在，能看原页、能查段落"],
       ["text", d.n_text_only, "只有文本", "PDF 不在了，只能查文字、看不到原页"],
       ["meta", d.n_meta, "仅元数据", "只有题名年份，查不到内容"]]
        .map(c => '<div class="kbdist-item" title="' + esc(c[3]) + '">'
          + '<span class="kbdist-dot" data-k="' + c[0] + '"></span>'
          + '<span class="kbdist-n">' + esc(String(c[1])) + '</span>'
          + '<span class="kbdist-lab">' + esc(c[2]) + '</span>'
          + '<span class="kbdist-pct">' + _pct(c[1]) + '%</span></div>').join("")
    + '</div>'
    + '<div class="kbdist-total"><b>' + esc(String(d.total)) + '</b>'
    + '<span>篇在库里，合计</span></div>'
    + '</div>';
  /* ★ 把"该处理什么"直接说出来。用户看不懂那四个数字的根子不在名词，
     而在于**看完不知道该干嘛** —— 这一句就是答案，且只在真有要处理的条目时出现
     （一条都没有就一个字都不说，不制造待办幻觉）。
     ★ 措辞返工：初版写「有 两种可以补」，把"两种"当名词用，读着别扭
       （截图里就是「有 两种可以补」）；改成"有几篇可以补"并直接列条数。 */
  const _todo = [];
  if (d.n_text_only) _todo.push(d.n_text_only + " 篇只有文字，看不到原页");
  if (d.n_meta) _todo.push(d.n_meta + " 篇只有题录，按段落查不到内容");
  if (_todo.length) {
    const _n = d.n_text_only + d.n_meta;
    h += '<div class="kbtodo"><b>' + _n + ' 篇可以补</b><span>'
      + esc(_todo.join("；"))
      + ' —— 找到 PDF 后走下面「新增文献 · 本地 PDF」，原文会归到同一篇上。</span></div>';
  }
  if (d.partial) h += '<div class="kbwarn">' + esc(d.partial_note || "统计只覆盖部分条目") + '</div>';
  if (d.tools_missing && d.tools_missing.length)
    h += '<div class="kbwarn">这些 kb 工具荣珂看不见：'
      + esc(d.tools_missing.join("、")) + '（接线断了，不是库空了）</div>';
  /* W18-F · 库目录原来是挂在「库况」标题右边的一串 `C:\Users\…\.dsh\…\litkb`。
     它是**有用的事实**（用户会问"我的 PDF 到底存哪了"），但**标题行不是放路径的地方**：
     挂在那儿读起来就是一句调试信息。搬到卡片正文里，并给它一个「库目录」的名头。 */
  /* ★ 2026-10-27：库目录原来是卡片正文里的一行 `C:\Users\…\litkb`。
     它不是没用的信息（用户确实会问"我的 PDF 存哪了"），但**摊在正文里**读起来
     就是一句调试输出，把整页从"我的文献"拉回"某人的文件系统"。
     收进技术细节抽屉：想找的人点得开，不想看的人看不见。 */
  if (d.library)
    h += '<details class="kbhow" style="margin:10px 0 0"><summary>库目录</summary>'
      + '<div class="kblib" style="margin:0"><span>' + esc(d.library) + '</span></div>'
      + '</details>';
  $("#kbStat").innerHTML = h;
  $("#kbHint").textContent = "";
  paintKbAi(d.ai);
}

/* 「列出全部」默认铺几篇。「产出」页是 10（一屏约 1.3），这里要更少：
   文献行比产出卡矮（约 60px/行 含间隔），但 251 篇的库一铺就是 12 屏。
   取 20 篇 ≈ 1.4 屏，与「一屏看完、想深看再展开」是同一个尺寸感。
   注意 KB_EXPAND 是模块级：refreshKbStat() 会重复调 kbListAll()，
   状态不能只活在闭包里，否则一刷新库况就弹回 20 篇。 */
const KB_SHOW = 20;
let KB_EXPAND = false;
/* ════════════════════════════════════════════════════════════════
   题录标题清洗（2026-10-27）
   litkb 里有 85/251 条标题带 HTML 噪声，占 34%，在列表里显示成
   `&lt;i&gt;Cordyceps&lt;/i&gt;` 这样的乱码。两种来源通道各带一种脏：
     · 32 条存的是**实体**：`&lt;i&gt;Auricularia heimuer&lt;/i&gt;`（Crossref 风格）
     · 53 条存的是**真标签**：`<i>Ophiocordyceps sinensis</i>`（PubMed 风格）
     两者交集为 0 —— 所以必须两种都处理，只修一种会漏掉一半。
   ★ 为什么修在前端而不是后端：litkb 是外部库格式，改数据会影响
     检索命中和去重；而且这里要的只是"列表里读得出来"，斜体本身不需要。
   ★ 只清洗**显示的标题**，不动 it.title —— title 属性与检索仍走原文。 */
const KB_ENT = { "&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"', "&#39;": "'", "&apos;": "'" };
function kbClean(t) {
  let s = String(t == null ? "" : t);
  if (!/&[a-z#0-9]+;|<[a-zA-Z\/!]/.test(s)) return s;   // 干净的直接返回，不做无用功
  s = s.replace(/&(lt|gt|amp|quot|apos|#39);/g, m => KB_ENT[m] || m);
  s = s.replace(/<\/?(?:i|em|b|strong|sub|sup|u|span|scp|italic)[^>]*>/gi, "");
  // 解实体后可能出现新的标签（&amp;lt;i&amp;gt; 这种双重转义），再清一遍
  s = s.replace(/<\/?(?:i|em|b|strong|sub|sup|u|span)[^>]*>/gi, "");
  /* ★ 截断的实体（2026-10-27）
     实测 kb438013b5 的标题正好 120 字符，末尾把一个 `&lt;/i&gt;` 切成了 `&lt;/i`
     —— 半个实体、没有分号，上面的完整实体正则匹配不到，残留成
     `...Cordyceps militaris</i&gt` 显示给用户。全库 251 条里有 1 条这种。
     只清**残缺实体**，不碰正文里合法的 &（"R&D" 这类）：
       · 无分号的实体名前缀 &lt &gt &amp &quot &apos &#…；
       · 行尾孤零零的 & 残片。 */
  s = s.replace(/&(?:lt|gt|amp|quot|apos|#\d*|#x[0-9a-f]*)(?![a-z0-9]*;)/gi, "");
  s = s.replace(/&+$/g, "");
  /* ★ 行尾的**半个标签**：源库里那条 120 字符的标题被截断在 `</i`，
     连 `>` 都没有，所以上面要求 `[^>]*>` 的正则匹配不到。
     只清行尾（`$`），不碰正文中间可能是数学符号的 `<`。 */
  s = s.replace(/<\/?[a-zA-Z][a-zA-Z0-9]*$/, "");
  s = s.replace(/<\/?$/, "");
  return s.replace(/\s+/g, " ").trim();
}

function kbRows(items) {  const rows = (items || []).map(it => {
    const tags = (it.tags || []).map(t =>
      '<span class="tag" data-tone="neutral">' + esc(t) + '</span>').join(" ");
    /* ★ 「有全文」是**两个不同的东西**，必须分开说：
         `fulltext=true` 只表示**抽出了正文文本**（texts/<id>.txt），
         不表示**原件 PDF 还在**（pdfs/<id>.pdf）。
       实测这份库：12 篇 fulltext=true，其中只有 3 篇盘上真有 PDF；
       另外 9 篇是"只有抽出来的文本、原件没留下"。
       所以"看原页"只给真有 PDF 的那几篇 —— 给了打不开更糟。 */
    const hasPdf = !!it.has_pdf;
    const meta = [it.year, it.journal,
                  hasPdf ? "有原文" : (it.fulltext ? "只有文本" : "仅元数据"),
                  it.status].filter(Boolean).join(" · ");
    /* ★ 2026-10-14：左边那一列原来直接摊内部 ID（`kb635b861b1`），
       用户扫一眼完全看不出是哪篇 —— 那是**我们的主键**，不是给人认的。
       改成：年份 + 语言这类"人认得的短标签"；拿不到才退回 ID。
       为什么不一刀切掉 ID：真有取不出来的时候（台账缺元数据），
       ID 是唯一能区分两行的东西 —— 留作兜底，而不是当门面。
       title 已在右边大字显示，这里不重复。 */
    /* ★ 2026-10-27 重塑：原来每行是「左边竖一个 2026 EN + 右边标题 + 下面一行灰字」，
       251 篇排下来就是一面无表头、无列对齐的文本墙（用户："完全毛坯房"）。
       改成真表格：年份 / 标题 / 期刊 / 状态各占一列，扫描时眼睛有纵向落点。
       ★ 仍然用 <tr class="kbrow">：既有的行点击委托（kbWireRows）、
         :focus-visible、以及探针里按 .kbrow 数的判据都不必改。 */
    const _lang = /[\u4e00-\u9fff]/.test(it.title || "") ? "中文" : "EN";
    const _src = hasPdf ? "有原文" : (it.fulltext ? "只有文本" : "仅元数据");
    const _srcK = hasPdf ? "pdf" : (it.fulltext ? "text" : "meta");
    return '<tr class="kbrow' + (hasPdf ? " clickable" : "") + '"'
      + (hasPdf ? ' data-kb="' + esc(it.id) + '" role="button" tabindex="0"' : "")
      + '><td class="kbt-year">' + esc(it.year || "—") + '</td>'
      + '<td class="kbt-title" title="' + esc(it.title || "") + '">'
      + (hasPdf ? '<span class="kbgo">看原页 ▤</span>' : "")
      + esc(kbClean(it.title) || "(无标题)") + '</td>'
      + '<td class="kbt-journal" title="' + esc(it.journal || "") + '">'
      + esc(it.journal || "—") + '</td>'
      + '<td class="kbt-src"><span class="kbsrc" data-s="' + _srcK + '">'
      + esc(_src) + '</span></td></tr>';
  }).join("");
  if (!rows) return '<div class="blank">没有结果</div>';
  /* ★ table-layout:fixed 下宽度必须显式给全，否则标题列分不到空间、
     被压成"一行一个字"（第一版实测：标题列只剩 20px，截图里每行只有一个字形）。
     定宽两列 + 百分比期刊列 + 标题列 auto 拿剩余 —— 这是 fixed 布局唯一稳的写法。 */
  /* ★ fixed 布局下 col 用百分比会先按"容器宽度"算再被 min-content 顶开（实测溢出）。
     全部改定宽，标题列不写宽度 —— fixed 下不写宽度的列自动吃掉剩余空间。 */
  /* ★★★ 列宽用**百分比且总和 100%**（2026-10-27 第三次才改对）
     前两次都错在同一个误解上 —— 以为 fixed 布局下"不写宽度的列会吃掉剩余空间"。
     实测（_diag_cols.py）：col = 58 / 0 / 170 / 86（合计 314），表格 798px，
     浏览器把多出来的 484px **按比例摊给所有列**，结果：
         58 → 147px、0 → 0px、170 → 432px、86 → 219px
     于是标题列被压成 0（内容被期刊列盖住），年份列反而胖到 147px。

     机制：table-layout:fixed 只在**列宽总和等于表格宽度**时严格成立；
     不足时浏览器按声明宽度比例缩放，所以 px 定宽是不可靠的。
     百分比总和 100% 才能既严格又自适应。
     798px 下约为 64 / 495 / 160 / 80。 */
  return '<table class="kbtable"><colgroup>'
    + '<col style="width:8%"><col style="width:62%"><col style="width:20%">'
    + '<col style="width:10%">'
    + '</colgroup><thead><tr><th>年份</th><th>标题</th><th>期刊</th><th>可读</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>';
}
/* 库列表/检索结果里点一行 → 右栏看 PDF 原页。事件委托：行是每次重画的。 */
function kbWireRows(host, items) {
  if (!host) return;
  host.querySelectorAll("[data-kb]").forEach(el => {
    const go = () => {
      const page = parseInt(el.dataset.page || "1", 10) || 1;
      if (window.RongkeLibrary) {
        RongkeLibrary.open(el.dataset.kb, page);
        return;
      }
      openOrig({ kb: el.dataset.kb, title: el.querySelector("b")
        ? el.querySelector("b").textContent : "文献", n: page });
    };
    el.onclick = go;
    el.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
  });
}

let KB_OUT_SEQ = 0;
let KB_LIST_BUSY = false;
let KB_SEARCH_BUSY = false;

async function kbListAll() {
  if (KB_LIST_BUSY) return;
  KB_LIST_BUSY = true;
  const seq = ++KB_OUT_SEQ;
  const out = $("#kbOut");
  const button = $("#kbAll");
  button.disabled = true;
  out.innerHTML = '<div class="blank">读取中…</div>';
  try {
    let d;
    try { d = await api("/api/kb/list?limit=500"); }
    catch (e) { d = { ok: false, error: errMsg(e) }; }
    if (seq !== KB_OUT_SEQ) return;
    if (!d || !d.ok) { out.innerHTML = kbFail("列出", d); return; }
    const items = d.items || [];
    const avail = items.length;
    const total = d.matched == null ? avail : d.matched;
    $("#kbSearchHint").textContent = "　共 " + total + " 篇";
    if (!avail) {
      out.innerHTML = total
        ? '<div class="kbwarn">库里有 ' + esc(String(total)) + ' 篇，但本次没有取回条目；请重试或用检索查找。</div>'
        : '<div class="blank">文献库还是空的 —— 用下面的 AI 检索候选或本地 PDF 入库。</div>';
      out.dataset.filled = "1";
      return;
    }
    const shown = KB_EXPAND ? avail : Math.min(KB_SHOW, avail);
    const moreLabel = total > avail ? "展开本次取回的 " + avail + " 篇" : "展开全部 " + avail + " 篇";
    out.innerHTML = kbRows(items.slice(0, shown))
      + (shown < avail
          ? '<div class="morebar"><button class="mini pri" id="kbMore">' + moreLabel +
            '</button><span class="hintl">现在只显示前 ' + shown + ' 篇' +
            (total > avail ? '；库里共 ' + total + ' 篇，其余 ' + (total - avail) +
              ' 篇请用检索查找' : '，其余都在库里') + '</span></div>'
          : (KB_EXPAND && avail > KB_SHOW
              ? '<div class="morebar"><button class="mini" id="kbLess">只看前 ' +
                KB_SHOW + ' 篇</button></div>' : ""))
      + (d.note ? '<div class="sub">' + esc(d.note) + '</div>' : "");
    kbWireRows(out, items);
    const more = $("#kbMore");
    if (more) more.onclick = () => { KB_EXPAND = true; kbListAll(); };
    const less = $("#kbLess");
    if (less) less.onclick = () => { KB_EXPAND = false; kbListAll(); };
    out.dataset.filled = "1";
  } finally {
    KB_LIST_BUSY = false;
    button.disabled = false;
  }
}

async function kbSearch() {
  if (KB_SEARCH_BUSY) return;
  const q = $("#kbQ").value.trim();
  if (!q) { $("#kbOut").innerHTML = '<div class="blank">先写点关键词</div>'; return; }
  KB_SEARCH_BUSY = true;
  const seq = ++KB_OUT_SEQ;
  const out = $("#kbOut");
  const button = $("#kbGo");
  button.disabled = true;
  out.innerHTML = '<div class="blank">检索中…</div>';
  try {
    let d;
    try { d = await api("/api/kb/search?q=" + encodeURIComponent(q) + "&n=30"); }
    catch (e) { d = { ok: false, error: errMsg(e) }; }
    if (seq !== KB_OUT_SEQ) return;
    if (!d || !d.ok) { out.innerHTML = kbFail("检索", d); return; }
    const hits = d.hits || [];
    $("#kbSearchHint").textContent = "　" + hits.length + " 段命中";
    out.dataset.filled = "1";
    if (!hits.length) {
      out.innerHTML = '<div class="blank">没有命中 —— 这表示'
        + '<b>你入库的 PDF 里没出现这些词</b>，不代表世上没有这类文献。'
        + '要么换个词，要么先把相关的 PDF 入库。</div>';
      return;
    }
    /* ★ 2026-10-27：这里原来左边直接摊内部主键（`kb63506101`）——
       列表路径早就改成「年份 语言」了（见上面 kbRows 的注释：
       "那是我们的主键，不是给人认的"），检索路径漏改，同一页面出现两套行结构。
       检索没有年份，但**有页码**，页码才是这一路真正有用的定位信息，换成年份位放它。 */
    out.innerHTML = hits.map(h =>
      '<div class="kbrow clickable" data-kb="' + esc(h.doc_id || "") + '" data-page="'
      + esc(String(h.page == null ? 1 : h.page)) + '" role="button" tabindex="0">'
      + '<div class="kbid" title="库内编号 ' + esc(h.doc_id || "-") + '">p.'
      + esc(String(h.page == null ? "?" : h.page)) + '</div>'
      + '<div class="kbt"><b>' + esc(kbClean(h.title) || "") + '</b><small>'
      + esc(h.snippet || "") + '</small></div></div>').join("")
      + '<div class="sub">这里是节选；要完整上下文就读命中那一页。</div>';
    kbWireRows(out, hits);
  } finally {
    KB_SEARCH_BUSY = false;
    button.disabled = false;
  }
}

let KB_UPLOAD_BUSY = false;
async function kbUpload(offline) {
  if (KB_UPLOAD_BUSY) return;
  const files = Array.from($("#kbFile").files || []);
  if (!files.length) { $("#kbUpOut").innerHTML = '<div class="blank">先选 PDF</div>'; return; }
  KB_UPLOAD_BUSY = true;
  $("#kbSend").disabled = true;
  $("#kbSendOff").disabled = true;
  const out = [];
  try {
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      $("#kbUpOut").innerHTML = '<div class="blank">入库中 ' + (i + 1) + '/' + files.length
        + '：' + esc(f.name) + (offline ? '（不联网）' : '') + '</div>';
      let d;
      try {
        const r = await fetch("/api/kb/upload?name=" + encodeURIComponent(f.name)
                              + (offline ? "&no_enrich=1" : ""),
                              { method: "POST", body: f });
        d = await r.json();
      } catch (e) { d = { ok: false, error: errMsg(e) }; }
      if (d && d.ok) {
        let s = '<div class="kbok">' + esc(f.name) + '：入库 ' + (d.added || 0) + ' 篇';
        if (d.skipped) s += '，跳过 ' + d.skipped + ' 篇（库里已有）';
        if (d.stored_name && d.stored_name !== f.name)
          s += '，存为 ' + esc(d.stored_name) + '（没覆盖同名原件）';
        s += '</div>';
        if (d.note) s += '<div class="kbwarn">' + esc(d.note) + '</div>';
        if (d.same_note) s += '<div class="kbwarn">' + esc(d.same_note) + '</div>';
        if (d.dup_suspect && d.dup_suspect.length)
          s += '<div class="kbwarn">' + esc(d.dup_note || "有入库后才发现重复的条目") + '</div>';
        if (d.unmatched && d.unmatched.length)
          s += '<div class="kbwarn">认不出的：' + esc(d.unmatched.join("、")) + '</div>';
        out.push(s);
      } else {
        out.push(kbFail(f.name, d || { error: "没有回包" }));
      }
      $("#kbUpOut").innerHTML = out.join("");
    }
    $("#kbFile").value = "";
    loadKb();
  } finally {
    KB_UPLOAD_BUSY = false;
    $("#kbSend").disabled = false;
    $("#kbSendOff").disabled = false;
  }
}

/* ═════════ 「让 AI 找文献」：检索 → 列候选 → 勾选 → 才入库（W23-B）═════════
   ★ 这一段**不含任何检索或下载实现**：两次请求打的是服务端 `/api/kb/find`
     与 `/api/kb/pick`，而那一头转的正是 agent 自己用的 `literature_search`
     与 `kb_add` —— 面板能列出来的，就是 agent 能查到的同一份。
   ★ 两步之间隔着**人**：检索这一步一个都不入，入库只处理勾选框里勾上的。
     自动全入会把库搞脏，而库脏了「只依据我的库回答」那条路就跟着脏。
   ★ KBFIND 是上一次检索的那一份候选；勾了哪些**以 DOM 里的勾选框为准**
     （不在 JS 里再存一份状态 —— 两份状态早晚会有一处没跟上）。 */
let KBFIND = [];
let KBFIND_HEAD = "";
let KBFIND_NOTE = "";
let KB_FIND_BUSY = false;
let KB_PICK_BUSY = false;
/* W25 · 候选表默认列出的行数。W23 验收报告 §10 记了这件事：limit 是**每源**
   上限，四源合并后候选一多表就有 36~46 行，真正的候选会被淹没。
   默认只列前 12 行（与单源 limit 同数），其余**如实说没列出来**（不是不存在）
   —— 截断必须自报，不许把"没看全"渲染成"就这么多"（M 型）。点一下全展开。 */
const KB_FIND_PAGE = 12;
let KB_FIND_FULL = false;

function kbPdfCell(c) {
  /* 「能不能拿到全文」：库里已有 ⇒ 库自己记着，这是**已知**的；
     库里没有 ⇒ literature_search 的回包里没这个字段，**只能报未知**。
     报"能"是猜，猜错了用户勾下去就是点了没反应。 */
  if (c.in_library) {
    return c.library_fulltext
      ? '<span class="tag" data-tone="success">库里有全文</span>'
      : '<span class="tag" data-tone="quiet">库里仅元数据</span>';
  }
  return '<span class="tag" data-tone="outline" title="'
    + esc("检索结果没给全文可得性，所以是未知；勾下去由 kb_add 实测，下不到会如实报")
    + '">未知</span>';
}

function kbFindTable() {
  const shown = KB_FIND_FULL ? KBFIND : KBFIND.slice(0, KB_FIND_PAGE);
  const hiddenN = KBFIND.length - shown.length;
  const rows = shown.map((c, i) => {
    /* 勾不动的原因有两种，必须分开说 —— 都写成"入不了"会让人以为库坏了。 */
    const why = c.can_pick ? ""
      : (c.in_library
        ? "库里已有这一篇（" + (c.library_id || "已记 DOI") + "）⇒ 不必再勾，勾了也会被跳过"
        : "检索结果没给 DOI ⇒ kb_add 这条路入不了，勾了也是白勾");
    return '<tr data-pk-row="' + i + '">'
      + '<td class="picksel"><label class="checkbox"><input type="checkbox" data-pk="' + i + '"'
      + (c.can_pick ? "" : " disabled")
      + (why ? ' title="' + esc(why) + '"' : "") + '></label></td>'
      + '<td><b>' + esc(c.title) + '</b>'
      + (c.in_library
        ? '<span class="picklib">★ 库里已有' + (c.library_id ? '（' + esc(c.library_id) + '）' : '')
          + ' —— 勾了也会被跳过，不用再勾</span>'
        : "")
      + (c.doi ? "" : '<span class="picklib">没给 DOI，入不了</span>')
      + '</td>'
      + '<td class="m">' + esc(String(c.year == null ? "-" : c.year)) + '</td>'
      + '<td class="m">' + esc(c.venue || "-") + (c.source ? '<span class="picklib">'
        + esc(c.source) + '</span>' : "") + '</td>'
      + '<td class="pk-doi">' + esc(c.doi || "（无）") + '</td>'
      + '<td class="m">' + kbPdfCell(c) + '</td></tr>';
  }).join("");
  return '<div class="kbfindscroll" tabindex="0" role="region" aria-label="文献候选表，可横向滚动">'
    + '<table class="grid"><thead><tr>'
    + '<th class="picksel" title="勾中哪些才入哪些">选</th>'
    + '<th>标题</th><th>年份</th><th>来源 / 期刊</th><th>DOI</th><th>能不能拿到全文</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table></div>'
    + (hiddenN > 0
      ? '<div class="pkbar"><span class="pkcount">还有 <b>' + hiddenN
        + '</b> 条**没列出来**（不是不存在）—— 全展开再勾也不迟</span>'
        + '<button class="mini" id="kbFindMore">显示全部 ' + KBFIND.length + ' 条</button></div>'
      : "")
    + '<div class="pkbar"><span class="pkcount" id="kbPickCount">还没有勾选</span>'
    + '<button class="mini" id="kbPickNone" title="清掉所有勾选">清空勾选</button>'
    + '<button class="mini pri" id="kbPickGo" title="只把勾中的那几篇下载入库，一个都不多入">入库选中</button>'
    + '</div>';
}

function kbWireFind(box) {
  box.querySelectorAll('input[data-pk]').forEach(el => {
    el.onchange = () => {
      el.closest("tr").classList.toggle("picked", el.checked);
      paintPickCount();
    };
  });
  const more = $("#kbFindMore");
  if (more) more.onclick = () => kbFindRender(true);
  $("#kbPickGo").onclick = kbPick;
  $("#kbPickNone").onclick = () => {
    box.querySelectorAll('input[data-pk]').forEach(el => {
      el.checked = false;
      el.closest("tr").classList.remove("picked");
    });
    paintPickCount();
  };
}

function kbFindRender(full) {
  KB_FIND_FULL = full;
  const box = $("#kbFindOut");
  box.innerHTML = KBFIND_HEAD + kbFindTable() + KBFIND_NOTE;
  kbWireFind(box);
  paintPickCount();
}

async function kbFind() {
  if (KB_FIND_BUSY || KB_PICK_BUSY) return;
  const q = $("#kbFindQ").value.trim();
  const box = $("#kbFindOut");
  if (!q) { box.innerHTML = '<div class="blank">先说一句要找什么</div>'; return; }
  KB_FIND_BUSY = true;
  $("#kbFindGo").disabled = true;
  $("#kbFindHint").textContent = "";
  box.innerHTML = '<div class="blank">检索中…（OpenAlex / arXiv / Crossref / PubMed 四源，'
    + '这一步<b>一个都不入</b>；最长约 2 分钟）</div>';
  try {
    let d;
    try {
      d = await api("/api/kb/find", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, limit: 12 }),
        /* 四源各自 30s 超时，并行的最坏情况会超过旧的 12s 死线。 */
        timeout: 120000
      });
    } catch (e) { d = { ok: false, error: errMsg(e) }; }
    if (!d || !d.ok) { box.innerHTML = kbFail("检索候选", d); return; }
    KBFIND = d.candidates || [];
    KBFIND_HEAD = "";
    KBFIND_NOTE = d.pdf_note ? '<div class="kbwarn">' + esc(d.pdf_note) + '</div>' : "";
    if (!KBFIND.length) {
      box.innerHTML = '<div class="blank">四源都没回候选 —— 这表示<b>这一轮检索没命中</b>，'
        + '不代表世上没有这类文献。换个说法（换英文学名、换同义词）再来一趟。</div>';
      return;
    }
    const dup = KBFIND.filter(c => c.in_library).length;
    KBFIND_HEAD = '<div class="sub">' + KBFIND.length + ' 条候选'
      + (d.limit_note ? '（' + esc(d.limit_note) + '）' : '')
      + (dup ? '，其中 <b>' + dup + ' 条库里已经有</b>（已标出，勾了也会被跳过）' : '')
      + (d.library_note ? '　' + esc(d.library_note) : '') + '</div>';
    kbFindRender(false);
    $("#kbFindHint").textContent = "　" + KBFIND.length + " 条待你勾选";
  } finally {
    KB_FIND_BUSY = false;
    $("#kbFindGo").disabled = false;
  }
}

function pickedRows() {
  return Array.from(document.querySelectorAll('#kbFindOut input[data-pk]:checked'))
    .map(el => KBFIND[Number(el.dataset.pk)])
    .filter(Boolean);
}

function paintPickCount() {
  const el = document.getElementById("kbPickCount");
  if (!el) return;
  const n = pickedRows().length;
  el.textContent = n ? ("已勾 " + n + " 篇 · 入库只会动这 " + n + " 篇") : "还没有勾选";
  const go = document.getElementById("kbPickGo");
  if (go) go.disabled = KB_PICK_BUSY || !n;
  const none = document.getElementById("kbPickNone");
  if (none) none.disabled = KB_PICK_BUSY;
}

async function kbPick() {
  if (KB_PICK_BUSY || KB_FIND_BUSY) return;
  const rows = pickedRows();
  const box = $("#kbFindOut");
  if (!rows.length) {
    box.insertAdjacentHTML("afterbegin",
      '<div class="kbwarn">一篇都没勾 —— 先勾再入库（这一步不替你决定入哪几篇）。</div>');
    return;
  }
  KB_PICK_BUSY = true;
  $("#kbFindGo").disabled = true;
  paintPickCount();
  box.querySelectorAll('input[data-pk]').forEach(el => { el.disabled = true; });
  box.insertAdjacentHTML("afterbegin",
    '<div class="blank" id="kbPickProg">入库中…逐篇走 <code>kb_add</code>（下载 PDF + 建段落索引，'
    + '每篇要几秒到几十秒）</div>');
  const pickT0 = Date.now();
  const pickTimer = setInterval(() => {
    const el = $("#kbPickProg");
    if (el) el.textContent = "入库中…已等 " + Math.round((Date.now() - pickT0) / 1000)
      + "s（逐篇下载 + 建索引，最长约 10 分钟；不要关页面）";
  }, 1000);
  try {
    let d;
    try {
      d = await api("/api/kb/pick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dois: rows.map(r => r.doi) }),
        /* 逐篇 kb_add 是同步的：20 篇 × 几十秒，旧的 12s 死线会把
           “服务器还在写”误报成失败。给足 10 分钟。 */
        timeout: 600000
      });
    } catch (e) { d = { ok: false, error: errMsg(e) }; }
    /* `ok=false` 只代表“有一篇失败”，此时 `rows` 仍然有逐篇结果；
       不要因为部分失败把整份结果丢掉、只显示一句“入库失败”。 */
    if (!d || (d.ok === false && !(d.rows || []).length)) {
      KBFIND_HEAD = kbFail("入库", d || { error: "没有回包" }) + KBFIND_HEAD;
      kbFindRender(KB_FIND_FULL);
      return;
    }
    if (d.ok === false) {
      box.insertAdjacentHTML("afterbegin",
        '<div class="kbwarn">有 ' + ((d.counts || {}).failed || 0)
        + ' 篇失败；下面按篇列出结果，失败原因照实写。</div>');
    }
    const c = d.counts || {};
    const kindLab = {
      fulltext: ["kbok", "入了，且有全文"],
      metadata: ["kbwarn", "入了，但只拿到元数据（PDF 没下到）"],
      duplicate: ["kbwarn", "跳过：库里已有"],
      failed: ["kbbad", "失败"]
    };
    const lines = (d.rows || []).map(r => {
      const k = kindLab[r.kind] || ["kbwarn", String(r.kind)];
      return '<div class="' + k[0] + '">' + esc(r.doi) + '：<b>' + k[1] + '</b>'
        + (r.title ? '　' + esc(r.title) : "")
        + (r.pages ? '　' + r.pages + ' 页已建段落索引' : "")
        + (r.id ? '　' + esc(r.id) : "")
        + (r.error ? '<br>原因：' + esc(r.error) : "") + '</div>';
    }).join("");
    const sum = '<div class="sub">入库结束：入了 ' + ((c.fulltext || 0) + (c.metadata || 0))
      + ' 篇（其中有全文 ' + (c.fulltext || 0) + '、仅元数据 ' + (c.metadata || 0)
      + '），跳过重复 ' + (c.duplicate || 0) + ' 篇，失败 ' + (c.failed || 0) + ' 篇。'
      + (c.metadata ? '⚠️ 仅元数据的那些段落检索查不到内容：要么自己找到 PDF 手动入库，要么换一篇候选。' : '')
      + '</div>';
    box.innerHTML = sum + lines;
    KBFIND = [];
    KBFIND_HEAD = "";
    KBFIND_NOTE = "";
    $("#kbFindHint").textContent = "";
    loadKb();
  } finally {
    clearInterval(pickTimer);
    KB_PICK_BUSY = false;
    $("#kbFindGo").disabled = KB_FIND_BUSY;
    paintPickCount();
  }
}

$("#kbGo").onclick = kbSearch;
$("#kbFindGo").onclick = kbFind;
$("#kbFindQ").addEventListener("keydown", e => { if (e.key === "Enter") kbFind(); });$("#kbAll").onclick = kbListAll;
$("#kbSend").onclick = () => kbUpload(false);
$("#kbSendOff").onclick = () => kbUpload(true);
$("#kbQ").addEventListener("keydown", e => { if (e.key === "Enter") kbSearch(); });

/* ═════════════ 顶栏 / 侧栏 ═════════════ */
function statusWhy() {
  const p = STATUS.provider || {};
  if (STATUS.config_error) return String(STATUS.config_error);
  if (!STATUS.kernel_ok) return String(STATUS.kernel_error || "内核未就绪");
  if (p.state && p.state !== "ok") return String(p.state);
  return "";
}
function paintTop() {
  const top = $("#topMeta");
  /* 首次状态还没回来时，别把"还不知道"画成"未连接"——
     那正是用户看到的假红：页面刚打开、/api/status 还在路上。 */
  if (!STATUS_READY) {
    top.innerHTML = '<span class="sdot"></span><span title="正在读取本机配置与密钥">连接中…</span>';
    return;
  }
  const p = STATUS.provider || {}, ok = STATUS.kernel_ok && p.state === "ok";
  const el = LIVE && T0 ? " · " + ((Date.now() - T0) / 1000).toFixed(0) + "s" : "";
  if (LIVE) {
    top.innerHTML = '<span class="spin"></span>进行中' + el;
    return;
  }
  /* 普通人要的是"它在线吗"，不是网关名 —— 原样信息收进 tooltip；
     未连接时必须把**真实原因**带上，否则只剩一个没法行动的灰点。 */
  const why = statusWhy();
  const tip = [p.name, p.model].filter(Boolean).join(" · ") + (why ? " · " + why : "");
  top.innerHTML =
    '<span class="sdot ' + (ok ? "" : "bad") + '" title="' + esc(tip) + '"></span>' +
    '<span title="' + esc(tip) + '">' + (ok ? "在线" : "未连接") + "</span>";
}
function startTimer() {
  stopTimer();
  TIMER = setInterval(() => { paintTop(); tickProg(); }, 1000);
}
function stopTimer() { if (TIMER) { clearInterval(TIMER); TIMER = null; } }

async function loadStatus() {
  try {
    STATUS = await api("/api/status");
    STATUS_READY = true;
    STATUS_RETRY = 0;
  } catch (e) {
    $("#foot").innerHTML = '<span class="sdot bad"></span>连不上服务端';
    /* 首次读失败时自动重试；已经读到过就保留上一次的真实状态，
       不因为一次失败把"在线"翻成"未连接"。 */
    if (!STATUS_READY && STATUS_RETRY < 3) {
      STATUS_RETRY += 1;
      setTimeout(loadStatus, 1500);
    }
    return;
  }
  const p = STATUS.provider || {}, ok = STATUS.kernel_ok && p.state === "ok";
  /* 侧栏只留"能不能用"。
     原来的 tokens / 缓存命中率 / 成本 UNKNOWN / 记忆条数全在这里堆着 ——
     那是给 agent 作者看的，不是给科研用户看的。它们没被删，只是搬到了
     「轨迹」页与成果卡的「技术细节」里（判定与口径一行没改）。 */
  /* 正常时**不说话**：顶栏那颗「在线」pill 的 title 里本来就有 name·model，
     这里再念一遍"就绪 / 模型名"是同一件事的第二个说法，还要吃掉 51px
     —— 那是历史列表能多露 1.5 行的高度。只在真的出问题时才现身。 */
  const why = statusWhy();
  $("#foot").innerHTML = why ? '<span class="sdot bad"></span>' + esc(why) : "";
  if (!STATUS.kernel_ok)
    $("#hint").innerHTML = '<span style="color:var(--err)">装载未就绪：' + esc(STATUS.kernel_error || "") + "</span>";
  paintTop();
  bindSessFilter();
  loadSessions();
  loadArts();
  wbOverview();
  if (STATUS.active && !CUR) attach(STATUS.active, "（重连正在跑的任务）");
}

/* ═════════════ 首屏概览（工作台）═════════════
   ★ 数据一格都不写死：四条接口各读各的，谁读不到谁那一格标"读不到"。
     不把读不到当 0 —— 「0 件产出」和「读不到产出数」是两件事，混成一个
     数字就是在说谎（这套判据在账单页已经栽过一次，UNKNOWN 不能被当 0）。
   ★ 失败不打扰主流程：这是首屏装饰性信息，接口挂了也只是那几格变灰，
     绝不让它抛错 —— 它在 loadStatus() 尾部被调用，抛错会中断后面的
     attach() 重连。
   ★ 只在首屏可见时才有意义：跑起来之后 #empty 被隐藏，这一块跟着走。 */
function wbNum(n) { return (typeof n === "number" && isFinite(n)) ? String(n) : null; }
/* ★ `allSettled` 只管「失败」，不管「不回」。一条路由挂住时，
   首屏那四格就永远停在「— 读取中…」—— 而那正是第一眼看到的东西。
   超时按「读不到」处理，**绝不能变成假 0**。 */
function wbDeadline(p, ms) {
  return Promise.race([p, new Promise((_, rej) =>
    setTimeout(() => rej(new Error(ms + "ms 内没应答")), ms))]);
}
function wbCell(idx, num, label, note, stale) {
  const box = document.querySelectorAll("#wbStat .kbn")[idx];
  if (!box) return;
  const b = box.querySelector("b"), s = box.querySelector("span"), i = box.querySelector("i");
  if (b) b.textContent = num === null ? "—" : num;
  if (s) s.textContent = label;
  if (i) i.textContent = note || "";
  box.dataset.stale = stale ? "1" : "0";
}
async function wbOverview() {
  /* 四条并发。`allSettled` 而非 `all`：一条挂掉不该让另三条的读数一起消失。 */
  const [runs, deliv, figs, kb] = await Promise.allSettled([
    wbDeadline(api("/api/runs?n=200"), 4000),
    wbDeadline(api("/api/deliverables"), 4000),
    wbDeadline(api("/api/figures"), 4000),
    wbDeadline(api("/api/kb/stat"), 4000),
  ]);
  const pick = (r) => (r.status === "fulfilled" ? r.value : null);

  /* ① 产出：件数 + 按类型 */
  const dv = pick(deliv);
  const dvN = dv && wbNum(dv.total);
  if (dvN === null) wbCell(0, null, "产出", "读不到", true);
  else {
    const kinds = dv.by_kind ? Object.keys(dv.by_kind).length : 0;
    wbCell(0, dvN, "产出", kinds ? kinds + " 种格式" : "点开看全稿", false);
  }

  /* ② 文献库：篇数 + 其中多少篇抽出了全文 */
  const kbs = pick(kb);
  const kbN = kbs && wbNum(kbs.total);
  if (kbN === null) wbCell(1, null, "文献库", "读不到", true);
  else {
    const ft = wbNum(kbs.fulltext);
    wbCell(1, kbN, "文献库", ft === null ? "点开可查原文" : ft + " 篇有全文", false);
  }

  /* ③ 配过的图：组数（不是张数 —— 接口按任务分组给） */
  const fg = pick(figs);
  const fgItems = fg && Array.isArray(fg.items) ? fg.items : null;
  if (!fgItems) wbCell(2, null, "配过的图", "读不到", true);
  else {
    const withFigs = fgItems.filter(x => Array.isArray(x.figs) && x.figs.length).length;
    wbCell(2, withFigs, "配过的图", withFigs ? "都有体检报告" : "还没配过图", false);
  }

  /* ④ 最近任务：接口只读最近 200 条，不能冒充历史总数。 */
  const rn = pick(runs);
  const rnList = rn && Array.isArray(rn.runs) ? rn.runs : null;
  if (!rnList) wbCell(3, null, "最近任务", "读不到", true);
  else {
    const done = rnList.filter(r => r.success === true).length;
    wbCell(3, rnList.length, "最近任务",
      "本次读取 " + rnList.length + " 条" + (done ? " · " + done + " 次完成" : ""), false);
  }
}
/* 概览里的快捷入口：走的还是 goView()，与侧栏同一个切页函数 ——
   不另写一套切页逻辑（那就是第二套「怎么切页」的真相）。
   ★ 用 `data-go` 而不是 `data-v`：`data-v` 是导航按钮的专属标记，
     下面那段 `document.querySelectorAll(".navbtn[data-v]").forEach(...)`
     会给每个带 data-v 的元素挂 onclick 并做 `.on` 高亮。概览里再挂一份
     data-v 就成了**同一个视图的两个导航按钮** —— 高亮会打架（点了概览里
     的「产出」，侧栏那一行不会亮），而且是第二套「哪些元素是导航」的真相。
     换个属性名，两套就彻底分开：概览只借 goView() 这一个函数。 */
async function openLatestProcess() {
  if (!SESS.length && !SESS_ERR) await loadSessions();
  const r = SESS.find(canReplay);
  if (!r) {
    $("#hint").innerHTML = '<span style="color:var(--warn)">还没有可查看的执行记录。先完成一个任务，再回这里。</span>';
    goView("chat");
    return;
  }
  openSession(r);
  goView("traj");
}
document.querySelectorAll("#wbGo .wg").forEach(b => {
  b.onclick = () => b.dataset.go === "traj" ? openLatestProcess() : goView(b.dataset.go);
});

/* ═════════════ 新任务主链：先整理任务单，确认后才执行 ═════════════ */
let PREVIEWING = false;
function flagGoalMissing() {
  const goal = $("#goal");
  goal.setAttribute("aria-invalid", "true");
  $("#hint").innerHTML = '<span style="color:var(--warn)">先写下你想完成的事，例如：帮我总结这篇论文。</span>';
  goal.focus();
  return false;
}
function restoreEmptyComposer() {
  if (!CUR && $("#thread").hidden) {
    $("#empty").hidden = false;
    $("#wbOverview").hidden = false;
  }
}
function submitComposer() {
  if (PREVIEWING) return;
  if (CUR && CUR.followable && !DRAFT) return send();
  return previewContract();
}
async function previewContract() {
  if (PREVIEWING || !pillFormApply()) return;
  const goal = $("#goal").value.trim();
  if (!goal) return flagGoalMissing();
  if (CUR && LIVE) { $("#hint").innerHTML = '<span style="color:var(--warn)">上一个任务还在跑，请先停止或等它结束。</span>'; return; }
  PREVIEWING = true;
  $("#btnPreview").disabled = true; $("#btnSend").disabled = true;
  $("#hint").innerHTML = '<span class="spin"></span>正在整理任务单（还不会执行）…';
  try {
    const d = await api("/api/contract", { method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({ goal, max_steps: MAXSTEPS, mode: MODE, preset: PRESET }) });
    DRAFT = d.contract;
    if (!DRAFT) throw new Error("服务端没有返回任务单，请重试或换个说法。");
    renderContractPreview();
    $("#hint").textContent = "";
  } catch (e) {
    DRAFT = null;
    restoreEmptyComposer();
    $("#hint").innerHTML = '<span style="color:var(--err)">任务单没整理出来：' + esc(e.message) +
      '。原话已保留，可以重试或返回修改。</span>';
  } finally {
    PREVIEWING = false;
    $("#btnPreview").disabled = false; $("#btnSend").disabled = false;
  }
}
function renderContractPreview() {
  const c = DRAFT;
  if (!c) { $("#cPreview").innerHTML = ""; return; }
  $("#empty").hidden = true; $("#wbOverview").hidden = true;
  const li = (arr) => (arr && arr.length)
    ? "<ul>" + arr.map(x => "<li>" + esc(x) + "</li>").join("") + "</ul>"
    : '<div class="ro" style="color:var(--fg-cap)">（无）</div>';
  $("#cPreview").innerHTML =
  '<div class="cprev">' +
    '<div class="ch" id="cPrevTitle" tabindex="-1">开工前，先确认这三件事<span class="pill dim">尚未执行</span></div>' +
    '<div class="cb">' +
      '<div class="fld"><label>准备做什么</label><div class="ro">' + esc(c.goal || c.raw_goal) + "</div></div>" +
      '<div class="fld"><label>会交付什么</label>' + li(c.deliverables) + "</div>" +
      '<div class="fld"><label>怎样算完成（可改，每行一条）</label>' +
        '<textarea id="cAcc" rows="' + Math.min(6, Math.max(2, (c.acceptance||[]).length + 1)) + '">' +
        esc((c.acceptance || []).join("\n")) + "</textarea></div>" +
      '<details><summary>更多要求（字数、步数与禁止项）</summary>' +
      '<div class="fld"><label>目标字数（留空 = 不限）</label>' +
        '<input id="cTgt" type="number" min="0" value="' + (c.target_chars || "") + '"></div>' +
      '<div class="fld"><label>步数上限（默认 ' +
        (STATUS.loop && STATUS.loop.max_steps ? STATUS.loop.max_steps : 40) + '）</label>' +
        '<input id="cMax" type="number" min="1" value="' + (MAXSTEPS || "") + '" placeholder="留空 = 默认"></div>' +
      (c.forbidden && c.forbidden.length
        ? '<div class="fld"><label>禁止</label>' + li(c.forbidden) + "</div>" : "") +
      '</details>' +
    "</div>" +
    '<div class="cf"><button class="mini pri" id="cGo">确认，开始执行</button>' +
      '<button class="mini" id="cCancel">返回修改</button>' +
      '<button class="mini" id="cAgain">重新整理</button></div>' +
  "</div>";
  $("#cGo").onclick = () => {
    DRAFT.acceptance = $("#cAcc").value.split("\n").map(s => s.trim()).filter(Boolean);
    const t = $("#cTgt").value.trim();
    DRAFT.target_chars = t ? parseInt(t, 10) : null;
    const mx = $("#cMax").value.trim();
    MAXSTEPS = mx ? parseInt(mx, 10) : null;
    $("#cPreview").innerHTML = "";
    const confirmed = DRAFT; DRAFT = null;
    send(confirmed);
  };
  $("#cAgain").onclick = () => { $("#cPreview").innerHTML = ""; DRAFT = null; previewContract(); };
  $("#cCancel").onclick = () => {
    $("#cPreview").innerHTML = ""; DRAFT = null; $("#hint").textContent = "";
    if (!LIVE) setMentor(false);
    restoreEmptyComposer(); $("#goal").focus();
  };
  $("#cPrevTitle").focus();
  $("#cPrevTitle").scrollIntoView({ block:"nearest" });
}

/* ═════════════ 导师模式（W22-B）═════════════
   三条纪律（用户定死，不许改）：① **每个会话一个开关**（不是全局模式）
   ② 优先用库 → 库里不够允许联网补 → **必须标注哪句来自库、哪句来自外网**
   ③ 库里没有时**明说**，再联网补并标注。

   ★ 这个开关不是前端的自我感觉：它进 `POST /api/run` 的 `kb_only` 字段，
     内核据此决定首条消息里注入不注入那段纪律（`rongke/loop.py: initial_messages`），
     跑完还从**调用台账**算那三条出处判据（`mentor_trace_audit`）。
     判据不采信模型的自我陈述 —— 界面这里显示的"合格/违规"是算出来的，不是它说的。
   ★ 状态真相当晚才知道：一场会话"当时开没开"由内核发的 `mentor_mode` 事件说了算，
     回放旧会话时由那条事件把开关摆回它当时的位置 —— 不是前端记的一个数。 */
let MENTOR = false;
let TOPIC = null;          // 最近一次从 /api/topic 读回来的课题台账（null = 还没读到）

function setMentor(on, why) {
  on = !!on;
  MENTOR = on;
  const sw = $("#mentorSwitch"), wrap = $("#mentorWrap"), tag = $("#mentorTag"),
        bar = $("#mentorBar");
  if (sw) sw.setAttribute("aria-checked", on ? "true" : "false");
  if (wrap) wrap.dataset.on = on ? "1" : "0";
  if (bar) bar.dataset.on = on ? "1" : "0";
  if (tag) {
    tag.textContent = on ? "开" : "关";
    tag.dataset.tone = on ? "info" : "neutral";
  }
  paintMentorBar();
  if (window.RongkeMentor) RongkeMentor.paint();
  if (why) note(why, "prog");
}
function toggleMentor() {
  if (LIVE) {
    $("#hint").innerHTML = '<span style="color:var(--warn)">任务在跑，这一场的开关先别动</span>';
    return;
  }
  setMentor(!MENTOR);
  if (MENTOR) loadTopic();
}

/* ═════════════ 任务模式 / 仅问答（W36）═════════════
   一句话：**这一轮按任务走，还是只是问我。**

   为什么要有它（用户原话「直接加个任务模式和仅问答不就行了」）：
     在这之前，"这一轮算不算任务"全靠契约起草器去猜。它被要求"绝不添加用户没说过
     的要求"，对一句问话只能给出空验收标准；而证据门对空验收标准一律判"无法判定
     完成" ⇒ **问话永远过不了门**。归档 `runs/20261031-085105` 的现场是：用户问
     "我下一步该做什么"，31 条 assistant 消息全是空正文 + 工具调用，一个字没答，
     两次交付都被打回，最后用户叫停。

   所以这件事该由**用户**说，不该由我们猜。这里的两个按钮就是那句话的落地：
     · `task`（默认）—— 与今天完全一致：要产物、要证据、交付过证据门。
     · `answer`       —— 只是问：直接回答，不要产物、不要验收标准。
   两种模式下「不许编造」都照旧生效（门的第 2 条不看模式）。

   ★ 状态只有这一份 `MODE`。它进 `POST /api/run` 与 `POST /api/follow` 的 `mode`
     字段，后端 `mode_from()` 再翻成内核认得的值 —— 界面不自己解释它。 */
let MODE = "task";

function setMode(mode, why) {
  MODE = (mode === "answer") ? "answer" : "task";
  const task = $("#modeTask"), ans = $("#modeAnswer");
  // 两个按钮一组：`aria-pressed` 是**唯一**的选中表达（不另加 class 表达同一件事，
  // 那是两套真相）。样式由 `[aria-pressed="true"]` 选。
  if (task) task.setAttribute("aria-pressed", MODE === "task" ? "true" : "false");
  if (ans) ans.setAttribute("aria-pressed", MODE === "answer" ? "true" : "false");
  // 提示条只说**这一轮会怎么走**，不复述内部机制。
  const hint = $("#hint");
  if (hint && MODE === "answer") {
    hint.innerHTML = '仅问答：直接回答，不要产物。需要我交东西时切回「任务模式」。';
  }
  if (why) note(why, "prog");
}
function wireMode() {
  const wrap = $("#modeWrap");
  if (!wrap) return;
  wrap.querySelectorAll("[data-mode]").forEach(b => {
    b.onclick = () => {
      if (LIVE) {
        $("#hint").innerHTML =
          '<span style="color:var(--warn)">任务在跑，这一轮的模式先别动</span>';
        return;
      }
      // 再按一次同一个不取消 —— 模式是二选一，不是开关。
      setMode(b.dataset.mode);
    };
  });
}
/* 会话条：只在开着时出现。内容全从内核读回来，前端**一个字都不自己编**。 */
function paintMentorBar() {
  const bar = $("#mentorBar");
  if (window.RongkeMentor) { RongkeMentor.paint(); return; }
  if (!bar) return;
  if (!MENTOR) { bar.innerHTML = ""; return; }
  const t = TOPIC;
  let h = '<div class="mrow"><span class="mk">当前课题：</span>';
  if (!t || t.error) {
    h += '<span class="tag" data-tone="warning">' +
         esc(t && t.error ? t.error : "课题台账读不到") + '</span>' +
         '<span class="mnote">这是内核读数取不到，不代表你没有课题。</span>';
  } else if (!t.current) {
    h += '<span class="tag" data-tone="outline">还没立课题</span>' +
         '<span class="mnote">立一个，之后的会话才记得住你读到哪、卡在哪。</span>';
  } else {
    const open = Number(t.open_total || 0);
    h += '<span class="mt">' + esc(t.slug || t.current) + '</span>' +
         (t.title && t.title !== t.slug ? '<span class="mnote">' + esc(t.title) + '</span>' : "") +
         '<span class="tag" data-tone="' + (open ? "warning" : "success") + '">' +
         "未闭环卡点 " + open + " 条</span>";
  }
  h += "</div>";
  /* 立 / 切课题：最小接线（工单 §3.5 明写"不要求做完整的课题管理界面"）。
     动作全部走 POST /api/topic → 内核的 topic_use，界面不自己写 topics/。 */
  const known = (t && Array.isArray(t.topics)) ? t.topics : [];
  h += '<div class="mrow"><span class="mk">立 / 切课题</span>' +
       '<span class="ipt"><input id="topicSlug" list="topicList" placeholder="课题名（字母数字中文都行）">' +
       "</span>" +
       (known.length ? '<datalist id="topicList">' +
         known.map(s => '<option value="' + esc(s) + '">').join("") + "</datalist>" : "") +
       '<button class="btn outline sm" id="btnTopicUse">切过去 / 新建</button>' +
       '<span class="mnote" id="topicMsg"></span></div>';
  /* 未闭环卡点：内核纪律①「未闭环必须浮现」—— 这里照它的口径列，超了如实报数。 */
  if (t && t.current && Array.isArray(t.blockers)) {
    const ops = t.blockers.filter(b => b.status === "open").slice(0, 3);
    if (ops.length) {
      h += '<div class="mrow"><span class="mk">卡在这些地方</span></div><ul class="bl">' +
           ops.map(b => "<li><b>" + esc(b.id || "") + "</b> " + esc(b.what || "") + "</li>").join("") +
           "</ul>";
      const rest = Number(t.open_total || 0) - ops.length;
      if (rest > 0) h += '<div class="mrow"><span class="mnote">还有 ' + rest +
                         ' 条没列出来（不是不存在）。</span></div>';
    }
  }
  bar.innerHTML = h;
  const btn = $("#btnTopicUse");
  if (btn) btn.onclick = useTopic;
  const inp = $("#topicSlug");
  if (inp) inp.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); useTopic(); } };
}
async function useTopic() {
  const el = $("#topicSlug");
  const slug = el ? String(el.value || "").trim() : "";
  const msg = $("#topicMsg");
  if (!slug) { if (msg) msg.textContent = "先给课题起个名字"; return; }
  if (msg) msg.textContent = "…";
  let d;
  try {
    d = await api("/api/topic", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "use", slug }) });
  } catch (e) {
    if (msg) msg.textContent = "没切成：" + e.message;
    return;
  }
  if (msg) msg.textContent = d && d.ok
    ? (d.created ? "已立新课题" : "已切到该课题")
    : "没切成：" + ((d && (d.error || d.message)) || "内核没回");
  await loadTopic();
}
/* ★ 读回来的**不是**界面自己的猜测：课题住在内核的 topics/ 台账里。
   读不到就如实留着错误，不把失败渲染成"没有课题"（本仓定律：两件事）。 */
async function loadTopic() {
  try {
    TOPIC = await api("/api/topic");
  } catch (e) {
    TOPIC = { error: e.message };
  }
  if (window.RongkeMentor) RongkeMentor.refresh();
  else paintMentorBar();
}
/* ★ 出处审计（工单 §3.3 三条判据）：这些勾和叉是内核**从调用台账算出来**的，
   不是模型自述"我标注了"。界面只做一件事：如实摆出来 —— 未通过就用警示色，
   不许粉饰成"没有结果"（把失败渲染成空，是本仓反复吃的 M 型）。 */
function mentorAuditCitations(host, citations) {
  const tx = host.querySelector('.nx > div');
  if (!tx || !citations.length) return;
  const row = document.createElement('div'); row.className = 'mw-doc-tools';
  citations.forEach(c => {
    const b = document.createElement('button'); b.type = 'button'; b.className = 'mini';
    b.dataset.mentorDoc = c.doc_id; b.dataset.mentorPage = c.page;
    b.textContent = c.doc_id + ' p.' + c.page;
    b.onclick = () => openOrig({kb:c.doc_id, title:b.textContent, n:Number(c.page)||1});
    row.appendChild(b);
  });
  tx.appendChild(row);
}
/* 出处复核（2026-11-04 结构收敛）：**只读内核给的 `verdict` 三态**，
   不在前端另算一遍「通过 / 未通过」。内核已声明 `advisory`（只挂事实、不是门），
   前端此前完全不读它，把 `citations=[]`（其实是「格式读不到」）渲染成
   「审计未通过：用了不说，等于没依据」—— 与屏幕上明明有出处的正文直接矛盾。 */
function renderMentorAudit(d) {
  if (!d) return;
  if (d.error) {
    note("出处复核没跑成（**未测**，不是通过）：" + d.error, "warn",
         "复核异常 ⇒ 这一层没测。「没测」不等于「没有违规」，也不等于「通过」。");
    return;
  }
  const j = d.judgments || {};
  const v = d.verdict || (d.pass ? "pass" : "fail");
  const mark = x => x === true ? "✔" : (x === false ? "✘" : "○");
  const cs = d.citations || [], bad = cs.filter(c => c.verdict !== "grounded");
  const nets = d.net_calls || [];
  const a = mark(j.must_query_library) + (j.must_query_library === null
            ? " 本条不适用（普通任务没有「必须查库」这条纪律）"
            : " 真查过库（成功 " + (d.kb_ok_calls || 0) + " 次）");
  /* 判据②的三态：**未测** ≠ 不符合。零命中时没标出处本来就是对的，
     标注格式读不到则是一句「无法判定」，都不许写成被抓到违规。 */
  const b = mark(j.citations_ground) + (j.citations_ground === null
            ? " 库内出处**未测**（标注格式读不到，无法判定）"
            : (d.library_hit === false
               ? " 库里没命中：这次**不该**有库内出处（标了才算违规）"
               : " 库内出处对得上本次命中集（" + (cs.length - bad.length) + "/" + cs.length + " 处）"));
  const c = mark(j.external_marked) + " 联网补的都指出了来源（" +
            nets.length + " 次调用 / " + (d.external_marks || []).length + " 处标注）";
  const unk = d.unknown_reasons || [];
  const tech = [
    "三条判据全部来自本次调用的台账（ToolCall 记录），不采信模型的自我陈述。",
    "复核读过的正文：" + ((d.sources || []).join("、") || "（只有聊天答复）"),
    "本次检索命中的文献：" + ((d.hit_docs || []).join("、")
      || "（一条都没有 —— 这只说明**我入库的这几篇**里没有，不代表世上没有这类文献）"),
    "全库清单：" + (d.library_ids_known
      ? "已取得 " + d.library_size + " 篇（可判「库里查无此 id」）" : "未取得（该层未测）"),
    "逐条出处判定：" + (cs.length
      ? cs.map(x => x.doc_id + " p." + x.page + " → " + x.verdict +
                    (x.form ? "（" + (x.form === "tight" ? "规范标记" : "散文式标注") + "）" : "")).join("；")
      : "（没读出可复核的页码标注）"),
    "联网调用：" + (nets.join("、") || "（无）"),
    d.note || "",
    unk.length ? "【未测项】" + unk.join("；") : "",
    d.advisory ? "这是来源级提示，不是交付门：不影响本次任务的成功与否。" : "",
  ].filter(Boolean).join("\n");
  window.MENTOR_AUDIT_CITES = cs;
  const title = v === "unknown" ? "出处复核：未测"
              : v === "fail" ? "出处复核：发现对不上的地方"
              : "出处复核：已核对";
  const lead = v === "fail" ? ((d.violations || []).join("；") || (a + "　" + b + "　" + c))
             : v === "unknown" ? (unk.join("；") || "判据依据不全，无法判定")
             : (d.summary || (a + "　" + b + "　" + c));
  note(title + "　" + a + "　" + b + "　" + c + "\n" + lead,
       v === "fail" ? "err" : (v === "unknown" ? "warn" : "mentor-audit"), tech);
}
$("#mentorWrap").onclick = toggleMentor;
$("#mentorSwitch").onkeydown = e => {
  if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggleMentor(); }
};

/* 模式开关的接线（W36）。放在这里与导师开关同一处初始化 ——
   两个开关都住在 `.ctool` 那一行，各接各的，但都在这一个 IIFE 里注册一次。 */
wireMode();
setMode("task");   // 默认与今天的行为一致：打开页面就是任务模式

/* ═════════════ 发任务 / 会话内追问 ═════════════ */
async function send(frozen, steps) {
  if (!pillFormApply()) return;  // 模块条挂着：先拼装目标（主槽位空就拦下）
  const goal = $("#goal").value.trim() || (frozen && frozen.raw_goal) || "";
  if (!goal) return flagGoalMissing();
  if (LIVE) { $("#hint").innerHTML = '<span style="color:var(--warn)">上一个任务还在跑，先停止或等它结束</span>'; return; }
  if (steps != null) MAXSTEPS = steps;
  /* 已开过头的会话：不带冻结契约的普通输入 = **接着说**（ZCode 式连续会话）。
     带冻结契约（预审路径）永远开新任务 —— 那是用户显式要"另起一份"的信号。 */
  if (CUR && CUR.followable && !frozen) return followUp(goal);

  LASTGOAL = goal;
  $("#goal").value = "";
  $("#empty").hidden = true; $("#thread").hidden = false;
  autoGrow();
  resetThread();
  /* ★ 用户说的话先落到气泡里再发请求。旧实现这里什么都不画，
     首轮只能靠顶部「目标」卡代替 —— 用户看不到"我刚说了什么"。 */
  beginUserTurn(goal);
  beginAssistantTurn({ live: true });
  setBusy(true);
  $("#hint").innerHTML = '<span class="spin"></span>正在启动…';
  T0 = Date.now(); RECONN = 0;
  AUTOOPENED = false;          // 新一次运行 ⇒ 自动展开的闩锁复位（见 autoOpenProc）

  let d;
  try {
    const body = { goal };
    if (frozen) body.contract = frozen;
    if (MAXSTEPS) body.max_steps = MAXSTEPS;
    // ★ 开关**每次显式带上**（开着 true、关着 false）：字段缺席在后端读成"关"，
    //   但让界面替内核猜一个默认值就是假功能的第一步 —— 要说就说死。
    body.kb_only = !!MENTOR;
    // ★ W36 模式开关：同理，**每次显式带上**。默认 "task"（= 今天的行为），
    //   只有用户亲手按了"仅问答"才变 "answer"。后端认不出的值一律回落到自动，
    //   所以拼错也不会静默变成一个假模式。
    body.mode = MODE;
    // ★ 每次显式带上 preset（空串 = baseline）。前端说了不算 —— 后端必须
    //   真的读、Run 必须真的存、_exec 必须真的传进 kernel.run（见 presets_live_test）。
    body.preset = PRESET;
    d = await api("/api/run", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body) });
  } catch (e) {
    setBusy(false);
    $("#goal").value = goal; autoGrow();
    /* 授权拦下 ⇒ 直接弹激活层，而不是在提示条上甩一句红字。
       `e.status === 403` 且后端标了 license 才算 —— 别的 403 不该弹这个。 */
    if (e && e.status === 403 && e.body && e.body.license) {
      NEED_LICENSE = true;
      showLicGate(e.message || "试用期已结束");
      return;
    }
    if (TURN) {
      TURN.title.textContent = "没能启动";
      TURN.head.dataset.st = "err";
    }
    note("这次没发出去：" + e.message, "err");
    $("#hint").innerHTML = '<span style="color:var(--err)">这次没发出去：' + esc(e.message) +
      '。原话已保留，可以修改后重试。</span>';
    $("#goal").focus();
    return;
  }
  attach(d.id, goal);
}
/* 会话内追问：**不清线程、不换工作区** —— 用户消息追加进现有对话气泡流，
   内核把上一轮的对话原样接回来接着跑。对比 send()：那边 resetThread() 清场重来。 */
async function followUp(text) {
  /* 先画这一轮的用户气泡，再发请求；上一轮的 DOM 一根手指都不动。 */
  beginUserTurn(text, { label: "追问" });
  beginAssistantTurn({ live: true });
  $("#goal").value = ""; autoGrow();
  setBusy(true);
  MATS = []; renderMats();
  $("#hint").innerHTML = '<span class="spin"></span>荣珂在想…';
  // 与 resetThread() 同一套计数清零 —— 只清这一轮的，历史轮原样留着。
  PENDING.clear(); aiRound = null;
  REJECTS = 0; STALLS = 0; STEPS = 0; T0 = Date.now(); RECONN = 0;
  PHASE = { text:"", sub:"" };
  STEP_CAP = null;
  LIVE = true; startTimer(); paintTop();
  phase("◌", "接着上一轮继续", "它带着这段会话的前情在回答");
  let d;
  try {
    d = await api("/api/follow", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ id: CUR.id, text, kb_only: !!MENTOR, mode: MODE, preset: PRESET }) });
  } catch (e) {
    LIVE = false; stopTimer();
    setBusy(false);
    /* 追问也是发起新任务：过期时后端回 403 + license，和 /api/run 同一套处理。 */
    if (e && e.status === 403 && e.body && e.body.license) {
      NEED_LICENSE = true;
      showLicGate(e.message || "试用期已结束");
      return;
    }
    if (TURN) {
      TURN.title.textContent = "这句没接上";
      TURN.head.dataset.st = "err";
    }
    $("#hint").innerHTML = '<span style="color:var(--err)">' + esc(e.message) + "</span>";
    note("这句没接上：" + e.message + " —— 上下文可能不在了，可以开个新对话", "err");
    return;
  }
  // 会话标题保持不变（CUR.goal 是这场会话最初的目标），只换流。
  CUR = { id: d.id, goal: CUR.goal, followable: true };
  $("#title").textContent = CUR.goal;
  openStream(d.id);
  loadSessions();
}
const THREAD_SHELL = '<div id="replayNote" class="replayNote" hidden></div>' +
                     '<div id="sayFlow"></div>';
function resetThread() {
  /* 外壳只剩一个轮次列表。目标条 / 进度条 / 统计条 / 过程抽屉 / 结果卡
     都不再是全局单例 —— 它们随 turn 建，一个用户输入一份。
     （以前全局单例的直接后果：追问一次就把上一轮的过程与成果覆盖掉。） */
  $("#thread").innerHTML = THREAD_SHELL;
  TURN = null; round = null;
  PENDING.clear(); aiRound = null;
  REJECTS = 0; STALLS = 0; STEPS = 0; T0 = T0 || Date.now();
  PHASE = { text:"", sub:"" };
  AUTOOPENED = false;
  FOLLOWING = true; paintToBottom();
}

/* ═════════════ 对话轮次：直播与回放共用的唯一构造器 ═════════════
   以前直播走 send()/followUp()、回放走 replay()，各画各的 ⇒ 直播首轮
   根本没有用户气泡（只有顶部「目标」卡）。现在两边都走这两个函数。 */
function ensureSayFlow() {
  let flow = $("#sayFlow");
  if (!flow) {
    $("#thread").innerHTML = THREAD_SHELL;
    flow = $("#sayFlow");
  }
  return flow;
}
function beginUserTurn(text, opts) {
  opts = opts || {};
  const flow = ensureSayFlow();
  const d = document.createElement("div");
  d.className = "turn me";
  d.dataset.turn = String(++TURN_SEQ);
  d.innerHTML = '<div class="bubble user-turn"' + (opts.history ? " data-history" : "") + ">" +
    (opts.label ? '<span class="fulab">' + esc(opts.label) + "</span>" : "") +
    esc(text == null ? "" : String(text)) + "</div>";
  flow.appendChild(d);
  if (LIVE) followBottom(true);
  return d;
}
/* id 只在**当前轮**上：`ui_shot` 靠 #resultCard / #askCard 找锚点，
   历史轮不该跟它抢。先挂新、再摘旧 —— 同一 tick 内不存在两个 id。 */
function markCurrent(t) {
  if (TURN && TURN !== t) TURN.current = false;
  TURN = t;
  t.current = true;
  [["resultCard", t.res], ["procWrap", t.proc], ["askCard", t.ask]].forEach(pair => {
    const id = pair[0], el = pair[1];
    el.id = id;
    document.querySelectorAll('[id="' + id + '"]').forEach(x => {
      if (x !== el) x.removeAttribute("id");
    });
  });
}
function beginAssistantTurn(opts) {
  opts = opts || {};
  const flow = ensureSayFlow();
  const open = PROCOPEN || transcriptView() === "verbose";
  const tpl = document.getElementById("tplTurn");
  const d = tpl.content.firstElementChild.cloneNode(true);
  d.dataset.turn = String(++TURN_SEQ);
  d.querySelector(".aproc").dataset.open = open ? "1" : "0";
  d.querySelector(".aproc-h").setAttribute("aria-expanded", open ? "true" : "false");
  // 模板里的 id 只作静态锚点；页面上由 markCurrent() 只挂给当前轮。
  d.querySelector("#askCard").removeAttribute("id");
  d.querySelector("#resultCard").removeAttribute("id");
  flow.appendChild(d);
  const t = {
    node: d,
    proc: d.querySelector(".aproc"),
    head: d.querySelector(".aproc-h"),
    title: d.querySelector(".aproc-h .t"),
    sub: d.querySelector(".aproc-h .sub"),
    mStep: d.querySelector(".m-step"),
    mTime: d.querySelector(".m-time"),
    mCtx: d.querySelector(".m-ctx"),
    body: d.querySelector(".aproc-b"),
    res: d.querySelector(".rescard"),
    ask: d.querySelector(".askcard"),
    live: !!opts.live,
    history: !!opts.history,
    current: !opts.history,
    started: Date.now()
  };
  t.head.onclick = () => {
    const nowOpen = t.proc.dataset.open !== "1";
    t.proc.dataset.open = nowOpen ? "1" : "0";
    t.head.setAttribute("aria-expanded", nowOpen ? "true" : "false");
    // 用户手动关过就别再自动弹开（与旧抽屉同一条纪律）
    if (!nowOpen) AUTOOPENED = true;
  };
  round = d.querySelector(".round");
  markCurrent(t);
  if (LIVE) followBottom(true);
  return t;
}
function endTurn(t) {
  sealThink();
  t = t || TURN;
  if (!t) return;
  const open = PROCOPEN || transcriptView() === "verbose";
  t.proc.dataset.open = open ? "1" : "0";
  t.head.setAttribute("aria-expanded", open ? "true" : "false");
}

function attach(id, goal) {
  if (!CUR || CUR.id !== id) {
    CUR = { id, goal: goal || id };
    $("#title").textContent = CUR.goal;
  }
  setBusy(true);
  // 材料已被服务端改名搬进这次任务的工作区 —— 前端这份清单要跟着清掉，
  // 否则它会一直挂在输入框下面，让人以为"还没交出去"（B 型：界面与事实各说各话）。
  MATS = []; renderMats();
  /* 重连路径（刷新页面时任务还在跑）没有经过 send()/followUp()，
     这里必须自己把 turn 外壳立起来 —— 否则 SSE 事件会 append 到 null，
     界面声称"进行中"却永远空白（旧事故见下）。
     位置在 LIVE = true 之前：外壳先立，流才有地方落。 */
  if (!$("#thread").innerHTML || !$("#sayFlow")) resetThread();
  if (!TURN) {
    beginUserTurn(goal || CUR.goal || id, { history: true });
    beginAssistantTurn({ live: true });
    phase("◌", "正在接上这一轮", "页面刷新过，界面从事件流的中途接回来");
  }
  AUTOOPENED = false;          // 新一次运行 ⇒ 自动展开的闩锁复位（见 autoOpenProc）
  $("#hint").innerHTML = '<span class="spin"></span>荣珂在工作…';
  LIVE = true; startTimer(); paintTop();
  if ($("#thread").hidden) { $("#empty").hidden = true; $("#thread").hidden = false; autoGrow(); }
  /* 「正在跑」的行要立刻出现在侧栏。不能只靠 loadSessions()：
     STATUS.active 是上一次轮询的旧值，此刻还不知道这次任务。
     这一行会被 done() → loadSessions() 换成带结果的归档行。 */
  if (!SESS.some(r => r.id === id))
    SESS.unshift({ id, goal: goal || id, live: true, success: null, ts_iso: "" });
  paintSessions();
  openStream(id);
}
function openStream(id) {
  if (ES) ES.close();
  ES = new EventSource("/api/stream?id=" + encodeURIComponent(id));
  ES.onmessage = m => { let e; try { e = JSON.parse(m.data); } catch (x) { return; } onEvent(e); };
  ES.onerror = async () => {
    if (ES) { ES.close(); ES = null; }
    if (!LIVE) return;
    // SSE 断线不等于任务失败 —— 先问服务端它还活着没，再决定重连还是收尾。
    RECONN++;
    if (RECONN > 5) { note("和界面断了，重连 5 次都没成 —— 任务没丢：侧栏会话列表里可以回放这次任务。", "err"); return done(); }
    $("#hint").innerHTML = '<span class="spin"></span>连接中断，正在重连（' + RECONN + '/5）…';
    await new Promise(r => setTimeout(r, 1200));
    try {
      const d = await api("/api/run?id=" + encodeURIComponent(id));
      if (d.done) { finish({ result: d.result, error: d.error }); return; }
      note("连接已恢复，继续跟踪。", "info");
      openStream(id);
    } catch (e) { done(); }
  };
}

async function stop() {
  if (!CUR) return;
  $("#btnStop").disabled = true;
  $("#hint").innerHTML = '<span class="spin"></span>正在停止…（会在下一步开头退出）';
  try { await api("/api/cancel", { method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ id: CUR.id }) }); } catch (e) {}
}
/* ═════════════ 收件箱：跑完的东西，和等你处理的活儿 ═════════════
   参考 dsh-coopanion「大肥鱼」的小信封（AGPL-3.0），**只借设计不搬代码**：
   那边是 TypeScript + DSH 的 Connection/投影，荣珂是单文件原生 JS。

   借来的三条纪律：
   ① **真相只来自真实结果**。条目只在 `finish(d)` 或 `blocker` 这类
      真事件里产生，不做"大概跑完了吧"的推测。这条是整套东西的地基：
      一个会误报"完成了"的收件箱，比没有收件箱更坏 —— 用户会开始不信它，
      然后所有通知一起失效。
   ② **已读 ≠ 成功**。`read` 只是呈现层的一个布尔，永远不改写 `tone`。
      点开一条失败的通知，它还是失败的。
   ③ **有界 + 去重**。同一 `runId + kind` 只进一次；最多留 60 条。
      长任务跑一晚上不该堆出一面墙。

   与它的差异（照荣珂的实情，不照抄它那 10 个类别）：荣珂没有原生
   approval/question 交互，它真正"停下来等人"的只有 `blocker` 和
   交付被打回；所以分类按 `loop.py` 里**真实存在的**终态来定。 */
const INBOX_KEY = "rongke-inbox";
const INBOX_MAX = 60;

/* 类别 → 语气 + 人话。tone 决定左侧色条与系统通知的紧急程度：
   ok = 做成了 / wait = 等你处理 / err = 出事了。 */
const INBOX_KIND = {
  done:    { tone: "ok",   label: "做完了",     t: "做完了" },
  partial: { tone: "wait", label: "没跑完",     t: "这次没跑完，可以接着跑" },
  blocked: { tone: "wait", label: "等你定夺",   t: "它停下来等你定夺" },
  gate:    { tone: "wait", label: "被打回",     t: "交付被打回过" },
  stall:   { tone: "wait", label: "原地打转",   t: "卡在同一个调用上" },
  error:   { tone: "err",  label: "出错了",     t: "这次执行出错了" },
  cancel:  { tone: "wait", label: "已取消",     t: "任务被取消了" },
};

/* 把一次真实运行结果翻译成「一件事」。
   ★ 判据顺序有讲究：先判 error，再判取消，再判 blocker —— 因为一个
     被取消的任务它的 `reason` 也可能是别的字，而 `cancelled` 标记是
     最确定的。顺序错了会把"取消"报成"失败"，让人以为出了事故。 */
function inboxClassify(res, err) {
  if (err) return "error";
  if (!res) return "error";
  if (res.cancelled) return "cancel";
  const why = String(res.reason || "");
  if (res.success) return "done";
  if (/阻塞/.test(why) || res.blocked) return "blocked";
  if (/打回/.test(why)) return "gate";
  if (/打转/.test(why)) return "stall";
  if (/取消/.test(why)) return "cancel";
  if (/步数耗尽/.test(why)) return "partial";
  return "partial";
}

function inboxLoad() {
  try {
    const a = JSON.parse(localStorage.getItem(INBOX_KEY) || "[]");
    return Array.isArray(a) ? a.filter(x => x && x.id) : [];
  } catch (e) { return []; }
}
function inboxSave(list) {
  try { localStorage.setItem(INBOX_KEY, JSON.stringify(list.slice(0, INBOX_MAX))); }
  catch (e) { /* 隐身模式写不进去：面板当场仍然可用，只是刷新后不留 */ }
}

/* 入队一条。`read` 初始为 0。
   ★ 去重键是 `kind + runId`，**不是**条目自己的 `id`。
     我第一版就写错了：id 里带了 `Date.now()`，于是同一件事每次入队
     都拿到不同的 id，`list.some(x => x.id === item.id)` 永远判不重 ——
     注释写着"同一 run 只进一次"，实现却在按时间戳判重，两句互相打脸。
     实测当场抓出来：灌 7 条再重复入 1 条，总数从 7 变成 8。
     ★ 一件真实的事会变的是"时间"，不会变的是"哪次运行 + 哪一类结果"，
       所以键必须取后者。 */
function inboxDedupKey(item) {
  return item.kind + "|" + (item.runId || ("err:" + (item.body || "").slice(0, 40)));
}
function inboxPush(item) {
  const list = inboxLoad();
  const k = inboxDedupKey(item);
  if (list.some(x => inboxDedupKey(x) === k)) return false;
  list.unshift(item);
  inboxSave(list);
  paintInbox();
  return true;
}

/* 重画顶栏那个小信封的未读数，并决定它出不出现。
   ★ 0 条 ⇒ 整个按钮 `hidden`：0 件事还挂个铃铛，是在制造"有东西等你"的错觉。 */
function paintInbox() {
  const list = inboxLoad();
  const unread = list.filter(x => !x.read).length;
  const btn = $("#btnInbox"), n = $("#inboxN");
  if (!btn) return;
  btn.hidden = list.length === 0;
  if (n) {
    n.hidden = unread === 0;
    n.textContent = unread > 9 ? "9+" : String(unread);
  }
  if ($("#inbox") && $("#inbox").dataset.open === "1") renderInbox();
}

function fmtWhen(ts) {
  const d = new Date(ts);
  if (isNaN(d)) return "";
  const p = x => String(x).padStart(2, "0");
  const now = new Date();
  const same = d.toDateString() === now.toDateString();
  return (same ? "" : (d.getMonth() + 1) + "/" + d.getDate() + " ") +
         p(d.getHours()) + ":" + p(d.getMinutes());
}

function renderInbox() {
  const host = $("#inboxList"), sub = $("#inboxSub");
  if (!host) return;
  const list = inboxLoad();
  const pending = list.filter(x => INBOX_KIND[x.kind] && INBOX_KIND[x.kind].tone !== "ok").length;
  const unread = list.filter(x => !x.read).length;
  if (sub) {
    sub.textContent = list.length
      ? (pending + " 件待处理 · " + unread + " 份未读回复")
      : "回复与待办";
  }
  if (!list.length) {
    host.innerHTML = '<div class="empty2">都处理好啦，安心做手边的事。</div>';
    return;
  }
  host.innerHTML = "";
  list.forEach(it => {
    const K = INBOX_KIND[it.kind] || INBOX_KIND.done;
    const el = document.createElement("div");
    el.className = "it";
    el.dataset.tone = K.tone;
    el.dataset.read = it.read ? "1" : "0";
    el.dataset.id = it.id;
    if (it.open) el.dataset.open = "1";
    el.tabIndex = 0;
    el.innerHTML =
      '<div class="k"><span class="dot"></span><span>' + esc(K.label) + "</span>" +
      (it.ts ? '<span style="margin-left:auto">' + esc(fmtWhen(it.ts)) + "</span>" : "") + "</div>" +
      '<div class="ti">' + esc(it.title || K.t) + "</div>" +
      (it.body ? '<div class="bd">' + esc(it.body) + "</div>" : "") +
      '<div class="ft"><span class="s">' + esc(it.goal || "") + "</span>" +
      (it.runId ? '<button class="go" type="button">复看这次 ↗</button>' : "") + "</div>";
    /* 点条目 = 标已读 + 展开正文（纪律②：只动 read，不动 tone）
       ★ 这里踩过两次，都记下来：
         ① `markRead()` 内部会 `renderInbox()` 重建整个列表，于是刚写上去的
            `data-open` 立刻被新 DOM 丢掉 —— 表现是"点了没反应，只有未读数变了"。
         ② 第二版改成 `it.open = !it.open` 还是错：`it` 是每次 `renderInbox()`
            从 localStorage 重建出来的**新对象**，`open` 不在存储里，
            所以每点一次都是「undefined → true」，永远收不回去。
       正确做法：展开状态**跟着条目一起存**（与 read 同一层语义），
       重画时据此恢复；判定当前状态读的是存储里的值，不是闭包里的旧对象。 */
    const markRead = () => {
      if (it.read) return false;
      const l = inboxLoad(), t = l.find(x => x.id === it.id);
      if (t) { t.read = 1; inboxSave(l); }
      it.read = 1;
      paintInbox();
      return true;
    };
    const toggleOpen = () => {
      const l = inboxLoad(), t = l.find(x => x.id === it.id);
      const next = !(t && t.open);
      if (t) { t.open = next ? 1 : 0; inboxSave(l); }
      it.open = next ? 1 : 0;
      return next;
    };
    el.addEventListener("click", ev => {
      if (ev.target.closest(".go")) return;
      const rebuilt = markRead();
      const next = toggleOpen();
      const node = rebuilt
        ? host.querySelector('.it[data-id="' + it.id + '"]') || el
        : el;
      node.dataset.open = next ? "1" : "0";
    });
    el.addEventListener("keydown", ev => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); el.click(); }
    });
    const go = el.querySelector(".go");
    if (go) go.addEventListener("click", ev => {
      ev.stopPropagation();
      markRead();
      closeInbox();
      /* 复看：走既有的会话行回放路径（与侧栏点历史同一条路），
         不自己另造一套加载逻辑 —— 那会造出第二处「怎么打开一次旧任务」的真相。 */
      const row = document.querySelector('#sessList .sess[data-id="' + it.runId + '"]');
      if (row) { row.click(); return; }
      if (typeof openSession === "function") { openSession(it.runId); return; }
      note("这次任务不在这台机器的会话列表里，可能是别的机器跑完同步过来的", "warn");
    });
    host.appendChild(el);
  });
}

function openInbox() {
  const p = $("#inbox");
  if (!p) return;
  p.dataset.open = "1";
  p.setAttribute("aria-hidden", "false");
  renderInbox();
}
function closeInbox() {
  const p = $("#inbox");
  if (!p) return;
  p.dataset.open = "0";
  p.setAttribute("aria-hidden", "true");
}

/* 系统通知：默认**不发**，先问过用户再说。
   ★ 三条克制：
     ① 权限只在用户主动点开收件箱之后才请求 —— 一进页面就弹权限框
        是最招人烦的做法，而且被拒一次就很难再要回来。
     ② 页面在前台且可见时不发系统通知（用户就在看，重复打扰）。
     ③ 只对 `err` / `wait` 这类"要你处理"的发，`done` 发系统通知
        属于噪音 —— 收件箱里留着就够了。 */
async function notifySystem(item) {
  try {
    if (typeof Notification === "undefined") return;
    if (Notification.permission !== "granted") return;
    if (document.visibilityState === "visible" && document.hasFocus()) return;
    const K = INBOX_KIND[item.kind] || INBOX_KIND.done;
    if (K.tone === "ok") return;
    const n = new Notification("荣珂 · " + K.t, {
      body: (item.title || "") + (item.goal ? "\n" + item.goal : ""),
      tag: item.id,          // 同一个 id 只留一条，与它的 tid 去重同一个道理
      silent: false,
    });
    n.onclick = () => { try { window.focus(); openInbox(); } catch (e) {} };
  } catch (e) { /* 通知失败不影响主流程 */ }
}

/* 从一次真实运行结果建条目。**唯一的调用点**在 finish() 里 —— 
   放两处就会造出两条通知（一条来自 finish、一条来自别处），
   而用户看到的将是同一件事被说两遍。 */
function inboxRecord(res, err) {
  const kind = inboxClassify(res, err);
  const K = INBOX_KIND[kind] || INBOX_KIND.done;
  const runId = (res && res.run_id) || (CUR && CUR.id) || "";
  if (!runId && !err) return;          // 连 id 都没有就不入队，免得攒一堆点不开的空条
  const goal = (CUR && CUR.goal) || (res && res.goal) || "";
  const body = err ? String(err).slice(0, 300)
    : (res && (res.final || res.draft || res.reason)) || "";
  const item = {
    id: kind + "|" + runId + "|" + Date.now(),
    kind: kind,
    runId: runId,
    goal: String(goal).slice(0, 120),
    title: K.t,
    body: String(body || "").replace(/\s+/g, " ").slice(0, 300),
    ts: Date.now(),
    read: 0,
  };
  if (inboxPush(item)) notifySystem(item);
}

/* 输入区只有一个「主座」：空闲是发送箭头，运行中是停止方块（DSH 同座切换）。
   旧实现两个按钮并排，跑到一半时一颗禁用箭头 + 一颗「停止」挤在一起。 */
function setBusy(on) {
  $("#btnSend").hidden = !!on;
  $("#btnStop").hidden = !on;
  $("#btnPreview").disabled = !!on;
  $("#mentorSwitch").setAttribute("aria-disabled", on ? "true" : "false");
  if (window.RongkeMentor) RongkeMentor.paint();
  if (!on) $("#btnStop").disabled = false;
}

function done() {
  LIVE = false; stopTimer();
  setBusy(false);
  // 跑完的会话就在原地：输入框不打烊，下一句话就是「接着说」。
  // 后端快照在不在由 /api/follow 如实把关 —— 前端乐观放行，不让用户多想一层。
  if (CUR) CUR.followable = true;
  renderMats();                 // 任务结束 ⇒ 又能递材料了（`＋ 材料` 按钮跟着解禁）
  if ($("#hint").querySelector(".spin")) $("#hint").textContent = "";
  loadStatus();
  loadSessions();               // 侧栏那一行从"进行中"换成结果
  // 进度条让位给结果卡：过程组已由 finish() 收起，这里只兜住"没跑完就断了"的情况。
  if (TURN && /^正在/.test(TURN.title.textContent || "")) {
    TURN.title.textContent = "已完成";
  }
  // 任务结束后就不存在"在等你"这回事了 —— 清掉悬空的介入卡。
  // （正常路径下它早已被 applied/note 换掉，这里防的是"卡还挂着但内核已走"的错位。）
  if (ASK) hideAsk();
}

/* ═════════════ 进度：一句人话 ═════════════
   原来"每一步都往屏幕上堆思考/工具/正文"，用户得自己从里面读出"它到底在干嘛"。
   现在这一步由内核事件**翻译成一句人话**，每次原地更新。 */
let PHASE = { text:"", sub:"" };
/* ★ W18 · 副行可以带一个"动作"。
   原先 `llm_call` 那句「思考与工具调用在下面的「过程」里」是一句**死话**：
   它指着一个默认收起的抽屉，自己却不可点 —— 用户读到的是一条指路牌，
   路上却没有门。现在这句在抽屉**关着**时渲染成按钮，点一下就展开并滚过去。
   `sub` 全部走 esc()，只有这个按钮是按固定模板拼的（不含任何事件载荷）——
   不把 innerHTML 开成"能塞任意 HTML"的口子。 */
/* 记住上一次 phase() 收到的参数。
   用途只有一个：副行按钮被点开抽屉之后，要把这一行**原样重渲染一遍**
   （那时 procIsOpen() 已是真，它会自然退回纯文字，按钮消失）。
   不记这个就得让调用方再发一次 phase()，而调用方在模型那一侧、并不知道
   用户刚点了按钮。 */
function phase(icon, text, sub, st) {
  PHASE = { text: text || "", sub: sub || "", st: st || "" };
  const t = TURN;
  if (!t) return;
  /* 过程组头就是旧的「进度条」。它本身可点，不再需要「展开 ›」那颗假按钮
     （旧实现要记住上一次参数重渲染，就是为了消掉它）。 */
  if (text) t.title.textContent = text;
  t.sub.textContent = sub || "";
  if (st) t.head.dataset.st = st; else delete t.head.dataset.st;
}
function tickProg() {
  if (!LIVE) return;
  autoOpenProc();
  const t = TURN;
  if (t && T0) {
    t.mTime.hidden = false;
    t.mTime.textContent = fmtClock((Date.now() - T0) / 1000);
  }
}

/* ★ W18 · 等超过 5 秒就把过程组自动开出来。
   实测：一次真任务里最长有 14.3 秒屏幕上没有任何新信息，
   而过程里此刻已经攒了上万字符 —— 用户却要自己想起来去点「▶」。
   三条纪律：① 只在本次运行内生效，不写回设置；② 只自动开一次；
   ③ 用户自己开过了就什么都不做。 */
const AUTOOPEN_AFTER_MS = 5000;
let AUTOOPENED = false;
function autoOpenProc() {
  if (AUTOOPENED || PROCOPEN || transcriptView() !== "verbose") return;
  const t = TURN;
  if (!t) return;
  if (t.proc.dataset.open === "1") { AUTOOPENED = true; return; }
  if (!T0 || Date.now() - T0 < AUTOOPEN_AFTER_MS) return;
  t.proc.dataset.open = "1";
  t.head.setAttribute("aria-expanded", "true");
  AUTOOPENED = true;
}
let STEPS = 0;
let STEP_CAP = null;      // 步数上限：内核报的数优先（wrapup_hint / extend 生效后的 max_steps）

/* ═════════════ W21-B · 过程组头的三位读数 ═════════════
   数据全部来自内核已有事件：contract.to_dict()、step.n、wrapup_hint.max_steps、
   budget_note.share、T0。界面一个新数都不自造。
   ★ 每一位仍是**独立节点**，phase() 覆写不到它们（旧实现吃过的亏）。 */
function fmtClock(s) {
  const n = Math.max(0, Math.floor(Number(s) || 0));
  return String(Math.floor(n / 60)).padStart(2, "0") + ":" + String(n % 60).padStart(2, "0");
}
function stepCap() { return STEP_CAP || MAXSTEPS || (STATUS.loop && STATUS.loop.max_steps) || 40; }
function paintSteps() {
  const t = TURN; if (!t) return;
  t.mStep.hidden = false;
  t.mStep.textContent = STEPS + "/" + stepCap() + " 步";
}
function paintCtx(share) {
  /* 宁可不显示，不显示假的：口径降级（拿不到 input_tokens）时 share 不在，
     这一位整块不出 —— 精确口径没到手之前，不承诺一个圆整过的百分比。 */
  const t = TURN; if (!t || share == null) return;
  t.mCtx.hidden = false;
  t.mCtx.textContent = "上下文 " + Math.round(share * 100) + "%";
}
function renderGoalBar(c) {
  const t = TURN; if (!t) return;
  /* 目标原文已经在用户气泡里了；这张条只放「契约里那些气泡没有的口径」。
     旧实现把 goal 再抄一遍 ⇒ 回放里同一段话出现两次（目标卡 + 气泡）。 */
  const terms = [];
  if (c.target_chars) terms.push(["目标字数", "约 " + c.target_chars + " 字"]);
  if (c.deliverables && c.deliverables.length) terms.push(["必须交付", c.deliverables.join("；")]);
  if (c.acceptance && c.acceptance.length) terms.push(["验收标准", c.acceptance.join("；")]);
  if (c.constraints && c.constraints.length) terms.push(["约束", c.constraints.join("；")]);
  if (c.forbidden && c.forbidden.length) terms.push(["禁止", c.forbidden.join("；")]);
  let bar = t.node.querySelector(".gbar");
  if (!terms.length) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "gbar";
    t.node.insertBefore(bar, t.proc);
  }
  bar.innerHTML = "";
  bar.dataset.open = "0";
  const ic = document.createElement("span"); ic.className = "ic"; ic.textContent = "◎";
  const cap = document.createElement("span"); cap.className = "cap"; cap.textContent = "任务单";
  const tx = document.createElement("div"); tx.className = "gtxt";
  tx.textContent = terms.length + " 条口径 · 点开核对";
  const chev = document.createElement("span"); chev.className = "gchev"; chev.textContent = "▶";
  const full = document.createElement("div");
  full.className = "gfull"; full.hidden = true;
  full.textContent = terms.map(p => p[0] + "：" + p[1]).join("\n");   // textContent ⇒ 无注入面
  bar.appendChild(ic); bar.appendChild(cap); bar.appendChild(tx);
  bar.appendChild(chev); bar.appendChild(full);
  bar.onclick = () => {
    const open = bar.dataset.open === "1";
    bar.dataset.open = open ? "0" : "1";
    full.hidden = open;
  };
  bar.hidden = false;
}

/* ═════════════ 过程层（默认折叠）═════════════ */
/* 兼容旧调用：用户块一律走 beginUserTurn（见 resetThread 下方）。
   history = 回放里的"曾经提交过"，比当前轮淡一档。 */
function addUser(text, history) {
  return beginUserTurn(text, { history: !!history });
}
function addUserInFlow(text, history) {
  return beginUserTurn(text, { history: !!history, label: "追问" });
}
let aiRound = null;           // 注意：`round` 已在顶部声明过，这里**不能**再声明一次
                              // （同一个 script 里 `let round` 两次 = SyntaxError，整页白屏）
/* 过程组里的一段。直播按事件顺序往里追加；回放也是同一条路。 */
function newRound() {
  const t = TURN;
  const host = t ? t.body : null;
  round = document.createElement("div");
  round.className = "round";
  if (host) host.appendChild(round);
  return round;
}
const cur = () => round || newRound();

/* 思考链：照 DSH `ReasoningRow` 的行为逐条搬。
   dsh_ref/.../client.js 原实现：
     summaryText = running ? latestCompletedParagraphFirstLine(text) : firstLine(text)
     summary = summaryText.replaceAll("**", "")
     preview   = !expanded && summary !== "" && (running || settledReasoningPreview)
   其中 settledReasoningPreview 在 compact=false、standard/detailed/verbose=true。
   这里用荣珂已有的 transcriptView() 当那面策略旗。 */
function firstLine(text) {
  const i = text.indexOf("\n");
  return i === -1 ? text : text.slice(0, i);
}
function latestCompletedParagraphFirstLine(text) {
  let summary = "";
  let paragraphStart = 0;
  const separator = /\r?\n(?:[\t ]*\r?\n)+/g;
  while (true) {
    const nextParagraph = separator.exec(text);
    const paragraphEnd = nextParagraph === null
      ? text.length : nextParagraph.index + nextParagraph[0].indexOf("\n");
    const newline = text.indexOf("\n", paragraphStart);
    if (newline !== -1 && newline <= paragraphEnd) {
      const candidate = text.slice(paragraphStart, newline).trim();
      if (candidate !== "") summary = candidate;
    }
    if (nextParagraph === null) return summary;
    paragraphStart = nextParagraph.index + nextParagraph[0].length;
  }
}
function thinkSummary(text, live) {
  const src = String(text == null ? "" : text);
  const clean = s => s.replace(/^[#\s>-]+/, "").replace(/^\d+[.)]\s+/, "")
    .replace(/[*_`~]/g, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").trim();
  const paragraphs = src.split(/\r?\n\s*\r?\n/).filter(p =>
    !/^[>\s`]*[a-z][a-z0-9]*_[a-z0-9_]+[`\s]*$/i.test(firstLine(p).trim()))
    .map(p => clean(firstLine(p))).filter(Boolean);
  const meaningful = s => /[\p{L}\p{N}]/u.test(s);
  const raw = live ? (paragraphs.length > 1 ? paragraphs[paragraphs.length - 2] : "")
                   : (paragraphs.find(meaningful) || "");
  return raw.length > 150 ? raw.slice(0, 149).trimEnd() + "…" : raw;
}
function paintThink(box, text, live, meta) {
  if (meta && typeof meta === "object") box._meta = meta;
  box._text = String(text == null ? "" : text);
  box.dataset.live = live ? "1" : "0";
  box.dataset.state = live ? "running" : "ok";
  const state = box.querySelector(".thinkstate");
  if (state) state.textContent = live ? "推理中" : "已结束";
  const cut = box.querySelector(".thinkcut");
  if (cut) cut.hidden = !(box._meta && box._meta.truncated);
  const open = box.dataset.open === "1";
  const sum = thinkSummary(box._text, live);
  /* DSH：settledReasoningPreview 只在 compact 下为 false。 */
  const preview = !open && sum !== "" && (live || transcriptView() !== "compact");
  /* DSH 用 `data-preview={preview || void 0}` —— 假值时不写属性。
     我们照做：CSS 的 `:not([data-preview])` 只认「没有这个属性」，
     写成 data-preview="0" 会让摘要继续显示。 */
  if (preview) box.dataset.preview = "1"; else delete box.dataset.preview;
  const h = box.querySelector(".row");
  if (h) h.setAttribute("aria-busy", live ? "true" : "false");
  const tsum = box.querySelector(".tsum"), tsumt = box.querySelector(".tsumt");
  tsumt.textContent = sum;
  if (live) tsum.dataset.streaming = "1"; else delete tsum.dataset.streaming;
  if (open) box.querySelector(".thinkb").innerHTML = md(box._text);
}
function addThink(text, live, meta) {
  /* 思考行进**过程组**。以前它挂在 #sayFlow 里，和正文平铺 ——
     截图里那一片「思考 · The library is empty…」就是这么来的。 */
  const host = cur();
  let box = host.querySelector(".think:last-of-type");
  if (!box || box.dataset.done === "1") {
    box = document.createElement("div");
    box.className = "think disclosure"; box.dataset.open = "0"; box.dataset.done = "0";
    box.innerHTML =
      '<div class="row" data-expandable role="button" tabindex="0" aria-expanded="false">' +
        '<span class="leading"><span class="iconIdle">' +
          '<svg viewBox="0 0 16 16" aria-hidden="true" stroke-width="1">' +
            '<path d="M10.2854 5.71481C12.9673 8.39663 14.1182 11.5938 12.8562 12.8559C11.5942 14.1179 8.39706 12.9669 5.71518 10.2851C3.03333 7.60323 1.88236 4.40608 3.14441 3.14403C4.40644 1.882 7.6036 3.03297 10.2854 5.71481Z"/>' +
            '<path d="M10.2854 10.2851C7.6036 12.9669 4.40644 14.1179 3.14441 12.8559C1.88236 11.5938 3.03333 8.39663 5.71518 5.71481C8.39706 3.03297 11.5942 1.882 12.8562 3.14403C14.1182 4.40608 12.9673 7.60323 10.2854 10.2851Z"/>' +
            '<path d="M8.86291 8.0002C8.86291 8.47549 8.47762 8.86087 8.00224 8.86087C7.52694 8.86087 7.1416 8.47549 7.1416 8.0002C7.1416 7.52485 7.52694 7.13953 8.00224 7.13953C8.47762 7.13953 8.86291 7.52485 8.86291 8.0002Z" fill="currentColor" stroke="none"/>' +
          '</svg></span><span class="chevronHover">›</span></span>' +
        '<span class="title">推理摘要</span><span class="thinkstate"></span><span class="tsep"></span>' +
        '<span class="tsum"><span class="tsumt"></span></span></div>' +
      '<div class="thinknote">模型推理，不等同于查证结果</div><div class="thinkcut" hidden>此处仅保留部分推理</div><div class="thinkb"></div>';
    const h = box.querySelector(".row");
    const toggle = () => {
      const open = box.dataset.open !== "1";
      box.dataset.open = open ? "1" : "0";
      h.setAttribute("aria-expanded", open ? "true" : "false");
      paintThink(box, box._text || "", box.dataset.live === "1");
    };
    h.onclick = toggle;
    h.onkeydown = e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    };
    host.appendChild(box);
  }
  paintThink(box, text, !!live, meta);
  return box;
}
function sealThink() {
  document.querySelectorAll('.think[data-live="1"]').forEach(b => {
    b.dataset.done = "1";
    paintThink(b, b._text || "", false);
  });
}

const GLYPH = [
  [/ledger|台账/i, "☰"], [/doi|verify|核验|verify_/i, "✓"], [/search|检索|find|query/i, "⌕"],
  [/read|get_|fetch|摘要|abstract/i, "▤"], [/write|edit|export|docx|save/i, "✎"],
  [/gen|build|make|生成/i, "⚙"], [/list|glob|enumerate/i, "≡"], [/skill|技能/i, "❖"],
];
function glyph(name) { for (const [re, g] of GLYPH) if (re.test(name)) return g; return "•"; }

/* ★ W18 · 这张表要覆盖内核**真正注册的每一个**工具名。
   对照 `ui_shots/firstuse/probes/_toolhuman_coverage.py` 实测：
   改前 56 个工具里 **32 个没被覆盖** ⇒ 用户在主视图上看到的是
   `deck_from_template`、`kb_search`、`set_slide_notes` 这种裸标识符。
   判据不手抄 —— 从 `/api/tools`（内核自己报的注册表）机械导出后再比。
   顺带修掉一个**过时名字**：表里写着 `export_document`，
   而内核现在注册的是 `export_docx` —— 老名字留在表里等于那条永远命中不了。 */
const TOOLHUMAN = {
  ledger_query: "查过程记录", load_skill: "装载技能", read_skill: "读技能", search_skills: "找技能",
  writing_brief: "取写作要求", originality: "查重复/原创性", quality_check: "质量自检",
  literature_search: "检索文献", build_references: "整理并核验参考文献",
  verify_citation: "核验引文", fetch_doi: "抓取 DOI 元数据", web_search: "联网检索",
  web_extract: "抓取网页正文", import_document: "读入稿件", export_docx: "导出稿件",
  mechanical_fix: "机械性修订", record_skill_use: "登记技能使用",
  origin_plot_xy: "用 Origin 画图", origin_plot_spec: "用 Origin 画图", origin_status: "查 Origin 状态",
  figure_audit_extract: "抽取文献插图", figure_reuse_check: "查图像复用",
  audit_figure: "图面体检", deliver_task: "交付", report_blocker: "报告阻塞",
  // ── 下面这批是原来漏掉的（实测 32 个）──
  // 文献与知识库
  kb_search: "检索知识库", kb_read: "读知识库条目", kb_add: "存进知识库", kb_ingest: "收进知识库",
  kb_list: "列知识库", kb_note: "记一条知识库笔记", kb_ask: "问知识库", kb_compare: "比对知识库条目",
  kb_rank: "给知识库条目排序", kb_review: "回顾知识库", kb_bibtex: "导出 BibTeX",
  kb_to_obsidian: "同步到 Obsidian",
  // ★ 2026-11-01：这三个漏了中文名，主视图会直接显示裸英文标识符
  //   （ui_check 的 W18 一直在报，共 3/61）。补上。
  kb_coverage: "看知识库覆盖情况", literature_fetch: "抓取文献全文",
  plot_data: "画数据图", plot_svg: "画原生 SVG 预览图",
  // 幻灯片
  compose_deck: "编排幻灯片", deck_from_template: "按模板做幻灯片",
  deck_template_plan: "定幻灯片模板方案", deck_reference: "取幻灯片样式参考",
  style_deck: "套用幻灯片风格", set_slide_notes: "写讲稿备注", audit_deck: "幻灯片体检",
  // 稿件与文献格式
  export_docx: "导出稿件", convert_pdf: "转换 PDF", read_file: "读文件",
  figure_render: "渲染图件",
  // 流程与协作
  dispatch_task: "派活给执行体", list_executors: "列出执行体", subagent: "开一个子任务",
  process_audit: "审一遍过程", raise_blocker: "提出阻塞",
  resolve_blocker: "解除阻塞", topic_status: "查课题状态", topic_use: "切到某课题",
  topic_write: "写进课题",
  /* ★ 2026-11-02：课题工具从 5 个长到 9 个，界面中文名一处补齐 ——
     这一表是**用户看到的唯一名称来源**，漏一个就显示裸 `lab_record`。
     漏的四个都不是"顺手加的"：topic_link 补的是「把文献挂到课题上」那只
     根本不存在的手；topic_update 补的是「在对话里改课题」；
     lab_record / lab_report 补的是「老师说我没数据」——
     把定性观察与定量测量分成两件事，缺口才算得出来。 */
  topic_link: "关联文献到课题", topic_update: "改课题信息",
  lab_record: "记实验台账", lab_report: "出汇报页", lab_method: "出方法卡",
  /* ★ 2026-10-22 补：这 16 个工具**真实调用过却没有中文名**（机械核对过
     runs 目录下所有 trace.jsonl，见 _x_miss.py）。最严重的两个：
       chat       1145 次 —— 全站最高频，界面上一直显示裸 "chat"
       write_file  206 次 —— 第 6 高频，用户最常见的就是它写出来的稿子
     幻灯片那 9 个是低频（1~13 次），但一旦走到做 PPT 的分支就全现原形。 */
  write_file: "写入文件",
  kb_index: "重建知识库索引",
  /* ★ 2026-11-01：这一批 PPT 工具名已全部作废 —— 那套 PPT MCP 换成现在的
     deck_* 七件套（见上面「幻灯片」那组）。留着它们只有一个后果：
     `TOOLHUMAN` 里永远命中不了的死条目，而 ui_check 的 W18 每次都在数它们。
     用户能看到的差别是「一个再也不会出现的中文名」，等于没有。
     删掉不是省事，是把"这张表描述的是当前内核"这件事恢复成真的。 */
};
/* 预设能力注册的工具**只在本次 run 存在**，不进 TOOLHUMAN：ui_check W18 的
   分母是开机常驻注册表 /api/tools，写进主表会被判成「内核没注册的旧名」。
   分开放，既不骗判据，也不让主视图显示裸英文标识符。 */
const PRESET_TOOLHUMAN = { writing_metrics: "量稿件字数与引注" };
function humanTool(name) { return TOOLHUMAN[name] || PRESET_TOOLHUMAN[name] || name; }

/* ═══════════════════════════════════════════════════════════════════════════
   DSH 过程组（StepProcess）—— 整段照搬，不是仿写
   ═══════════════════════════════════════════════════════════════════════════
   用户的原话：「完完全全一样，明白吗，先全照搬过去，在细改」。
   所以下面每一处都能指回 `dsh-client-ui-chat/lib/client.js` 的具体行：

     ① `activity()`          @ conversation-nodes/process-activity.js
     ② `processTitle()`      @ chat/step-process.js
     ③ `PROCESS_ICONS`       @ chat/ChatGroupSeat.js
     ④ `ProcessGroupHeader`  @ chat/ChatGroupSeat.js  （jsx 结构）
     ⑤ 128 条中文文案         @ client.js 内联的 zh 语言包

   ★ 这是荣珂缺的那一层。以前荣珂是「每调用一次工具 = 一行」，
     DSH 是「一段工作 = 一行合并标题」。用户截图里的
     「已读取图片并执行了命令」就是这么拼出来的 ——
     它背后可能是 5 次工具调用。 */

/* ① 工具名 → kind。前半段逐条照 DSH 的 activity() 抄，顺序也照抄，
      注意三条前缀规则（endsWith "_inspect"、startsWith "terminal_"、
      startsWith "subagent_"），漏掉它们会少分好几类。
      后半段是**荣珂自己的 56 个工具名**（实测从 runs 目录下的 trace.jsonl 统计出来，
      不是猜的）—— 它们一个都不在 DSH 的表里，不补就全落进 tools，
      界面上会出现「已调用工具」连着好几行，等于没分类。
      归类沿用 activity() 的**语义**，不是按名字像不像：
        kb_* 查库里已有的东西        → search（已搜索代码）
        kb_read / read_file 取内容    → read（已读取文件）
        kb_add / kb_bibtex 往上写     → write（已写入文件）
        figure_render / fig 类出图    → write（产出文件，与 DSH 的 write 同义）
        export_* / *_deck / slide 类  → write（也是产出文件）
        verify_* / audit_* 核对       → search（本质是查证）
        web_extract                   → webFetch（抓网页正文）
        invoke_agent / dispatch_task  → subagents（协调子智能体）
        ledger_query / *_status       → plan（看进度） */
function stepKind(name) {
  /* —— 以下到 `return "tools"` 之前，逐条照 DSH `activity()` —— */
  if (name === "read") return "read";
  if (name === "read_image") return "readImage";
  if (name === "grep" || name === "glob" || name.endsWith("_inspect")) return "search";
  if (name === "write") return "write";
  if (name === "edit" || name === "apply_patch") return "edit";
  if (["bash", "pwsh", "exec_command", "write_stdin"].indexOf(name) >= 0 ||
      name.startsWith("terminal_")) return "commands";
  if (name === "run_code") return "code";
  if (name === "web_search") return "webSearch";
  if (name === "web_fetch") return "webFetch";
  if (name === "subagent" || name.startsWith("subagent_")) return "subagents";
  if (["todo_write", "create_goal", "update_goal", "get_goal"].indexOf(name) >= 0) return "plan";
  if (name === "ask_user_question" || name === "request_user_input") return "questions";
  /* —— 以下是荣珂自有工具，按上面注释里的语义归 —— */
  if (name === "read_file" || name === "kb_read" || name === "read_skill" ||
      name === "get_slide_info" || name === "get_presentation_info" ||
      name === "writing_brief") return "read";
  if (name === "kb_search" || name === "kb_list" || name === "kb_rank" ||
      name === "kb_index" || name === "kb_ask" || name === "kb_review" ||
      name === "search_skills" || name === "literature_search" ||
      name === "fetch_doi" || name === "verify_citation" ||
      name === "audit_figure" || name === "audit_deck" ||
      name === "figure_reuse_check" || name === "originality" ||
      name === "quality_check" || name === "writing_metrics") return "search";
  if (name === "write_file" || name === "kb_add" || name === "kb_bibtex" ||
      name === "topic_write" || name === "mechanical_fix" ||
      name === "export_docx" || name === "export_document" ||
      name === "import_document" || name === "build_references" ||
      name === "figure_render" || name === "origin_plot_xy" ||
      name === "origin_plot_spec" || name === "deck_from_template" ||
      name === "set_slide_notes" || name === "deck_template_plan") return "write";
  if (name === "web_extract") return "webFetch";
  if (name === "invoke_agent" || name === "dispatch_task" ||
      name === "list_executors") return "subagents";
  if (name === "ledger_query" || name === "topic_status" ||
      name === "load_skill" || name === "record_skill_use" ||
      name === "origin_status" || name === "list_slide_templates" ||
      name === "switch_presentation") return "plan";
  return "tools";
}

/* ⑤ 文案。全部是 client.js 里 zh 语言包的原文，一个字没改。
     右列是 DSH 的 key，方便日后回查。 */
const SP_TXT = {
  done: {
    thinking: "已完成分析",        // message.stepProcess.done.thinking
    read: "已读取文件",            // message.stepProcess.done.read
    readImage: "已读取图片",       // message.stepProcess.done.readImage
    search: "已搜索代码",          // message.stepProcess.done.search
    edit: "修改了文件",            // message.stepProcess.done.edit
    write: "已写入文件",           // message.stepProcess.done.write
    commands: "执行了命令",        // message.stepProcess.done.commands
    code: "运行了代码",            // message.stepProcess.done.code
    webSearch: "已搜索网页",       // message.stepProcess.done.webSearch
    webFetch: "已访问网页",        // message.stepProcess.done.webFetch
    subagents: "已协调子智能体",   // message.stepProcess.done.subagents
    plan: "更新了计划",            // message.stepProcess.done.plan
    questions: "向用户提出了问题", // message.stepProcess.done.questions
    tools: "已调用工具",           // message.stepProcess.done.tools
  },
  sharedPrefix: "已",              // message.stepProcess.sharedPrefix
  comma: "，",                     // message.stepProcess.comma —— 全角逗号
  joinTwo: "{first}并{second}",    // message.stepProcess.joinTwo
  more: "{title}等",               // message.stepProcess.more
};

/* ② 合并标题。照 processTitle() 的三个分支逐行抄：
      0 个类别 → "已完成分析"
      1 个     → 原文
      2 个     → 若两者都以「已」开头，把第二个的「已」去掉，再拼成「A并B」
      3 个及以上 → 全角逗号连接；超过 3 个再补「等」
     ★ 那个「去前缀」是中文特有的 —— 写成「已读取图片并已执行了命令」就废了。 */
function stepTitle(counts) {
  const labels = counts.slice(0, 3).map(c => SP_TXT.done[c.kind] || SP_TXT.done.tools);
  if (!labels.length) return SP_TXT.done.thinking;
  const first = labels[0];
  if (labels.length === 1) return first;
  const cont = s => s.charAt(0).toLowerCase() + s.slice(1);   // 照抄，中文下是恒等
  const second = labels[1];
  if (labels.length === 2) {
    const shared = first.startsWith(SP_TXT.sharedPrefix) &&
                   second.startsWith(SP_TXT.sharedPrefix);
    const tail = shared ? second.slice(SP_TXT.sharedPrefix.length) : second;
    return SP_TXT.joinTwo.replace("{first}", first).replace("{second}", cont(tail));
  }
  const title = [first].concat(labels.slice(1).map(cont)).join(SP_TXT.comma);
  return counts.length > 3 ? SP_TXT.more.replace("{title}", title) : title;
}

/* ③ kind → 图标。照 `PROCESS_ICONS` 抄语义，形状用内联 SVG 自己画。
      ★ 为什么不是文字符号：第一版用 ✎ ▤ ✧ 这些字符，实测在 14px 下
        笔画发虚、基线还偏上（截图里那支铅笔看不清也歪着）。
        文字符号的基线由字体决定，控制不住；SVG 才能钉死 14×14。
      ★ 为什么不是 DSH 的原 SVG：`dsh-client-ui-primitives` 这个包
        不在 app.asar 里（`_asar_list.py "ui-primitives"` = 0 命中），
        web-frontend 的 dist 只解出 2 个 css、没有 js bundle，
        所以拿不到 `IconBrowseOutlineRegular` 的 path 数据。
        这里按 PROCESS_ICONS 的一一对应关系画同义形状，
        笔画宽度统一 1.5、viewBox 16、fill:none —— 与 DSH 的线性图标同族。 */
const SP_SVG = {
  /* IconThink：一圈散射的思考纹 */
  thinking: '<circle cx="8" cy="8" r="2.2"/><path d="M8 1.6v2M8 12.4v2M1.6 8h2M12.4 8h2M3.5 3.5l1.4 1.4M11.1 11.1l1.4 1.4M12.5 3.5l-1.4 1.4M4.9 11.1l-1.4 1.4"/>',
  /* IconBrowse：一页纸 */
  read: '<path d="M3.5 2.2h5.5l3.5 3.5v8.1h-9z"/><path d="M9 2.2v3.5h3.5"/>',
  readImage: '<rect x="2.4" y="3.4" width="11.2" height="9.2" rx="1"/><circle cx="5.9" cy="6.6" r="1.1"/><path d="M2.4 10.4l3.3-2.6 2.6 2 2-1.5 3.3 2.4"/>',
  /* IconSearch：放大镜 */
  search: '<circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2l3.4 3.4"/>',
  /* IconEdit：一支笔 */
  edit: '<path d="M11.2 2.6l2.2 2.2-7.6 7.6-3 .8.8-3z"/>',
  write: '<path d="M11.2 2.6l2.2 2.2-7.6 7.6-3 .8.8-3z"/>',
  /* IconApi：>_ */
  commands: '<path d="M4.4 5.2L2 8l2.4 2.8M11.6 5.2L14 8l-2.4 2.8"/>',
  /* IconCode：尖括号 */
  code: '<path d="M5.6 6L3.2 8l2.4 2M10.4 6L12.8 8l-2.4 2"/>',
  /* IconGlobe：地球 */
  webSearch: '<circle cx="8" cy="8" r="5.6"/><path d="M2.4 8h11.2M8 2.4c1.8 2 1.8 9.2 0 11.2M8 2.4c-1.8 2-1.8 9.2 0 11.2"/>',
  webFetch: '<circle cx="8" cy="8" r="5.6"/><path d="M2.4 8h11.2M8 2.4c1.8 2 1.8 9.2 0 11.2M8 2.4c-1.8 2-1.8 9.2 0 11.2"/>',
  /* IconAgentPreset：一个小人 */
  subagents: '<circle cx="8" cy="5.4" r="2.4"/><path d="M3.4 13.2c0-2.6 2-4.2 4.6-4.2s4.6 1.6 4.6 4.2"/>',
  /* IconPlan：清单 */
  plan: '<path d="M3 4.6h10M3 8h10M3 11.4h10"/><circle cx="1.6" cy="4.6" r=".7" fill="currentColor" stroke="none"/><circle cx="1.6" cy="8" r=".7" fill="currentColor" stroke="none"/><circle cx="1.6" cy="11.4" r=".7" fill="currentColor" stroke="none"/>',
  /* IconQuestion：问号 */
  questions: '<circle cx="8" cy="8" r="5.8"/><path d="M6.2 6.1c0-1 .8-1.7 1.9-1.7s1.9.7 1.9 1.6c0 1.4-1.9 1.5-1.9 2.8"/><circle cx="8" cy="11.4" r=".75" fill="currentColor" stroke="none"/>',
  /* IconSparkle：四角星 */
  tools: '<path d="M8 1.8l1.5 4.2 4.2 1.5-4.2 1.5L8 13.2 6.5 9 2.3 7.5 6.5 6z"/>',
};
function spIcon(kind) {
  const d = SP_SVG[kind] || SP_SVG.tools;
  return '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"' +
    ' fill="none" stroke="currentColor" stroke-width="1.5"' +
    ' stroke-linecap="round" stroke-linejoin="round">' + d + "</svg>";
}

/* ④ 把一串 trace 合成 DSH 那样的过程组。
      ★ 上一版我按「相邻且同 kind」切组，实测一次运行切出 **10 组**
        （更新了计划 / 已写入文件 / 已读取文件 / 已写入文件 / …），
        比原来还碎。DSH 不是这么切的：

        `processTitle()` 会做「A并B」「A，B，C」「A，B，C等」——
        这个"并/等"只有在**一组里含多个类别**时才有意义。
        如果按相邻同类切，每组永远只有一个 kind，
        那 joinTwo 和 more 两个分支**永远不可能走到**。
        能走到它们，说明 DSH 是**整轮聚成一组**。

      荣珂照此：一轮 = 一个过程组，组头是合并标题，点开才是逐次调用。 */
function stepGroups(trace) {
  const items = (trace || []).map(x => ({
    kind: stepKind(String(x.name || "")),
    name: String(x.name || ""),
    ok: x.ok !== false,
    x: x,
  }));
  if (!items.length) return [];
  /* counts：按出现次数降序，同数按首次出现（JS 稳定排序 ⇒ 与 DSH 的
     `sort((a,b)=>b.count-a.count)` 行为一致） */
  const m = new Map();
  for (const it of items) m.set(it.kind, (m.get(it.kind) || 0) + 1);
  const counts = Array.from(m, ([kind, count]) => ({ kind, count }))
    .sort((a, b) => b.count - a.count);
  return [{
    kind: counts[0].kind,          // 组头图标用第一位类别
    counts: counts,
    title: stepTitle(counts),
    items: items,
    failed: items.filter(i => !i.ok).length,
  }];
}


/* ★ W18 · 主视图那一行只放**人看得懂**的东西。
   实测（_tool_summary_probe.py，10 个真实形状的参数）：原先 10 条里有 **5 条**
   把原始串直接摊在主视图上 —— DOI（10.1021/jacs.4c01234）、
   URL（https://arxiv.org/abs/2401.01234）、绝对路径（C:/Users/…/论文初稿_v3.docx）。
   用户看到的是"它在干嘛"的位置，结果读到的是一串机器标识符。

   改法：**按参数名的类型决定怎么显示**，而不是全都 150 字符切一刀：
   · 路径类  → 只留文件名（`论文初稿_v3.docx`），全路径收进展开卡
   · DOI/URL → 换成"一份文献"这类描述，真值收进展开卡
   · 查询词  → 保留（这正是用户想知道"它在查什么"），但长查询截断
   · 其余    → 退回 key=value，但值截短
   展开卡里的"输入"那一栏**一个字都没删** —— 想看原文的人一展开就有。 */
const SUMM_KIND = {
  path:  ["path","file","dir","folder","out","output","target","src","source"],
  doi:   ["doi"],
  url:   ["url","link","href"],
  query: ["query","q","keyword","keywords","pattern","title","topic"],
};
function argKind(k) {
  const kk = k.toLowerCase();
  for (const [kind, names] of Object.entries(SUMM_KIND)) if (names.includes(kk)) return kind;
  return "other";
}
/* 只取文件名：Windows 与 POSIX 两种分隔符都认。
   取不到（没有分隔符）就原样返回 —— 那本来就是个短名字。 */
function baseName(p) {
  const s = String(p);
  const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
  return i >= 0 ? s.slice(i + 1) : s;
}
function toolSummary(t) {
  const a = t.args || {};
  const prefer = ["query","doi","title","path","goal","project","pattern","keyword","name","id"];
  const pick = (k) => {
    const v = String(a[k]);
    switch (argKind(k)) {
      case "path":  return baseName(v);
      case "doi":   return "一份文献";
      case "url":   return "一个网页";
      case "query": return v.length > 60 ? v.slice(0, 60) + "…" : v;
      default:      return v.length > 40 ? v.slice(0, 40) + "…" : v;
    }
  };
  for (const k of prefer) if (a[k]) return pick(k);
  const ks = Object.keys(a);
  if (!ks.length) return "";
  return ks.slice(0, 3).map(k => humanArgKey(k) + "=" + pick(k)).join("  ");
}
/* 兜底分支的键名也说人话 —— 原来直接把 k 这个标识符打上屏。 */
const ARGHUMAN = {
  spec:"规格", style:"风格", template:"模板", pages:"页数", count:"数量",
  max_results:"最多取", top_k:"最多取", year:"年份", author:"作者",
  journal:"期刊", lang:"语言", format:"格式", sheet:"工作表",
  start:"起始", end:"结束", n:"条数", id:"编号",
  url:"链接", doi:"文献", path:"文件", query:"要查的", x:"横轴", y:"纵轴",
  project:"课题", name:"名称", goal:"目标", title:"标题", keyword:"关键词",
};
function humanArgKey(k) { return ARGHUMAN[k] || k; }
function makeToolRow(name, args, st, sfx) {
  const sum = toolSummary({ args });
  const row = document.createElement("div");
  row.className = "trow"; row.dataset.st = st;
  row.innerHTML =
    '<span class="lead">' + glyph(name) + "</span>" +
    '<span class="tname">' + esc(humanTool(name)) + "</span>" +
    (sum ? '<span class="sep"></span><span class="sum">' + esc(sum) + "</span>" : '<span class="sum"></span>') +
    '<span class="sfx">' + esc(sfx || "") + "</span>";
  const detail = document.createElement("div");
  detail.className = "tcard"; detail.hidden = true; detail.dataset.st = st;
  /* W14-C · 展开卡 = TerminalBlock（数值照 dsh_ref 原文，只抄数值）：
     banner（状态点 + cwd=工具名 + 命令行 + 复制键）/ output（结果滚动区）。
     .io/.lab/.txt 三个类名是既有接线的锚点，原样保留在对应位置上。 */
  detail.innerHTML =
    '<div class="tblk">' +
      '<div class="tblk-h">' +
        '<span class="tblk-run"><span class="dot" aria-hidden="true"></span>' +
          '<span class="tblk-runlab">运行中</span></span>' +
        '<span class="tblk-cwd">' + esc(humanTool(name)) + "</span>" +
        (sum ? '<span class="io in tblk-cmd"><span class="lab">输入</span><span class="txt">' +
          esc(JSON.stringify(args || {}, null, 1).slice(0, 1200)) + "</span></span>" : '<span class="tblk-cmd"></span>') +
        '<button class="tblk-copy" data-tcopy type="button" title="复制这条工具的输入与结果">复制</button>' +
      "</div>" +
      '<div class="io out tblk-o"><span class="lab">结果</span>' +
        '<span class="txt">等待中…</span></div>' +
    "</div>";
  row.onclick = () => { detail.hidden = !detail.hidden; };
  return { row, detail, sum };
}
const PENDING = new Map();

function addToolStart(t) {
  const r = cur();
  const o = makeToolRow(t.name, t.args, "run", "运行中");
  r.appendChild(o.row); r.appendChild(o.detail);
  PENDING.set(String(t.seq), o);
}
/* 工具**结束**。★ 本轮的关键改动：结果不再只显示一串哈希。
   内核现在同时给出 `result_preview` / `result_orig_chars` / `result_dropped` /
   `anchors` —— 这三样让"它到底查到了什么"变成**可核验**的东西，
   而不是"相信门通过了"。截断则必须自报（下面那行"共 N 字"）。 */
function resultBody(t) {
  const err = t.error && String(t.error);
  if (err) return { lab: "错误", txt: err.slice(0, 3000), cls: " err" };
  const pv = t.result_preview;
  if (pv == null) return { lab: "结果", txt: t.digest ? "结果哈希 " + t.digest : "（无输出摘要）", cls: "" };
  let note = "";
  if (t.result_truncated) {
    const orig = t.result_orig_chars != null ? t.result_orig_chars : t.result_chars;
    const dropped = t.result_dropped != null ? t.result_dropped : 0;
    note = "\n\n—— 以上为预览；这条结果共 " + orig + " 字" +
           (dropped > 0 ? "，未带出 " + dropped + " 字" : "") + " ——";
  }
  return { lab: "结果", txt: pv + note, cls: "" };
}
function sealTool(o, st, b, sfx) {
  /* 状态从行传到卡：点色走 CSS（.tcard[data-st]），文字标签给读屏；
     data-body 决定 banner 下那条 .5px 发丝线画不画（DSH：没输出的运行态不画）。 */
  o.row.dataset.st = st;
  o.row.querySelector(".sfx").textContent = sfx;
  const out = o.detail.querySelector(".io.out");
  out.querySelector(".lab").textContent = b.lab;
  const txt = out.querySelector(".txt");
  txt.textContent = b.txt; txt.className = "txt" + b.cls;
  o.detail.dataset.st = st;
  const blk = o.detail.querySelector(".tblk");
  if (blk) {
    blk.dataset.body = "1";
    const lab = o.detail.querySelector(".tblk-runlab");
    if (lab) lab.textContent = st === "ok" ? "成功" : st === "err" ? "失败" : "已拦截";
  }
}
function addTool(t) {
  const key = String(t.seq);
  const o = PENDING.get(key);
  const st = t.blocked_by ? "block" : (t.ok ? "ok" : "err");
  const b = resultBody(t);
  const sfx = t.blocked_by ? "已拦截" : (t.ms != null ? t.ms + "ms" : "");
  if (o) {
    PENDING.delete(key);
    sealTool(o, st, b, sfx);
    return;
  }
  const r = cur();
  const n = makeToolRow(t.name, t.args, st, sfx);
  sealTool(n, st, b, sfx);
  r.appendChild(n.row); r.appendChild(n.detail);
}

function addBody(text) {
  if (!text) return;
  /* 模型每一步说的话 = 这一轮的过程叙述，收进过程组（DSH 的 Process），
     不再平铺进主流程。主流程只留「用户气泡 + 结果卡」——
     否则 13 条英文过程独白会和思考行混成一片日志（用户原话就是"一言难尽"）。 */
  const d = document.createElement("div");
  d.className = "say";
  d.innerHTML = md(text);
  cur().appendChild(d);
  if (typeof LIVE !== "undefined" && LIVE) scroll();
}

function addGate(d) {
  const r = cur();
  const el = document.createElement("div");
  if (d.passed) {
    const ev = (d.evidence || []).map(e =>
      '<div class="kv"><b>seq=' + e.call_seq + "</b><span>" + esc(e.tool) + " → " +
      esc((e.shows || "").slice(0, 150)) + "</span></div>").join("");
    el.className = "gate pass";
    el.innerHTML = "<b>完成核查通过</b> —— 每条验收标准都有真实操作记录撑着" + ev;
  } else {
    el.className = "gate fail";
    el.innerHTML = "<b>完成核查没通过</b><ul>" +
      (d.reasons || []).map(x => "<li>" + esc(x) + "</li>").join("") + "</ul>" +
      ((d.warnings || []).length ? '<div class="w">软提示：' + esc(d.warnings.join(" / ").slice(0, 260)) + "</div>" : "") +
      (d.writing ? "<pre>" + esc(d.writing) + "</pre>" : "");
  }
  r.appendChild(el);
}

/* ── 写作体检卡 ── */
/* 引注首现序是否递增：内核已把首现序记在 metrics.cite_first_order 里，
   这里只做展示，判定仍在内核 —— UI 不重复实现规则。 */
function citeDisorder(m) {
  const seq = m.cite_first_order || [];
  let hi = 0;
  for (const n of seq) { if (n < hi) return true; if (n > hi) hi = n; }
  return false;
}
function lintCard(lint, open) {
  if (!lint) return "";
  const m = lint.metrics || {};
  const issues = lint.issues || [];
  const hard = issues.filter(i => i.level === "hard");
  const soft = issues.filter(i => i.level === "soft");
  const sc = lint.score || 0;
  const col = sc >= 85 ? "var(--ok-fg)" : (sc >= 70 ? "var(--warn-fg)" : "var(--err-fg)");
  const row = i => '<div class="iss ' + i.level + '"><div class="t">' +
    (i.level === "hard" ? "必修" : "建议") + " · " + esc(i.msg) + "</div>" +
    '<div class="f">改法：' + esc(i.fix) + "</div></div>";
  return '<div class="lint" data-open="' + (open ? "1" : "0") + '">' +
    '<div class="lh">稿面体检' +
      (hard.length ? '<span class="pill err">必修 ' + hard.length + "</span>" : "") +
      (soft.length ? '<span class="pill warn">建议 ' + soft.length + "</span>" : "") +
      (!hard.length && !soft.length ? '<span class="pill ok">无问题</span>' : "") +
      '<span class="sc" style="color:' + col + '">' + sc + "/100</span></div>" +
    '<div class="lb"><div class="mt">' +
      "<span>" + (m.chars || 0) + " 字</span><span>" + (m.paragraphs || 0) + " 段</span>" +
      "<span>" + (m.sentences || 0) + " 句</span><span>均句 " + (m.avg_sentence || 0) + " 字</span>" +
      "<span>最长句 " + (m.max_sentence || 0) + " 字</span>" +
      (lint.target_chars ? "<span>目标 " + lint.target_chars + " 字</span>" : "") +
      (m.cliche_hits ? "<span>套话 " + m.cliche_hits + " 处</span>" : "") +
      (m.citations ? "<span>引注 " + m.citations + " 处</span>" : "") +
      (citeDisorder(m) ? "<span>引注未按首现序</span>" : "") +
      (m.has_refs ? "<span>正文 " + (m.chars_excl_refs || 0) + " 字（剔文献表）</span>" : "") +
      (m.figures ? "<span>图 " + m.figures + " 张</span>" : "") +
      (m.tables ? "<span>表 " + m.tables + " 个</span>" : "") +
      (m.placeholders ? "<span>占位符 " + m.placeholders + " 处</span>" : "") +
      (m.jargon_hits ? "<span>行话 " + m.jargon_hits + " 处</span>" : "") +
      (m.tail_repeat ? "<span>同尾 " + m.tail_repeat + " 段</span>" : "") +
      (m.absolutes ? "<span>绝对化 " + m.absolutes + " 处</span>" : "") +
      (m.max_en_words ? "<span>英文最长 " + m.max_en_words + " 词</span>" : "") +
      "</div>" + issues.map(row).join("") + "</div></div>";
}
function wireLint(box) {
  box.querySelectorAll(".lint .lh").forEach(h => {
    h.onclick = () => {
      const p = h.parentElement;
      p.dataset.open = p.dataset.open === "1" ? "0" : "1";
    };
  });
}

/* ═════════════ 时间 / 大小 / 成本 帮手 ═════════════
   三条纪律，都来自本项目踩过的坑：
   1) **UNKNOWN 原样显示**，绝不替换成 0 或 "—"。0 是"这次不要钱"的断言，
      而"我不知道"是事实 —— 把后者渲染成前者就是假安慰。
   2) 每缺一个数就说明**为什么缺**，而不是留个哑谜。
   3) 时间一律带单位且不足 1s 用毫秒。 */
let LAST_LLM = null;
function fmtMs(ms) {
  const v = Number(ms) || 0;
  if (v < 1000) return Math.round(v) + "ms";
  if (v < 60000) return (v / 1000).toFixed(1) + "s";
  return Math.floor(v / 60000) + "m" + Math.round((v % 60000) / 1000) + "s";
}
function fmtK(n) {
  const v = Number(n) || 0;
  return v >= 1000 ? (v / 1000).toFixed(1) + "k" : String(v);
}
function fmtSize(n) {
  const v = Number(n) || 0;
  if (v < 1024) return v + " B";
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " KB";
  return (v / 1048576).toFixed(1) + " MB";
}
function costText(res) {
  return (res && res.cost_display) ? res.cost_display : "UNKNOWN";
}

/* ═════════════ 事件分发 ═════════════ */
async function onEvent(ev) {
  const k = ev.kind, d = ev.data || {};
  switch (k) {
    case "workspace":  break;
    /* ── 能力预设（2026-11-04 插件化）──
       这个 switch 的 default 会静默丢掉没列出的 kind：归档里有动作、
       界面上一片安静，等于「做了但没人知道它做过」。内核这四条新事件
       必须在这儿露面，用户才知道这一轮用的是哪把尺子。 */
    case "preset_mounted": {
      const nm = PRESET_LABELS[d.preset] || d.preset || "这套要求";
      note("这一轮按「" + nm + "」的要求出稿", "prog",
           "预设 id=" + (d.preset || "") + " · healthy=" + d.healthy +
           " · 能力行数=" + ((d.rows || []).length) +
           " · broken=" + JSON.stringify(d.broken || []) +
           " · 本场临时工具=" + JSON.stringify(d.registered_tools || []) + "（跑完自动撤销）");
      break;
    }
    case "preset_stages": {
      /* 不同任务挂不同 skill —— 这一条要让用户在界面上看得见「换了工具箱」，
         而不只是「换了尺子」。技能名同时写进详情，能对着任务单逐条核。 */
      const nm = PRESET_LABELS[d.preset] || d.preset || "这套要求";
      const sk = d.skills || [];
      note("「" + nm + "」这轮要用 " + sk.length + " 个专属技能，开工前先读", "prog",
           "工序=" + JSON.stringify(d.stages || []) +
           " · 本轮补进=" + JSON.stringify(d.added || []) +
           " · 技能=" + JSON.stringify(sk) +
           " · 清单在任务单里逐条列出，交付门按同一份清单核装载");
      break;
    }
    /* ── 2026-11-05 内核优化：装配只在开场发生一次，之后整场不动。
       把「省了多少字、丢了哪些段」说成人话 —— 否则用户只看到提示变短，
       不知道内核替他做了什么，也无法察觉装配失败后的降级。 */
    case "system_assembled": {
      const nm = PRESET_LABELS[d.preset] || d.preset || "这套要求";
      if (d.error) {
        note("「" + nm + "」的上下文装配没生效，已按完整提示继续跑", "warn",
             "装配失败：" + d.error + " → 退回全量 SYSTEM（不影响出稿）");
      } else {
        note("「" + nm + "」开场只装配了用得上的提示段，省下 " + (d.saved || 0) +
             " 字符，本场不再改动", "prog",
             "保留=" + JSON.stringify(d.sections || []) +
             " · 裁掉=" + JSON.stringify(d.dropped || []) +
             " · 点名保留=" + JSON.stringify(d.kept_sections || []) +
             " · system " + d.chars + "/" + d.baseline_chars + " 字符" +
             /* `complete` 只代表「外部全文整体替换」；「内核骨架＋点名保留」
                也是结构性覆盖，读新字段才不会把 true 显示成 false。 */
             " · 结构性覆盖=" + !!(d.structural_override || d.complete) +
             " · 整场冻结=" + d.frozen);
      }
      break;
    }
    case "tool_scope_expanded":
      note("它点了一个本轮没列出的工具（" + humanTool(d.tool) +
           "），已放开给它，之后本场保持", "prog",
           "工具可见性一次性扩张：" + d.tool + " · 现在 " +
           (d.tools_available || 0) + " 个 · " + (d.reason || ""));
      break;
    /* 硬预算不是「悄悄少给几个工具」：少了哪几个、必留通道还在不在，
       都要当场说清楚。被裁的工具模型仍可点名，点名了就走一次性回退。 */
    case "tools_budget_applied": {
      const n = (d.dropped || []).length;
      note("这套要求把工具清单压到 " + (d.after_tools || 0) + " 个（原 " +
           (d.before_tools || 0) + " 个），省下 " +
           ((d.before_chars || 0) - (d.after_chars || 0)) + " 字符，本场不再改动",
           "prog",
           "预算=" + d.budget + " · 必留=" + JSON.stringify(d.essential || []) +
           " · 裁掉 " + n + " 个：" + JSON.stringify(d.dropped || []) +
           " · 整场冻结=" + d.frozen);
      break;
    }
    case "preset_error":
      note("这套要求没挂上，已按默认要求继续跑，不影响出稿", "warn",
           "预设 " + (d.preset || "") + " 装载失败：" + (d.error || "") +
           " → 回退 " + (d.fallback || "baseline"));
      break;
    case "followup_constraints":
      if (d.error) {
        note("这一轮新提的要求没能并进验收，仍按原尺子核", "warn",
             "合并失败：" + d.error);
      } else {
        note("这一轮新提的 " + (d.n || 0) + " 条要求已并进验收，改完照新尺子核", "prog",
             "新增：" + JSON.stringify(d.items || []) +
             " · 原有 " + (d.parent_kept || 0) + " 条保留");
      }
      break;
    case "followup_precompact":
      note("改稿前先收一收上一轮的长记录（" + (d.before || 0) + " → " +
           (d.after || 0) + " 条），省得每一步都重发", "prog",
           "追问预压缩：保留最近 " + (d.kept_live || 0) + " 条实时消息");
      break;
    case "contract_drafting":
      /* ★ W36：这条只是**过渡提示**。非写作任务不会紧跟着来一张任务单
         （内核按 needs_writing 分流，见 loop.py），所以措辞不能预设"马上给你单子"，
         否则用户会瞪着一个永远不会出现的任务单。 */
      phase("◌", "正在看这一轮要做什么", "先认出这是对话还是要交稿子"); break;
    case "contract":
      renderTrajContract(d); renderGoalBar(d); break;
    case "mode":
      /* ★ W36：内核按「这轮是不是写稿」分流时会发这条。
         `chat` = 纯对话（不列任务单），`answer`/`task` = 用户亲手按的模式开关。
         这里**只更新状态与提示**，不渲染任务单 —— 渲染仍是 `contract` 事件的活。 */
      if (d && d.text) note(d.text);
      break;
    case "memory_recall": break;
    case "llm_call":
      phase("◎", "第 " + d.n + " 步：模型在想", "思考与工具调用记在「过程」里", null, "openproc");
      break;
    case "llm_done":
      LAST_LLM = d;
      break;
    case "context_warn":
      note("上下文偏大了（" + fmtK(d.chars) + " 字符），越往后每一步越贵 —— 快满时它会自动瘦身，不用你管",
           "warn",
           "上下文偏大：" + fmtK(d.chars) + " 字符（阈值 " + fmtK(d.limit) +
           "）—— 每一步都会重发，越往后越贵"); break;
    // ── 上下文经济与预算闸（2026-09-25）──────────────────────
    // 这个 switch 的 `default: break` 会**静默丢掉**任何没列出的 kind。
    // 所以内核发新事件必须同时在这里加一行 —— 否则用户看到的是一片安静，
    // 而归档里其实有动作（那是 M 型的反面：做了，但没人知道它做过）。
    case "context_compact":
      note("上下文太满了，已自动清理，省下 " + fmtK(d.freed_chars) + " 字符（对话没断，继续跑）",
           "prog",
           "上下文出清（第 " + d.step + " 步，因 " + (d.why || "超阈值") + "）：" +
           "省下 " + fmtK(d.freed_chars) + " 字符，走了第 " +
           (d.levels || []).join("→") + " 级，这是本场第 " + d.n + " 次" +
           " —— 更早的工具结果换成了从过程记录现算的摘要，原文仍在过程记录里"); break;
    case "context_compact_skipped":
      note("上下文已经很紧，但再裁就要碰到还不能动的部分 —— 这次一分没省，先顶着跑",
           "prog",
           "上下文出清**没动手**：可动区已被「最近保留」护到底，本次一分钱没省，" +
           "因此也不付前缀缓存那一下"); break;
    case "context_overflow":
      note("上下文超了网关的线，它正在自动清理后重试一次", "warn",
           "⚠ 网关报上下文溢出（HTTP " + d.code + "）—— 先出清一次历史再重试一次"); break;
    case "context_overflow_recovered":
      note("清理后重试成功，任务继续（第 " + d.step + " 步）", "prog",
           "出清后重试成功，任务继续（第 " + d.step + " 步）"); break;
    case "context_overflow_failed":
      note("清理后仍装不下 —— 但进度已存档，可以接着跑，**这不是失败**", "err",
           "✘ 出清后仍不可用 —— 进度已落快照，**可以接着跑**，这不是失败"); break;
    case "wrapup_hint":
      STEP_CAP = d.max_steps || STEP_CAP;
      paintSteps();
      note("还剩 " + d.steps_left + " 步：已让它停止新检索、先把交付物做完整", "prog",
           "剩余 " + d.steps_left + " 步：已提示它停止新检索、先把交付物做完整"); break;
    case "budget_note":
      if (!d.degraded) paintCtx(d.share);
      note(d.degraded
        ? "网关没回准确的用量，改按字符数估算上下文压力（口径糙一点，不影响任务）"
        : ("上下文额度用去 " + Math.round((d.share || 0) * 100) + "%" +
           (d.share >= 0.8 ? " —— 偏高了，已提示它别再开新战线" : "，已提示它别再开新战线")),
        d.degraded ? "prog" : (d.share >= 0.8 ? "warn" : "prog"),
        d.degraded
          ? ("预算闸降级为字符闸（网关没回 input_tokens，口径：" + d.degraded + "）")
          : ("上下文额度用去 " + Math.round((d.share || 0) * 100) + "%：已提示别再开新战线")); break;
    case "resumed":
      // 接回来的可能是一份**脱过水**的历史。不说的话，下一轮出清的"省了多少"
      // 会拿缩水过的栈当基线，看着就像"压缩没起作用"。
      if (d.prior_compaction && Object.keys(d.prior_compaction).length)
        note("接回来的这段旧对话此前瘦身过（上次省了 " +
             fmtK(d.prior_compaction.freed_chars || 0) + " 字符），不是全量 —— 后面的账都按这份算",
             "prog",
             "续跑接回的这份历史**此前出清过**（上次省 " +
             fmtK(d.prior_compaction.freed_chars || 0) + " 字符）—— 它不是全量"); break;
    case "metrics":
      break;
    case "thinking":
      addThink(d.text, true, {text_chars:d.text_chars, truncated:!!d.truncated}); break;
    case "follow_up": {
      /* 直播：followUp() 已经把这条用户消息放进 #sayFlow；事件只用于回放。
         回放：优先用事件里的原文（新归档）；旧归档只有 chars，从
         /api/run 的 followups 里补（backend 从 conversation 快照提取）。 */
      if (LIVE) break;
      let text = d.text != null ? String(d.text) : "";
      if (!text) text = REPLAY_FOLLOWUPS[REPLAY_FU_I++] || "";
      if (text) addUserInFlow(text, true);
      else note("你在这之后追加了 " + (d.chars || "?") +
                " 个字符（旧归档没有存原文）", "prog",
                "会话内追问：旧归档只记了长度");
      break;
    }
    case "step":
      sealThink();
      STEPS = d.n;
      paintSteps();
      /* 不再每步 newRound()：那会在过程组里插 65 条分隔线。
         一轮就是一个过程组，步骤叙述按事件顺序接在后面。 */
      addBody(d.content);
      break;
    case "tool_start":
      addToolStart(d);
      phase("⛭", "正在" + humanTool(d.name), toolSummary({ args: d.args }));
      break;
    case "tool":       addTool(d); break;
    case "mentor_mode":
      /* ★ 开关的位置由**内核的事件**决定，不由前端自己的记忆决定：
         直播时收到一条、回放时从归档的 events 里重放同一条 —— 两处同一个来源。
         老归档里没有这条事件（那是 W22 之前跑的），前端保持默认"关"，不猜。 */
      setMentor(!!d.on);
      if (d.on) {
        if (d.topic) {
          try {
            TOPIC = await api("/api/topic?slug=" + encodeURIComponent(d.topic));
            if (window.RongkeMentor) RongkeMentor.refresh(); else paintMentorBar();
          } catch (e) { TOPIC = { error: e.message }; paintMentorBar(); }
        } else loadTopic();
      }
      break;
    case "mentor_mode_injected":
      note(d.action === "on"
        ? "这场会话中途打开了导师模式 —— 内核已把那段纪律补进对话里"
        : "这场会话中途关掉了导师模式 —— 内核已补一句撤销，前面那段不再管用", "prog");
      break;
    case "mentor_audit":
      renderMentorAudit(d);
      break;
    case "deliver_attempt":
      addGate(d);
      if (d.passed) phase("✔", "交付通过了，正在收尾");
      else {
        REJECTS++;
        phase("↺", "交付被打回，它正在按意见改", "第 " + REJECTS + " 次打回");
      }
      break;
    case "stall":
      STALLS++;
      note("它卡住了：同一个动作在原地重复（" + humanTool(d.tool) + "），已提示它换路子",
           "warn",
           "原地打转：连续重复同一个调用（" + d.tool + "），已注入换策略提示");
      phase("⚠", "它卡住了：在重复同一个动作", "已提示换策略（" + humanTool(d.tool) + "）", "warn");
      break;
    case "intervene_request":
      renderAsk(d);
      phase("✋", askWhy(d), "它停下来等你一句话 —— 回答，或直接让它继续", "warn");
      break;
    case "intervene_applied": {
      const what = d.action === "extend"
        ? "已放宽到 " + d.max_steps + " 步"
        : "已把你的补充接上（" + (d.chars || 0) + " 字）";
      if (d.action === "extend" && d.max_steps) { STEP_CAP = d.max_steps; paintSteps(); }
      const host = $("#askCard");
      if (host) {
        host.innerHTML = '<div class="ask"><div class="ab"><div class="done">' +
          "你的决定已生效：" + esc(what) + "</div></div></div>";
      }
      ASK = null;
      phase("▶", "按你的意思继续", what);
      break;
    }
    case "intervene_answered": break;   // 回执：等 applied / note 再改界面
    case "intervene_note":
      hideAsk();
      note(d.text || "没有人应答，已按原路继续");
      break;
    case "nudge": note("连续两轮没动手，已提醒它去做而不是只说", "warn",
                       "连续两轮没有调用工具，已提醒它动手"); break;
    case "remind": note("第 " + d.step + " 步：重申目标，防漂移"); break;
    case "cancel_requested": note("已请求停止 —— 会在下一步开头退出"); break;
    case "cancelled": note("任务已取消（第 " + d.step + " 步）"); break;
    case "memory_saved": break;
    case "note": note(d.text || ""); break;
    case "blocker":      addGate({ passed:false, reasons:["模型报告阻塞：" + (d.reason || "")], warnings: d.need ? ["需要：" + d.need] : [] });
      note("它说这一步干不下去了：" + (d.reason || "未说明原因") +
           (d.need ? "（它需要：" + d.need + "）" : "") + " —— 可以补一句话，或换个问法",
           "err", "模型报告阻塞：" + (d.reason || "") + (d.need ? " ｜需要：" + d.need : ""));
      phase("⛔", "它说干不下去了", d.reason || "", "warn");
      break;
    case "_end": finish(d); break;
    default: break;
  }
}
/* W21-B · 消息分级。**默认档逐字节不变**：不传 level（或传 "info"）走的
   还是原来那条 `--fs-xxs / --fg-cap` 小字、进过程抽屉 —— 20 多个既有调用点
   一个字都不必改。prog/warn/err 三档是**新路径**：带图标和颜色，
   留在对话流可见层，不随下一次 phase() 覆写消失。
   第三个参数 tech：原始术语原文，一个字不删，收进可展开的「技术细节」。 */
function note(t, level, tech) {
  if (!level || level === "info") {
    const r = cur();
    const el = document.createElement("div");
    el.style.cssText = "font-size:var(--fs-xxs);color:var(--fg-cap);padding:3px 0 3px 22px";
    el.textContent = t;
    r.appendChild(el);
    return;
  }
  /* ★ 状态通知（上下文清理 / 卡住 / 出错）归**过程抽屉**，不占主对话流。
     改前它们 insertBefore 到 #thread 顶部之下，回放一个 48 分钟的任务时
     实测 9 条大边框卡片（清理×5 + 卡住×4）把主区整个占满，
     53 条模型发言被挤到一屏之外 —— 用户看到的还是"没有历史记录"。
     DSH 的做法正是这样（`*_compactionBody`）：过程抽屉里一行 tertiary 小字，
     缩进 calc(22px + 字号增量)，没有边框、没有底色。
     标记保留：一眼能看出"这一步出过事"，点开过程才读细节。 */
  const r = cur();
  const el = document.createElement("div");
  el.className = "nfeed"; el.dataset.lv = level === 'mentor-audit' ? 'prog' : level;
  const ic = document.createElement("span"); ic.className = "ic";
  ic.textContent = level === "err" ? "✘" : level === "warn" ? "⚠" : level === 'mentor-audit' ? '▤' : "◈";
  const nx = document.createElement("div"); nx.className = "nx";
  const tx = document.createElement("div"); tx.textContent = t;
  nx.appendChild(tx);
  if (tech) {
    const tg = document.createElement("div");
    tg.className = "ntech"; tg.textContent = "技术细节 ▸";
    const tb = document.createElement("div");
    tb.className = "ntechb"; tb.hidden = true; tb.textContent = tech;
    tg.onclick = () => {
      const open = !tb.hidden;
      tb.hidden = open;
      tg.textContent = "技术细节 " + (open ? "▸" : "▾");
    };
    nx.appendChild(tg); nx.appendChild(tb);
  }
  el.appendChild(ic); el.appendChild(nx);
  // 收进**过程抽屉**的当前 round（与思考/工具同列），不再插到主区
  r.appendChild(el);
  if (level === 'mentor-audit') mentorAuditCitations(el, window.MENTOR_AUDIT_CITES || []);
}

/* ═════════════ ★ 介入卡 ═════════════
   内核跑到"再跑也是白跑"的三个地方会**停下来**问人（loop.py 的三个哨位：
   交付第 2 次打回 / 原地打转 / 步数耗尽）。这张卡就是那个提问。

   两条设计规矩：
   ① 它和成果卡**并列在默认层** —— 此刻全局最要紧的事就是回答它。
      塞进「过程」抽屉 = 把求助做成彩蛋。
   ② **"继续跑"必须永远可用**（且是免输入的一键）。
      用户在开会、在走路、没看懂 —— 都得有条"你别管了接着跑"的路。
      否则这张卡自己就变成了新的阻塞，那我们只是把"整轮重来"换成了"整轮卡死"。 */

const ASKWHY = {
  gate_reject: {
    t: "交付反复没过核查，它自己改不动了",
    why: "它已经交过两次，都被完成核查挡下。再让它自己试，多半还是同一条路。",
    ok: "按打回意见继续改",
  },
  stall: {
    t: "它卡住了：在原地重复同一个动作",
    why: "同一个工具、同一组参数反复调用，说明它没找到别的路子。",
    ok: "让它自己换策略再试",
  },
  step_limit: {
    t: "步数用完了，它还差一点",
    why: "上限是开工前定好的。现在可以放宽，也可以就此收手 —— 已经做出来的东西都在。",
    ok: "不加了，收手",
  },
};
function askWhy(d) { return (ASKWHY[d.kind] || {}).t || "它停下来等你决定"; }

function fmtWait(s) {
  const n = Number(s);
  if (!n || n <= 0) return "一段时间";
  return n < 90 ? Math.round(n) + " 秒" : Math.round(n / 60) + " 分钟";
}

function hideAsk() {
  const host = $("#askCard");
  if (host) host.innerHTML = "";
  ASK = null;
}

function renderAsk(d) {
  const host = $("#askCard");
  if (!host) return;
  ASK = d;
  const w = ASKWHY[d.kind] || { why: "", ok: "继续" };
  let extra = "";
  if (d.kind === "gate_reject") {
    const rs = (d.reasons || []).slice(0, 4);
    if (rs.length) {
      extra = '<ul class="reasons">' +
        rs.map(r => "<li>" + esc(r) + "</li>").join("") + "</ul>";
    }
  } else if (d.kind === "stall") {
    extra = '<div class="why">已连续 ' + (d.repeat || 3) + " 次调用 <b>" +
      esc(humanTool(d.tool)) + "</b>（seq=" + d.seq + "）。</div>";
  } else if (d.kind === "step_limit") {
    extra = '<div class="why">已跑满 <b>' + d.max_steps + "</b> 步。</div>";
  }
  const extendBtn = d.kind === "step_limit"
    ? '<button class="mini pri" data-ask="extend">再加 20 步</button>' : "";
  host.innerHTML =
    '<div class="ask"><div class="ah"><span class="ic">✋</span>' +
      '<span class="t">' + esc(w.t) + "</span>" +
      '<span class="sp">第 ' + (d.step || "?") + " 步 · 它在等</span></div>" +
    '<div class="ab"><div class="why">' + esc(w.why) + "</div>" + extra +
      '<div class="acts">' + extendBtn +
        '<button class="mini' + (d.kind === "step_limit" ? "" : " pri") +
          '" data-ask="steer">补一句，让它接着干</button>' +
        '<button class="mini' + (d.kind === "step_limit" ? "" : "") +
          '" data-ask="continue">' + esc(w.ok) + "</button>" +
        '<button class="mini" data-ask="stop">停止</button>' +
      "</div>" +
      '<div class="box" id="askBox">' +
        '<textarea id="askText" placeholder="例如：别再用 Crossref，换 OpenAlex；顺带把目标字数降到 200。"></textarea>' +
        '<div class="acts"><button class="mini pri" data-send="steer">按这句继续</button>' +
        (d.kind === "step_limit"
          ? '<button class="mini" data-send="steer-extend">按这句继续，并加 10 步</button>'
          : "") +
        "</div></div>" +
      '<div class="hint">不回答也行：' + fmtWait(d.timeout) +
        "后它会按原路继续 —— 你的任务不会因为你在忙而失败。</div>" +
    "</div></div>";
  document.querySelectorAll("[data-ask]").forEach(
    b => { b.onclick = () => onAskBtn(b.dataset.ask); });
  document.querySelectorAll("[data-send]").forEach(
    b => { b.onclick = () => sendAsk(b.dataset.send); });
}

function onAskBtn(a) {
  if (a === "steer") {
    const box = $("#askBox");
    if (box) { box.dataset.open = "1"; const t = $("#askText"); if (t) t.focus(); }
    return;
  }
  submitIntervene(a, "");
}

function sendAsk(mode) {
  const t = ($("#askText") && $("#askText").value || "").trim();
  if (!t) { const el = $("#askText"); if (el) el.focus(); return; }
  submitIntervene("steer", t, mode === "steer-extend" ? 10 : null);
}

/* 提交后**立刻**把卡改成"已提交"，不等服务端回话 ——
   内核要等到下一次模型调用才接住答复，这中间可能好几秒。
   若盯着原卡不动，用户会以为自己没点上、于是连点几次。 */
async function submitIntervene(action, text, steps) {
  if (!CUR || !CUR.id) return;
  const host = $("#askCard");
  const word = { continue: "继续跑", stop: "停止", extend: "加步数" }[action] ||
               (text ? "按你的补充继续" : "继续");
  if (host) {
    host.innerHTML = '<div class="ask"><div class="ab"><div class="done">' +
      "已提交（" + esc(word) + "）· 正在等它接住…</div></div></div>";
  }
  ASK = null;
  try {
    const r = await fetch("/api/intervene", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: CUR.id, action: action, text: text || "",
                             steps: steps == null ? null : steps }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      if (host) {
        host.innerHTML = '<div class="ask"><div class="ab"><div class="done">' +
          esc(j.error || ("提交失败（HTTP " + r.status + "）")) + "</div></div></div>";
      }
    }
  } catch (e) {
    if (host) {
      host.innerHTML = '<div class="ask"><div class="ab"><div class="done">' +
        "提交失败：" + esc(e.message) + "</div></div></div>";
    }
  }
}

/* ═════════════ ★ 成果卡 ═════════════
   默认层的主角：一句话结论 + 产出物 + 要你定夺什么 + 可核验的证据。
   所有系统内部量（门判定细节、tokens、成本口径、埋点）塞进「技术细节」折叠区。 */
function procLabel(res) {
  if (!res) return "已完成";
  if (!res.success) {
    const why = String(res.reason || "");
    if (/abort|interrupt|stop|cancel/i.test(why)) return "已停止";
    /* 「未完成」不是「失败」：步数耗尽/原地打转都能从断点接着跑。
       把它写成失败，用户会以为要重来一遍。 */
    if (/未完成|最大步数|步数耗尽|打转|stall/i.test(why)) return "未完成";
    return "处理失败";
  }
  const ms = (res.telemetry && res.telemetry.elapsed_ms) || 0;
  return ms > 0 ? "已完成，用时 " + fmtMs(ms) : "已完成";
}
/* `turn` 缺省 = 当前轮（直播）；回放按轮传入，历史轮只写自己的结果卡，
   不碰 RESUMABLE / TRAJ / 收件箱这些"当前会话"的东西。 */
function finish(d, turn) {
  const t = turn || TURN;
  const isCurrent = !t || !!t.current;
  sealThink();
  // 实时跑完的：没成功 ⇒ 对话快照还在（内核只在 success 时删它）⇒ 可以接着跑。
  // 回放的：不在这里覆盖 —— 那次的结果是"当时"的，快照有没有要问磁盘（replay 带回来）。
  if (LIVE && isCurrent) RESUMABLE = !(d.result && d.result.success);
  /* ★ 收件箱的**唯一入队点**。
     放在 renderResult 之前，是因为入队不该依赖渲染成功与否 ——
     渲染抛异常时，用户更需要那条"出错了"的提示，而不是一起丢掉。
     ★ `d.replay` 的回放**不入队**：回放是"看一段旧记录"，不是"刚发生的事"。 */
  if (!d.replay && LIVE && isCurrent) {
    try { inboxRecord(d.result, d.error); } catch (e) { /* 收件箱坏了不能拖累收尾 */ }
  }
  if (d.error) {
    if (t) {
      t.title.textContent = "执行异常";
      t.sub.textContent = "";
      t.head.dataset.st = "err";
      renderResult({ error: d.error }, t.res);
    }
  } else if (d.result) {
    if (t) {
      const lbl = procLabel(d.result);
      t.title.textContent = lbl;
      t.sub.textContent = "";            // 收尾时清掉最后一条「正在…」副行
      t.head.dataset.st = d.result.success ? "ok"
        : (lbl === "未完成" ? "warn" : "err");
      if (d.result.steps) {
        t.mStep.hidden = false;
        t.mStep.textContent = d.result.steps + " 步";
      }
      renderResult(d.result, t.res);
    }
    if (isCurrent) renderTrajResult(d.result);
  }
  endTurn(t);
  if (isCurrent) done();
}

function decisions(res, arts) {
  const out = [];
  if (!res.success) {
    const acts = [];
    if (RESUMABLE) {
      // 有快照 ⇒ **接着跑**才是第一选择："重跑"会把已经查到的证据、
      // 已经写好的段落全部再烧一遍钱和时间 —— 那正是评审要消灭的「整轮重来」。
      acts.push(["接着上次跑（不重来）", "resume"], ["从头重跑", "rerun2x"]);
    } else {
      acts.push(["加大步数重跑", "rerun2x"]);
    }
    acts.push(["改目标再跑", "editgoal"]);
    out.push({
      ic: "⚠",
      t: RESUMABLE ? "这次没跑完，但可以从断点接着跑" : "上次没做完",
      d: RESUMABLE
        ? "对话快照留在工作区里。接着跑会带上已经查到的证据、已经写好的部分继续 —— "
          + "不会把前面的步骤再烧一遍。"
          + (res.reason ? "（上次停在：" + res.reason + "）" : "")
        : (res.reason || ""),
      a: acts });
  }
  if (REJECTS > 0) {
    out.push({ ic:"↺", t:"交付被打回过 " + REJECTS + " 次",
      d:"打回意味着它对照验收标准判定证据不足或稿面不合格。若打回理由你觉得不合理，那就是验收标准写得验不了 —— 改标准比让它再跑一遍划算。",
      a:[["看打回理由", "openproc"]] });
  }
  if (STALLS > 0) {
    out.push({ ic:"⚠", t:"出现过原地打转", d:"它连续重复了同一个调用。通常是缺一个工具或缺一条约束。",
      a:[["看卡在哪一步", "openproc"]] });
  }
  if (!res.success && res.steps && (res.steps >= (MAXSTEPS || (STATUS.loop && STATUS.loop.max_steps) || 40))) {
    out.push({ ic:"⏱", t:"是撞到步数上限停的，不是失败",
      d:"这类任务加大步数通常能跑完（实测：同一目标 14 步耗尽 ⇒ 加到 30+ 步可到底）。",
      a:[["步数翻倍重跑", "rerun2x"]] });
  }
  if (arts && arts.exists && arts.count === 0 && res.success) {
    out.push({ ic:"◻", t:"这次没有落盘文件", d:"结论都在上面的正文里。若你要的是文件（docx/图/表），在目标里写明交付形式。" });
  }
  if (arts && !arts.exists && res.run_id) {
    out.push({ ic:"◻", t:"工作区目录不见了", d:"归档里记着这次任务，但磁盘上的工作区已不在 —— 可能是被清理掉了。" });
  }
  return out;
}
function actsHtml(a) {
  if (!a || !a.length) return "";
  return '<div class="da">' + a.map(([lab, act]) =>
    '<button class="mini' + ((act === "rerun2x" || act === "resume") ? " pri" : "") +
    '" data-act="' + act + '">' +
    esc(lab) + "</button>").join("") + "</div>";
}
function anchorHtml(anchors) {
  if (!anchors) return "";
  const names = { doi: "DOI", url: "网址", num: "数值" };
  let h = "";
  for (const k of ["doi", "url", "num"]) {
    const arr = anchors[k] || [];
    if (!arr.length) continue;
    h += '<span class="grp">' + names[k] + "</span>" +
      arr.map(x => "<code>" + esc(x) + "</code>").join("");
  }
  return h ? '<div class="anch">' + h + "</div>" : "";
}
/* ═══ 交付正文里的图片地址：把稿子里的相对路径换成控制台能真加载的 URL ═══
   ★ 这是「用户要照片汇报、拿到 4 个（照片位置）占位符」这条链上最长的一根刺。
     实测那次运行：工作区里明明躺着 `img_1.jpeg`(56KB) 与 `img_2.png`(297KB)，
     稿子里 `![](…)` 一个都没有，四张图全写成「图1（照片位置）」。
     模型为什么会那样写？因为它知道**就算写了也显示不出来** —— 稿子在
     `white-space:pre-wrap` 里当纯文本显示，`![](x.png)` 只是一串括号。
     于是"要求照片"和"能显示照片"之间缺了一整段路，它只能退回占位符。

     这里把路补上：`![](相对路径)` 经 `/api/artifact?id=<run>&path=<相对路径>`
     取字节（这个端点本来就有，产出物清单一直用它）。
     绝对路径 / http(s) / data: 一律原样放行（外链或已可用的地址，不要改写）。 */
function manuText(t, rid) {
  const run = rid || (DESK && DESK.runId) || (CUR && CUR.id) || "";
  if (!run) return String(t == null ? "" : t);
  return String(t).replace(
    /!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,
    (m, alt, src) => {
      if (/^(https?:|data:|\/)/i.test(src)) return m;
      /* 稿子里的路径可能带 ./ 前缀、或反斜杠（Windows 上写出来的） */
      const rel = String(src).replace(/\\/g, "/").replace(/^\.\//, "");
      return "![" + alt + "](/api/artifact?id=" + encodeURIComponent(run) +
        "&path=" + encodeURIComponent(rel) + ")";
    });
}
/* 证据卡：把"门通过了"翻译成"你自己能看的东西"。
   这是本轮的第二个关键改动 —— 内核给了 result_preview 与 anchors，
   于是"它到底查到了什么"不再是一串哈希。 */
/* 过程组渲染 —— 结构照 `ProcessGroupHeader` 的 jsx 一层不差。
   一层组 = 一段连续同类工作；组头是合并标题（如「已读取图片并执行了命令」），
   点开才是里面每一次调用。★ 这是用户截图里那种行的正身。 */
function stepGroupHtml(groups, id) {
  if (!groups.length) return '<div class="none">这次没有工具调用记录。</div>';
  return groups.map((g, gi) => {
    const n = g.items.length;
    /* 组头：图标叠箭头（leading）+ 合并标题（label） */
    const head = '<button class="spgh" type="button" aria-expanded="false"' +
      ' aria-controls="' + id + '-spb-' + gi + '"' +
      ' data-act="spgtog" data-gi="' + gi + '"' +
      ' data-process-activity="' + esc(g.kind) + '">' +
      '<span class="leading" aria-hidden="true">' +
        '<span class="activityIcon" data-step-process-icon="1">' +
          spIcon(g.kind) + "</span>" +
        '<span class="chevron" data-step-process-chevron="1">' +
          '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor"' +
          ' stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
          '<path d="M4 6.4L8 10.2l4-3.8"/></svg>' +
        "</span>" +
      "</span>" +
      '<span class="label">' + esc(g.title) + "</span></button>";
    /* 组成员：一次调用一行，照 GenericCommandCard 的形状 */
    const body = g.items.map(it => {
      const x = it.x;
      const b = resultBody(x);
      const nA = x.anchors ? Object.values(x.anchors).reduce((s, a) => s + a.length, 0) : 0;
      const cnt = (x.result_chars != null && x.result_truncated)
        ? "预览 " + (x.result_preview || "").length + "/" + (x.result_orig_chars || x.result_chars) + " 字"
        : (x.result_chars != null ? x.result_chars + " 字" : x.digest || "");
      const seq = x.seq != null ? "#" + x.seq : "";
      const at = x.ts ? String(x.ts).slice(11, 19) : "";
      return '<div class="spi">' +
        '<span class="ti">' + esc(humanTool(x.name)) + "</span>" +
        '<span class="sep"></span>' +
        '<span class="sm"' + (x.ok ? "" : ' data-error="1"') + ">" +
          esc(toolSummary({ args: x.args }) || "—") + "</span>" +
        (nA ? '<span class="mt">' + nA + " 锚点</span>" : "") +
        ((seq || at) ? '<span class="mt seqc"' +
          (nA ? ' style="margin-left:8px"' : "") + ">" +
          esc([seq, at].filter(Boolean).join(" · ")) + "</span>" : "") +
        "</div>" +
        '<div class="evb" hidden><div class="lab">它返回了什么' +
          (x.result_truncated ? "（预览，非全文）" : "") + "</div>" +
          "<pre>" + esc(b.txt.slice(0, 4000)) + "</pre>" +
          (nA ? '<div class="lab">可逐字核对的锚点</div>' + anchorHtml(x.anchors)
              : '<div class="none">这条结果里没有可作锚点的 DOI / 网址 / 数值。</div>') +
        "</div>";
    }).join("");
    return '<div class="spg">' + head +
      '<div class="spgb" id="' + id + "-spb-" + gi + '" hidden>' + body + "</div></div>";
  }).join("");
}

function evidenceCards(res, id) {
  /* 照 DSH：先按 activity() 分类、再按 processTitle() 合并成组头。
     ★ 以前这里是「一条调用 = 一行」，40 行铺下来就是用户说的"划半天"。
       DSH 的解法不是折叠，是**合并**：9 次调用可以只占 2 行组头。 */
  return stepGroupHtml(stepGroups(res.trace), id);
}

function evidenceCardsLegacy(res, id) {
  const tr = (res.trace || []).slice().reverse();
  if (!tr.length) return '<div class="none">这次没有工具调用记录。</div>';
  const shown = tr.slice(0, 40);
  return shown.map(x => {
    const b = resultBody(x);
    const nA = x.anchors ? Object.values(x.anchors).reduce((s, a) => s + a.length, 0) : 0;
    const cnt = (x.result_chars != null && x.result_truncated)
      ? "预览 " + (x.result_preview || "").length + "/" + (x.result_orig_chars || x.result_chars) + " 字"
      : (x.result_chars != null ? x.result_chars + " 字" : x.digest || "");
    /* ★ 序号与时刻：同一工具、同一文件可能被调两次（实测这次 `write_file`
       连着写了两次同一个 .md —— 第一次 seq=8「已新建 0→5420 字节」，
       第二次 seq=9「已覆盖 5420→5398 字节，全文 1876 字符」）。
       后端本来就有 seq/ts，界面以前不显示，于是两行长得一模一样，
       读起来像"同一个动作被记了两遍"——用户没法判断哪次才是最终的。
       序号是**这次运行的执行顺序**，摆出来就能自己排；时刻顺便带上，
       因为"隔了 20 秒又改了一遍"本身是重要信息。 */
    const seq = x.seq != null ? "#" + x.seq : "";
    const at = x.ts ? String(x.ts).slice(11, 19) : "";
    const meta = [seq, at].filter(Boolean).join(" · ");
    return '<div class="ev" data-open="0">' +
      '<div class="evh"><span class="en">' + esc(humanTool(x.name)) + "</span>" +
        (meta ? '<span class="eseq">' + esc(meta) + "</span>" : "") +
        '<span class="es">' + esc(toolSummary({ args: x.args }) || "—") + "</span>" +
        (nA ? '<span class="pill biz">' + nA + " 个锚点</span>" : "") +
        (x.ok ? "" : '<span class="pill err">失败</span>') +
        '<span class="ec">' + esc(cnt) + "</span></div>" +
      '<div class="evb"><div class="lab">它返回了什么' +
        (x.result_truncated ? "（预览，非全文）" : "") + "</div>" +
        "<pre>" + esc(b.txt.slice(0, 4000)) + "</pre>" +
        (nA ? '<div class="lab">可逐字核对的锚点</div>' + anchorHtml(x.anchors)
            : '<div class="none">这条结果里没有可作锚点的 DOI / 网址 / 数值。</div>') +
      "</div></div>";
  }).join("") + (tr.length > shown.length
    ? '<div class="none">另有 ' + (tr.length - shown.length) + " 次调用未列出（按时间倒序取最近 40 条）。</div>"
    : "");
}

/* DSH 的 `transcriptView`：工作步骤展示档位。
   真源 `dsh-client-ui-chat/lib/index.js` 的 `TRANSCRIPT_VIEW_MODES`
   与 `DEFAULT_TRANSCRIPT_VIEW_MODE = "detailed"`。
   荣珂把它存在 localStorage，键名照 DSH 的 `ui-chat.transcriptView`。 */
const TRANSCRIPT_VIEWS = ["compact", "standard", "detailed", "verbose"];
const TV_KEY = "rongke.transcriptView";
function transcriptView() {
  let v = "";
  try { v = localStorage.getItem(TV_KEY) || ""; } catch (e) { v = ""; }
  return TRANSCRIPT_VIEWS.indexOf(v) >= 0 ? v : "detailed";   // DSH 默认 detailed
}
function setTranscriptView(v) {
  if (TRANSCRIPT_VIEWS.indexOf(v) < 0) v = "detailed";
  try { localStorage.setItem(TV_KEY, v); } catch (e) { /* 隐身模式下写不进去，不致命 */ }
  // 展示档位是全局偏好：历史轮与当前轮一起换，不能只改最后一轮。
  document.querySelectorAll(".rescard .res").forEach(r => { r.dataset.view = v; });
}
function renderResult(res, host) {
  host = host || TURN && TURN.res || $("#resultCard");
  if (!host) return;
  const isCurrent = !TURN || (!!TURN.current && host === TURN.res);
  if (res.error) {
    host.innerHTML =
      '<div class="res bad">' +
      '<div class="sec"><div class="sum">这次没能跑完</div></div>' +
      '<div class="sec"><h4>发生了什么</h4><pre>' + esc(String(res.error).slice(0, 2000)) + "</pre>" +
      '<div class="da"><button class="mini pri" data-act="editgoal">修改需求</button>' +
      '<button class="mini" data-act="openproc">查看执行记录</button></div></div></div>';
    wireActs(host, res);
    return;
  }
  const ok = !!res.success;
  const u = res.usage || {};
  const steps = res.steps || 0;
  /* 用时取哪一份都行，但**不能靠 T0 硬算** —— 回放时 T0 是刚刚设的，
     硬算会得到 "0s"，把一次跑了 90 秒的任务说成瞬间完成（假数）。
     优先用归档里记下的墙钟（telemetry.elapsed_ms 是"当时值"）。 */
  const wallMs = (res.telemetry && res.telemetry.elapsed_ms)
    ? res.telemetry.elapsed_ms : (T0 ? Date.now() - T0 : 0);
  const lint = res.lint, m = (lint && lint.metrics) || {};
  const headline = ok
    ? (res.final ? res.final : "做完了")
    : (res.reason || "没做完");
  const wsId = res.run_id || (CUR && CUR.id) || "";

  /* ★ 这里原来还有一条「已完成，用时 …」的横条（.rh）。
     它和过程组的组头说的是同一件事 —— 两套过程容器。DSH 只有组头那一行，
     所以横条整条删掉；步骤数/工具数/稿面分仍留在 data-* 属性里可查。 */
  let h = '<div class="res ' + (ok ? "ok" : "bad") + '" data-tech="0"' +
    ' data-view="' + esc(transcriptView()) + '"' +
    ' data-turn-process-steps="' + steps + '"' +
    ' data-lint-score="' + (lint && lint.score != null ? lint.score : "") + '"' +
    ' data-turn-process-tools="' + ((res.trace || []).length) + '">' +
    '<div class="sec"><div class="sum">' + esc(headline) + "</div></div>";

  // 交付正文 —— **不折叠**（DSH 的正文永不折叠，折叠的是过程）
  if (res.draft) {
    /* 只认"出过正文的那一次"。失败的任务**不覆盖**它 ——
       否则一次跑挂就把你正在读的稿子换成一页空，等于把书从手里抽走。
       回放的历史轮不写 LAST_RESULT：稿件台里的"当前稿"永远是最后一轮。 */
    if (isCurrent) LAST_RESULT = res;
    h += '<div class="sec">' +
      '<div class="manu" data-r="manu">' + md(manuText(res.draft, res.run_id)) + "</div>" +
      '<div class="da"><button class="mini" data-r="deskRead" title="把正文摊到右边整面墙上看：带目录、查找、字号">读全稿 ▤</button>' +
        '<button class="mini" data-r="cpDraft">复制正文</button>' +
        '<button class="mini" data-act="dlmd">下载为 .md</button></div>' +
      lintCard(lint, false) + "</div>";
  } else if (lint) {
    h += '<div class="sec">' + lintCard(lint, false) + "</div>";
  }

  // 产出物（异步填）
  h += '<div class="sec"><h4>产出物<span class="n" data-r="artNote"></span></h4>' +
       '<div data-r="artifactBody"><div class="none">正在列工作区…</div></div></div>';

  // 要你定夺（先按已知信息渲染；产出物到位后再刷一次）
  h += '<div class="sec" data-r="decideSec"><h4>下一步你可以</h4>' +
       '<div data-r="decideBody"></div>' +
       '<div class="da" data-r="decideActs"></div></div>';

  // 可核验的证据
  h += '<div class="sec"><h4>证据' +
       '<span class="n">这次调用了哪些工具</span></h4>' +
       '<div data-r="verifBody">' + evidenceCards(res, wsId) + "</div></div>";

  // 技术细节（内部量都收在这里）
  h += '<button class="techbtn" data-r="techBtn"><span class="chev">▶</span>技术细节（门判定 · 用量 · 成本口径 · 工作区）</button>' +
    '<div class="tech sec">' +
    '<div class="kv"><b>结论</b><span>' + esc(res.reason || "") + "</span></div>" +
    '<div class="kv"><b>用量</b><span>输入 ' + (u.input_tokens || 0).toLocaleString() +
      " · 输出 " + (u.output_tokens || 0) + " · 思考 " + (u.reasoning_tokens || 0) +
      " · 缓存读 " + (u.cache_read || 0) +
      (res.cache_ratio ? "（命中 " + Math.round(res.cache_ratio * 100) + "%）" : "") + "</span></div>" +
    '<div class="kv"><b>成本</b><span>' + esc(costText(res)) +
      (costText(res) === "UNKNOWN" ? ' <span class="hint">价格表未填（python -m rongke --pricing）</span>' : "") +
      "</span></div>" +
    (res.telemetry ? '<div class="kv"><b>耗时</b><span>模型 ' + fmtMs(res.telemetry.llm_ms_total || 0) +
      "（" + (res.telemetry.llm_calls || 0) + " 次）· 工具 " + fmtMs(res.telemetry.tool_ms_total || 0) +
      "（" + (res.telemetry.tool_spans || 0) + " 次）· 墙钟 " + fmtMs(res.telemetry.elapsed_ms || 0) + "</span></div>" : "") +
    '<div class="kv"><b>工作区</b><span class="m" style="font-family:var(--mono);font-size:var(--fs-xxs)">' +
      esc(res.workspace || "") + "</span></div>" +
    (res.verdict ? '<div class="kv"><b>门判定</b><span>通过 · 证据 ' +
      ((res.verdict.evidence || []).length) + " 条</span></div>" : "") +
    "</div></div>";

  host.innerHTML = h;

  // ── 交互接线（全部按 host 作用域，历史轮与当前轮不会互相点错）──
  wireLint(host);
  const cp = host.querySelector('[data-r="cpDraft"]');
  if (cp) cp.onclick = () => copyText(res.draft || "");
  const rd = host.querySelector('[data-r="deskRead"]');
  if (rd) rd.onclick = () => openDesk(Object.assign(deskPayload(res), { restore: true }));
  const tb = host.querySelector('[data-r="techBtn"]');
  if (tb) tb.onclick = () => {
    const r = host.querySelector(".res");
    r.dataset.tech = r.dataset.tech === "1" ? "0" : "1";
  };
  wireActs(host, res);
  if (wsId) loadArtifacts(wsId, res, host);
  else paintDecide(host, res, null);
  if (isCurrent) deskSync();   // 面板正开着就换上新稿；没开就别动它
}
function wireActs(host, res) {
  host.querySelectorAll("[data-act]").forEach(b => {
    b.onclick = () => {
      const a = b.dataset.act;
      if (a === "openproc") {
        /* 展开**这张卡所属那一轮**的过程组，不是全局的「当前轮」 */
        const turn = b.closest(".turn.ai");
        const p = turn && turn.querySelector(".aproc");
        if (p) {
          p.dataset.open = "1";
          const h = p.querySelector(".aproc-h");
          if (h) h.setAttribute("aria-expanded", "true");
        }
        goView("chat");
      }
      else if (a === "dlmd") downloadText((res.draft || "") , "rongke-draft.md");
      else if (a === "resume") {
        // 接不上时必须**把原因端给用户**（409 的 error 是服务端写的人话），
        // 而不是静静地把按钮变回原样 —— 那会让人以为是网络抽风，然后反复点。
        const was = b.textContent;
        b.disabled = true; b.textContent = "正在接上…";
        fetch("/api/resume", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: CUR.id }),
        }).then(async r => {
          const j = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
          $("#hint").innerHTML =
            '<span style="color:var(--ok-fg)">接着上次继续 —— 前情已接回，不重来</span>';
          replay(j.id);
        }).catch(e => {
          b.disabled = false; b.textContent = was;
          $("#hint").innerHTML = '<span style="color:var(--warn)">接不上：' +
            esc(e.message) + "</span>";
        });
      }
      else if (a === "rerun2x") {
        const cur = MAXSTEPS || (STATUS.loop && STATUS.loop.max_steps) || 40;
        const nx = Math.max(cur * 2, (res.steps || 0) + 10);
        send(null, nx);
      } else if (a === "editgoal") {
        $("#goal").value = LASTGOAL || "";
        autoGrow(); $("#goal").focus();
        $("#hint").innerHTML = '<span style="color:var(--warn)">把要改的那一条改掉再发 —— 同一句话重跑只会得到同一个结果</span>';
      }
    };
  });
}
function paintDecide(host, res, arts) {
  const db = host.querySelector('[data-r="decideBody"]'),
        da = host.querySelector('[data-r="decideActs"]');
  if (!db) return;
  const ds = decisions(res, arts);
  if (!ds.length) {
    /* ★ 没有要用户决定的事时，**整块收起来**，不写"没有需要你决定的事"。
       2026-10-01 用户问「为什么非得给用户看他不需要看的呢」——
       一整块在说"没有东西"，就是纯粹的噪音：既没信息，又占位置，
       还把真正有内容的「证据」往下挤。真有事才出现。
       （这也和 DSH 右栏一致：DSH 的面板不留空壳，没内容就不占位。） */
    const sec = host.querySelector('[data-r="decideSec"]');
    if (sec) sec.hidden = true;
    da.innerHTML = "";
    return;
  }
  const sec2 = host.querySelector('[data-r="decideSec"]');
  if (sec2) sec2.hidden = false;
  db.innerHTML = ds.map(x =>
    '<div class="dec"><span class="di">' + x.ic + '</span><div class="db">' +
      '<div class="dt">' + esc(x.t) + "</div>" +
      (x.d ? '<div class="dd">' + esc(x.d) + "</div>" : "") +
      actsHtml(x.a) + "</div></div>").join("");
  da.innerHTML = "";
}

/* 把服务端给的绝对工作区路径 + 清单里的相对路径拼成绝对路径。
   只做拼接与分隔符归一，**不做安全判断** —— 判断在服务端（exists + 类型白名单）。 */
function joinAbs(root, rel) {
  const a = String(root || "").replace(/[\\/]+$/, "");
  const b = String(rel || "").replace(/^[\\/]+/, "").replace(/\//g, "\\");
  return a ? a + "\\" + b : b;
}

async function loadArtifacts(id, res, host) {
  host = host || TURN && TURN.res;
  if (!host) return;
  const body = host.querySelector('[data-r="artifactBody"]'),
        note = host.querySelector('[data-r="artNote"]');
  if (!body) return;
  let d;
  try { d = await api("/api/artifacts?id=" + encodeURIComponent(id)); }
  catch (e) { body.innerHTML = '<div class="none">列产出物失败：' + esc(e.message) + "</div>"; return; }
  const files = d.files || [];
  if (note) note.textContent = d.exists
    ? (files.length ? files.length + " 个 · 共 " + fmtSize(d.total_bytes) : "")
    : "工作区目录不存在";
  if (!files.length) {
    /* ★ 同样：工作区是空的、或目录早没了 —— 这一段是"没有东西"，
       折到「技术细节」里说（那里本来就报工作区路径），别在正文下面单开一块。
       注意**只有"没有文件"这一支**收起；真有文件时下面照常列。
       还在列的过程中（"正在列工作区…"）也不收 —— 那是"还没好"，不是"没有"。 */
    const sec = body.closest(".sec");
    if (sec) sec.hidden = true;
    body.innerHTML = "";
    paintDecide(host, res, d);
    return;
  }
  const secBack = body.closest(".sec");
  if (secBack) secBack.hidden = false;
  const TEXTLIKE = ["md","markdown","txt","json","csv","tsv","py","r","tex","bib","yaml","yml","log","html","svg"];
  /* 能"看原页"的：PDF 直接渲染，Office 经 PDF 中转。
     它们的**正确读法是看原页**，不是抽文本 —— 一份 PPT 抽成文本就不再是那份 PPT 了。 */
  const ORIGLIKE = ["pdf","pptx","ppt","odp","docx","doc","odt"];
  body.innerHTML = '<div class="files">' + files.map((f, i) => {
    const canPrev = TEXTLIKE.indexOf(f.ext) >= 0;
    const canOrig = ORIGLIKE.indexOf(f.ext) >= 0;
    const img = ["png","jpg","jpeg","gif","webp","svg"].indexOf(f.ext) >= 0;
    return '<div class="file"><span class="fi">' + fileGlyph(f.ext) + "</span>" +
      '<span class="fn" title="' + esc(f.path) + '">' + esc(f.path) + "</span>" +
      '<span class="fs">' + fmtSize(f.size) + "</span>" +
      '<span class="fa">' +
        (canOrig ? '<button class="mini" data-orig="' + i +
          '" title="在右边看原件本身：论文/PPT 的原样，版式一字不改">看原页 ▤</button>' : "") +
        (canPrev ? '<button class="mini" data-desk="' + i +
          '" title="摊到右边整面墙上看：不截断，带目录、查找、字号">读全稿 ▤</button>' : "") +
        (canPrev ? '<button class="mini" data-prev="' + i + '">预览</button>' : "") +
        (img ? '<button class="mini" data-img="' + i + '">看图</button>' : "") +
        '<a class="mini" style="text-decoration:none" href="/api/artifact?id=' +
          encodeURIComponent(id) + "&path=" + encodeURIComponent(f.path) + '&dl=1">下载</a>' +
      "</span></div>" +
      '<div data-prevhost="' + i + '"></div>';
  }).join("") + "</div>";
  /* 看原页：把工作区里那份文件的**绝对路径**交给右栏去渲染。
     服务端只认绝对路径 + exists()，不会因为"列表里有它"就放行。
     路径由服务端给的 workspace 拼（那是绝对路径），不自己猜盘上结构。 */
  body.querySelectorAll("[data-orig]").forEach(b => {
    b.onclick = () => {
      const f = files[parseInt(b.dataset.orig, 10)];
      const abs = joinAbs(d.workspace, f.path);
      openOrig({ file: abs, title: f.path });
    };
  });
  /* 读全稿：内联「预览」在 2 万字符处截断（长稿只能下载了再看），
     而稿件台带目录/查找/字号，且**不截断** —— 长稿的读法就该是它。 */
  body.querySelectorAll("[data-desk]").forEach(b => {
    b.onclick = () => deskReadFile(id, files[parseInt(b.dataset.desk, 10)]);
  });
  body.querySelectorAll("[data-prev]").forEach(b => {
    b.onclick = async () => {
      const f = files[parseInt(b.dataset.prev, 10)];
      const host = body.querySelector('[data-prevhost="' + b.dataset.prev + '"]');
      if (host.innerHTML) { host.innerHTML = ""; return; }
      host.innerHTML = '<div class="none">读取中…</div>';
      try {
        const r = await fetch("/api/artifact?id=" + encodeURIComponent(id) +
                              "&path=" + encodeURIComponent(f.path));
        const t = await r.text();
        const cut = t.length > 20000;
        host.innerHTML = '<div class="fileprev">' + esc(t.slice(0, 20000)) +
          (cut ? "\n\n……（预览截断，共 " + t.length + " 字符；完整内容请下载）" : "") + "</div>";
      } catch (e) { host.innerHTML = '<div class="none">读取失败：' + esc(e.message) + "</div>"; }
    };
  });
  body.querySelectorAll("[data-img]").forEach(b => {
    b.onclick = () => {
      const f = files[parseInt(b.dataset.img, 10)];
      const host = body.querySelector('[data-prevhost="' + b.dataset.img + '"]');
      if (host.innerHTML) { host.innerHTML = ""; return; }
      host.innerHTML = '<div class="fileprev"><img style="max-width:100%" src="/api/artifact?id=' +
        encodeURIComponent(id) + "&path=" + encodeURIComponent(f.path) + '" alt=""></div>';
    };
  });
  paintDecide(host, res, d);
}
function fileGlyph(ext) {
  if (["png","jpg","jpeg","gif","webp","svg","pdf","tif","tiff"].indexOf(ext) >= 0) return "▣";
  if (["md","markdown","txt","docx","doc","tex","bib"].indexOf(ext) >= 0) return "▤";
  if (["csv","tsv","xlsx","xls"].indexOf(ext) >= 0) return "▦";
  if (["json","yaml","yml","py","r","js"].indexOf(ext) >= 0) return "⌗";
  return "◻";
}
function downloadText(text, name) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}
/* ── W14-E · DSH Toast（.toast）：全站唯一的「短暂回执」改穿新皮 ──
   它接的是本页**已有**的那句临时反馈：原来它把按钮文字换成「已复制」1200ms 再换回来，
   现在是同一句话、同一个触发（复制成功）、同样会出现也会消失，只是形状换成 DSH 的 toast。
   ★ 卸载计时**从 CSS 的 --toast-hold 读**，不在 JS 里再写一份 3000 ——
     两份时长迟早会漂（B10 的注释就点了这件事）。
   ★ 为什么只接这一件、不接 #hint：#hint 是常驻状态行（跑任务全程都要看得见
     「荣珂在工作…」），把它换成会自动淡出的浮层是**改行为**，不是换皮。 */
function showToast(text) {
  document.querySelectorAll(".toast").forEach(n => n.remove());   // 连点两次不叠成一块
  document.body.insertAdjacentHTML("beforeend",
    '<div class="toast" role="status">' +
      '<span class="ticon success" aria-hidden="true">✓</span>' +
      '<span class="ttext">' + esc(text) + "</span></div>");
  const n = $(".toast");
  const hold = parseFloat(getComputedStyle(n).getPropertyValue("--toast-hold")) || 3000;
  setTimeout(() => { n.remove(); }, hold + 1000);   // +1000 = CSS 里那段淡出
  return n;
}
function copyText(t) {
  const done = () => showToast("已复制");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done).catch(() => fallbackCopy(t, done));
  } else fallbackCopy(t, done);
}
function fallbackCopy(t, done) {
  const a = document.createElement("textarea");
  a.value = t; document.body.appendChild(a); a.select();
  try { document.execCommand("copy"); done(); } catch (e) {}
  document.body.removeChild(a);
}
/* W14-C · 代码卡 / 终端卡的复制走事件委托：这两类卡是 md()/过程流现拼的，
   逐个挂 onclick 等于给每个拼装点加一份会漂的接线 —— 一个委托吃全部。 */
/* ═══ 过程组的两级展开 —— 事件委托，只装一次 ═══════════════════════════════
   对照 DSH `ProcessGroupHeader` 的 onClick（toggle + focus）与
   `GenericCommandCard` 的展开。挂 document 是因为同一份过程组会在
   结果卡 `#verifBody` 和右栏 `#trajBody` 两处各渲染一遍。 */
document.addEventListener("click", e => {
  const h = e.target.closest(".spgh");
  if (h) {
    const open = h.getAttribute("aria-expanded") === "true";
    h.setAttribute("aria-expanded", open ? "false" : "true");
    const bd = document.getElementById(h.getAttribute("aria-controls"));
    if (bd) bd.hidden = open;
    return;
  }
  const sp = e.target.closest(".spi");
  if (sp) {
    const bx = sp.nextElementSibling;
    if (bx && bx.classList.contains("evb")) bx.hidden = !bx.hidden;
  }
});

document.addEventListener("click", e => {
  const b = e.target.closest("[data-ccopy],[data-tcopy]");
  if (!b) return;
  let t = "";
  if (b.hasAttribute("data-ccopy")) {
    const src = b.closest(".ccard") && b.closest(".ccard").querySelector(".ccard-b");
    t = src ? (src.innerText != null ? src.innerText : src.textContent) : "";
  } else {
    const blk = b.closest(".tblk");
    if (blk) {
      const cmd = blk.querySelector(".tblk-cmd .txt");
      const out = blk.querySelector(".tblk-o .txt");
      t = [cmd && cmd.textContent, out && out.textContent].filter(Boolean).join("\n");
    }
  }
  if (t) copyText(t);
});

/* ═════════════ 轨迹页 ═════════════ */
let TRAJ = { contract:null, result:null };
function renderTrajContract(c) { TRAJ.contract = c; paintTraj(); }
function renderTrajResult(res) { TRAJ.result = res; paintTraj(); loadStatus(); }
function paintTraj() {
  const c = TRAJ.contract, r = TRAJ.result;
  /* ★ 这一页只显示**当前选中的那一次**。原来的空白态写「还没有任务」
     —— 而语料里有 64 条历史、运行过 83 次。那是「没选」，不是「没有」。 */
  if (!c && !r) { $("#trajBody").innerHTML = '<div class="blank">' +
    "这一页看的是<b>某一次</b>任务的每一步。<br>" +
    "在左边「历史」里点一条，这里就会列出它当时的目标契约、" +
    "每一步调了什么、拿到什么。</div>"; return; }
  let h = "";
  if (c) {
    const sec = (t, items, cls) => (!items || !items.length) ? "" :
      '<div class="' + (cls || "") + '"><h4>' + t + "</h4><ul>" +
      items.map(x => "<li>" + esc(x) + "</li>").join("") + "</ul></div>";
    h += '<div class="card"><h3>目标契约<span class="hint">这次任务开始时定下的交付与验收</span></h3><div class="inner">' +
      '<div class="contract">' +
      '<div class="full"><h4>用户原话</h4><div style="font-size:var(--fs-xs)">' + esc(c.raw_goal) + "</div></div>" +
      (c.goal && c.goal !== c.raw_goal ? '<div class="full"><h4>目标重述</h4><div style="font-size:var(--fs-xs);color:var(--fg-2)">' + esc(c.goal) + "</div></div>" : "") +
      (c.target_chars ? '<div class="full"><h4>目标字数</h4><div style="font-size:var(--fs-xs);color:var(--fg-2)">约 ' + c.target_chars + " 字（从用户原话抽取）</div></div>" : "") +
      sec("必须交付", c.deliverables) +
      sec("验收标准（每条都要有证据）", c.acceptance, "acc") +
      sec("约束", c.constraints) +
      sec("禁止", c.forbidden, "no") +
      (c.stages && c.stages.length ? sec("阶段技能（它强制要求）", c.stages) : "") +
      "</div></div></div>";
  }
  if (r) {
    const u = r.usage || {}, tr = r.trace || [];
    h += '<div class="card"><h3>这一次的账</h3><div class="inner">' +
      '<div class="kv"><b>结论</b><span>' + (r.success ? '<span class="pill ok">完成</span> ' : '<span class="pill err">未完成</span> ') + esc(r.reason || "") + "</span></div>" +
      '<div class="kv"><b>步数</b><span>' + r.steps + " 步 · 工具调用 " + tr.length + " 次（" + tr.filter(x => x.ok).length + " 成功）</span></div>" +
      '<div class="kv"><b>用量</b><span>输入 ' + (u.input_tokens || 0).toLocaleString() + " · 输出 " + (u.output_tokens || 0) +
        " · 思考 " + (u.reasoning_tokens || 0) + " · 缓存读 " + (u.cache_read || 0) +
        (r.cache_ratio ? "（命中 " + Math.round(r.cache_ratio * 100) + "%）" : "") + "</span></div>" +
      '<div class="kv"><b>成本</b><span>' + esc(costText(r)) +
        (costText(r) === "UNKNOWN"
          ? ' <span class="hint">价格表未填（python -m rongke --pricing）</span>' : "") + "</span></div>" +
      (r.telemetry ? '<div class="kv"><b>耗时</b><span>' +
        "模型 " + fmtMs(r.telemetry.llm_ms_total || 0) +
        "（" + (r.telemetry.llm_calls || 0) + " 次调用）· 工具 " + fmtMs(r.telemetry.tool_ms_total || 0) +
        "（" + (r.telemetry.tool_spans || 0) + " 次）· 墙钟 " + fmtMs(r.telemetry.elapsed_ms || 0) +
        (r.telemetry.uncovered_pct ? ' <span class="hint">未埋点 ' + r.telemetry.uncovered_pct + "%</span>" : "") +
        "</span></div>" : "") +
      (r.lint ? '<div class="kv"><b>稿面</b><span>' +
        (r.lint.score != null ? "评分 " + r.lint.score + "/100" : "") +
        (r.lint.hard ? " · 必修 " + r.lint.hard : "") + (r.lint.soft ? " · 建议 " + r.lint.soft : "") + "</span></div>" : "") +
      (r.workspace ? '<div class="kv"><b>工作区</b><span class="m" style="font-family:var(--mono);font-size:var(--fs-xxs)">' + esc(r.workspace) + "</span></div>" : "") +
      "</div></div>";
    if (r.lint) { h += '<div class="card"><h3>写作体检<span class="hint">它程序化核验，不评判论证</span></h3><div class="inner">' + lintCard(r.lint, true) + "</div></div>"; }
    if (tr.length) {
      h += '<div class="card"><h3>证据链<span class="hint">都是真实调用，不是模型自述；已按时间倒序</span></h3><div class="inner">' +
        evidenceCards(r, r.run_id) + "</div></div>";
    }
  }
  $("#trajBody").innerHTML = h;
  wireLint($("#trajBody"));
  $("#trajBody").querySelectorAll(".evh").forEach(hh => {
    hh.onclick = () => {
      const p = hh.parentElement;
      p.dataset.open = p.dataset.open === "1" ? "0" : "1";
    };
  });
}

/* ═════════════ 工具 ═════════════ */
let TOOLS = [];
async function loadTools() {
  /* ★ 这里原来是**裸 `api()`** —— 全站唯一没走 `pageGet` 的页面。
     后果和"没超时"一模一样：/api/tools 挂住（不报错、不应答）时，
     这一页永远停在「读取中…」，看起来在努力实际已死，刷新也一样。
     改用 `pageGet` 就同时拿到三件事：6 秒死线、非 JSON 的兜底、
     以及统一的 `{error, hint}` 形状 —— 和另外九页同一个口径。 */
  const d = await pageGet("/api/tools");
  if (d.error && !Array.isArray(d.tools)) {
    $("#toolsBody").innerHTML =
      '<div class="blank">工具清单没取回来。<br>' + esc(d.error) +
      (d.hint ? '<br><span style="color:var(--fg-3)">' + esc(d.hint) + "</span>" : "") +
      '<br><button class="mini" id="toolsRetry">重试</button></div>';
    const rt = $("#toolsRetry");
    if (rt) rt.onclick = () => { $("#toolsBody").innerHTML = '<div class="blank">读取中…</div>'; loadTools(); };
    return;
  }
  TOOLS = d.tools || [];
  $("#toolsHint").textContent = TOOLS.length + " 个";
  paintTools();
}
/* W18-K · 工具说明是在仓库里用 Markdown 写的（55 个里 31 个带标记），
   而这一列原来是 `esc(...)` 直接上屏 —— 于是 `**粗体**` 和反引号**被端给了用户**，
   跟"标题行里出现实现细节"是同一族毛病：写给另一种读者看的东西没转过来。
   ★ 两件事一起修：
     1) `rkmd()` 渲染行内 Markdown。**先 `esc()` 再替换** —— 替换只认已经转义过的文本，
        所以插进去的标签不带来注入面（顺序反了才是漏洞）。
     2) 原来还有 `.slice(0,120)` 硬裁、**不给省略号**，最长的一条 166 字就是这么被
        拦腰截断的（实测 55 条里只有 4 条超过 120）。改成 300 字才裁、且补 `…`
        并把**全文放进 `title`** —— 事实一个字都不丢，只是挪了个地方（与 W18-F 同一条原则）。 */
function rkmd(s) {
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
function toolDesc(t) {
  const full = (t.description || "").split("\n")[0];
  const cut = full.length > 300;
  return '<span' + (cut ? ' title="' + esc(full) + '"' : "") + ">" +
    rkmd(cut ? full.slice(0, 300) + "…" : full) + "</span>";
}
function paintTools() {
  const q = ($("#toolsFilt").value || "").trim().toLowerCase();
  const ts = !q ? TOOLS : TOOLS.filter(t =>
    (t.name + " " + t.description + " " + (t.params || []).join(",")).toLowerCase().includes(q));
  $("#toolsBody").innerHTML = ts.length ? '<table class="grid"><thead><tr>' +
    "<th style='width:180px'>工具</th><th style='width:180px'>参数</th><th style='width:74px'>来源</th><th>说明</th>" +
    "</tr></thead><tbody>" + ts.map(t => "<tr><td><b>" + esc(t.name) + "</b>" +
      (t.mutating ? ' <span class="tag" data-tone="warning">W</span>' : "") + "</td>" +
      "<td class='m' style='white-space:normal'>" + esc((t.params || []).join(", ") || "—") + "</td>" +
      "<td><span class='tag' data-tone=\"" + (t.source === "workbench" ? "info" : "neutral") + "\">" +
      esc(t.source) + "</span></td>" +
      "<td style='color:var(--fg-3)'>" + toolDesc(t) + "</td></tr>").join("") +
    "</tbody></table>" : '<div class="blank">没有匹配的工具</div>';
}

/* ═════════════ W2 四页：能力 / 门禁 / 交付物 / 图表 ═════════════
   四条纪律，都是照知识库页与工具页的既有做法，没有新发明：
   1) 内容一律由 JS 现填 —— HTML 里不写第二份（`ui_check` 的「id 唯一」会拦重复 id，
      而且写死的那份一定会和接口给的数据漂移）。
   2) 失败只有一份"原始诊断怎么渲染"：`kbFailBody()` —— `kbFail()` 把它铺在首屏，
      `pageFail()` 把它折进详情。再写一份"失败长什么样"就是第二套真相。
   3) `null` / 空 / 读不到 是**三种不同的事实**，各说各话：
      把"读不到"渲染成"没有"，人会去重做已经做完的事；
      把"没跑过"渲染成"全过"，那是给自己发合格证。
   4) 取回产出物只走 `/api/artifact` 那一条围栏通路；读正文只用稿件台。 */

/* 与 `api()` 的分工：`api()` 抛错时只带出响应体里的 `error`，
   而同一份 JSON 里那句 `hint`（"检查 rongke/gate.py 是否完好"之类）被丢在抛出路上。
   新四页要的恰好是"原样显示服务端原文 + 怎么修"，所以走这一条读法。
   它不改变 `api()` 的任何行为 —— 老页面照旧。 */
/* `ms` 是按调用点可调的死线（默认 6000）。
   ★ 为什么要能调：`/api/setup` 实测 3134~5511ms，对 6000ms 死线
   只有 **1.09 倍** 余量—— 而首次装的人一定会走这一页。
   让它在正常机器上随机变成红色错误，比“没有超时”更糟。
   其余八页实测都在 600ms 以内，6 秒已经是 10 倍余量。 */
async function pageGet(p, ms) {
  const LIMIT = ms || 6000;
  let r, t;
  /* `cache:"no-store"`：这些接口全是**现算的当前状态**（环境体检、
     授权、账单…）。让浏览器缓存它们，就会出现"改了、刷新一下才对"
     这种最难查的一类 bug —— 实测已经撞到一次：激活成功而界面仍写
     "已到期"，刷新后正常。 */
  /* ★ 死线：这一处改动覆盖**九个页面**。原来是裸 fetch，没有超时：
     服务端只要有一条路由挂住（不报错、不应答），那个页面就**永远**
     停在「读取中…」—— 看起来在努力，实际已经死了，刷新也一样。
     超时仍返回 `{error, hint}` 这个**既有形状** —— 各页的错误分支直接接手。 */
  const AC = typeof AbortController === "function" ? new AbortController() : null;
  const cut = AC ? setTimeout(() => AC.abort(), LIMIT) : 0;
  try { r = await fetch(p, { cache: "no-store", signal: AC ? AC.signal : undefined }); t = await r.text(); }
  catch (e) {
    return (e && e.name === "AbortError")
      ? { error: "这一页的数据 " + Math.round(LIMIT / 1000) + " 秒内没取回来。",
          hint: "服务端还在，只是这条路由这一次没答上 —— "
                + "点一下导航重进这页再试；一直如此就看控制台的启动日志" }
      : { error: "接口没有应答：" + (e && e.message ? e.message : e),
          hint: "控制台服务端可能没在跑 —— 重启它：python -m rongke --web" };
  }
  finally { if (cut) clearTimeout(cut); }
  let j;
  try { j = JSON.parse(t); }
  catch (e) { return { error: "这条路由返回的不是 JSON：" + String(t).slice(0, 200),
                       hint: "服务端可能被改动过 —— 先看控制台的启动日志" }; }
  if (!r.ok) return { error: String(j.error || ("HTTP " + r.status)), hint: j.hint };
  return j;
}
/* 服务端给了 `errors[]`（个别任务目录没读全）时，列表照出、但要说 —— 沉默=假装完整。 */
function partialNote(d) {
  const es = (d && d.errors) || [];
  if (!es.length) return "";
  return '<div class="kbwarn"><b>有 ' + es.length + " 条没读全：</b>" +
    es.slice(0, 5).map(x => esc(String(x))).join("<br>") +
    (es.length > 5 ? "<br>……还有 " + (es.length - 5) + " 条" : "") +
    (d.hint ? "<br>怎么修：" + esc(String(d.hint)) : "") + "</div>";
}
function humanView(x) {          // "view:traj" → 那个视图自己的名字（VIEWS 是唯一来源）
  const id = String(x || "").replace(/^view:/, "");
  return (VIEWS[id] || [id])[0];
}

/* ── 能力 ── */
async function loadCap() {
  const d = await pageGet("/api/capabilities");
  if (d.error || !Array.isArray(d.groups)) {
    $("#capStat").innerHTML = ""; $("#capHint").textContent = "";
    $("#capBody").innerHTML = pageFail("本事清单暂时读不出来",
      "这一页靠服务端告诉它有哪些本事，读不到就只能空着 —— 不影响任务本身在跑。",
      d.error ? d : {
        error: "返回里没有 groups（拿到的是：" + Object.keys(d).join(", ") + "）",
        hint: "这一页只读 /api/capabilities，不自己编清单 —— 请检查那条路由"});
    return;
  }
  const items = [];
  d.groups.forEach(g => (g.items || []).forEach(i => items.push(i)));
  const nLive = items.filter(i => i.live).length;
  const nDead = items.length - nLive;
  $("#capStat").innerHTML =
    '<div class="kbn"><b>' + items.length + "</b><span>条能力</span></div>" +
    '<div class="kbn"><b>' + nLive + "</b><span>在跑（文件核得到）</span></div>" +
    '<div class="kbn"><b>' + nDead + "</b><span>未接</span></div>" +
    '<div class="kbn"><b>' + d.groups.length + "</b><span>分组</span></div>";
  // 服务端的汇总数与逐条数必须对得上；对不上就报出来，不各说各话。
  const mism = [];
  if (d.count != null && d.count !== items.length) mism.push("条数 " + d.count + " ≠ 实列 " + items.length);
  if (d.live != null && d.live !== nLive) mism.push("在跑 " + d.live + " ≠ 实算 " + nLive);
  $("#capHint").innerHTML = mism.length ? '<span style="color:var(--err-fg)">汇总与明细不一致：' +
    esc(mism.join("；")) + "</span>" : (items.length ? "逐条现算" : "");
  $("#capBody").innerHTML = d.groups.map(g => {
    const gi = g.items || [];
    const lv = gi.filter(i => i.live).length;
    return '<div class="card"><h3>' + esc(g.name || g.id) +
      '<span class="hint">' + lv + "/" + gi.length + " 在跑</span></h3><div class='inner'>" +
      (gi.length ? gi.map(row => capRow(row)).join("") : '<div class="blank">这一组没有条目</div>') +
      "</div></div>";
  }).join("");
}
function capRow(it) {
  const wired = (it.wired || []).map(humanView);
  // 未接的原因必须挨着条目本体 —— 推到页脚等于没写。
  // ★ 2026-10-22 P3：源码路径不再当门面，收进 title 与括号里。
  //   用户原话是"看不懂写的啥"，而一个普通人看到 `rongke/contract.py` 得不到
  //   任何判断 —— 那一列真正在说的是"这条本事由哪段代码负责"，只有想深看的人要。
  //   所以：正文只留人能理解的"在哪个页露出"，路径退到 hover（title）与 `·` 之后。
  const f = (it.file || "").split("/").pop();   // contract.py
  return '<div class="caprow"><div class="capi"><b>' + esc(it.name || it.id) +
    (it.live ? ' <span class="pill ok">在跑</span>'
             : ' <span class="pill warn">未接</span>') +
    '<p>' + esc(it.digest || "") + "</p>" +
    (!it.live && it.why ? '<p class="capwhy">为什么没接：' + esc(it.why) + "</p>" : "") +
    "<p>" + (wired.length ? "已露出在：" + esc(wired.join(" · "))
                          : "只在它内部跑，界面上还没露出") + "</p>" +
    '</div><div class="capf" title="' + esc(it.file || "") + '">' + esc(f) + "</div></div>";
}

/* ── 质量把关 ── */
/* 读不出来时的两层：主行一句人话（用户只需要知道"这页暂时没数"），
   服务端原文与修法**不删、只折进详情** —— 那是排查线索，但也不该在用户面前直出。
   ★ W15-A 任务 2：原来这两句直接铺在页面上（defs 空 / docstring），像给开发者看的日志。 */
function gateReadFail(mainLine, d) {
  const raw = (d && (d.error || d.raw)) || "未知错误";
  const hint = (d && d.hint) ? String(d.hint) : "";
  return '<div class="kbwarn"><b>' + esc(mainLine) + "</b><br>" +
    "这一页暂时列不出那 9 道检查 —— 不影响任务本身在跑，" +
    "但你得等它读出来才看得到把关结果。" +
    detailRow("详情（排查用）",
      esc(String(raw).slice(0, 400)) +
      (hint ? "<br><b>怎么修：</b>" + esc(hint) : "")) +
    "</div>";
}
async function loadGate() {
  const d = await pageGet("/api/gates");
  const box = $("#gateDefs"), last = $("#gateLast");
  if (d.error && !Array.isArray(d.defs)) {        // 整条路由没成
    box.innerHTML = gateReadFail("检查表读不出来", d);
    last.innerHTML = '<div class="blank">上面那条读不到，本次的结果也读不到 —— 同一个接口</div>';
    return;
  }
  if (!Array.isArray(d.defs) || !d.defs.length) { //  defs 空 = 没解析出任何一条
    box.innerHTML = gateReadFail("检查表读不出来", {
      error: d.error || "defs 是空的（没从 gate.py 解析出任何判定）",
      hint: d.hint || "检查 rongke/gate.py 的模块 docstring"});
  } else {
    // 序号用**数组下标 +1**：服务端每条给的编号字段不是 `no`（取它会得到 undefined），
    // 而下标序恰好就是 docstring 里的 1~9 —— 不依赖一个不存在的字段。
    box.innerHTML = '<table class="grid"><thead><tr>' +
      "<th style='width:34px'>#</th><th>判定</th>" +
      "<th style='width:170px'>类别</th><th style='width:130px'>拦什么</th>" +
      "</tr></thead><tbody>" + d.defs.map((x, i) => "<tr>" +
      "<td class='m'>" + (i + 1) + "</td>" +
      "<td class='gt'>" + md(x.text || "") + "</td>" +
      "<td>" + esc(x.class || "") + "</td>" +
      /* 红色只给“出事了”。一条规则“是硬门”是它正常的属性，
         不是事故 —— 10 行里 9 行红，读起来像 9 个错误。
         ★ 这一列只说**类别里没说的那半句**：`class` 已经写着「证据类（硬门）」，
           第四列再重复一遍「硬门，阻断完成」= 同一行同一件事说两次。
           所以硬门只补「阻断完成」，软检查只补「不阻断完成」——
           两行都直接回答表头那个问句「拦什么」。 */
      "<td>" + (x.kind === "soft"
        ? '<span class="pill dim">不阻断完成</span>'
        : '<span class="gatekind">阻断完成</span>') + "</td></tr>").join("") +
      "</tbody></table>";
  }
  /* W18-F · `d.source` 是后端给的**事实**（这 9 条是从代码里逐字读出来的，不是界面手写的），
     但它的原文字面就把**文件名与"模块 docstring"**一起送上了标题行 ——
     那是写给维护者的措辞。用户要的是那条**保证**（逐字现读、不是手写），不是文件名。
     ★ 保证一个字不丢：原文整串留在 `title` 里，鼠标停上去就能核对。 */
  const src = String(d.source || "").trim();
  const nDefs = (d.defs || []).length;
  const gh = $("#gateDefsHint");
  gh.textContent = src ? (nDefs + " 条 · 逐字现读，不是手写") : "";
  if (src) gh.title = src; else gh.removeAttribute("title");

  // 下半张：最近一次判定。`last:null` **绝不渲染成"全过"**。
  if (d.last == null) {
    $("#gateLastHint").textContent = "";
    last.innerHTML = '<div class="blank">还没有跑过任务 —— 这里显示的是<b>最近一次</b>检查的结果，' +
      "不是它有没有在把关。<br>跑一次任务（或在侧栏会话列表里回放一次）后再来看。</div>" +
      (d.hint ? '<div class="kbwarn"><b>服务端原话：</b>' + esc(String(d.hint)) + "</div>" : "");
    return;
  }
  const L = d.last;
  $("#gateLastHint").textContent = (L.rid || "") + (L.when ? " · " + L.when : "");
  let h = '<div class="kv"><b>任务</b><span>' + esc(L.goal || "(没有目标)") + "</span></div>" +
    '<div class="kv"><b>运行号</b><span>' + esc(L.rid || "—") + "</span></div>";
  h += L.passed
    ? '<div class="kbok"><b>这道门放行了</b>（passed=true）—— ' +
      "硬门一条没过都不会是这个结果；细项见下面各条。</div>"
    : '<div class="kbbad"><b>被门打回</b>（passed=false）—— 它没有宣布完成，' +
      "逐条原因如下（原文照搬，界面不重述）：</div>";
  const rs = L.reasons || [];
  if (!L.passed) {
    h += rs.length ? "<ul class='rs'>" + rs.map(x => "<li>" + esc(x) + "</li>").join("") +
      "</ul>" : '<div class="kbwarn"><b>passed=false 但 reasons 是空的</b>：' +
      '服务端没给出逐条原因 —— 这不是「没原因就算过」。见上方 hint / 服务端日志。</div>';
  } else if (rs.length) {
    h += '<div class="kbwarn">过了却仍有 reasons（' + rs.length + " 条），照列：" +
      rs.map(x => esc(x)).join("<br>") + "</div>";
  }
  const ws = L.warnings || [];
  h += ws.length ? '<div class="kbwarn"><b>软检查（第 5 条，不阻断）' + ws.length +
    " 条：</b>" + ws.map(x => esc(String(x))).join("<br>") + "</div>"
    : '<div class="mtline"><span>软检查：没有</span></div>';
  // 写作体检细项：**复用**页面上那一份 lintCard()，不重写第二份渲染。
  const lc = lintCard(L.lint, true);
  h += lc ? '<div class="mtline"><span>写作体检细项（点开折叠）：</span></div>' + lc
          : '<div class="mtline"><span>写作体检：这次没有细项 —— lint 是 ' +
            (L.lint === null || L.lint === undefined ? "null（不是写作任务，或正文没落盘）"
                                                     : "一份认不出的形状") + "</span></div>";
  last.innerHTML = h;
  wireLint(last);
}

/* ── 交付物 ── */
/* 哪些扩展名进稿件台读正文。这是 `loadArtifacts()` 里那条 TEXTLIKE 的**副本** ——
   本项目最忌讳的就是副本，但 W2 硬约束 2「既有 5 个视图一处不许改」，
   不许把那边抽成共用常量。差异：这里不把 svg 当文本（它是图，归「图表」页管）。
   ★ 合并成一份是收尾工单的活，别长期留着。 */
const DELIV_READ = ["md", "markdown", "txt", "csv", "tsv", "json", "tex", "bib",
                    "yaml", "yml", "log", "html"];
/* 「产出」页默认铺几条。10 是**测出来的**：一条产出的卡头约 32px、带文件的还要
   加内衬里的每个文件行 ~30px，83 条全摊开是 10927px（11.2 屏）。
   取 10 条 ⇒ 约 1.3 屏，正好一屏看完最近的、又不用滚 —— 
   这是"默认视野里能看见最近做了什么"的最小值，不是随便定的整数。
   展开状态放在模块级布尔里：loadDeliv() 会被重复调用（切页、点刷新），
   状态不能只活在闭包里，否则一刷新就弹回 10 条，用户刚点开的全没了。 */
const DELIV_HEAD = 10;
let DELIV_EXPAND = false;
async function loadDeliv() {
  const d = await pageGet("/api/deliverables");
  if (d.error || !Array.isArray(d.items)) {
    $("#delivStat").innerHTML = ""; $("#delivHint").textContent = "";
    $("#delivBody").innerHTML = pageFail("产出列表暂时读不出来",
      "文件还在工作区里，只是这一页暂时列不出清单 —— 不影响已经交付的东西。",
      d.error ? d : {
        error: "返回里没有 items（拿到的是：" + Object.keys(d).join(", ") + "）",
        hint: "这一页只读 /api/deliverables —— 请检查那条路由"});
    return;
  }
  const bk = d.by_kind || {};
  /* 按数量降序：原来是后端字典序，
     想看“哪种格式最多”要一路看到最后一个。 */
  const kinds = Object.keys(bk).sort((a, b) => bk[b] - bk[a]);
  $("#delivStat").innerHTML =
    '<div class="kbn"><b>' + (d.total == null ? kinds.reduce((a, k) => a + bk[k], 0) : d.total) +
    "</b><span>个产出物</span></div>" +
    '<div class="kbn"><b>' + d.items.length + "</b><span>条任务</span></div>";
  $("#delivKinds").innerHTML = kinds.map(k =>
    '<span class="kbk"><b>' + bk[k] + "</b>" + esc(k) + "</span>").join("");
  const noFile = d.items.filter(x => !(x.files || []).length).length;
  // W18-C · 理由属于**章节级**：说一遍就够，不该在每张空卡上抄一遍。
  const noSnap = d.items.filter(x => !(x.files || []).length && !x.archived).length;
  $("#delivHint").textContent = d.items.length
    ? (noFile ? "其中 " + noFile + " 条没有归档产出物" +
        (noSnap ? "，这 " + noSnap + " 条连归档快照也不在磁盘上、已经回放不了" : "") +
        " —— 目标还列着，是因为「看得见」优先于「能回放」" : "每条都有归档产出物") : "";
  if (!d.items.length) {
    $("#delivBody").innerHTML = '<div class="blank">还没有归档过任何产出物 —— 跑一次任务后回来。' +
      "<br>（这不是「读不到」：接口应答正常，是磁盘上确实没有。）</div>";
    return;
  }
  // ★ 2026-10-22 P3 长页：默认只铺最近 DELIV_HEAD 条，其余收在「展开全部」后面。
  //   实测改前：83 条产出铺成 84 张卡、10077px ≈ 11 屏，其中 60 多张是几个月前的。
  //   用户要的是"我最近做的东西"，不是"全部历史"——历史仍在磁盘、仍在接口里，
  //   只是不再默认全摊开。**没有做分页**（他选的是折叠），因为翻页要引入页码状态，
  //   而"一屏看全部"是他现在的习惯。
  //   下标一致性：data-run / data-desk 存的是**在 d.items 里的原始下标**，
  //   不是切片后的下标 —— 这样折叠只影响"渲染几条"，不影响"点的是哪条"。
  //   展开时用同一个 items.slice(0, n) 再渲染一遍，两种状态共用一套渲染代码。
  const total = d.items.length;
  const shown = DELIV_EXPAND ? total : Math.min(DELIV_HEAD, total);
  $("#delivBody").innerHTML = partialNote(d) +
    d.items.slice(0, shown).map((it, i) => {
    const fs = it.files || [];
    // W17-A · 卡片头单行截断 + 可展开（与侧栏会话列表同一套截断思路）。
    // 全文在这里出现**两次**是刻意的：h3 里那份被 CSS 裁着看（.ttl），展开体那份
    // （.procb > .kv）才是逐字原文 —— 会话行的标题也是这个形状。
    // ★ 两份都是 esc(it.goal) 原样递过去，JS 不切一个字符。
    // W18-C · 没有产出物的卡片**不再渲染空身子**。
    // 改前实测：`.card .inner{padding:14px 16px}` 是**恒可见**的（折叠只切 .procb），
    // 于是 28 张没有产出物的卡各撑出一块 14px 内衬的空白，并在同一页里把同一句
    // 46 字的说明重复了 28 遍 —— 这是"产出"页 7.4 屏长墙的主要成分。
    // 现在：空卡 = 标题行一行（点开仍能看目标全文），理由在 #delivHint 说一遍。
    const goal = esc(it.goal || it.rid || "(没有目标)");
    return '<div class="card proc" data-open="0">' +
      '<h3 onclick="toggleDetail(this)"><button class="mini" data-run="' + i + '" ' +
      'title="把这次任务的产出物读进稿件台">读这次</button>' +
      '<span class="ttl" title="' + esc(ttl(goal)) + '">' +
      esc(humanTask(it.goal || it.rid || "(没有目标)")) + "</span>" +
      '<span class="hint">' + esc(it.when || "") + " · " + it.rid +
      (fs.length ? "" : " · 没有产出物") + "</span></h3>" +
      '<div class="procb"><div class="kv"><b>目标全文</b><span>' +
      goal + "</span></div></div>" +
      (fs.length ? '<div class="inner"><div class="files">' + fs.map((f, j) =>
        '<div class="file"><span class="fi">' + fileGlyph(f.kind) + "</span>" +
        '<span class="fn" title="' + esc(f.rel) + '">' + esc(f.rel) + "</span>" +
        '<span class="fs">' + fmtSize(f.bytes || 0) + "</span>" +
        '<span class="fa">' +
        (DELIV_READ.indexOf(String(f.kind || "").toLowerCase()) >= 0
          ? '<button class="mini" data-desk="' + i + "-" + j +
            '" title="摊到右边整面墙上看：不截断">读全稿 ▤</button>' : "") +
        '<a class="mini" style="text-decoration:none" href="' + esc(f.url || "") +
        '&amp;dl=1" title="走的就是那一条围栏通路">下载</a>' +
        "</span></div>").join("") + "</div></div>" : "") +
      "</div>";
  }).join("") +
    // 「展开全部」只在这一页真的有被收起来的东西时才出现 —— 少于 10 条时不该有按钮。
    (shown < total
      ? '<div class="morebar"><button class="mini pri" id="delivMore">展开全部 ' +
        total + ' 条</button><span class="hintl">现在只显示最近 ' + shown +
        ' 条，更早的都在磁盘上</span></div>'
      : (DELIV_EXPAND && total > DELIV_HEAD
          ? '<div class="morebar"><button class="mini" id="delivLess">只看最近 ' +
            DELIV_HEAD + ' 条</button></div>' : ""));
  const host = $("#delivBody");
  host.querySelectorAll("[data-desk]").forEach(b => {
    b.onclick = () => {
      const i = parseInt(b.dataset.desk.split("-")[0], 10);
      const j = parseInt(b.dataset.desk.split("-")[1], 10);
      const f = (d.items[i].files || [])[j] || {};
      // 稿件台是唯一的读法；它要的字段名是 path/size/ext，这里原样递过去。
      deskReadFile(d.items[i].rid, { path: f.rel, size: f.bytes, ext: f.kind });
    };
  });
  host.querySelectorAll("[data-run]").forEach(b => {
    // ★ 一次点击只该干一件事：这那颗钮在卡片标题行里，而**整行**是展开/收起的开关。
    //   不止住冒泡，点「读这次」会顺手把那张卡折起来（历史页 W16-B 同款冲突，同款解法）。
    b.onclick = (ev) => {
      if (ev && ev.stopPropagation) ev.stopPropagation();
      deskLoadRun(d.items[parseInt(b.dataset.run, 10)].rid);
    };
  });
  // 「展开全部 / 只看最近」：只切一个布尔再重渲染，不另写一套渲染代码。
  const more = $("#delivMore");
  if (more) more.onclick = () => { DELIV_EXPAND = true; loadDeliv(); };
  const less = $("#delivLess");
  if (less) less.onclick = () => { DELIV_EXPAND = false; loadDeliv(); };
}

/* ── 图表 ── */
const FIG_RASTER = ["png", "jpg", "jpeg", "gif", "webp"];
/* ══════════ 分析成图 ══════════
   ★ 一条纪律：这一页**不自己判断图好不好**。图面体检的结论由服务端
     `figure.FigureAuditor` 给，界面只负责把它摆出来。重算一次就有两个真相。
   ★ AN.last 记住上一次的结果 —— 重渲染（换表、换图型）时不用重新画图。 */
const AN = { view: null, last: null, busy: false };

const KIND_NAME = k => {
  const hit = ((AN.view && AN.view.kinds) || []).filter(x => x.kind === k)[0];
  return hit ? hit.name : k;
};

async function loadAn() {
  AN.view = null;
  const d = await pageGet("/api/analysis");
  if (d.error || !Array.isArray(d.kinds)) {
    $("#anStat").innerHTML = ""; $("#anHint").textContent = "";
    $("#anBody").innerHTML = pageFail("成图装置读不出来",
      "这一页暂时不知道这台机器能不能画图 —— 已经画好的图不受影响。",
      d.error ? d : { error: "返回里没有 kinds（拿到的是：" +
        Object.keys(d).join(", ") + "）", hint: "这一页只读 /api/analysis —— 请检查那条路由" });
    return;
  }
  AN.view = d;
  /* ★ 2026-10-28 重构：原来四张卡里塞了四种结构不同的东西——
     "两个数字挤在一个数字位"（`89 / 183`）、"状态词占着数量的位置"（`齐了`）。
     三张数量卡只说一件事（一个数 + 一个名词），状态移到下面一条，
     用全站已有的 .kbok/.kbwarn。且**齐了的时候不说话** —— 正常状态不占一块绿底横幅。 */
  const _nTab = (d.tables || []).length;
  $("#anStat").innerHTML =
    '<div class="kbn"><b>' + d.kinds.length + "</b><span>种图型可选</span></div>" +
    '<div class="kbn"><b>' + _nTab + "</b><span>张现成数据表</span></div>" +
    '<div class="kbn"><b>' + (d.widths ? d.widths.column + " mm" : "—") +
      "</b><span>单栏宽</span></div>" +
    /* 状态条：只在**不齐**的时候出现。不占数量的位置，也不占数量的字号。 */
    (d.ready ? "" :
      '<div class="kbwarn" style="grid-column:1/-1"><b>绘图装置不全</b>——这一页暂时画不了图。'
      + '去「首次配置」看看缺哪个引擎。</div>') +
    /* 双栏宽不进卡片（三张卡都得是一个数），但也**不能丢**：
       它是装下模板要用的真尺寸，放一行备查事实。 */
    (d.widths ? '<div class="kblib"><b>双栏宽</b><span>' + d.widths.full + '（mm）· 横排两栏的图用这个宽度</span></div>' : "");
  $("#anHint").textContent = d.ready ? "" : "绘图装置不全";
  renderAn();
}

/* 空状态不是"没东西"，是"下一步做什么" —— 所以它的位置给的是一条可走的路。 */
function renderAn() {
  const d = AN.view;
  if (!d) return;
  const tb = d.tables || [];
  let h = "";
  if (!d.ready) {
    h += '<div class="kbwarn"><b>这台机器现在画不了图</b> —— ' +
      esc(String(d.python || "找不到能用的解释器")) +
      "。<br>缺的东西：" +
      (d.notes && d.notes.length ? esc(d.notes.join("；")) : "没有说明") +
      "<br>去「首次配置」页能看更细的检查。</div>";
  }
  if (!tb.length) {
    h += '<div class="blank">还没有可用的数据表 —— 把 csv / xlsx 放进 <b>runs/_uploads/</b> ' +
      "后回来这一页。<br>（这不是「读不到」：接口应答正常，是确实还没放表。）</div>";
    $("#anBody").innerHTML = h;
    return;
  }
  const kinds = d.kinds.map(k =>
    '<option value="' + esc(k.kind) + '">' + esc(k.name) + "</option>").join("");
  h += '<div class="anrow">' +
    '<div class="anfield grow"><label for="anTable">数据表</label>' +
      '<select id="anTable">' + tb.map(t =>
        '<option value="' + esc(t.rel) + '">' + esc(t.name) +
        "（" + t.kb + " KB）</option>").join("") + "</select></div>" +
    '<div class="anfield"><label for="anKind">图型</label>' +
      '<select id="anKind"><option value="">让它自己挑</option>' + kinds + "</select></div>" +
    '<button class="anbtn" id="anGo" onclick="runAn()">画这张</button>' +
    "</div>";
  h += '<div id="anOut"></div>';
  $("#anBody").innerHTML = h;
  if (AN.last) paintAn(AN.last);
}

/* 真跑一次。按钮在跑的时候必须变成"正在画…"并禁用 ——
   几秒到几十秒的等待，没有反馈就等于死了。 */
async function runAn() {
  if (AN.busy) return;
  const tb = $("#anTable"), kd = $("#anKind"), go = $("#anGo");
  if (!tb) return;
  const out = $("#anOut");
  AN.busy = true;
  if (go) { go.disabled = true; go.textContent = "正在画…"; }
  if (out) out.innerHTML = '<div class="blank">正在读表、选图型、出图，然后过一遍图面体检…' +
    "<br>（这一步要起解释器真的画，通常十几秒。）</div>";
  let j = null, err = "";
  try {
    const r = await fetch("/api/analysis", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ table: tb.value, kind: kd ? kd.value : "",
                             width_mm: 89 })
    });
    const t = await r.text();
    try { j = JSON.parse(t); }
    catch (e) { err = "返回的不是 JSON：" + String(t).slice(0, 200); }
    if (!r.ok && !err) err = String((j && j.error) || ("HTTP " + r.status));
  } catch (e) {
    err = "接口没有应答：" + (e && e.message ? e.message : e);
  }
  AN.busy = false;
  if (go) { go.disabled = false; go.textContent = "画这张"; }
  if (err || !j) {
    if (out) out.innerHTML = '<div class="kbbad"><b>没能画出来</b> —— ' + esc(err) +
      "<br>换一张表、或去「首次配置」看看绘图装置齐不齐。</div>";
    return;
  }
  if (j.error) {
    if (out) out.innerHTML = '<div class="kbbad"><b>没能画出来</b> —— ' +
      esc(String(j.error)) + "</div>";
    return;
  }
  AN.last = j;
  paintAn(j);
}

/* 把一次结果摆出来：先图，再六道门，再图注。
   顺序是"结论先于证据"：人先看图对不对，再看它凭什么说对。 */
function paintAn(j) {
  const out = $("#anOut");
  if (!out) return;
  let h = "";
  const plan = j.plan || {};
  const sug = j.suggestions || [];
  if (sug.length) {
    h += '<div style="margin-top:16px"><div class="anfield"><label>它为什么这样画</label></div>' +
      sug.map(s => '<div class="ansug"><div class="why"><b>' +
        esc(KIND_NAME(s.kind)) + "</b> · " + esc(String(s.why || "")) +
        ((s.rejected || []).length ? '<span class="rej">放下的：' +
          esc(s.rejected.join(" ")) + "</span>" : "") +
        '</div><div class="pick"><span class="conf ' + esc(String(s.confidence || "")) +
        '">' + esc(String(s.confidence || "")) + "</span>" +
        '<span class="knd">' + esc(String(s.x || "")) + " × " + esc(String(s.y || "")) +
        "</span></div></div>").join("") + "</div>";
  }
  if (j.png_rel) {
    // ★ 图**不走**通用静态文件：runs/ 下有 session.json 之类不该公开的东西。
    //   走 /api/analysis/fig?f=<相对 _analysis/ 的文件名>，服务端只放图片。
    const fname = String(j.png_rel).replace(/^.*\//, "");
    h += '<div class="anfig"><img src="/api/analysis/fig?f=' + encodeURIComponent(fname) +
      '&t=' + Date.now() + '" alt="成图预览">' +
      '<div class="cap"><span>' + esc(KIND_NAME(plan.kind)) + " · " +
      esc(String(plan.width_mm || 89)) + " mm 单栏</span>" +
      (j.pdf_rel ? '<a href="/api/analysis/fig?f=figure.pdf" target="_blank" ' +
        'rel="noopener">下载 PDF（矢量，投稿用）</a>' : "") +
      "</div></div>";
  }
  const au = j.audit;
  if (au && (au.gates || []).length) {
    h += '<div class="angates">' + au.gates.map(g => {
      const v = String(g.verdict || "").toUpperCase();
      const cls = v === "PASS" ? "pass" : (v === "NOT APPLICABLE" ? "na" : "fail");
      return '<div class="g ' + cls + '" title="' + esc(String(g.detail || "")) +
        '"><b>' + esc(String(g.name)) + "</b><span>" + esc(v) + "</span></div>";
    }).join("") + "</div>";
  }
  if (au) {
    if (au.ok === true) h += '<div class="kbok"><b>图面体检通过</b> —— 六道门都过了' +
      (au.verdict && au.verdict !== "PASS" ? "（" + esc(String(au.verdict)) + "）" : "") + "</div>";
    else if (au.ok === false) h += '<div class="kbbad"><b>图面体检没过</b> —— ' +
      ((au.blockers || []).length ? esc(au.blockers.join("；"))
        : esc(String(au.verdict || "原因未给"))) + "</div>";
    if ((au.warnings || []).length) h += '<div class="kbwarn"><b>可以更好，但不拦</b><br>' +
      au.warnings.map(w => esc(String(w))).join("<br>") + "</div>";
  }
  if (j.legend) {
    // ★ 图注是给**稿件**用的 Markdown，抄出去必须保留 `**粗体**` 原样；
    //   但界面上显示生星号（`**biomass by date**`）很丑，也让人以为坏了。
    //   所以：屏上是渲染过的粗体，复制按钮拿的是**原文**。
    //   渲染只做 `**…**` 一种（先 esc 再替换，不会带进 HTML 注入）。
    const raw = String(j.legend);
    const pretty = esc(raw).replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    h += '<div class="anlegend"><div class="hd">' +
      '<span>图注草稿（可直接抄进稿件）</span>' +
      '<button class="anbtn ghost sm" id="anCopy">复制原文</button></div>' +
      '<div class="anLegendBody">' + pretty + "</div>" +
      '<div class="anLegendTip">___ 处要你自己填</div></div>';
  }
  if ((j.columns || []).length) {
    h += '<div class="ancol">' + j.columns.map(c =>
      '<span class="c ' + esc(String(c.kind || "")) + '">' + esc(String(c.name)) +
      "<i>" + esc(String(c.kind)) + (c.n_missing ? " · 缺 " + c.n_missing : "") +
      "</i></span>").join("") + "</div>";
  }
  out.innerHTML = h || '<div class="blank">跑完了，但没有可显示的产物 —— 检查接口返回。</div>';
  // 复制按钮拿的是**原文**（带 `**` 的 Markdown），不是屏幕上那段渲染结果。
  // 按钮文案要给出"复制成功了"的即时反馈，否则点了没感觉。
  const cp = $("#anCopy");
  if (cp && j.legend) {
    cp.addEventListener("click", async () => {
      const txt = String(j.legend);
      try {
        await navigator.clipboard.writeText(txt);
        cp.textContent = "已复制 ✓";
      } catch (e) {
        // 非 https / 老浏览器拿不到 clipboard 权限时，退回到老办法，
        // 而不是让按钮点了毫无反应。
        const ta = document.createElement("textarea");
        ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select();
        let bad = false;
        try { bad = !document.execCommand("copy"); } catch (e2) { bad = true; }
        document.body.removeChild(ta);
        cp.textContent = bad ? "浏览器不让复制，请手动选中" : "已复制 ✓";
      }
      setTimeout(() => { cp.textContent = "复制原文"; }, 1800);
    });
  }
}

/* 快捷键：⌘/Ctrl+Enter 直接开画（在成图页里、且焦点不在别处时）。 */
document.addEventListener("keydown", e => {
  if (!(e.metaKey || e.ctrlKey) || e.key !== "Enter") return;
  const sec = $("#v-fig");
  if (!sec || !sec.classList.contains("on")) return;
  if (!$("#anGo")) return;
  e.preventDefault();
  runAn();
});

async function loadFig() {
  const d = await pageGet("/api/figures");
  if (d.error || !Array.isArray(d.items)) {
    $("#figStat").innerHTML = ""; $("#figHint").textContent = "";
    $("#figBody").innerHTML = pageFail("配图记录暂时读不出来",
      "图文件还在原地，只是这一页暂时列不出它们的体检结果 —— 不影响已经画好的图。",
      d.error ? d : {
        error: "返回里没有 items（拿到的是：" + Object.keys(d).join(", ") + "）",
        hint: "这一页只读 /api/figures —— 请检查那条路由"});
    return;
  }
  const tv = d.tavotto || {};
  $("#figHint").textContent = tv.available ? "Tavotto " + (tv.version || "") + "可用：独立编辑，不覆盖原图" :
    (tv.reason || "Tavotto当前不可用；原有绘图与审计不受影响");
  const nFig = d.items.reduce((a, x) => a + (x.figs || []).length, 0);
  const nAud = d.items.reduce((a, x) => a + (x.audits || []).length, 0);
  const nFresh = d.items.filter(x => x.fresh === true).length;
  const nNoAudit = d.items.filter(x => x.fresh === null).length;
  $("#figStat").innerHTML =
    '<div class="kbn"><b>' + d.items.length + "</b><span>条任务有配图记录</span></div>" +
    '<div class="kbn"><b>' + nFig + "</b><span>张图</span></div>" +
    '<div class="kbn"><b>' + nAud + "</b><span>份体检报告</span></div>" +
    '<div class="kbn"><b>' + nFresh + "</b><span>报告是这一版的</span></div>" +
    '<div class="kbn"><b>' + nNoAudit + "</b><span>没做过体检</span></div>";
  /* W18-F · 原来写作「新鲜度沿用它的图面体检判据（界面不重算）」——
     括号里那句是**写给维护者的实现说明**，用户不需要知道"界面重不重算"。只留结论。 */
  window._figTavottoAvailable = !!tv.available;
  if (!d.items.length) {
    $("#figBody").innerHTML = '<div class="blank">还没有配图资产 —— 跑一次要出图的任务后回来。' +
      "<br>（这不是「读不到」：接口应答正常，是磁盘上确实没有。）</div>" + partialNote(d);
    return;
  }
  $("#figBody").innerHTML = partialNote(d) + d.items.map((it, i) => {
    const figs = it.figs || [], auds = it.audits || [];
    // W17-A · 与产出页同一张共用规则（CSS 的 `.card > h3 > .ttl`）：标题单行省略，
    //   点标题行展开 `.procb` 看逐字全文。折叠件沿用既有的 toggleDetail + .proc/.procb，
    //   全文那一行用既有的 .kv —— 这一页没有第二套折叠逻辑。
    let h = '<div class="card proc" data-open="0"><h3 onclick="toggleDetail(this)">' +
      '<span class="ttl" title="' + esc(ttl(it.goal || it.rid)) + '">' + esc(it.goal || it.rid) + "</span>" +
      '<span class="hint">' + esc(it.when || "") + " · " + it.rid + "</span></h3>" +
      '<div class="procb"><div class="kv"><b>目标全文</b><span>' +
      esc(it.goal || it.rid) + "</span></div></div><div class='inner'>";
    // fresh 有三态：true / false / null（null = 没有体检报告）。三种各说各话。
    if (it.fresh === true) h += '<div class="kbok"><b>体检报告是这一版的</b>（新鲜度判据见下方原话）</div>';
    else if (it.fresh === false) {
      h += '<div class="kbwarn"><b>报告不是这一版的</b> —— 图改过了，报告还是旧的。' +
        "这正是要重跑图面体检的那种情况。</div>" +
        ((it.stale_findings || []).length
          ? '<div class="kbbad"><b>它的原话：</b>' +
            it.stale_findings.map(x => esc(String(x))).join("<br>") + "</div>" : "");
    } else {
      h += '<div class="kbwarn"><b>没有体检报告</b> —— ' +
        esc(it.hint || "这些图没过图面体检") + "</div>";
    }
    if (figs.length) {
      h += '<div class="figgrid">' + figs.map((f, j) => {
        const ext = String(f.kind || (f.rel || "").split(".").pop() || "").toLowerCase();
        const isImg = FIG_RASTER.indexOf(ext) >= 0;
        return '<div class="figthumb" data-fi="' + i + "-" + j + '">' +
          (isImg ? '<a href="' + esc(f.url || "") + '" target="_blank" rel="noopener">' +
            '<img src="' + esc(f.url || "") + '" alt=""></a>'
                 : '<div class="fc">' + fileGlyph(ext) + " 位图之外（" + esc(ext || "?") +
                   "）不给缩略图，点下载在本机看</div>") +
          '<div class="fc">' + esc(f.rel) + " · " + fmtSize(f.bytes || 0) +
          (isImg ? "" : ' · <a href="' + esc(f.url || "") + '&amp;dl=1">下载</a>') + "</div>" +
          (window._figTavottoAvailable ? '<button class="mini" data-tv-rid="'+esc(it.rid)+'" data-tv-path="'+esc(f.rel)+'">交给 Tavotto 精修</button>' : "") +
          (String(f.rel).indexOf("tavotto/") >= 0 && ext === "pdf" ? '<button class="mini" data-tv-audit="'+esc(it.rid)+'" data-tv-path="'+esc(f.rel)+'">复审精修版</button>' : "") +
          "</div>";
      }).join("") + "</div>";
    } else {
      h += '<div class="blank">这一条没有配图文件，但有体检报告 —— 图可能被改过或没落盘。</div>';
    }
    if (auds.length) {
      h += '<div style="margin-top:10px">' + auds.map(a => {
        const v = String(a.verdict || "");
        const pill = !v ? '<span class="pill dim">报告里没有判定</span>'
          : /ok|pass/i.test(v) ? '<span class="pill ok">' + esc(v) + "</span>"
          : '<span class="pill err">' + esc(v) + "</span>";
        return '<div class="auditrow">' + pill + "<span class='ar'>" + esc(a.rel) +
          (a.at ? " · " + esc(a.at) : "") + "</span></div>";
      }).join("") + "</div>";
    } else if (it.fresh !== null) {
      // 不许静默省略：省略等于让人以为没问题。
      // ★ 但 fresh 已经是 null 时，上一个分支已经把
      //   同一句 hint 说过一遍了 —— 再说一遍就是两个瑶珀框喊同一句话。
      h += '<div class="kbwarn"><b>这张（些）图没过图面体检</b> —— 服务端给的 audits 是空的' +
        (it.hint ? "：" + esc(String(it.hint)) : "，也没有 hint 说明原因") + "。</div>";
    }
    if (it.fresh_basis) h += '<div class="mtline"><span>新鲜度判据（服务端原话）：' +
      esc(it.fresh_basis) + "</span></div>";
    return h + "</div></div>";
  }).join("");
  wireFigThumbs();
  document.querySelectorAll("[data-tv-rid]").forEach(b => b.onclick = () => tavottoFigure(b, false));
  document.querySelectorAll("[data-tv-audit]").forEach(b => b.onclick = () => tavottoFigure(b, true));
}
async function tavottoFigure(button, reaudit) {
  if (button.disabled) return;
  button.disabled = true;
  const old = button.textContent;
  button.textContent = reaudit ? "正在复审…" : "正在交接副本…";
  try {
    const d = await api("/api/figure/tavotto", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({rid:button.dataset.tvRid || button.dataset.tvAudit,
        path:button.dataset.tvPath, action:reaudit ? "reaudit" : "handoff"})});
    if (!d.ok) throw new Error(d.error || "Tavotto操作未完成");
    const note = document.createElement("div"); note.className = "mtline";
    note.textContent = reaudit ? "复审："+(d.verdict || "未通过")+"；报告 "+(d.report || "未生成") :
      (d.state === "verified_render" ? "已确认Tavotto渲染。" : "已交接，尚未确认渲染。") +
      " 独立目录："+d.edit_dir+"。原图与脚本未修改；请在Tavotto导出后复审。";
    button.parentElement.appendChild(note);
    if (reaudit) await loadFig();
  } catch(e) { alert(e.message || String(e)); }
  finally { button.disabled = false; button.textContent = old; }
}
/* 缩略图取不到时：撤掉破图标、留一行字（工单点名的判据）。
   `onerror` 挂在 JS 里而不是写在 HTML 属性上：这段标记本就是拼出来的字符串，
   属性里的引号会绕过 `esc()` 的转义。
   ★ 但**只挂 onerror 会漏**：404 常常在处理器挂上去之前就回来了（图片是 innerHTML
     插进去才开始请求的），那时错误事件没人接，界面上就留一个破图标。
     所以还要当场问一次 `complete` + `naturalWidth` —— 已经失败过的直接按失败处理。 */
function figDead(im) {
  const box = im.closest(".figthumb");
  const href = im.parentElement && im.parentElement.tagName === "A"
    ? im.parentElement.getAttribute("href") : "";
  if (!box) { im.style.display = "none"; return; }
  box.classList.add("dead");           // CSS: .figthumb.dead img{display:none}
  const a = im.parentElement;
  if (a && a.tagName === "A") a.remove();     // 点进去还是坏图，不如一并撤掉
  if (!box.querySelector(".deadnote")) {
    const n = document.createElement("div");
    n.className = "fc deadnote";
    n.textContent = "这张图没取到 —— 不是「没有图」，是这一趟请求没成。";
    if (href) {
      const l = document.createElement("a");
      l.href = href + "&dl=1";        // 同一条围栏通路，只是带上下载标志
      l.textContent = "下载后在本机看";
      n.appendChild(document.createTextNode(" "));
      n.appendChild(l);
    }
    box.appendChild(n);
  }
}
function wireFigThumbs() {
  $("#figBody").querySelectorAll(".figthumb img").forEach(im => {
    im.onerror = () => figDead(im);
    im.onload = () => { if (im.naturalWidth === 0) figDead(im); };
    if (im.complete && im.naturalWidth === 0) figDead(im);
  });
}

/* ═════════════ 设置页（W3 新增）═════════════
   三条老规矩照搬 W2：内容一律 JS 现填、失败只有一个渲染器 `kbFail()`、
   「读不到 / 没设 / 设了但没生效」是三种不同的事实，各说各话。
   ★ 这一页最坏的两件事，这里各专门挡了一道：
     ① 把 400 渲染成"已保存" —— 结果区只有服务端真的回了 `ok` 才用绿，
        否则原样贴服务端那句话（改坏了却显示成功，比不给设置页更糟）。
     ② 让人以为密钥填在这里 —— **W19-A 起这一页真的能填密钥值了**，
        但填的是"存到你家目录"，且**页面上永不回显**（`type="password"`、
        接口只回 `chars` 长度不回值）。这一页仍然没有任何一处显示密钥值。 */
let SETD = null;                 // 上一次读到的配置真相（用来只提交真改动过的字段）
/* 结果区的内容单独存一份：`renderSet()` 会整块重画 `#setBody`，
   回执若只写进 DOM，保存成功后那句"已重建 agent"会被紧随其后的重画抹掉 ——
   实测翻车过一次（浏览器里点保存，界面闪一下就空了，等于没告诉人结果）。 */
let SETRES = null;               // {ok:bool, text:string}
function setPill(kind, text) { return '<span class="pill ' + kind + '">' + esc(text) + "</span>"; }
function setRes(id, okv, text) {
  SETRES = { ok: okv, text: text };
  paintRes(id);
}
function paintRes(id) {
  const el = typeof id === "string" ? $(id) : id;
  if (!el || !SETRES) return;
  el.className = "setres " + (SETRES.ok ? "ok" : "bad");
  el.innerHTML = (SETRES.ok ? "<b>已保存</b>\n" : "<b>未保存</b>\n") + esc(SETRES.text);
}
/* 设置页的四个组件：字段行、分段、开关、分节卡。
   这些函数原来长在 renderSet() 里，每次重画都重新创建一遍；抽出来后
   renderSet 只负责"这一页由哪些字段组成"，控件皮与无障碍属性集中在一处。
   ★ 输出字符串一个字节都没改：id、name、for=、aria-* 全沿用原值 ——
     ui_check 的「JS 引用的 id 必须存在」只认静态字面量，拼接的它看不见。 */
const SetC = {
  field(id, label, ctrl, note, pill) {
    return '<div class="sfld"><label' + (id ? ' for="' + id + '"' : "") + ">" + esc(label) + "</label>" +
      '<div class="ctl">' + ctrl + (pill || "") + "</div>" +
      (note ? '<div class="note">' + note + "</div>" : "") + "</div>";
  },
  seg(opts, cur, idTag) {
    const i = Math.max(0, opts.map(o => o[0]).indexOf(cur));
    return '<span class="segctl" role="tablist" style="--seg-n:' + opts.length +
      ';--seg-i:' + i + '"><span class="segind" aria-hidden="true"></span>' +
      opts.map(o => '<button type="button" class="seg" role="tab" data-seg-val="' + esc(o[0]) +
        '" aria-selected="' + (o[0] === cur ? "true" : "false") + '">' + esc(o[1]) +
        "</button>").join("") + "</span>" +
      '<select class="narrow" ' + idTag + ' style="display:none">' +
      opts.map(o => '<option value="' + esc(o[0]) + '"' +
        (o[0] === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>").join("") +
      "</select>";
  },
  sw(opts, cur, idTag, label) {
    const on = cur === opts[1][0];
    const _al = (label || "开关").replace("{on}", opts[1][1]).replace("{off}", opts[0][1]);
    return '<button type="button" class="switch" role="switch" aria-checked="' +
      (on ? "true" : "false") + '" aria-label="' + esc(_al) + '" data-sw-on="' + esc(opts[1][0]) +
      '" data-sw-off="' + esc(opts[0][0]) + '"><span class="thumb"></span></button>' +
      "<span>" + esc(opts[on ? 1 : 0][1]) + "</span>" +
      '<select class="narrow" ' + idTag + ' style="display:none">' +
      opts.map(o => '<option value="' + esc(o[0]) + '"' +
        (o[0] === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>").join("") +
      "</select>";
  },
};

function renderSet(d) {
  const p = d.provider || {};
  const envSet = !!d.key_present;
  /* W19-A：`key_present` 现在有两级（环境变量 / 家目录密钥文件），
     所以它只说明"有个值能读到"。这里再要一个"是不是存在本机文件里"，
     用来决定要不要给「清掉」按钮、以及输入框的占位文案。 */
  const keyPresent = !!d.key_present;
  const keyInFile = !!d.key_in_secrets_file;
  const shadow = new Set(d.shadowed_by_env || []);
  const warn = (k) => shadow.has(k) ? setPill("warn", "被环境变量盖住") : "";
  /* ★ 这里给 label 补 for=id：不补的话「模型名 / 服务端点 / max_tokens」这些字只是
     视觉标签，屏幕阅读器念到输入框时读不出名字，点文字也不会聚焦到对应框。
     实测过：设置页 5 个控件里 4 个输在此处（1 个是分段控件里的 <button>）。
     `id` 参数本来就传进来了，接上就行，不用改任何调用点。 */
  const { field: row, seg: segCtl, sw: swCtl } = SetC;
  const tval = document.documentElement.dataset.themeSrc === "manual"
    ? (currentTheme() === "dark" ? "dark" : "light") : "system";
  $("#setBody").innerHTML =
    /* ── 通用（DSH 信息架构第一节）：外观 + 行为偏好 + 当前诊断概览 ──
       这两项都是把侧栏里已有的 shell 控件（主题、过程默认展开）在设置页如实呈现，
       走同一份状态（setTheme / PROCOPEN），荣珂不新增后端、不碰内核。 */
    '<div class="card"><h3>通用<span class="hint">' + setPill("dim", "偏好") + '</span></h3><div class="inner">' +
      row("setTheme", "外观主题",
          segCtl([["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]],
                 tval, 'id="setTheme"'),
          "默认跟随系统深浅色。手动选定后会记住；切回「跟随系统」即把决定权交还给系统。" +
          "与侧栏的主题按钮是同一份状态。", "") +
      row("setProc", "过程展开默认",
          swCtl([["0", "默认收起"], ["1", "默认展开"]],
                PROCOPEN ? "1" : "0", 'id="setProc"', "过程展开默认：{off}/{on}"),
          "新开任务时，思考与工具调用是否默认展开。与侧栏「默认展开过程」是同一份状态。", "") +
      /* ★ DSH 的 `transcriptView` —— 真源 `dsh-client-ui-chat/lib/index.js`：
         compact / standard / detailed / verbose，默认 detailed。
         README.zh.md L93 原文：「简洁、标准、详细模式收起符合条件的已完成轮次，
         **不隐藏最终答案**；完全展开模式保留时长或状态抬头，但不支持收起」。
         这是"划半天"的正解：收的是过程，答案永远露着。 */
      row("setTView", "工作步骤展示",
          segCtl([["compact", "简洁"], ["standard", "标准"],
                  ["detailed", "详细"], ["verbose", "完全展开"]],
                 transcriptView(), 'id="setTView"'),
          "控制已完成轮次里的过程如何呈现，<b>不影响最终答案</b>。简洁＝连「耗时」抬头一起收起；" +
          "标准＝只留抬头；详细（默认）＝抬头＋摘要；完全展开＝过程行全部摊开、不支持收起。", "") +
      '<div class="note" style="margin-top:2px">当前服务端诊断：<code>' + esc(p.state || "-") +
      "</code>　" + (p.ready ? setPill("ok", "就绪") : setPill("err", "不可用")) +
      " " + setPill("dim", esc(p.name || "-")) + "</div>" +
      "</div></div>" +
    /* ── magpie 通道（在「模型与服务端」之前）──────────────────────
       为什么单独一张卡、且排在前面：下面那些字段（base_url / api_key_env /
       模型名）是**给知道自己在干什么的人**用的。客户不知道 api_key_env 是什么意思，
       也不该去查 magpie 的端口号。这张卡让他只做一件事：点一下。
       卡片本身由 paintMagpie() 填充（要问后端网关在不在听，异步）。 */
    '<div class="card" id="magpieCard"><h3>用 magpie 的模型' +
      '<span class="hint" id="magpiePill">' + setPill("dim", "检测中…") +
      '</span></h3><div class="inner" id="magpieBody">' +
      '<div class="note">正在问本机 magpie 网关（127.0.0.1:3425）…</div>' +
      '</div></div>' +
    /* ── 模型与服务端（第二节）：换模型 / 端点 / 推理档 / 密钥变量名 + 保存动作 ── */
    '<div class="card"><h3>模型与服务端<span class="hint">' +
      (envSet ? setPill("ok", "密钥可用") : setPill("err", "还没有密钥")) +
      " " + setPill("dim", esc(p.model || "-")) + "</span></h3><div class='inner'>" +
      row("setModel", "模型名",
          '<input class="mono" id="setModel" type="text" list="modelList" value="' +
          esc(p.model == null ? "" : p.model) + '">' +
          '<datalist id="modelList"><option value="' + esc(d.model_default || "") + '"></option>' +
          (p.model ? '<option value="' + esc(p.model) + '"></option>' : "") + "</datalist>",
          "服务端是第三方转售，模型名<b>没法枚举</b> —— 下拉里那两个只是" +
          "「内置默认」与「当前值」，<b>不是能力清单，也不是列全了</b>。" +
          "要填别的名字请照服务端的写法手填。<br>" +
          "（想参考真实模型名：命令行跑 <code>python -m rongke --pricing</code> " +
          "会打一份骨架。这个接口<b>不在这里同步调它</b> —— 它可能要联网、可能很慢。）",
          warn("provider.model")) +
      row("setBaseUrl", "服务端点 base_url",
          '<input class="mono" id="setBaseUrl" type="text" value="' +
          esc(p.base_url == null ? "" : p.base_url) + '">',
          "必须以 http:// 或 https:// 开头。", warn("provider.base_url")) +
      row("setEffort", "推理档位 reasoning_effort",
          segCtl((d.reasoning_choices || []).map(x => [x, x]),
                 p.reasoning_effort, 'id="setEffort"'),
          "留空不保存。注意：内置默认写着" +
          (p.send_reasoning_effort ? "<b>会</b>" : "<b>不会</b>") +
          "把这个字段发给服务端（<code>send_reasoning_effort</code>，这一页不改它）。" +
          "有的网关收不到它就安静地不思考 —— 那一档要改请手改 config.json。",
          warn("provider.reasoning_effort")) +
      row("setMax", "max_tokens", '<input class="short" id="setMax" type="number" min="1" step="1" value="' +
          esc(p.max_tokens) + '">', "正整数。", "") +
      row("setStream", "通道只支持流式",
          swCtl([["0", "自动"], ["1", "只走流式"]], p.stream_required ? "1" : "0",
                'id="setStream"', "通道只支持流式：{off}/{on}"),
          "有些通道（例如只接受流式请求的网关）必须打开它；打开后每次调用都带 stream=true。" +
          "这与「推理档位是否发送」是两件事，后者仍在 config.json 的 send_reasoning_effort。",
          warn("provider.stream_required")) +
      row("setEnv", "密钥环境变量名",
          '<input class="mono" id="setEnv" type="text" value="' +
          esc(p.api_key_env == null ? "" : p.api_key_env) + '">',
          "密钥在程序里<b>认哪个名字</b>（字母/数字/下划线）。" +
          "一般不用改 —— 改它只是换一个" +
          "去环境变量里找的名字，值本身在下面那一行填。", "") +
      /* ── W19-A：密钥值 ──────────────────────────────────
         在此之前这一页**只有变量名输入框**，没有任何地方能填值；
         而用户是双击桌面窗口跑荣珂的，那种进程没有 shell 环境变量
         ⇒ 界面上永远「未连接」，一个任务都跑不起来。
         所以这里补的是"第一次打开就能用"的那一格。 */
      row("setKey", "接口密钥（选填）",
          '<input class="mono" id="setKey" type="password" autocomplete="off" ' +
          'placeholder="' + (keyPresent ? "已存好，留空就是不改" : "把密钥粘贴到这里") + '">',
          "存到 <code>" + esc(d.secrets_path || "~/.rongke/secrets.json") + "</code>" +
          "（<b>你家目录里</b>，不在项目里，不会被提交）。" +
          "<b>环境变量优先于它</b>：若同名环境变量已设置，以那个为准。" +
          "页面上永不回显已保存的值。",
          "") +
      '<div class="ctl" style="display:flex;gap:8px;align-items:center;margin:-2px 0 2px;flex-wrap:wrap">' +
      '<button class="mini pri" id="btnKeySave">存到本机并验证</button>' +
      (keyInFile ? '<button class="mini" id="btnKeyClear">清掉本机保存的密钥</button>' : "") +
      '<span class="note" id="keyNote">填完点它 —— 荣珂会<b>当场拿这个密钥真发一次请求</b>，' +
      '把服务端的原话回给你，而不是等你跑完一个任务才发现不对。' +
      (keyPresent && d.key_from_env
        ? '<br>⚠ 当前同名<b>环境变量</b>已设置，它优先于这里存的值 —— 存了也不会生效。' : "") +
      '</span></div>' +
      '<div id="setKeyBox"></div>' +
      '<div id="setResBox"></div>' +
      '<div class="ctl" style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">' +
      '<button class="mini pri" id="btnSetSave">保存并重建 agent</button>' +
      '<button class="mini" id="btnSetReload">放弃改动重新读</button>' +
      '<span class="note">只提交<b>真改动过</b>的字段；一个都没改就不发请求。</span></div>' +
      "</div></div>" +
    /* ── 配置从哪一层来（第三节）：配置文件层级与环境变量覆盖 ── */
    '<div class="card"><h3>配置从哪一层来' +
      '<span class="hint">' + ((d.shadowed_by_env || []).length ?
        setPill("warn", "有字段被环境变量覆盖") : setPill("dim", "无环境变量覆盖")) + "</span></h3>" +
      "<div class='inner'>" +
      '<div class="note" style="margin:0 0 9px">优先级从低到高：内置默认 → 用户级 → 项目级 → ' +
      "环境变量 <code>RONGKE_*</code>。<b>「保存」写的是项目级那一份。</b>" +
      "上面标了「被环境变量盖住」的字段，改文件也不会生效 —— 那是当前进程的环境变量在它上面。</div>" +
      '<table class="grid"><thead><tr><th>层</th><th>路径</th><th>存在</th></tr></thead><tbody>' +
      (d.config_files || []).map(f =>
        "<tr><td>" + esc(f.scope) + '</td><td class="path">' + esc(f.path) + "</td><td>" +
        (f.exists ? "是" : "否") + "</td></tr>").join("") +
      "</tbody></table>" +
      '<div class="note" style="margin-top:9px">环境变量：' +
      ((d.env_overrides || []).filter(e => e.set).map(e => "<code>" + esc(e.var) + "</code> 已设")
        .join(" · ") || "一个都没设") + "</div>" +
      "</div></div>";
  $("#btnSetSave").onclick = saveSet;
  $("#btnSetReload").onclick = () => { SETRES = null; loadSet(); };
  /* ── W19-A · 密钥：存到本机并当场验证 ──────────────────────
     两个方向都接：（存/清）。清空走的是同一个接口的 `value:""` 分支，
     服务端那边"空值 = 删掉这个键"，所以不存在"清一半"的状态。 */
  const keyBtn = $("#btnKeySave");
  if (keyBtn) keyBtn.onclick = () => saveKey(false);
  const keyClr = $("#btnKeyClear");
  if (keyClr) keyClr.onclick = () => {
    if (!confirm("清掉本机保存的密钥？\n\n清掉之后如果环境变量里也没有，荣珂就跑不了任务了。")) return;
    saveKey(true);
  };
  /* 通用两项走的是 shell 现成状态（setTheme / PROCOPEN），不经过 /api/settings，
     所以它们改动不该抹掉「模型保存」那张回执 —— 下面泛清的 input 监听跳过这两个。 */
  $("#setTheme").onchange = e => {
    const v = e.target.value;
    if (v === "system") {
      const sys = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark" : "light";
      setTheme(sys, false);
    } else setTheme(v, true);
  };
  $("#setProc").onchange = e => {
    PROCOPEN = e.target.value === "1";
    try { localStorage.setItem("rongke-proc", PROCOPEN ? "1" : "0"); } catch (err) {}
    applyProcOpen();
  };
  /* transcriptView 也只走 localStorage（DSH 侧也存在 user-settings 里，荣珂没有那个
     文档，按 shell 偏好处理），同样不抹「模型保存」回执。 */
  $("#setTView").onchange = e => { setTranscriptView(e.target.value); };
  /* 一开始改字段，上一张回执就该消失 —— 留着它，人会以为"这次也保存了"。 */
  $("#setBody").querySelectorAll("input,select").forEach(el => {
    if (el.id === "setTheme" || el.id === "setProc" || el.id === "setTView") return;
    el.addEventListener("input", () => { if (SETRES) { SETRES = null; paintRes($("#setResBox")); } });
  });
  /* ── W14-E · 新皮 → 既有 change 路径 ──
     这两段里**不写任何业务**：把值写回同排那只隐藏 select，照原生顺序派发
     input → change，剩下的全是上面的既有代码（setTheme / PROCOPEN / 抹回执 /
     saveSet 的脏字段比对）。指示器位移与 aria-checked 是这个件自己的外观，
     一律从 select.value 反推 —— 真源只有 select 一处，不去数点了几次。 */
  const driveSel = (sel, v) => {
    if (sel.value === v) return;
    sel.value = v;
    sel.dispatchEvent(new Event("input", { bubbles: true }));
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  };
  $("#setBody").querySelectorAll(".segctl").forEach(box => {
    const sel = box.parentElement.querySelector("select");
    if (!sel) return;
    const bs = Array.from(box.querySelectorAll(".seg"));
    const sync = () => {
      const i = Math.max(0, bs.findIndex(b => b.dataset.segVal === sel.value));
      bs.forEach((b, j) => b.setAttribute("aria-selected", j === i ? "true" : "false"));
      box.style.setProperty("--seg-i", String(i));
    };
    bs.forEach(b => {
      b.onclick = () => { driveSel(sel, b.dataset.segVal); sync(); };
    });
    sync();
  });
  $("#setBody").querySelectorAll(".switch").forEach(sw => {
    const sel = sw.parentElement.querySelector("select");
    if (!sel) return;
    const txt = sw.nextElementSibling;
    const sync = () => {
      const on = sel.value === sw.dataset.swOn;
      sw.setAttribute("aria-checked", on ? "true" : "false");
      if (txt) txt.textContent = sel.options[sel.selectedIndex].textContent;
    };
    sw.onclick = () => {
      driveSel(sel, sel.value === sw.dataset.swOn ? sw.dataset.swOff : sw.dataset.swOn);
      sync();
    };
    sync();
  });
  paintRes($("#setResBox"));
  paintMagpie();
}

/* ═════════════ magpie 通道卡片 ═════════════
   用户的原话：「主要是让用户配置的方便一点」。

   在此之前要接上 magpie，用户得自己：查端口、抄模型名、设环境变量、
   记 Docker 命令 —— 四件事里任何一件做错，结果是安静地不通。
   这张卡把四件事压成一次点击：后端探测网关 → 列出它上面的模型 →
   点一个就切过去。连不上时给的是**下一步怎么做**，不是一片空白。

   ★ 它只读。真正写配置仍然走 /api/settings 那条唯一通路
     （校验 → 备份 → 原子写 → 重建 agent），不在这里另开一条。 */
function paintMagpie(forced) {
  const box = $("#magpieBody"), pill = $("#magpiePill");
  if (!box || !pill) return;
  const ep = forced ? String(forced) : "";
  const go = (d) => {
    if (!box.isConnected) return;                 // 用户已经离开这一页
    const on = !!d.online;
    pill.innerHTML = on ? setPill("ok", "网关在线") : setPill("dim", "没检测到网关");
    const cur = String(d.current_base_url || "");
    const using = !!d.already_on_magpie;
    const curModel = String((SETD && SETD.provider || {}).model || "");
    let h = "";

    if (using) {
      h += '<div class="note" style="margin-bottom:8px">' +
        '<b>当前就是走 magpie 的。</b>端点 <code class="mono">' + esc(cur) +
        '</code>。下面是它提供的模型，点一个即可换模型。</div>';
    } else if (on) {
      h += '<div class="note" style="margin-bottom:8px">' +
        '检测到 magpie 网关在线。点下面的模型就会切过去 —— ' +
        '后面那些 base_url / 环境变量字段<b>都不用填</b>。</div>';
    } else {
      h += '<div class="note" style="margin-bottom:8px">' +
        esc(String(d.note || "没检测到 magpie 网关")) + '<br><br>' +
        '<b>如果你要用它</b>，先把它启动起来（任选一种）：<br>' +
        '· 装了 magpie 桌面版：打开它，菜单栏出现图标即可；<br>' +
        '· 有 Docker：<code class="mono">docker run -d --name magpie ' +
        '-p 127.0.0.1:3425:3425 -v magpie-config:/config ' +
        'ghcr.io/yetone/magpie</code><br><br>' +
        '启动后点下面的「重新检测」。<b>不想用它也没关系</b> —— ' +
        '下面那些字段照样可以手填，这张卡片不影响它们。</div>';
    }

    const models = d.models || [];
    if (on && models.length) {
      h += '<div class="mg-list" style="display:flex;gap:6px;flex-wrap:wrap">' +
        models.map(m => '<button class="mini' +
          (using && m === curModel ? " pri" : "") +
          '" data-mg="' + esc(m) + '">' + esc(m) + '</button>').join("") + '</div>';
      h += '<div class="note" style="margin-top:6px">模型清单来自网关本身' +
        '（<code class="mono">/v1/models</code>）—— 你在 magpie 里装了哪些渠道，' +
        '这里就有哪些。</div>';
    } else if (on) {
      h += '<div class="note" style="margin-bottom:8px">' +
        '网关在线，但它<b>没有返回任何模型</b> —— 可能 magpie 里还没加渠道。' +
        '先去 magpie 面板加一个（Providers → Add provider），回来点重新检测。</div>';
    }

    h += '<div class="row" style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">' +
      '<button class="mini" id="btnMgAgain">重新检测</button>' +
      '<button class="mini" id="btnMgHost">连别的机器…</button>' +
      '</div>';
    box.innerHTML = h;

    const again = $("#btnMgAgain");
    if (again) again.onclick = () => {
      box.innerHTML = '<div class="note">正在检测…</div>';
      setTimeout(() => paintMagpie(ep), 60);
    };
    const hostBtn = $("#btnMgHost");
    if (hostBtn) hostBtn.onclick = () => {
      const v = window.prompt(
        "magpie 网关地址（默认本机 http://127.0.0.1:3425）。\n" +
        "Docker 装在内网另一台机器上就填那台的地址，例如 http://192.168.1.9:3425",
        ep || "http://127.0.0.1:3425");
      if (v == null) return;
      box.innerHTML = '<div class="note">正在检测 ' + esc(v) + ' …</div>';
      api("/api/magpie?host=" + encodeURIComponent(v))
        .then(x => { if (box.isConnected) go({ ...x, endpoint: v }); })
        .catch(e => {
          if (box.isConnected) box.innerHTML =
            '<div class="note">检测失败：' + esc(e.message) + '</div>';
        });
    };
    box.querySelectorAll("[data-mg]").forEach(b => {
      b.onclick = () => switchToMagpie(b.dataset.mg, d.endpoint);
    });
  };

  api("/api/magpie" + (ep ? "?host=" + encodeURIComponent(ep) : ""))
    .then(go)
    .catch(e => {
      if (!box.isConnected) return;
      pill.innerHTML = setPill("dim", "检测失败");
      box.innerHTML = '<div class="note">问不到后端：' + esc(e.message) +
        '</div><div class="row"><button class="mini" id="btnMgRetry">重试</button></div>';
      const a = $("#btnMgRetry");
      if (a) a.onclick = () => paintMagpie(ep);
    });
}

/* 点了某个模型之后：走 /api/settings 那条唯一写通路（校验 → 备份 → 原子写
   → 重建 agent），不在这里另开一条。 */
async function switchToMagpie(model, endpoint) {
  const box = $("#magpieBody");
  if (box) box.innerHTML = '<div class="note">正在切到 ' + esc(model) + ' …</div>';
  const base = String(endpoint || "http://127.0.0.1:3425").replace(/\/+$/, "");
  const body = {
    "provider.model": model,
    "provider.base_url": base + "/v1",
    "provider.api_key_env": "MAGPIE_API_KEY",
    "provider.send_reasoning_effort": "false",
  };
  let r, j;
  try {
    r = await fetch("/api/settings", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    j = await r.json().catch(() => ({ error: "响应不是 JSON" }));
  } catch (e) {
    j = { error: "接口没有应答：" + (e && e.message ? e.message : e) };
  }
  if (!r || !r.ok || !j || !j.ok) {
    setRes("#setResBox", false, String((j && j.error) || ("HTTP " + (r ? r.status : "?"))));
    paintMagpie(endpoint);
    return;
  }
  const np = j.provider || {};
  const lines = ["端点：" + (np.base_url || "-"), "模型：" + (np.model || "-")];
  lines.push(j.rebuilt
    ? "已重建 agent —— 下一个任务就用 magpie 的模型，不用重启服务。"
    : "配置已落盘，但没能重新装载起来：" + (j.kernel_error || "未知原因"));
  if (!j.key_present) {
    lines.push("还差一步：magpie 网关接受<b>任意</b> key。在下面「接口密钥」里填 " +
      "magpie（就这六个字），点「存到本机并验证」，就成了。");
  }
  setRes("#setResBox", true, lines.join("\n"));
  SETD = null; loadSet(); loadStatus();
}
/* ═════════════ 首次配置（/api/setup）═════════════
   这一页的全部判据都在**服务端**（rongke/setupdoctor.py）：哪个解释器装了
   python-pptx、审计脚本在哪、模板在不在、Origin 的 COM 通不通 —— 这些真值
   在盘上和子进程里，前端够不到。前端只负责把结论**翻译成人话**并给动作。
   ★ 每条都标出它探的是「荣珂自己」还是「下游解释器」：这两侧会给出相反的答案，
     不标出来，用户会拿着一条"已就位"去解释另一侧为什么还是失败。 */
let SETUPD = null;
const SETUP_SIDE = { rongke: "荣珂自己那个进程", sub: "下游解释器" };
const SETUP_LVNAME = { ok: "已就位", info: "说明", warn: "有隐患", fail: "不通" };

async function loadSetup() {
  const box = $("#setupBody");
  if (box) box.innerHTML = '<div class="blank">正在检查…（要起几次子进程，约几秒）</div>';
  /* 45000：2026-11-05 实测刚起的控制台首次 /api/setup 要 22.6s（起几次子进程
     查依赖链），之后降到 3.5s。原来 25s 只剩 1.1 倍余量 ⇒ 高负载下超时 ⇒
     整张「环境检查」页显示"检查没跑成"，连账单区块都不生成。 */
  const d = await pageGet("/api/setup", 45000);
  if (d.error || !Array.isArray(d.groups)) {
    SETUPD = null;
    if (box) box.innerHTML = pageFail("检查没跑成",
      "这一页没有结论 —— 不代表你的环境有问题，是这次检查没做成。",
      d.error ? d : { error: "返回里没有 groups", hint: "这一页只读 /api/setup" });
    badgeSetup(null);
    return;
  }
  SETUPD = d;
  renderSetup(d);
  badgeSetup(d);
  loadBilling();
}

/* ══ 授权 ══════════════════════════════════════════════════
   这一页只做三件事：说清现在是什么状态、粘贴激活码、注销。
   ★ 话必须说准：授权是「软件本身」的，与模型 API 开销无关 ——
     用户自己填的 key，钱直接付给厂商，不经过这里。
   ★ 到期时**只拦发起新任务**，历史与归档照常能看。这一点要在
     还没到期的时候就说，别等拦下来才解释。 */
async function loadLic() {
  const box = $("#licBody");
  if (box) box.innerHTML = '<div class="blank">正在读授权状态…</div>';
  let d;
  try { d = await pageGet("/api/license"); }
  catch (e) {
    if (box) box.innerHTML = '<div class="blank">授权状态没读到：'
      + esc(e && e.message ? e.message : e) + "</div>";
    return;
  }
  if (!d || d.error || !d.state) {
    if (box) box.innerHTML = pageFail("授权状态没读到",
      "这一页没有结论 —— 不代表你的授权有问题，是这次没读成。",
      d || { error: "接口没有应答", hint: "这一页只读 /api/license" });
    badgeLic(null);
    return;
  }
  LICD = d;
  renderLic(d);
  badgeLic(d);
}

function renderLic(d) {
  const box = $("#licBody");
  if (!box) return;
  const st = String(d.state || "unknown");
  const out = [];
  const tone = st === "licensed" ? "ok" : (st === "trial" ? "info" : (st === "expired" ? "err" : "neutral"));
  const word = st === "licensed" ? "已授权"
    : st === "trial" ? "试用中"
    : st === "expired" ? "已到期" : "状态不明";

  out.push('<div class="card"><h3>当前状态 <span class="hint">'
    + esc(word) + "</span></h3>");
  out.push('<div class="setuprow"><span class="tag" data-tone="' + tone + '">'
    + esc(word) + "</span>"
    + '<span class="setupttl">' + esc(d.reason || "—") + "</span></div>");

  // 一码一机：客户要把这行发给发行方换码。放在状态卡里，任何状态下都可见。
  const mid = String(d.machine_id || "—");
  out.push('<div class="setuprow"><span class="setupttl">本机机器码：<code id="licMid">'
    + esc(mid) + "</code></span>"
    + '<button class="mini" onclick="copyMid()" title="复制机器码">复制</button></div>');
  out.push('<div class="setuphint">把这一行发给发行方换激活码。'
    + "换机器或重装系统后机器码会变，需要新码。</div>");

  if (st === "licensed") {
    if (d.customer) out.push('<div class="setuprow"><span class="setupttl">客户：<b>'
      + esc(d.customer) + "</b></span></div>");
    if (d.plan) out.push('<div class="setuprow"><span class="setupttl">套餐：'
      + esc(d.plan) + "</span></div>");
    out.push('<div class="setuphint">'
      + (d.expires ? "到期后可凭新激活码续期。" : "这是<b>永久授权</b>。")
      + "</div>");
  } else if (st === "expired") {
    out.push('<div class="kbwarn"><b>试用期结束了</b><br>'
      + "现在<b>只是不能发起新任务</b>。历史、产出、文献库都照常能看 —— "
      + "在下面粘贴激活码就能继续。</div>");
    out.push('<div class="setuphint">已经被问的话还能看：'
      + '<button class="mini" onclick="goView(\'deliv\')">打开「产出」</button></div>');
  } else if (st === "trial") {
    out.push('<div class="setuphint">试用期共 30 天，从<b>第一次运行</b>那天算起。'
      + "到期后只拦发起新任务，历史照常能看。</div>");
  }
  out.push("</div>");

  // ── 激活 / 注销 ──
  out.push('<div class="card"><h3>粘贴激活码</h3><div class="inner">');
  out.push('<div class="setuphint" style="margin-bottom:8px">'
    + "激活码是一整行，形如 <code>RK1.xxxx.yyyyyy</code>。"
    + "它由发行方用私钥签出，<b>改一个字就验不过</b> —— 所以别手改。</div>");
  out.push('<textarea id="licCode" rows="3" style="width:100%;box-sizing:border-box;'
    + 'font-family:var(--mono);font-size:var(--fs-xxs);padding:8px 10px;'
    + 'border:0;box-shadow:var(--elevation-stroke);border-radius:var(--r-sm);'
    + 'background:var(--bg);color:var(--fg);resize:vertical" '
    + 'placeholder="把激活码粘到这里"></textarea>');
  out.push('<div id="licMsg" class="setuphint"></div>');
  out.push('<div style="display:flex;gap:8px;margin-top:9px">'
    + '<button class="pri" id="licGo" onclick="activateLic()">激活</button>'
    + (st === "licensed"
      ? '<button class="btn sm" onclick="deactivateLic()">注销激活码</button>' : "")
    + "</div>");
  out.push("</div></div>");

  out.push('<div class="card"><h3>这一页管的是什么</h3><div class="inner">');
  out.push('<div class="setuphint">管的是<b>这个软件本身的授权</b>。'
    + "模型 API 的消耗不在这里 —— 你在「设置」里填自己的 key，"
    + "钱是你直接付给模型厂商的，<b>不经过荣珂</b>。"
    + "「首次配置」页那个余额是给你自己看的成本估算，不是账单。</div>");
  out.push('<div class="setuphint">离线校验：激活码用内嵌公钥在本地验签，'
    + "<b>不联网、不发任何数据出去</b>。</div>");
  out.push("</div></div>");
  box.innerHTML = out.join("");
}

async function copyMid() {
  const el = $("#licMid");
  if (!el) return;
  const text = (el.textContent || "").trim();
  let done = false;
  try { await navigator.clipboard.writeText(text); done = true; } catch (e) { done = false; }
  if (!done) {
    try {
      const r = document.createRange(); r.selectNodeContents(el);
      const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
      document.execCommand("copy"); done = true;
    } catch (e) { done = false; }
  }
  const msg = $("#licMsg");
  if (msg) msg.innerHTML = done
    ? '<span style="color:var(--ok)">✔ 机器码已复制</span>'
    : '<span style="color:var(--err)">复制失败，请手动选中机器码</span>';
}

async function activateLic() {
  const ta = $("#licCode"), msg = $("#licMsg"), btn = $("#licGo");
  const code = (ta && ta.value || "").trim();
  if (!code) { if (msg) msg.innerHTML = '<span style="color:var(--err)">先把激活码粘进来。</span>'; return; }
  if (btn) { btn.disabled = true; btn.textContent = "验证中…"; }
  let j;
  try {
    /* ★ `api()` 返回的**已经是解析好的对象**，不是 Response。
       写成 `const r = await api(...); j = await r.json()` 会抛
       `r.json is not a function` —— 被这个 catch 吞掉之后，
       表现是"点了激活、什么都没发生"（服务端其实已经成功了）。
       实测撞到：接口返回正确，界面纹丝不动。 */
    j = await api("/api/license", { method: "POST", headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify({ code: code, action: "activate" }) });
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "激活"; }
    if (msg) msg.innerHTML = '<span style="color:var(--err)">'
      + esc((e && e.message) || "激活没成功") + "</span>";
    return;
  }
  if (btn) { btn.disabled = false; btn.textContent = "激活"; }
  if (!j || !j.ok) {
    // 验签失败要说准是"码不对"，不能含糊成"服务器错误"
    if (msg) msg.innerHTML = '<span style="color:var(--err)">'
      + esc((j && j.error) || "激活没成功") + "</span>";
    return;
  }
  if (msg) msg.innerHTML = '<span style="color:var(--ok)">✔ 已激活</span>';
  /* ★ 直接用**响应里带回来的** license 重渲染，不再发一次 GET。
     原先这里调 `loadLic()` 走 `pageGet`，而 `pageGet` 没禁缓存 ——
     浏览器把上一次「已到期」的 `/api/license` 直接吐回来，于是
     激活成功了、界面还写着"已到期"，要刷新一次才对。
     实测就是这么被抓到的：刷新后正确、当场不正确。 */
  if (j.license) { LICD = j.license; renderLic(j.license); badgeLic(j.license); }
  else loadLic();
  NEED_LICENSE = false;
  hideLicGate();
}

async function deactivateLic() {
  const msg = $("#licMsg");
  let j;
  try {
    // 同样：`api()` 已经返回对象，不要再 `.json()`
    j = await api("/api/license", { method: "POST", headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify({ action: "deactivate" }) });
  } catch (e) {
    if (msg) msg.innerHTML = '<span style="color:var(--err)">'
      + esc((e && e.message) || "注销没成功") + "</span>";
    return;
  }
  if (j && j.ok) {
    if (j.license) { LICD = j.license; renderLic(j.license); badgeLic(j.license); }
    else loadLic();
    if (msg) msg.innerHTML = "已注销。";
  }
}

function badgeLic(d) {
  const b = $("#licBadge");
  if (!b) return;
  if (!d) { b.hidden = true; return; }
  const st = String(d.state || "");
  if (st === "licensed") { b.hidden = true; return; }
  b.hidden = false;
  b.textContent = st === "expired" ? "到期" : (st === "trial" ? (d.days_left + " 天") : "!");
  b.setAttribute("data-tone", st === "expired" ? "err" : "neutral");
  b.title = d.reason || "";
}

/* 被授权拦下时的那层提示。**不是登录层**：登录是"你是谁"，
   授权是"你买没买"。两者同时出现时先问身份 —— 所以这层只在
   已经登录（或本来就不需要登录）之后才可能弹出来。 */
function showLicGate(why) {
  let g = $("#licGate");
  if (!g) {
    g = document.createElement("div");
    g.id = "licGate";
    g.className = "logingate";
    g.innerHTML = '<div class="loginbox">'
      + '<h2>试用期已结束</h2>'
      + '<div id="licWhy" class="setuphint"></div>'
      + '<div class="setuphint">历史与产出<b>照常能看</b>，只是不能发起新任务。</div>'
      + '<textarea id="licGateCode" rows="3" placeholder="粘贴激活码 RK1.…"></textarea>'
      + '<div id="licGateErr" class="loginerr"></div>'
      + '<div style="display:flex;gap:8px">'
      + '<button class="pri" id="licGateGo">激活</button>'
      + '<button class="btn sm" id="licGateGo2">稍后</button>'
      + "</div></div>";
    document.body.appendChild(g);
    const go = () => activateFromGate();
    $("#licGateGo").addEventListener("click", go);
    $("#licGateGo2").addEventListener("click", hideLicGate);
  }
  const w = $("#licWhy");
  if (w) w.textContent = why || "";
  g.hidden = false;
  const ta = $("#licGateCode");
  if (ta) setTimeout(() => ta.focus(), 30);
}
function hideLicGate() { const g = $("#licGate"); if (g) g.hidden = true; }

async function activateFromGate() {
  const ta = $("#licGateCode"), err = $("#licGateErr");
  const code = (ta && ta.value || "").trim();
  if (!code) { if (err) err.textContent = "先把激活码粘进来。"; return; }
  let j;
  try {
    // `api()` 返回对象；这里原先也错写成 `await r.json()`
    j = await api("/api/license", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code, action: "activate" }) });
  } catch (e) {
    if (err) err.textContent = (e && e.message) || "激活没成功";
    return;
  }
  if (!j || !j.ok) {
    if (err) err.textContent = (j && j.error) || "激活没成功";
    return;
  }
  NEED_LICENSE = false;
  hideLicGate();
  if (typeof loadLic === "function") loadLic();
}

/* ══ 账单 ══════════════════════════════════════════════════ */
/* 账单：**只读**。放在这一页而不新开一个导航项 ——
   这一页本来就是「这台机器现在什么状态」，账单是其中一项。
   扣费发生在任务跑完之后，界面刻意**不给**"扣一笔/改余额"的入口：
   那等于让任何打开控制台的人都能给自己改账。
   ★ 两件事必须写在屏幕上，而不是只写在文档里：
     ① 没接支付网关（充值只是本地占位，不会真收款）
     ② 算不出金额时是 UNKNOWN，不是 0 —— 后者会被读成"这次没花钱"。 */
async function loadBilling() {
  const box = $("#setupBill");
  if (!box) return;
  let d;
  try { d = await pageGet("/api/billing"); }
  catch (e) { box.innerHTML = '<div class="blank">账单没读到：' + esc(e.message) + "</div>"; return; }
  if (!d || d.error) {
    box.innerHTML = '<div class="blank">账单没读到：'
      + esc((d && d.error) || "接口没有应答") + "</div>";
    return;
  }
  const bal = d.balance || {}, plan = d.plan || {};
  const money = v => (v === null || v === undefined) ? "—"
    : (v === "UNKNOWN" ? "算不出来" : "$" + Number(v).toFixed(2));
  const out = [];
  out.push('<div class="card"><h3>用量与额度</h3>');
  out.push('<div class="setuprow"><span class="tag" data-tone="info">'
    + esc(plan.name || plan.id || "—") + "</span>"
    + '<span class="setupttl">余额 ' + esc(money(bal.remaining))
    + "（已用 " + esc(money(bal.spent)) + " / 共 " + esc(money(bal.granted)) + "）</span></div>");
  if (bal.exact === false || bal.unknown_charges) {
    // 不给假精确：算不出来的部分必须显式说出来
    out.push('<div class="setuphint">有 ' + (bal.unknown_charges || 0)
      + " 笔消耗的金额算不出来（价格表没填全），所以上面的余额只是<b>上限</b>，不是真值。"
      + "填价格表：<code>rongke --pricing</code></div>");
  }
  out.push('<div class="setuphint">台账在 <code>'
    + esc((d.account && d.account.ledger) || "数据目录/billing/ledger.jsonl")
    + "</code>，只追加不改写。共 " + (bal.entries || 0) + " 条流水"
    + (bal.bad_lines ? "，其中 <b>" + bal.bad_lines + " 条坏行</b>（没被静默跳过）" : "")
    + "。</div>");
  out.push('<div class="setuphint"><b>这一版没有接支付网关。</b>'
    + "命令行 <code>--billing topup</code> 只往本地台账写一条记录，"
    + "<b>不会真的收款</b>。换套餐、充值都还没接真实计费。</div>");
  out.push("</div>");
  box.innerHTML = out.join("");
}

/* 侧栏那个角标：只报"缺几项"。齐全时**隐藏**，不制造待办幻觉。 */
function badgeSetup(d) {
  const b = $("#setupBadge");
  if (!b) return;
  const c = (d && d.counts) || {};
  const bad = (c.fail || 0);
  if (!d || !bad) { b.hidden = true; return; }
  b.hidden = false;
  b.textContent = bad + " 项待修";
  b.dataset.tone = d.verdict === "broken" ? "danger" : "warning";
}

function renderSetup(d) {
  const box = $("#setupBody");
  if (!box) return;
  const c = d.counts || {};
  const head = d.verdict === "ready" ? "ok" : (d.verdict === "broken" ? "bad" : "warn");
  const out = [];
  out.push('<div class="setuphead ' + head + '"><b>' + esc(d.summary_line || "") + "</b><br>"
    + '通过 ' + (c.ok || 0) + " · 说明 " + (c.info || 0)
    + " · 有隐患 " + (c.warn || 0) + " · 不通 " + (c.fail || 0)
    + (d.verdict === "ready" ? "" :
       "<br>「核心」那几项缺了，做 PPT 会卡住；其余缺项只影响画图/出图，不影响交稿。")
    + "</div>");

  const fx = Array.isArray(d.fixable) ? d.fixable : [];
  if (fx.length) {
    out.push('<div class="filt"><button class="mini pri" id="btnSetupFix">一键修 '
      + fx.length + " 项</button>"
      + '<span class="hint" style="display:inline;padding:0 0 0 8px">'
      + "只改配置里那几个「指向」（比如把 deck.python 指到一个已经装好 python-pptx 的解释器），"
      + "不会替你装任何东西；改前会先备份 config.json。</span></div>");
  }
  out.push('<div id="setupResBox"></div>');

  (d.groups || []).forEach(g => {
    out.push('<div class="card"><h3>' + esc(g.name) + "</h3>");
    (g.items || []).forEach(it => {
      const lv = it.level || "info";
      const tone = { ok: "success", info: "quiet", warn: "warning", fail: "danger" }[lv] || "quiet";
      const core = it.core ? ' <span class="tag" data-tone="info">核心</span>' : "";
      const side = SETUP_SIDE[it.side] || it.side || "";
      out.push('<div class="setupitem" data-lv="' + esc(lv) + '">');
      out.push('<div class="setuprow"><span class="tag" data-tone="' + tone + '">'
        + esc(SETUP_LVNAME[lv] || lv) + "</span>"
        + '<span class="setupttl">' + esc(it.title || "") + "</span>" + core + "</div>");
      if (it.detail) out.push('<div class="setupdetail">' + esc(it.detail) + "</div>");
      if (it.hint) out.push('<div class="setuphint">怎么补：' + esc(it.hint) + "</div>");
      if (it.fixable && lv !== "ok") {
        out.push('<div class="setuphint"><button class="mini" data-fixkey="'
          + esc(it.key) + '">修这一项</button></div>');
      }
      out.push('<div class="setupside">检查的是：' + esc(side) + "</div>");
      out.push("</div>");
    });
    out.push("</div>");
  });
  // 账单区块：内容由 loadBilling() 现填（第二份就是第二套真相）
  out.push('<div id="setupBill"></div>');
  out.push('<div class="sub" style="margin-top:8px">检查用的解释器：<code>'
    + esc(d.self_exe || "-") + "</code>（" + esc(d.version || "") + "）"
    + "。命令行同一份报告：<code>python -m rongke --setup</code>，自动修加 <code>--fix</code>。</div>");
  box.innerHTML = out.join("");

  const one = $("#btnSetupFix");
  if (one) one.onclick = () => fixSetup(fx);
  box.querySelectorAll("[data-fixkey]").forEach(b => {
    b.onclick = () => fixSetup([b.dataset.fixkey]);
  });
}

async function fixSetup(keys) {
  if (!Array.isArray(keys) || !keys.length) return;
  const box = $("#setupResBox");
  if (box) { box.className = "setres"; box.innerHTML = "正在修 " + keys.length + " 项…"; }
  let r, j;
  try {
    r = await fetch("/api/setup", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keys: keys }) });
    j = await r.json().catch(() => ({ error: "响应不是 JSON" }));
  } catch (e) {
    j = { error: "接口没有应答：" + (e && e.message ? e.message : e) };
  }
  if (!r || !r.ok || !j || j.error) {
    if (box) {
      box.className = "setres bad";
      box.innerHTML = "<b>没修成</b>\n" + esc(String((j && j.error) || ("HTTP " + (r ? r.status : "?"))));
    }
    return;
  }
  const rows = j.fixed || [];
  const lines = rows.map(x => (x.verified ? "✔ " : (x.fixed ? "✘ " : "· "))
    + x.key + "：" + x.why
    + (x.fixed && !x.verified ? "（修完重测仍是「" + (x.after_title || x.after_level) + "」）" : ""));
  if (box) {
    box.className = "setres " + (j.fixed_count ? "ok" : "bad");
    box.innerHTML = "<b>" + (j.fixed_count ? "修好了 " + j.fixed_count + " 项" : "一项都没修成")
      + "</b>\n" + esc(lines.join("\n"))
      + "\n" + (j.rebuilt ? "已重建 agent，下一个任务就用新配置。"
                          : "配置已落盘，但没能重新装载：" + (j.kernel_error || "未知原因"));
  }
  /* 重读整份报告：修完的那几项要当场变绿才算数（判据在服务端，重测也在服务端）。 */
  const d = await pageGet("/api/setup", 45000);
  if (!d.error && Array.isArray(d.groups)) { SETUPD = d; renderSetup(d); badgeSetup(d); }
}

async function loadSet() {
  const d = await pageGet("/api/settings");
  if (d.error || !d.provider) {
    SETD = null;
    $("#setBody").innerHTML = pageFail("配置暂时读不出来",
      "这里现在是空的，不代表你的配置丢了 —— 在读回来之前先别在这页改东西。",
      d.error ? d : {
        error: "返回里没有 provider（拿到的是：" + Object.keys(d).join(", ") + "）",
        hint: "这一页只读 /api/settings —— 请检查那条路由有没有被改动"});
    return;
  }
  SETD = d;
  renderSet(d);
}
async function saveSet() {
  if (!SETD) { $("#setBody").innerHTML = kbFail("设置", {error: "还没读到配置", hint: "点「重新读」"}); return; }
  const cur = SETD.provider || {};
  const want = {
    "provider.model": $("#setModel").value.trim(),
    "provider.base_url": $("#setBaseUrl").value.trim(),
    "provider.reasoning_effort": $("#setEffort").value.trim(),
    "provider.max_tokens": $("#setMax").value.trim(),
    "provider.api_key_env": $("#setEnv").value.trim(),
    "provider.stream_required": $("#setStream").value,
  };
  const body = {};
  const touched = [];
  Object.keys(want).forEach(k => {
    const bare = k.split(".")[1];
    const now = want[k];
    const was = String(cur[bare] == null ? "" : cur[bare]).trim();
    if (k === "provider.max_tokens") {
      if (String(parseInt(now, 10) || 0) !== String(parseInt(was, 10) || 0)) {
        body[k] = now; touched.push(bare);
      }
    } else if (k === "provider.stream_required") {
      const nb = String(now) === "1", ob = !!cur.stream_required;
      if (nb !== ob) { body[k] = nb; touched.push(bare); }
    } else if (now !== was) { body[k] = now; touched.push(bare); }
  });
  if (!touched.length) {
    setRes("#setResBox", false, "没有任何字段被改动 —— 这次不发请求" +
      "（否则会把没改的值也钉进 config.json，把内置默认悄悄变成硬配置）。");
    return;
  }
  $("#btnSetSave").disabled = true;
  setRes("#setResBox", false, "正在写入并重建 agent…（改了 " + touched.join("、") + "）");
  let r, j;
  try {
    r = await fetch("/api/settings", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    j = await r.json().catch(() => ({ error: "响应不是 JSON" }));
  } catch (e) {
    j = { error: "接口没有应答：" + (e && e.message ? e.message : e) };
  }
  $("#btnSetSave").disabled = false;
  if (!r || !r.ok || !j || !j.ok) {
    // 服务端怎么说就怎么显示，一个字都不美化。
    setRes("#setResBox", false, String((j && j.error) || ("HTTP " + (r ? r.status : "?"))));
    // ★ 失败后**不重读**：重读会把输入框刷回服务端旧值，人刚打错的那一下就没了，
    //   只能从头再敲一遍。留着他改的内容，改对再点保存。
    return;
  }
  const np = j.provider || {};
  const lines = [];
  lines.push("写入：" + Object.keys(j.written || {}).map(k => "provider." + k + " = " + j.written[k]).join("；"));
  lines.push("落盘：" + (j.path || "-"));
  lines.push("备份：" + (j.backup || "（原来没有配置文件，这是新建的）"));
  lines.push(j.rebuilt
    ? "已重建 agent —— 现在真正在用的模型是 " + (j.kernel_model || np.model || "-") +
      "。不用重启服务，下一个任务就用新模型。"
    : "配置已落盘，但<b>没能重新装载起来</b>：" + (j.kernel_error || "未知原因") +
      " —— 现在跑不了任务。修好之后按侧栏的「重试装载」（/api/boot）。");
  if (!j.key_present)
    lines.push("⚠ 环境变量 " + (np.api_key_env || "-") +
               " 现在没设 —— 换模型不会失败，但一发起调用就会被服务端拒。");
  setRes("#setResBox", true, lines.join("\n"));
  SETD = null; loadSet(); loadStatus();
}

/* ═════════════ W19-A · 密钥 ═════════════
   为什么单独一个接口而不是混进 saveSet：
   ① 它写的**不是 config.json**（那个在项目根、可能被提交），
      是家目录里的 secrets.json —— 落点不同，回执要说的话也不同。
   ② 它会**真发一次请求**去验证，"保存成功"和"能用"是两件事。
   ③ 失败时**不清空输入框、也不回退已存的值**：密钥可能只是打错一个字符，
      或者网关当时抽风（本项目实测过该网关间歇性长挂起）。 */
async function saveKey(clear) {
  const box = "#setKeyBox";
  const inp = $("#setKey");
  const name = ($("#setEnv") && $("#setEnv").value.trim()) || "";
  const value = clear ? "" : (inp ? inp.value.trim() : "");
  if (!clear && !value) {
    setRes(box, false, "没有填内容 —— 把密钥粘贴到上面那个框里再点。");
    return;
  }
  const btn = clear ? $("#btnKeyClear") : $("#btnKeySave");
  if (btn) btn.disabled = true;
  setRes(box, false, clear ? "正在清掉本机保存的密钥…"
                           : "正在存到本机，并拿它真发一次请求验证…（可能要十几秒）");
  let r, j;
  try {
    r = await fetch("/api/secret", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, value: value }) });
    j = await r.json().catch(() => ({ error: "响应不是 JSON" }));
  } catch (e) {
    j = { error: "接口没有应答：" + (e && e.message ? e.message : e) };
  }
  if (btn) btn.disabled = false;
  if (!r || !r.ok || !j || !j.ok) {
    // 服务端怎么说就怎么显示，一个字都不美化。
    setRes(box, false, String((j && j.error) || ("HTTP " + (r ? r.status : "?"))));
    return;
  }
  if (clear || j.cleared) {
    setRes(box, true, "已清掉 " + (j.name || "-") + " 在本机的值。\n落盘：" + (j.path || "-"));
    if (inp) inp.value = "";
    SETD = null; loadSet(); loadStatus();
    return;
  }
  const lines = [];
  lines.push("已存 " + (j.chars || 0) + " 个字符到：" + (j.path || "-") +
             "（页面上不回显，这里也拿不到它的值）");
  if (j.verified) {
    lines.push("✔ 已验证可用 —— 当场拿它发了一次请求，服务端回了：" +
               (j.verify_sample ? "「" + j.verify_sample + "」" : "正常响应"));
    lines.push("现在就能开任务了，不用重启。");
  } else {
    lines.push("✘ 存是存下了，但**验证没通过**：" + (j.verify_error || "未知原因"));
    lines.push("上面那句是服务端的原话。值**保留着没有删** —— " +
               "如果是密钥打错了，改对再点一次；如果看着是对的，可能是网关当时不稳，过一会儿再试。");
  }
  setRes(box, !!j.verified, lines.join("\n"));
  if (inp) inp.value = "";
  SETD = null; loadSet(); loadStatus();
}

/* ═════════════ composer ═════════════ */
function autoGrow() {
  const t = $("#goal");
  /* 先清内联高度，让 CSS 的 min-height 说话（空态 52 / 会话态 36）。
     旧写法 `height:auto` → `scrollHeight` 会把**上一屏**的 52px 固化进
     内联样式：从空态进入会话后输入框一直比 DSH 高 16px。 */
  t.style.height = "";
  const min = parseFloat(getComputedStyle(t).minHeight) || 36;
  /* ★ 2026-10-31：上限**以 CSS 的 max-height 为准**，不在这里写死 336。
     写死会造出第二套真相：CSS 把这档调到 196 之后，这里仍然按 336 算，
     于是"框只有 196 高、内容按 336 算" —— 中间那 140px 永远看不到，
     而且没有任何报错（本仓的老毛病：两处各记一个数，改一处静默失效）。
     `getComputedStyle().maxHeight` 在 CSS 写了绝对值时返回像素串；没写时是
     "none"，那时退回 336（老行为，别让"没设上限"变成"没有上限"）。 */
  const mx = parseFloat(getComputedStyle(t).maxHeight) || 336;
  t.style.height = Math.min(Math.max(t.scrollHeight, min), mx) + "px";
  /* 长到顶之后，光标可能已经落在滚动区外 —— 手动把它带回视野，
     否则用户打字时看见的是自己几分钟前的第一行。 */
  if (t.scrollHeight > t.clientHeight + 1) t.scrollTop = t.scrollHeight;
  syncComposerSpace();
}
$("#goal").addEventListener("input", () => { autoGrow(); paintSlashMenu(); });
$("#goal").addEventListener("keydown", e => {
  // 斜杠菜单开着时，方向键/回车先归菜单 —— 一次按键只干一件事。
  if (SLASH_OPEN) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = SLASH_HIT.length; if (!n) return;
      SLASH_SEL = (SLASH_SEL + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      paintSlashMenu();
      return;
    }
    if (e.key === "Enter" && !e.isComposing) {
      e.preventDefault();
      const item = SLASH_HIT[SLASH_SEL] || SLASH_HIT[0];
      closeSlashMenu();
      if (item) item[2]();
      return;
    }
    if (e.key === "Escape") { closeSlashMenu(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submitComposer(); }
});
$("#goal").addEventListener("input", () => $("#goal").removeAttribute("aria-invalid"));
$("#btnSend").onclick = submitComposer;
$("#btnPreview").onclick = previewContract;
/* 导师开关已提升为输入区一等控件，不再藏进“更多设置”。 */
$("#btnMorePills").onclick = () => {
  const panel = $("#morePills"), open = panel.hidden;
  panel.hidden = !open;
  $("#btnMorePills").setAttribute("aria-expanded", open ? "true" : "false");
  $("#btnMorePills").textContent = open ? "收起场景 ▴" : "更多场景 ▾";
};
$("#btnStop").onclick = stop;
$("#toolsFilt").addEventListener("input", paintTools);
$("#btnNew").onclick = () => {
  if (LIVE) { $("#hint").innerHTML = '<span style="color:var(--warn)">先停止当前任务</span>'; return; }
  if (ES) { ES.close(); ES = null; }
  CUR = null; TRAJ = { contract:null, result:null }; DRAFT = null;
  $("#thread").hidden = true; $("#empty").hidden = false; $("#wbOverview").hidden = false;
  $("#thread").innerHTML = "";
  $("#cPreview").innerHTML = "";
  autoGrow();
  delete $("#title").dataset.quiet;
  $("#title").textContent = "新任务"; $("#hint").textContent = "";
  /* 新对话 = 开关回到默认关（工单 §3.1「新对话默认关」）。
     不显式置关的话，上一场开着的状态会**串进**这一场 —— 那就是悄悄改变行为。 */
  setMentor(false);
  PRESET = "";             // 新对话 = 预设回到 baseline，别把上一场的尺子带过来
  paintTraj(); paintSessions(); goView("chat");
};
/* ═════════════ 场景表单（点胶囊 → 填槽位 → 后台拼装目标）═════════════
   用户的原话：点胶囊"只需要显示模块选择，不要写一堆文字"。
   模板文字是给内核看的，不是给人看的 —— 表单只露出该填的槽位，
   开工时由 build() 把槽位拼回完整目标（拼出来的文字与原 data-goal 同口径）。
   data-goal 保留：ui_shot 的承重判据 + 无表单配置胶囊的回退路径。 */
/* ★ 能力预设：点场景胶囊 = 选一份预设（判据/门/工具/上下文各不相同）。
   10 个胶囊归并成 5 类任务；这份表是**唯一的**前端映射，index.html 一个字不动。
   baseline / strict-v2 是对照夹具，不进这张表（不给人当任务选）。 */
const PILL_PRESETS = {
  "起草写作": "paper-writing",
  "润色文字": "paper-writing",
  "文献检索": "lit-search",
  "引用核验": "lit-verify",
  "查证说法": "lit-verify",
  "深入精读": "lit-verify",
  "通俗读懂": "lit-verify",
  "统计审查": "stats-review",
  "配图": "figure",
  "汇报 PPT": "deck",
  /* 第 8/9 份预设：前台入口由下面的 mountExtraPresets() 动态补进
     「更多场景」折叠区，index.html 一个字都不用改。 */
  "精简上下文": "lean-context",
  "先答后问": "ext-answer-first",
};
let PRESET = "";            // 会话级：追问继续沿用，#btnNew 才复位
/* 内核回的是预设 id（英文），主视图一律说中文。判定仍只认 id，这张表只管显示。 */
const PRESET_LABELS = {
  "baseline": "默认要求", "strict-v2": "严格核验", "paper-writing": "论文写作",
  "lit-verify": "文献核验", "lit-search": "文献检索 · 插件", "stats-review": "统计审查",
  "figure": "出版级配图", "deck": "汇报幻灯片",
  "ext-answer-first": "外部能力 · 先答后问",
  "lean-context": "精简上下文 · 按任务装配",
};
const PILL_FORMS = {
  "润色文字": {
    opts: ["更通顺", "更简洁", "更正式"], opt0: 0,
    fields: [{ k: "text", label: "要润色的原文", ph: "把原文粘贴在这里", rows: 5 },
             { k: "n", label: "目标字数（留空 = 不限）", ph: "例如 300", rows: 0 }],
    build: (v, opt) => "把下面这段话" + opt + "，保留原意和全部引注" +
      (v.n ? "，目标约 " + v.n + " 字" : "") + "，直接给出改后的版本：\n" + v.text,
  },
  "通俗读懂": {
    fields: [{ k: "text", label: "材料（PDF 拖进材料区，或把要点粘在这里）", ph: "材料要点 / 全文…", rows: 5 }],
    build: v => "用平常人能懂的话讲清楚下面这份材料的重点：它说了什么、结论是什么、值得注意什么：\n" + v.text,
  },
  "查证说法": {
    fields: [{ k: "text", label: "要查证的说法", ph: "把说法写在这里", rows: 3 }],
    build: v => "查证下面这个说法是否属实：给出可靠来源、原文出处与出处里的关键句；查不到就直说查不到，不要猜：\n" + v.text,
  },
  "文献检索": {
    fields: [{ k: "topic", label: "检索主题", ph: "例如：冬虫夏草人工培育", rows: 0 }],
    build: v => "检索 " + v.topic + " 近五年的关键论文，整理成一张对比表：作者、年份、方法、样本量、主要结论，每条都要能追到 DOI",
  },
  "引用核验": {
    fields: [{ k: "text", label: "参考文献列表", ph: "把列表粘贴在这里", rows: 6 }],
    build: v => "核验这份参考文献列表里每一条的 DOI 是否真实有效、是否被撤稿，把有问题的挑出来并给出正确出处：\n" + v.text,
  },
  "深入精读": {
    fields: [{ k: "text", label: "DOI 或论文标题", ph: "贴 DOI 或标题", rows: 0 }],
    build: v => "精读下面这篇论文，拆出它的方法、样本量与指标、主要结论和局限，每条结论都标出它在原文里的位置，并给出两个可接着做的研究问题：\n" + v.text,
  },
  "起草写作": {
    fields: [{ k: "text", label: "题目与要点", ph: "题目、核心论点、想覆盖的内容…", rows: 5 },
             { k: "n", label: "目标字数（留空 = 不限）", ph: "例如 1200", rows: 0 }],
    build: v => "按这个目标起草一份" + (v.n ? "约 " + v.n + " 字" : "") + "的正文，带可核验的引注，交付时附稿面体检报告：\n" + v.text,
  },
  "统计审查": {
    fields: [{ k: "text", label: "结果描述", ph: "把结果描述贴在这里", rows: 5 }],
    build: v => "审查我这份结果的统计报告：实验单元与重复有没有分清、不确定度怎么给的、检验方法与多重比较是否匹配，逐项指出问题与改法：\n" + v.text,
  },
  "配图": {
    fields: [{ k: "text", label: "数据与要求", ph: "x=…，y=…，导出格式…", rows: 3,
               value: "x=[0.5,1.2,2.4,3.1,4.8]，y=[0.31,0.42,0.55,0.61,0.73]，单分面，导出 PNG" }],
    build: v => "按投稿要求做一张出版级配图：" + v.text + "，并给出图注与源数据",
  },
  "汇报 PPT": {
    fields: [{ k: "text", label: "内容", ph: "把论文要点或讲稿大纲贴在这里", rows: 6 }],
    build: v => "把下面这份内容做成组会汇报 PPT：6~20 页、每页配讲稿备注、标题不超过 40 字，产出 .pptx 并附 deck 体检报告：\n" + v.text,
  },
};
let PF_STATE = null;             // 当前挂着的模块条 { cfg, label, opt }
function pillFormOpen(label, chip) {
  /* ★ 点胶囊时**先**锁定预设，再走表单逻辑。全局 PRESET 独立于 PF_STATE：
     pillFormApply() 会清 PF_STATE，但预审面板的「确认执行」仍要带着预设。
     调用处 workspace.js 那一行字面量被 ui_check 钉死，所以只改函数签名。 */
  PRESET = PILL_PRESETS[(chip && chip.dataset.form) || label] || "";
  const cfg = PILL_FORMS[label];
  if (!cfg) return false;        // 无表单配置 → 调用方回退到 data-goal 直填
  PF_STATE = { cfg, label, opt: cfg.opts ? cfg.opts[cfg.opt0 || 0] : null };
  const bar = $("#pillForm");
  const main = cfg.fields[0];
  const rest = cfg.fields.slice(1);
  bar.innerHTML =
    '<span class="pftag">' + esc(label) +
    (PRESET ? " · 按" + esc(PRESET_LABELS[PRESET] || PRESET) + "要求" : "") +
    (cfg.opts ? '<select class="pfsel" id="pfOpt">' + cfg.opts.map(o =>
      '<option' + (o === PF_STATE.opt ? " selected" : "") + ">" + esc(o) + "</option>").join("") + "</select>" : "") +
    '<button class="pfx" id="pfClose" title="收起">✕</button></span>' +
    '<input class="pfi" id="pfMain" data-k="' + main.k + '" placeholder="' + esc(main.ph || "") + '"' +
    (main.value != null ? ' value="' + esc(main.value) + '"' : "") + '>' +
    rest.map(f => '<input class="pfn" data-k="' + f.k + '" data-ph="1" placeholder="' + esc(f.label) + '"' +
      (f.value != null ? ' value="' + esc(f.value) + '"' : "") + '>').join("");
  bar.hidden = false;
  $("#pfClose").onclick = pillFormClose;
  const sel = $("#pfOpt");
  if (sel) sel.onchange = () => { PF_STATE.opt = sel.value; };
  rest.forEach(() => {});        /* 槽位占位：聚焦后 placeholder 由 label 原样提示 */
  $("#pfMain").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); submitComposer(); } });
  $("#pfMain").focus();
  return true;
}
function pillFormClose() { const b = $("#pillForm"); b.hidden = true; b.innerHTML = ""; PF_STATE = null; }
/* 把模块条拼装成完整目标并收起 —— send() / previewContract() 开头各调一次。
   主槽位必填：空着就返回 null 并把输入框标红，不拦发送以外的路。 */
function pillFormApply() {
  if (!PF_STATE) return true;
  const bar = $("#pillForm");
  const main = bar.querySelector("#pfMain");
  const s = (main.value || "").trim();
  if (!s) { main.placeholder = "先填这里，再发送"; main.classList.add("err"); main.focus(); return false; }
  const v = { [PF_STATE.cfg.fields[0].k]: s };
  PF_STATE.cfg.fields.slice(1).forEach(f => {
    const el = bar.querySelector('[data-k="' + f.k + '"]');
    v[f.k] = el ? (el.value || "").trim() : "";
  });
  const goal = PF_STATE.cfg.build(v, PF_STATE.opt);
  pillFormClose();
  $("#goal").value = goal;
  return true;
}
/* ═════════ 后加预设的前台胶囊入口 ═════════
   为什么动态补 DOM 而不改 index.html：预设是「插件容器」，前台入口也该随预设走；
   写死在 HTML 里就得每加一份预设改一次页面（用户明确要求 index.html 一字不动）。
   放进 #morePills 折叠区 ⇒ 首屏仍只露 3 个胶囊，ui_shot 的「默认露出 3 个」
   判据不受影响。★ 必须在下面 .chip 事件绑定之前执行，否则新按钮点了没反应。
   ★ data-goal 必须是能直接跑的完整目标（ui_shot 判 data-goal 非空、能填进输入框）。 */
(function mountExtraPresets() {
  const panel = $("#morePills");
  if (!panel || panel.dataset.extPresets === "1") return;
  const extras = [
    ["精简上下文", "精简上下文",
     "用最精简的上下文把这件事讲清楚：先给结论，再给依据，最后列一条还没核实的点。"],
    ["先答后问", "先答后问",
     "先直接回答我的问题，再说明依据和不确定处；不要一上来就反问，也不要只给提纲。"],
  ];
  extras.forEach(([form, label, goal]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip apill int";   /* 与既有胶囊同款，才能被下面的 .chip 接线拾起 */
    b.dataset.form = form;
    b.dataset.goal = goal;
    b.textContent = label;
    panel.appendChild(b);
  });
  panel.dataset.extPresets = "1";
})();
document.querySelectorAll(".chip").forEach(c => {
  c.onclick = () => {
    if (pillFormOpen(c.dataset.form || "", c)) return;   // 稳定程序键，与显示文案解耦
    $("#goal").value = c.dataset.goal; autoGrow(); $("#goal").focus();
  };
});

/* ═════════════ 材料区（把稿件 / 数据 / PDF 递进来）═════════════
   评审第④条「输入输出离科研工作流太远」的**入口**。没有它，能提的"真活"
   只剩"写一段摘要"—— 稿子、数据、PDF 根本递不进来；第③条那句
   "30 秒跑通第一件真活"也就落不了地。

   三条纪律：
   · **文件名只由服务端洗**（`safe_upload_name`）。前端不承担安全判断 ——
     前端拦了也拦不住绕过前端的请求，两边各写一套反而多一条会漂移的真相。
   · **所见即所发**：材料进来时，把「材料已放进工作区：x.md」这一行写进
     **目标输入框**（用户看得见、改得掉）。绝不偷偷往提示词里塞东西 ——
     悄悄注入的上下文一旦出错，用户连"为什么它没看我的稿子"都查不出来。
   · 运行中不许动材料：与服务端同一条规矩（那边 409），前端先拦一次少一次往返。 */
let MATS = [];

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function renderMats() {
  const host = $("#mats");
  if (!host) return;
  const btn = $("#btnMat");
  if (btn) btn.disabled = LIVE;
  if (!MATS.length) { host.hidden = true; host.innerHTML = ""; return; }
  host.hidden = false;
  host.innerHTML =
    '<span style="font-size:var(--fs-xxxs);color:var(--fg-cap)">材料 ' + MATS.length +
    " 份 · 提交后就在工作区里</span>" +
    MATS.map(m => '<span class="mat" title="' + esc(m.path || m.name) + '"><b>' +
      esc(m.name) + '</b><span class="np">' + esc(fmtSize(m.size)) +
      (m.previewable ? "" : " · 无预览") + "</span>" +
      '<button data-drop="' + esc(m.name) + '" title="移除">×</button></span>').join("") +
    '<span class="matx" id="matClear">清空</span>';
  host.querySelectorAll("[data-drop]").forEach(b => {
    b.onclick = () => dropMaterial(b.dataset.drop);
  });
  const c = $("#matClear");
  if (c) c.onclick = clearMaterials;
}

async function loadMaterials() {
  let left = [];
  try {
    const d = await api("/api/materials");
    MATS = d.materials || [];
    left = d.leftover || [];
  } catch (e) { MATS = []; }
  renderMats();
  // 上次提交有材料没搬进工作区 —— 刷新页面后也要看得见。
  // 不说的话，用户只会发现"它好像没看我那份稿子"，然后无从查起。
  if (left.length) {
    $("#hint").innerHTML = '<span style="color:var(--warn-fg)">上次有 ' + left.length +
      " 份材料没能搬进工作区（还在材料区）：" + esc(left.join("、")) + "</span>";
  }
}

/* 把"材料在工作区里"这条**事实**写进目标框 —— 可改、可删。
   这是"输入离工作流太远"最省事也最诚实的补救：用户不必知道该说什么暗号，
   而发出去的东西仍然全在输入框里摆着。 */
function noteMaterialsInGoal(names) {
  const g = $("#goal");
  if (!g) return;
  const add = names.filter(n => g.value.indexOf(n) < 0);
  if (!add.length) return;
  const line = "（材料已放进工作区：" + add.join("、") + "）";
  const cur = g.value.replace(/\s+$/, "");
  g.value = (cur ? cur + "\n\n" : "") + line;
  autoGrow();
}

async function uploadFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  if (LIVE) {
    $("#hint").innerHTML =
      '<span style="color:var(--warn)">任务在跑，等它结束再动材料</span>';
    return;
  }
  $("#hint").innerHTML = '<span class="spin"></span>正在收下 ' + list.length + " 份材料…";
  let ok = 0;
  const fail = [];
  for (const f of list) {
    try {
      // 一个文件一条请求（裸字节体 + `?name=`）：服务端不必手写 multipart 解析，
      // 而且**哪一份失败了就报哪一份** —— 批量请求会把失败糊成一句"上传失败"。
      const r = await fetch("/api/upload?name=" + encodeURIComponent(f.name),
                            { method: "POST", body: f });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
      // 名字以**服务端返回的**为准：它可能被洗过（`con.txt` → `_con.txt`）。
      MATS = j.materials || MATS;
      noteMaterialsInGoal([j.item.name]);
      ok++;
    } catch (e) { fail.push(f.name + "：" + e.message); }
  }
  renderMats();
  $("#hint").innerHTML = fail.length
    ? '<span style="color:var(--err)">' + fail.length + " 份没收下 —— " +
      esc(fail.join("；")) + "</span>"
    : '<span style="color:var(--ok-fg)">收下 ' + ok + " 份材料，已放进工作区</span>";
}

/* 移除一份材料。`quiet` 用于"清空"这种连点场景：逐条弹提示只会刷屏，
   由调用方在循环结束后统一说一句。

   ★ 服务端返回 `note` 时**照着它说**，不自作主张写"已移除"：
   原件可能挪不动（占用/权限），那时界面必须让用户知道它还在哪儿 ——
   以前这里把失败渲染成成功，是"两副面孔"里最坏的一种。 */
async function dropMaterial(name, quiet) {
  let d = null;
  try {
    d = await api("/api/material_drop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    MATS = d.materials || [];
    if (!quiet && d.note) {
      $("#hint").innerHTML = '<span style="color:var(--warn-fg)">' + esc(d.note) + "</span>";
    }
  } catch (e) {
    $("#hint").innerHTML = '<span style="color:var(--err)">' + esc(e.message) + "</span>";
  }
  renderMats();
  return d;
}

async function clearMaterials() {
  const n = MATS.length;
  let stuck = 0;
  for (const m of MATS.slice()) {
    const d = await dropMaterial(m.name, true);   // 逐条安静地撤，末尾统一说
    if (d && !d.moved) stuck++;
  }
  // 汇总也**分开报**：挪走了几份、有几份还在盘上。
  $("#hint").innerHTML = stuck
    ? '<span style="color:var(--warn-fg)">已从清单移除 ' + n + " 份；其中 " + stuck +
      " 份原件没挪动（还在材料区，可再试或手工处理）</span>"
    : '<span style="color:var(--fg-3)">已移除 ' + n + " 份材料（原件挪到了 _removed，可找回）</span>";
}

/* 拖放：接在 document 上，但**只在真有文件时**才接管 ——
   否则会吃掉页面里正常的拖拽（比如选文字）。 */
function wireDrop() {
  const mask = $("#dropmask");
  const hasFiles = e => !!e.dataTransfer &&
    Array.from(e.dataTransfer.types || []).indexOf("Files") >= 0;
  let depth = 0;
  document.addEventListener("dragenter", e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    if (!LIVE && mask) mask.classList.add("on");
  });
  document.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener("dragleave", e => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);          // 进出子元素会连发，计数才不会闪
    if (!depth && mask) mask.classList.remove("on");
  });
  document.addEventListener("drop", e => {
    if (!hasFiles(e)) return;
    // 这一句必须先行：不拦，浏览器会**打开/下载**那个文件，整个页面就跳走了。
    e.preventDefault();
    depth = 0;
    if (mask) mask.classList.remove("on");
    uploadFiles(e.dataTransfer.files);
  });
  const pick = $("#matFile");
  $("#btnMat").onclick = () => pick.click();
  pick.onchange = () => { uploadFiles(pick.files); pick.value = ""; };
}

/* ═════════════ 回放（打开 /?run=<id> 或点历史）═════════════ */
/* 回放里"这一轮用户说了什么"：
   · 首轮 = 归档的 goal；
   · 追问轮 = `follow_up` 事件里的原文（服务端在新归档里补了 text）；
   · 旧归档只有 chars 时如实写成"没留原文"，不编一句话。 */
function replayUserText(t, first) {
  const fu = (t.events || []).find(e => e.kind === "follow_up");
  if (!first && fu) {
    const dd = fu.data || {};
    if (dd.text) return String(dd.text);
    if (dd.chars) return "（旧归档只记了 " + dd.chars + " 个字符，原文没留下）";
    return "";
  }
  return t.goal || "";
}

async function replay(id) {
  let d;
  try { d = await api("/api/run?id=" + encodeURIComponent(id)); }
  catch (e) {
    $("#hint").innerHTML = '<span style="color:var(--err)">回放失败：' + esc(e.message) + "</span>";
    return;
  }
  $("#empty").hidden = true; $("#thread").hidden = false;
  resetComposerSpace();
  autoGrow();
  resetThread();
  $("#cPreview").innerHTML = "";
  CUR = { id, goal: d.goal || id };
  TRAJ = { contract: null, result: null };
  LIVE = false; T0 = Date.now();
  /* 回放开始前先无条件置关 —— 下面那圈事件里若有 `mentor_mode`，
     它会把开关摆回**那次会话当时**的位置；没有那条事件就是当时没开。
     顺序很重要：不先清掉，上一场残留的"开"会被读成这一场也开（串台）。 */
  setMentor(false);
  delete $("#title").dataset.quiet;
  $("#title").textContent = d.goal || id;
  // 必须在 finish() **之前**设：finish→renderResult→decisions 当场就要读它。
  RESUMABLE = !!d.resumable;
  // 回放完也能**接着说** —— 服务端如实回答快照在不在（followable），
  // 界面照它说，不自己猜。
  CUR.followable = !!d.followable;

  /* 追问过的会话：服务端把追问前的父轮也从 `turns/` 交回来。
     逐轮渲染，**每轮都建自己的过程组和结果卡** —— 旧实现每次 finish()
     都覆写全局 #resultCard，于是追问一次，上一轮的成果就被抹掉了。 */
  const prior = Array.isArray(d.turns) ? d.turns : [];
  const chain = prior.concat([{ goal: d.goal, events: d.events,
                                result: d.result, error: d.error, _current: true }]);
  const rn = $("#replayNote");
  if (rn) {
    rn.hidden = false;
    rn.innerHTML = "<span>▤</span><b>这是历史回放</b><span>" +
      (chain.length > 1 ? "共 " + chain.length + " 轮 · " : "") +
      (d.done ? "那次已结束" : "那次仍在进行") + " · " +
      (d.events || []).length + " 个事件 · 读的是磁盘归档，不是重跑</span>";
  }
  REPLAY_FOLLOWUPS = Array.isArray(d.followups) ? d.followups.slice() : [];
  REPLAY_FU_I = 0;

  for (let i = 0; i < chain.length; i++) {
    const t = chain[i];
    const first = i === 0;
    const text = replayUserText(t, first);
    if (text) beginUserTurn(text, { history: true, label: first ? "" : "追问" });
    const at = beginAssistantTurn({ history: true });
    /* 回放里只有**最后一轮**是"当前轮"（id、收件箱、稿件台都归它）；
       历史轮只管把自己画出来。 */
    at.current = (i === chain.length - 1);
    if (!at.current) TURN.current = false;
    if (!text && !first) {
      const fu = (t.events || []).find(e => e.kind === "follow_up");
      note("这一轮是你接着说的话（旧归档没留原文，只记了 " +
           ((fu && fu.data && fu.data.chars) || "?") + " 个字符）", "prog");
    }
    for (const ev of (t.events || [])) {
      // 追问轮的原文已经画成用户气泡了，别再让事件画第二遍；
      // 契约条也只在第一轮出现（它是同一份任务的验收口径，不是新任务）。
      if (!first && (ev.kind === "follow_up" || ev.kind === "contract" ||
                     ev.kind === "contract_drafting")) continue;
      await onEvent({ kind: ev.kind, data: ev.data || {} });
    }
    if (t.result || t.error) {
      if (t._current) RESUMABLE = !!d.resumable;
      finish({ result: t.result, error: t.error, replay: true }, at);
    } else {
      endTurn(at);
    }
  }
  $("#hint").textContent = "历史回放 · 在下面输入框继续说，它会带着前情接着做";
  /* 回放从第一轮开始读（不自动跳到底）；若不在底部就把「回到最新」亮出来。 */
  FOLLOWING = nearBottom();
  paintToBottom();
  // 打开一个**仍在等你回答**的任务时，卡必须回来 ——
  // 否则刷新一次页面，界面就再也看不到"它在等"，而内核还在原地等 300s：
  // 两边各说各话（B 型），用户只会觉得"点了没反应"。
  if (d.ask) {
    renderAsk(d.ask);
    phase("✋", askWhy(d.ask), "它停下来等你一句话 —— 回答，或直接让它继续", "warn");
  }
}

/* ═════════════ 稿件台（右侧阅读抽屉）═════════════
   为什么不直接把成果卡里的正文放大：
     卡片是「一次任务的摘要」，正文只是它的一节（后面还有产出物、证据、技术细节）。
     而 .res .manu 有 max-height:460px —— 长稿只能在那个小框里滚。
     稿子该有自己的地方。这是 Claude Artifacts、vercel/ai-chatbot 的 artifact 面板
     与 harness-deck 的共同结论：**对话是控制面，产物是载荷**。
   只搬它们的**功能**，不引依赖 —— 这里没有框架、没有 CDN、没有构建步骤。

   ★ 三层降级是实测逼出来的：归档里的稿是**整段无换行**的连续中文
     （runs/<run_id>/session.json：最长那份 176 字、只有 1 行）。
     只认 markdown `#` 会得到"一个大段落 + 空大纲"—— 而**空壳大纲比没有大纲更糟**：
     它承诺了导航却什么都不给。所以：标题标记 → 空行分段 → 句群分块，
     最后一层保证任何稿都有可点的导航。
   ⚠️ 写这条注释时踩了个经典坑：把归档路径写成通配符（runs 后面接 星号 再接 session.json）
     时，那个 星号+斜杠 的组合会**提前闭合块注释** ⇒ 后半句变成裸代码，整个 script 语法错。
     ⚠️ 更蠢的是：修它的那条注释里我又写了一次"星号斜杠"两个字符，于是**同一个坑原地复发**。
     ⇒ 纪律：块注释里**永远不要出现那两个字符连写**，不管你是不是在解释它。
        要举例就写"星号加斜杠"。 */
/* 稿件台逻辑见 reader.js。 */
