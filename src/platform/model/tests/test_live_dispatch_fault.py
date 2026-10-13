"""Local orchestration/ownership checks. These do not replace live Docker tests."""
import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

import live_dispatch_fault as smoke

OP = '11111111-1111-4111-8111-111111111111'
DEP = '22222222-2222-4222-8222-222222222222'
OLD = '33333333-3333-4333-8333-333333333333'
NEW = '44444444-4444-4444-8444-444444444444'
EVENT = '55555555-5555-4555-8555-555555555555'


class FakeLab(smoke.Lab):
    def preflight(self):
        self.network = 'test-network'
        self.job_image = 'test-gated-image'
        self.management = 'read-only-management:9000'
        self.current = {'operation_id': OP, 'status': 'pending', 'phase': 'queued', 'attempt': 0,
                        'dispatch_generation': 0, 'dispatch_token': None, 'execution_instance_id': None,
                        'execution_started_at': None, 'completed_at': None, 'updated_at': '2026-09-08T07:00:00+00:00',
                        'observed_at': '2026-09-08T07:00:00+00:00', 'dispatch_lease_until': '2026-09-08T07:02:00+00:00'}
        self.publications = 1
        self.killed = False
        self.stale_completes = False
        self.cleanup_result = []
        self.domain = {'status': 'ready', 'deployment_id': DEP, 'updated_at': '2026-09-08T07:02:03+00:00'}

    def setup(self):
        self.dispatch_env = {}
        self.operation_address = self.names['operation'] + ':9000'
        self.deployment_address = self.names['deployment'] + ':9000'

    def helper(self, *args, **kwargs):
        return {'operation_id': OP, 'deployment_id': DEP}

    def operation(self):
        return dict(self.current)

    def outbox(self):
        return {'event_id': EVENT, 'publish_attempts': self.publications, 'published': True}

    def row(self, *args):
        return dict(self.domain)

    def start(self, role, binary, env, extra=(), image=smoke.TEST_IMAGE):
        if role == 'fault':
            self.current.update(status='dispatching', phase='dispatching', attempt=1, dispatch_generation=1, dispatch_token=OLD)
            (self.temp / 'claim.json').write_text(json.dumps({'operation_id': OP, 'resource_id': DEP, 'dispatch_token': OLD}))
        elif role == 'reconciler':
            self.current.update(status='pending', phase='launch_retry_queued', dispatch_token=None, observed_at='2026-09-08T07:02:01+00:00')
            self.publications = 2
        elif role == 'recovery':
            self.current.update(status='succeeded', phase='completed', dispatch_generation=2, dispatch_token=NEW,
                                execution_instance_id=EVENT, execution_started_at='2026-09-08T07:02:02+00:00',
                                updated_at='2026-09-08T07:02:03+00:00', completed_at='2026-09-08T07:02:03+00:00')

    def inspect(self, name):
        return {'State': {'Running': name == self.names['fault'] and not self.killed,
                          'ExitCode': 137 if self.killed else 0, 'OOMKilled': False, 'Status': 'exited'}}

    def docker(self, *args, **kwargs):
        out, err = '', ''
        if args[0] == 'kill':
            self.killed = True
        elif args[0] == 'logs' and args[1] == self.names['recovery']:
            err = '\n'.join(f'record handled topic={self.topic} partition=0 offset={n}' for n in (0, 1))
        elif args[0] == 'logs' and args[1] == self.names['stale']:
            err = 'execution not authorized; process stopped: stale execution token or input identity mismatch'
            if self.stale_completes:
                err += '\ndemo deployment completed'
        return subprocess.CompletedProcess(args, 0, out, err)

    def wait(self, label, getter, predicate, timeout, brief=lambda v: v):
        value = getter()
        smoke.require(predicate(value), 'fake wait failed: ' + label)
        return value

    def collect_logs(self):
        pass

    def cleanup(self):
        self.cleanup_called = True
        self.temporary.cleanup()
        return self.cleanup_result


