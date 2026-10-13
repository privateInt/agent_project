"""Acceptance runner guards. Full real HTTP scenarios run via fault_check --local."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import fault_check as check, fault_probe as probe


class FakeDocker:
    def __init__(self):
        self.env = {}
        self.calls = []
        self.rows = {k: {'id': ('a' if k == 'ops' else 'b')*64, 'image': 'sha256:'+'c'*64,
            'started': '2026-09-27T01:47:33Z', 'running': True, 'health': 'healthy',
            'project': s[0], 'service': s[1],
            'user': f'{os.getuid()}:{os.getgid()}' if k == 'ops' else '65532:65532'} for k, s in check.SPECS.items()}

    def command(self, args):
        self.calls.append(args)
        if args[0] == 'ps':
            return self.rows['ops' if args[3].endswith('agent-project-ops') else 'agent']['id']
        if args[0] == 'inspect':
            return json.dumps(next(v for v in self.rows.values() if v['id'] == args[-1]))
        raise AssertionError('Unexpected Docker control')


def result(kind):
    cases = [{'result': 'passed', 'fault': mode, **({'source': source} if kind == 'ops' else {})}
             for source in (('prometheus', 'langfuse') if kind == 'ops' else ('export',)) for mode in probe.MODES]
    return {'component': kind, 'result': 'passed', 'cases': cases, 'queue_pressure': {'result': 'passed'},
            'synthetic_only': True, 'production_api_requests': 0, 'production_credentials_read': False,
            'source_hashes_verified': len(check.hashes(kind))}


class FaultCheckTests(unittest.TestCase):
    def test_guard_denies_external_ports_processes_and_state(self):
        guard = probe.Guard();guard.ports.add(12345)
        guard.audit('socket.connect', (None, ('127.0.0.1', 12345)))
        guard.audit('socket.bind', (None, ('127.0.0.1', 0)))
        guard.audit('socket.bind', (None, ('::1', 0)))
        for event, args in [('socket.connect', (None, ('127.0.0.1', 16320))),
                            ('socket.connect', (None, ('example.org', 443))),
                            ('socket.bind', (None, ('0.0.0.0', 0))),
                            ('open', ('/run/secrets/real-key',)),
                            ('open', ('/var/lib/ops/api-success.json',)),
                            ('subprocess.Popen', ('docker',))]:
            with self.subTest(event=event), self.assertRaises(probe.ProbeFailure):
                guard.audit(event, args)
        self.assertEqual(guard.blocked, 6)

    def test_worker_refuses_runtime_hash_mismatch_before_import(self):
        source = Path(check.__file__)
        script = ("import runpy; m=runpy.run_path(" + repr(str(Path(probe.__file__))) + "); "
                  "raise SystemExit(m['main']('ops', {"+repr(str(source))+": 'wrong'}))")
        completed = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stdout)['code'], 'RUNTIME_SOURCE_MISMATCH')

    def test_parse_requires_complete_scenarios_and_separate_scope(self):
        for kind in check.SPECS:
            good = result(kind)
            self.assertEqual(check.parse_probe(SimpleNamespace(returncode=0, stdout=json.dumps(good)), kind), good)
            for change in ({'cases': []}, {'cases': ['bad']}, {'production_api_requests': 1}, {'synthetic_only': False}):
                with self.subTest(kind=kind, change=change), self.assertRaises(ReadFailure):
                    check.parse_probe(SimpleNamespace(returncode=0, stdout=json.dumps(dict(good, **change))), kind)

    def test_docker_uses_only_inspect_and_exact_exec_ids(self):
        docker = FakeDocker();execs = []
        def invoke(args, payload, env):
            execs.append(args)
            kind = 'ops' if args[3] == 'a'*64 else 'agent'
            self.assertEqual(args, ['docker', 'exec', '-i', docker.rows[kind]['id'], 'python3', '-B', '-'])
            self.assertIn('RUNTIME_SOURCE_MISMATCH', payload)
            return SimpleNamespace(returncode=0, stdout=json.dumps(result(kind)))
        with patch.object(check, 'Docker', return_value=docker), patch.object(check, 'invoke', side_effect=invoke):
            actual = check.run()
        self.assertEqual(actual['result'], 'passed', actual)
        self.assertEqual(len(execs), 2)
        self.assertTrue(all(c[0] in ('ps', 'inspect') for c in docker.calls))
        self.assertFalse(actual['restart_requested'])
        self.assertFalse(actual['live_backend_outage_or_database_acceptance'])

    def test_unhealthy_or_wrong_labels_refuse_before_exec(self):
        for key, value in [('health', 'unhealthy'), ('project', 'other'), ('user', 'unexpected')]:
            docker = FakeDocker();docker.rows['agent'][key] = value
            with patch.object(check, 'Docker', return_value=docker), patch.object(check, 'invoke') as invoke:
                actual = check.run()
            self.assertEqual(actual['result'], 'attention_required')
            invoke.assert_not_called()

    def test_container_restart_during_probe_is_not_accepted(self):
        docker = FakeDocker()
        def invoke(*_):
            docker.rows['ops']['started'] = '2026-09-27T02:00:00Z'
            return SimpleNamespace(returncode=0, stdout=json.dumps(result('ops')))
        with patch.object(check, 'Docker', return_value=docker), patch.object(check, 'invoke', side_effect=invoke):
            actual = check.run()
        self.assertEqual(actual['error']['code'], 'CONTAINER_CHANGED_DURING_PROBE')

    def test_failed_probe_reports_code_without_stderr_or_unverified_pass(self):
        docker = FakeDocker()
        completed = SimpleNamespace(returncode=2, stdout=json.dumps({'component': 'ops', 'result': 'attention_required',
            'code': 'FAULT_ENVELOPE_INCORRECT'}), stderr='private sentinel')
        with patch.object(check, 'Docker', return_value=docker), patch.object(check, 'invoke', return_value=completed):
            actual = check.run()
        self.assertEqual(actual['error']['code'], 'FAULT_ENVELOPE_INCORRECT')
        self.assertEqual(actual['components'], {})
        self.assertNotIn('private', json.dumps(actual))

    def test_timeout_does_not_request_stop_or_restart(self):
        with patch.object(check.subprocess, 'run', side_effect=subprocess.TimeoutExpired('probe', 85)) as called:
            with self.assertRaises(ReadFailure) as caught:
                check.invoke(['docker', 'exec', '-i', 'a'*64, 'python3', '-B', '-'], 'code', {})
        self.assertEqual(caught.exception.code, 'PROBE_TIMEOUT')
        self.assertEqual(called.call_count, 1)


if __name__ == '__main__':
    unittest.main()
