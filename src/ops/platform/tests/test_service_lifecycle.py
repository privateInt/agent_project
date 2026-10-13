"""Private fixture lifecycle and real child processes; never control a real unit."""
from copy import deepcopy
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

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import manage, worker_state as ws, refresh_worker as worker
from src.ops.platform.ops import service_lifecycle as service, service_systemd as adapter, lifecycle_probe as probe


class Manager:
    """Explicit synthetic manager. Only temporary paths are projected."""
    def __init__(self, root, unit):
        self.root = root; self.unit = unit; self.env_binary = '/usr/bin/env'; self.control_commands = 0
        self.active = False; self.starts = 0; self.stops = 0; self.reloads = 0; self.resets = 0
        self.dropins = ''; self.fragment = None; self.pid = '0'; self.invocation = ''
        self.empty = True; self.fail_reload = False; self.fail_start = False
    @property
    def path(self): return self.root / 'systemd/user' / self.unit
    def unit_paths(self): return [str(self.path.parent)]
    def inspect(self):
        exists = self.path.exists()
        return {'Id': self.unit, 'LoadState': 'loaded' if exists else 'not-found',
                'FragmentPath': self.fragment or (str(self.path) if exists else ''),
                'DropInPaths': self.dropins, 'NeedDaemonReload': 'no', 'ControlGroup': '', 'Job': '0',
                'UnitFileState': ('enabled' if (self.path.parent / 'default.target.wants' / self.unit).is_symlink() else 'disabled') if exists else '',
                'ActiveState': 'active' if self.active else 'inactive', 'SubState': 'running' if self.active else 'dead',
                'MainPID': self.pid, 'InvocationID': self.invocation, 'Result': 'success', 'ExecMainStatus': '0'}
    def verify(self, manifest): pass
    def reload(self):
        self.control_commands += 1; self.reloads += 1
        if self.fail_reload:
            self.fail_reload = False; raise ReadFailure('SERVICE_COMMAND_OUTCOME_UNKNOWN')
    def start(self):
        self.control_commands += 1; self.starts += 1
        if self.fail_start: raise ReadFailure('SERVICE_COMMAND_OUTCOME_UNKNOWN')
        self.active = True; self.pid = '123456'; self.invocation = uuid.uuid4().hex
    def stop(self):
        self.control_commands += 1; self.stops += 1; self.active = False; self.pid = '0'
    def reset_failed(self): self.control_commands += 1; self.resets += 1
    def cgroup_empty(self, current): return self.empty


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ops-service-check-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.directory = self.root / 'server'; self.directory.mkdir(mode=0o700)
        self.owner = {'schema': 1, 'uid': os.getuid(), 'gid': os.getgid(), 'owner': str(uuid.uuid4()), 'project_root': str(worker.ROOT)}
        manage.write_new(self.directory / 'owner.json', self.owner)
        self.binding = {'directory': str(self.directory), 'owner': self.owner, 'code_sha256': 'a'*64,
                        'python': str(Path(sys.executable).resolve()), 'python_version': list(sys.version_info[:3]),
                        'docker_path': '/synthetic/bin/docker', 'daemon_id': 'synthetic', 'ops_container_id': 'b'*64, 'ops_image_id': 'sha256:'+'c'*64}
        self.reader = probe.Reader(env={'XDG_CONFIG_HOME': str(self.root / 'config'), 'PATH': os.defpath})
        self.manager = Manager(self.root / 'config', 'agent-project-ops-refresh-' + self.owner['owner'].replace('-', '') + '.service')
        self.report = {'blockers': [], 'docker': {'endpoint': 'unix:///synthetic/docker.sock'},
                       'persistence': {'selected': 'session', 'linger': False}}
        self.recoveries = 0
    def recover(self, directory, resume_guard=None):
        self.recoveries += 1; store = ws.Store(directory)
        with store.lock():
            if resume_guard: resume_guard(store)
            control = store.control(); control.update(desired='running', recovery=None)
            ws.write(store.path / 'control.json', control)
        store.status(phase='recovery_acknowledged', completed_cycles=0)
    def call(self, action, **kw):
        return service.run(action, self.directory, reader=self.reader, backend=self.manager,
            preflight_fn=lambda *a, **k: deepcopy(self.report), snapshot_fn=lambda _: deepcopy(self.binding),
            recovery_fn=kw.pop('recovery_fn', self.recover), **kw)
    def good(self, action, **kw):
        r = self.call(action, **kw)
        # Keep the useful diagnostic at the start of the bounded checker error,
        # instead of losing the code after a long dictionary of effect flags.
        self.assertEqual(r['result'], 'passed', 'code=' + str(r.get('code')) + '; action=' + action)
        return r

    def test_install_inactive_private_idempotent_without_keys(self):
        first = self.good('install'); self.assertTrue(first['unit_written']); self.assertEqual(self.manager.starts, 0)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        second = self.good('install'); self.assertFalse(second['unit_written'])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        self.assertEqual(ws.Store(self.directory).control()['desired'], 'paused')
        self.assertEqual(self.manager.path.stat().st_mode & 0o777, 0o600)

    def test_start_auto_installs_and_duplicate_does_not_start_again(self):
        self.good('start'); r = self.good('start')
        self.assertTrue(r['already_running']); self.assertEqual(self.manager.starts, 1)
        self.assertEqual(r['management']['evaluation_status'], 'first_evaluation_pending')

    def test_stop_persists_pause_before_backend_and_requires_resume(self):
        self.good('start'); original = self.manager.stop
        def stop():
            self.assertEqual(ws.Store(self.directory).control()['desired'], 'paused'); original()
        self.manager.stop = stop
        self.good('stop'); r = self.call('start')
        self.assertEqual(r['code'], 'SERVICE_EXPLICIT_RESUME_REQUIRED')
        self.good('resume'); self.assertEqual(self.recoveries, 1); self.assertEqual(self.manager.resets, 0)

    def test_boot_links_owned_only_and_stop_disables(self):
        self.report['persistence'] = {'selected': 'boot', 'linger': True}
        self.good('start'); manifest = service.load(self.directory); self.assertTrue(service.enabled(manifest))
        self.good('stop'); self.assertFalse(service.enabled(manifest))

    def test_saved_boot_policy_cannot_silently_downgrade(self):
        self.report['persistence'] = {'selected': 'boot', 'linger': True}; self.good('install')
        self.report['persistence'] = {'selected': 'session', 'linger': False}
        r = self.call('start'); self.assertEqual(r['code'], 'BOOT_PERSISTENCE_UNAVAILABLE'); self.assertEqual(self.manager.starts, 0)

    def test_changed_unit_and_dropin_are_not_overwritten(self):
        self.good('install'); self.manager.path.write_text('manual edit\n')
        r = self.call('install'); self.assertEqual(r['code'], 'SERVICE_UNIT_CHANGED')
        self.assertEqual(self.manager.path.read_text(), 'manual edit\n')
        self.manager.path.write_text(adapter.render(service.load(self.directory)))
        self.manager.dropins = '/synthetic/override.conf'
        self.assertEqual(self.call('start')['code'], 'SERVICE_DROPIN_CONFLICT'); self.assertEqual(self.manager.starts, 0)

    def test_unit_symlink_and_foreign_enable_link_refused(self):
        # pathlib mkdir(parents=True) uses default permissions for intermediate
        # directories. Create every fixture directory explicitly: a caller's
        # umask 0002 must not turn this symlink test into a permissions test.
        for path in (self.manager.root, self.manager.root / 'systemd', self.manager.path.parent):
            path.mkdir(mode=0o700)
        self.manager.path.symlink_to(self.root / 'missing')
        self.assertEqual(self.call('install')['code'], 'SYMLINK_REFUSED'); self.manager.path.unlink()
        self.good('install'); link = service.enabled_path(service.load(self.directory)); link.parent.mkdir(mode=0o700)
        link.symlink_to('/synthetic/foreign.service')
        self.assertEqual(self.call('start')['code'], 'SERVICE_ENABLE_LINK_CONFLICT'); self.assertTrue(link.is_symlink())

    def test_symlink_fixture_passes_under_four_child_process_umasks(self):
        program = ('import os,sys,unittest; sys.dont_write_bytecode=True; sys.path.insert(0,sys.argv[1]); '
            'from src.ops.platform.tests.test_service_lifecycle import ServiceTests; '
            'os.umask(int(sys.argv[2],8)); '
            'r=unittest.TextTestRunner(verbosity=0).run(unittest.TestSuite(['
            'ServiceTests("test_unit_symlink_and_foreign_enable_link_refused")])); '
            'sys.exit(0 if r.wasSuccessful() else 2)')
        for mask in ('0000', '0002', '0022', '0077'):
            with self.subTest(umask=mask):
                result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(worker.ROOT), mask],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 0, 'fixture_umask=' + mask + '; ' + result.stderr[-1000:])

    def test_group_writable_directory_still_refused_without_permission_repair(self):
        for path in (self.manager.root, self.manager.root / 'systemd', self.manager.path.parent):
            path.mkdir(mode=0o700)
        self.manager.path.parent.chmod(0o770)  # This test's private temporary path only.
        result = self.call('install')
        self.assertEqual(result['code'], 'SERVICE_DIRECTORY_UNSAFE')
        self.assertEqual(self.manager.path.parent.stat().st_mode & 0o777, 0o770)
        self.assertFalse(self.manager.path.exists()); self.assertEqual(self.manager.starts, 0)
        self.assertEqual(self.manager.control_commands, 0)
        self.assertEqual(ws.Store(self.directory).control()['desired'], 'paused')

    def test_install_reload_failure_rolls_back_only_own_unit(self):
        self.manager.fail_reload = True
        self.assertEqual(self.call('install')['code'], 'SERVICE_COMMAND_OUTCOME_UNKNOWN')
        self.assertFalse(self.manager.path.exists()); self.assertEqual(service.load(self.directory)['phase'], 'install_failed')
        self.good('install'); self.assertEqual(self.manager.starts, 0)

    def test_unit_validation_failure_creates_no_management_state(self):
        self.manager.verify = lambda _: (_ for _ in ()).throw(ReadFailure('SERVICE_UNIT_VALIDATION_FAILED'))
        self.assertEqual(self.call('install')['code'], 'SERVICE_UNIT_VALIDATION_FAILED')
        self.assertFalse(service.manifest_path(self.directory).exists()); self.assertFalse(self.manager.path.exists())

    def test_unknown_start_outcome_latches_without_second_start(self):
        self.manager.fail_start = True
        self.assertEqual(self.call('start')['code'], 'SERVICE_COMMAND_OUTCOME_UNKNOWN')
        control = ws.Store(self.directory).control(); self.assertEqual(control['desired'], 'paused'); self.assertIsNotNone(control['recovery'])
        self.assertEqual(self.call('start')['code'], 'SERVICE_EXPLICIT_RESUME_REQUIRED'); self.assertEqual(self.manager.starts, 1)

    def test_resume_pending_check_failure_does_not_reset_or_start(self):
        self.good('install')
        fail = lambda *a, **kw: (_ for _ in ()).throw(ReadFailure('REFRESH_PENDING_USE_CHECK_OR_ROLLBACK'))
        self.assertEqual(self.call('resume', recovery_fn=fail)['code'], 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
        self.assertEqual((self.manager.resets, self.manager.starts), (0, 0))

    def test_changed_binding_refuses_start_but_stop_remains_available(self):
        self.good('install'); self.binding['code_sha256'] = 'd'*64
        self.assertEqual(self.call('start')['code'], 'SERVICE_BINDING_CHANGED')
        self.report['blockers'] = ['DOCKER_QUERY_FAILED']; self.good('stop')

    def test_remove_requires_stop_and_preserves_state_audit_and_unrelated_unit(self):
        self.good('start'); self.assertEqual(self.call('remove')['code'], 'SERVICE_STOP_REQUIRED')
        self.good('stop'); unrelated = self.manager.path.parent / 'unrelated.service'; unrelated.write_text('unchanged')
        owner = (self.directory / 'owner.json').read_bytes(); self.good('remove'); self.good('remove')
        self.assertFalse(self.manager.path.exists()); self.assertEqual(unrelated.read_text(), 'unchanged')
        self.assertEqual((self.directory / 'owner.json').read_bytes(), owner)
        self.assertTrue((ws.location(self.directory) / 'control.json').exists())
        self.assertEqual(service.load(self.directory)['phase'], 'removed')

    def test_stop_cannot_claim_success_while_cgroup_populated(self):
        self.good('start'); self.manager.empty = False
        ticks = iter([0, 999])
        r = self.call('stop', _clock=lambda: next(ticks), _sleep=lambda _: None)
        self.assertEqual(r['code'], 'SERVICE_STOP_OUTCOME_UNKNOWN'); self.assertEqual(ws.Store(self.directory).control()['desired'], 'paused')

    def test_singleton_prevents_install_during_foreground_watch(self):
        with ws.automatic_lease(self.directory):
            self.assertEqual(self.call('install')['code'], 'AUTOMATIC_WORKER_BUSY')
        self.assertFalse(service.manifest_path(self.directory).exists())

    def test_status_rejects_old_invocation_pid_and_stale_timestamp(self):
        self.good('start'); manifest = service.load(self.directory)
        identity = worker.process_identity(); current = self.manager.inspect(); current['MainPID'] = str(identity['proc_pid'])
        observation = {'identity': identity, 'service_invocation': current['InvocationID'], 'phase': 'waiting', 'checked_at': worker.activation.rc.now()}
        self.assertTrue(service.current_observation(manifest, current, observation))
        observation['service_invocation'] = '0'*32; self.assertFalse(service.current_observation(manifest, current, observation))
        observation['service_invocation'] = current['InvocationID']; observation['checked_at'] = '2000-01-01T00:00:00Z'
        self.assertFalse(service.current_observation(manifest, current, observation))

    def test_status_does_not_create_files_or_run_control(self):
        self.good('install'); before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        commands = self.manager.control_commands
        service.status(self.directory, self.manager)
        self.assertEqual(commands, self.manager.control_commands)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()})

    def test_unit_encoding_clean_environment_and_deadline(self):
        self.good('install'); manifest = service.load(self.directory); manifest['environment']['HOME'] = '/tmp/a b%"z'
        text = adapter.render(manifest)
        self.assertIn('/tmp/a b%%\\"z', text); self.assertIn('INVOCATION_ID=${INVOCATION_ID}', text)
        self.assertIn('"-i"', text); self.assertIn('TimeoutStopSec=200', text); self.assertIn('RestartPreventExitStatus=2', text)
        self.assertNotIn('EnvironmentFile=', text); self.assertNotIn('sh -c', text)

    def test_internal_entry_missing_manager_invocation_exits_two(self):
        self.good('install')
        from src.ops.platform.ops import service_entry
        with patch.dict(os.environ, {}, clear=True), patch('builtins.print'):
            self.assertEqual(service_entry.main(['--state-dir', str(self.directory)]), 2)

    def test_stop_records_pause_even_when_unit_ownership_check_fails(self):
        self.good('start'); self.manager.path.write_text('user changed unit')
        self.assertEqual(self.call('stop')['code'], 'SERVICE_UNIT_CHANGED')
        self.assertEqual(ws.Store(self.directory).control()['desired'], 'paused')
        self.assertEqual(self.manager.stops, 0)

    def test_install_does_not_choose_a_directory_outside_manager_search_path(self):
        self.manager.unit_paths = lambda: ['/some/other/user/config']
        self.assertEqual(self.call('install')['code'], 'SERVICE_UNIT_DIRECTORY_NOT_IN_MANAGER_PATH')
        self.assertFalse(service.manifest_path(self.directory).exists())

    def test_explicit_persistence_policy_is_not_replaced(self):
        self.good('install')
        self.assertEqual(self.call('start', persistence='boot')['code'], 'SERVICE_POLICY_CHANGE_REQUIRES_REVIEW')

    def test_stop_with_unavailable_manager_still_persists_pause(self):
        self.good('start')
        with patch.object(probe, 'systemd', return_value={'status': 'unavailable'}):
            with self.assertRaises(ReadFailure): service.run('stop', self.directory, reader=self.reader)
        self.assertEqual(ws.Store(self.directory).control()['desired'], 'paused')

    def test_interrupted_owned_install_finishes_only_inactive_identical_unit(self):
        self.good('install'); manifest = service.load(self.directory); manifest['phase'] = 'installing'
        ws.write(service.manifest_path(self.directory), manifest)
        self.good('install'); self.assertEqual(service.load(self.directory)['phase'], 'installed')
        self.assertEqual(self.manager.starts, 0)

    def test_removal_retry_after_reload_failure_preserves_audit(self):
        self.good('install'); self.manager.fail_reload = True
        self.assertEqual(self.call('remove')['code'], 'SERVICE_COMMAND_OUTCOME_UNKNOWN')
        self.assertFalse(self.manager.path.exists()); self.assertEqual(service.load(self.directory)['phase'], 'removing')
        self.good('remove'); self.assertEqual(service.load(self.directory)['phase'], 'removed')

    def test_concurrent_stop_cancels_start_during_preflight(self):
        self.good('install')
        def preflight(*a, **kw):
            service.request_pause(self.directory)
            return deepcopy(self.report)
        r = service.run('start', self.directory, reader=self.reader, backend=self.manager,
                        preflight_fn=preflight, snapshot_fn=lambda _: self.binding)
        self.assertEqual(r['code'], 'SERVICE_STOP_REQUEST_CHANGED'); self.assertEqual(self.manager.starts, 0)
        self.assertEqual(ws.Store(self.directory).control()['desired'], 'paused')

    def test_concurrent_stop_cancels_resume_before_latch_release(self):
        self.good('install')
        def recovery(directory, resume_guard=None):
            service.request_pause(directory); self.recover(directory, resume_guard=resume_guard)
        self.assertEqual(self.call('resume', recovery_fn=recovery)['code'], 'SERVICE_STOP_REQUEST_CHANGED')
        self.assertEqual(self.manager.starts, 0); self.assertEqual(ws.Store(self.directory).control()['desired'], 'paused')


