#!/usr/bin/env python3
"""Reconstruct a missing local operational session from one retained n8n execution.

inspect: GET-only; restore: read the two referenced credentials with the local
n8n admin CLI and write private local files. Never create/publish a Flow or run it.
"""
import argparse
import copy
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ops'))
import acceptance as check
from operational import desired, node_output
from operational_workflow import matches
from live_check import save
from credential_reader import read_credential


def require(value, code):
    check.require(value, code)


def historical_matches(actual, expected):
    # n8n 2.40.5 materializes these exact node defaults in execution snapshots.
    # Accept only these values; never ignore extra or changed executable settings.
    defaults = {
        ('webhook', 2): {'multipleMethods': False, 'webhookNotice': ''},
        ('code', 2): {'mode': 'runOnceForAllItems', 'language': 'javaScript', 'notice': ''},
        ('if', 2.2): {'looseTypeValidation': False},
        ('wait', 1.1): {'resume': 'timeInterval'},
        ('stopAndError', 1): {'errorType': 'errorMessage'},
        ('respondToWebhook', 1.4): {'enableResponseOutput': False, 'generalNotice': '', 'webhookNotice': ''},
        ('httpRequest', 4.2): {'curlImport': '', 'provideSslCertificates': False, 'sendQuery': False,
                             'sendHeaders': False, 'sendBody': False, 'specifyHeaders': 'keypair',
                             'contentType': 'json', 'infoMessage': ''},
    }
    normalized = copy.deepcopy(actual)
    wanted = {n['id']: n for n in expected['nodes']}
    for node in normalized.get('nodes', []):
        extra = defaults.get((node['type'].removeprefix('n8n-nodes-base.'), node['typeVersion']), {})
        for key, value in extra.items():
            params = node.get('parameters', {})
            if key not in wanted.get(node['id'], {}).get('parameters', {}) and key in params:
                if type(params[key]) is not type(value) or params[key] != value:
                    return False
                del params[key]
        if node['type'] == 'n8n-nodes-base.httpRequest' and node['typeVersion'] == 4.2:
            response = node['parameters'].get('options', {}).get('response', {}).get('response', {})
            if 'neverError' in response:
                if response['neverError'] is not False:
                    return False
                del response['neverError']
    return matches(normalized, expected, history=True)


