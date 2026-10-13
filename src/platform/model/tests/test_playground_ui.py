"""Real Streamlit AppTest against controlled Gateway responses; no GPU Jobs."""
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None

ROOT = Path(os.environ.get('PLAYGROUND_SOURCE_DIR', Path(__file__).resolve().parents[1] / 'playground'))
MODEL = '99999999-1111-4111-8111-111111111111'
MODEL2 = '99999999-2222-4222-8222-222222222222'
BASE = 'cceae526-7b7c-47d4-870b-117372b51885'
ADAPTER = '19d02f67-9568-4802-aa0a-1495f2e3b28b'
DEP = 'd8be0fc3-6231-4a89-9360-78fc7404deaa'
DEP2 = '306f2dc3-f68f-484c-959a-a318f63df2ee'
OP = '8cd5b0ac-4e04-4da1-8e4c-c56e0db53f6a'
RUN = '143181ab-26cc-4f01-adb5-78b345fb7eeb'
CHILD = '0a4a2aa0-bb2f-4aff-a267-bca30cb92b5a'
DATASET = '33be8bf6-b149-4502-b852-bb9382a9716a'
DATA = '37cf7472-3269-4075-bde8-8369df60ba87'
EVAL = '65d89dd3-19c9-49cc-8c67-64d2b907aaf5'


