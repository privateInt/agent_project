import importlib.util
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(os.environ.get('PLAYGROUND_SOURCE_DIR', Path(__file__).resolve().parents[1]/'playground'))
def load(name):
    spec=importlib.util.spec_from_file_location('playground_'+name, ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
api=load('api')
with patch.dict(sys.modules,api=api): state=load('state')
with patch.dict(sys.modules,api=api,state=state):
    actions=load('actions')
    presentation=load('presentation')
ID='19d02f67-9568-4802-aa0a-1495f2e3b28b'
DEP='d8be0fc3-6231-4a89-9360-78fc7404deaa'
OP='8cd5b0ac-4e04-4da1-8e4c-c56e0db53f6a'


class PlaygroundStateTests(unittest.TestCase):
    def test_legacy_embedding_is_not_chat_and_stopped_embedding_is_not_available(self):
        dep = {'status': 'ready', 'runtime_state': 'ready', 'desired_state': 'running', 'serving': {'engine': 'vllm', 'profile': ''}}
        info = {'model': {'model_type': 'llm'}, 'origin': {'repo_id': state.EMBEDDING_REPOSITORY}}
        self.assertFalse(state.can_chat(dep, info))
        self.assertTrue(state.can_embed(dep, info))
        self.assertEqual(state.status_text(dep, info=info)[0], '임베딩용')
        dep['desired_state'] = 'stopped'
        self.assertFalse(state.can_embed(dep, info))
        self.assertEqual(state.status_text(dep, info=info)[0], '종료 중')

    def test_embedding_deployment_and_training_choices_use_supported_origin(self):
        version = {'repo_id': state.EMBEDDING_REPOSITORY, 'artifact_kind': 'snapshot'}
        for kind in ('embedding', 'llm'):
            model = {'model_type': kind}
            self.assertTrue(state.can_deploy_model(model, version))
            self.assertFalse(state.can_train_model(model, version))
        self.assertFalse(state.can_deploy_model({'model_type': 'embedding'}, {'repo_id': 'other/model', 'artifact_kind': 'snapshot'}))
        dep = {'status': 'ready', 'runtime_state': 'ready', 'desired_state': 'running', 'serving': {'engine': 'vllm', 'profile': state.EMBEDDING_PROFILE}}
        self.assertTrue(state.can_embed(dep))
        self.assertFalse(state.can_chat(dep))

    def test_gpu_oom_explains_failure_without_suggesting_waiting(self):
        title, message = state.status_text({'runtime_state': 'failed', 'status': 'failed', 'runtime_error_code': 'GPU_OUT_OF_MEMORY'})
        self.assertEqual(title, 'GPU 메모리 부족')
        self.assertIn('자동 재시작하지 않습니다', message)

    def test_reload_preserves_start_key_and_fingerprint(self):
        intent=state.new_intent(ID,0)
        restored=state.decode_intent(state.encode_intent(intent))
        self.assertEqual(intent,restored)
        self.assertEqual(state.deployment_body(intent),state.deployment_body(restored))
        self.assertEqual(state.accept_created(intent,{'deployment_id':DEP,'operation_id':OP}), (DEP,OP))

    def test_untrusted_intent_cannot_override_runtime_settings(self):
        intent=state.new_intent(ID,0)
        for key,value in [('v',2),('v',True),('gpu_id',True),('gpu_id',-1),('key','invalid'),('model_version_id','../models'),('endpoint','http://elsewhere')]:
            with self.subTest(key=key,value=value),self.assertRaises((ValueError,TypeError)):
                state.decode_intent(state.encode_intent(dict(intent,**{key:value})))
        with self.assertRaises(ValueError):state.decode_intent('x'*1025)

    def test_malformed_start_reply_is_uncertain_and_retains_original_intent(self):
        intent=state.new_intent(ID,0);before=dict(intent)
        with self.assertRaises(api.APIError):state.accept_created(intent,{'deployment_id':DEP})
        self.assertEqual(intent,before)

    def test_chat_gated_by_current_runtime_and_stop_intent(self):
        dep={'status':'ready','runtime_state':'ready','desired_state':'running','serving':{'engine':'vllm'}}
        self.assertTrue(state.can_chat(dep))
        for key,value in [('status','applying'),('runtime_state','unknown'),('desired_state','stopped')]:
            self.assertFalse(state.can_chat(dict(dep,**{key:value})))
        self.assertEqual(state.status_text({'runtime_state':'unknown'})[0],'시작 확인 중')
        self.assertEqual(state.status_text({'runtime_state':'queued'},{'phase':'waiting_resources'})[0],'GPU 대기')

    def test_chat_history_is_not_truncated_or_mutated(self):
        history=[{'role':'user','content':'Hi'},{'role':'assistant','content':'Hello','truncated':True}]
        messages=state.answer_messages(history,'Why?')
        self.assertEqual(len(history),2);self.assertEqual(len(messages),3)
        self.assertNotIn('truncated',messages[1])
        for h,p in [(history*17,'Q'),(history,'x'*16000),(history,' ')]:
            with self.assertRaises(api.APIError):state.answer_messages(h,p)

    def test_chat_excludes_ocr_and_unknown_serving_profiles(self):
        dep={'status':'ready','runtime_state':'ready','desired_state':'running','serving':{'engine':'vllm','profile':''}}
        self.assertTrue(state.can_chat(dep))
        for serving in ({'engine':'vllm','profile':'surya-ocr-v2'}, {'engine':'vllm','profile':'embedding'},
                        {'engine':'demo'}, None, [], 'vllm'):
            self.assertFalse(state.can_chat(dict(dep,serving=serving)))
        self.assertEqual(state.status_text(dict(dep,serving={'engine':'vllm','profile':'surya-ocr-v2'}))[0], '글자 인식용')

    def test_base_or_empty_answer_cannot_pass(self):
        response={'deployment_id':DEP,'model':'deployment-'+DEP,'content':'Hello','finish_reason':'length'}
        self.assertTrue(state.accept_answer(DEP,response)['truncated'])
        for key,value in [('model','deployment-'+DEP+'-base'),('deployment_id',ID),('content',''),('finish_reason','tool_calls')]:
            with self.assertRaises(api.APIError):state.accept_answer(DEP,dict(response,**{key:value}))


class PlaygroundClientTests(unittest.TestCase):
    def test_only_server_configured_origin_and_api_paths(self):
        for url in ('file:///tmp/data','http://user:pass@host','http://host/path','http://host?key=secret'):
            with self.assertRaises(ValueError):api.API(url)
        with self.assertRaises(ValueError):api.API('http://gateway:8000').call('GET','http://elsewhere')

    def test_post_is_not_retried_and_carries_original_key(self):
        for failure in (URLError('private internal detail'), IncompleteRead(b'partial')):
            client=api.API('http://gateway:8000');seen=[]
            def fail(request,timeout):
                seen.append(request);raise failure
            with patch.object(client.opener,'open',side_effect=fail):
                with self.assertRaises(api.APIError) as caught:client.call('POST','/api/deployments',{'x':1},'original-key')
            self.assertEqual(len(seen),1);self.assertEqual(seen[0].get_header('Idempotency-key'),'original-key')
            self.assertNotIn('private',str(caught.exception))

    def test_error_body_is_not_exposed_and_redirects_are_refused(self):
        client=api.API('http://gateway:8000')
        error=HTTPError('http://gateway:8000',400,'bad',{},io.BytesIO(b'{"code":"CHAT_INPUT_REJECTED","error":"private filesystem path"}'))
        with patch.object(client.opener,'open',side_effect=error),self.assertRaises(api.APIError) as caught:
            client.call('POST','/api/deployments/'+DEP+'/chat',{})
        self.assertEqual(caught.exception.code,'CHAT_INPUT_REJECTED');self.assertNotIn('private',str(caught.exception))
        self.assertIsNone(api.NoRedirect().redirect_request(None,None,302,'',{},'http://elsewhere'))

    def test_listing_encodes_filters_and_preserves_cursor(self):
        client=api.API('http://gateway:8000')
        with patch.object(client,'call',return_value={'items':[]}) as call:
            client.listing('models',q='가 나&',cursor='a+b/=',limit=10)
        self.assertIn('cursor=a%2Bb%2F%3D',call.call_args.args[1])


class ConsoleActionTests(unittest.TestCase):
    def test_retry_ticket_contains_no_secret_or_uploaded_content(self):
        value=actions.payload('import',ID,{'version':'v1','hf_token':'hf_PRIVATE_TOKEN','repo_id':'private/repo'})
        saved=actions.ticket(value); restored=actions.decode(actions.encode(saved))
        self.assertEqual(saved,restored);self.assertEqual(actions.request_key(restored,value),saved['key'])
        self.assertNotIn('hf_PRIVATE_TOKEN',json.dumps(restored));self.assertNotIn('private/repo',json.dumps(restored))
        data=actions.payload('upload',ID,{'version':'v1'},b'private dataset contents')
        self.assertNotIn('private dataset',json.dumps(actions.ticket(data)))

    def test_changed_form_file_or_token_cannot_send_with_pending_key(self):
        original={'version':'v1','hf_token':'secret'}
        value=actions.payload('import',ID,original); saved=actions.ticket(value)
        original['version']='edited'
        self.assertEqual(value['body']['version'],'v1')
        changed=actions.payload('import',ID,{'version':'v1','hf_token':'different'})
        class Client:
            def call(self,*a,**kw):raise AssertionError('must reject before HTTP')
        with self.assertRaises(api.APIError):actions.execute(Client(),saved,changed)
        uploaded=actions.payload('upload',ID,{'version':'v1'},b'one')
        with self.assertRaises(api.APIError):actions.request_key(actions.ticket(uploaded),actions.payload('upload',ID,{'version':'v1'},b'two'))

    def test_ticket_rejects_unknown_action_invalid_target_and_schema(self):
        saved=actions.ticket(actions.payload('train',ID,{}))
        for key,value in [('v',True),('kind','http://elsewhere'),('target','../files'),('key','bad'),('sha256','0'*63),('body',{})]:
            with self.subTest(key=key),self.assertRaises((ValueError,TypeError)):
                actions.decode(actions.encode(dict(saved,**{key:value})))

    def test_invalid_success_keeps_request_replayable(self):
        value=actions.payload('deploy',ID,{'model_version_id':ID});saved=actions.ticket(value)
        class Client:
            def call(self,*a,**kw):return {'deployment_id':DEP}
        with self.assertRaises(api.APIError):actions.execute(Client(),saved,value)
        self.assertEqual(actions.request_key(saved,value),saved['key'])

    def test_upload_uses_exact_two_part_contract_and_preserves_bytes(self):
        from email import policy
        from email.parser import BytesParser
        client=api.API('http://gateway:8000');seen=[]
        def receive(request,timeout):
            seen.append(request)
            self.assertEqual(timeout,150)
            return io.BytesIO(json.dumps({'dataset_version_id':ID}).encode())
        data='{"messages":[{"role":"user","content":"가나다"}]}'.encode()
        with patch.object(client.opener,'open',side_effect=receive):
            client.upload_dataset(ID,' v1 ',data,'stable-key')
        self.assertEqual(len(seen),1);request=seen[0]
        self.assertEqual(request.get_header('Idempotency-key'),'stable-key')
        message=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+request.get_header('Content-type')+'\r\n\r\n').encode()+request.data)
        parts=list(message.iter_parts());self.assertEqual(len(parts),2)
        self.assertEqual([p.get_param('name',header='content-disposition') for p in parts],['version','file'])
        self.assertEqual(parts[0].get_payload(decode=True),b'v1')
        self.assertEqual(parts[1].get_payload(decode=True),data)
        self.assertEqual(parts[1].get_filename(),'dataset.jsonl')

    def test_upload_rejects_missing_file_and_retry_preserves_request_key(self):
        client=api.API('http://gateway:8000')
        for version,data in [('',b'a'),('v1',b''),('v1','wrong type')]:
            with self.assertRaises(api.APIError):client.upload_dataset(ID,version,data,'key')
        value=actions.payload('upload',ID,{'version':'v1'},b'data');saved=actions.ticket(value);seen=[]
        class Client:
            def upload_dataset(self,*args):
                seen.append(args);return {'dataset_version_id':DEP,'dataset_id':ID}
        actions.execute(Client(),saved,value);actions.execute(Client(),actions.decode(actions.encode(saved)),value)
        self.assertEqual(seen[0],seen[1])


