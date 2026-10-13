"""Independent Agent lifecycle fake, persisted QA state and actual loopback HTTP."""
import copy
from contextlib import redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.apps.document_qa import readiness
from src.apps.document_qa.contracts import ContractError
from src.apps.document_qa.indexing import IndexingStore
from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.search_tool import SearchToolStore, MAX_ATTEMPTS, main
from src.apps.document_qa.search_tool_results import arguments_sha256, selection_checked
from src.apps.document_qa.search_tool_transport import AgentToolGateway, MAX_RESPONSE
from src.apps.document_qa.uploads import UploadStore
from src.apps.document_qa.tests.test_indexing import IndexFixture, DEFAULTS
from src.apps.document_qa.tests.test_refinement import encoded, running

INPUT = {'type': 'object', 'required': ['query'], 'additionalProperties': False,
         'properties': {'query': {'type': 'string', 'minLength': 1}}}
OUTPUT = {'type': 'object', 'required': ['index_id', 'index_version_id', 'embedding_space_id', 'returned_count', 'results']}


class FakeAgent:
    origin = 'http://127.0.0.1:15000'
    writes_enabled = True
    inference_enabled = True

    def __init__(self, candidate, selection):
        self.candidate, self.selection = copy.deepcopy(candidate), copy.deepcopy(selection)
        self.calls, self.resources, self.idempotency = [], {}, {}
        self.lose, self.interrupt, self.get_errors = set(), set(), set()
        self.enter = self.release = None
        for key in ('knowledge_connection', 'model_connection'):
            row = selection[key]
            self.resources['/api/connection-versions/' + row['version_id']] = {
                'connection_version_id': row['version_id'], 'connection_id': str(uuid4()), 'version': 1,
                'base_url': row['base_url'], 'auth_type': 'bearer', 'credential_ref': key,
                'credential_revision': 'v1', 'created_at': '2026-10-08T00:00:00Z'}

    def get(self, path):
        self.calls.append(('GET', path, None, None))
        if path in self.get_errors:
            raise UploadError('AGENT_TOOL_OUTCOME_UNKNOWN', 502)
        return copy.deepcopy(self.resources[path])

    def request(self, method, path, body=None, key=None):
        assert method == 'POST'
        self.calls.append((method, path, copy.deepcopy(body), key))
        if path == '/api/knowledge/inspect':
            profile = self.candidate['config']['embedding']
            return 200, encoded({'index_name': 'document-qa-' + self.candidate['library_id'], 'chunk_count': 4,
                 'binding': {'connection_version_id': body['connection_version_id'], 'knowledge': {
                 'model_connection_version_id': body['model_connection_version_id'],
                 'index_id': self.candidate['index_id'], 'index_version_id': self.candidate['index_version_id'],
                 'embedding_deployment_id': profile['deployment_id'], 'embedding_space_id': profile['embedding_space_id'],
                 'top_k': 5, 'candidate_k': 5}}})
        if (path, key) in self.idempotency:
            previous, status, response = self.idempotency[(path, key)]
            if previous != body:
                raise UploadError('AGENT_TOOL_CONFLICT', 502)
        elif path == '/api/tools':
            identifier = str(uuid4())
            response = {'tool_id': identifier, **body, 'enabled': True, 'revision': 1}
            self.resources['/api/tools/' + identifier] = copy.deepcopy(response)
            status = 201
        elif path.endswith('/versions'):
            tool = self.resources['/api/tools/' + path.split('/')[3]]
            if tool['revision'] != body['expected_revision']:
                raise UploadError('AGENT_TOOL_CONFLICT', 502)
            identifier = str(uuid4())
            number = 1 + sum(1 for p, v in self.resources.items() if p.startswith('/api/tool-versions/') and v['tool_id'] == tool['tool_id'])
            response = {'tool_version_id': identifier, 'tool_id': tool['tool_id'], 'version': number,
                        'revision': 1, 'state': 'draft', 'kind': body['kind'], 'binding': copy.deepcopy(body['binding']),
                        'timeout_ms': body['timeout_ms'], 'definition_sha256': hashlib.sha256(identifier.encode()).hexdigest(),
                        'input_schema': copy.deepcopy(INPUT), 'output_schema': copy.deepcopy(OUTPUT)}
            self.resources['/api/tool-versions/' + identifier] = copy.deepcopy(response)
            tool['revision'] += 1; status = 201
        elif path == '/api/tool-calls':
            version = self.resources['/api/tool-versions/' + body['tool_version_id']]
            assert version['state'] == 'draft' and body['mode'] == 'test'
            identifier = str(uuid4())
            response = {'tool_call_id': identifier, 'tool_version_id': version['tool_version_id'],
                        'state': 'accepted', 'status_url': '/api/tool-calls/' + identifier}
            self.resources[response['status_url']] = {**response, 'state': 'accepted', 'mode': 'test',
                'invocation_key': key, 'input_sha256': hashlib.sha256(json.dumps(body['arguments'], ensure_ascii=False,
                    sort_keys=True, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
                    .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029').encode()).hexdigest(),
                'definition_sha256': version['definition_sha256']}
            status = 202
        else:
            assert path.endswith('/publish')
            version = self.resources['/api/tool-versions/' + path.split('/')[3]]
            if body['expected_revision'] != version['revision']:
                raise UploadError('AGENT_TOOL_CONFLICT', 502)
            assert any(v.get('state') == 'succeeded' and v.get('tool_version_id') == version['tool_version_id']
                       for p, v in self.resources.items() if p.startswith('/api/tool-calls/'))
            version.update(state='published', revision=version['revision'] + 1)
            response, status = copy.deepcopy(version), 200
        self.idempotency.setdefault((path, key), (copy.deepcopy(body), status, copy.deepcopy(response)))
        if self.enter and path == '/api/tools':
            self.enter.set(); self.release.wait(3)
        if path in self.interrupt:
            raise KeyboardInterrupt()
        if path in self.lose:
            raise UploadError('AGENT_TOOL_OUTCOME_UNKNOWN', 502)
        return status, encoded(response)

    def finish(self, state='succeeded', empty=False):
        call = next(v for p, v in self.resources.items() if p.startswith('/api/tool-calls/'))
        call['state'] = state
        if state != 'succeeded':
            call['error'] = {'code': 'TOOL_TIMEOUT'}; return call
        version = self.resources['/api/tool-versions/' + call['tool_version_id']]
        knowledge = version['binding']['knowledge']
        item = self.candidate['documents'][0]
        hits = [] if empty else [{'chunk_id': 'd' * 64, 'source_document_id': item['source_document_id'],
                 'canonical_id': item['canonical_id'], 'kind': 'text', 'text': '자료의 실제 근거',
                 'rank': 1, 'retrieval_rank': 1, 'retrieval_score': 0.8,
                 'source_spans': [{'block_id': 'block-1'}], 'quality': {'state': 'ready'}}]
        call['result'] = {'completion': 'completed', 'data': {
            'index_id': knowledge['index_id'], 'index_version_id': knowledge['index_version_id'],
            'embedding_space_id': knowledge['embedding_space_id'], 'returned_count': len(hits), 'results': hits,
            'top_k': knowledge['top_k'], 'candidate_k': knowledge['candidate_k'], 'candidate_count': len(hits),
            'filters': {}, 'rerank': {'requested': False, 'applied': False, 'status': 'disabled'}},
            'metadata': {'adapter': 'knowledge_search_v1', 'index_version_id': knowledge['index_version_id'],
            'connection_version_id': version['binding']['connection_version_id'],
            'model_connection_version_id': knowledge['model_connection_version_id'],
            'embedding_profile': {**copy.deepcopy(DEFAULTS['embedding']), 'capability': 'embedding',
                  'status': 'ready', 'dimension': 1024, 'normalization': 'l2', 'truncation': 'reject'}}}
        return call

    def posts(self, suffix=None):
        return [c for c in self.calls if c[0] == 'POST' and c[1] != '/api/knowledge/inspect'
                and (suffix is None or c[1].endswith(suffix))]


class SearchFixture(IndexFixture):
    CREATE_PLAN = True
    def setUp(self):
        super().setUp()
        self.first = self.ready_job()
        self.indexing.activate(self.first['job_id'], self.knowledge)
        self.tools = SearchToolStore.initialize(self.uploads)
        self.selection = {'agent_origin': FakeAgent.origin,
            'knowledge_connection': {'version_id': str(uuid4()), 'base_url': 'http://knowledge-gateway:8000'},
            'model_connection': {'version_id': str(uuid4()), 'base_url': 'http://model-gateway:8000'},
            'top_k': 5, 'candidate_k': 10, 'timeout_ms': 180000}
        self.agent = FakeAgent(self.tools.candidate(), self.selection)
        if self.CREATE_PLAN:
            self.plan_id = self.tools.plan(self.selection)['plan_id']

    def draft(self):
        result = self.tools.prepare(self.plan_id, self.agent)
        self.assertEqual(result['state'], 'verified')
        return self.agent.resources['/api/tool-versions/' + result['tool_version_id']]

    def successful(self, query='자료의 결론은?'):
        self.draft()
        self.tools.test(self.plan_id, self.agent, query)
        call = self.agent.finish()
        self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'verified')
        return call


class StorageTests(SearchFixture):
    def test_additive_init_and_reopen_preserve_existing_tables_jobs_keys_and_head(self):
        before = self.uploads.library(), self.uploads.jobs(), self.indexing.head(), self.indexing.row(self.first['job_id'])
        keys = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file() and ('key' in p.name or p.name == 'api-token')}
        SearchToolStore.initialize(UploadStore(self.root))
        self.assertEqual(before, (self.uploads.library(), self.uploads.jobs(), self.indexing.head(), self.indexing.row(self.first['job_id'])))
        self.assertEqual(keys, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file() and ('key' in p.name or p.name == 'api-token')})
        with self.uploads.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertEqual(SearchToolStore(UploadStore(self.root)).status()['plan_count'], 1)
        self.assertEqual(self.agent.calls, [])

    def test_cli_init_and_status_are_local_without_credentials_network_or_GPU(self):
        for action in ('init', 'status', 'candidate'):
            out = io.StringIO()
            with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')), \
                 redirect_stdout(out):
                self.assertEqual(main([action, '--state', str(self.root)]), 0)
            result = json.loads(out.getvalue())
            self.assertEqual(result['production_api_requests'], 0)
            self.assertFalse(result['GPU_requested']); self.assertFalse(result['question_ready'])

    def test_paused_index_is_not_a_candidate_and_no_plan_is_created(self):
        with self.uploads.connect(write=True) as db:
            db.execute("UPDATE indexing_head SET generation=0,version_id='',documents='[]'")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(['candidate', '--state', str(self.root)]), 2)
        self.assertEqual(json.loads(out.getvalue())['code'], 'SEARCH_TOOL_ACTIVE_INDEX_REQUIRED')
        self.assertEqual(self.tools.status()['plan_count'], 1)

    def test_same_plan_reuses_identity_and_changed_selection_conflicts(self):
        self.assertEqual(self.tools.plan(copy.deepcopy(self.selection))['plan_id'], self.plan_id)
        changed = {**self.selection, 'top_k': 4}
        with self.assertRaises(UploadError) as error:
            self.tools.plan(changed)
        self.assertEqual(error.exception.code, 'SEARCH_TOOL_PLAN_CONFLICT')
        self.assertEqual(self.agent.calls, [])

    def test_selection_rejects_unbounded_or_unpinned_values(self):
        for update in ({'top_k': True}, {'candidate_k': 2}, {'timeout_ms': 240001},
                       {'agent_origin': 'http://host/path'}, {'knowledge_connection': {'version_id': 'active', 'base_url': 'http://host'}}):
            with self.subTest(update=update), self.assertRaises((UploadError, readiness.ReadinessError, ContractError)):
                selection_checked({**self.selection, **update})

    def test_missing_init_never_creates_a_replacement_upload_store(self):
        unknown = self.root / 'absent'
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['init', '--state', str(unknown)]), 2)
        self.assertFalse(unknown.exists())

    def test_corrupted_intent_blocks_requests(self):
        self.draft(); count = len(self.agent.calls)
        with self.uploads.connect(write=True) as db:
            db.execute("UPDATE search_tool_steps SET request_key=? WHERE operation='agent.tool.version'", (str(uuid4()),))
        with self.assertRaises(UploadError):
            self.tools.observe(self.plan_id, self.agent)
        self.assertEqual(len(self.agent.calls), count)

    def test_new_index_generation_reuses_tool_but_creates_pinned_new_version(self):
        first = self.draft(); original = copy.deepcopy(first)
        second = self.canonical_job('문서 B', b'document B')
        indexed = self.indexing.dispatch(second['job_id'], self.knowledge)
        self.knowledge.versions[indexed['index_version_id']].update(state='ready', reference_state='held',
             chunk_count=8, indexed_count=8, chunk_set_sha256='e' * 64)
        self.indexing.observe(second['job_id'], self.knowledge); self.indexing.activate(second['job_id'], self.knowledge)
        self.agent.candidate = self.tools.candidate()
        second_plan = self.tools.plan(self.selection)['plan_id']
        new = self.tools.prepare(second_plan, self.agent)
        self.assertEqual(new['tool_id'], first['tool_id'])
        self.assertNotEqual(new['tool_version_id'], first['tool_version_id'])
        self.assertEqual(len(self.agent.posts('/api/tools')), 1)
        self.assertEqual(self.agent.resources['/api/tool-versions/' + first['tool_version_id']], original)
        self.assertEqual(self.uploads.library()['question_revision'], 0)


