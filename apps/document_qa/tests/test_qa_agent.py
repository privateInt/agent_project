"""Q06b-2 independent Agent fake, real SQLite and authenticated loopback APIs."""
import copy
from contextlib import redirect_stdout
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from apps.document_qa.api import make_server
from apps.document_qa.contracts import ContractError, fingerprint
from apps.document_qa.intake import UploadError
from apps.document_qa.qa_agent import QAAgentStore, MAX_ATTEMPTS, main
from apps.document_qa import qa_agent_schema as schema
from apps.document_qa.qa_agent_results import CHAT_PROFILE, MAX_RESULT_BYTES, NO_EVIDENCE, question_checked, selection_checked
from apps.document_qa.qa_agent_transport import AgentQAGateway, request_checked
from apps.document_qa.question_store import QuestionStore
from apps.document_qa.uploads import UploadStore, file_lock
from apps.document_qa.tests.test_search_tool import SearchFixture
from apps.document_qa.tests.test_refinement import encoded, running
from apps.document_qa.tests.test_question_store import candidate

PROJECT = Path(__file__).resolve().parents[3]
INPUT = {'type': 'object', 'required': ['question'], 'additionalProperties': False,
         'properties': {'question': {'type': 'string', 'minLength': 1, 'maxLength': 8192}}}
OUTPUT = {'type': 'object', 'required': ['answer', 'outcome', 'citations', 'warnings', 'answer_truncated', 'tool_call_id', 'identity']}


def go_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    for char, escape in (('<', '\\u003c'), ('>', '\\u003e'), ('&', '\\u0026'), ('\u2028', '\\u2028'), ('\u2029', '\\u2029')):
        raw = raw.replace(char, escape)
    return hashlib.sha256(raw.encode()).hexdigest()


