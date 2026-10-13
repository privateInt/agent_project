import unittest
from unittest.mock import patch
import live_training_control_smoke as smoke


def cancelled():
    return {'status': 'cancelled', 'cancel_requested_at': 'request-time',
            'cancelled_at': 'stop-time', 'result_json': '', 'output_model_version_id': ''}


def logs():
    return {'training_run_id': 'run', 'operation_id': 'operation', 'source': 'docker',
            'availability': 'available', 'stdout': 'hello', 'stderr': '', 'truncated': False}


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None, key=None):
        self.calls.append((method, path))
        if path.endswith('/logs') or '/logs?' in path:
            return logs()
        if path == '/api/training-runs':
            return {'training_run_id': 'run', 'operation_id': 'operation'}
        if '/api/operations/' in path:
            return {'status': 'cancelled', 'execution_started_at': None}
        return dict(cancelled(), operation_id='operation')


class TrainingControlSmokeTests(unittest.TestCase):
    def test_read_only_logs_never_mutate(self):
        client, record = FakeAPI(), {'mode': 'logs', 'training_run_id': 'run'}
        smoke.exercise(client, record, lambda r: None, 1)
        self.assertTrue(record['passed'])
        self.assertTrue(all(method == 'GET' for method, _ in client.calls))

    def test_queued_requires_stopped_dispatcher_before_post(self):
        client = FakeAPI()
        with patch.object(smoke, 'dispatcher_stopped', side_effect=RuntimeError('still running')):
            with self.assertRaises(RuntimeError):
                smoke.exercise(client, {'mode': 'queued'}, lambda r: None, 1)
        self.assertEqual(client.calls, [])

    def test_queued_cancel_checks_replay_and_no_container(self):
        record = {'mode': 'queued', 'training_run_id': 'run', 'operation_id': 'operation',
                  'request': {}, 'idempotency_key': 'key'}
        with patch.object(smoke, 'dispatcher_stopped'), patch.object(smoke, 'owned_containers', return_value=[]):
            smoke.exercise(FakeAPI(), record, lambda r: None, 1)
        self.assertTrue(record['passed'])

    def test_pending_or_completed_is_not_cancelled(self):
        for state in ('cancelling', 'succeeded', 'failed', 'running'):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                smoke.check_cancelled(cancelled(), {'status': state}, 'running')

    def test_false_cancellation_evidence_rejected(self):
        for change in ({'cancelled_at': None}, {'output_model_version_id': 'output'}, {'result_json': '{}'}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                smoke.check_cancelled(dict(cancelled(), **change), {'status': 'cancelled'}, 'running')
        with self.assertRaises(RuntimeError):
            smoke.check_cancelled(cancelled(), {'status': 'cancelled', 'execution_started_at': 'started'}, 'queued')

    def test_log_identity_and_private_fields_rejected(self):
        for change in ({'training_run_id': 'other'}, {'dispatch_token': 'secret'}, {'truncated': 'false'}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                smoke.check_logs(dict(logs(), **change), 'run', 'operation')

    def test_running_mode_requires_observed_running_job(self):
        record = {'mode': 'running', 'training_run_id': 'run', 'operation_id': 'operation',
                  'cancel_requested': True, 'request': {}, 'idempotency_key': 'key'}
        with patch.object(smoke, 'owned_containers', return_value=[]), self.assertRaises(RuntimeError):
            smoke.exercise(FakeAPI(), record, lambda r: None, 1)
        self.assertFalse(record.get('passed', False))


if __name__ == '__main__':
    unittest.main()
