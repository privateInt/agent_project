"""Candidate core only: synthetic boot IDs/manager/Docker, real files and children."""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from ops_platform.contracts import ReadFailure
from ops_platform.ops import boot_lifecycle as boot, refresh_worker as worker, worker_state as ws, manage
from ops_platform.ops import lifecycle_probe as probe


class MetadataFixture:
    """Stdlib-only prepared metadata; no web dependency, server or real daemon."""
    def __init__(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ops-boot-metadata-');self.root=Path(self.temp.name)
        self.directory=self.root/'server';self.directory.mkdir(mode=0o700)
        (self.directory/'cache').mkdir(mode=0o700)
        manage.write_new(self.root/'agent-key','synthetic-agent-key')
        manage.write_new(self.root/'langfuse.json',{'project_id':'synthetic','public_key':'synthetic','secret_key':'synthetic'})
        manage.write_new(self.directory/'access-key','synthetic-access-key-for-private-test-only')
        # A temporary local port is metadata only; no listener/upstream is started.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url='http://127.0.0.1:'+str(port)
        cfg={'schema':1,'platforms':{role:{'url':url} for role in ('model','data','knowledge','agent')},
             'prometheus_url':url,'langfuse_url':url,'langfuse_file':str(self.root/'langfuse.json'),
             'public_url':url,'bind':'0.0.0.0','port':port,'timeout':5,'tls_cert':None,'tls_key':None}
        cfg['platforms']['agent']['token_file']=str(self.root/'agent-key')
        for name,value in manage.state_documents(self.directory,cfg,str(uuid.uuid4())).items():
            manage.write_new(self.directory/name,value)
        owner=ws.read(self.directory/'owner.json')
        mounts=[(self.directory/'config.json','/run/ops/config.json',False),
                (self.directory/'access-key','/run/ops/access-key',False),
                (self.root/'agent-key','/run/ops/agent-token',False),
                (self.root/'langfuse.json','/run/ops/langfuse.json',False),
                (self.directory/'cache','/var/lib/ops',True)]
        self.raw={'id':'a'*64,'image':'sha256:'+'b'*64,'created':'2026-01-01T00:00:00Z',
                  'started':'2026-01-01T00:00:01Z','running':True,'health':'healthy',
                  'project':'agent-project-ops','service':'ops-web','owner':owner['owner'],
                  'user':str(os.getuid())+':'+str(os.getgid()),'network':'host','readonly':True,
                  'mounts':[{'Type':'bind','Source':str(src),'Destination':dest,'RW':rw} for src,dest,rw in mounts]}
        self.calls=[];self.ids=[self.raw['id']]
        self.reader=probe.Reader(env={'PATH':'/synthetic/bin'},runner=self.command,
                                 which=lambda name,**kwargs:'/synthetic/bin/'+name)

    def command(self,args,**kwargs):
        tail=args[1:];assert Path(args[0]).name=='docker' and probe.allow('docker',tail)
        self.calls.append(('docker',tail,kwargs))
        if tail==['context','show']:out='synthetic'
        elif tail[:2]==['context','inspect']:out='unix:///synthetic/docker.sock'
        elif tail[2]=='info':out=json.dumps({'id':'synthetic-daemon','os':'linux','security':[],'version':'28.0.0'})
        elif tail[2]=='compose':out='2.39.0'
        elif tail[2]=='ps':out='\n'.join(self.ids)
        else:out=json.dumps(self.raw)
        return SimpleNamespace(returncode=0,stdout=out,stderr='')


def accepted(*_): return {'result':'passed','classification':'accepted','generation':None}


def signal_after_evaluation(*_):
    os.kill(os.getppid(), signal.SIGTERM); time.sleep(.12)
    return accepted()


class Clock:
    def __init__(self): self.value=0
    def now(self): return self.value
    def sleep(self, value): self.value += value


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ops-boot-core-');self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)/'server';self.directory.mkdir(mode=0o700)
        manage.write_new(self.directory/'owner.json',{'schema':1,'uid':os.getuid(),'gid':os.getgid(),
            'owner':str(uuid.uuid4()),'project_root':str(worker.ROOT)})
        self.binding={'scope':'isolated-boot-core','directory':str(self.directory)}
        self.store=ws.Store(self.directory,self.binding)
        self.manifest={'persistence':'boot','binding':self.binding,
            'boot_contract':{'schema':1,'generation':str(uuid.uuid4()),'startup_seconds':20}}
        self.invocation='a'*32;self.clock=Clock();self.policy_calls=0;self.snapshots=0
        self.arm()

    def arm(self):
        self.store.status(phase='service_starting',completed_cycles=0)
        ws.write(self.store.path/'boot.json',{'schema':1,'phase':'armed',
            'generation':self.manifest['boot_contract']['generation'],
            'manifest_sha256':boot.fingerprint(self.manifest),
            'control_sha256':boot.fingerprint(self.store.control()),
            'status_sha256':boot.fingerprint(self.store.optional('status.json')),
            'identity':worker.process_identity()})

    def context(self, **overrides):
        def policy(*_): self.policy_calls+=1
        def snapshot(*_): self.snapshots+=1;return self.binding
        options=dict(policy_check=policy,manager_check=lambda *_:None,pending_check=lambda *_:None,
                     snapshot_fn=snapshot,clock=self.clock.now,sleep=self.clock.sleep)
        options.update(overrides)
        return boot.Context(self.directory,self.manifest,**options)

    def call(self, evaluate=signal_after_evaluation, context=None, **kwargs):
        return worker.run(self.directory,cycles=kwargs.pop('cycles',3),deadline_seconds=kwargs.pop('deadline_seconds',2),
            snapshot_fn=lambda _:self.binding,evaluate_fn=evaluate,service_invocation=self.invocation,
            boot_context=context or self.context(),_wait=kwargs.pop('_wait',lambda *_:None),**kwargs)

    def clean(self):
        result=self.call();self.assertEqual(result['phase'],'boot_clean_exit');return result

    def test_signal_clean_exit_preserves_running_and_records_child_proof(self):
        r=self.clean();self.assertEqual(r['completed_cycles'],1)
        self.assertEqual(self.store.control()['desired'],'running')
        receipt=self.store.optional('boot.json');self.assertTrue(receipt['child_process_group_stopped'])
        self.assertEqual(receipt['termination_cause'],'signal_without_shutdown_attribution')
        self.assertEqual(receipt['status_sha256'],boot.fingerprint(self.store.optional('status.json')))

    def test_clean_receipt_reenters_only_different_synthetic_boot_and_new_manager(self):
        self.clean();old=worker.process_identity;self.invocation='b'*32
        with patch.object(worker,'process_identity',side_effect=lambda:dict(old(),boot_id='11111111-1111-4111-8111-111111111111')):
            result=self.call(accepted,cycles=1)
        self.assertEqual(result['completed_cycles'],1);self.assertEqual(self.store.optional('boot.json')['phase'],'entered')

    def test_same_boot_signal_is_not_shutdown_evidence(self):
        self.clean();self.invocation='b'*32
        with self.assertRaisesRegex(ReadFailure,'BOOT_SAME_BOOT_REENTRY_REFUSED'):self.call()
        self.assertEqual(self.store.control()['desired'],'paused')

    def test_armed_receipt_cannot_survive_boot_change(self):
        old=worker.process_identity
        with patch.object(worker,'process_identity',side_effect=lambda:dict(old(),boot_id='11111111-1111-4111-8111-111111111111')):
            with self.assertRaisesRegex(ReadFailure,'BOOT_EXPLICIT_START_REQUIRED'):self.call()
        self.assertEqual(self.snapshots,0)

    def test_consumed_receipt_cannot_replay_after_interruption(self):
        self.call(accepted,cycles=1)
        with self.assertRaisesRegex(ReadFailure,'BOOT_HANDOFF_REQUIRED'):self.call()

    def test_missing_receipt_does_not_autogenerate_permission(self):
        (self.store.path/'boot.json').unlink()
        with self.assertRaisesRegex(ReadFailure,'BOOT_HANDOFF_REQUIRED'):self.call()
        self.assertFalse((self.store.path/'boot.json').exists())

    def test_stop_or_recovery_blocks_before_manager_or_readiness(self):
        self.store.pause()
        with self.assertRaisesRegex(ReadFailure,'WORKER_PAUSED'):
            self.call(context=self.context(manager_check=lambda *_:self.fail('manager must not be queried')))
        self.assertEqual(self.snapshots,0)

    def test_modified_stop_token_receipt_binding_and_status_are_rejected(self):
        for field in ('token','manifest','status'):
            with self.subTest(field=field):
                ws.write(self.store.path/'control.json',{'schema':1,'desired':'running','recovery':None});self.arm()
                if field=='token':
                    c=self.store.control();c['service_stop_token']=uuid.uuid4().hex;ws.write(self.store.path/'control.json',c)
                elif field=='manifest':self.manifest['boot_contract']['startup_seconds']+=1
                else:self.store.status(phase='waiting',completed_cycles=99)
                with self.assertRaisesRegex(ReadFailure,'BOOT_HANDOFF_MISMATCH'):self.call()

    def test_pending_generation_prevents_receipt_consumption(self):
        before=(self.store.path/'boot.json').read_bytes()
        def pending(*_):raise ReadFailure('REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
        with self.assertRaisesRegex(ReadFailure,'REFRESH_PENDING'):self.call(context=self.context(pending_check=pending))
        self.assertEqual((self.store.path/'boot.json').read_bytes(),before)

    def test_open_intent_blocks_before_startup(self):
        ws.write(self.store.path/'intent.json',{'phase':'open','generation':1})
        with self.assertRaisesRegex(ReadFailure,'WORKER_UNFINISHED_INTENT'):self.call()
        self.assertEqual(self.snapshots,0)

    def test_delay_then_ready_runs_one_fresh_evaluation(self):
        calls=[]
        def snapshot(*_):
            calls.append(1)
            if len(calls)<4:raise boot.NotReady('BOOT_OPS_STARTING')
            return self.binding
        self.call(accepted,cycles=1,context=self.context(snapshot_fn=snapshot))
        ready=self.store.optional('status.json')['boot_readiness']
        self.assertEqual(ready['attempts'],4);self.assertAlmostEqual(ready['elapsed_seconds'],8,places=2)

    def test_startup_deadline_latches_without_evaluation_or_infinite_retry(self):
        self.manifest['boot_contract']['startup_seconds']=2;self.arm()
        def unavailable(*_):raise boot.NotReady('BOOT_DOCKER_SOCKET_WAIT')
        with self.assertRaisesRegex(ReadFailure,'BOOT_STARTUP_DEADLINE'):
            self.call(lambda *_:self.fail('no evaluation'),context=self.context(snapshot_fn=unavailable))
        r=self.store.optional('boot-readiness.json');self.assertEqual(r['attempts'],2)
        self.assertEqual(r['elapsed_seconds'],2);self.assertEqual(self.store.control()['recovery'],'BOOT_STARTUP_DEADLINE')

    def test_ownership_or_unknown_failure_is_not_retried(self):
        for code in ('WORKER_BINDING_CHANGED','MOUNT_MISMATCH','DOCKER_QUERY_FAILED','BOOT_OPS_UNHEALTHY'):
            with self.subTest(code=code):
                ws.write(self.store.path/'control.json',{'schema':1,'desired':'running','recovery':None});self.arm();calls=[]
                def fail(*_):calls.append(1);raise ReadFailure(code)
                with self.assertRaisesRegex(ReadFailure,code):self.call(context=self.context(snapshot_fn=fail))
                self.assertEqual(len(calls),1)

    def test_durable_stop_during_wait_is_observed_before_another_attempt(self):
        def unavailable(*_):self.store.pause();raise boot.NotReady('BOOT_OPS_STARTING')
        with self.assertRaisesRegex(ReadFailure,'BOOT_STARTUP_INTERRUPTED'):
            self.call(context=self.context(snapshot_fn=unavailable))
        self.assertEqual(self.clock.value,0);self.assertEqual(self.store.control()['desired'],'paused')

    def test_post_intent_signal_finishes_transaction_before_handoff(self):
        def evaluation(store,binding,snapshot,ident):
            ws.write(store.path/'intent.json',{'phase':'open','evaluation':ident})
            os.kill(os.getppid(),signal.SIGTERM);time.sleep(.12)
            ws.write(store.path/'intent.json',{'phase':'completed','evaluation':ident})
            return accepted()
        r=self.call(evaluation);self.assertEqual(r['phase'],'boot_clean_exit')
        self.assertEqual(self.store.optional('intent.json')['phase'],'completed')

    def test_prewrite_signal_cancels_child_without_permanent_pause(self):
        def evaluation(*_):os.kill(os.getppid(),signal.SIGTERM);time.sleep(10)
        r=self.call(evaluation);self.assertEqual(r['phase'],'boot_clean_exit')
        self.assertEqual(r['last_outcome']['classification'],'stopped')
        self.assertEqual(self.store.control()['desired'],'running')
        self.assertTrue(self.store.control()['boot_exit_request'])

    def test_post_intent_deadline_keeps_recovery_and_no_clean_receipt(self):
        def evaluation(store,binding,snapshot,ident):
            ws.write(store.path/'intent.json',{'phase':'open','evaluation':ident})
            os.kill(os.getppid(),signal.SIGTERM);time.sleep(10)
        r=self.call(evaluation,deadline_seconds=1);self.assertEqual(r['exit_code'],2)
        self.assertEqual(self.store.optional('boot.json')['phase'],'entered')
        self.assertIsNotNone(self.store.control()['recovery'])

    def test_waiting_signal_can_exit_cleanly_without_another_cycle(self):
        r=self.call(accepted,_wait=lambda *_:os.kill(os.getpid(),signal.SIGTERM))
        self.assertEqual(r['completed_cycles'],1);self.assertEqual(r['phase'],'boot_clean_exit')

    def test_explicit_stop_wins_race_with_signal(self):
        def evaluation(store,*_):store.pause();return signal_after_evaluation()
        r=self.call(evaluation);self.assertEqual(r['phase'],'stopped')
        self.assertNotEqual(self.store.optional('boot.json')['phase'],'clean_exit')

    def test_receipt_write_failure_latches_and_does_not_make_replayable_exit(self):
        real=ws.write
        def fail(path,data):
            if path.name=='boot.json' and data.get('phase')=='clean_exit':raise OSError('synthetic private failure')
            return real(path,data)
        with patch.object(ws,'write',side_effect=fail):
            with self.assertRaises(OSError):self.call()
        self.assertEqual(self.store.control()['desired'],'paused')
        self.assertEqual(self.store.optional('boot.json')['phase'],'entered')

    def test_contract_refuses_legacy_boot_and_invalid_budgets(self):
        for contract in (None,{},dict(self.manifest['boot_contract'],startup_seconds=0),
                         dict(self.manifest['boot_contract'],startup_seconds=True),dict(self.manifest['boot_contract'],generation='wrong')):
            with self.subTest(contract=contract):
                with self.assertRaises(ReadFailure):boot.policy(dict(self.manifest,boot_contract=contract))

    def test_interruption_after_consuming_receipt_cannot_replay(self):
        write=ws.write
        def fail(path,data):
            if path.name=='control.json' and self.store.optional('boot.json')['phase']=='entered':
                raise OSError('interrupted between records')
            return write(path,data)
        with patch.object(ws,'write',side_effect=fail):
            with self.assertRaises(OSError):self.call()
        self.assertEqual(self.store.optional('boot.json')['phase'],'entered')

    def test_changed_policy_while_waiting_is_terminal(self):
        calls=[]
        def policy(*_):
            calls.append(1)
            if len(calls)>3:raise ReadFailure('BOOT_POLICY_CHANGED')
        def delayed(*_):raise boot.NotReady('BOOT_OPS_STARTING')
        with self.assertRaisesRegex(ReadFailure,'BOOT_POLICY_CHANGED'):
            self.call(context=self.context(policy_check=policy,snapshot_fn=delayed))
        self.assertEqual(self.store.control()['recovery'],'BOOT_POLICY_CHANGED')

    def test_child_unknown_exit_does_not_create_clean_handoff(self):
        r=self.call(lambda *_:os._exit(0))
        self.assertEqual(r['exit_code'],2);self.assertEqual(self.store.optional('boot.json')['phase'],'entered')

    def test_pause_at_final_handoff_wins_without_clean_record(self):
        context=self.context();finish=context.finish
        def pause_first(*args,**kwargs):self.store.pause();return finish(*args,**kwargs)
        with patch.object(context,'finish',side_effect=pause_first):r=self.call(context=context)
        self.assertEqual(r['phase'],'stopped');self.assertEqual(self.store.optional('boot.json')['phase'],'entered')

    def test_reused_manager_invocation_on_different_boot_is_refused(self):
        self.clean();old=worker.process_identity
        with patch.object(worker,'process_identity',side_effect=lambda:dict(old(),boot_id='11111111-1111-4111-8111-111111111111')):
            with self.assertRaisesRegex(ReadFailure,'BOOT_MANAGER_INVOCATION_REUSED'):self.call()

    def test_signal_after_session_core_return_requires_terminal_record_and_free_lease(self):
        # Reproduce the old service fixture's late-SIGTERM window deliberately.
        program=('import os,signal,sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
                 'from ops_platform.ops import refresh_worker as w,worker_state as s;'
                 'd=Path(sys.argv[2]);b=s.Store(d).manifest()["binding"];'
                 'w.run(d,cycles=1,snapshot_fn=lambda _:b,evaluate_fn=lambda *_:{"classification":"accepted","result":"passed"});'
                 '(d/"returned").write_text("returned");time.sleep(10)')
        child=subprocess.Popen([sys.executable,'-I','-B','-c',program,str(worker.ROOT),str(self.directory)])
        try:
            until=time.monotonic()+3
            while not (self.directory/'returned').exists():
                self.assertLess(time.monotonic(),until);time.sleep(.01)
            child.terminate();child.wait(timeout=3);self.assertEqual(child.returncode,-signal.SIGTERM)
            self.assertEqual(self.store.optional('status.json')['phase'],'bounded_complete')
            self.assertTrue(self.store.optional('status.json')['outcome']['child_process_group_stopped'])
            with ws.automatic_lease(self.directory):pass
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=3)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture=MetadataFixture();self.addCleanup(self.fixture.temp.cleanup)
        self.directory=self.fixture.directory;self.reader=self.fixture.reader
        with patch.object(worker.probe,'Reader',return_value=self.reader):self.binding=worker.runtime_snapshot(self.directory)
        self.reader_factory=lambda:self.fixture.reader

    def call(self):
        return boot.snapshot(self.directory,self.binding,boot.Budget(30,lambda:False),
                             reader_factory=self.reader_factory,connect=lambda *_:None)

    def test_actual_metadata_parser_accepts_only_same_healthy_ops(self):
        self.assertEqual(self.call(),self.binding)
        self.assertTrue(all(t=='docker' for t,_,_ in self.fixture.calls))

    def test_starting_and_stopped_ops_retry_only_after_identity_validation(self):
        self.fixture.raw.update(running=False,health='starting')
        with self.assertRaisesRegex(boot.NotReady,'BOOT_OPS_STARTING'):self.call()
        self.fixture.raw['owner']=str(uuid.uuid4())
        with self.assertRaisesRegex(ReadFailure,'CONTAINER_OWNER_MISMATCH'):self.call()

    def test_changed_image_mount_ambiguous_missing_or_unhealthy_are_terminal(self):
        original=deepcopy(self.fixture.raw)
        cases=[('image','sha256:'+'1'*64,'WORKER_BINDING_CHANGED'),('health','unhealthy','BOOT_OPS_UNHEALTHY'),
               ('mounts',[],'MOUNT_MISMATCH')]
        for key,value,code in cases:
            with self.subTest(key=key):
                self.fixture.raw=deepcopy(original);self.fixture.raw[key]=value
                with self.assertRaisesRegex(ReadFailure,code):self.call()
        self.fixture.raw=original
        for ids,code in [([], 'OPS_CONTAINER_NOT_FOUND'),([original['id']]*2,'OPS_CONTAINER_AMBIGUOUS')]:
            self.fixture.ids=ids
            with self.assertRaisesRegex(ReadFailure,code):self.call()

    def test_parent_environment_and_local_endpoint_precedence_preserved(self):
        original=dict(os.environ);self.assertEqual(self.call(),self.binding)
        self.assertEqual(dict(os.environ),original)
        self.fixture.reader.original['DOCKER_HOST']='tcp://somewhere:2375'
        with self.assertRaisesRegex(ReadFailure,'LOCAL_DOCKER_REQUIRED'):self.call()

    def test_code_python_and_daemon_binding_changes_are_never_rebound(self):
        for key,value in [('code_sha256','0'*64),('python_version',[0,0,0]),('daemon_id','different-daemon')]:
            with self.subTest(key=key):
                before=deepcopy(self.binding);self.binding[key]=value
                with self.assertRaisesRegex(ReadFailure,'WORKER_BINDING_CHANGED'):self.call()
                self.binding=before

    def test_each_command_timeout_capped_by_remaining_budget(self):
        clock=Clock();budget=boot.Budget(2,lambda:False,clock.now,clock.sleep);calls=[]
        def run(args,**kwargs):calls.append(kwargs['timeout']);return 'ok'
        runner=budget.runner(run);runner(['metadata'],timeout=10);clock.value=1.8;runner(['metadata'],timeout=5)
        self.assertEqual(calls[0],2);self.assertAlmostEqual(calls[1],.2)
        clock.value=2
        with self.assertRaisesRegex(ReadFailure,'BOOT_STARTUP_DEADLINE'):runner(['metadata'])

    def test_real_child_metadata_deadline_terminates_command(self):
        budget=boot.Budget(.15,lambda:False);start=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            budget.runner(subprocess.run)([sys.executable,'-B','-c','import time;time.sleep(5)'],timeout=5,capture_output=True)
        self.assertLess(time.monotonic()-start,2)

    def test_socket_missing_wrong_type_and_permission_are_distinct(self):
        path=self.directory/'socket';endpoint='unix://'+str(path)
        with self.assertRaises(boot.NotReady):boot.socket_ready(endpoint,1)
        path.write_text('not socket')
        with self.assertRaisesRegex(ReadFailure,'BOOT_DOCKER_SOCKET_INVALID'):boot.socket_ready(endpoint,1)
        path.unlink()
        from types import SimpleNamespace
        with patch.object(Path,'lstat',return_value=SimpleNamespace(st_mode=stat.S_IFSOCK)), patch.object(socket,'socket') as sock:
            connect=sock.return_value.__enter__.return_value.connect
            connect.side_effect=PermissionError('private')
            with self.assertRaisesRegex(ReadFailure,'BOOT_DOCKER_SOCKET_UNAVAILABLE'):boot.socket_ready(endpoint,1)
            connect.side_effect=ConnectionRefusedError()
            with self.assertRaisesRegex(boot.NotReady,'BOOT_DOCKER_SOCKET_WAIT'):boot.socket_ready(endpoint,1)

    def test_native_private_socket_refused_then_ready(self):
        try: listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        except PermissionError:self.skipTest('Host disallows AF_UNIX socket creation')
        with listener:
            path=self.directory/'native-socket';endpoint='unix://'+str(path)
            listener.bind(str(path))
            with self.assertRaises(boot.NotReady):boot.socket_ready(endpoint,1)
            listener.listen(1);boot.socket_ready(endpoint,1)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        from ops_platform.tests import test_service_lifecycle as fixtures
        from ops_platform.ops import service_lifecycle as service
        self.service=service;self.fixture=fixtures.ServiceTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.fixture.good('start');self.directory=self.fixture.directory
        self.manifest=service.load(self.directory)
        self.manifest.update(persistence='boot',boot_contract={'schema':1,'generation':str(uuid.uuid4()),'startup_seconds':30})
        ws.write(service.manifest_path(self.directory),self.manifest);service.set_enabled(self.manifest,True)
        self.identity=worker.process_identity();self.fixture.manager.pid=str(self.identity['proc_pid'])
        self.fixture.manager.invocation='a'*32

    def call(self, **changes):
        from ops_platform.ops import service_systemd
        report=dict(status='ok',linger=True);report.update(changes)
        with patch.object(boot.probe,'Reader',return_value=self.fixture.reader), \
             patch.object(boot.probe,'systemd',return_value=report), \
             patch.object(service_systemd,'Systemd',return_value=self.fixture.manager):
            return boot.manager_policy(self.directory,self.manifest,'a'*32,self.identity,boot.Budget(30,lambda:False))

    def test_owned_enabled_same_manager_is_read_only(self):
        before=self.fixture.manager.control_commands;self.call()
        self.assertEqual(self.fixture.manager.control_commands,before)

    def test_linger_dropin_and_manager_identity_changes_refused(self):
        with self.assertRaisesRegex(ReadFailure,'BOOT_MANAGER_REQUIRED'):self.call(linger=False)
        self.fixture.manager.dropins='/synthetic/changed.conf'
        with self.assertRaisesRegex(ReadFailure,'SERVICE_DROPIN_CONFLICT'):self.call()
        self.fixture.manager.dropins='';self.fixture.manager.invocation='b'*32
        with self.assertRaisesRegex(ReadFailure,'BOOT_MANAGER_IDENTITY_MISMATCH'):self.call()

    def test_disabled_or_foreign_link_and_modified_unit_not_repaired(self):
        self.service.set_enabled(self.manifest,False)
        with self.assertRaisesRegex(ReadFailure,'BOOT_OWNED_ENABLE_REQUIRED'):self.call()
        link=self.service.enabled_path(self.manifest);link.symlink_to('/synthetic/foreign')
        with self.assertRaisesRegex(ReadFailure,'SERVICE_ENABLE_LINK_CONFLICT'):self.call()
        link.unlink();self.service.set_enabled(self.manifest,True)
        self.fixture.manager.path.write_text('edited')
        with self.assertRaisesRegex(ReadFailure,'SERVICE_UNIT_CHANGED'):self.call()
        self.assertEqual(self.fixture.manager.path.read_text(),'edited')

    def test_internal_entry_passes_boot_context_but_legacy_policy_is_refused(self):
        import io
        from ops_platform.ops import service_entry
        with patch.dict(os.environ,{'INVOCATION_ID':'a'*32}), patch.object(worker,'run',return_value={'exit_code':0}) as run, \
             patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(service_entry.main(['--state-dir',str(self.directory)]),0)
            self.assertIsInstance(run.call_args.kwargs['boot_context'],boot.Context)
            old=dict(self.manifest);old.pop('boot_contract');ws.write(self.service.manifest_path(self.directory),old)
            run.reset_mock()
            self.assertEqual(service_entry.main(['--state-dir',str(self.directory)]),2);run.assert_not_called()


class TransactionTests(unittest.TestCase):
    def setUp(self):
        from ops_platform.tests import test_refresh_worker as fixtures
        self.fixture=fixtures.GuardTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.directory=self.fixture.directory;self.store=self.fixture.store;self.binding=self.fixture.binding
        self.manifest={'persistence':'boot','binding':self.binding,
            'boot_contract':{'schema':1,'generation':str(uuid.uuid4()),'startup_seconds':20}}
        self.store.status(phase='service_starting',completed_cycles=0)
        ws.write(self.store.path/'boot.json',{'schema':1,'phase':'armed','generation':self.manifest['boot_contract']['generation'],
            'manifest_sha256':boot.fingerprint(self.manifest),'control_sha256':boot.fingerprint(self.store.control()),
            'status_sha256':boot.fingerprint(self.store.optional('status.json')),'identity':worker.process_identity()})

    def context(self):
        return boot.Context(self.directory,self.manifest,policy_check=lambda *_:None,manager_check=lambda *_:None,
            snapshot_fn=lambda *_:self.binding)

    def test_real_refresh_commit_after_signal_leaves_accepted_journal_and_clean_handoff(self):
        self.fixture.h.changed();complete=worker.Guard.complete
        def interrupted(guard,report):
            os.kill(os.getppid(),signal.SIGTERM);time.sleep(.12);complete(guard,report)
        def evaluate(store,binding,snapshot,evaluation):
            with patch.object(worker.Guard,'complete',interrupted):
                return worker.evaluate(store,binding,snapshot,evaluation,self.fixture.options())
        r=worker.run(self.directory,cycles=3,deadline_seconds=5,snapshot_fn=self.fixture.snapshot,evaluate_fn=evaluate,
                     service_invocation='a'*32,boot_context=self.context())
        self.assertEqual(r['phase'],'boot_clean_exit');self.assertTrue(r['last_outcome']['configuration_changed'])
        self.assertEqual(worker.refresh.lineage(self.directory)['records'][-1][1]['phase'],'accepted')
        self.assertEqual(self.store.optional('intent.json')['phase'],'completed')

    def test_real_pending_refresh_record_blocks_boot_without_replay(self):
        self.fixture.h.changed();self.fixture.h.failure='before';self.fixture.h.error('DOCKER_TIMEOUT')
        before=self.fixture.h.journals()[0].read_bytes();restarts=self.fixture.h.docker.restarts
        with self.assertRaisesRegex(ReadFailure,'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK'):
            worker.run(self.directory,cycles=1,snapshot_fn=self.fixture.snapshot,service_invocation='a'*32,
                       boot_context=self.context())
        self.assertEqual(self.fixture.h.journals()[0].read_bytes(),before)
        self.assertEqual(self.fixture.h.docker.restarts,restarts)


if __name__=='__main__':unittest.main()
