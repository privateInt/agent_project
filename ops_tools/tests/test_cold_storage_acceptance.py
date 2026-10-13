"""New runner boundaries and native host crypto; Docker/systemd are synthetic here."""
from copy import deepcopy
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from ops_tools import cold_storage_acceptance as n
from ops_tools.tests.test_maintenance_acceptance import MetadataEngine
from ops_candidates.cold_storage_20260930 import reader
from ops_platform.contracts import ReadFailure


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.ports=itertools.count(24000)
        mocked=patch.object(n.a.isolated,'free_port',side_effect=lambda **kw:next(self.ports));mocked.start();self.addCleanup(mocked.stop)
    def create(self):return n.create_run(self.base,MetadataEngine())
    def deny(self,fn):
        with self.assertRaises((ReadFailure,n.age_tool.Failure)):fn()

    def test_private_copy_preserves_runtime_and_all_pinned_candidates(self):
        directory,record=self.create();self.assertEqual(n.validate(directory),record)
        self.assertEqual(n.a.worker.code_hash(directory/'project'),n.a.core.CODE)
        files=json.loads((directory/'cold-run.json').read_text())['files']
        self.assertEqual(len(files),15)
        for rel in files:self.assertEqual((directory/'project'/rel).stat().st_mode&0o777,0o600)
        self.assertFalse((directory/'state').exists());self.assertFalse((directory/'keys/test-identity').exists())

    def test_source_copy_imports_in_a_fresh_real_process(self):
        directory,_=self.create();p=directory/'project'
        code='import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from ops_tools import cold_storage_acceptance as n;assert len(n.sources(Path(sys.argv[1])))==15;print("ok")'
        value=subprocess.run([sys.executable,'-I','-B','-c',code,str(p)],capture_output=True,text=True,timeout=10)
        self.assertEqual(value.returncode,0,value.stderr);self.assertEqual(value.stdout.strip(),'ok')

    def test_added_services_need_no_new_network_no_ports_or_pull(self):
        directory,record=self.create();marker=json.loads((directory/'cold-run.json').read_text())
        cfg=json.loads((directory/'project'/n.f.REL).read_text())['services']
        self.assertEqual(len(cfg),4)
        for role in (*n.f.ROLES,n.f.SETUP):
            row=cfg[role];self.assertEqual(row['network_mode'],'none');self.assertEqual(row['image'],record['image'])
            self.assertEqual(row['pull_policy'],'never');self.assertNotIn('ports',row);self.assertNotIn('build',row)
            self.assertTrue(row['read_only']);self.assertEqual(row['logging'],{'driver':'none'})
        self.assertNotEqual(marker['fixture']['uid'],os.getuid())
        self.assertEqual(cfg['cold-db']['cap_drop'],['ALL']);self.assertNotIn('cap_add',cfg['cold-db'])

    def test_root_initializer_has_only_one_new_run_bind_and_limited_capabilities(self):
        directory,_=self.create();cfg=json.loads((directory/'project'/n.f.REL).read_text())['services'][n.f.SETUP]
        self.assertEqual(cfg['user'],'0:0');self.assertEqual(cfg['volumes'][0]['source'],str(directory/'cold-source'))
        self.assertEqual(len(cfg['volumes']),1);self.assertEqual(cfg['volumes'][0]['target'],'/data')
        self.assertEqual(cfg['cap_add'],['CHOWN','DAC_OVERRIDE','FOWNER']);self.assertNotIn('privileged',cfg)
        compile(n.f.SETUP_CODE,'fixture','exec');compile(n.f.LOOP,'fixture','exec')

    def test_marker_owner_foreign_source_or_actor_uid_never_authorizes_mutation(self):
        directory,_=self.create();path=directory/'cold-run.json';original=json.loads(path.read_text())
        for field,value in (('owner','foreign'),('source','/home/foreign'),('uid',os.getuid())):
            changed=deepcopy(original)
            if field=='owner':changed[field]=value
            else:changed['fixture'][field]=value
            n.a.isolated.write(path,changed);self.deny(lambda:n.validate(directory))
        n.a.isolated.write(path,original)

    def test_source_symlink_or_inode_replacement_refuses_before_control(self):
        directory,_=self.create();folder=directory/'cold-source';folder.rmdir();folder.symlink_to(self.base)
        self.deny(lambda:n.validate(directory));folder.unlink();folder.mkdir(mode=0o700)
        # Inode reuse is possible; changing mode is still a distinct identity.
        folder.chmod(0o755);self.deny(lambda:n.validate(directory))

    def test_recipe_edit_is_refused_even_when_compose_digest_is_updated(self):
        directory,record=self.create();path=directory/'project'/n.f.REL;spec=json.loads(path.read_text())
        spec['services']['cold-db']['volumes'][0]['source']='/production'
        n.a.isolated.write(path,spec);record['compose'][n.f.REL]['sha256']=n.a.digest(path.read_bytes());n.a.isolated.write(directory/'run.json',record)
        self.deny(lambda:n.validate(directory))

    def test_upstream_base_edit_is_not_adopted_by_marker_and_record(self):
        directory,record=self.create();path=directory/'project'/n.f.REL;spec=json.loads(path.read_text())
        role=n.a.isolated.ROLES['langfuse'][0];spec['services'][role]['command']=['foreign']
        marker=json.loads((directory/'cold-run.json').read_text());marker['original_langfuse_spec']['services'][role]['command']=['foreign']
        n.a.isolated.write(directory/'cold-run.json',marker);n.a.isolated.write(path,spec)
        record['compose'][n.f.REL]['sha256']=n.a.digest(path.read_bytes());n.a.isolated.write(directory/'run.json',record)
        self.deny(lambda:n.validate(directory))

    def test_copy_edit_hardlink_symlink_or_added_runtime_blocks_invoke_and_cleanup(self):
        directory,_=self.create();path=directory/'project'/n.ENTRY;raw=path.read_bytes()
        path.write_bytes(raw+b'\n# changed')
        with patch.object(n.subprocess,'Popen') as spawn,patch.object(n.a.native,'cleanup_unit_subprocess') as unit:
            self.deny(lambda:n.invoke(directory,self.base));self.deny(lambda:object.__new__(n.Engine).cleanup(directory))
        spawn.assert_not_called();unit.assert_not_called();path.write_bytes(raw)
        path.unlink();path.symlink_to(n.ROOT/n.ENTRY);self.deny(lambda:n.validate(directory));path.unlink();path.write_bytes(raw);path.chmod(0o600)
        os.link(path,directory/'alias');self.deny(lambda:n.validate(directory))

    def test_foreign_job_or_duplicate_job_is_rejected_without_replay(self):
        directory,_=self.create();path=directory/'cold-run.json';marker=json.loads(path.read_text())
        marker['operations']=['not-uuid'];n.a.isolated.write(path,marker);self.deny(lambda:n.validate(directory))
        marker['operations']=[str(uuid.uuid4())]*2;n.a.isolated.write(path,marker);self.deny(lambda:n.validate(directory))

    def test_lost_operation_index_cannot_hide_saved_reader_job_during_cleanup(self):
        directory,_=self.create();job=str(uuid.uuid4())
        n.a.isolated.write(directory/('cold-plan-'+job+'.json'),{'id':job})
        with patch.object(n.a.native,'cleanup_unit_subprocess') as unit:
            self.deny(lambda:object.__new__(n.Engine).cleanup(directory))
        unit.assert_not_called()

    def test_missing_fetch_flag_or_preflight_failure_creates_nothing(self):
        with patch.object(n,'Engine') as engine:
            self.deny(lambda:n.run(self.base))
            with patch.object(n,'preflight',side_effect=ReadFailure('GATE')):self.deny(lambda:n.run(self.base,True))
        engine.assert_not_called();self.assertEqual(list(self.base.iterdir()),[])

    def test_root_initializer_refuses_unknown_daemon_before_any_compose(self):
        directory,_=self.create();engine=object.__new__(n.Engine)
        with patch.object(n,'backend_for',side_effect=ReadFailure('COLD_NATIVE_ROOTFUL_LOCAL_REQUIRED')),patch.object(engine,'compose') as create:
            self.deny(lambda:engine.initialize_storage(directory))
        create.assert_not_called()

    def test_declared_image_volume_is_refused_before_root_fixture(self):
        directory,record=self.create();engine=object.__new__(n.Engine)
        backend=SimpleNamespace(verify_host=lambda:None,query=lambda *args:[record['image'],{'/foreign':{}}])
        with patch.object(n,'backend_for',return_value=(backend,{'docker_root':'/synthetic/root'})),patch.object(engine,'compose') as create:
            self.deny(lambda:engine.initialize_storage(directory))
        create.assert_not_called()

    def test_additional_compose_roles_require_exact_recipe_before_cli(self):
        directory,_=self.create();engine=object.__new__(n.Engine)
        with patch.object(engine,'verify_fixture_network') as net,patch.object(engine,'start_healthless_fixture') as compose:
            engine.compose(directory,n.f.REL,'cold-db');compose.assert_called_once_with(directory,'cold-db')
            self.deny(lambda:engine.compose(directory,n.f.REL,'cold-db',True))
        self.assertEqual(net.call_count,1)

    def waiting_engine(self,directory,record,role=n.f.SETUP):
        engine=object.__new__(n.Engine);ident='a'*64
        engine.owned=lambda *args:[ident]
        engine.ids=lambda *args:[ident]
        engine.metadata=lambda *args:{'identity':{'project':record['compose'][n.f.REL]['project'],
            'service':role,'running':True}}
        state={'Status':'running','Running':True,'Pid':8,'ExitCode':0,'OOMKilled':False,'Error':''}
        return engine,state

    def test_healthless_roles_up_once_without_compose_wait_then_require_state(self):
        directory,record=self.create()
        for role in (*n.f.ROLES,n.f.SETUP):
            engine,state=self.waiting_engine(directory,record,role);calls=[]
            def command(args,timeout=30):
                calls.append((args,timeout))
                return '' if args[0]=='compose' else json.dumps([state,{'Test':['NONE']}])
            engine.command=command;engine.start_healthless_fixture(directory,role)
            up=[(args,timeout) for args,timeout in calls if args[0]=='compose']
            self.assertEqual(len(up),1);args,timeout=up[0]
            self.assertIn('--no-build',args);self.assertIn('--no-recreate',args)
            self.assertEqual(args[args.index('--pull')+1],'never');self.assertEqual(args[-1],role)
            self.assertNotIn('--wait',args);self.assertNotIn('--wait-timeout',args)
            self.assertNotIn('--force-recreate',args);self.assertEqual(timeout,60)
            self.assertIsNone(engine._fixture_start_until)

    def test_ordinary_upstreams_keep_inherited_compose_wait(self):
        directory,record=self.create();engine=object.__new__(n.Engine);role=n.a.isolated.ROLES['langfuse'][0]
        engine.ids=lambda *args:[];calls=[];engine.command=lambda args,timeout=30:(calls.append(args) or '')
        with patch.object(engine,'verify_fixture_network'):
            engine.compose(directory,n.f.REL,role)
        self.assertEqual(len(calls),1);self.assertIn('--wait',calls[0]);self.assertIn('--wait-timeout',calls[0])

    def test_failed_or_unknown_up_never_accepts_running_state_or_retries(self):
        directory,record=self.create()
        for code in ('ACCEPTANCE_DOCKER_COMMAND_FAILED','ACCEPTANCE_COMMAND_UNCERTAIN'):
            engine,_=self.waiting_engine(directory,record)
            with patch.object(engine,'command',side_effect=ReadFailure(code)) as command,patch.object(engine,'wait_healthless_fixture') as wait:
                with self.assertRaises(ReadFailure) as caught:engine.start_healthless_fixture(directory,n.f.SETUP)
            self.assertEqual(caught.exception.code,code);self.assertEqual(command.call_count,1);wait.assert_not_called()

    def test_project_collision_prevents_healthless_up_submission(self):
        directory,record=self.create();engine,_=self.waiting_engine(directory,record)
        engine.ids=lambda *args:['b'*64]
        with patch.object(engine,'command') as command:self.deny(lambda:engine.start_healthless_fixture(directory,n.f.SETUP))
        command.assert_not_called()

    def test_modified_healthless_recipe_refused_before_any_docker_request(self):
        directory,record=self.create();engine,_=self.waiting_engine(directory,record)
        path=directory/'project'/n.f.REL;spec=json.loads(path.read_text());spec['services'][n.f.SETUP]['healthcheck']={}
        n.a.isolated.write(path,spec);record['compose'][n.f.REL]['sha256']=n.a.digest(path.read_bytes());n.a.isolated.write(directory/'run.json',record)
        with patch.object(engine,'command') as command:self.deny(lambda:engine.start_healthless_fixture(directory,n.f.SETUP))
        command.assert_not_called()

    def test_created_fixture_is_observed_until_same_container_runs(self):
        directory,record=self.create();engine,state=self.waiting_engine(directory,record);states=[]
        created=dict(state,Status='created',Running=False,Pid=0)
        def read(*args):
            result=created if not states else state;states.append(result);return [result,{'Test':['NONE']}]
        engine.fixture_start_state=read
        engine.metadata=lambda *args:{'identity':{'project':record['compose'][n.f.REL]['project'],
            'service':n.f.SETUP,'running':bool(states)}}
        with patch.object(n.time,'sleep'):engine.wait_healthless_fixture(directory,record,n.f.SETUP)
        self.assertEqual(states,[created,state]);self.assertIsNone(engine._fixture_start_until)

    def test_exited_oom_error_restarting_paused_and_bad_pid_are_not_ready(self):
        directory,record=self.create()
        changes=({'Status':'exited','Running':False,'Pid':0},{'OOMKilled':True},
            {'Error':'SECRET OCI error'},{'Status':'restarting'},{'Status':'paused'},{'Pid':0},{'Pid':True},
            {'Health':{'Status':'healthy'}},{'ExitCode':1},{'Running':1})
        for delta in changes:
            engine,state=self.waiting_engine(directory,record);state.update(delta)
            engine.metadata=lambda *args:{'identity':{'project':record['compose'][n.f.REL]['project'],
                'service':n.f.SETUP,'running':state['Running']}}
            engine.fixture_start_state=lambda *args:[state,{'Test':['NONE']}]
            with patch.object(n.time,'sleep') as sleep:
                with self.assertRaises(ReadFailure) as caught:engine.wait_healthless_fixture(directory,record,n.f.SETUP)
            sleep.assert_not_called();self.assertNotIn('SECRET',json.dumps(n.error_report(caught.exception)))
            self.assertIsNone(engine._fixture_start_until)

    def test_missing_or_enabled_healthcheck_cannot_pass_disabled_state_gate(self):
        directory,record=self.create()
        for health in (None,{}, {'Test':['CMD','true']}, {'Test':'NONE'}):
            engine,state=self.waiting_engine(directory,record);engine.fixture_start_state=lambda *args:[state,health]
            with self.assertRaises(ReadFailure) as caught:engine.wait_healthless_fixture(directory,record,n.f.SETUP)
            self.assertEqual(caught.exception.code,'COLD_NATIVE_FIXTURE_HEALTHCHECK_CHANGED')

    def test_replacement_or_ambiguous_fixture_never_passes_running_gate(self):
        directory,record=self.create();engine,state=self.waiting_engine(directory,record)
        engine.owned=lambda *args:['a'*64,'b'*64]
        with patch.object(engine,'fixture_start_state') as read:self.deny(lambda:engine.wait_healthless_fixture(directory,record,n.f.SETUP))
        read.assert_not_called()
        engine,state=self.waiting_engine(directory,record);observations=[]
        engine.owned=lambda *args:['b'*64 if observations else 'a'*64]
        engine.metadata=lambda *args:{'identity':{'project':record['compose'][n.f.REL]['project'],
            'service':n.f.SETUP,'running':False}}
        engine.fixture_start_state=lambda *args:(observations.append(1) or [dict(state,Status='created',Running=False,Pid=0),{'Test':['NONE']}])
        with patch.object(n.time,'sleep'):
            with self.assertRaises(ReadFailure) as caught:engine.wait_healthless_fixture(directory,record,n.f.SETUP)
        self.assertEqual(caught.exception.code,'COLD_NATIVE_FIXTURE_REPLACED');self.assertEqual(observations,[1])

    def test_created_fixture_deadline_observes_without_another_up(self):
        directory,record=self.create();engine,state=self.waiting_engine(directory,record);clock=[0.0]
        engine.metadata=lambda *args:{'identity':{'project':record['compose'][n.f.REL]['project'],
            'service':n.f.SETUP,'running':False}}
        engine.fixture_start_state=lambda *args:[dict(state,Status='created',Running=False,Pid=0),{'Test':['NONE']}]
        with patch.object(n.time,'monotonic',side_effect=lambda:clock[0]),patch.object(n.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)),patch.object(engine,'command') as control:
            with self.assertRaises(ReadFailure) as caught:engine.wait_healthless_fixture(directory,record,n.f.SETUP)
        self.assertEqual(caught.exception.code,'COLD_NATIVE_FIXTURE_START_TIMEOUT');self.assertEqual(clock[0],45)
        control.assert_not_called();self.assertIsNone(engine._fixture_start_until)

    def test_final_owner_loss_cannot_publish_ready_fixture(self):
        directory,record=self.create();engine,state=self.waiting_engine(directory,record)
        engine.owned=iter([['a'*64],[]]).__next__
        engine.owned=lambda *args,next_ids=engine.owned:next_ids()
        engine.fixture_start_state=lambda *args:[state,{'Test':['NONE']}]
        with self.assertRaises(ReadFailure) as caught:engine.wait_healthless_fixture(directory,record,n.f.SETUP)
        self.assertEqual(caught.exception.code,'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')

    def test_running_transition_between_metadata_reads_requires_final_running_proof(self):
        directory,record=self.create();engine,state=self.waiting_engine(directory,record);observed=[]
        def metadata(*args):
            running=bool(observed);observed.append(1)
            return {'identity':{'project':record['compose'][n.f.REL]['project'],'service':n.f.SETUP,'running':running}}
        engine.metadata=metadata;engine.fixture_start_state=lambda *args:[state,{'Test':['NONE']}]
        engine.wait_healthless_fixture(directory,record,n.f.SETUP)
        self.assertEqual(len(observed),2)
        engine.metadata=lambda *args:{'identity':{'project':record['compose'][n.f.REL]['project'],
            'service':n.f.SETUP,'running':False}}
        with self.assertRaises(ReadFailure) as caught:engine.wait_healthless_fixture(directory,record,n.f.SETUP)
        self.assertEqual(caught.exception.code,'COLD_NATIVE_FIXTURE_START_FAILED')

    def test_run_record_edit_during_state_observation_is_not_adopted(self):
        directory,record=self.create();engine,state=self.waiting_engine(directory,record)
        def read(*args):
            changed=deepcopy(record);changed['protected']={'c'*64:{'id':'c'*64}}
            n.a.isolated.write(directory/'run.json',changed)
            return [state,{'Test':['NONE']}]
        engine.fixture_start_state=read
        with self.assertRaises(ReadFailure) as caught:engine.wait_healthless_fixture(directory,record,n.f.SETUP)
        self.assertEqual(caught.exception.code,'COLD_NATIVE_RUN_CHANGED')

    def test_state_query_timeout_is_capped_and_deadline_prevents_spawn(self):
        engine=object.__new__(n.Engine);engine.endpoint='unix:///isolated/socket';engine.env={};engine._fixture_start_until=11
        value=SimpleNamespace(returncode=0,stdout='[]',stderr='')
        with patch.object(n.time,'monotonic',return_value=10),patch.object(n.subprocess,'run',return_value=value) as run:
            engine.fixture_start_state('a'*64)
        self.assertEqual(run.call_args.kwargs['timeout'],1)
        with patch.object(n.time,'monotonic',return_value=11),patch.object(n.subprocess,'run') as run:
            with self.assertRaises(ReadFailure) as caught:engine.fixture_start_state('a'*64)
        run.assert_not_called();self.assertEqual(caught.exception.code,'COLD_NATIVE_FIXTURE_START_TIMEOUT')

    def test_state_query_deadline_kills_and_reaps_real_metadata_child(self):
        import time
        folder=self.base/'bin';folder.mkdir();binary=folder/'docker';pidfile=self.base/'child.pid'
        binary.write_text('#!'+sys.executable+'\nimport os,time\nfrom pathlib import Path\nPath('+repr(str(pidfile))+').write_text(str(os.getpid()))\ntime.sleep(10)\n')
        binary.chmod(0o700);engine=object.__new__(n.Engine);engine.endpoint='unix:///isolated/socket';engine.env={'PATH':str(folder)}
        engine._fixture_start_until=time.monotonic()+.5
        with self.assertRaises(ReadFailure) as caught:engine.fixture_start_state('a'*64)
        self.assertEqual(caught.exception.code,'ACCEPTANCE_COMMAND_UNCERTAIN')
        pid=int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    def test_failed_unit_cleanup_blocks_reader_or_container_control(self):
        directory,_=self.create();engine=object.__new__(n.Engine);engine.ids=lambda *args:(_ for _ in ()).throw(AssertionError('Docker queried'))
        with patch.object(n.a.native,'cleanup_unit_subprocess',side_effect=ReadFailure('UNIT_UNKNOWN')),patch.object(n.a.Engine,'cleanup') as old:
            self.deny(lambda:engine.cleanup(directory))
        old.assert_not_called()

    def test_cleanup_proves_exit_then_reader_absence_then_owned_cleanup(self):
        directory,_=self.create();engine=object.__new__(n.Engine);order=[]
        with patch.object(n.a.native,'cleanup_unit_subprocess',side_effect=lambda d:(order.append('unit') or {'worker_exit_verified':True})),\
             patch.object(engine,'initializer_observation',return_value={'status':'absent','source_ownership':'original_actor'}),\
             patch.object(n.a.Engine,'cleanup',side_effect=lambda d:(order.append('owned') or {'result':'passed'})):
            self.assertEqual(engine.cleanup(directory)['result'],'passed')
        self.assertEqual(order,['unit','owned'])

    def test_uncertain_reader_blocks_resource_cleanup_after_unit_exit(self):
        directory,_=self.create();marker=json.loads((directory/'cold-run.json').read_text());job=str(uuid.uuid4())
        marker['operations']=[job]
        with patch.object(n,'validate'),patch.object(n.a,'read',side_effect=lambda path,*args:json.dumps(marker).encode()),\
             patch.object(n.a.native,'cleanup_unit_subprocess',return_value={'worker_exit_verified':True}),\
             patch.object(n.a.Engine,'cleanup') as old:
            engine=object.__new__(n.Engine);calls=[];engine.ids=lambda filters:(calls.append(filters) or ['f'*64])
            self.deny(lambda:engine.cleanup(directory))
        old.assert_not_called();self.assertEqual(calls,[['label='+n.storage.LABEL+'='+job]])

    def test_unknown_error_text_is_redacted(self):
        self.assertNotIn('secret',json.dumps(n.error_report(RuntimeError('secret key /private'))))
        self.assertEqual(n.error_report(n.age_tool.Failure('COLD_READER_PROCESS_FAILED'))['code'],'COLD_READER_PROCESS_FAILED')

    def test_child_reference_is_rejected_before_engine_constructor(self):
        with patch.object(sys,'argv',[n.ENTRY,'worker']),patch.object(n,'Engine') as engine,patch.object(sys,'stdout',new_callable=io.StringIO) as out:
            self.assertEqual(n.main(),2)
        engine.assert_not_called();self.assertEqual(json.loads(out.getvalue())['code'],'ACCEPTANCE_RUN_DIR_REQUIRED')

    def test_mutation_requires_copied_root(self):
        directory,_=self.create()
        with patch.object(n.a.isolated,'prepare_test_ops') as prepare:
            self.deny(lambda:n.exercise(directory,MetadataEngine(),self.base))
        prepare.assert_not_called()

    def test_callback_disposes_only_live_created_helpers_before_lease_release(self):
        called=[];spec={'exact':'recipe'}
        backend=SimpleNamespace(owned_helpers={'f'*64:spec},helper=lambda *a:called.append('verify'),abort=lambda ident,s:(called.append('abort'),backend.owned_helpers.pop(ident)))
        class Operation:
            helpers={}
            def __call__(self,scope):raise n.age_tool.Failure('COLD_READER_SECURITY_CHANGED')
        op=Operation();op.backend=backend
        with patch.object(n.a,'leased_child'),patch.object(n,'LIFECYCLE_FD',7):self.deny(lambda:n.callback(op,self.base)(SimpleNamespace()))
        self.assertEqual(called,['verify','abort']);self.assertFalse(backend.owned_helpers)

    def test_parent_preparation_failure_runs_cleanup_and_never_invokes_worker(self):
        directory,record=self.create();engine=SimpleNamespace(create_fixture_network=lambda d:(_ for _ in ()).throw(ReadFailure('DOCKER_FAILED')),cleanup=lambda d:{'result':'passed'})
        with patch.object(n,'preflight'),patch.object(n,'Engine',return_value=engine),patch.object(n,'create_run',return_value=(directory,record)),\
             patch.object(n.age_tool,'fetch',return_value={}),patch.object(n,'invoke') as invoke,patch.object(sys,'stderr',new_callable=io.StringIO):
            result=n.run(self.base,True)
        invoke.assert_not_called();self.assertEqual(result['code'],'DOCKER_FAILED');self.assertEqual(result['cleanup']['result'],'passed')

    def test_parent_cleanup_failure_and_protected_change_cannot_report_pass(self):
        directory,record=self.create()
        for cleanup in ({'result':'attention_required'},{'result':'passed','prior_run_protected_baseline_match':False}):
            engine=SimpleNamespace(create_fixture_network=lambda d:None,compose=lambda *args:None,protect=lambda *a:None,
                initialize_storage=lambda d:None,cleanup=lambda d:cleanup)
            with patch.object(n,'preflight'),patch.object(n,'Engine',return_value=engine),patch.object(n,'create_run',return_value=(directory,record)),\
                 patch.object(n.age_tool,'fetch',return_value={}),patch.object(n,'invoke',return_value={'result':'passed'}),patch.object(sys,'stderr',new_callable=io.StringIO):
                result=n.run(self.base,True)
            self.assertEqual(result['result'],'attention_required')

    def test_tools_context_and_identity_are_deleted_even_after_worker_failure(self):
        directory,record=self.create();tools=[]
        def fetch(path):tools.append(path);(path/'test-identity').write_text('synthetic');return {}
        engine=SimpleNamespace(create_fixture_network=lambda d:None,compose=lambda *args:None,protect=lambda *a:None,
            initialize_storage=lambda d:None,cleanup=lambda d:{'result':'passed'})
        with patch.object(n,'preflight'),patch.object(n,'Engine',return_value=engine),patch.object(n,'create_run',return_value=(directory,record)),\
             patch.object(n.age_tool,'fetch',side_effect=fetch),patch.object(n,'invoke',side_effect=ReadFailure('FAILED')),patch.object(sys,'stderr',new_callable=io.StringIO):
            result=n.run(self.base,True)
        self.assertEqual(result['code'],'FAILED');self.assertTrue(tools);self.assertFalse(tools[0].exists())

    def test_parent_carries_valid_child_query_diagnostic_and_original_phase(self):
        directory,_=self.create()
        report=n.error_report(n.query_failure(['image','inspect','--format'],'COLD_DOCKER_METADATA_FAILED',1,
            b'map has no entry for key "Volumes" SECRET'))
        report['failure_phase']='storage_setup';report['storage_docker_diagnostic']['stderr']='SECRET'
        def spawn(*args,**kw):
            kw['stdout'].write(json.dumps(report));return SimpleNamespace(wait=lambda **kw:2)
        with patch.object(n.subprocess,'Popen',side_effect=spawn),patch.object(n,'CURRENT_PHASE','capture'):
            with self.assertRaises(ReadFailure) as caught:n.invoke(directory,self.base)
        result=n.error_report(caught.exception)
        self.assertEqual(result['failure_phase'],'storage_setup')
        self.assertEqual(result['storage_docker_diagnostic']['operation'],'image_inspect')
        self.assertNotIn('SECRET',json.dumps(result))


