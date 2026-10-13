"""Read existing logs, or create and cancel one owned local training run.
Default logs mode is read-only. queued requires the dispatcher already stopped;
running uses a real GPU. Request identity is saved before creation for --resume.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

from live_serving_smoke import API


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def docker_json(args):
    result = subprocess.run(['docker'] + args, capture_output=True, text=True, timeout=20)
    require(result.returncode == 0, 'Docker inspection unavailable')
    return json.loads(result.stdout)


def dispatcher_stopped():
    state = docker_json(['inspect', '--format', '{{json .State.Status}}',
                         'model-operation-dispatcher'])
    require(state == 'exited', 'queued mode requires docker compose stop dispatcher first')


def owned_containers(operation_id):
    result = subprocess.run(['docker', 'ps', '-aq', '--no-trunc', '--filter',
                             'label=model-platform.operation_id=' + operation_id],
                            capture_output=True, text=True, timeout=20)
    require(result.returncode == 0, 'Docker container lookup unavailable')
    items = []
    for container_id in result.stdout.split():
        item = docker_json(['inspect', '--format',
                            '{"id":{{json .Id}},"state":{{json .State.Status}},'
                            '"running":{{json .State.Running}},'
                            '"labels":{{json .Config.Labels}}}', container_id])
        labels = item['labels']
        require(labels.get('model-platform.owner') == 'operation-dispatcher'
                and labels.get('model-platform.operation_id') == operation_id
                and labels.get('model-platform.execution_type') == 'training.run',
                'container ownership mismatch')
        items.append({'id': item['id'], 'state': item['state'], 'running': item['running']})
    return items


def check_cancelled(run, operation, mode):
    require(run.get('status') == operation.get('status') == 'cancelled',
            'TrainingRun and Operation must both be cancelled')
    require(run.get('cancel_requested_at') and run.get('cancelled_at'),
            'cancellation timestamps missing')
    require(not run.get('output_model_version_id') and not run.get('result_json'),
            'cancelled run advertised a completed output')
    if mode == 'queued':
        require(not operation.get('execution_started_at'), 'queued fixture actually started')


def check_logs(logs, run_id, operation_id):
    require(logs.get('training_run_id') == run_id and logs.get('operation_id') == operation_id,
            'logs belong to a different run')
    require(logs.get('source') in ('docker', 'archive', 'none'), 'unknown log source')
    require(isinstance(logs.get('stdout'), str) and isinstance(logs.get('stderr'), str)
            and type(logs.get('truncated')) is bool, 'invalid log response')
    for key in ('dispatch_token', 'execution_instance_id', 'dispatch_owner'):
        require(key not in logs, 'execution credential exposed')


def observe(client, record):
    run = client.call('GET', '/api/training-runs/' + record['training_run_id'])
    operation = client.call('GET', '/api/operations/' + record['operation_id'])
    return run, operation


def wait_state(client, record, save, predicate, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        run, operation = observe(client, record)
        brief = {'training_status': run.get('status'), 'operation_status': operation.get('status'),
                 'phase': operation.get('phase'),
                 'cancellation_error_code': run.get('cancellation_error_code', '')}
        if brief != previous:
            print('[training-control] ' + json.dumps(brief), flush=True)
            record['observation'] = brief
            save(record)
            previous = brief
        if predicate(run, operation):
            return run, operation
        require(run.get('status') not in ('failed', 'succeeded')
                and operation.get('status') not in ('failed', 'succeeded'),
                'fixture completed or failed before cancellation; inspect saved IDs')
        time.sleep(2)
    raise RuntimeError('observation timed out; request persists; resume this report')


def exercise(client, record, save, timeout):
    mode = record['mode']
    if mode == 'logs':
        run = client.call('GET', '/api/training-runs/' + record['training_run_id'])
        logs = client.call('GET', '/api/training-runs/' + record['training_run_id'] + '/logs')
        check_logs(logs, record['training_run_id'], run['operation_id'])
        require(logs['availability'] == 'available' and logs['source'] in ('docker', 'archive'),
                'no retained log available for this run')
        record['log_source'] = logs['source']
        record['log_bytes'] = len(logs['stdout'].encode()) + len(logs['stderr'].encode())
        record['passed'] = True
        save(record)
        return
    if mode == 'queued':
        dispatcher_stopped()
    if not record.get('training_run_id'):
        result = client.call('POST', '/api/training-runs', record['request'], record['idempotency_key'])
        record.update(training_run_id=result['training_run_id'], operation_id=result['operation_id'])
        save(record)
    if mode == 'running' and not record.get('cancel_requested'):
        run, operation = observe(client, record)
        if run.get('status') not in ('cancelling', 'cancelled'):
            wait_state(client, record, save,
                       lambda r, o: o.get('phase') == 'axolotl_training'
                       and o.get('execution_started_at') and o.get('status') == 'running', timeout)
            active = owned_containers(record['operation_id'])
            require(len(active) == 1 and active[0]['running'], 'running training container not observed')
            record['running_observed'] = True
            save(record)
    path = '/api/training-runs/' + record['training_run_id']
    requested = client.call('POST', path + '/cancel')
    record['cancel_requested'] = True
    save(record)
    require(requested.get('status') in ('cancelling', 'cancelled'), 'cancellation did not win')
    run, operation = wait_state(client, record, save,
                               lambda r, o: r.get('status') == o.get('status') == 'cancelled', timeout)
    check_cancelled(run, operation, mode)
    replay = client.call('POST', path + '/cancel')
    require(replay.get('cancelled_at') == run['cancelled_at']
            and replay.get('cancel_requested_at') == run['cancel_requested_at'],
            'cancel replay changed timestamps')
    containers = owned_containers(record['operation_id'])
    require(all(c['state'] == 'exited' and not c['running'] for c in containers),
            'cancelled run still has a live container')
    if mode == 'queued':
        require(not containers, 'queued cancellation created a container')
    else:
        require(record.get('running_observed') is True, 'running cancellation was not observed')
    logs = client.call('GET', path + '/logs?tail_lines=20&max_bytes=16384')
    check_logs(logs, record['training_run_id'], record['operation_id'])
    if mode == 'running':
        require(logs['source'] in ('docker', 'archive') and logs['availability'] == 'available',
                'cancelled training logs not retained')
    # Original create replay must retain its IDs without scheduling another Job.
    again = client.call('POST', '/api/training-runs', record['request'], record['idempotency_key'])
    require(again['training_run_id'] == record['training_run_id']
            and again['operation_id'] == record['operation_id'], 'create replay changed identity')
    record.update(passed=True, containers=containers, log_source=logs['source'],
                  cancellation_error_code=run.get('cancellation_error_code', ''))
    save(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='http://127.0.0.1:13000')
    parser.add_argument('--mode', choices=('logs', 'queued', 'running'), default='logs')
    parser.add_argument('--training-run-id', type=lambda s: str(uuid.UUID(s)))
    parser.add_argument('--model-version-id', type=lambda s: str(uuid.UUID(s)))
    parser.add_argument('--gpu-id', type=int, default=0)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    require(args.timeout > 0 and args.gpu_id >= 0, 'positive timeout and nonnegative GPU ID required')
    if args.resume:
        report = args.resume.resolve()
        record = json.loads(report.read_text())
        require(record.get('mode') in ('logs', 'queued', 'running'), 'invalid report mode')
    else:
        if args.mode == 'logs':
            require(args.training_run_id is not None, '--training-run-id required for read-only logs mode')
        else:
            require(args.model_version_id is not None and args.training_run_id is None,
                    'cancel modes create their own fixture; provide only --model-version-id')
        key = str(uuid.uuid4())
        report = Path(__file__).resolve().parent / 'results' / 'training-control' / (key + '.json')
        record = {'api': args.api, 'mode': args.mode, 'idempotency_key': key, 'passed': False,
                  'training_run_id': args.training_run_id,
                  'request': {'source_model_version_id': args.model_version_id,
                              'resources': {'gpu_ids': [args.gpu_id]},
                              'training': {'max_steps': 100, 'sequence_length': 256,
                                           'micro_batch_size': 1, 'gradient_accumulation_steps': 1}}}
    report.parent.mkdir(parents=True, exist_ok=True)

    def save(value):
        report.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    save(record)
    print('[record] ' + str(report), flush=True)
    client = API(record['api'])
    try:
        exercise(client, record, save, args.timeout)
    except Exception as exc:
        record['passed'] = False
        record['error'] = str(exc)
        # Only this script's owned fixture is eligible for cleanup cancellation.
        if record['mode'] != 'logs' and record.get('training_run_id'):
            try:
                result = client.call('POST', '/api/training-runs/' + record['training_run_id'] + '/cancel')
                record['cleanup_requested_status'] = result.get('status')
            except Exception as cleanup:
                record['cleanup_error'] = str(cleanup)
        save(record)
        raise SystemExit('FAIL: ' + str(exc)) from None
    print('PASS: ' + ('read-only retained training logs; no Job started.' if record['mode'] == 'logs'
                      else record['mode'] + ' training cancellation, replay, stopped runtime and log availability.'))


if __name__ == '__main__':
    main()
