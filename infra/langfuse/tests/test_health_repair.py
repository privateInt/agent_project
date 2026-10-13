"""Real Node/HTTP binding probes plus guarded Docker command scope tests."""
import contextlib
import copy
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_langfuse import setup, manage, common
import health_repair as repair


def effective(s):
    services = {}
    for key in common.SERVICES:
        names = ['clickhouse', 'clickhouse-logs'] if key == 'lf-clickhouse' else [key.removeprefix('lf-')]
        services[key] = {'image': manage.IMAGES[key], 'labels': {common.OWNER: s['owner']},
                         'volumes': [{'target': common.STORES[n], 'source': str(Path(s['state_dir']) / 'data' / n)} for n in names if n in common.STORES]}
        if key in repair.APPS:
            services[key]['environment'] = {'HOSTNAME': '0.0.0.0'}
        if key == 'lf-web':
            services[key]['ports'] = [{'host_ip': '0.0.0.0', 'published': '16320', 'target': 3000}]
    return {'name': common.PROJECT, 'services': services, 'networks': {'private': {'internal': True}}}


class FakeDocker:
    def __init__(self, s):
        self.config = effective(s)
        self.items = {}
        self.calls = []
        self.error = None
        self.unavailable = False
        for service in common.SERVICES:
            c = self.config['services'][service]
            self.items[service] = {'Id': service, 'Image': 'image-' + service, 'RestartCount': 0,
                'Config': {'Image': c['image'], 'Hostname': 'container-host', 'Env': [],
                           'Labels': {'com.docker.compose.service': service, common.OWNER: s['owner']}},
                'Mounts': [{'Type': 'bind', 'Source': x['source'], 'Destination': x['target']} for x in c['volumes']],
                'State': {'Running': True, 'Status': 'running', 'StartedAt': 'original',
                          'Health': {'Status': 'unhealthy' if service in repair.APPS else 'healthy'}}}

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'ps':
            return '\n'.join(self.items)
        if args[:2] == ['network', 'ls']:
            return ''
        if 'up' in args:
            if self.error:
                raise self.error
            targets = args[args.index('--wait-timeout')+2:]
            for target in targets:
                self.items[target]['Id'] += '-fixed'
                self.items[target]['Config']['Env'] = ['HOSTNAME=0.0.0.0']
                self.items[target]['State']['Health']['Status'] = 'healthy'
            return ''
        raise AssertionError(args)

    def json(self, args):
        self.calls.append(args)
        if args[0] == 'inspect':
            return copy.deepcopy(list(self.items.values()))
        if 'config' in args:
            return copy.deepcopy(self.config)
        if args[0] == 'exec':
            item = next(x for x in self.items.values() if x['Id'] == args[1])
            service = item['Config']['Labels']['com.docker.compose.service']
            ready = repair.hostname(item) == '0.0.0.0'
            return [dict(address=address, endpoint=label, **({'status': 503 if self.unavailable else 200}
                        if ready or address == 'configured' else {'error': 'ECONNREFUSED'}))
                    for address in ('loopback', 'configured') for label, _ in repair.ENDPOINTS[service][1]]
        raise AssertionError(args)


class RepairTests(unittest.TestCase):
    def run_repair(self, d, s):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(manage, 'check', return_value={'services_healthy': 6}) as check:
            result = repair.repair(d, s, 1)
        check.assert_called_once()
        return result

    def test_only_mismatched_apps_recreated_and_retry_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td)); d = FakeDocker(s)
            before = repair.store_snapshot(d.items)
            first = self.run_repair(d, s)
            self.assertEqual(first['repaired_services'], list(repair.APPS))
            self.assertEqual(before, repair.store_snapshot(d.items))
            self.assertTrue(first['settings_and_secrets_preserved'])
            self.assertFalse(self.run_repair(d, s)['changes_made'])
            ups = [c for c in d.calls if 'up' in c]
            self.assertEqual(len(ups), 1)
            self.assertEqual(ups[0][ups[0].index('up'):], ['up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                '--wait', '--wait-timeout', '30', 'lf-worker', 'lf-web'])
            self.assertEqual(manage.load(Path(td)), s)

    def test_partial_previous_repair_only_changes_remaining_app(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td)); d = FakeDocker(s)
            d.items['lf-worker']['Config']['Env'] = ['HOSTNAME=0.0.0.0']
            d.items['lf-worker']['State']['Health']['Status'] = 'healthy'
            self.assertEqual(self.run_repair(d, s)['repaired_services'], ['lf-web'])

    def test_unproven_cause_unhealthy_store_and_foreign_binding_block_mutation(self):
        for scenario in ('http503', 'store', 'image', 'mount', 'hostname', 'owner', 'config'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as td:
                s = setup(Path(td)); d = FakeDocker(s)
                if scenario == 'http503': d.unavailable = True
                elif scenario == 'store': d.items['lf-minio']['State']['Health']['Status'] = 'unhealthy'
                elif scenario == 'image': d.items['lf-web']['Config']['Image'] = 'untrusted:latest'
                elif scenario == 'mount': d.items['lf-postgres']['Mounts'][0]['Source'] = '/foreign'
                elif scenario == 'hostname': d.items['lf-web']['Config']['Env'] = ['HOSTNAME=other-host']
                elif scenario == 'owner': d.items['lf-web']['Config']['Labels'][common.OWNER] = 'foreign'
                elif scenario == 'config': d.config['services']['lf-web']['environment'].pop('HOSTNAME')
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    repair.repair(d, s, 0)
                self.assertFalse(any('up' in c for c in d.calls))

    def test_storage_change_during_repair_is_not_reported_as_passed(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td)); d = FakeDocker(s)
            def changed(*args):
                d.items['lf-postgres']['State']['StartedAt'] = 'external-restart'
                return {}
            with contextlib.redirect_stdout(io.StringIO()), patch.object(manage, 'check', side_effect=changed), self.assertRaises(ValueError):
                repair.repair(d, s, 0)

    def test_failed_compose_does_not_retry_or_rotate_and_reports_redacted_failure(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td)); d = FakeDocker(s)
            d.error = common.DockerFailure(['compose', 'up'], 1, 'private', '')
            before = (Path(td) / 'settings.json').read_bytes()
            with contextlib.redirect_stdout(io.StringIO()), patch('diagnose.report', return_value={'redacted': True}) as report, self.assertRaises(common.DockerFailure):
                repair.repair(d, s, 0)
            report.assert_called_once()
            self.assertEqual(before, (Path(td) / 'settings.json').read_bytes())
            self.assertEqual(len([c for c in d.calls if 'up' in c]), 1)


