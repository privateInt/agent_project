"""Candidate mismatch evidence without relaxed equality or extra inference."""
import copy
import math
from pathlib import Path
import tempfile
import unittest

from src.platform.knowledge.evaluation.common import (
    APIError, REPORT_LIMIT, digest, encoded, identity, read_json, save_json,
)
from src.platform.knowledge.evaluation.diagnostics import (
    CandidateMismatch, DETAIL_LIMIT, REPORT_DETAIL_LIMIT, candidate_equal,
)
from src.platform.knowledge.evaluation.runner import compare, run
from src.platform.knowledge.tests.test_evaluation import Fixture, V, V2


class DiagnosticTests(unittest.TestCase):
    def execute(self, f, packet, dataset, config, checkpoint=lambda r: None):
        return run(dataset, packet, config, digest(encoded(dataset)),
                   'http://knowledge', 'http://model', f.call, checkpoint)

    def test_score_drift_still_fails_and_is_checkpointed(self):
        f = Fixture(); p, d, c = f.inputs()
        f.change_on = lambda r: r['results'][0].update(retrieval_score=math.nextafter(.7, 1.0))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            report = self.execute(f, p, d, c, lambda r: save_json(path, r, REPORT_LIMIT))
            saved, _ = read_json(path, REPORT_LIMIT)
        self.assertEqual(saved, report)
        failure = report['cases'][0]['variants']['baseline-on']
        self.assertEqual(failure['error_code'], 'CANDIDATE_IDENTITY_CHANGED')
        detail = failure['candidate_difference']
        self.assertTrue(detail['same_candidate_ids'])
        self.assertEqual(detail['changed_count'], 1)
        score = detail['changes'][0]['fields']['retrieval_score']
        self.assertEqual(score, {'off': .7, 'on': math.nextafter(.7, 1.0),
                                'on_minus_off': math.nextafter(.7, 1.0) - .7})
        self.assertNotEqual(detail['off_candidates_sha256'], detail['on_candidates_sha256'])
        self.assertEqual(report['status'], 'completed_with_errors')
        self.assertEqual(report['summary']['baseline-on']['all_questions_metrics']['recall'], 0)
        self.assertEqual(failure['candidate_recall'], 1)
        self.assertEqual(report['query_embedding_requests'], 1)
        self.assertEqual(report['search_requests'], 2)
        self.assertEqual(report['cases'][0]['query_embedding_sha256'], identity([1.0] + [0.0]*1023))
        self.assertNotIn('query_embedding"', encoded(report).decode())

    def test_text_metadata_and_rank_differences_are_distinguished(self):
        f = Fixture(); f.inputs()
        off = [f.hit(f.canonical['blocks'][0], 1, V), f.hit(f.canonical['blocks'][1], 2, V)]
        on = copy.deepcopy(off)
        on[0]['text'] += ' PRIVATE-SOURCE'
        on[0]['quality'] = {'state': 'PRIVATE-QUALITY'}
        on[0]['source_spans'][0]['end'] += 1
        on[0]['structure'] = {'caption': 'PRIVATE-CAPTION'}
        on[0]['retrieval_rank'], on[1]['retrieval_rank'] = 2, 1
        with self.assertRaises(CandidateMismatch) as raised:
            candidate_equal(off, on)
        detail = raised.exception.detail
        self.assertEqual(detail['changed_fields'], ['quality', 'retrieval_rank', 'source_spans', 'structure', 'text'])
        first = detail['changes'][0]['fields']
        self.assertEqual(first['retrieval_rank'], {'off': 1, 'on': 2})
        self.assertEqual(first['text']['off_sha256'], identity(off[0]['text']))
        self.assertEqual(first['text']['on_sha256'], identity(on[0]['text']))
        raw = encoded(detail).decode()
        self.assertNotIn('PRIVATE', raw)
        self.assertNotIn(f.canonical['blocks'][0]['text'], raw)

    def test_set_change_keeps_original_error_precedence_and_ids(self):
        f = Fixture(); p, d, c = f.inputs()
        def mutate(r):
            r['results'][0]['chunk_id'] = '5'*64
            r['results'][1]['quality'] = {'state': 'needs_review'}
        f.change_on = mutate
        report = self.execute(f, p, d, c)
        failure = report['cases'][0]['variants']['baseline-on']
        self.assertEqual(failure['error_code'], 'CANDIDATE_SET_CHANGED')
        detail = failure['candidate_difference']
        self.assertEqual(detail['only_off'], ['2'*64])
        self.assertEqual(detail['only_on'], ['5'*64])
        self.assertEqual(detail['changed_count'], 1)
        self.assertEqual(detail['changed_fields'], ['quality'])

    def test_reranking_changes_only_do_not_create_diagnostics(self):
        f = Fixture(); p, d, c = f.inputs()
        c['variants'] += [{**copy.deepcopy(v), 'id': v['id'].replace('baseline', 'recursive'),
                           'index_version_id': V2} for v in c['variants']]
        report = self.execute(f, p, d, c)
        self.assertEqual(report['status'], 'completed')
        self.assertTrue(all('candidate_difference' not in v for v in report['cases'][0]['variants'].values()))
        self.assertEqual(report['query_embedding_requests'], 1)
        self.assertEqual(report['search_requests'], 4)
        vectors = [body['query_embedding'] for path, method, body in f.calls if path.endswith('/search')]
        self.assertEqual(len(vectors), 4)
        self.assertTrue(all(v == vectors[0] for v in vectors))
        self.assertTrue(all(path.endswith(('/embeddings', '/search'))
                            for path, method, _ in f.calls if method == 'POST'))

    def test_api_and_invalid_response_errors_do_not_claim_candidate_comparison(self):
        for mode in ('http', 'invalid'):
            with self.subTest(mode=mode):
                f = Fixture(); p, d, c = f.inputs()
                if mode == 'http':
                    f.on_error = APIError(503, 'RERANK_UNAVAILABLE')
                else:
                    f.change_on = lambda r: r['results'][0].update(retrieval_score='invalid')
                report = self.execute(f, p, d, c)
                failure = report['cases'][0]['variants']['baseline-on']
                self.assertEqual(failure['status'], 'failed')
                self.assertNotIn('candidate_difference', failure)
                self.assertEqual(failure['error_code'], 'RERANK_UNAVAILABLE' if mode == 'http' else 'RESPONSE_CONTRACT_INVALID')

    def test_detail_bound_counts_all_changes_and_is_deterministic(self):
        f = Fixture(); f.inputs()
        off = [f.hit(f.canonical['blocks'][0], 1, V) for _ in range(50)]
        for i, h in enumerate(off):
            h.update(chunk_id=f'{i:064x}', retrieval_rank=i+1)
        on = copy.deepcopy(off)
        for h in on:
            for field in ('text', 'content_sha256', 'source_document_id', 'canonical_id',
                          'kind', 'source_spans', 'quality', 'structure'):
                h[field] = {'private': 'DO-NOT-STORE'*100}
            h['retrieval_score'] = .8000000000000002
        with self.assertRaises(CandidateMismatch) as a:
            candidate_equal(off, on)
        with self.assertRaises(CandidateMismatch) as b:
            candidate_equal(list(reversed(off)), list(reversed(on)))
        detail = a.exception.detail
        self.assertEqual(detail, b.exception.detail)
        self.assertEqual(detail['changed_count'], 50)
        self.assertTrue(detail['truncated'])
        self.assertGreater(len(detail['changes']), 0)
        self.assertEqual(detail['omitted_changed_count'], 50-len(detail['changes']))
        self.assertLessEqual(len(encoded(detail)), DETAIL_LIMIT)
        self.assertNotIn('DO-NOT-STORE', encoded(detail).decode())

    def test_report_budget_keeps_all_failures_with_explicit_omission(self):
        f = Fixture(); p, d, c = f.inputs()
        c['variants'] = [c['variants'][1]]
        d['cases'] = [{**copy.deepcopy(d['cases'][0]), 'case_id': f'q{i}',
                       'question': f'질문 {i}: 대한민국의 수도는?'} for i in range(100)]
        def mutate(r):
            for h in r['results']:
                h['quality'] = {'state': 'needs_review', 'note': 'SECRET'*1000}
                h['structure'] = {'caption': 'SECRET-CAPTION'}
                h['retrieval_score'] = math.nextafter(h['retrieval_score'], 1.0)
        f.change_on = mutate
        report = self.execute(f, p, d, c)
        entries = [case['variants']['baseline-on'] for case in report['cases']]
        self.assertEqual(len(entries), 100)
        self.assertTrue(all(e['error_code'] == 'CANDIDATE_IDENTITY_CHANGED' for e in entries))
        details = [e['candidate_difference'] for e in entries if 'candidate_difference' in e]
        omitted = [e for e in entries if 'candidate_difference_omitted' in e]
        self.assertTrue(details and omitted)
        self.assertLessEqual(sum(len(encoded(x)) for x in details), REPORT_DETAIL_LIMIT)
        self.assertTrue(all(e['candidate_difference_omitted'] == 'report_detail_budget_exhausted' for e in omitted))
        self.assertEqual(report['summary']['baseline-on']['failed'], 100)
        self.assertLessEqual(len(encoded(report)), REPORT_LIMIT)
        self.assertNotIn('SECRET', encoded(report).decode())

    def test_old_and_extended_reports_compare_without_mutating_or_promoting_failures(self):
        f = Fixture(); p, d, c = f.inputs()
        f.change_on = lambda r: r['results'][0].update(retrieval_score=math.nextafter(.7, 1.0))
        report = self.execute(f, p, d, c)
        legacy = copy.deepcopy(report)
        for case in legacy['cases']:
            case.pop('query_embedding_sha256', None)
            for outcome in case['variants'].values():
                outcome.pop('candidate_difference', None)
        original = encoded(legacy)
        comparison = compare(legacy, report, 'baseline-on', 'baseline-on')
        self.assertEqual(comparison['comparisons'][0]['left_failure_rate'], 1)
        self.assertEqual(comparison['comparisons'][0]['right_failure_rate'], 1)
        self.assertTrue(all(v == 0 for v in comparison['comparisons'][0]['right_minus_left'].values()))
        self.assertEqual(encoded(legacy), original)


if __name__ == '__main__':
    unittest.main()
