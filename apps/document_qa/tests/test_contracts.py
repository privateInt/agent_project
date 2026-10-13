"""Offline Q01 scenarios. Synthetic API shapes; no application/model requests."""
import ast
import copy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from apps.document_qa import contracts as c

FOLDER = Path(__file__).resolve().parents[1]


def raw(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class ContractScenarios(unittest.TestCase):
    def setUp(self):
        self.f = json.loads((FOLDER / 'fixtures/api-responses.json').read_text())
        self.ids = self.f['ids']
        self.refinement = c.acceptance('data.refine', 202, raw(self.f['receipts']['data.refine']), {})
        self.build = c.acceptance('knowledge.build', 202, raw(self.f['receipts']['knowledge.build']), {'index_id': self.ids['index']})
        self.document = c.canonical_reference(self.f['canonical_metadata'], raw(self.f['canonical_content']),
                                               self.refinement, self.ids['source'], 'b' * 64, self.ids['canonical'])
        self.head = c.QuestionHead(self.ids['library'], 1, (self.document,), self.ids['index'],
                                   self.ids['index_version'], self.ids['embedding'], self.f['space_id'],
                                   self.ids['tool_version'], self.ids['agent_version'])

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(c.ContractError) as context:
            function(*args, **kwargs)
        self.assertEqual(str(context.exception), code)

    def verified(self, head=None, **changes):
        readback = copy.deepcopy(self.f['binding_readback'])
        readback.update(changes)
        return c.verify_bindings(head or self.head, **readback)

    def test_upload_200_is_registration_not_job_completion(self):
        receipt = c.acceptance('data.upload', 200, raw(self.f['receipts']['data.upload']), {'sha256': 'b' * 64, 'size_bytes': 128})
        self.assertEqual(receipt.resource_id, self.ids['source'])
        self.assertEqual(receipt.operation_id, '')
        job = c.Job(self.ids['job'], self.ids['library'], 1)
        following = c.advance_job(job, 'upload', 'complete')
        self.assertEqual((following.stage, following.state), ('refinement', 'pending'))

    def test_upload_wrong_status_digest_or_deleted_source_refused(self):
        body = self.f['receipts']['data.upload']
        self.error('HTTP_RECEIPT_INVALID', c.acceptance, 'data.upload', 202, raw(body), {})
        self.error('RECEIPT_IDENTITY_MISMATCH', c.acceptance, 'data.upload', 200, raw(body), {'sha256': 'c' * 64})
        body['deleted_at'] = '2026-10-02T00:00:00Z'
        self.error('SOURCE_UNAVAILABLE', c.acceptance, 'data.upload', 200, raw(body), {})

    def test_refinement_acceptance_does_not_mean_completed(self):
        status = self.f['refinement_completed']
        for state in ('created', 'pending', 'dispatching', 'running', 'retry_wait', 'cancelling'):
            with self.subTest(state=state):
                self.assertEqual(c.observe_refinement(self.refinement, dict(status, status=state, output_document_id=''), self.ids['source']), 'waiting')
        self.assertEqual(c.observe_refinement(self.refinement, status, self.ids['source']), 'complete')

    def test_refinement_failure_missing_output_and_wrong_source(self):
        body = self.f['refinement_completed']
        self.assertEqual(c.observe_refinement(self.refinement, dict(body, status='failed'), self.ids['source']), 'failed')
        self.error('REFINEMENT_RESULT_INVALID', c.observe_refinement, self.refinement, dict(body, output_document_id=''), self.ids['source'])
        self.error('REFINEMENT_IDENTITY_MISMATCH', c.observe_refinement, self.refinement, body, self.ids['canonical'])
        self.error('REFINEMENT_RESULT_INVALID', c.observe_refinement, self.refinement, dict(body, status='new_unknown_state'), self.ids['source'])

    def test_canonical_ready_partial_empty_and_tampered_content(self):
        meta, body = self.f['canonical_metadata'], self.f['canonical_content']
        for state, blocks, code in [('needs_ocr', body['blocks'], 'CANONICAL_ATTENTION_REQUIRED'),
                                    ('needs_review', body['blocks'], 'CANONICAL_ATTENTION_REQUIRED'),
                                    ('ready', [], 'CANONICAL_EMPTY')]:
            changed = copy.deepcopy(body)
            changed['quality']['state'], changed['blocks'] = state, blocks
            encoded = raw(changed)
            modified_meta = dict(meta, state=state, size_bytes=len(encoded), sha256=hashlib.sha256(encoded).hexdigest())
            self.error(code, c.canonical_reference, modified_meta, encoded, self.refinement, self.ids['source'], 'b' * 64, self.ids['canonical'])
        self.error('CANONICAL_BYTES_MISMATCH', c.canonical_reference, meta, raw(body) + b' ', self.refinement, self.ids['source'], 'b' * 64, self.ids['canonical'])
        self.error('CANONICAL_IDENTITY_MISMATCH', c.canonical_reference, meta, raw(body), self.refinement, self.ids['source'], 'b' * 64, self.ids['tool_version'])

    def test_snapshot_keeps_existing_document_and_limits_total_input(self):
        other = c.CanonicalRef(self.ids['tool_version'], self.ids['agent_version'], 'd' * 64, 100)
        snapshot = c.document_snapshot((other, self.document))
        self.assertEqual(set(d.source_id for d in snapshot), {self.ids['source'], other.source_id})
        self.error('DOCUMENT_DUPLICATE', c.document_snapshot, (self.document, self.document))
        self.error('DOCUMENT_BYTES_EXCEEDED', c.document_snapshot, (replace(self.document, size_bytes=c.MAX_BYTES), other))
        self.error('DOCUMENT_COUNT_INVALID', c.document_snapshot, (self.document,) * 21)
        self.error('DOCUMENT_COUNT_INVALID', c.document_snapshot, ())

    def test_index_full_version_receipt_and_ready_gate(self):
        body = self.f['binding_readback']['version']
        for state in ('protecting', 'queued', 'indexing'):
            self.assertEqual(c.observe_index(self.build, dict(body, state=state), self.ids['index'], (self.document,), self.ids['embedding'], self.f['space_id']), 'waiting')
        self.assertEqual(c.observe_index(self.build, body, self.ids['index'], (self.document,), self.ids['embedding'], self.f['space_id']), 'complete')
        self.error('HTTP_RECEIPT_INVALID', c.acceptance, 'knowledge.build', 200, raw(body), {})

    def test_index_counts_references_snapshot_and_embedding_must_match(self):
        original = self.f['binding_readback']['version']
        variants = [({'indexed_count': 0}, 'INDEX_NOT_READY'), ({'chunk_count': 0, 'indexed_count': 0}, 'INDEX_NOT_READY'),
                    ({'reference_state': 'pending'}, 'INDEX_NOT_READY'), ({'canonical_ids': []}, 'INDEX_DOCUMENTS_MISMATCH'),
                    ({'canonical_ids': [self.document.canonical_id] * 2}, 'INDEX_DOCUMENTS_MISMATCH'),
                    ({'embedding_profile': {'deployment_id': self.ids['embedding'], 'embedding_space_id': 'es_' + 'b' * 64}}, 'INDEX_EMBEDDING_MISMATCH')]
        for patch, code in variants:
            with self.subTest(patch=patch):
                self.error(code, c.observe_index, self.build, dict(original, **patch), self.ids['index'], (self.document,), self.ids['embedding'], self.f['space_id'])

    def test_agent_acceptance_status_url_and_pinned_version(self):
        body = self.f['receipts']['agent.run']
        self.assertEqual(c.acceptance('agent.run', 202, raw(body), {'agent_version_id': self.ids['agent_version']}).status_url, body['status_url'])
        self.error('AGENT_NOT_ACCEPTED', c.acceptance, 'agent.run', 202, raw(dict(body, status_url='https://example.invalid/secret')), {})
        self.error('RECEIPT_IDENTITY_MISMATCH', c.acceptance, 'agent.run', 202, raw(body), {'agent_version_id': self.ids['tool_version']})

    def test_agent_run_wait_failure_and_no_evidence(self):
        body = self.f['binding_readback']['agent_test']
        receipt = c.Receipt(self.ids['agent_run'], version_id=self.ids['agent_version'])
        self.assertEqual(c.observe_agent_run(receipt, dict(body, state='accepted', result=None), 'test'), 'waiting')
        self.assertEqual(c.observe_agent_run(receipt, dict(body, state='interrupted', result=None), 'test'), 'failed')
        self.assertEqual(c.observe_agent_run(receipt, body, 'test'), 'complete')
        no_evidence = copy.deepcopy(body)
        no_evidence['result'].update(answer='선택한 자료에서 질문에 답할 충분한 근거를 찾지 못했습니다.', outcome='no_evidence', citations=[])
        self.assertEqual(c.observe_agent_run(receipt, no_evidence, 'test'), 'complete')
        self.error('AGENT_RUN_IDENTITY_MISMATCH', c.observe_agent_run, receipt, body, 'run')

    def test_failed_or_empty_answer_is_not_success(self):
        body = copy.deepcopy(self.f['binding_readback']['agent_test'])
        receipt = c.Receipt(self.ids['agent_run'], version_id=self.ids['agent_version'])
        body['result']['answer'] = ''
        self.error('AGENT_RESULT_INVALID', c.observe_agent_run, receipt, body, 'test')
        body['result']['answer'], body['result']['citations'] = '근거 없이 만든 답변', []
        self.error('AGENT_CITATIONS_INVALID', c.observe_agent_run, receipt, body, 'test')

    def test_both_index_and_agent_readback_required(self):
        verified = self.verified()
        self.assertEqual(c.switch_head(None, verified, 0), self.head)
        self.error('HEAD_NOT_VERIFIED', c.switch_head, None, self.head, 0)
        bundle = self.f['binding_readback']
        self.error('INDEX_ACTIVATION_UNVERIFIED', self.verified, index=dict(bundle['index'], active_version_id=''))
        self.error('TOOL_BINDING_UNVERIFIED', self.verified, tool=dict(bundle['tool'], state='draft'))
        self.error('AGENT_BINDING_UNVERIFIED', self.verified, agent=dict(bundle['agent'], tool_version_id=self.ids['canonical']))
        self.error('TOOL_TEST_UNVERIFIED', self.verified, tool_test=dict(bundle['tool_test'], state='failed'))
        self.error('AGENT_RUN_IDENTITY_MISMATCH', self.verified, agent_test=dict(bundle['agent_test'], agent_version_id=self.ids['tool_version']))

    def test_tool_envelope_and_agent_knowledge_identity(self):
        body = copy.deepcopy(self.f['binding_readback']['tool_test'])
        body['result'] = body['result']['data']  # Flattening the real API wrapper must fail.
        self.error('TOOL_TEST_IDENTITY_MISMATCH', self.verified, tool_test=body)
        body = copy.deepcopy(self.f['binding_readback']['agent_test'])
        body['result']['identity']['knowledge']['index_version_id'] = self.ids['canonical']
        self.error('AGENT_TEST_IDENTITY_MISMATCH', self.verified, agent_test=body)

    def test_separate_test_questions_can_have_different_query_token_counts(self):
        body = copy.deepcopy(self.f['binding_readback']['agent_test'])
        body['result']['identity']['knowledge']['embedding_input_tokens'] += 1
        self.assertIsInstance(self.verified(agent_test=body), c.VerifiedHead)

    def test_switch_conflict_preserves_prior_head_and_old_run_reference(self):
        old_question = c.question_target(self.head)
        following = replace(self.head, revision=2)
        verified = self.verified(following)
        self.error('LIBRARY_REVISION_CONFLICT', c.switch_head, self.head, verified, 0)
        self.assertEqual(c.switch_head(self.head, verified, 1), following)
        self.assertEqual(old_question.revision, 1)
        with self.assertRaises(FrozenInstanceError):
            self.head.revision = 7

    def test_addition_failure_uses_old_head_exclusion_blocks_new_questions(self):
        self.assertEqual(c.question_target(self.head), self.head)
        self.error('DOCUMENT_EXCLUSION_PENDING', c.question_target, self.head, True)
        self.error('NO_READY_DOCUMENTS', c.question_target, None)

    def test_same_request_key_survives_refresh_and_rejects_body_change(self):
        body = {'name': '자료', 'document_id': self.ids['source']}
        first = c.request_intent(self.ids['job'], 'data.refine', 1, body)
        same = c.request_intent(self.ids['job'], 'data.refine', 1, dict(reversed(list(body.items()))))
        self.assertEqual(first, same)
        self.assertEqual(c.reuse_intent(first, body), first.key)
        self.error('REQUEST_KEY_CONFLICT', c.reuse_intent, first, dict(body, name='바뀐 자료'))
        self.assertEqual(c.request_intent(self.ids['job'], 'data.refine', 1, dict(body, name='바뀐 자료')).key, first.key)
        self.assertNotEqual(c.request_intent(self.ids['job'], 'data.refine', 1, body, attempt=2).key, first.key)

    def test_key_is_deterministic_in_a_fresh_python_process(self):
        intent = c.request_intent(self.ids['job'], 'data.refine', 1, {'value': 1})
        code = f"from apps.document_qa.contracts import request_intent; print(request_intent({self.ids['job']!r}, 'data.refine', 1, {{'value': 1}}).key)"
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=FOLDER.parents[1], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), intent.key)

    def test_uncertain_acceptance_and_activation_have_different_recovery(self):
        self.assertEqual(c.recovery_action('data.refine', 'uncertain'), 'replay_same_key_and_body')
        self.assertEqual(c.recovery_action('data.refine', 'uncertain', True), 'get_existing_resource')
        self.assertEqual(c.recovery_action('data.refine', 'complete'), 'reuse_completed_receipt')
        self.assertEqual(c.recovery_action('data.refine', 'failed'), 'review_before_new_attempt')
        self.assertEqual(c.recovery_action('knowledge.activate', 'uncertain'), 'read_index_and_compare_revision')

    def test_out_of_order_and_duplicate_stage_cannot_advance_job_twice(self):
        job = c.Job(self.ids['job'], self.ids['library'], 1)
        self.error('JOB_TRANSITION_INVALID', c.advance_job, job, 'indexing', 'complete')
        following = c.advance_job(job, 'upload', 'complete')
        self.error('JOB_TRANSITION_INVALID', c.advance_job, following, 'upload', 'complete')
        for stage in ('refinement', 'indexing', 'binding'):
            following = c.advance_job(following, stage, 'complete')
        self.assertEqual(following.state, 'completed')
        self.error('JOB_TRANSITION_INVALID', c.advance_job, following, 'binding', 'complete')

    def test_uncertain_can_be_resolved_but_failed_job_needs_explicit_new_attempt(self):
        job = c.Job(self.ids['job'], self.ids['library'], 1)
        uncertain = c.advance_job(job, 'upload', 'uncertain')
        self.assertEqual(c.advance_job(uncertain, 'upload', 'complete').stage, 'refinement')
        failed = c.advance_job(job, 'upload', 'failed')
        self.error('JOB_TRANSITION_INVALID', c.advance_job, failed, 'upload', 'complete')

    def test_request_contracts_reject_unknown_duplicate_and_null_fields(self):
        body = {'name': '자료', 'document_id': self.ids['source']}
        self.assertEqual(c.validate_request('data.refine', raw(body)), body)
        self.error('REQUEST_FIELDS_INVALID', c.validate_request, 'data.refine', raw(dict(body, unknown=True)))
        self.error('REQUEST_FIELDS_INVALID', c.validate_request, 'data.refine', raw(dict(body, ocr_deployment_id=None)))
        self.error('JSON_DUPLICATE_KEY', c.validate_request, 'data.refine', b'{"name":"a","name":"b"}')
        self.error('JSON_INVALID', c.strict_json, b'{} {}')
        self.error('JSON_NONFINITE', c.strict_json, b'{"value":NaN}')
        self.error('JSON_INVALID_UTF8', c.strict_json, b'{"value":"\\ud800"}')

    def test_build_has_one_input_mode_and_new_input_cannot_have_subset(self):
        body = {'chunking': {'name': '자료함', 'canonical_ids': [self.ids['canonical']], 'chunker': {'id': 'recursive-character', 'options': {}}}}
        self.assertEqual(c.validate_request('knowledge.build', raw(body)), body)
        self.error('BUILD_INPUT_EXCLUSIVE', c.validate_request, 'knowledge.build', raw(dict(body, chunking_run_id=self.ids['refinement'])))
        self.error('BUILD_SUBSET_INVALID', c.validate_request, 'knowledge.build', raw(dict(body, canonical_ids=[self.ids['canonical']])))
        self.error('BUILD_INPUT_EXCLUSIVE', c.validate_request, 'knowledge.build', b'{}')

    def test_activate_requires_fresh_revision_not_an_idempotency_key_field(self):
        body = {'index_version_id': self.ids['index_version'], 'expected_active_version_id': '', 'expected_revision': 3}
        self.assertEqual(c.validate_request('knowledge.activate', raw(body)), body)
        self.error('ACTIVATION_REQUEST_INVALID', c.validate_request, 'knowledge.activate', raw(dict(body, expected_revision=True)))
        self.error('REQUEST_FIELDS_INVALID', c.validate_request, 'knowledge.activate', raw(dict(body, idempotency_key=self.ids['job'])))

    def test_user_question_uses_pinned_agent_version_and_utf8_byte_limit(self):
        body = {'agent_version_id': self.ids['agent_version'], 'mode': 'run', 'input': {'question': '운영 시간이 언제인가요?'}}
        self.assertEqual(c.validate_request('agent.run', raw(body)), body)
        body['input']['question'] = '가' * 3000
        self.error('QUESTION_INVALID', c.validate_request, 'agent.run', raw(body))

    def test_upload_preacceptance_limits_and_initial_formats(self):
        self.assertEqual(c.upload_metadata('자료', '자료.PDF', 128, 'b' * 64, 19)['filename'], '자료.PDF')
        self.error('DOCUMENT_CAPACITY_EXCEEDED', c.upload_metadata, '자료', 'a.txt', 128, 'b' * 64, 20)
        self.error('UPLOAD_BYTES_INVALID', c.upload_metadata, '자료', 'a.txt', c.MAX_BYTES + 1, 'b' * 64, 0)
        self.error('UPLOAD_BYTES_INVALID', c.upload_metadata, '자료', 'a.txt', 0, 'b' * 64, 0)
        for filename in ('scan.png', '../a.txt', 'a\\b.md', 'no_extension'):
            self.error('UPLOAD_FORMAT_UNSUPPORTED', c.upload_metadata, '자료', filename, 128, 'b' * 64, 0)

    def test_malformed_state_types_cannot_advance(self):
        self.error('REFINEMENT_RESULT_INVALID', c.observe_refinement, self.refinement,
                   dict(self.f['refinement_completed'], status=[]), self.ids['source'])
        self.error('INDEX_NOT_READY', c.observe_index, self.build, dict(self.f['binding_readback']['version'], state=[]),
                   self.ids['index'], (self.document,), self.ids['embedding'], self.f['space_id'])
        request = {'agent_version_id': self.ids['agent_version'], 'mode': [], 'input': {'question': '언제?'}}
        self.error('AGENT_REQUEST_INVALID', c.validate_request, 'agent.run', raw(request))

    def test_python310_syntax_and_catalog_covers_receipt_contracts(self):
        ast.parse((FOLDER / 'contracts.py').read_text(), feature_version=(3, 10))
        catalog = json.loads((FOLDER / 'api-contract.json').read_text())
        self.assertEqual(set(catalog['receipt_validators']), set(c.OPERATIONS))
        self.assertTrue(c.KEYED_OPERATIONS <= {v['operation'] for v in catalog['operations']})
        self.assertEqual(len({v['operation'] for v in catalog['operations']}), len(catalog['operations']))
        self.assertTrue(all((FOLDER.parents[1] / p).is_file() for p in catalog['source_sha256']))


if __name__ == '__main__':
    unittest.main()
