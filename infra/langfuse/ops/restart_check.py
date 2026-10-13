#!/usr/bin/env python3
"""One recorded restart of six owned Langfuse containers; compare the existing trace."""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import manage
import trace_check as trace
from lf_common import (DEFAULT_STATE, ROOT, SERVICES, Docker, Client, DockerFailure, HttpFailure,
                       require, read_json, private_read, write_private, fingerprint, operation_lock)

BASELINE_NAME = 'restart-acceptance-o01.json'
MAX_BASELINE_BYTES = 128 * 1024


def file_hash(path, private=True):
    if private:
        private_read(path)  # Require a private regular file owned by the invoking user.
    return hashlib.sha256(path.read_bytes()).hexdigest()


def selected_content(rows, record):
    if not trace.verify(rows, record):
        return None
    # API-derived costs, latency and update timestamps may change asynchronously.
    # Compare source fields; hash all metadata without copying any unknown body.
    result = []
    for row in rows:
        item = {key: row[key] for key in ('id', 'traceId', 'projectId', 'name', 'type', 'level', 'environment')}
        item.update(parent_id=row.get('parentObservationId') or None, status_message=row.get('statusMessage'),
                    start_ms=trace.millis(row['startTime']), end_ms=trace.millis(row['endTime']),
                    metadata_sha256=fingerprint(row.get('metadata', {})), input_empty=True, output_empty=True)
        result.append(item)
    return sorted(result, key=lambda item: item['id'])


class Runtime:
    def __init__(self, docker, settings, client=None):
        self.docker, self.settings = docker, settings
        self.root = Path(settings['state_dir'])
        self.client = client or Client(settings)
        self.baseline_path = self.root / BASELINE_NAME
        self.trace_path = self.root / 'trace-check.json'

    def binding(self):
        s = manage.load(self.root)
        require(s == self.settings, '작업 중 Langfuse 설정이 변경됐습니다.')
        files = ('settings.json', 'owner.json', 'trace-check.json', *manage.environments(s))
        return {'owner': s['owner'], 'host': s['host'], 'state_dir': s['state_dir'],
                'project_id': s['project_id'], 'storage': s['storage'], 'images': s['images'],
                'private_file_hashes': {name: file_hash(self.root / name) for name in files},
                'source_config_hashes': {name: file_hash(ROOT / name, private=False)
                                         for name in ('docker-compose.yml', 'images.lock.json')}}

    def record(self):
        record = read_json(self.trace_path)
        trace.validate_record(record, self.settings)
        require(record['post_attempts'] >= 1 and not record['terminal_rejection'],
                '성공 조회한 기존 합성 추적 기록이 필요합니다. 새 추적을 만들지 않습니다.')
        return record

    def containers(self):
        manage.check_networks(self.docker, self.settings['owner'])
        items = manage.owned_containers(self.docker, self.settings['owner'])
        require(len(items) == len(SERVICES), '원래 Langfuse 컨테이너 6개가 필요합니다. 자동 생성하지 않습니다.')
        result = {}
        for item in items:
            manage.inspect_binding(item, self.settings)
            name = item['Config']['Labels']['com.docker.compose.service']
            require(re.fullmatch('[0-9a-f]{64}', item['Id'])
                    and re.fullmatch('sha256:[0-9a-f]{64}', item['Image']), 'Docker 컨테이너/이미지 ID 형식이 다릅니다.')
            started = item['State']['StartedAt']
            require(trace.millis(started) > 0, '기존 컨테이너 시작 시각이 없습니다.')
            result[name] = {'id': item['Id'], 'image_id': item['Image'],
                            'config_sha256': fingerprint(item['Config']),
                            'host_config_sha256': fingerprint(item['HostConfig']),
                            'mounts_sha256': fingerprint(item['Mounts']),
                            'mounts_order_sha256': mount_order_hashes(item['Mounts']),
                            'started_at': started, 'running': item['State']['Running'],
                            'health': item['State'].get('Health', {}).get('Status', 'unknown')}
        require(set(result) == set(SERVICES), 'Langfuse 서비스 구성이 다릅니다.')
        return result

    def ready(self):
        # Reuse the existing real-version and authenticated project/org check.
        return manage.check(self.docker, self.settings, 0, self.client)

    def content(self, record):
        return selected_content(trace.observations(self.client, record), record)

    def restart(self, containers):
        # Immutable owned IDs: no compose up/recreate, dependency traversal or new image.
        self.docker.run(['restart', '--timeout', '60', *[containers[key]['id'] for key in SERVICES]], timeout=420)


def healthy(containers):
    return all(item['running'] and item['health'] == 'healthy' for item in containers.values())


def mount_order_hashes(mounts):
    # This stack has at most two mounts (ClickHouse data + logs). Preserve every
    # field and value; enumerate only array order, never normalize the objects.
    require(isinstance(mounts, list) and len(mounts) <= 2
            and len({m['Destination'] for m in mounts}) == len(mounts),
            '마운트 개수/목적지 구성이 예상과 다릅니다.')
    return sorted({fingerprint(list(order)) for order in itertools.permutations(mounts)})


