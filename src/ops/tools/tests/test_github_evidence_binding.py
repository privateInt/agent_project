"""Offline binding claims; matching fields never authenticate a GitHub gate."""
import ast
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch

from src.ops.tools import github_check_audit as q
from src.ops.tools.tests.test_github_check_audit import (
    CONTEXTS, FIELDS, FLAGS, assert_report, fixture,
)


PROJECT = Path(__file__).resolve().parents[4]
BASE = '66b3f4fb1e054862e2b3a94e167982f0c5173a5f'
SOURCE_PATH = 'src/ops/tools/github_check_audit.py'
OLD_TEST_PATH = 'src/ops/tools/tests/test_github_check_audit.py'
OLD_TEST_SHA256 = 'fcc0b85600114a578f55b8e075bc2519d4b71d900e5ddcd73faaf81b7e5539c5'
GOLDEN_DIGEST = 'bda08a5181637f313a0a74ac01c8e15baf4189b2ab9e7b703dd1a719329ac931'
PRIVATE = 'PRIVATE_BINDING_/secret/token?email=private@example.invalid'
BINDING_FIELDS = FIELDS | {'binding_fields_satisfied'}
LEGACY_FUNCTIONS = ('_result', '_positive_int', '_canonical_sha', 'audit_check_runs',
                    '_unique_object', '_bounded_integer', '_finite_float',
                    '_reject_constant', 'main')


def binding_fixture():
    return {'repository': 'ExampleOrg/example_repo', 'pr_number': 17,
            'base_sha': 'a' * 40, 'head_sha': 'b' * 40,
            'release_sha256': 'c' * 64, 'plan_sha256': 'd' * 64,
            'evidence_sha256': 'e' * 64}


def assert_bound(test, report, matched, fields):
    test.assertEqual(set(report), BINDING_FIELDS)
    legacy = {key: value for key, value in report.items() if key != 'binding_fields_satisfied'}
    assert_report(test, legacy, matched)
    test.assertIs(report['binding_fields_satisfied'], fields)


class EvidenceBindingTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding_fixture()
        _, self.apps, self.snapshot = fixture()
        for row in self.snapshot['check_runs']:
            row['head_sha'] = self.binding['head_sha']

    def evidence(self, binding=None, snapshot=None):
        observed = copy.deepcopy(self.binding if binding is None else binding)
        return {'binding': observed, 'binding_digest': q.canonical_binding_digest(observed),
                'snapshot': copy.deepcopy(self.snapshot if snapshot is None else snapshot)}

    def reject_binding(self, invalid):
        self.assertIsNone(q.canonical_binding_digest(invalid))
        assert_bound(self, q.audit_bound_checks(invalid, self.apps, self.evidence()), False, False)
        observed = self.evidence()
        observed['binding'] = copy.deepcopy(invalid)
        observed['binding_digest'] = q.canonical_binding_digest(invalid)
        assert_bound(self, q.audit_bound_checks(self.binding, self.apps, observed), False, False)

    def changed_fields(self):
        return {'base_sha': 'f' * 40, 'head_sha': 'f' * 40,
                'repository': 'OtherOrg/other_repo', 'pr_number': 18,
                'release_sha256': 'f' * 64, 'plan_sha256': 'f' * 64,
                'evidence_sha256': 'f' * 64}

    def test_01_literal_digest_vector_and_key_order(self):
        self.assertEqual(q.canonical_binding_digest(self.binding), GOLDEN_DIGEST)
        reversed_binding = dict(reversed(list(self.binding.items())))
        self.assertEqual(q.canonical_binding_digest(reversed_binding), GOLDEN_DIGEST)
        self.assertRegex(q.canonical_binding_digest(self.binding), r'^[0-9a-f]{64}$')

    def test_02_repository_valid_boundaries_and_case_are_preserved(self):
        for repository in ('a/b', 'a' * 100 + '/' + 'b' * 100, 'A_-.9/z.-_0'):
            with self.subTest(repository=repository):
                binding = {**self.binding, 'repository': repository}
                self.assertRegex(q.canonical_binding_digest(binding), r'^[0-9a-f]{64}$')
                assert_bound(self, q.audit_bound_checks(binding, self.apps, self.evidence(binding)), True, True)
        changed = {**self.binding, 'repository': self.binding['repository'].lower()}
        self.assertNotEqual(q.canonical_binding_digest(changed), GOLDEN_DIGEST)

    def test_03_exact_binding_shape_on_both_sides(self):
        cases = [None, [], PRIVATE, 7, {}, {**self.binding, 'author_PASS': True},
                 {**self.binding, 1: PRIVATE}]
        cases.extend({key: value for key, value in self.binding.items() if key != omitted}
                     for omitted in self.binding)
        for invalid in cases:
            with self.subTest(kind=type(invalid).__name__, size=len(invalid) if isinstance(invalid, dict) else None):
                self.reject_binding(invalid)

    def test_04_repository_invalid_values_are_not_normalized(self):
        repositories = ('', 'noslash', 'a/b/c', '/b', 'a/', 'a' * 101 + '/b',
                        'a/' + 'b' * 101, './b', '../b', 'a/.', 'a/..',
                        ' a/b', 'a/b ', 'a/\nb', 'a/\x00b', '한글/repo',
                        'a/@b', 'a/\\b', PRIVATE, None, True, 7)
        for repository in repositories:
            with self.subTest(kind=type(repository).__name__):
                self.reject_binding({**self.binding, 'repository': repository})

    def test_05_pr_number_exact_integer_bounds(self):
        for number in (1, 2**63 - 1):
            binding = {**self.binding, 'pr_number': number}
            self.assertIsNotNone(q.canonical_binding_digest(binding))
            assert_bound(self, q.audit_bound_checks(binding, self.apps, self.evidence(binding)), True, True)
        for number in (0, -1, 2**63, True, False, 17.0, '17', None):
            with self.subTest(number=number):
                self.reject_binding({**self.binding, 'pr_number': number})

    def test_06_sha_and_digest_shapes_on_both_sides(self):
        for field in ('base_sha', 'head_sha', 'release_sha256', 'plan_sha256', 'evidence_sha256'):
            width = 40 if field in ('base_sha', 'head_sha') else 64
            for value in ('a' * (width - 1), 'a' * (width + 1), 'A' * width,
                          'g' * width, 'a' * width + '\n', ' ' + 'a' * width,
                          None, True, 7, [], PRIVATE):
                with self.subTest(field=field, kind=type(value).__name__):
                    self.reject_binding({**self.binding, field: value})

    def test_07_each_valid_field_change_changes_digest_without_mutation(self):
        original = copy.deepcopy(self.binding)
        for field, value in self.changed_fields().items():
            with self.subTest(field=field):
                changed = {**self.binding, field: value}
                digest = q.canonical_binding_digest(changed)
                self.assertIsNotNone(digest)
                self.assertNotEqual(digest, GOLDEN_DIGEST)
        self.assertEqual(self.binding, original)

    def test_08_normal_report_exact_allowlist_and_false_trust(self):
        report = q.audit_bound_checks(self.binding, self.apps, self.evidence())
        assert_bound(self, report, True, True)
        self.assertIsNone(report['context'])
        self.assertEqual(len(report), 11)
        self.assertNotIn(GOLDEN_DIGEST, json.dumps(report))

    def test_09_exact_evidence_envelope(self):
        evidence = self.evidence()
        cases = [None, [], PRIVATE, {}, {**evidence, 'trusted_gate_verified': True},
                 {**evidence, 1: PRIVATE}]
        cases.extend({key: value for key, value in evidence.items() if key != omitted}
                     for omitted in evidence)
        for invalid in cases:
            with self.subTest(kind=type(invalid).__name__):
                assert_bound(self, q.audit_bound_checks(self.binding, self.apps, invalid), False, False)

    def test_10_each_observed_field_mismatch_with_valid_observed_claim_is_blocked(self):
        # Base first: a valid changed base cannot be rejected by the snapshot head check.
        changed = {**self.binding, 'base_sha': 'f' * 40}
        assert_bound(self, q.audit_bound_checks(self.binding, self.apps, self.evidence(changed)), False, False)
        for field, value in self.changed_fields().items():
            if field == 'base_sha':
                continue
            with self.subTest(field=field):
                changed = {**self.binding, field: value}
                assert_bound(self, q.audit_bound_checks(self.binding, self.apps, self.evidence(changed)), False, False)

    def test_11_valid_wrong_claim_and_malformed_claims_are_blocked(self):
        # Bindings match; only this valid, different lower-hex claim is wrong.
        evidence = self.evidence()
        evidence['binding_digest'] = 'f' * 64
        assert_bound(self, q.audit_bound_checks(self.binding, self.apps, evidence), False, False)
        for claimed in (None, True, 7, [], '', 'a' * 63, 'a' * 65, 'A' * 64,
                        'g' * 64, GOLDEN_DIGEST + '\n', PRIVATE):
            with self.subTest(kind=type(claimed).__name__):
                evidence = self.evidence()
                evidence['binding_digest'] = claimed
                assert_bound(self, q.audit_bound_checks(self.binding, self.apps, evidence), False, False)

    def test_12_invalid_none_digests_cannot_match_each_other(self):
        for invalid in (None, {}, {**self.binding, 'pr_number': False}):
            evidence = {'binding': invalid, 'binding_digest': None, 'snapshot': self.snapshot}
            assert_bound(self, q.audit_bound_checks(invalid, self.apps, evidence), False, False)

    def test_13_post_binding_check_failure_preserves_only_fields_satisfied(self):
        for kind in ('app', 'status', 'conclusion', 'missing', 'expected_apps', 'snapshot'):
            snapshot = copy.deepcopy(self.snapshot)
            apps = copy.deepcopy(self.apps)
            if kind == 'app':
                snapshot['check_runs'][0]['app']['id'] = 999
            elif kind == 'status':
                snapshot['check_runs'][0]['status'] = 'in_progress'
            elif kind == 'conclusion':
                snapshot['check_runs'][0]['conclusion'] = 'skipped'
            elif kind == 'missing':
                snapshot['check_runs'].pop()
                snapshot['total_count'] -= 1
            elif kind == 'expected_apps':
                apps = None
            else:
                snapshot = []
            with self.subTest(kind=kind):
                assert_bound(self, q.audit_bound_checks(self.binding, apps, self.evidence(snapshot=snapshot)), False, True)

    def test_14_matching_binding_head_does_not_replace_snapshot_head_guard(self):
        for all_rows in (False, True):
            snapshot = copy.deepcopy(self.snapshot)
            for row in snapshot['check_runs'] if all_rows else snapshot['check_runs'][:1]:
                row['head_sha'] = 'f' * 40
            assert_bound(self, q.audit_bound_checks(self.binding, self.apps, self.evidence(snapshot=snapshot)), False, True)

    def test_15_artifact_claim_is_not_a_snapshot_or_artifact_bytes_verification(self):
        binding = {**self.binding, 'evidence_sha256': '0' * 64}
        digest_before = q.canonical_binding_digest(binding)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.update(artifact_bytes=PRIVATE, evidence_sha256='1' * 64,
                        binding_verified=True, metadata={'path': PRIVATE})
        self.assertNotEqual(hashlib.sha256(json.dumps(snapshot).encode()).hexdigest(), binding['evidence_sha256'])
        report = q.audit_bound_checks(binding, self.apps, self.evidence(binding, snapshot))
        assert_bound(self, report, True, True)
        self.assertEqual(q.canonical_binding_digest(binding), digest_before)

    def test_16_forging_both_sides_never_authenticates_the_match(self):
        forged = {**self.binding, **self.changed_fields()}
        apps = dict.fromkeys(CONTEXTS, 777)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.update(input_origin_verified=True, trusted_gate_verified=True,
                        binding_verified=True, native_ready=True, merge_authorized=True, author_PASS=True)
        for row in snapshot['check_runs']:
            row.update(head_sha=forged['head_sha'], app={'id': 777}, provenance=PRIVATE)
        report = q.audit_bound_checks(forged, apps, self.evidence(forged, snapshot))
        assert_bound(self, report, True, True)
        for name in FLAGS:
            self.assertIs(report[name], False, name)

    def test_17_private_output_immutability_and_repeat_without_hidden_state(self):
        evidence = self.evidence()
        evidence['snapshot'].update(private=PRIVATE, binding_digest=GOLDEN_DIGEST)
        original = copy.deepcopy((self.binding, self.apps, evidence))
        stdout, stderr = io.StringIO(), io.StringIO()
        reports = []
        with redirect_stdout(stdout), redirect_stderr(stderr):
            reports.append(q.audit_bound_checks(self.binding, self.apps, evidence))
            bad = copy.deepcopy(evidence)
            bad['binding']['repository'] = PRIVATE
            reports.append(q.audit_bound_checks(self.binding, self.apps, bad))
            bad_claim = copy.deepcopy(evidence)
            bad_claim['binding_digest'] = PRIVATE
            reports.append(q.audit_bound_checks(self.binding, self.apps, bad_claim))
            reports.append(q.audit_bound_checks(self.binding, self.apps, evidence))
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual((self.binding, self.apps, evidence), original)
        self.assertEqual(reports[0], reports[-1])
        reports[0]['trusted_gate_verified'] = True
        self.assertIs(reports[-1]['trusted_gate_verified'], False)
        assert_bound(self, reports[-1], True, True)
        for report in reports[1:3]:
            assert_bound(self, report, False, False)
        rendered = json.dumps(reports[1:])
        forbidden = [PRIVATE, 'private@example.invalid', '/secret/', GOLDEN_DIGEST,
                     self.binding['repository'], str(self.binding['pr_number'])]
        forbidden += [self.binding[name] for name in self.binding if name.endswith('sha') or name.endswith('sha256')]
        forbidden += [str(value) for value in self.apps.values()]
        forbidden += [str(row['id']) for row in self.snapshot['check_runs']]
        for value in forbidden:
            self.assertNotIn(value, rendered)

    def test_18_legacy_source_and_test_preserved_stdlib_and_pure_api(self):
        baseline = subprocess.check_output(['git', 'show', BASE + ':' + SOURCE_PATH],
                                           cwd=PROJECT, text=True)
        current = (PROJECT / SOURCE_PATH).read_text()
        old_tree, new_tree = ast.parse(baseline), ast.parse(current, feature_version=(3, 10))
        old_functions = {node.name: node for node in old_tree.body if isinstance(node, ast.FunctionDef)}
        new_functions = {node.name: node for node in new_tree.body if isinstance(node, ast.FunctionDef)}
        for name in LEGACY_FUNCTIONS:
            self.assertEqual(ast.dump(old_functions[name]), ast.dump(new_functions[name]), name)
            self.assertEqual(ast.get_source_segment(baseline, old_functions[name]),
                             ast.get_source_segment(current, new_functions[name]), name)
        old_assigns = {node.targets[0].id: node for node in old_tree.body
                       if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
        new_assigns = {node.targets[0].id: node for node in new_tree.body
                       if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
        for name, node in old_assigns.items():
            self.assertEqual(ast.dump(node), ast.dump(new_assigns[name]), name)
            self.assertEqual(ast.get_source_segment(baseline, node),
                             ast.get_source_segment(current, new_assigns[name]), name)
        old_entry = [node for node in old_tree.body if isinstance(node, ast.If)]
        new_entry = [node for node in new_tree.body if isinstance(node, ast.If)]
        self.assertEqual(len(old_entry), 1)
        self.assertEqual(len(new_entry), 1)
        self.assertEqual(ast.dump(old_entry[0]), ast.dump(new_entry[0]))
        self.assertEqual(ast.get_source_segment(baseline, old_entry[0]), ast.get_source_segment(current, new_entry[0]))
        imports = lambda tree: [ast.dump(node) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports(new_tree), [ast.dump(ast.parse('import hashlib').body[0])] + imports(old_tree))
        self.assertEqual(hashlib.sha256((PROJECT / OLD_TEST_PATH).read_bytes()).hexdigest(), OLD_TEST_SHA256)
        evidence = self.evidence()
        with patch('builtins.open', side_effect=AssertionError('file IO')), \
             patch.object(socket, 'socket', side_effect=AssertionError('network IO')), \
             patch.object(subprocess, 'Popen', side_effect=AssertionError('subprocess IO')):
            self.assertEqual(q.canonical_binding_digest(self.binding), GOLDEN_DIGEST)
            assert_bound(self, q.audit_bound_checks(self.binding, self.apps, evidence), True, True)


if __name__ == '__main__':
    unittest.main()
