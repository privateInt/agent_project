import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import uuid

import live_dataset_smoke as smoke


class FakeDatasetAPI:
    def __init__(self):
        self.dataset_id, self.version_id, self.training_id, self.operation_id, self.output_id = [str(uuid.uuid4()) for _ in range(5)]
        self.calls = []
        self.version = None
        self.lose_upload_reply = False
        self.wrong_pin = False
        self.training_request = None

    def upload(self, dataset_id, version, content, key):
        digest = hashlib.sha256(content).hexdigest()
        if self.version is not None and self.version['sha256'] != digest:
            raise smoke.APIError(409, 'immutable version')
        self.version = dict(dataset_version_id=self.version_id, dataset_id=dataset_id, version=version,
                            state='ready', format='chat_messages', artifact_uri='s3://data/pinned/train.jsonl',
                            sha256=digest, size_bytes=len(content), record_count=2)
        if self.lose_upload_reply:
            self.lose_upload_reply = False
            raise urllib.error.URLError('response lost after commit')
        return self.version.copy()

    def call(self, method, path, body=None, key=None):
        self.calls.append((method, path, key))
        if path == '/api/datasets':
            return dict(dataset_id=self.dataset_id, **body)
        if path.startswith('/api/dataset-versions/'):
            return self.version.copy()
        if path == '/api/datasets/' + self.dataset_id + '/versions':
            return {'versions': [self.version.copy()]}
        if path == '/api/training-runs':
            self.training_request = body
            return dict(training_run_id=self.training_id, operation_id=self.operation_id, status='pending')
        if path.startswith('/api/training-runs/'):
            v = self.version
            pin = dict(source_type='version', source_uri=v['artifact_uri'], format='chat_messages',
                       dataset_version_id=self.version_id, sha256=v['sha256'],
                       size_bytes=v['size_bytes'], record_count=2)
            if self.wrong_pin:
                pin['sha256'] = 'wrong'
            result = dict(dataset_source_type='version', dataset_source_uri=v['artifact_uri'], dataset_format='chat_messages',
                          dataset_version_id=self.version_id, dataset_sha256=v['sha256'], dataset_bytes=v['size_bytes'], dataset_record_count=2)
            return dict(status='succeeded', dataset=pin, result=result, output_model_version_id=self.output_id)
        if path.startswith('/api/operations/'):
            return {'status': 'succeeded'}
        if path == '/api/model-versions/' + self.output_id:
            return dict(state='ready', artifact_kind='adapter', source_type='training', training_run_id=self.training_id,
                        source_model_version_id=self.training_request['source_model_version_id'])
        if path.startswith('/api/model-versions/'):
            return dict(model_version_id=path.rsplit('/', 1)[1], state='ready', artifact_kind='snapshot')
        raise AssertionError((method, path))


class DatasetSmokeTests(unittest.TestCase):
    def record(self, training=False):
        return dict(run_id=str(uuid.uuid4()), dataset_request={'name': 'test'}, gpu_id=0,
                    model_version_id=str(uuid.uuid4()) if training else None)

    def test_default_does_not_create_training(self):
        client, record = FakeDatasetAPI(), self.record()
        smoke.exercise(client, record, smoke.FIXTURE, 1, lambda _: None)
        self.assertTrue(record['passed'])
        self.assertTrue(record['registration_passed'])
        self.assertFalse(any(path == '/api/training-runs' for _, path, _ in client.calls))

    def test_upload_lost_response_recovers_same_version(self):
        client, record = FakeDatasetAPI(), self.record()
        client.lose_upload_reply = True
        with patch.object(smoke.time, 'sleep'):
            smoke.exercise(client, record, smoke.FIXTURE, 1, lambda _: None)
        self.assertEqual(record['version']['dataset_version_id'], client.version_id)

    def test_pinned_training_saves_request_before_submission(self):
        client, record = FakeDatasetAPI(), self.record(True)
        snapshots = []
        smoke.exercise(client, record, smoke.FIXTURE, 1, lambda value: snapshots.append(dict(value)))
        self.assertTrue(record['training_passed'])
        saved_before = next(value for value in snapshots if 'training_request' in value)
        self.assertNotIn('training', saved_before)
        self.assertEqual(record['output_model_version_id'], client.output_id)
        keys = [key for method, path, key in client.calls if method == 'POST' and path == '/api/training-runs']
        self.assertEqual(keys, [record['run_id'] + ':training'] * 2)

    def test_mismatched_training_pin_is_not_success(self):
        client, record = FakeDatasetAPI(), self.record(True)
        client.wrong_pin = True
        with self.assertRaisesRegex(RuntimeError, 'preserve'):
            smoke.exercise(client, record, smoke.FIXTURE, 1, lambda _: None)
        self.assertNotIn('training_passed', record)

    def test_registration_failure_does_not_start_gpu_work(self):
        client, record = FakeDatasetAPI(), self.record(True)
        with patch.object(client, 'upload', side_effect=smoke.APIError(400, 'invalid data')):
            with self.assertRaises(smoke.APIError):
                smoke.exercise(client, record, smoke.FIXTURE, 1, lambda _: None)
        self.assertIsNone(client.training_request)

    def test_retry_does_not_repeat_permanent_failure(self):
        with patch.object(smoke.time, 'sleep') as sleep:
            with self.assertRaises(smoke.APIError):
                smoke.retry(lambda: (_ for _ in ()).throw(smoke.APIError(409, 'conflict')))
            sleep.assert_not_called()


class AxolotlDatasetAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'jobs/training/axolotl/config_adapter.py'
        spec = importlib.util.spec_from_file_location('dataset_config_adapter', path)
        cls.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.adapter)

    def test_registered_dataset_uses_verified_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            local = workspace / 'train.jsonl'
            local.write_bytes(smoke.FIXTURE)
            for source, kind in [('version', 'registered_version_jsonl'), ('s3', 's3_jsonl')]:
                path, actual = self.adapter.materialize_dataset({'dataset': {'source_type': source,
                    'format': 'chat_messages', 'local_path': str(local)}}, workspace)
                self.assertEqual((path, actual), (local, kind))

    def test_missing_registered_file_does_not_fall_back_to_synthetic(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                self.adapter.materialize_dataset({'dataset': {'source_type': 'version', 'format': 'chat_messages',
                    'local_path': str(Path(directory) / 'missing.jsonl')}}, Path(directory))


if __name__ == '__main__':
    unittest.main()
