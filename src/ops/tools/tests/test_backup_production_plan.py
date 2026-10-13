"""New production preparation/metadata connection; no production mutations."""
from copy import deepcopy
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.ops.tools import backup_setup as s, backup_plan as p
from src.ops.tools.tests import test_plan_backup_capacity as captest
from src.ops.tools.tests import test_maintenance_control as mt
from src.ops.candidates.backup_generation_20261001 import generation as g

TOOLS=Path(os.environ.get('BACKUP_TEST_AGE_DIR','/nonexistent'))


def cached(directory):
    receipt=json.loads((TOOLS/'tool-receipt.json').read_text())
    for name in ('age','age-keygen'):
        shutil.copyfile(TOOLS/name,directory/name);(directory/name).chmod(0o700)
    return receipt


@unittest.skipUnless((TOOLS/'age-keygen').is_file(),'native pinned age test tools unavailable')
class Preparation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.paths=s.paths(self.home)
    def prepare(self):return s.prepare(self.home,fetch=cached)
    def test_new_native_identity_private_destination_and_no_private_output(self):
        result=self.prepare();self.assertTrue(result['new_identity_created'])
        self.assertIn('age1',result['recipient']);self.assertNotIn('AGE-SECRET-KEY',json.dumps(result))
        self.assertEqual(self.paths['identity'].stat().st_mode&0o777,0o600)
        for name in ('control','destination','tools','keys'):
            self.assertEqual(self.paths[name].stat().st_mode&0o777,0o700)
        self.assertFalse(result['identity_custody_verified']);self.assertFalse(result['backup_ready'])
    def test_repeat_and_status_do_not_read_rotate_or_execute_keygen(self):
        self.prepare();before=s.signature(self.paths['identity']);read=Path.read_bytes;text=Path.read_text
        def body(path,*a,**kw):
            self.assertNotEqual(path,self.paths['identity']);return read(path,*a,**kw)
        def chars(path,*a,**kw):
            self.assertNotEqual(path,self.paths['identity']);return text(path,*a,**kw)
        with patch.object(Path,'read_bytes',body),patch.object(Path,'read_text',chars),patch.object(subprocess,'run',side_effect=AssertionError('process')):
            self.assertFalse(self.prepare()['new_identity_created']);s.prepared(self.home)
        self.assertEqual(before,s.signature(self.paths['identity']))
    def test_existing_destination_contents_refused_without_deletion(self):
        self.paths['destination'].mkdir(parents=True,mode=0o700);self.paths['parent'].chmod(0o700)
        (self.paths['destination']/'prior').write_text('keep')
        with self.assertRaisesRegex(s.Failure,'NOT_EMPTY'):self.prepare()
        self.assertEqual((self.paths['destination']/'prior').read_text(),'keep')
    def test_foreign_or_nonprivate_parent_not_repaired(self):
        self.paths['parent'].mkdir(parents=True,mode=0o700);self.paths['parent'].chmod(0o775)
        with self.assertRaisesRegex(s.Failure,'PRIVATE_PATH'):self.prepare()
        self.assertEqual(self.paths['parent'].stat().st_mode&0o777,0o775)
    def test_symlink_destination_refused(self):
        self.paths['parent'].mkdir(parents=True,mode=0o700)
        self.paths['destination'].symlink_to(self.home,target_is_directory=True)
        with self.assertRaisesRegex(s.Failure,'SYMLINK'):self.prepare()
        self.assertTrue(self.paths['destination'].is_symlink())
    def test_partial_download_preserved_and_never_automatically_repeated(self):
        def fail(directory):
            (directory/'partial').write_text('keep');raise s.Failure('DOWNLOAD_FAILED')
        with self.assertRaisesRegex(s.Failure,'DOWNLOAD_FAILED'):s.prepare(self.home,fetch=fail)
        with self.assertRaisesRegex(s.Failure,'INCOMPLETE'):self.prepare()
        self.assertEqual((self.paths['tools']/'partial').read_text(),'keep')
    def test_failure_after_key_generation_keeps_key_and_cannot_rotate(self):
        original=s.durable_new
        def fail(path,value):
            if path==self.paths['config']:raise OSError('disk failure')
            return original(path,value)
        with patch.object(s,'durable_new',fail),self.assertRaises(OSError):self.prepare()
        original_key=s.signature(self.paths['identity'])
        with self.assertRaisesRegex(s.Failure,'INCOMPLETE'):self.prepare()
        self.assertEqual(s.signature(self.paths['identity']),original_key)
    def test_changed_key_metadata_and_hardlink_refused(self):
        self.prepare();self.paths['identity'].chmod(0o400)
        with self.assertRaisesRegex(s.Failure,'IDENTITY_METADATA'):s.prepared(self.home)
        self.paths['identity'].chmod(0o600);os.link(self.paths['identity'],self.home/'alias')
        with self.assertRaisesRegex(s.Failure,'HARDLINK'):s.prepared(self.home)
    def test_public_recipient_single_record_change_is_refused(self):
        self.prepare();record=s.json_read(self.paths['config']);record['recipient']=record['recipient'].replace('age1','age2')
        self.paths['config'].write_text(json.dumps(record))
        with self.assertRaises(Exception):s.prepared(self.home)
    def test_tool_digest_cannot_be_adopted_from_modified_public_record(self):
        self.prepare();record=s.json_read(self.paths['config'])
        (self.paths['tools']/'age').write_text('changed');record['tools']['binaries']['age']=s.sha(self.paths['tools']/'age')
        record['age_sha256']=record['tools']['binaries']['age'];self.paths['config'].write_text(json.dumps(record))
        with self.assertRaisesRegex(s.Failure,'TOOL_RELEASE'):s.prepared(self.home)
    def test_real_second_process_lock_blocks_preparation(self):
        self.prepare();fd=os.open(self.paths['control']/'prepare.lock',os.O_RDWR)
        self.addCleanup(os.close,fd);fcntl.flock(fd,fcntl.LOCK_EX)
        result=subprocess.run([os.sys.executable,'-B',str(s.ROOT/'src/ops/tools/backup_setup.py'),'prepare'],
                  env=dict(os.environ,HOME=str(self.home)),capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2);self.assertEqual(json.loads(result.stdout)['code'],'BACKUP_PREPARATION_BUSY')
    def test_prepared_destination_inode_replacement_refused(self):
        self.prepare();self.paths['destination'].rename(self.paths['parent']/'old')
        self.paths['destination'].mkdir(mode=0o700)
        with self.assertRaisesRegex(s.Failure,'DESTINATION_CHANGED'):s.prepared(self.home)
    def test_new_production_named_identity_encrypts_and_decrypts_synthetic_data(self):
        result=self.prepare();message=b'private synthetic backup fixture'
        encrypted=subprocess.run([str(self.paths['tools']/'age'),'-r',result['recipient']],
                                 input=message,capture_output=True,check=True,timeout=10)
        plain=subprocess.run([str(self.paths['tools']/'age'),'-d','-i',str(self.paths['identity'])],
                             input=encrypted.stdout,capture_output=True,check=True,timeout=10)
        self.assertEqual(plain.stdout,message);self.assertNotIn(b'AGE-SECRET-KEY',encrypted.stderr+plain.stderr)


