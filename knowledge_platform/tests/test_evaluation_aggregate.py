"""Regressions for reports produced with different float summation algorithms."""
import copy
import unittest

from knowledge_platform.evaluation.common import digest, encoded
from knowledge_platform.evaluation.metrics import summarize
from knowledge_platform.evaluation.runner import aggregate_matches, compare, run
from knowledge_platform.tests.test_evaluation import Fixture


OFF_MS = [52.817, 27.043, 21.738, 22.365, 19.283, 22.515,
          18.677, 28.530, 26.883, 23.168, 27.389, 26.791]


def sequential_sum(values):
    total = 0
    for value in values:
        total += value
    return total


def report_fixture():
    fixture = Fixture()
    packet, dataset, config = fixture.inputs()
    report = run(dataset, packet, config, digest(encoded(dataset)),
                 'http://knowledge', 'http://model', fixture.call)
    prototype = report['cases'][0]
    report['cases'] = []
    for i, ms in enumerate(OFF_MS):
        case = copy.deepcopy(prototype)
        case['case_id'] = 'q' + str(i + 1)
        case['variants']['baseline-off']['search_wall_ms'] = ms
        report['cases'].append(case)
    report['planned_questions'] = len(OFF_MS)
    report['summary'] = summarize(report['cases'], ['baseline-off', 'baseline-on'], len(OFF_MS))
    return report


class AggregateTests(unittest.TestCase):
    def test_sequential_and_precise_means_are_both_accepted_without_mutation(self):
        report = report_fixture()
        saved = copy.deepcopy(report)
        saved['summary']['baseline-off']['mean_successful_search_wall_ms'] = sequential_sum(OFF_MS) / len(OFF_MS)
        self.assertNotEqual(saved['summary'], report['summary'])
        original = encoded(saved)
        result = compare(saved, saved, 'baseline-off', 'baseline-on')
        self.assertEqual(result['schema_version'], 'retrieval-comparison-v1')
        self.assertEqual(encoded(saved), original)

    def test_rounded_metric_is_accepted_but_real_changes_are_rejected(self):
        report = report_fixture()
        report['summary']['baseline-off']['all_questions_metrics']['ndcg'] += 1e-15
        compare(report, report, 'baseline-off', 'baseline-on')
        for change in ('metric', 'latency', 'count', 'bool', 'missing', 'extra', 'nan', 'inf'):
            bad = report_fixture()
            summary = bad['summary']['baseline-off']
            if change == 'metric': summary['all_questions_metrics']['ndcg'] += 1e-6
            elif change == 'latency': summary['mean_successful_search_wall_ms'] += .001
            elif change == 'count': summary['succeeded'] += 1
            elif change == 'bool': summary['failed'] = False
            elif change == 'missing': summary.pop('not_run')
            elif change == 'extra': summary['extra'] = 0
            elif change == 'nan': summary['mean_successful_search_wall_ms'] = float('nan')
            elif change == 'inf': summary['mean_successful_search_wall_ms'] = float('inf')
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Report aggregate differs'):
                compare(bad, bad, 'baseline-off', 'baseline-on')

    def test_dataset_and_candidate_identity_still_require_exact_match(self):
        left = report_fixture()
        right = copy.deepcopy(left)
        right['dataset_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'controlled comparison'):
            compare(left, right, 'baseline-off', 'baseline-on')
        right = copy.deepcopy(left)
        right['cases'][0]['variants']['baseline-on']['candidate_set_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Candidate sets changed'):
            compare(left, right, 'baseline-off', 'baseline-on')

    def test_count_types_and_nulls_remain_strict(self):
        self.assertFalse(aggregate_matches({'count': 1.0}, {'count': 1}))
        self.assertFalse(aggregate_matches({'count': True}, {'count': 1}))
        self.assertFalse(aggregate_matches({'mean': 0}, {'mean': None}))
        self.assertTrue(aggregate_matches({'mean': None}, {'mean': None}))


if __name__ == '__main__':
    unittest.main()
