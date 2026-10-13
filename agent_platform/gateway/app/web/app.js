'use strict';
const $ = id => document.getElementById(id);
const state = {registration:'knowledge',direct:null,directImport:null,token:'', connected:false, tools:[], connections:[], imported:null, operation:null, tool:null, version:null,
  connectionVersions:[], view:'tools', step:1, busy:false, keys:new Map(), calls:new Map(), activeCall:null, pendingCall:null,
  tested:new Set(), jsonMode:false, inputTree:null, inputVersion:null, draftRequest:null, savedVersion:null, savedConnection:null};
const show = value => JSON.stringify(value,null,2);
const labels = {draft:'초안',published:'사용 중',retired:'폐기됨',accepted:'접수됨',running:'실행 중',succeeded:'성공',failed:'실패',unknown:'결과 확인 필요',external_pending:'외부 처리 중',interrupted:'중단됨'};
const callMessages = {succeeded:'실행을 마쳤습니다. 아래에서 응답 내용을 확인하세요.',failed:'실행하지 못했습니다. 아래 오류 내용을 확인하세요.',external_pending:'외부 서비스가 접수했지만 완료 여부는 아직 알 수 없습니다. 대상 서비스에서 확인하세요.',unknown:'외부 데이터가 변경되었는지 확인할 수 없습니다. 다시 실행하기 전에 대상 서비스에서 확인하세요.',interrupted:'실행이 중단됐습니다. 자동으로 다시 실행하지 않습니다.',accepted:'실행 요청을 접수했습니다.',running:'서비스의 응답을 기다리고 있습니다.'};
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function uuid(){const b=crypto.getRandomValues(new Uint8Array(16));b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128;return [...b].map((x,i)=>([4,6,8,10].includes(i)?'-':'')+x.toString(16).padStart(2,'0')).join('');}
function notice(text,error=false){$('notice').textContent=text;$('notice').className=error?'error':'';}
function option(select,value,label){const n=el('option',label);n.value=value;select.append(n);}
function clear(select,label){select.replaceChildren();if(label)option(select,'',label);}
function badge(node,status,text){node.textContent=text||labels[status]||status;node.className='badge'+(['published','succeeded'].includes(status)?' success':['failed','retired'].includes(status)?' error':['unknown','external_pending','interrupted'].includes(status)?' warning':'');}
function validField(n){const disabled=n.disabled;n.disabled=false;const ok=n.reportValidity();n.disabled=disabled;return ok;}
function controls(){
 const v=state.version,t=state.tool,ready=state.connected&&!state.busy;
 document.querySelectorAll('button,input,select,textarea').forEach(n=>{n.disabled=state.busy;});
 for(const id of ['nav-agents','nav-workflows','direct-next','knowledge-example','refresh','new-tool','nav-add','nav-results','import-file','save-connection','lookup','refresh-call'])$(id).disabled=!ready;
 const permissions={'next-connection':!!state.operation,'create-draft':!!state.operation&&!!$('connection-versions').value,
  'test-call':v?.state==='draft'&&t?.enabled&&!state.pendingCall,'run-call':v?.state==='published'&&t?.enabled&&!state.pendingCall,
  'publish':v?.state==='draft'&&t?.enabled&&!state.pendingCall&&!state.keys.has('/api/tool-calls'),'retire':v?.state==='published'&&!state.pendingCall&&!state.keys.has('/api/tool-calls'),'disable-tool':!!t,
  'load-arguments':!!v,'new-version':['openapi_http_v1','knowledge_search_v1'].includes(v?.kind)&&!state.keys.size&&!state.pendingCall};
 for(const [id,allowed] of Object.entries(permissions))$(id).disabled=!ready||!allowed;
 $('test-call').hidden=v?.state!=='draft';$('publish').hidden=v?.state!=='draft';$('run-call').hidden=v?.state!=='published';
 $('disable-tool').textContent=t?.enabled===false?'사용 재개':'사용 중지';
 $('tool-name').readOnly=!!state.tool;$('tool-description').readOnly=!!state.tool;
 document.querySelectorAll('[data-fixed]').forEach(n=>n.disabled=true);
 updateInputDisabled();knowledgeControls();if(typeof agentControls==='function')agentControls();if(typeof workflowControls==='function')workflowControls();
}
async function action(fn){if(state.busy)return;state.busy=true;controls();try{await fn();}catch(e){notice(e.message||'요청을 완료하지 못했습니다.',true);}finally{state.busy=false;controls();}}
async function api(path,{method='GET',body,raw=false,type='application/json'}={}){
 if(!state.token)throw Error('먼저 접속 토큰으로 연결하세요.');
 const payload=body===undefined?undefined:(raw?body:JSON.stringify(body));const post=method==='POST'&&!['/api/knowledge/inspect','/api/agent-models/inspect'].includes(path);let pending=state.keys.get(path);
 if(post){if(pending&&pending.payload!==payload)throw Error('이전 요청의 응답을 아직 확인하지 못했습니다. 입력을 원래대로 유지하고 같은 버튼으로 다시 확인하세요.');if(!pending){pending={payload,key:uuid()};state.keys.set(path,pending);}}
 const headers={'Authorization':'Bearer '+state.token,'Content-Type':type};if(method==='POST')headers['Idempotency-Key']=post?pending.key:uuid();
 const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),20000);
 try{const res=await fetch(path,{method,headers,body:payload,signal:abort.signal,credentials:'omit',redirect:'error'});let data;
 try{data=await res.json();}catch{throw Error('응답을 확인하지 못했습니다. 같은 입력으로 다시 누르면 기존 요청을 확인합니다.');}
 if(!res.ok){if(post&&res.status>=400&&res.status<500)state.keys.delete(path);const code=data.error?.code||String(res.status);const messages={WORKFLOW_LINK_CONFLICT:'이미 등록한 업무흐름입니다. 목록을 새로고침해 확인하세요.',WORKFLOW_LINK_INVALID:'n8n 편집 주소와 Agent 버전을 확인하세요.',REVISION_CONFLICT:'다른 작업에서 변경했습니다. 최신 상태를 다시 불러오세요.',AGENT_TEST_REQUIRED:'이 Agent 버전으로 시험 질문에 성공한 뒤 사용을 시작하세요.',AGENT_VERSION_UNAVAILABLE:'사용할 수 없는 Agent 버전입니다. 최신 상태를 확인하세요.',AGENT_INPUT_INVALID:'질문이나 Agent 설정의 형식과 길이를 확인하세요.',MODEL_NOT_READY:'답변 모델이 준비되지 않았습니다. Model Platform에서 배포 상태를 확인하세요.',MODEL_IDENTITY_MISMATCH:'저장한 답변 모델 버전과 현재 배포가 다릅니다.',MODEL_UNAVAILABLE:'답변 모델 서비스에 연결하지 못했습니다.',MODEL_NOT_FOUND:'답변 모델 배포를 찾을 수 없습니다.',KNOWLEDGE_NOT_READY:'선택한 지식이나 모델이 아직 준비되지 않았습니다.',KNOWLEDGE_PROFILE_MISMATCH:'지식의 임베딩 설정과 모델이 다릅니다. 기존 배포와 지식 버전을 확인하세요.',KNOWLEDGE_RESOURCE_NOT_FOUND:'지식 또는 모델 배포를 찾을 수 없습니다.',TOOL_UPSTREAM_UNAVAILABLE:'서비스에 연결하지 못했습니다. 서비스 주소와 실행 상태를 확인하세요.',TOOL_TARGET_BLOCKED:'사용할 수 없는 서비스 주소입니다. 컨테이너에서 접근 가능한 주소를 사용하세요.',TOOL_TIMEOUT:'서비스 응답 대기 시간이 초과되었습니다.',UNAUTHENTICATED:'접속 토큰을 확인하세요.',PERMISSION_DENIED:'이 작업을 수행할 권한이 없습니다.',TOOL_TEST_REQUIRED:'이 초안의 시험 실행에 성공한 뒤 사용을 시작하세요.',CREDENTIAL_NOT_CONFIGURED:'서비스 인증 정보가 서버에 등록되지 않았습니다. 관리자에게 확인하세요.',CONFLICT:'저장된 상태가 변경되었거나 작업 조건이 맞지 않습니다. 최신 버전을 확인하세요.'};const error=Error((messages[code]||data.error?.message||'요청을 완료하지 못했습니다.')+' ('+code+')');error.code=code;error.status=res.status;throw error;}
 if(post)state.keys.delete(path);return data;
 }catch(e){if(e.name==='AbortError'||e instanceof TypeError)throw Error('응답을 받지 못했습니다. 입력을 유지하고 같은 버튼으로 다시 확인하세요.');throw e;}finally{clearTimeout(timer);}
}
async function all(path){let items=[],cursor='';for(let n=0;n<50;n++){const p=await api(path+'?limit=100'+(cursor?'&cursor='+encodeURIComponent(cursor):''));items.push(...p.items);if(!p.next_cursor)return items;cursor=p.next_cursor;}throw Error('목록 한도를 초과했습니다.');}
function navigate(view){state.view=view;for(const name of ['agents','workflows','tools','add','detail','results'])$('view-'+name).hidden=!(view===name||(view==='add'&&name==='detail'&&state.step===3));
 for(const name of ['agents','workflows','tools','add','results']){const n=$('nav-'+name);if(view===name||(view==='detail'&&name==='tools'))n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');}
 for(let i=1;i<=3;i++){if(i<3)$('step-'+i).hidden=state.step!==i;const n=$('step-label-'+i);if(state.step===i)n.setAttribute('aria-current','step');else n.removeAttribute('aria-current');}
 if(view==='results'){$('history-result').append($('result-panel'));renderCalls();}else if(view==='detail'||(view==='add'&&state.step===3)){$('inline-result').append($('result-panel'));$('result-panel').hidden=!state.activeCall||state.activeCall.tool_version_id!==state.version?.tool_version_id;}
 if(view==='results')$('result-panel').hidden=!state.activeCall;
 if(typeof placeAgentResult==='function')placeAgentResult(view);
 controls();
}
async function refresh(){const [tools,connections,imports]=await Promise.all([all('/api/tools'),all('/api/connections'),all('/api/openapi-imports')]);state.tools=tools;state.connections=connections;renderTools();
 const selected=$('connections').value;clear($('connections'),'서비스 선택');for(const c of connections)option($('connections'),c.connection_id,c.display_name);$('connections').value=selected;
 const imp=state.imported?.import_id;clear($('imports'),'파일 선택');for(const i of imports)option($('imports'),i.import_id,`${i.title} · ${i.spec_version}`);if(imp)$('imports').value=imp;knowledgeConnections();
}
function renderTools(){const box=$('tools');box.replaceChildren();$('tool-count').textContent=state.tools.length;const q=$('tool-search').value.trim().toLowerCase();const rows=state.tools.filter(t=>(t.display_name+' '+t.description).toLowerCase().includes(q));
 if(!rows.length){const empty=el('div',undefined,'empty');empty.append(el('strong',!state.connected?'연결하면 내 도구를 볼 수 있어요':q?'일치하는 도구가 없습니다':'아직 등록한 도구가 없습니다'),el('p',state.connected?'도구 추가에서 지식 검색을 만들거나 원하는 API를 연결하세요.':'위에서 접속 토큰을 입력해 시작하세요.'));box.append(empty);}
 for(const t of rows){const row=el('div',undefined,'tool-row'),name=el('div');name.append(el('div',t.display_name,'tool-name'),el('p',t.description||'설명 없음','row-description'));const status=el('div');status.append(el('span',t.enabled?'등록됨':'사용 중지','badge'));const actions=el('div');const b=el('button','열기','secondary');b.onclick=()=>action(async()=>{if(state.keys.size||state.draftRequest||state.savedVersion||state.savedConnection)throw Error('진행 중인 저장 요청을 먼저 확인하세요.');if(await selectTool(t.tool_id))navigate('detail');notice('');});actions.append(b);row.append(name,status,actions);box.append(row);}
}
function resetDraft(){resetKnowledge();state.registration='knowledge';state.direct=null;state.directImport=null;$('base-url').readOnly=false;modeView();$('target-check').textContent='';for(const id of ['direct-endpoint','direct-input-schema','direct-response-schema','direct-path-schema','direct-query-schema','direct-header-schema'])$(id).value='';$('direct-method').value='POST';$('direct-input-location').value='body';$('direct-status').value='200';$('arguments-file').value='';directFields();state.imported=null;state.operation=null;state.tool=null;state.version=null;state.connectionVersions=[];state.inputVersion=null;state.draftRequest=null;state.savedVersion=null;state.step=1;$('wizard-title').textContent='새 도구 추가';
 for(const id of ['spec-file','tool-name','tool-description','base-url','connection-name','credential-ref','credential-revision','api-key-name','operation-search','connections','imports'])$(id).value='';
 $('effect').value='write';$('timeout').value='30';$('forward-key').checked=false;$('auth-type').value='none';$('api-key-in').value='header';authFields();
 clear($('connection-versions'),'버전 선택');clear($('auth-scheme'));$('operations').replaceChildren();$('unsupported-operations').replaceChildren();$('spec-summary').textContent='';$('selected-connection').textContent='';$('auth-match').textContent='';$('operation-selection').textContent='파일을 분석하고 기능을 선택하세요.';$('operation-search-label').hidden=true;$('unsupported-panel').hidden=true;$('create-connection').open=false;$('advanced-settings').open=false;notice('');
}
function startNew(){if(!state.connected){$('login-panel').hidden=false;notice('먼저 접속 토큰으로 연결하세요.');return;}if(state.keys.size||state.draftRequest||state.savedVersion||state.savedConnection){notice('진행 중인 저장 요청을 먼저 같은 버튼으로 확인하세요.',true);navigate('add');return;}resetDraft();navigate('add');}
function renderImport(imp){state.registration='openapi';state.direct=null;state.directImport=null;$('base-url').readOnly=false;$('target-check').textContent='';modeView();state.imported=imp;state.operation=null;const supported=imp.analysis.operations.filter(o=>o.supported);$('spec-summary').textContent=`${imp.analysis.title} · 선택 가능한 기능 ${supported.length}개`;$('operation-search-label').hidden=false;$('operation-search').value='';$('operation-selection').textContent='사용할 기능을 하나 선택하세요.';renderOperations();}
function renderOperations(){const q=$('operation-search').value.trim().toLowerCase();$('operations').replaceChildren();$('unsupported-operations').replaceChildren();let unsupported=0;
 for(const o of state.imported?.analysis.operations||[]){if(!o.supported){unsupported++;const n=el('div',undefined,'unsupported');n.append(el('strong',o.summary||o.operation_id||o.key));for(const p of o.problems||[])n.append(el('p',`${p.pointer}: ${p.reason}`));$('unsupported-operations').append(n);continue;}
 if(!(o.summary+' '+o.operation_id+' '+o.key).toLowerCase().includes(q))continue;
 const b=el('button',undefined,'operation'+(state.operation?.key===o.key?' selected':''));b.setAttribute('aria-pressed',String(state.operation?.key===o.key));b.append(el('span',o.summary||o.operation_id||o.key),el('small',`${o.method} ${o.path}`));b.onclick=()=>{if(state.busy)return;chooseOperation(o);renderOperations();};$('operations').append(b);}
 if(!$('operations').children.length)$('operations').append(el('p',q?'일치하는 기능이 없습니다.':'선택 가능한 기능이 없습니다. 아래의 지원하지 않는 이유를 확인하세요.','hint'));
 for(const p of state.imported?.analysis.problems||[])$('unsupported-operations').append(el('p',`${p.pointer}: ${p.reason}`));
 $('unsupported-panel').hidden=!unsupported&&!state.imported?.analysis.problems?.length;$('unsupported-title').textContent=`사용할 수 없는 기능 ${unsupported}개 · 이유 보기`;controls();
}
function chooseOperation(o){state.operation=o;state.version=null;$('selected-operation').textContent=(o.summary||o.operation_id||'선택한 기능')+' · '+o.method+' '+o.path;$('operation-selection').textContent='선택: '+(o.summary||o.operation_id||o.key);
 if(!state.tool){$('tool-name').value=(o.summary||o.operation_id||o.key).slice(0,100);$('tool-description').value=o.summary||'';}
 const server=o.servers?.find(x=>/^https?:\/\//.test(x));if(server)$('base-url').value=server;
 clear($('auth-scheme'));for(const a of o.auth_options)option($('auth-scheme'),a.name,a.type==='none'?'인증 없이 연결':a.name+' · '+a.type);$('auth-choice').hidden=o.auth_options.length<=1;
 $('mapping').textContent=show({parameters:o.parameters,input_schema:o.input_schema,responses:o.responses,auth_options:o.auth_options});matchAuth();controls();}
async function selectConnection(id,versionID){state.connectionVersions=[];clear($('connection-versions'),'버전 선택');$('selected-connection').textContent='';if(!id){matchAuth();return;}const c=await api('/api/connections/'+id);state.connectionVersions=c.versions;for(const v of [...c.versions].reverse())option($('connection-versions'),v.connection_version_id,`버전 ${v.version} · ${v.base_url}`);$('connection-versions').value=versionID||c.current_version_id;connectionDetail();}
function connectionDetail(){const v=state.connectionVersions.find(v=>v.connection_version_id===$('connection-versions').value);$('selected-connection').textContent=v?`${v.base_url} · 연결 버전 ${v.version}`:'';matchAuth();controls();}
function matchesAuth(a,v){return a.type===v.auth_type&&(a.type!=='api_key'||(a.key===v.api_key_name&&a.in===v.api_key_in));}
function matchAuth(){const v=state.connectionVersions.find(v=>v.connection_version_id===$('connection-versions').value),options=state.operation?.auth_options||[];if(state.direct){$('auth-choice').hidden=true;$('auth-match').textContent=v?'접속 방식: '+({none:'인증 없음',bearer:'Bearer',api_key:'API Key'})[v.auth_type]:'';$('target-check').textContent=v?(directTargetMatches(v)?'실제 호출 주소: '+state.direct.endpoint:'요청 주소가 다릅니다. '+state.direct.origin+'의 연결을 선택하세요.'):'요청 주소의 서버: '+state.direct.origin;return;}if(!v||!options.length){$('auth-match').textContent='';return;}const matches=options.filter(a=>matchesAuth(a,v));if(matches.length===1)$('auth-scheme').value=matches[0].name;const selected=options.find(a=>a.name===$('auth-scheme').value);$('auth-match').textContent=selected&&matchesAuth(selected,v)?'선택한 서비스의 접속 방식이 이 기능과 일치합니다.':'접속 방식이 맞지 않습니다. 다른 서비스 연결 또는 인증 방식을 선택하세요.';}
function authFields(){const auth=$('auth-type').value;$('credential-fields').hidden=auth==='none';$('api-key-fields').hidden=auth!=='api_key';for(const id of ['credential-ref','credential-revision'])$(id).required=auth!=='none';$('api-key-name').required=auth==='api_key';}
async function selectTool(id,versionID){const t=await api('/api/tools/'+id);state.tool=t;clear($('versions'));for(const v of [...t.versions].reverse())option($('versions'),v.tool_version_id,`버전 ${v.version} · ${labels[v.state]||v.state}`);const v=t.versions.find(x=>x.tool_version_id===versionID)||t.versions.filter(x=>x.state==='draft').at(-1)||t.versions.filter(x=>x.state==='published').at(-1)||t.versions.at(-1);
 if(v){$('versions').value=v.tool_version_id;setVersion(v);return true;}else{resetDraft();state.tool=t;state.version=null;state.step=1;state.imported=null;state.operation=null;$('tool-name').value=t.display_name;$('tool-description').value=t.description;renderImportEmpty();navigate('add');notice('아직 저장된 버전이 없습니다. 기능을 선택해 이어서 저장하세요.');return false;}
}
function renderImportEmpty(){$('operations').replaceChildren();$('spec-summary').textContent='';$('operation-selection').textContent='파일을 분석하고 기능을 선택하세요.';$('unsupported-panel').hidden=true;}
function setVersion(v){state.version=v;$('detail-name').textContent=state.tool.display_name;$('detail-description').textContent=state.tool.description||'';badge($('draft-status'),v.state,`버전 ${v.version} · ${labels[v.state]||v.state}`);
 $('version-note').textContent=!state.tool.enabled?'사용이 중지된 도구입니다. 버전 선택·관리에서 사용을 재개할 수 있습니다.':v.state==='draft'?'① 값을 입력하고 시험 실행 → ② 성공 결과 확인 → ③ 사용 시작':v.state==='published'?'사용 중인 도구입니다. 값을 입력하고 실행하세요.':'폐기된 버전입니다. 다른 버전을 선택하거나 새 버전을 만들어 주세요.';
 $('call-warning').textContent=v.kind==='openapi_http_v1'?(v.binding.effect==='read'?'시험 실행도 실제 서비스에 조회 요청을 보냅니다.':'시험 실행도 실제 서비스의 데이터를 변경할 수 있습니다. 시험용 연결과 값을 사용하세요.'):'등록된 진단 도구를 실행합니다.';
 $('mcp-connection').hidden=v.state!=='published'||!state.tool.enabled;$('mcp-url').value=location.origin+'/mcp';$('mcp-name').textContent='MCP 도구 이름: t_'+v.tool_id.replaceAll('-','')+'_v'+v.version;$('rest-example').textContent='POST /api/tool-calls\nAuthorization: Bearer <접속 토큰>\nIdempotency-Key: <새 UUID>\n'+show({tool_version_id:v.tool_version_id,mode:'run',arguments:{}});
 $('version-picker').open=false;if(state.inputVersion!==v.tool_version_id){renderInputs(v.kind==='knowledge_search_v1'?knowledgeFormSchema(v):v.input_schema);state.inputVersion=v.tool_version_id;}$('result-panel').hidden=!state.activeCall||state.activeCall.tool_version_id!==v.tool_version_id;knowledgeDetail(v);controls();}
// Explicit form values are serialized without converting numeric text to float64.
// Arrays, nullable values, free-form objects and complex bodies remain lossless JSON text.
function simpleSchema(s,depth=0){if(!s||typeof s!=='object'||depth>6||Array.isArray(s.type))return false;if(s.enum&&s.enum.some(x=>typeof x==='object'))return false;
 if(s.type==='object')return s.additionalProperties===false&&Object.entries(s.properties||{}).length<=40&&Object.values(s.properties||{}).every(x=>simpleSchema(x,depth+1));return ['string','number','integer','boolean'].includes(s.type);}
function renderInputs(schema){state.jsonMode=!simpleSchema(schema);state.inputTree=null;$('input-fields').replaceChildren();$('arguments').value='{}';
 if(!state.jsonMode){let i=0;const build=(s,name,required,parent,path=[])=>{const object=s.type==='object',container=el(object?'fieldset':'div',undefined,object?'input-group':'input-field');const title=s.title||({path:'주소에 넣을 값',query:'검색 조건',header:'요청 헤더',body:'요청 내용'})[name]||name;let included=null;
 if(name){if(object)container.append(el('legend',title));if(!required){const l=el('label',undefined,'check');included=el('input');included.type='checkbox';included.setAttribute('aria-label',title+' 포함');l.append(included,document.createTextNode((object?title:'선택 항목: '+title)+' 포함'));included.onchange=controls;container.append(l);}}
 const node={schema:s,path,required,included,children:[],input:null};parent.append(container);
 if(object){for(const [key,child] of Object.entries(s.properties||{}))node.children.push([key,build(child,key,s.required?.includes(key),container,[...path,key])]);}
 else{const id='arg-'+i++;const label=el('label',title+(required?' *':''));label.htmlFor=id;const input=el(s.enum||s.type==='boolean'?'select':s.title==='질문'?'textarea':'input');input.id=id;input.dataset.path=path.join('.');input.required=!!required;if(input.tagName==='SELECT'){option(input,'','값 선택');const values=s.enum||[true,false];values.forEach((x,idx)=>option(input,String(idx),typeof x==='boolean'?(x?'예 (true)':'아니요 (false)'):String(x)));node.values=values;}else{if(input.tagName!=='TEXTAREA')input.type='text';else input.rows=3;if(s.type==='integer'||s.type==='number'){input.inputMode='decimal';input.placeholder=s.type==='integer'?'정수 입력':'숫자 입력';}else{input.placeholder=s.description||'값 입력';if(s.minLength)input.minLength=s.minLength;if(s.maxLength)input.maxLength=s.maxLength;}}
 if('const'in s){input.value=input.tagName==='SELECT'?String(node.values.indexOf(s.const)):String(s.const);input.dataset.fixed='true';}
 node.input=input;container.append(label,input);if(s.description)container.append(el('p',s.description,'hint'));}
 return node;};state.inputTree=build(schema,'',true,$('input-fields'));}
 $('input-help').textContent=state.jsonMode?'이 기능은 복합 입력을 사용합니다. API에 맞는 JSON을 직접 입력하세요.':Object.keys(schema.properties||{}).length?'필수 항목(*)을 입력하세요. 선택 항목은 포함을 켠 경우에만 보냅니다.':'입력할 값이 없는 기능입니다. 바로 시험하거나 실행할 수 있습니다.';
 if(state.jsonMode)$('arguments').value=show(example(schema));inputMode();}
function example(s){if(s===false)return null;if(!s||s===true)return {};if('const'in s)return s.const;if(s.enum?.length)return s.enum[0];const t=Array.isArray(s.type)?s.type.find(x=>x!=='null'):s.type;if(t==='object')return Object.fromEntries(Object.entries(s.properties||{}).filter(([k])=>s.required?.includes(k)).map(([k,v])=>[k,example(v)]));if(t==='array')return [];if(t==='number'||t==='integer')return s.minimum??0;if(t==='boolean')return false;if(t==='null')return null;return '';}
function updateInputDisabled(){const walk=(node,enabled)=>{if(!node)return;const active=enabled&&(!node.included||node.included.checked);if(node.included)node.included.disabled=state.busy||!enabled;if(node.input)node.input.disabled=state.busy||!active||'fixed'in node.input.dataset;for(const [,child]of node.children)walk(child,active);};walk(state.inputTree,true);}
function formJSON(validate=true){const read=node=>{if(node.included&&!node.included.checked)return undefined;const s=node.schema;if(s.type==='object'){const entries=node.children.map(([key,child])=>[key,read(child)]).filter(([,value])=>value!==undefined);return '{'+entries.map(([k,v])=>JSON.stringify(k)+':'+v).join(',')+'}';}
 const input=node.input;let value=input.value;if(validate&&!validField(input)){throw Error('필수 입력과 형식을 확인하세요.');}if(input.tagName==='SELECT'){if(value===''){if(validate)throw Error('선택한 입력 항목의 값을 정하세요.');return 'null';}return JSON.stringify(node.values[Number(value)]);}
 if(s.type==='number'||s.type==='integer'){if(!value&&!validate)return 'null';if(!/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value)||!Number.isFinite(Number(value)))throw Error('숫자 형식을 확인하세요: '+node.path.join('.'));if(s.type==='integer'&&!Number.isInteger(Number(value)))throw Error('정수를 입력하세요: '+node.path.join('.'));return value;}return JSON.stringify(value);};return read(state.inputTree);}
function inputMode(){$('input-fields').hidden=state.jsonMode;$('json-input').hidden=!state.jsonMode;$('use-json').hidden=state.jsonMode;$('use-form').hidden=!state.jsonMode||!state.inputTree;controls();}
function argumentsText(){const raw=state.jsonMode?$('arguments').value:formJSON();try{JSON.parse(raw);}catch{throw Error('JSON 형식을 확인하세요.');}return raw;}
function renderCalls(){const box=$('recent-calls');box.replaceChildren();if(!state.calls.size){box.append(el('p','아직 이 페이지에서 확인한 실행이 없습니다. 도구 목록에서 도구를 열어 시험해 보세요.','empty'));return;}
 for(const c of [...state.calls.values()].reverse()){const row=el('div',undefined,'recent-call'),body=el('div');body.append(el('strong',c._name||'도구 실행'));const b=el('span');badge(b,c.state);body.append(el('p',(c.mode==='test'?'시험 실행':'실행')+' · '+c.tool_call_id.slice(0,8),'hint'));const button=el('button','결과 보기','secondary');button.onclick=()=>action(async()=>{await waitCall(c.tool_call_id);navigate('results');});row.append(body,b,button);box.append(row);}}
function showCall(c){const old=state.calls.get(c.tool_call_id);c._name=old?._name||(c.tool_version_id===state.version?.tool_version_id?state.tool?.display_name:'도구 실행');state.calls.set(c.tool_call_id,c);if(state.calls.size>50)state.calls.delete(state.calls.keys().next().value);state.activeCall=c;$('result-panel').hidden=false;badge($('call-state'),c.state);$('result-message').textContent=callMessages[c.state]||c.state;
 const clean={...c};delete clean._name;$('result').textContent=show(clean);$('result-id').textContent='실행 ID: '+c.tool_call_id;$('result-data').hidden=c.state!=='succeeded'&&!c.error;$('result-data').textContent=c.state==='succeeded'?show(c.result?.data??c.result):show(c.error);$('call-id').value=c.tool_call_id;$('refresh-call').hidden=false;
 if(c.state==='succeeded'&&c.mode==='test')state.tested.add(c.tool_version_id);renderKnowledgeResult(c);renderCalls();}
async function waitCall(id){if(state.pendingCall&&state.pendingCall!==id)throw Error('확인 중인 실행을 먼저 조회하세요: '+state.pendingCall);$('call-id').value=id;const until=Date.now()+250000;for(;;){let c;try{c=await api('/api/tool-calls/'+id);}catch(e){if(!state.pendingCall)throw e;throw Error('실행 ID를 보관했습니다. 실행 결과에서 이 실행을 다시 조회하세요. 새 실행은 시작하지 않습니다.');}showCall(c);if(!['accepted','running'].includes(c.state)){state.pendingCall=null;return c;}state.pendingCall=id;if(Date.now()>until){notice('아직 처리 중입니다. 이 실행 결과 다시 확인을 눌러 조회하세요.');return c;}await new Promise(resolve=>setTimeout(resolve,2000));}}
async function execute(mode){const v=state.version;if(!v||state.pendingCall)return;const args=argumentsText();if(v.kind==='openapi_http_v1'&&v.binding.effect!=='read'&&!state.keys.has('/api/tool-calls')&&!confirm('이 요청은 실제 서비스의 데이터를 변경할 수 있습니다. 선택한 연결과 입력으로 실행할까요?'))return;
 const raw='{"tool_version_id":'+JSON.stringify(v.tool_version_id)+',"mode":'+JSON.stringify(mode)+',"arguments":'+args+'}';const accepted=await api('/api/tool-calls',{method:'POST',body:raw,raw:true});state.calls.set(accepted.tool_call_id,{...accepted,mode,tool_version_id:v.tool_version_id,state:'accepted',_name:state.tool.display_name});state.pendingCall=accepted.tool_call_id;showCall(state.calls.get(accepted.tool_call_id));notice('실행 요청을 저장했습니다.');await waitCall(accepted.tool_call_id);}
// Direct registration preserves schema JSON tokens and uses the same server compiler as imports.
// No external API is invoked while selecting a mode, preparing inputs or validating a definition.
const DIRECT_OPERATION_ID='agent_console_direct_v1';
function jsonNode(raw,label){
 if(new TextEncoder().encode(raw).length>65536)throw Error(label+': schema는 64 KiB 이내로 입력하세요.');
 let pos=0,nodes=0;const ws=()=>{while(/[\t\r\n ]/.test(raw[pos]||'!'))pos++;};
 const fail=()=>{throw Error(label+': JSON 형식·중복 키·숫자 범위를 확인하세요.');};
 const parse=(depth=0)=>{ws();const start=pos;if(depth>24||++nodes>6000)fail();let value,children;
  if(raw[pos]==='"'){pos++;while(pos<raw.length){if(raw[pos]==='\\'){pos+=2;continue;}if(raw[pos++]==='"')break;}try{value=JSON.parse(raw.slice(start,pos));}catch{fail();}}
  else if(raw[pos]==='{'){pos++;children=new Map();ws();if(raw[pos]!=='}')for(;;){ws();if(raw[pos]!=='"')fail();const key=parse(depth+1).value;ws();if(raw[pos++]!==':'||children.has(key))fail();children.set(key,parse(depth+1));ws();if(raw[pos]!==',')break;pos++;}if(raw[pos++]!=='}')fail();value=Object.fromEntries([...children].map(([k,v])=>[k,v.value]));}
  else if(raw[pos]==='['){pos++;children=[];ws();if(raw[pos]!==']')for(;;){children.push(parse(depth+1));ws();if(raw[pos]!==',')break;pos++;}if(raw[pos++]!==']')fail();value=children.map(x=>x.value);}
  else{const token=/^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(raw.slice(pos));if(!token)fail();pos+=token[0].length;value=JSON.parse(token[0]);if(typeof value==='number'&&!Number.isFinite(value))fail();}
  return {value,children,raw:raw.slice(start,pos)};
 };const node=parse();ws();if(pos!==raw.length)fail();if(typeof node.value!=='boolean'&&(!node.value||Array.isArray(node.value)||typeof node.value!=='object'))throw Error(label+': JSON Schema 객체를 입력하세요.');return node;
}
function rawObject(entries){return '{'+entries.map(([key,value])=>JSON.stringify(key)+':'+value).join(',')+'}';}
function schemaParameters(raw,where){
 if(!raw.trim())return [];
 const node=jsonNode(raw,where+' schema'),s=node.value;
 const allowed=['type','properties','required','additionalProperties','title','description'];
 if(!s||s.type!=='object'||!s.properties||Array.isArray(s.properties)||typeof s.properties!=='object'||Object.keys(s).some(k=>!allowed.includes(k))||('additionalProperties'in s&&s.additionalProperties!==false))throw Error(where+': type=object, properties, required로 매개변수를 정의하세요. 속성 묶음에 대한 추가 제약은 지원하지 않습니다.');
 const required=s.required??[];if(!Array.isArray(required)||required.some(k=>typeof k!=='string'||!Object.hasOwn(s.properties,k))||new Set(required).size!==required.length)throw Error(where+': required에 정의한 속성 이름만 넣으세요.');
 return [...node.children.get('properties').children].map(([name,schema])=>{
  if(where==='path'&&!required.includes(name))throw Error('Path 변수는 모두 required에 포함해야 합니다: '+name);
  return {name,where,required:required.includes(name),schema};
 });
}
function directPlan(){
 const endpoint=$('direct-endpoint').value.trim(),method=$('direct-method').value,location=$('direct-input-location').value;
 if(!['GET','POST','PUT','PATCH','DELETE'].includes(method))throw Error('지원하는 요청 방식을 선택하세요.');
 const match=/^(https?:\/\/[^/?#]+)(\/[^?#]*)?$/.exec(endpoint);
 if(!match||/[\s\\%\x00-\x1f]/.test(endpoint))throw Error('요청 주소는 http(s) 전체 주소로 입력하세요. Query 값·인증 정보·#·인코딩된 경로는 제외하세요.');
 let url;try{url=new URL(match[1]);}catch{throw Error('서버 주소와 포트를 확인하세요.');}
 if(url.username||url.password||/[{}]/.test(match[1]))throw Error('서버 주소에 인증 정보나 변수를 넣지 마세요.');
 const path=match[2]||'/';if(path.length>2048||path.split('/').some(p=>p==='.'||p==='..'))throw Error('경로 길이 또는 . / .. 경로를 확인하세요.');
 const placeholders=[...new Set([...path.matchAll(/\{([^{}]+)\}/g)].map(x=>x[1]))];if(/[{}]/.test(path.replace(/\{[^{}]+\}/g,'')))throw Error('주소 변수는 {변수이름} 형식으로 입력하세요.');
 if(!['body','query','none'].includes(location))throw Error('입력 전달 위치를 선택하세요.');if(method==='GET'&&location==='body')throw Error('GET은 JSON 본문을 지원하지 않습니다. Query 또는 입력 없음을 선택하세요.');
 let body=null;const parameters=schemaParameters($('direct-path-schema').value,'path');
 if(placeholders.some(k=>!parameters.some(p=>p.name===k))||parameters.some(p=>!placeholders.includes(p.name)))throw Error('주소의 {변수}와 Path schema의 속성 이름을 맞추세요. 추가 입력 위치를 열어 Path schema를 입력하세요.');
 if(location!=='none'){const raw=$('direct-input-schema').value.trim();if(!raw)throw Error('입력 JSON Schema를 입력하세요.');if(location==='body')body=jsonNode(raw,'입력 schema');else parameters.push(...schemaParameters(raw,'query'));}
 if(location!=='query')parameters.push(...schemaParameters($('direct-query-schema').value,'query'));
 parameters.push(...schemaParameters($('direct-header-schema').value,'header'));
 const code=$('direct-status').value.trim();if(!/^2\d\d$/.test(code)||code==='202')throw Error('지원하는 동기 성공 코드(200~299, 202 제외)를 입력하세요.');
 const response=code==='204'?null:jsonNode($('direct-response-schema').value.trim(),'응답 schema');
 return {endpoint:url.origin+path,origin:url.origin,path,method,location,parameters,body,response,code};
}
function directSpecification(plan,cv,name){
 const parameterRaw=plan.parameters.map(p=>rawObject([['name',JSON.stringify(p.name)],['in',JSON.stringify(p.where)],['required',JSON.stringify(p.required)],['schema',p.schema.raw]]));
 const content=raw=>rawObject([['application/json',rawObject([['schema',raw]])]]);
 const response=plan.code==='204'?JSON.stringify({description:'No content'}):rawObject([['description',JSON.stringify('Success')],['content',content(plan.response.raw)]]);
 const entries=[['operationId',JSON.stringify(DIRECT_OPERATION_ID)],['summary',JSON.stringify(name)],['parameters','['+parameterRaw.join(',')+']'],['responses',rawObject([[plan.code,response]])]];
 if(plan.body)entries.push(['requestBody',rawObject([['required','true'],['content',content(plan.body.raw)]])]);
 const security=[];let schemes;
 if(cv.auth_type==='bearer'){schemes={directAuth:{type:'http',scheme:'bearer'}};security.push({directAuth:[]});}
 else if(cv.auth_type==='api_key'){schemes={directAuth:{type:'apiKey',in:cv.api_key_in,name:cv.api_key_name}};security.push({directAuth:[]});}
 else if(cv.auth_type!=='none')throw Error('이 연결의 인증 방식은 직접 등록에서 지원하지 않습니다.');
 entries.push(['security',JSON.stringify(security)]);
 const root=[['openapi',JSON.stringify('3.1.0')],['info',JSON.stringify({title:name,version:'1.0'})],['servers',JSON.stringify([{url:plan.origin}])],['paths',rawObject([[plan.path,rawObject([[plan.method.toLowerCase(),rawObject(entries)]])]])]];
 if(schemes)root.push(['components',JSON.stringify({securitySchemes:schemes})]);
 return rawObject(root);
}
function modeView(){const knowledge=state.registration==='knowledge';for(const mode of ['direct','openapi','knowledge']){$(mode+'-registration').hidden=mode!==state.registration;$('registration-'+mode).setAttribute('aria-pressed',String(mode===state.registration));}$('http-setup').hidden=knowledge;$('knowledge-setup').hidden=!knowledge;$('step-label-1').textContent=knowledge?'1. 지식 선택':'1. API 등록';$('step-label-2').textContent=knowledge?'2. 도구 설정':'2. 연결 설정';}
function setRegistration(mode){
 if(state.busy)return;if(state.keys.size||state.draftRequest||state.savedVersion||state.savedConnection){notice('진행 중인 저장 요청을 먼저 확인하세요.',true);return;}
 state.registration=mode;state.operation=null;state.imported=null;state.direct=null;state.directImport=null;state.step=1;$('base-url').readOnly=false;$('target-check').textContent='';$('operations').replaceChildren();$('spec-summary').textContent='';$('operation-selection').textContent='파일을 분석하고 기능을 선택하세요.';modeView();controls();notice('');
}
function directFields(){const location=$('direct-input-location').value;$('direct-input-box').hidden=location==='none';$('direct-query-box').hidden=location==='query';const empty=$('direct-status').value==='204';$('direct-response-box').hidden=empty;$('direct-empty-response').hidden=!empty;}
function prepareDirect(){
 const plan=directPlan();state.direct=plan;state.directImport=null;state.imported=null;state.version=null;state.operation={key:plan.method+' '+plan.path,method:plan.method,path:plan.path,auth_options:[]};
 if(!state.tool){$('tool-name').value=plan.method+' '+plan.path;$('tool-description').value='';}
 $('selected-operation').textContent=plan.method+' '+plan.endpoint;$('base-url').value=plan.origin;$('base-url').readOnly=true;$('connection-name').value=new URL(plan.origin).host;
 // A previously selected connection must be explicitly checked for the new full URL.
 $('connections').value='';state.connectionVersions=[];clear($('connection-versions'),'버전 선택');$('selected-connection').textContent='';$('target-check').textContent='요청 주소의 서버: '+plan.origin;$('auth-choice').hidden=true;$('auth-match').textContent='서비스 연결에서 인증 방식을 지정하세요.';
 $('mapping').textContent=show({method:plan.method,endpoint:plan.endpoint,input_location:plan.location,success_status:plan.code});$('create-connection').open=true;state.step=2;navigate('add');notice('주소와 schema를 확인했습니다. 서비스 연결을 선택하거나 저장하세요. 아직 대상 API를 실행하지 않았습니다.');
}
function directTargetMatches(cv){return !state.direct||(cv.base_url.replace(/\/+$/,'')+state.direct.path===state.direct.endpoint);}
async function compileDirect(cv){
 if(!directTargetMatches(cv))throw Error('선택한 연결의 주소가 직접 입력한 요청 주소와 다릅니다. '+state.direct.origin+' 연결을 사용하세요.');
 const raw=directSpecification(state.direct,cv,$('tool-name').value.trim());
 if(!state.directImport||state.directImport.raw!==raw)state.directImport={raw,imported:await api('/api/openapi-imports',{method:'POST',body:raw,raw:true})};
 const imported=state.directImport.imported,op=imported.analysis.operations.find(o=>o.key===state.direct.method+' '+state.direct.path);
 if(!op?.supported||imported.analysis.problems.length)throw Error('API 정의를 확인하세요: '+[...(imported.analysis.problems||[]),...(op?.problems||[])].map(p=>p.pointer+': '+p.reason).join(' / '));
 state.imported=imported;state.operation=op;clear($('auth-scheme'));for(const a of op.auth_options)option($('auth-scheme'),a.name,a.name);$('mapping').textContent=show({input_schema:op.input_schema,responses:op.responses});
}
function restoreDirect(op,cv){
 state.registration='direct';const schemas=op.input_schema.properties||{};const location=Object.hasOwn(schemas,'body')?'body':schemas.query?'query':'none';
 $('direct-method').value=op.method;$('direct-endpoint').value=cv.base_url.replace(/\/+$/,'')+op.path;$('direct-input-location').value=location;$('direct-input-schema').value=location==='none'?'':show(schemas[location]);
 $('direct-path-schema').value=schemas.path?show(schemas.path):'';$('direct-query-schema').value=location!=='query'&&schemas.query?show(schemas.query):'';$('direct-header-schema').value=schemas.header?show(schemas.header):'';
 const codes=Object.keys(op.responses);$('direct-status').value=codes[0];$('direct-response-schema').value=codes[0]==='204'?'':show(op.responses[codes[0]]);
 state.direct=directPlan();state.directImport=null;$('base-url').value=state.direct.origin;$('base-url').readOnly=true;directFields();modeView();
}
const knowledgeRequestSchema={type:'object',additionalProperties:false,required:['index_version_id','embedding_space_id','query_embedding'],properties:{index_version_id:{type:'string',format:'uuid'},embedding_space_id:{type:'string',pattern:'^es_[a-f0-9]{64}$'},query_embedding:{type:'array',items:{type:'number'},minItems:1024,maxItems:1024},top_k:{type:'integer',minimum:1,maximum:50},candidate_k:{type:'integer',minimum:1,maximum:100}}};
const knowledgeResponseSchema={type:'object',required:['index_id','index_version_id','embedding_space_id','returned_count','results'],properties:{index_id:{type:'string',format:'uuid'},index_version_id:{type:'string',format:'uuid'},embedding_space_id:{type:'string'},returned_count:{type:'integer',minimum:0},results:{type:'array',items:{type:'object',additionalProperties:true}}},additionalProperties:true};
$('registration-direct').onclick=()=>setRegistration('direct');$('registration-openapi').onclick=()=>setRegistration('openapi');
$('direct-next').onclick=()=>action(prepareDirect);$('direct-form').onsubmit=e=>{e.preventDefault();$('direct-next').click();};
$('direct-input-location').onchange=directFields;$('direct-status').oninput=directFields;
$('direct-method').onchange=()=>{if($('direct-method').value==='GET'&&$('direct-input-location').value==='body')$('direct-input-location').value='query';directFields();};
$('knowledge-example').onclick=()=>{if(state.busy)return;$('direct-method').value='POST';$('direct-endpoint').value='http://knowledge-gateway:8000/api/indexes/{index_id}/search';$('direct-input-location').value='body';$('direct-input-schema').value=show(knowledgeRequestSchema);$('direct-response-schema').value=show(knowledgeResponseSchema);$('direct-status').value='200';$('direct-path-schema').value=show({type:'object',properties:{index_id:{type:'string',format:'uuid'}},required:['index_id'],additionalProperties:false});$('direct-query-schema').value='';$('direct-header-schema').value='';directFields();notice('지식 검색 API 양식을 채웠습니다. 실제 질문 벡터는 시험 입력 준비 스크립트로 만드세요. API 호출은 하지 않았습니다.');};

$('login-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(state.connected)throw Error('다른 토큰을 쓰려면 먼저 연결을 해제하세요.');state.token=$('token').value.trim();state.connected=false;try{const status=await api('/api/status');if(status.stage!=='A07'||status.schema_version!==5)throw Error('A07 서버 업데이트가 필요합니다.');await refresh();state.connected=true;$('token').value='';$('login-panel').hidden=true;$('logout').hidden=false;$('connection-status').textContent='연결됨';$('connection-dot').classList.add('connected');renderTools();notice('');}catch(e){state.token='';$('connection-status').textContent='연결 실패';$('connection-dot').classList.remove('connected');throw e;}});};
$('show-login').onclick=()=>{$('login-panel').hidden=!$('login-panel').hidden;};
$('logout').onclick=()=>{if(state.busy)return;if((state.pendingCall||state.keys.size||(typeof agentUI!=='undefined'&&agentUI.pending)||(typeof workflowUI!=='undefined'&&(workflowUI.patch||workflowUI.saved||workflowUI.draft)))&&!confirm('확인 중인 요청이 있습니다. 실행 ID를 보관한 뒤 연결을 해제하세요. 연결을 해제할까요?'))return;if(typeof resetAgents==='function')resetAgents();if(typeof resetWorkflows==='function')resetWorkflows();state.token='';state.connected=false;state.keys.clear();state.calls.clear();state.tested.clear();state.pendingCall=null;state.activeCall=null;state.savedConnection=null;state.tools=[];state.connections=[];resetDraft();clear($('connections'),'서비스 선택');clear($('imports'),'파일 선택');$('token').value='';$('call-id').value='';$('result').textContent='';$('result-data').textContent='';$('result-panel').hidden=true;$('login-panel').hidden=false;$('logout').hidden=true;$('connection-status').textContent='연결 설정';$('connection-dot').classList.remove('connected');renderTools();navigate('tools');};
$('nav-tools').onclick=$('detail-list').onclick=$('cancel-add').onclick=()=>navigate('tools');$('new-tool').onclick=startNew;$('nav-add').onclick=()=>{if(state.tool&&state.step===3||state.view==='detail')startNew();else navigate('add');};$('nav-results').onclick=()=>navigate('results');$('refresh').onclick=()=>action(refresh);$('tool-search').oninput=renderTools;
$('import-file').onclick=()=>action(async()=>{const f=$('spec-file').files[0];if(!f)throw Error('API 설명 파일을 선택하세요.');if(f.size>5*1024*1024)throw Error('최대 5 MiB까지 지원합니다.');const type=/\.ya?ml$/i.test(f.name)?'application/yaml':/\.json$/i.test(f.name)?'application/json':null;if(!type)throw Error('JSON 또는 YAML 파일을 선택하세요.');const raw=new TextDecoder('utf-8',{fatal:true}).decode(await f.arrayBuffer());renderImport(await api('/api/openapi-imports',{method:'POST',body:raw,raw:true,type}));await refresh();notice('파일을 분석했습니다. 사용할 기능을 하나 선택하세요.');});
$('imports').onchange=()=>action(async()=>{if($('imports').value)renderImport(await api('/api/openapi-imports/'+$('imports').value));});$('operation-search').oninput=renderOperations;
$('next-connection').onclick=()=>{state.step=2;$('create-connection').open=!state.connections.length;navigate('add');notice('');};$('back-operation').onclick=()=>{if(state.draftRequest||state.savedVersion){notice('저장 요청을 먼저 확인하세요.',true);return;}state.step=1;navigate('add');};
$('connections').onchange=()=>action(()=>selectConnection($('connections').value));$('connection-versions').onchange=connectionDetail;$('auth-scheme').onchange=matchAuth;$('auth-type').onchange=authFields;
$('connection-form').onsubmit=e=>{e.preventDefault();action(async()=>{const auth=$('auth-type').value;const body={display_name:$('connection-name').value.trim(),base_url:$('base-url').value.trim(),auth_type:auth};if(auth!=='none'){body.credential_ref=$('credential-ref').value.trim();body.credential_revision=$('credential-revision').value.trim();}if(auth==='api_key'){body.api_key_in=$('api-key-in').value;body.api_key_name=$('api-key-name').value.trim();}const c=state.savedConnection||await api('/api/connections',{method:'POST',body});state.savedConnection=c;await refresh();$('connections').value=c.connection_id;await selectConnection(c.connection_id);$('create-connection').open=false;state.savedConnection=null;notice('서비스 연결을 저장했습니다. 도구를 저장하고 시험할 수 있습니다.');});};
$('draft-form').onsubmit=e=>{e.preventDefault();$('create-draft').click();};
$('create-draft').onclick=()=>action(async()=>{if(!state.savedVersion&&!state.draftRequest){if(!['tool-name','tool-description','timeout'].every(id=>validField($(id))))return;const cv=state.connectionVersions.find(v=>v.connection_version_id===$('connection-versions').value);if(!cv)throw Error('서비스 연결을 선택하세요.');if(state.direct)await compileDirect(cv);const auth=state.operation.auth_options.find(a=>a.name===$('auth-scheme').value);if(!cv||!auth||!matchesAuth(auth,cv))throw Error('선택한 서비스의 접속 방식이 기능의 인증과 일치해야 합니다.');
 let t=state.tool;if(!t){t=await api('/api/tools',{method:'POST',body:{display_name:$('tool-name').value.trim(),description:$('tool-description').value.trim()}});state.tool=t;}else t=await api('/api/tools/'+t.tool_id);
 state.draftRequest={path:'/api/tools/'+t.tool_id+'/versions',body:{expected_revision:t.revision,kind:'openapi_http_v1',binding:{import_id:state.imported.import_id,operation_key:state.operation.key,connection_version_id:cv.connection_version_id,auth_scheme:auth.name,effect:$('effect').value,forward_idempotency:$('forward-key').checked},timeout_ms:Math.round(Number($('timeout').value)*1000)}};}
 if(!state.savedVersion){try{state.savedVersion=await api(state.draftRequest.path,{method:'POST',body:state.draftRequest.body});}catch(e){if(!state.keys.has(state.draftRequest.path))state.draftRequest=null;throw e;}}
 const saved=state.savedVersion;await refresh();await selectTool(saved.tool_id,saved.tool_version_id);state.savedVersion=null;state.draftRequest=null;state.step=3;navigate('add');notice('초안을 저장했습니다. 값을 입력하고 시험 실행을 눌러 주세요.');});
$('versions').onchange=()=>action(async()=>{setVersion(await api('/api/tool-versions/'+$('versions').value));});
$('new-version').onclick=()=>action(async()=>{const v=state.version;if(v.kind==='knowledge_search_v1'){await newKnowledgeVersion(v);return;}const imp=await api('/api/openapi-imports/'+v.binding.import_id);renderImport(imp);chooseOperation(v.binding.http);const cv=await api('/api/connection-versions/'+v.binding.connection_version_id);$('connections').value=cv.connection_id;await selectConnection(cv.connection_id,cv.connection_version_id);if(v.binding.http.operation_id===DIRECT_OPERATION_ID)restoreDirect(v.binding.http,cv);$('auth-scheme').value=v.binding.auth_scheme;matchAuth();$('effect').value=v.binding.effect;$('forward-key').checked=!!v.binding.forward_idempotency;$('timeout').value=v.timeout_ms/1000;$('tool-name').value=state.tool.display_name;$('tool-description').value=state.tool.description;$('wizard-title').textContent='도구에 새 버전 추가';state.step=2;navigate('add');notice('기존 버전은 그대로 보존됩니다. 변경한 설정을 새 버전으로 저장하세요.');});
$('load-arguments').onclick=()=>action(async()=>{const file=$('arguments-file').files[0];if(!file||!file.name.toLowerCase().endsWith('.json'))throw Error('시험 입력 JSON 파일을 선택하세요.');if(file.size>65536)throw Error('시험 입력은 64 KiB 이내로 준비하세요.');const raw=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());const node=jsonNode(raw,'시험 입력');if(!node.value||typeof node.value!=='object'||Array.isArray(node.value))throw Error('시험 입력은 JSON 객체여야 합니다.');$('arguments').value=raw;state.jsonMode=true;inputMode();notice('시험 입력을 불러왔습니다. 값을 확인한 뒤 시험 실행을 누르세요.');});
$('use-json').onclick=()=>{try{$('arguments').value=formJSON(false);state.jsonMode=true;inputMode();}catch(e){notice(e.message,true);}};$('use-form').onclick=()=>{if(confirm('직접 입력한 JSON을 닫고 이전 입력칸의 값으로 돌아갈까요?')){state.jsonMode=false;inputMode();}};
$('test-call').onclick=()=>action(()=>execute('test'));$('run-call').onclick=()=>action(()=>execute('run'));
for(const [id,verb]of [['publish','publish'],['retire','retire']])$(id).onclick=()=>action(async()=>{const v=state.version;if(verb==='retire'&&!confirm('이 버전을 폐기할까요? 되돌릴 수 없으며 기존 실행 기록은 보존됩니다.'))return;const changed=await api('/api/tool-versions/'+v.tool_version_id+'/'+verb,{method:'POST',body:{expected_revision:v.revision}});state.version=changed;setVersion(changed);await refresh();await selectTool(v.tool_id,changed.tool_version_id);notice(verb==='publish'?'사용을 시작했습니다. 이 도구를 실행하거나 다른 서비스에 연결할 수 있습니다.':'이 버전을 폐기했습니다. 기존 실행 기록은 유지됩니다.');});
$('disable-tool').onclick=()=>action(async()=>{const t=await api('/api/tools/'+state.tool.tool_id);if(t.enabled&&!confirm('이 도구의 새 실행을 중지할까요? 기존 기록은 유지됩니다.'))return;const versionID=state.version?.tool_version_id;await api('/api/tools/'+t.tool_id,{method:'PATCH',body:{expected_revision:t.revision,enabled:!t.enabled}});await refresh();await selectTool(t.tool_id,versionID);notice(t.enabled?'도구 사용을 중지했습니다.':'도구 사용을 재개했습니다.');});
$('lookup-form').onsubmit=e=>{e.preventDefault();action(async()=>{const id=$('call-id').value.trim();if(!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id))throw Error('올바른 실행 ID를 입력하세요.');if(state.pendingCall&&state.pendingCall!==id)throw Error('확인 중인 실행을 먼저 조회하세요: '+state.pendingCall);await waitCall(id);navigate('results');});};$('refresh-call').onclick=()=>action(()=>waitCall(state.pendingCall||state.activeCall.tool_call_id));
window.addEventListener('beforeunload',e=>{if(state.keys.size||state.pendingCall||(typeof agentUI!=='undefined'&&agentUI.pending)||(typeof workflowUI!=='undefined'&&(workflowUI.patch||workflowUI.saved||workflowUI.draft))){e.preventDefault();e.returnValue='';}});
const knowledgeState={items:[],cursor:'',resolved:null,connections:null,signature:'',saved:{}};
function resetKnowledge(){knowledgeState.resolved=null;$('knowledge-summary').textContent='';$('knowledge-binding').textContent='';$('knowledge-name').value='';$('knowledge-description').value='';}
function knowledgeControls(){const ready=state.connected&&!state.busy;
 for(const id of ['knowledge-load','knowledge-more'])$(id).disabled=!ready;
 $('knowledge-next').disabled=!ready||!$('knowledge-index').value||!knowledgeState.connections;
 $('knowledge-save').disabled=!ready||!knowledgeState.resolved;
 $('knowledge-name').readOnly=$('knowledge-description').readOnly=!!state.tool;
 // Uncertain creation must be recovered using the same connection settings.
 const frozen=state.keys.size>0||!!state.draftRequest||!!state.savedVersion;
 for(const id of ['knowledge-connection','model-connection','knowledge-url','model-url','knowledge-index'])$(id).disabled=!ready||frozen;
}
function knowledgeConnections(){for(const kind of ['knowledge','model']){const select=$(kind+'-connection'),old=select.value;clear(select,'기본 주소로 연결');for(const c of state.connections)option(select,c.connection_id,c.display_name);select.value=old;$(kind+'-url-label').hidden=!!select.value;}}
function knowledgeSignature(){return JSON.stringify(['knowledge','model'].map(k=>[$(k+'-connection').value,$(k+'-url').value.trim()]));}
function invalidateKnowledge(){knowledgeState.connections=null;knowledgeState.resolved=null;knowledgeState.items=[];knowledgeState.cursor='';clear($('knowledge-index'),'지식 선택');$('knowledge-more').hidden=true;$('knowledge-list-help').textContent='연결 설정이 바뀌었습니다. 지식 목록을 다시 불러오세요.';for(const k of ['knowledge','model'])$(k+'-url-label').hidden=!!$(k+'-connection').value;controls();}
async function knowledgeConnection(kind){const chosen=$(kind+'-connection').value;
 if(chosen){const c=await api('/api/connections/'+chosen);return await api('/api/connection-versions/'+c.current_version_id);}
 const base=$(kind+'-url').value.trim().replace(/\/+$/,'');let url;try{url=new URL(base);}catch{throw Error('서비스 주소를 확인하세요.');}
 if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash||!['','/'].includes(url.pathname))throw Error('서비스의 http(s) 주소와 포트만 입력하세요.');
 const signature=kind+' '+base;if(knowledgeState.saved[signature])return knowledgeState.saved[signature];
 // Reuse an existing default connection only when URL and no-auth match exactly.
 const name=kind==='knowledge'?'Knowledge Platform · 지식 검색':'Model Platform · 지식 검색';
 for(const c of state.connections.filter(c=>c.display_name===name)){const cv=await api('/api/connection-versions/'+c.current_version_id);if(cv.base_url.replace(/\/+$/,'')===base&&cv.auth_type==='none'){knowledgeState.saved[signature]=cv;return cv;}}
 const c=await api('/api/connections',{method:'POST',body:{display_name:name,base_url:base,auth_type:'none'}});
 // Record the returned version immediately: a later GET/refresh outage must not create a duplicate.
 const cv={connection_version_id:c.current_version_id,connection_id:c.connection_id,base_url:base,auth_type:'none'};knowledgeState.saved[signature]=cv;return cv;
}
async function inspectKnowledge(body){const c=knowledgeState.connections;if(!c)throw Error('지식 목록을 먼저 불러오세요.');return api('/api/knowledge/inspect',{method:'POST',body:{connection_version_id:c.knowledge.connection_version_id,model_connection_version_id:c.model.connection_version_id,...body}});}
function renderKnowledgeIndexes(){const selected=$('knowledge-index').value;clear($('knowledge-index'),'지식 선택');for(const i of knowledgeState.items){option($('knowledge-index'),i.index_id,i.name+' · '+i.index_id.slice(0,8)+(i.selectable?'':' · 준비되지 않음'));$('knowledge-index').children[$('knowledge-index').children.length-1].disabled=!i.selectable;}if(knowledgeState.items.some(i=>i.index_id===selected&&i.selectable))$('knowledge-index').value=selected;$('knowledge-more').hidden=!knowledgeState.cursor;$('knowledge-list-help').textContent=knowledgeState.items.length?'지식을 선택하세요. 이름이 같은 경우 뒤의 번호로 구분할 수 있습니다.':'등록된 지식이 없습니다. 지식플랫폼에서 준비한 뒤 다시 불러오세요.';}
async function loadKnowledge(more=false){
 const signature=knowledgeSignature();if(more&&signature!==knowledgeState.signature)throw Error('연결 설정이 바뀌었습니다. 목록을 다시 불러오세요.');
 if(!more){knowledgeState.resolved=null;knowledgeState.connections=null;knowledgeState.items=[];knowledgeState.cursor='';clear($('knowledge-index'),'지식 선택');const knowledge=await knowledgeConnection('knowledge'),model=await knowledgeConnection('model');knowledgeState.connections={knowledge,model};knowledgeState.signature=signature;}
 const page=await inspectKnowledge({action:'indexes',...(more&&knowledgeState.cursor?{cursor:knowledgeState.cursor}:{})});
 for(const item of page.items)if(!knowledgeState.items.some(i=>i.index_id===item.index_id))knowledgeState.items.push(item);
 if(more&&page.next_cursor&&page.next_cursor===knowledgeState.cursor)throw Error('지식 목록의 다음 페이지를 확인할 수 없습니다.');knowledgeState.cursor=page.next_cursor||'';renderKnowledgeIndexes();await refresh();notice('지식 목록을 불러왔습니다. 아직 임베딩이나 검색은 실행하지 않았습니다.');
}
function renderKnowledgeSetup(resolved){knowledgeState.resolved=resolved;$('knowledge-summary').textContent=resolved.index_name+' · 검색 가능한 조각 '+resolved.chunk_count+'개';$('knowledge-binding').textContent=show(resolved.binding);if(!state.tool){$('knowledge-name').value=resolved.index_name+' 검색';$('knowledge-description').value=resolved.index_name+'에서 질문과 관련된 내용을 찾습니다.';}else{$('knowledge-name').value=state.tool.display_name;$('knowledge-description').value=state.tool.description;}state.registration='knowledge';state.step=2;modeView();navigate('add');}
async function saveKnowledge(){if(!state.savedVersion&&!state.draftRequest){if(!['knowledge-name','knowledge-description'].every(id=>validField($(id))))return;if(!knowledgeState.resolved)throw Error('검색할 지식을 먼저 선택하세요.');let t=state.tool;
 if(!t){t=await api('/api/tools',{method:'POST',body:{display_name:$('knowledge-name').value.trim(),description:$('knowledge-description').value.trim()}});state.tool=t;}else t=await api('/api/tools/'+t.tool_id);
 state.draftRequest={path:'/api/tools/'+t.tool_id+'/versions',body:{expected_revision:t.revision,kind:'knowledge_search_v1',binding:knowledgeState.resolved.binding,timeout_ms:180000}};
 }
 if(!state.savedVersion){try{state.savedVersion=await api(state.draftRequest.path,{method:'POST',body:state.draftRequest.body});}catch(e){if(!state.keys.has(state.draftRequest.path))state.draftRequest=null;throw e;}}
 const saved=state.savedVersion;await refresh();await selectTool(saved.tool_id,saved.tool_version_id);state.savedVersion=null;state.draftRequest=null;state.step=3;navigate('add');notice('도구를 저장했습니다. 질문을 입력하고 시험 실행을 누르세요.');
}
async function newKnowledgeVersion(v){state.registration='knowledge';state.direct=null;state.operation=null;resetKnowledge();const k=await api('/api/connection-versions/'+v.binding.connection_version_id),m=await api('/api/connection-versions/'+v.binding.knowledge.model_connection_version_id);$('knowledge-connection').value=k.connection_id;$('model-connection').value=m.connection_id;invalidateKnowledge();$('wizard-title').textContent='도구에 새 버전 추가';state.step=1;modeView();navigate('add');notice('지식 목록을 불러오고 사용할 지식을 다시 선택하세요. 기존 버전과 실행 기록은 유지됩니다.');}
function knowledgeFormSchema(v){const p=v.input_schema.properties;return {...v.input_schema,properties:{query:p.query,top_k:p.top_k}};}
function knowledgeDetail(v){const yes=v.kind==='knowledge_search_v1';$('input-heading').textContent=yes?'어떤 내용을 찾을까요?':'시험할 값 입력';$('knowledge-detail').hidden=!yes;if(!yes)return;
 $('knowledge-detail').textContent='질문을 임베딩으로 변환한 뒤, 저장된 지식 버전에서 검색합니다.';
 $('call-warning').textContent='시험 실행도 실제 모델과 지식플랫폼을 호출합니다. 검색 결과를 확인한 뒤 사용을 시작하세요.';
 $('input-help').textContent='질문만 입력해도 됩니다. 기본 '+v.binding.knowledge.top_k+'개를 검색합니다. 필터가 필요하면 JSON 입력에서 지정할 수 있습니다.';
 $('rest-example').textContent='POST /api/tool-calls\nAuthorization: Bearer <접속 토큰>\nIdempotency-Key: <새 UUID>\n'+show({tool_version_id:v.tool_version_id,mode:'run',arguments:{query:'AI 엔지니어에게 필요한 역량은 무엇인가요?'}});
}
const knowledgeStageLabels={input:'질문 확인',index:'지식 확인',embedding_profile:'임베딩 모델 확인',rerank_profile:'재정렬 모델 확인',embedding:'질문 임베딩',search:'지식 검색'};
function renderKnowledgeResult(c){const box=$('knowledge-results');box.replaceChildren();box.hidden=true;
 if(c.error&&knowledgeStageLabels[c.error.stage]){$('result-message').textContent=knowledgeStageLabels[c.error.stage]+' 단계에서 실행하지 못했습니다. '+({KNOWLEDGE_NOT_READY:'지식 또는 모델의 준비 상태를 확인하세요.',KNOWLEDGE_PROFILE_MISMATCH:'고정한 지식·모델 설정이 일치하는지 확인하세요.',CREDENTIAL_NOT_CONFIGURED:'서비스 연결의 인증 정보를 확인하세요.',TOOL_TIMEOUT:'대기 시간이 초과되었습니다. 실행 기록을 확인하세요.',TOOL_INPUT_INVALID:'질문 길이·결과 개수·필터를 확인하세요.'}[c.error.code]||'아래 오류와 기술 정보를 확인하세요.');}
 if(c.state!=='succeeded'||c.result?.metadata?.adapter!=='knowledge_search_v1')return;
 const data=c.result.data;if(!Array.isArray(data?.results))return;box.hidden=false;$('result-data').hidden=true;$('result-message').textContent='질문과 관련된 검색 결과 '+data.returned_count+'개입니다.';
 if(!data.results.length)box.append(el('p','조건에 맞는 결과가 없습니다. 질문이나 필터를 바꿔 보세요.','empty'));
 for(const [i,hit] of data.results.entries()){const card=el('article',undefined,'knowledge-hit');card.append(el('h3','검색 결과 '+(i+1)),el('p',hit.text,'knowledge-hit-text'));const pages=[...new Set((hit.source_spans||[]).map(s=>s.metadata?.location?.page).filter(p=>p!==undefined))];card.append(el('p',(pages.length?'원문 '+pages.join(', ')+'쪽 · ':'')+({text:'본문',table:'표',code:'코드'}[hit.kind]||hit.kind),'hint'));const detail=el('details');detail.append(el('summary','출처·품질·점수 확인'),el('pre',show({source_document_id:hit.source_document_id,canonical_id:hit.canonical_id,retrieval_score:hit.retrieval_score,rerank_score:hit.rerank_score,source_spans:hit.source_spans,quality:hit.quality})));card.append(detail);box.append(card);}
}
$('registration-knowledge').onclick=()=>setRegistration('knowledge');
$('knowledge-load').onclick=()=>action(()=>loadKnowledge());$('knowledge-more').onclick=()=>action(()=>loadKnowledge(true));
for(const kind of ['knowledge','model']){$(kind+'-connection').onchange=invalidateKnowledge;$(kind+'-url').oninput=invalidateKnowledge;}
$('knowledge-index').onchange=()=>{knowledgeState.resolved=null;controls();};
$('knowledge-next').onclick=()=>action(async()=>{const item=knowledgeState.items.find(i=>i.index_id===$('knowledge-index').value);if(!item?.selectable)throw Error('준비된 지식을 선택하세요.');const resolved=await inspectKnowledge({action:'resolve',index_id:item.index_id});renderKnowledgeSetup(resolved);notice('지식과 모델 설정을 확인했습니다. 도구 이름을 정하고 저장하세요.');});
$('knowledge-back').onclick=()=>{if(state.keys.size||state.draftRequest||state.savedVersion){notice('저장 요청을 먼저 확인하세요.',true);return;}state.step=1;navigate('add');};
$('knowledge-save').onclick=()=>action(saveKnowledge);$('knowledge-form').onsubmit=e=>{e.preventDefault();$('knowledge-save').click();};

modeView();directFields();renderTools();navigate('tools');
