#!/usr/bin/env python3
"""Read-only persistence diagnostics for the existing installation; never apply."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops_platform.contracts import require, ReadFailure
from ops_platform.ops import auto_refresh, manage, lifecycle_probe as probe, lifecycle_state as local
from ops_platform.ops import service_lifecycle as service, service_systemd, refresh_worker as worker, worker_state as ws
from ops_platform.transport import strict_json

STAGE = 'O04-persistence-readiness-diagnostics'
# Version evidence, not a deployment address. Unknown code never inherits this review.
REVIEWED_CODE_SHA256 = 'a24257d8636b5842167e74f7b02931c467d105519fb9b5f70ceb577437fccca4'
BOOT_INSPECT = '{"id":{{json .Id}},"image":{{json .Image}},"restart_policy":{{json .HostConfig.RestartPolicy}}}'
GAPS = ('SAVED_POLICY_TRANSITION_NOT_IMPLEMENTED', 'BOOT_REENTRY_NOT_IMPLEMENTED',
        'BOOT_STARTUP_WAIT_NOT_IMPLEMENTED', 'GUARDED_CODE_REBIND_NOT_IMPLEMENTED')


def extra_query(args):
    if len(args) != 8 or args[0] != '--host': return False
    probe.local_endpoint(args[1])
    return (args[2:7] == ['container', 'inspect', '--format', BOOT_INSPECT, '--']
            and probe.HEX.fullmatch(args[7]) is not None)


class ReadOnlyRunner:
    """Guard all existing preflight and additional metadata subprocess calls."""
    def __init__(self, runner): self.runner, self.paths = runner, {}

    def __call__(self, args, **kwargs):
        require(isinstance(args, list) and len(args) >= 2, 'PERSISTENCE_COMMAND_REFUSED')
        names = [name for name, path in self.paths.items() if path and args[0] == path]
        require(len(names) == 1, 'PERSISTENCE_COMMAND_REFUSED')
        tool, tail = names[0], args[1:]
        additional = tool == 'docker' and extra_query(tail)
        unit_query = (tool == 'systemctl' and len(tail) == 6
                      and tail[:4] == ['--user', '--no-pager', '--no-ask-password', 'show']
                      and tail[4] == '--property=' + service_systemd.PROPERTIES
                      and probe.UNIT.fullmatch(tail[5]) is not None)
        require(additional or unit_query or probe.allow(tool, tail), 'PERSISTENCE_COMMAND_REFUSED')
        require(not kwargs.get('shell') and kwargs.get('stdin') == subprocess.DEVNULL
                and kwargs.get('capture_output') is True and kwargs.get('text') is True
                and 0 < kwargs.get('timeout', 0) <= 10, 'PERSISTENCE_COMMAND_REFUSED')
        return self.runner(args, **kwargs)


class Source:
    def __init__(self, directory, reader=None):
        self.directory = directory
        self.reader = reader or probe.Reader()
        guard = ReadOnlyRunner(self.reader.runner)
        guard.paths = dict(self.reader.paths)
        self.reader.runner = guard

    def restart_policy(self, report):
        docker = report['docker']; ops = docker.get('ops', {})
        require(ops.get('owner_and_mounts_verified') is True and probe.HEX.fullmatch(ops['container_id']),
                'OPS_OWNERSHIP_REQUIRED')
        args = ['--host', probe.local_endpoint(docker['endpoint']), 'container', 'inspect',
                '--format', BOOT_INSPECT, '--', ops['container_id']]
        self.reader.calls += 1
        try:
            response = self.reader.runner([self.reader.paths['docker'], *args], env=self.reader.env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False)
        except subprocess.TimeoutExpired: raise ReadFailure('DOCKER_RESTART_POLICY_QUERY_TIMEOUT') from None
        except OSError: raise ReadFailure('DOCKER_RESTART_POLICY_QUERY_UNAVAILABLE') from None
        require(response.returncode == 0, 'DOCKER_RESTART_POLICY_QUERY_FAILED')
        require(len(response.stdout) <= 16384, 'DOCKER_RESTART_POLICY_RESPONSE_TOO_LARGE')
        value = strict_json(response.stdout)
        require(isinstance(value, dict) and set(value) == {'id', 'image', 'restart_policy'}
                and value['id'] == ops['container_id'] and value['image'] == ops['image_id'], 'DOCKER_IDENTITY_CHANGED')
        policy = value['restart_policy']
        require(isinstance(policy, dict) and set(policy) == {'Name', 'MaximumRetryCount'}
                and policy['Name'] in ('no', 'on-failure', 'always', 'unless-stopped')
                and type(policy['MaximumRetryCount']) is int and policy['MaximumRetryCount'] >= 0,
                'INVALID_DOCKER_RESTART_POLICY')
        return {'status': 'ok', 'name': policy['Name'], 'maximum_retry_count': policy['MaximumRetryCount'],
                'daemon_restart_candidate': policy['Name'] in ('always', 'unless-stopped'),
                'requires_container_not_deliberately_stopped': policy['Name'] == 'unless-stopped',
                'actual_boot_start_verified': False}

    def take(self):
        report = auto_refresh.run('status', self.directory, reader=self.reader)
        value = {'report': report, 'errors': [], 'manifest': None, 'worker_manifest': None,
                 'actual_binding': None, 'code_sha256': worker.code_hash(ROOT),
                 'restart_policy': {'status': 'not_checked'}}
        if report['management']['status'] == 'owned':
            try:
                value['manifest'] = service.load(self.directory)
                value['worker_manifest'] = ws.Store(self.directory).manifest()
                owner = ws.read(self.directory / 'owner.json')
                docker = report['docker']; ops = docker.get('ops', {})
                if docker.get('status') == 'ok' and ops.get('owner_and_mounts_verified') is True:
                    value['actual_binding'] = {'directory': str(self.directory), 'owner': owner,
                        'code_sha256': value['code_sha256'], 'python': str(Path(sys.executable).resolve()),
                        'python_version': list(sys.version_info[:3]), 'docker_path': self.reader.paths['docker'],
                        'daemon_id': docker['daemon_id'], 'ops_container_id': ops['container_id'], 'ops_image_id': ops['image_id']}
            except local.ERRORS as exc: value['errors'].append(local.error_code(exc))
        if report.get('docker', {}).get('ops', {}).get('owner_and_mounts_verified') is True:
            try: value['restart_policy'] = self.restart_policy(report)
            except local.ERRORS as exc:
                code = local.error_code(exc); value['errors'].append(code)
                value['restart_policy'] = {'status': 'unavailable', 'code': code}
        return value


def stable_projection(value):
    """Exclude advancing heartbeat/cycle counters, retain ownership/control changes."""
    report = value['report']; current = report['management']; systemd = report.get('systemd', {})
    stored = current.get('worker', {})
    return {**{k: value[k] for k in ('manifest', 'worker_manifest', 'actual_binding', 'code_sha256', 'restart_policy', 'errors')},
        'installation': report['installation'], 'docker': report.get('docker'),
        'manager': {k: current.get(k) for k in ('status', 'unit_name', 'unit_path', 'persistence', 'enabled', 'provider_status', 'desired_state', 'recovery')},
        'control': stored.get('control'), 'intent': stored.get('intent'),
        'systemd': {k: systemd.get(k) for k in ('status', 'linger', 'environment', 'manager_version')},
        'blockers': report['blockers'], 'prepared_state_stable': report.get('prepared_state_stable')}


def project(value, requested):
    report = value['report']; current = report['management']; manifest = value['manifest']
    docker = report.get('docker', {}); systemd = report.get('systemd', {})
    saved = manifest['persistence'] if manifest else None
    reasons = {'installation': list(report['blockers']) + list(value['errors']), 'environment': [], 'implementation': [], 'transition': []}
    warnings = list(report['warnings'])
    if current.get('status') != 'owned': reasons['installation'].append('OWNED_SERVICE_REQUIRED')
    if report.get('prepared_state_stable') is not True: reasons['installation'].append('PREPARED_STATE_NOT_STABLE')
    if current.get('desired_state') == 'paused': reasons['installation'].append('WORKER_PAUSED')
    if current.get('recovery'): reasons['installation'].append('WORKER_RECOVERY_REQUIRED')
    if current.get('worker', {}).get('intent') and current['worker']['intent'].get('phase') != 'completed':
        reasons['installation'].append('WORKER_UNFINISHED_INTENT')
    binding = {'status': 'not_verified', 'service_and_worker_equal': None, 'runtime_equal': None}
    if manifest and value['worker_manifest']:
        paired = manifest['binding'] == value['worker_manifest']['binding']
        live = value['actual_binding'] is not None and value['actual_binding'] == manifest['binding']
        binding.update(service_and_worker_equal=paired, runtime_equal=live, status='verified' if paired and live else 'mismatch')
        if not paired: reasons['installation'].append('SERVICE_WORKER_BINDING_MISMATCH')
        if not live: reasons['installation'].append('SERVICE_RUNTIME_BINDING_UNVERIFIED' if value['actual_binding'] is None else 'SERVICE_BINDING_CHANGED')
    else: reasons['installation'].append('SERVICE_BINDING_UNVERIFIED')
    known = value['code_sha256'] == REVIEWED_CODE_SHA256
    capabilities = {'review_status': 'reviewed_current_source' if known else 'unreviewed_source',
                    'source_sha256': value['code_sha256'], 'reviewed_source_sha256': REVIEWED_CODE_SHA256,
                    'saved_policy_transition': False if known else None, 'boot_reentry': False if known else None,
                    'bounded_boot_startup_wait': False if known else None, 'guarded_code_rebind': False if known else None}
    linger = systemd.get('linger', 'unknown')
    require(type(linger) is bool or linger == 'unknown', 'INVALID_LINGER_METADATA')
    if requested == 'boot':
        reasons['implementation'] += list(GAPS) if known else ['WORKER_BOOT_CAPABILITIES_UNREVIEWED']
        if linger is not True: reasons['environment'].append('LINGER_NOT_ENABLED' if linger is False else 'LINGER_UNDETERMINED')
        if systemd.get('status') != 'ok': reasons['environment'].append('SYSTEMD_USER_MANAGER_UNAVAILABLE')
        policy = value['restart_policy']
        if policy.get('status') != 'ok': reasons['environment'].append('OPS_RESTART_POLICY_UNVERIFIED')
        elif not policy['daemon_restart_candidate']: reasons['environment'].append('OPS_RESTART_POLICY_NOT_BOOT_CANDIDATE')
        reasons['environment'] += ['DOCKER_DAEMON_BOOT_START_UNVERIFIED', 'BOOT_PATH_AVAILABILITY_UNVERIFIED']
        if current.get('enabled') is not True: reasons['transition'].append('OWNED_AUTOSTART_LINK_NOT_ENABLED')
    elif current.get('enabled') is True: reasons['transition'].append('SESSION_AUTOSTART_LINK_ENABLED')
    if saved is not None and requested != saved: reasons['transition'].append('SAVED_POLICY_DIFFERS_FROM_REQUEST')
    if saved is not None and current.get('persistence') != saved: reasons['installation'].append('SERVICE_POLICY_CHANGED_DURING_READ')
    running = current.get('provider_status', {}).get('ActiveState') == 'active'
    if current.get('status') == 'owned' and (not running or current.get('current_invocation_verified') is not True):
        warnings.append('CURRENT_WORKER_NOT_VERIFIED_RUNNING')
    if current.get('evaluation_status') == 'degraded_retrying': warnings.append('WORKER_EVALUATION_DEGRADED')
    return {'policy': {'requested': requested, 'saved': saved,
                'environment_proposal': report.get('persistence', {}).get('selected'),
                'effective_saved_policy': saved, 'transition_required': None if saved is None else saved != requested,
                'policy_written': False},
        'installation': {k: report['installation'].get(k) for k in ('status', 'state_dir', 'installation_id', 'compose_project')},
        'current_service': {k: current.get(k) for k in ('status', 'unit_name', 'persistence', 'enabled', 'desired_state', 'recovery',
             'provider_status', 'current_invocation_verified', 'evaluation_status')},
        'binding': binding, 'worker_capabilities': capabilities,
        'environment': {'user_manager_status': systemd.get('status'), 'manager_version': systemd.get('manager_version'),
            'linger': linger, 'boot_manager_available': linger is True and systemd.get('status') == 'ok',
            'user_runtime': systemd.get('environment', {}),
            'docker': {k: docker.get(k) for k in ('status', 'endpoint_source', 'local_daemon', 'rootful', 'daemon_id')},
            'ops': deepcopy(docker.get('ops', {})), 'ops_restart_policy': deepcopy(value['restart_policy']),
            'daemon_boot_start': 'not_verified', 'paths_available_at_boot': 'not_verified'},
        'preflight': {k: report.get(k) for k in ('result', 'blockers', 'warnings', 'prepared_state_stable')},
        'blocking_reasons': reasons, 'warnings': list(dict.fromkeys(warnings))}


def run(directory, requested='boot', source_factory=Source):
    result = {'schema': 1, 'stage': STAGE, 'result': 'attention_required', 'diagnostic_status': 'incomplete',
              'scope': 'read_only_existing_installation_persistence_metadata', 'checked_at': worker.activation.rc.now(),
              'effects': {'files_written': [], 'configuration_written': False, 'service_control_requested': False,
                         'application_credential_contents_read': False, 'application_api_requests': 0,
                         'inference_requested': False, 'linger_changed': False, 'watch_started': False},
              'activation_supported': False, 'boot_ready': False, 'logout_or_boot_acceptance': False,
              'long_running_acceptance': False, 'kubernetes_acceptance': 'deferred',
              'snapshot_stable': False, 'historical_observation_reused': False,
              'next_step': 'review_persistence_diagnostics'}
    source = None
    try:
        require(requested in ('boot', 'session'), 'INVALID_PERSISTENCE_REQUEST')
        directory = Path(os.path.abspath(Path(directory).expanduser()))
        source = source_factory(directory)
        before = source.take()
        result.update(project(before, requested))
        after = source.take()
        first_reasons = deepcopy(result['blocking_reasons'])
        first_warnings = list(result['warnings'])
        result['initial_service'] = deepcopy(result['current_service'])
        result.update(project(after, requested))
        for key, codes in first_reasons.items(): result['blocking_reasons'][key] += codes
        result['warnings'] = list(dict.fromkeys(first_warnings + result['warnings']))
        result['snapshot_stable'] = stable_projection(before) == stable_projection(after)
        result['samples'] = 2
        if not result['snapshot_stable']:
            result['blocking_reasons']['installation'].append('PERSISTENCE_SNAPSHOT_CHANGED')
        # Keep the latest running state; advancing evaluations are not a policy mutation.
        result['last_service'] = {k: after['report']['management'].get(k) for k in
            ('provider_status', 'current_invocation_verified', 'evaluation_status', 'desired_state', 'recovery')}
        for key, codes in result['blocking_reasons'].items(): result['blocking_reasons'][key] = list(dict.fromkeys(codes))
        result['blockers'] = list(dict.fromkeys(code for codes in result['blocking_reasons'].values() for code in codes))
        result['diagnostic_status'] = 'completed'
        if not result['blockers']:
            result['result'] = 'passed'  # Session metadata only; never boot/activation acceptance.
        result['next_step'] = ('implement_guarded_boot_lifecycle_and_policy_transition' if requested == 'boot'
                               and result['snapshot_stable'] and result['binding']['status'] == 'verified'
                               else 'review_reported_persistence_conditions')
    except local.ERRORS as exc: result['code'] = local.error_code(exc)
    except KeyboardInterrupt: result['code'] = 'PERSISTENCE_DIAGNOSTICS_INTERRUPTED'
    finally:
        if source is not None and hasattr(source, 'reader'): result['metadata_commands'] = source.reader.calls
        result['finished_at'] = worker.activation.rc.now()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=manage.STATE)
    parser.add_argument('--target', choices=('boot', 'session'), default='boot')
    args = parser.parse_args(argv)
    result = run(args.state_dir, args.target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
