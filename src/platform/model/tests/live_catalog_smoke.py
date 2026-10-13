"""Read-only REST catalog checks. Never create datasets, Jobs, or GPU runtimes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
from urllib.parse import urlencode
import uuid

from live_serving_smoke import API, APIError
from smoke_timestamps import timestamp_key


COLLECTIONS = {
    'models': 'model_id', 'model-versions': 'model_version_id',
    'training-runs': 'training_run_id', 'deployments': 'deployment_id',
    'operations': 'operation_id',
}
CHANGED_FILTERS = {
    'models': {'model_type': 'ocr'}, 'model-versions': {'state': 'failed'},
    'training-runs': {'status': 'failed'}, 'deployments': {'status': 'stopped'},
    'operations': {'status': 'failed'},
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def get(client, path, **query):
    return client.call('GET', path + ('?' + urlencode(query) if query else ''))


def check_page(page, collection, limit, seen, previous=None):
    require(isinstance(page, dict) and isinstance(page.get('items'), list)
            and isinstance(page.get('next_cursor'), str), collection + ': invalid list envelope')
    items = page['items']
    require(len(items) <= limit, collection + ': limit was ignored')
    require(not page['next_cursor'] or len(items) == limit,
            collection + ': cursor returned for an incomplete page')
    key = COLLECTIONS[collection]
    for item in items:
        identity = item[key]
        require(str(uuid.UUID(identity)) == identity and identity not in seen,
                collection + ': invalid or duplicate ID')
        created = timestamp_key(item['created_at'])
        current = (created, identity)
        require(previous is None or current < previous, collection + ': newest-first order changed')
        previous = current
        seen.add(identity)
        for secret in ('hf_token', 'dispatch_token', 'dispatch_owner', 'execution_instance_id'):
            require(secret not in item, collection + ': private field exposed: ' + secret)
        if collection == 'training-runs':
            require('result_json' not in item and 'result' not in item, 'large training result in list')
        if collection == 'deployments' and (
                item.get('runtime_state') != 'ready' or item.get('desired_state') != 'running'):
            require(not item.get('endpoint'), 'inactive deployment advertised an inference endpoint')
    return previous


def expect_bad_query(client, path, **query):
    try:
        get(client, path, **query)
    except APIError as exc:
        require(exc.status == 400, 'invalid query returned HTTP ' + str(exc.status))
    else:
        raise RuntimeError('invalid query was accepted: ' + path)


def exercise(client, record, save):
    record['collections'] = {}
    for collection in COLLECTIONS:
        path = '/api/' + collection
        first = get(client, path, limit=2)
        seen = set()
        previous = check_page(first, collection, 2, seen)
        result = {'first_page_count': len(first['items']), 'pagination_observed': False}
        if first['next_cursor']:
            second = get(client, path, limit=2, cursor=first['next_cursor'])
            check_page(second, collection, 2, seen, previous)
            result.update(pagination_observed=True, second_page_count=len(second['items']))
            expect_bad_query(client, path, limit=2, cursor=first['next_cursor'],
                             **CHANGED_FILTERS[collection])
        expect_bad_query(client, path, cursor='invalid!')
        record['collections'][collection] = result
        save(record)
        print('[catalog] ' + collection + ' ' + json.dumps(result), flush=True)
    expect_bad_query(client, '/api/models', limit=0)
    expect_bad_query(client, '/api/models', limit=101)
    expect_bad_query(client, '/api/models', limt=2)
    expect_bad_query(client, '/api/models', q='x' * 201)
    require(get(client, '/api/models', q='absent-' + uuid.uuid4().hex)['items'] == [],
            'model search returned unrelated records')
    datasets = get(client, '/api/datasets', q='absent-' + uuid.uuid4().hex)
    require(datasets.get('datasets') == [] and datasets.get('next_after_id') == '',
            'dataset search/legacy envelope changed')
    target = record.get('model_version_id')
    if target:
        detail = get(client, '/api/model-versions/' + target)
        model = get(client, '/api/models/' + detail['model_id'])
        versions = get(client, '/api/models/' + detail['model_id'] + '/versions',
                       q=detail['version'], state=detail['state'],
                       artifact_kind=detail['artifact_kind'], limit=100)
        check_page(versions, 'model-versions', 100, set())
        matching = [v for v in versions['items'] if v['model_version_id'] == target]
        require(len(matching) == 1, 'target version absent in its filtered list; inspect pagination')
        summary = matching[0]
        require(summary['model_name'] == model['name'] and summary['model_type'] == model['model_type'],
                'version summary lost the model label')
        for key in ('model_id', 'state', 'source_type', 'artifact_kind', 'source_model_version_id'):
            require(summary.get(key) == detail.get(key), 'version summary disagrees with detail: ' + key)
        record['target_version_verified'] = True
    record['passed'] = True
    save(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='http://127.0.0.1:13000')
    parser.add_argument('--model-version-id', type=lambda value: str(uuid.UUID(value)))
    args = parser.parse_args()
    record = {'api': args.api, 'model_version_id': args.model_version_id,
              'python_version': platform.python_version(),
              'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False}
    report = Path(__file__).resolve().parent / 'results' / 'catalog' / (str(uuid.uuid4()) + '.json')
    report.parent.mkdir(parents=True, exist_ok=True)

    def save(value):
        report.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    save(record)
    print('[record] ' + str(report), flush=True)
    try:
        exercise(API(args.api), record, save)
    except Exception as exc:
        record['error'] = str(exc)
        save(record)
        raise SystemExit('FAIL: ' + str(exc)) from None
    print('PASS: read-only catalog, filters, observed pagination and public summaries; no Job started.')


if __name__ == '__main__':
    main()
