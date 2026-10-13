"""Q06b-3 additive CAS receipts; existing question/upload/Agent schemas stay intact."""
import hashlib

from .intake import check
from .question_schema import normalized_sql

VERSION = 1
MAX_ACTIVATIONS = 100
OBJECTS = {
    'qa_head_metadata': '''CREATE TABLE qa_head_metadata(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1),migration_sha256 TEXT NOT NULL
)''',
    'qa_head_activations': '''CREATE TABLE qa_head_activations(
 client_key TEXT PRIMARY KEY,request TEXT NOT NULL CHECK(length(request)<=2048),request_sha256 TEXT NOT NULL,
 plan_id TEXT NOT NULL REFERENCES qa_agent_plans(plan_id),
 expected_revision INTEGER NOT NULL UNIQUE CHECK(typeof(expected_revision)='integer' AND expected_revision>=0),
 previous_target_id TEXT REFERENCES question_targets(target_id),
 target_id TEXT NOT NULL UNIQUE REFERENCES question_targets(target_id),target_sha256 TEXT NOT NULL,
 proof TEXT NOT NULL CHECK(length(proof)<=8192),proof_sha256 TEXT NOT NULL,created_at TEXT NOT NULL
)''',
    'qa_head_activation_no_update': '''CREATE TRIGGER qa_head_activation_no_update BEFORE UPDATE ON qa_head_activations
 BEGIN SELECT RAISE(ABORT,'QUESTION_HEAD_RECEIPT_IMMUTABLE'); END''',
    'qa_head_activation_no_delete': '''CREATE TRIGGER qa_head_activation_no_delete BEFORE DELETE ON qa_head_activations
 BEGIN SELECT RAISE(ABORT,'QUESTION_HEAD_RECEIPT_IMMUTABLE'); END''',
}
MIGRATION_SHA256 = hashlib.sha256('\n'.join(OBJECTS.values()).encode()).hexdigest()


def present(db):
    rows = db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'qa_head_*'").fetchall()
    if not rows:
        return False
    check({r['name'] for r in rows} == set(OBJECTS)
          and all(normalized_sql(r['sql']) == normalized_sql(OBJECTS[r['name']]) for r in rows),
          'QUESTION_HEAD_SCHEMA_INVALID', 503)
    rows = db.execute('SELECT * FROM qa_head_metadata').fetchall()
    check(len(rows) == 1 and rows[0]['singleton'] == 1 and rows[0]['version'] == VERSION
          and rows[0]['migration_sha256'] == MIGRATION_SHA256, 'QUESTION_HEAD_SCHEMA_INVALID', 503)
    return True


def install(db):
    for statement in OBJECTS.values():
        db.execute(statement)
    db.execute('INSERT INTO qa_head_metadata VALUES(1,?,?)', (VERSION, MIGRATION_SHA256))
