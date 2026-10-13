"""Read-only prepared-state projection. Never load secret contents or create locks."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from infra.observability.ops import backup_preflight as fs
from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import manage, refresh_discovery as refresh, activate_discovery as activation
from ops_platform.ops import bootstrap_discovery as bootstrap
from ops_platform.ops.refresh_plan import FIELDS, value
from ops_platform.resource_profiles import profile
from ops_platform.transport import base_url, strict_json


def error_code(exc):
    if isinstance(exc, (ReadFailure, fs.Unavailable)):
        code = exc.code if isinstance(exc, ReadFailure) else str(exc)
        if re.fullmatch('[A-Z][A-Z0-9_]{0,95}', code): return code
    if isinstance(exc, FileNotFoundError): return 'LOCAL_FILE_MISSING'
    if isinstance(exc, PermissionError): return 'LOCAL_PERMISSION_DENIED'
    return 'INVALID_LOCAL_STATE'


ERRORS = (ReadFailure, fs.Unavailable, OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError)


class FileCheckFailure(ReadFailure):
    def __init__(self, diagnostic):
        super().__init__(diagnostic['code'])
        self.file_diagnostic = diagnostic


def metadata(path, role, private, directory=False, limit=None):
    """Inspect a named reference without opening it; preserve external ownership contracts."""
    path = Path(path)
    row = {'role': role, 'path': str(path), 'policy': 'private_ops_state' if private else 'existing_external_reference',
           'status': 'error', 'contents_read': False}
    try:
        info = fs.metadata(path, directory=directory)
        row.update(uid=info.st_uid, gid=info.st_gid, mode=format(stat.S_IMODE(info.st_mode), '04o'),
                   owner_matches=info.st_uid == os.getuid(), private_mode=not bool(info.st_mode & 0o077))
        readable = os.access(path, os.R_OK | (os.X_OK if directory else 0), effective_ids=True, follow_symlinks=False)
        row['readable_by_current_user'] = readable
        if private:
            require(row['owner_matches'] and row['private_mode'], 'PRIVATE_OWNER_MODE_MISMATCH')
        require(readable, 'REFERENCE_NOT_READABLE')
        if limit is not None: require(0 < info.st_size <= limit, 'INVALID_CREDENTIAL_FILE_SIZE')
        row['status'] = 'ok'
        if not private and not (row['owner_matches'] and row['private_mode']):
            row.update(status='warning', code='EXTERNAL_REFERENCE_PRIVATE_MODE_DIFFERS')
    except ERRORS as exc: row.update(status='error', code=error_code(exc))
    return row


def read(path, limit=65536):
    checked = metadata(path, Path(path).name, private=True)
    if checked['status'] == 'error':
        # Keep the established code for private Ops source-file failures.
        if checked['code'] == 'PRIVATE_OWNER_MODE_MISMATCH': checked['code'] = 'PRIVATE_STATE_REQUIRED'
        if checked['code'] == 'WRONG_FILE_TYPE': checked['code'] = 'INVALID_LOCAL_FILE'
        raise FileCheckFailure(checked)
    parent = fs.open_directory(Path(path).parent)
    try: fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally: os.close(parent)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'INVALID_LOCAL_FILE')
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'PRIVATE_STATE_REQUIRED')
        data = source.read(limit + 1)
        require(len(data) <= limit, 'INVALID_LOCAL_FILE')
        return data


@contextmanager
def snapshot_lock(directory):
    fd = fs.open_directory(directory)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'PRIVATE_STATE_REQUIRED')
        try: fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError: raise ReadFailure('PREPARED_STATE_BUSY') from None
        yield
    finally: os.close(fd)


def config(directory, owner):
    """Validate structure, paired config and references without settings.load/secret()."""
    cfg = strict_json(read(directory / 'host-config.json'))
    expected_keys = {'schema', 'platforms', 'prometheus_url', 'langfuse_url', 'langfuse_file',
                     'access_file', 'state_file', 'public_url', 'bind', 'port', 'timeout', 'tls_cert', 'tls_key'}
    require(isinstance(cfg, dict) and expected_keys <= cfg.keys() <= expected_keys | {'alertmanager_url', 'resource_profile'}
            and cfg['schema'] == 1, 'INVALID_CONFIG')
    require(cfg['bind'] in ('0.0.0.0', '127.0.0.1') and type(cfg['port']) is int and 1024 <= cfg['port'] <= 65535,
            'INVALID_BIND_OR_PORT')
    require(type(cfg['timeout']) in (int, float) and .1 <= cfg['timeout'] <= 10, 'INVALID_TIMEOUT')
    public = base_url(cfg['public_url'])
    require((public.port or (443 if public.scheme == 'https' else 80)) == cfg['port'], 'PUBLIC_PORT_MISMATCH')
    require(bool(cfg['tls_cert']) == bool(cfg['tls_key']) == (public.scheme == 'https'), 'TLS_CONFIG_MISMATCH')
    for key in ('prometheus_url', 'langfuse_url', 'alertmanager_url'):
        if key in cfg: base_url(cfg[key])
    require(isinstance(cfg['platforms'], dict) and set(cfg['platforms']) == {'model', 'data', 'knowledge', 'agent'}, 'INVALID_CONFIG')
    references = [(directory / 'access-key', 'ops_access_key', True, 4096),
                  (Path(cfg['langfuse_file']), 'langfuse_credentials', False, 16384)]
    for name, row in cfg['platforms'].items():
        require(isinstance(row, dict) and {'url'} <= row.keys() <= {'url', 'enabled', 'token_file'}, 'INVALID_CONFIG')
        base_url(row['url']); require(type(row.get('enabled', True)) is bool, 'INVALID_CONFIG')
        require(name != 'agent' or bool(row.get('token_file')), 'INVALID_CONFIG')
        if row.get('token_file'): references.append((Path(row['token_file']), name + '_token', False, 4096))
    effective = profile(cfg.get('resource_profile'))
    require(effective['environment'] == 'docker-compose', 'KUBERNETES_LIFECYCLE_DEFERRED')
    require(cfg['access_file'] == str(directory / 'access-key') and cfg['state_file'] == str(directory / 'cache/api-success.json'),
            'STATE_PATH_MISMATCH')
    for key in ('tls_cert', 'tls_key'):
        if cfg[key]: references.append((Path(cfg[key]), key, False, None))
    container = strict_json(read(directory / 'config.json'))
    wanted = deepcopy(cfg)
    wanted.update(langfuse_file='/run/ops/langfuse.json', access_file='/run/ops/access-key', state_file='/var/lib/ops/api-success.json')
    wanted['platforms']['agent']['token_file'] = '/run/ops/agent-token'
    require(container == wanted, 'HOST_CONTAINER_CONFIG_MISMATCH')
    env = {'OPS_UID': str(owner['uid']), 'OPS_GID': str(owner['gid']), 'OPS_OWNER': owner['owner'],
           'OPS_CONFIG_FILE': str(directory / 'config.json'), 'OPS_ACCESS_FILE': str(directory / 'access-key'),
           'OPS_AGENT_TOKEN_FILE': cfg['platforms']['agent']['token_file'], 'OPS_LANGFUSE_FILE': cfg['langfuse_file'],
           'OPS_CACHE_DIR': str(directory / 'cache')}
    require(read(directory / 'compose.env').decode() == ''.join(k + "='" + v + "'\n" for k, v in env.items()), 'COMPOSE_ENV_MISMATCH')
    checks = [metadata(path, role, private, limit=limit) for path, role, private, limit in references]
    checks.append(metadata(directory / 'cache', 'ops_cache_directory', True, directory=True))
    return cfg, checks


def provenance(directory, owner):
    """Reuse existing bounded lineage validation; expose no recorded configurations."""
    anchor = directory / 'discovery-activation/baseline.json'
    initial = directory / bootstrap.RELATIVE
    has_activation = anchor.exists() or anchor.is_symlink()
    has_bootstrap = initial.exists() or initial.is_symlink()
    require(not (has_activation and has_bootstrap), 'MULTIPLE_DISCOVERY_ANCHORS')
    require(has_activation or has_bootstrap, 'DISCOVERY_PROVENANCE_MISSING')
    if has_activation:
        record = activation.load_record(anchor, directory)
        require(record['baseline']['owner'] == owner, 'PROVENANCE_OWNER_MISMATCH')
        phase = record.get('phase')
        require(phase in ('prepared', 'restart_requested', 'accepted', 'rolling_back', 'rolled_back'), 'ACTIVATION_RECORD_INVALID')
        if phase != 'accepted':
            return {'status': 'recovery_required' if phase != 'rolled_back' else 'blocked',
                    'anchor': 'activation', 'code': 'ACTIVATION_PENDING' if phase != 'rolled_back' else 'ACCEPTED_ACTIVATION_REQUIRED',
                    'pending': {'kind': 'activation', 'generation': None, 'phase': phase},
                    'recovery_tool': 'activate_discovery.py', 'managed_fields': [],
                    'record_fingerprint': hashlib.sha256(read(anchor, 512*1024)).hexdigest()}, None
    else:
        bootstrap.load_anchor(directory)
    context = refresh.lineage(directory)
    for _, record in context['records']:
        require(record['baseline']['owner'] == owner, 'PROVENANCE_OWNER_MISMATCH')
    fields = context['fields']
    require(set(fields) == FIELDS, 'PROVENANCE_FIELDS_INVALID')
    for field in fields.values():
        require(isinstance(field, dict) and field.get('source') in ('discovery', 'existing_preserved', 'user_override')
                and type(field.get('automatic_future_update_allowed')) is bool and 'value' in field, 'PROVENANCE_FIELDS_INVALID')
        require(not field['automatic_future_update_allowed'] or field['source'] == 'discovery', 'PROVENANCE_FIELDS_INVALID')
    pending = context['pending']
    result = {'status': 'recovery_required' if pending else 'ok', 'anchor': 'activation' if has_activation else 'bootstrap',
              'generations': len(context['records']), 'history_limit': refresh.MAX_GENERATIONS,
              'next_generation': context['next'], 'pending': None, 'recovery_tool': None,
              'managed_fields': sorted(k for k, f in fields.items() if f['automatic_future_update_allowed']),
              'rejected_proposal_preserved': context['rejected'] is not None,
              'record_fingerprint': hashlib.sha256(json.dumps(context['evidence'], sort_keys=True).encode()).hexdigest()}
    if pending:
        result.update(code='REFRESH_PENDING_USE_CHECK_OR_ROLLBACK', recovery_tool='refresh_discovery.py',
                      pending={'kind': 'refresh', 'generation': pending[1]['baseline']['refresh_generation'], 'phase': pending[1]['phase']})
    elif context['next'] > refresh.MAX_GENERATIONS:
        result.update(status='blocked', code='REFRESH_HISTORY_LIMIT')
    return result, fields


def inspect_state(directory, root):
    result = {'status': 'unavailable', 'state_dir': str(directory), 'configuration': {'status': 'not_checked'},
              'provenance': {'status': 'not_checked'}}
    cfg = owner = None
    try:
        directory = manage.safe(directory)
        # A missing directory is an observation, not a request to prepare it.
        if not directory.exists():
            result.update(status='missing', code='PREPARED_STATE_NOT_FOUND'); return result, None, None
        with snapshot_lock(directory):
            owner = strict_json(read(directory / 'owner.json'))
            require(isinstance(owner, dict) and owner.get('schema') == 1 and type(owner.get('uid')) is int
                    and type(owner.get('gid')) is int and owner['uid'] == os.getuid() and owner['gid'] == os.getgid()
                    and owner.get('project_root') == str(root), 'OWNER_MISMATCH')
            require(str(uuid.UUID(owner['owner'])) == owner['owner'], 'OWNER_MISMATCH')
            project = manage.project_name(owner.get('compose_project', manage.DEFAULT_PROJECT))
            result.update(status='ok', installation_id=owner['owner'], compose_project=project)
            try:
                result['provenance'], fields = provenance(directory, owner)
            except ERRORS as exc:
                result['provenance'] = {'status': 'error', 'code': error_code(exc)}; fields = None
            try:
                cfg, checks = config(directory, owner)
                result['configuration'] = {'status': 'ok', 'credential_check': 'file_metadata_only',
                                           'credential_contents_verified': False, 'runtime_mount_contents_verified': False,
                                           'file_checks': checks}
                failures = [row for row in checks if row['status'] == 'error']
                if failures: result['configuration'].update(status='error', code=failures[0]['code'])
                if fields:
                    edited = sorted(k for k, f in fields.items() if value(cfg, k) != f['value'])
                    result['provenance']['manual_overrides_detected'] = edited
                    result['provenance']['effective_managed_fields'] = [k for k in result['provenance']['managed_fields'] if k not in edited]
                    result['provenance']['ownership_written'] = False
            except ERRORS as exc:
                result['configuration'] = {'status': 'error', 'code': error_code(exc)}
                if isinstance(exc, FileCheckFailure): result['configuration']['file_checks'] = [exc.file_diagnostic]
            if any(result[k]['status'] != 'ok' for k in ('configuration', 'provenance')):
                result['status'] = 'attention_required'
            if cfg is not None:
                result['configuration']['files_fingerprint'] = hashlib.sha256(b'\0'.join(
                    read(directory / name) for name in ('owner.json', 'host-config.json', 'config.json', 'compose.env'))).hexdigest()
    except ERRORS as exc:
        result.update(status='error', code=error_code(exc)); cfg = owner = None
        if isinstance(exc, FileCheckFailure): result['file_check'] = exc.file_diagnostic
    return result, cfg, owner