class ColdDocker(mt.FakeDocker):
    def detail(self,ident):
        row=next(x for rows in self.f.rows.values() for x in rows if x['id']==ident)
        return [ident,row['image_id'],dict(StartedAt=row['started_at'],Running=True,Status='running',Pid=123),
                '', 'unless-stopped']


@unittest.skipUnless((TOOLS/'age-keygen').is_file(),'native pinned age test tools unavailable')
class ProductionMapping(unittest.TestCase):
    def setUp(self):
        fixture=captest.Workflow('test_full_real_metadata_workflow_no_mutation_or_secret_read');fixture.setUp()
        self.addCleanup(fixture.tearDown);self.f=fixture.f;self.cap=mt.cap
        # Fake metadata uses actual private actor files; no Docker process.
        self.docker=ColdDocker(self.f)
        self.home=fixture.top/'selected-home';self.home.mkdir(mode=0o700)
        self.preparation=s.prepare(self.home,fetch=cached)
        owner=self.cap.bp.read_small(self.f.ops/'owner.json')
        mgmt=self.f.ops.parent/'auto-refresh'/owner['owner'];mgmt.mkdir(parents=True,mode=0o700)
        mgmt.parent.chmod(0o700)
        unit=fixture.top/'owned.service';captest.fixtures.write(unit,'[Service]\n',0o600)
        unit2=unit.with_name('agent-project-ops-refresh-'+owner['owner'].replace('-','')+'.service')
        unit.rename(unit2)
        captest.fixtures.write(mgmt/'service.json',dict(binding=dict(owner=owner,directory=str(self.f.ops)),unit_path=str(unit2)))
        self.unit=unit2
        hosts=[]
        for i in range(3):
            path=fixture.top/('host-config-'+str(i));captest.fixtures.write(path,'metadata fixture');hosts.append(path)
        original=self.cap.host_config_paths
        self.cap.host_config_paths=lambda:hosts;self.addCleanup(setattr,self.cap,'host_config_paths',original)
        self.r=SimpleNamespace(root=self.f.root,langfuse=self.f.lf,directory=self.f.ops,cap=self.cap,docker=self.docker,
                               manifest={'unit_name':unit2.name},sources={'code_state':'candidate'})
        self.state=dict(current_invocation_verified=True,desired_state='running',recovery=None,
                        provider_status={'InvocationID':'current'})
        self.maintenance=dict(review_id='1'*64,management=deepcopy(self.state))
        self.r.plan=lambda:deepcopy(self.maintenance);self.r.state=lambda:deepcopy(self.state)
        self.plan=p.Plan.__new__(p.Plan);self.plan.root=self.f.root;self.plan.home=self.home;self.plan.langfuse=self.f.lf
        self.plan.preparation=s.prepared(self.home);self.plan.generation=g;self.plan.review=self.r
    def test_complete_fresh_source_image_uid_mapping_readonly_no_secret_bodies(self):
        before=captest.fixtures.snapshot(self.f.root);opened=[];read=self.cap.bp.read_small
        def inspect(path,*a,**kw):
            opened.append(str(path));self.assertNotIn(Path(path).name,('access-key','api-token','langfuse-tracing.json','settings.json'))
            self.assertFalse(Path(path).parent==self.f.lf and str(path).endswith('.env'))
            return read(path,*a,**kw)
        with patch.object(self.cap.bp,'read_small',inspect):result,inv,_=self.plan.snapshot()
        self.assertEqual(len(result['cold']['services']),9);self.assertEqual(len(result['cold']['stores']),8)
        self.assertEqual(len([k for k in result['source_dispositions'] if k.startswith('image:')]),12)
        self.assertEqual(result['source_dispositions']['ops-cache/api-success.json'],'storage-ops-server-runtime')
        self.assertEqual(result['source_dispositions']['alertmanager-owner'],'storage-alertmanager')
        self.assertNotIn(captest.fixtures.SECRET,json.dumps(result));self.assertEqual(self.docker.exec_calls,0)
        self.assertEqual(before,captest.fixtures.snapshot(self.f.root));g.validate(result)
    def test_plan_connects_actual_mapper_and_never_claims_execution_or_custody(self):
        with patch.object(p,'capacity',return_value={'actual_source_size_measured':False}):result=self.plan.plan()
        self.assertEqual(result['result'],'passed');self.assertEqual(result['source_count'],22)
        self.assertFalse(result['backup_ready']);self.assertFalse(result['production_execution_adapter_available'])
        self.assertFalse(result['identity_custody_verified']);self.assertTrue(result['production_generation_plan_connected'])
        self.assertEqual(result['protected_running_count'],4) # three exporter containers plus unrelated business
    def test_cold_metadata_files_are_not_double_captured_in_actor_file_tree(self):
        result,_,_=self.plan.snapshot()
        self.assertFalse(any(Path(x['path']).is_relative_to(self.f.ops/'cache') for x in result['files'].values()))
        self.assertEqual(result['files']['ops-server']['exclude_children'],['cache'])
        self.assertEqual(result['files']['langfuse-records']['exclude_children'],['data'])
    def test_image_owner_mount_and_running_changes_block_readonly_mapping(self):
        for field,value in (('image_ref','wrong'),('owner','foreign'),('mounts',[]),('state','exited')):
            with self.subTest(field=field):
                row=self.f.rows['ops'][0];old=deepcopy(row[field]);row[field]=value
                with self.assertRaises(Exception):self.plan.snapshot()
                row[field]=old
    def test_detail_restarted_container_is_not_adopted(self):
        detail=self.docker.detail
        def changed(ident):
            value=detail(ident);value[2]['StartedAt']='changed';return value
        self.docker.detail=changed
        with self.assertRaisesRegex(s.Failure,'CONTAINER_CHANGED'):self.plan.snapshot()
    def test_unknown_health_and_userns_fail_before_capture_plan(self):
        detail=self.docker.detail
        for field,value in ((2,{'Running':True,'Status':'running','Pid':123,'StartedAt':'synthetic-start','Health':{'Status':'starting'}}),(3,'remap')):
            def changed(ident):
                out=detail(ident);out[field]=value;return out
            self.docker.detail=changed
            with self.assertRaises(Exception):self.plan.snapshot()
        self.docker.detail=detail
    def test_two_metadata_samples_must_agree_without_rebinding(self):
        snapshot=self.plan.snapshot;count=0
        def changed():
            nonlocal count
            value,inv,host=snapshot();count+=1
            if count==2:value['cold']['protected']['a'*64]='new'
            return value,inv,host
        self.plan.snapshot=changed
        with self.assertRaisesRegex(s.Failure,'DISCOVERY_CHANGED'):self.plan.plan()
    def test_destination_overlap_is_rejected_before_write_or_service_control(self):
        self.plan.preparation['paths']['destination']=str(self.f.ops/'cache')
        with self.assertRaisesRegex(Exception,'OVERLAP'):self.plan.snapshot()
    def test_stopped_worker_and_changed_invocation_cannot_finish_plan(self):
        self.state['desired_state']='paused'
        with self.assertRaisesRegex(s.Failure,'WORKER_CHANGED'):self.plan.plan()
    def test_reserve_envelope_counts_every_artifact(self):
        with patch.object(p,'capacity',side_effect=lambda dest,budget,bp:{'budget':budget}):result=self.plan.plan()
        budgets=result['stream_budgets'];self.assertGreater(result['capacity']['budget'],
                budgets['per_storage_bytes']+budgets['per_file_tree_bytes']+budgets['per_image_export_bytes'])
    def test_new_preparation_and_destination_are_not_inside_captured_sources(self):
        result,_,_=self.plan.snapshot()
        for role,item in result['files'].items():
            self.assertFalse(Path(self.preparation['destination']).is_relative_to(item['path']))
    def test_current_cache_timestamp_changes_do_not_invalidate_source_map(self):
        a,_,_=self.plan.snapshot();captest.fixtures.write(self.f.ops/'cache/api-success.json',{'updated':True});b,_,_=self.plan.snapshot()
        self.assertEqual(p.contract(a,self.maintenance,self.plan.preparation),p.contract(b,self.maintenance,self.plan.preparation))


