"""Real private files/leases/pipes; synthetic Docker, crypto and systemd only."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from ops_tools import backup_execute as e, backup_measure as m
from ops_tools.tests import test_maintenance_execute as wf
from ops_candidates.backup_generation_20261001.tests import test_generation as gf


class Limits(unittest.TestCase):
    def setUp(self):
        self.row={'stores':{'one':dict(logical_bytes=100,entries=2),'two':dict(logical_bytes=200,entries=3)},
                  'files':dict(logical_bytes=300,entries=4),'images':{'sha256:'+'1'*64:['sha256:'+'1'*64,'test',500]}}
    def test_aggregate_limits_and_uniform_storage_limit(self):
        self.row['files']['logical_bytes']=10**9
        self.row['images']['sha256:'+'1'*64][2]=10**9
        policy=m.limits(self.row)
        self.assertEqual(policy['aggregate_file_bytes'],1300000000)
        self.assertEqual(policy['aggregate_image_export_bytes'],1300000000+16*1024**2)
        self.assertEqual(policy['max_entries'],38)
        result=m.envelope(policy,2)
        expected=2*(policy['per_storage_bytes']+38*8192+e.g.c.MAX_MANIFEST+65536)
        expected+=policy['aggregate_file_bytes']+38*8192+e.g.c.MAX_MANIFEST+65536
        expected+=policy['aggregate_image_export_bytes']+e.g.c.MAX_MANIFEST+65536
        self.assertEqual(result['plaintext_upper_bytes'],expected)
        self.assertFalse(result['compression_savings_assumed'])
    def test_invalid_types_bounds_and_entry_overflow(self):
        for headroom in (True,19,101,1.2):
            with self.assertRaises(e.setup.Failure):m.limits(self.row,headroom)
        for key,value in (('logical_bytes',True),('entries',65536),('logical_bytes',-1)):
            changed=deepcopy(self.row);changed['files'][key]=value
            with self.assertRaises(e.setup.Failure):m.limits(changed)
    def test_image_metadata_is_not_export_measurement(self):
        self.assertFalse(m.envelope(m.limits(self.row),2)['archive_size_verified'])
        self.row['images']['sha256:'+'1'*64][0]='sha256:'+'2'*64
        with self.assertRaises(e.setup.Failure):m.limits(self.row)


class Survey(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); (self.root/'nested').mkdir(); (self.root/'body').write_bytes(b'private-body'*10)
        info=self.root.stat(); self.config=dict(schema=1,id=str(uuid.uuid4()),role='data',source='/source',
             identity=e.g.c.identity(info),uid=os.getuid(),gid=os.getgid(),seconds=10,
             max_bytes=1024,max_entries=100,quiescence_identity='test')
    def invoke(self):
        code=m.script().replace("os.open('/source',",'os.open('+repr(str(self.root))+',')
        result=subprocess.run([sys.executable,'-I','-B','-c',code,json.dumps(self.config)],capture_output=True,timeout=15)
        return result,json.loads(result.stdout)
    def test_real_helper_stats_without_opening_file_bodies(self):
        result,value=self.invoke(); self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(value['logical_bytes'],120);self.assertEqual(value['entries'],3)
        self.assertFalse(value['file_bodies_read']);self.assertNotIn('private-body',result.stdout.decode())
    def test_links_and_special_entries_refused(self):
        for kind in ('symlink','hardlink','fifo'):
            p=self.root/'bad'
            if kind=='symlink':p.symlink_to(self.root/'body')
            elif kind=='hardlink':os.link(self.root/'body',p)
            else:os.mkfifo(p)
            result,value=self.invoke();self.assertEqual(result.returncode,2);self.assertEqual(value['code'],'BACKUP_SURVEY_FAILED')
            p.unlink()
    def test_root_identity_uid_and_deadline_refused(self):
        for field,value in (('identity',[0]*5),('uid',os.getuid()+1),('seconds',0)):
            old=self.config[field];self.config[field]=value
            self.assertEqual(self.invoke()[0].returncode,2);self.config[field]=old
    def test_measure_service_mutations_always_refused(self):
        backend=m.Docker({'endpoint':'unix:///unused','daemon_id':'unused','docker_root':'/unused'},sys.executable)
        for method in (backend.stop,backend.start):
            with self.assertRaisesRegex(e.setup.Failure,'SERVICE_CONTROL_REFUSED'):method(None)
    def test_recipe_uses_dynamic_uid_no_network_and_exact_script(self):
        fixture=gf.GenerationTests('test_complete_generation_covers_storage_files_unique_images_and_catalog')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        spec=m.recipe(fixture.proposal,'data',str(uuid.uuid4()),10)
        self.assertEqual(spec['uid'],fixture.src.stat().st_uid);self.assertEqual(spec['script'],m.script())
        self.assertTrue(spec['name'].startswith('ops-measure-'))
        self.assertEqual(spec['spec_sha256'],e.g.c.fingerprint({k:v for k,v in spec.items() if k!='spec_sha256'}))


class Execution(unittest.TestCase):
    def setUp(self):
        self.w=wf.ExecuteTests('test_owned_worker_stops_once_containers_unchanged_and_return_is_explicit')
        self.w.setUp();self.addCleanup(self.w.doCleanups)
        self.gf=gf.GenerationTests('test_complete_generation_covers_storage_files_unique_images_and_catalog')
        self.gf.setUp();self.addCleanup(self.gf.doCleanups)
        # Rename the synthetic frontend role to the production role; no daemon.
        cold=deepcopy(self.gf.proposal); cold.pop('review_id')
        cold['services']['ops-web']=cold['services'].pop('frontend');cold['services']['ops-web']['service']='ops-web'
        cold['dependencies']['ops-web']=cold['dependencies'].pop('frontend');cold['stop_order']=['ops-web','database']
        cold['review_id']=e.g.c.fingerprint(cold);self.gf.backend.rows=deepcopy(cold['services'])
        self.gf.backend.owned_helpers=self.gf.backend.helpers
        self.gen=e.g.plan(cold,self.gf.files,self.gf.images)
        x=e.Execute.__new__(e.Execute); x.__dict__.update(self.w.x.__dict__);self.x=x
        x.control=self.gf.root/'control';x.control.mkdir(mode=0o700);x.folder=x.control/'executions'
        x.home=self.gf.root;x.connected=SimpleNamespace(preparation={'test':True})
        x.review.cap=SimpleNamespace(bp=None)
        capacity=patch.object(e.bp,'capacity',return_value=dict(usable_after_reserve_bytes=10**12,
            available_bytes=10**12,reserve_bytes=0,actual_archive_size_verified=False))
        capacity.start();self.addCleanup(capacity.stop)
        x.original_snapshot=x.controller.snapshot;x.generation=None;x.backend=self.gf.backend
        inv=e.s.Operation(cold,x.backend,None,cold['recipients']).inventory()
        self.w.snapshot['inventory']=deepcopy(inv);x.review.current=deepcopy(self.w.snapshot)
        x.controller.inventory=lambda:deepcopy(inv)
        self.binary=e.binary_identity(sys.executable)
        self.measured=dict(schema=1,kind=e.MEASUREMENT_KIND,id=str(uuid.uuid4()),measured_at=time.time(),
            base_generation=e.binding(self.gen),preparation=x.connected.preparation,source_pins=e.source_pins(x.root),
            docker_binary=self.binary,stores={'data':dict(logical_bytes=80000,entries=3)},
            files=dict(logical_bytes=100,entries=3),images={ident:self.gf.exporter.inspect(ident) for ident in self.gf.images})
        folder=x.control/'measurements';folder.mkdir(mode=0o700);self.measurement=folder/(self.measured['id']+'.json')
        e.setup.durable_new(self.measurement,self.measured)
        def fresh():
            # Core planning is real; only discovery comes from synthetic metadata.
            proposal=x.controller.plan()
            return {'result':'passed'},proposal,deepcopy(self.w.snapshot),deepcopy(self.gen),deepcopy(self.binary)
        x.fresh=fresh
        # Preparation/owner/daemon are fixture seams. All journals, reviewed
        # contracts, generation validation, service return and core leases run.
        x.metadata_guard=lambda row:(e.require(x.load(row['id'])==row,'BACKUP_EXECUTION_RECORD_CHANGED'),e.g.validate(row['generation']))
        x.paused_snapshot=lambda directory,row:x.original_snapshot(directory)
        x.build_generation=lambda row:e.Generation(row['generation'],x.backend,self.gf.codec,self.gf.exporter,test_codec=True)
        def synthetic_receipt(row):
            gen=x.generation;receipt=e.setup.json_read(gen.folder/'receipt.json')
            self.assertEqual(receipt['encryption'],'synthetic_identity_codec')
            self.assertEqual(e.g.inspect(gen.folder,receipt)['result'],'captured_integrity_verified');return receipt
        x.live_receipt=synthetic_receipt
    def capture(self):
        public=self.x.plan(self.measurement)[0]
        result=self.x.capture(public['review_id'],self.measurement,identity_custody=True,application_drain=True)
        return result['job_id'],result
    def fail_capture(self):
        self.gf.exporter.mode='fail';job,result=self.capture()
        self.assertEqual(result['result'],'attention_required',result);return job
    def test_readonly_plan_writes_nothing_and_requires_capacity(self):
        before=self.w.bytes(); public,row=self.x.plan(self.measurement)
        self.assertEqual(public['result'],'passed');self.assertFalse(public['review_authorizes_execution'])
        self.assertEqual(before,self.w.bytes());self.assertFalse(self.x.folder.exists());self.assertEqual(self.gf.backend.calls,[])
        self.assertEqual(self.w.f.manager.stops,0)
    def test_wrong_review_and_missing_attestations_refuse_before_effects(self):
        with self.assertRaisesRegex(e.setup.Failure,'ATTESTATIONS_REQUIRED'):self.x.capture('0'*64,self.measurement)
        with self.assertRaisesRegex(e.setup.Failure,'PLAN_CHANGED'):
            self.x.capture('0'*64,self.measurement,identity_custody=True,application_drain=True)
        self.assertEqual(self.w.f.manager.stops,0);self.assertFalse(self.x.folder.exists());self.assertEqual(self.gf.backend.calls,[])
    def test_capacity_insufficient_refuses_before_pause_or_generation(self):
        self.x.space=lambda generation,policy:({'stream_budget_fits_reserved_capacity':False},m.envelope(policy,1))
        public=self.x.plan(self.measurement)[0];self.assertEqual(public['result'],'blocked')
        with self.assertRaisesRegex(e.setup.Failure,'CAPACITY_INSUFFICIENT'):
            self.x.capture(public['review_id'],self.measurement,identity_custody=True,application_drain=True)
        self.assertEqual(self.w.f.manager.stops,0);self.assertFalse(self.x.folder.exists())
    def test_measurement_expired_changed_scope_or_source_is_refused(self):
        original=deepcopy(self.measured)
        for field,value in (('measured_at',time.time()-1000),('source_pins',{}),('stores',{}),('docker_binary',{})):
            row=deepcopy(original);row[field]=value
            self.measurement.write_text(json.dumps(row))
            with self.assertRaises(e.setup.Failure):self.x.plan(self.measurement)
        self.assertEqual(self.w.f.manager.stops,0)
    def test_capture_real_pipes_core_lease_service_order_and_explicit_return(self):
        job,result=self.capture();self.assertEqual(result['result'],'passed',result)
        self.assertEqual(self.w.f.manager.starts,0);self.assertEqual(self.x.load(job)['phase'],'paused')
        self.assertEqual(self.w.f.store.control()['desired'],'paused');self.assertFalse(result['backup_ready'])
        calls=[c for c in self.gf.backend.calls if c[0] in ('stop','start')]
        self.assertEqual(calls,[('stop','ops-web'),('stop','database'),('start','database'),('start','ops-web')])
        returned=self.x.return_worker(job);self.assertEqual(returned['result'],'passed',returned)
        self.assertEqual(self.w.f.manager.starts,1)
        with self.assertRaises(Exception):self.x.return_worker(job)
        self.assertEqual(self.w.f.manager.starts,1)
    def test_failed_capture_preserves_pause_and_encrypted_partial_no_automatic_start(self):
        job=self.fail_capture();self.assertEqual(self.x.load(job)['phase'],'attention_required')
        self.assertEqual(self.w.f.manager.starts,0);self.assertFalse(any(c[0]=='start' for c in self.gf.backend.calls))
        self.assertTrue(self.x.generation.folder.exists());self.assertFalse((self.x.generation.folder/'receipt.json').exists())
    def test_recovery_is_readonly_then_same_ids_once_worker_stays_paused(self):
        job=self.fail_capture();before=self.w.bytes();calls=deepcopy(self.gf.backend.calls)
        public=self.x.recovery_plan(job)[0]
        self.assertEqual(public['services_to_start'],['database','ops-web']);self.assertEqual(before,self.w.bytes())
        self.assertEqual(calls,self.gf.backend.calls)
        result=self.x.recover(job,public['recovery_review_id']);self.assertEqual(result['result'],'passed',result)
        self.assertEqual(self.w.f.manager.starts,0);self.assertEqual(self.w.f.store.control()['desired'],'paused')
        self.assertEqual([c for c in self.gf.backend.calls if c[0]=='start'],[('start','database'),('start','ops-web')])
        self.assertIn('recovery_audit',self.x.load(job));self.assertFalse(result['operation_replayed'])
        self.gf.exporter.mode='good';self.assertEqual(self.x.return_worker(job)['result'],'passed')
    def test_unknown_service_start_never_resubmitted_can_observe_fresh_healthy(self):
        job=self.fail_capture();public=self.x.recovery_plan(job)[0];self.gf.backend.start_unknown=True
        with self.assertRaisesRegex(Exception,'START_OUTCOME_UNKNOWN'):self.x.recover(job,public['recovery_review_id'])
        self.gf.backend.start_unknown=False;public=self.x.recovery_plan(job)[0]
        self.assertEqual(public['services_to_start'],['ops-web'])
        self.assertEqual(self.x.recover(job,public['recovery_review_id'])['result'],'passed')
        self.assertEqual([c for c in self.gf.backend.calls if c==('start','database')],[('start','database')])
    def test_unknown_start_with_no_fresh_running_evidence_blocks_recovery(self):
        job=self.fail_capture();public=self.x.recovery_plan(job)[0]
        def fail(row):raise e.setup.Failure('COLD_START_OUTCOME_UNKNOWN')
        self.gf.backend.start=fail
        with self.assertRaises(Exception):self.x.recover(job,public['recovery_review_id'])
        with self.assertRaisesRegex(e.setup.Failure,'START_OUTCOME_UNKNOWN'):self.x.recovery_plan(job)
    def test_newer_manual_pause_protected_change_and_inactive_worker_guard(self):
        job=self.fail_capture();self.gf.backend.protected['foreign']='restarted'
        with self.assertRaises(Exception):self.x.recovery_plan(job)
        self.gf.backend.protected=deepcopy(self.gen['cold']['protected'])
        control=self.w.f.store.control();control['service_stop_token']='newer-manual-stop'
        self.x.r.ws.write(self.w.f.store.path/'control.json',control)
        with self.assertRaisesRegex(Exception,'CONTROL_CHANGED'):self.x.recovery_plan(job)
        self.assertEqual(self.w.f.store.control()['service_stop_token'],'newer-manual-stop');self.assertEqual(self.w.f.manager.starts,0)
    def test_ciphertext_modified_after_capture_blocks_worker_return(self):
        job,result=self.capture();self.assertEqual(result['result'],'passed',result)
        row=self.x.load(job);artifact=next(iter(row['trusted_receipt']['artifacts'].values()))
        (self.x.generation.folder/artifact['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(Exception,'CIPHERTEXT_CHANGED'):self.x.return_worker(job)
        self.assertEqual(self.w.f.manager.starts,0)
    def test_record_modified_nonprivate_symlink_or_hardlink_refused(self):
        job,_=self.capture();path=self.x.path(job);raw=path.read_bytes();path.chmod(0o664)
        with self.assertRaises(Exception):self.x.load(job)
        path.chmod(0o600);alias=path.with_suffix('.alias');os.link(path,alias)
        with self.assertRaises(Exception):self.x.load(job)
        alias.unlink();row=json.loads(raw);row['generation']['max_file_bytes']+=1;path.write_text(json.dumps(row))
        with self.assertRaisesRegex(e.setup.Failure,'REVIEW_CHANGED'):self.x.load(job)
    def test_stale_recovery_review_refuses_before_start_or_latch_clear(self):
        job=self.fail_capture();before=deepcopy(self.w.f.store.control())
        with self.assertRaisesRegex(e.setup.Failure,'PLAN_CHANGED'):self.x.recover(job,'0'*64)
        self.assertEqual(before,self.w.f.store.control());self.assertFalse(any(c[0]=='start' for c in self.gf.backend.calls))
    def test_unknown_original_capture_start_not_replayed(self):
        self.gf.backend.start_unknown=True;job,result=self.capture()
        self.assertEqual(result['result'],'attention_required',result)
        # Docker start happened, but the container later exited before proof.
        state=self.gf.backend.rows['database']['state'];state.update(Running=False,Pid=0,Status='exited')
        with self.assertRaises(Exception):self.x.recovery_plan(job)

    def measurement_backend(self,fail=False):
        backend=self.gf.backend
        backend.baseline=lambda:None
        def attach(ident,spec):
            code=m.script().replace("os.open('/source',",'os.open('+repr(spec['source'])+',')
            if fail:code='print("{}")'
            child=subprocess.Popen([sys.executable,'-I','-B','-c',code,json.dumps(spec['config'])],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL,
                pass_fds=(backend.scope.descriptor,),start_new_session=True)
            backend.helpers[ident]['child']=child;return child
        backend.attach=attach;return backend

    def assert_measure_blocked_before_any_effect(self):
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.x.control.rglob('*') if p.is_file()}
        with patch.object(self.x,'fresh',side_effect=AssertionError('fresh must not run')),\
             patch.object(m,'Docker',side_effect=AssertionError('helper backend must not be created')):
            with self.assertRaisesRegex(e.setup.Failure,'BACKUP_MEASURE_MAINTENANCE_REQUIRED'):
                self.x.measure(10)
        after={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.x.control.rglob('*') if p.is_file()}
        self.assertEqual(before,after);self.assertEqual(self.w.f.manager.stops,0)
        self.assertEqual(self.w.f.manager.starts,0)

    def test_measure_running_worker_is_blocked_before_helper_lock_or_record(self):
        self.assert_measure_blocked_before_any_effect()

    def test_measure_paused_worker_does_not_replace_reviewed_maintenance_scope(self):
        self.w.f.store.pause('OTHER_CONTAINER_CHANGED')
        self.assert_measure_blocked_before_any_effect()

    def test_native_binding_snapshot_checks_fresh_identity_while_ops_is_stopped(self):
        public,row=self.x.plan(self.measurement);self.x.save(row,'prepared')
        self.gf.backend.rows['ops-web']['state'].update(Running=False,Status='exited',Pid=0)
        owner=self.x.r.ws.read(self.x.directory/'owner.json');owner['project_root']=str(self.x.root)
        with patch.object(self.x.r.ws,'read',return_value=owner):
            result=e.Execute.paused_snapshot(self.x,self.x.directory,row)
        self.assertEqual(result['ops_container_id'],self.gen['cold']['services']['ops-web']['container_id'])
        self.assertEqual(result['code_sha256'],self.x.r.worker.code_hash(self.x.root))
        self.gf.backend.rows['ops-web']['image_id']='sha256:'+'9'*64
        with patch.object(self.x.r.ws,'read',return_value=owner):
            with self.assertRaisesRegex(e.setup.Failure,'IDENTITY_CHANGED'):
                e.Execute.paused_snapshot(self.x,self.x.directory,row)


class Contracts(unittest.TestCase):
    def test_cli_argument_failure_is_safe_json_without_traceback_or_secrets(self):
        out=io.StringIO()
        with patch('sys.stdout',out):code=e.main(['capture'])
        self.assertEqual(code,2);self.assertEqual(json.loads(out.getvalue())['code'],'BACKUP_MEASUREMENT_ARGUMENT_REQUIRED')
    def test_pins_include_new_adapter_but_preserve_existing_three_sources(self):
        self.assertEqual({k:e.source_pins(e.ROOT)[k] for k in e.PINS},e.PINS)
    def test_closed_scope_refused_before_guard(self):
        fd=os.open('/dev/null',os.O_RDONLY);os.close(fd)
        scope=m.Scope(fd,lambda:None)
        with self.assertRaises(OSError):scope.checkpoint()
    def test_real_mutual_exclusion_and_private_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'lock'
            with e.lock(path):
                with self.assertRaisesRegex(e.setup.Failure,'BUSY'):
                    with e.lock(path):pass
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_native_inspect_projects_stable_state_and_normalizes_mounts(self):
        expected=dict(container_id='a'*64,owner_label='test.owner')
        state=dict(Running=True,Status='running',Pid=123,StartedAt='old',FinishedAt='zero',ExitCode=0,OOMKilled=False,
                   Health={'Log':['volatile-readback']})
        raw=['a'*64,'sha256:'+'b'*64,{'test.owner':'owner','com.docker.compose.service':'ops-web'},
             [{'Source':'/z'},{'Source':'/a'}],state,'healthy','','no']
        backend=e.Docker(dict(endpoint='unix:///test',daemon_id='test',docker_root='/docker'),sys.executable,
                         runner=lambda *a:(0,json.dumps(raw).encode(),b''))
        row=backend.inspect(expected);self.assertNotIn('Health',row['state'])
        self.assertEqual(row['mounts'],[{'Source':'/a'},{'Source':'/z'}]);self.assertIn('FinishedAt',row['state'])


if __name__=='__main__':unittest.main()
