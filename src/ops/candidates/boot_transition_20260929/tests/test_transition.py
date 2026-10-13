"""Real file/lease/process tests; manager and Docker metadata are synthetic."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import manage, worker_state as ws, refresh_worker as worker, service_lifecycle as service
from src.ops.platform.ops import lifecycle_probe as probe
from src.ops.platform.tests.test_service_lifecycle import Manager

SOURCE = Path(__file__).resolve().parents[5]
FOLDER = SOURCE / 'src/ops/candidates/boot_transition_20260929'
spec = importlib.util.spec_from_file_location('transition_candidate', FOLDER / 'transition.py')
tx = importlib.util.module_from_spec(spec); spec.loader.exec_module(tx)


class Interrupted(Exception): pass


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ops-transition-test-'); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name); self.root = self.folder / 'project'; self.root.mkdir(mode=0o700)
        self.bundle = tx.Bundle(FOLDER)
        for rel in self.bundle.base:
            target = self.root / rel; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / rel, target)
        fixture = 'src/ops/platform/tests/test_service_lifecycle.py'
        (self.root / fixture).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / fixture, self.root / fixture)
        profile = 'src/ops/platform/profiles/docker-compose.json'
        (self.root / profile).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / profile, self.root / profile)
        self.root_patch = patch.object(worker, 'ROOT', self.root); self.root_patch.start(); self.addCleanup(self.root_patch.stop)
        self.directory = self.folder / 'server'; self.directory.mkdir(mode=0o700)
        owner = {'schema': 1, 'uid': os.getuid(), 'gid': os.getgid(), 'owner': str(uuid.uuid4()), 'project_root': str(self.root)}
        manage.write_new(self.directory / 'owner.json', owner)
        self.binding = {'directory': str(self.directory), 'owner': owner, 'code_sha256': tx.BASE,
                        'python': str(Path(sys.executable).resolve()), 'python_version': list(sys.version_info[:3]),
                        'docker_path': '/synthetic/docker', 'daemon_id': 'fixture-daemon',
                        'ops_container_id': 'a' * 64, 'ops_image_id': 'sha256:' + 'b' * 64}
        self.reader = probe.Reader(env={'XDG_CONFIG_HOME': str(self.folder / 'config'), 'PATH': os.defpath})
        self.manager = Manager(self.folder / 'config', 'agent-project-ops-refresh-' + owner['owner'].replace('-', '') + '.service')
        self.report = {'blockers': [], 'docker': {'endpoint': 'unix:///fixture/docker.sock'},
                       'persistence': {'selected': 'session', 'linger': False}}
        r = service.run('install', self.directory, reader=self.reader, backend=self.manager,
                        preflight_fn=lambda *a, **k: self.report, snapshot_fn=self.snapshot)
        self.assertEqual(r['result'], 'passed', r)
        self.provenance = {'pending': None, 'evidence': {'fixture': 'hash'}, 'next': 1,
                           'records': [(Path('/synthetic/record'), {'phase': 'accepted'})]}
        old_stop = self.manager.stop
        def stop():
            old_stop()
            ws.Store(self.directory).status(phase='stopped', completed_cycles=8,
                                           outcome={'child_process_group_stopped': True})
        self.manager.stop = stop
        # Sentinels are never read by transaction code; compare independently.
        self.sentinels = {}
        for name in ('access-key', 'cache', 'application-config', 'discovery-evidence'):
            p = self.directory / ('sentinel-' + name); p.write_bytes(os.urandom(32)); p.chmod(0o600)
            self.sentinels[p] = (p.read_bytes(), p.stat().st_mtime_ns)
        self.transition = self.new()

    def snapshot(self, directory):
        value = deepcopy(self.binding); value['code_sha256'] = worker.code_hash(self.root); return value

    def new(self, **kwargs):
        return tx.Transition(self.directory, self.bundle, self.manager, snapshot=self.snapshot,
                             lineage=lambda _: deepcopy(self.provenance), **kwargs)

    def assert_code(self, code, fn):
        with self.assertRaises(ReadFailure) as caught: fn()
        self.assertEqual(caught.exception.code, code)

    def interrupt(self, boundary, operation=None):
        def hook(stage):
            if stage == boundary: raise Interrupted(stage)
        obj = self.new(hook=hook)
        with self.assertRaises(Interrupted):
            (operation(obj) if operation else obj.apply(obj.plan()))
        return self.new()

    def assert_paused(self):
        c = ws.Store(self.directory).control()
        self.assertEqual(c['desired'], 'paused'); self.assertFalse(self.manager.active)
        self.assertEqual(self.manager.starts, 0)
        self.assertFalse(service.enabled(service.load(self.directory)))

    def test_plan_is_read_only_and_does_not_claim_boot_readiness(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.folder.rglob('*') if p.is_file()}
        p = self.transition.plan()
        self.assertFalse(p['activation_supported']); self.assertFalse(p['boot_ready'])
        self.assertEqual(p['final_desired'], 'paused'); self.assertEqual(self.manager.stops, 0)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        self.assertFalse(self.transition.path.exists())

    def test_apply_stops_old_code_once_commits_both_bindings_and_stays_paused(self):
        self.manager.active = True; self.manager.pid = '12345'
        store = ws.Store(self.directory); c = store.control(); c['desired'] = 'running'; ws.write(store.path / 'control.json', c)
        unit = self.manager.path.read_bytes(); calls = self.manager.stops
        result = self.transition.apply(self.transition.plan())
        self.assertEqual(result['phase'], 'committed'); self.assertEqual(self.manager.stops, calls + 1)
        saved = service.load(self.directory)
        self.assertEqual(saved['persistence'], 'boot'); self.assertEqual(saved['binding'], store.manifest()['binding'])
        self.assertEqual(saved['binding'], self.snapshot(self.directory)); self.assertEqual(self.manager.path.read_bytes(), unit)
        self.assertEqual(saved['boot_contract']['startup_seconds'], 300)
        self.assert_paused(); self.assertIsNone(store.control()['recovery'])
        self.assertEqual(store.optional('status.json')['completed_cycles'], 8)
        self.assertEqual(self.sentinels, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.sentinels})

    def test_session_code_upgrade_does_not_silently_select_boot(self):
        self.transition.apply(self.transition.plan('session'))
        saved = service.load(self.directory)
        self.assertEqual(saved['persistence'], 'session'); self.assertNotIn('boot_contract', saved); self.assert_paused()

    def test_unchanged_finish_is_idempotent_without_stop_or_file_writes(self):
        self.transition.apply(self.transition.plan())
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.folder.rglob('*') if p.is_file()}
        calls = self.manager.control_commands; self.transition.finish()
        self.assertEqual(calls, self.manager.control_commands)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_modified_proposal_never_stops_or_writes(self):
        proposal = self.transition.plan(); proposal['after']['binding']['daemon_id'] = 'other'
        self.assert_code('TRANSITION_PROPOSAL_CHANGED', lambda: self.transition.apply(proposal))
        self.assertEqual(self.manager.stops, 0); self.assertFalse(self.transition.path.exists())

    def test_stale_stop_token_or_binding_never_stops(self):
        proposal = self.transition.plan(); service.request_pause(self.directory)
        self.assert_code('TRANSITION_PROPOSAL_STALE', lambda: self.transition.apply(proposal))
        self.assertEqual(self.manager.stops, 0)

    def test_pending_and_open_intent_are_not_migrated(self):
        self.provenance['pending'] = 'fixture'
        self.assert_code('REFRESH_PENDING_USE_CHECK_OR_ROLLBACK', self.transition.plan)
        self.provenance['pending'] = None
        store = ws.Store(self.directory); ws.write(store.path / 'intent.json', {'phase': 'open'})
        self.assert_code('WORKER_UNFINISHED_INTENT', self.transition.plan)
        self.assertFalse(self.transition.path.exists())

    def test_existing_recovery_is_never_cleared_by_migration(self):
        ws.Store(self.directory).pause('PRIOR_FAILURE')
        self.assert_code('WORKER_RECOVERY_REQUIRED', self.transition.plan)

    def test_lease_and_cgroup_prevent_any_code_write(self):
        obj = self.interrupt('stop_returned')
        with ws.automatic_lease(self.directory):
            self.assert_code('AUTOMATIC_WORKER_BUSY', obj.finish)
        self.manager.empty = False
        self.assert_code('SERVICE_STILL_RUNNING', obj.finish)
        self.assertEqual(worker.code_hash(self.root), tx.BASE)

    def test_unknown_stop_is_not_reissued_and_explicit_finish_requires_exit(self):
        self.manager.active = True; self.manager.pid = '12345'
        self.assert_code('TRANSITION_STOP_UNCONFIRMED', lambda: self.transition.apply(self.transition.plan(), stop=lambda: {'result': 'attention_required'}))
        self.assert_code('SERVICE_STILL_RUNNING', self.transition.finish)
        self.assertEqual(self.manager.stops, 0)
        service.run('stop', self.directory, backend=self.manager)
        self.transition.finish(); self.assertEqual(self.manager.stops, 1); self.assert_paused()

    def test_prewrite_cancellation_and_repeated_rollback_preserve_original_source(self):
        obj = self.interrupt('stop_returned')
        self.assertEqual(obj.rollback()['phase'], 'rolled_back'); obj.rollback()
        self.assertEqual(worker.code_hash(self.root), tx.BASE); self.assert_paused()
        self.assertFalse(obj.backup_dir(obj.load()).exists())

    def test_every_write_boundary_can_finish_without_repeating_stop(self):
        boundaries = ['journal:prepared', 'journal:writing'] + ['file:' + p for p in tx.TARGETS] + ['file:service', 'file:worker', 'journal:committed']
        # Each subcase gets a separate installation and actual file transaction.
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                case = TransitionTests(); case.setUp()
                try:
                    obj = case.interrupt(boundary); result = obj.finish()
                    case.assertEqual(result['phase'], 'committed'); case.assertEqual(case.manager.stops, 1)
                    case.assert_paused()
                finally: case.doCleanups()

    def test_every_partial_write_can_roll_back_exact_bytes_and_modes(self):
        for boundary in ['file:' + p for p in tx.TARGETS] + ['file:service', 'file:worker']:
            with self.subTest(boundary=boundary):
                case = TransitionTests(); case.setUp()
                try:
                    before = {p: tx.read(case.root / p) for p in case.bundle.base}
                    obj = case.interrupt(boundary); obj.rollback(); obj.rollback()
                    case.assertEqual({p: tx.read(case.root / p) for p in case.bundle.base}, before)
                    case.assertFalse((case.root / 'src/ops/platform/ops/boot_lifecycle.py').exists())
                    case.assert_paused()
                finally: case.doCleanups()

    def test_interrupted_rollback_resumes_only_rollback(self):
        obj = self.interrupt('file:worker')
        obj = self.interrupt('file:service', lambda t: t.rollback())
        self.assert_code('TRANSITION_FINISH_REFUSED', obj.finish)
        obj.rollback(); self.assertEqual(worker.code_hash(self.root), tx.BASE); self.assert_paused()

    def test_commit_can_be_explicitly_rolled_back_while_still_paused(self):
        self.transition.apply(self.transition.plan()); self.transition.rollback()
        self.assertEqual(service.load(self.directory)['persistence'], 'session')
        self.assertEqual(worker.code_hash(self.root), tx.BASE); self.assert_paused()

    def test_rollback_keeps_binding_mismatch_until_terminal_record_is_durable(self):
        self.transition.apply(self.transition.plan())
        obj = self.interrupt('journal:rolled_back', lambda t: t.rollback())
        self.assertEqual(worker.code_hash(self.root), tx.BASE)
        self.assertNotEqual(ws.Store(self.directory).manifest()['binding'], service.load(self.directory)['binding'])
        self.assertEqual(obj.load()['phase'], 'rolled_back')
        obj.rollback(); self.assertEqual(ws.Store(self.directory).manifest()['binding'], self.snapshot(self.directory))
        self.assert_paused()

    def test_interrupted_rollback_before_terminal_marker_cannot_finish_forward(self):
        self.transition.apply(self.transition.plan())
        obj = self.interrupt('file:' + tx.TARGETS[0], lambda t: t.rollback())
        self.assertEqual(worker.code_hash(self.root), tx.BASE)
        self.assertNotEqual(ws.Store(self.directory).manifest()['binding'], service.load(self.directory)['binding'])
        self.assert_code('TRANSITION_FINISH_REFUSED', obj.finish)
        obj.rollback(); self.assert_paused()

    def test_concurrent_stop_cancels_finish_but_rollback_keeps_new_stop_token(self):
        obj = self.interrupt('file:' + tx.TARGETS[0]); service.request_pause(self.directory)
        token = ws.Store(self.directory).control()['service_stop_token']
        self.assert_code('TRANSITION_STOP_REQUEST_CHANGED', obj.finish)
        obj.rollback(); self.assertEqual(ws.Store(self.directory).control()['service_stop_token'], token); self.assert_paused()

    def test_manual_file_edit_blocks_finish_and_rollback_without_overwrite(self):
        obj = self.interrupt('file:' + tx.TARGETS[0])
        path = self.root / tx.TARGETS[0]; path.write_bytes(b'# user edit\n')
        self.assert_code('TRANSITION_CODE_CHANGED', obj.finish)
        self.assert_code('TRANSITION_CODE_CHANGED', obj.rollback)
        self.assertEqual(path.read_bytes(), b'# user edit\n')

    def test_modes_hardlinks_symlinks_and_extra_runtime_are_not_adopted(self):
        obj = self.interrupt('journal:prepared'); path = self.root / tx.TARGETS[0]
        mode = path.stat().st_mode & 0o777; path.chmod(mode ^ 0o010)
        self.assert_code('TRANSITION_EXTERNAL_EDIT', obj.finish); path.chmod(mode)
        link = self.folder / 'duplicate'; os.link(path, link)
        self.assert_code('TRANSITION_FILE_UNSAFE', obj.finish); link.unlink()
        raw = path.read_bytes(); path.unlink(); path.symlink_to(self.folder / 'outside')
        self.assert_code('SYMLINK_REFUSED', obj.finish); path.unlink(); path.write_bytes(raw); path.chmod(mode)
        extra = self.root / 'src/ops/platform/unreviewed.py'; extra.write_text('# extra')
        self.assert_code('TRANSITION_CODE_SET_CHANGED', obj.finish)

    def test_backup_corruption_or_missing_backup_cannot_be_recovered_as_success(self):
        obj = self.interrupt('file:service'); record = obj.load()
        backup = obj.backup_dir(record) / '0.before'; original = backup.read_bytes(); backup.write_bytes(b'bad')
        self.assert_code('TRANSITION_BACKUP_CHANGED', obj.rollback)
        backup.write_bytes(original); backup.unlink()
        with self.assertRaises(FileNotFoundError): obj.finish()
        self.assertEqual(obj.load()['phase'], 'writing')

    def test_changed_daemon_python_container_or_image_binding_is_not_rebound(self):
        obj = self.interrupt('journal:prepared')
        for key in ('daemon_id', 'python', 'ops_container_id', 'ops_image_id', 'docker_path'):
            with self.subTest(key=key):
                before = self.binding[key]; self.binding[key] = 'changed'
                self.assert_code('TRANSITION_RUNTIME_IDENTITY_CHANGED', obj.finish)
                self.binding[key] = before
        self.assertEqual(worker.code_hash(self.root), tx.BASE)

    def test_changed_provenance_or_unit_is_not_repaired(self):
        obj = self.interrupt('journal:prepared')
        self.provenance['evidence']['fixture'] = 'new'
        self.assert_code('TRANSITION_PROVENANCE_CHANGED', obj.finish)
        self.provenance['evidence']['fixture'] = 'hash'; self.manager.path.write_text('manual unit')
        self.assert_code('TRANSITION_UNIT_CHANGED', obj.finish); self.assertEqual(self.manager.path.read_text(), 'manual unit')

    def test_stop_failure_preserves_its_recovery_and_never_writes_code(self):
        def failed_stop():
            service.request_pause(self.directory); ws.Store(self.directory).pause('STOP_UNKNOWN')
            return {'result': 'attention_required'}
        self.assert_code('TRANSITION_STOP_UNCONFIRMED', lambda: self.transition.apply(self.transition.plan(), stop=failed_stop))
        self.assert_code('WORKER_RECOVERY_REQUIRED', self.transition.finish)
        self.assertEqual(ws.Store(self.directory).control()['recovery'], 'STOP_UNKNOWN')
        self.assertEqual(worker.code_hash(self.root), tx.BASE)

    def test_changed_service_or_worker_record_is_not_overwritten(self):
        obj = self.interrupt('file:' + tx.TARGETS[-1])
        path = self.store_path = ws.location(self.directory) / 'owner.json'
        value = ws.read(path); value['binding']['daemon_id'] = 'manual'; ws.write(path, value)
        before = path.read_bytes()
        self.assert_code('TRANSITION_EXTERNAL_EDIT', obj.finish)
        self.assert_code('TRANSITION_EXTERNAL_EDIT', obj.rollback)
        self.assertEqual(path.read_bytes(), before)

    def test_foreign_enable_link_and_dropin_are_not_removed_by_recovery(self):
        obj = self.interrupt('journal:prepared'); manifest = service.load(self.directory)
        link = service.enabled_path(manifest); link.parent.mkdir(mode=0o700)
        link.symlink_to('/foreign/keep.service')
        self.assert_code('TRANSITION_UNIT_CHANGED', obj.finish)
        self.assertTrue(link.is_symlink()); link.unlink(); self.manager.dropins = '/foreign/dropin.conf'
        self.assert_code('TRANSITION_UNIT_CHANGED', obj.rollback)

    def test_unknown_phase_and_missing_record_never_reuse_old_success(self):
        self.transition.apply(self.transition.plan()); value = self.transition.load()
        value['phase'] = 'unknown'; ws.write(self.transition.path, value)
        self.assert_code('TRANSITION_FINISH_REFUSED', self.transition.finish)
        self.transition.path.unlink()
        with self.assertRaises(FileNotFoundError): self.transition.finish()
        self.assert_paused()

    def test_initial_record_write_failure_does_not_request_stop(self):
        proposal = self.transition.plan(); old = tx.ws.write
        def fail(path, value):
            if path == self.transition.path: raise OSError('fixture full disk')
            old(path, value)
        with patch.object(tx.ws, 'write', fail), self.assertRaises(OSError): self.transition.apply(proposal)
        self.assertEqual(self.manager.stops, 0); self.assertFalse(self.transition.path.exists())
        self.assertEqual(worker.code_hash(self.root), tx.BASE)

    def child(self, action):
        program = r'''
import importlib.util,json,pathlib,sys
root,source,directory=map(pathlib.Path,sys.argv[1:4]);sys.path.insert(0,str(root))
from src.ops.platform.ops import worker_state as ws,refresh_worker as worker,service_lifecycle as service
from src.ops.platform.tests.test_service_lifecycle import Manager
spec=importlib.util.spec_from_file_location('transition',source/'transition.py');tx=importlib.util.module_from_spec(spec);spec.loader.exec_module(tx)
saved=ws.read(ws.location(directory).parent/'code-transition.json');unit=saved['before']['unit_name']
manager=Manager(pathlib.Path(saved['before']['unit_path']).parents[2],unit)
def snap(_):
 value=dict(saved['before']['binding']);value['code_sha256']=worker.code_hash(root);return value
obj=tx.Transition(directory,tx.Bundle(source),manager,snapshot=snap,lineage=lambda _:{'pending':None,'evidence':{'fixture':'hash'},'next':1,'records':[]})
try:
 action=sys.argv[4]
 if action=='gate':ws.Store(directory).gate();answer={'code':'unexpected'}
 elif action=='resume':answer=service.run('resume',directory,backend=manager)
 else:answer=getattr(obj,action)()
except Exception as e:answer={'code':getattr(e,'code',type(e).__name__)}
print(json.dumps(answer))
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(self.root), str(FOLDER), str(self.directory), action],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-800:]); return json.loads(result.stdout)

    def test_fresh_process_gate_and_resume_refuse_partial_transaction(self):
        self.interrupt('file:' + tx.TARGETS[3])
        self.assertEqual(self.child('gate')['code'], 'CODE_TRANSITION_RECOVERY_REQUIRED')
        self.assertEqual(self.child('resume')['code'], 'CODE_TRANSITION_RECOVERY_REQUIRED')

    def test_actual_process_death_is_recoverable_from_disk_without_saved_pid_signal(self):
        proposal = self.transition.plan()
        pid = os.fork()
        if pid == 0:
            def crash(stage):
                if stage == 'file:service': os._exit(19)
            try: self.new(hook=crash).apply(proposal)
            except BaseException: os._exit(21)
            os._exit(22)
        _, status = os.waitpid(pid, 0); self.assertEqual(os.waitstatus_to_exitcode(status), 19)
        self.assertEqual(self.child('finish')['phase'], 'committed'); self.assert_paused()

    def test_completed_boot_upgrade_refuses_ordinary_resume_before_live_readback(self):
        self.transition.apply(self.transition.plan())
        # Stub preflight to isolate the candidate lifecycle's explicit-start gate.
        program = r'''
import pathlib,sys,json
sys.path.insert(0,sys.argv[1])
from src.ops.platform.ops import service_lifecycle as s,lifecycle_probe as p
from src.ops.platform.tests.test_service_lifecycle import Manager
d=pathlib.Path(sys.argv[2]);v=s.load(d);m=Manager(pathlib.Path(v['unit_path']).parents[2],v['unit_name'])
r=s.run('resume',d,backend=m,reader=p.Reader(),preflight_fn=lambda *a,**k:{'blockers':[],'persistence':{'selected':'boot','linger':True}},recovery_fn=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('unexpected recovery')))
print(json.dumps(r))
'''
        r = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(self.root), str(self.directory)], capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr[-800:]); self.assertEqual(json.loads(r.stdout)['code'], 'BOOT_EXPLICIT_START_ADAPTER_REQUIRED')

    def test_journal_binding_or_backup_index_tampering_is_rejected(self):
        obj = self.interrupt('journal:prepared'); record = obj.load(); original = deepcopy(record)
        record['after']['binding']['python'] = '/different/python'; ws.write(obj.path, record)
        self.assert_code('TRANSITION_RECORD_INVALID', obj.finish)
        record = original; record['files'][0]['index'] = '../not-allowed'; ws.write(obj.path, record)
        self.assert_code('TRANSITION_RECORD_INVALID', obj.finish)

    def test_real_child_holding_lease_blocks_rebind_until_child_exits(self):
        obj = self.interrupt('journal:prepared')
        ready_r, ready_w = os.pipe(); release_r, release_w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(ready_r); os.close(release_w)
            with ws.automatic_lease(self.directory): os.write(ready_w, b'1'); os.read(release_r, 1)
            os._exit(0)
        os.close(ready_w); os.close(release_r)
        try:
            self.assertEqual(os.read(ready_r, 1), b'1')
            self.assert_code('AUTOMATIC_WORKER_BUSY', obj.finish)
        finally:
            os.write(release_w, b'1'); os.close(release_w); os.close(ready_r); os.waitpid(pid, 0)
        obj.finish(); self.assert_paused()


if __name__ == '__main__': unittest.main()