class PrepareTests(SearchFixture):
    def test_draft_binding_pins_index_embedding_and_both_connection_versions(self):
        version = self.draft(); b = version['binding']; c = self.tools.candidate()
        self.assertEqual(b['connection_version_id'], self.selection['knowledge_connection']['version_id'])
        self.assertEqual(b['knowledge']['model_connection_version_id'], self.selection['model_connection']['version_id'])
        self.assertEqual(b['knowledge']['index_version_id'], c['index_version_id'])
        self.assertEqual(b['knowledge']['embedding_space_id'], c['config']['embedding']['embedding_space_id'])
        self.assertEqual(len(self.agent.posts()), 2)
        self.assertFalse(self.tools.status(self.plan_id)['question_ready'])

    def test_lost_creation_reply_is_not_automatically_reposted_and_retry_reuses_key(self):
        self.agent.lose.add('/api/tools')
        self.assertEqual(self.tools.prepare(self.plan_id, self.agent)['state'], 'confirmation_required')
        reopened = SearchToolStore(UploadStore(self.root))
        reopened.prepare(self.plan_id, self.agent)
        self.assertEqual(len(self.agent.posts()), 1)
        self.agent.lose.clear(); reopened.prepare(self.plan_id, self.agent, retry=True)
        posts = self.agent.posts('/api/tools')
        self.assertEqual(posts[0][2:], posts[1][2:])
        self.assertEqual(len([p for p in self.agent.resources if p.startswith('/api/tools/')]), 1)

    def test_interruption_before_local_receipt_preserves_attempt_and_same_intent(self):
        self.agent.interrupt.add('/api/tools')
        with self.assertRaises(KeyboardInterrupt):
            self.tools.prepare(self.plan_id, self.agent)
        reopened = SearchToolStore(UploadStore(self.root))
        self.assertEqual(reopened.status(self.plan_id)['steps'][0]['attempts'], 1)
        reopened.prepare(self.plan_id, self.agent)
        self.assertEqual(len(self.agent.posts()), 1)
        self.agent.interrupt.clear(); reopened.prepare(self.plan_id, self.agent, retry=True)
        self.assertEqual(self.agent.posts()[0][2:], self.agent.posts()[1][2:])

    def test_lost_version_reply_does_not_recreate_logical_tool(self):
        original = self.agent.request
        def request(method, path, body=None, key=None):
            if path.endswith('/versions'):
                self.agent.lose.add(path)
            return original(method, path, body, key)
        with patch.object(self.agent, 'request', side_effect=request):
            self.tools.prepare(self.plan_id, self.agent)
        self.tools.prepare(self.plan_id, self.agent)
        self.assertEqual(len(self.agent.posts()), 2)
        self.agent.lose.clear(); self.tools.prepare(self.plan_id, self.agent, retry=True)
        self.assertEqual(len(self.agent.posts('/api/tools')), 1)
        self.assertEqual(self.agent.posts('/versions')[0][2:], self.agent.posts('/versions')[1][2:])

    def test_known_version_receipt_with_failed_GET_recovers_without_POST(self):
        original = self.agent.request
        def request(method, path, body=None, key=None):
            code, raw = original(method, path, body, key)
            if path.endswith('/versions'):
                self.agent.get_errors.add('/api/tool-versions/' + json.loads(raw)['tool_version_id'])
            return code, raw
        with patch.object(self.agent, 'request', side_effect=request):
            self.assertEqual(self.tools.prepare(self.plan_id, self.agent)['state'], 'attention_required')
        self.agent.get_errors.clear()
        self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'verified')
        self.assertEqual(len(self.agent.posts()), 2)

    def test_changed_gateway_refuses_before_any_request(self):
        self.agent.origin = 'http://127.0.0.1:15001'
        with self.assertRaises(UploadError):
            self.tools.prepare(self.plan_id, self.agent)
        self.assertEqual(self.agent.calls, [])

    def test_wrong_connection_identity_or_origin_blocks_creation(self):
        for field, value in (('connection_version_id', str(uuid4())), ('base_url', 'http://other:8000')):
            path = '/api/connection-versions/' + self.selection['knowledge_connection']['version_id']
            original = copy.deepcopy(self.agent.resources[path]); self.agent.resources[path][field] = value
            with self.assertRaises(UploadError):
                self.tools.prepare(self.plan_id, self.agent)
            self.assertEqual(self.agent.posts(), []); self.agent.resources[path] = original

    def test_same_connection_version_cannot_change_credential_revision(self):
        self.draft()
        path = '/api/connection-versions/' + self.selection['model_connection']['version_id']
        self.agent.resources[path]['credential_revision'] = 'v2'
        with self.assertRaises(UploadError) as error:
            self.tools.prepare(self.plan_id, self.agent)
        self.assertEqual(error.exception.code, 'SEARCH_TOOL_CONNECTION_CHANGED')
        self.assertEqual(len(self.agent.posts()), 2)

    def test_resolve_must_find_exact_index_space_and_connection(self):
        original = self.agent.request
        def wrong(*args, **kwargs):
            code, raw = original(*args, **kwargs); result = json.loads(raw)
            result['binding']['knowledge']['index_version_id'] = str(uuid4())
            return code, encoded(result)
        with patch.object(self.agent, 'request', side_effect=wrong), self.assertRaises(UploadError):
            self.tools.prepare(self.plan_id, self.agent)
        self.assertEqual(self.agent.posts(), [])

    def test_changed_index_head_does_not_create_tool(self):
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE indexing_head SET active_job_id=?', (self.first['job_id'],))
        with self.assertRaises(UploadError):
            self.tools.prepare(self.plan_id, self.agent)
        self.assertEqual(self.agent.calls, [])

    def test_definition_mutation_is_not_accepted_on_readback(self):
        version = self.draft(); count = len(self.agent.posts())
        version['binding']['knowledge']['index_version_id'] = str(uuid4())
        self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'attention_required')
        self.assertEqual(len(self.agent.posts()), count)

    def test_revision_conflict_keeps_original_version_request(self):
        original = self.agent.request
        def conflicting(method, path, body=None, key=None):
            if path.endswith('/versions'):
                self.agent.resources['/api/tools/' + path.split('/')[3]]['revision'] += 1
            return original(method, path, body, key)
        with patch.object(self.agent, 'request', side_effect=conflicting):
            self.tools.prepare(self.plan_id, self.agent)
        first = self.agent.posts('/versions')[0]
        self.tools.prepare(self.plan_id, self.agent, retry=True)
        self.assertEqual(first[2:], self.agent.posts('/versions')[-1][2:])
        self.assertFalse(self.tools.status(self.plan_id)['tool_binding_verified'])

    def test_concurrent_prepare_refuses_second_lane(self):
        self.agent.enter, self.agent.release = threading.Event(), threading.Event(); failures = []
        def prepare():
            try:
                self.tools.prepare(self.plan_id, self.agent)
            except Exception as exc:
                failures.append(exc)
        worker = threading.Thread(target=prepare); worker.start()
        try:
            self.assertTrue(self.agent.enter.wait(2))
            with self.assertRaises(UploadError) as error:
                self.tools.prepare(self.plan_id, self.agent)
            self.assertEqual(error.exception.code, 'SEARCH_TOOL_BUSY')
        finally:
            self.agent.release.set(); worker.join(3)
        self.assertEqual(failures, []); self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.agent.posts('/api/tools')), 1)


