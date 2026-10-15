"""Mock classification and isolated public canaries; no operational App key."""
import ast
from contextlib import contextmanager, redirect_stderr
import errno
import inspect
import io
import itertools
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.ops.tools import github_app_key_access_probe as q


SOURCE = Path(__file__).resolve().parents[1] / 'github_app_key_access_probe.py'
WORKER, OWNER = 'probe_worker', 'probe_owner'
KEY_PATH = '/fixture-key-boundary/public-canary'
PRIVATE = 'PRIVATE_BOUNDARY_/private/trace?email=fixture@example.invalid'
UID, GID, OWNER_UID, OWNER_GID = 21001, 21002, 22001, 22002
FALSE_FLAGS = {'key_presence_verified', 'privilege_escalation_verified',
               'runtime_isolation_verified', 'trusted_gate_verified', 'native_ready', 'merge_authorized'}
FIELDS = {'scope', 'status', 'reason', 'worker_identity_verified', 'control_open_verified',
          'key_open_attempts', 'key_path_open_denied', 'key_bytes_read'} | FALSE_FLAGS
REASONS = {'ARGUMENTS_INVALID', 'IDENTITY_PINS_INVALID', 'EXECUTION_IDENTITY_MISMATCH',
           'CONTROL_OPEN_FAILED', 'KEY_PATH_OPEN_DENIED', 'KEY_PATH_OPEN_ALLOWED',
           'KEY_PATH_UNCONFIRMED', 'FD_CLOSE_FAILED', 'UNEXPECTED_FAILURE'}
CANARY_RESULTS = []


class BoundaryOS:
    def __init__(self):
        self.opens, self.closes, self.lookups = [], [], []
        self.forbidden_calls = []
        self.control_effect = 11
        self.key_effect = OSError(errno.EACCES, PRIVATE)
        self.close_errors = {}
        self.uids, self.gids = (UID,) * 3, (GID,) * 3
        self.accounts = {WORKER: SimpleNamespace(pw_uid=UID, pw_gid=GID),
                         OWNER: SimpleNamespace(pw_uid=OWNER_UID, pw_gid=OWNER_GID)}
        self.lookup_error = None
        self.proxy = SimpleNamespace(
            O_RDONLY=os.O_RDONLY, O_CLOEXEC=os.O_CLOEXEC,
            O_NONBLOCK=os.O_NONBLOCK, O_NOFOLLOW=os.O_NOFOLLOW,
            path=SimpleNamespace(normpath=os.path.normpath),
            getresuid=lambda: self.uids, getresgid=lambda: self.gids,
            open=self.open, close=self.close, read=self.forbidden,
            stat=self.forbidden, fstat=self.forbidden, lstat=self.forbidden,
        )
        self.pwd_proxy = SimpleNamespace(getpwnam=self.lookup)

    def lookup(self, name):
        self.lookups.append(name)
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.accounts[name]

    def forbidden(self, *args, **kwargs):
        self.forbidden_calls.append(True)
        raise AssertionError('key read/stat is forbidden')

    def open(self, path, flags):
        self.opens.append((path, flags))
        effect = self.control_effect if len(self.opens) == 1 else self.key_effect
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def close(self, fd):
        self.closes.append(fd)
        if fd in self.close_errors:
            raise self.close_errors[fd]


@contextmanager
def boundary(environment=None):
    environment = BoundaryOS() if environment is None else environment
    with patch.object(q, 'os', environment.proxy), patch.object(q, 'pwd', environment.pwd_proxy):
        yield environment


def argv():
    return ['--worker-user', WORKER, '--key-owner', OWNER, '--private-key', KEY_PATH]