class AdapterTests(unittest.TestCase):
    def make(self, runner):
        reader = probe.Reader(env={'PATH': os.defpath, 'UNRELATED_SECRET': 'never-copy'}, runner=runner)
        reader.paths['systemctl'] = '/usr/bin/systemctl'
        return adapter.Systemd(reader, {'status': 'ok', 'environment': {}}, 'agent-project-ops-refresh-' + 'a'*32 + '.service')

    def test_mutations_allow_only_owned_unit_and_sanitize_environment(self):
        calls = []
        def runner(args, **kw): calls.append((args, kw)); return SimpleNamespace(returncode=0, stdout='', stderr='')
        backend = self.make(runner); backend.start(); backend.stop(); backend.reload(); backend.reset_failed()
        self.assertEqual(len(calls), 4)
        for _, kw in calls: self.assertNotIn('UNRELATED_SECRET', kw['env']); self.assertLessEqual(kw['timeout'], 10)
        with self.assertRaises(ReadFailure): backend.call(['start', 'another.service'], control=True)
        with self.assertRaises(ReadFailure): backend.call(['daemon-reload'])

    def test_timeout_is_unknown_not_failed_and_does_not_leak_stderr(self):
        def timeout(*a, **kw): raise subprocess.TimeoutExpired(a, 10)
        backend = self.make(timeout)
        with self.assertRaises(ReadFailure) as caught: backend.start()
        self.assertEqual(caught.exception.code, 'SERVICE_COMMAND_OUTCOME_UNKNOWN')
        backend = self.make(lambda *a, **kw: SimpleNamespace(returncode=1, stdout='', stderr='SECRET'))
        with self.assertRaises(ReadFailure) as caught: backend.start()
        self.assertNotIn('SECRET', str(caught.exception))


