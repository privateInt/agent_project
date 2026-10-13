'use strict';
(() => {
  const $ = id => document.getElementById(id), upload = window.QAUpload, recovery=window.QARecovery;
  const STORE = 'document-qa.q07a.pending-upload';
  const ACTION_STORE='document-qa.q07b.pending-action';
  let questionScreen=null;
  const state = {token:'', epoch:0, busy:false, library:null, jobs:[], selected:null, pending:null,
    pendingError:false, controllers:new Set(), checked:0, queryFailed:false,
    pendingAction:null, recoveryError:false, review:null, recoveryButtons:[]};
  const messages = {
    AUTH_REQUIRED:'접속 키를 확인하고 다시 연결하세요.', HOST_NOT_ALLOWED:'자료함 서버의 주소로 접속하세요.',
    ORIGIN_NOT_ALLOWED:'자료함과 같은 주소에서 다시 연결하세요.',
    UPLOAD_NAME_INVALID:'문서 이름은 1~200자로 입력하세요.', UPLOAD_BYTES_INVALID:'빈 파일은 올릴 수 없으며 파일당 32MiB까지 지원합니다.',
    UPLOAD_FORMAT_UNSUPPORTED:'TXT, Markdown, PDF 파일을 선택하세요.', UPLOAD_FORMAT_MISMATCH:'파일 확장자와 실제 내용을 확인하세요.',
    UPLOAD_TEXT_NOT_UTF8:'텍스트 문서를 UTF-8 형식으로 저장한 뒤 올려주세요.',
    DOCUMENT_CAPACITY_EXCEEDED:'이 자료함에는 최대 20개 문서를 등록할 수 있습니다.',
    REQUEST_KEY_CONFLICT:'기존 업로드와 다른 파일입니다. 원래 파일의 접수를 먼저 확인하세요.',
    PENDING_RECORD_INVALID:'이 창의 업로드 확인 기록을 읽을 수 없습니다. 새 파일을 접수하기 전에 관리자의 확인이 필요합니다.',
    PENDING_STORE_UNAVAILABLE:'이 브라우저에서 업로드 확인 기록을 저장할 수 없습니다. 브라우저의 저장 허용 설정을 확인하세요.',
    PENDING_LIBRARY_CHANGED:'자료함이 변경됐습니다. 이전 업로드의 확인이 필요합니다.',
    PENDING_FILE_CHANGED:'이전 업로드와 같은 파일·문서 이름을 선택하세요. 파일 내용도 같아야 합니다.',
    RESPONSE_INVALID:'서버 응답을 확인할 수 없습니다. 기존 접수를 먼저 조회하세요.',
    API_CAPACITY_EXCEEDED:'다른 작업이 진행 중입니다. 잠시 후 상태를 다시 확인하세요.',
    RECOVERY_BODY_INVALID:'실행 내용을 확인할 수 없습니다. 문서 상태를 다시 조회하세요.',
    RECOVERY_STATE_CHANGED:'문서 상태가 변경됐습니다. 최신 상태에서 필요한 동작을 다시 선택하세요.',
    RECOVERY_ACTION_NOT_ALLOWED:'현재 상태에서는 이 동작을 실행할 수 없습니다. 문서 상태를 다시 확인하세요.',
    RECOVERY_RESPONSE_INVALID:'실행 결과를 확인할 수 없습니다. 이전 동작의 상태를 먼저 조회하세요.',
    RECOVERY_RECORD_INVALID:'이 창의 실행 확인 기록을 읽을 수 없습니다. 관리자 확인이 필요합니다.',
    RECOVERY_LIBRARY_CHANGED:'자료함이 변경됐습니다. 이전 동작을 수행한 자료함의 확인이 필요합니다.',
    RECOVERY_PENDING:'이전 동작의 결과를 먼저 확인하세요. 새 동작을 자동으로 실행하지 않습니다.',
    UPLOAD_BUSY:'이 문서의 동작이 진행 중입니다. 잠시 후 상태를 다시 확인하세요.',
  };
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  }
  function notice(text='', error=false) { $('notice').textContent=text; $('notice').classList.toggle('error',error); }
  function readable(error) { return messages[error.code || error.message] || '연결 또는 처리 결과를 확인할 수 없습니다. 상태를 다시 확인하세요.'; }
  function stamp(value) { const date=new Date(value); return Number.isNaN(date.valueOf()) ? '시각 확인 불가' : date.toLocaleString('ko-KR',{hour12:false}); }
  function size(bytes) { return bytes >= 1024*1024 ? (bytes/(1024*1024)).toFixed(1)+' MiB' : bytes >= 1024 ? (bytes/1024).toFixed(1)+' KiB' : bytes+' B'; }
  function button(text, action, className='secondary') { const element=node('button',text,className); element.type='button';element.addEventListener('click',action);return element; }
  function badge(text, kind='') { return node('span',text,'badge '+kind); }
  function jobChecked(job) {
    if (!job || !upload.uid(job.job_id) || typeof job.name !== 'string' || typeof job.filename !== 'string'
        || !Number.isInteger(job.size_bytes) || job.size_bytes < 1 || job.size_bytes > upload.MAX_BYTES
        || !/^[0-9a-f]{64}$/.test(job.sha256) || typeof job.state !== 'string') throw Error('RESPONSE_INVALID');
    return job;
  }
  function libraryChecked(value) {
    if (!value || !upload.uid(value.library_id) || typeof value.name !== 'string'
        || !Number.isInteger(value.reserved_documents) || value.reserved_documents < 0 || value.reserved_documents > 20
        || typeof value.question_ready !== 'boolean') throw Error('RESPONSE_INVALID');
    return value;
  }
  async function api(path, options={}) {
    const epoch=state.epoch, control=new AbortController();state.controllers.add(control);
    const timer=setTimeout(()=>control.abort(),options.body ? 210000 : 12000);
    try {
      const result=await fetch(path,{method:options.body?'POST':'GET',...options,credentials:'omit',cache:'no-store',redirect:'error',
        signal:control.signal,headers:{'Accept':'application/json','Authorization':'Bearer '+state.token,...options.headers}});
      const value=await result.json();
      if (epoch!==state.epoch) throw Error('SESSION_CHANGED');
      if (!result.ok) {
        const error=Object.assign(Error(value.code || 'REQUEST_FAILED'),{code:value.code,status:result.status});
        if (result.status===401) disconnect();
        throw error;
      }
      return value;
    } finally {clearTimeout(timer);state.controllers.delete(control);}
  }
  function loadPending() {
    state.pending=null;state.pendingError=false;
    try {const raw=sessionStorage.getItem(STORE);if(raw!==null) state.pending=upload.pending(JSON.parse(raw));}
    catch {state.pendingError=true;notice(messages.PENDING_RECORD_INVALID,true);}
  }
  function savePending(record) {
    try {sessionStorage.setItem(STORE,JSON.stringify(record));if(sessionStorage.getItem(STORE)!==JSON.stringify(record)) throw Error();}
    catch {throw Error('PENDING_STORE_UNAVAILABLE');}
    state.pending=record;
  }
  function clearPending() {
    try {sessionStorage.removeItem(STORE);if(sessionStorage.getItem(STORE)!==null) throw Error();}
    catch {throw Error('PENDING_STORE_UNAVAILABLE');}
    state.pending=null;
  }
  function loadRecovery() {
    state.pendingAction=null;state.recoveryError=false;
    try {const raw=sessionStorage.getItem(ACTION_STORE);if(raw!==null)state.pendingAction=recovery.pending(JSON.parse(raw));}
    catch {state.recoveryError=true;notice(messages.RECOVERY_RECORD_INVALID,true);}
  }
  function saveRecovery(record) {
    try {const raw=JSON.stringify(recovery.pending(record));sessionStorage.setItem(ACTION_STORE,raw);if(sessionStorage.getItem(ACTION_STORE)!==raw)throw Error();}
    catch {throw Error('PENDING_STORE_UNAVAILABLE');}
    state.pendingAction=record;
  }
  function clearRecovery() {
    try {sessionStorage.removeItem(ACTION_STORE);if(sessionStorage.getItem(ACTION_STORE)!==null)throw Error();}
    catch {throw Error('PENDING_STORE_UNAVAILABLE');}
    state.pendingAction=null;
  }
  function actionPlan(job) {return recovery.plan(job.recovery,job.job_id);}
  function actionAllowed(job,item) {
    const prior=state.pendingAction;
    return !state.recoveryError && (!prior || prior.library_id===state.library?.library_id
      && prior.job_id===job.job_id && prior.action===item.id && prior.revision===job.recovery.revision);
  }
  function reconcileRecovery() {
    if(!state.pendingAction)return;
    if(state.pendingAction.library_id!==state.library?.library_id){notice(messages.RECOVERY_LIBRARY_CHANGED,true);return;}
    const job=state.jobs.find(j=>j.job_id===state.pendingAction.job_id);if(!job)return;
    let plan;try {plan=actionPlan(job);}catch {return;}
    if(plan.revision!==state.pendingAction.revision){
      clearRecovery();state.review=null;
      notice('문서 상태가 변경됐습니다. 요청한 동작의 완료 여부는 현재 준비 상태에서 확인하세요.');
    }
  }
  function controls() {
    const connected=Boolean(state.token && state.library);
    $('upload-submit').disabled=!connected || state.busy || state.pendingError || (!state.pending && state.library.reserved_documents>=20);
    $('upload-submit').textContent=state.busy ? '접수 결과 확인 중…' : state.pending ? '같은 파일 접수 다시 확인' : '자료함에 올리기';
    $('refresh').disabled=!connected || state.busy;
    $('refresh-job').disabled=!connected || state.busy;
    $('recover-upload').disabled=!connected || state.busy || state.pendingError;
    $('file').disabled=state.busy; $('document-name').disabled=state.busy;
    $('pending-panel').hidden=!state.pending;
    if (state.pending) $('pending-description').textContent=state.pending.filename+' · '+size(state.pending.size_bytes)+' · '+state.pending.name;
    $('recovery-pending').hidden=!state.pendingAction;
    $('recover-action').disabled=!connected || state.busy || state.recoveryError;
    if(state.pendingAction)$('recovery-pending-text').textContent='응답을 확인하지 못한 동작이 있습니다. 상태를 조회한 뒤 필요한 경우 같은 동작을 명시적으로 다시 선택하세요.';
    for(const {element,job,item} of state.recoveryButtons)element.disabled=!connected || state.busy || state.queryFailed || !actionAllowed(job,item);
    $('recovery-review').hidden=!state.review;
    $('recovery-confirm').disabled=!connected || state.busy || state.queryFailed || !state.review;
    $('recovery-cancel').disabled=state.busy;
    questionScreen?.controls();
  }
  function disconnect() {
    state.epoch++;for(const control of state.controllers)control.abort();state.controllers.clear();
    state.token='';state.busy=false;state.library=null;state.jobs=[];state.selected=null;state.checked=0;
    state.review=null;state.recoveryButtons=[];
    questionScreen?.disconnect();
    $('token').value='';$('file').value='';$('document-name').value='';$('workspace').hidden=true;
    $('login-panel').hidden=false;$('logout').hidden=true;$('connection-state').textContent='연결 전';
    $('connection-state').classList.remove('connected');$('documents').replaceChildren();$('detail-panel').hidden=true;
    $('detail-info').replaceChildren();$('detail-steps').replaceChildren();$('login-submit').disabled=false;
    controls();
  }
  function stages(job) {
    const r=job.refinement || {}, cpu=job.refinement_workflow || {}, ix=job.indexing || {}, flow=job.indexing_workflow || {};
    const uploaded=job.state==='preparation_pending', canonical=r.state==='canonical_verified' || cpu.state==='canonical_verified';
    const bad=value=>['failed','attention_required','expired','interrupted','blocked'].includes(value);
    const paused=flow.error_code==='GPU_VERIFICATION_DEFERRED';
    const entryUnknown=job.workflow?.state==='confirmation_required';
    const uploadState=uploaded?['완료','good']:job.state==='upload_confirmation_required'?['접수 확인 필요','warn']:['접수됨',''];
    const refineState=canonical?['완료','good']:bad(r.state)||bad(cpu.state)?['확인 필요','bad']:entryUnknown||r.state==='confirmation_required'||cpu.delivery_state==='confirmation_required'?['접수 확인 필요','warn']:['waiting','running','awaiting_execution'].includes(r.state)||['running','awaiting_execution'].includes(cpu.state)?['진행 중','']:['대기',''];
    const indexState=ix.state==='activated'?['완료','good']:paused?['GPU 사용 대기','warn']:bad(ix.state)||bad(flow.state)?['확인 필요','bad']:['confirmation_required','activation_confirmation_required'].includes(ix.state)||flow.delivery_state==='confirmation_required'?['접수 확인 필요','warn']:ix.state==='index_ready'?['적용 대기','warn']:['waiting','running','awaiting_execution'].includes(ix.state)||['running','awaiting_execution'].includes(flow.state)?['진행 중','']:['대기',''];
    const errorCode=[job.error_code,job.workflow?.error_code,r.error_code,cpu.error_code,ix.error_code,flow.error_code].find(Boolean);
    let label,kind,help;
    if (!uploaded) {label=uploadState[0];kind=uploadState[1];help='접수 여부를 확인합니다. 이 화면의 조회는 파일을 다시 전송하지 않습니다.';}
    else if (refineState[1]==='bad' || indexState[1]==='bad') {label='확인 필요';kind='bad';help='처리 중 확인이 필요한 문제가 있습니다. 상태를 다시 조회하고, 계속되면 관리자에게 자세한 확인 정보를 전달하세요.';}
    else if (entryUnknown || refineState[0]==='접수 확인 필요' || indexState[0]==='접수 확인 필요') {label='접수 확인 필요';kind='warn';help='처리 요청의 결과를 확인하는 중입니다. 조회만으로 같은 작업을 다시 실행하지 않습니다.';}
    else if (paused) {label='GPU 사용 대기';kind='warn';help='내용 정리 이후 검색 준비가 보류돼 있습니다. GPU 사용이 가능해지면 관리자가 준비를 이어갑니다.';}
    else if (ix.state==='activated') {label='검색 자료 준비됨';kind='good';help='검색 자료의 적용을 확인했습니다. 질문 가능 여부는 자료함의 질문 준비 상태에서 별도로 확인합니다.';}
    else if (canonical) {label='검색 준비 '+indexState[0];kind=indexState[1];help='문서의 내용 정리를 완료했습니다. 검색 준비와 질문 연결을 확인한 뒤 사용할 수 있습니다.';}
    else {label='내용 정리 '+refineState[0];kind=refineState[1];help='업로드 접수를 확인했습니다. 내용 정리와 검색 준비는 각각 완료 상태를 확인합니다.';}
    return {items:[['원본 업로드',...uploadState],['내용 정리',...refineState],['검색 준비',...indexState]],label,kind,help,errorCode};
  }
  function renderList() {
    const query=$('search').value.trim().toLocaleLowerCase(), visible=state.jobs.filter(j=>(j.name+' '+j.filename).toLocaleLowerCase().includes(query));
    $('list-count').textContent=String(state.jobs.length);$('documents').replaceChildren();
    if (!visible.length) {$('documents').append(node('p',query?'찾는 문서가 없습니다.':'아직 등록한 문서가 없습니다. 첫 문서를 올려보세요.','empty'));return;}
    for (const job of [...visible].reverse()) {
      const row=node('article',undefined,'document-row'), content=node('div'), status=stages(job);
      row.append(node('span',job.filename.split('.').pop().toUpperCase(),'document-icon'));
      const title=button(job.name,()=>{state.selected=job.job_id;renderDetail();$('detail-panel').scrollIntoView({behavior:'smooth',block:'nearest'});},'document-title');
      title.setAttribute('aria-label',job.name+' 준비 상태 보기');content.append(title,node('p',job.filename,'small muted'),badge(status.label,status.kind));
      row.append(content,node('span',size(job.size_bytes),'small muted'));$('documents').append(row);
    }
  }
  function renderDetail() {
    const job=state.jobs.find(j=>j.job_id===state.selected);$('detail-panel').hidden=!job;if(!job)return;
    const status=stages(job);$('detail-name').textContent=job.name;$('detail-file').textContent=job.filename+' · '+size(job.size_bytes);
    $('detail-steps').replaceChildren();for(const [label,text,kind] of status.items){const item=node('li');item.append(node('strong',label),badge(text,kind));$('detail-steps').append(item);}
    $('detail-help').textContent=status.help;$('detail-info').replaceChildren();
    for(const [key,value] of [['등록 시각',stamp(job.created_at)],['작업 ID',job.job_id],['마지막 변경',stamp(job.updated_at)],['확인 코드',status.errorCode || '없음']]) $('detail-info').append(node('dt',key),node('dd',value));
    renderRecovery(job);
  }
  function renderRecovery(job) {
    $('recovery-actions').replaceChildren();state.recoveryButtons=[];
    let plan;try {plan=actionPlan(job);}catch {state.review=null;$('recovery-help').textContent='실행 가능한 동작을 확인할 수 없습니다. 상태를 다시 조회하거나 관리자에게 확인을 요청하세요.';controls();return;}
    $('recovery-help').textContent=state.recoveryError?messages.RECOVERY_RECORD_INVALID:state.pendingAction?messages.RECOVERY_PENDING:recovery.reason(plan.reason);
    if(state.review && (state.review.job_id!==job.job_id || state.review.revision!==plan.revision))state.review=null;
    for(const item of plan.actions) {
      const element=button(item.title,()=>action(async()=>{
        if(!actionAllowed(job,item))throw Error('RECOVERY_PENDING');
        const fresh=jobChecked(await api('/api/jobs/'+job.job_id)),current=actionPlan(fresh);
        state.jobs=state.jobs.map(j=>j.job_id===fresh.job_id?fresh:j);renderList();renderDetail();
        if(current.revision!==plan.revision || !current.actions.some(a=>a.id===item.id))throw Error('RECOVERY_STATE_CHANGED');
        state.review={schema:1,job_id:job.job_id,library_id:state.library.library_id,action:item.id,revision:current.revision};
        $('recovery-title').textContent=job.name+' · '+item.title;$('recovery-description').textContent=item.detail;controls();
      }));
      $('recovery-actions').append(element);state.recoveryButtons.push({element,job,item});
    }
    controls();
  }
  function render() {
    const library=state.library;if(!library)return;
    $('library-name').textContent=library.name;$('document-count').textContent=String(library.reserved_documents);
    $('question-state').textContent=library.question_ready?'질문 가능':'준비 전';$('question-state').classList.toggle('good',library.question_ready);
    $('question-help').textContent=library.question_ready?'질문할 수 있는 자료가 연결돼 있습니다.':'검색 자료와 답변 연결을 확인한 뒤 질문할 수 있습니다.';
    $('checked-at').textContent=state.checked?new Date(state.checked).toLocaleTimeString('ko-KR',{hour12:false}):'—';
    renderList();renderDetail();age();controls();
  }
  function age() {
    const stale=state.queryFailed || Date.now()-state.checked>20000;
    $('freshness').textContent=stale?'이전 조회 결과입니다. 상태를 다시 확인하세요.':'5초마다 등록된 작업을 조회합니다.';
    $('freshness').classList.toggle('stale',stale);
  }
  async function refresh() {
    const epoch=state.epoch;
    try {
      const [library,result]=await Promise.all([api('/api/library'),api('/api/jobs')]);
      libraryChecked(library);if(!result || !Array.isArray(result.items) || result.items.length>20)throw Error('RESPONSE_INVALID');
      const jobs=result.items.map(jobChecked);if(new Set(jobs.map(j=>j.job_id)).size!==jobs.length)throw Error('RESPONSE_INVALID');
      state.library=library;state.jobs=jobs;state.checked=Date.now();state.queryFailed=false;reconcileRecovery();render();
      await questionScreen?.refresh();
    }catch(error){if(epoch===state.epoch){state.queryFailed=true;age();controls();}throw error;}
  }
  function recovered(job) {
    jobChecked(job);if(!upload.matches(state.pending,job))throw Error('RESPONSE_INVALID');
    state.selected=job.job_id;clearPending();controls();return job;
  }
  async function lookupPending() {
    if(!state.pending)return null;
    if(state.pending.library_id!==state.library.library_id)throw Error('PENDING_LIBRARY_CHANGED');
    try {return recovered(await api('/api/upload-requests/'+state.pending.key));}
    catch(error){if(error.status===404 && error.code==='REQUEST_NOT_FOUND')return null;throw error;}
  }
  async function action(task) {
    if(state.busy)return;const epoch=state.epoch;state.busy=true;controls();
    try {await task(epoch);}catch(error){if(epoch===state.epoch)notice(readable(error),true);}
    finally {if(epoch===state.epoch){state.busy=false;controls();}}
  }
  $('login-form').addEventListener('submit',async event=>{
    event.preventDefault();if($('login-submit').disabled)return;
    const token=$('token').value.trim();if(!/^[A-Za-z0-9_-]{43}$/.test(token)){notice(messages.AUTH_REQUIRED,true);return;}
    state.epoch++;state.token=token;$('token').value='';$('login-submit').disabled=true;const epoch=state.epoch;
    try {
      await refresh();if(epoch!==state.epoch)return;
      notice();
      await action(async()=>{await questionScreen?.connected();});if(epoch!==state.epoch)return;
      $('login-panel').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;
      $('connection-state').textContent='연결됨';$('connection-state').classList.add('connected');loadPending();loadRecovery();reconcileRecovery();render();
      if(state.pending)await action(async()=>{const job=await lookupPending();notice(job?'이전 업로드의 접수를 확인했습니다.':'이전 업로드가 아직 조회되지 않습니다. 접수 여부를 다시 확인하거나 같은 파일을 선택하세요.');if(job)await refresh();});
      else if(state.pendingError)notice(messages.PENDING_RECORD_INVALID,true);
      if(state.recoveryError)notice(messages.RECOVERY_RECORD_INVALID,true);
    }catch(error){if(epoch===state.epoch){disconnect();notice(readable(error),true);}}
    finally {if(epoch===state.epoch)$('login-submit').disabled=false;}
  });
  $('logout').addEventListener('click',()=>{disconnect();notice('연결을 해제했습니다. 접수한 작업은 자료함에 유지됩니다.');});
  $('file').addEventListener('change',()=>{
    const file=$('file').files[0];if(!file){$('file-description').textContent='파일당 32MiB까지 올릴 수 있습니다.';return;}
    $('document-name').value=state.pending?.name || file.name.replace(/\.(txt|md|markdown|pdf)$/i,'').slice(0,200);
    $('file-description').textContent=file.name+' · '+size(file.size);
  });
  $('search').addEventListener('input',renderList);
  $('refresh').addEventListener('click',()=>action(async()=>{await refresh();notice('자료함 상태를 다시 확인했습니다.');}));
  $('refresh-job').addEventListener('click',()=>action(async()=>{
    const job=jobChecked(await api('/api/jobs/'+state.selected));state.jobs=state.jobs.map(value=>value.job_id===job.job_id?job:value);
    reconcileRecovery();renderList();renderDetail();notice('이 문서의 상태를 다시 확인했습니다.');
  }));
  $('recover-action').addEventListener('click',()=>action(async()=>{
    if(!state.pendingAction)return;
    if(state.pendingAction.library_id!==state.library.library_id)throw Error('RECOVERY_LIBRARY_CHANGED');
    const job=jobChecked(await api('/api/jobs/'+state.pendingAction.job_id));actionPlan(job);
    state.selected=job.job_id;state.jobs=state.jobs.map(j=>j.job_id===job.job_id?job:j);reconcileRecovery();render();
    notice(state.pendingAction?'동작의 상태가 아직 바뀌지 않았습니다. 최신 상태에서 같은 동작을 명시적으로 다시 선택할 수 있습니다.':'문서의 현재 상태를 확인했습니다. 완료 여부는 준비 단계에서 확인하세요.');
  }));
  $('recovery-cancel').addEventListener('click',()=>{state.review=null;controls();});
  $('recovery-confirm').addEventListener('click',()=>{
    if($('recovery-confirm').disabled)return;
    action(async(epoch)=>{
      const record=recovery.pending(state.review);if(record.library_id!==state.library.library_id)throw Error('RECOVERY_LIBRARY_CHANGED');
      const job=jobChecked(await api('/api/jobs/'+record.job_id)),plan=actionPlan(job);
      state.jobs=state.jobs.map(j=>j.job_id===job.job_id?job:j);renderList();renderDetail();
      const item=plan.actions.find(a=>a.id===record.action);
      if(plan.revision!==record.revision || !item || !actionAllowed(job,item))throw Error('RECOVERY_STATE_CHANGED');
      if(epoch!==state.epoch)return;
      saveRecovery(record);state.review=null;controls();
      try {
        const response=await api('/api/jobs/'+record.job_id+'/recover',{
          body:JSON.stringify({action:record.action,revision:record.revision}),headers:{'Content-Type':'application/json'}});
        if(response.action!==record.action || response.job?.job_id!==record.job_id || response.inference_requested!==false)throw Error('RECOVERY_RESPONSE_INVALID');
        jobChecked(response.job);actionPlan(response.job);clearRecovery();
        state.jobs=state.jobs.map(j=>j.job_id===record.job_id?response.job:j);render();
      }catch(error){
        if(epoch===state.epoch && [400,409].includes(error.status) && ['RECOVERY_BODY_INVALID','RECOVERY_STATE_CHANGED','RECOVERY_ACTION_NOT_ALLOWED'].includes(error.code))clearRecovery();
        throw error;
      }
      await refresh();notice('선택한 동작의 응답을 확인했습니다. 완료 여부는 준비 단계에서 확인하세요.');
    });
  });
  $('recover-upload').addEventListener('click',()=>action(async()=>{
    const job=await lookupPending();notice(job?'접수를 확인했습니다. 같은 파일을 다시 올릴 필요가 없습니다.':'아직 접수가 조회되지 않습니다. 새 파일을 올리지 말고 같은 파일로 확인하세요.');if(job)await refresh();
  }));
  $('upload-form').addEventListener('submit',event=>{
    event.preventDefault();if($('upload-submit').disabled)return;
    action(async(epoch)=>{
      await refresh();if(state.pendingError)throw Error('PENDING_RECORD_INVALID');
      if(state.pending){const prior=await lookupPending();if(prior){await refresh();notice('기존 업로드를 확인했습니다. 파일은 다시 전송하지 않았습니다.');return;}}
      const file=$('file').files[0], meta=upload.metadata($('document-name').value,file);
      if(!state.pending && state.library.reserved_documents>=20)throw Error('DOCUMENT_CAPACITY_EXCEEDED');
      notice('파일을 확인하고 접수하고 있습니다.');const sha256=await upload.sha256(new Uint8Array(await file.arrayBuffer()));
      if(epoch!==state.epoch)return;
      const record={schema:1,key:state.pending?.key || upload.uuid(),library_id:state.library.library_id,...meta,sha256};
      if(state.pending && !upload.matches(state.pending,record))throw Error('PENDING_FILE_CHANGED');
      savePending(record);controls();const body=new FormData();body.append('name',record.name);body.append('file',file,file.name);
      try {recovered(await api('/api/uploads',{body,headers:{'Idempotency-Key':record.key}}));}
      catch(error){
        const intakeCodes=['UPLOAD_NAME_INVALID','UPLOAD_METADATA_INVALID','UPLOAD_FILENAME_INVALID','UPLOAD_BYTES_INVALID',
          'UPLOAD_FORMAT_UNSUPPORTED','UPLOAD_FORMAT_MISMATCH','UPLOAD_TEXT_NOT_UTF8','MULTIPART_INVALID','UPLOAD_REQUEST_TOO_LARGE'];
        if(epoch===state.epoch && [400,413].includes(error.status) && intakeCodes.includes(error.code))clearPending();
        throw error;
      }
      $('file').value='';$('document-name').value='';$('file-description').textContent='파일당 32MiB까지 올릴 수 있습니다.';
      await refresh();notice('문서 접수를 확인했습니다. 준비 과정은 아래에서 확인하세요.');
    });
  });
  questionScreen=window.QAQuestions.mount({api,run:action,node,button,notice,readable,
    context:()=>({connected:Boolean(state.token && state.library),busy:state.busy,epoch:state.epoch,
      queryFailed:state.queryFailed,library:state.library,jobs:state.jobs}),
    selectDocument:id=>{state.selected=id;renderDetail();$('detail-panel').scrollIntoView({behavior:'smooth',block:'nearest'});}});
  setInterval(()=>{age();if(!state.token || !state.library || state.busy || document.hidden)return;
    action(async()=>{try{await refresh();}catch(error){state.queryFailed=true;age();throw error;}});
  },5000);
  window.addEventListener('pagehide',()=>{disconnect();});
  controls();
})();
