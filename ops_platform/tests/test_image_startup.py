"""Start the production entrypoint from runtime files, without host CLI/test modules."""
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import fastapi
import uvicorn


class ImageStartupTests(unittest.TestCase):
    def test_runtime_starts_without_ops_directory(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='ops-runtime-') as temporary:
            root = Path(temporary)
            app = root/'app'
            package = app/'ops_platform'
            package.mkdir(parents=True)
            for path in source.glob('*.py'):
                shutil.copy2(path, package/path.name)
            shutil.copytree(source/'web', package/'web')
            shutil.copytree(source/'profiles', package/'profiles')
            self.assertFalse((package/'ops').exists())
            self.assertFalse((package/'tests').exists())
            cache = root/'cache'
            cache.mkdir(mode=0o700)
            access = root/'access-key'
            key = 'synthetic-ops-startup-key-0123456789'
            access.write_text(key)
            access.chmod(0o600)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            config = {
                'schema': 1,
                'platforms': {name: {'url': 'http://127.0.0.1:1'}
                              for name in ('model', 'data', 'knowledge', 'agent')},
                'prometheus_url': 'http://127.0.0.1:1',
                'langfuse_url': 'http://127.0.0.1:1',
                'langfuse_file': str(root/'absent-langfuse-key'),
                'access_file': str(access), 'state_file': str(cache/'api-success.json'),
                'public_url': 'http://localhost:'+str(port), 'bind': '127.0.0.1',
                'port': port, 'timeout': 0.2, 'tls_cert': None, 'tls_key': None,
            }
            config['platforms']['agent']['token_file'] = str(root/'absent-agent-key')
            config_file = root/'config.json'
            config_file.write_text(json.dumps(config))
            env = dict(os.environ, OPS_CONFIG=str(config_file))
            # Preserve installed dependencies, never the checkout on PYTHONPATH.
            env['PYTHONPATH'] = os.pathsep.join(sorted({
                str(Path(module.__file__).resolve().parents[1])
                for module in (fastapi, uvicorn)
            }))

            def get(path, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
                try:
                    connection.request('GET', path, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            with (root/'process.log').open('w+') as log:
                process = subprocess.Popen([sys.executable, '-B', '-m', 'ops_platform.server'],
                                           cwd=app, env=env, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic()+10
                    while process.poll() is None and time.monotonic() < deadline:
                        try:
                            status, body = get('/healthz')
                            if status == 200:
                                break
                        except OSError:
                            pass
                        time.sleep(0.03)
                    else:
                        log.flush(); log.seek(0)
                        self.fail('Production entrypoint failed:\n'+log.read()[-4000:])
                    self.assertEqual(json.loads(body)['scope'], 'web_process_only')
                    health = subprocess.run([sys.executable, '-B', str(package/'healthcheck.py')],
                                            cwd=app, env=env, capture_output=True, text=True, timeout=5)
                    self.assertEqual(health.returncode, 0, health.stderr)
                    for path in ('/', '/app.js', '/style.css'):
                        self.assertEqual(get(path)[0], 200)
                    self.assertEqual(get('/api/catalog')[0], 401)
                    status, body = get('/api/catalog', {'Authorization': 'Bearer '+key})
                    self.assertEqual(status, 200)
                    self.assertEqual(len(json.loads(body)['platforms']), 4)
                    self.assertTrue((cache/'api-success.json').is_file())
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
