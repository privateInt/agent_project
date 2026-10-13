"""Runner copy, native stream guards and cleanup; manager/Docker synthetic."""
from copy import deepcopy
import itertools
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

from ops_tools import backup_generation_acceptance as b
from ops_tools.tests.test_maintenance_acceptance import MetadataEngine
from ops_candidates.backup_capture_20260930 import age_tool as a


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name);ports=itertools.count(25000)
        mocked=patch.object(b.a.isolated,'free_port',side_effect=lambda **kw:next(ports));mocked.start();self.addCleanup(mocked.stop)
    def create(self):return b.create_run(self.base,MetadataEngine())
    def deny(self,fn):
        with self.assertRaises((b.a.ReadFailure,a.Failure)):fn()
    def test_new_private_copy_preserves_active_closure_and_prior_candidate_pins(self):
        directory,record=self.create();self.assertEqual(b.validate(directory),record)
        self.assertEqual(b.a.worker.code_hash(directory/'project'),b.a.core.CODE)
        self.assertFalse((directory/'state').exists());self.assertFalse((directory/'generation-restores').exists())
        files=json.loads((directory/'generation-run.json').read_text())['files']
        self.assertEqual(len(files),10)
        for name in files:self.assertEqual((directory/'project'/name).stat().st_mode&0o777,0o600)
    def test_new_copy_imports_fresh_process_all_source_digests_checked(self):
        directory,_=self.create();p=directory/'project'
        code='import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from ops_tools import backup_generation_acceptance as b;assert len(b.sources(Path(sys.argv[1])))==10;print("ok")'
        result=subprocess.run([sys.executable,'-I','-B','-c',code,str(p)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'ok')
    def test_copied_writer_edit_refuses_invoke_and_cleanup_before_control(self):
        directory,_=self.create();path=directory/'project'/'ops_candidates/backup_restore_uid_20261001/writer.py';path.write_bytes(path.read_bytes()+b'\n# modified')
        with patch.object(b.subprocess,'Popen') as spawn,patch.object(b.a.native,'cleanup_unit_subprocess') as unit:
            self.deny(lambda:b.invoke(directory,self.base));self.deny(lambda:object.__new__(b.Engine).cleanup(directory))
        spawn.assert_not_called();unit.assert_not_called()
    def test_modified_marker_owner_or_source_scope_never_controls(self):
        directory,_=self.create();path=directory/'generation-run.json';old=json.loads(path.read_text())
        for name,value in (('owner','foreign'),('directory','/production'),('kind','different')):
            marker=deepcopy(old);marker[name]=value;b.a.isolated.write(path,marker);self.deny(lambda:b.validate(directory))
    def test_unknown_uid_helper_after_test_unit_exit_blocks_docker_cleanup(self):
        directory,_=self.create();engine=object.__new__(b.Engine);engine.ids=lambda *args:['b'*64]
        with patch.object(b.a.native,'cleanup_unit_subprocess',return_value={'worker_exit_verified':True}) as unit,patch.object(b.n.Engine,'cleanup') as docker:
            self.deny(lambda:engine.cleanup(directory))
        unit.assert_called_once();docker.assert_not_called()
    def test_unverified_unit_exit_blocks_uid_helper_and_container_cleanup(self):
        directory,_=self.create();engine=object.__new__(b.Engine);engine.ids=lambda *args:self.fail('must not list yet')
        with patch.object(b.a.native,'cleanup_unit_subprocess',return_value={'worker_exit_verified':False}),patch.object(b.n.Engine,'cleanup') as docker:
            self.deny(lambda:engine.cleanup(directory))
        docker.assert_not_called()
    def test_successful_unit_exit_and_helper_absence_allow_existing_scoped_cleanup(self):
        directory,_=self.create();engine=object.__new__(b.Engine);engine.ids=lambda *args:[]
        with patch.object(b.a.native,'cleanup_unit_subprocess',return_value={'worker_exit_verified':True}),patch.object(b.n.Engine,'cleanup',return_value={'result':'passed'}) as cleanup:
            self.assertEqual(engine.cleanup(directory),{'result':'passed'})
        cleanup.assert_called_once_with(directory)
    def test_missing_fetch_flag_and_preflight_failure_create_nothing(self):
        with patch.object(b,'Engine') as create:
            self.deny(lambda:b.run(self.base))
            with patch.object(b,'preflight',side_effect=b.a.ReadFailure('GATE')):self.deny(lambda:b.run(self.base,True))
        create.assert_not_called();self.assertEqual(list(self.base.iterdir()),[])
    def test_helper_audit_cannot_point_to_original_storage_or_foreign_image(self):
        directory,record=self.create();path=directory/'generation-run.json';marker=json.loads(path.read_text())
        source=directory/'generation-restores'/('restored-'+'a'*36+'.partial')/'storage-storage/data/storage'
        source.mkdir(mode=0o700,parents=True)
        spec=b.u.recipe(source,os.getuid(),os.getgid(),{'role':'storage'},record['image'],record['owner'],
                        (b.ROOT/'ops_candidates/backup_restore_uid_20261001/writer.py').read_text(),'extract')
        b.audit_helpers(directory,[spec]);self.assertEqual(b.validate(directory),record)
        for field,value in (('source',str(directory/'cold-source')),('image','sha256:'+'c'*64),('owner','foreign')):
            changed=deepcopy(spec);changed[field]=value;changed.pop('spec_sha256');changed['spec_sha256']=b.g.c.fingerprint(changed)
            marker=json.loads(path.read_text());marker['uid_helpers']=[changed];b.a.isolated.write(path,marker);self.deny(lambda:b.validate(directory))
    def test_helper_audit_cannot_discard_or_rewrite_earlier_recipe(self):
        directory,record=self.create();leaf=directory/'generation-restores'/('restored-'+'a'*36+'.partial')/'storage-storage/data/storage';leaf.mkdir(parents=True,mode=0o700)
        spec=b.u.recipe(leaf,os.getuid(),os.getgid(),{'role':'storage'},record['image'],record['owner'],
                        (b.ROOT/'ops_candidates/backup_restore_uid_20261001/writer.py').read_text(),'verify')
        b.audit_helpers(directory,[spec]);self.deny(lambda:b.audit_helpers(directory,[]))
    def test_failure_report_excludes_raw_stderr_private_paths_keys(self):
        exc=b.a.ReadFailure('SAFE_CODE');exc.failure_phase='restore';exc.private='/secret';exc.stderr='AGE-SECRET-KEY-1secret'
        value=b.error_report(exc);self.assertEqual(value['code'],'SAFE_CODE');self.assertEqual(value['failure_phase'],'restore')
        self.assertNotIn('secret',json.dumps(value).lower())


class Guards(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);path=Path(self.temp.name)
        self.fd=os.open(path,os.O_RDONLY);self.addCleanup(os.close,self.fd);self.control={'desired':'paused','service_stop_token':'x'}
        self.record={'id':'job','paused_control':deepcopy(self.control)};self.calls=[]
        self.parent=SimpleNamespace(store=SimpleNamespace(control=lambda:deepcopy(self.control)),load=lambda *args:deepcopy(self.record))
        self.original=SimpleNamespace(active=True,descriptor=self.fd,parent=self.parent,record=deepcopy(self.record),checkpoint=lambda:self.calls.append('full'))
        self.scope=b.StreamScope(self.original)
    def test_stream_checks_local_pause_each_block_and_full_metadata_at_boundaries(self):
        self.scope.checkpoint();self.scope.checkpoint();self.assertEqual(self.calls,['full'])
        self.scope.checkpoint(True);self.assertEqual(self.calls,['full','full'])
        self.control['desired']='running'
        with self.assertRaises(b.a.ReadFailure):self.scope.checkpoint()
    def test_changed_journal_or_closed_scope_never_uses_cached_full_check(self):
        self.scope.checkpoint();self.record['id']='changed'
        with self.assertRaises(b.a.ReadFailure):self.scope.checkpoint()
        self.record['id']='job';self.original.active=False
        with self.assertRaises(b.a.ReadFailure):self.scope.checkpoint()
    def test_inventory_excludes_only_verified_live_registered_helpers(self):
        row={'container_id':'a'*64,'image_id':'sha256:'+'b'*64,'owner':'o','owner_label':'owner','project':'p','service':'db','mounts':[],'userns':'','restart':{'Name':'no'}}
        p={'services':{'db':row},'protected':{'p'*64:'started'}}
        seen=[];backend=SimpleNamespace(verify_host=lambda:None,inspect=lambda r:deepcopy(r),running=lambda:{'a'*64:'x','p'*64:'started','h'*64:'helper'},helper=lambda *args:None)
        gen=SimpleNamespace(backend=backend,p={'cold':p},op=None)
        helper=SimpleNamespace(owned={'h'*64:{'spec':'live'}},inspect=lambda ident,spec:seen.append(ident))
        self.assertEqual(b.inventory(gen,helper)['protected'],p['protected']);self.assertEqual(seen,['h'*64])
        helper.owned={}
        with self.assertRaises((a.Failure,b.a.ReadFailure)):b.inventory(gen,helper)
    def test_new_protected_container_or_started_time_change_refuses_return(self):
        row={'container_id':'a'*64,'image_id':'sha256:'+'b'*64,'owner':'o','owner_label':'owner','project':'p','service':'db','mounts':[],'userns':'','restart':{'Name':'no'}}
        backend=SimpleNamespace(verify_host=lambda:None,inspect=lambda r:deepcopy(r),running=lambda:{'p'*64:'changed'})
        gen=SimpleNamespace(backend=backend,p={'cold':{'services':{'db':row},'protected':{'p'*64:'started'}}},op=None)
        with self.assertRaises((a.Failure,b.a.ReadFailure)):b.inventory(gen)
    def test_image_component_and_commit_force_full_checks_even_during_fast_stream(self):
        gen=object.__new__(b.NativeGeneration);gen.scope=self.scope;gen.last_full=123
        with patch.object(b.g.Generation,'encrypt',return_value='encrypted') as encrypt,patch.object(gen,'guard') as guard:
            self.assertEqual(gen.encrypt('image-id',object()),'encrypted')
        self.assertEqual(self.calls,['full','full']);guard.assert_called_once();encrypt.assert_called_once()
        self.calls=[]
        with patch.object(b.g.Generation,'__call__',side_effect=lambda scope:gen.hook('before_commit')),patch.object(gen,'guard') as guard:
            gen(self.scope)
        self.assertEqual(self.calls,['full']);guard.assert_called_once_with(False)

class NativeCLI(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('PREFLIGHT_DOCKER_TEST_BINARY'):self.skipTest('optional native Docker CLI unavailable')
        from ops_tools.tests.test_cold_storage_acceptance import NativeCLI as Fixture
        self.f=Fixture('test_real_cli_serializes_reviewed_reader_create_recipe');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.root/'restore';self.root.mkdir(mode=0o700)
    def spec(self,mode):return b.u.recipe(self.root,12345,os.getgid(),{'role':'storage'},self.f.image,'synthetic-run',
                                         (b.ROOT/'ops_candidates/backup_restore_uid_20261001/writer.py').read_text(),mode)
    def test_actual_cli_serializes_root_initializer_capabilities_only_on_fresh_leaf(self):
        result=self.f.invoke(b.u.command(self.spec('init')));self.assertEqual(result.returncode,0,result.stderr)
        row=self.f.requests[0];host=row['HostConfig'];self.assertEqual(row['User'],'0:0')
        self.assertEqual({x.removeprefix('CAP_') for x in host['CapAdd']},{'CHOWN','DAC_OVERRIDE'});self.assertEqual(host['CapDrop'],['ALL'])
        self.assertEqual(host['NetworkMode'],'none');self.assertTrue(host['ReadonlyRootfs'])
        self.assertEqual(len(host['Mounts']),1);self.assertEqual(host['Mounts'][0]['Source'],str(self.root))
        self.assertEqual(host['Mounts'][0]['Target'],'/restore');self.assertTrue(host['Mounts'][0]['BindOptions']['NonRecursive'])
        from ops_candidates.backup_restore_uid_20261001.tests.test_uid_restore import Contracts
        case=Contracts();case.setUp();self.addCleanup(case.doCleanups)
        spec=self.spec('init');value=case.row(spec);value['Id']=self.f.ident;value['Config']['User']='0:0';value['Config']['OpenStdin']=False
        value['HostConfig']['CapAdd']=host['CapAdd'];self.f.value=value
        helper=b.u.UIDHelper(self.f.backend(),case.scope,self.f.image,'synthetic-run',spec['script'],lambda:None)
        helper.owned[self.f.ident]=spec;self.assertEqual(helper.inspect(self.f.ident,spec)['Pid'],0)
    def test_actual_cli_writer_uid_pipe_and_readonly_verifier_bind(self):
        for mode,readonly in (('extract',False),('verify',True)):
            self.f.requests.clear();result=self.f.invoke(b.u.command(self.spec(mode)));self.assertEqual(result.returncode,0,result.stderr)
            row=self.f.requests[0];host=row['HostConfig'];self.assertEqual(row['User'],f'12345:{os.getgid()}')
            self.assertTrue(row['OpenStdin']);self.assertEqual(host['Mounts'][0].get('ReadOnly',False),readonly)
            self.assertFalse(host.get('CapAdd'));self.assertEqual(host['LogConfig']['Type'],'none');self.assertEqual(row['Healthcheck']['Test'],['NONE'])
    def test_actual_cli_full_own_helper_projection_checks_recipe_before_control(self):
        spec=self.spec('extract');from ops_candidates.backup_restore_uid_20261001.tests.test_uid_restore import Contracts
        case=Contracts();case.setUp();self.addCleanup(case.doCleanups);row=case.row(spec);row['Id']=self.f.ident
        self.f.value=row;backend=self.f.backend();scope=SimpleNamespace(active=True,descriptor=case.scope.descriptor,checkpoint=lambda:None)
        helper=b.u.UIDHelper(backend,scope,self.f.image,'synthetic-run',spec['script'],lambda:None);helper.owned[self.f.ident]=spec
        self.assertEqual(helper.inspect(self.f.ident,spec)['Pid'],0)
        self.f.value['HostConfig']['NetworkMode']='host'
        with self.assertRaises(a.Failure):helper.inspect(self.f.ident,spec)
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.f.calls))


