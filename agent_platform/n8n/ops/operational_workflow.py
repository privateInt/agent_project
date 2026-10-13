"""Authenticated webhook entrypoint for the existing bounded Agent workflow."""
import json
import uuid
from workflow import workflow, node


def build(version, base_url, agent_credential, hook_credential, path, name):
    w = workflow(version, '', base_url, agent_credential)
    w.pop('active')  # Public API accepts only writable fields.
    w['name'] = name
    start = w['nodes'][0]
    start.update(name='Question webhook', type='n8n-nodes-base.webhook', typeVersion=2,
                 webhookId=path,
                 parameters={'httpMethod': 'POST', 'path': path, 'authentication': 'jwtAuth',
                             'responseMode': 'responseNode', 'options': {}},
                 credentials={'jwtAuth': {'id': hook_credential, 'name': 'Agent workflow entry'}})
    config = json.dumps({'version': version, 'base_url': base_url, 'audience': path}, ensure_ascii=False)
    # The external input cannot select a different Agent, URL, mode, or caller context.
    w['nodes'][1]['parameters']['jsCode'] = '''const c=JSON.parse(CONFIG);
const input=$input.first().json,b=input.body,jwt=input.jwtPayload;
if (!b || typeof b!=='object' || Array.isArray(b) || Object.keys(b).sort().join(',')!=='question,request_id' ||
 !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(b.request_id) ||
 typeof b.question!=='string' || !b.question.trim() || b.question.includes('\\0') || Buffer.byteLength(b.question,'utf8')>8192)
 throw Error('WORKFLOW_INPUT_INVALID');
const digest=require('crypto').createHash('sha256').update(JSON.stringify({question:b.question,request_id:b.request_id}),'utf8').digest('hex');
if (!jwt || jwt.aud!==c.audience || jwt.sub!==c.version || jwt.request_sha256!==digest ||
 !Number.isInteger(jwt.iat) || !Number.isInteger(jwt.exp) || jwt.exp-jwt.iat>120 || jwt.exp<=Date.now()/1000 || jwt.iat>Date.now()/1000+5)
 throw Error('WORKFLOW_REQUEST_AUTH_INVALID');
// Request identity belongs to the durable caller request, not a transient n8n execution.
// Actual n8n execution IDs remain in trace; retries keep this entire body unchanged.
return [{json:{base_url:c.base_url,request:{invocation_key:b.request_id,body:{
 agent_version_id:c.version,mode:'run',input:{question:b.question},
 caller_context:{system:'n8n',workflow_id:String($workflow.id),execution_id:'request:'+b.request_id,node_id:'agent-call'}
}}}}];'''.replace('CONFIG', json.dumps(config, ensure_ascii=False))
    w['connections']['Question webhook'] = w['connections'].pop('Manual start')
    next(n for n in w['nodes'] if n['name'] == 'Wait two seconds')['webhookId'] = str(uuid.uuid5(uuid.NAMESPACE_URL, path + '/wait'))
    w['nodes'] += [
        node('Return answer', 'respondToWebhook', 1.4,
             {'respondWith': 'json', 'responseBody': '={{ JSON.stringify($json) }}', 'options': {'responseCode': 200}}, 2200, 40),
        node('Return failure', 'respondToWebhook', 1.4,
             {'respondWith': 'json', 'responseBody': '={{ JSON.stringify({state:$json.state,error:{code:$json.error?.code||"AGENT_FAILED",stage:$json.error?.stage||""},agent_run_id:$json.agent_run_id,agent_version_id:$json.request.body.agent_version_id,request_id:$json.request_id,tool_call_id:$json.tool_call_id,n8n_execution:$json.trace}) }}',
              'options': {'responseCode': 502}}, 1980, 240)]
    def edge(a, b): w['connections'][a] = {'main': [[{'node': b, 'type': 'main', 'index': 0}]]}
    edge('Answer and sources', 'Return answer')
    w['connections']['Agent succeeded']['main'][1][0]['node'] = 'Return failure'
    edge('Return failure', 'Agent failure')
    return w


def matches(actual, expected, *, history=False):
    """Ignore server metadata/default settings, never ignore executable content."""
    for key in (('connections',) if history else ('name', 'connections')):
        if actual.get(key) != expected.get(key): return False
    if len(actual.get('nodes', [])) != len(expected['nodes']): return False
    by_id = {n.get('id'): n for n in actual['nodes']}
    for node in expected['nodes']:
        current = by_id.get(node['id'], {})
        for key, value in node.items():
            if key == 'credentials':
                if {k: v.get('id') for k, v in current.get(key, {}).items()} != {k: v.get('id') for k, v in value.items()}: return False
                continue
            if current.get(key) != value: return False
        # Extra execution modifiers change behavior even when all expected fields match.
        if any(key not in node and key not in ('notes', 'notesInFlow') for key in current): return False
    return history or all(actual.get('settings', {}).get(k) == v for k, v in expected['settings'].items())
