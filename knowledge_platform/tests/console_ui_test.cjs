// Runs all shipped scripts against the actual HTML IDs and a DOM/fetch double.
// It validates interaction and persistence, not browser layout or live services.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
const root=path.join(__dirname,'../gateway/web'),html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const id=n=>`10000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
const ids={index:id(1),version:id(2),dep:id(3),reranker:id(4),source:id(5),canonical:id(6),run:id(7),modelVersion:id(8),model:id(9)};
const profile={deployment_id:ids.dep,capability:'embedding',status:'ready',embedding_space_id:'es_'+'a'.repeat(64),dimension:1024,repo_id:'embedding-repo'};
const rp={deployment_id:ids.reranker,capability:'reranker',status:'ready',reranker_profile_id:'rp_'+'b'.repeat(64),max_input_tokens:512,max_candidates:100,score_kind:'logit',score_direction:'higher_is_better',truncation:'reject',repo_id:'reranker-repo'};
const index={index_id:ids.index,name:'기존 지식',state:'active',embedding_deployment_id:ids.dep,active_version_id:ids.version,revision:4};
const version={index_id:ids.index,index_version_id:ids.version,state:'ready',reference_state:'held',chunk_count:48,indexed_count:48,embedding_profile:profile,canonical_ids:[ids.canonical],input_spec:{documents:[{canonical_id:ids.canonical,source_document_id:ids.source}],chunker:{id:'structure_v1'}}};
const chunker={id:'structure_v1',version:'2026-09-21.1',name:'문서 구조 기반 분할',description:'정리된 구조 사용',defaults:{max_characters:1000,overlap_characters:100}};
const flush=()=>new Promise(setImmediate);
class Element{
 constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.parentNode=null;this.listeners={};this.attrs={};this.dataset={};this.hidden=false;this.disabled=false;this.checked=false;this._text='';this._value='';this.className='';}
 append(...items){for(const i of items){i.parentNode=this;this.children.push(i);}}
 replaceChildren(...items){this.children.forEach(i=>i.parentNode=null);this.children=[];this._text='';this._value='';this.append(...items);}
 insertBefore(item,before){const at=this.children.indexOf(before);if(at<0)return this.append(item);item.parentNode=this;this.children.splice(at,0,item);}
 set textContent(text){this._text=String(text);this.children=[];}get textContent(){return this._text+this.children.map(i=>i.textContent).join('');}
 set value(value){this._value=String(value);}get value(){if(this.tagName==='SELECT'){const match=this.options.find(o=>o.value===this._value);return match?match.value:this.options[0]?.value||'';}return this._value;}
 get options(){return this.children.filter(x=>x.tagName==='OPTION');}get lastChild(){return this.children.at(-1);}get childElementCount(){return this.children.length;}
 setAttribute(key,value){this.attrs[key]=String(value);if(key.startsWith('data-'))this.dataset[key.slice(5)]=String(value);}removeAttribute(key){delete this.attrs[key];}
 addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}async emit(type,event={}){if(type==='click'&&this.disabled)return;for(const fn of this.listeners[type]||[])await fn(event);if(this['on'+type])await this['on'+type](event);}
 click(){return this.emit('click');}focus(){}scrollIntoView(){}
 closest(){let node=this;while(node){if(node.dataset.feedback)return node;node=node.parentNode;}return null;}
}
function dom(){
 const elements=new Map(),body=new Element('body'),stack=[body];
 for(const token of html.matchAll(/<\/?[a-zA-Z][^>]*>|[^<]+/g)){
  const t=token[0];if(t.startsWith('</')){if(stack.length>1)stack.pop();continue;}if(!t.startsWith('<')){stack.at(-1)._text+=t;continue;}
  const tag=t.match(/^<(\w+)/)?.[1];if(!tag)continue;const node=new Element(tag);for(const attr of t.matchAll(/([\w-]+)(?:="([^"]*)")?/g)){const [,key,value]=attr;if(key===tag)continue;node.setAttribute(key,value??'');if(key==='id'){assert.ok(!elements.has(value),'duplicate HTML id');elements.set(value,node);}if(key==='value')node.value=value;if(['hidden','disabled','checked'].includes(key))node[key]=true;}
  stack.at(-1).append(node);if(!['meta','link','input','br','hr'].includes(tag))stack.push(node);
 }
 return {getElementById:id=>elements.get(id)||null,createElement:tag=>new Element(tag),hidden:false,elements};
}
function boot(storage=new Map(),options={}){
 const doc=dom(),requests=[],timers=[],pauses=new Map();let drop=options.drop||'',hasNewIndex=false;
 function response(data,status=200){return {ok:status>=200&&status<300,status,json:async()=>data};}
 async function fetch(url,opt={}){
  requests.push({url,...opt});if(pauses.has(url)){const wait=pauses.get(url);pauses.delete(url);await wait;}
  if(url==='/api/chunkers')return response({items:[chunker]});
  if(url.startsWith('/api/source-groups'))return response({items:[]});
  if(url.startsWith('/api/sources?'))return response({items:[{document_id:ids.source,name:'보고서 <script>'}]});
  if(url.includes('/canonical-versions?'))return response({items:[{output_document_id:'00000000-0000-0000-0000-000000000000',status:'succeeded',name:'invalid'},{output_document_id:ids.canonical,status:'succeeded',name:'정리 완료',created_at:'2026-09-22T10:00:00Z'}]});
  if(url.startsWith('/console/model/deployments?'))return response({items:[{deployment_id:ids.dep,model_version_id:ids.modelVersion,status:'ready',desired_state:'running'},{deployment_id:ids.reranker,model_version_id:ids.modelVersion,status:'ready',desired_state:'running'}]});
  if(url.endsWith('/embedding-profile'))return url.includes(ids.dep)?response(profile):response({error:{message:'unsupported'}},409);
  if(url.endsWith('/reranker-profile'))return url.includes(ids.reranker)?response(rp):response({error:{message:'unsupported'}},409);
  if(url.startsWith('/console/model/model-versions/'))return response({model_version_id:ids.modelVersion,model_id:ids.model,version:'base-v1',repo_id:'repo'});
  if(url.startsWith('/console/model/models/'))return response({model_id:ids.model,name:'한국어 모델'});
  if(url==='/api/indexes'&&opt.method==='POST'){hasNewIndex=true;if(drop==='index'){drop='';throw new Error('lost index ACK');}return response({...index,index_id:id(20),name:'새 지식',active_version_id:''});}
  if(url.endsWith('/builds')&&opt.method==='POST'){if(drop==='build'){drop='';throw new Error('lost build ACK');}return response({...version,index_id:url.split('/')[3],index_version_id:id(21),state:'protecting'});}
  if(url.startsWith('/api/indexes?'))return response({items:[index,...hasNewIndex?[{...index,index_id:id(20),name:'새 지식',active_version_id:''}]:[]]});
  if(url.includes('/versions?'))return response({items:options.activeOffPage?[]:[{...version,index_id:url.split('/')[3]}]});
  if(url.startsWith('/api/indexes/')&&!opt.method)return response({...index,index_id:url.split('/')[3],active_version_id:url.includes(id(20))?'':ids.version});
  if(url==='/api/index-versions/'+ids.version)return response(version);
  if(url.startsWith('/api/chunking-runs?'))return response({items:[{chunking_run_id:ids.run,name:'저장 결과',chunk_count:48,result_registered:true}]});
  if(url==='/api/chunking-runs/'+ids.run)return response({chunking_run_id:ids.run,name:'저장 결과',result_registered:true,chunk_count:48,spec:version.input_spec});
  if(url.includes('/chunks?'))return response({items:[]});
  throw new Error('Unexpected request: '+url+' '+(opt.method||'GET'));
 }
 const context=vm.createContext({console,fetch,URLSearchParams,AbortController,TextEncoder,Option:class extends Element{constructor(text,value){super('option');this.textContent=text;this.value=value;}},crypto:options.fallbackCrypto?{getRandomValues:webcrypto.getRandomValues.bind(webcrypto)}:webcrypto,confirm:()=>true,setInterval:fn=>timers.push(fn),document:doc,localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>{if(options.storageFailure)throw new Error('storage unavailable');storage.set(k,v);},removeItem:k=>storage.delete(k)}});
 for(const file of ['app.js','indexing.js','search.js','console.js'])vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),context,{filename:file});
 return {run:code=>vm.runInContext(code,context),get:id=>{assert.ok(doc.elements.has(id),'missing real HTML id '+id);return doc.elements.get(id);},requests,storage,doc,timers,pauses};
}
async function ready(app){await flush();await flush();}
async function setup(app){await ready(app);await app.get('newKnowledge').click();const row=app.get('sources').children[0];await row.children[0].children[1].click();const host=row.children[1],select=host.children[1].children[0];assert.equal(select.options.length,1,'zero UUID must not be selectable');select.value=ids.canonical;await host.children[1].children[1].click();await app.get('stepNext').click();app.get('indexName').value='새 지식';app.get('indexDeployment').value=ids.dep;}
(async()=>{
 let app=boot();await setup(app);
 assert.equal(app.get('createStep2').hidden,false);assert.equal(app.get('stepNext').textContent,'내용 확인');assert.match(app.get('indexDeployment').options[1].textContent,/한국어 모델/);
 assert.equal(app.requests.filter(x=>x.method).length,0,'navigation, source and model selection are read only');
 app.get('buildHnswM').value='32';app.get('buildHnswEF').value='256';await app.get('stepNext').click();assert.match(app.get('createReview').textContent,/m 32/);assert.equal(app.get('createStep3').hidden,false);
 await app.get('startKnowledge').click();let mutations=app.requests.filter(x=>x.method);assert.equal(mutations.length,2);assert.equal(mutations[0].url,'/api/indexes');assert.equal(mutations[1].url,`/api/indexes/${id(20)}/builds`);
 const build=JSON.parse(mutations[1].body);assert.deepEqual(build.chunking.canonical_ids,[ids.canonical]);assert.deepEqual(build.hnsw,{m:32,ef_construction:256});assert.equal(build.embedding_deployment_id,ids.dep);assert.equal(app.storage.size,0);assert.equal(app.get('listPanel').hidden,false);assert.ok(!mutations.some(x=>x.url.includes('activate')||x.url.endsWith('embeddings')));
 for(const stage of ['index','build']){
  const storage=new Map();app=boot(storage,{drop:stage});await setup(app);await app.get('stepNext').click();await app.get('startKnowledge').click();const original=app.requests.filter(x=>x.method).at(-1);assert.equal(app.get('flowRetry').hidden,false);assert.equal(app.get('startKnowledge').disabled,true);
  const reloaded=boot(storage);await ready(reloaded);assert.equal(reloaded.requests.filter(x=>x.method).length,0,'reload cannot repeat mutations');await reloaded.get('flowRetry').click();const replay=reloaded.requests.filter(x=>x.method)[0];assert.equal(JSON.stringify(replay),JSON.stringify(original),'uncertain phase must replay exact frozen body and key');assert.equal(storage.size,0);assert.equal(reloaded.get('flowRetry').hidden,true);
 }
 app=boot();await setup(app);app.get('indexDeployment').value='invalid';await app.get('stepNext').click();assert.match(app.get('createStatus').textContent,/임베딩 모델/);assert.equal(app.requests.filter(x=>x.method).length,0);
 app.get('indexDeployment').value=ids.dep;app.get('buildHnswM').value='65';await app.get('stepNext').click();assert.match(app.get('createStatus').textContent,/HNSW/);assert.equal(app.requests.filter(x=>x.method).length,0);
 app=boot(new Map(),{storageFailure:true});await setup(app);await app.get('stepNext').click();await app.get('startKnowledge').click();assert.equal(app.requests.filter(x=>x.method).length,0,'no mutation without persisted key');assert.match(app.get('createStatus').textContent,/storage/);
 app=boot(new Map(),{fallbackCrypto:true});await ready(app);assert.equal(app.run('validUUID(key())'),true,'HTTP browser fallback keys must remain UUIDs');
 app=boot(new Map(),{activeOffPage:true});await ready(app);await app.run(`searchKnowledge('${ids.index}')`);assert.equal(app.get('searchVersion').value,ids.version);assert.equal(app.run('searchState.pin.version.index_version_id'),ids.version);assert.equal(app.requests.filter(x=>x.method).length,0);
 await app.run('loadModels()');app.get('searchRerank').checked=true;await app.get('searchRerank').emit('change');app.get('searchReranker').value=ids.reranker;await app.get('searchReranker').emit('change');assert.equal(app.run('searchState.rerankerPin.deployment_id'),ids.reranker);assert.equal(app.requests.filter(x=>x.method).length,0);
 app=boot();await setup(app);app.get('indexInput').value='saved';await app.get('indexInput').emit('change');app.get('runs').value=ids.run;await app.get('runs').emit('change');assert.equal(app.get('savedDocuments').children.length,1);app.run('consoleState.savedIDs.clear()');await assert.rejects(app.run('(async()=>createPayload())()'),/한 개 이상/);app.run(`consoleState.savedIDs.add('${ids.canonical}')`);assert.equal(app.run('createPayload().body.chunking_run_id'),ids.run);
 // Existing index: one build, never another logical index or implicit activation.
 app.run(`consoleState.target=${JSON.stringify(index)}`);await app.run('beginKnowledge()');mutations=app.requests.filter(x=>x.method);assert.equal(mutations.length,1);assert.equal(mutations[0].url,`/api/indexes/${ids.index}/builds`);
 // A delayed failure from a previous selection must not overwrite the new empty selection.
 app=boot();await ready(app);await app.get('tabSearch').click();let rejectOld;const wait=new Promise((_,reject)=>{rejectOld=reject;});app.pauses.set('/api/indexes/'+ids.index,wait);
 app.get('searchIndex').value=ids.index;const oldSelection=app.get('searchIndex').emit('change');await flush();app.get('searchIndex').value='';await app.get('searchIndex').emit('change');app.run("searchFeedback('새 선택')");rejectOld(new Error('late unavailable'));await oldSelection;assert.equal(app.get('searchStatus').textContent,'새 선택');assert.equal(app.run('searchState.pin'),null);
 // A second click and edits during an in-flight POST cannot change the committed body.
 app=boot();await setup(app);await app.get('stepNext').click();let releaseCreate;app.pauses.set('/api/indexes',new Promise(resolve=>{releaseCreate=resolve;}));const creating=app.get('startKnowledge').click();await flush();app.get('indexName').value='바뀐 이름';await app.get('startKnowledge').click();assert.equal(app.requests.filter(x=>x.method).length,1);releaseCreate();await creating;assert.equal(JSON.parse(app.requests.filter(x=>x.method).at(-1).body).chunking.name,'새 지식');
 // Typing a question during automatic selection must not cancel version/profile reads.
 app=boot();await ready(app);await app.get('tabSearch').click();let releaseVersions;app.pauses.set('/api/indexes/'+ids.index,new Promise(resolve=>{releaseVersions=resolve;}));app.get('searchIndex').value=ids.index;const selecting=app.get('searchIndex').emit('change');await flush();app.get('searchQuestion').value='새 질문';await app.get('searchQuestion').emit('input');releaseVersions();await selecting;assert.equal(app.run('searchState.pin.version.index_version_id'),ids.version);
 await app.run('loadModels()');app.get('searchReranker').value=ids.reranker;let releaseProfile;app.pauses.set('/console/model/deployments/'+ids.reranker+'/reranker-profile',new Promise(resolve=>{releaseProfile=resolve;}));const selectingModel=app.get('searchReranker').emit('change');await flush();app.get('searchQuestion').value='질문 수정';await app.get('searchQuestion').emit('input');releaseProfile();await selectingModel;assert.equal(app.run('searchState.rerankerPin.deployment_id'),ids.reranker);assert.equal(app.requests.filter(x=>x.method).length,0);
 // Editing the query during lazy preparation cancels that clicked search before inference.
 app.run('clearSearch()');let releasePrepare;app.pauses.set('/api/indexes/'+ids.index,new Promise(resolve=>{releasePrepare=resolve;}));const clickedSearch=app.get('searchRun').click();await flush();app.get('searchQuestion').value='다시 입력한 질문';await app.get('searchQuestion').emit('input');releasePrepare();await clickedSearch;assert.equal(app.requests.filter(x=>x.method).length,0);
 // Old UUID wording is presentation-only: never invent/rewrite an ID.
 assert.match(app.run("friendlyError(new Error('canonical nonzero UUID is required'))"),/선택 정보/);
 assert.equal(app.run("validUUID('00000000-0000-0000-0000-000000000000')"),false);
 console.log('PASS console UI: real HTML IDs; three-step flow; named capability-verified models; no implicit mutation; exact reload recovery for both phases; blocked storage and invalid selections; custom HNSW; saved subset; existing-index build; active-version paging; automatic reranker selection.');
})().catch(e=>{console.error(e);process.exitCode=1;});
