"""Reviewed CLI boundaries: actual files/processes, synthetic manager and metadata."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from ops_tools import boot_control as c, check_boot_start as copier


def prepare(root):
    copier.prepare(c.ROOT, root)
    (root/'ops_tools').mkdir(mode=0o700)
    shutil.copy2(c.ROOT/'ops_tools/boot_control.py', root/'ops_tools/boot_control.py')


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'project'; prepare(self.root)

    def code(self, expected):
        with self.assertRaises(c.Failure) as caught: c.reviewed_sources(self.root)
        self.assertEqual(caught.exception.code, expected)

    def test_base_candidate_and_every_mixed_source_load_in_fresh_interpreter(self):
        self.assertEqual(c.reviewed_sources(self.root)['code_state'], 'candidate')
        doc = json.loads((self.root/c.TX/'manifest.json').read_text())
        for row in doc['changes']:
            p = self.root/row['path']; before = self.root/'baseline'/row['path']
            if before.exists(): p.write_bytes(before.read_bytes())
            else: p.unlink()
        code = 'import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from ops_tools import boot_control as c;r=Path(sys.argv[1]);v=c.reviewed_sources(r);c.dependencies(r);print(v["code_state"])'
        changes = doc['changes']
        for i in range(len(changes)+1):
            p = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(self.root)], capture_output=True, text=True, timeout=10)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), 'base' if i == 0 else 'candidate' if i == len(changes) else 'mixed')
            if i < len(changes):
                rel = changes[i]['path']; (self.root/rel).write_bytes((self.root/c.TX/'source'/rel).read_bytes())

    def test_unknown_runtime_source_rejected_before_import(self):
        marker = Path(self.tmp.name)/'should-not-exist'
        (self.root/'ops_platform/ops/manage.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').touch()\n')
        p = subprocess.run([sys.executable, '-I', '-B', str(self.root/'ops_tools/boot_control.py'), 'status', '--state-dir', str(Path(self.tmp.name)/'absent')], capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 2); self.assertEqual(json.loads(p.stdout)['code'], 'BOOT_RUNTIME_SOURCE_CHANGED'); self.assertFalse(marker.exists())

    def test_manifest_controller_and_payload_pins(self):
        for rel, expected in ((c.TX/'manifest.json', 'BOOT_REVIEWED_MANIFEST_CHANGED'), (c.START/'arming.py', 'BOOT_REVIEWED_CONTROLLER_CHANGED'), (c.TX/'source/ops_platform/ops/worker_state.py', 'BOOT_REVIEWED_PAYLOAD_CHANGED')):
            p = self.root/rel; raw = p.read_bytes(); p.write_bytes(raw+b'\n'); self.code(expected); p.write_bytes(raw)

    def test_extra_missing_symlink_and_hardlink_source_refused(self):
        extra = self.root/'ops_platform/unreviewed.py'; extra.write_text(''); self.code('BOOT_RUNTIME_SET_CHANGED'); extra.unlink()
        p = self.root/'ops_platform/ops/manage.py'; raw = p.read_bytes(); p.unlink(); self.code('BOOT_RUNTIME_SET_CHANGED')
        p.symlink_to(c.ROOT/'ops_platform/ops/manage.py'); self.code('BOOT_SOURCE_SYMLINK_REFUSED'); p.unlink(); p.write_bytes(raw)
        os.link(p, Path(self.tmp.name)/'hardlink'); self.code('BOOT_SOURCE_UNSAFE')

    def test_missing_installation_creates_no_state_and_help_is_standalone(self):
        path = Path(self.tmp.name)/'absent'
        for args in (['--help'], ['status', '--state-dir', str(path)]):
            p = subprocess.run([sys.executable, '-I', '-B', str(self.root/'ops_tools/boot_control.py'), *args], capture_output=True, text=True, timeout=10)
            self.assertEqual(p.returncode, 0 if args == ['--help'] else 2, p.stderr)
        self.assertFalse(path.exists())

    def test_python_310_and_active_hash(self):
        ast.parse((c.ROOT/'ops_tools/boot_control.py').read_text(), feature_version=(3, 10))
        self.assertEqual(c.reviewed_sources(c.ROOT)['code_sha256'], c.BASE)


class ArgumentTests(unittest.TestCase):
    def invalid(self, expected, action, **kw):
        with patch.object(c, 'Controller') as factory: value = c.run(action, '/unused', factory=factory, **kw)
        factory.assert_not_called(); self.assertEqual(value['code'], expected)

    def test_apply_requires_review_token_before_reading_state(self):
        for value in (None, '', 'A'*64, 'b'*63): self.invalid('BOOT_REVIEW_ID_REQUIRED', 'apply', expect=value)
        self.invalid('BOOT_REVIEW_ID_REQUIRED', 'start', expect='b'*64)

    def test_action_budgets_and_option_scope(self):
        self.invalid('INVALID_BOOT_ACTION', 'reboot')
        for value in (0, 301, True): self.invalid('INVALID_BOOT_STARTUP_LIMIT', 'plan', startup_seconds=value)
        self.invalid('BOOT_STARTUP_OPTION_SCOPE', 'status', startup_seconds=100)
        for value in (0, 1201, True): self.invalid('BOOT_WAIT_OPTION_SCOPE', 'start', wait_seconds=value)
        self.invalid('BOOT_WAIT_OPTION_SCOPE', 'status', wait_seconds=1)

    def test_arbitrary_error_not_exposed(self):
        def factory(*_): raise ValueError('private arbitrary output')
        result = c.run('status', '/unused', factory=factory)
        self.assertEqual(result['code'], 'BOOT_LOCAL_OR_COMMAND_ERROR'); self.assertNotIn('private arbitrary', json.dumps(result))


class WaitTests(unittest.TestCase):
    def controller(self, outcomes):
        controller = object.__new__(c.Controller)
        self.now = 0.; self.sleeps = []; self.calls = 0
        def attempt(_):
            self.calls += 1
            value = outcomes[min(self.calls-1, len(outcomes)-1)]
            if isinstance(value, Exception): raise value
            return value
        controller.plan_once = attempt
        return controller

    def sleep(self, seconds): self.sleeps.append(seconds); self.now += seconds
    def plan(self, controller): return controller.plan(300, clock=lambda: self.now, sleep=self.sleep)

    def test_only_busy_and_evaluation_waits_retry_then_return_new_proposal(self):
        good = ({'result': 'passed', 'review_id': 'fresh'}, {'new': True})
        obj = self.controller([c.Failure('BOOT_PREFLIGHT_BLOCKED', {'blockers': ['PREPARED_STATE_BUSY']}),
                               c.Failure('PUBLIC_URL_CHANGE_BUSY'), c.Failure('BOOT_SESSION_EVALUATING'), good])
        value, proposal = self.plan(obj)
        self.assertEqual(value['plan_wait']['attempts'], 4); self.assertEqual(self.sleeps, [2, 2, 2])
        self.assertEqual(proposal, {'new': True}); self.assertEqual(value['review_id'], 'fresh')

    def test_recovery_or_second_blocker_is_never_retried(self):
        for blockers in (['WORKER_RECOVERY_REQUIRED'], ['PREPARED_STATE_BUSY', 'WORKER_RECOVERY_REQUIRED'], []):
            obj = self.controller([c.Failure('BOOT_PREFLIGHT_BLOCKED', {'blockers': blockers})])
            with self.assertRaises(c.Failure) as caught: self.plan(obj)
            self.assertEqual(caught.exception.code, 'BOOT_PREFLIGHT_BLOCKED')
            self.assertEqual(self.calls, 1); self.assertEqual(self.sleeps, [])

    def test_binding_ownership_and_changed_plan_errors_are_not_retried(self):
        for code in ('SERVICE_BINDING_CHANGED', 'OWNER_MISMATCH', 'BOOT_PLAN_SERVICE_CHANGED', 'OTHER_CONTAINER_CHANGED'):
            obj = self.controller([c.Failure(code)])
            with self.assertRaises(c.Failure) as caught: self.plan(obj)
            self.assertEqual(caught.exception.code, code); self.assertEqual(self.calls, 1); self.assertEqual(self.sleeps, [])

    def test_persistent_busy_has_finite_budget_and_bounded_diagnostic(self):
        obj = self.controller([c.Failure('PUBLIC_URL_CHANGE_BUSY')])
        with self.assertRaises(c.Failure) as caught: self.plan(obj)
        self.assertEqual(caught.exception.code, 'BOOT_PLAN_WAIT_TIMEOUT')
        self.assertEqual(self.now, 30); self.assertEqual(self.calls, 16)
        self.assertEqual(caught.exception.detail['last_retry_code'], 'PUBLIC_URL_CHANGE_BUSY')

    def test_timeout_report_keeps_readonly_effects_and_wait_reason(self):
        obj = self.controller([c.Failure('PREPARED_STATE_BUSY')])
        obj.r = SimpleNamespace(adapter=SimpleNamespace(command_details=lambda _: None))
        obj.execute = lambda *args: self.plan(obj)[0]
        value = c.run('plan', '/unused', factory=lambda _: obj)
        self.assertEqual(value['code'], 'BOOT_PLAN_WAIT_TIMEOUT')
        self.assertEqual(value['plan_wait']['last_retry_code'], 'PREPARED_STATE_BUSY')
        self.assertEqual(value['files_written'], []); self.assertFalse(value['service_control_requested'])
        self.assertEqual(value['worker_service_control_commands'], 0); self.assertFalse(value['durable_pause_requested'])

    def test_slow_metadata_does_not_start_another_attempt_after_budget(self):
        obj = self.controller([c.Failure('PREPARED_STATE_BUSY')]); previous = obj.plan_once
        def slow(value): self.now += 31; return previous(value)
        obj.plan_once = slow
        with self.assertRaises(c.Failure) as caught: self.plan(obj)
        self.assertEqual(caught.exception.code, 'BOOT_PLAN_WAIT_TIMEOUT'); self.assertEqual(self.calls, 1); self.assertEqual(self.sleeps, [])

    def test_first_success_does_not_sleep(self):
        obj = self.controller([({'result': 'passed'}, {'proposal': 'fresh'})])
        value, _ = self.plan(obj)
        self.assertEqual(value['plan_wait']['attempts'], 1); self.assertIsNone(value['plan_wait']['last_retry_code']); self.assertEqual(self.sleeps, [])

    def test_only_running_session_can_wait_for_evaluation(self):
        obj = object.__new__(c.Controller)
        value = {'status': 'owned', 'persistence': 'session', 'enabled': False, 'desired_state': 'running',
                 'recovery': None, 'provider_status': {'ActiveState': 'active'},
                 'evaluation_status': 'in_progress', 'current_invocation_verified': True}
        with self.assertRaises(c.Failure) as caught: obj.session_ready(value)
        self.assertEqual(caught.exception.code, 'BOOT_SESSION_EVALUATING')
        for edits in ({'desired_state': 'paused'}, {'recovery': 'OTHER_CONTAINER_CHANGED'}, {'persistence': 'boot'},
                      {'provider_status': {'ActiveState': 'failed'}}, {'evaluation_status': 'degraded_retrying'}):
            with self.assertRaises(c.Failure) as caught: obj.session_ready(dict(value, **edits))
            self.assertEqual(caught.exception.code, 'BOOT_HEALTHY_SESSION_REQUIRED')


FLOW = r'''
import json,pathlib,shutil,subprocess,sys,uuid
from copy import deepcopy
from unittest.mock import patch
root=pathlib.Path(sys.argv[1]);mode=sys.argv[2];sys.path.insert(0,str(root));sys.dont_write_bytecode=True
from ops_tools import boot_control as c
from ops_platform.ops import worker_state as ws,refresh_worker as worker,service_lifecycle as service
from ops_platform.contracts import ReadFailure
fixture=c.load('arming_fixture',root/c.START/'tests/test_arming.py')
base_mode=mode.startswith(('plan','apply','partial','stop','preflight','stale'))
f=fixture.fixture.TransitionTests(methodName='runTest') if base_mode else fixture.StartTests(methodName='runTest')
f.setUp();base=f if base_mode else f.f
lease_child=None
def release():
 global lease_child
 if lease_child is not None:lease_child.communicate(b'x',timeout=5);lease_child=None
try:
 for folder in (c.TX,c.START):shutil.copytree(root/folder,base.root/folder)
 (base.root/'ops_tools').mkdir(mode=0o700);shutil.copy2(root/'ops_tools/boot_control.py',base.root/'ops_tools/boot_control.py')
 store=ws.Store(base.directory);manager=base.manager
 def healthy():
  manager.active=True;manager.pid=str(worker.process_identity()['proc_pid']);manager.invocation=uuid.uuid4().hex
  control=store.control();control.update(desired='running',recovery=None);ws.write(store.path/'control.json',control)
  store.status(phase='waiting',identity=worker.process_identity(),invocation=uuid.uuid4().hex,service_invocation=manager.invocation,completed_cycles=3,
    outcome={'result':'passed','classification':'accepted','configuration_changed':False,'restart_requested':False,'child_process_group_stopped':True})
 if base_mode:healthy()
 class Local(c.Controller):
  def __init__(self,directory):super().__init__(directory,root=base.root)
  def connect(self,boot=False):
   super().connect(boot)
   self.tx.snapshot=base.snapshot;self.tx.lineage=lambda _:deepcopy(base.provenance)
  def plan(self,startup_seconds):
   if mode=='plan_real_lock':return super().plan(startup_seconds,sleep=lambda _:release())
   return super().plan(startup_seconds)
  def start_adapter(self):
   c.require(self.sources['code_state']=='candidate','BOOT_CANDIDATE_CODE_REQUIRED');self.connect(boot=True);return f.make()
 def run(action,**kw):return c.run(action,base.directory,factory=Local,**kw)
 report={'environment_ready':True,'warnings':['EXTERNAL_REFERENCE_PRIVATE_MODE_DIFFERS'],'prepared_state_stable':True}
 def snap():return {str(p.relative_to(base.folder)):(p.read_bytes(),p.stat().st_mtime_ns) for p in base.folder.rglob('*') if p.is_file() and not p.is_symlink()}
 systemd={'status':'unavailable' if mode=='stop_unavailable' else 'ok','linger':mode!='plan_linger_missing','manager_version':249}
 runtime=c.dependencies(base.root)
 with patch.object(runtime.preflight,'run',return_value=report),patch.object(runtime.probe,'systemd',return_value=systemd),patch.object(runtime.adapter,'Systemd',return_value=manager):
  if mode=='plan_real_lock':
   program='import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from ops_platform.ops.set_public_url import change_lock\nwith change_lock(Path(sys.argv[2])):\n print("locked",flush=True);sys.stdin.buffer.read(1)'
   lease_child=subprocess.Popen([sys.executable,'-I','-B','-c',program,str(root),str(base.directory)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
   assert lease_child.stdout.readline()==b'locked\n'
   def busy_preflight(*a,**k):
    try:
     with runtime.local.snapshot_lock(base.directory):return report
    except ReadFailure as exc:return dict(report,environment_ready=False,blockers=[exc.code])
   before=snap();calls=manager.control_commands
   with patch.object(runtime.preflight,'run',side_effect=busy_preflight):value=run('plan')
   assert value['result']=='passed' and value['plan_wait']['attempts']==2,value
   assert value['plan_wait']['last_retry_code']=='PREPARED_STATE_BUSY'
   assert snap()==before and manager.control_commands==calls and manager.starts==0 and manager.stops==0
  elif mode=='plan_evaluation_completes':
   original=Local.status;calls=[0]
   def status(self):
    value=original(self);calls[0]+=1
    if calls[0]==1:value['evaluation_status']='in_progress'
    return value
   with patch.object(Local,'status',status),patch.object(c.time,'sleep',lambda _:None):value=run('plan')
   assert value['result']=='passed' and value['plan_wait']['attempts']==2,value
   assert value['management']['evaluation_status']=='healthy';assert manager.starts==0 and manager.stops==0
  elif mode=='plan_readonly':
   before=snap();calls=manager.control_commands;one=run('plan');two=run('plan');assert one['result']=='passed',one
   assert one['files_written']==[] and not one['service_control_requested'] and not one['activation_requested']
   assert one['review_id']==two['review_id'];assert snap()==before;assert manager.control_commands==calls
   saved=store.optional('status.json');saved['completed_cycles']+=1;ws.write(store.path/'status.json',saved)
   assert run('plan')['review_id']==one['review_id'];assert not base.transition.path.exists()
  elif mode=='plan_linger_missing':
   value=run('plan');assert value['code']=='BOOT_MANAGER_REQUIRED',value;assert manager.stops==0 and manager.starts==0
  elif mode=='preflight_blocked':
   report.update(environment_ready=False,blockers=['LINGER_NOT_ENABLED','private text']);value=run('plan')
   assert value['code']=='BOOT_PREFLIGHT_BLOCKED',value;assert value['preflight']['blockers']==['LINGER_NOT_ENABLED'];assert manager.stops==0
  elif mode.startswith('stale_'):
   one=run('plan');assert one['result']=='passed',one
   if mode=='stale_controller':
    p=base.root/'ops_tools/boot_control.py';p.write_bytes(p.read_bytes()+b'\n# review changed\n')
   elif mode=='stale_invocation':healthy()
   elif mode=='stale_provenance':base.provenance['evidence']['fixture']='changed'
   elif mode=='stale_stop':
    control=store.control();control['service_stop_token']='e'*32;ws.write(store.path/'control.json',control)
   value=run('apply',expect=one['review_id']);assert value['code']=='BOOT_REVIEW_CHANGED',value
   assert manager.stops==0 and not base.transition.path.exists()
  elif mode=='apply_pause_finish_rollback':
   one=run('plan');assert one['result']=='passed',one;value=run('apply',expect=one['review_id']);assert value['result']=='passed',value
   assert store.control()['desired']=='paused' and not manager.active and manager.starts==0 and manager.stops==1
   assert c.reviewed_sources(base.root)['code_state']=='candidate'
   value=run('apply',expect=one['review_id']);assert value['code']=='BOOT_BASE_CODE_REQUIRED',value
   before=snap();calls=manager.control_commands;assert run('finish')['result']=='passed';assert snap()==before and calls==manager.control_commands
   value=run('rollback');assert value['result']=='passed',value;assert c.reviewed_sources(base.root)['code_state']=='base'
   assert store.control()['desired']=='paused' and manager.starts==0
  elif mode.startswith('partial_'):
   obj=base.interrupt('file:ops_platform/ops/worker_state.py')
   action=mode.split('_',1)[1]
   if action.startswith('fresh_'):
    action=action.split('_',1)[1]
    program="""
