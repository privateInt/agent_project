"""Private real files, leases and boot entry; no real manager or Docker/API."""
from copy import deepcopy
import os
from pathlib import Path
import signal
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import boot_lifecycle as boot, refresh_worker as worker
from src.ops.platform.ops import worker_state as ws, service_lifecycle as service
from src.ops.candidates.maintenance_20260930.tests import test_maintenance as fixtures
from src.ops.tools import recover_boot_worker as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MaintenanceTests('runTest'); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        f = self.f
        self.directory, self.store, self.manager = f.directory, f.store, f.manager
        self.transition_record = dict(schema=1, kind='stopped-code-policy-transition',
                                      directory=str(f.directory), phase='committed')
        self.initial_path = self.store.path.parent / 'boot-start.json'
        ws.write(self.initial_path, dict(schema=1, phase='accepted', manifest=f.manifest,
                                        transition_sha256=boot.fingerprint(self.transition_record)))
        self.store.status(phase='recovery_required', identity=f.identity, invocation=f.invocation,
            service_invocation=f.manager.invocation, completed_cycles=2511,
            outcome=dict(result='attention_required', classification='blocked',
                         code=recovery.REASON, child_process_group_stopped=True))
        self.store.pause(recovery.REASON)
        self.manager.active = False; self.manager.pid = '0'; self.failed = True
        self.inspect = self.manager.inspect
        def inspect():
            value = self.inspect()
            if self.failed:
                value.update(ActiveState='failed', SubState='failed', Result='exit-code', ExecMainStatus='2')
            return value
        self.manager.inspect = inspect
        def reset():
            self.manager.control_commands += 1; self.manager.resets += 1; self.failed = False
        self.manager.reset_failed = reset
        self.readbacks = 0; self.now = 0.; self.tick = lambda: None
        self.tx = SimpleNamespace(directory=f.directory, store=f.store, backend=f.manager,
                                  snapshot=lambda _: deepcopy(f.fixture.binding), history=lambda: f.history)
        self.initial = SimpleNamespace(transition=self.tx, store=f.store, backend=f.manager,
            directory=f.directory, path=self.initial_path,
            committed=lambda: (deepcopy(self.transition_record), service.load(f.directory)),
            identity=worker.process_identity, environment=lambda *a: None,
            readback=self.readback, status_fn=service.status)
        self.adapter = self.make()
        self.original = {name: (self.store.path/name).read_bytes() for name in ('control.json','status.json','boot.json')}
        self.fixed = f.fixed_bytes()

    def make(self, **options):
        return recovery.Recovery(self.initial, clock=lambda: self.now, sleep=self.sleep, **options)
    def sleep(self, seconds): self.now += seconds; self.tick()
    def readback(self, directory, manifest, guard):
        self.readbacks += 1; guard()
        self.assertEqual(self.store.control()['recovery'], recovery.REASON)
        self.assertTrue(service.enabled(manifest))
        return deepcopy(fixtures.READBACK)
    def files(self):
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.fixture.root.rglob('*') if p.is_file()}
    def code(self, code, fn):
        with self.assertRaises(ReadFailure) as caught: fn()
        self.assertEqual(caught.exception.code, code)
    def proposal(self): return self.adapter.plan(3)
    def good(self):
        p = self.proposal(); result = self.adapter.apply(self.adapter.review(p), 3)
        self.assertEqual(result['result'], 'passed', result)
        return p, result

    def test_plan_is_readonly_and_no_live_api_or_controls(self):
        before, commands = self.files(), self.manager.control_commands
        p = self.proposal()
        self.assertEqual(p['control']['recovery'], recovery.REASON)
        self.assertEqual(before, self.files()); self.assertEqual(commands, self.manager.control_commands)
        self.assertFalse(self.adapter.folder.exists()); self.assertEqual(self.readbacks, 0)

    def test_wrong_review_creates_no_journal_and_does_not_reset_or_readback(self):
        before = self.files()
        self.code('BOOT_RECOVERY_REVIEW_CHANGED', lambda: self.adapter.apply('0'*64, 3))
        self.assertEqual(before, self.files()); self.assertEqual(self.readbacks, 0); self.assertEqual(self.manager.resets, 0)

    def test_other_recovery_reason_is_not_cleared(self):
        self.store.pause('OTHER_CONTAINER_CHANGED')
        self.code('BOOT_RECOVERY_REASON_NOT_SUPPORTED', self.proposal)
        self.assertEqual(self.store.control()['recovery'], 'OTHER_CONTAINER_CHANGED')

    def test_open_or_completed_intent_is_not_adopted(self):
        for phase in ('open','completed'):
            ws.write(self.store.path/'intent.json', {'phase':phase})
            self.code('WORKER_UNFINISHED_INTENT', self.proposal)
        self.assertEqual(self.manager.starts, 0)

    def test_unconfirmed_exit_or_missing_child_proof_refuses_plan(self):
        self.manager.empty = False
        self.code('BOOT_FAILED_WORKER_EXIT_REQUIRED', self.proposal)
        self.manager.empty = True
        value = self.store.optional('status.json'); value['outcome']['child_process_group_stopped'] = False
        ws.write(self.store.path/'status.json', value)
        self.code('BOOT_FAILURE_EVIDENCE_REQUIRED', self.proposal)

    def test_receipt_invocation_identity_and_generation_changes_refused(self):
        old = self.store.optional('boot.json')
        for key in ('invocation','generation','manifest_sha256','service_invocation'):
            value = dict(old); value[key] = 'wrong'; ws.write(self.store.path/'boot.json', value)
            self.code('BOOT_FAILURE_RECEIPT_REQUIRED', self.proposal)
        ws.write(self.store.path/'boot.json', old)
        self.code('BOOT_FAILURE_RECEIPT_REQUIRED', lambda: self.make(identity_fn=lambda: {'boot_id':'different'}).plan())

    def test_binding_or_initial_history_change_refuses_plan(self):
        self.tx.snapshot = lambda _: {}
        self.code('SERVICE_BINDING_CHANGED', self.proposal)
        self.tx.snapshot = lambda _: deepcopy(self.f.fixture.binding)
        value = ws.read(self.initial_path); value['phase'] = 'prepared'; ws.write(self.initial_path,value)
        self.code('BOOT_INITIAL_HISTORY_REQUIRED', self.proposal)

    def test_disabled_or_foreign_enable_link_never_repaired(self):
        service.set_enabled(self.f.manifest, False)
        self.code('BOOT_OWNED_ENABLE_REQUIRED', self.proposal)
        link = service.enabled_path(self.f.manifest); link.symlink_to('/foreign/service')
        self.code('SERVICE_ENABLE_LINK_CONFLICT', self.proposal)
        self.assertEqual(os.readlink(link), '/foreign/service')

    def test_modified_unit_or_dropin_never_repaired(self):
        self.manager.dropins = '/foreign/dropin'
        self.code('SERVICE_DROPIN_CONFLICT', self.proposal)
        self.manager.dropins = ''; self.manager.path.write_text('manual edit')
        self.code('SERVICE_UNIT_CHANGED', self.proposal)
        self.assertEqual(self.manager.path.read_text(), 'manual edit')

    def test_real_child_lease_prevents_even_readonly_plan(self):
        ready_r, ready_w = os.pipe(); release_r, release_w = os.pipe(); pid = os.fork()
        if pid == 0:
            os.close(ready_r); os.close(release_w)
            with ws.automatic_lease(self.directory): os.write(ready_w,b'1'); os.read(release_r,1)
            os._exit(0)
        os.close(ready_w); os.close(release_r)
        try:
            self.assertEqual(os.read(ready_r,1), b'1')
            self.code('AUTOMATIC_WORKER_BUSY', self.proposal)
        finally:
            os.write(release_w,b'1'); os.close(release_w); os.close(ready_r); os.waitpid(pid,0)
        self.assertFalse(self.adapter.folder.exists())

    def test_changed_stop_token_after_review_never_mutates(self):
        p = self.proposal(); service.request_pause(self.directory); before = self.files()
        self.code('BOOT_RECOVERY_REVIEW_CHANGED', lambda: self.adapter.apply(self.adapter.review(p),3))
        self.assertEqual(before, self.files()); self.assertEqual(self.manager.resets,0)

    def test_durable_failed_invocation_and_live_readback_before_reset(self):
        reset = self.manager.reset_failed
        def checked_reset():
            record = ws.read(next(self.adapter.folder.glob('*.json')))
            self.assertEqual(record['phase'],'readback_verified')
            self.assertEqual(record['failure_status'], json_value(self.original['status.json']))
            self.assertEqual(self.readbacks,1); reset()
        self.manager.reset_failed = checked_reset
        p,result = self.good()
        self.assertTrue(result['fresh_evaluation_verified']); self.assertEqual(self.manager.starts,1)
        record = ws.read(self.adapter.journal_path(p['id']))
        for source,key in (('control.json','control'),('status.json','failure_status'),('boot.json','failure_receipt')):
            self.assertEqual(record[key],json_value(self.original[source]))
        self.assertEqual(record['phase'],'accepted'); self.assertEqual(self.fixed,self.f.fixed_bytes())
        self.assertTrue(service.enabled(self.f.manifest)); self.assertEqual(self.manager.reloads,1)

    def test_readback_failure_preserves_original_latch_status_and_receipt(self):
        def fail(*a): raise ReadFailure('OPS_RESOURCE_READBACK_FAILED')
        self.adapter.readback = fail; p = self.proposal()
        result = self.adapter.apply(self.adapter.review(p),3)
        self.assertEqual(result['code'],'OPS_RESOURCE_READBACK_FAILED')
        for name,raw in self.original.items(): self.assertEqual((self.store.path/name).read_bytes(),raw)
        self.assertEqual(self.manager.starts,0); self.assertEqual(self.manager.resets,0)

    def test_journal_write_failure_never_clears_recovery_or_requests_control(self):
        p = self.proposal(); before = self.store.control(); original = ws.write
        def failed(path, value):
            if path.parent == self.adapter.folder: raise OSError('synthetic write failure')
            return original(path,value)
        with patch.object(ws,'write',side_effect=failed):
            with self.assertRaises(OSError): self.adapter.apply(self.adapter.review(p),3)
        self.assertEqual(before,self.store.control()); self.assertEqual(self.readbacks,0); self.assertEqual(self.manager.resets,0)

    def test_newer_stop_during_readback_or_release_prevents_start(self):
        for phase in ('readback','running_released'):
            if phase == 'readback':
                def readback(d,m,guard): service.request_pause(d); guard(); return fixtures.READBACK
                obj = self.make(readback_fn=readback)
            else: obj = self.make(hook=lambda at: service.request_pause(self.directory) if at == phase else None)
            p = obj.plan(3); result = obj.apply(obj.review(p),3)
            self.assertEqual(result['result'],'attention_required'); self.assertEqual(self.manager.starts,0)
            self.assertEqual(self.store.control()['desired'],'paused')
            # One loop case per independent fixture.
            if phase == 'readback': self.doCleanups(); self.setUp()

    def test_old_manager_invocation_or_modified_entry_cannot_pass(self):
        old = self.manager.invocation; start = self.manager.start
        def reused():
            start(); self.manager.invocation = old
        self.manager.start = reused; p = self.proposal()
        result = self.adapter.apply(self.adapter.review(p),3)
        self.assertEqual(result['code'],'BOOT_MANAGER_INVOCATION_REUSED'); self.assertEqual(self.manager.starts,1)

    def test_changed_new_receipt_refuses_even_a_healthy_observation(self):
        start=self.manager.start
        def changed():
            start(); receipt=self.store.optional('boot.json'); receipt['previous_receipt_sha256']='wrong'
            ws.write(self.store.path/'boot.json',receipt)
        self.manager.start=changed; p=self.proposal()
        result=self.adapter.apply(self.adapter.review(p),3)
        self.assertEqual(result['code'],'BOOT_ENTRY_EVIDENCE_INVALID')
        self.assertEqual(self.store.control()['desired'],'paused')

    def test_newer_stop_during_observation_or_final_binding_check_wins(self):
        start=self.manager.start
        def degraded():
            start(); self.f.accepted(worker.process_identity(),self.store.optional('boot.json')['invocation'])
            status=self.store.optional('status.json'); status['outcome']['classification']='retryable_prewrite'
            ws.write(self.store.path/'status.json',status)
        self.manager.start=degraded; self.tick=lambda:service.request_pause(self.directory)
        p=self.proposal(); result=self.adapter.apply(self.adapter.review(p),3)
        self.assertEqual(result['code'],'SERVICE_STOP_REQUEST_CHANGED')
        self.assertEqual(self.store.control()['desired'],'paused')

    def test_evaluation_may_commit_a_new_owned_generation_after_handoff(self):
        start=self.manager.start
        def changed():
            start(); self.f.history='a'*64
        self.manager.start=changed
        _,result=self.good(); self.assertTrue(result['fresh_evaluation_verified'])

    def test_prepared_interruption_cannot_be_replayed(self):
        class Interrupted(Exception): pass
        def crash(at):
            if at=='journal:prepared': raise Interrupted('synthetic interruption')
        obj=self.make(hook=crash); p=obj.plan(3)
        with self.assertRaises(Interrupted): obj.apply(obj.review(p),3)
        self.code('BOOT_RECOVERY_ALREADY_RECORDED_USE_CHECK',self.proposal)
        self.assertEqual(self.manager.starts,0); self.assertEqual(self.store.control()['recovery'],recovery.REASON)

    def test_changed_or_hardlinked_recovery_record_never_overwritten(self):
        p=self.proposal()
        def changed(d,m,guard):
            path=self.adapter.journal_path(p['id']); row=ws.read(path); row['manual']='keep'; ws.write(path,row)
            guard(); return fixtures.READBACK
        self.adapter.readback=changed
        with self.assertRaisesRegex(ReadFailure,'JOURNAL_CHANGED'):
            self.adapter.apply(self.adapter.review(p),3)
        path=self.adapter.journal_path(p['id']); self.assertEqual(ws.read(path)['manual'],'keep')
        os.link(path,self.f.fixture.root/'hardlink')
        self.code('BOOT_RECOVERY_RECORD_UNSAFE',lambda:self.adapter.check(p['id']))
        self.assertEqual(self.manager.starts,0)

    def test_stale_saved_pid_cannot_claim_fresh_evaluation(self):
        start = self.manager.start
        def stale(): start(); self.manager.pid = '99999999'
        self.manager.start = stale; p = self.proposal()
        result = self.adapter.apply(self.adapter.review(p),3)
        self.assertEqual(result['code'],'SERVICE_EVALUATION_WAIT_TIMEOUT'); self.assertEqual(self.store.control()['desired'],'paused')

    def test_unknown_start_latches_and_duplicate_apply_never_replays(self):
        def unknown():
            self.manager.starts += 1; raise ReadFailure('SERVICE_COMMAND_OUTCOME_UNKNOWN')
        self.manager.start = unknown; p = self.proposal()
        result = self.adapter.apply(self.adapter.review(p),3)
        self.assertEqual(result['code'],'SERVICE_COMMAND_OUTCOME_UNKNOWN'); self.assertEqual(self.store.control()['desired'],'paused')
        with self.assertRaises(ReadFailure): self.adapter.apply(self.adapter.review(p),3)
        self.assertEqual(self.manager.starts,1)

    def test_duplicate_success_and_readonly_check_never_submit_or_write(self):
        p,_ = self.good(); before=self.files(); starts=self.manager.starts
        result=self.adapter.check(p['id']); self.assertEqual(result['result'],'passed')
        self.assertEqual(before,self.files()); self.assertEqual(starts,self.manager.starts)
        with self.assertRaises(ReadFailure): self.adapter.apply(self.adapter.review(p),3)
        self.assertEqual(starts,self.manager.starts)

    def test_process_death_after_start_can_be_checked_without_second_start(self):
        # Actual child leaves durable start_requested after submission but
        # before the adapter marker. Manager projection then reads disk only.
        p=self.proposal(); pid=os.fork()
        if pid == 0:
            obj=self.make(hook=lambda at: os._exit(23) if at=='start_returned' else None)
            obj.apply(obj.review(p),3); os._exit(24)
        _,status=os.waitpid(pid,0); self.assertEqual(os.waitstatus_to_exitcode(status),23)
        entry=self.store.optional('boot.json'); self.manager.active=True; self.failed=False
        self.manager.invocation=entry['service_invocation']; self.manager.pid=str(os.getpid())
        # Child's PID has exited; stale process evidence must fail until a
        # live synthetic manager supplies a genuinely live observation.
        self.code('SERVICE_EVALUATION_WAIT_TIMEOUT',lambda:self.make().check(p['id']))
        self.assertEqual(self.manager.starts,0)
        self.assertEqual(ws.read(self.adapter.journal_path(p['id']))['phase'],'start_requested')

    def test_lost_submitted_marker_check_uses_current_entry_without_resubmission(self):
        p,_=self.good(); record=ws.read(self.adapter.journal_path(p['id'])); record['phase']='start_requested'
        ws.write(self.adapter.journal_path(p['id']),record); before=self.files()
        self.assertEqual(self.make().check(p['id'])['result'],'passed')
        self.assertEqual(before,self.files()); self.assertEqual(self.manager.starts,1)

    def test_real_worker_consumes_new_receipt_and_evaluates_then_explicitly_stops(self):
        child=[None]
        def start():
            self.manager.starts+=1; self.manager.active=True; self.manager.invocation=uuid.uuid4().hex
            pid=os.fork()
            if pid==0:
                try:
                    for descriptor in list(Path('/proc/self/fd').iterdir()):
                        try:
                            if int(descriptor.name)>2: os.close(int(descriptor.name))
                        except OSError: pass
                    context=boot.Context(self.directory,self.f.manifest,manager_check=lambda *a:None,
                        pending_check=lambda *a:None,snapshot_fn=lambda _,binding,budget:binding)
                    result=worker.run(self.directory,interval=30,deadline_seconds=3,
                        snapshot_fn=lambda _:deepcopy(self.f.fixture.binding),service_invocation=self.manager.invocation,
                        boot_context=context,evaluate_fn=lambda *a:dict(result='passed',classification='accepted',
                                              configuration_changed=False,restart_requested=False))
                    os._exit(result['exit_code'])
                except BaseException: os._exit(27)
            child[0]=pid
        inspect=self.manager.inspect
        def observed():
            value=self.store.optional('status.json') or {}
            if child[0] and value.get('service_invocation')==self.manager.invocation:
                self.manager.pid=str(value.get('identity',{}).get('proc_pid','0'))
            return inspect()
        self.manager.start=start; self.manager.inspect=observed
        obj=recovery.Recovery(self.initial,clock=time.monotonic,sleep=time.sleep)
        p=obj.plan(8)
        try:
            result=obj.apply(obj.review(p),8); self.assertEqual(result['result'],'passed',result)
            self.assertTrue(result['fresh_evaluation_verified']); service.request_pause(self.directory)
            _,status=os.waitpid(child[0],0); child[0]=None
            self.assertEqual(os.waitstatus_to_exitcode(status),0)
            self.assertEqual(self.store.optional('status.json')['phase'],'stopped')
        finally:
            if child[0]: os.kill(child[0],signal.SIGKILL); os.waitpid(child[0],0)

    def test_cli_reports_only_bounded_codes_and_no_private_exception_text(self):
        def failing(_): raise OSError('secret private key and stderr')
        result=recovery.run('plan',self.directory,factory=failing)
        self.assertEqual(result['code'],'BOOT_RECOVERY_LOCAL_ERROR')
        self.assertNotIn('secret',str(result)); self.assertEqual(result['worker_service_control_commands'],0)


