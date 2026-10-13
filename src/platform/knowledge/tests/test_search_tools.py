import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import live_search as live


INDEX = 'aafe22b7-852b-424e-afaa-58fa8c1d739f'
VERSION = '150826c2-1f4c-495c-8e07-54aacaa1a245'
DEPLOYMENT = '44247297-5184-4a36-9225-7791c80d3add'
CANONICAL = '157c335f-5436-4338-8078-edb040ebe1ca'
SOURCE = '11111111-1111-4111-8111-111111111111'
SPACE = 'es_' + 'a'*64


class SearchFixture:
    def __init__(self):
        self.calls = []
        self.profile = {'deployment_id': DEPLOYMENT, 'embedding_space_id': SPACE, 'dimension': 1024,
                        'capability': 'embedding', 'status': 'ready', 'normalization': 'l2', 'max_input_tokens': 2048, 'runtime_container_id': 'b'*64}
        self.version = {'index_id': INDEX, 'index_version_id': VERSION, 'state': 'ready', 'reference_state': 'held',
                        'chunk_count': 48, 'indexed_count': 48, 'embedding_profile': self.profile,
                        'canonical_ids': [CANONICAL], 'input_spec': {'documents': [{'canonical_id': CANONICAL, 'source_document_id': SOURCE}]}}
        self.vector = [1.0] + [0.0]*1023
        self.text = '환경 영향을 평가했습니다.'
        self.hit = {'chunk_id': 'c'*64, 'text': self.text, 'content_sha256': hashlib.sha256(self.text.encode()).hexdigest(),
                    'canonical_id': CANONICAL, 'source_document_id': SOURCE, 'kind': 'text', 'rank': 1, 'retrieval_rank': 1, 'retrieval_score': 0.8,
                    'source_spans': [{'block_id': 'b1', 'start': 0, 'end': len(self.text), 'chunk_start': 0, 'chunk_end': len(self.text)}],
                    'quality': {'state': 'needs_ocr'}}
        self.mutate_result = lambda result: result
        self.mutate_embedding = lambda result: result

    def response(self, body):
        return {'index_id': INDEX, 'index_version_id': VERSION, 'embedding_space_id': SPACE, 'top_k': body['top_k'],
                'candidate_k': body['candidate_k'], 'candidate_count': 1, 'returned_count': 1, 'filters': body['filters'],
                'rerank': {'requested': False, 'applied': False, 'status': 'disabled'},
                'score_semantics': {'id': 'opensearch_lucene_cosinesimil_v1'}, 'results': [copy.deepcopy(self.hit)]}

    def call(self, base, path, method='GET', body=None):
        self.calls.append((base, path, method, copy.deepcopy(body)))
        if path == '/api/indexes/' + INDEX:
            return {'index_id': INDEX, 'state': 'active', 'active_version_id': VERSION}
        if path == '/api/index-versions/' + VERSION:
            return copy.deepcopy(self.version)
        if path.endswith('/embedding-profile'):
            return copy.deepcopy(self.profile)
        if path.endswith('/embeddings'):
            return self.mutate_embedding({'profile': copy.deepcopy(self.profile), 'role': 'query', 'data': [{'id': 'query-1', 'index': 0, 'embedding': list(self.vector), 'input_tokens': 10}]})
        if path == '/api/indexes/' + INDEX + '/search':
            if body['embedding_space_id'] != SPACE:
                raise live.APIError(409, 'SEARCH_SPACE_MISMATCH')
            if len(body['query_embedding']) != 1024:
                raise live.APIError(400, 'SEARCH_DIMENSION_MISMATCH')
            if body['filters'].get('canonical_ids') == []:
                raise live.APIError(400, 'SEARCH_INVALID_FILTER')
            if 'unsupported' in body['filters']:
                raise live.APIError(400, 'SEARCH_INVALID_REQUEST')
            return self.mutate_result(self.response(body))
        raise AssertionError('Unexpected endpoint: ' + path)


def arguments(**overrides):
    return SimpleNamespace(**{'index_id': INDEX, 'index_version_id': VERSION, 'deployment_id': '',
                             'question': '이 자료는 어떤 내용인가요?', 'top_k': 5,
                             'knowledge_url': 'http://knowledge', 'model_url': 'http://model',
                             'skip_rejections': False, **overrides})


