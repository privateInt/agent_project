"""Read-only lifecycle probes: private fixtures, no production service or key reads."""
from copy import deepcopy
import fcntl
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

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import auto_refresh as a, lifecycle_probe as probe, lifecycle_state as state
from src.ops.platform.ops import manage, activate_discovery as activation, set_public_url as change
from src.ops.platform.ops import bootstrap_discovery as bootstrap
from src.ops.platform.tests import test_discovery_activation as fixtures
from src.ops.platform.tests.test_restart_check import IDENT, TOKEN, LF_SECRET


class Commands:
    def __init__(self, raw):
        self.raw = deepcopy(raw); self.calls = []; self.linger = 'yes'; self.context_endpoint = 'unix:///synthetic/docker.sock'
        self.ids = [IDENT]; self.unit = 'not-found'; self.fail = None; self.rootless = False
        self.manager_version = '255'; self.manager_state = 'running'; self.runtime_path = ''

    def __call__(self, args, **kwargs):
        tool = Path(args[0]).name; tail = args[1:]; self.calls.append((tool, tail, kwargs))
        assert probe.allow(tool, tail), tail
        if self.fail and self.fail(tool, tail):
            return SimpleNamespace(returncode=1, stdout='', stderr='private-secret-not-for-output')
        if tool == 'docker':
            if tail == ['context', 'show']: out = 'local-fixture'
            elif tail[:2] == ['context', 'inspect']: out = self.context_endpoint
            else:
                cmd = tail[2:]
                if cmd[:1] == ['info']:
                    out = json.dumps({'id': 'synthetic-daemon', 'os': 'linux', 'security': ['name=rootless'] if self.rootless else [], 'version': '28.4.0'})
                elif cmd[:2] == ['compose', 'version']: out = '2.39.0'
                elif cmd[0] == 'ps': out = '\n'.join(self.ids)
                elif cmd[:2] == ['container', 'inspect']: out = json.dumps(self.raw)
                else: raise AssertionError(cmd)
        elif tool == 'systemctl':
            if tail == ['--version']: out = 'systemd 255 (synthetic fixture)'
            elif tail[0] == '--system':
                out = 'Id=user@'+str(os.getuid())+'.service\nLoadState=loaded\nActiveState=active\nSubState=running\nResult=success'
            elif tail[-1] == '--property=' + probe.MANAGER_PROPERTIES:
                out = 'Version=' + self.manager_version + '\nSystemState=' + self.manager_state
            else: out = 'LoadState=' + self.unit
        else:
            fields = {'UID': str(os.getuid()), 'Linger': self.linger, 'RuntimePath': self.runtime_path, 'State': 'active'}
            selected = [x[len('--property='):] for x in tail if x.startswith('--property=')]
            out = '\n'.join(('' if '--value' in tail else k+'=') + v for k,v in fields.items() if k in selected)
        return SimpleNamespace(returncode=0, stdout=out, stderr='')


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.ActivationTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.h.call(); self.f = self.h.f
        self.commands = Commands(self.f.docker.raw)
        self.reader = probe.Reader(env={'PATH': '/synthetic/bin'}, runner=self.commands,
                                   which=lambda name, **kw: '/synthetic/bin/' + name)

    def run_probe(self, action='plan', **kwargs):
        return a.run(action, self.f.directory, reader=self.reader, **kwargs)

    def test_plan_passes_metadata_without_install_or_upstream_calls(self):
        before = tree(self.f.root); restarts = self.f.docker.restarts; logins = self.f.api.logins
        r = self.run_probe()
        self.assertEqual(r['result'], 'passed', r); self.assertTrue(r['environment_ready'])
        self.assertEqual(r['proposal']['owned_fields'], ['alertmanager_url', 'resource_profile'])
        self.assertEqual(r['management']['status'], 'not_installed'); self.assertTrue(r['capabilities']['start'])
        self.assertEqual(r['persistence']['selected'], 'boot'); self.assertEqual(tree(self.f.root), before)
        self.assertEqual((self.f.docker.restarts, self.f.api.logins), (restarts, logins))

    def test_status_is_distinct_and_does_not_claim_worker_running(self):
        r = self.run_probe('status'); self.assertNotIn('proposal', r)
        self.assertEqual(r['worker_status'], 'guarded_service_implemented'); self.assertEqual(r['evaluation_status'], 'not_performed')

    def test_worker_core_metadata_does_not_claim_service_install_or_live_process(self):
        from src.ops.platform.ops import worker_state
        worker_state.Store(self.f.directory, {'fixture': 'isolated'})
        before = tree(self.f.root)
        r = self.run_probe('status')
        self.assertEqual(r['management']['status'], 'core_only_service_not_installed')
        self.assertFalse(r['worker']['live_process_verified']); self.assertTrue(r['capabilities']['start'])
        self.assertEqual(tree(self.f.root), before)

    def test_worker_open_intent_is_reported_as_a_blocker_without_clearing_it(self):
        from src.ops.platform.ops import worker_state
        store = worker_state.Store(self.f.directory, {'fixture': 'isolated'})
        worker_state.write(store.path / 'intent.json', {'phase': 'open', 'evaluation': 'fixture', 'generation': 1})
        before = tree(self.f.root); r = self.run_probe()
        self.assertIn('WORKER_UNFINISHED_INTENT', r['blockers']); self.assertEqual(tree(self.f.root), before)

    def test_credentials_are_not_opened_or_serialized(self):
        forbidden = {str(self.f.directory / 'access-key'), self.f.cfg['langfuse_file'], self.f.cfg['platforms']['agent']['token_file'], str(self.f.cache)}
        real_open = os.open
        real_io_open = io.open
        def guarded_io(path, *args, **kwargs):
            if isinstance(path, (str, Path)): self.assertNotIn(str(path), forbidden)
            return real_io_open(path, *args, **kwargs)
        def guarded(path, flags, *args, **kwargs):
            if isinstance(path, (str, Path)):
                self.assertNotIn(str(path), forbidden)
                self.assertNotIn(str(path), {'access-key', 'agent-key', 'lf.json', 'api-success.json'})
            return real_open(path, flags, *args, **kwargs)
        with patch('os.open', side_effect=guarded), patch('io.open', side_effect=guarded_io), \
             patch('src.ops.platform.transport.secret', side_effect=AssertionError('secret read')), \
             patch('src.ops.platform.transport.Transport.get', side_effect=AssertionError('upstream request')):
            r = self.run_probe()
        self.assertEqual(r['result'], 'passed', r)
        for secret in (TOKEN, LF_SECRET, 'synthetic-ops-access-key-for-tests-123456'):
            self.assertNotIn(secret, json.dumps(r))

    def test_missing_state_is_not_created(self):
        path = self.f.root / 'missing' / 'server'
        r = a.run('plan', path, reader=self.reader)
        self.assertIn('PREPARED_STATE_NOT_FOUND', r['blockers']); self.assertFalse(path.parent.exists())

    def test_wrong_owner_blocks_project_specific_container_queries(self):
        path = self.f.directory / 'owner.json'; obj = json.loads(path.read_text()); obj['uid'] += 1; path.write_text(json.dumps(obj))
        r = self.run_probe(); self.assertIn('OWNER_MISMATCH', r['blockers'])
        self.assertFalse(any('ps' in cmd for _, cmd, _ in self.commands.calls))

    def test_different_checkout_is_not_automatically_adopted(self):
        r = self.run_probe(root=self.f.root)
        self.assertIn('OWNER_MISMATCH', r['blockers'])

    def test_private_mode_and_symlink_refused_without_repair(self):
        path = self.f.directory / 'owner.json'; path.chmod(0o644)
        r = self.run_probe(); self.assertIn('PRIVATE_STATE_REQUIRED', r['blockers']); self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        path.chmod(0o600); target = self.f.root / 'other'; path.rename(target); path.symlink_to(target)
        r = self.run_probe(); self.assertEqual(r['result'], 'attention_required'); self.assertTrue(path.is_symlink())

    def test_fifo_owner_is_rejected_without_blocking(self):
        p = self.f.directory / 'owner.json'; p.unlink(); os.mkfifo(p, 0o600)
        r = self.run_probe(); self.assertIn('INVALID_LOCAL_FILE', r['blockers'])

    def test_valid_preparation_in_space_percent_path(self):
        factory = tempfile.TemporaryDirectory
        with patch('tempfile.TemporaryDirectory', side_effect=lambda **kw: factory(prefix='ops valid space%-')):
            h = fixtures.ActivationTests(); h.setUp(); self.addCleanup(h.doCleanups); h.call()
        commands = Commands(h.f.docker.raw)
        reader = probe.Reader(env={}, runner=commands, which=lambda name, **kw: '/fake/' + name)
        before = tree(h.f.root); r = a.run('plan', h.f.directory, reader=reader)
        self.assertEqual(r['result'], 'passed', r); self.assertEqual(tree(h.f.root), before)

    def test_config_mismatch_retains_provenance_result(self):
        path = self.f.directory / 'config.json'; cfg = json.loads(path.read_text()); cfg['port'] += 1; path.write_text(json.dumps(cfg))
        r = self.run_probe(); self.assertIn('HOST_CONTAINER_CONFIG_MISMATCH', r['blockers'])
        self.assertEqual(r['installation']['provenance']['status'], 'ok')

    def test_missing_credential_reports_metadata_error_without_creating_it(self):
        p = Path(self.f.cfg['platforms']['agent']['token_file']); p.unlink()
        r = self.run_probe(); self.assertIn('LOCAL_FILE_MISSING', r['blockers']); self.assertFalse(p.exists())

    def test_busy_transaction_is_reported_without_new_lock_file(self):
        before = tree(self.f.root)
        with change.change_lock(self.f.directory): r = self.run_probe()
        self.assertIn('PREPARED_STATE_BUSY', r['blockers']); self.assertEqual(tree(self.f.root), before)

    def test_missing_anchor_does_not_adopt_existing_values(self):
        self.h.evidence.unlink(); r = self.run_probe()
        self.assertIn('DISCOVERY_PROVENANCE_MISSING', r['blockers']); self.assertEqual(r['proposal']['owned_fields'], [])

    def test_activation_pending_is_reported_and_never_resumed(self):
        p = self.h.evidence; record = json.loads(p.read_text()); record['phase'] = 'restart_requested'; p.write_text(json.dumps(record))
        before = p.read_bytes(); r = self.run_probe()
        self.assertIn('ACTIVATION_PENDING', r['blockers']); self.assertEqual(r['recovery']['kind'], 'activation')
        self.assertEqual(p.read_bytes(), before)

    def test_refresh_pending_is_reported_and_never_resumed(self):
        from src.ops.platform.tests.test_discovery_refresh import RefreshTests
        h = RefreshTests(); h.setUp(); self.addCleanup(h.doCleanups); h.changed(); h.failure = 'before'
        with self.assertRaises(ReadFailure): h.call()
        c = Commands(h.docker.raw); reader = probe.Reader(env={}, runner=c, which=lambda name, **kw: '/fake/' + name)
        before = tree(h.h.f.root); r = a.run('plan', h.directory, reader=reader)
        self.assertIn('REFRESH_PENDING_USE_CHECK_OR_ROLLBACK', r['blockers'])
        self.assertEqual(r['recovery']['generation'], 1); self.assertEqual(tree(h.h.f.root), before)

    def test_corrupt_lineage_is_not_healthy(self):
        record = json.loads(self.h.evidence.read_text()); record['baseline']['directory'] = '/other'
        self.h.evidence.write_text(json.dumps(record)); r = self.run_probe()
        self.assertIn('ACTIVATION_RECORD_INVALID', r['blockers'])

    def test_coherent_manual_override_releases_only_effective_plan_ownership(self):
        self.h.replace_configs(lambda cfg: cfg.update(alertmanager_url='http://127.0.0.1:29999'))
        before = self.h.evidence.read_bytes(); r = self.run_probe()
        self.assertEqual(r['result'], 'passed', r); self.assertEqual(r['proposal']['owned_fields'], ['resource_profile'])
        self.assertEqual(r['installation']['provenance']['manual_overrides_detected'], ['alertmanager_url'])
        self.assertEqual(self.h.evidence.read_bytes(), before)

    def test_bootstrap_anchor_supports_eight_managed_fields(self):
        cfg = json.loads((self.f.directory / 'host-config.json').read_text())
        record = json.loads(self.h.evidence.read_text()); fields = record['baseline']['plan']['fields']
        for field in fields.values(): field.update(source='discovery', automatic_future_update_allowed=True)
        b = {'directory': str(self.f.directory), 'owner': self.f.owner, 'fields': fields, 'source_sha256': 'f'*64}
        obj = {'schema': 1, 'kind': bootstrap.STAGE, 'phase': 'configuration_prepared', 'runtime_acceptance': 'pending_first_start_and_readback',
               'baseline': b, 'baseline_sha256': change.digest(b)}
        self.h.evidence.unlink(); p = self.f.directory / bootstrap.RELATIVE; p.parent.mkdir(mode=0o700); manage.write_new(p, obj)
        r = self.run_probe(); self.assertEqual(r['result'], 'passed', r); self.assertEqual(len(r['proposal']['owned_fields']), 8)

    def test_session_scope_is_explicit_when_linger_is_off(self):
        self.commands.linger = 'no'; r = self.run_probe()
        self.assertEqual(r['result'], 'passed', r); self.assertEqual(r['persistence']['selected'], 'session')
        self.assertIn('SESSION_SCOPE_ONLY', r['warnings'])

    def test_boot_request_requires_observed_linger(self):
        self.commands.linger = 'no'; r = self.run_probe(persistence='boot')
        self.assertIn('BOOT_PERSISTENCE_UNAVAILABLE', r['blockers'])

    def test_unknown_linger_does_not_claim_boot_and_session_can_be_selected(self):
        self.commands.fail = lambda tool, args: tool == 'loginctl'
        r = self.run_probe(); self.assertIn('PERSISTENCE_UNDETERMINED', r['blockers'])
        r = self.run_probe(persistence='session'); self.assertEqual(r['result'], 'passed', r)
        self.assertFalse(r['persistence']['boot_available'])

    def test_manager_unavailable_is_distinct_from_binary_presence(self):
        self.commands.fail = lambda tool, args: tool == 'systemctl' and args[0] == '--user'
        r = self.run_probe(); self.assertIn('SYSTEMD_USER_MANAGER_UNAVAILABLE', r['blockers'])
        self.assertIsNotNone(r['command_paths']['systemctl']); self.assertEqual(r['management']['status'], 'unknown')

    def test_running_old_manager_does_not_pass_new_cli_version(self):
        self.commands.manager_version = '239'; r = self.run_probe()
        self.assertIn('SYSTEMD_USER_MANAGER_VERSION_UNSUPPORTED', r['blockers'])

    def test_existing_unit_or_state_is_unowned_not_running(self):
        self.commands.unit = 'loaded'; r = self.run_probe()
        self.assertIn('MANAGEMENT_UNIT_ALREADY_EXISTS', r['blockers']); self.assertEqual(r['management']['status'], 'conflict')
        self.commands.unit = 'not-found\nUnitFileState=enabled'; r = self.run_probe()
        self.assertIn('MANAGEMENT_UNIT_ALREADY_EXISTS', r['blockers'])
        self.commands.unit = 'not-found'; p = self.f.directory.parent / 'auto-refresh' / self.f.owner['owner']; p.mkdir(parents=True)
        r = self.run_probe(); self.assertIn('MANAGEMENT_STATE_ALREADY_EXISTS', r['blockers'])

    def test_container_ambiguity_and_mount_drift_are_not_ready(self):
        self.commands.ids = [IDENT, 'b'*64]; r = self.run_probe(); self.assertIn('OPS_CONTAINER_AMBIGUOUS', r['blockers'])
        self.commands.ids = [IDENT]; self.commands.raw['mounts'][0]['RW'] = True
        r = self.run_probe(); self.assertIn('MOUNT_MISMATCH', r['blockers'])

    def test_docker_rootless_and_stopped_ops_are_reported(self):
        self.commands.rootless = True; r = self.run_probe(); self.assertIn('ROOTFUL_DOCKER_REQUIRED', r['blockers'])
        self.commands.rootless = False; self.commands.raw['running'] = False
        r = self.run_probe(); self.assertIn('OPS_CONTAINER_NOT_HEALTHY', r['blockers'])

    def test_changed_state_during_metadata_queries_invalidates_plan(self):
        def mutate(tool, args):
            if tool == 'loginctl': self.h.replace_configs(lambda cfg: cfg.update(alertmanager_url='http://127.0.0.1:29991'))
            return False
        self.commands.fail = mutate; r = self.run_probe()
        self.assertIn('PREPARED_STATE_CHANGED_DURING_CHECK', r['blockers'])

    def test_error_text_is_not_reported(self):
        self.commands.fail = lambda tool, args: True
        r = self.run_probe(); self.assertNotIn('private-secret-not-for-output', json.dumps(r))


