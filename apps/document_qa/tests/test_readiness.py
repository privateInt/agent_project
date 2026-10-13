"""Q02 scenarios: fake gateways, temporary own settings, one loopback HTTP server."""
import copy
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError

from apps.document_qa import readiness as q

FIXTURE = json.loads((Path(__file__).parents[1] / 'fixtures/readiness-responses.json').read_text())


class Fake:
    def __init__(self):
        self.f = copy.deepcopy(FIXTURE)
        self.deps = [self.f['embedding'], self.f['chat']]
        self.fail = {}
        self.calls = 0
        self.credential_reads = 0
        self.connections = []

    def get(self, role, path, query=None):
        self.calls += 1
        key = (role, path)
        if key in self.fail:
            raise q.ReadinessError(self.fail[key])
        if path in ('/health', '/readyz'):
            return {'status': 'ready' if path == '/readyz' else 'ok'}
        if path == '/api/source-documents':
            return {'items': [], 'next_cursor': ''}
        if path == '/api/connections':
            return {'items': copy.deepcopy(self.connections), 'next_cursor': ''}
        if path == '/api/chunkers':
            return copy.deepcopy(self.f['chunkers'])
        if path == '/api/deployments':
            return {'items': copy.deepcopy(self.deps), 'next_cursor': ''}
        if path.endswith('/embedding-profile'):
            if self.f['embedding']['deployment_id'] in path:
                return copy.deepcopy(self.f['profile'])
            raise q.ReadinessError('MODEL_NOT_READY')
        if path.startswith('/api/deployments/'):
            return copy.deepcopy(next(x for x in self.deps if path.endswith(x['deployment_id'])))
        if path.startswith('/api/model-versions/'):
            return copy.deepcopy(self.f['version'])
        if path.startswith('/api/models/'):
            return copy.deepcopy(self.f['model'])
        raise AssertionError(path)


