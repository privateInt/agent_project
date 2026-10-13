'use strict';
const $=id=>document.getElementById(id);
const state={selected:new Map(),descriptors:[],sourceCursor:'',groupCursor:'',runCursor:'',chunkCursor:'',current:null,pending:null,comparison:null,sourceNames:new Map(),sourceGeneration:0,sourceRequest:0,runGeneration:0};
const pendingKey='knowledge.pending.create.v1';
try{const saved=JSON.parse(localStorage.getItem(pendingKey)||'null');if(saved&&typeof saved.key==='string'&&typeof saved.body==='string')state.pending=saved;}catch{}
function persistRequest(storageKey,value){
 try{if(value)localStorage.setItem(storageKey,JSON.stringify(value));else localStorage.removeItem(storageKey);}
 catch{throw new Error('요청 확인 정보를 브라우저에 저장하지 못했습니다. 브라우저 저장 공간·설정을 확인한 뒤 다시 시도해 주세요.');}
}
function setPreviewPending(value){persistRequest(pendingKey,value);state.pending=value;$('retry').hidden=!value;uiChanged();}
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function message(text,error=false){$('message').textContent=text;$('message').className=error?'error':'';}
async function api(path,options={}){const response=await fetch(path,{cache:'no-store',...options});let body;try{body=await response.json();}catch{throw new Error('서버 응답을 읽을 수 없습니다. 같은 요청을 다시 전송할 수 있습니다.');}if(!response.ok){const error=new Error(body.error?.message||`요청 실패 (${response.status})`);error.httpStatus=response.status;throw error;}return body;}
function friendlyError(error){
 const text=error?.message||String(error);
 if(text.includes('canonical nonzero UUID is required'))return '선택 정보가 올바르지 않습니다. 자료·모델·버전을 목록에서 다시 선택해 주세요.';
 return text;
}
function uiChanged(){if(typeof syncConsole==='function')syncConsole();}
function statusAt(id,text,error=false){const host=$(id);if(host){host.textContent=text;host.className=error?'error':'';}}
const actionWorking=new Set();
function action(id,fn){const node=$(id);if(!node)return;node.addEventListener('click',async()=>{const b=$(id);if(b.disabled)return;const label=b.textContent;actionWorking.add(id);b.disabled=true;b.textContent='처리 중…';const target=b.closest?.('[data-feedback]')?.dataset.feedback||'message';statusAt(target,'처리하고 있습니다.');try{await fn();if($(target)?.textContent==='처리하고 있습니다.')statusAt(target,'');}catch(e){statusAt(target,friendlyError(e),true);}finally{actionWorking.delete(id);b.disabled=false;if(b.textContent==='처리 중…')b.textContent=label;uiChanged();}});}
function validUUID(id){return typeof id==='string'&&/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(id)&&id!=='00000000-0000-0000-0000-000000000000';}
function requireUUID(id,label){if(!validUUID(id))throw new Error(label+'을(를) 목록에서 선택해 주세요.');return id;}
function sourceName(id){return state.sourceNames.get(id)||'자료 '+String(id).slice(0,8);}
function chunkOptions(){const max=Number($('maxCharacters').value),overlap=Number($('overlap').value);if(!Number.isInteger(max)||!Number.isInteger(overlap)||max<100||max>8000||overlap<0||overlap*2>=max)throw new Error('최대 글자 수는 100~8000, 겹침은 최대 글자 수의 절반 미만이어야 합니다.');return {max_characters:max,overlap_characters:overlap};}
function selectedChunking(name){if(!state.selected.size)throw new Error('정리된 자료를 먼저 추가해 주세요.');const ids=[...state.selected.keys()];ids.forEach(id=>requireUUID(id,'정리된 자료'));if(!state.descriptors.some(d=>d.id===$('chunker').value))throw new Error('내용을 나누는 방식을 선택해 주세요.');return {name,canonical_ids:ids,chunker:{id:$('chunker').value,options:chunkOptions()}};}

