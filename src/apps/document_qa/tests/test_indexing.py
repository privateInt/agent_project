"""Q05a temporary state, independent fake Knowledge lifecycle and real loopback HTTP."""
import copy
from contextlib import redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.apps.document_qa.contracts import ContractError, MAX_BYTES, request_intent
from src.apps.document_qa.indexing import IndexingStore, MAX_ATTEMPTS, main
from src.apps.document_qa.indexing_results import defaults_checked, snapshot_checked
from src.apps.document_qa.indexing_transport import KnowledgeIndexer
from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.uploads import UploadStore
from src.apps.document_qa.tests.test_refinement import Fixture, encoded, running

DEFAULTS = {
    'embedding': {'deployment_id': '44247297-5184-4a36-9225-7791c80d3add',
                  'model_version_id': '84a54704-1fef-4016-b820-030f5cb6df47',
                  'embedding_space_id': 'es_' + 'a' * 64, 'profile_id': 'snowflake-ko-v1',
                  'max_input_tokens': 8192, 'max_input_bytes': 131072},
    'chunker': {'id': 'structure_v1', 'version': '2026-09-21.1',
                'options': {'max_characters': 1000, 'overlap_characters': 100}},
}


class FakeKnowledge:
    origin = 'http://127.0.0.1:14500'
    writes_enabled = True
    build_enabled = True

    def __init__(self, data, uploaded):
        self.data, self.uploaded = data, uploaded
        self.calls, self.keys, self.versions = [], {}, {}
        self.index = None
        self.lose_create = self.lose_build = self.lose_activate = False
        self.interrupt_build = self.interrupt_activate = False
        self.activate_before_loss = True
        self.conflict_once = False
        self.enter = self.release = None

    def input(self, identifier):
        meta = self.data.meta[identifier]
        source = self.uploaded.sources[meta['source_document_id']]
        return {'canonical_id': identifier, 'canonical_sha256': meta['sha256'], 'size_bytes': meta['size_bytes'],
                'source_document_id': source['document_id'], 'source_sha256': source['sha256'],
                'refinement_run_id': meta['refinement_run_id'], 'schema_version': meta['schema_version'],
                'processor_version': meta['processor_version'], 'rules_version': meta['rules_version'],
                'processor_image_id': meta['processor_image_id'], 'format': source['format']}

    def get(self, path):
        self.calls.append(('GET', path, None, None))
        if path.startswith('/api/index-versions/'):
            return copy.deepcopy(self.versions[path.split('/')[-1]])
        return copy.deepcopy(self.index)

    def request(self, method, path, body=None, key=None):
        self.calls.append((method, path, copy.deepcopy(body), key))
        if path == '/api/indexes':
            if self.index is None:
                self.index = {'index_id': str(uuid4()), 'name': body['name'],
                              'embedding_deployment_id': body['embedding_deployment_id'],
                              'revision': 1, 'state': 'active', 'active_version_id': ''}
            if self.lose_create:
                raise UploadError('KNOWLEDGE_INDEXING_OUTCOME_UNKNOWN', 502)
            return 201, encoded(self.index)
        if path.endswith('/builds'):
            if key not in self.keys:
                inputs = [self.input(identifier) for identifier in sorted(body['chunking']['canonical_ids'])]
                identifier = str(uuid4())
                self.keys[key] = (copy.deepcopy(body), identifier)
                self.versions[identifier] = {
                    'schema_version': 'index-version-v1', 'index_version_id': identifier,
                    'index_id': self.index['index_id'], 'operation_id': str(uuid4()),
                    'chunking_run_id': str(uuid4()), 'parent_version_id': body.get('parent_version_id', ''),
                    'canonical_ids': [item['canonical_id'] for item in inputs],
                    'embedding_profile': copy.deepcopy(DEFAULTS['embedding']),
                    'state': 'protecting', 'reference_state': 'pending', 'error_code': '',
                    'chunk_count': 0, 'indexed_count': 0, 'chunk_set_sha256': '',
                    'input_spec': {'schema_version': 'chunking-spec-v1', 'snapshot_id': hashlib.sha256(encoded(inputs)).hexdigest(),
                                   'documents': inputs, 'chunker': {**copy.deepcopy(DEFAULTS['chunker']), 'policy_hash': 'b' * 64}},
                }
                self.index['revision'] += 1
            previous, identifier = self.keys[key]
            assert previous == body
            if self.enter:
                self.enter.set()
                assert self.release.wait(3)
            if self.interrupt_build:
                raise KeyboardInterrupt()
            if self.lose_build:
                raise UploadError('KNOWLEDGE_INDEXING_OUTCOME_UNKNOWN', 502)
            return 202, encoded(self.versions[identifier])
        assert path.endswith('/activate') and key is None
        if self.conflict_once:
            self.index['revision'] += 1
            self.conflict_once = False
        if self.index['revision'] != body['expected_revision'] or self.index['active_version_id'] != body['expected_active_version_id']:
            raise UploadError('INDEXING_ACTIVATION_CONFLICT', 502)
        if self.activate_before_loss:
            self.index.update(active_version_id=body['index_version_id'], revision=self.index['revision'] + 1)
        if self.interrupt_activate:
            raise KeyboardInterrupt()
        if self.lose_activate:
            raise UploadError('KNOWLEDGE_INDEXING_OUTCOME_UNKNOWN', 502)
        return 200, encoded(self.index)


class IndexFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.indexing = IndexingStore.initialize(self.uploads)
        self.knowledge = FakeKnowledge(self.api, self.uploaded)

    def canonical_job(self, name='문서 A', data=b'document A'):
        job = self.job(name, data=data)
        result = self.refinement.dispatch(job['job_id'], self.api)
        run = self.api.runs[result['receipt']['resource_id']]
        run.update(status='succeeded', phase='completed')
        result = self.refinement.observe(job['job_id'], self.api)
        self.assertEqual(result['state'], 'canonical_verified')
        return job

    def configure(self):
        result = self.indexing.configure(self.knowledge, DEFAULTS)
        self.assertIsNone(result['error_code'])
        return result

    def ready_job(self, name='문서 A', data=b'document A'):
        self.configure()
        job = self.canonical_job(name, data)
        result = self.indexing.dispatch(job['job_id'], self.knowledge)
        self.knowledge.versions[result['index_version_id']].update(
            state='ready', reference_state='held', chunk_count=4, indexed_count=4, chunk_set_sha256='c' * 64)
        self.assertEqual(self.indexing.observe(job['job_id'], self.knowledge)['state'], 'index_ready')
        return job

    def kb_posts(self, suffix=None):
        return [call for call in self.knowledge.calls if call[0] == 'POST' and (suffix is None or call[1].endswith(suffix))]


