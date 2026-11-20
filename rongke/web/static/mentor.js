/* 导师与课题管理：课题事实只在 topics/，文献事实只在共享库。 */
(() => {
  'use strict';
  const state = {tab:'overview', docs:{}, blockers:null, blockerStatus:'open',
                 outline:null, outlineEditOpen:false, outlineJsonMode:false,
                 outlineDraft:null, outlineSyncInfo:null,
                 labProg:null, advisorDetail:null,
                 ecosystem:null, ecoSeq:0,
                 docEditOpen:false, docMode:{plan:'append', reading:'append', evidence:'append'},
                 docDrafts:{plan:'', reading:'', evidence:''}, docVerifyMsg:'',
                 critiqueText:'', critiqueMode:'review',
                 slug:'', topics:[], library:null, candidates:null,
                 seq:0, libSeq:0, docSeq:0, blockerSeq:0, advSeq:0, busy:false, error:'', notice:''};
  const q = s => document.querySelector(s);
  const text = v => String(v == null ? '' : v);
  const html = v => text(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const live = () => typeof LIVE !== 'undefined' && !!LIVE;
  const enabled = () => typeof MENTOR !== 'undefined' && !!MENTOR;
  const current = () => state.topics.find(t => t.slug === state.slug) || null;
  function error(e) { return '<div class="mw-error">'+html(e && e.message || e)+'</div>'; }
  async function post(body) { return api('/api/topic', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }

  const DOC_TITLE = {
    plan: '研究计划 (PLAN.md)',
    reading: '精读笔记 (READING.md)',
    evidence: '证据表 (EVIDENCE.md)',
  };
  const DOC_TPL_LABEL = {
    plan: '填入研究计划骨架',
    reading: '填入精读笔记模板 〔库·doc_id p.1〕',
    evidence: '填入证据对照表模板',
  };
  const DOC_TEMPLATES = {
    plan: '# 研究计划\n\n## 1. 研究目标与核心假设\n- 核心问题：\n- 可检验假设：\n\n## 2. 阶段安排与里程碑\n1. 阶段一（文献检索与方案定型）：\n2. 阶段二（关键实验与对照验证）：\n3. 阶段三（统计复核与论文成稿）：\n\n## 3. 风险点与兜底方案\n- 主要风险：\n- 兜底措施：\n',
    reading: '## 文献精读：〔库·doc_id p.1〕\n- **研究对象与模型**：\n- **实验方法与对照设置**：\n- **样本量与关键指标（含统计）**：\n- **主要结论与原页锚点**：〔库·doc_id p.1〕\n- **局限性与本课题可借鉴/改进点**：\n',
    evidence: '| 核心论断 | 支撑来源（库内页码 / 实验台账） | 样本量与统计指标 | 强度 / 缺口状态 |\n| --- | --- | --- | --- |\n| 示例论断 1 | 〔库·doc_id p.1〕 / LAB | n=3, p<0.05 | 已核 / 待补对照 |\n',
  };

  function canRepaintQuietly() {
    const act = document.activeElement;
    if (!act) return true;
    const page = q('#mentorPage');
    if (!page || !page.contains(act)) return true;
    const tag = act.tagName;
    return tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'SELECT';
  }

  function switchTab(t) {
    state.tab = t;
    state.docVerifyMsg = '';
    paint();
    if (['plan','reading','evidence'].includes(t)) loadDoc(t, 0);
    if (t === 'blockers') loadBlockers();
    if (t === 'outline') loadOutline();
    if (t === 'advisor') loadAdvisorDetail();
  }

  async function load(slug, preserve) {
    const seq=++state.seq; state.error=''; if(!preserve) state.notice='';
    try {
      const d=await api('/api/topic'+(slug?'?slug='+encodeURIComponent(slug):''));
      if(seq!==state.seq)return;
      if(d.ok===false||d.error)throw new Error(d.error||'课题台账读取失败');
      state.topics=d.topic_details || [];
      if(!state.topics.length && d.current)state.topics=[{slug:d.current,title:d.title,question:d.question,library:d.library||[]}];
      const nextSlug=slug || d.current || state.slug || '';
      const slugChanged=nextSlug!==state.slug;
      state.slug=nextSlug;
      if(!preserve||slugChanged){
        state.library=null; state.candidates=null; state.docs={};
        state.blockers=null; state.outline=null; state.labProg=null;
        state.ecosystem=null; state.outlineDraft=null; state.outlineSyncInfo=null;
        state.docVerifyMsg=''; state.docEditOpen=false; state.outlineEditOpen=false;
      }
      paint();
      if(state.slug){ loadLibrary(); loadLabProgress(); loadEcosystem(); }
      if(['plan','reading','evidence'].includes(state.tab))loadDoc(state.tab,0);
      if(state.tab==='blockers')loadBlockers();
      if(state.tab==='outline')loadOutline();
      if(state.tab==='advisor')loadAdvisorDetail();
    }catch(e){if(seq===state.seq){state.error=e.message;paint();}}
  }
  async function loadLabProgress() {
    if(!state.slug)return;
    const slug=state.slug;
    try{
      const d=await api('/api/lab?slug='+encodeURIComponent(slug));
      if(slug===state.slug && d && d.progress){
        state.labProg=d.progress;
        if(state.tab==='overview'){
          if(canRepaintQuietly()) paint();
          else updateLpMetaInPlace();
        }
      }
    }catch(e){}
  }
  async function loadEcosystem() {
    if(!state.slug){ state.ecosystem=null; return; }
    const seq=++state.ecoSeq, slug=state.slug;
    try{
      const d=await api('/api/topic/ecosystem?slug='+encodeURIComponent(slug));
      if(seq===state.ecoSeq && slug===state.slug){
        state.ecosystem=d;
        if(state.tab==='overview'){
          if(canRepaintQuietly()) paint();
          else updateEcoStripInPlace();
        }
      }
    }catch(e){
      if(seq===state.ecoSeq && slug===state.slug){
        state.ecosystem={error:e.message};
        if(state.tab==='overview' && canRepaintQuietly()) paint();
      }
    }
  }
  async function loadOutline() {
    if(!state.slug)return;
    const slug=state.slug; state.outline=null; paint();
    try{
      const d=await api('/api/topic/outline?slug='+encodeURIComponent(slug));
      if(slug===state.slug){
        state.outline=d;
        if(!state.outlineDraft && d && d.exists && d.outline) state.outlineDraft=cloneOutline(d.outline);
        paint();
      }
    }catch(e){ if(slug===state.slug){ state.outline={error:e.message}; paint(); } }
  }
  async function loadAdvisorDetail(slugOverride) {
    const advSlug = slugOverride !== undefined ? slugOverride : (typeof ADVISOR === 'string' ? ADVISOR : '');
    if(!advSlug){ state.advisorDetail=null; paint(); return; }
    const seq=++state.advSeq; state.advisorDetail={loading:true}; paint();
    try{
      const d=await api('/api/advisor?slug='+encodeURIComponent(advSlug));
      if(seq===state.advSeq){ state.advisorDetail=d; paint(); }
    }catch(e){ if(seq===state.advSeq){ state.advisorDetail={error:e.message}; paint(); } }
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
    catch(e){state.error=e.message;}finally{state.busy=false;paint();}
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
  function launchAdvisorChat(promptText, useAnswerMode) {
    if(typeof setMentor==='function') setMentor(true);
    if(useAnswerMode && typeof setMode==='function') setMode('answer');
    const goal=q('#goal');
    if(goal){ goal.value=promptText; if(typeof autoGrow==='function') autoGrow(); }
    if(typeof goView==='function') goView('chat');
    if(goal) goal.focus();
  }

  function lpMetaText(t, lp) {
    return lp
      ? '实验进度 '+html(lp.overall_progress_pct)+'% · 实验 '+(lp.counts.experiments_done||0)+'/'+(lp.counts.experiments_total||0)+' · 逾期 '+(lp.counts.overdue_total||0)+' · 未解卡点 '+(t.open_blockers||0)
      : '结合课题资料、大纲、实验台账与卡点，让导师给出可核验的推进建议。';
  }
  function updateLpMetaInPlace() {
    const el=q('#mwLpMeta'), t=current();
    if(el && t) el.innerHTML=lpMetaText(t, state.labProg);
  }
  function ecoActionLabel(act) {
    const map={
      plan_to_lab:'一键推入实验台',
      edit_outline:'去新建/编辑大纲',
      open_outline:'查看开题大纲',
      open_lab:'前往实验台',
      open_methods:'去方法与专利',
      open_blockers:'处理卡点',
      open_plan:'编辑研究计划',
      open_reading:'打开阅读台账',
      open_evidence:'打开证据表',
      open_advisor:'配置导师档案',
    };
    return map[act] || '';
  }
  function ecoStripHtml(t) {
    const eco=state.ecosystem;
    if(!eco) return '<div class="mw-eco-loading">正在汇总 360° 课题生态诊断…</div>';
    if(eco.error) return '<div class="mw-eco-err">生态诊断读取失败：'+html(eco.error)+'</div>';
    if(eco.no_topic) return '';
    const olObj=eco.outline||{};
    const olRep=olObj.reconcile||olObj||{};
    const hasOl=Boolean(olObj.exists!==undefined?olObj.exists:(olObj.outline||olRep.ok!==undefined&&!olRep.error));
    const olStarved=olObj.starved_count!=null?Number(olObj.starved_count):(Array.isArray(olRep.starved)?olRep.starved.length:0);
    const olSecs=Number(olObj.sections!=null?olObj.sections:(olRep.sections||(olObj.outline&&olObj.outline.sections&&olObj.outline.sections.length)||0));
    const labRaw=eco.lab||{};
    const lp=labRaw.progress||labRaw||state.labProg;
    const lpCounts=(lp&&lp.counts)||{};
    const lpPct=lp&&lp.overall_progress_pct!=null?Number(lp.overall_progress_pct):0;
    const mObj=eco.methods||{};
    const mCount=Number(mObj.count!=null?mObj.count:((mObj.methods||[]).length));
    const pViews=Array.isArray(mObj.patent_views)?mObj.patent_views:[];
    const checkFirstN=mObj.check_first!=null?Number(mObj.check_first):pViews.filter(p=>p&&p.verdict==='check_first').length;
    const worthN=mObj.worth_asking!=null?Number(mObj.worth_asking):pViews.filter(p=>p&&p.verdict==='worth_asking').length;
    const tObj=eco.topic||{};
    const docsMap=tObj.docs||(t&&t.docs)||{};
    const docDone=['plan','reading','evidence'].filter(k=>!!docsMap[k]).length;
    const openBlk=Number(tObj.open_blockers!=null?tObj.open_blockers:((tObj.blockers&&tObj.blockers.open_total)!=null?tObj.blockers.open_total:(tObj.open_total!=null?tObj.open_total:(t&&t.open_blockers)||0)));
    const libN=tObj.library_count!=null?Number(tObj.library_count):(Array.isArray(tObj.library)?tObj.library.length:((t&&t.library)||[]).length);
    const advRaw=eco.advisor;
    const advCount=Array.isArray(advRaw)?advRaw.length:Number((advRaw&&advRaw.count!=null)?advRaw.count:((advRaw&&advRaw.advisors&&advRaw.advisors.length)||0));

    const pills=[
      '<span class="mw-eco-pill" data-tone="'+(hasOl?(olStarved?'warn':'ok'):'warn')+'"><b>开题大纲</b> '+(hasOl?(olSecs+' 节'+(olStarved?' · 缺数 '+olStarved:'')):'未建')+'</span>',
      '<span class="mw-eco-pill" data-tone="'+((lpCounts.overdue||lpCounts.overdue_total)?'err':(lpPct>=80?'ok':''))+'"><b>实验进度</b> '+html(lpPct)+'%（'+(lpCounts.experiments_done||0)+'/'+(lpCounts.experiments_total||0)+'）</span>',
      '<span class="mw-eco-pill" data-tone="'+(checkFirstN?'warn':(worthN?'ok':''))+'"><b>方法与专利</b> '+mCount+' 项'+(worthN?' · 可问代理人 '+worthN:(checkFirstN?' · 待补 '+checkFirstN:''))+'</span>',
      '<span class="mw-eco-pill"><b>台账文档</b> '+docDone+'/3 · 文献 '+libN+' 篇</span>',
      '<span class="mw-eco-pill" data-tone="'+(openBlk?'warn':'ok')+'"><b>未解卡点</b> '+openBlk+'</span>',
      '<span class="mw-eco-pill"><b>导师模型</b> '+advCount+' 个</span>',
    ];
    const steps=Array.isArray(eco.next_steps)?eco.next_steps:[];
    const stepsHtml=steps.length
      ? '<div class="mw-eco-steps">'+steps.map(s=>{
          const prio=s.priority==='high'?'danger':s.priority==='medium'?'warning':'info';
          const prioCn=s.priority==='high'?'优先':s.priority==='medium'?'建议':'提示';
          const lbl=ecoActionLabel(s.action);
          return '<button type="button" class="mw-eco-chip" data-mw-eco-action="'+html(s.action||'')+'" data-mw-eco-kind="'+html(s.kind||'')+'" '+(live()||state.busy?'disabled':'')+'>'+
            '<span class="tag" data-tone="'+prio+'">'+prioCn+'</span>'+
            '<span class="mw-eco-text">'+html(s.text||'')+'</span>'+
            (lbl?'<span class="mw-eco-go">'+html(lbl)+' →</span>':'')+
          '</button>';
        }).join('')+'</div>'
      : '<div class="mw-eco-steps"><span class="tag" data-tone="success">✔ 360° 课题生态各环节均已就绪</span></div>';
    return '<div class="mw-eco-metrics">'+pills.join('')+'</div>'+stepsHtml;
  }
  function updateEcoStripInPlace() {
    const el=q('#mwEcoStrip'), t=current();
    if(el && t){
      el.innerHTML=ecoStripHtml(t);
      el.querySelectorAll('[data-mw-eco-action]').forEach(b=>{
        b.onclick=()=>handleEcoAction(b.dataset.mwEcoAction, b.dataset.mwEcoKind);
      });
    }
  }
  async function handleEcoAction(act, kind) {
    if(!state.slug)return;
    if(act==='plan_to_lab'){ await runOutlineToLab(true); return; }
    if(act==='edit_outline'||act==='open_outline'){
      if(act==='edit_outline') state.outlineEditOpen=true;
      switchTab('outline');
      return;
    }
    if(act==='open_methods'){
      if(typeof goView==='function') goView('lab');
      if(window.RongkeLab && typeof window.RongkeLab.showTab==='function'){
        window.RongkeLab.showTab('methods', state.slug);
      }else{
        if(window.RongkeLab && window.RongkeLab.state) window.RongkeLab.state.tab='methods';
        const mb=q('#labPage [data-tab="methods"]');
        if(mb) mb.click();
      }
      return;
    }
    if(act==='open_lab'){
      if(typeof goView==='function') goView('lab');
      if(window.RongkeLab && typeof window.RongkeLab.showTab==='function'){
        window.RongkeLab.showTab('plan', state.slug);
      }
      return;
    }
    if(act==='open_blockers'||kind==='blocker'){ switchTab('blockers'); return; }
    if(act==='open_plan'){
      const t=current();
      if(!t||!t.docs||!t.docs.plan) state.docEditOpen=true;
      switchTab('plan');
      return;
    }
    if(act==='open_reading'){ switchTab('reading'); return; }
    if(act==='open_evidence'){ switchTab('evidence'); return; }
    if(act==='open_advisor'||kind==='advisor'){ switchTab('advisor'); return; }
    if(kind==='outline'){ switchTab('outline'); return; }
    if(kind==='lab'){
      if(typeof goView==='function') goView('lab');
      if(window.RongkeLab && typeof window.RongkeLab.showTab==='function') window.RongkeLab.showTab('plan', state.slug);
      return;
    }
    if(kind==='methods'){
      if(typeof goView==='function') goView('lab');
      if(window.RongkeLab && typeof window.RongkeLab.showTab==='function') window.RongkeLab.showTab('methods', state.slug);
      return;
    }
    if(kind==='doc'){ switchTab('plan'); return; }
  }

  function overview() {
    const t=current();
    const lp=state.labProg;
    const progBanner = t ? '<div class="mw-launchpad">'+
      '<div class="mw-lp-top">'+
        '<div class="mw-lp-info"><b>导师指导与课题验收台</b>'+
        '<span id="mwLpMeta">'+lpMetaText(t, lp)+'</span>'+
        '</div>'+
        '<div class="mw-lp-acts">'+
          '<button class="mini pri" type="button" id="mwActGuide">导师指导推进项目</button>'+
          '<button class="mini" type="button" id="mwActStudy">深入理解课题 / 追问验收</button>'+
          '<button class="mini" type="button" id="mwActCritique">诊断报告并挑错</button>'+
        '</div>'+
      '</div>'+
      '<div class="mw-eco-strip" id="mwEcoStrip">'+ecoStripHtml(t)+'</div>'+
      '</div>' : '';
    const workflow=t?'<label>当前工作阶段<select id="mwStage">'+[['ideate','构思评估'],['design','研究设计'],['evidence','文献与证据'],['execute','实验与写作'],['submission','投稿与回复']].map(x=>'<option value="'+x[0]+'">'+x[1]+'</option>').join('')+'</select></label>'+
      '<label>下一步（最多240字）<textarea id="mwNext" rows="2" maxlength="240">'+html(t.next_step)+'</textarea></label>':'';
    const actions='<div class="mw-form-actions"><button class="mini pri" id="mwSave" '+(live()?'disabled':'')+'>'+(t?'保存课题与工作阶段':'创建课题')+'</button>'+
      (t?'<button class="mini" id="mwPrepare">按此阶段准备对话</button>':'')+
      (t?'<button type="button" class="mini" id="mwDeleteTopic" '+(live()||state.busy?'disabled':'')+'>删除此课题</button>':'')+
      '<span class="mw-note">会话开始后课题锁定；运行中不能修改或切换。</span></div></div>';
    return progBanner+'<div class="mw-topic-form"><label>课题标识<input id="mwSlug" maxlength="48" value="'+html(state.slug)+'" '+(t?'readonly':'')+'></label>'+
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
    if(!state.slug)return '<div class="mw-empty">先选择课题。</div>';
    const d=state.docs[doc];if(!d)return '<div class="mw-empty">正在读取…</div>';
    if(d.error)return error(d.error);
    const total=Number(d.total_chars)||text(d.text).length,off=Number(d.offset)||0;
    const mode=state.docMode[doc]||'append';
    const draft=text(state.docDrafts[doc]);
    let h='<div class="mw-doc-head"><div class="mw-doc-meta"><b>'+html(DOC_TITLE[doc]||doc)+'</b>'+
      '<span>'+(d.empty?'尚未写入':(total+' 字'))+'</span>'+
      (state.docVerifyMsg?'<span class="mw-verified-badge">'+html(state.docVerifyMsg)+'</span>':'')+
      '</div><button type="button" class="mini" id="mwDocToggleEdit">'+(state.docEditOpen?'收起编辑':'编辑 / 追加内容')+'</button></div>';
    if(state.docEditOpen){
      h+='<div class="mw-doc-editor">'+
        '<div class="mw-doc-tools">'+
          '<label>模式 <select id="mwDocMode">'+
            '<option value="append"'+(mode==='append'?' selected':'')+'>追加一段 (append)</option>'+
            '<option value="overwrite"'+(mode==='overwrite'?' selected':'')+'>整篇保存 (overwrite)</option>'+
          '</select></label>'+
          '<button type="button" class="mini" id="mwDocTpl">'+html(DOC_TPL_LABEL[doc]||'填入模板')+'</button>'+
          '<button type="button" class="mini" id="mwDocLoadCur"'+(d.empty?' disabled':'')+'>载入当前窗口正文</button>'+
        '</div>'+
        (mode==='overwrite'&&d.truncated?'<div class="mw-warn">注意：当前文档超过单窗 12000 字，整篇保存（overwrite）将用编辑框内容替换整份文件。</div>':'')+
        '<textarea id="mwDocText" rows="8" placeholder="在此输入要追加或保存的 Markdown 内容…">'+html(draft)+'</textarea>'+
        '<div class="mw-form-actions">'+
          '<button type="button" class="mini pri" id="mwDocSave" '+(live()||state.busy?'disabled':'')+'>保存并核验落盘</button>'+
          '<span class="mw-note">写入后将自动读回核验并刷新文档窗口。</span>'+
        '</div></div>';
    }
    if(d.empty||!text(d.text)) return h+'<div class="mw-empty">这份台账还是空的，导师任务会逐步沉淀到这里，或点击上方「编辑 / 追加内容」立即录入。</div>';
    return h+'<pre class="mw-doc">'+html(d.text)+'</pre>'+(total>text(d.text).length?'<div class="mw-page"><button class="mini" data-mw-earlier="'+doc+'" '+(off<=0?'disabled':'')+'>上一段</button><span>'+(off+1)+'–'+(off+text(d.text).length)+' / '+total+' 字</span><button class="mini" data-mw-more="'+doc+'" '+(!d.truncated?'disabled':'')+'>下一段</button></div>':'');
  }

  function cloneOutline(ol) {
    const src=ol||{};
    return {
      title: text(src.title),
      question: text(src.question),
      thesis: text(src.thesis),
      sections: Array.isArray(src.sections) ? src.sections.map(s=>({
        id: text(s.id), title: text(s.title), kind: text(s.kind||'result'),
        intent: text(s.intent),
        needs: Array.isArray(s.needs) ? s.needs.map(n=>({
          kind: text(n.kind||'table'), what: text(n.what),
          status: text(n.status||'missing'), unit: text(n.unit), note: text(n.note)
        })) : []
      })) : [],
      experiments: Array.isArray(src.experiments) ? src.experiments.map(x=>({
        id: text(x.id), title: text(x.title), status: text(x.status||'planned'),
        feeds: Array.isArray(x.feeds) ? x.feeds.slice() : [],
        method: text(x.method), controls: text(x.controls),
        n: text(x.n), days: x.days!=null&&x.days!==''?Number(x.days):'',
        depends_on: Array.isArray(x.depends_on) ? x.depends_on.slice() : [],
        note: text(x.note)
      })) : []
    };
  }
  function makeOutlineDemo(t) {
    return {
      title: (t&&t.title) ? (t.title+' · 开题大纲') : '课题开题报告与数据对账大纲',
      question: (t&&t.question) || '在目标条件下，关键处理如何调控核心指标与表型变化？',
      thesis: '通过设立阳性/阴性对照与平行重复实验，验证参数优化对核心指标的提升并完成文献基准对账。',
      sections: [
        {
          id:'sec-intro', title:'1. 研究背景与立论依据', kind:'intro',
          intent:'梳理近五年文献空白与科学问题边界',
          needs:[{kind:'text', what:'核心文献对比表与关键参数基准', status:'missing', unit:'', note:'需标出库内页码'}]
        },
        {
          id:'sec-method', title:'2. 实验设计与技术路线', kind:'method',
          intent:'明确对照、样本量 n 与质控口径',
          needs:[{kind:'table', what:'实验分组与对照设计表', status:'planned', unit:'组', note:''}]
        },
        {
          id:'sec-result', title:'3. 核心结果与定量验证', kind:'result',
          intent:'验证关键处理梯度下的定量表现',
          needs:[
            {kind:'figure', what:'核心指标梯度对比曲线', status:'planned', unit:'mg/L', note:'n>=3'},
            {kind:'stat', what:'批次重复 CV% 与组间显著性检验', status:'missing', unit:'%', note:''}
          ]
        },
        {
          id:'sec-disc', title:'4. 讨论与边界条件', kind:'discussion',
          intent:'解释机制并梳理局限性与反例',
          needs:[{kind:'text', what:'失败边界与潜在创新点梳理', status:'missing', unit:'', note:''}]
        }
      ],
      experiments: [
        {
          id:'exp-1', title:'关键条件梯度与标准对照实验', status:'planned',
          feeds:['sec-method','sec-result'],
          method:'设置实验组与阳性/空白对照并测核心定量指标',
          controls:'空白对照 + 文献基准阳性对照', n:'n=3', days:5, depends_on:[], note:'供数第 2、3 节'
        }
      ]
    };
  }
  function ensureOutlineDraft() {
    if(!state.outlineDraft){
      const o=state.outline;
      if(o && o.exists && o.outline) state.outlineDraft=cloneOutline(o.outline);
      else {
        const t=current();
        state.outlineDraft={title:(t&&t.title)||'', question:(t&&t.question)||'', thesis:'', sections:[], experiments:[]};
      }
    }
    return state.outlineDraft;
  }
  function syncOutlineFromDom() {
    const d=ensureOutlineDraft();
    const ti=q('#mwOutTitle'), qu=q('#mwOutQuestion'), th=q('#mwOutThesis');
    if(ti) d.title=ti.value;
    if(qu) d.question=qu.value;
    if(th) d.thesis=th.value;
    if(state.outlineJsonMode){
      const sj=q('#mwOutSectionsJson'), ej=q('#mwOutExpsJson');
      if(sj){
        try{ const parsed=JSON.parse(sj.value||'[]'); if(Array.isArray(parsed)) d.sections=parsed; }catch(e){}
      }
      if(ej){
        try{ const parsed=JSON.parse(ej.value||'[]'); if(Array.isArray(parsed)) d.experiments=parsed; }catch(e){}
      }
      return d;
    }
    document.querySelectorAll('[data-ol-sec]').forEach(el=>{
      const si=Number(el.dataset.olSec), fld=el.dataset.field;
      if(d.sections[si] && fld) d.sections[si][fld]=el.value;
    });
    document.querySelectorAll('[data-ol-need]').forEach(el=>{
      const parts=el.dataset.olNeed.split(':'), si=Number(parts[0]), ni=Number(parts[1]), fld=el.dataset.field;
      if(d.sections[si] && d.sections[si].needs && d.sections[si].needs[ni] && fld) d.sections[si].needs[ni][fld]=el.value;
    });
    document.querySelectorAll('[data-ol-exp]').forEach(el=>{
      const xi=Number(el.dataset.olExp), fld=el.dataset.field;
      if(d.experiments[xi] && fld){
        d.experiments[xi][fld] = fld==='days' ? (el.value===''?'':Number(el.value)) : el.value;
      }
    });
    d.experiments.forEach((x, xi)=>{
      const boxes=document.querySelectorAll('[data-ol-feed-exp="'+xi+'"]');
      if(boxes.length){
        const checked=[];
        boxes.forEach(cb=>{ if(cb.checked) checked.push(cb.value); });
        x.feeds=checked;
      }
    });
    return d;
  }
  function renderOutlineEditor() {
    const d=ensureOutlineDraft();
    const secKinds=[['intro','引言'],['method','方法'],['result','结果'],['discussion','讨论']];
    const dataKinds=[['table','表格'],['figure','图'],['stat','数字'],['image','图像'],['text','定性文本']];
    const dataSt=[['missing','还没有'],['planned','已排实验'],['have','已有未核'],['done','已核对']];
    const expSt=[['planned','计划中'],['running','进行中'],['done','声明做完'],['dropped','放弃']];
    let h='<div class="mw-outline-editor">'+
      '<div class="mw-ol-group-h"><b>编辑 / 新建开题大纲</b>'+
        '<div class="mw-doc-tools">'+
          '<button type="button" class="mini" id="mwOutLoadTpl">载入开题大纲示范模板</button>'+
          '<button type="button" class="mini" id="mwOutJsonToggle">'+(state.outlineJsonMode?'返回表单编辑':'切换 JSON 编辑')+'</button>'+
          '<button type="button" class="mini" id="mwOutEditCancel">收起</button>'+
        '</div>'+
      '</div>'+
      '<div class="mw-topic-form" style="margin-bottom:0">'+
        '<label style="grid-column:1/-1">大纲标题 (title)<input id="mwOutTitle" maxlength="160" value="'+html(d.title)+'" placeholder="如：冬虫夏草人工培育与功能验证开题大纲"></label>'+
        '<label style="grid-column:1/-1">核心研究问题 (question)<textarea id="mwOutQuestion" rows="2" maxlength="2000" placeholder="要回答什么关键科学问题？">'+html(d.question)+'</textarea></label>'+
        '<label style="grid-column:1/-1">一句话主张 (thesis)<textarea id="mwOutThesis" rows="2" maxlength="2000" placeholder="预期核心结论与立论主线">'+html(d.thesis)+'</textarea></label>'+
      '</div>';
    if(state.outlineJsonMode){
      h+='<label class="mw-note">章节与数据需求 (sections JSON)<textarea id="mwOutSectionsJson" class="mw-ol-json" rows="10">'+html(JSON.stringify(d.sections||[],null,2))+'</textarea></label>'+
         '<label class="mw-note">供数实验 (experiments JSON)<textarea id="mwOutExpsJson" class="mw-ol-json" rows="8">'+html(JSON.stringify(d.experiments||[],null,2))+'</textarea></label>';
    }else{
      const secOpts=d.sections.map(s=>({id:text(s.id).trim(), title:text(s.title).trim()})).filter(s=>s.id);
      h+='<div class="mw-ol-group"><div class="mw-ol-group-h"><b>章节与数据需求（sections · '+(d.sections||[]).length+'）</b><button type="button" class="mini" id="mwOutAddSec">＋ 添加章节</button></div>';
      if(!(d.sections||[]).length) h+='<div class="mw-note">暂无章节，点击「＋ 添加章节」或「载入开题大纲示范模板」。</div>';
      h+=(d.sections||[]).map((s,si)=>{
        const needsHtml=(s.needs||[]).map((n,ni)=>
          '<div class="mw-ol-needrow">'+
            '<select data-ol-need="'+si+':'+ni+'" data-field="kind">'+dataKinds.map(o=>'<option value="'+o[0]+'"'+(n.kind===o[0]?' selected':'')+'>'+o[1]+'</option>').join('')+'</select>'+
            '<input data-ol-need="'+si+':'+ni+'" data-field="what" value="'+html(n.what)+'" placeholder="这一节需要什么数据/证据">'+
            '<select data-ol-need="'+si+':'+ni+'" data-field="status">'+dataSt.map(o=>'<option value="'+o[0]+'"'+(n.status===o[0]?' selected':'')+'>'+o[1]+'</option>').join('')+'</select>'+
            '<input data-ol-need="'+si+':'+ni+'" data-field="unit" value="'+html(n.unit||'')+'" placeholder="单位">'+
            '<button type="button" class="mini" data-ol-del-need="'+si+':'+ni+'">删需求</button>'+
          '</div>'
        ).join('');
        return '<div class="mw-ol-card">'+
          '<div class="mw-ol-row">'+
            '<label>节 ID（字母/数字/-/_）<input data-ol-sec="'+si+'" data-field="id" value="'+html(s.id)+'" placeholder="sec-1"></label>'+
            '<label>节标题<input data-ol-sec="'+si+'" data-field="title" value="'+html(s.title)+'" placeholder="如 3. 实验结果"></label>'+
            '<label>类别<select data-ol-sec="'+si+'" data-field="kind">'+secKinds.map(o=>'<option value="'+o[0]+'"'+(s.kind===o[0]?' selected':'')+'>'+o[1]+'</option>').join('')+'</select></label>'+
            '<label>本节意图<input data-ol-sec="'+si+'" data-field="intent" value="'+html(s.intent||'')+'" placeholder="可空"></label>'+
          '</div>'+
          '<div class="mw-ol-needs">'+needsHtml+
            '<div class="mw-doc-tools"><button type="button" class="mini" data-ol-add-need="'+si+'">＋ 添加数据需求</button><button type="button" class="mini" data-ol-del-sec="'+si+'">删除此节</button></div>'+
          '</div></div>';
      }).join('')+'</div>';

      h+='<div class="mw-ol-group"><div class="mw-ol-group-h"><b>供数实验（experiments · '+(d.experiments||[]).length+'）</b><button type="button" class="mini" id="mwOutAddExp">＋ 添加实验</button></div>';
      if(!(d.experiments||[]).length) h+='<div class="mw-note">暂无供数实验，点击「＋ 添加实验」。</div>';
      h+=(d.experiments||[]).map((x,xi)=>{
        const feeds=Array.isArray(x.feeds)?x.feeds:[];
        const feedChks=secOpts.length
          ? secOpts.map(so=>'<label class="mw-ol-feedchk"><input type="checkbox" data-ol-feed-exp="'+xi+'" value="'+html(so.id)+'"'+(feeds.indexOf(so.id)>=0?' checked':'')+'>'+html(so.id+(so.title?' ('+so.title+')':''))+'</label>').join('')
          : '<span class="mw-note">先添加章节，再勾选此实验为哪些章节供数。</span>';
        return '<div class="mw-ol-card">'+
          '<div class="mw-ol-row">'+
            '<label>实验 ID（字母/数字/-/_）<input data-ol-exp="'+xi+'" data-field="id" value="'+html(x.id)+'" placeholder="exp-1"></label>'+
            '<label>实验名称<input data-ol-exp="'+xi+'" data-field="title" value="'+html(x.title)+'" placeholder="实验标题"></label>'+
            '<label>状态<select data-ol-exp="'+xi+'" data-field="status">'+expSt.map(o=>'<option value="'+o[0]+'"'+(x.status===o[0]?' selected':'')+'>'+o[1]+'</option>').join('')+'</select></label>'+
            '<label>预计天数<input type="number" min="0" data-ol-exp="'+xi+'" data-field="days" value="'+html(x.days)+'" placeholder="可空"></label>'+
          '</div>'+
          '<div class="mw-ol-row">'+
            '<label>方法<input data-ol-exp="'+xi+'" data-field="method" value="'+html(x.method||'')+'" placeholder="核心方法"></label>'+
            '<label>对照<input data-ol-exp="'+xi+'" data-field="controls" value="'+html(x.controls||'')+'" placeholder="对照设置"></label>'+
            '<label>样本量 n<input data-ol-exp="'+xi+'" data-field="n" value="'+html(x.n||'')+'" placeholder="如 n=3"></label>'+
          '</div>'+
          '<div class="mw-ol-feeds"><span>供数章节 (feeds)：</span>'+feedChks+'</div>'+
          '<div class="mw-doc-tools"><button type="button" class="mini" data-ol-del-exp="'+xi+'">删除此实验</button></div>'+
          '</div>';
      }).join('')+'</div>';
    }
    h+='<div class="mw-form-actions">'+
      '<button type="button" class="mini pri" id="mwOutSave" '+(live()||state.busy?'disabled':'')+'>保存大纲并对账</button>'+
      '<span class="mw-note">保存后会同步更新 outline.json 与 OUTLINE.md，并立即与 LAB.jsonl 对账。</span>'+
      '</div></div>';
    return h;
  }

  function outlineBody() {
    if(!state.slug)return '<div class="mw-empty">先选择课题。</div>';
    const o=state.outline;
    if(!o)return '<div class="mw-empty">正在读取开题大纲与对账结果…</div>';
    if(o.error)return error(o.error);
    const rep=o.reconcile||{};
    const starved=rep.starved||[];
    const orphans=rep.orphan_experiments||[];
    const unv=rep.unverified||[];
    const emptySecs=rep.empty_sections||[];
    const missKinds=rep.missing_kinds||[];

    let h='<div class="mw-section-title"><h3>开题大纲与数据对账</h3><span>'+
      (o.exists?(starved.length?'有 '+starved.length+' 项需求缺实验供数':'各节均已对齐实验/文献'):'尚未建立结构化开题大纲')+'</span></div>';

    h+='<div class="mw-outline-acts">';
    if(o.exists){
      h+='<button type="button" class="mini pri" id="mwOutToLab" '+(live()||state.busy?'disabled':'')+'>一键将大纲缺口推入实验台待办</button>';
    }
    h+='<button type="button" class="mini" id="mwOutGoLab">前往实验台查看</button>';
    h+='<button type="button" class="mini" id="mwOutEditToggle">'+(state.outlineEditOpen?'收起大纲编辑器':'编辑 / 新建开题大纲')+'</button>';
    h+='<button type="button" class="mini" id="mwGenOutline">'+(o.exists?'请导师审阅并完善大纲':'请导师一起拟定开题大纲')+'</button>';
    h+='</div>';

    if(state.outlineSyncInfo){
      const si=state.outlineSyncInfo;
      h+='<div class="mw-notice">已同步到实验台：新增待办 '+html(si.todos_added)+' 项'+
        (si.skipped?'（已存在跳过 '+html(si.skipped)+' 项）':'')+
        '，同步实验 '+html(si.experiments_synced)+' 项。</div>';
    }
    if(state.outlineEditOpen) h+=renderOutlineEditor();
    if(!o.exists){
      return h+'<div class="mw-empty">'+html(o.why_missing||'还没有建立结构化开题大纲。可点击上方「编辑 / 新建开题大纲」并载入示范模板，或请导师一起拟定。')+'</div>';
    }

    h+='<div class="mw-reconcile"><div class="mw-reconcile-row">'+
      '<span class="tag" data-tone="'+(rep.ok?'success':'warning')+'">'+(rep.ok?'✔ 对账通过':'⚠ 存在对账缺口')+'</span>'+
      '<span>章节 '+html(rep.sections||0)+' · 实验 '+html(rep.experiments||0)+' · 需求 '+html(rep.needs||0)+'</span>'+
      (starved.length?'<span class="tag" data-tone="danger">缺供数 '+starved.length+'</span>':'')+
      (orphans.length?'<span class="tag" data-tone="warning">孤儿实验 '+orphans.length+'</span>':'')+
      (unv.length?'<span class="tag" data-tone="warning">台账未核实 '+unv.length+'</span>':'')+
      (emptySecs.length?'<span class="tag" data-tone="warning">空需求章节 '+emptySecs.length+'</span>':'')+
      '</div>';
    if(starved.length){
      h+='<div><b>缺实验供数：</b>'+starved.map(s=>html(s.section)+'（'+html(s.what)+'）').join('；')+'</div>';
    }
    if(orphans.length){
      h+='<div><b>未挂载到章节的实验：</b>'+orphans.map(x=>html(x.id)+' '+html(x.title)).join('；')+'</div>';
    }
    if(unv.length){
      h+='<div><b>台账未核实：</b>'+unv.map(u=>html(u.section)+'（'+html(u.what)+'）').join('；')+'</div>';
    }
    if(missKinds.length){
      h+='<div class="mw-note">四类章节缺失提醒：'+missKinds.map(m=>html(m.label||m.kind)).join('、')+'</div>';
    }
    h+='</div>';
    h+='<pre class="mw-doc">'+html(o.md||'')+'</pre>';
    return h;
  }

  async function runOutlineToLab(fromEco) {
    if(live()||state.busy||!state.slug)return;
    state.busy=true; state.error=''; state.notice='';
    try{
      const res=await api('/api/topic/outline',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'plan_to_lab', slug:state.slug, sync_experiments:true})
      });
      if(res.ok===false||res.error) throw new Error(res.error||'推送实验台失败');
      const todosAdded=Number(res.total_added||0);
      const expsSynced=Number(res.experiments_synced||0);
      const skippedN=Array.isArray(res.skipped)?res.skipped.length:0;
      state.outlineSyncInfo={todos_added:todosAdded, experiments_synced:expsSynced, skipped:skippedN};
      state.notice='已同步到实验台：新增待办 '+todosAdded+' 项'+(skippedN?'（跳过已有 '+skippedN+' 项）':'')+'，同步实验 '+expsSynced+' 项。';
      if(state.outline && res.reconcile) state.outline.reconcile=res.reconcile;
      await Promise.all([loadLabProgress(), loadEcosystem()]);
      if(!fromEco && state.tab==='outline') await loadOutline();
    }catch(e){ state.error=e.message; }
    finally{ state.busy=false; paint(); }
  }

  async function loadBlockers() {
    if(!state.slug)return;const seq=++state.blockerSeq,slug=state.slug;state.blockers=null;paint();
    try{const d=await api('/api/topic/blockers?slug='+encodeURIComponent(slug)+'&status='+state.blockerStatus+'&limit=100');
      if(seq===state.blockerSeq&&slug===state.slug){state.blockers=d;paint();}}
    catch(e){if(seq===state.blockerSeq){state.blockers={error:e.message};paint();}}
  }
  function blockerBody() {
    const d=state.blockers;if(!d)return '<div class="mw-empty">正在读取…</div>';if(d.error)return error(d.error);
    const addForm = '<div class="mw-topic-form" style="margin-top:12px">'+
      '<label>新卡点描述<input id="mwBlkWhat" maxlength="300" placeholder="卡在哪个实验/论证/数据问题"></label>'+
      '<label>出现位置（可空）<input id="mwBlkWhere" maxlength="120" placeholder="如 第3章 / LAB B01"></label>'+
      '<div class="mw-form-actions"><button class="mini pri" id="mwBlkAdd" '+(live()?'disabled':'')+'>记入卡点</button></div></div>';
    return '<div class="mw-doc-tools"><select id="mwBlockerStatus"><option value="open">未解决</option><option value="resolved">已解决</option><option value="all">全部</option></select><span class="mw-note">历史不会被覆盖。</span></div>'+
      ((d.items||[]).length?'<ul class="mw-blockers">'+d.items.map(b=>'<li class="mw-blocker" data-status="'+html(b.status)+'">'+
        '<div class="mw-blocker-head"><b>'+html(b.what)+'</b><span class="mw-blocker-id">'+html(b.id||'')+'</span></div>'+
        '<p>'+html(b.status==='open'?'未解决':'已解决')+' · '+html(b.opened_at||b.at)+(b.where?' · '+html(b.where):'')+'</p>'+
        (b.resolved_how?'<p>解决：'+html(b.resolved_how)+'</p>':'')+
        (b.status==='open'?'<div style="margin-top:6px"><button class="mini" data-mw-resolve="'+html(b.id)+'" '+(live()?'disabled':'')+'>标记闭环</button></div>':'')+
        '</li>').join('')+'</ul>':'<div class="mw-empty">这一状态下没有卡点。</div>')+addForm;
  }
  function critiqueBody() {
    return '<div class="mw-section-title"><h3>报告诊断与导师挑错</h3><span>粘贴论文初稿、组会讲稿、开题报告或实验周报，让导师逐条指出问题</span></div>'+
      '<div class="mw-topic-form">'+
      '<label>诊断方式<select id="mwCritMode">'+
        '<option value="review"'+(state.critiqueMode==='review'?' selected':'')+'>全面审稿（论断-证据 / 方法学 / 数字复算 / 反例）</option>'+
        '<option value="group"'+(state.critiqueMode==='group'?' selected':'')+'>组会预演（预判台下老师最先问住你的问题）</option>'+
        '<option value="defence"'+(state.critiqueMode==='defence'?' selected':'')+'>开题/答辩深挖（追问细枝末节直到吃透为止）</option>'+
      '</select></label>'+
      '<label class="mw-wide" style="grid-column:1/-1">快捷载入<div class="mw-doc-tools" style="margin-top:4px">'+
        '<button type="button" class="mini" id="mwCritLoadLab">载入当前课题实验报告 (LAB.md)</button>'+
        '<button type="button" class="mini" id="mwCritLoadOut">载入当前课题开题大纲 (OUTLINE.md)</button>'+
      '</div></label>'+
      '<label class="mw-wide" style="grid-column:1/-1">报告 / 汇报正文<textarea id="mwCritText" rows="9" placeholder="把需要导师诊断的报告正文、Markdown 表格或大纲粘贴在这里…">'+html(state.critiqueText)+'</textarea></label>'+
      '<div class="mw-form-actions"><button class="mini pri" id="mwCritRun">开始导师诊断</button>'+
      '<span class="mw-note">会自动开启「导师模式」，并逐条输出带位置、证据、影响和改法的诊断清单。</span></div></div>';
  }
  function syncAdvisorInputsToState() {
    if(!state.advisorDetail) state.advisorDetail={layer_texts:{}};
    const d=state.advisorDetail;
    const sl=q('#mwAdvSlug'), nm=q('#mwAdvName'), md=q('#mwAdvMode'), st=q('#mwAdvStrat'), sf=q('#mwAdvSuff');
    if(sl) d.slug=sl.value;
    if(nm) d.name=nm.value;
    if(md) d.mode=md.value;
    if(st) d.strategy=st.value;
    if(sf && sf.value!=='') d.sufficiency=Number(sf.value);
    if(!d.layer_texts) d.layer_texts={};
    ['method_core','academic','persona','playbook'].forEach(k=>{
      const el=q('#mwAdvL_'+k);
      if(el) d.layer_texts[k]=el.value;
    });
  }
  function advisorBody() {
    const curSlug = (typeof ADVISOR === 'string') ? ADVISOR : '';
    const d = state.advisorDetail;
    const texts = (d && d.layer_texts) || {};
    const layersMeta = [
      ['method_core','方法论核心（怎么推进/优先级/兜底）'],
      ['academic','学术判准（选题/实验规范/论文标准）'],
      ['persona','追问习惯与脾气（先问哪类/雷区）'],
      ['playbook','过线路线（最小可交付/保底方案）'],
    ];
    const stratOpts = [
      ['hybrid_distill','素材为主 + 模板补空'],
      ['strict_distill','严格仅用素材'],
      ['template_first','模板优先'],
    ];
    const suffVal = (d && d.sufficiency != null && d.sufficiency !== '') ? Number(d.sufficiency) : 60;
    let h='<div class="mw-section-title"><h3>导师档案（四层判断标准）</h3><span>'+
      (curSlug ? '当前选中：'+html(curSlug) : '当前使用：通用导师视角')+'</span></div>';
    if(typeof ADVISOR_ERR === 'string' && ADVISOR_ERR){
      h+='<div class="mw-error">导师模型清单读不到：'+html(ADVISOR_ERR)+'</div>';
    }
    h+='<div class="mw-doc-tools" style="margin-bottom:12px"><label>切换当前导师视角 '+advisorSel()+'</label>'+
      '<button type="button" class="mini" id="mwAdvNew">＋ 新建/编辑导师</button></div>';
    if(d && d.loading) h+='<div class="mw-note">正在读取导师档案详情…</div>';
    else if(d && d.error) h+=error(d.error);
    else if(d && d.audit){
      const au=d.audit;
      h+='<div class="mw-adv-audit"><div><span class="tag" data-tone="'+(au.ok?'success':'warning')+'">'+(au.ok?'✔ audit() 通过':'⚠ 待补缺口')+'</span> '+
        '<b>版本 '+html(d.version)+'</b> · 素材充分度 '+html(d.sufficiency)+'%</div>'+
        '<div class="mw-note">'+(au.complete?'四层完整，出处标注齐全':('缺层/待补：'+html((au.gaps||[]).join('；'))))+'</div></div>';
    }
    h+='<div class="mw-topic-form">'+
      '<label>导师标识（英文短横，留空按姓名生成）<input id="mwAdvSlug" value="'+html(d&&d.slug!=null?d.slug:curSlug)+'" placeholder="如 prof-zhang"></label>'+
      '<label>导师姓名<input id="mwAdvName" value="'+html(d&&d.name||'')+'" placeholder="如 张老师"></label>'+
      '<label>工作镜头<select id="mwAdvMode"><option value="academic_ideal"'+((!d||d.mode==='academic_ideal')?' selected':'')+'>学术理想型（优先严谨与完整性）</option><option value="graduation_first"'+(d&&d.mode==='graduation_first'?' selected':'')+'>毕业优先型（红线内优先可交付）</option></select></label>'+
      '<label>蒸馏策略<select id="mwAdvStrat">'+stratOpts.map(x=>'<option value="'+x[0]+'"'+(((!d||!d.strategy)&&x[0]==='hybrid_distill')||(d&&d.strategy===x[0])?' selected':'')+'>'+x[1]+'</option>').join('')+'</select></label>'+
      '<label style="grid-column:1/-1">素材充分度（0–100；strict_distill 策略下需 ≥ 40）<input id="mwAdvSuff" type="number" min="0" max="100" value="'+html(suffVal)+'"></label>'+
      '<div class="mw-adv-prov"><span class="mw-note">audit() 出处规则：非空层须包含 <code>source=user</code> 或 <code>source=material</code>（无材料请填「未挖到材料」），防止模板冒充导师原话。</span>'+
        '<button type="button" class="mini" id="mwAdvAutoSrc">自动补齐 source=user 出处标记</button></div>'+
      layersMeta.map(([k,lab])=>'<label style="grid-column:1/-1">'+lab+'（每条建议标 source=user 或 source=material，没材料可写「未挖到材料」）<textarea id="mwAdvL_'+k+'" rows="3">'+(texts[k]?html(texts[k]):'')+'</textarea></label>').join('')+
      '<div class="mw-form-actions"><button type="button" class="mini pri" id="mwAdvSave" '+(live()||state.busy?'disabled':'')+'>保存导师档案</button>'+
      (d && (d.slug || curSlug) && !d.loading && !d.error && (d.version || (typeof ADVISORS !== 'undefined' && ADVISORS.some(a => a.slug === (d.slug || curSlug))))
        ? '<button type="button" class="mini" id="mwAdvDel" '+(live()||state.busy?'disabled':'')+'>删除此导师档案</button>'
        : '')+
      '</div></div>';

    if(d && !d.loading && !d.error && Array.isArray(d.versions)){
      const vers=d.versions;
      h+='<div class="mw-adv-versions"><div class="mw-section-title"><h3>版本历史快照</h3><span>共 '+vers.length+' 个备份</span></div>';
      if(vers.length){
        h+='<ul class="mw-adv-verlist">'+vers.map(v=>
          '<li class="mw-adv-ver"><div class="mw-adv-ver-info"><b>'+html(v.version)+'</b>'+
          '<span>'+html(v.at||'')+(v.files&&v.files.length?' · '+html(v.files.join('、')):'')+'</span></div>'+
          '<button type="button" class="mini" data-mw-adv-rollback="'+html(v.version)+'" '+(live()||state.busy?'disabled':'')+'>回滚到此版</button></li>'
        ).join('')+'</ul>';
      }else{
        h+='<div class="mw-note">暂无历史版本快照（修改并保存后会生成版本备份）。</div>';
      }
      h+='</div>';
    }
    return h;
  }

  function paint() {
    const page=q('#mentorPage');if(!page)return;
    const tabs=[['overview','课题资料'],['library','本课题文献'],['outline','开题大纲'],['plan','计划'],['reading','阅读台账'],['evidence','证据表'],['blockers','卡点'],['critique','报告诊断'],['advisor','导师档案']];
    let body=state.tab==='overview'?overview()
      :state.tab==='library'?libraryBody()
      :state.tab==='outline'?outlineBody()
      :state.tab==='blockers'?blockerBody()
      :state.tab==='critique'?critiqueBody()
      :state.tab==='advisor'?advisorBody()
      :docBody(state.tab);
    page.innerHTML='<div class="mw-manager"><div class="mw-manager-head"><div><h2>导师与课题</h2><p>按课题指导推进、深入拆解主题，并对你的报告进行四镜头诊断。</p></div><button class="mini" id="mwMentor" aria-pressed="'+enabled()+'" '+(live()?'disabled':'')+'>导师模式：'+(enabled()?'已开启':'已关闭')+'</button></div>'+
      (state.error?error(state.error):'')+(state.notice?'<div class="mw-notice">'+html(state.notice)+'</div>':'')+
      '<div class="mw-layout"><aside class="mw-sidebar"><button class="mini" id="mwNew">＋ 新建课题</button><div class="mw-topic-list">'+(state.topics.length?state.topics.map(t=>'<button data-mw-topic="'+html(t.slug)+'" class="'+(t.slug===state.slug?'on':'')+'"><b>'+html(t.title||t.slug)+'</b><small>'+(t.library||[]).length+' 篇文献 · '+(t.open_blockers||0)+' 卡点</small></button>').join(''):'<div class="mw-empty">还没有课题</div>')+'</div></aside><main class="mw-main"><div class="mw-current"><b>'+html(current()&&current().title||state.slug||'未选择课题')+'</b><span>'+(live()?'本次会话课题已锁定':'选择用于新会话')+'</span></div><div class="mw-tabs">'+tabs.map(t=>'<button class="mw-tab" data-mw-tab="'+t[0]+'" aria-selected="'+(state.tab===t[0])+'">'+t[1]+'</button>').join('')+'</div><div class="mw-body">'+body+'</div></main></div></div>';
    wire(page);
    const bs=q('#mwBlockerStatus');if(bs)bs.value=state.blockerStatus;
    const ws=q('#mwStage');if(ws && current())ws.value=current().stage||'ideate';
  }
  function wire(page) {
    const mentor=q('#mwMentor');if(mentor)mentor.onclick=()=>{if(typeof toggleMentor==='function')toggleMentor();paint();};
    const nw=q('#mwNew');if(nw)nw.onclick=()=>{if(live())return;state.slug='';state.tab='overview';state.library=null;state.ecosystem=null;paint();};
    page.querySelectorAll('[data-mw-topic]').forEach(b=>b.onclick=()=>{if(live())return;state.tab='overview';state.outlineDraft=null;state.outlineSyncInfo=null;state.docVerifyMsg='';action({slug:b.dataset.mwTopic,action:'use'});});
    page.querySelectorAll('[data-mw-tab]').forEach(b=>b.onclick=()=>switchTab(b.dataset.mwTab));
    const saveBtn=q('#mwSave');if(saveBtn)saveBtn.onclick=save;
    const delTopic=q('#mwDeleteTopic');if(delTopic)delTopic.onclick=async()=>{
      if(live()||state.busy||!state.slug)return;
      const t=current();
      const label=(t && t.title) || state.slug;
      if(!confirm('确认删除课题「'+label+'」？\n\n课题目录将移入 topics/_deleted/ 回收区（可从磁盘找回）。'))return;
      state.busy=true;state.error='';state.notice='';
      try{
        const res=await post({action:'delete', slug:state.slug, confirm:true});
        if(res.ok===false||res.error) throw new Error(res.error||'删除课题失败');
        state.slug=''; state.library=null; state.outline=null; state.outlineDraft=null; state.ecosystem=null;
        await load('', true);
        state.notice='课题「'+label+'」已移入 _deleted 回收区。';
      }catch(e){ state.error=e.message; }
      finally{ state.busy=false; paint(); }
    };
    const prep=q('#mwPrepare');if(prep)prep.onclick=()=>{
      if(typeof setMentor==='function') setMentor(true);
      const t=current(), prompts={ideate:'评估这个课题的科学问题、创新性与可行性，先指出证据缺口。',
        design:'为这个课题研究设计列出可检验假设、对照、实验单位与统计计划。',
        evidence:'核对本课题文献与证据，列出能支撑关键结论的页码锚点和未闭环缺口。',
        execute:'围绕当前实验与写作推进，先处理已登记卡点和可验证的下一步。',
        submission:'进行投稿前检查；若有审稿意见，逐条对应修改与证据回复。'};
      const goal=q('#goal');if(goal)goal.value=prompts[t.stage||'ideate']+(t.next_step?' 用户记录的下一步：'+t.next_step:'');
      if(typeof goView==='function')goView('chat');
      if(goal)goal.focus();
    };
    const ag=q('#mwActGuide');if(ag)ag.onclick=()=>{
      const t=current()||{};
      launchAdvisorChat('请作为我的导师指导推进当前课题「'+(t.title||state.slug)+'」：结合现状、实验进度、逾期排期与未闭环卡点，帮我梳理最堵的瓶颈，并定出本周最优先的 1-2 个可执行动作。', false);
    };
    const ast=q('#mwActStudy');if(ast)ast.onclick=()=>{
      const t=current()||{};
      launchAdvisorChat('我想彻底吃透当前课题「'+(t.title||state.slug)+'」，请以答辩验收视角针对我的研究问题、实验设计与关键参数逐层追问我，帮我找出还没想清的细枝末节。', true);
    };
    const ac=q('#mwActCritique');if(ac)ac.onclick=()=>{ state.tab='critique'; paint(); };
    page.querySelectorAll('[data-mw-eco-action]').forEach(b=>b.onclick=()=>handleEcoAction(b.dataset.mwEcoAction, b.dataset.mwEcoKind));

    /* 文档内联编辑与追加 */
    const dTog=q('#mwDocToggleEdit');if(dTog)dTog.onclick=()=>{
      state.docEditOpen=!state.docEditOpen;
      if(state.docEditOpen){
        const doc=state.tab, d=state.docs[doc];
        if((state.docMode[doc]||'append')==='overwrite' && !text(state.docDrafts[doc]).trim() && d && d.text){
          state.docDrafts[doc]=d.text;
        }
      }
      paint();
    };
    const dMode=q('#mwDocMode');if(dMode)dMode.onchange=()=>{
      const doc=state.tab, d=state.docs[doc];
      state.docMode[doc]=dMode.value;
      if(dMode.value==='overwrite' && !text(state.docDrafts[doc]).trim() && d && d.text){
        state.docDrafts[doc]=d.text;
      }
      paint();
    };
    const dTxt=q('#mwDocText');if(dTxt)dTxt.oninput=()=>{ state.docDrafts[state.tab]=dTxt.value; };
    const dTpl=q('#mwDocTpl');if(dTpl)dTpl.onclick=()=>{
      const doc=state.tab, tpl=DOC_TEMPLATES[doc]||'';
      const cur=text(state.docDrafts[doc]);
      state.docDrafts[doc]=cur.trim() ? (cur.replace(/\s+$/,'')+'\n\n'+tpl) : tpl;
      if(dTxt) dTxt.value=state.docDrafts[doc];
      else paint();
    };
    const dLoad=q('#mwDocLoadCur');if(dLoad)dLoad.onclick=()=>{
      const doc=state.tab, d=state.docs[doc];
      if(!d || !d.text)return;
      state.docDrafts[doc]=d.text;
      if(dTxt) dTxt.value=d.text;
      else paint();
    };
    const dSave=q('#mwDocSave');if(dSave)dSave.onclick=async()=>{
      if(live()||state.busy||!state.slug)return;
      const doc=state.tab, mode=state.docMode[doc]||'append';
      const txt=text(q('#mwDocText')&&q('#mwDocText').value);
      state.docDrafts[doc]=txt;
      if(!txt.trim()){ state.error='请输入要保存的正文内容。'; paint(); return; }
      state.busy=true; state.error=''; state.notice=''; state.docVerifyMsg='';
      try{
        const res=await post({action:'doc', slug:state.slug, doc:doc, mode:mode, text:txt});
        if(res.ok===false||res.error) throw new Error(res.error||'保存失败');
        if(res.verified){
          state.docVerifyMsg='✔ 已落盘核验（verified）';
          state.notice=(DOC_TITLE[doc]||doc)+' ✔ 已落盘核验（verified · '+(res.chars||txt.length)+' 字）。';
        }else{
          state.docVerifyMsg='⚠ 写入完成但读回核验未通过';
          state.notice=(DOC_TITLE[doc]||doc)+' 已写入（verified=false）。';
        }
        if(mode==='append') state.docDrafts[doc]='';
        const t=current(); if(t && t.docs) t.docs[doc]=true;
        const curOff=Number((state.docs[doc]&&state.docs[doc].offset)||0);
        await loadDoc(doc, mode==='overwrite'?0:curOff);
        loadEcosystem();
      }catch(e){ state.error=e.message; }
      finally{ state.busy=false; paint(); }
    };

    /* 开题大纲闭环 */
    const goOut=q('#mwGenOutline');if(goOut)goOut.onclick=()=>{
      const t=current()||{};
      launchAdvisorChat('请帮当前课题「'+(t.title||state.slug)+'」制定结构化开题大纲（outline_save），明确每一节需要什么数据支撑、由哪个实验提供。', false);
    };
    const outToLab=q('#mwOutToLab');if(outToLab)outToLab.onclick=()=>runOutlineToLab(false);
    const outGoLab=q('#mwOutGoLab');if(outGoLab)outGoLab.onclick=()=>{
      if(typeof goView==='function') goView('lab');
      if(window.RongkeLab && typeof window.RongkeLab.showTab==='function') window.RongkeLab.showTab('plan', state.slug);
    };
    const outEditTog=q('#mwOutEditToggle');if(outEditTog)outEditTog.onclick=()=>{
      state.outlineEditOpen=!state.outlineEditOpen;
      if(state.outlineEditOpen) ensureOutlineDraft();
      paint();
    };
    const outEditCancel=q('#mwOutEditCancel');if(outEditCancel)outEditCancel.onclick=()=>{
      syncOutlineFromDom();
      state.outlineEditOpen=false;
      paint();
    };
    const outLoadTpl=q('#mwOutLoadTpl');if(outLoadTpl)outLoadTpl.onclick=()=>{
      state.outlineDraft=makeOutlineDemo(current());
      state.notice='已载入开题大纲示范模板，可根据课题按需修改后点击「保存大纲并对账」。';
      paint();
    };
    const outJsonTog=q('#mwOutJsonToggle');if(outJsonTog)outJsonTog.onclick=()=>{
      if(state.outlineJsonMode){
        const sj=q('#mwOutSectionsJson'), ej=q('#mwOutExpsJson');
        try{
          const d=ensureOutlineDraft();
          const ti=q('#mwOutTitle'), qu=q('#mwOutQuestion'), th=q('#mwOutThesis');
          if(ti) d.title=ti.value;
          if(qu) d.question=qu.value;
          if(th) d.thesis=th.value;
          if(sj) d.sections=JSON.parse(sj.value||'[]');
          if(ej) d.experiments=JSON.parse(ej.value||'[]');
          state.outlineJsonMode=false; state.error='';
          paint();
        }catch(e){ state.error='JSON 解析失败：'+e.message; paint(); }
      }else{
        syncOutlineFromDom();
        state.outlineJsonMode=true; state.error='';
        paint();
      }
    };
    const outAddSec=q('#mwOutAddSec');if(outAddSec)outAddSec.onclick=()=>{
      const d=syncOutlineFromDom();
      const idx=d.sections.length+1;
      let sid='sec-'+idx;
      while(d.sections.some(s=>s.id===sid)) sid='sec-'+(++idx);
      d.sections.push({id:sid, title:idx+'. 新章节', kind:'result', intent:'', needs:[{kind:'table', what:'', status:'missing', unit:'', note:''}]});
      paint();
    };
    page.querySelectorAll('[data-ol-del-sec]').forEach(b=>b.onclick=()=>{
      const d=syncOutlineFromDom(), si=Number(b.dataset.olDelSec);
      const removed=d.sections.splice(si,1)[0];
      if(removed && removed.id){
        d.experiments.forEach(x=>{ x.feeds=(x.feeds||[]).filter(f=>f!==removed.id); });
      }
      paint();
    });
    page.querySelectorAll('[data-ol-add-need]').forEach(b=>b.onclick=()=>{
      const d=syncOutlineFromDom(), si=Number(b.dataset.olAddNeed);
      if(d.sections[si]){
        d.sections[si].needs=d.sections[si].needs||[];
        d.sections[si].needs.push({kind:'table', what:'', status:'missing', unit:'', note:''});
        paint();
      }
    });
    page.querySelectorAll('[data-ol-del-need]').forEach(b=>b.onclick=()=>{
      const d=syncOutlineFromDom(), parts=b.dataset.olDelNeed.split(':'), si=Number(parts[0]), ni=Number(parts[1]);
      if(d.sections[si] && d.sections[si].needs) d.sections[si].needs.splice(ni,1);
      paint();
    });
    const outAddExp=q('#mwOutAddExp');if(outAddExp)outAddExp.onclick=()=>{
      const d=syncOutlineFromDom();
      let idx=d.experiments.length+1, xid='exp-'+idx;
      while(d.experiments.some(x=>x.id===xid)) xid='exp-'+(++idx);
      d.experiments.push({id:xid, title:'', status:'planned', feeds:[], method:'', controls:'', n:'', days:'', depends_on:[], note:''});
      paint();
    };
    page.querySelectorAll('[data-ol-del-exp]').forEach(b=>b.onclick=()=>{
      const d=syncOutlineFromDom(), xi=Number(b.dataset.olDelExp);
      d.experiments.splice(xi,1);
      paint();
    });
    page.querySelectorAll('[data-ol-sec],[data-ol-need],[data-ol-exp],[data-ol-feed-exp]').forEach(el=>{
      el.onchange=()=>syncOutlineFromDom();
    });
    const outSave=q('#mwOutSave');if(outSave)outSave.onclick=async()=>{
      if(live()||state.busy||!state.slug)return;
      let d;
      if(state.outlineJsonMode){
        d=ensureOutlineDraft();
        const ti=q('#mwOutTitle'), qu=q('#mwOutQuestion'), th=q('#mwOutThesis');
        if(ti) d.title=ti.value;
        if(qu) d.question=qu.value;
        if(th) d.thesis=th.value;
        try{
          const sj=q('#mwOutSectionsJson'), ej=q('#mwOutExpsJson');
          d.sections=JSON.parse((sj&&sj.value)||'[]');
          d.experiments=JSON.parse((ej&&ej.value)||'[]');
        }catch(e){ state.error='JSON 格式有误：'+e.message; paint(); return; }
      }else{
        d=syncOutlineFromDom();
      }
      const exps=(d.experiments||[]).map(x=>{
        const copy=Object.assign({}, x);
        if(copy.days===''||copy.days==null||Number.isNaN(Number(copy.days))) copy.days=null;
        else copy.days=Math.round(Number(copy.days));
        return copy;
      });
      state.busy=true; state.error=''; state.notice='';
      try{
        const res=await api('/api/topic/outline',{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            action:'save', slug:state.slug,
            title:text(d.title).trim(), question:text(d.question).trim(), thesis:text(d.thesis).trim(),
            sections:d.sections||[], experiments:exps
          })
        });
        if(res.ok===false||res.error) throw new Error(res.error||'保存开题大纲失败');
        state.outline={ok:true, slug:state.slug, exists:true, outline:res.outline, reconcile:res.reconcile, md:res.md};
        if(res.outline) state.outlineDraft=cloneOutline(res.outline);
        const rep=res.reconcile||{};
        state.notice='开题大纲已保存并对账（verified='+Boolean(res.verified)+'，章节 '+(rep.sections||0)+'，缺供数 '+(rep.starved||[]).length+' 项）。';
        loadEcosystem();
      }catch(e){ state.error=e.message; }
      finally{ state.busy=false; paint(); }
    };

    const find=q('#mwFindGo');if(find)find.onclick=findDocs;
    const inp=q('#mwFind');if(inp)inp.onkeydown=e=>{if(e.key==='Enter')findDocs();};
    page.querySelectorAll('[data-mw-link]').forEach(b=>b.onclick=()=>action({slug:state.slug,action:'link',doc_id:b.dataset.mwLink}));
    page.querySelectorAll('[data-mw-unlink]').forEach(b=>b.onclick=()=>action({slug:state.slug,action:'unlink',doc_id:b.dataset.mwUnlink}));
    page.querySelectorAll('[data-mw-open]').forEach(b=>b.onclick=()=>{if(window.RongkeLibrary)RongkeLibrary.open(b.dataset.mwOpen);});
    const bs=q('#mwBlockerStatus');if(bs)bs.onchange=()=>{state.blockerStatus=bs.value;loadBlockers();};
    const badd=q('#mwBlkAdd');if(badd)badd.onclick=async()=>{
      const w=text(q('#mwBlkWhat')&&q('#mwBlkWhat').value).trim();
      const wh=text(q('#mwBlkWhere')&&q('#mwBlkWhere').value).trim();
      if(!w){state.error='请填写卡点描述。';paint();return;}
      try{
        await post({action:'raise_blocker',slug:state.slug,what:w,where:wh});
        state.notice='已记入卡点台账。';
        loadEcosystem();
        await loadBlockers();
      }catch(e){state.error=e.message;paint();}
    };
    page.querySelectorAll('[data-mw-resolve]').forEach(b=>b.onclick=async()=>{
      try{
        await post({action:'resolve_blocker',slug:state.slug,id:b.dataset.mwResolve,how:'已在控制台闭环'});
        state.notice='卡点 '+b.dataset.mwResolve+' 已闭环。';
        loadEcosystem();
        await loadBlockers();
      }catch(e){state.error=e.message;paint();}
    });
    page.querySelectorAll('[data-mw-more]').forEach(b=>b.onclick=()=>{const d=state.docs[b.dataset.mwMore]||{};loadDoc(b.dataset.mwMore,Number(d.offset||0)+text(d.text).length);});
    page.querySelectorAll('[data-mw-earlier]').forEach(b=>b.onclick=()=>{const d=state.docs[b.dataset.mwEarlier]||{};loadDoc(b.dataset.mwEarlier,Math.max(0,Number(d.offset||0)-12000));});

    const clLab=q('#mwCritLoadLab');if(clLab)clLab.onclick=async()=>{
      if(!state.slug)return;
      try{const d=await api('/api/lab/report?slug='+encodeURIComponent(state.slug));
        state.critiqueText=d.md||''; paint();
      }catch(e){state.error=e.message;paint();}
    };
    const clOut=q('#mwCritLoadOut');if(clOut)clOut.onclick=async()=>{
      if(!state.slug)return;
      try{const d=await api('/api/topic/outline?slug='+encodeURIComponent(state.slug));
        state.critiqueText=d.md||''; if(!d.md) state.error='当前课题还没有开题大纲。'; paint();
      }catch(e){state.error=e.message;paint();}
    };
    const cm=q('#mwCritMode');if(cm)cm.onchange=()=>{state.critiqueMode=cm.value;};
    const ct=q('#mwCritText');if(ct)ct.oninput=()=>{state.critiqueText=ct.value;};
    const crun=q('#mwCritRun');if(crun)crun.onclick=()=>{
      const txt=text(q('#mwCritText')&&q('#mwCritText').value).trim();
      state.critiqueText=txt;
      if(!txt){state.error='请先贴入报告内容或从快捷按钮载入。';paint();return;}
      const mode=q('#mwCritMode')?q('#mwCritMode').value:state.critiqueMode;
      const prefix = mode==='group'
        ? '明天组会要汇报下面这份材料，请用导师预演视角指出台下会被当面问住的问题、证据缺口和怎么答：\n\n'
        : mode==='defence'
        ? '准备开题答辩，请针对下面这份材料一路往下追问细枝末节，帮我查出真正答不出来的地方：\n\n'
        : '请以导师视角审查下面这份报告，按四个镜头逐条指出论断、方法、数字复算与边界反例上的潜在问题：\n\n';
      launchAdvisorChat(prefix+txt, false);
    };

    const advSel=page.querySelector('#mwAdvisor');
    if(advSel) advSel.onchange=()=>{
      if(typeof setAdvisor==='function') setAdvisor(advSel.value, advSel.value?'这一场按所选导师的判准评':'已退回通用导师视角');
      loadAdvisorDetail(advSel.value);
    };
    const advNew=q('#mwAdvNew');if(advNew)advNew.onclick=()=>{ state.advisorDetail={slug:'',name:'',mode:'academic_ideal',strategy:'hybrid_distill',sufficiency:60,versions:[],layer_texts:{}}; paint(); };
    page.querySelectorAll('#mwAdvSlug,#mwAdvName,#mwAdvMode,#mwAdvStrat,#mwAdvSuff,[id^="mwAdvL_"]').forEach(el=>{
      el.oninput=()=>syncAdvisorInputsToState();
      el.onchange=()=>syncAdvisorInputsToState();
    });
    const advAutoSrc=q('#mwAdvAutoSrc');if(advAutoSrc)advAutoSrc.onclick=()=>{
      syncAdvisorInputsToState();
      let fixed=0;
      ['method_core','academic','persona','playbook'].forEach(k=>{
        const el=q('#mwAdvL_'+k);
        if(!el)return;
        const raw=text(el.value).trim();
        if(!raw || raw==='未挖到材料' || raw.indexOf('source=')>=0) return;
        const next=raw+'\n<!-- source=user -->';
        el.value=next;
        if(state.advisorDetail && state.advisorDetail.layer_texts) state.advisorDetail.layer_texts[k]=next;
        fixed++;
      });
      state.error='';
      state.notice=fixed
        ? ('已为 '+fixed+' 个非空层补齐 source=user 出处标记；点击「保存导师档案」即可落盘并通过 audit() 校验。')
        : '所有已填写层均已含 source= 标记（或为「未挖到材料」），无需补齐。';
      paint();
    };
    const advSave=q('#mwAdvSave');if(advSave)advSave.onclick=async()=>{
      if(live()||state.busy)return;
      const sl=text(q('#mwAdvSlug')&&q('#mwAdvSlug').value).trim();
      const nm=text(q('#mwAdvName')&&q('#mwAdvName').value).trim();
      if(!sl && !nm){state.error='请填写导师标识或姓名。';paint();return;}
      const layers={};
      ['method_core','academic','persona','playbook'].forEach(k=>{
        const el=q('#mwAdvL_'+k);
        if(el && el.value.trim()) layers[k]=el.value.trim();
      });
      const suffRaw=q('#mwAdvSuff')?Number(q('#mwAdvSuff').value):60;
      const suff=Number.isFinite(suffRaw)?Math.max(0,Math.min(100,Math.round(suffRaw))):60;
      state.busy=true; state.error=''; state.notice='';
      try{
        const res=await api('/api/advisor',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            slug:sl, name:nm,
            mode:q('#mwAdvMode')?q('#mwAdvMode').value:'academic_ideal',
            strategy:q('#mwAdvStrat')?q('#mwAdvStrat').value:'hybrid_distill',
            sufficiency:suff,
            layers,
          })});
        if(res.ok===false||res.error) throw new Error(res.error||'保存导师档案失败');
        if(typeof loadAdvisors==='function') await loadAdvisors();
        if(typeof setAdvisor==='function' && res.slug) setAdvisor(res.slug, '已切换到刚保存的导师档案');
        await loadAdvisorDetail(res.slug);
        state.notice='导师档案「'+(res.name||res.slug)+'」已保存（'+res.version+'，verified='+res.verified+(res.complete?'，audit 通过':'')+'）。';
        loadEcosystem();
      }catch(e){state.error=e.message;}
      finally{ state.busy=false; paint(); }
    };
    const advDel=q('#mwAdvDel');if(advDel)advDel.onclick=async()=>{
      if(live()||state.busy)return;
      const sl=(state.advisorDetail&&state.advisorDetail.slug)||text(q('#mwAdvSlug')&&q('#mwAdvSlug').value).trim()||(typeof ADVISOR==='string'?ADVISOR:'');
      if(!sl){state.error='没有可删除的导师标识。';paint();return;}
      if(!confirm('确认删除导师档案「'+sl+'」？\n\n档案将移入 topics/advisors/_deleted/ 回收区（可从磁盘找回）。'))return;
      state.busy=true; state.error=''; state.notice='';
      try{
        const res=await api('/api/advisor',{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({action:'delete', slug:sl, confirm:true})
        });
        if(res.ok===false||res.error) throw new Error(res.error||'删除导师档案失败');
        if(typeof loadAdvisors==='function') await loadAdvisors();
        if(typeof setAdvisor==='function' && typeof ADVISOR==='string' && ADVISOR===sl){
          setAdvisor('', '已删除当前导师档案，退回通用导师视角');
        }
        state.advisorDetail=null;
        state.notice='导师档案「'+sl+'」已移入 _deleted 回收区。';
        loadEcosystem();
      }catch(e){ state.error=e.message; }
      finally{ state.busy=false; paint(); }
    };
    page.querySelectorAll('[data-mw-adv-rollback]').forEach(b=>b.onclick=async()=>{
      if(live()||state.busy)return;
      const ver=b.dataset.mwAdvRollback;
      const sl=(state.advisorDetail&&state.advisorDetail.slug)||text(q('#mwAdvSlug')&&q('#mwAdvSlug').value).trim()||(typeof ADVISOR==='string'?ADVISOR:'');
      if(!sl||!ver)return;
      state.busy=true; state.error=''; state.notice='';
      try{
        const res=await api('/api/advisor',{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({action:'rollback', slug:sl, version:ver})
        });
        if(res.ok===false||res.error) throw new Error(res.error||'回滚失败');
        if(typeof loadAdvisors==='function') await loadAdvisors();
        await loadAdvisorDetail(sl);
        state.notice='导师档案「'+sl+'」已回滚到版本 '+ver+'（恢复 '+((res.restored||[]).length)+' 个文件）。';
        loadEcosystem();
      }catch(e){ state.error=e.message; }
      finally{ state.busy=false; paint(); }
    });
  }
  /* ★ 导师模型（2026-11-14 续）：这个横条是导师模式**时时在眼前**的那一层，
     下拉框必须在这里 —— 只放在课题页里，用户要先点一步才能选导师。
     值从 workspace.js 的 ADVISOR / ADVISORS 取（单源；两份状态就是两尚真相）。 */
  function advisorSel() {
    if (typeof ADVISORS === 'undefined') return '';
    const cur = typeof ADVISOR === 'string' ? ADVISOR : '';
    if (typeof ADVISOR_ERR === 'string' && ADVISOR_ERR) {
      return '<span class="tag" data-tone="warning">导师模型清单读不到</span>';
    }
    if (!ADVISORS.length) return '<span class="mw-advisor-none">导师模型：还没建过</span>';
    const opts = ADVISORS.map(a => '<option value="' + html(a.slug) + '"' +
      (a.slug === cur ? ' selected' : '') + '>' + html(a.name || a.slug) +
      '（' + html(a.version || 'v1') + '）</option>').join('');
    return '<select class="mini" id="mwAdvisor"><option value=""' + (cur ? '' : ' selected') +
      '>通用导师视角</option>' + opts + '</select>';
  }
  function paintBar() {
    const bar=q('#mentorBar');if(!bar)return;
    if(!enabled()){bar.innerHTML='';return;}
    const t=current();
    bar.innerHTML='<div class="mw-bar"><span>导师模式 · '+html(t&&t.title||state.slug||'尚未选择课题')+'</span>'+
      advisorSel()+'<button class="mini" id="mwManage">管理导师与课题</button></div>';
    const b=q('#mwManage');if(b)b.onclick=()=>goView('mentor');
    const a=q('#mwAdvisor');
    if(a)a.onchange=()=>{
      if(typeof setAdvisor==='function')setAdvisor(a.value,
        a.value?'这一场按所选导师的判准评':'已退回通用导师视角');
    };
  }
  /* 开面板时顺手把导师清单也拉一次：横条和面板都要能选导师，
     而两处读的是**同一份**内核清单（workspace.js 的 ADVISORS）。 */
  function openManager(){
    if(typeof loadAdvisors==='function' && typeof ADVISORS!=='undefined' && !ADVISORS.length) loadAdvisors();
    if(!state.topics.length&&!state.busy)load();else paint();
  }
  window.RongkeMentor={paint:paintBar,refresh:()=>{if(q('#v-mentor.on')){if(!state.busy)load(undefined,true);}else{state.topics=[];paintBar();}},openManager,state,showTab:t=>switchTab(t)};
})();
