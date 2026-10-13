// Node built-ins only. Execute the shipped browser JS with minimal DOM/fetch
// doubles to exercise request/replay state. This is not a browser rendering test.
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
const script=fs.readFileSync(path.join(__dirname,'../gateway/web/app.js'),'utf8');
class Element{
 constructor(){this.value='';this.hidden=false;this.children=[];this.textContent='';this.className='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=[...items];}
 addEventListener(){}
 focus(){}
 get childElementCount(){return this.children.length;}
}
const storage=new Map(),posts=[];
let firstDrop=true,reject=0,storageFailure='';
const canonical='22222222-2222-4222-8222-222222222222';
const run={chunking_run_id:'33333333-3333-4333-8333-333333333333',operation_id:'44444444-4444-4444-8444-444444444444',name:'preview',result_registered:false,submission_status:'delivered',spec:{snapshot_id:'snapshot-a',documents:[{canonical_id:canonical,source_document_id:'source-a'}],chunker:{id:'structure_v1',options:{max_characters:1000,overlap_characters:100}}},chunk_count:3};
async function fetchStub(url,opts={}){
 let body={};
 if(url==='/api/chunkers')body={items:['recursive_character_v1','structure_v1'].map(id=>({id,name:id,description:'test',defaults:{max_characters:1000,overlap_characters:100}}))};
 else if(url.startsWith('/api/source-groups'))body={items:[]};
 else if(url==='/api/chunking-runs'&&opts.method==='POST'){
  posts.push({key:opts.headers['Idempotency-Key'],body:opts.body});
  if(firstDrop){firstDrop=false;throw new Error('network acknowledgement lost');}
  if(reject)return {ok:false,status:reject,json:async()=>({error:{message:'invalid request'}})};
  body={chunking_run_id:'33333333-3333-4333-8333-333333333333',operation_id:'44444444-4444-4444-8444-444444444444',status:'accepted'};
 }else if(url==='/api/chunking-runs/33333333-3333-4333-8333-333333333333')body=run;
 else if(url.startsWith('/api/chunking-runs?'))body={items:[run]};
 else if(url.startsWith('/api/chunking-runs/33333333-3333-4333-8333-333333333333/chunks?'))body={items:[{ordinal:1,kind:'table',text:'<table>원문😀</table>',quality:{state:'needs_review'},structure:{unit:'rows',start:4,end:7,part:2,parts:3,context_rows:[0,1]},source_spans:[]}]};
 else if(url==='/api/operations/44444444-4444-4444-8444-444444444444')body={status:'running',phase:'chunking'};
 else throw new Error('unexpected fetch '+url);
 return {ok:true,status:200,json:async()=>body};
}
function boot(){
 const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 get('name').value='preview';
 const context=vm.createContext({console,fetch:fetchStub,URLSearchParams,Option:class extends Element{constructor(text,value){super();this.textContent=text;this.value=value;}},crypto:webcrypto,document:{getElementById:get,createElement:()=>new Element()},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>{if(storageFailure==='set')throw new Error('quota');storage.set(k,v);},removeItem:k=>{if(storageFailure==='remove')throw new Error('blocked');storage.delete(k);}}});
 vm.runInContext(script,context);return {context,get,run:code=>vm.runInContext(code,context)};
}
const flush=()=>new Promise(setImmediate);
(async()=>{
 let app=boot();await flush();assert.equal(app.get('chunker').value,'structure_v1');
 app.run(`state.selected.set('${canonical}',{name:'doc',sourceID:'source-a'})`);
 await assert.rejects(app.run('create()'),/network acknowledgement lost/);
 assert.equal(storage.size,1);assert.equal(app.get('retry').hidden,false);
 await assert.rejects(app.run('create()'),/이전 미리보기의 접수/);assert.equal(posts.length,1);
 app=boot();await flush();assert.equal(app.get('retry').hidden,false);
 await app.run('submitPending()');assert.equal(posts.length,2);assert.deepEqual(posts[0],posts[1]);assert.equal(storage.size,0);
 reject=400;app.run(`state.selected.set('${canonical}',{name:'doc',sourceID:'source-a'})`);
 await assert.rejects(app.run('create()'),/invalid request/);assert.equal(storage.size,0);assert.equal(app.get('retry').hidden,true);
 reject=0;app.run('state.current.result_registered=true; compare()');assert.equal(app.get('chunker').value,'recursive_character_v1');assert.equal(app.run('state.selected.size'),1);assert.equal(app.get('maxCharacters').value,1000);
 app.get('maxCharacters').value=100;app.get('overlap').value=50;
 const before=posts.length;await assert.rejects(app.run('create()'),/절반 미만/);assert.equal(posts.length,before);
 await app.run('openRun("33333333-3333-4333-8333-333333333333")');
 const card=app.get('chunks').children[0];
 assert.ok(card.children.some(x=>x.textContent==='원본 5~7행 · 분할 2/3'));
 assert.ok(card.children.some(x=>x.textContent==='문맥으로 반복한 원본 행: 1, 2'));
 assert.ok(card.children.some(x=>x.textContent==='<table>원문😀</table>'));
 // A browser storage failure must never send an unrecoverable mutation.
 app.get('maxCharacters').value=1000;app.get('overlap').value=100;
 storageFailure='set';const blockedCount=posts.length;
 await assert.rejects(app.run('create()'),/브라우저.*저장/);
 assert.equal(posts.length,blockedCount);assert.equal(app.run('state.pending'),null);
 assert.equal(storage.size,0);assert.equal(app.get('retry').hidden,true);
 // If cleanup fails after acknowledgement, keep the original key in memory/on disk.
 storageFailure='remove';await assert.rejects(app.run('create()'),/브라우저.*저장/);
 const acknowledged=posts.at(-1);assert.ok(app.run('state.pending'));assert.equal(storage.size,1);
 storageFailure='';app=boot();await flush();await app.run('submitPending()');
 assert.deepEqual(posts.at(-1),acknowledged);assert.equal(storage.size,0);assert.equal(app.run('state.pending'),null);
 console.log('PASS preview storage: blocked writes send no request; failed cleanup keeps the exact replay key');
 console.log('PASS structure preview: primary rows, repeated context rows, literal source text');
 console.log('PASS request state: default strategy; ambiguous response blocks new create; reload preserves exact replay; definitive rejection clears pending; comparison reuses canonical inputs/options; invalid overlap makes no request');
})().catch(e=>{console.error(e);process.exitCode=1;});