class IndexStorageTests(IndexFixture):
    def test_init_is_additive_and_status_does_not_change_existing_library_jobs_or_token(self):
        job = self.canonical_job()
        before = self.uploads.library(), self.uploads.jobs(), self.uploads.token_path.read_bytes(), self.refinement.job(job['job_id'])
        IndexingStore.initialize(UploadStore(self.root))
        self.assertEqual(before, (self.uploads.library(), self.uploads.jobs(), self.uploads.token_path.read_bytes(), self.refinement.job(job['job_id'])))
        self.assertEqual(self.knowledge.calls, [])
        self.assertFalse(self.indexing.status()['question_ready'])
        self.assertFalse(self.indexing.status()['production_indexing_verified'])
        with self.uploads.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_cli_init_status_and_plan_are_local_only(self):
        job = self.canonical_job()
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('no HTTP')):
            for action in ('init', 'status', 'plan'):
                stream = io.StringIO()
                args = [action, '--state-dir', str(self.root)]
                if action == 'plan':
                    args += ['--job-id', job['job_id']]
                with redirect_stdout(stream):
                    self.assertEqual(main(args), 0)
                result = json.loads(stream.getvalue())
                self.assertEqual(result['production_api_requests'], 0)
                self.assertFalse(result['inference_requested'])
                self.assertFalse(result['GPU_requested'])
        self.assertEqual(self.knowledge.calls, [])
        self.assertIsNone(self.indexing.binding())

    def test_init_and_plan_preserve_upload_and_refinement_receipts(self):
        job = self.canonical_job()
        before = self.uploads.job(job['job_id']), self.refinement.job(job['job_id'])
        calls = list(self.api.calls)
        result = self.indexing.plan(job['job_id'])
        self.assertEqual(result['document_count'], 1)
        self.assertEqual(before, (self.uploads.job(job['job_id']), self.refinement.job(job['job_id'])))
        self.assertEqual(self.api.calls, calls)

    def test_unverified_upload_or_refinement_cannot_plan_or_build(self):
        self.configure()
        job = self.job()
        before = len(self.knowledge.calls)
        for function in (lambda: self.indexing.plan(job['job_id']),
                         lambda: self.indexing.dispatch(job['job_id'], self.knowledge)):
            with self.assertRaises(UploadError):
                function()
        self.assertEqual(len(self.knowledge.calls), before)
        self.assertIsNone(self.indexing.row(job['job_id']))

    def test_missing_defaults_and_disabled_writes_refuse_before_any_request(self):
        with self.assertRaises(UploadError):
            self.indexing.configure(self.knowledge, {})
        self.knowledge.writes_enabled = False
        with self.assertRaises(UploadError):
            self.indexing.configure(self.knowledge, DEFAULTS)
        self.assertEqual(self.knowledge.calls, [])
        self.assertIsNone(self.indexing.binding())

    def test_build_requires_inference_opt_in_even_with_canonical_and_binding(self):
        self.configure()
        job = self.canonical_job()
        self.knowledge.build_enabled = False
        count = len(self.knowledge.calls)
        with self.assertRaises(UploadError) as error:
            self.indexing.dispatch(job['job_id'], self.knowledge)
        self.assertEqual(error.exception.code, 'INDEXING_INFERENCE_NOT_AUTHORIZED')
        self.assertEqual(len(self.knowledge.calls), count)
        self.assertIsNone(self.indexing.row(job['job_id']))

    def test_default_chunker_and_profile_invalid_shapes_or_limits_are_refused(self):
        for changes in (
            {'embedding': {**DEFAULTS['embedding'], 'max_input_tokens': True}},
            {'embedding': {**DEFAULTS['embedding'], 'embedding_space_id': 'bad'}},
            {'chunker': {**DEFAULTS['chunker'], 'id': 'unknown'}},
            {'chunker': {**DEFAULTS['chunker'], 'options': {'max_characters': 1000, 'overlap_characters': 500}}},
            {'embedding': {**DEFAULTS['embedding'], 'max_input_bytes': 3999}},
        ):
            with self.subTest(changes=changes), self.assertRaises(UploadError):
                defaults_checked({**DEFAULTS, **changes})

    def test_create_outcome_unknown_only_explicit_replay_uses_same_key_and_body(self):
        self.knowledge.lose_create = True
        self.assertIsNone(self.indexing.configure(self.knowledge, DEFAULTS)['index_id'])
        reopened = IndexingStore(UploadStore(self.root))
        reopened.configure(self.knowledge, DEFAULTS)
        self.assertEqual(len(self.kb_posts()), 1)
        self.knowledge.lose_create = False
        result = reopened.configure(self.knowledge, DEFAULTS, retry=True)
        self.assertEqual(result['index_id'], self.knowledge.index['index_id'])
        self.assertEqual(self.kb_posts()[0][2:], self.kb_posts()[1][2:])

    def test_configuration_or_origin_change_does_not_recreate_an_index(self):
        self.configure()
        count = len(self.knowledge.calls)
        changed = copy.deepcopy(DEFAULTS)
        changed['chunker']['options']['max_characters'] = 1500
        with self.assertRaises(UploadError):
            self.indexing.configure(self.knowledge, changed, retry=True)
        self.knowledge.origin = 'http://127.0.0.1:14501'
        with self.assertRaises(UploadError):
            self.indexing.configure(self.knowledge, DEFAULTS)
        self.assertEqual(len(self.knowledge.calls), count)

    def test_completed_generation_keeps_old_document_when_second_is_added(self):
        first = self.ready_job()
        self.indexing.activate(first['job_id'], self.knowledge)
        old_head = self.indexing.head()
        second = self.canonical_job('문서 B', b'document B')
        result = self.indexing.dispatch(second['job_id'], self.knowledge)
        self.assertEqual(result['document_count'], 2)
        self.assertEqual(self.indexing.head()['documents'], old_head['documents'])
        body = self.kb_posts('/builds')[-1][2]
        self.assertEqual(len(body['chunking']['canonical_ids']), 2)
        self.assertEqual(body['parent_version_id'], old_head['version_id'])
        self.knowledge.versions[result['index_version_id']].update(
            state='ready', reference_state='held', chunk_count=8, indexed_count=8, chunk_set_sha256='c' * 64)
        self.indexing.observe(second['job_id'], self.knowledge)
        self.indexing.activate(second['job_id'], self.knowledge)
        self.assertEqual(self.indexing.head()['generation'], 2)
        self.assertEqual(len(self.indexing.head()['documents']), 2)
        self.assertEqual(self.uploads.library()['question_revision'], 0)
        self.assertFalse(self.indexing.job(second['job_id'])['question_ready'])

    def test_snapshot_duplicate_and_combined_bytes_are_bounded(self):
        item = self.indexing.plan(self.canonical_job()['job_id'])['documents'][0]
        with self.assertRaises(ContractError):
            snapshot_checked([item, item])
        other = {**item, 'canonical_id': str(uuid4()), 'source_document_id': str(uuid4())}
        with self.assertRaises(ContractError):
            snapshot_checked([{**item, 'size_bytes': MAX_BYTES}, other])
        with self.assertRaises(ContractError):
            snapshot_checked([])


