# -*- coding: utf-8 -*-
"""文献库主工作区（library.js / library.css）的静态与行为契约。

## 这个文件测什么、为什么这么测

`library.js` 是本页唯一**新写**的前端模块，它要同时做到三件互相拉扯的事：

  1. **搬节点而不重绑**：`#kbQ/#kbGo/#kbOut/#kbFindQ/#kbFile/...` 这些控件与
     workspace.js 里挂好的 `kbSearch()/kbListAll()/kbFind()/kbUpload()` 必须
     原样继续工作 —— 一旦这里再绑一次，事件处理器会被覆盖或重复，而
     `KB_SEARCH_BUSY` 会变成两套状态（点了没反应，或者双发请求）。
  2. **真实服务端分页**：翻页/筛选必须进 query 串，不能在前端切数组。
  3. **所有外部数据过 esc**：题名/期刊/作者/摘要/正文都来自库文件或工具回包，
     直接拼 innerHTML 就是一个 XSS。

第 2、3 条只有**真跑一遍**才看得见，读代码看不出来（拼错的参数名、
漏掉的一次转义，语法都合法）。所以这里用 Node 起一个极小的 DOM 桩，把
`library.js` 真的加载进去，用假 `api()` 喂回包，然后按**渲染出来的 HTML
字符串**断言。`index.html` 那侧只在 Python 里做静态核对（id 是否还在、
脚本顺序、初始化调用点）。

用法：python library_ui_test.py
退出码：0 全过 / 1 有失败
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "rongke" / "web"
STATIC = WEB / "static"
JS = STATIC / "library.js"
CSS = STATIC / "library.css"
HTML = WEB / "index.html"

# 行首 ✔/✘ 在 GBK 控制台上会抛 UnicodeEncodeError（实测：跑到第一条判据就炸，
# 于是"全过"永远看不到）。与仓里其它测试同一处治法：先把两条流换成 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

FAILED: list[str] = []
PASSED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED
    print(("✔ " if ok else "✘ ") + label + ("" if ok else ("   ← " + detail if detail else "")))
    if ok:
        PASSED += 1
    else:
        FAILED.append(label)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.is_file() else ""


# ══════════════════ 0. 文件在位 ══════════════════
js = read(JS)
css = read(CSS)
html = read(HTML)
check("library.js 存在且非空", bool(js.strip()), str(JS))
check("library.css 存在且非空", bool(css.strip()), str(CSS))
if not js.strip() or not css.strip():
    print("✘ 缺文件，后面的判据无法进行")
    sys.exit(1)

# ══════════════════ 1. 语法 / 传统脚本（无构建）══════════════════
node = shutil.which("node")
check("本机有 node（行为判据要用）", bool(node), "找不到 node，无法验证运行行为")
if node:
    r = subprocess.run([node, "--check", str(JS)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    check("library.js 通过 node --check", r.returncode == 0, (r.stderr or "")[:300])

check("传统脚本：不含 import/export", not re.search(r"(?m)^\s*(?:import|export)\s", js))
# "只暴露一个全局"要**数**出来，不能只看有没有那一行：`window.xxx = 1` 里
# 少写一处 RongkeLibrary 就会被别的页面看见（本仓纪律：不造全局变量）。
winrefs = re.findall(r"window\.([A-Za-z_$][\w$]*)", js)
extra = sorted(set(winrefs) - {"RongkeLibrary"})
check("只读 window 上除 RongkeLibrary 外的属性 / 不新造全局",
      not extra,
      "还动了 window." + "、window.".join(extra))
check("导出四件：init/refresh/open/search",
      all(k in js for k in ("function init", "function refresh",
                            "function openItemById", "function search")))

# ══════════════════ 2. 不重复绑定既有控件 ══════════════════
KEEP = ["kbStat", "kbAiNote", "kbFindQ", "kbFindGo", "kbFindOut", "kbFile",
        "kbSend", "kbSendOff", "kbUpOut", "kbQ", "kbGo", "kbOut",
        "kbSearchHint", "kbAll"]
absent_html = [i for i in KEEP if ('id="%s"' % i) not in html]
check("index.html 仍保留全部既有控件", not absent_html, "缺：" + "、".join(absent_html))

# 搬过来的控件不许在这里重新挂处理器：出现 `.onclick =` 或者对这些 id 的
# addEventListener 都算重绑（workspace.js 已经挂过一份）。
rebind = re.findall(r"""\.onclick\s*=""", js)
check("library.js 里没有任何 .onclick 赋值（不抢既有绑定）", not rebind,
      "出现 %d 处" % len(rebind))
targeted = re.findall(r"""addEventListener\(\s*["'](?:click|keydown|change)["']""", js)
# 读既有 id 是必须的（要找到那几张卡片搬走），**绑**它们才犯规。
binding = re.findall(r"""byId\(\s*["']kb(?:Q|Go|All|Out|Find|File|Send|Up)[\w]*["']\s*\)\s*\.\s*(?:onclick|addEventListener)""", js)
check("addEventListener 只挂在自建容器与自建控件上（不针对既有 id）",
      not binding and len(targeted) <= 12,
      "绑定了既有 id %d 处 / addEventListener %d 处" % (len(binding), len(targeted)))

check("不调用 init/refresh 之外的既有全局（kbSearch/kbListAll 只在注释里出现）",
      "kbSearch()" not in js.replace("`kbSearch()`", "").replace("kbSearch()`", "")
      or js.count("kbSearch") <= 3, "kbSearch 出现 %d 次" % js.count("kbSearch"))

# ══════════════════ 3. 接口契约（参数名照 library_api.py）═════════════════
check("列表走 /api/kb/list", "/api/kb/list" in js)
for name in ("page=", "page_size=", "q=", "source=", "sort=", "order="):
    check("列表参数带上 " + name.rstrip("="), name in js)
check("读取走 /api/kb/read", "/api/kb/read" in js)
check("读取带 doc_id 与 page_from",
      "doc_id=" in js and "page_from=" in js)
check("单次跨度常量 = 20（library_api.KB_READ_MAX_SPAN）",
      re.search(r"const\s+SPAN\s*=\s*20\b", js) is not None)
check("有 PDF 复用 openOrig({kb,title,n})",
      "openOrig({" in js and "kb:" in js and "title:" in js and "n:" in js)
check("库况复用 loadKb（不另写一条 stat 通路）",
      "if (typeof loadKb === 'function') loadKb();" in js
      and not re.search(r"""["'`]/api/kb/stat""", js),
      "自己发了 stat 请求，或者没调 loadKb")
check("不展示 BibTeX 导出按钮（该路由不存在）",
      "bibtex" not in js.lower())

# ══════════════════ 4. 安全：转义与竞态 ══════════════════
check("定义并使用 esc()", re.search(r"const\s+esc\s*=", js) is not None
      and js.count("esc(") >= 25, "esc( 用了 %d 次" % js.count("esc("))
raw_interp = re.findall(r"\+\s*(?:it|d|r)\s*\.\s*(?:title|journal|abstract|authors|text)\b", js)
check("没有把外部字段直接拼进 HTML（必须过 esc）", not raw_interp,
      "发现 %d 处" % len(raw_interp))
check("有异步序列号（防竞态）", "++state.seq" in js and "seq !== state.seq" in js)
check("详情读取另有独立序列号（列表与正文互不作废）",
      "++state.readSeq" in js and "seq !== state.readSeq" in js)

# ══════════════════ 5. CSS：作用域 / 令牌 / 响应式 ══════════════════
hexcol = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
check("CSS 不写死色值（只用令牌变量）", not hexcol, "发现 " + "、".join(hexcol[:5]))
check("CSS 不用 !important", "!important" not in css)

tokens = read(STATIC / "tokens.css") + read(STATIC / "workspace.css")
used = set(re.findall(r"var\(\s*(--[A-Za-z0-9_-]+)", css))
defined = set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", tokens))
undef = sorted(used - defined)
check("CSS 变量都在 tokens/workspace 里有定义", not undef, "未定义：" + "、".join(undef[:8]))

check("窄屏（375 档）有专门布局", "@media (max-width:620px)" in css)
check("窄屏下表格转块级（无横向溢出）",
      "display:block" in css and ".kbwork .kwtable td" in css
      and "thead{display:none}" in css.replace(" ", ""))

# 共用零件不许被这里改成全局规则：.card/.kbtable/.kbrow/.kbtn 在别的页也在用。
bad_global = []
for sel in re.findall(r"(?m)^([^@{}\n][^{}\n]*)\{", css):
    s = sel.strip()
    if not s or s.startswith("@") or s.startswith("/*"):
        continue
    if ".kbwork" in s or "#v-kb" in s or s.startswith("@media"):
        continue
    if re.search(r"\.(?:card|kbtable|kbrow|mini|blank)\b", s):
        bad_global.append(s)
check("没有把共用类写成全局规则", not bad_global, "、".join(bad_global[:5]))

# ══════════════════ 6. index.html 接线 ══════════════════
check("index.html 引了 library.css", "/static/library.css" in html)
check("index.html 引了 library.js 且在 workspace.js 之后",
      html.find("/static/library.js") > html.find("/static/workspace.js") > -1)
check("init 在 workspace.js 之后调用（此时既有函数已定义）",
      html.find("RongkeLibrary.init()") > html.find("/static/workspace.js") > -1)
check("导航刷新点存在（RongkeLibrary.refresh 由 workspace 调）",
      "RongkeLibrary.refresh()" in read(STATIC / "workspace.js"))
check("导航有 loadKb 兜底（万一 library.js 没加载）",
      "else loadKb();" in read(STATIC / "workspace.js"))

# ══════════════════ 7. 行为：真加载 library.js，假 DOM + 假 api ══════════════════
PROBE = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const SRC = fs.readFileSync(process.argv[2], 'utf8');

let pass = 0;
const failed = [];
function check(label, ok, detail) {
  console.log((ok ? '✔ ' : '✘ ') + label + (ok ? '' : ('   ← ' + (detail || ''))));
  if (ok) pass++; else failed.push(label);
}

/* ── 极小 DOM 桩：够 library.js 用即可（createElement/getElementById/
      className/id/hidden/value/innerHTML/appendChild/insertBefore/
      children/parentNode/nextSibling/setAttribute/getAttribute/listeners）── */
const reg = new Map();
const all = [];
function node(tag) {
  const n = {
    tagName: String(tag).toUpperCase(),
    children: [], attributes: {}, listeners: {}, dataset: {},
    parentNode: null, hidden: false, disabled: false, value: '',
    textContent: '', _html: '', _class: '', _id: '',
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(this.attributes, k)
        ? this.attributes[k] : null;
    },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    appendChild(c) {
      if (c.parentNode) {
        const i = c.parentNode.children.indexOf(c);
        if (i >= 0) c.parentNode.children.splice(i, 1);
      }
      c.parentNode = this; this.children.push(c); return c;
    },
    insertBefore(c, ref) {
      if (c.parentNode) {
        const i = c.parentNode.children.indexOf(c);
        if (i >= 0) c.parentNode.children.splice(i, 1);
      }
      c.parentNode = this;
      const at = ref ? this.children.indexOf(ref) : -1;
      if (at < 0) this.children.push(c); else this.children.splice(at, 0, c);
      return c;
    }
  };
  Object.defineProperty(n, 'className', {
    get() { return this._class; },
    set(v) { this._class = String(v == null ? '' : v); }
  });
  Object.defineProperty(n, 'id', {
    get() { return this._id; },
    set(v) {
      if (this._id) reg.delete(this._id);
      this._id = String(v == null ? '' : v);
      if (this._id) reg.set(this._id, this);
    }
  });
  Object.defineProperty(n, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v == null ? '' : v); this.children = []; }
  });
  Object.defineProperty(n, 'nextSibling', {
    get() {
      if (!this.parentNode) return null;
      const cs = this.parentNode.children, i = cs.indexOf(this);
      return i >= 0 && i + 1 < cs.length ? cs[i + 1] : null;
    }
  });
  all.push(n);
  return n;
}
const document = {
  createElement: node,
  getElementById: id => reg.get(id) || null,
  querySelector: () => null
};