class SearchTools(unittest.TestCase):
    def test_one_query_inference_no_mutations_and_pinned_version(self):
        fixture = SearchFixture()
        receipt = live.run(arguments(), fixture.call)
        self.assertEqual(receipt['status'], 'passed')
        embeds = [x for x in fixture.calls if x[1].endswith('/embeddings')]
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0][3]['inputs'][0]['text'], arguments().question)
        self.assertEqual(embeds[0][3]['role'], 'query')
        posts = [x for x in fixture.calls if x[2] == 'POST']
        self.assertTrue(all(x[1].endswith('/search') or x[1].endswith('/embeddings') for x in posts))
        searches = [x for x in posts if x[1].endswith('/search')]
        self.assertEqual(len(searches), 6)
        self.assertTrue(all(x[3]['index_version_id'] == VERSION for x in searches))
        saved = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn(fixture.text, saved)
        self.assertNotIn(arguments().question, saved)
        self.assertNotIn('query_embedding":', saved)
        self.assertEqual(receipt['quality_states'], ['needs_ocr'])

    def test_inactive_or_incomplete_version_stops_before_inference(self):
        for field, value in [('state', 'deleting'), ('reference_state', 'released'), ('indexed_count', 0)]:
            fixture = SearchFixture(); fixture.version[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                live.run(arguments(), fixture.call)
            self.assertFalse(any(x[1].endswith('/embeddings') for x in fixture.calls))

    def test_changed_model_space_stops_before_query(self):
        fixture = SearchFixture()
        original = fixture.call
        def call(base, path, method='GET', body=None):
            out = original(base, path, method, body)
            if path.endswith('/embedding-profile'):
                out['embedding_space_id'] = 'es_'+'f'*64
            return out
        with self.assertRaisesRegex(ValueError, 'profile'):
            live.run(arguments(), call)
        self.assertFalse(any(x[1].endswith('/embeddings') for x in fixture.calls))

    def test_invalid_query_receipts_never_reach_search(self):
        for change in [lambda x: x.update(role='document'), lambda x: x['data'][0].update(id='another'),
                       lambda x: x['data'][0].update(index=False), lambda x: x['data'][0]['embedding'].__setitem__(1, True),
                       lambda x: x['data'][0]['embedding'].__setitem__(1, float('nan')),
                       lambda x: x['data'][0]['embedding'].__setitem__(0, 2.0), lambda x: x['data'].append(copy.deepcopy(x['data'][0]))]:
            fixture = SearchFixture()
            def mutate(out):
                change(out); return out
            fixture.mutate_embedding = mutate
            with self.assertRaises(ValueError):
                live.run(arguments(), fixture.call)
            self.assertFalse(any(x[1].endswith('/search') for x in fixture.calls))

    def test_bad_search_receipts_fail(self):
        for change in [lambda x: x.update(index_version_id=INDEX), lambda x: x.update(returned_count=2),
                       lambda x: x['results'][0].update(text='changed'), lambda x: x['results'][0].update(source_document_id=INDEX),
                       lambda x: x['results'][0].update(retrieval_score=float('inf')), lambda x: x['results'][0].update(source_spans=[]),
                       lambda x: x.update(rerank={'requested': True, 'applied': True, 'status': 'applied'})]:
            fixture = SearchFixture()
            def mutate(out):
                change(out); return out
            fixture.mutate_result = mutate
            with self.assertRaises(ValueError):
                live.run(arguments(), fixture.call)

    def test_valid_empty_results_are_distinct_from_missing_scope(self):
        fixture = SearchFixture()
        body = {'embedding_space_id': SPACE, 'top_k': 5, 'candidate_k': 5, 'filters': {'kinds': ['code']}}
        result = fixture.response(body)
        result.update(results=[], candidate_count=0, returned_count=0, empty_reason='no_matches')
        self.assertEqual(live.validate_result(result, fixture.version, body), [])
        del result['empty_reason']
        with self.assertRaises(ValueError):
            live.validate_result(result, fixture.version, body)

    def test_same_space_redeployment_can_be_selected_explicitly(self):
        fixture = SearchFixture()
        pinned = copy.deepcopy(fixture.profile)
        fixture.version['embedding_profile'] = pinned
        fixture.profile['deployment_id'] = SOURCE
        receipt = live.run(arguments(deployment_id=SOURCE), fixture.call)
        self.assertEqual(receipt['deployment_id'], SOURCE)
        self.assertEqual(receipt['embedding_space_id'], SPACE)

    def test_strict_json_and_compact_saved_summary(self):
        for raw in ['{"a":1,"a":2}', '{"number":NaN}', '{"number":Infinity}']:
            with self.assertRaises(ValueError):
                live.strict_json(raw)
        fixture = SearchFixture()
        receipt = live.run(arguments(), fixture.call)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'summary.json'
            live.save_summary(target, receipt)
            self.assertEqual(json.loads(target.read_text()), receipt)
            self.assertLess(target.stat().st_size, 4096)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
