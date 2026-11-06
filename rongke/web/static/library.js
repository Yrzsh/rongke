/* 文献库主工作区（2026-10-29）
   ════════════════════════════════════════════════════════════════════
   这一层只做**装配与呈现**，不新造任何取数通路：

     · 馆藏：走 `GET /api/kb/list`（服务端分页 + 元数据筛选），
             一页 20/40 条，翻页是**真的在服务端切片**，不在前端切数组。
     · 详情：走 `GET /api/kb/read`（按页读正文，单次跨度 20 页 —— 与
             `library_api.KB_READ_MAX_SPAN` 同口径）。有 PDF 的条目复用
             `openOrig({kb,title,n})` 看原页，不另造一个预览器。
     · 段落检索：**不重写**。既有的 `#kbQ / #kbGo / #kbAll / #kbOut /
             #kbSearchHint` 连同胞 workspace.js 里挂好的 `kbSearch()` /
             `kbListAll()` 一起，整体搬进「段落检索」tab —— 元素挪走、
             事件处理器跟着元素走，所以这里**一条都不许重新绑定**。
     · 库况：复用 `loadKb()`（它还会顺手把段落列表填上，与旧行为一致）。
     · AI 候选 / 本地 PDF：两张既有卡片整体搬进「新增文献」tab，
             `kbFind / kbPick / kbUpload` 的绑定仍在 workspace.js 里，不动。

   为什么可以搬节点：index.html 里那些控件是 workspace.js 在加载时就取好
   引用并挂上处理器的；DOM 里换个父节点不影响既有引用与处理器。反之，
   如果这里再绑一次 `#kbGo.onclick`，workspace.js 那次就会被覆盖或重复，
   而 `kbSearch()` 内部的 `KB_SEARCH_BUSY` 也会变成两套状态。

   本文件是传统脚本（非 ES module），只导出 `window.RongkeLibrary`，
   其余全部关在这个 IIFE 里 —— 不往 window 上丢状态，也不与 workspace.js
   的同名函数（esc / $）争顶层绑定。
   ════════════════════════════════════════════════════════════════════ */
