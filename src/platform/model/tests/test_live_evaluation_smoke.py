import copy
import math
import unittest
import live_evaluation_smoke as smoke


def fixture():
    version=dict(dataset_version_id='eval',sha256='a'*64,record_count=2,size_bytes=100,artifact_uri='s3://data/eval.jsonl')
    pin=dict(source_type='version',dataset_version_id='eval',sha256='a'*64,record_count=2,size_bytes=100,source_uri=version['artifact_uri'],format='chat_messages')
    result=dict(protocol='final_assistant_nll_v1',compatibility_sha256='b'*64,tokenized_sha256='c'*64,dataset_version_id='eval',dataset_sha256='a'*64,
                record_count=2,sequence_length=256,trained_steps=2,training_run_id='run',source_model_version_id='base',scored_tokens=4,nll_sum=8,loss=2,perplexity=math.exp(2),perplexity_overflow=False)
    run=dict(training_run_id='run',source_model_version_id='base',output_model_version_id='adapter',evaluation={'dataset':pin},training={'max_steps':2,'sequence_length':256},result={'evaluation':result})
    view=dict(training_run_id='run',source_model_version_id='base',output_model_version_id='adapter',state='ready',result_origin='training',evaluation={'dataset':pin},result=result)
    return view,run,version


class EvaluationSmokeTests(unittest.TestCase):
    def test_committed_full_evaluation_accepted(self):
        smoke.assert_evaluation(*fixture())

    def test_partial_stale_or_invalid_numeric_result_rejected(self):
        for change in ({'record_count':1},{'trained_steps':1},{'scored_tokens':0},{'nll_sum':7},{'loss':float('nan')},{'perplexity':0},{'dataset_sha256':'other'},{'training_run_id':'other'}):
            view,run,version=copy.deepcopy(fixture());view['result'].update(change)
            with self.subTest(change=change),self.assertRaises(RuntimeError):smoke.assert_evaluation(view,run,version)
        view,run,version=copy.deepcopy(fixture());view['state']='pending'
        with self.assertRaises(RuntimeError):smoke.assert_evaluation(view,run,version)

    def test_same_conditions_and_requested_row_order_required(self):
        first,_,_=fixture();second=copy.deepcopy(first);second['training_run_id']='second';second['result']['training_run_id']='second'
        second['result'].update(loss=1,nll_sum=4,perplexity=math.e)
        comparison={'comparable':True,'reason':'','items':[dict(first,loss_delta_from_first=0),dict(second,loss_delta_from_first=-1)]}
        smoke.assert_comparison(comparison,[first,second])
        for change in ('conditions','order','delta','comparable'):
            broken=copy.deepcopy(comparison);views=copy.deepcopy([first,second])
            if change=='conditions':views[1]['result']['tokenized_sha256']='d'*64;broken['items'][1]['result']=views[1]['result']
            if change=='order':broken['items'].reverse()
            if change=='delta':broken['items'][1]['loss_delta_from_first']=0
            if change=='comparable':broken['comparable']=False
            with self.subTest(change=change),self.assertRaises(RuntimeError):smoke.assert_comparison(broken,views)

    def test_mlflow_checks_values_steps_and_pin_tags(self):
        _,run,_=fixture();run['mlflow_run_id']='track';result=run['result']['evaluation']
        response={'run':{'info':{'run_id':'track','status':'FINISHED'},'data':{'metrics':[{'key':'heldout_'+k,'value':result[k],'step':2} for k in ('loss','perplexity','scored_tokens','record_count')],
                    'tags':[{'key':'heldout_dataset_version_id','value':'eval'},{'key':'heldout_compatibility_sha256','value':'b'*64}]}}}
        class Client:
            def call(self,*a):return response
        smoke.assert_tracking(Client(),run,result)
        response['run']['data']['metrics'][0]['step']=1
        with self.assertRaises(RuntimeError):smoke.assert_tracking(Client(),run,result)


if __name__=='__main__':unittest.main()
