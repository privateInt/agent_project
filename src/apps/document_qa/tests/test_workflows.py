"""Q03b own SQLite + Node Code-node evaluation + loopback HTTP, no real n8n/GPU."""
import base64
import copy
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from src.apps.document_qa.api import make_server
from src.apps.document_qa.contracts import fingerprint
from src.apps.document_qa.intake import UploadError, metadata, encode_multipart
from src.apps.document_qa.uploads import UploadStore
from src.apps.document_qa.workflows import WorkflowStore, EngineHTTP, VERSION, signed_headers
from src.apps.document_qa.workflow_template import build, matches

ORIGIN = 'http://127.0.0.1:16200'
DATA_ORIGIN = 'http://127.0.0.1:12345'


class DataFixture:
    origin = DATA_ORIGIN
    def upload(self, meta, data, key):
        return 200, json.dumps({'document_id': str(uuid4()), 'name': meta['name'], 'original_filename': meta['filename'],
                               'sha256': meta['sha256'], 'size_bytes': meta['size_bytes'], 'state': 'ready', 'source_type': 'upload'}).encode()


class FakeEngine:
    origin = ORIGIN
    def __init__(self, workflow):
        self.workflow, self.calls, self.resources = workflow, [], {'credentials': {}, 'workflows': {}}
        self.lost_create, self.lost_reply, self.no_callback, self.generic_reply = None, False, False, False
        self.version = VERSION
        self.callback = None
        self.entry = None

    def items(self, collection):
        return list(copy.deepcopy(self.resources[collection.rsplit('/', 1)[-1]]).values())

    def request(self, method, route, body=None, headers=None):
        self.calls.append((method, route, copy.deepcopy(body), headers))
        if route == '/rest/settings':
            return {'data': {'versionCli': self.version}}
        if route.startswith('/webhook/'):
            if self.no_callback:
                raise UploadError('ENGINE_OUTCOME_UNKNOWN', 502)
            if self.generic_reply:
                return {'message': 'Workflow got started'}
            setup = self.workflow.setup()
            authorization = headers['Authorization'].split(' ', 1)[1]
            header, claims, signature = authorization.split('.')
            expected = base64.urlsafe_b64encode(hmac.new(self.workflow.hook_path.read_text().strip().encode(),
                                                       (header + '.' + claims).encode(), hashlib.sha256).digest()).rstrip(b'=').decode()
            assert hmac.compare_digest(expected, signature)
            jwt = json.loads(base64.urlsafe_b64decode(claims + '=' * (-len(claims) % 4)))
            assert jwt['request_sha256'] == fingerprint(body)
            value = {**body, 'workflow_id': setup['resources']['workflow']['id'],
                     'execution_id': 'execution_' + str(len([x for x in self.calls if x[1].startswith('/webhook/')])),
                     'definition_sha256': setup['definition_sha256']}
            if self.entry:
                self.entry.set()
                assert self.release.wait(3)
            receipt = self.callback(value) if self.callback else self.workflow.accept_receipt(value)
            if self.lost_reply:
                raise UploadError('ENGINE_OUTCOME_UNKNOWN', 502)
            return receipt
        parts = route.split('/')
        collection = parts[3]
        if method == 'POST' and len(parts) == 4:
            identifier = collection + '_' + str(len(self.resources[collection]) + 1)
            value = {'id': identifier, **copy.deepcopy(body)}
            if collection == 'workflows':
                value.update(versionId=str(uuid4()), active=False, activeVersionId=None)
            self.resources[collection][identifier] = value
            if self.lost_create == collection:
                self.lost_create = None
                raise UploadError('ENGINE_OUTCOME_UNKNOWN', 502)
            return {'id': identifier}
        value = self.resources[collection][parts[4]]
        if method == 'POST' and parts[-1] == 'publish':
            assert body == {'versionId': value['versionId']}
            value.update(active=True, activeVersionId=value['versionId'])
        return copy.deepcopy(value)


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qa-workflow-tests-')
        self.root = Path(self.temp.name) / 'uploads'
        self.uploads = UploadStore.initialize(self.root)
        self.workflow = WorkflowStore.initialize(self.uploads)
        self.engine = FakeEngine(self.workflow)
        self.key_path = Path(self.temp.name) / 'engine-key.json'
        self.key_path.write_text(json.dumps({'url': ORIGIN, 'api_key': 'test-only-api-key'})); self.key_path.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, qa_origin='http://127.0.0.1:17100'):
        return self.workflow.prepare(qa_origin, self.key_path, self.engine)

    def job(self, name='자료'):
        job = self.uploads.accept(str(uuid4()), name, 'x.txt', b'GPU-free fixture source', DATA_ORIGIN)
        return self.uploads.dispatch(job['job_id'], DataFixture())

    def posts(self, prefix='/api/v1/'):
        return [c for c in self.engine.calls if c[0] == 'POST' and c[1].startswith(prefix)]


