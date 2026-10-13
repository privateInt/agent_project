"""Q04a: explicit CPU Data refinement adapter. n8n automatic stages follow in Q04b."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import re
import sqlite3
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from . import readiness
from .contracts import ContractError, MAX_BYTES, Receipt, acceptance, observe_refinement, request_intent, uid
from .intake import check, UploadError
from .uploads import UploadStore, file_lock, now
from .workflows import canonical
from .canonical_results import checked_result, FORMATS, RULES

MAX_ATTEMPTS = 16
SCHEMA = '''
CREATE TABLE refinement_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL CHECK(version=1));
INSERT INTO refinement_metadata VALUES(1,1);
CREATE TABLE refinement_tasks (
 job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), data_origin TEXT NOT NULL,
 request_body TEXT NOT NULL, request_sha256 TEXT NOT NULL, request_key TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('pending','confirmation_required','waiting','canonical_verified','attention_required','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, receipt TEXT, canonical_receipt TEXT,
 terminal INTEGER NOT NULL DEFAULT 0 CHECK(terminal IN (0,1)), error_code TEXT,
 last_data_status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE refinement_lane (singleton INTEGER PRIMARY KEY CHECK(singleton=1), job_id TEXT REFERENCES refinement_tasks(job_id));
INSERT INTO refinement_lane VALUES(1,NULL);
'''


@contextmanager
def lane_lock(root):
    try:
        with file_lock(root / 'refinement-lane.lock'):
            yield
    except UploadError as exc:
        if exc.code == 'UPLOAD_BUSY':
            raise UploadError('REFINEMENT_BUSY', 409) from None
        raise


class DataRefiner:
    """One explicit action's bounded Data-only HTTP; no model or dynamic URLs."""
    def __init__(self, row, timeout=10, budget_seconds=60, clock=time.monotonic):
        check(row.get('enabled', True), 'DATA_CONNECTION_DISABLED', 503)
        self.origin = readiness.origin(row['url'])
        self.token_file = row.get('token_file')
        self.timeout, self.clock, self.calls = timeout, clock, 0
        self.deadline = clock() + budget_seconds

    def request(self, method, path, body=None, key=None, content_limit=None):
        allowed = method == 'POST' and path == '/api/refinement-runs' and isinstance(body, dict) and uid(key)
        if method == 'POST':
            check(isinstance(body, dict) and set(body) == {'name','document_id','rules_version'}
                  and isinstance(body.get('name'), str) and re.fullmatch(r'document-qa-[a-f0-9-]{36}', body['name'])
                  and uid(body.get('document_id')) and body.get('rules_version') == RULES,
                  'CPU_REFINEMENT_REQUEST_REQUIRED', 503)
        allowed = allowed or (method == 'GET' and body is None and key is None and re.fullmatch(
            r'/api/(source-documents|refinement-runs|canonical-documents)/[a-f0-9-]{36}(?:/content)?', path))
        # Content is only the immutable canonical JSON, never source/artifact URLs.
        check(allowed and (not path.endswith('/content') or path.startswith('/api/canonical-documents/')),
              'DATA_REFINEMENT_PATH_NOT_ALLOWED', 503)
        check(self.calls < 8 and self.clock() < self.deadline, 'DATA_REFINEMENT_BUDGET_EXCEEDED', 504)
        limit = 64 << 10
        if path.endswith('/content'):
            check(type(content_limit) is int and 1 <= content_limit <= MAX_BYTES, 'CANONICAL_SIZE_INVALID', 502)
            limit = content_limit
        headers = {'Accept': 'application/json'}
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'Idempotency-Key': key})
        if self.token_file:
            try:
                token = readiness.read_file(self.token_file, 8192, credential=True).decode('ascii').strip()
                check(0 < len(token) <= 4096 and all(32 < ord(c) < 127 for c in token), 'DATA_AUTH_UNAVAILABLE', 503)
            except (readiness.ReadinessError, UnicodeError):
                raise UploadError('DATA_AUTH_UNAVAILABLE', 503) from None
            headers['Authorization'] = 'Bearer ' + token
        raw = canonical(body).encode() if body is not None else None
        request = Request(self.origin + path, data=raw, headers=headers, method=method)
        self.calls += 1
        try:
            with build_opener(ProxyHandler({}), readiness.NoRedirect()).open(request, timeout=min(self.timeout, self.deadline-self.clock())) as response:
                check(response.status == (202 if method == 'POST' else 200)
                      and response.headers.get_content_type() == 'application/json', 'DATA_REFINEMENT_RESPONSE_INVALID', 502)
                chunks, size = [], 0
                while True:
                    check(self.clock() < self.deadline, 'DATA_REFINEMENT_BUDGET_EXCEEDED', 504)
                    part = response.read1(min(65536, limit + 1 - size))
                    if not part:
                        break
                    chunks.append(part); size += len(part)
                    check(size <= limit, 'DATA_REFINEMENT_RESPONSE_TOO_LARGE', 502)
                return response.status, b''.join(chunks)
        except HTTPError as exc:
            code = ('DATA_REDIRECT_REFUSED' if 300 <= exc.code < 400 else 'DATA_AUTH_REQUIRED' if exc.code in (401,403)
                    else 'DATA_RESOURCE_UNAVAILABLE' if exc.code in (404,410) else 'DATA_REFINEMENT_HTTP_UNCONFIRMED')
            exc.close(); raise UploadError(code, 502) from None
        except (OSError, URLError, HTTPException, TimeoutError):
            raise UploadError('DATA_REFINEMENT_OUTCOME_UNKNOWN', 502) from None

    def get(self, path):
        from .contracts import strict_json
        try:
            return strict_json(self.request('GET', path)[1])
        except ContractError:
            raise UploadError('DATA_REFINEMENT_JSON_INVALID', 502) from None


