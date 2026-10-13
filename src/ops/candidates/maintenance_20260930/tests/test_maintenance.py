"""Private files, synthetic manager/inventory and actual leases/child processes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import boot_lifecycle as boot, refresh_worker as worker
from src.ops.platform.ops import service_lifecycle as service, worker_state as ws
from src.ops.platform.tests import test_service_lifecycle as fixture
from src.ops.candidates.maintenance_20260930 import maintenance as core

PASSED = {'result': 'passed', 'child_processes_stopped': True}
READBACK = {'result': 'passed', 'live_checks_performed': True, 'configuration_written': False, 'restart_requested': False}


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the fixture setup, not its inherited test case set.
        self.fixture = fixture.ServiceTests('test_install_inactive_private_idempotent_without_keys')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        f = self.fixture; self.directory, self.manager = f.directory, f.manager
        f.binding['code_sha256'] = worker.code_hash(worker.ROOT)
        f.good('install'); self.store = ws.Store(self.directory)
        self.manifest = service.load(self.directory); self.manifest.update(persistence='boot',
            boot_contract={'schema': 1, 'generation': str(uuid.uuid4()), 'startup_seconds': 2})
        ws.write(service.manifest_path(self.directory), self.manifest)
        self.manager.path.write_text(service.render(self.manifest)); self.manager.path.chmod(0o600)
        service.set_enabled(self.manifest, True)
        self.manager.active = True; self.manager.pid = str(worker.process_identity()['proc_pid']); self.manager.invocation = uuid.uuid4().hex
        control = self.store.control(); control.update(desired='running', recovery=None, service_stop_token=uuid.uuid4().hex)
        ws.write(self.store.path / 'control.json', control)
        self.identity = worker.process_identity(); self.invocation = str(uuid.uuid4())
        self.accepted(self.identity, self.invocation)
        receipt = {'schema': 1, 'phase': 'entered', 'generation': boot.policy(self.manifest)['generation'],
                   'manifest_sha256': boot.fingerprint(self.manifest), 'identity': self.identity,
                   'invocation': self.invocation, 'service_invocation': self.manager.invocation,
                   'previous_receipt_sha256': 'f'*64}
        ws.write(self.store.path / 'boot.json', receipt)
        for name, kind, phase in (('boot-start.json', 'initial-boot-start', 'accepted'),
                                  ('code-transition.json', 'stopped-code-policy-transition', 'committed')):
            ws.write(self.store.path.parent / name, {'schema': 1, 'kind': kind, 'phase': phase,
                     'directory': str(self.directory), 'id': str(uuid.uuid4())})
        self.fixed_before = self.fixed_bytes()
        self.inventory = {'scope': {'ops': {'container_id': f.binding['ops_container_id'],
            'image_id': f.binding['ops_image_id'], 'mounts_sha256': 'd'*64, 'owner': f.owner['owner']}},
            'protected': {'e'*64: 'synthetic-start-1'}}
        self.history = 'f'*64; self.readbacks = 0; self.operation_calls = 0
        self.controller = core.Maintenance(self.directory, self.manager,
            inventory_fn=lambda: deepcopy(self.inventory), snapshot_fn=lambda _: deepcopy(f.binding),
            history_fn=lambda _: self.history, environment_fn=lambda *a: None, readback_fn=self.readback,
            clock=time.monotonic, sleep=lambda _: None)
        self.manager.start = self.start
        self.plan = self.controller.plan(wait_seconds=1)

    def fixed_bytes(self):
        names = ('boot-start.json', 'code-transition.json', 'service.json')
        return {n: ((self.store.path.parent / n).read_bytes(), (self.store.path.parent / n).stat().st_mode & 0o777) for n in names}

    def accepted(self, identity, invocation, count=1):
        self.store.status(phase='waiting', identity=identity, invocation=invocation,
            service_invocation=self.manager.invocation, completed_cycles=count,
            outcome={'result': 'passed', 'classification': 'accepted', 'configuration_changed': False,
                     'restart_requested': False, 'child_process_group_stopped': True})

    def start(self):
        self.manager.control_commands += 1; self.manager.starts += 1
        self.manager.active = True; self.manager.pid = str(worker.process_identity()['proc_pid']); self.manager.invocation = uuid.uuid4().hex
        invocation = str(uuid.uuid4()); identity = worker.process_identity()
        context = boot.Context(self.directory, self.manifest, policy_check=lambda *a: None,
            manager_check=lambda *a: None, pending_check=lambda *a: None,
            snapshot_fn=lambda *a: deepcopy(self.fixture.binding))
        context.enter(self.store, identity, invocation, self.manager.invocation, lambda: False)
        self.accepted(identity, invocation)

    def readback(self, directory, manifest, guard):
        self.readbacks += 1; guard(); return deepcopy(READBACK)

    def operation(self, scope):
        self.operation_calls += 1; scope.checkpoint(); return deepcopy(PASSED)

    def stop_job(self):
        result = self.controller.run(self.plan, self.operation)
        self.assertEqual(result['result'], 'passed', str(result))
        self.assertEqual((self.manager.stops, self.manager.starts), (1, 0))
        return result['job_id']

    def assert_paused(self):
        self.assertEqual(self.store.control()['desired'], 'paused')
        self.assertFalse(service.enabled(self.manifest))

    def test_plan_readonly_healthy_boot_history_and_new_scope(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.fixture.root.rglob('*') if p.is_file()}
        self.controller.plan()
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        self.assertFalse(self.controller.jobs.exists()); self.assertEqual(self.manager.control_commands, 1)

    def test_scoped_stop_and_explicit_return_preserve_policy_code_and_initial_records(self):
        job = self.stop_job(); self.assert_paused()
        self.assertEqual(self.controller.load(job)['prior_receipt'], self.plan['prior_receipt'])
        result = self.controller.resume(job)
        self.assertEqual(result['result'], 'passed', str(result)); self.assertTrue(result['fresh_evaluation_verified'])
        self.assertEqual((self.manager.stops, self.manager.starts), (1, 1))
        self.assertEqual(self.fixed_before, self.fixed_bytes()); self.assertTrue(service.enabled(self.manifest))
        self.assertEqual(self.controller.load(job)['phase'], 'returned')
        self.assertNotEqual(self.manager.invocation, self.plan['prior_service_invocation'])

    def test_manual_and_automatic_refresh_lease_blocked_through_operation_and_readback(self):
        def blocked(scope):
            with self.assertRaisesRegex(ReadFailure, 'AUTOMATIC_WORKER_BUSY'):
                with ws.automatic_lease(self.directory): pass
            scope.checkpoint(); return PASSED
        job = self.controller.run(self.plan, blocked)['job_id']
        self.controller.readback = lambda d, m, guard: (blocked(type('ReadScope', (), {'checkpoint': staticmethod(guard)})()), READBACK)[1]
        self.assertEqual(self.controller.resume(job)['result'], 'passed')

    def test_operation_failure_stays_paused_and_is_not_replayed(self):
        def fail(scope): raise ReadFailure('SYNTHETIC_OPERATION_FAILED')
        result = self.controller.run(self.plan, fail); self.assert_paused()
        self.assertEqual(result['code'], 'SYNTHETIC_OPERATION_FAILED')
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_UNRESOLVED_JOB'): self.controller.run(self.plan, self.operation)
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_RETURN_REFUSED'): self.controller.resume(result['job_id'])
        self.assertEqual(self.operation_calls, 0); self.assertEqual(self.manager.stops, 1)

    def test_child_exit_evidence_required_before_ready_to_return(self):
        result = self.controller.run(self.plan, lambda scope: {'result': 'passed', 'child_processes_stopped': False})
        self.assertEqual(result['code'], 'MAINTENANCE_OPERATION_EXIT_UNCONFIRMED'); self.assert_paused()

    def test_scope_capability_closed_after_callback(self):
        saved = []
        self.controller.run(self.plan, lambda scope: (saved.append(scope), PASSED)[1])
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_SCOPE_CLOSED'): saved[0].checkpoint()

    def test_newer_explicit_stop_during_operation_wins_return(self):
        def stopped(scope): service.request_pause(self.directory); return PASSED
        result = self.controller.run(self.plan, stopped)
        self.assertEqual(result['code'], 'MAINTENANCE_STOP_OR_RECOVERY_CHANGED'); self.assert_paused()
        self.assertEqual(self.manager.starts, 0)

    def test_newer_stop_during_readback_or_running_release_is_preserved(self):
        for boundary in ('readback', 'running_released'):
            with self.subTest(boundary=boundary):
                if boundary != 'readback': self.tearDown_fixture_and_setup()
                job = self.stop_job()
                if boundary == 'readback':
                    def fail(d, m, guard): service.request_pause(d); guard(); return READBACK
                    self.controller.readback = fail
                else:
                    self.controller.hook = lambda p: service.request_pause(self.directory) if p == boundary else None
                result = self.controller.resume(job); self.assertEqual(result['result'], 'attention_required')
                self.assertNotEqual(self.store.control()['service_stop_token'], self.controller.load(job)['stop_token'])
                self.assert_paused(); self.assertEqual(self.manager.starts, 0)

    def tearDown_fixture_and_setup(self):
        self.fixture.doCleanups(); self.setUp()

    def test_pending_intent_recovery_unhealthy_and_session_policy_prevent_stop(self):
        mutations = ('intent', 'recovery', 'unhealthy', 'policy')
        for index, kind in enumerate(mutations):
            if index: self.tearDown_fixture_and_setup()
            if kind == 'intent': ws.write(self.store.path / 'intent.json', {'schema': 1, 'phase': 'open'})
            if kind == 'recovery': self.store.pause('UNRELATED_RECOVERY')
            if kind == 'unhealthy': self.store.status(phase='evaluating', completed_cycles=1)
            if kind == 'policy':
                value = deepcopy(self.manifest); value['persistence'] = 'session'; ws.write(service.manifest_path(self.directory), value)
            with self.assertRaises(ReadFailure): self.controller.run(self.plan, self.operation)
            self.assertEqual(self.manager.stops, 0); self.assertEqual(self.operation_calls, 0)

    def test_changed_review_or_scope_never_pauses_or_requests_stop(self):
        changed = deepcopy(self.plan); changed['inventory']['scope']['ops']['container_id'] = 'f'*64
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_PROPOSAL_CHANGED'): self.controller.run(changed, self.operation)
        self.assertEqual(self.store.control()['desired'], 'running'); self.assertEqual(self.manager.stops, 0)

    def test_started_time_changed_or_new_protected_container_blocks_return(self):
        for index, kind in enumerate(('changed', 'added')):
            if index: self.tearDown_fixture_and_setup()
            job = self.stop_job()
            if kind == 'changed': self.inventory['protected']['e'*64] = 'synthetic-start-2'
            else: self.inventory['protected']['f'*64] = 'synthetic-start-3'
            result = self.controller.resume(job)
            self.assertEqual(result['code'], 'MAINTENANCE_CONTAINER_SCOPE_CHANGED'); self.assert_paused()
            self.assertEqual(self.readbacks, 0); self.assertEqual(self.manager.starts, 0)

    def test_scoped_image_mount_or_owner_change_is_not_adopted(self):
        for index, field in enumerate(('image_id', 'mounts_sha256', 'owner')):
            if index: self.tearDown_fixture_and_setup()
            job = self.stop_job(); row = self.inventory['scope']['ops']
            row[field] = str(uuid.uuid4()) if field == 'owner' else ('sha256:' if field == 'image_id' else '') + 'f'*64
            result = self.controller.resume(job); self.assertEqual(result['code'], 'MAINTENANCE_CONTAINER_SCOPE_CHANGED')
            self.assertEqual(self.manager.starts, 0); self.assert_paused()

    def test_scope_disjoint_and_hash_format_required_before_stop(self):
        self.inventory['protected'][self.fixture.binding['ops_container_id']] = 'synthetic-start'
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_SCOPE_INVALID'): self.controller.plan()
        self.inventory['protected'].pop(self.fixture.binding['ops_container_id'])
        self.inventory['scope']['ops']['image_id'] = 'sha256:' + 'z'*64
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_SCOPE_INVALID'): self.controller.plan()
        self.assertEqual(self.manager.stops, 0)

    def test_binding_history_initial_record_or_unit_change_refuses_return(self):
        for index, kind in enumerate(('binding', 'history', 'initial', 'unit')):
            if index: self.tearDown_fixture_and_setup()
            job = self.stop_job()
            if kind == 'binding': self.fixture.binding['daemon_id'] = 'different'
            if kind == 'history': self.history = 'a'*64
            if kind == 'initial': ws.write(self.store.path.parent / 'boot-start.json', {'schema': 1, 'phase': 'accepted'})
            if kind == 'unit': self.manager.path.write_text('manual unit edit')
            result = self.controller.resume(job); self.assertEqual(result['result'], 'attention_required')
            self.assertEqual(self.manager.starts, 0); self.assertEqual(self.store.control()['desired'], 'paused')

    def test_boot_change_or_linger_loss_is_not_repaired(self):
        job = self.stop_job()
        self.controller.identity = lambda: dict(self.identity, boot_id='changed')
        self.assertEqual(self.controller.resume(job)['code'], 'MAINTENANCE_BOOT_CHANGED'); self.assert_paused()
        self.tearDown_fixture_and_setup(); job = self.stop_job()
        self.controller.environment = lambda *a: (_ for _ in ()).throw(ReadFailure('BOOT_MANAGER_REQUIRED'))
        self.assertEqual(self.controller.resume(job)['code'], 'BOOT_MANAGER_REQUIRED'); self.assertEqual(self.manager.starts, 0)

    def test_unknown_stop_and_populated_cgroup_never_repeat_or_run_operation(self):
        def unknown(): self.manager.stops += 1; raise ReadFailure('SERVICE_COMMAND_OUTCOME_UNKNOWN')
        self.manager.stop = unknown
        result = self.controller.run(self.plan, self.operation)
        self.assertEqual(result['code'], 'SERVICE_COMMAND_OUTCOME_UNKNOWN'); self.assertEqual(self.operation_calls, 0)
        self.assert_paused()
        with self.assertRaises(ReadFailure): self.controller.run(self.plan, self.operation)
        self.assertEqual(self.manager.stops, 1)
        self.tearDown_fixture_and_setup(); self.manager.empty = False
        ticks = iter((0, 999)); self.controller.clock = lambda: next(ticks)
        result = self.controller.run(self.plan, self.operation)
        self.assertEqual(result['code'], 'MAINTENANCE_STOP_OUTCOME_UNKNOWN'); self.assertEqual(self.operation_calls, 0)

    def test_live_readback_failure_does_not_arm_enable_or_start(self):
        job = self.stop_job(); prior = self.store.optional('boot.json')
        self.controller.readback = lambda *a: (_ for _ in ()).throw(ReadFailure('SYNTHETIC_READBACK_FAILED'))
        self.assertEqual(self.controller.resume(job)['code'], 'SYNTHETIC_READBACK_FAILED')
        self.assertEqual(prior, self.store.optional('boot.json')); self.assertEqual(self.manager.starts, 0); self.assert_paused()

    def test_unknown_start_and_second_resume_never_resubmit(self):
        job = self.stop_job()
        def unknown(): self.manager.starts += 1; raise ReadFailure('SERVICE_COMMAND_OUTCOME_UNKNOWN')
        self.manager.start = unknown
        self.assertEqual(self.controller.resume(job)['code'], 'SERVICE_COMMAND_OUTCOME_UNKNOWN')
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_RETURN_REFUSED'): self.controller.resume(job)
        self.assertEqual(self.manager.starts, 1); self.assert_paused()

    def test_old_invocation_receipt_or_saved_pid_cannot_claim_new_evaluation(self):
        for index, kind in enumerate(('invocation', 'receipt', 'pid')):
            if index: self.tearDown_fixture_and_setup()
            job = self.stop_job(); original = self.start
            def stale():
                original()
                if kind == 'invocation': self.manager.invocation = self.plan['prior_service_invocation']
                if kind == 'receipt':
                    receipt = self.store.optional('boot.json'); receipt['previous_receipt_sha256'] = '0'*64
                    ws.write(self.store.path / 'boot.json', receipt)
                if kind == 'pid': self.manager.pid = '99999999'
            self.manager.start = stale
            result = self.controller.resume(job); self.assertEqual(result['result'], 'attention_required')
            self.assertEqual(self.controller.load(job)['phase'], 'attention_required'); self.assert_paused()

    def test_returned_job_cannot_replay_and_next_job_keeps_separate_history(self):
        job = self.stop_job(); self.assertEqual(self.controller.resume(job)['result'], 'passed')
        raw = self.controller.path(job).read_bytes()
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_RETURN_REFUSED'): self.controller.resume(job)
        with self.assertRaises(ReadFailure): self.controller.run(self.plan, self.operation)
        self.assertEqual(raw, self.controller.path(job).read_bytes())
        self.plan = self.controller.plan(wait_seconds=1)
        second = self.controller.run(self.plan, self.operation)
        self.assertNotEqual(job, second['job_id']); self.assertEqual(raw, self.controller.path(job).read_bytes())

    def test_reusing_completed_identifier_does_not_overwrite_audit(self):
        job = self.stop_job(); self.controller.resume(job); raw = self.controller.path(job).read_bytes()
        next_plan = self.controller.plan(wait_seconds=1); next_plan['id'] = job
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_ID_ALREADY_RECORDED'): self.controller.run(next_plan, self.operation)
        self.assertEqual(raw, self.controller.path(job).read_bytes()); self.assertEqual(self.manager.stops, 1)

    def test_record_symlink_hardlink_and_private_modes_are_refused(self):
        self.controller.jobs.mkdir(mode=0o700)
        victim = self.controller.jobs / (str(uuid.uuid4()) + '.json')
        victim.symlink_to(self.store.path / 'control.json')
        with self.assertRaises(ReadFailure): self.controller.plan()
        victim.unlink(); self.controller.jobs.chmod(0o770)
        with self.assertRaises(ReadFailure): self.controller.plan()
        self.assertEqual(self.controller.jobs.stat().st_mode & 0o777, 0o770)
        self.controller.jobs.chmod(0o700)
        initial = self.store.path.parent / 'boot-start.json'; link = self.fixture.root / 'linked'; os.link(initial, link)
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_HARDLINK_REFUSED'): self.controller.plan()
        self.assertEqual(self.manager.stops, 0)

    def test_status_reads_without_creating_or_trusting_saved_pid(self):
        job = self.stop_job(); before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.fixture.root.rglob('*') if p.is_file()}
        result = self.controller.inspect(job); self.assertEqual(result['phase'], 'ready_to_return')
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}); self.assertEqual(self.manager.starts, 0)

    def test_stop_before_final_success_prevents_returned_record(self):
        job = self.stop_job(); original = self.controller.snapshot; calls = 0
        def snapshot(directory):
            nonlocal calls
            calls += 1
            if self.manager.starts: service.request_pause(directory)
            return original(directory)
        self.controller.snapshot = snapshot
        result = self.controller.resume(job); self.assertEqual(result['result'], 'attention_required')
        self.assertEqual(self.controller.load(job)['phase'], 'attention_required'); self.assert_paused()

    def test_durable_journal_before_pause_and_commands_and_partial_boundaries_block_reentry(self):
        boundaries = ('journal:prepared', 'pause_written', 'journal:stop_requested', 'journal:stopped', 'journal:operation_running')
        for index, boundary in enumerate(boundaries):
            if index: self.tearDown_fixture_and_setup()
            self.controller.hook = lambda p: (_ for _ in ()).throw(ReadFailure('SYNTHETIC_INTERRUPTED')) if p == boundary else None
            result = self.controller.run(self.plan, self.operation)
            self.assertEqual(result['result'], 'attention_required'); self.assert_paused()
            self.assertEqual(self.operation_calls, 0)
            with self.assertRaises(ReadFailure): self.controller.run(self.plan, self.operation)

    def test_initial_journal_write_failure_does_not_pause_or_control_manager(self):
        original = ws.write
        def fail(path, value):
            if path.parent == self.controller.jobs: raise OSError('synthetic unavailable storage')
            return original(path, value)
        with patch.object(ws, 'write', side_effect=fail):
            with self.assertRaises(OSError): self.controller.run(self.plan, self.operation)
        self.assertEqual(self.store.control()['desired'], 'running')
        self.assertEqual(self.manager.stops, 0); self.assertEqual(self.manager.starts, 0)

    def test_journal_write_failure_after_start_still_pauses_and_never_replays(self):
        job = self.stop_job(); original = ws.write; failed = False
        def fail(path, value):
            nonlocal failed
            if path == self.controller.path(job) and value.get('phase') == 'submitted' and not failed:
                failed = True; raise OSError('synthetic journal write failure')
            return original(path, value)
        with patch.object(ws, 'write', side_effect=fail):
            result = self.controller.resume(job)
        self.assertEqual(result['result'], 'attention_required'); self.assert_paused()
        self.assertEqual(self.manager.starts, 1)
        with self.assertRaises(ReadFailure): self.controller.resume(job)
        self.assertEqual(self.manager.starts, 1)

    def test_manual_journal_change_not_overwritten_and_return_permission_is_revoked(self):
        job = self.stop_job()
        def changed(d, m, guard):
            record = self.controller.load(job); record['manual_marker'] = 'keep'
            ws.write(self.controller.path(job), record); guard(); return READBACK
        self.controller.readback = changed
        result = self.controller.resume(job)
        self.assertEqual(result['code'], 'MAINTENANCE_RECORD_CHANGED')
        self.assertEqual(self.controller.load(job)['manual_marker'], 'keep'); self.assert_paused()
        self.assertEqual(self.manager.starts, 0)

    def test_binding_change_after_pause_prevents_stop_submission(self):
        def hook(phase):
            if phase == 'journal:stop_requested': self.fixture.binding['daemon_id'] = 'changed'
        self.controller.hook = hook
        result = self.controller.run(self.plan, self.operation)
        self.assertEqual(result['code'], 'MAINTENANCE_BINDING_CHANGED'); self.assert_paused()
        self.assertEqual(self.manager.stops, 0); self.assertEqual(self.operation_calls, 0)

    def test_arming_write_boundaries_never_automatically_resubmit(self):
        boundaries = ('journal:armed', 'enable_link', 'journal:start_requested', 'running_released', 'start_returned', 'journal:submitted')
        for index, boundary in enumerate(boundaries):
            if index: self.tearDown_fixture_and_setup()
            job = self.stop_job()
            self.controller.hook = lambda p: (_ for _ in ()).throw(ReadFailure('SYNTHETIC_INTERRUPTED')) if p == boundary else None
            result = self.controller.resume(job); self.assertEqual(result['result'], 'attention_required')
            self.assert_paused(); starts = self.manager.starts
            with self.assertRaises(ReadFailure): self.controller.resume(job)
            self.assertEqual(self.manager.starts, starts)

    def test_real_child_inherits_lease_and_prevents_return_until_exit(self):
        pipe_r, pipe_w = os.pipe(); child = None
        def operation(scope):
            nonlocal child
            child = os.fork()
            if child == 0:
                self.close_management_lock()
                os.close(pipe_w); os.read(pipe_r, 1); os._exit(0)
            os.close(pipe_r); return PASSED
        result = self.controller.run(self.plan, operation); job = result['job_id']
        try:
            returned = self.controller.resume(job)
            self.assertEqual(returned['code'], 'AUTOMATIC_WORKER_BUSY'); self.assertEqual(self.manager.starts, 0)
        finally:
            os.write(pipe_w, b'x'); os.close(pipe_w); os.waitpid(child, 0)
        with ws.automatic_lease(self.directory): pass

    def close_management_lock(self):
        # Mirror subprocess pass_fds: the child retains the operation lease,
        # while unrelated controller descriptors are closed in a fresh exec.
        for path in Path('/proc/self/fd').iterdir():
            try:
                if os.readlink(path) == str(self.directory / 'service-management.lock'): os.close(int(path.name))
            except FileNotFoundError: pass

    def test_real_controller_death_during_operation_stays_paused_with_nonreplayable_job(self):
        pid = os.fork()
        if pid == 0:
            self.controller.run(self.plan, lambda scope: os._exit(23)); os._exit(99)
        _, status = os.waitpid(pid, 0); self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        record = self.controller.load(self.plan['id']); self.assertEqual(record['phase'], 'operation_running')
        self.assert_paused()
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_UNRESOLVED_JOB'): self.controller.plan()
        with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_RETURN_REFUSED'): self.controller.resume(record['id'])
        self.assertEqual(self.manager.starts, 0)

    def test_real_worker_consumes_return_receipt_then_new_evaluation_and_stop(self):
        job = self.stop_job(); child = None
        def start():
            nonlocal child
            self.manager.starts += 1; self.manager.invocation = uuid.uuid4().hex
            pipe_r, pipe_w = os.pipe()
            child = os.fork()
            if child == 0:
                self.close_management_lock()
                os.close(pipe_r); os.write(pipe_w, str(worker.process_identity()['proc_pid']).encode()); os.close(pipe_w)
                context = boot.Context(self.directory, self.manifest, policy_check=lambda *a: None,
                    manager_check=lambda *a: None, pending_check=lambda *a: None,
                    snapshot_fn=lambda *a: deepcopy(self.fixture.binding))
                try:
                    worker.run(self.directory, interval=30, deadline_seconds=2,
                        snapshot_fn=lambda _: deepcopy(self.fixture.binding),
                        evaluate_fn=lambda *a: {'result': 'passed', 'classification': 'accepted',
                            'configuration_changed': False, 'restart_requested': False},
                        service_invocation=self.manager.invocation, boot_context=context)
                    os._exit(0)
                except BaseException: os._exit(22)
            os.close(pipe_w); self.manager.pid = os.read(pipe_r, 64).decode(); os.close(pipe_r)
            self.manager.active = True
        self.manager.start = start; self.controller.sleep = time.sleep
        self.plan['wait_seconds'] = 5
        record = self.controller.load(job); record['wait_seconds'] = 5; ws.write(self.controller.path(job), record)
        try:
            result = self.controller.resume(job)
            self.assertEqual(result['result'], 'passed', str(result)); self.assertTrue(result['fresh_evaluation_verified'])
            self.assertEqual(self.store.optional('boot.json')['phase'], 'entered')
            service.request_pause(self.directory); os.kill(child, signal.SIGTERM)
            _, status = os.waitpid(child, 0); child = None
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertEqual(self.store.optional('status.json')['phase'], 'stopped')
        finally:
            if child:
                os.kill(child, signal.SIGKILL); os.waitpid(child, 0)
        with ws.automatic_lease(self.directory): pass
