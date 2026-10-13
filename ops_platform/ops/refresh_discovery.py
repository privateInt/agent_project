#!/usr/bin/env python3
"""Rediscover prepared Compose installations and refresh provenance-owned settings."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infra.observability.ops.backup_preflight import Unavailable
from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import activate_discovery as activation, manage, restart_check as rc, set_public_url as change
from ops_platform.ops.activation_plan import validate_effective
from ops_platform.ops.refresh_plan import merge, FIELDS

STAGE = 'O04-discovery-refresh'
MAX_GENERATIONS = 256
RETRYABLE = {'OPS_ACTIVATION_NOT_READY', 'MOUNTED_CONFIG_NOT_ACTIVATED',
             'ROLLBACK_NOT_READY', 'ROLLBACK_MOUNT_NOT_READY', 'CONNECTION_FAILED', 'TIMEOUT'}


def lineage(directory):
    """Read a bounded, gap-free chain; accepted ancestors are never rewritten."""
    from ops_platform.ops import bootstrap_discovery as bootstrap
    anchor = directory / 'discovery-activation/baseline.json'
    initial = directory / bootstrap.RELATIVE
    require(not ((anchor.exists() or anchor.is_symlink()) and (initial.exists() or initial.is_symlink())),
            'MULTIPLE_DISCOVERY_ANCHORS')
    if initial.exists() or initial.is_symlink():
        original = bootstrap.load_anchor(directory)
        evidence = {bootstrap.RELATIVE: rc.read_file(initial, 512*1024, private=True)[1]}
        fields, source = deepcopy(original['baseline']['fields']), original['baseline']['source_sha256']
    else:
        original = activation.load_record(anchor, directory)
        require(original.get('phase') == 'accepted' and original.get('report', {}).get('result') == 'passed',
                'ACCEPTED_ACTIVATION_REQUIRED')
        evidence = {'discovery-activation/baseline.json': rc.read_file(anchor, 512*1024, private=True)[1]}
        fields = deepcopy(original['baseline']['plan']['fields'])
        source = original['baseline']['source_sha256']
    require(set(fields) == FIELDS, 'PROVENANCE_FIELDS_INVALID')
    folder = directory / 'discovery-refresh'
    records, pending, orphan, rejected = [], None, None, None
    if folder.exists() or folder.is_symlink():
        manage.private(folder)
        children = sorted(folder.iterdir())
        require(len(children) <= MAX_GENERATIONS, 'REFRESH_HISTORY_LIMIT')
        for number, child in enumerate(children, 1):
            require(child.name == f'{number:06d}' and child.is_dir(), 'REFRESH_HISTORY_INVALID')
            manage.private(child)
            path = child / 'baseline.json'
            if not path.exists() and not path.is_symlink():
                require(number == len(children) and not list(child.iterdir()), 'REFRESH_HISTORY_INVALID')
                orphan = number; break
            record = activation.load_record(path, directory)
            b = record['baseline']
            require(b.get('refresh_generation') == number and b.get('additional_evidence') == evidence,
                    'REFRESH_LINEAGE_CHANGED')
            require(record.get('phase') in ('prepared', 'restart_requested', 'accepted', 'rolling_back', 'rolled_back'),
                    'REFRESH_HISTORY_INVALID')
            records.append((path, record))
            if record['phase'] == 'accepted':
                require(record.get('report', {}).get('result') == 'passed', 'REFRESH_HISTORY_INVALID')
                fields, source, rejected = deepcopy(b['plan']['fields']), b['source_sha256'], None
            elif record['phase'] == 'rolled_back':
                fields, source = deepcopy(b['plan']['rollback_fields']), b['source_sha256']
                rejected = {'source': source, 'desired': change.digest(b['desired']['host-config.json'])}
            else:
                require(number == len(children), 'REFRESH_UNFINISHED_ANCESTOR')
                pending = (path, record)
            evidence[str(path.relative_to(directory))] = rc.read_file(path, 512*1024, private=True)[1]
    return {'fields': fields, 'source': source, 'evidence': evidence, 'records': records, 'pending': pending,
            'next': orphan or len(records) + 1, 'rejected': rejected}


def save(path, record):
    require(len(change.body(record).encode()) < 512*1024, 'REFRESH_RECORD_LIMIT')
    rc.write_record(path, record)


def summary(report, record, generation, journal_written):
    report = deepcopy(report); plan = record['baseline']['plan']
    report.pop('existing_values_preserved', None)
    report.update(stage=STAGE, scope='provenance_managed_compose_refresh', generation=generation,
                  configuration_changed=bool(plan['changed_fields']), unmanaged_values_preserved=True,
                  manual_overrides_detected=plan['manual_overrides_detected'], managed_fields=plan['managed_fields'],
                  preserved_overrides_differing_from_discovery=plan['preserved_overrides_differing_from_discovery'],
                  deployment_metadata_changed=plan.get('deployment_metadata_changed', False),
                  provenance_journal_written=journal_written, continuous_refresh_supported=True,
                  watch_enabled=False)
    return report


def finish(action, directory, path, record, docker, source_fn, validate_fn, api_factory, readback_fn, wait_seconds):
    b = record['baseline']
    if action != 'rollback':
        require(not record.get('rollback'), 'ROLLBACK_IN_PROGRESS_USE_ROLLBACK')
        if record.get('phase') == 'accepted':
            activation.guard(directory, b)
            current = change.current(docker, directory, manage.existing(directory), b['owner'], b['container']['identity']['id'])
            require(current == record['accepted_container'], 'ACCEPTED_OPS_CONTAINER_CHANGED')
            return dict(record['report'], evidence_reused=True, live_checks_performed=False)
        if action == 'apply': activation.install(directory, record, path, docker, source_fn, validate_fn)
    elif record.get('phase') == 'rolled_back':
        activation.guard(directory, b, 'original')
        current = change.current(docker, directory, manage.existing(directory), b['owner'], b['container']['identity']['id'])
        require(current == record['rollback_container'], 'ROLLED_BACK_OPS_CONTAINER_CHANGED')
        return dict(record['rollback_report'], stage=STAGE, generation=b['refresh_generation'],
                    evidence_reused=True, live_checks_performed=False)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            if action == 'rollback':
                result = activation.rollback(directory, record, path, docker, api_factory)
                return dict(result, stage=STAGE, generation=b['refresh_generation'], evidence_reused=False)
            report = activation.verify(directory, record, docker, api_factory, readback_fn, source_fn)
            break
        except ReadFailure as exc:
            if exc.code not in RETRYABLE or time.monotonic() >= deadline: raise
            time.sleep(.5)
    report = summary(report, record, b['refresh_generation'], True)
    current = change.current(docker, directory, manage.existing(directory), b['owner'], b['container']['identity']['id'])
    require(current['started'] == report['started_after'], 'OPS_CHANGED_AFTER_VERIFY')
    record.update(phase='accepted', report=report, accepted_container=current); save(path, record)
    return dict(report, evidence_reused=False)


def run(action, directory, docker=None, discover_fn=activation.discover_now, validate_fn=validate_effective,
        source_fn=activation.source_snapshot, api_factory=rc.API, readback_fn=activation.readback,
        wait_seconds=60, retry_rolled_back=False, automatic_guard=None):
    require(action in ('plan', 'apply', 'check', 'rollback'), 'INVALID_REFRESH_ACTION')
    require(automatic_guard is None or (action == 'apply' and not retry_rolled_back), 'AUTOMATIC_APPLY_ONLY')
    directory = manage.safe(directory)
    with change.change_lock(directory):
        context = lineage(directory)
        # A manual apply may finish a recorded operation. Automation must never
        # enter that branch, including when a pending record appeared after startup.
        if automatic_guard is not None:
            require(not context['pending'], 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
            automatic_guard.enter(context)
        docker = docker or activation.Docker(project=manage.prepared_project(directory))
        if context['pending']:
            require(action != 'plan', 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
            path, record = context['pending']
            return finish(action, directory, path, record, docker, source_fn, validate_fn, api_factory, readback_fn, wait_seconds)
        if action in ('check', 'rollback'):
            require(context['records'], 'NO_REFRESH_RECORD')
            path, record = context['records'][-1]
            if record['phase'] == 'rolled_back': action = 'rollback'
            return finish(action, directory, path, record, docker, source_fn, validate_fn, api_factory, readback_fn, wait_seconds)
        record = activation.capture(directory, docker, discover_fn, validate_fn, source_fn, api_factory,
                    planner=lambda cfg, discovered: merge(cfg, discovered, context['fields']),
                    additional_evidence=context['evidence'])
        b = record['baseline']; plan = b['plan']
        plan['deployment_metadata_changed'] = b['source_sha256'] != context['source']
        rejected = context['rejected']
        same_rejected = bool(rejected and plan['changed_fields'] and rejected['source'] == b['source_sha256']
                             and rejected['desired'] == change.digest(b['desired']['host-config.json']))
        needs_record = bool(plan['changed_fields'] or plan['manual_overrides_detected'] or plan['deployment_metadata_changed'])
        record['baseline_sha256'] = change.digest(b)
        if action == 'plan':
            return {'stage': STAGE, 'result': 'planned', 'plan': plan, 'provenance_journal_required': needs_record,
                    'rolled_back_proposal_blocked': same_rejected, 'configuration_written': False,
                    'restart_requested': False, 'inference_requested': False}
        require(not same_rejected or retry_rolled_back, 'ROLLED_BACK_PROPOSAL_REQUIRES_REVIEW')
        if not needs_record:
            if automatic_guard is not None: automatic_guard.checkpoint()
            report = activation.verify(directory, record, docker, api_factory, readback_fn, source_fn)
            return dict(summary(report, record, None, False), evidence_reused=False)
        require(context['next'] <= MAX_GENERATIONS, 'REFRESH_HISTORY_LIMIT')
        b['refresh_generation'] = context['next']; record['baseline_sha256'] = change.digest(b)
        if automatic_guard is not None:
            # Durable controller intent precedes even the new provenance folder.
            automatic_guard.before_write(record)
        folder = directory / 'discovery-refresh'
        folder.mkdir(mode=0o700, exist_ok=True); manage.private(folder)
        folder = folder / f"{context['next']:06d}"
        folder.mkdir(mode=0o700, exist_ok=True); manage.private(folder)
        path = folder / 'baseline.json'; require(not path.exists() and not path.is_symlink(), 'REFRESH_RECORD_EXISTS')
        save(path, record)
        report = finish('apply', directory, path, record, docker, source_fn, validate_fn, api_factory, readback_fn, wait_seconds)
        if automatic_guard is not None: automatic_guard.complete(report)
        return report


def error_result(exc):
    code = exc.code if isinstance(exc, ReadFailure) else str(exc) if isinstance(exc, Unavailable) else 'LOCAL_CONFIG_OR_COMMAND_ERROR'
    return {'stage': STAGE, 'result': 'attention_required', 'code': code,
            'guidance': 'Keep activation and refresh records. Use check for a pending operation, rollback for recorded recovery; do not repeat restart manually.'}


def watch(directory, interval=60, cycles=None, run_fn=run, emit=None, sleep=time.sleep):
    """Opt-in foreground worker; no daemon installation or implicit rollback."""
    from ops_platform.ops.worker_state import automatic_lease, existing_worker_gate
    with automatic_lease(directory):
        existing_worker_gate(directory)
        return _watch(directory, interval, cycles, run_fn, emit, sleep)


def _watch(directory, interval, cycles, run_fn, emit, sleep):
    require(30 <= interval <= 3600 and (cycles is None or 1 <= cycles <= 100000), 'INVALID_WATCH_INTERVAL')
    emit = emit or (lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
    completed = 0
    class LegacyGate:
        def checkpoint(self):
            from ops_platform.ops.worker_state import existing_worker_gate
            existing_worker_gate(directory)
        def enter(self, context): self.checkpoint()
        def before_write(self, record): self.checkpoint()
        def complete(self, report): pass
    while cycles is None or completed < cycles:
        from ops_platform.ops.worker_state import existing_worker_gate
        existing_worker_gate(directory)
        failed = False
        try:
            result = run_fn('apply', directory, **({'automatic_guard': LegacyGate()} if run_fn is run else {}))
        except (ReadFailure, Unavailable, OSError, ValueError, KeyError, TypeError) as exc:
            result = error_result(exc); failed = True
        if failed:
            # A rejected discovery can recover on the next cycle; interrupted writes
            # or uncertain restarts require explicit recovery instead of a restart loop.
            try: pending = bool(lineage(manage.safe(directory))['pending'])
            except (ReadFailure, Unavailable, OSError, ValueError, KeyError, TypeError): pending = True
            result['watch_stopped_for_recovery'] = pending
        else: pending = False
        emit(dict(result, watch_enabled=True))
        if pending: return 2
        completed += 1
        if cycles is None or completed < cycles: sleep(interval)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply', 'check', 'rollback', 'watch'))
    parser.add_argument('--state-dir', type=Path, default=manage.STATE)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--cycles', type=int)
    parser.add_argument('--retry-rolled-back', action='store_true', help='explicitly retry the latest rolled-back proposal after review')
    args = parser.parse_args()
    try:
        require(not args.retry_rolled_back or args.action == 'apply', 'RETRY_REQUIRES_EXPLICIT_APPLY')
        if args.action == 'watch': return watch(args.state_dir, args.interval, args.cycles)
        result = run(args.action, args.state_dir, retry_rolled_back=args.retry_rolled_back)
    except KeyboardInterrupt: return 130
    except (ReadFailure, Unavailable, OSError, ValueError, KeyError, TypeError) as exc: result = error_result(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] in ('planned', 'passed', 'rolled_back') else 2


if __name__ == '__main__': raise SystemExit(main())
