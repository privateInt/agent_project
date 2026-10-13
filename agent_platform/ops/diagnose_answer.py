#!/usr/bin/env python3
"""Read stored reference diagnostics. No inference, replay or file/remote writes."""
import argparse
import json
import os
from pathlib import Path
import re
import stat

from smoke_console import ROOT, Reader, identifier

STEPS = ('validate', 'search', 'context', 'generate', 'finalize')
STATES = {'pending', 'accepted', 'running', 'succeeded', 'failed', 'skipped', 'interrupted'}
ISSUES = {'MISSING_REFERENCE', 'MALFORMED_REFERENCE', 'REFERENCE_OUT_OF_RANGE'}
COUNTS = ('selected_source_count', 'max_tokens', 'recognized_count', 'malformed_count',
          'out_of_range_count', 'completion_tokens', 'answer_bytes')


def request_run_id(path):
    """Read only the existing receipt; never make or change a request identity."""
    path = path.expanduser()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1048576:
        raise ValueError('요청 파일은 현재 사용자 소유·0600인 1 MiB 이하 일반 파일이어야 합니다.')
    request = json.loads(path.read_text())
    run_id = (request.get('receipt') or {}).get('agent_run_id')
    if not run_id:
        raise ValueError('저장된 실행 ID가 없습니다. 기존 요청을 lookup하거나 --run-id로 지정하세요. 새 run은 실행하지 마세요.')
    return identifier(run_id)


def diagnostic_summary(value):
    if value is None:
        return {'status': 'not_recorded', 'reason': 'NO_STORED_REFERENCE_DIAGNOSTICS'}
    if (not isinstance(value, dict) or value.get('schema') != 1
            or value.get('scope') != 'reference_syntax_only'
            or value.get('status') not in ('not_checked', 'valid', 'invalid', 'no_evidence_signal')
            or value.get('finish_reason') not in ('stop', 'length', 'other')
            or not isinstance(value.get('issues'), list)
            or any(not isinstance(x, str) or x not in ISSUES for x in value['issues'])):
        raise ValueError('지원하지 않는 인용 진단 형식입니다.')
    result = {k: value[k] for k in ('schema', 'scope', 'status', 'finish_reason', 'issues')}
    for key in COUNTS:
        if key not in value: continue
        if type(value[key]) is not int or not 0 <= value[key] <= 10000000:
            raise ValueError('인용 진단 수치가 올바르지 않습니다.')
        result[key] = value[key]
    return result


def diagnose(reader, run_id):
    run_id = identifier(run_id)
    route = '/api/agent-runs/' + run_id
    run = reader.get(route)
    steps = reader.get(route + '/steps')['items']
    if run.get('agent_run_id') != run_id or [s['step_id'] for s in steps] != list(STEPS):
        raise ValueError('조회한 실행 또는 단계 식별자가 다릅니다.')
    if run.get('state') not in STATES or any(s.get('state') not in STATES for s in steps):
        raise ValueError('지원하지 않는 실행 상태입니다.')
    result = {'result': 'diagnosed', 'agent_run_id': run_id,
              'state': run['state'], 'inference_executed': False, 'changes_made': False,
              'steps': [{'step': s['step_id'], 'state': s['state']} for s in steps]}
    for key in ('agent_version_id', 'request_id', 'tool_call_id'):
        if run.get(key): result[key] = identifier(run[key])
    error = run.get('error') or {}
    result['error'] = {k: v for k, v in error.items() if k in ('code', 'stage')
                       and isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', v)}
    detail = {s['step_id']: s.get('detail') or {} for s in steps}
    selected = detail['context'].get('selected_chunk_ids')
    if isinstance(selected, list): result['selected_source_count'] = len(selected)
    report = diagnostic_summary(detail['generate'].get('reference_diagnostics'))
    if ('selected_source_count' in report and 'selected_source_count' in result
            and report['selected_source_count'] != result['selected_source_count']):
        raise ValueError('인용 진단과 저장된 문맥 개수가 다릅니다.')
    result['reference_diagnostics'] = report
    # This is a read-operation result, never an acceptance of answer quality.
    result['answer_delivery_verified'] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--run-id')
    selection.add_argument('--request-file', type=Path)
    parser.add_argument('--url', default='http://127.0.0.1:16100')
    parser.add_argument('--token-file', type=Path, default=ROOT / '.secrets/api-token')
    args = parser.parse_args()
    run_id = args.run_id if args.run_id else request_run_id(args.request_file)
    reader = Reader(args.url, args.token_file.expanduser().read_text().strip())
    print(json.dumps(diagnose(reader, run_id), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        raise SystemExit('FAIL: ' + (str(exc) if type(exc) is ValueError else '연결·실행 ID·저장된 요청 파일을 확인하세요.'))