class IndexLifecycleTests(IndexFixture):
    def test_202_is_waiting_not_index_ready_or_question_ready(self):
        self.configure()
        job = self.canonical_job()
        result = self.indexing.dispatch(job['job_id'], self.knowledge)
        self.assertEqual(result['state'], 'waiting')
        self.assertFalse(result['indexing_ready'])
        self.assertFalse(result['question_ready'])
        self.assertEqual(self.indexing.observe(job['job_id'], self.knowledge)['state'], 'waiting')

    def test_lost_build_response_and_reopen_do_not_automatically_repost(self):
        self.configure()
        job = self.canonical_job()
        self.knowledge.lose_build = True
        result = self.indexing.dispatch(job['job_id'], self.knowledge)
        self.assertEqual(result['state'], 'confirmation_required')
        reopened = IndexingStore(UploadStore(self.root))
        reopened.dispatch(job['job_id'], self.knowledge)
        reopened.observe(job['job_id'], self.knowledge)
        self.assertEqual(len(self.kb_posts('/builds')), 1)
        self.knowledge.lose_build = False
        result = reopened.dispatch(job['job_id'], self.knowledge, retry=True)
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(self.kb_posts('/builds')[0][2:], self.kb_posts('/builds')[1][2:])
        self.assertEqual(len(self.knowledge.versions), 1)

    def test_interruption_retains_build_intent_before_POST_and_retry_reuses_it(self):
        self.configure()
        job = self.canonical_job()
        self.knowledge.interrupt_build = True
        with self.assertRaises(KeyboardInterrupt):
            self.indexing.dispatch(job['job_id'], self.knowledge)
        reopened = IndexingStore(UploadStore(self.root))
        self.assertEqual(reopened.job(job['job_id'])['state'], 'confirmation_required')
        reopened.dispatch(job['job_id'], self.knowledge)
        self.assertEqual(len(self.kb_posts('/builds')), 1)
        self.knowledge.interrupt_build = False
        reopened.dispatch(job['job_id'], self.knowledge, retry=True)
        self.assertEqual(len(self.knowledge.versions), 1)

    def test_known_build_receipt_never_reposts_even_with_retry(self):
        job = self.ready_job()
        count = len(self.knowledge.calls)
        self.indexing.dispatch(job['job_id'], self.knowledge, retry=True)
        self.assertEqual(len(self.knowledge.calls), count)

    def test_foreign_active_version_blocks_first_build_before_POST(self):
        self.configure()
        job = self.canonical_job()
        self.knowledge.index['active_version_id'] = str(uuid4())
        with self.assertRaises(UploadError):
            self.indexing.dispatch(job['job_id'], self.knowledge)
        self.assertEqual(self.kb_posts('/builds'), [])

    def test_another_job_cannot_build_until_current_generation_finishes(self):
        first = self.ready_job()
        second = self.canonical_job('문서 B', b'document B')
        count = len(self.knowledge.calls)
        with self.assertRaises(UploadError) as error:
            self.indexing.dispatch(second['job_id'], self.knowledge)
        self.assertEqual(error.exception.code, 'LIBRARY_INDEXING_BUSY')
        self.assertEqual(len(self.knowledge.calls), count)
        self.assertEqual(self.indexing.head()['active_job_id'], first['job_id'])

    def test_concurrent_job_is_refused_while_first_POST_holds_lane(self):
        self.configure()
        first = self.canonical_job()
        second = self.canonical_job('문서 B', b'document B')
        self.knowledge.enter, self.knowledge.release = threading.Event(), threading.Event()
        failures = []
        def dispatch():
            try:
                self.indexing.dispatch(first['job_id'], self.knowledge)
            except Exception as exc:
                failures.append(exc)
        worker = threading.Thread(target=dispatch)
        worker.start()
        try:
            self.assertTrue(self.knowledge.enter.wait(2))
            with self.assertRaises(UploadError) as error:
                self.indexing.dispatch(second['job_id'], self.knowledge)
            self.assertEqual(error.exception.code, 'INDEXING_BUSY')
        finally:
            self.knowledge.release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(self.kb_posts('/builds')), 1)

    def test_failed_build_releases_lane_and_preserves_old_active_documents(self):
        first = self.ready_job()
        self.indexing.activate(first['job_id'], self.knowledge)
        head = copy.deepcopy(self.indexing.head())
        second = self.canonical_job('문서 B', b'document B')
        result = self.indexing.dispatch(second['job_id'], self.knowledge)
        self.knowledge.versions[result['index_version_id']].update(state='failed', error_code='MODEL_NOT_READY')
        self.assertEqual(self.indexing.observe(second['job_id'], self.knowledge)['state'], 'failed')
        self.assertEqual(self.indexing.head(), head)
        self.assertEqual(self.indexing.observe(second['job_id'], self.knowledge)['state'], 'failed')
        self.assertEqual(self.indexing.dispatch(second['job_id'], self.knowledge, retry=True)['state'], 'failed')

    def test_build_retry_with_foreign_active_version_does_not_repost(self):
        self.configure()
        job = self.canonical_job()
        self.knowledge.lose_build = True
        self.indexing.dispatch(job['job_id'], self.knowledge)
        self.knowledge.index['active_version_id'] = str(uuid4())
        with self.assertRaises(UploadError):
            self.indexing.dispatch(job['job_id'], self.knowledge, retry=True)
        self.assertEqual(len(self.kb_posts('/builds')), 1)


