"""Q06b-1 local migration, immutable targets and durable question head reads.

No head activation/CAS, AgentRun, HTTP, credential creation or inference. The
legacy library.question_revision=0 remains a compatibility projection; the
extension's question_head.revision is the authority for future question code.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from .contracts import ContractError, fingerprint, integer, uid
from .intake import UploadError, check
from . import question_schema as schema
from .question_targets import (checked_target, decoded_target, encoded_target,
                               MAX_REVISION, target_id)
from .search_tool import SearchToolStore
from .uploads import UploadStore, file_lock, now


@contextmanager
def question_lane(root):
    try:
        with file_lock(root / 'question-schema.lock'):
            yield
    except UploadError as exc:
        if exc.code == 'UPLOAD_BUSY':
            raise UploadError('QUESTION_STORE_BUSY', 409) from None
        raise


def stored_target(row, library_id):
    try:
        body = decoded_target(row['body'].encode('utf-8'))
        check(body['library_id'] == library_id == row['library_id']
              and row['target_id'] == target_id(body)
              and row['body_sha256'] == fingerprint(body)
              and row['body'] == encoded_target(body).decode('utf-8')
              and isinstance(row['created_at'], str) and bool(row['created_at']),
              'QUESTION_TARGET_CORRUPT', 503)
    except (ContractError, UploadError, AttributeError, TypeError, KeyError, UnicodeError):
        raise UploadError('QUESTION_TARGET_CORRUPT', 503) from None
    return {'target_id': row['target_id'], 'body': body,
            'body_sha256': row['body_sha256'], 'created_at': row['created_at']}


def read_state(db):
    library_id = schema.legacy_library(db)
    check(schema.schema_present(db), 'QUESTION_NOT_INITIALIZED', 503)
    rows = db.execute('SELECT * FROM question_targets LIMIT ?', (schema.MAX_TARGETS + 1,)).fetchall()
    check(len(rows) <= schema.MAX_TARGETS, 'QUESTION_TARGET_LIMIT_EXCEEDED', 503)
    targets = {row['target_id']: stored_target(row, library_id) for row in rows}
    heads = db.execute('SELECT * FROM question_head').fetchall()
    check(len(heads) == 1 and heads[0]['singleton'] == 1 and heads[0]['library_id'] == library_id
          and integer(heads[0]['revision'], 0, MAX_REVISION), 'QUESTION_HEAD_INVALID', 503)
    row = heads[0]
    target = targets.get(row['target_id'])
    check((row['revision'] == 0 and row['target_id'] is None)
          or (row['revision'] > 0 and target is not None
              and target['body']['revision'] == row['revision']), 'QUESTION_HEAD_INVALID', 503)
    return {'library_id': library_id, 'revision': row['revision'],
            'target_id': row['target_id'], 'target': target}, targets


class QuestionStore:
    def __init__(self, uploads):
        SearchToolStore(uploads)  # Existing Q06a must already be initialized.
        self.uploads, self.root = uploads, uploads.root
        with uploads.connect() as db:
            read_state(db)

    @classmethod
    def migrate(cls, uploads):
        SearchToolStore(uploads)
        with question_lane(uploads.root):
            with uploads.connect(write=True) as db:
                library_id = schema.legacy_library(db)
                if not schema.schema_present(db):
                    schema.install(db, library_id)
                read_state(db)  # Invalid/partial schemas are refused, never repaired/reset.
        return cls(uploads)

    def head(self):
        with self.uploads.connect() as db:
            return read_state(db)[0]

    def target(self, identifier):
        check(uid(identifier), 'QUESTION_TARGET_ID_INVALID')
        with self.uploads.connect() as db:
            targets = read_state(db)[1]
            check(identifier in targets, 'QUESTION_TARGET_NOT_FOUND', 404)
            return targets[identifier]

    def save_target(self, body):
        """Freeze structurally valid references; this does not verify or activate them."""
        body = checked_target(body)
        identifier, digest = target_id(body), fingerprint(body)
        with question_lane(self.root):
            with self.uploads.connect(write=True) as db:
                head, targets = read_state(db)
                check(body['library_id'] == head['library_id'], 'QUESTION_LIBRARY_MISMATCH', 409)
                if identifier in targets:
                    check(targets[identifier]['body_sha256'] == digest
                          and targets[identifier]['body'] == body, 'QUESTION_TARGET_CONFLICT', 409)
                    return targets[identifier]
                check(len(targets) < schema.MAX_TARGETS, 'QUESTION_TARGET_LIMIT_EXCEEDED', 409)
                db.execute('INSERT INTO question_targets VALUES(?,?,?,?,?)',
                           (identifier, body['library_id'], encoded_target(body).decode('utf-8'), digest, now()))
                return stored_target(db.execute('SELECT * FROM question_targets WHERE target_id=?',
                                               (identifier,)).fetchone(), head['library_id'])

    def status(self):
        with self.uploads.connect() as db:
            head, targets = read_state(db)
        return {'schema': 1, 'question_storage_schema': schema.VERSION, 'upload_schema': 1,
                'head_revision': head['revision'], 'head_target_id': head['target_id'],
                'target_count': len(targets), 'max_targets': schema.MAX_TARGETS,
                'revision_authority': 'question_head', 'legacy_question_revision': 0,
                'question_ready': False, 'head_activation_implemented': False,
                'production_question_verified': False,
                'next_step': 'Q06b_2_fixed_QA_Agent_adapter'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('migrate', 'status'))
    parser.add_argument('--state', default=str(Path.home() / '.local/state/agent-project-document-qa/uploads'))
    args = parser.parse_args(argv)
    try:
        uploads = UploadStore(args.state)
        store = QuestionStore.migrate(uploads) if args.action == 'migrate' else QuestionStore(uploads)
        result = store.status()
        result.update(result='passed', production_api_requests=0, inference_requested=False, GPU_requested=False)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False)); return 0
    except UploadError as exc:
        code = exc.code
    except (OSError, sqlite3.Error, ContractError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        code = 'LOCAL_QUESTION_STATE_UNCERTAIN'
    print(json.dumps({'result': 'attention_required', 'code': code, 'question_ready': False,
                      'production_api_requests': 0, 'inference_requested': False, 'GPU_requested': False})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
