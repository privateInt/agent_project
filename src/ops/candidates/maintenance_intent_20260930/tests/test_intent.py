"""Original 33 contracts plus deterministic first-evaluation intent boundaries."""
from copy import deepcopy
from pathlib import Path
import os
import signal
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from src.ops.candidates.maintenance_20260930.tests.test_maintenance import MaintenanceTests as OriginalTests
from src.ops.candidates.maintenance_intent_20260930 import maintenance as core
from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import worker_state as ws, refresh_worker as worker, boot_lifecycle as boot, service_lifecycle as service


class MaintenanceTests(OriginalTests):
    def setUp(self):
        super().setUp()
        self.controller.__class__ = core.Maintenance
        self.controller.return_capture = lambda *args: {'synthetic': True}
        self.controller.return_check = lambda *args: None
        self.plan = self.controller.plan(wait_seconds=1)

    def open_evaluation(self):
        self.start(); last = self.store.optional('status.json')
        evaluation = last['invocation'] + ':1'
        self.store.status(phase='evaluating', identity=last['identity'], invocation=last['invocation'],
            service_invocation=self.manager.invocation, completed_cycles=0, evaluation=evaluation,
            deadline_seconds=self.manifest['deadline_seconds'])
        ws.write(self.store.path/'intent.json', {'schema':1,'phase':'open','evaluation':evaluation,'generation':1})

    def complete(self):
        intent=self.store.optional('intent.json'); intent['phase']='completed'; ws.write(self.store.path/'intent.json',intent)
        last=self.store.optional('status.json'); self.accepted(last['identity'],last['invocation'])

    def test_current_first_evaluation_open_intent_is_waited_then_fresh_completed_return_passes(self):
        job=self.stop_job(); self.manager.start=self.open_evaluation; waits=[]
        def finish(_): waits.append(1); self.complete()
        self.controller.sleep=finish
        result=self.controller.resume(job)
        self.assertEqual(result['result'],'passed',result); self.assertTrue(waits)
        self.assertEqual(self.manager.starts,1); self.assertIsNone(self.store.control()['recovery'])
        self.assertEqual(self.controller.load(job)['phase'],'returned')

    def test_open_intent_remains_forbidden_before_arm_and_when_waiting_or_stale(self):
        job=self.stop_job(); ws.write(self.store.path/'intent.json',{'schema':1,'phase':'open'})
        result=self.controller.resume(job)
        self.assertEqual(result['code'],'WORKER_UNFINISHED_INTENT'); self.assertEqual(self.manager.starts,0)

    def test_waiting_success_cannot_retain_an_open_intent(self):
        job=self.stop_job()
        def waiting_open():
            self.open_evaluation(); last=self.store.optional('status.json'); self.accepted(last['identity'],last['invocation'])
        self.manager.start=waiting_open
        result=self.controller.resume(job)
        self.assertEqual(result['code'],'WORKER_UNFINISHED_INTENT'); self.assert_paused()
        self.assertEqual(self.manager.starts,1)

    def test_mismatched_evaluation_or_unverified_worker_never_bypasses_gate(self):
        job=self.stop_job()
        def wrong():
            self.open_evaluation(); intent=self.store.optional('intent.json'); intent['evaluation']='old:1'
            ws.write(self.store.path/'intent.json',intent)
        self.manager.start=wrong
        self.assertEqual(self.controller.resume(job)['code'],'WORKER_UNFINISHED_INTENT')

    def test_open_intent_timeout_latches_and_explicit_return_is_not_resubmitted(self):
        job=self.stop_job(); self.manager.start=self.open_evaluation
        clock=iter([0,0,1,2,3,4]); self.controller.clock=lambda:next(clock)
        result=self.controller.resume(job)
        self.assertEqual(result['code'],'SERVICE_EVALUATION_WAIT_TIMEOUT',result); self.assert_paused()
        with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_RETURN_REFUSED'): self.controller.resume(job)
        self.assertEqual(self.manager.starts,1)

    def test_real_worker_open_intent_then_completion_passes_in_one_invocation(self):
        job=self.stop_job(); child=None
        def evaluate(store,*args):
            evaluation=args[-1]
            ws.write(store.path/'intent.json',{'schema':1,'phase':'open','evaluation':evaluation,'generation':1})
            time.sleep(.15)
            ws.write(store.path/'intent.json',{'schema':1,'phase':'completed','evaluation':evaluation,'generation':1})
            return {'result':'passed','classification':'accepted','configuration_changed':False,'restart_requested':False}
        def start():
            nonlocal child
            self.manager.starts+=1; self.manager.invocation=uuid.uuid4().hex
            pipe_r,pipe_w=os.pipe(); child=os.fork()
            if child==0:
                self.close_management_lock(); os.close(pipe_r)
                os.write(pipe_w,str(worker.process_identity()['proc_pid']).encode());os.close(pipe_w)
                context=boot.Context(self.directory,self.manifest,policy_check=lambda *args:None,
                    manager_check=lambda *args:None,pending_check=lambda *args:None,
                    snapshot_fn=lambda *args:deepcopy(self.fixture.binding))
                try:
                    worker.run(self.directory,interval=30,deadline_seconds=self.manifest['deadline_seconds'],
                        snapshot_fn=lambda _:deepcopy(self.fixture.binding),evaluate_fn=evaluate,
                        service_invocation=self.manager.invocation,boot_context=context)
                    os._exit(0)
                except BaseException:os._exit(22)
            os.close(pipe_w);self.manager.pid=os.read(pipe_r,64).decode();os.close(pipe_r);self.manager.active=True
        self.manager.start=start; self.controller.sleep=lambda _:time.sleep(.02)
        record=self.controller.load(job);record['wait_seconds']=5;ws.write(self.controller.path(job),record)
        observed=[]; status=self.controller.status_fn
        def observe(*args):
            value=status(*args)
            if (value['worker'].get('intent') or {}).get('phase')=='open':observed.append(1)
            return value
        self.controller.status_fn=observe
        try:
            result=self.controller.resume(job)
            self.assertEqual(result['result'],'passed',result);self.assertTrue(observed)
            self.assertEqual(self.manager.starts,1)
            service.request_pause(self.directory);os.kill(child,signal.SIGTERM)
            _,exit_status=os.waitpid(child,0);child=None;self.assertEqual(os.waitstatus_to_exitcode(exit_status),0)
        finally:
            if child:os.kill(child,signal.SIGKILL);os.waitpid(child,0)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory=Path('/synthetic/state'); self.files={'config.json':{'sha256':'a'*64}}
        self.fields={'prometheus_url':{'source':'discovery','value':'synthetic-url','automatic_future_update_allowed':True,'identity':{'service':'prometheus'}}}
        self.old={'bootstrap.json':{'sha256':'b'*64}}
        self.evidence={'source_sha256':'c'*64,'files_sha256':boot.fingerprint(self.files),
            'fields_sha256':boot.fingerprint(core.ownership(self.fields)),'evidence_sha256':core.change.digest(self.old),'generation':1}
        self.record={'return_evidence':self.evidence}
        self.path=self.directory/'discovery-refresh/000001/baseline.json'
        self.baseline={'refresh_generation':1,'source_sha256':'c'*64,'additional_evidence':deepcopy(self.old),'files':deepcopy(self.files),
            'plan':{'fields':deepcopy(self.fields),'deployment_metadata_changed':True,'changed_fields':[],
                    'manual_overrides_detected':[],'restart_required':False}}
        self.new={'phase':'prepared','baseline':self.baseline,'baseline_sha256':'d'*64}
        self.context={'records':[(self.path,self.new)],'evidence':{**self.old,str(self.path.relative_to(self.directory)):{'sha256':'e'*64}},
                      'fields':deepcopy(self.fields),'pending':(self.path,self.new)}
        self.intent={'schema':1,'phase':'open','evaluation':'test:1','generation':1,'baseline_sha256':'d'*64,
            'prewrite_files':deepcopy(self.files),'prewrite_evidence_sha256':self.evidence['evidence_sha256']}
        self.patches=[patch.object(worker.manage,'existing',return_value={}),
            patch.object(worker.activation.rc,'files',side_effect=lambda *args:deepcopy(self.files)),
            patch.object(worker.refresh,'lineage',side_effect=lambda *args:deepcopy(self.context))]
        for value in self.patches:value.start();self.addCleanup(value.stop)

    def check(self,intent=None):core.check_return(self.directory,self.record,self.intent if intent is None else intent)

    def test_one_metadata_generation_allows_pending_current_intent_then_accepted_completed(self):
        self.check();self.new.update(phase='accepted',report={'result':'passed','configuration_changed':False,'restart_requested':False,'generation':1})
        self.context['pending']=None;self.context['fields']['prometheus_url']['observed_identity']={'service':'prometheus'}
        self.new['baseline']['plan']['fields']=deepcopy(self.context['fields'])
        intent=deepcopy(self.intent);intent['phase']='completed';self.check(intent)

    def test_new_generation_source_config_override_restart_or_ownership_changes_are_refused(self):
        original=deepcopy(self.baseline)
        for edit in ('source','files','configuration','override','restart','ownership'):
            self.baseline.clear();self.baseline.update(deepcopy(original))
            if edit=='source':self.baseline['source_sha256']='f'*64
            elif edit=='files':self.baseline['files']['config.json']['sha256']='f'*64
            elif edit=='configuration':self.baseline['plan']['changed_fields']=['prometheus_url']
            elif edit=='override':self.baseline['plan']['manual_overrides_detected']=['prometheus_url']
            elif edit=='restart':self.baseline['plan']['restart_required']=True
            else:self.baseline['plan']['fields']['prometheus_url']['automatic_future_update_allowed']=False
            with self.subTest(edit=edit),self.assertRaisesRegex(ReadFailure,'MAINTENANCE_RETURN_GENERATION_CHANGED'):self.check()

    def test_old_evidence_mutation_extra_generation_or_mismatched_intent_is_not_adopted(self):
        self.context['evidence']['bootstrap.json']['sha256']='f'*64
        with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_PROVENANCE_CHANGED'):self.check()
        self.context['evidence']['bootstrap.json']['sha256']='b'*64
        self.context['records'].append((self.path,self.new))
        with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_PROVENANCE_CHANGED'):self.check()
        self.context['records'].pop();self.intent['baseline_sha256']='f'*64
        with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_RETURN_INTENT_CHANGED'):self.check()

    def test_current_configuration_drift_and_pending_without_matching_intent_are_refused(self):
        self.files['config.json']['sha256']='f'*64
        with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_RETURN_CONFIGURATION_CHANGED'):self.check()
        self.files['config.json']['sha256']='a'*64
        with self.assertRaisesRegex(ReadFailure,'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK'):self.check({})


