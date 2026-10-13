#!/usr/bin/env python3
"""Actual persistent n8n HTTP engine / isolated SQLite / Agent HTTP fixture.

Run with AGENT_TEST_N8N_BIN, or N8N_TEST_URL pointing at a NEW isolated test instance.
No GPU, operational n8n DB, project secrets, or real external platform calls.
"""
import argparse
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'n8n/ops'))
import operational as op
from api_access import Client, HTTPFailure, connect, read_private, save_private, verify_key


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


class Fixture:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.version = {'agent_version_id': str(uuid.uuid4()), 'agent_id': str(uuid.uuid4()),
                        'revision': 1, 'state': 'draft', 'definition_sha256': 'a' * 64}
        self.keys, self.runs, self.links = {}, {}, {}
        self.effects = self.posts = self.lost = 0
        self.lock = threading.Lock()

    def route(self, method, path, body, headers):
        with self.lock:
            if headers.get('Authorization') != 'Bearer ' + self.token: return 401, {}
            key = headers.get('Idempotency-Key')
            if method == 'GET':
                if path.startswith('/api/agent-versions/'): return 200, self.version
                if path.startswith('/api/workflow-links/'): return 200, self.links[path.rsplit('/', 1)[-1]]
                if path.endswith('/steps'):
                    run = self.runs[path.split('/')[-2]]
                    return 200, {'items': [{'step_id': s, 'state': 'failed' if run['state'] == 'failed' and s == 'generate' else 'succeeded'} for s in ['validate', 'search', 'context', 'generate', 'finalize']]}
                if path.startswith('/api/agent-runs/'): return 200, self.runs[path.rsplit('/', 1)[-1]]
            if method == 'POST' and path == '/api/agent-runs': self.posts += 1
            fingerprint = json.dumps([path, body], sort_keys=True)
            if key in self.keys:
                old, status, value = self.keys[key]
                if old != fingerprint: return 409, {'code': 'IDEMPOTENCY_CONFLICT'}
            elif method == 'POST' and path.endswith('/publish'):
                assert body == {'expected_revision': 1}
                self.version.update(state='published', revision=2)
                status, value = 200, copy.deepcopy(self.version)
                self.keys[key] = (fingerprint, status, value)
            elif method == 'POST' and path == '/api/workflow-links':
                assert self.version['state'] == 'published'
                uid = str(uuid.uuid4())
                value = {**body, 'workflow_link_id': uid, 'edit_url': body['instance_url'] + '/workflow/' + body['workflow_id'], 'enabled': True}
                self.links[uid] = value; status = 201
                self.keys[key] = (fingerprint, status, value)
            elif method == 'POST' and path == '/api/agent-runs':
                assert body['agent_version_id'] == self.version['agent_version_id'] and body['mode'] == 'run'
                self.effects += 1; uid = str(uuid.uuid4())
                failure = body['input']['question'] == 'fixture-failure'
                empty = body['input']['question'] == 'fixture-empty'
                self.runs[uid] = {**body, 'agent_run_id': uid, 'request_id': str(uuid.uuid4()),
                                  'invocation_key': key, 'definition_sha256': self.version['definition_sha256'],
                                  'tool_call_id': str(uuid.uuid4()), 'state': 'failed' if failure else 'succeeded',
                                  'error': {'code': 'MODEL_NOT_READY', 'stage': 'generate'} if failure else None,
                                  'result': None if failure else {'outcome': 'no_evidence' if empty else 'answered',
                                    'answer': '근거가 없습니다.' if empty else '시험 근거입니다. [S1]',
                                    'citations': [] if empty else [{'citation_id': 'S1', 'source': {'chunk_id': 'source-1', 'text': '시험 근거'}}],
                                    'warnings': [], 'answer_truncated': False}}
                status, value = 202, {'agent_run_id': uid, 'agent_version_id': body['agent_version_id'], 'state': 'accepted', 'status_url': '/api/agent-runs/' + uid}
                self.keys[key] = (fingerprint, status, value)
            else: return 404, {}
            if path == '/api/agent-runs' and self.lost:
                self.lost -= 1; return 0, {}
            return status, value


def serve(fixture):
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0')))) if self.command == 'POST' else None
                status, data = fixture.route(self.command, self.path.split('?')[0], body, self.headers)
                if not status:
                    self.close_connection = True; self.connection.shutdown(socket.SHUT_RDWR); return
                raw = json.dumps(data).encode()
                self.send_response(status); self.send_header('Content-Type', 'application/json')
                self.send_header('X-Request-ID', self.headers.get('X-Request-ID', ''))
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass
        do_GET = do_POST = handle_request
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    return server


