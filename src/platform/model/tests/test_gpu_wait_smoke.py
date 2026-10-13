import contextlib
import io
import unittest
from unittest.mock import Mock

import live_dataset_smoke as smoke


class GPUWaitObservationTests(unittest.TestCase):
    def observe(self, operation, record=None):
        client = Mock()
        client.call.side_effect = [{'status': 'created'}, operation]
        record = {} if record is None else record
        saved = []
        with contextlib.redirect_stdout(io.StringIO()):
            result = smoke.observe_training(client, 'run', 'op', record, lambda value: saved.append(dict(value)))
        return result, record, saved, client

    def test_waiting_reason_is_saved_without_creating_or_cancelling_work(self):
        op = dict(status='pending', phase='waiting_resources', error_code='GPU_BUSY',
                  error_message='GPU reserved by contextrag-backend', dispatch_generation=12, gpu_wait_count=12)
        run, record, saved, client = self.observe(op)
        self.assertEqual(run['status'], 'created')
        self.assertEqual(saved[0]['operation_observation']['error_message'], op['error_message'])
        self.assertTrue(all(call.args[0] == 'GET' for call in client.call.call_args_list))
        _, _, repeated, _ = self.observe(op, record)
        self.assertEqual(repeated, [])

    def test_exhausted_unstarted_operation_fails_without_waiting_for_smoke_timeout(self):
        op = dict(status='dispatching', dispatch_generation=3, gpu_wait_count=0,
                  dispatch_lease_until='2000-01-01T00:00:00Z', execution_started_at=None)
        with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            self.observe(op)

    def test_variable_fraction_in_expired_lease_is_supported(self):
        op = dict(status='dispatching', dispatch_generation=3, gpu_wait_count=0,
                  dispatch_lease_until='2000-01-01T00:00:00.92866Z', execution_started_at=None)
        with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            self.observe(op)

    def test_gpu_waits_do_not_count_as_exhausted_launches(self):
        op = dict(status='dispatching', dispatch_generation=20, gpu_wait_count=19,
                  dispatch_lease_until='2000-01-01T00:00:00Z', execution_started_at=None)
        self.observe(op)
        op.update(gpu_wait_count=0, execution_started_at='2000-01-01T00:00:00Z')
        self.observe(op)

    def test_terminal_operation_is_reported_even_if_training_is_still_created(self):
        for state in ('failed', 'cancelled'):
            with self.subTest(state=state), self.assertRaisesRegex(RuntimeError, 'terminal'):
                self.observe(dict(status=state, error_message='reason'))

    def test_legacy_operation_response_without_dispatch_fields_is_supported(self):
        self.observe(dict(status='running', phase='axolotl_training'))
