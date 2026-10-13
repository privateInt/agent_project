#!/usr/bin/env python3
"""Discover first-install settings without fixed upstream endpoints or activation."""
import argparse
from copy import deepcopy
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infra.observability.ops.backup_preflight import Unavailable
from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import discover, manage, restart_check as rc, set_public_url as change
from ops_platform.ops.activation_plan import PLATFORMS, validate_effective
from ops_platform.ops.discovery_compose import DockerDiscovery, scoped
from ops_platform.ops.refresh_plan import FIELDS, URLS, value
from ops_platform.resource_profiles import validate_profile
from ops_platform.transport import base_url, strict_json

STAGE = 'O04-first-install-discovery'
RELATIVE = 'discovery-bootstrap/baseline.json'


class DiscoveryNotReady(ReadFailure):
    def __init__(self, report):
        super().__init__('FIRST_INSTALL_DISCOVERY_NOT_READY')
        self.details = {'services': {role: {k: row[k] for k in ('status', 'candidate_count', 'error') if k in row}
                                    for role, row in report['services'].items()},
                        'metrics': {k: report['metrics'][k] for k in
                                    ('status', 'reason', 'error', 'active_target_count', 'fresh_healthy_target_count')
                                    if k in (report.get('metrics') or {})},
                        'deployment_metadata_stable': report.get('deployment_metadata_stable')}


