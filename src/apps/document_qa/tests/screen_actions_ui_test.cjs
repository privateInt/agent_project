// Q07b behavior with a DOM/fetch model, not a real browser.
'use strict';
const assert=require('node:assert/strict');
const {boot}=require('./screen_ui_test.cjs');
const KEY='11111111-1111-4111-8111-111111111111',LIB='22222222-2222-4222-8222-222222222222',STORE='document-qa.q07b.pending-action';
const second='33333333-3333-4333-8333-333333333333';
const clone=v=>structuredClone(v);
const item=(id='retry-upload',title='원본 전달 이어가기')=>({id,title,detail:'서버에 보관된 같은 파일을 원래 요청 키로 전달합니다.'});
function job(extra={}){return {job_id:KEY,name:'기존 문서',filename:'기존.txt',size_bytes:3,sha256:'c'.repeat(64),state:'upload_confirmation_required',attempts:1,
  created_at:'2026-10-08T04:00:00Z',updated_at:'2026-10-08T04:00:00Z',
  recovery:{schema:1,job_id:KEY,revision:'a'.repeat(64),actions:[item()],reason:'EXPLICIT_ACTION',inference_requested:false},...extra};}
const record=(extra={})=>({schema:1,library_id:LIB,job_id:KEY,revision:'a'.repeat(64),action:'retry-upload',...extra});
function fixture(options={}){
  let mode=options.mode||'',reads=0;
  const jobs=options.jobs||[job()];
  const a=boot({...options,jobs,onRequest:async(url,config,data)=>{
    if(config.method==='GET' && url==='/api/jobs/'+KEY){reads++;if(options.onRead)await options.onRead(reads,data);}
    if(!url.endsWith('/recover'))return;
    assert.equal(config.method,'POST');assert.equal(config.headers['Content-Type'],'application/json');
    const value=JSON.parse(config.body),stored=JSON.parse(data.storage.get(STORE));
    assert.equal(value.action,stored.action);assert.equal(value.revision,stored.revision);
    assert.equal(stored.library_id,LIB);assert.equal(stored.job_id,KEY);assert.equal(Object.keys(stored).length,5);
    assert.doesNotMatch(JSON.stringify(stored),/Bearer|token|credential|file_bytes/);
    if(mode==='before')throw Error('lost before stage dispatch');
    if(mode==='unauthorized')return {status:401,value:{code:'AUTH_REQUIRED'}};
    if(mode==='stale')return {status:409,value:{code:'RECOVERY_STATE_CHANGED'}};
    data.jobs[0].state='preparation_pending';data.jobs[0].attempts++;
    data.jobs[0].recovery={...data.jobs[0].recovery,revision:'b'.repeat(64),actions:[item('dispatch-workflow','문서 처리 연결하기')]};
    if(mode==='after')throw Error('lost after committed stage');
    if(mode==='bad')return {status:202,value:{action:value.action,job:{},inference_requested:false}};
    return {status:202,value:{schema:1,action:value.action,job:data.jobs[0],inference_requested:false}};
  }});
  return Object.assign(a,{mode:v=>mode=v});
}
async function select(a,index=0){await a.elements.documents.children[index].children[1].children[0].listeners.click();await a.settle();}
async function choose(a,index=0){const b=a.elements['recovery-actions'].children[index];if(!b.disabled)await b.listeners.click();await a.settle();}
const posts=a=>a.requests.filter(r=>r.method==='POST');
async function run(){let checks=0,a;
  a=fixture();await a.login();await select(a);assert.equal(posts(a).length,0);assert.equal(a.elements['recovery-actions'].children.length,1);checks++;
  await choose(a);assert.equal(a.elements['recovery-review'].hidden,false);assert.equal(posts(a).length,0);await a.event('recovery-cancel');assert.equal(a.elements['recovery-review'].hidden,true);checks++;
  await choose(a);await a.event('recovery-confirm');assert.equal(posts(a).length,1);assert.equal(posts(a)[0].url,'/api/jobs/'+KEY+'/recover');assert.equal(a.storage.size,0);assert.match(a.elements.notice.textContent,/완료 여부/);
  checks++;
  a=fixture();await a.login();await select(a);await choose(a);const confirm=a.elements['recovery-confirm'];await Promise.all([confirm.listeners.click(),confirm.listeners.click()]);await a.settle();assert.equal(posts(a).length,1);checks++;
  a=fixture({mode:'after'});await a.login();await select(a);await choose(a);await a.event('recovery-confirm');assert.equal(posts(a).length,1);assert.equal(a.storage.size,1);assert.equal(a.elements['recovery-pending'].hidden,false);await a.event('recover-action');assert.equal(a.storage.size,0);assert.equal(posts(a).length,1);assert.match(a.elements.notice.textContent,/완료 여부/);checks++;
  a=fixture({mode:'before'});await a.login();await select(a);await choose(a);await a.event('recovery-confirm');assert.equal(a.storage.size,1);const prior=posts(a)[0].body;
  const reload=fixture({storage:a.storage,jobs:a.snapshot()});await reload.login();assert.equal(posts(reload).length,0);assert.equal(reload.elements['recovery-pending'].hidden,false);await reload.event('recover-action');assert.equal(posts(reload).length,0);await choose(reload);await reload.event('recovery-confirm');assert.equal(posts(reload).length,1);assert.equal(posts(reload)[0].body,prior);assert.equal(reload.storage.size,0);checks++;
  a=fixture({mode:'before'});await a.login();await select(a);await choose(a);await a.event('recovery-confirm');a.timers[0]();await a.settle();assert.equal(posts(a).length,1);assert.equal(a.storage.size,1);checks++;
  a=fixture({storage:new Map([[STORE,'{broken']])});await a.login();await select(a);assert.equal(a.elements['recovery-actions'].children[0].disabled,true);assert.match(a.elements['recovery-help'].textContent,/기록을 읽을 수 없습니다/);
  assert.equal(posts(a).length,0);checks++;
  a=fixture({storageFail:true});await a.login();await select(a);await choose(a);await a.event('recovery-confirm');assert.equal(posts(a).length,0);assert.match(a.elements.notice.textContent,/저장할 수 없습니다/);checks++;
  a=fixture({storage:new Map([[STORE,JSON.stringify(record({library_id:second}))]])});await a.login();await select(a);assert.equal(a.elements['recovery-actions'].children[0].disabled,true);await a.event('recover-action');assert.equal(posts(a).length,0);assert.match(a.elements.notice.textContent,/자료함이 변경/);checks++;
  const other=job({job_id:second,recovery:{...job().recovery,job_id:second}});
  a=fixture({jobs:[job(),other],storage:new Map([[STORE,JSON.stringify(record())]])});await a.login();await select(a,0);assert.equal(a.elements['recovery-actions'].children[0].disabled,true);assert.equal(posts(a).length,0);checks++;
  a=fixture({onRead:async(n,{jobs})=>{if(n===1)jobs[0].recovery.revision='d'.repeat(64);}});await a.login();await select(a);await choose(a);assert.equal(posts(a).length,0);assert.equal(a.elements['recovery-review'].hidden,true);assert.match(a.elements.notice.textContent,/상태가 변경/);checks++;
  a=fixture({onRead:async(n,{jobs})=>{if(n===2)jobs[0].recovery.revision='d'.repeat(64);}});await a.login();await select(a);await choose(a);await a.event('recovery-confirm');assert.equal(posts(a).length,0);assert.equal(a.storage.size,0);assert.match(a.elements.notice.textContent,/상태가 변경/);checks++;
  a=fixture({mode:'unauthorized'});await a.login();await select(a);await choose(a);await a.event('recovery-confirm');assert.equal(a.elements.workspace.hidden,true);assert.equal(a.elements.token.value,'');assert.equal(a.storage.size,1);checks++;
  let release,entered;const enteredPromise=new Promise(r=>entered=r),held=new Promise(r=>release=r);
  a=fixture({onRead:async n=>{if(n===2){entered();await held;}}});await a.login();await select(a);await choose(a);const task=a.elements['recovery-confirm'].listeners.click();await enteredPromise;await a.event('logout');release();await task;await a.settle();assert.equal(posts(a).length,0);assert.equal(a.storage.size,0);checks++;
  a=fixture({jobs:[job({name:'<img onerror=alert(1)>',recovery:{...job().recovery,actions:[item('retry-upload','<script>evil</script>')]}})]});await a.login();await select(a);await choose(a);assert.match(a.elements['recovery-title'].textContent,/<script>/);assert.equal(posts(a).length,0);checks++;
  a=fixture({jobs:[job({recovery:{...job().recovery,actions:[item('resume-indexing-workflow')]}})]});await a.login();await select(a);assert.equal(a.elements['recovery-actions'].children.length,0);assert.equal(posts(a).length,0);checks++;
  a=fixture({jobs:[job({state:'preparation_pending',refinement:{state:'canonical_verified'},indexing_workflow:{state:'blocked',error_code:'GPU_VERIFICATION_DEFERRED'},recovery:{...job().recovery,actions:[],reason:'GPU_WAIT'}})],library:{question_ready:true}});await a.login();await select(a);assert.equal(a.elements['recovery-actions'].children.length,0);assert.match(a.elements['recovery-help'].textContent,/GPU/);assert.equal(posts(a).length,0);checks++;
  a=fixture();await a.login();await select(a);a.failReads();await a.event('refresh');assert.equal(a.elements['recovery-actions'].children[0].disabled,true);assert.equal(posts(a).length,0);checks++;
  a=fixture({mode:'stale'});await a.login();await select(a);await choose(a);await a.event('recovery-confirm');assert.equal(posts(a).length,1);assert.equal(a.storage.size,0);checks++;
  a=fixture({mode:'bad'});await a.login();await select(a);await choose(a);await a.event('recovery-confirm');assert.equal(a.storage.size,1);assert.equal(posts(a).length,1);await a.event('refresh');assert.equal(a.storage.size,0);assert.equal(posts(a).length,1);checks++;
  a=fixture();await a.login();await select(a);await a.event('refresh-job');await a.event('refresh');assert.equal(posts(a).length,0);checks++;
  a=fixture();const R=a.window.QARecovery;
  for(const value of [record({token:'secret'}),record({action:'resume-indexing-workflow'}),record({revision:'bad'}),record({job_id:'bad'})])assert.throws(()=>R.pending(value));checks++;
  for(const value of [{...job().recovery,job_id:second},{...job().recovery,inference_requested:true},{...job().recovery,reason:'other'},{...job().recovery,actions:[item(),item()]}])assert.throws(()=>R.plan(value,KEY));checks++;
  console.log(JSON.stringify({schema:1,result:'passed',checks,scope:'Q07b_DOM_and_fetch_model_not_browser',production_API_requests:0,inference_requested:false,GPU_requested:false}));
}
if(require.main===module)run().catch(error=>{console.error(error);process.exitCode=1;});
module.exports={run};
