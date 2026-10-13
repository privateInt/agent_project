"""Owned systemd user lifecycle; no business service deployment or inference."""
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import lifecycle_probe as probe, lifecycle_state as local, manage
from ops_platform.ops import refresh_worker as worker, worker_state as ws, set_public_url as change
from ops_platform.ops.service_systemd import Systemd, render, command_details

STAGE = 'O04-auto-refresh-service'


def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def manifest_path(directory): return ws.location(directory).parent / 'service.json'


def load(directory):
    value = ws.read(manifest_path(directory))
    owner = ws.read(directory / 'owner.json')
    require(value.get('schema') == 1 and value.get('provider') == 'systemd-user'
            and value.get('binding', {}).get('owner') == owner and value['binding'].get('directory') == str(directory),
            'SERVICE_OWNER_MISMATCH')
    require(value.get('unit_name') == 'agent-project-ops-refresh-' + owner['owner'].replace('-', '') + '.service', 'SERVICE_OWNER_MISMATCH')
    require(value.get('phase') in ('installing', 'installed', 'install_failed', 'removing', 'removed'), 'SERVICE_RECORD_INVALID')
    require(type(value.get('interval_seconds')) is int and 30 <= value['interval_seconds'] <= 3600
            and type(value.get('deadline_seconds')) is int and 1 <= value['deadline_seconds'] <= 600
            and value.get('persistence') in ('boot', 'session'), 'SERVICE_RECORD_INVALID')
    require(Path(value['unit_path']).name == value['unit_name'] and Path(value['unit_path']).is_absolute()
            and value.get('unit_sha256') == digest(render(value)), 'SERVICE_RECORD_INVALID')
    return value


def ensure_directory(path):
    """Create only missing owner directories. Never chmod existing config dirs."""
    path = manage.safe(path)
    if not path.exists():
        ensure_directory(path.parent); path.mkdir(mode=0o700)
    st = path.stat()
    require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022, 'SERVICE_DIRECTORY_UNSAFE')
    return path


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def unit_bytes(path):
    raw, _ = worker.activation.rc.read_file(manage.safe(path), 32768, private=True)
    require(path.stat().st_nlink == 1, 'SERVICE_UNIT_HARDLINK_REFUSED')
    return raw.decode() if isinstance(raw, bytes) else raw


def enabled_path(manifest):
    return Path(manifest['unit_path']).parent / 'default.target.wants' / manifest['unit_name']


def enabled(manifest):
    link = enabled_path(manifest)
    manage.safe(link.parent)
    if not link.exists() and not link.is_symlink(): return False
    st = link.lstat()
    require(stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid()
            and os.readlink(link) == manifest['unit_path'], 'SERVICE_ENABLE_LINK_CONFLICT')
    return True


def set_enabled(manifest, value):
    was = enabled(manifest)
    if was == value: return False
    link = enabled_path(manifest)
    if value:
        ensure_directory(link.parent); os.symlink(manifest['unit_path'], link)
    else: link.unlink()
    fsync_directory(link.parent)
    return True


def owned(manifest, backend, allow_absent=False):
    path = Path(manifest['unit_path'])
    if path.exists() or path.is_symlink():
        require(unit_bytes(path) == render(manifest), 'SERVICE_UNIT_CHANGED')
    else: require(allow_absent, 'SERVICE_UNIT_MISSING')
    current = backend.inspect()
    require(not current.get('DropInPaths'), 'SERVICE_DROPIN_CONFLICT')
    if current['LoadState'] == 'not-found':
        require(allow_absent and not current.get('FragmentPath'), 'SERVICE_UNIT_NOT_LOADED')
    else:
        require(current.get('FragmentPath') == manifest['unit_path'], 'SERVICE_FRAGMENT_CONFLICT')
        require(current.get('NeedDaemonReload', 'no') == 'no', 'SERVICE_DAEMON_RELOAD_REQUIRED')
    require(current.get('UnitFileState', '') in ('', 'disabled', 'enabled'), 'SERVICE_ENABLE_STATE_CONFLICT')
    require(current.get('UnitFileState') != 'enabled' or enabled(manifest), 'SERVICE_ENABLE_STATE_CONFLICT')
    return current