class CombinedAdditionTests(unittest.TestCase):
    def value(self, kind):
        return actions.payload(kind, body={'registration': {'name': 'my item'},
            'version': {'version': 'v1', 'repo_id': 'private/repo', 'hf_token': 'hf_SECRET'} if kind == 'add_model' else {'version': 'v1'}},
            data=b'private file' if kind == 'add_dataset' else None)

    def test_loss_after_either_commit_replays_one_parent_and_one_version(self):
        for kind in ('add_model', 'add_dataset'):
            for lose in ('parent', 'version'):
                with self.subTest(kind=kind, lose=lose):
                    stored = {}; counts = {'parent': 0, 'version': 0}; observed = []; lost = []
                    class Client:
                        def apply(self, stage, signature, key):
                            if key not in stored:
                                counts[stage] += 1
                                if stage == 'parent':
                                    result = {'model_id' if kind == 'add_model' else 'dataset_id': ID}
                                elif kind == 'add_model': result = {'model_version_id': DEP, 'operation_id': OP}
                                else: result = {'dataset_version_id': DEP, 'dataset_id': ID}
                                stored[key] = (signature, result)
                            if stored[key][0] != signature: raise AssertionError('changed request reused a key')
                            if stage == lose and not lost:
                                lost.append(True); raise api.APIError('response lost')
                            return stored[key][1]
                        def call(self, method, path, body, key):
                            observed.append((method, path, body, key))
                            return self.apply('parent' if path in ('/api/models', '/api/datasets') else 'version', (path, body), key)
                        def upload_dataset(self, target, version, data, key):
                            observed.append(('upload', target, version, data, key))
                            return self.apply('version', (target, version, data), key)
                    value = self.value(kind); saved = actions.ticket(value); client = Client()
                    with self.assertRaises(api.APIError): actions.execute(client, saved, value)
                    if lose == 'parent': self.assertEqual(counts['version'], 0)
                    result = actions.execute(client, actions.decode(actions.encode(saved)), value)
                    self.assertEqual(counts, {'parent': 1, 'version': 1})
                    self.assertEqual(result['model_id' if kind == 'add_model' else 'dataset_id'], ID)
                    self.assertEqual(len(stored), 2)
                    self.assertNotIn(saved['key'], stored)
                    self.assertNotIn('hf_SECRET', json.dumps(saved)); self.assertNotIn('private file', json.dumps(saved))

    def test_invalid_parent_response_cannot_start_import_or_upload(self):
        for kind in ('add_model', 'add_dataset'):
            calls = []
            class Client:
                def call(self, *args): calls.append(args); return {}
                def upload_dataset(self, *args): raise AssertionError('unconfirmed parent')
            value = self.value(kind)
            with self.assertRaises(api.APIError): actions.execute(Client(), actions.ticket(value), value)
            self.assertEqual(len(calls), 1)

    def test_changed_addition_input_is_rejected_before_any_stage(self):
        class Client:
            def call(self, *args): raise AssertionError('must not call API')
            def upload_dataset(self, *args): raise AssertionError('must not upload')
        for kind in ('add_model', 'add_dataset'):
            value = self.value(kind); saved = actions.ticket(value)
            changed = self.value(kind); changed['body']['registration']['name'] = 'different'
            with self.assertRaises(api.APIError): actions.execute(Client(), saved, changed)
            if kind == 'add_dataset':
                changed = self.value(kind); changed['data'] = b'different file'
                with self.assertRaises(api.APIError): actions.execute(Client(), saved, changed)
            else:
                changed = self.value(kind); changed['body']['version']['hf_token'] = 'changed'
                with self.assertRaises(api.APIError): actions.execute(Client(), saved, changed)


