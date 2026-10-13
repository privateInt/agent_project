#!/usr/bin/env python3
"""Isolated Langfuse infra lifecycle. Existing domain services are outside this project."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import uuid

from lf_common import (ROOT, PROJECT, OWNER, DEFAULT_STATE, PORT, API_URL, SERVICES, STORES,
                       Docker, Client, HttpFailure, require, safe_path, read_json,
                       private_read, write_private, fingerprint, operation_lock, DockerFailure)

GIB = 1024 ** 3
LOCK = json.loads((ROOT / 'images.lock.json').read_text())
IMAGES = {key: value['image'] for key, value in LOCK['images'].items()}
VERSION = LOCK['langfuse_version']


def host_info(docker):
    require(sys.platform == 'linux' and os.getuid() != 0, 'Linux 호스트의 일반 사용자로 실행하세요. sudo는 사용하지 않습니다.')
    context = docker.run(['context', 'show']).strip()
    endpoint = docker.json(['context', 'inspect', context])[0]['Endpoints']['docker']['Host']
    if not os.environ.get('DOCKER_CONTEXT') and os.environ.get('DOCKER_HOST'):
        endpoint = os.environ['DOCKER_HOST']
    require(endpoint in ('unix:///var/run/docker.sock', 'unix:///run/docker.sock'), '로컬 rootful Docker만 지원합니다.')
    info = docker.json(['info', '--format', '{{json .}}'])
    require(info['OSType'] == 'linux' and info['Architecture'] in ('x86_64', 'amd64', 'aarch64', 'arm64'),
            'Linux amd64/arm64 호스트가 필요합니다.')
    require(not any('rootless' in x for x in info.get('SecurityOptions', [])), 'rootless Docker는 지원하지 않습니다.')
    version = docker.run(['compose', 'version', '--short']).strip().lstrip('v')
    require(tuple(int(x) for x in version.split('.')[:2]) >= (2, 30), 'Compose 2.30 이상이 필요합니다 (raw env_file).')
    root = safe_path(info['DockerRootDir'])
    require(root.is_dir(), '로컬 Docker 저장 경로를 확인하지 못했습니다.')
    return {'docker_root': str(root), 'context': context, 'hostname': socket.gethostname(),
            'cpus': int(info['NCPU']), 'memory_bytes': int(info['MemTotal'])}


def resources(root, host):
    root = safe_path(root)
    parent = root
    while not parent.exists():
        parent = parent.parent
    docker_root = Path(host['docker_root'])
    require(not root.is_relative_to(docker_root) and parent.stat().st_dev != docker_root.stat().st_dev,
            'Langfuse 상태/데이터는 Docker 저장소와 다른 파일시스템에 두세요.')
    require(not root.is_relative_to(ROOT.parents[1]), 'Langfuse 데이터는 프로젝트 소스 밖에 보관하세요.')
    disks = {}
    for name, path, minimum in [('data', parent, 50), ('docker', docker_root, 20)]:
        usage = shutil.disk_usage(path)
        require(usage.free >= minimum * GIB, name + ' 여유 공간이 ' + str(minimum) + ' GiB 미만입니다. 자동 정리하지 않습니다.')
        disks[name] = {'free_gib': round(usage.free / GIB, 1), 'used_percent': round(usage.used / usage.total * 100, 1)}
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    available = int(memory['MemAvailable'].strip().split()[0]) * 1024
    require(host['cpus'] >= 4 and host['memory_bytes'] >= 24 * GIB and available >= 24 * GIB,
            '이번 21 GiB 컨테이너 한도 구성에는 CPU 4개·가용 메모리 24 GiB 이상이 필요합니다.')
    return {'disks': disks, 'available_memory_gib': round(available / GIB, 1)}


def validate_url(url):
    try:
        value = urllib.parse.urlsplit(url)
        require(value.scheme == 'http' and value.hostname and value.port == PORT and not value.username
                and not value.password and value.path in ('', '/') and not value.query and not value.fragment,
                '이번 직접 접속 주소는 http://<서버IP 또는 호스트명>:16320 형식입니다.')
        require(not any(c.isspace() for c in url), '접속 주소에 공백을 넣을 수 없습니다.')
    except ValueError:
        raise ValueError('접속 주소는 http://<서버IP 또는 호스트명>:16320 형식이어야 합니다.') from None
    return url.rstrip('/')


def owned_containers(docker, owner=None):
    ids = docker.run(['ps', '-aq', '--filter', 'label=com.docker.compose.project=' + PROJECT]).split()
    items = docker.json(['inspect', *ids]) if ids else []
    seen = set()
    for item in items:
        labels = item['Config'].get('Labels') or {}
        service = labels.get('com.docker.compose.service')
        require(owner and labels.get(OWNER) == owner and service in SERVICES and service not in seen,
                'Langfuse 컨테이너 소유권/중복을 확인할 수 없습니다. 자동 인수하지 않습니다.')
        seen.add(service)
    return items


def check_networks(docker, owner):
    ids = docker.run(['network', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + PROJECT]).split()
    for item in docker.json(['network', 'inspect', *ids]) if ids else []:
        labels = item.get('Labels') or {}
        require(owner and labels.get(OWNER) == owner and labels.get('com.docker.compose.network') in ('private', 'ingress'),
                'Langfuse 네트워크 소유권이 일치하지 않습니다.')


def port_free(items):
    if any(x['State']['Running'] and x['Config']['Labels']['com.docker.compose.service'] == 'lf-web' for x in items):
        return
    with socket.socket() as sock:
        try:
            sock.bind(('0.0.0.0', PORT))
        except OSError:
            raise ValueError('Langfuse 포트 16320이 이미 사용 중입니다.') from None


def env_text(values):
    # env_file format: raw; passwords containing $, quotes or backslashes stay literal.
    require(all(not any(c in str(v) for c in '\r\n\x00') for v in values.values()), '환경값에 개행/NUL이 있습니다.')
    return ''.join(k + '=' + str(v) + '\n' for k, v in sorted(values.items()))


def canonical_image(value):
    value = value.removeprefix('docker.io/').removeprefix('registry-1.docker.io/')
    return value.removeprefix('library/')


def environments(s):
    common = {
        'NEXTAUTH_URL': s['public_url'], 'DATABASE_URL': 'postgresql://langfuse:' + s['db_password'] + '@lf-postgres:5432/langfuse',
        'SALT': s['salt'], 'ENCRYPTION_KEY': s['encryption_key'], 'TELEMETRY_ENABLED': 'false',
        'CLICKHOUSE_MIGRATION_URL': 'clickhouse://lf-clickhouse:9000', 'CLICKHOUSE_URL': 'http://lf-clickhouse:8123',
        'CLICKHOUSE_USER': 'langfuse', 'CLICKHOUSE_PASSWORD': s['ch_password'], 'CLICKHOUSE_CLUSTER_ENABLED': 'false',
        'REDIS_HOST': 'lf-redis', 'REDIS_PORT': '6379', 'REDIS_AUTH': s['redis_password'],
        'LANGFUSE_S3_BATCH_EXPORT_ENABLED': 'false', 'LANGFUSE_IN_APP_AGENT_ENABLED': 'false',
        'LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES': 'false', 'LANGFUSE_OBSERVATION_FIELD_OVERFLOW_ENABLED': 'false',
        'LANGFUSE_OTEL_INGESTION_MAX_BODY_BYTES': '1048576',
    }
    for kind, prefix in [('EVENT', 'events/'), ('MEDIA', 'media/')]:
        for key, value in {'BUCKET': 'langfuse', 'REGION': 'us-east-1', 'ACCESS_KEY_ID': s['s3_key'],
                           'SECRET_ACCESS_KEY': s['s3_secret'], 'ENDPOINT': 'http://lf-minio:9000',
                           'FORCE_PATH_STYLE': 'true', 'PREFIX': prefix}.items():
            common['LANGFUSE_S3_' + kind + '_UPLOAD_' + key] = value
    web = dict(common, NEXTAUTH_SECRET=s['nextauth_secret'], AUTH_DISABLE_SIGNUP='true',
               LANGFUSE_INIT_ORG_ID=s['org_id'], LANGFUSE_INIT_ORG_NAME='Agent Project',
               LANGFUSE_INIT_PROJECT_ID=s['project_id'], LANGFUSE_INIT_PROJECT_NAME='Agent traces',
               LANGFUSE_INIT_PROJECT_PUBLIC_KEY=s['public_key'], LANGFUSE_INIT_PROJECT_SECRET_KEY=s['secret_key'],
               LANGFUSE_INIT_USER_EMAIL=s['email'], LANGFUSE_INIT_USER_NAME=s['user_name'],
               LANGFUSE_INIT_USER_PASSWORD=s['password'])
    return {
        'worker.env': env_text(common), 'web.env': env_text(web),
        'postgres.env': env_text({'POSTGRES_USER': 'langfuse', 'POSTGRES_DB': 'langfuse',
                                  'POSTGRES_PASSWORD': s['db_password'], 'TZ': 'UTC', 'PGTZ': 'UTC'}),
        'clickhouse.env': env_text({'CLICKHOUSE_USER': 'langfuse', 'CLICKHOUSE_DB': 'default',
                                    'CLICKHOUSE_PASSWORD': s['ch_password']}),
        'redis.env': env_text({'REDISCLI_AUTH': s['redis_password']}),
        'minio.env': env_text({'MINIO_ROOT_USER': s['s3_key'], 'MINIO_ROOT_PASSWORD': s['s3_secret'], 'HOME': '/tmp',
                               'MC_HOST_local': 'http://' + s['s3_key'] + ':' + s['s3_secret'] + '@127.0.0.1:9000'}),
        'compose.env': env_text({'LF_STATE_DIR': s['state_dir'], 'LF_OWNER': s['owner'], 'LF_UID': s['uid'], 'LF_GID': s['gid']}),
    }


def load(root, host=None, check_env=True):
    root = safe_path(root)
    info = root.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'Langfuse 상태 폴더는 현재 사용자 소유 0700이어야 합니다.')
    s = read_json(root / 'settings.json')
    require(s['schema'] == 1 and s['state_dir'] == str(root) and s['uid'] == os.getuid() and s['gid'] == os.getgid(),
            'Langfuse 상태 경로/사용자/schema가 바뀌었습니다.')
    require(str(uuid.UUID(s['owner'])) == s['owner'] and s['images'] == IMAGES, 'Langfuse 소유권/이미지 고정이 일치하지 않습니다.')
    require(read_json(root / 'owner.json') == {'owner': s['owner']}, 'Langfuse 저장소 소유권 표식이 일치하지 않습니다.')
    if host:
        require(s['host'] == {k: host[k] for k in ('docker_root', 'context', 'hostname')}, 'Docker 저장소/context/호스트가 바뀌었습니다.')
    for name, binding in s['storage'].items():
        path = safe_path(root / 'data' / name)
        st = path.stat()
        require(path.is_dir() and [st.st_dev, st.st_ino] == binding, '기존 Langfuse 저장 연결이 없어졌거나 바뀌었습니다: ' + name)
    require(set(s['storage']) == set(STORES), 'Langfuse 저장 연결이 누락되었습니다.')
    if check_env:
        for name, expected in environments(s).items():
            require(private_read(root / name) == expected, 'Langfuse 환경 파일이 상태와 다릅니다: ' + name)
    return s


def prepare(docker, root, host, public_url=None):
    root = safe_path(root)
    if (root / 'settings.json').exists():
        s = load(root, host, check_env=False)
        require(public_url is None or validate_url(public_url) == s['public_url'], '기존 접속 주소를 자동 교체하지 않습니다.')
        for name, expected in environments(s).items():
            path = root / name
            if path.exists():
                require(private_read(path) == expected, '기존 환경 파일이 다릅니다: ' + name)
            else:
                write_private(path, expected)
        return load(root, host)
    require(not root.exists(), '설정 없는 기존 폴더는 자동 인수하지 않습니다: ' + str(root))
    require(not any(c in str(root) for c in "\r\n\x00$'\\,"), '상태 경로에 지원하지 않는 문자가 있습니다.')
    owned_containers(docker)
    check_networks(docker, None)
    port_free([])
    resources(root, host)
    url = validate_url(public_url or input('Langfuse 접속 주소 [Enter: http://localhost:16320]: ').strip() or 'http://localhost:16320')
    email = input('Langfuse 최초 관리자 이메일: ').strip().lower()
    require(re.fullmatch(r'[^\s@=]+@[^\s@=]+\.[^\s@=]+', email), '이메일 형식을 확인하세요.')
    name = input('관리자 표시 이름 [Enter: 관리자]: ').strip() or '관리자'
    password = getpass.getpass('Langfuse 비밀번호 (8자 이상, UTF-8 72바이트 이하): ')
    require(len(password) >= 8 and len(password.encode()) <= 72 and not any(c in password for c in '\r\n\x00'),
            '비밀번호는 8자 이상·UTF-8 72바이트 이하이며 개행을 포함할 수 없습니다.')
    require(password == getpass.getpass('비밀번호 확인: '), '비밀번호가 일치하지 않습니다.')
    s = {'schema': 1, 'owner': str(uuid.uuid4()), 'uid': os.getuid(), 'gid': os.getgid(), 'state_dir': str(root),
         'host': {k: host[k] for k in ('docker_root', 'context', 'hostname')}, 'images': IMAGES,
         'public_url': url, 'email': email, 'user_name': name, 'password': password,
         'org_id': 'agent-org-' + uuid.uuid4().hex, 'project_id': 'agent-traces-' + uuid.uuid4().hex,
         'public_key': 'pk-lf-' + str(uuid.uuid4()), 'secret_key': 'sk-lf-' + secrets.token_hex(32),
         'storage': {}}
    for key in ('db_password', 'ch_password', 'redis_password', 's3_secret', 'salt', 'encryption_key', 'nextauth_secret'):
        s[key] = secrets.token_hex(32)
    s['s3_key'] = 'lf' + secrets.token_hex(12)
    environments(s)  # Reject unrepresentable values before creating any files.
    root.mkdir(parents=True, mode=0o700)
    (root / 'data').mkdir(mode=0o700)
    for name in STORES:
        path = root / 'data' / name
        path.mkdir(mode=0o700)
        st = path.stat()
        s['storage'][name] = [st.st_dev, st.st_ino]
    write_private(root / 'owner.json', {'owner': s['owner']})
    write_private(root / 'settings.json', s)
    for name, content in environments(s).items():
        write_private(root / name, content)
    return load(root, host)


def compose(s, *args):
    return ['compose', '--project-name', PROJECT, '--env-file', str(Path(s['state_dir']) / 'compose.env'),
            '-f', str(ROOT / 'docker-compose.yml'), *args]


def config_contract(config, s):
    require(config.get('name') == PROJECT and set(config['services']) == set(SERVICES), 'Compose 프로젝트/서비스 범위가 다릅니다.')
    for key, service in config['services'].items():
        require(service['image'] == IMAGES[key] and service.get('labels', {}).get(OWNER) == s['owner'], 'Compose 이미지/소유권이 다릅니다.')
        require(not service.get('privileged') and not service.get('devices') and not service.get('network_mode'), 'Langfuse 권한 범위가 다릅니다.')
        expected = {'postgres': 'lf-postgres', 'clickhouse': 'lf-clickhouse', 'clickhouse-logs': 'lf-clickhouse', 'redis': 'lf-redis', 'minio': 'lf-minio'}
        mounts = {m['target']: m['source'] for m in service.get('volumes', [])}
        require(mounts == {STORES[n]: str(Path(s['state_dir']) / 'data' / n) for n, owner in expected.items() if owner == key},
                'Compose 저장 경로가 다릅니다.')
        if key in ('lf-worker', 'lf-web'):
            require(service.get('environment', {}).get('HOSTNAME') == '0.0.0.0', 'Web/Worker HOSTNAME=0.0.0.0 설정이 필요합니다.')
        ports = service.get('ports', [])
        require((key == 'lf-web' and len(ports) == 1 and ports[0]['host_ip'] == '0.0.0.0'
                 and str(ports[0]['published']) == str(PORT) and ports[0]['target'] == 3000)
                or (key != 'lf-web' and not ports), 'Compose 공개 포트 범위가 다릅니다.')
    require(config['networks']['private'].get('internal') is True, '저장소 전용 내부 네트워크가 필요합니다.')


def inspect_binding(item, s):
    service = item['Config']['Labels']['com.docker.compose.service']
    require(canonical_image(item['Config']['Image']) == canonical_image(IMAGES[service]), '기존 컨테이너 이미지가 고정본과 다릅니다.')
    names = ['clickhouse', 'clickhouse-logs'] if service == 'lf-clickhouse' else [service.removeprefix('lf-')]
    expected = {STORES[n]: str(Path(s['state_dir']) / 'data' / n) for n in names if n in STORES}
    actual = {m['Destination']: m['Source'] for m in item['Mounts'] if m['Type'] == 'bind'}
    require(actual == expected and all(m['Type'] == 'bind' for m in item['Mounts']), '컨테이너 저장 연결이 달라졌습니다.')


def check(docker, s, wait_seconds=120, client=None):
    client = client or Client(s)
    deadline = time.monotonic() + wait_seconds
    last = '준비 중'
    while True:
        items = owned_containers(docker, s['owner'])
        for item in items:
            inspect_binding(item, s)
        healthy = len(items) == 6 and all(x['State']['Running'] and x['State'].get('Health', {}).get('Status') == 'healthy' for x in items)
        try:
            require(healthy, 'Langfuse 서비스 6개의 Healthy를 기다리고 있습니다.')
            health = client.request('GET', '/api/public/health?failIfDatabaseUnavailable=true', authenticated=False)
            require(health.get('version', '').lstrip('v') == VERSION, '실제 Langfuse 버전이 고정 버전과 다릅니다.')
            client.request('GET', '/api/public/ready', authenticated=False)
            projects = client.request('GET', '/api/public/projects')['data']
            require(len(projects) == 1 and projects[0]['id'] == s['project_id']
                    and projects[0]['organization']['id'] == s['org_id'], '초기 프로젝트/API 키 연결이 다릅니다.')
            return {'stage': 'O01-Langfuse', 'result': 'ready', 'services_healthy': 6, 'version': VERSION,
                    'project_id': s['project_id'], 'web_url': s['public_url'], 'state_dir': s['state_dir'],
                    'retention_days': projects[0].get('retentionDays'), 'hard_disk_quota': False,
                    'trace_delivery_verified': False, 'agent_instrumented': False, 'inference_executed': False}
        except (ValueError, KeyError) as exc:
            # Do not leak HTTP bodies or environment values into the report.
            last = str(exc) if isinstance(exc, ValueError) else 'Langfuse API 응답 계약이 다릅니다.'
        if time.monotonic() >= deadline:
            raise ValueError('Langfuse 준비 검증 미통과: ' + last)
        time.sleep(min(3, max(0, deadline - time.monotonic())))


def up(docker, s, host):
    root = Path(s['state_dir'])
    capacity = resources(root, host)
    items = owned_containers(docker, s['owner'])
    check_networks(docker, s['owner'])
    for item in items:
        inspect_binding(item, s)
    port_free(items)
    config_contract(docker.json(compose(s, 'config', '--format', 'json')), s)
    for service in SERVICES:
        image = IMAGES[service]
        try:
            present = docker.json(['image', 'inspect', image])
            require(present and canonical_image(image) in [canonical_image(x) for x in present[0].get('RepoDigests', [])], '이미지 digest를 확인하지 못했습니다.')
        except ValueError:
            print(service + ' 고정 이미지 준비 중...', flush=True)
            docker.run(['pull', image], timeout=1200)
            present = docker.json(['image', 'inspect', image])
            require(canonical_image(image) in [canonical_image(x) for x in present[0].get('RepoDigests', [])], '이미지 digest를 확인하지 못했습니다.')
    # Only the two dedicated ClickHouse directory roots require initial non-root ownership.
    # Never traverse/chown existing data recursively; inode bindings are checked above.
    if not any(x['Config']['Labels']['com.docker.compose.service'] == 'lf-clickhouse' for x in items):
        docker.run(['run', '--rm', '--pull', 'never', '--network', 'none', '--user', '0:0', '--entrypoint', '/bin/sh',
                    '--mount', 'type=bind,source=' + str(root / 'data/clickhouse') + ',target=/ch-data',
                    '--mount', 'type=bind,source=' + str(root / 'data/clickhouse-logs') + ',target=/ch-logs',
                    IMAGES['lf-clickhouse'], '-ec', 'chown 101:101 /ch-data /ch-logs'])
    print('Langfuse 전용 서비스 6개를 시작하고 초기 프로젝트를 확인합니다.', flush=True)
    try:
        docker.run(compose(s, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '300'), timeout=360)
    except DockerFailure as exc:
        # Report only; do not retry/restart anything or change the existing state.
        from diagnose import report
        try:
            print(json.dumps(report(docker, root, failure=exc), ensure_ascii=False, indent=2), flush=True)
        except (ValueError, OSError, KeyError, subprocess.SubprocessError):
            print('자동 진단을 완료하지 못했습니다. manage.py diagnose를 실행하세요.', file=sys.stderr)
        raise
    result = check(docker, s)
    result['capacity'] = capacity
    result['domain_services_restarted'] = False
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'up', 'check', 'status', 'diagnose', 'repair-health', 'stop'])
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--public-url')
    parser.add_argument('--wait-seconds', type=int, default=120)
    args = parser.parse_args(argv)
    require(0 <= args.wait_seconds <= 600, '--wait-seconds는 0~600입니다.')
    require(args.public_url is None or args.action in ('prepare', 'up'), '--public-url은 prepare/up에서만 사용합니다.')
    docker = Docker()
    if args.action == 'diagnose':
        from diagnose import report
        print(json.dumps(report(docker, args.state_dir), ensure_ascii=False, indent=2))
        return 0
    host = host_info(docker)
    if args.action in ('prepare', 'up', 'repair-health', 'stop'):
        with operation_lock():
            if args.action == 'repair-health':
                from health_repair import repair
                s = load(args.state_dir, host)
                result = repair(docker, s, args.wait_seconds)
            elif args.action == 'stop':
                s = load(args.state_dir, host)
                items = owned_containers(docker, s['owner'])
                for item in items:
                    inspect_binding(item, s)
                if items:
                    docker.run(['stop', '--time', '60', *[x['Id'] for x in items]])
                result = {'result': 'stopped', 'containers': len(items), 'data_preserved': True}
            else:
                s = prepare(docker, args.state_dir, host, args.public_url)
                result = up(docker, s, host) if args.action == 'up' else {
                    'result': 'prepared', 'version': VERSION, 'state_dir': s['state_dir'], 'web_url': s['public_url'],
                    'secrets_printed': False, 'containers_started': False, 'inference_executed': False}
    else:
        s = load(args.state_dir, host)
        if args.action == 'check':
            result = check(docker, s, args.wait_seconds)
        else:
            items = owned_containers(docker, s['owner'])
            for item in items:
                inspect_binding(item, s)
            result = {'result': 'read', 'services': {x['Config']['Labels']['com.docker.compose.service']:
                       {'state': x['State']['Status'], 'health': x['State'].get('Health', {}).get('Status')} for x in items},
                      'web_url': s['public_url'], 'inference_executed': False, 'changes_made': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('중단했습니다. 상태/비밀/데이터를 보존하고 같은 명령으로 재개하세요.', file=sys.stderr)
        sys.exit(130)
