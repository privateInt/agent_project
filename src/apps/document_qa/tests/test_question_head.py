"""Private SQLite CAS, real process death, independent readbacks and loopback API."""
import copy
from contextlib import contextmanager, redirect_stdout
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

from src.apps.document_qa import api, question_head_schema as schema
from src.apps.document_qa.contracts import fingerprint
from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.question_head import QuestionHeadStore, main, request_checked
from src.apps.document_qa.qa_agent_transport import AgentQAGateway
from src.apps.document_qa.indexing_transport import KnowledgeIndexer
from src.apps.document_qa.uploads import UploadStore
from src.apps.document_qa.workflows import canonical
from src.apps.document_qa.tests.test_qa_agent import Fixture as QAFixture, FakeQA
from src.apps.document_qa.tests.test_question_store import candidate
from src.apps.document_qa.tests.test_search_tool import FakeAgent
from src.apps.document_qa.tests.test_refinement import encoded, running

PROJECT = Path(__file__).absolute().parents[4]


class Fixture(QAFixture):
    def setUp(self):
        super().setUp()
        self.head = QuestionHeadStore.initialize(self.uploads)
        self.key = str(uuid4())

    def activate(self, key=None, expected=0, plan=None):
        return self.head.activate(key or self.key, plan or self.qa_plan, expected, self.fake, self.knowledge)

    def active(self):
        self.published()
        return self.activate()

    def next_plan(self):
        # A new pinned chat selection produces a new AgentVersion on the same index/Tool.
        selected = copy.deepcopy(self.selected)
        selected['chat_connection']['version_id'] = str(uuid4())
        selected['chat_deployment_id'] = str(uuid4())
        selected['chat_model_version_id'] = str(uuid4())
        self.fake = FakeQA(self.fake, selected)
        identifier = self.qa.plan(self.plan_id, selected)['plan_id']
        self.qa.prepare(identifier, self.fake)
        self.qa.test(identifier, self.fake, '다음 질문')
        self.fake.finish()
        self.qa.observe(identifier, self.fake)
        self.qa.publish(identifier, self.fake)
        return identifier

    def snapshot(self):
        with self.uploads.connect() as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {n: [tuple(row) for row in db.execute('SELECT * FROM ' + n + ' ORDER BY rowid')] for n in names}

    def rejected(self, code=None, expected=0, plan=None):
        before = self.snapshot()
        with self.assertRaises(UploadError) as result:
            self.activate(str(uuid4()), expected, plan)
        if code:
            self.assertEqual(result.exception.code, code)
        self.assertEqual(self.snapshot(), before)


