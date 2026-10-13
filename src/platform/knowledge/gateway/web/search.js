'use strict';
const searchState={selectionRequest:0,prepareRequest:0,rerankerRequest:0,indexListRequest:0,versionRequest:0,indexCursor:'',versionCursor:'',versionIndex:'',versions:new Set(),pin:null,rerankerPin:null,generation:0,abort:null};

function searchFeedback(text,error=false){
 $('searchStatus').textContent=text;$('searchStatus').className=error?'error':'';
}
function searchAction(id,fn,progress,busyLabel='조회 중…'){
 if(!$(id))return;$(id).addEventListener('click',async()=>{
  const button=$(id),label=button.textContent;
  if(button.disabled)return;
  button.disabled=true;button.textContent=busyLabel;
  searchFeedback(progress);
  let generation=searchState.generation;
  try{
   const pending=fn();generation=searchState.generation;
   if(!$('searchStatus').textContent)searchFeedback(progress);
   await pending;
   if(generation===searchState.generation&&$('searchStatus').textContent===progress)searchFeedback('조회가 완료됐습니다. 항목을 선택하세요.');
  }catch(e){
   if(generation===searchState.generation){searchFeedback(friendlyError(e),true);$('searchResults').replaceChildren();}
  }finally{button.disabled=false;button.textContent=label;}
 });
}
function clearSearch(clearPin=true){
 searchState.generation++;
 if(searchState.abort)searchState.abort.abort();
 searchState.abort=null;
 if(clearPin){searchState.prepareRequest++;searchState.pin=null;$('searchProfile').replaceChildren();$('searchDocument').replaceChildren(new Option('이 버전의 전체 자료',''));}
 $('searchResults').replaceChildren();searchFeedback('');
}
async function searchAPI(path,options={}){
 const res=await fetch(path,{cache:'no-store',...options});
 let body;try{body=await res.json();}catch{throw new Error('서버 응답을 읽지 못했습니다.');}
 if(!res.ok){const error=new Error(body.error?.message||(typeof body.error==='string'?body.error:null)||`요청 실패 (${res.status})`);error.httpStatus=res.status;throw error;}
 return body;
}
async function loadSearchIndexes(more=false){
 const request=++searchState.indexListRequest;
 if(!more){clearSearch();$('searchIndex').replaceChildren(new Option('인덱스 선택',''));$('searchVersion').replaceChildren(new Option('버전 선택',''));searchState.versionIndex='';}
 const generation=searchState.generation;
 const q=new URLSearchParams({limit:'20'});if(more)q.set('cursor',searchState.indexCursor);
 const data=await searchAPI('/api/indexes?'+q);if(generation!==searchState.generation||request!==searchState.indexListRequest)return false;
 for(const idx of data.items||[])if(idx.state==='active')$('searchIndex').append(new Option(idx.name,idx.index_id));
 searchState.indexCursor=data.next_cursor||'';$('searchIndexesMore').hidden=!searchState.indexCursor;return true;
}
async function loadSearchVersions(more=false){
 const request=++searchState.versionRequest;
 const id=$('searchIndex').value;if(!id)throw new Error('검색할 인덱스를 선택하세요.');
 if(!more){clearSearch();searchState.versionCursor='';searchState.versionIndex=id;searchState.versions.clear();$('searchVersion').replaceChildren(new Option('버전 선택',''));}
 if(searchState.versionIndex!==id)throw new Error('선택한 인덱스의 버전을 다시 조회하세요.');
 const generation=searchState.generation;
 const idx=await searchAPI('/api/indexes/'+id);
 if(request!==searchState.versionRequest||searchState.versionIndex!==id||$('searchIndex').value!==id)return;
 if(idx.state!=='active')throw new Error('삭제 중이거나 삭제된 인덱스입니다.');
 const q=new URLSearchParams({limit:'20'});if(more)q.set('cursor',searchState.versionCursor);
 const data=await searchAPI(`/api/indexes/${id}/versions?`+q);if(request!==searchState.versionRequest||searchState.versionIndex!==id||$('searchIndex').value!==id)return;
 for(const v of data.items||[]){
  if(v.index_id!==id)throw new Error('선택한 지식과 버전 정보가 다릅니다.');
  if(v.state!=='ready'||v.reference_state!=='held'||searchState.versions.has(v.index_version_id))continue;
  searchState.versions.add(v.index_version_id);
  $('searchVersion').append(new Option(`${idx.active_version_id===v.index_version_id?'기본 검색 버전':'보존된 버전'} · ${v.chunk_count}개 · ${v.index_version_id.slice(0,8)}`,v.index_version_id));
 }
 if(searchState.versions.has(idx.active_version_id)&&!$('searchVersion').value)$('searchVersion').value=idx.active_version_id;
 // The active version may be older than the first list page. Read exactly that version.
 if(!more&&idx.active_version_id&&!searchState.versions.has(idx.active_version_id)){
  const active=await searchAPI('/api/index-versions/'+idx.active_version_id);if(request!==searchState.versionRequest||searchState.versionIndex!==id||$('searchIndex').value!==id)return;
  if(active.index_id!==id||active.index_version_id!==idx.active_version_id)throw new Error('기본 검색 버전이 선택한 지식과 다릅니다.');
  if(active.state==='ready'&&active.reference_state==='held'){searchState.versions.add(active.index_version_id);$('searchVersion').append(new Option('기본 검색 버전 · '+active.chunk_count+'개',active.index_version_id));$('searchVersion').value=active.index_version_id;}
 }
 searchState.versionCursor=data.next_cursor||'';$('searchVersionsMore').hidden=!searchState.versionCursor;
 if(!searchState.versions.size)$('searchStatus').textContent='검색 가능한 버전이 없습니다. 인덱스 빌드 상태를 확인하세요.';
}
async function prepareSearch(){
 clearSearch();const request=searchState.prepareRequest;
 const id=$('searchIndex').value,versionID=$('searchVersion').value;
 if(!id||!versionID)throw new Error('검색할 인덱스와 버전을 선택하세요.');
 const idx=await searchAPI('/api/indexes/'+id);
 const version=await searchAPI('/api/index-versions/'+versionID);
 if(request!==searchState.prepareRequest||$('searchIndex').value!==id||$('searchVersion').value!==versionID)return;
 if(idx.index_id!==id||version.index_version_id!==versionID||idx.state!=='active'||version.index_id!==id||version.state!=='ready'||version.reference_state!=='held')throw new Error('선택한 버전을 검색할 수 없습니다. 버전 상태를 다시 확인하세요.');
 const p=version.embedding_profile;
 if(!p||p.capability!=='embedding'||p.dimension!==1024||!/^es_[a-f0-9]{64}$/.test(p.embedding_space_id))throw new Error('인덱스의 임베딩 프로필이 올바르지 않습니다.');
 searchState.pin={index:idx,version};
 $('searchProfile').replaceChildren(el('p',`${idx.name} · ${version.chunk_count}개 청크 · ${idx.active_version_id===versionID?'활성 버전':'선택한 보존 버전'}`),el('p',`${p.repo_id} · ${p.dimension}차원`,'hint'),details('선택한 버전·임베딩 공간',{index_id:id,index_version_id:versionID,embedding_space_id:p.embedding_space_id,deployment_id:p.deployment_id}));
 $('searchDocument').replaceChildren(new Option('이 버전의 전체 자료',''));
 for(const input of version.input_spec.documents)if(version.canonical_ids.includes(input.canonical_id))$('searchDocument').append(new Option(`${sourceName(input.source_document_id)} · 정리 ${input.canonical_id.slice(0,8)}`,input.canonical_id));
 $('searchStatus').textContent='검색 준비가 됐습니다. 질문을 입력하고 검색을 누르세요.';
}
function queryVector(result,p){
 const rows=result?.data,profile=result?.profile;
 if(result?.role!=='query'||!profile||profile.embedding_space_id!==p.embedding_space_id||profile.dimension!==p.dimension||profile.deployment_id!==p.deployment_id||!Array.isArray(rows)||rows.length!==1||rows[0].id!=='query-1'||rows[0].index!==0)throw new Error('질문 임베딩의 공간·역할·입력 정보가 일치하지 않습니다.');
 const vector=rows[0].embedding;
 if(!Array.isArray(vector)||vector.length!==p.dimension||!vector.every(x=>typeof x==='number'&&Number.isFinite(x))||Math.abs(Math.sqrt(vector.reduce((sum,x)=>sum+x*x,0))-1)>0.001)throw new Error('질문 임베딩 벡터를 검증하지 못했습니다.');
 return vector;
}
function validReranker(p,id){
 return p&&p.deployment_id===id&&p.capability==='reranker'&&p.status==='ready'&&/^rp_[a-f0-9]{64}$/.test(p.reranker_profile_id)&&p.score_kind==='logit'&&p.score_direction==='higher_is_better'&&p.truncation==='reject'&&Number.isInteger(p.max_input_tokens)&&p.max_input_tokens>0&&Number.isInteger(p.max_candidates)&&p.max_candidates>0&&p.max_candidates<=100;
}
async function loadRerankerProfile(){
 clearSearch(false);searchState.rerankerPin=null;$('searchRerankerProfile').replaceChildren();
 const request=++searchState.rerankerRequest,generation=searchState.generation,id=$('searchReranker').value.trim();
 if(!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(id))throw new Error('재정렬 모델을 목록에서 선택해 주세요.');
 const p=await searchAPI('/console/model/deployments/'+id+'/reranker-profile');
 if(request!==searchState.rerankerRequest||$('searchReranker').value!==id)return;
 if(!validReranker(p,id))throw new Error('사용 가능한 재정렬 모델 프로필을 확인하지 못했습니다.');
 searchState.rerankerPin=p;
 $('searchRerankerProfile').append(el('p',`${p.repo_id} · 질문+후보 ${p.max_input_tokens}토큰 · 최대 ${p.max_candidates}개 후보`,'hint'));
 if(generation===searchState.generation)searchFeedback('재정렬 모델을 확인했습니다. 검색을 누르세요.');
}
function renderSearch(data,pin,request={}){
 const v=pin.version;
 if(data.index_id!==pin.index.index_id||data.index_version_id!==v.index_version_id||data.embedding_space_id!==v.embedding_profile.embedding_space_id||!Array.isArray(data.results)||data.returned_count!==data.results.length)throw new Error('검색 결과의 버전·공간·개수가 일치하지 않습니다.');
 const on=request.rerank?.enabled===true,r=data.rerank;
 if(on){
  if(r?.requested!==true||r.deployment_id!==request.rerank.deployment_id||r.reranker_profile_id!==request.rerank.expected_reranker_profile_id||!validReranker(r.profile,r.deployment_id)||r.profile.reranker_profile_id!==r.reranker_profile_id||r.applied!==(data.results.length>0)||r.status!==(data.results.length?'applied':'skipped_no_candidates'))throw new Error('요청한 재정렬의 적용 결과·모델이 일치하지 않습니다.');
  const seen=new Set();let previous=null;
  for(const hit of data.results){
   if(typeof hit.rerank_score!=='number'||!Number.isFinite(hit.rerank_score)||!Number.isInteger(hit.retrieval_rank)||hit.retrieval_rank<1||hit.retrieval_rank>data.candidate_count||seen.has(hit.retrieval_rank)||!Number.isInteger(hit.rerank_input_tokens)||hit.rerank_input_tokens<1||hit.rerank_input_tokens>r.profile.max_input_tokens||previous&&(previous.rerank_score<hit.rerank_score||previous.rerank_score===hit.rerank_score&&previous.retrieval_rank>hit.retrieval_rank))throw new Error('재정렬 점수·순위·입력 한도를 검증하지 못했습니다.');
   seen.add(hit.retrieval_rank);previous=hit;
  }
 }else if(r?.requested!==false||r.applied!==false||r.status!=='disabled')throw new Error('검색의 재정렬 적용 여부가 요청과 다릅니다.');
 const host=$('searchResults');host.replaceChildren();
 $('searchStatus').textContent=`후보 ${data.candidate_count}개 → 결과 ${data.returned_count}개 · 검색 처리 ${data.timings?.total_ms??'?'} ms · ${r.applied?'재정렬 적용':on?'후보 없음 · 재정렬 생략':'재정렬 미적용'}`;
 if(!data.results.length)host.append(el('p','관련 내용을 찾지 못했습니다. 질문이나 자료 범위를 바꿔보세요.','empty'));
 if(data.results.length)host.append(el('p',r.applied?'재정렬 점수가 클수록 관련성이 높습니다. 벡터 점수와 다른 척도이며 확률이 아닙니다.':'벡터 점수가 클수록 가깝습니다. 정답 확률은 아닙니다.','hint'));
 for(const hit of data.results){
  const card=el('article',undefined,'card');card.append(el('h3',`${hit.rank}. ${sourceName(hit.source_document_id)} · ${({text:'본문',table:'표',code:'코드'})[hit.kind]||hit.kind}`),el('p',hit.text,'chunkText'));
  const scores=details('관련성 점수·순위',{vector_score:hit.retrieval_score,vector_rank:hit.retrieval_rank,rerank_score:hit.rerank_score,final_rank:hit.rank,input_tokens:hit.rerank_input_tokens});card.append(scores);
  if(hit.quality?.state!=='ready')card.append(el('p','원본 품질: '+(hit.quality?.state||'확인 필요'),'warning'));
  card.append(details('출처·원본 구조',{chunk_id:hit.chunk_id,source_document_id:hit.source_document_id,canonical_id:hit.canonical_id,source_spans:hit.source_spans,structure:hit.structure,quality:hit.quality}));host.append(card);
 }
 host.append(details('검색 조건·처리 정보',{index_version_id:data.index_version_id,embedding_space_id:data.embedding_space_id,filters:data.filters,candidate_count:data.candidate_count,returned_count:data.returned_count,score_semantics:data.score_semantics,rerank:data.rerank,timings:data.timings}));
}
async function runSearch(){
 clearSearch(false);
 if(!searchState.pin){const pending=prepareSearch(),generation=searchState.generation;await pending;if(generation!==searchState.generation)return;}
 const pin=searchState.pin;if(!pin)return;
 const text=$('searchQuestion').value,topK=Number($('searchTopK').value);
 if(!text.trim())throw new Error('질문을 입력하세요.');
 if(!Number.isInteger(topK)||topK<1||topK>50)throw new Error('결과 수는 1~50의 정수여야 합니다.');
 const rerankOn=$('searchRerank').checked===true,candidateK=rerankOn?Number($('searchCandidateK').value):topK;
 let rerank;
 if(rerankOn){
  const p=searchState.rerankerPin,id=$('searchReranker').value.trim();
  if(!validReranker(p,id))throw new Error('재정렬 모델을 선택하고 모델 확인이 끝난 뒤 검색해 주세요.');
  if(!Number.isInteger(candidateK)||candidateK<topK||candidateK>p.max_candidates)throw new Error('후보 수는 결과 수 이상, 모델의 최대 후보 수 이하여야 합니다.');
  if(new TextEncoder().encode(text).length>8192||text.includes('\0'))throw new Error('재정렬 질문은 8KiB 이내여야 하며 NUL 문자를 포함할 수 없습니다.');
  rerank={enabled:true,deployment_id:id,expected_reranker_profile_id:p.reranker_profile_id};
 }
 const filters={};if($('searchDocument').value)filters.canonical_ids=[$('searchDocument').value];if($('searchKind').value)filters.kinds=[$('searchKind').value];
 const generation=searchState.generation,abort=new AbortController();searchState.abort=abort;
 const p=pin.version.embedding_profile,path='/console/model/deployments/'+p.deployment_id;
 try{
  $('searchStatus').textContent='질문 임베딩 모델을 확인하고 있습니다.';
  const current=await searchAPI(path+'/embedding-profile',{signal:abort.signal});
  if(generation!==searchState.generation)return;
  if(current.embedding_space_id!==p.embedding_space_id||current.dimension!==p.dimension||current.capability!=='embedding'||current.status!=='ready'||current.deployment_id!==p.deployment_id)throw new Error('모델이 준비되지 않았거나 인덱스와 임베딩 공간이 다릅니다.');
  $('searchStatus').textContent='질문을 임베딩하고 있습니다.';
  const embedded=await searchAPI(path+'/embeddings',{method:'POST',signal:abort.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_embedding_space_id:p.embedding_space_id,role:'query',inputs:[{id:'query-1',text}]})});
  if(generation!==searchState.generation)return;
  const vector=queryVector(embedded,p);
  $('searchStatus').textContent=rerankOn?'후보 검색과 재정렬을 진행하고 있습니다.':'선택한 버전에서 관련 청크를 찾고 있습니다.';
  const request={index_version_id:pin.version.index_version_id,embedding_space_id:p.embedding_space_id,query_embedding:vector,top_k:topK,candidate_k:candidateK,filters,...(rerankOn?{query_text:text,rerank}:{})};
  const data=await searchAPI(`/api/indexes/${pin.index.index_id}/search`,{method:'POST',signal:abort.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
  if(generation!==searchState.generation)return;
  renderSearch(data,pin,request);
 }catch(e){if(generation!==searchState.generation)return;searchFeedback('검색 실패: '+e.message,true);$('searchResults').replaceChildren();throw e;}
 finally{if(generation===searchState.generation)searchState.abort=null;}
}
searchAction('searchIndexesLoad',()=>loadSearchIndexes(),'검색 인덱스를 조회하고 있습니다.');
searchAction('searchIndexesMore',()=>loadSearchIndexes(true),'검색 인덱스를 더 조회하고 있습니다.');
searchAction('searchVersionsLoad',async()=>{await loadSearchVersions();if($('searchVersion').value)await prepareSearch();},'검색 버전을 조회하고 있습니다.');
searchAction('searchVersionsMore',()=>loadSearchVersions(true),'검색 버전을 더 조회하고 있습니다.');

