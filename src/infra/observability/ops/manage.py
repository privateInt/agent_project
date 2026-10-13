#!/usr/bin/env python3
"""Opt-in, local Docker host resource monitoring. No domain API or inference calls."""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'infrastructure-observability'
OWNER = 'org.agent-project.observability.owner'
SERVICES = ('obs-prometheus', 'obs-node-exporter', 'obs-cadvisor')
PORTS = dict(zip(SERVICES, (16310, 16311, 16312)))
TAGS = {
    'obs-prometheus': 'quay.io/prometheus/prometheus:v3.13.3',
    'obs-node-exporter': 'quay.io/prometheus/node-exporter:v1.12.1',
    'obs-cadvisor': 'ghcr.io/google/cadvisor:v0.60.6',
}
DEFAULT_DATA = Path.home() / '.local/state/agent-project-observability/prometheus'
GIB = 1024 ** 3


def require(condition, message):
    if not condition:
        raise ValueError(message)


def no_symlinks(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        require(not part.is_symlink(), '심볼릭 링크 경로는 사용할 수 없습니다: ' + str(part))
    return path


def read_private(path):
    path = no_symlinks(path)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, '상태 파일 소유자 또는 권한을 확인하세요: ' + str(path))
    return json.loads(path.read_text())


def write_private(path, value):
    no_symlinks(path)
    fd, name = tempfile.mkstemp(prefix='.obs-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value if isinstance(value, str) else json.dumps(value, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Docker:
    def __init__(self):
        require(not os.environ.get('DOCKER_API_VERSION'), 'DOCKER_API_VERSION을 해제하고 실행하세요.')
        # Compose environment overrides must not change this stack's project, files or bindings.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('OBS_', 'COMPOSE_'))}

    def run(self, args, *, visible=False, timeout=120):
        result = subprocess.run(['docker', *args], env=self.env, text=True,
                                stdout=None if visible else subprocess.PIPE,
                                stderr=None if visible else subprocess.PIPE, timeout=timeout)
        require(result.returncode == 0, 'Docker 명령 실패: docker ' + ' '.join(args[:2])
                + ' (exit ' + str(result.returncode) + '). 기존 서비스는 변경하지 않습니다.')
        return result.stdout or ''

    def json(self, args):
        return json.loads(self.run(args))


def host_info(docker):
    require(sys.platform == 'linux' and os.getuid() != 0,
            'Linux 호스트의 일반 사용자로 실행하세요. sudo 실행은 지원하지 않습니다.')
    context = docker.run(['context', 'show']).strip()
    endpoint = docker.json(['context', 'inspect', context])[0]['Endpoints']['docker']['Host']
    # Explicit DOCKER_CONTEXT wins over DOCKER_HOST, as in the Docker CLI.
    if not os.environ.get('DOCKER_CONTEXT') and os.environ.get('DOCKER_HOST'):
        endpoint = os.environ['DOCKER_HOST']
    require(endpoint in ('unix:///var/run/docker.sock', 'unix:///run/docker.sock'),
            '이 구성은 로컬 /var/run/docker.sock Docker만 지원합니다. 원격·rootless context는 사용할 수 없습니다.')
    info = docker.json(['info', '--format', '{{json .}}'])
    require(info['OSType'] == 'linux' and info['Architecture'] in ('x86_64', 'aarch64', 'amd64', 'arm64'),
            'Linux amd64/arm64 Docker 호스트가 필요합니다.')
    require(not any('rootless' in v for v in info.get('SecurityOptions', [])), 'rootless Docker는 지원하지 않습니다.')
    require(str(info['CgroupVersion']) == '2' and info['Driver'] == 'overlay2',
            '이번 O01 구성은 cgroup v2 / overlay2용입니다. 다른 환경은 별도 검증이 필요합니다.')
    version = docker.run(['compose', 'version', '--short']).strip().lstrip('v')
    require(tuple(int(v) for v in version.split('.')[:2]) >= (2, 24), 'Docker Compose 2.24 이상이 필요합니다.')
    root = no_symlinks(Path(info['DockerRootDir']))
    require(root.is_absolute() and root.is_dir(), '로컬 Docker 저장 경로를 확인할 수 없습니다.')
    return {'docker_root': str(root), 'docker_version': info['ServerVersion'],
            'compose_version': version, 'cpu_count': int(info['NCPU']),
            'memory_bytes': int(info['MemTotal']), 'hostname': socket.gethostname(),
            'context': context}


def mountpoint(path):
    path = Path(path).resolve()
    while not os.path.ismount(path):
        path = path.parent
    return str(path)


def check_disks(data_dir, docker_root):
    data = Path(data_dir)
    parent = data if data.exists() else data.parent
    while not parent.exists():
        parent = parent.parent
    root = Path(docker_root)
    require(not data.is_relative_to(root), '관측 데이터는 Docker 저장 경로 밖에 보관해야 합니다.')
    require(parent.stat().st_dev != root.stat().st_dev,
            'Docker 디스크가 97% 사용 중인 서버용 구성입니다. 관측 데이터는 다른 파일시스템을 선택하세요.')
    usage = {'data': shutil.disk_usage(parent), 'docker': shutil.disk_usage(root)}
    for name, value in usage.items():
        require(value.free >= 20 * GIB, name + ' 파일시스템의 여유 공간이 20 GiB 미만입니다. 자동 정리는 하지 않습니다.')
    return {name: {'free_gib': round(value.free / GIB, 1),
                   'used_percent': round(100 * value.used / value.total, 1)}
            for name, value in usage.items()}


def project_containers(docker, config_id=None):
    ids = docker.run(['ps', '-aq', '--filter', 'label=com.docker.compose.project=' + PROJECT]).split()
    if not ids:
        return []
    items = docker.json(['inspect', *ids])
    for item in items:
        labels = item['Config'].get('Labels') or {}
        require(config_id and labels.get(OWNER) == config_id
                and labels.get('com.docker.compose.service') in SERVICES,
                '소유권을 확인할 수 없는 관측 컨테이너가 있습니다. 자동 인수·삭제하지 않습니다.')
    return items


def check_ports(items):
    running = {x['Config']['Labels']['com.docker.compose.service'] for x in items if x['State']['Running']}
    for service, port in PORTS.items():
        if service in running:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
            except OSError as exc:
                raise ValueError('관측 포트가 이미 사용 중입니다: ' + str(port)) from exc


def new_state(path, host):
    path = no_symlinks(path)
    require(path.name == 'prometheus' and path.is_relative_to(Path.home()),
            '--data-dir은 홈 디렉터리 안의 전용 prometheus 디렉터리여야 합니다.')
    require(not path.is_relative_to(ROOT.parents[2]), '프로젝트 소스 안에는 관측 데이터를 저장하지 않습니다.')
    require(not path.exists() and not path.parent.exists(),
            '기존 디렉터리를 자동 인수하지 않습니다. 새 전용 상위 디렉터리를 선택하세요.')
    state = {'schema': 1, 'config_id': str(uuid.uuid4()), 'data_dir': str(path),
             'docker_root': host['docker_root'], 'uid': os.getuid(), 'gid': os.getgid(), 'images': {}}
    path.parent.mkdir(parents=True, mode=0o700)
    path.mkdir(mode=0o700)
    write_private(path.parent / '.owner.json', {'config_id': state['config_id']})
    return state


def validate_state(state, host, data_dir=None):
    require(state['schema'] == 1 and str(uuid.UUID(state['config_id'])) == state['config_id'], '지원하지 않는 상태 파일입니다.')
    path = no_symlinks(Path(state['data_dir']))
    require(state['uid'] == os.getuid() and state['gid'] == os.getgid(), '최초 준비 사용자와 같은 계정으로 실행하세요.')
    require(state['docker_root'] == host['docker_root'], 'Docker 저장 경로가 바뀌었습니다. 기존 데이터 연결을 확인하세요.')
    require(data_dir is None or str(no_symlinks(data_dir)) == str(path), '기존 관측 데이터 경로를 변경할 수 없습니다.')
    require(path.is_dir(), '기존 관측 데이터 디렉터리가 없습니다. 빈 저장소로 자동 재생성하지 않습니다.')
    require(path.stat().st_uid == state['uid'] and not path.stat().st_mode & 0o077,
            '관측 데이터 디렉터리 소유자·권한이 변경됐습니다.')
    require(read_private(path.parent / '.owner.json') == {'config_id': state['config_id']},
            '관측 데이터 소유권 표식이 일치하지 않습니다.')
    for service, entry in state['images'].items():
        require(service in TAGS and entry['tag'] == TAGS[service], '이미지 버전이 상태 파일과 일치하지 않습니다.')
        require(re.fullmatch(re.escape(TAGS[service].rsplit(':', 1)[0]) + r'@sha256:[0-9a-f]{64}', entry['digest']),
                '고정된 이미지 digest가 올바르지 않습니다.')


def environment(state):
    # Single-quoted dotenv values prevent interpolation of the regex end marker ($).
    exclude = r'^(/(dev|proc|sys|run|var/lib/kubelet)($|/)|' + re.escape(state['docker_root']) + r'($|/))'
    values = {'OBS_CONFIG_ID': state['config_id'], 'OBS_UID': state['uid'], 'OBS_GID': state['gid'],
              'OBS_DATA_DIR': state['data_dir'], 'OBS_DOCKER_ROOT': state['docker_root'], 'OBS_FS_EXCLUDE': exclude}
    for name, service in zip(('OBS_PROMETHEUS_IMAGE', 'OBS_NODE_IMAGE', 'OBS_CADVISOR_IMAGE'), SERVICES):
        values[name] = state['images'][service]['digest']
    for value in values.values():
        require(not any(c in str(value) for c in "'\n\r\0"), '지원하지 않는 경로 문자가 있습니다.')
    return ''.join(key + "='" + str(value) + "'\n" for key, value in values.items())


def gpu_enabled(root):
    path = root / 'gpu/.state.json'
    if not path.exists() and not path.is_symlink():
        return False
    gpu = read_private(path)
    require(gpu.get('schema') == 1 and gpu.get('config_id') == read_private(root / '.state.json')['config_id'],
            'GPU 관측 설정의 소유권이 일치하지 않습니다.')
    return gpu.get('enabled') is True


def prometheus_config(root):
    if alerts_enabled(root):
        return root / 'alerts/prometheus.yml'
    return root / ('gpu/prometheus.yml' if gpu_enabled(root) else 'prometheus/prometheus.yml')


def alerts_enabled(root):
    path = root / 'alerts/.state.json'
    if not path.exists() and not path.is_symlink():
        return False
    state = read_private(path)
    require(state.get('schema') == 1 and state.get('config_id') == read_private(root / '.state.json')['config_id'],
            'O04 경보 설정 소유권이 일치하지 않습니다.')
    require(state.get('enabled') is not True or gpu_enabled(root), 'O04 구성에는 검증된 GPU 수집 설정이 필요합니다.')
    return state.get('enabled') is True


def compose(docker, root, args, **kwargs):
    files = ['-f', str(root / 'docker-compose.yml')]
    if gpu_enabled(root):
        files += ['-f', str(root / 'gpu/prometheus-overlay.yml')]
    if alerts_enabled(root):
        files += ['-f', str(root / 'alerts/prometheus-overlay.yml')]
    return docker.run(['compose', '--project-name', PROJECT, '--env-file', str(root / '.env'),
                       *files, *args], **kwargs)


def config_check(docker, root, state):
    no_symlinks(root / '.env')
    require((root / '.env').read_text() == environment(state), '생성된 관측 .env가 변경됐습니다. prepare로 상태와 다시 맞추세요.')
    config = json.loads(compose(docker, root, ['config', '--format', 'json']))
    require(set(config['services']) == set(SERVICES), '관측 구성에 예상하지 않은 서비스가 있습니다.')
    for name, service in config['services'].items():
        require(service['image'] == state['images'][name]['digest']
                and service.get('network_mode') == 'host'
                and service.get('labels', {}).get(OWNER) == state['config_id'],
                '관측 서비스 이미지·네트워크·소유권 설정이 변경됐습니다.')
        require(not service.get('ports') and service.get('read_only') is True,
                '관측 서비스의 공개 포트 또는 파일시스템 설정이 변경됐습니다.')
        # Compose config can accept a YAML flow list that splits tmpfs options
        # into separate mounts. Validate the resolved mounts before daemon calls.
        expected_tmpfs = [] if name == 'obs-node-exporter' else ['/tmp:size=16777216,mode=1777']
        require(service.get('tmpfs', []) == expected_tmpfs,
                '관측 tmpfs 마운트가 올바르지 않습니다: ' + name
                + '. /tmp 경로와 size/mode 옵션은 하나의 문자열이어야 합니다.')
        flags = service.get('command', [])
        listen = ('--listen_ip=127.0.0.1' if name == 'obs-cadvisor'
                  else '--web.listen-address=127.0.0.1:' + str(PORTS[name]))
        require(listen in flags, '수집기와 저장소는 loopback에서만 접근해야 합니다.')
    mounts = config['services']['obs-prometheus']['volumes']
    require(any(v.get('source') == state['data_dir'] and v['target'] == '/prometheus' for v in mounts),
            'Prometheus 데이터 연결이 일치하지 않습니다.')


def prepare(docker, root, host, data_dir=None):
    file = root / '.state.json'
    if file.exists() or file.is_symlink():
        state = read_private(file)
        validate_state(state, host, data_dir)
    else:
        project_containers(docker)
        check_ports([])
        check_disks(data_dir or DEFAULT_DATA, host['docker_root'])
        state = new_state(data_dir or DEFAULT_DATA, host)
        write_private(file, state)
    project_containers(docker, state['config_id'])
    check_disks(state['data_dir'], host['docker_root'])
    for service, tag in TAGS.items():
        if service in state['images']:
            # An absent local image is pulled by its recorded immutable digest, never by a moving tag.
            image = state['images'][service]['digest']
            present = docker.run(['image', 'ls', '-q', image]).strip()
            if not present:
                docker.run(['pull', image], visible=True, timeout=900)
            continue
        docker.run(['pull', tag], visible=True, timeout=900)
        image = docker.json(['image', 'inspect', tag])[0]
        repository = tag.rsplit(':', 1)[0] + '@sha256:'
        digests = [v for v in image.get('RepoDigests', []) if v.startswith(repository)]
        require(len(digests) == 1, '공식 저장소 이미지 digest를 확정할 수 없습니다: ' + service)
        state['images'][service] = {'tag': tag, 'digest': digests[0], 'image_id': image['Id']}
        write_private(file, state)
    validate_state(state, host, data_dir)
    write_private(root / '.env', environment(state))
    config_check(docker, root, state)
    return state


class Prometheus:
    def __init__(self, url='http://127.0.0.1:16310'):
        self.url = url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path):
        with self.opener.open(self.url + path, timeout=5) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        require(len(raw) <= 8 * 1024 * 1024, '지표 응답이 제한 크기를 초과했습니다.')
        body = json.loads(raw)
        require(body.get('status') == 'success', 'Prometheus API 응답이 실패했습니다.')
        return body['data']

    def query(self, expression):
        data = self.get('/api/v1/query?' + urllib.parse.urlencode({'query': expression}))
        require(data.get('resultType') == 'vector', '예상하지 않은 지표 형식입니다.')
        return data['result']


