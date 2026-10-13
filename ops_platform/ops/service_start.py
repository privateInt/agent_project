#!/usr/bin/env python3
"""Start the discovered Ops refresh service and observe one new accepted evaluation."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops_platform.contracts import require
from ops_platform.ops import auto_refresh, manage, lifecycle_probe as probe, lifecycle_state as local
from ops_platform.ops import service_lifecycle as service, refresh_worker as worker
from ops_platform.ops.service_systemd import Systemd, command_details

STAGE = 'O04-production-auto-refresh-start'


class Controller:
    """Reuse the owned lifecycle; observation never controls or repairs a service."""
    def __init__(self, directory, persistence):
        self.directory = directory
        self.reader = probe.Reader()
        self.report = auto_refresh.run('plan', directory, persistence, reader=self.reader)
        self.backend = None

    def connect(self):
        self.backend = Systemd(self.reader, self.report['systemd'], self.report['management']['unit_name'])

    def manifest(self): return service.load(self.directory)

    def verify_binding(self, manifest):
        require(worker.runtime_snapshot(self.directory) == manifest['binding'], 'SERVICE_BINDING_CHANGED')

    def start(self, persistence, interval):
        return service.run('start', self.directory, persistence, interval, reader=self.reader, backend=self.backend)

    def status(self): return service.status(self.directory, self.backend)


def checkpoint(value):
    provider = value.get('provider_status') or {}
    invocation = provider.get('InvocationID')
    observation = (value.get('worker') or {}).get('last_observation') or {}
    count = observation.get('completed_cycles', 0)
    if observation.get('service_invocation') != invocation or type(count) is not int or count < 0: count = 0
    return invocation, count


def projection(value):
    """No manifest, command environment, credential paths or arbitrary exceptions."""
    observation = (value.get('worker') or {}).get('last_observation') or {}
    outcome = observation.get('outcome') or {}
    return {
        'unit_name': value.get('unit_name'), 'provider_status': value.get('provider_status'),
        'persistence': value.get('persistence'), 'autostart_enabled': value.get('enabled'),
        'desired_state': value.get('desired_state'), 'recovery': value.get('recovery'),
        'current_invocation_verified': value.get('current_invocation_verified', False),
        'evaluation_status': value.get('evaluation_status'),
        'completed_cycles': observation.get('completed_cycles', 0),
        'observation_checked_at': observation.get('checked_at'),
        'outcome': {key: outcome[key] for key in ('result', 'classification', 'code', 'generation',
            'configuration_changed', 'restart_requested', 'child_process_group_stopped') if key in outcome}}


def observe(controller, before, manifest, result, wait_seconds, clock, sleep, submitted_invocation=None):
    initial_invocation, initial_count = checkpoint(before)
    expected = initial_invocation if (before.get('provider_status') or {}).get('ActiveState') in ('active', 'activating') else submitted_invocation
    started = clock()
    while True:
        require(controller.manifest() == manifest, 'SERVICE_MANIFEST_CHANGED_DURING_OBSERVATION')
        current = controller.status()
        result['management'] = projection(current)
        result['observation_seconds'] = round(clock() - started, 3)
        provider = current['provider_status']
        result['worker_running_at_last_observation'] = provider['ActiveState'] == 'active'
        require(not current.get('recovery'), 'WORKER_RECOVERY_REQUIRED')
        require(current.get('desired_state') == 'running', 'WORKER_PAUSED')
        intent = current['worker'].get('intent')
        # Open intent during evaluation is legitimate; the worker owns the
        # deadline and recovery gate. A waiting success cannot retain one.
        require(provider['ActiveState'] not in ('failed', 'deactivating'), 'SERVICE_NOT_RUNNING')
        invocation, count = checkpoint(current)
        if provider['ActiveState'] in ('active', 'activating') and invocation:
            if expected is None: expected = invocation
            require(invocation == expected, 'SERVICE_INVOCATION_CHANGED_DURING_OBSERVATION')
        observation = current['worker'].get('last_observation') or {}
        outcome = observation.get('outcome') or {}
        fresh = invocation != initial_invocation or count > initial_count
        if (provider['ActiveState'] == 'active' and current.get('current_invocation_verified') is True
                and observation.get('service_invocation') == expected and expected is not None
                and observation.get('phase') == 'waiting' and current.get('evaluation_status') == 'healthy'
                and fresh and count >= 1):
            require(not intent or intent.get('phase') == 'completed', 'WORKER_UNFINISHED_INTENT')
            require(outcome.get('result') == 'passed' and outcome.get('classification') == 'accepted'
                    and outcome.get('child_process_group_stopped') is True
                    and type(outcome.get('configuration_changed')) is bool
                    and type(outcome.get('restart_requested')) is bool, 'WORKER_ACCEPTANCE_EVIDENCE_INVALID')
            result.update(result='passed', fresh_evaluation_verified=True,
                          historical_observation_reused=False, worker_left_running=True,
                          next_step='observe_session_service_over_time')
            return result
        require(clock() - started < wait_seconds, 'SERVICE_EVALUATION_WAIT_TIMEOUT')
        sleep(min(1.0, max(0.0, wait_seconds - (clock() - started))))


def run(action, directory, persistence='session', interval=60, wait_seconds=None,
        controller_factory=Controller, _clock=time.monotonic, _sleep=time.sleep):
    result = {'schema': 1, 'stage': STAGE, 'action': action, 'result': 'attention_required',
              'scope': 'production_owned_service_start_and_new_evaluation',
              'checked_at': worker.activation.rc.now(), 'start_action_called': False,
              'start_request_submitted': False, 'fresh_evaluation_verified': False,
              'worker_running_at_last_observation': None, 'observer_requested_stop': False,
              'inference_requested': False, 'image_build_or_pull_requested': False,
              'business_service_control_requested': False, 'linger_changed': False,
              'logout_or_boot_acceptance': False, 'long_running_acceptance': False,
              'kubernetes_acceptance': 'deferred', 'observer_application_api_requests': 0,
              'worker_application_readback': 'existing_discovery_refresh_GET_and_ops_auth_lifecycle',
              'next_step': 'inspect_status_and_reported_gate'}
    try:
        require(action in ('run', 'check'), 'INVALID_SERVICE_START_ACTION')
        require(persistence in ('session', 'boot', 'auto') and type(interval) is int and 30 <= interval <= 3600,
                'INVALID_WORKER_LIMIT')
        require(wait_seconds is None or type(wait_seconds) is int and 1 <= wait_seconds <= 7200,
                'INVALID_OBSERVATION_LIMIT')
        directory = Path(os.path.abspath(Path(directory).expanduser()))
        controller = controller_factory(directory, persistence if action == 'run' else 'auto')
        report = controller.report
        result['preflight'] = {key: report.get(key) for key in ('result', 'blockers', 'warnings', 'prepared_state_stable')}
        result['managed_fields_at_preflight'] = report.get('installation', {}).get('provenance', {}).get('effective_managed_fields', [])
        require(report.get('environment_ready') is True and not report.get('blockers'), 'SERVICE_PREFLIGHT_BLOCKED')
        before = report['management']
        submitted_invocation = None
        controller.connect()
        if before['status'] == 'owned':
            existing = controller.manifest()
            if action == 'run':
                require(persistence == 'auto' or persistence == existing['persistence'],
                        'SERVICE_POLICY_CHANGE_REQUIRES_REVIEW')
            controller.verify_binding(existing)
            before = controller.status()
        elif action == 'check': require(False, 'SERVICE_NOT_INSTALLED')
        if action == 'run':
            result['start_action_called'] = True
            submitted = controller.start(persistence, interval)
            result['start_request_submitted'] = submitted.get('start_request_submitted', False)
            result['already_running'] = submitted.get('already_running', False)
            result['unit_written'] = submitted.get('unit_written', False)
            detail = command_details(submitted.get('service_command'))
            if detail is not None: result['service_command'] = detail
            require(submitted['result'] == 'passed', submitted.get('code', 'SERVICE_START_FAILED'))
            provider = submitted.get('management', {}).get('provider_status', {})
            if provider.get('ActiveState') in ('active', 'activating'):
                submitted_invocation = provider.get('InvocationID') or None
        else:
            require(before.get('provider_status', {}).get('ActiveState') in ('active', 'activating'), 'SERVICE_NOT_RUNNING')
        manifest = controller.manifest()
        result['interval_seconds'] = manifest['interval_seconds']
        result['persistence'] = manifest['persistence']
        result['ops_container_id'] = manifest['binding']['ops_container_id']
        result['ops_image_id'] = manifest['binding']['ops_image_id']
        result['python_executable'] = manifest['binding'].get('python')
        budget = wait_seconds if wait_seconds is not None else manifest['interval_seconds'] + manifest['deadline_seconds'] + 30
        result['observation_budget_seconds'] = budget
        return observe(controller, before, manifest, result, budget, _clock, _sleep, submitted_invocation)
    except local.ERRORS as exc:
        result['code'] = local.error_code(exc)
        detail = command_details(getattr(exc, 'service_command', None))
        if detail is not None: result['service_command'] = detail
    except KeyboardInterrupt:
        result['code'] = 'SERVICE_OBSERVATION_INTERRUPTED'
    # This command does not silently stop a worker after a read error or a
    # bounded observation timeout. Existing durable worker gates remain owner.
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'check'))
    parser.add_argument('--state-dir', type=Path, default=manage.STATE)
    parser.add_argument('--persistence', choices=('session', 'boot', 'auto'), default='session')
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--wait-seconds', type=int)
    args = parser.parse_args(argv)
    result = run(args.action, args.state_dir, args.persistence, args.interval, args.wait_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
