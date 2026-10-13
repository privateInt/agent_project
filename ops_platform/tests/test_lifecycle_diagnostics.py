"""Permission-role diagnostics and bounded, same-user login metadata discovery."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops_platform.ops import lifecycle_probe as probe, lifecycle_state as state
from ops_platform.tests import test_auto_refresh as prior


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.h = prior.PreflightTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.f = self.h.f

    def test_external_readable_file_keeps_existing_contract_and_reports_permissions(self):
        p = Path(self.f.cfg['platforms']['agent']['token_file']); p.chmod(0o644)
        before = prior.tree(self.f.root)
        r = self.h.run_probe()
        self.assertEqual(r['result'], 'passed', r)
        self.assertEqual(r['next_step'], 'isolated_systemd_and_docker_acceptance')
        self.assertIn('EXTERNAL_REFERENCE_PRIVATE_MODE_DIFFERS', r['warnings'])
        item = next(x for x in r['installation']['configuration']['file_checks'] if x['role'] == 'agent_token')
        self.assertEqual(item['path'], str(p)); self.assertEqual(item['mode'], '0644')
        self.assertTrue(item['readable_by_current_user']); self.assertFalse(item['contents_read'])
        self.assertEqual(item['policy'], 'existing_external_reference'); self.assertEqual(prior.tree(self.f.root), before)

    def test_private_ops_key_stays_blocked_and_exact_path_is_reported(self):
        p = self.f.directory / 'access-key'; p.chmod(0o644)
        r = self.h.run_probe(); self.assertIn('PRIVATE_OWNER_MODE_MISMATCH', r['blockers'])
        item = next(x for x in r['installation']['configuration']['file_checks'] if x['role'] == 'ops_access_key')
        self.assertEqual(item['status'], 'error'); self.assertEqual(item['mode'], '0644'); self.assertEqual(item['path'], str(p))
        self.assertEqual(p.stat().st_mode & 0o777, 0o644)
        self.assertEqual(r['docker']['ops']['status'], 'ok')  # Independent metadata read, not readiness of the key.
        self.assertFalse(r['environment_ready'])
        self.assertEqual(r['next_step'], 'resolve_reported_preflight_blockers')

    def test_cache_private_failure_reports_directory_separately(self):
        p = self.f.directory / 'cache'; p.chmod(0o755)
        r = self.h.run_probe()
        item = next(x for x in r['installation']['configuration']['file_checks'] if x['role'] == 'ops_cache_directory')
        self.assertEqual(item['status'], 'error'); self.assertEqual(item['mode'], '0755')
        self.assertIn('PRIVATE_OWNER_MODE_MISMATCH', r['blockers'])

    def test_multiple_failures_preserve_each_role_without_secret_reads(self):
        p = Path(self.f.cfg['langfuse_file']); p.unlink()
        key = self.f.directory / 'access-key'; key.chmod(0o644)
        real_open = io.open
        def guard(path, *args, **kw):
            if isinstance(path, (str, Path)) and str(path) in (str(key), str(p)):
                raise AssertionError('credential content read')
            return real_open(path, *args, **kw)
        with patch('io.open', side_effect=guard): r = self.h.run_probe()
        self.assertIn('LOCAL_FILE_MISSING', r['blockers']); self.assertIn('PRIVATE_OWNER_MODE_MISMATCH', r['blockers'])
        roles = {x['role'] for x in r['installation']['configuration']['file_checks'] if x['status'] == 'error'}
        self.assertEqual(roles, {'ops_access_key', 'langfuse_credentials'})

    def test_metadata_policy_does_not_turn_unreadable_external_file_into_warning(self):
        original = os.access
        target = Path(self.f.cfg['langfuse_file'])
        with patch('os.access', side_effect=lambda p, *args, **kw: False if Path(p) == target else original(p, *args, **kw)):
            r = self.h.run_probe()
        self.assertIn('REFERENCE_NOT_READABLE', r['blockers'])

    def test_foreign_owned_readable_reference_is_explicitly_reported(self):
        original = state.fs.metadata; p = Path(self.f.cfg['langfuse_file'])
        def replace(path, **kw):
            info = original(path, **kw)
            if Path(path) == p:
                return SimpleNamespace(st_uid=os.getuid()+1, st_gid=os.getgid()+1, st_mode=stat.S_IFREG | 0o644, st_size=info.st_size)
            return info
        with patch.object(state.fs, 'metadata', side_effect=replace): r = self.h.run_probe()
        item = next(x for x in r['installation']['configuration']['file_checks'] if x['role'] == 'langfuse_credentials')
        self.assertEqual(item['status'], 'warning'); self.assertFalse(item['owner_matches'])

    def test_private_source_read_failure_names_file(self):
        p = self.f.directory / 'host-config.json'; p.chmod(0o644)
        r = self.h.run_probe(); self.assertIn('PRIVATE_STATE_REQUIRED', r['blockers'])
        self.assertEqual(r['installation']['configuration']['file_checks'][0]['path'], str(p))

    def test_changed_permissions_during_probe_invalidates_snapshot(self):
        def mutate(tool, args):
            if tool == 'loginctl': (self.f.directory / 'cache').chmod(0o755)
            return False
        self.h.commands.fail = mutate
        r = self.h.run_probe(); self.assertIn('PREPARED_STATE_CHANGED_DURING_CHECK', r['blockers'])

    def test_symlink_external_reference_still_blocked(self):
        p = Path(self.f.cfg['langfuse_file']); target = p.with_suffix('.other'); p.rename(target); p.symlink_to(target)
        r = self.h.run_probe(); self.assertIn('SYMLINK_REFUSED', r['blockers']); self.assertTrue(p.is_symlink())


class UserSessionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(prefix='ops-user-session-'); self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'runtime space%'; self.runtime.mkdir(mode=0o700)
        self.commands = prior.Commands({}); self.commands.runtime_path = str(self.runtime)
        self.failure_reason = 'Failed to connect to bus: No medium found; private-sentinel'
        self.fail_fallback = False; self.envs = []
        def runner(args, **kwargs):
            if Path(args[0]).name == 'systemctl' and args[-1] == '--property=' + probe.MANAGER_PROPERTIES:
                env = kwargs['env']; self.envs.append(dict(env))
                if env.get('XDG_RUNTIME_DIR') != str(self.runtime) or self.fail_fallback:
                    return SimpleNamespace(returncode=1, stdout='', stderr=self.failure_reason)
            return self.commands(args, **kwargs)
        self.reader = probe.Reader(env={}, runner=runner, which=lambda name, **kw: '/fake/' + name)

    def socket_metadata(self, uid=None, mode=stat.S_IFSOCK | 0o600):
        original = os.stat
        def inspect(path, *args, **kw):
            if str(path) == 'bus' and kw.get('dir_fd') is not None:
                return SimpleNamespace(st_mode=mode, st_uid=os.getuid() if uid is None else uid)
            return original(path, *args, **kw)
        return patch('os.stat', side_effect=inspect)

    def test_logind_survives_manager_failure_and_linger_is_not_invented(self):
        self.commands.linger = 'no'; self.failure_reason = 'Failed to connect to bus: Permission denied private-sentinel'
        self.fail_fallback = True
        with self.socket_metadata(): r = probe.systemd(self.reader, None)
        self.assertEqual(r['code'], 'SYSTEMD_USER_MANAGER_UNAVAILABLE'); self.assertFalse(r['linger'])
        self.assertEqual(r['login']['runtime_path'], str(self.runtime)); self.assertEqual(len(self.envs), 1)
        self.assertEqual(r['manager_query_error']['reason'], 'USER_BUS_PERMISSION_DENIED')
        self.assertNotIn('private-sentinel', json.dumps(r)); self.assertFalse(r['environment']['fallback_attempted'])

    def test_missing_environment_uses_verified_current_user_runtime_once(self):
        before = dict(os.environ); original = dict(self.reader.env)
        unit = 'agent-project-ops-refresh-' + 'a'*32 + '.service'
        with self.socket_metadata(): r = probe.systemd(self.reader, unit)
        self.assertEqual(r['status'], 'ok', r); self.assertEqual(len(self.envs), 1)
        self.assertEqual(r['environment']['source'], 'logind_runtime_current_user')
        self.assertEqual(r['environment']['selection'], 'verified_logind_runtime_before_query')
        self.assertEqual(r['unit']['status'], 'absent'); self.assertEqual(self.reader.env, original); self.assertEqual(dict(os.environ), before)
        self.assertIn('space%25', self.envs[0]['DBUS_SESSION_BUS_ADDRESS'])
        self.assertIn('%20', self.envs[0]['DBUS_SESSION_BUS_ADDRESS'])
        self.assertFalse(r['environment']['parent_environment_changed'])

    def test_explicit_wrong_environment_is_preserved(self):
        self.reader.original['XDG_RUNTIME_DIR'] = '/explicit/other'; self.reader.env['XDG_RUNTIME_DIR'] = '/explicit/other'
        with self.socket_metadata(): r = probe.systemd(self.reader, None)
        self.assertEqual(r['code'], 'SYSTEMD_USER_MANAGER_UNAVAILABLE'); self.assertEqual(len(self.envs), 1)
        self.assertEqual(r['environment']['fallback'], 'explicit_environment_preserved')

    def test_missing_or_wrong_socket_does_not_trigger_query_to_candidate(self):
        for uid, mode in ((os.getuid()+1, stat.S_IFSOCK | 0o600), (os.getuid(), stat.S_IFREG | 0o600)):
            self.envs.clear()
            with self.socket_metadata(uid, mode): r = probe.systemd(self.reader, None)
            self.assertEqual(r['environment']['code'], 'USER_BUS_SOCKET_OWNER_OR_TYPE_INVALID')
            self.assertEqual(len(self.envs), 0)

    def test_non_private_or_symlink_runtime_refused(self):
        self.runtime.chmod(0o755)
        with self.socket_metadata(): r = probe.systemd(self.reader, None)
        self.assertEqual(r['environment']['code'], 'PRIVATE_OWNER_MODE_MISMATCH'); self.assertEqual(len(self.envs), 0)
        self.runtime.chmod(0o700); other = self.runtime.with_name('other'); self.runtime.rename(other); self.runtime.symlink_to(other)
        self.envs.clear()
        with self.socket_metadata(): r = probe.systemd(self.reader, None)
        self.assertEqual(r['environment']['code'], 'SYMLINK_REFUSED'); self.assertEqual(len(self.envs), 0)

    def test_linger_is_retained_if_selected_endpoint_fails(self):
        self.fail_fallback = True
        with self.socket_metadata(): r = probe.systemd(self.reader, None)
        self.assertTrue(r['linger']); self.assertEqual(r['status'], 'unavailable')
        self.assertEqual(len(self.envs), 1); self.assertIn('manager_query_error', r)
        self.assertFalse(r['environment']['fallback_attempted'])

    def test_missing_environment_never_uses_error_text_to_choose_runtime(self):
        original = self.reader.runner
        def guard(args, **kw):
            if args[1:2] == ['--user']:
                self.assertEqual(kw['env'].get('XDG_RUNTIME_DIR'), str(self.runtime))
            return original(args, **kw)
        self.reader.runner = guard
        self.failure_reason = '$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined'
        with self.socket_metadata(): r = probe.systemd(self.reader, None)
        self.assertEqual(r['status'], 'ok'); self.assertEqual(len(self.envs), 1)
        self.assertNotIn('manager_query_error', r)

    def test_selected_runtime_failure_never_retries_another_environment(self):
        self.failure_reason = 'unrecognized backend failure private-sentinel'; self.fail_fallback = True
        with self.socket_metadata(): r = probe.systemd(self.reader, None)
        self.assertEqual(r['manager_query_error']['reason'], 'USER_BUS_QUERY_FAILED')
        self.assertEqual(len(self.envs), 1); self.assertFalse(r['environment']['fallback_attempted'])
        self.assertNotIn('private-sentinel', json.dumps(r))

    def test_explicit_bus_environment_is_preserved_without_discovery(self):
        bus = 'unix:path=/explicit/user/bus'
        self.reader.original['DBUS_SESSION_BUS_ADDRESS'] = bus; self.reader.env['DBUS_SESSION_BUS_ADDRESS'] = bus
        with self.socket_metadata(): r = probe.systemd(self.reader, None)
        self.assertEqual(r['environment']['selection'], 'explicit_environment_preserved')
        self.assertEqual(self.envs[0]['DBUS_SESSION_BUS_ADDRESS'], bus)
        self.assertNotIn('selected_runtime', r['environment'])

    def test_logind_failure_does_not_hide_working_manager(self):
        c = prior.Commands({}); c.fail = lambda tool, args: tool == 'loginctl'
        reader = probe.Reader(env={}, runner=c, which=lambda name, **kw: '/fake/' + name)
        r = probe.systemd(reader, None)
        self.assertEqual(r['status'], 'ok'); self.assertEqual(r['linger'], 'unknown')
        self.assertEqual(r['login']['code'], 'LOGINCTL_QUERY_FAILED')

    def test_foreign_logind_uid_cannot_select_user_bus(self):
        original = self.reader.runner
        def wrong(args, **kw):
            if Path(args[0]).name == 'loginctl':
                return SimpleNamespace(returncode=0, stdout='UID='+str(os.getuid()+1)+'\nLinger=yes\nRuntimePath='+str(self.runtime)+'\nState=active', stderr='')
            return original(args, **kw)
        self.reader.runner = wrong
        with self.socket_metadata(): r = probe.systemd(self.reader, None)
        self.assertEqual(r['login']['code'], 'INVALID_LOGIND_USER_RESPONSE'); self.assertEqual(len(self.envs), 1)

    def test_query_environment_override_rejects_non_user_systemctl_and_unverified_path(self):
        with self.assertRaisesRegex(Exception, 'USER_BUS_ENVIRONMENT_SCOPE_INVALID'):
            self.reader.query('loginctl', probe.login_args(), user_runtime=str(self.runtime))
        with self.assertRaises(OSError):
            self.reader.query('systemctl', ['--user', '--no-pager', '--no-ask-password', 'show', '--property='+probe.MANAGER_PROPERTIES], user_runtime=str(self.runtime))


if __name__ == '__main__': unittest.main()
