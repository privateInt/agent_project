"""Q06b-1 real private SQLite, process interruption and legacy compatibility."""
import copy
from contextlib import redirect_stdout
import http.client
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from apps.document_qa.api import make_server
from apps.document_qa.contracts import ContractError, fingerprint, validate_head
from apps.document_qa.indexing import IndexingStore
from apps.document_qa.intake import UploadError
from apps.document_qa import question_schema as schema
from apps.document_qa import question_store as storage
from apps.document_qa.question_store import QuestionStore, main
from apps.document_qa.question_targets import (MAX_REVISION, MAX_TARGET_BYTES, as_head,
    checked_target, decoded_target, encoded_target, target_id)
from apps.document_qa.refinement import RefinementStore
from apps.document_qa.search_tool import SearchToolStore
from apps.document_qa.uploads import UploadStore, file_lock
from apps.document_qa.tests.test_indexing_flow import Fixture as FlowFixture
from apps.document_qa.tests.test_refinement import running

PROJECT = Path(__file__).absolute().parents[3]


def candidate(library_id=None, revision=1, generation=7):
    doc = {'canonical_id': str(uuid4()), 'canonical_sha256': 'a' * 64, 'size_bytes': 42,
           'source_document_id': str(uuid4()), 'source_sha256': 'b' * 64,
           'refinement_run_id': str(uuid4()), 'schema_version': 'canonical-document-v1',
           'processor_version': 'cpu-parser-v1', 'rules_version': 'basic-cleaning-v1',
           'processor_image_id': 'sha256:' + 'c' * 64, 'format': 'text'}
    return {'schema': 'question-target-v1', 'library_id': library_id or str(uuid4()),
            'revision': revision, 'indexing_generation': generation, 'documents': [doc],
            **{k: str(uuid4()) for k in ('index_id', 'index_version_id',
                       'embedding_deployment_id', 'tool_version_id', 'agent_version_id')},
            'embedding_space_id': 'es_' + 'd' * 64,
            'pins': {**{k: str(uuid4()) for k in ('embedding_model_version_id',
                'knowledge_connection_version_id', 'embedding_connection_version_id',
                'chat_connection_version_id', 'chat_deployment_id', 'chat_model_version_id')},
                'embedding_profile_id': 'snowflake-ko-v1', 'chat_profile_id': 'gemma-chat-v1',
                'tool_definition_sha256': 'e' * 64, 'agent_definition_sha256': 'f' * 64}}


class TargetTests(unittest.TestCase):
    def test_full_references_roundtrip_and_Q01_head_contract(self):
        body = candidate()
        self.assertEqual(decoded_target(encoded_target(body)), body)
        validate_head(as_head(checked_target(body)))
        self.assertEqual(as_head(body).documents[0].sha256, body['documents'][0]['canonical_sha256'])

    def test_document_order_canonicalization_has_one_identity(self):
        body = candidate(); body['documents'].append(candidate()['documents'][0])
        original = copy.deepcopy(body)
        reversed_body = copy.deepcopy(body); reversed_body['documents'].reverse()
        self.assertEqual(target_id(checked_target(body)), target_id(checked_target(reversed_body)))
        self.assertEqual(body, original)

    def test_revision_and_indexing_generation_have_distinct_integer_limits(self):
        body = candidate(revision=2, generation=7)
        self.assertEqual((checked_target(body)['revision'], body['indexing_generation']), (2, 7))
        for field in ('revision', 'indexing_generation'):
            for value in (0, -1, True, 1.0, '1', MAX_REVISION + 1):
                with self.subTest(field=field, value=value):
                    changed = copy.deepcopy(body); changed[field] = value
                    with self.assertRaises(UploadError): checked_target(changed)

    def test_unknown_fields_credentials_and_readiness_claims_are_refused(self):
        for field, value in (('credentials', {'token': 'secret'}), ('question_ready', True),
                             ('hit_text', 'document contents'), ('verified', True)):
            body = candidate(); body[field] = value
            with self.subTest(field=field), self.assertRaises(UploadError): checked_target(body)

    def test_all_pins_are_required_and_typed(self):
        for field in candidate()['pins']:
            body = candidate(); body['pins'].pop(field)
            with self.subTest(field=field), self.assertRaises(UploadError): checked_target(body)
        for field, value in (('chat_deployment_id', 'bad'), ('tool_definition_sha256', 'x' * 64),
                             ('chat_profile_id', 'secret\nvalue'), ('embedding_model_version_id', None)):
            body = candidate(); body['pins'][field] = value
            with self.subTest(field=field), self.assertRaises(UploadError): checked_target(body)

    def test_invalid_document_subset_duplicates_and_binding_are_refused(self):
        for mutation in ('empty', 'duplicate', 'hash', 'identity', 'space'):
            body = candidate()
            if mutation == 'empty': body['documents'] = []
            elif mutation == 'duplicate': body['documents'] *= 2
            elif mutation == 'hash': body['documents'][0]['canonical_sha256'] = 'wrong'
            elif mutation == 'identity': body['library_id'] = 'wrong'
            else: body['embedding_space_id'] = 'wrong'
            with self.subTest(mutation=mutation), self.assertRaises((ContractError, UploadError)): checked_target(body)

    def test_bounded_JSON_rejects_duplicates_nonfinite_and_bad_UTF8(self):
        for raw in (b'{"schema":1,"schema":2}', b'{"x":NaN}', b'{"x":"\xff"}',
                    b'x' * (MAX_TARGET_BYTES + 1)):
            with self.subTest(size=len(raw)), self.assertRaises((UploadError, ContractError)): decoded_target(raw)
        body = candidate(); body['pins']['chat_profile_id'] = 'x' * MAX_TARGET_BYTES
        with self.assertRaisesRegex(UploadError, 'QUESTION_TARGET_TOO_LARGE'): checked_target(body)


