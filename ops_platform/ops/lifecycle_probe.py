"""Allowlisted local Docker/systemd metadata queries for lifecycle preflight."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from urllib.parse import quote

from infra.observability.ops import backup_preflight as fs
from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import restart_check as rc
from ops_platform.transport import strict_json

INFO = '{"id":{{json .ID}},"os":{{json .OSType}},"security":{{json .SecurityOptions}},"version":{{json .ServerVersion}}}'
CONTEXT = '{{.Endpoints.docker.Host}}'
MANAGER_PROPERTIES = 'Version,SystemState'
LOGIN_PROPERTIES = 'UID,Linger,RuntimePath,State'
SYSTEM_USER_PROPERTIES = 'Id,LoadState,ActiveState,SubState,Result'
UNIT_PROPERTIES = 'Id,LoadState,ActiveState,SubState,UnitFileState,MainPID,InvocationID,Result,ExecMainStatus,FragmentPath'
UNIT = re.compile(r'agent-project-ops-refresh-[0-9a-f]{32}\.service')
HEX = re.compile('[0-9a-f]{64}')


def login_args():
    # loginctl takes one property name per option; unlike systemctl, do not rely on comma splitting.
    return ['--no-pager', '--no-ask-password', 'show-user', str(os.getuid()), '--all',
            *['--property=' + name for name in LOGIN_PROPERTIES.split(',')]]


def system_user_args():
    return ['--system', '--no-pager', '--no-ask-password', 'show',
            '--property=' + SYSTEM_USER_PROPERTIES, 'user@' + str(os.getuid()) + '.service']


def local_endpoint(value):
    require(isinstance(value, str) and value.startswith('unix:///') and len(value) < 4096
            and not any(ord(c) < 32 or c in '\x7f?#' for c in value), 'LOCAL_DOCKER_REQUIRED')
    return value


def allow(tool, args):
    if tool == 'docker':
        if args == ['context', 'show']: return True
        if len(args) == 5 and args[:2] == ['context', 'inspect'] and args[3:] == ['--format', CONTEXT]:
            return re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}', args[2]) is not None
        if len(args) < 3 or args[0] != '--host': return False
        local_endpoint(args[1]); tail = args[2:]
        if tail in (['info', '--format', INFO], ['compose', 'version', '--short']): return True
        if len(tail) == 9 and tail[:4] == ['ps', '-a', '--no-trunc', '--filter'] and tail[5:7] == ['--filter', 'label=com.docker.compose.service=ops-web'] and tail[7:] == ['--format', '{{.ID}}']:
            return re.fullmatch('label=com.docker.compose.project=[a-z0-9][a-z0-9_-]{0,62}', tail[4]) is not None
        return len(tail) == 6 and tail[:5] == ['container', 'inspect', '--format', rc.INSPECT, '--'] and HEX.fullmatch(tail[5]) is not None
    if tool == 'systemctl':
        if args == ['--version']: return True
        if args == system_user_args(): return True
        if args == ['--user', '--no-pager', '--no-ask-password', 'show', '--property=' + MANAGER_PROPERTIES]: return True
        return (len(args) == 6 and args[:4] == ['--user', '--no-pager', '--no-ask-password', 'show']
                and args[4] == '--property=' + UNIT_PROPERTIES and UNIT.fullmatch(args[5]) is not None)
    if tool == 'loginctl':
        return args in (['--no-pager', '--no-ask-password', 'show-user', str(os.getuid()), '--property=Linger', '--value'],
                        login_args())
    return False


def user_bus_reason(stderr):
    """Only a fixed category leaves the command boundary, never raw error text."""
    text = stderr[:8192].lower()
    for needle, code in (('permission denied', 'USER_BUS_PERMISSION_DENIED'), ('access denied', 'USER_BUS_PERMISSION_DENIED'),
                         ('operation not permitted', 'USER_BUS_PERMISSION_DENIED'), ('no medium found', 'USER_BUS_ADDRESS_MISSING'),
                         ('no such file', 'USER_BUS_SOCKET_MISSING'), ('connection refused', 'USER_BUS_CONNECTION_REFUSED')):
        if needle in text: return code
    return 'USER_BUS_QUERY_FAILED'


def validate_runtime(path):
    """Accept only the current user's existing private directory and owned bus socket."""
    path = fs.safe(Path(path))
    fs.metadata(path, private=True, directory=True)
    parent = fs.open_directory(path)
    try: info = os.stat('bus', dir_fd=parent, follow_symlinks=False)
    finally: os.close(parent)
    require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid(), 'USER_BUS_SOCKET_OWNER_OR_TYPE_INVALID')
    return str(path)


