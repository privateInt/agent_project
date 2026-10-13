"""Read-only catalog/terminal-state snapshot before and after planned service restart."""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
from common import OpsError, private_json, require
from platform_ops import api_get

COLLECTIONS = {'models': 'model_id', 'model-versions': 'model_version_id', 'datasets': 'dataset_id',
               'training-runs': 'training_run_id', 'deployments': 'deployment_id', 'operations': 'operation_id'}
# Runtime observation timestamps change on GET/reconciliation. Compare durable identities/state instead.
FIELDS = ('name', 'model_type', 'model_id', 'model_version_id', 'version', 'state', 'status',
          'source_type', 'artifact_kind', 'source_model_version_id', 'output_model_version_id',
          'training_run_id', 'deployment_id', 'operation_id', 'submission_status', 'error_code')


def capture(api):
    result = {'format': 1, 'api': api, 'collections': {}, 'read_only': True}
    api_get(api, '/health')
    for collection, key in COLLECTIONS.items():
        rows, cursor, observed = {}, '', set()
        for _ in range(10000):
            query = {'limit': 100}
            if cursor:
                query['after_id' if collection == 'datasets' else 'cursor'] = cursor
            data = api_get(api, '/api/' + collection + '?' + urlencode(query))
            items = data.get('datasets' if collection == 'datasets' else 'items')
            require(isinstance(items, list), 'Invalid list: ' + collection)
            for row in items:
                identity = row[key]
                require(str(uuid.UUID(identity)) == identity and identity not in rows, 'Duplicate/invalid identity')
                if collection in ('operations', 'training-runs'):
                    require(row['status'] in ('succeeded', 'failed', 'cancelled'), 'Finish or cancel active work before the restart snapshot')
                if collection == 'deployments':
                    require(row['status'] in ('stopped', 'succeeded', 'failed'), 'Stop active deployments before the restart snapshot')
                if collection == 'model-versions':
                    require(row['state'] in ('ready', 'failed'), 'Wait for current model imports')
                rows[identity] = {field: row[field] for field in FIELDS if field in row}
            cursor = data.get('next_after_id' if collection == 'datasets' else 'next_cursor')
            if not cursor:
                break
            require(cursor not in observed, 'Repeated pagination cursor')
            observed.add(cursor)
        else:
            raise OpsError('Catalog exceeded capture page limit')
        result['collections'][collection] = rows
    return result


def compare(before, after):
    require(before.get('format') == after.get('format') == 1 and before.get('read_only') is True,
            'Invalid before snapshot')
    require(before['api'] == after['api'], 'Use the same Gateway endpoint')
    require(set(before['collections']) == set(COLLECTIONS), 'Incomplete before snapshot')
    require(before['collections'] == after['collections'], 'Catalog identity/state changed; inspect both reports')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='http://127.0.0.1:13000')
    parser.add_argument('--report', required=True)
    parser.add_argument('--compare', help='Previously captured report, before planned maintenance/restart')
    args = parser.parse_args()
    require(not Path(args.report).exists(), 'Use a new report filename')
    data = capture(args.api)
    # Store observations even when comparison fails, without overwriting the baseline.
    private_json(args.report, data)
    if args.compare:
        compare(json.loads(Path(args.compare).read_text()), data)
    print('[report] ' + str(Path(args.report).absolute()))
    print('PASS: ' + ('catalog identity and terminal state preserved across restart' if args.compare else 'read-only idle catalog snapshot captured'))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('FAIL: ' + (str(exc) if isinstance(exc, OpsError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