class Discovery(unittest.TestCase):
    def test_metadata_failure_projects_only_fixed_operation_reason_and_exit(self):
        secret=b'template parsing error: map has no entry for key "Volumes"; token=SECRET /private/path'
        calls=[]
        backend=n.NativeDocker({'endpoint':'unix:///isolated/docker.sock'},'/isolated/docker',lambda argv,env,seconds:(calls.append(argv) or (1,b'',secret)))
        with self.assertRaises(ReadFailure) as caught:backend.query(['image','inspect','--format',n.storage.IMAGE_FORMAT,'sha256:'+'a'*64])
        report=n.error_report(caught.exception)
        self.assertEqual(report['storage_docker_diagnostic'],{'operation':'image_inspect','outcome':'failed',
            'reason':'TEMPLATE_FIELD_MISSING','exit_code':1,'template_field':'Volumes'})
        self.assertEqual(len(calls),1);self.assertNotIn('SECRET',json.dumps(report));self.assertNotIn('/private/path',json.dumps(report))

    def test_nonempty_volume_projection_preserves_existing_rejection_gate(self):
        image='sha256:'+'a'*64
        backend=n.NativeDocker({'endpoint':'unix:///isolated/docker.sock'},'/isolated/docker',lambda *a:(0,json.dumps([image,{'/data':{}}]).encode(),b''))
        result=backend.query(['image','inspect','--format',n.storage.IMAGE_FORMAT,image])
        self.assertTrue(result[1])
        with self.assertRaises(ReadFailure):n.require(not result[1],'COLD_NATIVE_IMAGE_VOLUME_UNSUPPORTED')

    def test_changed_image_id_is_refused_before_any_query(self):
        backend=n.NativeDocker({},'/isolated/docker',lambda *a:self.fail('query must not run'))
        for image in ('latest','sha256:bad','--format','/private/path'):
            with self.assertRaises(ReadFailure):backend.query(['image','inspect','--format',n.storage.IMAGE_FORMAT,image])

    def test_timeout_limit_missing_cli_and_invalid_json_have_no_retry(self):
        args=['info','--format',n.storage.HOST_FORMAT]
        values=[('COLD_DOCKER_OUTCOME_UNKNOWN','COMMAND_TIMEOUT'),('COLD_DOCKER_RESPONSE_LIMIT','RESPONSE_LIMIT')]
        for code,reason in values:
            with patch.object(n.storage.Docker,'call',side_effect=n.age_tool.Failure(code)) as call:
                with self.assertRaises(ReadFailure) as caught:n.NativeDocker({},'/isolated/docker').query(args)
            self.assertEqual(call.call_count,1);self.assertEqual(n.error_report(caught.exception)['storage_docker_diagnostic']['reason'],reason)
        for failure,reason in ((FileNotFoundError('SECRET'),'CLI_UNAVAILABLE'),(None,'INVALID_JSON')):
            with patch.object(n.storage.Docker,'call',side_effect=failure,return_value=(0,b'SECRET invalid JSON',b'')) as call:
                with self.assertRaises(ReadFailure) as caught:n.NativeDocker({},'/isolated/docker').query(args)
            report=n.error_report(caught.exception)
            self.assertEqual(call.call_count,1);self.assertEqual(report['storage_docker_diagnostic']['reason'],reason)
            self.assertNotIn('SECRET',json.dumps(report))

    def test_safe_projection_rejects_foreign_values_and_drops_added_fields(self):
        valid={'operation':'host_info','outcome':'failed','reason':'UNKNOWN','exit_code':1}
        self.assertEqual(n.safe_query_diagnostic(dict(valid,stderr='SECRET',argv=['SECRET'],template_field='SECRET')),valid)
        for key,value in (('operation',[]),('reason',{}),('outcome','SECRET'),('exit_code',True),('exit_code',999)):
            self.assertIsNone(n.safe_query_diagnostic(dict(valid,**{key:value})))
        self.assertIsNone(n.safe_query_diagnostic('SECRET'))

    def test_error_reason_classification_never_copies_raw_error_text(self):
        args=['info','--format',n.storage.HOST_FORMAT]
        for text,reason in ((b'permission denied token SECRET','ACCESS_DENIED'),(b'Cannot connect SECRET','CONNECTION_FAILED'),
            (b'No such image SECRET','IMAGE_NOT_FOUND'),(b'template parsing error SECRET','TEMPLATE_PARSE_ERROR'),(b'arbitrary SECRET','UNKNOWN')):
            report=n.error_report(n.query_failure(args,'COLD_DOCKER_METADATA_FAILED',1,text))
            self.assertEqual(report['storage_docker_diagnostic']['reason'],reason);self.assertNotIn('SECRET',json.dumps(report))

    def test_error_phase_projection_ignores_invalid_saved_phase(self):
        error=n.query_failure(['image','inspect','--format'],'COLD_DOCKER_METADATA_FAILED',1,b'map has no entry for key "Volumes"')
        error.failure_phase='storage_setup'
        with patch.object(n,'CURRENT_PHASE','capture'):
            self.assertEqual(n.error_report(error)['failure_phase'],'storage_setup')
        error.failure_phase=['SECRET']
        self.assertNotIn('SECRET',json.dumps(n.error_report(error)))

    def test_discovered_owner_mount_uid_has_no_fixed_database_uid(self):
        uid=n.f.choose_uid();self.assertNotEqual(uid,os.getuid());self.assertTrue(10000<=uid<600000)

    def test_foreign_or_ambiguous_scope_is_not_adopted(self):
        record={'owner':str(uuid.uuid4()),'prefix':'test','image':'sha256:'+'a'*64};directory=Path('/tmp/private-test')
        engine=SimpleNamespace(owned=lambda *a:['f'*64],metadata=lambda _:{'identity':{'service':'cold-db','project':'foreign','image':record['image']},'files':str(directory/'project'/n.f.REL)})
        with self.assertRaises(ReadFailure):n.f.discover(directory,record,engine,SimpleNamespace())

    def test_native_compatibility_adapter_rewrites_only_exact_inspect_projection(self):
        calls=[]
        def runner(argv,env,seconds):calls.append(argv[3:]);return 0,b'[]',b''
        d=n.NativeDocker({'endpoint':'unix:///fixture','daemon_id':'fake','docker_root':'/tmp/fake'},sys.executable,runner)
        d.query(['container','inspect','--format',n.storage.FORMAT,'a'*64])
        self.assertEqual(calls[-1],['container','inspect','--format',n.FORMAT,'a'*64])
        d.query(['container','inspect','--format',n.storage.HELPER_FORMAT,'a'*64])
        self.assertEqual(calls[-1][3],n.storage.HELPER_FORMAT)
        self.assertNotIn('.Env',n.FORMAT);self.assertNotIn('list ',n.FORMAT)

    def test_native_adapter_rejects_invalid_id_before_command(self):
        d=n.NativeDocker({'endpoint':'unix:///fixture','daemon_id':'fake','docker_root':'/tmp/fake'},sys.executable,
            lambda *args:(_ for _ in ()).throw(AssertionError('command')))
        with self.assertRaises(ReadFailure):d.query(['container','inspect','--format',n.storage.FORMAT,'foreign'])

    def test_native_initializer_metadata_commands_never_read_source_file_bodies(self):
        self.assertNotIn('.Config.Env',n.storage.FORMAT);self.assertNotIn('cat ',n.f.SETUP_CODE)
        self.assertIn('os.O_NOFOLLOW',n.f.SETUP_CODE);self.assertNotIn('rmtree',n.f.SETUP_CODE)