class Boundaries(unittest.TestCase):
    def test_source_pin_change_is_refused_before_project_module_execution(self):
        with patch.object(s,'sha',return_value='0'*64),self.assertRaisesRegex(s.Failure,'SOURCE_CHANGED'):s.reviewed()
    def test_path_traversal_and_control_characters_refused(self):
        for path in ('/','/tmp/../etc','/tmp/line\nkey'):
            with self.assertRaises(s.Failure):s.safe(path)
    def test_dependency_order_stops_clients_before_backends_and_returns_reverse(self):
        order=g.s.stop_order(p.DEPENDENCIES,p.DEPENDENCIES)
        self.assertLess(order.index('lf-web'),order.index('lf-worker'))
        self.assertLess(order.index('lf-worker'),order.index('lf-postgres'))
        self.assertLess(order.index('ops-web'),order.index('obs-prometheus'))
    def test_plan_cli_has_no_capture_stop_or_key_generation_action(self):
        result=subprocess.run([os.sys.executable,'-B',str(p.ROOT/'src/ops/tools/backup_plan.py'),'capture'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn("invalid choice",result.stderr)
    def test_missing_preparation_fails_before_manager_or_inventory_module_load(self):
        with tempfile.TemporaryDirectory() as home,patch.object(p,'load',side_effect=AssertionError('project load')):
            with self.assertRaises(FileNotFoundError):p.Plan(Path(home)/'server',Path(home)/'langfuse',Path(home))


@unittest.skipUnless(os.environ.get('PREFLIGHT_DOCKER_TEST_BINARY'),'native Docker CLI against synthetic Engine only')
class NativeCLI(unittest.TestCase):
    def setUp(self):
        from src.ops.tools.tests.test_cold_storage_acceptance import NativeCLI as Fixture
        self.f=Fixture('test_real_cli_service_projection_handles_missing_health_without_env');self.f.setUp()
        self.addCleanup(self.f.doCleanups)
    def project(self):
        result=self.f.invoke(['container','inspect','--format',p.DETAIL_FORMAT,self.f.ident])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.f.calls))
        return json.loads(result.stdout)
    def test_real_cli_healthless_metadata_contains_no_config_environment(self):
        values=self.project();self.assertEqual(values[0],self.f.ident);self.assertIsNone(values[2]['Health'])
        self.assertNotIn('Config',json.dumps(values));self.assertEqual(values[4],'no')
    def test_real_cli_healthy_and_starting_are_separately_projected(self):
        for state in ('healthy','starting'):
            self.f.value['State']['Health']={'Status':state,'Log':[{'Output':'SYNTHETIC_DO_NOT_PRINT'}]}
            self.assertEqual(self.project()[2]['Health']['Status'],state)
    def test_real_cli_exited_state_and_user_namespace_remain_visible(self):
        self.f.value['State'].update(Status='exited',Running=False,Pid=0)
        self.f.value['HostConfig']['UsernsMode']='remap'
        values=self.project();self.assertFalse(values[2]['Running']);self.assertEqual(values[3],'remap')


if __name__=='__main__':unittest.main()
