"""Native-runner boundaries: private copies, metadata projection and cleanup order."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.ops.tools import maintenance_execute_acceptance as n
from src.ops.tools.tests.test_maintenance_acceptance import MetadataEngine
from src.ops.platform.contracts import ReadFailure


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
    def create(self): return n.create_run(self.base,MetadataEngine())

    def test_source_and_candidate_copy_preserves_runtime_and_private_modes(self):
        directory, record = self.create()
        self.assertEqual(n.validate(directory),record)
        marker = json.loads((directory/'execution-run.json').read_text())
        self.assertEqual(marker['executor_sha256'],n.EXECUTOR_SHA)
        self.assertEqual(n.a.worker.code_hash(directory/'project'),n.a.core.CODE)
        self.assertEqual(len(marker['host_sources']),14)
        for rel in marker['host_sources']:
            self.assertEqual((directory/'project'/rel).stat().st_mode&0o777,0o600)
        self.assertNotIn('.secrets',json.dumps(marker)); self.assertFalse((directory/'state').exists())

    def test_copied_reviewed_dependencies_import_in_real_fresh_process(self):
        directory,_ = self.create();project=directory/'project'
        code = ('import sys,json; from pathlib import Path; root=Path(sys.argv[1]);sys.path.insert(0,str(root));'
                'from src.ops.tools import maintenance_execute as e;'
                'm=e.dependencies(root);bc,reviewed,r,core,cap=m.dependencies(root);'
                'assert reviewed["code_state"]=="candidate";'
                'assert r.worker.ROOT==root;print("reviewed-copy-pass")')
        result = subprocess.run([sys.executable,'-I','-B','-c',code,str(project)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'reviewed-copy-pass')

    def test_changed_copy_is_refused_before_child_execution_or_cleanup(self):
        directory,_ = self.create();path=directory/'project/src/ops/tools/maintenance_execute.py'
        path.write_bytes(path.read_bytes()+b'\n# unreviewed\n')
        with patch.object(n.subprocess,'Popen') as spawn,patch.object(n.a.native,'cleanup_unit_subprocess') as unit:
            with self.assertRaises(ReadFailure):n.invoke(directory)
            engine=object.__new__(n.Engine)
            with self.assertRaises(ReadFailure):engine.cleanup(directory)
        spawn.assert_not_called();unit.assert_not_called()

    def test_owner_marker_or_host_source_set_cannot_authorize_execution(self):
        directory,_ = self.create();path=directory/'execution-run.json';marker=json.loads(path.read_text())
        for field,value in (('owner','foreign'),('executor_sha256','0'*64),('host_sources',{})):
            changed=dict(marker);changed[field]=value;n.a.isolated.write(path,changed)
            with self.assertRaises(ReadFailure):n.validate(directory)
        n.a.isolated.write(path,marker);self.assertEqual(n.validate(directory)['owner'],marker['owner'])

    def test_extra_runtime_or_symlink_hardlink_copy_refused(self):
        directory,_ = self.create();project=directory/'project';p=project/'src/ops/platform/unreviewed.py'
        p.write_text('');p.chmod(0o600)
        with self.assertRaises(ReadFailure):n.validate(directory)
        p.unlink();p=project/n.ENTRY;raw=p.read_bytes();p.unlink();p.symlink_to(n.ROOT/n.ENTRY)
        with self.assertRaises(ReadFailure):n.validate(directory)
        p.unlink();p.write_bytes(raw);p.chmod(0o600);os.link(p,directory/'alias')
        with self.assertRaises(ReadFailure):n.validate(directory)

    def test_preflight_linger_or_source_failure_creates_no_resources(self):
        with patch.object(n.a,'preflight',side_effect=ReadFailure('MAINTENANCE_ACCEPTANCE_LINGER_REQUIRED')),patch.object(n,'Engine') as engine:
            with self.assertRaises(ReadFailure):n.run(self.base/'unused')
        engine.assert_not_called();self.assertEqual(list(self.base.iterdir()),[])

    def test_parent_rechecks_unit_before_owned_docker_cleanup(self):
        directory,_ = self.create();engine=object.__new__(n.Engine);order=[]
        with patch.object(n.a.Engine,'cleanup',side_effect=lambda d:(order.append('verified-owned-cleanup') or {'result':'passed'})):
            self.assertEqual(engine.cleanup(directory),{'result':'passed'})
        self.assertEqual(order,['verified-owned-cleanup'])

    def test_parent_records_failed_cleanup_instead_of_pass(self):
        directory,record=self.create()
        engine=SimpleNamespace(create_fixture_network=lambda d:None,compose=lambda *a:None,
            cleanup=lambda d:(_ for _ in ()).throw(ReadFailure('ACCEPTANCE_UNIT_CLEANUP_UNCERTAIN')))
        with patch.object(n,'preflight'),patch.object(n,'Engine',return_value=engine),patch.object(n,'create_run',return_value=(directory,record)),patch.object(n,'invoke',return_value={'result':'passed','stage':n.STAGE}):
            result=n.run(self.base)
        self.assertEqual(result['result'],'attention_required');self.assertEqual(result['cleanup']['code'],'ACCEPTANCE_UNIT_CLEANUP_UNCERTAIN')

    def test_prepare_failure_does_not_invoke_worker_and_cleanup_is_reported(self):
        directory,record=self.create();calls=[]
        engine=SimpleNamespace(create_fixture_network=lambda d:None,
            compose=lambda *a:(_ for _ in ()).throw(ReadFailure('ACCEPTANCE_DOCKER_COMMAND_FAILED')),
            cleanup=lambda d:(calls.append('cleanup') or {'result':'passed'}))
        with patch.object(n,'preflight'),patch.object(n,'Engine',return_value=engine),patch.object(n,'create_run',return_value=(directory,record)),patch.object(n,'invoke') as invoke:
            result=n.run(self.base)
        invoke.assert_not_called();self.assertEqual(calls,['cleanup']);self.assertEqual(result['code'],'ACCEPTANCE_DOCKER_COMMAND_FAILED')

    def test_old_protected_difference_cannot_report_acceptance_pass(self):
        directory,record=self.create();engine=SimpleNamespace(create_fixture_network=lambda d:None,compose=lambda *a:None,
            cleanup=lambda d:{'result':'passed','prior_run_protected_baseline_match':False})
        with patch.object(n,'preflight'),patch.object(n,'Engine',return_value=engine),patch.object(n,'create_run',return_value=(directory,record)),patch.object(n,'invoke',return_value={'result':'passed','stage':n.STAGE}):
            result=n.run(self.base)
        self.assertEqual(result['result'],'attention_required');self.assertEqual(result['code'],'ACCEPTANCE_PROTECTED_CONTAINER_CHANGED')

    def test_mutation_requires_copied_root_before_constructor(self):
        directory,_=self.create()
        with patch.object(n.execution,'Execute') as ctor:
            with self.assertRaisesRegex(ReadFailure,'COPIED_ENTRY_REQUIRED'):n.executor(directory,MetadataEngine())
        ctor.assert_not_called()

    def test_error_report_redacts_unknown_text_and_preserves_fixed_diagnostics(self):
        value=n.error_report(RuntimeError('private-token-endpoint'))
        self.assertNotIn('private-token',json.dumps(value));self.assertEqual(value['stage'],n.STAGE)
        exc=ReadFailure('ACCEPTANCE_DOCKER_COMMAND_FAILED')
        exc.docker_diagnostic=dict(operation='compose_up',outcome='failed',reason='ADDRESS_POOL_EXHAUSTED',exit_code=1,
            text_truncated=False,phase='fixture_preparation',fixture_role='langfuse')
        self.assertEqual(n.error_report(exc)['docker_diagnostic'],exc.docker_diagnostic)

    def test_cli_invalid_run_reference_never_connects_engine(self):
        with patch.object(sys,'argv',[n.ENTRY,'worker']),patch.object(n,'Engine') as engine,patch.object(sys,'stdout',new_callable=io.StringIO) as output:
            self.assertEqual(n.main(),2)
        engine.assert_not_called();self.assertEqual(json.loads(output.getvalue())['code'],'ACCEPTANCE_RUN_DIR_REQUIRED')

    def test_failure_phase_is_fixed_label_only(self):
        exc=ReadFailure('EXECUTION_ACCEPTANCE_CHILD_FAILED');exc.execution_phase='recovery'
        self.assertEqual(n.error_report(exc)['execution_phase'],'recovery')
        exc.execution_phase='private-token-or-path'
        self.assertNotIn('execution_phase',n.error_report(exc))

    def test_progress_checks_protection_before_operation(self):
        directory,record=self.create();engine=SimpleNamespace(protect=lambda value:self.assertEqual(value,record['protected']))
        with patch.object(sys,'stderr',new_callable=io.StringIO):n.progress(directory,engine,'recovery','test phase')
        self.assertEqual(n.CURRENT_PHASE,'recovery')
        with self.assertRaises(ReadFailure):n.progress(directory,engine,'private-secret','bad phase')


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory,self.record=n.create_run(Path(self.temp.name),MetadataEngine())
        state=self.directory/'state';state.mkdir(mode=0o700)
        for name in ('owner.json','host-config.json','config.json','compose.env','access-key'):
            (state/name).write_text('{}');(state/name).chmod(0o600)
        self.cfg={'platforms':{'agent':{'token_file':str(self.directory/'keys/agent')}},'langfuse_file':str(self.directory/'keys/langfuse.json')}
        self.metas={};self.active={}
        roles=[(self.record['prefix']+'-'+role,service) for role,(service,_) in n.a.isolated.ROLES.items()]
        roles += [(self.record['prefix']+'-model','manual-model'),(self.record['prefix']+'-ops','ops-web')]
        for i,(project,service) in enumerate(roles):
            ident=f'{i+1:064x}'
            row=dict(id=ident,project=project,service=service,image=self.record['image'],running=True,health='healthy',started='start-'+str(i),
                mounts=[{'Type':'bind','Source':str(self.directory/'project'),'Destination':'/fixture-source','RW':False}])
            self.metas[ident]={'identity':row,'acceptance':self.record['owner']}
            self.active[ident]={'id':ident,'running':True,'started':row['started']}
        self.active['e'*64]={'id':'e'*64,'running':True,'started':'protected'}
        engine=SimpleNamespace(owned=lambda *a:list(self.metas),metadata=lambda i:deepcopy(self.metas[i]),snapshot=lambda:deepcopy(self.active))
        self.host=dict(context='test-context',endpoint=self.record['endpoint'],daemon_id='test-daemon')
        review=SimpleNamespace(docker=SimpleNamespace(host=lambda:deepcopy(self.host)))
        self.inventory=n.FixtureInventory(self.directory,engine,review)
        self.addCleanup(patch.stopall)
        patch.object(n.a.native,'checked_state',return_value=(state,{})).start()
        patch.object(n.a.manage,'existing',return_value=self.cfg).start()

    def test_native_metadata_projection_disjoint_scope_protection_and_private_files(self):
        result=self.inventory.capture()
        self.assertEqual(len(result['inventory']['scope']),9)
        self.assertEqual(result['inventory']['protected'],{'e'*64:'protected'})
        self.assertIn('ops-web',result['inventory']['scope']);self.assertEqual(len(result['files']),7)
        self.assertEqual(result['host'],self.host)

    def test_running_health_owner_and_started_mismatch_are_not_adopted(self):
        ident=next(iter(self.metas));original=deepcopy(self.metas[ident])
        for key,value in (('health','unhealthy'),('running',False),('started','replacement')):
            self.metas[ident]['identity'][key]=value
            with self.assertRaises(ReadFailure):self.inventory.capture()
            self.metas[ident]=deepcopy(original)
        self.metas[ident]['acceptance']='foreign'
        with self.assertRaises(ReadFailure):self.inventory.capture()

    def test_mount_outside_run_wrong_type_or_unexpected_tmpfs_refused(self):
        ident=next(iter(self.metas));mount=self.metas[ident]['identity']['mounts'][0]
        for change in ({'Source':'/outside-production'},{'Type':'volume'},{'Type':'tmpfs','Destination':'/tmp'}):
            original=deepcopy(mount);mount.update(change)
            with self.assertRaises(ReadFailure):self.inventory.capture()
            mount.clear();mount.update(original)

    def test_file_or_daemon_identity_change_is_present_in_snapshot(self):
        before=self.inventory.capture();p=self.directory/'state/config.json';p.write_text('{"changed":true}')
        after=self.inventory.capture();self.assertNotEqual(before['files'],after['files'])
        self.host['endpoint']='unix:///foreign'
        with self.assertRaisesRegex(ReadFailure,'DAEMON_CHANGED'):self.inventory.capture()

    def test_double_collection_refuses_new_or_restarted_protected_container(self):
        real=self.inventory.once;count=0
        def collect():
            nonlocal count
            count+=1
            if count==2:self.active['e'*64]['started']='restarted'
            return real()
        self.inventory.once=collect
        with self.assertRaisesRegex(ReadFailure,'INVENTORY_UNSTABLE'):self.inventory.capture()

    def test_missing_or_duplicate_fixture_set_refused(self):
        original=deepcopy(self.metas);self.metas.pop(next(iter(self.metas)))
        with self.assertRaises(ReadFailure):self.inventory.capture()
        self.metas.update(original);first,last=list(self.metas)[0],list(self.metas)[-1]
        self.metas[last]['identity'].update(project=self.metas[first]['identity']['project'],service=self.metas[first]['identity']['service'])
        with self.assertRaises(ReadFailure):self.inventory.capture()


if __name__=='__main__':unittest.main()
