#!/usr/bin/env python3
"""Capture fixed historical samples, restart only owned collectors, verify preservation."""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import manage as op

QUERIES = (
    'up{job=~"prometheus|host|containers"}[3m]',
    'node_cpu_seconds_total{job="host",mode="idle"}[3m]',
    'node_memory_MemTotal_bytes{job="host"}[3m]',
    'node_filesystem_avail_bytes{job="host",fstype!="tmpfs"}[3m]',
    'container_cpu_usage_seconds_total{job="containers",name!=""}[3m]',
    'container_memory_working_set_bytes{job="containers",name!=""}[3m]',
)
MAX_BASELINE_BYTES = 4 * 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(op.no_symlinks(path).read_bytes()).hexdigest()


def write_record(path, record):
    body = encoded(record) + '\n'
    op.require(len(body.encode()) <= MAX_BASELINE_BYTES, '재시작 기준 파일의 4 MiB 한도를 초과했습니다.')
    op.write_private(path, body)
    # The restart intent must survive a host crash after the rename, too.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_record(path):
    op.require(op.no_symlinks(path).stat().st_size <= MAX_BASELINE_BYTES, '기준 파일이 너무 큽니다.')
    record = op.read_private(path)
    core = record['baseline']
    op.require(core['schema'] == 1 and core['kind'] == 'O01-restart'
               and core['queries'] == list(QUERIES) and record['baseline_sha256'] == digest(core),
               '재시작 기준 파일이 변경됐거나 지원하지 않는 형식입니다. 원본을 보존하세요.')
    return record


def matrix(prom, query, end):
    data = prom.get('/api/v1/query?' + urllib.parse.urlencode({'query': query, 'time': end}))
    op.require(data.get('resultType') == 'matrix' and data.get('result'), '과거 시계열이 없습니다: ' + query)
    rows, keys = [], set()
    for item in data['result']:
        labels = item['metric']
        op.require(isinstance(labels, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()),
                   '시계열 라벨 형식이 올바르지 않습니다.')
        key = encoded(labels)
        op.require(key not in keys, '중복 시계열 라벨입니다.')
        keys.add(key)
        samples = item.get('values')
        op.require(samples and not item.get('histograms'), '비교할 숫자 시계열이 없습니다.')
        previous = float('-inf')
        for timestamp, value in samples:
            stamp = float(timestamp)
            op.require(math.isfinite(stamp) and math.isfinite(float(value))
                       and float(end) - 180 < stamp <= float(end) and stamp > previous,
                       '시계열 시각·값 또는 고정 조회 구간이 올바르지 않습니다.')
            previous = stamp
        rows.append({'metric': labels, 'values': samples})
    return sorted(rows, key=lambda row: encoded(row['metric']))


def history(prom, end):
    return {query: matrix(prom, query, end) for query in QUERIES}


def process_start(prom):
    # Query the serving process directly; process collector metrics can be absent.
    value = op.scrape_timestamp(prom.get('/api/v1/status/runtimeinfo')['startTime'])
    op.require(0 < value <= time.time() + 1, 'Prometheus 프로세스 시작 시각을 확인할 수 없습니다.')
    return value


def counts(hist):
    return {'series': sum(len(rows) for rows in hist.values()),
            'samples': sum(len(row['values']) for rows in hist.values() for row in rows)}


