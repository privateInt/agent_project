"""Q07a public assets and private HTTP routes, using disposable stores only."""
import http.client
import json
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.document_qa import web_assets
from apps.document_qa.api import make_server
from apps.document_qa.tests.test_uploads import FakeData, running
from apps.document_qa.uploads import UploadStore


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = UploadStore.initialize(Path(self.temp.name) / 'state')
        self.upstream = FakeData()

    def request(self, port, path, *, method='GET', headers=None, body=None, auth=False):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
        self.addCleanup(conn.close)
        values = {'Authorization': 'Bearer ' + self.store.token_path.read_text().strip()} if auth else {}
        values.update(headers or {})
        conn.request(method, path, body, values)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def test_public_page_and_fixed_assets_no_private_state_or_key(self):
        secret = self.store.token_path.read_bytes().strip()
        before = self.store.db_path.read_bytes()
        with running(make_server(self.store, self.upstream, port=0)) as port:
            for path, (name, mime) in web_assets.ASSETS.items():
                status, headers, raw = self.request(port, path)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], mime)
                self.assertEqual(raw, (web_assets.WEB / name).read_bytes())
                self.assertNotIn(secret, raw)
        self.assertEqual(self.store.db_path.read_bytes(), before)
        self.assertEqual(self.upstream.calls, [])

    def test_csp_no_external_script_inline_script_frame_or_referrer(self):
        with running(make_server(self.store, self.upstream, port=0)) as port:
            status, headers, raw = self.request(port, '/console')
            self.assertEqual(status, 200)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(headers['X-Frame-Options'], 'DENY')
            self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
            self.assertEqual(headers['Content-Security-Policy'], web_assets.CSP)
            self.assertNotIn(b'https://', raw)
            self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])

    def test_private_reads_and_writes_keep_authentication(self):
        with running(make_server(self.store, self.upstream, port=0)) as port:
            for path, method in [('/api/library', 'GET'), ('/api/jobs', 'GET'), ('/api/uploads', 'POST')]:
                status, _, raw = self.request(port, path, method=method)
                self.assertEqual((status, json.loads(raw)['code']), (401, 'AUTH_REQUIRED'))
            self.assertEqual(self.request(port, '/api/library', auth=True)[0], 200)
        self.assertFalse(self.upstream.calls)

    def test_unknown_static_path_and_traversal_never_serve_local_files(self):
        with running(make_server(self.store, self.upstream, port=0)) as port:
            for path in ['/console/api.py', '/console/../api.py', '/console/%2e%2e/api.py', '/console?token=secret']:
                status, _, raw = self.request(port, path, auth=True)
                self.assertIn(status, (400, 404))
                self.assertNotIn(b'"""', raw)

    def test_foreign_host_refused_for_public_page_and_authenticated_api(self):
        with running(make_server(self.store, self.upstream, port=0)) as port:
            for path in ['/console', '/api/library']:
                status, _, raw = self.request(port, path, auth=True, headers={'Host': 'attacker.example'})
                self.assertEqual((status, json.loads(raw)['code']), (403, 'HOST_NOT_ALLOWED'))

    def test_localhost_alias_works_on_loopback(self):
        with running(make_server(self.store, self.upstream, port=0)) as port:
            headers = {'Host': 'localhost:' + str(port), 'Origin': 'http://localhost:' + str(port)}
            self.assertEqual(self.request(port, '/console', headers=headers)[0], 200)
            self.assertEqual(self.request(port, '/api/library', headers=headers, auth=True)[0], 200)

    def test_foreign_or_null_origin_refused_before_any_upload(self):
        with running(make_server(self.store, self.upstream, port=0)) as port:
            for origin in ['http://attacker.example', 'null', 'http://127.0.0.1:1']:
                status, _, raw = self.request(port, '/api/uploads', method='POST', auth=True,
                                              headers={'Origin': origin}, body=b'not a file')
                self.assertEqual((status, json.loads(raw)['code']), (403, 'ORIGIN_NOT_ALLOWED'))
        self.assertFalse(self.upstream.calls)
        self.assertEqual(self.store.jobs(), [])

    def test_asset_query_and_post_cannot_change_state(self):
        with running(make_server(self.store, self.upstream, port=0)) as port:
            self.assertEqual(self.request(port, '/console/style.css?x=1', auth=True)[0], 400)
            self.assertEqual(self.request(port, '/console', method='POST', auth=True)[0], 404)
        self.assertEqual(self.store.jobs(), [])

    def test_symlink_and_hardlink_asset_refused(self):
        directory = Path(self.temp.name) / 'web'
        directory.mkdir()
        target = directory / 'index.html'
        outside = Path(self.temp.name) / 'private'
        outside.write_text('private bytes')
        import os
        for kind in ['symlink', 'hardlink']:
            if kind == 'symlink':
                target.symlink_to(outside)
            else:
                os.link(outside, target)
            with patch.object(web_assets, 'WEB', directory), running(make_server(self.store, self.upstream, port=0)) as port:
                status, _, raw = self.request(port, '/console')
                self.assertEqual((status, json.loads(raw)['code']), (503, 'SCREEN_ASSET_UNAVAILABLE'))
                self.assertNotIn(b'private bytes', raw)
            target.unlink()

    def test_missing_asset_fails_closed_and_api_still_operates(self):
        directory = Path(self.temp.name) / 'missing'
        with patch.object(web_assets, 'WEB', directory), running(make_server(self.store, self.upstream, port=0)) as port:
            self.assertEqual(self.request(port, '/console')[0], 503)
            self.assertEqual(self.request(port, '/api/jobs', auth=True)[0], 200)

    def test_static_read_cannot_enter_operator_or_workflow_action(self):
        # Current callbacks intentionally keep HTTP/1.0 no-Host compatibility.
        with running(make_server(self.store, self.upstream, port=0)) as port:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
            conn._http_vsn = 10
            conn._http_vsn_str = 'HTTP/1.0'
            conn.putrequest('GET', '/api/library', skip_host=True)
            conn.putheader('Authorization', 'Bearer ' + self.store.token_path.read_text().strip())
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read(); conn.close()
            self.assertEqual(self.request(port, '/console', headers={'Content-Length':'1'}, body=b'x')[0], 400)
        self.assertFalse(self.upstream.calls)

    def test_cli_status_reports_screen_without_initializing_or_changing_state(self):
        from apps.document_qa.tests.test_question_head import Fixture
        fixture = Fixture()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        before = {p.name: p.read_bytes() for p in fixture.root.iterdir() if p.is_file()}
        result = subprocess.run([sys.executable, '-B', '-m', 'apps.document_qa.api', 'status',
                                 '--state-dir', str(fixture.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(value['screen_implemented'])
        self.assertEqual((value['screen_path'], value['screen_version']), ('/console', 'q08.1'))
        self.assertTrue(value['screen_questions_implemented'])
        self.assertEqual(value['screen_question_policy_source'], '/api/question-policy')
        self.assertEqual(value['production_api_requests'], 0)
        self.assertFalse(value['inference_requested'])
        self.assertFalse(value['library']['question_ready'])
        self.assertEqual(value['question_head']['head_revision'], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in fixture.root.iterdir() if p.is_file()})


if __name__ == '__main__':
    unittest.main()