class CallTests(SearchFixture):
    def test_test_inference_opt_in_blocks_before_any_HTTP(self):
        self.draft(); count = len(self.agent.calls); self.agent.inference_enabled = False
        with self.assertRaises(UploadError):
            self.tools.test(self.plan_id, self.agent, '질문')
        self.assertEqual(len(self.agent.calls), count)
        self.assertIsNone(self.tools._step(self.plan_id, 'agent.tool_call'))

    def test_202_waiting_and_failed_call_never_mean_verified_or_question_ready(self):
        self.draft()
        result = self.tools.test(self.plan_id, self.agent, '질문')
        self.assertEqual(result['state'], 'waiting'); self.assertFalse(result['question_ready'])
        self.agent.finish(state='failed')
        self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'failed')
        self.tools.test(self.plan_id, self.agent, '질문', retry=True)
        self.assertEqual(len(self.agent.posts('/api/tool-calls')), 1)

    def test_lost_call_receipt_and_reopen_keep_same_query_key_and_version(self):
        self.draft(); self.agent.lose.add('/api/tool-calls')
        self.tools.test(self.plan_id, self.agent, '그대로 유지할 질문')
        reopened = SearchToolStore(UploadStore(self.root)); reopened.test(self.plan_id, self.agent, '그대로 유지할 질문')
        self.assertEqual(len(self.agent.posts('/api/tool-calls')), 1)
        self.agent.lose.clear(); reopened.test(self.plan_id, self.agent, '그대로 유지할 질문', retry=True)
        self.assertEqual(self.agent.posts('/api/tool-calls')[0][2:], self.agent.posts('/api/tool-calls')[1][2:])
        self.assertEqual(len([p for p in self.agent.resources if p.startswith('/api/tool-calls/')]), 1)

    def test_changed_test_question_cannot_silently_allocate_another_key(self):
        self.successful(); count = len(self.agent.calls)
        with self.assertRaises(UploadError):
            self.tools.test(self.plan_id, self.agent, '다른 질문', retry=True)
        self.assertEqual(len(self.agent.calls), count)

    def test_readback_requires_call_key_arguments_hash_and_definition_identity(self):
        call = self.successful(); original = copy.deepcopy(call)
        for patch_value in ({'invocation_key': str(uuid4())}, {'input_sha256': 'e' * 64},
                            {'definition_sha256': 'f' * 64}, {'mode': 'run'}, {'tool_version_id': str(uuid4())}):
            call.clear(); call.update(copy.deepcopy(original)); call.update(patch_value)
            self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'attention_required')
        self.assertEqual(len(self.agent.posts('/api/tool-calls')), 1)

    def test_wrapper_connection_profile_and_index_mismatches_block_verification(self):
        call = self.successful(); original = copy.deepcopy(call)
        mutations = [lambda x: x['result'].update(completion='accepted_only'),
                     lambda x: x['result'].update(data=[]),
                     lambda x: x['result']['metadata'].update(connection_version_id=str(uuid4())),
                     lambda x: x['result']['metadata'].update(model_connection_version_id=str(uuid4())),
                     lambda x: x['result']['metadata']['embedding_profile'].update(model_version_id=str(uuid4())),
                     lambda x: x['result']['data'].update(index_version_id=str(uuid4())),
                     lambda x: x['result']['data'].update(returned_count=True)]
        for mutate in mutations:
            call.clear(); call.update(copy.deepcopy(original)); mutate(call)
            self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'attention_required')

    def test_foreign_document_and_empty_spans_are_refused(self):
        call = self.successful(); original = copy.deepcopy(call)
        for field, value in (('canonical_id', str(uuid4())), ('source_document_id', str(uuid4())), ('source_spans', []), ('retrieval_score', float('nan'))):
            call.clear(); call.update(copy.deepcopy(original)); call['result']['data']['results'][0][field] = value
            self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'attention_required')

    def test_empty_result_is_valid_but_not_semantic_quality_or_question_ready(self):
        self.draft(); self.tools.test(self.plan_id, self.agent, '자료에 없는 내용')
        self.agent.finish(empty=True)
        result = self.tools.observe(self.plan_id, self.agent)
        self.assertEqual(result['state'], 'verified'); self.assertFalse(result['question_ready'])
        summary = self.tools._step(self.plan_id, 'agent.tool_call')['summary']
        self.assertEqual(summary['returned_count'], 0); self.assertFalse(summary['semantic_quality_verified'])

    def test_verified_result_is_compact_and_does_not_store_canonical_body_vectors_or_hit_text(self):
        self.successful(); step = self.tools._step(self.plan_id, 'agent.tool_call')
        text = json.dumps(step['summary'], ensure_ascii=False)
        self.assertNotIn('자료의 실제 근거', text); self.assertNotIn('embedding', text)
        self.assertEqual(len(step['summary']['references']), 1)

    def test_Go_JSON_hash_preserves_exact_query_including_HTML_and_separator(self):
        query = '<>&\u2028'
        self.assertEqual(arguments_sha256({'query': query}), hashlib.sha256(b'{"query":"\\u003c\\u003e\\u0026\\u2028"}').hexdigest())
        self.successful(query)
        self.assertEqual(self.agent.posts('/api/tool-calls')[0][2]['arguments']['query'], query)

    def test_saved_max_attempts_refuses_before_resubmission(self):
        self.draft(); self.agent.lose.add('/api/tool-calls')
        self.tools.test(self.plan_id, self.agent, '질문')
        with self.uploads.connect(write=True) as db:
            db.execute("UPDATE search_tool_steps SET attempts=? WHERE operation='agent.tool_call'", (MAX_ATTEMPTS,))
        with self.assertRaises(UploadError):
            self.tools.test(self.plan_id, self.agent, '질문', retry=True)
        self.assertEqual(len(self.agent.posts('/api/tool-calls')), 1)

    def test_terminal_failure_cannot_be_rewritten_by_later_response(self):
        self.draft(); self.tools.test(self.plan_id, self.agent, '질문'); self.agent.finish(state='interrupted')
        self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'failed')
        self.agent.finish(state='succeeded').pop('error', None)
        self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'failed')
        with self.assertRaises(UploadError):
            self.tools.publish(self.plan_id, self.agent)

    def test_malformed_nested_result_becomes_attention_instead_of_raw_exception(self):
        call = self.successful()
        call['result']['data']['results'][0]['canonical_id'] = ['malformed']
        self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'attention_required')