class FakeQA:
    origin = 'http://127.0.0.1:15000'
    writes_enabled = True
    inference_enabled = True

    def __init__(self, tool_agent, selected):
        self.resources = copy.deepcopy(tool_agent.resources)
        self.candidate = copy.deepcopy(tool_agent.candidate)
        self.selected = copy.deepcopy(selected)
        self.calls, self.keys = [], {}
        self.lose, self.interrupt, self.get_errors = set(), set(), set()
        self.enter = self.release = None
        conn = selected['chat_connection']
        self.resources['/api/connection-versions/' + conn['version_id']] = {
            'connection_version_id': conn['version_id'], 'connection_id': str(uuid4()), 'version': 1,
            'base_url': conn['base_url'], 'auth_type': 'bearer', 'credential_ref': 'chat', 'credential_revision': 'v1'}
        self.identity = {'deployment_id': selected['chat_deployment_id'], 'model_version_id': selected['chat_model_version_id'],
                         'model_id': str(uuid4()), 'repo_id': 'google/gemma-3-4b-it',
                         'served_model_name': 'deployment-' + selected['chat_deployment_id'], 'max_model_len': 16384}

    def get(self, path):
        self.calls.append(('GET', path, None, None))
        if path in self.get_errors:
            raise UploadError('AGENT_TOOL_OUTCOME_UNKNOWN', 502)
        return copy.deepcopy(self.resources[path])

    def posts(self, suffix=None):
        return [c for c in self.calls if c[0] == 'POST' and c[1] != '/api/agent-models/inspect'
                and (suffix is None or c[1].endswith(suffix))]

    def request(self, method, path, body=None, key=None):
        assert method == 'POST'
        self.calls.append((method, path, copy.deepcopy(body), key))
        if path == '/api/agent-models/inspect':
            return 200, encoded({'model': {'connection_version_id': self.selected['chat_connection']['version_id'],
                'deployment_id': self.selected['chat_deployment_id'], 'model_version_id': self.selected['chat_model_version_id']},
                'identity': copy.deepcopy(self.identity)})
        if (path, key) in self.keys:
            old_body, status, response = self.keys[(path, key)]
            if old_body != body:
                raise UploadError('AGENT_TOOL_CONFLICT', 502)
        elif path == '/api/agents':
            identifier = str(uuid4())
            response = {'agent_id': identifier, **body, 'enabled': True, 'revision': 1}
            self.resources['/api/agents/' + identifier] = copy.deepcopy(response)
            status = 201
        elif path.endswith('/versions'):
            agent = self.resources['/api/agents/' + path.split('/')[3]]
            if agent['revision'] != body['expected_revision']:
                raise UploadError('AGENT_TOOL_CONFLICT', 502)
            tool = self.resources['/api/tool-versions/' + body['tool_version_id']]
            assert tool['state'] == 'published'
            identifier = str(uuid4())
            definition = {k: v for k, v in body.items() if k != 'expected_revision'}
            response = {'agent_version_id': identifier, 'agent_id': agent['agent_id'], 'version': 1,
                'revision': 1, 'state': 'draft', **definition, 'input_schema': copy.deepcopy(INPUT), 'output_schema': copy.deepcopy(OUTPUT),
                'definition_sha256': go_hash({'definition': definition, 'input_schema': INPUT, 'output_schema': OUTPUT})}
            self.resources['/api/agent-versions/' + identifier] = copy.deepcopy(response)
            agent['revision'] += 1
            status = 201
        elif path == '/api/agent-runs':
            version = self.resources['/api/agent-versions/' + body['agent_version_id']]
            assert version['state'] == ('draft' if body['mode'] == 'test' else 'published')
            identifier = str(uuid4())
            response = {'agent_run_id': identifier, 'agent_version_id': body['agent_version_id'], 'state': 'accepted',
                        'status_url': '/api/agent-runs/' + identifier}
            self.resources[response['status_url']] = {**response, 'input': copy.deepcopy(body['input']), 'mode': body['mode'],
                'input_sha256': go_hash(body['input']), 'invocation_key': key, 'definition_sha256': version['definition_sha256'],
                'caller_context': {}}
            status = 202
        else:
            assert path.endswith('/publish')
            version = self.resources['/api/agent-versions/' + path.split('/')[3]]
            if version['revision'] != body['expected_revision']:
                raise UploadError('AGENT_TOOL_CONFLICT', 502)
            assert any(v.get('mode') == 'test' and v.get('state') == 'succeeded' and v.get('agent_version_id') == version['agent_version_id']
                       for p, v in self.resources.items() if p.startswith('/api/agent-runs/'))
            version.update(state='published', revision=version['revision'] + 1)
            response, status = copy.deepcopy(version), 200
        self.keys.setdefault((path, key), (copy.deepcopy(body), status, copy.deepcopy(response)))
        if self.enter and path == '/api/agents':
            self.enter.set(); self.release.wait(3)
        if path in self.interrupt:
            raise KeyboardInterrupt()
        if path in self.lose:
            raise UploadError('AGENT_TOOL_OUTCOME_UNKNOWN', 502)
        return status, encoded(response)

    def finish(self, state='succeeded', empty=False, no_evidence=False):
        runs = [v for p, v in self.resources.items() if p.startswith('/api/agent-runs/')]
        run = runs[-1]
        run['state'] = state
        if state in ('accepted', 'running'):
            return run
        if state != 'succeeded':
            run['error'] = {'code': 'MODEL_TIMEOUT', 'stage': 'generate'}
            return run
        version = self.resources['/api/agent-versions/' + run['agent_version_id']]
        tool = self.resources['/api/tool-versions/' + version['tool_version_id']]
        k = tool['binding']['knowledge']; document = self.candidate['documents'][0]
        hits = [] if empty else [{'chunk_id': 'd' * 64, 'canonical_id': document['canonical_id'],
            'source_document_id': document['source_document_id'], 'kind': 'text', 'text': '이 자료의 결론은 검증된 사실입니다.',
            'rank': 1, 'retrieval_rank': 1, 'retrieval_score': 0.8, 'source_spans': [{'block_id': 'paragraph-1'}],
            'quality': {'state': 'ready'}}]
        identifier = str(uuid4())
        child = {'tool_call_id': identifier, 'parent_agent_run_id': run['agent_run_id'], 'parent_step_id': 'search',
            'mode': 'run', 'caller_context': {}, 'state': 'succeeded', 'tool_version_id': tool['tool_version_id'],
            'invocation_key': str(uuid4()), 'input_sha256': go_hash({'query': run['input']['question']}),
            'definition_sha256': tool['definition_sha256'], 'result': {'completion': 'completed', 'data': {
                'index_id': k['index_id'], 'index_version_id': k['index_version_id'], 'embedding_space_id': k['embedding_space_id'],
                'results': hits, 'returned_count': len(hits), 'top_k': k['top_k'], 'candidate_k': k['candidate_k'],
                'candidate_count': len(hits), 'filters': {}, 'rerank': {'requested': False, 'applied': False, 'status': 'disabled'}},
                'metadata': {'adapter': 'knowledge_search_v1', 'index_version_id': k['index_version_id'],
                    'connection_version_id': tool['binding']['connection_version_id'], 'model_connection_version_id': k['model_connection_version_id'],
                    'embedding_profile': {**self.candidate['config']['embedding'], 'capability': 'embedding', 'status': 'ready',
                                          'dimension': 1024, 'normalization': 'l2', 'truncation': 'reject'}}}}
        self.resources['/api/tool-calls/' + identifier] = child
        run['tool_call_id'] = identifier
        run['result'] = {'answer': NO_EVIDENCE if empty or no_evidence else '검증된 사실입니다. [S1]',
            'outcome': 'no_evidence' if empty or no_evidence else 'answered',
            'citations': [] if empty or no_evidence else [{'citation_id': 'S1', 'source': copy.deepcopy(hits[0])}],
            'warnings': [], 'answer_truncated': False, 'tool_call_id': identifier,
            'selected_chunk_ids': [h['chunk_id'] for h in hits], 'excluded_sources': [],
            'identity': {'template_id': 'knowledge_qa_v1', 'template_version': 1, 'tool_version_id': tool['tool_version_id'],
                         'model': {} if empty else copy.deepcopy(self.identity), 'knowledge': copy.deepcopy(child['result']['metadata'])},
            'usage': {} if empty else {'prompt_tokens': 10, 'completion_tokens': 8, 'total_tokens': 18}}
        return run


