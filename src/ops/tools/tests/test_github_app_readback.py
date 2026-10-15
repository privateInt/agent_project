"""Isolated App metadata reads: fixture RSA/TLS only, never a native App."""
import ast
import base64
import copy
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.ops.tools import github_app_readback as q


PROJECT = Path(__file__).resolve().parents[4]
SOURCE = PROJECT / 'src/ops/tools/github_app_readback.py'
PLAN = PROJECT / 'docs/codex/github-app-readback-test-plan.md'
PLAN_SHA = '03e95cbc819d822809a1f2c644f2ab9f9a6acea8348175603989ef60bb4c0232'
APP_ID, INSTALL_ID = 12345, 67890
OWNER, REPO, SLUG = 'fixture-owner', 'fixture-owner/fixture_repo', 'fixture-checks'
PRIVATE = 'PRIVATE_READBACK_/secret/token?email=private@example.invalid'
DUMMY_JWT = 'fixture.header.signature'
PERMISSIONS = {'checks': 'write', 'contents': 'read', 'pull_requests': 'read', 'metadata': 'read'}
PATHS = ('/app', '/app/installations/' + str(INSTALL_ID), '/repos/' + REPO + '/installation')
FALSE_FLAGS = {'selected_repository_list_verified', 'input_origin_verified', 'trusted_gate_verified',
               'binding_verified', 'native_ready', 'merge_authorized', 'independent_review_verified',
               'ui_observations_are_authenticated_evidence'}
FIELDS = {'scope', 'status', 'stage', 'reason', 'get_attempts', 'identity_metadata_verified'} | FALSE_FLAGS
STAGES = {'ARGUMENTS', 'KEY', 'SIGN', 'APP', 'INSTALLATION', 'REPOSITORY', 'COMPLETE', 'INTERNAL'}
REASONS = {'OBSERVED', 'ARGUMENTS_INVALID', 'KEY_INVALID', 'SIGNING_FAILED', 'READ_NOT_ALLOWED',
           'HTTP_STATUS_INVALID', 'HTTP_TRANSPORT_FAILED', 'RESPONSE_INVALID', 'APP_MISMATCH',
           'INSTALLATION_MISMATCH', 'REPOSITORY_INSTALLATION_MISMATCH', 'UNEXPECTED_FAILURE'}
REAL_HTTPS = http.client.HTTPSConnection
REAL_RUN = subprocess.run


def encode(value):
    return json.dumps(value, separators=(',', ':')).encode('utf-8')


def fixture_rows():
    app = {'id': APP_ID, 'owner': {'login': OWNER, 'type': 'User'}, 'slug': SLUG,
           'permissions': dict(PERMISSIONS), 'events': [], 'unknown': PRIVATE}
    installation = {'id': INSTALL_ID, 'app_id': APP_ID,
                    'account': {'login': OWNER, 'type': 'User'},
                    'permissions': dict(PERMISSIONS), 'repository_selection': 'selected',
                    'suspended_at': None, 'unknown': PRIVATE}
    return {PATHS[0]: app, PATHS[1]: installation, PATHS[2]: copy.deepcopy(installation)}


def openssl_fixture(arguments, data=None):
    result = REAL_RUN(['/usr/bin/openssl'] + arguments, input=data, stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, timeout=8, check=False,
                      env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                           'OPENSSL_CONF': '/dev/null'})
    if result.returncode:
        raise AssertionError('fixture OpenSSL failed')
    return result.stdout


def decode_part(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def verify_fixture_jwt(token, public_key, directory):
    parts = token.split('.')
    if len(parts) != 3:
        raise AssertionError('fixture JWT shape')
    signature = Path(directory) / 'fixture-signature.bin'
    signature.write_bytes(decode_part(parts[2]))
    openssl_fixture(['dgst', '-sha256', '-verify', str(public_key), '-signature', str(signature)],
                    (parts[0] + '.' + parts[1]).encode('ascii'))
    return json.loads(decode_part(parts[0])), json.loads(decode_part(parts[1]))


@contextmanager
def connection_patch(factory=None):
    replacement = Mock(side_effect=factory)
    proxy = SimpleNamespace(client=SimpleNamespace(HTTPSConnection=replacement))
    with patch.object(q, 'http', proxy):
        yield replacement


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]


