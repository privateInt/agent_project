#!/usr/bin/env python3
"""Bounded isolated native maintenance; never attach to a production worker."""
from copy import deepcopy
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import docker_acceptance as isolated, service_acceptance as native
from ops_platform.ops import manage, worker_state as ws, refresh_worker as worker
from ops_platform.ops import service_lifecycle as service, restart_check as rc, lifecycle_state as local
from ops_platform.ops import boot_lifecycle as boot, refresh_discovery as refresh, set_public_url as change
from ops_platform.transport import strict_json, Transport
from ops_candidates.maintenance_intent_20260930 import maintenance as core

STAGE = 'O04-isolated-native-maintenance-acceptance'
KIND = 'isolated-native-maintenance'
ENTRY = 'ops_tools/maintenance_acceptance.py'
SPEC = 'ops_candidates/maintenance_intent_20260930/manifest.json'
AUXILIARY = {ENTRY, SPEC, 'ops_candidates/maintenance_intent_20260930/maintenance.py',
             'ops_candidates/boot_start_20260929/arming.py'}
LIFECYCLE_FD = None
LEGACY_CLEANUP_SHA256 = '230a29a6a037485bc6e900ce1aac465bbbb1a311fcd7a05b9d1be05573a6dc54'
RESOURCE_GROUPS = ('targets', 'host_cpu', 'host_memory', 'filesystems', 'container_cpu', 'container_memory', 'gpu')
DOCKER_OPERATIONS = {'compose_up', 'container_list', 'container_inspect', 'container_exec',
    'container_stop', 'container_start', 'container_remove', 'network_list',
    'network_inspect', 'network_remove', 'network_create', 'image_inspect', 'unknown'}
DOCKER_REASONS = {'ADDRESS_POOL_EXHAUSTED', 'HOST_PORT_BUSY', 'STORAGE_FULL',
    'CONTAINER_UNHEALTHY', 'NETWORK_SETUP_FAILED', 'DOCKER_ACCESS_DENIED',
    'DOCKER_CONNECTION_FAILED', 'IMAGE_UNAVAILABLE', 'MULTIPLE_SIGNATURES',
    'COMMAND_TIMEOUT', 'CLI_UNAVAILABLE', 'RESPONSE_LIMIT', 'CONTAINER_NOT_FOUND', 'UNKNOWN'}
FIXTURE_ROLES = set(isolated.ROLES) | {'manual_model', 'ops'}


def docker_operation(args):
    """Return fixed labels only; argv can contain private paths or payloads."""
    if args and args[0] == 'compose': return 'compose_up' if 'up' in args else 'unknown'
    if args and args[0] == 'ps': return 'container_list'
    if args and args[0] == 'inspect': return 'container_inspect'
    if args and args[0] == 'exec': return 'container_exec'
    pairs = {('container', 'inspect'): 'container_inspect', ('container', 'exec'): 'container_exec',
        ('container', 'stop'): 'container_stop', ('container', 'start'): 'container_start',
        ('container', 'rm'): 'container_remove', ('network', 'ls'): 'network_list',
        ('network', 'inspect'): 'network_inspect', ('network', 'rm'): 'network_remove', ('network', 'create'): 'network_create',
        ('image', 'inspect'): 'image_inspect'}
    return pairs.get(tuple(args[:2]), 'unknown')


def docker_reason(stdout, stderr):
    """Classify bounded text without returning/storing the raw CLI output."""
    patterns = {
        'ADDRESS_POOL_EXHAUSTED': ('all predefined address pools have been fully subnetted',
            'could not find an available, non-overlapping ipv4 address pool'),
        'HOST_PORT_BUSY': ('port is already allocated', 'address already in use'),
        'STORAGE_FULL': ('no space left on device', 'disk quota exceeded'),
        'CONTAINER_UNHEALTHY': (' is unhealthy', 'dependency failed to start'),
        'NETWORK_SETUP_FAILED': ('failed to create network', 'failed to set up container networking'),
        'DOCKER_ACCESS_DENIED': ('permission denied', 'access is denied'),
        'DOCKER_CONNECTION_FAILED': ('cannot connect to the docker daemon',
            'error during connect', 'connection refused'),
        'IMAGE_UNAVAILABLE': ('no such image', 'image not found'),
        'CONTAINER_NOT_FOUND': ('no such object:', 'no such container:'),
    }
    parts = [x.decode('utf-8', 'replace') if isinstance(x, bytes) else x if isinstance(x, str) else ''
             for x in (stdout, stderr)]
    text = '\n'.join(x[:32768].lower() for x in parts)
    matches = {name for name, phrases in patterns.items() if any(p in text for p in phrases)}
    # Creation/networking wrappers do not conceal a more specific cause.
    if len(matches) > 1: matches.discard('NETWORK_SETUP_FAILED')
    reason = next(iter(matches)) if len(matches) == 1 else 'MULTIPLE_SIGNATURES' if matches else 'UNKNOWN'
    return reason, any(len(x) > 32768 for x in parts)


def safe_docker_diagnostic(value):
    required = {'operation', 'outcome', 'reason', 'exit_code', 'text_truncated', 'phase', 'fixture_role'}
    if not isinstance(value, dict) or set(value) != required: return None
    if not isinstance(value['operation'], str) or value['operation'] not in DOCKER_OPERATIONS: return None
    if not isinstance(value['reason'], str) or value['reason'] not in DOCKER_REASONS: return None
    if not isinstance(value['outcome'], str) or value['outcome'] not in ('failed', 'timeout', 'unavailable', 'response_limit'): return None
    if value['exit_code'] is not None and not (type(value['exit_code']) is int and -255 <= value['exit_code'] <= 255): return None
    if type(value['text_truncated']) is not bool: return None
    if not isinstance(value['phase'], str) or value['phase'] not in ('fixture_preparation', 'cleanup', 'metadata_or_test_operation'): return None
    if value['fixture_role'] is not None and (not isinstance(value['fixture_role'], str) or value['fixture_role'] not in FIXTURE_ROLES): return None
    return deepcopy(value)