def run_checks(url, folder, fixture, fixture_url, container_fixture_url, restart=None):
    client = Client(url)
    settings = client.request('GET', '/rest/settings')['data']
    assert settings.get('userManagement', {}).get('showSetupOnFirstLoad'), 'Only a NEW isolated n8n instance is allowed'
    owner = {'email': 'agent-check@example.invalid', 'firstName': 'Agent', 'lastName': 'Check', 'password': 'Aa1' + secrets.token_hex(16)}
    keypath = folder / 'api-key.json'
    assert connect(client, keypath, setup=owner)['reused'] is False
    assert connect(Client(url), keypath)['reused'] is True
    public = verify_key(read_private(keypath))
    assert not public.items('/api/v1/workflows')
    checks = ['normal owner setup and scoped API key; existing key reused']
    directory = folder / 'session'; directory.mkdir(mode=0o700)
    args = argparse.Namespace(agent_version_id=fixture.version['agent_version_id'], from_live_session=None,
                              publish_agent=True, instance_url=url, agent_url=fixture_url,
                              container_agent_url=container_fixture_url, name='운영 기능 검사')
    api = op.API(fixture_url, fixture.token)

    class LostCreate:
        def __init__(self, real): self.real = real; self.url = real.url; self.once = True
        def request(self, method, route, body=None, **kwargs):
            response = self.real.request(method, route, body, **kwargs)
            if self.once and method == 'POST' and route == '/api/v1/workflows':
                self.once = False; raise ValueError('fixture lost create response')
            return response
        def items(self, route): return self.real.items(route)
    try: op.prepare(args, directory, LostCreate(public), api, fixture.token)
    except ValueError as e: assert 'fixture lost' in str(e)
    else: raise AssertionError('missing simulated lost response')
    result = op.prepare(args, directory, public, api, fixture.token)
    again = op.prepare(args, directory, public, api, fixture.token)
    assert result['workflow_id'] == again['workflow_id'] and len(public.items('/api/v1/workflows')) == 1
    assert len(public.items('/api/v1/credentials')) == 2 and len(fixture.links) == 1 and fixture.effects == 0
    checks.append('lost create response reconciled; prepare twice preserves IDs and does not infer')
    state = op.private_read(directory / 'settings.json')
    wf = op.checked_workflow(public, state, published=True)
    assert fixture.token not in json.dumps(wf) and state['hook_secret'] not in json.dumps(wf)
    hook = '/webhook/' + state['webhook_path']
    try: public.request('POST', hook, {'request_id': str(uuid.uuid4()), 'question': 'no auth'})
    except HTTPFailure as e: assert e.status in (401, 403)
    else: raise AssertionError('Unauthenticated webhook allowed')
    assert fixture.effects == 0
    checks.append('webhook authentication required; workflow JSON contains no tokens')
    request_path = directory / 'request.json'
    req = op.request_state(request_path, state, '시험 질문')
    saved = op.private_read(request_path)
    assert saved == req
    fixture.lost = 3
    headers = op.webhook_headers(state, req['body'])
    try: public.request('POST', hook, req['body'], headers=headers, timeout=90)
    except HTTPFailure: pass
    else: raise AssertionError('All acceptance responses should be lost')
    assert fixture.effects == 1 and op.private_read(request_path) == saved
    with redirect_stdout(io.StringIO()): uncertain = op.lookup(public, api, state, req, request_path)
    assert uncertain['agent_run_id'] is None and uncertain['inference_executed'] == 'unknown'
    first = public.request('POST', hook, req['body'], headers=headers, timeout=90)
    second = public.request('POST', hook, req['body'], headers=headers, timeout=90)
    assert first['agent_run_id'] == second['agent_run_id'] and first['n8n_execution'] != second['n8n_execution']
    assert first['original_caller_context'] == second['original_caller_context'] and fixture.effects == 1
    req['receipt'] = {'agent_run_id': second['agent_run_id'], 'n8n_execution_id': second['n8n_execution']['execution_id']}; op.save(request_path, req)
    def lookup(request=req, path=request_path):
        for _ in range(30):
            with redirect_stdout(io.StringIO()): result = op.lookup(public, api, state, request, path)
            if result['result'] not in ('pending', 'incomplete'): return result
            time.sleep(.2)
        return result
    report = lookup()
    assert report['result'] == 'passed' and report['replay_verified'], report
    for execution in op.find_executions(public, state, req):
        assert state['hook_secret'] not in json.dumps(execution), 'webhook signing key persisted'
        assert fixture.token not in json.dumps(execution), 'Agent token persisted'
    checks.append('all three acceptance responses lost; durable request replay preserves one Agent effect and original caller')
    changed = {**req['body'], 'question': 'changed body'}
    before = fixture.posts
    try: public.request('POST', hook, changed, headers=headers, timeout=30)
    except HTTPFailure: pass
    else: raise AssertionError('JWT must bind the question body')
    assert fixture.posts == before
    try: public.request('POST', hook, changed, headers=op.webhook_headers(state, changed), timeout=30)
    except HTTPFailure: pass
    else: raise AssertionError('Changed body should fail')
    assert fixture.effects == 1
    try: op.request_state(request_path, state, 'changed local question')
    except ValueError: pass
    else: raise AssertionError('Changed local question accepted')
    checks.append('same key with changed question rejected locally and by Agent')
    for label, expected in [('fixture-empty', 'passed'), ('fixture-failure', 'failed')]:
        path = directory / (label + '.json'); request = op.request_state(path, state, label)
        try: response = public.request('POST', hook, request['body'], headers=op.webhook_headers(state, request['body']), timeout=90)
        except HTTPFailure as e: assert label == 'fixture-failure' and e.status == 502
        else:
            request['receipt'] = {'agent_run_id': response['agent_run_id']}; op.save(path, request)
        report = lookup(request, path); assert report['result'] == expected, report
        if label == 'fixture-failure': assert report['error'] == {'code': 'MODEL_NOT_READY', 'stage': 'generate'}
        else: assert report['outcome'] == 'no_evidence'
    checks.append('no_evidence is successful; Agent failure preserves code/stage and n8n fails')
    wrong = copy.deepcopy(state); wrong['workflow_name'] = 'unexpected'
    try: op.checked_workflow(public, wrong, published=True)
    except ValueError: pass
    else: raise AssertionError('Workflow drift accepted')
    count = fixture.effects
    if restart:
        restart(); op.checked_workflow(public, state, published=True)
        report = lookup(); assert report['result'] == 'passed' and fixture.effects == count
        checks.append('n8n process restart preserves published workflow/credentials/executions; GET-only lookup')
    public.request('POST', '/api/v1/workflows/' + wf['id'] + '/unpublish')
    assert not op.checked_workflow(public, state)['active']
    report = lookup(); assert report['result'] == 'passed' and fixture.effects == count
    checks.append('workflow drift blocked; unpublish preserves GET-only history')
    return {'result': 'passed', 'stage': 'A07-operational', 'scope': 'real n8n HTTP / isolated SQLite / Agent HTTP fixture',
            'checks': checks, 'effects': fixture.effects, 'posts': fixture.posts}