def collect(api, client, link_id, execution_id, token):
    link_id, execution_id = check.uuid_id(link_id), check.n8n_id(execution_id)
    link = api.get('/api/workflow-links/' + link_id)
    require(link['workflow_link_id'] == link_id and link['enabled'], 'LINK_DISABLED_OR_CHANGED')
    wid = check.n8n_id(link['workflow_id'])
    wf = client.request('GET', '/api/v1/workflows/' + wid)
    require(wf['id'] == wid and wf.get('active') and wf.get('activeVersionId') == wf.get('versionId'),
            'WORKFLOW_NOT_CURRENTLY_PUBLISHED')
    execution = client.request('GET', '/api/v1/executions/' + execution_id + '?includeData=true')
    require(str(execution['id']) == execution_id and str(execution['workflowId']) == wid and
            execution['status'] in ('success', 'error', 'crashed', 'canceled'), 'EXECUTION_NOT_RETAINED_TERMINAL')
    prepared, pointer = node_output(execution, 'Prepare request'), node_output(execution, 'Run pointer')
    require(prepared and pointer, 'EXECUTION_DATA_NOT_RETAINED')
    envelope = prepared['request']
    require(pointer['request'] == envelope and pointer['base_url'] == prepared['base_url'], 'EXECUTION_REQUEST_CHANGED')
    run_id = check.uuid_id(pointer['agent_run_id'])
    _, summary, run = check.inspect_run(api, run_id)
    av = api.get('/api/agent-versions/' + check.uuid_id(link['agent_version_id']))
    require(av['state'] == 'published', 'AGENT_NOT_PUBLISHED')
    nodes = {n['name']: n for n in wf['nodes']}
    webhook = nodes['Question webhook']
    path = webhook['parameters']['path']
    require(path.startswith('agent-question-'), 'WEBHOOK_PATH_INVALID')
    session_id = check.uuid_id(path.removeprefix('agent-question-'))
    agent_credential = check.n8n_id(nodes['Accept Agent run']['credentials']['httpHeaderAuth']['id'])
    hook_credential = check.n8n_id(webhook['credentials']['jwtAuth']['id'])
    require(agent_credential != hook_credential, 'CREDENTIAL_BINDING_INVALID')
    logical_id = check.uuid_id(envelope['invocation_key'])
    caller = {'system': 'n8n', 'workflow_id': wid, 'execution_id': 'request:' + logical_id, 'node_id': 'agent-call'}
    require(run['agent_version_id'] == av['agent_version_id'] and run['mode'] == 'run' and
            run['definition_sha256'] == av['definition_sha256'] and run['invocation_key'] == logical_id and
            run['caller_context'] == caller and set(run['input']) == {'question'} and
            envelope == {'invocation_key': logical_id, 'body': {
                'agent_version_id': av['agent_version_id'], 'mode': 'run', 'input': run['input'], 'caller_context': caller}},
            'AGENT_EXECUTION_BINDING_CHANGED')
    state = {'schema': 1, 'session_id': session_id, 'agent_version_id': av['agent_version_id'],
             'definition_sha256': av['definition_sha256'], 'agent_url': api.url,
             'container_agent_url': prepared['base_url'], 'n8n_url': client.url,
             'instance_url': link['instance_url'], 'display_name': link['display_name'], 'workflow_name': wf['name'],
             'webhook_path': path, 'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
             'resources': {'workflow': {'id': wid}, 'agent_credential': {'id': agent_credential},
                           'hook_credential': {'id': hook_credential}},
             'published_version_id': wf['activeVersionId'], 'workflow_link_id': link_id, 'prepared': True,
             'recovered_from': {'n8n_execution_id': execution_id, 'agent_run_id': run_id}}
    expected = desired(state)
    require(matches(wf, expected), 'WORKFLOW_CONTENT_CHANGED')
    require(wf.get('activeVersion') and matches(wf['activeVersion'], expected, history=True), 'PUBLISHED_CONTENT_CHANGED')
    historical = execution.get('workflowData')
    require(historical and historical_matches(historical, expected), 'HISTORICAL_CONTENT_CHANGED')
    request = {'schema': 1, 'session_id': session_id,
               'body': {'request_id': logical_id, 'question': run['input']['question']},
               'receipt': {'agent_run_id': run_id, 'n8n_execution_id': execution_id}}
    return {'state': state, 'request': request, 'workflow': wf, 'link': link, 'execution': execution,
            'summary': {'workflow_id': wid, 'workflow_link_id': link_id, 'workflow_active': True,
                        'n8n_execution_id': execution_id, 'n8n_execution_state': execution['status'],
                        'agent_run_id': run_id, 'agent_state': run['state'], 'error': summary.get('error'),
                        'logical_request_id': logical_id}}


def destination(value):
    path = value.expanduser().absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'SESSION_SYMLINK')
    require(path != ROOT.parents[2] and ROOT.parents[2] not in path.parents, 'SESSION_MUST_BE_OUTSIDE_PROJECT')
    require(path.parent.is_dir(), 'SESSION_PARENT_MISSING')
    return path


def credentials(state, token, reader):
    hostname = urlsplit(state['n8n_url']).hostname
    local = hostname == 'localhost'
    try:
        local = local or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    require(local, 'LOCAL_N8N_REQUIRED_FOR_CREDENTIAL_RECOVERY')
    sid = state['session_id']
    for slot, kind, name in [('agent_credential', 'httpHeaderAuth', 'Agent API'),
                             ('hook_credential', 'jwtAuth', 'Agent workflow entry')]:
        cid = state['resources'][slot]['id']
        value = reader(cid)
        require(value.get('id') == cid and value.get('type') == kind and value.get('name') == name + ' · ' + sid,
                'CREDENTIAL_IDENTITY_CHANGED')
        data = value['data']
        if slot == 'agent_credential':
            require(data.get('name') == 'Authorization' and isinstance(data.get('value'), str) and
                    hmac.compare_digest(data['value'], 'Bearer ' + token), 'AGENT_CREDENTIAL_CHANGED')
        else:
            require(data.get('keyType') == 'passphrase' and data.get('algorithm') == 'HS256' and
                    isinstance(data.get('secret'), str) and len(data['secret']) >= 32, 'HOOK_CREDENTIAL_CHANGED')
            state['hook_secret'] = data['secret']