class StorageTests(Fixture):
    def test_schema_migration_preserves_library_jobs_request_keys_and_original(self):
        job = self.uploads.accept(str(uuid4()), '기존 업로드', 'x.txt', b'original', DATA_ORIGIN)
        before, token = self.uploads.library(), self.uploads.token_path.read_bytes()
        WorkflowStore.initialize(UploadStore(self.root))
        self.assertEqual(UploadStore(self.root).library(), before)
        self.assertEqual(self.uploads.token_path.read_bytes(), token)
        self.assertEqual(self.uploads.job(job['job_id']), job)
        with self.uploads.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT version FROM workflow_metadata').fetchone()[0], 1)
        self.assertEqual(self.uploads.dispatch(job['job_id'], DataFixture())['state'], 'preparation_pending')

    def test_plaintext_public_callback_address_refused_before_registration(self):
        with self.assertRaises(UploadError):
            self.prepare('http://8.8.8.8:17100')
        self.assertEqual(self.engine.calls, [])

    def test_initialization_and_status_do_not_call_engine_or_configure_it(self):
        keys = [p.read_bytes() for p in (self.workflow.token_path, self.workflow.hook_path)]
        again = WorkflowStore.initialize(self.uploads)
        self.assertFalse(again.status()['workflow_configured'])
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(keys, [p.read_bytes() for p in (again.token_path, again.hook_path)])

    def test_prepare_twice_reuses_credentials_flow_and_published_version(self):
        self.prepare(); before = copy.deepcopy(self.workflow.setup())
        posts = len(self.posts()); self.prepare()
        self.assertEqual(self.workflow.setup(), before)
        self.assertEqual(len(self.posts()), posts)
        self.assertEqual(len(self.engine.resources['credentials']), 2)
        self.assertEqual(len(self.engine.resources['workflows']), 1)
        self.assertTrue(self.workflow.status()['workflow_configured'])
        self.assertFalse(self.workflow.status()['configuration_live_verified'])

    def test_engine_version_change_after_prepare_blocks_new_delivery(self):
        self.prepare(); job = self.job(); self.engine.version = 'unverified-version'
        with self.assertRaises(UploadError):
            self.workflow.dispatch(job['job_id'], self.engine)
        self.assertEqual(self.posts('/webhook/'), [])
        self.assertEqual(self.workflow.handoff(job['job_id'])['attempts'], 0)

    def test_wrong_engine_version_does_not_create_remote_resources(self):
        self.engine.version = 'unverified-version'
        with self.assertRaises(UploadError) as context:
            self.prepare()
        self.assertEqual(context.exception.code, 'ENGINE_VERSION_UNVERIFIED')
        self.assertEqual(self.posts(), [])

    def test_lost_credential_create_is_reconciled_without_a_second_create(self):
        self.engine.lost_create = 'credentials'
        with self.assertRaises(UploadError):
            self.prepare()
        self.prepare()
        self.assertEqual(len(self.engine.resources['credentials']), 2)
        self.assertEqual(len([c for c in self.posts() if c[1] == '/api/v1/credentials']), 2)

    def test_lost_workflow_create_is_reconciled_without_a_duplicate_flow(self):
        self.engine.lost_create = 'workflows'
        with self.assertRaises(UploadError):
            self.prepare()
        self.prepare()
        self.assertEqual(len(self.engine.resources['workflows']), 1)
        self.assertEqual(len([c for c in self.posts() if c[1] == '/api/v1/workflows']), 1)

    def test_unconfirmed_create_not_found_is_not_blindly_reposted(self):
        self.engine.lost_create = 'credentials'
        with self.assertRaises(UploadError):
            self.prepare()
        self.engine.resources['credentials'].clear()
        previous = len(self.posts())
        with self.assertRaises(UploadError) as context:
            self.prepare()
        self.assertEqual(context.exception.code, 'ENGINE_CREATE_UNCONFIRMED')
        self.assertEqual(len(self.posts()), previous)

    def test_ambiguous_resource_name_is_not_auto_selected(self):
        self.engine.lost_create = 'credentials'
        with self.assertRaises(UploadError):
            self.prepare()
        prior = next(iter(self.engine.resources['credentials'].values()))
        self.engine.resources['credentials']['duplicate'] = {**prior, 'id': 'duplicate'}
        with self.assertRaises(UploadError) as context:
            self.prepare()
        self.assertEqual(context.exception.code, 'ENGINE_CREATE_UNCONFIRMED')

    def test_modified_flow_is_neither_executed_nor_overwritten(self):
        self.prepare()
        flow = next(iter(self.engine.resources['workflows'].values()))
        flow['nodes'][2]['parameters']['url'] = 'https://unreviewed.invalid/steal'
        previous = len(self.posts())
        with self.assertRaises(UploadError):
            self.workflow.dispatch(self.job()['job_id'], self.engine)
        with self.assertRaises(UploadError):
            self.prepare()
        self.assertEqual(len(self.posts()), previous)

    def test_changed_binding_refuses_before_remote_mutation(self):
        self.prepare(); previous = len(self.engine.calls)
        with self.assertRaises(UploadError) as context:
            self.prepare('http://127.0.0.1:9999')
        self.assertEqual(context.exception.code, 'WORKFLOW_SETUP_CHANGED')
        self.assertEqual(len(self.engine.calls), previous)

    def test_upload_must_have_a_verified_source_receipt(self):
        self.prepare()
        job = self.uploads.accept(str(uuid4()), 'pending', 'x.txt', b'pending', DATA_ORIGIN)
        with self.assertRaises(UploadError) as context:
            self.workflow.dispatch(job['job_id'], self.engine)
        self.assertEqual(context.exception.code, 'SOURCE_RECEIPT_REQUIRED')
        self.assertEqual(self.workflow.handoff(job['job_id'])['state'], 'not_requested')

    def test_uncertain_delivery_keeps_original_body_and_requires_explicit_retry(self):
        self.prepare(); job = self.job(); self.engine.no_callback = True
        first = self.workflow.dispatch(job['job_id'], self.engine)
        self.assertEqual(first['state'], 'confirmation_required')
        self.workflow.dispatch(job['job_id'], self.engine)
        self.assertEqual(len(self.posts('/webhook/')), 1)
        self.engine.no_callback = False
        resumed = WorkflowStore(UploadStore(self.root))
        self.assertEqual(resumed.dispatch(job['job_id'], self.engine, retry=True)['state'], 'accepted')
        calls = self.posts('/webhook/')
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertNotIn('execution_id', calls[0][2])

    def test_lost_response_after_callback_is_confirmed_without_retransmission(self):
        self.prepare(); job = self.job(); self.engine.lost_reply = True
        report = self.workflow.dispatch(job['job_id'], self.engine)
        self.assertEqual(report['state'], 'accepted')
        self.assertIsNone(report['error_code'])
        self.workflow.dispatch(job['job_id'], self.engine, retry=True)
        self.assertEqual(len(self.posts('/webhook/')), 1)

    def test_generic_webhook_started_response_is_not_a_verified_receipt(self):
        self.prepare(); self.engine.generic_reply = True
        report = self.workflow.dispatch(self.job()['job_id'], self.engine)
        self.assertEqual(report['state'], 'confirmation_required')
        self.assertFalse(report['workflow_started'])

    def test_duplicate_triggers_return_one_receipt_and_never_grant_processing(self):
        self.prepare(); job = self.job()
        first = self.workflow.dispatch(job['job_id'], self.engine)['receipt']
        body = self.posts('/webhook/')[0][2]
        def duplicate(i):
            return self.workflow.accept_receipt({**body, 'workflow_id': first['workflow_id'],
                'definition_sha256': first['definition_sha256'], 'execution_id': 'duplicate_' + str(i)})
        with ThreadPoolExecutor(max_workers=5) as pool:
            receipts = list(pool.map(duplicate, range(8)))
        self.assertTrue(all(x == first for x in receipts))
        self.assertFalse(first['may_execute_next_stage'])
        self.assertFalse(first['question_ready'])
        self.assertEqual(len(self.workflow.handoff(job['job_id'])['execution_ids']), 9)

    def test_callback_cannot_change_source_library_sequence_or_target(self):
        self.prepare(); job = self.job(); first = self.workflow.dispatch(job['job_id'], self.engine)['receipt']
        value = {**self.posts('/webhook/')[0][2], 'workflow_id': first['workflow_id'],
                 'execution_id': 'reject', 'definition_sha256': first['definition_sha256']}
        for field, replacement in [('source_document_id', str(uuid4())), ('library_id', str(uuid4())),
                                    ('sequence', 999), ('schema', True), ('workflow_id', 'other'),
                                    ('definition_sha256', '0' * 64), ('callback_url', 'https://unreviewed.invalid')]:
            with self.subTest(field=field), self.assertRaises(UploadError):
                self.workflow.accept_receipt({**value, field: replacement})
        self.assertEqual(self.workflow.handoff(job['job_id'])['receipt'], first)

    def test_process_death_preserves_intent_and_releases_sender_lock(self):
        self.prepare(); job = self.job()
        code = '''
import os,sys
from src.apps.document_qa.uploads import UploadStore
from src.apps.document_qa.workflows import WorkflowStore
w=WorkflowStore(UploadStore(sys.argv[1]));s=w.setup();flow=w.desired(s)[0]
class Crash:
 origin=s['engine_origin']
 def request(self,method,route,body=None,headers=None):
  if method=='POST':os._exit(7)
  if route=='/rest/settings':return {'data':{'versionCli':'2.40.5'}}
  return dict(flow,active=True,versionId=s['published_version_id'],activeVersionId=s['published_version_id'])
w.dispatch(sys.argv[2],Crash())
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.root), job['job_id']], capture_output=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.workflow.handoff(job['job_id'])['state'], 'confirmation_required')
        self.assertEqual(self.workflow.dispatch(job['job_id'], self.engine, retry=True)['state'], 'accepted')
        self.assertEqual(self.uploads.library()['question_revision'], 0)

    def test_same_job_sender_is_locked_but_remote_io_does_not_hold_db(self):
        self.prepare(); job = self.job(); self.engine.entry, self.engine.release = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.workflow.dispatch, job['job_id'], self.engine)
            self.assertTrue(self.engine.entry.wait(3))
            try:
                with self.assertRaises(UploadError) as context:
                    self.workflow.dispatch(job['job_id'], self.engine, retry=True)
                self.assertEqual(context.exception.code, 'UPLOAD_BUSY')
                self.job('second')
            finally:
                self.engine.release.set()
            self.assertEqual(pending.result()['state'], 'accepted')

    def test_attempt_and_execution_history_limits_are_bounded(self):
        self.prepare(); job = self.job(); body, setup = self.workflow.ensure_intent(job['job_id'])
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE handoffs SET attempts=16 WHERE job_id=?', (job['job_id'],))
        with self.assertRaises(UploadError) as context:
            self.workflow.dispatch(job['job_id'], self.engine, retry=True)
        self.assertEqual(context.exception.code, 'HANDOFF_REVIEW_REQUIRED')
        value = {**body, 'workflow_id': setup['resources']['workflow']['id'], 'definition_sha256': setup['definition_sha256']}
        for i in range(16):
            self.workflow.accept_receipt({**value, 'execution_id': str(i)})
        with self.assertRaises(UploadError):
            self.workflow.accept_receipt({**value, 'execution_id': 'seventeenth'})
        self.assertEqual(self.workflow.accept_receipt({**value, 'execution_id': '0'})['first_execution_id'], '0')

    def test_public_status_excludes_secrets_and_original_bytes(self):
        self.prepare(); self.workflow.dispatch(self.job()['job_id'], self.engine)
        output = json.dumps(self.workflow.status()) + json.dumps(self.workflow.handoff(self.uploads.jobs()[0]['job_id']))
        for secret in (self.workflow.token_path.read_text().strip(), self.workflow.hook_path.read_text().strip(),
                       self.uploads.token_path.read_text().strip(), 'test-only-api-key', 'GPU-free fixture source'):
            self.assertNotIn(secret, output)


class TemplateTests(Fixture):
    def evaluate(self, name, value, references=None):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node unavailable: Code-node evaluation not verified')
        flow = self.workflow.desired(self.workflow.setup())[0]
        script = '''const fs=require('fs'),vm=require('vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));
try {const result=vm.runInNewContext('(function(){'+x.code+'})()',{$input:{first:()=>({json:x.value})},
$:name=>({first:()=>({json:x.references[name]})}),$workflow:{id:x.workflow_id},$execution:{id:'node_execution'},require,Buffer,Date});
process.stdout.write(JSON.stringify(result[0].json));}catch(e){process.stdout.write(JSON.stringify({error:e.message}));process.exitCode=2;}'''
        code = next(n['parameters']['jsCode'] for n in flow['nodes'] if n['name'] == name)
        payload = {'code': code, 'value': value, 'references': references or {}, 'workflow_id': self.workflow.setup()['resources']['workflow']['id']}
        result = subprocess.run([node, '-e', script], input=json.dumps(payload), text=True, capture_output=True, timeout=5)
        return result.returncode, json.loads(result.stdout)

    def webhook_input(self):
        job = self.job(); body, setup = self.workflow.ensure_intent(job['job_id'])
        token = signed_headers(self.workflow.hook_path.read_text().strip(), setup['path'], body)['Authorization'].split(' ')[1]
        claims = token.split('.')[1]
        return {'body': body, 'jwtPayload': json.loads(base64.urlsafe_b64decode(claims + '=' * (-len(claims) % 4)))}

    def test_fixed_template_is_deterministic_and_credentials_are_references(self):
        self.prepare(); setup = self.workflow.setup(); a = self.workflow.desired(setup); b = self.workflow.desired(setup)
        self.assertEqual(a, b)
        self.assertTrue(matches(a[0], b[0]))
        exported = json.dumps(a[0])
        self.assertNotIn(self.workflow.token_path.read_text().strip(), exported)
        self.assertNotIn(self.workflow.hook_path.read_text().strip(), exported)
        self.assertNotIn('/api/refinement', exported)
        self.assertNotIn('$env', exported)

    def test_actual_javascript_accepts_bound_request_and_retains_stable_handoff(self):
        self.prepare(); value = self.webhook_input()
        status, output = self.evaluate('Validate handoff', value)
        self.assertEqual(status, 0)
        self.assertEqual(output['handoff_id'], value['body']['handoff_id'])
        self.assertEqual(output['source_document_id'], value['body']['source_document_id'])
        self.assertEqual(output['execution_id'], 'node_execution')

    def test_actual_javascript_rejects_changed_payload_expired_token_and_extra_url(self):
        self.prepare(); value = self.webhook_input()
        mutations = [copy.deepcopy(value) for _ in range(3)]
        mutations[0]['body']['source_document_id'] = str(uuid4())
        mutations[1]['jwtPayload']['exp'] = 0
        mutations[2]['body']['callback_url'] = 'https://unreviewed.invalid'
        for changed in mutations:
            self.assertEqual(self.evaluate('Validate handoff', changed)[0], 2)

    def test_actual_javascript_verifies_receipt_and_blocks_next_stage(self):
        self.prepare(); value = self.webhook_input(); _, prepared = self.evaluate('Validate handoff', value)
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE handoffs SET attempts=1')
        receipt = self.workflow.accept_receipt(prepared)
        status, output = self.evaluate('Verify QA receipt', {'statusCode': 200, 'body': receipt}, {'Validate handoff': prepared})
        self.assertEqual((status, output), (0, receipt))
        for bad in ({**receipt, 'may_execute_next_stage': True}, {**receipt, 'question_ready': True}):
            self.assertEqual(self.evaluate('Verify QA receipt', {'statusCode': 200, 'body': bad}, {'Validate handoff': prepared})[0], 2)


class ApiTests(Fixture):
    def request(self, port, method, path, body=None, workflow_auth=False):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
        token = (self.workflow.token_path if workflow_auth else self.uploads.token_path).read_text().strip()
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_callback_auth_is_separate_from_upload_operator_auth(self):
        with running(make_server(self.uploads, DataFixture(), port=0, workflow=self.workflow)) as port:
            self.assertEqual(self.request(port, 'POST', '/api/workflow/receipts', {}, workflow_auth=False)[0], 401)
            self.assertEqual(self.request(port, 'GET', '/api/library', workflow_auth=True)[0], 401)
            self.assertEqual(self.request(port, 'GET', '/api/workflows')[1]['workflow_configured'], False)

    def test_real_http_receipt_callback_matches_durable_job_and_duplicate_result(self):
        self.prepare(); job = self.job()
        with running(make_server(self.uploads, DataFixture(), port=0, workflow=self.workflow)) as port:
            self.engine.callback = lambda body: self.request(port, 'POST', '/api/workflow/receipts', body, workflow_auth=True)[1]
            report = self.workflow.dispatch(job['job_id'], self.engine)
            self.assertEqual(report['state'], 'accepted')
            self.assertEqual(self.request(port, 'GET', '/api/jobs/' + job['job_id'] + '/workflow'), (200, report))

    def test_status_routes_report_durable_handoff_without_marking_question_ready(self):
        self.prepare(); job = self.job()
        self.workflow.dispatch(job['job_id'], self.engine)
        with running(make_server(self.uploads, DataFixture(), port=0, workflow=self.workflow)) as port:
            for path in ('/api/jobs/' + job['job_id'], '/api/library'):
                status, value = self.request(port, 'GET', path)
                self.assertEqual(status, 200)
                self.assertTrue(value['workflow_started'])
                self.assertFalse(value['question_ready'])
            self.assertTrue(self.request(port, 'GET', '/api/jobs')[1]['items'][0]['workflow_started'])

    def test_four_operator_dispatches_leave_capacity_for_actual_http_callbacks(self):
        from unittest.mock import patch
        self.prepare()
        jobs = [self.job('자료 ' + str(n)) for n in range(4)]
        barrier = threading.Barrier(4)
        with running(make_server(self.uploads, DataFixture(), port=0, workflow=self.workflow)) as port:
            def callback(body):
                barrier.wait(3)
                status, receipt = self.request(port, 'POST', '/api/workflow/receipts', body, workflow_auth=True)
                self.assertEqual(status, 200)
                return receipt
            self.engine.callback = callback
            with patch('src.apps.document_qa.workflows.key_client', return_value=self.engine), ThreadPoolExecutor(4) as pool:
                reports = list(pool.map(lambda job: self.request(port, 'POST', '/api/jobs/' + job['job_id'] + '/dispatch-workflow', {}), jobs))
            self.assertTrue(all(status == 202 and value['state'] == 'accepted' for status, value in reports), reports)

    def test_wildcard_and_public_network_bind_are_refused(self):
        for bind in ('0.0.0.0', '8.8.8.8', 'unresolved.invalid'):
            with self.subTest(bind=bind), self.assertRaises(UploadError):
                make_server(self.uploads, DataFixture(), port=0, workflow=self.workflow, bind=bind)

    def test_upload_with_unconfigured_workflow_keeps_verified_source_and_waits(self):
        meta = metadata('새 자료', 'x.txt', b'CPU only')
        content_type, raw = encode_multipart(meta, b'CPU only', str(uuid4()))
        with running(make_server(self.uploads, DataFixture(), port=0, workflow=self.workflow)) as port:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
            try:
                conn.request('POST', '/api/uploads', raw, {'Authorization': 'Bearer ' + self.uploads.token_path.read_text().strip(),
                             'Content-Type': content_type, 'Idempotency-Key': str(uuid4())})
                response = conn.getresponse(); body = json.loads(response.read())
                self.assertEqual(response.status, 202)
                self.assertEqual(body['state'], 'preparation_pending')
                self.assertEqual(body['workflow']['dispatch_code'], 'WORKFLOW_NOT_CONFIGURED')
                self.assertFalse(body['workflow_started'])
                self.assertEqual(self.uploads.library()['pending_original_bytes'], 0)
            finally:
                conn.close()


class TransportTests(Fixture):
    @contextmanager
    def engine_http(self, response='valid'):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                seen.append((self.path, self.headers.get('X-N8N-API-KEY')))
                raw = b'{"id":"flow"}'
                if response == 'redirect':
                    self.send_response(307); self.send_header('Location', '/redirect'); self.end_headers(); return
                if response == 'oversize':
                    raw = b'x' * ((2 << 20) + 1)
                if response == 'duplicate':
                    raw = b'{"id":"one","id":"two"}'
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(raw)
        with running(ThreadingHTTPServer(('127.0.0.1', 0), Handler)) as port:
            yield EngineHTTP(f'http://127.0.0.1:{port}', 'test-secret'), seen

    def test_n8n_api_auth_sent_only_to_configured_api_origin(self):
        with self.engine_http() as (client, seen):
            self.assertEqual(client.request('GET', '/api/v1/workflows/flow'), {'id':'flow'})
        self.assertEqual(seen, [('/api/v1/workflows/flow', 'test-secret')])

    def test_redirect_and_unknown_routes_are_not_executed(self):
        with self.engine_http('redirect') as (client, seen):
            with self.assertRaises(UploadError) as context:
                client.request('GET', '/api/v1/workflows/flow')
            self.assertEqual(context.exception.code, 'ENGINE_REDIRECT_REFUSED')
            with self.assertRaises(UploadError):
                client.request('POST', '/api/v1/credentials/other/delete', {})
        self.assertEqual(len(seen), 1)

    def test_response_limit_and_duplicate_json_rejected(self):
        for mode in ('oversize','duplicate'):
            with self.subTest(mode=mode), self.engine_http(mode) as (client, seen), self.assertRaises(UploadError):
                client.request('GET', '/api/v1/workflows/flow')

    def test_shared_deadline_blocks_before_request(self):
        clock = [0]
        client = EngineHTTP(ORIGIN, 'test-secret', clock=lambda: clock[0]); clock[0] = 61
        with self.assertRaises(UploadError) as context:
            client.request('GET', '/api/v1/workflows/flow')
        self.assertEqual(context.exception.code, 'ENGINE_BUDGET_EXCEEDED')


if __name__ == '__main__':
    unittest.main()
