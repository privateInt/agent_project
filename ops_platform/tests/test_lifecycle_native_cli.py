"""Native Docker formatting against a synthetic loopback Engine; no Docker daemon.

The test runner substitutes only the Unix transport with loopback TCP, because
the local test environment cannot create Unix sockets. Production rejects TCP.
"""
from copy import deepcopy
import http.server
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit

from ops_platform.ops import lifecycle_probe as probe, lifecycle_state as state
from ops_platform.tests import test_auto_refresh as fixtures

BINARY = os.environ.get('PREFLIGHT_DOCKER_TEST_BINARY')


@unittest.skipUnless(BINARY, 'opt-in native Docker CLI; synthetic Engine and Compose version')
class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.PreflightTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups); self.fixture = fixture
        self.calls = []; self.home = fixture.f.root / 'client-home'; self.home.mkdir()
        raw = fixture.commands.raw
        labels = {'com.docker.compose.project': raw['project'], 'com.docker.compose.service': 'ops-web',
                  'org.agent-project.ops.owner': raw['owner']}
        self.container = {'Id': raw['id'], 'Image': raw['image'], 'Created': raw['created'],
            'Config': {'Labels': labels, 'User': raw['user'], 'Env': ['PRIVATE=do-not-return-this-sentinel']},
            'HostConfig': {'NetworkMode': raw['network'], 'ReadonlyRootfs': True},
            'State': {'StartedAt': raw['started'], 'Running': True, 'Health': {'Status': 'healthy'}}, 'Mounts': raw['mounts']}
        outer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_HEAD(self):
                outer.calls.append(('HEAD', self.path)); self.send_response(200)
                self.send_header('API-Version', '1.51'); self.end_headers()
            def do_GET(self):
                outer.calls.append(('GET', self.path)); path = urlsplit(self.path).path
                if path.endswith('/info'):
                    value = {'ID': 'fixture-daemon', 'OSType': 'linux', 'SecurityOptions': ['name=seccomp'], 'ServerVersion': '28.4.0'}
                elif path.endswith('/containers/json'): value = [{'Id': outer.container['Id'], 'Names': ['/synthetic-ops']}]
                elif path.endswith('/containers/' + outer.container['Id'] + '/json'): value = outer.container
                else:
                    self.send_error(404); return
                data = json.dumps(value).encode(); self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def test_native_pinned_info_list_inspect_without_environment_or_writes(self):
        calls = []
        def invoke(args, **kwargs):
            calls.append(args)
            if args[-3:] == ['compose', 'version', '--short']:
                return SimpleNamespace(returncode=0, stdout='2.39.0', stderr='')  # No Compose plugin installed in this fixture.
            self.assertEqual(args[1:3], ['--host', 'unix:///synthetic-only.sock'])
            translated = [args[0], '--host', 'tcp://127.0.0.1:' + str(self.server.server_port), *args[3:]]
            return subprocess.run(translated, **kwargs)
        reader = probe.Reader(env={'HOME': str(self.home), 'DOCKER_HOST': 'unix:///synthetic-only.sock'}, runner=invoke,
                              which=lambda name, **kw: BINARY if name == 'docker' else None)
        r = probe.docker(reader, self.fixture.f.directory, self.fixture.f.cfg, self.fixture.f.owner)
        self.assertEqual(r['status'], 'ok'); self.assertEqual(r['ops']['status'], 'ok')
        self.assertTrue(r['ops']['owner_and_mounts_verified']); self.assertEqual(list(self.home.iterdir()), [])
        self.assertNotIn('do-not-return-this-sentinel', json.dumps(r))
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(method in ('GET', 'HEAD') for method, _ in self.calls))
        self.assertTrue(any('/containers/json?' in path for _, path in self.calls))
        self.assertTrue(any(path.endswith('/info') for _, path in self.calls))

    def test_native_current_context_resolution_uses_no_context_mutations(self):
        reader = probe.Reader(env={'HOME': str(self.home)}, which=lambda name, **kw: BINARY if name == 'docker' else None)
        context = reader.query('docker', ['context', 'show'])
        endpoint = reader.query('docker', ['context', 'inspect', context, '--format', probe.CONTEXT])
        self.assertEqual(context, 'default'); self.assertTrue(endpoint.startswith('unix:///'))
        self.assertEqual(list(self.home.iterdir()), [])


if __name__ == '__main__': unittest.main()