class CommandBoundaryTests(unittest.TestCase):
    def reader(self, env=None, commands=None):
        return probe.Reader(env=env or {}, runner=commands or Commands({}), which=lambda name, **kw: '/fake/' + name)

    def test_mutating_or_unbounded_commands_never_reach_subprocess(self):
        commands = Commands({}); r = self.reader(commands=commands)
        for tool, args in [('docker', ['restart', IDENT]), ('docker', ['--host', 'unix:///test', 'container', 'inspect', IDENT]),
                           ('docker', ['--host', 'unix:///test', 'exec', IDENT, 'env']), ('systemctl', ['--user', 'start', 'anything']),
                           ('loginctl', ['enable-linger']), ('systemctl', ['--user', 'show', '--all'])]:
            with self.subTest(args=args), self.assertRaises(ReadFailure): r.query(tool, args)
        self.assertEqual(commands.calls, [])

    def test_remote_endpoint_rejected_before_daemon_contact(self):
        c = Commands({}); c.context_endpoint = 'ssh://remote'; r = self.reader(commands=c)
        with self.assertRaisesRegex(ReadFailure, 'LOCAL_DOCKER_REQUIRED'): probe.docker(r, Path('/x'), None, None)
        self.assertEqual(len(c.calls), 2)

    def test_context_override_wins_and_pinned_commands_drop_ambient_credentials(self):
        c = Commands({}); r = self.reader({'DOCKER_HOST': 'ssh://wrong', 'DOCKER_CONTEXT': 'chosen', 'API_KEY': 'private',
                                          'HTTP_PROXY': 'http://remote', 'DOCKER_CUSTOM_HEADERS': 'private'}, c)
        probe.docker(r, Path('/x'), None, None)
        self.assertEqual(c.calls[0][1][2], 'chosen')
        for _, args, kw in c.calls:
            self.assertFalse(any(k in kw['env'] for k in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'API_KEY', 'HTTP_PROXY', 'DOCKER_CUSTOM_HEADERS')))
            self.assertEqual(kw['timeout'], 5)

    def test_explicit_unix_host_avoids_wrong_current_context(self):
        c = Commands({}); r = self.reader({'DOCKER_HOST': 'unix:///selected.sock'}, c)
        probe.docker(r, Path('/x'), None, None)
        self.assertTrue(all(args[:2] == ['--host', 'unix:///selected.sock'] for _, args, _ in c.calls))

    def test_timeout_and_oversize_metadata_are_bounded_errors(self):
        def timeout(*a, **kw): raise subprocess.TimeoutExpired('secret command text', 5)
        r = self.reader(commands=timeout)
        with self.assertRaisesRegex(ReadFailure, 'SYSTEMCTL_QUERY_TIMEOUT'): r.query('systemctl', ['--version'])
        r.runner = lambda *a, **kw: SimpleNamespace(returncode=0, stdout='x'*(128*1024+1))
        with self.assertRaisesRegex(ReadFailure, 'METADATA_RESPONSE_TOO_LARGE'): r.query('systemctl', ['--version'])

    def test_no_systemd_binary_does_not_try_other_supervisors(self):
        r = probe.Reader(env={}, which=lambda *a, **kw: None)
        self.assertEqual(probe.systemd(r, None)['code'], 'SYSTEMCTL_NOT_INSTALLED')
        self.assertEqual(r.calls, 0)

    def test_empty_systemd_version_is_structured_failure(self):
        r = self.reader(commands=lambda *a, **kw: SimpleNamespace(returncode=0, stdout=''))
        self.assertEqual(probe.systemd(r, None)['code'], 'INVALID_SYSTEMD_VERSION')

    def test_cli_stdlib_only_missing_state_and_unsupported_actions(self):
        with tempfile.TemporaryDirectory(prefix='lifecycle cli space%-') as temp:
            path = Path(temp) / 'server'; before = tree(Path(temp))
            env = dict(os.environ, PATH='/nonexistent')
            p = subprocess.run([sys.executable, '-B', '-S', a.__file__, 'plan', '--state-dir', str(path)], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stderr); self.assertIn('PREPARED_STATE_NOT_FOUND', json.loads(p.stdout)['blockers'])
            self.assertEqual(tree(Path(temp)), before)
            p = subprocess.run([sys.executable, '-B', '-S', a.__file__, 'start', '--state-dir', str(path)], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2); self.assertFalse(path.exists())


if __name__ == '__main__': unittest.main()
