import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

spec = importlib.util.spec_from_file_location('prepare_tool', Path(__file__).resolve().parents[1] / 'ops/prepare_knowledge_tool_test.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
INDEX = 'e011832c-ecef-491c-a545-ddf16bb320dd'
VERSION = '0464117d-17a2-4696-a681-1be428671ac4'
DEPLOYMENT = '44247297-5184-4a36-9225-7791c80d3add'
SPACE = 'es_' + 'a' * 64


def args(**kw):
    return SimpleNamespace(**dict({'knowledge_url': 'http://host:15100', 'tool_base_url': 'http://knowledge-gateway:8000', 'index_name': 'test1', 'index_id': '', 'index_version_id': '', 'question': 'AI 역량은 무엇인가요?', 'top_k': 5, 'check_search': False}, **kw))


class Fixture:
    def __init__(self):
        self.calls = []
        self.profile = {'deployment_id': DEPLOYMENT, 'embedding_space_id': SPACE, 'dimension': 1024, 'capability': 'embedding', 'status': 'ready', 'normalization': 'l2'}
        self.version = {'index_id': INDEX, 'index_version_id': VERSION, 'state': 'ready', 'reference_state': 'held', 'chunk_count': 42, 'indexed_count': 42, 'embedding_profile': copy.deepcopy(self.profile)}
        self.vector = [1.0] + [0.0] * 1023
        self.search = {'index_id': INDEX, 'index_version_id': VERSION, 'embedding_space_id': SPACE, 'results': [], 'returned_count': 0, 'rerank': {'requested': False, 'applied': False, 'status': 'disabled'}}
        self.duplicate = False

    def call(self, base, path, method='GET', body=None):
        self.calls.append((path, method, copy.deepcopy(body)))
        if path.startswith('/api/indexes?'):
            item = {'index_id': INDEX, 'name': 'test1', 'state': 'active'}
            return {'items': [item, item] if self.duplicate else [item]}
        if path == '/api/indexes/' + INDEX:
            return {'index_id': INDEX, 'name': 'test1', 'state': 'active', 'active_version_id': VERSION}
        if path == '/api/index-versions/' + VERSION:
            return copy.deepcopy(self.version)
        if path.endswith('/embedding-profile'):
            return copy.deepcopy(self.profile)
        if path.endswith('/embeddings'):
            return {'role': 'query', 'profile': copy.deepcopy(self.profile), 'data': [{'id': 'query-1', 'index': 0, 'embedding': self.vector}]}
        if path == '/api/indexes/' + INDEX + '/search':
            return copy.deepcopy(self.search)
        raise AssertionError(path)


class PrepareTests(unittest.TestCase):
    def test_current_version_and_one_embedding_without_search(self):
        f = Fixture(); payload, receipt = m.prepare(args(), f.call)
        self.assertEqual(payload['path'], {'index_id': INDEX})
        self.assertEqual(payload['body']['index_version_id'], VERSION)
        self.assertEqual(len(payload['body']['query_embedding']), 1024)
        self.assertNotIn('query_text', payload['body'])
        posts = [x for x in f.calls if x[1] == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][0].endswith('/embeddings'))
        self.assertEqual(posts[0][2]['inputs'][0]['text'], args().question)
        self.assertEqual(receipt['agent_tool_execution'], 'not_run')
        self.assertEqual(receipt['lifecycle_writes'], 0)

    def test_optional_baseline_accepts_empty_result(self):
        f = Fixture(); _, receipt = m.prepare(args(check_search=True), f.call)
        self.assertEqual(receipt['baseline_search'], {'status': 'passed', 'returned_count': 0})
        self.assertEqual([x[1] for x in f.calls].count('POST'), 2)
        self.assertTrue(all(p.endswith(('/search', '/embeddings')) for p, method, _ in f.calls if method == 'POST'))

    def test_incomplete_or_unavailable_stops_before_inference(self):
        for key, value in [('state', 'failed'), ('reference_state', 'released'), ('indexed_count', 1)]:
            with self.subTest(key=key):
                f = Fixture(); f.version[key] = value
                with self.assertRaises(ValueError): m.prepare(args(), f.call)
                self.assertFalse(any(method == 'POST' for _, method, _ in f.calls))

    def test_mismatched_profile_stops_before_inference(self):
        f = Fixture(); f.profile['embedding_space_id'] = 'es_' + 'f' * 64
        with self.assertRaises(ValueError): m.prepare(args(), f.call)
        self.assertFalse(any(method == 'POST' for _, method, _ in f.calls))

    def test_invalid_vectors_rejected(self):
        for vector in [[0.0] * 1024, [1.0], [True] + [0.0] * 1023, [float('nan')] + [0.0] * 1023]:
            f = Fixture(); f.vector = vector
            with self.assertRaises(ValueError): m.prepare(args(), f.call)
            self.assertFalse(any(p.endswith('/search') for p, _, _ in f.calls))

    def test_ambiguous_name_and_invalid_ids(self):
        f = Fixture(); f.duplicate = True
        with self.assertRaises(ValueError): m.prepare(args(), f.call)
        self.assertFalse(any(method == 'POST' for _, method, _ in f.calls))
        with self.assertRaises(ValueError): m.prepare(args(index_id='bad'), Fixture().call)

    def test_search_receipt_must_match(self):
        for key, value in [('index_version_id', INDEX), ('returned_count', 2), ('rerank', {'requested': True})]:
            f = Fixture(); f.search[key] = value
            with self.assertRaises(ValueError): m.prepare(args(check_search=True), f.call)

    def test_outputs_are_private_and_never_overwritten(self):
        payload, receipt = m.prepare(args(), Fixture().call)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'test'
            m.write_outputs(out, payload, receipt, 'http://knowledge-gateway:8000')
            self.assertEqual(len(list(out.iterdir())), 6)
            self.assertEqual(json.loads((out / 'tool-arguments.json').read_text()), payload)
            self.assertEqual((out / 'tool-arguments.json').stat().st_mode & 0o777, 0o600)
            self.assertNotIn(args().question, (out / 'preparation-result.json').read_text())
            spec = json.loads((out / 'openapi.json').read_text())
            self.assertIn('/api/indexes/{index_id}/search', spec['paths'])
            with self.assertRaises(FileExistsError): m.write_outputs(out, payload, receipt, 'http://knowledge-gateway:8000')

    def test_transport_and_redirect_rejection(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location', '/ok'); self.end_headers()
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(b'{"ready":true}')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_port)
            self.assertEqual(m.api(base, '/ok'), {'ready': True})
            with self.assertRaisesRegex(ValueError, 'HTTP 302'): m.api(base, '/redirect')
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_strict_json_and_tool_address(self):
        with self.assertRaises(ValueError): m.strict_json('{"a":1,"a":2}')
        with self.assertRaises(ValueError): m.strict_json('{"a":NaN}')
        with self.assertRaises(ValueError): m.validate_base('http://localhost:15100', tool=True)
        with self.assertRaises(ValueError): m.validate_base('http://host:15100/path')


if __name__ == '__main__':
    unittest.main()
