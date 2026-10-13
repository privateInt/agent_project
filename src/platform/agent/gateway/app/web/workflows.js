'use strict';
// WorkflowLink is a reference owned by Agent. n8n owns workflow contents/authentication.
const workflowUI={items:[],current:null,agent:null,version:null,draft:null,saved:null,patch:null,origins:new Map()};
const workflowIDPattern=/^[A-Za-z0-9_-]{1,128}$/;
function resetWorkflows(){
 Object.assign(workflowUI,{items:[],current:null,agent:null,version:null,draft:null,saved:null,patch:null,origins:new Map()});
 for(const id of ['workflow-list','workflow-history','workflow-definition','question-history','workflow-name','workflow-agent-label','workflow-status-help','workflow-address-help','workflow-history-help'])$(id).replaceChildren();
 for(const id of ['workflow-search','workflow-create-name','workflow-create-url','workflow-browser-origin','workflow-rename'])$(id).value='';
 $('workflow-create').hidden=$('workflow-detail').hidden=true;
 clear($('workflow-create-agent'));$('workflow-edit').removeAttribute('href');toolResults();
}
function workflowControls(){
 const ready=state.connected&&!state.busy,locked=!!workflowUI.draft||!!workflowUI.saved||!!workflowUI.patch;
 for(const id of ['workflows-refresh','workflow-search','workflow-history-refresh','workflow-browser-origin'])$(id).disabled=!ready;
 $('workflow-new').disabled=!ready||locked;$('workflows-refresh').disabled=!ready||locked;
 for(const id of ['workflow-create-name','workflow-create-url','workflow-create-agent','workflow-create-cancel'])$(id).disabled=!ready||locked;
 $('workflow-save').disabled=!ready;$('workflow-save').textContent=workflowUI.draft||workflowUI.saved?'이 등록 요청 다시 확인':'연결 등록';
 for(const id of ['workflow-rename','workflow-rename-save','workflow-toggle'])$(id).disabled=!ready||!workflowUI.current||locked;
 $('workflow-rename-save').disabled=!ready||!workflowUI.current;
 $('workflow-rename-save').textContent=workflowUI.patch?'이 변경 요청 확인':'이름 저장';
 $('workflow-ask').disabled=!ready||!workflowUI.current?.enabled||!workflowUI.agent?.enabled||workflowUI.version?.state!=='published'||!!agentUI.pending||!!agentUI.runRequest||state.keys.size>0;
 const editDisabled=!ready||!$('workflow-edit').getAttribute('href');$('workflow-edit').setAttribute('aria-disabled',String(editDisabled));$('workflow-edit').tabIndex=editDisabled?-1:0;
}
function workflowOrigin(raw){
 if(typeof raw!=='string'||raw.length>512||/[\\\s\x00-\x20\x7f]/.test(raw))throw Error('n8n 주소는 http(s) 서버 주소만 입력하세요.');
 const u=new URL(raw);
 if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash||!['','/'].includes(u.pathname))throw Error('n8n 주소에서 인증정보·경로·검색 조건을 빼주세요.');
 return u.origin;
}
function parseWorkflowURL(raw){
 if(typeof raw!=='string'||raw.length>660||/[\\\s%\x00-\x20\x7f]/.test(raw))throw Error('n8n 편집 주소를 그대로 입력하세요.');
 const u=new URL(raw),m=/^\/workflow\/([A-Za-z0-9_-]{1,128})$/.exec(u.pathname);
 if(!m||u.username||u.password||u.search||u.hash)throw Error('n8n 편집 주소는 http(s)://서버/workflow/업무흐름ID 형식입니다.');
 const origin=workflowOrigin(u.origin);
 // Reject normalization of traversal and ambiguous URLs before saving a reference.
 if(raw!==origin+u.pathname)throw Error('정규 n8n 편집 주소를 입력하세요.');
 return {instance_url:origin,workflow_id:m[1]};
}
function loopback(host){return host==='localhost'||host==='[::1]'||host==='::1'||/^127\./.test(host);}
function workflowEditAddress(){
 const link=$('workflow-edit');link.removeAttribute('href');
 if(!workflowUI.current)return;
 try{
  const origin=workflowOrigin($('workflow-browser-origin').value.trim());
  if(!workflowIDPattern.test(workflowUI.current.workflow_id))throw Error('업무흐름 ID가 올바르지 않습니다.');
  if(loopback(new URL(origin).hostname)&&!loopback(location.hostname))throw Error('등록된 주소는 서버 내부 주소입니다. 현재 PC에서 접속할 수 있는 n8n 서버 주소를 입력하세요.');
  workflowUI.origins.set(workflowUI.current.workflow_link_id,origin);
  link.href=origin+'/workflow/'+workflowUI.current.workflow_id;
  $('workflow-address-help').textContent=origin+' · 새 탭에서 n8n에 별도로 로그인합니다.';
 }catch(e){$('workflow-address-help').textContent=e instanceof TypeError?'올바른 http(s) 주소를 입력하세요.':e.message;}
 workflowControls();
}
function renderWorkflows(){
 const box=$('workflow-list');box.replaceChildren();const q=$('workflow-search').value.trim().toLowerCase();
 const items=workflowUI.items.filter(w=>(w.display_name+' '+(w.agent_display_name||'')).toLowerCase().includes(q));
 if(!items.length){const empty=el('div',undefined,'empty');empty.append(el('strong',q?'일치하는 업무흐름이 없습니다.':'아직 연결된 업무흐름이 없습니다.'),el('p','이미 준비한 n8n 업무흐름의 편집 주소를 연결하면 이곳에서 찾을 수 있습니다.'));box.append(empty);}
 for(const w of items){const row=el('div',undefined,'tool-row'),info=el('div');
  info.append(el('strong',w.display_name),el('p',(w.agent_display_name||'Agent')+(w.agent_version?' · 버전 '+w.agent_version:''),'hint'));
  const tag=el('span',w.enabled?'연결 등록됨':'연결 숨김','badge'+(w.enabled?'':' warning'));
  const b=el('button','열기','secondary');b.onclick=()=>action(()=>openWorkflow(w.workflow_link_id));row.append(info,tag,b);box.append(row);
 }
}
async function refreshWorkflows(){workflowUI.items=await all('/api/workflow-links');renderWorkflows();}
function workflowDraftGuard(){if(workflowUI.draft||workflowUI.saved||workflowUI.patch)throw Error('확인 중인 연결 요청을 먼저 같은 버튼으로 확인하세요.');}
async function openWorkflow(id,recover=false){
 if(!recover)workflowDraftGuard();
 const w=await api('/api/workflow-links/'+id);let version=null,agent=null;
 try{version=await api('/api/agent-versions/'+w.agent_version_id);agent=await api('/api/agents/'+version.agent_id);}
 catch(e){if(e.status!==404)throw e;}
 Object.assign(workflowUI,{current:w,version,agent});
 $('workflow-create').hidden=true;$('workflow-detail').hidden=false;$('workflow-name').textContent=w.display_name;
 $('workflow-agent-label').textContent=agent?agent.display_name+' · 버전 '+version.version+' · '+(labels[version.state]||version.state):'연결된 Agent를 찾을 수 없습니다.';
 $('workflow-badge').textContent=w.enabled?'연결 등록됨':'연결 숨김';$('workflow-badge').className='badge'+(w.enabled?'':' warning');
 $('workflow-status-help').textContent=!w.enabled?'이 연결은 숨김 상태입니다. n8n 워크플로가 중지됐다는 뜻은 아닙니다.':!agent||!agent.enabled||version.state!=='published'?'연결된 Agent 버전을 새 질문에 사용할 수 없습니다. 기존 실행 기록은 확인할 수 있습니다.':'Agent 연결을 확인했습니다. n8n의 게시·실행 상태는 편집 화면에서 확인하세요.';
 $('workflow-rename').value=w.display_name;$('workflow-toggle').textContent=w.enabled?'연결 숨기기':'연결 다시 표시';
 $('workflow-definition').textContent=show(w);$('workflow-history').replaceChildren();$('workflow-history-help').textContent='기록 조회를 누르면 저장된 Agent 실행을 확인합니다. n8n 전체 실행 상태와는 다릅니다.';
 $('workflow-browser-origin').value=workflowUI.origins.get(w.workflow_link_id)||w.instance_url;workflowEditAddress();
 $('workflow-address-panel').open=!$('workflow-edit').getAttribute('href');navigate('workflows');
}
async function newWorkflow(){
 workflowDraftGuard();if(state.keys.size)throw Error('확인 중인 저장·실행 요청을 먼저 확인하세요.');
 const list=await all('/api/agents');clear($('workflow-create-agent'),'Agent 버전 선택');
 // Definition selection only; no model inspection or inference.
 for(const a of list){if(!a.enabled)continue;const detail=await api('/api/agents/'+a.agent_id);for(const v of detail.versions||[])if(v.state==='published')option($('workflow-create-agent'),v.agent_version_id,`${a.display_name} · 버전 ${v.version}`);}
 $('workflow-create-name').value='';$('workflow-create-url').value='';$('workflow-detail').hidden=true;$('workflow-create').hidden=false;
 if($('workflow-create-agent').options.length===1)notice('사용 중인 Agent가 없습니다. Agent를 시험하고 사용 시작한 뒤 연결하세요.');
}
async function saveWorkflow(){
 if(!workflowUI.draft&&!workflowUI.saved){if(!['workflow-create-name','workflow-create-url','workflow-create-agent'].every(id=>validField($(id))))return;
  workflowUI.draft={display_name:$('workflow-create-name').value.trim(),...parseWorkflowURL($('workflow-create-url').value.trim()),agent_version_id:$('workflow-create-agent').value};}
 if(!workflowUI.saved){try{workflowUI.saved=await api('/api/workflow-links',{method:'POST',body:workflowUI.draft});}
  catch(e){if(!state.keys.has('/api/workflow-links'))workflowUI.draft=null;throw e;}}
 const id=workflowUI.saved.workflow_link_id;await refreshWorkflows();await openWorkflow(id,true);workflowUI.saved=null;workflowUI.draft=null;
 notice('업무흐름 연결을 등록했습니다. n8n의 실행 설정과 게시 상태는 편집 화면에서 확인하세요.');
}
async function changeWorkflow(change){
 const w=workflowUI.current;if(!w)return;
 if(!workflowUI.patch)workflowUI.patch={id:w.workflow_link_id,revision:w.revision,change};
 const pending=workflowUI.patch,path='/api/workflow-links/'+pending.id;
 // A PATCH response may be lost. Re-read revision and requested fields before retrying.
 const latest=await api(path);
 const matches=Object.entries(pending.change).every(([k,v])=>latest[k]===v);
 if(latest.revision===pending.revision+1&&matches){workflowUI.patch=null;await refreshWorkflows();await openWorkflow(pending.id,true);notice('저장된 변경을 확인했습니다.');return;}
 if(latest.revision!==pending.revision){workflowUI.patch=null;await refreshWorkflows();await openWorkflow(pending.id,true);throw Error('다른 작업에서 연결을 변경했습니다. 최신 내용을 확인하고 다시 변경하세요.');}
 try{await api(path,{method:'PATCH',body:{expected_revision:pending.revision,...pending.change}});}
 catch(e){if(e.status>=400&&e.status<500)workflowUI.patch=null;throw e;}
 // Keep the original intent until a read confirms the changed resource.
 await refreshWorkflows();await openWorkflow(pending.id,true);workflowUI.patch=null;notice('연결 표시를 저장했습니다. n8n 워크플로와 Agent의 실행 설정은 유지됩니다.');
}
async function workflowHistory(){
 const w=workflowUI.current;if(!w)return;await agentHistory();
 if(agentUI.history.some(r=>!Object.hasOwn(r,'caller_context'))){throw Error('실행 목록의 호출 정보가 없습니다. A08 Service 업데이트를 확인하세요.');}
 const rows=agentUI.history.filter(r=>r.agent_version_id===w.agent_version_id&&r.caller_context?.system==='n8n'&&r.caller_context?.workflow_id===w.workflow_id);
 renderAgentHistory($('workflow-history'),rows);
 const ambiguous=workflowUI.items.filter(x=>x.workflow_id===w.workflow_id&&x.agent_version_id===w.agent_version_id).length>1;
 $('workflow-history-help').textContent=`같은 업무흐름 ID·Agent 버전으로 기록된 ${rows.length}개 중 최근 ${Math.min(50,rows.length)}개입니다. n8n 전체 실행 상태는 별도로 확인하세요.`+(ambiguous?' 여러 n8n 서버에 같은 ID가 등록돼 있어 이 목록만으로 서버를 구분할 수 없습니다.':'');
}
$('nav-workflows').onclick=()=>action(async()=>{navigate('workflows');await refreshWorkflows();});
$('workflows-refresh').onclick=()=>action(async()=>{await refreshWorkflows();if(workflowUI.current&&!workflowUI.patch)await openWorkflow(workflowUI.current.workflow_link_id,true);});
$('workflow-search').oninput=renderWorkflows;
$('workflow-new').onclick=()=>action(newWorkflow);
$('workflow-create-cancel').onclick=()=>{$('workflow-create').hidden=true;};
$('workflow-create-form').onsubmit=e=>{e.preventDefault();action(saveWorkflow);};
$('workflow-browser-origin').oninput=workflowEditAddress;
$('workflow-edit').onclick=e=>{if(state.busy||!state.connected||!$('workflow-edit').getAttribute('href'))e.preventDefault();};
$('workflow-ask').onclick=()=>action(async()=>{const id=workflowUI.current.workflow_link_id;await openWorkflow(id,true);if(!workflowUI.current.enabled||!workflowUI.agent?.enabled||workflowUI.version?.state!=='published')throw Error('사용 가능한 Agent 연결이 아닙니다.');await openAgent(workflowUI.agent.agent_id,workflowUI.version.agent_version_id);notice('연결된 버전의 Agent에 직접 질문합니다. n8n 전체 업무흐름 실행은 n8n에서 시작하세요.');});
$('workflow-history-refresh').onclick=()=>action(workflowHistory);
$('workflow-rename-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(workflowUI.patch){await changeWorkflow({});return;}if(validField($('workflow-rename')))await changeWorkflow({display_name:$('workflow-rename').value.trim()});});};
$('workflow-toggle').onclick=()=>action(()=>changeWorkflow({enabled:!workflowUI.current.enabled}));
workflowControls();
