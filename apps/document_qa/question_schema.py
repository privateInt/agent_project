"""Additive question storage schema; upload user_version/library stay unchanged."""
import hashlib

from .contracts import uid
from .intake import check

VERSION = 1
MAX_TARGETS = 100
OBJECTS = {
    'question_metadata': '''CREATE TABLE question_metadata(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 version INTEGER NOT NULL CHECK(version=1), migration_sha256 TEXT NOT NULL
)''',
    'question_targets': '''CREATE TABLE question_targets(
 target_id TEXT PRIMARY KEY, library_id TEXT NOT NULL,
 body TEXT NOT NULL CHECK(typeof(body)='text' AND length(body)>0 AND length(body)<=131072),
 body_sha256 TEXT NOT NULL CHECK(length(body_sha256)=64), created_at TEXT NOT NULL,
 UNIQUE(target_id,library_id)
)''',
    'question_head': '''CREATE TABLE question_head(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1) REFERENCES library(singleton),
 library_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),
 target_id TEXT,
 CHECK((revision=0 AND target_id IS NULL) OR (revision>0 AND target_id IS NOT NULL)),
 FOREIGN KEY(target_id,library_id) REFERENCES question_targets(target_id,library_id)
)''',
    'question_target_library_guard': '''CREATE TRIGGER question_target_library_guard
 BEFORE INSERT ON question_targets
 WHEN NEW.library_id<>(SELECT library_id FROM library WHERE singleton=1)
 BEGIN SELECT RAISE(ABORT,'QUESTION_LIBRARY_MISMATCH'); END''',
    'question_target_no_update': '''CREATE TRIGGER question_target_no_update
 BEFORE UPDATE ON question_targets
 BEGIN SELECT RAISE(ABORT,'QUESTION_TARGET_IMMUTABLE'); END''',
    'question_target_no_delete': '''CREATE TRIGGER question_target_no_delete
 BEFORE DELETE ON question_targets
 BEGIN SELECT RAISE(ABORT,'QUESTION_TARGET_IMMUTABLE'); END''',
}
MIGRATION_SHA256 = hashlib.sha256('\n'.join(OBJECTS.values()).encode()).hexdigest()


def normalized_sql(sql):
    return ' '.join(sql.split()) if isinstance(sql, str) else ''


def schema_present(db):
    rows = db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'question_*'").fetchall()
    if not rows:
        return False
    check({r['name'] for r in rows} == set(OBJECTS)
          and all(normalized_sql(r['sql']) == normalized_sql(OBJECTS[r['name']]) for r in rows),
          'QUESTION_SCHEMA_INVALID', 503)
    rows = db.execute('SELECT singleton,version,migration_sha256 FROM question_metadata').fetchall()
    check(len(rows) == 1 and rows[0]['singleton'] == 1 and rows[0]['version'] == VERSION
          and rows[0]['migration_sha256'] == MIGRATION_SHA256, 'QUESTION_SCHEMA_INVALID', 503)
    return True


def legacy_library(db):
    rows = db.execute('SELECT singleton,library_id,question_revision FROM library').fetchall()
    check(db.execute('PRAGMA user_version').fetchone()[0] == 1
          and len(rows) == 1 and rows[0]['singleton'] == 1 and uid(rows[0]['library_id'])
          and rows[0]['question_revision'] == 0, 'QUESTION_LEGACY_SCHEMA_INVALID', 503)
    return rows[0]['library_id']


def install(db, library_id):
    # executescript would implicitly commit; each DDL shares uploads.connect's transaction.
    for statement in OBJECTS.values():
        db.execute(statement)
    db.execute('INSERT INTO question_metadata VALUES(1,?,?)', (VERSION, MIGRATION_SHA256))
    db.execute('INSERT INTO question_head VALUES(1,?,0,NULL)', (library_id,))