/* ── 夹具：照 index.html 里 v-kb 的结构搭一份（class 必须真实，
      因为 library.js 是拿 .card 上溯找容器的）── */
function el(tag, cls, id) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (id) n.id = id;
  return n;
}
const page = el('div', 'page');
const statCard = el('div', 'card');
const statH3 = el('h3');
const kbHint = el('span', 'hint', 'kbHint');
statH3.appendChild(kbHint);
statCard.appendChild(statH3);
const kbStat = el('div', '', 'kbStat');
statCard.appendChild(kbStat);
const kbAiNote = el('div', '', 'kbAiNote');
statCard.appendChild(kbAiNote);
page.appendChild(statCard);

const segCard = el('div', 'card');
const segH3 = el('h3');
const kbSearchHint = el('span', 'hint', 'kbSearchHint');
segH3.appendChild(kbSearchHint);
segCard.appendChild(segH3);
const segSub = el('div', 'sub'); segCard.appendChild(segSub);
const segFilt = el('div', 'filt');
const ipt = el('span', 'ipt');
const kbQ = el('input', '', 'kbQ');
ipt.appendChild(kbQ); segFilt.appendChild(ipt);
const kbGo = el('button', 'mini', 'kbGo');
const kbAll = el('button', 'mini', 'kbAll');
segFilt.appendChild(kbGo); segFilt.appendChild(kbAll);
segCard.appendChild(segFilt);
const kbOut = el('div', '', 'kbOut');
segCard.appendChild(kbOut);
page.appendChild(segCard);

const findCard = el('div', 'card');
findCard.appendChild(el('h3'));
const findFilt = el('div', 'filt');
const kbFindQ = el('input', '', 'kbFindQ');
const kbFindGo = el('button', 'mini pri', 'kbFindGo');
findFilt.appendChild(kbFindQ); findFilt.appendChild(kbFindGo);
findCard.appendChild(findFilt);
const kbFindOut = el('div', '', 'kbFindOut');
findCard.appendChild(kbFindOut);
page.appendChild(findCard);

const upCard = el('div', 'card');
upCard.appendChild(el('h3'));
const upFilt = el('div', 'filt');
const kbFile = el('input', '', 'kbFile');
const kbSend = el('button', 'mini', 'kbSend');
const kbSendOff = el('button', 'mini', 'kbSendOff');
upFilt.appendChild(kbFile); upFilt.appendChild(kbSend); upFilt.appendChild(kbSendOff);
upCard.appendChild(upFilt);
const kbUpOut = el('div', '', 'kbUpOut');
upCard.appendChild(kbUpOut);
page.appendChild(upCard);

const host = el('div', 'page');
host.appendChild(page);

/* workspace.js 早就挂好的处理器：用哨兵函数表示，事后核"没被覆盖" */
const mark = {};
['kbGo', 'kbAll', 'kbFindGo', 'kbSend', 'kbSendOff'].forEach(k => {
  mark[k] = function sentinel() { mark[k].called = true; };
  reg.get(k).onclick = mark[k];
});
const beforeNode = {};
['kbQ', 'kbGo', 'kbAll', 'kbOut', 'kbSearchHint', 'kbFindQ', 'kbFindGo',
 'kbFindOut', 'kbFile', 'kbSend', 'kbSendOff', 'kbUpOut', 'kbStat', 'kbAiNote']
  .forEach(k => { beforeNode[k] = reg.get(k); });

/* ── 假 api：按 url 给回包，必要时挂起（验证竞态）── */
const calls = [];
let handler = () => ({ ok: true, total: 0, items: [], page: 1, page_size: 20,
                       has_more: false, showing: 0, note: '' });
const pending = [];
function defer(url) {
  return new Promise(res => { pending.push({ url: url, res: res }); });
}
async function api(url) { calls.push(url); return handler(url); }
/* ★ 这里刻意**不只**记次数：loadKb() 的真实实现开头就是
   `if (out.dataset.filled !== "1") kbListAll();`，而 kbListAll() 发的是
   `/api/kb/list?limit=500` —— 整库一次拉回。桩必须复现这一行，否则
   "不会触发整库拉取"这条判据永远是假绿（它根本没机会发生）。 */
let kbLoaded = 0;
let legacyPulls = 0;
function kbListAll() {
  legacyPulls++;
  return api('/api/kb/list?limit=500');
}
function loadKb() {
  kbLoaded++;
  const out = reg.get('kbOut');
  if (out && out.dataset.filled !== '1') kbListAll();
  return Promise.resolve();
}
const origCalls = [];
function openOrig(o) { origCalls.push(o); }
function kbClean(t) { return String(t == null ? '' : t).replace(/<\/?[a-z]+>/gi, ''); }
function errMsg(e) { return e && e.message ? String(e.message) : String(e); }

