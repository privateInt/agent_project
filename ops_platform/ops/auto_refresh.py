#!/usr/bin/env python3
"""Discovered Ops refresh lifecycle: read-only plan/status and explicit service actions."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops_platform.contracts import require
from ops_platform.ops import manage, lifecycle_probe as probe, lifecycle_state as state

STAGE = 'O04-auto-refresh-preflight'


def run(action, directory, persistence='auto', reader=None, root=ROOT):
    require(action in ('plan', 'status') and persistence in ('auto', 'session', 'boot'), 'INVALID_PREFLIGHT_ACTION')
    directory = Path(os.path.abspath(Path(directory).expanduser()))
    reader = reader or probe.Reader()
    result = {'schema': 1, 'stage': STAGE, 'action': action, 'scope': 'read_only_lifecycle_metadata',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'runtime': {'python_version': '.'.join(map(str, sys.version_info[:3])),
                          'python_executable': str(Path(sys.executable).resolve()), 'platform': sys.platform},
              'effects': {'files_changed': [], 'service_control_requested': False, 'configuration_written': False,
                          'application_credentials_read': False, 'application_credentials_sent': False,
                          'upstream_api_requests': 0, 'inference_requested': False, 'watch_started': False},
              'capabilities': {'plan': True, 'status': True, 'install': True, 'start': True,
                               'stop': True, 'resume': True, 'remove': True, 'kubernetes': False},
              'worker_status': 'guarded_service_implemented', 'evaluation_status': 'not_performed',
              'desired_state': None, 'blockers': [], 'warnings': []}
    if sys.platform != 'linux' or sys.version_info < (3, 10):
        result.update(result='attention_required', environment_ready=False, blockers=['LINUX_PYTHON_310_REQUIRED'])
        return result
    prepared, cfg, owner = state.inspect_state(directory, root)
    result['installation'] = prepared
    unit_name = 'agent-project-ops-refresh-' + owner['owner'].replace('-', '') + '.service' if owner else None
    management_dir = directory.parent / 'auto-refresh' / owner['owner'] if owner else None
    result['management'] = {'status': 'not_installed' if owner else 'unknown', 'unit_name': unit_name}
    if management_dir:
        try:
            manage.safe(management_dir)
            if management_dir.exists():
                from ops_platform.ops import worker_state
                if (management_dir / 'service.json').exists():
                    result['management'].update(status='record_requires_provider_verification')
                elif set(p.name for p in management_dir.iterdir()) == {'worker'}:
                    manage.private(management_dir)
                    result['worker'] = worker_state.status(directory)
                    result['management'].update(status='core_only_service_not_installed')
                    result['desired_state'] = result['worker']['control']['desired']
                    if result['worker']['control']['recovery']:
                        result['management']['code'] = 'WORKER_RECOVERY_REQUIRED'
                    elif result['worker']['intent'] and result['worker']['intent'].get('phase') != 'completed':
                        result['management']['code'] = 'WORKER_UNFINISHED_INTENT'
                else:
                    result['management'].update(status='unsupported_state', code='MANAGEMENT_STATE_ALREADY_EXISTS')
        except state.ERRORS as exc:
            result['management'].update(status='unknown', code=state.error_code(exc))
    for key, function in [('docker', lambda: probe.docker(reader, directory, cfg, owner)),
                          ('systemd', lambda: probe.systemd(reader, unit_name))]:
        try: result[key] = function()
        except state.ERRORS as exc: result[key] = {'status': 'unavailable', 'code': state.error_code(exc)}
    if result['management']['status'] == 'record_requires_provider_verification':
        try:
            from ops_platform.ops import service_lifecycle, service_systemd
            backend = service_systemd.Systemd(reader, result['systemd'], unit_name)
            result['management'] = service_lifecycle.status(directory, backend)
            result['desired_state'] = result['management']['desired_state']
            result['evaluation_status'] = result['management']['evaluation_status']
            if result['management']['recovery']:
                result['management']['code'] = 'WORKER_RECOVERY_REQUIRED'
            intent = result['management']['worker'].get('intent')
            if intent and intent.get('phase') != 'completed': result['management']['code'] = 'WORKER_UNFINISHED_INTENT'
            result['systemd']['unit'] = {'status': 'owned', 'name': unit_name}
        except state.ERRORS as exc:
            result['management'].update(status='conflict', code=state.error_code(exc))
    elif result['systemd'].get('unit', {}).get('status') == 'unowned':
        result['management'].update(status='conflict', code='MANAGEMENT_UNIT_ALREADY_EXISTS')
    elif result['systemd'].get('unit', {}).get('status') != 'absent' and owner and result['management']['status'] == 'not_installed':
        result['management']['status'] = 'unknown'
    linger = result['systemd'].get('linger', 'unknown')
    selected = persistence if persistence != 'auto' else ('boot' if linger is True else 'session' if linger is False else 'unknown')
    result['persistence'] = {'requested': persistence, 'selected': selected, 'linger': linger,
                             'boot_available': linger is True and result['systemd']['status'] == 'ok',
                             'policy_written': False, 'boot_acceptance_verified': False}
    if persistence == 'boot' and linger is not True: result['blockers'].append('BOOT_PERSISTENCE_UNAVAILABLE')
    if selected == 'unknown': result['blockers'].append('PERSISTENCE_UNDETERMINED')
    if selected == 'session': result['warnings'].append('SESSION_SCOPE_ONLY')
    for part in (prepared, prepared['configuration'], prepared['provenance'], result['management'],
                 result['docker'], result['docker'].get('ops', {}), result['systemd']):
        if part.get('code'): result['blockers'].append(part['code'])
    for item in prepared['configuration'].get('file_checks', []):
        if item.get('code') and item['status'] == 'error': result['blockers'].append(item['code'])
        if item.get('code') and item['status'] == 'warning': result['warnings'].append(item['code'])
    if owner:
        # Recheck local state after external metadata reads; never reuse a changed preparation as a ready plan.
        after, after_cfg, after_owner = state.inspect_state(directory, root)
        stable = after == prepared and after_cfg == cfg and after_owner == owner
        result['prepared_state_stable'] = stable
        if not stable: result['blockers'].append('PREPARED_STATE_CHANGED_DURING_CHECK')
    else: result['prepared_state_stable'] = False
    result['blockers'] = list(dict.fromkeys(result['blockers']))
    result['warnings'] = list(dict.fromkeys(result['warnings']))
    result['environment_ready'] = not result['blockers'] and prepared['status'] == 'ok' and result['prepared_state_stable']
    result['result'] = 'passed' if result['environment_ready'] else 'attention_required'
    result['metadata_commands'] = reader.calls
    result['command_paths'] = reader.paths
    result['recovery'] = prepared['provenance'].get('pending')
    result['management_start_supported'] = result['environment_ready']
    result['native_service_acceptance_verified'] = False
    result['next_step'] = ('isolated_systemd_and_docker_acceptance' if result['environment_ready']
                           else 'resolve_reported_preflight_blockers')
    if action == 'plan':
        result['proposal'] = {'provider': 'systemd-user', 'unit_name': unit_name,
                              'management_dir': str(management_dir) if management_dir else None,
                              'interval_seconds': 60, 'persistence': selected,
                              'owned_fields': prepared['provenance'].get('effective_managed_fields', []),
                              'activation_requested': False, 'unit_generated': False}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'status', 'install', 'start', 'stop', 'resume', 'remove'))
    parser.add_argument('--state-dir', type=Path, default=manage.STATE)
    parser.add_argument('--persistence', choices=('auto', 'session', 'boot'), default='auto')
    parser.add_argument('--interval', type=int, default=60)
    args = parser.parse_args(argv)
    try:
        if args.action in ('plan', 'status'): result = run(args.action, args.state_dir, args.persistence)
        else:
            from ops_platform.ops import service_lifecycle
            result = service_lifecycle.run(args.action, args.state_dir, args.persistence, args.interval)
    except state.ERRORS as exc:
        readonly = args.action in ('plan', 'status')
        result = {'schema': 1, 'stage': STAGE, 'result': 'attention_required', 'code': state.error_code(exc),
                  'action': args.action, 'service_control_requested': False if readonly else None,
                  'files_changed': [] if readonly else None, 'inference_requested': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
