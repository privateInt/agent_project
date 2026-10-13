"""Read-only MLflow verification for a passed running-cancellation smoke report.
Does not create/cancel Jobs, update MLflow, or rewrite the input report.
"""
import argparse
import json
from pathlib import Path
import re
import urllib.error
from urllib.parse import urlencode
import uuid

from live_serving_smoke import API


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def inspect(record, platform, mlflow):
    require(record.get('passed') is True and record.get('mode') == 'running'
            and record.get('running_observed') is True, 'passed running-cancellation report required')
    run_id, operation_id = record['training_run_id'], record['operation_id']
    require(str(uuid.UUID(run_id)) == run_id and str(uuid.UUID(operation_id)) == operation_id,
            'canonical training and operation IDs required')
    run = platform.call('GET', '/api/training-runs/' + run_id)
    operation = platform.call('GET', '/api/operations/' + operation_id)
    require(run.get('training_run_id') == run_id and run.get('operation_id') == operation_id
            and operation.get('operation_id') == operation_id, 'platform identity mismatch')
    require(run.get('status') == operation.get('status') == 'cancelled',
            'TrainingRun and Operation must remain cancelled')
    require(run.get('cancel_requested_at') and run.get('cancelled_at'), 'cancellation timestamps missing')
    require(not run.get('cancellation_error_code'), 'cancellation follow-up still pending: '
            + str(run.get('cancellation_error_code')))
    tracking_id = run.get('mlflow_run_id')
    require(isinstance(tracking_id, str) and bool(tracking_id.strip()), 'MLflow run ID missing; tracking not verified')
    response = mlflow.call('GET', '/api/2.0/mlflow/runs/get?' + urlencode({'run_id': tracking_id}))
    info = response.get('run', {}).get('info', {})
    require(info.get('run_id') == tracking_id, 'MLflow run identity mismatch')
    require(info.get('status') in ('KILLED', 'FAILED', 'FINISHED'), 'MLflow run is not terminal')
    end_time = info.get('end_time')
    if isinstance(end_time, str) and re.fullmatch(r'[0-9]{1,19}', end_time):
        end_time = int(end_time)
    require(type(end_time) is int and 0 < end_time <= 2**63 - 1, 'MLflow end_time missing or invalid')
    return {'training_run_id': run_id, 'operation_id': operation_id,
            'training_status': run['status'], 'operation_status': operation['status'],
            'mlflow_run_id': tracking_id, 'mlflow_status': info['status'],
            'mlflow_end_time': end_time, 'cancellation_error_code': '', 'passed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--mlflow-api', default='http://127.0.0.1:5000')
    args = parser.parse_args()
    record = json.loads(args.report.read_text())
    result = inspect(record, API(record['api']), API(args.mlflow_api))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('PASS: cancelled training and terminal MLflow run; read-only verification.')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, urllib.error.URLError) as exc:
        raise SystemExit('FAIL: ' + str(exc)) from None
