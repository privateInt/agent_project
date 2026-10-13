import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import live_training_output_smoke as smoke
from test_multi_gpu import adapter


class TrainingOutputSmokeTests(unittest.TestCase):
    def fixture(self):
        run = {'training_run_id': 'run', 'operation_id': 'op', 'status': 'failed',
               'result_json': '{"original":"failure"}', 'mlflow_run_id': 'tracking',
               'output_model_version_id': '', 'output_recovery_status': 'succeeded',
               'recovered_model_version_id': 'adapter'}
        op = {'operation_id': 'op', 'status': 'failed', 'phase': 'failed', 'attempt': 1,
              'error_code': 'TRAINING_ARTIFACT_PUBLISH_FAILED'}
        output = {'training_run_id': 'run', 'operation_id': 'op', 'recovery_status': 'succeeded',
                  'availability': 'retained', 'output_model_version_id': 'adapter',
                  'manifest_sha256': 'digest', 'file_count': 2, 'size_bytes': 9}
        model = {'model_version_id': 'adapter', 'state': 'ready', 'artifact_kind': 'adapter',
                 'source_type': 'training', 'training_run_id': 'run', 'source_model_version_id': 'base',
                 'artifact_manifest_sha256': 'digest', 'artifact_file_count': 2, 'artifact_size_bytes': 9}
        containers = [{'id': 'container', 'state': 'exited', 'running': False}]
        record = {'training_run_id': 'run', 'operation_id': 'op', 'saved_digest': 'digest',
                  'request': {'source_model_version_id': 'base'}, 'containers_before': containers,
                  'original_execution': smoke.execution_snapshot(run, op)}
        return record, run, op, output, model, containers

    def test_complete_recovery_evidence(self):
        smoke.check_recovered(*self.fixture())

    def test_retraining_or_rewritten_history_is_rejected(self):
        for target, change in [(1, {'status': 'succeeded'}), (2, {'attempt': 2}),
                               (2, {'status': 'succeeded'}), (1, {'mlflow_run_id': 'new'})]:
            values = list(copy.deepcopy(self.fixture()))
            values[target].update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                smoke.check_recovered(*values)
        values = list(self.fixture())
        values[-1] = [{'id': 'new-container', 'state': 'exited', 'running': False}]
        with self.assertRaises(RuntimeError):
            smoke.check_recovered(*values)

    def test_wrong_model_or_manifest_cannot_pass(self):
        for change in ({'state': 'importing'}, {'training_run_id': 'other'},
                       {'source_model_version_id': 'other'}, {'artifact_kind': 'snapshot'},
                       {'artifact_manifest_sha256': 'other'}, {'artifact_file_count': 3},
                       {'artifact_size_bytes': 0}):
            values = list(copy.deepcopy(self.fixture()))
            values[4].update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                smoke.check_recovered(*values)

    def test_prepare_requires_stopped_dispatcher_before_creation(self):
        with patch.object(smoke, 'dispatcher_stopped', side_effect=RuntimeError('running')):
            with self.assertRaises(RuntimeError):
                smoke.prepare(None, {}, lambda _: None)

    def test_resume_only_publishes_and_reuses_request_key(self):
        record, run, op, output, model, containers = self.fixture()
        record.update(recovery_key='saved-key', recovery_id='recovery')
        output['recovery_id'] = 'recovery'
        calls = []

        class Client:
            def call(self, method, path, body=None, key=None):
                calls.append((method, path, key))
                if path.startswith('/api/operations?'):
                    return {'items': [op], 'next_cursor': ''}
                if path == '/api/operations/op':
                    return op
                if path == '/api/model-versions/adapter':
                    return model
                if path == '/api/training-runs/run':
                    return run
                if path in ('/api/training-runs/run/output', '/api/training-runs/run/output/retry'):
                    return output
                raise AssertionError('unexpected action ' + method + ' ' + path)

        with patch.object(smoke, 'owned_containers', return_value=containers):
            smoke.recover(Client(), record, lambda _: None, 1)
        self.assertTrue(record['passed'])
        self.assertTrue(all(path.endswith('/output/retry') and key == 'saved-key'
                            for method, path, key in calls if method == 'POST'))

    def test_extra_operation_cannot_pass_no_retraining_check(self):
        class Client:
            def call(self, *args, **kwargs):
                return {'items': [{'operation_id': 'op'}, {'operation_id': 'another'}]}

        with self.assertRaises(RuntimeError):
            smoke.check_single_operation(Client(), self.fixture()[0])

    def test_config_writes_only_output_to_durable_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = root / 'model'
            model.mkdir()
            (model / 'config.json').write_text('{"model_type":"gemma3_text"}')
            workspace = root / 'temporary'
            workspace.mkdir()
            saved = root / 'durable' / 'output'
            saved.mkdir(parents=True)
            spec = dict(model_dir=str(model), output_dir=str(saved),
                        dataset=dict(source_type='synthetic', format='chat_messages'),
                        training=dict(method='qlora', sequence_length=256, sample_packing=False,
                                      lora=dict(r=8, alpha=16, dropout=0), gradient_accumulation_steps=1,
                                      micro_batch_size=1, max_steps=1, learning_rate=0.0002))
            result = adapter.render_config(spec, workspace)
            self.assertEqual(result['output_dir'], str(saved))
            self.assertEqual(Path(result['config_path']).parent, workspace)
            self.assertEqual(Path(result['dataset_path']).parent, workspace)
            content = Path(result['config_path']).read_text()
            self.assertIn('output_dir: ' + json.dumps(str(saved)), content)


if __name__ == '__main__':
    unittest.main()
