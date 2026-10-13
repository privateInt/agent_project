// Deterministic screen behavior in a DOM/fetch model. This is not a browser run.
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {webcrypto,createHash}=require('node:crypto');
const WEB=path.join(__dirname,'../web'),TOKEN='t'.repeat(43),KEY='11111111-1111-4111-8111-111111111111',LIB='22222222-2222-4222-8222-222222222222';
const clone=value=>structuredClone(value),hash=raw=>createHash('sha256').update(raw).digest('hex');
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.hidden=false;this.disabled=false;this.value='';this.files=[];this.listeners={};this.attributes={};this.raw='';this.classes=new Set();
    this.classList={toggle:(name,enabled)=>{if(enabled===undefined)enabled=!this.classes.has(name);enabled?this.classes.add(name):this.classes.delete(name);},add:name=>this.classes.add(name),remove:name=>this.classes.delete(name)};}
  set textContent(value){this.raw=String(value);this.children=[];}get textContent(){return this.raw+this.children.map(x=>x.textContent).join('');}
  set innerHTML(value){throw Error('HTML injection sink used');}
  append(...items){this.children.push(...items);}replaceChildren(...items){this.raw='';this.children=[...items];}
  addEventListener(name,handler){this.listeners[name]=handler;}setAttribute(name,value){this.attributes[name]=value;}
  scrollIntoView(){}
}
function boot(options={}){
  const ids=[...fs.readFileSync(path.join(WEB,'index.html'),'utf8').matchAll(/id="([^"]+)"/g)].map(x=>x[1]);
  const elements=Object.fromEntries(ids.map(id=>[id,new Element()])),timers=[],events={},requests=[],storage=options.storage || new Map();
  elements.workspace.hidden=true;elements['detail-panel'].hidden=true;elements.logout.hidden=true;
  const document={hidden:false,getElementById:id=>elements[id],createElement:tag=>new Element(tag)};
  let jobs=clone(options.jobs||[]),lib={library_id:LIB,name:'업무 자료함',reserved_documents:jobs.length,question_ready:false,...options.library},loss=null,unauthorized=false,badJobs=false,failReads=false,hold=null;
  const accepted=new Map(options.accepted || []);
  const sessionStorage={getItem:k=>storage.has(k)?storage.get(k):null,setItem:(k,v)=>{if(options.storageFail)throw Error('blocked');storage.set(k,v);},removeItem:k=>storage.delete(k)};
  async function fetch(url,config){
    requests.push({url,method:config.method,headers:{...config.headers},body:config.body});
    if(hold){await hold.promise;}
    if(unauthorized)return {ok:false,status:401,json:async()=>({code:'AUTH_REQUIRED'})};
    if(config.headers.Authorization!=='Bearer '+TOKEN)throw Error('bad credential');
    if(failReads && config.method==='GET')throw Error('read outage');
    const response=(value,status=200)=>({ok:status<400,status,json:async()=>clone(value)});
    if(options.onRequest){const custom=await options.onRequest(url,config,{jobs,lib,storage,requests});if(custom)return response(custom.value,custom.status||200);}
    if(url==='/api/library')return response(lib);
    if(url==='/api/jobs')return response(badJobs?{items:[{}]}:{items:jobs});
    if(url==='/api/question-policy')return response({question_ready:lib.question_ready,question_inference_enabled:lib.question_ready,
      question_API_connected:true,head_active:lib.question_ready,head_revision:lib.question_ready?1:0,
      head_target_id:lib.question_ready?KEY:null,policy_source:'explicit_server_opt_in'});
    if(url==='/api/questions')return response({items:[]});
    if(url.startsWith('/api/jobs/'))return response(jobs.find(j=>j.job_id===url.slice('/api/jobs/'.length)));
    if(url.startsWith('/api/upload-requests/'))return accepted.has(url.split('/').pop())?response(accepted.get(url.split('/').pop())):response({code:'REQUEST_NOT_FOUND'},404);
    if(url==='/api/uploads'){
      const key=config.headers['Idempotency-Key'];
      if(loss==='before'){loss=null;throw Error('response lost before commit');}
      if(options.intakeError)return response({code:'UPLOAD_TEXT_NOT_UTF8'},400);
      let job=accepted.get(key);
      if(!job){const file=config.body.get('file'),raw=Buffer.from(await file.arrayBuffer());job={job_id:webcrypto.randomUUID(),name:config.body.get('name'),filename:file.name,size_bytes:file.size,sha256:hash(raw),state:'preparation_pending',created_at:'2026-10-08T04:00:00Z',updated_at:'2026-10-08T04:00:00Z'};jobs.push(job);lib.reserved_documents++;accepted.set(key,job);}
      if(loss==='after'){loss=null;throw Error('response lost after commit');}
      return response(job,202);
    }
    throw Error('unexpected request '+url);
  }
  const window={addEventListener:(name,handler)=>events[name]=handler};
  const context=vm.createContext({window,document,fetch,crypto:{getRandomValues:array=>webcrypto.getRandomValues(array)},sessionStorage,TextEncoder,Uint8Array,Int32Array,DataView,FormData,AbortController,setTimeout,clearTimeout,setInterval:fn=>timers.push(fn),console});
  vm.runInContext(fs.readFileSync(path.join(WEB,'upload.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(WEB,'recovery.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(WEB,'questions.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(WEB,'app.js'),'utf8'),context);
  const flush=()=>new Promise(setImmediate);
  async function settle(){for(let i=0;i<1000;i++){await flush();if(!elements['file'].disabled && !elements['login-submit'].disabled)return;}throw Error('screen did not settle');}
  async function event(id,name='click'){if(elements[id].disabled)return;await elements[id].listeners[name]?.({preventDefault(){}});await settle();}
  async function login(){elements.token.value=TOKEN;await event('login-form','submit');}
  async function file(name='문서.txt',raw='자료입니다.'){elements.file.files=[new File([raw],name)];await event('file','change');}
  const snapshot=()=>clone(jobs);
  return {context,window,elements,document,requests,storage,events,timers,login,file,event,settle,snapshot,
    loss:value=>loss=value,unauthorized:()=>unauthorized=true,badJobs:()=>badJobs=true,failReads:()=>failReads=true,
    changeLibrary:id=>lib.library_id=id,hold:()=>{let release;hold={promise:new Promise(r=>release=r)};return ()=>{hold=null;release();};}};
}
function existing(overrides={}){return {job_id:KEY,name:'기존 문서',filename:'기존.md',size_bytes:5,sha256:hash('hello'),state:'preparation_pending',created_at:'2026-10-08T04:00:00Z',updated_at:'2026-10-08T04:00:00Z',...overrides};}
function record(overrides={}){return {schema:1,key:KEY,library_id:LIB,name:'문서',filename:'문서.txt',size_bytes:3,sha256:hash('abc'),...overrides};}
async function run(){let checks=0;
  let a=boot();assert.equal(a.requests.length,0);assert.equal(a.elements['upload-submit'].disabled,true);checks++;
  a.elements.token.value='bad';await a.event('login-form','submit');assert.equal(a.requests.length,0);checks++;
  await a.login();assert.equal(a.elements.workspace.hidden,false);assert.equal(a.elements.token.value,'');assert.equal(a.storage.size,0);assert.ok(a.requests.every(r=>r.method==='GET'));checks++;
  await a.file();assert.equal(a.elements['document-name'].value,'문서');await a.event('upload-form','submit');assert.equal(a.snapshot().length,1);assert.equal(a.requests.filter(r=>r.method==='POST').length,1);assert.equal(a.storage.size,0);assert.match(a.elements.documents.textContent,/내용 정리 대기/);checks++;
  a.timers[0]();await a.settle();assert.equal(a.requests.filter(r=>r.method==='POST').length,1);checks++;
  await a.event('logout');assert.equal(a.elements.workspace.hidden,true);assert.equal(a.elements.documents.textContent,'');assert.equal(a.elements.token.value,'');checks++;
  a=boot();await a.login();await a.file();a.loss('after');await a.event('upload-form','submit');assert.equal(a.snapshot().length,1);assert.equal(a.storage.size,1);assert.equal(a.elements['pending-panel'].hidden,false);await a.event('recover-upload');assert.equal(a.storage.size,0);assert.equal(a.requests.filter(r=>r.method==='POST').length,1);checks++;
  a=boot();await a.login();await a.file('문서.txt','abc');a.loss('before');await a.event('upload-form','submit');const originalKey=a.requests.find(r=>r.method==='POST').headers['Idempotency-Key'];await a.file('문서.txt','xyz');await a.event('upload-form','submit');assert.equal(a.requests.filter(r=>r.method==='POST').length,1);assert.match(a.elements.notice.textContent,/내용도 같아야/);await a.file('문서.txt','abc');await a.event('upload-form','submit');const posts=a.requests.filter(r=>r.method==='POST');assert.equal(posts.length,2);assert.equal(posts[1].headers['Idempotency-Key'],originalKey);assert.equal(a.snapshot().length,1);checks++;
  const storage=new Map([['document-qa.q07a.pending-upload',JSON.stringify(record())]]);a=boot({storage});await a.login();assert.equal(a.elements['pending-panel'].hidden,false);assert.equal(a.requests.filter(r=>r.method==='POST').length,0);checks++;
  a=boot();await a.login();await a.file();a.loss('after');await a.event('upload-form','submit');const stored=JSON.parse([...a.storage.values()][0]);const reloaded=boot({storage:a.storage,jobs:a.snapshot(),accepted:[[stored.key,a.snapshot()[0]]]});await reloaded.login();assert.equal(reloaded.storage.size,0);assert.equal(reloaded.requests.filter(r=>r.method==='POST').length,0);assert.equal(reloaded.snapshot().length,1);checks++;
  a=boot({storage:new Map([['document-qa.q07a.pending-upload','{broken']])});await a.login();assert.equal(a.elements['upload-submit'].disabled,true);assert.match(a.elements.notice.textContent,/기록을 읽을 수 없습니다/);checks++;
  a=boot({storageFail:true});await a.login();await a.file();await a.event('upload-form','submit');assert.equal(a.requests.filter(r=>r.method==='POST').length,0);assert.match(a.elements.notice.textContent,/저장할 수 없습니다/);checks++;
  a=boot({intakeError:true});await a.login();await a.file();await a.event('upload-form','submit');assert.equal(a.storage.size,0);assert.match(a.elements.notice.textContent,/UTF-8/);checks++;
  a=boot({jobs:[existing({name:'<img src=x onerror=alert(1)>',refinement:{state:'canonical_verified'},indexing:{state:'activated'}})]});await a.login();assert.match(a.elements.documents.textContent,/<img/);assert.equal(a.elements['question-state'].textContent,'준비 전');assert.match(a.elements.documents.textContent,/검색 자료 준비됨/);checks++;
  a=boot({jobs:[existing({refinement:{state:'canonical_verified'},indexing_workflow:{state:'blocked',error_code:'GPU_VERIFICATION_DEFERRED'}})]});await a.login();assert.match(a.elements.documents.textContent,/GPU 사용 대기/);const row=a.elements.documents.children[0];await row.children[1].children[0].listeners.click();assert.match(a.elements['detail-steps'].textContent,/원본 업로드완료내용 정리완료검색 준비GPU 사용 대기/);checks++;
  a=boot({jobs:[existing({indexing_workflow:{state:'blocked',error_code:'OTHER_FAILURE'}})]});await a.login();assert.match(a.elements.documents.textContent,/확인 필요/);assert.doesNotMatch(a.elements.documents.textContent,/GPU/);checks++;
  a=boot({jobs:[existing()],library:{reserved_documents:20}});await a.login();assert.equal(a.elements['upload-submit'].disabled,true);checks++;
  a=boot({jobs:[existing()],library:{question_ready:true}});await a.login();assert.equal(a.elements['question-state'].textContent,'질문 가능');assert.doesNotMatch(a.elements.documents.textContent,/검색 자료 준비됨/);checks++;
  a=boot({jobs:[existing()]});await a.login();const prior=a.elements.documents.textContent;a.badJobs();await a.event('refresh');assert.equal(a.elements.documents.textContent,prior);assert.match(a.elements.notice.textContent,/서버 응답/);checks++;
  a=boot();await a.login();a.unauthorized();await a.event('refresh');assert.equal(a.elements.workspace.hidden,true);assert.equal(a.storage.size,0);checks++;
  a=boot({storage:new Map([['document-qa.q07a.pending-upload',JSON.stringify(record({library_id:'33333333-3333-4333-8333-333333333333'}))]])});await a.login();assert.match(a.elements.notice.textContent,/자료함이 변경/);assert.equal(a.requests.filter(r=>r.method==='POST').length,0);checks++;
  a=boot();await a.login();let release;const delayed=new Promise(r=>release=r);a.elements.file.files=[{name:'문서.txt',size:3,arrayBuffer:()=>delayed}];a.elements['document-name'].value='문서';a.elements['upload-form'].listeners.submit({preventDefault(){}});await new Promise(setImmediate);await a.event('logout');await a.login();release(new TextEncoder().encode('abc').buffer);await a.settle();assert.equal(a.requests.filter(r=>r.method==='POST').length,0);checks++;
  a=boot();const U=a.window.QAUpload;
  for(const input of [Buffer.alloc(0),Buffer.from('abc'),Buffer.from('한글 내용'),Buffer.alloc(55,0xab),Buffer.alloc(56,0xcd),Buffer.alloc(64,0xef),Buffer.alloc(1024*1024+3,0x34),Buffer.alloc(U.MAX_BYTES,0x52)])assert.equal(await U.sha256(new Uint8Array(input)),hash(input));checks++;
  for(const [name,file] of [['',{name:'x.txt',size:1}],['a'.repeat(201),{name:'x.txt',size:1}],['자료',{name:'x.txt',size:0}],['자료',{name:'x.txt',size:U.MAX_BYTES+1}],['자료',{name:'x.csv',size:1}]])assert.throws(()=>U.metadata(name,file));assert.equal(U.metadata('자료',{name:'x.txt',size:U.MAX_BYTES}).size_bytes,U.MAX_BYTES);assert.throws(()=>U.pending({...record(),token:TOKEN}));checks++;
  a=boot();await a.login();a.document.hidden=true;const count=a.requests.length;a.timers[0]();await a.settle();assert.equal(a.requests.length,count);checks++;
  console.log(JSON.stringify({schema:1,result:'passed',checks,scope:'DOM_and_fetch_model_not_browser',production_API_requests:0,inference_requested:false,GPU_requested:false}));
}
if(require.main===module)run().catch(error=>{console.error(error);process.exitCode=1;});
module.exports={boot,run};