class DispatchFaultChecks(unittest.TestCase):
    def setUp(self):
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)

    def lab(self):
        args = argparse.Namespace(output=self.output.name, timeout=300, model_version_id=OP,
                                  postgres_container='postgres', kafka_container='ops-kafka')
        lab = FakeLab(args)
        self.addCleanup(lab.temporary.cleanup)
        return lab

    def test_scope_replaces_public_without_fallback(self):
        schema = 'dispatch_smoke_' + 'a' * 32
        url = smoke.scoped_url('postgresql://user:password@postgres/db?sslmode=disable&search_path=public', schema)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query['search_path'], [schema])
        self.assertEqual(query['sslmode'], ['disable'])
        for invalid in ('public', 'dispatch_smoke_;DROP SCHEMA public', 'dispatch_smoke_abc'):
            with self.assertRaises(RuntimeError):
                smoke.scoped_url('postgresql://user@postgres/db', invalid)
        with self.assertRaises(RuntimeError):
            smoke.scoped_url('postgresql://user@postgres/db?options=-csearch_path%3Dpublic', schema)

    def test_full_orchestration_checks_recovery_and_fence(self):
        lab = self.lab()
        with redirect_stdout(StringIO()) as output:
            lab.run()
        self.assertTrue(lab.cleanup_called)
        self.assertIn('PASS:', output.getvalue())
        report = json.loads((lab.output / 'report.json').read_text())
        self.assertTrue(report['passed'])
        self.assertEqual(report['recovery']['dispatch_generation'], 2)
        self.assertEqual(report['handled_offsets'], [0, 1])
        self.assertTrue(report['stale_token_rejected'])

    def test_variable_fraction_lease_boundary_is_exact(self):
        for observed, expired in (('2026-09-08T07:02:00.928660000+00:00', True),
                                  ('2026-09-08T07:02:00.928659999+00:00', False)):
            with self.subTest(observed=observed):
                lab = self.lab()
                original_preflight, original_start = lab.preflight, lab.start

                def preflight():
                    original_preflight()
                    lab.current['dispatch_lease_until'] = '2026-09-08T07:02:00.92866+00:00'

                def start(role, *args, **kwargs):
                    original_start(role, *args, **kwargs)
                    if role == 'reconciler':
                        lab.current['observed_at'] = observed

                lab.preflight, lab.start = preflight, start
                with redirect_stdout(StringIO()):
                    if expired:
                        lab.run()
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'before the original lease expired'):
                            lab.run()
                self.assertTrue(lab.cleanup_called)

    def test_stale_completion_fails_and_still_cleans(self):
        lab = self.lab()
        original = lab.setup
        def setup():
            original()
            lab.stale_completes = True
        lab.setup = setup
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, 'performed domain completion'):
            lab.run()
        self.assertTrue(lab.cleanup_called)
        self.assertFalse(json.loads((lab.output / 'report.json').read_text())['passed'])

    def test_cleanup_error_prevents_pass(self):
        lab = self.lab()
        original = lab.setup
        def setup():
            original()
            lab.cleanup_result = ['test consumer group remains']
        lab.setup = setup
        with redirect_stdout(StringIO()) as output, self.assertRaisesRegex(RuntimeError, 'consumer group remains'):
            lab.run()
        self.assertNotIn('PASS:', output.getvalue())
        self.assertFalse(json.loads((lab.output / 'report.json').read_text())['passed'])

    def test_foreign_container_is_never_removed(self):
        lab = self.lab()
        with patch.object(lab, 'inspect', return_value={'Labels': {smoke.LABEL: 'other-run'}}), patch.object(lab, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'foreign container'):
                lab.remove_owned('some-container')
            docker.assert_not_called()

    def test_cleanup_refuses_foreign_schema_marker(self):
        lab = self.lab()
        lab.schemas = ['model_deployment']
        calls = []
        def sql(database, statement):
            calls.append(statement)
            return 'different-test-owner'
        empty = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(lab, 'inspect', return_value=None), patch.object(lab, 'docker', return_value=empty), patch.object(lab, 'sql', sql):
            errors = smoke.Lab.cleanup(lab)
        self.assertTrue(any('different ownership marker' in e for e in errors))
        self.assertFalse(any('DROP SCHEMA' in query for query in calls))

    def test_topic_cleanup_uses_an_exact_escaped_pattern(self):
        lab = self.lab()
        lab.topic_owned = True
        calls = []
        empty = subprocess.CompletedProcess([], 0, '', '')
        def kafka(script, *args, **kwargs):
            calls.append(args)
            return empty
        with patch.object(lab, 'inspect', return_value=None), patch.object(lab, 'docker', return_value=empty), patch.object(lab, 'kafka', kafka, create=True):
            self.assertEqual(smoke.Lab.cleanup(lab), [])
        delete = next(args for args in calls if '--delete' in args)
        pattern = delete[delete.index('--topic') + 1]
        self.assertIsNotNone(re.fullmatch(pattern, lab.topic))
        self.assertIsNone(re.fullmatch(pattern, lab.topic.replace('.', 'X')))
        self.assertIsNone(re.fullmatch(pattern, lab.topic + '-other'))

    def test_changed_winner_is_detected(self):
        lab = self.lab()
        lab.preflight()
        before = lab.operation()
        after = dict(before, execution_instance_id=EVENT)
        self.assertNotEqual(smoke.fence_snapshot(before), smoke.fence_snapshot(after))


if __name__ == '__main__':
    unittest.main()
