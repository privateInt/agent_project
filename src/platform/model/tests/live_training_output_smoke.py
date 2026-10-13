"""Preserve completed compute and publish it without retraining.

--prepare creates one 1-step request while Dispatcher is stopped. The operator
then enables the run-scoped failure hook on Dispatcher and invokes this script
with the same --report. Observation defaults to 600 seconds per stage. Timeouts
never cancel or recreate the saved run; resume the same report.
"""
import argparse
import json
from pathlib import Path
import time
import uuid

from live_serving_smoke import API
from live_training_control_smoke import dispatcher_stopped, owned_containers


def require(value, message):
    if not value:
        raise RuntimeError(message)


def prepare(client, record, save):
    dispatcher_stopped()
    result = client.call('POST', '/api/training-runs', record['request'], key=record['idempotency_key'])
    for field in ('training_run_id', 'operation_id'):
        value = str(uuid.UUID(result[field]))
        require(not record.get(field) or record[field] == value, 'training request replay changed identity')
        record[field] = value
    save(record)
    print('[training_run_id] ' + record['training_run_id'], flush=True)


def observe_failure(client, record, save, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        run = client.call('GET', '/api/training-runs/' + record['training_run_id'])
        op = client.call('GET', '/api/operations/' + record['operation_id'])
        brief = {"training_status": run.get('status'), 'operation_status': op.get('status'),
                 'phase': op.get('phase'), 'error_code': op.get('error_code')}
        if previous != brief:
            print('[training-output] ' + json.dumps(brief), flush=True)
            previous = brief
            record['observation'] = brief
            save(record)
        if run.get('status') == op.get('status') == 'failed':
            require(op.get('error_code') == 'TRAINING_ARTIFACT_PUBLISH_FAILED',
                    'the fixture failed before publication; inspect its logs')
            containers = owned_containers(record['operation_id'])
            if containers and all(c['state'] == 'exited' and not c['running'] for c in containers):
                return run, op, containers
        require(run.get('status') not in ('cancelled', 'succeeded') and
                op.get('status') not in ('cancelled', 'succeeded'),
                'expected a run-scoped publication failure, but training ended differently')
        time.sleep(2)
    raise RuntimeError('training observation timed out; resume the same report, do not create another run')


def execution_snapshot(run, operation):
    return {'run': {k: run.get(k) for k in ('training_run_id', 'operation_id', 'status',
                    'result_json', 'mlflow_run_id', 'output_model_version_id')},
            'operation': {k: operation.get(k) for k in ('operation_id', 'status', 'phase', 'attempt',
                        'dispatch_generation', 'execution_started_at', 'error_code', 'completed_at')}}


def check_recovered(record, run, operation, output, model, containers):
    require(output.get('recovery_status') == 'succeeded' and output.get('availability') == 'retained',
            'publication or retention was not verified')
    require(output.get('training_run_id') == record['training_run_id'] and
            output.get('operation_id') == record['operation_id'], 'output identity mismatch')
    require(execution_snapshot(run, operation) == record['original_execution'],
            'recovery changed the original execution outcome')
    require(containers == record['containers_before'], 'recovery changed the set of training containers')
    require(model.get('model_version_id') == output.get('output_model_version_id') and
            model.get('state') == 'ready' and model.get('artifact_kind') == 'adapter' and
            model.get('source_type') == 'training' and
            model.get('training_run_id') == record['training_run_id'] and
            model.get('source_model_version_id') == record['request']['source_model_version_id'],
            'recovered model does not match the completed training output')
    require(model.get('artifact_manifest_sha256') == output.get('manifest_sha256') == record['saved_digest']
            and model.get('artifact_size_bytes') == output.get('size_bytes')
            and model.get('artifact_file_count') == output.get('file_count'), 'recovered manifest mismatch')
    require(run.get('output_recovery_status') == 'succeeded' and
            run.get('recovered_model_version_id') == model.get('model_version_id'),
            'training detail is missing recovery status')


def check_single_operation(client, record):
    page = client.call('GET', '/api/operations?resource_type=training_run&resource_id=' + record['training_run_id'])
    require(not page.get('next_cursor') and len(page.get('items', [])) == 1 and
            page['items'][0].get('operation_id') == record['operation_id'],
            'training output recovery created or selected another Operation')


def recover(client, record, save, timeout):
    require(record.get('training_run_id') and record.get('operation_id'), 'run --prepare first')
    base = '/api/training-runs/' + record['training_run_id']
    if not record.get('original_execution'):
        run, op, containers = observe_failure(client, record, save, timeout)
        output = client.call('GET', base + '/output')
        require(output.get('availability') == 'retained', 'completed output receipt was not retained')
        require(output.get('file_count', 0) > 0 and output.get('size_bytes', 0) > 0,
                'saved output is empty')
        record['original_execution'] = execution_snapshot(run, op)
        record['containers_before'] = sorted(containers, key=lambda c: c['id'])
        record['saved_digest'] = output['manifest_sha256']
        record['recovery_key'] = str(uuid.uuid4())
        save(record)
    check_single_operation(client, record)
    first = client.call('POST', base + '/output/retry', key=record['recovery_key'])
    require(not record.get('recovery_id') or record['recovery_id'] == first['recovery_id'],
            'recovery resume changed identity')
    record['recovery_id'] = first['recovery_id']
    save(record)
    second = client.call('POST', base + '/output/retry', key=record['recovery_key'])
    require(second.get('recovery_id') == first.get('recovery_id'), 'duplicate recovery request was created')
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        output = client.call('GET', base + '/output')
        require(output.get('recovery_id') == record['recovery_id'], 'another recovery superseded this smoke')
        brief = {k: output.get(k) for k in ('availability', 'recovery_status', 'error_code')}
        if brief != previous:
            print('[output-recovery] ' + json.dumps(brief), flush=True)
            previous = brief
        record['output'] = output
        save(record)
        require(output.get('recovery_status') != 'failed', 'publication retry failed: ' + str(output.get('error_code')))
        if output.get('recovery_status') == 'succeeded':
            break
        time.sleep(2)
    else:
        raise RuntimeError('publication observation timed out; resume the same report')
    run = client.call('GET', base)
    op = client.call('GET', '/api/operations/' + record['operation_id'])
    model = client.call('GET', '/api/model-versions/' + output['output_model_version_id'])
    containers = sorted(owned_containers(record['operation_id']), key=lambda c: c['id'])
    check_recovered(record, run, op, output, model, containers)
    replay = client.call('POST', base + '/output/retry', key=record['recovery_key'])
    require(replay.get('recovery_id') == record['recovery_id'] and replay.get('recovery_status') == 'succeeded',
            'completed recovery replay changed')
    check_single_operation(client, record)
    record['passed'] = True
    record['output_model_version_id'] = model['model_version_id']
    save(record)
    print('[output_model_version_id] ' + model['model_version_id'], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--model-version-id')
    parser.add_argument('--gpu-id', type=int, default=0)
    parser.add_argument('--api', default='http://127.0.0.1:13000')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    require(args.timeout > 0 and args.gpu_id >= 0, 'positive timeout and nonnegative GPU ID required')
    if args.report.exists():
        record = json.loads(args.report.read_text())
        require(record.get('kind') == 'training-output-recovery-v1', 'wrong report kind')
    else:
        require(args.prepare and args.model_version_id, 'a new report requires --prepare and --model-version-id')
        record = {'kind': 'training-output-recovery-v1', 'api': args.api, 'passed': False,
                  'idempotency_key': str(uuid.uuid4()),
                  'request': {'source_model_version_id': str(uuid.UUID(args.model_version_id)),
                              'resources': {'gpu_ids': [args.gpu_id]},
                              'training': {'max_steps': 1, 'sequence_length': 256,
                                           'micro_batch_size': 1, 'gradient_accumulation_steps': 1}}}
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save(value):
        pending = args.report.with_suffix('.pending')
        pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        pending.replace(args.report)

    save(record)
    print('[record] ' + str(args.report.resolve()), flush=True)
    try:
        (prepare if args.prepare else recover)(API(record['api']), record, save,
                                               *([] if args.prepare else [args.timeout]))
    except Exception as exc:
        record['passed'] = False
        record['error'] = str(exc)
        save(record)
        raise SystemExit('FAIL: ' + str(exc)) from None
    if args.prepare:
        print('PREPARED: one queued training request; enable the scoped failure hook before starting Dispatcher.')
    else:
        print('PASS: completed output retained, upload-only retry, immutable execution history and ready adapter.')


if __name__ == '__main__':
    main()