@contextlib.contextmanager
def http_fixture(address='127.0.0.1', status=200):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.server.paths.append(self.path)
            self.send_response(status); self.end_headers()
            try: self.wfile.write(b'private-body-must-not-appear')
            except (BrokenPipeError, ConnectionResetError): pass
    server = http.server.ThreadingHTTPServer((address, 0), Handler)
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield server
    finally: server.shutdown(); server.server_close(); thread.join()


@unittest.skipUnless(shutil.which('node'), 'Node needed for actual healthcheck execution')
class NativeNodeTests(unittest.TestCase):
    def probe(self, server, wildcard):
        # A second local address models an interface-only listener without Docker/DNS.
        env = dict(os.environ, NODE_USE_ENV_PROXY='0', HOSTNAME='0.0.0.0' if wildcard else '127.0.0.2')
        code = "require('node:os').hostname = () => '127.0.0.2';\n" + repair.PROBE
        return subprocess.run(['node', '-e', code, json.dumps([server.server_port, repair.ENDPOINTS['lf-web'][1]])],
                              env=env, capture_output=True, text=True, timeout=8)

    def test_real_interface_binding_fails_loopback_and_wildcard_repairs_it(self):
        with http_fixture('127.0.0.2') as server:
            p = self.probe(server, False)
            self.assertEqual(p.returncode, 0, p.stderr)
            rows = json.loads(p.stdout)
            self.assertTrue(all(x.get('error') == 'ECONNREFUSED' for x in rows if x['address'] == 'loopback'))
            self.assertTrue(all(x.get('status') == 200 for x in rows if x['address'] == 'configured'))
            self.assertNotIn('private-body', p.stdout)
        with http_fixture('0.0.0.0') as server:
            p = self.probe(server, True)
            self.assertTrue(all(x.get('status') == 200 for x in json.loads(p.stdout)))

    def test_shipped_healthchecks_200_503_and_connection_failure(self):
        checks = [json.loads(line.strip().removeprefix('test: ')) for line in (manage.ROOT/'docker-compose.yml').read_text().splitlines()
                  if line.strip().startswith('test: ["CMD", "node"')]
        self.assertEqual(len(checks), 2)
        for command, service in zip(checks, repair.APPS):
            port = repair.ENDPOINTS[service][0]
            for status in (200, 503):
                with self.subTest(service=service, status=status), http_fixture(status=status) as server:
                    js = command[-1].replace(':'+str(port), ':'+str(server.server_port))
                    p = subprocess.run(['node','-e',js], capture_output=True, text=True, timeout=8)
                    self.assertEqual(p.returncode, 0 if status == 200 else 1, p.stderr)
                    self.assertTrue(all(x['status'] == status for x in json.loads(p.stdout)))
                    self.assertEqual(set(server.paths), {x[1] for x in repair.ENDPOINTS[service][1]})
                    self.assertNotIn('private-body', p.stdout)
            # The closed ephemeral listener produces a real ECONNREFUSED.
            p = subprocess.run(['node','-e',js], capture_output=True, text=True, timeout=8)
            self.assertEqual(p.returncode, 1)
            self.assertTrue(all(x.get('error') == 'ECONNREFUSED' for x in json.loads(p.stdout)))


if __name__ == '__main__': unittest.main()