class StorageTests(Fixture):
    def test_additive_init_repeat_preserves_all_old_schema_rows_and_secret_bytes(self):
        before = self.snapshot()
        files = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file() and p.name != 'uploads.sqlite3'}
        with self.uploads.connect() as db:
            sql = list(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            sql = [tuple(row) for row in sql]
        for action in ('init', 'init', 'status'):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main([action, '--state', str(self.root)]), 0)
            result = json.loads(out.getvalue())
            self.assertEqual((result['head_revision'], result['activation_count']), (0, 0))
            self.assertTrue(result['head_activation_implemented'])
            self.assertFalse(result['question_ready'])
            self.assertFalse(result['GPU_requested'])
            self.assertEqual(result['production_api_requests'], 0)
        self.assertEqual(self.snapshot(), before)
        with self.uploads.connect() as db:
            self.assertEqual(sql, [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")])
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.root.iterdir() if p.name in files})
        self.assertEqual(self.fake.calls, [])

    def test_partial_or_unknown_head_schema_is_not_repaired(self):
        with self.uploads.connect(write=True) as db:
            db.execute('DROP TRIGGER qa_head_activation_no_delete')
        before = self.snapshot()
        with self.assertRaises(UploadError) as result:
            QuestionHeadStore.initialize(self.uploads)
        self.assertEqual(result.exception.code, 'QUESTION_HEAD_SCHEMA_INVALID')
        self.assertEqual(self.snapshot(), before)

    def test_prerequisite_agent_schema_is_required_without_silent_init(self):
        with self.uploads.connect(write=True) as db:
            db.execute('DROP TABLE qa_agent_metadata')
        with self.assertRaises(UploadError):
            QuestionHeadStore.initialize(self.uploads)
        with self.uploads.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='qa_agent_metadata'").fetchone())

    def test_structural_positive_head_cannot_be_adopted_as_verified(self):
        self.published()
        saved = self.qa.questions.save_target(self.qa.target(self.qa_plan, 1))
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE question_head SET revision=1,target_id=?', (saved['target_id'],))
        with self.assertRaises(UploadError) as result:
            QuestionHeadStore.initialize(self.uploads)
        self.assertEqual(result.exception.code, 'QUESTION_HEAD_ACTIVATION_UNVERIFIED')
        with self.assertRaises(UploadError):
            self.head.accept(str(uuid4()), '질문')

    def test_receipt_is_immutable_and_corruption_is_detected(self):
        self.active()
        for statement in ('UPDATE qa_head_activations SET target_sha256=?', 'DELETE FROM qa_head_activations WHERE client_key=?'):
            with self.assertRaises(sqlite3.IntegrityError), self.uploads.connect(write=True) as db:
                db.execute(statement, (self.key,))
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE qa_head_metadata SET migration_sha256=?', ('f' * 64,))
        with self.assertRaises(UploadError):
            self.head.by_request(self.key)

    def test_receipt_history_and_target_cannot_be_skipped_by_manual_head_update(self):
        self.active()
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE question_head SET revision=0,target_id=NULL')
        with self.assertRaises(UploadError) as result:
            self.head.status()
        self.assertEqual(result.exception.code, 'QUESTION_HEAD_ACTIVATION_UNVERIFIED')

    def test_request_rejects_boolean_negative_overflow_and_untrusted_plan(self):
        for value in (True, -1, 1.0, '0', None, (1 << 63) - 1):
            with self.subTest(value=value), self.assertRaises(UploadError):
                request_checked(self.key, self.qa_plan, value)
        for value in ('bad', '', '00000000-0000-0000-0000-000000000000'):
            with self.assertRaises(UploadError): request_checked(value, self.qa_plan, 0)

    def test_target_limit_failure_rolls_back_head_and_receipt(self):
        self.published()
        for revision in range(2, 102):
            self.qa.questions.save_target(self.qa.target(self.qa_plan, revision))
        self.rejected('QUESTION_TARGET_LIMIT_EXCEEDED')


