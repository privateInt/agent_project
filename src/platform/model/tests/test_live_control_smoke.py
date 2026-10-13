"""Regression checks for smoke image selection after splitting service images."""
import json
import unittest
from unittest.mock import patch

import live_control_smoke as smoke


class SmokeImageSelection(unittest.TestCase):
    def test_uses_running_digest_and_configured_job_image(self):
        digest = 'sha256:' + 'a' * 64
        job = 'local/deployment-job:custom'
        config = {'services': {'dispatcher': {'environment': {'DEPLOYMENT_JOB_IMAGE': job}}}}
        with patch.object(smoke, 'command', side_effect=[json.dumps(config), digest, digest, 'job-id']) as command:
            self.assertEqual(smoke.resolve_images(), (digest, job))
        calls = [call.args[0] for call in command.call_args_list]
        self.assertEqual(calls[1], ['docker', 'inspect', '--format', '{{.Image}}', 'model-operation'])
        self.assertEqual(calls[2][-1], digest)
        self.assertEqual(calls[3][-1], job)
        self.assertFalse(any('model-platform-control:dev' in args for args in calls))

    def test_missing_image_fails_before_fixture_creation(self):
        config = {'services': {'dispatcher': {'environment': {'DEPLOYMENT_JOB_IMAGE': ''}}}}
        with patch.object(smoke, 'command', side_effect=[json.dumps(config), 'sha256:running']) as command:
            with self.assertRaisesRegex(RuntimeError, 'must be configured'):
                smoke.resolve_images()
        self.assertEqual(command.call_count, 2)


class SmokeSubmissionWait(unittest.TestCase):
    def test_pending_waits_and_delivered_proceeds(self):
        self.assertFalse(smoke.submission_delivered(None, 'op'))
        for state, expected in [('pending', False), ('delivered', True)]:
            self.assertEqual(smoke.submission_delivered(
                {'operation_id': 'op', 'submission_status': state}, 'op'), expected)

    def test_blocked_and_mixed_versions_fail_promptly(self):
        with self.assertRaisesRegex(RuntimeError, 'blocked: InvalidArgument'):
            smoke.submission_delivered({'operation_id': 'op', 'submission_status': 'blocked',
                                        'submission_error_code': 'InvalidArgument'}, 'op')
        for value in [{}, {'operation_id': 'other'}, {'operation_id': 'op'}]:
            with self.assertRaisesRegex(RuntimeError, 'check service versions'):
                smoke.submission_delivered(value, 'op')


if __name__ == '__main__':
    unittest.main()