from ops_candidates.backup_generation_20261001.tests.test_generation import NativeAgeTests


class NativeReceipt(NativeAgeTests):
    """The actual native wrapper, live receipt and restore under a real lease.

    Manager/Docker are synthetic. The UID writer runs as actor UID because
    this executor has no foreign UID namespace; no native daemon claim.
    """
    fail=unittest.TestCase.fail
    def setUp(self):
        super().setUp()
        from ops_candidates.backup_restore_20261001.tests.test_restore import image_fixture,Exporter
        self.ident,raw=image_fixture()
        for row in self.rows.values():row['image_id']=self.ident
        self.helper['image_id']=self.ident;self.images=[self.ident];self.proposal=self.plan()
        self.backend.rows=deepcopy(self.rows);self.backend.owned_helpers=self.backend.helpers
        self.exporter=Exporter(self.ident,raw)
        self.gen=b.NativeGeneration(b.g.plan(self.proposal,self.files,self.images),self.backend,self.codec,self.exporter)
        self.prior,self.ctl=self.controller(self.gen)

    def callback(self,operation):
        def run(original):
            scope=b.StreamScope(original);self.gen(scope);scope.checkpoint(True)
            operation(scope)
            return {'result':'passed','child_processes_stopped':True}
        return self.ctl.run(self.ctl.plan(wait_seconds=1),run)

    def rejected_receipt(self,edit):
        def operation(scope):
            edit()
            with patch.object(b.r,'plan',side_effect=AssertionError('restore must not begin')):
                b.captured_receipt(self.gen)
        result=self.callback(operation)
        self.assertEqual(result['result'],'attention_required',result)
        self.prior.assert_paused();self.assertEqual(self.prior.manager.starts,0)
        self.assertFalse(list(self.target.iterdir()));self.assertFalse(self.backend.helpers)

    def test_native_wrapper_receipt_original_review_and_filesystem_return_path(self):
        from ops_candidates.backup_restore_uid_20261001.tests.test_uid_restore import HostHelper
        box={};leases=[];spawn=self.codec.spawn
        def checked(args,**kwargs):
            leases.append(kwargs.get('lease',()));return spawn(args,**kwargs)
        def operation(scope):
            trust=b.captured_receipt(self.gen)
            self.assertNotIn('review_id',self.gen.p)
            self.assertEqual(trust['review_id'],self.gen.original['review_id'])
            helper=HostHelper(scope,self.ident);helper.guard=scope.checkpoint
            class Restore(b.u.Restore):
                def tree(inner,role,*args):
                    if not role.startswith('storage-'):return super().tree(role,*args)
                    proxy=SimpleNamespace(**{name:getattr(os,name) for name in dir(os)})
                    proxy.getuid=lambda:os.getuid()+1
                    with patch.object(b.u,'os',proxy):return super().tree(role,*args)
            restored=Restore(b.r.plan(self.gen.folder,trust,self.target,self.identity),
                             self.codec,helper,self.gen.p['cold']['stores']).run()
            scope.checkpoint(True);helper.empty()
            self.assertEqual(helper.calls,['init','extract','verify'])
            self.assertTrue(restored['crypto_eof_authenticated'])
            self.assertTrue(restored['foreign_uid_materialization'])
            self.assertTrue(restored['foreign_uid_readonly_verification'])
            self.assertEqual(len(restored['image_archives']),1)
            folder=Path(restored['directory'])
            self.assertEqual((folder/'storage-data/data/data/record').read_bytes(),(self.src/'record').read_bytes())
            self.assertEqual((folder/'files/data/config/settings').read_bytes(),(self.config/'settings').read_bytes())
            self.assertFalse(restored['backup_ready']);box.update(restored=restored,fd=scope.descriptor)
        with patch.object(self.codec,'spawn',side_effect=checked):result=self.callback(operation)
        self.assertEqual(result['result'],'passed',result);self.prior.assert_paused()
        self.assertTrue(leases and all(box['fd'] in row for row in leases))
        self.assertEqual((self.prior.manager.stops,self.prior.manager.starts),(1,0))
        self.assertEqual(self.ctl.load(result['job_id'])['phase'],'ready_to_return')
        returned=self.ctl.resume(result['job_id']);self.assertTrue(returned['fresh_evaluation_verified'])
        self.assertEqual(self.prior.manager.starts,1)

    def test_changed_durable_review_even_with_updated_record_cannot_authorize_restore(self):
        def edit():
            trust=b.r.read_json(self.gen.folder/'receipt.json');trust['review_id']='0'*64
            b.g.c.atomic_json(self.gen.folder,'receipt.json',trust,(self.gen.folder/'receipt.json').read_bytes())
            self.gen.record['receipt_sha256']=b.g.c.fingerprint(trust)
        self.rejected_receipt(edit)

    def test_changed_durable_artifact_set_even_with_updated_record_refused(self):
        def edit():
            trust=b.r.read_json(self.gen.folder/'receipt.json');trust['artifacts'].pop('files')
            b.g.c.atomic_json(self.gen.folder,'receipt.json',trust,(self.gen.folder/'receipt.json').read_bytes())
            self.gen.record['receipt_sha256']=b.g.c.fingerprint(trust)
        self.rejected_receipt(edit)

    def test_ciphertext_edit_after_capture_blocks_restore_and_worker_return(self):
        def edit():
            path=self.gen.folder/'files.age';path.write_bytes(path.read_bytes()[:-1])
        self.rejected_receipt(edit)

    def test_changed_live_plan_body_cannot_reuse_original_receipt(self):
        self.rejected_receipt(lambda:self.gen.p.update(max_file_bytes=self.gen.p['max_file_bytes']+1))


