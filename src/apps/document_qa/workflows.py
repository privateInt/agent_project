"""Q03b: durable n8n entry delivery and receipts, not document processing."""
import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from uuid import uuid4, uuid5, NAMESPACE_URL
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener
from urllib.parse import urlsplit

from . import readiness
from .contracts import fingerprint, strict_json, uid, ContractError
from .intake import check, UploadError
from .uploads import UploadStore, file_lock, private_path, now
from .workflow_template import build, matches
from .engine_identity import ENGINE_VERSION as VERSION, verify_engine_version

MAX_DELIVERIES = 16
SCHEMA = '''
CREATE TABLE workflow_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL CHECK(version=1));
INSERT INTO workflow_metadata VALUES(1,1);
CREATE TABLE workflow_setup (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL);
CREATE TABLE handoffs (
 job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), handoff_id TEXT NOT NULL UNIQUE,
 body TEXT NOT NULL, body_sha256 TEXT NOT NULL, setup_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','confirmation_required','accepted')),
 attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT, receipt TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((state='accepted')=(receipt IS NOT NULL))
);
CREATE TABLE handoff_executions (
 handoff_id TEXT NOT NULL REFERENCES handoffs(handoff_id), execution_id TEXT NOT NULL,
 PRIMARY KEY(handoff_id,execution_id)
);
'''


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def engine_id(value):
    check(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value), 'ENGINE_ID_INVALID')
    return value


def private_secret(path):
    private_path(path)
    value = readiness.read_file(path, limit=256, credential=True).decode('ascii').strip()
    check(re.fullmatch(r'[A-Za-z0-9_-]{43}', value), 'WORKFLOW_KEY_INVALID', 503)
    return value