@unittest.skipIf(AppTest is None, 'Streamlit not installed; run in Playground image')
class PlaygroundUITests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT))
        import api
        self.calls = []; self.failed = ''; self.ready = True; self.stopped = False
        self.embedding = False
        self.kind = 'llm'; self.training_status = 'running'; self.paginate = False
        self.admin_name = 'e2e-model-1788416939'; self.metadata_failed = False
        self.operation_value = {'status': 'running', 'phase': 'execution_started'}
        self.version_state = 'ready'
        self.purged = set(); self.deleted = set(); self.stop_ids = set(); self.extra_model = False
        self.ocr_ids = set(); self.version_labels = {}; self.removed_deployments = set()
        self.serving_profile = ''; self.deployment_overrides = {}
        test = self

        class FakeAPI:
            base = 'http://127.0.0.1:13000'
            def deployment(self, id=DEP):
                stopped = id in test.stop_ids
                profile = 'surya-ocr-v2' if id in test.ocr_ids else test.serving_profile
                return {'deployment_id': id, 'model_version_id': BASE if test.embedding or id in test.ocr_ids else ADAPTER, 'operation_id': OP,
                        'status': 'stopped' if stopped else ('ready' if test.ready else 'applying'),
                        'runtime_state': 'stopped' if stopped else ('ready' if test.ready else 'queued'),
                        'desired_state': 'stopped' if stopped else 'running',
                        'serving': {'engine': 'vllm', 'profile': profile, 'max_model_len': 18000 if id in test.ocr_ids else 512 if profile == 'ko-reranker-v1' else 2048, 'gpu_memory_utilization': 0.5},
                        'resources': {'gpu_ids': [0], 'gpu_count': 1, 'node_name': ''},
                        'artifact_kind': 'snapshot' if test.embedding or id in test.ocr_ids else 'adapter', 'created_at': '2026-09-10T05:35:55Z',
                        **test.deployment_overrides.get(id, {})}

            def version(self, id):
                return {'model_version_id': id, 'model_id': MODEL, 'version': test.version_labels.get(id, '원본' if id == BASE else '학습 완료'),
                        'state': test.version_state, 'source_type': 'huggingface' if id == BASE else 'training',
                        'artifact_kind': 'snapshot' if id == BASE else 'adapter', 'operation_id': OP,
                        'repo_id': 'dragonkue/snowflake-arctic-embed-l-v2.0-ko' if test.embedding else 'google/gemma-3-1b-it', 'requested_revision': 'main', 'resolved_revision': 'a' * 40,
                        'source_model_version_id': '' if id == BASE else BASE}

            def run(self, id=RUN):
                return {'training_run_id': id, 'status': test.training_status, 'operation_id': OP,
                        'source_model_version_id': BASE, 'output_model_version_id': ADAPTER if test.training_status == 'succeeded' else '',
                        'training': {'max_steps': 1}, 'created_at': '2026-09-10T05:35:55Z'}

            def listing(self, collection, **filters):
                test.calls.append(('GET', '/api/' + collection, filters, None))
                if collection == 'models':
                    second = bool(filters.get('cursor'))
                    items = [{'model_id': MODEL2 if second else MODEL, 'name': '다음 모델' if second else test.admin_name, 'model_type': test.kind, 'description': '테스트 모델'}]
                    if test.extra_model and not second:
                        items.append({'model_id': MODEL2, 'name': '다른 모델', 'model_type': test.kind})
                    return {'items': [r for r in items if r['model_id'] not in test.purged and (filters.get('include_deleted') == 'true' or r['model_id'] not in test.deleted)],
                            'next_cursor': 'next+page/=' if test.paginate and not second else ''}
                if collection.startswith('models/') and collection.endswith('/versions'):
                    rows = [self.version(ADAPTER), self.version(BASE)]
                    for row in rows:
                        row.pop('requested_revision'); row.pop('resolved_revision')
                    return {'items': rows[:filters.get('limit', 10)], 'next_cursor': ''}
                if collection == 'datasets':
                    return {'datasets': [{'dataset_id': DATASET, 'name': '연습 데이터', 'description': ''}], 'next_after_id': ''}
                if collection.startswith('datasets/'):
                    return {'versions': [{'dataset_version_id': id, 'dataset_id': DATASET, 'version': label, 'state': 'ready',
                                           'sha256': digest * 64, 'record_count': 2, 'size_bytes': 180} for id, label, digest in [(DATA, 'train-v1', 'a'), (EVAL, 'eval-v1', 'b')]], 'next_after_id': ''}
                if collection == 'training-runs':
                    return {'items': [self.run(), self.run(CHILD)], 'next_cursor': ''}
                if collection.endswith('/logs'):
                    return {'availability': 'available', 'source': 'docker', 'stdout': 'training step 1', 'stderr': '', 'truncated': False}
                if collection == 'training-runs/compare':
                    return {'comparable': True, 'reason': '', 'items': []}
                if collection == 'deployments':
                    return {'items': [self.deployment(id) for id in (DEP, DEP2) if id not in test.removed_deployments], 'next_cursor': ''}
                raise AssertionError((collection, filters))

            def call(self, method, path, body=None, key=None, timeout=30):
                test.calls.append((method, path, body, key))
                if path.endswith('/embedding-profile') or path.endswith('/embeddings'):
                    id = path.split('/')[-2]
                    profile = {'deployment_id': id, 'capability': 'embedding', 'embedding_space_id': 'es_' + 'a'*64, 'dimension': 1024}
                    if method == 'GET': return profile
                    return {'profile': profile, 'role': body['role'], 'data': [{'id': 'preview-1', 'index': 0, 'embedding': [1.0] + [0.0]*1023, 'input_tokens': 10}]}
                if method == 'GET' and path.startswith('/api/model-versions/') and test.metadata_failed:
                    raise api.APIError('unavailable', code='CONNECTION_ERROR')
                if method == 'PATCH':
                    if path.startswith('/api/models/'):
                        test.admin_name = body['name']
                        result = {'model_id': path.rsplit('/', 1)[-1], 'name': test.admin_name}
                    else:
                        id = path.rsplit('/', 1)[-1]
                        test.version_labels[id] = body['version']
                        result = self.version(id)
                    if test.failed == 'rename_reply': raise api.APIError('lost response', code='CONNECTION_ERROR')
                    return result
                if method == 'DELETE' and path.startswith('/api/deployments/'):
                    id = path.rsplit('/', 1)[-1]
                    if id not in test.stop_ids: raise api.APIError('stop first', 409, 'FailedPrecondition')
                    test.removed_deployments.add(id)
                    if test.failed == 'deployment_delete_reply': raise api.APIError('lost response', code='CONNECTION_ERROR')
                    return {'deployment_id': id, 'deleted_at': '2026-09-18T00:00:00Z'}
                if method == 'DELETE':
                    model_id = path.rsplit('/', 1)[-1]
                    test.deleted.add(model_id)
                    if test.failed == 'delete_reply':
                        raise api.APIError('lost delete response', code='CONNECTION_ERROR')
                    return {'model_id': model_id, 'deleted_at': '2026-09-10T01:00:00Z'}
                if method == 'POST' and path.endswith('/purge'):
                    model_id = path.split('/')[-2]
                    if test.failed == 'purge': raise api.APIError('MODEL_DEPLOYED: stop runtime', 409, 'FailedPrecondition')
                    test.purged.add(model_id); test.deleted.add(model_id)
                    return {'model_id': model_id, 'deleted_at': '2026-09-16T10:00:00Z', 'purge_state': 'purged', 'purged_at': '2026-09-16T10:00:00Z'}
                if method == 'POST':
                    if test.failed and path.endswith(test.failed):
                        raise api.APIError('lost response', code='CONNECTION_ERROR')
                    if path == '/api/models': return {'model_id': MODEL}
                    if path.endswith('/imports/huggingface'): return {'model_version_id': BASE, 'operation_id': OP, 'status': 'pending'}
                    if path == '/api/datasets': return {'dataset_id': DATASET}
                    if path == '/api/training-runs': return {'training_run_id': RUN, 'operation_id': OP, 'status': 'pending'}
                    if path.endswith('/resume'): return {'training_run_id': CHILD, 'operation_id': OP, 'status': 'pending'}
                    if path.endswith('/output/retry'): return {'training_run_id': RUN, 'recovery_status': 'queued'}
                    if path.endswith('/cancel'):
                        test.training_status = 'cancelled'; return self.run()
                    if path == '/api/deployments': return {'deployment_id': DEP, 'operation_id': OP, 'status': 'pending'}
                    if path.endswith('/stop'):
                        test.stopped = True; test.stop_ids.add(path.split('/')[-2]); return self.deployment(path.split('/')[-2])
                    if path.endswith('/chat'):
                        id = path.split('/')[-2]
                        return {'deployment_id': id, 'model': 'deployment-' + id, 'content': 'Hello!', 'finish_reason': 'stop'}
                if path.startswith('/api/models/'):
                    return {'model_id': path.rsplit('/', 1)[-1], 'name': test.admin_name, 'model_type': test.kind, 'description': '', 'deleted_at': '2026-09-10T01:00:00Z' if path.rsplit('/', 1)[-1] in test.deleted else None}
                if path.startswith('/api/model-versions/'): return self.version(path.rsplit('/', 1)[1])
                if path == '/api/datasets/' + DATASET: return {'dataset_id': DATASET, 'name': '연습 데이터'}
                if path.startswith('/api/operations/'): return {'operation_id': OP, **test.operation_value}
                if path.endswith('/checkpoints'):
                    return {'availability': 'retained', 'checkpoints': [{'step': 2, 'max_steps': 4, 'resumable': True}]}
                if path.endswith('/output'):
                    return {'training_run_id': RUN, 'availability': 'retained', 'recovery_status': ''}
                if path.endswith('/evaluation'): return {'training_run_id': RUN, 'state': 'completed', 'result': {'loss': 2.0}}
                if path.startswith('/api/training-runs/'): return self.run(path.rsplit('/', 1)[1])
                if path.startswith('/api/deployments/'): return self.deployment(path.rsplit('/', 1)[1])
                raise AssertionError((method, path, body))

            def upload_dataset(self, dataset_id, version, data, key, timeout=150):
                test.calls.append(('UPLOAD', dataset_id, (version, data), key))
                if test.failed == 'upload':
                    raise api.APIError('lost upload response', code='CONNECTION_ERROR')
                return {'dataset_version_id': DATA, 'dataset_id': dataset_id, 'state': 'ready'}

        self.patch = patch.object(api, 'API', FakeAPI); self.patch.start()
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10).run()
        self.assert_clean()

    def tearDown(self):
        self.patch.stop(); sys.path.remove(str(ROOT))

    def assert_clean(self):
        self.assertFalse(self.app.exception, [e.message for e in self.app.exception])

    def widget(self, kind, label=None, key=None):
        matches = [w for w in getattr(self.app, kind) if (key is None or w.key == key) and (label is None or w.label == label)]
        self.assertEqual(len(matches), 1, (kind, label, key))
        return matches[0]

    def click(self, label=None, key=None):
        button = self.widget('button', label, key)
        self.assertFalse(button.disabled, label or key)
        button.click().run(); self.assert_clean()

    def page(self, value):
        self.widget('radio', key='console_nav').set_value('chat' if value == 'chat' else 'models').run(); self.assert_clean()

    def posts(self, suffix):
        return [r for r in self.calls if r[0] == 'POST' and r[1].endswith(suffix)]

    def choose_version(self, version=BASE):
        self.click('모델 조회'); self.click(key='model_' + MODEL); self.click('버전 조회')
        self.click(key='version_' + version)

    def test_two_pages_keep_all_management_sections_and_legacy_links(self):
        self.assertEqual(self.widget('radio', key='console_nav').options, ['관리', '채팅'])
        for label in ('모델 조회', '배포 조회', '데이터셋 조회', '학습 작업 조회'):
            self.widget('button', label)
        self.assertEqual(self.calls, [])
        for old in ('datasets', 'training', 'deployments'):
            self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
            self.app.query_params['page'] = old
            self.app.run(); self.assert_clean()
            self.assertEqual(self.app.session_state.console['page'], 'models')
            self.assertEqual(self.app.query_params['page'], ['models'])
        self.page('chat')
        self.assertFalse([b for b in self.app.button if b.label == '모델 추가'])
        self.assertFalse([c for c in self.calls if c[0] != 'GET'])

    def test_add_dialog_close_preserves_catalog_without_post(self):
        self.click('모델 조회')
        before = self.app.session_state.console['lists']['models']['page']
        self.click('모델 추가')
        self.assertEqual(len(self.app.get('dialog')), 1)
        self.assertTrue([b for b in self.app.button if b.key == 'model_' + MODEL])
        self.fill_model_add()
        self.click(key='close_add_model')
        self.assertIsNone(self.app.session_state.console['show_add'])
        self.assertEqual(self.app.session_state.console['lists']['models']['page'], before)
        self.assertFalse(self.posts('/api/models'))
        self.app.run(); self.assert_clean()
        self.assertFalse([b for b in self.app.button if b.label == '모델 추가하기'])
        self.click('데이터셋 추가')
        self.assertEqual(len(self.app.get('dialog')), 1)
        self.click(key='close_add_dataset')
        self.assertFalse(self.posts('/api/datasets'))

    def test_measured_progress_updates_and_failure_never_restarts_job(self):
        self.version_state = 'importing'
        self.operation_value = {'status': 'running', 'phase': 'transferring_files',
            'progress': {'stage': 'downloading', 'current_file': 'model.safetensors',
                         'bytes_completed': 512, 'bytes_total': 1024, 'files_completed': 2, 'files_total': 5}}
        self.choose_version()
        self.assertEqual(self.app.get('progress')[0].proto.value, 50)
        self.assertIn('전체 5개 중 2개 저장 완료', self.visible_text())
        self.operation_value['progress']['bytes_total'] = 0
        self.app.run(); self.assert_clean()
        self.assertFalse(self.app.get('progress'))
        self.assertIn('전체 크기 확인 불가', self.visible_text())
        self.version_state = 'failed'
        self.operation_value.update(status='failed', phase='failed', error_code='TRANSFER_FAILED', error_message='download model.safetensors: status 401')
        self.app.run(); self.assert_clean()
        self.assertFalse(self.app.get('progress'))
        self.assertTrue(any('Hugging Face 다운로드 권한' in e.value for e in self.app.error))
        self.assertFalse([c for c in self.calls if c[0] != 'GET'])

    def test_saved_detail_ids_do_not_render_panels_or_open_dialogs(self):
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
        self.app.query_params.update(page='models', deployment=DEP, dataset=DATASET, run=RUN)
        self.app.run(); self.assert_clean()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.app.get('dialog'))
        self.assertFalse(any(b.label in ('배포 중지', '학습 취소', '데이터 버전 추가하기') for b in self.app.button))
        self.app.run(); self.assert_clean()
        self.assertEqual(self.calls, [])

    def test_detail_close_preserves_list_and_stops_selected_reads(self):
        self.click('배포 조회')
        page = self.app.session_state.console['lists']['model_deployments']['page']
        self.click(key='dep_' + DEP2)
        self.assertEqual(len(self.app.get('dialog')), 1)
        self.assertEqual(self.app.session_state.console['detail_dialog'], {'kind': 'deployment', 'id': DEP2})
        self.click(key='close_detail_dialog')
        count = len([c for c in self.calls if c[1] == '/api/deployments/' + DEP2])
        self.app.run(); self.assert_clean()
        self.assertIsNone(self.app.session_state.console['detail_dialog'])
        self.assertFalse(any(b.label == '배포 중지' for b in self.app.button))
        self.assertEqual(len([c for c in self.calls if c[1] == '/api/deployments/' + DEP2]), count)
        self.assertEqual(self.app.session_state.console['lists']['model_deployments']['page'], page)
        self.click('데이터셋 조회'); self.click(key='ds_' + DATASET)
        self.assertEqual(self.app.session_state.console['detail_dialog']['kind'], 'dataset')
        self.widget('button', '데이터 버전 추가하기')
        self.click(key='close_detail_dialog')
        self.click('학습 작업 조회'); self.click(key='run_' + RUN)
        self.assertEqual(self.app.session_state.console['detail_dialog']['kind'], 'run')
        self.click('로그 조회')
        self.assertTrue(any('training step 1' in c.value for c in self.app.code))
        self.page('chat'); self.page('models')
        self.assertIsNone(self.app.session_state.console['detail_dialog'])
        self.assertFalse(any(b.label == '학습 취소' for b in self.app.button))
        self.assertFalse([c for c in self.calls if c[0] != 'GET'])

    def test_restored_pending_detail_opens_only_on_explicit_request(self):
        import actions
        for kind, target, body, data in [('resume', RUN, {'checkpoint_step': 2}, None),
                                         ('retry_output', RUN, None, None),
                                         ('upload', DATASET, {'version': 'v2'}, b'example')]:
            ticket = actions.ticket(actions.payload(kind, target, body, data))
            self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
            self.app.query_params.update(page='models', pending=actions.encode(ticket))
            self.app.run(); self.assert_clean()
            self.assertIsNone(self.app.session_state.console.get('detail_dialog'))
            self.click('요청 화면으로 이동')
            self.assertEqual(self.app.session_state.console['detail_dialog'],
                             {'kind': 'dataset' if kind == 'upload' else 'run', 'id': target})
            self.assertEqual(self.app.session_state.console['pending']['key'], ticket['key'])
            self.click(key='close_detail_dialog')
            self.assertIsNotNone(self.app.session_state.console['pending'])
        self.assertFalse([c for c in self.calls if c[0] != 'GET'])

    def test_explicit_query_pagination_and_model_click_never_mutate(self):
        self.assertEqual(self.calls, [])
        self.paginate = True; self.click('모델 조회'); self.click(key='next_models')
        self.assertEqual([c for c in self.calls if c[1] == '/api/models'][-1][2]['cursor'], 'next+page/=')
        self.click(key='prev_models'); self.click(key='model_' + MODEL); self.click('버전 조회')
        self.app.run(); self.assert_clean()
        self.assertFalse([r for r in self.calls if r[0] == 'POST'])

    def visible_text(self):
        return '\n'.join(str(w.value) for kind in ('text', 'caption', 'subheader', 'info', 'success') for w in getattr(self.app, kind))

    def test_origin_and_admin_identity_in_model_deployment_training_and_chat(self):
        self.choose_version(ADAPTER)
        self.assertIn('e2e-model-1788416939', self.visible_text())
        self.assertIn('기반 Hugging Face 원본: google/gemma-3-1b-it', self.visible_text())
        self.assertIn('요청 버전: main', self.visible_text())
        self.assertIn('a' * 40, self.visible_text())
        self.assertIn('플랫폼 버전: 학습 완료', self.visible_text())
        self.page('training'); self.click('학습 작업 조회'); self.click(key='run_' + RUN)
        self.assertIn('Hugging Face 원본: google/gemma-3-1b-it', self.visible_text())
        self.assertIn('학습 상태: 실행 중', self.visible_text())
        self.page('deployments'); self.click('배포 조회'); self.click(key='dep_' + DEP)
        self.assertIn('기반 Hugging Face 원본: google/gemma-3-1b-it', self.visible_text())
        self.click(key='row_chat_' + DEP)
        self.assertIn('a' * 40, self.visible_text())
        self.assertFalse([c for c in self.calls if c[0] == 'POST'])

    def test_failed_origin_lookup_is_explicit_and_query_refresh_recovers(self):
        self.metadata_failed = True; self.click('모델 조회')
        self.assertIn('원본 정보를 조회하지 못했습니다', self.visible_text())
        self.assertNotIn('요청 버전: main', self.visible_text())
        self.metadata_failed = False; self.click('모델 조회')
        self.assertIn('기반 원본 · ' + 'a' * 12, self.visible_text())
        self.assertFalse([c for c in self.calls if c[0] == 'POST'])

    def test_advanced_settings_start_collapsed_and_keep_original_defaults(self):
        self.choose_version(); self.click('학습 설정')
        advanced = [e for e in self.app.expander if e.label.startswith('고급 학습 설정')]
        self.assertEqual(len(advanced), 1); self.assertFalse(advanced[0].proto.expanded)
        self.widget('radio', key='training_source').set_value('내장 테스트 데이터').run()
        self.click('학습 시작')
        body = self.posts('/api/training-runs')[0][2]
        self.assertEqual(body['training'], {'method': 'qlora', 'sequence_length': 256, 'max_steps': 1,
                         'micro_batch_size': 1, 'gradient_accumulation_steps': 1, 'learning_rate': 0.0002,
                         'lora': {'r': 8, 'alpha': 16, 'dropout': 0.0}, 'sample_packing': False, 'checkpoint_steps': 0})
        self.assertEqual(body['source_model_version_id'], BASE)
        self.assertEqual(body['resources'], {'gpu_ids': [0]})

    def fill_model_add(self):
        self.widget('text_input', '등록 이름').set_value('업무용 Gemma')
        self.widget('text_input', key='add_repo').set_value('google/gemma-3-1b-it')
        self.widget('text_input', key='add_hf_token').set_value('hf_TEST_SECRET')

    def test_one_model_add_button_registers_and_imports_and_hides_token(self):
        self.click('모델 추가'); self.fill_model_add()
        self.assertFalse([c for c in self.calls if c[0] == 'POST'])
        self.click('모델 추가하기')
        self.assertEqual(len(self.posts('/api/models')), 1)
        request = self.posts('/imports/huggingface')[0]
        self.assertEqual(request[2]['hf_token'], 'hf_TEST_SECRET')
        self.assertNotEqual(request[3], self.posts('/api/models')[0][3])
        self.assertNotIn('hf_TEST_SECRET', str(dict(self.app.query_params)))
        self.assertIsNone(self.app.session_state.console['pending_payload'])
        self.assertEqual(self.app.session_state.console['ids']['version'], BASE)
        self.assertNotIn('add_hf_token', self.app.session_state)

    def test_incomplete_combined_forms_do_not_create_empty_registrations(self):
        self.click('모델 추가'); self.click('모델 추가하기')
        self.widget('text_input', '등록 이름').set_value('missing repo'); self.click('모델 추가하기')
        self.assertFalse(self.posts('/api/models'))
        self.page('datasets'); self.click('데이터셋 추가')
        self.widget('text_input', '데이터셋 이름').set_value('missing file'); self.click('데이터셋 추가하기')
        self.assertFalse(self.posts('/api/datasets'))

    def test_import_failure_reload_replays_same_registration_and_import_keys(self):
        self.click('모델 추가'); self.fill_model_add(); self.failed = '/imports/huggingface'
        self.click('모델 추가하기')
        first_parent = self.posts('/api/models')[0]; first_import = self.posts('/imports/huggingface')[0]
        self.assertEqual(self.app.session_state.console['ids']['model'], MODEL)
        self.assertIsNotNone(self.app.session_state.console['pending'])
        saved = dict(self.app.query_params)
        self.assertNotIn('hf_TEST_SECRET', str(saved))
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
        self.app.query_params.update(saved); self.app.run(); self.assert_clean()
        self.assertEqual(len(self.posts('/api/models')), 1)
        self.fill_model_add(); self.widget('text_input', key='add_repo').set_value('different/repo')
        self.click('모델 추가하기'); self.assertEqual(len(self.posts('/api/models')), 1)
        self.widget('text_input', key='add_repo').set_value('google/gemma-3-1b-it')
        self.failed = ''; self.click('모델 추가하기')
        self.assertEqual(self.posts('/api/models')[1], first_parent)
        self.assertEqual(self.posts('/imports/huggingface')[1], first_import)

    def test_existing_model_and_dataset_version_add_never_register_again(self):
        self.choose_version()
        self.widget('text_input', 'Hugging Face 저장소').set_value('google/gemma-3-1b-it')
        self.click('모델 버전 추가하기')
        self.assertFalse(self.posts('/api/models')); self.assertEqual(len(self.posts('/imports/huggingface')), 1)
        self.page('datasets'); self.click('데이터셋 조회'); self.click(key='ds_' + DATASET)
        with patch('streamlit.file_uploader', return_value=io.BytesIO(b'{"messages":[]}\n')):
            self.app.run(); self.click('데이터 버전 추가하기')
        self.assertFalse(self.posts('/api/datasets'))
        self.assertEqual(len([c for c in self.calls if c[0] == 'UPLOAD']), 1)

    def test_deploy_selected_model_then_chat_separately_and_stop(self):
        self.choose_version(ADAPTER); self.click('배포 설정'); self.click('배포 시작')
        self.assertEqual(self.posts('/api/deployments')[0][2]['model_version_id'], ADAPTER)
        self.assertEqual(self.app.session_state.console['page'], 'models')
        self.assertEqual(len(self.app.chat_input), 0)
        self.click('이 배포와 채팅')
        self.app.chat_input[0].set_value('Hello').run(); self.assert_clean()
        self.app.chat_input[0].set_value('Again').run(); self.assert_clean()
        self.assertEqual(len(self.posts('/chat')[-1][2]['messages']), 3)
        self.assertEqual(len(self.posts('/api/deployments')), 1)
        self.click('관리에서 배포 보기'); self.click('배포 중지')
        self.assertTrue(self.stopped); self.assertTrue(self.widget('button', '이 배포와 채팅').disabled)

    def test_pinned_training_with_separate_evaluation_and_result_page(self):
        self.choose_version(); self.click('학습 설정')
        self.click(key='query_train_datasets'); self.click(key='query_train_versions_' + DATASET)
        self.widget('checkbox', '별도 데이터셋으로 평가').check().run()
        self.click(key='query_eval_datasets'); self.click(key='query_eval_versions_' + DATASET)
        self.widget('selectbox', key='eval_version_' + DATASET).set_value(EVAL).run()
        self.click('학습 시작')
        body = self.posts('/api/training-runs')[0][2]
        self.assertEqual(body['source_model_version_id'], BASE)
        self.assertEqual(body['dataset']['dataset_version_id'], DATA)
        self.assertEqual(body['evaluation']['dataset']['dataset_version_id'], EVAL)
        self.assertEqual(self.app.session_state.console['ids']['run'], RUN)
        self.click('로그 조회'); self.assertTrue(any('training step 1' in c.value for c in self.app.code))
        self.click('평가 결과 조회'); self.click('학습 취소')
        self.assertEqual(len(self.posts('/cancel')), 1)
        self.app.run(); self.assertEqual(len(self.posts('/api/training-runs')), 1)

    def test_synthetic_training(self):
        self.choose_version(); self.click('학습 설정')
        self.widget('radio', key='training_source').set_value('내장 테스트 데이터').run()
        self.click('학습 시작')
        self.assertEqual(self.posts('/api/training-runs')[0][2]['dataset']['source_type'], 'synthetic')

    def test_missing_dataset_and_same_evaluation_data_never_start_training(self):
        self.choose_version(); self.click('학습 설정'); self.click('학습 시작')
        self.assertFalse(self.posts('/api/training-runs'))
        self.click(key='query_train_datasets'); self.click(key='query_train_versions_' + DATASET)
        self.widget('checkbox', '별도 데이터셋으로 평가').check().run()
        self.click(key='query_eval_datasets'); self.click(key='query_eval_versions_' + DATASET)
        self.click('학습 시작')
        self.assertTrue(any('내용이 다른' in e.value for e in self.app.error))
        self.assertFalse(self.posts('/api/training-runs'))

    def test_one_dataset_add_button_registers_and_uploads(self):
        self.page('datasets'); self.click('데이터셋 추가')
        with patch('streamlit.file_uploader', return_value=io.BytesIO(b'{"messages":[]}\n')):
            self.app.run(); self.assert_clean()
            self.widget('text_input', '데이터셋 이름').set_value('Test data')
            self.assertFalse([c for c in self.calls if c[0] in ('POST', 'UPLOAD')])
            self.click('데이터셋 추가하기')
        uploaded = [c for c in self.calls if c[0] == 'UPLOAD']
        self.assertEqual(len(self.posts('/api/datasets')), 1)
        self.assertEqual(len(uploaded), 1); self.assertEqual(uploaded[0][1], DATASET)
        self.assertEqual(uploaded[0][2], ('v1', b'{"messages":[]}\n'))
        self.assertNotEqual(uploaded[0][3], self.posts('/api/datasets')[0][3])
        self.assertNotIn('messages', str(dict(self.app.query_params)))

    def test_upload_failure_requires_click_and_preserves_both_keys_and_file(self):
        self.page('datasets'); self.click('데이터셋 추가'); self.failed = 'upload'
        with patch('streamlit.file_uploader', return_value=io.BytesIO(b'original file')):
            self.app.run(); self.widget('text_input', '데이터셋 이름').set_value('Test data')
            self.click('데이터셋 추가하기'); self.app.run(); self.assert_clean()
        self.assertEqual(len(self.posts('/api/datasets')), 1)
        self.assertEqual(self.app.session_state.console['ids']['dataset'], DATASET)
        self.assertNotIn('original file', str(dict(self.app.query_params)))
        self.failed = ''; self.click('추가 이어서 진행')
        parent = self.posts('/api/datasets'); uploaded = [c for c in self.calls if c[0] == 'UPLOAD']
        self.assertEqual(parent[0], parent[1]); self.assertEqual(uploaded[0], uploaded[1])

    def test_lost_reply_reload_requires_same_input_and_reuses_key(self):
        self.choose_version(ADAPTER); self.click('배포 설정')
        self.failed = '/api/deployments'; self.click('배포 시작')
        original = self.posts('/api/deployments')[0]; saved = dict(self.app.query_params)
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
        self.app.query_params.update(saved); self.app.run(); self.assert_clean()
        self.assertEqual(len(self.posts('/api/deployments')), 1)
        self.click('요청 화면으로 이동'); self.click('배포 설정')
        self.widget('selectbox', '최대 입력·응답 길이').set_value(4096).run()
        self.click('배포 시작'); self.assertEqual(len(self.posts('/api/deployments')), 1)
        self.widget('selectbox', '최대 입력·응답 길이').set_value(2048).run()
        self.failed = ''; self.click('배포 시작')
        self.assertEqual(self.posts('/api/deployments')[1][2:], original[2:])

    def test_legacy_start_url_restores_key_without_automatic_post(self):
        import state
        legacy = state.new_intent(ADAPTER, 0)
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
        self.app.query_params['request'] = state.encode_intent(legacy)
        self.app.run(); self.assert_clean(); self.assertFalse(self.posts('/api/deployments'))
        self.click('같은 요청 다시 확인')
        self.assertEqual(self.posts('/api/deployments')[0][3], legacy['key'])
        self.assertEqual(self.posts('/api/deployments')[0][2], state.deployment_body(legacy))

    def test_chat_selection_cannot_create_job_and_history_does_not_leak(self):
        self.page('chat'); self.click('대화 가능한 배포 조회'); self.click(key='chat_dep_' + DEP)
        self.app.chat_input[0].set_value('Hello').run(); self.assert_clean()
        self.click(key='chat_dep_' + DEP2)
        self.assertEqual(self.app.session_state.console['messages'], [])
        self.assertFalse(self.posts('/api/deployments')); self.assertFalse(self.posts('/api/training-runs'))

    def test_legacy_embedding_does_not_appear_as_chat(self):
        self.embedding = True  # Existing registration may still say llm.
        self.page('chat'); self.click('대화 가능한 배포 조회')
        self.assertFalse([b for b in self.app.button if str(b.key).startswith('chat_dep_')])
        self.assertFalse(self.posts('/chat'))

    def test_embedding_detail_only_calls_api_after_explicit_submit(self):
        self.embedding = True
        self.page('models'); self.click('배포 조회')
        self.assertEqual(self.widget('button', key='row_chat_' + DEP).label, '임베딩')
        self.click(key='row_chat_' + DEP)
        self.assertTrue(self.widget('button', '이 배포와 채팅').disabled)
        self.assertFalse(self.posts('/embeddings'))
        self.click('임베딩 실행')
        requests = self.posts('/embeddings')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][2]['role'], 'query')
        self.assertEqual(requests[0][2]['expected_embedding_space_id'], 'es_' + 'a'*64)
        self.assertEqual(requests[0][2]['inputs'][0]['text'], '대한민국의 수도는 어디인가?')
        self.assertFalse(self.posts('/api/deployments'))
        self.assertTrue(any('1024차원' in item.value for item in self.app.success))

    def test_chat_list_excludes_ocr_while_management_keeps_it(self):
        self.ocr_ids.add(DEP2)
        self.page('chat'); self.click('대화 가능한 배포 조회')
        self.assertTrue([b for b in self.app.button if b.key == 'chat_dep_' + DEP])
        self.assertFalse([b for b in self.app.button if b.key == 'chat_dep_' + DEP2])
        self.assertFalse([c for c in self.calls if c[0] != 'GET'])
        self.page('models'); self.click('배포 조회')
        self.assertEqual(len(self.app.session_state.console['lists']['model_deployments']['page']['items']), 2)

    def test_ocr_direct_chat_link_cannot_send_or_retry(self):
        self.ocr_ids.add(DEP)
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
        self.app.query_params['deployment'] = DEP
        self.app.run(); self.assert_clean()
        self.assertTrue(self.app.chat_input[0].disabled)
        self.assertFalse([b for b in self.app.button if str(b.key).startswith('example_')])
        self.assertFalse(self.posts('/chat'))

    def test_chat_list_requires_llm_model_metadata(self):
        self.kind = 'embedding'
        self.page('chat'); self.click('대화 가능한 배포 조회')
        self.assertFalse([b for b in self.app.button if str(b.key).startswith('chat_dep_')])
        self.assertFalse(self.posts('/chat'))

    def test_unready_chat_disabled_and_failed_chat_requires_click_retry(self):
        self.ready = False
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
        self.app.query_params['deployment'] = DEP; self.app.run(); self.assert_clean()
        self.assertTrue(self.app.chat_input[0].disabled)
        self.ready = True; self.app.run(); self.failed = '/chat'
        self.app.chat_input[0].set_value('Hello').run(); self.assert_clean()
        self.app.run(); self.assertEqual(len(self.posts('/chat')), 1)
        self.assertEqual(self.app.session_state.console['messages'], [])
        self.failed = ''; self.click('같은 질문 다시 보내기')
        self.assertEqual(len(self.posts('/chat')), 2)

    def test_adapter_and_non_llm_training_are_disabled(self):
        self.choose_version(ADAPTER)
        self.assertTrue(self.widget('button', '학습 설정').disabled)
        self.kind = 'embedding'; self.app.run(); self.assert_clean()
        self.assertTrue(self.widget('button', '배포 설정').disabled)

    def test_toolbar_filter_edits_wait_for_query_and_footer_pins_search(self):
        self.paginate = True
        self.widget('text_input', '모델 검색').set_value('Gemma').run(); self.assert_clean()
        self.assertFalse(self.calls)
        self.click('모델 조회')
        self.widget('text_input', '모델 검색').set_value('other name').run()
        self.assertEqual(len([c for c in self.calls if c[1] == '/api/models']), 1)
        self.click(key='next_models')
        calls = [c for c in self.calls if c[1] == '/api/models']
        self.assertEqual(calls[-1][2]['q'], 'Gemma')
        self.assertEqual(calls[-1][2]['cursor'], 'next+page/=')
        self.click('모델 조회')
        calls = [c for c in self.calls if c[1] == '/api/models']
        self.assertEqual(calls[-1][2]['q'], 'other name'); self.assertEqual(calls[-1][2]['cursor'], '')
        self.assertFalse([c for c in self.calls if c[0] in ('POST', 'DELETE')])

    def test_model_rows_open_exact_version_forms_without_extra_navigation(self):
        self.click('모델 조회')
        self.assertFalse(self.widget('button', key='row_deploy_' + MODEL).disabled)
        self.assertTrue(self.widget('button', key='row_train_' + MODEL).disabled)
        self.assertFalse([w for w in self.app.text_input if w.label == 'Hugging Face 저장소'])
        self.widget('selectbox', key='row_version_' + MODEL).set_value(BASE).run()
        self.click(key='row_train_' + MODEL)
        self.assertFalse(self.posts('/api/training-runs'))
        self.widget('radio', key='training_source').set_value('내장 테스트 데이터').run()
        self.click('학습 시작')
        self.assertEqual(self.posts('/api/training-runs')[0][2]['source_model_version_id'], BASE)

    def test_model_row_deployment_pins_choice_and_version_change_closes_form(self):
        self.click('모델 조회'); self.click(key='row_deploy_' + MODEL)
        self.assertFalse(self.posts('/api/deployments'))
        self.widget('selectbox', key='row_version_' + MODEL).set_value(BASE).run(); self.assert_clean()
        self.assertFalse([b for b in self.app.button if b.label == '배포 시작'])
        self.click(key='row_deploy_' + MODEL); self.click('배포 시작')
        self.assertEqual(self.posts('/api/deployments')[0][2]['model_version_id'], BASE)

    def test_stale_catalog_cannot_open_execution_for_deleted_model(self):
        self.click('모델 조회'); self.deleted.add(MODEL)
        self.click(key='row_deploy_' + MODEL)
        self.assertFalse([b for b in self.app.button if b.label == '배포 시작'])
        self.assertFalse(self.posts('/api/deployments'))
        self.assertTrue(self.app.error)

    def test_model_files_purge_needs_consent_and_keeps_active_deployment_visible(self):
        self.click('모델 조회')
        self.click(key='row_delete_' + MODEL)
        self.app.selectbox(key='model_delete_mode_' + MODEL).set_value('파일까지 영구 삭제').run()
        self.assertTrue(self.app.button(key='confirm_delete_' + MODEL).disabled)
        self.assertFalse(self.posts('/purge'))
        self.app.checkbox(key='model_purge_confirm_' + MODEL).check().run()
        self.failed = 'purge'
        self.click(key='confirm_delete_' + MODEL)
        self.assertFalse(self.purged)
        self.assertFalse(self.posts('/stop'))
        self.failed = ''
        self.click(key='confirm_delete_' + MODEL)
        self.assertEqual(self.purged, {MODEL})
        self.assertEqual(len(self.posts('/purge')), 2)
        self.assertFalse(self.posts('/stop'))

    def test_delete_requires_confirmation_targets_one_model_and_keeps_deployments(self):
        self.extra_model = True
        self.click('모델 조회'); self.click('배포 조회')
        self.click(key='row_delete_' + MODEL2); self.app.run(); self.assert_clean()
        self.assertFalse([c for c in self.calls if c[0] == 'DELETE'])
        self.click(key='cancel_delete_' + MODEL2)
        self.click(key='row_delete_' + MODEL); self.click(key='confirm_delete_' + MODEL)
        self.assertEqual(self.deleted, {MODEL}); self.assertFalse(self.posts('/stop'))
        self.assertTrue(any(b.key == 'model_' + MODEL2 for b in self.app.button))
        self.assertFalse(any(b.key == 'model_' + MODEL for b in self.app.button))
        self.assertTrue(any(b.key == 'row_stop_' + DEP for b in self.app.button))
        self.app.run(); self.assertEqual(len([c for c in self.calls if c[0] == 'DELETE']), 1)

    def test_lost_delete_reply_only_retries_same_id_on_click(self):
        self.click('모델 조회'); self.failed = 'delete_reply'
        self.click(key='row_delete_' + MODEL); self.click(key='confirm_delete_' + MODEL)
        self.app.run(); self.assert_clean()
        self.assertEqual(len([c for c in self.calls if c[0] == 'DELETE']), 1)
        self.failed = ''; self.click(key='confirm_delete_' + MODEL)
        deletes = [c for c in self.calls if c[0] == 'DELETE']
        self.assertEqual(deletes[0], deletes[1]); self.assertEqual(self.deleted, {MODEL})

    def test_lower_deployment_rows_query_and_confirm_stop_one_runtime(self):
        self.click('배포 조회'); self.click(key='dep_' + DEP2)
        self.assertEqual(self.app.session_state.console['page'], 'models')
        self.assertFalse([c for c in self.calls if c[0] in ('POST', 'DELETE')])
        self.click(key='row_stop_' + DEP2); self.app.run()
        self.assertFalse(self.posts('/stop'))
        self.click(key='confirm_stop_' + DEP2)
        self.assertEqual(self.posts('/stop')[0][1], '/api/deployments/' + DEP2 + '/stop')
        self.assertFalse(self.widget('button', key='row_redeploy_' + DEP2).disabled)
        self.assertFalse(self.widget('button', key='row_stop_' + DEP).disabled)
        self.assertEqual(self.stop_ids, {DEP2})
        self.assertFalse(self.posts('/api/deployments'))

    def test_lost_stop_reply_does_not_repeat_on_refresh(self):
        self.click('배포 조회'); self.click(key='row_stop_' + DEP)
        self.failed = '/stop'; self.click(key='confirm_stop_' + DEP)
        self.app.run(); self.assert_clean(); self.assertEqual(len(self.posts('/stop')), 1)
        self.failed = ''; self.click(key='confirm_stop_' + DEP)
        self.assertEqual(self.posts('/stop')[0], self.posts('/stop')[1])

    def test_checkpoint_resume_creates_child_and_output_retry_is_distinct(self):
        self.training_status = 'failed'; self.page('training'); self.click('학습 작업 조회'); self.click(key='run_' + RUN)
        self.click('체크포인트 조회'); self.click('체크포인트에서 새 학습 시작')
        self.assertEqual(self.posts('/resume')[0][2], {'checkpoint_step': 2})
        self.assertEqual(self.app.session_state.console['ids']['run'], CHILD)
        self.click('학습 작업 조회'); self.click(key='run_' + RUN)
        self.click('출력 보존 상태 조회'); self.click('완료 출력 업로드 재시도')
        self.assertIsNone(self.posts('/output/retry')[0][2]); self.assertFalse(self.posts('/api/training-runs'))

    def test_evaluation_comparison_and_result_model_navigation_are_read_only(self):
        self.training_status = 'succeeded'; self.page('training'); self.click('학습 작업 조회')
        self.widget('multiselect', '비교할 작업 (2~5개)').set_value([RUN, CHILD]).run()
        self.click('평가 비교 조회'); self.click(key='run_' + RUN); self.click('학습 결과 모델 열기')
        self.assertEqual(self.app.session_state.console['ids']['version'], ADAPTER)
        self.assertFalse([c for c in self.calls if c[0] == 'POST'])

    def test_rename_preserves_selected_uuid_when_row_selection_changes(self):
        self.click('모델 조회')
        self.click(key='row_rename_' + MODEL)
        self.widget('selectbox', key='row_version_' + MODEL).set_value(BASE).run()
        self.widget('text_input', key='rename_version_' + ADAPTER).set_value('검증 버전').run()
        self.click('버전 이름 저장')
        patches = [c for c in self.calls if c[0] == 'PATCH']
        self.assertEqual(patches[-1][1:3], ('/api/model-versions/' + ADAPTER, {'version': '검증 버전', 'expected_version': '학습 완료'}))
        self.widget('text_input', key='rename_name_' + MODEL).set_value('새 등록 이름').run()
        self.click('등록 이름 저장')
        self.assertTrue(any(b.label == '새 등록 이름' for b in self.app.button))
        self.assert_clean()

    def test_row_import_progress_is_current_file_progress(self):
        self.version_state = 'importing'
        self.operation_value = {'status': 'running', 'progress': {'stage': 'downloading', 'current_file': 'weights', 'bytes_completed': 42, 'bytes_total': 100, 'files_completed': 3, 'files_total': 8}}
        self.click('모델 조회')
        self.assertTrue(any('현재 파일 42% · 3/8개 저장' in c.value for c in self.app.caption))
        self.assertTrue(self.widget('button', key='row_deploy_' + MODEL).disabled)
        self.assert_clean()

    def test_stopped_deployment_delete_is_separate_from_model_delete(self):
        self.stop_ids.add(DEP2)
        self.click('배포 조회')
        self.assertTrue(self.widget('button', key='row_delete_deployment_' + DEP).disabled)
        self.click(key='row_delete_deployment_' + DEP2)
        self.assertFalse(any(c[0] == 'DELETE' for c in self.calls))
        self.click(key='confirm_delete_deployment_' + DEP2)
        self.assertEqual([c[1] for c in self.calls if c[0] == 'DELETE'], ['/api/deployments/' + DEP2])
        self.assertFalse(self.deleted)
        self.assertFalse(any(b.key == 'row_delete_deployment_' + DEP2 for b in self.app.button))
        self.assert_clean()

    def test_redeploy_ocr_is_explicit_and_copies_stored_version_and_settings(self):
        self.stop_ids.add(DEP2); self.ocr_ids.add(DEP2)
        self.click('배포 조회'); self.click(key='row_redeploy_' + DEP2)
        self.app.run(); self.assert_clean()
        self.assertFalse(self.posts('/api/deployments'))
        self.click(key='confirm_redeploy_row_' + DEP2)
        posts = self.posts('/api/deployments')
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {'model_version_id': BASE,
            'serving': {'engine': 'vllm', 'profile': 'surya-ocr-v2', 'max_model_len': 18000, 'gpu_memory_utilization': 0.5},
            'resources': {'gpu_ids': [0], 'gpu_count': 1, 'node_name': ''}})
        self.assertEqual(self.app.session_state.console['ids']['deployment'], DEP)
        self.assertEqual(self.stop_ids, {DEP2}); self.assertFalse(self.removed_deployments)
        self.assertFalse([r for r in self.calls if r[0] == 'POST' and r[1] != '/api/deployments'])

    def test_redeploy_rechecks_state_at_open_and_confirmation(self):
        self.stop_ids.add(DEP2); self.click('배포 조회'); self.stop_ids.clear()
        self.click(key='row_redeploy_' + DEP2)
        self.assertFalse(self.posts('/api/deployments'))
        self.assertIsNone(self.app.session_state.console.get('redeploy'))
        self.stop_ids.add(DEP2); self.click('배포 조회'); self.click(key='row_redeploy_' + DEP2)
        self.stop_ids.clear(); self.click(key='confirm_redeploy_row_' + DEP2)
        self.assertFalse(self.posts('/api/deployments'))

    def test_redeploy_rejects_changed_settings(self):
        self.stop_ids.add(DEP2); self.click('배포 조회'); self.click(key='row_redeploy_' + DEP2)
        self.deployment_overrides[DEP2] = {'resources': {'gpu_ids': [1], 'gpu_count': 1, 'node_name': ''}}
        self.click(key='confirm_redeploy_row_' + DEP2)
        self.assertFalse(self.posts('/api/deployments'))
        self.assertTrue(any('배포 설정이 변경' in e.value for e in self.app.error))

    def test_redeploy_lost_reply_requires_explicit_replay_with_same_key(self):
        self.stop_ids.add(DEP2); self.serving_profile = 'snowflake-ko-v1'; self.embedding = True
        self.click('배포 조회'); self.click(key='row_redeploy_' + DEP2)
        self.failed = '/api/deployments'; self.click(key='confirm_redeploy_row_' + DEP2)
        self.app.run(); self.assert_clean()
        self.assertEqual(len(self.posts('/api/deployments')), 1)
        self.assertTrue(self.widget('button', key='confirm_redeploy_row_' + DEP2).disabled)
        self.failed = ''; self.click(key='retry_action')
        posts = self.posts('/api/deployments')
        self.assertEqual(posts[0], posts[1]); self.assertTrue(posts[0][3])
        self.assertEqual(posts[0][2]['serving']['profile'], 'snowflake-ko-v1')

    def test_redeploy_from_detail_preserves_reranker_profile(self):
        self.stop_ids.add(DEP2); self.serving_profile = 'ko-reranker-v1'
        self.click('배포 조회'); self.click(key='dep_' + DEP2)
        self.click(key='detail_redeploy_' + DEP2)
        self.assertFalse(self.posts('/api/deployments'))
        self.click(key='confirm_redeploy_detail_' + DEP2)
        body = self.posts('/api/deployments')[0][2]
        self.assertEqual(body['serving']['profile'], 'ko-reranker-v1')
        self.assertEqual(body['serving']['max_model_len'], 512)

    def test_redeploy_after_url_restore_reuses_pending_key(self):
        self.stop_ids.add(DEP2); self.ocr_ids.add(DEP2)
        self.click('배포 조회'); self.click(key='row_redeploy_' + DEP2)
        self.failed = '/api/deployments'; self.click(key='confirm_redeploy_row_' + DEP2)
        params = dict(self.app.query_params)
        self.app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
        self.app.query_params.update(params); self.app.run(); self.assert_clean()
        self.assertEqual(len(self.posts('/api/deployments')), 1)
        self.failed = ''; self.click('배포 조회'); self.click(key='row_redeploy_' + DEP2)
        self.click(key='confirm_redeploy_row_' + DEP2)
        posts = self.posts('/api/deployments'); self.assertEqual(posts[0], posts[1])

    def test_redeploy_cancel_and_stopping_or_deleted_have_no_post(self):
        self.stop_ids.add(DEP2); self.click('배포 조회'); self.click(key='row_redeploy_' + DEP2)
        self.click(key='cancel_redeploy_row_' + DEP2)
        self.assertIsNone(self.app.session_state.console.get('redeploy'))
        for override in ({'runtime_state': 'stopping'}, {'deleted_at': '2026-09-21T00:00:00Z'}):
            self.deployment_overrides[DEP2] = override; self.click('배포 조회')
            self.assertFalse(any(b.key == 'row_redeploy_' + DEP2 for b in self.app.button))
        self.assertFalse(self.posts('/api/deployments'))

    def test_redeploy_retry_rejects_changed_server(self):
        self.stop_ids.add(DEP2); self.click('배포 조회'); self.click(key='row_redeploy_' + DEP2)
        self.failed = '/api/deployments'; self.click(key='confirm_redeploy_row_' + DEP2)
        import api
        with patch.object(api.API, 'base', 'http://elsewhere:13000'):
            self.click(key='retry_action')
        self.assertEqual(len(self.posts('/api/deployments')), 1)
        self.assertTrue(any('서버 연결이 바뀌었습니다' in e.value for e in self.app.error))


if __name__ == '__main__':
    unittest.main()
