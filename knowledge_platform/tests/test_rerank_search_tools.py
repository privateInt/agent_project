import copy
import json
from types import SimpleNamespace
import unittest

import live_rerank_search as live
from live_search import APIError, validate_result
from test_search_tools import SearchFixture, INDEX, VERSION, DEPLOYMENT

RERANKER = 'cc64a683-a69f-48a5-836e-9f52f0255cc6'
PROFILE = {'deployment_id': RERANKER, 'model_version_id': DEPLOYMENT, 'reranker_profile_id': 'rp_'+'e'*64,
           'capability': 'reranker', 'status': 'ready', 'score_kind': 'logit', 'score_direction': 'higher_is_better',
           'truncation': 'reject', 'max_candidates': 100, 'max_input_tokens': 512, 'runtime_container_id': 'a'*64}


def args(**kw):
    return SimpleNamespace(**dict({'index_id': INDEX, 'index_version_id': VERSION, 'embedding_deployment_id': '',
        'reranker_deployment_id': RERANKER, 'question': '환경 영향은?', 'top_k': 1, 'candidate_k': 2,
        'knowledge_url': 'http://knowledge', 'model_url': 'http://model'}, **kw))


class Fixture(SearchFixture):
    def __init__(self):
        super().__init__()
        self.error = None
        self.mutate_rerank = lambda result: result

    def response(self, body):
        result = super().response(body)
        if body.get('rerank', {}).get('enabled'):
            if not body.get('query_text', '').strip():
                raise APIError(400, 'SEARCH_INVALID_REQUEST')
            if body['rerank']['expected_reranker_profile_id'] != PROFILE['reranker_profile_id']:
                raise APIError(409, 'RERANK_PROFILE_MISMATCH')
            if self.error:
                raise self.error
            result['rerank'] = {'requested': True, 'applied': True, 'status': 'applied', 'deployment_id': RERANKER,
                                'reranker_profile_id': PROFILE['reranker_profile_id'], 'profile': copy.deepcopy(PROFILE)}
            result['results'][0].update(rerank_score=-2.5, rerank_input_tokens=42)
            return self.mutate_rerank(result)
        return result

    def call(self, base, path, method='GET', body=None):
        if path.endswith('/reranker-profile'):
            self.calls.append((base, path, method, copy.deepcopy(body)))
            return copy.deepcopy(PROFILE)
        return super().call(base, path, method, body)


class RerankSearchTools(unittest.TestCase):
    def test_one_embedding_and_no_lifecycle_actions_or_saved_text(self):
        f = Fixture(); result = live.run(args(), f.call)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len([x for x in f.calls if x[1].endswith('/embeddings')]), 1)
        self.assertEqual(result['lifecycle_changes'], 0)
        self.assertTrue(all(x[1].endswith(('/search', '/embeddings')) for x in f.calls if x[2] == 'POST'))
        raw = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(args().question, raw); self.assertNotIn(f.text, raw); self.assertNotIn('"query_embedding":', raw)

    def test_model_length_failure_stays_failure(self):
        f = Fixture(); f.error = APIError(400, 'RERANK_INPUT_TOO_LONG')
        with self.assertRaises(APIError): live.run(args(), f.call)
        self.assertEqual(len([x for x in f.calls if x[1].endswith('/search')]), 2)

    def test_missing_bad_score_profile_or_fallback_rejected(self):
        for mutate in [lambda r: r['results'][0].pop('rerank_score'),
                       lambda r: r['results'][0].update(rerank_score=float('nan')),
                       lambda r: r['results'][0].update(rerank_input_tokens=513),
                       lambda r: r['rerank'].update(applied=False),
                       lambda r: r['rerank']['profile'].update(score_kind='probability'),
                       lambda r: r['rerank'].update(reranker_profile_id='rp_'+'a'*64)]:
            with self.subTest(mutate=mutate):
                f = Fixture()
                def change(result): mutate(result); return result
                f.mutate_rerank = change
                with self.assertRaises(ValueError): live.run(args(), f.call)

    def test_changed_provenance_or_candidate_set_is_inconclusive(self):
        f = Fixture()
        def change(result): result['results'][0]['quality'] = {'state': 'ready'}; return result
        f.mutate_rerank = change
        with self.assertRaisesRegex(ValueError, 'inconclusive'): live.run(args(), f.call)

    def test_reordered_scores_keep_original_retrieval_rank(self):
        f = Fixture(); body = {'top_k': 2, 'candidate_k': 2, 'filters': {}, 'embedding_space_id': f.profile['embedding_space_id'],
                              'query_text': '질문', 'rerank': {'enabled': True, 'deployment_id': RERANKER, 'expected_reranker_profile_id': PROFILE['reranker_profile_id']}}
        result = f.response(body); second = copy.deepcopy(result['results'][0]); second.update(chunk_id='d'*64, rank=1, retrieval_rank=2, retrieval_score=.2, rerank_score=9)
        result['results'][0]['rank'] = 2; result['results'].insert(0, second); result.update(candidate_count=2, returned_count=2)
        self.assertEqual(validate_result(result, f.version, body)[0]['retrieval_rank'], 2)
        result['results'][1]['rerank_score'] = 9
        with self.assertRaisesRegex(ValueError, 'order'): validate_result(result, f.version, body)

    def test_count_limits_rejected_before_any_api_call(self):
        f = Fixture()
        for values in [{'candidate_k': 51}, {'top_k': 3, 'candidate_k': 2}, {'question': ' '}]:
            with self.assertRaises(ValueError): live.run(args(**values), f.call)
        self.assertFalse(f.calls)


if __name__ == '__main__':
    unittest.main()
