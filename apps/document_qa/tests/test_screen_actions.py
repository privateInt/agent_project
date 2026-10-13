"""Q07b real loopback HTTP with disposable SQLite and independent upstream fakes."""
import http.client
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from apps.document_qa.api import make_server
from apps.document_qa.intake import UploadError
from apps.document_qa.recovery import projection
from apps.document_qa.refinement_flow import FLOW_SECONDS
from apps.document_qa.tests.test_refinement_flow import Fixture
from apps.document_qa.tests.test_refinement import DATA, ORIGIN, running
from apps.document_qa.tests.test_uploads import FakeData as FakeUpload
from apps.document_qa.tests.test_indexing_flow import Fixture as IndexingFixture


CAPS = {'upload': True, 'entry_flow': True, 'cpu_flow': True, 'refiner': True}


def synthetic(**extra):
    return {'job_id': str(uuid4()), 'state': 'preparation_pending', 'attempts': 1,
            'workflow': {'state': 'accepted'}, **extra}


class RecoveryTests(Fixture):
    def server(self, **options):
        return make_server(self.uploads, options.pop('uploader', self.uploaded), port=0,
                           workflow=self.entry, refinement=self.refinement, refinement_flow=self.flow,
                           refiner_factory=lambda: self.data, **options)

    def request(self, port, path, value=None, auth=True, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        values = {'Authorization': 'Bearer ' + self.uploads.token_path.read_text().strip()} if auth else {}
        values.update(headers or {})
        if value is not None:
            values['Content-Type'] = 'application/json'
        try:
            conn.request('GET' if value is None else 'POST', path,
                         None if value is None else json.dumps(value), values)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def plan(self, port, job):
        status, value = self.request(port, '/api/jobs/' + job['job_id'])
        self.assertEqual(status, 200, value)
        return value['recovery']

    def perform(self, port, job, plan, action):
        return self.request(port, '/api/jobs/' + job['job_id'] + '/recover',
                            {'revision': plan['revision'], 'action': action})

    def ids(self, plan):
        return [item['id'] for item in plan['actions']]

    def test_GET_projects_local_actions_without_calls_DB_or_secret_changes(self):
        job = self.job()
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        counts = len(self.entry_engine.calls), len(self.engine.calls), len(self.data.calls)
        with running(self.server()) as port:
            for route in ['/api/jobs', '/api/jobs/' + job['job_id']]:
                status, value = self.request(port, route)
                self.assertEqual(status, 200)
                self.assertNotIn(self.uploads.token_path.read_text().strip(), json.dumps(value))
            self.assertEqual(self.plan(port, job)['reason'], 'SETUP_REQUIRED')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertEqual(counts, (len(self.entry_engine.calls), len(self.engine.calls), len(self.data.calls)))

    def test_same_revision_repeat_and_response_loss_cannot_repost_original(self):
        uploader = FakeUpload()
        job = self.uploads.accept(str(uuid4()), '자료', 'x.txt', DATA, uploader.origin)
        with running(self.server(uploader=uploader)) as port:
            plan = self.plan(port, job)
            self.assertEqual(self.ids(plan), ['retry-upload'])
            status, response = self.perform(port, job, plan, 'retry-upload')
            self.assertEqual(status, 202, response)
            self.assertEqual(response['job']['state'], 'preparation_pending')
            self.assertEqual(self.perform(port, job, plan, 'retry-upload')[0], 409)
        self.assertEqual(len(uploader.calls), 1)
        self.assertEqual(len(self.uploads.jobs()), 1)
        self.assertEqual(self.entry_engine.calls, [])

    def test_ambiguous_original_retry_uses_saved_bytes_and_key(self):
        uploader = FakeUpload(error=True)
        job = self.uploads.accept(str(uuid4()), '자료', 'x.txt', DATA, uploader.origin)
        self.uploads.dispatch(job['job_id'], uploader)
        before = self.uploads.job(job['job_id'])
        uploader.error = False
        with running(self.server(uploader=uploader)) as port:
            plan = self.plan(port, job)
            status, response = self.perform(port, job, plan, 'retry-upload')
            self.assertEqual(status, 202, response)
            self.assertGreater(response['job']['attempts'], before['attempts'])
        self.assertEqual(uploader.calls[0], uploader.calls[1])
        self.assertEqual(len(self.uploads.jobs()), 1)

    def test_entry_trigger_does_not_implicitly_dispatch_CPU_or_indexing(self):
        self.prepare(); job = self.job()
        with patch('apps.document_qa.workflows.key_client', return_value=self.entry_engine), running(self.server()) as port:
            plan = self.plan(port, job)
            self.assertEqual(self.ids(plan), ['dispatch-workflow'])
            status, response = self.perform(port, job, plan, 'dispatch-workflow')
            self.assertEqual(status, 202, response)
            self.assertEqual(response['job']['workflow']['state'], 'accepted')
            self.assertEqual(self.ids(response['job']['recovery']), ['dispatch-refinement-workflow'])
        self.assertIsNone(self.flow.current(job['job_id']))
        self.assertEqual(self.data.calls, [])

    def test_entry_unknown_replays_saved_handoff_not_new_intent(self):
        self.prepare(); job = self.job(); self.entry_engine.no_callback = True
        self.entry.dispatch(job['job_id'], self.entry_engine)
        with self.uploads.connect() as db:
            before = db.execute('SELECT body FROM handoffs').fetchone()[0]
        self.entry_engine.no_callback = False
        with patch('apps.document_qa.workflows.key_client', return_value=self.entry_engine), running(self.server()) as port:
            plan = self.plan(port, job)
            self.assertEqual(self.ids(plan), ['retry-workflow'])
            self.assertEqual(self.perform(port, job, plan, 'retry-workflow')[0], 202)
        with self.uploads.connect() as db:
            self.assertEqual(db.execute('SELECT body FROM handoffs').fetchone()[0], before)

    def test_CPU_trigger_and_unknown_replay_preserve_generation_and_body(self):
        self.prepare(); job = self.job(); self.entry.dispatch(job['job_id'], self.entry_engine)
        self.engine.no_callback = True
        with patch('apps.document_qa.refinement_flow.key_client', return_value=self.engine), running(self.server()) as port:
            plan = self.plan(port, job)
            self.assertEqual(self.perform(port, job, plan, 'dispatch-refinement-workflow')[0], 202)
            row = self.flow.current(job['job_id']); body = row['body']; handoff = row['handoff_id']
            plan = self.plan(port, job); self.assertEqual(self.ids(plan), ['retry-refinement-workflow'])
            self.engine.no_callback = False
            self.assertEqual(self.perform(port, job, plan, 'retry-refinement-workflow')[0], 202)
        after = self.flow.current(job['job_id'])
        self.assertEqual((after['generation'], after['body'], after['handoff_id']), (1, body, handoff))
        self.assertEqual(self.data.calls, [])

    def test_unknown_Data_refinement_requires_explicit_same_key_replay(self):
        job, grant, _ = self.started(); self.data.lost_reply = True
        self.flow.step(grant, lambda: self.data)
        with self.uploads.connect() as db:
            before = dict(db.execute('SELECT * FROM refinement_tasks').fetchone())
        self.data.lost_reply = False
        with running(self.server()) as port:
            plan = self.plan(port, job); self.assertEqual(self.ids(plan), ['retry-refinement'])
            status, response = self.perform(port, job, plan, 'retry-refinement')
            self.assertEqual(status, 202, response)
            self.assertEqual(response['job']['refinement']['state'], 'waiting')
        after = self.refinement.row(job['job_id'])
        self.assertEqual(before['request_key'], after['request_key'])
        self.assertEqual(before['request_body'], after['request_body'])
        self.assertEqual(len(self.data.runs), 1)
        self.assertEqual(self.flow.current(job['job_id'])['generation'], 1)

    def test_observe_known_receipt_performs_only_Data_GET_and_can_complete(self):
        job, grant, _ = self.started(); self.flow.step(grant, lambda: self.data)
        run_id = self.refinement.job(job['job_id'])['receipt']['resource_id']
        self.data.runs[run_id].update(status='succeeded', phase='completed')
        count = len(self.data.calls)
        with running(self.server()) as port:
            plan = self.plan(port, job); self.assertEqual(self.ids(plan), ['observe-refinement'])
            status, response = self.perform(port, job, plan, 'observe-refinement')
            self.assertEqual(status, 202, response)
            self.assertEqual(response['job']['refinement']['state'], 'canonical_verified')
            self.assertEqual(response['job']['recovery']['actions'], [])
        self.assertTrue(all(call[0] == 'GET' for call in self.data.calls[count:]))

    def test_expired_CPU_resume_keeps_Data_receipt_and_supersedes_old_grant(self):
        job, grant, _ = self.started(); self.flow.step(grant, lambda: self.data)
        receipt = self.refinement.job(job['job_id'])['receipt']
        self.epoch += FLOW_SECONDS + 1
        posts = len(self.posts())
        with patch('apps.document_qa.refinement_flow.key_client', return_value=self.engine), running(self.server()) as port:
            plan = self.plan(port, job)
            self.assertEqual(self.ids(plan), ['observe-refinement', 'resume-refinement-workflow'])
            self.assertEqual(self.perform(port, job, plan, 'resume-refinement-workflow')[0], 202)
            self.assertEqual(self.perform(port, job, plan, 'resume-refinement-workflow')[0], 409)
        self.assertEqual(self.flow.current(job['job_id'])['generation'], 2)
        self.assertEqual(self.refinement.job(job['job_id'])['receipt'], receipt)
        self.assertEqual(len(self.posts()), posts)
        with self.assertRaises(UploadError):
            self.flow.step(grant, lambda: self.data)

    def test_concurrent_same_snapshot_has_one_upstream_delivery(self):
        uploader = FakeUpload(); job = self.uploads.accept(str(uuid4()), '자료', 'x.txt', DATA, uploader.origin)
        with running(self.server(uploader=uploader)) as port:
            plan = self.plan(port, job)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.perform(port, job, plan, 'retry-upload'), range(2)))
            self.assertEqual(sorted(status for status, _ in results), [202, 409])
        self.assertEqual(len(uploader.calls), 1)

    def test_changed_config_blocks_snapshot_before_any_remote_call(self):
        self.prepare(); job = self.job()
        with running(self.server()) as port:
            plan = self.plan(port, job)
            setup = self.entry.setup(); setup['test_marker'] = 'changed'; self.entry.save_setup(setup)
            status, response = self.perform(port, job, plan, 'dispatch-workflow')
            self.assertEqual((status, response['code']), (409, 'RECOVERY_STATE_CHANGED'))
        self.assertEqual(self.entry.handoff(job['job_id'])['state'], 'not_requested')

    def test_no_auth_foreign_origin_invalid_body_or_unknown_action_cannot_write(self):
        job = self.job(); before = self.uploads.db_path.read_bytes()
        with running(self.server()) as port:
            path = '/api/jobs/' + job['job_id'] + '/recover'
            self.assertEqual(self.request(port, path, {}, auth=False)[0], 401)
            self.assertEqual(self.request(port, path, {}, headers={'Origin': 'https://foreign.invalid'})[0], 403)
            for value in [{}, [], {'action':'retry-upload','revision':'bad'}, {'action':'retry-upload','revision':'a'*64,'extra':1}]:
                self.assertEqual(self.request(port, path, value)[0], 400)
            plan = self.plan(port, job)
            for action in ['resume-indexing-workflow', 'activate-head', 'retry-refinement', '../retry-upload']:
                self.assertEqual(self.perform(port, job, plan, action)[0], 409)
        self.assertEqual(self.uploads.db_path.read_bytes(), before)

    def test_unknown_job_does_not_create_lock(self):
        before = {p.name for p in (self.root / 'locks').iterdir()}
        with running(self.server()) as port:
            self.assertEqual(self.request(port, '/api/jobs/' + str(uuid4()) + '/recover',
                                          {'action':'retry-upload','revision':'a'*64})[0], 404)
        self.assertEqual(before, {p.name for p in (self.root / 'locks').iterdir()})

    def test_terminal_and_integrity_errors_are_not_replayed(self):
        for domain in [{'state':'failed','terminal':True,'receipt':{'resource_id':str(uuid4())}},
                       {'state':'attention_required','terminal':True},
                       {'state':'attention_required','receipt':{'resource_id':str(uuid4())},'error_code':'REFINEMENT_PROVENANCE_MISMATCH'},
                       {'state':'attention_required','receipt':{'resource_id':str(uuid4())},'error_code':'CANONICAL_BYTES_MISMATCH'},
                       {'state':'attention_required','receipt':{'resource_id':str(uuid4())},'error_code':'CANONICAL_SOURCE_MISMATCH'}]:
            with self.subTest(domain=domain):
                self.assertEqual(projection(synthetic(refinement=domain), CAPS)['actions'], [])

    def test_GPU_paused_index_has_no_recovery_even_if_inference_elsewhere_enabled(self):
        job = synthetic(refinement={'state':'canonical_verified','terminal':True},
                        indexing={'state':'not_requested'},
                        indexing_workflow={'state':'blocked','error_code':'GPU_VERIFICATION_DEFERRED',
                                           'receipt':{'may_enable_inference':False}})
        plan = projection(job, {**CAPS, 'indexing_inference_enabled':True})
        self.assertEqual((plan['actions'], plan['reason']), ([], 'GPU_WAIT'))
        self.assertFalse(job['indexing_workflow']['receipt']['may_enable_inference'])

    def test_limits_missing_setup_and_active_CPU_are_fail_closed(self):
        for job in [synthetic(state='upload_confirmation_required',attempts=100),
                    synthetic(workflow={'state':'confirmation_required','attempts':16}),
                    synthetic(refinement={'state':'confirmation_required','attempts':16}),
                    synthetic(refinement_workflow={'delivery_state':'accepted','state':'expired','generation':16}),
                    synthetic(refinement_workflow={'delivery_state':'accepted','state':'running','generation':1})]:
            with self.subTest(job=job):
                self.assertEqual(projection(job, CAPS)['actions'], [])
        self.assertEqual(projection(synthetic(), {k:False for k in CAPS})['reason'], 'SETUP_REQUIRED')

    def test_old_accepted_stage_cannot_be_manually_selected_again(self):
        job, _, _ = self.started()
        with running(self.server()) as port:
            plan = self.plan(port, job)
            for action in ['retry-upload', 'retry-workflow', 'dispatch-refinement-workflow', 'resume-refinement-workflow']:
                status, response = self.perform(port, job, plan, action)
                self.assertEqual((status, response['code']), (409, 'RECOVERY_ACTION_NOT_ALLOWED'))

    def test_native_snapshot_change_is_rejected_before_refiner_factory(self):
        job, grant, _ = self.started()
        with running(self.server()) as port:
            plan = self.plan(port, job)
            self.flow.step(grant, lambda: self.data)
            posts = len(self.posts())
            self.assertEqual(self.perform(port, job, plan, 'retry-refinement')[0], 409)
            self.assertEqual(len(self.posts()), posts)

    def test_native_definition_verification_still_blocks_changed_engine(self):
        self.prepare(); job = self.job()
        setup = self.entry.setup()
        workflow = self.entry_engine.resources['workflows'][setup['resources']['workflow']['id']]
        workflow['nodes'][0]['parameters']['test_change'] = 'changed'
        with patch('apps.document_qa.workflows.key_client', return_value=self.entry_engine), running(self.server()) as port:
            plan = self.plan(port, job)
            status, _ = self.perform(port, job, plan, 'dispatch-workflow')
            self.assertNotEqual(status, 202)
        self.assertEqual(self.entry.handoff(job['job_id'])['attempts'], 0)


