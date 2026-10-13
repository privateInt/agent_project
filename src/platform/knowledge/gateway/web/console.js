'use strict';
// Console navigation and explicit create workflow. Page entry and polling only read.
const consoleState={page:'list',step:1,target:null,flow:null,busy:false,polling:false,loadedSources:false,savedIDs:new Set(),savedRun:'',modelCursor:'',modelGeneration:0,models:new Map(),modelLoaded:false,modelFailures:0,modelJobs:new Map()};
const flowKey='knowledge.console.create.v1';
function persistFlow(value){
 // A durable request key must exist before either mutation is sent.
 if(value)localStorage.setItem(flowKey,JSON.stringify(value));else localStorage.removeItem(flowKey);
 consoleState.flow=value;syncConsole();
}
try{
 const f=JSON.parse(localStorage.getItem(flowKey)||'null');
 if(f){if(f.schema!==1||!['index','build'].includes(f.stage)||typeof f.buildBody!=='string'||typeof f.indexBody!=='string'||!validUUID(f.indexKey)||!validUUID(f.buildKey)||f.stage==='build'&&!validUUID(f.indexID))throw new Error('invalid saved workflow');consoleState.flow=f;}
}catch{consoleState.brokenFlow=true;}
function syncConsole(){
 const pending=Boolean(consoleState.flow),blocked=pending||consoleState.busy||consoleState.brokenFlow;
 $('recovery').hidden=!(pending||state.pending||indexState.pending||consoleState.brokenFlow);
 $('flowRetry').hidden=!pending;$('flowRetry').disabled=consoleState.busy;
 const managed=indexState.current?.state==='active';$('indexDelete').disabled=!managed||blocked||Boolean(indexState.pending);$('addVersion').disabled=!managed||blocked;$('indexRefresh').disabled=!indexState.current||actionWorking.has('indexRefresh');
 $('startKnowledge').disabled=blocked||Boolean(state.pending||indexState.pending)||actionWorking.has('startKnowledge');
 if(consoleState.brokenFlow)statusAt('createStatus','저장된 접수 확인 정보를 읽지 못했습니다. 새 작업을 시작하기 전에 지식 목록에서 이전 작업 상태를 확인해 주세요.',true);
 $('create').disabled=blocked||Boolean(state.pending)||actionWorking.has('create');
}
async function showPage(page){
 consoleState.page=page;
 for(const [name,panel,tab]of [['list','listPanel','tabList'],['create','createPanel','tabCreate'],['search','searchPanel','tabSearch']]){ $(panel).hidden=name!==page;if(name===page)$(tab).setAttribute('aria-current','page');else $(tab).removeAttribute('aria-current'); }
 message('');
 if(page==='search'&&!$('searchIndex').options.length)await loadSearchIndexes();
}
function step(number){
 consoleState.step=number;
 for(let n=1;n<=3;n++){$('createStep'+n).hidden=n!==number;$('stepLabel'+n).className=n===number?'current':'';if(n===number)$('stepLabel'+n).setAttribute('aria-current','step');else $('stepLabel'+n).removeAttribute('aria-current');}
 $('stepBack').hidden=number===1;$('stepNext').hidden=number===3;$('startKnowledge').hidden=number!==3;$('stepNext').textContent=number===1?'처리 설정으로':'내용 확인';statusAt('createStatus','');syncConsole();
}
async function startWizard(target=null){
 consoleState.target=target;consoleState.wizardStarted=true;
 $('createTitle').textContent=target?'새 버전 만들기':'지식 만들기';$('createSubtitle').textContent=target?target.name+'에 새 버전을 추가합니다. 기존 검색 버전은 유지됩니다.':'자료와 모델을 선택하면 청크 생성부터 색인까지 순서대로 진행합니다.';
 $('indexName').value=target?.name||'';$('indexName').disabled=Boolean(target);
 $('indexInput').value='new';$('buildHnswM').value='';$('buildHnswEF').value='';
 $('lineageOptions').hidden=!target;$('buildParent').replaceChildren(new Option('연결하지 않음',''));
 if(target)for(const v of indexState.versions.values())if(v.index_id===target.index_id&&v.state==='ready')$('buildParent').append(new Option('버전 '+v.index_version_id.slice(0,8)+(target.active_version_id===v.index_version_id?' · 기본 검색':''),v.index_version_id));
 step(1);await showPage('create');await inputMode();
}
async function inputMode(){
 const saved=$('indexInput').value==='saved';$('sourcePicker').hidden=saved;$('savedPicker').hidden=!saved;$('chunkerSettings').hidden=saved;
 if(saved){if(!$('runs').options.length||$('runs').options.length===1)await runs();renderSavedDocuments();}
 else if(!consoleState.loadedSources){await sources();consoleState.loadedSources=true;}
}
function renderSavedDocuments(){
 const run=state.current,host=$('savedDocuments');host.replaceChildren();
 if(!run?.result_registered||run.chunking_run_id!==$('runs').value){$('savedStatus').textContent='완료된 청크 결과를 선택하세요.';return;}
 if(consoleState.savedRun!==run.chunking_run_id){consoleState.savedRun=run.chunking_run_id;consoleState.savedIDs=new Set(run.spec.documents.map(d=>d.canonical_id));}
 $('savedStatus').textContent=run.chunk_count+'개 청크 · 포함할 자료를 선택하세요.';
 for(const d of run.spec.documents){const label=el('label',undefined,'check'),box=el('input');box.type='checkbox';box.checked=consoleState.savedIDs.has(d.canonical_id);box.onchange=()=>{if(box.checked)consoleState.savedIDs.add(d.canonical_id);else consoleState.savedIDs.delete(d.canonical_id);};label.append(box,el('span',sourceName(d.source_document_id)+' · 정리 '+d.canonical_id.slice(0,8)));host.append(label);}
}
function validateInputs(){
 if($('indexInput').value==='saved'){
  const r=state.current;if(!r?.result_registered||r.chunking_run_id!==$('runs').value)throw new Error('완료된 청크 결과를 선택해 주세요.');requireUUID(r.chunking_run_id,'청크 작업');
  if(!consoleState.savedIDs.size)throw new Error('색인에 포함할 자료를 한 개 이상 선택해 주세요.');
  const allowed=new Set(r.spec.documents.map(d=>d.canonical_id));for(const id of consoleState.savedIDs)if(!validUUID(id)||!allowed.has(id))throw new Error('선택한 청크 결과의 자료를 다시 선택해 주세요.');
 }else{if(!state.selected.size)throw new Error('자료 목록에서 정리된 결과를 추가해 주세요.');for(const id of state.selected.keys())requireUUID(id,'정리된 자료');}
}
function createPayload(){
 validateInputs();const name=$('indexName').value.trim();if(!name||Array.from(name).length>120)throw new Error('지식 이름을 1~120자로 입력해 주세요.');
 const deployment=requireUUID($('indexDeployment').value,'임베딩 모델'),model=consoleState.models.get(deployment);
 if(!model?.embedding)throw new Error('사용 가능한 임베딩 모델을 선택해 주세요.');
 const body={embedding_deployment_id:deployment};
 if($('indexInput').value==='saved'){body.chunking_run_id=state.current.chunking_run_id;body.canonical_ids=[...consoleState.savedIDs];}
 else body.chunking=selectedChunking(name);
 const hnsw=buildHNSW();if(hnsw)body.hnsw=hnsw;
 if($('buildParent').value){if(!consoleState.target)throw new Error('이전 버전 연결은 기존 지식에서만 사용할 수 있습니다.');body.parent_version_id=requireUUID($('buildParent').value,'부모 버전');}
 return {name,deployment,body,model};
}
function reviewCreate(){
 const p=createPayload(),host=$('createReview');host.replaceChildren();
 const lines=[['지식',p.name+(consoleState.target?' · 새 버전':' · 새 지식')],['자료',$('indexInput').value==='saved'?consoleState.savedIDs.size+'개 · 저장된 청크 재사용':[...state.selected.values()].map(d=>d.name).join(', ')],['임베딩 모델',p.model.label],['내용 나누기',p.body.chunking?state.descriptors.find(d=>d.id===p.body.chunking.chunker.id)?.name||p.body.chunking.chunker.id:'저장된 청크 그대로 사용'],['HNSW','m '+(p.body.hnsw?.m??16)+' / ef_construction '+(p.body.hnsw?.ef_construction??128)]];
 for(const [label,value]of lines){const row=el('div',undefined,'reviewLine');row.append(el('span',label),el('span',value));host.append(row);}
 if(p.body.chunking)host.append(el('p',`청크 최대 ${p.body.chunking.chunker.options.max_characters}자 · 겹침 ${p.body.chunking.chunker.options.overlap_characters}자`,'hint'));
}
async function advanceStep(){
 if(consoleState.step===1){validateInputs();step(2);if(!consoleState.modelLoaded)await loadModels();if(consoleState.target&&consoleState.models.has(consoleState.target.embedding_deployment_id))$('indexDeployment').value=consoleState.target.embedding_deployment_id;}
 else{reviewCreate();step(3);}
}
async function beginKnowledge(){
 if(consoleState.busy||consoleState.flow||consoleState.brokenFlow||indexState.pending||state.pending)throw new Error('접수 확인이 필요한 이전 작업을 먼저 확인해 주세요.');
 const p=createPayload();
 persistFlow({schema:1,stage:consoleState.target?'build':'index',indexID:consoleState.target?.index_id||'',name:p.name,indexBody:JSON.stringify({name:p.name,embedding_deployment_id:p.deployment}),buildBody:JSON.stringify(p.body),indexKey:key(),buildKey:key()});
 await resumeKnowledge();
}
async function resumeKnowledge(){
 if(consoleState.busy||!consoleState.flow)return;consoleState.busy=true;syncConsole();const f=consoleState.flow;
 try{
  if(f.stage==='index'){
   statusAt('createStatus','지식을 등록하고 있습니다.');
   const i=await api('/api/indexes',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':f.indexKey},body:f.indexBody});requireUUID(i.index_id,'생성된 지식');
   persistFlow({...f,stage:'build',indexID:i.index_id});
  }
  const pending=consoleState.flow;statusAt('createStatus','자료 처리와 색인을 요청하고 있습니다.');
  const version=await api(`/api/indexes/${pending.indexID}/builds`,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':pending.buildKey},body:pending.buildBody});
  if(version.index_id!==pending.indexID||!validUUID(version.index_version_id))throw new Error('접수 응답을 확인하지 못했습니다. 이어서 확인을 눌러주세요.');
  const id=pending.indexID;persistFlow(null);consoleState.wizardStarted=false;await showPage('list');
  statusAt('listStatus','생성을 접수했습니다. 아래에서 진행 상황을 자동으로 확인합니다. 완료 후 ‘검색에 사용’을 눌러주세요.');
  try{await loadIndexes();$('indexes').value=id;await openIndex(id);}catch{statusAt('listStatus','생성은 접수됐습니다. 목록을 새로고침해 진행 상황을 확인해 주세요.',true);}
 }catch(e){
  if([400,404,409,413].includes(e.httpStatus)){
   const created=consoleState.flow?.stage==='build',id=consoleState.flow?.indexID;
   persistFlow(null);
   // Keep a successfully created logical index and retry a NEW validated build there.
   if(created){consoleState.target={index_id:id,name:f.name,embedding_deployment_id:JSON.parse(f.indexBody).embedding_deployment_id};$('indexName').disabled=true;}
   statusAt('createStatus',friendlyError(e)+(created?'\n등록된 지식은 보존됩니다. 설정을 수정하면 이 지식에 새 버전을 요청합니다.':''),true);
  }else{statusAt('createStatus',friendlyError(e)+'\n접수 여부를 확인하지 못했습니다. 상단의 ‘지식 만들기 이어서 확인’을 눌러주세요.',true);}
 }finally{consoleState.busy=false;syncConsole();}
}
function renderModelOptions(){
 for(const [id,cap]of [['indexDeployment','embedding'],['searchReranker','reranker']]){
  const previous=$(id).value;$(id).replaceChildren(new Option('사용할 모델 선택',''));
  for(const [key,item]of consoleState.models)if(item[cap])$(id).append(new Option(item.label+' · '+key.slice(0,8),key));
  if(consoleState.models.get(previous)?.[cap])$(id).value=previous;
 }
 $('modelsMore').hidden=!consoleState.modelCursor;$('searchModelsMore').hidden=!consoleState.modelCursor;
}
async function modelIdentity(deployment){
 const v=await api('/console/model/model-versions/'+requireUUID(deployment.model_version_id,'모델 버전'));
 if(v.model_version_id!==deployment.model_version_id)throw new Error('모델 버전 응답이 다릅니다.');
 const m=await api('/console/model/models/'+requireUUID(v.model_id,'등록 모델'));if(m.model_id!==v.model_id)throw new Error('등록 모델 응답이 다릅니다.');
 return {label:(m.name||v.repo_id||'모델')+' · '+(v.version||'원본 버전'),repo:v.repo_id};
}
async function loadModels(more=false){
 const generation=++consoleState.modelGeneration;
 if(!more){consoleState.models.clear();consoleState.modelCursor='';consoleState.modelFailures=0;consoleState.modelLoaded=false;searchState.rerankerRequest++;searchState.rerankerPin=null;$('searchRerankerProfile').replaceChildren();renderModelOptions();}
 statusAt('modelsStatus','사용 가능한 모델을 확인하고 있습니다.');
 const q=new URLSearchParams({limit:'20'});if(more)q.set('cursor',consoleState.modelCursor);
 let data;try{data=await api('/console/model/deployments?'+q);}catch(e){if(generation===consoleState.modelGeneration)statusAt('modelsStatus','모델 목록을 불러오지 못했습니다. 모델 플랫폼 연결을 확인하고 다시 조회해 주세요.',true);throw e;}
 const rows=(data.items||[]).filter(d=>validUUID(d.deployment_id)&&d.status==='ready'&&d.desired_state==='running');
 // Four workers bound profile/identity reads. Capability is determined by Model's profile,
 // never guessed from the registered type (older embedding/reranker entries may be llm).
 let offset=0;const discovered=[];let failures=0;
 async function worker(){while(offset<rows.length&&generation===consoleState.modelGeneration){const d=rows[offset++];const item={deployment:d,label:'',embedding:null,reranker:null};
  for(const cap of ['embedding','reranker'])try{const profile=await api('/console/model/deployments/'+d.deployment_id+'/'+cap+'-profile');
   if(cap==='embedding'&&profile.deployment_id===d.deployment_id&&profile.capability==='embedding'&&profile.status==='ready'&&profile.dimension===1024&&/^es_[a-f0-9]{64}$/.test(profile.embedding_space_id))item.embedding=profile;
   if(cap==='reranker'&&validReranker(profile,d.deployment_id))item.reranker=profile;
  }catch(e){if(![400,404,409].includes(e.httpStatus))failures++;}
  if(item.embedding||item.reranker){try{Object.assign(item,await modelIdentity(d));}catch{item.label=(item.embedding||item.reranker).repo_id||'배포 모델';failures++;}discovered.push(item);}
 }}
 await Promise.all(Array.from({length:Math.min(4,rows.length)},worker));if(generation!==consoleState.modelGeneration)return;
 for(const item of discovered.sort((a,b)=>a.label.localeCompare(b.label)))consoleState.models.set(item.deployment.deployment_id,item);
 consoleState.modelFailures+=failures;consoleState.modelCursor=data.next_cursor||'';consoleState.modelLoaded=true;renderModelOptions();
 const embedding=[...consoleState.models.values()].filter(m=>m.embedding).length,reranker=[...consoleState.models.values()].filter(m=>m.reranker).length;
 statusAt('modelsStatus',`임베딩 ${embedding}개 · 재정렬 ${reranker}개 확인`+(consoleState.modelFailures?' · 일부 모델 정보를 확인하지 못했습니다. 새로고침으로 다시 확인해 주세요.':'')+(!embedding&&!reranker?' · 모델 플랫폼에서 해당 모델을 배포해 주세요.':''));
}
async function searchKnowledge(indexID,versionID=''){
 const request=++searchState.selectionRequest;await showPage('search');searchFeedback('검색할 지식을 준비하고 있습니다.');
 try{
  if(await loadSearchIndexes()===false||request!==searchState.selectionRequest)return;if(!Array.from($('searchIndex').options).some(o=>o.value===indexID)){
   const i=await api('/api/indexes/'+requireUUID(indexID,'검색할 지식'));if(i.state!=='active')throw new Error('삭제 중이거나 삭제된 지식입니다.');$('searchIndex').append(new Option(i.name,i.index_id));}
  if(request!==searchState.selectionRequest)return;$('searchIndex').value=indexID;await loadSearchVersions();if(request!==searchState.selectionRequest)return;
  if(versionID){if(!Array.from($('searchVersion').options).some(o=>o.value===versionID))$('searchVersion').append(new Option('선택한 버전 · '+versionID.slice(0,8),versionID));$('searchVersion').value=versionID;}
  if($('searchVersion').value)await prepareSearch();else searchFeedback('기본 검색 버전이 없습니다. 검색 버전 변경에서 완료된 버전을 선택해 주세요.');
  $('searchQuestion').focus();
 }catch(e){if(request===searchState.selectionRequest)searchFeedback(friendlyError(e),true);}
}
async function pollConsole(){
 if(document.hidden||consoleState.polling||consoleState.busy||indexState.pending)return;
 consoleState.polling=true;
 try{
  if(consoleState.page==='list'&&indexState.current&&[...indexState.versions.values()].some(v=>!['ready','failed','deleted'].includes(v.state))){await loadIndexVersions();}
  if(consoleState.page==='create'&&state.current&&!state.current.result_registered&&!['failed','cancelled'].includes(state.current.operation_status)){await openRun(state.current.chunking_run_id);if($('indexInput').value==='saved')renderSavedDocuments();}
 }catch(e){statusAt(consoleState.page==='list'?'indexBuildStatus':'createStatus','상태를 새로 확인하지 못했습니다. ‘상태 새로고침’으로 다시 확인해 주세요.',true);}finally{consoleState.polling=false;}
}
for(const [id,page]of [['tabList','list'],['tabCreate','create'],['tabSearch','search']])action(id,async()=>{if(page==='create'){if(consoleState.wizardStarted)await showPage('create');else await startWizard();}else{await showPage(page);if(page==='list')await loadIndexes();else await loadSearchIndexes();}});
action('newKnowledge',()=>startWizard());action('addVersion',()=>startWizard(openedIndex()));action('cancelCreate',()=>showPage('list'));action('closeIndex',async()=>clearIndex());
action('stepNext',advanceStep);action('stepBack',async()=>step(Math.max(1,consoleState.step-1)));action('startKnowledge',beginKnowledge);action('flowRetry',resumeKnowledge);
action('modelsRefresh',()=>loadModels());action('modelsMore',()=>loadModels(true));
for(const [id,more]of [['searchModelsRefresh',false],['searchModelsMore',true]])searchAction(id,async()=>{await loadModels(more);searchFeedback('사용 가능한 재정렬 모델을 선택하세요.');},'사용 가능한 모델을 조회하고 있습니다.');
$('indexInput').addEventListener('change',async()=>{try{await inputMode();}catch(e){statusAt('createStatus',friendlyError(e),true);}});
$('runs').addEventListener('change',async()=>{state.runGeneration++;state.current=null;renderSavedDocuments();const id=$('runs').value;if(!id)return;try{await openRun(id);if($('runs').value===id)renderSavedDocuments();}catch(e){if($('runs').value===id)statusAt('createStatus',friendlyError(e),true);}});
$('group').addEventListener('change',()=>sources().catch(e=>statusAt('createStatus',friendlyError(e),true)));
$('query').addEventListener('keydown',e=>{if(e.key==='Enter')$('sourcesLoad').click();});
selection();syncConsole();loadIndexes().catch(e=>statusAt('listStatus',friendlyError(e),true));
setInterval(pollConsole,5000);
