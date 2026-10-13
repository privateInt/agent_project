"""Read-only verification after the prepared reset and after an optional restart."""
import argparse
import json
from pathlib import Path
import sys
import time

from common import Context, OpsError, containers, private_json, public_container, require
from platform_ops import api_get, audit
from reset_test_data import Backend, KINDS, TABLES, db_inventory, hash_json, load_plan, http
from urllib.parse import urlencode


def empty_catalog(api):
    counts = {}
    for name in ('models', 'model-versions', 'datasets', 'training-runs', 'deployments', 'operations'):
        page = api_get(api, '/api/' + name + '?limit=1')
        field = 'datasets' if name == 'datasets' else 'items'
        require(isinstance(page.get(field), list), 'Invalid catalog response: ' + name)
        require(not page[field] and not page.get('next_cursor') and not page.get('next_after_id'),
                'Catalog is not empty: ' + name)
        counts[name] = 0
    return counts


def wait_ready(api, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        try:
            ctx = Context()
            pending = [s for s, c in ctx.services.items() if c['State'].get('Status') != 'running'
                       or c['State'].get('Health', {}).get('Status') in ('starting', 'unhealthy')]
            require(not pending, 'Waiting for services: ' + ','.join(pending))
            require(ctx.services['playground']['State'].get('Health', {}).get('Status') == 'healthy', 'Waiting for Playground health')
            api_get(api, '/health')
            return ctx
        except Exception as exc:
            message = str(exc) if isinstance(exc, OpsError) else type(exc).__name__
            if message != previous:
                print('[waiting] ' + message, flush=True)
                previous = message
            if time.monotonic() >= deadline:
                raise OpsError('Service readiness timeout; inspect Compose logs') from None
            time.sleep(3)


def verify(ctx, backend, plan, reset_receipt, api):
    require(reset_receipt.get('passed') is True and reset_receipt.get('plan_sha256') == hash_json(plan)
            and reset_receipt.get('phase') == 'complete', 'Successful matching reset receipt required')
    require(backend.binding()['config_sha256'] == plan['binding']['config_sha256'], 'Compose configuration changed since reset')
    result = {'format': 1, 'scope': 'read-only-after-test-reset', 'reset_passed': False,
              'capacity_passed': False, 'passed': False, 'read_only': True}
    result['catalog'] = empty_catalog(api)
    db = db_inventory(ctx)
    require(all(row['count'] == 0 for tables in db.values() for row in tables.values()), 'Application database is not empty')
    result['database_rows'] = {name: {table: row['count'] for table, row in tables.items()} for name, tables in db.items()}
    items = containers()
    require(not any((c['Config'].get('Labels') or {}).get('model-platform.owner') in ('operation-dispatcher', 'deployment-serving') for c in items),
            'A model Job/runtime exists after reset')
    result['buckets'] = backend.objects(plan['identities'])
    require(all(not value['objects'] and not value['uploads'] for value in result['buckets'].values()), 'A platform bucket is not empty')
    result['volumes'] = {}
    for k in KINDS:
        entries = backend.volume(k, {**plan['identities'], 'operations': []})
        if k == 'gpu-admission':
            require(all(r['path'] in ('admission.lock', 'claims.json') and r['type'] == 'file' for r in entries), 'Unexpected admission content')
        else:
            require(not entries, 'A reset data volume is not empty: ' + k)
        result['volumes'][k] = {'entries': len(entries), 'test_data_empty': True}
    require(backend.offsets(-2) == plan['kafka']['end'] == backend.offsets(-1), 'Kafka contains new/unremoved events')
    result['kafka'] = {'no_old_records': True, 'no_new_events': True}
    for row in plan['mlflow_runs']:
        got = http(backend.mlflow_endpoint, '/api/2.0/mlflow/runs/get?' + urlencode({'run_id': row['run_id']}), missing=True)
        require(got is None or got['run']['info'].get('lifecycle_stage') == 'deleted', 'A platform MLflow run is still active')
    result['mlflow'] = {'platform_runs_deleted': len(plan['mlflow_runs']), 'mode': 'soft-delete; shared metadata retained'}
    result['reset_passed'] = True
    result['audit'] = audit(ctx, api)
    result['capacity_passed'] = all(v['pressure'] not in ('critical', 'unavailable') for v in result['audit']['volumes'].values()) and all(v['pressure'] not in ('critical', 'unavailable') for v in result['audit']['minio_paths'])
    result['passed'] = result['audit']['passed'] and result['capacity_passed']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--reset-report', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--api', default='http://127.0.0.1:13000')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    require(1 <= args.timeout <= 600, 'Timeout must be between 1 and 600 seconds')
    require(not Path(args.report).exists(), 'Use a new report filename')
    plan = load_plan(args.plan)
    reset_receipt = json.loads(Path(args.reset_report).read_text())
    ctx = wait_ready(args.api, args.timeout)
    result = verify(ctx, Backend(ctx), plan, reset_receipt, args.api)
    private_json(args.report, result)
    print('[report] ' + str(Path(args.report).absolute()))
    print(json.dumps({'reset_passed': result['reset_passed'], 'capacity_passed': result['capacity_passed'],
                      'passed': result['passed'], 'catalog': result['catalog']}, ensure_ascii=False))
    if not result['passed']:
        print('Reset checks passed, but operational audit still has a problem. Do not repeat data deletion; inspect the report.')
        return 2
    print('PASS: empty platform data, services ready, no old events and no critical capacity warning.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('FAIL: ' + (str(exc) if isinstance(exc, OpsError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
