#!/usr/bin/env python3
"""Opt-in passive GPU collection. Never deploys models or changes GPU settings."""
import argparse
import csv
import io
import json
import math
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import manage as op
import restart_check as retained

PROJECT = 'infrastructure-observability-gpu'
TAG = 'nvcr.io/nvidia/k8s/dcgm-exporter:4.6.1-4.8.4-distroless'
IMAGE = 'nvcr.io/nvidia/k8s/dcgm-exporter@sha256:148b0c025e5f2850256816fa33754fbf4733b7a086697a95613fc0ba3db5b003'
PORT = 16313
FIELDS = ('GPU_UTIL', 'FB_TOTAL', 'FB_USED', 'FB_FREE', 'GPU_TEMP', 'POWER_USAGE')
METRICS = tuple('DCGM_FI_DEV_' + name for name in FIELDS)


def inventory(run=subprocess.run):
    proc = run(['nvidia-smi', '--query-gpu=uuid,name,driver_version,memory.total',
                '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=15)
    op.require(proc.returncode == 0, 'nvidia-smi 조회 실패. GPU/드라이버 접근을 확인하세요. 설정은 변경하지 않습니다.')
    result = {}
    for row in csv.reader(io.StringIO(proc.stdout)):
        op.require(len(row) == 4, 'nvidia-smi CSV 형식이 예상과 다릅니다.')
        key, name, driver, total = (value.strip() for value in row)
        op.require(key.startswith('GPU-') and str(uuid.UUID(key[4:])) == key[4:] and key not in result,
                   'GPU UUID가 없거나 중복됐습니다. MIG/프로세스별 관측은 이번 범위가 아닙니다.')
        memory = float(total)
        op.require(name and re.fullmatch(r'\d+\.\d+(?:\.\d+)?', driver)
                   and math.isfinite(memory) and 0 < memory < 1024 * 1024, 'GPU 기본 정보가 유효하지 않습니다.')
        result[key] = {'name': name, 'driver': driver, 'memory_total_mib': memory}
    op.require(result, '조회 가능한 NVIDIA GPU가 없습니다.')
    return result


def parse_metrics(body):
    rows = {name: [] for name in METRICS}
    for line in body.splitlines():
        if not line or line.startswith('#'):
            continue
        name = re.split(r'[{\s]', line, 1)[0]
        if name not in rows:
            continue
        match = re.fullmatch(r'([A-Z0-9_]+)\{(.*)\}\s+(\S+)(?:\s+\d+)?', line)
        op.require(match is not None, 'GPU 지표 텍스트 형식이 올바르지 않습니다: ' + name)
        labels, rest = {}, match[2]
        while rest:
            part = re.match(r'([a-zA-Z_][a-zA-Z_0-9]*)=("(?:[^"\\]|\\[\\"n])*")(?:,|$)', rest)
            op.require(part is not None and part[1] not in labels, 'GPU 지표 라벨 형식이 올바르지 않습니다.')
            labels[part[1]] = json.loads(part[2])
            rest = rest[part.end():]
        rows[name].append((labels, float(match[3])))
    return rows


def validate_metrics(rows, devices):
    report = {key: {} for key in devices}
    issues = []
    for field, metric in zip(FIELDS, METRICS):
        seen = set()
        for labels, value in rows.get(metric, []):
            key = labels.get('UUID')
            op.require(key in devices and key not in seen, 'GPU UUID 누락·중복·불일치: ' + metric)
            seen.add(key)
            maximum = {'GPU_UTIL': 100, 'GPU_TEMP': 150, 'POWER_USAGE': 2000}.get(field, devices[key]['memory_total_mib'] + 256)
            minimum = 1 if field == 'FB_TOTAL' else 0
            if not math.isfinite(value) or not minimum <= value <= maximum:
                issues.append(metric + ':' + key + ':invalid_or_unsupported')
                continue
            report[key][field] = value
        issues.extend(metric + ':' + key + ':missing_or_unsupported' for key in sorted(set(devices) - seen))
    op.require(not issues, 'GPU 지표 확인 미통과: ' + ', '.join(issues)
               + '. N/A·오류 sentinel을 0으로 바꾸지 않습니다. 지원 범위/권한 확인이 필요합니다.')
    for key, values in report.items():
        op.require(abs(values['FB_TOTAL'] - devices[key]['memory_total_mib']) <= 256
                   and values['FB_USED'] <= values['FB_TOTAL'] and values['FB_FREE'] <= values['FB_TOTAL']
                   and values['FB_USED'] + values['FB_FREE'] <= values['FB_TOTAL'] + 16,
                   'GPU VRAM 용량이 호스트/수집기와 일치하지 않습니다: ' + key)
    return report


def direct_metrics(devices):
    with op.Prometheus().opener.open('http://127.0.0.1:%d/metrics' % PORT, timeout=8) as response:
        raw = response.read(1024 * 1024 + 1)
    op.require(len(raw) <= 1024 * 1024, 'GPU 지표 응답 크기 한도 초과')
    return validate_metrics(parse_metrics(raw.decode()), devices)


def wait_for(fn, seconds):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return fn()
        except (OSError, ValueError, KeyError) as exc:
            if time.monotonic() >= deadline:
                raise ValueError(str(exc) + ' 상태/기준 파일을 보존하고 같은 up 또는 check로 이어가세요.') from exc
            time.sleep(min(3, max(0, deadline - time.monotonic())))


def compose(docker, root, args, **kwargs):
    return docker.run(['compose', '--project-name', PROJECT, '--env-file', str(root / 'gpu/.env'),
                       '-f', str(root / 'gpu/docker-compose.yml'), *args], **kwargs)


def gpu_containers(docker, state):
    ids = docker.run(['ps', '-aq', '--filter', 'label=com.docker.compose.project=' + PROJECT]).split()
    items = docker.json(['inspect', *ids]) if ids else []
    op.require(len(items) <= 1, 'GPU 관측 컨테이너가 중복됐습니다.')
    for item in items:
        labels = item['Config'].get('Labels', {})
        op.require(state and labels.get(op.OWNER) == state['config_id']
                   and labels.get('com.docker.compose.service') == 'obs-gpu', '기존 GPU 수집기의 소유권을 확인할 수 없습니다.')
        op.require(item['Image'] == state['image']['image_id'] and item['Config']['Image'] == state['image']['digest'],
                   'GPU 수집기 이미지가 고정값과 다릅니다.')
    return items


def environment(state):
    return "OBS_GPU_CONFIG_ID='" + state['config_id'] + "'\nOBS_GPU_IMAGE='" + state['image']['digest'] + "'\n"


def validate_state(state, base, devices):
    op.require(state['schema'] == 1 and state['config_id'] == base['config_id'] and state['devices'] == devices,
               'GPU 관측 소유권·UUID·드라이버·용량이 변경됐습니다. 자동 갱신하지 않습니다.')
    image = state['image']
    op.require(image['tag'] == TAG and image['digest'] == IMAGE
               and re.fullmatch(r'sha256:[0-9a-f]{64}', image['image_id']), 'GPU 이미지 고정값이 유효하지 않습니다.')


def config_check(docker, root, state):
    op.require(op.read_private(root / 'gpu/.state.json') == state and
               op.no_symlinks(root / 'gpu/.env').read_text() == environment(state), 'GPU 상태/환경 파일 불일치')
    config = json.loads(compose(docker, root, ['config', '--format', 'json']))
    op.require(set(config['services']) == {'obs-gpu'}, 'GPU 구성에는 수집기 1개만 허용합니다.')
    service = config['services']['obs-gpu']
    devices = service.get('deploy', {}).get('resources', {}).get('reservations', {}).get('devices')
    flags = service.get('command', [])
    op.require(service['image'] == state['image']['digest'] and service.get('network_mode') == 'host'
               and service.get('labels', {}).get(op.OWNER) == state['config_id']
               and not service.get('ports') and not service.get('privileged') and not service.get('cap_add')
               and service.get('cap_drop') == ['ALL'] and service.get('read_only') is True
               and service.get('security_opt') == ['no-new-privileges:true']
               and service.get('entrypoint') == ['/usr/bin/dcgm-exporter']
               and service.get('environment', {}).get('NVIDIA_DRIVER_CAPABILITIES') == 'utility'
               and devices == [{'capabilities': ['gpu'], 'count': -1, 'driver': 'nvidia'}]
               and flags == ['--address=127.0.0.1:16313', '--collectors=/etc/dcgm-exporter/basic.csv',
                             '--collect-interval=5000', '--devices=g', '--no-hostname', '--kubernetes=false']
               and service.get('tmpfs') == ['/tmp:size=16777216,mode=1777'], 'GPU 수집기 권한·주소·장치·명령 설정이 변경됐습니다.')
    mounts = service.get('volumes', [])
    op.require(len(mounts) == 1 and mounts[0]['source'] == str(root / 'gpu/basic.csv')
               and mounts[0]['target'] == '/etc/dcgm-exporter/basic.csv' and mounts[0].get('read_only'),
               'GPU 수집기는 지정 CSV만 읽기 전용으로 연결합니다.')
    configured = []
    for row in csv.reader((root / 'gpu/basic.csv').read_text().splitlines()):
        if row and not row[0].startswith('#'):
            op.require(len(row) == 3 and row[1].strip() == 'gauge', 'GPU CSV 설정이 유효하지 않습니다.')
            configured.append(row[0].strip())
    op.require(configured == list(METRICS), '기본 6개 GPU 필드만 허용합니다. profiling/진단은 실행하지 않습니다.')


def prepare(docker, root, runtime, devices):
    path = root / 'gpu/.state.json'
    if path.exists() or path.is_symlink():
        state = op.read_private(path)
        validate_state(state, runtime.state, devices)
    else:
        gpu_containers(docker, None)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', PORT))
        op.check_disks(runtime.state['data_dir'], runtime.host['docker_root'])
        docker.run(['pull', IMAGE], visible=True, timeout=900)
        image = docker.json(['image', 'inspect', IMAGE])[0]
        pins = [d for d in image.get('RepoDigests', []) if d == IMAGE]
        op.require(len(pins) == 1, '공식 GPU 이미지 digest를 확인할 수 없습니다.')
        state = {'schema': 1, 'config_id': runtime.state['config_id'], 'enabled': False, 'devices': devices,
                 'image': {'tag': TAG, 'digest': pins[0], 'image_id': image['Id']}}
        retained.write_record(path, state)
    validate_state(state, runtime.state, devices)
    gpu_containers(docker, state)
    if not docker.run(['image', 'ls', '-q', state['image']['digest']]).strip():
        docker.run(['pull', state['image']['digest']], visible=True, timeout=900)
    op.write_private(root / 'gpu/.env', environment(state))
    config_check(docker, root, state)
    return state


def storage_binding(runtime):
    binding = runtime.binding()
    return {k: v for k, v in binding.items() if k != 'files_sha256'} | {
        'original_state_sha256': retained.file_hash(runtime.root / '.state.json'),
        'original_env_sha256': retained.file_hash(runtime.root / '.env'),
        'configuration_sha256': {name: retained.file_hash(runtime.root / name) for name in (
            'docker-compose.yml', 'prometheus/prometheus.yml', 'gpu/docker-compose.yml',
            'gpu/basic.csv', 'gpu/prometheus.yml', 'gpu/prometheus-overlay.yml')}}


def transition(runtime, path):
    if path.exists() or path.is_symlink():
        record = op.read_private(path)
        op.require(record['schema'] == 1 and record['sha256'] == retained.digest(record['before']), 'GPU 전환 기준 파일이 변경됐습니다.')
        return record
    containers = runtime.containers()
    retained.healthy(containers)
    names = runtime.expected_names()
    runtime.metrics(names)
    end = '%.3f' % (time.time() - 15)
    before = {'binding': storage_binding(runtime), 'containers': containers,
              'expected_names': names, 'query_time': end, 'history': retained.history(runtime.prom, end)}
    op.require(containers == runtime.containers(), '기준 수집 중 관측 서비스가 변경됐습니다.')
    record = {'schema': 1, 'before': before, 'sha256': retained.digest(before), 'prometheus_change_requested': False}
    retained.write_record(path, record)
    return record


def config_source(docker, runtime):
    items = op.project_containers(docker, runtime.state['config_id'])
    item = next(x for x in items if x['Config']['Labels']['com.docker.compose.service'] == 'obs-prometheus')
    sources = [m['Source'] for m in item['Mounts'] if m['Destination'] == '/etc/prometheus/prometheus.yml']
    op.require(len(sources) == 1, '실제 Prometheus 설정 mount가 올바르지 않습니다.')
    return sources[0]


def assert_transition(runtime, record):
    op.require(storage_binding(runtime) == record['before']['binding'], '기존 TSDB·이미지 고정 상태·소유권이 변경됐습니다.')
    current = runtime.containers()
    for service in ('obs-node-exporter', 'obs-cadvisor'):
        op.require(current[service] == record['before']['containers'][service], '기존 CPU/컨테이너 수집기가 재시작/변경됐습니다.')
    return current


def attach(docker, root, runtime, state, record, path):
    current = assert_transition(runtime, record)
    wanted = str(root / 'gpu/prometheus.yml')
    source = config_source(docker, runtime)
    op.require(source in (wanted, str(root / 'prometheus/prometheus.yml')), '알 수 없는 Prometheus 설정을 자동 교체하지 않습니다.')
    if source != wanted:
        op.require(current['obs-prometheus'] == record['before']['containers']['obs-prometheus'],
                   '연결 전 Prometheus가 외부에서 변경됐습니다. 기존 기준을 보존하세요.')
        op.require(not record['prometheus_change_requested'],
                   '이전 Prometheus 교체 요청의 완료가 불확실합니다. status와 로그를 확인하세요. 자동 반복하지 않습니다.')
        docker.run(['run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--entrypoint', '/bin/promtool',
                    '--mount', 'type=bind,src=' + wanted + ',dst=/etc/prometheus/prometheus.yml,readonly',
                    runtime.state['images']['obs-prometheus']['digest'], 'check', 'config', '/etc/prometheus/prometheus.yml'], visible=True)
    state['enabled'] = True
    retained.write_record(root / 'gpu/.state.json', state)
    op.config_check(docker, root, runtime.state)
    if source != wanted:
        record['prometheus_change_requested'] = True
        retained.write_record(path, record)
        print('GPU 지표 확인 완료. 같은 TSDB·이미지로 Prometheus 설정만 교체합니다.', flush=True)
        try:
            op.compose(docker, root, ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate',
                                     '--wait', '--wait-timeout', '180', 'obs-prometheus'], visible=True, timeout=240)
        except (ValueError, OSError, subprocess.SubprocessError):
            print('Prometheus 교체 응답을 확정하지 못했습니다. 실제 연결과 저장 데이터를 조회합니다.', flush=True)


def verify(docker, root, runtime, state, record):
    validate_state(state, runtime.state, inventory())
    config_check(docker, root, state)
    items = gpu_containers(docker, state)
    op.require(len(items) == 1 and items[0]['State']['Running'], 'GPU 수집기가 실행 중이어야 합니다.')
    op.require(op.gpu_enabled(root) and config_source(docker, runtime) == str(root / 'gpu/prometheus.yml'), 'GPU 수집 설정이 아직 연결되지 않았습니다.')
    current = assert_transition(runtime, record)
    retained.healthy(current)
    metrics = runtime.metrics(record['before']['expected_names'])
    targets = runtime.prom.get('/api/v1/targets')['activeTargets']
    gpu = [t for t in targets if t['labels']['job'] == 'gpu']
    op.require(len(gpu) == 1 and gpu[0]['scrapeUrl'] == 'http://127.0.0.1:16313/metrics'
               and gpu[0]['health'] == 'up' and 0 <= time.time() - op.scrape_timestamp(gpu[0]['lastScrape']) < 90,
               'GPU Prometheus scrape가 아직 정상·최신 상태가 아닙니다.')
    values = {metric: op.values(runtime.prom.query(metric + '{job="gpu"}')) for metric in METRICS}
    report = validate_metrics(values, state['devices'])
    hist = retained.history(runtime.prom, record['before']['query_time'])
    op.require(hist == record['before']['history'], 'GPU 연결 전 고정 과거 시계열이 일치하지 않습니다.')
    return {'stage': 'O01-gpu', 'result': 'passed', 'scope': 'passive whole-GPU metrics',
            'gpu_count': len(report), 'gpus': report, 'targets_up': len(targets),
            'platform_containers_verified': metrics['platform_containers_verified'],
            'historical_samples_preserved': True, 'historical_counts': retained.counts(hist),
            'original_tsdb_and_image_pins_preserved': True, 'node_exporter_and_cadvisor_unchanged': True,
            'gpu_image': state['image']['digest'], 'inference_executed': False,
            'gpu_configuration_changed': False, 'profiling_or_stress_test_executed': False,
            'domain_services_restarted': False, 'per_model_usage_attribution': 'not_assessed', 'llm_traces_assessed': False}


def execute(action, docker, root, wait_seconds):
    runtime = retained.Runtime(docker, root)
    devices = inventory()
    if action == 'probe':
        return {'stage': 'O01-gpu', 'result': 'read', 'devices': devices, 'dcgm_fields_verified': False,
                'changes_made': False, 'inference_executed': False}
    path = Path(runtime.state['data_dir']).parent / 'gpu-transition-o01.json'
    if action == 'up':
        state = prepare(docker, root, runtime, devices)
        items = gpu_containers(docker, state)
        if not items or not items[0]['State']['Running']:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', PORT))
            compose(docker, root, ['up', '-d', '--no-build', '--pull', 'never', 'obs-gpu'], visible=True)
        wait_for(lambda: direct_metrics(devices), wait_seconds)
        record = wait_for(lambda: transition(runtime, path), wait_seconds)
        attach(docker, root, runtime, state, record, path)
    else:
        state = op.read_private(root / 'gpu/.state.json')
        record = transition_read(path)
    result = wait_for(lambda: verify(docker, root, runtime, state, record), wait_seconds)
    result['baseline_file'] = str(path)
    return result


def transition_read(path):
    record = op.read_private(path)
    op.require(record['schema'] == 1 and record['sha256'] == retained.digest(record['before']), 'GPU 전환 기준 파일이 변경됐습니다.')
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('probe', 'up', 'check'))
    parser.add_argument('--wait-seconds', type=int, default=180)
    args = parser.parse_args(argv)
    op.require(0 <= args.wait_seconds <= 300, '--wait-seconds 범위는 0~300입니다.')
    with op.lock(op.ROOT) if args.action == 'up' else op.contextlib.nullcontext():
        result = execute(args.action, op.Docker(), op.ROOT, args.wait_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        sys.exit(1)