searchAction('searchRun',runSearch,'검색 조건을 확인하고 있습니다.','검색 중…');
$('searchIndex').addEventListener('change',async()=>{
 const request=++searchState.selectionRequest;clearSearch();searchState.versionIndex='';$('searchVersion').replaceChildren(new Option('버전 선택',''));$('searchVersionsMore').hidden=true;
 if(!$('searchIndex').value)return;
 try{searchFeedback('검색할 버전을 확인하고 있습니다.');await loadSearchVersions();if(request!==searchState.selectionRequest)return;if($('searchVersion').value)await prepareSearch();else searchFeedback('기본 검색 버전이 없습니다. 검색 버전 변경에서 완료된 버전을 선택하세요.');}catch(e){if(request===searchState.selectionRequest)searchFeedback(friendlyError(e),true);}
});
$('searchVersion').addEventListener('change',async()=>{const request=++searchState.selectionRequest;clearSearch();if(!$('searchVersion').value)return;try{await prepareSearch();}catch(e){if(request===searchState.selectionRequest)searchFeedback(friendlyError(e),true);}});
for(const id of ['searchQuestion','searchTopK','searchDocument','searchKind','searchCandidateK'])$(id).addEventListener('input',()=>clearSearch(false));
$('searchRerank').addEventListener('change',async()=>{
 clearSearch(false);const on=$('searchRerank').checked;$('searchRerankOptions').hidden=!on;if($('candidateOption'))$('candidateOption').hidden=!on;
 if(on&&typeof loadModels==='function'&&!consoleState.modelLoaded){try{searchFeedback('재정렬 모델을 조회하고 있습니다.');await loadModels();if($('searchRerank').checked)searchFeedback('사용할 재정렬 모델을 선택하세요.');}catch(e){if($('searchRerank').checked)searchFeedback(friendlyError(e),true);}}
 else if(on&&!searchState.rerankerPin)searchFeedback('사용할 재정렬 모델을 선택하세요.');
});
function clearReranker(){searchState.rerankerRequest++;clearSearch(false);searchState.rerankerPin=null;$('searchRerankerProfile').replaceChildren();}
$('searchReranker').addEventListener('input',clearReranker);
$('searchReranker').addEventListener('change',async()=>{clearReranker();if(!$('searchReranker').value)return;const pending=loadRerankerProfile(),generation=searchState.generation;try{await pending;}catch(e){if(generation===searchState.generation)searchFeedback(friendlyError(e),true);}});
