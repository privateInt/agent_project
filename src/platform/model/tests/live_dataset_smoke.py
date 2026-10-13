"""Register immutable JSONL; optionally run a one-step GPU training with its pin.
Default mode creates dataset metadata and MinIO objects, without starting a Job.
--resume reuses the saved request keys; no automatic dataset or model deletion.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import subprocess
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

from live_serving_smoke import API, APIError, wait_for
from smoke_timestamps import timestamp_key

MAX_BYTES = 64 << 20
FIXTURE = b'{"messages":[{"role":"user","content":"What is 2 + 3?"},{"role":"assistant","content":"5"}]}\n' + b'{"messages":[{"role":"user","content":"What is 2 * 4?"},{"role":"assistant","content":"8"}]}\n'


class DatasetAPI(API):
    def upload(self, dataset_id, version, content, key):
        boundary = 'dataset-' + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="version"\r\n\r\n{version}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="train.jsonl"\r\n'
                'Content-Type: application/x-ndjson\r\n\r\n').encode()
        body += content + f'\r\n--{boundary}--\r\n'.encode()
        request = urllib.request.Request(self.base + '/api/datasets/' + dataset_id + '/versions',
                                         data=body, method='POST', headers={
                                             'Content-Type': 'multipart/form-data; boundary=' + boundary,
                                             'Idempotency-Key': key})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise APIError(exc.code, exc.read(2048).decode(errors='replace')) from exc


def retry(action):
    for attempt in range(3):
        try:
            return action()
        except APIError as exc:
            if exc.status < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(1)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_pin(run, version):
    expected = {'source_type': 'version', 'dataset_version_id': version['dataset_version_id'],
                'source_uri': version['artifact_uri'], 'format': 'chat_messages',
                'sha256': version['sha256'], 'size_bytes': version['size_bytes'],
                'record_count': version['record_count']}
    require(run.get('dataset') == expected, 'Training run did not preserve the registered dataset pin')


def check_parallelism(run, gpu_ids, training_run_id):
    count = len(gpu_ids)
    result = (run.get('result') or {}).get('parallelism') or {}
    ranks = result.get('ranks') or []
    resources, training = run.get('resources') or {}, run.get('training') or {}
    require(resources.get('gpu_count') == count and resources.get('gpu_ids') == gpu_ids
            and training.get('strategy') == 'ddp', 'TrainingRun changed its DDP GPU assignment')
    require(result.get('strategy') == 'ddp' and result.get('world_size') == count
            and result.get('effective_batch_size') == count
            and len(ranks) == count, 'DDP result did not include all requested GPUs')
    require({r.get('rank') for r in ranks} == set(range(count))
            and len({r.get('device_uuid') for r in ranks}) == count, 'duplicate or missing DDP ranks/devices')
    for rank in ranks:
        require(rank.get('local_rank') == rank.get('rank') and rank.get('world_size') == count
                and rank.get('training_run_id') == training_run_id and bool(rank.get('device_uuid'))
                and rank.get('initialized') is True and rank.get('ddp_wrapped') is True
                and rank.get('completed') is True and rank.get('global_step') == 1,
                'a DDP worker did not complete the requested training')


def observe_training(client, run_id, operation_id, record, save):
    run = client.call('GET', '/api/training-runs/' + run_id)
    operation = client.call('GET', '/api/operations/' + operation_id)
    fields = ('status', 'phase', 'error_code', 'error_message', 'dispatch_generation',
              'gpu_wait_count', 'gpu_wait_until', 'dispatch_lease_until', 'execution_started_at')
    brief = {key: operation[key] for key in fields if key in operation}
    if brief != record.get('operation_observation'):
        record['operation_observation'] = brief
        save(record)
        print('[training-operation] ' + json.dumps(brief, sort_keys=True), flush=True)
    if operation.get('status') in ('failed', 'cancelled'):
        raise RuntimeError('training Operation is terminal: ' + json.dumps(brief, sort_keys=True))
    generation, waits = operation.get('dispatch_generation'), operation.get('gpu_wait_count', 0)
    lease = operation.get('dispatch_lease_until')
    if (operation.get('status') == 'dispatching' and not operation.get('execution_started_at')
            and type(generation) is int and type(waits) is int and generation - waits >= 3 and lease):
        expiry = timestamp_key(lease)
        if expiry <= timestamp_key(datetime.now(timezone.utc).isoformat()):
            raise RuntimeError('pre-start launch recovery budget exhausted; inspect this Operation before retrying: '
                               + json.dumps(brief, sort_keys=True))
    return run


def verify_remote(record, run, operation, gpu_ids):
    node = record['node_name']
    for view in (run, operation):
        resources = view.get('resources') or {}
        require(resources.get('node_name') == node and resources.get('gpu_ids') == gpu_ids,
                'remote placement changed in TrainingRun or Operation')
    cmd = ['docker', 'compose', '-f', 'docker-compose.yml', '-f', 'remote/control.compose.yml',
           'exec', '-T', 'dispatcher', '/app/dispatcher', '--inspect-node-job', operation['operation_id']]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    require(result.returncode == 0, 'remote Job evidence unavailable; resume this report after checking the node')
    report = json.loads(result.stdout)
    check_remote_evidence(report, node, operation['operation_id'], gpu_ids)
    return report


def check_remote_evidence(report, node, operation_id, gpu_ids):
    require(report.get('version') == 1 and report.get('action') == 'inspect'
            and report.get('status') == 'observed' and report.get('node_name') == node
            and report.get('operation_id') == operation_id and bool(report.get('daemon_id'))
            and report.get('architecture') == 'amd64', 'remote worker identity mismatch')
    evidence = report.get('evidence') or {}
    require(bool(evidence.get('container_id')) and bool(evidence.get('image_id'))
            and evidence.get('gpu_ids') == gpu_ids and evidence.get('state') in ('running', 'exited')
            and (evidence.get('state') != 'exited' or evidence.get('exit_code') == 0),
            'remote Job evidence did not confirm the requested GPU training')


def exercise(client, record, content, timeout, save):
    key = record['run_id']
    source_id = record.get('model_version_id')
    gpu_ids = record.get('gpu_ids') or [record['gpu_id']]
    require(1 <= len(gpu_ids) <= 16 and len(set(gpu_ids)) == len(gpu_ids)
            and all(type(i) is int and i >= 0 for i in gpu_ids), 'invalid exact GPU IDs')
    gpu_ids = sorted(gpu_ids)
    if source_id:
        source = client.call('GET', '/api/model-versions/' + source_id)
        require(source.get('model_version_id') == source_id and source.get('state') == 'ready'
                and source.get('artifact_kind') == 'snapshot', 'training source must be a ready snapshot')
    dataset = retry(lambda: client.call('POST', '/api/datasets', record['dataset_request'], key + ':dataset'))
    record['dataset'] = dataset
    save(record)
    replay = retry(lambda: client.call('POST', '/api/datasets', record['dataset_request'], key + ':dataset'))
    require(dataset == replay, 'dataset create replay changed its response')
    dataset_id = str(uuid.UUID(dataset['dataset_id']))
    version = retry(lambda: client.upload(dataset_id, 'v1', content, key + ':version'))
    record['version'] = version
    save(record)
    require(version.get('dataset_id') == dataset_id and version.get('version') == 'v1'
            and version.get('state') == 'ready' and version.get('format') == 'chat_messages', 'wrong version metadata')
    require(version.get('sha256') == hashlib.sha256(content).hexdigest()
            and version.get('size_bytes') == len(content)
            and version.get('record_count') == sum(bool(line.strip()) for line in content.splitlines()),
            'registered dataset digest, bytes or record count differ from the uploaded file')
    for upload_key in (key + ':version', key + ':same-content'):
        require(version == retry(lambda: client.upload(dataset_id, 'v1', content, upload_key)),
                'same label/content did not return the same version')
    different = FIXTURE if content != FIXTURE else FIXTURE + b'\n'
    try:
        retry(lambda: client.upload(dataset_id, 'v1', different, key + ':conflict'))
    except APIError as exc:
        require(exc.status == 409, 'changed content returned an unexpected error: ' + str(exc))
    else:
        raise RuntimeError('immutable version accepted different content')
    current = client.call('GET', '/api/dataset-versions/' + version['dataset_version_id'])
    require(current == version, 'version changed after conflict')
    listed = client.call('GET', '/api/datasets/' + dataset_id + '/versions')
    require(version in listed.get('versions', []), 'registered version is missing from its dataset')
    record['registration_passed'] = True
    save(record)
    print('[dataset] ' + json.dumps(version, sort_keys=True), flush=True)
    if not source_id:
        record['passed'] = True
        save(record)
        return
    request = {'source_model_version_id': source_id,
               'dataset': {'source_type': 'version', 'dataset_version_id': version['dataset_version_id']},
               'training': {'method': 'qlora', 'sequence_length': 256, 'max_steps': 1,
                            'micro_batch_size': 1, 'gradient_accumulation_steps': 1,
                            'learning_rate': 0.0002, 'lora': {'r': 8, 'alpha': 16, 'dropout': 0.0},
                            'sample_packing': False},
               'resources': {'gpu_ids': gpu_ids}}
    if record.get('node_name'):
        require(len(gpu_ids) == 1, 'remote smoke requires exactly one GPU')
        request['resources']['node_name'] = record['node_name']
    if len(gpu_ids) > 1:
        request['training']['strategy'] = 'ddp'
    record['training_request'] = request
    save(record)  # Persist the exact request and key before creating GPU work.
    created = retry(lambda: client.call('POST', '/api/training-runs', request, key + ':training'))
    record['training'] = created
    save(record)
    replay = retry(lambda: client.call('POST', '/api/training-runs', request, key + ':training'))
    require(created == replay, 'training replay changed its response')
    run_id, operation_id = str(uuid.UUID(created['training_run_id'])), str(uuid.UUID(created['operation_id']))
    print('[training] ' + json.dumps(created, sort_keys=True), flush=True)
    run = wait_for('dataset-training', lambda: observe_training(client, run_id, operation_id, record, save),
                   lambda view: view.get('status') == 'succeeded', timeout)
    check_pin(run, version)
    if len(gpu_ids) > 1:
        check_parallelism(run, gpu_ids, run_id)
        record['parallelism'] = run['result']['parallelism']
    result = run.get('result') or {}
    for field, expected in {'dataset_version_id': version['dataset_version_id'], 'dataset_sha256': version['sha256'],
                            'dataset_bytes': version['size_bytes'], 'dataset_record_count': version['record_count'],
                            'dataset_source_type': 'version', 'dataset_source_uri': version['artifact_uri'],
                            'dataset_format': 'chat_messages'}.items():
        require(result.get(field) == expected, 'training result mismatch: ' + field)
    operation = wait_for('dataset-operation', lambda: client.call('GET', '/api/operations/' + operation_id),
                         lambda view: view.get('status') == 'succeeded', timeout)
    output_id = str(uuid.UUID(run['output_model_version_id']))
    output = client.call('GET', '/api/model-versions/' + output_id)
    require(output.get('state') == 'ready' and output.get('artifact_kind') == 'adapter'
            and output.get('source_type') == 'training' and output.get('training_run_id') == run_id
            and output.get('source_model_version_id') == source_id, 'registered adapter lineage mismatch')
    if record.get('node_name'):
        record['remote_evidence'] = verify_remote(record, run, operation, gpu_ids)
        save(record)
    record.update(training_run=run, operation=operation, output_model_version_id=output_id,
                  training_passed=True, passed=True)
    save(record)
    print('[output_model_version_id] ' + output_id, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-base')
    parser.add_argument('--node-name', help='explicit enabled remote node; also verifies actual worker container')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--model-version-id', help='opt in to real one-step GPU training using this snapshot')
    gpu_group = parser.add_mutually_exclusive_group()
    gpu_group.add_argument('--gpu-id', type=int, default=0)
    gpu_group.add_argument('--gpu-ids', type=int, nargs='+', help='exact GPU IDs on ONE Docker host, e.g. 0 1')
    parser.add_argument('--timeout', type=int, default=1800, help='observation timeout in seconds; use 600 for a 10-minute smoke; does not cancel training')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    require(args.node_name is None or (args.model_version_id is not None and args.resume is None),
            '--node-name requires a new --model-version-id run; resume keeps the saved node')
    require(args.gpu_ids is None or (args.model_version_id is not None and args.resume is None),
            '--gpu-ids requires a new --model-version-id training run')
    require(args.timeout > 0 and args.gpu_id >= 0, 'timeout must be positive and gpu-id nonnegative')
    root = Path(__file__).resolve().parents[1]
    require(Path.cwd().resolve() == root, 'run from ~/workspace/agent_project/model_platform')
    if args.resume:
        require(args.file is None and args.model_version_id is None and args.api_base is None,
                '--resume uses the original file, model and API saved in the report')
        path = args.resume.resolve()
        record = json.loads(path.read_text(encoding='utf-8'))
        require(record.get('schema_version') == 1, 'unsupported report schema')
    else:
        run_id = str(uuid.uuid4())
        path = root / 'tests' / 'results' / 'datasets' / (run_id + '.json')
        record = {'schema_version': 1, 'run_id': run_id, 'api_base': (args.api_base or 'http://127.0.0.1:13000').rstrip('/'),
                  'file': str(args.file.resolve()) if args.file else None, 'gpu_id': args.gpu_id,
                  'model_version_id': str(uuid.UUID(args.model_version_id)) if args.model_version_id else None,
                  'dataset_request': {'name': 'dataset-smoke-' + run_id, 'description': 'Dataset version integration validation'},
                  'passed': False}
        if args.node_name:
            require(args.node_name.strip() == args.node_name and bool(args.node_name), 'invalid node name')
            record['node_name'] = args.node_name
        if args.gpu_ids is not None:
            require(1 <= len(args.gpu_ids) <= 16 and len(set(args.gpu_ids)) == len(args.gpu_ids)
                    and min(args.gpu_ids) >= 0, 'invalid exact GPU IDs')
            record['gpu_ids'] = sorted(args.gpu_ids)
    if record['file']:
        with Path(record['file']).open('rb') as handle:
            content = handle.read(MAX_BYTES + 1)
    else:
        content = FIXTURE
    require(0 < len(content) <= MAX_BYTES, 'file must contain between 1 byte and 64 MiB of uncompressed JSONL')
    digest = hashlib.sha256(content).hexdigest()
    require(record.get('content_sha256', digest) == digest, 'source file changed since the saved request')
    record.update(content_sha256=digest, content_size_bytes=len(content), passed=False)
    record.pop('error', None)
    path.parent.mkdir(parents=True, exist_ok=True)

    def save(value):
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temp.replace(path)

    save(record)
    print('[record] ' + str(path), flush=True)
    try:
        exercise(DatasetAPI(record['api_base']), record, content, args.timeout, save)
    except Exception as exc:
        record.update(passed=False, error=str(exc))
        save(record)
        raise
    print('PASS: dataset upload, hash, replay and immutable version' +
          ('; pinned GPU training and ready adapter.' if record.get('training_passed') else '; no training Job started.'))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit('FAIL: ' + str(exc))
