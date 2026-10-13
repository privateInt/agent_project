"""Private file transactions and actual leases; synthetic manager/inventory/readback."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from ops_tools import maintenance_execute as e
from ops_tools.tests import test_maintenance_control as fixture
from ops_platform.contracts import ReadFailure


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        review=fixture.ReviewTests('test_plan_actual_core_gates_without_journal_mutation_or_control')
        review.setUp(); self.addCleanup(review.doCleanups)
        self.f=review.f; self.snapshot=review.snapshot
        self.x=e.Execute.__new__(e.Execute)
        self.x.m=fixture.m; self.x.review=review.r; self.x.root=e.ROOT; self.x.r=fixture.runtime
        self.x.core=fixture.core; self.x.directory=self.f.directory; self.x.controller=review.r.controller
        self.x.folder=self.x.controller.store.path.parent/'maintenance-control'
        self.x.binding=None; self.x.durable={}; self.x.hook=lambda phase:None
        self.x.connect_control=lambda:None  # The only synthetic manager; never systemctl.
        self.x.controller.readback=self.f.readback
        self.x.controller.return_capture=lambda *a:{'synthetic':True}
        self.x.controller.return_check=lambda *a:None

    def bytes(self):return fixture.fixtures.snapshot(self.f.fixture.root)
    def pause(self):
        public=self.x.plan()[0]; result=self.x.stop(public['review_id'])
        self.assertEqual(result['result'],'passed',result); return result['job_id']
    def failed_readback(self):
        job=self.pause()
        self.x.controller.readback=lambda *a:(_ for _ in ()).throw(ReadFailure('OPS_RESOURCE_READBACK_FAILED'))
        result=self.x.return_worker(job)
        self.assertEqual(result['code'],'OPS_RESOURCE_READBACK_FAILED',result)
        return job

    def test_plan_creates_no_files_and_does_not_read_keys_or_control(self):
        before=self.bytes(); commands=self.f.manager.control_commands
        public,proposal,discovery=self.x.plan()
        self.assertEqual(public['result'],'passed');self.assertFalse(public['review_authorizes_execution'])
        self.assertEqual(before,self.bytes());self.assertFalse(self.x.folder.exists())
        self.assertEqual(commands,self.f.manager.control_commands)
        self.assertEqual(proposal['inventory'],discovery['inventory'])

    def test_wrong_review_never_writes_or_stops(self):
        before=self.bytes();commands=self.f.manager.control_commands
        with self.assertRaisesRegex(e.Failure,'PLAN_CHANGED'):self.x.stop('0'*64)
        self.assertEqual(before,self.bytes());self.assertEqual(commands,self.f.manager.control_commands)

    def test_owned_worker_stops_once_containers_unchanged_and_return_is_explicit(self):
        before=deepcopy(self.snapshot);fixed=self.f.fixed_bytes();job=self.pause()
        self.assertEqual((self.f.manager.stops,self.f.manager.starts),(1,0))
        self.assertEqual(self.snapshot,before);self.assertEqual(self.x.load(job)['phase'],'paused')
        self.assertEqual(self.f.store.control()['desired'],'paused')
        self.assertFalse(self.x.r.service.enabled(self.f.manifest));self.assertEqual(fixed,self.f.fixed_bytes())

    def test_explicit_return_new_receipt_evaluation_and_no_second_start(self):
        job=self.pause();old=self.f.manager.invocation
        result=self.x.return_worker(job)
        self.assertEqual(result['result'],'passed',result);self.assertTrue(result['fresh_evaluation_verified'])
        self.assertNotEqual(old,self.f.manager.invocation);self.assertEqual(self.f.manager.starts,1)
        self.assertEqual(self.x.load(job)['phase'],'returned')
        with self.assertRaisesRegex(e.Failure,'RETURN_REFUSED'):self.x.return_worker(job)
        self.assertEqual(self.f.manager.starts,1)

    def test_second_stop_during_paused_job_does_not_stop_again(self):
        self.pause();commands=self.f.manager.control_commands
        with self.assertRaisesRegex(e.Failure,'UNRESOLVED_JOB'):self.x.stop('0'*64)
        self.assertEqual(commands,self.f.manager.control_commands)

    def test_scope_protected_files_context_and_started_time_changes_block_before_stop(self):
        public=self.x.plan()[0]
        self.snapshot['inventory']['protected']['e'*64]='changed'
        with self.assertRaisesRegex(e.Failure,'PLAN_CHANGED'):self.x.stop(public['review_id'])
        self.assertEqual(self.f.manager.stops,0)

    def test_scope_identity_changes_after_pause_block_return_without_start(self):
        job=self.pause();self.snapshot['files']['foreign-change']=[]
        with self.assertRaisesRegex(ReadFailure,'DISCOVERY_CHANGED'):self.x.return_worker(job)
        self.assertEqual(self.f.manager.starts,0);self.assertEqual(self.f.store.control()['desired'],'paused')

    def test_no_start_after_newer_manual_stop_token(self):
        job=self.pause();control=self.f.store.control();control['service_stop_token']='new-manual-stop'
        self.x.r.ws.write(self.f.store.path/'control.json',control)
        result=self.x.return_worker(job)
        self.assertEqual(result['result'],'attention_required');self.assertEqual(self.f.manager.starts,0)
        self.assertEqual(self.f.store.control()['service_stop_token'],'new-manual-stop')

    def test_live_readback_failure_preserves_pause_and_repeated_return_refused(self):
        job=self.failed_readback();commands=self.f.manager.control_commands
        self.assertEqual(self.f.manager.starts,0)
        with self.assertRaisesRegex(e.Failure,'RETURN_REFUSED'):self.x.return_worker(job)
        self.assertEqual(commands,self.f.manager.control_commands)

    def test_recovery_plan_is_readonly_and_does_not_clear_latch(self):
        job=self.failed_readback();before=self.bytes();commands=self.f.manager.control_commands
        public=self.x.recovery_plan(job)[0]
        self.assertEqual(public['recovery_mode'],'prepare_explicit_return')
        self.assertFalse(public['worker_will_be_started']);self.assertEqual(before,self.bytes())
        self.assertEqual(commands,self.f.manager.control_commands)
        self.assertEqual(self.f.store.control()['recovery'],'OPS_RESOURCE_READBACK_FAILED')

    def test_recover_stays_paused_retains_failure_audit_then_requires_fresh_readback(self):
        job=self.failed_readback();before=deepcopy(self.x.controller.load(job));commands=self.f.manager.control_commands
        token=self.x.recovery_plan(job)[0]['recovery_review_id'];result=self.x.recover(job,token)
        self.assertEqual(result['result'],'passed');self.assertFalse(result['worker_started'])
        self.assertEqual(commands,self.f.manager.control_commands);self.assertEqual(self.f.store.control()['desired'],'paused')
        self.assertIsNone(self.f.store.control()['recovery']);self.assertFalse(self.x.r.service.enabled(self.f.manifest))
        self.assertEqual(self.x.load(job)['recovery_audit']['before_job'],before)
        self.x.controller.readback=self.f.readback
        self.assertEqual(self.x.return_worker(job)['result'],'passed');self.assertEqual(self.f.readbacks,1)

    def test_wrong_recovery_review_never_clears_or_rewrites(self):
        job=self.failed_readback();before=self.bytes();commands=self.f.manager.control_commands
        with self.assertRaisesRegex(e.Failure,'RECOVERY_PLAN_CHANGED'):self.x.recover(job,'0'*64)
        # Named mutex may be created by an explicit mutation attempt; no journal/control content changes.
        after=self.bytes()
        for path,raw in before.items():self.assertEqual(after[path],raw)
        self.assertEqual(commands,self.f.manager.control_commands)

    def test_recovery_will_not_replay_armed_or_submitted_start(self):
        job=self.failed_readback();record=self.x.controller.load(job)
        record['failure_phase']='start_requested';self.x.r.ws.write(self.x.controller.path(job),record)
        with self.assertRaisesRegex(e.Failure,'START_NOT_REPLAYABLE'):self.x.recovery_plan(job)
        self.assertEqual(self.f.manager.starts,0)

    def test_unknown_start_response_not_repeated_by_return_check_or_recover(self):
        job=self.pause();self.f.manager.start=lambda:(_ for _ in ()).throw(ReadFailure('SERVICE_COMMAND_OUTCOME_UNKNOWN'))
        result=self.x.return_worker(job);self.assertEqual(result['result'],'attention_required')
        with self.assertRaisesRegex(e.Failure,'CHECK_REFUSED'):self.x.check(job)
        with self.assertRaisesRegex(e.Failure,'START_NOT_REPLAYABLE'):self.x.recovery_plan(job)

    def test_open_refresh_intent_blocks_recovery_without_erasure(self):
        job=self.failed_readback();self.x.r.ws.write(self.f.store.path/'intent.json',{'schema':1,'phase':'open'})
        before=self.bytes()
        with self.assertRaisesRegex(Exception,'UNFINISHED_INTENT'):self.x.recovery_plan(job)
        self.assertEqual(before,self.bytes())

    def test_newer_manual_pause_is_never_cleared_by_recovery(self):
        job=self.failed_readback();control=self.f.store.control();control['service_stop_token']='new-manual-stop'
        self.x.r.ws.write(self.f.store.path/'control.json',control);before=self.bytes()
        with self.assertRaisesRegex(e.Failure,'CONTROL_CHANGED'):self.x.recovery_plan(job)
        self.assertEqual(before,self.bytes())

    def test_active_worker_or_populated_cgroup_and_manager_job_block_recovery(self):
        job=self.failed_readback()
        self.f.manager.empty=False
        with self.assertRaisesRegex(e.Failure,'WORKER_EXIT_REQUIRED'):self.x.recovery_plan(job)
        self.f.manager.empty=True;self.f.manager.active=True
        with self.assertRaisesRegex(e.Failure,'WORKER_EXIT_REQUIRED'):self.x.recovery_plan(job)

    def test_real_child_lease_blocks_recovery_before_state_write(self):
        job=self.failed_readback();token=self.x.recovery_plan(job)[0]['recovery_review_id']
        read,write=os.pipe();child=os.fork()
        if child==0:
            os.close(read)
            try:
                with self.x.r.ws.automatic_lease(self.f.directory):
                    os.write(write,b'1');os.close(write);signal.pause()
            finally:os._exit(0)
        os.close(write)
        try:
            self.assertEqual(os.read(read,1),b'1');before=self.bytes()
            with self.assertRaisesRegex(ReadFailure,'AUTOMATIC_WORKER_BUSY'):self.x.recover(job,token)
            after=self.bytes()
            for path,raw in before.items():self.assertEqual(after[path],raw)
        finally:os.close(read);os.kill(child,signal.SIGTERM);os.waitpid(child,0)

    def test_stale_source_or_modified_contract_refused_without_control(self):
        job=self.pause();row=self.x.load(job);row['discovery']['files']['manual']=[]
        self.x.r.worker.activation.rc.write_record(self.x.path(job),row)
        commands=self.f.manager.control_commands
        with self.assertRaisesRegex(e.Failure,'REVIEW_CHANGED'):self.x.return_worker(job)
        self.assertEqual(commands,self.f.manager.control_commands)

    def test_symlink_hardlink_private_modes_refused_without_repair(self):
        job=self.pause();path=self.x.path(job);raw=path.read_bytes();path.chmod(0o664)
        with self.assertRaises(Exception):self.x.load(job)
        self.assertEqual(path.stat().st_mode&0o777,0o664);path.chmod(0o600)
        alias=path.with_suffix('.alias');os.link(path,alias)
        with self.assertRaisesRegex(e.Failure,'HARDLINK'):self.x.load(job)
        alias.unlink();path.unlink();alias.write_bytes(raw);path.symlink_to(alias)
        with self.assertRaises(Exception):self.x.load(job)
        self.assertTrue(path.is_symlink())

    def test_recovery_partial_write_boundaries_finish_without_control_replay(self):
        for boundary in ('adapter:recovery_prepared','recovery:job_written','recovery:control_written'):
            with self.subTest(boundary=boundary):
                # A fresh isolated fixture per interruption; no state reuse between jobs.
                self.doCleanups();self.setUp();job=self.failed_readback();commands=self.f.manager.control_commands
                token=self.x.recovery_plan(job)[0]['recovery_review_id']
                self.x.hook=lambda phase:(_ for _ in ()).throw(OSError('synthetic interruption')) if phase==boundary else None
                with self.assertRaises(OSError):self.x.recover(job,token)
                self.x.hook=lambda phase:None
                token=self.x.recovery_plan(job)[0]['recovery_review_id']
                self.assertEqual(self.x.recover(job,token)['result'],'passed')
                self.assertEqual(commands,self.f.manager.control_commands)
                self.assertEqual(self.f.store.control()['desired'],'paused')

    def test_recovery_control_race_preserves_new_manual_stop(self):
        job=self.failed_readback();token=self.x.recovery_plan(job)[0]['recovery_review_id']
        def edit(phase):
            if phase=='recovery:job_written':
                control=self.f.store.control();control['service_stop_token']='newer-stop'
                self.x.r.ws.write(self.f.store.path/'control.json',control)
        self.x.hook=edit
        with self.assertRaisesRegex(e.Failure,'RECOVERY_CHANGED'):self.x.recover(job,token)
        self.assertEqual(self.f.store.control()['service_stop_token'],'newer-stop')

    def test_sidecar_edit_during_stop_is_not_overwritten(self):
        original=self.x.controller.run
        def run(proposal,operation):
            result=original(proposal,operation)
            path=self.x.path(proposal['id']);row=self.x.load(proposal['id']);row['manual']='edit'
            self.x.r.worker.activation.rc.write_record(path,row);return result
        self.x.controller.run=run
        with self.assertRaisesRegex(e.Failure,'RECORD_CHANGED'):self.pause()
        row=self.x.records()[0];self.assertEqual(row['manual'],'edit')
        self.assertEqual(self.f.manager.stops,1);self.assertEqual(self.f.manager.starts,0)

    def test_unsubmitted_stop_can_only_close_without_stopping_or_returning(self):
        self.x.hook=lambda phase:(_ for _ in ()).throw(OSError('before core')) if phase=='adapter:stop_requested' else None
        with self.assertRaises(OSError):self.pause()
        job=self.x.records()[0]['id'];self.x.hook=lambda phase:None
        token=self.x.recovery_plan(job)[0]['recovery_review_id'];commands=self.f.manager.control_commands
        self.assertEqual(self.x.recover(job,token)['phase'],'closed_unsubmitted')
        self.assertEqual(commands,self.f.manager.control_commands);self.assertEqual(self.f.manager.stops,0)
        self.assertEqual(self.x.plan()[0]['result'],'passed')

    def test_unsubmitted_audit_closes_while_running_worker_holds_real_lease(self):
        self.x.hook=lambda phase:(_ for _ in ()).throw(OSError('before core')) if phase=='adapter:stop_requested' else None
        with self.assertRaises(OSError):self.pause()
        job=self.x.records()[0]['id'];self.x.hook=lambda phase:None
        token=self.x.recovery_plan(job)[0]['recovery_review_id']
        with self.x.r.ws.automatic_lease(self.f.directory):
            result=self.x.recover(job,token)
        self.assertEqual(result['phase'],'closed_unsubmitted');self.assertEqual(self.f.manager.stops,0)

    def test_core_acceptance_marker_loss_check_observes_no_second_start(self):
        job=self.pause()
        self.x.hook=lambda phase:(_ for _ in ()).throw(OSError('marker loss')) if phase=='adapter:returned' else None
        # Simulate crash before adapter acceptance write, after actual core acceptance.
        save=self.x.save
        def fail(row,phase):
            if phase=='returned':raise OSError('marker not written')
            return save(row,phase)
        self.x.save=fail
        with self.assertRaises(OSError):self.x.return_worker(job)
        self.x.save=save;self.x.hook=lambda phase:None;commands=self.f.manager.control_commands
        result=self.x.check(job)
        self.assertEqual(result['result'],'passed');self.assertFalse(result['start_request_submitted'])
        self.assertEqual(commands,self.f.manager.control_commands);self.assertEqual(self.f.manager.starts,1)

    def test_repeated_recovery_and_completed_check_cannot_replay(self):
        job=self.failed_readback();token=self.x.recovery_plan(job)[0]['recovery_review_id'];self.x.recover(job,token)
        commands=self.f.manager.control_commands
        with self.assertRaisesRegex(e.Failure,'START_NOT_REPLAYABLE'):self.x.recover(job,token)
        with self.assertRaisesRegex(e.Failure,'CHECK_REFUSED'):self.x.check(job)
        self.assertEqual(commands,self.f.manager.control_commands)

    def test_readonly_status_preserves_all_state_and_never_claims_replay(self):
        job=self.pause();before=self.bytes();commands=self.f.manager.control_commands
        result=self.x.status(job);self.assertEqual(result['result'],'attention_required')
        self.assertFalse(result['execution_jobs'][0]['saved_state_authorizes_replay'])
        self.assertEqual(before,self.bytes());self.assertEqual(commands,self.f.manager.control_commands)


class BoundaryTests(unittest.TestCase):
    def test_dependency_pin_rejects_changed_source_before_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p=root/'ops_tools/maintenance_control.py';p.parent.mkdir();p.write_text('raise RuntimeError("not imported")')
            with self.assertRaisesRegex(e.Failure,'REVIEW_SOURCE_CHANGED'):e.dependencies(root)

    def test_argument_validation_precedes_constructor_and_hides_secret_errors(self):
        for args in (['stop'],['return'],['return','--job-id','bad'],['plan','--job-id','bad'],['recover','--job-id','bad']):
            with patch.object(sys,'argv',['maintenance_execute.py',*args]),patch.object(e,'Execute') as ctor,patch.object(sys,'stdout',new_callable=io.StringIO) as stream:
                self.assertEqual(e.main(),2);ctor.assert_not_called();self.assertNotIn(fixture.fixtures.SECRET,stream.getvalue())

    def test_unknown_constructor_failure_is_fixed_label_and_no_raw_text(self):
        with patch.object(sys,'argv',['maintenance_execute.py','plan']),patch.object(e,'Execute',side_effect=RuntimeError(fixture.fixtures.SECRET)),patch.object(sys,'stdout',new_callable=io.StringIO) as stream:
            self.assertEqual(e.main(),2);report=json.loads(stream.getvalue())
        self.assertEqual(report['code'],'MAINTENANCE_EXECUTION_UNAVAILABLE');self.assertNotIn(fixture.fixtures.SECRET,stream.getvalue())
        self.assertFalse(report['application_readback_requested']);self.assertFalse(report['lifecycle_writes_requested'])

    def test_cli_readonly_plan_has_zero_effects_and_never_connects_control(self):
        fake=type('Fake',(),{})()
        fake.plan=lambda:({'result':'passed','review_authorizes_execution':False},None,None)
        fake.controller=type('Controller',(),{'backend':type('Backend',(),{'control_commands':0})()})()
        fake.review=type('Review',(),{'reader':type('Reader',(),{'calls':0})(),
            'docker':type('Docker',(),{'calls':0,'base':type('Base',(),{'calls':0})()})()})()
        fake.m=fixture.m
        with patch.object(sys,'argv',['maintenance_execute.py','plan']),patch.object(e,'Execute',return_value=fake),patch.object(sys,'stdout',new_callable=io.StringIO) as stream:
            self.assertEqual(e.main(),0);report=json.loads(stream.getvalue())
        self.assertEqual(report['worker_service_control_commands'],0);self.assertEqual(report['effects']['files_written'],[])
        self.assertFalse(report['effects']['application_credential_contents_read']);self.assertEqual(report['effects']['application_api_requests'],0)


if __name__=='__main__':unittest.main()
