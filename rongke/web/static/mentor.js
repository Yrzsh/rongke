/* 导师与课题管理：课题事实只在 topics/，文献事实只在共享库。 */
(() => {
  'use strict';
  const state = {tab:'overview', docs:{}, blockers:null, blockerStatus:'open',
                 slug:'', topics:[], library:null, candidates:null, seq:0, libSeq:0, docSeq:0, blockerSeq:0, busy:false, error:''};
  const q = s => document.querySelector(s);
  const text = v => String(v == null ? '' : v);
  const html = v => text(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const live = () => typeof LIVE !== 'undefined' && !!LIVE;
  const enabled = () => typeof MENTOR !== 'undefined' && !!MENTOR;
  const current = () => state.topics.find(t => t.slug === state.slug) || null;
  function error(e) { return '<div class="mw-error">'+html(e && e.message || e)+'</div>'; }
  async function post(body) { return api('/api/topic', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }
  async function load(slug, preserve) {
    const seq=++state.seq; state.error='';
    try {
      const d=await api('/api/topic'+(slug?'?slug='+encodeURIComponent(slug):''));
      if(seq!==state.seq)return;
      if(d.ok===false||d.error)throw new Error(d.error||'课题台账读取失败');
      state.topics=d.topic_details || [];
      if(!state.topics.length && d.current)state.topics=[{slug:d.current,title:d.title,question:d.question,library:d.library||[]}];
      state.slug=slug || d.current || state.slug || '';
      if(!preserve){state.library=null;state.candidates=null;state.docs={};state.blockers=null;}
      paint();
      if(state.slug)loadLibrary();
      if(['plan','reading','evidence'].includes(state.tab))loadDoc(state.tab,0);
      if(state.tab==='blockers')loadBlockers();
    }catch(e){if(seq===state.seq){state.error=e.message;paint();}}
  }
  async function loadLibrary() {
    const slug=state.slug,seq=++state.libSeq;state.library=null;paint();
    try{const d=await api('/api/topic/library?slug='+encodeURIComponent(slug));
      if(seq===state.libSeq&&state.slug===slug){state.library=d;paint();}
    }catch(e){if(seq===state.libSeq&&state.slug===slug){state.library={error:e.message};paint();}}
  }
  async function findDocs() {
    const input=q('#mwFind'),seq=++state.seq,slug=state.slug;
    state.candidates={loading:true};paint();
    try{const d=await api('/api/kb/list?page_size=50&q='+encodeURIComponent(input?input.value.trim():''));
      if(seq===state.seq&&state.slug===slug){state.candidates=d;paint();}
    }catch(e){if(seq===state.seq){state.candidates={error:e.message};paint();}}
  }
  async function action(body) {
    if(state.busy||live())return;
    state.busy=true;state.error='';
    try{const d=await post(body);if(d.error)throw new Error(d.error);state.slug=body.slug||state.slug;await load(state.slug,true);await loadLibrary();}
    catch(e){state.error=e.message;paint();}finally{state.busy=false;}
  }
  async function save() {
    if(live()){state.error='任务运行中，不能切换或修改课题。';paint();return;}
    const s=text(q('#mwSlug')&&q('#mwSlug').value).trim();
    const title=text(q('#mwTitle')&&q('#mwTitle').value).trim();
    const question=text(q('#mwQuestion')&&q('#mwQuestion').value).trim();
    if(!s){state.error='请填写课题标识。';paint();return;}
    const exists=state.topics.some(t=>t.slug===s);
    await action({slug:s,title:title,question:question,
      stage:q('#mwStage')?q('#mwStage').value:undefined,
      next_step:q('#mwNext')?q('#mwNext').value:undefined,
      action:exists?'update':'use'});
  }
  function overview() {
    const t=current();
    const workflow=t?'<label>当前工作阶段<select id="mwStage">'+[['ideate','构思评估'],['design','研究设计'],['evidence','文献与证据'],['execute','实验与写作'],['submission','投稿与回复']].map(x=>'<option value="'+x[0]+'">'+x[1]+'</option>').join('')+'</select></label>'+
      '<label>下一步（最多240字）<textarea id="mwNext" rows="2" maxlength="240">'+html(t.next_step)+'</textarea></label>':'';
    const actions='<div class="mw-form-actions"><button class="mini pri" id="mwSave" '+(live()?'disabled':'')+'>'+(t?'保存课题与工作阶段':'创建课题')+'</button>'+
      (t?'<button class="mini" id="mwPrepare">按此阶段准备对话</button>':'')+
      '<span class="mw-note">会话开始后课题锁定；运行中不能修改或切换。</span></div></div>';
    return '<div class="mw-topic-form"><label>课题标识<input id="mwSlug" maxlength="48" value="'+html(state.slug)+'" '+(t?'readonly':'')+'></label>'+
      '<label>课题名称<input id="mwTitle" maxlength="160" value="'+html(t&&t.title)+'"></label>'+
      '<label>研究问题<textarea id="mwQuestion" rows="4" maxlength="1200">'+html(t&&t.question)+'</textarea></label>'+
      workflow+actions+
      (t?'<p class="mw-question">'+html(t.question||'还没有写研究问题。')+'</p>':'');
  }
  function libraryBody() {
    if(!state.slug)return '<div class="mw-empty">先选择课题。</div>';
    const d=state.library;
    let h='<div class="mw-section-title"><h3>本课题文献</h3><span>共享库只存一份；这里管理关联。</span></div>';
    if(!d)h+='<div class="mw-empty">正在读取关联文献…</div>';
    else if(d.error)h+=error(d.error);
    else {
      h+=(d.items||[]).length?'<div class="mw-docs">'+d.items.map(i=>'<article class="mw-docrow"><button class="mw-docopen" data-mw-open="'+html(i.id)+'"><b>'+html(i.title||i.id)+'</b><small>'+html(i.year||'年份未知')+' · '+(i.has_pdf?'有PDF':i.fulltext?'有正文':'仅题录')+'</small></button><button class="mini" data-mw-unlink="'+html(i.id)+'">解除关联</button></article>').join('')+'</div>':'<div class="mw-empty">还没有关联文献。从下方共享库添加。</div>';
      if((d.missing_ids||[]).length)h+='<div class="mw-note">共享库中已找不到：'+html(d.missing_ids.join('、'))+'。关联保留，未自动删除。</div>';
    }
    h+='<div class="mw-picker"><label>从共享库添加<input id="mwFind" placeholder="按题名、年份或ID检索"></label><button class="mini" id="mwFindGo">检索</button></div>';
    const c=state.candidates;
    if(c&&c.loading)h+='<div class="mw-note">正在检索共享库…</div>';
    else if(c&&c.error)h+=error(c.error);
    else if(c)h+=(c.items||[]).length?'<div class="mw-candidates">'+c.items.map(i=>'<div><span>'+html(i.title||i.id)+'</span><button class="mini" data-mw-link="'+html(i.id)+'">添加到课题</button></div>').join('')+'</div>':'<div class="mw-empty">共享库没有匹配条目；这不等于世界上不存在相关文献。</div>';
    return h;
  }
  async function loadDoc(doc,offset) {
    if(!state.slug)return;const seq=++state.docSeq,slug=state.slug;state.docs[doc]=null;paint();
    try{const d=await api('/api/topic/doc?slug='+encodeURIComponent(slug)+'&doc='+doc+'&offset='+Math.max(0,offset||0)+'&limit=12000');
      if(seq===state.docSeq&&slug===state.slug){state.docs[doc]=d;paint();}}
    catch(e){if(seq===state.docSeq){state.docs[doc]={error:e.message};paint();}}
  }
  function docBody(doc) {
    const d=state.docs[doc];if(!d)return '<div class="mw-empty">正在读取…</div>';
    if(d.error)return error(d.error);
    if(d.empty||!text(d.text))return '<div class="mw-empty">这份台账还是空的，导师任务会逐步沉淀到这里。</div>';
    const total=Number(d.total_chars)||text(d.text).length,off=Number(d.offset)||0;
    return '<pre class="mw-doc">'+html(d.text)+'</pre>'+(total>text(d.text).length?'<div class="mw-page"><button class="mini" data-mw-earlier="'+doc+'" '+(off<=0?'disabled':'')+'>上一段</button><span>'+(off+1)+'–'+(off+text(d.text).length)+' / '+total+' 字</span><button class="mini" data-mw-more="'+doc+'" '+(!d.truncated?'disabled':'')+'>下一段</button></div>':'');
  }
  async function loadBlockers() {
    if(!state.slug)return;const seq=++state.blockerSeq,slug=state.slug;state.blockers=null;paint();
    try{const d=await api('/api/topic/blockers?slug='+encodeURIComponent(slug)+'&status='+state.blockerStatus+'&limit=100');
      if(seq===state.blockerSeq&&slug===state.slug){state.blockers=d;paint();}}
    catch(e){if(seq===state.blockerSeq){state.blockers={error:e.message};paint();}}
  }
  function blockerBody() {
    const d=state.blockers;if(!d)return '<div class="mw-empty">正在读取…</div>';if(d.error)return error(d.error);
    return '<div class="mw-doc-tools"><select id="mwBlockerStatus"><option value="open">未解决</option><option value="resolved">已解决</option><option value="all">全部</option></select><span class="mw-note">历史不会被覆盖。</span></div>'+
      ((d.items||[]).length?'<ul class="mw-blockers">'+d.items.map(b=>'<li class="mw-blocker"><b>'+html(b.what)+'</b><p>'+html(b.status==='open'?'未解决':'已解决')+' · '+html(b.opened_at||b.at)+'</p>'+(b.resolved_how?'<p>解决：'+html(b.resolved_how)+'</p>':'')+'</li>').join('')+'</ul>':'<div class="mw-empty">这一状态下没有卡点。</div>');
  }
  function paint() {
    const page=q('#mentorPage');if(!page)return;
    const tabs=[['overview','课题资料'],['library','本课题文献'],['plan','计划'],['reading','阅读台账'],['evidence','证据表'],['blockers','卡点']];
    let body=state.tab==='overview'?overview():state.tab==='library'?libraryBody():state.tab==='blockers'?blockerBody():docBody(state.tab);
    page.innerHTML='<div class="mw-manager"><div class="mw-manager-head"><div><h2>导师与课题</h2><p>让每次讨论围绕同一个研究问题、证据和未解决卡点继续。</p></div><button class="mini" id="mwMentor" aria-pressed="'+enabled()+'" '+(live()?'disabled':'')+'>导师模式：'+(enabled()?'已开启':'已关闭')+'</button></div>'+
      (state.error?error(state.error):'')+'<div class="mw-layout"><aside class="mw-sidebar"><button class="mini" id="mwNew">＋ 新建课题</button><div class="mw-topic-list">'+(state.topics.length?state.topics.map(t=>'<button data-mw-topic="'+html(t.slug)+'" class="'+(t.slug===state.slug?'on':'')+'"><b>'+html(t.title||t.slug)+'</b><small>'+(t.library||[]).length+' 篇关联文献</small></button>').join(''):'<div class="mw-empty">还没有课题</div>')+'</div></aside><main class="mw-main"><div class="mw-current"><b>'+html(current()&&current().title||state.slug||'未选择课题')+'</b><span>'+(live()?'本次会话课题已锁定':'选择用于新会话')+'</span></div><div class="mw-tabs">'+tabs.map(t=>'<button class="mw-tab" data-mw-tab="'+t[0]+'" aria-selected="'+(state.tab===t[0])+'">'+t[1]+'</button>').join('')+'</div><div class="mw-body">'+body+'</div></main></div></div>';
    wire(page);
    const bs=q('#mwBlockerStatus');if(bs)bs.value=state.blockerStatus;
    const ws=q('#mwStage');if(ws)ws.value=current().stage||'ideate';
  }
  function wire(page) {
    const mentor=q('#mwMentor');if(mentor)mentor.onclick=()=>{if(typeof toggleMentor==='function')toggleMentor();paint();};
    const nw=q('#mwNew');if(nw)nw.onclick=()=>{if(live())return;state.slug='';state.tab='overview';state.library=null;paint();};
    page.querySelectorAll('[data-mw-topic]').forEach(b=>b.onclick=()=>{if(live())return;state.tab='overview';action({slug:b.dataset.mwTopic,action:'use'});});
    page.querySelectorAll('[data-mw-tab]').forEach(b=>b.onclick=()=>{state.tab=b.dataset.mwTab;paint();if(['plan','reading','evidence'].includes(state.tab))loadDoc(state.tab,0);if(state.tab==='blockers')loadBlockers();});
    const saveBtn=q('#mwSave');if(saveBtn)saveBtn.onclick=save;
    const prep=q('#mwPrepare');if(prep)prep.onclick=()=>{
      const t=current(), prompts={ideate:'评估这个课题的科学问题、创新性与可行性，先指出证据缺口。',
        design:'为这个课题研究设计列出可检验假设、对照、实验单位与统计计划。',
        evidence:'核对本课题文献与证据，列出能支撑关键结论的页码锚点和未闭环缺口。',
        execute:'围绕当前实验与写作推进，先处理已登记卡点和可验证的下一步。',
        submission:'进行投稿前检查；若有审稿意见，逐条对应修改与证据回复。'};
      const goal=q('#goal');if(goal)goal.value=prompts[t.stage||'ideate']+(t.next_step?' 用户记录的下一步：'+t.next_step:'');
      if(typeof goView==='function')goView('chat');
      if(goal)goal.focus();
    };
    const find=q('#mwFindGo');if(find)find.onclick=findDocs;
    const inp=q('#mwFind');if(inp)inp.onkeydown=e=>{if(e.key==='Enter')findDocs();};
    page.querySelectorAll('[data-mw-link]').forEach(b=>b.onclick=()=>action({slug:state.slug,action:'link',doc_id:b.dataset.mwLink}));
    page.querySelectorAll('[data-mw-unlink]').forEach(b=>b.onclick=()=>action({slug:state.slug,action:'unlink',doc_id:b.dataset.mwUnlink}));
    page.querySelectorAll('[data-mw-open]').forEach(b=>b.onclick=()=>{if(window.RongkeLibrary)RongkeLibrary.open(b.dataset.mwOpen);});
    const bs=q('#mwBlockerStatus');if(bs)bs.onchange=()=>{state.blockerStatus=bs.value;loadBlockers();};
    page.querySelectorAll('[data-mw-more]').forEach(b=>b.onclick=()=>{const d=state.docs[b.dataset.mwMore]||{};loadDoc(b.dataset.mwMore,Number(d.offset||0)+text(d.text).length);});
    page.querySelectorAll('[data-mw-earlier]').forEach(b=>b.onclick=()=>{const d=state.docs[b.dataset.mwEarlier]||{};loadDoc(b.dataset.mwEarlier,Math.max(0,Number(d.offset||0)-12000));});
  }
  function paintBar() {
    const bar=q('#mentorBar');if(!bar)return;
    if(!enabled()){bar.innerHTML='';return;}
    const t=current();bar.innerHTML='<div class="mw-bar"><span>导师模式 · '+html(t&&t.title||state.slug||'尚未选择课题')+'</span><button class="mini" id="mwManage">管理导师与课题</button></div>';
    const b=q('#mwManage');if(b)b.onclick=()=>goView('mentor');
  }
  function openManager(){if(!state.topics.length&&!state.busy)load();else paint();}
  window.RongkeMentor={paint:paintBar,refresh:()=>{state.topics=[];if(q('#v-mentor.on'))load();else paintBar();},openManager,state,showTab:t=>{state.tab=t;paint();}};
})();