class KeyAccessTests(unittest.TestCase):
    def report(self, report, observed, reason=None, attempts=None):
        self.assertEqual(set(report), FIELDS)
        self.assertEqual(report['scope'], 'WORKER_KEY_PATH_OPEN_ONLY')
        self.assertEqual(report['status'], 'OBSERVED' if observed else 'BLOCKED')
        self.assertIn(report['reason'], REASONS)
        if reason is not None:
            self.assertEqual(report['reason'], reason)
        for name in ('worker_identity_verified', 'control_open_verified', 'key_path_open_denied'):
            self.assertIs(type(report[name]), bool)
        self.assertIs(report['key_path_open_denied'], observed)
        self.assertIs(type(report['key_open_attempts']), int)
        self.assertIn(report['key_open_attempts'], (0, 1))
        self.assertIs(type(report['key_bytes_read']), int)
        self.assertEqual(report['key_bytes_read'], 0)
        if attempts is not None:
            self.assertEqual(report['key_open_attempts'], attempts)
        for name in FALSE_FLAGS:
            self.assertIs(report[name], False, name)
        if observed:
            self.assertEqual(report['reason'], 'KEY_PATH_OPEN_DENIED')
            self.assertIs(report['worker_identity_verified'], True)
            self.assertIs(report['control_open_verified'], True)
            self.assertEqual(report['key_open_attempts'], 1)
        rendered = json.dumps(report)
        for value in (WORKER, OWNER, KEY_PATH, PRIVATE, 'fixture@example.invalid',
                      str(UID), str(GID), str(OWNER_UID), str(OWNER_GID), 'Traceback'):
            self.assertNotIn(value, rendered)

    def test_01_exact_six_cli_tokens_order_help_and_direct_input_types(self):
        pairs = [argv()[index:index + 2] for index in range(0, 6, 2)]
        for order in itertools.permutations(pairs):
            with boundary() as env, redirect_stderr(io.StringIO()) as stderr:
                output = io.StringIO()
                self.assertEqual(q.main([value for pair in order for value in pair], output), 2)
                self.report(json.loads(output.getvalue()), True, attempts=1)
                self.assertEqual(stderr.getvalue(), '')
                self.assertEqual(len(env.opens), 2)
        invalid = [[], ['--help'], argv()[:-1], tuple(argv()),
                   ['--worker-user=' + WORKER, '--key-owner', OWNER, '--private-key', KEY_PATH],
                   ['--worker-user', WORKER, '--worker-user', WORKER, '--private-key', KEY_PATH],
                   ['--worker', WORKER, '--key-owner', OWNER, '--private-key', KEY_PATH],
                   ['--' + PRIVATE, WORKER, '--key-owner', OWNER, '--private-key', KEY_PATH],
                   ['--worker-user', 1, '--key-owner', OWNER, '--private-key', KEY_PATH]]
        for arguments in invalid:
            with self.subTest(kind=type(arguments).__name__), boundary() as env, redirect_stderr(io.StringIO()) as stderr:
                output = io.StringIO()
                self.assertEqual(q.main(arguments, output), 2)
                self.report(json.loads(output.getvalue()), False, 'ARGUMENTS_INVALID', 0)
                self.assertEqual(env.lookups + env.opens, [])
                self.assertEqual(stderr.getvalue(), '')
        for values in ((None, OWNER, KEY_PATH), (WORKER, True, KEY_PATH), (WORKER, OWNER, b'/canary')):
            with boundary() as env:
                self.report(q.probe(*values), False, 'ARGUMENTS_INVALID', 0)
                self.assertEqual(env.lookups + env.opens, [])

    def test_02_username_canonical_path_and_utf8_boundaries_without_echo(self):
        for path in ('/' + 'x' * 4094, '/' + '가' * 1364 + 'xx'):
            self.assertEqual(len(path.encode('utf-8')), 4095)
            with boundary() as env:
                self.report(q.probe(WORKER, OWNER, path), True, attempts=1)
                self.assertEqual(env.opens[-1][0], path)
        invalid_paths = ('', 'relative/key', '//canary', '/a/../b', '/a//b', '/a/.', '/a/',
                         '/a\x00b', '/a\nb', '/a\x7fb', '/a\ud800b', '/' + 'x' * 4095,
                         '/' + '가' * 1364 + 'xxx')
        for path in invalid_paths:
            with boundary() as env:
                self.report(q.probe(WORKER, OWNER, path), False, 'ARGUMENTS_INVALID', 0)
                self.assertEqual(env.lookups + env.opens, [])
        for name in ('Root', 'bad/name', 'name with space', 'a' * 33, '', PRIVATE):
            for position in (0, 1):
                values = [WORKER, OWNER, KEY_PATH]
                values[position] = name
                with boundary() as env:
                    self.report(q.probe(*values), False, 'ARGUMENTS_INVALID', 0)
                    self.assertEqual(env.lookups + env.opens, [])

    def test_03_bad_account_pins_never_open_control_or_key(self):
        for account in (WORKER, OWNER):
            for field in ('pw_uid', 'pw_gid'):
                for value in (True, 0, -1, 1.0, '1'):
                    env = BoundaryOS()
                    setattr(env.accounts[account], field, value)
                    with boundary(env):
                        self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'IDENTITY_PINS_INVALID', 0)
                        self.assertEqual(env.opens, [])
        env = BoundaryOS()
        env.accounts[OWNER].pw_uid = UID
        with boundary(env):
            self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'IDENTITY_PINS_INVALID', 0)
            self.assertEqual(env.opens, [])
        env = BoundaryOS()
        env.lookup_error = KeyError(PRIVATE)
        with boundary(env):
            self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'IDENTITY_PINS_INVALID', 0)
            self.assertEqual(env.opens, [])
        env = BoundaryOS()
        env.accounts[OWNER].pw_gid = GID
        with boundary(env):
            self.report(q.probe(WORKER, OWNER, KEY_PATH), True, attempts=1)

    def test_04_saved_root_identity_mismatch_never_reaches_any_open(self):
        # All account/control/key fixtures are otherwise valid; saved root alone must block.
        env = BoundaryOS()
        env.uids = (UID, UID, 0)
        with boundary(env):
            report = q.probe(WORKER, OWNER, KEY_PATH)
            self.assertEqual(env.opens, [])
            self.report(report, False, 'EXECUTION_IDENTITY_MISMATCH', 0)
        for kind in ('uids', 'gids'):
            expected = UID if kind == 'uids' else GID
            for position in range(3):
                env = BoundaryOS()
                values = [expected] * 3
                values[position] = expected + 1
                setattr(env, kind, tuple(values))
                with boundary(env):
                    self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'EXECUTION_IDENTITY_MISMATCH', 0)
                    self.assertEqual(env.opens, [])
        for value in ([UID] * 3, (UID, UID), (True, UID, UID)):
            env = BoundaryOS()
            env.uids = value
            with boundary(env):
                self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'EXECUTION_IDENTITY_MISMATCH', 0)
                self.assertEqual(env.opens, [])
        env = BoundaryOS()
        env.proxy.getresuid = None
        with boundary(env):
            self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'UNEXPECTED_FAILURE', 0)
            self.assertEqual(env.opens, [])

    def test_05_control_open_and_close_are_required_before_private_attempt(self):
        for failure in (OSError(errno.EACCES, PRIVATE), OSError(errno.ENOENT, PRIVATE), RuntimeError(PRIVATE)):
            env = BoundaryOS()
            env.control_effect = failure
            with boundary(env):
                report = q.probe(WORKER, OWNER, KEY_PATH)
                self.report(report, False, 'CONTROL_OPEN_FAILED', 0)
                self.assertIs(report['worker_identity_verified'], True)
                self.assertIs(report['control_open_verified'], False)
                self.assertEqual(len(env.opens), 1)
                self.assertEqual(env.closes, [])
        env = BoundaryOS()
        env.close_errors[11] = OSError(errno.EACCES, PRIVATE)
        with boundary(env):
            report = q.probe(WORKER, OWNER, KEY_PATH)
            self.report(report, False, 'FD_CLOSE_FAILED', 0)
            self.assertIs(report['control_open_verified'], False)
            self.assertEqual(len(env.opens), 1)
            self.assertEqual(env.closes, [11])

    def test_06_only_key_open_eacces_is_observed_with_exact_flags_and_no_reads(self):
        with boundary() as env:
            report = q.probe(WORKER, OWNER, KEY_PATH)
            self.report(report, True, 'KEY_PATH_OPEN_DENIED', 1)
        control_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        self.assertEqual(env.opens, [('/dev/null', control_flags),
                                     (KEY_PATH, control_flags | os.O_NOFOLLOW)])
        self.assertEqual(env.closes, [11])
        self.assertEqual(env.forbidden_calls, [])

    def test_07_enoent_and_other_failures_are_not_key_denial_success(self):
        # ENOENT first and outside subTest: the M1 target must be an assertion FAIL.
        env = BoundaryOS()
        env.key_effect = OSError(errno.ENOENT, PRIVATE)
        with boundary(env):
            self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'KEY_PATH_UNCONFIRMED', 1)
        for number in (errno.EPERM, errno.ELOOP, errno.EIO, None):
            env = BoundaryOS()
            env.key_effect = OSError(number, PRIVATE)
            with boundary(env):
                self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'KEY_PATH_UNCONFIRMED', 1)
                self.assertEqual(len(env.opens), 2)
        env = BoundaryOS()
        env.key_effect = RuntimeError(PRIVATE)
        with boundary(env):
            self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'UNEXPECTED_FAILURE', 1)

    def test_08_allowed_fd_zero_and_key_close_eacces_remain_blocked(self):
        for fd in (0, 31):
            env = BoundaryOS()
            env.key_effect = fd
            with boundary(env):
                self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'KEY_PATH_OPEN_ALLOWED', 1)
                self.assertEqual(env.closes, [11, fd])
                self.assertEqual(env.forbidden_calls, [])
        for failure in (OSError(errno.EACCES, PRIVATE), OSError(errno.EBADF, PRIVATE), RuntimeError(PRIVATE)):
            env = BoundaryOS()
            env.key_effect = 31
            env.close_errors[31] = failure
            with boundary(env):
                self.report(q.probe(WORKER, OWNER, KEY_PATH), False, 'FD_CLOSE_FAILED', 1)
                self.assertEqual(env.closes, [11, 31])
                self.assertEqual(env.forbidden_calls, [])

    def test_09_report_flags_counts_and_repeated_calls_do_not_share_state(self):
        with boundary() as env:
            first = q.probe(WORKER, OWNER, KEY_PATH)
            self.report(first, True, attempts=1)
        with boundary():
            second = q.probe(WORKER, OWNER, KEY_PATH)
            self.report(second, True, attempts=1)
        self.assertEqual(first, second)
        first['trusted_gate_verified'] = True
        self.assertIs(second['trusted_gate_verified'], False)
        self.assertEqual(tuple(inspect.signature(q.probe).parameters), ('worker_user', 'key_owner', 'key_path'))

    def test_10_private_exceptions_cli_and_broken_output_never_echo(self):
        class WriteFailure:
            def write(self, value):
                raise OSError(PRIVATE)
            def flush(self):
                raise ValueError(PRIVATE)
        class FlushFailure(io.StringIO):
            def flush(self):
                raise OSError(PRIVATE)
        for output in (WriteFailure(), FlushFailure()):
            with boundary(), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(q.main(argv(), output), 2)
            self.assertEqual(stderr.getvalue(), '')
        with patch.object(q, 'probe', side_effect=RuntimeError(PRIVATE)), redirect_stderr(io.StringIO()) as stderr:
            output = io.StringIO()
            self.assertEqual(q.main(argv(), output), 2)
        self.report(json.loads(output.getvalue()), False, 'UNEXPECTED_FAILURE', 0)
        self.assertEqual(stderr.getvalue(), '')
        process = subprocess.Popen([sys.executable, '-I', '-B', str(SOURCE), '--help'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        stderr = process.stderr.read()
        process.stderr.close()
        self.assertEqual(process.wait(timeout=3), 2)
        self.assertEqual(stderr, b'')

    def run_canary(self, allow):
        label = 'allowed_public_canary' if allow else 'denied_public_canary'
        if os.geteuid() != 0 or not all(hasattr(os, name) for name in ('setgroups', 'setresuid', 'setresgid')):
            CANARY_RESULTS.append({'case': label, 'status': 'NOT_RUN', 'reason': 'missing_local_uid_capability'})
            self.skipTest('cross_uid_prerequisites_unavailable')
        accounts = []
        for name in ('nobody', 'daemon', 'www-data', 'mail'):
            try:
                account = pwd.getpwnam(name)
            except KeyError:
                continue
            if account.pw_uid > 0 and account.pw_gid > 0 and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', name):
                if all(account.pw_uid != prior.pw_uid for prior in accounts):
                    accounts.append(account)
        if len(accounts) < 2:
            CANARY_RESULTS.append({'case': label, 'status': 'NOT_RUN', 'reason': 'missing_existing_nonroot_accounts'})
            self.skipTest('cross_uid_accounts_unavailable')
        worker, owner = accounts[:2]
        with tempfile.TemporaryDirectory(prefix='key-boundary-public-canary-') as temporary:
            folder = Path(temporary)
            folder.chmod(0o755)
            helper = folder / 'probe.py'
            shutil.copyfile(SOURCE, helper)
            helper.chmod(0o444)
            protected = folder / 'owner-area'
            protected.mkdir(mode=0o700)
            canary = protected / 'canary'
            canary.write_bytes(b'PUBLIC CANARY - NOT AN APP KEY')
            try:
                try:
                    os.chown(protected, owner.pw_uid, owner.pw_gid)
                    os.chown(canary, owner.pw_uid, owner.pw_gid)
                    protected.chmod(0o755 if allow else 0o700)
                    canary.chmod(0o444 if allow else 0o600)
                except OSError as error:
                    if error.errno not in (errno.EPERM, errno.EACCES, errno.EINVAL, errno.ENOSYS):
                        raise
                    CANARY_RESULTS.append({'case': label, 'status': 'NOT_RUN',
                                           'reason': 'temporary_chown_unavailable',
                                           'errno_name': errno.errorcode.get(error.errno, 'UNKNOWN'),
                                           'root_fallback': False})
                    self.skipTest('temporary_chown_unavailable')
                def drop_identity():
                    os.setgroups([])
                    os.setresgid(worker.pw_gid, worker.pw_gid, worker.pw_gid)
                    os.setresuid(worker.pw_uid, worker.pw_uid, worker.pw_uid)
                    if os.getresuid() != (worker.pw_uid,) * 3 or os.getresgid() != (worker.pw_gid,) * 3:
                        raise RuntimeError('fixture identity transition failed')
                command = [sys.executable, '-I', '-B', str(helper), '--worker-user', worker.pw_name,
                           '--key-owner', owner.pw_name, '--private-key', str(canary)]
                try:
                    result = subprocess.run(command, preexec_fn=drop_identity, capture_output=True,
                                            timeout=5, env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
                except subprocess.TimeoutExpired:
                    raise
                except (subprocess.SubprocessError, PermissionError):
                    CANARY_RESULTS.append({'case': label, 'status': 'NOT_RUN', 'reason': 'child_identity_or_exec_unavailable', 'root_fallback': False})
                    self.skipTest('cross_uid_child_unavailable_no_root_fallback')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, b'')
                report = json.loads(result.stdout)
                self.report(report, not allow, 'KEY_PATH_OPEN_ALLOWED' if allow else 'KEY_PATH_OPEN_DENIED', 1)
                self.assertIs(report['worker_identity_verified'], True)
                self.assertIs(report['control_open_verified'], True)
                CANARY_RESULTS.append({'case': label, 'status': 'PASS', 'real_other_uid': True,
                                       'saved_ids_match': True, 'operational_key_used': False,
                                       'key_bytes_read': 0, 'root_fallback': False})
            finally:
                if protected.exists():
                    os.chown(protected, os.getuid(), os.getgid())
                    protected.chmod(0o700)
        self.assertFalse(folder.exists())

    def test_11_real_nonroot_private_parent_denies_public_canary(self):
        self.run_canary(False)

    def test_12_real_nonroot_readable_public_canary_is_blocked_as_allowed(self):
        self.run_canary(True)

    def test_13_standalone_cli_python310_stdlib_and_no_key_read_stat_or_external_calls(self):
        source = SOURCE.read_text()
        tree = ast.parse(source, feature_version=(3, 10))
        ast.parse(Path(__file__).read_text(), feature_version=(3, 10))
        imports = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module for node in tree.body if isinstance(node, ast.ImportFrom))
        self.assertTrue(all(name.split('.')[0] in sys.stdlib_module_names for name in imports))
        self.assertFalse(imports & {'subprocess', 'socket', 'http', 'urllib', 'ssl'})
        forbidden = {'read', 'read_bytes', 'read_text', 'stat', 'fstat', 'lstat', 'system', 'popen', 'execv', 'spawnv'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, forbidden)
                elif isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ('open', 'eval', 'exec', 'compile'))
        result = subprocess.run([sys.executable, '-I', '-B', str(SOURCE), '--help'], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, b'')
        self.report(json.loads(result.stdout), False, 'ARGUMENTS_INVALID', 0)


if __name__ == '__main__':
    unittest.main()