class Runtime:
    def __init__(self, docker, root=op.ROOT, prom=None):
        self.docker, self.root = docker, root
        self.prom = prom or op.Prometheus()
        self.host = op.host_info(docker)
        self.state = op.read_private(root / '.state.json')
        op.validate_state(self.state, self.host)
        op.config_check(docker, root, self.state)
        self.default_baseline = Path(self.state['data_dir']).parent / 'restart-acceptance-o01.json'

    def binding(self):
        op.validate_state(self.state, self.host)
        data = Path(self.state['data_dir'])
        info = data.stat()
        files = ('.state.json', '.env', 'docker-compose.yml', 'prometheus/docker-compose.yml',
                 'prometheus/prometheus.yml', 'node-exporter/docker-compose.yml', 'cadvisor/docker-compose.yml')
        if op.gpu_enabled(self.root):
            files += ('gpu/.state.json', 'gpu/prometheus-overlay.yml', 'gpu/prometheus.yml')
        return {'config_id': self.state['config_id'], 'hostname': self.host['hostname'],
                'docker_root': self.host['docker_root'], 'data_dir': str(data),
                'data_device': info.st_dev, 'data_inode': info.st_ino,
                'owner_sha256': file_hash(data.parent / '.owner.json'),
                'files_sha256': {name: file_hash(self.root / name) for name in files}}

    def containers(self):
        items = op.project_containers(self.docker, self.state['config_id'])
        result = {}
        for item in items:
            service = item['Config']['Labels']['com.docker.compose.service']
            op.require(service not in result, '중복 관측 서비스 컨테이너가 있습니다.')
            op.require(re.fullmatch(r'[0-9a-f]{64}', item['Id']), 'Docker 컨테이너 ID 형식이 올바르지 않습니다.')
            pin = self.state['images'][service]
            op.require(item['Image'] == pin['image_id'] and item['Config']['Image'] == pin['digest'],
                       '실행 중인 관측 이미지가 고정 이미지와 다릅니다: ' + service)
            if service == 'obs-prometheus':
                mounts = [m for m in item['Mounts'] if m['Destination'] == '/prometheus']
                op.require(len(mounts) == 1 and mounts[0]['Type'] == 'bind'
                           and mounts[0]['Source'] == self.state['data_dir'] and mounts[0]['RW'],
                           '실제 Prometheus 컨테이너의 TSDB 연결이 일치하지 않습니다.')
            result[service] = {'id': item['Id'], 'image_id': item['Image'],
                               'started_at': item['State']['StartedAt'], 'running': item['State']['Running'],
                               'health': item['State'].get('Health', {}).get('Status', 'unknown')}
        op.require(set(result) == set(op.SERVICES), '기존 관측 서비스 3개가 모두 있어야 합니다. 자동 생성하지 않습니다.')
        return result

    def expected_names(self):
        return sorted(op.required_containers(self.docker))

    def metrics(self, names):
        return op.inspect_metrics(self.prom, self.host, set(names), self.state['data_dir'])

    def restart(self, containers):
        # Immutable, ownership-checked IDs; no Compose up/recreate, pull or domain service commands.
        ids = [containers[name]['id'] for name in op.SERVICES]
        self.docker.run(['restart', '--time', '30', *ids], timeout=180)


def healthy(containers):
    op.require(all(c['running'] and c['health'] == 'healthy' for c in containers.values()),
               '관측 서비스 3개가 아직 모두 Healthy가 아닙니다.')


def assert_identity(before, current):
    op.require(set(before) == set(current), '관측 서비스 구성이 바뀌었습니다.')
    for name in before:
        op.require(all(before[name][key] == current[name][key] for key in ('id', 'image_id')),
                   '기존 관측 컨테이너/이미지가 교체됐습니다: ' + name)


def assert_bound(runtime, record):
    op.require(record['baseline']['binding'] == runtime.binding(),
               '원래 설정·이미지 고정값·TSDB 경로/소유권이 변경됐습니다. 기준을 덮어쓰지 않습니다.')


def capture(runtime, path):
    if path.exists() or path.is_symlink():
        record = read_record(path)
        assert_bound(runtime, record)
        return record, True
    before = runtime.containers()
    healthy(before)
    names = runtime.expected_names()
    runtime.metrics(names)
    binding = runtime.binding()
    end = '%.3f' % (time.time() - 15)
    hist = history(runtime.prom, end)
    up = hist[QUERIES[0]]
    op.require(len(up) == 3 and {r['metric'].get('job') for r in up} == {'prometheus', 'host', 'containers'}
               and all(len(r['values']) >= 2 and float(r['values'][-1][1]) == 1
                       and float(end) - float(r['values'][-1][0]) < 90 for r in up),
               '재시작 전 정상 수집 기록이 부족합니다. 1분 후 같은 명령으로 다시 확인하세요.')
    start = process_start(runtime.prom)
    after = runtime.containers()
    op.require(before == after and binding == runtime.binding(), '기준 수집 중 관측 서비스/설정이 바뀌었습니다.')
    core = {'schema': 1, 'kind': 'O01-restart', 'captured_at': time.time(), 'query_time': end,
            'queries': list(QUERIES), 'history': hist, 'binding': binding,
            'containers': before, 'expected_names': names, 'process_start_before': start}
    record = {'baseline': core, 'baseline_sha256': digest(core), 'restart': None}
    write_record(path, record)
    return record, False