class ProcessServiceTests(unittest.TestCase):
    def setUp(self):
        self.f = ServiceTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.child = None
        self.addCleanup(self.cleanup_child)

    def cleanup_child(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try: self.child.wait(timeout=5)
            except subprocess.TimeoutExpired: self.child.kill(); self.child.wait(timeout=5)

    def start_child(self, classification='accepted'):
        manager = self.f.manager
        manager.invocation = uuid.uuid4().hex; manager.starts += 1; manager.control_commands += 1
        # The fixture injects only a synthetic evaluator/snapshot into the real
        # core, in a real child process. It does not invoke the production entry.
        program = ('import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
            'from src.ops.platform.ops import refresh_worker as w, service_lifecycle as s; '
            'd=Path(sys.argv[2]); m=s.load(d); '
            'r=w.run(d,interval=30,deadline_seconds=3,snapshot_fn=lambda _:m["binding"],'
            'evaluate_fn=lambda *_:{"result":"passed","classification":sys.argv[4],"code":"SYNTHETIC_BLOCK"},'
            'service_invocation=sys.argv[3]); sys.exit(r["exit_code"])')
        self.child = subprocess.Popen([sys.executable, '-I', '-B', '-c', program, str(worker.ROOT), str(self.f.directory),
            manager.invocation, classification], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            observation = ws.Store(self.f.directory).optional('status.json') or {}
            if observation.get('service_invocation') == manager.invocation:
                manager.pid = str(observation['identity']['proc_pid']); manager.active = True; return
            if self.child.poll() is not None: break
            time.sleep(.02)
        raise AssertionError('isolated worker did not initialize')

    def stop_child(self):
        self.assertEqual(ws.Store(self.f.directory).control()['desired'], 'paused')
        # The durable pause can already be ending the worker. Wait for that
        # exit before signalling, so SIGTERM cannot hit after handlers restore.
        try: self.child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.child.terminate(); self.child.wait(timeout=5)
        self.assertEqual(self.child.returncode, 0)
        self.f.manager.active = False; self.f.manager.pid = '0'; self.f.manager.control_commands += 1

    def test_real_worker_start_duplicate_pause_exit_and_resume_new_invocation(self):
        self.f.manager.start = self.start_child; self.f.manager.stop = self.stop_child
        self.f.good('start'); initial = self.f.manager.invocation
        self.f.good('start'); self.assertEqual(self.f.manager.starts, 1)
        observation = service.status(self.f.directory, self.f.manager)
        self.assertTrue(observation['current_invocation_verified'])
        self.f.good('stop'); self.f.good('resume')
        self.assertNotEqual(initial, self.f.manager.invocation)
        self.f.good('stop'); self.f.good('remove')
        self.assertFalse(self.f.manager.path.exists())

    def test_real_worker_blocked_exit_two_and_record_cannot_be_clean_started(self):
        self.f.manager.start = lambda: self.start_child('blocked')
        self.f.good('start'); self.child.wait(timeout=5); self.assertEqual(self.child.returncode, 2)
        self.f.manager.active = False; self.f.manager.pid = '0'
        self.assertEqual(ws.Store(self.f.directory).control()['recovery'], 'SYNTHETIC_BLOCK')
        self.assertEqual(self.f.call('start')['code'], 'SERVICE_EXPLICIT_RESUME_REQUIRED')
        self.assertEqual(ws.Store(self.f.directory).control()['recovery'], 'SYNTHETIC_BLOCK')


class NativeUnitTests(unittest.TestCase):
    def test_generated_unit_passes_real_offline_systemd_parser(self):
        import shutil
        binary = os.environ.get('SERVICE_ANALYZE_TEST_BINARY') or shutil.which('systemd-analyze')
        if not binary: self.skipTest('systemd-analyze unavailable')
        f = ServiceTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.good('install'); manifest = service.load(f.directory)
        runtime = f.root / 'offline-runtime'; runtime.mkdir(mode=0o700)
        reader = probe.Reader(env={'PATH': os.defpath, 'HOME': str(f.root), 'XDG_RUNTIME_DIR': str(runtime)})
        backend = adapter.Systemd(reader, {'status': 'ok', 'environment': {}}, f.manager.unit); backend.analyze = binary
        backend.verify(manifest)
        # Prove that a malformed unit is rejected by the same invocation.
        with patch.object(adapter, 'render', return_value='[Service]\nExecStart=/not/an/executable\n'):
            with self.assertRaises(ReadFailure) as caught: backend.verify(manifest)
        self.assertEqual(caught.exception.code, 'SERVICE_UNIT_VALIDATION_FAILED')
