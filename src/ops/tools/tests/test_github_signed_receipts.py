"""15개 좁은 검증 주제. 키는 임시 Ed25519 fixture만 사용한다."""
import ast
import copy
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import hashlib
import inspect
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

from src.ops.tools import github_signed_receipts as q


ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / 'src/ops/tools/github_signed_receipts.py'
DOMAIN = b'agent-project:signed-receipt:v1\n'
SPKI = bytes.fromhex('302a300506032b6570032100')
RFC_PUBLIC = bytes.fromhex('3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c')
RFC_MESSAGE = bytes.fromhex('72')
RFC_SIGNATURE = bytes.fromhex(
    '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da'
    '085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00')
ENV = {'LC_ALL': 'C', 'PATH': '/usr/bin:/bin', 'OPENSSL_CONF': '/dev/null'}
PRIVATE = 'PRIVATE_RECEIPT_/private/path?email=fixture@example.invalid'
POSITIVE = {'signature_pair_verified', 'artifact_hashes_verified', 'binding_fields_satisfied'}
FALSE = {'input_origin_verified', 'signer_roster_origin_verified', 'issuer_environment_verified',
         'runtime_isolation_verified', 'independent_review_verified', 'trusted_gate_verified',
         'binding_verified', 'native_ready', 'merge_authorized'}