class IndexReadbackTests(IndexFixture):
    def test_identity_counts_inputs_model_and_chunker_mismatches_cannot_activate(self):
        job = self.ready_job()
        identifier = self.indexing.job(job['job_id'])['index_version_id']
        original = copy.deepcopy(self.knowledge.versions[identifier])
        patches = (
            {'operation_id': str(uuid4())}, {'indexed_count': 3}, {'indexed_count': True},
            {'reference_state': 'pending'}, {'canonical_ids': []}, {'chunk_set_sha256': ''},
            {'parent_version_id': str(uuid4())}, {'chunking_run_id': str(uuid4())},
            {'embedding_profile': {**original['embedding_profile'], 'model_version_id': str(uuid4())}},
            {'input_spec': {**original['input_spec'], 'documents': []}},
            {'input_spec': {**original['input_spec'], 'documents': [{**original['input_spec']['documents'][0], 'canonical_sha256': 'e' * 64}]}},
            {'input_spec': {**original['input_spec'], 'documents': [{**original['input_spec']['documents'][0], 'size_bytes': float(original['input_spec']['documents'][0]['size_bytes'])}]}},
            {'input_spec': {**original['input_spec'], 'chunker': {**original['input_spec']['chunker'], 'version': 'old-version'}}},
            {'input_spec': {**original['input_spec'], 'chunker': {**original['input_spec']['chunker'], 'options': {'max_characters': 1000.0, 'overlap_characters': 100}}}},
        )
        for changes in patches:
            with self.subTest(changes=changes):
                self.knowledge.versions[identifier] = {**copy.deepcopy(original), **changes}
                result = self.indexing.activate(job['job_id'], self.knowledge)
                self.assertEqual(result['state'], 'attention_required')
                self.assertEqual(self.kb_posts('/activate'), [])
                self.assertEqual(self.indexing.head()['generation'], 0)

    def test_activation_uses_fresh_revision_after_build_and_never_question_ready(self):
        job = self.ready_job()
        self.assertEqual(self.knowledge.index['revision'], 2)
        result = self.indexing.activate(job['job_id'], self.knowledge)
        self.assertEqual(result['state'], 'activated')
        self.assertEqual(self.kb_posts('/activate')[0][2]['expected_revision'], 2)
        self.assertEqual(self.knowledge.index['revision'], 3)
        self.assertFalse(result['question_ready'])
        count = len(self.knowledge.calls)
        self.indexing.activate(job['job_id'], self.knowledge, retry=True)
        self.indexing.observe(job['job_id'], self.knowledge)
        self.assertEqual(len(self.knowledge.calls), count)

    def test_lost_activation_reply_is_confirmed_by_GET_without_second_POST(self):
        job = self.ready_job()
        self.knowledge.lose_activate = True
        result = self.indexing.activate(job['job_id'], self.knowledge)
        self.assertEqual(result['state'], 'activated')
        self.assertEqual(len(self.kb_posts('/activate')), 1)

    def test_interruption_after_remote_activation_recovers_by_GET_on_reopen(self):
        job = self.ready_job()
        self.knowledge.interrupt_activate = True
        with self.assertRaises(KeyboardInterrupt):
            self.indexing.activate(job['job_id'], self.knowledge)
        reopened = IndexingStore(UploadStore(self.root))
        self.assertEqual(reopened.observe(job['job_id'], self.knowledge)['state'], 'activated')
        self.assertEqual(len(self.kb_posts('/activate')), 1)

    def test_unknown_activation_that_did_not_commit_never_automatically_reposts(self):
        job = self.ready_job()
        self.knowledge.activate_before_loss = False
        self.knowledge.lose_activate = True
        result = self.indexing.activate(job['job_id'], self.knowledge)
        self.assertEqual(result['state'], 'activation_confirmation_required')
        self.indexing.activate(job['job_id'], self.knowledge)
        self.indexing.observe(job['job_id'], self.knowledge)
        self.assertEqual(len(self.kb_posts('/activate')), 1)
        self.assertEqual(self.indexing.head()['generation'], 0)
        self.knowledge.activate_before_loss = True
        self.knowledge.lose_activate = False
        result = self.indexing.activate(job['job_id'], self.knowledge, retry=True)
        self.assertEqual(result['state'], 'activated')
        self.assertEqual(len(self.kb_posts('/activate')), 2)
        self.assertEqual(len(json.loads(self.indexing.row(job['job_id'])['activation_history'])), 2)

    def test_revision_race_is_not_forced_and_requires_explicit_retry(self):
        job = self.ready_job()
        self.knowledge.conflict_once = True
        self.assertEqual(self.indexing.activate(job['job_id'], self.knowledge)['state'], 'activation_confirmation_required')
        self.indexing.activate(job['job_id'], self.knowledge)
        self.assertEqual(len(self.kb_posts('/activate')), 1)
        self.assertEqual(self.indexing.activate(job['job_id'], self.knowledge, retry=True)['state'], 'activated')

    def test_foreign_activation_is_not_overwritten(self):
        job = self.ready_job()
        foreign = str(uuid4())
        self.knowledge.index['active_version_id'] = foreign
        result = self.indexing.activate(job['job_id'], self.knowledge)
        self.assertEqual(result['state'], 'attention_required')
        self.assertEqual(self.knowledge.index['active_version_id'], foreign)
        self.assertEqual(self.kb_posts('/activate'), [])

    def test_changed_origin_and_local_head_refuse_before_requests(self):
        job = self.ready_job()
        count = len(self.knowledge.calls)
        self.knowledge.origin = 'http://127.0.0.1:14501'
        with self.assertRaises(UploadError):
            self.indexing.observe(job['job_id'], self.knowledge)
        self.knowledge.origin = 'http://127.0.0.1:14500'
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE indexing_head SET generation=9 WHERE singleton=1')
        with self.assertRaises(UploadError):
            self.indexing.activate(job['job_id'], self.knowledge)
        self.assertEqual(len(self.knowledge.calls), count)

    def test_saved_request_must_match_snapshot_even_if_its_hash_is_recomputed(self):
        job = self.ready_job()
        row = self.indexing.row(job['job_id'])
        body = json.loads(row['request_body'])
        body['chunking']['canonical_ids'] = [str(uuid4())]
        intent = request_intent(job['job_id'], 'knowledge.build', row['base_generation'] + 1, body)
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE indexing_tasks SET request_body=?,request_sha256=? WHERE job_id=?',
                       (json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False), intent.request_sha256, job['job_id']))
        count = len(self.knowledge.calls)
        with self.assertRaises(UploadError) as error:
            self.indexing.activate(job['job_id'], self.knowledge)
        self.assertEqual(error.exception.code, 'INDEXING_INTENT_CHANGED')
        self.assertEqual(len(self.knowledge.calls), count)


