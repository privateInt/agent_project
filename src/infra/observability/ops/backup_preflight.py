#!/usr/bin/env python3
"""Read-only observability storage inventory. Never a backup/restore executor."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import errno
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
PROJECT_ROOT = Path(__file__).resolve().parents[4]
MAX_JSON = 1024 * 1024
IDENT = re.compile(r'(?:sha256:)?[0-9a-f]{64}')
STORES = {'postgres': '/var/lib/postgresql/data', 'clickhouse': '/var/lib/clickhouse',
          'clickhouse-logs': '/var/log/clickhouse-server', 'redis': '/data', 'minio': '/data'}
PROJECTS = {'prometheus': 'infrastructure-observability',
            'alertmanager': 'infrastructure-observability-alerts',
            'langfuse': 'infrastructure-langfuse', 'ops': 'agent-project-ops'}
OWNERS = {'prometheus': 'org.agent-project.observability.owner',
          'alertmanager': 'org.agent-project.observability.owner',
          'langfuse': 'org.agent-project.langfuse.owner', 'ops': 'org.agent-project.ops.owner'}
INFO_FORMAT = '[' + ','.join('{{json .' + x + '}}' for x in
                           ('DockerRootDir', 'Name', 'Architecture', 'OSType', 'SecurityOptions')) + ']'
IMAGE_FORMAT = '[{{json .Id}},{{json .RepoDigests}},{{json .Architecture}},{{json .Size}}]'


def container_format(component):
    fields = ['.Id', '.Image', '.Config.Image', '.State.Status', '.State.StartedAt', '.Config.User',
              '(index .Config.Labels "com.docker.compose.project")',
              '(index .Config.Labels "com.docker.compose.service")',
              '(index .Config.Labels "' + OWNERS[component] + '")', '.Mounts']
    return '[' + ','.join('{{json ' + x + '}}' for x in fields) + ']'


class Unavailable(Exception):
    pass


def require(ok, code):
    if not ok:
        raise Unavailable(code)


def code(exc):
    if isinstance(exc, Unavailable):
        return str(exc)
    if isinstance(exc, OSError):
        return {errno.ENOENT: 'MISSING', errno.EACCES: 'PERMISSION_DENIED',
                errno.EPERM: 'PERMISSION_DENIED', errno.ELOOP: 'SYMLINK_REFUSED',
                errno.ENOTDIR: 'NOT_DIRECTORY_OR_SYMLINK'}.get(exc.errno, 'OS_ERROR')
    return 'INVALID_CONFIGURATION_OR_RESPONSE'


def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def safe(path):
    path = Path(path).expanduser()
    require(path.is_absolute() and '..' not in path.parts and
            not any(ord(c) < 32 or ord(c) == 127 for c in str(path)), 'INVALID_PATH')
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'SYMLINK_REFUSED')
    return path


def open_directory(path):
    """Open every component without following links, including during a rename race."""
    path = safe(path)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def metadata(path, private=False, directory=False):
    path = safe(path)
    if path == Path('/'):
        require(directory and not private, 'INVALID_ROOT_ROLE')
        return path.stat()
    fd = open_directory(path.parent)
    try:
        st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    require((stat.S_ISDIR if directory else stat.S_ISREG)(st.st_mode), 'WRONG_FILE_TYPE')
    if private:
        require(st.st_uid == os.getuid() and not st.st_mode & 0o077, 'PRIVATE_OWNER_MODE_MISMATCH')
    return st


def read_small(path, private=True, json_value=True):
    path = safe(path)
    parent = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        require(stat.S_ISREG(st.st_mode) and st.st_size <= MAX_JSON, 'INVALID_CONFIG_FILE')
        if private:
            require(st.st_uid == os.getuid() and not st.st_mode & 0o077, 'PRIVATE_OWNER_MODE_MISMATCH')
        raw = f.read(MAX_JSON + 1)
        require(len(raw) <= MAX_JSON, 'CONFIG_TOO_LARGE')
    text = raw.decode('utf-8')
    return json.loads(text) if json_value else text


class DockerReadOnly:
    """Only these fixed metadata projections may reach subprocess. No raw inspect/env."""
    def __init__(self):
        self.calls = 0
        self.endpoint = None
        self.env = {k: v for k, v in os.environ.items() if k not in
                    ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_API_VERSION', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')
                    and not k.startswith('COMPOSE_')}

    def _command(self, args, bootstrap=False):
        allowed = ((args == ['context', 'show']) or
                   (len(args) == 5 and args[:2] == ['context', 'inspect'] and args[3:] == ['--format', '{{.Endpoints.docker.Host}}']) or
                   (args == ['info', '--format', INFO_FORMAT]) or
                   (len(args) == 5 and args[:4] == ['ps', '-aq', '--no-trunc', '--filter'] and
                    args[4] in ['label=com.docker.compose.project=' + p for p in PROJECTS.values()]) or
                   (len(args) == 5 and
                    ((args[:3] == ['container', 'inspect', '--format'] and args[3] in {container_format(c) for c in PROJECTS}) or
                     (args[:3] == ['image', 'inspect', '--format'] and args[3] == IMAGE_FORMAT)) and
                    IDENT.fullmatch(args[4]) is not None))
        require(allowed and not any('.Env' in a for a in args), 'READ_ONLY_DOCKER_COMMAND_REQUIRED')
        require(bootstrap or self.endpoint is not None, 'LOCAL_DOCKER_NOT_VERIFIED')
        prefix = [] if bootstrap else ['--host', self.endpoint]
        self.calls += 1
        try:
            r = subprocess.run(['docker', *prefix, *args], capture_output=True, text=True,
                               env=self.env, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Unavailable('DOCKER_READ_UNAVAILABLE') from None
        require(r.returncode == 0 and len(r.stdout) <= 4 * MAX_JSON, 'DOCKER_READ_FAILED')
        return r.stdout.strip()

    def host(self):
        require(not os.environ.get('DOCKER_API_VERSION'), 'DOCKER_API_OVERRIDE_REFUSED')
        context = os.environ.get('DOCKER_CONTEXT') or self._command(['context', 'show'], True)
        require(re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', context) is not None, 'INVALID_DOCKER_CONTEXT')
        endpoint = self._command(['context', 'inspect', context, '--format', '{{.Endpoints.docker.Host}}'], True)
        if not os.environ.get('DOCKER_CONTEXT') and os.environ.get('DOCKER_HOST'):
            endpoint = os.environ['DOCKER_HOST']
        require(endpoint in ('unix:///var/run/docker.sock', 'unix:///run/docker.sock'), 'LOCAL_DOCKER_REQUIRED')
        self.endpoint = endpoint
        root, name, arch, system, security = json.loads(self._command(['info', '--format', INFO_FORMAT]))
        require(system == 'linux' and not any('rootless' in x for x in security or []), 'ROOTFUL_LINUX_REQUIRED')
        return {'docker_root': str(safe(root)), 'hostname': name, 'context': context,
                'architecture': arch, 'endpoint': endpoint}

    def containers(self, component):
        project = PROJECTS[component]
        ids = self._command(['ps', '-aq', '--no-trunc', '--filter', 'label=com.docker.compose.project=' + project]).split()
        require(len(ids) <= 16 and all(IDENT.fullmatch(x) for x in ids), 'INVALID_CONTAINER_IDS')
        fmt = container_format(component)
        rows = []
        for ident in ids:
            values = json.loads(self._command(['container', 'inspect', '--format', fmt, ident]))
            rows.append(dict(zip(('id', 'image_id', 'image_ref', 'state', 'started_at', 'user',
                                  'project', 'service', 'owner', 'mounts'), values)))
        return rows

    def image(self, ident):
        require(IDENT.fullmatch(ident) is not None, 'INVALID_IMAGE_ID')
        value = json.loads(self._command(['image', 'inspect', '--format', IMAGE_FORMAT, ident]))
        return dict(zip(('id', 'repo_digests', 'architecture', 'size_bytes'), value))


def mount_table(text):
    rows = []
    unescape = lambda s: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), s)
    for line in text.splitlines():
        a, b = line.split(' - ', 1)
        left, right = a.split(), b.split()
        rows.append({'device': left[2], 'mountpoint': unescape(left[4]), 'fstype': right[0],
                     'source': unescape(right[1]), 'readonly': 'ro' in left[5].split(',') or 'ro' in right[2].split(',')})
    return rows


def physical_disks(device, sysroot=Path('/sys')):
    """Resolve Linux block partitions/slaves; unknown devices never imply independence."""
    def visit(dev, seen):
        require(dev not in seen and len(seen) < 32, 'UNKNOWN_BLOCK_TOPOLOGY')
        seen = seen | {dev}
        link = sysroot / 'dev/block' / dev
        require(link.exists(), 'UNKNOWN_BLOCK_TOPOLOGY')
        p = link.resolve()
        if (p / 'partition').exists():
            p = p.parent
        slaves = list((p / 'slaves').iterdir()) if (p / 'slaves').is_dir() else []
        if slaves:
            result = set()
            for child in slaves:
                result |= visit((child / 'dev').read_text().strip(), seen)
            return result
        require('virtual' not in p.parts and (p / 'device').exists(), 'UNKNOWN_BLOCK_TOPOLOGY')
        return {p.name}
    try:
        return sorted(visit(device, set()))
    except (Unavailable, OSError, ValueError):
        return None


def filesystem(path, mounts):
    path = safe(path)
    st = path.stat()
    device = str(os.major(st.st_dev)) + ':' + str(os.minor(st.st_dev))
    matching = [m for m in mounts if m['device'] == device and path.is_relative_to(m['mountpoint'])]
    require(bool(matching), 'MOUNT_NOT_IDENTIFIED')
    row = dict(max(matching, key=lambda m: len(m['mountpoint'])))
    v = os.statvfs(path)
    row.update(available_bytes=v.f_bavail * v.f_frsize, total_bytes=v.f_blocks * v.f_frsize,
               physical_disks=physical_disks(device))
    return row


def measure(path, mounts, seconds=15, entries=200000, exclude_children=()):
    """Metadata only. No file contents, followed links, foreign mounts, or writes."""
    started = time.monotonic()
    counts = Counter(files=0, directories=0, links=0, hardlink_duplicates=0, visited=0, excluded_children=0)
    errors = Counter()
    logical = allocated = 0
    seen = set()
    boundaries = {Path(m['mountpoint']) for m in mounts}

    def walk(fd, current, device, depth):
        nonlocal logical, allocated
        if depth > 128:
            errors['DEPTH_LIMIT'] += 1
            return
        counts['directories'] += 1
        try:
            with os.scandir(fd) as scan:
                for entry in scan:
                    if counts['visited'] >= entries or time.monotonic() - started >= seconds:
                        errors['SCAN_LIMIT'] += 1
                        return
                    counts['visited'] += 1
                    try:
                        if depth == 0 and entry.name in exclude_children:
                            counts['excluded_children'] += 1
                            continue
                        st = entry.stat(follow_symlinks=False)
                        child = current / entry.name
                        if stat.S_ISLNK(st.st_mode):
                            counts['links'] += 1
                            errors['SYMLINK_NOT_FOLLOWED'] += 1
                        elif st.st_dev != device or child in boundaries:
                            errors['NESTED_MOUNT_NOT_SCANNED'] += 1
                        elif stat.S_ISDIR(st.st_mode):
                            cfd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            try:
                                actual = os.fstat(cfd)
                                if (actual.st_dev, actual.st_ino) != (st.st_dev, st.st_ino):
                                    errors['DIRECTORY_CHANGED'] += 1
                                else:
                                    walk(cfd, child, device, depth + 1)
                            finally:
                                os.close(cfd)
                        elif stat.S_ISREG(st.st_mode):
                            ident = (st.st_dev, st.st_ino)
                            if ident in seen:
                                counts['hardlink_duplicates'] += 1
                            else:
                                seen.add(ident)
                                counts['files'] += 1
                                logical += st.st_size
                                allocated += st.st_blocks * 512
                        else:
                            errors['SPECIAL_FILE_NOT_SCANNED'] += 1
                    except OSError as exc:
                        errors[code(exc)] += 1
        except OSError as exc:
            errors[code(exc)] += 1

    try:
        fd = open_directory(path)
        try:
            walk(fd, safe(path), os.fstat(fd).st_dev, 0)
        finally:
            os.close(fd)
    except (OSError, Unavailable) as exc:
        errors[code(exc)] += 1
    complete = not errors
    return {'status': 'complete' if complete else 'partial' if counts['files'] else 'unavailable',
            'logical_bytes': logical if complete else None, 'allocated_bytes': allocated if complete else None,
            'observed_logical_bytes': logical, 'observed_allocated_bytes': allocated,
            'counts': dict(counts), 'issues': dict(errors), 'elapsed_seconds': round(time.monotonic() - started, 3),
            'consistent_snapshot': False, 'scope': 'live_regular_file_metadata_excludes_directory_overhead'}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComposeInventory:
    """Docker Compose adapter. Kubernetes inventory must use its own API/PVC adapter."""
    def __init__(self, root, lf_root, ops_root, docker, mounts, seconds=15, entries=200000):
        self.root, self.lf_root, self.ops_root = map(safe, (root, lf_root, ops_root))
        self.obs = self.root / 'src/infra/observability'
        self.docker, self.mounts, self.seconds, self.entries = docker, mounts, seconds, entries
        self.files, self.stores, self.services, self.components = [], [], {}, {}
        self.host = None

    def file(self, name, path, private=True, optional=False, credential_reference=False):
        row = {'name': name, 'path': str(safe(path)), 'required': not optional}
        try:
            st = metadata(path, private and not credential_reference)
            if credential_reference:
                require(st.st_uid == os.getuid(), 'CREDENTIAL_OWNER_MISMATCH')
                if st.st_mode & 0o077:
                    require(stat.S_IMODE(st.st_mode) == 0o444 and st.st_nlink == 1,
                            'CREDENTIAL_PROTECTION_UNVERIFIED')
                    parent = metadata(Path(path).parent, private=True, directory=True)
                    require(stat.S_IMODE(parent.st_mode) == 0o700, 'CREDENTIAL_PARENT_NOT_PRIVATE')
                    row['protection'] = 'readonly_file_in_private_parent'
                else:
                    row['protection'] = 'private_file'
            row.update(status='ok', uid=st.st_uid, gid=st.st_gid, mode=oct(stat.S_IMODE(st.st_mode)), bytes=st.st_size,
                       metadata_signature=[st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns])
        except (OSError, ValueError, Unavailable) as exc:
            row.update(status='unavailable', error=code(exc))
        self.files.append(row)
        return row

    def store(self, name, path, service, target, optional=False):
        self.stores.append({'name': name, 'path': str(safe(path)), 'service': service,
                            'container_target': target, 'optional_diagnostic': optional})

    def service(self, name, component, owner, image, mounts, image_id=None, user=None):
        self.services[name] = dict(component=component, owner=owner, image=image,
                                   mounts=mounts, image_id=image_id, user=user)

    def load_prometheus(self):
        s = read_small(self.obs / '.state.json')
        self.file('observability-state', self.obs / '.state.json')
        op = load_module('_preflight_obs_manage', self.obs / 'ops/manage.py')
        op.validate_state(s, self.host)
        require(read_small(self.obs / '.env', json_value=False) == op.environment(s), 'PROMETHEUS_ENV_DRIFT')
        self.file('observability-env', self.obs / '.env')
        data = safe(s['data_dir'])
        self.file('observability-owner', data.parent / '.owner.json')
        self.store('prometheus', data, 'obs-prometheus', '/prometheus')
        self.store('observability-records', data.parent, None, None, optional=True)
        self.stores[-1]['exclude_children'] = ['prometheus', 'alertmanager']
        image = s['images']['obs-prometheus']
        self.service('obs-prometheus', 'prometheus', s['config_id'], image['digest'], {
            '/prometheus': (str(data), True),
            '/etc/prometheus/prometheus.yml': (str(self.obs / 'alerts/prometheus.yml'), False),
            '/etc/prometheus/rules/o04.rules.yml': (str(self.obs / 'alerts/o04.rules.yml'), False)},
            image['image_id'], str(s['uid']) + ':' + str(s['gid']))
        for name in ('prometheus/docker-compose.yml', 'alerts/prometheus-overlay.yml', 'alerts/prometheus.yml',
                     'alerts/o04.rules.yml', 'gpu/.state.json', 'gpu/.env'):
            self.file(name, self.obs / name, private='/.' in name)
        self.prom = s

    def load_alertmanager(self):
        require(hasattr(self, 'prom'), 'PROMETHEUS_CONFIGURATION_UNAVAILABLE')
        s = read_small(self.obs / 'alerts/.state.json')
        require(s['schema'] == 1 and s['enabled'] is True and s['config_id'] == self.prom['config_id'], 'ALERTMANAGER_STATE_DRIFT')
        data = safe(s['data_dir'])
        require(data == Path(self.prom['data_dir']).parent / 'alertmanager', 'ALERTMANAGER_STORAGE_DRIFT')
        metadata(data, private=True, directory=True)
        require(read_small(data / '.owner.json') == {'config_id': s['config_id']}, 'ALERTMANAGER_OWNER_DRIFT')
        require(s['image']['tag'] == 'quay.io/prometheus/alertmanager:v0.32.2' and
                re.fullmatch(r'quay.io/prometheus/alertmanager@sha256:[0-9a-f]{64}', s['image']['digest']), 'ALERTMANAGER_IMAGE_DRIFT')
        values = {'OBS_ALERTS_IMAGE': s['image']['digest'], 'OBS_CONFIG_ID': s['config_id'],
                  'OBS_UID': self.prom['uid'], 'OBS_GID': self.prom['gid'], 'OBS_ALERTS_DATA': s['data_dir']}
        require(read_small(self.obs / 'alerts/.env', json_value=False) ==
                ''.join(k + "='" + str(v) + "'\n" for k, v in values.items()), 'ALERTMANAGER_ENV_DRIFT')
        for name in ('alerts/.state.json', 'alerts/.env', 'alerts/alertmanager.yml', 'alerts/docker-compose.yml'):
            self.file(name, self.obs / name, private='/.' in name)
        self.file('alertmanager-owner', data / '.owner.json')
        self.store('alertmanager', data, 'obs-alertmanager', '/alertmanager')
        self.service('obs-alertmanager', 'alertmanager', s['config_id'], s['image']['digest'], {
            '/alertmanager': (str(data), True), '/etc/alertmanager/alertmanager.yml':
            (str(self.obs / 'alerts/alertmanager.yml'), False)}, s['image']['image_id'],
            str(self.prom['uid']) + ':' + str(self.prom['gid']))

    def load_langfuse(self):
        # Existing validation is read-only: private env equality, host, owner, inode/device.
        folder = self.root / 'src/infra/langfuse/ops'
        sys.path.insert(0, str(folder))
        try:
            module = load_module('_preflight_lf_manage', folder / 'manage.py')
            module.read_json = lambda path: read_small(path)
            module.private_read = lambda path: read_small(path, json_value=False)
            s = module.load(self.lf_root, self.host)
        finally:
            sys.path.pop(0)
        for name in ('settings.json', 'owner.json', 'compose.env', 'postgres.env', 'clickhouse.env',
                     'redis.env', 'minio.env', 'worker.env', 'web.env'):
            self.file('langfuse-' + name, self.lf_root / name)
        for name in ('images.lock.json', 'docker-compose.yml'):
            self.file('langfuse-' + name, self.root / 'src/infra/langfuse' / name, private=False)
        for name, target in STORES.items():
            service = 'lf-clickhouse' if name == 'clickhouse-logs' else 'lf-' + name
            self.store('langfuse-' + name, self.lf_root / 'data' / name, service, target, name == 'clickhouse-logs')
        for service, image in s['images'].items():
            mounts = {row['container_target']: (row['path'], True) for row in self.stores if row['service'] == service}
            self.service(service, 'langfuse', s['owner'], image, mounts)
        self.store('langfuse-records', self.lf_root, None, None, optional=True)
        self.stores[-1]['exclude_children'] = ['data']

    def load_ops(self):
        root = self.ops_root
        metadata(root, private=True, directory=True)
        owner = read_small(root / 'owner.json')
        cfg = read_small(root / 'host-config.json')
        require(owner['schema'] == cfg['schema'] == 1 and str(uuid.UUID(owner['owner'])) == owner['owner'] and
                owner['uid'] == os.getuid() and owner['gid'] == os.getgid() and
                owner['project_root'] == str(self.root), 'OPS_OWNER_DRIFT')
        expected = json.loads(json.dumps(cfg))
        expected.update(langfuse_file='/run/ops/langfuse.json', access_file='/run/ops/access-key',
                        state_file='/var/lib/ops/api-success.json')
        expected['platforms']['agent']['token_file'] = '/run/ops/agent-token'
        require(read_small(root / 'config.json') == expected, 'OPS_HOST_CONTAINER_CONFIG_DRIFT')
        require(cfg['access_file'] == str(root / 'access-key') and cfg['state_file'] == str(root / 'cache/api-success.json'), 'OPS_STATE_PATH_DRIFT')
        require(not cfg.get('tls_cert') and not cfg.get('tls_key'), 'CUSTOM_TLS_INVENTORY_REQUIRED')
        agent, lf = safe(cfg['platforms']['agent']['token_file']), safe(cfg['langfuse_file'])
        env = {'OPS_UID': str(owner['uid']), 'OPS_GID': str(owner['gid']), 'OPS_OWNER': owner['owner'],
               'OPS_CONFIG_FILE': str(root / 'config.json'), 'OPS_ACCESS_FILE': cfg['access_file'],
               'OPS_AGENT_TOKEN_FILE': str(agent), 'OPS_LANGFUSE_FILE': str(lf), 'OPS_CACHE_DIR': str(root / 'cache')}
        require(read_small(root / 'compose.env', json_value=False) == ''.join(k + "='" + v + "'\n" for k, v in env.items()), 'OPS_COMPOSE_ENV_DRIFT')
        for name in ('owner.json', 'host-config.json', 'config.json', 'compose.env', 'access-key', 'cache/api-success.json'):
            self.file('ops-' + name, root / name)
        self.file('agent-token-reference', agent, credential_reference=True)
        self.file('langfuse-key-reference', lf, credential_reference=True)
        self.file('ops-compose', self.root / 'src/ops/platform/docker-compose.yml', private=False)
        self.store('ops-server', root, None, None)
        self.stores[-1]['runtime_subdirectory'] = {'path': str(root / 'cache'), 'service': 'ops-web', 'target': '/var/lib/ops'}
        self.service('ops-web', 'ops', owner['owner'], 'agent-project-ops:o04.3', {
            '/run/ops/config.json': (str(root / 'config.json'), False), '/run/ops/access-key': (cfg['access_file'], False),
            '/run/ops/agent-token': (str(agent), False), '/run/ops/langfuse.json': (str(lf), False),
            '/var/lib/ops': (str(root / 'cache'), True)}, user=str(owner['uid']) + ':' + str(owner['gid']))

    def containers(self):
        result = {}
        for component in PROJECTS:
            try:
                items = self.docker.containers(component)
                allowed_extras = {'obs-node-exporter', 'obs-cadvisor'} if component == 'prometheus' else set()
                require(all(x['service'] in self.services or x['service'] in allowed_extras for x in items), 'UNEXPECTED_PROJECT_CONTAINER')
                relevant = [x for x in items if x['service'] in self.services and self.services[x['service']]['component'] == component]
                expected = [name for name, s in self.services.items() if s['component'] == component]
                require(sorted(x['service'] for x in relevant) == sorted(expected), 'CONTAINER_SET_MISMATCH')
                for x in relevant:
                    s = self.services[x['service']]
                    row = {'id': x['id'], 'image_id': x['image_id'], 'state': x['state'], 'started_at': x['started_at'], 'status': 'unavailable'}
                    result[x['service']] = row
                    require(x['owner'] == s['owner'] and x['project'] == PROJECTS[component], 'CONTAINER_OWNER_MISMATCH')
                    require(x['state'] == 'running', 'CONTAINER_NOT_RUNNING')
                    # Docker normalizes docker.io references; both spellings identify the same repository.
                    canonical = lambda value: value.removeprefix('docker.io/')
                    require(canonical(x['image_ref']) == canonical(s['image']), 'IMAGE_REFERENCE_MISMATCH')
                    require(not s['image_id'] or x['image_id'] == s['image_id'], 'IMAGE_ID_MISMATCH')
                    require(s['user'] is None or x['user'] == s['user'], 'CONTAINER_USER_MISMATCH')
                    require(all(m['Destination'] == '/tmp' and component != 'langfuse'
                                for m in x['mounts'] if m['Type'] == 'tmpfs'), 'UNEXPECTED_TMPFS_MOUNT')
                    mounts = [m for m in x['mounts'] if m['Type'] != 'tmpfs']
                    require(all(m['Type'] == 'bind' for m in mounts) and len(mounts) == len(s['mounts']) and
                            {m['Destination']: (m['Source'], m['RW']) for m in mounts} == s['mounts'], 'CONTAINER_MOUNT_MISMATCH')
                    image = self.docker.image(x['image_id'])
                    require(image['id'] == x['image_id'], 'IMAGE_ID_MISMATCH')
                    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(self.host['architecture'], self.host['architecture'])
                    require(image['architecture'] == arch, 'IMAGE_ARCHITECTURE_MISMATCH')
                    if '@sha256:' in s['image']:
                        require(canonical(s['image']) in [canonical(d) for d in image['repo_digests'] or []], 'IMAGE_DIGEST_UNAVAILABLE')
                    row.update(status='matched', user=x['user'], image=image,
                               local_image_export_candidate=component == 'ops')
            except (OSError, ValueError, KeyError, TypeError, Unavailable) as exc:
                result[component + '_issue'] = {'status': 'unavailable', 'error': code(exc)}
        return result

    def destination(self, path):
        row = {'path': str(path), 'selected': False, 'write_probe_executed': False}
        try:
            path = safe(path)
            st = metadata(path, directory=True)
            fs = filesystem(path, self.mounts)
            relations = {}
            source_overlap = path.is_relative_to(self.host['docker_root']) or Path(self.host['docker_root']).is_relative_to(path)
            for source in self.stores:
                src = Path(source['path'])
                source_overlap |= path.is_relative_to(src) or src.is_relative_to(path)
                other = source.get('filesystem', {})
                if not other:
                    relation = 'unknown'
                elif other['device'] == fs['device']:
                    relation = 'same_filesystem'
                elif not other.get('physical_disks') or not fs['physical_disks']:
                    relation = 'unknown'
                elif set(other['physical_disks']) & set(fs['physical_disks']):
                    relation = 'same_physical_disk'
                else:
                    relation = 'different_block_devices_same_host'
                relations[source['name']] = relation
            row.update(status='observed', filesystem=fs, uid=st.st_uid, gid=st.st_gid,
                       mode=oct(stat.S_IMODE(st.st_mode)), write_access_advisory=os.access(path, os.W_OK | os.X_OK),
                       source_overlap=source_overlap, source_relations=relations,
                       archive_encryption_required=True, restore_directory_suitable='unverified',
                       capacity_sufficient=None)
            try:
                docker_fs = filesystem(self.host['docker_root'], self.mounts)
                row['shares_docker_filesystem'] = fs['device'] == docker_fs['device']
            except (OSError, Unavailable, ValueError):
                row['shares_docker_filesystem'] = None
        except (OSError, ValueError, Unavailable) as exc:
            row.update(status='unavailable', error=code(exc))
        return row

    def run(self, destinations):
        report = {'schema': 1, 'stage': 'observability-backup-preflight', 'started_at': stamp(),
                  'backup_executed': False, 'restore_executed': False, 'service_control_requested': False,
                  'production_api_requests': 0, 'inference_requested': False, 'filesystem_writes_requested': 0,
                  'destination_selected': False, 'backup_ready': False,
                  'inventory_provider': 'docker-compose', 'resource_scope': 'execution_host',
                  'kubernetes_inventory_supported': False}
        self.host = self.docker.host()
        report['host'] = self.host
        try:
            mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
            report['host']['memory_total_bytes'] = int(mem['MemTotal'].split()[0]) * 1024
            report['host']['memory_available_bytes'] = int(mem['MemAvailable'].split()[0]) * 1024
        except (OSError, KeyError, ValueError):
            report['host']['memory_available_bytes'] = None
        for component in PROJECTS:
            try:
                getattr(self, 'load_' + component)()
                self.components[component] = {'status': 'matched'}
            except Exception as exc:
                # Existing guards include values in their messages; never forward their text.
                self.components[component] = {'status': 'unavailable', 'error': code(exc)}
        report['components'] = self.components
        report['containers'] = self.containers()
        for row in self.stores:
            try:
                st = metadata(row['path'], directory=True)
                row['root_metadata'] = {'uid': st.st_uid, 'gid': st.st_gid, 'mode': oct(stat.S_IMODE(st.st_mode)),
                                        'device': st.st_dev, 'inode': st.st_ino}
                row['filesystem'] = filesystem(row['path'], self.mounts)
                extra = row.get('exclude_children', [])
                row['size'] = measure(row['path'], self.mounts, self.seconds, self.entries, extra)
                # Deliberate exclusions of separately measured data are not missing data.
                if extra:
                    row['size']['scope'] = 'records_and_configs_only_data_children_excluded'
            except (OSError, ValueError, Unavailable) as exc:
                row['size'] = {'status': 'unavailable', 'error': code(exc)}
        report['storage'] = self.stores
        report['required_files'] = self.files
        report['destinations'] = [self.destination(p) for p in destinations]
        storage_complete = bool(self.stores) and all(r['size']['status'] == 'complete' for r in self.stores)
        report['capacity'] = {'total_required_bytes': None, 'image_export_bytes': None,
                              'measured_store_logical_bytes': sum(r['size']['logical_bytes'] for r in self.stores) if storage_complete else None,
                              'measured_store_allocated_bytes': sum(r['size']['allocated_bytes'] for r in self.stores) if storage_complete else None,
                              'reason': 'live_sizes_not_archive_estimate_no_image_export_no_generation_or_workspace_reservation'}
        after = self.containers()
        expected = set(self.services)
        complete = (all(c['status'] == 'matched' for c in self.components.values()) and bool(expected) and
                    set(after) == set(report['containers']) == expected and
                    all(v.get('status') == 'matched' for v in [*after.values(), *report['containers'].values()]))
        report['container_metadata_stable_during_scan'] = after == report['containers'] if complete else None
        report['container_comparison_status'] = ('matched' if after == report['containers'] else 'changed') if complete else 'unavailable'
        changed = []
        for f in self.files:
            signature = f.pop('metadata_signature', None)
            if signature is None:
                continue
            try:
                st = metadata(f['path'])
                if signature != [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]:
                    changed.append(f['name'])
            except (OSError, Unavailable):
                changed.append(f['name'])
        report['files_changed_during_scan'] = changed
        report['scope_limits'] = ['metadata_inventory_not_consistent_backup', 'running_data_can_change',
                                  'unreadable_storage_not_zero', 'write_permission_advisory_only',
                                  'credentials_content_not_reported', 'no_api_health_or_restore_verification',
                                  'exporter_images_and_optional_cli_cache_not_in_core_nine_container_inventory']
        issues = any(v['status'] != 'matched' for v in self.components.values())
        issues |= any(k.endswith('_issue') for k in report['containers'])
        issues |= not report['container_metadata_stable_during_scan'] or bool(changed)
        issues |= any(f['required'] and f['status'] != 'ok' for f in self.files)
        issues |= any(r['size']['status'] != 'complete' for r in self.stores)
        issues |= any(d['status'] != 'observed' or d.get('source_overlap') or
                      not d.get('write_access_advisory') or d.get('filesystem', {}).get('readonly') for d in report['destinations'])
        report.update(result='attention_required' if issues else 'inventory_complete',
                      checked_at=stamp(), docker_metadata_commands=self.docker.calls)
        return report


def render(report):
    """Keep complete JSON, with one row per item for practical terminal copying."""
    dump = lambda value: json.dumps(value, ensure_ascii=False)
    lines = []
    for name, value in report.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            text = '[\n' + ',\n'.join('    ' + dump(x) for x in value) + '\n  ]'
        elif name == 'containers':
            text = '{\n' + ',\n'.join('    ' + dump(k) + ': ' + dump(v) for k, v in value.items()) + '\n  }'
        else:
            text = dump(value)
        lines.append('  ' + dump(name) + ': ' + text)
    return '{\n' + ',\n'.join(lines) + '\n}'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('docker-compose', 'kubernetes'), default='docker-compose')
    parser.add_argument('--destination', action='append', type=Path, default=[], help='Existing candidate directory; never selected or created.')
    parser.add_argument('--langfuse-state-dir', type=Path, default=Path.home() / '.local/state/agent-project-langfuse')
    parser.add_argument('--ops-state-dir', type=Path, default=Path.home() / '.local/state/agent-project-ops/server')
    parser.add_argument('--max-seconds-per-store', type=int, default=15)
    parser.add_argument('--max-entries-per-store', type=int, default=200000)
    args = parser.parse_args(argv)
    try:
        require(args.provider == 'docker-compose', 'KUBERNETES_INVENTORY_NOT_IMPLEMENTED')
        require(sys.platform == 'linux' and os.getuid() != 0, 'RUN_AS_ORIGINAL_NONROOT_USER_NO_SUDO')
        require(1 <= args.max_seconds_per_store <= 60 and 1 <= args.max_entries_per_store <= 1000000 and
                len(args.destination) <= 4, 'INVALID_SCAN_LIMIT')
        mounts = mount_table(Path('/proc/self/mountinfo').read_text())
        inventory = ComposeInventory(PROJECT_ROOT, args.langfuse_state_dir, args.ops_state_dir, DockerReadOnly(),
                              mounts, args.max_seconds_per_store, args.max_entries_per_store)
        report = inventory.run(args.destination)
    except Exception as exc:
        report = {'schema': 1, 'stage': 'observability-backup-preflight', 'result': 'unavailable', 'error': code(exc),
                  'backup_executed': False, 'restore_executed': False, 'service_control_requested': False,
                  'production_api_requests': 0, 'inference_requested': False, 'filesystem_writes_requested': 0}
    print(render(report))
    return 0 if report['result'] == 'inventory_complete' else 2


if __name__ == '__main__':
    raise SystemExit(main())