def identity_details(before, after):
    require(set(before) == set(after) == set(SERVICES), '재시작 대상 서비스 구성이 바뀌었습니다.')
    fields = ('id', 'image_id', 'config_sha256', 'host_config_sha256', 'mounts_sha256')
    result = {}
    for name in SERVICES:
        changed = [field for field in fields if before[name][field] != after[name][field]]
        order_only = ('mounts_sha256' in changed
                      and before[name]['mounts_sha256'] in after[name].get('mounts_order_sha256', []))
        result[name] = {'changed_fields': changed, 'mount_order_equivalence_verified': order_only,
                        'unresolved_fields': [field for field in changed if not (field == 'mounts_sha256' and order_only)]}
    return result


def identity(before, after):
    details = identity_details(before, after)
    errors = [name + ':' + ','.join(row['unresolved_fields']) for name, row in details.items() if row['unresolved_fields']]
    require(not errors, '컨테이너 보존 비교 불일치 (' + '; '.join(errors) + '). 원래 기준을 보존하고 diagnose로 확인하세요.')
    return details


def read_baseline(path):
    # Validate before reading any potentially large data. read_json also rejects symlinks.
    require(path.stat().st_size <= MAX_BASELINE_BYTES, '재시작 기준 파일이 너무 큽니다.')
    saved = read_json(path)
    b = saved['baseline']
    require(b['schema'] == 1 and b['kind'] == 'O01-Langfuse-restart'
            and b['version'] == manage.VERSION and saved['baseline_sha256'] == fingerprint(b),
            '재시작 기준 파일의 형식/hash가 다릅니다. 삭제하거나 덮어쓰지 마세요.')
    require(b['restart_intent'] is True and type(b['intent_ms']) is int and b['intent_ms'] > 0
            and len(b['content']) == 3 and fingerprint(b['content']) == b['content_sha256']
            and set(b['containers']) == set(SERVICES), '재시작 기준 내용이 올바르지 않습니다.')
    return saved


def compare(runtime, saved, wait_seconds, requested=False, restart_error=False):
    b = saved['baseline']
    deadline = time.monotonic() + wait_seconds
    while True:
        require(runtime.binding() == b['binding'], '설정/키/원래 합성 기록/저장 경로가 기준과 다릅니다.')
        record = runtime.record()
        require(record['trace_id'] == b['trace_id'] and record['payload_sha256'] == b['payload_sha256'],
                '원래 trace ID/payload hash가 바뀌었습니다.')
        current = runtime.containers()
        differences = identity(b['containers'], current)
        restarted = sum(trace.millis(current[key]['started_at']) > trace.millis(b['containers'][key]['started_at'])
                        and trace.millis(current[key]['started_at']) >= b['intent_ms'] - 1000 for key in SERVICES)
        ready = False
        content = None
        if healthy(current):
            try:
                runtime.ready()
                ready = True
                content = runtime.content(record)
            except HttpFailure:
                pass  # Bounded GET retry only; never another restart or trace submission.
            # manage.check wraps readiness failures as ValueError, preserving its safe message.
            except ValueError as exc:
                if str(exc).startswith('Langfuse 준비 검증 미통과:'):
                    pass
                else:
                    raise
        if content is not None:
            require(content == b['content'], '기존 span 내용이 바뀌었습니다. 새 추적으로 대체하지 않습니다.')
        passed = restarted == len(SERVICES) and ready and content is not None
        if passed:
            # Do not accept an identity/config change which raced the HTTP read.
            require(runtime.binding() == b['binding'], '검사 중 비공개 상태가 바뀌었습니다.')
            final = runtime.containers()
            identity(current, final)
            require(healthy(final) and all(final[k]['started_at'] == current[k]['started_at'] for k in SERVICES),
                    '검사 중 컨테이너가 다시 재시작됐습니다. 같은 compare로 재조회하세요.')
        if passed or time.monotonic() >= deadline:
            return {'stage': 'O01-Langfuse-restart', 'result': 'passed' if passed else 'pending',
                    'scope': 'six original Langfuse containers; pinned synthetic trace source fields',
                    'services_healthy': sum(x['running'] and x['health'] == 'healthy' for x in current.values()),
                    'services_restarted_since_baseline': restarted,
                    'same_container_ids_images_config_storage': True,
                    'mount_order_equivalence_verified': [key for key, value in differences.items()
                                                         if value['mount_order_equivalence_verified']],
                    'settings_secrets_original_trace_preserved': True,
                    'trace_id': b['trace_id'], 'project_id': record['project_id'],
                    'original_payload_sha256': b['payload_sha256'],
                    'observations_verified': len(content) if content is not None else 0,
                    'trace_content_preserved': content is not None, 'readiness_and_project_verified': ready,
                    'restart_requested_this_invocation': requested,
                    'restart_command_error_this_invocation': restart_error,
                    'baseline_sha256': saved['baseline_sha256'], 'baseline_file': str(runtime.baseline_path),
                    'post_submitted': False, 'new_trace_created': False, 'inference_executed': False,
                    'domain_services_restarted_by_this_tool': False, 'whole_storage_backup_verified': False,
                    'agent_instrumented': False, 'next_on_pending': 'same compare; preserve baseline and trace-check.json'}
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def diagnose(runtime):
    """Read the original hashes and current snapshot without accepting or rewriting."""
    saved = read_baseline(runtime.baseline_path)
    b = saved['baseline']
    binding_matches = runtime.binding() == b['binding']
    current = runtime.containers()
    details = identity_details(b['containers'], current)
    for name, row in details.items():
        row.update(running=current[name]['running'], health=current[name]['health'],
                   restart_observed=(trace.millis(current[name]['started_at']) > trace.millis(b['containers'][name]['started_at'])
                                     and trace.millis(current[name]['started_at']) >= b['intent_ms'] - 1000))
    ready = False
    trace_state = 'not_checked'
    count = 0
    if binding_matches and healthy(current):
        try:
            runtime.ready()
            ready = True
            record = runtime.record()
            if record['trace_id'] == b['trace_id'] and record['payload_sha256'] == b['payload_sha256']:
                content = runtime.content(record)
                count = len(content) if content is not None else 0
                trace_state = 'preserved' if content == b['content'] else 'not_visible' if content is None else 'changed'
            else:
                trace_state = 'record_changed'
        except (ValueError, KeyError, OSError):
            trace_state = 'unavailable_or_invalid'  # No API bodies, inspect values or credentials.
    return {'stage': 'O01-Langfuse-restart-diagnose', 'result': 'diagnosed',
            'scope': 'read-only differences; not restart acceptance',
            'baseline_sha256': saved['baseline_sha256'], 'binding_matches_baseline': binding_matches,
            'services': details, 'readiness_and_project_verified': ready,
            'trace': {'state': trace_state, 'observations_verified': count, 'trace_id': b['trace_id'],
                      'original_payload_sha256': b['payload_sha256']},
            'restart_acceptance': 'not_assessed', 'baseline_rewritten': False,
            'changes_made': False, 'restart_executed': False, 'post_submitted': False, 'inference_executed': False}


