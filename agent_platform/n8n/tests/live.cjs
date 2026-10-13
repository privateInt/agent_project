'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {validate,parseOutput}=require('../live/run.cjs');
function sample(){
 const source={chunk_id:'chunk-1',text:'자료 원문',source_spans:[{metadata:{location:{page:3}}}],quality:{requires_review:false}};
 const knowledge={index_id:'index',index_version_id:'index-v',embedding_space_id:'es_fixed',embedding_deployment_id:'embed-d'};
 const meta={adapter:'knowledge_search_v1',embedding_calls:1,search_calls:1,index_version_id:'index-v',embedding_profile:{deployment_id:'embed-d',embedding_space_id:'es_fixed'}};
 const s={mode:'test',question:'연구 범위?',agent_version:{agent_version_id:'agent-v',definition_sha256:'agent-hash',model:{deployment_id:'chat-d',model_version_id:'chat-v'}},tool_version:{tool_version_id:'tool-v',definition_sha256:'tool-hash',binding:{knowledge}}};
 const request={request:{invocation_key:'key',body:{caller_context:{execution_id:'first'}}}};
 const result={answer:'자료에 따른 답변 [S1]',outcome:'answered',citations:[{citation_id:'S1',source}],answer_truncated:false,warnings:[],tool_call_id:'call',identity:{tool_version_id:'tool-v',knowledge:meta,model:{deployment_id:'chat-d',model_version_id:'chat-v'}},usage:{prompt_tokens:20,completion_tokens:10,total_tokens:30}};
 const run={agent_run_id:'run',agent_version_id:'agent-v',state:'succeeded',mode:'test',input:{question:s.question},caller_context:{execution_id:'first'},invocation_key:'key',definition_sha256:'agent-hash',tool_call_id:'call',request_id:'request',result};
 const child={tool_call_id:'call',tool_version_id:'tool-v',state:'succeeded',definition_sha256:'tool-hash',parent_agent_run_id:'run',parent_step_id:'search',request_id:'request',result:{completion:'completed',metadata:meta,data:{...knowledge,results:[source],returned_count:1}}};
 const steps=['validate','search','context','generate','finalize'].map(step_id=>({step_id,state:'succeeded'}));
 const output={...run,...result,original_caller_context:run.caller_context};
 return {s,request,run,steps,child,output};
}
const check=x=>validate(x.s,x.request,x.run,x.steps,x.child,x.output);
test('answer, source spans, pinned model and ToolCall correlation pass',()=>assert.equal(check(sample()).result,'passed'));
test('modified citation source is rejected',()=>{const x=sample();x.run.result.citations[0].source=structuredClone(x.run.result.citations[0].source);x.run.result.citations[0].source.source_spans[0].metadata.location.page=8;assert.throws(()=>check(x),/CITATION_SOURCE_CHANGED/);});
test('changed embedding deployment or Agent definition fails',()=>{for(const change of [x=>x.child.result.metadata.embedding_profile.deployment_id='another',x=>x.run.definition_sha256='another',x=>x.child.request_id='another']){const x=sample();change(x);assert.throws(()=>check(x));}});
test('no evidence is incomplete, not answer acceptance',()=>{const x=sample();x.run.result.outcome=x.output.outcome='no_evidence';x.run.result.citations=x.output.citations=[];x.run.result.answer=x.output.answer='근거 부족';x.child.result.data.results=[];x.child.result.data.returned_count=0;x.steps[3].state='skipped';assert.equal(check(x).result,'incomplete');assert.equal(check(x).generation_called,false);});
test('missing generation cannot certify answered result',()=>{const x=sample();x.steps[3].state='skipped';assert.throws(()=>check(x),/GENERATION_SKIPPED_UNEXPECTEDLY/);});
test('n8n cannot rewrite source or answer from Agent',()=>{const x=sample();x.output.answer='replaced';assert.throws(()=>check(x),/N8N_ANSWER_CHANGED/);});
test('CLI result parser accepts startup logs and rejects missing result',()=>{assert.deepEqual(parseOutput('startup {x}\n'+JSON.stringify({finished:true,data:{resultData:{}}},null,2)+'\n'),{finished:true,data:{resultData:{}}});assert.throws(()=>parseOutput('startup only'));});
test('workflow defaults to run, supports test, preserves complete replay body',()=>{
 const w=JSON.parse(fs.readFileSync(path.join(__dirname,'../workflows/agent-question.json'),'utf8'));
 const code=w.nodes.find(n=>n.name==='Prepare request').parameters.jsCode;
 const settings={base_url:'http://agent-gateway:8000',agent_version_id:'11111111-1111-4111-8111-111111111111',question:'질문'};
 function prepare(x,execution){return vm.runInNewContext('(function(){'+code+'})()',{require,Buffer,$input:{first:()=>({json:x})},$workflow:{id:'workflow'},$execution:{id:execution}})[0].json;}
 assert.equal(prepare(settings,'1').request.body.mode,'run');
 const first=prepare({...settings,mode:'test'},'2');assert.equal(first.request.body.mode,'test');
 const replay=prepare({...settings,request:first.request},'3');assert.equal(JSON.stringify(replay.request),JSON.stringify(first.request));assert.equal(replay.trace.execution_id,'3');assert.equal(replay.request.body.caller_context.execution_id,'2');
 assert.throws(()=>prepare({...settings,mode:'invalid'},'4'));
});