class PublishTests(SearchFixture):
    def test_publish_requires_verified_test_and_never_enables_questions(self):
        self.draft()
        with self.assertRaises(UploadError):
            self.tools.publish(self.plan_id, self.agent)
        self.successful()
        result = self.tools.publish(self.plan_id, self.agent)
        self.assertTrue(result['tool_binding_verified']); self.assertFalse(result['question_ready'])
        self.assertFalse(result['production_tool_verified'])
        self.assertEqual(self.uploads.library()['question_revision'], 0)

    def test_lost_publication_reply_recovers_by_GET_without_second_POST(self):
        self.successful(); version = self.tools.status(self.plan_id)['tool_version_id']
        path = '/api/tool-versions/' + version + '/publish'; self.agent.lose.add(path)
        self.assertTrue(self.tools.publish(self.plan_id, self.agent)['tool_binding_verified'])
        SearchToolStore(UploadStore(self.root)).publish(self.plan_id, self.agent, retry=True)
        self.assertEqual(len(self.agent.posts('/publish')), 1)

    def test_interrupted_publication_recovers_by_GET_on_reopen(self):
        self.successful(); path = '/api/tool-versions/' + self.tools.status(self.plan_id)['tool_version_id'] + '/publish'
        self.agent.interrupt.add(path)
        with self.assertRaises(KeyboardInterrupt):
            self.tools.publish(self.plan_id, self.agent)
        self.assertTrue(SearchToolStore(UploadStore(self.root)).observe(self.plan_id, self.agent)['tool_binding_verified'])
        self.assertEqual(len(self.agent.posts('/publish')), 1)

    def test_revoked_or_changed_test_cannot_publish_from_stale_local_success(self):
        call = self.successful(); call['state'] = 'failed'; call['error'] = {'code': 'TOOL_TIMEOUT'}
        with self.assertRaises(UploadError):
            self.tools.publish(self.plan_id, self.agent)
        self.assertEqual(self.agent.posts('/publish'), [])

    def test_readback_checks_published_revision_and_immutable_definition(self):
        self.successful(); self.tools.publish(self.plan_id, self.agent)
        version = self.agent.resources['/api/tool-versions/' + self.tools.status(self.plan_id)['tool_version_id']]
        version['revision'] = 9
        self.assertFalse(self.tools.observe(self.plan_id, self.agent)['tool_binding_verified'])

    def test_readonly_gateway_can_observe_existing_receipts_without_inference(self):
        self.successful(); self.agent.inference_enabled = self.agent.writes_enabled = False
        count = len(self.agent.posts())
        self.assertEqual(self.tools.observe(self.plan_id, self.agent)['state'], 'verified')
        self.assertEqual(len(self.agent.posts()), count)


