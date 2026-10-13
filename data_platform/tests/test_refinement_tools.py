import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import live_refinement_smoke as smoke


class FakeAPI:
    base = 'http://127.0.0.1:14000'
    def __init__(self, name, source_id):
        self.posts = 0
        self.original = b'hello'
        self.source = {'document_id': source_id, 'state': 'ready', 'size_bytes': 5, 'sha256': hashlib.sha256(self.original).hexdigest()}
        self.run = {'status': 'succeeded', 'phase': 'completed', 'refinement_run_id': 'run', 'output_document_id': 'canonical', 'source_sha256': self.source['sha256'], 'source_format': 'text'}
        self.document = {'schema_version': 'canonical-document-v1', 'source': {'document_id': source_id, 'sha256': self.source['sha256'], 'format': 'text'}, 'processor': {'version': 'cpu-parser-v1', 'rules_version': 'basic-cleaning-v1', 'image_id': 'sha256:' + 'a' * 64}, 'blocks': [{'id': 'b1', 'order': 0, 'type': 'text', 'text': 'hello'}], 'pages': [], 'quality': {'state': 'ready', 'issues': []}}
        self.content = json.dumps(self.document).encode()
        self.metadata = {'document_id': 'canonical', 'refinement_run_id': 'run', 'source_document_id': source_id, 'size_bytes': len(self.content), 'sha256': hashlib.sha256(self.content).hexdigest(), 'schema_version': 'canonical-document-v1', 'processor_version': 'cpu-parser-v1', 'processor_image_id': self.document['processor']['image_id'], 'state': 'ready', 'block_count': 1, 'page_count': 0}
        self.name = name
        self.lose_reply = False
    def call(self, path, body=None, key=None, **kwargs):
        if body is not None:
            self.posts += 1
            if self.lose_reply:
                self.lose_reply = False
                raise OSError('lost acceptance reply')
            if body['name'] != self.name: return 409, {'error': 'conflict'}
            return 202, {'refinement_run_id': 'run', 'operation_id': 'op', 'status': 'accepted'}
        if path.startswith('/api/operations?'): return 200, {'items': [{'operation_id': 'op', 'status': 'succeeded'}]}
        if path.startswith('/api/refinement-runs?'): return 200, {'items': [self.run]}
        if path.startswith('/api/refinement-runs/'): return 200, self.run
        if path.startswith('/api/canonical-documents/'):
            if path.endswith('/content'): return 200, self.content, {'Content-Disposition': 'attachment; filename="canonical.json"', 'X-Content-Type-Options': 'nosniff'}
            return 200, self.metadata
        if path.endswith('/content'): return 200, self.original, {}
        return 200, self.source


class RefinementToolsTests(unittest.TestCase):
    def setup_case(self):
        report = smoke.make_report(smoke.API(FakeAPI.base), ['11111111-1111-4111-8111-111111111111'])
        item = report['items'][0]
        return report, FakeAPI(item['name'], item['source_document_id'])

    def test_success_and_resume_keep_original_request_identity(self):
        report, api = self.setup_case()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            observed = [{'running': False, 'state': 'exited', 'image_id': api.metadata['processor_image_id'], 'exit_code': 0}]
            with patch.object(smoke, 'runtimes', return_value=observed):
                smoke.execute(api, report, path, 1)
                key = report['items'][0]['key']
                smoke.execute(api, report, path, 1)
            self.assertTrue(report['passed'])
            self.assertEqual(key, report['items'][0]['key'])
            self.assertEqual(report['items'][0]['accepted']['operation_id'], 'op')

    def test_lost_acceptance_reply_retains_saved_key(self):
        report, api = self.setup_case(); api.lose_reply = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            with self.assertRaises(OSError): smoke.execute(api, report, path, 1)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['items'][0]['key'], report['items'][0]['key'])
            self.assertEqual(saved['items'][0]['request']['document_id'], report['items'][0]['source_document_id'])
            self.assertFalse(saved['passed'])

    def test_corrupt_content_and_wrong_source_rejected(self):
        _, api = self.setup_case()
        with self.assertRaises(ValueError): smoke.verify_result(api.source, api.run, api.metadata, api.content + b' ')
        source = dict(api.source, sha256='b' * 64)
        with self.assertRaises(ValueError): smoke.verify_result(source, api.run, api.metadata, api.content)

    def test_ocr_need_cannot_be_hidden_as_ready(self):
        _, api = self.setup_case()
        api.document['quality'] = {'state': 'ready', 'issues': [{'code': 'missing_image', 'requires_ocr': True}]}
        data = json.dumps(api.document).encode()
        metadata = dict(api.metadata, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        with self.assertRaises(ValueError): smoke.verify_result(api.source, api.run, metadata, data)

if __name__ == '__main__': unittest.main()
