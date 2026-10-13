"""Q05a explicit indexing adapter. CLI init/status/plan never calls a platform.

Functions are ready for Q05b's separate workflow; no existing API route dispatches
them. Knowledge alone owns indexes, chunking, embedding and activation. A local
indexing generation is not the library question revision (Q06 still follows).
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from . import readiness
from .contracts import ContractError, acceptance, request_intent, strict_json, uid
from .intake import UploadError, check
from .uploads import UploadStore, file_lock, now
from .refinement import RefinementStore
from .workflows import canonical
from .indexing_results import canonical_input, checked_index, checked_version, defaults_checked, snapshot_checked

MAX_ATTEMPTS = 16
SCHEMA = '''
CREATE TABLE indexing_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1));
INSERT INTO indexing_metadata VALUES(1,1);
CREATE TABLE indexing_binding (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), knowledge_origin TEXT NOT NULL, config TEXT NOT NULL,
 request_body TEXT NOT NULL, request_sha256 TEXT NOT NULL, request_key TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, index_id TEXT, error_code TEXT
);
CREATE TABLE indexing_head (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL DEFAULT 0,
 version_id TEXT NOT NULL DEFAULT '', documents TEXT NOT NULL DEFAULT '[]', active_job_id TEXT
);
INSERT INTO indexing_head(singleton) VALUES(1);
CREATE TABLE indexing_tasks (
 job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), index_id TEXT NOT NULL, knowledge_origin TEXT NOT NULL,
 config TEXT NOT NULL, base_generation INTEGER NOT NULL, base_version_id TEXT NOT NULL, base_documents TEXT NOT NULL,
 documents TEXT NOT NULL, request_body TEXT NOT NULL, request_sha256 TEXT NOT NULL, request_key TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('pending','confirmation_required','waiting','index_ready','activation_confirmation_required','activated','attention_required','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, receipt TEXT, activation_body TEXT,
 activation_attempts INTEGER NOT NULL DEFAULT 0, activation_history TEXT NOT NULL DEFAULT '[]',
 error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
'''


def build_body(job_id, items, config, parent):
    body = {'chunking': {'name': 'document-qa-' + job_id,
                         'canonical_ids': [value['canonical_id'] for value in items],
                         'chunker': {key: config['chunker'][key] for key in ('id', 'options')}},
            'embedding_deployment_id': config['embedding']['deployment_id']}
    if parent:
        body['parent_version_id'] = parent
    return body


@contextmanager
def lane_lock(root):
    try:
        with file_lock(root / 'indexing-lane.lock'):
            yield
    except UploadError as exc:
        if exc.code == 'UPLOAD_BUSY':
            raise UploadError('INDEXING_BUSY', 409) from None
        raise


class IndexingStore:
    def __init__(self, uploads):
        self.uploads, self.root = uploads, uploads.root
        self.refinement = RefinementStore(uploads)
        with uploads.connect() as db:
            check(db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='indexing_metadata'").fetchone()[0] == 1,
                  'INDEXING_NOT_INITIALIZED', 503)
            row = db.execute('SELECT version FROM indexing_metadata WHERE singleton=1').fetchone()
            check(row is not None and row[0] == 1, 'INDEXING_SCHEMA_INVALID', 503)

    @classmethod
    def initialize(cls, uploads):
        RefinementStore(uploads)
        with file_lock(uploads.root / 'indexing-init.lock'):
            with uploads.connect(write=True) as db:
                if not db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='indexing_metadata'").fetchone()[0]:
                    for statement in SCHEMA.split(';'):
                        if statement.strip():
                            db.execute(statement)
        return cls(uploads)

    def binding(self):
        with self.uploads.connect() as db:
            row = db.execute('SELECT * FROM indexing_binding WHERE singleton=1').fetchone()
        return dict(row) if row else None

    def head(self):
        with self.uploads.connect() as db:
            row = db.execute('SELECT * FROM indexing_head WHERE singleton=1').fetchone()
        check(row is not None, 'INDEXING_SCHEMA_INVALID', 503)
        return {**dict(row), 'documents': json.loads(row['documents'])}

    def row(self, job_id):
        self.uploads.job(job_id)
        with self.uploads.connect() as db:
            row = db.execute('SELECT * FROM indexing_tasks WHERE job_id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def job(self, job_id):
        row = self.row(job_id)
        if row is None:
            return {'job_id': job_id, 'state': 'not_requested', 'question_ready': False}
        receipt = json.loads(row['receipt']) if row['receipt'] else None
        return {'job_id': job_id, 'state': row['state'], 'attempts': row['attempts'],
                'activation_attempts': row['activation_attempts'], 'index_id': row['index_id'],
                'index_version_id': receipt['receipt']['resource_id'] if receipt else None,
                'document_count': len(json.loads(row['documents'])), 'error_code': row['error_code'],
                'indexing_ready': row['state'] in ('index_ready', 'activated'),
                'indexing_active': row['state'] == 'activated', 'question_ready': False,
                'checked_at': row['updated_at']}

    def status(self):
        with self.uploads.connect() as db:
            counts = {row[0]: row[1] for row in db.execute('SELECT state,count(*) FROM indexing_tasks GROUP BY state')}
        binding, head = self.binding(), self.head()
        return {'schema': 1, 'indexing_schema': 1, 'upload_schema': 1,
                'indexing_adapter_implemented': True, 'index_configured': bool(binding and binding['index_id']),
                'index_id': binding['index_id'] if binding else None, 'indexing_counts': counts,
                'active_job_id': head['active_job_id'], 'indexing_generation': head['generation'],
                'active_document_count': len(head['documents']), 'question_ready': False,
                'configuration_live_verified': False, 'n8n_indexing_connected': False,
                'production_indexing_verified': False, 'next_step': 'Q05b_indexing_workflow_without_GPU'}

    def plan(self, job_id):
        with lane_lock(self.root):
            head = self.head()
            check(head['active_job_id'] in (None, job_id), 'LIBRARY_INDEXING_BUSY', 409)
            job = self.uploads.job(job_id)
            item = canonical_input(job, self.refinement.job(job_id))
            items = [dict(value) for value in head['documents']]
            existing = [value for value in items if value['source_document_id'] == item['source_document_id']]
            check(not existing or existing == [item], 'INDEXING_SOURCE_ALREADY_BOUND', 409)
            if not existing:
                items.append(item)
            items = snapshot_checked(items)
            return {'job_id': job_id, 'base_generation': head['generation'], 'base_version_id': head['version_id'],
                    'documents': items, 'document_count': len(items), 'question_ready': False}

    def configure(self, api, defaults, retry=False):
        config = defaults_checked(defaults)
        body = {'name': 'document-qa-' + self.uploads.library()['library_id'],
                'embedding_deployment_id': config['embedding']['deployment_id']}
        intent = request_intent(self.uploads.library()['library_id'], 'knowledge.create', 1, body)
        with lane_lock(self.root):
            row = self.binding()
            if row:
                check(row['knowledge_origin'] == api.origin and row['config'] == canonical(config)
                      and row['request_body'] == canonical(body) and row['request_key'] == intent.key
                      and row['request_sha256'] == intent.request_sha256, 'INDEXING_BINDING_CHANGED', 409)
                if row['index_id'] or row['attempts'] and not retry:
                    return {'index_id': row['index_id'], 'error_code': row['error_code'], 'question_ready': False}
            check(getattr(api, 'writes_enabled', False) is True, 'KNOWLEDGE_WRITE_NOT_AUTHORIZED', 409)
            if not row:
                with self.uploads.connect(write=True) as db:
                    db.execute('INSERT INTO indexing_binding(singleton,knowledge_origin,config,request_body,request_sha256,request_key) VALUES(1,?,?,?,?,?)',
                               (api.origin, canonical(config), canonical(body), intent.request_sha256, intent.key))
                row = self.binding()
            check(row['attempts'] < MAX_ATTEMPTS, 'INDEXING_REVIEW_REQUIRED', 409)
            with self.uploads.connect(write=True) as db:
                db.execute("UPDATE indexing_binding SET attempts=attempts+1,error_code='INDEX_CREATE_OUTCOME_UNKNOWN' WHERE singleton=1")
            try:
                code, raw = api.request('POST', '/api/indexes', body, intent.key)
                value = strict_json(raw)
                check(code == 201, 'INDEX_CREATE_RECEIPT_INVALID', 502)
                checked_index(value, value.get('index_id'), config['embedding'], body['name'])
                check(value['active_version_id'] == '', 'INDEX_CREATE_RECEIPT_INVALID', 502)
            except (UploadError, ContractError) as exc:
                self.binding_error(exc.code if isinstance(exc, UploadError) else 'INDEX_CREATE_RECEIPT_INVALID')
                return {'index_id': None, 'error_code': self.binding()['error_code'], 'question_ready': False}
            with self.uploads.connect(write=True) as db:
                db.execute('UPDATE indexing_binding SET index_id=?,error_code=NULL WHERE singleton=1', (value['index_id'],))
            return {'index_id': value['index_id'], 'error_code': None, 'question_ready': False}

    def binding_error(self, code):
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE indexing_binding SET error_code=? WHERE singleton=1', (code,))

    def _checked_row(self, job_id, api):
        row, binding, head = self.row(job_id), self.binding(), self.head()
        check(row is not None and binding is not None and uid(binding['index_id']), 'INDEXING_NOT_REQUESTED', 409)
        config = defaults_checked(json.loads(row['config']))
        items = snapshot_checked(json.loads(row['documents']))
        body = json.loads(row['request_body'])
        intent = request_intent(job_id, 'knowledge.build', row['base_generation'] + 1, body)
        check(row['index_id'] == binding['index_id'] and row['config'] == binding['config']
              and api.origin == row['knowledge_origin'] == binding['knowledge_origin'], 'INDEXING_TARGET_CHANGED', 409)
        check(intent.key == row['request_key'] and intent.request_sha256 == row['request_sha256']
              and row['request_body'] == canonical(build_body(job_id, items, config, row['base_version_id'])),
              'INDEXING_INTENT_CHANGED', 409)
        if row['state'] not in ('activated', 'failed'):
            check(head['generation'] == row['base_generation'] and head['version_id'] == row['base_version_id']
                  and canonical(head['documents']) == row['base_documents'] and head['active_job_id'] == job_id,
                  'INDEXING_HEAD_CHANGED', 409)
        return row, config, {**row, 'documents': items}

    def _index(self, api, row, config):
        name = 'document-qa-' + self.uploads.library()['library_id']
        return checked_index(api.get('/api/indexes/' + row['index_id']), row['index_id'], config['embedding'], name)

    def dispatch(self, job_id, api, retry=False):
        self.uploads.job(job_id)
        with lane_lock(self.root):
            row = self.row(job_id)
            if row:
                self._checked_row(job_id, api)
                if row['receipt'] or row['state'] == 'failed' or row['attempts'] and not retry:
                    return self.job(job_id)
            check(getattr(api, 'build_enabled', False) is True, 'INDEXING_INFERENCE_NOT_AUTHORIZED', 409)
            if not row:
                head, binding = self.head(), self.binding()
                check(head['active_job_id'] is None, 'LIBRARY_INDEXING_BUSY', 409)
                check(binding is not None and uid(binding['index_id']), 'INDEXING_BINDING_REQUIRED', 409)
                check(binding['knowledge_origin'] == api.origin, 'INDEXING_TARGET_CHANGED', 409)
                config = defaults_checked(json.loads(binding['config']))
                item = canonical_input(self.uploads.job(job_id), self.refinement.job(job_id))
                check(not any(value['source_document_id'] == item['source_document_id'] for value in head['documents']),
                      'INDEXING_SOURCE_ALREADY_BOUND', 409)
                items = snapshot_checked([*head['documents'], item])
                index = self._index(api, binding, config)
                check(index['active_version_id'] == head['version_id'], 'INDEXING_ACTIVE_VERSION_CONFLICT', 409)
                body = build_body(job_id, items, config, head['version_id'])
                intent = request_intent(job_id, 'knowledge.build', head['generation'] + 1, body)
                with self.uploads.connect(write=True) as db:
                    stamp = now()
                    db.execute('INSERT INTO indexing_tasks(job_id,index_id,knowledge_origin,config,base_generation,base_version_id,base_documents,documents,request_body,request_sha256,request_key,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,\'pending\',?,?)',
                               (job_id, binding['index_id'], api.origin, binding['config'], head['generation'],
                                head['version_id'], canonical(head['documents']), canonical(items), canonical(body),
                                intent.request_sha256, intent.key, stamp, stamp))
                    db.execute('UPDATE indexing_head SET active_job_id=? WHERE singleton=1', (job_id,))
                row = self.row(job_id)
            check(row['attempts'] < MAX_ATTEMPTS, 'INDEXING_REVIEW_REQUIRED', 409)
            if row['attempts']:
                checked_row, config, _ = self._checked_row(job_id, api)
                index = self._index(api, checked_row, config)
                check(index['active_version_id'] == row['base_version_id'], 'INDEXING_ACTIVE_VERSION_CONFLICT', 409)
            with self.uploads.connect(write=True) as db:
                db.execute("UPDATE indexing_tasks SET state='confirmation_required',attempts=attempts+1,error_code='INDEX_BUILD_OUTCOME_UNKNOWN',updated_at=? WHERE job_id=?", (now(), job_id))
            try:
                code, raw = api.request('POST', '/api/indexes/' + row['index_id'] + '/builds',
                                        json.loads(row['request_body']), row['request_key'])
                receipt = acceptance('knowledge.build', code, raw, {'index_id': row['index_id']})
                value = strict_json(raw)
                saved = {'receipt': asdict(receipt), 'chunking_run_id': value['chunking_run_id']}
            except (UploadError, ContractError) as exc:
                self.error(job_id, exc.code if isinstance(exc, UploadError) else 'INDEX_BUILD_RECEIPT_INVALID', 'confirmation_required')
                return self.job(job_id)
            with self.uploads.connect(write=True) as db:
                db.execute("UPDATE indexing_tasks SET state='waiting',receipt=?,error_code=NULL,updated_at=? WHERE job_id=?", (canonical(saved), now(), job_id))
            return self.job(job_id)

    def error(self, job_id, code, state='attention_required'):
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE indexing_tasks SET state=?,error_code=?,updated_at=? WHERE job_id=?', (state, code, now(), job_id))
            if state == 'failed':
                db.execute('UPDATE indexing_head SET active_job_id=NULL WHERE singleton=1 AND active_job_id=?', (job_id,))

    def _version(self, api, row, config, checked):
        receipt = json.loads(row['receipt'])
        value = api.get('/api/index-versions/' + receipt['receipt']['resource_id'])
        return checked_version(value, receipt, checked, config)

    def observe(self, job_id, api):
        with lane_lock(self.root):
            row, config, checked = self._checked_row(job_id, api)
            if row['state'] in ('activated', 'failed') or not row['receipt']:
                return self.job(job_id)
            if row['activation_body']:
                return self._observe_activation(job_id, api, row, config, checked)
            try:
                result = self._version(api, row, config, checked)
            except (UploadError, ContractError) as exc:
                self.error(job_id, exc.code if isinstance(exc, UploadError) else str(exc))
            else:
                if result == 'failed':
                    self.error(job_id, 'KNOWLEDGE_BUILD_FAILED', 'failed')
                else:
                    with self.uploads.connect(write=True) as db:
                        db.execute('UPDATE indexing_tasks SET state=?,error_code=NULL,updated_at=? WHERE job_id=?',
                                   ('index_ready' if result == 'complete' else 'waiting', now(), job_id))
            return self.job(job_id)

    def _commit_activation(self, job_id, row):
        receipt = json.loads(row['receipt'])
        with self.uploads.connect(write=True) as db:
            changed = db.execute('UPDATE indexing_head SET generation=generation+1,version_id=?,documents=?,active_job_id=NULL WHERE singleton=1 AND generation=? AND version_id=? AND documents=? AND active_job_id=?',
                                 (receipt['receipt']['resource_id'], row['documents'], row['base_generation'],
                                  row['base_version_id'], row['base_documents'], job_id)).rowcount
            check(changed == 1, 'INDEXING_HEAD_CHANGED', 409)
            db.execute("UPDATE indexing_tasks SET state='activated',error_code=NULL,updated_at=? WHERE job_id=?", (now(), job_id))
        return self.job(job_id)

    def _observe_activation(self, job_id, api, row, config, checked):
        try:
            check(self._version(api, row, config, checked) == 'complete', 'INDEXING_NOT_READY', 409)
            index = self._index(api, row, config)
            body = json.loads(row['activation_body'])
            version = json.loads(row['receipt'])['receipt']['resource_id']
            if index['active_version_id'] == version and index['revision'] >= body['expected_revision'] + 1:
                return self._commit_activation(job_id, row)
            self.error(job_id, 'INDEXING_ACTIVATION_UNCONFIRMED', 'activation_confirmation_required')
        except (UploadError, ContractError) as exc:
            self.error(job_id, exc.code if isinstance(exc, UploadError) else str(exc), 'activation_confirmation_required')
        return self.job(job_id)

    def activate(self, job_id, api, retry=False):
        with lane_lock(self.root):
            row, config, checked = self._checked_row(job_id, api)
            if row['state'] == 'activated':
                return self.job(job_id)
            check(row['receipt'] is not None and row['state'] != 'failed', 'INDEXING_NOT_READY', 409)
            if row['activation_body']:
                result = self._observe_activation(job_id, api, row, config, checked)
                if result['state'] == 'activated' or not retry:
                    return result
            elif row['activation_attempts'] and not retry:
                return self.job(job_id)
            check(getattr(api, 'writes_enabled', False) is True, 'KNOWLEDGE_WRITE_NOT_AUTHORIZED', 409)
            check(row['activation_attempts'] < MAX_ATTEMPTS, 'INDEXING_REVIEW_REQUIRED', 409)
            try:
                check(self._version(api, row, config, checked) == 'complete', 'INDEXING_NOT_READY', 409)
                index = self._index(api, row, config)
                check(index['active_version_id'] == row['base_version_id'], 'INDEXING_ACTIVE_VERSION_CONFLICT', 409)
            except (UploadError, ContractError) as exc:
                self.error(job_id, exc.code if isinstance(exc, UploadError) else str(exc))
                return self.job(job_id)
            body = {'index_version_id': json.loads(row['receipt'])['receipt']['resource_id'],
                    'expected_active_version_id': row['base_version_id'], 'expected_revision': index['revision']}
            history = json.loads(row['activation_history']) + [body]
            with self.uploads.connect(write=True) as db:
                db.execute("UPDATE indexing_tasks SET state='activation_confirmation_required',activation_body=?,activation_history=?,activation_attempts=activation_attempts+1,error_code='INDEX_ACTIVATION_OUTCOME_UNKNOWN',updated_at=? WHERE job_id=?",
                           (canonical(body), canonical(history), now(), job_id))
            try:
                code, raw = api.request('POST', '/api/indexes/' + row['index_id'] + '/activate', body)
                value = strict_json(raw)
                checked_index(value, row['index_id'], config['embedding'], 'document-qa-' + self.uploads.library()['library_id'])
                check(code == 200 and value['active_version_id'] == body['index_version_id']
                      and value['revision'] == body['expected_revision'] + 1, 'INDEX_ACTIVATION_RECEIPT_INVALID', 502)
            except (UploadError, ContractError) as exc:
                error = exc.code if isinstance(exc, UploadError) else 'INDEX_ACTIVATION_RECEIPT_INVALID'
                self.error(job_id, error, 'activation_confirmation_required')
            return self._observe_activation(job_id, api, self.row(job_id), config, checked)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'status', 'plan'))
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/agent-project-document-qa/uploads')
    parser.add_argument('--job-id')
    args = parser.parse_args(argv)
    try:
        uploads = UploadStore(args.state_dir)
        store = IndexingStore.initialize(uploads) if args.action == 'init' else IndexingStore(uploads)
        result = store.plan(args.job_id) if args.action == 'plan' else store.status()
        result.update(result='passed', production_api_requests=0, inference_requested=False, GPU_requested=False)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (UploadError, readiness.ReadinessError) as exc:
        code = exc.code
    except (OSError, sqlite3.Error, UnicodeError, ContractError, ValueError, TypeError, KeyError, AttributeError):
        code = 'LOCAL_INDEXING_STATE_UNCERTAIN'
    print(json.dumps({'result': 'attention_required', 'code': code, 'question_ready': False,
                      'production_api_requests': 0, 'inference_requested': False, 'GPU_requested': False}))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