def resource_summary(body):
    """Only bounded structural evidence; never labels, queries, values or URLs."""
    rows = body.get('metrics') if isinstance(body, dict) else None
    require(isinstance(rows, dict), 'MAINTENANCE_RESOURCE_DIAGNOSTIC_INVALID')
    summary = {}
    for name in RESOURCE_GROUPS:
        row = rows.get(name)
        if not isinstance(row, dict):
            summary[name] = {'status': 'missing', 'count': None, 'coverage': None, 'error_code': None}
            continue
        data = row.get('data') if isinstance(row.get('data'), dict) else {}
        status = row.get('status'); count = data.get('count'); coverage = data.get('coverage')
        code = row.get('error', {}).get('code') if isinstance(row.get('error'), dict) else None
        summary[name] = {'status': status if status in ('ok', 'error', 'partial') else 'invalid',
            'count': count if type(count) is int and 0 <= count <= 1000000 else None,
            'coverage': coverage if coverage in ('query_result', 'truncated') else None,
            'error_code': code if isinstance(code, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,79}', code) else None}
    return summary


def safe_summary(value):
    if not isinstance(value, dict) or set(value) != set(RESOURCE_GROUPS): return None
    for row in value.values():
        if not isinstance(row, dict) or set(row) != {'status', 'count', 'coverage', 'error_code'}: return None
        if row['status'] not in ('ok', 'error', 'partial', 'missing', 'invalid'): return None
        if row['count'] is not None and not (type(row['count']) is int and 0 <= row['count'] <= 1000000): return None
        if row['coverage'] not in (None, 'query_result', 'truncated'): return None
        code = row['error_code']
        if code is not None and not (isinstance(code, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,79}', code)): return None
    return deepcopy(value)


def error_report(exc):
    value = native.error_report(exc)
    value['stage'] = STAGE
    summary = safe_summary(getattr(exc, 'resource_readback', None))
    if summary is not None: value['resource_readback'] = summary
    if getattr(exc, 'failure_phase', None) in core.PHASES: value['failure_phase'] = exc.failure_phase
    diagnostic = safe_docker_diagnostic(getattr(exc, 'docker_diagnostic', None))
    if diagnostic is not None: value['docker_diagnostic'] = diagnostic
    progress = getattr(exc, 'cleanup_progress', None)
    if isinstance(progress, dict) and set(progress) == {'removed_containers', 'removed_networks', 'owned_absence_verified', 'worker_exit_verified'}:
        if all(type(progress[n]) is int and 0 <= progress[n] <= 512 for n in ('removed_containers','removed_networks')) and all(type(progress[n]) is bool for n in ('owned_absence_verified','worker_exit_verified')):
            value['cleanup_progress'] = deepcopy(progress)
    return value


class ReadbackProbe:
    """Keep the existing acceptance gate; diagnose only a failed isolated GET."""
    def __init__(self, directory): self.directory, self.summary = directory, None

    def __call__(self, state, manifest, guard):
        self.summary = None
        validate(self.directory)
        require(state == checked_state(self.directory), 'MAINTENANCE_ACCEPTANCE_STATE_CHANGED')
        try: return core.live_readback(state, manifest, guard)
        except ReadFailure as exc:
            if exc.code == 'OPS_RESOURCE_READBACK_FAILED':
                try:
                    guard()
                    cfg = manage.existing(state)
                    _, body = Transport('http://127.0.0.1:' + str(cfg['port']), cfg['access_file'],
                                        timeout=10, max_bytes=1024*1024).get('/api/resources')
                    self.summary = resource_summary(body)
                    guard()
                except local.ERRORS: self.summary = None
            raise


def digest(raw): return hashlib.sha256(raw).hexdigest()
def lifecycle_lock(directory): return ws.file_lock(directory / 'maintenance-lifecycle.lock', 'MAINTENANCE_ACCEPTANCE_BUSY')


def read(path, limit=1024*1024):
    raw, _ = rc.read_file(manage.safe(path), limit, private=True)
    require(path.stat().st_nlink == 1, 'MAINTENANCE_ACCEPTANCE_HARDLINK_REFUSED')
    return raw


def sources(root):
    bodies = {}
    for rel in AUXILIARY:
        path = manage.safe(root / rel)
        require(path.is_file() and path.stat().st_nlink == 1 and path.stat().st_size < 1024*1024,
                'MAINTENANCE_ACCEPTANCE_SOURCE_INVALID')
        bodies[rel] = path.read_bytes()
    spec = strict_json(bodies[SPEC])
    require(spec.get('schema') == 1 and spec.get('activation_supported') is False
            and spec.get('production_cli_available') is False and spec.get('active_code_files') == 61
            and spec.get('active_code_sha256') == core.CODE, 'MAINTENANCE_ACCEPTANCE_SPEC_CHANGED')
    wanted = {row['path']: row['sha256'] for row in spec['auxiliary_files']}
    for rel in AUXILIARY - {ENTRY, SPEC}:
        require(wanted.get(rel) == digest(bodies[rel]), 'MAINTENANCE_ACCEPTANCE_CANDIDATE_CHANGED')
    require(worker.code_hash(root) == core.CODE, 'MAINTENANCE_ACCEPTANCE_RUNTIME_CHANGED')
    return bodies


def preflight():
    value = native.native_preflight()
    require(value.get('linger') is True, 'MAINTENANCE_ACCEPTANCE_LINGER_REQUIRED')
    sources(ROOT)
    return dict(value, schema=1, stage=STAGE, result='passed', files_written=[],
                scope='read_only_current_user_manager_and_reviewed_sources', linger_changed=False,
                production_service_control_requested=False, host_reboot_requested=False)


def create_run(base, engine):
    bodies = sources(ROOT)
    directory, record = isolated.create_run(base, engine, scenario='service')
    # Docker may allocate a different ephemeral publication after stop/start.
    # Select free loopback ports per run, then bind them for this test lifetime.
    selected = set()
    for role, (_, subtree) in isolated.ROLES.items():
        rel = str(Path(subtree) / ('acceptance-' + role + '.json'))
        path = directory / 'project' / rel; spec = strict_json(read(path))
        for cfg in spec['services'].values():
            require(cfg.get('ports') == [{'target': 8080, 'host_ip': '127.0.0.1', 'protocol': 'tcp'}],
                    'MAINTENANCE_ACCEPTANCE_PORT_SPEC_CHANGED')
            port = isolated.free_port(exclude=selected); selected.add(port)
            cfg['ports'][0]['published'] = port
        spec['networks'] = {'default': {'name': record['prefix']+'-fixtures', 'external': True}}
        isolated.write(path, spec); record['compose'][rel]['sha256'] = digest(read(path))
    record['fixture_network'] = {'name': record['prefix']+'-fixtures', 'driver': 'bridge', 'owner': record['owner']}
    isolated.write(directory / 'run.json', record)
    for rel, raw in bodies.items():
        target = directory / 'project' / rel
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(raw); target.chmod(0o600)
    isolated.write(directory / 'maintenance-run.json', {'schema': 1, 'kind': KIND,
        'directory': str(directory), 'owner': record['owner'], 'code_sha256': core.CODE,
        'source_set_sha256': change.digest(record['source_hashes']),
        'auxiliary_hashes': {rel: digest(raw) for rel, raw in bodies.items()}})
    validate(directory)
    return directory, record


def validate(directory, legacy_cleanup=False):
    record = isolated.validate_run(directory)
    require(record.get('scenario') == 'service', 'MAINTENANCE_ACCEPTANCE_SCENARIO_INVALID')
    marker = strict_json(read(directory / 'maintenance-run.json', 128*1024))
    require(marker.get('schema') == 1 and marker.get('kind') == KIND
            and marker.get('directory') == str(directory) and marker.get('owner') == record['owner']
            and marker.get('code_sha256') == core.CODE
            and marker.get('source_set_sha256') == change.digest(record['source_hashes']),
            'MAINTENANCE_ACCEPTANCE_RECORD_INVALID')
    bodies = sources(ROOT)
    expected = {rel: digest(raw) for rel, raw in bodies.items()}
    if legacy_cleanup:
        require('fixture_network' not in record, 'MAINTENANCE_LEGACY_CLEANUP_ONLY')
        expected[ENTRY] = LEGACY_CLEANUP_SHA256
    require(marker.get('auxiliary_hashes') == expected,
            'MAINTENANCE_ACCEPTANCE_TOOL_CHANGED')
    project = directory / 'project'
    for rel, wanted in {**record['source_hashes'], **marker['auxiliary_hashes']}.items():
        path = Path(rel)
        require(not path.is_absolute() and '..' not in path.parts, 'MAINTENANCE_ACCEPTANCE_SOURCE_SCOPE')
        require(digest(read(project / path)) == wanted, 'MAINTENANCE_ACCEPTANCE_SOURCE_CHANGED')
    require(worker.code_hash(project) == core.CODE, 'MAINTENANCE_ACCEPTANCE_RUNTIME_CHANGED')
    if not legacy_cleanup:
        require(record.get('fixture_network') == {'name': record['prefix']+'-fixtures',
            'driver':'bridge','owner':record['owner']}, 'MAINTENANCE_ACCEPTANCE_NETWORK_PLAN_CHANGED')
        for role, (_, subtree) in isolated.ROLES.items():
            path = project / subtree / ('acceptance-'+role+'.json')
            require(strict_json(read(path))['networks'] == {'default': {'name':record['fixture_network']['name'],
                'external':True}}, 'MAINTENANCE_ACCEPTANCE_NETWORK_PLAN_CHANGED')
    return record


def checked_state(directory):
    validate(directory)
    require(ROOT == directory / 'project', 'MAINTENANCE_ACCEPTANCE_COPIED_ENTRY_REQUIRED')
    return native.checked_state(directory)[0]


def invoke(directory, timeout=850):
    record = validate(directory); project = directory / 'project'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('OPS_', 'COMPOSE_', 'PYTHON'))
           and k not in ('DOCKER_CONTEXT', 'DOCKER_API_VERSION', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')}
    env.update(DOCKER_HOST=record['endpoint'], PYTHONDONTWRITEBYTECODE='1')
    path = directory / 'maintenance-worker.log'
    if path.exists(): manage.private(path)
    with path.open('w') as stream:
        path.chmod(0o600)
        child = subprocess.Popen([sys.executable, '-I', '-B', str(project / ENTRY), 'worker',
            '--run-dir', str(directory)], cwd=project, env=env, stdin=subprocess.DEVNULL,
            stdout=stream, start_new_session=True)
        try: code = child.wait(timeout=timeout)
        except BaseException: isolated.stop_worker(child); raise
    answer = strict_json(read(path, 128*1024))
    if code != 0 or answer.get('result') != 'passed':
        exc = ReadFailure(answer.get('code', 'MAINTENANCE_ACCEPTANCE_CHILD_FAILED'))
        exc.service_action = answer.get('failure_context', {}).get('action')
        exc.service_command = answer.get('failure_context', {}).get('service_command')
        exc.resource_readback = safe_summary(answer.get('resource_readback'))
        exc.failure_phase = answer.get('failure_phase') if answer.get('failure_phase') in core.PHASES else None
        exc.docker_diagnostic = safe_docker_diagnostic(answer.get('docker_diagnostic'))
        raise exc
    require(answer.get('stage') == STAGE and answer.get('action') == 'worker', 'MAINTENANCE_ACCEPTANCE_REPORT_MISMATCH')
    return answer


class Engine(isolated.Engine):
    def owned(self, directory, record):
        # A disappearing object is never a permission to control a replacement.
        for attempt in range(2):
            try: return super().owned(directory, record)
            except ReadFailure as exc:
                if attempt or (getattr(exc, 'docker_diagnostic', None) or {}).get('reason') != 'CONTAINER_NOT_FOUND': raise
        raise ReadFailure('ACCEPTANCE_CONTAINER_LIST_UNSTABLE')

    def snapshot(self, ids=None):
        # Retry read-only enumeration once when a running object vanished.
        if ids is not None: return super().snapshot(ids)
        for attempt in range(2):
            try: return super().snapshot()
            except ReadFailure as exc:
                if attempt or (getattr(exc, 'docker_diagnostic', None) or {}).get('reason') != 'CONTAINER_NOT_FOUND': raise
        raise ReadFailure('ACCEPTANCE_CONTAINER_LIST_UNSTABLE')

    def create_fixture_network(self, directory):
        record = validate(directory); plan = record['fixture_network']
        found = self.command(['network','ls','--no-trunc','-q','--filter',
            'label='+isolated.LABEL+'='+record['owner']]).split()
        require(not found, 'MAINTENANCE_ACCEPTANCE_NETWORK_ALREADY_PRESENT')
        ident = self.command(['network','create','--driver','bridge','--label',
            isolated.LABEL+'='+record['owner'],plan['name']]).strip()
        require(re.fullmatch('[a-f0-9]{64}',ident), 'MAINTENANCE_ACCEPTANCE_NETWORK_RESPONSE_INVALID')
        self.verify_fixture_network(directory)

    def verify_fixture_network(self, directory):
        record = validate(directory); plan = record['fixture_network']
        fmt = '{"name":{{json .Name}},"driver":{{json .Driver}},"owner":{{json (index .Labels "'+isolated.LABEL+'")}}}'
        value = strict_json(self.command(['network','inspect','--format',fmt,plan['name']]))
        require(value == plan, 'MAINTENANCE_ACCEPTANCE_NETWORK_OWNERSHIP_CHANGED')

    def command(self, args, timeout=30):
        context = getattr(self, '_docker_context', None) or {}
        def fail(code, outcome, reason, exit_code=None, truncated=False):
            exc = ReadFailure(code)
            exc.docker_diagnostic = {'operation': docker_operation(args), 'outcome': outcome,
                'reason': reason, 'exit_code': exit_code, 'text_truncated': truncated,
                'phase': context.get('phase', 'metadata_or_test_operation'),
                'fixture_role': context.get('fixture_role')}
            raise exc from None
        try:
            result = subprocess.run(['docker', '--host', self.endpoint, *args], env=self.env,
                capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            fail('ACCEPTANCE_COMMAND_UNCERTAIN', 'timeout', 'COMMAND_TIMEOUT')
        except OSError:
            fail('ACCEPTANCE_DOCKER_UNAVAILABLE', 'unavailable', 'CLI_UNAVAILABLE')
        if result.returncode != 0:
            reason, truncated = docker_reason(result.stdout, result.stderr)
            fail('ACCEPTANCE_DOCKER_COMMAND_FAILED', 'failed', reason, result.returncode, truncated)
        if len(result.stdout) > 1024*1024:
            fail('ACCEPTANCE_DOCKER_RESPONSE_LIMIT', 'response_limit', 'RESPONSE_LIMIT', 0, True)
        return result.stdout

    def compose(self, directory, relative, service, recreate=False):
        role = next((role for role, (name, subtree) in isolated.ROLES.items()
            if relative == str(Path(subtree)/('acceptance-'+role+'.json'))
            and service in (name, 'manual-model') and (service != 'manual-model' or role == 'model')), None)
        if relative == 'ops_platform/docker-compose.yml' and service == 'ops-web': role = 'ops'
        require(role is not None, 'MAINTENANCE_ACCEPTANCE_FIXTURE_ROLE_INVALID')
        previous = getattr(self, '_docker_context', None)
        self._docker_context = {'phase': 'fixture_preparation',
            'fixture_role': 'manual_model' if service == 'manual-model' else role}
        try:
            self.verify_fixture_network(directory)
            return super().compose(directory, relative, service, recreate)
        finally: self._docker_context = previous

    def prometheus_publication(self, directory, ident):
        record = validate(directory)
        require(ident in self.owned(directory, record), 'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
        identity = self.metadata(ident)['identity']
        require(identity['project'] == record['prefix'] + '-prometheus'
                and identity['service'] == isolated.ROLES['prometheus'][0], 'MAINTENANCE_ACCEPTANCE_PORT_TARGET_INVALID')
        fmt = '{"configured":{{json (index .HostConfig.PortBindings "8080/tcp")}},"published":{{json (index .NetworkSettings.Ports "8080/tcp")}}}'
        value = strict_json(self.command(['container', 'inspect', '--format', fmt, ident]))
        rel = str(Path(isolated.ROLES['prometheus'][1]) / 'acceptance-prometheus.json')
        spec = strict_json(read(directory / 'project' / rel))
        port = spec['services'][isolated.ROLES['prometheus'][0]]['ports'][0]['published']
        expected = [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]
        require(value.get('configured') == expected and value.get('published') == expected,
                'MAINTENANCE_ACCEPTANCE_PORT_BINDING_CHANGED')
        return {'host_ip': '127.0.0.1', 'host_port': port, 'container_port': 8080}

    def cleanup(self, directory, legacy=False):
        self._docker_context = {'phase': 'cleanup', 'fixture_role': None}
        record = validate(directory, legacy_cleanup=legacy)
        progress = {'removed_containers':0,'removed_networks':0,'owned_absence_verified':False,'worker_exit_verified':False}
        try: return self._cleanup(directory, record, progress)
        except BaseException as exc:
            exc.cleanup_progress = deepcopy(progress)
            raise

    def _cleanup(self, directory, record, progress):
        with lifecycle_lock(directory):
            # This runs a separately verified copy and proves unit exit before
            # deleting any container. A surviving operation child holds this
            # orchestration lock, so loss of its supervisor cannot race cleanup.
            unit = native.cleanup_unit_subprocess(directory)
            progress['worker_exit_verified'] = unit.get('worker_exit_verified') is True
            operation = directory / 'maintenance-operation.json'
            if operation.exists() or operation.is_symlink():
                value = strict_json(read(operation, 32768))
                require(value.get('schema') == 1 and value.get('kind') == KIND
                        and value.get('directory') == str(directory) and value.get('owner') == record['owner']
                        and value.get('phase') == 'completed', 'MAINTENANCE_ACCEPTANCE_OPERATION_UNCERTAIN')
            owned = self.owned(directory, record)
            current = self.snapshot()
            protected = {ident:row for ident,row in current.items() if ident not in owned}
            old = record['protected']
            difference = {'missing_or_stopped':len(set(old)-set(protected)),
                'changed':sum(protected[i] != row for i,row in old.items() if i in protected),
                'added_running':len(set(protected)-set(old))}
            for ident in owned:
                require(ident in self.owned(directory, record), 'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
                self.command(['container', 'rm', '-f', ident], timeout=30)
                progress['removed_containers'] += 1
            networks = self.command(['network', 'ls', '--no-trunc', '-q', '--filter',
                'label=' + isolated.LABEL + '=' + record['owner']]).split()
            require(len(networks) <= 8 and all(re.fullmatch('[a-f0-9]{64}', x) for x in networks), 'ACCEPTANCE_NETWORK_LIMIT')
            for ident in networks:
                fmt = '{"owner":{{json (index .Labels "' + isolated.LABEL + '")}},"containers":{{json .Containers}},"name":{{json .Name}},"driver":{{json .Driver}}}'
                net = strict_json(self.command(['network', 'inspect', '--format', fmt, ident]))
                names = {row['project']+'_default' for row in record['compose'].values()}
                if record.get('fixture_network'): names = {record['fixture_network']['name']}
                require(net['owner'] == record['owner'] and net.get('name') in names and net.get('driver') == 'bridge',
                        'MAINTENANCE_ACCEPTANCE_NETWORK_OWNERSHIP_CHANGED')
                require(not net['containers'], 'ACCEPTANCE_NETWORK_IN_USE')
                self.command(['network', 'rm', ident])
                progress['removed_networks'] += 1
            require(not self.owned(directory, record), 'ACCEPTANCE_CLEANUP_INCOMPLETE')
            remaining = self.command(['network','ls','--no-trunc','-q','--filter',
                'label='+isolated.LABEL+'='+record['owner']]).split()
            require(not remaining, 'ACCEPTANCE_CLEANUP_INCOMPLETE')
            progress['owned_absence_verified'] = True
            self.protect(protected)
            return {'result': 'passed', 'service': unit, 'owned_containers_removed': len(owned),
                'owned_networks_removed': len(networks), 'protected_containers_unchanged': len(protected),
                'protection_scope':'current_running_containers_during_cleanup',
                'prior_run_protected_baseline_match':not difference['missing_or_stopped'] and not difference['changed'],
                'prior_run_protected_difference':difference, 'owned_absence_verified':True,
                'images_or_volumes_removed': False}


class Inventory:
    """Discover two disposable targets by ownership/Compose identity, never IP."""
    def __init__(self, directory, engine):
        self.directory, self.engine = directory, engine

    def collect(self):
        record = validate(self.directory)
        owned = self.engine.owned(self.directory, record)
        wanted = {(record['prefix'] + '-ops', 'ops-web'): 'ops',
                  (record['prefix'] + '-prometheus', isolated.ROLES['prometheus'][0]): 'prometheus'}
        rows = {}
        for ident in owned:
            meta = self.engine.metadata(ident); value = meta['identity']
            role = wanted.get((value['project'], value['service']))
            if role is None: continue
            require(role not in rows and value['id'] == ident and meta['acceptance'] == record['owner']
                    and value['image'] == record['image'], 'MAINTENANCE_ACCEPTANCE_SCOPE_AMBIGUOUS')
            mounts = value.get('mounts')
            require(isinstance(mounts, list) and mounts, 'MAINTENANCE_ACCEPTANCE_MOUNTS_INVALID')
            projection = []
            for mount in mounts:
                if role == 'ops' and mount.get('Type') == 'tmpfs' and mount.get('Destination') == '/tmp':
                    require(mount.get('RW') is True, 'MAINTENANCE_ACCEPTANCE_MOUNTS_INVALID')
                    projection.append({'Type': 'tmpfs', 'Destination': '/tmp'})
                    continue
                require(isinstance(mount, dict) and mount.get('Type') == 'bind'
                        and type(mount.get('RW')) is bool and isinstance(mount.get('Source'), str)
                        and isinstance(mount.get('Destination'), str), 'MAINTENANCE_ACCEPTANCE_MOUNTS_INVALID')
                source = Path(mount['Source'])
                require(source.is_absolute() and source.is_relative_to(self.directory)
                        and source == Path(os.path.normpath(source)), 'MAINTENANCE_ACCEPTANCE_MOUNT_OUTSIDE_RUN')
                projection.append({key: mount[key] for key in ('Type', 'Source', 'Destination', 'RW')})
            require(len({r['Destination'] for r in projection}) == len(projection), 'MAINTENANCE_ACCEPTANCE_MOUNTS_INVALID')
            rows[role] = {'container_id': ident, 'image_id': value['image'],
                'mounts_sha256': boot.fingerprint(sorted(projection, key=lambda x: x['Destination'])),
                'owner': record['owner']}
        require(set(rows) == {'ops', 'prometheus'}, 'MAINTENANCE_ACCEPTANCE_SCOPE_MISSING')
        active = self.engine.snapshot()
        scope_ids = {row['container_id'] for row in rows.values()}
        protected = {ident: row['started'] for ident, row in active.items() if ident not in scope_ids}
        require(all(row.get('id') == ident and row.get('running') is True for ident, row in active.items()),
                'MAINTENANCE_ACCEPTANCE_PROTECTED_INVALID')
        return core.inventory({'scope': rows, 'protected': protected})

    def __call__(self):
        first = self.collect(); second = self.collect()
        require(first == second, 'MAINTENANCE_ACCEPTANCE_INVENTORY_UNSTABLE')
        return first


LEASE_CHILD = r'''
import fcntl,json,os,pathlib,signal,stat,sys,time
signal.signal(signal.SIGALRM,lambda *_:os._exit(3));signal.alarm(8)
state=pathlib.Path(sys.argv[1]); run=pathlib.Path(sys.argv[2]); fd=int(sys.argv[3]); outer=int(sys.argv[4])
for descriptor,path in ((fd,state/'automatic-refresh.lock'),(outer,run/'maintenance-lifecycle.lock')):
 a=os.fstat(descriptor);b=path.lstat()
 assert stat.S_ISREG(a.st_mode) and a.st_uid==os.getuid() and not a.st_mode&0o077 and a.st_nlink==1
 assert (a.st_dev,a.st_ino)==(b.st_dev,b.st_ino)
 test=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
 try:
  try:fcntl.flock(test,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:pass
  else:raise AssertionError('lease was not held')
 finally:os.close(test)
time.sleep(float(sys.argv[5]));print(json.dumps({'result':'passed','both_inherited_leases_verified':True}))
'''


def leased_child(scope, directory, outer, delay=0.02):
    scope.checkpoint()
    require(type(outer) is int and outer >= 3 and 0 <= delay <= 2, 'MAINTENANCE_ACCEPTANCE_LEASE_REQUIRED')
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', LEASE_CHILD, str(scope.parent.directory),
        str(directory), str(scope.descriptor), str(outer), str(delay)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        start_new_session=True, pass_fds=(scope.descriptor, outer))
    try: out, _ = child.communicate(timeout=10)
    except BaseException: isolated.stop_worker(child); raise
    require(child.returncode == 0 and len(out) < 1024 and strict_json(out) ==
        {'result': 'passed', 'both_inherited_leases_verified': True}, 'MAINTENANCE_ACCEPTANCE_CHILD_FAILED')
    scope.checkpoint()
    return {'result': 'passed', 'child_processes_stopped': True}


def wait_container(engine, ident, running, timeout=45):
    until = time.monotonic() + timeout
    while True:
        row = engine.metadata(ident)['identity']
        if row['running'] is running and (not running or row.get('health') == 'healthy'): return row
        require(time.monotonic() < until, 'MAINTENANCE_ACCEPTANCE_CONTAINER_WAIT_TIMEOUT')
        time.sleep(.2)


def cycle_scoped(scope, directory, engine):
    record = validate(directory); path = directory / 'maintenance-operation.json'
    require(not path.exists() and not path.is_symlink(), 'MAINTENANCE_ACCEPTANCE_OPERATION_ALREADY_RECORDED')
    targets = scope.record['inventory']['scope']
    publication = engine.prometheus_publication(directory, targets['prometheus']['container_id'])
    journal = {'schema': 1, 'kind': KIND, 'directory': str(directory), 'owner': record['owner'],
               'job_id': scope.record['id'], 'scope': targets, 'commands': []}
    before = {role: engine.metadata(row['container_id'])['identity']['started'] for role, row in targets.items()}
    def command(operation, role):
        scope.checkpoint()
        ident = targets[role]['container_id']
        require(ident in engine.owned(directory, record), 'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
        journal.update(phase=operation + '_requested', target=role)
        journal['commands'].append({'operation': operation, 'container_id': ident})
        isolated.write(path, journal)  # A lost reply is not retried or silently cleaned.
        args = ['container', operation] + (['-t', '5'] if operation == 'stop' else []) + [ident]
        engine.command(args, timeout=30)
        wait_container(engine, ident, operation == 'start')
        journal['phase'] = operation + '_verified'; isolated.write(path, journal)
        scope.checkpoint()
    for role in sorted(targets): command('stop', role)
    evidence = leased_child(scope, directory, LIFECYCLE_FD)
    for role in reversed(sorted(targets)): command('start', role)
    after = {role: engine.metadata(row['container_id'])['identity']['started'] for role, row in targets.items()}
    require(all(before[role] != after[role] for role in targets), 'MAINTENANCE_ACCEPTANCE_START_TIME_UNCHANGED')
    journal.update(phase='completed', child_processes_stopped=True, started_before=before, started_after=after)
    isolated.write(path, journal)
    require(engine.prometheus_publication(directory, targets['prometheus']['container_id']) == publication,
            'MAINTENANCE_ACCEPTANCE_PORT_BINDING_CHANGED')
    journal.update(prometheus_publication_preserved=True, prometheus_publication=publication)
    isolated.write(path, journal)
    return evidence


def initialize_boot_fixture(state, ctl):
    """Fresh disposable history only. This is not a production policy migration.

    The installed unit is initially paused/disabled. A test contract and explicit
    receipt are written before one manager start. Accepted initial history is
    recorded only after the real worker consumes that receipt and evaluates.
    """
    ctl.good('install')
    manifest = service.load(state); before = Path(manifest['unit_path']).read_bytes()
    store = ws.Store(state)
    service.stopped(state, service.owned(manifest, ctl.backend), ctl.backend)
    with ws.automatic_lease(state):
        with change.change_lock(state):
            require(worker.runtime_snapshot(state) == manifest['binding'], 'SERVICE_BINDING_CHANGED')
            manifest.update(persistence='boot', boot_contract={'schema': 1, 'generation': str(uuid.uuid4()), 'startup_seconds': 120})
            require(service.render(manifest).encode() == before, 'MAINTENANCE_ACCEPTANCE_UNIT_CHANGED')
            ws.write(service.manifest_path(state), manifest)
            ws.write(store.path.parent / 'code-transition.json', {'schema': 1,
                'kind': 'stopped-code-policy-transition', 'phase': 'committed', 'directory': str(state),
                'id': str(uuid.uuid4()), 'fixture_only': True, 'source_written': False})
            with store.lock():
                control = dict(store.control(), desired='running', service_stop_token=uuid.uuid4().hex)
                store.status(phase='service_starting', completed_cycles=0)
                receipt = {'schema': 1, 'phase': 'armed', 'generation': boot.policy(manifest)['generation'],
                    'manifest_sha256': boot.fingerprint(manifest), 'control_sha256': boot.fingerprint(control),
                    'status_sha256': boot.fingerprint(store.optional('status.json')), 'identity': worker.process_identity()}
                ws.write(store.path / 'boot.json', receipt)
                service.set_enabled(manifest, True); ctl.backend.reload()
                ws.write(store.path / 'control.json', control)
    ctl.backend.start()  # Never resubmit an unknown result.
    current = ctl.wait(native.healthy)
    entered = store.optional('boot.json'); last = native.observation(current)
    require(current['enabled'] and current['persistence'] == 'boot' and entered.get('phase') == 'entered'
            and entered.get('previous_receipt_sha256') == boot.fingerprint(receipt)
            and entered.get('service_invocation') == native.invocation(current)
            and entered.get('invocation') == last.get('invocation') and entered.get('identity') == last.get('identity')
            and last['outcome']['restart_requested'] is False, 'MAINTENANCE_ACCEPTANCE_INITIAL_ENTRY_FAILED')
    ws.write(store.path.parent / 'boot-start.json', {'schema': 1, 'kind': 'initial-boot-start',
        'phase': 'accepted', 'directory': str(state), 'id': str(uuid.uuid4()), 'fixture_only': True,
        'accepted_service_invocation': native.invocation(current), 'fresh_evaluation_verified': True})
    return current


def plan_wait(controller, seconds=30):
    until = time.monotonic() + seconds
    while True:
        try: return controller.plan(wait_seconds=300)
        except ReadFailure as exc:
            require(exc.code in ('PREPARED_STATE_BUSY', 'PUBLIC_URL_CHANGE_BUSY', 'MAINTENANCE_HEALTHY_WORKER_REQUIRED'), exc.code)
            require(time.monotonic() < until, 'MAINTENANCE_ACCEPTANCE_PLAN_WAIT_TIMEOUT')
            time.sleep(.25)


def rejected(fn, codes):
    try: fn()
    except ReadFailure as exc: require(exc.code in codes, exc.code); return exc.code
    raise ReadFailure('MAINTENANCE_ACCEPTANCE_REFUSAL_MISSING')


def phase(directory, engine, message):
    engine.protect(validate(directory)['protected'])
    print('[maintenance-acceptance] ' + message, file=sys.stderr, flush=True)


def exercise(directory, engine):
    validate(directory); require(ROOT == directory / 'project', 'MAINTENANCE_ACCEPTANCE_COPIED_ENTRY_REQUIRED')
    phase(directory, engine, 'prepare disposable Ops and fresh boot-policy worker')
    c = isolated.prepare_test_ops(directory, engine); state = checked_state(directory); ctl = native.Controller(state)
    first = initialize_boot_fixture(state, ctl)
    inventory = Inventory(directory, engine)
    probe = ReadbackProbe(directory)
    controller = core.Maintenance(state, ctl.backend, inventory_fn=inventory, readback_fn=probe)
    store = ws.Store(state); manifest = service.load(state)
    fixed = {name: read(store.path.parent / name, 32768) for name in ('boot-start.json', 'code-transition.json', 'service.json')}
    config = {name: read(state / name) for name in ('host-config.json', 'config.json', 'owner.json', 'compose.env', 'access-key')}
    api = rc.API(manage.existing(state)); cookie = api.login(); cases = {}
    phase(directory, engine, 'native scoped stop, inherited child leases, same-container service return')
    proposal = plan_wait(controller); before = inventory()
    result = controller.run(proposal, lambda scope: cycle_scoped(scope, directory, engine))
    require(result.get('result') == 'passed' and result.get('worker_exit_verified') is True
            and store.control()['desired'] == 'paused' and not service.enabled(manifest),
            result.get('code', 'MAINTENANCE_ACCEPTANCE_STOP_FAILED'))
    require(inventory() == before, 'MAINTENANCE_ACCEPTANCE_IDENTITY_CHANGED')
    api.expired(cookie)
    cases.update(native_discovered_scope_and_protected_inventory='passed',
                 native_stop_cgroup_exit_and_disabled_policy='passed',
                 scoped_same_container_stop_start='passed', real_child_inherits_both_leases_and_exits='passed',
                 old_ops_session_rejected='passed')
    phase(directory, engine, 'explicit return requires real readback and fresh worker receipt/evaluation')
    journals_before = len(refresh.lineage(state)['records'])
    result = controller.resume(proposal['id'])
    if result.get('result') != 'passed' or result.get('fresh_evaluation_verified') is not True:
        exc = ReadFailure(result.get('code', 'MAINTENANCE_ACCEPTANCE_RETURN_FAILED'))
        exc.resource_readback = probe.summary
        exc.failure_phase = result.get('failure_phase')
        raise exc
    current = ctl.status()
    require(native.healthy(current) and native.invocation(current) != native.invocation(first),
            'MAINTENANCE_ACCEPTANCE_STALE_INVOCATION')
    cases['explicit_return_new_receipt_invocation_and_evaluation'] = 'passed'
    journals_after = len(refresh.lineage(state)['records'])
    require(journals_after - journals_before in (0,1), 'MAINTENANCE_ACCEPTANCE_EXTRA_GENERATION')
    controls = ctl.backend.control_commands; record = read(controller.path(proposal['id']), 32768)
    rejected(lambda: controller.resume(proposal['id']), {'MAINTENANCE_RETURN_REFUSED'})
    require(ctl.backend.control_commands == controls and read(controller.path(proposal['id']), 32768) == record
            and native.invocation(ctl.status()) == native.invocation(current), 'MAINTENANCE_ACCEPTANCE_DUPLICATE_MUTATED')
    cases['returned_job_cannot_resubmit_start'] = 'passed'
    follow = ctl.wait(lambda v: native.healthy(v, native.observation(current)['completed_cycles'] + 1))
    require(native.invocation(follow) == native.invocation(current) and not native.observation(follow)['outcome']['restart_requested'],
            'MAINTENANCE_ACCEPTANCE_FOLLOWUP_CHANGED')
    require(len(refresh.lineage(state)['records']) == journals_after, 'MAINTENANCE_ACCEPTANCE_EXTRA_GENERATION')
    cases['unchanged_followup_no_duplicate_restart_or_journal'] = 'passed'
    phase(directory, engine, 'second job: failed live readback preserves pause; repeated return refused')
    second = plan_wait(controller)
    stopped = controller.run(second, lambda scope: leased_child(scope, directory, LIFECYCLE_FD))
    require(stopped.get('result') == 'passed', stopped.get('code', 'MAINTENANCE_ACCEPTANCE_SECOND_STOP_FAILED'))
    before_commands = ctl.backend.control_commands
    isolated.write(c['control'], {'version': 0, 'rules_fault': True})
    try:
        denied = controller.resume(second['id'])
        require(denied.get('result') == 'attention_required' and denied.get('code') == 'OPS_ALERT_READBACK_FAILED'
                and ctl.backend.control_commands == before_commands
                and store.control()['desired'] == 'paused' and not service.enabled(manifest)
                and service.inactive(service.owned(manifest, ctl.backend)) and ctl.backend.cgroup_empty(ctl.backend.inspect()),
                'MAINTENANCE_ACCEPTANCE_READBACK_GATE_FAILED')
        rejected(lambda: controller.resume(second['id']), {'MAINTENANCE_RETURN_REFUSED'})
        require(ctl.backend.control_commands == before_commands, 'MAINTENANCE_ACCEPTANCE_FAILURE_REPLAYED')
    finally: isolated.write(c['control'], {'version': 0})
    cases['native_live_readback_failure_keeps_paused_disabled'] = 'passed'
    cases['failed_return_not_replayed_or_automatically_recovered'] = 'passed'
    require({name: read(store.path.parent / name, 32768) for name in fixed} == fixed
            and {name: read(state / name) for name in config} == config
            and inventory() == before and worker.code_hash(ROOT) == core.CODE,
            'MAINTENANCE_ACCEPTANCE_FIXED_STATE_CHANGED')
    isolated.preserve_test_ops(c, engine)
    cases['policy_source_credentials_cache_and_initial_history_preserved'] = 'passed'
    phase(directory, engine, 'remove owned unit, retain both job audits; parent checks before Docker cleanup')
    removed = native.cleanup_unit(directory)
    require(removed.get('worker_exit_verified') and removed.get('unit_absence_verified'), 'MAINTENANCE_ACCEPTANCE_UNIT_REMOVAL_FAILED')
    require(controller.load(proposal['id'])['phase'] == 'returned' and controller.load(second['id'])['phase'] == 'attention_required',
            'MAINTENANCE_ACCEPTANCE_AUDIT_CHANGED')
    cases['owned_unit_removed_and_job_audits_preserved'] = 'passed'
    return {'schema': 1, 'stage': STAGE, 'action': 'worker', 'result': 'passed', 'checked_at': rc.now(),
        'scope': 'native_user_manager_docker_same_boot_synthetic_upstreams_fixture_initial_history', 'cases': cases,
        'manager_version': ctl.manager_version, 'interval_seconds': 30, 'image_id': c['record']['image'],
        'runtime_source_hashes_verified': c['hashes'], 'active_code_sha256': core.CODE,
        'same_test_ops_container_image_mounts': True, 'initial_cache_entries_preserved': len(c['initial_cache']['success_times']),
        'fixture_ports': 'automatically_selected_explicit_loopback_bindings', 'prometheus_publication_preserved': True,
        'synthetic_credentials_preserved': True, 'bootstrap_record_preserved': True,
        'maintenance_jobs': 2, 'job_phases': ['returned', 'attention_required'],
        'refresh_journal_count': len(refresh.lineage(state)['records']), 'scoped_containers': 2,
        'return_metadata_generations': journals_after - journals_before,
        'scoped_container_stops': 2, 'scoped_container_starts': 2, 'test_unit_name': manifest['unit_name'],
        'test_unit_removed': True, 'test_worker_exit_verified': True, 'initial_boot_history': 'isolated_fixture_with_real_fresh_evaluation',
        'code_policy_transition_acceptance': False, 'production_state_read': False,
        'production_credentials_read': False, 'production_worker_stopped': False,
        'production_service_control_requested': False, 'production_application_api_requests': 0,
        'backup_executed': False, 'restore_executed': False, 'backup_executor_integration': False,
        'inference_requested': False, 'image_build_or_pull_requested': False, 'linger_changed': False,
        'host_reboot_requested': False, 'logout_or_boot_acceptance': False, 'promql_engine_acceptance': False,
        'long_running_acceptance': False, 'kubernetes_acceptance': 'deferred'}


def run(base):
    preflight()
    with isolated.run_lock(base):
        engine = Engine(); directory, record = create_run(base, engine)
        print('[maintenance-acceptance] evidence: ' + str(directory), file=sys.stderr, flush=True)
        result = {'schema': 1, 'stage': STAGE, 'result': 'attention_required',
                  'production_worker_stopped': False, 'production_service_control_requested': False,
                  'production_credentials_read': False, 'backup_executed': False,
                  'restore_executed': False, 'inference_requested': False, 'linger_changed': False}
        try:
            engine.create_fixture_network(directory)
            for role, (name, subtree) in isolated.ROLES.items():
                rel = str(Path(subtree) / ('acceptance-' + role + '.json'))
                print('[maintenance-acceptance] fixture preparation: ' + role, file=sys.stderr, flush=True)
                engine.compose(directory, rel, name)
                if role == 'model':
                    print('[maintenance-acceptance] fixture preparation: manual_model', file=sys.stderr, flush=True)
                    engine.compose(directory, rel, 'manual-model')
            result = invoke(directory)
        except (KeyboardInterrupt, subprocess.TimeoutExpired): result['code'] = 'MAINTENANCE_ACCEPTANCE_INTERRUPTED'
        except local.ERRORS as exc: result.update(error_report(exc), stage=STAGE)
        finally:
            try: cleanup = engine.cleanup(directory)
            except (subprocess.TimeoutExpired, *local.ERRORS) as exc:
                cleanup = error_report(exc)
                result['result'] = 'attention_required'
            if cleanup.get('result') == 'passed' and cleanup.get('prior_run_protected_baseline_match') is False and result.get('result') == 'passed':
                result.update(result='attention_required', code='ACCEPTANCE_PROTECTED_CONTAINER_CHANGED')
            result.update(run_directory=str(directory), cleanup=cleanup)
            isolated.write(directory / 'maintenance-report.json', result)
        return result


def main():
    global LIFECYCLE_FD
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'run', 'worker', 'cleanup'))
    parser.add_argument('--state-dir', type=Path, default=isolated.BASE)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--legacy-cleanup', action='store_true', help='cleanup only the hash-reviewed previous runner version')
    args = parser.parse_args()
    try:
        require(not args.legacy_cleanup or args.action == 'cleanup', 'MAINTENANCE_LEGACY_CLEANUP_ONLY')
        if args.action in ('preflight', 'run'):
            require(args.run_dir is None, 'ACCEPTANCE_RUN_DIR_NOT_ALLOWED')
            report = preflight() if args.action == 'preflight' else run(args.state_dir)
        else:
            require(args.run_dir is not None, 'ACCEPTANCE_RUN_DIR_REQUIRED')
            directory = manage.safe(args.run_dir); record = validate(directory, legacy_cleanup=args.legacy_cleanup)
            engine = Engine(record['endpoint'])
            if args.action == 'cleanup':
                with isolated.run_lock(directory.parent):
                    report = {'schema': 1, 'stage': STAGE, 'result': 'passed', 'run_directory': str(directory),
                              'cleanup': engine.cleanup(directory, legacy=args.legacy_cleanup),
                              'production_worker_stopped':False,'production_service_control_requested':False,
                              'acceptance_result_not_reused':True}
                    isolated.write(directory/'maintenance-cleanup.json', report)
            else:
                require(ROOT == directory / 'project', 'MAINTENANCE_ACCEPTANCE_COPIED_ENTRY_REQUIRED')
                with lifecycle_lock(directory) as descriptor:
                    LIFECYCLE_FD = descriptor
                    try: report = exercise(directory, engine)
                    finally: LIFECYCLE_FD = None
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['result'] == 'passed' else 2
    except (subprocess.TimeoutExpired, *local.ERRORS) as exc:
        print(json.dumps(dict(error_report(exc), schema=1, stage=STAGE, action=args.action,
            production_service_control_requested=False, production_worker_stopped=False, linger_changed=False)))
        return 2


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
