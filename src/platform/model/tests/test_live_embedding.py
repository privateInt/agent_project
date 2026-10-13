import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('live_embedding_check', Path(__file__).with_name('live_embedding.py'))
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class EmbeddingReceiptTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'deployment_id': 'd1', 'embedding_space_id': 'es_1', 'source_manifest_sha256': 'a',
                        'tokenizer_sha256': 'b', 'runtime_image_id': 'c', 'runtime_container_id': 'd',
                        'model_version_id': 'v1', 'dimension': 1024, 'max_input_tokens': 2048}
        self.result = {'profile': dict(self.profile), 'role': 'query', 'data': [
            {'id': 'q1', 'index': 0, 'embedding': [1.0] + [0.0]*1023, 'input_tokens': 8}]}

    def test_summary_retains_evidence_without_vectors_or_text(self):
        summary = live.validate_result(self.result, self.profile, 'query', ['q1'])
        self.assertEqual(summary[0]['dimension'], 1024)
        self.assertEqual(summary[0]['l2_norm'], 1.0)
        self.assertNotIn('embedding', summary[0])

    def test_changed_identity_and_malformed_vectors_cannot_pass_smoke(self):
        cases = []
        for key in ('embedding_space_id', 'runtime_container_id', 'source_manifest_sha256'):
            result = copy.deepcopy(self.result)
            result['profile'][key] = 'other'
            cases.append(result)
        for vector in ([1.0], [0.0]*1024, [float('nan')]+[0.0]*1023, [True]+[0.0]*1023):
            result = copy.deepcopy(self.result)
            result['data'][0]['embedding'] = vector
            cases.append(result)
        for key, value in (('id', 'q2'), ('index', 1), ('input_tokens', 2049)):
            result = copy.deepcopy(self.result)
            result['data'][0][key] = value
            cases.append(result)
        for result in cases:
            with self.subTest(result=result['data'][0]['id']), self.assertRaises(ValueError):
                live.validate_result(result, self.profile, 'query', ['q1'])


if __name__ == '__main__':
    unittest.main()