def inactive(current):
    return current['ActiveState'] in ('inactive', 'failed') and current['MainPID'] == '0' and current.get('Job', '') in ('', '0')


def stopped(directory, current, backend):
    require(inactive(current) and backend.cgroup_empty(current), 'SERVICE_STILL_RUNNING')
    with ws.automatic_lease(directory): pass


def save_event(directory, action, phase, code=None, service_command=None):
    # Bounded last-operation journal. Worker intent and canonical history are
    # separate, and are never deleted or rewritten by this adapter.
    record = {'schema': 1, 'action': action, 'phase': phase, 'code': code, 'checked_at': worker.activation.rc.now()}
    detail = command_details(service_command)
    if detail is not None: record['service_command'] = detail
    ws.write(manifest_path(directory).parent / 'service-operation.json', record)


def request_pause(directory):
    store = ws.Store(directory)
    with store.lock():
        control = store.control()
        control.update(desired='paused', service_stop_token=uuid.uuid4().hex,
                       pause_requested_at=worker.activation.rc.now())
        ws.write(store.path / 'control.json', control)


def stop_token_unchanged(store, token):
    require(store.control().get('service_stop_token') == token, 'SERVICE_STOP_REQUEST_CHANGED')


def current_observation(manifest, current, observation):
    """A saved PID is only evidence to compare, never a target for a signal."""
    if not observation or current['ActiveState'] != 'active': return False
    identity = observation.get('identity') or {}
    if not current.get('InvocationID') or observation.get('service_invocation') != current['InvocationID']: return False
    if str(identity.get('proc_pid')) != current['MainPID']: return False
    try:
        if identity.get('boot_id') != Path('/proc/sys/kernel/random/boot_id').read_text().strip(): return False
        content = Path('/proc/' + current['MainPID'] + '/stat').read_text()
        if str(identity.get('start_ticks')) != content.rsplit(')', 1)[1].split()[19]: return False
        from datetime import datetime, timezone
        checked = datetime.fromisoformat(observation['checked_at'].replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        bound = manifest['deadline_seconds'] + 20 if observation.get('phase') == 'evaluating' else manifest['interval_seconds'] + 20
        return 0 <= age <= bound and observation.get('phase') in ('evaluating', 'waiting')
    except (OSError, ValueError, KeyError, IndexError, TypeError): return False


def status(directory, backend):
    manifest = load(directory)
    current = owned(manifest, backend, allow_absent=manifest['phase'] in ('removed', 'install_failed', 'installing', 'removing'))
    stored = ws.status(directory)
    observation = stored.get('last_observation')
    live = current_observation(manifest, current, observation)
    evaluation = 'first_evaluation_pending' if current['ActiveState'] in ('active', 'activating') else 'not_running'
    if live:
        evaluation = 'in_progress' if observation['phase'] == 'evaluating' else (
            'healthy' if (observation.get('outcome') or {}).get('classification') == 'accepted' else 'degraded_retrying')
    return {'status': 'owned' if manifest['phase'] == 'installed' else manifest['phase'],
            'unit_name': manifest['unit_name'], 'unit_path': manifest['unit_path'],
            'persistence': manifest['persistence'], 'enabled': enabled(manifest),
            'provider_status': {key: current.get(key) for key in ('ActiveState', 'SubState', 'MainPID', 'InvocationID', 'Result', 'ExecMainStatus')},
            'desired_state': stored['control']['desired'], 'recovery': stored['control']['recovery'],
            'worker': stored, 'current_invocation_verified': live, 'evaluation_status': evaluation,
            'historical_observation_reused_as_current': False}


def proposal(directory, report, reader, backend, binding, interval):
    home = manage.safe(Path.home())
    config = manage.safe(reader.original.get('XDG_CONFIG_HOME') or home / '.config')
    unit_dir = config / 'systemd/user'
    require(str(unit_dir) in backend.unit_paths(), 'SERVICE_UNIT_DIRECTORY_NOT_IN_MANAGER_PATH')
    require(backend.env_binary, 'SERVICE_ENV_EXECUTABLE_MISSING')
    environment = {'HOME': str(home), 'LC_ALL': 'C', 'PATH': str(Path(binding['docker_path']).parent) + ':' + os.defpath,
                   'DOCKER_HOST': probe.local_endpoint(report['docker']['endpoint'])}
    # Docker CLI plugins can be installed under this config directory. It is a
    # reference only; config/registry credentials are not copied to the unit.
    if reader.original.get('DOCKER_CONFIG'):
        environment['DOCKER_CONFIG'] = str(manage.safe(reader.original['DOCKER_CONFIG']))
    value = {'schema': 1, 'provider': 'systemd-user', 'phase': 'installing', 'binding': binding,
             'unit_name': backend.unit, 'unit_path': str(unit_dir / backend.unit),
             'env_executable': str(Path(backend.env_binary).absolute()), 'environment': environment,
             'interval_seconds': interval, 'deadline_seconds': 180, 'persistence': report['persistence']['selected'],
             'created_at': worker.activation.rc.now()}
    value['unit_sha256'] = digest(render(value))
    return value


def install(directory, report, reader, backend, interval, snapshot_fn):
    target = manifest_path(directory)
    with ws.automatic_lease(directory):
        with change.change_lock(directory): binding = snapshot_fn(directory)
        value = proposal(directory, report, reader, backend, binding, interval)
        previous = load(directory) if target.exists() else None
        if previous:
            comparable = lambda item: {k: v for k, v in item.items() if k not in ('phase', 'created_at')}
            require(comparable(previous) == comparable(value), 'SERVICE_INSTALLATION_CHANGE_REQUIRES_REVIEW')
            if previous['phase'] == 'installed':
                owned(previous, backend); return previous, False
            require(previous['phase'] in ('removed', 'install_failed', 'installing'), 'SERVICE_INSTALLATION_INCOMPLETE')
            require(ws.Store(directory).control()['desired'] == 'paused', 'WORKER_PAUSE_REQUIRED')
            if previous['phase'] == 'installing' and Path(previous['unit_path']).exists():
                require(unit_bytes(Path(previous['unit_path'])) == render(previous), 'SERVICE_UNIT_CHANGED')
                observed = backend.inspect()
                require(inactive(observed) and backend.cgroup_empty(observed) and not enabled(previous), 'SERVICE_STILL_RUNNING')
                require(not observed.get('DropInPaths') and observed.get('FragmentPath', '') in ('', previous['unit_path']), 'SERVICE_FRAGMENT_CONFLICT')
                backend.verify(previous); backend.reload(); owned(previous, backend)
                previous['phase'] = 'installed'; ws.write(target, previous)
                save_event(directory, 'install', 'completed'); return previous, False
        else: require(not ws.location(directory).exists(), 'WORKER_STATE_ALREADY_EXISTS')
        current = backend.inspect()
        require(current['LoadState'] == 'not-found' and inactive(current) and not current.get('DropInPaths')
                and not current.get('FragmentPath'), 'MANAGEMENT_UNIT_ALREADY_EXISTS')
        path = manage.safe(value['unit_path'])
        require(not path.exists() and not enabled(value), 'MANAGEMENT_UNIT_ALREADY_EXISTS')
        backend.verify(value)  # Validate before creating any persistent management files.
        store = ws.Store(directory, binding); store.pause()
        if previous is None: store.status(phase='installed', completed_cycles=0)
        ws.write(target, value); save_event(directory, 'install', 'intent')
        created = False
        try:
            ensure_directory(path.parent)
            manage.write_new(path, render(value)); created = True; fsync_directory(path.parent)
            backend.reload(); owned(value, backend)
            value['phase'] = 'installed'; ws.write(target, value)
            save_event(directory, 'install', 'completed'); return value, True
        except BaseException:
            # Delete only our exact, newly written unit. Keep paused worker and
            # audit state if a daemon-reload response was lost or cleanup fails.
            if created and unit_bytes(path) == render(value):
                path.unlink(); fsync_directory(path.parent)
                try: backend.reload()
                except local.ERRORS: pass
            value['phase'] = 'install_failed'; ws.write(target, value)
            save_event(directory, 'install', 'failed'); raise


def run(action, directory, persistence='auto', interval=60, reader=None, backend=None,
        preflight_fn=None, snapshot_fn=worker.runtime_snapshot, recovery_fn=worker.acknowledge_recovery,
        _sleep=time.sleep, _clock=time.monotonic):
    require(action in ('install', 'start', 'stop', 'resume', 'remove'), 'INVALID_SERVICE_ACTION')
    require(type(interval) is int and 30 <= interval <= 3600, 'INVALID_WORKER_LIMIT')
    directory = manage.private(directory); reader = reader or probe.Reader()
    if action in ('install', 'start', 'resume'): ws.transition_gate(directory)
    from ops_platform.ops import auto_refresh
    preflight_fn = preflight_fn or auto_refresh.run
    # stop/remove must work when Docker is down or host code changed. Their
    # gate is unit/state ownership, not readiness of business upstreams.
    owner = ws.read(directory / 'owner.json')
    require(owner.get('uid') == os.getuid() and owner.get('gid') == os.getgid()
            and owner.get('project_root') == str(worker.ROOT), 'OWNER_MISMATCH')
    unit = 'agent-project-ops-refresh-' + owner['owner'].replace('-', '') + '.service'
    token = (ws.Store(directory).control().get('service_stop_token') if manifest_path(directory).exists() else None)
    if action == 'stop':
        # Persist the user's intent even when the manager connection or a
        # manually edited unit later prevents safe systemctl control.
        load(directory); request_pause(directory)
    if backend is None:
        systemd = probe.systemd(reader, unit)
        backend = Systemd(reader, systemd, unit)
    result = {'schema': 1, 'stage': STAGE, 'action': action, 'inference_requested': False,
              'business_service_control_requested': False, 'boot_acceptance_verified': False,
              'configuration_activation_acceptance': False, 'systemd_native_acceptance': False}
    with ws.file_lock(directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
        try:
            if action in ('install', 'start', 'resume'):
                report = preflight_fn('plan', directory, persistence, reader=reader)
                ignored = {'WORKER_PAUSED', 'WORKER_RECOVERY_REQUIRED', 'WORKER_UNFINISHED_INTENT'} if action == 'resume' else set()
                require(not set(report['blockers']) - ignored, 'SERVICE_PREFLIGHT_BLOCKED')
                if manifest_path(directory).exists():
                    saved = load(directory)
                    require(persistence == 'auto' or persistence == saved['persistence'], 'SERVICE_POLICY_CHANGE_REQUIRES_REVIEW')
                    report['persistence']['selected'] = saved['persistence']
                    if saved['persistence'] == 'boot': require(report['persistence']['linger'] is True, 'BOOT_PERSISTENCE_UNAVAILABLE')
                if action == 'install' or action == 'start' and not manifest_path(directory).exists():
                    manifest, changed = install(directory, report, reader, backend, interval, snapshot_fn)
                    result['unit_written'] = changed
                    if action == 'install': return dict(result, result='passed', service_started=False, management=status(directory, backend))
            manifest = load(directory)
            require(manifest['phase'] == 'installed' or action == 'remove' and manifest['phase'] in ('removed', 'removing'), 'SERVICE_NOT_INSTALLED')
            current = owned(manifest, backend, allow_absent=manifest['phase'] in ('removed', 'removing'))
            store = ws.Store(directory)
            if action in ('start', 'resume'):
                require(manifest['persistence'] != 'boot', 'BOOT_EXPLICIT_START_ADAPTER_REQUIRED')
                if current['ActiveState'] in ('active', 'activating'):
                    require(action == 'start', 'SERVICE_STOP_REQUIRED')
                    store.gate()
                    return dict(result, result='passed', service_started=False, already_running=True, management=status(directory, backend))
                stopped(directory, current, backend)
                with ws.automatic_lease(directory):
                    with change.change_lock(directory):
                        require(snapshot_fn(directory) == manifest['binding'] == store.manifest()['binding'], 'SERVICE_BINDING_CHANGED')
                    intent = store.optional('intent.json')
                    if action == 'start':
                        prior = store.optional('status.json') or {}
                        require(prior.get('phase') == 'installed' and not store.control()['recovery'] and not intent, 'SERVICE_EXPLICIT_RESUME_REQUIRED')
                        with store.lock():
                            stop_token_unchanged(store, token)
                            control = store.control(); control.update(desired='running', recovery=None)
                            ws.write(store.path / 'control.json', control)
                    else: store.pause()
                save_event(directory, action, 'intent')
                if action == 'resume':
                    # Existing explicit live checks only; never apply/rollback a pending generation.
                    recovery_fn(directory, resume_guard=lambda saved: stop_token_unchanged(saved, token))
                    # A cleanly stopped unit may be unloaded between queries.
                    # ResetFailedUnit needs an existing in-memory unit; it is
                    # unnecessary for inactive units. Recheck ownership/exit
                    # after live recovery before clearing an actual failure.
                    recovered = owned(manifest, backend)
                    stopped(directory, recovered, backend)
                    if recovered['ActiveState'] == 'failed': backend.reset_failed()
                store.status(phase='service_starting', completed_cycles=0)
                set_enabled(manifest, manifest['persistence'] == 'boot'); backend.reload()
                with store.lock(): stop_token_unchanged(store, token); store.gate()
                owned(manifest, backend); backend.start()
                save_event(directory, action, 'submitted')
                observed = status(directory, backend)
                require(observed['provider_status']['ActiveState'] != 'failed', 'SERVICE_START_FAILED')
                return dict(result, result='passed', start_request_submitted=True,
                            process_start_verified=observed['current_invocation_verified'],
                            first_evaluation_verified=False, management=observed)
            if action == 'stop':
                store.pause()  # No prepared lock: promptly reaches an in-flight evaluation.
                save_event(directory, action, 'intent')
                set_enabled(manifest, False); backend.reload(); owned(manifest, backend)
                backend.stop()
                until = _clock() + manifest['deadline_seconds'] + 30
                while True:
                    current = owned(manifest, backend)
                    if inactive(current) and backend.cgroup_empty(current): break
                    require(_clock() < until, 'SERVICE_STOP_OUTCOME_UNKNOWN'); _sleep(.2)
                stopped(directory, current, backend)
                observation = store.optional('status.json') or {}
                if current.get('Result') == 'timeout' or observation.get('phase') in ('evaluating', 'waiting'):
                    store.pause('WORKER_PREVIOUS_INVOCATION_INTERRUPTED')
                save_event(directory, action, 'completed')
                return dict(result, result='passed', worker_exit_verified=True, daemon_operation_cancellation_verified=False,
                            management=status(directory, backend))
            require(store.control()['desired'] == 'paused', 'SERVICE_STOP_REQUIRED')
            stopped(directory, current, backend)
            require(not enabled(manifest) and current.get('UnitFileState', '') != 'enabled', 'SERVICE_DISABLE_REQUIRED')
            with ws.automatic_lease(directory):
                require(inactive(backend.inspect()), 'SERVICE_STILL_RUNNING')
                if manifest['phase'] != 'removed':
                    save_event(directory, action, 'intent'); manifest['phase'] = 'removing'; ws.write(manifest_path(directory), manifest)
                    # A failed unit is retained by the manager until its failed
                    # state is cleared. Only this owned, stopped unit is reset;
                    # durable worker recovery and transaction evidence remain.
                    if current['ActiveState'] == 'failed': backend.reset_failed()
                    path = Path(manifest['unit_path'])
                    if path.exists():
                        require(unit_bytes(path) == render(manifest), 'SERVICE_UNIT_CHANGED')
                        path.unlink(); fsync_directory(path.parent)
                    backend.reload()
                    after = backend.inspect()
                    require(after['LoadState'] == 'not-found' and inactive(after), 'SERVICE_REMOVE_OUTCOME_UNKNOWN')
                    manifest['phase'] = 'removed'; ws.write(manifest_path(directory), manifest)
            save_event(directory, action, 'completed')
            return dict(result, result='passed', owned_unit_removed=True, audit_and_worker_records_preserved=True)
        except local.ERRORS as exc:
            detail = command_details(getattr(exc, 'service_command', None))
            if detail is not None: result['service_command'] = detail
            if manifest_path(directory).exists():
                # Any uncertain start is paused. A late queued start therefore
                # meets a durable gate rather than silently becoming automatic.
                if action in ('start', 'resume'):
                    try:
                        saved = ws.Store(directory)
                        saved.pause(None if saved.control()['recovery'] else local.error_code(exc))
                    except local.ERRORS: pass
                save_event(directory, action, 'attention_required', local.error_code(exc), service_command=detail)
            return dict(result, result='attention_required', code=local.error_code(exc),
                        service_commands_requested=backend.control_commands,
                        recovery_records_preserved=True)