class Fixture(SearchFixture):
    def setUp(self):
        super().setUp()
        self.successful()
        self.tools.publish(self.plan_id, self.agent)
        QuestionStore.migrate(self.uploads)
        self.qa = QAAgentStore.initialize(self.uploads)
        self.selected = {'chat_connection': {'version_id': str(uuid4()), 'base_url': 'http://model-gateway:8000'},
                         'chat_deployment_id': str(uuid4()), 'chat_model_version_id': str(uuid4())}
        self.fake = FakeQA(self.agent, self.selected)
        self.qa_plan = self.qa.plan(self.plan_id, self.selected)['plan_id']

    def prepared(self):
        result = self.qa.prepare(self.qa_plan, self.fake)
        self.assertEqual(result['state'], 'verified', result)
        return result

    def verified_test(self, empty=False):
        self.prepared()
        self.qa.test(self.qa_plan, self.fake, '결론은?')
        run = self.fake.finish(empty=empty)
        observed = self.qa.observe(self.qa_plan, self.fake)
        self.assertEqual(observed['state'], 'verified', observed)
        return run

    def published(self):
        self.verified_test()
        result = self.qa.publish(self.qa_plan, self.fake)
        self.assertTrue(result['agent_binding_verified'], result)
        return result

    def active_fixture(self, revision=1):
        self.published()
        body = self.qa.target(self.qa_plan, revision)
        saved = self.qa.questions.save_target(body)
        # Positive head fixtures only. No production activation API exists in Q06b-2.
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE question_head SET revision=?,target_id=?', (revision, saved['target_id']))
        return saved