class Fixture(FlowFixture):
    def setUp(self):
        super().setUp()
        self.tools = SearchToolStore.initialize(self.uploads)
        self.body = candidate(self.uploads.library()['library_id'])

    def snapshot(self, extension=False):
        with self.uploads.connect() as db:
            objects = db.execute("SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if not extension: objects = [r for r in objects if not r['name'].startswith('question_')]
            tables = {r['name']: tuple(tuple(row) for row in db.execute('SELECT * FROM ' + r['name'] + ' ORDER BY rowid'))
                      for r in objects if r['type'] == 'table'}
            version = db.execute('PRAGMA user_version').fetchone()[0]
        secrets = {p.name: p.read_bytes() for p in self.root.iterdir()
                   if p.is_file() and ('token' in p.name or 'secret' in p.name)}
        return tuple(tuple(r) for r in objects), tables, version, secrets, self.key.read_bytes()

    def extension_names(self):
        with self.uploads.connect() as db:
            return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name GLOB 'question_*' ORDER BY name")]

    def cli_process(self, action):
        return subprocess.run([sys.executable, '-B', '-m', 'apps.document_qa.question_store',
                               action, '--state', str(self.root)], cwd=PROJECT, capture_output=True, text=True, timeout=10)

    def future_head_fixture(self, store, revision=1):
        """Fixture-only future CAS state; production Q06b-1 exposes no activation."""
        body = copy.deepcopy(self.body); body['revision'] = revision
        target = store.save_target(body)
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE question_head SET revision=?,target_id=? WHERE singleton=1',
                       (revision, target['target_id']))
        return target


