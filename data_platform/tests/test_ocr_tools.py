import copy
import json
from pathlib import Path
import tempfile
import unittest
import live_ocr_smoke as smoke


class OCRToolsTests(unittest.TestCase):
    def test_authenticated_import_never_saves_token_and_resume_follows_acceptance(self):
        class API:
            lost = True
            calls = []
            def call(self, endpoint, body, key, **kwargs):
                self.calls.append((endpoint, body, key))
                if self.lost:
                    self.lost = False
                    raise OSError('reply lost')
                return 202, {'model_version_id': 'version', 'operation_id': 'operation'}
        api = API()
        secret = 'test-only-hf-secret'
        body = {'version': 'v1', 'repo_id': smoke.REPOSITORY, 'revision': smoke.REVISION}
        report = {'key': 'recorded'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.json'
            with self.assertRaises(OSError):
                smoke.post(api, report, path, 'import', '/imports', body, 202, hf_token=secret)
            saved = json.loads(path.read_text())
            self.assertNotIn(secret, path.read_text())
            self.assertEqual(saved['import']['request'], body)
            with self.assertRaises(ValueError):
                smoke.post(api, saved, path, 'import', '/imports', body, 202)
            result = smoke.post(api, saved, path, 'import', '/imports', body, 202, hf_token=secret)
            self.assertEqual(api.calls[0], api.calls[1])
            self.assertEqual(api.calls[0][1]['hf_token'], secret)
            self.assertNotIn(secret, path.read_text())
            self.assertEqual(smoke.post(api, saved, path, 'import', '/imports', body, 202), result)
            self.assertEqual(len(api.calls), 2)

    def test_retry_import_creates_new_identity_under_existing_model_only_after_failed(self):
        old = {'schema_version': 1, 'kind': 'surya-ocr-smoke', 'key': 'old-key', 'data_api': 'http://data',
               'model': {'accepted': {'model_id': 'model'}},
               'import': {'request': {'repo_id': smoke.REPOSITORY, 'revision': smoke.REVISION, 'version': 'old-version'},
                          'accepted': {'model_version_id': 'old-version-id', 'operation_id': 'old-operation'}}}
        class API:
            base = 'http://model'
            calls = []
            values = {
                '/api/model-versions/old-version-id': {'model_version_id': 'old-version-id', 'model_id': 'model',
                     'operation_id': 'old-operation', 'state': 'failed', 'repo_id': smoke.REPOSITORY, 'requested_revision': smoke.REVISION},
                '/api/operations/old-operation': {'operation_id': 'old-operation', 'status': 'failed'},
                '/api/models/model': {'model_id': 'model', 'model_type': 'ocr'}}
            def call(self, endpoint):
                self.calls.append(endpoint)
                return 200, self.values[endpoint]
        api = API()
        preserved = copy.deepcopy(old)
        report = smoke.retry_import_report(old, api, 'new-key', 0)
        self.assertEqual(old, preserved)
        self.assertEqual(report['model_id'], 'model')
        self.assertEqual(report['key'], 'new-key')
        self.assertNotEqual(report['import_version'], 'old-version')
        self.assertNotIn('import', report)
        self.assertNotIn('deployment', report)
        self.assertEqual(report['retry_of']['operation_id'], 'old-operation')
        for endpoint, field, value in [('/api/model-versions/old-version-id', 'state', 'importing'),
                                        ('/api/operations/old-operation', 'status', 'running'),
                                        ('/api/models/model', 'model_type', 'llm')]:
            initial = api.values[endpoint][field]
            api.values[endpoint][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                smoke.retry_import_report(old, api, 'new-key', 0)
            api.values[endpoint][field] = initial

    def test_post_records_identity_before_lost_reply_and_reuses_it(self):
        class API:
            calls = []
            lost = True
            def call(self, endpoint, body, key, **kwargs):
                self.calls.append((endpoint, body, key))
                if self.lost:
                    self.lost = False
                    raise OSError('reply lost')
                return 201, {'model_id': 'fixed'}
        api = API()
        report = {'key': 'fixture'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.json'
            with self.assertRaises(OSError): smoke.post(api, report, path, 'model', '/api/models', {'name': 'ocr'}, 201)
            saved = json.loads(path.read_text())
            smoke.post(api, saved, path, 'model', '/api/models', {'name': 'ocr'}, 201)
            self.assertEqual(api.calls[0], api.calls[1])
            with self.assertRaises(ValueError): smoke.post(api, saved, path, 'model', '/api/models', {'name': 'changed'}, 201)

    def test_semantic_gate_rejects_missing_text_or_unresolved_ocr(self):
        identity = {'deployment_id': 'fixture'}
        document = {'ocr': {'identity': identity, 'client_version': 'surya-openai-v1', 'pages': [{'page': 1, 'status': 'completed'}]},
                    'blocks': [{'text': 'OCR TEST 12345', 'location': {'bbox': [0, 0, 90, 90]}}],
                    'pages': [{'width': 100, 'height': 100}],
                    'quality': {'state': 'needs_review', 'issues': [{'code': 'ocr_output_requires_review', 'requires_ocr': False}]}}
        self.assertTrue(smoke.verify_ocr_document(document, identity)['recognized_fixture_text'])
        for name in ('text', 'state', 'page', 'coordinate', 'identity'):
            changed = copy.deepcopy(document)
            if name == 'text': changed['blocks'][0]['text'] = 'wrong'
            if name == 'state': changed['quality']['state'] = 'needs_ocr'
            if name == 'page': changed['ocr']['pages'][0]['status'] = 'failed'
            if name == 'coordinate': changed['blocks'][0]['location']['bbox'][2] = 101
            if name == 'identity': changed['ocr']['identity'] = {}
            with self.subTest(name=name), self.assertRaises(ValueError): smoke.verify_ocr_document(changed, identity)


if __name__ == '__main__': unittest.main()
