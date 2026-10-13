// Browser logic with DOM/fetch doubles. Does not claim rendering or live services.
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
class Element{constructor(){this.value='';this.hidden=false;this.children=[];this.textContent='';this.listeners={};}append(...x){this.children.push(...x);}replaceChildren(...x){this.children=[...x];}addEventListener(k,v){this.listeners[k]=v;}focus(){}}
const storage=new Map(),requests=[];
let drop=true,storageFailure='';
const idx={index_id:'idx',name:'<script>자료</script>',state:'active',revision:7,active_version_id:'old'};
const version={index_id:'idx',index_version_id:'v2',state:'ready',chunk_count:48,indexed_count:48,reference_state:'held',input_spec:{chunker:{id:'structure_v1'}},embedding_profile:{profile_id:'snowflake-ko-v1'}};
async function fetchStub(url,options={}){
 let body={};
 if(url==='/api/chunkers')body={items:[]};else if(url.startsWith('/api/source-groups'))body={items:[]};
 else if(options.method){requests.push({url,...options});if(drop){drop=false;throw new Error('ack lost');}body={...idx,index_version_id:'v2'};}
 else if(url.startsWith('/api/indexes?'))body={items:[idx]};
 else if(url==='/api/indexes/idx')body=idx;
 else if(url.startsWith('/api/indexes/idx/versions?'))body={items:[version]};
 else throw new Error('Unexpected request '+url);
 return {ok:true,status:200,json:async()=>body};
}
function boot(){const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};const context=vm.createContext({console,fetch:fetchStub,URLSearchParams,Option:class extends Element{constructor(text,value){super();this.textContent=text;this.value=value;}},crypto:webcrypto,confirm:()=>true,document:{getElementById:get,createElement:()=>new Element()},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>{if(storageFailure==='set')throw new Error('quota');storage.set(k,v);},removeItem:k=>{if(storageFailure==='remove')throw new Error('blocked');storage.delete(k);}}});for(const file of ['app.js','indexing.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../gateway/web',file),'utf8'),context);return {get,run:code=>vm.runInContext(code,context)};}
(async()=>{
 let app=boot();await new Promise(setImmediate);assert.equal(requests.length,0,'opening UI must not create a build');
 await assert.rejects(app.run(`indexMutation('/api/indexes/idx/activate',{index_version_id:'v2',expected_active_version_id:'old',expected_revision:7})`),/ack lost/);
 assert.equal(app.get('indexRetry').hidden,false);
 await assert.rejects(app.run(`indexMutation('/api/indexes/idx/builds',{chunking_run_id:'r1'})`),/동일 요청/);assert.equal(requests.length,1);
 app=boot();await new Promise(setImmediate);await app.run('sendIndexPending()');assert.equal(requests.length,2);assert.equal(JSON.stringify(requests[0]),JSON.stringify(requests[1]));assert.equal(storage.size,0);assert.equal(app.get('indexRetry').hidden,true);
 assert.equal(app.get('indexInfo').children[0].textContent,idx.name,'model text must remain literal');
 assert.equal(app.get('indexVersions').children[0].children[0].textContent,'완료 · 48/48개 (100%)');
 app.get('buildHnswM').value='32';app.get('buildHnswEF').value='256';assert.deepEqual(JSON.parse(JSON.stringify(app.run('buildHNSW()'))),{m:32,ef_construction:256});
 for(const [id,value]of [['buildHnswM','1'],['buildHnswM','65'],['buildHnswM','2.5'],['buildHnswEF','15'],['buildHnswEF','1025']]){
  app.get('buildHnswM').value='16';app.get('buildHnswEF').value='128';app.get(id).value=value;
  assert.throws(()=>app.run('buildHNSW()'),/HNSW/);
 }
 app.get('buildHnswM').value='';app.get('buildHnswEF').value='512';assert.deepEqual(JSON.parse(JSON.stringify(app.run('buildHNSW()'))),{ef_construction:512});
 storageFailure='set';const blockedCount=requests.length;
 await assert.rejects(app.run(`indexMutation('/api/indexes/idx/activate',{index_version_id:'v2'})`),/브라우저.*저장/);
 assert.equal(requests.length,blockedCount);assert.equal(app.run('indexState.pending'),null);assert.equal(storage.size,0);
 storageFailure='remove';await assert.rejects(app.run(`indexMutation('/api/indexes/idx/activate',{index_version_id:'v2'})`),/브라우저.*저장/);
 const acknowledged=requests.at(-1);assert.ok(app.run('indexState.pending'));assert.equal(storage.size,1);
 storageFailure='';app=boot();await new Promise(setImmediate);await app.run('sendIndexPending()');
 assert.equal(JSON.stringify(requests.at(-1)),JSON.stringify(acknowledged));assert.equal(storage.size,0);assert.equal(app.run('indexState.pending'),null);
 console.log('PASS index storage: blocked writes send no request; failed cleanup keeps the exact replay key');
 console.log('PASS indexing UI: persisted exact replay/CAS; unknown request blocks a new mutation; literal text; progress; bounded HNSW options.');
})().catch(e=>{console.error(e);process.exitCode=1;});
