"""Optional JS tests must not load modern fixtures in incompatible server Node."""
import io
import json
import subprocess
import unittest
from unittest.mock import patch

from src.apps.document_qa.tests import test_screen_questions_uuid as screen


class NodeRuntimeTests(unittest.TestCase):
    def probe(self, version='24.19.0', capable=True, raw=None, code=0):
        reply = subprocess.CompletedProcess([], code,
            json.dumps({'version': version, 'capable': capable}) if raw is None else raw, '')
        with patch.object(screen.shutil, 'which', return_value='/test/node'), \
                patch.object(screen.subprocess, 'run', return_value=reply) as run:
            result = screen.node_runtime()
        self.assertEqual(run.call_args.args[0], ['/test/node', '-e', screen.NODE_PROBE])
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        return result

    def test_old_Node_does_not_load_unsupported_optional_chaining_fixture(self):
        node, reason = self.probe('12.22.12')
        self.assertIsNone(node)
        self.assertIn('found Node 12.22.12', reason)
        self.assertIn('Node >=18', reason)

    def test_supported_version_and_required_features_use_exact_probed_executable(self):
        for version in ('18.20.0', '20.19.0', '24.19.0'):
            with self.subTest(version=version):
                self.assertEqual(self.probe(version), ('/test/node', ''))

    def test_modern_Node_without_required_globals_is_unavailable(self):
        self.assertIsNone(self.probe('24.19.0', capable=False)[0])
        self.assertIsNone(self.probe('24.19.0', capable='true')[0])

    def test_missing_Node_is_optional_without_subprocess(self):
        with patch.object(screen.shutil, 'which', return_value=None), \
                patch.object(screen.subprocess, 'run') as run:
            node, reason = screen.node_runtime()
        self.assertIsNone(node)
        self.assertIn('Node not found', reason)
        run.assert_not_called()

    def test_malformed_output_nonzero_probe_and_invalid_version_are_unavailable(self):
        for options in ({'raw': ''}, {'raw': 'not json'}, {'raw': 'null'}, {'raw': '[]'},
                        {'version': 'unknown'}, {'version': '24.1'}, {'version': 24}, {'code': 1}):
            with self.subTest(options=options):
                self.assertIsNone(self.probe(**options)[0])

    def test_timeout_or_executable_error_is_optional(self):
        for error in (OSError('not runnable'), subprocess.TimeoutExpired('/test/node', 5)):
            with self.subTest(error=type(error).__name__):
                with patch.object(screen.shutil, 'which', return_value='/test/node'), \
                        patch.object(screen.subprocess, 'run', side_effect=error):
                    self.assertIsNone(screen.node_runtime()[0])

    def test_incompatible_runtime_skips_before_disposable_fixture_or_JS_execution(self):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(screen.QuestionUUIDScreenTests)
        with patch.object(screen, 'node_runtime', return_value=(None, 'Node >=18 required')), \
                patch.object(screen.Fixture, 'setUp') as setup, \
                patch.object(screen.subprocess, 'run') as run:
            result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.skipped), 1)  # unittest reports one class-level skip.
        self.assertEqual(result.testsRun, 0)
        setup.assert_not_called()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