def values(rows):
    result = []
    for row in rows:
        timestamp, raw = row['value']
        value = float(raw)
        require(math.isfinite(value) and time.time() - float(timestamp) < 90, '지표가 오래됐거나 유효하지 않습니다.')
        result.append((row['metric'], value))
    return result


def required_containers(docker):
    rows = docker.run(['ps', '--format', '{{json .}}']).splitlines()
    names = set()
    for row in rows:
        name = json.loads(row)['Names']
        if re.match(r'^(model|data|knowledge)-op-', name):
            continue
        if re.match(r'^(agent-platform-|infrastructure-|model-|data-|knowledge-|ops-|postgres$|pgadmin$|opensearch-)', name):
            names.add(name)
    require(names, '검사할 실행 중 플랫폼 컨테이너가 없습니다.')
    return names


def scrape_timestamp(value):
    """Parse Prometheus RFC3339Nano without version-dependent fromisoformat.

    Keep the explicit timezone and truncate sub-microsecond precision only;
    freshness is still checked against the same 90-second limit.
    """
    message = 'Prometheus lastScrape 시각 형식이 올바르지 않습니다.'
    require(isinstance(value, str), message)
    match = re.fullmatch(
        r'([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})'
        r'(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})', value)
    require(match is not None, message)
    parts = match.groups()
    microsecond = int((parts[6] or '').ljust(6, '0')[:6])
    zone = dt.timezone.utc
    if parts[7] != 'Z':
        offset = parts[7]
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        require(hours < 24 and minutes < 60, message)
        delta = dt.timedelta(hours=hours, minutes=minutes)
        zone = dt.timezone(delta if offset[0] == '+' else -delta)
    try:
        return dt.datetime(*(int(v) for v in parts[:6]), microsecond=microsecond, tzinfo=zone).timestamp()
    except ValueError as exc:
        raise ValueError(message) from exc


