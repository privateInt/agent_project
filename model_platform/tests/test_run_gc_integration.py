import contextlib
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

import run_gc_integration as runner


class GCIntegrationRunnerChecks(unittest.TestCase):
    def config(self):
        return {
            'services': {
                'gc': {'environment': {
                    'MINIO_ENDPOINT': 'minio:9000', 'MINIO_ACCESS_KEY': 'test-access-only',
                    'MINIO_SECRET_KEY': 'test-secret-only', 'MINIO_USE_SSL': 'false',
                }},
                'operation': {'container_name': 'model-operation'},
            },
            'networks': {'ops-infra': {'name': 'test-network'}},
        }

    def run_mock(self, image=None, returncode=0, stdout="--- PASS: TestLiveGCArchiveAndRemoval (0.01s)\n"):
        output = io.StringIO()
        with patch.object(runner.Path, 'cwd', return_value=Path(runner.__file__).resolve().parents[1]), \
             patch.object(runner, 'parsed', side_effect=[json.dumps(self.config()), image or 'sha256:'+'a'*64]), \
             patch.object(runner.subprocess, 'run', return_value=types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr='')) as run, \
             patch.object(runner.sys, 'argv', ['run_gc_integration.py']), \
             contextlib.redirect_stdout(output):
            runner.main()
            return run.call_args, output.getvalue()

    def test_credentials_only_in_environment_and_no_gpu_or_database(self):
        call, output = self.run_mock()
        args = call.args[0]
        self.assertNotIn('test-secret-only', ' '.join(args)+output)
        self.assertNotIn('test-access-only', ' '.join(args)+output)
        self.assertEqual(call.kwargs['env']['GC_TEST_MINIO_SECRET_KEY'], 'test-secret-only')
        self.assertEqual(call.kwargs['env']['GC_TEST_NETWORK'], 'test-network')
        self.assertIn('model-platform-check:gc', args)
        self.assertIn('--pull=never', args)
        self.assertNotIn('--gpus', args)
        self.assertFalse(any('POSTGRES' in a for a in args))
        self.assertEqual(args[-1], '^TestLiveGCArchiveAndRemoval$')

    def test_unpinned_image_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'immutable image'):
            self.run_mock(image='model-platform-operation:dev')

    def test_failed_go_test_cannot_report_success(self):
        with self.assertRaisesRegex(RuntimeError, 'integration did not pass'):
            self.run_mock(returncode=1)

    def test_zero_exit_without_pass_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'integration did not pass'):
            self.run_mock(stdout='--- SKIP: TestLiveGCArchiveAndRemoval (0.00s)\n')


if __name__ == '__main__':
    unittest.main()
