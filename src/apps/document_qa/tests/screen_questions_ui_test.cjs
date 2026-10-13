// Q08 question flow in a DOM/fetch model, not a browser or a live Agent.
'use strict';
const assert=require('node:assert/strict');
const {boot}=require('./screen_ui_test.cjs');
const {randomUUID}=require('node:crypto');
const LIB='22222222-2222-4222-8222-222222222222',QID='33333333-3333-5333-8333-333333333333',TARGET='01851866-e7bd-5c5f-a405-2f949c22c60d',RUN='55555555-5555-4555-8555-555555555555',SOURCE='66666666-6666-4666-8666-666666666666',CANON='77777777-7777-4777-8777-777777777777',KEY='11111111-1111-4111-8111-111111111111';
const STORE='document-qa.q08.pending-question',clone=v=>structuredClone(v);
const policy=(extra={})=>({question_ready:true,question_inference_enabled:true,question_API_connected:true,head_active:true,head_revision:1,head_target_id:TARGET,policy_source:'explicit_server_opt_in',...extra});
const record=(extra={})=>({schema:1,kind:'accept',library_id:LIB,key:KEY,question:'결론은 무엇인가요?',...extra});
const source=()=>({chunk_id:'d'.repeat(64),canonical_id:CANON,source_document_id:SOURCE,text:'실제 출처 원문입니다.',source_spans:[{block_id:'paragraph-1',start:0,end:12,metadata:{location:{page:2}}}]});
const result=(extra={})=>({answer:'검증된 결론입니다. [S1]',outcome:'answered',citations:[{citation_id:'S1',source:source()}],warnings:[],answer_truncated:false,...extra});
const question=(extra={})=>({question_id:QID,question:'결론은 무엇인가요?',target_id:TARGET,head_revision:1,agent_version_id:TARGET,state:'pending',agent_run_id:null,attempts:0,error_code:null,question_ready:false,semantic_quality_verified:false,...extra});
const complete=(extra={})=>question({state:'complete',agent_run_id:RUN,attempts:1,result:result(),...extra});
function fixture(options={}){
  let p=policy(options.policy),qs=clone(options.questions||[]),loss=options.loss||'',reads=0;
  const keys=new Map(options.keys||[]);
  const a=boot({...options,library:{question_ready:p.question_ready,...options.library},onRequest:async(url,config,data)=>{
    const response=(value,status=200)=>({value,status});
    if(options.onRequest){const custom=await options.onRequest(url,config,{...data,p,qs,keys,reads});if(custom)return custom;}
    if(url==='/api/question-policy')return response(p);
    if(url==='/api/questions' && config.method==='GET')return response({items:qs});
    if(url.startsWith('/api/question-requests/')){const q=qs.find(q=>q.question_id===keys.get(url.split('/').pop()));return q?response(q):response({code:'QUESTION_NOT_FOUND'},404);}
    if(url==='/api/questions'){
      const saved=JSON.parse(data.storage.get(STORE)),body=JSON.parse(config.body),key=config.headers['Idempotency-Key'];
      assert.equal(saved.kind,'accept');assert.equal(saved.key,key);assert.equal(saved.question,body.question);assert.equal(saved.library_id,LIB);
      assert.equal(Object.keys(saved).length,5);assert.doesNotMatch(JSON.stringify(saved),/Bearer|token|credential/);
      if(loss==='accept-before'){loss='';throw Error('accept ack lost before commit');}
      if(!p.question_ready)return response({code:'QUESTION_HEAD_NOT_ACTIVE'},409);
      let q=qs.find(q=>q.question_id===keys.get(key));
      if(!q){q=question({question_id:randomUUID().replace(/^(........-....-)4/,'$15'),question:body.question,head_revision:p.head_revision,target_id:p.head_target_id});keys.set(key,q.question_id);qs.push(q);}
      if(loss==='accept-after'){loss='';throw Error('accept ack lost after commit');}
      return response(q,202);
    }
    if(url.startsWith('/api/questions/')){
      const parts=url.split('/'),q=qs.find(q=>q.question_id===parts[3]);if(!q)return response({code:'QUESTION_NOT_FOUND'},404);
      if(config.method==='GET'){reads++;if(options.onRead)await options.onRead(reads,{p,qs,q,data});return response(q);}
      const op=parts[4],saved=JSON.parse(data.storage.get(STORE));assert.equal(saved.kind,'action');assert.equal(saved.question_id,q.question_id);assert.equal(saved.operation,op);assert.equal(config.body,'{}');
      if(loss===op+'-before'){loss='';throw Error('run ack lost before execution');}
      if(op==='observe'){
        if(options.finishOnObserve){q.state='complete';q.result=result();}
      }else{
        if(!p.question_inference_enabled)return response({code:'QA_AGENT_INFERENCE_NOT_AUTHORIZED'},409);
        if(options.unknownRun){q.state='confirmation_required';q.attempts++;q.error_code='QA_AGENT_OUTCOME_UNKNOWN';}
        else if(!q.agent_run_id){q.state='waiting';q.agent_run_id=RUN;q.attempts++;}
      }
      if(loss===op+'-after'){loss='';throw Error('run ack lost after execution');}
      return response(q,op==='observe'?200:202);
    }
  }});
  return Object.assign(a,{qaSnapshot:()=>clone(qs),policyChange:v=>Object.assign(p,v),qaChange:(id,v)=>Object.assign(qs.find(q=>q.question_id===id),v),lossQA:v=>loss=v});
}
const posts=a=>a.requests.filter(r=>r.method==='POST');
async function ask(a,content='결론은 무엇인가요?'){a.elements['question-input'].value=content;await a.event('question-form','submit');}
async function select(a,index=0){await a.elements['question-list'].children[index].children[0].listeners.click();await a.settle();}
async function choose(a){const b=a.elements['answer-actions'].children[0];if(b && !b.disabled)await b.listeners.click();await a.settle();}
async function run(){let checks=0,a;
  a=fixture();assert.equal(a.requests.length,0);assert.equal(a.elements['question-submit'].disabled,true);checks++;
  a=fixture({policy:{question_ready:false,question_inference_enabled:false,head_active:false,head_revision:0,head_target_id:null}});await a.login();assert.equal(a.elements['question-submit'].disabled,true);assert.equal(a.elements['qa-status'].textContent,'준비 전');assert.equal(posts(a).length,0);checks++;
  a=fixture({policy:{question_ready:false,question_inference_enabled:false}});await a.login();assert.equal(a.elements['question-submit'].disabled,true);assert.equal(a.elements['question-state'].textContent,'준비 전');checks++;
  a=fixture();await a.login();await ask(a);assert.equal(posts(a).length,2);assert.equal(posts(a)[0].url,'/api/questions');assert.equal(posts(a)[1].url.endsWith('/dispatch'),true);assert.equal(a.storage.size,0);assert.match(a.elements['answer-state'].textContent,/준비 중/);checks++;
  a=fixture();await a.login();a.elements['question-input'].value='중복 클릭';await Promise.all([a.elements['question-form'].listeners.submit({preventDefault(){}}),a.elements['question-form'].listeners.submit({preventDefault(){}})]);await a.settle();assert.equal(posts(a).length,2);assert.equal(a.qaSnapshot().length,1);checks++;
  a.timers[0]();await a.settle();assert.equal(posts(a).length,2);checks++;
  a=fixture({finishOnObserve:true});await a.login();await ask(a);await choose(a);assert.equal(posts(a).length,2);await a.event('question-cancel');assert.equal(posts(a).length,2);await choose(a);await a.event('question-confirm');assert.equal(posts(a).length,3);assert.equal(posts(a)[2].url.endsWith('/observe'),true);assert.match(a.elements['answer-text'].textContent,/검증된 결론/);assert.match(a.elements['answer-sources'].textContent,/2페이지/);checks++;
  const job={job_id:KEY,name:'실제 문서 이름',filename:'자료.pdf',size_bytes:3,sha256:'a'.repeat(64),state:'preparation_pending',source_document_id:SOURCE,refinement:{state:'canonical_verified'},recovery:{schema:1,job_id:KEY,revision:'a'.repeat(64),actions:[],reason:'GPU_WAIT',inference_requested:false}};
  a=fixture({questions:[complete()],jobs:[job]});await a.login();await select(a);assert.match(a.elements['answer-sources'].textContent,/실제 문서 이름/);assert.match(a.elements['answer-sources'].textContent,/실제 출처 원문/);const link=a.elements['answer-links'].children[0];assert.equal(link.attributes.href,'#qa-source-'+QID+'-S1');assert.equal(a.elements['answer-sources'].children[0].attributes.id,link.attributes.href.slice(1));assert.equal(posts(a).length,0);checks++;
  const block=complete();delete block.result.citations[0].source.source_spans[0].metadata;
  a=fixture({questions:[block]});await a.login();await select(a);assert.doesNotMatch(a.elements['answer-sources'].textContent,/페이지/);assert.match(a.elements['answer-sources'].textContent,/블록 paragraph-1/);checks++;
  a=fixture({questions:[complete()],policy:{question_ready:false,question_inference_enabled:false,head_revision:2,head_target_id:CANON}});await a.login();await select(a);assert.match(a.elements['answer-info'].textContent,/질문 당시 자료 버전1/);assert.match(a.elements['answer-text'].textContent,/검증된 결론/);assert.equal(a.elements['question-submit'].disabled,true);checks++;
  a=fixture({questions:[complete({result:result({answer:'선택한 자료에서 질문에 답할 충분한 근거를 찾지 못했습니다.',outcome:'no_evidence',citations:[]})})]});await a.login();await select(a);assert.equal(a.elements['answer-state'].textContent,'근거를 찾지 못함');assert.equal(a.elements['answer-sources'].children.length,0);assert.match(a.elements['answer-warnings'].textContent,/근거가 부족/);checks++;
  a=fixture({questions:[question({state:'failed',agent_run_id:RUN,attempts:1,error_code:'MODEL_TIMEOUT'})]});await a.login();await select(a);assert.equal(a.elements['answer-state'].textContent,'답변 실패');assert.equal(a.elements['answer-text'].textContent,'');assert.equal(a.elements['answer-actions'].children.length,0);checks++;
  a=fixture({questions:[complete({result:result({answer:'<img onerror=alert(1)> [S1]',warnings:['ANSWER_TRUNCATED','CHECK_SOURCE_QUALITY'],answer_truncated:true})})]});await a.login();await select(a);assert.match(a.elements['answer-text'].textContent,/<img/);assert.match(a.elements['answer-warnings'].textContent,/길이 제한/);assert.equal(posts(a).length,0);checks++;
  a=fixture({questions:[complete({result:result({answer:'잘못된 출처 [S2]'})})]});await a.login();assert.equal(a.elements['question-submit'].disabled,true);assert.equal(a.elements['answer-text'].textContent,'');checks++;
  a=fixture({loss:'accept-after'});await a.login();await ask(a);assert.equal(posts(a).length,1);assert.equal(a.storage.size,1);await a.event('question-recover');assert.equal(a.storage.size,0);assert.equal(posts(a).length,1);assert.equal(a.elements['answer-state'].textContent,'실행 전');checks++;
  a=fixture({loss:'accept-before'});await a.login();await ask(a);const original=posts(a)[0];assert.equal(a.storage.size,1);await ask(a,'다른 질문');assert.equal(posts(a).length,2);assert.equal(posts(a)[1].body,original.body);assert.equal(posts(a)[1].headers['Idempotency-Key'],original.headers['Idempotency-Key']);assert.equal(a.qaSnapshot().length,1);assert.equal(a.qaSnapshot()[0].state,'pending');checks++;
  a=fixture({loss:'dispatch-after'});await a.login();await ask(a);assert.equal(posts(a).length,2);assert.equal(a.storage.size,1);await a.event('question-recover');assert.equal(a.storage.size,0);assert.equal(posts(a).length,2);assert.equal(a.qaSnapshot()[0].attempts,1);checks++;
  a=fixture({loss:'dispatch-before'});await a.login();await ask(a);const stored=clone(a.storage),qs=a.qaSnapshot();const reload=fixture({storage:stored,questions:qs});await reload.login();assert.equal(posts(reload).length,0);assert.equal(reload.storage.size,1);await choose(reload);await reload.event('question-confirm');assert.equal(posts(reload).length,1);assert.equal(posts(reload)[0].url.endsWith('/dispatch'),true);assert.equal(reload.qaSnapshot().length,1);checks++;
  a=fixture({unknownRun:true});await a.login();await ask(a);assert.equal(a.elements['answer-state'].textContent,'실행 접수 확인 필요');await choose(a);await a.event('question-confirm');assert.equal(posts(a)[2].url.endsWith('/retry'),true);assert.equal(a.qaSnapshot().length,1);checks++;
  a=fixture({storageFail:true});await a.login();await ask(a);assert.equal(posts(a).length,0);assert.match(a.elements.notice.textContent,/저장할 수 없습니다/);checks++;
  a=fixture({storage:new Map([[STORE,'{broken']])});await a.login();assert.equal(a.elements['question-submit'].disabled,true);assert.equal(posts(a).length,0);checks++;
  a=fixture({storage:new Map([[STORE,JSON.stringify(record({library_id:SOURCE}))]])});await a.login();assert.equal(a.elements['question-submit'].disabled,true);assert.match(a.elements.notice.textContent,/자료함이 변경/);assert.equal(posts(a).length,0);checks++;
  a=fixture({onRequest:(url,config)=>url==='/api/question-policy'?{status:503,value:{code:'QUESTION_HEAD_NOT_INITIALIZED'}}:null});await a.login();assert.equal(a.elements.workspace.hidden,false);assert.equal(a.elements['upload-submit'].disabled,false);assert.equal(a.elements['question-submit'].disabled,true);checks++;
  a=fixture();await a.login();a.policyChange({question_ready:false,question_inference_enabled:false});await ask(a);assert.equal(posts(a).length,0);checks++;
  a=fixture();await a.login();a.failReads();await a.event('refresh');assert.equal(a.elements['question-submit'].disabled,true);assert.equal(posts(a).length,0);checks++;
  a=fixture({questions:[question({state:'waiting',agent_run_id:RUN,attempts:1})],policy:{question_ready:false,question_inference_enabled:false},finishOnObserve:true});await a.login();await select(a);await choose(a);await a.event('question-confirm');assert.equal(posts(a).length,1);assert.equal(posts(a)[0].url.endsWith('/observe'),true);assert.equal(a.elements['answer-state'].textContent,'답변 완료');checks++;
  a=fixture({questions:[question()]});await a.login();await select(a);await choose(a);a.qaChange(QID,{state:'failed',agent_run_id:RUN,attempts:1,error_code:'MODEL_TIMEOUT'});await a.event('question-confirm');assert.equal(posts(a).length,0);assert.equal(a.elements['answer-state'].textContent,'답변 실패');checks++;
  a=fixture({onRequest:(url,c)=>url==='/api/questions' && c.method==='POST'?{status:401,value:{code:'AUTH_REQUIRED'}}:null});await a.login();await ask(a);assert.equal(a.elements.workspace.hidden,true);assert.equal(a.elements['question-input'].value,'');assert.equal(a.elements['answer-text'].textContent,'');assert.equal(a.storage.size,1);checks++;
  a=fixture({loss:'accept-after'});await a.login();await ask(a);await a.event('logout');assert.equal(a.elements['question-input'].value,'');assert.equal(a.elements['question-pending-text'].textContent,'');assert.equal(a.storage.size,1);checks++;
  a=fixture();await a.login();await ask(a,'한'.repeat(2731));assert.equal(posts(a).length,0);await ask(a,' ');assert.equal(posts(a).length,0);await ask(a,'x'.repeat(8192));assert.equal(posts(a).length,2);checks++;
  a=fixture();const Q=a.window.QAQuestions;
  for(const v of [record({token:'secret'}),record({question:'\0'}),record({question:'\ud800'}),record({key:'bad'})])assert.throws(()=>Q.pending(v));
  for(const v of [complete({result:result({answer:''})}),complete({result:result({outcome:'no_evidence'})}),question({state:'failed',result:result()})])assert.throws(()=>Q.question(v));
  assert.equal(Q.location({block_id:'x',metadata:{location:{page:0}}}),'블록 x');checks++;
  a=fixture({questions:[question({state:'confirmation_required',attempts:16,error_code:'QA_AGENT_OUTCOME_UNKNOWN'})]});await a.login();await select(a);assert.equal(a.elements['answer-actions'].children.length,0);assert.equal(posts(a).length,0);checks++;
  a=fixture({loss:'accept-after'});await a.login();await ask(a);const key=JSON.parse(a.storage.get(STORE)).key;
  const restored=fixture({questions:a.qaSnapshot(),storage:clone(a.storage),keys:[[key,a.qaSnapshot()[0].question_id]]});await restored.login();assert.equal(posts(restored).length,0);assert.equal(restored.storage.size,0);assert.equal(restored.elements['answer-state'].textContent,'실행 전');checks++;
  a=fixture({loss:'dispatch-after'});await a.login();await ask(a);const count=posts(a).length;a.timers[0]();await a.settle();assert.equal(posts(a).length,count);assert.equal(a.storage.size,0);checks++;
  a=fixture({questions:[question({state:'confirmation_required',attempts:1,error_code:'QA_AGENT_OUTCOME_UNKNOWN'})]});await a.login();await select(a);await choose(a);await Promise.all([a.elements['question-confirm'].listeners.click(),a.elements['question-confirm'].listeners.click()]);await a.settle();assert.equal(posts(a).length,1);assert.equal(a.qaSnapshot()[0].attempts,2);checks++;
  let entered,release;const waiting=new Promise(r=>entered=r),held=new Promise(r=>release=r);
  a=fixture({questions:[question()],onRead:async n=>{if(n===2){entered();await held;}}});await a.login();await select(a);await choose(a);a.elements['question-confirm'].listeners.click();await waiting;await a.event('logout');release();await a.settle();assert.equal(posts(a).length,0);assert.equal(a.storage.size,0);checks++;
  console.log(JSON.stringify({schema:1,result:'passed',checks,scope:'Q08_DOM_and_fetch_model_not_browser',production_API_requests:0,inference_requested:false,GPU_requested:false}));
}
if(require.main===module)run().catch(error=>{console.error(error);process.exitCode=1;});
module.exports={run};