class TransportTests(unittest.TestCase):
    def serve(self, status=200, payload=None, content_type='application/json', headers=None):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): self.reply()
            def do_POST(self): self.reply()
            def reply(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                calls.append((self.command, self.path, dict(self.headers), raw))
                content = payload if isinstance(payload, bytes) else encoded(payload or {'ok': True})
                self.send_response(status); self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(content)))
                for key, value in (headers or {}).items(): self.send_header(key, value)
                self.end_headers(); self.wfile.write(content)
            def log_message(self, *args): pass
        return ThreadingHTTPServer(('127.0.0.1', 0), Handler), calls

    def test_POST_management_and_inference_have_independent_gates(self):
        api = AgentToolGateway({'url': 'http://127.0.0.1:9'})
        with self.assertRaises(UploadError):
            api.request('POST', '/api/tools', {'display_name': '검사', 'description': ''}, str(uuid4()))
        api.writes_enabled = True
        with self.assertRaises(UploadError) as error:
            api.request('POST', '/api/tool-calls', {'tool_version_id': str(uuid4()), 'arguments': {'query': '질문'}, 'mode': 'test'}, str(uuid4()))
        self.assertEqual(error.exception.code, 'TOOL_INFERENCE_NOT_AUTHORIZED'); self.assertEqual(api.calls, 0)

    def test_disallowed_paths_method_query_and_run_mode_do_not_reach_network(self):
        api = AgentToolGateway({'url': 'http://127.0.0.1:9'}, execute=True, allow_inference=True)
        for path in ('/api/agents', '/api/tools', '/api/tool-calls/' + str(uuid4()) + '?token=private', '/api/deployments/' + str(uuid4())):
            with self.assertRaises(UploadError): api.get(path)
        with self.assertRaises(UploadError):
            api.request('POST', '/api/tool-calls', {'tool_version_id': str(uuid4()), 'arguments': {'query': '질문'}, 'mode': 'run'}, str(uuid4()))
        self.assertEqual(api.calls, 0)

    def test_real_GET_ignores_proxy_and_has_no_JSON_content_type(self):
        server, calls = self.serve()
        with running(server) as port, patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}):
            api = AgentToolGateway({'url': 'http://127.0.0.1:' + str(port)})
            self.assertTrue(api.get('/api/tool-versions/' + str(uuid4()))['ok'])
        self.assertNotIn('Content-Type', calls[0][2])

    def test_real_POST_sends_pinned_body_and_idempotency_key(self):
        server, calls = self.serve(status=201); body = {'display_name': '문서 QA', 'description': ''}; key = str(uuid4())
        with running(server) as port:
            api = AgentToolGateway({'url': 'http://127.0.0.1:' + str(port)}, execute=True)
            self.assertEqual(api.request('POST', '/api/tools', body, key)[0], 201)
        self.assertEqual(json.loads(calls[0][3]), body); self.assertEqual(calls[0][2]['Idempotency-Key'], key)

    def test_inspect_POST_is_read_only_and_does_not_require_write_or_inference_opt_in(self):
        server, calls = self.serve()
        body = {'action': 'resolve', **{k: str(uuid4()) for k in ('connection_version_id', 'model_connection_version_id', 'index_id', 'index_version_id')}}
        with running(server) as port:
            api = AgentToolGateway({'url': 'http://127.0.0.1:' + str(port)})
            api.request('POST', '/api/knowledge/inspect', body, str(uuid4()))
        self.assertEqual(len(calls), 1)

    def test_redirect_and_raw_backend_secret_are_not_followed_or_exposed(self):
        for status in (302, 401, 409, 503):
            server, calls = self.serve(status=status, payload={'message': 'PRIVATE-CREDENTIAL'}, headers={'Location': 'http://127.0.0.1:1/private'})
            with running(server) as port:
                api = AgentToolGateway({'url': 'http://127.0.0.1:' + str(port)})
                with self.assertRaises(UploadError) as error: api.get('/api/tools/' + str(uuid4()))
                self.assertNotIn('PRIVATE-CREDENTIAL', str(error.exception))
            self.assertEqual(len(calls), 1)

    def test_duplicate_JSON_wrong_mime_and_oversize_are_refused(self):
        for payload, mime in ((b'{"x":1,"x":2}', 'application/json'), (b'{}', 'text/html'), (b' ' * (MAX_RESPONSE + 1), 'application/json')):
            server, calls = self.serve(payload=payload, content_type=mime)
            with running(server) as port:
                api = AgentToolGateway({'url': 'http://127.0.0.1:' + str(port)})
                with self.assertRaises(UploadError): api.get('/api/tools/' + str(uuid4()))
            self.assertEqual(len(calls), 1)

    def test_budget_refuses_before_network(self):
        api = AgentToolGateway({'url': 'http://127.0.0.1:9'}, clock=lambda: 1, budget_seconds=0)
        with self.assertRaises(UploadError): api.get('/api/tools/' + str(uuid4()))
        self.assertEqual(api.calls, 0)


