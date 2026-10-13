import contextlib
import copy
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse

OPS = Path(__file__).resolve().parents[1] / 'ops'
sys.path.insert(0, str(OPS))
import lf_common as common
import manage
import trace_check as trace


def config(root):
    s = {'schema': 1, 'owner': 'bdd46bf0-98fb-44c1-a239-cdcc71ad7ab4', 'uid': os.getuid(), 'gid': os.getgid(),
         'state_dir': str(root), 'host': {'docker_root': '/docker', 'context': 'default', 'hostname': 'fixture'},
         'images': manage.IMAGES, 'public_url': 'http://localhost:16320', 'email': 'admin@example.test',
         'user_name': 'fixture', 'password': "a$B'\\\"=Secret9", 'org_id': 'fixture-org', 'project_id': 'fixture-project',
         'public_key': 'pk-lf-test', 'secret_key': 'sk-lf-test', 'storage': {}}
    for key in ('db_password', 'ch_password', 'redis_password', 's3_secret', 'salt', 'encryption_key', 'nextauth_secret', 's3_key'):
        s[key] = 'a' * 64
    return s


def setup(root):
    root.chmod(0o700)
    s = config(root)
    (root / 'data').mkdir(mode=0o700)
    for name in common.STORES:
        path = root / 'data' / name
        path.mkdir(mode=0o700)
        st = path.stat()
        s['storage'][name] = [st.st_dev, st.st_ino]
    common.write_private(root / 'settings.json', s)
    common.write_private(root / 'owner.json', {'owner': s['owner']})
    for name, value in manage.environments(s).items():
        common.write_private(root / name, value)
    return s


