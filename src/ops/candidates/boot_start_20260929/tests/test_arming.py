"""Temporary transitions and real boot-entry protocol; no native manager/API."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import signal
import sys
import time
import unittest
from unittest.mock import patch
import uuid

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import worker_state as ws, refresh_worker as worker, service_lifecycle as service
from src.ops.platform.ops import boot_lifecycle as boot

ROOT = Path(__file__).resolve().parents[5]
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value
fixture = module('transition_fixture', ROOT / 'src/ops/candidates/boot_transition_20260929/tests/test_transition.py')
fixture.SOURCE = ROOT / 'baseline'
arming = module('initial_boot_candidate', ROOT / 'src/ops/candidates/boot_start_20260929/arming.py')


class Interrupted(Exception): pass


class StartTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.TransitionTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.tx, self.manager, self.directory = self.f.transition, self.f.manager, self.f.directory
        self.tx.apply(self.tx.plan())
        self.store = ws.Store(self.directory)
        self.environment_calls = 0; self.readbacks = 0
        self.manager.invocation = '1' * 32
        self.now = 0.; self.tick = lambda: None
        self.manager.start = self.start_healthy
        self.adapter = self.make()

    def environment(self, *_): self.environment_calls += 1
    def readback(self, directory, manifest, guard):
        self.readbacks += 1; guard()
        self.assertEqual(self.store.control()['desired'], 'paused')
        self.assertFalse(service.enabled(manifest))
        return {'result': 'passed', 'live_checks_performed': True,
                'configuration_written': False, 'restart_requested': False}
    def sleep(self, seconds): self.now += seconds; self.tick()
    def make(self, **kwargs):
        options = dict(environment_fn=self.environment, readback_fn=self.readback,
                       clock=lambda: self.now, sleep=self.sleep)
        options.update(kwargs); return arming.Start(self.tx, **options)
    def enter(self, invocation):
        identity = worker.process_identity()
        with ws.automatic_lease(self.directory):
            context = boot.Context(self.directory, service.load(self.directory),
                manager_check=lambda *a: None, pending_check=lambda _: None,
                snapshot_fn=lambda _, binding, budget: binding)
            context.enter(self.store, identity, uuid.uuid4().hex, invocation, lambda: False)
        return context.entry
    def waiting(self, entry, classification='accepted'):
        self.store.status(phase='waiting', identity=entry['identity'], invocation=entry['invocation'],
            service_invocation=entry['service_invocation'], completed_cycles=1,
            outcome={'result': 'passed', 'classification': classification,
                     'configuration_changed': False, 'restart_requested': False,
                     'child_process_group_stopped': True})
    def start_healthy(self):
        self.manager.control_commands += 1; self.manager.starts += 1
        self.manager.active = True; self.manager.invocation = uuid.uuid4().hex
        self.manager.pid = str(worker.process_identity()['proc_pid'])
        self.waiting(self.enter(self.manager.invocation))
    def code(self, expected, function):
        with self.assertRaises(ReadFailure) as caught: function()
        self.assertEqual(caught.exception.code, expected)
    def good(self):
        r = self.adapter.apply(self.adapter.plan(3))
        self.assertEqual(r['result'], 'passed', r); return r

    def test_plan_reads_metadata_without_writes_or_api(self):
        paths = [p for p in self.f.folder.rglob('*') if p.is_file()]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        p = self.adapter.plan()
        self.assertFalse(p['production_cli_available']); self.assertEqual(p['wait_seconds'], 570)
        self.assertEqual(self.readbacks, 0); self.assertEqual(self.manager.starts, 0)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})
        self.assertFalse(self.adapter.path.exists())

    def test_committed_transition_arms_once_and_requires_new_entry_and_evaluation(self):
        r = self.good()
        self.assertTrue(r['fresh_evaluation_verified']); self.assertFalse(r['actual_reboot_verified'])
        self.assertEqual(self.manager.starts, 1); self.assertEqual(self.readbacks, 1)
        self.assertEqual(self.store.optional('boot.json')['phase'], 'entered')
        self.assertEqual(self.adapter.load()['phase'], 'accepted')
        self.assertTrue(service.enabled(service.load(self.directory)))
        self.assertEqual(self.store.control()['desired'], 'running')
        self.assertEqual(self.f.sentinels, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.sentinels})

    def test_duplicate_apply_never_requests_start_again(self):
        proposal = self.adapter.plan(3); self.adapter.apply(proposal)
        self.code('BOOT_START_ALREADY_RECORDED', lambda: self.adapter.apply(proposal))
        self.assertEqual(self.manager.starts, 1)

    def test_ordinary_resume_cannot_bypass_explicit_adapter(self):
        r = service.run('resume', self.directory, backend=self.manager, reader=self.f.reader,
            preflight_fn=lambda *a, **k: {'blockers': [], 'persistence': {'selected': 'boot', 'linger': True}})
        self.assertEqual(r['code'], 'BOOT_EXPLICIT_START_ADAPTER_REQUIRED')
        self.assertEqual(self.manager.starts, 0)

    def test_partial_or_rolled_back_transition_is_rejected(self):
        value = self.tx.load()
        for phase in ('writing', 'rolled_back'):
            value['phase'] = phase; ws.write(self.tx.path, value)
            self.code('BOOT_COMMITTED_TRANSITION_REQUIRED', self.adapter.plan)
        self.assertEqual(self.manager.starts, 0)

    def test_session_policy_is_not_changed_by_start(self):
        self.tx.rollback(); self.code('BOOT_COMMITTED_TRANSITION_REQUIRED', self.adapter.plan)
        self.assertEqual(service.load(self.directory)['persistence'], 'session')

    def test_linger_or_manager_failure_is_not_repaired(self):
        def unavailable(*_): raise ReadFailure('BOOT_MANAGER_REQUIRED')
        self.code('BOOT_MANAGER_REQUIRED', self.make(environment_fn=unavailable).plan)
        self.assertFalse(self.adapter.path.exists()); self.assertEqual(self.manager.starts, 0)

    def test_changed_proposal_or_stop_token_prevents_side_effects(self):
        p = self.adapter.plan(3); p['manifest']['binding']['python'] = '/changed'
        self.code('BOOT_START_PROPOSAL_CHANGED', lambda: self.adapter.apply(p))
        p = self.adapter.plan(3); service.request_pause(self.directory)
        self.code('BOOT_START_PROPOSAL_CHANGED', lambda: self.adapter.apply(p))
        self.assertFalse(self.adapter.path.exists()); self.assertEqual(self.readbacks, 0)

    def test_runtime_binding_and_code_changes_are_never_rebound(self):
        self.f.binding['daemon_id'] = 'other'
        self.code('TRANSITION_RUNTIME_IDENTITY_CHANGED', self.adapter.plan)
        self.assertFalse(self.adapter.path.exists())

    def test_open_intent_pending_or_recovery_refused(self):
        ws.write(self.store.path / 'intent.json', {'phase': 'writing'})
        self.code('WORKER_UNFINISHED_INTENT', self.adapter.plan)
        (self.store.path / 'intent.json').unlink()
        self.f.provenance['pending'] = 'fixture'
        self.code('REFRESH_PENDING_USE_CHECK_OR_ROLLBACK', self.adapter.plan)
        self.f.provenance['pending'] = None; self.store.pause('EXISTING_RECOVERY')
        self.code('BOOT_CONTROL_CHANGED', self.adapter.plan)
        self.assertEqual(self.store.control()['recovery'], 'EXISTING_RECOVERY')

    def test_foreign_enable_link_and_dropin_are_preserved(self):
        m = service.load(self.directory); link = service.enabled_path(m)
        link.parent.mkdir(mode=0o700); link.symlink_to('/foreign/untouched')
        self.code('TRANSITION_UNIT_CHANGED', self.adapter.plan); self.assertTrue(link.is_symlink())
        link.unlink(); self.manager.dropins = '/foreign/dropin'
        self.code('TRANSITION_UNIT_CHANGED', self.adapter.plan)

    def test_live_readback_failure_keeps_pause_without_start_or_enable(self):
        def fail(*_): raise ReadFailure('OPS_ALERT_READBACK_FAILED')
        obj = self.make(readback_fn=fail); p = obj.plan(3)
        self.code('OPS_ALERT_READBACK_FAILED', lambda: obj.apply(p))
        self.assertEqual(self.store.control()['desired'], 'paused'); self.assertFalse(obj.path.exists())
        self.assertFalse(service.enabled(service.load(self.directory))); self.assertEqual(self.manager.starts, 0)

    def test_changed_saved_status_during_readback_is_not_overwritten(self):
        def edited(d, m, guard):
            result = self.readback(d, m, guard)
            self.store.status(phase='manual-observation', completed_cycles=42)
            return result
        obj = self.make(readback_fn=edited); p = obj.plan(3)
        self.code('BOOT_START_PRIOR_RECORD_CHANGED', lambda: obj.apply(p))
        self.assertEqual(self.store.optional('status.json')['phase'], 'manual-observation')
        self.assertFalse(obj.path.exists()); self.assertEqual(self.manager.starts, 0)

    def test_stop_during_readback_wins_before_arming(self):
        def stopped(d, m, guard): service.request_pause(d); guard()
        obj = self.make(readback_fn=stopped); p = obj.plan(3)
        self.code('BOOT_CONTROL_CHANGED', lambda: obj.apply(p))
        self.assertFalse(obj.path.exists()); self.assertEqual(self.manager.starts, 0)

    def test_each_prestart_interruption_is_durable_and_not_replayed(self):
        for phase in ('journal:prepared', 'journal:armed', 'enable_link', 'journal:start_requested', 'running_released'):
            with self.subTest(phase=phase):
                # A new fixture per crash boundary, not reuse of mutated state.
                case = StartTests(methodName='runTest'); case.setUp()
                try:
                    def crash(at):
                        if at == phase: raise Interrupted(at)
                    obj = case.make(hook=crash); p = obj.plan(3)
                    with case.assertRaises(Interrupted): obj.apply(p)
                    case.code('BOOT_START_ALREADY_RECORDED', lambda: case.make().apply(p))
                    case.assertEqual(case.manager.starts, 0)
                    case.assertEqual(case.make().cancel()['result'], 'passed')
                    case.assertFalse(service.enabled(service.load(case.directory)))
                finally: case.doCleanups()

    def test_stop_after_running_release_prevents_submission(self):
        def stop(at):
            if at == 'running_released': service.request_pause(self.directory)
        obj = self.make(hook=stop); r = obj.apply(obj.plan(3))
        self.assertEqual(r['code'], 'BOOT_CONTROL_CHANGED'); self.assertEqual(self.manager.starts, 0)
        self.assertEqual(self.store.control()['desired'], 'paused')

    def test_unknown_start_response_latches_without_repeating(self):
        def unknown(): self.manager.starts += 1; raise ReadFailure('SERVICE_COMMAND_OUTCOME_UNKNOWN')
        self.manager.start = unknown; p = self.adapter.plan(3); r = self.adapter.apply(p)
        self.assertEqual(r['code'], 'SERVICE_COMMAND_OUTCOME_UNKNOWN'); self.assertEqual(self.manager.starts, 1)
        self.code('BOOT_START_ALREADY_RECORDED', lambda: self.make().apply(p))
        self.assertFalse(service.enabled(service.load(self.directory))); self.assertEqual(self.store.control()['desired'], 'paused')

    def test_lost_submission_record_can_observe_without_second_start(self):
        def crash(at):
            if at == 'start_returned': raise Interrupted(at)
        obj = self.make(hook=crash)
        with self.assertRaises(Interrupted): obj.apply(obj.plan(3))
        self.assertEqual(self.adapter.load()['phase'], 'start_requested')
        self.assertEqual(self.adapter.check()['result'], 'passed'); self.assertEqual(self.manager.starts, 1)

    def test_old_invocation_success_is_refused(self):
        self.manager.invocation = '2' * 32
        def reused():
            self.start_healthy(); self.manager.invocation = '2' * 32
        self.manager.start = reused
        r = self.adapter.apply(self.adapter.plan(3))
        self.assertEqual(r['code'], 'BOOT_MANAGER_INVOCATION_REUSED')

    def test_stale_pid_and_incomplete_evidence_never_pass(self):
        def stale(): self.start_healthy(); self.manager.pid = '99999999'
        self.manager.start = stale
        r = self.adapter.apply(self.adapter.plan(2))
        self.assertEqual(r['code'], 'SERVICE_EVALUATION_WAIT_TIMEOUT'); self.assertFalse(r['fresh_evaluation_verified'])

    def test_missing_or_changed_entry_receipt_refuses_healthy_sample(self):
        def changed():
            self.start_healthy(); value = self.store.optional('boot.json'); value['previous_receipt_sha256'] = 'wrong'
            ws.write(self.store.path / 'boot.json', value)
        self.manager.start = changed
        r = self.adapter.apply(self.adapter.plan(3)); self.assertEqual(r['code'], 'BOOT_ENTRY_EVIDENCE_INVALID')

    def test_retryable_evaluation_can_recover_in_same_invocation(self):
        def degraded(): self.start_healthy(); self.waiting(self.store.optional('boot.json'), 'retryable_prewrite')
        self.manager.start = degraded
        self.tick = lambda: self.waiting(self.store.optional('boot.json'))
        self.assertEqual(self.adapter.apply(self.adapter.plan(3))['result'], 'passed'); self.assertEqual(self.manager.starts, 1)

    def test_stop_during_observation_is_preserved(self):
        def waiting(): self.start_healthy(); self.waiting(self.store.optional('boot.json'), 'retryable_prewrite')
        self.manager.start = waiting; self.tick = lambda: service.request_pause(self.directory)
        r = self.adapter.apply(self.adapter.plan(3)); self.assertEqual(r['code'], 'SERVICE_STOP_REQUEST_CHANGED')
        self.assertEqual(self.store.control()['desired'], 'paused')

    def test_cancel_requires_exit_and_keeps_failure_evidence(self):
        self.good(); self.store.pause('KEEP_THIS_REASON')
        self.manager.empty = False
        self.code('BOOT_CANCEL_EXIT_UNCONFIRMED', lambda: self.adapter.cancel(stop_fn=lambda: {'result': 'passed', 'worker_exit_verified': True}))
        self.manager.empty = True
        self.assertEqual(self.adapter.cancel()['result'], 'passed')
        self.assertEqual(self.store.control()['recovery'], 'KEEP_THIS_REASON')
        self.assertTrue(self.tx.path.exists()); self.assertTrue(self.adapter.path.exists())

    def test_lease_held_by_real_child_blocks_start(self):
        ready_r, ready_w = os.pipe(); release_r, release_w = os.pipe(); pid = os.fork()
        if pid == 0:
            os.close(ready_r); os.close(release_w)
            with ws.automatic_lease(self.directory): os.write(ready_w, b'1'); os.read(release_r, 1)
            os._exit(0)
        os.close(ready_w); os.close(release_r)
        try:
            self.assertEqual(os.read(ready_r, 1), b'1'); p = self.adapter.plan(3)
            self.code('AUTOMATIC_WORKER_BUSY', lambda: self.adapter.apply(p))
        finally:
            os.write(release_w, b'1'); os.close(release_w); os.close(ready_r); os.waitpid(pid, 0)
        self.assertFalse(self.adapter.path.exists()); self.assertEqual(self.manager.starts, 0)

    def test_real_process_death_after_release_cannot_replay_start(self):
        p = self.adapter.plan(3); pid = os.fork()
        if pid == 0:
            def crash(at):
                if at == 'running_released': os._exit(23)
            try: self.make(hook=crash).apply(p)
            except BaseException: os._exit(24)
            os._exit(25)
        _, status = os.waitpid(pid, 0); self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        self.code('BOOT_START_ALREADY_RECORDED', lambda: self.make().apply(p))
        self.assertEqual(self.make().cancel()['result'], 'passed'); self.assertEqual(self.manager.starts, 0)

    def test_old_loaded_function_is_detected_even_when_disk_is_candidate(self):
        def obsolete(*a, **k): pass
        with patch.object(worker, 'run', obsolete):
            self.code('BOOT_FRESH_INTERPRETER_REQUIRED', self.adapter.plan)

    def test_boot_change_before_release_refuses_arming(self):
        old = self.adapter.plan(3)
        obj = self.make(identity_fn=lambda: {'boot_id': 'different'})
        self.code('BOOT_START_PROPOSAL_CHANGED', lambda: obj.apply(old)); self.assertEqual(self.manager.starts, 0)

    def test_final_stop_after_binding_check_prevents_success_record(self):
        original = self.tx.snapshot
        def snapshot(directory):
            value = original(directory)
            if self.store.optional('boot.json') and self.store.optional('boot.json')['phase'] == 'entered':
                service.request_pause(directory)
            return value
        self.tx.snapshot = snapshot
        r = self.adapter.apply(self.adapter.plan(3))
        self.assertEqual(r['result'], 'attention_required')
        self.assertEqual(self.adapter.load()['phase'], 'attention_required')
        self.assertEqual(self.store.control()['desired'], 'paused')

    def test_real_worker_consumes_receipt_evaluates_and_stops_cleanly(self):
        child = [None]
        def start():
            self.manager.starts += 1; self.manager.active = True; self.manager.invocation = uuid.uuid4().hex
            pid = os.fork()
            if pid == 0:
                try:
                    # The real manager starts a separate process without the
                    # caller's lock fds. Reproduce that boundary in this fork.
                    for descriptor in list(Path('/proc/self/fd').iterdir()):
                        try:
                            if int(descriptor.name) > 2: os.close(int(descriptor.name))
                        except OSError: pass
                    ctx = boot.Context(self.directory, service.load(self.directory), manager_check=lambda *a: None,
                        pending_check=lambda _: None, snapshot_fn=lambda _, binding, budget: binding)
                    result = worker.run(self.directory, interval=30, deadline_seconds=3,
                        snapshot_fn=self.f.snapshot, service_invocation=self.manager.invocation, boot_context=ctx,
                        evaluate_fn=lambda *a: {'result': 'passed', 'classification': 'accepted',
                                               'configuration_changed': False, 'restart_requested': False})
                    os._exit(result['exit_code'])
                except BaseException: os._exit(27)
            child[0] = pid
        old_inspect = self.manager.inspect
        def inspect():
            latest = self.store.optional('status.json') or {}
            if child[0] and latest.get('service_invocation') == self.manager.invocation:
                self.manager.pid = str(latest['identity']['proc_pid'])
            return old_inspect()
        def stop():
            self.manager.stops += 1
            if child[0]:
                _, status = os.waitpid(child[0], 0); child[0] = None
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.manager.active = False; self.manager.pid = '0'
        self.manager.start = start; self.manager.inspect = inspect; self.manager.stop = stop
        obj = self.make(clock=time.monotonic, sleep=time.sleep)
        try:
            r = obj.apply(obj.plan(8)); self.assertEqual(r['result'], 'passed', r)
            self.assertTrue(r['fresh_evaluation_verified']); self.assertEqual(self.manager.starts, 1)
            self.assertEqual(obj.cancel()['result'], 'passed')
            self.assertEqual(self.store.optional('status.json')['phase'], 'stopped')
        finally:
            if child[0]:
                os.kill(child[0], signal.SIGKILL); os.waitpid(child[0], 0)


class ReadbackTests(unittest.TestCase):
    def run_readback(self, fail=False):
        activation = worker.activation; calls = []
        current = {'selected': 'current'}; proposed = {'selected': 'proposal'}
        class API:
            def __init__(self, cfg):
                self_cfg = cfg
                if self_cfg != current: raise AssertionError('proposal used for readback')
            def health(self): calls.append('health')
            def login(self): calls.append('login'); return 'synthetic-cookie'
            def logout(self, cookie):
                if cookie != 'synthetic-cookie': raise AssertionError('wrong cookie')
                calls.append('logout')
        def capture(*args, **kwargs):
            self.assertEqual(kwargs['planner'](current, {}), proposed)
            calls.append('capture'); return {'baseline': {'id': 'original'}}
        def readback(cfg):
            self.assertEqual(cfg, current); calls.append('readback')
            if fail: raise ReadFailure('OPS_ALERT_READBACK_FAILED')
        with patch.object(worker.refresh, 'lineage', lambda _: {'pending': None, 'next': 1, 'fields': {}, 'evidence': {}}), \
             patch.object(worker.refresh, 'merge', lambda *a: proposed), \
             patch.object(activation, 'capture', capture), \
             patch.object(worker.manage, 'existing', lambda _: current), \
             patch.object(activation, 'guard', lambda d, baseline, side: calls.append('guard:' + side)), \
             patch.object(arming.change, 'protect', lambda *a: calls.append('protected')):
            try:
                answer = arming.live_readback(Path('/synthetic'), {}, lambda: None,
                    {'docker': object(), 'api_factory': API, 'readback_fn': readback})
            except ReadFailure:
                self.assertTrue(fail); answer = None
        return answer, calls

    def test_current_settings_are_checked_without_activating_discovery_proposal(self):
        result, calls = self.run_readback()
        self.assertTrue(result['live_checks_performed']); self.assertFalse(result['configuration_written'])
        self.assertEqual(calls, ['capture', 'health', 'login', 'readback', 'logout', 'guard:original', 'protected'])

    def test_failed_readback_logs_out_and_does_not_report_live_success(self):
        result, calls = self.run_readback(True)
        self.assertIsNone(result); self.assertEqual(calls, ['capture', 'health', 'login', 'readback', 'logout'])


if __name__ == '__main__': unittest.main()
