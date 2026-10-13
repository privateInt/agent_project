"""Native Prometheus evaluation of the generated profiles; no running server.

PROMTOOL_TEST_BINARY=/path/to/promtool python3 -B -m unittest \
  src.ops.platform.tests.test_resource_promql -v
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from src.ops.platform.resource_profiles import DEFAULT_PROFILE, METRIC_UNITS, PROFILE_ROOT, queries


def series(name, labels):
    return name + '{' + ','.join(k + '=' + json.dumps(v) for k, v in sorted(labels.items())) + '}'


def fixture(cfg):
    inputs, expected = [], {k: [] for k in queries(cfg)}
    def add(name, labels, value, sequence=None):
        inputs.append({'series': series(name, labels), 'values': sequence or str(value) + '+0x5'})
    def result(key, name, labels, value):
        expected[key].append({'labels': series(name, labels), 'value': value})
    roles = {role: dict(labels, **cfg['scope'], instance='same:9100') for role, labels in cfg['sources'].items()}
    for role, labels in roles.items():
        add('up', labels, 1); result('targets', 'up', labels, 1)
    host = dict(roles['host'], node='node-a')
    add('node_cpu_seconds_total', dict(host, cpu='0', mode='idle'), 0, '0+30x5')
    result('host_cpu', '', {k: v for k, v in host.items() if k in ('cluster', 'job', 'instance', 'node')}, 50)
    add('node_memory_MemTotal_bytes', host, 100)
    add('node_memory_MemAvailable_bytes', host, 25)
    result('host_memory', '', host, 75)
    disk = dict(host, device='/dev/fixture', mountpoint='/', fstype='ext4')
    add('node_filesystem_size_bytes', disk, 1000)
    add('node_filesystem_avail_bytes', disk, 100)
    result('filesystems', '', disk, 90)
    for namespace in ('team-a', 'team-b'):
        container = dict(roles['containers'], node='node-a', namespace=namespace, pod='same-pod', container='worker')
        if cfg['environment'] == 'docker-compose': container['name'] = namespace + '-worker'
        add('container_cpu_usage_seconds_total', container, 0, '0+60x5')
        add('container_memory_working_set_bytes', container, 1024)
        result('container_cpu', '', container, 1)
        result('container_memory', 'container_memory_working_set_bytes', container, 1024)
    if cfg['environment'] == 'kubernetes':
        # The pause container and a container without a Pod are excluded.
        for pod, name in (('same-pod', 'POD'), ('', 'worker')):
            add('container_memory_working_set_bytes', dict(roles['containers'], pod=pod, container=name), 999)
    for namespace in ('team-a', 'team-b'):
        gpu = dict(roles['gpu'], node='node-a', namespace=namespace, pod='same-pod', container='worker', UUID='GPU-synthetic')
        for name, value in zip(METRIC_UNITS, (0, 24576, 4096, 20480, 46, 72.5)):
            add(name, gpu, value); result('gpu', name, dict(gpu, ops_metric_name=name), value)
    return inputs, expected


@unittest.skipUnless(os.environ.get('PROMTOOL_TEST_BINARY'), 'native promtool opt-in')
class NativeResourceQueryTests(unittest.TestCase):
    def test_two_profiles_fresh_down_stale_and_cluster_isolation(self):
        cases = []
        k8s = json.loads((PROFILE_ROOT / 'kubernetes.example.json').read_text())
        for cfg in (deepcopy(DEFAULT_PROFILE), k8s):
            inputs, expected = fixture(cfg)
            expressions = queries(cfg)
            # A foreign cluster reusing all endpoint/Pod names must not enter a scoped query.
            if cfg['scope']:
                inputs += [dict(row, series=row['series'].replace('example-cluster', 'other-cluster')) for row in list(inputs)]
            exprs = [{'expr': row[2], 'eval_time': '5m', 'exp_samples': expected[key]}
                     for key, row in expressions.items()]
            cases.append({'name': cfg['environment'] + ' fresh multi-namespace', 'interval': '1m',
                          'input_series': inputs, 'promql_expr_test': exprs})
            down = [dict(row, values='0+0x5') if row['series'].startswith('up{') else row for row in inputs]
            cases.append({'name': cfg['environment'] + ' exporter down', 'interval': '1m', 'input_series': down,
                          'promql_expr_test': [{'expr': row[2], 'eval_time': '5m', 'exp_samples': []}
                                              for key, row in expressions.items() if key != 'targets']})
            cases.append({'name': cfg['environment'] + ' source stale', 'interval': '1m', 'input_series': inputs,
                          'promql_expr_test': [{'expr': row[2], 'eval_time': '7m', 'exp_samples': []}
                                              for row in expressions.values()]})
        # With no cluster filter, a healthy exporter in another cluster cannot
        # make a down exporter with the same instance appear healthy.
        multi = deepcopy(k8s); multi['scope'] = {}; multi['default_cluster'] = None
        inputs = []
        for cluster, healthy in (('c1', 1), ('c2', 0)):
            labels = {'cluster': cluster, 'job': 'node-exporter', 'instance': 'same:9100', 'node': 'same-node'}
            inputs.extend([
                {'series': series('up', labels), 'values': str(healthy) + '+0x5'},
                {'series': series('node_memory_MemTotal_bytes', labels), 'values': '100+0x5'},
                {'series': series('node_memory_MemAvailable_bytes', labels), 'values': '25+0x5'},
            ])
        wanted = {'cluster': 'c1', 'job': 'node-exporter', 'instance': 'same:9100', 'node': 'same-node'}
        cases.append({'name': 'cluster-aware health join', 'interval': '1m', 'input_series': inputs,
                      'promql_expr_test': [{'expr': queries(multi)['host_memory'][2], 'eval_time': '5m',
                                           'exp_samples': [{'labels': series('', wanted), 'value': 75}]}]})
        with tempfile.TemporaryDirectory(prefix='ops-promql-') as folder:
            path = Path(folder) / 'queries.json'
            path.write_text(json.dumps({'rule_files': [], 'evaluation_interval': '1m', 'tests': cases}))
            run = subprocess.run([os.environ['PROMTOOL_TEST_BINARY'], 'test', 'rules', str(path)],
                                 text=True, capture_output=True, timeout=45)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == '__main__': unittest.main()