@unittest.skipUnless(os.environ.get('OPS_CAPTURE_TEST_AGE_DIR'),'explicit temporary native age tool directory required')
class Crypto(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir(mode=0o700);p=self.source/'payload.bin';p.write_bytes(n.f.PAYLOAD);p.chmod(0o600)
        tools=Path(os.environ['OPS_CAPTURE_TEST_AGE_DIR']);self.tool=n.age_tool.Age(tools/'age',n.age_tool.sha(tools/'age'))
        self.identity=self.root/'test-identity'
        with self.identity.open('xb') as out:subprocess.run([str(tools/'age-keygen')],stdout=out,stderr=subprocess.DEVNULL,check=True,timeout=10)
        self.identity.chmod(0o600)
        recipient=subprocess.check_output([str(tools/'age-keygen'),'-y',str(self.identity)],timeout=10).decode().strip()
        self.proposal={'stores':{'storage':{'uid':os.getuid(),'gid':os.getgid()}},'quiescence':'fixture'}
        identifier=str(uuid.uuid4());cfg={'schema':1,'id':identifier,'role':'storage','source':str(self.source),'identity':n.f.identity(self.source),
            'uid':os.getuid(),'gid':os.getgid(),'seconds':20,'max_bytes':1024**2,'max_entries':16,'quiescence_identity':n.capture.fingerprint(self.proposal)}
        output=io.BytesIO();summary=io.BytesIO();reader.stream(cfg,output,summary)
        raw=output.getvalue().split(b'\n',1)[1];stats=json.loads(summary.getvalue())
        self.folder=self.root/'captured';self.folder.mkdir(mode=0o700);self.cipher=self.folder/'payload.age'
        with self.cipher.open('xb') as out:
            process=self.tool.spawn(['-e','-r',recipient],output_fd=out.fileno());process.communicate(raw,timeout=10)
            self.assertEqual(process.returncode,0)
        self.cipher.chmod(0o600)
        self.receipt={'id':identifier,'capture_only':True,'restore_verified':False,'ciphertext_sha256':n.age_tool.sha(self.cipher),
            'manifest_sha256':stats['manifest_sha256'],'entry_count':stats['entry_count'],'logical_bytes':stats['logical_bytes']}
        n.a.isolated.write(self.folder/'receipt.json',self.receipt)
        self.op=SimpleNamespace(p=self.proposal,record={'phase':'services_returned','captures':[{'job_directory':str(self.folder),'receipt':self.receipt}]})

    def test_real_reader_to_native_age_and_memory_verification_preserves_bytes_and_uid(self):
        result=n.verify_ciphertext(self.op,self.tool,self.identity)
        self.assertTrue(result['plaintext_verified_in_memory_only']);self.assertFalse(result['foreign_uid_restore_performed'])
        self.assertEqual(result['storage_uid'],os.getuid());self.assertEqual(list(self.root.iterdir()).count(self.folder),1)

    def test_receipt_or_ciphertext_corruption_never_passes(self):
        raw=self.cipher.read_bytes();self.cipher.write_bytes(raw[:-10])
        with self.assertRaises(ReadFailure):n.verify_ciphertext(self.op,self.tool,self.identity)

    def test_wrong_manifest_binding_uid_or_count_never_passes(self):
        for field in ('uid','gid'):
            self.proposal['stores']['storage'][field]+=1
            with self.assertRaises(ReadFailure):n.verify_ciphertext(self.op,self.tool,self.identity)
            self.proposal['stores']['storage'][field]-=1
        self.receipt['entry_count']=3;n.a.isolated.write(self.folder/'receipt.json',self.receipt)
        with self.assertRaises(ReadFailure):n.verify_ciphertext(self.op,self.tool,self.identity)

    def test_incorrect_key_reaps_native_crypto_child(self):
        bad=self.root/'bad';bad.write_text('invalid');bad.chmod(0o600)
        with self.assertRaises(n.age_tool.Failure):n.verify_ciphertext(self.op,self.tool,bad)

if __name__=='__main__':unittest.main()


@unittest.skipUnless(os.environ.get('PREFLIGHT_DOCKER_TEST_BINARY'),'explicit real Docker CLI; isolated synthetic Engine only')
class NativeCLI(unittest.TestCase):
    def setUp(self):
        import http.server,threading
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.calls=[];self.requests=[]
        self.binary=os.environ['PREFLIGHT_DOCKER_TEST_BINARY'];self.ident='a'*64;self.image='sha256:'+'b'*64
        self.value={'Id':self.ident,'Image':self.image,'Config':{'User':'12345:321','Entrypoint':['python3'],'Cmd':['-I','-B','-c','fixture'],
            'Tty':False,'Labels':{n.f.OWNER_LABEL:str(uuid.uuid4()),'com.docker.compose.project':'fixture','com.docker.compose.service':'cold-db'},
            'Env':['SECRET=SYNTHETIC_DO_NOT_PRINT']},'Mounts':[{'Type':'bind','Source':'/isolated/source','Destination':'/source','RW':False}],
            'State':{'Running':True,'Pid':9,'Status':'running','StartedAt':'start','ExitCode':0,'OOMKilled':False},
            'HostConfig':{'UsernsMode':'','RestartPolicy':{'Name':'no'},'ReadonlyRootfs':True,'NetworkMode':'none',
                'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges=true'],'Privileged':False,'PidMode':'','IpcMode':'none',
                'LogConfig':{'Type':'none'},'PidsLimit':32,'Memory':268435456,'NanoCpus':1000000000}}
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_HEAD(self):
                owner.calls.append(('HEAD',self.path));self.send_response(200);self.send_header('API-Version','1.51');self.end_headers()
            def send(self,value,code=200):
                raw=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                owner.calls.append(('GET',self.path))
                if self.path.endswith('/info'):self.send({'ID':'fake-daemon','OSType':'linux','SecurityOptions':[]})
                elif '/images/' in self.path:self.send({'Id':owner.image,'Config':{'Volumes':None}})
                else:self.send(owner.value)
            def do_POST(self):
                owner.calls.append(('POST',self.path));raw=self.rfile.read(int(self.headers['Content-Length']));owner.requests.append(json.loads(raw))
                self.send({'Id':owner.ident,'Warnings':[]},201)
        self.server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def invoke(self,args):
        env={'PATH':os.defpath,'HOME':str(self.root),'LC_ALL':'C'}
        result=subprocess.run([self.binary,'--config',str(self.root/'config'),'--host','tcp://127.0.0.1:'+str(self.server.server_port),*args],
            capture_output=True,env=env,timeout=10)
        self.assertNotIn(b'SYNTHETIC_DO_NOT_PRINT',result.stdout+result.stderr)
        return result
    def test_real_cli_service_projection_handles_missing_health_without_env(self):
        result=self.invoke(['container','inspect','--format',n.FORMAT,self.ident])
        self.assertEqual(result.returncode,0,result.stderr);values=json.loads(result.stdout)
        self.assertEqual(values[5],'none');self.assertEqual(values[3][0]['Destination'],'/source')
        self.value['State']['Health']={'Status':'healthy'}
        healthy=self.invoke(['container','inspect','--format',n.FORMAT,self.ident])
        self.assertEqual(healthy.returncode,0,healthy.stderr);self.assertEqual(json.loads(healthy.stdout)[5],'healthy')
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.calls))
    def test_original_generic_metadata_template_fails_on_healthless_initializer(self):
        self.value['Created']='2026-10-01T00:00:00Z'
        result=self.invoke(['inspect','--type','container','--format',n.a.isolated.META,self.ident])
        self.assertNotEqual(result.returncode,0);self.assertIn(b'map has no entry for key "Health"',result.stderr)
    def test_new_generic_metadata_projects_healthless_running_created_and_exited_states(self):
        self.value['Created']='2026-10-01T00:00:00Z'
        for status,running in (('running',True),('created',False),('exited',False)):
            self.value['State'].update(Status=status,Running=running)
            result=self.invoke(['inspect','--type','container','--format',n.META,self.ident])
            self.assertEqual(result.returncode,0,result.stderr)
            row=json.loads(result.stdout);self.assertIsNone(row['identity']['health'])
            self.assertEqual(row['identity']['running'],running);self.assertNotIn(b'SYNTHETIC_DO_NOT_PRINT',result.stdout)
    def test_new_generic_metadata_keeps_declared_health_and_owner_mounts(self):
        self.value['Created']='2026-10-01T00:00:00Z';self.value['State']['Health']={'Status':'healthy'}
        owner=str(uuid.uuid4());self.value['Config']['Labels'][n.a.isolated.LABEL]=owner
        result=self.invoke(['inspect','--type','container','--format',n.META,self.ident])
        self.assertEqual(result.returncode,0,result.stderr);row=json.loads(result.stdout)
        self.assertEqual(row['identity']['health'],'healthy');self.assertEqual(row['acceptance'],owner)
        self.assertEqual(row['identity']['mounts'],self.value['Mounts'])
    def test_native_engine_metadata_uses_actual_fixed_cli_command(self):
        self.value['Created']='2026-10-01T00:00:00Z';engine=object.__new__(n.Engine)
        engine.endpoint='tcp://127.0.0.1:'+str(self.server.server_port)
        engine.env={'PATH':str(Path(self.binary).parent)+':'+os.defpath,'HOME':str(self.root),'LC_ALL':'C'}
        value=engine.metadata(self.ident);self.assertEqual(value['identity']['id'],self.ident)
        self.assertIsNone(value['identity']['health']);self.assertTrue(all(m in ('HEAD','GET') for m,_ in self.calls))

    def test_real_cli_disabled_health_fixture_state_projection_has_no_health_map_or_env(self):
        self.value['State']['Error']='';self.value['Config']['Healthcheck']={'Test':['NONE']}
        for status,running,pid in (('created',False,0),('running',True,9),('exited',False,0)):
            self.value['State'].update(Status=status,Running=running,Pid=pid)
            result=self.invoke(['container','inspect','--format',n.FIXTURE_START_FORMAT,self.ident])
            self.assertEqual(result.returncode,0,result.stderr);state,health=json.loads(result.stdout)
            self.assertEqual(state['Status'],status);self.assertEqual(health['Test'],['NONE'])
            self.assertNotIn('Health',state);self.assertNotIn(b'SYNTHETIC_DO_NOT_PRINT',result.stdout)
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.calls))

    def test_real_cli_disabled_fixture_state_gate_accepts_only_owned_running_state(self):
        directory,record=n.create_run(self.root,MetadataEngine());engine=object.__new__(n.Engine)
        engine.endpoint='tcp://127.0.0.1:'+str(self.server.server_port)
        engine.env={'PATH':str(Path(self.binary).parent)+':'+os.defpath,'HOME':str(self.root),'LC_ALL':'C'}
        engine.owned=lambda *args:[self.ident]
        engine.metadata=lambda *args:{'identity':{'project':record['compose'][n.f.REL]['project'],
            'service':n.f.SETUP,'running':self.value['State']['Running']}}
        self.value['State']['Error']='';self.value['Config']['Healthcheck']={'Test':['NONE']}
        engine.wait_healthless_fixture(directory,record,n.f.SETUP)
        self.value['State'].update(Status='exited',Running=False,Pid=0,ExitCode=1)
        with self.assertRaises(ReadFailure) as caught:engine.wait_healthless_fixture(directory,record,n.f.SETUP)
        self.assertEqual(caught.exception.code,'COLD_NATIVE_FIXTURE_START_FAILED')
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.calls))

    def test_real_cli_missing_healthcheck_projects_null_for_state_gate_to_refuse(self):
        result=self.invoke(['container','inspect','--format',n.FIXTURE_START_FORMAT,self.ident])
        self.assertEqual(result.returncode,0,result.stderr);self.assertIsNone(json.loads(result.stdout)[1])
    def test_previous_healthless_template_reproduces_real_formatter_failure(self):
        result=self.invoke(['container','inspect','--format',n.storage.FORMAT,self.ident])
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'map has no entry for key "Health"',result.stderr)
    def test_real_cli_helper_security_projection_preserves_numeric_budgets(self):
        result=self.invoke(['container','inspect','--format',n.storage.HELPER_FORMAT,self.ident])
        self.assertEqual(result.returncode,0,result.stderr);values=json.loads(result.stdout)
        self.assertEqual(values[0],'12345:321');self.assertEqual(values[4:9],[True,'none',['ALL'],['no-new-privileges=true'],False])
        self.assertEqual(values[-3:],[32,268435456,1000000000])
    def test_real_cli_host_and_image_templates_are_primary_field_projections(self):
        for args,wanted in ((['info','--format',n.storage.HOST_FORMAT],['fake-daemon','linux',[]]),
            (['image','inspect','--format',n.storage.IMAGE_FORMAT,self.image],[self.image,None])):
            result=self.invoke(args);self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout),wanted)
    def backend(self):
        return n.NativeDocker({'endpoint':'tcp://127.0.0.1:'+str(self.server.server_port),'daemon_id':'fake-daemon',
            'docker_root':'/isolated'},self.binary)
    def test_actual_candidate_call_environment_can_query_synthetic_host(self):
        # Exact sanitized candidate call path, without the old test's explicit --config.
        self.backend().verify_host()
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.calls))
    def test_absent_optional_image_volumes_reproduces_old_cli_failure_and_new_pass(self):
        original=self.server.RequestHandlerClass.do_GET
        def get(handler):
            if '/images/' in handler.path:handler.send({'Id':self.image,'Config':{}})
            else:original(handler)
        self.server.RequestHandlerClass.do_GET=get
        args=['image','inspect','--format',n.storage.IMAGE_FORMAT,self.image]
        result=self.invoke(args);self.assertNotEqual(result.returncode,0)
        self.assertIn(b'map has no entry for key "Volumes"',result.stderr)
        self.assertEqual(self.backend().query(args),[self.image,None])
    def test_declared_image_volume_survives_optional_field_projection(self):
        original=self.server.RequestHandlerClass.do_GET
        def get(handler):
            if '/images/' in handler.path:handler.send({'Id':self.image,'Config':{'Volumes':{'/data':{}}}})
            else:original(handler)
        self.server.RequestHandlerClass.do_GET=get
        result=self.backend().query(['image','inspect','--format',n.storage.IMAGE_FORMAT,self.image])
        self.assertEqual(result,[self.image,{'/data':{}}]);self.assertTrue(result[1])
    def test_real_formatter_failure_has_bounded_safe_native_diagnostic(self):
        original=self.server.RequestHandlerClass.do_GET
        def get(handler):
            if '/images/' in handler.path:handler.send({'Id':self.image,'Config':{}})
            else:original(handler)
        self.server.RequestHandlerClass.do_GET=get
        # Invoke an unmodified old template so diagnostics see the real CLI error.
        with patch.object(n,'IMAGE_FORMAT',n.storage.IMAGE_FORMAT):
            with self.assertRaises(ReadFailure) as caught:self.backend().query(['image','inspect','--format',n.storage.IMAGE_FORMAT,self.image])
        report=n.error_report(caught.exception)
        self.assertEqual(report['storage_docker_diagnostic']['reason'],'TEMPLATE_FIELD_MISSING')
        self.assertEqual(report['storage_docker_diagnostic']['template_field'],'Volumes')
        self.assertNotIn('Config',json.dumps(report));self.assertNotIn(self.image,json.dumps(report))
    def test_real_cli_serializes_reviewed_reader_create_recipe(self):
        # Use the existing candidate's actual recipe builder and Docker create.
        from ops_candidates.cold_storage_20260930.tests.test_storage import Files
        fixture=Files();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.image=fixture.helper['image_id']
        host=deepcopy(fixture.host);host['daemon_id']='fake-daemon';proposal=deepcopy(fixture.proposal);proposal['host']=host
        # Recompute the reviewed proposal after this explicit fixture-only host projection.
        proposal['review_id']=n.capture.fingerprint({k:v for k,v in proposal.items() if k!='review_id'})
        def runner(argv,env,seconds):
            result=self.invoke(argv[3:]);return result.returncode,result.stdout,result.stderr
        backend=n.storage.Docker(host,self.binary,runner);backend.bind(proposal,fixture.scope,n.time.monotonic()+30)
        spec=n.storage.recipe(n.storage.validate(proposal),'data',str(uuid.uuid4()),10)
        self.assertEqual(backend.create(spec),self.ident);self.assertEqual(len(self.requests),1)
        row=self.requests[0];control=row['HostConfig']
        self.assertEqual(row['User'],f"{os.getuid()}:{os.getgid()}");self.assertEqual(control['NetworkMode'],'none')
        self.assertTrue(control['ReadonlyRootfs']);self.assertEqual(control['CapDrop'],['ALL']);self.assertEqual(control['LogConfig']['Type'],'none')
        self.assertEqual(control['Mounts'][0]['Source'],str(fixture.src));self.assertTrue(control['Mounts'][0]['ReadOnly'])
        self.assertTrue(control['Mounts'][0]['BindOptions']['NonRecursive']);self.assertFalse(control.get('Binds'))
        self.assertEqual(row['Entrypoint'],['python3']);self.assertFalse(control['Privileged'])


