"""Q08 API composition with real loopback HTTP and disposable private stores only."""
import http.client
import json
from uuid import uuid4

from apps.document_qa.api import make_server
from apps.document_qa.tests.test_question_head import Fixture
from apps.document_qa.tests.test_refinement import running


class QuestionScreenTests(Fixture):
    def server(self, inference=False, **options):
        return make_server(self.uploads, object(), port=0, qa_agent=self.qa,
                           question_head=self.head, agent_factory=lambda: self.fake,
                           question_inference_enabled=inference, **options)

    def request(self, port, path, value=None, key=None, auth=True, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        fields = {'Authorization': 'Bearer ' + self.uploads.token_path.read_text().strip()} if auth else {}
        fields.update(headers or {})
        if value is not None:
            fields['Content-Type'] = 'application/json'
        if key is not None:
            fields['Idempotency-Key'] = key
        try:
            conn.request('GET' if value is None else 'POST', path,
                         None if value is None else json.dumps(value), fields)
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw)
        finally:
            conn.close()

    def accept(self, port, question='자료의 결론은?', key=None):
        status, q = self.request(port, '/api/questions', {'question': question}, key or str(uuid4()))
        self.assertEqual(status, 202, q)
        return q

    def test_empty_head_policy_and_rejected_intake_leave_existing_document_and_secrets(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with running(self.server()) as port:
            status, policy = self.request(port, '/api/question-policy')
            self.assertEqual(status, 200)
            self.assertFalse(policy['question_ready'])
            self.assertFalse(policy['head_active'])
            self.assertEqual(self.request(port, '/api/questions'), (200, {'items': []}))
            status, response = self.request(port, '/api/questions', {'question': '질문'}, str(uuid4()))
            self.assertEqual((status, response['code']), (409, 'QUESTION_HEAD_NOT_ACTIVE'))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertEqual(self.fake.calls, [])

    def test_ready_policy_accept_then_dispatch_and_explicit_observe_verified_sources(self):
        self.active()
        self.fake.calls.clear()
        with running(self.server(True)) as port:
            self.assertTrue(self.request(port, '/api/question-policy')[1]['question_ready'])
            q = self.accept(port)
            self.assertEqual((q['state'], q['agent_run_id']), ('pending', None))
            self.assertEqual(self.fake.calls, [])
            path = '/api/questions/' + q['question_id']
            status, waiting = self.request(port, path + '/dispatch', {})
            self.assertEqual(status, 202)
            self.assertEqual(waiting['state'], 'waiting')
            self.assertEqual(len(self.fake.posts('/api/agent-runs')), 1)
            self.fake.finish()
            before = len(self.fake.posts('/api/agent-runs'))
            status, complete = self.request(port, path + '/observe', {})
            self.assertEqual((status, complete['state']), (200, 'complete'))
            self.assertEqual(complete['head_revision'], q['head_revision'])
            source = complete['result']['citations'][0]['source']
            self.assertEqual(source['source_spans'], [{'block_id': 'paragraph-1'}])
            self.assertEqual(source['canonical_id'], self.fake.candidate['documents'][0]['canonical_id'])
            self.assertIn('[S1]', complete['result']['answer'])
            self.assertFalse(complete['semantic_quality_verified'])
            self.assertEqual(len(self.fake.posts('/api/agent-runs')), before)

    def test_active_head_with_inference_paused_never_dispatches_a_run(self):
        self.active()
        self.fake.calls.clear()
        with running(self.server(False)) as port:
            p = self.request(port, '/api/question-policy')[1]
            self.assertTrue(p['head_active'])
            self.assertFalse(p['question_ready'])
            q = self.accept(port)  # Existing API permits local-only intake; the screen uses policy first.
            status, body = self.request(port, '/api/questions/' + q['question_id'] + '/dispatch', {})
            self.assertEqual((status, body['code']), (409, 'QA_AGENT_INFERENCE_NOT_AUTHORIZED'))
        self.assertEqual(self.fake.calls, [])

    def test_local_history_and_key_lookup_do_not_poll_remote_or_rewrite_DB(self):
        self.active()
        key = str(uuid4())
        q = self.head.accept(key, '조회만 하는 질문')
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        calls = len(self.fake.calls)
        with running(self.server()) as port:
            for _ in range(2):
                for path in ['/api/questions', '/api/questions/' + q['question_id'],
                             '/api/question-requests/' + key, '/api/question-policy']:
                    self.assertEqual(self.request(port, path)[0], 200)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})

    def test_lost_run_ack_local_get_then_explicit_retry_reuses_original_intent(self):
        self.active()
        self.fake.calls.clear()
        with running(self.server(True)) as port:
            key = str(uuid4())
            q = self.accept(port, key=key)
            path = '/api/questions/' + q['question_id']
            self.fake.lose.add('/api/agent-runs')
            unknown = self.request(port, path + '/dispatch', {})[1]
            self.assertEqual(unknown['state'], 'confirmation_required')
            self.assertIsNone(unknown['agent_run_id'])
            self.assertEqual(self.request(port, '/api/question-requests/' + key)[1], unknown)
            self.assertEqual(self.request(port, path)[1], unknown)
            self.assertEqual(len(self.fake.posts('/api/agent-runs')), 1)
            self.fake.lose.clear()
            recovered = self.request(port, path + '/retry', {})[1]
            self.assertEqual(recovered['state'], 'waiting')
            self.assertEqual(self.fake.posts('/api/agent-runs')[0], self.fake.posts('/api/agent-runs')[1])
            runs = [v for path, v in self.fake.resources.items() if path.startswith('/api/agent-runs/') and v['mode'] == 'run']
            self.assertEqual(len(runs), 1)
            self.assertEqual(len(self.qa.question_list()['items']), 1)

    def test_known_run_observe_remains_available_after_new_inference_is_disabled(self):
        self.active()
        q = self.head.accept(str(uuid4()), '진행 중 질문')
        self.qa.dispatch(q['question_id'], self.fake)
        self.fake.finish()
        calls = len(self.fake.posts('/api/agent-runs'))
        with running(self.server(False)) as port:
            self.assertFalse(self.request(port, '/api/question-policy')[1]['question_ready'])
            status, answer = self.request(port, '/api/questions/' + q['question_id'] + '/observe', {})
            self.assertEqual((status, answer['state']), (200, 'complete'))
        self.assertEqual(len(self.fake.posts('/api/agent-runs')), calls)

    def test_no_evidence_and_failed_runs_are_distinct_from_empty_success(self):
        self.active()
        with running(self.server(True)) as port:
            for fail in [False, True]:
                q = self.accept(port)
                path = '/api/questions/' + q['question_id']
                self.request(port, path + '/dispatch', {})
                self.fake.finish(state='failed' if fail else 'succeeded', empty=not fail)
                result = self.request(port, path + '/observe', {})[1]
                if fail:
                    self.assertEqual(result['state'], 'failed')
                    self.assertNotIn('result', result)
                else:
                    self.assertEqual(result['state'], 'complete')
                    self.assertEqual(result['result']['outcome'], 'no_evidence')
                    self.assertEqual(result['result']['citations'], [])
                    self.assertTrue(result['result']['answer'])

    def test_changed_citation_fails_closed_and_does_not_display_an_answer(self):
        self.active()
        with running(self.server(True)) as port:
            q = self.accept(port)
            path = '/api/questions/' + q['question_id']
            self.request(port, path + '/dispatch', {})
            run = self.fake.finish()
            run['result']['citations'][0]['source']['text'] = 'invented source'
            response = self.request(port, path + '/observe', {})[1]
            self.assertEqual(response['state'], 'attention_required')
            self.assertEqual(response['error_code'], 'QA_AGENT_CITATION_INVALID')
            self.assertNotIn('result', response)

    def test_screen_routes_keep_auth_Host_and_Origin_checks_before_question_write(self):
        self.active()
        before = self.snapshot()
        calls = len(self.fake.calls)
        with running(self.server(True)) as port:
            for path in ['/api/questions', '/api/question-policy', '/api/question-requests/' + str(uuid4())]:
                self.assertEqual(self.request(port, path, auth=False)[0], 401)
            for fields, expected in [({'Host': 'attacker.example'}, 'HOST_NOT_ALLOWED'),
                                     ({'Origin': 'http://attacker.example'}, 'ORIGIN_NOT_ALLOWED')]:
                status, response = self.request(port, '/api/questions', {'question': '차단할 질문'}, str(uuid4()), headers=fields)
                self.assertEqual((status, response['code']), (403, expected))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.fake.calls), calls)

    def test_question_byte_limit_and_client_key_conflict_cannot_create_extra_questions(self):
        self.active()
        with running(self.server(True)) as port:
            key = str(uuid4())
            q = self.accept(port, '한' * 2730 + 'ab', key)
            status, response = self.request(port, '/api/questions', {'question': '다른 질문'}, key)
            self.assertEqual((status, response['code']), (409, 'QUESTION_CLIENT_KEY_CONFLICT'))
            status, response = self.request(port, '/api/questions', {'question': '한' * 2731}, str(uuid4()))
            self.assertEqual((status, response['code']), (413, 'QUESTION_TOO_LARGE'))
            self.assertEqual(self.request(port, '/api/question-requests/' + key)[1]['question_id'], q['question_id'])
            self.assertEqual(len(self.request(port, '/api/questions')[1]['items']), 1)
