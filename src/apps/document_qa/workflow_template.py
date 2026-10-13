"""Fixed Q03b n8n entry flow. No documents, secrets, model selection or domain calls."""
import copy
import hashlib
import json
from uuid import UUID, uuid5, NAMESPACE_URL

from .readiness import origin
from .intake import check

VALIDATE = r'''const c=CONFIG, input=$input.first().json, b=input.body, jwt=input.jwtPayload;
const uuid=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
if(!b || Array.isArray(b) || Object.keys(b).sort().join(',')!=='handoff_id,job_id,library_id,schema,sequence,source_document_id' ||
 b.schema!==1 || !Number.isSafeInteger(b.sequence) || b.sequence<1 ||
 !['handoff_id','job_id','library_id','source_document_id'].every(k=>typeof b[k]==='string' && uuid.test(b[k]) && b[k]!=='00000000-0000-0000-0000-000000000000')) throw Error('HANDOFF_INPUT_INVALID');
const canonical=Object.fromEntries(Object.keys(b).sort().map(k=>[k,b[k]]));
const sha=require('crypto').createHash('sha256').update(JSON.stringify(canonical),'utf8').digest('hex');
const now=Math.floor(Date.now()/1000);
if(!jwt || jwt.aud!==c.path || jwt.sub!==b.handoff_id || jwt.request_sha256!==sha ||
 !Number.isInteger(jwt.iat) || !Number.isInteger(jwt.exp) || jwt.exp<=now || jwt.exp-jwt.iat>120 ||
 jwt.exp<=jwt.iat || jwt.iat>now+5) throw Error('HANDOFF_AUTH_INVALID');
if(!/^[A-Za-z0-9_-]{1,128}$/.test(String($workflow.id)) || !/^[A-Za-z0-9_-]{1,128}$/.test(String($execution.id))) throw Error('ENGINE_ID_INVALID');
return [{json:{...b,workflow_id:String($workflow.id),execution_id:String($execution.id),definition_sha256:c.definition_sha256}}];'''

VERIFY = r'''const r=$input.first().json, p=$('Validate handoff').first().json, b=r.body;
if(r.statusCode!==200 || !b || b.handoff_id!==p.handoff_id || b.job_id!==p.job_id ||
 b.source_document_id!==p.source_document_id || b.workflow_id!==p.workflow_id ||
 b.definition_sha256!==p.definition_sha256 || b.phase!=='awaiting_refinement' ||
 b.question_ready!==false || b.workflow_started!==true || b.may_execute_next_stage!==false ||
 !/^[A-Za-z0-9_-]{1,128}$/.test(b.first_execution_id)) throw Error('HANDOFF_RECEIPT_INVALID');
// Q04 extends a separately checked stage API. A trigger alone never authorizes domain effects.
return [{json:b}];'''


def build(qa_origin, path, qa_credential, hook_credential):
    qa_origin = origin(qa_origin)
    check(isinstance(path, str) and path.startswith('document-qa-') and len(path) <= 100
          and all(c.isalnum() or c == '-' for c in path), 'WORKFLOW_PATH_INVALID')
    def node(name, kind, version, params, x, **extra):
        return dict(id=str(uuid5(NAMESPACE_URL, 'document-qa-q03b/' + name)), name=name,
                    type='n8n-nodes-base.' + kind, typeVersion=version,
                    parameters=params, position=[x, 0], **extra)
    config = {'path': path, 'definition_sha256': 'DEFINITION_SHA256'}
    nodes = [
        node('Document preparation webhook', 'webhook', 2,
             {'httpMethod': 'POST', 'path': path, 'authentication': 'jwtAuth',
              'responseMode': 'responseNode', 'options': {}}, 0,
             webhookId=path, credentials={'jwtAuth': {'id': hook_credential, 'name': 'Document QA entry'}}),
        node('Validate handoff', 'code', 2,
             {'jsCode': VALIDATE.replace('CONFIG', json.dumps(config, sort_keys=True, separators=(',', ':')))}, 220),
        node('Commit QA receipt', 'httpRequest', 4.2,
             {'authentication': 'genericCredentialType', 'genericAuthType': 'httpHeaderAuth',
              'method': 'POST', 'url': qa_origin + '/api/workflow/receipts',
              'sendBody': True, 'specifyBody': 'json', 'jsonBody': '={{ JSON.stringify($json) }}',
              'options': {'timeout': 8000, 'redirect': {'redirect': {'followRedirects': False}},
                          'response': {'response': {'fullResponse': True, 'responseFormat': 'json'}}}}, 440,
             credentials={'httpHeaderAuth': {'id': qa_credential, 'name': 'Document QA receipt API'}},
             retryOnFail=False),
        node('Verify QA receipt', 'code', 2, {'jsCode': VERIFY}, 660),
        node('Return preparation receipt', 'respondToWebhook', 1.4,
             {'respondWith': 'json', 'responseBody': '={{ JSON.stringify($json) }}',
              'options': {'responseCode': 200}}, 880)
    ]
    edges = {a['name']: {'main': [[{'node': b['name'], 'type': 'main', 'index': 0}]]}
             for a, b in zip(nodes, nodes[1:])}
    flow = {'name': 'Document QA preparation · ' + path, 'nodes': nodes, 'connections': edges,
            'settings': {'executionOrder': 'v1', 'executionTimeout': 60, 'saveExecutionProgress': True,
                         'saveManualExecutions': True, 'saveDataErrorExecution': 'all',
                         'saveDataSuccessExecution': 'all', 'callerPolicy': 'workflowsFromSameOwner'}}
    digest = hashlib.sha256(json.dumps(flow, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    nodes[1]['parameters']['jsCode'] = nodes[1]['parameters']['jsCode'].replace('DEFINITION_SHA256', digest)
    return flow, digest


def matches(actual, wanted):
    if actual.get('name') != wanted['name'] or actual.get('connections') != wanted['connections']:
        return False
    if any(actual.get('settings', {}).get(k) != v for k, v in wanted['settings'].items()):
        return False
    rows = actual.get('nodes')
    if not isinstance(rows, list) or len(rows) != len(wanted['nodes']):
        return False
    by_id = {n.get('id'): n for n in rows if isinstance(n, dict)}
    if len(by_id) != len(rows):
        return False
    for node in wanted['nodes']:
        current = copy.deepcopy(by_id.get(node['id'], {}))
        for k in ('notes', 'notesInFlow'):
            current.pop(k, None)
        for cred in current.get('credentials', {}).values():
            cred.pop('name', None)
        expected = copy.deepcopy(node)
        for cred in expected.get('credentials', {}).values():
            cred.pop('name', None)
        # n8n assigns webhookId to API-created Wait nodes too. It has no
        # routing role in this fixed time-interval mode. Keep the authored
        # definition/digest unchanged; every other field still compares exactly.
        if ('webhookId' in current and 'webhookId' not in expected
                and expected.get('type') == 'n8n-nodes-base.wait'
                and expected.get('typeVersion') == 1.1
                and expected.get('parameters', {}).get('resume') == 'timeInterval'):
            generated = current['webhookId']
            if not isinstance(generated, str):
                return False
            try:
                identity = UUID(generated)
            except ValueError:
                return False
            if str(identity) != generated or not identity.int:
                return False
            current.pop('webhookId')
        if current != expected:
            return False
    return True
