import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import diagnose_answer as diagnostic

RUN = '5ea4459c-597b-4616-92dc-73d0214c0e75'


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.run = {'agent_run_id': RUN, 'state': 'failed', 'input': {'question': 'PRIVATE_QUESTION'},
                    'error': {'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize', 'message': 'PRIVATE_ERROR'}}
        self.steps = [{'step_id': s, 'state': 'succeeded', 'detail': {}} for s in diagnostic.STEPS]
        self.steps[-1].update(state='failed', detail={'raw': 'PRIVATE_ANSWER'})
        self.steps[2]['detail'] = {'selected_chunk_ids': ['PRIVATE_SOURCE']}
        self.report = {'schema': 1, 'scope': 'reference_syntax_only', 'status': 'invalid',
                       'finish_reason': 'length', 'issues': ['MISSING_REFERENCE'], 'selected_source_count': 1,
                       'max_tokens': 512, 'recognized_count': 0, 'malformed_count': 0,
                       'out_of_range_count': 0, 'answer_bytes': 300, 'completion_tokens': 512}
        self.steps[3]['detail']['reference_diagnostics'] = self.report
        self.routes = []

    def get(self, path):
        self.routes.append(path)
        if path == '/api/agent-runs/' + RUN:return copy.deepcopy(self.run)
        if path == '/api/agent-runs/' + RUN + '/steps':return {'items': copy.deepcopy(self.steps)}
        self.fail('unexpected route')

    def test_read_only_summary_preserves_error_and_never_prints_private_content(self):
        self.report['raw_answer'] = 'PRIVATE_GENERATION'
        result = diagnostic.diagnose(self, RUN)
        self.assertEqual(result['error'], {'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize'})
        self.assertEqual(result['state'], 'failed');self.assertFalse(result['changes_made'])
        self.assertFalse(result['inference_executed']);self.assertFalse(result['answer_delivery_verified'])
        self.assertEqual(len(self.routes), 2)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('raw_answer', result['reference_diagnostics'])

    def test_old_failed_record_is_not_reinterpreted(self):
        self.steps[3]['detail'] = {'model': {}}
        result = diagnostic.diagnose(self, RUN)
        self.assertEqual(result['reference_diagnostics']['status'], 'not_recorded')
        self.assertNotIn('issues', result['reference_diagnostics'])
        self.assertEqual(result['state'], 'failed')

    def test_private_request_is_read_without_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'request.json'
            path.write_text(json.dumps({'receipt': {'agent_run_id': RUN}, 'body': {'secret': 'PRIVATE'}}))
            path.chmod(0o600);before = path.read_bytes()
            self.assertEqual(diagnostic.request_run_id(path), RUN)
            self.assertEqual(before, path.read_bytes())
            link = Path(td) / 'link';link.symlink_to(path)
            with self.assertRaises(ValueError):diagnostic.request_run_id(link)
            path.chmod(0o644)
            with self.assertRaises(ValueError):diagnostic.request_run_id(path)

    def test_missing_receipt_and_untrusted_fields_do_not_become_output(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'request.json';path.write_text('{}');path.chmod(0o600)
            with self.assertRaises(ValueError):diagnostic.request_run_id(path)
            self.assertEqual(path.read_text(), '{}')
        for key, value in [('issues', ['PRIVATE_MODEL_TEXT']), ('answer_bytes', True), ('status', 'PRIVATE')]:
            bad = {**self.report, key: value}
            with self.assertRaises(ValueError) as error:diagnostic.diagnostic_summary(bad)
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_cross_run_or_source_count_mismatch_rejected(self):
        self.report['selected_source_count'] = 2
        with self.assertRaises(ValueError):diagnostic.diagnose(self, RUN)
        self.report['selected_source_count'] = 1
        self.run['agent_run_id'] = '15981e7e-7ab3-41d6-bef8-ab2924cbe67e'
        with self.assertRaises(ValueError):diagnostic.diagnose(self, RUN)


if __name__ == '__main__':unittest.main()