def runtime_metadata(value):
    """Inspect the selected current-user directory/socket; no connection or path guessing."""
    from ops_platform.ops.lifecycle_state import ERRORS, error_code
    result = {'status': 'not_set', 'bus': {'status': 'not_checked'}}
    if not value: return result
    try:
        require(isinstance(value, str) and len(value) <= 4096, 'INVALID_PATH')
        path = fs.safe(Path(value))
        result['path'] = str(path)
        info = fs.metadata(path, directory=True)
        result.update(uid=info.st_uid, gid=info.st_gid, mode=format(stat.S_IMODE(info.st_mode), '04o'))
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'PRIVATE_OWNER_MODE_MISMATCH')
        result['status'] = 'ok'
    except ERRORS as exc:
        result.update(status='unavailable', code=error_code(exc)); return result
    try:
        # Recheck ownership/private mode at this boundary; validation never creates or connects to a socket.
        validate_runtime(str(path))
        result['bus'] = {'status': 'ok', 'socket_owner_verified': True, 'connection_verified': False}
    except ERRORS as exc:
        code = 'USER_BUS_SOCKET_MISSING' if isinstance(exc, FileNotFoundError) else error_code(exc)
        result['bus'] = {'status': 'unavailable', 'code': code, 'connection_verified': False}
    return result


def system_user_metadata(reader):
    from ops_platform.ops.lifecycle_state import ERRORS, error_code
    name = 'user@' + str(os.getuid()) + '.service'
    result = {'status': 'unavailable', 'name': name, 'scope': 'system_manager_read_only'}
    try:
        values = properties(reader.query('systemctl', system_user_args(), accept=(1, 4)),
                            set(SYSTEM_USER_PROPERTIES.split(',')))
        require(values.get('Id') == name and values.get('LoadState') in
                ('not-found', 'loaded', 'error', 'masked', 'merged', 'stub', 'bad-setting'), 'INVALID_SYSTEMD_RESPONSE')
        require(values.get('ActiveState') in ('active', 'reloading', 'inactive', 'failed', 'activating', 'deactivating', 'maintenance'),
                'INVALID_SYSTEMD_RESPONSE')
        require(re.fullmatch('[a-z][a-z0-9-]{0,63}', values.get('SubState', '')) is not None
                and re.fullmatch('[a-z0-9-]{0,64}', values.get('Result', '')) is not None, 'INVALID_SYSTEMD_RESPONSE')
        result.update(status='ok', load_state=values['LoadState'], active_state=values['ActiveState'],
                      sub_state=values['SubState'], service_result=values.get('Result') or None)
    except ERRORS as exc: result['code'] = error_code(exc)
    return result


