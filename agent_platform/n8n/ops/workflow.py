#!/usr/bin/env python3
"""Generate a portable, inactive n8n Agent workflow with no embedded credentials."""
import argparse, json, re, uuid
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]
PREPARE=r'''const x=$input.first().json;
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
if (typeof x.base_url!=='string'||!/^https?:\/\/[^\s/?#@]+(?::\d+)?$/.test(x.base_url)) throw Error('Configure the Agent Gateway origin');
let request=x.request;
if (!request) {
  const mode=x.mode||'run';if(!['run','test'].includes(mode))throw Error('Choose run or test mode');
  if(!uuid.test(x.agent_version_id)||x.agent_version_id==='00000000-0000-0000-0000-000000000000')throw Error('Choose a published Agent version');
  if(typeof x.question!=='string'||!x.question.trim()||x.question.includes('\0')||Buffer.byteLength(x.question,'utf8')>8192)throw Error('Question must be nonblank and at most 8 KiB');
  // Stable inside this execution; HTTP retries consume this saved output.
  const hex=require('crypto').createHash('sha256').update(String($workflow.id)+'/'+String($execution.id)+'/agent-call').digest('hex');
  const key=hex.slice(0,8)+'-'+hex.slice(8,12)+'-4'+hex.slice(13,16)+'-a'+hex.slice(17,20)+'-'+hex.slice(20,32);
  request={invocation_key:key,body:{agent_version_id:x.agent_version_id,mode,input:{question:x.question},caller_context:{system:'n8n',workflow_id:String($workflow.id),execution_id:String($execution.id),node_id:'agent-call'}}};
}
if(!uuid.test(request.invocation_key)||!uuid.test(request.body?.agent_version_id)||!['run','test'].includes(request.body.mode)||typeof request.body.input?.question!=='string'||!request.body.input.question.trim()||request.body.input.question.includes('\0')||Buffer.byteLength(request.body.input.question,'utf8')>8192||!request.body.caller_context?.execution_id)throw Error('Invalid replay envelope: preserve the original key, body and caller_context');
return [{json:{base_url:x.base_url,request,started_ms:Date.now(),trace:{workflow_id:String($workflow.id),execution_id:String($execution.id)}}}];'''
POINTER=r'''const p=$('Prepare request').first().json,r=$input.first().json;
const b=r.body;
if(r.statusCode!==202||!b||b.state!=='accepted'||!/^[-0-9a-f]{36}$/.test(b.agent_run_id)||b.agent_version_id!==p.request.body.agent_version_id||b.status_url!=='/api/agent-runs/'+b.agent_run_id)throw Error('Invalid Agent acceptance; preserve Prepare request output before retry');
return [{json:{...p,agent_run_id:b.agent_run_id,status_url:b.status_url}}];'''
EVALUATE=r'''const p=$('Run pointer').first().json,r=$input.first().json,b=r.body;
if(r.statusCode!==200||!b||b.agent_run_id!==p.agent_run_id||b.agent_version_id!==p.request.body.agent_version_id||!['accepted','running','succeeded','failed','interrupted'].includes(b.state))throw Error('Invalid Agent status; use the saved run ID to look up the result');
let keep_waiting=['accepted','running'].includes(b.state);
const timed_out=keep_waiting&&(Date.now()-p.started_ms>=650000||$runIndex>=300);
if(timed_out)keep_waiting=false;
if(b.state==='succeeded'&&(!b.result||!['answered','no_evidence'].includes(b.result.outcome)||typeof b.result.answer!=='string'||!Array.isArray(b.result.citations)||(b.result.outcome==='answered'&&!b.result.citations.length)))throw Error('Invalid final Agent result');
return [{json:{...p,keep_waiting,succeeded:b.state==='succeeded',state:timed_out?'poll_timeout':b.state,request_id:b.request_id||'',tool_call_id:b.tool_call_id||'',result:b.result||null,error:timed_out?{code:'POLL_TIMEOUT',stage:'poll'}:b.error||null,poll_count:$runIndex+1}}];'''
SUCCESS=r'''const x=$input.first().json;return [{json:{agent_run_id:x.agent_run_id,agent_version_id:x.request.body.agent_version_id,request_id:x.request_id,tool_call_id:x.tool_call_id,state:x.state,answer:x.result.answer,citations:x.result.citations,outcome:x.result.outcome,answer_truncated:x.result.answer_truncated,warnings:x.result.warnings||[],agent_status_url:x.base_url+x.status_url,n8n_execution:x.trace,original_caller_context:x.request.body.caller_context,replay_request:x.request}}];'''
def node(name,kind,version,params,x,y,**kwargs):
    return dict(id=str(uuid.uuid5(uuid.NAMESPACE_URL,'agent-a07/'+name)),name=name,type='n8n-nodes-base.'+kind,typeVersion=version,parameters=params,position=[x,y],**kwargs)
