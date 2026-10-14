"""Offline structural comparisons; no GitHub source authentication or gate use."""
import ast
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from src.ops.tools import github_check_audit as q


CONTEXTS = ('codex/required-tests', 'codex/independent-review', 'codex/policy')
FLAGS = {'input_origin_verified', 'trusted_gate_verified', 'binding_verified',
         'native_ready', 'merge_authorized'}
FIELDS = {'scope', 'status', 'structural_satisfied', 'reason', 'context'} | FLAGS
PRIVATE = 'PRIVATE_AUDIT_INPUT_/secret/token?email=private@example.invalid'
PROJECT = Path(__file__).resolve().parents[4]
MAX_BYTES = 1024 * 1024


def fixture(second=False):
    head = ('b' if second else 'a') * 40
    apps = {name: number + (700 if second else 100) for number, name in enumerate(CONTEXTS, 1)}
    rows = [{'id': 9001 + index, 'name': name, 'app': {'id': apps[name]},
             'head_sha': head, 'status': 'completed', 'conclusion': 'success'}
            for index, name in enumerate(CONTEXTS)]
    return head, apps, {'total_count': len(rows), 'check_runs': rows}


def envelope(head, apps, snapshot):
    return {'expected_head_sha': head, 'expected_apps': apps, 'snapshot': snapshot}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def assert_report(test, report, matched):
    test.assertEqual(set(report), FIELDS)
    test.assertEqual(report['scope'], 'OFFLINE_ONLY')
    test.assertEqual(report['status'], 'STRUCTURAL_MATCH' if matched else 'BLOCKED')
    test.assertIs(report['structural_satisfied'], matched)
    for name in FLAGS:
        test.assertIs(report[name], False, name)
    test.assertIs(type(report['reason']), str)
    test.assertRegex(report['reason'], r'^[A-Z][A-Z0-9_]{0,79}$')
    test.assertIn(report['context'], (None,) + CONTEXTS)
    test.assertNotIn('PASS', report.values())


def assert_private(test, text, head=None, apps=None, snapshot=None):
    forbidden = [PRIVATE, 'private@example.invalid', '/secret/', 'Traceback']
    if head:
        forbidden.append(head)
    if apps:
        forbidden.extend(str(value) for value in apps.values())
    if snapshot:
        forbidden.extend(str(row['id']) for row in snapshot['check_runs'] if 'id' in row)
    for value in forbidden:
        test.assertNotIn(value, text)


