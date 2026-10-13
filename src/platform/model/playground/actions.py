"""Fixed API actions and secret-free retry tickets. No network on restoration."""
import base64
import hashlib
import json
import re
import uuid

from api import APIError
from state import identifier

SPECS = {
    'add_model': ('모델 추가', 'models', '', 'model_version_id'),
    'add_dataset': ('데이터셋 추가', 'datasets', '', 'dataset_version_id'),
    'model': ('모델 등록', 'models', '/api/models', 'model_id'),
    'import': ('모델 가져오기', 'models', '/api/models/{target}/imports/huggingface', 'model_version_id'),
    'dataset': ('데이터셋 등록', 'datasets', '/api/datasets', 'dataset_id'),
    'upload': ('데이터셋 버전 업로드', 'datasets', '/api/datasets/{target}/versions', 'dataset_version_id'),
    'train': ('학습 시작', 'training', '/api/training-runs', 'training_run_id'),
    'deploy': ('배포 시작', 'deployments', '/api/deployments', 'deployment_id'),
    'resume': ('체크포인트 재개', 'training', '/api/training-runs/{target}/resume', 'training_run_id'),
    'retry_output': ('출력 업로드 재시도', 'training', '/api/training-runs/{target}/output/retry', 'training_run_id'),
}


def payload(kind, target='', body=None, data=None):
    if kind not in SPECS:
        raise ValueError('unsupported action')
    if kind not in ('model', 'dataset', 'add_model', 'add_dataset'):
        target = identifier(target)
    elif target:
        raise ValueError('unexpected target')
    # Freeze mutable form data at click time.
    return {'kind': kind, 'target': target, 'body': json.loads(json.dumps(body, allow_nan=False)), 'data': data}


def fingerprint(value):
    public = {k: value[k] for k in ('kind', 'target', 'body')}
    public['file_sha256'] = hashlib.sha256(value['data']).hexdigest() if value['data'] is not None else None
    return hashlib.sha256(json.dumps(public, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def ticket(value, key=None):
    return {'v': 2, 'kind': value['kind'], 'target': value['target'],
            'key': identifier(key or str(uuid.uuid4())), 'sha256': fingerprint(value)}


def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode()


def decode(raw):
    if len(raw) > 1024:
        raise ValueError('invalid ticket')
    value = json.loads(base64.b64decode(raw, altchars=b'-_', validate=True))
    if not isinstance(value, dict) or set(value) != {'v', 'kind', 'target', 'key', 'sha256'}:
        raise ValueError('invalid ticket')
    if type(value['v']) is not int or value['v'] != 2 or value['kind'] not in SPECS:
        raise ValueError('unsupported ticket')
    if not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256']):
        raise ValueError('invalid fingerprint')
    payload(value['kind'], value['target'])
    value['key'] = identifier(value['key'])
    return value


def request_key(saved, value):
    if saved['kind'] != value['kind'] or saved['target'] != value['target'] or saved['sha256'] != fingerprint(value):
        raise APIError('이전 요청과 입력이 다릅니다. 같은 값·파일·토큰으로 결과를 확인하세요.', 409, 'PENDING_INPUT_CHANGED')
    return saved['key']


def execute(client, saved, value, on_registered=None):
    key = request_key(saved, value)
    kind, target = value['kind'], value['target']
    if kind in ('add_model', 'add_dataset'):
        # One UI action, two existing APIs. Deterministic separate keys make a
        # replay after either lost response return the same parent and version.
        parent_kind, version_kind = ('model', 'import') if kind == 'add_model' else ('dataset', 'upload')
        parent_value = payload(parent_kind, body=value['body']['registration'])
        parent_key = str(uuid.uuid5(uuid.UUID(key), kind + '/registration'))
        parent = execute(client, ticket(parent_value, parent_key), parent_value)
        resource_id = identifier(parent[SPECS[parent_kind][3]])
        if on_registered is not None:
            on_registered(parent_kind, resource_id)
        version_value = payload(version_kind, resource_id, value['body']['version'], value['data'])
        version_key = str(uuid.uuid5(uuid.UUID(key), kind + '/version'))
        result = execute(client, ticket(version_value, version_key), version_value)
        return {**result, SPECS[parent_kind][3]: resource_id}
    if kind == 'upload':
        result = client.upload_dataset(target, value['body']['version'], value['data'], key)
    else:
        result = client.call('POST', SPECS[kind][2].format(target=target), value['body'], key)
    try:
        identifier(result[SPECS[kind][3]])
        if kind in ('import', 'train', 'deploy', 'resume'):
            identifier(result['operation_id'])
        if kind == 'upload' and result['dataset_id'] != target:
            raise ValueError('wrong dataset')
        if kind == 'retry_output' and result['training_run_id'] != target:
            raise ValueError('wrong run')
    except (KeyError, TypeError, ValueError, AttributeError):
        raise APIError('응답을 확인하지 못했습니다. 같은 요청을 다시 확인하세요.', code='INVALID_RESPONSE') from None
    return result