def output(record):
    spans = record['payload']['resourceSpans'][0]['scopeSpans'][0]['spans']
    return [{'id': x['spanId'], 'traceId': x['traceId'], 'projectId': record['project_id'],
             'parentObservationId': x.get('parentSpanId'), 'name': x['name'], 'type': 'SPAN',
             'level': 'ERROR' if x['status']['code'] == 2 else 'DEFAULT', 'statusMessage': x['status']['message'],
             'startTime': trace.stamp(int(x['startTimeUnixNano']) // 1000000),
             'endTime': trace.stamp(int(x['endTimeUnixNano']) // 1000000), 'environment': 'infra-check',
             'metadata': {'infra_owner': record['owner']}, 'input': None, 'output': None} for x in spans]


class HttpFixture:
    def __init__(self, cfg):
        self.config = cfg
        self.posts = []
        self.rows = []
        self.gets = []
        self.lose_response = False
        self.accept_without_publish = False
        self.reject_partial = False
        self.redirect = None
        self.record_path = Path(cfg['state_dir']) / 'trace-check.json'

    def __enter__(self):
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fixture.gets.append(self.path)
                if fixture.redirect:
                    self.send_response(302)
                    self.send_header('Location', fixture.redirect)
                    self.end_headers()
                    return
                assert self.headers.get('Authorization', '').startswith('Basic ')
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'data': fixture.rows, 'meta': {}}).encode())

            def do_POST(self):
                assert self.path == '/api/public/otel/v1/traces'
                assert self.headers['x-langfuse-ingestion-version'] == '4'
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                record = common.read_json(fixture.record_path)
                assert record['payload'] == data and record['post_attempts'] > 0
                fixture.posts.append(data)
                if not fixture.accept_without_publish and not fixture.reject_partial:
                    fixture.rows = output(record)
                if fixture.lose_response:
                    self.close_connection = True
                    return
                self.send_response(200)
                self.end_headers()
                value = {'partialSuccess': {'rejectedSpans': '1'}} if fixture.reject_partial else {}
                self.wfile.write(json.dumps(value).encode())
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = common.Client(self.config, 'http://127.0.0.1:' + str(self.server.server_port))
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class StateTests(unittest.TestCase):
    def test_private_state_and_raw_special_char_password(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            self.assertEqual(manage.load(root), s)
            self.assertIn('LANGFUSE_INIT_USER_PASSWORD=' + s['password'] + '\n', (root / 'web.env').read_text())
            self.assertFalse((root / 'web.env').stat().st_mode & 0o077)
            self.assertNotIn('LANGFUSE_INIT_USER_PASSWORD', (root / 'worker.env').read_text())
            self.assertNotIn('LANGFUSE_INIT_PROJECT_RETENTION', (root / 'web.env').read_text())

    def test_reprepare_reuses_keys_without_prompt_and_recovers_missing_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            old = (root / 'settings.json').read_bytes()
            (root / 'worker.env').unlink()
            with patch('builtins.input', side_effect=AssertionError('must not prompt')):
                result = manage.prepare(None, root, s['host'])
            self.assertEqual(result, s)
            self.assertEqual((root / 'settings.json').read_bytes(), old)
            self.assertEqual(manage.load(root), s)

    def test_changed_or_missing_storage_rejected_before_start(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            setup(root)
            (root / 'data/redis').rename(root / 'data/saved-redis')
            (root / 'data/redis').mkdir()
            with self.assertRaises(ValueError):
                manage.load(root)

    def test_changed_secrets_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            setup(root)
            common.write_private(root / 'web.env', 'changed')
            with self.assertRaises(ValueError):
                manage.load(root)
            target = root / 'link'
            target.symlink_to(root / 'settings.json')
            with self.assertRaises(ValueError):
                common.private_read(target)

    def test_url_validation_does_not_accept_credentials_paths_or_wrong_ports(self):
        for url in ['https://example.test:16320', 'http://a:b@x:16320', 'http://x:16320/a', 'http://x:3000', 'http://x:16320?q=1']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                manage.validate_url(url)
        self.assertEqual(manage.validate_url('http://192.168.0.1:16320/'), 'http://192.168.0.1:16320')

    def test_selector_short_circuits_n8n_and_preserves_options(self):
        path = OPS.parents[1] / 'ops/up.py'
        spec = importlib.util.spec_from_file_location('isolated_infra_up', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for flag, program in [('--langfuse-only', 'langfuse/ops/manage.py'), ('--gpu-only', 'observability/ops/gpu.py'),
                              ('--observability-only', 'observability/ops/manage.py')]:
            with patch.object(sys, 'argv', ['up.py', flag]), patch.object(module.subprocess, 'call', return_value=0) as call:
                self.assertEqual(module.main(), 0)
                self.assertTrue(call.call_args.args[0][2].endswith(program))
                self.assertEqual(call.call_args.args[0][-1], 'up')

    def test_foreign_container_prevents_adoption(self):
        class Docker:
            def run(self, args):
                return 'foreign'
            def json(self, args):
                return [{'Config': {'Labels': {'com.docker.compose.service': 'lf-web', common.OWNER: 'foreign'}}}]
        with self.assertRaises(ValueError):
            manage.owned_containers(Docker(), 'own')

    def test_registry_alias_digest_normalization(self):
        self.assertEqual(manage.canonical_image('docker.io/library/postgres@sha256:1'), 'postgres@sha256:1')
        self.assertNotEqual(manage.canonical_image('evil.test/postgres@sha256:1'), 'postgres@sha256:1')


class TraceTests(unittest.TestCase):
    def test_http_roundtrip_same_request_and_get_only_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            with HttpFixture(s) as server:
                first = trace.execute('run', root, s, server.client, 0)
                before = (root / 'trace-check.json').read_bytes()
                again = trace.execute('run', root, s, server.client, 0)
                read = trace.execute('check', root, s, server.client, 0)
                self.assertEqual(first['result'], 'passed')
                self.assertEqual(first['trace_id'], again['trace_id'])
                self.assertFalse(again['post_submitted'])
                self.assertFalse(read['post_submitted'])
                self.assertEqual(len(server.posts), 1)
                self.assertEqual(before, (root / 'trace-check.json').read_bytes())
                self.assertNotIn('gen_ai.prompt', json.dumps(server.posts))

    def test_lost_post_response_recovered_by_read_without_new_trace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            with HttpFixture(s) as server:
                server.lose_response = True
                first = trace.execute('run', root, s, server.client, 0)
                second = trace.execute('run', root, s, server.client, 0)
                self.assertEqual(first['result'], 'passed')
                self.assertEqual(first['trace_id'], second['trace_id'])
                self.assertEqual(len(server.posts), 1)

    def test_uncertain_not_delivered_reuses_exact_body_on_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            with HttpFixture(s) as server:
                server.lose_response = True
                server.accept_without_publish = True
                first = trace.execute('run', root, s, server.client, 0)
                self.assertEqual(first['result'], 'pending')
                server.accept_without_publish = False
                second = trace.execute('run', root, s, server.client, 0)
                self.assertEqual(second['result'], 'passed')
                self.assertEqual(server.posts[0], server.posts[1])

    def test_accepted_pending_check_never_reposts_or_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            with HttpFixture(s) as server:
                server.accept_without_publish = True
                result = trace.execute('run', root, s, server.client, 0)
                before = (root / 'trace-check.json').read_bytes()
                for action in ['run', 'check']:
                    result = trace.execute(action, root, s, server.client, 0)
                    self.assertEqual(result['result'], 'pending')
                self.assertEqual(len(server.posts), 1)
                self.assertEqual(before, (root / 'trace-check.json').read_bytes())

    def test_partial_rejection_stays_failed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            with HttpFixture(s) as server:
                server.reject_partial = True
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        trace.execute('run', root, s, server.client, 0)
                self.assertEqual(len(server.posts), 1)

    def test_lookup_before_run_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            with self.assertRaises(ValueError):
                trace.execute('check', root, s, None, 0)
            self.assertFalse((root / 'trace-check.json').exists())

    def test_payload_tamper_rejected_before_http(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            record = trace.fixture(s)
            record['payload']['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['name'] = 'changed'
            common.write_private(root / 'trace-check.json', record)
            with self.assertRaises(ValueError):
                trace.execute('run', root, s, None, 0)

    def test_wrong_parent_status_time_project_and_duplicate_rejected(self):
        record = trace.fixture(config(Path('/fixture')))
        good = output(record)
        self.assertTrue(trace.verify(good, record))
        for field, value in [('parentObservationId', 'wrong'), ('level', 'DEFAULT'), ('projectId', 'wrong'),
                             ('endTime', '2000-01-01T00:00:00Z'), ('input', 'private prompt')]:
            rows = copy.deepcopy(good)
            rows[2][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                trace.verify(rows, record)
        with self.assertRaises(ValueError):
            trace.verify(good + [good[0]], record)
        self.assertFalse(trace.verify(good[:2], record))

    def test_auth_redirect_not_followed(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td))
            with HttpFixture(s) as server:
                server.redirect = 'http://127.0.0.1:1/secret'
                with self.assertRaises(common.HttpFailure) as exc:
                    server.client.request('GET', '/api/public/projects')
                self.assertEqual(exc.exception.status, 302)

    def test_nanosecond_timestamp_on_old_python_contract(self):
        self.assertEqual(trace.millis('2026-09-26T04:39:56.158859873+00:00'), trace.millis('2026-09-26T04:39:56.158859Z'))

class LifecycleTests(unittest.TestCase):
    def test_up_scopes_commands_to_owned_stack_and_never_forces_recreate(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td))
            class Docker:
                def __init__(self): self.calls = []
                def run(self, args, timeout=120): self.calls.append(args); return ''
                def json(self, args):
                    self.calls.append(args)
                    if args[:2] == ['image', 'inspect']:
                        return [{'RepoDigests': [manage.canonical_image(args[2])]}]
                    return {}
            docker = Docker()
            with patch.object(manage, 'resources', return_value={}), patch.object(manage, 'owned_containers', return_value=[]), \
                 patch.object(manage, 'check_networks'), patch.object(manage, 'port_free'), \
                 patch.object(manage, 'config_contract'), patch.object(manage, 'check', return_value={'result': 'ready'}):
                result = manage.up(docker, s, s['host'])
            self.assertEqual(result['result'], 'ready')
            self.assertFalse(result['domain_services_restarted'])
            commands = json.dumps(docker.calls)
            for forbidden in ['down', 'prune', '--force-recreate', 'restart', 'agent-runtime', 'ops-infra', 'infrastructure-observability']:
                self.assertNotIn(forbidden, commands)
            creates = [x for x in docker.calls if x[0] == 'run']
            self.assertEqual(len(creates), 1)
            self.assertIn('chown 101:101 /ch-data /ch-logs', creates[0])
            self.assertNotIn('-R', creates[0][-1])
            ups = [x for x in docker.calls if 'up' in x]
            self.assertEqual(ups[0][2], common.PROJECT)
            self.assertIn('--pull', ups[0])
            self.assertIn('never', ups[0])

    def test_prepare_first_use_is_private_and_reuses_prompts_only_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'new'
            host = {'docker_root': '/docker', 'context': 'default', 'hostname': 'fixture'}
            with patch.object(manage, 'owned_containers'), patch.object(manage, 'check_networks'), \
                 patch.object(manage, 'port_free'), patch.object(manage, 'resources'), \
                 patch('builtins.input', side_effect=['http://server:16320', 'admin@example.test', '관리자']), \
                 patch.object(manage.getpass, 'getpass', side_effect=['Password123', 'Password123']):
                s = manage.prepare(None, root, host)
            self.assertEqual(s, manage.load(root, host))
            self.assertEqual(len(s['storage']), 5)
            self.assertFalse(root.stat().st_mode & 0o077)
            with patch('builtins.input', side_effect=AssertionError('prompt repeated')):
                self.assertEqual(manage.prepare(None, root, host), s)

    def test_wrong_pinned_mount_or_digest_blocks_container_reuse(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td))
            item = {'Config': {'Labels': {'com.docker.compose.service': 'lf-postgres'}, 'Image': manage.IMAGES['lf-postgres']},
                    'Mounts': [{'Type': 'bind', 'Destination': '/var/lib/postgresql/data', 'Source': str(Path(td)/'data/postgres')}]}
            manage.inspect_binding(item, s)
            item['Mounts'][0]['Source'] = '/other-database'
            with self.assertRaises(ValueError): manage.inspect_binding(item, s)

    def test_ready_requires_healthy_services_version_and_project(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td))
            items = [{'State': {'Running': True, 'Health': {'Status': 'healthy'}}} for _ in range(6)]
            class Client:
                version = manage.VERSION
                def request(self, method, path, **kwargs):
                    if path.startswith('/api/public/health'): return {'version': self.version}
                    if path == '/api/public/projects': return {'data': [{'id': s['project_id'], 'organization': {'id': s['org_id']}}]}
                    return {}
            client = Client()
            with patch.object(manage, 'owned_containers', return_value=items), patch.object(manage, 'inspect_binding'):
                self.assertEqual(manage.check(None, s, 0, client)['result'], 'ready')
                client.version = '0.0.0'
                with self.assertRaises(ValueError): manage.check(None, s, 0, client)
                client.version = manage.VERSION
                items[0]['State']['Health']['Status'] = 'unhealthy'
                with self.assertRaises(ValueError): manage.check(None, s, 0, client)


if __name__ == '__main__':
    unittest.main()