class MigrationTests(Fixture):
    def test_migration_preserves_BLOB_aliases_jobs_Flow_receipts_setup_and_secrets(self):
        job, grant, receipt = self.started()
        self.flow.step(grant, lambda **kwargs: self.fail('paused step must not call Knowledge'),
                       lambda remaining: (_ for _ in ()).throw(UploadError('GPU_VERIFICATION_DEFERRED', 409)))
        self.assertEqual(self.flow.job(job['job_id'])['state'], 'blocked')
        pending = self.uploads.accept(str(uuid4()), 'pending', 'pending.txt', b'pending original', 'http://127.0.0.1:14000')
        self.uploads.accept(str(uuid4()), 'pending', 'pending.txt', b'pending original', 'http://127.0.0.1:14000')
        before = self.snapshot()
        store = QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(store.head()['revision'], 0)
        self.assertIsNone(store.head()['target'])
        self.assertEqual(self.uploads.job(pending['job_id'])['state'], 'upload_accepted')
        self.assertFalse(store.status()['question_ready'])

    def test_migrate_is_idempotent_after_process_reopen(self):
        store = QuestionStore.migrate(self.uploads); before = self.snapshot(extension=True)
        again = QuestionStore.migrate(UploadStore(self.root))
        self.assertEqual(store.status(), again.status())
        self.assertEqual(before, self.snapshot(extension=True))
        self.assertEqual(set(self.extension_names()), set(schema.OBJECTS))

    def test_status_never_creates_schema_or_lock(self):
        before = self.snapshot(extension=True)
        with self.assertRaisesRegex(UploadError, 'QUESTION_NOT_INITIALIZED'): QuestionStore(self.uploads)
        result = self.cli_process('status')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['code'], 'QUESTION_NOT_INITIALIZED')
        self.assertEqual(before, self.snapshot(extension=True))
        self.assertFalse((self.root / 'question-schema.lock').exists())

    def test_missing_state_never_creates_a_replacement_DB(self):
        missing = self.root.parent / 'missing'
        stream = io.StringIO()
        with redirect_stdout(stream): self.assertEqual(main(['migrate', '--state', str(missing)]), 2)
        self.assertEqual(json.loads(stream.getvalue())['code'], 'STORE_NOT_INITIALIZED')
        self.assertFalse(missing.exists())

    def test_Q06a_is_required_and_never_implicitly_initialized(self):
        root = self.root.parent / 'without-tools'; uploads = UploadStore.initialize(root)
        RefinementStore.initialize(uploads); IndexingStore.initialize(uploads)
        with self.assertRaisesRegex(UploadError, 'SEARCH_TOOL_NOT_INITIALIZED'): QuestionStore.migrate(uploads)
        with uploads.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name GLOB 'question_*'").fetchone()[0], 0)

    def test_partial_or_foreign_schema_is_refused_without_repair(self):
        with self.uploads.connect(write=True) as db: db.execute('CREATE TABLE question_unknown(user_value TEXT)')
        before = self.snapshot(extension=True)
        with self.assertRaisesRegex(UploadError, 'QUESTION_SCHEMA_INVALID'): QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_metadata_fingerprint_tampering_is_refused_without_reset(self):
        QuestionStore.migrate(self.uploads)
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE question_metadata SET migration_sha256=?', ('wrong-version',))
        before = self.snapshot(extension=True)
        with self.assertRaisesRegex(UploadError, 'QUESTION_SCHEMA_INVALID'): QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_altered_columns_or_removed_trigger_are_refused(self):
        QuestionStore.migrate(self.uploads)
        with self.uploads.connect(write=True) as db: db.execute('DROP TRIGGER question_target_no_update')
        before = self.snapshot(extension=True)
        with self.assertRaisesRegex(UploadError, 'QUESTION_SCHEMA_INVALID'): QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_missing_head_or_foreign_library_is_refused_without_reset(self):
        QuestionStore.migrate(self.uploads)
        with self.uploads.connect(write=True) as db: db.execute('UPDATE question_head SET library_id=?', (str(uuid4()),))
        before = self.snapshot(extension=True)
        with self.assertRaisesRegex(UploadError, 'QUESTION_HEAD_INVALID'): QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_invalid_legacy_identity_is_refused_before_migration(self):
        with self.uploads.connect(write=True) as db: db.execute("UPDATE library SET library_id='invalid'")
        before = self.snapshot(extension=True)
        with self.assertRaisesRegex(UploadError, 'QUESTION_LEGACY_SCHEMA_INVALID'): QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_mid_DDL_error_rolls_back_all_new_objects_and_keeps_old_rows(self):
        before = self.snapshot(extension=True)
        def failed(db, library_id):
            db.execute(schema.OBJECTS['question_metadata']); db.execute('INVALID migration SQL')
        with patch.object(schema, 'install', failed), self.assertRaises(sqlite3.Error): QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot(extension=True))
        self.assertEqual(self.extension_names(), [])
        self.assertEqual(QuestionStore.migrate(self.uploads).head()['revision'], 0)

    def test_KeyboardInterrupt_before_commit_rolls_back_all_new_objects(self):
        before = self.snapshot(extension=True); install = schema.install
        def interrupted(db, library_id): install(db, library_id); raise KeyboardInterrupt()
        with patch.object(schema, 'install', interrupted), self.assertRaises(KeyboardInterrupt): QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_abrupt_process_exit_recovers_after_first_or_last_DDL(self):
        program = '''import os,sys
from apps.document_qa import question_schema as schema
from apps.document_qa.question_store import QuestionStore
from apps.document_qa.uploads import UploadStore
install=schema.install
def crash(db,library_id):
    if sys.argv[2]=='first': db.execute(schema.OBJECTS['question_metadata'])
    else: install(db,library_id)
    os._exit(19)
schema.install=crash
QuestionStore.migrate(UploadStore(sys.argv[1]))
'''
        before = self.snapshot(extension=True)
        for position in ('first', 'last'):
            with self.subTest(position=position):
                result = subprocess.run([sys.executable, '-B', '-c', program, str(self.root), position],
                                        cwd=PROJECT, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 19, result.stderr)
                UploadStore(self.root)  # A fresh process connection performs hot-journal recovery.
                self.assertEqual(before, self.snapshot(extension=True))
                self.assertEqual(self.extension_names(), [])
        self.assertEqual(QuestionStore.migrate(self.uploads).status()['head_revision'], 0)

    def test_committed_migration_with_lost_output_reuses_existing_state(self):
        program = '''import os,sys
from apps.document_qa.question_store import QuestionStore
from apps.document_qa.uploads import UploadStore
QuestionStore.migrate(UploadStore(sys.argv[1]))
os._exit(23)
'''
        result = subprocess.run([sys.executable, '-B', '-c', program, str(self.root)], cwd=PROJECT,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        before = self.snapshot(extension=True)
        self.assertEqual(QuestionStore.migrate(UploadStore(self.root)).head()['revision'], 0)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_concurrent_process_migration_is_busy_then_explicit_retry_succeeds(self):
        entered, release = threading.Event(), threading.Event(); errors = []; install = schema.install
        def paused(db, library_id):
            install(db, library_id); entered.set()
            if not release.wait(5): raise RuntimeError('fixture release timeout')
        def worker():
            try: QuestionStore.migrate(self.uploads)
            except BaseException as exc: errors.append(exc)
        with patch.object(schema, 'install', paused):
            thread = threading.Thread(target=worker); thread.start()
            try:
                self.assertTrue(entered.wait(3)); result = self.cli_process('migrate')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stdout)['code'], 'QUESTION_STORE_BUSY')
            finally: release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(self.cli_process('migrate').returncode, 0)

    def test_existing_future_head_and_history_are_not_reset_by_migration(self):
        store = QuestionStore.migrate(self.uploads); target = self.future_head_fixture(store)
        next_body = copy.deepcopy(self.body); next_body['revision'] = 2; store.save_target(next_body)
        before = self.snapshot(extension=True)
        reopened = QuestionStore.migrate(UploadStore(self.root))
        self.assertEqual(reopened.head()['target'], target)
        self.assertEqual(reopened.head()['revision'], 1)
        self.assertEqual(before, self.snapshot(extension=True))
        self.assertFalse(reopened.status()['head_activation_implemented'])

    def test_legacy_upload_writer_waits_for_migration_and_is_not_lost(self):
        entered, release, started, finished = [threading.Event() for _ in range(4)]
        errors, jobs = [], []; install = schema.install; key = str(uuid4())
        def paused(db, library_id):
            install(db, library_id); entered.set()
            if not release.wait(5): raise RuntimeError('fixture release timeout')
        def migrate():
            try: QuestionStore.migrate(self.uploads)
            except BaseException as exc: errors.append(exc)
        def upload():
            started.set()
            try: jobs.append(self.uploads.accept(key, 'concurrent', 'concurrent.txt', b'concurrent content', self.uploaded.origin))
            except BaseException as exc: errors.append(exc)
            finally: finished.set()
        with patch.object(schema, 'install', paused):
            migration = threading.Thread(target=migrate); migration.start()
            writer = threading.Thread(target=upload)
            try:
                self.assertTrue(entered.wait(3))
                status = self.cli_process('status')
                self.assertEqual(json.loads(status.stdout)['code'], 'QUESTION_NOT_INITIALIZED')
                writer.start(); self.assertTrue(started.wait(3)); self.assertFalse(finished.wait(0.1))
            finally:
                release.set(); migration.join(5)
                if writer.ident is not None: writer.join(5)
        self.assertFalse(migration.is_alive()); self.assertFalse(writer.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(self.uploads.by_request(key)['job_id'], jobs[0]['job_id'])
        self.assertEqual(self.uploads.library()['accepted_sequence'], 1)
        self.assertEqual(QuestionStore(self.uploads).head()['revision'], 0)

    def test_CLI_migrate_and_status_are_local_only_and_redact_references(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('no network')):
            for action in ('migrate', 'status'):
                stream = io.StringIO()
                with redirect_stdout(stream): self.assertEqual(main([action, '--state', str(self.root)]), 0)
                result = json.loads(stream.getvalue())
                self.assertEqual(result['head_revision'], 0); self.assertEqual(result['target_count'], 0)
                self.assertEqual(result['upload_schema'], 1); self.assertFalse(result['question_ready'])
                self.assertEqual(result['production_api_requests'], 0); self.assertFalse(result['GPU_requested'])
                self.assertNotIn('pins', result); self.assertNotIn('body', result)

    def test_CLI_corrupt_state_emits_bounded_error_without_data(self):
        QuestionStore.migrate(self.uploads)
        with self.uploads.connect(write=True) as db: db.execute("UPDATE question_head SET library_id='private-secret-value'")
        result = self.cli_process('status')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['code'], 'QUESTION_HEAD_INVALID')
        self.assertNotIn('private-secret-value', result.stdout + result.stderr)