class Reader:
    def __init__(self, env=None, runner=subprocess.run, which=shutil.which):
        self.original = dict(os.environ if env is None else env)
        self.env = {k: v for k, v in self.original.items() if k in
                    ('HOME', 'PATH', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'DOCKER_CONFIG')}
        bus = self.original.get('DBUS_SESSION_BUS_ADDRESS')
        if bus and bus.startswith('unix:'): self.env['DBUS_SESSION_BUS_ADDRESS'] = bus
        self.env.update(LC_ALL='C', SYSTEMD_PAGER='', SYSTEMD_COLORS='0')
        self.runner = runner
        found = {name: which(name, path=self.original.get('PATH', os.defpath)) for name in ('docker', 'systemctl', 'loginctl')}
        self.paths = {name: str(Path(path).absolute()) if path else None for name, path in found.items()}
        self.calls = 0

    def query(self, tool, args, accept=(), user_runtime=None):
        require(allow(tool, args), 'LIFECYCLE_READ_ONLY_COMMAND_REQUIRED')
        require(self.paths.get(tool), tool.upper() + '_NOT_INSTALLED')
        environment = self.env
        if user_runtime is not None:
            require(tool == 'systemctl' and args[0] == '--user', 'USER_BUS_ENVIRONMENT_SCOPE_INVALID')
            runtime = validate_runtime(user_runtime)
            environment = dict(self.env, XDG_RUNTIME_DIR=runtime,
                               DBUS_SESSION_BUS_ADDRESS='unix:path=' + quote(runtime + '/bus', safe='/-_.'))
        self.calls += 1
        try:
            result = self.runner([self.paths[tool], *args], env=environment, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=5, check=False)
        except subprocess.TimeoutExpired: raise ReadFailure(tool.upper() + '_QUERY_TIMEOUT') from None
        except OSError: raise ReadFailure(tool.upper() + '_EXEC_UNAVAILABLE') from None
        if result.returncode != 0 and result.returncode not in accept:
            error = ReadFailure(tool.upper() + '_QUERY_FAILED')
            if tool == 'systemctl' and args[0] == '--user': error.reason = user_bus_reason(result.stderr)
            raise error
        require(len(result.stdout) <= 128 * 1024, 'METADATA_RESPONSE_TOO_LARGE')
        return result.stdout.strip()


def properties(raw, expected):
    output = {}
    for line in raw.splitlines():
        key, sep, value = line.partition('=')
        require(sep and key in expected and key not in output, 'INVALID_SYSTEMD_RESPONSE')
        output[key] = value
    return output


def docker(reader, directory, cfg, owner):
    # Honor Docker's context-over-host precedence; all daemon queries are pinned to the verified Unix endpoint.
    context = reader.original.get('DOCKER_CONTEXT')
    endpoint = reader.original.get('DOCKER_HOST') if not context else None
    if endpoint:
        endpoint = local_endpoint(endpoint); source = 'DOCKER_HOST'
    else:
        source = 'DOCKER_CONTEXT' if context else 'current_context'
        context = context or reader.query('docker', ['context', 'show'])
        endpoint = local_endpoint(reader.query('docker', ['context', 'inspect', context, '--format', CONTEXT]))
    prefix = ['--host', endpoint]
    info = strict_json(reader.query('docker', prefix + ['info', '--format', INFO]))
    require(isinstance(info, dict) and info.get('os') == 'linux' and isinstance(info.get('security'), list), 'LINUX_DOCKER_REQUIRED')
    require(not any('rootless' in str(v) for v in info['security']), 'ROOTFUL_DOCKER_REQUIRED')
    require(isinstance(info.get('id'), str) and re.fullmatch('[A-Za-z0-9:_.-]{1,128}', info['id']), 'INVALID_DOCKER_METADATA')
    require(isinstance(info.get('version'), str) and re.fullmatch(r'[0-9][A-Za-z0-9_.+-]{0,95}', info['version']), 'INVALID_DOCKER_METADATA')
    compose = reader.query('docker', prefix + ['compose', 'version', '--short'])
    require(re.fullmatch(r'v?[0-9]+\.[0-9]+(?:\.[0-9]+)?[A-Za-z0-9_.+-]*', compose), 'INVALID_COMPOSE_VERSION')
    result = {'status': 'ok', 'endpoint_source': source, 'endpoint': endpoint, 'local_daemon': True, 'rootful': True,
              'server_version': info['version'], 'compose_version': compose,
              'daemon_id': info['id'], 'ops': {'status': 'not_checked'}}
    if cfg is None or owner is None: return result
    ids = reader.query('docker', prefix + ['ps', '-a', '--no-trunc', '--filter',
        'label=com.docker.compose.project=' + owner.get('compose_project', rc.PROJECT), '--filter',
        'label=com.docker.compose.service=ops-web', '--format', '{{.ID}}']).split()
    require(len(ids) <= 16 and all(HEX.fullmatch(v) for v in ids), 'INVALID_CONTAINER_IDS')
    if len(ids) != 1:
        result['ops'] = {'status': 'attention_required', 'candidate_count': len(ids),
                         'code': 'OPS_CONTAINER_NOT_FOUND' if not ids else 'OPS_CONTAINER_AMBIGUOUS'}
        return result
    raw = strict_json(reader.query('docker', prefix + ['container', 'inspect', '--format', rc.INSPECT, '--', ids[0]]))
    require(raw.get('id') == ids[0], 'DOCKER_IDENTITY_CHANGED')
    require(type(raw.get('running')) is bool and raw.get('health') in (None, 'starting', 'healthy', 'unhealthy'), 'INVALID_DOCKER_METADATA')
    checked = rc.container(raw, directory, cfg, owner)
    result['ops'] = {'status': 'ok' if checked['healthy'] else 'attention_required', 'candidate_count': 1,
                     'container_id': ids[0], 'image_id': raw['image'], 'started_at': raw['started'],
                     'running': raw['running'], 'health': raw['health'], 'owner_and_mounts_verified': True,
                     'mounted_file_contents_verified': False}
    if not checked['healthy']: result['ops']['code'] = 'OPS_CONTAINER_NOT_HEALTHY'
    return result