import json,pathlib,sys
from copy import deepcopy
sys.path.insert(0,sys.argv[1])
from ops_tools import boot_control as c
from ops_platform.tests.test_service_lifecycle import Manager
class Fresh(c.Controller):
 def __init__(self,directory):super().__init__(directory,root=pathlib.Path(sys.argv[1]))
 def connect(self,boot=False):
  manifest=self.manifest();self.backend=Manager(pathlib.Path(manifest['unit_path']).parents[2],manifest['unit_name'])
  binding=manifest['binding']
  def snapshot(_):
   value=deepcopy(binding);value['code_sha256']=self.r.worker.code_hash(self.root);return value
  self.tx=self.transition_module.Transition(self.directory,self.bundle,self.backend,snapshot=snapshot,lineage=lambda _:json.loads(sys.argv[4]))
print(json.dumps(c.run(sys.argv[3],pathlib.Path(sys.argv[2]),factory=Fresh)))
"""
    history={k:v for k,v in base.provenance.items() if k!='records'}
    proc=subprocess.run([sys.executable,'-I','-B','-c',program,str(base.root),str(base.directory),action,json.dumps(history)],capture_output=True,text=True,timeout=10)
    assert proc.returncode==0,proc.stderr;value=json.loads(proc.stdout)
   else:value=run(action)
   assert value['result']=='passed',value
   assert manager.stops==1 and manager.starts==0 and store.control()['desired']=='paused'
  elif mode=='stop_unavailable':
   value=run('stop');assert value['code']=='SYSTEMD_USER_MANAGER_UNAVAILABLE',value
   assert value['durable_pause_requested'] and store.control()['desired']=='paused' and manager.stops==0
  elif mode=='start_check_duplicate':
   value=run('start',wait_seconds=3);assert value['result']=='passed',value;assert value['fresh_evaluation_verified']
   calls=manager.starts;before=snap();value=run('start',wait_seconds=3)
   assert value['code']=='BOOT_START_ALREADY_RECORDED',value;assert manager.starts==calls and before==snap()
   value=run('check');assert value['result']=='passed',value;assert manager.starts==calls
  elif mode=='start_failure_paused':
   def fail(*_):raise ReadFailure('OPS_ALERT_READBACK_FAILED')
   f.readback=fail;value=run('start',wait_seconds=3);assert value['code']=='OPS_ALERT_READBACK_FAILED',value
   assert manager.starts==0 and store.control()['desired']=='paused' and not service.enabled(service.load(base.directory))
  elif mode=='check_different_boot_no_pause':
   assert run('start',wait_seconds=3)['result']=='passed';before=snap();identity=worker.process_identity();identity['boot_id']='e'*36
   with patch.object(worker,'process_identity',return_value=identity):value=run('check')
   assert value['code']=='BOOT_INITIAL_CHECK_DIFFERENT_BOOT_USE_STATUS',value;assert before==snap()
  elif mode=='check_uses_record_without_start':
   f.good();record=f.adapter.load();record['phase']='start_requested';ws.write(f.adapter.path,record)
   calls=manager.starts;value=run('check');assert value['result']=='passed',value;assert manager.starts==calls
  elif mode=='cancel_and_source_rollback':
   assert run('start',wait_seconds=3)['result']=='passed';value=run('cancel');assert value['result']=='passed',value
   assert not manager.active and not service.enabled(service.load(base.directory))
   value=run('rollback');assert value['result']=='passed',value;assert c.reviewed_sources(base.root)['code_state']=='base' and store.control()['desired']=='paused'
  elif mode=='status_current_not_historical':
   assert run('start',wait_seconds=3)['result']=='passed';value=run('status');assert value['result']=='passed' and value['current_health_verified'],value
   manager.pid='987654321';value=run('status');assert value['result']=='attention_required' and not value['current_health_verified'],value
  elif mode=='resume_requires_rollback':
   value=run('resume-session',wait_seconds=1);assert value['code']=='BOOT_BASE_CODE_REQUIRED',value;assert manager.starts==0
  elif mode.startswith('resume_rollback_'):
   f.tx.rollback();calls=manager.starts;original=service.run
   if mode!='resume_rollback_session':store.pause('BOOT_START_OUTCOME_UNKNOWN')
   def resume(action,directory,**kw):
    assert action=='resume' and kw['persistence']=='session'
    def recover(directory,resume_guard=None):
     if mode=='resume_rollback_failed_readback':raise ReadFailure('OPS_ALERT_READBACK_FAILED')
     if resume_guard:resume_guard(store)
     control=store.control();control.update(desired='running',recovery=None);ws.write(store.path/'control.json',control)
     store.status(phase='recovery_acknowledged',completed_cycles=0)
    return original(action,directory,**kw,preflight_fn=lambda *a,**k:base.report,snapshot_fn=base.snapshot,recovery_fn=recover)
   def start():manager.starts+=1;manager.control_commands+=1;healthy()
   manager.start=start
   with patch.object(service,'run',side_effect=resume):value=run('resume-session',wait_seconds=2)
   if mode=='resume_rollback_failed_readback':
    assert value['code']=='OPS_ALERT_READBACK_FAILED',value
    assert manager.starts==calls and store.control()['desired']=='paused' and store.control()['recovery']=='BOOT_START_OUTCOME_UNKNOWN'
    print(json.dumps({'result':'passed'}));sys.exit(0)
   assert value['result']=='passed' and value['fresh_evaluation_verified'],value
   assert manager.starts==calls+1 and service.load(base.directory)['persistence']=='session' and not service.enabled(service.load(base.directory))
  else:raise AssertionError(mode)
 print(json.dumps({'result':'passed','scenario':mode,'temporary_state_only':True}))
finally:release();f.doCleanups()
'''


class IntegrationTests(unittest.TestCase):
    def scenario(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'project'; prepare(root)
            proc = subprocess.run([sys.executable, '-I', '-B', '-c', FLOW, str(root), mode], capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout); self.assertEqual(json.loads(proc.stdout)['result'], 'passed')


def add_scenario(name):
    def test(self): self.scenario(name)
    test.__name__ = 'test_' + name; setattr(IntegrationTests, test.__name__, test)

for _name in ('plan_real_lock', 'plan_evaluation_completes', 'plan_readonly', 'plan_linger_missing', 'preflight_blocked', 'stale_controller', 'stale_invocation', 'stale_provenance', 'stale_stop', 'apply_pause_finish_rollback', 'partial_finish', 'partial_rollback', 'partial_fresh_finish', 'partial_fresh_rollback', 'stop_unavailable', 'start_check_duplicate', 'start_failure_paused', 'check_different_boot_no_pause', 'check_uses_record_without_start', 'cancel_and_source_rollback', 'status_current_not_historical', 'resume_requires_rollback', 'resume_rollback_session', 'resume_rollback_recovery', 'resume_rollback_failed_readback'):
    add_scenario(_name)

if __name__ == '__main__': unittest.main()