def verify(runtime, record, wait_seconds):
    op.require(record.get('restart'), '재시작 요청 기록이 없습니다. run으로 한 번만 재시작할 수 있습니다.')
    core = record['baseline']
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            assert_bound(runtime, record)
            current = runtime.containers()
            assert_identity(core['containers'], current)
            healthy(current)
            started = {name: op.scrape_timestamp(c['started_at']) for name, c in current.items()}
            for name in op.SERVICES:
                op.require(started[name] > op.scrape_timestamp(core['containers'][name]['started_at'])
                           and started[name] >= record['restart']['requested_at'] - 1,
                           '재시작 완료를 아직 확인하지 못했습니다: ' + name)
            metrics = runtime.metrics(core['expected_names'])
            targets = runtime.prom.get('/api/v1/targets')['activeTargets']
            op.require(all(op.scrape_timestamp(t['lastScrape']) > max(started.values()) for t in targets),
                       '세 서비스 재시작 후 새 scrape를 기다립니다.')
            op.require(process_start(runtime.prom) > core['process_start_before'],
                       '새 Prometheus 프로세스의 수집을 아직 확인하지 못했습니다.')
            preserved = history(runtime.prom, core['query_time'])
            op.require(preserved == core['history'], '고정된 과거 시계열이 일치하지 않습니다. 원래 기준 파일을 보존하세요.')
            op.require(current == runtime.containers(), '검증 도중 관측 서비스 상태가 바뀌었습니다.')
            assert_bound(runtime, record)
            return {'stage': 'O01-restart', 'result': 'passed',
                    'scope': 'three observation containers; pinned historical metric samples',
                    'observation_containers_restarted': 3, 'same_container_ids': True,
                    'same_images_and_storage_binding': True, 'historical_samples_preserved': True,
                    'new_scrapes_after_restart': True, 'historical_counts': counts(preserved),
                    'baseline_sha256': record['baseline_sha256'], 'query_time': core['query_time'],
                    'targets_up': metrics['targets_up'], 'platform_containers_verified': metrics['platform_containers_verified'],
                    'inference_executed': False, 'domain_services_restarted_by_this_tool': False,
                    'whole_tsdb_backup_verified': False, 'gpu_metrics_assessed': False, 'llm_traces_assessed': False}
        except (ValueError, OSError, KeyError) as exc:
            if time.monotonic() >= deadline:
                raise ValueError('재시작 인수 미완료: ' + str(exc)
                                 + ' 기준 파일을 유지하고 compare로 조회하세요. 자동 재시작을 반복하지 않습니다.') from exc
            time.sleep(min(3, max(0, deadline - time.monotonic())))


def run_action(runtime, action, path, wait_seconds=180):
    path = op.no_symlinks(path)
    op.require(path.parent.is_dir(), '기준 파일의 상위 디렉터리가 없습니다.')
    op.require(not path.is_relative_to(Path(runtime.state['data_dir'])), '기준 파일을 TSDB 내부에 저장하지 않습니다.')
    if action == 'compare':
        result = verify(runtime, read_record(path), wait_seconds)
    else:
        record, reused = capture(runtime, path)
        if action == 'capture':
            return {'stage': 'O01-restart', 'result': 'captured', 'reused': reused,
                    'baseline_file': str(path), 'baseline_sha256': record['baseline_sha256'],
                    'historical_counts': counts(record['baseline']['history']), 'restart_executed': False}
        if not record.get('restart'):
            assert_bound(runtime, record)
            current = runtime.containers()
            healthy(current)
            op.require(current == record['baseline']['containers'], '기준 저장 이후 컨테이너가 이미 재시작/변경됐습니다.')
            op.require(history(runtime.prom, record['baseline']['query_time']) == record['baseline']['history'],
                       '재시작 전 과거 시계열이 이미 달라졌습니다. 저장한 기준을 보존하세요.')
            # Persist intent BEFORE mutation. A lost response must never submit a second restart.
            record['restart'] = {'requested_at': time.time(), 'command_returned': False}
            write_record(path, record)
            print('관측 서비스 3개만 재시작합니다. 원래 시계열 기준과 데이터 경로를 유지합니다.', flush=True)
            try:
                runtime.restart(current)
            except (ValueError, OSError, subprocess.SubprocessError):
                print('재시작 명령 응답을 확정하지 못했습니다. 저장한 기준으로 실제 상태를 조회합니다.', flush=True)
            else:
                record['restart']['command_returned'] = True
                write_record(path, record)
        result = verify(runtime, record, wait_seconds)
    result['baseline_file'] = str(path)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'capture', 'compare'))
    parser.add_argument('--baseline-file', type=Path)
    parser.add_argument('--wait-seconds', type=int, default=180)
    args = parser.parse_args(argv)
    op.require(0 <= args.wait_seconds <= 300, '--wait-seconds 범위는 0~300입니다.')
    context = op.lock(op.ROOT) if args.action != 'compare' else op.contextlib.nullcontext()
    with context:
        runtime = Runtime(op.Docker())
        result = run_action(runtime, args.action, args.baseline_file or runtime.default_baseline, args.wait_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        sys.exit(1)