class CASTests(Fixture):
    def test_activation_freezes_target_and_commits_one_receipt_without_remote_writes(self):
        self.published()
        before = self.snapshot()
        self.fake.calls.clear(); self.knowledge.calls.clear()
        result = self.activate()
        self.assertEqual(result['head_revision'], 1)
        self.assertIsNone(result['previous_target_id'])
        self.assertEqual(self.head.by_request(self.key), result)
        self.assertEqual(self.head.status()['activation_count'], 1)
        after = self.snapshot()
        for table in before:
            if table not in ('question_head', 'question_targets', 'qa_head_activations'):
                self.assertEqual(before[table], after[table], table)
        self.assertEqual([c[1] for c in self.fake.calls if c[0] == 'POST'], ['/api/agent-models/inspect'])
        self.assertTrue(all(c[0] == 'GET' for c in self.knowledge.calls))
        self.assertEqual(self.uploads.library()['question_revision'], 0)

    def test_same_key_replay_after_reopen_needs_no_remote_availability(self):
        result = self.active()
        reopened = QuestionHeadStore(UploadStore(self.root))
        class Unavailable:
            def __getattr__(self, name): raise AssertionError('remote access on replay')
        self.assertEqual(reopened.activate(self.key, self.qa_plan, 0, Unavailable(), Unavailable()), result)
        self.assertEqual(reopened.status()['head_revision'], 1)

    def test_old_key_after_later_head_returns_original_receipt(self):
        first = self.active()
        new = self.next_plan()
        second = self.activate(str(uuid4()), 1, new)
        self.assertEqual(second['previous_target_id'], first['target_id'])
        self.assertEqual(second['head_revision'], 2)
        self.assertEqual(self.activate(), first)
        self.assertEqual(self.head.status()['head_revision'], 2)

    def test_key_reuse_with_changed_request_is_conflict(self):
        self.active()
        before = self.snapshot()
        with self.assertRaises(UploadError) as result:
            self.activate(self.key, 1)
        self.assertEqual(result.exception.code, 'QUESTION_HEAD_CLIENT_KEY_CONFLICT')
        self.assertEqual(self.snapshot(), before)

    def test_stale_revision_is_rejected_before_remote_readback(self):
        self.active(); self.fake.calls.clear(); self.knowledge.calls.clear()
        self.rejected('QUESTION_HEAD_REVISION_CONFLICT')
        self.assertEqual(self.fake.calls + self.knowledge.calls, [])

    def test_same_assets_with_new_key_do_not_consume_revision(self):
        self.active(); self.fake.calls.clear()
        self.rejected('QUESTION_HEAD_TARGET_ALREADY_ACTIVE', expected=1)
        self.assertEqual(self.fake.calls, [])

    def test_unpublished_agent_and_structural_target_are_insufficient(self):
        self.qa.questions.save_target(candidate(library_id=self.uploads.library()['library_id']))
        self.rejected('QA_AGENT_PUBLICATION_REQUIRED')

    def test_question_retry_keeps_old_revision_and_new_question_uses_new_target(self):
        first = self.active(); key = str(uuid4())
        old = self.head.accept(key, '기존 질문')
        self.qa.dispatch(old['question_id'], self.fake)
        self.fake.finish()
        new = self.next_plan()
        second = self.activate(str(uuid4()), 1, new)
        self.assertEqual(self.head.accept(key, '기존 질문')['target_id'], first['target_id'])
        self.assertEqual(self.head.accept(str(uuid4()), '새 질문')['target_id'], second['target_id'])
        observed = self.qa.observe_question(old['question_id'], self.fake)
        self.assertEqual((observed['state'], observed['head_revision']), ('complete', 1))
        self.assertEqual(self.qa.question(old['question_id']), observed)

    def test_local_evidence_changed_during_inspection_refuses_commit(self):
        self.published()
        original = self.fake.request
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            with self.uploads.connect(write=True) as db:
                db.execute("UPDATE indexing_binding SET error_code='changed'")
            return result
        self.fake.request = changed
        with self.assertRaises(UploadError) as result: self.activate()
        self.assertEqual(result.exception.code, 'QUESTION_HEAD_LOCAL_EVIDENCE_CHANGED')
        self.assertEqual(self.head.status()['head_revision'], 0)
        self.assertEqual(self.head.status()['target_count'], 0)

    def test_parallel_activation_has_one_winner_and_preserves_request_lookup(self):
        self.published()
        entered, release = threading.Event(), threading.Event()
        original = self.fake.get
        def blocked(path):
            if path.startswith('/api/tool-versions/'):
                entered.set(); self.assertTrue(release.wait(3))
            return original(path)
        self.fake.get = blocked
        results = []
        def first():
            try: results.append(self.activate())
            except BaseException as exc: results.append(exc)
        worker = threading.Thread(target=first)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.rejected('QA_AGENT_BUSY')
        finally:
            release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(results[0], dict, results)
        self.assertEqual(self.head.status()['activation_count'], 1)

    def test_failed_new_candidate_preserves_existing_head_question_and_receipt(self):
        first = self.active(); old = self.head.accept(str(uuid4()), '남길 질문')
        new = self.next_plan()
        version = self.qa.target(new, 2)['agent_version_id']
        self.fake.resources['/api/agent-versions/' + version]['state'] = 'draft'
        self.rejected(expected=1, plan=new)
        self.assertEqual(self.head.by_request(self.key), first)
        self.assertEqual(self.qa.question(old['question_id']), old)

    def test_second_index_generation_keeps_all_documents_and_old_completed_question(self):
        first = self.active()
        old = self.head.accept(str(uuid4()), '첫 문서 질문')
        self.qa.dispatch(old['question_id'], self.fake); self.fake.finish()
        old = self.qa.observe_question(old['question_id'], self.fake)
        next_job = self.ready_job('두 번째 문서', b'second document')
        self.indexing.activate(next_job['job_id'], self.knowledge)
        tool_agent = FakeAgent(self.tools.candidate(), self.selection)
        tool_agent.resources.update({p: copy.deepcopy(v) for p, v in self.fake.resources.items()
                                     if not p.startswith(('/api/tool-calls/', '/api/agent-runs/'))})
        tool_plan = self.tools.plan(self.selection)['plan_id']
        self.tools.prepare(tool_plan, tool_agent)
        self.tools.test(tool_plan, tool_agent, '두 문서 질문'); tool_agent.finish()
        self.tools.observe(tool_plan, tool_agent); self.tools.publish(tool_plan, tool_agent)
        self.fake = FakeQA(tool_agent, self.selected)
        new = self.qa.plan(tool_plan, self.selected)['plan_id']
        self.qa.prepare(new, self.fake); self.qa.test(new, self.fake, '두 문서 질문')
        self.fake.finish(); self.qa.observe(new, self.fake); self.qa.publish(new, self.fake)
        result = self.activate(str(uuid4()), 1, new)
        target = self.qa.questions.target(result['target_id'])['body']
        self.assertEqual((target['revision'], target['indexing_generation'], len(target['documents'])), (2, 2, 2))
        self.assertNotEqual(target['index_version_id'], self.qa.questions.target(first['target_id'])['body']['index_version_id'])
        self.assertEqual(self.qa.question(old['question_id']), old)
        self.assertEqual(self.head.accept(str(uuid4()), '새 질문')['head_revision'], 2)