def main():
    fixture = Fixture(); server = serve(fixture)
    external = os.environ.get('N8N_TEST_URL')
    process = None
    with tempfile.TemporaryDirectory(prefix='n8n-operational-') as td:
        folder = Path(td)
        def stop():
            nonlocal process
            if process:
                process.terminate()
                try: process.wait(timeout=12)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
                process = None
        def start():
            nonlocal process
            process = subprocess.Popen([os.environ['AGENT_TEST_N8N_BIN'], 'start'], env=env, stdout=log, stderr=log)
            wait_ready()
        def wait_ready():
            for _ in range(150):
                try: Client(url).request('GET', '/healthz/readiness'); return
                except ValueError:
                    if process and process.poll() is not None: raise ValueError('n8n test server exited')
                    time.sleep(.2)
            raise ValueError('n8n test server did not become ready')
        def restart(): stop(); start()
        url = external or 'http://127.0.0.1:' + str(free_port())
        # Docker test DNS is local to its private test network; transport is isolated.
        if external:
            import api_access
            api_access.origin = op.origin
        fixture_url = 'http://127.0.0.1:' + str(server.server_port)
        container_fixture_url = 'http://' + os.environ.get('N8N_TEST_FIXTURE_HOST', '127.0.0.1') + ':' + str(server.server_port)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DB_', 'N8N_', 'NODE_FUNCTION_ALLOW_'))}
        env.update(N8N_USER_FOLDER=str(folder / 'n8n'), DB_TYPE='sqlite', N8N_ENCRYPTION_KEY=secrets.token_hex(32),
                   N8N_PORT=url.rsplit(':', 1)[-1], N8N_LISTEN_ADDRESS='127.0.0.1', N8N_RUNNERS_BROKER_PORT=str(free_port()),
                   N8N_SECURE_COOKIE='false', N8N_DIAGNOSTICS_ENABLED='false', N8N_VERSION_NOTIFICATIONS_ENABLED='false',
                   N8N_COMMUNITY_PACKAGES_ENABLED='false', N8N_PERSONALIZATION_ENABLED='false',
                   N8N_BLOCK_ENV_ACCESS_IN_NODE='true', NODE_FUNCTION_ALLOW_BUILTIN='crypto', EXECUTIONS_DATA_PRUNE='false')
        with (folder / 'n8n.log').open('w+') as log:
            try:
                if not external: start()
                else: wait_ready()
                print(json.dumps(run_checks(url, folder, fixture, fixture_url, container_fixture_url, None if external else restart), ensure_ascii=False, indent=2))
            except Exception:
                log.flush(); log.seek(0)
                # Fixture logs only; operational secrets are never mounted here.
                print(log.read()[-6000:], file=sys.stderr)
                raise
            finally: stop(); server.shutdown(); server.server_close()


if __name__ == '__main__': main()
