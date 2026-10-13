'use strict';
(() => {
  const U=window.QAUpload, STORE='document-qa.q08.pending-question', MAX_BYTES=8192;
  const states=['pending','accepted','waiting','confirmation_required','attention_required','failed','complete'];
  const warnings={CONTEXT_SOURCES_EXCLUDED:'일부 자료는 답변에 포함되지 않았습니다.',ANSWER_TRUNCATED:'답변이 길이 제한으로 끝났습니다. 출처를 함께 확인하세요.',CHECK_SOURCE_QUALITY:'자료의 품질을 확인하세요.'};
  const labels={pending:'실행 전',accepted:'답변 준비 중',waiting:'답변 준비 중',confirmation_required:'실행 접수 확인 필요',attention_required:'결과 확인 필요',failed:'답변 실패',complete:'답변 완료'};
  const messages={QUESTION_INVALID:'질문 내용을 입력하세요.',QUESTION_TOO_LARGE:'질문은 UTF-8 기준 8KiB까지 입력할 수 있습니다.',
    QUESTION_HEAD_NOT_ACTIVE:'질문 자료가 아직 준비되지 않았습니다.',QUESTION_LIMIT_EXCEEDED:'질문 기록 한도에 도달했습니다. 관리자에게 확인하세요.',
    QUESTION_CLIENT_KEY_CONFLICT:'이전 질문과 내용이 다릅니다. 원래 질문부터 확인하세요.',QA_AGENT_INFERENCE_NOT_AUTHORIZED:'답변 실행이 허용되지 않았습니다. 관리자 준비를 기다리세요.',
    QUESTION_PENDING_INVALID:'이 탭의 질문 확인 기록을 읽을 수 없습니다. 관리자 확인이 필요합니다.',QUESTION_STORE_UNAVAILABLE:'질문 확인 기록을 저장할 수 없습니다. 브라우저 저장 설정을 확인하세요.',
    QUESTION_LIBRARY_CHANGED:'자료함이 변경됐습니다. 이전 질문의 자료함에서 먼저 확인하세요.',QUESTION_RESPONSE_INVALID:'질문 또는 답변의 확인 정보를 읽을 수 없습니다. 상태를 다시 확인하세요.',
    QUESTION_POLICY_UNAVAILABLE:'질문 연결을 확인할 수 없습니다. 자료함 상태를 다시 확인하세요.',QUESTION_STATE_CHANGED:'질문 상태가 변경됐습니다. 현재 상태에서 필요한 동작을 다시 선택하세요.',
    QUESTION_PENDING:'이전 질문의 상태를 먼저 확인하세요. 새로운 질문을 자동으로 실행하지 않습니다.'};
  function fail(code='QUESTION_RESPONSE_INVALID'){throw Object.assign(Error(code),{code});}
  // Server references use canonical nonzero UUIDs, including deterministic v5
  // question/target IDs. Browser request keys still use QAUpload's v4 generator.
  const serverId=value=>typeof value==='string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value) && value!=='00000000-0000-0000-0000-000000000000';
  function text(value,max=MAX_BYTES){return typeof value==='string' && !value.includes('\0') && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?:^|[^\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value) && new TextEncoder().encode(value).length<=max;}
  function input(value){if(!text(value,1<<20) || !value.trim())fail('QUESTION_INVALID');if(new TextEncoder().encode(value).length>MAX_BYTES)fail('QUESTION_TOO_LARGE');return value;}
  function policy(v){
    if(!v || !['question_ready','question_inference_enabled','question_API_connected','head_active'].every(k=>typeof v[k]==='boolean')
      || !Number.isSafeInteger(v.head_revision) || v.head_revision<0 || !(v.head_target_id===null || serverId(v.head_target_id))
      || v.policy_source!=='explicit_server_opt_in' || v.head_active && (!v.head_revision || !serverId(v.head_target_id))
      || v.question_ready!==Boolean(v.head_active && v.question_inference_enabled) || v.question_ready && !v.question_API_connected)fail('QUESTION_POLICY_UNAVAILABLE');
    return v;
  }
  function result(v){
    if(!v || !text(v.answer,65536) || !v.answer.trim() || !['answered','no_evidence'].includes(v.outcome)
      || !Array.isArray(v.citations) || v.citations.length>50 || !Array.isArray(v.warnings) || new Set(v.warnings).size!==v.warnings.length
      || !v.warnings.every(w=>Object.hasOwn(warnings,w)) || typeof v.answer_truncated!=='boolean'
      || v.answer_truncated!==v.warnings.includes('ANSWER_TRUNCATED') || new TextEncoder().encode(JSON.stringify(v)).length>512*1024)fail();
    const ids=[];
    for(const c of v.citations){
      const s=c?.source;
      if(!c || !/^S[1-9][0-9]*$/.test(c.citation_id) || !s || !serverId(s.canonical_id) || !serverId(s.source_document_id)
        || !/^[0-9a-f]{64}$/.test(s.chunk_id) || !text(s.text,512*1024) || !s.text.trim()
        || !Array.isArray(s.source_spans) || !s.source_spans.length || s.source_spans.length>1000
        || !s.source_spans.every(span=>span && text(span.block_id,1024) && span.block_id.length>0))fail();
      ids.push(c.citation_id);
    }
    const refs=[...v.answer.matchAll(/\[S([^\]\r\n]*)\]/g)].map(m=>'S'+m[1]);
    if(new Set(ids).size!==ids.length || !refs.every(r=>ids.includes(r)) || !ids.every(id=>refs.includes(id)))fail();
    if(v.outcome==='no_evidence'){
      if(ids.length || refs.length || v.answer_truncated || v.answer!=='선택한 자료에서 질문에 답할 충분한 근거를 찾지 못했습니다.')fail();
    }else if(!ids.length)fail();
    return v;
  }
  function question(v){
    if(!v || !serverId(v.question_id) || !text(v.question) || !v.question.trim() || !serverId(v.target_id) || !serverId(v.agent_version_id)
      || !Number.isSafeInteger(v.head_revision) || v.head_revision<1 || !states.includes(v.state)
      || !Number.isInteger(v.attempts) || v.attempts<0 || v.attempts>16 || !(v.agent_run_id===null || serverId(v.agent_run_id))
      || !(v.error_code===null || typeof v.error_code==='string' && /^[A-Z][A-Z0-9_]{0,99}$/.test(v.error_code)))fail();
    if(v.state==='complete'){if(!serverId(v.agent_run_id))fail();result(v.result);}else if(v.result!==undefined)fail();
    return v;
  }
  function pending(v){
    if(!v || v.schema!==1 || !serverId(v.library_id))fail('QUESTION_PENDING_INVALID');
    const keys=v.kind==='accept'?['schema','kind','library_id','key','question']:['schema','kind','library_id','question_id','operation'];
    if(Object.keys(v).length!==keys.length || !keys.every(k=>Object.hasOwn(v,k)))fail('QUESTION_PENDING_INVALID');
    if(v.kind==='accept'){if(!U.uid(v.key))fail('QUESTION_PENDING_INVALID');try{input(v.question);}catch{fail('QUESTION_PENDING_INVALID');}}
    else if(v.kind!=='action' || !serverId(v.question_id) || !['dispatch','retry','observe'].includes(v.operation))fail('QUESTION_PENDING_INVALID');
    return v;
  }
  function operation(q,p){
    if(!p?.question_API_connected || q.state==='complete' || q.state==='failed')return null;
    if(q.agent_run_id)return 'observe';
    if(!p.question_inference_enabled || q.attempts>=16)return null;
    if(q.state==='pending')return q.attempts?'retry':'dispatch';
    if(q.state==='confirmation_required')return 'retry';
    return null;
  }
  function same(a,b){if(a.question_id!==b.question_id || a.question!==b.question || a.target_id!==b.target_id || a.head_revision!==b.head_revision || a.agent_version_id!==b.agent_version_id)fail();return b;}
  function location(span){
    const parts=[],page=span.metadata?.location?.page;
    if(Number.isSafeInteger(page) && page>0)parts.push(page+'페이지');
    parts.push('블록 '+span.block_id);
    if(Number.isSafeInteger(span.start) && span.start>=0 && Number.isSafeInteger(span.end) && span.end>=span.start)
      parts.push('문자 위치 '+span.start+'–'+span.end+' (0부터, 끝 제외)');
    return parts.join(' · ');
  }
  function mount(host){
    const $=id=>document.getElementById(id),s={policy:null,items:[],selected:null,pending:null,broken:false,stale:true,review:null,buttons:[]};
    function report(e){host.notice(messages[e.code||e.message] || host.readable(e),true);}
    function run(task){return host.run(async epoch=>{try{await task(epoch);}catch(e){if(host.context().epoch===epoch)report(e);}});}
    function load(){s.pending=null;s.broken=false;try{const raw=sessionStorage.getItem(STORE);if(raw!==null)s.pending=pending(JSON.parse(raw));}catch{s.broken=true;report(Error('QUESTION_PENDING_INVALID'));}}
    function save(v){try{const raw=JSON.stringify(pending(v));sessionStorage.setItem(STORE,raw);if(sessionStorage.getItem(STORE)!==raw)throw Error();}catch{fail('QUESTION_STORE_UNAVAILABLE');}s.pending=v;controls();}
    function clear(){try{sessionStorage.removeItem(STORE);if(sessionStorage.getItem(STORE)!==null)throw Error();}catch{fail('QUESTION_STORE_UNAVAILABLE');}s.pending=null;controls();}
    function allowed(q){const c=host.context();return !s.broken && (!s.pending || s.pending.library_id===c.library?.library_id && s.pending.kind==='action' && s.pending.question_id===q.question_id);}
    function controls(){
      const c=host.context(),ready=c.connected && !c.busy && !c.queryFailed && !s.stale && s.policy?.question_ready;
      $('question-submit').disabled=!ready || s.broken || Boolean(s.pending && (s.pending.kind!=='accept' || s.pending.library_id!==c.library?.library_id));
      $('question-submit').textContent=s.pending?.kind==='accept'?'같은 질문 접수 다시 확인':'질문하기';
      $('question-input').disabled=c.busy || Boolean(s.pending) || s.broken;
      $('question-recover').disabled=!c.connected || c.busy || s.broken || !s.pending;
      $('question-pending').hidden=!s.pending;
      if(s.pending)$('question-pending-text').textContent=s.pending.kind==='accept'?s.pending.question:'답변 동작의 응답을 확인하지 못했습니다. 같은 질문의 상태부터 확인하세요.';
      for(const {element,q} of s.buttons)element.disabled=!c.connected || c.busy || c.queryFailed || s.stale || !allowed(q);
      $('question-review').hidden=!s.review;
      $('question-confirm').disabled=!c.connected || c.busy || c.queryFailed || s.stale || !s.review;
      $('question-cancel').disabled=c.busy;
    }
    function upsert(q){question(q);s.items=s.items.filter(v=>v.question_id!==q.question_id);s.items.push(q);s.selected=q.question_id;render();return q;}
    function render(){
      const p=s.policy,c=host.context();
      $('qa-status').textContent=s.stale?'확인 필요':p?.question_ready?'질문 가능':'준비 전';
      $('qa-status').classList.toggle('good',!s.stale && Boolean(p?.question_ready));
      $('qa-help').textContent=s.stale?'질문 연결을 확인할 수 없습니다. 자료함 상태를 다시 확인하세요.':p?.question_ready?'현재 준비된 자료를 기준으로 답변합니다. 출처의 원문을 함께 확인하세요.':'자료의 검색 준비와 답변 연결을 마치면 질문할 수 있습니다. 이전 질문 기록은 그대로 유지됩니다.';
      $('question-state').textContent=s.stale?'확인 필요':p?.question_ready?'질문 가능':'준비 전';$('question-state').classList.toggle('good',!s.stale && Boolean(p?.question_ready));$('question-help').textContent=$('qa-help').textContent;
      $('question-list').replaceChildren();
      if(!s.items.length)$('question-list').append(host.node('p',s.stale?'질문 기록을 확인하지 못했습니다.':'아직 질문 기록이 없습니다.','empty'));
      for(const q of [...s.items].reverse()){
        const row=host.node('article',undefined,'question-row'),b=host.button(q.question,()=>{s.selected=q.question_id;s.review=null;render();},'document-title');
        row.append(b,host.node('span',q.state==='complete' && q.result.outcome==='no_evidence'?'근거를 찾지 못함':labels[q.state],'badge '+(q.state==='failed'?'bad':'')));$('question-list').append(row);
      }
      const q=s.items.find(v=>v.question_id===s.selected);$('answer-panel').hidden=!q;s.buttons=[];$('answer-actions').replaceChildren();
      if(!q){s.review=null;controls();return;}
      $('answer-question').textContent=q.question;$('answer-state').textContent=q.state==='complete' && q.result.outcome==='no_evidence'?'근거를 찾지 못함':labels[q.state];
      $('answer-state').className='badge '+(q.state==='failed'?'bad':q.state==='complete'?'good':'warn');
      $('answer-help').textContent=s.stale?'이전 조회 결과입니다. 상태를 다시 확인하세요.':q.state==='complete'?'질문을 접수했을 때의 자료로 생성한 답변입니다. 출처와 내용을 확인하세요.':q.state==='failed'?'답변을 완료하지 못했습니다. 새 실행으로 바꾸지 않고 실패 기록을 유지합니다.':q.agent_run_id?'답변 진행 확인을 누르면 기존 실행의 결과를 조회합니다. 새 답변을 생성하지 않습니다.':'실행 접수 상태를 확인한 뒤 필요한 동작을 선택하세요.';
      const op=operation(q,p);
      if(op){const b=host.button(op==='observe'?'답변 진행 확인':op==='retry'?'같은 질문 실행 접수 확인':'답변 생성하기',()=>run(async()=>{
        if(!allowed(q))fail('QUESTION_PENDING');const fresh=question(await host.api('/api/questions/'+q.question_id));
        same(q,fresh);if(operation(fresh,s.policy)!==op) {upsert(fresh);fail('QUESTION_STATE_CHANGED');}
        s.review={question_id:q.question_id,operation:op,library_id:c.library.library_id};
        $('question-review-title').textContent=op==='observe'?'기존 답변의 진행 상태를 확인합니다.':op==='retry'?'같은 질문의 실행 접수를 다시 확인합니다.':'이 질문의 답변을 생성합니다.';
        $('question-review-help').textContent=op==='observe'?'기존 답변 실행만 조회합니다.':op==='retry'?'원래 질문과 실행 요청을 유지합니다. 새로운 질문을 만들지 않습니다.':'접수 당시 자료로 답변을 한 번 요청합니다.';controls();
      }));$('answer-actions').append(b);s.buttons.push({element:b,q});}
      $('answer-text').replaceChildren();$('answer-warnings').replaceChildren();$('answer-links').replaceChildren();$('answer-sources').replaceChildren();$('answer-info').replaceChildren();
      for(const [label,value] of [['질문 ID',q.question_id],['질문 당시 자료 버전',q.head_revision],['질문 대상',q.target_id],['실행 ID',q.agent_run_id || '아직 없음'],['확인 코드',q.error_code || '없음']])$('answer-info').append(host.node('dt',label),host.node('dd',value));
      if(q.state==='complete'){
        const r=q.result;$('answer-text').textContent=r.answer;
        for(const w of r.warnings)$('answer-warnings').append(host.node('p',warnings[w],'notice'));
        if(r.outcome==='no_evidence')$('answer-warnings').append(host.node('p','선택한 자료에서 답변할 근거가 부족합니다. 질문을 구체적으로 바꾸거나 다른 자료를 준비하세요.','small muted'));
        for(const citation of r.citations){
          const source=citation.source,id='qa-source-'+q.question_id+'-'+citation.citation_id;
          const a=host.node('a','['+citation.citation_id+'] 원문 보기');a.setAttribute('href','#'+id);$('answer-links').append(a);
          const box=host.node('article',undefined,'source-card');box.setAttribute('id',id);
          const jobs=c.jobs.filter(job=>job.source_document_id===source.source_document_id),job=jobs.length===1?jobs[0]:null;
          box.append(host.node('h4','['+citation.citation_id+'] '+(job?.name || '질문 당시 자료')));
          for(const span of source.source_spans)box.append(host.node('p',location(span),'small muted'));
          box.append(host.node('blockquote',source.text,'source-text'));
          if(job)box.append(host.button('이 문서의 준비 상태 보기',()=>host.selectDocument(job.job_id)));
          const detail=host.node('details'),summary=host.node('summary','출처 확인 정보'),info=host.node('dl');
          for(const [label,value] of [['원본 자료 ID',source.source_document_id],['정제 자료 ID',source.canonical_id],['자료 조각 ID',source.chunk_id]])info.append(host.node('dt',label),host.node('dd',value));
          detail.append(summary,info);box.append(detail);$('answer-sources').append(box);
        }
      }
      controls();
    }
    async function refresh(){
      const epoch=host.context().epoch;
      try{
        const [p,list]=await Promise.all([host.api('/api/question-policy'),host.api('/api/questions')]);policy(p);
        if(!list || !Array.isArray(list.items) || list.items.length>100)fail();const items=list.items.map(question);
        if(new Set(items.map(q=>q.question_id)).size!==items.length)fail();
        if(host.context().epoch!==epoch)return;
        s.policy=p;s.items=items;s.stale=false;
        if(s.pending?.kind==='action' && s.pending.library_id===host.context().library?.library_id){
          const q=items.find(q=>q.question_id===s.pending.question_id);
          if(q && (q.state==='complete' || q.state==='failed' || s.pending.operation!=='observe' && q.agent_run_id))clear();
        }
        if(s.review && !items.some(q=>q.question_id===s.review.question_id && operation(q,p)===s.review.operation))s.review=null;render();
      }catch(e){if(host.context().epoch!==epoch)return;s.stale=true;s.review=null;render();if(e.status!==503)report(e);}
    }
    async function lookup(){
      if(!s.pending)return null;const record=pending(s.pending),c=host.context();if(record.library_id!==c.library?.library_id)fail('QUESTION_LIBRARY_CHANGED');
      let q;
      try{q=question(await host.api(record.kind==='accept'?'/api/question-requests/'+record.key:'/api/questions/'+record.question_id));}
      catch(e){if(record.kind==='accept' && e.status===404 && e.code==='QUESTION_NOT_FOUND')return null;throw e;}
      if(record.kind==='accept' && q.question!==record.question || record.kind==='action' && q.question_id!==record.question_id)fail();
      upsert(q);
      if(record.kind==='accept' || q.state==='complete' || q.state==='failed' || record.operation!=='observe' && q.agent_run_id)clear();
      return q;
    }
    async function execute(q,op){
      save({schema:1,kind:'action',library_id:host.context().library.library_id,question_id:q.question_id,operation:op});
      const response=question(await host.api('/api/questions/'+q.question_id+'/'+op,{body:'{}',headers:{'Content-Type':'application/json'}}));
      same(q,response);
      upsert(response);clear();
    }
    $('question-form').addEventListener('submit',event=>{event.preventDefault();if($('question-submit').disabled)return;run(async epoch=>{
      if(s.broken)fail('QUESTION_PENDING_INVALID');const previous=s.pending;
      if(previous && previous.kind!=='accept')fail('QUESTION_PENDING');
      if(previous){const found=await lookup();if(found){host.notice('이전 질문의 접수를 확인했습니다. 필요한 답변 동작은 질문 기록에서 선택하세요.');return;}}
      const content=input(previous?.question || $('question-input').value),p=policy(await host.api('/api/question-policy'));s.policy=p;
      if(!p.question_ready)fail('QUESTION_HEAD_NOT_ACTIVE');if(host.context().epoch!==epoch)return;
      const record=previous || {schema:1,kind:'accept',library_id:host.context().library.library_id,key:U.uuid(),question:content};save(record);
      const q=question(await host.api('/api/questions',{body:JSON.stringify({question:content}),headers:{'Content-Type':'application/json','Idempotency-Key':record.key}}));
      if(q.question!==content)fail();upsert(q);clear();$('question-input').value='';
      // Only a newly submitted, explicitly requested question may continue to its first dispatch.
      if(!previous && q.state==='pending' && q.attempts===0 && !q.agent_run_id){
        const latest=policy(await host.api('/api/question-policy'));s.policy=latest;
        if(!latest.question_inference_enabled || !latest.question_API_connected)fail('QA_AGENT_INFERENCE_NOT_AUTHORIZED');
        const fresh=same(q,question(await host.api('/api/questions/'+q.question_id)));
        if(operation(fresh,latest)==='dispatch')await execute(fresh,'dispatch');else upsert(fresh);
      }
      host.notice('질문 상태를 확인했습니다. 답변 준비 중이면 답변 진행 확인을 눌러주세요.');
    });});
    $('question-recover').addEventListener('click',()=>run(async()=>{const q=await lookup();host.notice(q?'기존 질문의 현재 상태를 확인했습니다.':'아직 접수가 조회되지 않습니다. 같은 질문 접수 다시 확인으로 원래 요청을 확인할 수 있습니다.');render();}));
    $('question-cancel').addEventListener('click',()=>{s.review=null;controls();});
    $('question-confirm').addEventListener('click',()=>{if($('question-confirm').disabled)return;run(async epoch=>{
      const review={...s.review},p=policy(await host.api('/api/question-policy'));s.policy=p;
      if(review.library_id!==host.context().library?.library_id)fail('QUESTION_LIBRARY_CHANGED');
      const q=question(await host.api('/api/questions/'+review.question_id));
      if(!allowed(q) || operation(q,p)!==review.operation){s.review=null;upsert(q);fail('QUESTION_STATE_CHANGED');}
      if(host.context().epoch!==epoch)return;s.review=null;await execute(q,review.operation);host.notice('질문의 현재 상태를 확인했습니다.');
    });});
    $('question-input').addEventListener('input',()=>{$('question-size').textContent=new TextEncoder().encode($('question-input').value).length.toLocaleString('ko-KR')+' / 8,192 bytes';});
    function disconnect(){
      s.policy=null;s.items=[];s.selected=null;s.pending=null;s.broken=false;s.review=null;s.buttons=[];s.stale=true;
      $('question-input').value='';
      for(const id of ['answer-text','answer-sources','answer-links','answer-info','answer-warnings','answer-actions',
        'question-list','answer-question','answer-help','answer-state','question-pending-text','question-review-title','question-review-help'])$(id).replaceChildren();
      $('answer-panel').hidden=true;$('question-review').hidden=true;$('question-pending').hidden=true;
    }
    async function connected(){load();if(s.pending?.kind==='accept')$('question-input').value=s.pending.question;if(s.pending){try{await lookup();}catch(e){report(e);}}render();}
    controls();return {refresh,controls,disconnect,connected};
  }
  window.QAQuestions={mount,input,policy,question,result,pending,operation,location,STORE,MAX_BYTES};
})();
