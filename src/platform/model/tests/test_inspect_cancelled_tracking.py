import unittest

import inspect_cancelled_tracking as check


RUN = '11111111-1111-4111-8111-111111111111'
OP = '22222222-2222-4222-8222-222222222222'
TRACKING = 'a' * 32


class FakeAPI:
    def __init__(self, run=None, operation=None, info=None):
        self.run = dict(training_run_id=RUN, operation_id=OP, status='cancelled',
                        cancel_requested_at='requested', cancelled_at='stopped',
                        cancellation_error_code='', mlflow_run_id=TRACKING)
        self.run.update(run or {})
        self.operation = dict(operation_id=OP, status='cancelled')
        self.operation.update(operation or {})
        self.info = dict(run_id=TRACKING, status='KILLED', end_time=1)
        self.info.update(info or {})
        self.calls = []

    def call(self, method, path):
        self.calls.append((method, path))
        if '/mlflow/' in path:
            return {'run': {'info': self.info}}
        if '/operations/' in path:
            return self.operation
        return self.run


def report(**changes):
    return dict(dict(mode='running', passed=True, running_observed=True,
                     training_run_id=RUN, operation_id=OP), **changes)


class TrackingInspectionTests(unittest.TestCase):
    def test_terminal_states_verified_by_get_only(self):
        for state in ('KILLED', 'FAILED', 'FINISHED'):
            with self.subTest(state=state):
                platform, tracking = FakeAPI(), FakeAPI(info={'status': state})
                result = check.inspect(report(), platform, tracking)
                self.assertTrue(result['passed'])
                self.assertEqual(result['mlflow_status'], state)
                self.assertTrue(all(method == 'GET' for method, _ in platform.calls + tracking.calls))
                self.assertEqual(tracking.calls, [('GET', '/api/2.0/mlflow/runs/get?run_id=' + TRACKING)])

    def test_invalid_report_rejected_before_requests(self):
        for change in ({'passed': False}, {'mode': 'queued'}, {'running_observed': False}, {'training_run_id': 'bad'}):
            with self.subTest(change=change):
                client = FakeAPI()
                with self.assertRaises((RuntimeError, ValueError)):
                    check.inspect(report(**change), client, client)
                self.assertEqual(client.calls, [])

    def test_decimal_int64_time_supported(self):
        result = check.inspect(report(), FakeAPI(), FakeAPI(info={'end_time': '1788998400000'}))
        self.assertEqual(result['mlflow_end_time'], 1788998400000)

    def test_nonterminal_tracking_rejected(self):
        for state in ('RUNNING', 'SCHEDULED', 'unknown'):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                check.inspect(report(), FakeAPI(), FakeAPI(info={'status': state}))

    def test_identity_mismatch_rejected(self):
        for client in (FakeAPI(run={'training_run_id': OP}), FakeAPI(run={'operation_id': RUN}),
                       FakeAPI(operation={'operation_id': RUN}), FakeAPI(info={'run_id': 'b' * 32})):
            with self.subTest(client=client), self.assertRaises(RuntimeError):
                check.inspect(report(), client, client)

    def test_incomplete_platform_cancellation_rejected(self):
        for client in (FakeAPI(run={'status': 'cancelling'}), FakeAPI(operation={'status': 'running'}),
                       FakeAPI(run={'cancelled_at': ''}), FakeAPI(run={'cancellation_error_code': 'CANCEL_TRACKING_SYNC_PENDING'})):
            with self.subTest(client=client), self.assertRaises(RuntimeError):
                check.inspect(report(), client, client)

    def test_missing_tracking_evidence_rejected(self):
        for client in (FakeAPI(run={'mlflow_run_id': ''}), FakeAPI(info={'end_time': None}),
                       FakeAPI(info={'end_time': 0}), FakeAPI(info={'end_time': True}),
                       FakeAPI(info={'end_time': '1.5'}), FakeAPI(info={'end_time': '9223372036854775808'})):
            with self.subTest(client=client), self.assertRaises(RuntimeError):
                check.inspect(report(), client, client)


if __name__ == '__main__':
    unittest.main()
