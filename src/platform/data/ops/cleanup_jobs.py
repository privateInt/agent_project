#!/usr/bin/env python3
"""Inspect/remove stopped collection, refinement, import and deployment Jobs.

Manual cleanup does not archive container logs. It never removes images,
volumes, source files, model-serving containers, or training Jobs. Scheduled
Model GC remains responsible for training Jobs and archived model logs.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import re
import subprocess
import urllib.error
import urllib.request
import uuid

PREFIXES = {'data': ('data-platform.', 'dispatcher', 14000),
            'model': ('model-platform.', 'operation-dispatcher', 13000)}
TYPES = {'collection.crawl': 'crawl_run', 'collection.browser': 'crawl_run',
         'refinement.parse': 'refinement_run', 'management.import-huggingface': 'model_version',
         'deployment.apply': 'deployment', 'deployment.serve': 'deployment'}
INSPECT = ('{"ID":{{json .Id}},"Name":{{json .Name}},"Labels":{{json .Config.Labels}},'
           '"RestartCount":{{json .RestartCount}},"AutoRemove":{{json .HostConfig.AutoRemove}},'
           '"RestartPolicy":{{json .HostConfig.RestartPolicy.Name}},"State":{{json .State}}}')


def docker(*args):
    try:
        return subprocess.run(['docker', *args], check=True, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Docker 명령을 확인하지 못했습니다. 컨테이너를 유지합니다.') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def operation(api, op_id):
    try:
        with urllib.request.build_opener(NoRedirect()).open(api.rstrip('/') + '/api/operations/' + op_id, timeout=10) as response:
            raw = response.read((1 << 20) + 1)
        if len(raw) > 1 << 20:
            raise ValueError()
        return json.loads(raw)
    except (OSError, ValueError):
        raise ValueError('Operation API 조회 실패. 컨테이너를 유지합니다.') from None


def valid_uuid(value):
    try:
        parsed = uuid.UUID(value)
        return parsed.int != 0 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        return False


def timestamp(value):
    # Docker uses RFC3339Nano; Python 3.10 accepts microsecond precision.
    value = re.sub(r'(\.\d{6})\d+', r'\1', value)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('시간대 없는 종료 시각')
    return parsed


def snapshot(platform, container_id, api, *, call=docker, get=operation):
    if not re.fullmatch('[0-9a-f]{64}', container_id):
        raise ValueError('전체 컨테이너 ID가 필요합니다.')
    prefix, owner, _ = PREFIXES[platform]
    value = json.loads(call('container', 'inspect', '--format', INSPECT, container_id))
    labels, state = value.get('Labels') or {}, value['State']
    op_id, token = labels.get(prefix + 'operation_id'), labels.get(prefix + 'dispatch_token')
    execution = labels.get(prefix + 'execution_type')
    allowed = ('collection.crawl', 'collection.browser', 'refinement.parse') if platform == 'data' else (
        'management.import-huggingface', 'deployment.apply', 'deployment.serve')
    if (value.get('ID') != container_id or labels.get(prefix + 'owner') != owner
            or not valid_uuid(op_id) or not valid_uuid(token) or execution not in allowed
            or value.get('Name') != '/' + platform + '-op-' + op_id + '-' + token
            or labels.get(prefix + 'requested_node')):
        raise ValueError('지원되는 로컬 작업의 소유권·이름을 확인하지 못했습니다.')
    if (state.get('Status') != 'exited' or state.get('Running') or state.get('Paused') or state.get('Restarting')
            or value.get('RestartCount') != 0 or value.get('AutoRemove')
            or value.get('RestartPolicy') not in ('', 'no')):
        raise ValueError('종료된 일회성 작업이 아닙니다.')
    op = get(api, op_id)
    if (op.get('operation_id') != op_id or op.get('status') not in ('succeeded', 'failed', 'cancelled')
            or op.get('resource_type') != TYPES[execution] or not valid_uuid(op.get('resource_id'))):
        raise ValueError('Operation의 최종 상태·대상 자료를 확인하지 못했습니다.')
    if platform == 'data' and op['resource_id'] != labels.get(prefix + 'resource_id'):
        raise ValueError('작업의 자료 ID가 일치하지 않습니다.')
    if op.get('execution_type') and op['execution_type'] != execution:
        raise ValueError('작업의 실행 종류가 일치하지 않습니다.')
    started, finished = timestamp(state['StartedAt']), timestamp(state['FinishedAt'])
    # Model's public single-operation response omits completed_at. Manual
    # removal still requires terminal API state; use the container age there.
    completed = timestamp(op['completed_at']) if op.get('completed_at') else finished
    now = datetime.now(timezone.utc)
    if started > finished or max(finished, completed) > now:
        raise ValueError('작업 종료 시각이 올바르지 않습니다.')
    return {'runtime': value, 'operation': {k: op.get(k) for k in
            ('operation_id', 'resource_type', 'resource_id', 'status', 'completed_at', 'error_code')},
            'ended_at': max(finished, completed)}


def cleanup(platform, container_id, api, *, apply=False, include_failed=False, min_age_hours=1,
            call=docker, get=operation):
    before = snapshot(platform, container_id, api, call=call, get=get)
    state, op = before['runtime']['State'], before['operation']
    if not include_failed and (op['status'] != 'succeeded' or state['ExitCode'] != 0 or state.get('OOMKilled')):
        return 'kept_failure'
    if (datetime.now(timezone.utc) - before['ended_at']).total_seconds() < min_age_hours * 3600:
        return 'kept_recent'
    if not apply:
        return 'would_remove'
    after = snapshot(platform, container_id, api, call=call, get=get)
    if after != before:
        raise ValueError('삭제 직전 상태가 변경되었습니다. 컨테이너를 유지합니다.')
    call('rm', container_id)  # No force, volume or prune operation.
    return 'removed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--include-failed', action='store_true')
    parser.add_argument('--min-age-hours', type=float, default=1)
    parser.add_argument('--platform', choices=['both', 'data', 'model'], default='both')
    parser.add_argument('--data-api', default='http://127.0.0.1:14000')
    parser.add_argument('--model-api', default='http://127.0.0.1:13000')
    args = parser.parse_args()
    if not math.isfinite(args.min_age_hours) or args.min_age_hours < 0:
        parser.error('--min-age-hours must be finite and nonnegative')
    counts = {}
    for platform, (prefix, owner, _) in PREFIXES.items():
        if args.platform not in ('both', platform):
            continue
        ids = docker('ps', '-aq', '--no-trunc', '--filter', 'label=' + prefix + 'owner=' + owner,
                     '--filter', 'status=exited').split()
        for container_id in ids:
            try:
                action = cleanup(platform, container_id, getattr(args, platform + '_api'), apply=args.apply,
                                 include_failed=args.include_failed, min_age_hours=args.min_age_hours)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                action = 'retained'
                print('[retained]', platform, container_id[:12], str(error))
            else:
                print('[' + action + ']', platform, container_id[:12])
            counts[action] = counts.get(action, 0) + 1
    print(json.dumps({'mode': 'apply' if args.apply else 'preview', 'counts': counts}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit('FAIL: ' + str(error)) from None