(() => {
  'use strict';

  /* ── 契约常量（照着 library_api.py 写，别在这里另发明一套）── */
  const SPAN = 20;                     // KB_READ_MAX_SPAN：一次最多读 20 页
  const SOURCES = [['all', '全部'], ['pdf', '有原文'], ['text', '只有文本'],
                   ['meta', '仅元数据']];
  const SORTS = [['year', '年份'], ['title', '题名']];
  const ORDERS = [['desc', '降序'], ['asc', '升序']];
  const SIZES = [['20', '每页 20'], ['40', '每页 40']];
  /* 三档「能读到什么程度」：与 /api/kb/stat 的 n_pdf / n_text_only / n_meta
     同一个口径（盘上事实），也与旧列表里那枚胶囊同义。 */
  const KIND = {
    pdf: { label: '有原文', why: '盘上还有 PDF 原件：能看原页，也能按段落查' },
    text: { label: '只有文本', why: 'PDF 不在了，只能读抽出来的文字，看不到原页' },
    meta: { label: '仅元数据', why: '只有题录（题名 / 年份），正文读不了' },
  };

  const state = {
    built: false, tab: 'col',
    seq: 0, readSeq: 0,
    page: 1, size: 20, q: '', source: 'all', sort: 'year', order: 'desc',
    busy: false, data: null, err: null,
    openItem: null, read: null, readFrom: 1,
    el: {},
  };

  /* ── 小工具：全部本地，不新增全局 ── */
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const txt = v => String(v == null ? '' : v);
  const num = (v, d) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : d;
  };
  /* 题名清洗：workspace.js 有个 `kbClean`（解实体、去残留标签，针对实测的
     85/251 条脏标题）。能复用就复用，复用不到原样返回 —— 这条只是显示层
     的归一，不该成为本文件能否加载的前提。 */
  const clean = t => (typeof kbClean === 'function' ? kbClean(txt(t)) : txt(t));
  const byId = id => (typeof document !== 'undefined' && document.getElementById
    ? document.getElementById(id) : null);
  /* 取包着某控件的 .card。用 parentNode 上溯而不是 closest：
     环境里那些 DOM 桩（含测试用的）不一定实现 closest。 */
  function cardOf(node) {
    let p = node;
    while (p) {
      const cn = txt(p.className);
      if (cn && cn.split(/\s+/).indexOf('card') >= 0) return p;
      p = p.parentNode;
    }
    return null;
  }
  function kid(node, tag) {
    const cs = (node && node.children) || [];
    for (let i = 0; i < cs.length; i++) {
      if (txt(cs[i].tagName).toUpperCase() === tag) return cs[i];
    }
    return null;
  }
  const errMsgOf = e => (typeof errMsg === 'function' ? errMsg(e)
    : txt(e && e.message ? e.message : e));

  /* ══════════════════ 骨架 ══════════════════ */
  function build() {
    if (state.built) return true;
    const stat = byId('kbStat');
    if (!stat) return false;                 // 这页没有文献库区块：什么都不做
    const statCard = cardOf(stat);
    const host = (statCard && statCard.parentNode) || null;
    if (!host) return false;
    if (byId('kbWork')) { state.built = true; return true; }

    const shell = document.createElement('div');
    shell.className = 'card kbwork';
    shell.id = 'kbWork';
    shell.setAttribute('data-kbwork', '1');

    /* 头部：tab 条 + 一行事实（条数/页码由 paintCol 写） */
    const head = document.createElement('div');
    head.className = 'kwhead';
    const tabs = document.createElement('div');
    tabs.className = 'kwtabs';
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', '文献库工作区');
    state.el.tab = {};
    [['col', '馆藏'], ['seg', '段落检索'], ['add', '新增文献']].forEach(pair => {
      const b = document.createElement('button');
      b.className = 'kwtab';
      b.setAttribute('type', 'button');
      b.setAttribute('role', 'tab');
      b.setAttribute('data-kbact', 'tab');
      b.setAttribute('data-kbtab', pair[0]);
      b.textContent = pair[1];
      tabs.appendChild(b);
      state.el.tab[pair[0]] = b;
    });
    const hint = document.createElement('span');
    hint.className = 'kwhint';
    state.el.hint = hint;
    head.appendChild(tabs);
    head.appendChild(hint);

    const body = document.createElement('div');
    body.className = 'kwbody';
    state.el.panel = {};
    [['col', '馆藏'], ['seg', '段落检索'], ['add', '新增文献']].forEach(pair => {
      const p = document.createElement('div');
      p.className = 'kwpanel';
      p.setAttribute('data-kbpanel', pair[0]);
      state.el.panel[pair[0]] = p;
      body.appendChild(p);
    });

    buildColPanel(state.el.panel.col);
    buildSegPanel(state.el.panel.seg);
    buildAddPanel(state.el.panel.add);

    shell.appendChild(head);
    shell.appendChild(body);
    if (statCard.nextSibling) host.insertBefore(shell, statCard.nextSibling);
    else host.appendChild(shell);

    /* 一个监听器管全部按钮/行：行和按钮都是每次重画出来的，
       逐个挂处理器等于每画一次挂一串，且旧的一定没解绑。 */
    shell.addEventListener('click', onShellClick);
    shell.addEventListener('keydown', onShellKey);

    state.built = true;
    paintTabs();
    return true;
  }

  /* ── 馆藏面板：筛选条 + 详情区 + 列表区 ── */
  function buildColPanel(panel) {
    const bar = document.createElement('div');
    bar.className = 'kwfilt';

    const q = document.createElement('input');
    q.className = 'kwin';
    q.id = 'kbColQ';
    q.setAttribute('type', 'search');
    q.setAttribute('aria-label', '按题名、年份或库内编号筛选');
    q.setAttribute('placeholder', '按题名 / 年份 / 编号筛（只筛元数据，不查正文）');
    state.el.q = q;

    const apply = document.createElement('button');
    apply.className = 'mini';
    apply.setAttribute('type', 'button');
    apply.setAttribute('data-kbact', 'apply');
    apply.textContent = '筛选';

    const sel = (act, opts, val, label) => {
      const s = document.createElement('select');
      s.className = 'kwsel';
      s.setAttribute('aria-label', label);
      s.setAttribute('data-kbact', act);
      s.innerHTML = opts.map(o => '<option value="' + esc(o[0]) + '">'
        + esc(o[1]) + '</option>').join('');
      s.value = val;
      return s;
    };
    state.el.src = sel('source', SOURCES, state.source, '按能读到什么程度筛');
    state.el.sort = sel('sort', SORTS, state.sort, '排序字段');
    state.el.order = sel('order', ORDERS, state.order, '排序方向');
    state.el.size = sel('size', SIZES, String(state.size), '每页条数');

    bar.appendChild(q);
    bar.appendChild(apply);
    bar.appendChild(state.el.src);
    bar.appendChild(state.el.sort);
    bar.appendChild(state.el.order);
    bar.appendChild(state.el.size);

    const detail = document.createElement('div');
    detail.className = 'kwdet';
    detail.hidden = true;
    state.el.detail = detail;

    const list = document.createElement('div');
    list.className = 'kwlist';
    state.el.list = list;

    panel.appendChild(bar);
    panel.appendChild(detail);
    panel.appendChild(list);

    q.addEventListener('keydown', ev => {
      if (ev && ev.key === 'Enter') { readFilt(); state.page = 1; loadList(); }
    });
    [state.el.src, state.el.sort, state.el.order, state.el.size].forEach(s => {
      s.addEventListener('change', () => {
        /* 换排序字段时把方向拨回该字段的默认（年份新的在前 / 题名 A→Z），
           与 library_api.parse_list_params 的默认一致 —— 否则「按题名 +
           降序」会变成一个没人选过的组合。 */
        if (s === state.el.sort) {
          state.el.order.value = s.value === 'year' ? 'desc' : 'asc';
        }
        readFilt();
        state.page = 1;
        loadList();
      });
    });
  }

  /* ── 段落面板：搬既有卡片（一条都不重新绑定）── */
  function buildSegPanel(panel) {
    const card = cardOf(byId('kbQ')) || cardOf(byId('kbOut'));
    if (!card) {
      panel.innerHTML = '<div class="blank">这一页上找不到段落检索控件'
        + '（#kbQ / #kbGo / #kbOut）—— 那不是库空了，是页面结构与预期不符。</div>';
      return;
    }
    /* 卡片自带的 h3 是「馆藏与检索」：馆藏已经搬到隔壁 tab 了，
       留着这行标题会指错方向。tab 条已经写明「段落检索」，这里隐掉它。 */
    const h = kid(card, 'H3');
    if (h) h.hidden = true;
    panel.appendChild(card);
  }

  /* ── 挡掉 loadKb() 顺手发起的那次「整库一次拉回」──
     既有 loadKb() 第一行是：`if (out && out.dataset.filled !== "1") kbListAll();`
     而 kbListAll() 请求的是 `/api/kb/list?limit=500` —— 把整个库（251 篇、
     真要长大就是几千条）一次拉回前端，只为了铺前 20 行。以前它是"列出全部"
     的唯一入口；现在馆藏 tab 是**服务端分页**，这条 500 条的老路径就只剩副作用:
     每次切到文献库都白发一个大请求、白渲染一张前端切片的表。

     所以：进来先把 #kbOut 标成已填（内容换成一句指路），loadKb() 那一行就不再
     触发。「列出全部」按钮本身不受影响 —— 它直接绑到 kbListAll()，用户点了
     才发生那一次拉取，且那时他确实要这张表。 */
  function primeSegOut() {
    const out = byId('kbOut');
    if (!out || !out.dataset) return;
    if (out.dataset.filled === '1') return;      // 已经有过内容：不覆盖用户在看的东西
    out.dataset.filled = '1';
    out.innerHTML = '<div class="blank">要按段落查，用上面的检索框'
      + '（它查的是 PDF 里的正文，不是题名）；'
      + '想把整个库铺成一张表，按「列出全部」。</div>';
  }

  /* ── 新增面板：AI 候选 + 本地 PDF 两张既有卡片 ── */
  function buildAddPanel(panel) {
    const find = cardOf(byId('kbFindQ')) || cardOf(byId('kbFindOut'));
    const up = cardOf(byId('kbFile')) || cardOf(byId('kbUpOut'));
    const moved = [];
    [find, up].forEach(c => {
      if (c && moved.indexOf(c) < 0) { moved.push(c); panel.appendChild(c); }
    });
    if (!moved.length) {
      panel.innerHTML = '<div class="blank">这一页上找不到新增文献的控件'
        + '（#kbFindQ / #kbFile）—— 不是库满了，是页面结构与预期不符。</div>';
    }
  }

  /* ══════════════════ tab ══════════════════ */
  function switchTab(k) {
    if (!state.el.tab || !state.el.tab[k]) return;
    state.tab = k;
    paintTabs();
  }
  function paintTabs() {
    const tabs = state.el.tab || {};
    Object.keys(tabs).forEach(k => {
      const b = tabs[k];
      b.setAttribute('aria-selected', k === state.tab ? 'true' : 'false');
      b.className = 'kwtab' + (k === state.tab ? ' on' : '');
    });
    const panels = state.el.panel || {};
    Object.keys(panels).forEach(k => { panels[k].hidden = (k !== state.tab); });
  }

  /* ══════════════════ 馆藏：取数 ══════════════════ */
  function readFilt() {
    state.q = txt(state.el.q && state.el.q.value).trim();
    state.source = txt(state.el.src && state.el.src.value) || 'all';
    state.sort = txt(state.el.sort && state.el.sort.value) || 'year';
    state.order = txt(state.el.order && state.el.order.value)
      || (state.sort === 'year' ? 'desc' : 'asc');
    state.size = num(state.el.size && state.el.size.value, 20) || 20;
  }

  function listUrl() {
    return '/api/kb/list?page=' + state.page
      + '&page_size=' + state.size
      + '&q=' + encodeURIComponent(state.q)
      + '&source=' + encodeURIComponent(state.source)
      + '&sort=' + encodeURIComponent(state.sort)
      + '&order=' + encodeURIComponent(state.order);
  }

  async function loadList() {
    if (!build()) return;
    const seq = ++state.seq;
    state.busy = true;
    state.err = null;
    paintCol();
    let d = null;
    let caught = null;
    try { d = await api(listUrl()); }
    catch (e) { caught = e; }
    if (seq !== state.seq) return;          // 后来者已经重画过：这一份作废
    state.busy = false;
    if (caught) {
      state.data = null;
      state.err = { error: errMsgOf(caught), hint: txt(caught && caught.hint),
                    status: caught && caught.status };
      paintCol();
      return;
    }
    if (!d || !d.ok) {
      state.data = null;
      state.err = d || { error: '没有回包' };
      paintCol();
      return;
    }
    state.data = d;
    /* 服务端把 page 归一过（越界时可能被 clamp），以回包为准 —— 否则
       「下一页」会按本地那个已经不成立的页码继续往后点。 */
    state.page = Math.max(1, num(d.page, state.page));
    paintCol();
  }

  /* ══════════════════ 馆藏：画 ══════════════════ */
  function paintCol() {
    const list = state.el.list;
    if (!list) return;
    list.innerHTML = colBody();
    if (state.el.hint) {
      const d = state.data;
      if (state.busy) state.el.hint.textContent = '读取中…';
      else if (state.err) state.el.hint.textContent = '读不出来';
      else if (d) state.el.hint.textContent = '共 ' + num(d.total, 0) + ' 篇';
      else state.el.hint.textContent = '';
    }
    paintDetail();
  }

  function kindOf(it) {
    if (it && it.has_pdf) return 'pdf';
    if (it && it.fulltext) return 'text';
    return 'meta';
  }

  function failBox(where, d) {
    const e = txt(d && (d.error || d.raw)) || '未知错误';
    const h = d && d.hint
      ? '<div class="kwfix"><b>怎么修：</b>' + esc(txt(d.hint)) + '</div>' : '';
    return '<div class="kbwarn kwfail"><b>' + esc(where) + '</b>'
      + '<div class="kwerr">' + esc(e.slice(0, 400)) + '</div>' + h + '</div>';
  }

  function colBody() {
    if (state.busy && !state.data) return '<div class="blank">读取中…</div>';
    if (state.err) {
      /* 读不出来 ≠ 库空了。旧列表在工具接线断掉时也会 blank，
         于是「库没挂上」和「库里一篇都没有」在界面上长得一模一样。 */
      return failBox('馆藏读不出来（这不是库空了）', state.err)
        + '<div class="kwretry"><button class="mini" data-kbact="apply">'
        + '再试一次</button></div>';
    }
    const d = state.data;
    if (!d) return '<div class="blank">切到这一页就会列出馆藏。</div>';
    const items = Array.isArray(d.items) ? d.items : [];
    const total = num(d.total, 0);
    const filtered = !!state.q || state.source !== 'all';
    if (!total) {
      return filtered
        ? '<div class="blank">这个筛选下没有条目 —— '
          + '<b>是筛选没命中，不是库空了</b>。'
          + '<button class="mini" data-kbact="clear">清掉筛选</button></div>'
        : '<div class="blank">库里还没有文献 —— 切到「新增文献」，'
          + '用 AI 检索候选或本地 PDF 入库。</div>';
    }
    if (!items.length) {
      /* total>0 但这一页空：只剩「页码越界」这一种解释（服务端一般会 clamp）。 */
      return '<div class="blank">第 ' + esc(String(state.page)) + ' 页没有条目'
        + '（共 ' + esc(String(total)) + ' 篇）—— 回到第一页再翻。'
        + '<button class="mini" data-kbact="prev">回第一页</button></div>';
    }
    return tableHtml(items) + pagerHtml(d, total);
  }

  function tableHtml(items) {
    const rows = items.map(it => {
      const k = kindOf(it);
      const did = txt(it && it.id);
      const year = txt(it && it.year) || '—';
      const journal = txt(it && it.journal) || '—';
      return '<tr class="kwrow" data-kbact="open" data-kbid="' + esc(did) + '">'
        + '<td class="kwc-year">' + esc(year) + '</td>'
        + '<td class="kwc-title"><button class="kwlink" type="button"'
        + ' data-kbact="open" data-kbid="' + esc(did) + '">'
        + esc(clean(it && it.title) || '(无题名)') + '</button></td>'
        + '<td class="kwc-journal">' + esc(journal) + '</td>'
        + '<td class="kwc-kind"><span class="kwsrc" data-s="' + k + '">'
        + esc(KIND[k].label) + '</span></td>'
        + '</tr>';
    }).join('');
    return '<table class="kwtable"><colgroup>'
      + '<col style="width:11%"><col style="width:auto">'
      + '<col style="width:30%"><col style="width:20%">'
      + '</colgroup><thead><tr><th>年份</th><th>题名</th><th>期刊</th>'
      + '<th>能读到</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function pagerHtml(d, total) {
    const size = num(d.page_size, state.size) || state.size;
    const pages = Math.max(1, Math.ceil(total / size));
    const page = Math.max(1, num(d.page, state.page));
    const showing = num(d.showing, (d.items || []).length);
    return '<div class="kwpg">'
      + '<span class="kwpg-n">共 ' + esc(String(total)) + ' 篇 · 第 ' + page
      + '/' + pages + ' 页 · 本页 ' + esc(String(showing)) + ' 条</span>'
      + '<span class="kwpg-b">'
      + '<button class="mini" type="button" data-kbact="prev"'
      + (page <= 1 ? ' disabled' : '') + '>上一页</button>'
      + '<button class="mini" type="button" data-kbact="next"'
      + (d.has_more ? '' : ' disabled') + '>下一页</button></span>'
      + (txt(d.note) ? '<div class="kwnote">' + esc(txt(d.note)) + '</div>' : '')
      + (txt(d.meta_note) ? '<div class="kwnote">' + esc(txt(d.meta_note))
        + '</div>' : '')
      + '</div>';
  }

  /* ══════════════════ 详情 ══════════════════ */
  function findItem(id) {
    const items = (state.data && state.data.items) || [];
    for (let i = 0; i < items.length; i++) {
      if (txt(items[i] && items[i].id) === txt(id)) return items[i];
    }
    return null;
  }

  function metaBlock(it) {
    const rows = [];
    const put = (label, value, mono) => {
      const v = txt(value).trim();
      if (!v) return;
      rows.push('<div class="kwmeta-row"><dt>' + esc(label) + '</dt><dd'
        + (mono ? ' class="kwmono"' : '') + '>' + esc(v) + '</dd></div>');
    };
    put('年份', it.year);
    put('期刊', it.journal);
    put('DOI', it.doi, true);
    put('页码', it.pages);
    if (Array.isArray(it.tags) && it.tags.length) put('标签', it.tags.join('、'));
    if (Array.isArray(it.authors) && it.authors.length) {
      put('作者', it.authors.join('；'));
    } else if (txt(it.authors)) put('作者', it.authors);
    put('库内编号', it.id, true);
    const abs = txt(it.abstract).trim();
    let h = rows.length ? '<dl class="kwmeta">' + rows.join('') + '</dl>' : '';
    if (abs) {
      h += '<details class="kwabs"><summary>摘要</summary><p>'
        + esc(abs) + '</p></details>';
    }
    return h;
  }

  function kindBlock(it) {
    const k = kindOf(it);
    return '<div class="kwdkind" data-k="' + k + '">'
      + '<b>' + esc(KIND[k].label) + '</b>'
      + '<span>' + esc(KIND[k].why) + '</span>'
      + (txt(it.status)
        ? '<span class="kwraw">库里记的状态：' + esc(txt(it.status)) + '</span>'
        : '')
      + '</div>';
  }

  function readBlock(it) {
    const r = state.read;
    if (!r) return '<div class="blank">正在准备正文…</div>';
    if (r.state === 'loading') return '<div class="blank">读正文…</div>';
    if (r.state === 'err') {
      /* 四种"读不出来"必须分开说：库坏了 / 这篇没正文 / 编号不存在 /
         页码越界。混成一句"加载失败"会让人以为这一篇是空的。 */
      const st = num(r.status, 0);
      if (st === 409) {
        return '<div class="kbwarn kwfail"><b>这篇在库里，但没有抽出来的正文</b>'
          + '<div class="kwerr">' + esc(r.error) + '</div>'
          + '<div class="kwfix">这不是空文档 —— 库里只有它的题录。'
          + (it && it.has_pdf
            ? '盘上还有 PDF 原件，点上面的「看原页」能读原页。'
            : '盘上也没有 PDF 原件，所以这一篇暂时只能看题录。')
          + '要补正文：找到这篇的 PDF，走「新增文献 · 本地 PDF」入库。'
          + '</div></div>';
      }
      if (st === 404) {
        return '<div class="kbwarn kwfail"><b>库里找不到这个编号</b>'
          + '<div class="kwerr">' + esc(r.error) + '</div></div>';
      }
      if (st === 400) {
        return '<div class="kbwarn kwfail"><b>页码不对</b>'
          + '<div class="kwerr">' + esc(r.error) + '</div>'
          + (r.hint ? '<div class="kwfix">' + esc(r.hint) + '</div>' : '')
          + '<div class="kwretry"><button class="mini" data-kbact="rfirst">'
          + '从第 1 页读</button></div></div>';
      }
      if (st === 503) {
        return '<div class="kbwarn kwfail"><b>读正文这条通路没接上</b>'
          + '<div class="kwerr">' + esc(r.error) + '</div>'
          + '<div class="kwfix">这是接线断了，<b>不是这篇没有内容</b>。</div></div>';
      }
      return '<div class="kbwarn kwfail"><b>正文这一次没读出来</b>'
        + '<div class="kwerr">' + esc(r.error) + '</div>'
        + (r.hint ? '<div class="kwfix">' + esc(r.hint) + '</div>' : '')
        + '<div class="kwretry"><button class="mini" data-kbact="rfirst">'
        + '重试</button></div></div>';
    }
    const d = r.data || {};
    const from = num(d.page_from, 1) || 1;
    const to = num(d.page_to, from);
    const total = num(d.pages_total, 0);
    const range = d.page_range && d.page_range.length
      ? num(d.page_range[0], from) + '–' + num(d.page_range[d.page_range.length - 1], to)
      : from + '–' + to;
    let h = '<div class="kwtext">'
      + '<div class="kwtext-bar"><span>第 ' + esc(range) + ' 页'
      + (total ? ' / 共 ' + esc(String(total)) + ' 页' : '') + '</span>'
      + '<span class="kwpg-b">'
      + '<button class="mini" type="button" data-kbact="rprev"'
      + (from <= 1 ? ' disabled' : '') + '>上一段</button>'
      + '<button class="mini" type="button" data-kbact="rnext"'
      + ((d.has_more_pages || from > 1) ? '' : ' disabled') + '>下一段</button></span></div>';
    if (d.empty_window) {
      return h + '<div class="kwnote">这一页没有抽出来的正文，不是整篇回退。'
        + '可继续往后翻，或重新抽取 PDF。</div></div>';
    }
    if (d.truncated) {
      h += '<div class="kwnote">这一段的文字被字数上限截断'
        + (num(d.char_total, 0)
          ? '（本段 ' + esc(String(num(d.chars, 0))) + ' 字 / 全篇 '
            + esc(String(num(d.char_total, 0))) + ' 字）' : '')
        + ' —— 想看全，用「下一段」往后读。</div>';
    }
    h += '<pre class="kwpre">' + esc(txt(d.text)) + '</pre>';
    if (d.has_more_pages) {
      h += '<div class="kwmore"><button class="mini" type="button"'
        + ' data-kbact="rnext">继续读后面的页</button></div>';
    }
    return h + '</div>';
  }

  function paintDetail() {
    const box = state.el.detail;
    if (!box) return;
    const it = state.openItem;
    if (!it) { box.innerHTML = ''; box.hidden = true; return; }
    box.hidden = false;
    const did = txt(it.id);
    let h = '<div class="kwdhead">'
      + '<b>' + esc(clean(it.title) || '(无题名)') + '</b>'
      + '<span class="kwdid">' + esc(did) + '</span>'
      + '<span class="kwdbtn">'
      + (it.has_pdf
        ? '<button class="mini" type="button" data-kbact="orig" data-kbid="'
          + esc(did) + '">看原页</button>' : '')
      + '<button class="mini" type="button" data-kbact="closedet">收起</button>'
      + '</span></div>';
    h += kindBlock(it);
    h += metaBlock(it);
    h += readBlock(it);
    box.innerHTML = h;
  }

  /* ══════════════════ 详情：取数 ══════════════════ */
  async function loadRead(it, from) {
    if (!it) return;
    const seq = ++state.readSeq;
    state.readFrom = Math.max(1, num(from, 1) || 1);
    state.read = { state: 'loading' };
    paintDetail();
    const url = '/api/kb/read?doc_id=' + encodeURIComponent(txt(it.id))
      /* 只给 page_from：`page_to` 一头由服务端按 20 页跨度补上，
         并且它只对**显式给**的那头做越界判定 —— 自己补 20 页会在这篇
         不足 20 页时被 400 拦下（library_api.read_payload 的既有口径）。 */
      + '&page_from=' + state.readFrom;
    let d = null;
    let caught = null;
    try { d = await api(url); }
    catch (e) { caught = e; }
    if (seq !== state.readSeq) return;
    if (caught) {
      const body = (caught && caught.body) || null;
      state.read = {
        state: 'err', status: caught && caught.status,
        error: txt(body && body.error) || errMsgOf(caught),
        hint: txt((body && body.hint) || (caught && caught.hint)),
      };
      paintDetail();
      return;
    }
    if (!d || !d.ok) {
      state.read = {
        state: 'err', status: 0,
        error: txt(d && (d.error || d.raw)) || '没有回包',
        hint: txt(d && d.hint),
      };
      paintDetail();
      return;
    }
    state.read = { state: 'ok', data: d };
    paintDetail();
  }

  /* ══════════════════ 对外动作 ══════════════════ */
  /* `page` 可选：段落检索命中某一页时，直接落到那一页上（`open(id, 37)`）——
     否则"命中在第 37 页"之后还得自己一段一段往后翻，等于把命中信息扔了。
     传进来的页按 20 页跨度往下取整对齐，让 `page_range` 覆盖到它。 */
  async function openItemById(id, page) {
    const did = txt(id).trim();
    if (!did) return;
    if (!build()) return;
    switchTab('col');
    primeSegOut();                       // 切过来也不该触发整库拉取
    const want = Math.max(1, num(page, 1) || 1);
    const from = Math.floor((want - 1) / SPAN) * SPAN + 1;
    let it = findItem(did);
    if (!it) {
      /* 不在当前这一页：用元数据筛选把它捞出来（`q` 匹配 id，见
         library_api._match_q），而不是拿一条没有元数据的空壳去画。 */
      state.q = did;
      state.page = 1;
      if (state.el.q) state.el.q.value = did;
      await loadList();
      it = findItem(did);
    }
    state.openItem = it || { id: did, title: '', status: '', fulltext: false,
                             has_pdf: false };
    state.read = null;
    state.readSeq++;                      // 上一份正文的响应一律作废
    paintCol();                           // 从段落命中进入时把详情也画到馆藏面板
    if (it) await loadRead(it, from);
    else {
      state.read = { state: 'err', status: 404,
                     error: '库里没有 ' + did + ' 这一条（也可能是筛选没捞到）' };
      paintCol();
    }
  }

  function search(q) {
    if (!build()) return;
    switchTab('col');
    state.q = txt(q).trim();
    state.page = 1;
    if (state.el.q) state.el.q.value = state.q;
    loadList();
  }

  function refresh() {
    if (!build()) return;
    paintTabs();
    /* ★ 先挡掉老路径，再调 loadKb()：见 primeSegOut 的注释 ——
       loadKb() 会顺手 kbListAll()，那是 `/api/kb/list?limit=500` 的整库拉取。 */
    primeSegOut();
    if (typeof loadKb === 'function') loadKb();
    loadList();
  }

  function init() {
    if (!build()) return;
    refresh();
  }

  /* ══════════════════ 事件 ══════════════════ */
  function hit(ev) {
    const t = ev && ev.target;
    if (!t || typeof t.closest !== 'function') return null;
    return t.closest('[data-kbact]');
  }

  function onShellClick(ev) {
    const t = hit(ev);
    if (!t) return;
    const act = txt(t.getAttribute('data-kbact'));
    const did = txt(t.getAttribute('data-kbid'));
    if (act === 'tab') { switchTab(txt(t.getAttribute('data-kbtab'))); return; }
    if (act === 'apply') { readFilt(); state.page = 1; loadList(); return; }
    if (act === 'clear') {
      state.q = ''; state.source = 'all'; state.page = 1;
      if (state.el.q) state.el.q.value = '';
      if (state.el.src) state.el.src.value = 'all';
      loadList();
      return;
    }
    if (act === 'prev') {
      if (state.page > 1) { state.page -= 1; loadList(); }
      return;
    }
    if (act === 'next') {
      state.page += 1; loadList();
      return;
    }
    if (act === 'open') { openItemById(did || (state.openItem && state.openItem.id)); return; }
    if (act === 'closedet') {
      state.openItem = null; state.read = null; state.readSeq++;
      paintDetail();
      return;
    }
    if (act === 'orig') {
      const it = state.openItem;
      if (!it || !it.has_pdf) return;
      if (typeof openOrig === 'function') {
        openOrig({ kb: txt(it.id), title: clean(it.title) || '文献',
                   n: Math.max(1, num(state.readFrom, 1) || 1) });
      }
      return;
    }
    if (act === 'rfirst') { loadRead(state.openItem, 1); return; }
    if (act === 'rprev') {
      const from = Math.max(1, num(state.readFrom, 1) - SPAN);
      loadRead(state.openItem, from);
      return;
    }
    if (act === 'rnext') {
      const d = (state.read && state.read.data) || {};
      const from = Math.max(1, num(d.page_to, state.readFrom) + 1);
      loadRead(state.openItem, from);
    }
  }

  function onShellKey(ev) {
    if (!ev || (ev.key !== 'Enter' && ev.key !== ' ')) return;
    const t = hit(ev);
    if (!t) return;
    const tag = txt(t.tagName).toUpperCase();
    /* 真正的 <button> 自己会发 click，不用再补一次（补了就是双击）。
       这里只补「表格行」这种非表单元素上的回车/空格。 */
    if (tag === 'BUTTON' || tag === 'INPUT' || tag === 'SELECT') return;
    if (ev.preventDefault) ev.preventDefault();
    onShellClick({ target: t });
  }

  window.RongkeLibrary = { init, refresh, open: openItemById, search };
})();
