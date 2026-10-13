"""Native-runner guards and real source/process transactions; no native manager."""
import ast
from contextlib import redirect_stdout
import io
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

from src.ops.tools import boot_acceptance as b, check_boot_start as copier
from src.ops.platform.contracts import ReadFailure


class MetadataEngine:
    endpoint = 'unix:///var/run/docker.sock'
    def image(self, root): return 'sha256:' + 'b' * 64
    def snapshot(self, ids=None): return {}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def create(self): return b.create_run(self.base, MetadataEngine())[0]
    def code(self, code, fn):
        with self.assertRaises(ReadFailure) as caught: fn()
        self.assertEqual(caught.exception.code, code)

    def test_linger_gate_runs_before_directory_or_engine_creation(self):
        with patch.object(b.native, 'native_preflight', return_value={'linger': False}), patch.object(b, 'Engine') as engine:
            self.code('BOOT_ACCEPTANCE_LINGER_REQUIRED', lambda: b.run(self.base/'unused'))
        engine.assert_not_called(); self.assertEqual(list(self.base.iterdir()), [])

    def test_preflight_is_readonly_and_does_not_claim_reboot(self):
        with patch.object(b.native, 'native_preflight', return_value={'linger': True, 'persistence': 'session'}):
            value = b.preflight()
        self.assertEqual(value['files_written'], []); self.assertEqual(value['persistence'], 'boot')
        self.assertFalse(value['host_reboot_requested']); self.assertFalse(value['linger_changed'])

    def test_generated_copy_preserves_base_and_fixed_auxiliary_scope(self):
        directory = self.create(); record = b.validate(directory)
        self.assertEqual(b.worker.code_hash(directory/'project'), b.BASE_HASH)
        marker = json.loads((directory/'boot-run.json').read_text())
        self.assertEqual(len(marker['auxiliary_hashes']), 10)
        self.assertEqual(len(record['compose']), 8)
        self.assertEqual(marker['source_base_sha256'], b.change.digest(record['source_hashes']))
        self.assertNotIn('.secrets', json.dumps(marker))

    def test_cleanup_entry_runs_real_isolated_python_before_preparation(self):
        directory = self.create()
        value = b.invoke(directory, 'cleanup-unit', timeout=20)
        self.assertTrue(value['worker_exit_verified']); self.assertFalse(value['unit_created'])
        self.assertEqual(value['action'], 'cleanup-unit')

    def test_reviewed_target_overlay_and_rollback_both_validate(self):
        directory = self.create(); project = directory/'project'
        raw, spec = b.candidate_sources(b.ROOT); original = {}
        for row in spec['changes']:
            p = project/row['path']; original[p] = p.read_bytes() if p.exists() else None
            p.write_bytes(raw[str(b.TRANSITION/'source'/row['path'])]); p.chmod(0o600)
        b.validate(directory); self.assertEqual(b.worker.code_hash(project), b.CANDIDATE_HASH)
        value = b.invoke(directory, 'cleanup-unit', timeout=20)
        self.assertTrue(value['worker_exit_verified'])
        for p, body in original.items():
            if body is None: p.unlink()
            else: p.write_bytes(body)
        b.validate(directory); self.assertEqual(b.worker.code_hash(project), b.BASE_HASH)

    def test_copied_tools_or_runtime_tamper_refused_before_execution(self):
        directory = self.create()
        for rel in (b.ENTRY, b.START/'arming.py', Path('src/ops/platform/ops/service_entry.py')):
            p = directory/'project'/rel; original = p.read_bytes(); p.write_bytes(original+b'\n# changed\n')
            with patch.object(b.subprocess, 'Popen') as spawn:
                with self.assertRaises(ReadFailure): b.invoke(directory, 'cleanup-unit')
            spawn.assert_not_called(); p.write_bytes(original)

    def test_extra_runtime_symlink_and_hardlink_are_rejected(self):
        directory = self.create(); project = directory/'project'
        p = project/'src/ops/platform/unreviewed.py'; p.write_text(''); p.chmod(0o600)
        self.code('BOOT_ACCEPTANCE_SOURCE_SET_CHANGED', lambda: b.validate(directory)); p.unlink()
        p = project/b.ENTRY; original = p.read_bytes(); p.unlink(); p.symlink_to(b.ROOT/b.ENTRY)
        with self.assertRaises(ReadFailure): b.validate(directory)
        p.unlink(); p.write_bytes(original); p.chmod(0o600)
        os.link(p, directory/'foreign-link')
        self.code('BOOT_ACCEPTANCE_HARDLINK_REFUSED', lambda: b.validate(directory))

    def test_foreign_scenario_and_marker_owner_never_reach_control(self):
        directory = self.create(); path = directory/'boot-run.json'; value = json.loads(path.read_text())
        value['owner'] = 'foreign'; b.isolated.write(path, value)
        with patch.object(b.native, 'cleanup_unit') as cleanup:
            self.code('BOOT_ACCEPTANCE_RECORD_INVALID', lambda: b.cleanup_unit(directory))
        cleanup.assert_not_called()
        other, _ = b.isolated.create_run(self.base, MetadataEngine(), scenario='watch')
        self.code('BOOT_ACCEPTANCE_SCENARIO_INVALID', lambda: b.validate(other))

    def test_failed_or_busy_unit_cleanup_prevents_docker_removal(self):
        directory = self.create(); engine = object.__new__(b.Engine)
        for code in ('SERVICE_STILL_RUNNING', 'BOOT_ACCEPTANCE_RUN_STILL_ACTIVE'):
            with patch.object(b, 'invoke', side_effect=ReadFailure(code)), \
                 patch.object(engine, 'owned') as owned, patch.object(engine, 'command') as command:
                self.code(code, lambda: engine.cleanup(directory))
            owned.assert_not_called(); command.assert_not_called()

    def test_unit_cleanup_precedes_owned_container_and_network_removal(self):
        directory = self.create(); engine = object.__new__(b.Engine); events = []; owned_calls = 0
        def owned(*_):
            nonlocal owned_calls
            owned_calls += 1
            return ['a'*64] if owned_calls <= 2 else []
        def command(args, **kw):
            events.append(args)
            return ''
        with patch.object(b, 'invoke', side_effect=lambda *a, **k: events.append('unit') or {'result': 'passed'}), \
             patch.object(engine, 'owned', side_effect=owned), patch.object(engine, 'command', side_effect=command), \
             patch.object(engine, 'protect'):
            value = engine.cleanup(directory)
        self.assertEqual(events[0], 'unit'); self.assertEqual(events[1], ['container', 'rm', '-f', 'a'*64])
        self.assertEqual(value['owned_containers_removed'], 1); self.assertFalse(value['images_or_volumes_removed'])

    def test_live_nested_process_keeps_lease_after_supervisor_closes_it(self):
        directory = self.create()
        with b.lifecycle_lock(directory) as fd:
            self.assertEqual(b.inherited_lease(directory, fd), fd)
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import sys; sys.stdin.buffer.read(1)'],
                                     pass_fds=(fd,), stdin=subprocess.PIPE)
        try:
            with patch.object(b, 'ROOT', directory/'project'), patch.object(b.native, 'cleanup_unit') as cleanup:
                self.code('BOOT_ACCEPTANCE_RUN_STILL_ACTIVE', lambda: b.cleanup_unit(directory))
            cleanup.assert_not_called()
        finally: child.communicate(b'x', timeout=5)
        with b.lifecycle_lock(directory): pass

    def test_unlocked_or_unrelated_descriptor_cannot_adopt_lease(self):
        directory = self.create()
        self.code('BOOT_ACCEPTANCE_LEASE_REQUIRED', lambda: b.inherited_lease(directory, None))
        p = directory/'boot-lifecycle.lock'; p.write_text(''); p.chmod(0o600)
        with p.open('rb') as stream:
            self.code('BOOT_ACCEPTANCE_LEASE_NOT_HELD', lambda: b.inherited_lease(directory, stream.fileno()))
        with (directory/'boot-run.json').open('rb') as stream:
            self.code('BOOT_ACCEPTANCE_LEASE_INVALID', lambda: b.inherited_lease(directory, stream.fileno()))

    def test_nested_actions_require_a_real_inherited_lease(self):
        directory = self.create()
        with patch.object(b.subprocess, 'Popen') as spawn:
            self.code('BOOT_ACCEPTANCE_LEASE_REQUIRED', lambda: b.invoke(directory, 'candidate'))
        spawn.assert_not_called()
        proc = subprocess.run([sys.executable, '-I', '-B', str(directory/'project'/b.ENTRY),
                               'candidate', '--run-dir', str(directory)], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)['code'], 'BOOT_ACCEPTANCE_LEASE_REQUIRED')

    def test_timeout_cancels_owned_child_and_never_returns_saved_success(self):
        directory = self.create()
        with patch.object(b.subprocess, 'Popen') as spawn, patch.object(b.isolated, 'stop_worker') as stop:
            spawn.return_value.wait.side_effect = subprocess.TimeoutExpired('fixture', 1)
            with self.assertRaises(subprocess.TimeoutExpired): b.invoke(directory, 'worker', timeout=1)
        stop.assert_called_once_with(spawn.return_value)

    def test_child_environment_sanitizes_keys_and_preserves_explicit_session(self):
        with patch.dict(os.environ, {'OPS_ACCESS_CODE': 'sensitive', 'PYTHONPATH': '/foreign', 'COMPOSE_PROJECT_NAME': 'foreign',
                                    'XDG_RUNTIME_DIR': '/explicit/user', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/explicit/bus'}):
            env = b.child_env({'endpoint': 'unix:///var/run/docker.sock'})
        self.assertNotIn('OPS_ACCESS_CODE', env); self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('COMPOSE_PROJECT_NAME', env); self.assertEqual(env['XDG_RUNTIME_DIR'], '/explicit/user')
        self.assertEqual(env['DBUS_SESSION_BUS_ADDRESS'], 'unix:path=/explicit/bus')

    def test_no_native_stage_is_reported_successful_from_an_old_log(self):
        directory = self.create(); b.isolated.write(directory/'worker.log', {'stage': b.STAGE, 'action': 'worker', 'result': 'passed'})
        with patch.object(b.subprocess, 'Popen') as spawn:
            spawn.return_value.wait.return_value = 0
            with self.assertRaises(ReadFailure): b.invoke(directory, 'worker')

    def test_child_duplicate_diagnostic_survives_parent_report(self):
        directory = self.create()
        def spawn(args, **kw):
            json.dump({'result': 'attention_required', 'code': 'BOOT_ACCEPTANCE_DUPLICATE_GUARD_FAILED',
                       'duplicate_start_rejection': 'SERVICE_MANAGEMENT_BUSY'}, kw['stdout'])
            kw['stdout'].flush()
            return SimpleNamespace(wait=lambda **kwargs: 2)
        with patch.object(b.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(ReadFailure) as caught: b.invoke(directory, 'worker')
        value = b.error_report(caught.exception)
        self.assertEqual(value['duplicate_start_rejection'], 'SERVICE_MANAGEMENT_BUSY')

    def test_python_310_syntax_and_active_runtime_preserved(self):
        ast.parse((b.ROOT/b.ENTRY).read_text(), feature_version=(3, 10))
        self.assertEqual(b.worker.code_hash(b.ROOT), b.BASE_HASH)


FLOW = r'''
import importlib.util,json,pathlib,subprocess,sys
from types import SimpleNamespace
from unittest.mock import patch
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root));sys.dont_write_bytecode=True
def module(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
fixture=module('boot_start_fixture',root/'src/ops/candidates/boot_start_20260929/tests/test_arming.py')
check=module('native_boot_check',root/'src/ops/tools/boot_acceptance.py')
f=fixture.StartTests(methodName='runTest');f.setUp()
mode=sys.argv[2] if len(sys.argv)>2 else 'journal'
lease_child=None
def release():
 global lease_child
 if lease_child is not None:
  lease_child.communicate(b'x',timeout=5);assert lease_child.returncode==0;lease_child=None
try:
 directory=f.f.folder; control=directory/'control';control.mkdir(mode=0o700)
 check.isolated.write(control/'control.json',{'version':0})
 def readback(state,manifest,guard):
  guard()
  if json.loads((control/'control.json').read_text()).get('rules_fault'):raise check.ReadFailure('OPS_ALERT_READBACK_FAILED')
  return f.readback(state,manifest,guard)
 start=f.make(readback_fn=readback)
 original_apply=start.apply
 original_stop=f.manager.stop
 def stop():release();original_stop()
 f.manager.stop=stop
 def apply(proposal):
  global lease_child
  if not start.path.exists():
   result=original_apply(proposal)
   if mode.startswith('busy'):
    code='import sys;sys.path.insert(0,sys.argv[1]);from pathlib import Path;from src.ops.platform.ops import worker_state as w\nwith w.automatic_lease(Path(sys.argv[2])):\n print("locked",flush=True);sys.stdin.buffer.read(1)'
    lease_child=subprocess.Popen([sys.executable,'-I','-B','-c',code,str(root),str(f.directory)],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    assert lease_child.stdout.readline()==b'locked\n'
   return result
  if mode=='unknown':raise check.ReadFailure('SERVICE_MANAGEMENT_BUSY')
  if mode=='raw_error':raise check.ReadFailure('untrusted stderr with private text')
  if mode=='returned':return {'result':'attention_required','code':'AUTOMATIC_WORKER_BUSY'}
  if mode=='busy_mutation':start.path.write_bytes(start.path.read_bytes()+b' ')
  if mode=='invocation':f.manager.invocation='e'*32
  if mode=='disable':check.service.enabled_path(check.service.load(f.directory)).unlink()
  return original_apply(proposal)
 start.apply=apply
 def status():return check.service.status(f.directory,f.manager)
 def wait(predicate):
  saved=f.store.optional('status.json');saved['completed_cycles']+=1
  check.ws.write(f.store.path/'status.json',saved)
  value=status();assert predicate(value),value;return value
 ctl=SimpleNamespace(backend=f.manager,status=status,wait=wait)
 tx=SimpleNamespace(Transition=lambda *a,**k:f.tx,Bundle=lambda *a:f.f.bundle)
 arm=SimpleNamespace(Start=lambda *a,**k:start)
 with patch.object(check,'ROOT',f.f.root),patch.object(check,'checked_state',return_value=f.directory), \
      patch.object(check.native,'Controller',return_value=ctl),patch.object(check,'phase'), \
      patch.object(check,'module',side_effect=lambda name,path:arm if name=='boot_start_candidate' else tx):
  try:report=check.candidate_phase(directory,None)
  except check.ReadFailure as exc:
   print(json.dumps(check.error_report(exc)));sys.exit(0)
 assert len(report['cases'])==6,report
 assert check.worker.code_hash(f.f.root)==check.BASE_HASH
 assert check.service.load(f.directory)['persistence']=='session'
 assert not f.manager.active and not check.service.enabled(check.service.load(f.directory))
 assert f.manager.starts==1
 print(json.dumps({'result':'passed','cases':report['cases'],'duplicate_start_guard':report['duplicate_start_guard'],
                   'source_restored':True,'real_file_transactions':True,'manager':'synthetic'}))
finally:release();f.doCleanups()
'''


class IntegrationTests(unittest.TestCase):
    def scenario(self, mode='journal'):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'project'; copier.prepare(b.ROOT, root)
            p = root/b.ENTRY; p.parent.mkdir(mode=0o700); p.write_bytes((b.ROOT/b.ENTRY).read_bytes())
            proc = subprocess.run([sys.executable, '-I', '-B', '-c', FLOW, str(root), mode],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

    def test_real_transition_arming_receipt_failure_cancel_and_rollback_sequence(self):
        value = self.scenario()
        self.assertEqual(value['result'], 'passed'); self.assertEqual(len(value['cases']), 6)
        self.assertEqual(value['duplicate_start_guard']['rejection_code'], 'BOOT_START_ALREADY_RECORDED')

    def test_actual_child_lease_rejects_duplicate_before_journal_then_cancel_and_rollback_pass(self):
        value = self.scenario('busy')
        self.assertEqual(value['result'], 'passed'); self.assertTrue(value['source_restored'])
        self.assertEqual(value['duplicate_start_guard']['rejection_code'], 'AUTOMATIC_WORKER_BUSY')

    def test_legitimate_busy_code_with_modified_accepted_record_cannot_pass(self):
        value = self.scenario('busy_mutation')
        self.assertEqual(value['code'], 'BOOT_ACCEPTANCE_DUPLICATE_MUTATED_STATE')

    def test_legitimate_journal_code_with_replaced_invocation_cannot_pass(self):
        value = self.scenario('invocation')
        self.assertEqual(value['code'], 'BOOT_ACCEPTANCE_DUPLICATE_CHANGED_WORKER')

    def test_disabled_link_cannot_pass_even_with_duplicate_refusal(self):
        value = self.scenario('disable')
        self.assertEqual(value['code'], 'BOOT_ACCEPTANCE_ENABLE_LINK_CHANGED')

    def test_unknown_refusal_fails_and_keeps_bounded_diagnostic(self):
        value = self.scenario('unknown')
        self.assertEqual(value['code'], 'BOOT_ACCEPTANCE_DUPLICATE_GUARD_FAILED')
        self.assertEqual(value['duplicate_start_rejection'], 'SERVICE_MANAGEMENT_BUSY')

    def test_arbitrary_error_text_is_not_exposed_in_diagnostic(self):
        value = self.scenario('raw_error')
        self.assertEqual(value['duplicate_start_rejection'], 'UNCLASSIFIED_GUARD_RESPONSE')
        self.assertNotIn('private text', json.dumps(value))

    def test_returning_an_attention_object_does_not_prove_duplicate_rejection(self):
        value = self.scenario('returned')
        self.assertEqual(value['code'], 'BOOT_ACCEPTANCE_DUPLICATE_START_ALLOWED')


if __name__ == '__main__': unittest.main()