class Diagnostics(Boundaries):
    def test_callback_failure_projection_has_only_enumerated_metadata(self):
        secret='AGE-SECRET-KEY-1private /private/source'
        exc=KeyError(secret);exc.stderr=secret
        diagnostic=b.callback_diagnostic(exc,'receipt',SimpleNamespace(record={'phase':'captured'}))
        self.assertEqual(diagnostic,{'step':'receipt','exception_type':'KeyError','generation_phase':'captured','code':'INVALID_LOCAL_STATE'})
        self.assertEqual(b.callback_diagnostic(a.Failure('COLD_SOURCE_CHANGED'),'capture',SimpleNamespace(record=None))['code'],'COLD_SOURCE_CHANGED')
        wrapped=b.a.ReadFailure('INVALID_LOCAL_STATE');wrapped.callback_diagnostic=diagnostic
        self.assertEqual(b.error_report(wrapped)['callback_diagnostic'],diagnostic)
        self.assertNotIn('private',json.dumps(b.error_report(wrapped)))

    def test_malformed_diagnostic_is_dropped_without_leaking_free_text(self):
        valid={'step':'receipt','exception_type':'KeyError','generation_phase':'captured','code':'INVALID_LOCAL_STATE'}
        invalid=[dict(valid,stderr='/secret'),dict(valid,step='/secret'),dict(valid,exception_type='SecretError'),
                 dict(valid,generation_phase=['secret']),dict(valid,code='/secret'),None,'secret']
        for value in invalid:
            exc=b.a.ReadFailure('INVALID_LOCAL_STATE');exc.callback_diagnostic=value
            self.assertNotIn('callback_diagnostic',b.error_report(exc))
        class SecretError(Exception):pass
        self.assertEqual(b.callback_diagnostic(SecretError('/secret'),'restore',SimpleNamespace(record=None)),
                         {'step':'restore','exception_type':'Other','generation_phase':'unavailable','code':'INVALID_LOCAL_STATE'})
        self.assertEqual(b.callback_diagnostic(KeyError('/secret'),'receipt',SimpleNamespace(record={'phase':[]}))['generation_phase'],
                         'unavailable')
        exc=b.a.ReadFailure('INVALID_LOCAL_STATE');exc.failure_phase=[]
        self.assertNotIn('failure_phase',b.error_report(exc))

    def test_safe_callback_diagnostic_survives_child_report_projection(self):
        directory,_=self.create()
        expected={'step':'receipt','exception_type':'KeyError','generation_phase':'captured','code':'INVALID_LOCAL_STATE'}
        report={'schema':1,'stage':b.STAGE,'result':'attention_required','code':'INVALID_LOCAL_STATE',
                'failure_phase':'capture','callback_diagnostic':expected,'stderr':'/private/secret'}
        def child(*args,**kw):
            json.dump(report,kw['stdout']);kw['stdout'].flush()
            return SimpleNamespace(wait=lambda **kwargs:2)
        with patch.object(b.subprocess,'Popen',side_effect=child):
            with self.assertRaises(b.a.ReadFailure) as caught:b.invoke(directory,self.base)
        value=b.error_report(caught.exception)
        self.assertEqual(value['callback_diagnostic'],expected);self.assertEqual(value['failure_phase'],'capture')
        self.assertNotIn('secret',json.dumps(value))
