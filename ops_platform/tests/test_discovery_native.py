"""Opt-in native Docker formatter and Prometheus engine checks on synthetic data."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ops_platform.ops.discovery_compose import FORMAT, FIELDS
from ops_platform.ops.discovery_metrics import SIGNATURES, probe_query, filesystem_query, FS_EXCLUDE
from ops_platform.resource_profiles import DEFAULT_PROFILE, queries
from ops_platform.tests.test_resource_promql import fixture, series


@unittest.skipUnless(os.environ.get('PREFLIGHT_DOCKER_TEST_BINARY'), 'native Docker CLI opt-in; synthetic Engine only')
class NativeDockerDiscoveryTests(unittest.TestCase):
    def test_projected_labels_ports_and_listen_flag_execute_without_secret_output(self):
        path = Path(__file__).resolve().parents[2] / 'infra/observability/tests/test_backup_docker_template.py'
        spec = importlib.util.spec_from_file_location('discovery_engine_fixture', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        engine = module.NativeTemplateTests(); engine.setUp(); self.addCleanup(engine.tearDown)
        engine.value['Config']['Cmd'] = ['--config.file=/private/config', '--web.listen-address=127.0.0.1:34567', '--password=synthetic-private-sentinel']
        engine.value['Config']['Labels'].update({'com.docker.compose.project.working_dir': '/different/project/infra/observability',
            'com.docker.compose.project.config_files': '/different/project/infra/observability/compose.yml'})
        engine.value.update(HostConfig={'NetworkMode': 'host'}, NetworkSettings={'Ports': {}})
        result = engine.invoke(FORMAT)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = dict(zip(FIELDS, json.loads(result.stdout)))
        self.assertEqual(row['listen_flags'], ['--web.listen-address=127.0.0.1:34567', None])
        self.assertEqual(row['network_mode'], 'host')
        self.assertEqual(row['working_dir'], '/different/project/infra/observability')
        self.assertNotIn('synthetic-private-sentinel', result.stdout + result.stderr)
        engine.value['Config']['Cmd'] = None
        engine.value['HostConfig']['NetworkMode'] = 'bridge'
        engine.value['NetworkSettings']['Ports'] = {'8000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '34568'}]}
        result = engine.invoke(FORMAT)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = dict(zip(FIELDS, json.loads(result.stdout)))
        self.assertEqual(row['listen_flags'], [None])
        self.assertEqual(row['ports']['8000/tcp'][0]['HostPort'], '34568')
        self.assertTrue(all(method in ('HEAD', 'GET') for method, path in engine.calls))


@unittest.skipUnless(os.environ.get('PROMTOOL_TEST_BINARY'), 'native promtool opt-in')
class NativeDiscoveryQueryTests(unittest.TestCase):
    def test_signatures_fresh_stale_and_changed_job_names(self):
        cfg = deepcopy(DEFAULT_PROFILE)
        for role in cfg['sources']: cfg['sources'][role]['job'] = 'changed-' + role
        cfg['target_jobs'] = ['changed-' + role for role in SIGNATURES]
        cfg['filesystems'] = {'mountpoint_include': '/|/srv/data\\-new', 'fstype_exclude': FS_EXCLUDE}
        inputs, expected = fixture(cfg)
        inputs.append({'series': 'prometheus_build_info{job="changed-prometheus",instance="same:9100"}', 'values': '1+0x5'})
        wanted = []
        for role, metrics in SIGNATURES.items():
            labels = series('', {'job': 'changed-' + role, 'instance': 'same:9100'})
            for metric in metrics:
                wanted.append({'expr': probe_query(metric), 'eval_time': '5m',
                               'exp_samples': [{'labels': labels, 'value': 2 if role in ('containers', 'gpu') else 1}]})
                wanted.append({'expr': probe_query(metric), 'eval_time': '7m', 'exp_samples': []})
        wanted.append({'expr': filesystem_query(cfg['sources']['host']), 'eval_time': '5m',
                       'exp_samples': [{'labels': series('', {'job': 'changed-host', 'instance': 'same:9100', 'mountpoint': '/'}), 'value': 1}]})
        wanted += [{'expr': row[2], 'eval_time': '5m', 'exp_samples': expected[key]}
                   for key, row in queries(cfg).items()]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'discovery.json'
            path.write_text(json.dumps({'rule_files': [], 'evaluation_interval': '1m', 'tests': [
                {'name': 'fresh and stale capability discovery', 'interval': '1m',
                 'input_series': inputs, 'promql_expr_test': wanted}]}))
            result = subprocess.run([os.environ['PROMTOOL_TEST_BINARY'], 'test', 'rules', str(path)],
                                    capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__': unittest.main()
