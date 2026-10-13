import unittest
from unittest.mock import patch
import urllib.error
import uuid

import live_serving_smoke as smoke


class FakeControl:
    def __init__(self):
        self.deployment = str(uuid.uuid4())
        self.operation = str(uuid.uuid4())
        self.keys = []
        self.stopped = False
        self.stop_error = False
        self.submission_lost = False

    def call(self, method, path, body=None, key=None):
        if path == '/api/deployments':
            self.keys.append(key)
            if self.submission_lost:
                self.submission_lost = False
                raise urllib.error.URLError('reply lost')
            return {'deployment_id': self.deployment, 'operation_id': self.operation, 'status': 'pending'}
        if path.endswith('/stop'):
            if self.stop_error:
                raise RuntimeError('stop unavailable')
            self.stopped = True
            return {}
        if path.startswith('/api/operations/'):
            return {'status': 'succeeded'}
        return {'operation_id': self.operation, 'status': 'stopped' if self.stopped else 'ready',
                'runtime_state': 'stopped' if self.stopped else 'ready', 'serving': {'engine': 'vllm'},
                'endpoint': 'http://127.0.0.1:18000/v1', 'served_model_name': 'test-model',
                'runtime_container_id': 'container', 'runtime_image_id': 'image'}


class FakeInference:
    def __init__(self, failure=False):
        self.failure = failure

    def call(self, method, path, body=None):
        if path == '/models':
            return {'data': [{'id': 'test-model'}]}
        if self.failure:
            raise RuntimeError('inference unavailable')
        return {'model': body['model'], 'choices': [{'message': {'content': 'Hello'}}]}


class LiveServingChecks(unittest.TestCase):
    def record(self):
        return {'idempotency_key': str(uuid.uuid4()), 'request': {'serving': {'engine': 'vllm'}}}

    def test_lost_submission_reuses_key_and_stops_after_inference(self):
        control = FakeControl()
        control.submission_lost = True
        record = self.record()
        with patch.object(smoke.time, 'sleep'), patch('builtins.print'):
            result = smoke.exercise(control, record, 60, runtime_factory=lambda _: FakeInference())
        self.assertEqual(control.keys, [record['idempotency_key']] * 3)
        self.assertTrue(result['passed'])
        self.assertTrue(result['stopped'])

    def test_inference_failure_still_stops_when_keep_running_requested(self):
        control = FakeControl()
        record = self.record()
        with patch('builtins.print'), self.assertRaisesRegex(RuntimeError, 'inference unavailable'):
            smoke.exercise(control, record, 60, runtime_factory=lambda _: FakeInference(True), keep_running=True)
        self.assertTrue(control.stopped)
        self.assertFalse(record['passed'])

    def test_cleanup_failure_cannot_report_success(self):
        control = FakeControl()
        control.stop_error = True
        with patch('builtins.print'), self.assertRaisesRegex(RuntimeError, 'stop unavailable'):
            smoke.exercise(control, self.record(), 60, runtime_factory=lambda _: FakeInference())

    def test_foreign_endpoint_and_identity_are_rejected(self):
        for url in ('http://example.com:8000/v1', 'http://0.0.0.0:8000/v1',
                    'http://user@127.0.0.1:18000/v1', 'http://127.0.0.1:18000/v1?key=x'):
            with self.assertRaises(RuntimeError):
                smoke.runtime_endpoint(url)
        a = {'deployment_id': str(uuid.uuid4()), 'operation_id': str(uuid.uuid4())}
        b = dict(a, operation_id=str(uuid.uuid4()))
        with self.assertRaises(RuntimeError):
            smoke.same_request_identity(a, b)

    def test_gateway_chat_is_used_and_wrong_model_fails_with_cleanup(self):
        for wrong_model in (False, True):
            with self.subTest(wrong_model=wrong_model):
                class GatewayControl(FakeControl):
                    chats = 0
                    def call(self, method, path, body=None, key=None):
                        if path.endswith('/chat'):
                            self.chats += 1
                            if 'model' in body: raise AssertionError('caller may not choose runtime alias')
                            return {'deployment_id':self.deployment,'model':'base' if wrong_model else 'test-model',
                                    'content':'Hello','finish_reason':'stop'}
                        return super().call(method,path,body,key)
                class ModelsOnly(FakeInference):
                    def call(self,method,path,body=None):
                        if method!='GET':raise AssertionError('chat bypassed Gateway')
                        return super().call(method,path,body)
                control=GatewayControl();record=dict(self.record(),via_gateway=True)
                with patch('builtins.print'):
                    if wrong_model:
                        with self.assertRaisesRegex(RuntimeError,'requested model'):
                            smoke.exercise(control,record,60,runtime_factory=lambda _:ModelsOnly())
                    else:
                        smoke.exercise(control,record,60,runtime_factory=lambda _:ModelsOnly())
                self.assertEqual(control.chats,1);self.assertTrue(control.stopped)
                self.assertEqual(record['passed'],not wrong_model)
                self.assertEqual(record['inference_transport'],'gateway')


if __name__ == '__main__':
    unittest.main()