const win = {};
const sandbox = {
  window: win, document: document, api: api, loadKb: loadKb,
  openOrig: openOrig, kbClean: kbClean, errMsg: errMsg,
  console: console, Promise: Promise, setTimeout: setTimeout
};
vm.runInNewContext(SRC, sandbox, { filename: 'library.js' });
const RL = win.RongkeLibrary;

/* 顶层 await 在 CJS 里不合法（探针第一版就是这么炸的：一条判据都没跑）。
   所以判据整体关进一个 async 函数里。 */
(async () => {

check('导出 window.RongkeLibrary', !!RL);
check('导出四件（init/refresh/open/search）',
      !!RL && ['init', 'refresh', 'open', 'search'].every(k => typeof RL[k] === 'function'));

const wait = () => new Promise(r => setTimeout(r, 0));

/* 找节点的小工具 */
function byClass(cls) {
  return all.filter(n => (' ' + n.className + ' ').indexOf(' ' + cls + ' ') >= 0);
}
function lastByClass(cls) { const l = byClass(cls); return l[l.length - 1]; }
/* 注意：表格行是 innerHTML 字符串，不是 DOM 节点 —— 「有几行」只能数
   渲染出来的 HTML，不能数节点（第一版就是把它当节点数，永远得 0）。 */
function byAttr(k, v) {
  return all.filter(n => (v === undefined
    ? n.getAttribute(k) !== null
    : n.getAttribute(k) === v));
}
function shell() { return byClass('kbwork')[0]; }
/* 造一个"点在某个 data-kbact 元素上"的事件。library.js 只用到
   target.closest(selector)，所以这里给一个最小的 closest 就够。 */
function fire(act, extra) {
  const attrs = { 'data-kbact': act };
  Object.keys(extra || {}).forEach(k => { attrs[k] = extra[k]; });
  const el = {
    tagName: 'BUTTON',
    getAttribute: k => (Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null),
    closest: () => el
  };
  const ev = { target: el, key: 'Enter', preventDefault() {} };
  const s = shell();
  (s.listeners.click || []).forEach(fn => fn(ev));
}
function fireKey(act, extra) {
  const attrs = { 'data-kbact': act };
  Object.keys(extra || {}).forEach(k => { attrs[k] = extra[k]; });
  const el = {
    tagName: 'TR',
    getAttribute: k => (Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null),
    closest: () => el
  };
  const ev = { target: el, key: 'Enter', preventDefault() {} };
  (shell().listeners.keydown || []).forEach(fn => fn(ev));
}

/* ── 7.1 初始化：建壳、搬节点、不重绑 ── */
handler = url => ({ ok: true, total: 2, page: 1, page_size: 20, has_more: false,
                    showing: 2, note: '',
                    items: [
                      { id: 'kbA', title: '甲文', year: '2024', journal: '菌物学报',
                        status: 'read', fulltext: true, has_pdf: true },
                      { id: 'kbB', title: '乙文', year: '2023', journal: 'Mycologia',
                        status: 'unread', fulltext: false, has_pdf: false }
                    ] });
RL.init();
check('init 后建出主工作区 .kbwork', !!shell());
check('库况复用 loadKb（被调用一次）', kbLoaded === 1, 'kbLoaded=' + kbLoaded);
check('★ 不再触发 loadKb 顺手发起的 limit=500 整库拉取',
      legacyPulls === 0, '仍发了 ' + legacyPulls + ' 次');
check('★ 也没有任何请求带 limit=500（老路径彻底不走）',
      !calls.some(u => u.indexOf('limit=500') >= 0),
      calls.filter(u => u.indexOf('limit=500') >= 0).join(' | '));
check('板段列表被换成一句指路（而不是空白）',
      reg.get('kbOut').innerHTML.indexOf('按段落查') >= 0,
      reg.get('kbOut').innerHTML.slice(0, 120));
check('工作区挂在库况卡片之后（同容器）',
      !!shell() && shell().parentNode === page);
check('init 就发出一次列表请求', calls.some(u => u.indexOf('/api/kb/list') === 0));

await wait();

check('既有控件节点身份不变（搬的是节点，不是重建）',
      Object.keys(beforeNode).every(k => reg.get(k) === beforeNode[k]));
check('既有 onclick 未被覆盖（kbGo/kbAll/kbFindGo/kbSend/kbSendOff）',
      Object.keys(mark).every(k => reg.get(k).onclick === mark[k]));
check('没有产生第二个 #kbGo 之类的重定义',
      all.filter(n => n.id === 'kbGo').length === 1);
check('段落检索卡片被搬进工作区',
      !!shell() && byClass('kwpanel').some(p => p.children.indexOf(segCard) >= 0));
check('AI 候选卡片被搬进「新增文献」',
      byClass('kwpanel').some(p => p.children.indexOf(findCard) >= 0));
check('本地 PDF 卡片被搬进「新增文献」',
      byClass('kwpanel').some(p => p.children.indexOf(upCard) >= 0));
check('段落卡的旧标题被隐掉（馆藏已另立 tab）', segH3.hidden === true);

/* ── 7.2 tab 切换 ── */
const tabBtns = byAttr('data-kbtab');
check('三个 tab 都在', tabBtns.length === 3, '实际 ' + tabBtns.length);
fire('tab', { 'data-kbtab': 'seg' });
check('切到段落检索：aria-selected 跟着走',
      byAttr('data-kbtab', 'seg')[0].getAttribute('aria-selected') === 'true'
      && byAttr('data-kbtab', 'col')[0].getAttribute('aria-selected') === 'false');
check('切到段落检索：面板显隐正确',
      byAttr('data-kbpanel', 'seg')[0].hidden === false
      && byAttr('data-kbpanel', 'col')[0].hidden === true);
fire('tab', { 'data-kbtab': 'col' });

/* ── 7.3 列表：服务端分页参数 + 渲染 + 转义 ── */
const list = lastByClass('kwlist');
const firstList = calls.filter(u => u.indexOf('/api/kb/list') === 0)[0];
check('首屏请求 page=1&page_size=20', /[?&]page=1\b/.test(firstList)
      && /[?&]page_size=20\b/.test(firstList), firstList);
check('默认排序 year/desc（与 parse_list_params 一致）',
      /[?&]sort=year\b/.test(firstList) && /[?&]order=desc\b/.test(firstList), firstList);
check('默认 source=all', /[?&]source=all\b/.test(firstList), firstList);
check('空 q 仍然显式带上（不让服务端猜）', /[?&]q=/.test(firstList), firstList);
check('渲染出两条数据行（按渲染出的 HTML 数，行不是 DOM 节点）',
      (lastByClass('kwlist').innerHTML.match(/class="kwrow"/g) || []).length === 2,
      '实际 ' + (lastByClass('kwlist').innerHTML.match(/class="kwrow"/g) || []).length);
check('年份/期刊/能读到 都在',
      list.innerHTML.indexOf('2024') >= 0 && list.innerHTML.indexOf('菌物学报') >= 0
      && list.innerHTML.indexOf('有原文') >= 0 && list.innerHTML.indexOf('仅元数据') >= 0);
check('翻页条给出总条数与页码',
      list.innerHTML.indexOf('共 2 篇') >= 0 && list.innerHTML.indexOf('第 1/1 页') >= 0);

/* XSS 负控：把恶意字段喂进去，必须被转义；同时验证"检测器本身能认出坏串" */
handler = url => ({ ok: true, total: 1, page: 1, page_size: 20, has_more: false,
                    showing: 1, note: '',
                    items: [{ id: 'kbX', title: '<img src=x onerror=alert(1)>',
                              year: '<b>2020</b>', journal: '"><script>x</script>',
                              status: 'unread', fulltext: false, has_pdf: false }] });
fire('apply');
await wait();
const xhtml = lastByClass('kwlist').innerHTML;
check('★ XSS：题名里的 <img> 被转义', xhtml.indexOf('<img') < 0
      && xhtml.indexOf('&lt;img') >= 0, xhtml.slice(0, 200));
check('★ XSS：期刊里的引号+脚本被转义', xhtml.indexOf('<script') < 0
      && xhtml.indexOf('&lt;script') >= 0);
check('★ XSS：年份里的标签被转义', xhtml.indexOf('<b>2020') < 0);
/* 负控：如果检测器连"未转义"都认不出来，上面三条就是假绿。 */
const detector = s => s.indexOf('<img') >= 0 || s.indexOf('<script') >= 0;
check('负控：检测器能认出未转义串（否则上面三条是假绿）',
      detector('<img src=x>') && detector('<script>x</script>')
      && !detector('&lt;img src=x&gt;'));

/* ── 7.4 筛选与翻页真的进 query ── */
const srcSel = byAttr('data-kbact', 'source')[0];
const sortSel = byAttr('data-kbact', 'sort')[0];
const orderSel = byAttr('data-kbact', 'order')[0];
const sizeSel = byAttr('data-kbact', 'size')[0];
const qIn = reg.get('kbColQ');
check('筛选控件齐全（输入 + source/sort/order/page_size）',
      !!qIn && !!srcSel && !!sortSel && !!orderSel && !!sizeSel);
check('每页档位给的是 20 / 40（服务端上限 500，但界面不铺那么长）',
      sizeSel.innerHTML.indexOf('value="20"') >= 0
      && sizeSel.innerHTML.indexOf('value="40"') >= 0);

handler = () => ({ ok: true, total: 0, items: [], page: 1, page_size: 20,
                   has_more: false, showing: 0 });
calls.length = 0;
qIn.value = ' 原生质体 融合 ';
srcSel.value = 'pdf';
srcSel.listeners.change[0]();
await wait();
const qUrl = calls.filter(u => u.indexOf('/api/kb/list') === 0).pop() || '';
check('按 source 筛选发出 source=pdf', /[?&]source=pdf\b/.test(qUrl), qUrl);
check('q 做了 URL 编码并去掉首尾空白',
      qUrl.indexOf('q=%E5%8E%9F%E7%94%9F%E8%B4%A8%E4%BD%93') >= 0, qUrl);
check('改筛选回到第 1 页', /[?&]page=1\b/.test(qUrl), qUrl);

calls.length = 0;
sizeSel.value = '40';
sizeSel.listeners.change[0]();
await wait();
const sUrl = calls.filter(u => u.indexOf('/api/kb/list') === 0).pop() || '';
check('每页 40 进 page_size', /[?&]page_size=40\b/.test(sUrl), sUrl);

calls.length = 0;
sortSel.value = 'title';
sortSel.listeners.change[0]();
await wait();
const tUrl = calls.filter(u => u.indexOf('/api/kb/list') === 0).pop() || '';
check('换排序字段后方向自动拨回该字段默认（title→asc）',
      /[?&]sort=title\b/.test(tUrl) && /[?&]order=asc\b/.test(tUrl)
      && orderSel.value === 'asc', tUrl);

/* ── 7.5 翻页 ── */
handler = () => ({ ok: true, total: 45, page: 1, page_size: 20, has_more: true,
                   showing: 20, items: [{ id: 'kbA', title: 'p1', year: '2024',
                                          status: 'unread', fulltext: true,
                                          has_pdf: false }] });
fire('apply');
await wait();
check('有下一页时「下一页」可点',
      lastByClass('kwlist').innerHTML.indexOf('data-kbact="next"') >= 0
      && lastByClass('kwlist').innerHTML.indexOf('data-kbact="next" disabled') < 0);
check('第 1 页时「上一页」是禁用的',
      lastByClass('kwlist').innerHTML.indexOf('data-kbact="prev" disabled') >= 0);
calls.length = 0;
handler = () => ({ ok: true, total: 45, page: 2, page_size: 20, has_more: true,
                   showing: 20, items: [{ id: 'kbB', title: 'p2', year: '2023',
                                          status: 'unread', fulltext: false,
                                          has_pdf: false }] });
fire('next');
await wait();
const p2Url = calls.filter(u => u.indexOf('/api/kb/list') === 0).pop() || '';
check('翻页请求带 page=2（真去服务端取）', /[?&]page=2\b/.test(p2Url), p2Url);
check('第二页渲染成新的那一页', lastByClass('kwlist').innerHTML.indexOf('p2') >= 0);

/* ── 7.6 三种状态不许混 ── */
handler = () => ({ ok: true, total: 0, items: [], page: 1, page_size: 20,
                   has_more: false, showing: 0 });
qIn.value = '';
srcSel.value = 'all';
fire('apply');
await wait();
const emptyHtml = lastByClass('kwlist').innerHTML;
check('空库说「库里还没有文献」', emptyHtml.indexOf('库里还没有文献') >= 0, emptyHtml);
check('空库不说成失败（没有 .kbwarn）', emptyHtml.indexOf('kbwarn') < 0);

qIn.value = '不存在的词';
fire('apply');
await wait();
const noHit = lastByClass('kwlist').innerHTML;
check('筛选没命中 ≠ 空库（两句话不同）',
      noHit.indexOf('筛选没命中') >= 0 && noHit.indexOf('库里还没有文献') < 0, noHit);

handler = () => ({ ok: false, error: '荣珂看不到 kb_list', hint: '接线断了' });
fire('apply');
await wait();
const errHtml = lastByClass('kwlist').innerHTML;
check('工具失败说成「读不出来（这不是库空了）」',
      errHtml.indexOf('这不是库空了') >= 0, errHtml);
check('工具失败带出错误原文与修法',
      errHtml.indexOf('荣珂看不到 kb_list') >= 0 && errHtml.indexOf('接线断了') >= 0);
check('工具失败不冒充空库（没有「库里还没有文献」）',
      errHtml.indexOf('库里还没有文献') < 0);
check('工具失败给重试按钮', errHtml.indexOf('data-kbact="apply"') >= 0);

/* ── 7.7 详情：元数据 / 状态说明 / 按页正文 / 原页 ── */
const ITEMS = [
  { id: 'kbA', title: '<i>甲文</i>', year: '2024', journal: '菌物学报',
    status: 'read', fulltext: true, has_pdf: true, doi: '10.1/abc',
    pages: '12-30', tags: ['冬虫夏草', '转化'],
    authors: ['张三', '李四'], abstract: '<script>alert(2)</script>摘要正文' },
  { id: 'kbB', title: '乙文', year: '2023', journal: 'Mycologia',
    status: 'unread', fulltext: false, has_pdf: false }
];
handler = url => {
  if (url.indexOf('/api/kb/list') === 0) {
    return { ok: true, total: 2, page: 1, page_size: 20, has_more: false,
             showing: 2, items: ITEMS };
  }
  if (url.indexOf('/api/kb/read') === 0) {
    return { ok: true, doc_id: 'kbA', title: '甲文', pages_total: 40,
             page_from: 1, page_to: 20, page_range: [1, 20], truncated: false,
             chars: 100, char_total: 900, has_more_pages: true,
             text: '第一段正文 <b>不该被当成标签</b>' };
  }
  return { ok: true };
};
qIn.value = '';
srcSel.value = 'all';
fire('apply');
await wait();
calls.length = 0;
delete reg.get('kbB');
const pOpen = RL.open('kbA');
await wait();
const det = byClass('kwdet')[0];
check('打开详情走 /api/kb/read?doc_id=kbA&page_from=1',
      calls.some(u => u.indexOf('/api/kb/read?doc_id=kbA') >= 0
                      && u.indexOf('page_from=1') >= 0),
      calls.filter(u => u.indexOf('/api/kb/read') === 0).join(' | '));
check('详情显示年份/期刊/DOI/页码/标签/作者',
      ['2024', '菌物学报', '10.1/abc', '12-30', '冬虫夏草', '张三']
        .every(s => det.innerHTML.indexOf(s) >= 0), det.innerHTML.slice(0, 300));
check('详情有状态说明（三档里的一档 + 一句为什么）',
      det.innerHTML.indexOf('有原文') >= 0
      && det.innerHTML.indexOf('能看原页') >= 0);
check('详情给出库内编号', det.innerHTML.indexOf('kbA') >= 0);
check('★ 摘要里的 <script> 被转义',
      det.innerHTML.indexOf('<script') < 0 && det.innerHTML.indexOf('&lt;script') >= 0);
check('★ 正文里的 <b> 被转义（正文是纯文本渲染）',
      det.innerHTML.indexOf('&lt;b&gt;') >= 0
      && det.innerHTML.indexOf('<b>不该被当成标签') < 0);
check('按页读：给出页范围与总页数',
      det.innerHTML.indexOf('第 1–20 页') >= 0
      && det.innerHTML.indexOf('共 40 页') >= 0, det.innerHTML.slice(0, 400));
check('有 PDF ⇒ 出现「看原页」', det.innerHTML.indexOf('data-kbact="orig"') >= 0);
fire('orig', { 'data-kbid': 'kbA' });
check('看原页复用 openOrig({kb,title,n})',
      origCalls.length === 1 && origCalls[0].kb === 'kbA'
      && typeof origCalls[0].title === 'string' && origCalls[0].n === 1,
      JSON.stringify(origCalls));

calls.length = 0;
handler = url => {
  if (url.indexOf('/api/kb/read') === 0) {
    return { ok: true, doc_id: 'kbA', pages_total: 40, page_from: 21,
             page_to: 40, page_range: [21, 40], truncated: false, chars: 10,
             char_total: 900, has_more_pages: false, text: '第二段' };
  }
  return { ok: true };
};
fire('rnext');
await wait();
const rUrl = calls.filter(u => u.indexOf('/api/kb/read') === 0).pop() || '';
check('「下一段」从上一段的末页 +1 开始（=21，不重叠也不跳页）',
      /[?&]page_from=21\b/.test(rUrl), rUrl);

/* ── 7.8 正文读不出来的四种情况分得开 ── */
function throwApi(status, body) {
  handler = () => { const e = new Error((body && body.error) || 'x');
                    e.status = status; e.body = body; throw e; };
}
throwApi(409, { ok: false, error: 'kb_read: no fulltext cache for kbB' });
fire('rfirst');
await wait();
let d409 = byClass('kwdet')[0].innerHTML;
check('409 说「这篇在库里，但没有抽出来的正文」（不冒充空文档）',
      d409.indexOf('没有抽出来的正文') >= 0, d409.slice(0, 300));
check('409 给出补正文的下一步', d409.indexOf('本地 PDF') >= 0);

throwApi(400, { ok: false, error: '页码越界：这篇共 40 页，请求的是 999..1018',
                hint: 'page_from / page_to 是 1 起的页码' });
fire('rfirst');
await wait();
const d400 = byClass('kwdet')[0].innerHTML;
check('400 说「页码不对」并给「从第 1 页读」', d400.indexOf('页码不对') >= 0
      && d400.indexOf('从第 1 页读') >= 0, d400.slice(0, 300));

throwApi(503, { ok: false, error: '荣珂看不到 kb_read', hint: 'workbench 未挂载' });
fire('rfirst');
await wait();
const d503 = byClass('kwdet')[0].innerHTML;
check('503 说「通路没接上」，并明说「不是这篇没有内容」',
      d503.indexOf('通路没接上') >= 0 && d503.indexOf('不是这篇没有内容') >= 0);

/* ── 7.9 竞态：先发的慢回包不许覆盖后发的 ──
   桩的写法有个坑（第一版就是栽在这里，且**退出码是 0**）：把「要不要挂起」
   做成一个布尔开关，开关开着的时候两次请求就都被挂起，事后只 resolve 了
   一个，另一个 Promise 永远悬着 —— node 的事件循环一空就以 0 退出，剩下的
   判据一条都不跑，看上去像"通过"。所以这里改成**按次数**挂起：只挂起第 1 次
   命中的请求，后面的正常回包。 */
handler = url => {
  if (url.indexOf('/api/kb/list') === 0) {
    return { ok: true, total: 2, page: 1, page_size: 20, has_more: false,
             showing: 2, items: ITEMS };
  }
  return { ok: true };
};
qIn.value = '';
fire('apply');
await wait();
pending.length = 0;

let deferRead = 1;                       // 只挂起第一条 /api/kb/read
const base = handler;
handler = url => {
  if (deferRead && url.indexOf('/api/kb/read') === 0) {
    deferRead -= 1;
    return defer(url);
  }
  return base(url);
};
const p1 = RL.open('kbA');
await wait();
const p2 = RL.open('kbB');
await wait();
check('后一次详情请求先落地：标题是乙文',
      byClass('kwdet')[0].innerHTML.indexOf('乙文') >= 0);
check('先发的那一条确实还挂着（否则这组判据测不到竞态）',
      pending.length === 1, 'pending=' + pending.length);
/* 现在才把先发的那份回包放出来（它已经过时了） */
const stale = pending.shift();
stale.res({ ok: true, doc_id: 'kbA', pages_total: 5, page_from: 1,
            page_to: 5, page_range: [1, 5], truncated: false,
            chars: 5, char_total: 5, has_more_pages: false,
            text: '迟到的旧正文' });
await p1; await p2; await wait();
const afterRace = byClass('kwdet')[0].innerHTML;
check('★ 迟到的旧回包被序列号挡掉（正文里没有「迟到的旧正文」）',
      afterRace.indexOf('迟到的旧正文') < 0, afterRace.slice(0, 300));
check('★ 详情仍停在乙文上', afterRace.indexOf('乙文') >= 0);

/* 列表竞态：连点两次筛选，只有最后一次的回包生效 */
pending.length = 0;
let deferList = 1;                       // 只挂起第一条 /api/kb/list
const base2 = handler;
handler = url => {
  if (deferList && url.indexOf('/api/kb/list') === 0) {
    deferList -= 1;
    return defer(url);
  }
  return base2(url);
};
qIn.value = '旧';
fire('apply');
await wait();
qIn.value = '新';
fire('apply');
await wait();
const late = pending.shift();
check('先发的那条列表请求还挂着（否则测不到竞态）', !!late);
if (late) late.res({ ok: true, total: 1, page: 1, page_size: 20, has_more: false,
                     showing: 1, note: '', items: [{ id: 'kbOLD',
                       title: '旧筛选的迟到结果', year: '1999',
                       status: 'unread', fulltext: false, has_pdf: false }] });
await wait();
check('★ 迟到的列表回包被挡掉（列表里没有旧结果）',
      lastByClass('kwlist').innerHTML.indexOf('旧筛选的迟到结果') < 0,
      lastByClass('kwlist').innerHTML.slice(0, 200));

/* ── 7.10 搜索跳页：命中在第 37 页要能直接落到那一页 ── */
/* 这是「检索命中 → 读那一页」这条主路径的关键：命中信息里带着页码，
   如果打开详情永远从第 1 页开始，用户就得自己一段一段往后翻 ——
   等于把命中信息扔了。所以 open(id, page) 必须把页码翻译成 page_from，
   并且对齐到 20 页跨度（服务端一次只给 20 页，单页起始会浪费窗口）。 */
handler = url => {
  if (url.indexOf('/api/kb/list') === 0) {
    return { ok: true, total: 2, page: 1, page_size: 20, has_more: false,
             showing: 2, items: ITEMS };
  }
  return { ok: true, doc_id: 'kbA', pages_total: 60, page_from: 21, page_to: 40,
           page_range: [21, 40], truncated: false, chars: 90, char_total: 900,
           has_more_pages: true, text: '第三十七页附近' };
};
qIn.value = '';
fire('apply');
await wait();
calls.length = 0;
RL.open('kbA', 37);
await wait();
const jumpUrl = calls.filter(u => u.indexOf('/api/kb/read') === 0).pop() || '';
check('★ 命中第 37 页 → page_from=21（对齐 20 页跨度，窗口覆盖 37）',
      /[?&]page_from=21\b/.test(jumpUrl), jumpUrl);
check('★ 跳页时不自己补 page_to（越界判定只对显式那端生效）',
      jumpUrl.indexOf('page_to') < 0, jumpUrl);
check('★ 详情确实停在第 21–40 页那一段',
      byClass('kwdet')[0].innerHTML.indexOf('第 21–40 页') >= 0,
      byClass('kwdet')[0].innerHTML.slice(0, 240));

calls.length = 0;
RL.open('kbA', 1);
await wait();
check('★ 命中第 1 页 → page_from=1', /[?&]page_from=1\b/.test(
      calls.filter(u => u.indexOf('/api/kb/read') === 0).pop() || ''));
calls.length = 0;
RL.open('kbA', 40);
await wait();
check('★ 命中第 40 页 → 仍落在 21–40 这一窗（不跳到 41）',
      /[?&]page_from=21\b/.test(calls.filter(u => u.indexOf('/api/kb/read') === 0).pop() || ''));
calls.length = 0;
RL.open('kbA', 41);
await wait();
check('★ 命中第 41 页 → 落到下一窗 page_from=41',
      /[?&]page_from=41\b/.test(calls.filter(u => u.indexOf('/api/kb/read') === 0).pop() || ''));

/* ── 7.11 空搜索词与外部入口 ── */
qIn.value = '';
handler = url => ({ ok: true, total: 0, items: [], page: 1, page_size: 20,
                    has_more: false, showing: 0 });
RL.search('冬虫夏草');
await wait();
check('search() 把词放进 q 并回到第 1 页',
      calls.some(u => u.indexOf('/api/kb/list') === 0
                      && u.indexOf(encodeURIComponent('冬虫夏草')) >= 0));
check('search() 停在馆藏 tab',
      byAttr('data-kbtab', 'col')[0].getAttribute('aria-selected') === 'true');
check('search() 之后的老路径依然没被触发', legacyPulls === 0,
      'legacyPulls=' + legacyPulls);

/* 行上的回车/空格也要能打开详情（表格行不是 <button>） */
handler = url => {
  if (url.indexOf('/api/kb/list') === 0) {
    return { ok: true, total: 1, page: 1, page_size: 20, has_more: false,
             showing: 1, items: [ITEMS[1]] };
  }
  return { ok: true, doc_id: 'kbB', pages_total: 0, page_from: 1, page_to: 20,
           page_range: [], truncated: false, chars: 0, char_total: 0,
           has_more_pages: false, text: '正文' };
};
fire('apply');
await wait();
calls.length = 0;
fireKey('open', { 'data-kbid': 'kbB' });
await wait();
check('表格行按回车也能打开详情', calls.some(u => u.indexOf('/api/kb/read?doc_id=kbB') >= 0));

console.log('');
if (failed.length) {
  console.log('✘ 行为判据失败 ' + failed.length + ' 项：' + failed.join('、'));
  process.exit(1);
}
console.log('✔ 行为判据全过（' + pass + ' 项）');

})().catch(e => {
  /* 探针自己崩了也要留下痕迹：否则 Python 那边只会看到"没有输出"，
     分不清是判据失败还是探针写坏了。 */
  console.log('✘ 行为探针异常：' + (e && e.stack ? e.stack.split('\n')[0] : e));
  process.exit(1);
});
"""

probe = ROOT / "runs" / "_uicheck" / "library_probe.js"
if node:
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(PROBE, encoding="utf-8")
    r = subprocess.run([node, str(probe), str(JS)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    for line in out.splitlines():
        if line.startswith("✔ "):
            PASSED += 1
        elif line.startswith("✘ "):
            FAILED.append(line[2:])
    print(out)
    if r.returncode != 0 and not (r.stdout or "").strip():
        print("✘ 行为探针没有输出")
        print((r.stderr or "")[:1500])
        FAILED.append("行为探针没有输出")

print("")

# ══════════════════ 8. 与真服务端函数对账 ══════════════════
# 上面第 7 节用的是**手写**的回包。手写回包的问题是：它按我以为的契约写，
# 于是"我以为的契约"错了也测不出来（参数名拼错、键名换了，全都照样绿）。
# 这一节把 `rongke/web/library_api.py` 里**真的** `list_payload` /
# `read_payload` 跑起来（fetch 用桩喂一个假库），拿它们真正吐出来的 body
# 喂给 library.js —— 契约对不上就会在这里炸。
sys.path.insert(0, str(ROOT))
try:
    from rongke.web import library_api as LA
except Exception as e:                                    # noqa: BLE001
    check("能导入 rongke.web.library_api（真契约对账的前提）", False, repr(e))
    LA = None

if LA is not None:
    LIB_ROOT = str(ROOT / "runs" / "_uicheck" / "fakelib")
    # `has_pdf` 是**盘上事实**（`<库根>/pdfs/<id>.pdf` 在不在），不是工具字段。
    # 所以想测到「有原文」这一档，就必须真的放一个文件在那儿 —— 这正好也
    # 顺带证明了这一档不是靠某个标志位猜出来的。
    pdfdir = Path(LIB_ROOT) / "pdfs"
    pdfdir.mkdir(parents=True, exist_ok=True)
    (pdfdir / "kbAAA.pdf").write_bytes(b"%PDF-1.4\n% fake\n")
    if (pdfdir / "kbBBB.pdf").exists():
        (pdfdir / "kbBBB.pdf").unlink()
    FAKE_DOCS = [
        # 故意做成三种可读程度各一条，且题名带脏标签（真实库里 85/251 条如此）
        {"id": "kbAAA", "title": "<i>Cordyceps militaris</i> 的固态发酵",
         "year": "2024", "status": "read", "fulltext": True,
         "journal": "菌物学报", "doi": "10.13346/j.mycosystema.240001",
         "pages": "1-12", "tags": ["冬虫夏草", "发酵"]},
        {"id": "kbBBB", "title": "Ophiocordyceps sinensis 多糖",
         "year": "2023", "status": "unread", "fulltext": True,
         "journal": "Mycologia"},
        {"id": "kbCCC", "title": "只有题录的一篇", "year": "2019",
         "status": "unread", "fulltext": False},
    ]

    def fake_fetch(name, **kw):
        if name == "kb_list":
            return {"ok": True, "items": [dict(d) for d in FAKE_DOCS],
                    "matched": 3, "total": 3, "library": LIB_ROOT}
        if name == "kb_read":
            did = kw.get("doc_id")
            lo, hi = kw.get("page_from"), kw.get("page_to")
            if did == "kbCCC":
                return {"ok": False, "doc_id": did,
                        "error": "no fulltext cache for kbCCC"}
            return {"ok": True, "doc_id": did, "title": "t", "pages_total": 60,
                    "page_from": lo, "page_to": hi, "page_range": [lo, hi],
                    "page_max": 60, "truncated": False,
                    "pages": {"%d" % p: "第 %d 页的正文" % p
                              for p in range(lo, hi + 1)},
                    "text": "第 %d..%d 页的正文" % (lo, hi)}
        if name == "kb_meta":
            return {"ok": True, "items": {}}
        return {"ok": False, "error": "未知工具 " + name}

    code, body = LA.list_payload(
        fake_fetch, qs={"page": ["1"], "page_size": ["20"], "sort": ["year"],
                        "order": ["desc"], "source": ["all"]},
        library="litkb", tools=("kb_list", "kb_read"), library_root=lambda: LIB_ROOT)
    check("真 list_payload 回 200 且 ok", code == 200 and body.get("ok"), str(body)[:200])

    FRONT_KEYS = ["ok", "total", "items", "page", "page_size", "has_more", "showing"]
    missing = [k for k in FRONT_KEYS if k not in body]
    check("前端读的列表键在真回包里都有", not missing, "缺：" + "、".join(missing))
    ITEM_KEYS = ["id", "title", "year", "status", "fulltext", "has_pdf"]
    miss_it = [k for k in ITEM_KEYS
               if body.get("items") and k not in body["items"][0]]
    check("前端读的条目键在真回包里都有", not miss_it, "缺：" + "、".join(miss_it))

    # 分页在服务端真的切片（而不是回全量让前端自己切）
    _, p2 = LA.list_payload(
        fake_fetch, qs={"page": ["2"], "page_size": ["2"]}, library="litkb",
        tools=("kb_list",), library_root=lambda: LIB_ROOT)
    check("真 list_payload 按 page/page_size 切片",
          len(p2["items"]) == 1 and p2["page"] == 2 and p2["page_size"] == 2
          and p2["total"] == 3, str({k: p2[k] for k in ("page", "page_size",
                                                        "total", "showing")}))
    check("第 2 页 has_more 为假（3 篇 / 每页 2）", p2["has_more"] is False)

    # source 筛选在服务端生效
    _, only_meta = LA.list_payload(
        fake_fetch, qs={"source": ["meta"]}, library="litkb",
        tools=("kb_list",), library_root=lambda: LIB_ROOT)
    check("真 source=meta 只回仅元数据那一条",
          [it["id"] for it in only_meta["items"]] == ["kbCCC"],
          str([it["id"] for it in only_meta["items"]]))
    _, only_pdf = LA.list_payload(
        fake_fetch, qs={"source": ["pdf"]}, library="litkb",
        tools=("kb_list",), library_root=lambda: LIB_ROOT)
    check("真 source=pdf 只回盘上真有原件的那一条（kbAAA 放了 pdf）",
          [it["id"] for it in only_pdf["items"]] == ["kbAAA"],
          str([it["id"] for it in only_pdf["items"]]))

    # 非法参数在服务端就 400（前端不该靠"猜"来兜）
    code_bad, body_bad = LA.list_payload(
        fake_fetch, qs={"page": ["abc"]}, library="litkb",
        tools=("kb_list",), library_root=lambda: LIB_ROOT)
    check("真 list_payload 对非法 page 回 400 + 中文错因",
          code_bad == 400 and isinstance(body_bad.get("error"), str)
          and body_bad["error"], str(body_bad)[:160])

    # 读取：单次跨度 >20 必须 400（前端的 SPAN=20 就是照这个来的）
    code_span, body_span = LA.read_payload(
        fake_fetch, doc_id="kbAAA", page_from="1", page_to="21",
        library="litkb", tools=("kb_read", "kb_list"))
    check("真 read_payload 对跨度 21 页回 400", code_span == 400,
          str(body_span)[:160])

    # 只给 page_from 时窗口往后 20 页（前端正是只传 page_from）
    code_ok, body_ok = LA.read_payload(
        fake_fetch, doc_id="kbAAA", page_from="21", library="litkb",
        tools=("kb_read", "kb_list"))
    check("真 read_payload 只给 page_from=21 ⇒ 窗口 21..40",
          code_ok == 200 and body_ok["page_from"] == 21
          and body_ok["page_to"] == 40, str(body_ok)[:200])
    READ_KEYS = ["ok", "pages_total", "page_from", "page_to", "page_range",
                 "truncated", "text"]
    miss_r = [k for k in READ_KEYS if k not in body_ok]
    check("前端读的读取键在真回包里都有", not miss_r, "缺：" + "、".join(miss_r))

    # 没有全文 ⇒ 409 且**带 doc_id**（前端靠 status=409 分开说"没正文"）
    code_409, body_409 = LA.read_payload(
        fake_fetch, doc_id="kbCCC", page_from="1", library="litkb",
        tools=("kb_read", "kb_list"))
    check("真 read_payload 对无全文的条目回 409", code_409 == 409, str(body_409)[:160])
    check("真 409 回包带着 doc_id（前端据此说「不是空文档」）",
          body_409.get("doc_id") == "kbCCC")

    # 工具没注册 ⇒ 503（前端据此说「通路没接上」）
    code_503, _ = LA.read_payload(fake_fetch, doc_id="kbAAA", library="litkb",
                                  tools=("kb_list",))
    check("真 read_payload 在没有 kb_read 时回 503", code_503 == 503)

    # ── 把真回包喂给 library.js：渲染出来的东西必须说得通 ──
    REAL = {
        "list": body,
        "list_meta": only_meta,
        "read": body_ok,
        "read_409_code": code_409,
        "read_409": body_409,
        "read_503_code": code_503,
    }
    import json
    real_json = ROOT / "runs" / "_uicheck" / "library_real.json"
    real_json.write_text(json.dumps(REAL, ensure_ascii=False), encoding="utf-8")

    REAL_PROBE = r"""
