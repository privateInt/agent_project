"""GPU metrics, ownership and one-time Prometheus transition boundaries; no GPU required."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import gpu
from test_restart_check import Runtime as FakeRuntime

op = gpu.op
UUID = 'GPU-12345678-1234-1234-1234-123456789abc'
DEVICES = {UUID: {'name': 'NVIDIA GeForce RTX 4090', 'driver': '580.126.09', 'memory_total_mib': 24564.0}}
VALUES = dict(zip(gpu.FIELDS, (0, 24564, 4096, 20468, 45, 75.5)))


def text_metrics():
    return '\n'.join('%s{gpu="0",UUID="%s",device="nvidia0",modelName="NVIDIA GeForce RTX 4090"} %s'
                     % (metric, UUID, VALUES[field]) for field, metric in zip(gpu.FIELDS, gpu.METRICS))


class MetricTests(unittest.TestCase):
    def test_inventory_only_uses_bounded_read_only_query(self):
        calls = []
        def run(args, **kw):
            calls.append((args, kw))
            return subprocess.CompletedProcess(args, 0, UUID + ', NVIDIA GeForce RTX 4090, 580.126.09, 24564\n')
        self.assertEqual(gpu.inventory(run), DEVICES)
        self.assertEqual(calls[0][0], ['nvidia-smi', '--query-gpu=uuid,name,driver_version,memory.total', '--format=csv,noheader,nounits'])
        self.assertEqual(calls[0][1]['timeout'], 15)

    def test_inventory_error_empty_unknown_and_duplicate_rejected(self):
        row = UUID + ', RTX 4090, 580.126.09, 24564\n'
        for code, text in ((1, ''), (0, ''), (0, row * 2), (0, row.replace('24564', 'N/A'))):
            with self.subTest(code=code, text=text), self.assertRaises(ValueError):
                gpu.inventory(lambda *a, **k: subprocess.CompletedProcess([], code, text))

    def test_valid_zero_utilization_is_preserved_with_units(self):
        report = gpu.validate_metrics(gpu.parse_metrics(text_metrics()), DEVICES)
        self.assertEqual(report[UUID], VALUES)

    def test_sentinel_nan_inf_negative_and_missing_never_become_zero(self):
        for value in ('9223372036854775794', 'NaN', '+Inf', '-1'):
            text = text_metrics().replace('} 45', '} ' + value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid_or_unsupported'):
                gpu.validate_metrics(gpu.parse_metrics(text), DEVICES)
        text = '\n'.join(text_metrics().splitlines()[:-1])
        with self.assertRaisesRegex(ValueError, 'missing_or_unsupported'):
            gpu.validate_metrics(gpu.parse_metrics(text), DEVICES)

    def test_duplicate_or_foreign_gpu_identity_rejected(self):
        for text in (text_metrics() + '\n' + text_metrics().splitlines()[0], text_metrics().replace(UUID, 'GPU-other')):
            with self.assertRaisesRegex(ValueError, 'UUID'):
                gpu.validate_metrics(gpu.parse_metrics(text), DEVICES)

    def test_vram_disagreement_is_not_healthy(self):
        text = text_metrics().replace('} 24564', '} 8192')
        with self.assertRaisesRegex(ValueError, 'VRAM'):
            gpu.validate_metrics(gpu.parse_metrics(text), DEVICES)

    def test_label_escaping_and_duplicate_labels(self):
        text = text_metrics().replace('NVIDIA GeForce RTX 4090', 'name\\"with\\nline')
        gpu.validate_metrics(gpu.parse_metrics(text), DEVICES)
        with self.assertRaisesRegex(ValueError, '라벨'):
            gpu.parse_metrics(text_metrics().replace('gpu="0"', 'gpu="0",gpu="1"'))


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        source = Path(__file__).resolve().parents[1]
        for name in ('docker-compose.yml', 'prometheus/prometheus.yml', 'gpu/docker-compose.yml',
                     'gpu/basic.csv', 'gpu/prometheus.yml', 'gpu/prometheus-overlay.yml'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        self.runtime = FakeRuntime(self.root)
        self.runtime.root = self.root
        self.runtime.state.update(config_id='owner', images={'obs-prometheus': {'digest': 'prom@sha256:' + 'a' * 64}})
        op.write_private(self.root / '.state.json', self.runtime.state)
        op.write_private(self.root / '.env', 'original environment')
        self.state = {'schema': 1, 'config_id': 'owner', 'enabled': False, 'devices': DEVICES,
                      'image': {'tag': gpu.TAG, 'digest': gpu.IMAGE, 'image_id': 'sha256:' + 'a' * 64}}
        gpu.retained.write_record(self.root / 'gpu/.state.json', self.state)
        self.path = self.root / 'transition.json'
        self.source = str(self.root / 'prometheus/prometheus.yml')
        self.calls = []
        self.docker = type('Docker', (), {'run': lambda _, args, **kw: self.calls.append(args)})()
        self.record = gpu.transition(self.runtime, self.path)
        self.patches = [patch.object(gpu, 'config_source', side_effect=lambda *a: self.source),
                        patch.object(op, 'config_check'), patch.object(op, 'compose', side_effect=self.compose)]
        for p in self.patches: p.start()
        self.fail = None

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def compose(self, docker, root, args, **kw):
        self.assertTrue(gpu.transition_read(self.path)['prometheus_change_requested'])
        self.calls.append(args)
        if self.fail == 'before': raise OSError('response lost')
        self.source = str(self.root / 'gpu/prometheus.yml')
        self.runtime.items['obs-prometheus']['id'] = 'f' * 64
        if self.fail == 'after': raise OSError('response lost')

    def attach(self):
        gpu.attach(self.docker, self.root, self.runtime, self.state, self.record, self.path)

    def test_transition_only_recreates_prometheus_once(self):
        before = copy.deepcopy(self.record['before'])
        self.attach()
        self.attach()
        effects = [call for call in self.calls if call[0] == 'up']
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][-1], 'obs-prometheus')
        self.assertIn('--no-deps', effects[0])
        self.assertEqual(gpu.transition_read(self.path)['before'], before)
        self.assertTrue(op.gpu_enabled(self.root))

    def test_lost_response_after_effect_resumes_without_recreation(self):
        self.fail = 'after'
        self.attach()
        self.record = gpu.transition_read(self.path)
        self.attach()
        self.assertEqual(len([c for c in self.calls if c[0] == 'up']), 1)

    def test_lost_response_before_effect_does_not_submit_again(self):
        self.fail = 'before'
        self.attach()
        with self.assertRaisesRegex(ValueError, '불확실'):
            self.attach()
        self.assertEqual(len([c for c in self.calls if c[0] == 'up']), 1)

    def test_storage_and_auxiliary_changes_refuse_mutation(self):
        self.runtime.bound['owner'] = 'other'
        with self.assertRaisesRegex(ValueError, 'TSDB'):
            self.attach()
        self.runtime.bound['owner'] = 'fixture'
        self.runtime.items['obs-cadvisor']['id'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, '수집기'):
            self.attach()
        self.assertEqual(self.calls, [])

    def test_foreign_prometheus_configuration_is_not_adopted(self):
        self.source = '/another/prometheus.yml'
        with self.assertRaisesRegex(ValueError, '알 수 없는'):
            self.attach()
        self.assertEqual(self.calls, [])

    def test_changed_collector_configuration_refuses_transition(self):
        path = self.root / 'gpu/prometheus.yml'
        path.write_text(path.read_text() + '\n# changed after baseline\n')
        with self.assertRaisesRegex(ValueError, '변경'):
            self.attach()
        self.assertEqual(self.calls, [])

    def test_changed_gpu_driver_uuid_and_image_are_not_adopted(self):
        for field in ('devices', 'image'):
            state = copy.deepcopy(self.state)
            if field == 'devices': state[field][UUID]['driver'] = '999.00'
            else: state[field]['digest'] = 'foreign@sha256:' + 'b' * 64
            with self.assertRaises(ValueError):
                gpu.validate_state(state, self.runtime.state, DEVICES)

    def test_changed_baseline_hash_is_rejected(self):
        self.record['before']['query_time'] = '0'
        gpu.retained.write_record(self.path, self.record)
        with self.assertRaisesRegex(ValueError, '변경'):
            gpu.transition(self.runtime, self.path)


class ComposeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('OBS_TEST_COMPOSE'), 'Set OBS_TEST_COMPOSE for real Compose validation')
    def test_actual_compose_gpu_permissions_and_optional_prometheus_mount(self):
        src = Path(__file__).resolve().parents[1]
        binary = os.environ['OBS_TEST_COMPOSE']
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'observability'
            shutil.copytree(src, root, ignore=shutil.ignore_patterns('__pycache__'))
            base = {'config_id': 'owner', 'uid': 1006, 'gid': 1006, 'data_dir': '/tmp/original-tsdb',
                    'docker_root': '/tmp/docker', 'images': {n: {'digest': 'image@sha256:' + 'a' * 64} for n in op.SERVICES}}
            state = {'schema': 1, 'config_id': 'owner', 'enabled': False, 'devices': DEVICES,
                     'image': {'digest': gpu.IMAGE, 'tag': gpu.TAG, 'image_id': 'sha256:' + 'a' * 64}}
            op.write_private(root / '.state.json', base)
            op.write_private(root / '.env', op.environment(base))
            op.write_private(root / 'gpu/.state.json', state)
            op.write_private(root / 'gpu/.env', gpu.environment(state))
            class Docker:
                def run(self, args, **kw):
                    return subprocess.check_output([binary, *args[1:]], text=True)
            docker = Docker()
            gpu.config_check(docker, root, state)
            before = json.loads(op.compose(docker, root, ['config', '--format', 'json']))
            state['enabled'] = True
            op.write_private(root / 'gpu/.state.json', state)
            after = json.loads(op.compose(docker, root, ['config', '--format', 'json']))
            self.assertEqual(set(after['services']), set(op.SERVICES))
            for service in ('obs-cadvisor', 'obs-node-exporter'):
                self.assertEqual(before['services'][service], after['services'][service])
            old, new = before['services']['obs-prometheus'], after['services']['obs-prometheus']
            for mount in new['volumes']:
                if mount['target'] == '/etc/prometheus/prometheus.yml':
                    self.assertEqual(mount['source'], str(root / 'gpu/prometheus.yml'))
                    mount['source'] = str(root / 'prometheus/prometheus.yml')
            self.assertEqual(old, new)
            yaml = (root / 'gpu/docker-compose.yml').read_text()
            (root / 'gpu/docker-compose.yml').write_text(yaml.replace('127.0.0.1:16313', '0.0.0.0:16313'))
            with self.assertRaisesRegex(ValueError, '설정'):
                gpu.config_check(docker, root, state)

    def test_unowned_gpu_state_does_not_change_base_compose(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / 'gpu').mkdir()
            op.write_private(root / '.state.json', {'config_id': 'owner'})
            op.write_private(root / 'gpu/.state.json', {'schema': 1, 'config_id': 'foreign', 'enabled': True})
            with self.assertRaisesRegex(ValueError, '소유권'):
                op.gpu_enabled(root)


if __name__ == '__main__':
    unittest.main()