class HTTPIntegrationTests(SearchFixture):
    CREATE_PLAN = False

    def serve(self):
        backend, headers = self.agent, []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                headers.append(dict(self.headers))
                raw = encoded(backend.get(self.path)); self.reply(200, raw)
            def do_POST(self):
                headers.append(dict(self.headers))
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                code, raw = backend.request('POST', self.path, body, self.headers['Idempotency-Key'])
                self.reply(code, raw)
            def reply(self, code, raw):
                self.send_response(code); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def log_message(self, *args): pass
        return ThreadingHTTPServer(('127.0.0.1', 0), Handler), headers

    def test_entire_adapter_lifecycle_over_real_HTTP_with_auth_and_fresh_budgets(self):
        server, headers = self.serve(); token = self.root / 'agent-test-token'
        token.write_text('local-fixture-token'); token.chmod(0o600)
        with running(server) as port:
            self.selection['agent_origin'] = self.agent.origin = 'http://127.0.0.1:' + str(port)
            self.plan_id = self.tools.plan(self.selection)['plan_id']
            def gateway():
                return AgentToolGateway({'url': self.agent.origin, 'token_file': str(token)}, execute=True, allow_inference=True)
            self.assertEqual(self.tools.prepare(self.plan_id, gateway())['state'], 'verified')
            self.assertEqual(self.tools.test(self.plan_id, gateway(), '<원문>의 결론은?')['state'], 'waiting')
            self.agent.finish()
            self.assertEqual(self.tools.observe(self.plan_id, gateway())['state'], 'verified')
            result = self.tools.publish(self.plan_id, gateway())
            self.assertTrue(result['tool_binding_verified']); self.assertFalse(result['question_ready'])
        self.assertTrue(all(h.get('Authorization') == 'Bearer local-fixture-token' for h in headers))
        self.assertEqual(len(self.agent.posts()), 4)

    def test_loopback_adapter_draft_does_not_execute_Tool_without_inference_authorization(self):
        server, headers = self.serve()
        with running(server) as port:
            self.selection['agent_origin'] = self.agent.origin = 'http://127.0.0.1:' + str(port)
            self.plan_id = self.tools.plan(self.selection)['plan_id']
            api = AgentToolGateway({'url': self.agent.origin}, execute=True)
            self.tools.prepare(self.plan_id, api); count = len(headers)
            with self.assertRaises(UploadError): self.tools.test(self.plan_id, api, '질문')
            self.assertEqual(len(headers), count)
        self.assertEqual(self.agent.posts('/api/tool-calls'), [])


