"""Discovery acceptance against real HTTP fixtures; no daemon or live API needed."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import http.server
import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import discover as runner
from src.ops.platform.ops.discovery_compose import DockerDiscovery, FORMAT, ROLES, belongs, endpoints
from src.ops.platform.ops.discovery_metrics import SIGNATURES, probe_query, discover_metrics
from src.ops.platform.resource_profiles import DEFAULT_PROFILE
from src.ops.platform.transport import Transport


class Fixture:
    def __init__(self):
        self.calls = []
        self.jobs = {'host': 'machine-new', 'containers': 'runtime-new', 'gpu': 'accelerator-new', 'prometheus': 'metrics-new'}
        self.cluster = ''
        self.missing = set()
        self.duplicate = None
        self.stale = False
        self.down = False
        self.warnings = False
        self.limit = False
        self.invalid = False
        self.fail_health = None
        self.empty_catalog = False
        self.timestamp = datetime.now(timezone.utc)
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                owner.calls.append((self.command, self.path, self.headers.get('Authorization')))
                body = owner.response(self.path)
                raw = (b'not-json' if owner.invalid and self.path.startswith('/api/v1/query') else json.dumps(body).encode())
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def labels(self, role, duplicate=False):
        labels = {'job': self.jobs[role] + ('-other' if duplicate else ''), 'instance': role + ':9999'}
        if self.cluster: labels['cluster'] = self.cluster
        return labels

    def sample(self, labels, value='1'):
        return {'metric': labels, 'value': [self.timestamp.timestamp(), value]}

    def response(self, path):
        if path == self.fail_health: return {'status': 'not-healthy', 'private': 'never-report'}
        if path == '/health': return {'status': 'ok', 'service': 'model-gateway'}
        if path == '/healthz': return {'status': 'ok', 'platform': 'agent'}
        if path == '/api/v1/status/buildinfo': return {'status': 'success', 'data': {'version': '3.13.3'}}
        if path == '/api/v2/status': return {'versionInfo': {'version': '0.31.1'}, 'config': {'original': 'synthetic-private-sentinel'}}
        if path.startswith('/api/public/health'): return {'status': 'OK', 'version': '3.155.1'}
        if path == '/api/v1/targets?state=active':
            when = self.timestamp - timedelta(seconds=120 if self.stale else 0)
            rows = [{'labels': self.labels(role, extra), 'health': 'down' if self.down else 'up',
                     'lastScrape': when.isoformat(), 'scrapeUrl': 'http://must-never-probe.invalid/metrics',
                     'discoveredLabels': {'private': 'synthetic-private-sentinel'}}
                    for role in self.jobs for extra in ([False, True] if role == self.duplicate else [False])]
            return {'status': 'success', 'data': {'activeTargets': rows}}
        if path.startswith('/api/v1/query?'):
            query = parse_qs(urlsplit(path).query)['query'][0]
            result = None
            for role, metrics in SIGNATURES.items():
                for metric in metrics:
                    if query == probe_query(metric):
                        result = [] if metric in self.missing else [self.sample(self.labels(role))]
                        if self.duplicate == role and result: result.append(self.sample(self.labels(role, True)))
            if result is None and query.startswith('count by (job,instance,cluster,mountpoint)'):
                result = [self.sample({**self.labels('host'), 'mountpoint': mount}) for mount in ('/', '/srv/data-new')]
            if result is None:
                result = [] if self.empty_catalog else [self.sample({'job': 'generated-query', 'instance': 'fixture:9999'}, '27')]
            if self.limit: result *= 257
            body = {'status': 'success', 'data': {'resultType': 'vector', 'result': result}}
            if self.warnings: body['warnings'] = ['do-not-report-private-warning']
            return body
        raise AssertionError('Unexpected request: ' + path)


class DockerFixture:
    def __init__(self, rows): self.rows, self.reads = rows, 0; self.changed = False
    def host(self): return {'endpoint': 'synthetic-only'}
    def inventory(self):
        self.reads += 1
        rows = deepcopy(self.rows)
        if self.changed and self.reads > 1: rows[0]['started_at'] = 'changed'
        return rows


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture(); self.addCleanup(self.fixture.close)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'relocated-project'
        self.rows = []
        for n, (role, (service, subtree)) in enumerate(ROLES.items()):
            self.rows.append({'id': format(n + 1, '064x'), 'image': 'sha256:' + 'a' * 64, 'state': 'running',
                'started_at': '2026-09-28T00:00:00Z', 'project': 'renamed-' + role, 'service': service,
                'working_dir': str(self.root / subtree), 'config_files': str(self.root / subtree / 'docker-compose.yml'),
                'network_mode': 'bridge', 'ports': {'8000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(self.fixture.server.server_port)}]},
                'listen_flags': [None]})
        for role in ('prometheus', 'alertmanager'):
            row = self.rows[list(ROLES).index(role)]
            row.update(network_mode='host', ports={}, listen_flags=['--web.listen-address=127.0.0.1:' + str(self.fixture.server.server_port), None])
        self.cfg = {'timeout': 1, 'platforms': {p: {'url': 'http://127.0.0.1:1', 'token_file': 'never-read-token'}
                                             for p in ('model', 'data', 'knowledge', 'agent')},
                    'prometheus_url': 'http://127.0.0.1:2', 'langfuse_url': 'http://127.0.0.1:3'}

    def run_discovery(self, docker=None):
        before = deepcopy(self.cfg)
        result = runner.discover(self.cfg, self.root, docker or DockerFixture(self.rows))
        self.assertEqual(self.cfg, before)
        self.assertTrue(all(method == 'GET' and auth is None for method, path, auth in self.fixture.calls))
        raw = json.dumps(result)
        for private in ('never-read-token', 'synthetic-private-sentinel', 'must-never-probe.invalid', 'private-warning'):
            self.assertNotIn(private, raw)
        self.assertFalse(result['service_control_requested']); self.assertEqual(result['files_changed'], [])
        return result

    def test_relocated_project_random_ports_and_renamed_jobs_generate_validated_overlay(self):
        result = self.run_discovery()
        self.assertEqual(result['result'], 'discovered', result)
        overlay = result['connection_overlay']
        self.assertEqual(overlay['platforms']['agent'], {'url': self.fixture.url})
        profile = overlay['resource_profile']
        self.assertEqual(profile['sources']['host']['job'], 'machine-new')
        self.assertEqual(profile['filesystems']['mountpoint_include'], '/|/srv/data\\-new')
        self.assertEqual(len(result['metrics']['profile_validation']), 7)
        self.assertTrue(result['services']['agent']['reconciliation_required'])

    def test_explicit_profile_preserved_without_suppressing_discovery(self):
        self.cfg['resource_profile'] = deepcopy(DEFAULT_PROFILE)
        result = self.run_discovery()
        self.assertEqual(result['result'], 'discovered')
        self.assertNotIn('resource_profile', result['connection_overlay'])
        self.assertEqual(result['metrics']['generated_profile']['sources']['host']['job'], 'machine-new')

    def test_duplicate_service_no_probe_no_overlay(self):
        other = deepcopy(self.rows[0]); other['id'] = 'f' * 64
        self.rows.append(other)
        result = self.run_discovery()
        self.assertEqual(result['services']['model']['error'], 'SERVICE_AMBIGUOUS')
        self.assertIsNone(result['connection_overlay'])

    def test_other_checkout_is_ignored(self):
        other = deepcopy(self.rows[0]); other.update(id='f' * 64, config_files='/another/src/platform/model/compose.yml')
        self.rows.append(other)
        self.assertEqual(self.run_discovery()['result'], 'discovered')

    def test_stopped_service_is_unavailable(self):
        self.rows[0]['state'] = 'exited'
        result = self.run_discovery()
        self.assertEqual(result['services']['model']['error'], 'SERVICE_NOT_RUNNING')

    def test_ambiguous_published_ports_are_not_guessed(self):
        self.rows[0]['ports']['9000/tcp'] = [{'HostIp': '127.0.0.1', 'HostPort': '65500'}]
        result = self.run_discovery()
        self.assertEqual(result['services']['model']['error'], 'ENDPOINT_MISSING_OR_AMBIGUOUS')

    def test_deployment_change_invalidates_proposal(self):
        docker = DockerFixture(self.rows); docker.changed = True
        result = self.run_discovery(docker)
        self.assertEqual(result['error'], 'DEPLOYMENT_CHANGED_DURING_DISCOVERY')
        self.assertIsNone(result['connection_overlay'])

    def test_unhealthy_identity_not_promoted(self):
        self.fixture.fail_health = '/healthz'
        result = self.run_discovery()
        self.assertEqual(result['services']['agent']['error'], 'SERVICE_HEALTH_CONTRACT_MISMATCH')

    def test_missing_gpu_does_not_become_zero_or_default_job(self):
        self.fixture.missing.add(SIGNATURES['gpu'][0])
        result = self.run_discovery()
        self.assertEqual(result['metrics']['roles']['gpu']['status'], 'not_observed')
        self.assertIsNone(result['connection_overlay'])

    def test_metric_job_collision_is_ambiguous(self):
        self.fixture.duplicate = 'host'
        result = self.run_discovery()
        self.assertEqual(result['metrics']['roles']['host']['status'], 'ambiguous')

    def test_stale_scrape_is_unavailable(self):
        self.fixture.stale = True
        self.assertEqual(self.run_discovery()['metrics']['fresh_healthy_target_count'], 0)

    def test_down_exporter_is_unavailable(self):
        self.fixture.down = True
        self.assertIsNone(self.run_discovery()['metrics']['generated_profile'])

    def test_partial_query_results_are_not_accepted(self):
        self.fixture.warnings = True
        result = self.run_discovery()
        self.assertEqual(result['metrics']['roles']['host']['error'], 'INCOMPLETE_METRIC_RESPONSE')

    def test_query_limit_is_not_silent_truncation(self):
        self.fixture.limit = True
        result = self.run_discovery()
        self.assertEqual(result['metrics']['roles']['host']['error'], 'DISCOVERY_SERIES_LIMIT')

    def test_invalid_json_stays_unavailable(self):
        self.fixture.invalid = True
        result = self.run_discovery()
        self.assertEqual(result['metrics']['roles']['host']['error'], 'INVALID_RESPONSE')

    def test_empty_rate_window_does_not_pass_validation(self):
        self.fixture.empty_catalog = True
        result = self.run_discovery()
        self.assertEqual(result['metrics']['profile_validation']['host_cpu']['status'], 'not_observed')
        self.assertIsNone(result['connection_overlay'])

    def test_observed_cluster_is_preserved(self):
        self.fixture.cluster = 'lab-new'
        result = self.run_discovery()
        self.assertEqual(result['metrics']['generated_profile']['scope'], {'cluster': 'lab-new'})

    def test_kubernetes_never_falls_back_to_compose(self):
        self.cfg['resource_profile'] = {'environment': 'kubernetes'}
        with self.assertRaisesRegex(ReadFailure, 'KUBERNETES_DISCOVERY_NOT_IMPLEMENTED'):
            self.run_discovery()


class CommandBoundaryTests(unittest.TestCase):
    def test_non_read_commands_rejected_before_subprocess(self):
        docker = DockerDiscovery(); docker.endpoint = 'unix:///var/run/docker.sock'
        with patch('subprocess.run') as run:
            for cmd in (['restart', 'ops'], ['container', 'inspect', 'a' * 64], ['exec', 'ops', 'env']):
                with self.assertRaises(Exception): docker._command(cmd)
            run.assert_not_called()

    def test_remote_context_rejected_without_discovery(self):
        docker = DockerDiscovery()
        with patch.dict(os.environ, {'DOCKER_CONTEXT': 'remote'}, clear=True), \
             patch.object(docker, '_command', return_value='ssh://other-host') as command:
            with self.assertRaisesRegex(Exception, 'LOCAL_DOCKER_REQUIRED'): docker.host()
            self.assertEqual(command.call_count, 1)

    def test_ipv6_and_dual_wildcard_bind(self):
        row = {'network_mode': 'bridge', 'ports': {'8000/tcp': [
            {'HostIp': '0.0.0.0', 'HostPort': '45678'}, {'HostIp': '::', 'HostPort': '45678'}]}}
        self.assertEqual(endpoints(row, 'model')[0], 'http://127.0.0.1:45678')
        row.update(network_mode='host', listen_flags=['--web.listen-address=[::1]:45679'])
        self.assertEqual(endpoints(row, 'prometheus')[0], 'http://[::1]:45679')

    def test_public_cli_reports_missing_kubectl_without_compose_fallback(self):
        result = subprocess.run([os.sys.executable, '-B', runner.__file__, '--provider', 'kubernetes', '--state-dir', '/nonexistent/ops-kubernetes-fixture'],
                                env=dict(os.environ, PATH='/nonexistent'), capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['error'], 'KUBECTL_UNAVAILABLE')

    def test_prepared_config_change_invalidates_cli_overlay(self):
        cfg = {'platforms': {}}
        proposed = {'result': 'discovered', 'connection_overlay': {'prometheus_url': 'http://127.0.0.1:34567'}}
        with patch.object(runner.manage, 'existing', return_value=cfg), \
             patch.object(runner, 'discover', return_value=proposed), \
             patch.object(runner, 'config_stamp', side_effect=[{'config': 'before'}, {'config': 'after'}]), \
             patch('sys.argv', ['discover.py']), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(runner.main(), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result['error'], 'CONFIG_CHANGED_DURING_DISCOVERY')
        self.assertIsNone(result['connection_overlay'])

    def test_connection_refusal_is_a_structured_read_failure(self):
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            with self.assertRaisesRegex(ReadFailure, 'CONNECTION_FAILED'):
                runner.health('agent', Transport('http://127.0.0.1:' + str(port), timeout=.2))


if __name__ == '__main__': unittest.main()
