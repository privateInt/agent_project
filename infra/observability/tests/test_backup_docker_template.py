"""Run the real Docker CLI formatter against a synthetic loopback Engine HTTP server.

No daemon/container is created or inspected. Set PREFLIGHT_DOCKER_TEST_BINARY to
an existing Docker CLI. The fixture is private and only serves synthetic JSON.
"""
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location('backup_preflight_template', Path(__file__).resolve().parents[1] / 'ops/backup_preflight.py')
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)
BINARY = os.environ.get('PREFLIGHT_DOCKER_TEST_BINARY')


@unittest.skipUnless(BINARY, 'Set PREFLIGHT_DOCKER_TEST_BINARY for real Docker template evaluation')
class NativeTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        self.ident = 'a' * 64
        self.value = {'Id': self.ident, 'Image': 'sha256:' + 'b' * 64,
                      'Config': {'Image': 'example@sha256:' + 'c' * 64, 'User': '1000:1000',
                                 'Labels': {'com.docker.compose.project': 'fixture', 'com.docker.compose.service': 'service',
                                            **{owner: 'fixture-owner' for owner in bp.OWNERS.values()}},
                                 'Env': ['DO_NOT_REPORT=synthetic-private-sentinel']},
                      'State': {'Status': 'running', 'StartedAt': '2026-09-28T00:00:00Z'}, 'Mounts': []}
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_HEAD(self):
                owner.calls.append(('HEAD', self.path))
                self.send_response(200); self.send_header('API-Version', '1.51'); self.end_headers()
            def do_GET(self):
                owner.calls.append(('GET', self.path))
                raw = json.dumps(owner.value).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def invoke(self, fmt):
        env = {k: v for k, v in os.environ.items() if not k.startswith('DOCKER_')}
        return subprocess.run([BINARY, '--host', 'tcp://127.0.0.1:' + str(self.server.server_port),
                               '--config', str(self.root / 'client'), 'container', 'inspect',
                               '--format', fmt, self.ident], capture_output=True, text=True, env=env, timeout=10)

    def test_all_component_formats_execute_without_secret_output(self):
        for component in bp.PROJECTS:
            with self.subTest(component=component):
                r = self.invoke(bp.container_format(component))
                self.assertEqual(r.returncode, 0, r.stderr)
                row = json.loads(r.stdout)
                self.assertEqual(row[0], self.ident)
                self.assertEqual(row[6:9], ['fixture', 'service', 'fixture-owner'])
                self.assertNotIn('synthetic-private-sentinel', r.stdout + r.stderr)
        self.assertTrue(all(method in ('HEAD', 'GET') for method, path in self.calls))

    def test_original_unparenthesized_expression_reproduces_failure(self):
        r = self.invoke('{{json index .Config.Labels "com.docker.compose.project"}}')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('template', r.stderr.lower())
        self.assertNotIn('synthetic-private-sentinel', r.stdout + r.stderr)


if __name__ == '__main__': unittest.main()