class GPURecoveryTests(IndexingFixture):
    request = RecoveryTests.request
    plan = RecoveryTests.plan
    perform = RecoveryTests.perform

    def test_real_saved_GPU_deferral_cannot_be_resumed_or_grant_changed(self):
        job = self.canonical_job()
        self.flow.dispatch(job['job_id'], self.engine, paused=True)
        grant, receipt = self.grant_for(self.flow, job['job_id'])
        self.assertEqual(self.step(grant)['error_code'], 'GPU_VERIFICATION_DEFERRED')
        before = self.uploads.db_path.read_bytes()
        keys = {p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file() and ('token' in p.name or 'secret' in p.name)}
        server = make_server(self.uploads, self.uploaded, port=0, workflow=self.entry,
                             refinement=self.refinement, refinement_flow=self.cpu,
                             refiner_factory=lambda:self.fail('completed refinement must not be reopened'),
                             indexing=self.indexing, indexing_flow=self.flow,
                             indexer_factory=lambda **kw:self.fail('no Knowledge call'),
                             indexing_gate=type('Gate', (), {'allow_inference':True})())
        with running(server) as port:
            plan = self.plan(port, job)
            self.assertEqual((plan['actions'], plan['reason']), ([], 'GPU_WAIT'))
            for action in ['resume-indexing-workflow','retry-indexing-workflow','retry-upload','resume-refinement-workflow']:
                self.assertEqual(self.perform(port, job, plan, action)[0], 409)
        self.assertEqual(before, self.uploads.db_path.read_bytes())
        self.assertEqual(receipt, json.loads(self.flow.current(job['job_id'])['receipt']))
        self.assertEqual(keys, {p.name:p.read_bytes() for p in self.root.iterdir() if p.name in keys})
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.gate_calls, [])