def _manager(reader, runtime=None):
    return properties(reader.query('systemctl', ['--user', '--no-pager', '--no-ask-password', 'show',
                      '--property=' + MANAGER_PROPERTIES], user_runtime=runtime), set(MANAGER_PROPERTIES.split(',')))


def _inspect_manager(reader, unit_name, result):
    raw_version = reader.query('systemctl', ['--version']).splitlines()
    require(bool(raw_version), 'INVALID_SYSTEMD_VERSION')
    version = raw_version[0]
    match = re.match(r'systemd ([0-9]+)\b', version)
    require(match is not None, 'INVALID_SYSTEMD_VERSION')
    result['version'] = int(match[1])
    # Conservative version prerequisite for the planned provider, not generated-unit verification.
    require(result['version'] >= 240, 'SYSTEMD_VERSION_UNSUPPORTED')
    runtime = None
    missing_environment = not reader.original.get('XDG_RUNTIME_DIR') and not reader.original.get('DBUS_SESSION_BUS_ADDRESS')
    if not missing_environment:
        result['environment'].update(selection='explicit_environment_preserved', fallback='explicit_environment_preserved')
    elif result['login']['status'] == 'ok' and result['login'].get('runtime_path'):
        # Choose a validated endpoint before the first connection. Error text varies across systemd versions
        # and must not decide whether discovery runs. An explicit caller environment is never replaced.
        try: runtime = validate_runtime(result['login']['runtime_path'])
        except (ReadFailure, fs.Unavailable, OSError, ValueError) as validation_error:
            from ops_platform.ops.lifecycle_state import error_code
            result['environment'].update(selection='candidate_rejected', code=error_code(validation_error))
            raise ReadFailure('SYSTEMD_USER_MANAGER_UNAVAILABLE') from None
        result['environment'].update(source='logind_runtime_current_user', selection='verified_logind_runtime_before_query',
                                      selected_runtime=runtime)
    else:
        result['environment']['selection'] = 'no_verified_logind_runtime'
    try: manager = _manager(reader, runtime)
    except ReadFailure as exc:
        result['manager_query_error'] = {'code': exc.code, 'reason': getattr(exc, 'reason', None)}
        if exc.code == 'SYSTEMCTL_QUERY_FAILED': raise ReadFailure('SYSTEMD_USER_MANAGER_UNAVAILABLE') from None
        raise
    require(bool(manager.get('Version')) and bool(manager.get('SystemState')), 'SYSTEMD_USER_MANAGER_UNAVAILABLE')
    manager_version = re.match(r'([0-9]+)\b', manager['Version'])
    require(manager_version is not None and int(manager_version[1]) >= 240, 'SYSTEMD_USER_MANAGER_VERSION_UNSUPPORTED')
    require(manager['SystemState'] in ('initializing', 'starting', 'running', 'degraded', 'maintenance', 'stopping', 'offline', 'unknown'),
            'INVALID_SYSTEMD_RESPONSE')
    result.update(status='ok', user_manager_state=manager['SystemState'], manager_version=int(manager_version[1]))
    if manager['SystemState'] not in ('running', 'degraded'):
        result.update(status='attention_required', code='SYSTEMD_USER_MANAGER_NOT_READY')
    if unit_name:
        values = properties(reader.query('systemctl', ['--user', '--no-pager', '--no-ask-password', 'show',
            '--property=' + UNIT_PROPERTIES, unit_name], accept=(1, 4), user_runtime=runtime), set(UNIT_PROPERTIES.split(',')))
        require(values.get('LoadState') in ('not-found', 'loaded', 'error', 'masked', 'merged', 'stub', 'bad-setting'), 'INVALID_SYSTEMD_RESPONSE')
        absent = values['LoadState'] == 'not-found' and not values.get('FragmentPath') and not values.get('UnitFileState')
        result['unit'] = {'status': 'absent' if absent else 'unowned',
                          'load_state': values['LoadState'], 'name': unit_name}
        if not absent:
            # No managed writer exists in this version. Never claim ownership/liveness of an existing unit.
            result['unit']['code'] = 'MANAGEMENT_UNIT_ALREADY_EXISTS'
    return result


