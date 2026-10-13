"""Prompt-only candidates must preserve existing production versions and effects."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import citation_trial as trial


def uid(n): return f'00000000-0000-4000-8000-{n:012d}'


class API:
    url = 'http://127.0.0.1:16100'

    def __init__(self):
        self.parent = {'agent_id': uid(1), 'enabled': True, 'revision': 2}
        self.version = {'agent_id': uid(1), 'agent_version_id': uid(2), 'definition_sha256': 'original',
                        'state': 'published', 'template_id': 'knowledge_qa_v1', 'template_version': 1,
                        'tool_version_id': uid(4), 'model': {'connection_version_id': uid(5),
                        'deployment_id': uid(6), 'model_version_id': uid(7)},
                        'prompt': 'PRIVATE_EXISTING_INSTRUCTION', 'context_max_chars': 8000,
                        'max_tokens': 512, 'temperature': 0.2, 'timeout_ms': 300000}
        self.source = {'agent_run_id': uid(3), 'agent_version_id': uid(2), 'definition_sha256': 'original',
                       'state': 'failed', 'input': {'question': 'PRIVATE_QUESTION', 'filters': {'kinds': ['text']}},
                       'error': {'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize'}}
        diagnostic = {'schema': 1, 'scope': 'reference_syntax_only', 'status': 'invalid', 'finish_reason': 'stop',
                      'issues': ['MISSING_REFERENCE'], 'recognized_count': 0, 'selected_source_count': 5,
                      'malformed_count': 0, 'out_of_range_count': 0, 'max_tokens': 512, 'completion_tokens': 175}
        self.steps = [{'step_id': s, 'state': 'succeeded', 'detail': {}} for s in ('validate','search','context','generate','finalize')]
        self.steps[2]['detail']['selected_chunk_ids'] = ['PRIVATE_SOURCE']*5
        self.steps[3]['detail']['reference_diagnostics'] = diagnostic
        self.steps[-1]['state'] = 'failed'
        self.routes = {'/api/status': {'ready': True, 'stage': 'A07', 'schema_version': 5},
                       '/api/agents/' + uid(1): self.parent, '/api/agent-versions/' + uid(2): self.version,
                       '/api/agent-runs/' + uid(3): self.source, '/api/agent-runs/' + uid(3) + '/steps': {'items': self.steps}}
        self.posts = [];self.saved = {};self.effects = {'draft': 0, 'run': 0};self.lose = set()
        self.outcome = 'answered';self.fail_answer = False;self.pending = False;self.truncated = False

    def get(self, route): return copy.deepcopy(self.routes[route])

    def request(self, method, route, body, expected, key):
        assert method == 'POST'
        assert route in ('/api/agents/' + uid(1) + '/versions', '/api/agent-runs')
        request = (method, route, copy.deepcopy(body), expected, key);self.posts.append(request)
        if key in self.saved:
            original, result = self.saved[key]
            assert original == request
            return copy.deepcopy(result)
        if route.endswith('/versions'):
            assert expected == 201 and body['expected_revision'] == self.parent['revision']
            result = {'agent_id': uid(1), 'agent_version_id': uid(8), 'state': 'draft', 'definition_sha256': 'candidate',
                      **{k: copy.deepcopy(body[k]) for k in trial.DEFINITION}}
            self.routes['/api/agent-versions/' + uid(8)] = result
            self.parent['revision'] += 1;self.effects['draft'] += 1
        else:
            assert expected == 202 and body['mode'] == 'test' and body['agent_version_id'] == uid(8)
            result = {'agent_run_id': uid(9), 'agent_version_id': uid(8), 'state': 'accepted'}
            run = {'agent_run_id': uid(9), 'definition_sha256': 'candidate', 'invocation_key': key,
                   'request_id': uid(10), 'tool_call_id': uid(11), **copy.deepcopy(body),
                   'state': 'running' if self.pending else 'failed' if self.fail_answer else 'succeeded'}
            if self.fail_answer:run['error'] = {'code':'ANSWER_REFERENCE_INVALID','stage':'finalize'}
            else:run['result'] = {'outcome': self.outcome, 'answer': 'PRIVATE_ANSWER [S1]',
                                 'citations': [{'source': 'PRIVATE_SOURCE'}] if self.outcome == 'answered' else [],
                                 'answer_truncated': self.truncated}
            steps = copy.deepcopy(self.steps)
            if not self.fail_answer and not self.pending:
                steps[-1]['state'] = 'succeeded'
                steps[3]['detail']['reference_diagnostics'].update(status='valid', issues=[], recognized_count=1)
            self.routes['/api/agent-runs/' + uid(9)] = run
            self.routes['/api/agent-runs/' + uid(9) + '/steps'] = {'items': steps}
            self.effects['run'] += 1
        self.saved[key] = (request, copy.deepcopy(result))
        if route in self.lose:
            self.lose.remove(route)
            raise ValueError('simulated lost POST response')
        return copy.deepcopy(result)


class CitationTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name);self.directory.chmod(0o700);self.api = API()

    def prepare(self):return trial.prepare(self.api, self.directory, uid(3))
    def run_trial(self, submit=True):return trial.trial(self.api, self.directory, submit=submit, wait_seconds=0)

    def test_only_prompt_changes_and_original_input_filters_records_are_preserved(self):
        original = copy.deepcopy((self.api.version, self.api.source, self.api.steps))
        result = self.prepare()
        self.assertEqual(result['changed_definition_fields'], ['prompt']);self.assertFalse(result['inference_executed'])
        candidate = self.api.get('/api/agent-versions/' + uid(8))
        self.assertEqual([k for k in trial.DEFINITION if candidate[k] != self.api.version[k]], ['prompt'])
        self.assertTrue(candidate['prompt'].startswith(self.api.version['prompt']))
        result = self.run_trial();self.assertEqual(result['result'], 'passed')
        self.assertEqual(self.api.get('/api/agent-runs/' + uid(9))['input'], self.api.source['input'])
        self.assertEqual(original, (self.api.version, self.api.source, self.api.steps))
        self.assertFalse(result['workflow_changed']);self.assertFalse(result['agent_published_automatically'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual((self.directory / 'preparation.json').stat().st_mode & 0o777, 0o600)

    def test_repeated_prepare_test_and_lookup_do_not_create_new_effects(self):
        self.prepare();self.prepare();first = self.run_trial();before = (self.directory/'preparation.json').read_bytes()
        second = self.run_trial();third = self.run_trial(submit=False)
        self.assertEqual(first['agent_run_id'], second['agent_run_id']);self.assertEqual(first['agent_run_id'], third['agent_run_id'])
        self.assertFalse(second['execution_request_submitted']);self.assertFalse(third['execution_request_submitted'])
        self.assertEqual(self.api.effects, {'draft': 1, 'run': 1});self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(before, (self.directory/'preparation.json').read_bytes())

    def test_lost_draft_response_reuses_key_despite_changed_parent_revision(self):
        self.api.lose.add('/api/agents/' + uid(1) + '/versions')
        with self.assertRaises(ValueError):self.prepare()
        self.prepare()
        self.assertEqual(self.api.posts[0], self.api.posts[1]);self.assertEqual(self.api.effects['draft'], 1)

    def test_lost_execution_response_reuses_exact_key_body_and_effect(self):
        self.prepare();self.api.lose.add('/api/agent-runs')
        with self.assertRaises(ValueError):self.run_trial()
        before = len(self.api.posts)
        with self.assertRaises(ValueError):self.run_trial(submit=False)
        self.assertEqual(before, len(self.api.posts))
        self.assertEqual(self.run_trial()['result'], 'passed')
        self.assertEqual(self.api.posts[-1], self.api.posts[-2]);self.assertEqual(self.api.effects['run'], 1)

    def test_failed_trial_remains_failed_and_never_regenerates(self):
        self.api.fail_answer = True;self.prepare()
        for _ in range(2):
            report = self.run_trial();self.assertEqual(report['result'], 'failed')
            self.assertEqual(report['error']['code'], 'ANSWER_REFERENCE_INVALID')
        self.assertEqual(self.api.effects['run'], 1)

    def test_no_evidence_and_truncation_are_not_citation_success(self):
        self.api.outcome = 'no_evidence';self.prepare()
        self.assertEqual(self.run_trial()['result'], 'inconclusive')
        self.api.routes['/api/agent-runs/' + uid(9)]['result'].update(outcome='answered', citations=[{}], answer_truncated=True)
        self.assertEqual(self.run_trial(submit=False)['result'], 'inconclusive')

    def test_pending_does_not_submit_more_executions(self):
        self.api.pending = True;self.prepare()
        self.assertEqual(self.run_trial()['result'], 'pending')
        self.assertEqual(self.run_trial(submit=False)['result'], 'pending')
        self.assertEqual(self.api.effects['run'], 1)

    def test_other_diagnostic_rejected_before_any_post(self):
        self.api.steps[3]['detail']['reference_diagnostics']['issues'] = ['REFERENCE_OUT_OF_RANGE']
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(self.api.posts, [])

    def test_changed_source_and_candidate_rejected(self):
        self.prepare()
        with self.assertRaises(ValueError):trial.prepare(self.api, self.directory, uid(99))
        self.api.routes['/api/agent-versions/' + uid(8)]['temperature'] = 0
        with self.assertRaises(ValueError):self.run_trial()
        self.assertEqual(self.api.effects['run'], 0)

    def test_lookup_before_submission_does_not_write_or_infer(self):
        self.prepare();before = (self.directory/'preparation.json').read_bytes()
        with self.assertRaises(ValueError):self.run_trial(submit=False)
        self.assertEqual(before, (self.directory/'preparation.json').read_bytes());self.assertEqual(self.api.effects['run'], 0)

    def test_long_prompt_and_duplicate_guidance_fail_before_post(self):
        for text in ('가'*1300, trial.GUIDANCE):
            self.api.version['prompt'] = text
            with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(self.api.posts, [])

    def test_changed_execution_binding_is_rejected(self):
        self.prepare();self.run_trial()
        self.api.routes['/api/agent-runs/' + uid(9)]['input']['question'] = 'changed'
        with self.assertRaises(ValueError):self.run_trial(submit=False)

    def test_separate_cli_processes_use_real_http_and_reuse_persisted_trial(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import subprocess
        import threading
        api = self.api;token = 'fixture-token-0123456789';seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):pass
            def handle_request(self, method):
                seen.append((method, self.path))
                if self.headers.get('Authorization') != 'Bearer ' + token:
                    self.send_error(401);return
                if method == 'GET':
                    status = 200;value = api.get(self.path)
                else:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    status = 201 if self.path.endswith('/versions') else 202
                    value = api.request(method, self.path, body, status, self.headers['Idempotency-Key'])
                raw = json.dumps(value).encode()
                self.send_response(status);self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('X-Request-ID', self.headers['X-Request-ID'])
                self.end_headers();self.wfile.write(raw)
            def do_GET(self):self.handle_request('GET')
            def do_POST(self):self.handle_request('POST')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            token_file = self.directory / 'api-token';token_file.write_text(token);token_file.chmod(0o600)
            state = self.directory / 'cli-state'
            base = [sys.executable, '-B', str(Path(trial.__file__).resolve())]
            options = ['--url', 'http://127.0.0.1:' + str(server.server_port), '--token-file', str(token_file),
                       '--state-dir', str(state), '--wait-seconds', '0']
            for action in ('prepare', 'test', 'test', 'lookup'):
                args = base + [action] + options + (['--from-run-id', uid(3)] if action == 'prepare' else [])
                result = subprocess.run(args, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertEqual(value['result'], 'prepared' if action == 'prepare' else 'passed')
                self.assertNotIn('PRIVATE', result.stdout);self.assertNotIn(token, result.stdout)
            self.assertEqual(api.effects, {'draft': 1, 'run': 1})
            self.assertEqual(sum(method == 'POST' for method, _ in seen), 2)
            self.assertEqual((state / 'preparation.json').stat().st_mode & 0o777, 0o600)
        finally:
            server.shutdown();thread.join();server.server_close()


if __name__ == '__main__':unittest.main()
