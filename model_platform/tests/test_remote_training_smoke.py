import copy
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import live_dataset_smoke as smoke
from test_live_dataset_smoke import FakeDatasetAPI


class RemoteEvidenceTests(unittest.TestCase):
    def report(self):
        return dict(version=1, action='inspect', status='observed', node_name='worker',
                    operation_id='op', daemon_id='daemon', architecture='amd64',
                    evidence=dict(container_id='container', image_id='sha256:image',
                                  state='exited', exit_code=0, gpu_ids=[0]))

    def test_exact_remote_evidence_passes(self):
        smoke.check_remote_evidence(self.report(), 'worker', 'op', [0])

    def test_wrong_node_operation_gpu_or_failed_container_is_rejected(self):
        for field, value in [('node_name', 'other'), ('operation_id', 'other'),
                             ('daemon_id', ''), ('status', 'accepted')]:
            with self.subTest(field=field):
                report = self.report()
                report[field] = value
                with self.assertRaises(RuntimeError):
                    smoke.check_remote_evidence(report, 'worker', 'op', [0])
        for field, value in [('container_id', ''), ('gpu_ids', [1]), ('exit_code', 1),
                             ('state', 'missing'), ('state', 'created')]:
            with self.subTest(field=field, value=value):
                report = self.report()
                report['evidence'][field] = value
                with self.assertRaises(RuntimeError):
                    smoke.check_remote_evidence(report, 'worker', 'op', [0])

    def test_unreachable_worker_cannot_pass_from_api_alone(self):
        run = dict(resources=dict(node_name='worker', gpu_ids=[0]))
        operation = dict(operation_id='op', **run)
        with patch.object(smoke.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaisesRegex(RuntimeError, 'evidence unavailable'):
                smoke.verify_remote({'node_name': 'worker'}, run, operation, [0])

    def test_remote_request_pin_is_saved_before_post_and_survives_resume(self):
        client = FakeDatasetAPI()
        record = dict(run_id='remote-test', dataset_request={'name': 'test'}, gpu_id=0,
                      node_name='worker', model_version_id='11111111-1111-4111-8111-111111111111')
        snapshots = []
        def save(value):
            snapshots.append(copy.deepcopy(value))
        with patch.object(smoke, 'verify_remote', return_value=self.report()) as verify:
            smoke.exercise(client, record, smoke.FIXTURE, 1, save)
            first = next(s for s in snapshots if 'training_request' in s)
            self.assertNotIn('training', first)
            self.assertEqual(first['training_request']['resources'], dict(node_name='worker', gpu_ids=[0]))
            original = copy.deepcopy(record['training_request'])
            smoke.exercise(client, record, smoke.FIXTURE, 1, save)
            self.assertEqual(record['training_request'], original)
            self.assertEqual(verify.call_count, 2)


if __name__ == '__main__':
    unittest.main()