'use strict';
const fs = require('fs'), vm = require('vm');
const SRC = fs.readFileSync(process.argv[2], 'utf8');
const REAL = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
let pass = 0; const failed = [];
function check(l, ok, d) {
  console.log((ok ? '✔ ' : '✘ ') + l + (ok ? '' : '   ← ' + (d || '')));
  if (ok) pass++; else failed.push(l);
}
const reg = new Map(), all = [];
function node(tag) {
  const n = { tagName: String(tag).toUpperCase(), children: [],
    attributes: {}, listeners: {}, dataset: {}, parentNode: null,
    hidden: false, value: '', textContent: '', _html: '', _class: '', _id: '',
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k)
      ? this.attributes[k] : null; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    appendChild(c) { if (c.parentNode) { const i = c.parentNode.children.indexOf(c);
      if (i >= 0) c.parentNode.children.splice(i, 1); }
      c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) { if (c.parentNode) { const i = c.parentNode.children.indexOf(c);
      if (i >= 0) c.parentNode.children.splice(i, 1); }
      c.parentNode = this; const at = ref ? this.children.indexOf(ref) : -1;
      if (at < 0) this.children.push(c); else this.children.splice(at, 0, c);
      return c; } };
  Object.defineProperty(n, 'className', { get() { return this._class; },
    set(v) { this._class = String(v == null ? '' : v); } });
  Object.defineProperty(n, 'id', { get() { return this._id; },
    set(v) { if (this._id) reg.delete(this._id);
      this._id = String(v == null ? '' : v); if (this._id) reg.set(this._id, this); } });
  Object.defineProperty(n, 'innerHTML', { get() { return this._html; },
    set(v) { this._html = String(v == null ? '' : v); this.children = []; } });
  Object.defineProperty(n, 'nextSibling', { get() { if (!this.parentNode) return null;
    const cs = this.parentNode.children, i = cs.indexOf(this);
    return i >= 0 && i + 1 < cs.length ? cs[i + 1] : null; } });
  all.push(n); return n;
}
const document = { createElement: node,
  getElementById: id => reg.get(id) || null, querySelector: () => null };