def settings():
    return {'schema': 1, 'platforms': {x: {'url': 'http://127.0.0.1:1', 'enabled': True,
            **({'token_file': '/example/api-token'} if x == 'agent' else {})} for x in q.ROLES},
            'library_name': '문서 자료함', 'defaults': {}}


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.s, self.r = settings(), Fake()

    def result(self, choices=None):
        return q.inspect(self.s, self.r, choices)

    def codes(self, report):
        return {x['code'] for x in report['blockers']}

    def test_unique_compatible_defaults_and_question_not_ready(self):
        r = self.result()
        self.assertTrue(r['defaults_ready'])
        self.assertFalse(r['question_ready'])
        self.assertEqual(r['defaults']['chunker']['id'], 'structure_v1')
        self.assertEqual(r['effects']['api_methods'], ['GET'])
        self.assertFalse(r['effects']['configuration_written'])

    def test_no_running_model(self):
        self.r.deps = []
        self.assertIn('NO_COMPATIBLE_MODEL', self.codes(self.result()))

    def test_stopped_selected_model_is_not_replaced(self):
        prior = self.result()['defaults']
        self.s['defaults'] = prior
        self.r.deps[1]['desired_state'] = 'stopped'
        r = self.result()
        self.assertIn('SELECTED_MODEL_UNAVAILABLE', self.codes(r))
        self.assertEqual(r['excluded_models'][0]['code'], 'MODEL_NOT_READY')

    def test_several_candidates_require_choice_and_ambiguous_name(self):
        a = self.result()['defaults']['chat']
        b = dict(a, deployment_id='abcdef00-0000-4000-8000-000000000001', label='second')
        with self.assertRaisesRegex(q.ReadinessError, 'SELECTION_REQUIRED'):
            q.select([a, b])
        with self.assertRaisesRegex(q.ReadinessError, 'SELECTION_AMBIGUOUS'):
            q.select([a, b], choice=a['name'])
        self.assertEqual(q.select([a, b], choice='second'), b)

    def test_pinned_space_mismatch_is_blocked(self):
        self.s['defaults'] = self.result()['defaults']
        self.r.f['profile']['embedding_space_id'] = 'es_' + 'f' * 64
        self.assertIn('SELECTED_MODEL_CHANGED', self.codes(self.result()))

    def test_mismatched_profile_deployment_is_blocked(self):
        self.r.f['profile']['deployment_id'] = self.r.f['chat']['deployment_id']
        self.assertIn('IDENTITY_MISMATCH', self.codes(self.result()))

    def test_embedding_profile_from_another_model_is_not_candidate(self):
        self.r.f['profile']['dimension'] = 768
        r = self.result()
        self.assertFalse(r['defaults_ready'])
        self.assertEqual(r['candidates']['embedding'], [])
        self.assertIn('NO_COMPATIBLE_MODEL', self.codes(r))

    def test_legacy_embedding_repository_never_used_as_chat(self):
        self.r.deps = [self.r.f['embedding']]
        self.r.deps[0]['serving']['profile'] = ''
        self.r.f['version'].update(model_version_id=self.r.deps[0]['model_version_id'], repo_id=q.REPOSITORY)
        r = self.result()
        self.assertEqual(len(r['candidates']['embedding']), 1)
        self.assertEqual(r['candidates']['chat'], [])

    def test_failed_later_page_does_not_autoselect_earlier_candidate(self):
        get = self.r.get
        def call(role, path, query=None):
            if path == '/api/deployments':
                if query.get('cursor'):
                    raise q.ReadinessError('CONNECTION_UNAVAILABLE')
                return {'items': self.r.deps, 'next_cursor': 'next'}
            return get(role, path, query)
        self.r.get = call
        r = self.result()
        self.assertEqual(r['defaults'], {})
        self.assertIn('CONNECTION_UNAVAILABLE', self.codes(r))

    def test_cursor_loop_and_duplicate_ids_block_selection(self):
        class Repeated:
            def get(self, *a):
                return {'items': [], 'next_cursor': 'same'}
        with self.assertRaisesRegex(q.ReadinessError, 'CURSOR_REPEATED'):
            q.pages(Repeated(), 'model', '/api/deployments', 'deployment_id')
        self.r.deps.append(self.r.deps[0])
        self.assertIn('INVENTORY_CHANGED', self.codes(self.result()))

    def test_changed_inventory_identity_blocks_all_defaults(self):
        get = self.r.get
        def call(role, path, query=None):
            result = get(role, path, query)
            if path == '/api/deployments/' + self.r.f['chat']['deployment_id']:
                result['model_version_id'] = self.r.f['embedding']['model_version_id']
            return result
        self.r.get = call
        self.assertIn('INVENTORY_CHANGED', self.codes(self.result()))

    def test_authentication_and_permission_errors_preserved(self):
        for code in ('AUTH_REQUIRED', 'PERMISSION_DENIED'):
            self.r.fail[('agent', '/api/connections')] = code
            self.assertIn(code, self.codes(self.result()))

    def test_gateway_health_alone_does_not_prove_data_or_agent_api(self):
        self.r.fail[('data', '/api/source-documents')] = 'HTTP_UNAVAILABLE'
        self.assertFalse(self.result()['defaults_ready'])

    def test_chunker_version_and_byte_budget_are_fixed(self):
        self.s['defaults'] = self.result()['defaults']
        self.r.f['chunkers']['items'][1]['version'] = 'future'
        self.assertIn('CHUNKER_VERSION_CHANGED', self.codes(self.result()))
        self.s['defaults'] = {}
        self.r.f['profile']['max_input_bytes'] = 100
        self.assertIn('CHUNKER_BYTE_LIMIT_INCOMPATIBLE', self.codes(self.result()))

    def test_source_config_reused_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'host-config.json'
            raw = json.dumps({'platforms': self.s['platforms'], 'unrelated_ops_field': 'ignored'}).encode()
            source.write_bytes(raw)
            value, sha, origin = q.load_settings(Path(d) / 'new/settings.json', source)
            self.assertIsNone(sha)
            self.assertEqual(origin, 'existing_platform_references')
            self.assertEqual(value['platforms'], self.s['platforms'])
            self.assertEqual(source.read_bytes(), raw)

    def test_persist_reload_and_refuse_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'qa/settings.json'
            r = self.result()
            self.assertTrue(q.save_settings(path, self.s, r, None))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded, sha, _ = q.load_settings(path, Path(d) / 'missing')
            self.assertTrue(q.inspect(loaded, Fake())['defaults_ready'])
            self.assertFalse(q.save_settings(path, loaded, r, sha))
            path.write_bytes(path.read_bytes() + b'\n')
            with self.assertRaisesRegex(q.ReadinessError, 'SETTINGS_CHANGED'):
                q.save_settings(path, loaded, r, sha)
            self.assertFalse(path.with_name('settings.lock').exists())

    def test_failed_readiness_does_not_save(self):
        self.r.deps = []
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'qa/settings.json'
            with self.assertRaisesRegex(q.ReadinessError, 'DEFAULTS_NOT_READY'):
                q.save_settings(path, self.s, self.result(), None)
            self.assertFalse(path.exists())

    def test_secrets_not_in_report_and_existing_readonly_key_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / 'key'
            key.write_bytes(b'test-secret-token\n')
            key.chmod(0o444)
            rows = copy.deepcopy(self.s['platforms'])
            rows['agent']['token_file'] = str(key)
            reader = q.GatewayReader(rows)
            class Failure:
                def open(self, request, timeout):
                    self.request = request
                    raise HTTPError(request.full_url, 401, 'secret error', {}, None)
            failure = Failure()
            reader.opener = failure
            with self.assertRaisesRegex(q.ReadinessError, 'AUTH_REQUIRED'):
                reader.get('agent', '/api/connections')
            self.assertEqual(failure.request.get_header('Authorization'), 'Bearer test-secret-token')
            self.assertEqual(key.stat().st_mode & 0o777, 0o444)
            self.assertNotIn('test-secret-token', json.dumps(self.result()))

    def test_symlink_key_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'key'; p.write_text('token')
            link = Path(d) / 'link'; link.symlink_to(p)
            with self.assertRaisesRegex(q.ReadinessError, 'CREDENTIAL_UNAVAILABLE'):
                q.read_file(link, credential=True)

    def test_origin_and_get_allowlist_prevent_inference_or_redirect(self):
        for value in ('http://user:secret@host', 'http://host/api', 'http://host?x=1', 'file:///tmp/file'):
            with self.assertRaises(q.ReadinessError):
                q.origin(value)
        reader = q.GatewayReader(self.s['platforms'])
        for path in ('/api/deployments/' + self.r.f['chat']['deployment_id'] + '/chat', '/embeddings', '//external'):
            with self.assertRaisesRegex(q.ReadinessError, 'READ_PATH_NOT_ALLOWED'):
                reader.get('model', path)
        self.assertEqual(reader.calls, 0)

    def test_budget_exhaustion_precedes_request(self):
        reader = q.GatewayReader(self.s['platforms'], budget_seconds=0)
        with self.assertRaisesRegex(q.ReadinessError, 'DISCOVERY_BUDGET_EXCEEDED'):
            reader.get('model', '/health')
        self.assertEqual(reader.calls, 0)

    def test_real_transport_json_and_redirect_refusal(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                if self.path == '/health':
                    self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                else:
                    self.send_response(302); self.send_header('Location', '/redirect-target'); self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            rows = copy.deepcopy(self.s['platforms']); rows['model']['url'] = 'http://127.0.0.1:' + str(server.server_port)
            reader = q.GatewayReader(rows)
            self.assertEqual(reader.get('model', '/health'), {'status': 'ok'})
            with self.assertRaisesRegex(q.ReadinessError, 'REDIRECT_REFUSED'):
                reader.get('model', '/api/deployments')
            self.assertNotIn('/redirect-target', seen)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_malformed_and_oversized_responses_not_accepted(self):
        class Response(io.BytesIO):
            status = 200
            headers = Message()
            headers['Content-Type'] = 'application/json'
        class Opener:
            def __init__(self, raw):
                self.raw = raw
            def open(self, *a, **kw):
                return Response(self.raw)
        for raw, code in ((b'{"x":1,"x":2}', 'RESPONSE_INVALID'), (b' ' * (q.MAX_RESPONSE + 1), 'RESPONSE_TOO_LARGE')):
            reader = q.GatewayReader(self.s['platforms']); reader.opener = Opener(raw)
            with self.assertRaisesRegex(q.ReadinessError, code):
                reader.get('model', '/health')

    def test_invalid_uuid_never_becomes_candidate(self):
        self.r.deps[0]['deployment_id'] = 'not-a-uuid'
        self.assertIn('IDENTITY_MISMATCH', self.codes(self.result()))

    def test_malformed_nested_model_response_is_reported(self):
        self.r.deps[0]['serving'] = ['invalid']
        self.assertIn('RESPONSE_INVALID', self.codes(self.result()))

    def test_runtime_profile_identity_mismatch_is_reported(self):
        self.r.f['profile']['runtime_container_id'] = 'f' * 64
        self.assertIn('IDENTITY_MISMATCH', self.codes(self.result()))

    def test_embedding_selection_can_override_old_pin_explicitly(self):
        self.s['defaults'] = self.result()['defaults']
        self.r.f['profile']['embedding_space_id'] = 'es_' + 'f' * 64
        r = self.result({'embedding': q.REPOSITORY})
        self.assertTrue(r['defaults_ready'])
        self.assertEqual(r['defaults']['embedding']['embedding_space_id'], 'es_' + 'f' * 64)

    def test_absent_and_disabled_connections_are_not_invented(self):
        rows = copy.deepcopy(self.s['platforms']); rows['model']['enabled'] = False
        with self.assertRaisesRegex(q.ReadinessError, 'CONNECTION_DISABLED'):
            q.GatewayReader(rows).get('model', '/health')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(q.ReadinessError, 'CONFIG_UNAVAILABLE'):
                q.load_settings(Path(d) / 'qa/settings.json', Path(d) / 'missing')

    def test_multiple_live_chat_deployments_need_named_choice(self):
        extra = copy.deepcopy(self.r.f['chat'])
        identity = 'abcdef00-0000-4000-8000-000000000001'
        extra.update(deployment_id=identity, served_model_name='deployment-' + identity,
                     internal_endpoint='http://model-serving-' + identity + ':8000/v1')
        self.r.deps.append(extra)
        report = self.result()
        self.assertIn('SELECTION_REQUIRED', self.codes(report))
        self.assertNotIn('chat', report['defaults'])
        label = next(x['label'] for x in report['candidates']['chat'] if x['deployment_id'] == identity)
        chosen = self.result({'chat': label})
        self.assertTrue(chosen['defaults_ready'])
        self.assertEqual(chosen['defaults']['chat']['deployment_id'], identity)


if __name__ == '__main__':
    unittest.main()
