import unittest
from unittest.mock import Mock, patch
import uuid

import live_serving_smoke as smoke
from test_live_serving_smoke import FakeControl, FakeInference


class AdapterControl(FakeControl):
    def __init__(self):
        super().__init__()
        self.adapter = str(uuid.uuid4())
        self.base = str(uuid.uuid4())

    def call(self, method, path, body=None, key=None):
        result = super().call(method, path, body, key)
        if path.startswith('/api/deployments/') and not path.endswith('/stop'):
            result.update(artifact_kind='adapter', adapter_model_version_id=self.adapter,
                          base_model_version_id=self.base, base_served_model_name='test-base')
        return result


class AdapterInference(FakeInference):
    def __init__(self, deployment, base_only=False, wrong_response=False):
        super().__init__()
        self.deployment = deployment
        self.base_only = base_only
        self.wrong_response = wrong_response

    def call(self, method, path, body=None):
        if path == '/models':
            entries = [{'id': 'test-base'}]
            if not self.base_only:
                entries.append({'id': 'test-model', 'parent': 'test-base',
                                'root': '/models/' + self.deployment + '/adapter'})
            return {'data': entries}
        result = super().call(method, path, body)
        if self.wrong_response:
            result['model'] = 'test-base'
        return result


class AdapterSmokeChecks(unittest.TestCase):
    def test_missing_or_wrong_source_type_stops_preflight(self):
        source_id = str(uuid.uuid4())
        source = {'model_version_id': source_id, 'state': 'ready', 'artifact_kind': 'adapter'}
        for extra in ({}, {'source_type': 'upload'}, {'source_type': ''}):
            with self.subTest(extra=extra):
                client = Mock()
                client.call.return_value = dict(source, **extra)
                with self.assertRaisesRegex(RuntimeError, 'source_type'):
                    smoke.source_lineage(client, source_id)
                client.call.assert_called_once_with('GET', '/api/model-versions/' + source_id)

    def test_source_preflight_checks_exact_base(self):
        adapter_id, base_id = str(uuid.uuid4()), str(uuid.uuid4())
        source = {'model_version_id': adapter_id, 'state': 'ready', 'artifact_kind': 'adapter',
                  'source_type': 'training', 'model_id': 'model', 'source_model_version_id': base_id,
                  'base_model_artifact_uri': 's3://artifacts/base/'}
        base = {'model_version_id': base_id, 'state': 'ready', 'artifact_kind': 'snapshot',
                'model_id': 'model', 'artifact_uri': source['base_model_artifact_uri']}
        client = Mock()
        client.call.side_effect = [source, base]
        self.assertEqual(smoke.source_lineage(client, adapter_id), {
            'expected_artifact_kind': 'adapter', 'expected_base_model_version_id': base_id,
            'expected_adapter_model_version_id': adapter_id})
        client.call.side_effect = [source, dict(base, artifact_uri='s3://artifacts/another/')]
        with self.assertRaisesRegex(RuntimeError, 'lineage'):
            smoke.source_lineage(client, adapter_id)

    def record(self, control):
        return {'idempotency_key': str(uuid.uuid4()),
                'request': {'model_version_id': control.adapter, 'serving': {'engine': 'vllm'}},
                'expected_artifact_kind': 'adapter', 'expected_base_model_version_id': control.base,
                'expected_adapter_model_version_id': control.adapter}

    def test_adapter_alias_inference_and_stop(self):
        control = AdapterControl()
        with patch('builtins.print'):
            result = smoke.exercise(control, self.record(control), 60,
                                    runtime_factory=lambda _: AdapterInference(control.deployment))
        self.assertTrue(result['passed'])
        self.assertTrue(result['stopped'])
        self.assertEqual(result['inference']['model'], 'test-model')

    def test_base_only_or_wrong_model_response_cannot_pass(self):
        for kwargs in ({'base_only': True}, {'wrong_response': True}):
            with self.subTest(**kwargs):
                control = AdapterControl()
                with patch('builtins.print'), self.assertRaises(RuntimeError):
                    smoke.exercise(control, self.record(control), 60, keep_running=True,
                                   runtime_factory=lambda _: AdapterInference(control.deployment, **kwargs))
                self.assertTrue(control.stopped)

    def test_wrong_base_lineage_cannot_pass(self):
        control = AdapterControl()
        record = self.record(control)
        record['expected_base_model_version_id'] = str(uuid.uuid4())
        with patch('builtins.print'), self.assertRaisesRegex(RuntimeError, 'base_model_version_id'):
            smoke.exercise(control, record, 60, runtime_factory=lambda _: AdapterInference(control.deployment))
        self.assertTrue(control.stopped)


if __name__ == '__main__':
    unittest.main()