class Recovery(unittest.TestCase):
    setUp=Boundaries.setUp
    create=Boundaries.create
    deny=Boundaries.deny

    def legacy(self):
        source=os.environ.get('COLD_LEGACY_ENTRY_TEST_SOURCE')
        if not source:self.skipTest('explicit reviewed previous runner bytes required')
        raw=Path(source).read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),n.LEGACY_ENTRY_SHA256)
        directory,record=self.create();target=directory/'project'/n.ENTRY
        target.write_bytes(raw);target.chmod(0o600)
        marker=json.loads((directory/'cold-run.json').read_text());marker['files'][n.ENTRY]=n.LEGACY_ENTRY_SHA256
        n.a.isolated.write(directory/'cold-run.json',marker)
        return directory,record

    def test_legacy_review_accepts_only_actual_pinned_old_bytes_without_rewriting(self):
        directory,record=self.legacy();paths=[directory/'cold-run.json',directory/'project'/n.ENTRY]
        before=[p.read_bytes() for p in paths]
        self.assertEqual(n.validate(directory,legacy_cleanup=True),record)
        self.assertEqual(before,[p.read_bytes() for p in paths]);self.assertNotEqual(n.source_read(n.ROOT/n.ENTRY),before[1])

    def test_legacy_copy_is_not_valid_for_normal_run_worker_or_cleanup(self):
        directory,_=self.legacy();self.deny(lambda:n.validate(directory))
        with patch.object(n.a.native,'cleanup_unit_subprocess') as unit:
            self.deny(lambda:object.__new__(n.Engine).cleanup(directory))
        unit.assert_not_called()

    def test_edited_legacy_copy_is_rejected_even_with_updated_marker_hash(self):
        directory,_=self.legacy();target=directory/'project'/n.ENTRY;target.write_bytes(target.read_bytes()+b'\n# edit')
        marker=json.loads((directory/'cold-run.json').read_text());marker['files'][n.ENTRY]=n.a.digest(target.read_bytes())
        n.a.isolated.write(directory/'cold-run.json',marker)
        self.deny(lambda:n.validate(directory,legacy_cleanup=True))

    def test_changed_dependency_is_not_adopted_by_legacy_review(self):
        directory,_=self.legacy();target=directory/'project'/'ops_tools/cold_storage_fixtures.py'
        target.write_bytes(target.read_bytes()+b'\n# edit')
        self.deny(lambda:n.validate(directory,legacy_cleanup=True))

    def test_current_run_is_not_accepted_as_legacy(self):
        directory,_=self.create();self.deny(lambda:n.validate(directory,legacy_cleanup=True))

    def test_legacy_flag_cannot_reach_run_preflight_or_worker_control(self):
        for action in ('run','worker','preflight'):
            with patch.object(sys,'argv',[n.ENTRY,action,'--legacy-cleanup']),patch.object(n,'preflight') as preflight,\
                 patch.object(n,'Engine') as engine,patch.object(sys,'stdout',new_callable=io.StringIO) as out:
                self.assertEqual(n.main(),2)
            self.assertEqual(json.loads(out.getvalue())['code'],'COLD_NATIVE_LEGACY_CLEANUP_ONLY')
            engine.assert_not_called();preflight.assert_not_called()

    def test_legacy_cleanup_proves_exit_observes_initializer_then_owned_cleanup(self):
        directory,_=self.legacy();engine=object.__new__(n.Engine);order=[]
        observation={'status':'absent','source_ownership':'original_actor'}
        with patch.object(n.a.native,'cleanup_unit_subprocess',side_effect=lambda *a:(order.append('exit') or {'worker_exit_verified':True})),\
             patch.object(engine,'initializer_observation',side_effect=lambda *a:(order.append('observe') or observation)),\
             patch.object(n.a.Engine,'cleanup',side_effect=lambda *a:(order.append('cleanup') or {'result':'passed'})):
            result=engine.cleanup(directory,legacy_cleanup=True)
        self.assertEqual(order,['exit','observe','cleanup'])
        self.assertEqual(result['initializer_observation_before_cleanup'],observation)

    def test_wrong_legacy_bytes_never_reach_unit_or_container_control(self):
        directory,_=self.create();engine=object.__new__(n.Engine)
        with patch.object(n.a.native,'cleanup_unit_subprocess') as unit,patch.object(engine,'command') as command:
            self.deny(lambda:engine.cleanup(directory,legacy_cleanup=True))
        unit.assert_not_called();command.assert_not_called()

    def test_partial_cleanup_retains_initializer_observation_and_exact_progress(self):
        directory,_=self.create();engine=object.__new__(n.Engine)
        failure=ReadFailure('ACCEPTANCE_DOCKER_COMMAND_FAILED')
        failure.cleanup_progress={'removed_containers':2,'removed_networks':0,'owned_absence_verified':False,'worker_exit_verified':True}
        observation={'status':'created','running':False,'exit_code':0,'oom_killed':False,
            'error_reason':'OCI_RUNTIME_ERROR','source_ownership':'original_actor'}
        with patch.object(n.a.native,'cleanup_unit_subprocess',return_value={'worker_exit_verified':True}),\
             patch.object(engine,'initializer_observation',return_value=observation),patch.object(n.a.Engine,'cleanup',side_effect=failure):
            with self.assertRaises(ReadFailure) as caught:engine.cleanup(directory)
        report=n.error_report(caught.exception)
        self.assertEqual(report['failure_phase'],'cleanup');self.assertEqual(report['initializer_observation'],observation)
        self.assertEqual(report['cleanup_progress']['removed_containers'],2);self.assertFalse(report['cleanup_progress']['owned_absence_verified'])

    def test_initializer_observation_distinguishes_exit_and_never_exposes_raw_error(self):
        directory,record=self.create();engine=object.__new__(n.Engine);ident='a'*64;calls=[]
        engine.owned=lambda *a:[ident];engine.metadata=lambda *a:{'identity':{'project':record['prefix']+'-langfuse','service':n.f.SETUP}}
        engine.command=lambda args:(calls.append(args) or json.dumps(['created',False,0,False,
            'OCI runtime create failed: SECRET /private/path']))
        report=engine.initializer_observation(directory,record)
        self.assertEqual(report['error_reason'],'OCI_RUNTIME_ERROR');self.assertEqual(report['status'],'created')
        self.assertNotIn('SECRET',json.dumps(report));self.assertNotIn('/private/path',json.dumps(report))
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][:2],['container','inspect'])

    def test_initializer_absence_is_metadata_only_and_not_acceptance(self):
        directory,record=self.create();engine=object.__new__(n.Engine);engine.owned=lambda *a:[]
        with patch.object(engine,'command') as control:report=engine.initializer_observation(directory,record)
        control.assert_not_called();self.assertEqual(report,{'status':'absent','source_ownership':'original_actor'})

    def test_ambiguous_initializer_never_runs_inspect_or_control(self):
        directory,record=self.create();engine=object.__new__(n.Engine);engine.owned=lambda *a:['a'*64,'b'*64]
        engine.metadata=lambda *a:{'identity':{'project':record['prefix']+'-langfuse','service':n.f.SETUP}}
        with patch.object(engine,'command') as command:self.deny(lambda:engine.initializer_observation(directory,record))
        command.assert_not_called()

    def test_failed_fixture_compose_context_is_classified_and_restored(self):
        directory,_=self.create();engine=object.__new__(n.Engine);engine._cold_context={'phase':'capture','fixture_role':None}
        seen=[]
        def fail(*args):seen.append(deepcopy(engine._cold_context));raise ReadFailure('ACCEPTANCE_DOCKER_COMMAND_FAILED')
        with patch.object(engine,'verify_fixture_network'),patch.object(engine,'start_healthless_fixture',side_effect=fail):
            self.deny(lambda:engine.compose(directory,n.f.REL,n.f.SETUP))
        self.assertEqual(seen,[{'phase':'storage_setup','fixture_role':'cold_setup'}])
        self.assertEqual(engine._cold_context,{'phase':'capture','fixture_role':None})

    def test_fixture_cli_failure_is_single_call_and_no_raw_text_is_published(self):
        engine=object.__new__(n.Engine);engine.endpoint='unix:///isolated/socket';engine.env={}
        engine._cold_context={'phase':'storage_setup','fixture_role':'cold_setup'}
        value=SimpleNamespace(returncode=1,stdout='',stderr='OCI runtime create failed: executable file not found SECRET /private')
        with patch.object(n.subprocess,'run',return_value=value) as run:
            with self.assertRaises(ReadFailure) as caught:engine.command(['compose','up','cold-setup'])
        report=n.error_report(caught.exception);self.assertEqual(run.call_count,1)
        self.assertEqual(report['fixture_docker_diagnostic']['fixture_role'],'cold_setup')
        self.assertEqual(report['fixture_docker_diagnostic']['reason'],'EXECUTABLE_UNAVAILABLE')
        self.assertNotIn('SECRET',json.dumps(report));self.assertNotIn('/private',json.dumps(report))

    def test_fixture_cli_timeout_is_unknown_and_does_not_repeat_submission(self):
        engine=object.__new__(n.Engine);engine.endpoint='unix:///isolated/socket';engine.env={}
        with patch.object(n.subprocess,'run',side_effect=subprocess.TimeoutExpired('SECRET',1)) as run:
            with self.assertRaises(ReadFailure) as caught:engine.command(['compose','up'])
        report=n.error_report(caught.exception);self.assertEqual(run.call_count,1)
        self.assertEqual(report['code'],'ACCEPTANCE_COMMAND_UNCERTAIN');self.assertNotIn('SECRET',json.dumps(report))

    def test_safe_fixture_and_initializer_fields_ignore_added_or_invalid_data(self):
        value={'operation':'compose_up','outcome':'failed','reason':'UNKNOWN','exit_code':1,
            'text_truncated':False,'phase':'storage_setup','fixture_role':'cold_setup'}
        self.assertEqual(n.safe_fixture_diagnostic(value),value)
        for field,item in (('fixture_role','SECRET'),('reason',[]),('exit_code',True),('phase',{})):
            self.assertIsNone(n.safe_fixture_diagnostic(dict(value,**{field:item})))
        self.assertIsNone(n.safe_fixture_diagnostic(dict(value,stderr='SECRET')))
        self.assertIsNone(n.safe_initializer_observation({'status':'absent','source_ownership':'SECRET'}))

    def test_fixture_signature_precedence_and_secret_redaction(self):
        for text,reason in (('map has no entry for key "Health" SECRET','TEMPLATE_FIELD_MISSING'),
            ('invalid mount config: bind source path does not exist SECRET','BIND_SOURCE_MISSING'),
            ('container exited (1) SECRET','CONTAINER_EXITED'),('OCI runtime create failed SECRET','OCI_RUNTIME_ERROR'),
            ('container private-name has no healthcheck configured SECRET','DISABLED_HEALTHCHECK_WAIT')):
            self.assertEqual(n.fixture_reason('',text),(reason,False))

    def test_metadata_fix_does_not_adopt_foreign_owner_or_replacement(self):
        directory,record=self.create();engine=object.__new__(n.Engine);engine.ids=lambda *a:['a'*64]
        rel=n.f.REL;row={'acceptance':'foreign','files':str(directory/'project'/rel),
            'identity':{'image':record['image'],'project':record['compose'][rel]['project']}}
        engine.metadata=lambda *a:row
        self.deny(lambda:engine.owned(directory,record))