class StorageTests(Fixture):
    def test_process_exit_inside_init_or_after_commit_recovers_without_reset(self):
        for mode in ('first_DDL', 'all_DDL', 'after_commit'):
            clone = self.root.parent / ('qa-crash-' + mode)
            shutil.copytree(self.root, clone)
            copied = UploadStore(clone)
            with copied.connect(write=True) as db:
                for key in reversed(tuple(schema.OBJECTS)):
                    db.execute('DROP ' + ('TRIGGER ' if 'no_update' in key or 'no_delete' in key else 'TABLE ') + key)
            script = '''import os,sys
from apps.document_qa.qa_agent import QAAgentStore
from apps.document_qa import qa_agent_schema as schema
from apps.document_qa.uploads import UploadStore
mode=sys.argv[2]
original=schema.install
def crash(db):
 if mode=='first_DDL': db.execute(next(iter(schema.OBJECTS.values())))
 else: original(db)
 os._exit(79)
if mode!='after_commit': schema.install=crash
QAAgentStore.initialize(UploadStore(sys.argv[1]))
os._exit(79)
'''
            child = subprocess.run([sys.executable, '-B', '-c', script, str(clone), mode], cwd=PROJECT, capture_output=True)
            self.assertEqual(child.returncode, 79, child.stderr)
            with copied.connect() as db: self.assertEqual(schema.present(db), mode == 'after_commit')
            recovered = QAAgentStore.initialize(copied)
            self.assertEqual(recovered.status()['agent_plan_count'], 0)
            self.assertEqual(recovered.status()['head_revision'], 0)
            self.assertEqual(copied.jobs(), self.uploads.jobs())
            self.assertEqual(copied.token_path.read_bytes(), self.uploads.token_path.read_bytes())

    def test_local_init_repeat_preserves_all_old_rows_secrets_and_schemas(self):
        with self.uploads.connect() as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'qa_agent_*'")]
            before = {n: [tuple(r) for r in db.execute('SELECT * FROM ' + n)] for n in names}
        secrets = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file() and ('key' in p.name or p.name == 'api-token')}
        QAAgentStore.initialize(UploadStore(self.root))
        with self.uploads.connect() as db:
            self.assertEqual(before, {n: [tuple(r) for r in db.execute('SELECT * FROM ' + n)] for n in names})
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertEqual(secrets, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file() and ('key' in p.name or p.name == 'api-token')})
        self.assertEqual(self.qa.status()['head_revision'], 0)
        self.assertFalse(self.qa.status()['question_ready'])
        self.assertEqual(self.fake.calls, [])

    def test_cli_init_status_are_local_and_report_CAS_next(self):
        for action in ('init', 'status'):
            out = io.StringIO()
            with redirect_stdout(out): self.assertEqual(main([action, '--state', str(self.root)]), 0)
            value = json.loads(out.getvalue())
            self.assertEqual(value['production_api_requests'], 0)
            self.assertFalse(value['inference_requested']); self.assertFalse(value['GPU_requested'])
            self.assertEqual(value['next_step'], 'Q06b_3_question_head_CAS_activation')

    def test_partial_and_changed_extension_refused_without_repair(self):
        with self.uploads.connect(write=True) as db: db.execute('DROP TRIGGER qa_agent_plan_no_update')
        with self.assertRaises(UploadError): QAAgentStore.initialize(self.uploads)

    def test_metadata_change_refused(self):
        with self.uploads.connect(write=True) as db: db.execute("UPDATE qa_agent_metadata SET migration_sha256=?", ('0' * 64,))
        with self.assertRaises(UploadError): QAAgentStore(self.uploads)

    def test_error_and_keyboard_interrupt_rollback_schema_transaction(self):
        for exception in (OSError('injected'), KeyboardInterrupt()):
            with self.uploads.connect(write=True) as db:
                for key in reversed(tuple(schema.OBJECTS)):
                    db.execute('DROP ' + ('TRIGGER ' if 'no_update' in key or 'no_delete' in key else 'TABLE ') + key)
            def fail(db):
                db.execute(next(iter(schema.OBJECTS.values())))
                raise exception
            with patch.object(schema, 'install', fail), self.assertRaises(type(exception)):
                QAAgentStore.initialize(self.uploads)
            with self.uploads.connect() as db: self.assertFalse(schema.present(db))
            QAAgentStore.initialize(self.uploads)

    def test_plan_and_question_SQL_immutability(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.uploads.connect(write=True) as db: db.execute("UPDATE qa_agent_plans SET body='{}'")
        self.active_fixture()
        question = self.qa.accept(str(uuid4()), '결론은?')
        with self.assertRaises(sqlite3.IntegrityError):
            with self.uploads.connect(write=True) as db: db.execute('DELETE FROM qa_agent_questions WHERE question_id=?', (question['question_id'],))

    def test_lane_busy_has_no_network_or_new_intents(self):
        with file_lock(self.root / 'qa-agent-lane.lock'), self.assertRaises(UploadError): self.qa.prepare(self.qa_plan, self.fake)
        self.assertEqual(self.fake.calls, [])

    def test_plan_requires_current_verified_published_tool(self):
        with self.uploads.connect(write=True) as db:
            db.execute("UPDATE search_tool_steps SET state='waiting' WHERE operation='agent.tool.publish'")
        with self.assertRaises(UploadError): self.qa.plan(self.plan_id, self.selected)

    def test_plan_selection_changes_create_distinct_immutable_plan(self):
        selected = {**self.selected, 'max_tokens': 128}
        second = self.qa.plan(self.plan_id, selected)['plan_id']
        self.assertNotEqual(second, self.qa_plan)
        self.assertEqual(self.qa.plan(self.plan_id, self.selected)['plan_id'], self.qa_plan)


class LifecycleTests(Fixture):
    def test_concurrent_prepare_busy_preserves_single_agent_and_version(self):
        self.fake.enter, self.fake.release = threading.Event(), threading.Event()
        results = []
        thread = threading.Thread(target=lambda: results.append(self.qa.prepare(self.qa_plan, self.fake)))
        thread.start()
        try:
            self.assertTrue(self.fake.enter.wait(2))
            with self.assertRaises(UploadError) as raised: self.qa.prepare(self.qa_plan, self.fake)
            self.assertEqual(raised.exception.code, 'QA_AGENT_BUSY')
        finally:
            self.fake.release.set(); thread.join(3)
        self.assertEqual(results[0]['state'], 'verified')
        self.assertEqual(len(self.fake.posts('/api/agents')), 1)
        self.assertEqual(len(self.fake.posts('/versions')), 1)

    def test_prepare_reopen_reuses_agent_version_and_connections(self):
        result = self.prepared(); before = len(self.fake.posts())
        reopened = QAAgentStore(UploadStore(self.root))
        self.assertEqual(reopened.prepare(self.qa_plan, self.fake)['agent_version_id'], result['agent_version_id'])
        self.assertEqual(len(self.fake.posts()), before)

    def test_create_and_version_lost_response_need_explicit_same_key_retry(self):
        for suffix in ('/api/agents', '/versions'):
            path = suffix
            if suffix == '/versions': self.prepared()
            if suffix == '/versions':
                # A fresh plan creates a second version on the same logical Agent.
                selected = {**self.selected, 'max_tokens': 128}
                self.qa_plan = self.qa.plan(self.plan_id, selected)['plan_id']
                agent_id = next(v['agent_id'] for p, v in self.fake.resources.items() if p.startswith('/api/agents/'))
                path = '/api/agents/' + agent_id + '/versions'
            self.fake.lose.add(path)
            self.qa.prepare(self.qa_plan, self.fake)
            first = self.fake.posts(suffix)[-1]
            before = len(self.fake.posts(suffix))
            self.qa.prepare(self.qa_plan, self.fake)
            self.assertEqual(len(self.fake.posts(suffix)), before)
            self.fake.lose.clear()
            self.qa.prepare(self.qa_plan, self.fake, retry=True)
            self.assertEqual(self.fake.posts(suffix)[-1][2:], first[2:])

    def test_keyboard_interrupt_keeps_unknown_attempt_for_explicit_retry(self):
        self.fake.interrupt.add('/api/agents')
        with self.assertRaises(KeyboardInterrupt): self.qa.prepare(self.qa_plan, self.fake)
        self.assertEqual(self.qa.status(self.qa_plan)['state'], 'confirmation_required')
        self.fake.interrupt.clear()
        before = len(self.fake.posts()); self.qa.prepare(self.qa_plan, self.fake)
        self.assertEqual(len(self.fake.posts()), before)
        self.assertEqual(self.qa.prepare(self.qa_plan, self.fake, retry=True)['state'], 'verified')

    def test_explicit_inference_and_write_opt_in_required(self):
        self.fake.writes_enabled = False
        with self.assertRaises(UploadError): self.qa.prepare(self.qa_plan, self.fake)
        self.assertEqual(self.fake.posts(), [])
        self.fake.writes_enabled = True; self.prepared(); self.fake.inference_enabled = False
        with self.assertRaises(UploadError): self.qa.test(self.qa_plan, self.fake, '질문')
        self.assertEqual(self.fake.posts('/api/agent-runs'), [])

    def test_test_waits_and_success_verifies_before_publish(self):
        self.prepared()
        self.assertEqual(self.qa.test(self.qa_plan, self.fake, '결론은?')['state'], 'waiting')
        self.fake.finish(state='running')
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'waiting')
        self.fake.finish()
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'verified')
        self.assertTrue(self.qa.publish(self.qa_plan, self.fake)['agent_binding_verified'])
        self.assertEqual(self.qa.questions.head()['revision'], 0)

    def test_failure_and_interruption_are_terminal_and_not_replayed(self):
        for state in ('failed', 'interrupted'):
            selected = {**self.selected, 'max_tokens': 128 if state == 'failed' else 256}
            self.qa_plan = self.qa.plan(self.plan_id, selected)['plan_id']
            self.prepared(); self.qa.test(self.qa_plan, self.fake, '결론은?'); self.fake.finish(state)
            self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'failed')
            count = len(self.fake.posts('/api/agent-runs'))
            self.qa.test(self.qa_plan, self.fake, '결론은?', retry=True)
            self.assertEqual(len(self.fake.posts('/api/agent-runs')), count)
            with self.assertRaises(UploadError): self.qa.publish(self.qa_plan, self.fake)

    def test_empty_and_model_no_evidence_pass_without_invented_answer(self):
        self.prepared(); self.qa.test(self.qa_plan, self.fake, '결론은?')
        self.fake.finish(empty=True)
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'verified')
        self.fake.finish(no_evidence=True)
        # A changed already-verified result is refused, even when individually valid.
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required')

    def test_publish_lost_response_reconciles_by_get_without_second_post(self):
        self.verified_test()
        version = self.qa.status(self.qa_plan)['agent_version_id']
        self.fake.lose.add('/api/agent-versions/' + version + '/publish')
        self.assertTrue(self.qa.publish(self.qa_plan, self.fake)['agent_binding_verified'])
        self.qa.publish(self.qa_plan, self.fake)
        self.assertEqual(len(self.fake.posts('/publish')), 1)

    def test_connection_or_tool_or_chat_model_changes_block_before_Agent_writes(self):
        for kind in ('tool', 'connection', 'chat'):
            original = copy.deepcopy(self.fake.resources), copy.deepcopy(self.fake.identity)
            if kind == 'tool':
                version = self.qa._plan(self.qa_plan)['tool']['tool_version_id']
                self.fake.resources['/api/tool-versions/' + version]['binding']['knowledge']['index_version_id'] = str(uuid4())
            elif kind == 'connection':
                key = self.selected['chat_connection']['version_id']
                self.fake.resources['/api/connection-versions/' + key]['base_url'] = 'http://other:8000'
            else: self.fake.identity['model_version_id'] = str(uuid4())
            with self.assertRaises(UploadError): self.qa.prepare(self.qa_plan, self.fake)
            self.assertEqual(self.fake.posts(), [])
            self.fake.resources, self.fake.identity = original

    def test_publish_requires_verifiable_test_and_version_immutability(self):
        self.prepared()
        with self.assertRaises(UploadError): self.qa.publish(self.qa_plan, self.fake)
        self.qa.test(self.qa_plan, self.fake, '결론은?'); self.fake.finish()
        version = self.qa.status(self.qa_plan)['agent_version_id']
        self.fake.resources['/api/agent-versions/' + version]['prompt'] = 'changed prompt'
        self.qa.observe(self.qa_plan, self.fake)
        with self.assertRaises(UploadError): self.qa.publish(self.qa_plan, self.fake)
        self.assertEqual(self.fake.posts('/publish'), [])

    def test_wrong_origin_and_changed_test_question_are_refused(self):
        self.fake.origin = 'http://other:15000'
        with self.assertRaises(UploadError): self.qa.prepare(self.qa_plan, self.fake)
        self.fake.origin = FakeQA.origin; self.prepared(); self.qa.test(self.qa_plan, self.fake, '결론은?')
        with self.assertRaises(UploadError): self.qa.test(self.qa_plan, self.fake, '다른 질문')


