"""Synthetic fixtures only: no GPU, DB or real retrieval quality claims."""
import copy
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import threading
import json
import math
from pathlib import Path
import tempfile
import unittest

from knowledge_platform.evaluation.common import (
    Client, APIError, identity, digest, encoded, save_json, read_json,
    DATASET_LIMIT, PACKET_LIMIT, REPORT_LIMIT,
)
from knowledge_platform.evaluation.dataset import prepare, validate_dataset, add_judgment
from knowledge_platform.evaluation.metrics import coverage, assign, metrics
from knowledge_platform.evaluation.runner import run, compare, validate_variants
from knowledge_platform.evaluation.__main__ import main

I = 'aafe22b7-852b-424e-afaa-58fa8c1d739f'
V = '150826c2-1f4c-495c-8e07-54aacaa1a245'
V2 = '250826c2-1f4c-495c-8e07-54aacaa1a245'
D = '44247297-5184-4a36-9225-7791c80d3add'
R = 'cc64a683-a69f-48a5-836e-9f52f0255cc6'
C = '157c335f-5436-4338-8078-edb040ebe1ca'
S = '11111111-1111-4111-8111-111111111111'


class Fixture:
    def __init__(self):
        self.calls = []
        self.canonical = {'schema_version': 'canonical-document-v1', 'source': {'document_id': S, 'sha256': 'a'*64},
                          'quality': {'state': 'needs_ocr', 'issues': []}, 'blocks': [
                              {'id': 'b1', 'type': 'text', 'text': '😀 대한민국의 수도는 서울이다.', 'location': {'page': 1}},
                              {'id': 'b2', 'type': 'table', 'text': '일본의 수도는 도쿄다.', 'location': {'page': 2}}]}
        self.raw = encoded(self.canonical)
        pin = {'canonical_id': C, 'canonical_sha256': digest(self.raw), 'source_document_id': S, 'source_sha256': 'a'*64, 'size_bytes': len(self.raw)}
        self.profile = {'deployment_id': D, 'model_version_id': S, 'embedding_space_id': 'es_'+'a'*64, 'dimension': 1024,
                        'capability': 'embedding', 'status': 'ready', 'normalization': 'l2', 'max_input_tokens': 2048,
                        'runtime_container_id': 'c'*64}
        self.reranker = {'deployment_id': R, 'model_version_id': S, 'reranker_profile_id': 'rp_'+'b'*64, 'capability': 'reranker',
                         'status': 'ready', 'score_kind': 'logit', 'score_direction': 'higher_is_better', 'truncation': 'reject',
                         'max_candidates': 100, 'max_input_tokens': 512, 'runtime_container_id': 'd'*64}
        self.version = {'index_id': I, 'index_version_id': V, 'state': 'ready', 'reference_state': 'held', 'chunk_count': 2, 'indexed_count': 2,
                        'chunking_run_id': S, 'chunk_set_sha256': 'e'*64, 'canonical_ids': [C], 'embedding_profile': self.profile,
                        'input_spec': {'snapshot_id': 'f'*64, 'documents': [pin], 'chunker': {'id': 'structure_v1', 'version': '2026-09-18.2',
                                      'options': {'max_characters': 1000, 'overlap_characters': 100}, 'policy_hash': '9'*64}}}
        self.version2 = copy.deepcopy(self.version)
        self.version2.update(index_version_id=V2, chunk_set_sha256='8'*64)
        self.version2['input_spec']['chunker'].update(id='recursive_character_v1', policy_hash='7'*64)
        self.on_error = None
        self.embedding_error = None
        self.change_on = lambda x: None
        self.change_embedding = lambda x: None
        self.empty = False

    def get_raw(self, base, path, **kw):
        self.calls.append((path, 'GET', None))
        return self.raw

    def hit(self, block, rank, vid):
        text = block['text']
        return {'chunk_id': (('1' if rank == 1 else '2') if vid == V else ('3' if rank == 1 else '4'))*64,
                'canonical_id': C, 'source_document_id': S, 'kind': block['type'], 'rank': rank, 'retrieval_rank': rank,
                'retrieval_score': .8 if rank == 1 else .7, 'text': text, 'content_sha256': digest(text.encode()),
                'source_spans': [{'block_id': block['id'], 'start': 0, 'end': len(text), 'chunk_start': 0, 'chunk_end': len(text)}],
                'quality': copy.deepcopy(self.canonical['quality'])}

    def call(self, base, path, method='GET', body=None):
        self.calls.append((path, method, copy.deepcopy(body)))
        if path == '/api/indexes/' + I:
            return {'index_id': I, 'state': 'active', 'active_version_id': V}
        if path == '/api/index-versions/' + V:
            return copy.deepcopy(self.version)
        if path == '/api/index-versions/' + V2:
            return copy.deepcopy(self.version2)
        if path.endswith('/embedding-profile'):
            return copy.deepcopy(self.profile)
        if path.endswith('/reranker-profile'):
            return copy.deepcopy(self.reranker)
        if path.endswith('/embeddings'):
            if self.embedding_error:
                raise self.embedding_error
            out = {'profile': copy.deepcopy(self.profile), 'role': 'query', 'data': [{'id': 'query-1', 'index': 0, 'embedding': [1.0]+[0.0]*1023, 'input_tokens': 9}]}
            self.change_embedding(out)
            return out
        if path.endswith('/search'):
            hits = [] if self.empty else [self.hit(self.canonical['blocks'][1], 1, body['index_version_id']), self.hit(self.canonical['blocks'][0], 2, body['index_version_id'])]
            on = body.get('rerank', {}).get('enabled')
            if on and self.on_error:
                raise self.on_error
            rr = {'requested': False, 'applied': False, 'status': 'disabled'}
            if on:
                hits.reverse()
                for i, h in enumerate(hits, 1):
                    h.update(rank=i, rerank_score=10 if i == 1 else -4, rerank_input_tokens=18)
                rr = {'requested': True, 'applied': bool(hits), 'status': 'applied' if hits else 'skipped_no_candidates',
                      'profile': copy.deepcopy(self.reranker), 'deployment_id': R, 'reranker_profile_id': self.reranker['reranker_profile_id']}
            result = {'index_id': I, 'index_version_id': body['index_version_id'], 'embedding_space_id': self.profile['embedding_space_id'],
                      'candidate_count': len(hits), 'returned_count': len(hits), 'top_k': body['top_k'], 'candidate_k': body['candidate_k'],
                      'filters': {}, 'results': hits, 'rerank': rr, 'score_semantics': {'id': 'opensearch_lucene_cosinesimil_v1'}, 'timings': {'total_ms': 10}}
            if not hits:
                result['empty_reason'] = 'no_matches'
            if on:
                self.change_on(result)
            return result
        raise AssertionError('Unexpected endpoint ' + path)

    def inputs(self):
        packet, dataset, config = prepare(I, V, 'http://knowledge', 'http://data', 'http://model', R, self.call, self.get_raw)
        dataset = add_judgment(dataset, packet, 'q1', '대한민국의 수도는?', C, 'b1', '대한민국의 수도는 서울이다.')
        dataset['review'] = {'status': 'reviewed', 'reviewer': 'synthetic-test'}
        config.update(candidate_k=2, top_k=1)
        self.calls.clear()
        return packet, dataset, config


