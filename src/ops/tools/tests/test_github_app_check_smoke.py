"""새 diagnostic smoke의 격리 검사. 실제 App/key/GitHub에는 연결하지 않는다."""
import ast
from collections import Counter
from contextlib import ExitStack, contextmanager, redirect_stderr
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import http.server
import inspect
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.ops.tools import github_app_check_smoke as q


ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / 'src/ops/tools/github_app_check_smoke.py'
AUTH_SOURCE = ROOT / 'src/ops/tools/github_app_readback.py'
AUTH_TEST = ROOT / 'src/ops/tools/tests/test_github_app_readback.py'
PLAN = ROOT / 'docs/codex/github-app-check-smoke-test-plan.md'
PINS = {'app_id': 543219901, 'installation_id': 765432190,
        'owner': 'FixtureOwner', 'repository': 'FixtureOwner/demo', 'slug': 'fixture-smoke'}
PR, REPO_ID, CHECK_ID = 31, 4422991, 345679201
BASE, HEAD = 'a' * 40, 'b' * 40
KEY_PATH = '/fixture-smoke/not-an-app-key.pem'
JWT, TOKEN = 'fixture_app_jwt_not_a_credential', 'fixture_installation_token_not_a_credential'
PRIVATE = 'PRIVATE_SMOKE_/private/path?email=fixture@example.invalid'
NAME, TITLE = 'codex/emitter-smoke', 'Diagnostic smoke only'
SUMMARY = ('Intentional failure. This diagnostic does not approve required checks, independent '
           'review, policy, binding, runtime isolation, or merge.')
APP_PERMISSIONS = {'checks': 'write', 'contents': 'read', 'pull_requests': 'read', 'metadata': 'read'}
TOKEN_PERMISSIONS = {'checks': 'write', 'pull_requests': 'read', 'metadata': 'read'}
FALSE_FLAGS = {'input_origin_verified', 'independent_review_verified', 'binding_verified',
               'trusted_gate_verified', 'runtime_isolation_verified', 'native_ready', 'merge_authorized'}
BOOL_FIELDS = {'identity_metadata_verified', 'check_record_observed', 'pr_pins_rechecked',
               'token_revoked_verified', 'create_outcome_unconfirmed'} | FALSE_FLAGS
COUNTERS = {'metadata_get_attempts': 3, 'api_get_attempts': 6, 'token_post_attempts': 1,
            'check_post_attempts': 1, 'token_delete_attempts': 1}
FIELDS = {'scope', 'status', 'stage', 'reason', 'check_run_id'} | BOOL_FIELDS | set(COUNTERS)
STAGES = set(('ARGUMENTS KEY SIGN APP INSTALLATION REPOSITORY TOKEN TOKEN_SCOPE PR CHECK_LIST '
              'PR_RECHECK CHECK_CREATE CHECK_READBACK REVOKE COMPLETE INTERNAL').split())
REASONS = set(('OBSERVED ARGUMENTS_INVALID KEY_INVALID SIGNING_FAILED READ_NOT_ALLOWED '
               'HTTP_STATUS_INVALID HTTP_TRANSPORT_FAILED RESPONSE_INVALID APP_MISMATCH '
               'INSTALLATION_MISMATCH REPOSITORY_INSTALLATION_MISMATCH UNEXPECTED_FAILURE '
               'TOKEN_ISSUE_UNCONFIRMED TOKEN_SCOPE_INVALID PR_PINS_MISMATCH CHECK_LIST_INVALID '
               'CHECK_NOT_FOUND CHECK_CREATE_UNCONFIRMED CHECK_RECORD_MISMATCH '
               'TOKEN_REVOKE_UNCONFIRMED DEADLINE_EXCEEDED').split())
REAL_HTTPS = http.client.HTTPSConnection
DROP = object()


def external_id(pins=PINS, pr=PR, base=BASE, head=HEAD):
    value = {'repository': pins['repository'], 'pr_number': pr, 'base_sha': base,
             'head_sha': head, 'app_id': pins['app_id'], 'installation_id': pins['installation_id']}
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return 'agent-project-emitter-smoke-v1:' + hashlib.sha256(
        b'agent-project:github-app-check-smoke:v1\n' + raw).hexdigest()


def repo():
    return {'id': REPO_ID, 'full_name': PINS['repository'], 'name': 'demo'}


def pr_row():
    return {'number': PR, 'state': 'open', 'draft': True, 'merged': False,
            'base': {'sha': BASE, 'repo': repo()}, 'head': {'sha': HEAD, 'repo': repo()}}


def record():
    return {'id': CHECK_ID, 'name': NAME, 'app': {'id': PINS['app_id'], 'slug': PINS['slug']},
            'head_sha': HEAD, 'status': 'completed', 'conclusion': 'failure',
            'external_id': external_id(), 'output': {'title': TITLE, 'summary': SUMMARY}}


def future_expiry():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')


def cli(emit=False):
    result = ['--app-id', str(PINS['app_id']), '--installation-id', str(PINS['installation_id']),
              '--owner', PINS['owner'], '--repository', PINS['repository'], '--slug', PINS['slug'],
              '--private-key', KEY_PATH, '--pr-number', str(PR), '--base-sha', BASE, '--head-sha', HEAD]
    return result + (['--emit-once'] if emit else [])


