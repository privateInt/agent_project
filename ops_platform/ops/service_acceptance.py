#!/usr/bin/env python3
"""Bounded actual user-unit/Docker acceptance, using disposable synthetic upstreams."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import docker_acceptance as isolated, lifecycle_probe as probe, lifecycle_state as local
from ops_platform.ops import service_lifecycle as service, service_systemd as adapter, worker_state as ws
from ops_platform.ops import refresh_discovery as refresh, activate_discovery as activation, manage, restart_check as rc
from ops_platform.ops.watch_acceptance import snapshot
from ops_platform.transport import strict_json

STAGE = 'O04-isolated-systemd-docker-acceptance'
INTERVAL = 30
NO_RESTART_SECONDS = 35  # Exceeds the generated unit's RestartSec=30.


def failure_context(value):
    """Bounded labels only; never forward stderr, arguments or arbitrary report fields."""
    if not isinstance(value, dict): return {}
    result = {}
    if value.get('action') in ('install', 'start', 'stop', 'resume', 'remove'):
        result['action'] = value['action']
    command = adapter.command_details(value.get('service_command'))
    if command is not None: result['service_command'] = command
    return result


def error_report(exc):
    report = {'stage': STAGE, 'result': 'attention_required', 'code': local.error_code(exc)}
    context = failure_context({'action': getattr(exc, 'service_action', None),
                               'service_command': getattr(exc, 'service_command', None)})
    if context: report['failure_context'] = context
    return report


def native_preflight():
    """Read current-user capabilities before creating any acceptance resources."""
    reader = probe.Reader()
    unit = 'agent-project-ops-refresh-' + uuid.uuid4().hex + '.service'
    report = probe.systemd(reader, unit)
    require(report.get('status') == 'ok', report.get('code', 'SYSTEMD_USER_MANAGER_UNAVAILABLE'))
    backend = adapter.Systemd(reader, report, unit)
    require(backend.analyze and backend.env_binary, 'SERVICE_VALIDATOR_NOT_INSTALLED')
    config = manage.safe(reader.original.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    require(str(config / 'systemd/user') in backend.unit_paths(), 'SERVICE_UNIT_DIRECTORY_NOT_IN_MANAGER_PATH')
    return {'systemd_version': report['version'], 'manager_version': report['manager_version'],
            'linger': report['linger'], 'persistence': 'session', 'service_control_requested': False}


def checked_run(directory):
    record = isolated.validate_run(directory)
    require(record.get('scenario') == 'service', 'ACCEPTANCE_SCENARIO_INVALID')
    return record


def checked_state(directory):
    record = checked_run(directory)
    state = manage.safe(directory / 'state')
    owner = ws.read(state / 'owner.json')
    require(owner.get('project_root') == str(directory / 'project')
            and owner.get('compose_project') == record['prefix'] + '-ops'
            and owner.get('uid') == os.getuid() and owner.get('gid') == os.getgid(),
            'ACCEPTANCE_SERVICE_OWNER_MISMATCH')
    return state, owner


class Controller:
    def __init__(self, state):
        self.state = state
        owner = ws.read(state / 'owner.json')
        self.reader = probe.Reader()
        unit = 'agent-project-ops-refresh-' + owner['owner'].replace('-', '') + '.service'
        report = probe.systemd(self.reader, unit)
        self.backend = adapter.Systemd(self.reader, report, unit)
        self.manager_version = report['manager_version']

    def call(self, action):
        return service.run(action, self.state, persistence='session', interval=INTERVAL,
                           reader=self.reader, backend=self.backend)

    def good(self, action):
        try: report = self.call(action)
        except local.ERRORS as exc:
            exc.service_action = action
            raise
        if report.get('result') != 'passed':
            exc = ReadFailure(report.get('code', 'ACCEPTANCE_SERVICE_ACTION_FAILED'))
            exc.service_action = action
            exc.service_command = adapter.command_details(report.get('service_command'))
            raise exc
        return report

    def status(self): return service.status(self.state, self.backend)

    def wait(self, predicate, timeout=220):
        until = time.monotonic() + timeout
        while True:
            value = self.status()
            if predicate(value): return value
            require(time.monotonic() < until, 'ACCEPTANCE_SERVICE_WAIT_TIMEOUT')
            time.sleep(.5)

    def stable_failed(self, expected, guard, seconds=NO_RESTART_SECONDS):
        started = time.monotonic()
        until = started + seconds
        samples = 0
        while True:
            current = self.status()
            require(current['provider_status'] == expected['provider_status']
                    and current['worker']['last_observation'] == expected['worker']['last_observation'],
                    'ACCEPTANCE_FAILED_UNIT_RESTARTED')
            guard(); samples += 1
            remaining = until - time.monotonic()
            if remaining <= 0:
                return {'seconds': seconds, 'observed_seconds': round(time.monotonic() - started, 3),
                        'samples': samples, 'real_time': True}
            time.sleep(min(1, remaining))


def observation(value): return value['worker']['last_observation'] or {}
def invocation(value): return value['provider_status']['InvocationID']


def healthy(value, cycles=1):
    return (value['current_invocation_verified'] and value['evaluation_status'] == 'healthy'
            and observation(value).get('completed_cycles', 0) >= cycles)


def failed(value):
    provider = value['provider_status']
    return (provider['ActiveState'] == 'failed' and provider['MainPID'] == '0'
            and provider['ExecMainStatus'] == '2' and bool(value['recovery']))


def cleanup_unit(directory, controller_factory=Controller):
    """Called from the hashed copied project; never attach to production state."""
    checked_run(directory)
    require(ROOT == directory / 'project', 'ACCEPTANCE_WORKER_SOURCE_MISMATCH')
    state = manage.safe(directory / 'state')
    if not (state / 'owner.json').exists() and not (state / 'owner.json').is_symlink():
        return {'result': 'passed', 'unit_created': False, 'worker_exit_verified': True}
    state, owner = checked_state(directory)
    target = service.manifest_path(state)
    if not target.exists() and not target.is_symlink():
        # The service writer always records its manifest before creating a unit.
        return {'result': 'passed', 'unit_created': False, 'worker_exit_verified': True}
    manifest = service.load(state)
    controller = controller_factory(state)
    if manifest['phase'] in ('installing', 'install_failed'):
        # An interrupted install can leave an inactive owned unit. Preserve its
        # audit until an explicit installer retry; never reinterpret as installed.
        service.request_pause(state)
        current = service.owned(manifest, controller.backend, allow_absent=True)
        service.stopped(state, current, controller.backend)
        require(not Path(manifest['unit_path']).exists() and not service.enabled(manifest)
                and current['LoadState'] == 'not-found', 'ACCEPTANCE_PARTIAL_UNIT_REQUIRES_REVIEW')
    else:
        if manifest['phase'] == 'installed': controller.good('stop')
        controller.good('remove')
    return {'result': 'passed', 'unit_name': manifest['unit_name'], 'worker_exit_verified': True,
            'unit_absence_verified': True, 'audit_and_worker_records_preserved': True,
            'autostart_enabled': False, 'linger_changed': False}


def cleanup_unit_subprocess(directory):
    """Parent's independent cleanup, also used by older generic cleanup entrypoints."""
    record = checked_run(directory)
    project = directory / 'project'
    # Validate before executing a copied entrypoint. No mutable PID file is used.
    for name, wanted in record['source_hashes'].items():
        rel = Path(name)
        require(not rel.is_absolute() and '..' not in rel.parts, 'ACCEPTANCE_SOURCE_INVALID')
        raw = rc.read_file(project / rel, 1024*1024, private=True)[0]
        require(hashlib.sha256(raw).hexdigest() == wanted, 'ACCEPTANCE_SOURCE_CHANGED')
    script = project / 'ops_platform/ops/service_acceptance.py'
    require('ops_platform/ops/service_acceptance.py' in record['source_hashes'], 'ACCEPTANCE_SOURCE_INVALID')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'OPS_', 'COMPOSE_'))}
    try:
        result = subprocess.run([sys.executable, '-I', '-B', str(script), 'cleanup-unit', '--run-dir', str(directory)],
                                cwd=project, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=280, check=False)
    except (subprocess.TimeoutExpired, OSError): raise ReadFailure('ACCEPTANCE_UNIT_CLEANUP_UNCERTAIN') from None
    require(len(result.stdout) < 32768, 'ACCEPTANCE_UNIT_CLEANUP_RESPONSE_LIMIT')
    report = strict_json(result.stdout)
    require(result.returncode == 0 and report.get('result') == 'passed'
            and report.get('worker_exit_verified') is True,
            report.get('code', 'ACCEPTANCE_UNIT_CLEANUP_FAILED'))
    return report


