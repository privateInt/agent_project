"""Read-only result checks: controlled API responses, no server or GPU work."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('DATA_CONSOLE_SOURCE_DIR', Path(__file__).resolve().parents[1] / 'console'))
sys.path.insert(0, str(ROOT))
import check_connection
from data_api import APIError, verified


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.raw = b'original source bytes'
        self.sha = hashlib.sha256(self.raw).hexdigest()
        self.source = {'document_id': 'source', 'sha256': self.sha, 'size_bytes': len(self.raw)}
        self.run = {'refinement_run_id': 'run', 'status': 'succeeded', 'source_document_id': 'source',
                    'output_document_id': 'canonical', 'source_sha256': self.sha, 'source_format': 'pdf',
                    'ocr': {'deployment_id': 'deployment', 'runtime_image_id': 'sha256:runtime'}}
        self.document = {
            'schema_version': 'canonical-document-v1',
            'source': {'document_id': 'source', 'sha256': self.sha, 'format': 'pdf'},
            'processor': {'version': 'cpu-parser-surya-v1', 'image_id': 'sha256:parser'},
            'pages': [{'number': 1}, {'number': 2}],
            'blocks': [{'location': {'page': 1}, 'text': '첫 페이지'},
                       {'location': {'page': 2}, 'cells': [{'text': '둘째 페이지 표'}]}],
            'quality': {'state': 'needs_review', 'issues': [{'requires_ocr': False}]},
            'ocr': {'identity': copy.deepcopy(self.run['ocr']), 'pages': [
                {'page': 1, 'status': 'completed'}, {'page': 2, 'status': 'completed'}]},
        }
        self.meta_overrides = {}
        self.corrupt = ''
        self.calls = []
        test = self

        class ReadOnlyAPI:
            def get(self, collection, identifier):
                test.calls.append(('GET', collection, identifier))
                if collection == 'refinement-runs':
                    return test.run
                if collection == 'source-documents':
                    return test.source
                if collection == 'canonical-documents':
                    raw = json.dumps(test.document).encode()
                    return dict({
                        'document_id': 'canonical', 'source_document_id': 'source', 'refinement_run_id': 'run',
                        'schema_version': 'canonical-document-v1', 'processor_version': test.document['processor']['version'],
                        'processor_image_id': 'sha256:parser', 'state': test.document['quality']['state'],
                        'page_count': len(test.document['pages']), 'block_count': len(test.document['blocks']),
                        'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw),
                    }, **test.meta_overrides)
                raise AssertionError(collection)

            def download(self, collection, identifier, metadata):
                test.calls.append(('DOWNLOAD', collection, identifier))
                raw = test.raw if collection == 'source-documents' else json.dumps(test.document).encode()
                return verified(b'corrupt' if test.corrupt == collection else raw, metadata)

        self.api = ReadOnlyAPI()

    def check(self, **kwargs):
        return check_connection.check_refinement(self.api, 'run', **kwargs)

    def test_completed_ocr_checks_original_and_both_pages_using_reads_only(self):
        result = self.check(expected_ocr_pages=2, expected_source_sha256=self.sha)
        self.assertEqual(result['ocr_completed_pages'], 2)
        self.assertEqual(result['source_sha256'], self.sha)
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all(c[0] in ('GET', 'DOWNLOAD') for c in self.calls))

    def test_native_result_does_not_require_ocr_without_an_expectation(self):
        self.run.pop('ocr')
        self.document.pop('ocr')
        self.document['processor']['version'] = 'cpu-parser-v1'
        self.assertEqual(self.check()['ocr_completed_pages'], 0)
        with self.assertRaises(APIError):
            self.check(expected_ocr_pages=2)

    def test_missing_duplicate_failed_skipped_or_extra_ocr_pages_fail(self):
        records = self.document['ocr']['pages']
        alternatives = [records[:1], [records[0], records[0]], records + [records[1]]]
        alternatives.extend([[records[0], {'page': 2, 'status': status}] for status in ('failed', 'skipped', 'partial')])
        for value in alternatives:
            with self.subTest(records=value):
                self.document['ocr']['pages'] = value
                with self.assertRaises(APIError):
                    self.check(expected_ocr_pages=2)

    def test_completed_status_cannot_hide_empty_page_or_unresolved_ocr(self):
        self.document['blocks'].pop()
        with self.assertRaises(APIError):
            self.check(expected_ocr_pages=2)
        self.document['quality'] = {'state': 'needs_ocr', 'issues': [{'requires_ocr': True}]}
        with self.assertRaises(APIError):
            self.check(expected_ocr_pages=2)

    def test_corrupt_original_or_canonical_download_fails(self):
        for collection in ('source-documents', 'canonical-documents'):
            with self.subTest(collection=collection):
                self.corrupt = collection
                with self.assertRaises(APIError):
                    self.check()

    def test_wrong_expected_file_or_changed_provenance_fails(self):
        with self.assertRaises(APIError):
            self.check(expected_source_sha256='0' * 64)
        baseline = copy.deepcopy(self.document)
        for section, field, value in [('source', 'document_id', 'other'), ('source', 'sha256', '0' * 64),
                                      ('processor', 'image_id', 'other'), ('ocr', 'identity', {})]:
            with self.subTest(section=section, field=field):
                self.document = copy.deepcopy(baseline)
                self.document[section][field] = value
                with self.assertRaises(APIError):
                    self.check()

    def test_failed_run_or_mismatched_metadata_fails(self):
        self.run['status'] = 'failed'
        with self.assertRaises(APIError):
            self.check()
        self.run['status'] = 'succeeded'
        for field, value in [('page_count', 99), ('block_count', 99), ('source_document_id', 'other'),
                             ('refinement_run_id', 'other'), ('state', 'ready'), ('schema_version', 'other')]:
            with self.subTest(field=field):
                self.meta_overrides = {field: value}
                with self.assertRaises(APIError):
                    self.check()

    def test_invalid_cli_expectations_fail_before_any_network_call(self):
        invalid = [['--expect-ocr-pages', '2'], ['--expect-source-sha256', self.sha],
                   ['--refinement-run-id', 'run', '--expect-ocr-pages', '0'],
                   ['--refinement-run-id', 'run', '--expect-source-sha256', 'invalid']]
        for args in invalid:
            with self.subTest(args=args), patch.object(sys, 'argv', ['check_connection.py', *args]), \
                    patch('check_connection.urllib.request.urlopen') as network, \
                    patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    check_connection.main()
                self.assertEqual(error.exception.code, 2)
                network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
