"""Restart boundaries, retained raw samples and lost-response handling; no Docker daemon."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

OPS = Path(__file__).resolve().parents[1] / 'ops'
sys.path.insert(0, str(OPS))
import restart_check as check
op = check.op


def stamp(value):
    # Docker RFC3339Nano, including the older-Python regression case.
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + '%09dZ' % (int(value % 1 * 1000000) * 1000)


class Prom:
    def __init__(self):
        self.start = time.time() - 300
        self.scrape = time.time()
        self.saved = {}
        self.requests = []
        self.change = None

    def get(self, path):
        self.requests.append(path)
        if path == '/api/v1/status/runtimeinfo':
            return {'startTime': stamp(self.start)}
        if path == '/api/v1/targets':
            return {'activeTargets': [{'lastScrape': stamp(self.scrape), 'health': 'up'} for _ in range(3)]}
        args = parse_qs(urlsplit(path).query)
        query, end = args['query'][0], args['time'][0]
        key = (query, end)
        if key not in self.saved:
            jobs = ('prometheus', 'host', 'containers') if query == check.QUERIES[0] else ('host',)
            self.saved[key] = [{'metric': {'job': job, 'instance': '127.0.0.1'},
                                'values': [[float(end) - 60, '1'], [float(end) - 30, '1']]}
                               for job in jobs]
        result = copy.deepcopy(self.saved[key])
        if self.change:
            self.change(query, result)
        return {'resultType': 'matrix', 'result': result}

    def query(self, expression):
        assert expression == 'process_start_time_seconds{job="prometheus"}'
        return [{'metric': {'job': 'prometheus'}, 'value': [time.time(), str(self.start)]}]


class Runtime:
    def __init__(self, root):
        self.state = {'data_dir': str(root / 'prometheus')}
        self.prom = Prom()
        self.bound = {'data_dir': self.state['data_dir'], 'owner': 'fixture'}
        self.items = {name: {'id': str(i + 1) * 64, 'image_id': 'sha256:' + 'a' * 64,
                             'started_at': stamp(time.time() - 300), 'running': True, 'health': 'healthy'}
                      for i, name in enumerate(op.SERVICES)}
        self.effects = []
        self.failure = None
        self.baseline_path = root / 'baseline.json'

    def binding(self):
        return copy.deepcopy(self.bound)

    def containers(self):
        return copy.deepcopy(self.items)

    def expected_names(self):
        return ['agent-platform-agent-service-1']

    def metrics(self, names):
        assert names == self.expected_names()
        return {'targets_up': 3, 'platform_containers_verified': 1}

    def restart(self, containers):
        # A readable, durable intent must already exist before submitting the effect.
        assert check.read_record(self.baseline_path)['restart']['command_returned'] is False
        self.effects.append(copy.deepcopy(containers))
        if self.failure == 'before':
            raise subprocess.TimeoutExpired('docker', 180)
        now = time.time()
        for item in self.items.values():
            item['started_at'] = stamp(now)
        self.prom.start, self.prom.scrape = now, now + 0.01
        if self.failure == 'after':
            raise subprocess.TimeoutExpired('docker', 180)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = Runtime(self.root)
        self.path = self.runtime.baseline_path

    def tearDown(self):
        self.temp.cleanup()

    def action(self, name):
        return check.run_action(self.runtime, name, self.path, wait_seconds=0)

    def test_normal_run_replay_compare_preserve_original_private_baseline(self):
        first = self.action('run')
        saved = self.path.read_bytes()
        for action in ('run', 'compare', 'capture'):
            result = self.action(action)
            self.assertEqual(result['baseline_sha256'], first['baseline_sha256'])
            self.assertEqual(saved, self.path.read_bytes())
        self.assertEqual(len(self.runtime.effects), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(first['historical_samples_preserved'])
        self.assertFalse(first['inference_executed'])
        self.assertFalse(first['domain_services_restarted_by_this_tool'])

    def test_capture_compare_without_restart_never_mutate_remote(self):
        self.action('capture')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '재시작 요청 기록'):
            self.action('compare')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.runtime.effects, [])

    def test_missing_compare_does_not_create_baseline(self):
        with self.assertRaises(FileNotFoundError):
            self.action('compare')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.runtime.effects, [])

    def test_lost_response_after_effect_passes_without_second_restart(self):
        self.runtime.failure = 'after'
        self.assertEqual(self.action('run')['result'], 'passed')
        self.assertFalse(check.read_record(self.path)['restart']['command_returned'])
        self.assertEqual(self.action('run')['result'], 'passed')
        self.assertEqual(len(self.runtime.effects), 1)

    def test_lost_response_before_effect_stays_pending_and_never_resubmits(self):
        self.runtime.failure = 'before'
        for action in ('run', 'run', 'compare'):
            with self.assertRaisesRegex(ValueError, '재시작 완료'):
                self.action(action)
        self.assertEqual(len(self.runtime.effects), 1)

    def test_changed_binding_or_containers_rejected_before_restart(self):
        self.action('capture')
        self.runtime.bound['owner'] = 'other'
        with self.assertRaisesRegex(ValueError, '소유권'):
            self.action('run')
        self.runtime.bound['owner'] = 'fixture'
        self.runtime.items[op.SERVICES[0]]['id'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, '이미 재시작/변경'):
            self.action('run')
        self.assertEqual(self.runtime.effects, [])

    def test_recreated_container_cannot_pass_healthy_after_restart(self):
        self.action('run')
        self.runtime.items[op.SERVICES[0]]['id'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, '교체'):
            self.action('compare')

    def test_changed_value_label_timestamp_or_missing_series_cannot_pass(self):
        self.action('run')
        def value(query, rows): rows[0]['values'][0][1] = '2'
        def label(query, rows): rows[0]['metric']['instance'] = 'changed'
        def timestamp(query, rows): rows[0]['values'][0][0] += 0.001
        def missing(query, rows): rows.pop()
        for change in (value, label, timestamp, missing):
            with self.subTest(change=change.__name__):
                self.runtime.prom.change = change
                with self.assertRaisesRegex(ValueError, '과거 시계열'):
                    self.action('compare')
        self.assertEqual(len(self.runtime.effects), 1)

    def test_expired_history_rejected_before_restart(self):
        self.action('capture')
        self.runtime.prom.change = lambda query, rows: rows.clear()
        with self.assertRaisesRegex(ValueError, '과거 시계열'):
            self.action('run')
        self.assertEqual(self.runtime.effects, [])

    def test_stale_scrapes_and_old_process_cannot_pass(self):
        self.action('run')
        original = self.runtime.prom.scrape
        self.runtime.prom.scrape -= 10
        with self.assertRaisesRegex(ValueError, '새 scrape'):
            self.action('compare')
        self.runtime.prom.scrape = original
        self.runtime.prom.start -= 600
        with self.assertRaisesRegex(ValueError, '새 Prometheus'):
            self.action('compare')

    def test_insufficient_initial_scrapes_rejected_before_baseline_or_restart(self):
        self.runtime.prom.change = lambda query, rows: [r['values'].pop() for r in rows]
        with self.assertRaisesRegex(ValueError, '기록이 부족'):
            self.action('run')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.runtime.effects, [])

    def test_baseline_modification_and_symlink_rejected(self):
        self.action('capture')
        record = json.loads(self.path.read_text())
        record['baseline']['expected_names'] = []
        self.path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, '변경됐거나'):
            self.action('run')
        linked = self.root / 'link.json'
        linked.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, '심볼릭'):
            check.run_action(self.runtime, 'run', linked, 0)
        self.assertEqual(self.runtime.effects, [])

    def test_fixed_time_raw_matrix_sorting_and_window_validation(self):
        end = str(time.time() - 15)
        original = check.history(self.runtime.prom, end)
        self.runtime.prom.change = lambda query, rows: rows.reverse()
        self.assertEqual(check.history(self.runtime.prom, end), original)
        for request in self.runtime.prom.requests:
            self.assertEqual(parse_qs(urlsplit(request).query)['time'], [end])
            self.assertTrue(request.startswith('/api/v1/query?'))
        def outside(query, rows): rows[0]['values'][0][0] = float(end) - 180
        self.runtime.prom.change = outside
        with self.assertRaisesRegex(ValueError, '고정 조회 구간'):
            check.history(self.runtime.prom, end)

    def test_command_uses_exact_three_owned_ids_only(self):
        calls = []
        docker = type('Docker', (), {'run': lambda _, args, **kw: calls.append((args, kw))})()
        runtime = object.__new__(check.Runtime)
        runtime.docker = docker
        runtime.restart(self.runtime.items)
        self.assertEqual(calls, [(['restart', '--time', '30', *[self.runtime.items[n]['id'] for n in op.SERVICES]], {'timeout': 180})])

    def test_missing_runtime_start_is_not_replaced_with_current_time(self):
        get = self.runtime.prom.get
        with patch.object(self.runtime.prom, 'get', side_effect=lambda p: {} if p == '/api/v1/status/runtimeinfo' else get(p)):
            with self.assertRaises(KeyError):
                self.action('run')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.runtime.effects, [])

    def test_actual_container_pins_and_tsdb_mount_are_validated(self):
        runtime = object.__new__(check.Runtime)
        runtime.docker = None
        runtime.state = {'config_id': 'fixture', 'data_dir': '/private/prometheus',
                         'images': {name: {'image_id': 'sha256:' + 'a' * 64, 'digest': 'image@sha256:' + 'b' * 64}
                                    for name in op.SERVICES}}
        items = [{'Id': str(i + 1) * 64, 'Image': 'sha256:' + 'a' * 64,
                  'Config': {'Image': 'image@sha256:' + 'b' * 64, 'Labels': {'com.docker.compose.service': name}},
                  'Mounts': [{'Destination': '/prometheus', 'Source': '/private/prometheus', 'RW': True, 'Type': 'bind'}],
                  'State': {'Running': True, 'Health': {'Status': 'healthy'}, 'StartedAt': stamp(time.time() - 300)}}
                 for i, name in enumerate(op.SERVICES)]
        with patch.object(op, 'project_containers', return_value=items):
            self.assertEqual(set(runtime.containers()), set(op.SERVICES))
            items[0]['Mounts'][0]['Source'] = '/wrong'
            with self.assertRaisesRegex(ValueError, 'TSDB'):
                runtime.containers()
            items[0]['Mounts'][0]['Source'] = '/private/prometheus'
            items[1]['Image'] = 'sha256:' + 'c' * 64
            with self.assertRaisesRegex(ValueError, '고정 이미지'):
                runtime.containers()


if __name__ == '__main__':
    unittest.main()
