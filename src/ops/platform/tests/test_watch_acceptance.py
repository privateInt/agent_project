"""Bounded real watch/HTTP flow; native Docker and 30-second waits are separate."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import docker_acceptance as isolated, watch_acceptance as watch
from src.ops.platform.tests.test_docker_acceptance import MetadataEngine
from src.ops.platform.tests import test_docker_acceptance as acceptance_tests


class WatchLifecycleTests(unittest.TestCase):
    scenario = acceptance_tests.RealLifecycleTests.scenario

    def test_watch_actual_http_change_two_failures_recovery_pending_stop_rollback_and_return(self):
        waits = []
        def exercise(*args, **kwargs):
            return watch.exercise(*args, **kwargs, sleep_fn=waits.append)
        report, docker = self.scenario(exercise, 'watch')
        self.assertEqual(report['result'], 'passed')
        self.assertEqual(docker.restarts, 3)
        self.assertEqual(report['refresh_journal_count'], 2)
        self.assertEqual(waits, [30]*6)
        self.assertFalse(report['wall_clock_interval_verified'])
        self.assertEqual(report['timer_mode'], 'accelerated_test')
        self.assertEqual([(x['completed_cycles'], x['exit_code'], x['wait_count'])
                          for x in report['watch_invocations']], [(6, 0, 5), (1, 2, 0), (2, 0, 1)])
        self.assertEqual(len(report['cycles']), 9)
        failures = [x for x in report['cycles'] if x['result'] == 'attention_required']
        self.assertEqual([x['code'] for x in failures],
                         ['VERIFIED_DISCOVERY_REQUIRED']*2 + ['OPS_ALERT_READBACK_FAILED'])
        self.assertEqual([x['stopped_for_recovery'] for x in failures], [False, False, True])
        self.assertTrue(report['test_watch_returned'])
        self.assertFalse(report['production_watch_started'])
        self.assertFalse(report['production_credentials_read'])


class WatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watch-guard-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_unknown_scenario_refused_before_image_or_filesystem_mutation(self):
        with patch.object(MetadataEngine, 'image') as image:
            with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_SCENARIO_INVALID'):
                isolated.create_run(self.base/'never-created', MetadataEngine(), '../../foreign')
        image.assert_not_called()
        self.assertFalse((self.base/'never-created').exists())

    def test_existing_discovery_record_still_valid_without_scenario(self):
        directory, record = isolated.create_run(self.base, MetadataEngine())
        record.pop('scenario')
        isolated.write(directory/'run.json', record)
        self.assertEqual(isolated.validate_run(directory)['owner'], record['owner'])

    def test_watch_refuses_discovery_state_before_preparation(self):
        directory, _ = isolated.create_run(self.base, MetadataEngine())
        with patch.object(isolated, 'prepare_test_ops') as prepare:
            with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_SCENARIO_INVALID'):
                watch.exercise(directory, MetadataEngine())
        prepare.assert_not_called()

    def test_watch_cleanup_refuses_foreign_scenario_before_docker(self):
        directory, _ = isolated.create_run(self.base, MetadataEngine())
        with patch.object(sys, 'argv', ['watch_acceptance.py', 'cleanup', '--run-dir', str(directory)]), \
             patch.object(isolated, 'Engine') as engine, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(watch.main(), 2)
        engine.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['code'], 'ACCEPTANCE_SCENARIO_INVALID')

    def test_copied_watch_worker_imports_without_workspace_or_test_packages(self):
        directory, _ = isolated.create_run(self.base, MetadataEngine(), 'watch')
        script = directory/'project/src/ops/platform/ops/watch_acceptance.py'
        env = {k: v for k, v in os.environ.items() if not k.startswith('PYTHON')}
        result = subprocess.run([sys.executable, '-B', str(script), '--help'], cwd=directory,
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cleanup', result.stdout)

    def test_run_cli_uses_bounded_watch_scenario(self):
        with patch.object(sys, 'argv', ['watch_acceptance.py', 'run', '--state-dir', str(self.base)]), \
             patch.object(isolated, 'run', return_value={'stage': watch.STAGE, 'result': 'passed'}) as run, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(watch.main(), 0)
        run.assert_called_once_with(self.base, scenario='watch')
        self.assertEqual(json.loads(output.getvalue())['stage'], watch.STAGE)

    def test_run_rejects_user_supplied_existing_run_directory(self):
        with patch.object(sys, 'argv', ['watch_acceptance.py', 'run', '--run-dir', str(self.base)]), \
             patch.object(isolated, 'run') as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(watch.main(), 2)
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['code'], 'ACCEPTANCE_RUN_DIR_NOT_ALLOWED')


class SupervisorTests(unittest.TestCase):
    def scenario(self, failure=None, wrong_stage=False, cleanup_failure=False):
        temp = tempfile.TemporaryDirectory(prefix='watch-supervisor-')
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        events = []
        test = self

        class Engine(MetadataEngine):
            def compose(self, *args):
                events.append('compose')
            def cleanup(self, directory):
                events.append('cleanup')
                if cleanup_failure:
                    raise ReadFailure('ACCEPTANCE_CLEANUP_INCOMPLETE')
                return {'owned_containers_removed': 9, 'owned_networks_removed': 7,
                        'protected_containers_unchanged': 0, 'images_or_volumes_removed': False}

        class Child:
            def __init__(self, args, **kwargs):
                events.append('worker')
                test.assertTrue(args[2].endswith('/src/ops/platform/ops/watch_acceptance.py'))
                test.assertTrue(kwargs['start_new_session'])
                test.assertNotIn('OPS_ACCESS_CODE', kwargs['env'])
                test.assertNotIn('COMPOSE_PROJECT_NAME', kwargs['env'])
                directory = Path(args[-1])
                isolated.write(directory/'worker-report.json', {
                    'stage': isolated.STAGE if wrong_stage else watch.STAGE, 'result': 'passed'})
            def wait(self, timeout):
                events.append('wait')
                test.assertEqual(timeout, 600)
                if failure:
                    raise failure
                return 0

        with patch.object(isolated, 'Engine', Engine), patch.object(isolated.subprocess, 'Popen', Child), \
             patch.object(isolated, 'stop_worker', side_effect=lambda child: events.append('stop')), \
             patch.dict(os.environ, {'OPS_ACCESS_CODE': 'private-do-not-pass', 'COMPOSE_PROJECT_NAME': 'foreign'}):
            report = isolated.run(base, scenario='watch')
        return report, events

    def test_watch_selected_and_scoped_cleanup_completed(self):
        report, events = self.scenario()
        self.assertEqual(report['stage'], watch.STAGE)
        self.assertEqual(report['result'], 'passed')
        self.assertEqual(events.count('compose'), 8)
        self.assertEqual(events[-1], 'cleanup')
        self.assertNotIn('stop', events)

    def test_timeout_stops_worker_before_cleanup_and_does_not_claim_watch_never_started(self):
        report, events = self.scenario(subprocess.TimeoutExpired('worker', 600))
        self.assertEqual(report['result'], 'attention_required')
        self.assertEqual(events[-2:], ['stop', 'cleanup'])
        self.assertTrue(report['test_watch_requested'])
        self.assertFalse(report['production_watch_started'])
        self.assertNotIn('watch_started', report)

    def test_interrupt_stops_worker_before_cleanup(self):
        report, events = self.scenario(KeyboardInterrupt())
        self.assertEqual(report['code'], 'ACCEPTANCE_INTERRUPTED')
        self.assertEqual(events[-2:], ['stop', 'cleanup'])

    def test_wrong_report_stage_is_rejected_and_cleaned(self):
        report, events = self.scenario(wrong_stage=True)
        self.assertEqual(report['code'], 'ACCEPTANCE_REPORT_STAGE_MISMATCH')
        self.assertEqual(report['result'], 'attention_required')
        self.assertEqual(events[-1], 'cleanup')

    def test_cleanup_failure_prevents_passed_result(self):
        report, events = self.scenario(cleanup_failure=True)
        self.assertEqual(report['result'], 'attention_required')
        self.assertEqual(report['cleanup']['code'], 'ACCEPTANCE_CLEANUP_INCOMPLETE')
        self.assertEqual(events[-1], 'cleanup')


if __name__ == '__main__':
    unittest.main()