class ReadbackTests(Fixture):
    def setUp(self):
        super().setUp(); self.published()
        self.target = self.qa.target(self.qa_plan, 1)

    def test_wrong_knowledge_origin_is_refused(self):
        self.knowledge.origin = 'http://127.0.0.1:14501'
        self.rejected('QUESTION_HEAD_KNOWLEDGE_ORIGIN_CHANGED')

    def test_wrong_agent_origin_is_refused(self):
        self.fake.origin = 'http://127.0.0.1:15001'
        self.rejected('QA_AGENT_ORIGIN_CHANGED')

    def test_active_index_version_mismatch_is_refused(self):
        self.knowledge.index['active_version_id'] = str(uuid4())
        self.rejected('QUESTION_HEAD_INDEX_VERSION_CHANGED')

    def test_partial_or_failed_index_is_refused(self):
        version = self.knowledge.versions[self.target['index_version_id']]
        for state in ('indexing', 'failed'):
            version['state'] = state
            self.rejected()

    def test_full_canonical_snapshot_mismatch_is_refused(self):
        version = self.knowledge.versions[self.target['index_version_id']]
        version['input_spec']['documents'][0]['source_sha256'] = 'f' * 64
        self.rejected('INDEXING_INPUT_SPEC_MISMATCH')

    def test_embedding_model_and_chunker_change_are_refused(self):
        version = self.knowledge.versions[self.target['index_version_id']]
        original = copy.deepcopy(version)
        version['embedding_profile']['model_version_id'] = str(uuid4())
        self.rejected('INDEXING_PROFILE_CHANGED')
        version.clear(); version.update(original)
        version['input_spec']['chunker']['options']['overlap_characters'] += 1
        self.rejected('INDEXING_CHUNKER_CHANGED')

    def test_local_index_generation_changed_cannot_activate_old_agent(self):
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE indexing_head SET generation=generation+1')
        self.rejected()

    def test_tool_unpublished_or_definition_changed_is_refused(self):
        version = self.fake.resources['/api/tool-versions/' + self.target['tool_version_id']]
        version['state'] = 'draft'
        self.rejected()
        version['state'] = 'published'; version['definition_sha256'] = 'f' * 64
        self.rejected()

    def test_connection_credential_revision_changed_is_refused(self):
        version = self.target['pins']['knowledge_connection_version_id']
        self.fake.resources['/api/connection-versions/' + version]['credential_revision'] = 'v2'
        self.rejected('QUESTION_HEAD_CONNECTION_CHANGED')

    def test_tool_test_result_change_is_refused_even_when_still_succeeded(self):
        step = self.qa.tools._step(self.plan_id, 'agent.tool_call')
        self.fake.resources[step['receipt']['status_url']]['result']['data']['results'][0]['text'] = '바뀐 근거'
        self.rejected('QUESTION_HEAD_TOOL_TEST_CHANGED')

    def test_tool_test_error_or_waiting_state_is_refused(self):
        step = self.qa.tools._step(self.plan_id, 'agent.tool_call')
        call = self.fake.resources[step['receipt']['status_url']]
        call['state'] = 'running'
        self.rejected('QUESTION_HEAD_TOOL_TEST_CHANGED')

    def test_agent_disabled_or_model_pin_changed_is_refused(self):
        version = self.fake.resources['/api/agent-versions/' + self.target['agent_version_id']]
        agent = self.fake.resources['/api/agents/' + version['agent_id']]
        agent['enabled'] = False
        self.rejected('QA_AGENT_RESOURCE_MISMATCH')
        agent['enabled'] = True; version['model']['model_version_id'] = str(uuid4())
        self.rejected('QA_AGENT_VERSION_MISMATCH')

    def test_agent_test_child_parent_and_original_citation_mismatch_are_refused(self):
        step = self.qa._step(self.qa_plan, 'agent.run')
        run = self.fake.resources[step['receipt']['status_url']]
        child = self.fake.resources['/api/tool-calls/' + run['tool_call_id']]
        child['parent_agent_run_id'] = str(uuid4())
        self.rejected('QA_AGENT_CHILD_MISMATCH')
        child['parent_agent_run_id'] = run['agent_run_id']
        run['result']['citations'][0]['source']['text'] = '원문과 다른 내용'
        self.rejected()

    def test_chat_model_or_connection_revision_change_is_refused(self):
        self.fake.identity['model_version_id'] = str(uuid4())
        self.rejected('QA_AGENT_MODEL_MISMATCH')
        self.fake.identity['model_version_id'] = self.selected['chat_model_version_id']
        version = self.target['pins']['chat_connection_version_id']
        self.fake.resources['/api/connection-versions/' + version]['credential_revision'] = 'v2'
        self.rejected('QUESTION_HEAD_CHAT_CHANGED')

    def test_remote_error_does_not_write_head_target_or_receipt(self):
        self.fake.get_errors.add('/api/agent-versions/' + self.target['agent_version_id'])
        self.rejected('AGENT_TOOL_OUTCOME_UNKNOWN')

    def test_index_changed_during_chat_inspection_is_detected_by_final_readback(self):
        original = self.fake.request
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.knowledge.index['active_version_id'] = str(uuid4())
            return result
        self.fake.request = changed
        self.rejected('QUESTION_HEAD_INDEX_VERSION_CHANGED')

    def test_agent_retired_during_other_inspection_is_refused(self):
        original = self.fake.request
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.fake.resources['/api/agent-versions/' + self.target['agent_version_id']]['state'] = 'draft'
            return result
        self.fake.request = changed
        self.rejected('QUESTION_HEAD_AGENT_PUBLICATION_CHANGED')


