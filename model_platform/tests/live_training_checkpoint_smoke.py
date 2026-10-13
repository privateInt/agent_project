"""One sealed checkpoint -> a new run executing only the remaining steps.

Prepare once with Dispatcher stopped. Use the same report for every timeout or
uncertain reply. Each observation phase defaults to 600 seconds; no timeout
implicitly cancels a run. --mode cancelled tests cancellation after a sealed save.
"""
import argparse
import json
from pathlib import Path
import time
import uuid

from live_serving_smoke import API
from live_training_control_smoke import dispatcher_stopped, owned_containers
from live_training_output_smoke import require, execution_snapshot


def poll(label, getter, done, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        value = getter()
        text = json.dumps(value, sort_keys=True)
        if previous != text:
            print('[' + label + '] ' + text, flush=True)
            previous = text
        if done(value):
            return value
        time.sleep(2)
    raise RuntimeError(label + ' observation timed out; resume this report without --prepare')


def prepare(client, record, save):
    dispatcher_stopped()
    result = client.call('POST', '/api/training-runs', record['request'], key=record['idempotency_key'])
    for field in ('training_run_id', 'operation_id'):
        value = str(uuid.UUID(result[field]))
        require(not record.get(field) or record[field] == value, 'parent replay changed identity')
        record[field] = value
    save(record)
    print('[training_run_id] ' + record['training_run_id'], flush=True)


def assert_resumed(record, parent, parent_op, child, child_op, model):
    require(execution_snapshot(parent, parent_op) == record['original_execution'], 'original execution was rewritten')
    require(child['training_run_id'] != parent['training_run_id'] and child['operation_id'] != parent['operation_id'], 'resume reused original execution')
    require(child.get('resume') == record['selection'], 'resume lineage changed')
    require(child['training'] == parent['training'] and child['dataset'] == parent['dataset']
            and child['resources'] == parent['resources'], 'resume changed training intent')
    require(child['status'] == child_op['status'] == 'succeeded', 'resumed execution did not succeed')
    evidence = (child.get('result') or json.loads(child.get('result_json') or '{}')).get('checkpoint', {})
    step = record['selection']['checkpoint_step']
    total = parent['training']['max_steps']
    require(evidence.get('version') == 1 and evidence.get('training_run_id') == child['training_run_id']
            and evidence.get('source_training_run_id') == parent['training_run_id']
            and evidence.get('checkpoint_sha256') == record['selection']['checkpoint_sha256']
            and evidence.get('start_step') == step and evidence.get('final_step') == total
            and evidence.get('executed_steps') == total-step, 'remaining-step evidence mismatch')
    require(all(evidence.get(name + '_loaded') is True for name in ('model','optimizer','scheduler','rng')), 'not all checkpoint state was restored')
    require(model.get('state') == 'ready' and model.get('artifact_kind') == 'adapter'
            and model.get('model_version_id') == child['output_model_version_id']
            and model.get('training_run_id') == child['training_run_id']
            and model.get('source_model_version_id') == parent['source_model_version_id'], 'resumed adapter lineage mismatch')
    if parent.get('mlflow_run_id'):
        require(child.get('mlflow_run_id') and child['mlflow_run_id'] != parent['mlflow_run_id'], 'resume reused original MLflow run')


def exercise(client, record, save, timeout):
    parent_path = '/api/training-runs/' + record['training_run_id']
    def parent_state():
        run=client.call('GET',parent_path);op=client.call('GET','/api/operations/'+record['operation_id'])
        return {'training_status':run['status'],'operation_status':op['status'],'phase':op.get('phase'),'error_code':op.get('error_code')}
    if not record.get('original_execution'):
        if record['mode']=='cancelled':
            poll('checkpoint-save',lambda:client.call('GET',parent_path+'/checkpoints'),lambda v:any(c['step']==2 for c in v['checkpoints']),timeout)
            # Cancellation is idempotent. Repeating after response loss is safe.
            client.call('POST',parent_path+'/cancel')
        expected='cancelled' if record['mode']=='cancelled' else 'failed'
        def ended(v):
            require(v['training_status']!='succeeded','fixture ended normally; scoped checkpoint stop was not applied')
            if v['training_status']==v['operation_status']==expected:
                if expected=='failed':require(v['error_code']=='AXOLOTL_TRAIN_FAILED','fixture failed before expected Trainer interruption')
                return True
            return False
        poll('checkpoint-parent',parent_state,ended,timeout)
        poll('checkpoint-parent-runtime',lambda:owned_containers(record['operation_id']),lambda cs:len(cs)==1 and all(c['state']=='exited' and not c['running'] for c in cs),120)
        parent=client.call('GET',parent_path);op=client.call('GET','/api/operations/'+record['operation_id'])
        checkpoints=client.call('GET',parent_path+'/checkpoints')
        selected=[c for c in checkpoints['checkpoints'] if c['step']==2 and c['resumable']]
        require(len(selected)==1,'sealed step 2 is not resumable')
        if record['mode']=='failed':
            logs=client.call('GET',parent_path+'/logs?tail_lines=200&max_bytes=131072')
            require('MODEL_PLATFORM_CHECKPOINT_STOP_FIXTURE' in logs.get('stdout','')+logs.get('stderr',''),'expected scoped stop not present in logs')
        record['selection']={'source_training_run_id':record['training_run_id'],'checkpoint_step':2,'checkpoint_sha256':selected[0]['checkpoint_sha256']}
        record['original_execution']=execution_snapshot(parent,op)
        record['parent_containers']=owned_containers(record['operation_id'])
        record['resume_key']=str(uuid.uuid4())
        save(record)
    response=client.call('POST',parent_path+'/resume',{'checkpoint_step':2},key=record['resume_key'])
    require(not record.get('child') or record['child']==response,'resume replay changed identity')
    record['child']=response;save(record)
    again=client.call('POST',parent_path+'/resume',{'checkpoint_step':2},key=record['resume_key'])
    require(again==response,'same resume key created another request')
    child_path='/api/training-runs/'+response['training_run_id']
    def child_state():
        run=client.call('GET',child_path);op=client.call('GET','/api/operations/'+response['operation_id'])
        require(run['status'] not in ('failed','cancelled') and op['status'] not in ('failed','cancelled'),'resumed execution failed; inspect saved child IDs and logs')
        return {'training_status':run['status'],'operation_status':op['status'],'phase':op.get('phase')}
    poll('checkpoint-resume',child_state,lambda v:v['training_status']==v['operation_status']=='succeeded',timeout)
    poll('checkpoint-child-runtime',lambda:owned_containers(response['operation_id']),lambda cs:len(cs)==1 and all(c['state']=='exited' and not c['running'] for c in cs),120)
    parent=client.call('GET',parent_path);parent_op=client.call('GET','/api/operations/'+record['operation_id'])
    child=client.call('GET',child_path);child_op=client.call('GET','/api/operations/'+response['operation_id'])
    model=client.call('GET','/api/model-versions/'+child['output_model_version_id'])
    assert_resumed(record,parent,parent_op,child,child_op,model)
    require(owned_containers(record['operation_id'])==record['parent_containers'],'resume changed original container')
    for run_id,operation_id in ((record['training_run_id'],record['operation_id']),(response['training_run_id'],response['operation_id'])):
        page=client.call('GET','/api/operations?resource_type=training_run&resource_id='+run_id)
        require(not page.get('next_cursor') and len(page['items'])==1 and page['items'][0]['operation_id']==operation_id,'unexpected duplicate Operation')
    checkpoints=client.call('GET',parent_path+'/checkpoints')
    require(any(c['step']==2 and c['checkpoint_sha256']==record['selection']['checkpoint_sha256'] for c in checkpoints['checkpoints']),'source checkpoint changed')
    require(client.call('POST',parent_path+'/resume',{'checkpoint_step':2},key=record['resume_key'])==response,'completed resume replay changed')
    record.update(passed=True,output_model_version_id=child['output_model_version_id'],evidence=child['result']['checkpoint'])
    save(record)
    print('[output_model_version_id] '+record['output_model_version_id'],flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true');parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--model-version-id');parser.add_argument('--gpu-id',type=int,default=0)
    parser.add_argument('--mode',choices=('failed','cancelled'),default='failed')
    parser.add_argument('--api',default='http://127.0.0.1:13000');parser.add_argument('--timeout',type=int,default=600)
    args=parser.parse_args();require(args.timeout>0 and args.gpu_id>=0,'invalid timeout or GPU ID')
    if args.report.exists():
        record=json.loads(args.report.read_text());require(record.get('kind')=='training-checkpoint-v1','wrong report kind')
    else:
        require(args.prepare and args.model_version_id,'new report requires --prepare and model version')
        record=dict(kind='training-checkpoint-v1',api=args.api,mode=args.mode,passed=False,idempotency_key=str(uuid.uuid4()),
             request=dict(source_model_version_id=str(uuid.UUID(args.model_version_id)),resources=dict(gpu_ids=[args.gpu_id]),
                          training=dict(max_steps=4,checkpoint_steps=2,sequence_length=256,micro_batch_size=1,gradient_accumulation_steps=1)))
    args.report.parent.mkdir(parents=True,exist_ok=True)
    def save(value):
        temporary=args.report.with_suffix('.pending');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temporary.replace(args.report)
    save(record);print('[record] '+str(args.report.resolve()),flush=True)
    try:
        client=API(record['api'])
        if args.prepare:
            prepare(client,record,save)
            print('PREPARED: enable the run-scoped checkpoint fixture before restarting Dispatcher.')
        else:
            exercise(client,record,save,args.timeout)
            print('PASS: sealed checkpoint, new resumed execution, restored state, remaining steps, immutable parent and ready adapter.')
    except Exception as exc:
        record.update(passed=False,error=str(exc));save(record)
        raise SystemExit('FAIL: '+str(exc)) from None


if __name__=='__main__':main()