def inspect_metrics(prom, host, expected_names, data_dir):
    targets = prom.get('/api/v1/targets')['activeTargets']
    jobs = {t['labels']['job'] for t in targets}
    require(jobs in ({'prometheus', 'host', 'containers'}, {'prometheus', 'host', 'containers', 'gpu'})
            and len(targets) == len(jobs), '기본 3개와 선택한 GPU 수집 대상 구성이 일치하지 않습니다.')
    for target in targets:
        when = scrape_timestamp(target['lastScrape'])
        require(target['health'] == 'up' and 0 <= time.time() - when < 90,
                '수집 대상이 아직 정상 상태가 아닙니다: ' + target['labels']['job'])
    uname = values(prom.query('node_uname_info{job="host"}'))
    require(any(labels.get('nodename') == host['hostname'] for labels, _ in uname), '호스트 이름 지표가 실제 호스트와 일치하지 않습니다.')
    cpus = values(prom.query('node_cpu_seconds_total{job="host",mode="idle"}'))
    require(len({labels.get('cpu') for labels, _ in cpus}) == host['cpu_count'], '호스트 CPU 수가 일치하지 않습니다.')
    memory = values(prom.query('node_memory_MemTotal_bytes{job="host"}'))
    require(len(memory) == 1 and abs(memory[0][1] - host['memory_bytes']) < 64 * 1024 ** 2,
            '호스트 메모리 지표가 일치하지 않습니다.')
    filesystems = values(prom.query('node_filesystem_avail_bytes{job="host",fstype!="tmpfs"}'))
    mounts = {labels.get('mountpoint') for labels, value in filesystems if value > 0}
    needed_mounts = {'/', mountpoint(host['docker_root']), mountpoint(data_dir)}
    require(needed_mounts <= mounts, '필수 디스크 지표가 없습니다: ' + ', '.join(sorted(needed_mounts - mounts)))
    for metric in ('container_cpu_usage_seconds_total', 'container_memory_working_set_bytes', 'container_last_seen'):
        rows = values(prom.query(metric + '{job="containers",name!=""}'))
        seen = {labels['name'].lstrip('/') for labels, value in rows
                if metric != 'container_last_seen' or 0 <= time.time() - value < 90}
        missing = expected_names - seen
        require(not missing, metric + ' 지표 누락: ' + ', '.join(sorted(missing)))
    return {'result': 'passed', 'stage': 'O01', 'scope': 'host and Docker resource metrics',
            'targets_up': len(targets), 'host_cpu_count': host['cpu_count'], 'host_memory_verified': True,
            'filesystem_mounts_verified': sorted(needed_mounts), 'platform_containers_verified': len(expected_names),
            'inference_executed': False, 'domain_success_assessed': False,
            'gpu_metrics_assessed': False, 'llm_traces_assessed': False}


