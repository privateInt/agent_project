"""Fixed Q04b n8n flow: grant receipt, early response and bounded CPU observations."""
import hashlib
import json
from uuid import uuid5, NAMESPACE_URL

from .workflow_template import VALIDATE
from .readiness import origin
from .intake import check

VERIFY = r'''const r=$input.first().json,p=$('Validate handoff').first().json,b=r.body;
const uuid=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
if(r.statusCode!==200 || !b || b.schema!==1 ||
 !['handoff_id','job_id','library_id','sequence','generation','source_document_id','workflow_id','definition_sha256'].every(k=>b[k]===p[k]) ||
 b.phase!=='cpu_refinement' || b.may_execute_next_stage!==true || b.question_ready!==false ||
 typeof b.grant_id!=='string' || !uuid.test(b.grant_id) ||
 !/^[A-Za-z0-9_-]{1,128}$/.test(b.first_execution_id) ||
 !/^[A-Za-z0-9_-]{1,128}$/.test(b.published_version_id)) throw Error('REFINEMENT_FLOW_RECEIPT_INVALID');
return [{json:b}];'''

OWNER = r'''const b=$input.first().json,p=$('Validate handoff').first().json;
// Duplicate executions return the same receipt but cannot perform domain effects.
if(b.first_execution_id!==p.execution_id) return [];
return [{json:{schema:1,handoff_id:b.handoff_id,job_id:b.job_id,generation:b.generation,
 workflow_id:b.workflow_id,execution_id:p.execution_id,definition_sha256:b.definition_sha256,grant_id:b.grant_id}}];'''

RESULT = r'''const r=$input.first().json,p=$('Own execution grant').first().json,b=r.body;
if(r.statusCode!==200 || !b || b.schema!==1 ||
 !['handoff_id','job_id','generation','execution_id'].every(k=>b[k]===p[k]) || b.question_ready!==false ||
 !Number.isSafeInteger(b.steps) || b.steps<0 || b.steps>40 || typeof b.continue_wait!=='boolean' ||
 !['running','canonical_verified','attention_required','expired'].includes(b.state) ||
 !b.refinement || b.refinement.job_id!==p.job_id || b.refinement.question_ready!==false ||
 typeof b.refinement.refinement_ready!=='boolean' ||
 b.continue_wait!==(b.state==='running') ||
 (b.state==='canonical_verified' && (b.refinement.refinement_ready!==true || b.refinement.state!=='canonical_verified')) ||
 (b.continue_wait && b.refinement.refinement_ready)) throw Error('REFINEMENT_FLOW_RESULT_INVALID');
// Success and attention both end this execution. QA stores the detailed result.
return b.continue_wait ? [{json:b}] : [];'''


def build(qa_origin,path,qa_credential,hook_credential):
    qa_origin=origin(qa_origin)
    check(isinstance(path,str) and path.startswith('document-qa-') and len(path)<=100
          and all(c.isalnum() or c=='-' for c in path),'WORKFLOW_PATH_INVALID')
    def node(name,kind,version,params,x,y=0,**extra):
        return dict(id=str(uuid5(NAMESPACE_URL,'document-qa-q04b/'+name)),name=name,
                    type='n8n-nodes-base.'+kind,typeVersion=version,parameters=params,position=[x,y],**extra)
    def http(name,route,body,x,y=0,timeout=8000):
        return node(name,'httpRequest',4.2,{'authentication':'genericCredentialType','genericAuthType':'httpHeaderAuth',
             'method':'POST','url':qa_origin+route,'sendBody':True,'specifyBody':'json','jsonBody':body,
             'options':{'timeout':timeout,'redirect':{'redirect':{'followRedirects':False}},
                        'response':{'response':{'fullResponse':True,'responseFormat':'json'}}}},x,y,
             credentials={'httpHeaderAuth':{'id':qa_credential,'name':'Document QA CPU stage API'}},retryOnFail=False)
    config={'path':path,'definition_sha256':'DEFINITION_SHA256'}
    validate=VALIDATE.replace('handoff_id,job_id,library_id,schema,sequence,source_document_id',
                             'generation,handoff_id,job_id,library_id,schema,sequence,source_document_id')
    validate=validate.replace('b.schema!==1 ||','b.schema!==1 || !Number.isSafeInteger(b.generation) || b.generation<1 || b.generation>16 ||')
    nodes=[node('CPU preparation webhook','webhook',2,{'httpMethod':'POST','path':path,'authentication':'jwtAuth',
              'responseMode':'responseNode','options':{}},0,webhookId=path,
              credentials={'jwtAuth':{'id':hook_credential,'name':'Document QA CPU entry'}}),
           node('Validate handoff','code',2,{'jsCode':validate.replace('CONFIG',json.dumps(config,sort_keys=True,separators=(',',':')))},220),
           http('Commit execution grant','/api/workflow/refinement/receipts','={{ JSON.stringify($json) }}',440),
           node('Verify execution grant','code',2,{'jsCode':VERIFY},660),
           node('Return execution receipt','respondToWebhook',1.4,{'respondWith':'json','responseBody':'={{ JSON.stringify($json) }}','options':{'responseCode':200}},880),
           node('Own execution grant','code',2,{'jsCode':OWNER},880,220),
           http('CPU refinement step','/api/workflow/refinement/step',"={{ JSON.stringify($('Own execution grant').first().json) }}",660,220,65000),
           node('Verify refinement result','code',2,{'jsCode':RESULT},440,220),
           node('Wait before observation','wait',1.1,{'resume':'timeInterval','amount':5,'unit':'seconds'},220,220)]
    edges={a['name']:{'main':[[{'node':b['name'],'type':'main','index':0}]]} for a,b in zip(nodes,nodes[1:])}
    edges[nodes[-1]['name']]={'main':[[{'node':'CPU refinement step','type':'main','index':0}]]}
    flow={'name':'Document QA CPU refinement · '+path,'nodes':nodes,'connections':edges,
          'settings':{'executionOrder':'v1','executionTimeout':600,'saveExecutionProgress':True,
                      'saveManualExecutions':True,'saveDataErrorExecution':'all','saveDataSuccessExecution':'all',
                      'callerPolicy':'workflowsFromSameOwner'}}
    digest=hashlib.sha256(json.dumps(flow,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    nodes[1]['parameters']['jsCode']=nodes[1]['parameters']['jsCode'].replace('DEFINITION_SHA256',digest)
    return flow,digest
