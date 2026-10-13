"""Read-only observer regressions; real child tests use isolated synthetic evaluations."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from src.ops.tools import observe_refresh as watch
from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import service_lifecycle as service, refresh_worker as worker, worker_state as ws
from src.ops.platform.ops import lifecycle_probe as probe
from src.ops.platform.tests import test_service_lifecycle as fixtures


def sample(count=10, phase='waiting', category='accepted'):
    outcome = {'result': 'passed' if category == 'accepted' else 'attention_required',
               'classification': category, 'child_process_group_stopped': True}
    if category == 'accepted': outcome.update(configuration_changed=False, restart_requested=False, generation=None)
    else: outcome['code'] = 'CONNECTION_FAILED'
    observation = {'phase': phase, 'completed_cycles': count, 'invocation': 'worker-invocation',
                   'service_invocation': 'a'*32, 'identity': {'boot_id': 'boot', 'start_ticks': '123'},
                   'checked_at': worker.activation.rc.now()}
    if phase == 'waiting': observation['outcome'] = outcome
    return {'status': 'owned', 'unit_name': 'synthetic', 'persistence': 'session', 'enabled': False,
            'provider_status': {'ActiveState': 'active', 'MainPID': '321', 'InvocationID': 'a'*32},
            'desired_state': 'running', 'recovery': None, 'current_invocation_verified': True,
            'evaluation_status': 'in_progress' if phase == 'evaluating' else 'healthy' if category == 'accepted' else 'degraded_retrying',
            'worker': {'intent': None, 'last_observation': observation}}


class FakeSource:
    def __init__(self):
        self.report = {'result': 'passed', 'environment_ready': True, 'blockers': [], 'warnings': [],
                       'prepared_state_stable': True, 'management': {'status': 'owned'}}
        self.value = {'phase': 'installed', 'interval_seconds': 60, 'deadline_seconds': 2,
                      'persistence': 'session', 'binding': {'python': sys.executable}}
        self.sample_fn = lambda: sample(); self.connects = 0; self.verifications = 0
    def connect(self): self.connects += 1
    def manifest(self): return deepcopy(self.value)
    def sample(self): return self.sample_fn()
    def verify_binding(self, value): self.verifications += 1


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.source = FakeSource(); self.clock = 0.0
        self.source.sample_fn = lambda: sample(10 + int(self.clock // 3))
        self.tick = lambda: None
    def sleep(self, seconds): self.clock += seconds; self.tick()
    def invoke(self, duration=12, **kwargs):
        return watch.run('/synthetic/state', duration=duration, poll_seconds=1,
                         source_factory=lambda _: self.source, _clock=lambda: self.clock, _sleep=self.sleep, **kwargs)

    def test_full_duration_and_new_cycles_required_not_early_exit_after_three(self):
        r = self.invoke()
        self.assertEqual(r['result'], 'passed', r); self.assertGreaterEqual(r['observed_seconds'], 12)
        self.assertEqual(r['accepted_cycles_observed'], 4); self.assertEqual(r['sample_count'], 13)
        self.assertEqual(self.source.verifications, 2); self.assertFalse(r['continuous_between_sample_uptime_verified'])

    def test_existing_large_success_count_is_not_reused_as_progress(self):
        self.source.sample_fn = lambda: sample(500)
        r = self.invoke(); self.assertEqual(r['code'], 'OBSERVATION_INSUFFICIENT_NEW_EVALUATIONS')
        self.assertEqual(r['new_completed_cycles'], 0)

    def test_duplicate_samples_do_not_duplicate_evaluation_rows(self):
        r = self.invoke()
        self.assertEqual([row['completed_cycle'] for row in r['evaluations']], [11, 12, 13, 14])

    def test_skipped_outcomes_do_not_become_all_healthy(self):
        self.source.sample_fn = lambda: sample(10 + 2*int(self.clock // 3))
        r = self.invoke(); self.assertEqual(r['code'], 'OBSERVATION_MISSED_CYCLE_OUTCOMES')
        self.assertEqual((r['new_completed_cycles'], r['accepted_cycles_observed'], r['missing_outcome_cycles']), (8, 4, 4))

    def test_degraded_cycle_then_recovery_is_reported_not_erased(self):
        self.source.sample_fn = lambda: sample(10 + int(self.clock // 3), category='retryable_prewrite' if 3 <= self.clock < 6 else 'accepted')
        r = self.invoke(); self.assertEqual(r['code'], 'OBSERVATION_DEGRADED_EVALUATION')
        self.assertEqual(r['degraded_cycles_observed'], 1); self.assertTrue(r['recovered_by_end'])

    def test_initial_degraded_state_is_kept_after_later_success(self):
        self.source.sample_fn = lambda: sample(10 + int(self.clock // 3), category='retryable_prewrite' if self.clock < 3 else 'accepted')
        r = self.invoke(); self.assertIn('INITIAL_EVALUATION_DEGRADED', r['warnings'])
        self.assertEqual(r['code'], 'OBSERVATION_DEGRADED_EVALUATION')

    def test_same_service_worker_and_process_identity_are_required(self):
        for changed in ('service', 'worker', 'pid', 'boot', 'ticks'):
            with self.subTest(changed=changed):
                self.clock = 0
                def get():
                    row = sample(10 + int(self.clock // 3))
                    if self.clock >= 3:
                        obs = row['worker']['last_observation']
                        if changed == 'service':
                            row['provider_status']['InvocationID'] = 'b'*32; obs['service_invocation'] = 'b'*32
                        elif changed == 'worker': obs['invocation'] = 'another-worker'
                        elif changed == 'pid': row['provider_status']['MainPID'] = '999'
                        elif changed == 'boot': obs['identity']['boot_id'] = 'another-boot'
                        else: obs['identity']['start_ticks'] = '456'
                    return row
                self.source.sample_fn = get
                self.assertEqual(self.invoke()['code'], 'SERVICE_PROCESS_CHANGED_DURING_OBSERVATION')

    def test_recovery_pause_or_exit_stops_observation_without_control(self):
        for update, code in [({'recovery': 'FAILED', 'desired_state': 'paused'}, 'WORKER_RECOVERY_REQUIRED'),
                             ({'desired_state': 'paused'}, 'WORKER_PAUSED'),
                             ({'provider_status': {'ActiveState': 'failed'}}, 'SERVICE_NOT_RUNNING'),
                             ({'current_invocation_verified': False}, 'WORKER_LIVE_OBSERVATION_UNVERIFIED')]:
            with self.subTest(code=code):
                self.clock = 0
                self.source.sample_fn = lambda: dict(sample(), **update)
                r = self.invoke(); self.assertEqual(r['code'], code)
                self.assertFalse(r['effects']['service_control_requested']); self.assertFalse(r['observer_requested_stop'])

    def test_regressing_counter_is_refused(self):
        self.source.sample_fn = lambda: sample(10 if self.clock == 0 else 9)
        self.assertEqual(self.invoke()['code'], 'WORKER_CYCLE_COUNT_REGRESSED')

    def test_final_active_evaluation_can_finish_within_bounded_grace(self):
        def get():
            if 12 <= self.clock < 14: return sample(13, phase='evaluating')
            return sample(14 if self.clock >= 14 else 10 + int(self.clock // 3))
        self.source.sample_fn = get
        r = self.invoke(); self.assertEqual(r['result'], 'passed', r); self.assertEqual(r['observed_seconds'], 14)

    def test_never_finishing_final_evaluation_is_not_infinite_wait(self):
        self.source.sample_fn = lambda: sample(13, phase='evaluating') if self.clock >= 12 else sample(10 + int(self.clock // 3))
        r = self.invoke(); self.assertEqual(r['code'], 'OBSERVATION_FINAL_EVALUATION_TIMEOUT')
        self.assertEqual(r['observed_seconds'], 34)

    def test_open_intent_is_allowed_only_during_evaluation(self):
        def get():
            row = sample(10 + int(self.clock // 3)); row['worker']['intent'] = {'phase': 'open'}; return row
        self.source.sample_fn = get
        self.assertEqual(self.invoke()['code'], 'WORKER_UNFINISHED_INTENT')

    def test_incomplete_child_or_mutated_same_cycle_outcome_cannot_pass(self):
        def get():
            row = sample(); row['worker']['last_observation']['outcome']['child_process_group_stopped'] = False; return row
        self.source.sample_fn = get
        self.assertEqual(self.invoke()['code'], 'WORKER_EVALUATION_EVIDENCE_INVALID')
        def mutate():
            row = sample(); row['worker']['last_observation']['outcome']['configuration_changed'] = self.clock > 0; return row
        self.source.sample_fn = mutate
        self.assertEqual(self.invoke()['code'], 'WORKER_SAME_CYCLE_OUTCOME_CHANGED')

    def test_observed_change_and_restart_are_reported_once_without_being_requested(self):
        def get():
            row = sample(10 + int(self.clock // 3))
            if 3 <= self.clock < 6:
                row['worker']['last_observation']['outcome'].update(configuration_changed=True, restart_requested=True, generation='one')
            return row
        self.source.sample_fn = get
        r = self.invoke(); self.assertEqual(r['result'], 'passed')
        self.assertEqual(sum(row['restart_requested'] for row in r['evaluations']), 1)
        self.assertFalse(r['effects']['service_control_requested'])

    def test_preflight_or_uninstalled_service_cannot_create_anything(self):
        self.source.report.update(environment_ready=False, blockers=['SYSTEMD_USER_MANAGER_UNAVAILABLE'])
        r = self.invoke(); self.assertEqual(r['code'], 'SERVICE_PREFLIGHT_BLOCKED'); self.assertEqual(self.source.connects, 0)
        self.source.report.update(environment_ready=True, blockers=[], management={'status': 'not_installed'})
        self.assertEqual(self.invoke()['code'], 'SERVICE_NOT_INSTALLED'); self.assertEqual(self.source.connects, 0)

    def test_manifest_or_binding_changes_are_not_repaired(self):
        self.tick = lambda: self.source.value.update(interval_seconds=120)
        self.assertEqual(self.invoke()['code'], 'SERVICE_MANIFEST_CHANGED_DURING_OBSERVATION')
        self.clock = 0; self.tick = lambda: None
        def verify(_): raise ReadFailure('SERVICE_BINDING_CHANGED')
        self.source.verify_binding = verify
        self.assertEqual(self.invoke()['code'], 'SERVICE_BINDING_CHANGED')

    def test_failed_end_binding_check_prevents_pass(self):
        def verify(_):
            self.source.verifications += 1
            if self.source.verifications == 2: raise ReadFailure('SERVICE_BINDING_CHANGED')
        self.source.verify_binding = verify
        r = self.invoke(); self.assertEqual(r['code'], 'SERVICE_BINDING_CHANGED'); self.assertNotEqual(r['result'], 'passed')

    def test_query_failure_or_interrupt_preserves_partial_report_without_stop(self):
        for exc, code in [(ReadFailure('SERVICE_QUERY_TIMEOUT'), 'SERVICE_QUERY_TIMEOUT'), (KeyboardInterrupt(), 'OBSERVATION_INTERRUPTED')]:
            with self.subTest(code=code):
                self.clock = 0
                def get():
                    if self.clock >= 6: raise exc
                    return sample(10 + int(self.clock // 3))
                self.source.sample_fn = get
                r = self.invoke(); self.assertEqual(r['code'], code); self.assertEqual(r['accepted_cycles_observed'], 1)
                self.assertFalse(r['observer_requested_stop'])

    def test_default_window_adapts_to_saved_interval_without_fixed_addresses(self):
        self.source.value['interval_seconds'] = 300
        self.source.sample_fn = lambda: sample(10 + int(self.clock // 300))
        r = self.invoke(duration=None)
        self.assertEqual(r['requested_duration_seconds'], 932); self.assertEqual(r['result'], 'passed', r)

    def test_initial_sample_latency_is_excluded_from_observation_window(self):
        calls = 0
        def get():
            nonlocal calls
            if calls == 0: self.clock += 4
            calls += 1
            return sample(10 + int((self.clock - 4) // 3))
        self.source.sample_fn = get
        r = self.invoke(); self.assertEqual(r['result'], 'passed'); self.assertEqual(r['observed_seconds'], 12)


class AdapterTests(unittest.TestCase):
    def test_readonly_backend_rejects_all_control_before_runner(self):
        calls = []
        reader = probe.Reader(env={'PATH': os.defpath}, runner=lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout='', stderr=''))
        reader.paths['systemctl'] = '/usr/bin/systemctl'
        backend = watch.ReadOnlySystemd(reader, {'status': 'ok', 'environment': {}}, 'agent-project-ops-refresh-'+'a'*32+'.service')
        for method in (backend.start, backend.stop, backend.reload, backend.reset_failed):
            with self.assertRaisesRegex(ReadFailure, 'OBSERVER_SERVICE_CONTROL_REFUSED'): method()
        self.assertEqual(calls, []); self.assertEqual(backend.control_commands, 0)

    def test_help_works_with_isolated_python_without_bytecode_or_optional_dependencies(self):
        script = Path(watch.__file__)
        r = subprocess.run([sys.executable, '-I', '-B', str(script), '--help'], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn('--duration', r.stdout)


class RealProcessTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.child = None; self.addCleanup(self.stop_child)
    def stop_child(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try: self.child.wait(timeout=5)
            except subprocess.TimeoutExpired: self.child.kill(); self.child.wait(timeout=5)
    def launch(self):
        manager = self.f.manager; manager.invocation = uuid.uuid4().hex; manager.starts += 1
        program = '''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from src.ops.platform.ops import refresh_worker as w, service_lifecycle as s
d=Path(sys.argv[2]);m=s.load(d)
r=w.run(d,interval=30,deadline_seconds=3,snapshot_fn=lambda _:m['binding'],
 evaluate_fn=lambda *_:{'result':'passed','classification':'accepted','generation':None,
                       'configuration_changed':False,'restart_requested':False},
 _wait=lambda _:time.sleep(.15),service_invocation=sys.argv[3])
sys.exit(r['exit_code'])
'''
        self.child = subprocess.Popen([sys.executable, '-I', '-B', '-c', program, str(worker.ROOT),
            str(self.f.directory), manager.invocation], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        until = time.monotonic()+5
        while time.monotonic() < until:
            row = ws.Store(self.f.directory).optional('status.json') or {}
            if row.get('service_invocation') == manager.invocation:
                manager.pid = str(row['identity']['proc_pid']); manager.active = True; return
            time.sleep(.01)
        self.fail('private child did not start')

    def test_actual_worker_multiple_cycles_and_readonly_source_leave_child_running(self):
        self.f.manager.start = self.launch; self.f.good('start')
        report = {'result': 'passed', 'environment_ready': True, 'blockers': [],
                  'management': {'status': 'owned', 'unit_name': self.f.manager.unit}, 'systemd': {'status': 'ok'}}
        unchanged = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.f.manager.path, self.f.directory/'owner.json',
                     service.manifest_path(self.f.directory), ws.location(self.f.directory)/'control.json')}
        with patch.object(watch.auto_refresh, 'run', return_value=report), \
             patch.object(watch, 'ReadOnlySystemd', return_value=self.f.manager), \
             patch.object(worker, 'runtime_snapshot', return_value=self.f.binding), \
             patch.object(ws, 'write', side_effect=AssertionError('observer must not write')), \
             patch.object(ws, 'file_lock', side_effect=AssertionError('observer must not create/acquire lease')), \
             patch.object(service, 'run', side_effect=AssertionError('observer must not control service')):
            r = watch.run(self.f.directory, duration=1, minimum_cycles=3, poll_seconds=1,
                          _sleep=lambda _: time.sleep(.01))
        self.assertEqual(r['result'], 'passed', r); self.assertGreaterEqual(r['accepted_cycles_observed'], 3)
        self.assertTrue(r['same_service_process_at_samples']); self.assertIsNone(self.child.poll())
        self.assertEqual(self.f.manager.starts, 1)
        self.assertEqual(unchanged, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in unchanged})


if __name__ == '__main__': unittest.main()
