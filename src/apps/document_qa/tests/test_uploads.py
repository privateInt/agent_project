"""Q03a uses temporary own DBs and loopback HTTP only; no GPU/platform calls."""
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from src.apps.document_qa.api import make_server
from src.apps.document_qa.contracts import MAX_BYTES
from src.apps.document_qa.intake import UploadError, metadata, multipart, encode_multipart
from src.apps.document_qa.uploads import DataUploader, UploadStore

ORIGIN = 'http://127.0.0.1:12345'
DATA = '문서 질의응답 안내\n테스트 자료입니다.\n'.encode()


class FakeData:
    origin = ORIGIN

    def __init__(self, error=False, changes=None):
        self.calls, self.error, self.changes = [], error, changes or {}
        self.source_id = str(uuid4())

    def upload(self, meta, data, key):
        self.calls.append((meta.copy(), data, key))
        if self.error:
            raise UploadError('DATA_UPLOAD_OUTCOME_UNKNOWN', 502)
        return 200, json.dumps({'document_id': self.source_id, 'state': 'ready', 'source_type': 'upload',
                                'name': meta['name'], 'original_filename': meta['filename'],
                                'sha256': meta['sha256'], 'size_bytes': meta['size_bytes'],
                                **self.changes}).encode()


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


class IntakeTests(unittest.TestCase):
    def test_utf8_name_and_filename_binary_pdf_roundtrip(self):
        data = b'%PDF-1.7\n\xff\x00binary\r\n'
        meta = metadata('  한국어 이름  ', '한국어 "파일".pdf', data)
        content_type, raw = encode_multipart(meta, data, str(uuid4()))
        got, original = multipart(content_type, raw)
        self.assertEqual((got, original), (meta, data))

    def test_extension_does_not_mask_binary_or_bad_utf8(self):
        for filename, data in [('x.txt', b'\xff'), ('x.md', b'%PDF-1.7\n'), ('x.pdf', DATA),
                               ('x.txt', b'PK\x03\x04'), ('x.txt', b'a\x00b')]:
            with self.subTest(filename=filename, data=data), self.assertRaises(UploadError):
                metadata('자료', filename, data)

    def test_unsupported_metadata_and_path_are_rejected(self):
        for name, filename in [('자료', '../x.txt'), ('자료', 'x:1.txt'), ('자료', ' x.txt'),
                               ('자료', 'x.csv'), ('a\nb', 'x.txt'), ('a' * 201, 'x.txt'),
                               ('자료', '가' * 86 + '.txt')]:
            with self.subTest(name=name, filename=filename), self.assertRaises(UploadError):
                metadata(name, filename, DATA)

    def test_nonempty_and_max_file_boundary(self):
        self.assertEqual(metadata('large', 'x.txt', b'a' * MAX_BYTES)['size_bytes'], MAX_BYTES)
        for data in (b'', b'a' * (MAX_BYTES + 1)):
            with self.assertRaises(UploadError):
                metadata('large', 'x.txt', data)

    def test_duplicate_field_unknown_field_missing_end_and_encoding_rejected(self):
        boundary = 'test-boundary'
        prefix = b'--test-boundary\r\nContent-Disposition: form-data; name="name"\r\n\r\nname\r\n'
        file = b'--test-boundary\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\nx\r\n'
        good = prefix + file + b'--test-boundary--\r\n'
        content_type = 'multipart/form-data; boundary=' + boundary
        self.assertEqual(multipart(content_type, good)[1], b'x')
        for raw in (prefix + good, good.replace(b'name="name"', b'name="other"'), good[:-6],
                    good.replace(b'filename="x.txt"', b'filename="x.txt"; filename="y.txt"'),
                    good.replace(b'\r\n\r\nx', b'\r\nContent-Transfer-Encoding: base64\r\n\r\neA=='),
                    good + b'trailing non-multipart data'):
            with self.subTest(raw=raw), self.assertRaises(UploadError):
                multipart(content_type, raw)

    def test_deterministic_boundary_collision_keeps_original_bytes(self):
        key = str(uuid4())
        data = b'first\r\n--document-qa-' + key.encode() + b'\r\nlast'
        meta = metadata('collision', 'x.txt', data)
        content_type, raw = encode_multipart(meta, data, key)
        self.assertEqual(encode_multipart(meta, data, key), (content_type, raw))
        self.assertEqual(multipart(content_type, raw), (meta, data))

    def test_filename_parameter_looking_text_is_not_an_extra_field(self):
        meta = metadata('자료', '세미콜론; name="문자".txt', DATA)
        content_type, raw = encode_multipart(meta, DATA, str(uuid4()))
        self.assertEqual(multipart(content_type, raw), (meta, DATA))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qa-upload-tests-')
        self.root = Path(self.temp.name) / 'uploads'
        self.store = UploadStore.initialize(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def accept(self, key=None, name='자료', filename='x.txt', data=DATA):
        return self.store.accept(key or str(uuid4()), name, filename, data, ORIGIN)

    def test_init_idempotence_private_files_and_no_question_head(self):
        before = self.store.library()
        token = self.store.token_path.read_bytes()
        again = UploadStore.initialize(self.root, 'renaming is not init')
        self.assertEqual(again.library(), before)
        self.assertEqual(self.store.token_path.read_bytes(), token)
        self.assertEqual(before['question_revision'], 0)
        self.assertFalse(before['question_ready'])
        for path in (self.root, self.store.token_path, self.store.db_path, self.root / 'locks'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_status_does_not_initialize_absent_store(self):
        missing = Path(self.temp.name) / 'missing'
        with self.assertRaises(UploadError) as context:
            UploadStore(missing)
        self.assertEqual(context.exception.code, 'STORE_NOT_INITIALIZED')
        self.assertFalse(missing.exists())

    def test_nested_init_preserves_private_parent_for_later_q02_settings(self):
        parent = Path(self.temp.name) / 'qa-state'
        store = UploadStore.initialize(parent / 'uploads')
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(store.root.stat().st_mode & 0o777, 0o700)

    def test_unknown_retry_job_does_not_create_lock_file(self):
        with self.assertRaises(UploadError) as context:
            self.store.dispatch(str(uuid4()), FakeData(), retry=True)
        self.assertEqual(context.exception.code, 'JOB_NOT_FOUND')
        self.assertEqual(list((self.root / 'locks').iterdir()), [])

    def test_receipt_only_commits_verified_data_id_and_releases_original(self):
        job = self.accept()
        upstream = FakeData()
        result = self.store.dispatch(job['job_id'], upstream)
        self.assertEqual(result['state'], 'preparation_pending')
        self.assertEqual(result['source_document_id'], upstream.source_id)
        self.assertEqual(self.store.library()['pending_original_bytes'], 0)
        self.assertFalse(result['question_ready'])
        self.assertFalse(result['workflow_started'])
        self.store.dispatch(job['job_id'], upstream, retry=True)
        self.assertEqual(len(upstream.calls), 1)
        self.assertEqual(self.store.library()['question_revision'], 0)

    def test_same_request_reuses_job_after_reopen(self):
        key = str(uuid4())
        job = self.accept(key)
        reopened = UploadStore(self.root)
        self.assertEqual(reopened.by_request(key), job)
        self.assertEqual(self.accept(key), job)
        self.assertEqual(len(reopened.jobs()), 1)

    def test_status_projection_never_contains_original_origin_keys_or_token(self):
        job = self.accept()
        public = json.dumps({'library': self.store.library(), 'jobs': self.store.jobs(), 'job': self.store.job(job['job_id'])})
        for private in (DATA.decode(), ORIGIN, self.store.token_path.read_text().strip(), 'upload_key', 'original"'):
            self.assertNotIn(private, public)

    def test_reused_client_key_with_changed_file_conflicts_without_mutation(self):
        key = str(uuid4())
        self.accept(key)
        before = self.store.library()
        for kwargs in ({'data': DATA + b'x'}, {'filename': 'new.txt'}, {'name': '다른 자료'}):
            with self.assertRaises(UploadError) as context:
                self.accept(key, **kwargs)
            self.assertEqual(context.exception.code, 'REQUEST_KEY_CONFLICT')
        self.assertEqual(self.store.library(), before)

    def test_same_semantic_file_new_key_is_one_member_alias_is_durable(self):
        first = self.accept()
        key = str(uuid4())
        self.assertEqual(self.accept(key)['job_id'], first['job_id'])
        self.assertEqual(self.store.by_request(key)['job_id'], first['job_id'])
        self.assertEqual(self.store.library()['reserved_documents'], 1)
        self.assertNotEqual(self.accept(name='다른 이름')['job_id'], first['job_id'])

    def test_concurrent_reservations_never_cross_twenty(self):
        def attempt(i):
            try:
                return self.accept(name=f'document-{i}')['sequence']
            except UploadError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(30)))
        self.assertEqual(sorted(x for x in results if isinstance(x, int)), list(range(1, 21)))
        self.assertEqual(results.count('DOCUMENT_CAPACITY_EXCEEDED'), 10)
        # Existing receipt replays are still allowed when the library is full.
        first = self.store.jobs()[0]
        self.assertEqual(self.accept(name=first['name'])['job_id'], first['job_id'])

    def test_concurrent_duplicate_keys_and_files_are_one_logical_job(self):
        key = str(uuid4())
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda _: self.accept(key), range(10)))
        self.assertEqual(len({row['job_id'] for row in rows}), 1)
        self.assertEqual(self.store.library()['accepted_sequence'], 1)

    def test_unknown_outcome_is_not_automatically_replayed(self):
        key, upstream = str(uuid4()), FakeData(error=True)
        job = self.accept(key)
        result = self.store.dispatch(job['job_id'], upstream)
        self.assertEqual(result['state'], 'upload_confirmation_required')
        self.assertEqual(self.store.library()['pending_original_bytes'], len(DATA))
        self.store.dispatch(self.accept(key)['job_id'], upstream)
        self.assertEqual(len(upstream.calls), 1)
        upstream.error = False
        self.store.dispatch(job['job_id'], upstream, retry=True)
        self.assertEqual(upstream.calls[0], upstream.calls[1])
        self.assertEqual(self.store.library()['pending_original_bytes'], 0)

    def test_process_death_preserves_intent_and_releases_job_lock(self):
        job = self.accept()
        script = '''
import os,sys
from src.apps.document_qa.uploads import UploadStore
class Crash:
 origin = "http://127.0.0.1:12345"
 def upload(self,meta,data,key):
  os._exit(7)
UploadStore(sys.argv[1]).dispatch(sys.argv[2],Crash())
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root), job['job_id']], capture_output=True)
        self.assertEqual(result.returncode, 7)
        fresh = UploadStore(self.root)
        self.assertEqual(fresh.job(job['job_id'])['attempts'], 1)
        self.assertEqual(fresh.job(job['job_id'])['state'], 'upload_confirmation_required')
        upstream = FakeData()
        self.assertEqual(fresh.dispatch(job['job_id'], upstream, retry=True)['state'], 'preparation_pending')
        self.assertEqual(upstream.calls[0][1], DATA)

    def test_same_job_is_serialized_during_upstream_call(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeData):
            def upload(inner, *args):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test gate failed')
                return super(Slow, inner).upload(*args)
        upstream, job = Slow(), self.accept()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.store.dispatch, job['job_id'], upstream)
            self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(UploadError) as context:
                    self.store.dispatch(job['job_id'], upstream, retry=True)
                self.assertEqual(context.exception.code, 'UPLOAD_BUSY')
                # Another document can be persisted while the first is in HTTP I/O.
                self.accept(name='second')
            finally:
                release.set()
            self.assertEqual(pending.result()['state'], 'preparation_pending')
        self.assertEqual(len(upstream.calls), 1)

    def test_changed_data_origin_cannot_replay_in_another_platform(self):
        job, upstream = self.accept(), FakeData()
        upstream.origin = 'http://127.0.0.1:9999'
        with self.assertRaises(UploadError) as context:
            self.store.dispatch(job['job_id'], upstream)
        self.assertEqual(context.exception.code, 'UPLOAD_TARGET_CHANGED')
        self.assertEqual(upstream.calls, [])
        self.assertEqual(self.store.job(job['job_id'])['attempts'], 0)

    def test_invalid_receipt_does_not_discard_original_or_release_capacity(self):
        for change in ({'sha256': '0' * 64}, {'size_bytes': 1}, {'original_filename': 'other.txt'},
                       {'source_type': 'crawl'}, {'state': 'pending'}, {'document_id': 'not-a-uuid'},
                       {'deleted_at': '2026-10-07T00:00:00Z'}):
            with self.subTest(change=change):
                job = self.accept(name=str(change))
                result = self.store.dispatch(job['job_id'], FakeData(changes=change))
                self.assertEqual(result['state'], 'upload_confirmation_required')
                self.assertIsNone(result['source_document_id'])
        self.assertEqual(self.store.library()['reserved_documents'], 7)
        self.assertEqual(self.store.library()['pending_original_bytes'], 7 * len(DATA))

    def test_data_async_receipt_is_not_an_upload_receipt(self):
        class Async(FakeData):
            def upload(self, *args):
                return 202, b'{"job_id":"not-a-source"}'
        result = self.store.dispatch(self.accept()['job_id'], Async())
        self.assertEqual(result['error_code'], 'UPLOAD_RECEIPT_INVALID')
        self.assertIsNone(result['source_document_id'])

    def test_tampered_original_is_not_sent(self):
        job, upstream = self.accept(), FakeData()
        with self.store.connect(write=True) as db:
            db.execute('UPDATE jobs SET original=? WHERE job_id=?', (DATA + b'changed', job['job_id']))
        with self.assertRaises(UploadError) as context:
            self.store.dispatch(job['job_id'], upstream)
        self.assertEqual(context.exception.code, 'STORED_UPLOAD_CHANGED')
        self.assertEqual(upstream.calls, [])

    def test_max_upload_reclaims_sqlite_pages_after_data_ack(self):
        original = b'a' * MAX_BYTES
        job = self.accept(data=original)
        allocated = self.store.db_path.stat().st_size
        self.assertGreaterEqual(allocated, MAX_BYTES)
        self.store.dispatch(job['job_id'], FakeData())
        self.assertEqual(self.store.library()['pending_original_bytes'], 0)
        self.assertLess(self.store.db_path.stat().st_size, 128 << 10)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_database_hardlink_or_symlink_is_refused(self):
        external = Path(self.temp.name) / 'external-db'
        os.link(self.store.db_path, external)
        with self.assertRaises(UploadError):
            UploadStore(self.root)
        external.unlink()
        self.store.db_path.rename(external)
        self.store.db_path.symlink_to(external)
        with self.assertRaises(UploadError):
            UploadStore(self.root)

    def test_incomplete_init_refuses_to_overwrite_staged_database(self):
        root = Path(self.temp.name) / 'incomplete'
        root.mkdir(mode=0o700)
        stage = root / 'init.sqlite3'; stage.write_bytes(b'evidence')
        with self.assertRaises(UploadError) as context:
            UploadStore.initialize(root)
        self.assertEqual(context.exception.code, 'STORE_INIT_INCOMPLETE')
        self.assertEqual(stage.read_bytes(), b'evidence')
        self.assertFalse((root / 'uploads.sqlite3').exists())

    def test_alias_and_manual_attempt_limits_bound_retry_history(self):
        job = self.accept()
        for _ in range(99):
            self.accept()
        with self.assertRaises(UploadError) as context:
            self.accept()
        self.assertEqual(context.exception.code, 'REQUEST_ALIAS_LIMIT')
        with self.store.connect(write=True) as db:
            db.execute('UPDATE jobs SET attempts=100 WHERE job_id=?', (job['job_id'],))
        upstream = FakeData()
        with self.assertRaises(UploadError) as context:
            self.store.dispatch(job['job_id'], upstream, retry=True)
        self.assertEqual(context.exception.code, 'UPLOAD_REVIEW_REQUIRED')
        self.assertEqual(upstream.calls, [])


class ApiTests(StoreTests):
    # This class uses the store fixture, not its tests (see load_tests below).
    def request(self, port, method, path, data=None, headers=None, authenticated=True):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
        values = {'Authorization': 'Bearer ' + self.store.token_path.read_text().strip()} if authenticated else {}
        values.update(headers or {})
        try:
            conn.request(method, path, body=data, headers=values)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def upload_request(self, port, key=None, data=DATA):
        meta = metadata('자료', 'x.txt', data)
        content_type, raw = encode_multipart(meta, data, str(uuid4()))
        return self.request(port, 'POST', '/api/uploads', raw,
                            {'Content-Type': content_type, 'Idempotency-Key': key or str(uuid4())})

    def test_api_upload_lookup_and_readiness_reuse(self):
        upstream, key = FakeData(), str(uuid4())
        with running(make_server(self.store, upstream, lambda: {'defaults_ready': False, 'question_ready': False}, 0)) as port:
            status, job = self.upload_request(port, key)
            self.assertEqual(status, 202)
            self.assertEqual(job['state'], 'preparation_pending')
            self.assertEqual(self.request(port, 'GET', job['status_url']), (200, job))
            self.assertEqual(self.request(port, 'GET', '/api/upload-requests/' + key), (200, job))
            self.assertFalse(self.request(port, 'GET', '/api/readiness')[1]['defaults_ready'])
            self.assertEqual(self.upload_request(port, key), (202, job))
        self.assertEqual(len(upstream.calls), 1)

    def test_authentication_blocks_upload_and_private_status(self):
        upstream = FakeData()
        with running(make_server(self.store, upstream, port=0)) as port:
            for method, path in [('GET', '/api/library'), ('GET', '/api/jobs'), ('POST', '/api/uploads')]:
                status, body = self.request(port, method, path, authenticated=False)
                self.assertEqual((status, body['code']), (401, 'AUTH_REQUIRED'))
        self.assertEqual(upstream.calls, [])
        self.assertEqual(self.store.library()['reserved_documents'], 0)

    def test_malformed_whole_body_cannot_commit_a_job(self):
        meta = metadata('자료', 'x.txt', DATA)
        content_type, raw = encode_multipart(meta, DATA, str(uuid4()))
        with running(make_server(self.store, FakeData(), port=0)) as port:
            for headers, body in [({'Content-Type': content_type}, raw),
                                  ({'Content-Type': content_type, 'Idempotency-Key': str(uuid4())}, raw[:-7]),
                                  ({'Content-Type': content_type, 'Idempotency-Key': str(uuid4()), 'Content-Encoding': 'gzip'}, raw)]:
                self.assertEqual(self.request(port, 'POST', '/api/uploads', body, headers)[0], 400)
        self.assertEqual(self.store.library()['reserved_documents'], 0)

    def test_ambiguous_upload_returns_durable_job_and_explicit_retry_uses_same_key(self):
        upstream, key = FakeData(error=True), str(uuid4())
        with running(make_server(self.store, upstream, port=0)) as port:
            status, job = self.upload_request(port, key)
            self.assertEqual((status, job['state']), (202, 'upload_confirmation_required'))
            self.upload_request(port, key)
            self.assertEqual(len(upstream.calls), 1)
            upstream.error = False
            status, receipt = self.request(port, 'POST', job['status_url'] + '/retry-upload', b'{}', {'Content-Type': 'application/json'})
            self.assertEqual((status, receipt['state']), (202, 'preparation_pending'))
        self.assertEqual(upstream.calls[0], upstream.calls[1])

    def test_changed_file_same_client_key_returns_conflict(self):
        key = str(uuid4())
        with running(make_server(self.store, FakeData(), port=0)) as port:
            self.upload_request(port, key)
            status, body = self.upload_request(port, key, DATA + b'new')
            self.assertEqual((status, body['code']), (409, 'REQUEST_KEY_CONFLICT'))

    def test_retry_cannot_change_payload_or_call_unknown_path(self):
        job = self.accept()
        with running(make_server(self.store, FakeData(), port=0)) as port:
            self.assertEqual(self.request(port, 'POST', job['status_url'] + '/retry-upload',
                                          b'{"new_key":"other"}', {'Content-Type': 'application/json'})[0], 400)
            self.assertEqual(self.request(port, 'GET', '/api/jobs/' + str(uuid4()))[0], 404)
            self.assertEqual(self.request(port, 'GET', '/api/library?token=secret')[0], 400)
            self.assertEqual(self.request(port, 'POST', '/api/model/start', b'{}')[0], 404)

    def test_page_disconnect_does_not_cancel_accepted_upload(self):
        import socket
        done = threading.Event()
        class Observed(FakeData):
            def upload(inner, *args):
                value = super(Observed, inner).upload(*args)
                done.set()
                return value
        key = str(uuid4())
        content_type, raw = encode_multipart(metadata('자료', 'x.txt', DATA), DATA, str(uuid4()))
        with running(make_server(self.store, Observed(), port=0)) as port:
            with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
                headers = (f'POST /api/uploads HTTP/1.0\r\nContent-Type: {content_type}\r\nContent-Length: {len(raw)}\r\n'
                           f'Idempotency-Key: {key}\r\nAuthorization: Bearer {self.store.token_path.read_text().strip()}\r\n\r\n')
                sock.sendall(headers.encode() + raw)
            self.assertTrue(done.wait(3))
        self.assertEqual(self.store.by_request(key)['state'], 'preparation_pending')


class TransportTests(unittest.TestCase):
    @contextmanager
    def upstream(self, response='valid'):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                meta, data = multipart(self.headers['Content-Type'], raw)
                seen.append((self.path, self.headers.get('Idempotency-Key'), meta, data, self.headers.get('Authorization')))
                if response == 'redirect':
                    self.send_response(307); self.send_header('Location', '/must-not-replay'); self.end_headers(); return
                status, body = FakeData().upload(meta, data, self.headers['Idempotency-Key'])
                if response == 'oversize':
                    body = b' ' * ((64 << 10) + 1)
                if response == 'auth':
                    status, body = 403, b'private upstream error and secret'
                self.send_response(status); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)
        with running(ThreadingHTTPServer(('127.0.0.1', 0), Handler)) as port:
            yield DataUploader({'url': f'http://127.0.0.1:{port}'}), seen

    def test_real_http_multipart_preserves_file_and_idempotency_key(self):
        key, meta = str(uuid4()), metadata('HTTP 자료', 'http.md', DATA)
        with self.upstream() as (uploader, seen):
            status, raw = uploader.upload(meta, DATA, key)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['sha256'], hashlib.sha256(DATA).hexdigest())
        self.assertEqual(seen, [('/api/source-documents', key, meta, DATA, None)])

    def test_redirect_is_never_followed_or_replayed(self):
        with self.upstream('redirect') as (uploader, seen):
            with self.assertRaises(UploadError) as context:
                uploader.upload(metadata('자료', 'x.txt', DATA), DATA, str(uuid4()))
        self.assertEqual(context.exception.code, 'DATA_REDIRECT_REFUSED')
        self.assertEqual(len(seen), 1)

    def test_large_receipt_is_bounded(self):
        with self.upstream('oversize') as (uploader, seen):
            with self.assertRaises(UploadError) as context:
                uploader.upload(metadata('자료', 'x.txt', DATA), DATA, str(uuid4()))
        self.assertEqual(context.exception.code, 'UPLOAD_RECEIPT_TOO_LARGE')

    def test_upstream_error_body_is_not_exposed(self):
        with self.upstream('auth') as (uploader, seen):
            with self.assertRaises(UploadError) as context:
                uploader.upload(metadata('자료', 'x.txt', DATA), DATA, str(uuid4()))
        self.assertEqual(str(context.exception), 'DATA_AUTH_REQUIRED')
        self.assertNotIn('secret', str(context.exception))

    def test_existing_data_bearer_reference_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp, self.upstream() as (uploader, seen):
            token = Path(temp) / 'external-key'; token.write_text('existing-token\n'); token.chmod(0o444)
            uploader.token_file = str(token)
            uploader.upload(metadata('자료', 'x.txt', DATA), DATA, str(uuid4()))
            self.assertEqual(token.read_text(), 'existing-token\n')
            self.assertEqual(token.stat().st_mode & 0o777, 0o444)
            self.assertEqual(seen[0][-1], 'Bearer existing-token')


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (IntakeTests, StoreTests, ApiTests, TransportTests):
        # Avoid rerunning inherited StoreTests in ApiTests.
        for name in sorted(n for n in cls.__dict__ if n.startswith('test_')):
            suite.addTest(cls(name))
    return suite