class InterruptionTests(Fixture):
    def test_process_exit_during_schema_DDL_or_after_commit_recovers_without_reset(self):
        for mode in ('first_DDL', 'all_DDL', 'after_commit'):
            clone = self.root.parent / ('head-init-' + mode)
            shutil.copytree(self.root, clone)
            copied = UploadStore(clone)
            with copied.connect(write=True) as db:
                for name in reversed(tuple(schema.OBJECTS)):
                    db.execute('DROP ' + ('TRIGGER ' if 'no_' in name else 'TABLE ') + name)
            script = '''import os,sys
from src.apps.document_qa.question_head import QuestionHeadStore
from src.apps.document_qa import question_head_schema as schema
from src.apps.document_qa.uploads import UploadStore
mode=sys.argv[2];original=schema.install
def die(db):
 if mode=='first_DDL': db.execute(next(iter(schema.OBJECTS.values())))
 else: original(db)
 os._exit(79)
if mode!='after_commit': schema.install=die
QuestionHeadStore.initialize(UploadStore(sys.argv[1]))
os._exit(79)
'''
            child = subprocess.run([sys.executable, '-B', '-c', script, str(clone), mode], cwd=PROJECT, capture_output=True)
            self.assertEqual(child.returncode, 79, child.stderr)
            recovered = QuestionHeadStore.initialize(copied)
            self.assertEqual((recovered.status()['head_revision'], recovered.status()['activation_count']), (0, 0))

    def test_process_death_at_each_CAS_write_rolls_back_all_and_lost_commit_reply_is_lookupable(self):
        self.published()
        payload = {'agent': self.fake.resources, 'model': self.fake.identity, 'selection': self.selected,
                   'index': self.knowledge.index, 'versions': self.knowledge.versions}
        script = '''import os,sys,json
from contextlib import contextmanager
from src.apps.document_qa.question_head import QuestionHeadStore
from src.apps.document_qa.uploads import UploadStore
from src.apps.document_qa.workflows import canonical
p=json.load(open(sys.argv[2]));mode=sys.argv[3]
class Agent:
 origin='http://127.0.0.1:15000'
 def get(self,path): return p['agent'][path]
 def request(self,*args):
  s=p['selection'];return 200,canonical({'model':{'connection_version_id':s['chat_connection']['version_id'],'deployment_id':s['chat_deployment_id'],'model_version_id':s['chat_model_version_id']},'identity':p['model']}).encode()
class Knowledge:
 origin='http://127.0.0.1:14500'
 def get(self,path): return p['index'] if path.startswith('/api/indexes/') else p['versions'][path.split('/')[-1]]
original=UploadStore.connect
class Proxy:
 def __init__(self,db): self.db=db
 def execute(self,sql,*args):
  result=self.db.execute(sql,*args)
  wanted={'target':'INSERT INTO question_targets','head':'UPDATE question_head','receipt':'INSERT INTO qa_head_activations'}.get(mode)
  if wanted and sql.startswith(wanted): os._exit(79)
  return result
@contextmanager
def connect(self,write=False):
 with original(self,write) as db: yield Proxy(db)
UploadStore.connect=connect
QuestionHeadStore(UploadStore(sys.argv[1])).activate(sys.argv[4],sys.argv[5],0,Agent(),Knowledge())
os._exit(79)
'''
        metadata = self.root.parent / 'head-fake-readbacks.json'; metadata.write_text(json.dumps(payload))
        for mode in ('target', 'head', 'receipt', 'after_commit'):
            clone = self.root.parent / ('head-CAS-' + mode); shutil.copytree(self.root, clone)
            child = subprocess.run([sys.executable, '-B', '-c', script, str(clone), str(metadata), mode, self.key, self.qa_plan], cwd=PROJECT, capture_output=True)
            self.assertEqual(child.returncode, 79, child.stderr)
            recovered = QuestionHeadStore(UploadStore(clone))
            if mode == 'after_commit':
                self.assertEqual(recovered.by_request(self.key)['head_revision'], 1)
                self.assertEqual(recovered.activate(self.key, self.qa_plan, 0, None, None), recovered.by_request(self.key))
            else:
                self.assertEqual((recovered.status()['head_revision'], recovered.status()['target_count']), (0, 0))
                self.assertEqual(recovered.activate(self.key, self.qa_plan, 0, self.fake, self.knowledge)['head_revision'], 1)