def propose(root, docker=None, validate_fn=validate_effective):
    """Use the same discovery contracts as refresh, without an existing config."""
    root = manage.safe(root)
    docker = docker or DockerDiscovery(); docker.host()
    fingerprint = lambda: change.digest(scoped(docker.inventory(), root))
    before = fingerprint()
    seed = {'timeout': 5, 'platforms': {role: {'url': None} for role in PLATFORMS}}
    report = discover.discover(seed, root, docker)
    if report.get('result') != 'discovered' or not report['deployment_metadata_stable']:
        raise DiscoveryNotReady(report)
    cfg = deepcopy(report['connection_overlay']); cfg['timeout'] = seed['timeout']
    fields = {}
    for key, role in URLS.items():
        endpoint = value(cfg, key); base_url(endpoint)
        fields[key] = {'source': 'discovery', 'value': endpoint, 'discovered_value': endpoint,
                       'identity': deepcopy(report['services'][role]['identity']),
                       'matches_discovery': True, 'automatic_future_update_allowed': True}
    profile = validate_profile(cfg['resource_profile'])
    fields['resource_profile'] = {'source': 'discovery', 'value': profile,
                                 'prometheus_identity': deepcopy(report['services']['prometheus']['identity']),
                                 'automatic_future_update_allowed': True}
    validation = validate_fn(cfg)
    require(fingerprint() == before, 'DISCOVERY_BINDING_CHANGED')
    return {'configuration': cfg, 'fields': fields, 'source_sha256': before,
            'checked_at': report['checked_at'], 'effective_profile_validation': validation,
            'active_target_count': report['metrics']['active_target_count'],
            'fresh_healthy_target_count': report['metrics']['fresh_healthy_target_count']}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def publish_no_replace(source, destination):
    """Linux atomic publication. Never replace even a concurrently created empty dir."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    require(rename is not None, 'ATOMIC_PREPARE_UNSUPPORTED')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY): raise ReadFailure('PREPARE_DESTINATION_EXISTS')
        raise ReadFailure('ATOMIC_PREPARE_FAILED')


def publish(directory, cfg, proposal, source_fn=None, compose_project=None):
    """Validate a private stage, then publish a complete new installation once."""
    directory = manage.safe(directory)
    require(not directory.exists(), 'PREPARE_DESTINATION_EXISTS')
    require(set(proposal['fields']) == FIELDS, 'PROVENANCE_FIELDS_INVALID')
    require(all(value(cfg, key) == item['value'] for key, item in proposal['fields'].items()),
            'BOOTSTRAP_CONFIGURATION_CHANGED')
    if source_fn is None:
        docker = DockerDiscovery(); docker.host()
        source_fn = lambda: change.digest(scoped(docker.inventory(), manage.ROOT.parent))
    require(source_fn() == proposal['source_sha256'], 'DISCOVERY_BINDING_CHANGED')
    directory.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    manage.safe(directory.parent)
    stage = Path(tempfile.mkdtemp(prefix='.ops-prepare-', dir=directory.parent))
    try:
        owner = str(uuid.uuid4())
        (stage / 'cache').mkdir(mode=0o700)
        manage.write_new(stage / 'access-key', secrets.token_urlsafe(32) + '\n')
        for name, document in manage.state_documents(stage, cfg, owner, compose_project).items():
            manage.write_new(stage / name, document)
        manage.existing(stage)
        # Rewrite only our unpublished files to reference the final location.
        for name, document in manage.state_documents(directory, cfg, owner, compose_project).items():
            (stage / name).unlink(); manage.write_new(stage / name, document)
        baseline = {'directory': str(directory), 'owner': json.loads((stage / 'owner.json').read_text()),
                    'checked_at': proposal['checked_at'], 'fields': deepcopy(proposal['fields']),
                    'source_sha256': proposal['source_sha256']}
        record = {'schema': 1, 'kind': STAGE, 'phase': 'configuration_prepared',
                  'runtime_acceptance': 'pending_first_start_and_readback',
                  'baseline': baseline, 'baseline_sha256': change.digest(baseline)}
        (stage / 'discovery-bootstrap').mkdir(mode=0o700)
        manage.write_new(stage / RELATIVE, record)
        require(source_fn() == proposal['source_sha256'], 'DISCOVERY_BINDING_CHANGED')
        manage.safe(directory)
        for folder in (stage / 'cache', stage / 'discovery-bootstrap', stage): sync_directory(folder)
        publish_no_replace(stage, directory)
        sync_directory(directory.parent)
    finally:
        # Only a stage created by this call is removed. Published/foreign state is untouched.
        if stage.exists(): shutil.rmtree(stage)
    manage.existing(directory)
    load_anchor(directory)


def load_anchor(directory):
    record = strict_json(rc.read_file(directory / RELATIVE, 512*1024, private=True)[0])
    b = record['baseline']
    require(record.get('schema') == 1 and record.get('kind') == STAGE
            and record.get('phase') == 'configuration_prepared'
            and record.get('runtime_acceptance') == 'pending_first_start_and_readback'
            and b['directory'] == str(directory) and record.get('baseline_sha256') == change.digest(b)
            and b['owner'] == strict_json(rc.read_file(directory / 'owner.json', private=True)[0]),
            'BOOTSTRAP_RECORD_INVALID')
    require(set(b['fields']) == FIELDS and re.fullmatch('[0-9a-f]{64}', b['source_sha256']), 'BOOTSTRAP_RECORD_INVALID')
    for key, field in b['fields'].items():
        require(field.get('source') == 'discovery' and field.get('automatic_future_update_allowed') is True,
                'BOOTSTRAP_RECORD_INVALID')
        if key == 'resource_profile':
            validate_profile(field['value']); identity = field['prometheus_identity']
        else:
            base_url(field['value']); identity = field['identity']
        require(identity.get('provider') == 'docker-compose' and bool(identity.get('project'))
                and bool(identity.get('service')), 'BOOTSTRAP_RECORD_INVALID')
    return record


def state_stamp(directory):
    """Observe existing state without making a cache, lock or provenance journal."""
    names = ['owner.json', 'host-config.json', 'config.json', 'compose.env', 'access-key',
             'cache/api-success.json', 'cache/api-success.json.lock', RELATIVE,
             'discovery-activation/baseline.json']
    for folder in ('discovery-refresh', 'restart-check', 'public-url-change'):
        parent = directory / folder
        if parent.exists() or parent.is_symlink():
            manage.private(parent)
            names.extend(str(p.relative_to(directory)) for p in parent.rglob('baseline.json'))
    return {name: rc.read_file(directory / name, 512*1024, private=True)[1]
            if (directory / name).exists() or (directory / name).is_symlink() else None for name in sorted(set(names))}


def plan(directory, propose_fn=None):
    """Native read-only acceptance on an already running host; no second Ops install."""
    directory = manage.safe(directory)
    present = directory.exists()
    before = state_stamp(directory) if present else None
    reuse = manage.prepare(directory) if present else None
    found = (propose_fn or propose)(manage.ROOT.parent)
    stable = before == (state_stamp(directory) if present else None) and directory.exists() == present
    require(stable, 'PREPARED_STATE_CHANGED_DURING_PLAN')
    return {'schema': 1, 'stage': STAGE, 'result': 'passed', 'checked_at': rc.now(),
            'scope': 'read_only_first_install_plan', 'provider': 'docker-compose',
            'services': {role: value(found['configuration'], key) for key, role in URLS.items()},
            'active_target_count': found['active_target_count'],
            'fresh_healthy_target_count': found['fresh_healthy_target_count'],
            'resource_validation': found['effective_profile_validation'],
            'managed_fields_for_new_install': sorted(found['fields']),
            'existing_preparation_reused': reuse is not None and reuse['result'] == 'reused',
            'existing_prepared_state_unchanged': stable if present else None,
            'configuration_written': False, 'provenance_written': False, 'service_control_requested': False,
            'credentials_sent': False, 'inference_requested': False,
            'new_install_runtime_acceptance': False, 'watch_enabled': False,
            'kubernetes_supported': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan',))
    parser.add_argument('--state-dir', type=Path, default=manage.STATE)
    args = parser.parse_args()
    try: result = plan(args.state_dir)
    except (ReadFailure, Unavailable, OSError, ValueError, TypeError, KeyError) as exc:
        code = exc.code if isinstance(exc, ReadFailure) else str(exc) if isinstance(exc, Unavailable) else 'BOOTSTRAP_INPUT_UNAVAILABLE'
        result = {'stage': STAGE, 'result': 'attention_required', 'code': code,
                  'configuration_written': False, 'service_control_requested': False, 'inference_requested': False,
                  'guidance': 'Discovery needs seven unique healthy Compose services and fresh metric roles; use discover.py on an existing installation for role diagnostics.'}
        if getattr(exc, 'details', None): result['details'] = exc.details
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