function el(t, c, i) { const n = node(t); if (c) n.className = c; if (i) n.id = i; return n; }
const page = el('div', 'page');
const statCard = el('div', 'card');
const h1 = el('h3'); h1.appendChild(el('span', 'hint', 'kbHint'));
statCard.appendChild(h1);
statCard.appendChild(el('div', '', 'kbStat'));
statCard.appendChild(el('div', '', 'kbAiNote'));
page.appendChild(statCard);
const segCard = el('div', 'card');
const h2 = el('h3'); h2.appendChild(el('span', 'hint', 'kbSearchHint'));
segCard.appendChild(h2);
const f1 = el('div', 'filt');
const i1 = el('span', 'ipt'); i1.appendChild(el('input', '', 'kbQ')); f1.appendChild(i1);
f1.appendChild(el('button', 'mini', 'kbGo'));
f1.appendChild(el('button', 'mini', 'kbAll'));
segCard.appendChild(f1);
segCard.appendChild(el('div', '', 'kbOut'));
page.appendChild(segCard);
const findCard = el('div', 'card'); findCard.appendChild(el('h3'));
const f2 = el('div', 'filt');
f2.appendChild(el('input', '', 'kbFindQ'));
f2.appendChild(el('button', 'mini', 'kbFindGo'));
findCard.appendChild(f2);
findCard.appendChild(el('div', '', 'kbFindOut'));
page.appendChild(findCard);
const upCard = el('div', 'card'); upCard.appendChild(el('h3'));
const f3 = el('div', 'filt');
f3.appendChild(el('input', '', 'kbFile'));
f3.appendChild(el('button', 'mini', 'kbSend'));
f3.appendChild(el('button', 'mini', 'kbSendOff'));
upCard.appendChild(f3);
upCard.appendChild(el('div', '', 'kbUpOut'));
page.appendChild(upCard);
const host = el('div', 'page'); host.appendChild(page);

