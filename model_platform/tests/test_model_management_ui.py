"""Focused UI boundary tests without installing Streamlit or contacting services."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('PLAYGROUND_SOURCE_DIR', Path(__file__).resolve().parents[1] / 'playground'))
def load(name):
    spec = importlib.util.spec_from_file_location('management_test_' + name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
class Rerun(Exception): pass
class ManagementUITests(unittest.TestCase):
    def setUp(self):
        self.data = {'lists': {}, 'ids': {}, 'display_cache': {}}
        self.calls = []; self.result = {}; self.fail = False
        self.api = load('api')
        with patch.dict(sys.modules, api=self.api): self.state = load('state')
        def call(method, path, body=None, **kwargs):
            self.calls.append((method, path, body))
            if self.fail: raise self.api.APIError('lost response', code='CONNECTION_ERROR')
            return self.result
        self.client = SimpleNamespace(base='http://localhost:13000', call=call)
        def rerun(): raise Rerun()
        self.st = SimpleNamespace(fragment=lambda **kw: lambda fn: fn, dialog=lambda *a, **kw: lambda fn: fn, session_state={}, rerun=rerun)
        self.ui = SimpleNamespace(state=lambda: self.data, client=lambda: self.client, api=self.api, sync_url=lambda: None)
        with patch.dict(sys.modules, api=self.api, state=self.state, streamlit=self.st, ui=self.ui, presentation=SimpleNamespace(), table_style=SimpleNamespace()):
            self.progress = load('progress_view')
            with patch.dict(sys.modules, progress_view=self.progress):
                embedding_view = load('embedding_view')
                with patch.dict(sys.modules, embedding_view=embedding_view, reranking_validation=load('reranking_validation')):
                    reranking_view = load('reranking_view')
                    with patch.dict(sys.modules, reranking_view=reranking_view):
                        self.models = load('model_views'); self.jobs = load('job_views')
        self.mid = '99999999-1111-4111-8111-111111111111'
        self.vid = '99999999-2222-4222-8222-222222222222'
        self.editor = {'gateway': self.client.base, 'model': {'model_id': self.mid, 'name': 'old'}, 'version': {'model_version_id': self.vid, 'version': 'v1'}}

    def test_compact_progress_never_claims_overall_percentage(self):
        op = {'status': 'running', 'progress': {'stage': 'downloading', 'bytes_completed': 42, 'bytes_total': 100, 'files_completed': 3, 'files_total': 8}}
        self.assertEqual(self.progress.compact_progress(op), '현재 파일 42% · 3/8개 저장')
        op['progress']['bytes_total'] = 0
        self.assertNotIn('%', self.progress.compact_progress(op))
        op['progress']['stage'] = 'uploading'
        self.assertNotIn('%', self.progress.compact_progress(op))
        op['status'] = 'failed'
        self.assertEqual(self.progress.compact_progress(op), '실패 처리 중')
        self.assertEqual(self.progress.compact_progress({'status': 'pending'}), '시작 대기')

    def test_rename_updates_only_label_for_captured_version_and_checks_receipt(self):
        self.data['ids']['version'] = self.mid  # Row selector changed after opening editor.
        self.result = {'model_version_id': self.vid, 'version': '새 버전'}
        with self.assertRaises(Rerun): self.models.save_label(self.editor, True, ' 새 버전 ')
        self.assertEqual(self.calls, [('PATCH', '/api/model-versions/' + self.vid, {'version': '새 버전', 'expected_version': 'v1'})])
        self.assertEqual(self.editor['version']['version'], '새 버전')
        self.result = {'model_id': self.vid, 'name': 'new'}
        with self.assertRaises(self.api.APIError): self.models.save_label(self.editor, False, 'new')
        self.assertEqual(self.editor['model']['name'], 'old')

    def test_rename_response_loss_preserves_request_and_never_retries_automatically(self):
        self.fail = True
        with self.assertRaises(self.api.APIError): self.models.save_label(self.editor, False, 'new')
        self.assertEqual(len(self.calls), 1); self.assertEqual(self.editor['model']['name'], 'old')
        self.fail = False; self.result = {'model_id': self.mid, 'name': 'new'}
        with self.assertRaises(Rerun): self.models.save_label(self.editor, False, 'new')
        self.assertEqual(self.calls[0], self.calls[1])

    def test_server_change_rejects_edit_and_deletion(self):
        self.client.base = 'http://elsewhere:13000'
        with self.assertRaises(self.api.APIError): self.models.save_label(self.editor, False, 'new')
        self.data['delete_deployment_gateway'] = self.editor['gateway']
        with self.assertRaises(self.api.APIError): self.jobs.remove_deployment(self.vid)
        self.assertFalse(self.calls)

    def test_delete_requires_confirmed_stop_and_keeps_registered_models(self):
        row = {'status': 'stopped', 'runtime_state': 'stopped', 'desired_state': 'stopped', 'serving': {'engine': 'vllm'}}
        self.assertTrue(self.jobs.can_delete_deployment(row))
        for key, value in [('status', 'stopping'), ('runtime_state', 'unknown'), ('desired_state', 'running'), ('deleted_at', 'timestamp')]:
            self.assertFalse(self.jobs.can_delete_deployment(dict(row, **{key: value})))
        self.data['delete_deployment_gateway'] = self.client.base
        self.data['lists'] = {'models': {'page': {'items': [{'model_id': self.mid}]}}, 'deployments': {'page': {'items': [{'deployment_id': self.vid}, {'deployment_id': self.mid}]}}}
        self.result = {'deployment_id': self.vid}  # Receipt missing deletion timestamp.
        with self.assertRaises(self.api.APIError): self.jobs.remove_deployment(self.vid)
        self.assertEqual(len(self.data['lists']['deployments']['page']['items']), 2)
        self.result['deleted_at'] = 'timestamp'
        with self.assertRaises(Rerun): self.jobs.remove_deployment(self.vid)
        self.assertEqual(self.data['lists']['models']['page']['items'], [{'model_id': self.mid}])
        self.assertEqual(self.data['lists']['deployments']['page']['items'], [{'deployment_id': self.mid}])

    def test_redeploy_preserves_adapter_or_legacy_blank_profile_and_excludes_runtime_ids(self):
        dep = {'model_version_id': self.vid, 'deployment_id': self.mid, 'artifact_kind': 'adapter',
               'runtime_container_id': 'old', 'operation_id': 'old', 'endpoint': 'old',
               'status': 'stopped', 'runtime_state': 'stopped', 'desired_state': 'stopped',
               'serving': {'engine': 'vllm', 'profile': '', 'max_model_len': 4096, 'gpu_memory_utilization': 0.6},
               'resources': {'gpu_ids': [3], 'gpu_count': 1, 'node_name': 'worker'}}
        body = self.jobs.redeployment_body(dep)
        self.assertEqual(set(body), {'model_version_id', 'serving', 'resources'})
        self.assertEqual(body['model_version_id'], self.vid)
        self.assertEqual(body['serving'], dep['serving']); self.assertEqual(body['resources'], dep['resources'])
        dep['resources']['gpu_ids'][0] = 4
        self.assertEqual(body['resources']['gpu_ids'], [3])
        for key in ('resources', 'model_version_id', 'serving'):
            incomplete = dict(dep); incomplete.pop(key)
            with self.assertRaises(self.api.APIError): self.jobs.redeployment_body(incomplete)

    def test_redeploy_rejects_gateway_change_before_any_request(self):
        saved = {'gateway': 'http://elsewhere:13000'}
        with self.assertRaises(self.api.APIError) as caught: self.jobs.confirm_redeployment(saved)
        self.assertEqual(caught.exception.code, 'SERVER_CHANGED'); self.assertFalse(self.calls)

if __name__ == '__main__': unittest.main()