class APITests(Fixture):
    def call(self, port, method, path, value=None, key=None, authorized=True, extra=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        headers = {'Authorization': 'Bearer ' + self.uploads.token_path.read_text().strip()} if authorized else {}
        if value is not None: headers['Content-Type'] = 'application/json'
        if key: headers['Idempotency-Key'] = key
        headers.update(extra or {})
        conn.request(method, path, body=encoded(value) if value is not None else None, headers=headers)
        response = conn.getresponse(); result = response.status, json.loads(response.read()); conn.close()
        return result

    def server(self, enable=True, inference=False):
        return api.make_server(self.uploads, object(), port=0, qa_agent=self.qa,
                               agent_factory=lambda: self.fake, question_head=self.head,
                               head_factory=(lambda: (self.fake, self.knowledge)) if enable else None,
                               question_inference_enabled=inference)

    def test_authenticated_head_activate_replay_and_request_lookup(self):
        self.published(); body = {'plan_id': self.qa_plan, 'expected_revision': 0}
        with running(self.server()) as port:
            self.assertEqual(self.call(port, 'POST', '/api/question-head/activate', body, self.key, False)[0], 401)
            code, result = self.call(port, 'POST', '/api/question-head/activate', body, self.key)
            self.assertEqual(code, 200, result)
            count = len(self.fake.calls) + len(self.knowledge.calls)
            self.assertEqual(self.call(port, 'POST', '/api/question-head/activate', body, self.key)[1], result)
            self.assertEqual(self.call(port, 'GET', '/api/question-head-requests/' + self.key)[1], result)
            self.assertEqual(len(self.fake.calls) + len(self.knowledge.calls), count)
            self.assertFalse(self.call(port, 'GET', '/api/library')[1]['question_ready'])

    def test_activation_disabled_and_malformed_requests_do_not_call_platforms(self):
        self.fake.calls.clear(); self.knowledge.calls.clear()
        body = {'plan_id': self.qa_plan, 'expected_revision': 0}
        with running(self.server(False)) as port:
            self.assertEqual(self.call(port, 'POST', '/api/question-head/activate', body, self.key)[0], 409)
        with running(self.server()) as port:
            for value in ({**body, 'verified': True}, {**body, 'expected_revision': True}, {'plan_id': 'bad', 'expected_revision': 0}):
                self.assertEqual(self.call(port, 'POST', '/api/question-head/activate', value, self.key)[0], 400)
            self.assertEqual(self.call(port, 'POST', '/api/question-head/activate', body)[0], 400)
            self.assertEqual(self.call(port, 'GET', '/api/question-head', authorized=False)[0], 401)
        self.assertEqual(self.fake.calls + self.knowledge.calls, [])

    def test_policy_local_acceptance_and_explicit_inference_are_separate(self):
        self.active(); key = str(uuid4())
        self.fake.calls.clear(); self.knowledge.calls.clear()
        with running(self.server()) as port:
            policy = self.call(port, 'GET', '/api/question-policy')[1]
            self.assertTrue(policy['head_active']); self.assertFalse(policy['question_ready'])
            code, question = self.call(port, 'POST', '/api/questions', {'question': '질문'}, key)
            self.assertEqual(code, 202)
            self.assertEqual(self.call(port, 'POST', '/api/questions/' + question['question_id'] + '/dispatch', {})[0], 409)
            self.assertEqual(self.fake.calls + self.knowledge.calls, [])
        with running(self.server(inference=True)) as port:
            self.assertTrue(self.call(port, 'GET', '/api/library')[1]['question_ready'])
            self.assertEqual(self.call(port, 'POST', '/api/questions/' + question['question_id'] + '/dispatch', {})[0], 202)
            self.fake.finish()
            self.assertEqual(self.call(port, 'POST', '/api/questions/' + question['question_id'] + '/observe', {})[1]['state'], 'complete')

    def test_HTTP_replay_precedes_factory_construction_after_response_loss(self):
        result = self.active()
        def unavailable(): raise AssertionError('transport setup on replay')
        with running(api.make_server(self.uploads, object(), port=0, qa_agent=self.qa,
                                     question_head=self.head, head_factory=unavailable)) as port:
            body = {'plan_id': self.qa_plan, 'expected_revision': 0}
            self.assertEqual(self.call(port, 'POST', '/api/question-head/activate', body, self.key), (200, result))

    def test_cli_serve_wires_initialized_stores_and_keeps_both_opt_ins_off(self):
        self.active()
        captured = {}
        class Server:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def serve_forever(self, **kwargs): raise KeyboardInterrupt()
        def capture(*args, **kwargs):
            captured['args'] = args; return Server()
        settings = {'platforms': {'data': {'url': 'http://127.0.0.1:12345'},
                                 'knowledge': {'url': self.knowledge.origin}, 'agent': {'url': self.fake.origin}}}
        out = io.StringIO()
        with patch.object(api, 'make_server', side_effect=capture), patch.object(api.readiness, 'load_settings', return_value=(settings, None, None)), redirect_stdout(out):
            self.assertEqual(api.main(['serve', '--state-dir', str(self.root)]), 0)
        self.assertFalse(json.loads(out.getvalue())['question_ready'])
        args = captured['args']
        self.assertIsNotNone(args[13]); self.assertIsNotNone(args[14]); self.assertIsNotNone(args[15])
        self.assertIsNone(args[16]); self.assertFalse(args[17])
        gateway = args[14]()
        self.assertFalse(gateway.writes_enabled); self.assertFalse(gateway.inference_enabled)
        out = io.StringIO()
        with patch.object(api, 'make_server', side_effect=capture), patch.object(api.readiness, 'load_settings', return_value=(settings, None, None)), redirect_stdout(out):
            self.assertEqual(api.main(['serve', '--state-dir', str(self.root), '--allow-question-inference']), 0)
        self.assertTrue(json.loads(out.getvalue())['question_ready'])
        args = captured['args']
        self.assertTrue(args[14]().inference_enabled)
        self.assertIsNone(args[16]); self.assertFalse(args[12].allow_inference)

    def test_cli_head_opt_in_cannot_enable_question_or_indexing_inference(self):
        captured = {}
        class Server:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def serve_forever(self, **kwargs): raise KeyboardInterrupt()
        def capture(*args): captured['args'] = args; return Server()
        settings = {'platforms': {'data': {'url': 'http://127.0.0.1:12345'},
                                 'knowledge': {'url': self.knowledge.origin}, 'agent': {'url': self.fake.origin}}}
        with patch.object(api, 'make_server', side_effect=capture), patch.object(api.readiness, 'load_settings', return_value=(settings, None, None)), redirect_stdout(io.StringIO()):
            self.assertEqual(api.main(['serve', '--state-dir', str(self.root), '--allow-question-head-activation']), 0)
        args = captured['args']; agent, knowledge = args[16]()
        self.assertFalse(agent.writes_enabled); self.assertFalse(agent.inference_enabled)
        self.assertFalse(knowledge.writes_enabled); self.assertFalse(knowledge.build_enabled)
        self.assertFalse(args[12].allow_inference); self.assertFalse(args[17])

    def test_cli_old_state_disables_new_routes_without_migrating_head_storage(self):
        with self.uploads.connect(write=True) as db:
            for name in reversed(tuple(schema.OBJECTS)):
                db.execute('DROP ' + ('TRIGGER ' if 'no_' in name else 'TABLE ') + name)
        captured = {}
        class Server:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def serve_forever(self, **kwargs): raise KeyboardInterrupt()
        def capture(*args): captured['args'] = args; return Server()
        settings = {'platforms': {'data': {'url': 'http://127.0.0.1:12345'},
                                 'knowledge': {'url': self.knowledge.origin}, 'agent': {'url': self.fake.origin}}}
        with patch.object(api, 'make_server', side_effect=capture), patch.object(api.readiness, 'load_settings', return_value=(settings, None, None)), redirect_stdout(io.StringIO()):
            self.assertEqual(api.main(['serve', '--state-dir', str(self.root)]), 0)
        self.assertEqual(captured['args'][13:17], (None, None, None, None))
        with self.uploads.connect() as db: self.assertFalse(schema.present(db))


class TransportTests(Fixture):
    def test_authenticated_readonly_Agent_and_Knowledge_HTTP_complete_activation(self):
        self.published()
        seen = {'agent': [], 'knowledge': []}
        fake, knowledge = self.fake, self.knowledge
        class AgentHandler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def respond(self, value):
                raw = encoded(value); self.send_response(200)
                self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                seen['agent'].append(('GET', self.path, self.headers.get('Authorization')))
                self.respond(fake.get(self.path))
            def do_POST(self):
                seen['agent'].append(('POST', self.path, self.headers.get('Authorization')))
                assert self.path == '/api/agent-models/inspect'
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                code, raw = fake.request('POST', self.path, body, self.headers['Idempotency-Key'])
                self.respond(json.loads(raw))
        class KnowledgeHandler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                seen['knowledge'].append(('GET', self.path, self.headers.get('Authorization')))
                raw = encoded(knowledge.get(self.path)); self.send_response(200)
                self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
        token = self.root / 'head-readback-token'; token.write_text('read-only-test-token'); token.chmod(0o600)
        with running(ThreadingHTTPServer(('127.0.0.1', 0), AgentHandler)) as agent_port, running(ThreadingHTTPServer(('127.0.0.1', 0), KnowledgeHandler)) as knowledge_port:
            ag = AgentQAGateway({'url': 'http://127.0.0.1:' + str(agent_port), 'token_file': str(token)})
            kn = KnowledgeIndexer({'url': 'http://127.0.0.1:' + str(knowledge_port), 'token_file': str(token)})
            # The transports retain their actual destinations. These adapter proxies
            # represent the configured host/internal URL distinction without rewriting immutable plans.
            class AgentProxy:
                origin = fake.origin
                def get(self, path): return ag.get(path)
                def request(self, *args): return ag.request(*args)
            class KnowledgeProxy:
                origin = knowledge.origin
                def get(self, path): return kn.get(path)
            result = self.head.activate(self.key, self.qa_plan, 0, AgentProxy(), KnowledgeProxy())
            self.assertEqual(result['head_revision'], 1)
            self.assertEqual(ag.calls, 13)
            self.assertEqual(kn.calls, 3)
            for calls in seen.values():
                self.assertTrue(all(c[2] == 'Bearer read-only-test-token' for c in calls))
            self.assertEqual([c[1] for c in seen['agent'] if c[0] == 'POST'], ['/api/agent-models/inspect'])
            self.assertTrue(all(c[0] == 'GET' for c in seen['knowledge']))


if __name__ == '__main__':
    unittest.main()