class InitializationTests(IndexFixture):
    def test_init_with_paused_index_is_additive_idempotent_and_never_initializes_indexing_again(self):
        before = self.uploads.library(), self.indexing.head(), self.uploads.token_path.read_bytes()
        store = SearchToolStore.initialize(self.uploads)
        SearchToolStore.initialize(UploadStore(self.root))
        self.assertEqual(before, (self.uploads.library(), self.indexing.head(), self.uploads.token_path.read_bytes()))
        self.assertEqual(store.status()['plan_count'], 0)
        self.assertEqual(self.knowledge.calls, [])

    def test_initialization_error_rolls_back_all_new_tables_without_resetting_old_state(self):
        from src.apps.document_qa.search_tool import SCHEMA
        before = self.uploads.library(), self.indexing.head(), self.uploads.token_path.read_bytes()
        with patch('src.apps.document_qa.search_tool.SCHEMA', SCHEMA + ' INVALID SQL;'), self.assertRaises(sqlite3.OperationalError):
            SearchToolStore.initialize(self.uploads)
        with self.uploads.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'search_tool_%'").fetchone()[0], 0)
        self.assertEqual(before, (self.uploads.library(), self.indexing.head(), self.uploads.token_path.read_bytes()))
        self.assertEqual(SearchToolStore.initialize(self.uploads).status()['plan_count'], 0)


if __name__ == '__main__':
    unittest.main()