FIELDS = {'scope', 'status', 'reason'} | POSITIVE | FALSE
REASONS = {'CRYPTOGRAPHIC_MATCH', 'EXPECTED_BINDING_INVALID', 'SIGNER_PINS_INVALID',
           'RECEIPTS_INVALID', 'BINDING_MISMATCH', 'ARTIFACTS_INVALID',
           'ARTIFACT_HASH_MISMATCH', 'SIGNATURE_NOT_VERIFIED', 'UNEXPECTED_FAILURE'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def message(payload):
    return DOMAIN + json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


class Proxy:
    def __init__(self, original, **overrides):
        self.original, self.overrides = original, overrides
    def __getattr__(self, name):
        return self.overrides[name] if name in self.overrides else getattr(self.original, name)


class SignedReceiptTests(unittest.TestCase):
    @classmethod
    def fixture_command(cls, arguments):
        result = subprocess.run(['/usr/bin/openssl', *arguments], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=dict(ENV), timeout=5, check=False)
        if result.returncode != 0:
            raise RuntimeError('temporary Ed25519 fixture prerequisite unavailable')

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='signed-receipts-fixture-')
        cls.folder = Path(cls.temporary.name)
        cls.keys, cls.raw, cls.serial = {}, {}, 0
        try:
            for role in ('tests', 'reviewer'):
                private = cls.folder / (role + '-canary-key.pem')
                public = cls.folder / (role + '-public.der')
                cls.fixture_command(['genpkey', '-algorithm', 'ED25519', '-out', str(private)])
                private.chmod(0o600)
                cls.fixture_command(['pkey', '-in', str(private), '-pubout', '-outform', 'DER', '-out', str(public)])
                encoded = public.read_bytes()
                if len(encoded) != 44 or encoded[:12] != SPKI:
                    raise RuntimeError('temporary public key encoding invalid')
                cls.keys[role], cls.raw[role] = private, encoded[12:]
            cls.signers = {
                'author': {'actor': 'author-fixture', 'key_sha256': sha(RFC_PUBLIC)},
                'tests': {'actor': 'tests-fixture', 'public_key_hex': cls.raw['tests'].hex()},
                'reviewer': {'actor': 'reviewer-fixture', 'public_key_hex': cls.raw['reviewer'].hex()},
            }
            cls.base_case = cls.make_case()
        except Exception:
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def sign(cls, payload, actual_role, raw_message=None):
        cls.serial += 1
        source, signature = cls.folder / ('message-' + str(cls.serial)), cls.folder / ('signature-' + str(cls.serial))
        source.write_bytes(message(payload) if raw_message is None else raw_message)
        try:
            cls.fixture_command(['pkeyutl', '-sign', '-rawin', '-inkey', str(cls.keys[actual_role]),
                                 '-in', str(source), '-out', str(signature)])
            result = signature.read_bytes()
            if len(result) != 64:
                raise RuntimeError('temporary signature length invalid')
            return result.hex()
        finally:
            source.unlink(missing_ok=True)
            signature.unlink(missing_ok=True)

    @classmethod
    def make_case(cls, artifacts=None, binding=None, signers=None, actual_roles=None):
        artifacts = {'tests': b'fixture-tests', 'reviewer': b'fixture-reviewer'} if artifacts is None else artifacts
        binding = {'repository': 'FixtureOwner/demo', 'pr_number': 31, 'base_sha': 'a' * 40,
                   'head_sha': 'b' * 40, 'release_sha256': 'c' * 64, 'plan_sha256': 'd' * 64,
                   'evidence_sha256': sha(artifacts['tests'])} if binding is None else binding
        signers = copy.deepcopy(cls.signers if signers is None else signers)
        receipts = {}
        for role in ('tests', 'reviewer'):
            payload = {'schema': 1, 'role': role, 'actor': signers[role]['actor'],
                       'key_sha256': sha(bytes.fromhex(signers[role]['public_key_hex'])),
                       'binding': copy.deepcopy(binding), 'artifact_sha256': sha(artifacts[role])}
            receipts[role] = {'payload': payload, 'signature_hex': cls.sign(payload, (actual_roles or {}).get(role, role))}
        return copy.deepcopy(binding), signers, receipts, dict(artifacts)

    def case(self):
        return copy.deepcopy(self.base_case)

    def result(self, result, reason):
        matched = reason == 'CRYPTOGRAPHIC_MATCH'
        self.assertEqual(set(result), FIELDS)
        self.assertEqual(result['scope'], 'RECEIPT_PAIR_VERIFICATION_ONLY')
        self.assertEqual(result['status'], 'CRYPTOGRAPHIC_MATCH' if matched else 'BLOCKED')
        self.assertEqual(result['reason'], reason)
        self.assertIn(result['reason'], REASONS)
        for key in POSITIVE | FALSE:
            self.assertIs(type(result[key]), bool)
        for key in POSITIVE:
            self.assertIs(result[key], matched)
        for key in FALSE:
            self.assertIs(result[key], False)
        rendered = json.dumps(result)
        for private in (PRIVATE, 'FixtureOwner/demo', 'tests-fixture', 'reviewer-fixture',
                        self.raw['tests'].hex(), self.raw['reviewer'].hex(), 'a' * 40,
                        'fixture-tests', 'fixture-reviewer', 'Traceback'):
            self.assertNotIn(private, rendered)

    def call(self, case, reason):
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            value = q.verify_receipt_pair(*case)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')
        self.result(value, reason)
        return value

    def no_crypto(self, case, reason):
        with patch.object(q, '_verify_ed25519', return_value=True) as backend:
            self.call(case, reason)
            backend.assert_not_called()

    def test_01_real_pair_all_or_nothing_flags_and_inputs_unchanged(self):
        case = self.case()
        before = copy.deepcopy(case)
        with patch.object(q, '_verify_ed25519', wraps=q._verify_ed25519) as backend:
            self.call(case, 'CRYPTOGRAPHIC_MATCH')
            self.assertEqual(backend.call_count, 2)
        self.assertEqual(case, before)

    def test_02_canonical_whole_payload_order_domain_and_partial_signatures(self):
        binding, signers, receipts, artifacts = self.case()
        payload = receipts['tests']['payload']
        expected = ('{"actor":"tests-fixture","artifact_sha256":"' + sha(artifacts['tests'])
                    + '","binding":{"base_sha":"' + 'a' * 40 + '","evidence_sha256":"' + sha(artifacts['tests'])
                    + '","head_sha":"' + 'b' * 40 + '","plan_sha256":"' + 'd' * 64
                    + '","pr_number":31,"release_sha256":"' + 'c' * 64
                    + '","repository":"FixtureOwner/demo"},"key_sha256":"' + sha(self.raw['tests'])
                    + '","role":"tests","schema":1}').encode('ascii')
        self.assertEqual(message(payload), DOMAIN + expected)
        receipts['tests']['payload'] = dict(reversed(list(payload.items())))
        receipts['tests']['payload']['binding'] = dict(reversed(list(binding.items())))
        self.call((binding, signers, receipts, artifacts), 'CRYPTOGRAPHIC_MATCH')
        for raw in (b'wrong-domain\n' + expected, DOMAIN + b'{"role":"tests"}'):
            case = self.case()
            case[2]['tests']['signature_hex'] = self.sign(case[2]['tests']['payload'], 'tests', raw)
            self.call(case, 'SIGNATURE_NOT_VERIFIED')

    def test_03_expected_binding_invalid_including_none_never_starts_crypto(self):
        for invalid in (None, {}, dict(self.base_case[0], extra='x'), dict(self.base_case[0], pr_number=True),
                        dict(self.base_case[0], repository='../demo'), dict(self.base_case[0], base_sha='A' * 40),
                        dict(self.base_case[0], plan_sha256='x' * 64)):
            case = list(self.case());case[0] = invalid
            case[2]['tests']['payload']['binding'] = None
            self.no_crypto(case, 'EXPECTED_BINDING_INVALID')

    def test_04_signer_shape_actor_hex_author_and_cross_role_distinctness(self):
        variations = []
        for role in ('tests', 'reviewer'):
            for field, value in (('actor', ''), ('actor', 'bad/name'), ('actor', 'x' * 129),
                                 ('public_key_hex', 'A' * 64), ('public_key_hex', 'a' * 62)):
                row = copy.deepcopy(self.signers);row[role][field] = value;variations.append(row)
        for role in ('tests', 'reviewer'):
            row = copy.deepcopy(self.signers);row[role]['actor'] = row['author']['actor'];variations.append(row)
            row = copy.deepcopy(self.signers);row['author']['key_sha256'] = sha(self.raw[role]);variations.append(row)
        row = copy.deepcopy(self.signers);row['reviewer']['actor'] = row['tests']['actor'];variations.append(row)
        row = copy.deepcopy(self.signers);row['reviewer']['public_key_hex'] = row['tests']['public_key_hex'];variations.append(row)
        variations.extend(({}, dict(self.signers, extra={}), dict(self.signers, author={'actor': 'author-fixture', 'key_sha256': 'A' * 64})))
        for signers in variations:
            case = list(self.case());case[1] = signers
            self.no_crypto(case, 'SIGNER_PINS_INVALID')

    def test_05_both_receipt_shapes_validated_before_either_crypto(self):
        for role in ('tests', 'reviewer'):
            for field, value in (('schema', True), ('role', 'author'), ('actor', 'author-fixture'),
                                 ('key_sha256', '0' * 64), ('artifact_sha256', 'A' * 64), ('binding', None)):
                case = self.case();case[2][role]['payload'][field] = value
                self.no_crypto(case, 'RECEIPTS_INVALID')
            for signature in ('', 'a' * 126, 'A' * 128, b'a' * 128):
                case = self.case();case[2][role]['signature_hex'] = signature
                self.no_crypto(case, 'RECEIPTS_INVALID')
            case = self.case();case[2][role]['payload']['algorithm'] = 'Ed25519'
            self.no_crypto(case, 'RECEIPTS_INVALID')
            case = self.case();case[2][role]['public_key_hex'] = self.raw[role].hex()
            self.no_crypto(case, 'RECEIPTS_INVALID')
        for receipt in ({}, dict(self.base_case[2], author={})):
            case = list(self.case());case[2] = receipt
            self.no_crypto(case, 'RECEIPTS_INVALID')

    def test_06_tampered_full_length_signature_is_rejected_by_real_backend(self):
        # M1 target: only one valid-format signature byte changes.
        case = self.case()
        signature = bytearray.fromhex(case[2]['tests']['signature_hex']);signature[0] ^= 1
        case[2]['tests']['signature_hex'] = bytes(signature).hex()
        with patch.object(q, '_verify_ed25519', wraps=q._verify_ed25519) as backend:
            self.call(case, 'SIGNATURE_NOT_VERIFIED')
            self.assertEqual(backend.call_count, 1)

    def test_07_other_role_signing_key_and_author_receipt_are_rejected(self):
        for role in ('tests', 'reviewer'):
            case = self.case()
            case[2][role]['signature_hex'] = self.sign(case[2][role]['payload'], 'reviewer' if role == 'tests' else 'tests')
            self.call(case, 'SIGNATURE_NOT_VERIFIED')
        case = self.case();case[2]['tests']['payload']['role'] = 'author'
        self.no_crypto(case, 'RECEIPTS_INVALID')

    def test_08_fully_resigned_stale_seven_field_bindings_are_rejected(self):
        changes = {'repository': 'FixtureOwner/other', 'pr_number': 32, 'base_sha': 'e' * 40,
                   'head_sha': 'f' * 40, 'release_sha256': 'e' * 64,
                   'plan_sha256': 'f' * 64, 'evidence_sha256': '0' * 64}
        for field, value in changes.items():
            case = self.case()
            for role in ('tests', 'reviewer'):
                case[2][role]['payload']['binding'][field] = value
                case[2][role]['signature_hex'] = self.sign(case[2][role]['payload'], role)
            self.no_crypto(case, 'BINDING_MISMATCH')

    def test_09_reviewer_actual_artifact_tamper_is_rejected_with_valid_signatures(self):
        # M2 target: tests bytes/evidence binding stay normal; only reviewer bytes change.
        case = self.case();case[3]['reviewer'] = b'X' + case[3]['reviewer'][1:]
        with patch.object(q, '_verify_ed25519', wraps=q._verify_ed25519) as backend:
            self.call(case, 'ARTIFACT_HASH_MISMATCH')
            backend.assert_not_called()
        case = self.case();case[3]['tests'] = b'X' + case[3]['tests'][1:]
        self.no_crypto(case, 'ARTIFACT_HASH_MISMATCH')
        case = self.case();case[2]['reviewer']['payload']['artifact_sha256'] = '0' * 64
        self.no_crypto(case, 'ARTIFACT_HASH_MISMATCH')

    def test_10_artifact_exact_bytes_roles_and_minimum_maximum_bounds(self):
        class BytesSubclass(bytes):
            pass
        for role in ('tests', 'reviewer'):
            for value in (b'', b'x' * (1024 * 1024 + 1), bytearray(b'x'), memoryview(b'x'), BytesSubclass(b'x'), PRIVATE):
                case = self.case();case[3][role] = value
                self.no_crypto(case, 'ARTIFACTS_INVALID')
        for values in ({}, {'tests': b'x'}, dict(self.base_case[3], author=b'x')):
            case = list(self.case());case[3] = values
            self.no_crypto(case, 'ARTIFACTS_INVALID')
        for artifacts in ({'tests': b'\x00', 'reviewer': b'\xff'},
                          {'tests': b'a' * (1024 * 1024), 'reviewer': b'b' * (1024 * 1024)}):
            self.call(self.make_case(artifacts=artifacts), 'CRYPTOGRAPHIC_MATCH')

    def test_11_rfc8032_vector_real_verify_and_fixed_public_only_subprocess(self):
        captured = []
        def run(command, **kwargs):
            fds = tuple(kwargs['pass_fds'])
            self.assertEqual(len(fds), 3);self.assertEqual(len(set(fds)), 3)
            self.assertTrue(all(type(fd) is int and fd >= 3 for fd in fds))
            self.assertEqual(command, ['/usr/bin/openssl', 'pkeyutl', '-verify', '-pubin', '-keyform', 'DER',
                                       '-rawin', '-inkey', '/proc/self/fd/' + str(fds[0]),
                                       '-in', '/proc/self/fd/' + str(fds[1]), '-sigfile', '/proc/self/fd/' + str(fds[2])])
            self.assertEqual(kwargs, {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
                                      'stderr': subprocess.DEVNULL, 'pass_fds': fds, 'close_fds': True,
                                      'shell': False, 'check': False, 'timeout': 5, 'env': ENV})
            self.assertEqual([os.pread(fd, 4096, 0) for fd in fds], [SPKI + RFC_PUBLIC, RFC_MESSAGE, RFC_SIGNATURE])
            self.assertEqual([os.lseek(fd, 0, os.SEEK_CUR) for fd in fds], [0, 0, 0])
            self.assertTrue(all(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC for fd in fds))
            captured.extend(fds)
            return subprocess.run(command, **kwargs)
        with patch.object(q, 'subprocess', Proxy(subprocess, run=run)):
            self.assertIs(q._verify_ed25519(RFC_PUBLIC, RFC_MESSAGE, RFC_SIGNATURE), True)
        for fd in captured:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_12_backend_failures_partial_writes_and_cleanup_are_fail_closed(self):
        for effect in (SimpleNamespace(returncode=1), FileNotFoundError(PRIVATE),
                       subprocess.TimeoutExpired('fixed-openssl', 5), subprocess.SubprocessError(PRIVATE)):
            def run(*args, **kwargs):
                if isinstance(effect, BaseException):
                    raise effect
                return effect
            created = []
            def memfd(*args):
                fd = os.memfd_create(*args);created.append(fd);return fd
            with patch.object(q, 'os', Proxy(os, memfd_create=memfd)), patch.object(q, 'subprocess', Proxy(subprocess, run=run)):
                self.call(self.case(), 'SIGNATURE_NOT_VERIFIED')
            for fd in created:
                with self.assertRaises(OSError):os.fstat(fd)
        for fault in ('create', 'write0', 'seek', 'close'):
            created, closed = [], []
            def memfd(*args):
                if fault == 'create' and created:raise OSError(PRIVATE)
                fd = os.memfd_create(*args);created.append(fd);return fd
            def close(fd):
                closed.append(fd);os.close(fd)
                if fault == 'close' and len(closed) == 1:raise RuntimeError(PRIVATE)
            def write(fd, data):
                return 0 if fault == 'write0' else os.write(fd, data)
            def seek(*args):
                if fault == 'seek':raise OSError(PRIVATE)
                return os.lseek(*args)
            with patch.object(q, 'os', Proxy(os, memfd_create=memfd, close=close, write=write, lseek=seek)):
                self.call(self.case(), 'SIGNATURE_NOT_VERIFIED')
            self.assertEqual(closed, created)
            for fd in created:
                with self.assertRaises(OSError):os.fstat(fd)
        writes = []
        def partial(fd, data):
            writes.append(len(data));return os.write(fd, data[:max(1, len(data) // 2)])
        with patch.object(q, 'os', Proxy(os, write=partial)):
            self.assertIs(q._verify_ed25519(RFC_PUBLIC, RFC_MESSAGE, RFC_SIGNATURE), True)
        self.assertGreater(len(writes), 3)
        # Virtual descriptors: simulate closing lowFD before raising, never touch parent stdin.
        closed, virtual_open = [], {0, 17}
        def virtual_close(fd):
            closed.append(fd);virtual_open.remove(fd)
            if fd == 0:raise OSError(PRIVATE)
        with patch.object(q, 'os', Proxy(os, memfd_create=lambda *args: 0, close=virtual_close)), patch.object(
                q, 'fcntl', Proxy(fcntl, fcntl=lambda *args: 17)), patch.object(q, 'subprocess', Proxy(subprocess, run=lambda *a, **k: self.fail('must stop before subprocess'))):
            self.assertIs(q._verify_ed25519(RFC_PUBLIC, RFC_MESSAGE, RFC_SIGNATURE), False)
        self.assertEqual(closed, [0, 17]);self.assertEqual(virtual_open, set())
        with patch.object(q, '_verify_ed25519', side_effect=[True, False]) as backend:
            self.call(self.case(), 'SIGNATURE_NOT_VERIFIED');self.assertEqual(backend.call_count, 2)

    def test_13_real_closed_standard_fds_reserve_public_memfds_and_verify(self):
        reader, writer = os.pipe()
        channel = fcntl.fcntl(writer, fcntl.F_DUPFD_CLOEXEC, 10);os.close(writer)
        script = '''import json,os,sys
sys.path.insert(0,sys.argv[1])
from src.ops.tools import github_signed_receipts as q
channel=int(sys.argv[2]);real=os;created=[]
class Proxy:
 def __getattr__(self,name):return getattr(real,name)
 def memfd_create(self,*args):
  fd=real.memfd_create(*args);created.append(fd);return fd
for fd in (0,1,2):os.close(fd)
q.os=Proxy()
ok=q._verify_ed25519(bytes.fromhex(sys.argv[3]),b'\\x72',bytes.fromhex(sys.argv[4]))
leaks=0
for name in os.listdir('/proc/self/fd'):
 try:target=os.readlink('/proc/self/fd/'+name)
 except OSError:continue
 if 'memfd:receipt-public' in target:leaks+=1
data=json.dumps({'verified':ok,'created':created,'memfd_leaks':leaks}).encode()
while data:data=data[os.write(channel,data):]
os.close(channel);os._exit(0)
'''
        try:
            process = subprocess.Popen([sys.executable, '-I', '-B', '-c', script, str(ROOT), str(channel),
                                        RFC_PUBLIC.hex(), RFC_SIGNATURE.hex()], pass_fds=(channel,),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            os.close(channel);channel = None
            try:_, stderr = process.communicate(timeout=10)
            except BaseException:
                process.kill();process.wait();raise
            raw = b''
            while True:
                chunk = os.read(reader, 4096)
                if not chunk:break
                raw += chunk
            self.assertEqual(process.returncode, 0);self.assertEqual(stderr, b'')
            value = json.loads(raw)
            self.assertIs(value['verified'], True)
            self.assertEqual(len(value['created']), 3)
            self.assertTrue(all(fd in (0, 1, 2) for fd in value['created']))
            self.assertEqual(value['memfd_leaks'], 0)
        finally:
            os.close(reader)
            if channel is not None:os.close(channel)

    def test_14_opaque_fail_claims_untrusted_rosters_exceptions_and_hooks(self):
        self.call(self.make_case(artifacts={'tests': b'FAIL ' + PRIVATE.encode(), 'reviewer': b'NOT JSON; review denied'}), 'CRYPTOGRAPHIC_MATCH')
        signers = copy.deepcopy(self.signers)
        signers['tests']['public_key_hex'], signers['reviewer']['public_key_hex'] = signers['reviewer']['public_key_hex'], signers['tests']['public_key_hex']
        self.call(self.make_case(signers=signers, actual_roles={'tests': 'reviewer', 'reviewer': 'tests'}), 'CRYPTOGRAPHIC_MATCH')
        with patch.object(q, 'canonical_binding_digest', side_effect=RuntimeError(PRIVATE)):
            self.call(self.case(), 'UNEXPECTED_FAILURE')
        called = []
        class EvilDict(dict):
            def get(self, *args):called.append('get');raise RuntimeError(PRIVATE)
            def __iter__(self):called.append('iter');raise RuntimeError(PRIVATE)
            def __eq__(self, other):called.append('eq');raise RuntimeError(PRIVATE)
        class EvilStr(str):
            def __eq__(self, other):called.append('str_eq');raise RuntimeError(PRIVATE)
            __hash__ = str.__hash__
        for position, reason in ((0, 'EXPECTED_BINDING_INVALID'), (1, 'SIGNER_PINS_INVALID'),
                                 (2, 'RECEIPTS_INVALID'), (3, 'ARTIFACTS_INVALID')):
            case = list(self.case());case[position] = EvilDict(case[position])
            self.no_crypto(case, reason)
        case = self.case();case[1]['tests']['actor'] = EvilStr('tests-fixture')
        self.no_crypto(case, 'SIGNER_PINS_INVALID')
        self.assertEqual(called, [])

    def test_15_python310_api_imports_and_existing_source_test_bytes_preserved(self):
        tree = ast.parse(SOURCE.read_text(), feature_version=(3, 10))
        ast.parse(Path(__file__).read_text(), feature_version=(3, 10))
        self.assertEqual(tuple(inspect.signature(q.verify_receipt_pair).parameters),
                         ('expected_binding', 'expected_signers', 'receipts', 'artifacts'))
        self.assertEqual(tuple(inspect.signature(q._verify_ed25519).parameters), ('public_key', 'message', 'signature'))
        self.assertEqual(q.canonical_binding_digest.__module__, 'src.ops.tools.github_check_audit')
        forbidden_imports = {'socket', 'http', 'urllib', 'ssl', 'argparse', 'tempfile'}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split('.')[0], sys.stdlib_module_names)
                    self.assertNotIn(alias.name.split('.')[0], forbidden_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.module, 'src.ops.tools.github_check_audit')
                self.assertEqual([item.name for item in node.names], ['canonical_binding_digest'])
        forbidden_calls = {'open', 'print', 'eval', 'exec', 'loads', 'load', 'chmod', 'chown', 'unlink', 'system', 'popen'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ''
                self.assertNotIn(name, forbidden_calls)
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    self.assertFalse(node.func.value.id == 'os' and node.func.attr == 'remove')
        self.assertNotIn('__main__', SOURCE.read_text())
        pins = {
            'src/ops/tools/github_check_audit.py': 'd2b8deb1b9a652d396c63f42d2c581af51c7c19aa9bfa7785f2e740580b3515e',
            'src/ops/tools/tests/test_github_check_audit.py': 'fcc0b85600114a578f55b8e075bc2519d4b71d900e5ddcd73faaf81b7e5539c5',
            'src/ops/tools/tests/test_github_evidence_binding.py': '9bb7a99002451f3422e3db7fe130bafacac39d294d1facc371b340535ca01f41',
            'docs/codex/github-signed-receipts-test-plan.md': '4e363aa576b810a180f0a125de87055bca0ade8c4cd377e7616fcf86cbaea765',
        }
        for path, expected in pins.items():self.assertEqual(sha((ROOT / path).read_bytes()), expected)


if __name__ == '__main__':
    unittest.main()