def restore(api, client, bundle, path, token, reader=read_credential):
    path = destination(path)
    state, request = bundle['state'], bundle['request']
    if path.exists():
        require(path.is_dir() and path.stat().st_uid == os.getuid() and not path.stat().st_mode & 0o077,
                'SESSION_DIRECTORY_PERMISSIONS')
        saved, old_request = (check.read_private(path / name) for name in ('settings.json', 'request.json'))
        # Only reuse a prior complete recovery of these exact records. Never replace arbitrary sessions.
        require(all(saved.get(k) == v for k, v in state.items()) and old_request == request and
                isinstance(saved.get('hook_secret'), str) and len(saved['hook_secret']) >= 32,
                'EXISTING_SESSION_PRESERVED')
        return 'already_restored'
    credentials(state, token, reader)
    # Recheck the live and published definitions and the retained execution after reading the credentials.
    for route, expected in [('/api/v1/workflows/' + state['resources']['workflow']['id'], bundle['workflow']),
                            ('/api/v1/executions/' + state['recovered_from']['n8n_execution_id'] + '?includeData=true', bundle['execution'])]:
        require(check.digest(client.request('GET', route)) == check.digest(expected), 'REMOTE_RECORD_CHANGED_DURING_RECOVERY')
    require(api.get('/api/workflow-links/' + state['workflow_link_id']) == bundle['link'], 'LINK_CHANGED_DURING_RECOVERY')
    # Create the complete directory atomically without replacing even an empty concurrent directory.
    with tempfile.TemporaryDirectory(prefix='.n8n-recover-', dir=path.parent) as td:
        stage = Path(td)
        save(stage / 'settings.json', state)
        save(stage / 'request.json', request)
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        require(hasattr(libc, 'renameat2'), 'LINUX_RENAME_NOREPLACE_REQUIRED')
        result = libc.renameat2(-100, os.fsencode(stage), -100, os.fsencode(path), 1)
        if result != 0:
            raise OSError(ctypes.get_errno(), 'Session not replaced')
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return 'restored'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['inspect', 'restore'])
    p.add_argument('--workflow-link-id', required=True)
    p.add_argument('--execution-id', required=True)
    p.add_argument('--session-dir', type=Path, default=check.DEFAULT_SESSION)
    p.add_argument('--key-file', type=Path, default=check.DEFAULT_KEY)
    p.add_argument('--token-file', type=Path, default=ROOT / '.secrets/api-token')
    p.add_argument('--agent-url', default='http://127.0.0.1:16100')
    a = p.parse_args()
    token = a.token_file.expanduser().read_text().strip()
    api = check.Reader(a.agent_url, token)
    client = check.N8nReader(check.read_private(a.key_file.expanduser()))
    path = destination(a.session_dir)
    bundle = collect(api, client, a.workflow_link_id, a.execution_id, token)
    result = restore(api, client, bundle, path, token) if a.action == 'restore' else 'inspected'
    print(json.dumps({'result': result, **bundle['summary'], 'session_dir': str(path),
                      'inference_executed': False, 'flow_or_credentials_created': False,
                      'local_session_written': result == 'restored', 'answer_delivery_acceptance': 'incomplete'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError) as exc:
        print(json.dumps({'result': 'failed', 'code': str(exc) if isinstance(exc, check.CheckError)
                          else 'RECOVERY_READ_OR_LOCAL_WRITE_FAILED', 'inference_executed': False,
                          'hint': 'Preserve existing files; check local n8n CLI access and retained execution data.'}))
        raise SystemExit(1)
