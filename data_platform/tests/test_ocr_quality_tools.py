"""Offline rubric/replay tests. Synthetic responses are not GPU quality evidence."""
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
from ocr_quality import (evaluate_case, geometry_score, load_suite, normalize,
                         order_score, table_score, text_score, write_html)

DEP = '11111111-1111-4111-8111-111111111111'
IDENTITY = {'deployment_id': DEP, 'model_version_id': '22222222-2222-4222-8222-222222222222',
            'runtime_image_id': 'sha256:' + 'a' * 64, 'runtime_container_id': 'b' * 64,
            'served_model_name': 'deployment-' + DEP, 'profile': 'surya-ocr-v2',
            'internal_endpoint': 'http://model-serving-' + DEP + ':8000/v1', 'source_manifest_sha256': 'c' * 64}
PIN = {k: IDENTITY[k] for k in live.PIN_FIELDS}


def synthetic_document(case):
    blocks = []
    for p in case['pages']:
        region = p['anchors'][0]['bbox']
        for line in p['text'].splitlines():
            matches = [a['bbox'] for a in p['anchors'] if normalize(a['text']) in normalize(line)]
            region = matches[0] if matches else region
            blocks.append({'type': 'paragraph', 'text': line, 'location': {'page': p['number'], 'bbox': region[:]}})
        for table in p['tables']:
            blocks.append({'type': 'table', 'text': '\n'.join(c['text'] for c in table['cells']),
                           'cells': copy.deepcopy(table['cells']), 'location': {'page': p['number'], 'bbox': table['bbox'][:]}})
    for n, block in enumerate(blocks):
        block.update({'id': 'b' + str(n + 1), 'order': n})
        if case['pages'][block['location']['page'] - 1]['mode'] == 'ocr':
            block['extraction'] = 'surya-openai-v1'
    return {'schema_version': 'canonical-document-v1', 'processor': {'version': 'cpu-parser-surya-v1',
            'rules_version': 'basic-cleaning-v1', 'image_id': 'sha256:' + 'd' * 64},
            'pages': [{k: p[k] for k in ('number', 'width', 'height')} for p in case['pages']], 'blocks': blocks,
            'quality': {'state': 'needs_review', 'issues': [{'code': 'ocr_output_requires_review', 'requires_ocr': False}]},
            'ocr': {'identity': copy.deepcopy(IDENTITY), 'client_version': 'surya-openai-v1', 'policy': 'selective-pages-v1',
                    'pages': [{'page': p['number'], 'status': 'completed'} for p in case['pages'] if p['mode'] == 'ocr']}}


class MetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite, _ = load_suite(live.DEFAULT_SUITE)

    def test_perfect_authored_answers_pass_all_cases(self):
        for case in self.suite['cases']:
            with self.subTest(case=case['id']):
                result = evaluate_case(case, synthetic_document(case), IDENTITY)
                self.assertTrue(result['passed'], result)

    def test_cer_normalizes_only_spacing_and_nfc(self):
        self.assertEqual(text_score('가 é', '가\ne\u0301')['cer'], 0)
        self.assertEqual(text_score('1234', '1239')['cer'], .25)
        self.assertFalse(text_score('a' * 5001, 'a')['passed'])
        self.assertTrue(text_score('a' * 100, 'a' * 98)['passed'])
        self.assertFalse(text_score('a' * 100, 'a' * 97)['passed'])

    def test_order_rejects_missing_duplicate_and_interleaved_columns(self):
        markers = ['가.', '나.', '다.', '라.']
        for text in ['가. 다. 나. 라.', '가. 나. 라.', '가. 가. 나. 다. 라.']:
            self.assertFalse(order_score(markers, text)['passed'])

    def test_geometry_rejects_wrong_column_and_invalid_box(self):
        anchors = [{'text': '오른쪽', 'bbox': [300, 100, 500, 300]}]
        block = {'text': '오른쪽', 'location': {'bbox': [50, 100, 200, 150]}}
        self.assertFalse(geometry_score(anchors, [block], 600, 800)['passed'])
        block['location']['bbox'] = [300, 100, 500, 150]
        self.assertTrue(geometry_score(anchors, [block], 600, 800)['passed'])
        block['location']['bbox'][2] = float('nan')
        self.assertFalse(geometry_score(anchors, [block], 600, 800)['passed'])

    def test_correct_table_words_do_not_hide_bad_spans_or_positions(self):
        expected = self.suite['cases'][1]['pages'][0]['tables']
        actual = copy.deepcopy(expected)
        actual[0]['cells'][0]['column_span'] = 1
        score = table_score(expected, actual)
        self.assertEqual(score['tables'][0]['cell_value_accuracy'], 1)
        self.assertFalse(score['passed'])
        actual = copy.deepcopy(expected)
        actual[0]['cells'][0]['row'] = 9
        self.assertFalse(table_score(expected, actual)['passed'])
        actual = copy.deepcopy(expected)
        actual[0]['cells'].append(copy.deepcopy(actual[0]['cells'][0]))
        self.assertFalse(table_score(expected, actual)['passed'])
        self.assertFalse(table_score(expected, [])['passed'])
        self.assertFalse(table_score([], actual)['passed'])

    def test_missing_ocr_or_native_page_reocr_fails(self):
        case = self.suite['cases'][3]
        doc = synthetic_document(case)
        doc['ocr']['pages'] = [{'page': 1, 'status': 'completed'}, {'page': 2, 'status': 'completed'}]
        self.assertFalse(evaluate_case(case, doc, IDENTITY)['checks']['page_selection'])
        doc = synthetic_document(case)
        doc['blocks'][0]['extraction'] = 'surya-openai-v1'
        self.assertFalse(evaluate_case(case, doc, IDENTITY)['passed'])
        doc = synthetic_document(case)
        doc['quality']['issues'].append({'requires_ocr': True})
        self.assertFalse(evaluate_case(case, doc, IDENTITY)['passed'])
        for status in ('failed', 'partial'):
            doc = synthetic_document(case)
            doc['ocr']['pages'][0]['status'] = status
            self.assertFalse(evaluate_case(case, doc, IDENTITY)['passed'])

    def test_html_escapes_model_text_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.html'
            case = self.suite['cases'][0]
            result = evaluate_case(case, synthetic_document(case), IDENTITY)
            result['pages'][0]['actual_text'] = '<script>alert(1)</script>'
            write_html({'cases': [{'id': 'test', 'evaluation': result}, {'id': 'pending', 'error': '<img onerror=x>'}]}, target)
            text = target.read_text()
            self.assertNotIn('<script>', text)
            self.assertNotIn('<img ', text)
            self.assertIn('&lt;script&gt;', text)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report, self.path = live.initialize(API('http://127.0.0.1:14000'), API('http://127.0.0.1:13000'),
                                                 DEP, live.DEFAULT_SUITE, Path(self.temp.name))
        self.contents = {}

    def ready(self, model, report, path):
        report['pinned_runtime'] = PIN.copy()
        live.persist(path, report)

    def fake_refine(self, api, child, path, timeout, bad=False):
        case = next(c for c in self.report['suite']['cases'] if c['id'] == path.stem)
        doc = synthetic_document(case)
        if bad:
            doc['blocks'][0]['text'] = 'WRONG'
        item = child['items'][0]
        source = {'document_id': 'source-' + path.stem, 'sha256': item['upload']['sha256'], 'size_bytes': item['upload']['size_bytes']}
        doc['source'] = {'document_id': source['document_id'], 'sha256': source['sha256'], 'format': 'pdf'}
        content = (json.dumps(doc, ensure_ascii=False) + '\n').encode()
        run = {'status': 'succeeded', 'output_document_id': 'canonical-' + path.stem,
               'refinement_run_id': 'run-' + path.stem, 'source_format': 'pdf', 'source_sha256': source['sha256'], 'ocr': IDENTITY.copy()}
        metadata = {'document_id': run['output_document_id'], 'refinement_run_id': run['refinement_run_id'],
                    'source_document_id': source['document_id'], 'size_bytes': len(content),
                    'sha256': hashlib.sha256(content).hexdigest(), 'schema_version': doc['schema_version'],
                    'processor_version': doc['processor']['version'], 'processor_image_id': doc['processor']['image_id'],
                    'state': 'needs_review', 'block_count': len(doc['blocks']), 'page_count': len(doc['pages'])}
        item.update({'source': source, 'last_run': run, 'canonical': metadata, 'passed': True})
        child['passed'] = True
        self.contents['/api/canonical-documents/' + metadata['document_id'] + '/content'] = content
        save(path, child)

    def api_content(self, endpoint, **kwargs):
        return 200, self.contents[endpoint], {}

    def run_fake(self, refinement=None):
        with redirect_stdout(io.StringIO()), patch.object(live, 'ready_deployment', side_effect=self.ready), patch.object(live, 'refine', side_effect=refinement or self.fake_refine), patch.object(API, 'call', side_effect=self.api_content):
            live.execute(self.report, self.path, 5)

    def test_all_cases_run_despite_quality_failures(self):
        self.run_fake(lambda *args: self.fake_refine(*args, bad=True))
        self.assertEqual(self.report['evaluated_cases'], 4)
        self.assertEqual(self.report['passed_cases'], 0)
        self.assertFalse(self.report['passed'])
        with patch.object(API, 'call', side_effect=AssertionError('offline network')), patch.object(live, 'refine', side_effect=AssertionError('offline job')):
            live.execute(self.report, self.path, 5, offline=True)
        self.assertEqual(self.report['evaluated_cases'], 4)

    def test_lost_reply_resumes_original_keys(self):
        seeds = copy.deepcopy([i['seed'] for i in self.report['cases']])
        with self.assertRaises(OSError):
            self.run_fake(lambda *args: (_ for _ in ()).throw(OSError('lost acceptance reply')))
        self.assertEqual(self.report['cases'][0]['state'], 'pending')
        self.report = json.loads(self.path.read_text())
        self.run_fake()
        self.assertEqual([i['seed'] for i in self.report['cases']], seeds)
        self.assertTrue(self.report['passed'])
        with patch.object(API, 'call', side_effect=AssertionError('completed runs need no service')):
            live.execute(self.report, self.path, 5)

    def test_terminal_failure_preserved_while_other_cases_finish(self):
        def failed_first(api, child, path, timeout):
            if path.stem == 'korean-body':
                child['items'][0]['last_run'] = {'status': 'failed', 'error_code': 'OCR_FAILED'}
                save(path, child)
                raise ValueError('failed operation')
            self.fake_refine(api, child, path, timeout)
        self.run_fake(failed_first)
        self.assertEqual(self.report['cases'][0]['state'], 'execution_failed')
        self.assertEqual(self.report['passed_cases'], 3)
        self.assertFalse(self.report['passed'])
        with patch.object(live, 'refine', side_effect=AssertionError('do not replace failed operation')):
            live.execute(self.report, self.path, 5)

    def test_changed_suite_or_child_request_rejected_before_job(self):
        self.report['suite']['cases'][0]['pages'][0]['text'] = 'changed'
        with patch.object(live, 'refine') as submitted, self.assertRaises(ValueError):
            live.execute(self.report, self.path, 5)
        submitted.assert_not_called()
        self.report['suite'], _ = load_suite(live.DEFAULT_SUITE)
        childfile = live.child_path(self.path, self.report['cases'][0])
        child = json.loads(childfile.read_text())
        child['items'][0]['key'] = 'changed'
        save(childfile, child)
        with patch.object(live, 'refine') as submitted, self.assertRaises(ValueError):
            live.execute(self.report, self.path, 5)
        submitted.assert_not_called()

    def test_corrupt_cache_cannot_pass_offline_or_submit_new_work(self):
        self.run_fake()
        target = live.canonical_path(self.path, self.report['cases'][0])
        target.write_bytes(target.read_bytes() + b' ')
        for offline in (True, False):
            with patch.object(live, 'refine') as submitted, self.assertRaises(ValueError):
                live.execute(self.report, self.path, 5, offline=offline)
            submitted.assert_not_called()
            self.assertFalse(json.loads(self.path.read_text())['passed'])

    def test_changed_last_fixture_blocks_every_request(self):
        fixtures = Path(self.temp.name) / 'fixtures'
        fixtures.mkdir()
        suite = self.report['suite']
        for case in suite['cases']:
            (fixtures / case['file']).write_bytes((live.DEFAULT_SUITE.parent / case['file']).read_bytes())
        (fixtures / 'suite.json').write_bytes(live.DEFAULT_SUITE.read_bytes())
        report, path = live.initialize(API(self.report['api']), API(self.report['model_api']), DEP,
                                       fixtures / 'suite.json', Path(self.temp.name) / 'changed')
        source = fixtures / suite['cases'][-1]['file']
        source.write_bytes(source.read_bytes() + b'changed')
        with patch.object(API, 'call') as network, patch.object(live, 'refine') as submitted, self.assertRaises(ValueError):
            live.execute(report, path, 5)
        network.assert_not_called()
        submitted.assert_not_called()

    def test_incomplete_offline_report_does_not_pass(self):
        with patch.object(API, 'call', side_effect=AssertionError('offline network')):
            live.execute(self.report, self.path, 5, offline=True)
        self.assertFalse(self.report['passed'])
        self.assertEqual(self.report['evaluated_cases'], 0)

    def test_runtime_change_rejected_before_job(self):
        self.report['pinned_runtime'] = PIN.copy()
        dep = dict(PIN, status='ready', runtime_state='ready', desired_state='running',
                   artifact_kind='snapshot', serving={'profile': 'surya-ocr-v2'}, runtime_container_id='e' * 64)
        with patch.object(API, 'call', return_value=(200, dep)), patch.object(live, 'refine') as submitted, self.assertRaises(ValueError):
            live.execute(self.report, self.path, 5)
        submitted.assert_not_called()


if __name__ == '__main__':
    unittest.main()