class QuestionTests(Fixture):
    def test_process_exit_after_run_intent_keeps_key_and_does_not_auto_send(self):
        self.active_fixture(); question = self.qa.accept(str(uuid4()), '중단 검사')
        fixture = self.root.parent / 'qa-agent-fixture.json'
        fixture.write_text(json.dumps({'resources': self.fake.resources, 'identity': self.fake.identity,
            'model': {'connection_version_id': self.selected['chat_connection']['version_id'],
                      'deployment_id': self.selected['chat_deployment_id'], 'model_version_id': self.selected['chat_model_version_id']}}))
        script = '''import json,os,sys
from apps.document_qa.qa_agent import QAAgentStore
from apps.document_qa.uploads import UploadStore
values=json.load(open(sys.argv[3]))
class API:
 origin='http://127.0.0.1:15000'
 writes_enabled=True
 inference_enabled=True
 def get(self,path): return values['resources'][path]
 def request(self,method,path,body,key):
  if path=='/api/agent-models/inspect': return 200,json.dumps({'model':values['model'],'identity':values['identity']}).encode()
  if path=='/api/agent-runs': os._exit(86)
  raise AssertionError(path)
QAAgentStore(UploadStore(sys.argv[1])).dispatch(sys.argv[2],API())
'''
        child = subprocess.run([sys.executable, '-B', '-c', script, str(self.root), question['question_id'], str(fixture)],
                               cwd=PROJECT, capture_output=True)
        self.assertEqual(child.returncode, 86, child.stderr)
        recovered = QAAgentStore(UploadStore(self.root))
        result = recovered.question(question['question_id'])
        self.assertEqual(result['state'], 'confirmation_required'); self.assertEqual(result['attempts'], 1)
        prior = recovered._step(question['question_id'], 'agent.run')
        count = len(self.fake.posts('/api/agent-runs'))
        recovered.dispatch(question['question_id'], self.fake)
        self.assertEqual(len(self.fake.posts('/api/agent-runs')), count)
        recovered.dispatch(question['question_id'], self.fake, retry=True)
        self.assertEqual(self.fake.posts('/api/agent-runs')[-1][2:], (prior['body'], prior['request_key']))

    def test_concurrent_duplicate_accept_keeps_one_record(self):
        self.active_fixture(); key = str(uuid4()); outputs = []
        def accept():
            try: outputs.append(self.qa.accept(key, '동시 질문'))
            except UploadError as exc: outputs.append(exc.code)
        threads = [threading.Thread(target=accept) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertTrue(all(isinstance(o, dict) or o == 'QA_AGENT_BUSY' for o in outputs))
        result = self.qa.accept(key, '동시 질문')
        self.assertTrue(all(o['question_id'] == result['question_id'] for o in outputs if isinstance(o, dict)))
        self.assertEqual(self.qa.status()['question_count'], 1)

    def test_inactive_and_structural_only_head_cannot_accept_questions(self):
        with self.assertRaises(UploadError): self.qa.accept(str(uuid4()), '질문')
        saved = self.qa.questions.save_target(candidate(self.uploads.library()['library_id']))
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE question_head SET revision=1,target_id=?', (saved['target_id'],))
        with self.assertRaises(UploadError): self.qa.accept(str(uuid4()), '아직 검증 안 된 대상')
        self.assertEqual(self.qa.status()['question_count'], 0)

    def test_duplicate_key_preserves_frozen_head_after_revision_switch(self):
        old = self.active_fixture(); key = str(uuid4())
        question = self.qa.accept(key, '첫 질문')
        body = self.qa.target(self.qa_plan, 2); target = self.qa.questions.save_target(body)
        with self.uploads.connect(write=True) as db: db.execute('UPDATE question_head SET revision=2,target_id=?', (target['target_id'],))
        self.assertEqual(self.qa.accept(key, '첫 질문'), question)
        self.assertEqual(self.qa.by_request(key)['target_id'], old['target_id'])
        self.assertEqual(self.qa.accept(str(uuid4()), '두 번째 질문')['head_revision'], 2)
        with self.assertRaises(UploadError): self.qa.accept(key, '변경한 질문')

    def test_dispatch_and_read_only_observe_keep_old_head_and_source(self):
        self.active_fixture(); question = self.qa.accept(str(uuid4()), '<질문>&\u2028')
        identifier = question['question_id']
        self.assertEqual(self.qa.dispatch(identifier, self.fake)['state'], 'waiting')
        self.fake.finish()
        target = self.qa.questions.save_target(self.qa.target(self.qa_plan, 2))
        with self.uploads.connect(write=True) as db: db.execute('UPDATE question_head SET revision=2,target_id=?', (target['target_id'],))
        self.fake.inference_enabled = False; self.fake.writes_enabled = False
        result = self.qa.observe_question(identifier, self.fake)
        self.assertEqual(result['state'], 'complete'); self.assertEqual(result['head_revision'], 1)
        self.assertIn('citations', result['result']); self.assertFalse(result['semantic_quality_verified'])
        self.assertEqual(QAAgentStore(UploadStore(self.root)).question(identifier), result)

    def test_new_document_and_index_do_not_change_already_accepted_question(self):
        old = self.active_fixture(); question = self.qa.accept(str(uuid4()), '기존 자료 질문')
        second = self.ready_job('문서 B', b'different document B')
        self.indexing.activate(second['job_id'], self.knowledge)
        self.assertNotEqual(self.indexing.head()['version_id'], old['body']['index_version_id'])
        self.assertEqual(self.qa.dispatch(question['question_id'], self.fake)['state'], 'waiting')
        run = self.fake.finish()
        self.assertEqual(run['result']['identity']['knowledge']['index_version_id'], old['body']['index_version_id'])
        self.assertEqual(self.qa.observe_question(question['question_id'], self.fake)['state'], 'complete')
        with self.assertRaises(UploadError): self.qa.plan(self.plan_id, self.selected)

    def test_lost_run_response_does_not_replay_without_retry_or_change_key(self):
        self.active_fixture(); identifier = self.qa.accept(str(uuid4()), '질문')['question_id']
        self.fake.lose.add('/api/agent-runs')
        self.assertEqual(self.qa.dispatch(identifier, self.fake)['state'], 'confirmation_required')
        old = self.fake.posts('/api/agent-runs')[-1]
        count = len(self.fake.posts('/api/agent-runs')); self.qa.dispatch(identifier, self.fake)
        self.assertEqual(len(self.fake.posts('/api/agent-runs')), count)
        self.fake.lose.clear(); self.qa.dispatch(identifier, self.fake, retry=True)
        self.assertEqual(self.fake.posts('/api/agent-runs')[-1][2:], old[2:])

    def test_failed_question_preserved_after_new_target_and_not_retried(self):
        self.active_fixture(); identifier = self.qa.accept(str(uuid4()), '질문')['question_id']
        self.qa.dispatch(identifier, self.fake); self.fake.finish('failed')
        self.assertEqual(self.qa.observe_question(identifier, self.fake)['state'], 'failed')
        count = len(self.fake.posts('/api/agent-runs')); self.qa.dispatch(identifier, self.fake, retry=True)
        self.assertEqual(len(self.fake.posts('/api/agent-runs')), count)
        self.assertEqual(self.qa.question(identifier)['error_code'], 'MODEL_TIMEOUT')

    def test_historical_question_survives_later_Agent_version_readback_failure(self):
        self.active_fixture(); identifier = self.qa.accept(str(uuid4()), '질문')['question_id']
        self.qa.dispatch(identifier, self.fake); self.fake.finish()
        original = self.qa.observe_question(identifier, self.fake)
        version = original['agent_version_id']
        self.fake.get_errors.add('/api/agent-versions/' + version)
        self.assertEqual(self.qa.prepare(self.qa_plan, self.fake)['state'], 'verified')
        self.assertEqual(self.qa._step(self.qa_plan, 'agent.agent.version')['state'], 'attention_required')
        self.assertEqual(self.qa.question(identifier), original)
        self.assertEqual(self.qa.observe_question(identifier, self.fake), original)

    def test_question_requires_explicit_inference_before_any_remote_request(self):
        self.active_fixture(); identifier = self.qa.accept(str(uuid4()), '질문')['question_id']
        self.fake.inference_enabled = False; count = len(self.fake.calls)
        with self.assertRaises(UploadError): self.qa.dispatch(identifier, self.fake)
        self.assertEqual(len(self.fake.calls), count)

    def test_question_limit_refuses_before_acceptance_without_losing_prior(self):
        self.active_fixture()
        with patch.object(schema, 'MAX_QUESTIONS', 1):
            question = self.qa.accept(str(uuid4()), '첫 질문')
            with self.assertRaises(UploadError): self.qa.accept(str(uuid4()), '둘째 질문')
            self.assertEqual(self.qa.question_list()['items'], [question])


class ResultTests(Fixture):
    def test_model_no_evidence_is_verified_with_selected_sources_and_usage(self):
        self.prepared(); self.qa.test(self.qa_plan, self.fake, '결론은?')
        run = self.fake.finish(no_evidence=True)
        self.assertTrue(run['result']['selected_chunk_ids'])
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'verified')

    def start_run(self):
        self.prepared(); self.qa.test(self.qa_plan, self.fake, '결론은?')
        return self.fake.finish()

    def test_wrong_result_identities_and_foreign_parent_refused(self):
        run = self.start_run(); original = copy.deepcopy(run)
        for field in ('agent_run_id', 'agent_version_id', 'invocation_key', 'definition_sha256', 'input_sha256', 'mode'):
            run.clear(); run.update(copy.deepcopy(original)); run[field] = str(uuid4())
            self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required', field)
        run.clear(); run.update(original)
        child = self.fake.resources['/api/tool-calls/' + run['tool_call_id']]
        child['parent_agent_run_id'] = str(uuid4())
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required')

    def test_wrong_embedding_and_chat_pins_refused(self):
        run = self.start_run(); original = copy.deepcopy(run)
        for field in ('model_version_id', 'deployment_id', 'served_model_name'):
            run.clear(); run.update(copy.deepcopy(original)); run['result']['identity']['model'][field] = str(uuid4())
            self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required', field)
        run.clear(); run.update(original)
        child = self.fake.resources['/api/tool-calls/' + run['tool_call_id']]
        child['result']['metadata']['embedding_profile']['model_version_id'] = str(uuid4())
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required')

    def test_invented_citation_text_document_location_and_reference_refused(self):
        run = self.start_run(); original = copy.deepcopy(run)
        for field in ('text', 'canonical_id', 'source_document_id', 'source_spans'):
            run.clear(); run.update(copy.deepcopy(original))
            run['result']['citations'][0]['source'][field] = [] if field == 'source_spans' else 'invented'
            self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required', field)
        for answer in ('인용 없음', '가짜 [S2]', '[S01]', '[S' + '9' * 10000 + ']'):
            run.clear(); run.update(copy.deepcopy(original)); run['result']['answer'] = answer
            self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required')

    def test_context_usage_partial_search_and_empty_failure_refused(self):
        run = self.start_run(); original = copy.deepcopy(run)
        mutations = (('selected_chunk_ids', []), ('usage', {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 9}),
                     ('citations', []), ('answer_truncated', True), ('outcome', 'failed'), ('warnings', ['invented']))
        for field, value in mutations:
            run.clear(); run.update(copy.deepcopy(original)); run['result'][field] = value
            self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required', field)
        run.clear(); run.update(original)
        child = self.fake.resources['/api/tool-calls/' + run['tool_call_id']]
        child['result']['completion'] = 'partial'
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required')

    def test_local_result_bound_and_no_evidence_without_sources(self):
        run = self.start_run(); run['result']['extra'] = 'x' * MAX_RESULT_BYTES
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'attention_required')
        run = self.fake.finish(empty=True)
        self.assertEqual(self.qa.observe(self.qa_plan, self.fake)['state'], 'verified')


