"""Preparation recovery and selection invariants; no network/model calls."""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('live_check', Path(__file__).resolve().parents[1] / 'ops/live_check.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def args(**overrides):
    values = dict(agent_url='http://localhost:16100', container_agent_url='http://agent-gateway:8000',
        question='연구 범위는?', non_interactive=True, new_agent=True, agent_version_id=None,
        tool_version_id='tool-v', model_connection_version_id=None, deployment_id='chat-d', name=None, publish_tool=False)
    return argparse.Namespace(**(values | overrides))


class API:
    def __init__(self, draft=False):
        self.model = dict(connection_version_id='model-c', deployment_id='chat-d', model_version_id='chat-v')
        self.tool = dict(tool_version_id='tool-v', revision=1, state='draft' if draft else 'published', kind='knowledge_search_v1', version=1,
            binding=dict(connection_version_id='knowledge-c', knowledge=dict(model_connection_version_id='model-c', index_id='index', index_version_id='index-v', embedding_space_id='es_fixed', embedding_deployment_id='embed-d')))
        self.agent = None; self.calls = []; self.effects = {}; self.lose = None; self.drift = False

    def get(self, route):
        if route == '/api/status': return dict(ready=True, stage='A07', schema_version=5)
        if route.startswith('/api/tools?'): return dict(items=[dict(tool_id='tool', enabled=True, display_name='내 검색')], next_cursor='')
        if route == '/api/tools/tool': return dict(display_name='내 검색', versions=[copy.deepcopy(self.tool)])
        if route == '/api/tool-versions/tool-v': return copy.deepcopy(self.tool)
        if route == '/api/agent-versions/agent-v': return copy.deepcopy(self.agent)
        if route == '/api/agents/agent': return dict(enabled=True, display_name='기존 Agent')
        raise AssertionError(route)

    def inspect(self, route, body):
        if route == '/api/knowledge/inspect':
            binding = copy.deepcopy(self.tool['binding'])
            if self.drift: binding['knowledge']['embedding_space_id'] = 'es_changed'
            return dict(binding=binding, index_name='논문', chunk_count=3)
        if body['action'] == 'deployments':
            return dict(items=[dict(deployment_id='chat-d', model_version_id='chat-v', display_name='답변 모델')], next_cursor='')
        return dict(model=self.model, identity=dict(display_name='답변 모델'))

    def request(self, method, route, body, expected, key):
        self.calls.append((route, copy.deepcopy(body), key))
        if key not in self.effects:
            if route.endswith('/publish'):
                self.tool['state'] = 'published'; result = copy.deepcopy(self.tool)
            elif route == '/api/agents': result = dict(agent_id='agent', revision=1)
            elif route == '/api/agents/agent/versions':
                self.agent = dict(agent_version_id='agent-v', agent_id='agent', state='draft', model=body['model'], tool_version_id=body['tool_version_id'])
                result = self.agent
            else: raise AssertionError(route)
            self.effects[key] = result
        if self.lose == route:
            self.lose = None
            raise ValueError('response lost after commit')
        return copy.deepcopy(self.effects[key])


class LivePreparation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.output = contextlib.redirect_stdout(io.StringIO()); self.output.__enter__(); self.addCleanup(self.output.__exit__, None, None, None)

    def test_uncertain_create_replays_original_key_and_same_body(self):
        api = API(); api.lose = '/api/agents'
        with self.assertRaisesRegex(ValueError, 'response lost'): m.prepare(args(), api, self.directory)
        first = api.calls[0]
        result = m.prepare(args(), api, self.directory)
        self.assertEqual(first, api.calls[1]); self.assertEqual(len(api.effects), 2)
        self.assertEqual(result['mode'], 'test'); self.assertTrue(result['created_agent'])
        self.assertFalse(any(route.endswith('/publish') for route, _, _ in api.calls))
        # Repeating the original successful prepare command is idempotent.
        m.prepare(args(), api, self.directory); self.assertEqual(len(api.effects), 2)
        self.assertEqual((self.directory / 'settings.json').stat().st_mode & 0o777, 0o600)

    def test_resume_changed_selection_rejected_before_mutation(self):
        api = API(); api.lose = '/api/agents'
        with self.assertRaises(ValueError): m.prepare(args(), api, self.directory)
        count = len(api.calls)
        for changed in [dict(question='다른 질문'), dict(deployment_id='changed'), dict(tool_version_id='changed'), dict(name='다른 이름'), dict(new_agent=False, agent_version_id='another')]:
            with self.assertRaises(ValueError): m.prepare(args(**changed), api, self.directory)
        self.assertEqual(count, len(api.calls))

    def test_completed_session_also_freezes_tool_and_model(self):
        api = API(); m.prepare(args(), api, self.directory)
        with self.assertRaises(ValueError): m.prepare(args(model_connection_version_id='another'), api, self.directory)
        self.assertEqual(len(api.effects), 2)

    def test_resource_drift_does_not_create_or_publish(self):
        api = API(draft=True); api.drift = True
        with self.assertRaisesRegex(ValueError, 'embedding_space_id'): m.prepare(args(publish_tool=True), api, self.directory)
        self.assertEqual(api.calls, [])

    def test_draft_tool_requires_explicit_publication(self):
        api = API(draft=True)
        with self.assertRaisesRegex(ValueError, '--publish-tool'): m.prepare(args(), api, self.directory)
        self.assertEqual(api.calls, [])
        m.prepare(args(publish_tool=True), api, self.directory)
        self.assertEqual([route for route, _, _ in api.calls], ['/api/tool-versions/tool-v/publish', '/api/agents', '/api/agents/agent/versions'])
        m.prepare(args(publish_tool=True, tool_version_id='내 검색'), api, self.directory)
        self.assertEqual(len(api.calls), 3)

    def test_existing_agent_never_creates_or_publishes(self):
        api = API(); api.agent = dict(agent_version_id='agent-v', agent_id='agent', state='published', model=api.model, tool_version_id='tool-v')
        result = m.prepare(args(new_agent=False, agent_version_id='agent-v', name=None), api, self.directory)
        self.assertEqual(result['mode'], 'run'); self.assertEqual(api.calls, [])

    def test_pagination_repeated_cursor_fails(self):
        class Loop:
            def get(self, route): return dict(items=[], next_cursor='repeat')
        with self.assertRaises(ValueError): m.all_items(Loop(), '/api/tools')

    def test_same_embedding_deployment_excluded_and_resolve_rejected(self):
        api = API()
        self.assertEqual(m.model_choices(api, 'model-c', 'chat-d'), [])
        api.tool['binding']['knowledge']['embedding_deployment_id'] = 'chat-d'
        with self.assertRaisesRegex(ValueError, '임베딩 배포'):
            m.inspect_resources(api, api.tool, api.model)
        self.assertEqual(api.calls, [])

    def test_diagnose_only_reads_records_and_omits_answer_and_raw_errors(self):
        api = API(); settings = m.prepare(args(), api, self.directory)
        run_id, call_id = str(uuid.uuid4()), str(uuid.uuid4())
        m.save(self.directory / 'accepted.json', {'agent_run_id': run_id})
        calls = []
        class Reader:
            def get(self, route):
                calls.append(route)
                if route.endswith('/steps'): return {'items': [{'step_id':'generate','state':'failed','detail':{'private':'omit'}}]}
                if route.startswith('/api/tool-calls/'):
                    return {'tool_call_id':call_id,'parent_agent_run_id':run_id,'state':'succeeded','result':{'text':'private document'}}
                return {'agent_run_id':run_id,'agent_version_id':'agent-v','request_id':'original','state':'failed','tool_call_id':call_id,
                    'error':{'code':'MODEL_NOT_READY','stage':'generate','message':'private unstructured diagnostic'}}
        with patch.object(m, 'API', return_value=Reader()): report = m.diagnose(self.directory, 'private-token')
        self.assertEqual(len(calls), 3); self.assertEqual(report['error'], {'code':'MODEL_NOT_READY','stage':'generate'})
        self.assertEqual(report['search']['state'], 'succeeded'); self.assertFalse(report['inference_executed'])
        text = (self.directory / 'diagnostic.json').read_text()
        for value in ['private-token', 'private document', 'private unstructured diagnostic']: self.assertNotIn(value, text)
        self.assertNotIn('run', report); self.assertEqual(report['diagnostic'], 'records_read')

    def test_private_session_file_rejects_readable_or_symlink(self):
        path = self.directory / 'state.json'; path.write_text('{}'); path.chmod(0o644)
        with self.assertRaises(ValueError): m.private_read(path)
        link = self.directory / 'link.json'; link.symlink_to(path)
        with self.assertRaises(ValueError): m.private_read(link)

    def test_unsafe_origins_rejected(self):
        for value in ['http://user:pass@host', 'https://host/path', 'https://host?q=1', 'https://host\n']:
            with self.assertRaises(ValueError): m.origin(value)
        self.assertEqual(m.origin('https://host:16100/'), 'https://host:16100')

if __name__ == '__main__': unittest.main()
