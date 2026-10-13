"""Profile configuration, common identity and real GET-only HTTP projection tests."""
from copy import deepcopy
import json
from pathlib import Path
import time
import unittest
import urllib.parse

from ops_platform.contracts import ReadFailure
from ops_platform.observations import Prometheus
from ops_platform.resource_profiles import (DEFAULT_PROFILE, PROFILE_ROOT, profile, queries, resource_identity)
from ops_platform.settings import load
from ops_platform.tests.test_console import Environment


def k8s():
    return profile(json.loads((PROFILE_ROOT / 'kubernetes.example.json').read_text()))


class ProfileTests(unittest.TestCase):
    def test_deployment_selectors_change_without_query_code_changes(self):
        cfg = deepcopy(DEFAULT_PROFILE)
        cfg['sources']['host']['job'] = 'other-node-exporter'
        cfg['filesystems']['mountpoint_include'] = '/srv/data|/srv/archive'
        result = queries(cfg)
        self.assertIn('job="other-node-exporter"', result['host_memory'][2])
        self.assertIn('/srv/data|/srv/archive', result['filesystems'][2])
        self.assertNotIn('/mnt/nvme1', result['filesystems'][2])
        self.assertEqual(profile(), DEFAULT_PROFILE)

    def test_unknown_profiles_and_browser_query_fields_rejected(self):
        cases = []
        cfg = k8s(); cfg['query'] = 'up'; cases.append(cfg)
        cfg = k8s(); cfg['url'] = 'http://untrusted'; cases.append(cfg)
        cfg = k8s(); cfg['environment'] = 'auto'; cases.append(cfg)
        cfg = k8s(); cfg['scope'] = {'namespace': 'misapplied-to-node'}; cases.append(cfg)
        cfg = k8s(); cfg['sources']['host']['__name__'] = 'other'; cases.append(cfg)
        cfg = k8s(); cfg['filesystems']['mountpoint_include'] = '(?=x)'; cases.append(cfg)
        cfg = k8s(); cfg['default_cluster'] = 'wrong-cluster'; cases.append(cfg)
        cfg = k8s(); cfg['sources']['host']['mode'] = 'user'; cases.append(cfg)
        cfg = k8s(); cfg['container_name_label'] = 'name'; cases.append(cfg)
        for cfg in cases:
            with self.subTest(cfg=cfg), self.assertRaises(ReadFailure): profile(cfg)

    def test_selector_values_are_quoted_not_executable_promql(self):
        cfg = deepcopy(DEFAULT_PROFILE)
        cfg['sources']['host']['job'] = 'host"} or vector(1) #'
        result = queries(cfg)['host_memory'][2]
        self.assertIn('job="host\\\"} or vector(1) #"', result)

    def test_kubernetes_query_scope_and_container_semantics(self):
        result = queries(k8s())
        self.assertEqual(set(result), {'targets', 'host_cpu', 'host_memory', 'filesystems', 'container_cpu', 'container_memory', 'gpu'})
        for key, row in result.items():
            self.assertIn('cluster="example-cluster"', row[2], key)
            self.assertNotIn('/mnt/nvme1', row[2], key)
        self.assertIn('metrics_path="/metrics/cadvisor"', result['container_memory'][2])
        self.assertIn('container!="POD"', result['container_memory'][2])
        self.assertIn('avg by(cluster,job,instance,node)', result['host_cpu'][2])
        self.assertIn('on(cluster,job,instance)', result['host_cpu'][2])

    def test_resource_names_in_other_namespaces_or_clusters_remain_distinct(self):
        cfg = k8s()
        labels = dict(cluster='c1', node='n1', namespace='one', pod='same-pod', container='worker', instance='node:10250')
        first = resource_identity('container_memory', labels, cfg)
        for other in (dict(labels, namespace='two'), dict(labels, cluster='c2'), dict(labels, node='n2')):
            self.assertNotEqual(resource_identity('container_memory', other, cfg)['id'], first['id'])
        self.assertTrue(first['metadata_complete'])
        self.assertIsNone(first['workload'])  # A Pod name does not prove a Deployment owner.

    def test_missing_identity_stays_unknown_and_gpu_labels_are_not_collapsed(self):
        identity = resource_identity('host_memory', {'instance': 'endpoint:9100'}, DEFAULT_PROFILE)
        self.assertIsNone(identity['cluster']); self.assertIsNone(identity['node'])
        self.assertFalse(identity['metadata_complete'])
        cfg = k8s()
        a = resource_identity('gpu', {'UUID': 'one', 'namespace': 'a'}, cfg)
        b = resource_identity('gpu', {'UUID': 'one', 'namespace': 'b'}, cfg)
        self.assertNotEqual(a['id'], b['id'])
        self.assertIn('"ops_metric_name", "$1", "__name__"', queries(cfg)['gpu'][2])


class ResourceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment()
        self.addCleanup(self.env.close)
        self.cfg = k8s()
        self.rows = [dict(cluster='example-cluster', node='node-a', namespace=ns, pod='same-pod', container='worker',
                          job='kubelet', instance='node-a:10250', metrics_path='/metrics/cadvisor', private='PRIVATE-SENTINEL')
                     for ns in ('team-a', 'team-b')]
        self.expressions = []
        def metrics(path):
            self.expressions.append(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['query'][0])
            return {'status': 'success', 'data': {'resultType': 'vector', 'result': [
                {'metric': labels, 'value': [time.time(), str(1024 * (i + 1))]} for i, labels in enumerate(self.rows)]}}
        self.env.prom_routes['/api/v1/query'] = metrics

    def test_profile_flows_through_authenticated_ops_api(self):
        self.env.config['resource_profile'] = self.cfg
        client = self.env.client()
        from ops_platform.tests.test_console import OPS_KEY
        response = client.get('/api/resources', headers={'Authorization': 'Bearer ' + OPS_KEY})
        self.assertEqual(response.status_code, 200)
        data = response.json()['metrics']['container_memory']['data']
        self.assertEqual(data['resource_schema'], 1)
        self.assertEqual(data['environment'], 'kubernetes')
        self.assertEqual(data['measurement_scope'], 'container_working_set')
        items = data['items']
        self.assertEqual({r['resource']['namespace'] for r in items}, {'team-a', 'team-b'})
        self.assertEqual(len({r['resource']['id'] for r in items}), 2)
        self.assertNotIn('PRIVATE-SENTINEL', response.text)
        self.assertTrue(all(method == 'GET' for method, path, auth in self.env.prom_calls))
        self.assertEqual(set(self.expressions), {r[2] for r in queries(self.cfg).values()})

    def test_profile_and_identity_changes_do_not_reuse_wrong_success_timestamp(self):
        times = {}
        one = deepcopy(DEFAULT_PROFILE); one['default_cluster'] = 'one'
        two = deepcopy(DEFAULT_PROFILE); two['default_cluster'] = 'two'
        a = Prometheus(self.env.config['prometheus_url'], times, resource_profile=one)
        self.assertEqual(a.metric('host_memory')['status'], 'ok')
        self.env.prom_routes['/api/v1/query'] = (503, {})
        b = Prometheus(self.env.config['prometheus_url'], times, resource_profile=two)
        result = b.metric('host_memory')
        self.assertEqual(result['status'], 'error'); self.assertIsNone(result['last_success_at'])
        self.assertIsNotNone(a.metric('host_memory')['last_success_at'])

    def test_old_config_and_new_inline_profile_are_validated_without_rewriting(self):
        path = self.env.tmp / 'read-config.json'
        for cfg in (self.env.config, dict(self.env.config, resource_profile=self.cfg)):
            raw = json.dumps(cfg).encode(); path.write_bytes(raw)
            self.assertEqual(load(path), cfg)
            self.assertEqual(path.read_bytes(), raw)
        cfg = dict(self.env.config, resource_profile={'schema': 999})
        path.write_text(json.dumps(cfg))
        with self.assertRaises(ReadFailure): load(path)


if __name__ == '__main__': unittest.main()