def signed_headers(secret, path, body):
    stamp = int(time.time())
    def encoded(value):
        return base64.urlsafe_b64encode(canonical(value).encode()).rstrip(b'=')
    message = encoded({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + encoded({
        'aud': path, 'sub': body['handoff_id'], 'request_sha256': fingerprint(body),
        'iat': stamp, 'exp': stamp + 120})
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).rstrip(b'=')
    return {'Authorization': 'Bearer ' + (message + b'.' + signature).decode()}


class EngineHTTP:
    """Bounded official n8n API/webhook only. No proxies, redirects or retries."""
    def __init__(self, origin, key, timeout=8, clock=time.monotonic):
        self.origin = readiness.origin(origin)
        self.key, self.timeout, self.calls = key, timeout, 0
        self.clock, self.deadline = clock, clock() + 60
        self.observations = []

    def request(self, method, route, body=None, headers=None):
        allowed = (method == 'GET' and (route == '/rest/settings' or re.fullmatch(
            r'/api/v1/(workflows|credentials)(/[A-Za-z0-9_-]{1,128})?(\?limit=100(&cursor=[A-Za-z0-9_%+=.-]{1,512})?)?', route)))
        allowed = allowed or (method == 'POST' and (route in ('/api/v1/credentials', '/api/v1/workflows')
            or re.fullmatch(r'/api/v1/workflows/[A-Za-z0-9_-]{1,128}/publish', route)
            or re.fullmatch(r'/webhook/document-qa-[a-f0-9-]{36}', route)))
        check(allowed and self.calls < 80, 'ENGINE_REQUEST_NOT_ALLOWED', 503)
        check(self.clock() < self.deadline, 'ENGINE_BUDGET_EXCEEDED', 503)
        self.calls += 1
        raw = None if body is None else canonical(body).encode()
        values = {'Accept': 'application/json'}
        if raw is not None:
            values['Content-Type'] = 'application/json'
        if route.startswith('/api/v1/'):
            values['X-N8N-API-KEY'] = self.key
        values.update(headers or {})
        # Record only bounded route/status metadata, never keys, bodies or cursors.
        observation = {'method': method, 'route': route.partition('?')[0],
                       'http_status': None, 'response_format': 'unavailable'}
        self.observations.append(observation)
        request = Request(self.origin + route, method=method, data=raw, headers=values)
        try:
            with build_opener(ProxyHandler({}), readiness.NoRedirect()).open(request, timeout=min(self.timeout, self.deadline-self.clock())) as response:
                observation['http_status'] = response.status
                check(response.status in (200, 201), 'ENGINE_RESPONSE_INVALID', 502)
                check(response.headers.get_content_type() == 'application/json', 'ENGINE_RESPONSE_INVALID', 502)
                chunks, size = [], 0
                while True:
                    check(self.clock() < self.deadline, 'ENGINE_BUDGET_EXCEEDED', 503)
                    part = response.read1(min(65536, (2 << 20) + 1-size))
                    if not part:
                        break
                    chunks.append(part); size += len(part)
                    check(size <= 2 << 20, 'ENGINE_RESPONSE_TOO_LARGE', 502)
                value = strict_json(b''.join(chunks))
                observation['response_format'] = 'json_object'
                return value
        except HTTPError as exc:
            code = 'ENGINE_REDIRECT_REFUSED' if 300 <= exc.code < 400 else 'ENGINE_AUTH_REQUIRED' if exc.code in (401, 403) else 'ENGINE_HTTP_UNCONFIRMED'
            observation.update(http_status=exc.code, error_code=code)
            exc.close()
            raise UploadError(code, 502) from None
        except (OSError, URLError, HTTPException, TimeoutError):
            observation['error_code'] = 'ENGINE_OUTCOME_UNKNOWN'
            raise UploadError('ENGINE_OUTCOME_UNKNOWN', 502) from None
        except ContractError:
            observation['error_code'] = 'ENGINE_RESPONSE_INVALID'
            raise UploadError('ENGINE_RESPONSE_INVALID', 502) from None
        except UploadError as exc:
            observation['error_code'] = exc.code
            raise

    def items(self, collection):
        from urllib.parse import quote
        out, cursor, seen = [], '', set()
        for _ in range(10):
            route = collection + '?limit=100' + ('&cursor=' + quote(cursor, safe='') if cursor else '')
            result = self.request('GET', route)
            rows = result.get('data')
            check(isinstance(rows, list) and len(rows) <= 100 and all(isinstance(x, dict) for x in rows),
                  'ENGINE_LIST_INVALID', 502)
            out.extend(rows)
            cursor = result.get('nextCursor')
            if not cursor:
                return out
            check(isinstance(cursor, str) and len(cursor) <= 256 and cursor not in seen, 'ENGINE_LIST_INCOMPLETE', 502)
            seen.add(cursor)
        raise UploadError('ENGINE_LIST_INCOMPLETE', 502)


def key_client(path):
    private_path(path)
    value = strict_json(readiness.read_file(path, limit=65536, credential=True))
    endpoint, key = value.get('url'), value.get('api_key')
    endpoint = readiness.origin(endpoint)
    url = urlsplit(endpoint)
    check(url.scheme == 'https' or url.hostname in ('localhost', '127.0.0.1', '::1'), 'ENGINE_AUTH_TRANSPORT_INVALID', 503)
    check(isinstance(key, str) and 0 < len(key) <= 4096 and all(32 < ord(c) < 127 for c in key),
          'ENGINE_KEY_INVALID', 503)
    return EngineHTTP(endpoint, key)


def qa_endpoint(value):
    value = readiness.origin(value)
    url = urlsplit(value)
    try:
        address = ipaddress.IPv4Address(url.hostname)
        private = address.is_loopback or any(address in ipaddress.IPv4Network(cidr) for cidr in
                                            ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    except ipaddress.AddressValueError:
        private = url.hostname == 'localhost'
    check(url.scheme == 'https' or private, 'QA_AUTH_TRANSPORT_INVALID', 503)
    return value


class WorkflowStore:
    # Fixed class-owned table/key names allow a separately owned Q04b flow.
    # The original Q03b tables, keys and registered definition remain unchanged.
    metadata_table = 'workflow_metadata'
    setup_table = 'workflow_setup'
    token_name = 'workflow-api-token'
    hook_name = 'workflow-hook-secret'
    init_lock = 'workflow-init.lock'
    schema = SCHEMA

    def __init__(self, uploads):
        self.uploads = uploads
        self.root = uploads.root
        with uploads.connect() as db:
            check(db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (self.metadata_table,)).fetchone()[0] == 1,
                  'WORKFLOW_NOT_INITIALIZED', 503)
            check(db.execute('SELECT version FROM ' + self.metadata_table + ' WHERE singleton=1').fetchone()[0] == 1,
                  'WORKFLOW_SCHEMA_INVALID', 503)
        self.token_path = self.root / self.token_name
        self.hook_path = self.root / self.hook_name
        private_secret(self.token_path)
        private_secret(self.hook_path)

    @classmethod
    def initialize(cls, uploads):
        with file_lock(uploads.root / cls.init_lock):
            with uploads.connect(write=True) as db:
                check(db.execute('PRAGMA user_version').fetchone()[0] == 1, 'STORE_SCHEMA_INVALID', 503)
                present = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (cls.metadata_table,)).fetchone()[0]
                if not present:
                    # executescript would implicitly commit: each statement stays in this transaction.
                    for statement in cls.schema.split(';'):
                        if statement.strip():
                            db.execute(statement)
            for name in (cls.token_name, cls.hook_name):
                path = uploads.root / name
                if not path.exists() and not path.is_symlink():
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write((secrets.token_urlsafe(32) + '\n').encode()); stream.flush(); os.fsync(stream.fileno())
                private_secret(path)
            fd = os.open(uploads.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return cls(uploads)

    def setup(self, required=True):
        with self.uploads.connect() as db:
            row = db.execute('SELECT value FROM ' + self.setup_table + ' WHERE singleton=1').fetchone()
        if not row:
            check(not required, 'WORKFLOW_NOT_CONFIGURED', 503)
            return None
        return json.loads(row[0])

    def save_setup(self, value):
        with self.uploads.connect(write=True) as db:
            db.execute('INSERT INTO ' + self.setup_table + ' VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET value=excluded.value',
                       (canonical(value),))

    def desired(self, setup):
        flow, digest = build(setup['qa_origin'], setup['path'], setup['resources']['qa_credential']['id'],
                             setup['resources']['hook_credential']['id'])
        return flow, digest

    def prepare(self, qa_origin, key_file, client=None):
        """Explicit new-flow registration only; unknown creates reconcile by unique owned name."""
        qa_origin = qa_endpoint(qa_origin)
        key_file = Path(key_file).expanduser().absolute()
        api = client or key_client(key_file)
        with file_lock(self.root / 'workflow-setup.lock'):
            setup = self.setup(False)
            hashes = [hashlib.sha256(private_secret(p).encode()).hexdigest() for p in (self.token_path, self.hook_path)]
            if setup:
                check(setup['qa_origin'] == qa_origin and setup['engine_origin'] == api.origin
                      and setup['engine_key_file'] == str(key_file) and setup['secret_sha256'] == hashes,
                      'WORKFLOW_SETUP_CHANGED', 409)
            else:
                setup = {'schema': 1, 'setup_id': str(uuid4()), 'qa_origin': qa_origin, 'engine_origin': api.origin,
                         'engine_key_file': str(key_file), 'secret_sha256': hashes, 'resources': {}, 'prepared': False}
                setup['path'] = 'document-qa-' + setup['setup_id']
                self.save_setup(setup)
            verify_engine_version(api)
            def resource(slot, collection, body):
                intent = setup['resources'].get(slot)
                if intent:
                    check(intent['request_sha256'] == fingerprint(body), 'WORKFLOW_SETUP_CHANGED', 409)
                    if not intent.get('id'):
                        found = [x for x in api.items(collection) if x.get('name') == body['name']]
                        check(len(found) == 1, 'ENGINE_CREATE_UNCONFIRMED', 409)
                        intent['id'] = engine_id(found[0].get('id')); self.save_setup(setup)
                else:
                    intent = {'name': body['name'], 'request_sha256': fingerprint(body)}
                    setup['resources'][slot] = intent; self.save_setup(setup)
                    result = api.request('POST', collection, body)
                    intent['id'] = engine_id(result.get('id')); self.save_setup(setup)
                return api.request('GET', collection + '/' + intent['id'])
            for slot, kind, data in (
                ('qa_credential', 'httpHeaderAuth', {'name': 'Authorization', 'value': 'Bearer ' + private_secret(self.token_path)}),
                ('hook_credential', 'jwtAuth', {'keyType': 'passphrase', 'algorithm': 'HS256', 'secret': private_secret(self.hook_path)})):
                name = 'Document QA ' + slot + ' · ' + setup['setup_id']
                result = resource(slot, '/api/v1/credentials', {'name': name, 'type': kind, 'data': data})
                check(result.get('name') == name and result.get('type') == kind, 'ENGINE_CREDENTIAL_CHANGED', 409)
            desired, digest = self.desired(setup)
            current = resource('workflow', '/api/v1/workflows', desired)
            check(matches(current, desired), 'ENGINE_WORKFLOW_CHANGED', 409)
            version = engine_id(current.get('versionId'))
            check(not setup.get('published_version_id') or setup['published_version_id'] == version,
                  'ENGINE_WORKFLOW_CHANGED', 409)
            if not current.get('active'):
                api.request('POST', '/api/v1/workflows/' + setup['resources']['workflow']['id'] + '/publish', {'versionId': version})
            setup.update(published_version_id=version, definition_sha256=digest, prepared=True)
            # Readback must pass before storing prepared=true.
            self.verify_setup(setup, api)
            self.save_setup(setup)
            return self.status()

    def verify_setup(self, setup, api):
        check(setup.get('prepared'), 'WORKFLOW_NOT_CONFIGURED', 503)
        check(api.origin == setup['engine_origin'], 'WORKFLOW_TARGET_CHANGED', 409)
        verify_engine_version(api)
        current = api.request('GET', '/api/v1/workflows/' + setup['resources']['workflow']['id'])
        desired, digest = self.desired(setup)
        check(matches(current, desired) and current.get('active') is True
              and current.get('versionId') == current.get('activeVersionId') == setup['published_version_id']
              and digest == setup['definition_sha256'], 'ENGINE_WORKFLOW_CHANGED', 409)
        if current.get('activeVersion'):
            historical = {**current, **current['activeVersion'], 'name': current['name']}
            check(matches(historical, desired), 'ENGINE_WORKFLOW_CHANGED', 409)

    def status(self):
        setup = self.setup(False)
        with self.uploads.connect() as db:
            states = {r[0]: r[1] for r in db.execute('SELECT state,count(*) FROM handoffs GROUP BY state')}
        return {'schema': 1, 'workflow_schema': 1, 'upload_schema': 1,
                'workflow_configured': bool(setup and setup.get('prepared')),
                'configuration_live_verified': False, 'handoff_counts': states,
                'question_ready': False, 'document_processing_implemented': False,
                'next_step': 'Q04_refinement_adapter' if setup and setup.get('prepared') else 'explicit_workflow_connection_setup'}

    def project_job(self, job):
        handoff = self.handoff(job['job_id'])
        return {**job, 'workflow': handoff, 'workflow_started': handoff['workflow_started']}

    def project_library(self):
        with self.uploads.connect() as db:
            started = db.execute("SELECT count(*) FROM handoffs WHERE state='accepted'").fetchone()[0] > 0
        return {**self.uploads.library(), 'workflow_started': started}

    def handoff(self, job_id):
        self.uploads.job(job_id)
        with self.uploads.connect() as db:
            row = db.execute('SELECT * FROM handoffs WHERE job_id=?', (job_id,)).fetchone()
            executions = [] if not row else [r[0] for r in db.execute('SELECT execution_id FROM handoff_executions WHERE handoff_id=? ORDER BY execution_id', (row['handoff_id'],))]
        if not row:
            return {'job_id': job_id, 'state': 'not_requested', 'workflow_started': False, 'question_ready': False}
        return {'job_id': job_id, 'handoff_id': row['handoff_id'], 'state': row['state'], 'attempts': row['attempts'],
                'error_code': row['error_code'], 'receipt': json.loads(row['receipt']) if row['receipt'] else None,
                'execution_ids': executions, 'workflow_started': row['state'] == 'accepted', 'question_ready': False}

    def ensure_intent(self, job_id):
        job, setup = self.uploads.job(job_id), self.setup()
        check(job['state'] == 'preparation_pending' and uid(job['source_document_id']), 'SOURCE_RECEIPT_REQUIRED', 409)
        check(setup.get('prepared'), 'WORKFLOW_NOT_CONFIGURED', 503)
        body = {'schema': 1, 'handoff_id': str(uuid5(NAMESPACE_URL, 'document-qa-q03b/' + job_id)),
                'job_id': job_id, 'library_id': self.uploads.library()['library_id'],
                'sequence': job['sequence'], 'source_document_id': job['source_document_id']}
        with self.uploads.connect(write=True) as db:
            row = db.execute('SELECT * FROM handoffs WHERE job_id=?', (job_id,)).fetchone()
            if row:
                check(row['body_sha256'] == fingerprint(body) and row['setup_sha256'] == fingerprint(setup),
                      'HANDOFF_INTENT_CHANGED', 409)
            else:
                stamp = now()
                db.execute('INSERT INTO handoffs(job_id,handoff_id,body,body_sha256,setup_sha256,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                           (job_id, body['handoff_id'], canonical(body), fingerprint(body), fingerprint(setup), 'pending', stamp, stamp))
        return body, setup

    def dispatch(self, job_id, client=None, retry=False):
        self.uploads.job(job_id)
        with file_lock(self.root / 'locks' / (job_id + '.workflow.lock')):
            existing = self.handoff(job_id)
            if existing['state'] == 'accepted' or existing.get('attempts', 0) > 0 and not retry:
                return existing
            body, setup = self.ensure_intent(job_id)
            api = client or key_client(Path(setup['engine_key_file']))
            self.verify_setup(setup, api)
            with self.uploads.connect(write=True) as db:
                row = db.execute('SELECT attempts FROM handoffs WHERE job_id=?', (job_id,)).fetchone()
                check(row[0] < MAX_DELIVERIES, 'HANDOFF_REVIEW_REQUIRED', 409)
                db.execute("UPDATE handoffs SET state='confirmation_required',attempts=attempts+1,error_code='HANDOFF_OUTCOME_UNKNOWN',updated_at=? WHERE job_id=?", (now(), job_id))
            try:
                response = api.request('POST', '/webhook/' + setup['path'], body,
                                       signed_headers(private_secret(self.hook_path), setup['path'], body))
                current = self.handoff(job_id)
                check(current['state'] == 'accepted' and response == current['receipt'], 'HANDOFF_RECEIPT_UNCONFIRMED', 502)
            except UploadError as exc:
                with self.uploads.connect(write=True) as db:
                    # The callback may have committed before the webhook response was lost.
                    db.execute("UPDATE handoffs SET error_code=?,updated_at=? WHERE job_id=? AND state!='accepted'", (exc.code, now(), job_id))
            return self.handoff(job_id)

    def accept_receipt(self, value):
        expected = {'schema','handoff_id','job_id','library_id','sequence','source_document_id',
                    'workflow_id','execution_id','definition_sha256'}
        check(isinstance(value, dict) and set(value) == expected, 'HANDOFF_INPUT_INVALID')
        check(all(uid(value[k]) for k in ('handoff_id','job_id','library_id','source_document_id')),
              'HANDOFF_INPUT_INVALID')
        execution_id = engine_id(value['execution_id'])
        setup = self.setup()
        check(value['workflow_id'] == setup['resources']['workflow']['id']
              and value['definition_sha256'] == setup['definition_sha256'], 'HANDOFF_TARGET_CHANGED', 409)
        with self.uploads.connect(write=True) as db:
            row = db.execute('SELECT * FROM handoffs WHERE handoff_id=?', (value['handoff_id'],)).fetchone()
            check(row is not None, 'HANDOFF_NOT_FOUND', 404)
            body = {k: value[k] for k in expected - {'workflow_id','execution_id','definition_sha256'}}
            check(canonical(body) == row['body'] and fingerprint(setup) == row['setup_sha256']
                  and row['attempts'] > 0, 'HANDOFF_INTENT_CHANGED', 409)
            seen = db.execute('SELECT 1 FROM handoff_executions WHERE handoff_id=? AND execution_id=?',
                              (value['handoff_id'], execution_id)).fetchone()
            if not seen:
                check(db.execute('SELECT count(*) FROM handoff_executions WHERE handoff_id=?', (value['handoff_id'],)).fetchone()[0] < MAX_DELIVERIES,
                      'HANDOFF_REVIEW_REQUIRED', 409)
                db.execute('INSERT INTO handoff_executions VALUES(?,?)', (value['handoff_id'], execution_id))
            if row['receipt']:
                return json.loads(row['receipt'])
            receipt = {'handoff_id': value['handoff_id'], 'job_id': value['job_id'],
                       'source_document_id': value['source_document_id'], 'workflow_id': value['workflow_id'],
                       'published_version_id': setup['published_version_id'], 'definition_sha256': value['definition_sha256'],
                       'first_execution_id': execution_id, 'phase': 'awaiting_refinement',
                       'workflow_started': True, 'question_ready': False, 'may_execute_next_stage': False}
            db.execute("UPDATE handoffs SET state='accepted',receipt=?,error_code=NULL,updated_at=? WHERE handoff_id=?", (canonical(receipt), now(), value['handoff_id']))
            return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init','status','prepare','dispatch','retry','export'))
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/agent-project-document-qa/uploads')
    parser.add_argument('--qa-origin')
    parser.add_argument('--engine-key-file', type=Path, default=Path(__file__).resolve().parents[3] / 'src/infra/n8n/.secrets/agent-integration-api-key.json')
    parser.add_argument('--job-id')
    args = parser.parse_args(argv)
    try:
        uploads = UploadStore(args.state_dir)
        workflow = WorkflowStore.initialize(uploads) if args.action == 'init' else WorkflowStore(uploads)
        if args.action in ('init','status'):
            result = {'result': 'passed', **workflow.status(), 'production_api_requests': 0, 'inference_requested': False}
        elif args.action == 'prepare':
            check(args.qa_origin, 'QA_ORIGIN_REQUIRED')
            result = {'result': 'prepared', **workflow.prepare(args.qa_origin, args.engine_key_file), 'inference_requested': False}
        elif args.action in ('dispatch','retry'):
            result = workflow.dispatch(args.job_id, retry=args.action == 'retry')
        else:
            setup = workflow.setup()
            result = workflow.desired(setup)[0]
            result['active'] = False
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 2 if result.get('state') == 'confirmation_required' else 0
    except (UploadError, readiness.ReadinessError) as exc:
        print(json.dumps({'result':'attention_required','code':exc.code,'question_ready':False})); return 2
    except (OSError, sqlite3.Error, UnicodeError, ContractError):
        print(json.dumps({'result':'attention_required','code':'LOCAL_WORKFLOW_STATE_UNCERTAIN','question_ready':False})); return 2
    except (KeyError, ValueError, TypeError, AttributeError):
        print(json.dumps({'result':'attention_required','code':'WORKFLOW_RESPONSE_INVALID','question_ready':False})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