class IndexTransportTests(unittest.TestCase):
    def test_writes_and_build_need_separate_opt_ins_and_disallowed_paths_make_no_requests(self):
        api = KnowledgeIndexer({'url': 'http://127.0.0.1:9'})
        identifier = str(uuid4())
        body = {'name': 'document-qa-' + identifier, 'embedding_deployment_id': str(uuid4())}
        with self.assertRaises(UploadError):
            api.request('POST', '/api/indexes', body, str(uuid4()))
        for path in ('/api/indexes', '/api/search', '/api/deployments/' + identifier,
                     '/api/indexes/' + identifier + '?private=secret', '/api/indexes/' + identifier + '/content'):
            with self.subTest(path=path), self.assertRaises(UploadError):
                api.get(path)
        api.writes_enabled = True
        with self.assertRaises(UploadError) as error:
            api.request('POST', '/api/indexes/' + identifier + '/builds', {}, str(uuid4()))
        self.assertEqual(error.exception.code, 'INDEXING_INFERENCE_NOT_AUTHORIZED')
        self.assertEqual(api.calls, 0)

    def serve(self, status=200, payload=None, content_type='application/json', headers=None):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply()
            def do_POST(self):
                self.reply()
            def reply(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                calls.append((self.command, self.path, dict(self.headers), raw))
                content = payload if isinstance(payload, bytes) else encoded(payload or {'ok': True})
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(content)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *args):
                pass
        return ThreadingHTTPServer(('127.0.0.1', 0), Handler), calls

    def test_real_HTTP_GET_has_no_JSON_content_type_and_ignores_proxy(self):
        server, calls = self.serve()
        with running(server) as port, patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}):
            api = KnowledgeIndexer({'url': 'http://127.0.0.1:' + str(port)})
            self.assertTrue(api.get('/api/indexes/' + str(uuid4()))['ok'])
        self.assertNotIn('Content-Type', calls[0][2])
        self.assertEqual(api.calls, 1)

    def test_real_create_POST_has_stable_JSON_and_key(self):
        server, calls = self.serve(status=201)
        body = {'name': 'document-qa-' + str(uuid4()), 'embedding_deployment_id': str(uuid4())}
        key = str(uuid4())
        with running(server) as port:
            api = KnowledgeIndexer({'url': 'http://127.0.0.1:' + str(port)}, execute=True)
            self.assertEqual(api.request('POST', '/api/indexes', body, key)[0], 201)
        self.assertEqual(json.loads(calls[0][3]), body)
        self.assertEqual(calls[0][2]['Idempotency-Key'], key)

    def test_redirect_and_raw_backend_errors_are_not_followed_or_exposed(self):
        for status in (302, 503):
            server, calls = self.serve(status=status, payload={'error_message': 'PRIVATE-CREDENTIAL'},
                                       headers={'Location': 'http://127.0.0.1:1/private'})
            with running(server) as port:
                api = KnowledgeIndexer({'url': 'http://127.0.0.1:' + str(port)})
                with self.assertRaises(UploadError) as error:
                    api.get('/api/indexes/' + str(uuid4()))
                self.assertNotIn('PRIVATE-CREDENTIAL', str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_duplicate_JSON_oversize_and_wrong_content_type_are_refused(self):
        for payload, mime in ((b'{"x":1,"x":2}', 'application/json'), (b' ' * 65537, 'application/json'),
                              (b'{}', 'text/html')):
            server, calls = self.serve(payload=payload, content_type=mime)
            with running(server) as port:
                api = KnowledgeIndexer({'url': 'http://127.0.0.1:' + str(port)})
                with self.assertRaises(UploadError):
                    api.get('/api/indexes/' + str(uuid4()))
                self.assertEqual(len(calls), 1)

    def test_request_budget_refuses_before_network(self):
        api = KnowledgeIndexer({'url': 'http://127.0.0.1:9'}, clock=lambda: 1, budget_seconds=0)
        with self.assertRaises(UploadError):
            api.get('/api/indexes/' + str(uuid4()))
        self.assertEqual(api.calls, 0)


if __name__ == '__main__':
    unittest.main()
