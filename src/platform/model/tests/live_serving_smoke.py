"""Real single-GPU vLLM smoke: snapshot/adapter -> serving -> inference -> stop.
Run from model_platform after the Go/DB tests and image rollout.
A request record is written before POST; --resume reuses its Idempotency-Key.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class APIError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f'HTTP {status}: {message}')
        self.status = status


class API:
    def __init__(self, base):
        self.base = base.rstrip('/')

    def call(self, method, path, body=None, key=None):
        headers = {'Content-Type': 'application/json'}
        if key is not None:
            headers['Idempotency-Key'] = key
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise APIError(exc.code, exc.read(2048).decode(errors='replace')) from exc


def same_request_identity(first, second):
    for name in ('deployment_id', 'operation_id'):
        if str(uuid.UUID(first[name])) != first[name] or first[name] != second.get(name):
            raise RuntimeError('idempotent deployment replay changed ' + name)


def runtime_endpoint(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or
            url.username is not None or url.password is not None or url.path != '/v1' or
            url.query or url.fragment):
        raise RuntimeError('expected a host-loopback vLLM endpoint')
    return value.rstrip('/')


def wait_for(label, getter, predicate, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        try:
            value = getter()
        except APIError as exc:
            if exc.status < 500:
                raise
            time.sleep(2)
            continue
        except urllib.error.URLError:
            time.sleep(2)
            continue
        brief = {k: value[k] for k in ('status', 'phase', 'apply_status', 'runtime_state',
                                       'submission_status', 'runtime_error_code') if k in value}
        if brief != previous:
            print(f'[{label}] ' + json.dumps(brief, sort_keys=True), flush=True)
            previous = brief
        if predicate(value):
            return value
        if label != 'stop' and (value.get('submission_status') == 'blocked' or value.get('status') == 'failed' or value.get('apply_status') == 'failed'):
            raise RuntimeError(f'{label} failed: {value}')
        time.sleep(2)
    raise RuntimeError(f'{label} did not complete within {timeout}s')


def submit(client, record):
    for attempt in range(3):
        try:
            return client.call('POST', '/api/deployments', record['request'], record['idempotency_key'])
        except APIError as exc:
            if exc.status < 500 or attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(1)
    raise RuntimeError('submission did not return a result')


def source_lineage(client, source_id):
    source = client.call('GET', '/api/model-versions/' + source_id)
    if source.get('model_version_id') != source_id or source.get('state') != 'ready':
        raise RuntimeError('source model version must match the requested ready version')
    kind = source.get('artifact_kind')
    if kind == 'snapshot':
        return {'expected_artifact_kind': 'snapshot', 'expected_base_model_version_id': source_id,
                'expected_adapter_model_version_id': ''}
    if kind != 'adapter':
        raise RuntimeError('source must be a snapshot or a registered training adapter')
    if source.get('source_type') is None:
        raise RuntimeError('model version response is missing source_type; rebuild and restart Gateway')
    if source['source_type'] != 'training':
        raise RuntimeError('adapter source_type must be training; received ' + repr(source['source_type']))
    base_id = str(uuid.UUID(source['source_model_version_id']))
    base = client.call('GET', '/api/model-versions/' + base_id)
    if (base.get('model_version_id') != base_id or base.get('state') != 'ready' or
            base.get('artifact_kind') != 'snapshot' or base.get('model_id') != source.get('model_id') or
            base.get('artifact_uri') != source.get('base_model_artifact_uri')):
        raise RuntimeError('adapter base snapshot lineage does not match')
    return {'expected_artifact_kind': 'adapter', 'expected_base_model_version_id': base_id,
            'expected_adapter_model_version_id': source_id}


def check_serving_lineage(view, record):
    for field in ('artifact_kind', 'base_model_version_id', 'adapter_model_version_id'):
        expected = 'expected_' + field
        if expected in record and view.get(field) != record[expected]:
            raise RuntimeError('serving result changed ' + field)
    if record.get('expected_artifact_kind') == 'adapter':
        if not view.get('base_served_model_name') or view['base_served_model_name'] == view['served_model_name']:
            raise RuntimeError('adapter alias must differ from its base model')


def check_serving_models(models, view, record):
    entries = {m.get('id'): m for m in models.get('data', [])}
    selected = entries.get(view['served_model_name'])
    if selected is None:
        raise RuntimeError('vLLM model list did not contain the registered serving name')
    if record.get('expected_artifact_kind') == 'adapter':
        base = view['base_served_model_name']
        if (base not in entries or selected.get('parent') != base or
                selected.get('root') != '/models/' + record['deployment_id'] + '/adapter'):
            raise RuntimeError('vLLM did not register the expected adapter and base lineage')


def exercise(client, record, timeout, *, save=lambda _: None, runtime_factory=API, keep_running=False):
    success = False
    record['passed'] = False
    deployment_id = record.get('deployment_id')
    try:
        first = submit(client, record)
        same_request_identity(first, first)
        deployment_id = first['deployment_id']
        record.update(deployment_id=deployment_id, operation_id=first['operation_id'])
        save(record)
        second = submit(client, record)
        same_request_identity(first, second)
        record['idempotent_replay'] = True
        print('[request] ' + json.dumps(first, sort_keys=True), flush=True)
        view = wait_for('serving', lambda: client.call('GET', '/api/deployments/' + deployment_id),
                        lambda v: v.get('status') == 'ready' and v.get('runtime_state') == 'ready', timeout)
        if view.get('operation_id') != record['operation_id'] or view.get('serving', {}).get('engine') != 'vllm':
            raise RuntimeError('serving result does not match the requested operation/engine')
        check_serving_lineage(view, record)
        endpoint = runtime_endpoint(view['endpoint'])
        record.update(endpoint=endpoint, served_model_name=view['served_model_name'],
                      runtime_container_id=view['runtime_container_id'], runtime_image_id=view['runtime_image_id'])
        for field in ('artifact_kind', 'base_model_version_id', 'adapter_model_version_id', 'base_served_model_name'):
            record[field] = view.get(field)
        save(record)
        wait_for('operation', lambda: client.call('GET', '/api/operations/' + record['operation_id']),
                 lambda v: v.get('status') == 'succeeded', min(timeout, 120))
        inference = runtime_factory(endpoint)
        models = inference.call('GET', '/models')
        check_serving_models(models, view, record)
        chat = {
            'messages': [{'role': 'user', 'content': 'Reply with a short greeting.'}],
            'max_tokens': 32, 'temperature': 0,
        }
        if record.get('via_gateway'):
            answer = client.call('POST', '/api/deployments/' + deployment_id + '/chat', chat)
            if answer.get('deployment_id') != deployment_id or answer.get('finish_reason') not in ('stop', 'length'):
                raise RuntimeError('Gateway chat did not identify the deployment or completion')
            response = {'model': answer.get('model'), 'choices': [{'message': {'content': answer.get('content')}}], 'usage': answer.get('usage')}
            record['inference_transport'] = 'gateway'
        else:
            response = inference.call('POST', '/chat/completions', dict(chat, model=view['served_model_name']))
        if response.get('model') != view['served_model_name']:
            raise RuntimeError('inference response did not identify the requested model/adapter')
        content = response.get('choices', [{}])[0].get('message', {}).get('content', '')
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError('vLLM returned no assistant text')
        record['inference'] = {'model': response['model'], 'content': content, 'usage': response.get('usage')}
        print('[inference] ' + json.dumps(record['inference'], ensure_ascii=False), flush=True)
        success = True
    finally:
        if deployment_id and (not keep_running or not success):
            client.call('POST', '/api/deployments/' + deployment_id + '/stop')
            wait_for('stop', lambda: client.call('GET', '/api/deployments/' + deployment_id),
                     lambda v: v.get('status') == 'stopped' and v.get('runtime_state') == 'stopped', 120)
            record['stopped'] = True
        record['passed'] = success
        save(record)
    if success and record.get('stopped'):
        final = client.call('GET', '/api/operations/' + record['operation_id'])
        if final.get('status') != 'succeeded':
            record['passed'] = False
            save(record)
            raise RuntimeError('runtime stop changed the completed provisioning Operation')
    return record


def command(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Docker preflight failed: ' + ' '.join(args[:3]))
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='http://127.0.0.1:13000')
    parser.add_argument('--model-version-id', default='cceae526-7b7c-47d4-870b-117372b51885')
    parser.add_argument('--gpu-id', type=int, default=0)
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--keep-running', action='store_true', help='leave a successfully tested runtime running')
    parser.add_argument('--via-gateway', action='store_true', help='send chat through Gateway; still verify the vLLM model list directly')
    parser.add_argument('--resume', type=Path, help='reuse an interrupted run JSON request record')
    args = parser.parse_args()
    if args.gpu_id < 0 or not 60 <= args.timeout <= 2700:
        parser.error('gpu-id must be nonnegative and timeout must be 60..2700')
    root = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != root:
        parser.error('run from ~/workspace/agent_project/model_platform')
    if args.resume:
        record_path = args.resume.resolve()
        record = json.loads(record_path.read_text())
        if record.get('stopped'):
            print('This recorded runtime was already stopped; use a new run for another deployment.')
            return
        uuid.UUID(record['idempotency_key'])
        if record['request'].get('serving', {}).get('engine') != 'vllm':
            raise RuntimeError('resume record is not a vLLM request')
        if args.via_gateway and not record.get('via_gateway'):
            raise RuntimeError('resume must retain its original inference route; use a new run for Gateway chat')
    else:
        run_id = str(uuid.uuid4())
        record_path = root / 'tests' / 'results' / 'serving' / (run_id + '.json')
        record = {'api': args.api.rstrip('/'), 'idempotency_key': run_id, 'request': {
            'model_version_id': str(uuid.UUID(args.model_version_id)),
            'serving': {'engine': 'vllm', 'max_model_len': 2048, 'gpu_memory_utilization': 0.5},
            'resources': {'gpu_ids': [args.gpu_id]},
        }}
        if args.via_gateway:
            record['via_gateway'] = True
    client = API(record['api'])
    lineage = source_lineage(client, record['request']['model_version_id'])
    for key, value in lineage.items():
        if key in record and record[key] != value:
            raise RuntimeError('resume source lineage changed: ' + key)
    record.update(lineage)
    # Values in full Compose config can contain credentials. Only read locally.
    config = json.loads(command(['docker', 'compose', 'config', '--format', 'json']))
    image = config['services']['deployment']['environment']['VLLM_IMAGE']
    command(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
    serving_job = config['services']['dispatcher']['environment']['DEPLOYMENT_SERVING_JOB_IMAGE']
    if command(['docker', 'image', 'inspect', '--format', '{{index .Config.Labels "model-platform.deployment-serving"}}', serving_job]) != '1':
        raise RuntimeError('serving coordinator image contract is missing')
    record_path.parent.mkdir(parents=True, exist_ok=True)
    def save(value):
        temp = record_path.with_suffix('.pending')
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        temp.replace(record_path)
    save(record)
    print('[record] ' + str(record_path), flush=True)
    print('[gpu] ' + str(record['request']['resources']['gpu_ids']), flush=True)
    result = exercise(client, record, args.timeout, save=save, keep_running=args.keep_running)
    print('[summary] ' + json.dumps(result, ensure_ascii=False), flush=True)
    print('PASS: idempotent request, real vLLM readiness and chat inference; ' +
          ('runtime stopped, logs/cache retained.' if result.get('stopped') else 'runtime kept running.'), flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print('FAIL: ' + str(exc), flush=True)
        raise SystemExit(1)
