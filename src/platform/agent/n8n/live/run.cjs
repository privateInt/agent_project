'use strict';
// Real n8n -> configured Agent REST. No HTTP/model fixtures in this runner.
// The private session is separate from the operational n8n database and keys.
const fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const crypto=require('node:crypto'),assert=require('node:assert/strict');
const SESSION=path.resolve(process.env.AGENT_LIVE_SESSION||'/session');
const TEMPLATE=path.resolve(__dirname,'../workflows/agent-question.json');
const STEPS=['validate','search','context','generate','finalize'];
function check(ok,code){if(!ok)throw Error(code);}
function file(name){return path.join(SESSION,name);}
function read(name){const p=file(name);check(!fs.lstatSync(p).isSymbolicLink(),'SESSION_SYMLINK');check(fs.statSync(p).size<=16*1024*1024,'SESSION_FILE_TOO_LARGE');return JSON.parse(fs.readFileSync(p,'utf8'));}
function save(name,data){const target=file(name);if(fs.existsSync(target))check(!fs.lstatSync(target).isSymbolicLink(),'SESSION_SYMLINK');const tmp=target+'.tmp-'+crypto.randomUUID();const fd=fs.openSync(tmp,'wx',0o600);try{fs.writeFileSync(fd,JSON.stringify(data,null,2)+'\n');fs.fsyncSync(fd);}finally{fs.closeSync(fd);}fs.renameSync(tmp,target);const dir=fs.openSync(SESSION,'r');try{fs.fsyncSync(dir);}finally{fs.closeSync(dir);}}
function parseOutput(out){const end=out.lastIndexOf('\n}')+2;check(end>1,'N8N_RESULT_MISSING');let count=0;for(let i=out.indexOf('{');i>=0&&count++<1000;i=out.indexOf('{',i+1)){try{const v=JSON.parse(out.slice(i,end));if(v.data?.resultData)return v;}catch{}}throw Error('N8N_RESULT_MISSING');}
function checkpoint(code,name,value){return `const items=(function(){${code}})();const v=items[0].json;const fs=require('fs');const target=${JSON.stringify(file(name))};const data=${value};
if(fs.existsSync(target)){if(fs.lstatSync(target).isSymbolicLink()||JSON.stringify(JSON.parse(fs.readFileSync(target,'utf8')))!==JSON.stringify(data))throw Error('LIVE_CHECKPOINT_CHANGED');}
else{const temp=target+'.tmp-'+require('crypto').randomUUID();const fd=fs.openSync(temp,'wx',0o600);try{fs.writeFileSync(fd,JSON.stringify(data));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}fs.renameSync(temp,target);const dir=fs.openSync(${JSON.stringify(SESSION)},'r');try{fs.fsyncSync(dir);}finally{fs.closeSync(dir);}}
return items;`;}
function same(a,b,code){try{assert.deepEqual(a,b);}catch{throw Error(code);}}
function validSettings(s){
 const u=new URL(s.agent_url);check(['http:','https:'].includes(u.protocol)&&!u.username&&!u.password&&!u.search&&!u.hash&&u.pathname==='/','AGENT_ORIGIN_INVALID');
 check(s.schema===1&&typeof s.session_id==='string'&&['test','run'].includes(s.mode),'SESSION_INVALID');
 check(typeof s.question==='string'&&s.question.trim()&&!s.question.includes('\0')&&Buffer.byteLength(s.question)<=8192,'QUESTION_INVALID');
 for(const id of [s.agent_version.agent_version_id,s.tool_version.tool_version_id])check(/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id),'VERSION_INVALID');
}
function validate(s,checkpointValue,run,steps,child,output){
 const av=s.agent_version,tv=s.tool_version,result=run.result;
 check(run.state==='succeeded'&&run.agent_version_id===av.agent_version_id&&run.mode===s.mode,'AGENT_RESULT_INVALID');
 same(run.input,{question:s.question},'QUESTION_CHANGED');same(run.caller_context,checkpointValue.request.body.caller_context,'CALLER_CONTEXT_CHANGED');
 check(run.invocation_key===checkpointValue.request.invocation_key&&run.definition_sha256===av.definition_sha256,'AGENT_IDENTITY_CHANGED');
 check(result&&['answered','no_evidence'].includes(result.outcome)&&typeof result.answer==='string'&&result.answer.trim()&&Array.isArray(result.citations),'ANSWER_INVALID');
 same(steps.map(x=>x.step_id),STEPS,'STEP_ORDER_CHANGED');check(steps.every(x=>['succeeded','skipped'].includes(x.state)),'STEP_FAILED');
 check(result.tool_call_id===run.tool_call_id&&child.tool_call_id===run.tool_call_id&&child.state==='succeeded','SEARCH_CALL_INVALID');
 check(child.definition_sha256===tv.definition_sha256,'SEARCH_DEFINITION_CHANGED');
 check(child.tool_version_id===tv.tool_version_id&&child.parent_agent_run_id===run.agent_run_id&&child.parent_step_id==='search'&&child.request_id===run.request_id,'SEARCH_CORRELATION_CHANGED');
 const search=child.result,meta=search?.metadata,data=search?.data,k=tv.binding.knowledge;
 check(search?.completion==='completed'&&meta?.adapter==='knowledge_search_v1'&&meta.embedding_calls===1&&meta.search_calls===1,'SEARCH_RECEIPT_INVALID');
 check(meta.embedding_profile?.deployment_id===k.embedding_deployment_id&&meta.embedding_profile?.embedding_space_id===k.embedding_space_id,'EMBEDDING_IDENTITY_CHANGED');
 check(data?.index_id===k.index_id&&data?.index_version_id===k.index_version_id&&meta.index_version_id===k.index_version_id&&data.embedding_space_id===k.embedding_space_id,'INDEX_IDENTITY_CHANGED');
 check(Array.isArray(data.results)&&data.returned_count===data.results.length,'SEARCH_COUNT_CHANGED');
 same(result.identity?.knowledge,meta,'KNOWLEDGE_RECEIPT_CHANGED');
 check(result.identity?.tool_version_id===tv.tool_version_id,'TOOL_IDENTITY_CHANGED');
 const generate=steps.find(x=>x.step_id==='generate');
 if(generate.state==='succeeded'){
  check(result.identity.model?.deployment_id===av.model.deployment_id&&result.identity.model?.model_version_id===av.model.model_version_id,'MODEL_IDENTITY_CHANGED');
  check(result.usage&&Number.isInteger(result.usage.total_tokens)&&result.usage.total_tokens===result.usage.prompt_tokens+result.usage.completion_tokens,'MODEL_USAGE_INVALID');
 }else check(result.outcome==='no_evidence'&&data.results.length===0,'GENERATION_SKIPPED_UNEXPECTEDLY');
 if(result.outcome==='answered'){
  check(generate.state==='succeeded'&&result.citations.length>0,'ANSWER_WITHOUT_GENERATION');
  for(const c of result.citations){check(/^S[1-9][0-9]*$/.test(c.citation_id)&&result.answer.includes('['+c.citation_id+']'),'CITATION_INVALID');const hit=data.results.find(x=>x.chunk_id===c.source?.chunk_id);check(hit,'CITATION_NOT_IN_SEARCH');same(c.source,hit,'CITATION_SOURCE_CHANGED');}
 }
 if(output){for(const field of ['agent_run_id','agent_version_id','request_id','tool_call_id','state'])same(output[field],run[field],'N8N_'+field.toUpperCase()+'_CHANGED');for(const field of ['answer','citations','outcome','answer_truncated','warnings'])same(output[field],result[field],'N8N_'+field.toUpperCase()+'_CHANGED');same(output.original_caller_context,run.caller_context,'N8N_CALLER_CHANGED');}
 return {result:result.outcome==='answered'?'passed':'incomplete',stage:'A07-live',mode:s.mode,scope:'n8n CLI -> configured Agent REST -> configured Model/Knowledge APIs',agent_run_id:run.agent_run_id,agent_version_id:run.agent_version_id,request_id:run.request_id,tool_call_id:run.tool_call_id,index_version_id:data.index_version_id,embedding_space_id:data.embedding_space_id,model_deployment_id:av.model.deployment_id,outcome:result.outcome,citation_count:result.citations.length,returned_count:data.returned_count,generation_called:generate.state==='succeeded',answer_truncated:result.answer_truncated,warnings:result.warnings,semantic_quality:'not_assessed'};
}
async function main(){
 check(fs.statSync(SESSION).isDirectory()&&!fs.lstatSync(SESSION).isSymbolicLink(),'SESSION_INVALID');
 let stdin='';for await(const chunk of process.stdin){stdin+=chunk;check(Buffer.byteLength(stdin)<65536,'CREDENTIAL_INPUT_TOO_LARGE');}
 const payload=JSON.parse(stdin);check(typeof payload.token==='string'&&payload.token.length>=16&&!/[\r\n]/.test(payload.token),'TOKEN_INVALID');
 const s=read('settings.json');validSettings(s);const base=s.agent_url.replace(/\/$/,'');
 async function api(route){let response;try{response=await fetch(base+route,{headers:{Authorization:'Bearer '+payload.token},redirect:'error',signal:AbortSignal.timeout(40000)});}catch{throw Error('AGENT_CONNECTION_FAILED');}check(response.status===200,'AGENT_HTTP_'+response.status);const chunks=[];let size=0;for await(const part of response.body){size+=part.length;check(size<=16*1024*1024,'AGENT_RESPONSE_TOO_LARGE');chunks.push(part);}return JSON.parse(Buffer.concat(chunks).toString());}
 async function receipt(output){const ptr=read('accepted.json'),req=read('request.json');const run=await api('/api/agent-runs/'+ptr.agent_run_id);if(['accepted','running'].includes(run.state))return {summary:{result:'pending',stage:'A07-live',agent_run_id:run.agent_run_id,state:run.state},run};if(run.state!=='succeeded')return {summary:{result:'failed',stage:'A07-live',agent_run_id:run.agent_run_id,request_id:run.request_id,state:run.state,error:run.error},run};const steps=(await api('/api/agent-runs/'+ptr.agent_run_id+'/steps')).items;const child=await api('/api/tool-calls/'+run.tool_call_id);return {summary:validate(s,req,run,steps,child,output),run,steps,tool_call:child};}
 if(payload.action==='lookup'){check(fs.existsSync(file('accepted.json')),'NO_ACCEPTED_RUN_USE_RUN_TO_RESUME');const r=await receipt();r.summary.mode='lookup';save('lookup.json',r);console.log(JSON.stringify(r.summary,null,2));if(r.summary.result!=='passed')process.exitCode=2;return;}
 check(payload.action==='run','ACTION_INVALID');
 const status=await api('/api/status');check(status.ready&&status.schema_version===5&&status.stage==='A07','AGENT_NOT_READY');
 const av=await api('/api/agent-versions/'+s.agent_version.agent_version_id);check(av.definition_sha256===s.agent_version.definition_sha256,'PINNED_AGENT_CHANGED');
 const n8nRoot=file('n8n');if(!fs.existsSync(n8nRoot))fs.mkdirSync(n8nRoot,{mode:0o700});check(!fs.lstatSync(n8nRoot).isSymbolicLink(),'SESSION_SYMLINK');
 const keyPath=file('encryption-key');if(!fs.existsSync(keyPath))fs.writeFileSync(keyPath,crypto.randomBytes(32).toString('hex'),{flag:'wx',mode:0o600});check(!fs.lstatSync(keyPath).isSymbolicLink(),'SESSION_SYMLINK');
 const env={...process.env};for(const k of Object.keys(env))if(k.startsWith('DB_')||k.startsWith('N8N_')||k.startsWith('NODE_FUNCTION_ALLOW_'))delete env[k];
 Object.assign(env,{N8N_USER_FOLDER:n8nRoot,DB_TYPE:'sqlite',N8N_ENCRYPTION_KEY:fs.readFileSync(keyPath,'utf8'),N8N_DIAGNOSTICS_ENABLED:'false',N8N_VERSION_NOTIFICATIONS_ENABLED:'false',N8N_COMMUNITY_PACKAGES_ENABLED:'false',N8N_BLOCK_ENV_ACCESS_IN_NODE:'true',NODE_FUNCTION_ALLOW_BUILTIN:'crypto,fs',N8N_SECURE_COOKIE:'false',EXECUTIONS_DATA_PRUNE:'false'});
 env.N8N_RUNNERS_BROKER_PORT=String(await new Promise((resolve,reject)=>{const server=require('net').createServer();server.on('error',reject);server.listen(0,'127.0.0.1',()=>{const port=server.address().port;server.close(()=>resolve(port));});}));
 const command=process.env.AGENT_LIVE_N8N_BIN||'n8n';
 async function cli(args){return new Promise((resolve,reject)=>{const p=cp.spawn(command,args,{env,cwd:SESSION});let out='',err='',limited=false;const timer=setTimeout(()=>p.kill('SIGKILL'),args[0]==='execute'?780000:150000);p.on('error',()=>{clearTimeout(timer);reject(Error('N8N_START_FAILED'));});p.stdout.on('data',x=>{out+=x;if(Buffer.byteLength(out)>16*1024*1024){limited=true;p.kill('SIGKILL');}});p.stderr.on('data',x=>{err+=x;if(Buffer.byteLength(err)>2*1024*1024){limited=true;p.kill('SIGKILL');}});p.on('close',code=>{clearTimeout(timer);save('last-cli-log.json',{command:args[0],stdout:out.slice(0,16*1024*1024),stderr:err.slice(0,2*1024*1024)});resolve({code,out,limited});});});}
 save('credentials.json',[{id:'agentLiveCredential',name:'Agent live check',type:'httpHeaderAuth',data:{name:'Authorization',value:'Bearer '+payload.token}}]);
 let imported;try{imported=await cli(['import:credentials','--input='+file('credentials.json')]);}finally{fs.unlinkSync(file('credentials.json'));}check(imported.code===0,'N8N_CREDENTIAL_IMPORT_FAILED');
 async function execute(){
  const saved=fs.existsSync(file('request.json'))?read('request.json'):null;
  if(saved){check(saved.base_url===base,'SAVED_ORIGIN_CHANGED');same(saved.request.body.input,{question:s.question},'SAVED_QUESTION_CHANGED');check(saved.request.body.agent_version_id===av.agent_version_id&&saved.request.body.mode===s.mode,'SAVED_VERSION_MODE_CHANGED');}
  const w=JSON.parse(fs.readFileSync(TEMPLATE,'utf8'));w.id='live'+s.session_id.replaceAll('-','');w.name='Agent live check · '+s.session_id;
  const settings={base_url:base,agent_version_id:av.agent_version_id,question:s.question,mode:s.mode,...(saved?{request:saved.request}:{})};
  w.nodes.find(n=>n.name==='Question settings').parameters.jsCode='return [{json:JSON.parse('+JSON.stringify(JSON.stringify(settings))+')}];';
  const prep=w.nodes.find(n=>n.name==='Prepare request');prep.parameters.jsCode=checkpoint(prep.parameters.jsCode,'request.json','{base_url:v.base_url,request:v.request}');
  const pointer=w.nodes.find(n=>n.name==='Run pointer');pointer.parameters.jsCode=checkpoint(pointer.parameters.jsCode,'accepted.json','{agent_run_id:v.agent_run_id,agent_version_id:v.request.body.agent_version_id,invocation_key:v.request.invocation_key}');
  for(const n of w.nodes.filter(n=>n.type==='n8n-nodes-base.httpRequest'))n.credentials={httpHeaderAuth:{id:'agentLiveCredential',name:'Agent live check'}};
  save('workflow.json',w);const i=await cli(['import:workflow','--input='+file('workflow.json')]);check(i.code===0,'N8N_WORKFLOW_IMPORT_FAILED');
  const r=await cli(['execute','--id='+w.id,'--rawOutput']);check(r.code===0&&!r.limited,'N8N_EXECUTION_FAILED');
  const run=parseOutput(r.out);check(run.finished===true,'N8N_EXECUTION_NOT_FINISHED');const data=run.data.resultData.runData;
  return {output:data['Answer and sources'][0].data.main[0][0].json,accept:data['Accept Agent run'][0].data.main[0][0].json};
 }
 const first=await execute(),before=await receipt(first.output);check(before.summary.result!=='failed'&&before.summary.result!=='pending','AGENT_NOT_COMPLETED');save('first-result.json',before);
 const second=await execute(),after=await receipt(second.output);same(first.output.agent_run_id,second.output.agent_run_id,'REPLAY_RUN_CHANGED');same(first.output.request_id,second.output.request_id,'REPLAY_REQUEST_CHANGED');same(first.output.tool_call_id,second.output.tool_call_id,'REPLAY_SEARCH_CHANGED');same(first.output.original_caller_context,second.output.original_caller_context,'REPLAY_CALLER_CHANGED');
 check(first.output.n8n_execution.execution_id!==second.output.n8n_execution.execution_id,'REPLAY_N8N_EXECUTION_NOT_NEW');
 check(second.accept.headers?.['idempotency-replayed']==='true','REPLAY_HEADER_MISSING');same(before.run,after.run,'STORED_RESULT_CHANGED');same(before.tool_call,after.tool_call,'STORED_SEARCH_CHANGED');same(before.steps,after.steps,'STORED_STEPS_CHANGED');
 after.summary.replay_verified=true;after.summary.n8n_execution_ids=[first.output.n8n_execution.execution_id,second.output.n8n_execution.execution_id];
 save('result.json',after);console.log(JSON.stringify(after.summary,null,2));if(after.summary.result==='incomplete')process.exitCode=2;
}
if(require.main===module)main().catch(async e=>{let report={result:'failed',stage:'A07-live',code:/^[A-Z0-9_]+$/.test(e.message)?e.message:'LIVE_CHECK_FAILED',resume:'reuse the same session; do not remove request.json'};try{if(fs.existsSync(file('accepted.json')))report.agent_run_id=read('accepted.json').agent_run_id;save('failure.json',report);}catch{}console.error(JSON.stringify(report,null,2));process.exitCode=1;});
module.exports={validate,parseOutput,validSettings};
