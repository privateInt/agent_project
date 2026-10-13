"""Q03a durable upload receipts in an integration-owned, single-host SQLite DB.

Data owns registered originals. BLOBs here are a bounded retry envelope, removed
in the same transaction as a verified receipt. No refinement/workflow dispatch.
"""
import fcntl
import hashlib
import os
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from . import readiness
from .contracts import ContractError, MAX_DOCUMENTS, acceptance, fingerprint, request_intent, uid
from .intake import UploadError, check, encode_multipart, metadata

MAX_ALIASES = 100
MAX_ATTEMPTS = 100
PUBLIC_FIELDS = ('job_id,sequence,name,filename,size_bytes,sha256,state,source_document_id,'
                 'attempts,error_code,created_at,updated_at')
SCHEMA = '''
CREATE TABLE library (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), library_id TEXT NOT NULL,
 name TEXT NOT NULL, accepted_sequence INTEGER NOT NULL DEFAULT 0,
 question_revision INTEGER NOT NULL DEFAULT 0 CHECK(question_revision=0)
);
CREATE TABLE jobs (
 job_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE,
 request_sha256 TEXT NOT NULL UNIQUE, name TEXT NOT NULL, filename TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, data_origin TEXT NOT NULL,
 upload_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL
 CHECK(state IN ('upload_accepted','upload_confirmation_required','preparation_pending')),
 source_document_id TEXT, original BLOB, attempts INTEGER NOT NULL DEFAULT 0,
 error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((source_document_id IS NULL AND original IS NOT NULL AND state!='preparation_pending')
    OR (source_document_id IS NOT NULL AND original IS NULL AND state='preparation_pending'))
);
CREATE TABLE requests (
 request_key TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES jobs(job_id)
);
PRAGMA user_version=1;
'''


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def private_path(path, directory=False):
    info = path.lstat()
    check((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
          and info.st_uid == os.getuid() and not info.st_mode & 0o077, 'STORE_PATH_UNSAFE', 503)


@contextmanager
def file_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
              and not info.st_mode & 0o077, 'STORE_PATH_UNSAFE', 503)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UploadError('UPLOAD_BUSY', 409) from None
        yield
    finally:
        os.close(fd)


class UploadStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().absolute()
        check('..' not in self.root.parts and not any(p.is_symlink() for p in (self.root, *self.root.parents)),
              'STORE_PATH_UNSAFE', 503)
        check(self.root.exists(), 'STORE_NOT_INITIALIZED', 503)
        private_path(self.root, directory=True)
        self.db_path = self.root / 'uploads.sqlite3'
        self.token_path = self.root / 'api-token'
        check(self.db_path.exists(), 'STORE_NOT_INITIALIZED', 503)
        private_path(self.db_path)
        private_path(self.token_path)
        private_path(self.root / 'locks', directory=True)
        with self.connect() as db:
            check(db.execute('PRAGMA user_version').fetchone()[0] == 1
                  and db.execute('SELECT count(*) FROM library').fetchone()[0] == 1, 'STORE_SCHEMA_INVALID', 503)

    @classmethod
    def initialize(cls, root, name='문서 자료함'):
        # Only an explicit init may create own state; status/server never invents a replacement DB.
        check(isinstance(name, str) and name.strip() and len(name.encode('utf-8')) <= 800
              and not any(ord(c) < 32 for c in name), 'LIBRARY_NAME_INVALID')
        root = Path(root).expanduser().absolute()
        check('..' not in root.parts and not any(p.is_symlink() for p in (root, *root.parents)),
              'STORE_PATH_UNSAFE', 503)
        missing, parent = [], root
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for folder in reversed(missing):
            folder.mkdir(mode=0o700, exist_ok=True)
        private_path(root, directory=True)
        with file_lock(root / 'init.lock'):
            if (root / 'uploads.sqlite3').exists() or (root / 'uploads.sqlite3').is_symlink():
                return cls(root)
            locks = root / 'locks'
            locks.mkdir(mode=0o700, exist_ok=True)
            private_path(locks, directory=True)
            token = root / 'api-token'
            if not token.exists() and not token.is_symlink():
                fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write((secrets.token_urlsafe(32) + '\n').encode()); stream.flush(); os.fsync(stream.fileno())
            private_path(token)
            # Build and sync before rename: a crashed init cannot expose a half-built schema.
            stage = root / 'init.sqlite3'
            check(not stage.exists() and not stage.is_symlink(), 'STORE_INIT_INCOMPLETE', 503)
            fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            db = sqlite3.connect(stage)
            try:
                db.execute('PRAGMA auto_vacuum=FULL')
                db.execute('PRAGMA synchronous=FULL')
                db.executescript(SCHEMA)
                db.execute('INSERT INTO library(singleton,library_id,name) VALUES(1,?,?)', (str(uuid4()), name.strip()))
                db.commit()
            finally:
                db.close()
            with stage.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(stage, root / 'uploads.sqlite3')
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return cls(root)

    @contextmanager
    def connect(self, write=False):
        private_path(self.db_path)
        for suffix in ('-journal', '-wal', '-shm'):
            sidecar = Path(str(self.db_path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                private_path(sidecar)
        db = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA secure_delete=ON')
            check(db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
                  and db.execute('PRAGMA auto_vacuum').fetchone()[0] == 1, 'STORE_SCHEMA_INVALID', 503)
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def project(row):
        return {**{k: row[k] for k in ('job_id', 'sequence', 'name', 'filename', 'size_bytes', 'sha256',
                                       'state', 'source_document_id', 'attempts', 'error_code', 'created_at', 'updated_at')},
                'question_ready': False, 'workflow_started': False,
                'status_url': '/api/jobs/' + row['job_id']}

    def library(self):
        with self.connect() as db:
            row = db.execute('SELECT * FROM library').fetchone()
            return {**{k: row[k] for k in ('library_id', 'name', 'accepted_sequence', 'question_revision')},
                    'reserved_documents': db.execute('SELECT count(*) FROM jobs').fetchone()[0],
                    'pending_original_bytes': db.execute('SELECT coalesce(sum(length(original)),0) FROM jobs').fetchone()[0],
                    'question_ready': False, 'workflow_started': False}

    def jobs(self):
        with self.connect() as db:
            rows = db.execute('SELECT ' + PUBLIC_FIELDS + ' FROM jobs ORDER BY sequence').fetchall()
            return [self.project(row) for row in rows]

    def job(self, job_id):
        check(uid(job_id), 'JOB_ID_INVALID')
        with self.connect() as db:
            row = db.execute('SELECT ' + PUBLIC_FIELDS + ' FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            check(row is not None, 'JOB_NOT_FOUND', 404)
            return self.project(row)

    def by_request(self, key):
        check(uid(key), 'REQUEST_KEY_INVALID')
        with self.connect() as db:
            fields = ','.join('jobs.' + field for field in PUBLIC_FIELDS.split(','))
            row = db.execute('SELECT ' + fields + ' FROM jobs JOIN requests USING(job_id) WHERE request_key=?', (key,)).fetchone()
            check(row is not None, 'REQUEST_NOT_FOUND', 404)
            return self.project(row)

    def accept(self, key, name, filename, data, data_origin):
        check(uid(key), 'REQUEST_KEY_INVALID')
        meta = metadata(name, filename, data)
        try:
            target = readiness.origin(data_origin)
        except readiness.ReadinessError:
            raise UploadError('CONNECTION_INVALID', 503) from None
        request_hash = fingerprint(meta)
        with self.connect(write=True) as db:
            prior = db.execute('SELECT * FROM requests WHERE request_key=?', (key,)).fetchone()
            if prior:
                check(prior['request_sha256'] == request_hash, 'REQUEST_KEY_CONFLICT', 409)
                return self.project(db.execute('SELECT * FROM jobs WHERE job_id=?', (prior['job_id'],)).fetchone())
            # Same file/name/filename with a new client key still means one library member.
            row = db.execute('SELECT * FROM jobs WHERE request_sha256=?', (request_hash,)).fetchone()
            if row:
                check(db.execute('SELECT count(*) FROM requests WHERE job_id=?', (row['job_id'],)).fetchone()[0] < MAX_ALIASES,
                      'REQUEST_ALIAS_LIMIT', 409)
                db.execute('INSERT INTO requests VALUES(?,?,?)', (key, request_hash, row['job_id']))
                return self.project(row)
            check(db.execute('SELECT count(*) FROM jobs').fetchone()[0] < MAX_DOCUMENTS, 'DOCUMENT_CAPACITY_EXCEEDED', 409)
            seq = db.execute('SELECT accepted_sequence FROM library').fetchone()[0] + 1
            job_id = str(uuid4())
            intent = request_intent(job_id, 'data.upload', seq, meta)
            stamp = now()
            db.execute('INSERT INTO jobs(job_id,sequence,request_sha256,name,filename,size_bytes,sha256,data_origin,'
                       'upload_key,state,original,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, seq, request_hash, meta['name'], meta['filename'], meta['size_bytes'], meta['sha256'],
                        target, intent.key, 'upload_accepted', data, stamp, stamp))
            db.execute('INSERT INTO requests VALUES(?,?,?)', (key, request_hash, job_id))
            db.execute('UPDATE library SET accepted_sequence=? WHERE singleton=1', (seq,))
            return self.project(db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone())

    def dispatch(self, job_id, uploader, retry=False):
        check(uid(job_id), 'JOB_ID_INVALID')
        self.job(job_id)  # Unknown UUIDs must not create unbounded lock files.
        # OS releases the lock on process death; an ambiguous POST keeps the original intent.
        with file_lock(self.root / 'locks' / (job_id + '.lock')):
            with self.connect(write=True) as db:
                row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
                check(row is not None, 'JOB_NOT_FOUND', 404)
                if row['source_document_id']:
                    return self.project(row)
                if row['attempts'] > 0 and not retry:
                    return self.project(row)
                check(row['data_origin'] == uploader.origin, 'UPLOAD_TARGET_CHANGED', 409)
                check(row['attempts'] < MAX_ATTEMPTS, 'UPLOAD_REVIEW_REQUIRED', 409)
                data = row['original']
                meta = metadata(row['name'], row['filename'], data)
                check(meta['sha256'] == row['sha256'] and meta['size_bytes'] == row['size_bytes']
                      and fingerprint(meta) == row['request_sha256'], 'STORED_UPLOAD_CHANGED', 409)
                intent = request_intent(job_id, 'data.upload', row['sequence'], meta)
                check(intent.key == row['upload_key'], 'STORED_UPLOAD_CHANGED', 409)
                db.execute("UPDATE jobs SET state='upload_confirmation_required', attempts=attempts+1,"
                           "error_code='UPLOAD_OUTCOME_UNKNOWN',updated_at=? WHERE job_id=?", (now(), job_id))
            # No SQLite transaction is held across the upstream request.
            try:
                status, raw = uploader.upload(meta, data, intent.key)
                receipt = acceptance('data.upload', status, raw, {
                    'name': meta['name'], 'original_filename': meta['filename'],
                    'sha256': meta['sha256'], 'size_bytes': meta['size_bytes'], 'source_type': 'upload'})
            except (UploadError, ContractError) as exc:
                code = exc.code if isinstance(exc, UploadError) else 'UPLOAD_RECEIPT_INVALID'
                with self.connect(write=True) as db:
                    db.execute('UPDATE jobs SET error_code=?,updated_at=? WHERE job_id=?', (code, now(), job_id))
            else:
                with self.connect(write=True) as db:
                    db.execute("UPDATE jobs SET state='preparation_pending', source_document_id=?,original=NULL,"
                               "error_code=NULL,updated_at=? WHERE job_id=?", (receipt.resource_id, now(), job_id))
            return self.job(job_id)


class DataUploader:
    """Only Data's synchronous upload API. No redirect, proxy, automatic replay or GPU call."""
    def __init__(self, row, timeout=125, clock=time.monotonic):
        try:
            self.origin = readiness.origin(row['url'])
        except (KeyError, TypeError, readiness.ReadinessError):
            raise UploadError('CONNECTION_INVALID', 503) from None
        check(row.get('enabled', True) is True, 'DATA_DISABLED', 503)
        self.token_file = row.get('token_file')
        self.timeout, self.clock = timeout, clock

    def upload(self, meta, data, key):
        content_type, raw = encode_multipart(meta, data, key)
        headers = {'Content-Type': content_type, 'Idempotency-Key': key, 'Accept': 'application/json'}
        if self.token_file:
            try:
                token = readiness.read_file(self.token_file, limit=8192, credential=True).decode('ascii').strip()
                check(token and len(token) <= 4096 and all(32 < ord(c) < 127 for c in token), 'DATA_AUTH_UNAVAILABLE', 503)
            except (UnicodeError, readiness.ReadinessError):
                raise UploadError('DATA_AUTH_UNAVAILABLE', 503) from None
            headers['Authorization'] = 'Bearer ' + token
        request = Request(self.origin + '/api/source-documents', data=raw, headers=headers, method='POST')
        deadline = self.clock() + self.timeout
        try:
            with build_opener(ProxyHandler({}), readiness.NoRedirect()).open(request, timeout=self.timeout) as response:
                check(response.status == 200, 'DATA_UPLOAD_HTTP_UNCONFIRMED', 502)
                check(response.headers.get_content_type() == 'application/json', 'UPLOAD_RECEIPT_INVALID', 502)
                chunks, size = [], 0
                while True:
                    check(self.clock() < deadline, 'DATA_UPLOAD_TIMEOUT', 504)
                    part = response.read1(min(65536, (64 << 10) + 1 - size))
                    if not part:
                        break
                    chunks.append(part); size += len(part)
                    check(size <= 64 << 10, 'UPLOAD_RECEIPT_TOO_LARGE', 502)
                return 200, b''.join(chunks)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            code = 'DATA_REDIRECT_REFUSED' if 300 <= status < 400 else 'DATA_AUTH_REQUIRED' if status in (401, 403) else 'DATA_UPLOAD_HTTP_UNCONFIRMED'
            raise UploadError(code, 502) from None
        except (TimeoutError, URLError, OSError, HTTPException):
            raise UploadError('DATA_UPLOAD_OUTCOME_UNKNOWN', 502) from None
