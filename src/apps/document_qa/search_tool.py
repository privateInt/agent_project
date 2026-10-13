"""Q06a durable search Tool adapter. CLI init/status/candidate stays entirely local.

Agent owns Connection/Tool/ToolCall. Only references, request intents, immutable
definitions and compact verification summaries are stored here. No question head
migration, workflow dispatch, AgentRun, inference enablement or service control.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid5

from . import readiness
from .contracts import (ContractError, NAMESPACE, acceptance, fingerprint, integer,
                        request_intent, strict_json, uid)
from .indexing import IndexingStore, build_body, lane_lock
from .indexing_results import defaults_checked, snapshot_checked
from .intake import UploadError, check
from .search_tool_results import (binding, call_checked, connection_checked,
                                  selection_checked, tool_checked, version_checked)
from .uploads import UploadStore, file_lock, now
from .workflows import canonical

MAX_ATTEMPTS = 16
SCHEMA = '''
CREATE TABLE search_tool_metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1));
INSERT INTO search_tool_metadata VALUES(1,1);
CREATE TABLE search_tool_plans(
 plan_id TEXT PRIMARY KEY,indexing_generation INTEGER NOT NULL UNIQUE,
 body TEXT NOT NULL,body_sha256 TEXT NOT NULL,connection_snapshot TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE search_tool_steps(
 plan_id TEXT NOT NULL REFERENCES search_tool_plans(plan_id),operation TEXT NOT NULL,
 path TEXT NOT NULL,body TEXT NOT NULL,request_sha256 TEXT NOT NULL,request_key TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL
 CHECK(state IN ('pending','confirmation_required','accepted','waiting','verified','failed','attention_required')),
 receipt TEXT,summary TEXT,error_code TEXT,updated_at TEXT NOT NULL,
 PRIMARY KEY(plan_id,operation)
);
'''


@contextmanager
def tool_lane(root):
    try:
        with file_lock(root / 'search-tool-lane.lock'), lane_lock(root):
            yield
    except UploadError as exc:
        if exc.code in ('UPLOAD_BUSY', 'INDEXING_BUSY'):
            raise UploadError('SEARCH_TOOL_BUSY', 409) from None
        raise


class SearchToolStore:
    def __init__(self, uploads):
        self.uploads, self.root = uploads, uploads.root
        self.indexing = IndexingStore(uploads)
        with uploads.connect() as db:
            check(db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='search_tool_metadata'").fetchone()[0] == 1,
                  'SEARCH_TOOL_NOT_INITIALIZED', 503)
            row = db.execute('SELECT version FROM search_tool_metadata WHERE singleton=1').fetchone()
            check(row is not None and row[0] == 1, 'SEARCH_TOOL_SCHEMA_INVALID', 503)

    @classmethod
    def initialize(cls, uploads):
        IndexingStore(uploads)
        with file_lock(uploads.root / 'search-tool-init.lock'):
            with uploads.connect(write=True) as db:
                if not db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='search_tool_metadata'").fetchone()[0]:
                    for statement in SCHEMA.split(';'):
                        if statement.strip():
                            db.execute(statement)
        return cls(uploads)

    def candidate(self):
        head, registered = self.indexing.head(), self.indexing.binding()
        check(integer(head['generation'], 1) and uid(head['version_id']) and head['active_job_id'] is None
              and registered is not None and uid(registered['index_id']), 'SEARCH_TOOL_ACTIVE_INDEX_REQUIRED', 409)
        with self.uploads.connect() as db:
            rows = db.execute("SELECT * FROM indexing_tasks WHERE state='activated' AND receipt IS NOT NULL").fetchall()
        matches = [dict(row) for row in rows
                   if strict_json(row['receipt'].encode())['receipt']['resource_id'] == head['version_id']]
        check(len(matches) == 1, 'SEARCH_TOOL_INDEX_EVIDENCE_REQUIRED', 409)
        row = matches[0]
        config, documents = defaults_checked(strict_json(row['config'].encode())), snapshot_checked(head['documents'])
        expected_body = build_body(row['job_id'], documents, config, row['base_version_id'])
        intent = request_intent(row['job_id'], 'knowledge.build', row['base_generation'] + 1, expected_body)
        receipt = strict_json(row['receipt'].encode())
        activation = strict_json(row['activation_body'].encode()) if row['activation_body'] else {}
        check(row['base_generation'] + 1 == head['generation'] and row['index_id'] == registered['index_id']
              and row['knowledge_origin'] == registered['knowledge_origin'] and row['config'] == registered['config']
              and canonical(documents) == canonical(snapshot_checked(json.loads(row['documents'])))
              and row['request_body'] == canonical(expected_body) and row['request_sha256'] == intent.request_sha256
              and row['request_key'] == intent.key and uid(receipt['receipt'].get('operation_id'))
              and uid(receipt.get('chunking_run_id'))
              and activation.get('index_version_id') == head['version_id']
              and activation.get('expected_active_version_id') == row['base_version_id']
              and integer(activation.get('expected_revision'), 1),
              'SEARCH_TOOL_INDEX_EVIDENCE_MISMATCH', 409)
        return {'library_id': self.uploads.library()['library_id'], 'indexing_generation': head['generation'],
                'index_id': row['index_id'], 'index_version_id': head['version_id'],
                'indexing_job_id': row['job_id'], 'documents': documents, 'config': config}

    def plan(self, selection):
        selected = selection_checked(selection)
        with tool_lane(self.root):
            candidate = self.candidate()
            plan_id = str(uuid5(NAMESPACE, 'q06a:' + candidate['library_id'] + ':' + str(candidate['indexing_generation'])))
            body = {**candidate, 'selection': selected, 'plan_id': plan_id}
            with self.uploads.connect(write=True) as db:
                row = db.execute('SELECT * FROM search_tool_plans WHERE plan_id=?', (plan_id,)).fetchone()
                if row:
                    check(row['body'] == canonical(body) and row['body_sha256'] == fingerprint(body),
                          'SEARCH_TOOL_PLAN_CONFLICT', 409)
                else:
                    db.execute('INSERT INTO search_tool_plans(plan_id,indexing_generation,body,body_sha256,created_at) VALUES(?,?,?,?,?)',
                               (plan_id, candidate['indexing_generation'], canonical(body), fingerprint(body), now()))
        return self.status(plan_id)

    def _plan(self, plan_id, api=None, current=True):
        check(uid(plan_id), 'SEARCH_TOOL_PLAN_ID_INVALID')
        with self.uploads.connect() as db:
            row = db.execute('SELECT * FROM search_tool_plans WHERE plan_id=?', (plan_id,)).fetchone()
        check(row is not None, 'SEARCH_TOOL_PLAN_NOT_FOUND', 404)
        body = strict_json(row['body'].encode())
        check(set(body) == {'library_id', 'indexing_generation', 'index_id', 'index_version_id', 'indexing_job_id',
                           'documents', 'config', 'selection', 'plan_id'}
              and row['body_sha256'] == fingerprint(body) and row['body'] == canonical(body)
              and body['plan_id'] == plan_id and body['library_id'] == self.uploads.library()['library_id']
              and integer(body['indexing_generation'], 1) and row['indexing_generation'] == body['indexing_generation']
              and plan_id == str(uuid5(NAMESPACE, 'q06a:' + body['library_id'] + ':' + str(body['indexing_generation'])))
              and all(uid(body[k]) for k in ('index_id', 'index_version_id', 'indexing_job_id')),
              'SEARCH_TOOL_PLAN_CHANGED', 409)
        defaults_checked(body['config']); snapshot_checked(body['documents']); selection_checked(body['selection'])
        if api is not None:
            check(api.origin == body['selection']['agent_origin'], 'SEARCH_TOOL_TARGET_CHANGED', 409)
        if current:
            candidate = self.candidate()
            check(canonical({k: body[k] for k in candidate}) == canonical(candidate),
                  'SEARCH_TOOL_INDEX_HEAD_CHANGED', 409)
        return body, json.loads(row['connection_snapshot']) if row['connection_snapshot'] else None

    def _connections(self, plan, api):
        values = {key: connection_checked(api.get('/api/connection-versions/' + plan['selection'][key]['version_id']),
                                         plan['selection'][key]) for key in ('knowledge_connection', 'model_connection')}
        with self.uploads.connect(write=True) as db:
            row = db.execute('SELECT connection_snapshot FROM search_tool_plans WHERE plan_id=?', (plan['plan_id'],)).fetchone()
            check(not row[0] or row[0] == canonical(values), 'SEARCH_TOOL_CONNECTION_CHANGED', 409)
            if not row[0]:
                db.execute('UPDATE search_tool_plans SET connection_snapshot=? WHERE plan_id=?', (canonical(values), plan['plan_id']))
        # Resolve through Agent's pinned Connections. Host and internal addresses may differ.
        request = {'action': 'resolve', 'connection_version_id': values['knowledge_connection']['connection_version_id'],
                   'model_connection_version_id': values['model_connection']['connection_version_id'],
                   'index_id': plan['index_id'], 'index_version_id': plan['index_version_id']}
        code, raw = api.request('POST', '/api/knowledge/inspect', request,
                                str(uuid5(NAMESPACE, 'q06a:inspect:' + plan['plan_id'])))
        result = strict_json(raw)
        actual, expected = result.get('binding'), binding(plan)
        check(code == 200 and result.get('index_name') == 'document-qa-' + plan['library_id']
              and integer(result.get('chunk_count'), 1) and isinstance(actual, dict)
              and actual.get('connection_version_id') == expected['connection_version_id']
              and isinstance(actual.get('knowledge'), dict)
              and set(actual['knowledge']) == set(expected['knowledge'])
              and integer(actual['knowledge'].get('top_k'), 1, 50)
              and integer(actual['knowledge'].get('candidate_k'), actual['knowledge']['top_k'], 100)
              and all(actual['knowledge'].get(k) == v for k, v in expected['knowledge'].items()
                      if k not in ('top_k', 'candidate_k')),
              'SEARCH_TOOL_RESOLVE_MISMATCH', 502)

    def _step(self, plan_id, operation):
        with self.uploads.connect() as db:
            row = db.execute('SELECT * FROM search_tool_steps WHERE plan_id=? AND operation=?', (plan_id, operation)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ('body', 'receipt', 'summary'):
            result[key] = json.loads(result[key]) if result[key] else None
        owner = self._plan(plan_id, current=False)[0]['library_id'] if operation == 'agent.tool.create' else plan_id
        intent = request_intent(owner, operation, 1, result['body'])
        check(intent.key == result['request_key'] and intent.request_sha256 == result['request_sha256'],
              'SEARCH_TOOL_INTENT_CHANGED', 409)
        return result

    def _ensure(self, plan, operation, path, body):
        row = self._step(plan['plan_id'], operation)
        if row:
            check(row['path'] == path and canonical(row['body']) == canonical(body), 'SEARCH_TOOL_REQUEST_CHANGED', 409)
            return row
        owner = plan['library_id'] if operation == 'agent.tool.create' else plan['plan_id']
        intent = request_intent(owner, operation, 1, body)
        with self.uploads.connect(write=True) as db:
            db.execute('INSERT INTO search_tool_steps(plan_id,operation,path,body,request_sha256,request_key,state,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       (plan['plan_id'], operation, path, canonical(body), intent.request_sha256, intent.key, 'pending', now()))
        return self._step(plan['plan_id'], operation)

    def _record(self, plan_id, operation, state, error=None, receipt=None, summary=None):
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE search_tool_steps SET state=?,error_code=?,receipt=coalesce(?,receipt),summary=coalesce(?,summary),updated_at=? WHERE plan_id=? AND operation=?',
                       (state, error, canonical(receipt) if receipt is not None else None,
                        canonical(summary) if summary is not None else None, now(), plan_id, operation))

    def _send(self, plan, api, step, expected, validate, retry):
        if step['receipt'] or step['state'] in ('verified', 'failed') or step['attempts'] and not retry:
            return self._step(plan['plan_id'], step['operation'])
        check(getattr(api, 'writes_enabled', False) is True, 'AGENT_WRITE_NOT_AUTHORIZED', 409)
        check(step['operation'] != 'agent.tool_call' or getattr(api, 'inference_enabled', False) is True,
              'TOOL_INFERENCE_NOT_AUTHORIZED', 409)
        check(step['attempts'] < MAX_ATTEMPTS, 'SEARCH_TOOL_REVIEW_REQUIRED', 409)
        with self.uploads.connect(write=True) as db:
            db.execute("UPDATE search_tool_steps SET attempts=attempts+1,state='confirmation_required',error_code='AGENT_TOOL_OUTCOME_UNKNOWN',updated_at=? WHERE plan_id=? AND operation=?",
                       (now(), plan['plan_id'], step['operation']))
        try:
            code, raw = api.request('POST', step['path'], step['body'], step['request_key'])
            check(type(code) is int and code == expected, 'SEARCH_TOOL_RECEIPT_INVALID', 502)
            receipt = validate(code, raw)
            self._record(plan['plan_id'], step['operation'], 'accepted', receipt=receipt)
        except (UploadError, ValueError, TypeError, KeyError, AttributeError, readiness.ReadinessError) as exc:
            error = exc.code if hasattr(exc, 'code') else 'SEARCH_TOOL_RECEIPT_INVALID'
            self._record(plan['plan_id'], step['operation'], 'confirmation_required', error)
        return self._step(plan['plan_id'], step['operation'])

    def status(self, plan_id=None):
        with self.uploads.connect() as db:
            count = db.execute('SELECT count(*) FROM search_tool_plans').fetchone()[0]
        result = {'schema': 1, 'search_tool_schema': 1, 'upload_schema': 1, 'plan_count': count,
                  'search_tool_adapter_implemented': True, 'question_ready': False,
                  'production_tool_verified': False, 'next_step': 'Q06b_1_question_head_storage_migration'}
        if plan_id is not None:
            plan = self._plan(plan_id, current=False)[0]
            steps = [self._step(plan_id, op) for op in ('agent.tool.create', 'agent.tool.version', 'agent.tool_call', 'agent.tool.publish')]
            present = [s for s in steps if s]
            result.update(plan_id=plan_id, index_version_id=plan['index_version_id'],
                          indexing_generation=plan['indexing_generation'],
                          steps=[{k: s[k] for k in ('operation', 'state', 'attempts', 'error_code')} for s in present],
                          state=present[-1]['state'] if present else 'planned',
                          tool_id=steps[0]['receipt']['tool_id'] if steps[0] and steps[0]['receipt'] else None,
                          tool_version_id=steps[1]['receipt']['tool_version_id'] if steps[1] and steps[1]['receipt'] else None,
                          tool_binding_verified=bool(steps[3] and steps[3]['state'] == 'verified'))
        return result

    def prepare(self, plan_id, api, retry=False):
        with tool_lane(self.root):
            plan = self._plan(plan_id, api)[0]
            create_body = {'display_name': '문서 QA 검색 ' + plan['library_id'],
                           'description': '문서 QA 자료함의 고정 버전을 검색합니다.'}
            self._connections(plan, api)
            step = self._ensure(plan, 'agent.tool.create', '/api/tools', create_body)
            # Reuse the same logical Tool across indexing generations and process restarts.
            if not step['attempts'] and not step['receipt']:
                with self.uploads.connect() as db:
                    rows = db.execute("SELECT s.receipt,p.body FROM search_tool_steps s JOIN search_tool_plans p USING(plan_id) WHERE s.operation='agent.tool.create' AND s.state='verified' AND s.plan_id!=? AND s.request_key=? AND s.request_sha256=? AND p.connection_snapshot IS NOT NULL", (plan_id, step['request_key'], step['request_sha256'])).fetchall()
                for old in rows:
                    if json.loads(old[1])['selection']['agent_origin'] != api.origin:
                        continue
                    receipt = json.loads(old[0]); tool_checked(receipt, create_body)
                    self._record(plan_id, step['operation'], 'accepted', receipt=receipt)
                    break
                step = self._step(plan_id, step['operation'])
            step = self._send(plan, api, step, 201, lambda code, raw: tool_checked(strict_json(raw), create_body), retry)
            if not step['receipt']:
                return self.status(plan_id)
            try:
                tool = tool_checked(api.get('/api/tools/' + step['receipt']['tool_id']), create_body, step['receipt']['tool_id'])
                self._record(plan_id, step['operation'], 'verified')
                version_step = self._step(plan_id, 'agent.tool.version')
                body = version_step['body'] if version_step else {'expected_revision': tool['revision'],
                         'kind': 'knowledge_search_v1', 'binding': binding(plan), 'timeout_ms': plan['selection']['timeout_ms']}
                check(body['binding'] == binding(plan) and body['kind'] == 'knowledge_search_v1'
                      and body['timeout_ms'] == plan['selection']['timeout_ms'], 'SEARCH_TOOL_REQUEST_CHANGED', 409)
                version_step = self._ensure(plan, 'agent.tool.version', '/api/tools/' + tool['tool_id'] + '/versions', body)
                version_step = self._send(plan, api, version_step, 201,
                      lambda code, raw: version_checked(strict_json(raw), plan, tool['tool_id']), retry)
                if version_step['receipt']:
                    verified = version_checked(api.get('/api/tool-versions/' + version_step['receipt']['tool_version_id']),
                                               plan, tool['tool_id'], version_step['receipt']['tool_version_id'], version_step['receipt'])
                    self._record(plan_id, version_step['operation'], 'verified', summary=verified)
            except (UploadError, ValueError, TypeError, KeyError, AttributeError, readiness.ReadinessError) as exc:
                op = 'agent.tool.version' if self._step(plan_id, 'agent.tool.version') else 'agent.tool.create'
                self._record(plan_id, op, 'attention_required', exc.code if hasattr(exc, 'code') else 'SEARCH_TOOL_READBACK_INVALID')
            return self.status(plan_id)

    def _version(self, plan, api):
        step = self._step(plan['plan_id'], 'agent.tool.version')
        check(step is not None and step['receipt'] and step['state'] == 'verified', 'SEARCH_TOOL_DRAFT_REQUIRED', 409)
        return version_checked(api.get('/api/tool-versions/' + step['receipt']['tool_version_id']), plan,
                               step['receipt']['tool_id'], step['receipt']['tool_version_id'], step['receipt'])

    def test(self, plan_id, api, query, retry=False):
        with tool_lane(self.root):
            plan = self._plan(plan_id, api)[0]
            check(isinstance(query, str) and query.strip() and '\0' not in query
                  and len(query.encode()) <= 8192, 'QUESTION_INVALID')
            existing = self._step(plan_id, 'agent.tool_call')
            if existing:
                check(existing['body']['arguments'] == {'query': query}, 'SEARCH_TOOL_TEST_CHANGED', 409)
                if existing['receipt']:
                    return self._observe_test(plan, api)
            check(getattr(api, 'inference_enabled', False) is True, 'TOOL_INFERENCE_NOT_AUTHORIZED', 409)
            self._connections(plan, api)
            version = self._version(plan, api)
            check(version['state'] == 'draft', 'SEARCH_TOOL_DRAFT_REQUIRED', 409)
            body = {'tool_version_id': version['tool_version_id'], 'arguments': {'query': query}, 'mode': 'test'}
            step = self._ensure(plan, 'agent.tool_call', '/api/tool-calls', body)
            step = self._send(plan, api, step, 202, lambda code, raw: vars(acceptance('agent.tool_call', code, raw,
                               {'tool_version_id': version['tool_version_id']})), retry)
            return self._observe_test(plan, api) if step['receipt'] else self.status(plan_id)

    def _observe_test(self, plan, api):
        step = self._step(plan['plan_id'], 'agent.tool_call')
        check(step is not None and step['receipt'], 'SEARCH_TOOL_TEST_RECEIPT_REQUIRED', 409)
        if step['state'] == 'failed':
            return self.status(plan['plan_id'])
        try:
            version = self._version(plan, api)
            state, summary = call_checked(api.get(step['receipt']['status_url']), plan, version, step)
            self._record(plan['plan_id'], step['operation'], state,
                         'SEARCH_TOOL_TEST_FAILED' if state == 'failed' else None, summary=summary)
        except (UploadError, ValueError, TypeError, KeyError, AttributeError, readiness.ReadinessError) as exc:
            self._record(plan['plan_id'], step['operation'], 'attention_required',
                         exc.code if hasattr(exc, 'code') else 'SEARCH_TOOL_READBACK_INVALID')
        return self.status(plan['plan_id'])

    def _observe_publish(self, plan, api):
        step = self._step(plan['plan_id'], 'agent.tool.publish')
        try:
            version = self._version(plan, api)
            if version['state'] == 'published' and version['revision'] == step['body']['expected_revision'] + 1:
                self._record(plan['plan_id'], step['operation'], 'verified', receipt=version,
                             summary={'tool_version_id': version['tool_version_id'], 'definition_sha256': version['definition_sha256']})
            else:
                self._record(plan['plan_id'], step['operation'], 'confirmation_required', 'SEARCH_TOOL_PUBLICATION_UNCONFIRMED')
        except (UploadError, ValueError, TypeError, KeyError, AttributeError, readiness.ReadinessError) as exc:
            self._record(plan['plan_id'], step['operation'], 'attention_required',
                         exc.code if hasattr(exc, 'code') else 'SEARCH_TOOL_READBACK_INVALID')
        return self.status(plan['plan_id'])

    def publish(self, plan_id, api, retry=False):
        with tool_lane(self.root):
            plan = self._plan(plan_id, api)[0]
            test = self._step(plan_id, 'agent.tool_call')
            check(test is not None and test['state'] == 'verified', 'SEARCH_TOOL_TEST_REQUIRED', 409)
            self._observe_test(plan, api)
            check(self._step(plan_id, 'agent.tool_call')['state'] == 'verified', 'SEARCH_TOOL_TEST_REQUIRED', 409)
            existing = self._step(plan_id, 'agent.tool.publish')
            if existing:
                result = self._observe_publish(plan, api)
                if result['tool_binding_verified'] or not retry or self._step(plan_id, 'agent.tool.publish')['receipt']:
                    return result
            self._connections(plan, api)
            version = self._version(plan, api)
            check(version['state'] == 'draft', 'SEARCH_TOOL_DRAFT_REQUIRED', 409)
            body = existing['body'] if existing else {'expected_revision': version['revision']}
            step = self._ensure(plan, 'agent.tool.publish', '/api/tool-versions/' + version['tool_version_id'] + '/publish', body)
            self._send(plan, api, step, 200,
                       lambda code, raw: version_checked(strict_json(raw), plan, version['tool_id'], version['tool_version_id'], version), retry)
            return self._observe_publish(plan, api)

    def observe(self, plan_id, api):
        with tool_lane(self.root):
            plan = self._plan(plan_id, api, current=False)[0]
            if self._step(plan_id, 'agent.tool.publish'):
                self._observe_test(plan, api)
                check(self._step(plan_id, 'agent.tool_call')['state'] == 'verified', 'SEARCH_TOOL_TEST_REQUIRED', 409)
                return self._observe_publish(plan, api)
            test = self._step(plan_id, 'agent.tool_call')
            if test and test['receipt']:
                return self._observe_test(plan, api)
            version = self._step(plan_id, 'agent.tool.version')
            if version and version['receipt']:
                try:
                    checked = version_checked(api.get('/api/tool-versions/' + version['receipt']['tool_version_id']), plan,
                                              version['receipt']['tool_id'], version['receipt']['tool_version_id'], version['receipt'])
                    self._record(plan_id, version['operation'], 'verified', summary=checked)
                except (UploadError, ValueError, TypeError, KeyError, AttributeError, readiness.ReadinessError) as exc:
                    self._record(plan_id, version['operation'], 'attention_required',
                                 exc.code if hasattr(exc, 'code') else 'SEARCH_TOOL_READBACK_INVALID')
            return self.status(plan_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'status', 'candidate'))
    parser.add_argument('--state', default=str(Path.home() / '.local/state/agent-project-document-qa/uploads'))
    args = parser.parse_args(argv)
    try:
        uploads = UploadStore(args.state)
        store = SearchToolStore.initialize(uploads) if args.action == 'init' else SearchToolStore(uploads)
        result = store.candidate() if args.action == 'candidate' else store.status()
        # candidate reports references only; creating an operational plan is a later API integration.
        result.update(result='passed', question_ready=False, production_api_requests=0,
                      inference_requested=False, GPU_requested=False)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False)); return 0
    except (UploadError, readiness.ReadinessError) as exc:
        code = exc.code
    except (OSError, sqlite3.Error, UnicodeError, ContractError, ValueError, TypeError, KeyError, AttributeError):
        code = 'LOCAL_SEARCH_TOOL_STATE_UNCERTAIN'
    print(json.dumps({'result': 'attention_required', 'code': code, 'question_ready': False,
                      'production_api_requests': 0, 'inference_requested': False, 'GPU_requested': False})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