class PureAndTransportTests(unittest.TestCase):
    def test_question_limits_blank_NUL_invalid_unicode(self):
        for value in ('', ' ', '\0', '가' * 3000, '\ud800', None):
            with self.subTest(value=str(value)[:20]), self.assertRaises(UploadError): question_checked(value)
        self.assertEqual(question_checked('a' * 8192), 'a' * 8192)

    def test_selection_rejects_credentials_unknown_bounds_and_bool(self):
        base = {'chat_connection': {'version_id': str(uuid4()), 'base_url': 'http://model:8000'},
                'chat_deployment_id': str(uuid4()), 'chat_model_version_id': str(uuid4())}
        for field, value in (('token', 'secret'), ('max_tokens', True), ('temperature', float('nan')), ('context_max_chars', 12001)):
            with self.assertRaises(UploadError): selection_checked({**base, field: value})

    def test_gateway_default_does_not_enable_runs_and_restricts_paths(self):
        gate = AgentQAGateway({'url': FakeQA.origin})
        body = {'agent_version_id': str(uuid4()), 'input': {'question': '질문'}, 'mode': 'run'}
        with self.assertRaises(UploadError): gate.request('POST', '/api/agent-runs', body, str(uuid4()))
        self.assertEqual(gate.calls, 0)
        for path in ('/api/agents', '/api/agent-runs?limit=100', '/api/agent-runs/../secrets', '/api/tool-calls'):
            with self.assertRaises(UploadError): request_checked('GET', path, None, None)

    def test_gateway_actual_loopback_key_and_receipt(self):
        seen = []
        run_id, version, key = str(uuid4()), str(uuid4()), str(uuid4())
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                seen.append((self.path, self.headers.get('Idempotency-Key'), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                raw = encoded({'agent_run_id': run_id, 'agent_version_id': version, 'state': 'accepted', 'status_url': '/api/agent-runs/' + run_id})
                self.send_response(202); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        with running(ThreadingHTTPServer(('127.0.0.1', 0), Handler)) as server:
            gate = AgentQAGateway({'url': 'http://127.0.0.1:' + str(server)}, execute=True, allow_inference=True)
            code, raw = gate.request('POST', '/api/agent-runs', {'agent_version_id': version, 'input': {'question': '질문'}, 'mode': 'run'}, key)
            self.assertEqual(code, 202); self.assertEqual(json.loads(raw)['agent_run_id'], run_id)
            self.assertEqual(seen[0][1], key)


class LocalHTTPTests(Fixture):
    def call(self, server, method, path, value=None, key=None, authorized=True):
        connection = http.client.HTTPConnection('127.0.0.1', server, timeout=3)
        headers = {'Authorization': 'Bearer ' + self.uploads.token_path.read_text().strip()} if authorized else {}
        if value is not None: headers['Content-Type'] = 'application/json'
        if key is not None: headers['Idempotency-Key'] = key
        connection.request(method, path, body=encoded(value) if value is not None else None, headers=headers)
        response = connection.getresponse(); result = response.status, json.loads(response.read()); connection.close()
        return result

    def test_authenticated_accept_replay_request_lookup_local_GET_and_dispatch(self):
        self.active_fixture(); key = str(uuid4())
        with running(make_server(self.uploads, object(), port=0, qa_agent=self.qa, agent_factory=lambda: self.fake)) as server:
            self.assertEqual(self.call(server, 'POST', '/api/questions', {'question': '질문'}, key, False)[0], 401)
            code, value = self.call(server, 'POST', '/api/questions', {'question': '질문'}, key)
            self.assertEqual(code, 202); identifier = value['question_id']; count = len(self.fake.calls)
            self.assertEqual(self.call(server, 'POST', '/api/questions', {'question': '질문'}, key)[1], value)
            self.assertEqual(self.call(server, 'GET', '/api/question-requests/' + key)[1], value)
            self.assertEqual(self.call(server, 'GET', '/api/questions/' + identifier)[1], value)
            self.assertEqual(len(self.fake.calls), count)
            self.assertEqual(self.call(server, 'POST', '/api/questions/' + identifier + '/dispatch', {})[0], 202)
            self.fake.finish()
            self.assertEqual(self.call(server, 'POST', '/api/questions/' + identifier + '/observe', {})[1]['state'], 'complete')
            self.assertFalse(self.call(server, 'GET', '/api/library')[1]['question_ready'])

    def test_empty_head_disabled_adapter_bad_fields_and_missing_key_block(self):
        with running(make_server(self.uploads, object(), port=0, qa_agent=self.qa)) as server:
            self.assertEqual(self.call(server, 'POST', '/api/questions', {'question': '질문'}, str(uuid4()))[0], 409)
            self.assertEqual(self.call(server, 'POST', '/api/questions', {'question': '질문', 'agent_version_id': str(uuid4())}, str(uuid4()))[0], 400)
            self.assertEqual(self.call(server, 'POST', '/api/questions', {'question': '질문'})[0], 400)
            self.assertEqual(self.fake.calls, [])
        with running(make_server(self.uploads, object(), port=0)) as server:
            self.assertEqual(self.call(server, 'GET', '/api/questions')[0], 503)


if __name__ == '__main__':
    unittest.main()
