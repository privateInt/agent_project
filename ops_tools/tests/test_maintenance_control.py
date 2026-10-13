"""Production review: real private files, validated synthetic metadata; no service control."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops_tools import maintenance_control as m
from ops_tools.tests import test_plan_backup_capacity as capacity_tests
from ops_candidates.maintenance_20260930.tests import test_maintenance as maintenance_fixture

bc, reviewed, runtime, core, cap = m.dependencies(m.ROOT)
fixtures = capacity_tests.fixtures


class FakeDocker(capacity_tests.Fake):
    def __init__(self, fixture):
        super().__init__(fixture); self.rows = {}; self.extra = {'e'*64:'unrelated-running'}
    def host(self): return dict(super().host(), daemon_id='fixture-daemon')
    def containers_for(self, component, owner):
        rows = super().containers_for(component, owner)
        self.rows[(component,owner)] = deepcopy(rows)
        return rows
    def running(self):
        return dict(self.extra, **{x['id']:x['started_at'] for rows in self.f.rows.values() for x in rows})


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.f = fixtures.Fixture(Path(self.tmp.name))
        self.addCleanup(lambda:sys.modules.pop('lf_common',None))
        self.docker = FakeDocker(self.f)
        self.inv = m.Inventory(self.f.root,self.f.lf,self.f.ops,cap,self.docker)

    def test_discovered_projects_and_protected_other_running_no_writes_or_key_reads(self):
        for rows in self.f.rows.values():
            for row in rows: row['project']='user-renamed-'+row['project']
        before=fixtures.snapshot(Path(self.tmp.name)); opened=[]; read=cap.bp.read_small
        def safe(path,*a,**kw):
            opened.append(str(path)); self.assertNotIn(str(path),[str(self.f.ops/'access-key'),str(self.f.lf/'settings.json')])
            self.assertFalse(str(path).endswith(('api-token','langfuse-tracing.json','postgres.env','worker.env')))
            return read(path,*a,**kw)
        with patch.object(cap.bp,'read_small',safe): result=self.inv.capture()
        self.assertEqual(len(result['inventory']['scope']),9)
        self.assertEqual(result['inventory']['protected'],self.docker.extra)
        self.assertTrue(all(x['compose_project'].startswith('user-renamed-') for x in result['evidence'].values()))
        self.assertNotIn(fixtures.SECRET,json.dumps(result)); self.assertEqual(self.docker.exec_calls,0)
        self.assertEqual(before,fixtures.snapshot(Path(self.tmp.name)))

    def test_extra_exporter_and_business_containers_remain_protected(self):
        self.docker.extra.update({'c'*64:'exporter','d'*64:'business'})
        self.assertEqual(len(self.inv.capture()['inventory']['protected']),3)

    def test_ambiguous_same_owner_core_service_refused(self):
        row=deepcopy(self.f.rows['ops'][0]); row['id']='a'*64; self.f.rows['ops'].append(row)
        with self.assertRaisesRegex(m.Failure,'OWNED_SCOPE_UNVERIFIED'):self.inv.capture()

    def test_wrong_owner_image_mount_and_stopped_core_refused(self):
        for field,value in (('owner','foreign'),('image_ref','foreign:image'),('state','exited'),('mounts',[])):
            with self.subTest(field=field):
                row=self.f.rows['ops'][0]; old=deepcopy(row[field]); row[field]=value
                with self.assertRaisesRegex(m.Failure,'OWNED_SCOPE_UNVERIFIED'):self.inv.capture()
                row[field]=old

    def test_disappearing_or_restarted_core_not_adopted(self):
        live=self.docker.running
        self.docker.running=lambda:{k:v for k,v in live().items() if k!=self.f.rows['ops'][0]['id']}
        with self.assertRaisesRegex(m.Failure,'SCOPED_CONTAINER_CHANGED'):self.inv.capture()

    def test_external_key_symlink_refused_without_permission_repair(self):
        p=self.f.root/'agent_platform/.secrets/api-token'; raw=p.read_bytes();p.unlink();p.symlink_to(self.f.ops/'access-key')
        with self.assertRaises(Exception):self.inv.capture()
        self.assertTrue(p.is_symlink());self.assertEqual((self.f.ops/'access-key').read_bytes(),raw)

    def test_live_cache_change_excluded_but_config_and_key_metadata_retained(self):
        a=self.inv.capture();fixtures.write(self.f.ops/'cache/api-success.json',{'updated':True});b=self.inv.capture()
        self.assertEqual(a,b)
        self.assertIn(str(self.f.ops/'access-key'),a['files'])
        self.assertIn(str(self.f.ops/'host-config.json'),a['files'])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        f=maintenance_fixture.MaintenanceTests('test_plan_readonly_healthy_boot_history_and_new_scope');f.setUp()
        self.addCleanup(f.doCleanups);self.f=f
        self.r=m.Review.__new__(m.Review);self.r.r=runtime;self.r.root=m.ROOT;self.r.sources=reviewed
        self.r.directory=f.directory;self.r.backend=f.manager
        self.r.controller=core.Maintenance(f.directory,f.manager,inventory_fn=lambda:deepcopy(self.r.current['inventory']),
            snapshot_fn=lambda _:deepcopy(f.fixture.binding),history_fn=lambda _:f.history,
            environment_fn=lambda *a:None)
        self.snapshot={'inventory':{'scope':{'ops-web':deepcopy(f.inventory['scope']['ops'])},
                                    'protected':deepcopy(f.inventory['protected'])},
                       'host':{'daemon_id':f.fixture.binding['daemon_id']},'files':{},'evidence':{'ops-web':{}}}
        self.r.inventory=SimpleNamespace(capture=lambda:deepcopy(self.snapshot));self.r.current=deepcopy(self.snapshot)

    def test_plan_actual_core_gates_without_journal_mutation_or_control(self):
        before=fixtures.snapshot(self.f.fixture.root);commands=self.f.manager.control_commands
        result=self.r.plan();self.assertEqual(result['result'],'passed');self.assertEqual(len(result['review_id']),64)
        self.assertFalse(result['review_authorizes_execution']);self.assertEqual(result['plan_wait']['attempts'],1)
        self.assertEqual(before,fixtures.snapshot(self.f.fixture.root));self.assertFalse(self.r.controller.jobs.exists())
        self.assertEqual(commands,self.f.manager.control_commands)

    def test_real_constructor_reads_existing_owner_and_pinned_sources_without_install(self):
        before=fixtures.snapshot(self.f.fixture.root);commands=self.f.manager.control_commands
        with patch.object(runtime.probe,'systemd',return_value={'status':'ok','linger':True}),\
             patch.object(m,'read_only_type',return_value=lambda *a:self.f.manager):
            review=m.Review(self.f.directory,self.f.fixture.root/'unused-langfuse')
            result=review.status()
        self.assertEqual(result['result'],'passed');self.assertEqual(result['jobs'],[])
        self.assertEqual(before,fixtures.snapshot(self.f.fixture.root));self.assertEqual(commands,self.f.manager.control_commands)

    def test_changed_protected_container_between_scans_refuses_plan(self):
        b=deepcopy(self.snapshot);b['inventory']['protected']['e'*64]='new-start'
        self.r.inventory.capture=unittest.mock.Mock(side_effect=[self.snapshot,b])
        with self.assertRaisesRegex(m.Failure,'DISCOVERY_CHANGED'):self.r.plan_once()

    def test_changed_scope_or_configuration_between_scans_refuses_plan(self):
        for key in ('inventory','files'):
            b=deepcopy(self.snapshot)
            if key=='inventory':b[key]['scope']['ops-web']['image_id']='sha256:'+'a'*64
            else:b[key]['changed']=[1,2,3]
            self.r.inventory.capture=unittest.mock.Mock(side_effect=[self.snapshot,b])
            with self.assertRaisesRegex(m.Failure,'DISCOVERY_CHANGED'):self.r.plan_once()

    def test_daemon_and_ops_binding_are_not_rebound(self):
        for key in ('daemon','ops'):
            b=deepcopy(self.snapshot)
            if key=='daemon':b['host']['daemon_id']='foreign'
            else:b['inventory']['scope']['ops-web']['container_id']='0'*64
            self.r.inventory.capture=lambda:deepcopy(b)
            with self.assertRaisesRegex(m.Failure,'BINDING_CHANGED'):self.r.plan_once()

    def test_explicit_stop_and_recovery_never_wait_or_clear(self):
        for desired,recovery in (('paused',None),('running','OTHER_CONTAINER_CHANGED')):
            control=self.f.store.control();control.update(desired=desired,recovery=recovery)
            runtime.ws.write(self.f.store.path/'control.json',control)
            with self.assertRaises(Exception):self.r.plan(sleep=lambda _:self.fail('must not wait'))
            result=self.r.status();self.assertTrue(result['review_blocked']);self.assertEqual(result['result'],'attention_required')
            self.assertEqual(result['recovery_required'],bool(recovery))
            self.assertEqual(self.f.store.control(),control)

    def test_unfinished_job_blocks_new_plan_and_status_does_not_replay(self):
        p=self.r.controller.plan();self.r.controller.save(p,'operation_running')
        before=fixtures.snapshot(self.f.fixture.root);commands=self.f.manager.control_commands
        with self.assertRaisesRegex(Exception,'UNRESOLVED_JOB'):self.r.plan()
        result=self.r.status();self.assertEqual(result['unresolved_jobs'],1)
        self.assertFalse(result['saved_records_authorize_replay']);self.assertEqual(before,fixtures.snapshot(self.f.fixture.root))
        self.assertEqual(commands,self.f.manager.control_commands)

    def test_historical_return_never_substitutes_for_current_health(self):
        p=self.r.controller.plan();self.r.controller.save(p,'returned');self.f.manager.active=False
        result=self.r.status();self.assertTrue(result['jobs'][0]['historical_return_only'])
        self.assertFalse(result['current_health_verified']);self.assertEqual(result['result'],'attention_required')

    def test_foreign_job_symlink_and_private_mode_refused_without_repair(self):
        p=self.r.controller.plan();self.r.controller.save(p,'returned');path=self.r.controller.path(p['id'])
        path.chmod(0o664)
        with self.assertRaises(Exception):self.r.status()
        self.assertEqual(path.stat().st_mode&0o777,0o664)
        path.chmod(0o600);raw=path.read_bytes();path.unlink();other=path.parent/'foreign';other.write_bytes(raw)
        path.symlink_to(other)
        with self.assertRaises(Exception):self.r.status()
        self.assertTrue(path.is_symlink())

    def test_contention_only_wait_is_bounded_and_does_not_control(self):
        now=[0];sleep=lambda seconds:now.__setitem__(0,now[0]+seconds)
        self.r.plan_once=unittest.mock.Mock(side_effect=[m.Failure('PREPARED_STATE_BUSY'),{'result':'passed'}])
        result=self.r.plan(clock=lambda:now[0],sleep=sleep);self.assertEqual(result['plan_wait']['attempts'],2)
        self.r.plan_once=unittest.mock.Mock(side_effect=m.Failure('PREPARED_STATE_BUSY'))
        with self.assertRaisesRegex(m.Failure,'PREPARED_STATE_BUSY'):self.r.plan(clock=lambda:now[0],sleep=sleep)
        self.assertEqual(now[0],31)

    def test_active_intent_waits_only_verified_in_progress_worker(self):
        self.r.plan_once=unittest.mock.Mock(side_effect=[m.Failure('WORKER_UNFINISHED_INTENT'),{'result':'passed'}])
        self.r.state=lambda:dict(current_invocation_verified=True,evaluation_status='in_progress',desired_state='running',recovery=None)
        self.assertEqual(self.r.plan(sleep=lambda _:None)['plan_wait']['attempts'],2)
        self.r.plan_once=unittest.mock.Mock(side_effect=m.Failure('WORKER_UNFINISHED_INTENT'))
        self.r.state=lambda:dict(current_invocation_verified=False,evaluation_status='in_progress',desired_state='running',recovery=None)
        with self.assertRaisesRegex(m.Failure,'UNFINISHED_INTENT'):self.r.plan(sleep=lambda _:self.fail('no wait'))

    def test_current_evaluation_is_a_review_gate_not_a_recovery_fault(self):
        self.r.state=lambda:dict(current_invocation_verified=True,evaluation_status='in_progress',desired_state='running',recovery=None)
        with patch.object(self.r.controller.store,'optional',return_value={'phase':'open'}):result=self.r.status()
        self.assertTrue(result['evaluation_in_progress']);self.assertTrue(result['review_blocked'])
        self.assertFalse(result['recovery_required']);self.assertFalse(result['current_health_verified'])


class BoundaryTests(unittest.TestCase):
    def test_changed_reviewed_source_refused_before_project_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p=root/'ops_tools/boot_control.py';p.parent.mkdir();p.write_text("raise RuntimeError('must not import')")
            with self.assertRaisesRegex(m.Failure,'REVIEWED_SOURCE_CHANGED'):m.dependencies(root)

    def test_root_and_symlink_source_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'source';p.write_text('safe');link=Path(temp)/'link';link.symlink_to(p)
            with self.assertRaisesRegex(m.Failure,'SOURCE_SYMLINK'):m.source(link)

    def test_command_allowlist_refuses_mutations_and_raw_env_before_runner(self):
        calls=[];d=m.docker_type(cap,runtime.probe)(runner=lambda *a:(calls.append(a) or (0,b'',b'')))
        d.base.endpoint='unix:///discovered/socket'
        for args in (['stop','a'*64],['exec','a'*64,'cat','key'],['container','inspect','--format','{{json .Config.Env}}','a'*64]):
            with self.assertRaisesRegex(m.Failure,'COMMAND_REFUSED'):d.query(args)
        self.assertEqual(calls,[])

    def test_actual_readonly_systemd_inspect_accepts_absence_and_refuses_control(self):
        unit='agent-project-ops-refresh-'+'a'*32+'.service';calls=[]
        reader=SimpleNamespace(env={},original={},paths={'systemctl':'/usr/bin/systemctl'},calls=0)
        def command(args,**kw):
            calls.append(args)
            return SimpleNamespace(returncode=4,stdout='Id='+unit+'\nLoadState=not-found\nActiveState=inactive\nMainPID=0\nInvocationID=\n',stderr='')
        reader.runner=command
        backend=m.read_only_type(runtime.adapter.Systemd)(reader,{'status':'ok'},unit)
        self.assertEqual(backend.inspect()['LoadState'],'not-found')
        count=len(calls)
        for action in (backend.reload,backend.start,backend.stop,backend.reset_failed,lambda:backend.verify({})):
            with self.assertRaisesRegex(m.Failure,'CONTROL_NOT_AVAILABLE'):action()
        self.assertEqual(len(calls),count);self.assertEqual(backend.control_commands,0)

    def test_cli_has_no_mutating_action_or_secret_error_output(self):
        with patch.object(sys,'argv',['maintenance_control.py','plan']),patch.object(m,'Review',side_effect=RuntimeError(fixtures.SECRET)):
            import io
            with patch.object(sys,'stdout',new_callable=io.StringIO) as stream:
                self.assertEqual(m.main(),2);report=json.loads(stream.getvalue())
        self.assertNotIn(fixtures.SECRET,json.dumps(report))
        self.assertEqual(report['effects']['files_written'],[])
        self.assertEqual(report['capabilities'],dict(plan=True,status=True,stop=False,return_worker=False,recover=False,backup=False))

    def test_batched_running_metadata_refuses_missing_stopped_or_duplicate(self):
        d=m.docker_type(cap,runtime.probe)();d.query=lambda args:'a'*64 if args[0]=='ps' else json.dumps(['a'*64,True,'started'])
        self.assertEqual(d.running(),{'a'*64:'started'})
        for text in ('',json.dumps(['a'*64,False,'started']),json.dumps(['b'*64,True,'started'])):
            d.query=lambda args:'a'*64 if args[0]=='ps' else text
            with self.assertRaisesRegex(m.Failure,'RUNNING_SET_CHANGED'):d.running()

    def test_nondefault_local_socket_and_context_precedence_without_parent_change(self):
        for env,wanted in (({'DOCKER_HOST':'unix:///custom/rootful.sock'},'unix:///custom/rootful.sock'),
                           ({'DOCKER_CONTEXT':'owned','DOCKER_HOST':'unix:///ignored.sock'},'unix:///context.sock')):
            with patch.dict(os.environ,env,clear=True):
                d=m.docker_type(cap,runtime.probe)();commands=[]
                d.base._command=lambda args,*a:(commands.append(args) or ('current' if args==['context','show'] else 'unix:///context.sock'))
                d.query=lambda args:json.dumps(['/tmp/docker','host','x86_64','linux',[],'daemon'])
                self.assertEqual(d.host()['endpoint'],wanted);self.assertEqual(dict(os.environ),env)

    def test_remote_rootless_and_user_namespace_are_not_assumed_supported(self):
        for endpoint,security in (('tcp://remote:2375',[]),('unix:///custom.sock',['rootless']),('unix:///custom.sock',['userns'])):
            with patch.dict(os.environ,{'DOCKER_HOST':endpoint},clear=True):
                d=m.docker_type(cap,runtime.probe)();d.base._command=lambda *a:'unix:///default.sock'
                d.query=lambda args:json.dumps(['/tmp/docker','host','amd64','linux',security,'daemon'])
                with self.assertRaises(Exception):d.host()


if __name__=='__main__':unittest.main()