def check(docker, state, host, wait_seconds):
    expected = required_containers(docker)
    prom = Prometheus()
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            result = inspect_metrics(prom, host, expected, state['data_dir'])
            result.update(data_dir=state['data_dir'], docker_root=host['docker_root'],
                          retention={'time': '7d', 'size': '5GB', 'hard_disk_quota': False})
            return result
        except (ValueError, OSError, KeyError) as exc:
            if time.monotonic() >= deadline:
                raise ValueError('수집 검증 미통과: ' + str(exc)) from exc
            time.sleep(min(5, max(0, deadline - time.monotonic())))


@contextlib.contextmanager
def lock(root):
    fd = os.open(root / '.ops.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('다른 관측 설정 작업이 진행 중입니다.') from exc
        yield


def execute(action, docker, root, data_dir=None, wait_seconds=120):
    host = host_info(docker)
    if action in ('prepare', 'up'):
        state = prepare(docker, root, host, data_dir)
    else:
        require((root / '.state.json').is_file(), '먼저 prepare 또는 src/infra/ops/up.py --observability-only를 실행하세요.')
        state = read_private(root / '.state.json')
        validate_state(state, host, data_dir)
        config_check(docker, root, state)
    items = project_containers(docker, state['config_id'])
    if action == 'status':
        return {'result': 'read', 'project': PROJECT, 'data_dir': state['data_dir'],
                'containers': [{'service': x['Config']['Labels']['com.docker.compose.service'],
                                'running': x['State']['Running'],
                                'health': x['State'].get('Health', {}).get('Status', 'not_reported')} for x in items],
                'metrics_acceptance': 'not_assessed'}
    if action == 'stop':
        compose(docker, root, ['stop', *SERVICES], visible=True)
        return {'result': 'stopped', 'project': PROJECT, 'data_preserved': True}
    if action == 'check':
        return check(docker, state, host, wait_seconds)
    disks = check_disks(state['data_dir'], host['docker_root'])
    check_ports(items)
    if action == 'up':
        docker.run(['run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--entrypoint', '/bin/promtool',
                    '--mount', 'type=bind,src=' + str(prometheus_config(root)) + ',dst=/etc/prometheus/prometheus.yml,readonly',
                    state['images']['obs-prometheus']['digest'], 'check', 'config', '/etc/prometheus/prometheus.yml'], visible=True)
        compose(docker, root, ['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180', *SERVICES],
                visible=True, timeout=240)
        return check(docker, state, host, wait_seconds)
    return {'result': 'prepared' if action == 'prepare' else 'ready', 'stage': 'O01',
            'data_dir': state['data_dir'], 'disks': disks, 'image_tags': TAGS,
            'images_pinned_by_digest': True, 'services_started': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'preflight', 'up', 'check', 'status', 'stop'))
    parser.add_argument('--data-dir', type=Path, help='New dedicated path ending in /prometheus; cannot change an existing binding')
    parser.add_argument('--wait-seconds', type=int, default=120)
    args = parser.parse_args(argv)
    require(0 <= args.wait_seconds <= 300, '--wait-seconds 범위는 0~300입니다.')
    with lock(ROOT) if args.action in ('prepare', 'up', 'stop') else contextlib.nullcontext():
        result = execute(args.action, Docker(), ROOT, args.data_dir, args.wait_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        sys.exit(1)
