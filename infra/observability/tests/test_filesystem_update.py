"""One-time filesystem transition, source/record protection and interrupted command recovery."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

OPS = Path(__file__).resolve().parents[1] / 'ops'
sys.path.insert(0, str(OPS))
import filesystem_update as update
from test_restart_check import Runtime, stamp


class FilesystemUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'alerts').mkdir()
        self.state = {'enabled': True, 'config_id': 'fixture'}
        update.op.write_private(self.root / 'alerts/.state.json', self.state)
        self.runtime = Runtime(self.root)
        self.runtime.root = self.root
        self.runtime.state.update(config_id='fixture', images={'obs-prometheus': {'digest': 'fixture@sha256:pin'}})
        self.runtime.docker = object()
        self.prior = self.root / 'alerts-deployment-o04.json'
        self.prior.write_text('original accepted evidence')
        self.path = self.root / 'filesystem-update-o04.json'
        self.bound = {'config': 'fixed'}
        self.protected = {'business': 'old-start'}
        self.am = {'id': 'am', 'image_id': 'am-image', 'started_at': 'old-am-start'}
        self.effects = []
        self.failure = None
        self.real_query = self.runtime.prom.query
        self.real_get = self.runtime.prom.get
        def query(expression):
            if expression == update.QUERIES['filesystems'][2]:
                return [{'metric': {'job': 'host', 'mountpoint': m}, 'value': [time.time(), '95']}
                        for m in ('/', '/mnt/nvme1', '/media/disk')]
            jobs = ('prometheus', 'host', 'containers', 'gpu', 'alertmanager')
            return [{'metric': {'job': job}, 'value': [time.time(), '0' if expression.startswith('time()') else '1']}
                    for job in jobs]
        self.runtime.prom.query = query
        def get(path):
            if path.startswith('/api/v1/rules'):
                return {'groups': [{'name': update.engine.SCOPE, 'rules': [
                    {'name': name, 'query': 'mountpoint=~"/|/media/.*|/mnt/nvme1"', 'duration': 900,
                     'health': 'ok', 'lastEvaluation': stamp(time.time()), 'state': 'pending'}
                    for name in update.engine.RULES]}]}
            return self.real_get(path)
        self.runtime.prom.get = get
        patches = [patch.object(update, 'binding', side_effect=lambda *a: deepcopy(self.bound)),
                   patch.object(update, 'prior_guard', return_value=hashlib.sha256(self.prior.read_bytes()).hexdigest()),
                   patch.object(update, 'manager', side_effect=lambda *a: deepcopy(self.am)),
                   patch.object(update, 'native_tests', return_value={'filesystem_scenarios': 8}),
                   patch.object(update, 'mounted_rule_hash', side_effect=lambda *a: update.NEW_RULE_HASH),
                   patch.object(update.engine, 'protected', side_effect=lambda *a: deepcopy(self.protected)),
                   patch.object(update.engine, 'config_guard'), patch.object(update.op, 'config_check'),
                   patch.object(update.op, 'compose', side_effect=self.recreate)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def recreate(self, docker, root, args, **kwargs):
        self.assertEqual(args, ['up', '-d', '--no-deps', '--no-build', '--pull', 'never',
            '--force-recreate', '--wait', '--wait-timeout', '180', 'obs-prometheus'])
        saved = update.read_record(self.path)
        self.assertTrue(saved['intent'])
        self.assertFalse(saved['command_completed'])
        self.effects.append(args)
        if self.failure == 'before':
            raise subprocess.TimeoutExpired('docker', 240)
        self.runtime.items['obs-prometheus'].update(id='7' * 64, started_at=stamp(time.time()))
        if self.failure == 'after':
            raise subprocess.TimeoutExpired('docker', 240)

    def run_action(self, action='apply'):
        return update.run(self.runtime, action, wait_seconds=0)

    def test_success_preserves_original_acceptance_and_reuses_without_control(self):
        original = self.prior.read_bytes()
        result = self.run_action()
        self.assertEqual(result['result'], 'passed')
        self.assertTrue(result['same_prometheus_image_and_tsdb'])
        self.assertTrue(result['prometheus_command_completion_recorded'])
        self.assertEqual(self.prior.read_bytes(), original)
        self.assertTrue(self.run_action()['evidence_reused'])
        self.assertEqual(len(self.effects), 1)

    def test_lost_response_after_effect_is_read_back_without_second_recreate(self):
        self.failure = 'after'
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_action()
        result = self.run_action('check')
        self.assertEqual(result['result'], 'passed')
        self.assertFalse(result['prometheus_command_completion_recorded'])
        self.assertEqual(len(self.effects), 1)

    def test_lost_response_before_effect_never_repeats_control(self):
        self.failure = 'before'
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_action()
        with self.assertRaisesRegex(ValueError, 'REPLACE_NOT_OBSERVED'):
            self.run_action()
        self.assertEqual(len(self.effects), 1)

    def test_native_failure_and_missing_nvme_stop_before_mutation(self):
        with patch.object(update, 'native_tests', side_effect=ValueError('native failed')):
            with self.assertRaisesRegex(ValueError, 'native failed'):
                self.run_action()
        self.assertFalse(self.path.exists())
        with patch.object(self.runtime.prom, 'query', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'NO_VALID_SAMPLES'):
                self.run_action()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.effects, [])

    def test_config_drift_and_original_record_edit_refuse_control(self):
        record = update.capture(self.runtime, self.state)
        update.retained.write_record(self.path, record)
        self.bound['config'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'CONFIG_CHANGED'):
            self.run_action()
        self.bound['config'] = 'fixed'
        self.prior.write_text('tamper')
        with self.assertRaisesRegex(ValueError, 'PRIOR_ACCEPTANCE_CHANGED'):
            self.run_action()
        self.assertEqual(self.effects, [])

    def test_missing_record_check_does_not_capture_or_control(self):
        with self.assertRaisesRegex(ValueError, 'NO_UPDATE_RECORD'):
            self.run_action('check')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.effects, [])

    def test_record_tamper_refused(self):
        self.run_action()
        record = update.read_record(self.path)
        record['baseline']['query_time'] = 'different'
        update.retained.write_record(self.path, record)
        with self.assertRaisesRegex(ValueError, 'RECORD_INVALID'):
            self.run_action()
        self.assertEqual(len(self.effects), 1)

    def test_history_loss_or_protected_service_restart_does_not_pass(self):
        record = update.capture(self.runtime, self.state)
        update.retained.write_record(self.path, record)
        update.replace_once(self.runtime, self.state, record, self.path)
        self.protected['business'] = 'new-start'
        with self.assertRaisesRegex(ValueError, 'PROTECTED_CONTAINER_CHANGED'):
            update.verify(self.runtime, self.state, record)
        self.protected['business'] = 'old-start'
        with patch.object(update, 'history', return_value={}):
            with self.assertRaisesRegex(ValueError, 'FIXED_HISTORY_CHANGED'):
                update.verify(self.runtime, self.state, record)

    def test_native_command_scope_uses_only_pinned_promtool_and_readonly_mounts(self):
        commands = []
        class Docker:
            def run(_, args, **kwargs):
                commands.append(args)
        self.runtime.docker = Docker()
        # The real helper is exercised through a separately loaded module; no daemon involved.
        import importlib.util
        spec = importlib.util.spec_from_file_location('fs_native_scope', OPS / 'filesystem_update.py')
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        real.native_tests(self.runtime)
        self.assertEqual(len(commands), 2)
        for args in commands:
            self.assertEqual(args[:6], ['run', '--rm', '--pull', 'never', '--network', 'none'])
            self.assertIn('fixture@sha256:pin', args)
            self.assertIn('/bin/promtool', args)
            self.assertTrue(all('readonly' in value for value in args if value.startswith('type=bind,')))

    def test_prior_guard_accepts_only_exact_rule_change_and_keeps_original_bytes(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('fs_prior_guard', OPS / 'filesystem_update.py')
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        current = {'config_id': 'fixture', 'files': {real.RULE_FILE: real.NEW_RULE_HASH, 'unrelated': 'original'}}
        prior = deepcopy(current)
        prior['files'][real.RULE_FILE] = real.OLD_RULE_HASH
        core = {'binding': prior}
        record = {'schema': 1, 'kind': 'O04-deploy', 'baseline': core,
                  'baseline_sha256': real.retained.digest(core), 'report': {'result': 'passed'},
                  'accepted_containers': self.runtime.containers(),
                  'accepted_manager': {k: self.am[k] for k in ('id', 'started_at')}}
        real.retained.write_record(self.prior, record)
        original = self.prior.read_bytes()
        with patch.object(real.engine, 'binding', side_effect=lambda *a: deepcopy(current)), \
             patch.object(real, 'manager', return_value=self.am):
            self.assertEqual(real.prior_guard(self.runtime, self.state), hashlib.sha256(original).hexdigest())
            current['files']['unrelated'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'OTHER_O04_CONFIG_CHANGED'):
                real.prior_guard(self.runtime, self.state)
            current['files']['unrelated'] = 'original'
            current['files'][real.RULE_FILE] = 'unapproved'
            with self.assertRaisesRegex(ValueError, 'UNEXPECTED_RULE_VERSION'):
                real.prior_guard(self.runtime, self.state)
        self.assertEqual(self.prior.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