def json_value(raw):
    import json
    return json.loads(raw)


class SourceReviewTests(unittest.TestCase):
    def test_pinned_controller_candidate_and_loaded_boot_protocol_are_current(self):
        from src.ops.tools import boot_control as ctl
        root=Path(recovery.__file__).resolve().parents[1]
        self.assertEqual(ctl.sha(ctl.source_read(root/'src/ops/tools/boot_control.py')),recovery.BOOT_CONTROL_SHA)
        state=ctl.reviewed_sources(root)
        self.assertEqual(state['code_state'],'candidate'); self.assertEqual(state['code_files'],61)
        arming=ctl.load('recovery_test_reviewed_arming',root/ctl.START/'arming.py')
        bundle=ctl.load('recovery_test_reviewed_transition',root/ctl.TX/'transition.py').Bundle(root/ctl.TX)
        for module,names in ((boot,('Context.enter','Context.guards')),
                             (worker,('run','acknowledge_recovery')),
                             (ws,('transition_gate','Store.gate')),(service,('run',))):
            raw=bundle.payload['src/ops/platform/ops/'+Path(module.__file__).name]
            for name in names: arming.loaded_function(module,name,raw)

    def test_changed_controller_source_is_refused_before_state_or_manager_access(self):
        with patch.object(Path,'read_bytes',return_value=b'changed controller'):
            with self.assertRaises(ValueError): recovery.controller(Path('/unread-private-state'))
