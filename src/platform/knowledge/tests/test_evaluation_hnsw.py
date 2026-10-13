"""HNSW evidence must remain explicit without invalidating legacy reports."""
import copy
import unittest

from src.platform.knowledge.evaluation.common import indexing_config
from src.platform.knowledge.evaluation.runner import compare
from src.platform.knowledge.tests.test_evaluation import Fixture, evaluate, V2


class HNSWEvaluationTests(unittest.TestCase):
    def test_report_pins_resolved_settings_and_preserves_source(self):
        f = Fixture()
        p, d, c = f.inputs()
        f.version['indexing_config'] = indexing_config({})
        f.version['indexing_config']['parameters'] = {'m': 32, 'ef_construction': 256}
        before = copy.deepcopy(f.version)
        report = evaluate(f, p, d, c)
        self.assertTrue(all(v['indexing_config'] == before['indexing_config'] for v in report['variants']))
        self.assertEqual(f.version, before)

    def test_invalid_config_rejected_before_inference(self):
        for bad in [None, {}, {'parameters': {'m': 32}},
                    {**indexing_config({}), 'parameters': {'m': True, 'ef_construction': 128}},
                    {**indexing_config({}), 'parameters': {'m': 16, 'ef_construction': 1025}}]:
            with self.subTest(config=bad):
                f = Fixture()
                p, d, c = f.inputs()
                f.version['indexing_config'] = bad
                with self.assertRaises(ValueError):
                    evaluate(f, p, d, c)
                self.assertFalse(any(method == 'POST' for _, method, _ in f.calls))

    def test_old_reports_compare_without_mutation_and_same_version_drift_fails(self):
        f = Fixture()
        p, d, c = f.inputs()
        current = evaluate(f, p, d, c)
        old = copy.deepcopy(current)
        for variant in old['variants']:
            variant.pop('indexing_config')
        before = copy.deepcopy(old)
        result = compare(old, current, 'baseline-off', 'baseline-on')
        self.assertEqual(old, before)
        row = result['comparisons'][0]
        self.assertEqual(row['left_indexing_config'], row['right_indexing_config'])
        changed = copy.deepcopy(current)
        changed['variants'][1]['indexing_config']['parameters']['m'] = 32
        with self.assertRaisesRegex(ValueError, 'Same index version'):
            compare(old, changed, 'baseline-off', 'baseline-on')
        changed['variants'][1]['index_version_id'] = V2
        result = compare(old, changed, 'baseline-off', 'baseline-on')
        self.assertEqual(result['comparisons'][0]['right_indexing_config']['parameters']['m'], 32)


if __name__ == '__main__':
    unittest.main()