class JournalTests(unittest.TestCase):
    def test_actual_worker_intent_and_refresh_lineage_files_complete_one_metadata_only_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'state';directory.mkdir(mode=0o700)
            owner={'schema':1,'uid':os.getuid(),'gid':os.getgid(),'owner':str(uuid.uuid4())}
            ws.write(directory/'owner.json',owner);binding={'fixture':'same-owned-installation'}
            store=ws.Store(directory,binding)
            fields={name:{'source':'discovery','value':'synthetic-'+name,'automatic_future_update_allowed':True}
                    for name in worker.refresh.FIELDS}
            anchor={'schema':1,'kind':worker.activation.KIND,'phase':'accepted','report':{'result':'passed'},
                'baseline':{'directory':str(directory),'plan':{'fields':fields},'source_sha256':'a'*64}}
            anchor['baseline_sha256']=core.change.digest(anchor['baseline'])
            folder=directory/'discovery-activation';folder.mkdir(mode=0o700);ws.write(folder/'baseline.json',anchor)
            ws.write(directory/'synthetic-config.json',{'url':'unchanged'})
            files=lambda *args:{'config.json':worker.activation.rc.read_file(directory/'synthetic-config.json',private=True)[1]}
            with patch.object(worker.manage,'existing',return_value={}),patch.object(worker.activation.rc,'files',side_effect=files), \
                 patch.object(worker.activation,'source_snapshot',return_value='b'*64):
                record={'history_sha256':core.history(directory)}
                record['return_evidence']=core.capture_return(directory,record)
                context=worker.refresh.lineage(directory)
                baseline={'directory':str(directory),'refresh_generation':1,'source_sha256':'b'*64,
                    'additional_evidence':context['evidence'],'files':files(),'container':{'identity':{'id':'c'*64}},
                    'plan':{'fields':fields,'deployment_metadata_changed':True,'changed_fields':[],
                            'manual_overrides_detected':[],'restart_required':False}}
                pending={'schema':1,'kind':worker.activation.KIND,'phase':'prepared','baseline':baseline,
                    'baseline_sha256':core.change.digest(baseline)}
                guard=worker.Guard(store,binding,lambda _:binding,'new-invocation:1')
                guard.before_write(pending)
                # Intent can precede even the provenance directory; validate both boundaries.
                core.check_return(directory,record,store.optional('intent.json'))
                journal=directory/'discovery-refresh/000001';journal.mkdir(mode=0o700,parents=True)
                journal.parent.chmod(0o700);path=journal/'baseline.json';worker.refresh.save(path,pending)
                self.assertIsNotNone(worker.refresh.lineage(directory)['pending'])
                core.check_return(directory,record,store.optional('intent.json'))
                report={'result':'passed','configuration_changed':False,'restart_requested':False,'generation':1}
                pending.update(phase='accepted',report=report);worker.refresh.save(path,pending);guard.complete(report)
                core.check_return(directory,record,store.optional('intent.json'))
                self.assertEqual(store.optional('intent.json')['phase'],'completed')
                self.assertEqual(len(worker.refresh.lineage(directory)['records']),1)
                self.assertEqual(worker.activation.rc.read_file(directory/'synthetic-config.json',private=True)[1],files()['config.json'])


del OriginalTests
if __name__=='__main__':unittest.main()
