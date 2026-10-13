// DOM/fetch doubles exercise browser orchestration; this is not a rendering test.
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
class Element{constructor(){this.value='';this.hidden=false;this.children=[];this.textContent='';this.listeners={};}append(...x){this.children.push(...x);}replaceChildren(...x){this.children=[...x];}addEventListener(k,v){this.listeners[k]=v;}focus(){}}
const idx='aafe22b7-852b-424e-afaa-58fa8c1d739f',versionID='150826c2-1f4c-495c-8e07-54aacaa1a245',dep='44247297-5184-4a36-9225-7791c80d3add',canonical='157c335f-5436-4338-8078-edb040ebe1ca';
const profile={deployment_id:dep,embedding_space_id:'es_'+'a'.repeat(64),dimension:1024,capability:'embedding',status:'ready',normalization:'l2',repo_id:'snowflake'};
const version={index_id:idx,index_version_id:versionID,state:'ready',reference_state:'held',chunk_count:48,embedding_profile:profile,canonical_ids:[canonical],input_spec:{documents:[{canonical_id:canonical,source_document_id:'source-1'}]}};
const index={index_id:idx,name:'자료',state:'active',active_version_id:versionID};
const reranker='cc64a683-a69f-48a5-836e-9f52f0255cc6',rp={deployment_id:reranker,reranker_profile_id:'rp_'+'e'.repeat(64),capability:'reranker',status:'ready',score_kind:'logit',score_direction:'higher_is_better',truncation:'reject',max_input_tokens:512,max_candidates:100,repo_id:'ko-reranker'};
const requests=[],storage=new Map();let mismatch=false,pause=false,release,entered,rerankFailure=false,wrongRerank=false,profileFailure=false;
async function fetchStub(url,options={}){
 requests.push({url,...options});let data;
 if(url==='/api/chunkers'||url.startsWith('/api/source-groups'))data={items:[]};
 else if(url.startsWith('/api/indexes?'))data={items:[index]};
 else if(url===`/api/indexes/${idx}`)data={...index};
 else if(url.startsWith(`/api/indexes/${idx}/versions?`))data={items:[version]};
 else if(url===`/api/index-versions/${versionID}`)data=version;
 else if(url.endsWith('/embedding-profile'))data={...profile,embedding_space_id:mismatch?'es_'+'f'.repeat(64):profile.embedding_space_id};
 else if(url.endsWith('/reranker-profile')){
  if(profileFailure)return {ok:false,status:503,json:async()=>({error:{message:'재정렬 모델을 조회할 수 없습니다.'}})};
  data={...rp};
 }
 else if(url.endsWith('/embeddings')){
  assert.equal(JSON.parse(options.body).inputs[0].text,'원문 <질문>');
  index.active_version_id='another-version'; // Active switches while the query is embedded.
  if(pause){entered();await new Promise(resolve=>{release=resolve;});}
  data={profile,role:'query',data:[{id:'query-1',index:0,embedding:[1,...Array(1023).fill(0)]}]};
 }else if(url===`/api/indexes/${idx}/search`){const body=JSON.parse(options.body);data={index_id:idx,index_version_id:body.index_version_id,embedding_space_id:profile.embedding_space_id,candidate_count:1,returned_count:1,filters:body.filters,rerank:{requested:false,applied:false,status:'disabled'},score_semantics:{id:'opensearch_lucene_cosinesimil_v1'},timings:{total_ms:5},results:[{rank:1,retrieval_rank:1,retrieval_score:0.8,chunk_id:'b'.repeat(64),canonical_id:canonical,source_document_id:'source-1',kind:'text',text:'<img src=x onerror=alert(1)>',quality:{state:'needs_ocr'},source_spans:[{block_id:'b90'}]}]};}
 else throw new Error('Unexpected request '+url);
 if(url.endsWith('/search')&&JSON.parse(options.body).rerank?.enabled){
  if(rerankFailure)return {ok:false,status:400,json:async()=>({error:{message:'RERANK_INPUT_TOO_LONG: model pair limit exceeded'}})};
  data.rerank={requested:true,applied:true,status:'applied',deployment_id:reranker,reranker_profile_id:wrongRerank?'rp_'+'f'.repeat(64):rp.reranker_profile_id,profile:rp};
  data.results[0].rerank_score=-1.25;data.results[0].rerank_input_tokens=30;
 }
 return {ok:true,status:200,json:async()=>data};
}
const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const context=vm.createContext({console,fetch:fetchStub,URLSearchParams,AbortController,TextEncoder,Option:class extends Element{constructor(text,value){super();this.textContent=text;this.value=value;}},crypto:webcrypto,confirm:()=>true,document:{getElementById:get,createElement:()=>new Element()},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}});
for(const file of ['app.js','indexing.js','search.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../gateway/web',file),'utf8'),context);
const run=code=>vm.runInContext(code,context);
const click=id=>get(id).listeners.click();
(async()=>{
 await new Promise(setImmediate);assert.equal(requests.filter(x=>x.method==='POST').length,0);
 await run('loadSearchIndexes()');get('searchIndex').value=idx;await run('loadSearchVersions()');get('searchVersion').value=versionID;await run('prepareSearch()');
 assert.equal(requests.filter(x=>x.url.includes('/console/model/')).length,0,'preparing must not call Model');
 get('searchQuestion').value='원문 <질문>';get('searchTopK').value='5';get('searchDocument').value=canonical;
 await run('runSearch()');
 const posts=requests.filter(x=>x.method==='POST');assert.equal(posts.length,2);assert.ok(posts[0].url.startsWith('/console/model/'));assert.ok(posts[1].url.endsWith('/search'));
 const body=JSON.parse(posts[1].body);assert.equal(body.index_version_id,versionID);assert.equal(body.query_text,undefined);assert.deepEqual(body.filters,{canonical_ids:[canonical]});
 const card=get('searchResults').children.find(x=>x.children.some(c=>c.textContent==='<img src=x onerror=alert(1)>'));assert.ok(card,'original text must be literal');assert.ok(card.children.some(x=>x.textContent==='원본 품질: needs_ocr'));assert.equal(storage.size,0);
 mismatch=true;let before=requests.filter(x=>x.method==='POST').length;await assert.rejects(run('runSearch()'),/공간/);assert.equal(requests.filter(x=>x.method==='POST').length,before);assert.equal(get('searchResults').children.length,0);
 mismatch=false;pause=true;const started=new Promise(resolve=>{entered=resolve;});const pending=run('runSearch()');await started;run('clearSearch(false)');release();await pending;
 assert.equal(requests.filter(x=>x.url.endsWith('/search')).length,1,'stale inference must not launch search');
 assert.ok(requests.filter(x=>x.method==='POST').every(x=>x.url.endsWith('/search')||x.url.endsWith('/embeddings')));
 assert.equal(requests.filter(x=>x.url.includes('/rerank')).length,0,'off must never contact reranker');
 pause=false;get('searchRerank').checked=true;get('searchReranker').value=reranker;get('searchCandidateK').value='20';
 before=requests.filter(x=>x.method==='POST').length;
 await assert.rejects(run('runSearch()'),/모델 확인/);assert.equal(requests.filter(x=>x.method==='POST').length,before);
 // Reproduce the reported click: a typed deployment ID without a fetched profile.
 get('searchRun').textContent='검색';get('searchResults').append(new Element());get('message').textContent='unrelated notice';
 await click('searchRun');
 assert.match(get('searchStatus').textContent,/모델 확인/);assert.equal(get('searchStatus').className,'error');
 assert.equal(get('searchResults').children.length,0);assert.equal(get('message').textContent,'unrelated notice','search feedback must be local');
 assert.equal(get('searchRun').textContent,'검색');assert.equal(get('searchRun').disabled,false);
 assert.equal(requests.filter(x=>x.method==='POST').length,before,'missing profile must not run inference');
 profileFailure=true;await get('searchReranker').listeners.change();assert.match(get('searchStatus').textContent,/조회할 수 없습니다/);assert.equal(get('searchStatus').className,'error');
 profileFailure=false;await get('searchReranker').listeners.change();assert.match(get('searchStatus').textContent,/모델을 확인했습니다/);assert.equal(get('searchStatus').className,'');assert.equal(requests.filter(x=>x.method==='POST').length,before,'profile selection must be read only');
 await run('runSearch()');
 let request=JSON.parse(requests.filter(x=>x.url.endsWith('/search')).at(-1).body);
 assert.equal(request.query_text,'원문 <질문>');assert.equal(request.candidate_k,20);assert.equal(request.top_k,5);assert.equal(request.rerank.expected_reranker_profile_id,rp.reranker_profile_id);
 assert.match(get('searchStatus').textContent,/재정렬 적용/);assert.equal(requests.filter(x=>x.url.endsWith('/rerank')).length,0,'browser must not score candidates itself');
 assert.ok(JSON.stringify(get('searchResults').children).includes('rerank_score'));assert.ok(JSON.stringify(get('searchResults').children).includes('-1.25'));
 wrongRerank=true;await assert.rejects(run('runSearch()'),/적용 결과/);assert.equal(get('searchResults').children.length,0);wrongRerank=false;
 rerankFailure=true;await assert.rejects(run('runSearch()'),/RERANK_INPUT_TOO_LONG/);assert.equal(get('searchResults').children.length,0);rerankFailure=false;
 await run('runSearch()');assert.ok(get('searchResults').children.length>0);
 get('searchCandidateK').value='2';before=requests.filter(x=>x.method==='POST').length;await assert.rejects(run('runSearch()'),/후보 수/);assert.equal(requests.filter(x=>x.method==='POST').length,before);assert.equal(get('searchResults').children.length,0,'invalid new search must clear previous success even without an input event');
 await click('searchRun');assert.match(get('searchStatus').textContent,/후보 수/);assert.equal(get('searchStatus').className,'error');assert.equal(requests.filter(x=>x.method==='POST').length,before);
 get('searchRerank').checked=false;await run('runSearch()');request=JSON.parse(requests.filter(x=>x.url.endsWith('/search')).at(-1).body);assert.equal(request.rerank,undefined);assert.equal(request.query_text,undefined);assert.equal(storage.size,0);
 for(const [id,value,expected]of [['searchQuestion',' ',/질문을 입력/],['searchTopK','0',/결과 수/]]){
  const previous=get(id).value;get(id).value=value;const calls=requests.length;
  await click('searchRun');assert.match(get('searchStatus').textContent,expected);assert.equal(get('searchStatus').className,'error');assert.equal(get('searchResults').children.length,0);assert.equal(requests.length,calls);get(id).value=previous;
 }
 // Exercise button state while the asynchronous query is in flight, including duplicate clicks.
 pause=true;const clickStarted=new Promise(resolve=>{entered=resolve;});const clicked=click('searchRun');await clickStarted;
 assert.equal(get('searchRun').textContent,'검색 중…');assert.equal(get('searchRun').disabled,true);assert.match(get('searchStatus').textContent,/임베딩하고 있습니다/);assert.equal(get('searchStatus').className,'');
 const calls=requests.length;await click('searchRun');assert.equal(requests.length,calls);
 release();await clicked;pause=false;assert.equal(get('searchRun').textContent,'검색');assert.equal(get('searchRun').disabled,false);assert.match(get('searchStatus').textContent,/재정렬 미적용/);
 // An invalidated request must not replace the new selection's feedback or start retrieval.
 pause=true;const staleStarted=new Promise(resolve=>{entered=resolve;});const staleClick=click('searchRun');await staleStarted;
 const searches=requests.filter(x=>x.url.endsWith('/search')).length;
 run("clearSearch(false);searchFeedback('새 검색 조건')");release();await staleClick;pause=false;
 assert.equal(get('searchStatus').textContent,'새 검색 조건');assert.equal(get('searchResults').children.length,0);assert.equal(requests.filter(x=>x.url.endsWith('/search')).length,searches);
 // Search preparation errors also belong next to the clicked button.
 run('clearSearch()');get('searchIndex').value='';const beforePrepare=requests.length;
 await click('searchRun');assert.match(get('searchStatus').textContent,/인덱스와 버전/);assert.equal(get('searchStatus').className,'error');assert.equal(requests.length,beforePrepare);
 console.log('PASS search UI: explicit Model query call; pinned version across activation; filters; literal provenance/quality; no stored vectors; mismatch and stale request cancellation.');
 console.log('PASS rerank UI: default off; read-only profile selection; candidate_k/top_k; one Knowledge search; distinct scores; error/mismatch never falls back; switching off excludes raw query.');
 console.log('PASS search feedback: actual click handlers; local validation/profile errors; no invalid inference; old results cleared; busy label restored; duplicate click blocked; stale feedback suppressed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