class StorageTests(Fixture):
    def setUp(self):
        super().setUp(); self.store = QuestionStore.migrate(self.uploads)

    def test_save_reopen_and_repeated_body_preserve_one_immutable_target(self):
        original = copy.deepcopy(self.body); target = self.store.save_target(self.body)
        self.body['pins']['chat_profile_id'] = 'changed'
        self.assertEqual(self.store.target(target['target_id'])['body'], original)
        reopened = QuestionStore(UploadStore(self.root))
        self.assertEqual(reopened.save_target(original), target)
        self.assertEqual(reopened.status()['target_count'], 1)
        self.assertEqual(reopened.head()['revision'], 0); self.assertIsNone(reopened.head()['target'])

    def test_later_target_keeps_old_head_history_and_version_pins(self):
        first = self.future_head_fixture(self.store)
        second_body = copy.deepcopy(self.body); second_body['revision'] = 2
        second_body['indexing_generation'] = 8; second_body['index_version_id'] = str(uuid4())
        second_body['pins']['chat_deployment_id'] = str(uuid4())
        second = self.store.save_target(second_body)
        self.assertNotEqual(first['target_id'], second['target_id'])
        self.assertEqual(self.store.head()['target'], first)
        self.assertEqual(self.store.target(first['target_id']), first)
        self.assertEqual(self.store.target(second['target_id'])['body'], second_body)
        self.assertFalse(self.store.status()['question_ready'])

    def test_foreign_library_save_is_refused_before_write(self):
        before = self.snapshot(extension=True); body = copy.deepcopy(self.body); body['library_id'] = str(uuid4())
        with self.assertRaisesRegex(UploadError, 'QUESTION_LIBRARY_MISMATCH'): self.store.save_target(body)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_invalid_candidate_does_not_change_active_head(self):
        self.future_head_fixture(self.store); before = self.snapshot(extension=True)
        body = copy.deepcopy(self.body); body['pins']['agent_definition_sha256'] = 'invalid'
        with self.assertRaises(UploadError): self.store.save_target(body)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_SQL_update_and_delete_of_frozen_target_are_rejected(self):
        target = self.store.save_target(self.body); before = self.snapshot(extension=True)
        for statement in ('UPDATE question_targets SET body=body', 'DELETE FROM question_targets'):
            with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.IntegrityError, 'QUESTION_TARGET_IMMUTABLE'):
                with self.uploads.connect(write=True) as db: db.execute(statement)
        self.assertEqual(before, self.snapshot(extension=True))
        self.assertEqual(self.store.target(target['target_id']), target)

    def test_foreign_library_insert_is_rejected_by_SQL_guard(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'QUESTION_LIBRARY_MISMATCH'):
            with self.uploads.connect(write=True) as db:
                db.execute('INSERT INTO question_targets VALUES(?,?,?,?,?)',
                           (str(uuid4()), str(uuid4()), '{}', 'a' * 64, 'test'))
        self.assertEqual(self.store.status()['target_count'], 0)

    def test_unknown_or_malformed_target_id_has_no_side_effects(self):
        before = self.snapshot(extension=True)
        for identifier, code in (('wrong', 'QUESTION_TARGET_ID_INVALID'), (str(uuid4()), 'QUESTION_TARGET_NOT_FOUND')):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(UploadError, code): self.store.target(identifier)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_error_after_target_insert_rolls_back_receipt_and_head(self):
        before = self.snapshot(extension=True)
        with patch.object(storage, 'stored_target', side_effect=OSError('injected write/readback failure')):
            with self.assertRaises(OSError): self.store.save_target(self.body)
        self.assertEqual(before, self.snapshot(extension=True))
        self.assertEqual(self.store.save_target(self.body)['body'], self.body)

    def test_corrupted_stored_hash_is_detected_without_repair(self):
        self.store.save_target(self.body)
        with self.uploads.connect(write=True) as db:
            db.execute('DROP TRIGGER question_target_no_update')
            db.execute("UPDATE question_targets SET body_sha256=?", ('0' * 64,))
            db.execute(schema.OBJECTS['question_target_no_update'])
        before = self.snapshot(extension=True)
        with self.assertRaisesRegex(UploadError, 'QUESTION_TARGET_CORRUPT'): QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_bounded_history_keeps_idempotence_at_capacity(self):
        first = self.store.save_target(self.body)
        with self.uploads.connect(write=True) as db:
            for generation in range(2, schema.MAX_TARGETS + 1):
                body = copy.deepcopy(self.body); body['indexing_generation'] = generation + 100
                normalized = checked_target(body)
                db.execute('INSERT INTO question_targets VALUES(?,?,?,?,?)',
                           (target_id(normalized), normalized['library_id'], encoded_target(normalized).decode(),
                            fingerprint(normalized), 'fixture'))
        self.assertEqual(self.store.status()['target_count'], schema.MAX_TARGETS)
        self.assertEqual(self.store.save_target(self.body), first)
        body = copy.deepcopy(self.body); body['indexing_generation'] = 999
        with self.assertRaisesRegex(UploadError, 'QUESTION_TARGET_LIMIT_EXCEEDED'): self.store.save_target(body)
        self.assertEqual(self.store.head()['revision'], 0)

    def test_migration_and_target_save_share_exclusive_lane(self):
        before = self.snapshot(extension=True)
        with file_lock(self.root / 'question-schema.lock'):
            with self.assertRaisesRegex(UploadError, 'QUESTION_STORE_BUSY'): self.store.save_target(self.body)
            with self.assertRaisesRegex(UploadError, 'QUESTION_STORE_BUSY'): QuestionStore.migrate(self.uploads)
        self.assertEqual(before, self.snapshot(extension=True))

    def test_legacy_upload_dispatch_and_old_initializers_keep_new_state(self):
        first = self.future_head_fixture(self.store); before_head = self.store.head()
        uploads = UploadStore.initialize(self.root)
        for initializer in (self.entry.initialize, RefinementStore.initialize, self.cpu.initialize,
                            IndexingStore.initialize, self.flow.initialize, SearchToolStore.initialize): initializer(uploads)
        job = uploads.accept(str(uuid4()), 'new legacy upload', 'new.txt', b'new legacy document', self.uploaded.origin)
        self.assertEqual(uploads.dispatch(job['job_id'], self.uploaded)['state'], 'preparation_pending')
        self.assertEqual(self.store.head(), before_head)
        self.assertEqual(self.store.target(first['target_id']), first)
        self.assertEqual(uploads.library()['question_revision'], 0)
        self.assertFalse(uploads.library()['question_ready'])

    def test_legacy_HTTP_library_remains_safe_after_future_head_fixture(self):
        self.future_head_fixture(self.store)
        server = make_server(self.uploads, self.uploaded, port=0)
        with running(server) as port:
            client = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                client.request('GET', '/api/library', headers={'Authorization': 'Bearer ' + self.uploads.token_path.read_text().strip()})
                response = client.getresponse(); body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(body['question_revision'], 0); self.assertFalse(body['question_ready'])
            finally: client.close()
        self.assertEqual(self.store.head()['revision'], 1)

    def test_symlink_lock_is_refused_without_touching_outside_file(self):
        outside = self.root.parent / 'outside'; outside.write_bytes(b'outside-private-content'); outside.chmod(0o600)
        lock = self.root / 'question-schema.lock'; lock.unlink(); lock.symlink_to(outside)
        before = self.snapshot(extension=True)
        with self.assertRaises(OSError): self.store.save_target(self.body)
        self.assertEqual(outside.read_bytes(), b'outside-private-content')
        self.assertEqual(before, self.snapshot(extension=True))


if __name__ == '__main__': unittest.main()