let handler = () => REAL.list;
const seen = [];
async function api(url) { seen.push(url);
  if (url.indexOf('/api/kb/read') === 0) {
    if (url.indexOf('kbCCC') >= 0) {
      const e = new Error(REAL.read_409.error);
      e.status = REAL.read_409_code; e.body = REAL.read_409; throw e;
    }
    return REAL.read;
  }
  if (url.indexOf('source=meta') >= 0) return REAL.list_meta;
  return handler(url);
}
const win = {};
vm.runInNewContext(SRC, {
  window: win, document: document, api: api,
  loadKb: function () {}, openOrig: function () {},
  kbClean: t => String(t == null ? '' : t).replace(/<\/?[a-z]+>/gi, ''),
  errMsg: e => (e && e.message ? String(e.message) : String(e)),
  console: console, Promise: Promise, setTimeout: setTimeout
}, { filename: 'library.js' });
const RL = win.RongkeLibrary;
const wait = () => new Promise(r => setTimeout(r, 0));
function byClass(c) { return all.filter(n => (' ' + n.className + ' ')
  .indexOf(' ' + c + ' ') >= 0); }
function lastByClass(c) { const l = byClass(c); return l[l.length - 1]; }
function fire(act, extra) {
  const a = { 'data-kbact': act };
  Object.keys(extra || {}).forEach(k => { a[k] = extra[k]; });
  const t = { tagName: 'BUTTON',
    getAttribute: k => (Object.prototype.hasOwnProperty.call(a, k) ? a[k] : null),
    closest: () => t };
  (byClass('kbwork')[0].listeners.click || []).forEach(f => f({ target: t, key: 'Enter' }));
}

