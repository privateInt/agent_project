"""Manual Job cleanup safety with controlled Docker/API responses."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import cleanup_jobs as cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.op_id = '11111111-1111-4111-8111-111111111111'
        self.token = '22222222-2222-4222-8222-222222222222'
        self.resource = '33333333-3333-4333-8333-333333333333'
        self.id = 'a' * 64
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.runtime = {'ID': self.id, 'Name': '/data-op-' + self.op_id + '-' + self.token,
                        'Labels': {'data-platform.owner': 'dispatcher', 'data-platform.operation_id': self.op_id,
                                   'data-platform.dispatch_token': self.token, 'data-platform.execution_type': 'refinement.parse',
                                   'data-platform.resource_id': self.resource},
                        'RestartCount': 0, 'RestartPolicy': 'no', 'AutoRemove': False,
                        'State': {'Status': 'exited', 'Running': False, 'Paused': False, 'Restarting': False,
                                  'ExitCode': 0, 'OOMKilled': False, 'StartedAt': past, 'FinishedAt': past}}
        self.operation = {'operation_id': self.op_id, 'resource_type': 'refinement_run', 'resource_id': self.resource,
                          'status': 'succeeded', 'completed_at': past}
        self.calls = []
        self.reads = 0
        self.change = None

    def docker(self, *args):
        self.calls.append(args)
        if args[0] == 'container':
            return json.dumps(self.runtime)
        if args == ('rm', self.id):
            return self.id
        raise AssertionError(args)

    def api(self, base, operation_id):
        self.assertEqual(operation_id, self.op_id)
        self.reads += 1
        if self.change and self.reads == 2:
            self.change()
        return copy.deepcopy(self.operation)

    def run_cleanup(self, **kwargs):
        platform = kwargs.pop('platform', 'data')
        return cleanup.cleanup(platform, self.id, 'http://127.0.0.1:14000', call=self.docker, get=self.api, **kwargs)

    def test_preview_is_read_only_and_apply_checks_twice_without_force(self):
        self.assertEqual(self.run_cleanup(), 'would_remove')
        self.assertFalse(any(c[0] == 'rm' for c in self.calls))
        self.calls.clear()
        self.reads = 0
        self.assertEqual(self.run_cleanup(apply=True), 'removed')
        self.assertEqual(self.reads, 2)
        self.assertEqual(self.calls[-1], ('rm', self.id))

    def test_failure_requires_explicit_flag_even_if_process_exited_zero(self):
        self.operation['status'] = 'failed'
        self.assertEqual(self.run_cleanup(apply=True), 'kept_failure')
        self.assertEqual(self.run_cleanup(apply=True, include_failed=True), 'removed')

    def test_recent_runtime_or_recent_operation_is_kept(self):
        recent = datetime.now(timezone.utc).isoformat()
        old = self.runtime['State']['FinishedAt']
        self.runtime['State']['FinishedAt'] = recent
        self.assertEqual(self.run_cleanup(apply=True), 'kept_recent')
        self.runtime['State']['FinishedAt'] = old
        self.operation['completed_at'] = recent
        self.assertEqual(self.run_cleanup(apply=True), 'kept_recent')
        self.assertFalse(any(c[0] == 'rm' for c in self.calls))

    def test_running_foreign_restarted_training_or_nonterminal_are_preserved(self):
        original = copy.deepcopy(self.runtime)
        for change in [lambda: self.runtime.update(Name='/model-serving-deployment'),
                       lambda: self.runtime['State'].update(Running=True),
                       lambda: self.runtime.update(RestartCount=1),
                       lambda: self.runtime.update(RestartPolicy='always'),
                       lambda: self.runtime['Labels'].update({'data-platform.owner': 'other'}),
                       lambda: self.runtime['Labels'].update({'data-platform.execution_type': 'training.run'})]:
            self.runtime = copy.deepcopy(original)
            change()
            with self.assertRaises(ValueError):
                self.run_cleanup(apply=True, include_failed=True)
        self.runtime = original
        self.operation['status'] = 'running'
        with self.assertRaises(ValueError):
            self.run_cleanup(apply=True)
        self.assertFalse(any(c[0] == 'rm' for c in self.calls))

    def test_second_observation_change_or_failure_prevents_delete(self):
        for change in [lambda: self.operation.update(status='running'),
                       lambda: self.operation.update(error_code='changed')]:
            self.setUp()
            self.change = change
            with self.assertRaises(ValueError):
                self.run_cleanup(apply=True)
            self.assertFalse(any(c[0] == 'rm' for c in self.calls))
        def unavailable(*args):
            raise ValueError('API unavailable')
        with self.assertRaises(ValueError):
            cleanup.cleanup('data', self.id, 'local', apply=True, call=self.docker, get=unavailable)
        self.assertFalse(any(c[0] == 'rm' for c in self.calls))

    def test_model_import_response_without_completion_timestamp_is_supported(self):
        self.runtime['Name'] = '/model-op-' + self.op_id + '-' + self.token
        self.runtime['Labels'] = {'model-platform.owner': 'operation-dispatcher', 'model-platform.operation_id': self.op_id,
                                  'model-platform.dispatch_token': self.token, 'model-platform.execution_type': 'management.import-huggingface'}
        self.operation['resource_type'] = 'model_version'
        self.operation.pop('completed_at')
        self.assertEqual(self.run_cleanup(platform='model', apply=True), 'removed')

    def test_non_full_id_and_wrong_resource_are_rejected(self):
        with self.assertRaises(ValueError):
            cleanup.cleanup('data', self.id[:12], 'local', apply=True, call=self.docker, get=self.api)
        self.assertFalse(self.calls)
        self.operation['resource_id'] = self.op_id
        with self.assertRaises(ValueError):
            self.run_cleanup(apply=True)
        self.assertFalse(any(c[0] == 'rm' for c in self.calls))

    def test_docker_nanoseconds_and_timezone_are_validated(self):
        value = cleanup.timestamp('2026-09-15T00:12:18.123456789Z')
        self.assertEqual(value.microsecond, 123456)
        self.assertIsNotNone(value.tzinfo)
        with self.assertRaises(ValueError):
            cleanup.timestamp('2026-09-15T00:12:18')


if __name__ == '__main__':
    unittest.main()
