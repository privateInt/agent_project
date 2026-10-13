"""Real local HTTP fixtures exercise the fixed adapter and pinned transport.

Only DNS resolution is substituted to reach the loopback test server. These are
not Model GPU/Knowledge DB acceptance tests.
"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from agent_runtime import http_tools, knowledge_tools as kt
from agent_runtime.tools import ToolError, execute


def uid(n):
    return f'10000000-0000-4000-8000-{n:012d}'


def profile():
    return {'deployment_id': uid(3), 'model_version_id': uid(4), 'embedding_space_id': 'es_'+'a'*64,
            'dimension': 1024, 'normalization': 'l2', 'capability': 'embedding', 'status': 'ready',
            'truncation': 'reject', 'max_input_tokens': 8192, 'max_input_bytes': 131072}


def binding():
    return {'connection_version_id': uid(10), 'knowledge': {
        'model_connection_version_id': uid(11), 'index_id': uid(1), 'index_version_id': uid(2),
        'embedding_deployment_id': uid(3), 'embedding_space_id': 'es_'+'a'*64, 'top_k': 5, 'candidate_k': 5}}


def hit():
    return {'chunk_id': 'b'*64, 'source_document_id': uid(8), 'canonical_id': uid(9), 'kind': 'text',
            'rank': 1, 'retrieval_rank': 1, 'retrieval_score': 0.91, 'text': 'AI 엔지니어는 데이터를 이해하고 모델을 검증합니다.',
            'source_spans': [{'block_id': 'p1', 'metadata': {'location': {'page': 3}}}],
            'quality': {'warnings': [{'code': 'review_table'}]}, 'extra_future_field': {'kept': True}}


class Context:
    def __init__(self, seconds=4): self.deadline = time.monotonic()+seconds
    def time_remaining(self): return max(0, self.deadline-time.monotonic())
    def is_active(self): return self.time_remaining() > 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self): self.reply()
    def do_POST(self): self.reply()
    def reply(self):
        f = self.server.fixture
        body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if 'Content-Length' in self.headers else None
        path = urlsplit(self.path).path
        f['calls'].append((self.command, self.path, body))
        status = f.get('http_status', {}).get(path, 200)
        if path.endswith('/embeddings') and f.get('delay'): time.sleep(f['delay'])
        if path == '/api/indexes': result = {'items': [f['index']], 'next_cursor': f.get('next_cursor', '')}
        elif path == '/api/indexes/'+uid(1): result = f['index']
        elif path == '/api/index-versions/'+uid(2): result = f['version']
        elif path.endswith('/embedding-profile'): result = f['profile']
        elif path.endswith('/reranker-profile'): result = f['reranker']
        elif path.endswith('/embeddings'): result = f['receipt']
        elif path.endswith('/search'):
            result = {'index_id': uid(1), 'index_version_id': uid(2), 'embedding_space_id': 'es_'+'a'*64,
                      'top_k': body['top_k'], 'candidate_k': body['candidate_k'], 'candidate_count': len(f['hits']),
                      'filters': {k: sorted(v) for k, v in body['filters'].items()}, 'returned_count': len(f['hits']),
                      'results': deepcopy(f['hits']), 'rerank': {'requested': False, 'applied': False, 'status': 'disabled'}}
            if body.get('rerank'):
                result['rerank'] = {'requested': True, 'applied': bool(f['hits']), 'status': 'applied' if f['hits'] else 'skipped_no_candidates',
                                    'deployment_id': uid(20), 'reranker_profile_id': 'rp_'+'d'*64}
                for h in result['results']: h['rerank_score'] = -1.1
            result.update(f.get('search_override', {}))
            f['search_result'] = deepcopy(result)
        else: status, result = 404, {}
        raw = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        if status == 302: self.send_header('Location', '/not-followed')
        self.end_headers()
        try: self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError): pass


class KnowledgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def setUp(self):
        p = profile()
        self.f = {'calls': [], 'index': {'index_id': uid(1), 'name': 'test1', 'state': 'active', 'active_version_id': uid(2)},
                  'version': {'index_id': uid(1), 'index_version_id': uid(2), 'state': 'ready', 'reference_state': 'held',
                              'chunk_count': 2, 'indexed_count': 2, 'embedding_profile': deepcopy(p)}, 'profile': deepcopy(p),
                  'receipt': {'profile': deepcopy(p), 'role': 'query', 'data': [{'id': 'question-1', 'index': 0, 'input_tokens': 6, 'embedding': [1.]+[0.]*1023}]},
                  'hits': [hit()], 'reranker': {'deployment_id': uid(20), 'reranker_profile_id': 'rp_'+'d'*64, 'capability': 'reranker',
                          'status': 'ready', 'truncation': 'reject', 'score_kind': 'logit', 'score_direction': 'higher_is_better', 'max_candidates': 100}}
        self.server.fixture = self.f
        self.b = binding()
        self.connections = {k: {'connection_version_id': uid(n), 'base_url': f'http://{k}.example.test:{self.server.server_port}', 'auth_type': 'none'} for k, n in [('knowledge', 10), ('model', 11)]}
        self.dns = patch.object(http_tools, 'resolve', return_value='127.0.0.1'); self.dns.start(); self.addCleanup(self.dns.stop)
    def run_tool(self, args=None, seconds=4):
        return execute('knowledge_search_v1', self.b, args or {'query': '질문'}, self.connections, Context(seconds))
    def failed(self, code, stage, args=None, seconds=4):
        with self.assertRaises(ToolError) as caught: self.run_tool(args, seconds)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.result['stage'], stage)
        self.assertIsNone(caught.exception.result['data'])
        return caught.exception
    def inspection(self, action, **extra):
        return kt.inspect({'action': action, 'connection_version_id': uid(10), 'model_connection_version_id': uid(11), **extra}, self.connections['knowledge'], self.connections['model'], Context())
    def test_discovery_is_read_only_and_resolves_pinned_ids(self):
        page = self.inspection('indexes'); self.assertTrue(page['items'][0]['selectable'])
        out = self.inspection('resolve', index_id=uid(1)); self.assertEqual(out['binding'], self.b)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.f['calls']))
    def test_opaque_knowledge_cursor_is_preserved(self):
        # Knowledge uses a URL-safe base64 cursor, not an Agent UUID cursor.
        token = 'eyJ2IjoxLCJzY29wZSI6ImluZGV4ZXMifQ'
        self.f['next_cursor'] = token
        page = self.inspection('indexes')
        self.assertEqual(page['next_cursor'], token)
        self.inspection('indexes', cursor=token)
        self.assertIn('cursor='+token, self.f['calls'][-1][1])
        with self.assertRaisesRegex(ToolError, 'TOOL_INPUT_INVALID'):
            self.inspection('indexes', cursor='x&other=1')

    def test_raw_question_and_complete_provenance_are_preserved(self):
        query = '  AI 엔지니어의 역량은?\n'
        out = self.run_tool({'query': query})
        self.assertEqual(out['data'], self.f['search_result'])
        self.assertEqual(out['data']['results'][0], hit())
        posts = [(p, b) for m, p, b in self.f['calls'] if m == 'POST']
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][1]['inputs'], [{'id': 'question-1', 'text': query}])
        self.assertEqual(posts[0][1]['role'], 'query')
        self.assertNotIn('query_text', posts[1][1]); self.assertNotIn('rerank', posts[1][1])
        self.assertNotIn('query_embedding', json.dumps(out)); self.assertEqual(out['metadata']['embedding_calls'], 1)
    def test_new_active_index_does_not_change_saved_binding(self):
        self.f['index']['active_version_id'] = uid(99)
        self.run_tool()
        self.assertIn(('GET', '/api/index-versions/'+uid(2), None), self.f['calls'])
        self.assertFalse(any(uid(99) in path for _, path, _ in self.f['calls']))
    def test_index_not_held_stops_before_model_calls(self):
        self.f['version']['reference_state'] = 'released'
        self.failed('KNOWLEDGE_NOT_READY', 'index')
        self.assertEqual(len(self.f['calls']), 2)
    def test_same_dimension_wrong_space_stops_before_embedding(self):
        self.f['profile']['embedding_space_id'] = 'es_'+'c'*64
        self.failed('KNOWLEDGE_PROFILE_MISMATCH', 'embedding_profile')
        self.assertFalse(any(m == 'POST' for m, _, _ in self.f['calls']))
    def test_receipt_identity_role_and_tokens_stop_before_search(self):
        baseline = deepcopy(self.f['receipt'])
        for mutate in [lambda r: r.update(role='document'), lambda r: r['data'][0].update(index=1),
                       lambda r: r['data'][0].update(id='other'), lambda r: r['data'][0].update(input_tokens=0),
                       lambda r: r['profile'].update(model_version_id=uid(98))]:
            self.f['receipt'] = deepcopy(baseline); self.f['calls'] = []; mutate(self.f['receipt'])
            self.failed('TOOL_RESPONSE_INVALID', 'embedding')
            self.assertFalse(any(p.endswith('/search') for _, p, _ in self.f['calls']))
    def test_bad_vectors_are_rejected_without_repair_or_search(self):
        for vector in [[0.]*1024, [2.]+[0.]*1023, [True]+[0.]*1023, [1.]+[0.]*1022, [float('nan')]+[0.]*1023]:
            self.f['receipt']['data'][0]['embedding'] = vector; self.f['calls'] = []
            self.failed('TOOL_RESPONSE_INVALID', 'embedding')
            self.assertFalse(any(p.endswith('/search') for _, p, _ in self.f['calls']))
    def test_http_failure_does_not_retry_or_search(self):
        path = '/api/deployments/'+uid(3)+'/embeddings'
        for status in [503, 302, 202]:
            self.f['http_status'] = {path: status}; self.f['calls'] = []
            self.failed('TOOL_RESPONSE_INVALID' if status == 202 else 'TOOL_HTTP_ERROR', 'embedding')
            self.assertEqual(sum(m == 'POST' for m, _, _ in self.f['calls']), 1)
    def test_timeout_stops_before_search_and_keeps_error_stage(self):
        self.f['delay'] = .15
        self.failed('TOOL_TIMEOUT', 'embedding', seconds=.07)
        self.assertFalse(any(p.endswith('/search') for _, p, _ in self.f['calls']))
        time.sleep(.16)  # drain the local fixture before the next test
    def test_input_validation_happens_before_any_http(self):
        for args in [{'query': ' '}, {'query': '가'*3000}, {'query': 'a\0b'}, {'query': 'x', 'url': 'http://host'},
                     {'query': 'x', 'top_k': True}, {'query': 'x', 'top_k': 7, 'candidate_k': 5},
                     {'query': 'x', 'filters': {'kinds': []}}, {'query': 'x', 'filters': {'kinds': ['text', 'text']}}]:
            self.failed('TOOL_INPUT_INVALID', 'input', args)
        self.assertEqual(self.f['calls'], [])
    def test_connection_snapshot_mismatch_stops_before_http(self):
        self.connections['model']['connection_version_id'] = uid(50)
        self.failed('TOOL_INPUT_INVALID', 'input'); self.assertEqual(self.f['calls'], [])
    def test_filters_and_result_identity_are_validated(self):
        out = self.run_tool({'query': 'x', 'top_k': 8, 'filters': {'kinds': ['text', 'table']}})
        self.assertEqual(out['data']['candidate_k'], 8)
        self.assertEqual(out['data']['filters']['kinds'], ['table', 'text'])
        self.f['search_override'] = {'index_version_id': uid(90)}
        self.failed('TOOL_RESPONSE_INVALID', 'search')
    def test_out_of_scope_result_is_rejected(self):
        self.failed('TOOL_RESPONSE_INVALID', 'search', {'query': 'x', 'filters': {'kinds': ['table']}})
    def test_empty_results_are_successful(self):
        self.f['hits'] = []
        self.assertEqual(self.run_tool()['data']['results'], [])
    def test_rerank_is_explicit_and_never_silently_falls_back(self):
        out = self.inspection('resolve', index_id=uid(1), rerank_deployment_id=uid(20)); self.b = out['binding']
        self.f['calls'] = []; result = self.run_tool({'query': ' 원문 질문 '})
        self.assertTrue(result['data']['rerank']['applied'])
        body = self.f['calls'][-1][2]; self.assertEqual(body['query_text'], ' 원문 질문 ')
        self.assertEqual(body['rerank']['expected_reranker_profile_id'], 'rp_'+'d'*64)
        self.f['search_override'] = {'rerank': {'requested': False, 'applied': False, 'status': 'disabled'}}
        self.failed('TOOL_RESPONSE_INVALID', 'search')
    def test_rerank_profile_mismatch_stops_before_inference(self):
        self.b['knowledge']['rerank'] = {'deployment_id': uid(20), 'expected_reranker_profile_id': 'rp_'+'c'*64}
        self.failed('KNOWLEDGE_PROFILE_MISMATCH', 'rerank_profile')
        self.assertFalse(any(m == 'POST' for m, _, _ in self.f['calls']))


if __name__ == '__main__': unittest.main()
