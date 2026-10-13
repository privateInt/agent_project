// No npm dependencies. Runs shipped HTML/JS against a DOM and API double.
// This validates behavior, NOT browser layout, real HTTP execution, or PostgreSQL.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
const root=path.join(__dirname,'../gateway/app/web');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8'),js=fs.readFileSync(path.join(root,'app.js'),'utf8');
class Element{
 constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.parentNode=null;this.attrs={};this.dataset={};this.hidden=false;this.disabled=false;this.checked=false;this.required=false;this.readOnly=false;this._text='';this._value='';this.className='';this.files=[];this.classList={add:x=>this.className+=' '+x,remove:x=>this.className=this.className.split(' ').filter(v=>v!==x).join(' ')};}
 append(...items){for(let item of items){if(typeof item==='string'){const n=new Element('#text');n.textContent=item;item=n;}if(item.parentNode)item.parentNode.children=item.parentNode.children.filter(x=>x!==item);item.parentNode=this;this.children.push(item);}}
 replaceChildren(...items){this.children=[];this._text='';this._value='';this.append(...items);}
 set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
 set value(v){this._value=String(v);}get value(){if(this.tagName==='SELECT'){const match=this.children.find(x=>x.value===this._value);return match?match.value:this._value===''?this.children[0]?.value||'':'';}return this._value;}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')this.id=v;if(k.startsWith('data-'))this.dataset[k.slice(5)]=v;}removeAttribute(k){delete this.attrs[k];}
 checkValidity(){if(this.disabled||this.readOnly)return true;if(this.required&&!this.value)return false;if(this.type==='number'&&this.value){const n=Number(this.value);return Number.isFinite(n)&&(!this.min||n>=Number(this.min))&&(!this.max||n<=Number(this.max));}return true;}
 reportValidity(){return this.checkValidity();}focus(){}
 click(){if(!this.disabled)this.onclick?.({preventDefault(){}});}
}
function makeDOM(){const elements=new Map(),body=new Element('body'),stack=[body];for(const match of html.matchAll(/<\/?[a-zA-Z][^>]*>|[^<]+/g)){const token=match[0];if(token.startsWith('</')){stack.pop();continue;}if(!token.startsWith('<')){stack.at(-1)._text+=token;continue;}const tag=token.match(/^<(\w+)/)[1];const n=new Element(tag);for(const a of token.matchAll(/([\w-]+)(?:="([^"]*)")?/g)){const [,k,v]=a;if(k===tag)continue;n.setAttribute(k,v??'');if(k==='id'){assert(!elements.has(v),'duplicate ID '+v);elements.set(v,n);}if(['hidden','required','disabled','checked','readonly'].includes(k))n[k==='readonly'?'readOnly':k]=true;if(['value','type','min','max','maxlength'].includes(k))n[k]=v;}
 stack.at(-1).append(n);if(!['meta','link','input','br','hr'].includes(tag))stack.push(n);}
 const walk=(n,out=[])=>{out.push(n);for(const child of n.children)walk(child,out);return out;};
 return {getElementById:id=>{assert(elements.has(id),'missing HTML ID '+id);return elements.get(id);},createElement:tag=>new Element(tag),createTextNode:text=>{const n=new Element('#text');n.textContent=text;return n;},querySelectorAll:query=>{const tags=query.split(',').map(x=>x.toUpperCase());return walk(body).filter(n=>query==='[data-fixed]'?'fixed'in n.dataset:tags.includes(n.tagName));},body};}
const id=n=>`10000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
const input={type:'object',additionalProperties:false,required:['path'],properties:{path:{type:'object',additionalProperties:false,required:['item_id'],properties:{item_id:{type:'string',minLength:1}}},query:{type:'object',additionalProperties:false,properties:{lang:{type:'string',enum:['ko','en']},limit:{type:'integer'},enabled:{type:'boolean'}}}}};
const op={key:'GET /items/{item_id}',method:'GET',path:'/items/{item_id}',operation_id:'getItem',summary:'상품 정보 조회',supported:true,problems:[],servers:['https://example.test/v1'],auth_options:[{name:'none',type:'none'}],input_schema:input,responses:{200:{type:'object'}}};
const imp={import_id:id(1),title:'상품 서비스',spec_version:'3.0.3',analysis:{title:'상품 서비스',spec_version:'3.0.3',operations:[op,{...op,key:'POST /upload',summary:'파일 업로드',supported:false,problems:[{pointer:'/paths/upload',reason:'multipart unsupported'}]}],problems:[]}};
const cv={connection_version_id:id(3),connection_id:id(2),version:1,base_url:'https://example.test/v1',auth_type:'none'};
const connection={connection_id:id(2),display_name:'상품 서비스',current_version_id:id(3),versions:[cv]};
// This is an API double for UI flow assertions, not the production OpenAPI compiler.
function compileFixture(spec){
 const [path,item]=Object.entries(spec.paths)[0], [method,o]=Object.entries(item)[0];
 const props={},required=[];
 for(const where of ['path','query','header']){const params=(o.parameters||[]).filter(p=>p.in===where);if(params.length){const req=params.filter(p=>p.required).map(p=>p.name);props[where]={type:'object',properties:Object.fromEntries(params.map(p=>[p.name,p.schema])),required:req,additionalProperties:false};if(req.length)required.push(where);}}
 if(o.requestBody){props.body=o.requestBody.content['application/json'].schema;if(o.requestBody.required)required.push('body');}
 const schemes=spec.components?.securitySchemes||{};const auth=o.security.length?Object.entries(schemes).map(([name,a])=>({name,type:a.type==='http'?'bearer':'api_key',in:a.in,key:a.name})):[{name:'none',type:'none'}];
 const unsupported=JSON.stringify(o).includes('oneOf');
 return {import_id:id(100),title:spec.info.title,spec_version:spec.openapi,analysis:{title:spec.info.title,spec_version:spec.openapi,problems:[],operations:[{key:method.toUpperCase()+' '+path,method:method.toUpperCase(),path,operation_id:o.operationId,summary:o.summary,supported:!unsupported,problems:unsupported?[{pointer:'/schema/oneOf',reason:'unsupported'}]:[],parameters:o.parameters,auth_options:auth,servers:spec.servers.map(s=>s.url),responses:Object.fromEntries(Object.entries(o.responses).map(([k,v])=>[k,k==='204'?null:v.content['application/json'].schema])),input_schema:{type:'object',properties:props,required,additionalProperties:false}}]}};
}
const flush=()=>new Promise(setImmediate);
function boot(){const document=makeDOM(),requests=[],versions=[],calls=new Map(),tools=[],connections=[structuredClone(connection)],imports=[structuredClone(imp)];let loss=null,hold=null,nextCall=20;const replay=new Map();let confirmations=true;const counters={tool:0,version:0,connection:0,call:0};
 async function fetch(url,options){const method=options.method||'GET';const p=url.split('?')[0];requests.push({path:p,method,body:options.body,headers:options.headers});if(hold&&hold.path===p&&method==='POST')await hold.promise;
 if(loss?.path===p&&loss.before){loss=null;throw new TypeError('lost before response');}
 const replayKey=options.headers['Idempotency-Key'];let data;const body=options.body?JSON.parse(options.body):{};
 if(method==='POST'&&replay.has(replayKey))data=replay.get(replayKey);else if(p==='/api/status')data={stage:'A07',schema_version:5,version:'a07.1'};
 else if(p==='/api/tools'&&method==='GET')data={items:tools};
 else if(p==='/api/connections'&&method==='GET')data={items:connections};
 else if(p==='/api/openapi-imports'&&method==='GET')data={items:imports};
 else if(p==='/api/openapi-imports'&&method==='POST'){if(body.openapi){data=compileFixture(body);imports.push(data);}else data=imp;}
 else if(p.startsWith('/api/openapi-imports/')&&method==='GET')data=imports.find(x=>p.endsWith(x.import_id));
 else if(p.startsWith('/api/connections/')&&method==='GET')data=connections.find(x=>p.endsWith(x.connection_id));
 else if(p.startsWith('/api/connection-versions/')&&method==='GET')data=connections.flatMap(x=>x.versions).find(x=>p.endsWith(x.connection_version_id));
 else if(p==='/api/connections'&&method==='POST'){counters.connection++;const vid=id(201+counters.connection);data={connection_id:id(101+counters.connection),display_name:body.display_name,current_version_id:vid,versions:[{...body,connection_id:id(101+counters.connection),connection_version_id:vid,version:1}]};connections.push(data);}
 else if(p==='/api/knowledge/inspect'&&method==='POST'){
 if(body.action==='indexes')data={items:[{index_id:id(30),name:'test1',state:'active',selectable:true},{index_id:id(31),name:'준비 중',state:'active',selectable:false}],next_cursor:''};
 else data={index_name:'test1',chunk_count:42,binding:{connection_version_id:body.connection_version_id,knowledge:{model_connection_version_id:body.model_connection_version_id,index_id:body.index_id,index_version_id:id(32),embedding_deployment_id:id(33),embedding_space_id:'es_'+'a'.repeat(64),top_k:5,candidate_k:5}}};
 }
 else if(p==='/api/tools'&&method==='POST'){counters.tool++;data={tool_id:id(4),display_name:body.display_name,description:body.description,enabled:true,revision:1};tools.push(data);}
 else if(p==='/api/tools/'+id(4)+'/versions'&&method==='POST'&&body.kind==='knowledge_search_v1'){counters.version++;data={tool_id:id(4),tool_version_id:id(5+versions.length),version:versions.length+1,revision:1,state:'draft',kind:body.kind,binding:body.binding,timeout_ms:body.timeout_ms,input_schema:{type:'object',additionalProperties:false,required:['query'],properties:{query:{type:'string',title:'질문',minLength:1,maxLength:8192},top_k:{type:'integer',title:'결과 개수'},candidate_k:{type:'integer',title:'검색 후보 개수'},filters:{type:'object',properties:{kinds:{type:'array',items:{type:'string'}}},additionalProperties:false}}}};versions.push(data);}
 else if(p==='/api/tools/'+id(4)+'/versions'&&method==='POST'){counters.version++;data={tool_id:id(4),tool_version_id:id(5+versions.length),version:versions.length+1,revision:1,state:'draft',kind:'openapi_http_v1',input_schema:imports.find(x=>x.import_id===body.binding.import_id).analysis.operations.find(x=>x.key===body.binding.operation_key).input_schema,binding:{...body.binding,http:imports.find(x=>x.import_id===body.binding.import_id).analysis.operations.find(x=>x.key===body.binding.operation_key)},timeout_ms:body.timeout_ms};versions.push(data);}
 else if(p==='/api/tools/'+id(4)&&method==='GET')data={...tools[0],versions};
 else if(p==='/api/tools/'+id(4)&&method==='PATCH'){Object.assign(tools[0],body);data=tools[0];}
 else if(p.startsWith('/api/tool-versions/')&&method==='GET')data=versions.find(v=>p.endsWith(v.tool_version_id));
 else if(p.startsWith('/api/tool-versions/')&&method==='POST'){const v=versions.find(v=>p.includes(v.tool_version_id));if(p.endsWith('/publish')&&![...calls.values()].some(c=>c.tool_version_id===v.tool_version_id&&c.mode==='test'&&c.state==='succeeded'))return {ok:false,status:409,json:async()=>({error:{code:'TOOL_TEST_REQUIRED'}})};v.state=p.endsWith('/publish')?'published':'retired';v.revision++;data=v;}
 else if(p==='/api/tool-calls'&&method==='POST'){counters.call++;data={tool_call_id:id(nextCall++),tool_version_id:body.tool_version_id,mode:body.mode,state:'succeeded',result:{data:{id:body.arguments.path?.item_id,name:'예시 상품'}}};if(versions.find(v=>v.tool_version_id===body.tool_version_id)?.kind==='knowledge_search_v1')data.result={metadata:{adapter:'knowledge_search_v1'},data:{returned_count:1,results:[{text:'<script>안전한 검색 결과</script>',kind:'text',source_spans:[{metadata:{location:{page:3}}}],quality:{warning:'확인 필요'}}]}};calls.set(data.tool_call_id,data);}
 else if(p.startsWith('/api/tool-calls/')&&method==='GET'){data=calls.get(p.split('/').at(-1));if(!data)return {ok:false,status:404,json:async()=>({error:{code:'NOT_FOUND'}})};}
 else throw Error('unexpected fixture request '+method+' '+p);
 if(method==='POST')replay.set(replayKey,structuredClone(data));if(loss?.path===p){loss=null;throw new TypeError('response lost');}
 return {ok:true,status:200,json:async()=>structuredClone(data)};
 }
 const ctx=vm.createContext({document,fetch,crypto:webcrypto,TextDecoder,TextEncoder,AbortController,URL,location:{origin:'http://localhost:16100'},window:{addEventListener(){}},confirm:()=>confirmations,setTimeout,clearTimeout,console,Uint8Array,TypeError});vm.runInContext(js,ctx);
 const run=s=>vm.runInContext(s,ctx),get=id=>document.getElementById(id);
 async function settle(){for(let i=0;i<100;i++){await flush();if(!run('state.busy'))return;}throw Error('busy loop');}
 async function event(id,type='click'){const n=get(id);if(type==='click'&&n.disabled)return;n['on'+type]?.({preventDefault(){}});await settle();}
 async function login(){get('token').value='test-token';await event('login-form','submit');assert.equal(run('state.connected'),true);}
 return {get,run,event,settle,login,requests,counters,calls,tools,versions,document,lose:(p,before=false)=>loss={path:p,before},pause:p=>{let release;hold={path:p,promise:new Promise(r=>release=r)};return ()=>{hold=null;release();};},confirm:value=>confirmations=value};
}
async function draft(a){await a.login();await a.event('new-tool');await a.event('registration-openapi');a.get('imports').value=id(1);await a.event('imports','change');a.get('operations').children[0].click();assert.equal(a.get('next-connection').disabled,false);await a.event('next-connection');a.get('connections').value=id(2);await a.event('connections','change');}
module.exports={boot,id,input,op,imp,cv,connection,draft};
if(require.main===module)(async()=>{
 let checks=0;
 const check=()=>checks++;
 let a=boot();assert.equal(a.requests.length,0);assert.equal(a.get('view-tools').hidden,false);check();
 await draft(a);assert.equal(a.get('step-1').hidden,true);assert.equal(a.get('step-2').hidden,false);assert.equal(a.get('advanced-settings').open,false);assert.equal(a.requests.filter(r=>r.method!=='GET').length,0);assert.match(a.get('unsupported-operations').textContent,/multipart/);check();
 a.get('tool-name').value='';await a.event('create-draft');assert.equal(a.counters.tool,0);a.get('tool-name').value='상품 정보 조회';check();
 a.get('timeout').value='121';await a.event('create-draft');assert.equal(a.counters.tool,0);a.get('timeout').value='30';check();
 a.get('connections').value='';await a.event('connections','change');assert.equal(a.get('create-draft').disabled,true);a.get('connections').value=id(2);await a.event('connections','change');check();
 await a.event('create-draft');assert.equal(a.counters.tool,1);assert.equal(a.counters.version,1);assert.equal(a.run('state.step'),3);assert.equal(a.get('view-detail').hidden,false);assert.equal(a.get('json-input').hidden,true);check();
 await a.event('publish');assert.match(a.get('notice').textContent,/시험 실행/);assert.equal(a.run('state.version.state'),'draft');check();
 await a.event('test-call');assert.equal(a.counters.call,0);check();
 a.get('input-fields');a.run("state.inputTree.children[0][1].children[0][1].input.value='A-100'");a.confirm(false);await a.event('test-call');assert.equal(a.counters.call,0);a.confirm(true);check();
 await a.event('test-call');assert.equal(a.counters.call,1);assert.equal(a.get('call-state').textContent,'성공');assert.match(a.get('result-data').textContent,/예시 상품/);const sent=JSON.parse(a.requests.find(r=>r.path==='/api/tool-calls'&&r.method==='POST').body);assert.deepEqual(sent.arguments,{path:{item_id:'A-100'}});check();
 await a.event('publish');assert.equal(a.run('state.version.state'),'published');assert.equal(a.get('mcp-connection').hidden,false);assert.equal(a.run("state.inputTree.children[0][1].children[0][1].input.value"),'A-100');check();
 await a.event('run-call');assert.equal(a.counters.call,2);await a.event('nav-results');assert.equal(a.get('recent-calls').children.length,2);assert.equal(a.get('result-panel').parentNode,a.get('history-result'));check();
 a.get('call-id').value=id(900);await a.event('lookup-form','submit');assert.equal(a.run('state.pendingCall'),null);check();
 await a.event('nav-tools');a.get('tools').children[0].children[2].children[0].click();await a.settle();assert.equal(a.run('state.view'),'detail');check();
 const count=a.requests.filter(r=>r.method!=='GET').length;await a.event('new-version');assert.equal(a.run('state.step'),2);assert.equal(a.get('effect').value,'write');assert.equal(a.requests.filter(r=>r.method!=='GET').length,count);check();
 // Accepted call followed by GET outage: blocked rerun; lookup reuses the old ID.
 a=boot();await draft(a);await a.event('create-draft');a.run("state.inputTree.children[0][1].children[0][1].input.value='A-100'");a.lose('/api/tool-calls/'+id(20),true);await a.event('test-call');assert.equal(a.counters.call,1);assert.equal(a.get('test-call').disabled,true);await a.event('nav-results');assert.equal(a.get('recent-calls').children.length,1);await a.event('refresh-call');assert.equal(a.counters.call,1);assert.equal(a.run('state.pendingCall'),null);check();
 // POST response loss reuses exactly the same body and key; a new payload is blocked.
 a=boot();await draft(a);await a.event('create-draft');a.run("state.inputTree.children[0][1].children[0][1].input.value='first'");a.lose('/api/tool-calls');await a.event('test-call');a.run("state.inputTree.children[0][1].children[0][1].input.value='changed'");await a.event('test-call');assert.equal(a.counters.call,1);assert.match(a.get('notice').textContent,/원래대로/);a.run("state.inputTree.children[0][1].children[0][1].input.value='first'");await a.event('test-call');const tries=a.requests.filter(r=>r.path==='/api/tool-calls'&&r.method==='POST');assert.equal(tries.length,2);assert.deepEqual(tries[0],tries[1]);assert.equal(a.counters.call,1);check();
 // Draft phase 2 can recover after an uncertain response or after a successful POST + failed refresh.
 for(const phase of ['post','refresh']){a=boot();await draft(a);a.lose(phase==='post'?'/api/tools/'+id(4)+'/versions':'/api/openapi-imports',phase==='refresh');await a.event('create-draft');assert.equal(a.counters.version,1);await a.event('create-draft');assert.equal(a.counters.version,1);assert.equal(a.counters.tool,1);assert.equal(a.run('state.step'),3);check();}
 // Double click does not schedule two writes; UI actions stay disabled while awaiting response.
 a=boot();await draft(a);const release=a.pause('/api/tools');a.get('create-draft').click();await flush();assert.equal(a.get('new-tool').disabled,true);a.get('create-draft').click();release();await a.settle();assert.equal(a.counters.tool,1);assert.equal(a.counters.version,1);check();
 // Typed values keep false, enum and large integer text. Optional groups remain omitted by default.
 a.run("state.inputTree.children[1][1].included.checked=true; const q=state.inputTree.children[1][1];q.children.forEach(([,n])=>n.included.checked=true);q.children[0][1].input.value='0';q.children[1][1].input.value='9007199254740993';q.children[2][1].input.value='1';state.inputTree.children[0][1].children[0][1].input.value='A'");assert.match(a.run('formJSON()'),/9007199254740993/);assert.match(a.run('formJSON()'),/"enabled":false/);check();
 await a.event('use-json');a.get('arguments').value='{"path":{"item_id":"A"},"body":{"amount":9007199254740993}}';await a.event('test-call');assert.match(a.requests.filter(r=>r.path==='/api/tool-calls'&&r.method==='POST').at(-1).body,/9007199254740993/);check();
 a.run("renderInputs({type:'object',properties:{body:{type:['string','null']}},additionalProperties:false})");assert.equal(a.get('json-input').hidden,false);check();
 a=boot();await draft(a);a.run("state.connectionVersions[0].auth_type='bearer'");await a.event('create-draft');assert.equal(a.counters.tool,0);assert.match(a.get('notice').textContent,/접속 방식/);check();
 // API key match includes location and name, not just the type.
 assert.equal(a.run("matchesAuth({type:'api_key',in:'header',key:'X-Key'},{auth_type:'api_key',api_key_in:'query',api_key_name:'X-Key'})"),false);check();
 await a.event('logout');assert.equal(a.run('state.token'),'');assert.equal(a.run('state.calls.size'),0);assert.equal(a.get('view-tools').hidden,false);assert.equal(a.get('result-panel').hidden,true);check();
 // Untrusted names and errors are text nodes, never interpolated into HTML.
 assert(!/innerHTML|insertAdjacentHTML|localStorage|sessionStorage/.test(js));assert(!/A04 ·|연결과 매핑/.test(html));check();
 console.log(JSON.stringify({result:'passed',checks,scope:'shipped HTML/JS + DOM/API doubles; no browser/native services'},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