def exercise(directory, engine, changed_port=isolated.change_port, read_hashes=isolated.runtime_hashes,
             controller_factory=Controller):
    record = checked_run(directory)
    def phase(name):
        print('[service-acceptance] ' + name, file=sys.stderr, flush=True)
        engine.protect(record['protected'])
    phase('isolated preparation')
    c = isolated.prepare_test_ops(directory, engine, read_hashes)
    state, _ = checked_state(directory)
    controller = controller_factory(state)
    cases = {}
    def passed(name): cases[name] = 'passed'
    def update(**data): isolated.write(c['control'], data)
    def unchanged(before): require(snapshot(c) == before, 'ACCEPTANCE_UNEXPECTED_CONFIGURATION_CHANGE')

    phase('install inactive session-only unit')
    first = controller.good('install')
    view = controller.status()
    require(first.get('unit_written') is True and view['provider_status']['ActiveState'] == 'inactive'
            and not view['enabled'] and view['persistence'] == 'session', 'ACCEPTANCE_INSTALL_NOT_INACTIVE')
    require(not controller.good('install')['unit_written'], 'ACCEPTANCE_INSTALL_NOT_IDEMPOTENT')
    passed('inactive_idempotent_install')

    phase('actual worker start and unchanged first evaluation')
    baseline = snapshot(c)
    controller.good('start')
    initial = controller.wait(healthy)
    require(observation(initial)['outcome']['restart_requested'] is False, 'ACCEPTANCE_FIRST_CYCLE_CHANGED')
    unchanged(baseline); passed('manager_invocation_and_first_evaluation')
    duplicate = controller.good('start')
    require(duplicate.get('already_running') and invocation(controller.status()) == invocation(initial),
            'ACCEPTANCE_DUPLICATE_START')
    passed('duplicate_start_does_not_replace_invocation')

    phase('live endpoint and metric job change')
    cookie = rc.API(manage.existing(state)).login()
    changed_port(directory, engine); update(version=1)
    changed = controller.wait(lambda v: healthy(v) and observation(v).get('outcome', {}).get('configuration_changed'))
    require(invocation(changed) == invocation(initial), 'ACCEPTANCE_WORKER_INVOCATION_CHANGED')
    history = refresh.lineage(state)
    require(len(history['records']) == 1 and history['pending'] is None, 'ACCEPTANCE_CHANGE_GENERATION_INVALID')
    require(history['records'][0][1]['baseline']['plan']['changed_fields'] ==
            ['platforms.model.url', 'resource_profile'], 'ACCEPTANCE_CHANGED_FIELDS_INVALID')
    require(snapshot(c)['started'] != baseline['started'], 'ACCEPTANCE_TEST_OPS_NOT_RESTARTED')
    rc.API(manage.existing(state)).expired(cookie)
    passed('live_endpoint_profile_change_and_session_rejection')
    accepted = snapshot(c)
    count = observation(changed)['completed_cycles']
    next_value = controller.wait(lambda v: healthy(v, count + 1))
    require(invocation(next_value) == invocation(initial)
            and not observation(next_value)['outcome']['restart_requested'], 'ACCEPTANCE_NOOP_FAILED')
    unchanged(accepted); passed('unchanged_followup_without_duplicate_journal')

    phase('durable stop, exit/lease proof and explicit resume')
    stopped = controller.good('stop')
    require(stopped.get('worker_exit_verified') and controller.status()['desired_state'] == 'paused',
            'ACCEPTANCE_STOP_FAILED')
    # A stopped worker must not be implicitly started by the ordinary start command.
    refused = controller.call('start')
    require(refused.get('result') == 'attention_required'
            and refused.get('code') == 'SERVICE_EXPLICIT_RESUME_REQUIRED', 'ACCEPTANCE_START_BYPASSED_PAUSE')
    unchanged(accepted); passed('stop_exit_lease_and_start_refusal')
    controller.good('resume')
    resumed = controller.wait(healthy)
    require(invocation(resumed) != invocation(initial), 'ACCEPTANCE_RESUME_REUSED_INVOCATION')
    unchanged(accepted); passed('explicit_resume_new_invocation')

    phase('post-write readback failure, exit 2 and no automatic restart')
    update(version=2, rules_fault=True)
    blocked = controller.wait(failed)
    require(blocked['recovery'] == 'OPS_ALERT_READBACK_FAILED'
            and refresh.lineage(state)['pending'] is not None, 'ACCEPTANCE_PENDING_FAILURE_MISSING')
    pending = snapshot(c)
    require(len(pending['history']) == 2, 'ACCEPTANCE_PENDING_GENERATION_INVALID')
    hold = controller.stable_failed(blocked, lambda: unchanged(pending))
    passed('exit_two_prevents_automatic_restart')
    # Pending evidence blocks resume even though its failure pauses the service.
    before = invocation(controller.status())
    refused = controller.call('resume')
    require(refused.get('result') == 'attention_required' and invocation(controller.status()) == before
            and refresh.lineage(state)['pending'] is not None, 'ACCEPTANCE_PENDING_RESUME_ALLOWED')
    unchanged(pending); passed('pending_resume_refused_without_mutation')

    phase('explicit rollback outside service, recovery readback and resume')
    update(version=1)
    rollback = refresh.run('rollback', state)
    require(rollback['result'] == 'rolled_back' and snapshot(c)['configuration'] == accepted['configuration'],
            'ACCEPTANCE_ROLLBACK_FAILED')
    activation.readback(manage.existing(state))
    controller.good('resume')
    recovered = controller.wait(healthy)
    require(invocation(recovered) != invocation(resumed) and not recovered['recovery']
            and refresh.lineage(state)['pending'] is None, 'ACCEPTANCE_RECOVERY_FAILED')
    passed('explicit_rollback_recovery_and_new_invocation')
    phase('stop/remove owned unit and preserve test evidence')
    controller.good('stop'); controller.good('remove')
    final = controller.status()
    require(final['status'] == 'removed' and not final['enabled'] and final['desired_state'] == 'paused',
            'ACCEPTANCE_UNIT_REMOVAL_FAILED')
    passed('unit_removed_audit_preserved')
    isolated.preserve_test_ops(c, engine)
    phase('completed; parent will recheck unit absence before Docker cleanup')
    return {'schema': 1, 'stage': STAGE, 'result': 'passed', 'checked_at': rc.now(),
            'scope': 'native_user_manager_and_docker_with_synthetic_upstreams', 'cases': cases,
            'image_id': record['image'], 'runtime_source_hashes_verified': c['hashes'],
            'manager_version': controller.manager_version, 'interval_seconds': INTERVAL,
            'failed_unit_observation': hold, 'test_unit_name': final['unit_name'],
            'test_unit_removed': True, 'test_worker_exit_verified': True,
            'same_test_ops_container_image_mounts': True, 'synthetic_credentials_preserved': True,
            'initial_cache_entries_preserved': len(c['initial_cache']['success_times']),
            'bootstrap_record_preserved': True, 'refresh_journal_count': 2,
            'test_service_persistence': 'session', 'linger_changed': False, 'autostart_enabled': False,
            'production_worker_started': False, 'production_service_control_requested': False,
            'production_credentials_read': False, 'production_application_api_requests': 0,
            'inference_requested': False, 'image_build_or_pull_requested': False,
            'promql_engine_acceptance': False, 'fresh_business_platform_installation_acceptance': False,
            'logout_or_boot_acceptance': False, 'long_running_acceptance': False,
            'kubernetes_acceptance': 'deferred'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'cleanup', 'worker', 'cleanup-unit'))
    parser.add_argument('--state-dir', type=Path, default=isolated.BASE)
    parser.add_argument('--run-dir', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'run':
            require(args.run_dir is None, 'ACCEPTANCE_RUN_DIR_NOT_ALLOWED')
            report = isolated.run(args.state_dir, scenario='service')
        else:
            require(args.run_dir is not None, 'ACCEPTANCE_RUN_DIR_REQUIRED')
            directory = manage.safe(args.run_dir); record = checked_run(directory)
            if args.action == 'cleanup-unit': report = cleanup_unit(directory)
            else:
                engine = isolated.Engine(record['endpoint'])
                if args.action == 'cleanup':
                    with isolated.run_lock(directory.parent):
                        report = {'stage': STAGE, 'result': 'cleaned', 'cleanup': engine.cleanup(directory),
                                  'run_directory': str(directory)}
                else:
                    try: report = exercise(directory, engine)
                    except local.ERRORS as exc: report = error_report(exc)
                    isolated.write(directory / 'worker-report.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['result'] in ('passed', 'cleaned') else 2
    except local.ERRORS as exc:
        print(json.dumps(error_report(exc)))
        return 2


if __name__ == '__main__':
    def interrupted(signum, frame): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
