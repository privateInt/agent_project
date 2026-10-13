#!/usr/bin/env python3
"""Bounded foreground watch acceptance in disposable Docker/Ops state."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.infra.observability.ops.backup_preflight import Unavailable
from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.ops import docker_acceptance as isolated
from src.ops.platform.ops import manage, refresh_discovery as refresh, activate_discovery as activation
from src.ops.platform.ops import restart_check as rc

STAGE = 'O04-isolated-docker-watch-acceptance'
BASE = isolated.BASE  # Share the run/cleanup lock with the preceding acceptance tool.
INTERVAL = 30


def snapshot(context):
    state = context['state']
    lineage = refresh.lineage(state)
    return {
        'configuration': {name: (state/name).read_bytes() for name in
                          ('host-config.json', 'config.json', 'owner.json', 'compose.env', 'access-key')},
        'history': {str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path, _ in lineage['records']},
        'pending': bool(lineage['pending']),
        'started': context['docker'].inspect(context['ident'])['started'],
    }


def unchanged(before, after, report):
    require(before == after, 'WATCH_NOOP_MUTATED_STATE')
    require(report.get('result') == 'passed' and not report.get('restart_requested')
            and not report.get('provenance_journal_written'), 'WATCH_NOOP_REPORT_INVALID')


def exercise(directory, engine, changed_port=isolated.change_port,
             read_hashes=isolated.runtime_hashes, sleep_fn=time.sleep):
    record = isolated.validate_run(directory)
    require(record.get('scenario') == 'watch', 'ACCEPTANCE_SCENARIO_INVALID')
    print('[watch-acceptance] isolated preparation', file=sys.stderr, flush=True)
    context = isolated.prepare_test_ops(directory, engine, read_hashes)
    state = context['state']
    control = context['control']
    waits, cycles, invocations = [], [], []
    real_interval = sleep_fn is time.sleep
    restarts = 0

    def wait(seconds):
        require(seconds == INTERVAL, 'WATCH_INTERVAL_CHANGED')
        begin = time.monotonic()
        sleep_fn(seconds)
        elapsed = time.monotonic() - begin
        if real_interval:
            require(elapsed >= seconds - .01, 'WATCH_INTERVAL_TOO_SHORT')
        waits.append(round(elapsed, 3))

    def run_cycles(name, limit, before_cycle, verify):
        number = 0
        before = None

        def perform(action, directory):
            nonlocal number, before
            number += 1
            require(number <= limit and action == 'apply' and directory == state, 'WATCH_SCOPE_CHANGED')
            engine.protect(record['protected'])
            before_cycle(number)
            before = snapshot(context)
            return refresh.run(action, directory)

        def emit(report):
            nonlocal restarts
            after = snapshot(context)
            require(report.get('watch_enabled') is True, 'WATCH_NOT_ENABLED')
            verify(number, before, after, report)
            restarts += int(before['started'] != after['started'])
            cycles.append({'phase': name, 'cycle': number, 'result': report['result'],
                           'code': report.get('code'),
                           'restart_observed': before['started'] != after['started'],
                           'journal_count': len(after['history']),
                           'stopped_for_recovery': report.get('watch_stopped_for_recovery', False)})
            engine.protect(record['protected'])
            print('[watch-acceptance] ' + name + ' ' + str(number) + '/' + str(limit)
                  + ': ' + report['result'], file=sys.stderr, flush=True)

        count = len(waits)
        code = refresh.watch(state, interval=INTERVAL, cycles=limit,
                             run_fn=perform, emit=emit, sleep=wait)
        invocations.append({'phase': name, 'requested_cycles': limit, 'completed_cycles': number,
                            'exit_code': code, 'wait_count': len(waits)-count})
        return code, number

    cookie = None

    def repeated_before(number):
        nonlocal cookie
        if number == 2:
            changed_port(directory, engine)
            isolated.write(control, {'version': 1})
            cookie = rc.API(manage.existing(state)).login()
        elif number == 4:
            isolated.write(control, {'version': 1, 'health_fault': True})
        elif number == 6:
            isolated.write(control, {'version': 1})

    def repeated_verify(number, before, after, report):
        if number == 2:
            require(report.get('result') == 'passed' and report.get('changed_fields') ==
                    ['platforms.model.url', 'resource_profile'] and report.get('restart_requested')
                    and report.get('provenance_journal_written') and report.get('generation') == 1,
                    'WATCH_CHANGED_REFRESH_FAILED')
            require(before['started'] != after['started'] and len(after['history']) == 1
                    and not after['pending'] and before['configuration'] != after['configuration'],
                    'WATCH_CHANGE_NOT_APPLIED_ONCE')
            rc.API(manage.existing(state)).expired(cookie)
        elif number in (4, 5):
            require(report.get('result') == 'attention_required' and report.get('code') ==
                    'VERIFIED_DISCOVERY_REQUIRED' and report.get('watch_stopped_for_recovery') is False,
                    'WATCH_DISCOVERY_RETRY_FAILED')
            require(before == after, 'WATCH_DISCOVERY_FAILURE_MUTATED_STATE')
        else:
            unchanged(before, after, report)

    require(run_cycles('change_failure_recovery', 6, repeated_before, repeated_verify) == (0, 6),
            'WATCH_CYCLE_LIMIT_FAILED')
    require(restarts == 1 and len(waits) == 5, 'WATCH_DUPLICATE_RESTART_OR_WAIT')
    accepted = snapshot(context)
    require(not accepted['pending'] and len(accepted['history']) == 1, 'WATCH_ACCEPTED_STATE_INVALID')

    def pending_before(number):
        require(number == 1, 'WATCH_RETRIED_PENDING_OPERATION')
        isolated.write(control, {'version': 2, 'rules_fault': True})

    def pending_verify(number, before, after, report):
        require(report.get('result') == 'attention_required' and report.get('code') ==
                'OPS_ALERT_READBACK_FAILED' and report.get('watch_stopped_for_recovery') is True,
                'WATCH_PENDING_STOP_FAILED')
        require(after['pending'] and len(after['history']) == 2 and before['started'] != after['started']
                and before['configuration'] != after['configuration'], 'WATCH_PENDING_STATE_INVALID')
        require(all(after['history'].get(k) == v for k, v in before['history'].items()),
                'WATCH_ACCEPTED_RECORD_CHANGED')

    require(run_cycles('pending_stops_watch', 3, pending_before, pending_verify) == (2, 1),
            'WATCH_PENDING_DID_NOT_STOP')
    require(restarts == 2 and len(waits) == 5, 'WATCH_PENDING_RESTART_LOOP')
    print('[watch-acceptance] explicit rollback after watch returned', file=sys.stderr, flush=True)
    # Recovery is explicitly outside watch. No automatic rollback or implicit retry.
    before_rollback = snapshot(context)
    isolated.write(control, {'version': 1})
    rollback = refresh.run('rollback', state)
    after_rollback = snapshot(context)
    require(rollback.get('result') == 'rolled_back' and not after_rollback['pending']
            and after_rollback['configuration'] == accepted['configuration']
            and before_rollback['started'] != after_rollback['started'], 'WATCH_EXPLICIT_ROLLBACK_FAILED')
    restarts += 1
    activation.readback(manage.existing(state))
    require(all(after_rollback['history'].get(k) == v for k, v in accepted['history'].items()),
            'WATCH_ACCEPTED_RECORD_CHANGED')
    require(run_cycles('after_explicit_rollback', 2, lambda number: None,
                       lambda number, before, after, report: unchanged(before, after, report)) == (0, 2),
            'WATCH_RECOVERY_FOLLOWUP_FAILED')
    require(snapshot(context) == after_rollback and restarts == 3 and len(waits) == 6,
            'WATCH_FINAL_STATE_CHANGED')
    isolated.preserve_test_ops(context, engine)
    print('[watch-acceptance] completed; returning to scoped cleanup', file=sys.stderr, flush=True)
    return {
        'schema': 1, 'stage': STAGE, 'result': 'passed', 'checked_at': rc.now(),
        'scope': 'native_docker_foreground_watch_with_synthetic_upstreams',
        'image_id': record['image'], 'runtime_source_hashes_verified': context['hashes'],
        'interval_seconds': INTERVAL, 'timer_mode': 'real_interval' if real_interval else 'accelerated_test',
        'wall_clock_interval_verified': real_interval, 'wait_count': len(waits),
        'minimum_wait_observed_seconds': min(waits), 'watch_invocations': invocations,
        'cycles': cycles, 'test_ops_restarts_observed': restarts, 'refresh_journal_count': 2,
        'bounded_cycles_completed': True, 'discovery_failure_retried_without_mutation': True,
        'pending_stopped_before_retry': True, 'rollback_explicit_outside_watch': True,
        'no_duplicate_restart_or_journal': True, 'old_session_rejected': True,
        'same_test_ops_container_image_mounts': True, 'synthetic_credentials_preserved': True,
        'initial_cache_entries_preserved': len(context['initial_cache']['success_times']),
        'bootstrap_record_preserved': True, 'test_watch_started': True, 'test_watch_returned': True,
        'production_watch_started': False, 'production_service_control_requested': False,
        'production_credentials_read': False, 'production_application_api_requests': 0,
        'inference_requested': False, 'image_build_or_pull_requested': False,
        'upstreams': 'synthetic_GET_only_HTTP', 'promql_engine_acceptance': False,
        'fresh_business_platform_installation_acceptance': False,
        'service_timer_or_long_running_acceptance': False, 'kubernetes_acceptance': 'deferred',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'cleanup', 'worker'))
    parser.add_argument('--state-dir', type=Path, default=BASE)
    parser.add_argument('--run-dir', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'run':
            require(args.run_dir is None, 'ACCEPTANCE_RUN_DIR_NOT_ALLOWED')
            report = isolated.run(args.state_dir, scenario='watch')
        else:
            require(args.run_dir is not None, 'ACCEPTANCE_RUN_DIR_REQUIRED')
            directory = manage.safe(args.run_dir)
            record = isolated.validate_run(directory)
            require(record.get('scenario') == 'watch', 'ACCEPTANCE_SCENARIO_INVALID')
            engine = isolated.Engine(record['endpoint'])
            if args.action == 'cleanup':
                with isolated.run_lock(directory.parent):
                    report = {'stage': STAGE, 'result': 'cleaned', 'run_directory': str(directory),
                              'cleanup': engine.cleanup(directory)}
            else:
                try:
                    report = exercise(directory, engine)
                except (ReadFailure, Unavailable, OSError, ValueError, KeyError, TypeError) as exc:
                    report = {'stage': STAGE, 'result': 'attention_required',
                              'code': exc.code if isinstance(exc, ReadFailure) else 'WATCH_WORKER_UNAVAILABLE'}
                isolated.write(directory/'worker-report.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['result'] in ('passed', 'cleaned') else 2
    except (ReadFailure, Unavailable, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'stage': STAGE, 'result': 'attention_required',
                          'code': exc.code if isinstance(exc, ReadFailure) else 'WATCH_PREFLIGHT_UNAVAILABLE'}))
        return 2


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