def systemd(reader, unit_name):
    from ops_platform.ops.lifecycle_state import ERRORS, error_code
    result = {'provider': 'systemd-user', 'status': 'unavailable', 'linger': 'unknown',
              'login': {'status': 'unavailable'}, 'unit': {'status': 'not_checked'},
              'environment': {'source': 'current_environment', 'fallback': 'not_needed', 'fallback_attempted': False,
                              'xdg_runtime_dir_present': bool(reader.original.get('XDG_RUNTIME_DIR')),
                              'session_bus_address_present': bool(reader.original.get('DBUS_SESSION_BUS_ADDRESS')),
                              'parent_environment_changed': False},
              'generated_unit_validation': 'pending_implementation'}
    # logind uses the system bus. Its observations remain useful even if the user manager is inaccessible.
    try:
        values = properties(reader.query('loginctl', login_args()), set(LOGIN_PROPERTIES.split(',')))
        result['login']['observed_properties'] = sorted(values)
        result['login']['missing_properties'] = sorted({'UID', 'Linger', 'State'} - values.keys())
        require(values.get('UID') == str(os.getuid()) and values.get('Linger') in ('yes', 'no'), 'INVALID_LOGIND_USER_RESPONSE')
        runtime = values.get('RuntimePath', '')
        require(len(runtime) <= 4096 and (not runtime or Path(runtime).is_absolute())
                and not any(ord(c) < 32 or ord(c) == 127 for c in runtime), 'INVALID_LOGIND_USER_RESPONSE')
        user_state = values.get('State')
        require(user_state in ('offline', 'lingering', 'online', 'active', 'closing'), 'INVALID_LOGIND_USER_RESPONSE')
        result.update(linger=values['Linger'] == 'yes', login={'status': 'ok', 'uid': int(values['UID']),
                      'runtime_path': runtime or None, 'user_state': user_state})
    except ERRORS as exc:
        result['linger_error'] = error_code(exc)
        result['login'].update(status='unavailable', code=error_code(exc))
    try: return _inspect_manager(reader, unit_name, result)
    except ERRORS as exc:
        result.update(status='unavailable', code=error_code(exc))
        # Diagnose independently; never start the manager or replace explicitly configured user paths.
        result['environment']['xdg_runtime'] = runtime_metadata(reader.original.get('XDG_RUNTIME_DIR'))
        if result['login'].get('runtime_path'):
            path = result['login']['runtime_path']
            result['login']['runtime'] = runtime_metadata(path)
            result['environment']['xdg_matches_logind'] = reader.original.get('XDG_RUNTIME_DIR') == path
        result['system_user_service'] = system_user_metadata(reader)
        return result
