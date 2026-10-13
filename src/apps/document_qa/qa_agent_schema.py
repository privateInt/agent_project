"""Q06b-2 atomic extension; Q06b-1 and legacy upload schemas stay unchanged."""
import hashlib

from .intake import check
from .question_schema import normalized_sql

VERSION = 1
MAX_PLANS = 100
MAX_QUESTIONS = 100
OBJECTS = {
    'qa_agent_metadata': '''CREATE TABLE qa_agent_metadata(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1),migration_sha256 TEXT NOT NULL
)''',
    'qa_agent_plans': '''CREATE TABLE qa_agent_plans(
 plan_id TEXT PRIMARY KEY,body TEXT NOT NULL,body_sha256 TEXT NOT NULL,created_at TEXT NOT NULL
)''',
    'qa_agent_steps': '''CREATE TABLE qa_agent_steps(
 owner_id TEXT NOT NULL,operation TEXT NOT NULL,intent_owner TEXT NOT NULL,
 path TEXT NOT NULL,body TEXT NOT NULL,request_sha256 TEXT NOT NULL,request_key TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0 AND attempts<=16),
 state TEXT NOT NULL CHECK(state IN ('pending','confirmation_required','accepted','waiting','verified','failed','attention_required')),
 receipt TEXT,summary TEXT,error_code TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(owner_id,operation)
)''',
    'qa_agent_questions': '''CREATE TABLE qa_agent_questions(
 question_id TEXT PRIMARY KEY,client_key TEXT NOT NULL UNIQUE,question TEXT NOT NULL,
 plan_id TEXT NOT NULL REFERENCES qa_agent_plans(plan_id),
 target_id TEXT NOT NULL REFERENCES question_targets(target_id),target_sha256 TEXT NOT NULL,
 request_sha256 TEXT NOT NULL,created_at TEXT NOT NULL
)''',
    'qa_agent_chat': '''CREATE TABLE qa_agent_chat(
 plan_id TEXT PRIMARY KEY REFERENCES qa_agent_plans(plan_id),snapshot TEXT NOT NULL,snapshot_sha256 TEXT NOT NULL
)''',
    'qa_agent_chat_no_update': '''CREATE TRIGGER qa_agent_chat_no_update BEFORE UPDATE ON qa_agent_chat
 BEGIN SELECT RAISE(ABORT,'QA_AGENT_CHAT_IMMUTABLE'); END''',
    'qa_agent_chat_no_delete': '''CREATE TRIGGER qa_agent_chat_no_delete BEFORE DELETE ON qa_agent_chat
 BEGIN SELECT RAISE(ABORT,'QA_AGENT_CHAT_IMMUTABLE'); END''',
    'qa_agent_step_no_update': '''CREATE TRIGGER qa_agent_step_no_update
 BEFORE UPDATE OF owner_id,operation,intent_owner,path,body,request_sha256,request_key ON qa_agent_steps
 BEGIN SELECT RAISE(ABORT,'QA_AGENT_INTENT_IMMUTABLE'); END''',
    'qa_agent_step_no_delete': '''CREATE TRIGGER qa_agent_step_no_delete BEFORE DELETE ON qa_agent_steps
 BEGIN SELECT RAISE(ABORT,'QA_AGENT_INTENT_IMMUTABLE'); END''',
    'qa_agent_plan_no_update': '''CREATE TRIGGER qa_agent_plan_no_update BEFORE UPDATE ON qa_agent_plans
 BEGIN SELECT RAISE(ABORT,'QA_AGENT_PLAN_IMMUTABLE'); END''',
    'qa_agent_plan_no_delete': '''CREATE TRIGGER qa_agent_plan_no_delete BEFORE DELETE ON qa_agent_plans
 BEGIN SELECT RAISE(ABORT,'QA_AGENT_PLAN_IMMUTABLE'); END''',
    'qa_agent_question_no_update': '''CREATE TRIGGER qa_agent_question_no_update BEFORE UPDATE ON qa_agent_questions
 BEGIN SELECT RAISE(ABORT,'QA_QUESTION_IMMUTABLE'); END''',
    'qa_agent_question_no_delete': '''CREATE TRIGGER qa_agent_question_no_delete BEFORE DELETE ON qa_agent_questions
 BEGIN SELECT RAISE(ABORT,'QA_QUESTION_IMMUTABLE'); END''',
}
MIGRATION_SHA256 = hashlib.sha256('\n'.join(OBJECTS.values()).encode()).hexdigest()


def present(db):
    rows = db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'qa_agent_*'").fetchall()
    if not rows:
        return False
    check({r['name'] for r in rows} == set(OBJECTS)
          and all(normalized_sql(r['sql']) == normalized_sql(OBJECTS[r['name']]) for r in rows),
          'QA_AGENT_SCHEMA_INVALID', 503)
    rows = db.execute('SELECT * FROM qa_agent_metadata').fetchall()
    check(len(rows) == 1 and rows[0]['singleton'] == 1 and rows[0]['version'] == VERSION
          and rows[0]['migration_sha256'] == MIGRATION_SHA256, 'QA_AGENT_SCHEMA_INVALID', 503)
    check(db.execute('SELECT count(*) FROM qa_agent_plans').fetchone()[0] <= MAX_PLANS
          and db.execute('SELECT count(*) FROM qa_agent_questions').fetchone()[0] <= MAX_QUESTIONS,
          'QA_AGENT_STATE_LIMIT_EXCEEDED', 503)
    return True


def install(db):
    for statement in OBJECTS.values():
        db.execute(statement)
    db.execute('INSERT INTO qa_agent_metadata VALUES(1,?,?)', (VERSION, MIGRATION_SHA256))
