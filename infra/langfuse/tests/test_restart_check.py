"""Real HTTP readback, durable one-shot intent and container-scope failure cases."""
import contextlib
import copy
import hashlib
import http.server
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_langfuse import setup, output, common, manage, trace
from test_health_repair import FakeDocker as BindingDocker
import restart_check as restart


class RestartDocker(BindingDocker):
    def __init__(self, settings):
        super().__init__(settings)
        self.settings = settings
        self.mutations = []
        self.effect = 'all'
        self.failure = None
        self.after = None
        for name, item in self.items.items():
            item['Id'] = hashlib.sha256(name.encode()).hexdigest()
            item['Image'] = 'sha256:' + hashlib.sha256(('image-' + name).encode()).hexdigest()
            item['Config']['Env'] = ['HOSTNAME=0.0.0.0', 'PRIVATE_TEST_KEY=' + settings['secret_key']]
            item['HostConfig'] = {'RestartPolicy': {'Name': 'unless-stopped'}}
            item['State']['Health']['Status'] = 'healthy'
            item['State']['StartedAt'] = '2026-01-01T00:00:00.123456789Z'

    def run(self, args, **kwargs):
        if args[0] != 'restart':
            return super().run(args, **kwargs)
        self.calls.append(args)
        path = Path(self.settings['state_dir']) / restart.BASELINE_NAME
        saved = restart.read_baseline(path)  # Must exist BEFORE the external mutation.
        assert saved['baseline']['restart_intent'] is True
        assert args == ['restart', '--timeout', '60', *[self.items[x]['Id'] for x in common.SERVICES]]
        self.mutations.append(args)
        names = common.SERVICES if self.effect == 'all' else common.SERVICES[:2] if self.effect == 'partial' else []
        for name in names:
            self.items[name]['State']['StartedAt'] = trace.stamp(int(time.time() * 1000) + 10)
        if self.after:
            self.after()
        if self.failure:
            raise self.failure
        return '\n'.join(args[3:])


class ReadServer:
    def __init__(self, settings, record):
        self.settings, self.rows = settings, output(record)
        self.requests = []
        self.unavailable = False
        self.project = settings['project_id']

    def __enter__(self):
        fixture = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                fixture.requests.append(('GET', self.path))
                status = 503 if fixture.unavailable else 200
                if self.path.startswith('/api/public/health'):
                    body = {'version': manage.VERSION}
                elif self.path == '/api/public/ready':
                    body = {'status': 'OK'}
                else:
                    assert self.headers.get('Authorization', '').startswith('Basic ')
                    body = {'data': [{'id': fixture.project, 'organization': {'id': fixture.settings['org_id']}}]} \
                        if self.path == '/api/public/projects' else {'data': fixture.rows, 'meta': {}}
                self.send_response(status); self.end_headers(); self.wfile.write(json.dumps(body).encode())
            def do_POST(self):
                fixture.requests.append(('POST', self.path)); self.send_response(405); self.end_headers()
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.client = common.Client(self.settings, 'http://127.0.0.1:' + str(self.server.server_port))
        return self

    def __exit__(self, *args):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


class RestartTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td))
            record = trace.fixture(s); record.update(post_attempts=1, accepted=True)
            common.write_private(Path(td) / 'trace-check.json', record)
            docker = RestartDocker(s)
            with ReadServer(s, record) as server:
                runtime = restart.Runtime(docker, s, server.client)
                yield runtime, docker, server

    def execute(self, action, runtime):
        with contextlib.redirect_stdout(io.StringIO()):
            return restart.execute(action, runtime, 0)

    def test_six_original_ids_restart_once_http_readback_and_repeat_compare_preserved(self):
        with self.fixture() as (r, d, server):
            original = {p.name:p.read_bytes() for p in r.root.iterdir() if p.is_file()}
            first = self.execute('run', r)
            baseline = r.baseline_path.read_bytes()
            again = self.execute('run', restart.Runtime(d, manage.load(r.root), server.client))
            read = self.execute('compare', restart.Runtime(d, manage.load(r.root), server.client))
            for result in (first, again, read):
                self.assertEqual(result['result'], 'passed')
                self.assertEqual(result['services_restarted_since_baseline'], 6)
                self.assertEqual(result['observations_verified'], 3)
                self.assertFalse(result['post_submitted'])
                self.assertEqual(result['baseline_sha256'], first['baseline_sha256'])
            self.assertTrue(first['restart_requested_this_invocation'])
            self.assertFalse(read['restart_requested_this_invocation'])
            self.assertEqual(len(d.mutations), 1)
            self.assertEqual(r.baseline_path.read_bytes(), baseline)
            self.assertEqual(original, {name:(r.root/name).read_bytes() for name in original})
            self.assertTrue(all(method == 'GET' for method, _ in server.requests))
            self.assertFalse(r.baseline_path.stat().st_mode & 0o077)
            for key in ('password','secret_key','db_password'):
                self.assertNotIn(r.settings[key], baseline.decode())

    def test_compare_without_baseline_does_not_write_or_restart(self):
        with self.fixture() as (r, d, server):
            with self.assertRaises(ValueError): self.execute('compare', r)
            self.assertFalse(r.baseline_path.exists()); self.assertFalse(d.mutations)
            self.assertFalse(server.requests)

    def test_partial_or_missing_source_trace_blocks_before_baseline_or_restart(self):
        for condition in ('file', 'span'):
            with self.subTest(condition=condition), self.fixture() as (r, d, server):
                if condition == 'file': r.trace_path.unlink()
                else: server.rows.pop()
                with self.assertRaises((ValueError, FileNotFoundError)): self.execute('run', r)
                self.assertFalse(r.baseline_path.exists()); self.assertFalse(d.mutations)
                self.assertFalse(any(method == 'POST' for method, _ in server.requests))

    def test_foreign_container_image_mount_and_unhealthy_before_do_not_restart(self):
        for condition in ('owner','image','mount','unhealthy','replaced_storage'):
            with self.subTest(condition=condition), self.fixture() as (r, d, server):
                if condition == 'owner': d.items['lf-web']['Config']['Labels'][common.OWNER] = 'foreign'
                elif condition == 'image': d.items['lf-web']['Config']['Image'] = 'untrusted:latest'
                elif condition == 'mount': d.items['lf-postgres']['Mounts'][0]['Source'] = '/foreign'
                elif condition == 'unhealthy': d.items['lf-web']['State']['Health']['Status'] = 'unhealthy'
                else:
                    p=r.root/'data/redis'; p.rename(r.root/'data/old-redis'); p.mkdir()
                with self.assertRaises(ValueError): self.execute('run', r)
                self.assertFalse(r.baseline_path.exists()); self.assertFalse(d.mutations)

    def test_lost_restart_response_checks_existing_effect_without_repeat(self):
        for failure in (common.DockerFailure(['restart'], 1), subprocess.TimeoutExpired(['docker','restart'], 1)):
            with self.subTest(failure=type(failure).__name__), self.fixture() as (r, d, server):
                d.failure = failure
                result = self.execute('run', r)
                self.assertEqual(result['result'], 'passed')
                self.assertTrue(result['restart_command_error_this_invocation'])
                self.assertEqual(self.execute('run', r)['result'], 'passed')
                self.assertEqual(len(d.mutations), 1)

    def test_crash_after_durable_intent_before_restart_is_not_retried_or_passed(self):
        with self.fixture() as (r, d, server):
            with patch.object(r, 'restart', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                self.execute('run', r)
            baseline=r.baseline_path.read_bytes()
            for action in ('run','compare'):
                result=self.execute(action,r)
                self.assertEqual(result['result'],'pending')
                self.assertEqual(result['services_restarted_since_baseline'],0)
            self.assertFalse(d.mutations); self.assertEqual(baseline,r.baseline_path.read_bytes())

    def test_partial_restart_remains_pending_and_does_not_restart_remaining(self):
        with self.fixture() as (r, d, server):
            d.effect='partial'
            first=self.execute('run',r)
            self.assertEqual(first['result'],'pending')
            self.assertEqual(first['services_restarted_since_baseline'],2)
            self.assertEqual(self.execute('run',r)['result'],'pending')
            self.assertEqual(len(d.mutations),1)

    def test_missing_trace_after_restart_never_reposts_and_read_can_recover(self):
        with self.fixture() as (r, d, server):
            original=copy.deepcopy(server.rows)
            d.after=lambda: server.rows.clear()
            self.assertEqual(self.execute('run',r)['result'],'pending')
            baseline=r.baseline_path.read_bytes()
            self.assertEqual(self.execute('compare',r)['result'],'pending')
            server.rows=original
            self.assertEqual(self.execute('compare',r)['result'],'passed')
            self.assertEqual(len(d.mutations),1); self.assertEqual(baseline,r.baseline_path.read_bytes())
            self.assertTrue(all(method=='GET' for method,_ in server.requests))

    def test_source_field_corruption_never_passes(self):
        for field,value in [('parentObservationId','wrong'),('level','DEFAULT'),('metadata',{'infra_owner':'wrong'}),
                            ('metadata_extra', 'changed'),('endTime','2026-01-01T00:00:00Z'),('input','private')]:
            with self.subTest(field=field), self.fixture() as (r, d, server):
                def mutate():
                    if field=='metadata_extra': server.rows[2]['metadata']['extra']=value
                    else: server.rows[2][field]=value
                d.after=mutate
                with self.assertRaises(ValueError): self.execute('run',r)
                self.assertEqual(len(d.mutations),1)
                with self.assertRaises(ValueError): self.execute('compare',r)
                self.assertEqual(len(d.mutations),1)

    def test_changed_container_or_original_file_rejected_on_compare(self):
        for condition in ('id','image_id','config','trace_file','baseline'):
            with self.subTest(condition=condition), self.fixture() as (r, d, server):
                self.execute('run',r)
                if condition=='id': d.items['lf-web']['Id']='a'*64
                elif condition=='image_id': d.items['lf-web']['Image']='sha256:'+'b'*64
                elif condition=='config': d.items['lf-web']['Config']['Env'].append('OTHER=changed')
                elif condition=='trace_file': r.trace_path.write_text(r.trace_path.read_text()+'\n')
                else:
                    record=common.read_json(r.baseline_path);record['baseline']['intent_ms']-=1
                    common.write_private(r.baseline_path,record)
                with self.assertRaises(ValueError): self.execute('compare',r)
                self.assertEqual(len(d.mutations),1)

    def test_readiness_or_project_failure_after_restart_is_not_acceptance(self):
        for condition in ('http503','project'):
            with self.subTest(condition=condition), self.fixture() as (r, d, server):
                def mutate():
                    if condition=='http503': server.unavailable=True
                    else: server.project='other-project'
                d.after=mutate
                self.assertEqual(self.execute('run',r)['result'],'pending')
                self.assertEqual(len(d.mutations),1)

    def test_cli_compare_uses_existing_state_and_exit_two_for_pending(self):
        with self.fixture() as (r, d, server):
            d.effect='none';self.execute('run',r)
            with patch.object(restart,'Docker',return_value=d), patch.object(manage,'host_info',return_value=r.settings['host']), \
                 patch.object(restart,'Client',return_value=server.client), contextlib.redirect_stdout(io.StringIO()):
                code=restart.main(['compare','--state-dir',str(r.root),'--wait-seconds','0'])
            self.assertEqual(code,2);self.assertEqual(len(d.mutations),1)


if __name__ == '__main__': unittest.main()