def evaluate(f, packet, dataset, config, checkpoint=lambda r: None):
    return run(dataset, packet, config, identity(dataset), 'http://knowledge', 'http://model', f.call, checkpoint)


class DatasetTests(unittest.TestCase):
    def test_prepare_is_read_only_and_draft(self):
        f = Fixture()
        packet, dataset, config = prepare(I, V, 'http://knowledge', 'http://data', 'http://model', R, f.call, f.get_raw)
        self.assertTrue(all(method == 'GET' for _, method, _ in f.calls))
        self.assertEqual(len(config['variants']), 2)
        self.assertEqual(packet['documents'][0]['quality']['state'], 'needs_ocr')
        self.assertEqual(dataset['cases'], [])
        with self.assertRaisesRegex(ValueError, 'human review'):
            validate_dataset(dataset, packet)

    def test_bad_canonical_hash_is_rejected(self):
        f = Fixture(); f.raw += b' '
        with self.assertRaisesRegex(ValueError, 'hash/size'):
            prepare(I, V, 'http://knowledge', 'http://data', 'http://model', '', f.call, f.get_raw)

    def test_unicode_offsets_and_review_reset(self):
        f = Fixture(); packet, ds, _ = f.inputs()
        self.assertEqual(ds['cases'][0]['judgments'][0]['start'], 2)
        out = add_judgment(ds, packet, 'q2', '일본의 수도는?', C, 'b2', '도쿄', 2)
        self.assertEqual(out['review']['status'], 'draft')
        self.assertEqual(out['cases'][1]['judgments'][0]['start'], 8)

    def test_ambiguous_quote_requires_explicit_start(self):
        f = Fixture(); f.canonical['blocks'][0]['text'] = '서울 서울'; f.raw = encoded(f.canonical)
        f.version['input_spec']['documents'][0].update(canonical_sha256=digest(f.raw), size_bytes=len(f.raw))
        p, d, _ = prepare(I, V, 'http://knowledge', 'http://data', 'http://model', '', f.call, f.get_raw)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            add_judgment(d, p, 'q1', '도시는?', C, 'b1', '서울')
        out = add_judgment(d, p, 'q1', '도시는?', C, 'b1', '서울', start=3)
        self.assertEqual(out['cases'][0]['judgments'][0]['start'], 3)

    def test_bad_anchors_and_identity_are_rejected(self):
        f = Fixture(); p, d, _ = f.inputs()
        for mutate in [lambda x: x.update(sources_sha256='0'*64), lambda x: x['cases'][0]['judgments'][0].update(start=True),
                       lambda x: x['cases'][0]['judgments'][0].update(grade=True), lambda x: x['cases'][0]['judgments'][0].update(quote_sha256='0'*64),
                       lambda x: x['cases'][0].update(question=' '), lambda x: x.update(unknown=1)]:
            bad = copy.deepcopy(d); mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): validate_dataset(bad, p)
        bad = copy.deepcopy(d); another = copy.deepcopy(bad['cases'][0]['judgments'][0]); another['judgment_id'] = 'e2'; bad['cases'][0]['judgments'].append(another)
        with self.assertRaisesRegex(ValueError, 'Overlapping'): validate_dataset(bad, p)

    def test_duplicate_json_and_atomic_size_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'a.json'; path.write_text('{"a":1,"a":2}')
            with self.assertRaises(ValueError): read_json(path, 100)
            before = path.read_bytes()
            with self.assertRaises(ValueError): save_json(path, {'large': 'a'*200}, 100)
            self.assertEqual(path.read_bytes(), before)
            save_json(path, {'a': 1}, 100)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cli_validate_and_review_require_actual_cases(self):
        f = Fixture(); p, d, _ = f.inputs()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); save_json(root/'sources.json', p, PACKET_LIMIT); save_json(root/'dataset.json', d, DATASET_LIMIT)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['validate', '--sources', str(root/'sources.json'), '--dataset', str(root/'dataset.json')]), 0)


    def test_cli_workflow_over_local_http_double(self):
        fixture = Fixture()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def serve(self, method):
                if self.path.endswith('/content'):
                    raw = fixture.raw
                else:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if method == 'POST' else None
                    raw = encoded(fixture.call('http://double', self.path, method, body))
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                self.serve('GET')
            def do_POST(self):
                self.serve('POST')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
                out = Path(tmp)/'evaluation'; url = 'http://127.0.0.1:' + str(server.server_port)
                self.assertEqual(main(['prepare', '--index-id', I, '--index-version-id', V, '--reranker-deployment-id', R,
                                       '--knowledge-url', url, '--model-url', url, '--data-url', url, '--output-dir', str(out)]), 0)
                common = ['--sources', str(out/'sources.json'), '--dataset', str(out/'dataset.json')]
                self.assertEqual(main(['add-case', *common, '--case-id', 'q1', '--question', '대한민국의 수도는?',
                                       '--canonical-id', C, '--block-id', 'b1', '--quote', '서울']), 0)
                self.assertEqual(main(['review', *common, '--reviewer', 'synthetic-test']), 0)
                cfg, _ = read_json(out/'variants.json', DATASET_LIMIT); cfg.update(candidate_k=2, top_k=1); save_json(out/'variants.json', cfg, DATASET_LIMIT)
                self.assertEqual(main(['run', *common, '--variants', str(out/'variants.json'), '--output', str(out/'report.json'),
                                       '--knowledge-url', url, '--model-url', url]), 0)
                self.assertEqual(main(['compare', '--left', str(out/'report.json'), '--right', str(out/'report.json'),
                                       '--left-variant', 'baseline-off', '--right-variant', 'baseline-on', '--output', str(out/'comparison.json')]), 0)
                report, _ = read_json(out/'report.json', REPORT_LIMIT)
                self.assertEqual(report['status'], 'completed'); self.assertEqual(report['query_embedding_requests'], 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


class MetricTests(unittest.TestCase):
    def test_hand_calculated_graded_metrics(self):
        assignment = [{'grade': 1}, {'grade': 0}, {'grade': 3}]
        result = metrics(assignment, [{'grade': 3}, {'grade': 1}], 3)
        self.assertEqual(result['hit_rate'], 1)
        self.assertEqual(result['recall'], 1)
        self.assertEqual(result['mrr'], 1)
        self.assertAlmostEqual(result['ndcg'], (1 + 7/2)/(7 + 1/math.log2(3)))

    def test_repeated_spans_do_not_inflate_coverage(self):
        j = {'canonical_id': C, 'source_document_id': S, 'block_id': 'b1', 'start': 0, 'end': 10, 'grade': 3, 'judgment_id': 'e1'}
        hit = {'canonical_id': C, 'source_document_id': S, 'chunk_id': '1'*64, 'source_spans': [{'block_id': 'b1', 'start': 0, 'end': 5}]*3}
        self.assertEqual(coverage(hit, j), .5)
        self.assertEqual(assign([hit], [j], 1)[0]['grade'], 0)
        self.assertEqual(assign([hit, hit], [j], .5)[1]['grade'], 0)
        hit['source_spans'].append({'block_id': 'b1', 'start': 5, 'end': 10})
        self.assertEqual(coverage(hit, j), 1)

    def test_one_to_one_grade_and_tie_policy(self):
        f = Fixture(); p, d, _ = f.inputs(); j = d['cases'][0]['judgments'][0]
        other = {**j, 'judgment_id': 'e2', 'grade': 1}
        hit = f.hit(f.canonical['blocks'][0], 1, V)
        a = assign([hit], [other, j], 1)
        self.assertEqual(a[0]['judgment_id'], 'e1')
        self.assertEqual(metrics(a, [other, j], 1)['recall'], .5)
        self.assertEqual(metrics([], [j], 1), {'hit_rate': 0., 'recall': 0., 'mrr': 0., 'ndcg': 0.})


class RunnerTests(unittest.TestCase):
    def test_off_on_one_embedding_and_no_mutations(self):
        f = Fixture(); p, d, c = f.inputs(); r = evaluate(f, p, d, c)
        self.assertEqual(r['status'], 'completed')
        self.assertEqual(r['query_embedding_requests'], 1); self.assertEqual(r['search_requests'], 2)
        self.assertEqual(r['summary']['baseline-off']['all_questions_metrics']['hit_rate'], 0)
        self.assertEqual(r['summary']['baseline-on']['all_questions_metrics']['hit_rate'], 1)
        self.assertEqual(r['summary']['baseline-off']['candidate_recall_all_questions'], 1)
        self.assertTrue(all(path.endswith(('/embeddings', '/search')) for path, method, _ in f.calls if method == 'POST'))
        raw = json.dumps(r, ensure_ascii=False)
        self.assertNotIn(d['cases'][0]['question'], raw)
        self.assertNotIn(f.canonical['blocks'][0]['text'], raw)
        self.assertNotIn('query_embedding"', raw)

    def test_two_chunkers_share_query_embedding_and_keep_pins(self):
        f = Fixture(); p, d, c = f.inputs()
        c['variants'] += [{**copy.deepcopy(v), 'id': v['id'].replace('baseline', 'recursive'), 'index_version_id': V2} for v in c['variants']]
        r = evaluate(f, p, d, c)
        self.assertEqual(r['status'], 'completed'); self.assertEqual(r['query_embedding_requests'], 1); self.assertEqual(r['search_requests'], 4)
        self.assertEqual(len(r['variants']), 4)
        self.assertNotEqual(r['variants'][0]['chunker'], r['variants'][2]['chunker'])

    def test_failures_are_in_denominator_and_do_not_fallback(self):
        f = Fixture(); p, d, c = f.inputs(); f.on_error = APIError(400, 'RERANK_INPUT_TOO_LONG')
        r = evaluate(f, p, d, c)
        self.assertEqual(r['status'], 'completed_with_errors')
        s = r['summary']['baseline-on']; self.assertEqual(s['failed'], 1); self.assertEqual(s['failure_rate'], 1)
        self.assertEqual(s['all_questions_metrics']['recall'], 0); self.assertIsNone(s['successful_only_metrics'])
        self.assertEqual(s['candidate_recall_all_questions'], 1); self.assertEqual(s['candidate_observed'], 1)
        self.assertEqual(r['cases'][0]['variants']['baseline-on']['error_code'], 'RERANK_INPUT_TOO_LONG')
        self.assertEqual(r['search_requests'], 2)

    def test_mixed_success_and_failure_mean(self):
        f = Fixture(); p, d, c = f.inputs()
        d = add_judgment(d, p, 'q2', '두 번째 질문?', C, 'b2', '도쿄'); d['review'] = {'status': 'reviewed', 'reviewer': 'test'}
        original = f.call
        def call(base, path, method='GET', body=None):
            if path.endswith('/search') and body['rerank']['enabled'] and body['query_text'] == '두 번째 질문?':
                raise APIError(504, 'RERANK_TIMEOUT')
            return original(base, path, method, body)
        f.call = call
        r = evaluate(f, p, d, c); s = r['summary']['baseline-on']
        self.assertEqual(s['all_questions_metrics']['hit_rate'], .5)
        self.assertEqual(s['successful_only_metrics']['hit_rate'], 1)
        self.assertEqual(s['failure_rate'], .5)

    def test_query_failure_fails_all_variants_without_search(self):
        f = Fixture(); p, d, c = f.inputs(); f.embedding_error = APIError(503, 'EMBEDDING_NOT_READY')
        r = evaluate(f, p, d, c)
        self.assertEqual(r['search_requests'], 0)
        self.assertTrue(all(s['failed'] == 1 for s in r['summary'].values()))

    def test_candidate_drift_in_unreturned_topk_is_detected(self):
        f = Fixture(); p, d, c = f.inputs()
        f.change_on = lambda r: r['results'][1].update(chunk_id='5'*64)
        r = evaluate(f, p, d, c)
        self.assertEqual(r['cases'][0]['variants']['baseline-on']['error_code'], 'CANDIDATE_SET_CHANGED')

    def test_runtime_profile_change_fails(self):
        f = Fixture(); p, d, c = f.inputs()
        f.change_embedding = lambda e: e['profile'].update(runtime_container_id='6'*64)
        r = evaluate(f, p, d, c)
        self.assertEqual(r['search_requests'], 0)
        f = Fixture(); p, d, c = f.inputs()
        f.change_on = lambda r: r['rerank']['profile'].update(runtime_container_id='6'*64)
        r = evaluate(f, p, d, c)
        self.assertEqual(r['cases'][0]['variants']['baseline-on']['error_code'], 'RERANKER_PROFILE_CHANGED')

    def test_snapshot_mismatch_rejected_before_inference(self):
        f = Fixture(); p, d, c = f.inputs(); f.version['input_spec']['snapshot_id'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'snapshot'): evaluate(f, p, d, c)
        self.assertFalse(any(method == 'POST' for _, method, _ in f.calls))

    def test_invalid_budget_and_unreviewed_rejected_before_calls(self):
        for field, value in [('candidate_k', 51), ('top_k', True)]:
            f = Fixture(); p, d, c = f.inputs(); c[field] = value
            with self.assertRaises(ValueError): evaluate(f, p, d, c)
            self.assertFalse(f.calls)
        f = Fixture(); p, d, c = f.inputs(); d['review']['status'] = 'draft'
        with self.assertRaises(ValueError): evaluate(f, p, d, c)
        self.assertFalse(f.calls)

    def test_empty_is_successful_zero_not_error(self):
        f = Fixture(); p, d, c = f.inputs(); f.empty = True
        r = evaluate(f, p, d, c)
        self.assertEqual(r['status'], 'completed')
        self.assertEqual(r['summary']['baseline-on']['all_questions_metrics']['recall'], 0)

    def test_source_spans_must_match_actual_text(self):
        f = Fixture(); p, d, c = f.inputs()
        old = f.hit
        def hit(block, rank, vid):
            result = old(block, rank, vid)
            result['source_spans'][0].update(start=1, end=len(block['text'])+1)
            return result
        f.hit = hit
        r = evaluate(f, p, d, c)
        self.assertEqual(r['search_requests'], 1)
        self.assertTrue(all(s['failed'] == 1 for s in r['summary'].values()))

    def test_interrupt_checkpoints_and_compare_refuses_incomplete(self):
        f = Fixture(); p, d, c = f.inputs(); old = f.call
        def interrupted(base, path, method='GET', body=None):
            if path.endswith('/embeddings'): raise KeyboardInterrupt
            return old(base, path, method, body)
        f.call = interrupted; checkpoints = []
        r = evaluate(f, p, d, c, lambda x: checkpoints.append(copy.deepcopy(x)))
        self.assertEqual(r['status'], 'interrupted'); self.assertEqual(r['summary']['baseline-off']['not_run'], 1)
        self.assertEqual(len(checkpoints), 2)
        with self.assertRaises(ValueError): compare(r, r, 'baseline-off', 'baseline-on')

    def test_compare_requires_identical_inputs_and_candidate_sets(self):
        f = Fixture(); p, d, c = f.inputs(); left = evaluate(f, p, d, c)
        result = compare(left, left, 'baseline-off', 'baseline-on')
        self.assertEqual(result['comparisons'][0]['right_minus_left']['hit_rate'], 1)
        for key, value in [('dataset_sha256', '0'*64), ('candidate_k', 5), ('query_profile', {})]:
            right = copy.deepcopy(left); right[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): compare(left, right, 'baseline-off', 'baseline-on')
        right = copy.deepcopy(left); right['cases'][0]['variants']['baseline-on']['candidate_set_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'Candidate'): compare(left, right, 'baseline-off', 'baseline-on')

    def test_client_disallows_mutations_and_redirects(self):
        client = Client()
        for method, path in [('DELETE', '/api/indexes/'+I), ('POST', '/api/indexes/'+I+'/builds'), ('POST', '/api/deployments')]:
            with self.subTest(method=method), self.assertRaises(ValueError): client('http://localhost', path, method, {})
        with self.assertRaises(ValueError): client('http://name:secret@localhost', '/api/indexes/'+I)


if __name__ == '__main__':
    unittest.main()