class RefinementStore:
    def __init__(self, uploads):
        self.uploads, self.root = uploads, uploads.root
        with uploads.connect() as db:
            check(db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='refinement_metadata'").fetchone()[0] == 1,
                  'REFINEMENT_NOT_INITIALIZED', 503)
            row = db.execute('SELECT version FROM refinement_metadata WHERE singleton=1').fetchone()
            check(row is not None and row[0] == 1, 'REFINEMENT_SCHEMA_INVALID', 503)

    @classmethod
    def initialize(cls, uploads):
        with file_lock(uploads.root / 'refinement-init.lock'):
            with uploads.connect(write=True) as db:
                check(db.execute('PRAGMA user_version').fetchone()[0] == 1, 'STORE_SCHEMA_INVALID', 503)
                if not db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='refinement_metadata'").fetchone()[0]:
                    for statement in SCHEMA.split(';'):
                        if statement.strip():
                            db.execute(statement)
        return cls(uploads)

    def row(self, job_id):
        self.uploads.job(job_id)
        with self.uploads.connect() as db:
            return db.execute('SELECT * FROM refinement_tasks WHERE job_id=?', (job_id,)).fetchone()

    def job(self, job_id):
        row = self.row(job_id)
        if not row:
            return {'job_id': job_id, 'state': 'not_requested', 'refinement_ready': False, 'question_ready': False}
        return {'job_id': job_id, 'state': row['state'], 'attempts': row['attempts'],
                'receipt': json.loads(row['receipt']) if row['receipt'] else None,
                'canonical': json.loads(row['canonical_receipt']) if row['canonical_receipt'] else None,
                'terminal': bool(row['terminal']), 'error_code': row['error_code'],
                'last_data_status': row['last_data_status'], 'checked_at': row['updated_at'],
                'refinement_ready': row['state'] == 'canonical_verified', 'question_ready': False}

    def project_job(self, job):
        return {**job, 'refinement': self.job(job['job_id'])}

    def status(self):
        with self.uploads.connect() as db:
            counts = {r[0]: r[1] for r in db.execute('SELECT state,count(*) FROM refinement_tasks GROUP BY state')}
            owner = db.execute('SELECT job_id FROM refinement_lane WHERE singleton=1').fetchone()
        check(owner is not None, 'REFINEMENT_SCHEMA_INVALID', 503)
        return {'schema': 1, 'refinement_schema': 1, 'upload_schema': 1, 'active_job_id': owner[0],
                'refinement_counts': counts, 'question_ready': False, 'configuration_live_verified': False,
                'n8n_refinement_connected': False, 'ocr_supported': False, 'next_step': 'Q04b_n8n_refinement_stage'}

    def source_job(self, job_id):
        job = self.uploads.job(job_id)
        check(job['state'] == 'preparation_pending' and uid(job['source_document_id']), 'SOURCE_RECEIPT_REQUIRED', 409)
        return job

    def ensure_intent(self, job_id, api):
        job = self.source_job(job_id)
        with self.uploads.connect() as db:
            target = db.execute('SELECT data_origin FROM jobs WHERE job_id=?', (job_id,)).fetchone()[0]
            owner = db.execute('SELECT job_id FROM refinement_lane WHERE singleton=1').fetchone()[0]
            existing = db.execute('SELECT * FROM refinement_tasks WHERE job_id=?', (job_id,)).fetchone()
        check(api.origin == target, 'REFINEMENT_TARGET_CHANGED', 409)
        check(owner in (None, job_id), 'LIBRARY_REFINEMENT_BUSY', 409)
        body = {'name': 'document-qa-' + job_id, 'document_id': job['source_document_id'], 'rules_version': RULES}
        intent = request_intent(job_id, 'data.refine', job['sequence'], body)
        if existing:
            check(existing['request_body'] == canonical(body) and existing['request_key'] == intent.key
                  and existing['request_sha256'] == intent.request_sha256 and existing['data_origin'] == api.origin,
                  'REFINEMENT_INTENT_CHANGED', 409)
            return existing
        # Read source before first intent; replay uses the saved request even if Collection is down.
        source = api.get('/api/source-documents/' + job['source_document_id'])
        expected = {'document_id': job['source_document_id'], 'name': job['name'], 'original_filename': job['filename'],
                    'sha256': job['sha256'], 'size_bytes': job['size_bytes'], 'source_type': 'upload',
                    'state': 'ready', 'format': FORMATS[job['filename'].rsplit('.', 1)[-1].lower()]}
        check(all(source.get(k) == v and type(source.get(k)) is type(v) for k, v in expected.items())
              and not source.get('deleted_at') and not source.get('purge_state') and not source.get('purged_at'),
              'REFINEMENT_SOURCE_MISMATCH', 502)
        with self.uploads.connect(write=True) as db:
            stamp = now()
            db.execute('INSERT INTO refinement_tasks(job_id,data_origin,request_body,request_sha256,request_key,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       (job_id, api.origin, canonical(body), intent.request_sha256, intent.key, 'pending', stamp, stamp))
            db.execute('UPDATE refinement_lane SET job_id=? WHERE singleton=1', (job_id,))
        return self.row(job_id)

    def dispatch(self, job_id, api, retry=False):
        self.source_job(job_id)
        with lane_lock(self.root):
            row = self.row(job_id)
            if row and (row['receipt'] or row['terminal'] or row['attempts'] and not retry):
                return self.job(job_id)
            row = self.ensure_intent(job_id, api)
            check(row['attempts'] < MAX_ATTEMPTS, 'REFINEMENT_REVIEW_REQUIRED', 409)
            with self.uploads.connect(write=True) as db:
                db.execute("UPDATE refinement_tasks SET state='confirmation_required',attempts=attempts+1,error_code='REFINEMENT_OUTCOME_UNKNOWN',updated_at=? WHERE job_id=?", (now(), job_id))
            try:
                code, raw = api.request('POST', '/api/refinement-runs', json.loads(row['request_body']), row['request_key'])
                receipt = acceptance('data.refine', code, raw, {})
            except (UploadError, ContractError) as exc:
                self.error(job_id, exc.code if isinstance(exc, UploadError) else 'REFINEMENT_RECEIPT_INVALID', 'confirmation_required')
                return self.job(job_id)
            with self.uploads.connect(write=True) as db:
                db.execute("UPDATE refinement_tasks SET state='waiting',receipt=?,error_code=NULL,updated_at=? WHERE job_id=?", (canonical(asdict(receipt)), now(), job_id))
            return self.job(job_id)

    def error(self, job_id, code, state='attention_required'):
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE refinement_tasks SET state=?,error_code=?,updated_at=? WHERE job_id=? AND terminal=0', (state,code,now(),job_id))

    def observe(self, job_id, api):
        job = self.source_job(job_id)
        with lane_lock(self.root):
            row = self.row(job_id)
            check(row is not None, 'REFINEMENT_NOT_REQUESTED', 409)
            if row['terminal'] or not row['receipt']:
                return self.job(job_id)
            check(api.origin == row['data_origin'], 'REFINEMENT_TARGET_CHANGED', 409)
            receipt = Receipt(**json.loads(row['receipt']))
            try:
                run = api.get('/api/refinement-runs/' + receipt.resource_id)
                expected = {'name': json.loads(row['request_body'])['name'], 'source_sha256': job['sha256'],
                            'source_format': FORMATS[job['filename'].rsplit('.', 1)[-1].lower()], 'rules_version': RULES}
                check(all(run.get(k) == v for k, v in expected.items()) and not run.get('ocr'),
                      'REFINEMENT_PROVENANCE_MISMATCH', 502)
                observed = observe_refinement(receipt, run, job['source_document_id'])
                check(not run.get('purged_at'), 'REFINEMENT_UNAVAILABLE', 502)
                result, error, summary, terminal = 'waiting', None, None, 0
                if observed == 'failed':
                    result, error, terminal = 'failed', 'DATA_REFINEMENT_FAILED', 1
                elif observed == 'complete':
                    metadata = api.get('/api/canonical-documents/' + run['output_document_id'])
                    expected_metadata = {'document_id':run['output_document_id'], 'refinement_run_id':receipt.resource_id,
                                         'source_document_id':job['source_document_id'], 'schema_version':'canonical-document-v1'}
                    check(all(metadata.get(k) == v for k,v in expected_metadata.items()), 'CANONICAL_IDENTITY_MISMATCH', 502)
                    check(type(metadata.get('size_bytes')) is int and 1 <= metadata['size_bytes'] <= MAX_BYTES,
                          'CANONICAL_SIZE_INVALID', 502)
                    _, raw = api.request('GET', '/api/canonical-documents/' + run['output_document_id'] + '/content',
                                         content_limit=metadata['size_bytes'])
                    summary, error = checked_result(metadata, raw, receipt, job, run)
                    result, terminal = ('attention_required' if error else 'canonical_verified'), 1
                elif run.get('submission_error_code') or run.get('submission_status') in ('blocked','failed'):
                    result, error = 'attention_required', 'DATA_SUBMISSION_ATTENTION_REQUIRED'
            except (UploadError, ContractError) as exc:
                self.error(job_id, exc.code if isinstance(exc, UploadError) else str(exc))
                return self.job(job_id)
            with self.uploads.connect(write=True) as db:
                db.execute('UPDATE refinement_tasks SET state=?,canonical_receipt=?,terminal=?,error_code=?,last_data_status=?,updated_at=? WHERE job_id=?',
                           (result,canonical(summary) if summary else None,terminal,error,run['status'],now(),job_id))
                if terminal:
                    db.execute('UPDATE refinement_lane SET job_id=NULL WHERE singleton=1 AND job_id=?', (job_id,))
            return self.job(job_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init','status','dispatch','retry','observe'))
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/agent-project-document-qa/uploads')
    parser.add_argument('--settings', type=Path, default=Path.home()/'.local/state/agent-project-document-qa/settings.json')
    parser.add_argument('--connections-from', type=Path, default=Path.home()/'.local/state/agent-project-ops/server/host-config.json')
    parser.add_argument('--job-id')
    args = parser.parse_args(argv)
    try:
        uploads = UploadStore(args.state_dir)
        store = RefinementStore.initialize(uploads) if args.action == 'init' else RefinementStore(uploads)
        if args.action in ('init','status'):
            result = {'result':'passed',**store.status(),'production_api_requests':0,'inference_requested':False}
        else:
            settings, _, _ = readiness.load_settings(args.settings,args.connections_from)
            api = DataRefiner(settings['platforms']['data'])
            result = store.observe(args.job_id,api) if args.action == 'observe' else store.dispatch(args.job_id,api,retry=args.action=='retry')
            result.update(production_api_requests=api.calls,ocr_requested=False,inference_requested=False)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return 2 if result.get('state') in ('confirmation_required','attention_required','failed') else 0
    except (UploadError,readiness.ReadinessError) as exc:
        print(json.dumps({'result':'attention_required','code':exc.code,'question_ready':False}));return 2
    except (OSError,sqlite3.Error,UnicodeError,ContractError):
        print(json.dumps({'result':'attention_required','code':'LOCAL_REFINEMENT_STATE_UNCERTAIN','question_ready':False}));return 2
    except (KeyError,ValueError,TypeError,AttributeError):
        print(json.dumps({'result':'attention_required','code':'REFINEMENT_RESPONSE_INVALID','question_ready':False}));return 2


if __name__ == '__main__':
    raise SystemExit(main())
