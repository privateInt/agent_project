"""Persist submitted inputs for button-only retry with the original request key."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from data_api import encode, identifier
from data_state import ID_FIELD, match, pack, unpack

RETENTION_SECONDS = 7 * 24 * 60 * 60


class RequestStoreError(ValueError):
    pass


def receipt(kind, value):
    if not isinstance(value, dict): raise ValueError('invalid receipt')
    identifier(value[ID_FIELD['source' if kind == 'upload' else kind]])
    if kind != 'upload': identifier(value['operation_id'])
    return value


class RequestStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('DATA_UI_REQUEST_DIR', '/var/lib/data-console'))

    @contextmanager
    def connection(self):
        db = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.directory / 'requests.sqlite3'
            db = sqlite3.connect(path, timeout=10)
            path.chmod(0o600)
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                key TEXT PRIMARY KEY, ticket TEXT NOT NULL, body TEXT, file BLOB,
                accepted TEXT, created REAL NOT NULL)''')
            with db:
                yield db
        except (OSError, sqlite3.Error) as e:
            raise RequestStoreError('요청을 보관하거나 불러오지 못했습니다. 잠시 후 다시 시도하세요.') from e
        finally:
            if db is not None: db.close()

    def _read(self, db, t, base):
        unpack(pack(t))
        if t['gateway'] != hashlib.sha256(base.encode()).hexdigest():
            raise RequestStoreError('서버 연결 설정이 바뀌어 이전 요청을 다시 보낼 수 없습니다.')
        row = db.execute('SELECT ticket,body,file,accepted,created FROM requests WHERE key=?', (t['key'],)).fetchone()
        if row is None or row[4] < time.time() - RETENTION_SECONDS:
            return None
        if row[0] != pack(t):
            raise RequestStoreError('보관된 요청과 이 화면의 정보가 일치하지 않습니다.')
        try:
            if row[3] is not None:
                return {'accepted': receipt(t['kind'], json.loads(row[3]))}
            body = json.loads(row[1])
            if not isinstance(body, dict): raise ValueError('invalid body')
            match(t, t['kind'], body, base, row[2])
            return {'payload': (t['kind'], body, row[2])}
        except (ValueError, TypeError, KeyError) as e:
            raise RequestStoreError('보관된 요청을 확인할 수 없습니다. 작업 현황을 확인하세요.') from e

    def load(self, t, base):
        with self.connection() as db:
            return self._read(db, t, base)

    def prepare(self, t, kind, body, base, file=None):
        match(t, kind, body, base, file)
        with self.connection() as db:
            db.execute('DELETE FROM requests WHERE created < ?', (time.time() - RETENTION_SECONDS,))
            db.execute('INSERT OR IGNORE INTO requests(key,ticket,body,file,created) VALUES(?,?,?,?,?)',
                       (t['key'], pack(t), encode(body).decode(), file, time.time()))
            saved = self._read(db, t, base)
            if saved is None: raise RequestStoreError('보관된 요청을 확인할 수 없습니다.')
            return saved.get('accepted')

    def complete(self, t, accepted):
        value = receipt(t['kind'], accepted)
        with self.connection() as db:
            # Once accepted, keep only the result IDs; release uploaded bytes.
            db.execute('UPDATE requests SET body=NULL,file=NULL,accepted=? WHERE key=? AND ticket=?',
                       (encode(value).decode(), t['key'], pack(t)))
