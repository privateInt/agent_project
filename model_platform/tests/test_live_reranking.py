import contextlib
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'playground'))
sys.path.insert(0, str(ROOT/'tests'))
import live_reranking as live
import reranking_validation as validation
import api
import state

DEP = '77a27d74-7515-4e61-85df-cd2584184359'

def profile():
    p = dict(deployment_id=DEP, model_version_id='fb77d339-9d27-4832-ab20-d90093e2cd44',
             profile_id=validation.PROFILE, serving_profile=validation.PROFILE,
             repo_id=validation.REPOSITORY, capability='reranker', status='ready',
             registered_model_type='llm', resolved_revision='c'*40,
             source_manifest_sha256='a'*64, tokenizer_sha256='b'*64,
             runtime_image_id='sha256:'+'d'*64, runtime_container_id='e'*64,
             served_model_name='deployment-'+DEP, score_kind='logit', score_direction='higher_is_better',
             pair_policy=validation.PAIR_POLICY, max_input_tokens=512, max_candidates=100,
             max_input_bytes=131072, truncation='reject')
    p['reranker_profile_id'] = validation.identity(p)
    return p


def result(p, ids=('relevant','unrelated')):
    return {'profile':deepcopy(p), 'data':[{'id':key,'index':i,'score':2.5 if key in ('relevant','candidate-1') else -3.0,'input_tokens':16} for i,key in enumerate(ids)]}


class RerankingTests(unittest.TestCase):
    def test_profile_identity_and_fixed_policy(self):
        p=profile();self.assertEqual(validation.validate_profile(p, DEP),p)
        for key,value in [('max_input_tokens',2048),('truncation','truncate'),('score_kind','probability'),('runtime_image_id',None),('model_version_id',3),('resolved_revision',[]),('tokenizer_sha256','f'*64)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validation.validate_profile(dict(p,**{key:value}),DEP)
        self.assertEqual(validation.identity(dict(p,deployment_id='other',runtime_container_id='f'*64)),p['reranker_profile_id'])

    def test_score_contract_rejects_malformed_and_changed_identity(self):
        p=profile();self.assertEqual(len(validation.validate_result(result(p),p,['relevant','unrelated'])),2)
        for key,value in [('id','wrong'),('index',True),('index',1),('score',None),('score',[]),('score',float('nan')),('score',True),('input_tokens',513),('input_tokens',True)]:
            bad=result(p);bad['data'][0][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError): validation.validate_result(bad,p,['relevant','unrelated'])
        for bad in [{'profile':p,'data':[]},result(dict(p,runtime_container_id='f'*64))]:
            with self.assertRaises(ValueError): validation.validate_result(bad,p,['relevant','unrelated'])

    def test_zero_score_is_valid(self):
        p=profile();r=result(p);r['data'][0]['score']=0
        self.assertEqual(validation.validate_result(r,p,['relevant','unrelated'])[0]['score'],0)

    def test_live_check_executes_only_explicit_inference_and_negative_probes(self):
        p=profile();calls=[]
        def call(suffix,body=None,expected=200,reason=None):
            calls.append((suffix,body,expected,reason))
            if suffix=='/reranker-profile':return p
            if expected!=200:return {'code':reason}
            return result(p,[c['id'] for c in body['candidates']])
        summary=live.check(call,DEP)
        self.assertEqual(summary['status'],'passed');self.assertEqual(summary['lifecycle_changes'],0)
        self.assertEqual(len(calls),8);self.assertEqual(sum(c[2]==200 and c[0]=='/rerank' for c in calls),2)
        self.assertNotIn('text',json.dumps(summary));self.assertNotIn('query',json.dumps(summary))
        self.assertEqual([c[2] for c in calls[1:6]],[409,400,400,400,400])

    def test_live_check_rejects_swapped_relevance(self):
        p=profile()
        def call(suffix,body=None,expected=200,reason=None):
            if suffix=='/reranker-profile':return p
            if expected!=200:return {}
            r=result(p);r['data'][0]['score']=-4;return r
        with self.assertRaisesRegex(ValueError,'relevance pair'):live.check(call,DEP)

    def test_strict_json_and_private_summary(self):
        for raw in ['{"a":1,"a":2}','{"x":{"s":1,"s":2}}','{"score":NaN}','{} {}']:
            with self.assertRaises(ValueError):live.decode(raw)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'summary.json';live.save_summary(path,{'status':'passed'})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(json.loads(path.read_text()),{'status':'passed'})

    def test_capabilities_do_not_treat_legacy_reranker_as_chat(self):
        version={'repo_id':validation.REPOSITORY,'artifact_kind':'snapshot'}
        for kind in ('llm','reranker'):
            self.assertTrue(state.can_deploy_model({'model_type':kind},version))
            self.assertFalse(state.can_train_model({'model_type':kind},version))
        self.assertFalse(state.can_deploy_model({'model_type':'llm'},dict(version,artifact_kind='adapter')))
        dep={'status':'ready','runtime_state':'ready','desired_state':'running','serving':{'engine':'vllm','profile':''}}
        info={'model':{'model_type':'llm'},'origin':version}
        self.assertFalse(state.can_chat(dep,info));self.assertFalse(state.can_rerank(dep,info))
        self.assertEqual(state.status_text(dep,info=info)[0],'재정렬 설정 필요')
        dep['serving']['profile']=validation.PROFILE
        self.assertTrue(state.can_rerank(dep,info));dep['desired_state']='stopped'
        self.assertFalse(state.can_rerank(dep,info))

    def test_ui_never_infers_on_render_and_drops_previous_on_failure(self):
        p=profile();data={'results':{}};calls=[];submitted=[False];fail=[False]
        dep={'deployment_id':DEP,'runtime_container_id':p['runtime_container_id'],'status':'ready','runtime_state':'ready','desired_state':'running','serving':{'engine':'vllm','profile':validation.PROFILE}}
        def call(method,path,body=None,**kwargs):
            calls.append((method,path,body))
            if fail[0]:raise api.APIError('failed',code='RERANK_UNAVAILABLE')
            return p if method=='GET' else result(p,['candidate-1','candidate-2'])
        st=SimpleNamespace(markdown=lambda *a:None,caption=lambda *a:None,form=lambda *a:contextlib.nullcontext(),
            text_area=lambda *a,**kw:kw['value'],form_submit_button=lambda *a,**kw:submitted[0],
            spinner=lambda *a:contextlib.nullcontext(),warning=lambda *a:None,success=lambda *a:None,table=lambda *a:None)
        ui=SimpleNamespace(state=lambda:data,client=lambda:SimpleNamespace(call=call),api=api,error=lambda *a:None,details=lambda *a:None)
        spec=importlib.util.spec_from_file_location('reranking_view_under_test',ROOT/'playground/reranking_view.py')
        view=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,streamlit=st,ui=ui):spec.loader.exec_module(view)
        view.render(dep,{});self.assertFalse(calls)
        submitted[0]=True;view.render(dep,{})
        self.assertEqual([x[0] for x in calls],['GET','POST']);self.assertEqual(len(data['results']),1)
        fail[0]=True;view.render(dep,{})
        self.assertEqual(data['results'],{});self.assertEqual(len(calls),3)


if __name__=='__main__':unittest.main()
