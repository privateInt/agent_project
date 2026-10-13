"""A09 failures must never become a successful answer or a destructive recovery."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import acceptance as check


def uid(n):
    return str(uuid.UUID(int=n))


class API:
    url = 'http://127.0.0.1:16100'

    def __init__(self):
        model = {'deployment_id': uid(6), 'model_version_id': uid(7)}
        source = {'chunk_id': uid(8), 'text': 'PRIVATE_SOURCE', 'quality': {'warnings': []}}
        metadata = {'index_version_id': uid(9), 'embedding_space_id': uid(10)}
        self.run = {'agent_run_id': uid(1), 'agent_version_id': uid(2), 'request_id': uid(3),
                    'definition_sha256': 'agent-hash', 'tool_call_id': uid(4), 'state': 'succeeded',
                    'created_at': '2026-09-24T01:00:00Z', 'mode': 'run', 'input': {'question': 'PRIVATE_QUESTION'},
                    'invocation_key': uid(15), 'caller_context': {},
                    'result': {'outcome': 'answered', 'answer': 'PRIVATE_ANSWER [S1]', 'tool_call_id': uid(4),
                               'citations': [{'citation_id': 'S1', 'source': source}], 'selected_chunk_ids': [uid(8)],
                               'warnings': [], 'answer_truncated': False,
                               'identity': {'tool_version_id': uid(5), 'model': model, 'knowledge': metadata}}}
        self.steps = [{'step_id': s, 'state': 'succeeded'} for s in check.STEPS]
        self.version = {'agent_version_id': uid(2), 'definition_sha256': 'agent-hash',
                        'tool_version_id': uid(5), 'state': 'published', 'revision': 2, 'model': model}
        self.child = {'tool_call_id': uid(4), 'tool_version_id': uid(5), 'parent_agent_run_id': uid(1),
                      'parent_step_id': 'search', 'request_id': uid(3), 'state': 'succeeded',
                      'definition_sha256': 'tool-hash', 'result': {'metadata': metadata, 'data': {'results': [source]}}}
        self.tv = {'tool_version_id': uid(5), 'kind': 'knowledge_search_v1', 'definition_sha256': 'tool-hash'}
        self.routes = {'/api/agent-runs/' + uid(1): self.run, '/api/agent-runs/' + uid(1) + '/steps': {'items': self.steps},
                       '/api/agent-versions/' + uid(2): self.version, '/api/tool-calls/' + uid(4): self.child,
                       '/api/tool-versions/' + uid(5): self.tv,
                       '/api/status': {'ready': True, 'stage': 'A07', 'schema_version': 5}}

    def get(self, path):
        return copy.deepcopy(self.routes[path])

    def items(self, path):
        assert path == '/api/agent-runs'
        return [self.get('/api/agent-runs/' + uid(1))]


def output(value):
    return [{'data': {'main': [[{'json': value}]]}}]


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.api = API()

    def inspect(self):
        return check.inspect_run(self.api, uid(1))

    def session(self, folder, states=('success', 'success')):
        r = self.api.run
        r['caller_context'] = {'system': 'n8n', 'workflow_id': 'flow1', 'execution_id': 'request:' + uid(15), 'node_id': 'agent-call'}
        state = {'session_id': uid(16), 'agent_version_id': uid(2), 'definition_sha256': 'agent-hash',
                 'workflow_link_id': uid(17), 'published_version_id': 'v1'}
        request = {'body': {'request_id': uid(15), 'question': 'PRIVATE_QUESTION'}, 'receipt': {'agent_run_id': uid(1)}}
        paths = [folder / 'settings.json', folder / 'request.json']
        for p, value in zip(paths, [state, request]):
            p.write_text(json.dumps(value)); p.chmod(0o600)
        executions = []
        for i, status in enumerate(states, 1):
            answer = {**r['result'], **{k: r[k] for k in ('agent_run_id', 'agent_version_id', 'request_id', 'tool_call_id')}}
            run_data = {'Prepare request': output({'request': {'invocation_key': uid(15)}}),
                        'Run pointer': output({'agent_run_id': uid(1)})}
            if status == 'success':
                run_data['Answer and sources'] = output(answer)
            executions.append({'id': str(i), 'workflowId': 'flow1', 'status': status, 'data': {'resultData': {'runData': run_data}}})
        class Client:
            url = 'http://127.0.0.1:16200'
            def executions(self, workflow):
                return [{'id': e['id']} for e in executions]
            def request(self, method, path):
                assert method == 'GET'
                return copy.deepcopy(next(e for e in executions if path.endswith('/' + e['id'] + '?includeData=true')))
        return {'state': state, 'request': request, 'client': Client(),
                'workflow': {'id': 'flow1', 'active': True, 'activeVersionId': 'v1'}, 'link': {},
                'paths': paths, 'fingerprints': [check.digest(state), check.digest(request)]}, executions

    def test_valid_chain_and_private_digest_baseline(self):
        record, summary, _ = self.inspect()
        text = json.dumps([record, summary])
        for secret in ('PRIVATE_SOURCE', 'PRIVATE_QUESTION', 'PRIVATE_ANSWER'):
            self.assertNotIn(secret, text)
        self.assertEqual(summary['citation_count'], 1)

    def test_search_parent_mismatch(self):
        self.api.child['parent_agent_run_id'] = uid(99)
        with self.assertRaisesRegex(check.CheckError, 'SEARCH_CORRELATION_CHANGED'):
            self.inspect()

    def test_forged_source_with_same_chunk_id(self):
        self.api.run['result']['citations'][0]['source'] = {'chunk_id': uid(8), 'text': 'forged'}
        with self.assertRaisesRegex(check.CheckError, 'CITATION_SOURCE_CHANGED'):
            self.inspect()

    def test_missing_or_invalid_citation(self):
        for answer in ('no citation', '[S0]', '[S99]', '[S 1]', '[S1] [Sx]'):
            self.api.run['result']['answer'] = answer
            with self.subTest(answer=answer), self.assertRaises(check.CheckError):
                self.inspect()

    def test_model_identity_mismatch(self):
        self.api.run['result']['identity']['model'] = {'deployment_id': uid(99), 'model_version_id': uid(7)}
        with self.assertRaisesRegex(check.CheckError, 'MODEL_IDENTITY_CHANGED'):
            self.inspect()

    def test_no_evidence_not_counted_as_answered(self):
        result = self.api.run['result']
        result.update(outcome='no_evidence', answer='근거 부족', citations=[], selected_chunk_ids=[])
        result['identity']['model'] = {}
        self.assertEqual(self.inspect()[1]['outcome'], 'no_evidence')

    def test_failed_run_preserved_without_success(self):
        self.api.run.update(state='failed', error={'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize'})
        del self.api.run['result']; self.api.steps[-1]['state'] = 'failed'
        with patch.object(check, 'read_session', return_value=None):
            report, _ = check.collect(self.api, Path('/absent'), Path('/absent'))
        self.assertEqual(report['runs'][0]['state'], 'failed')
        self.assertEqual(report['mvp_acceptance'], 'incomplete')
        self.assertIn('operational_n8n_answer_delivery', report['pending'])

    def test_changed_terminal_answer_detected(self):
        first = {'runs': {uid(1): self.inspect()[0]}, 'n8n': {}}
        self.api.run['result']['answer'] = 'different answer [S1]'
        second = {'runs': {uid(1): self.inspect()[0]}, 'n8n': {}}
        self.assertEqual(check.comparison(first, second), ['runs'])

    def test_version_retirement_keeps_definition_comparable(self):
        before = self.inspect()[0]
        self.api.version.update(state='retired', revision=3)
        self.assertEqual(before, self.inspect()[0])

    def test_baseline_is_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p = check.baseline_path(Path(d) / 'baseline.json')
            value = {'schema': 1, 'runs': {uid(1): self.inspect()[0]}, 'n8n': {}}
            check.save_baseline(p, value); original = p.read_bytes()
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(check.load_baseline(p), value)
            with self.assertRaises(FileExistsError):
                check.save_baseline(p, {'runs': {'other': {}}})
            self.assertEqual(original, p.read_bytes())

    def test_baseline_symlink_and_project_target_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            alias = Path(d) / 'alias'; alias.symlink_to(Path(d) / 'missing')
            with self.assertRaisesRegex(check.CheckError, 'BASELINE_SYMLINK'):
                check.baseline_path(alias)
        with self.assertRaisesRegex(check.CheckError, 'BASELINE_MUST_BE_OUTSIDE_PROJECT'):
            check.baseline_path(check.ROOT / 'baseline.json')

    def test_n8n_guard_rejects_mutation_before_network(self):
        obj = object.__new__(check.N8nReader)
        with self.assertRaisesRegex(check.CheckError, 'READ_ONLY_BOUNDARY'):
            obj.request('POST', '/api/v1/workflows')

    def test_n8n_delivery_and_replay_agree_with_agent(self):
        with tempfile.TemporaryDirectory() as d:
            session, _ = self.session(Path(d))
            before = [p.read_bytes() for p in session['paths']]
            summary, records, rid = check.inspect_n8n(session, self.api)
            self.assertEqual(rid, uid(1)); self.assertTrue(summary['delivery_verified']); self.assertTrue(summary['replay_verified'])
            self.assertEqual(len(records['executions']), 2)
            self.assertEqual(before, [p.read_bytes() for p in session['paths']])

    def test_one_delivery_is_not_replay(self):
        with tempfile.TemporaryDirectory() as d:
            session, _ = self.session(Path(d), ('success',))
            summary, _, _ = check.inspect_n8n(session, self.api)
            self.assertTrue(summary['delivery_verified']); self.assertFalse(summary['replay_verified'])

    def test_n8n_corrupt_delivery_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            session, executions = self.session(Path(d))
            executions[0]['data']['resultData']['runData']['Answer and sources'] = output({'answer': 'forged'})
            with self.assertRaisesRegex(check.CheckError, 'N8N_DELIVERY_CHANGED'):
                check.inspect_n8n(session, self.api)

    def test_duplicate_agent_runs_for_same_n8n_request(self):
        with tempfile.TemporaryDirectory() as d:
            session, executions = self.session(Path(d))
            executions[1]['data']['resultData']['runData']['Run pointer'] = output({'agent_run_id': uid(99)})
            with self.assertRaisesRegex(check.CheckError, 'DUPLICATE_AGENT_RUNS_FOR_REQUEST'):
                check.inspect_n8n(session, self.api)

    def test_n8n_original_failure_is_not_delivery(self):
        with tempfile.TemporaryDirectory() as d:
            session, _ = self.session(Path(d), ('error',))
            self.api.run.update(state='failed', error={'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize'})
            summary, _, _ = check.inspect_n8n(session, self.api)
            self.assertFalse(summary['delivery_verified']); self.assertFalse(summary['replay_verified'])
            self.assertEqual(summary['error']['code'], 'ANSWER_REFERENCE_INVALID')

    def test_missing_pruned_execution_cannot_compare_equal(self):
        with tempfile.TemporaryDirectory() as d:
            session, executions = self.session(Path(d))
            before = check.inspect_n8n(session, self.api)[1]
            executions.pop()
            after = check.inspect_n8n(session, self.api)[1]
            self.assertEqual(check.comparison({'n8n': before}, {'n8n': after}), ['n8n'])


if __name__ == '__main__':
    unittest.main()
