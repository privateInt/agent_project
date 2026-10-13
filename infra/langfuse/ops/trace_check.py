#!/usr/bin/env python3
"""One persistent, metadata-only OTLP fixture; check only reads. Never calls an LLM."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import time
import urllib.parse
import uuid

from lf_common import (DEFAULT_STATE, Client, HttpFailure, Docker, require, read_json,
                       write_private, fingerprint, operation_lock)
import manage


def stamp(milliseconds):
    return dt.datetime.fromtimestamp(milliseconds / 1000, dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def millis(value):
    # Python 3.9 accepts microseconds; API timestamps may contain nanoseconds.
    value = re.sub(r'(\.\d{6})\d+(?=Z|[+-]\d{2}:\d{2}$)', r'\1', value)
    return round(dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)


def fixture(config, start_ms=None):
    start_ms = int(time.time() * 1000) - 2000 if start_ms is None else start_ms
    trace = uuid.uuid4().hex
    root, success, error = [uuid.uuid4().hex[:16] for _ in range(3)]
    spans = []
    for ident, name, parent, offset, duration, code, message in [
        (root, 'infra-langfuse-check', None, 0, 300, 1, 'synthetic_ok'),
        (success, 'synthetic-success', root, 20, 50, 1, 'synthetic_ok'),
        (error, 'synthetic-error', root, 100, 50, 2, 'intentional_synthetic_error'),
    ]:
        attributes = {
            'langfuse.trace.name': 'infra-langfuse-check', 'langfuse.environment': 'infra-check',
            'langfuse.observation.type': 'span', 'langfuse.observation.metadata.infra_owner': config['owner'],
            'langfuse.trace.metadata.infra_owner': config['owner'],
            'langfuse.observation.metadata.synthetic': 'true',
        }
        span = {'traceId': trace, 'spanId': ident, 'name': name, 'kind': 1,
                'startTimeUnixNano': str((start_ms + offset) * 1000000),
                'endTimeUnixNano': str((start_ms + offset + duration) * 1000000),
                'status': {'code': code, 'message': message},
                'attributes': [{'key': k, 'value': {'stringValue': v}} for k, v in attributes.items()]}
        if parent:
            span['parentSpanId'] = parent
        spans.append(span)
    payload = {'resourceSpans': [{'resource': {'attributes': [
        {'key': 'service.name', 'value': {'stringValue': 'agent-project-infra-check'}}]},
        'scopeSpans': [{'scope': {'name': 'infra.langfuse.acceptance', 'version': '1'}, 'spans': spans}]}]}
    return {'schema': 1, 'owner': config['owner'], 'project_id': config['project_id'],
            'version': manage.VERSION, 'trace_id': trace, 'payload': payload, 'payload_sha256': fingerprint(payload),
            'post_attempts': 0, 'accepted': False, 'terminal_rejection': False}


def validate_record(record, config):
    require(record['schema'] == 1 and record['owner'] == config['owner'] and record['project_id'] == config['project_id']
            and record['version'] == manage.VERSION and record['payload_sha256'] == fingerprint(record['payload']),
            '합성 추적의 프로젝트/원본 요청이 바뀌었습니다. 기존 파일을 보존하세요.')
    require(re.fullmatch('[0-9a-f]{32}', record['trace_id']), '합성 trace ID가 올바르지 않습니다.')
    spans = record['payload']['resourceSpans'][0]['scopeSpans'][0]['spans']
    require(len(spans) == 3 and all(x['traceId'] == record['trace_id'] for x in spans), '합성 span 계약이 다릅니다.')
    require(0 <= record['post_attempts'] <= 3, '합성 추적 제출 횟수가 올바르지 않습니다.')


def observations(client, record):
    spans = record['payload']['resourceSpans'][0]['scopeSpans'][0]['spans']
    start = min(int(x['startTimeUnixNano']) // 1000000 for x in spans)
    end = max(int(x['endTimeUnixNano']) // 1000000 for x in spans)
    query = urllib.parse.urlencode({'traceId': record['trace_id'], 'limit': 10,
                                   'fields': 'core,basic,metadata,io,trace_context',
                                   'fromStartTime': stamp(start - 1000), 'toStartTime': stamp(end + 1000)})
    response = client.request('GET', '/api/public/v2/observations?' + query)
    require(isinstance(response.get('data'), list) and not response.get('meta', {}).get('cursor'),
            '합성 추적 조회가 누락되었거나 예상보다 많은 결과가 있습니다.')
    return response['data']


def verify(items, record):
    spans = record['payload']['resourceSpans'][0]['scopeSpans'][0]['spans']
    expected = {s['spanId']: s for s in spans}
    require(len(items) <= len(expected) and len({x['id'] for x in items}) == len(items), '합성 추적 결과가 중복되었습니다.')
    for item in items:
        ident = item['id']
        require(ident in expected, '예상하지 않은 합성 span ID입니다.')
        span = expected[ident]
        require(item['traceId'] == record['trace_id'] and item['projectId'] == record['project_id'], '추적/프로젝트 ID가 다릅니다.')
        require((item.get('parentObservationId') or None) == span.get('parentSpanId') and item['name'] == span['name'],
                '합성 추적 부모/자식 연결이 다릅니다.')
        require(item['level'] == ('ERROR' if span['status']['code'] == 2 else 'DEFAULT')
                and item.get('statusMessage') == span['status']['message'], '합성 성공/오류 상태가 다릅니다.')
        for key, source in [('startTime', 'startTimeUnixNano'), ('endTime', 'endTimeUnixNano')]:
            require(item.get(key) and abs(millis(item[key]) - int(span[source]) // 1000000) <= 1, '합성 추적 시간이 다릅니다.')
        require(item.get('environment') == 'infra-check' and item.get('type') == 'SPAN', '합성 추적 환경/type이 다릅니다.')
        require(item.get('metadata', {}).get('infra_owner') == record['owner'], '합성 추적 메타데이터 소유권이 다릅니다.')
        require(item.get('input') in (None, '', 'null') and item.get('output') in (None, '', 'null'), '합성 추적에 입력/출력 본문이 있습니다.')
    return len(items) == len(expected)


def poll(client, record, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    while True:
        if verify(observations(client, record), record):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def execute(action, root, config, client, wait_seconds):
    path = root / 'trace-check.json'
    if path.exists():
        record = read_json(path)
        validate_record(record, config)
    else:
        require(action == 'run', '합성 추적 기록이 없습니다. 먼저 trace_check.py run을 실행하세요.')
        record = fixture(config)
        write_private(path, record)  # Persist exact body/IDs/timestamps before the first network write.
    sent = False
    passed = poll(client, record, 0)
    if not passed and action == 'run' and not record['accepted']:
        require(not record['terminal_rejection'], '합성 전송이 명시적으로 거절됐습니다. 원본 기록을 보존하고 원인을 확인하세요.')
        require(record['post_attempts'] < 3, '동일 합성 전송이 3회에 도달했습니다. check로 조회하고 원본을 보존하세요.')
        record['post_attempts'] += 1
        write_private(path, record)
        sent = True
        try:
            response = client.request('POST', '/api/public/otel/v1/traces', record['payload'])
        except HttpFailure as exc:
            # A lost response may mean accepted. Reuse the exact persisted IDs and body next time.
            if isinstance(exc.status, int) and 400 <= exc.status < 500 and exc.status not in (408, 429):
                record['terminal_rejection'] = True
                write_private(path, record)
                raise
        else:
            partial = response.get('partialSuccess') or response.get('partial_success') or {}
            rejected = int(partial.get('rejectedSpans', partial.get('rejected_spans', 0)))
            record['accepted'] = rejected == 0
            record['terminal_rejection'] = rejected != 0
            write_private(path, record)
            require(not rejected, 'OTLP 합성 span 일부가 거절됐습니다. 성공으로 처리하지 않습니다.')
    if not passed:
        passed = poll(client, record, wait_seconds)
    return {'stage': 'O01-Langfuse-trace', 'result': 'passed' if passed else 'pending', 'mode': action,
            'scope': 'synthetic OTLP JSON / Langfuse v4 observations API', 'version': manage.VERSION,
            'trace_id': record['trace_id'], 'project_id': record['project_id'],
            'observations_verified': 3 if passed else 0, 'parent_child_status_time_verified': passed,
            'metadata_only': True, 'post_submitted': sent, 'post_attempts': record['post_attempts'],
            'original_payload_sha256': record['payload_sha256'], 'record_file': str(path),
            'intentional_error_span': 'synthetic-error', 'agent_instrumented': False,
            'inference_executed': False, 'restart_preservation_assessed': False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['run', 'check'])
    p.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    p.add_argument('--wait-seconds', type=int, default=120)
    a = p.parse_args(argv)
    require(0 <= a.wait_seconds <= 600, '--wait-seconds는 0~600입니다.')
    docker = Docker()
    host = manage.host_info(docker)
    config = manage.load(a.state_dir, host)
    manage.check(docker, config, wait_seconds=0)
    if a.action == 'run':
        with operation_lock():
            result = execute(a.action, a.state_dir, config, Client(config), a.wait_seconds)
    else:
        result = execute(a.action, a.state_dir, config, Client(config), a.wait_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('합성 추적 원본을 보존했습니다. check로 조회하거나 같은 run으로 재개하세요.', file=sys.stderr)
        sys.exit(130)