def blocked_report():
    result = dict.fromkeys(FIELDS, False)
    result.update(scope='APP_CHECK_SMOKE_ONLY', status='BLOCKED', stage='ARGUMENTS',
                  reason='ARGUMENTS_INVALID', check_run_id=None)
    result.update({name: 0 for name in COUNTERS})
    return result


class ModuleProxy:
    def __init__(self, original, **overrides):
        self.original, self.overrides = original, overrides

    def __getattr__(self, name):
        return self.overrides[name] if name in self.overrides else getattr(self.original, name)


class FixtureServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.flow.handle(self, 'GET')

    def do_POST(self):
        self.server.flow.handle(self, 'POST')

    def do_DELETE(self):
        self.server.flow.handle(self, 'DELETE')


class Flow:
    """TLS responses are fixed fixture data, never reflections of POST payload."""
    def __init__(self, test, existing=False, overrides=None, trusted=True, redirect_target=False):
        self.test, self.overrides, self.trusted = test, overrides or {}, trusted
        self.records = [record()] if existing else []
        self.requests, self.connections, self.target_requests = [], [], []
        self.fds, self.closed_fds = [], []
        self.get_seen = False
        self.fd_open_at_network = False
        self.redirect_target = redirect_target
        self.stack = ExitStack()

    def _server(self, target=False):
        server = FixtureServer(('127.0.0.1', 0), Handler)
        server.flow = self
        server.is_target = target
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.test.cert), str(self.test.tls_key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.stack.callback(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
        return server

    def __enter__(self):
        self.server = self._server()
        if self.redirect_target:
            self.target = self._server(target=True)
            self.target_url = 'https://localhost:' + str(self.target.server_port) + '/must-not-receive-auth'

        def context_factory(*args, **kwargs):
            self.test.assertEqual((args, kwargs), ((), {}))
            return ssl.create_default_context(cafile=str(self.test.cert) if self.trusted else None)

        def connection_factory(host, port=None, timeout=None, context=None, **kwargs):
            self.test.assertEqual(host, 'api.github.com')
            self.test.assertIn(port, (None, 443))
            self.test.assertIs(context.check_hostname, True)
            self.test.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.test.assertIn(timeout, (5, 10))
            self.test.assertFalse(kwargs)
            self.connections.append((host, timeout))
            if any(fd not in self.closed_fds for fd in self.fds):
                self.fd_open_at_network = True
            return REAL_HTTPS('localhost', self.server.server_port, timeout=timeout, context=context)

        http_proxy = ModuleProxy(http.client, HTTPSConnection=connection_factory)
        ssl_proxy = ModuleProxy(ssl, create_default_context=context_factory)
        for module in (q, q.auth):
            self.stack.enter_context(patch.object(module, 'http', ModuleProxy(http, client=http_proxy)))
            self.stack.enter_context(patch.object(module, 'ssl', ssl_proxy))
        self.stack.enter_context(patch.object(q.auth, 'open_key_checked', side_effect=self.open_key))
        self.stack.enter_context(patch.object(q.auth, 'sign_jwt', side_effect=self.sign))

        def close(fd):
            if fd in self.fds:
                self.closed_fds.append(fd)
            return os.close(fd)
        for module in (q, q.auth):
            if hasattr(module, 'os'):
                self.stack.enter_context(patch.object(module, 'os', ModuleProxy(os, close=close)))
        return self

    def __exit__(self, *args):
        self.stack.close()
        for fd in self.fds:
            if fd not in self.closed_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def open_key(self, path):
        self.test.assertEqual(path, KEY_PATH)
        fd = os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC)
        self.fds.append(fd)
        return fd

    def sign(self, fd, app_id, *args, **kwargs):
        self.test.assertIn(fd, self.fds)
        self.test.assertEqual(app_id, PINS['app_id'])
        os.fstat(fd)
        return JWT

    def invoke(self, emit=False):
        return q.smoke(dict(PINS), KEY_PATH, PR, BASE, HEAD, emit_once=emit)

    def operation(self, method, path):
        if method == 'GET' and path == '/app':
            return 'APP'
        if method == 'GET' and path == '/app/installations/' + str(PINS['installation_id']):
            return 'INSTALLATION'
        if method == 'GET' and path == '/repos/' + PINS['repository'] + '/installation':
            return 'REPOSITORY'
        if method == 'POST' and path == '/app/installations/' + str(PINS['installation_id']) + '/access_tokens':
            return 'TOKEN'
        if method == 'GET' and path == '/installation/repositories?per_page=100&page=1':
            return 'REPOS'
        if method == 'GET' and path == '/repos/' + PINS['repository'] + '/pulls/' + str(PR):
            if self.get_seen:
                return 'PR_FINAL'
            return 'PR_PRECREATE' if any(row['op'] == 'PR_INITIAL' for row in self.requests) else 'PR_INITIAL'
        expected_list = ('/repos/' + PINS['repository'] + '/commits/' + HEAD
                         + '/check-runs?check_name=codex%2Femitter-smoke&filter=all&per_page=100&page=1&app_id='
                         + str(PINS['app_id']))
        if method == 'GET' and path == expected_list:
            return 'LIST'
        if method == 'POST' and path == '/repos/' + PINS['repository'] + '/check-runs':
            return 'CREATE'
        if method == 'GET' and path == '/repos/' + PINS['repository'] + '/check-runs/' + str(CHECK_ID):
            return 'GET_CHECK'
        if method == 'DELETE' and path == '/installation/token':
            return 'REVOKE'
        return 'UNEXPECTED'

    def default(self, op):
        if op == 'APP':
            return {'id': PINS['app_id'], 'slug': PINS['slug'],
                    'owner': {'login': PINS['owner'], 'type': 'User'},
                    'permissions': dict(APP_PERMISSIONS), 'events': []}
        if op in ('INSTALLATION', 'REPOSITORY'):
            return {'id': PINS['installation_id'], 'app_id': PINS['app_id'],
                    'account': {'login': PINS['owner'], 'type': 'User'},
                    'permissions': dict(APP_PERMISSIONS), 'repository_selection': 'selected',
                    'suspended_at': None}
        if op == 'TOKEN':
            return {'token': TOKEN, 'expires_at': future_expiry(), 'permissions': dict(TOKEN_PERMISSIONS),
                    'repository_selection': 'selected', 'repositories': [repo()]}
        if op == 'REPOS':
            return {'total_count': 1, 'repositories': [repo()]}
        if op.startswith('PR_'):
            return pr_row()
        if op == 'LIST':
            return {'total_count': len(self.records), 'check_runs': copy.deepcopy(self.records)}
        if op in ('CREATE', 'GET_CHECK'):
            return record()
        return {}

    def handle(self, handler, method):
        if handler.server.is_target:
            self.target_requests.append({'method': method, 'authorization': handler.headers.get('Authorization')})
            handler.send_response(200)
            handler.end_headers()
            return
        length = int(handler.headers.get('Content-Length', '0'))
        raw = handler.rfile.read(length) if length else b''
        body = json.loads(raw.decode()) if raw else None
        op = self.operation(method, handler.path)
        row = {'op': op, 'method': method, 'path': handler.path, 'body': body,
               'authorization': handler.headers.get('Authorization'),
               'accept': handler.headers.get('Accept'),
               'api_version': handler.headers.get('X-GitHub-Api-Version')}
        self.requests.append(row)
        if op == 'CREATE':
            self.records = [record()]
        if op == 'GET_CHECK':
            self.get_seen = True
        value = self.default(op)
        action = self.overrides.get(op, value)
        if callable(action):
            action = action(self, copy.deepcopy(value))
        if action is DROP:
            handler.close_connection = True
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            handler.connection.close()
            return
        status, headers = (201 if op in ('TOKEN', 'CREATE') else 204 if op == 'REVOKE' else 200), {}
        if isinstance(action, tuple):
            status, action, headers = action
        encoded = action if isinstance(action, bytes) else json.dumps(action, separators=(',', ':')).encode()
        handler.send_response(status)
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.send_header('Content-Length', str(len(encoded) if status != 204 else 0))
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        if status != 204:
            try:
                handler.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def ops(self):
        return [row['op'] for row in self.requests]


def changed(path, value):
    def change(flow, row):
        target = row
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        return row
    return change


class CheckSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='check-smoke-public-tls-')
        folder = Path(cls.temporary.name)
        cls.cert, cls.tls_key = folder / 'canary-cert.pem', folder / 'canary-tls-key.pem'
        result = subprocess.run(
            ['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
             '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
             '-keyout', str(cls.tls_key), '-out', str(cls.cert)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        if result.returncode != 0:
            cls.temporary.cleanup()
            raise RuntimeError('local TLS canary prerequisite unavailable')

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def report(self, report, observed):
        self.assertEqual(set(report), FIELDS)
        self.assertEqual(report['scope'], 'APP_CHECK_SMOKE_ONLY')
        self.assertEqual(report['status'], 'OBSERVED' if observed else 'BLOCKED')
        self.assertIn(report['stage'], STAGES)
        self.assertIn(report['reason'], REASONS)
        for name in BOOL_FIELDS:
            self.assertIs(type(report[name]), bool, name)
        for name, limit in COUNTERS.items():
            self.assertIs(type(report[name]), int, name)
            self.assertLessEqual(0, report[name])
            self.assertLessEqual(report[name], limit)
        identifier = report['check_run_id']
        self.assertTrue(identifier is None or type(identifier) is int and 1 <= identifier <= (1 << 63) - 1)
        for name in FALSE_FLAGS:
            self.assertIs(report[name], False, name)
        if observed:
            self.assertEqual((report['stage'], report['reason']), ('COMPLETE', 'OBSERVED'))
            for name in ('identity_metadata_verified', 'check_record_observed', 'pr_pins_rechecked', 'token_revoked_verified'):
                self.assertIs(report[name], True, name)
            self.assertEqual(identifier, CHECK_ID)
            self.assertIs(report['create_outcome_unconfirmed'], False)
        rendered = json.dumps(report)
        for private in (KEY_PATH, JWT, TOKEN, PRIVATE, PINS['owner'], PINS['repository'], PINS['slug'],
                        BASE, HEAD, external_id(), str(PINS['app_id']), str(PINS['installation_id']), 'Traceback'):
            self.assertNotIn(private, rendered)

    def checked(self, flow, emit=False, observed=True):
        result = flow.invoke(emit)
        self.report(result, observed)
        self.assertFalse(flow.fd_open_at_network)
        self.assertEqual(flow.closed_fds, flow.fds)
        self.assertNotIn('UNEXPECTED', flow.ops())
        return result

    def test_01_api_cli_strict_pins_tokens_and_help_reject_before_io(self):
        expected_parameters = ('pins', 'key_path', 'pr_number', 'base_sha', 'head_sha', 'emit_once')
        self.assertEqual(tuple(inspect.signature(q.smoke).parameters), expected_parameters)
        for arguments in (cli(), cli(True), list(reversed([cli()[i:i + 2] for i in range(0, 18, 2)]))):
            if arguments and isinstance(arguments[0], list):
                arguments = [item for pair in arguments for item in pair]
            with patch.object(q, 'smoke', return_value=blocked_report()) as invoke, redirect_stderr(io.StringIO()) as stderr:
                out = io.StringIO()
                self.assertEqual(q.main(arguments, out), 2)
                self.report(json.loads(out.getvalue()), False)
                self.assertEqual(invoke.call_count, 1)
                self.assertEqual(stderr.getvalue(), '')
        invalid = [[], ['--help'], cli()[:-1], cli() + ['--emit-once', '--emit-once'],
                   cli() + ['--unknown-' + PRIVATE], ['--app-id=' + str(PINS['app_id'])] + cli()[2:],
                   ['--app', str(PINS['app_id'])] + cli()[2:], cli() + ['--pr-number', str(PR)]]
        for arguments in invalid:
            with patch.object(q.auth, 'open_key_checked') as key, redirect_stderr(io.StringIO()) as stderr:
                out = io.StringIO()
                self.assertEqual(q.main(arguments, out), 2)
                self.report(json.loads(out.getvalue()), False)
                key.assert_not_called()
                self.assertEqual(stderr.getvalue(), '')
        variants = [(dict(PINS, app_id=True), PR, BASE, HEAD, False),
                    (dict(PINS, repository='Other/demo'), PR, BASE, HEAD, False),
                    (dict(PINS, extra=True), PR, BASE, HEAD, False),
                    (PINS, True, BASE, HEAD, False), (PINS, 0, BASE, HEAD, False),
                    (PINS, 1 << 63, BASE, HEAD, False), (PINS, PR, BASE.upper(), HEAD, False),
                    (PINS, PR, BASE[:-1], HEAD, False), (PINS, PR, BASE, HEAD, 1)]
        for pins, pr, base, head, emit in variants:
            with patch.object(q.auth, 'open_key_checked') as key:
                self.report(q.smoke(pins, KEY_PATH, pr, base, head, emit), False)
                key.assert_not_called()

    def test_02_reused_key_sign_boundary_closes_before_network_and_failures_stop(self):
        for helper, error in (('open_key_checked', q.auth.ProbeError('KEY', 'KEY_INVALID')),
                              ('sign_jwt', q.auth.ProbeError('SIGN', 'SIGNING_FAILED'))):
            with Flow(self) as flow, patch.object(q.auth, helper, side_effect=error):
                result = flow.invoke(True)
                self.report(result, False)
                self.assertEqual(flow.requests, [])
                self.assertEqual(flow.closed_fds, flow.fds)
        with Flow(self) as flow:
            self.checked(flow, observed=False)

    def test_03_metadata_three_gets_mismatch_stops_before_token(self):
        for operation, override, expected_count in (
                ('APP', changed(['id'], PINS['app_id'] + 1), 1),
                ('INSTALLATION', changed(['suspended_at'], '2026-01-01T00:00:00Z'), 2),
                ('REPOSITORY', changed(['permissions'], dict(APP_PERMISSIONS, administration='write')), 3)):
            with self.subTest(operation=operation), Flow(self, overrides={operation: override}) as flow:
                result = self.checked(flow, True, False)
                self.assertEqual(result['metadata_get_attempts'], expected_count)
                self.assertIs(result['identity_metadata_verified'], False)
                self.assertEqual(result['token_post_attempts'], 0)
                self.assertEqual(len(flow.requests), expected_count)

    def test_04_token_issue_scope_body_and_expiry_are_exact(self):
        with Flow(self) as flow:
            self.checked(flow, observed=False)
            issue = next(row for row in flow.requests if row['op'] == 'TOKEN')
            self.assertEqual(issue['body'], {'repositories': ['demo'], 'permissions': TOKEN_PERMISSIONS})
            self.assertEqual(issue['authorization'], 'Bearer ' + JWT)
        bad = [(['token'], ''), (['token'], 'bad\n' + PRIVATE), (['token'], 'x' * 4097),
               (['expires_at'], '2000-01-01T00:00:00Z'), (['expires_at'], '2099-01-01T00:00:00+00:00'),
               (['permissions'], dict(TOKEN_PERMISSIONS, contents='read')),
               (['permissions'], {'checks': 'write'}), (['repository_selection'], 'all'),
               (['repositories'], [dict(repo(), full_name='Other/demo')]),
               (['repositories'], [repo(), dict(repo(), id=REPO_ID + 1)]),
               (['repositories', 0, 'id'], True)]
        for path, value in bad:
            with self.subTest(path=path), Flow(self, overrides={'TOKEN': changed(path, value)}) as flow:
                result = self.checked(flow, True, False)
                self.assertEqual(result['check_post_attempts'], 0)
                self.assertNotIn('REPOS', flow.ops())

    def test_05_token_repository_list_has_exact_single_scope_and_no_pagination(self):
        for override in (changed(['total_count'], True), changed(['total_count'], 2),
                         changed(['repositories'], []), changed(['repositories', 0, 'id'], REPO_ID + 1),
                         changed(['repositories', 0, 'full_name'], 'Other/demo'),
                         (200, {'total_count': 1, 'repositories': [repo()]}, {'Link': '<https://example.invalid/next>; rel="next"'})):
            with Flow(self, overrides={'REPOS': override}) as flow:
                result = self.checked(flow, True, False)
                self.assertNotIn('PR_INITIAL', flow.ops())
                self.assertEqual(result['token_delete_attempts'], 1)

    def test_06_initial_pull_request_strict_identity_and_draft_state(self):
        invalid = [(['number'], True), (['number'], PR + 1), (['state'], 'closed'),
                   (['draft'], False), (['draft'], 1), (['merged'], True), (['merged'], 0),
                   (['base', 'sha'], 'c' * 40), (['head', 'sha'], 'c' * 40),
                   (['base', 'repo', 'id'], REPO_ID + 1),
                   (['head', 'repo', 'full_name'], 'Other/demo')]
        for path, value in invalid:
            with self.subTest(path=path), Flow(self, overrides={'PR_INITIAL': changed(path, value)}) as flow:
                result = self.checked(flow, True, False)
                self.assertEqual(result['check_post_attempts'], 0)
                self.assertNotIn('LIST', flow.ops())

    def test_07_external_id_independent_vector_and_all_six_fields(self):
        pins = dict(PINS, repository='Example/demo', app_id=17, installation_id=29)
        vector = ('agent-project-emitter-smoke-v1:'
                  '7a1a37f6fba06620f43a7d6f7d6ee7eb1a50d01e0f6558fc6bcc5273312ec581')
        self.assertEqual(q._external_id(pins, 31, BASE, HEAD), vector)
        alternatives = [(dict(pins, repository='Example/other'), 31, BASE, HEAD),
                        (dict(pins, app_id=18), 31, BASE, HEAD),
                        (dict(pins, installation_id=30), 31, BASE, HEAD),
                        (pins, 32, BASE, HEAD), (pins, 31, 'c' * 40, HEAD),
                        (pins, 31, BASE, 'c' * 40)]
        for values in alternatives:
            self.assertNotEqual(q._external_id(*values), vector)
        self.assertEqual(q._external_id(pins, 31, BASE, HEAD), vector)

    def test_08_invalid_existing_records_are_blocked_without_new_post(self):
        rows = []
        for path, value in ((['app', 'id'], PINS['app_id'] + 1), (['head_sha'], 'c' * 40),
                            (['external_id'], 'other'), (['conclusion'], 'success'), (['id'], True)):
            row = record()
            changed(path, value)(None, row)
            rows.append({'total_count': 1, 'check_runs': [row]})
        rows.extend([{'total_count': 2, 'check_runs': [record(), record()]},
                     {'total_count': True, 'check_runs': [record()]},
                     {'total_count': 0, 'check_runs': [record()]},
                     {'total_count': 101, 'check_runs': [record()] * 101}])
        for value in rows:
            with Flow(self, overrides={'LIST': value}) as flow:
                result = self.checked(flow, True, False)
                self.assertEqual(result['check_post_attempts'], 0)
        with Flow(self, overrides={'LIST': (200, {'total_count': 0, 'check_runs': []},
                                           {'Link': '<https://example.invalid/next>; rel="next"'})}) as flow:
            self.checked(flow, True, False)
            self.assertNotIn('CREATE', flow.ops())

    def test_09_default_lookup_can_issue_and_revoke_token_but_never_create(self):
        with Flow(self) as flow:
            result = self.checked(flow, observed=False)
            self.assertEqual(result['reason'], 'CHECK_NOT_FOUND')
            self.assertEqual((result['token_post_attempts'], result['check_post_attempts'], result['token_delete_attempts']), (1, 0, 1))
            self.assertIs(result['token_revoked_verified'], True)
            self.assertIs(result['create_outcome_unconfirmed'], False)
            self.assertEqual(flow.ops(), ['APP', 'INSTALLATION', 'REPOSITORY', 'TOKEN', 'REPOS', 'PR_INITIAL', 'LIST', 'REVOKE'])

    def test_10_existing_record_is_read_back_and_pr_rechecked_without_create(self):
        with Flow(self, existing=True) as flow:
            result = self.checked(flow)
            self.assertEqual(result['check_post_attempts'], 0)
            self.assertEqual(result['api_get_attempts'], 5)
            self.assertEqual(flow.ops()[-3:], ['GET_CHECK', 'PR_FINAL', 'REVOKE'])
        with Flow(self, existing=True, overrides={'GET_CHECK': (500, {}, {})}) as flow:
            result = self.checked(flow, observed=False)
            self.assertEqual(result['check_run_id'], CHECK_ID)
            self.assertIs(result['check_record_observed'], False)
            self.assertNotIn('CREATE', flow.ops())

    def test_11_real_tls_emit_sends_exact_completed_failure_payload(self):
        # D38 M1: independent response stays normal; assert actual received body.
        with Flow(self) as flow:
            result = self.checked(flow, True)
            create = next(row for row in flow.requests if row['op'] == 'CREATE')
            body = dict(create['body'])
            completed_at = body.pop('completed_at')
            self.assertRegex(completed_at, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
            self.assertEqual(body, {'name': NAME, 'head_sha': HEAD, 'status': 'completed',
                                    'conclusion': 'failure', 'external_id': external_id(),
                                    'output': {'title': TITLE, 'summary': SUMMARY}})
            self.assertEqual(flow.ops(), ['APP', 'INSTALLATION', 'REPOSITORY', 'TOKEN', 'REPOS',
                                         'PR_INITIAL', 'LIST', 'PR_PRECREATE', 'CREATE', 'GET_CHECK', 'PR_FINAL', 'REVOKE'])
            self.assertEqual(result['api_get_attempts'], 6)
            self.assertEqual(result['check_post_attempts'], 1)

    def test_12_readback_wrong_app_and_other_record_pins_are_rejected(self):
        # D38 M2: only GET app.id differs; all token/PR/create/other record pins valid.
        with Flow(self, overrides={'GET_CHECK': changed(['app', 'id'], PINS['app_id'] + 1)}) as flow:
            result = flow.invoke(True)
            self.report(result, False)
            self.assertIs(result['check_record_observed'], False)
            self.assertEqual(result['check_run_id'], CHECK_ID)
            self.assertEqual(result['token_delete_attempts'], 1)
        for path, value in ((['id'], CHECK_ID + 1), (['app', 'slug'], 'other'),
                            (['head_sha'], 'c' * 40), (['name'], 'codex/required-tests'),
                            (['external_id'], 'other'), (['status'], 'in_progress'),
                            (['conclusion'], 'success'), (['output', 'title'], 'other'),
                            (['output', 'summary'], PRIVATE)):
            with self.subTest(path=path), Flow(self, overrides={'GET_CHECK': changed(path, value)}) as flow:
                self.checked(flow, True, False)

    def test_13_pull_request_moves_before_or_after_create_fail_closed(self):
        for op in ('PR_PRECREATE', 'PR_FINAL'):
            with Flow(self, overrides={op: changed(['head', 'sha'], 'c' * 40)}) as flow:
                result = self.checked(flow, True, False)
                self.assertEqual(result['check_post_attempts'], 0 if op == 'PR_PRECREATE' else 1)
                self.assertIs(result['pr_pins_rechecked'], False)
                self.assertIs(result['token_revoked_verified'], True)

    def test_14_create_response_loss_preserves_ambiguity_and_lookup_does_not_repost(self):
        with Flow(self, overrides={'CREATE': DROP}) as flow:
            result = self.checked(flow, True, False)
            self.assertEqual(result['check_post_attempts'], 1)
            self.assertIs(result['create_outcome_unconfirmed'], True)
            self.assertEqual(Counter(flow.ops())['CREATE'], 1)
            self.assertEqual(len(flow.records), 1)
            self.assertNotIn('GET_CHECK', flow.ops())
            flow.overrides.clear()
            flow.requests.clear()
            flow.get_seen = False
            self.checked(flow)
            self.assertNotIn('CREATE', flow.ops())
        for value in (b'{"id":', dict(record(), id=True), (403, {'private': PRIVATE}, {})):
            with Flow(self, overrides={'CREATE': value}) as flow:
                result = self.checked(flow, True, False)
                self.assertEqual(result['check_post_attempts'], 1)
                self.assertIs(result['create_outcome_unconfirmed'], True)

    def test_15_token_cleanup_is_once_and_never_overwrites_original_failure(self):
        with Flow(self, overrides={'TOKEN': changed(['permissions'], dict(TOKEN_PERMISSIONS, contents='read'))}) as flow:
            result = self.checked(flow, True, False)
            self.assertEqual(result['token_delete_attempts'], 1)
            self.assertIs(result['token_revoked_verified'], True)
        with Flow(self, overrides={'REVOKE': (500, {}, {})}) as flow:
            result = self.checked(flow, True, False)
            self.assertEqual((result['stage'], result['reason']), ('REVOKE', 'TOKEN_REVOKE_UNCONFIRMED'))
            self.assertIs(result['token_revoked_verified'], False)
            self.assertIs(result['check_record_observed'], True)
        with Flow(self, overrides={'REVOKE': (200, {}, {})}) as flow:
            result = self.checked(flow, observed=False)
            self.assertEqual(result['reason'], 'CHECK_NOT_FOUND')
            self.assertIs(result['token_revoked_verified'], False)
        with Flow(self, overrides={'TOKEN': DROP}) as flow:
            result = self.checked(flow, True, False)
            self.assertEqual(result['token_post_attempts'], 1)
            self.assertEqual(result['token_delete_attempts'], 0)
            self.assertNotIn('REPOS', flow.ops())
        clock = [100.0]
        def expires_work_deadline(flow, row):
            clock[0] = 171.0
            return row
        with Flow(self, overrides={'TOKEN': expires_work_deadline}) as flow, patch.object(
                q, 'time', ModuleProxy(time, monotonic=lambda: clock[0])):
            result = self.checked(flow, True, False)
            self.assertEqual(result['reason'], 'DEADLINE_EXCEEDED')
            self.assertEqual(result['api_get_attempts'], 0)
            self.assertEqual(result['token_delete_attempts'], 1)
            self.assertIs(result['token_revoked_verified'], True)

    def test_16_http_tls_redirect_and_strict_response_limits(self):
        for status in (401, 403, 404, 500):
            with Flow(self, overrides={'REPOS': (status, {'private': PRIVATE}, {})}) as flow:
                result = self.checked(flow, True, False)
                self.assertNotIn('PR_INITIAL', flow.ops())
                self.assertEqual(result['token_delete_attempts'], 1)
        with Flow(self, redirect_target=True) as flow:
            flow.overrides['REPOS'] = (302, flow.default('REPOS'), {'Location': flow.target_url})
            self.checked(flow, True, False)
            self.assertEqual(flow.target_requests, [])
        with Flow(self, trusted=False) as flow:
            self.checked(flow, True, False)
            self.assertEqual(flow.requests, [])
        with Flow(self) as flow:
            original_factory = q.http.client.HTTPSConnection
            def timeout_factory(*args, **kwargs):
                connection = original_factory(*args, **kwargs)
                class TimeoutConnection:
                    def request(self, method, path, *args, **kwargs):
                        if path.startswith('/installation/repositories?'):
                            raise socket.timeout(PRIVATE)
                        return connection.request(method, path, *args, **kwargs)
                    def __getattr__(self, name):
                        return getattr(connection, name)
                return TimeoutConnection()
            with patch.object(q, 'http', ModuleProxy(http, client=ModuleProxy(
                    http.client, HTTPSConnection=timeout_factory))):
                result = self.checked(flow, True, False)
                self.assertEqual(result['api_get_attempts'], 1)
                self.assertEqual(result['token_delete_attempts'], 1)
        normal = json.dumps({'total_count': 1, 'repositories': [repo()]}).encode()
        deep = {}
        for _ in range(33):
            deep = {'nested': deep}
        invalid = [normal.ljust(128 * 1024 + 1, b' '), b'\xff', b'{',
                   b'{"total_count":1,"total_count":1,"repositories":[]}',
                   normal[:-1] + b',"ignored":NaN}', normal[:-1] + b',"ignored":Infinity}',
                   normal[:-1] + b',"ignored":123456789012345678901}',
                   json.dumps(dict(json.loads(normal), ignored=deep)).encode()]
        for raw in invalid:
            with Flow(self, overrides={'REPOS': raw}) as flow:
                self.checked(flow, True, False)
                self.assertNotIn('PR_INITIAL', flow.ops())
        with Flow(self, overrides={'REPOS': normal.ljust(128 * 1024, b' ')}) as flow:
            result = self.checked(flow, observed=False)
            self.assertEqual(result['reason'], 'CHECK_NOT_FOUND')
            self.assertIn('PR_INITIAL', flow.ops())
            self.assertIn('LIST', flow.ops())

    def test_17_fixed_operations_authorization_counts_and_proxy_environment(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1'}), Flow(self) as flow:
            result = self.checked(flow, True)
            for row in flow.requests:
                expected = JWT if row['op'] in ('APP', 'INSTALLATION', 'REPOSITORY', 'TOKEN') else TOKEN
                self.assertEqual(row['authorization'], 'Bearer ' + expected)
                self.assertEqual(row['accept'], 'application/vnd.github+json')
                self.assertEqual(row['api_version'], '2026-03-10')
            self.assertEqual(Counter(row['method'] for row in flow.requests), {'GET': 9, 'POST': 2, 'DELETE': 1})
            self.assertEqual(sum(1 for _, timeout in flow.connections if timeout == 5), 9)
            self.assertEqual(result['metadata_get_attempts'], 3)
        with Flow(self) as flow:
            client = q._Client(JWT, dict(PINS), PR, HEAD, external_id(), True, time.monotonic())
            for operation in ('REPOS', 'CREATE', 'GET_CHECK', 'REVOKE', PRIVATE, 'GET /app'):
                with self.assertRaises(q.auth.ProbeError):
                    client.call(operation)
            self.assertEqual(flow.requests, [])
            client.call('TOKEN')
            client.set_token(TOKEN)
            with self.assertRaises(q.auth.ProbeError):
                client.call('TOKEN')
            client.call('REPOS')
            with self.assertRaises(q.auth.ProbeError):
                client.call('REPOS')
            with self.assertRaises(TypeError):
                client.call('CREATE', {'conclusion': 'success'})
            client.call('REVOKE')
            with self.assertRaises(q.auth.ProbeError):
                client.call('REVOKE')
            self.assertEqual(flow.ops(), ['TOKEN', 'REPOS', 'REVOKE'])

    def test_18_output_privacy_unknown_errors_and_broken_stdout_are_sanitized(self):
        for arguments in (cli(), cli(True)):
            with Flow(self) as flow, redirect_stderr(io.StringIO()) as stderr:
                output = io.StringIO()
                self.assertEqual(q.main(arguments, output), 2)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.report(json.loads(output.getvalue()), '--emit-once' in arguments)
                self.assertEqual(stderr.getvalue(), '')
        with patch.object(q.auth, 'open_key_checked', side_effect=q.auth.ProbeError(PRIVATE, PRIVATE)):
            self.report(q.smoke(PINS, KEY_PATH, PR, BASE, HEAD), False)
        class Broken:
            def write(self, value):
                raise OSError(PRIVATE)
            def flush(self):
                raise ValueError(PRIVATE)
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(q.main(['--help'], Broken()), 2)
            self.assertEqual(stderr.getvalue(), '')
        class FlushFailure(io.StringIO):
            flushed = False
            def flush(self):
                self.flushed = True
                raise OSError(PRIVATE)
        output = FlushFailure()
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(q.main(['--help'], output), 2)
            self.assertIs(output.flushed, True)
            self.report(json.loads(output.getvalue()), False)
            self.assertEqual(stderr.getvalue(), '')
        process = subprocess.Popen([sys.executable, '-B', '-m', 'src.ops.tools.github_app_check_smoke', '--help'],
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        stderr = process.stderr.read()
        process.stderr.close()
        self.assertEqual(process.wait(timeout=5), 2)
        self.assertEqual(stderr, b'')

    def test_19_python310_imports_auth_preservation_and_two_file_isolated_launcher(self):
        source = SOURCE.read_text()
        tree = ast.parse(source, feature_version=(3, 10))
        ast.parse(Path(__file__).read_text(), feature_version=(3, 10))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split('.')[0], sys.stdlib_module_names)
            elif isinstance(node, ast.ImportFrom):
                self.assertTrue(node.module == 'src.ops.tools' or node.module.split('.')[0] in sys.stdlib_module_names)
        self.assertEqual(hashlib.sha256(AUTH_SOURCE.read_bytes()).hexdigest(), '97e8fdd2675a6d08c14d9f70da06dfb360165002bf99ec93f92ebac2c1f7f1b3')
        self.assertEqual(hashlib.sha256(AUTH_TEST.read_bytes()).hexdigest(), 'c2ac4580c6b9dc0d5ecdeceb44a6e138795b0e920811236270d736ac4b23551b')
        self.assertEqual(hashlib.sha256(PLAN.read_bytes()).hexdigest(), 'd156352ba97647e705421879a42c77663b1c63c35839f9c8662e52dec1caaee0')
        with tempfile.TemporaryDirectory(prefix='check-smoke-thin-package-') as temporary:
            folder = Path(temporary)
            for relative in ('src', 'src/ops', 'src/ops/tools'):
                package = folder / relative
                package.mkdir(parents=True, exist_ok=True)
                (package / '__init__.py').write_bytes(b'')
            for path in (SOURCE, AUTH_SOURCE):
                target = folder / 'src/ops/tools' / path.name
                shutil.copyfile(path, target)
                self.assertEqual(target.read_bytes(), path.read_bytes())
            launcher = ('import runpy,sys;sys.path.insert(0,sys.argv.pop(1));'
                        'runpy.run_module("src.ops.tools.github_app_check_smoke",run_name="__main__",alter_sys=True)')
            result = subprocess.run([sys.executable, '-I', '-B', '-c', launcher, str(folder), '--help'],
                                    cwd=temporary, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, b'')
            self.report(json.loads(result.stdout), False)
            self.assertEqual(list(folder.rglob('__pycache__')), [])

    def test_20_extra_untrusted_claims_never_promote_trust_or_enable_success(self):
        def untrusted(flow, row):
            row.update({'trusted_gate_verified': True, 'merge_authorized': True,
                        'binding_verified': True, 'author_PASS': PRIVATE, 'url': 'https://example.invalid/' + PRIVATE})
            return row
        overrides = {op: untrusted for op in ('APP', 'INSTALLATION', 'REPOSITORY', 'TOKEN', 'REPOS',
                                             'PR_INITIAL', 'PR_PRECREATE', 'CREATE', 'GET_CHECK', 'PR_FINAL')}
        with Flow(self, overrides=overrides) as flow:
            result = self.checked(flow, True)
            self.assertEqual(set(result), FIELDS)
            self.assertTrue(all(result[name] is False for name in FALSE_FLAGS))
            self.assertEqual(result['status'], 'OBSERVED')


if __name__ == '__main__':
    unittest.main()