class FakeTransport:
    def __init__(self, rows=None, replacements=None):
        rows = fixture_rows() if rows is None else rows
        self.responses = {path: FakeResponse(200, encode(row)) for path, row in rows.items()}
        self.responses.update(replacements or {})
        self.connections, self.requests, self.factories = [], [], []

    def factory(self, host, port=None, *, timeout=None, context=None):
        transport = self
        self.factories.append((host, port, timeout, context.check_hostname, context.verify_mode))

        class Connection:
            closed = False
            path = None

            def request(self, method, path, body=None, headers=None):
                self.path = path
                transport.requests.append((method, path, body, headers))

            def getresponse(self):
                response = transport.responses[self.path]
                if isinstance(response, Exception):
                    raise response
                return response

            def close(self):
                self.closed = True

        connection = Connection()
        self.connections.append(connection)
        return connection


class TLSFixture:
    def __init__(self, cert, key, routes):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.command, self.path, self.headers.get('Authorization')))
                status, body, headers = routes.get(self.path, (404, b'', {}))
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass

            def log_message(self, *arguments):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *arguments):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError('fixture TLS server not stopped')


class ReadbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='app-readback-fixtures-')
        cls.folder = Path(cls.temporary.name)
        cls.key, cls.public = cls.folder / 'fixture-key.pem', cls.folder / 'fixture-public.pem'
        cls.cert, cls.ec = cls.folder / 'fixture-cert.pem', cls.folder / 'fixture-ec.pem'
        cls.encrypted = cls.folder / 'fixture-encrypted.pem'
        openssl_fixture(['genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(cls.key)])
        openssl_fixture(['pkey', '-in', str(cls.key), '-pubout', '-out', str(cls.public)])
        openssl_fixture(['req', '-new', '-x509', '-key', str(cls.key), '-out', str(cls.cert),
                         '-days', '1', '-subj', '/CN=localhost', '-addext',
                         'subjectAltName=DNS:localhost,IP:127.0.0.1'])
        openssl_fixture(['genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256', '-out', str(cls.ec)])
        openssl_fixture(['pkcs8', '-topk8', '-in', str(cls.key), '-out', str(cls.encrypted),
                         '-passout', 'pass:fixture-only-not-a-secret'])
        for path in (cls.key, cls.ec, cls.encrypted):
            path.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='case-', dir=self.folder)
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.pins = q.pins_checked(APP_ID, INSTALL_ID, OWNER, REPO, SLUG)

    def report(self, report, observed, attempts=None):
        self.assertEqual(set(report), FIELDS)
        self.assertEqual(report['scope'], 'AUTHENTICATED_APP_METADATA_ONLY')
        self.assertEqual(report['status'], 'OBSERVED' if observed else 'BLOCKED')
        self.assertIs(report['identity_metadata_verified'], observed)
        self.assertIn(report['stage'], STAGES)
        self.assertIn(report['reason'], REASONS)
        self.assertIs(type(report['get_attempts']), int)
        self.assertIn(report['get_attempts'], range(4))
        if attempts is not None:
            self.assertEqual(report['get_attempts'], attempts)
        for name in FALSE_FLAGS:
            self.assertIs(report[name], False, name)
        rendered = json.dumps(report)
        for value in (PRIVATE, '/secret/', 'private@example.invalid', OWNER, REPO, SLUG,
                      str(APP_ID), str(INSTALL_ID), str(self.key), DUMMY_JWT, 'Traceback'):
            self.assertNotIn(value, rendered)

    def argv(self):
        return ['--app-id', str(APP_ID), '--installation-id', str(INSTALL_ID), '--owner', OWNER,
                '--repository', REPO, '--slug', SLUG, '--private-key', str(self.key)]

    def fake_probe(self, rows=None, replacements=None):
        transport = FakeTransport(rows, replacements)
        with connection_patch(transport.factory), \
             patch.object(q, 'sign_jwt', return_value=DUMMY_JWT):
            report = q.probe(self.pins, str(self.key))
        return report, transport

    @contextmanager
    def tls_mapping(self, server, sink=None, trusted=True):
        seen = []
        fixture_context = ssl.create_default_context(cafile=str(self.cert))

        def factory(host, port=None, *, timeout=None, context=None):
            seen.append((host, port, timeout, context.check_hostname, context.verify_mode))
            if host == 'api.github.com':
                destination = server.port
            elif sink is not None and host == 'localhost' and port == sink.port:
                destination = sink.port
            else:
                raise AssertionError('unapproved fixture destination')
            return REAL_HTTPS('localhost', destination, timeout=timeout,
                              context=fixture_context if trusted else context)

        with connection_patch(factory):
            yield seen

    def test_01_strict_pins_cli_help_and_invalid_args_never_sign_or_connect(self):
        self.assertEqual(set(self.pins), {'app_id', 'installation_id', 'owner', 'repository', 'slug'})
        for field, value in (('app_id', True), ('installation_id', 0), ('app_id', 2**63),
                             ('owner', '한글'), ('repository', 'other/repo'),
                             ('repository', OWNER + '/..'), ('repository', REPO + '/extra'),
                             ('slug', 'UPPER'), ('repository', REPO + '?x=1')):
            pins = {**self.pins, field: value}
            with self.subTest(field=field), \
                 patch.object(q, 'sign_jwt') as signer, \
                 connection_patch() as network:
                self.report(q.probe(pins, str(self.key)), False, 0)
                signer.assert_not_called()
                network.assert_not_called()
        cases = [[], ['--help'], ['--' + PRIVATE], self.argv()[:-1]]
        for number in ('0', '01', '+1', '1e3', '1' * 10000):
            arguments = self.argv()
            arguments[1] = number
            cases.append(arguments)
        for arguments in cases:
            with self.subTest(size=len(arguments)), redirect_stderr(io.StringIO()) as stderr, \
                 patch.object(q, 'sign_jwt') as signer, \
                 connection_patch() as network:
                output = io.StringIO()
                self.assertEqual(q.main(arguments, output), 2)
                self.report(json.loads(output.getvalue()), False, 0)
                self.assertEqual(stderr.getvalue(), '')
                signer.assert_not_called()
                network.assert_not_called()

    def test_02_key_metadata_flags_permissions_links_owner_and_fifo_are_guarded(self):
        real_open = os.open
        for mode in (0o400, 0o600):
            key = self.work / ('valid-' + str(mode))
            shutil.copyfile(self.key, key)
            key.chmod(mode)
            with patch.object(q.os, 'open', wraps=real_open) as opened:
                fd = q.open_key_checked(str(key))
                try:
                    self.assertGreaterEqual(fd, 3)
                    flags = opened.call_args.args[1]
                    for flag in (os.O_NOFOLLOW, os.O_NONBLOCK, os.O_CLOEXEC):
                        self.assertEqual(flags & flag, flag)
                    self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                finally:
                    os.close(fd)
        invalid = []
        for mode in (0o640, 0o604, 0o700):
            path = self.work / ('mode-' + str(mode))
            path.write_bytes(b'fixture')
            path.chmod(mode)
            invalid.append(path)
        for name, body in (('empty', b''), ('large', b'x' * 16385)):
            path = self.work / name
            path.write_bytes(body)
            path.chmod(0o600)
            invalid.append(path)
        symlink = self.work / 'symlink'
        symlink.symlink_to(self.key)
        invalid.extend((symlink, self.work, self.work / 'missing'))
        hard = self.work / 'hard-original'
        shutil.copyfile(self.key, hard)
        hard.chmod(0o600)
        os.link(hard, self.work / 'hard-second')
        invalid.append(hard)
        before = len(os.listdir('/proc/self/fd'))
        for path in invalid:
            with self.subTest(name=path.name), self.assertRaises(q.ProbeError):
                q.open_key_checked(str(path))
        real_stat = os.fstat
        def wrong_owner(fd):
            values = list(real_stat(fd))
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        with patch.object(q.os, 'fstat', side_effect=wrong_owner), self.assertRaises(q.ProbeError):
            q.open_key_checked(str(self.key))
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        fifo = self.work / 'fifo'
        os.mkfifo(fifo, 0o600)
        script = 'from src.ops.tools import github_app_readback as q\nimport sys\ntry:\n q.open_key_checked(sys.argv[1])\nexcept q.ProbeError:\n pass\nelse:\n raise AssertionError("FIFO accepted")\n'
        result = REAL_RUN([sys.executable, '-B', '-c', script, str(fifo)], cwd=PROJECT,
                          capture_output=True, timeout=2)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))

    def test_03_closed_stdin_fd_reservation_and_replaced_path_still_sign_original(self):
        script = ('import os,sys,tempfile\nfrom src.ops.tools import github_app_readback as q\n'
                  'from src.ops.tools.tests.test_github_app_readback import verify_fixture_jwt\n'
                  'os.close(0)\nfd=q.open_key_checked(sys.argv[1])\nassert fd>=3\n'
                  'token=q.sign_jwt(fd,12345,now=1700000000)\nos.close(fd)\n'
                  'with tempfile.TemporaryDirectory() as folder:\n verify_fixture_jwt(token,sys.argv[2],folder)\n')
        result = REAL_RUN([sys.executable, '-B', '-c', script, str(self.key), str(self.public)],
                          cwd=PROJECT, capture_output=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))
        path = self.work / 'replace.pem'
        shutil.copyfile(self.key, path)
        path.chmod(0o600)
        fd = q.open_key_checked(str(path))
        try:
            path.rename(self.work / 'original.pem')
            path.write_bytes(b'not a key')
            path.chmod(0o600)
            token = q.sign_jwt(fd, APP_ID, now=1700000000)
            verify_fixture_jwt(token, self.public, self.work)
        finally:
            os.close(fd)

    def test_04_real_rsa_signature_and_claims_have_exact_future_validity(self):
        fd = q.open_key_checked(str(self.key))
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                token = q.sign_jwt(fd, APP_ID, now=1700000000)
            self.assertGreater(os.fstat(fd).st_size, 0)
            header, payload = verify_fixture_jwt(token, self.public, self.work)
            self.assertEqual(header, {'alg': 'RS256', 'typ': 'JWT'})
            self.assertEqual(payload, {'iss': str(APP_ID), 'iat': 1699999940, 'exp': 1700000300})
            self.assertEqual(payload['exp'] - payload['iat'], 360)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))
        finally:
            os.close(fd)

    def test_05_bad_encrypted_ec_and_signer_failures_never_reach_http(self):
        malformed = self.work / 'malformed.pem'
        malformed.write_bytes(b'not a key ' + PRIVATE.encode())
        malformed.chmod(0o600)
        for key in (malformed, self.ec, self.encrypted):
            with self.subTest(kind=key.name), connection_patch() as network:
                self.report(q.probe(self.pins, str(key)), False, 0)
                network.assert_not_called()
        failures = [FileNotFoundError(PRIVATE), subprocess.TimeoutExpired('/usr/bin/openssl', 5, output=PRIVATE),
                    subprocess.CompletedProcess([], 1, b'', PRIVATE),
                    subprocess.CompletedProcess([], 0, b'', PRIVATE),
                    subprocess.CompletedProcess([], 0, b'x' * 1025, PRIVATE)]
        for failure in failures:
            options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with self.subTest(kind=type(failure).__name__), patch.object(q.subprocess, 'run', **options), \
                 connection_patch() as network:
                report = q.probe(self.pins, str(self.key))
                self.report(report, False, 0)
                self.assertEqual((report['stage'], report['reason']), ('SIGN', 'SIGNING_FAILED'))
                network.assert_not_called()

    def test_06_openssl_argv_environment_and_passed_fd_expose_no_key_material(self):
        fd = q.open_key_checked(str(self.key))
        try:
            completed = subprocess.CompletedProcess([], 0, b'x' * 256)
            with patch.object(q.subprocess, 'run', return_value=completed) as called:
                q.sign_jwt(fd, APP_ID, now=1700000000)
            args, kwargs = called.call_args
            self.assertEqual(args[0], ['/usr/bin/openssl', 'dgst', '-sha256', '-sign',
                                     '/proc/self/fd/' + str(fd), '-passin', 'pass:',
                                     '-sigopt', 'rsa_padding_mode:pkcs1'])
            self.assertEqual(kwargs['env'], {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'OPENSSL_CONF': '/dev/null'})
            self.assertEqual(kwargs['pass_fds'], (fd,))
            self.assertEqual(kwargs['timeout'], 5)
            self.assertIs(kwargs['shell'], False)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            self.assertEqual(kwargs['stdout'], subprocess.PIPE)
            self.assertIs(type(kwargs['input']), bytes)
            self.assertEqual(kwargs['input'].count(b'.'), 1)
            self.assertNotIn(str(self.key), repr(args))
            self.assertNotIn(b'PRIVATE KEY', kwargs['input'])
        finally:
            os.close(fd)

    def test_07_reader_fixed_host_three_gets_no_duplicates_or_extra_paths(self):
        transport = FakeTransport()
        reader = q.GitHubReader(DUMMY_JWT, self.pins)
        with connection_patch(transport.factory):
            reader.get_json(PATHS[0])
            for path in (PATHS[0], '/app/installations/1/access_tokens', 'https://other.invalid/app'):
                with self.subTest(path=path), self.assertRaises(q.ProbeError):
                    reader.get_json(path)
            reader.get_json(PATHS[1])
            reader.get_json(PATHS[2])
            with self.assertRaises(q.ProbeError):
                reader.get_json(PATHS[0])
            capped = q.GitHubReader(DUMMY_JWT, self.pins)
            capped.get_attempts = 3
            with self.assertRaises(q.ProbeError):
                capped.get_json(PATHS[0])
        self.assertEqual(reader.get_attempts, 3)
        self.assertEqual([row[1] for row in transport.requests], list(PATHS))
        for method, path, body, headers in transport.requests:
            self.assertEqual(method, 'GET')
            self.assertIsNone(body)
            self.assertEqual(headers['Authorization'], 'Bearer ' + DUMMY_JWT)
            self.assertEqual(headers['Accept'], 'application/vnd.github+json')
        self.assertEqual(transport.factories, [('api.github.com', None, 10, True, ssl.CERT_REQUIRED)] * 3)
        self.assertTrue(all(connection.closed for connection in transport.connections))

    def test_08_real_tls_loopback_three_gets_match_metadata_only(self):
        routes = {path: (200, encode(row), {}) for path, row in fixture_rows().items()}
        with TLSFixture(self.cert, self.key, routes) as server, self.tls_mapping(server) as seen:
            report = q.probe(self.pins, str(self.key))
            self.report(report, True, 3)
            self.assertEqual([row[1] for row in server.requests], list(PATHS))
            self.assertTrue(all(row[2].startswith('Bearer ') for row in server.requests))
            self.assertEqual(seen, [('api.github.com', None, 10, True, ssl.CERT_REQUIRED)] * 3)

    def test_09_redirect_302_valid_metadata_is_blocked_without_target_authorization(self):
        with TLSFixture(self.cert, self.key, {'/steal': (200, encode(fixture_rows()[PATHS[0]]), {})}) as sink:
            routes = {path: (200, encode(row), {}) for path, row in fixture_rows().items()}
            routes[PATHS[0]] = (302, encode(fixture_rows()[PATHS[0]]),
                               {'Location': 'https://localhost:' + str(sink.port) + '/steal'})
            with TLSFixture(self.cert, self.key, routes) as server, \
                 self.tls_mapping(server, sink) as seen, patch.object(q, 'sign_jwt', return_value=DUMMY_JWT):
                report = q.probe(self.pins, str(self.key))
                self.report(report, False, 1)
                self.assertEqual(report['reason'], 'HTTP_STATUS_INVALID')
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(sink.requests, [])
                self.assertEqual([item[0] for item in seen], ['api.github.com'])

    def test_10_http_failures_and_idle_errors_stop_before_body_or_later_gets(self):
        for status in (301, 401, 403, 404, 500):
            response = FakeResponse(status, encode(fixture_rows()[PATHS[0]]))
            report, transport = self.fake_probe(replacements={PATHS[0]: response})
            self.report(report, False, 1)
            self.assertEqual(response.read_sizes, [])
            self.assertEqual(len(transport.requests), 1)
        for error in (socket.timeout(PRIVATE), OSError(PRIVATE), ssl.SSLError(PRIVATE)):
            report, transport = self.fake_probe(replacements={PATHS[0]: error})
            self.report(report, False, 1)
            self.assertEqual(report['reason'], 'HTTP_TRANSPORT_FAILED')
            self.assertTrue(all(connection.closed for connection in transport.connections))

    def test_11_stream_cap_and_strict_json_reject_private_invalid_inputs(self):
        normal = encode(fixture_rows()[PATHS[0]])
        boundary = FakeResponse(200, normal.ljust(128 * 1024, b' '))
        report, _ = self.fake_probe(replacements={PATHS[0]: boundary})
        self.report(report, True, 3)
        invalid = [b'\xff', b'{', b'[]', normal[:-1] + b',"x":1,"x":2}',
                   normal[:-1] + b',"x":{"a":1,"a":2}}',
                   normal[:-1] + b',"x":NaN}', normal[:-1] + b',"x":Infinity}',
                   normal[:-1] + b',"x":123456789012345678901}',
                   normal[:-1] + b',"x":' + b'[' * 65 + b'0' + b']' * 65 + b'}',
                   normal[:-1] + b',"x":' + b'[' * 1200 + b'0' + b']' * 1200 + b'}',
                   normal.ljust(128 * 1024 + 1, b' ')]
        for index, body in enumerate(invalid):
            with self.subTest(index=index):
                response = FakeResponse(200, body)
                report, transport = self.fake_probe(replacements={PATHS[0]: response})
                self.report(report, False, 1)
                self.assertEqual(report['reason'], 'RESPONSE_INVALID')
                self.assertEqual(response.read_sizes, [128 * 1024 + 1])
                self.assertEqual(len(transport.requests), 1)

    def test_12_app_identity_slug_events_and_bool_ids_are_not_coerced(self):
        cases = [('id', True), ('id', APP_ID + 1), ('owner', {'login': 'other', 'type': 'User'}),
                 ('owner', {'login': OWNER, 'type': 'Organization'}), ('slug', 'other'),
                 ('events', ['push']), ('events', None)]
        for field, value in cases:
            with self.subTest(field=field):
                rows = fixture_rows()
                rows[PATHS[0]][field] = value
                report, _ = self.fake_probe(rows)
                self.report(report, False, 1)
                self.assertEqual(report['reason'], 'APP_MISMATCH')

    def test_13_both_installations_require_strict_identity_selected_and_present_null(self):
        cases = [('id', True), ('id', INSTALL_ID + 1), ('app_id', True), ('app_id', APP_ID + 1),
                 ('account', {'login': 'other', 'type': 'User'}),
                 ('account', {'login': OWNER, 'type': 'Organization'}),
                 ('repository_selection', 'all'), ('suspended_at', '2026-01-01'), ('suspended_at', 'MISSING')]
        for position in (1, 2):
            for field, value in cases:
                with self.subTest(position=position, field=field):
                    rows = fixture_rows()
                    if value == 'MISSING':
                        rows[PATHS[position]].pop(field)
                    else:
                        rows[PATHS[position]][field] = value
                    report, _ = self.fake_probe(rows)
                    self.report(report, False, position + 1)
                    self.assertEqual(report['stage'], 'INSTALLATION' if position == 1 else 'REPOSITORY')

    def test_14_same_extra_installation_grants_and_permission_map_errors_are_blocked(self):
        # Both installation responses agree; only exact granted-map enforcement can reject them.
        rows = fixture_rows()
        for path in PATHS[1:]:
            rows[path]['permissions']['administration'] = 'write'
        report, _ = self.fake_probe(rows)
        self.report(report, False)
        maps = [{key: value for key, value in PERMISSIONS.items() if key != 'contents'},
                {**PERMISSIONS, 'issues': 'read'}, {**PERMISSIONS, 'contents': 'write'},
                {**PERMISSIONS, 'metadata': True}, None]
        for position in range(3):
            for permissions in maps:
                with self.subTest(position=position, kind=type(permissions).__name__):
                    rows = fixture_rows()
                    rows[PATHS[position]]['permissions'] = permissions
                    report, _ = self.fake_probe(rows)
                    self.report(report, False, position + 1)

    def test_15_main_success_is_one_exact_json_but_always_exit_two(self):
        transport = FakeTransport()
        output, stderr = io.StringIO(), io.StringIO()
        with connection_patch(transport.factory), \
             patch.object(q, 'sign_jwt', return_value=DUMMY_JWT), redirect_stderr(stderr):
            self.assertEqual(q.main(self.argv(), output), 2)
        self.assertEqual(output.getvalue().count('\n'), 1)
        self.report(json.loads(output.getvalue()), True, 3)
        self.assertEqual(stderr.getvalue(), '')

    def test_16_private_runtime_failures_and_broken_output_are_sanitized(self):
        rows = fixture_rows()
        rows[PATHS[0]]['owner']['login'] = PRIVATE
        report, _ = self.fake_probe(rows)
        self.report(report, False, 1)
        with patch.object(q.GitHubReader, 'get_json', side_effect=q.ProbeError(PRIVATE, PRIVATE)), \
             patch.object(q, 'sign_jwt', return_value=DUMMY_JWT):
            self.report(q.probe(self.pins, str(self.key)), False, 0)
        for failure in (RuntimeError(PRIVATE), OSError(PRIVATE), UnicodeError(PRIVATE)):
            output, stderr = io.StringIO(), io.StringIO()
            with patch.object(q, 'probe', side_effect=failure), redirect_stderr(stderr):
                self.assertEqual(q.main(self.argv(), output), 2)
            self.report(json.loads(output.getvalue()), False, 0)
            self.assertEqual(stderr.getvalue(), '')
        class BrokenOutput:
            def write(self, value):
                raise OSError(PRIVATE)
            def flush(self):
                raise ValueError(PRIVATE)
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(q.main(['--help'], BrokenOutput()), 2)
        self.assertEqual(stderr.getvalue(), '')
        child = subprocess.Popen([sys.executable, '-I', '-B', str(SOURCE), '--help'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child.stdout.close()
        stderr = child.stderr.read()
        child.stderr.close()
        self.assertEqual(child.wait(timeout=3), 2)
        self.assertEqual(stderr, b'')

    def test_17_failures_close_fd_before_http_and_stop_at_the_failed_step(self):
        opened = []
        original = q.open_key_checked
        def tracked(path):
            fd = original(path)
            opened.append(fd)
            return fd
        with patch.object(q, 'open_key_checked', side_effect=tracked), \
             patch.object(q, 'sign_jwt', side_effect=q.ProbeError('SIGN', 'SIGNING_FAILED')), \
             connection_patch() as network:
            self.report(q.probe(self.pins, str(self.key)), False, 0)
            network.assert_not_called()
        with self.assertRaises(OSError):
            os.fstat(opened[-1])
        for position in range(3):
            response = FakeResponse(403, encode({'private': PRIVATE}))
            report, transport = self.fake_probe(replacements={PATHS[position]: response})
            self.report(report, False, position + 1)
            self.assertEqual([request[1] for request in transport.requests], list(PATHS[:position + 1]))
            self.assertTrue(all(connection.closed for connection in transport.connections))
        transport = FakeTransport()
        original_factory = transport.factory
        def check_closed(*args, **kwargs):
            with self.assertRaises(OSError):
                os.fstat(opened[-1])
            return original_factory(*args, **kwargs)
        with patch.object(q, 'open_key_checked', side_effect=tracked), \
             patch.object(q, 'sign_jwt', return_value=DUMMY_JWT), \
             connection_patch(check_closed):
            self.report(q.probe(self.pins, str(self.key)), True, 3)

    def test_18_real_untrusted_tls_proxy_environment_and_standalone_cli(self):
        routes = {path: (200, encode(row), {}) for path, row in fixture_rows().items()}
        with TLSFixture(self.cert, self.key, routes) as server, self.tls_mapping(server, trusted=False), \
             patch.object(q, 'sign_jwt', return_value=DUMMY_JWT):
            report = q.probe(self.pins, str(self.key))
            self.report(report, False, 1)
            self.assertEqual(report['reason'], 'HTTP_TRANSPORT_FAILED')
            self.assertEqual(server.requests, [])
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9'}):
            report, transport = self.fake_probe()
        self.report(report, True, 3)
        self.assertTrue(all(item[0] == 'api.github.com' for item in transport.factories))
        result = REAL_RUN([sys.executable, '-I', '-B', str(SOURCE), '--help'], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, b'')
        self.report(json.loads(result.stdout), False, 0)

    def test_19_untrusted_claims_extra_repo_metadata_and_repeat_never_promote_flags(self):
        rows = fixture_rows()
        for row in rows.values():
            row.update({name: True for name in FALSE_FLAGS})
            row.update(selected_repositories=['fixture_repo', 'other_repo'], evidence=PRIVATE)
        original = copy.deepcopy(rows)
        first, _ = self.fake_probe(rows)
        second, _ = self.fake_probe(rows)
        self.report(first, True, 3)
        self.report(second, True, 3)
        self.assertEqual(rows, original)
        first['trusted_gate_verified'] = True
        self.assertIs(second['trusted_gate_verified'], False)
        self.assertEqual(tuple(inspect.signature(q.probe).parameters), ('pins', 'key_path'))

    def test_20_python310_syntax_stdlib_source_pins_and_old_tests_unchanged(self):
        source = SOURCE.read_text()
        tree = ast.parse(source, feature_version=(3, 10))
        ast.parse(Path(__file__).read_text(), feature_version=(3, 10))
        imports = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module for node in tree.body if isinstance(node, ast.ImportFrom))
        self.assertTrue(all(name.split('.')[0] in sys.stdlib_module_names for name in imports))
        for value in ('5323842', '171552314'):
            self.assertNotIn(value, source)
        literal_strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertFalse(literal_strings & {'POST', 'PUT', 'PATCH', 'DELETE'})
        self.assertEqual(hashlib.sha256(PLAN.read_bytes()).hexdigest(), PLAN_SHA)
        old_hashes = {'test_github_check_audit.py': 'fcc0b85600114a578f55b8e075bc2519d4b71d900e5ddcd73faaf81b7e5539c5',
                      'test_github_evidence_binding.py': '9bb7a99002451f3422e3db7fe130bafacac39d294d1facc371b340535ca01f41'}
        for name, digest in old_hashes.items():
            self.assertEqual(hashlib.sha256((SOURCE.parent / 'tests' / name).read_bytes()).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()
