"""Two small GPU trainings on a pinned train/eval pair, then read-only comparison.

The second run also enables checkpoint saves. Timeouts retain exact request keys;
--resume observes/replays the same runs and never substitutes a new request.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import uuid

from live_dataset_smoke import DatasetAPI, retry, require, check_pin, observe_training
from live_serving_smoke import API, wait_for


def fixture(pairs):
    return b''.join((json.dumps({'messages':[{'role':'user','content':q},{'role':'assistant','content':a}]},separators=(',',':'))+'\n').encode() for q,a in pairs)

TRAIN = fixture([('What is 2 + 3?','5'),('What is 2 * 4?','8')])
EVALUATION = fixture([('What is 3 + 4?','7'),('What is 3 * 3?','9')])


def assert_evaluation(view, run, version):
    require(view.get('state')=='ready' and view.get('result_origin')=='training', 'evaluation is not a committed final training result')
    require(view.get('training_run_id')==run['training_run_id'] and view.get('source_model_version_id')==run['source_model_version_id']
            and view.get('output_model_version_id')==run.get('output_model_version_id'), 'evaluation output identity changed')
    check_pin({'dataset':(view.get('evaluation') or {}).get('dataset')},version)
    check_pin({'dataset':(run.get('evaluation') or {}).get('dataset')},version)
    result=view.get('result') or {}
    expected={'protocol':'final_assistant_nll_v1','dataset_version_id':version['dataset_version_id'],'dataset_sha256':version['sha256'],
              'record_count':version['record_count'],'sequence_length':run['training']['sequence_length'],
              'trained_steps':run['training']['max_steps'],'training_run_id':run['training_run_id'],'source_model_version_id':run['source_model_version_id']}
    require(all(result.get(k)==v for k,v in expected.items()),'evaluation did not cover the full requested dataset or final step')
    for key in ('compatibility_sha256','tokenized_sha256'):
        value=result.get(key);require(isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value),'evaluation digest missing: '+key)
    tokens=result.get('scored_tokens');nll=result.get('nll_sum');loss=result.get('loss')
    require(type(tokens) is int and version['record_count']<=tokens<=version['record_count']*(run['training']['sequence_length']-1),'invalid scored token count')
    require(all(type(v) in (int,float) and math.isfinite(v) and v>=0 for v in (nll,loss)),'invalid evaluation loss')
    require(math.isclose(loss,nll/tokens,rel_tol=1e-6,abs_tol=1e-6),'loss is not token weighted')
    if loss>700:require(result.get('perplexity') is None and result.get('perplexity_overflow') is True,'invalid perplexity overflow')
    else:
        perplexity=result.get('perplexity')
        require(type(perplexity) in (int,float) and math.isfinite(perplexity) and math.isclose(perplexity,math.exp(loss),rel_tol=1e-6)
                and result.get('perplexity_overflow') is False,'perplexity does not match loss')
    require((run.get('result') or {}).get('evaluation')==result,'REST evaluation differs from committed result')
    return result


def assert_comparison(comparison, views):
    require(comparison.get('comparable') is True and comparison.get('reason')=='','same pinned evaluation was not comparable')
    items=comparison.get('items') or [];require(len(items)==len(views),'comparison row count changed')
    for i,(item,view) in enumerate(zip(items,views)):
        require(item.get('training_run_id')==view['training_run_id'] and item.get('result')==view['result'],'comparison changed order or evidence')
        delta=item.get('loss_delta_from_first')
        expected=view['result']['loss']-views[0]['result']['loss']
        require(type(delta) in (int,float) and math.isclose(delta,expected,abs_tol=1e-8,rel_tol=1e-6),'incorrect comparison delta')
        for key in ('dataset_version_id','dataset_sha256','source_model_version_id','protocol','compatibility_sha256','tokenized_sha256','sequence_length','record_count','scored_tokens'):
            require(view['result'][key]==views[0]['result'][key],'comparison mixed different evaluation conditions')


def assert_tracking(mlflow, run, result):
    require(run.get('mlflow_run_id'),'training did not link MLflow')
    view=mlflow.call('GET','/api/2.0/mlflow/runs/get?run_id='+run['mlflow_run_id'])['run']
    require(view['info']['run_id']==run['mlflow_run_id'] and view['info']['status']=='FINISHED','MLflow run not finished')
    metrics={item['key']:item for item in view.get('data',{}).get('metrics',[])}
    for name in ('loss','scored_tokens','record_count','perplexity'):
        if result[name] is None:continue
        metric=metrics.get('heldout_'+name) or {}
        require(metric.get('step')==result['trained_steps'] and type(metric.get('value')) in (int,float)
                and math.isclose(metric['value'],result[name],rel_tol=1e-6,abs_tol=1e-8),'MLflow metric missing or changed: '+name)
    tags={item['key']:item['value'] for item in view.get('data',{}).get('tags',[])}
    require(tags.get('heldout_dataset_version_id')==result['dataset_version_id'] and tags.get('heldout_compatibility_sha256')==result['compatibility_sha256'],'MLflow evaluation tags changed')


def exercise(client, mlflow, record, timeout, save):
    source_id=record['model_version_id'];key=record['key']
    source=client.call('GET','/api/model-versions/'+source_id)
    require(source.get('model_version_id')==source_id and source.get('state')=='ready' and source.get('artifact_kind')=='snapshot','source must be a ready snapshot')
    dataset=retry(lambda:client.call('POST','/api/datasets',{'name':'evaluation-smoke-'+key},key+':dataset'))
    record['dataset']=dataset;save(record)
    versions=[]
    for label,content in (('train',TRAIN),('eval',EVALUATION)):
        version=retry(lambda:client.upload(dataset['dataset_id'],label,content,key+':'+label))
        require(version.get('state')=='ready' and version.get('sha256')==hashlib.sha256(content).hexdigest() and version.get('size_bytes')==len(content) and version.get('record_count')==2,'dataset upload identity mismatch')
        require(client.call('GET','/api/dataset-versions/'+version['dataset_version_id'])==version,'dataset pin lookup changed')
        record[label+'_version']=version;save(record);versions.append(version)
    train,ev=versions;views=[];runs=[]
    for index,steps in enumerate((1,2)):
        name='run'+str(index+1);entry=record.setdefault(name,{})
        request={'source_model_version_id':source_id,'dataset':{'source_type':'version','dataset_version_id':train['dataset_version_id']},
                 'evaluation':{'dataset':{'source_type':'version','dataset_version_id':ev['dataset_version_id']}},
                 'resources':{'gpu_ids':[record['gpu_id']]},
                 'training':{'method':'qlora','sequence_length':256,'max_steps':steps,'micro_batch_size':1,'gradient_accumulation_steps':1,
                             'learning_rate':.0002,'lora':{'r':8,'alpha':16,'dropout':0},'sample_packing':False}}
        if index==1:request['training']['checkpoint_steps']=1
        require(not entry.get('request') or entry['request']==request,'saved request changed; use the original smoke version')
        entry['request']=request;save(record)
        created=retry(lambda:client.call('POST','/api/training-runs',request,key+':'+name))
        require(not entry.get('created') or entry['created']==created,'training replay changed IDs')
        entry['created']=created;save(record);print('[evaluation-training] '+json.dumps(created,sort_keys=True),flush=True)
        replay=retry(lambda:client.call('POST','/api/training-runs',request,key+':'+name));require(replay==created,'training request is not idempotent')
        run_id,op_id=created['training_run_id'],created['operation_id']
        run=wait_for('evaluation-'+name,lambda:observe_training(client,run_id,op_id,entry,lambda _:save(record)),lambda v:v.get('status')=='succeeded',timeout)
        op=wait_for('evaluation-operation',lambda:client.call('GET','/api/operations/'+op_id),lambda v:v.get('status')=='succeeded',timeout)
        require(op.get('resource_id')==run_id,'Operation is not linked to this run')
        check_pin(run,train)
        output=client.call('GET','/api/model-versions/'+run['output_model_version_id'])
        require(output.get('state')=='ready' and output.get('artifact_kind')=='adapter' and output.get('training_run_id')==run_id and output.get('source_model_version_id')==source_id,'output is not the ready trained adapter')
        view=client.call('GET','/api/training-runs/'+run_id+'/evaluation');result=assert_evaluation(view,run,ev)
        assert_tracking(mlflow,run,result)
        if index==1:
            evidence=(run.get('result') or {}).get('checkpoint') or {}
            require(evidence.get('start_step')==0 and evidence.get('final_step')==2 and evidence.get('executed_steps')==2,'evaluation/checkpoint wrapper did not complete both steps')
        entry.update(run=run,evaluation=view,output=output,passed=True);save(record)
        views.append(view);runs.append(run);print('[evaluation] '+json.dumps(result,sort_keys=True),flush=True)
    path='/api/training-runs/compare?training_run_ids='+','.join(run['training_run_id'] for run in runs)
    comparison=client.call('GET',path);assert_comparison(comparison,views)
    for run in runs:require(client.call('GET','/api/training-runs/'+run['training_run_id'])==run,'comparison mutated training history')
    record.update(comparison=comparison,passed=True);save(record)
    print('[comparison] '+json.dumps(comparison,sort_keys=True),flush=True)
    print('[output_model_version_id] '+runs[-1]['output_model_version_id'],flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api',default='http://127.0.0.1:13000');parser.add_argument('--mlflow',default='http://127.0.0.1:5000')
    parser.add_argument('--model-version-id');parser.add_argument('--gpu-id',type=int,default=0)
    parser.add_argument('--timeout',type=int,default=600);parser.add_argument('--resume',type=Path)
    args=parser.parse_args()
    if args.timeout<=0 or args.gpu_id<0:parser.error('timeout must be positive and GPU ID nonnegative')
    if args.resume:
        if args.model_version_id:parser.error('--resume uses the saved source model')
        path=args.resume.resolve();record=json.loads(path.read_text());require(record.get('schema')==1,'unsupported report schema')
    else:
        if not args.model_version_id:parser.error('--model-version-id is required; this smoke starts two GPU trainings')
        key=str(uuid.uuid4());record={'schema':1,'key':key,'api':args.api.rstrip('/'),'mlflow':args.mlflow.rstrip('/'),'model_version_id':str(uuid.UUID(args.model_version_id)),'gpu_id':args.gpu_id}
        path=Path(__file__).parent/'results/evaluation'/(key+'.json');path=path.resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    def save(value):
        temp=path.with_suffix('.pending');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n');temp.replace(path)
    save(record);print('[record] '+str(path),flush=True)
    try:
        exercise(DatasetAPI(record['api']),API(record['mlflow']),record,args.timeout,save)
        record.pop('error',None);save(record)
    except Exception as exc:
        record['passed']=False;record['error']=str(exc);save(record)
        raise SystemExit('FAIL: '+str(exc)+'; retain this report and use --resume after diagnosis')
    print('PASS: pinned disjoint data, final-answer evaluation, MLflow metrics, checkpoint-enabled training and read-only comparable results.')


if __name__=='__main__':main()
