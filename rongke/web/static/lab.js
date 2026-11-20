/* 实验台：规划与排期 / 成果与进度 / 整体分析 / 全部项目，全部现算自 topics/<slug>/LAB.jsonl。
   ★ 这一页**不另存一份数据、不另算一套折叠**：矩阵、缺口、下一步、实验计划、随访排期、
     项目进度、作废标记一律来自 /api/lab（后端转 rongke/lab.py），前端只负责画与收集输入。
     录入走 /api/lab → lab.record 与 /api/lab/plan → lab.import_plan，与内核同一处校验。 */
(() => {
  'use strict';
  const state = {slug:'', topics:[], current:'', data:null, tab:'plan',
                 error:'', notice:'', busy:false, seq:0,
                 formKind:'measure', retractSeq:null, retractMethodSeq:null, report:null, reportSeq:0,
                 methodsData:null, methodsSeq:0, methodFormKind:'method',
                 methodDraft:{id:'', title:'', origin:'adapted', version:'2',
                              differs_from:'', what_changed:'', paramsText:'',
                              stepsText:'', pitfallsText:'', instrument:'',
                              consumable:'', yield:'', note:'',
                              obsVer:'', obsStatus:'worked', obsText:'',
                              disclosed_at:'', first_public:'', claimsText:''},
                 plateDraft:{wells:'', high:'A1-A6', low:'H1-H6', min_controls:'4', batch:'', strain:''},
                 plateResult:null,
                 planDraft:'', planTitle:'', planStart:'', planDays:3, parsedPlan:null,
                 progressEditId:null};
  const q = s => document.querySelector(s);
  const text = v => String(v == null ? '' : v);
  const esc = v => text(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const html = esc;
  const live = () => typeof LIVE !== 'undefined' && !!LIVE;
  const num = v => { const s = text(v).trim(); if (!s) return undefined; const n = Number(s); return Number.isNaN(n) ? undefined : n; };
  const KIND_CN = {strain:'菌株', batch:'批次', measure:'定量', observe:'定性', metric:'指标',
                   todo:'待办', plan:'实验计划', experiment:'实验步骤', progress:'进度更新',
                   update:'更新', close:'完成', retract:'作废'};
  const EXP_STATUS_CN = {planned:'计划中', running:'进行中', blocked:'受阻', done:'已完成', dropped:'已放弃'};
  const STAGE_CN = {ideate:'构思评估', design:'研究设计', evidence:'文献与证据', execute:'实验与写作', submission:'投稿与回复'};
  const ORIGIN_LIST = [
    ['adapted', '改编自库内文献（adapted）'],
    ['own', '自主设计（own）'],
    ['literature', '照文献复现（literature）'],
    ['standard', '行业标准/试剂盒（standard）'],
  ];
  const VERDICT_TONE = {worth_asking:'ok', check_first:'warn', published:'err', weak:'neutral'};
  const PLATE_DEMO = 'A1=1.234 A2=1.198 A3=nan A4=1.241 A5=1.205 A6=1.219 H1=0.102 H2=<0.05 H3=0.099 H4=ND H5=0.105 H6=0.098 B1=0.412 B2=0.988 B3=0.135 B4=0.764';
  const GROWTH = [['slow','慢'],['fast','快'],['normal','正常'],['none','不长'],['dead','已死']];
  const ROLE = [['candidate','候选'],['fusant','融合子'],['parent','亲本']];
  const GAP_CN = {missing:'无测量', few_reps:'n 不足', no_control:'缺亲本对照'};
  const BUCKET_META = [
    ['overdue', '已逾期', 'err'],
    ['today', '今日跟进', 'warn'],
    ['this_week', '7 天内到期', 'info'],
    ['upcoming', '后续排期', 'neutral'],
    ['unscheduled', '未定截止日', 'neutral'],
  ];

  function errBox(e){ return '<div class="lb-error">'+esc(e && e.message || e)+'</div>'; }
  function fmtRich(s){
    return esc(s)
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`\n]+)`/g, '<code class="lb-code">$1</code>');
  }
  function fmtNum(v){
    const n=Number(v);
    if(v==null||v===''||!Number.isFinite(n)) return '—';
    if(Math.abs(n)>=1000||(Math.abs(n)>0&&Math.abs(n)<0.001)) return n.toExponential(2);
    return String(Number(n.toPrecision(4)));
  }
  function switchToTask(promptText){
    if(!promptText) return;
    if(typeof setMentor==='function') setMentor(true);
    const goal=q('#goal');
    if(goal){
      goal.value=promptText;
      if(typeof autoGrow==='function') autoGrow();
    }
    if(typeof goView==='function') goView('chat');
    if(goal) goal.focus();
  }
  async function copyText(str, okMsg){
    state.error='';
    try{
      let copied=false;
      if(navigator.clipboard && navigator.clipboard.writeText){
        try{ await navigator.clipboard.writeText(str); copied=true; }catch(e){}
      }
      if(!copied){
        const ta=document.createElement('textarea');
        ta.value=str; ta.style.position='fixed'; ta.style.opacity='0';
        document.body.appendChild(ta); ta.select();
        try{ copied=document.execCommand('copy'); }catch(e){}
        ta.remove();
      }
      if(!copied) throw new Error('浏览器未授权剪贴板，请手动选中文本复制。');
      state.notice=okMsg||'已复制到剪贴板。';
    }catch(e){ state.error='复制失败：'+e.message; }
    paint();
  }

  async function load(slug, keepNotice){
    const seq = ++state.seq; state.error=''; if(!keepNotice) state.notice='';
    try{
      const d = await api('/api/lab'+(slug?'?slug='+encodeURIComponent(slug):''));
      if(seq!==state.seq) return;
      if(d.ok===false||d.error) throw new Error(d.error||'实验台账读取失败');
      const prevSlug = state.slug;
      state.data=d; state.topics=d.topics||[]; state.current=d.current||'';
      state.slug=d.slug||slug||''; state.retractSeq=null; state.progressEditId=null;
      if(prevSlug !== state.slug){
        state.plateResult=null;
        state.methodsData=null;
      }
      paint();
      if(state.slug){
        loadMethods(state.slug);
        if(state.tab==='analysis') loadReport();
      }
    }catch(e){ if(seq===state.seq){ state.error=e.message; state.data=null; paint(); } }
  }

  async function loadMethods(slug){
    const targetSlug=slug||state.slug;
    if(!targetSlug) return;
    const seq=++state.methodsSeq;
    try{
      const d=await api('/api/methods?slug='+encodeURIComponent(targetSlug));
      if(seq===state.methodsSeq && targetSlug===state.slug){
        state.methodsData=d;
        paint();
      }
    }catch(e){
      if(seq===state.methodsSeq && targetSlug===state.slug){
        state.methodsData={error:e.message, methods:[], md:''};
        paint();
      }
    }
  }

  async function loadReport(){
    if(!state.slug) return;
    const seq=++state.reportSeq, slug=state.slug; state.report=null;
    try{
      const d=await api('/api/lab/report?slug='+encodeURIComponent(slug));
      if(seq===state.reportSeq && state.slug===slug){ state.report=d; paint(); }
    }catch(e){ if(seq===state.reportSeq && state.slug===slug){ state.report={error:e.message}; paint(); } }
  }

  async function record(fields){
    if(state.busy) return;
    if(live()){ state.error='任务运行中，结束后再记台账。'; paint(); return; }
    if(!state.slug){ state.error='先选择（或新建）一个课题。'; paint(); return; }
    state.busy=true; state.error=''; state.notice='';
    try{
      const d=await api('/api/lab',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(Object.assign({slug:state.slug},fields))});
      if(d.ok===false||d.error) throw new Error(d.error||'记录失败');
      const ev=d.event||{};
      state.notice = fields.kind==='retract'
        ? '已作废 #'+text(ev.target_seq)+'，并读回核验。原始行仍在台账里。'
        : '已记录 #'+text(ev.seq)+' '+(KIND_CN[ev.kind]||ev.kind)+'，并读回核验（verified='+d.verified+'）。';
      state.retractSeq=null; state.progressEditId=null;
      await load(state.slug, true);
    }catch(e){ state.error=e.message; paint(); }
    finally{ state.busy=false; }
  }

  async function submitPlan(previewOnly){
    if(state.busy) return;
    const raw=text(q('#lbPlanText')&&q('#lbPlanText').value).trim();
    const title=text(q('#lbPlanTitle')&&q('#lbPlanTitle').value).trim();
    const start=text(q('#lbPlanStart')&&q('#lbPlanStart').value).trim();
    const days=num(q('#lbPlanDays')&&q('#lbPlanDays').value) || 3;
    state.planDraft=raw; state.planTitle=title; state.planStart=start; state.planDays=days;
    if(!raw){ state.error='请粘贴或输入实验计划内容。'; paint(); return; }
    if(!previewOnly && live()){ state.error='任务运行中，结束后再导入实验计划。'; paint(); return; }
    if(!previewOnly && !state.slug){ state.error='先选择（或新建）一个课题。'; paint(); return; }
    state.busy=true; state.error=''; state.notice='';
    try{
      const d=await api('/api/lab/plan',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          action: previewOnly ? 'parse' : 'import',
          slug: state.slug, title, raw_text: raw, start_date: start,
          default_follow_up_days: days, auto_schedule: true,
        })});
      if(d.ok===false||d.error) throw new Error(d.error||'实验计划处理失败');
      if(previewOnly){
        state.parsedPlan=d.parsed;
        paint();
      }else{
        state.parsedPlan=null; state.planDraft=''; state.planTitle='';
        state.notice='已按课题「'+state.slug+'」接收计划 '+text(d.plan_id)+'：创建 '+
          (d.experiments||[]).length+' 个实验步骤，自动排定 '+(d.todos||[]).length+' 项跟进任务（verified='+d.verified+'）。';
        await load(state.slug, true);
      }
    }catch(e){ state.error=e.message; paint(); }
    finally{ state.busy=false; }
  }

  async function createTopic(){
    const slug=text(q('#lbNewSlug')&&q('#lbNewSlug').value).trim();
    const title=text(q('#lbNewTitle')&&q('#lbNewTitle').value).trim();
    const question=text(q('#lbNewQ')&&q('#lbNewQ').value).trim();
    if(!slug){ state.error='请填写课题标识（字母/数字/中文/短横，≤48 字）。'; paint(); return; }
    if(live()){ state.error='任务运行中，结束后再建课题。'; paint(); return; }
    state.busy=true; state.error='';
    try{
      const d=await api('/api/topic',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'use',slug,title,question})});
      if(d.ok===false||d.error) throw new Error(d.error||'建课题失败');
      state.slug=slug; await load(slug);
    }catch(e){ state.error=e.message; paint(); }
    finally{ state.busy=false; }
  }

  function head(){
    const opts=state.topics.map(t=>'<option value="'+html(t.slug)+'"'+(t.slug===state.slug?' selected':'')+'>'+html(t.title||t.slug)+'</option>').join('');
    const sel=state.topics.length
      ? '<label>查看的课题<select id="lbTopic">'+opts+'</select></label>'
      : '';
    return '<div class="lb-head"><div><h2>实验台</h2><p>接收实验计划、自动排定后续跟进、追踪实验进展并按课题存档（LAB.jsonl）。</p></div>'+
      '<div class="lb-topic">'+sel+'<button class="mini" id="lbRefresh">刷新</button></div></div>';
  }

  function noTopic(){
    return '<div class="lb-card"><h3>先立一个课题</h3><p class="lb-note">实验计划、排期和进展都按课题保存。还没有课题 —— 先建一个，或去「导师与课题」页。</p>'+
      '<div class="lb-form">'+
      '<label>课题标识<input id="lbNewSlug" maxlength="48" placeholder="如 cordyceps-fusant"></label>'+
      '<label>课题名称<input id="lbNewTitle" maxlength="160" placeholder="如 蛹虫草融合子选育"></label>'+
      '<label class="lb-wide">研究问题<textarea id="lbNewQ" rows="3" maxlength="1200" placeholder="想回答什么？"></textarea></label>'+
      '<div class="lb-actions"><button class="mini pri" id="lbCreate" '+(live()?'disabled':'')+'>创建课题</button>'+
      '<button class="mini" id="lbGoMentor">去导师与课题页</button></div></div></div>';
  }

  function summary(){
    const st=state.data.state||{};
    const prog=state.data.progress||{};
    const cnt=prog.counts||{};
    const pct=Number(prog.overall_progress_pct||0);
    const batches=Object.values(st.batches||{});
    const running=batches.filter(b=>b.status!=='done').length;
    const todos=Object.values(st.todos||{}).filter(t=>t.status!=='closed').length;
    const cells=[
      ['总体进度', pct+'%'],
      ['实验步骤', (cnt.experiments_done||0)+'/'+(cnt.experiments_total||0)],
      ['待办/跟进', todos+(cnt.overdue_total?'（逾期 '+cnt.overdue_total+'）':'')],
      ['菌株', Object.keys(st.strains||{}).length],
      ['批次', batches.length+'（进行中 '+running+'）'],
      ['定量 / 缺口', (st.measures||[]).length+' / '+(state.data.gaps||[]).length],
    ];
    return '<div class="lb-progwrap"><div class="lb-progbar"><i style="width:'+pct+'%"></i></div>'+
      '<div class="lb-progmeta"><span>课题 <b>'+html(state.slug)+'</b> 总体实验进度 <b>'+pct+'%</b></span>'+
      '<span>进行中实验 '+(cnt.experiments_running||0)+' · 受阻 '+(cnt.experiments_blocked||0)+' · 逾期跟进 '+(cnt.overdue_total||0)+'</span></div></div>'+
      '<div class="lb-summary">'+cells.map(c=>'<div class="lb-stat"><b>'+html(c[1])+'</b><span>'+c[0]+'</span></div>').join('')+
      (st.n_retracted?'<div class="lb-stat lb-stat-muted"><b>'+st.n_retracted+'</b><span>已作废</span></div>':'')+'</div>';
  }

  function planImportBox(){
    const today=state.planStart || new Date().toISOString().slice(0,10);
    const parsed=state.parsedPlan;
    let pv='';
    if(parsed && parsed.steps){
      pv='<div class="lb-parsed"><b>解析预览：'+html(parsed.title)+'（共 '+parsed.steps.length+' 步，预计至 '+html(parsed.target_date||'')+'）</b>'+
        '<ol>'+parsed.steps.map(s=>'<li><b>'+html(s.title)+'</b>'+(s.phase?' <small>['+html(s.phase)+']</small>':'')+
        ' <small>起始 '+html(s.started)+' → 截止 '+html(s.due)+' · 跟进 '+html(s.follow_up_at)+'</small></li>').join('')+'</ol></div>';
    }
    return '<div class="lb-section"><h3>接收实验计划并自动排期<span class="lb-sub">贴入编号步骤/Day 1-3/预计天数，自动拆成实验与随访日程</span></h3>'+
      '<div class="lb-form">'+
      '<label>计划名称（可空，默认取首行）<input id="lbPlanTitle" value="'+html(state.planTitle)+'" placeholder="如 第一阶段发酵优化计划"></label>'+
      '<label>起始日期<input id="lbPlanStart" type="date" value="'+html(today)+'"></label>'+
      '<label>默认跟进间隔（天）<input id="lbPlanDays" type="number" min="1" max="60" value="'+html(state.planDays||3)+'"></label>'+
      '<label class="lb-wide">实验计划全文<textarea id="lbPlanText" rows="4" placeholder="例如：\n1. 完成亲本与融合子核相染色阳性对照（预计 2 天）\n2. 摇瓶液体发酵并每 24h 取样测得率（Day 3-7）\n3. 测定甘露醇与腺苷含量（预计 3 天，关键）">'+html(state.planDraft)+'</textarea></label>'+
      '<div class="lb-actions lb-wide">'+
        '<button class="mini" id="lbPlanParse" type="button">预览解析</button>'+
        '<button class="mini pri" id="lbPlanImport" type="button" '+(live()?'disabled':'')+'>接收并排期入账</button>'+
        '<span class="lb-note">入账后写入当前课题的 LAB.jsonl，可在下方随时更新进度或改期。</span>'+
      '</div></div>'+pv+'</div>';
  }

  function scheduleBoard(){
    const prog=state.data.progress||{};
    const sched=prog.schedule||{};
    let groups='';
    BUCKET_META.forEach(([key, label, tone])=>{
      const items=sched[key]||[];
      if(!items.length) return;
      groups+='<div class="lb-bucket"><div class="lb-bucket-h"><span class="tag" data-tone="'+tone+'">'+label+'</span><b>'+items.length+' 项</b></div>'+
        '<ul class="lb-list">'+items.map(it=>scheduleRow(it)).join('')+'</ul></div>';
    });
    if(!groups) groups='<div class="lb-empty">还没有已排期的实验或待办。在上方导入实验计划，或新增待办/实验。</div>';
    return '<div class="lb-section"><h3>后续随访排期与进度追踪<span class="lb-sub">按时效自动分桶：已逾期 / 今日 / 本周 / 后续</span></h3>'+groups+'</div>';
  }

  function scheduleRow(it){
    const pct=Number(it.progress||0);
    const id=text(it.id);
    const isExp=it.kind==='experiment';
    const isTodo=it.kind==='todo';
    const isEditing=state.progressEditId===id;
    const dueInfo=it.follow_up_at||it.due
      ? ('跟进 '+(it.follow_up_at||it.due)+(it.days_left!=null?(it.days_left<0?'（已逾期 '+Math.abs(it.days_left)+' 天）':it.days_left===0?'（今日）':'（还 '+it.days_left+' 天）'):''))
      : '未定日期';
    let row='<li class="lb-sched-item" data-id="'+html(id)+'">'+
      '<div class="lb-sched-main"><div><span class="lb-evkind" data-k="'+html(it.kind)+'">'+html(KIND_CN[it.kind]||it.kind)+'</span> '+
      '<b>'+html(id)+' · '+html(it.title||id)+'</b></div>'+
      '<small>'+html(dueInfo)+' · 状态 '+html(EXP_STATUS_CN[it.status]||it.status||'open')+' · 进度 '+pct+'%'+
      (it.last_note?' · 「'+html(it.last_note)+'」':'')+'</small>'+
      (isExp?'<div class="lb-minibar"><i style="width:'+pct+'%"></i></div>':'')+
      '</div><div class="lb-sched-acts">'+
      ((isExp||isTodo)?'<button class="mini" data-prog-open="'+html(id)+'" '+(live()?'disabled':'')+'>更新进度/改期</button>':'')+
      '<button class="mini" data-close="'+html(id)+'" data-how="'+(isExp?'实验完成':'完成')+'" '+(live()?'disabled':'')+'>完成</button>'+
      '</div>';
    if(isEditing){
      const nextDef=it.follow_up_at||it.due||'';
      row+='<div class="lb-prog-editor lb-wide">'+
        '<label>进度 %<input id="lbPEPct" type="number" min="0" max="100" value="'+pct+'"></label>'+
        '<label>状态<select id="lbPEStatus">'+
          (isExp ? [['planned','计划中'],['running','进行中'],['blocked','受阻'],['done','已完成'],['dropped','放弃']]
                 : [['open','待办中'],['closed','已完成']])
          .map(o=>'<option value="'+o[0]+'"'+(o[0]===it.status?' selected':'')+'>'+o[1]+'</option>').join('')+
        '</select></label>'+
        '<label>下次跟进日<input id="lbPEDue" type="date" value="'+html(nextDef)+'"></label>'+
        '<label class="lb-wide">进展备注 / 遇到的问题<input id="lbPENote" maxlength="600" placeholder="如 已完成固定与染色，周三上机拍照"></label>'+
        '<div class="lb-actions lb-wide"><button class="mini pri" id="lbPESave">保存进度</button><button class="mini" id="lbPECancel">取消</button></div>'+
        '</div>';
    }
    return row+'</li>';
  }

  /* ── 规划：进度概览 + 接收实验计划 + 排期追踪 + 下一步 + 待办 + 批次 ── */
  function planBody(){
    const d=state.data, st=d.state||{};
    let h=summary();
    h+=planImportBox();
    h+=scheduleBoard();

    h+='<div class="lb-section"><h3>下一步（机械排序）<span class="lb-sub">每条注明来源，结合到点批次、逾期跟进与数据缺口</span></h3>';
    const acts=d.actions||[];
    h+= acts.length ? '<ol class="lb-acts">'+acts.map(a=>'<li><b>'+html(a.what)+'</b><p>'+html(a.why)+'</p>'+
      '<span class="lb-src">来源：'+html(a.source)+(a.due?' · 截止 '+html(a.due):'')+'</span></li>').join('')+'</ol>'
      : '<div class="lb-empty">台账里还没有可排的行动。导入实验计划、或记菌株/批次/待办。</div>';
    h+='</div>';

    // 待办
    const todos=Object.values(st.todos||{}).filter(t=>t.status!=='closed');
    h+='<div class="lb-section"><h3>待办</h3>';
    h+= todos.length ? '<ul class="lb-list">'+todos.map(t=>'<li><div><b>'+html(t.what||t.id)+'</b>'+
      '<small>'+html(t.id)+(t.due?' · 截止 '+html(t.due):'')+'</small></div>'+
      '<button class="mini" data-close="'+html(t.id)+'" data-how="待办完成" '+(live()?'disabled':'')+'>完成</button></li>').join('')+'</ul>'
      : '<div class="lb-empty">没有在办待办。</div>';
    h+= todoForm(st);
    h+='</div>';

    // 批次
    const batches=Object.values(st.batches||{});
    h+='<div class="lb-section"><h3>发酵批次</h3>';
    h+= batches.length ? '<ul class="lb-list">'+batches.map(b=>'<li><div><b>'+html(b.id)+' · '+html(b.code||'—')+'</b>'+
      '<small>起始 '+html(b.started||'—')+(b.expect_days?' · 预计 '+html(b.expect_days)+' 天':'')+
      ' · '+html(b.status==='done'?'已完成':'进行中')+'</small></div>'+
      (b.status!=='done'?'<button class="mini" data-close="'+html(b.id)+'" data-how="批次完成" '+(live()?'disabled':'')+'>标记完成</button>':'')+
      '</li>').join('')+'</ul>' : '<div class="lb-empty">还没有批次。</div>';
    h+= batchForm(st);
    h+='</div>';
    return h;
  }

  function nextId(map, prefix){
    let n=1; while(map[prefix+String(n).padStart(2,'0')]) n++;
    return prefix+String(n).padStart(2,'0');
  }
  function todoForm(st){
    const id=nextId(st.todos||{},'T');
    return '<div class="lb-form lb-inline"><label>事项<input id="lbTodoWhat" maxlength="600" placeholder="如 摸染色条件"></label>'+
      '<label>截止<input id="lbTodoDue" type="date"></label>'+
      '<label>编号<input id="lbTodoId" value="'+id+'" maxlength="24"></label>'+
      '<button class="mini pri" id="lbTodoAdd" '+(live()?'disabled':'')+'>添加待办</button></div>';
  }
  function batchForm(st){
    const id=nextId(st.batches||{},'B');
    const codes=Object.keys(st.strains||{});
    const today=new Date().toISOString().slice(0,10);
    return '<div class="lb-form lb-inline"><label>菌株<input id="lbBatchCode" list="lbCodes" placeholder="如 S01"></label>'+
      '<label>起始<input id="lbBatchStart" type="date" value="'+today+'"></label>'+
      '<label>预计天数<input id="lbBatchDays" type="number" min="0" placeholder="可空"></label>'+
      '<label>编号<input id="lbBatchId" value="'+id+'" maxlength="24"></label>'+
      '<button class="mini pri" id="lbBatchAdd" '+(live()?'disabled':'')+'>登记批次</button>'+
      '<datalist id="lbCodes">'+codes.map(c=>'<option value="'+html(c)+'">').join('')+'</datalist></div>';
  }

  /* ── 成果：录入表单 + 进度日志 + 事件流（含作废）── */
  function resultsBody(){
    const d=state.data;
    let h='<div class="lb-section"><h3>记一条实验事实或进度<span class="lb-sub">定量与定性分开记；实验与进度按课题追加存档</span></h3>';
    h+= recordForm(d);
    h+='</div>';
    const logs=(d.progress&&d.progress.recent_logs)||[];
    if(logs.length){
      h+='<div class="lb-section"><h3>最近进度更新</h3><ul class="lb-list">'+logs.slice(0,8).map(l=>
        '<li><div><b>'+html(l.id)+(l.progress!=null?' · '+html(l.progress)+'%':'')+(l.status?' · '+html(EXP_STATUS_CN[l.status]||l.status):'')+'</b>'+
        '<small>'+html(l.at)+(l.next_follow_up?' · 下次跟进 '+html(l.next_follow_up):'')+(l.note?' · '+html(l.note):'')+'</small></div></li>'
      ).join('')+'</ul></div>';
    }
    h+='<div class="lb-section"><h3>台账事件流<span class="lb-sub">追加式，不改写历史；记错了用「作废」</span></h3>';
    h+= timeline(d);
    h+='</div>';
    return h;
  }

  function recordForm(d){
    const kinds=[['measure','定量测量'],['observe','定性观察'],['experiment','新增实验步骤'],['progress','记录进度'],['strain','建菌株'],['metric','自定义指标']];
    const tabs=kinds.map(k=>'<button class="lb-kind" data-kind="'+k[0]+'" aria-pressed="'+(state.formKind===k[0])+'">'+k[1]+'</button>').join('');
    const st=d.state||{}, codes=Object.keys(st.strains||{});
    const dl='<datalist id="lbCodes2">'+codes.map(c=>'<option value="'+html(c)+'">').join('')+'</datalist>';
    const metrics=d.metrics||{};
    const trackIds=Object.keys(st.experiments||{}).concat(Object.keys(st.todos||{})).concat(Object.keys(st.batches||{}));
    let f='<div class="lb-form">';
    if(state.formKind==='measure'){
      const mopts=Object.values(metrics).map(m=>'<option value="'+html(m.key)+'">'+html(m.label||m.key)+(m.unit?'（'+html(m.unit)+'）':'')+'</option>').join('');
      f+='<label>菌株<input id="lbCode" list="lbCodes2" placeholder="如 S01"></label>'+
        '<label>指标<select id="lbMetric">'+mopts+'</select></label>'+
        '<label>数值<input id="lbValue" type="number" step="any" placeholder="必须是数字"></label>'+
        '<label>重复 n<input id="lbN" type="number" min="1" placeholder="如 3"></label>'+
        '<label>标准差 sd<input id="lbSd" type="number" step="any" placeholder="可空"></label>'+
        '<label class="lb-wide">方法/备注<input id="lbNote" maxlength="600" placeholder="可空"></label>';
    }else if(state.formKind==='observe'){
      f+='<label>菌株<input id="lbObsCode" list="lbCodes2" placeholder="如 S01"></label>'+
        '<label>生长<select id="lbGrowth">'+GROWTH.map(g=>'<option value="'+g[0]+'">'+g[1]+'</option>').join('')+'</select></label>'+
        '<label>温度 ℃<input id="lbTemp" type="number" step="any" placeholder="如 23"></label>'+
        '<label class="lb-wide">观察<textarea id="lbObsNote" rows="2" maxlength="600" placeholder="如 菌落白色、气生菌丝稀疏"></textarea></label>';
    }else if(state.formKind==='experiment'){
      const today=new Date().toISOString().slice(0,10);
      f+='<label>实验名称<input id="lbExpTitle" maxlength="200" placeholder="如 验证酶解时间梯度"></label>'+
        '<label>所属阶段<input id="lbExpPhase" maxlength="80" placeholder="如 阶段一（可空）"></label>'+
        '<label>起始日<input id="lbExpStart" type="date" value="'+today+'"></label>'+
        '<label>截止/跟进日<input id="lbExpDue" type="date"></label>'+
        '<label>优先级<select id="lbExpPrio"><option value="normal">普通</option><option value="high">关键优先</option><option value="low">低</option></select></label>'+
        '<label class="lb-wide">操作要点 / 预期结果<input id="lbExpProto" maxlength="600" placeholder="可空"></label>';
    }else if(state.formKind==='progress'){
      const opts=trackIds.map(i=>'<option value="'+html(i)+'">'+html(i)+'</option>').join('');
      f+='<label>对象编号<select id="lbProgId">'+(opts||'<option value="">（先建实验或待办）</option>')+'</select></label>'+
        '<label>进度 %<input id="lbProgPct" type="number" min="0" max="100" value="50"></label>'+
        '<label>状态<select id="lbProgStatus"><option value="running">进行中</option><option value="blocked">受阻</option><option value="done">已完成</option><option value="planned">计划中</option></select></label>'+
        '<label>下次跟进日<input id="lbProgFollow" type="date"></label>'+
        '<label class="lb-wide">进展说明<input id="lbProgNote" maxlength="600" placeholder="写了什么、结果如何、遇到什么卡点"></label>';
    }else if(state.formKind==='strain'){
      f+='<label>菌株编号<input id="lbStrCode" placeholder="如 S01（真实株号）"></label>'+
        '<label>物种<input id="lbSpecies" maxlength="120" placeholder="如 Ophiocordyceps sinensis"></label>'+
        '<label>角色<select id="lbRole">'+ROLE.map(r=>'<option value="'+r[0]+'">'+r[1]+'</option>').join('')+'</select></label>'+
        '<label class="lb-check"><input id="lbPlaceholder" type="checkbox"> 这是占位编号（还没拿到真实株号）</label>';
    }else{
      f+='<label>指标 key<input id="lbKey" placeholder="如 astaxanthin（小写英文）"></label>'+
        '<label>名称<input id="lbLabel" maxlength="120" placeholder="如 虾青素含量"></label>'+
        '<label>单位<input id="lbUnit" placeholder="如 mg/g（必填）"></label>'+
        '<label>最少重复 n<input id="lbNmin" type="number" min="1" value="3"></label>'+
        '<label class="lb-wide">方法<input id="lbMetNote" maxlength="600" placeholder="可空"></label>';
    }
    f+='<div class="lb-actions lb-wide"><button class="mini pri" id="lbRecord" '+(live()?'disabled':'')+'>记录</button>'+
      '<span class="lb-note">写完会读回核验；所有事实按课题保存在 topics/&lt;slug&gt;/LAB.jsonl。</span></div></div>'+dl;
    return '<div class="lb-kinds">'+tabs+'</div>'+f;
  }

  function evSummary(e){
    const k=e.kind;
    if(k==='measure') return html(e.code)+' · '+html(e.metric)+' = '+html(e.value)+(e.unit?' '+html(e.unit):'')+(e.n?' (n='+html(e.n)+')':'');
    if(k==='observe') return html(e.code)+' · '+html((GROWTH.find(g=>g[0]===e.growth)||[])[1]||e.growth||'')+(e.temp_c!=null&&e.temp_c!==''?' · '+html(e.temp_c)+'℃':'')+(e.text?' · '+html(e.text):'');
    if(k==='strain') return html(e.code)+(e.species?' · '+html(e.species):'')+' · '+html((ROLE.find(r=>r[0]===e.role)||[])[1]||e.role||'候选')+(e.placeholder?'（占位）':'');
    if(k==='batch') return html(e.id)+' · '+html(e.code||'')+(e.started?' · 起始 '+html(e.started):'');
    if(k==='metric') return html(e.key)+' · '+html(e.label||'')+'（'+html(e.unit)+'）';
    if(k==='todo') return html(e.id)+' · '+html(e.what||'')+(e.due?' · 截止 '+html(e.due):'');
    if(k==='plan') return html(e.id)+' · '+html(e.title||'')+(e.step_count?'（'+html(e.step_count)+' 步）':'');
    if(k==='experiment') return html(e.id)+' · '+html(e.title||'')+(e.due?' · 截止 '+html(e.due):'');
    if(k==='progress') return html(e.id)+(e.progress!=null?' → '+html(e.progress)+'%':'')+(e.status?' ['+html(EXP_STATUS_CN[e.status]||e.status)+']':'')+(e.note?' · '+html(e.note):'');
    if(k==='close') return html(e.id)+(e.how?' · '+html(e.how):'');
    if(k==='update') return html(e.id)+' · 更新字段';
    if(k==='retract') return '作废 #'+html(e.target_seq)+(e.reason?' · '+html(e.reason):'');
    return html(e.code||e.id||'');
  }

  function timeline(d){
    const tl=(d.timeline||[]).slice().reverse();
    if(!tl.length) return '<div class="lb-empty">台账还是空的。上面记一条，或在对话里用 lab_plan / lab_record。</div>';
    return '<ul class="lb-timeline">'+tl.map(e=>{
      const dead=e.retracted;
      const canRetract=!dead && e.kind!=='retract';
      let row='<li class="lb-ev'+(dead?' lb-ev-dead':'')+'" data-seq="'+html(e.seq)+'">'+
        '<span class="lb-evseq">#'+html(e.seq)+'</span>'+
        '<span class="lb-evkind" data-k="'+html(e.kind)+'">'+html(KIND_CN[e.kind]||e.kind)+'</span>'+
        '<span class="lb-evbody">'+evSummary(e)+(dead?'<span class="lb-evreason">已作废'+(e.retract_reason?'：'+html(e.retract_reason):'')+'</span>':'')+
        '<small>'+html(e.at||'')+'</small></span>';
      if(state.retractSeq===e.seq){
        row+='<span class="lb-retract"><input id="lbRetractReason" maxlength="600" placeholder="为什么要作废？（必填）">'+
          '<button class="mini pri" id="lbRetractGo">确认作废</button>'+
          '<button class="mini" id="lbRetractCancel">取消</button></span>';
      }else if(canRetract){
        row+='<button class="mini lb-evbtn" data-retract="'+html(e.seq)+'" '+(live()?'disabled':'')+'>作废</button>';
      }
      return row+'</li>';
    }).join('')+'</ul>';
  }

  /* ── 分析：矩阵 + 缺口 + 摘要 + 组会一页 ── */
  function analysisBody(){
    const d=state.data, mx=d.matrix||{cols:[],rows:[]};
    let h=summary();
    h+='<div class="lb-section"><h3>菌株 × 指标<span class="lb-sub">空格 = 还没测，不是「没这项」</span></h3>';
    if(!mx.rows.length){ h+='<div class="lb-empty">还没有菌株记录。</div>'; }
    else{
      h+='<div class="lb-tablewrap"><table class="lb-table"><thead><tr><th>菌株</th><th>物种/角色</th><th>温度</th><th>生长</th>'+
        mx.cols.map(c=>'<th>'+esc(c.label)+(c.unit?'<small>（'+esc(c.unit)+'）</small>':'')+'</th>').join('')+'</tr></thead><tbody>';
      h+=mx.rows.map(r=>{
        const role=(ROLE.find(x=>x[0]===r.role)||[])[1]||r.role||'—';
        const cells=mx.cols.map(c=>{
          const v=r.cells[c.key];
          if(!v) return '<td class="lb-emptycell">—</td>';
          let s=esc(v.value)+(v.sd!=null&&v.sd!==''?' ± '+esc(v.sd):'');
          if(v.n) s+='<small> (n='+esc(v.n)+')</small>';
          return '<td>'+s+'</td>';
        }).join('');
        return '<tr><td><b>'+esc(r.code)+'</b>'+(r.placeholder?'<span class="lb-ph" title="占位编号">*</span>':'')+'</td>'+
          '<td>'+esc(r.species||'—')+'/'+esc(role)+'</td>'+
          '<td>'+(r.temp_c!=null&&r.temp_c!==''?esc(r.temp_c)+'℃':'—')+'</td>'+
          '<td>'+esc(r.growth||'—')+'</td>'+cells+'</tr>';
      }).join('');
      h+='</tbody></table></div>';
      if(mx.rows.some(r=>r.placeholder)) h+='<p class="lb-note">* = 占位编号：还没拿到真实株号。拿到后在对话里用 lab_record(kind="update") 改名，历史观察会自动跟过去。</p>';
    }
    h+='</div>';

    const gaps=d.gaps||[];
    h+='<div class="lb-section"><h3>缺口<span class="lb-sub">「没数据」的具体位置</span></h3>';
    h+= gaps.length ? '<ul class="lb-gaps">'+gaps.map(g=>'<li><span class="lb-gapkind" data-g="'+esc(g.kind)+'">'+esc(GAP_CN[g.kind]||g.kind)+'</span>'+
      '<b>'+esc(g.code)+' · '+esc(g.label)+'</b><p>'+esc(g.why)+'</p></li>').join('')+'</ul>'
      : '<div class="lb-empty">没有缺口。</div>';
    h+='</div>';

    if(d.digest) h+='<div class="lb-section"><h3>处境摘要</h3><pre class="lb-digest">'+esc(d.digest)+'</pre></div>';

    h+='<div class="lb-section"><h3>组会一页<span class="lb-sub">矩阵 + 缺口 + 实验进度 + 下一步，可直接送导师诊断</span></h3>';
    const rp=state.report;
    if(!rp) h+='<div class="lb-empty">正在生成…</div>';
    else if(rp.error) h+=errBox(rp.error);
    else{
      h+='<div class="lb-actions"><button class="mini pri" id="lbAskAdvisor">请导师诊断此报告</button>'+
        '<button class="mini" id="lbCopyMd">复制 Markdown</button>'+
        '<button class="mini" id="lbDlMd">下载 LAB.md</button>'+
        '<button class="mini" id="lbDlCsv">下载 LAB.csv</button></div>'+
        '<pre class="lb-report">'+esc(rp.md)+'</pre>';
    }
    h+='</div>';

    h+='<div class="lb-section"><h3>方法与专利台账导出（METHODS.md）<span class="lb-sub">与方法版本、参数出处及专利线索单源同步</span></h3>';
    const mdData=state.methodsData;
    if(!mdData) h+='<div class="lb-empty">正在读取 METHODS.md…</div>';
    else if(mdData.error) h+=errBox(mdData.error);
    else{
      const mmd=text(mdData.md);
      h+='<div class="lb-actions">'+
        '<button class="mini" id="lbCopyMethodsMd"'+(mmd?'':' disabled')+'>复制 METHODS.md</button>'+
        '<button class="mini" id="lbDlMethodsMd"'+(mmd?'':' disabled')+'>下载 METHODS.md</button></div>'+
        (mmd?'<pre class="lb-report" id="lbMethodsMdView">'+esc(mmd)+'</pre>':'<div class="lb-empty">当前课题还没有 METHODS.md 内容。</div>');
    }
    h+='</div>';
    return h;
  }

  /* ── 方法与专利：参数出处 + 版本迭代 + 实操日志 + 专利线索 ── */
  function srcCategory(src){
    const s=text(src).trim();
    if(s.startsWith('kb:')) return 'kb';
    if(s==='design') return 'design';
    if(s.startsWith('std:')) return 'std';
    return 'user';
  }

  function parseParamsText(raw){
    const lines=text(raw).split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
    const out=[];
    for(let i=0;i<lines.length;i++){
      const parts=lines[i].split(/\s*[|｜\t]\s*/);
      if(parts.length<2){
        throw new Error('第 '+(i+1)+' 行参数格式应为「参数名 | 数值(含单位) | 出处(可省，默认 user)」：'+lines[i]);
      }
      const name=parts[0].trim();
      const value=parts[1].trim();
      const src=(parts[2]||'').trim()||'user';
      if(!name||!value){
        throw new Error('第 '+(i+1)+' 行参数的名称或数值不能为空：'+lines[i]);
      }
      out.push({name, value, src});
    }
    return out;
  }

  function splitLines(raw){
    return text(raw).split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
  }

  function buildMethodAdvisorPrompt(m){
    const pv=m.patent_view||{};
    const latest=m.latest||{};
    const params=(latest.params||[]).map(p=>'- '+p.name+'：'+p.value+' ['+p.src+']').join('\n')||'（未填参数）';
    const gaps=(pv.gaps||[]).map(g=>'- '+g).join('\n')||'（无显式缺口）';
    const claims=(pv.claims_basis||[]).map(c=>'- '+c).join('\n');
    return '请以导师视角评估当前课题「'+state.slug+'」中的实验方法「'+m.title+'（'+m.id+'）」与专利线索：\n\n'+
      '1. 方法来源：'+m.origin+' · 当前共 '+(m.versions||[]).length+' 个版本（最新 v'+(latest.version||1)+'）\n'+
      (latest.what_changed?'2. 版本/文献改动：'+latest.what_changed+'\n':'')+
      '3. 最新版本参数：\n'+params+'\n'+
      '4. 专利线索现状：'+(pv.verdict_cn||pv.verdict||'未评估')+'（自有参数 '+(pv.own_params||[]).length+' 条 / 文献参数 '+(pv.kb_params||[]).length+' 条）\n'+
      (pv.first_public?'5. 首次公开日期：'+pv.first_public+'\n':'5. 首次公开日期：尚未登记（公开前需评估新颖性）\n')+
      (claims?'6. 主张要点：\n'+claims+'\n':'')+
      '7. 待补缺口：\n'+gaps+'\n\n'+
      '请重点帮我分析：(1) 该方法的参数出处与实验步骤是否足以复现、有无遗漏对照或边界条件；(2) 专利线索中哪些自有参数具有差异化价值，还需要补哪些定量数值范围或对照数据才能拿去咨询专利代理人。';
  }

  function syncMethodDraft(){
    const d=state.methodDraft;
    const val=(sel, cur)=>{ const el=q(sel); return el?el.value:cur; };
    const k=state.methodFormKind;
    if(k==='method'){
      d.id=val('#lbMetId', d.id);
      d.title=val('#lbMetTitle', d.title);
      d.origin=val('#lbMetOrigin', d.origin);
      d.differs_from=val('#lbMetDiffers', d.differs_from);
    }else if(k==='version'){
      d.id=val('#lbMetVerId', d.id);
      d.version=val('#lbMetVerNum', d.version);
    }else if(k==='observe'){
      d.id=val('#lbMetObsId', d.id);
      d.obsVer=val('#lbMetObsVer', d.obsVer);
      d.obsStatus=val('#lbMetObsStatus', d.obsStatus);
      d.obsText=val('#lbMetObsText', d.obsText);
    }else if(k==='patent'){
      d.id=val('#lbMetPatId', d.id);
      d.disclosed_at=val('#lbMetPatDisclosed', d.disclosed_at);
      d.first_public=val('#lbMetPatPublic', d.first_public);
      d.claimsText=val('#lbMetPatClaims', d.claimsText);
    }
    if(k==='method'||k==='version'){
      d.what_changed=val('#lbMetChanged', d.what_changed);
      d.paramsText=val('#lbMetParams', d.paramsText);
      d.stepsText=val('#lbMetSteps', d.stepsText);
      d.pitfallsText=val('#lbMetPitfalls', d.pitfallsText);
      d.instrument=val('#lbMetInst', d.instrument);
      d.consumable=val('#lbMetCons', d.consumable);
      d.yield=val('#lbMetYield', d.yield);
      d.note=val('#lbMetNoteFld', d.note);
    }
  }

  function methodForm(methods){
    const d=state.methodDraft;
    const kinds=[['method','新建方法'],['version','迭代新版本'],['observe','记一次实操结果'],['patent','登记专利线索']];
    const tabs=kinds.map(k=>'<button type="button" class="lb-kind" data-mkind="'+k[0]+'" aria-pressed="'+(state.methodFormKind===k[0])+'">'+k[1]+'</button>').join('');
    const k=state.methodFormKind;
    const mOpts=methods.map(m=>'<option value="'+esc(m.id)+'"'+(m.id===d.id?' selected':'')+'>'+esc(m.id)+' · '+esc(m.title)+'</option>').join('');
    const emptyOpt='<option value="">（请先新建一个方法）</option>';
    let f='<div class="lb-form">';
    if(k==='method'){
      f+='<label>方法英文 ID<input id="lbMetId" maxlength="48" value="'+esc(d.id)+'" placeholder="字母/数字/下划线/短横，如 peg-fusion"></label>'+
        '<label>方法名称<input id="lbMetTitle" maxlength="120" value="'+esc(d.title)+'" placeholder="如 两步变温溶壁酶原生质体制备"></label>'+
        '<label>方法来源 (origin)<select id="lbMetOrigin">'+ORIGIN_LIST.map(o=>'<option value="'+o[0]+'"'+(d.origin===o[0]?' selected':'')+'>'+o[1]+'</option>').join('')+'</select></label>'+
        '<label>区别于（可空，逗号分隔）<input id="lbMetDiffers" value="'+esc(d.differs_from)+'" placeholder="如 kb:doc101"></label>';
    }else if(k==='version'){
      f+='<label>选择方法<select id="lbMetVerId">'+(mOpts||emptyOpt)+'</select></label>'+
        '<label>新版本号 (整数)<input id="lbMetVerNum" type="number" min="1" value="'+esc(d.version||'2')+'"></label>';
    }else if(k==='observe'){
      f+='<label>选择方法<select id="lbMetObsId">'+(mOpts||emptyOpt)+'</select></label>'+
        '<label>对应版本（可空）<input id="lbMetObsVer" maxlength="16" value="'+esc(d.obsVer)+'" placeholder="如 1 或 2"></label>'+
        '<label>实操结果<select id="lbMetObsStatus">'+
          [['worked','✔ 成功跑通 (worked)'],['failed','✘ 失败/翻车 (failed)'],['note','· 一般记录']].map(o=>'<option value="'+o[0]+'"'+(d.obsStatus===o[0]?' selected':'')+'>'+o[1]+'</option>').join('')+
        '</select></label>'+
        '<label class="lb-wide">过程与现象记录<textarea id="lbMetObsText" rows="2" maxlength="3000" placeholder="这次按该版本做了什么、结果如何、哪里出现异常">'+esc(d.obsText)+'</textarea></label>';
    }else if(k==='patent'){
      f+='<label>选择方法<select id="lbMetPatId">'+(mOpts||emptyOpt)+'</select></label>'+
        '<label>内部梳理/交底日期<input id="lbMetPatDisclosed" type="date" value="'+esc(d.disclosed_at)+'"></label>'+
        '<label>首次公开日期（未公开请留空）<input id="lbMetPatPublic" type="date" value="'+esc(d.first_public)+'"></label>'+
        '<label class="lb-wide">可主张技术特征 / 专利要点（每行一条）<textarea id="lbMetPatClaims" rows="3" placeholder="每行一条具体技术特征与参数范围，例如：\n两段变温酶解（26.1 ℃ 30 min 后转 18 ℃ 60 min）提高原生质体再生率">'+esc(d.claimsText)+'</textarea></label>';
    }
    if(k==='method'||k==='version'){
      f+='<label class="lb-wide">改动/差异说明 (what_changed)<input id="lbMetChanged" maxlength="3000" value="'+esc(d.what_changed)+'" placeholder="写清改了哪个参数、从多少改到多少（填了 differs_from 或迭代新版本时建议必填）"></label>'+
        '<div class="lb-wide"><div class="lb-fieldlabel">参数表构建器 <small>（每行一条：参数名 | 数值(含单位) | 出处；省略出处自动补 user）</small></div>'+
        '<div class="lb-pbuilder">'+
          '<input id="lbMetPName" placeholder="参数名（如 溶壁酶浓度）">'+
          '<input id="lbMetPVal" placeholder="数值含单位（如 2.06%）">'+
          '<input id="lbMetPSrc" list="lbSrcPresets" value="user" placeholder="出处：user / design / kb:... / std:...">'+
          '<datalist id="lbSrcPresets"><option value="user"><option value="design"><option value="kb:"><option value="std:"></datalist>'+
          '<button type="button" class="mini" id="lbMetAddParam">＋ 加入参数表</button>'+
        '</div>'+
        '<textarea id="lbMetParams" rows="3" placeholder="参数名 | 数值(含单位) | 出处(user/design/std:.../kb:...)\n例如：\n溶壁酶浓度 | 2.06% | user\n缓冲液 | 0.6M KCl | kb:doc101 p.4">'+esc(d.paramsText)+'</textarea></div>'+
        '<label class="lb-wide">操作步骤（每行一步，建议带量与时长）<textarea id="lbMetSteps" rows="3" placeholder="1. 取对数期菌丝 1.0 g 湿重\n2. 加入 2.06% 溶壁酶 26.1 ℃ 酶解 2 h">'+esc(d.stepsText)+'</textarea></label>'+
        '<label class="lb-wide">避坑要点（每行一条：会怎么失败 / 如何识别）<textarea id="lbMetPitfalls" rows="2" placeholder="酶解超过 2.5 h 后菌丝虽碎但再生率显著下降">'+esc(d.pitfallsText)+'</textarea></label>'+
        '<details class="lb-wide lb-details"><summary>可选扩展信息（仪器 / 耗材 / 得率 / 备注）</summary>'+
          '<div class="lb-form" style="margin-top:8px">'+
            '<label>关键仪器<input id="lbMetInst" maxlength="300" value="'+esc(d.instrument)+'" placeholder="如 恒温摇床"></label>'+
            '<label>关键耗材/试剂<input id="lbMetCons" maxlength="300" value="'+esc(d.consumable)+'" placeholder="如 Sigma L1412 溶壁酶"></label>'+
            '<label>实测得率<input id="lbMetYield" maxlength="300" value="'+esc(d.yield)+'" placeholder="如 2.4×10^7 /mL"></label>'+
            '<label>补充备注<input id="lbMetNoteFld" maxlength="600" value="'+esc(d.note)+'" placeholder="其他注意事项"></label>'+
          '</div></details>';
    }
    const needExisting = k!=='method' && !methods.length;
    f+='<div class="lb-actions lb-wide"><button type="button" class="mini pri" id="lbMetSubmit" '+(live()||needExisting?'disabled':'')+'>'+
      (k==='method'?'创建方法 (v1)':k==='version'?'提交新版本':k==='observe'?'记录实操结果':'保存专利线索')+
      '</button><span class="lb-note">追加写入 topics/&lt;slug&gt;/METHODS.jsonl，并同步渲染 METHODS.md。</span></div></div>';
    return '<div class="lb-kinds">'+tabs+'</div>'+f;
  }

  function methodCard(m, originsMap){
    const pv=m.patent_view||{};
    const versions=m.versions||[];
    const latestVer=versions.length?versions[versions.length-1].version:1;
    const originText=(originsMap&&originsMap[m.origin])||m.origin||'—';
    const ownN=(pv.own_params||[]).length;
    const kbN=(pv.kb_params||[]).length;
    const softN=(pv.soft_params||[]).length;
    const vTone=VERDICT_TONE[pv.verdict]||'neutral';

    let patHtml='<div class="lb-patcard" data-verdict="'+esc(pv.verdict||'weak')+'">'+
      '<div class="lb-pathead">'+
        '<div class="lb-pathead-l"><span class="lb-tag" data-tone="'+vTone+'">'+esc(pv.verdict_cn||pv.verdict||'未评估')+'</span>'+
        '<span class="lb-patcnt">自有参数 <b>'+ownN+'</b> 条 · 文献参数 <b>'+kbN+'</b> 条'+(softN?' · 定性未定量 <b>'+softN+'</b> 条':'')+'</span></div>'+
        '<button type="button" class="mini pri" data-m-ask="'+esc(m.id)+'">请导师评估此方法与专利线索</button>'+
      '</div>';
    if(pv.first_public){
      patHtml+='<div class="lb-patwarn" data-tone="err">⚠ 首次公开日期：<b>'+esc(pv.first_public)+'</b> —— 已公开可能影响新颖性，问专利代理人时务必最先说明！</div>';
    }else{
      patHtml+='<div class="lb-patwarn" data-tone="warn">未填写首次公开日期（first_public）—— 发表论文或会议交流前请先评估并补齐。</div>';
    }
    if(pv.disclosed_at){
      patHtml+='<div class="lb-note">内部梳理/交底日期：'+esc(pv.disclosed_at)+'</div>';
    }
    if(pv.claims_basis && pv.claims_basis.length){
      patHtml+='<div class="lb-patclaims"><b>主张要点：</b><ul>'+pv.claims_basis.map(c=>'<li>'+esc(c)+'</li>').join('')+'</ul></div>';
    }
    if(pv.gaps && pv.gaps.length){
      patHtml+='<ul class="lb-patgaps">'+pv.gaps.map(g=>'<li>'+fmtRich(g)+'</li>').join('')+'</ul>';
    }
    if(pv.note){
      patHtml+='<div class="lb-note">'+fmtRich(pv.note)+'</div>';
    }
    patHtml+='</div>';

    const versHtml=versions.map((ver, idx)=>{
      const isLatest=idx===versions.length-1;
      const ps=ver.params||[];
      const extras=[];
      if(ver.instrument) extras.push('仪器：'+ver.instrument);
      if(ver.consumable) extras.push('耗材：'+ver.consumable);
      if(ver.yield) extras.push('得率：'+ver.yield);
      if(ver.note) extras.push('备注：'+ver.note);
      let vh='<div class="lb-ver">'+
        '<div class="lb-verhead"><b>v'+esc(ver.version)+'</b>'+
        (isLatest?'<span class="lb-tag" data-tone="info">最新</span>':'')+
        '<small>'+esc(ver.at||'')+'</small></div>';
      if(ver.what_changed){
        vh+='<div class="lb-vchange"><b>改动/差异：</b>'+esc(ver.what_changed)+'</div>';
      }
      if(extras.length){
        vh+='<div class="lb-vmeta">'+esc(extras.join(' · '))+'</div>';
      }
      if(ps.length){
        vh+='<div class="lb-tablewrap"><table class="lb-table lb-ptable"><thead><tr><th>参数</th><th>数值</th><th>出处</th></tr></thead><tbody>'+
          ps.map(p=>'<tr><td><b>'+esc(p.name)+'</b></td><td>'+esc(p.value)+'</td><td><span class="lb-srcbadge" data-src="'+esc(srcCategory(p.src))+'">'+esc(p.src)+'</span></td></tr>').join('')+
          '</tbody></table></div>';
      }else{
        vh+='<div class="lb-note">该版本未记录参数。</div>';
      }
      if(ver.steps && ver.steps.length){
        vh+='<div class="lb-vsub"><b>步骤</b><ol class="lb-steplist">'+ver.steps.map(s=>'<li>'+esc(s)+'</li>').join('')+'</ol></div>';
      }
      if(ver.pitfalls && ver.pitfalls.length){
        vh+='<div class="lb-vsub"><b>避坑要点</b><ul class="lb-pitlist">'+ver.pitfalls.map(p=>'<li>'+esc(p)+'</li>').join('')+'</ul></div>';
      }
      return vh+'</div>';
    }).join('');

    const obs=m.observes||[];
    const obsHtml=obs.length
      ? '<div class="lb-mobs"><b>实操记录（'+obs.length+'）</b><ul class="lb-obslist">'+obs.map(o=>{
          const st=o.worked?'worked':(o.failed?'failed':'note');
          const mark=o.worked?'✔':(o.failed?'✘':'·');
          return '<li><span class="lb-obsmark" data-st="'+st+'">'+mark+'</span>'+
            (o.version?'<span class="lb-code">v'+esc(o.version)+'</span> ':'')+
            '<span>'+esc(o.text)+'</span> <small>'+esc(o.at||'')+'</small></li>';
        }).join('')+'</ul></div>'
      : '';

    return '<article class="lb-mcard" data-mid="'+esc(m.id)+'">'+
      '<div class="lb-mhead">'+
        '<div class="lb-mtitle"><code class="lb-code">'+esc(m.id)+'</code> <b>'+esc(m.title)+'</b> '+
        '<span class="lb-tag" data-tone="info">'+esc(m.origin)+'</span> '+
        '<span class="lb-tag" data-tone="neutral">v1..v'+esc(latestVer)+'</span></div>'+
        '<div class="lb-actions">'+
          '<button type="button" class="mini" data-m-ver="'+esc(m.id)+'">迭代 v'+(Number(latestVer)+1)+'</button>'+
          '<button type="button" class="mini" data-m-obs="'+esc(m.id)+'">记实操</button>'+
          '<button type="button" class="mini" data-m-pat="'+esc(m.id)+'">专利线索</button>'+
          (m.seq ? '<button type="button" class="mini" data-m-retract="'+esc(m.seq)+'" '+(live()?'disabled':'')+'>作废</button>' : '')+
        '</div>'+
      '</div>'+
      (m.seq && state.retractMethodSeq===m.seq
        ? '<div class="lb-retract" style="margin:6px 0"><input id="lbMRetractWhy" maxlength="600" placeholder="为什么要作废该方法？（必填）">'+
          '<button type="button" class="mini pri" id="lbMRetractGo">确认作废</button>'+
          '<button type="button" class="mini" id="lbMRetractCancel">取消</button></div>'
        : '')+
      '<div class="lb-note" style="margin-top:4px">来源：'+esc(originText)+
      (m.differs_from && m.differs_from.length ? ' · 区别于：'+m.differs_from.map(x=>'<code class="lb-code">'+esc(x)+'</code>').join(' ') : '')+
      '</div>'+
      patHtml+
      '<div class="lb-vlist">'+versHtml+'</div>'+
      obsHtml+
    '</article>';
  }

  function methodsBody(){
    const md=state.methodsData;
    if(!md) return '<div class="lb-empty">正在读取方法与专利台账…</div>';
    if(md.error) return errBox(md.error);
    const methods=md.methods||[];
    const originsMap=md.origins||{};
    let h='<div class="lb-section"><h3>录入与迭代方法 / 专利线索<span class="lb-sub">参数必须带出处（kb: / user / design / std:）；版本号不可复用</span></h3>';
    h+=methodForm(methods);
    h+='</div>';

    h+='<div class="lb-section"><h3>已登记方法与专利线索（'+methods.length+'）<span class="lb-sub">按版本保留完整轨迹；点击按钮可送导师评估</span></h3>';
    h+=methods.length
      ? '<div class="lb-mgrid">'+methods.map(m=>methodCard(m, originsMap)).join('')+'</div>'
      : '<div class="lb-empty">还没有登记实验方法。在上方表单新建第一个方法。</div>';
    h+='</div>';
    return h;
  }

  /* ── 板内质控：96 孔板读数解析与 Z' / SSMD / 空间诊断 ── */
  function syncPlateDraft(){
    const d=state.plateDraft;
    const val=(sel, cur)=>{ const el=q(sel); return el?el.value:cur; };
    d.wells=val('#lbPlateWells', d.wells);
    d.high=val('#lbPlateHigh', d.high);
    d.low=val('#lbPlateLow', d.low);
    d.min_controls=val('#lbPlateMinCtrl', d.min_controls);
    d.strain=val('#lbPlateStrain', d.strain);
    d.batch=val('#lbPlateBatch', d.batch);
  }

  function plateReportCard(res){
    const rep=res.report||{};
    const assay=rep.assay||{};
    const groups=rep.groups||{};
    const spatial=rep.spatial||{};
    const warnings=rep.warnings||[];
    const missing=rep.missing||[];
    const censored=rep.censored||[];
    const underpowered=warnings.some(w=>text(w).indexOf('有效孔')>=0);
    const z=assay.zprime;
    const passed=!underpowered && z!=null && z>=0.5;
    const minCtrl=Math.max(2, num(state.plateDraft.min_controls)||4);

    let bannerTxt='';
    if(underpowered) bannerTxt='⚠ 板质控没过：对照孔有效数不足（要求 ≥'+minCtrl+' 孔），Z\' 算得出来但不可信';
    else if(!passed) bannerTxt='⚠ 板质控没过：'+(assay.zprime_grade||'Z\' < 0.5 或无法判定');
    else bannerTxt='✔ 板质控通过：'+(assay.zprime_grade||'可用')+'（已解析 '+text(res.wells_parsed||0)+' 孔，有效数值 '+text(rep.n_ok||0)+' 孔）';

    const stats=[
      ['Z\'', z!=null?Number(z).toFixed(3):'无法计算', '合格线 ≥ 0.5'],
      ['SSMD', assay.ssmd!=null?Number(assay.ssmd).toFixed(2):'—', '分离良好 > 3'],
      ['信号窗', assay.signal_window!=null?fmtNum(assay.signal_window):'—', 'High − Low'],
      ['信噪比', assay.signal_to_background!=null?Number(assay.signal_to_background).toFixed(2):'—', 'High / Low'],
    ];

    const gRows=['high','low'].map(name=>{
      const g=groups[name];
      const label=name==='high'?'阳性对照 (high)':'阴性对照 (low)';
      if(!g) return '<tr><td><b>'+label+'</b></td><td colspan="6" class="lb-emptycell">未配置</td></tr>';
      const cvPct=g.cv!=null?(Number(g.cv)*100).toFixed(1)+'%':'—';
      const cvTag=g.cv==null?'—':(g.cv<=0.15?'<span class="lb-tag" data-tone="ok">✔ ≤ 15%</span>':'<span class="lb-tag" data-tone="err">⚠ 超 15%</span>');
      const nWarn=g.n<minCtrl?' <span class="lb-tag" data-tone="warn">不足 '+minCtrl+'</span>':'';
      const range=(g.min!=null&&g.max!=null)?(fmtNum(g.min)+' ~ '+fmtNum(g.max)):'—';
      return '<tr><td><b>'+label+'</b></td><td>'+esc(g.n)+nWarn+'</td><td>'+fmtNum(g.mean)+'</td>'+
        '<td>'+fmtNum(g.stdev)+'</td><td>'+esc(cvPct)+'</td><td>'+esc(range)+'</td><td>'+cvTag+'</td></tr>';
    }).join('');

    const edgeVal=spatial.edge_ratio;
    const driftVal=spatial.column_drift;
    const edgeStr=edgeVal!=null?((Number(edgeVal)*100).toFixed(1)+'%'):'无法评估';
    const driftStr=driftVal!=null?((Number(driftVal)*100).toFixed(1)+'%'):'无法评估';
    const edgeNote=edgeVal!=null&&Math.abs(edgeVal)>0.05?'<span class="lb-tag" data-tone="warn">提示边沿效应 (&gt;5%)</span>':'';
    const driftNote=driftVal!=null&&Math.abs(driftVal)>0.10?'<span class="lb-tag" data-tone="warn">提示列漂移 (&gt;10%)</span>':'';

    const chipList=(arr, tone)=>arr.length
      ? '<div class="lb-wellchips">'+arr.map(w=>'<span class="lb-tag" data-tone="'+tone+'">'+esc(w)+'</span>').join('')+'</div>'
      : '<div class="lb-note">无</div>';

    return '<div class="lb-section"><h3>质控结果报告<span class="lb-sub">阳性孔 '+esc((res.high||[]).join(', '))+' · 阴性孔 '+esc((res.low||[]).join(', '))+'</span></h3>'+
      '<div class="lb-plate-banner" data-pass="'+(passed?'1':'0')+'">'+fmtRich(bannerTxt)+'</div>'+
      '<div class="lb-summary">'+stats.map(s=>'<div class="lb-stat"><b>'+esc(s[1])+'</b><span>'+esc(s[0])+' <small>('+esc(s[2])+')</small></span></div>').join('')+'</div>'+
      '<div class="lb-tablewrap"><table class="lb-table"><thead><tr><th>对照组</th><th>有效孔 n</th><th>均值</th><th>SD</th><th>CV%</th><th>数值范围</th><th>复孔 CV (≤15%)</th></tr></thead><tbody>'+gRows+'</tbody></table></div>'+
      '<div class="lb-plate-diag">'+
        '<div class="lb-plate-box"><b>空间诊断</b>'+
          '<div class="lb-note">边沿比 (edge_ratio)：<b>'+esc(edgeStr)+'</b> '+edgeNote+'</div>'+
          '<div class="lb-note">列漂移 (column_drift)：<b>'+esc(driftStr)+'</b> '+driftNote+'</div>'+
        '</div>'+
        '<div class="lb-plate-box"><b>缺失孔 missing（'+missing.length+'）</b><div class="lb-note" style="margin-top:2px">nan / inf / NA 等，已排除未作 0</div>'+chipList(missing, 'err')+'</div>'+
        '<div class="lb-plate-box"><b>删失孔 censored（'+censored.length+'）</b><div class="lb-note" style="margin-top:2px">ND / &lt;0.05 / BQL 等，低于或超限</div>'+chipList(censored, 'warn')+'</div>'+
      '</div>'+
      (warnings.length?'<div class="lb-plate-box"><b>告警与提示（'+warnings.length+'）</b><ul class="lb-plate-warns">'+warnings.map(w=>'<li>'+fmtRich(w)+'</li>').join('')+'</ul></div>':'')+
      (res.summary?'<details class="lb-details" style="margin-top:10px"><summary>查看服务端纯文本摘要</summary><pre class="lb-digest">'+esc(res.summary)+'</pre></details>':'')+
    '</div>';
  }

  function plateBody(){
    const st=(state.data&&state.data.state)||{};
    const strains=Object.keys(st.strains||{});
    const batches=Object.keys(st.batches||{});
    const d=state.plateDraft;
    let h='<div class="lb-section"><h3>96 孔板内质控工作台<span class="lb-sub">严格区分缺失值 (nan/inf) 与删失值 (ND/&lt;0.05)，只判合格性不剔孔</span></h3>'+
      '<div class="lb-form">'+
      '<label>阳性对照孔 (high)<input id="lbPlateHigh" value="'+esc(d.high)+'" placeholder="如 A1-A6"></label>'+
      '<label>阴性对照孔 (low)<input id="lbPlateLow" value="'+esc(d.low)+'" placeholder="如 H1-H6"></label>'+
      '<label>最少对照孔数<input id="lbPlateMinCtrl" type="number" min="2" max="48" value="'+esc(d.min_controls||'4')+'"></label>'+
      '<label>关联菌株（可空）<input id="lbPlateStrain" list="lbPlateStrains" value="'+esc(d.strain)+'" placeholder="如 S01"></label>'+
      '<label>关联批次（可空）<input id="lbPlateBatch" list="lbPlateBatches" value="'+esc(d.batch)+'" placeholder="如 B01"></label>'+
      '<datalist id="lbPlateStrains">'+strains.map(c=>'<option value="'+esc(c)+'">').join('')+'</datalist>'+
      '<datalist id="lbPlateBatches">'+batches.map(b=>'<option value="'+esc(b)+'">').join('')+'</datalist>'+
      '<label class="lb-wide">读板数据（支持 A1=1.234 键值对或 8×12 矩阵表格粘贴）'+
        '<textarea id="lbPlateWells" rows="6" placeholder="粘贴 A1=1.234 A2=1.198 ... 或直接粘贴 8×12 孔板表格（空格/逗号/Tab 分隔）">'+esc(d.wells)+'</textarea></label>'+
      '<div class="lb-actions lb-wide">'+
        '<button type="button" class="mini" id="lbPlateDemo">填入示例读板数据（含 ND/&lt;0.05/nan）</button>'+
        '<button type="button" class="mini" id="lbPlateClear">清空</button>'+
        '<button type="button" class="mini pri" id="lbPlateCalc">立即质控计算</button>'+
        '<button type="button" class="mini" id="lbPlateRecord" '+(live()?'disabled':'')+'>质控并记入本课题台账</button>'+
      '</div></div></div>';
    if(state.plateResult) h+=plateReportCard(state.plateResult);
    return h;
  }

  async function postMethod(fields){
    if(state.busy) return;
    if(live()){ state.error='任务运行中，结束后再记录方法。'; paint(); return; }
    if(!state.slug){ state.error='先选择（或新建）一个课题。'; paint(); return; }
    state.busy=true; state.error=''; state.notice='';
    try{
      const d=await api('/api/methods',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(Object.assign({slug:state.slug},fields))});
      if(d.ok===false||d.error) throw new Error(d.error||'方法记录失败');
      const ev=d.event||{};
      const mdVer=d.md_sync && d.md_sync.verified;
      state.notice='已记录方法事件 #'+text(ev.seq||'')+'（'+text(ev.kind||'')+(ev.id?' · '+text(ev.id):'')+'），并核验落盘（verified='+text(d.verified)+(mdVer!=null?'，METHODS.md='+text(mdVer):'')+'）。';
      if(fields.kind==='method'||fields.kind==='version'){
        state.methodDraft=Object.assign({},state.methodDraft,{
          id: fields.kind==='version' ? fields.id : '',
          title:'', differs_from:'', what_changed:'', paramsText:'', stepsText:'', pitfallsText:'',
          instrument:'', consumable:'', yield:'', note:'',
        });
      }else if(fields.kind==='observe'){
        state.methodDraft.obsText='';
      }else if(fields.kind==='patent'){
        state.methodDraft.claimsText='';
      }
      await loadMethods(state.slug);
    }catch(e){ state.error=e.message; paint(); }
    finally{ state.busy=false; }
  }

  async function runPlate(recordToLab){
    if(state.busy) return;
    syncPlateDraft();
    const d=state.plateDraft;
    if(!text(d.wells).trim()){
      state.error='请先粘贴孔板读数（支持 A1=1.234 键值对或 8×12 表格），或点「填入示例读板数据」。';
      paint(); return;
    }
    if(recordToLab && live()){ state.error='任务运行中，结束后再记入本课题台账。'; paint(); return; }
    if(recordToLab && !state.slug){ state.error='先选择（或新建）一个课题。'; paint(); return; }
    state.busy=true; state.error=''; state.notice='';
    try{
      const minCtrl=Math.max(2, num(d.min_controls)||4);
      const res=await api('/api/lab/plate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          slug: state.slug,
          wells: d.wells,
          high: text(d.high).trim()||'A1-A6',
          low: text(d.low).trim()||'H1-H6',
          min_controls: minCtrl,
          batch: text(d.batch).trim(),
          strain: text(d.strain).trim(),
          record_to_lab: !!recordToLab,
        })});
      if(res.ok===false||res.error) throw new Error(res.error||'板内质控计算失败');
      state.plateResult=res;
      if(recordToLab){
        const ev=(res.lab_event&&res.lab_event.event)||{};
        const ver=res.lab_event&&res.lab_event.verified;
        state.notice='已完成板内质控并记入本课题台账（#'+text(ev.seq||'')+'，verified='+text(ver)+'）。';
        await load(state.slug, true);
      }else{
        state.notice='已完成板内质控计算（解析 '+text(res.wells_parsed||0)+' 孔，未记入台账）。';
        paint();
      }
    }catch(e){ state.error=e.message; paint(); }
    finally{ state.busy=false; }
  }

  /* ── 全部项目总览：按课题存档与进度对照 ── */
  function projectsBody(){
    const ps=(state.data&&state.data.projects)||[];
    if(!ps.length) return '<div class="lb-empty">还没有任何课题项目。</div>';
    return '<div class="lb-section"><h3>按项目保存的实验进度总览<span class="lb-sub">点卡片直接切过去看排期与进展</span></h3>'+
      '<div class="lb-projgrid">'+ps.map(p=>
        '<article class="lb-projcard'+(p.slug===state.slug?' on':'')+'">'+
          '<div class="lb-proj-top"><b>'+html(p.title||p.slug)+'</b>'+
          '<span class="tag" data-tone="'+(p.overdue_total?'err':p.overall_progress_pct>=80?'ok':'info')+'">'+html(p.overall_progress_pct)+'%</span></div>'+
          '<div class="lb-proj-sub">'+html(STAGE_CN[p.stage]||p.stage)+' · 事件 '+html(p.n_events)+' 条'+(p.open_blockers?' · 卡点 '+p.open_blockers:'')+'</div>'+
          '<div class="lb-minibar"><i style="width:'+html(p.overall_progress_pct)+'%"></i></div>'+
          '<div class="lb-proj-meta">实验 '+p.experiments_done+'/'+p.experiments_total+' · 待办 '+p.todos_open+
          (p.overdue_total?' · 逾期 '+p.overdue_total:'')+
          (p.next_due?' · 最近到期 '+html(p.next_due):'')+'</div>'+
          (p.next_step?'<div class="lb-proj-next">下一步：'+html(p.next_step)+'</div>':'')+
          '<div class="lb-actions"><button class="mini'+(p.slug===state.slug?'':' pri')+'" data-switch-proj="'+html(p.slug)+'">'+(p.slug===state.slug?'当前项目':'切换到此项目')+'</button></div>'+
        '</article>'
      ).join('')+'</div></div>';
  }

  function paint(){
    const page=q('#labPage'); if(!page) return;
    const tabs=[['plan','规划与排期'],['results','进展与台账'],['methods','方法与专利'],['plate','板内质控'],['analysis','分析与汇报'],['projects','全部项目']];
    let body;
    if(state.error && !state.data) body=errBox(state.error);
    else if(!state.data) body='<div class="lb-empty">正在读取实验台账…</div>';
    else if(state.data.no_topic || !state.slug) body=noTopic();
    else body = state.tab==='plan'?planBody()
      : state.tab==='results'?resultsBody()
      : state.tab==='methods'?methodsBody()
      : state.tab==='plate'?plateBody()
      : state.tab==='projects'?projectsBody()
      : analysisBody();
    page.innerHTML='<div class="lb-manager">'+head()+
      (state.error&&state.data?errBox(state.error):'')+
      (state.notice?'<div class="lb-notice">'+html(state.notice)+'</div>':'')+
      (state.data&&state.slug?'<div class="lb-tabs">'+tabs.map(t=>'<button class="lb-tab" data-tab="'+t[0]+'" aria-selected="'+(state.tab===t[0])+'">'+t[1]+'</button>').join('')+'</div>':'')+
      '<div class="lb-body">'+body+'</div></div>';
    wire(page);
  }

  function download(name, content, mime){
    const blob=new Blob([content],{type:mime||'text/plain;charset=utf-8'});
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a'); a.href=url; a.download=name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }

  function wire(page){
    const sel=q('#lbTopic'); if(sel) sel.onchange=()=>{ state.report=null; state.methodsData=null; state.plateResult=null; load(sel.value); };
    const rf=q('#lbRefresh'); if(rf) rf.onclick=()=>load(state.slug);
    const cr=q('#lbCreate'); if(cr) cr.onclick=createTopic;
    const gm=q('#lbGoMentor'); if(gm) gm.onclick=()=>{ if(typeof goView==='function') goView('mentor'); };
    page.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{
      if(state.tab==='methods') syncMethodDraft();
      if(state.tab==='plate') syncPlateDraft();
      state.tab=b.dataset.tab; state.retractSeq=null; paint();
      if(state.tab==='analysis'){ loadReport(); if(!state.methodsData) loadMethods(state.slug); }
      if(state.tab==='methods') loadMethods(state.slug);
    });
    page.querySelectorAll('[data-kind]').forEach(b=>b.onclick=()=>{ state.formKind=b.dataset.kind; paint(); });
    const methodsList=(state.methodsData&&state.methodsData.methods)||[];
    const applyMethodDefaults=(kind, id, copyLatest)=>{
      const m=methodsList.find(x=>x.id===id)||methodsList[0];
      if(!m) return;
      state.methodDraft.id=m.id;
      if(kind==='version'){
        const vers=m.versions||[];
        const maxV=vers.reduce((mx,v)=>Math.max(mx, Number(v.version||0)), 1);
        state.methodDraft.version=String(maxV+1);
        if(copyLatest){
          const latest=m.latest||(vers.length?vers[vers.length-1]:null)||{};
          state.methodDraft=Object.assign({},state.methodDraft,{
            what_changed:'',
            paramsText:(latest.params||[]).map(p=>p.name+' | '+p.value+' | '+p.src).join('\n'),
            stepsText:(latest.steps||[]).join('\n'),
            pitfallsText:(latest.pitfalls||[]).join('\n'),
            instrument:latest.instrument||'', consumable:latest.consumable||'',
            yield:latest.yield||'', note:latest.note||'',
          });
        }
      }else if(kind==='observe'){
        state.methodDraft.obsVer=m.latest&&m.latest.version?String(m.latest.version):'';
      }else if(kind==='patent'){
        const pat=m.patent||{};
        state.methodDraft.disclosed_at=pat.disclosed_at||'';
        state.methodDraft.first_public=pat.first_public||'';
        state.methodDraft.claimsText=(pat.claims_basis||[]).join('\n');
      }
    };

    page.querySelectorAll('[data-mkind]').forEach(b=>b.onclick=()=>{
      syncMethodDraft();
      state.methodFormKind=b.dataset.mkind;
      if(state.methodFormKind!=='method' && methodsList.length){
        applyMethodDefaults(state.methodFormKind, state.methodDraft.id, false);
      }
      paint();
    });
    const mVerSel=q('#lbMetVerId'); if(mVerSel) mVerSel.onchange=()=>{ syncMethodDraft(); applyMethodDefaults('version', mVerSel.value, false); paint(); };
    const mObsSel=q('#lbMetObsId'); if(mObsSel) mObsSel.onchange=()=>{ syncMethodDraft(); applyMethodDefaults('observe', mObsSel.value, false); paint(); };
    const mPatSel=q('#lbMetPatId'); if(mPatSel) mPatSel.onchange=()=>{ syncMethodDraft(); applyMethodDefaults('patent', mPatSel.value, false); paint(); };
    page.querySelectorAll('[data-switch-proj]').forEach(b=>b.onclick=()=>{
      const s=b.dataset.switchProj; state.tab='plan'; state.report=null; state.methodsData=null; state.plateResult=null; load(s);
    });

    const addP=q('#lbMetAddParam'); if(addP) addP.onclick=()=>{
      syncMethodDraft();
      const pn=text(q('#lbMetPName')&&q('#lbMetPName').value).trim();
      const pv=text(q('#lbMetPVal')&&q('#lbMetPVal').value).trim();
      const ps=text(q('#lbMetPSrc')&&q('#lbMetPSrc').value).trim()||'user';
      if(!pn||!pv){ state.error='请填写参数名与数值（含单位）后再加入参数表。'; paint(); return; }
      const line=pn+' | '+pv+' | '+ps;
      state.methodDraft.paramsText=(state.methodDraft.paramsText?state.methodDraft.paramsText.replace(/\s+$/,'')+'\n':'')+line;
      state.error='';
      paint();
      const nEl=q('#lbMetPName'); if(nEl) nEl.focus();
    };
    const metSub=q('#lbMetSubmit'); if(metSub) metSub.onclick=()=>{
      syncMethodDraft();
      const k=state.methodFormKind;
      const d=state.methodDraft;
      if(k==='method'||k==='version'){
        const id=text(d.id).trim();
        if(!id){ state.error='请填写或选择方法英文 ID。'; paint(); return; }
        let paramsText=d.paramsText;
        const pn=text(q('#lbMetPName')&&q('#lbMetPName').value).trim();
        const pv=text(q('#lbMetPVal')&&q('#lbMetPVal').value).trim();
        const ps=text(q('#lbMetPSrc')&&q('#lbMetPSrc').value).trim()||'user';
        if(pn && pv){
          paramsText=(paramsText?paramsText.replace(/\s+$/,'')+'\n':'')+pn+' | '+pv+' | '+ps;
          d.paramsText=paramsText;
        }
        let params;
        try{ params=parseParamsText(paramsText); }
        catch(e){ state.error=e.message; paint(); return; }
        const steps=splitLines(d.stepsText);
        const pitfalls=splitLines(d.pitfallsText);
        const payload={
          kind: k, id,
          what_changed: text(d.what_changed).trim()||undefined,
          params, steps, pitfalls,
          instrument: text(d.instrument).trim()||undefined,
          consumable: text(d.consumable).trim()||undefined,
          yield: text(d.yield).trim()||undefined,
          note: text(d.note).trim()||undefined,
        };
        if(k==='method'){
          const title=text(d.title).trim();
          if(!title){ state.error='请填写方法名称 (title)。'; paint(); return; }
          const differs=text(d.differs_from).split(/[,，;\s]+/).map(s=>s.trim()).filter(Boolean);
          payload.title=title;
          payload.origin=d.origin||'adapted';
          payload.differs_from=differs;
        }else{
          const ver=parseInt(text(d.version).trim(), 10);
          if(!ver||ver<1){ state.error='版本号必须是 ≥ 1 的整数。'; paint(); return; }
          payload.version=ver;
        }
        postMethod(payload);
      }else if(k==='observe'){
        const id=text(d.id).trim();
        const obsTxt=text(d.obsText).trim();
        if(!id){ state.error='请先选择方法。'; paint(); return; }
        if(!obsTxt){ state.error='请填写实操观察记录。'; paint(); return; }
        const payload={kind:'observe', id, text:obsTxt, version:text(d.obsVer).trim()||undefined};
        if(d.obsStatus==='worked') payload.worked=true;
        else if(d.obsStatus==='failed') payload.failed=true;
        postMethod(payload);
      }else if(k==='patent'){
        const id=text(d.id).trim();
        if(!id){ state.error='请先选择方法。'; paint(); return; }
        postMethod({
          kind:'patent', id,
          disclosed_at: text(d.disclosed_at).trim()||undefined,
          first_public: text(d.first_public).trim()||undefined,
          claims_basis: splitLines(d.claimsText),
        });
      }
    };

    page.querySelectorAll('[data-m-ver]').forEach(b=>b.onclick=()=>{
      state.methodFormKind='version';
      applyMethodDefaults('version', b.dataset.mVer, true);
      paint();
      const ch=q('#lbMetChanged'); if(ch){ ch.scrollIntoView({behavior:'smooth',block:'center'}); ch.focus(); }
    });
    page.querySelectorAll('[data-m-obs]').forEach(b=>b.onclick=()=>{
      state.methodFormKind='observe';
      applyMethodDefaults('observe', b.dataset.mObs, false);
      paint();
      const ot=q('#lbMetObsText'); if(ot){ ot.scrollIntoView({behavior:'smooth',block:'center'}); ot.focus(); }
    });
    page.querySelectorAll('[data-m-pat]').forEach(b=>b.onclick=()=>{
      state.methodFormKind='patent';
      applyMethodDefaults('patent', b.dataset.mPat, false);
      paint();
      const pd=q('#lbMetPatDisclosed'); if(pd){ pd.scrollIntoView({behavior:'smooth',block:'center'}); pd.focus(); }
    });
    page.querySelectorAll('[data-m-ask]').forEach(b=>b.onclick=()=>{
      const m=methodsList.find(x=>x.id===b.dataset.mAsk); if(!m) return;
      switchToTask(buildMethodAdvisorPrompt(m));
    });
    page.querySelectorAll('[data-m-retract]').forEach(b=>b.onclick=()=>{
      state.retractMethodSeq=Number(b.dataset.mRetract);
      paint();
      const inp=q('#lbMRetractWhy'); if(inp) inp.focus();
    });
    const mrg=q('#lbMRetractGo'); if(mrg) mrg.onclick=()=>{
      const why=text(q('#lbMRetractWhy')&&q('#lbMRetractWhy').value).trim();
      if(!why){ state.error='作废方法必须填写理由（why）。'; paint(); return; }
      const seq=state.retractMethodSeq;
      state.retractMethodSeq=null;
      postMethod({slug:state.slug, kind:'retract', seq:seq, why:why});
    };
    const mrc=q('#lbMRetractCancel'); if(mrc) mrc.onclick=()=>{ state.retractMethodSeq=null; paint(); };

    const plDemo=q('#lbPlateDemo'); if(plDemo) plDemo.onclick=()=>{
      syncPlateDraft();
      state.plateDraft.wells=PLATE_DEMO;
      state.plateDraft.high='A1-A6';
      state.plateDraft.low='H1-H6';
      state.error='';
      paint();
    };
    const plClear=q('#lbPlateClear'); if(plClear) plClear.onclick=()=>{
      syncPlateDraft();
      state.plateDraft.wells='';
      state.plateResult=null;
      state.error='';
      paint();
    };
    const plCalc=q('#lbPlateCalc'); if(plCalc) plCalc.onclick=()=>runPlate(false);
    const plRec=q('#lbPlateRecord'); if(plRec) plRec.onclick=()=>runPlate(true);

    const pp=q('#lbPlanParse'); if(pp) pp.onclick=()=>submitPlan(true);
    const pi=q('#lbPlanImport'); if(pi) pi.onclick=()=>submitPlan(false);

    page.querySelectorAll('[data-prog-open]').forEach(b=>b.onclick=()=>{
      const id=b.dataset.progOpen;
      state.progressEditId = state.progressEditId===id ? null : id;
      paint();
    });
    const pec=q('#lbPECancel'); if(pec) pec.onclick=()=>{ state.progressEditId=null; paint(); };
    const pes=q('#lbPESave'); if(pes) pes.onclick=()=>{
      record({
        kind:'progress',
        id: state.progressEditId,
        progress: num(q('#lbPEPct')&&q('#lbPEPct').value),
        status: q('#lbPEStatus')?q('#lbPEStatus').value:undefined,
        next_follow_up: text(q('#lbPEDue')&&q('#lbPEDue').value).trim()||undefined,
        note: text(q('#lbPENote')&&q('#lbPENote').value).trim()||undefined,
      });
    };

    const ta=q('#lbTodoAdd'); if(ta) ta.onclick=()=>{
      const what=text(q('#lbTodoWhat').value).trim();
      if(!what){ state.error='请填写待办事项。'; paint(); return; }
      record({kind:'todo', id:text(q('#lbTodoId').value).trim(), what, due:text(q('#lbTodoDue').value).trim()});
    };
    const ba=q('#lbBatchAdd'); if(ba) ba.onclick=()=>{
      const code=text(q('#lbBatchCode').value).trim();
      if(!code){ state.error='请填写批次对应的菌株。'; paint(); return; }
      record({kind:'batch', id:text(q('#lbBatchId').value).trim(), code,
              started:text(q('#lbBatchStart').value).trim(), expect_days:num(q('#lbBatchDays').value)});
    };
    page.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>record({kind:'close', id:b.dataset.close, how:b.dataset.how}));

    const rc=q('#lbRecord'); if(rc) rc.onclick=()=>{
      const k=state.formKind;
      if(k==='measure'){
        const value=num(q('#lbValue').value);
        if(value===undefined){ state.error='定量测量必须填一个数字。'; paint(); return; }
        record({kind:'measure', code:text(q('#lbCode').value).trim(), metric:q('#lbMetric').value,
                value, n:num(q('#lbN').value), sd:num(q('#lbSd').value), note:text(q('#lbNote').value).trim()});
      }else if(k==='observe'){
        record({kind:'observe', code:text(q('#lbObsCode').value).trim(), growth:q('#lbGrowth').value,
                temp_c:num(q('#lbTemp').value), text:text(q('#lbObsNote').value).trim()});
      }else if(k==='experiment'){
        record({kind:'experiment', title:text(q('#lbExpTitle').value).trim(),
                phase:text(q('#lbExpPhase').value).trim(),
                started:text(q('#lbExpStart').value).trim(),
                due:text(q('#lbExpDue').value).trim(),
                priority:q('#lbExpPrio')?q('#lbExpPrio').value:'normal',
                protocol:text(q('#lbExpProto').value).trim()});
      }else if(k==='progress'){
        record({kind:'progress', id:q('#lbProgId')?q('#lbProgId').value:'',
                progress:num(q('#lbProgPct')&&q('#lbProgPct').value),
                status:q('#lbProgStatus')?q('#lbProgStatus').value:undefined,
                next_follow_up:text(q('#lbProgFollow')&&q('#lbProgFollow').value).trim()||undefined,
                note:text(q('#lbProgNote')&&q('#lbProgNote').value).trim()||undefined});
      }else if(k==='strain'){
        const ph=q('#lbPlaceholder');
        record({kind:'strain', code:text(q('#lbStrCode').value).trim(), species:text(q('#lbSpecies').value).trim(),
                role:q('#lbRole').value, placeholder:ph?ph.checked:undefined});
      }else{
        record({kind:'metric', key:text(q('#lbKey').value).trim(), label:text(q('#lbLabel').value).trim(),
                unit:text(q('#lbUnit').value).trim(), n_min:num(q('#lbNmin').value), method:text(q('#lbMetNote').value).trim()});
      }
    };

    page.querySelectorAll('[data-retract]').forEach(b=>b.onclick=()=>{ state.retractSeq=Number(b.dataset.retract); paint(); const i=q('#lbRetractReason'); if(i)i.focus(); });
    const rg=q('#lbRetractGo'); if(rg) rg.onclick=()=>{
      const reason=text(q('#lbRetractReason').value).trim();
      if(!reason){ state.error='作废必须写理由 —— 三个月后没人知道这条为什么被划掉。'; paint(); return; }
      record({kind:'retract', seq:state.retractSeq, reason});
    };
    const rcan=q('#lbRetractCancel'); if(rcan) rcan.onclick=()=>{ state.retractSeq=null; paint(); };

    const askAdv=q('#lbAskAdvisor'); if(askAdv) askAdv.onclick=()=>{
      if(!state.report||!state.report.md) return;
      switchToTask('请以导师视角诊断我这份实验进展报告（指出数据缺口、实验逻辑漏洞与下一步优先级）：\n\n'+state.report.md);
    };
    const cp=q('#lbCopyMd'); if(cp) cp.onclick=()=>copyText(state.report.md, '已复制 LAB.md 到剪贴板。');
    const dm=q('#lbDlMd'); if(dm) dm.onclick=()=>download('LAB.md', state.report.md, 'text/markdown;charset=utf-8');
    const dc=q('#lbDlCsv'); if(dc) dc.onclick=()=>download('LAB.csv', state.report.csv, 'text/csv;charset=utf-8');
    const cpMet=q('#lbCopyMethodsMd'); if(cpMet) cpMet.onclick=()=>{
      if(state.methodsData&&state.methodsData.md) copyText(state.methodsData.md, '已复制 METHODS.md 到剪贴板。');
    };
    const dlMet=q('#lbDlMethodsMd'); if(dlMet) dlMet.onclick=()=>{
      if(state.methodsData&&state.methodsData.md) download('METHODS.md', state.methodsData.md, 'text/markdown;charset=utf-8');
    };
  }

  function open(){
    if(!state.data) load(state.slug);
    else{
      paint();
      if(state.slug && !state.methodsData) loadMethods(state.slug);
      if(state.slug && state.tab==='analysis' && !state.report) loadReport();
    }
  }
  function showTab(t, slug){
    const target = t==='report' ? 'analysis' : (t || 'plan');
    state.tab=target;
    state.retractSeq=null;
    if(slug && slug!==state.slug){
      state.report=null; state.methodsData=null; state.plateResult=null;
      load(slug);
      return;
    }
    if(!state.data) load(state.slug);
    else{
      paint();
      if(target==='methods') loadMethods(state.slug);
      if(target==='analysis'){ loadReport(); if(!state.methodsData) loadMethods(state.slug); }
    }
  }
  window.RongkeLab={open, showTab, refresh:()=>{ if(q('#v-lab.on')) load(state.slug); }, state};
})();
