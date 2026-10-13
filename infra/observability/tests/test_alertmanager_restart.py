"""Guarded production restart lifecycle; no Docker daemon or production HTTP required."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.parse

OPS = Path(__file__).resolve().parents[1] / 'ops'
sys.path.insert(0, str(OPS))
import alertmanager_restart as target
from test_restart_check import stamp


def rules(now=None, firing=True):
    return {'groups': [{'name': target.engine.SCOPE, 'rules': [
        {'name': name, 'health': 'ok', 'lastEvaluation': stamp(now or time.time()),
         'alerts': [{'state': 'firing', 'labels': LABELS}] if firing and name == 'OpsFilesystemHigh' else []}
        for name in target.engine.RULES]}]}


LABELS = {'alertname': 'OpsFilesystemHigh', 'scope': target.engine.SCOPE, 'mountpoint': '/mnt/nvme1'}


class Client:
    def __init__(self):
        self.responses = {'/-/ready': True,
            '/api/v2/status': {'versionInfo': {'version': '0.32.2'}, 'config': {'original': 'receiver: ops-local-only'}},
            '/api/v2/silences': [], target.PATHS[3]: [
                {'labels': LABELS, 'status': {'state': 'active'}, 'endsAt': stamp(time.time() + 1000)}]}
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return deepcopy(self.responses[path])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / 'alertmanager'
        self.data.mkdir()
        self.state = {'data_dir': str(self.data)}
        self.am = {'id': 'a' * 64, 'image_id': 'sha256:pinned',
                   'started_at': stamp(time.time() - 300), 'configuration_sha256': 'fixed'}
        self.collectors = {'prom': {'running': True, 'health': 'healthy', 'started_at': 'unchanged'}}
        self.protected = {'prometheus': 'start1', 'ops': 'start2', 'agent': 'start3'}
        self.bound = {'config': 'fixed', 'prior': 'hash'}
        self.effects = []
        self.fail = None
        self.rule_data = None
        self.client = Client()
        self.runtime = SimpleNamespace(root=self.root, state={'data_dir': str(self.root / 'prometheus')},
            containers=lambda: deepcopy(self.collectors), docker=SimpleNamespace(run=self.restart),
            prom=SimpleNamespace(get=lambda path: self.rule_data or rules(), query=lambda q: [
                {'metric': {'job': 'alertmanager'}, 'value': [time.time(), str(time.time()) if q.startswith('timestamp(') else '1']}]))
        self.path = self.root / 'alertmanager-restart-o04.json'
        self.prior = {'report': {'result': 'passed'}, 'accepted_containers': deepcopy(self.collectors),
                      'baseline': {'manager': {k: v for k, v in self.am.items() if k != 'configuration_sha256'}}}
        patches = [patch.object(target, 'state_guard', return_value=self.state),
            patch.object(target, 'binding', side_effect=lambda *a: deepcopy(self.bound)),
            patch.object(target, 'manager', side_effect=lambda *a: deepcopy(self.am)),
            patch.object(target, 'protected', side_effect=lambda *a: deepcopy(self.protected)),
            patch.object(target.filesystem, 'read_record', return_value=self.prior),
            patch.object(target.filesystem, 'assert_bound')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def restart(self, args, **kwargs):
        self.assertEqual(args, ['restart', '--time', '30', 'a' * 64])
        saved = target.read_record(self.path)
        self.assertEqual(saved['intent']['id'], 'a' * 64)
        self.assertFalse(saved['command_completed'])
        self.effects.append(args)
        if self.fail == 'before':
            raise subprocess.TimeoutExpired('docker', 90)
        self.am['started_at'] = stamp(time.time())
        for name in target.SNAPSHOTS:
            (self.data / name).write_bytes(b'rewritten fixture' if (self.data / name).exists() else b'')
        if self.fail == 'after':
            raise subprocess.TimeoutExpired('docker', 90)

    def run_action(self, action='run'):
        return target.run(self.runtime, action, wait_seconds=0, client=self.client)

    def started_record(self):
        record = target.capture(self.runtime, self.state, self.client)
        target.retained.write_record(self.path, record)
        target.restart_once(self.runtime, self.state, self.client, record, self.path)
        return record

    def test_success_empty_storage_scope_and_idempotence(self):
        report = self.run_action()
        self.assertEqual(report['result'], 'passed')
        self.assertFalse(report['silences']['nonempty_unexpired_preservation_verified'])
        self.assertEqual(report['alerts']['current_firing_received'], 1)
        self.assertEqual(report['snapshots']['nflog']['after_bytes'], 0)
        calls = len(self.client.calls)
        self.assertTrue(self.run_action()['evidence_reused'])
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(len(self.effects), 1)

    def test_lost_reply_after_effect_reconciles_without_restarting(self):
        self.fail = 'after'
        with self.assertRaises(subprocess.TimeoutExpired): self.run_action()
        result = self.run_action('check')
        self.assertEqual(result['result'], 'passed')
        self.assertFalse(result['restart_command_completion_recorded'])
        self.assertEqual(len(self.effects), 1)

    def test_lost_reply_before_effect_never_repeats(self):
        self.fail = 'before'
        with self.assertRaises(subprocess.TimeoutExpired): self.run_action()
        with self.assertRaisesRegex(ValueError, 'NOT_OBSERVED'): self.run_action()
        self.assertEqual(len(self.effects), 1)

    def test_read_only_missing_record_does_not_capture(self):
        with self.assertRaisesRegex(ValueError, 'NO_RECORD'): self.run_action('check')
        self.assertFalse(self.path.exists())
        self.assertFalse(self.effects)

    def test_write_failure_before_intent_does_not_restart(self):
        record = target.capture(self.runtime, self.state, self.client)
        with patch.object(target.retained, 'write_record', side_effect=OSError('fsync failed')):
            with self.assertRaises(OSError):
                target.restart_once(self.runtime, self.state, self.client, record, self.path)
        self.assertFalse(self.effects)

    def test_preflight_missing_alert_and_prior_manager_drift_refuse(self):
        self.client.responses[target.PATHS[3]] = []
        with self.assertRaisesRegex(ValueError, 'FIRING_ALERT_NOT_RECEIVED'): self.run_action()
        self.assertFalse(self.path.exists())
        self.am['started_at'] = stamp(time.time())
        with self.assertRaisesRegex(ValueError, 'PRIOR_MANAGER_CHANGED'): self.run_action()
        self.assertFalse(self.effects)

    def test_changed_binding_and_prior_hash_refuse_without_control(self):
        record = target.capture(self.runtime, self.state, self.client)
        target.retained.write_record(self.path, record)
        self.bound['prior'] = 'altered evidence'
        with self.assertRaisesRegex(ValueError, 'CONFIG_CHANGED_KEEP_BASELINE'): self.run_action()
        self.assertFalse(self.effects)

    def test_tampered_baseline_refused(self):
        self.run_action()
        record = target.read_record(self.path)
        record['baseline']['manager']['id'] = 'other'
        target.retained.write_record(self.path, record)
        with self.assertRaisesRegex(ValueError, 'RECORD_INVALID'): self.run_action()
        self.assertEqual(len(self.effects), 1)

    def test_other_container_restart_fails(self):
        record = self.started_record()
        self.protected['ops'] = 'new-start'
        with self.assertRaisesRegex(ValueError, 'OTHER_CONTAINER_CHANGED'):
            target.verify(self.runtime, self.state, self.client, record)

    def test_collector_image_change_fails(self):
        record = self.started_record()
        self.collectors['prom']['image_id'] = 'different'
        with self.assertRaisesRegex(ValueError, 'COLLECTORS_CHANGED'):
            target.verify(self.runtime, self.state, self.client, record)

    def test_manager_recreation_or_configuration_change_fails(self):
        record = self.started_record()
        self.am['id'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'ID_IMAGE_CONFIG_CHANGED'):
            target.verify(self.runtime, self.state, self.client, record)

    def test_loaded_config_change_fails(self):
        record = self.started_record()
        self.client.responses['/api/v2/status']['config']['original'] = 'other receiver'
        with self.assertRaisesRegex(ValueError, 'LOADED_CONFIG_CHANGED'):
            target.verify(self.runtime, self.state, self.client, record)

    def test_missing_or_stale_shutdown_snapshot_fails(self):
        record = self.started_record()
        (self.data / 'nflog').unlink()
        with self.assertRaisesRegex(ValueError, 'SNAPSHOT_WRITE_NOT_OBSERVED'):
            target.verify(self.runtime, self.state, self.client, record)
        (self.data / 'nflog').write_bytes(b'')
        os.utime(self.data / 'nflog', (1, 1))
        with self.assertRaisesRegex(ValueError, 'SNAPSHOT_WRITE_NOT_OBSERVED'):
            target.verify(self.runtime, self.state, self.client, record)

    def test_snapshot_rewrite_is_observed_without_claiming_byte_preservation(self):
        for name in target.SNAPSHOTS: (self.data / name).write_bytes(b'old snapshot fixture')
        report = self.run_action()
        self.assertFalse(report['snapshots']['nflog']['bytes_equal_to_pre_restart'])
        self.assertTrue(report['notification_log_semantic_preservation'].startswith('not_verified'))

    def test_nonempty_notification_log_disappearing_requires_review(self):
        (self.data / 'nflog').write_bytes(b'old fixture')
        record = self.started_record()
        (self.data / 'nflog').write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'NONEMPTY_NFLOG_BECAME_EMPTY'):
            target.verify(self.runtime, self.state, self.client, record)

    def test_firing_missing_then_naturally_resolved_is_separate_scope(self):
        record = self.started_record()
        self.client.responses[target.PATHS[3]] = []
        with self.assertRaisesRegex(ValueError, 'FIRING_ALERT_NOT_RECEIVED'):
            target.verify(self.runtime, self.state, self.client, record)
        self.rule_data = rules(firing=False)
        report = target.verify(self.runtime, self.state, self.client, record)
        self.assertEqual(report['alerts']['baseline_firing_no_longer_firing'], 1)
        self.assertFalse(report['alerts']['delivery_sample_verified'])

    def test_nonempty_silence_survives_real_lifecycle_comparison(self):
        self.client.responses['/api/v2/silences'] = [{'id': 'keep', 'status': {'state': 'active'},
            'startsAt': stamp(time.time() - 100), 'endsAt': stamp(time.time() + 500),
            'comment': 'fixture', 'matchers': [{'name': 'job', 'value': 'fixture'}]}]
        record = self.started_record()
        report = target.verify(self.runtime, self.state, self.client, record)
        self.assertEqual(report['silences']['unexpired_preserved'], 1)
        self.client.responses['/api/v2/silences'] = []
        with self.assertRaisesRegex(ValueError, 'UNEXPIRED_SILENCE_CHANGED'):
            target.verify(self.runtime, self.state, self.client, record)

    def test_scrape_before_restart_is_not_recovery_evidence(self):
        record = self.started_record()
        self.runtime.prom.query = lambda q: [{'metric': {'job': 'alertmanager'},
            'value': [time.time(), str(time.time() - 30) if q.startswith('timestamp(') else '1']}]
        with self.assertRaisesRegex(ValueError, 'SCRAPE_NOT_READY'):
            target.verify(self.runtime, self.state, self.client, record)

    def test_stale_rule_or_down_target_fails(self):
        record = self.started_record()
        self.rule_data = rules(time.time() - 120)
        with self.assertRaisesRegex(ValueError, 'RULES_NOT_FRESH'):
            target.verify(self.runtime, self.state, self.client, record)
        self.rule_data = None
        self.runtime.prom.query = lambda q: [{'metric': {'job': 'alertmanager'}, 'value': [time.time(), '0']}]
        with self.assertRaisesRegex(ValueError, 'SCRAPE_NOT_READY'):
            target.verify(self.runtime, self.state, self.client, record)


class RuntimeGuardTests(unittest.TestCase):
    def test_actual_container_contract_rejects_mount_command_and_owner_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'alerts').mkdir()
            config_file = root / 'alerts/alertmanager.yml'
            config_file.write_text('fixture config')
            state = {'data_dir': str(root / 'data'), 'config_id': 'owned',
                     'image': {'digest': 'image@sha256:fixed', 'image_id': 'sha256:fixed'}}
            item = {'Id': 'a' * 64, 'Image': 'sha256:fixed',
                    'State': {'Running': True, 'StartedAt': 'old', 'Health': {'Status': 'healthy'}},
                    'Config': {'Image': 'image@sha256:fixed', 'User': '1000:1000',
                        'Labels': {target.op.OWNER: 'owned', 'com.docker.compose.service': target.engine.SERVICE},
                        'Cmd': ['--config.file=/etc/alertmanager/alertmanager.yml',
                            '--web.listen-address=127.0.0.1:16330', '--cluster.listen-address=',
                            '--storage.path=/alertmanager', '--data.retention=120h']},
                    'HostConfig': {'NetworkMode': 'host', 'ReadonlyRootfs': True},
                    'Mounts': [{'Source': state['data_dir'], 'Destination': '/alertmanager', 'Type': 'bind', 'RW': True},
                        {'Source': str(config_file), 'Destination': '/etc/alertmanager/alertmanager.yml', 'Type': 'bind', 'RW': False}]}
            commands = []
            def command(args):
                commands.append(args)
                return 'a' * 64 if args[0] == 'ps' else config_file.read_text()
            docker = SimpleNamespace(run=command, json=lambda args: [deepcopy(item)])
            runtime = SimpleNamespace(root=root, state={'uid': 1000, 'gid': 1000}, docker=docker)
            self.assertEqual(target.manager(runtime, state)['id'], 'a' * 64)
            item['Mounts'][0]['Source'] = '/wrong'
            with self.assertRaisesRegex(ValueError, 'RUNTIME_MOUNTS_CHANGED'): target.manager(runtime, state)
            item['Mounts'][0]['Source'] = state['data_dir']
            item['Config']['Cmd'][1] = '--web.listen-address=0.0.0.0:16330'
            with self.assertRaisesRegex(ValueError, 'RUNTIME_CONFIG_CHANGED'): target.manager(runtime, state)
            item['Config']['Labels'][target.op.OWNER] = 'other'
            with self.assertRaisesRegex(ValueError, 'OWNER_MISMATCH'): target.manager(runtime, state)
            self.assertTrue(all(c[0] in ('ps', 'exec') for c in commands))

    def test_protection_includes_prometheus_ops_and_transient_running_containers(self):
        items = [{'Id': name, 'State': {'StartedAt': name + '-start'}} for name in ('am', 'prom', 'ops', 'job')]
        docker = SimpleNamespace(run=lambda args: 'am prom ops job', json=lambda args: items)
        self.assertEqual(target.protected(SimpleNamespace(docker=docker), 'am'),
                         {'prom': 'prom-start', 'ops': 'ops-start', 'job': 'job-start'})

    def test_unhealthy_or_changed_alert_version_refused(self):
        client = Client()
        client.responses['/api/v2/status']['versionInfo']['version'] = 'different'
        with self.assertRaisesRegex(ValueError, 'STATUS_INVALID'): target.status(client)


class StorageTests(unittest.TestCase):
    def test_unexpired_silence_loss_or_modification_fails_but_expiry_is_reported(self):
        before = {'a': {'sha256': 'original', 'ends_at': 200}, 'b': {'sha256': 'expired', 'ends_at': 10}}
        with self.assertRaisesRegex(ValueError, 'UNEXPIRED_SILENCE_CHANGED'):
            target.compare_silences(before, {}, 100)
        with self.assertRaisesRegex(ValueError, 'UNEXPIRED_SILENCE_CHANGED'):
            target.compare_silences(before, {'a': {'sha256': 'changed'}}, 100)
        report = target.compare_silences(before, {'a': {'sha256': 'original'}}, 100)
        self.assertEqual(report['unexpired_preserved'], 1)
        self.assertEqual(report['expired_outside_comparison'], 1)

    def test_derived_silence_status_may_change_persistent_fields_may_not(self):
        client = Client()
        row = {'id': 'silence1', 'startsAt': stamp(time.time() - 10), 'endsAt': stamp(time.time() + 500),
               'status': {'state': 'pending'}, 'matchers': [], 'comment': 'private fixture'}
        client.responses['/api/v2/silences'] = [row]
        before = target.silence_state(client)
        row['status']['state'] = 'active'
        after = target.silence_state(client)
        self.assertEqual(before['silence1']['sha256'], after['silence1']['sha256'])
        self.assertNotIn('private fixture', json.dumps(before))
        row['comment'] = 'changed'
        self.assertNotEqual(before['silence1']['sha256'], target.silence_state(client)['silence1']['sha256'])

    def test_symlink_directory_and_oversize_snapshot_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = {'data_dir': folder}
            (root / 'silences').symlink_to(root / 'outside')
            with self.assertRaises(ValueError): target.snapshots(state)
            (root / 'silences').unlink()
            (root / 'silences').mkdir()
            with self.assertRaisesRegex(ValueError, 'SNAPSHOT_UNSAFE'): target.snapshots(state)
            (root / 'silences').rmdir()
            with (root / 'silences').open('wb') as out: out.truncate(target.MAX_BYTES + 1)
            with self.assertRaisesRegex(ValueError, 'SNAPSHOT_UNSAFE'): target.snapshots(state)


class HttpTests(unittest.TestCase):
    def test_real_get_responses_redirect_and_path_boundary(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(('GET', self.path))
                if self.path == '/api/v2/status':
                    self.send_response(302)
                    self.send_header('Location', '/unexpected')
                    self.end_headers()
                else:
                    body = b'Ready' if self.path == '/-/ready' else b'[]'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = target.Client()
            original_open = client.opener.open
            def local_open(url, **kwargs):
                self.assertTrue(url.startswith('http://127.0.0.1:16330/'))
                return original_open(url.replace(':16330/', ':' + str(server.server_port) + '/'), **kwargs)
            with patch.object(client.opener, 'open', side_effect=local_open):
                self.assertTrue(client.get('/-/ready'))
                self.assertEqual(client.get('/api/v2/silences'), [])
                with self.assertRaisesRegex(ValueError, 'REDIRECT_REFUSED'): client.get('/api/v2/status')
                with self.assertRaisesRegex(ValueError, 'HTTP_PATH_REFUSED'): client.get('/api/v2/silences/x')
            self.assertEqual(seen, [('GET', '/-/ready'), ('GET', '/api/v2/silences'), ('GET', '/api/v2/status')])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__': unittest.main()