(async () => {
  RL.init();
  await wait();
  const html = lastByClass('kwlist').innerHTML;
  check('真回包渲染出 3 行', (html.match(/class="kwrow"/g) || []).length === 3,
        (html.match(/class="kwrow"/g) || []).length + ' 行');
  check('真回包的年份出现在表里', html.indexOf('2024') >= 0);
  check('真回包的期刊出现在表里', html.indexOf('菌物学报') >= 0);
  check('真回包的三档胶囊各自出现（有原文/只有文本/仅元数据）',
        html.indexOf('有原文') >= 0 && html.indexOf('只有文本') >= 0
        && html.indexOf('仅元数据') >= 0);
  check('★ 真回包里带 <i> 的题名被清成纯文本',
        html.indexOf('<i>Cordyceps') < 0, html.slice(0, 200));
  check('翻页条用真 total 算页数（3 篇 / 每页 20 ⇒ 1 页）',
        html.indexOf('共 3 篇') >= 0 && html.indexOf('第 1/1 页') >= 0);

  fire('apply', {});                      /* 先确认整库那一屏 */
  await wait();
  /* source=meta 那一路：必须真的把下拉拨到 meta 再触发 change，
     否则发的还是 source=all（第一版就是这么"测"了个寂寞）。 */
  const srcSel = all.filter(n => n.getAttribute('data-kbact') === 'source')[0];
  check('找到 source 下拉', !!srcSel);
  srcSel.value = 'meta';
  (srcSel.listeners.change || []).forEach(f => f());
  await wait();
  const metaHtml = lastByClass('kwlist').innerHTML;
  check('真 source=meta 回包只渲染那一条',
        (metaHtml.match(/class="kwrow"/g) || []).length === 1
        && metaHtml.indexOf('只有题录的一篇') >= 0, metaHtml.slice(0, 200));

  /* 打开详情前把筛选拨回 all：这时列表里才有 kbAAA。否则 open() 会
     按"当前筛选下找不到"如实回 404（那是对的行为，但测不到正文渲染）。 */
  srcSel.value = 'all';
  (srcSel.listeners.change || []).forEach(f => f());
  await wait();
  RL.open('kbAAA');
  await wait();
  const det = byClass('kwdet')[0].innerHTML;
  check('真 read 回包渲染出正文', det.indexOf('第 21..21 页的正文') >= 0
        && det.indexOf('第 40..40 页的正文') >= 0,
        det.slice(0, 300));
  check('真 read 回包的窗口写进标题行（第 21–40 页 / 共 60 页）',
        det.indexOf('第 21–40 页') >= 0 && det.indexOf('共 60 页') >= 0);

  RL.open('kbCCC');
  await wait();
  const d409 = byClass('kwdet')[0].innerHTML;
  check('★ 真 409 回包被说成「没有抽出来的正文」而不是加载失败',
        d409.indexOf('没有抽出来的正文') >= 0, d409.slice(0, 300));
  check('★ 真 409 里工具的原话也带出来了',
        d409.indexOf('no fulltext cache') >= 0);

  console.log('');
  if (failed.length) {
    console.log('✘ 真契约对账失败 ' + failed.length + ' 项：' + failed.join('、'));
    process.exit(1);
  }
  console.log('✔ 真契约对账全过（' + pass + ' 项）');
})().catch(e => {
  console.log('✘ 真契约探针异常：' + (e && e.stack ? e.stack.split('\n')[0] : e));
  process.exit(1);
});
"""
    rp = ROOT / "runs" / "_uicheck" / "library_real_probe.js"
    rp.write_text(REAL_PROBE, encoding="utf-8")
    r = subprocess.run([node, str(rp), str(JS), str(real_json)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = (r.stdout or "").strip()
    for line in out.splitlines():
        if line.startswith("✔ "):
            PASSED += 1
        elif line.startswith("✘ "):
            FAILED.append(line[2:])
    print(out)
    if not out:
        print("✘ 真契约探针没有输出")
        print((r.stderr or "")[:1500])
        FAILED.append("真契约探针没有输出")

print("")
if FAILED:
    print("✘ %d 项失败" % len(FAILED))
    for f in FAILED:
        print("   · " + f)
    sys.exit(1)
print("全部通过 ✔（%d 项）" % PASSED)

# ══════════════════ 9. 负控：证明上面那些判据真的会红 ══════════════════
# 一个只会全绿的测试没有价值 —— 必须证明它在该红的时候红。
# 这一段**默认不跑**（它要反复重跑整个文件，慢），用 `--negctl` 显式开启：
#
#     python library_ui_test.py --negctl
#
# 每条负控只注入一个缺陷、跑一遍完整测试、核对失败项里有没有预期的那条，
# 然后**无条件还原**。注入后没红 = 那条判据是假绿，直接算失败。
if "--negctl" in sys.argv:

    def _run_child() -> tuple[int, str]:
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                            "--plain"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def _fails(o: str) -> list[str]:
        return [ln[2:].split("   ←")[0].strip()
                for ln in o.splitlines() if ln.startswith("✘ ")]

    print("\n── 负控 ──")
    _JS = JS.read_text(encoding="utf-8")
    _CSS = CSS.read_text(encoding="utf-8")
    CASES = [
        # 用精确字面量而不是正则：第一版用正则替换 esc，`(?:\s*\.replace\([^;]*?\))*`
        # 在 `'&amp;'` 里那个分号上就停了，于是"改了"其实没改 —— 负控自己变成假绿。
        ("esc 恒等（XSS 判据该红）", JS, _JS,
         "const esc = s => String(s == null ? '' : s)\n"
         "    .replace(/&/g, '&amp;').replace(/</g, '&lt;')\n"
         "    .replace(/>/g, '&gt;').replace(/\"/g, '&quot;');",
         "const esc = s => String(s == null ? '' : s);", ["XSS"]),
        # 只把页码钉成 1，保持表达式合法 —— 直接砍掉 `+ state.page` 会造成语法错，
        # 那样红的是 node --check，测不到分页本身。
        ("翻页恒为第 1 页（分页判据该红）", JS, _JS,
         "?page=' + state.page", "?page=1' + ''", ["page"]),
        ("去掉列表序列号守卫（竞态判据该红）", JS, _JS,
         "if (seq !== state.seq) return;", "", ["迟到"]),
        ("把「读不出来」说成「库里还没有文献」", JS, _JS,
         "return failBox('馆藏读不出来（这不是库空了）', state.err)",
         "return '<div class=\"blank\">库里还没有文献</div>'", ["读不出来"]),
        ("恢复 loadKb 顺手发 limit=500", JS, _JS,
         "    primeSegOut();\n    if (typeof loadKb === 'function') loadKb();",
         "    if (typeof loadKb === 'function') loadKb();", ["limit=500"]),
        ("CSS 写死色值", CSS, _CSS,
         ".kbwork{--kwgap:12px}", ".kbwork{--kwgap:12px;color:#333}", ["色值"]),
        ("CSS 引用未定义变量", CSS, _CSS,
         ".kbwork{--kwgap:12px}",
         ".kbwork{--kwgap:12px;color:var(--nope-token)}", ["变量"]),
        ("去掉窄屏媒体查询", CSS, _CSS,
         "@media (max-width:620px){", "@media (max-width:1px){", ["窄屏"]),
    ]

    _bad = 0
    for label, path, orig, old, new, expect in CASES:
        mutated = orig.replace(old, new, 1)
        if mutated == orig:
            print("✘ 负控「%s」的变异没落地（替换串没匹配上）" % label)
            _bad += 1
            continue
        path.write_text(mutated, encoding="utf-8")
        try:
            code, out = _run_child()
        finally:
            path.write_text(orig, encoding="utf-8")   # 无条件还原
        hits = [f for f in _fails(out) if any(e in f for e in expect)]
        if code == 0:
            print("✘ 负控「%s」没被抓住 —— 那条判据是假绿" % label)
            _bad += 1
        elif not hits:
            print("✘ 负控「%s」红了，但红在别处（红的是：%s）"
                  % (label, "、".join(_fails(out)[:3])))
            _bad += 1
        else:
            print("✔ 负控「%s」被抓：%s" % (label, hits[0]))

    code, _ = _run_child()
    if code != 0:
        print("✘ 还原后基线没恢复")
        _bad += 1
    else:
        print("✔ 还原后基线恢复全绿")
    print("")
    if _bad:
        print("✘ %d 条负控没生效" % _bad)
        sys.exit(1)
    print("负控全部通过 ✔（%d 条都真的会红）" % len(CASES))
