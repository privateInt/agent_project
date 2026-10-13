#!/usr/bin/env python3
"""Build a test index, or explicitly retry a failed version; keep exact replay journals."""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid


def api(base, path, method='GET', body=None, key=None):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Idempotency-Key'] = key
    request = Request(base.rstrip('/') + path, method=method, headers=headers, data=None if body is None else json.dumps(body, ensure_ascii=False).encode())
    try:
        with urlopen(request, timeout=105) as response:
            return json.load(response)
    except HTTPError as error:
        text = error.read(3000).decode(errors='replace')
        raise ValueError(f'HTTP {error.code}: {text}') from None


def save(path, data):
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def retry_failed_state(expected, previous_path, state_path):
    if previous_path.resolve() == state_path.resolve():
        raise ValueError('Retry requires a different --state-file; preserve the failed journal')
    previous = json.loads(previous_path.read_text())
    if previous.get('input') != expected:
        raise ValueError('Failed journal belongs to different input')
    index_id, version_id = previous.get('index_id'), previous.get('index_version_id')
    for value in (index_id, version_id):
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError('Failed journal must identify an index and version')
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if (state.get('input') != expected or state.get('index_id') != index_id
                or state.get('retry_of_version') != version_id):
            raise ValueError('Retry journal belongs to another execution; no file was overwritten')
        return state
    version = api(expected['base_url'], '/api/index-versions/' + version_id)
    if (version.get('state') != 'failed' or version.get('index_version_id') != version_id
            or version.get('index_id') != index_id
            or version.get('chunking_run_id') != expected['chunking_run_id']
            or version.get('embedding_profile', {}).get('deployment_id') != expected['deployment_id']):
        raise ValueError('Source version must be failed and match the requested index, chunks and model')
    index = api(expected['base_url'], '/api/indexes/' + index_id)
    if index.get('index_id') != index_id or index.get('state') != 'active' or index.get('active_version_id') != '':
        raise ValueError('Retry helper requires an active test index without an active version; use the UI otherwise')
    state = {
        'input': expected, 'index_id': index_id,
        'build_key': str(uuid.uuid4()), 'retry_of_version': version_id,
        'build_request': {'chunking_run_id': expected['chunking_run_id'],
                          'embedding_deployment_id': expected['deployment_id']},
    }
    save(state_path, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:15100')
    parser.add_argument('--chunking-run-id', default='6e3eaf24-c2dc-4756-87bb-83468a96c827')
    parser.add_argument('--deployment-id', default='44247297-5184-4a36-9225-7791c80d3add')
    parser.add_argument('--state-file', type=Path, default=Path('indexing_check_20260918.json'))
    parser.add_argument('--retry-failed-from', type=Path,
                        help='Create one new version in the failed test index, using a separate journal')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    expected = {'base_url': args.base_url, 'chunking_run_id': args.chunking_run_id, 'deployment_id': args.deployment_id}
    if args.retry_failed_from:
        state = retry_failed_state(expected, args.retry_failed_from, args.state_file)
    elif args.state_file.exists():
        state = json.loads(args.state_file.read_text())
        if state.get('input') != expected:
            raise ValueError('Journal belongs to different input; choose another --state-file')
    else:
        state = {'input': expected, 'create_key': str(uuid.uuid4()), 'build_key': str(uuid.uuid4()), 'name': 'K04 확인 ' + time.strftime('%Y-%m-%d %H:%M:%S')}
        save(args.state_file, state)
    if not state.get('index_id'):
        out = api(args.base_url, '/api/indexes', 'POST', {'name': state['name'], 'embedding_deployment_id': args.deployment_id}, state['create_key'])
        state['index_id'] = out['index_id']
        save(args.state_file, state)
    index_path = '/api/indexes/' + state['index_id']
    if not state.get('index_version_id'):
        body = state.get('build_request', {'chunking_run_id': args.chunking_run_id})
        out = api(args.base_url, index_path + '/builds', 'POST', body, state['build_key'])
        state['index_version_id'] = out['index_version_id']
        save(args.state_file, state)
    version_path = '/api/index-versions/' + state['index_version_id']
    deadline, previous = time.monotonic() + args.timeout, None
    while True:
        version = api(args.base_url, version_path)
        progress = (version['state'], version['indexed_count'], version['chunk_count'], version['error_code'])
        if progress != previous:
            print(json.dumps({'state': progress[0], 'indexed': progress[1], 'total': progress[2], 'error_code': progress[3]}, ensure_ascii=False), flush=True)
            previous = progress
        if version['state'] == 'ready':
            break
        if version['state'] in ('failed', 'deleting', 'deleted'):
            raise ValueError('Build did not become ready: ' + version['error_code'] + ' ' + version['error_message'])
        if time.monotonic() > deadline:
            raise ValueError('Wait timed out. Same command resumes observation; inference is not resubmitted.')
        time.sleep(2)
    profile = version['embedding_profile']
    if version['indexed_count'] < 1 or version['indexed_count'] != version['chunk_count'] or version['reference_state'] != 'held' or profile['deployment_id'] != args.deployment_id or profile['dimension'] != 1024:
        raise ValueError('Final build receipt differs')
    index = api(args.base_url, index_path)
    if index['active_version_id'] != version['index_version_id']:
        if state.get('retry_of_version') and index['active_version_id']:
            raise ValueError('Another version became active; choose activation in the UI')
        # Preserve the original CAS request across an ambiguous response/restart.
        body = state.setdefault('activation_request', {'index_version_id': version['index_version_id'], 'expected_active_version_id': index['active_version_id'], 'expected_revision': index['revision']})
        save(args.state_file, state)
        index = api(args.base_url, index_path + '/activate', 'POST', body)
    receipt = {'status': 'passed', 'index_id': index['index_id'], 'index_version_id': version['index_version_id'], 'active_version_id': index['active_version_id'], 'chunking_run_id': version['chunking_run_id'], 'chunk_set_sha256': version['chunk_set_sha256'], 'chunks': version['chunk_count'], 'indexed': version['indexed_count'], 'references': version['reference_state'], 'embedding_space_id': profile['embedding_space_id'], 'dimension': profile['dimension'], 'runtime_container_id': profile['runtime_container_id'], 'chunker': version['input_spec']['chunker'], 'canonical_ids': version['canonical_ids']}
    state['receipt'] = receipt
    save(args.state_file, state)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit('FAIL: ' + str(exc))