function details(title,value){const d=el('details');d.append(el('summary',title),el('pre',JSON.stringify(value,null,2)));return d;}
function selection(){
 const host=$('selected');host.replaceChildren();$('selectionCount').textContent=state.selected.size;
 if(!state.selected.size)host.append(el('p','아직 선택한 자료가 없습니다. 위 목록에서 자료를 추가하세요.','hint'));
 for(const [id,item]of state.selected){const row=el('div',undefined,'selectedRow');row.append(el('span',item.name+' · '+(item.versionLabel||'정리 결과 '+id.slice(0,8))));const remove=el('button','선택 해제');remove.onclick=()=>{state.selected.delete(id);selection();};row.append(remove);host.append(row);}
 uiChanged();
}
async function groups(more=false){const q=new URLSearchParams({limit:'100'});if(more)q.set('cursor',state.groupCursor);const data=await api('/api/source-groups?'+q);if(!more)$('group').replaceChildren(new Option('모든 자료',''));for(const g of data.items||[])$('group').append(new Option(g.name,g.group_id));state.groupCursor=data.next_cursor||'';$('groupMore').hidden=!state.groupCursor;}
async function sources(more=false){
 if(!more)state.sourceGeneration++;const generation=state.sourceGeneration,request=++state.sourceRequest;
 const q=new URLSearchParams({q:$('query').value,group_id:$('group').value,limit:'20'});if(more)q.set('cursor',state.sourceCursor);
 if(!more){$('sources').replaceChildren(el('p','자료를 불러오고 있습니다.','hint'));state.sourceCursor='';$('sourcesMore').hidden=true;}
 let data;try{data=await api('/api/sources?'+q);}catch(e){if(generation===state.sourceGeneration)$('sources').replaceChildren(el('p','자료를 불러오지 못했습니다. 자료 조회를 다시 눌러주세요.','warning'));throw e;}
 if(generation!==state.sourceGeneration||request!==state.sourceRequest)return;
 if(!more)$('sources').replaceChildren();
 for(const source of data.items||[]){
  if(!validUUID(source.document_id))continue;
  const name=source.name||source.original_filename||'이름 없는 자료';state.sourceNames.set(source.document_id,name);
  const row=el('div',undefined,'sourceRow'),head=el('div',undefined,'sourceTitle'),button=el('button','추가');head.append(el('strong',name),button);row.append(head);
  const host=el('div',undefined,'sourceVersions'),select=el('select'),choose=el('button','이 정리 결과 추가'),moreButton=el('button','정리 결과 더 보기'),status=el('p',undefined,'hint');select.setAttribute?.('aria-label',name+'의 정리 결과');host.hidden=true;row.append(host);let cursor='',loaded=false,loading=false;const versions=new Map();
  const load=async(moreVersions=false)=>{
   if(loading)return;loading=true;button.disabled=true;moreButton.disabled=true;host.hidden=false;status.textContent='정리 결과를 확인하고 있습니다.';
   if(!loaded)host.replaceChildren(status);
   try{
    const query=new URLSearchParams({limit:'20'});if(moreVersions)query.set('cursor',cursor);
    const out=await api(`/api/sources/${source.document_id}/canonical-versions?`+query);if(generation!==state.sourceGeneration)return;
    if(!moreVersions){versions.clear();select.replaceChildren();}
    for(const run of out.items||[]){if(!validUUID(run.output_document_id)||run.deleted_at||run.purged_at||run.purge_state)continue;versions.set(run.output_document_id,run);select.append(new Option(`${run.name||'정리 결과'} · ${String(run.created_at||'').slice(0,16).replace('T',' ')} · ${run.output_document_id.slice(0,8)}`,run.output_document_id));}
    cursor=out.next_cursor||'';moreButton.hidden=!cursor;loaded=true;choose.disabled=!versions.size;select.hidden=!versions.size;choose.hidden=!versions.size;
    status.textContent=versions.size?'사용할 정리 결과를 선택해 추가하세요.':'완료된 정리 결과가 없습니다. 데이터 플랫폼에서 자료를 먼저 정리해 주세요.';
    const controls=el('div',undefined,'row');controls.append(select,choose,moreButton);host.replaceChildren(status,controls);
   }catch(e){status.textContent=friendlyError(e);}finally{loading=false;button.disabled=false;moreButton.disabled=false;button.textContent='정리 결과 보기';}
  };
  button.onclick=()=>load(false);moreButton.onclick=()=>load(true);
  choose.onclick=()=>{const id=select.value,run=versions.get(id);if(!run||!validUUID(id)){status.textContent='추가할 정리 결과를 선택해 주세요.';return;}if(state.selected.size>=20&&!state.selected.has(id)){status.textContent='한 번에 최대 20개를 선택할 수 있습니다.';return;}state.selected.set(id,{name,sourceID:source.document_id,versionLabel:String(run.created_at||'').slice(0,16).replace('T',' ')});selection();status.textContent='선택한 자료에 추가했습니다.';};
  $('sources').append(row);
 }
 state.sourceCursor=data.next_cursor||'';$('sourcesMore').hidden=!state.sourceCursor;
 if(!more&&!$('sources').childElementCount)$('sources').append(el('p','조회된 자료가 없습니다. 데이터 플랫폼에서 자료를 가져오고 내용을 정리해 주세요.','empty'));
}
function optionDefaults(){const d=state.descriptors.find(x=>x.id===$('chunker').value);if(d){$('chunkerDescription').textContent=d.description;$('maxCharacters').value=d.defaults.max_characters;$('overlap').value=d.defaults.overlap_characters;}}
async function chunkers(){const data=await api('/api/chunkers');state.descriptors=data.items||[];$('chunker').replaceChildren(...state.descriptors.map(d=>new Option(d.name,d.id)));$('chunker').value='structure_v1';optionDefaults();}
function key(){if(globalThis.crypto?.randomUUID)return crypto.randomUUID();const b=new Uint8Array(16);crypto.getRandomValues(b);b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128;const hex=Array.from(b,x=>x.toString(16).padStart(2,'0')).join('');return [hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join('-');}
async function submitPending(){if(!state.pending)return;let data;try{data=await api('/api/chunking-runs',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':state.pending.key},body:state.pending.body});}catch(e){if([400,409,413].includes(e.httpStatus)){setPreviewPending(null);}throw e;}setPreviewPending(null);state.current={chunking_run_id:data.chunking_run_id,operation_id:data.operation_id};$('refresh').hidden=false;try{await openRun(data.chunking_run_id);await runs();$('runs').value=data.chunking_run_id;statusAt('createStatus','미리보기를 접수했습니다. 이 화면에서 진행 상태를 확인할 수 있습니다.');uiChanged();}catch(e){message('작업은 접수됐습니다: '+data.chunking_run_id+'\n결과 조회를 완료하지 못했습니다. 상태 새로고침 또는 작업 목록 조회를 눌러주세요.',true);}}
async function create(){if(typeof consoleState!=='undefined'&&(consoleState.flow||consoleState.busy))throw new Error('진행 중인 지식 만들기의 접수를 먼저 확인해 주세요.');if(state.pending)throw new Error('이전 미리보기의 접수를 먼저 확인해 주세요.');const name=$('name').value.trim();if(!name||Array.from(name).length>120)throw new Error('미리보기 이름을 1~120자로 입력해 주세요.');setPreviewPending({key:key(),body:JSON.stringify(selectedChunking(name))});try{await submitPending();}catch(e){if(state.pending)e.message+='\n미리보기 이어서 확인 버튼으로 같은 요청을 확인할 수 있습니다.';throw e;}}
async function runs(more=false){const q=new URLSearchParams({limit:'50'});if(more)q.set('cursor',state.runCursor);const data=await api('/api/chunking-runs?'+q);if(!more)$('runs').replaceChildren(new Option('작업 선택',''));for(const run of data.items||[])$('runs').append(new Option(`${run.name} · ${run.result_registered?run.chunk_count+' 청크':'진행 상태 확인'} · ${run.chunking_run_id.slice(0,8)}`,run.chunking_run_id));state.runCursor=data.next_cursor||'';$('runsMore').hidden=!state.runCursor;}
async function openRun(id){requireUUID(id,'청크 작업');const generation=++state.runGeneration;state.current=null;const run=await api('/api/chunking-runs/'+id);if(generation!==state.runGeneration)return;if(run.chunking_run_id!==id)throw new Error('선택한 청크 작업과 응답이 다릅니다.');state.current=run;state.chunkCursor='';$('chunks').replaceChildren();$('summary').replaceChildren();$('comparison').replaceChildren();$('chunksMore').hidden=true;$('refresh').hidden=false;$('compare').hidden=!run.result_registered;const status=$('runStatus');status.replaceChildren(el('h3',run.name),details('작업 정보',{chunking_run_id:id,created_at:run.created_at}));if(run.result_registered){status.append(el('p',`${run.chunk_count}개 청크 저장 완료`,'pill'));$('summary').append(details('입력·청커·옵션',run.spec),details('원본 품질·미인식/제외 영역',run.summary));if(state.comparison){const same=state.comparison.snapshot===run.spec.snapshot_id;$('comparison').append(el('p',same?`같은 입력 비교: ${state.comparison.chunker} ${state.comparison.count}개 → ${run.spec.chunker.id} ${run.chunk_count}개`:'비교 기준과 입력 snapshot이 다릅니다. 동일 입력 비교로 해석하지 마세요.','hint'));}await chunks(false);uiChanged();}else{status.append(el('p',`전달 상태: ${run.submission_status||'조회 중'} ${run.submission_error_code||''}`));try{const op=await api('/api/operations/'+run.operation_id);if(generation!==state.runGeneration||state.current?.chunking_run_id!==id)return;state.current.operation_status=op.status;status.append(el('p',typeof statusLabel==='function'?statusLabel(op.status):op.status));uiChanged();if(op.error_message)status.append(el('p',op.error_message,'warning'));}catch(e){status.append(el('p','실행 상태를 아직 조회할 수 없습니다. 잠시 후 새로고침하세요.','hint'));}}}
async function chunks(more){if(!state.current)return;const q=new URLSearchParams({limit:'20'});if(more)q.set('cursor',state.chunkCursor);const id=state.current.chunking_run_id,generation=state.runGeneration;const data=await api('/api/chunking-runs/'+id+'/chunks?'+q);if(generation!==state.runGeneration||state.current?.chunking_run_id!==id)return;if(!more)$('chunks').replaceChildren();for(const c of data.items||[]){const card=el('article',undefined,'card');card.append(el('h3',`청크 ${c.ordinal+1} · ${c.kind} · ${Array.from(c.text).length}자`),el('p',c.text,'chunkText'));if(c.structure){const s=c.structure;const unit=s.unit==='rows'?'행':'줄';card.append(el('p',`원본 ${s.start+1}~${s.end}${unit} · 분할 ${s.part}/${s.parts}`,'hint'));if(s.context_rows?.length)card.append(el('p',`문맥으로 반복한 원본 행: ${s.context_rows.map(r=>r+1).join(', ')}`,'hint'));}if(c.quality?.state!=='ready')card.append(el('p','원본 품질: '+c.quality?.state,'warning'));card.append(details('출처·원본 구조',{source_document_id:c.source_document_id,canonical_id:c.canonical_id,source_spans:c.source_spans,structure:c.structure,quality:c.quality}));$('chunks').append(card);}state.chunkCursor=data.next_cursor||'';$('chunksMore').hidden=!state.chunkCursor;}
function compare(){const run=state.current;if(!run?.result_registered)return;state.comparison={snapshot:run.spec.snapshot_id,chunker:run.spec.chunker.id,count:run.chunk_count};state.selected.clear();for(const input of run.spec.documents)state.selected.set(input.canonical_id,{name:sourceName(input.source_document_id),sourceID:input.source_document_id});selection();$('chunker').value=run.spec.chunker.id==='structure_v1'?'recursive_character_v1':'structure_v1';optionDefaults();$('maxCharacters').value=run.spec.chunker.options.max_characters;$('overlap').value=run.spec.chunker.options.overlap_characters;message('같은 정제 버전과 크기 옵션을 선택했습니다. 다른 청커로 생성한 뒤 결과를 비교하세요.');$('chunker').focus();}
action('sourcesLoad',()=>sources());action('sourcesMore',()=>sources(true));action('groupMore',()=>groups(true));action('create',create);action('retry',submitPending);action('runsLoad',()=>runs());action('runsMore',()=>runs(true));action('refresh',()=>openRun(state.current?.chunking_run_id));action('chunksMore',()=>chunks(true));action('compare',compare);$('chunker').onchange=optionDefaults;
Promise.allSettled([chunkers(),groups()]).then(results=>{const errors=results.filter(x=>x.status==='rejected');if(errors.length)message(errors.map(x=>x.reason.message).join('\n'),true);});

if(state.pending){$('retry').hidden=false;message('접수 여부를 확인하지 못한 이전 요청이 있습니다. 동일 요청 다시 전송으로 결과를 확인하세요.');}
