"""Local lifecycle and HTTP acceptance tests; no Docker daemon or inference needed."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SPEC = importlib.util.spec_from_file_location('obs_manage', Path(__file__).resolve().parents[1] / 'ops/manage.py')
op = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(op)


def config(state):
    return {'services': {name: {
        'image': state['images'][name]['digest'], 'network_mode': 'host', 'read_only': True,
        'labels': {op.OWNER: state['config_id']},
        'tmpfs': [] if name == 'obs-node-exporter' else ['/tmp:size=16777216,mode=1777'],
        'command': ['--listen_ip=127.0.0.1' if name == 'obs-cadvisor'
                    else '--web.listen-address=127.0.0.1:' + str(op.PORTS[name])],
        'volumes': [{'source': state['data_dir'], 'target': '/prometheus'}],
    } for name in op.SERVICES}}


class FakeDocker:
    def __init__(self, root):
        self.root, self.calls, self.items = root, [], []
        self.fail_tag = None
        self.config_mutation = None

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[:1] == ['pull']:
            if args[1] == self.fail_tag:
                raise ValueError('simulated registry failure')
            return ''
        if args[:2] == ['image', 'inspect']:
            return json.dumps([{'RepoDigests': [args[2].rsplit(':', 1)[0] + '@sha256:' + 'a' * 64], 'Id': 'sha256:' + 'b' * 64}])
        if args[:2] == ['image', 'ls']:
            return 'present\n'
        if args[:2] == ['ps', '-aq']:
            return '\n'.join(str(i) for i in range(len(self.items)))
        if args[:1] == ['inspect']:
            return json.dumps(self.items)
        if args[:2] == ['ps', '--format']:
            return '\n'.join(json.dumps({'Names': x}) for x in ('agent-platform-agent-service-1', 'model-op-test', 'ops-mlflow'))
        if args[:1] == ['compose'] and 'config' in args:
            value = config(op.read_private(self.root / '.state.json'))
            if self.config_mutation:
                self.config_mutation(value)
            return json.dumps(value)
        if args[:1] in (['compose'], ['run']):
            return ''
        raise AssertionError(args)

    def json(self, args):
        return json.loads(self.run(args))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'src/infra'
        self.root.mkdir(parents=True)
        self.data = self.base / 'state/prometheus'
        self.host = {'docker_root': str(self.base / 'docker'), 'hostname': 'test-host',
                     'cpu_count': 2, 'memory_bytes': 8 * op.GIB}
        self.docker = FakeDocker(self.root)
        self.patches = [patch.object(op.Path, 'home', return_value=self.base),
                        patch.object(op, 'ROOT', self.root / 'project/source'),
                        patch.object(op, 'check_disks', return_value={'test': True}),
                        patch.object(op, 'check_ports')]
        # Keep project exclusion unrelated to this test's home path.
        self.patches[1] = patch.object(op, 'ROOT', Path('/opt/unrelated/project/src/infra/observability'))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def prepare(self):
        return op.prepare(self.docker, self.root, self.host, self.data)

    def test_prepare_persists_pins_and_reapply_never_pulls_tags_or_replaces_data(self):
        state = self.prepare()
        (self.data / 'existing-wal').write_text('keep')
        self.docker.calls.clear()
        again = self.prepare()
        self.assertEqual(state, again)
        self.assertFalse(any(c[0] == 'pull' for c in self.docker.calls))
        self.assertEqual((self.data / 'existing-wal').read_text(), 'keep')
        for path in (self.root / '.state.json', self.root / '.env', self.data.parent / '.owner.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_second_pull_resumes_with_original_owner_and_first_pin(self):
        self.docker.fail_tag = op.TAGS['obs-node-exporter']
        with self.assertRaisesRegex(ValueError, 'registry'):
            self.prepare()
        before = op.read_private(self.root / '.state.json')
        self.assertEqual(list(before['images']), ['obs-prometheus'])
        self.docker.calls.clear()
        self.docker.fail_tag = None
        after = self.prepare()
        self.assertEqual(before['config_id'], after['config_id'])
        self.assertEqual(before['images']['obs-prometheus'], after['images']['obs-prometheus'])
        self.assertNotIn(['pull', op.TAGS['obs-prometheus']], self.docker.calls)

    def test_existing_unowned_directory_and_containers_are_not_adopted(self):
        self.data.parent.mkdir()
        with self.assertRaisesRegex(ValueError, '자동 인수'):
            self.prepare()
        self.assertFalse((self.root / '.state.json').exists())
        self.docker.items = [{'Config': {'Labels': {'com.docker.compose.service': 'obs-cadvisor'}}}]
        with self.assertRaisesRegex(ValueError, '소유권'):
            op.project_containers(self.docker, 'other')

    def test_missing_or_changed_data_binding_never_recreates_storage(self):
        state = self.prepare()
        with self.assertRaisesRegex(ValueError, '변경할 수'):
            op.validate_state(state, self.host, self.base / 'elsewhere/prometheus')
        with self.assertRaisesRegex(ValueError, 'Docker 저장'):
            op.validate_state(state, {**self.host, 'docker_root': '/different'})
        self.data.rmdir()
        with self.assertRaisesRegex(ValueError, '재생성'):
            self.prepare()
        self.assertFalse(self.data.exists())

    def test_owner_permissions_symlink_and_image_changes_are_rejected(self):
        state = self.prepare()
        tampered = copy.deepcopy(state)
        tampered['images']['obs-prometheus']['digest'] = 'attacker/image:latest'
        with self.assertRaises(ValueError):
            op.validate_state(tampered, self.host)
        (self.data.parent / '.owner.json').chmod(0o644)
        with self.assertRaisesRegex(ValueError, '권한'):
            op.validate_state(state, self.host)
        (self.root / '.env').unlink()
        (self.root / '.env').symlink_to(self.root / '.state.json')
        with self.assertRaisesRegex(ValueError, '심볼릭'):
            op.config_check(self.docker, self.root, state)

    def test_effective_compose_rejects_foreign_service_and_public_listen(self):
        state = self.prepare()
        self.docker.config_mutation = lambda c: c['services'].update({'other': {}})
        with self.assertRaisesRegex(ValueError, '예상하지 않은'):
            op.config_check(self.docker, self.root, state)
        self.docker.config_mutation = lambda c: c['services']['obs-cadvisor'].update(command=['--listen_ip=0.0.0.0'])
        with self.assertRaisesRegex(ValueError, 'loopback'):
            op.config_check(self.docker, self.root, state)

    def test_split_tmpfs_is_rejected_before_starting_containers(self):
        state = self.prepare()
        for service in ('obs-prometheus', 'obs-cadvisor'):
            with self.subTest(service=service):
                self.docker.calls.clear()
                self.docker.config_mutation = lambda c: c['services'][service].update(
                    tmpfs=['/tmp:size=16777216', 'mode=1777'])
                with patch.object(op, 'host_info', return_value=self.host):
                    with self.assertRaisesRegex(ValueError, 'tmpfs'):
                        op.execute('up', self.docker, self.root)
                self.assertFalse(any(c[0] == 'run' or 'up' in c for c in self.docker.calls))
                self.assertEqual(op.read_private(self.root / '.state.json'), state)

    def test_retry_after_partial_create_keeps_original_pins_owner_and_data(self):
        state = self.prepare()
        (self.data / 'wal-fixture').write_text('original')
        self.docker.items = [{'Config': {'Labels': {
            op.OWNER: state['config_id'], 'com.docker.compose.service': 'obs-node-exporter'}},
            'State': {'Running': False}}]
        self.docker.calls.clear()
        with patch.object(op, 'host_info', return_value=self.host), patch.object(op, 'check', return_value={'result': 'passed'}):
            op.execute('up', self.docker, self.root)
        self.assertEqual(op.read_private(self.root / '.state.json'), state)
        self.assertEqual((self.data / 'wal-fixture').read_text(), 'original')
        self.assertFalse(any(c[0] == 'pull' or 'down' in c or 'rm' in c for c in self.docker.calls))
        ups = [c for c in self.docker.calls if c[0] == 'compose' and 'up' in c]
        self.assertEqual(len(ups), 1)
        self.assertEqual(ups[0][-3:], list(op.SERVICES))

    def test_up_and_stop_target_only_observation_project_and_check_is_read_only(self):
        self.prepare()
        with patch.object(op, 'host_info', return_value=self.host), patch.object(op, 'check', return_value={'result': 'passed'}):
            op.execute('up', self.docker, self.root)
            op.execute('stop', self.docker, self.root)
            before = len(self.docker.calls)
            op.execute('check', self.docker, self.root, wait_seconds=0)
        self.assertFalse(any(c[0] in ('pull', 'run') or 'up' in c or 'stop' in c for c in self.docker.calls[before:]))
        mutations = [c for c in self.docker.calls if c[0] == 'compose' and ('up' in c or 'stop' in c)]
        self.assertEqual(len(mutations), 2)
        for call in mutations:
            self.assertEqual(call[-3:], list(op.SERVICES))
            self.assertIn(op.PROJECT, call)
            self.assertEqual(call[call.index('-f') + 1], str(self.root / 'docker-compose.yml'))
        self.assertTrue((self.data.parent / '.owner.json').exists())

    def test_busy_port_refuses_new_stack(self):
        # Use a real bound TCP socket; no fixed test port assumptions.
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with patch.object(op, 'PORTS', {'obs-prometheus': listener.getsockname()[1]}):
                with self.assertRaisesRegex(ValueError, '사용 중'):
                    # Call the original function because setup patches normal prepare port probes.
                    self.patches[-1].temp_original([])

    def test_disk_guard_rejects_same_device_and_low_free_space(self):
        real_check = self.patches[2].temp_original
        docker = self.base / 'docker'
        docker.mkdir()
        with self.assertRaisesRegex(ValueError, '다른 파일시스템'):
            real_check(self.data, docker)
        class Stat:
            st_dev = 1
        class OtherStat:
            st_dev = 2
        original = op.Path.stat
        def different(path, *args, **kwargs):
            return OtherStat() if path == docker else original(path, *args, **kwargs)
        with patch.object(op.Path, 'stat', different), patch.object(op.shutil, 'disk_usage', return_value=op.shutil._ntuple_diskusage(100 * op.GIB, 90 * op.GIB, 10 * op.GIB)):
            with self.assertRaisesRegex(ValueError, '20 GiB'):
                real_check(self.data, docker)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.host = {'hostname': 'test-host', 'cpu_count': 2, 'memory_bytes': 8 * op.GIB, 'docker_root': '/docker'}
        self.required = {'agent-platform-agent-service-1', 'ops-mlflow'}
        def series(labels, value):
            return {'metric': labels, 'value': [now, str(value)]}
        self.queries = {
            'node_uname_info': [series({'nodename': 'test-host'}, 1)],
            'node_cpu_seconds_total': [series({'cpu': str(v)}, 0) for v in range(2)],
            'node_memory_MemTotal_bytes': [series({}, 8 * op.GIB)],
            'node_filesystem_avail_bytes': [series({'mountpoint': v}, 10 * op.GIB) for v in ('/', '/docker')],
        }
        for name in ('container_cpu_usage_seconds_total', 'container_memory_working_set_bytes', 'container_last_seen'):
            self.queries[name] = [series({'name': n}, now if name == 'container_last_seen' else 0) for n in self.required]
        self.targets = [{'labels': {'job': j}, 'health': 'up', 'lastScrape': dt.datetime.now(dt.timezone.utc).isoformat()}
                        for j in ('prometheus', 'host', 'containers')]
        test = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == '/api/v1/targets':
                    data = {'activeTargets': test.targets}
                elif parsed.path == '/api/v1/query':
                    query = parse_qs(parsed.query)['query'][0]
                    data = {'resultType': 'vector', 'result': test.queries[query.split('{')[0]]}
                else:
                    self.send_error(404)
                    return
                body = json.dumps({'status': 'success', 'data': data}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.prom = op.Prometheus('http://127.0.0.1:' + str(self.server.server_port))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def inspect(self):
        with patch.object(op, 'mountpoint', side_effect=lambda p: '/docker' if p == '/docker' else '/'):
            return op.inspect_metrics(self.prom, self.host, self.required, '/state')

    def test_real_http_validates_host_filesystems_and_named_container_metrics(self):
        result = self.inspect()
        self.assertEqual(result['result'], 'passed')
        self.assertEqual(result['platform_containers_verified'], 2)
        self.assertFalse(result['domain_success_assessed'])

    def test_nanosecond_scrape_passes_but_stale_and_future_scrapes_still_fail(self):
        now = dt.datetime.now(dt.timezone.utc)
        for item in self.targets:
            item['lastScrape'] = now.strftime('%Y-%m-%dT%H:%M:%S.%f') + '873Z'
        self.assertEqual(self.inspect()['result'], 'passed')
        for seconds in (-120, 120):
            with self.subTest(seconds=seconds):
                self.targets[0]['lastScrape'] = (now + dt.timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%S.%f') + '873+00:00'
                with self.assertRaisesRegex(ValueError, '정상 상태'):
                    self.inspect()

    def test_all_targets_up_without_container_data_is_not_a_pass(self):
        self.queries['container_cpu_usage_seconds_total'] = []
        with self.assertRaisesRegex(ValueError, '지표 누락'):
            self.inspect()

    def test_stale_container_host_name_and_missing_disk_are_rejected(self):
        row = self.queries['container_last_seen'][0]
        row['value'][1] = str(time.time() - 120)
        with self.assertRaisesRegex(ValueError, '지표 누락'):
            self.inspect()
        row['value'][1] = str(time.time())
        self.queries['node_filesystem_avail_bytes'].pop()
        with self.assertRaisesRegex(ValueError, '디스크'):
            self.inspect()
        self.queries['node_uname_info'][0]['metric']['nodename'] = 'container-host'
        with self.assertRaisesRegex(ValueError, '호스트 이름'):
            self.inspect()

    def test_old_or_down_scrape_is_rejected(self):
        self.targets[0]['health'] = 'down'
        with self.assertRaisesRegex(ValueError, '정상 상태'):
            self.inspect()
        self.targets[0]['health'] = 'up'
        self.targets[0]['lastScrape'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)).isoformat()
        with self.assertRaisesRegex(ValueError, '정상 상태'):
            self.inspect()


class EntrypointTests(unittest.TestCase):
    def test_original_default_and_n8n_only_modes_keep_existing_lifecycle(self):
        path = op.ROOT.parents[0] / 'ops/up.py'
        spec = importlib.util.spec_from_file_location('infra_original_entrypoint', path)
        entrypoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entrypoint)
        lifecycle = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'n8n').mkdir()
            with patch.object(entrypoint, 'ROOT', root), patch.dict(op.sys.modules, {'lifecycle': lifecycle}), patch.object(op.sys, 'path', list(op.sys.path)):
                for flags, expected in (([], False), (['--n8n-only'], True)):
                    with patch.object(op.sys, 'argv', ['up.py', *flags]):
                        self.assertEqual(entrypoint.main(), 0)
                    lifecycle.start.assert_called_with(n8n_only=expected)

    def test_observation_selection_does_not_load_n8n_or_other_infra(self):
        path = op.ROOT.parents[0] / 'ops/up.py'
        spec = importlib.util.spec_from_file_location('infra_entrypoint', path)
        entrypoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entrypoint)
        with patch.object(op.sys, 'argv', ['up.py', '--observability-only']), patch.object(entrypoint.subprocess, 'call', return_value=0) as call:
            self.assertEqual(entrypoint.main(), 0)
        call.assert_called_once_with([op.sys.executable, '-B', str(op.ROOT / 'ops/manage.py'), 'up'])

    def test_remote_context_rejected_before_reading_docker_host_paths(self):
        docker = unittest.mock.Mock()
        docker.run.return_value = 'remote'
        docker.json.return_value = [{'Endpoints': {'docker': {'Host': 'ssh://server'}}}]
        with patch.object(op.os, 'getuid', return_value=1006), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, '로컬'):
                op.host_info(docker)
        docker.json.assert_called_once_with(['context', 'inspect', 'remote'])


class ScrapeTimestampTests(unittest.TestCase):
    def test_exact_server_timestamp_and_equivalent_timezones(self):
        expected = dt.datetime(2026, 9, 26, 4, 39, 56, 158859, tzinfo=dt.timezone.utc).timestamp()
        for value in ('2026-09-26T04:39:56.158859873+00:00',
                      '2026-09-26T04:39:56.158859873Z',
                      '2026-09-26T13:39:56.158859873+09:00',
                      '2026-09-25T23:09:56.158859873-05:30'):
            with self.subTest(value=value):
                self.assertEqual(op.scrape_timestamp(value), expected)

    def test_zero_to_nine_fraction_digits_without_fromisoformat(self):
        class NoIsoformat(dt.datetime):
            @classmethod
            def fromisoformat(cls, value):
                raise AssertionError('Must not depend on Python ISO parser precision')
        with patch.object(op.dt, 'datetime', NoIsoformat):
            for digits in range(10):
                fraction = '123456789'[:digits]
                stamp = '2026-09-26T04:39:56' + ('.' + fraction if fraction else '') + 'Z'
                expected = NoIsoformat(2026, 9, 26, 4, 39, 56,
                                       int(fraction.ljust(6, '0')[:6]), tzinfo=dt.timezone.utc).timestamp()
                with self.subTest(digits=digits):
                    self.assertEqual(op.scrape_timestamp(stamp), expected)

    def test_invalid_or_timezone_free_values_are_not_treated_as_now(self):
        for value in (None, '', 'not-a-date', '2026-09-26T04:39:56',
                      '2026-09-26T04:39:56.1234567890Z', '2026-09-26T04:39:56+24:00',
                      '2026-09-26T04:39:56+00:60', '2026-02-30T04:39:56Z',
                      '2026-09-26T24:39:56Z'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'lastScrape'):
                    op.scrape_timestamp(value)


@unittest.skipUnless(os.environ.get('OBS_TEST_COMPOSE'), 'Set OBS_TEST_COMPOSE to a standalone Compose binary; no daemon required')
class ComposeRegressionTests(unittest.TestCase):
    def test_checked_in_yaml_has_one_tmpfs_and_original_yaml_fails_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'observability'
            shutil.copytree(op.ROOT, root, ignore=shutil.ignore_patterns('__pycache__', '.env', '.state.json', '.ops.lock'))
            state = {'config_id': '05ad1e64-2ae0-4d7a-92cf-42da5645d0b8',
                     'data_dir': '/home/fixture/.local/state/observation/prometheus',
                     'docker_root': '/media/simsys/SSD_EXT4/docker', 'uid': 1006, 'gid': 1006,
                     'images': {k: {'digest': v.rsplit(':', 1)[0] + '@sha256:' + 'a' * 64} for k, v in op.TAGS.items()}}
            (root / '.env').write_text(op.environment(state))

            class ComposeOnly:
                def run(self, args, **kwargs):
                    assert args[0] == 'compose' and args[-3:] == ['config', '--format', 'json']
                    env = {k: v for k, v in os.environ.items() if not k.startswith(('OBS_', 'COMPOSE_'))}
                    result = subprocess.run([os.environ['OBS_TEST_COMPOSE'], *args[1:]], env=env,
                                            capture_output=True, text=True, check=True)
                    return result.stdout

            docker = ComposeOnly()
            op.config_check(docker, root, state)
            resolved = json.loads(op.compose(docker, root, ['config', '--format', 'json']))
            for name in ('obs-prometheus', 'obs-cadvisor'):
                self.assertEqual(resolved['services'][name]['tmpfs'], ['/tmp:size=16777216,mode=1777'])
            # Exercise the original failure through the real YAML/Compose parser.
            file = root / 'prometheus/docker-compose.yml'
            text = file.read_text()
            fixed = 'tmpfs:\n      - "/tmp:size=16777216,mode=1777"'
            self.assertIn(fixed, text)
            file.write_text(text.replace(fixed, 'tmpfs: [/tmp:size=16777216,mode=1777]'))
            with self.assertRaisesRegex(ValueError, 'tmpfs'):
                op.config_check(docker, root, state)


if __name__ == '__main__':
    unittest.main()
