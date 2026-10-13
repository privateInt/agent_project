#!/usr/bin/env python3
"""Activate newly discovered Ops settings once; preserve existing overrides and evidence."""
from copy import deepcopy
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.infra.observability.ops.backup_preflight import Unavailable
from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.ops import discover as discovery, manage, restart_check as rc, set_public_url as url_change
from src.ops.platform.ops.activation_plan import merge, validate_effective
from src.ops.platform.ops.discovery_compose import DockerDiscovery, scoped
from src.ops.platform.transport import Transport, strict_json

KIND = 'O04-discovery-activation'
CONFIGS = url_change.CONFIGS
PROTECTED_FORMAT = '{"id":{{json .Id}},"started":{{json .State.StartedAt}}}'


class Docker(url_change.Docker):
    """Pin the local daemon used by discovery and allow only fixed operations."""
    def __init__(self, project=rc.PROJECT):
        self.project = manage.project_name(project)
        local = DockerDiscovery(); local.host()
        self.endpoint, self.env = local.endpoint, local.env

    def command(self, args, timeout=5):
        ids = lambda values: bool(values) and all(re.fullmatch('[a-f0-9]{64}', v) for v in values)
        allowed = (args == ['ps', '-q', '--no-trunc'] or args == [
            'ps', '-a', '--no-trunc', '--filter', 'label=com.docker.compose.project=' + getattr(self,'project',rc.PROJECT),
            '--filter', 'label=com.docker.compose.service=' + rc.SERVICE, '--format', '{{.ID}}'] or
            (len(args) == 6 and args[:5] == ['inspect', '--type', 'container', '--format', rc.INSPECT] and ids(args[5:])) or
            (len(args) >= 4 and args[:3] == ['inspect', '--format', PROTECTED_FORMAT] and ids(args[3:])) or
            (len(args) == 4 and args[0] == 'exec' and ids(args[1:2]) and args[2:] == ['cat', '/run/ops/config.json']) or
            (len(args) == 5 and args[:4] == ['container', 'restart', '-t', '20'] and ids(args[4:])))
        require(allowed, 'ACTIVATION_DOCKER_COMMAND_REFUSED')
        try:
            result = subprocess.run(['docker', '--host', self.endpoint, *args], env=self.env,
                                    capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ReadFailure('DOCKER_COMMAND_UNCERTAIN') from None
        require(result.returncode == 0, 'DOCKER_COMMAND_FAILED')
        require(len(result.stdout) <= 128 * 1024, 'DOCKER_RESPONSE_TOO_LARGE')
        return result.stdout


def sha(raw): return hashlib.sha256(raw).hexdigest()


def source_snapshot():
    docker = DockerDiscovery(); docker.host()
    return url_change.digest(scoped(docker.inventory(), discovery.ROOT))


def discover_now(cfg):
    return discovery.discover(cfg, discovery.ROOT, DockerDiscovery())


def history(directory):
    result = {}
    for rel in ('restart-check/baseline.json', 'public-url-change/baseline.json'):
        path = directory / rel
        result[rel] = rc.read_file(path, 512*1024, private=True)[1] if path.exists() or path.is_symlink() else None
    return result


def config_bytes(baseline, name, desired):
    if desired and baseline['plan']['changed_fields']:
        return url_change.body(baseline['desired'][name]).encode()
    return baseline['original'][name].encode()


def guard(directory, baseline, mode='desired'):
    cfg = strict_json(baseline['original']['host-config.json'])
    files = rc.files(directory, cfg)
    for name, before in baseline['files'].items():
        wanted = deepcopy(before)
        if name in CONFIGS:
            wanted['sha256'] = sha(config_bytes(baseline, name, mode != 'original'))
        require(files[name] == wanted or (mode == 'partial' and files[name] == before),
                'ACTIVATION_CONFIG_OR_CREDENTIAL_DRIFT')
    require(history(directory) == baseline['previous_evidence'], 'PRIOR_ACCEPTANCE_RECORD_CHANGED')
    for rel, expected in baseline.get('additional_evidence', {}).items():
        require(rel in ('discovery-activation/baseline.json', 'discovery-bootstrap/baseline.json') or
                re.fullmatch(r'discovery-refresh/[0-9]{6}/baseline.json', rel), 'INVALID_PROVENANCE_PATH')
        require(rc.read_file(directory / rel, 512*1024, private=True)[1] == expected, 'PROVENANCE_RECORD_CHANGED')
    return url_change.cache_guard(cfg, baseline['cache'])


def atomic_config(path, raw, metadata):
    """Durable replacement preserving original bytes and ownership on rollback."""
    manage.private(path)
    fd, temporary = tempfile.mkstemp(prefix='.discovery-config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchown(out.fileno(), metadata['uid'], metadata['gid'])
            os.fchmod(out.fileno(), metadata['mode'])
            out.write(raw); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def capture(directory, docker, discover_fn, validate_fn, source_fn, api_factory,
            planner=merge, additional_evidence=None):
    cfg = manage.existing(directory)
    owner = strict_json(rc.read_file(directory / 'owner.json', private=True)[0])
    before_files = rc.files(directory, cfg)
    before_cache, before_history = rc.cache(cfg), history(directory)
    original = {name: rc.read_file(directory / name, private=True)[0].decode() for name in CONFIGS}
    current = url_change.current(docker, directory, cfg, owner, docker.discover())
    require(current['healthy'], 'OPS_NOT_HEALTHY_BEFORE_ACTIVATION')
    protected = docker.protected(current['identity']['id'])
    require(docker.mounted_hash(current['identity']['id']) == before_files['config.json']['sha256'], 'MOUNTED_CONFIG_DIFFERS')
    api_factory(cfg).health()
    source = source_fn()
    result = discover_fn(cfg)
    desired_host, plan = planner(cfg, result)
    validation = validate_fn(desired_host)
    require(source_fn() == source, 'DISCOVERY_BINDING_CHANGED')
    desired_container = deepcopy(desired_host)
    desired_container.update(langfuse_file='/run/ops/langfuse.json', access_file='/run/ops/access-key', state_file='/var/lib/ops/api-success.json')
    desired_container['platforms']['agent']['token_file'] = '/run/ops/agent-token'
    baseline = {'directory': str(directory), 'created_at': rc.now(), 'owner': owner, 'original': original,
                'desired': {'host-config.json': desired_host, 'config.json': desired_container},
                'plan': plan, 'effective_profile_validation': validation, 'source_sha256': source,
                'files': before_files, 'cache': before_cache, 'container': current, 'protected': protected,
                'previous_evidence': before_history}
    if additional_evidence is not None: baseline['additional_evidence'] = deepcopy(additional_evidence)
    require(url_change.current(docker, directory, cfg, owner, current['identity']['id']) == current, 'OPS_CHANGED_DURING_PLAN')
    url_change.protect(docker, baseline); guard(directory, baseline, 'original')
    return {'schema': 1, 'kind': KIND, 'baseline': baseline, 'baseline_sha256': url_change.digest(baseline),
            'restart_intent': None, 'restart_command_completed': False, 'phase': 'prepared'}


def load_record(path, directory):
    record = strict_json(rc.read_file(path, 512*1024, private=True)[0])
    baseline = record['baseline']
    require(record.get('schema') == 1 and record.get('kind') == KIND and baseline['directory'] == str(directory)
            and record['baseline_sha256'] == url_change.digest(baseline), 'ACTIVATION_RECORD_INVALID')
    return record


def install(directory, record, path, docker, source_fn, validate_fn):
    b = record['baseline']; guard(directory, b, 'partial')
    require(not record.get('rollback'), 'ROLLBACK_IN_PROGRESS')
    if record['restart_intent']: return
    old = strict_json(b['original']['host-config.json']); ident = b['container']['identity']['id']
    require(url_change.current(docker, directory, old, b['owner'], ident) == b['container'], 'OPS_CHANGED_BEFORE_ACTIVATION')
    url_change.protect(docker, b)
    require(source_fn() == b['source_sha256'], 'DISCOVERY_BINDING_CHANGED')
    # Revalidate after a delayed/resumed operation, without replaying a stale file.
    validate_fn(b['desired']['host-config.json'])
    require(source_fn() == b['source_sha256'], 'DISCOVERY_BINDING_CHANGED')
    guard(directory, b, 'partial')
    for name in CONFIGS:
        desired = config_bytes(b, name, True)
        if rc.read_file(directory / name, private=True)[0] != desired:
            atomic_config(directory / name, desired, b['files'][name])
    manage.existing(directory); guard(directory, b)
    url_change.protect(docker, b)
    require(url_change.current(docker, directory, old, b['owner'], ident) == b['container'], 'OPS_CHANGED_BEFORE_RESTART')
    if b['plan']['restart_required']:
        record['restart_intent'] = {'at': rc.now(), 'id': ident}
        record['phase'] = 'restart_requested'; rc.write_record(path, record)
        docker.restart(ident)
        record['restart_command_completed'] = True; rc.write_record(path, record)


def readback(cfg):
    # Ops login is checked separately. This bearer is sent only to the local Ops API.
    transport = Transport('http://127.0.0.1:' + str(cfg['port']), cfg['access_file'], timeout=30)
    _, body = transport.get('/api/resources')
    rows = body.get('metrics')
    require(isinstance(rows, dict) and set(rows) == {'targets', 'host_cpu', 'host_memory', 'filesystems',
            'container_cpu', 'container_memory', 'gpu'}, 'OPS_RESOURCE_READBACK_INVALID')
    result = {}
    for key, row in rows.items():
        data = row.get('data') or {}
        require(row.get('status') == 'ok' and data.get('count', 0) > 0 and data.get('coverage') == 'query_result'
                and len(data.get('items', [])) == data['count']
                and all(x.get('value') is not None for x in data['items'])
                and (key != 'targets' or all(x['value'] == 1 for x in data['items'])), 'OPS_RESOURCE_READBACK_FAILED')
        result[key] = {'status': 'ok', 'count': data['count']}
    _, alerts = transport.get('/api/alerts')
    require(alerts.get('status') == 'ok' and all(alerts.get('sources', {}).get(k, {}).get('status') == 'ok'
            for k in ('prometheus', 'alertmanager')), 'OPS_ALERT_READBACK_FAILED')
    return {'resources': result, 'alert_sources': ['prometheus', 'alertmanager']}


def verify(directory, record, docker, api_factory, readback_fn, source_fn):
    b = record['baseline']; cache = guard(directory, b)
    cfg = manage.existing(directory); ident = b['container']['identity']['id']
    current = url_change.current(docker, directory, cfg, b['owner'], ident)
    require(current['identity'] == b['container']['identity'] and current['healthy'], 'OPS_ACTIVATION_NOT_READY')
    if b['plan']['restart_required']:
        intent = record['restart_intent']; require(intent, 'ACTIVATION_RESTART_NOT_REQUESTED')
        require(rc.date(current['started']) > rc.date(b['container']['started']) and
                rc.date(current['started']) >= rc.date(intent['at']), 'OPS_ACTIVATION_NOT_READY')
    else:
        require(current == b['container'], 'UNEXPECTED_OPS_RESTART')
    require(docker.mounted_hash(ident) == sha(config_bytes(b, 'config.json', True)), 'MOUNTED_CONFIG_NOT_ACTIVATED')
    require(source_fn() == b['source_sha256'], 'DISCOVERY_BINDING_CHANGED')
    url_change.protect(docker, b)
    api = api_factory(cfg); api.health()
    cookie = api.login()
    try: checks = readback_fn(cfg)
    finally: api.logout(cookie)
    cache = guard(directory, b); url_change.protect(docker, b)
    require(url_change.current(docker, directory, cfg, b['owner'], ident) == current, 'OPS_CHANGED_DURING_VERIFY')
    return {'stage': KIND, 'result': 'passed', 'checked_at': rc.now(), 'scope': 'existing_ops_configuration_activation',
            'changed_fields': b['plan']['changed_fields'], 'provenance_recorded': True,
            'preserved_overrides_differing_from_discovery': [key for key, field in b['plan']['fields'].items()
                if field.get('matches_discovery') is False],
            'existing_values_preserved': True, 'credentials_preserved': True, 'public_url_preserved': True,
            'same_ops_container_image_mounts': True, 'container_id': ident, 'started_after': current['started'],
            'restart_requested': bool(record['restart_intent']), 'restart_command_completion_recorded': record['restart_command_completed'],
            'cache': cache, 'prior_acceptance_records_preserved': True,
            'protected_containers_unchanged': len(b['protected']), 'same_access_code_login_logout': True,
            'ops_readback': checks, 'build_requested': False, 'new_container_requested': False,
            'other_service_control_requested': False, 'inference_requested': False,
            'continuous_refresh_supported': False, 'kubernetes_activation_supported': False,
            'live_checks_performed': True}


def rollback(directory, record, path, docker, api_factory):
    b = record['baseline']; guard(directory, b, 'partial')
    old = strict_json(b['original']['host-config.json']); ident = b['container']['identity']['id']
    current = url_change.current(docker, directory, old, b['owner'], ident)
    require(current['identity'] == b['container']['identity'], 'OPS_IDENTITY_CHANGED')
    url_change.protect(docker, b)
    rb = record.get('rollback')
    if rb is None:
        if record['restart_intent'] and not record['restart_command_completed']:
            require(current['healthy'] and rc.date(current['started']) > rc.date(b['container']['started']),
                    'RESTART_EFFECT_UNKNOWN_CHECK_FIRST')
        rb = {'container_before': current, 'restart_required': bool(record['restart_intent']),
              'restart_intent': None, 'restart_command_completed': False}
        record.update(rollback=rb, phase='rolling_back'); rc.write_record(path, record)
    for name in CONFIGS:
        raw = config_bytes(b, name, False)
        if rc.read_file(directory / name, private=True)[0] != raw:
            atomic_config(directory / name, raw, b['files'][name])
    manage.existing(directory); guard(directory, b, 'original'); url_change.protect(docker, b)
    if rb['restart_required'] and rb['restart_intent'] is None:
        require(url_change.current(docker, directory, old, b['owner'], ident) == rb['container_before'], 'OPS_CHANGED_BEFORE_ROLLBACK')
        rb['restart_intent'] = {'at': rc.now(), 'id': ident}; rc.write_record(path, record)
        docker.restart(ident); rb['restart_command_completed'] = True; rc.write_record(path, record)
    observed = url_change.current(docker, directory, old, b['owner'], ident)
    require(observed['healthy'] and observed['identity'] == b['container']['identity'], 'ROLLBACK_NOT_READY')
    if rb['restart_required']:
        require(rc.date(observed['started']) > rc.date(rb['container_before']['started']) and
                rc.date(observed['started']) >= rc.date(rb['restart_intent']['at']), 'ROLLBACK_NOT_READY')
    else: require(observed == rb['container_before'], 'UNEXPECTED_OPS_RESTART')
    require(docker.mounted_hash(ident) == b['files']['config.json']['sha256'], 'ROLLBACK_MOUNT_NOT_READY')
    api = api_factory(old); api.health(); cookie = api.login(); api.logout(cookie)
    cache = guard(directory, b, 'original'); url_change.protect(docker, b)
    report = {'stage': KIND, 'result': 'rolled_back', 'checked_at': rc.now(), 'original_config_bytes_restored': True,
              'same_ops_container_image_mounts': True, 'credentials_preserved': True, 'cache': cache,
              'prior_acceptance_records_preserved': True, 'restart_requested': rb['restart_required'],
              'other_service_control_requested': False, 'inference_requested': False}
    require(url_change.current(docker, directory, old, b['owner'], ident) == observed, 'OPS_CHANGED_DURING_ROLLBACK')
    record.update(phase='rolled_back', rollback_report=report, rollback_container=observed); rc.write_record(path, record)
    return report


def run(action, directory, docker=None, discover_fn=discover_now, validate_fn=validate_effective,
        source_fn=source_snapshot, api_factory=rc.API, readback_fn=readback, wait_seconds=60):
    directory = manage.safe(directory)
    require(not (directory / 'discovery-bootstrap').exists() and not (directory / 'discovery-bootstrap').is_symlink(),
            'BOOTSTRAP_USE_REFRESH')
    with url_change.change_lock(directory):
        docker = docker or Docker(project=manage.prepared_project(directory))
        folder = directory / 'discovery-activation'; path = folder / 'baseline.json'
        if folder.exists() or folder.is_symlink(): manage.private(folder)
        if action == 'plan':
            require(not path.exists(), 'ACTIVATION_RECORD_EXISTS_USE_CHECK')
            record = capture(directory, docker, discover_fn, validate_fn, source_fn, api_factory)
            return {'stage': KIND, 'result': 'planned', 'plan': record['baseline']['plan'],
                    'configuration_written': False, 'restart_requested': False, 'inference_requested': False}
        if path.exists() or path.is_symlink(): record = load_record(path, directory)
        else:
            require(action == 'apply', 'NO_ACTIVATION_RECORD')
            record = capture(directory, docker, discover_fn, validate_fn, source_fn, api_factory)
            folder.mkdir(mode=0o700, exist_ok=True); rc.write_record(path, record)
        if record.get('phase') == 'rolled_back':
            b = record['baseline']; guard(directory, b, 'original')
            require(url_change.current(docker, directory, manage.existing(directory), b['owner'], b['container']['identity']['id'])
                    == record['rollback_container'], 'ROLLED_BACK_OPS_CONTAINER_CHANGED')
            return dict(record['rollback_report'], evidence_reused=True, live_checks_performed=False)
        if record.get('report') and action != 'rollback':
            guard(directory, record['baseline'])
            b = record['baseline']; cfg = manage.existing(directory)
            require(url_change.current(docker, directory, cfg, b['owner'], b['container']['identity']['id']) == record['accepted_container'],
                    'ACCEPTED_OPS_CONTAINER_CHANGED')
            return dict(record['report'], evidence_reused=True, live_checks_performed=False)
        if action != 'rollback':
            require(not record.get('rollback'), 'ROLLBACK_IN_PROGRESS_USE_ROLLBACK')
            if action == 'apply': install(directory, record, path, docker, source_fn, validate_fn)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                if action == 'rollback': return rollback(directory, record, path, docker, api_factory)
                report = verify(directory, record, docker, api_factory, readback_fn, source_fn)
                break
            except ReadFailure as exc:
                if exc.code not in ('OPS_ACTIVATION_NOT_READY', 'MOUNTED_CONFIG_NOT_ACTIVATED',
                                    'ROLLBACK_NOT_READY', 'ROLLBACK_MOUNT_NOT_READY', 'CONNECTION_FAILED', 'TIMEOUT') \
                        or time.monotonic() >= deadline: raise
                time.sleep(.5)
        b = record['baseline']
        record['accepted_container'] = url_change.current(docker, directory, manage.existing(directory), b['owner'], b['container']['identity']['id'])
        require(record['accepted_container']['started'] == report['started_after'], 'OPS_CHANGED_AFTER_VERIFY')
        record.update(phase='accepted', report=report); rc.write_record(path, record)
        return dict(report, evidence_reused=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply', 'check', 'rollback'))
    parser.add_argument('--state-dir', type=Path, default=manage.STATE)
    args = parser.parse_args()
    try: result = run(args.action, args.state_dir)
    except (ReadFailure, Unavailable, OSError, ValueError, KeyError, TypeError) as exc:
        result = {'stage': KIND, 'result': 'attention_required',
                  'code': exc.code if isinstance(exc, ReadFailure) else str(exc) if isinstance(exc, Unavailable) else 'LOCAL_CONFIG_OR_COMMAND_ERROR',
                  'guidance': 'Keep discovery-activation/baseline.json. apply resumes partial writes; check never restarts; rollback restores only recorded settings.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] in ('planned', 'passed', 'rolled_back') else 2


if __name__ == '__main__': raise SystemExit(main())