def execute(action, runtime, wait_seconds):
    if action == 'diagnose':
        return diagnose(runtime)
    path = runtime.baseline_path
    if path.exists() or path.is_symlink():
        result = compare(runtime, read_baseline(path), wait_seconds)
        result['mode'] = action
        return result
    require(action == 'run', '재시작 기준이 없습니다. 먼저 restart_check.py run을 실행하세요.')
    record = runtime.record()  # Never call trace.execute or create a new fixture.
    binding = runtime.binding()
    before = runtime.containers()
    require(healthy(before), 'Langfuse 6개 Healthy 상태에서 시작하세요. 재시작하지 않았습니다.')
    runtime.ready()
    content = runtime.content(record)
    require(content is not None, '기존 합성 trace의 3개 span이 모두 조회돼야 합니다. 재시작하지 않았습니다.')
    fresh = runtime.containers()
    identity(before, fresh)
    require(runtime.binding() == binding and all(before[key][field] == fresh[key][field]
            for key in SERVICES for field in ('started_at', 'running', 'health')),
            '기준 수집 중 상태가 바뀌었습니다. 재시작하지 않았습니다.')
    b = {'schema': 1, 'kind': 'O01-Langfuse-restart', 'version': manage.VERSION,
         'binding': binding, 'containers': before, 'trace_id': record['trace_id'],
         'payload_sha256': record['payload_sha256'], 'content': content, 'content_sha256': fingerprint(content),
         'restart_intent': True, 'intent_ms': int(time.time() * 1000)}
    saved = {'baseline': b, 'baseline_sha256': fingerprint(b)}
    require(len(json.dumps(saved).encode()) <= MAX_BASELINE_BYTES, '재시작 기준 크기 한도를 초과했습니다.')
    write_private(path, saved)  # Atomic file + parent fsync BEFORE any Docker mutation.
    print('원래 trace 기준을 저장했습니다. Langfuse 컨테이너 6개만 한 번 재시작합니다.', flush=True)
    error = False
    try:
        runtime.restart(before)
    except (DockerFailure, subprocess.TimeoutExpired):
        error = True
        print('재시작 명령의 완료를 확인하지 못했습니다. 다시 요청하지 않고 현재 상태를 조회합니다.', flush=True)
    result = compare(runtime, saved, wait_seconds, requested=True, restart_error=error)
    result['mode'] = action
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'compare', 'diagnose'])
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--wait-seconds', type=int, default=180)
    args = parser.parse_args(argv)
    require(0 <= args.wait_seconds <= 600, '--wait-seconds는 0~600입니다.')
    docker = Docker()
    host = manage.host_info(docker)
    root = args.state_dir.absolute()
    if args.action == 'run':
        with operation_lock():
            runtime = Runtime(docker, manage.load(root, host))
            result = execute(args.action, runtime, args.wait_seconds)
    else:
        runtime = Runtime(docker, manage.load(root, host))
        result = execute(args.action, runtime, args.wait_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] in ('passed', 'diagnosed') else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('기준/원래 trace를 보존했습니다. 같은 compare는 재시작 없이 조회만 합니다.', file=sys.stderr)
        sys.exit(130)
