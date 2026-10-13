"""Stdlib-only geometry rubric/replay checks. No real OCR or service requests."""
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import live_ocr_quality as live
from live_collection_smoke import API, save
from ocr_quality import evaluate_case, load_suite, write_html
from test_ocr_quality_tools import IDENTITY, PIN, DEP, synthetic_document

SUITE_PATH = live.DEFAULT_SUITE.parent.parent / 'ocr-geometry/suite.json'


def fake_document(case):
    doc = synthetic_document(case)
    for p, expected in zip(doc['pages'], case['pages']):
        p['coordinate_system'] = expected['coordinate_system']
        if 'rotation' in expected: p['rotation'] = expected['rotation']
    for block in doc['blocks']:
        block['location']['coordinate_system'] = case['pages'][0]['coordinate_system']
    if case['pages'][0]['mode'] == 'guarded':
        page = case['pages'][0]
        native = [{'id': 'b1', 'order': 0, 'type': 'image', 'text': '',
                   'location': {'page': 1, 'bbox': [0, 0, page['width'], page['height']], 'coordinate_system': page['coordinate_system']}}]
        issue = {'code': 'pdf_page_has_no_extractable_text', 'requires_ocr': True, 'location': {'page': 1}}
        doc['blocks'] = copy.deepcopy(native)
        doc['ocr']['pages'] = [{'page': 1, 'status': 'failed', 'reason': 'pdf_coordinate_mismatch',
                                'native_blocks': native, 'native_issues': [issue]}]
        doc['quality'] = {'state': 'needs_ocr', 'issues': [issue, {'code': 'ocr_pdf_coordinate_mismatch', 'requires_ocr': True, 'location': {'page': 1}}]}
    return doc


class GeometryToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite, _ = load_suite(SUITE_PATH)

    def test_suite_contains_five_quality_and_two_explicit_guard_cases(self):
        self.assertEqual(len(self.suite['cases']), 7)
        self.assertEqual(sum(c['pages'][0]['mode'] == 'ocr' for c in self.suite['cases']), 5)
        self.assertEqual(sum(c['pages'][0]['mode'] == 'guarded' for c in self.suite['cases']), 2)

    def test_expected_guard_is_not_ocr_success_and_has_no_cer(self):
        for case in self.suite['cases'][3:5]:
            result = evaluate_case(case, fake_document(case), IDENTITY)
            self.assertTrue(result['passed'], result)
            self.assertFalse(result['ocr_succeeded'])
            self.assertIsNone(result['pages'][0]['text']['cer'])
            self.assertIsNone(result['pages'][0]['text']['passed'])

    def test_guard_cannot_hide_wrong_reason_inference_or_native_loss(self):
        case = self.suite['cases'][4]
        mutations = [lambda d: d['quality'].update(state='needs_review'),
                     lambda d: d['ocr']['pages'][0].update(reason='timeout'),
                     lambda d: d['ocr']['pages'][0].update(raw_html='<p>inferred</p>'),
                     lambda d: d['ocr']['pages'][0].update(rendered_image={'width': 1, 'height': 1}),
                     lambda d: d['blocks'][0].update(text='changed'),
                     lambda d: d['quality'].update(issues=[])]
        for mutate in mutations:
            doc = fake_document(case); mutate(doc)
            self.assertFalse(evaluate_case(case, doc, IDENTITY)['passed'])

    def test_coordinate_or_rotation_mismatch_fails_despite_perfect_text(self):
        for case in (self.suite['cases'][0], self.suite['cases'][-1]):
            doc = fake_document(case)
            self.assertTrue(evaluate_case(case, doc, IDENTITY)['passed'])
            doc['pages'][0]['coordinate_system'] = 'wrong'
            self.assertFalse(evaluate_case(case, doc, IDENTITY)['passed'])
        case = self.suite['cases'][1]; doc = fake_document(case)
        doc['pages'][0]['rotation'] = 0
        self.assertFalse(evaluate_case(case, doc, IDENTITY)['passed'])

    def test_legacy_rubric_cannot_accept_guard_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = copy.deepcopy(self.suite); suite['rubric_version'] = 'ocr-quality-v1'
            suite['cases'] = [suite['cases'][3]]
            case = suite['cases'][0]
            (root / case['file']).write_bytes((SUITE_PATH.parent / case['file']).read_bytes())
            save(root / 'suite.json', suite)
            with self.assertRaises(ValueError): load_suite(root / 'suite.json')

    def test_guard_html_is_explicit_and_metrics_are_not_applicable(self):
        with tempfile.TemporaryDirectory() as directory:
            case = self.suite['cases'][3]; target = Path(directory) / 'report.html'
            result = evaluate_case(case, fake_document(case), IDENTITY)
            write_html({'passed': True, 'cases': [{'id': case['id'], 'evaluation': result}]}, target)
            text = target.read_text()
            self.assertIn('GUARD_PASS (OCR 미수행)', text)
            self.assertIn('N/A', text)
            self.assertNotIn('CER: 0', text)

    def test_runner_preserves_separate_counts_and_offline_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            report, path = live.initialize(API('http://127.0.0.1:14000'), API('http://127.0.0.1:13000'), DEP, SUITE_PATH, Path(directory))
            contents = {}
            def refine(api, child, target, timeout):
                case = next(c for c in self.suite['cases'] if c['id'] == target.stem)
                doc = fake_document(case); item = child['items'][0]; upload = item['upload']
                source = {'document_id': 'source-' + case['id'], 'sha256': upload['sha256'], 'size_bytes': upload['size_bytes']}
                doc['source'] = {'document_id': source['document_id'], 'sha256': source['sha256'], 'format': upload['format']}
                raw = json.dumps(doc, ensure_ascii=False).encode()
                run = {'status': 'succeeded', 'output_document_id': 'canonical-' + case['id'], 'refinement_run_id': 'run-' + case['id'],
                       'source_format': upload['format'], 'source_sha256': source['sha256'], 'ocr': copy.deepcopy(IDENTITY)}
                meta = {'document_id': run['output_document_id'], 'refinement_run_id': run['refinement_run_id'],
                        'source_document_id': source['document_id'], 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                        'schema_version': doc['schema_version'], 'processor_version': doc['processor']['version'],
                        'processor_image_id': doc['processor']['image_id'], 'state': doc['quality']['state'],
                        'block_count': len(doc['blocks']), 'page_count': len(doc['pages'])}
                item.update({'source': source, 'last_run': run, 'canonical': meta, 'passed': True})
                child['passed'] = True; save(target, child)
                contents['/api/canonical-documents/' + meta['document_id'] + '/content'] = raw
            def request(endpoint, **kwargs):
                if endpoint == '/api/deployments/' + DEP:
                    return 200, dict(PIN, status='ready', runtime_state='ready', desired_state='running', artifact_kind='snapshot', serving={'profile': 'surya-ocr-v2'})
                return 200, contents[endpoint], {}
            with redirect_stdout(io.StringIO()), patch.object(live, 'refine', side_effect=refine), patch.object(API, 'call', side_effect=request):
                live.execute(report, path, 5)
            self.assertTrue(report['passed'])
            self.assertEqual((report['ocr_passed_cases'], report['guard_passed_cases']), (5, 2))
            with patch.object(API, 'call', side_effect=AssertionError('offline network')):
                live.execute(report, path, 5, offline=True)
            self.assertTrue(report['passed'])
            self.assertEqual((report['ocr_passed_cases'], report['guard_passed_cases']), (5, 2))


if __name__ == '__main__':
    unittest.main()
