"""Q06b-2 durable fixed QA adapter. CLI init/status is local and GPU-free.

Agent owns Agents, versions, runs and child Tool calls. This extension stores
immutable pins and keyed intents, never activates question_head or starts Flow.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid5

from . import readiness, qa_agent_schema as schema
from .contracts import ContractError, NAMESPACE, acceptance, fingerprint, integer, request_intent, strict_json, uid
from .intake import UploadError, check
from .qa_agent_results import (CHAT_PROFILE, agent_checked, definition, model_checked, question_checked,
                               run_checked, selection_checked, version_checked)
from .qa_agent_transport import request_checked
from .question_store import QuestionStore, question_lane, read_state
from .question_targets import checked_target
from .search_tool import SearchToolStore
from .search_tool_results import connection_checked, tool_checked, version_checked as tool_version_checked
from .uploads import UploadStore, file_lock, now
from .workflows import canonical

MAX_ATTEMPTS = 16
ERRORS = (UploadError, ContractError, readiness.ReadinessError, ValueError, TypeError, KeyError, AttributeError, UnicodeError)


@contextmanager
def agent_lane(root):
    try:
        with file_lock(root / 'qa-agent-lane.lock'):
            yield
    except UploadError as exc:
        if exc.code == 'UPLOAD_BUSY':
            raise UploadError('QA_AGENT_BUSY', 409) from None
        raise


def error_code(exc):
    return exc.code if hasattr(exc, 'code') else 'QA_AGENT_READBACK_INVALID'


class QAAgentStore:
    def __init__(self, uploads):
        self.questions, self.tools = QuestionStore(uploads), SearchToolStore(uploads)
        self.uploads, self.root = uploads, uploads.root
        with uploads.connect() as db:
            check(schema.present(db), 'QA_AGENT_NOT_INITIALIZED', 503)

    @classmethod
    def initialize(cls, uploads):
        QuestionStore(uploads)  # Never silently initialize or replace prerequisite state.
        with agent_lane(uploads.root), question_lane(uploads.root):
            with uploads.connect(write=True) as db:
                read_state(db)
                if not schema.present(db):
                    schema.install(db)
                schema.present(db)
        return cls(uploads)

    def plan(self, tool_plan_id, selection):
        selected = selection_checked(selection)
        with agent_lane(self.root):
            source, connections = self.tools._plan(tool_plan_id)
            published = self.tools._step(tool_plan_id, 'agent.tool.publish')
            tested = self.tools._step(tool_plan_id, 'agent.tool_call')
            check(connections is not None and published and published['state'] == 'verified' and published['receipt']
                  and tested and tested['state'] == 'verified', 'QA_AGENT_PUBLISHED_TOOL_REQUIRED', 409)
            tool = tool_version_checked(published['receipt'], source, published['receipt']['tool_id'])
            check(tool['state'] == 'published', 'QA_AGENT_PUBLISHED_TOOL_REQUIRED', 409)
            payload = {'schema': 'qa-agent-plan-v1', 'library_id': source['library_id'], 'source': source,
                       'tool': tool, 'connections': connections, 'selection': selected}
            identifier = str(uuid5(NAMESPACE, 'q06b2:plan:' + fingerprint(payload)))
            body = {**payload, 'plan_id': identifier}
            with self.uploads.connect(write=True) as db:
                row = db.execute('SELECT body FROM qa_agent_plans WHERE plan_id=?', (identifier,)).fetchone()
                if row:
                    check(row[0] == canonical(body), 'QA_AGENT_PLAN_CONFLICT', 409)
                else:
                    check(db.execute('SELECT count(*) FROM qa_agent_plans').fetchone()[0] < schema.MAX_PLANS,
                          'QA_AGENT_PLAN_LIMIT_EXCEEDED', 409)
                    db.execute('INSERT INTO qa_agent_plans VALUES(?,?,?,?)', (identifier, canonical(body), fingerprint(body), now()))
        return self.status(identifier)

    def _plan(self, identifier, api=None, current=False):
        check(uid(identifier), 'QA_AGENT_PLAN_ID_INVALID')
        with self.uploads.connect() as db:
            row = db.execute('SELECT * FROM qa_agent_plans WHERE plan_id=?', (identifier,)).fetchone()
        check(row is not None, 'QA_AGENT_PLAN_NOT_FOUND', 404)
        body = strict_json(row['body'].encode())
        check(set(body) == {'schema', 'library_id', 'source', 'tool', 'connections', 'selection', 'plan_id'}
              and body['schema'] == 'qa-agent-plan-v1' and body['plan_id'] == identifier
              and body['library_id'] == self.uploads.library()['library_id']
              and row['body_sha256'] == fingerprint(body) and row['body'] == canonical(body)
              and identifier == str(uuid5(NAMESPACE, 'q06b2:plan:' + fingerprint({k: v for k, v in body.items() if k != 'plan_id'})))
              and selection_checked(body['selection']) == body['selection'], 'QA_AGENT_PLAN_CHANGED', 409)
        source, connections = self.tools._plan(body['source']['plan_id'], current=current)
        check(source == body['source'] and connections == body['connections'], 'QA_AGENT_SOURCE_CHANGED', 409)
        tool = tool_version_checked(body['tool'], source, body['tool']['tool_id'])
        check(tool['state'] == 'published', 'QA_AGENT_PUBLISHED_TOOL_REQUIRED', 409)
        if api is not None:
            check(api.origin == source['selection']['agent_origin'], 'QA_AGENT_ORIGIN_CHANGED', 409)
        return body

    def _step(self, owner, operation):
        with self.uploads.connect() as db:
            row = db.execute('SELECT * FROM qa_agent_steps WHERE owner_id=? AND operation=?', (owner, operation)).fetchone()
        if row is None:
            return None
        result = dict(row)
        check(operation in ('agent.agent.create', 'agent.agent.version', 'agent.agent.publish', 'agent.run'),
              'QA_AGENT_OPERATION_INVALID', 409)
        for key in ('body', 'receipt', 'summary'):
            result[key] = strict_json(row[key].encode()) if row[key] else None
        intent = request_intent(result['intent_owner'], operation, 1, result['body'])
        check(uid(result['intent_owner']) and intent.key == result['request_key']
              and intent.request_sha256 == result['request_sha256'] and row['body'] == canonical(result['body'])
              and integer(row['attempts'], 0, MAX_ATTEMPTS), 'QA_AGENT_INTENT_CHANGED', 409)
        request_checked('POST', result['path'], result['body'], result['request_key'])
        if operation == 'agent.run' and result['receipt']:
            expected = {'agent_version_id': result['body']['agent_version_id']}
            r = result['receipt']
            checked = vars(acceptance('agent.run', 202, canonical({'agent_run_id': r['resource_id'],
                  'agent_version_id': r['version_id'], 'state': 'accepted', 'status_url': r['status_url']}).encode(), expected))
            check(checked == r, 'QA_AGENT_RECEIPT_CHANGED', 409)
        return result

    def _ensure(self, owner, operation, path, body, intent_owner=None):
        step = self._step(owner, operation)
        if step:
            check(step['path'] == path and step['body'] == body, 'QA_AGENT_REQUEST_CHANGED', 409)
            return step
        intent_owner = intent_owner or owner
        intent = request_intent(intent_owner, operation, 1, body)
        request_checked('POST', path, body, intent.key)
        with self.uploads.connect(write=True) as db:
            db.execute('INSERT INTO qa_agent_steps(owner_id,operation,intent_owner,path,body,request_sha256,request_key,state,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (owner, operation, intent_owner, path, canonical(body), intent.request_sha256, intent.key, 'pending', now()))
        return self._step(owner, operation)

    def _record(self, owner, operation, state, error=None, receipt=None, summary=None):
        step = self._step(owner, operation)
        check(step is not None, 'QA_AGENT_INTENT_REQUIRED', 409)
        check(receipt is None or step['receipt'] is None or canonical(step['receipt']) == canonical(receipt),
              'QA_AGENT_RECEIPT_CHANGED', 409)
        if operation == 'agent.run' and step['summary'] is not None and summary is not None:
            check(step['summary'] == summary, 'QA_AGENT_RESULT_CHANGED', 502)
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE qa_agent_steps SET state=?,error_code=?,receipt=coalesce(?,receipt),summary=coalesce(?,summary),updated_at=? WHERE owner_id=? AND operation=?',
                       (state, error, canonical(receipt) if receipt is not None else None,
                        canonical(summary) if summary is not None else None, now(), owner, operation))

    def _send(self, owner, api, step, code, validate, retry):
        if step['receipt'] or step['state'] in ('verified', 'failed') or step['attempts'] and not retry:
            return step
        check(getattr(api, 'writes_enabled', False) is True, 'AGENT_WRITE_NOT_AUTHORIZED', 409)
        check(step['operation'] != 'agent.run' or getattr(api, 'inference_enabled', False) is True,
              'QA_AGENT_INFERENCE_NOT_AUTHORIZED', 409)
        check(step['attempts'] < MAX_ATTEMPTS, 'QA_AGENT_REVIEW_REQUIRED', 409)
        with self.uploads.connect(write=True) as db:
            db.execute("UPDATE qa_agent_steps SET attempts=attempts+1,state='confirmation_required',error_code='QA_AGENT_OUTCOME_UNKNOWN',updated_at=? WHERE owner_id=? AND operation=?",
                       (now(), owner, step['operation']))
        try:
            actual, raw = api.request('POST', step['path'], step['body'], step['request_key'])
            check(type(actual) is int and actual == code, 'QA_AGENT_RECEIPT_INVALID', 502)
            self._record(owner, step['operation'], 'accepted', receipt=validate(actual, raw))
        except ERRORS as exc:
            self._record(owner, step['operation'], 'confirmation_required', error_code(exc))
        return self._step(owner, step['operation'])

    def _assets(self, plan, api):
        source = plan['source']
        create = self.tools._step(source['plan_id'], 'agent.tool.create')
        check(create and create['receipt'], 'QA_AGENT_TOOL_EVIDENCE_REQUIRED', 409)
        tool_checked(api.get('/api/tools/' + plan['tool']['tool_id']), create['body'], plan['tool']['tool_id'])
        tool = tool_version_checked(api.get('/api/tool-versions/' + plan['tool']['tool_version_id']), source,
                                   plan['tool']['tool_id'], plan['tool']['tool_version_id'], plan['tool'])
        check(tool['state'] == 'published', 'QA_AGENT_PUBLISHED_TOOL_REQUIRED', 409)
        for key in ('knowledge_connection', 'model_connection'):
            selected = source['selection'][key]
            actual = connection_checked(api.get('/api/connection-versions/' + selected['version_id']), selected)
            check(actual == plan['connections'][key], 'QA_AGENT_CONNECTION_CHANGED', 409)
        selected = plan['selection']['chat_connection']
        conn = connection_checked(api.get('/api/connection-versions/' + selected['version_id']), selected)
        body = {'action': 'resolve', 'connection_version_id': selected['version_id'],
                'deployment_id': plan['selection']['chat_deployment_id']}
        actual, raw = api.request('POST', '/api/agent-models/inspect', body,
                                str(uuid5(NAMESPACE, 'q06b2:inspect:' + plan['plan_id'])))
        check(actual == 200, 'QA_AGENT_MODEL_MISMATCH', 502)
        summary = {'connection': conn, 'model': model_checked(strict_json(raw), plan)}
        # Connection/Model readback is immutable and never creates an Agent intent.
        with self.uploads.connect(write=True) as db:
            row = db.execute('SELECT snapshot,snapshot_sha256 FROM qa_agent_chat WHERE plan_id=?', (plan['plan_id'],)).fetchone()
            if row:
                check(row[0] == canonical(summary) and row[1] == fingerprint(summary), 'QA_AGENT_CHAT_CONNECTION_CHANGED', 409)
            else:
                db.execute('INSERT INTO qa_agent_chat VALUES(?,?,?)', (plan['plan_id'], canonical(summary), fingerprint(summary)))

    def prepare(self, identifier, api, retry=False):
        with agent_lane(self.root):
            plan = self._plan(identifier, api, current=True)
            self._assets(plan, api)
            create_body = {'display_name': '문서 QA 답변 ' + plan['library_id'],
                           'description': '고정된 문서 QA 검색 버전으로 답변합니다.'}
            logical = str(uuid5(NAMESPACE, 'q06b2:agent:' + plan['library_id'] + ':' + api.origin))
            step = self._ensure(identifier, 'agent.agent.create', '/api/agents', create_body, logical)
            if not step['attempts'] and not step['receipt']:
                with self.uploads.connect() as db:
                    rows = db.execute("SELECT receipt FROM qa_agent_steps WHERE operation='agent.agent.create' AND state='verified' AND request_key=? AND owner_id!=?", (step['request_key'], identifier)).fetchall()
                if rows:
                    saved = agent_checked(strict_json(rows[0][0].encode()), create_body)
                    self._record(identifier, step['operation'], 'accepted', receipt=saved)
                    step = self._step(identifier, step['operation'])
            step = self._send(identifier, api, step, 201, lambda code, raw: agent_checked(strict_json(raw), create_body), retry)
            if not step['receipt']:
                return self.status(identifier)
            try:
                agent = agent_checked(api.get('/api/agents/' + step['receipt']['agent_id']), create_body, step['receipt']['agent_id'])
                self._record(identifier, step['operation'], 'verified')
                prior = self._step(identifier, 'agent.agent.version')
                body = prior['body'] if prior else {'expected_revision': agent['revision'], **definition(plan)}
                version = self._ensure(identifier, 'agent.agent.version', '/api/agents/' + agent['agent_id'] + '/versions', body)
                version = self._send(identifier, api, version, 201,
                    lambda code, raw: version_checked(strict_json(raw), plan, agent['agent_id']), retry)
                if version['receipt']:
                    readback = version_checked(api.get('/api/agent-versions/' + version['receipt']['agent_version_id']), plan,
                        agent['agent_id'], version['receipt']['agent_version_id'], version['receipt'])
                    self._record(identifier, version['operation'], 'verified', summary=readback)
            except ERRORS as exc:
                op = 'agent.agent.version' if self._step(identifier, 'agent.agent.version') else 'agent.agent.create'
                self._record(identifier, op, 'attention_required', error_code(exc))
            return self.status(identifier)

    def _version(self, plan, api=None, historical=False):
        step = self._step(plan['plan_id'], 'agent.agent.version')
        check(step and step['receipt'] and (historical or step['state'] == 'verified'), 'QA_AGENT_VERSION_REQUIRED', 409)
        saved = step['receipt']
        return version_checked(api.get('/api/agent-versions/' + saved['agent_version_id']) if api else saved,
                               plan, saved['agent_id'], saved['agent_version_id'], saved)

    def _observe_run(self, owner, plan, api):
        step = self._step(owner, 'agent.run')
        check(step and step['receipt'], 'QA_AGENT_RUN_RECEIPT_REQUIRED', 409)
        if step['state'] == 'failed':
            return
        try:
            version = self._version(plan, historical=True)
            body = api.get(step['receipt']['status_url'])
            child = None
            if body.get('state') == 'succeeded':
                result = body.get('result')
                check(isinstance(result, dict) and uid(result.get('tool_call_id')), 'QA_AGENT_CHILD_MISMATCH', 502)
                child = api.get('/api/tool-calls/' + result['tool_call_id'])
            state, summary = run_checked(body, plan, version, step, child)
            self._record(owner, 'agent.run', state, summary['error_code'] if state == 'failed' else None, summary=summary)
        except ERRORS as exc:
            self._record(owner, 'agent.run', 'attention_required', error_code(exc))

    def test(self, identifier, api, question, retry=False):
        question_checked(question)
        with agent_lane(self.root):
            plan = self._plan(identifier, api)
            prior = self._step(identifier, 'agent.run')
            if prior:
                check(prior['body']['input'] == {'question': question}, 'QA_AGENT_TEST_CHANGED', 409)
                if prior['receipt']:
                    self._observe_run(identifier, plan, api)
                    return self.status(identifier)
            check(getattr(api, 'inference_enabled', False) is True, 'QA_AGENT_INFERENCE_NOT_AUTHORIZED', 409)
            self._plan(identifier, current=True)
            self._assets(plan, api)
            version = self._version(plan, api)
            check(version['state'] == 'draft', 'QA_AGENT_DRAFT_REQUIRED', 409)
            body = {'agent_version_id': version['agent_version_id'], 'input': {'question': question}, 'mode': 'test'}
            step = self._ensure(identifier, 'agent.run', '/api/agent-runs', body)
            step = self._send(identifier, api, step, 202,
                lambda code, raw: vars(acceptance('agent.run', code, raw, {'agent_version_id': version['agent_version_id']})), retry)
            if step['receipt']:
                self._observe_run(identifier, plan, api)
            return self.status(identifier)

    def _observe_publish(self, plan, api):
        owner = plan['plan_id']
        step = self._step(owner, 'agent.agent.publish')
        try:
            version = self._version(plan, api)
            if step['attempts'] > 0 and version['state'] == 'published' and version['revision'] == step['body']['expected_revision'] + 1:
                self._record(owner, step['operation'], 'verified', summary=version)
            else:
                self._record(owner, step['operation'], 'confirmation_required', 'QA_AGENT_PUBLICATION_UNCONFIRMED')
        except ERRORS as exc:
            self._record(owner, step['operation'], 'attention_required', error_code(exc))

    def publish(self, identifier, api, retry=False):
        with agent_lane(self.root):
            plan = self._plan(identifier, api, current=True)
            self._observe_run(identifier, plan, api)
            check(self._step(identifier, 'agent.run')['state'] == 'verified', 'QA_AGENT_TEST_REQUIRED', 409)
            existing = self._step(identifier, 'agent.agent.publish')
            if existing:
                self._observe_publish(plan, api)
                refreshed = self._step(identifier, existing['operation'])
                if refreshed['state'] == 'verified' or refreshed['receipt'] or not retry:
                    return self.status(identifier)
            self._assets(plan, api)
            version = self._version(plan, api)
            check(version['state'] == 'draft', 'QA_AGENT_DRAFT_REQUIRED', 409)
            body = existing['body'] if existing else {'expected_revision': version['revision']}
            step = self._ensure(identifier, 'agent.agent.publish', '/api/agent-versions/' + version['agent_version_id'] + '/publish', body)
            self._send(identifier, api, step, 200,
                lambda code, raw: version_checked(strict_json(raw), plan, version['agent_id'], version['agent_version_id'], version), retry)
            self._observe_publish(plan, api)
            return self.status(identifier)

    def observe(self, identifier, api):
        with agent_lane(self.root):
            plan = self._plan(identifier, api)
            step = self._step(identifier, 'agent.run')
            if step and step['receipt']:
                self._observe_run(identifier, plan, api)
            if self._step(identifier, 'agent.agent.publish'):
                check(step and self._step(identifier, 'agent.run')['state'] == 'verified', 'QA_AGENT_TEST_REQUIRED', 409)
                self._observe_publish(plan, api)
            return self.status(identifier)

    def target(self, identifier, revision, historical=False):
        plan = self._plan(identifier)
        published = self._step(identifier, 'agent.agent.publish')
        tested = self._step(identifier, 'agent.run')
        check(published and published['attempts'] > 0 and published['summary']
              and tested and tested['summary'] and 'result' in tested['summary']
              and (historical or published['state'] == 'verified' and tested['state'] == 'verified'),
              'QA_AGENT_PUBLICATION_REQUIRED', 409)
        saved = self._version(plan, historical=historical)
        version = version_checked(published['summary'], plan, saved['agent_id'], saved['agent_version_id'], saved)
        check(version['state'] == 'published' and version['revision'] == published['body']['expected_revision'] + 1
              and tested['summary'].get('result_sha256') == fingerprint(tested['summary']['result'])
              and tested['receipt'] and tested['summary'].get('agent_run_id') == tested['receipt']['resource_id'],
              'QA_AGENT_PUBLICATION_REQUIRED', 409)
        source, s, tool = plan['source'], plan['selection'], plan['tool']
        embed = source['config']['embedding']
        return checked_target({'schema': 'question-target-v1', 'library_id': plan['library_id'], 'revision': revision,
            'indexing_generation': source['indexing_generation'], 'documents': source['documents'],
            'index_id': source['index_id'], 'index_version_id': source['index_version_id'],
            'embedding_deployment_id': embed['deployment_id'], 'embedding_space_id': embed['embedding_space_id'],
            'tool_version_id': tool['tool_version_id'], 'agent_version_id': version['agent_version_id'],
            'pins': {'embedding_model_version_id': embed['model_version_id'], 'embedding_profile_id': embed['profile_id'],
                'knowledge_connection_version_id': source['selection']['knowledge_connection']['version_id'],
                'embedding_connection_version_id': source['selection']['model_connection']['version_id'],
                'chat_connection_version_id': s['chat_connection']['version_id'], 'chat_deployment_id': s['chat_deployment_id'],
                'chat_model_version_id': s['chat_model_version_id'], 'chat_profile_id': CHAT_PROFILE,
                'tool_definition_sha256': tool['definition_sha256'], 'agent_definition_sha256': version['definition_sha256']}})

    def accept(self, client_key, question, head_gate=None):
        check(uid(client_key), 'QUESTION_CLIENT_KEY_INVALID')
        question_checked(question)
        request_hash = fingerprint({'question': question})
        with agent_lane(self.root):
            with self.uploads.connect(write=True) as db:
                existing = db.execute('SELECT question_id,request_sha256 FROM qa_agent_questions WHERE client_key=?', (client_key,)).fetchone()
                if existing:
                    check(existing['request_sha256'] == request_hash, 'QUESTION_CLIENT_KEY_CONFLICT', 409)
                    identifier = existing['question_id']
                else:
                    if head_gate is not None:
                        head_gate(db)  # Q06b-3 checks the activation receipt in this same transaction.
                    head, _ = read_state(db)
                    check(head['target'] is not None, 'QUESTION_HEAD_NOT_ACTIVE', 409)
                    target = head['target']
                    rows = db.execute('SELECT plan_id FROM qa_agent_plans ORDER BY plan_id').fetchall()
                    matches = []
                    for row in rows:
                        try:
                            if self.target(row['plan_id'], head['revision']) == target['body']:
                                matches.append(row['plan_id'])
                        except UploadError as exc:
                            if exc.code != 'QA_AGENT_PUBLICATION_REQUIRED':
                                raise
                    check(len(matches) == 1, 'QUESTION_AGENT_BINDING_UNVERIFIED', 409)
                    check(db.execute('SELECT count(*) FROM qa_agent_questions').fetchone()[0] < schema.MAX_QUESTIONS,
                          'QUESTION_LIMIT_EXCEEDED', 409)
                    identifier = str(uuid5(NAMESPACE, 'q06b2:question:' + head['library_id'] + ':' + client_key))
                    db.execute('INSERT INTO qa_agent_questions VALUES(?,?,?,?,?,?,?,?)',
                               (identifier, client_key, question, matches[0], target['target_id'], target['body_sha256'], request_hash, now()))
        return self.question(identifier)

    def _question(self, identifier):
        check(uid(identifier), 'QUESTION_ID_INVALID')
        with self.uploads.connect() as db:
            row = db.execute('SELECT * FROM qa_agent_questions WHERE question_id=?', (identifier,)).fetchone()
        check(row is not None, 'QUESTION_NOT_FOUND', 404)
        row = dict(row)
        question_checked(row['question'])
        target = self.questions.target(row['target_id'])
        check(uid(row['client_key']) and row['request_sha256'] == fingerprint({'question': row['question']})
              and target['body_sha256'] == row['target_sha256']
              and identifier == str(uuid5(NAMESPACE, 'q06b2:question:' + target['body']['library_id'] + ':' + row['client_key']))
              and self.target(row['plan_id'], target['body']['revision'], historical=True) == target['body'], 'QUESTION_RECORD_CHANGED', 409)
        return row, target['body']

    def by_request(self, client_key):
        check(uid(client_key), 'QUESTION_CLIENT_KEY_INVALID')
        with self.uploads.connect() as db:
            row = db.execute('SELECT question_id FROM qa_agent_questions WHERE client_key=?', (client_key,)).fetchone()
        check(row is not None, 'QUESTION_NOT_FOUND', 404)
        return self.question(row[0])

    def question_list(self):
        with self.uploads.connect() as db:
            rows = db.execute('SELECT question_id FROM qa_agent_questions ORDER BY created_at,question_id LIMIT ?',
                              (schema.MAX_QUESTIONS + 1,)).fetchall()
        check(len(rows) <= schema.MAX_QUESTIONS, 'QA_AGENT_STATE_LIMIT_EXCEEDED', 503)
        return {'items': [self.question(row[0]) for row in rows]}

    def question(self, identifier):
        row, target = self._question(identifier)
        step = self._step(identifier, 'agent.run')
        check(step is None or step['body'] == {'agent_version_id': target['agent_version_id'],
              'input': {'question': row['question']}, 'mode': 'run'}, 'QUESTION_REQUEST_CHANGED', 409)
        result = {'question_id': identifier, 'question': row['question'], 'target_id': row['target_id'],
                  'head_revision': target['revision'], 'agent_version_id': target['agent_version_id'],
                  'state': 'pending' if step is None else 'complete' if step['state'] == 'verified' else step['state'],
                  'agent_run_id': step['receipt']['resource_id'] if step and step['receipt'] else None,
                  'attempts': step['attempts'] if step else 0, 'error_code': step['error_code'] if step else None,
                  'semantic_quality_verified': False, 'question_ready': False}
        if step and step['state'] == 'verified':
            check(step['summary'] and step['summary']['result_sha256'] == fingerprint(step['summary']['result']),
                  'QA_AGENT_RESULT_CHANGED', 503)
            result['result'] = step['summary']['result']
        return result

    def dispatch(self, identifier, api, retry=False):
        with agent_lane(self.root):
            row, target = self._question(identifier)
            plan = self._plan(row['plan_id'], api)
            prior = self._step(identifier, 'agent.run')
            if prior and prior['receipt']:
                self._observe_run(identifier, plan, api)
                return self.question(identifier)
            check(getattr(api, 'inference_enabled', False) is True, 'QA_AGENT_INFERENCE_NOT_AUTHORIZED', 409)
            self._assets(plan, api)
            version = self._version(plan, api)
            check(version['state'] == 'published' and version['agent_version_id'] == target['agent_version_id'],
                  'QA_AGENT_PUBLICATION_REQUIRED', 409)
            body = {'agent_version_id': version['agent_version_id'], 'input': {'question': row['question']}, 'mode': 'run'}
            step = self._ensure(identifier, 'agent.run', '/api/agent-runs', body)
            step = self._send(identifier, api, step, 202,
                lambda code, raw: vars(acceptance('agent.run', code, raw, {'agent_version_id': version['agent_version_id']})), retry)
            if step['receipt']:
                self._observe_run(identifier, plan, api)
            return self.question(identifier)

    def observe_question(self, identifier, api):
        with agent_lane(self.root):
            row, _ = self._question(identifier)
            plan = self._plan(row['plan_id'], api)
            self._observe_run(identifier, plan, api)
            return self.question(identifier)

    def status(self, identifier=None):
        with self.uploads.connect() as db:
            check(schema.present(db), 'QA_AGENT_SCHEMA_INVALID', 503)
            plan_count = db.execute('SELECT count(*) FROM qa_agent_plans').fetchone()[0]
            question_count = db.execute('SELECT count(*) FROM qa_agent_questions').fetchone()[0]
        result = {**self.questions.status(), 'qa_agent_schema': schema.VERSION, 'agent_plan_count': plan_count,
                  'question_count': question_count, 'qa_agent_adapter_implemented': True,
                  'production_agent_verified': False, 'next_step': 'Q06b_3_question_head_CAS_activation'}
        if identifier:
            plan = self._plan(identifier)
            steps = [self._step(identifier, op) for op in ('agent.agent.create', 'agent.agent.version', 'agent.run', 'agent.agent.publish')]
            present = [s for s in steps if s]
            result.update(plan_id=identifier, tool_version_id=plan['tool']['tool_version_id'],
                agent_version_id=steps[1]['receipt']['agent_version_id'] if steps[1] and steps[1]['receipt'] else None,
                steps=[{k: s[k] for k in ('operation', 'state', 'attempts', 'error_code')} for s in present],
                state=present[-1]['state'] if present else 'planned',
                agent_binding_verified=bool(steps[3] and steps[3]['state'] == 'verified' and steps[2] and steps[2]['state'] == 'verified'))
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'status'))
    parser.add_argument('--state', default=str(Path.home() / '.local/state/agent-project-document-qa/uploads'))
    args = parser.parse_args(argv)
    try:
        uploads = UploadStore(args.state)
        store = QAAgentStore.initialize(uploads) if args.action == 'init' else QAAgentStore(uploads)
        result = store.status()
        result.update(result='passed', production_api_requests=0, inference_requested=False, GPU_requested=False)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False)); return 0
    except (UploadError, readiness.ReadinessError) as exc:
        code = exc.code
    except (OSError, sqlite3.Error, ContractError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        code = 'LOCAL_QA_AGENT_STATE_UNCERTAIN'
    print(json.dumps({'result': 'attention_required', 'code': code, 'question_ready': False,
                      'production_api_requests': 0, 'inference_requested': False, 'GPU_requested': False})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
