"""Filesystem coverage through authenticated Ops API and real HTTP fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import re
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from src.ops.platform.observations import QUERIES
from src.ops.platform.ops.check_filesystems import summarize
from src.ops.platform.tests.test_alerts import rules_fixture
from src.ops.platform.tests.test_console import Environment, OPS_KEY
from src.ops.platform.tests.test_status import server


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment()
        self.addCleanup(self.env.close)
        self.old_metric = self.env.prom_routes['/api/v1/query']
        self.samples = [('/', '23'), ('/media/simsys/SSD_EXT4', '96.85'), ('/mnt/nvme1', '91.81'),
                        ('/mnt/nvme10', '95'), ('/mnt/nvme1/sub', '95'), ('/run', '99')]
        def metric(path):
            query = parse_qs(urlsplit(path).query)['query'][0]
            if 'node_filesystem_size_bytes' not in query:
                return self.old_metric(path)
            pattern = re.search(r'mountpoint=~"([^"]+)"', query)[1]
            return {'status': 'success', 'data': {'resultType': 'vector', 'result': [
                {'metric': {'job': 'host', 'instance': '127.0.0.1:16311', 'mountpoint': m,
                            'device': '/dev/fixture' + str(i)}, 'value': [time.time(), value]}
                for i, (m, value) in enumerate(self.samples) if re.fullmatch(pattern, m)]}}
        self.env.prom_routes['/api/v1/query'] = metric
        self.rules = rules_fixture()
        self.env.prom_routes['/api/v1/rules'] = lambda _: self.rules
        url, self.am_routes, self.am_calls = self.env.stack.enter_context(server('alertmanager', {'/api/v2/alerts': []}))
        self.env.config['alertmanager_url'] = url
        self.client = self.env.client()
        self.auth = {'Authorization': 'Bearer ' + OPS_KEY}

    def responses(self):
        return (self.client.get('/api/resources', headers=self.auth).json(),
                self.client.get('/api/alerts', headers=self.auth).json())

    def test_exact_nvme_mount_and_prior_disks_are_visible_without_business_calls(self):
        resources, alerts = self.responses()
        result = summarize(resources, alerts)
        self.assertEqual(result['result'], 'passed')
        self.assertEqual({r['labels']['mountpoint'] for r in result['filesystems']},
                         {'/', '/media/simsys/SSD_EXT4', '/mnt/nvme1'})
        self.assertTrue(all(not calls for calls in self.env.calls.values()))
        self.assertTrue(all(method == 'GET' and auth is None for method, _, auth in self.env.prom_calls + self.am_calls))

    def test_missing_nvme_series_fails_instead_of_healthy_zero(self):
        self.samples = [('/', '23')]
        self.assertEqual(summarize(*self.responses())['result'], 'attention_required')

    def test_unknown_or_stale_values_and_truncation_do_not_pass(self):
        resources, alerts = self.responses()
        for field, value in (('value', None), ('value', float('nan')), ('value_status', 'unavailable'),
                             ('evaluated_at', '2020-01-01T00:00:00Z')):
            changed = deepcopy(resources)
            changed['metrics']['filesystems']['data']['items'][0][field] = value
            self.assertEqual(summarize(changed, alerts)['result'], 'attention_required')
        resources['metrics']['filesystems']['data']['coverage'] = 'truncated'
        self.assertEqual(summarize(resources, alerts)['result'], 'attention_required')

    def test_pending_and_firing_do_not_fail_coverage_acceptance(self):
        rule = next(r for r in self.rules['data']['groups'][0]['rules'] if r['name'] == 'OpsFilesystemHigh')
        for state in ('pending', 'firing'):
            rule['state'] = state
            rule['alerts'] = [{'labels': {'alertname': rule['name'], 'scope': 'agent-project-o04',
                'severity': 'warning', 'mountpoint': '/mnt/nvme1', 'job': 'host', 'instance': 'fixture'},
                'state': state, 'activeAt': rule['lastEvaluation']}]
            self.assertEqual(summarize(*self.responses())['result'], 'passed')

    def test_engine_error_keeps_attention_and_resource_auth_is_required(self):
        self.assertEqual(self.client.get('/api/resources').status_code, 401)
        self.am_routes['/api/v2/alerts'] = (503, {})
        self.assertEqual(summarize(*self.responses())['result'], 'attention_required')

    def test_resource_and_alert_selectors_have_identical_scope(self):
        path = Path(__file__).resolve().parents[4] / 'src/infra/observability/alerts/o04.rules.yml'
        rule = next(r for r in json.loads(path.read_text())['groups'][0]['rules'] if r['alert'] == 'OpsFilesystemHigh')
        selectors = re.findall(r'node_filesystem_(?:size|avail)_bytes\{([^}]+)\}', rule['expr'])
        query_selectors = re.findall(r'node_filesystem_(?:size|avail)_bytes\{([^}]+)\}', QUERIES['filesystems'][2])
        self.assertEqual(set(selectors), set(query_selectors))
        self.assertEqual(len(set(selectors)), 1)
        self.assertEqual(rule['for'], '900s')


if __name__ == '__main__':
    unittest.main()
