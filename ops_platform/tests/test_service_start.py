"""Production-entry orchestration with synthetic metadata and real worker children."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

from ops_platform.contracts import ReadFailure
from ops_platform.ops import service_start as entry, service_lifecycle as service
from ops_platform.ops import refresh_worker as worker, worker_state as ws
from ops_platform.ops.service_systemd import CommandFailure
from ops_platform.tests import test_service_lifecycle as fixtures


def observation(count=1, invocation='a'*32, classification='accepted', live=True):
    return {'status': 'owned', 'unit_name': 'synthetic', 'persistence': 'session', 'enabled': False,
            'provider_status': {'ActiveState': 'active', 'InvocationID': invocation, 'MainPID': '123'},
            'desired_state': 'running', 'recovery': None, 'current_invocation_verified': live,
            'evaluation_status': 'healthy' if classification == 'accepted' else 'degraded_retrying',
            'worker': {'intent': None, 'last_observation': {'phase': 'waiting', 'service_invocation': invocation,
                'completed_cycles': count, 'outcome': {'result': 'passed', 'classification': classification,
                'configuration_changed': False, 'restart_requested': False, 'generation': None,
                'child_process_group_stopped': True}}}}


class FakeController:
    def __init__(self):
        self.report = {'environment_ready': True, 'result': 'passed', 'blockers': [], 'warnings': ['SESSION_SCOPE_ONLY'],
                       'prepared_state_stable': True, 'management': {'status': 'not_installed'},
                       'installation': {'provenance': {'effective_managed_fields': ['resource_profile']}}}
        self.value = {'interval_seconds': 60, 'deadline_seconds': 180, 'persistence': 'session',
                      'binding': {'ops_container_id': 'b'*64, 'ops_image_id': 'sha256:'+'c'*64}}
        self.current = observation(); self.starts = 0; self.connections = 0; self.verified = 0
        self.submitted = {'result': 'passed', 'start_request_submitted': True, 'unit_written': True}
    def connect(self): self.connections += 1
    def manifest(self): return deepcopy(self.value)
    def verify_binding(self, manifest): self.verified += 1
    def status(self): return deepcopy(self.current)
    def start(self, policy, interval): self.starts += 1; return deepcopy(self.submitted)


class ObservationTests(unittest.TestCase):
    def setUp(self): self.ctl = FakeController(); self.clock = 0; self.tick = lambda: None
    def sleep(self, seconds): self.clock += seconds; self.tick()
    def run_entry(self, action='run', **kwargs):
        return entry.run(action, Path('/synthetic/state'), controller_factory=lambda *a: self.ctl,
                         _clock=lambda: self.clock, _sleep=self.sleep, wait_seconds=3, **kwargs)
    def running(self):
        self.ctl.report['management'] = deepcopy(self.ctl.current)
        self.ctl.submitted = {'result': 'passed', 'already_running': True}

    def test_new_service_requires_accepted_live_evaluation_and_left_running(self):
        result = self.run_entry()
        self.assertEqual(result['result'], 'passed'); self.assertEqual(self.ctl.starts, 1)
        self.assertTrue(result['fresh_evaluation_verified']); self.assertTrue(result['worker_left_running'])
        self.assertFalse(result['logout_or_boot_acceptance']); self.assertFalse(result['observer_requested_stop'])

    def test_preflight_blocker_prevents_connect_or_start(self):
        self.ctl.report.update(environment_ready=False, blockers=['SYSTEMD_USER_MANAGER_UNAVAILABLE'])
        result = self.run_entry()
        self.assertEqual(result['code'], 'SERVICE_PREFLIGHT_BLOCKED')
        self.assertEqual((self.ctl.starts, self.ctl.connections), (0, 0))
        self.assertEqual(result['preflight']['blockers'], ['SYSTEMD_USER_MANAGER_UNAVAILABLE'])

    def test_existing_success_cannot_pass_check_without_a_new_cycle(self):
        self.running(); result = self.run_entry('check')
        self.assertEqual(result['code'], 'SERVICE_EVALUATION_WAIT_TIMEOUT')
        self.assertEqual(self.ctl.starts, 0); self.assertFalse(result['fresh_evaluation_verified'])
        self.assertTrue(result['worker_running_at_last_observation'])

    def test_check_accepts_next_cycle_without_start_or_state_writes(self):
        self.running(); self.tick = lambda: self.ctl.current.update(observation(2))
        result = self.run_entry('check')
        self.assertEqual(result['result'], 'passed'); self.assertEqual(self.ctl.starts, 0)
        self.assertEqual(self.ctl.verified, 1)

    def test_duplicate_run_waits_for_new_cycle_and_does_not_claim_new_start(self):
        self.running(); self.tick = lambda: self.ctl.current.update(observation(2))
        result = self.run_entry()
        self.assertEqual(result['result'], 'passed'); self.assertEqual(self.ctl.starts, 1)
        self.assertTrue(result['already_running']); self.assertFalse(result['start_request_submitted'])

    def test_check_of_missing_unit_never_installs(self):
        result = self.run_entry('check')
        self.assertEqual(result['code'], 'SERVICE_NOT_INSTALLED'); self.assertEqual(self.ctl.starts, 0)

    def test_stale_pid_or_invocation_evidence_is_not_accepted(self):
        self.ctl.current = observation(live=False)
        result = self.run_entry(); self.assertEqual(result['code'], 'SERVICE_EVALUATION_WAIT_TIMEOUT')

    def test_changed_invocation_during_wait_is_not_treated_as_first_success(self):
        self.running(); self.tick = lambda: self.ctl.current.update(observation(2, 'd'*32))
        result = self.run_entry('check')
        self.assertEqual(result['code'], 'SERVICE_INVOCATION_CHANGED_DURING_OBSERVATION')

    def test_invocation_in_start_receipt_is_not_replaced_before_first_poll(self):
        self.ctl.submitted['management'] = observation(invocation='d'*32)
        self.assertEqual(self.run_entry()['code'], 'SERVICE_INVOCATION_CHANGED_DURING_OBSERVATION')
        # A previous inactive invocation is not evidence of this start.
        self.ctl.submitted['management']['provider_status']['ActiveState'] = 'inactive'
        self.assertEqual(self.run_entry()['result'], 'passed')

    def test_transient_degradation_can_recover_without_second_start(self):
        self.ctl.current = observation(classification='retryable_prewrite')
        self.tick = lambda: self.ctl.current.update(observation(2))
        result = self.run_entry()
        self.assertEqual(result['result'], 'passed'); self.assertEqual(self.ctl.starts, 1)

    def test_recovery_gate_never_auto_resumes_or_uses_old_success(self):
        self.ctl.current.update(desired_state='paused', recovery='OPS_ALERT_READBACK_FAILED')
        result = self.run_entry()
        self.assertEqual(result['code'], 'WORKER_RECOVERY_REQUIRED'); self.assertEqual(self.ctl.starts, 1)
        self.assertFalse(result['fresh_evaluation_verified'])

    def test_concurrent_pause_prevents_acceptance(self):
        self.ctl.current['desired_state'] = 'paused'
        self.assertEqual(self.run_entry()['code'], 'WORKER_PAUSED')

    def test_live_open_intent_waits_but_waiting_success_with_open_intent_fails(self):
        self.ctl.current['worker']['intent'] = {'phase': 'open'}
        result = self.run_entry(); self.assertEqual(result['code'], 'WORKER_UNFINISHED_INTENT')
        self.ctl.current['worker']['last_observation']['phase'] = 'evaluating'
        self.ctl.current['evaluation_status'] = 'in_progress'
        self.tick = lambda: self.ctl.current.update(observation())
        self.assertEqual(self.run_entry()['result'], 'passed')

    def test_unknown_start_outcome_preserves_only_bounded_command_diagnostic(self):
        self.ctl.submitted = {'result': 'attention_required', 'code': 'SERVICE_COMMAND_OUTCOME_UNKNOWN',
            'service_command': {'operation': 'start', 'outcome': 'timeout', 'stderr': 'secret'}}
        result = self.run_entry()
        self.assertEqual(result['code'], 'SERVICE_COMMAND_OUTCOME_UNKNOWN')
        self.assertEqual(result['service_command'], {'operation': 'start', 'outcome': 'timeout'})
        self.assertNotIn('secret', json.dumps(result)); self.assertEqual(self.ctl.starts, 1)

    def test_query_error_or_keyboard_interrupt_does_not_stop_worker(self):
        for error, code in [(CommandFailure('SERVICE_QUERY_TIMEOUT', 'inspect_unit', 'timeout'), 'SERVICE_QUERY_TIMEOUT'),
                            (KeyboardInterrupt(), 'SERVICE_OBSERVATION_INTERRUPTED')]:
            with self.subTest(code=code):
                self.ctl.status = lambda: (_ for _ in ()).throw(error)
                result = self.run_entry()
                self.assertEqual(result['code'], code); self.assertFalse(result['observer_requested_stop'])

    def test_changed_binding_prevents_duplicate_start(self):
        self.running()
        self.ctl.verify_binding = lambda _: (_ for _ in ()).throw(ReadFailure('SERVICE_BINDING_CHANGED'))
        self.assertEqual(self.run_entry()['code'], 'SERVICE_BINDING_CHANGED'); self.assertEqual(self.ctl.starts, 0)

    def test_saved_boot_policy_is_not_downgraded_or_paused_by_session_default(self):
        self.running(); self.ctl.value['persistence'] = 'boot'
        self.assertEqual(self.run_entry()['code'], 'SERVICE_POLICY_CHANGE_REQUIRES_REVIEW')
        self.assertEqual(self.ctl.starts, 0)
        self.tick = lambda: self.ctl.current.update(observation(2))
        self.assertEqual(self.run_entry(persistence='auto')['result'], 'passed')

    def test_manifest_change_during_observation_is_not_accepted(self):
        self.ctl.current = observation(classification='retryable_prewrite')
        self.tick = lambda: self.ctl.value.update(interval_seconds=120)
        self.assertEqual(self.run_entry()['code'], 'SERVICE_MANIFEST_CHANGED_DURING_OBSERVATION')

    def test_incomplete_or_unreaped_child_evidence_is_not_accepted(self):
        self.ctl.current['worker']['last_observation']['outcome']['child_process_group_stopped'] = False
        self.assertEqual(self.run_entry()['code'], 'WORKER_ACCEPTANCE_EVIDENCE_INVALID')


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.child = None; self.addCleanup(self.stop_child)
    def stop_child(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try: self.child.wait(timeout=5)
            except subprocess.TimeoutExpired: self.child.kill(); self.child.wait(timeout=5)
    def start_child(self):
        manager = self.f.manager
        manager.invocation = uuid.uuid4().hex; manager.starts += 1; manager.control_commands += 1
        program = '''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from ops_platform.ops import refresh_worker as w, service_lifecycle as s
d=Path(sys.argv[2]);m=s.load(d)
r=w.run(d,interval=30,deadline_seconds=3,snapshot_fn=lambda _:m['binding'],
    evaluate_fn=lambda *_:{'result':'passed','classification':'accepted','generation':None,
                          'configuration_changed':False,'restart_requested':False},
    service_invocation=sys.argv[3])
sys.exit(r['exit_code'])
'''
        self.child = subprocess.Popen([sys.executable, '-I', '-B', '-c', program, str(worker.ROOT),
            str(self.f.directory), manager.invocation], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            seen = ws.Store(self.f.directory).optional('status.json') or {}
            if seen.get('service_invocation') == manager.invocation:
                manager.pid = str(seen['identity']['proc_pid']); manager.active = True; return
            if self.child.poll() is not None: break
            time.sleep(.02)
        self.fail('worker fixture failed to start')

    def controller(self):
        fixture = self.f
        class LiveFixture:
            report = {'environment_ready': True, 'result': 'passed', 'blockers': [],
                      'management': {'status': 'not_installed'}}
            def connect(self): pass
            def manifest(self): return service.load(fixture.directory)
            def verify_binding(self, manifest):
                if manifest['binding'] != fixture.binding: raise ReadFailure('SERVICE_BINDING_CHANGED')
            def start(self, policy, interval): return fixture.call('start', persistence=policy, interval=interval)
            def status(self): return service.status(fixture.directory, fixture.manager)
        return LiveFixture()

    def test_real_child_first_success_and_readonly_check_cannot_replay_it(self):
        self.f.manager.start = self.start_child; ctl = self.controller()
        result = entry.run('run', self.f.directory, controller_factory=lambda *a: ctl, wait_seconds=5)
        self.assertEqual(result['result'], 'passed', result)
        self.assertTrue(result['management']['current_invocation_verified'])
        self.assertIsNone(self.child.poll()); self.assertEqual(self.f.manager.starts, 1)
        ctl.report['management'] = ctl.status()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob('*') if p.is_file()}
        result = entry.run('check', self.f.directory, controller_factory=lambda *a: ctl, wait_seconds=1)
        self.assertEqual(result['code'], 'SERVICE_EVALUATION_WAIT_TIMEOUT')
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        self.assertEqual(self.f.manager.starts, 1); self.assertIsNone(self.child.poll())

    def test_existing_paused_service_cannot_be_resumed_by_run(self):
        self.f.good('start'); self.f.good('stop'); ctl = self.controller()
        ctl.report['management'] = ctl.status()
        result = entry.run('run', self.f.directory, controller_factory=lambda *a: ctl, wait_seconds=1)
        self.assertEqual(result['code'], 'SERVICE_EXPLICIT_RESUME_REQUIRED')
        self.assertEqual(self.f.manager.starts, 1)
        self.assertEqual(ws.Store(self.f.directory).control()['desired'], 'paused')


if __name__ == '__main__': unittest.main()