class AuditorTests(unittest.TestCase):
    def setUp(self):
        self.head, self.apps, self.snapshot = fixture()

    def audit(self, snapshot=None, head=None, apps=None):
        return q.audit_check_runs(self.head if head is None else head,
                                  self.apps if apps is None else apps,
                                  self.snapshot if snapshot is None else snapshot)

    def blocked(self, head, apps, snapshot):
        report = q.audit_check_runs(head, apps, snapshot)
        assert_report(self, report, False)
        return report

    def test_01_valid_external_comparison_pins_and_shared_app(self):
        for second in (False, True):
            head, apps, snapshot = fixture(second)
            assert_report(self, q.audit_check_runs(head, apps, snapshot), True)
        apps = dict.fromkeys(CONTEXTS, 555)
        for row in self.snapshot['check_runs']:
            row['app']['id'] = 555
        assert_report(self, self.audit(apps=apps), True)

    def test_02_order_and_unrelated_failure_do_not_replace_required_checks(self):
        self.snapshot['check_runs'].reverse()
        for number, status in enumerate(('completed', 'in_progress')):
            self.snapshot['check_runs'].append({'id': 20001 + number, 'name': 'other/ci-' + str(number),
                'app': {'id': 999}, 'head_sha': self.head, 'status': status, 'conclusion': 'failure'})
        self.snapshot['total_count'] = len(self.snapshot['check_runs'])
        assert_report(self, self.audit(), True)

    def test_03_invalid_expected_head_is_not_normalized_or_replaced(self):
        for head in (None, True, 7, '', 'A' * 40, 'a' * 39, 'a' * 41, ' ' + self.head, self.head + '\n'):
            with self.subTest(head=head):
                self.blocked(head, self.apps, self.snapshot)

    def test_04_expected_apps_requires_exact_fixed_keys(self):
        cases = [None, [], PRIVATE, {**self.apps, 'author/PASS': 999},
                 {name: value for name, value in self.apps.items() if name != CONTEXTS[0]},
                 {**self.apps, 1: 999}]
        for apps in cases:
            with self.subTest(kind=type(apps).__name__):
                self.blocked(self.head, apps, self.snapshot)

    def test_05_expected_app_ids_are_positive_integers_not_coerced(self):
        for value in (True, False, 0, -1, 101.0, '101', None):
            apps = dict(self.apps)
            apps[CONTEXTS[0]] = value
            with self.subTest(value=value):
                self.blocked(self.head, apps, self.snapshot)

    def test_06_malformed_snapshot_list_rows_and_missing_fields(self):
        for snapshot in (None, [], PRIVATE, {}, {'total_count': 3}, {'check_runs': []},
                         {'total_count': 1, 'check_runs': None}, {'total_count': 1, 'check_runs': {}}):
            with self.subTest(kind=type(snapshot).__name__):
                self.blocked(self.head, self.apps, snapshot)
        for row in (None, [], PRIVATE, {}, {'id': 99}, {'id': 99, 'name': ''}, {'id': 99, 'name': 'x' * 257}):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['check_runs'][0] = row
            with self.subTest(row_type=type(row).__name__):
                self.blocked(self.head, self.apps, snapshot)

    def test_07_count_type_and_incomplete_collection_are_blocked(self):
        for count in (True, False, -1, 3.0, '3', None, 2, 4):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['total_count'] = count
            with self.subTest(count=count):
                self.blocked(self.head, self.apps, snapshot)

    def test_08_empty_and_row_limit_boundaries(self):
        self.blocked(self.head, self.apps, {'total_count': 0, 'check_runs': []})
        for number in (1000, 1001):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['check_runs'].extend({'id': 30000 + index, 'name': 'other/' + str(index)}
                                          for index in range(number - 3))
            snapshot['total_count'] = number
            assert_report(self, self.audit(snapshot=snapshot), number == 1000)

    def test_09_every_run_id_must_be_positive_integer(self):
        for value in (True, False, 0, -1, 9.0, '9', None):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['check_runs'].append({'id': value, 'name': 'other/check'})
            snapshot['total_count'] += 1
            with self.subTest(value=value):
                self.blocked(self.head, self.apps, snapshot)

    def test_10_run_id_duplicates_across_names_are_blocked(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['check_runs'].append({'id': snapshot['check_runs'][0]['id'], 'name': 'other/check'})
        snapshot['total_count'] += 1
        self.blocked(self.head, self.apps, snapshot)

    def test_11_each_required_context_is_needed(self):
        for index in range(3):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['check_runs'].pop(index)
            snapshot['total_count'] -= 1
            with self.subTest(index=index):
                self.blocked(self.head, self.apps, snapshot)

    def test_12_same_context_same_app_head_duplicate_is_ambiguous(self):
        row = copy.deepcopy(self.snapshot['check_runs'][0])
        row['id'] = 17001
        self.snapshot['check_runs'].append(row)
        self.snapshot['total_count'] += 1
        self.blocked(self.head, self.apps, self.snapshot)

    def test_13_duplicate_name_is_blocked_for_other_app_or_head_in_either_order(self):
        for field in ('app', 'head_sha'):
            for first in (False, True):
                snapshot = copy.deepcopy(self.snapshot)
                row = copy.deepcopy(snapshot['check_runs'][0])
                row['id'] = 17001
                row[field] = {'id': 999} if field == 'app' else 'c' * 40
                snapshot['check_runs'].insert(0 if first else len(snapshot['check_runs']), row)
                snapshot['total_count'] += 1
                with self.subTest(field=field, first=first):
                    self.blocked(self.head, self.apps, snapshot)

    def test_14_wrong_app_with_exact_context_is_blocked(self):
        self.snapshot['check_runs'][0]['app']['id'] = 999
        self.blocked(self.head, self.apps, self.snapshot)

    def test_15_required_app_missing_or_wrong_type_is_blocked(self):
        for app in (None, [], PRIVATE, {}, {'slug': PRIVATE}, {'id': True}, {'id': 0},
                    {'id': -1}, {'id': 101.0}, {'id': '101'}):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['check_runs'][0]['app'] = app
            with self.subTest(app=app):
                self.blocked(self.head, self.apps, snapshot)

    def test_16_stale_and_mixed_head_are_blocked_despite_new_timestamps(self):
        for all_rows in (False, True):
            snapshot = copy.deepcopy(self.snapshot)
            for row in snapshot['check_runs'] if all_rows else snapshot['check_runs'][:1]:
                row.update(head_sha='c' * 40, completed_at='2099-12-31T23:59:59Z')
            self.blocked(self.head, self.apps, snapshot)

    def test_17_required_head_missing_or_malformed(self):
        for head in (None, True, 7, '', 'A' * 40, 'a' * 39, self.head + ' '):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['check_runs'][0]['head_sha'] = head
            with self.subTest(head=head):
                self.blocked(self.head, self.apps, snapshot)
        self.snapshot['check_runs'][0].pop('head_sha')
        self.blocked(self.head, self.apps, self.snapshot)

    def test_18_unfinished_unknown_and_wrong_type_status_are_blocked(self):
        for status in ('queued', 'in_progress', 'pending', 'unknown', None, True, 7):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['check_runs'][0]['status'] = status
            with self.subTest(status=status):
                self.blocked(self.head, self.apps, snapshot)

    def test_19_all_conclusions_except_success_are_blocked(self):
        for conclusion in ('failure', 'cancelled', 'timed_out', 'action_required', 'skipped',
                           'neutral', 'stale', 'startup_failure', 'unknown', None, True, 7):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['check_runs'][0]['conclusion'] = conclusion
            with self.subTest(conclusion=conclusion):
                self.blocked(self.head, self.apps, snapshot)

    def test_20_names_are_exact_not_case_space_or_substring_matches(self):
        for name in (CONTEXTS[0].upper(), ' ' + CONTEXTS[0], CONTEXTS[0] + ' ',
                     'prefix/' + CONTEXTS[0], 'codex/required', PRIVATE):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['check_runs'][0]['name'] = name
            with self.subTest(name=name):
                self.blocked(self.head, self.apps, snapshot)

    def test_21_well_formed_forgery_never_authenticates_a_gate(self):
        self.snapshot.update(input_origin_verified=True, trusted_gate_verified=True,
                             merge_authorized=True, author_PASS=True, signed_by=PRIVATE)
        for row in self.snapshot['check_runs']:
            row.update(trusted=True, author_PASS=True, provenance=PRIVATE)
        report = self.audit()
        assert_report(self, report, True)
        self.assertIs(report['trusted_gate_verified'], False)
        assert_private(self, json.dumps(report), self.head, self.apps, self.snapshot)

    def test_22_repository_base_rules_plan_and_evidence_remain_unverified(self):
        for field in ('repository', 'pr_number', 'base_sha', 'release_digest', 'plan_digest', 'evidence_digest'):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot[field] = PRIVATE
            report = self.audit(snapshot=snapshot)
            assert_report(self, report, True)
            self.assertIs(report['binding_verified'], False)

    def test_23_unknown_raw_fields_private_ids_paths_and_errors_are_not_output(self):
        self.snapshot['url'] = PRIVATE
        self.snapshot['check_runs'][0].update(external_id=PRIVATE, raw_stdout=PRIVATE, error=PRIVATE)
        self.snapshot['check_runs'][0]['app']['slug'] = PRIVATE
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            report = self.audit()
        assert_report(self, report, True)
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        assert_private(self, json.dumps(report), self.head, self.apps, self.snapshot)
        invalid_reasons = set()
        for value in (PRIVATE, PRIVATE + '/another'):
            report = self.blocked(value, self.apps, self.snapshot)
            invalid_reasons.add(report['reason'])
            assert_private(self, json.dumps(report))
        self.assertEqual(len(invalid_reasons), 1)

    def test_24_inputs_and_results_have_no_hidden_mutable_state(self):
        before = copy.deepcopy((self.head, self.apps, self.snapshot))
        first = self.audit()
        first['trusted_gate_verified'] = True
        assert_report(self, self.audit(), True)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['check_runs'][0]['conclusion'] = 'skipped'
        failed_before = copy.deepcopy(snapshot)
        self.blocked(self.head, self.apps, snapshot)
        self.assertEqual(snapshot, failed_before)
        self.assertEqual((self.head, self.apps, self.snapshot), before)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.head, self.apps, self.snapshot = fixture()
        self.raw = encode(envelope(self.head, self.apps, self.snapshot))

    def run_cli(self, raw, argv=None, stream=None):
        output, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = q.main([] if argv is None else argv, io.BytesIO(raw) if stream is None else stream, output)
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue(), '')
        report = json.loads(output.getvalue())
        assert_private(self, output.getvalue(), self.head, self.apps, self.snapshot)
        return report

    def test_25_matching_library_data_still_has_nonzero_cli_exit(self):
        assert_report(self, q.audit_check_runs(self.head, self.apps, self.snapshot), True)
        assert_report(self, self.run_cli(self.raw), True)
        result = subprocess.run([sys.executable, '-B', '-m', 'src.ops.tools.github_check_audit'],
                                cwd=PROJECT, input=self.raw, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, b'')
        assert_report(self, json.loads(result.stdout), True)
        assert_private(self, result.stdout.decode(), self.head, self.apps, self.snapshot)

    def test_26_cli_blocked_invalid_envelope_arguments_and_read_errors(self):
        for value in ({}, [], {'snapshot': self.snapshot},
                      {**envelope(self.head, self.apps, self.snapshot), 'PRIVATE_EXTRA': PRIVATE}):
            with self.subTest(envelope_type=type(value).__name__):
                assert_report(self, self.run_cli(encode(value)), False)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['check_runs'][0]['conclusion'] = 'skipped'
        assert_report(self, self.run_cli(encode(envelope(self.head, self.apps, snapshot))), False)
        forged = copy.deepcopy(self.snapshot)
        forged.update(input_origin_verified=True, trusted_gate_verified=True,
                      binding_verified=True, native_ready=True, merge_authorized=True, author_PASS=True)
        forged['check_runs'][0].update(author_PASS=True, provenance=PRIVATE)
        assert_report(self, self.run_cli(encode(envelope(self.head, self.apps, forged))), True)
        assert_report(self, self.run_cli(self.raw, argv=[PRIVATE]), False)
        assert_report(self, self.run_cli(b'', stream=io.StringIO(PRIVATE)), False)
        broken = unittest.mock.Mock()
        broken.read.side_effect = OSError(PRIVATE)
        assert_report(self, self.run_cli(b'', stream=broken), False)
        broken.read.assert_called_once_with(MAX_BYTES + 1)
        for failure_point in ('write', 'flush'):
            for error_type in (OSError, ValueError):
                output = unittest.mock.Mock()
                getattr(output, failure_point).side_effect = error_type(PRIVATE)
                err = io.StringIO()
                with self.subTest(failure_point=failure_point, error_type=error_type.__name__), redirect_stderr(err):
                    self.assertEqual(q.main([], io.BytesIO(self.raw), output), 2)
                self.assertEqual(err.getvalue(), '')
        process = subprocess.Popen([sys.executable, '-B', '-m', 'src.ops.tools.github_check_audit'],
                                   cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.stdout.close()
        process.stdout = None
        _, stderr = process.communicate(input=self.raw, timeout=5)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(stderr, b'')

    def test_27_json_duplicates_nonfinite_integer_depth_and_byte_limits(self):
        invalid = [b'', b'\xff' + PRIVATE.encode(), b'{' + PRIVATE.encode(),
                   b'{"snapshot":0,"snapshot":1}', b'NaN', b'Infinity', b'-Infinity',
                   b'{"x":1e400}', b'[' * 2000 + b'0' + b']' * 2000]
        for token in ('NaN', 'Infinity', '-Infinity', '1e400', '9' * 21):
            invalid.append(self.raw.replace(b'"total_count":3', ('"total_count":' + token).encode()))
        for raw in invalid:
            with self.subTest(size=len(raw)):
                report = self.run_cli(raw)
                assert_report(self, report, False)
                self.assertEqual(report['reason'], 'INPUT_INVALID')
        apps = dict.fromkeys(CONTEXTS, int('9' * 20))
        snapshot = copy.deepcopy(self.snapshot)
        for row in snapshot['check_runs']:
            row['app']['id'] = int('9' * 20)
        assert_report(self, self.run_cli(encode(envelope(self.head, apps, snapshot))), True)
        exact = self.raw + b' ' * (MAX_BYTES - len(self.raw))
        assert_report(self, self.run_cli(exact), True)
        class LimitedInput:
            def __init__(self):
                self.sizes = []
            def read(self, size):
                self.sizes.append(size)
                return exact + b' '
        stream = LimitedInput()
        report = self.run_cli(b'', stream=stream)
        assert_report(self, report, False)
        self.assertEqual(report['reason'], 'INPUT_INVALID')
        self.assertEqual(stream.sizes, [MAX_BYTES + 1])

    def test_28_stdlib_python310_syntax_and_pure_no_io_boundary(self):
        source = Path(q.__file__).read_text()
        tree = ast.parse(source, feature_version=(3, 10))
        ast.parse(Path(__file__).read_text(), feature_version=(3, 10))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or '').split('.')[0])
        self.assertTrue(imports <= sys.stdlib_module_names)
        self.assertTrue(imports.isdisjoint({'socket', 'subprocess', 'urllib', 'http'}))
        with patch('builtins.open', side_effect=AssertionError('unexpected file read')) as opened, \
                patch.object(socket, 'socket', side_effect=AssertionError('unexpected network')) as network, \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('unexpected subprocess')) as process:
            assert_report(self, q.audit_check_runs(self.head, self.apps, self.snapshot), True)
            opened.assert_not_called()
            network.assert_not_called()
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