def workflow(version='00000000-0000-0000-0000-000000000000',question='선택한 자료의 연구 범위는 무엇인가요?',base_url='http://agent-gateway:8000',credential=None,request=None):
    u=urlsplit(base_url)
    if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment or re.search(r'[\s\x00-\x20\x7f]',base_url):raise ValueError('Agent URL must be an http(s) origin')
    if str(uuid.UUID(version))!=version:raise ValueError('Invalid Agent version')
    settings={'base_url':base_url.rstrip('/'),'agent_version_id':version,'question':question}
    if request is not None:settings['request']=request
    # Double JSON encoding: question text cannot become executable JavaScript.
    initial='return [{json:JSON.parse('+json.dumps(json.dumps(settings,ensure_ascii=False),ensure_ascii=False)+')}];'
    nodes=[node('Manual start','manualTrigger',1,{},0,0),node('Question settings','code',2,{'jsCode':initial},220,0),node('Prepare request','code',2,{'jsCode':PREPARE},440,0)]
    httpbase={'authentication':'genericCredentialType','genericAuthType':'httpHeaderAuth','options':{'timeout':8000,'redirect':{'redirect':{'followRedirects':False}},'response':{'response':{'fullResponse':True,'responseFormat':'json'}}}}
    cred={'credentials':{'httpHeaderAuth':{'id':credential,'name':'Agent Platform API'}}} if credential else {}
    nodes.append(node('Accept Agent run','httpRequest',4.2,dict(httpbase,method='POST',url='={{ $json.base_url + "/api/agent-runs" }}',sendHeaders=True,headerParameters={'parameters':[{'name':'Idempotency-Key','value':'={{ $json.request.invocation_key }}'}]},sendBody=True,specifyBody='json',jsonBody='={{ JSON.stringify($json.request.body) }}'),660,0,retryOnFail=True,maxTries=3,waitBetweenTries=1000,**cred))
    nodes.append(node('Run pointer','code',2,{'jsCode':POINTER},880,0))
    nodes.append(node('Read Agent run','httpRequest',4.2,dict(httpbase,method='GET',url='={{ $("Run pointer").first().json.base_url + $("Run pointer").first().json.status_url }}'),1100,0,retryOnFail=True,maxTries=3,waitBetweenTries=1000,**cred))
    nodes.append(node('Evaluate state','code',2,{'jsCode':EVALUATE},1320,0))
    def branch(name,field,x,y):return node(name,'if',2.2,{'conditions':{'options':{'caseSensitive':True,'leftValue':'','typeValidation':'strict','version':2},'conditions':[{'id':str(uuid.uuid5(uuid.NAMESPACE_URL,name)),'leftValue':'={{ $json.'+field+' }}','rightValue':'','operator':{'type':'boolean','operation':'true','singleValue':True}}],'combinator':'and'},'options':{}},x,y)
    nodes += [branch('Still waiting','keep_waiting',1540,0),node('Wait two seconds','wait',1.1,{'amount':2,'unit':'seconds'},1540,-200),branch('Agent succeeded','succeeded',1760,120),node('Answer and sources','code',2,{'jsCode':SUCCESS},1980,40),node('Agent failure','stopAndError',1,{'errorMessage':'={{ JSON.stringify({state:$json.state,error:$json.error,agent_run_id:$json.agent_run_id,request_id:$json.request_id,tool_call_id:$json.tool_call_id,status_url:$json.base_url+$json.status_url}) }}'},1980,240)]
    connections={}
    def connect(a,b,output=0):
        main=connections.setdefault(a,{'main':[]})['main']
        while len(main)<=output:main.append([])
        main[output].append({'node':b,'type':'main','index':0})
    chain=['Manual start','Question settings','Prepare request','Accept Agent run','Run pointer','Read Agent run','Evaluate state','Still waiting']
    for a,b in zip(chain,chain[1:]):connect(a,b)
    connect('Still waiting','Wait two seconds');connect('Wait two seconds','Read Agent run');connect('Still waiting','Agent succeeded',1);connect('Agent succeeded','Answer and sources');connect('Agent succeeded','Agent failure',1)
    return {'name':'Agent Platform · 질문과 출처','active':False,'nodes':nodes,'connections':connections,'settings':{'executionOrder':'v1','executionTimeout':720,'saveExecutionProgress':True,'saveManualExecutions':True,'saveDataErrorExecution':'all','saveDataSuccessExecution':'all','callerPolicy':'workflowsFromSameOwner'}}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--agent-version-id',default='00000000-0000-0000-0000-000000000000');p.add_argument('--question',default='선택한 자료의 연구 범위는 무엇인가요?');p.add_argument('--agent-url',default='http://agent-gateway:8000');p.add_argument('--credential-id');p.add_argument('--replay-file',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists() or a.output.is_symlink():raise ValueError('Output already exists; choose a new file')
    request=json.loads(a.replay_file.read_text()) if a.replay_file else None
    w=workflow(a.agent_version_id,a.question,a.agent_url,a.credential_id,request)
    with a.output.open('x') as f:json.dump(w,f,ensure_ascii=False,indent=2);f.write('\n')
    a.output.chmod(0o600);print('Created inactive workflow; select Agent API Header Auth credentials before execution.')
if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as e:raise SystemExit('FAIL: '+str(e))