class ModelPresentationTests(unittest.TestCase):
    def test_requested_revision_and_resolved_revision_are_distinct(self):
        v = {'model_version_id': ID, 'artifact_kind': 'snapshot', 'repo_id': 'org/original',
             'requested_revision': 'release/v2', 'resolved_revision': 'a' * 40}
        result = presentation.origin(v, lambda _: self.fail('snapshot must not resolve parent'))
        self.assertEqual(result['requested_revision'], 'release/v2')
        self.assertEqual(result['resolved_revision'], 'a' * 40)
        self.assertIn('release/v2', presentation.revision_label(result))
        v.pop('requested_revision'); v.pop('resolved_revision')
        result = presentation.origin(v, lambda _: None)
        self.assertEqual(result['requested_revision'], '')
        self.assertEqual(result['resolved_revision'], '')
        self.assertNotIn('main', presentation.revision_label(result))
        self.assertIn('미확정', presentation.revision_label(result))

    def test_adapter_origin_uses_its_explicit_base_not_copied_labels(self):
        child = {'model_version_id': ID, 'artifact_kind': 'adapter', 'source_model_version_id': DEP,
                 'repo_id': 'stale/copied', 'requested_revision': 'wrong', 'resolved_revision': 'b' * 40}
        parent = {'model_version_id': DEP, 'artifact_kind': 'snapshot', 'repo_id': 'org/base',
                  'requested_revision': 'main', 'resolved_revision': 'a' * 40}
        seen = []
        def fetch(key):
            seen.append(key); return parent
        result = presentation.origin(child, fetch)
        self.assertEqual(seen, [DEP]); self.assertTrue(result['derived'])
        self.assertEqual(result['repo_id'], 'org/base')
        self.assertEqual(result['resolved_revision'], 'a' * 40)
        self.assertEqual(child['repo_id'], 'stale/copied')

    def test_missing_broken_and_cyclic_origin_never_guess_a_model(self):
        child = {'model_version_id': ID, 'artifact_kind': 'adapter', 'source_model_version_id': DEP}
        def offline(_): raise api.APIError('offline')
        cases = [({}, lambda _: None),
                 (child, offline),
                 (child, lambda _: {'model_version_id': OP, 'artifact_kind': 'snapshot', 'repo_id': 'wrong/model'}),
                 (child, lambda key: {'model_version_id': key, 'artifact_kind': 'adapter', 'source_model_version_id': ID if key == DEP else DEP}),
                 ({'model_version_id': ID, 'artifact_kind': 'adapter'}, lambda _: None)]
        for v, fetch in cases:
            with self.subTest(v=v):
                result = presentation.origin(v, fetch)
                self.assertTrue(result['problem']); self.assertFalse(result['repo_id'])
                self.assertFalse(result['requested_revision']); self.assertFalse(result['resolved_revision'])

    def test_generated_version_display_does_not_change_persisted_identity(self):
        v = {'model_version_id': ID, 'artifact_kind': 'adapter', 'version': 'train-' + OP}
        before = dict(v)
        self.assertEqual(presentation.version_label(v), '학습 결과 · ' + OP[:8])
        self.assertEqual(v, before)
        v['version'] = 'customer-v3'
        self.assertEqual(presentation.version_label(v), 'customer-v3')
        self.assertEqual(presentation.state_label('running'), '실행 중')


if __name__=='__main__':unittest.main()
