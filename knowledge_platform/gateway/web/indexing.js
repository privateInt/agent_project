'use strict';
const indexState={current:null,cursor:'',versionCursor:'',pending:null,generation:0,listGeneration:0,versionsGeneration:0,items:new Map(),versions:new Map()};
const indexPendingKey='knowledge.pending.index.v1';
try{const saved=JSON.parse(localStorage.getItem(indexPendingKey)||'null');if(saved&&typeof saved.path==='string'&&saved.path.startsWith('/api/index')&&typeof saved.body==='string'&&['POST','DELETE'].includes(saved.method))indexState.pending=saved;}catch{}
function setIndexPending(value){persistRequest(indexPendingKey,value);indexState.pending=value;$('indexRetry').hidden=!value;uiChanged();}
function clearIndex(){
 indexState.generation++;indexState.versionsGeneration++;indexState.current=null;indexState.versionCursor='';
 indexState.versions.clear();if($('indexDetail'))$('indexDetail').hidden=true;$('indexInfo').replaceChildren();$('indexVersions').replaceChildren();$('indexBuildStatus').textContent='';$('versionsMore').hidden=true;uiChanged();
}
function openedIndex(){
 const i=indexState.current;
 if(!i||$('indexes').value!==i.index_id)throw new Error('선택한 인덱스의 상세를 먼저 여세요.');
 return i;
}
async function indexMutation(path,body,method='POST'){
 if(typeof consoleState!=='undefined'&&(consoleState.flow||consoleState.busy))throw new Error('진행 중인 지식 만들기의 접수를 먼저 확인해 주세요.');
 if(indexState.pending)throw new Error('접수 여부를 확인하지 못한 요청이 있습니다. 동일 요청 다시 전송을 먼저 눌러주세요.');
 setIndexPending({path,body:JSON.stringify(body),method,key:key()});return sendIndexPending();
}
async function sendIndexPending(){
 const p=indexState.pending;if(!p)return;const generation=indexState.generation;
 let out;try{out=await api(p.path,{method:p.method,headers:{'Content-Type':'application/json','Idempotency-Key':p.key},body:p.body});}catch(e){if([400,404,409,413].includes(e.httpStatus)){setIndexPending(null);}throw e;}
 setIndexPending(null);
 const id=out.index_id||indexState.current?.index_id;
 statusAt('listStatus',out.index_version_id?'버전 요청을 접수했습니다. 진행 상태를 확인하고 있습니다.':'요청을 처리했습니다.');
 try{if(generation===indexState.generation){const refresh=loadIndexes(),refreshGeneration=indexState.generation,listGeneration=indexState.listGeneration;await refresh;if(id&&refreshGeneration===indexState.generation&&listGeneration===indexState.listGeneration){$('indexes').value=id;await openIndex(id);}}}catch(e){message('요청은 처리됐지만 목록 조회를 완료하지 못했습니다. 인덱스: '+id,true);}
 return out;
}
async function loadIndexes(more=false){
 const request=++indexState.listGeneration;
 if(!more){clearIndex();indexState.cursor='';indexState.items.clear();$('indexes').value='';if($('indexList'))$('indexList').replaceChildren(el('p','지식을 불러오고 있습니다.','empty'));$('indexesMore').hidden=true;}
 const generation=indexState.generation,q=new URLSearchParams({limit:'20'});if(more)q.set('cursor',indexState.cursor);
 let data;try{data=await api('/api/indexes?'+q);}catch(e){if(request===indexState.listGeneration&&$('indexList'))$('indexList').replaceChildren(el('p','목록을 불러오지 못했습니다. 새로고침을 눌러 다시 확인하세요.','empty'));throw e;}
 if(request!==indexState.listGeneration||generation!==indexState.generation)return;
 for(const i of data.items||[])indexState.items.set(i.index_id,i);renderIndexList();
 indexState.cursor=data.next_cursor||'';$('indexesMore').hidden=!indexState.cursor;
}
async function openIndex(id){
 clearIndex();if(!id)throw new Error('지식 목록에서 항목을 선택하세요.');
 const generation=indexState.generation;
 const i=await api('/api/indexes/'+id);
 if(generation!==indexState.generation||$('indexes').value!==id)return;
 if(i.index_id!==id)throw new Error('인덱스 조회 응답이 선택과 다릅니다.');
 indexState.current=i;
 if($('indexDetail'))$('indexDetail').hidden=false;
 $('indexInfo').replaceChildren(el('h3',i.name),el('p',i.active_version_id?'기본 검색 버전이 지정되어 있습니다.':'완료된 버전에서 ‘검색에 사용’을 눌러주세요.','hint'),details('식별자·관리 정보',i));
 $('indexDelete').disabled=i.state!=='active';if($('addVersion'))$('addVersion').disabled=i.state!=='active';
 await loadIndexVersions();
}
function statusLabel(value){return ({active:'사용 중',protecting:'자료 연결 중',chunking:'내용 나누는 중',queued:'대기 중',indexing:'색인 중',ready:'완료',failed:'실패',deleting:'삭제 중',deleted:'삭제됨',created:'접수됨',running:'처리 중',succeeded:'완료',cancelled:'취소됨',waiting:'대기 중'})[value]||value||'확인 중';}
function renderIndexList(){
 const host=$('indexList');if(!host)return;host.replaceChildren();const q=($('indexFilter')?.value||'').trim().toLowerCase(),include=$('showDeleted')?.checked;
 const rows=[...indexState.items.values()].filter(i=>(include||i.state!=='deleted')&&(!q||i.name.toLowerCase().includes(q)));
 const header=el('div',undefined,'listRow tableHead');for(const label of ['지식 이름','기본 검색 버전','등록일','작업'])header.append(el('span',label));host.append(header);
 if(!rows.length){host.append(el('p',q?'불러온 목록에 일치하는 지식이 없습니다.':'표시할 지식이 없습니다. ‘지식 만들기’로 자료를 추가하세요.','empty'));return;}
 for(const i of rows){const row=el('div',undefined,'listRow');row.append(el('span',i.name,'listName'),el('span',i.state==='active'?(i.active_version_id?'지정됨':'아직 없음'):statusLabel(i.state),'pill'),el('span',String(i.created_at||'').slice(0,10)||'—','hint'));const actions=el('div',undefined,'listActions'),open=el('button','상세'),search=el('button','검색');
 open.onclick=async()=>{if(open.disabled)return;open.disabled=true;try{$('indexes').value=i.index_id;await openIndex(i.index_id);$('indexDetail')?.scrollIntoView?.({block:'nearest'});}catch(e){statusAt('listStatus',friendlyError(e),true);}finally{open.disabled=false;}};
 search.disabled=i.state!=='active'||!i.active_version_id;search.onclick=()=>typeof searchKnowledge==='function'&&searchKnowledge(i.index_id,i.active_version_id);actions.append(open,search);row.append(actions);host.append(row);}
}
async function loadIndexVersions(more=false){
 const i=openedIndex(),generation=indexState.generation,request=++indexState.versionsGeneration;
 const q=new URLSearchParams({limit:'20'});if(more)q.set('cursor',indexState.versionCursor);
 const data=await api(`/api/indexes/${i.index_id}/versions?`+q);if(generation!==indexState.generation||request!==indexState.versionsGeneration)return;
 if((data.items||[]).some(v=>v.index_id!==i.index_id))throw new Error('선택한 지식과 버전 응답이 다릅니다.');
 if(!more){$('indexVersions').replaceChildren();indexState.versions.clear();}
 for(const v of data.items||[]){
  indexState.versions.set(v.index_version_id,v);
  const active=i.active_version_id===v.index_version_id,card=el('article',undefined,'versionCard'+(active?' active':''));
  const progress=v.chunk_count?` · ${v.indexed_count}/${v.chunk_count}개 (${Math.floor(v.indexed_count/v.chunk_count*100)}%)`:'';
  card.append(el('h3',(active?'기본 검색 버전 · ':'')+statusLabel(v.state)+progress),el('p','버전 '+v.index_version_id.slice(0,8),'hint'));
  if(!['ready','failed','deleted'].includes(v.state)){const bar=el('progress');if(v.chunk_count){bar.max=v.chunk_count;bar.value=v.indexed_count||0;}bar.setAttribute?.('aria-label','색인 진행률');card.append(bar);}
  const chunker=state.descriptors.find(d=>d.id===v.input_spec?.chunker?.id);card.append(el('p',`${v.embedding_profile?.repo_id||v.embedding_profile?.profile_id||'모델 확인 중'} · ${chunker?.name||v.input_spec?.chunker?.id||'청크 준비 중'}`,'hint'));
  if(v.error_code)card.append(el('p','작업을 완료하지 못했습니다. '+(v.error_message||v.error_code),'warning'));
  const hnsw=v.indexing_config?.parameters||{m:16,ef_construction:128},info=details('처리 설정·출처·기술 정보',v);info.insertBefore?.(el('p',`HNSW · m ${hnsw.m} · ef_construction ${hnsw.ef_construction}`,'hint'),info.lastChild);card.append(info);
  const row=el('div',undefined,'row'),activate=el('button',active?'검색에 사용 중':'검색에 사용'),remove=el('button','버전 삭제'),inspect=el('button','실행 정보'),search=el('button','이 버전 검색');
  activate.disabled=i.state!=='active'||v.state!=='ready'||active;activate.className=active?'':'primary';
  activate.onclick=async()=>{if(activate.disabled)return;if(!confirm('이 버전을 기본 검색 버전으로 지정할까요? 이후 기본 검색에 새 버전이 사용됩니다.'))return;activate.disabled=true;try{if(openedIndex()!==i)throw new Error('선택한 지식을 다시 확인해 주세요.');await indexMutation(`/api/indexes/${i.index_id}/activate`,{index_version_id:v.index_version_id,expected_active_version_id:i.active_version_id,expected_revision:i.revision});}catch(e){statusAt('indexBuildStatus',friendlyError(e),true);activate.disabled=false;}};
  remove.disabled=i.state!=='active'||['deleted','deleting'].includes(v.state)||active;remove.className='danger';remove.onclick=async()=>{if(remove.disabled)return;if(!confirm('이 버전의 검색용 데이터를 삭제할까요? 진행 중인 작업은 완료되지 않습니다. 원본 자료와 공유 청크는 보존됩니다.'))return;remove.disabled=true;try{if(openedIndex()!==i)throw new Error('선택한 지식을 다시 확인해 주세요.');await indexMutation('/api/index-versions/'+v.index_version_id,{},'DELETE');}catch(e){statusAt('indexBuildStatus',friendlyError(e),true);remove.disabled=false;}};
  inspect.disabled=!v.operation_id;inspect.onclick=async()=>{try{const op=await api('/api/operations/'+v.operation_id);card.append(details('실행 정보',op));}catch(e){statusAt('indexBuildStatus',friendlyError(e),true);}};
  search.disabled=i.state!=='active'||v.state!=='ready'||v.reference_state!=='held';search.onclick=()=>typeof searchKnowledge==='function'&&searchKnowledge(i.index_id,v.index_version_id);
  row.append(activate,remove,inspect,search);card.append(row);$('indexVersions').append(card);
 }
 if(!indexState.versions.size)$('indexVersions').append(el('p','아직 만든 버전이 없습니다. ‘새 버전 만들기’로 자료를 선택하세요.','empty'));
 indexState.versionCursor=data.next_cursor||'';$('versionsMore').hidden=!indexState.versionCursor;uiChanged();
}
function buildHNSW(){
 const options={};
 for(const [id,key,min,max]of [['buildHnswM','m',2,64],['buildHnswEF','ef_construction',16,1024]]){
  const raw=$(id).value.trim();if(!raw)continue;
  const value=Number(raw);
  if(!Number.isInteger(value)||value<min||value>max)throw new Error(`HNSW ${key}는 ${min}~${max}의 정수여야 합니다.`);
  options[key]=value;
 }
 return Object.keys(options).length?options:undefined;
}
action('indexesLoad',()=>loadIndexes());action('indexesMore',()=>loadIndexes(true));action('indexRefresh',()=>openIndex(indexState.current?.index_id));action('indexRetry',sendIndexPending);action('versionsMore',()=>loadIndexVersions(true));
action('indexDelete',async()=>{const i=openedIndex();if(i.state!=='active')throw new Error('이미 삭제 중이거나 삭제된 지식입니다.');if(!confirm('이 지식의 모든 버전과 검색용 데이터를 삭제할까요? 원본 자료와 공유 청크는 보존됩니다.'))return;await indexMutation('/api/indexes/'+i.index_id,{expected_active_version_id:i.active_version_id,expected_revision:i.revision},'DELETE');});
$('indexes').addEventListener('change',clearIndex);
$('indexFilter')?.addEventListener('input',renderIndexList);$('showDeleted')?.addEventListener('change',renderIndexList);
$('indexRetry').hidden=!indexState.pending;uiChanged();
