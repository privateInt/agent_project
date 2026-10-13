import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import lf_common as common
import manage
import diagnose
from test_langfuse import setup


def item(s, name='lf-minio', status='exited', healthy=None):
    return {'Id': 'a' * 64, 'Config': {'User': '1006:1006', 'Labels': {
        common.OWNER: s['owner'], 'com.docker.compose.project': common.PROJECT,
        'com.docker.compose.service': name},
        'Env': ['MINIO_ROOT_PASSWORD=' + s['s3_secret'], 'OTHER_AUTH_TOKEN=new-private-value']},
        'State': {'Status': status, 'Running': status == 'running', 'ExitCode': 1,
                  'OOMKilled': False, 'Error': 'startup failed',
                  'Health': {'Status': healthy, 'Log': [{'ExitCode': 1, 'Output': 'Access denied ' + s['s3_secret']}]}},
        'RestartCount': 2}


class FakeDocker:
    def __init__(self, items, logs=''):
        self.items, self.logs, self.calls = items, logs, []

    def run(self, args, timeout=120, combined=False):
        self.calls.append(args)
        if args[0] == 'ps': return '\n'.join(x['Id'] for x in self.items)
        if args[0] == 'logs':
            assert combined
            return self.logs
        raise AssertionError('unexpected Docker operation: ' + str(args))

    def json(self, args):
        self.calls.append(args)
        assert args[0] == 'inspect'
        return self.items


class DiagnoseTests(unittest.TestCase):
    def test_report_reads_only_and_preserves_settings_keys_and_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            docker = FakeDocker([item(s)], 'FATAL: permission denied\npassword=' + s['password'])
            def snapshot():
                return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                        for p in root.rglob('*') if p.is_file()}
            before = snapshot()
            r = diagnose.report(docker, root)
            self.assertEqual(before, snapshot())
            self.assertEqual(r['services']['lf-minio']['state'], 'exited')
            self.assertEqual(r['services']['lf-web']['state'], 'not_created')
            self.assertIn('permission denied', r['services']['lf-minio']['recent_log_excerpt'])
            self.assertNotIn(s['password'], json.dumps(r))
            self.assertNotIn(s['s3_secret'], json.dumps(r))
            self.assertFalse(r['restart_executed'])
            self.assertTrue(all(c[0] in ('ps', 'inspect', 'logs') for c in docker.calls))

    def test_raw_stderr_is_captured_but_exception_string_never_leaks(self):
        completed = subprocess.CompletedProcess(['docker'], 1, stdout='progress', stderr='PASSWORD=private-value')
        with patch.object(common.subprocess, 'run', return_value=completed):
            with self.assertRaises(common.DockerFailure) as caught:
                common.Docker().run(['compose', '--project-name', common.PROJECT, 'up'])
        self.assertNotIn('private-value', str(caught.exception))
        self.assertIn('Docker compose up', str(caught.exception))
        self.assertIn('private-value', caught.exception.detail)
        completed.returncode = 0
        with patch.object(common.subprocess, 'run', return_value=completed):
            self.assertIn('PASSWORD=private-value', common.Docker().run(['logs', 'a' * 64], combined=True))

    def test_literal_encoded_json_uri_and_header_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td))
            instance = item(s)
            instance['Config']['Env'].extend(['AUTH_DISABLE_SIGNUP=true', 'NEXTAUTH_URL=http://localhost:16320'])
            redact = diagnose.Redactor(s, [instance])
            auth = base64.b64encode((s['public_key'] + ':' + s['secret_key']).encode()).decode()
            variants = [s['password'], json.dumps(s['password'])[1:-1], urllib.parse.quote(s['password'], safe=''),
                        s['email'], 'new-private-value', auth]
            raw = '\n'.join(variants) + '\npostgresql://unknown:unlisted@host/db\nAuthorization: Bearer some-unlisted-token'
            cleaned = redact.text(raw)
            for secret in variants + ['unknown:unlisted', 'some-unlisted-token']:
                self.assertNotIn(secret, cleaned)
            self.assertIn('postgresql://[REDACTED]@host/db', cleaned)
            self.assertEqual(redact.text('expected true, received false'), 'expected true, received false')

    def test_redaction_happens_before_output_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            s = setup(Path(td))
            s['password'] = 'VeryLongSecretThatCrossesTheTruncationBoundary'
            r = diagnose.Redactor(s, [])
            value = r.text('x' * 10 + s['password'] + ' tail', limit=20)
            self.assertNotIn('VeryLong', value)

    def test_foreign_container_never_has_logs_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            other = item(s)
            other['Config']['Labels'][common.OWNER] = 'different-owner'
            docker = FakeDocker([other])
            with self.assertRaises(ValueError): diagnose.report(docker, root)
            self.assertFalse(any(c[0] == 'logs' for c in docker.calls))

    def test_healthy_logs_are_not_read_and_missing_storage_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            (root / 'data/minio').rmdir()
            docker = FakeDocker([item(s, status='running', healthy='healthy')])
            r = diagnose.report(docker, root)
            self.assertFalse(r['storage_directories']['minio']['exists_or_accessible'])
            self.assertFalse(any(c[0] == 'logs' for c in docker.calls))

    def test_partial_start_and_compose_error_are_not_reported_as_passed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            failure = common.DockerFailure(['compose', 'up'], 1, '', 'container failed; secret=' + s['secret_key'])
            r = diagnose.report(FakeDocker([]), root, failure)
            self.assertEqual(r['result'], 'diagnosed')
            self.assertEqual(len(r['not_healthy']), 6)
            self.assertNotIn(s['secret_key'], json.dumps(r))
            self.assertEqual(r['compose_failure']['exit_code'], 1)

    def test_diagnose_selector_does_not_prepare_or_up(self):
        with patch.object(manage, 'Docker'), patch.object(diagnose, 'report', return_value={'result': 'diagnosed'}) as report, \
             patch.object(manage, 'prepare', side_effect=AssertionError('mutation')), \
             patch.object(manage, 'host_info', side_effect=AssertionError('strict preflight')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(manage.main(['diagnose']), 0)
            self.assertEqual(report.call_count, 1)

    def test_up_failure_diagnoses_once_without_retry_and_still_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = setup(root)
            class Docker:
                calls = []
                def run(self, args, timeout=120):
                    self.calls.append(args)
                    if 'up' in args: raise common.DockerFailure(args, 1, '', 'dependency failed')
                    return ''
                def json(self, args):
                    if args[:2] == ['image', 'inspect']: return [{'RepoDigests': [args[2]]}]
                    return {}
            docker = Docker()
            with patch.object(manage, 'resources', return_value={}), patch.object(manage, 'owned_containers', return_value=[]), \
                 patch.object(manage, 'check_networks'), patch.object(manage, 'port_free'), patch.object(manage, 'config_contract'), \
                 patch.object(diagnose, 'report', return_value={'result': 'diagnosed'}) as report, \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(common.DockerFailure): manage.up(docker, s, s['host'])
            self.assertEqual(report.call_count, 1)
            self.assertEqual(len([x for x in docker.calls if 'up' in x]), 1)


if __name__ == '__main__':
    unittest.main()
