#!/usr/bin/env python3
"""Bounded read-only sampling of an existing Ops refresh service; never start it."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops_platform.contracts import require
from ops_platform.ops import auto_refresh, manage, lifecycle_probe as probe, lifecycle_state as local
from ops_platform.ops import service_lifecycle as service, refresh_worker as worker
from ops_platform.ops.service_systemd import Systemd, command_details
from ops_platform.ops.service_start import projection

STAGE = 'O04-production-auto-refresh-observation'


class ReadOnlySystemd(Systemd):
    def call(self, args, control=False, accept=()):
        require(not control, 'OBSERVER_SERVICE_CONTROL_REFUSED')
        return super().call(args, control=False, accept=accept)


class Source:
    def __init__(self, directory):
        self.directory = directory
        self.reader = probe.Reader()
        self.report = auto_refresh.run('status', directory, reader=self.reader)
        self.backend = None

    def connect(self):
        self.backend = ReadOnlySystemd(self.reader, self.report['systemd'], self.report['management']['unit_name'])

    def manifest(self): return service.load(self.directory)
    def sample(self): return service.status(self.directory, self.backend)

    def verify_binding(self, manifest):
        # Metadata only; no lease, prepared-state lock, credential contents or
        # application readback. The active worker owns its own transactions.
        require(worker.runtime_snapshot(self.directory) == manifest['binding'], 'SERVICE_BINDING_CHANGED')


def frame(value):
    provider = value.get('provider_status') or {}
    require(provider.get('ActiveState') == 'active', 'SERVICE_NOT_RUNNING')
    require(not value.get('recovery'), 'WORKER_RECOVERY_REQUIRED')
    require(value.get('desired_state') == 'running', 'WORKER_PAUSED')
    require(value.get('current_invocation_verified') is True, 'WORKER_LIVE_OBSERVATION_UNVERIFIED')
    saved = value['worker']['last_observation']
    require(saved.get('phase') in ('waiting', 'evaluating'), 'WORKER_OBSERVATION_PHASE_INVALID')
    require(saved.get('service_invocation') == provider.get('InvocationID') and bool(provider.get('InvocationID')),
            'WORKER_SERVICE_INVOCATION_MISMATCH')
    count = saved.get('completed_cycles')
    require(type(count) is int and count >= 0, 'WORKER_CYCLE_COUNT_INVALID')
    identity = saved.get('identity') or {}
    key = (provider['InvocationID'], provider.get('MainPID'), saved.get('invocation'),
           identity.get('boot_id'), identity.get('start_ticks'))
    require(all(item is not None and str(item) for item in key), 'WORKER_IDENTITY_INCOMPLETE')
    if saved['phase'] == 'waiting':
        intent = value['worker'].get('intent')
        require(not intent or intent.get('phase') == 'completed', 'WORKER_UNFINISHED_INTENT')
        outcome = saved.get('outcome') or {}
        category = outcome.get('classification')
        require(category in ('accepted', 'retryable_prewrite') and outcome.get('child_process_group_stopped') is True,
                'WORKER_EVALUATION_EVIDENCE_INVALID')
        if category == 'accepted':
            require(outcome.get('result') == 'passed' and value.get('evaluation_status') == 'healthy'
                    and type(outcome.get('configuration_changed')) is bool
                    and type(outcome.get('restart_requested')) is bool, 'WORKER_EVALUATION_EVIDENCE_INVALID')
        else:
            require(outcome.get('result') == 'attention_required' and value.get('evaluation_status') == 'degraded_retrying',
                    'WORKER_EVALUATION_EVIDENCE_INVALID')
    return key, count, saved


def run(directory, duration=None, minimum_cycles=3, poll_seconds=5, source_factory=Source,
        _clock=time.monotonic, _sleep=time.sleep, progress=lambda _: None):
    result = {'schema': 1, 'stage': STAGE, 'result': 'attention_required',
              'scope': 'bounded_read_only_existing_service_samples', 'checked_at': worker.activation.rc.now(),
              'effects': {'service_control_requested': False, 'files_written': [], 'application_api_requests': 0,
                         'application_credential_contents_read': False, 'configuration_written': False, 'inference_requested': False},
              'worker_continues_independently': True, 'observer_requested_stop': False,
              'logout_or_boot_acceptance': False, 'long_running_acceptance': False,
              'kubernetes_acceptance': 'deferred', 'historical_success_reused': False,
              'sample_count': 0, 'maximum_sample_gap_seconds': 0.0,
              'new_completed_cycles': 0, 'accepted_cycles_observed': 0, 'degraded_cycles_observed': 0,
              'missing_outcome_cycles': 0, 'evaluations': [], 'warnings': [],
              'worker_running_at_last_observation': None,
              'continuous_between_sample_uptime_verified': False,
              'next_step': 'inspect_observation_result'}
    started = None
    try:
        require(duration is None or type(duration) is int and 1 <= duration <= 14400, 'INVALID_OBSERVATION_DURATION')
        require(type(minimum_cycles) is int and 1 <= minimum_cycles <= 100, 'INVALID_OBSERVATION_CYCLES')
        require(type(poll_seconds) is int and 1 <= poll_seconds <= 30, 'INVALID_OBSERVATION_POLL')
        directory = Path(os.path.abspath(Path(directory).expanduser()))
        source = source_factory(directory)
        report = source.report
        result['preflight'] = {k: report.get(k) for k in ('result', 'blockers', 'warnings', 'prepared_state_stable')}
        require(report.get('environment_ready') is True and not report.get('blockers'), 'SERVICE_PREFLIGHT_BLOCKED')
        require(report['management']['status'] == 'owned', 'SERVICE_NOT_INSTALLED')
        source.connect()
        manifest = source.manifest()
        require(manifest['phase'] == 'installed', 'SERVICE_NOT_INSTALLED')
        source.verify_binding(manifest)
        interval, deadline = manifest['interval_seconds'], manifest['deadline_seconds']
        budget = duration if duration is not None else max(600, minimum_cycles * interval + deadline + 30)
        require(budget <= 14400, 'OBSERVATION_DURATION_REQUIRES_EXPLICIT_LIMIT')
        result.update(requested_duration_seconds=budget, required_new_accepted_cycles=minimum_cycles,
                      poll_seconds=poll_seconds, interval_seconds=interval, persistence=manifest['persistence'],
                      python_executable=manifest['binding']['python'], binding_verified_at_start=True,
                      final_evaluation_grace_seconds=deadline + 20)
        initial_key = None; initial_count = None; previous_count = None; last_outcome_count = None
        previous_sample = None; next_progress = 0; last_waiting = None
        started = _clock()
        progress('watching existing service; duration=' + str(budget) + 's, minimum_new_cycles=' + str(minimum_cycles))
        while True:
            require(source.manifest() == manifest, 'SERVICE_MANIFEST_CHANGED_DURING_OBSERVATION')
            current = source.sample()
            now = _clock(); elapsed = now - started
            result['observed_seconds'] = round(elapsed, 3)
            result['sample_count'] += 1
            if previous_sample is not None:
                result['maximum_sample_gap_seconds'] = round(max(result['maximum_sample_gap_seconds'], now - previous_sample), 3)
            previous_sample = now
            result['last_management'] = projection(current)
            result['worker_running_at_last_observation'] = current.get('provider_status', {}).get('ActiveState') == 'active'
            key, count, saved = frame(current)
            if initial_key is None:
                # Measure the configured window from the first valid sample,
                # excluding initial metadata command latency.
                started = now; elapsed = 0.0; result['observed_seconds'] = 0.0
                initial_key = key; initial_count = count; previous_count = count; last_outcome_count = count
                result.update(unit_name=current['unit_name'], service_invocation=key[0],
                              initial_completed_cycles=count, initial_management=projection(current))
                if saved['phase'] == 'waiting' and saved['outcome']['classification'] == 'retryable_prewrite':
                    result['warnings'].append('INITIAL_EVALUATION_DEGRADED')
            require(key == initial_key, 'SERVICE_PROCESS_CHANGED_DURING_OBSERVATION')
            require(count >= previous_count, 'WORKER_CYCLE_COUNT_REGRESSED')
            previous_count = count
            result['final_completed_cycles'] = count
            result['new_completed_cycles'] = count - initial_count
            if saved['phase'] == 'waiting':
                outcome = saved['outcome']
                if count == last_outcome_count and last_waiting is not None:
                    require(outcome == last_waiting, 'WORKER_SAME_CYCLE_OUTCOME_CHANGED')
                if count > last_outcome_count:
                    result['missing_outcome_cycles'] += count - last_outcome_count - 1
                    last_outcome_count = count
                    row = {k: outcome[k] for k in ('result', 'classification', 'code', 'generation',
                        'configuration_changed', 'restart_requested') if k in outcome}
                    row.update(completed_cycle=count, checked_at=saved.get('checked_at'), elapsed_seconds=round(elapsed, 3))
                    require(len(result['evaluations']) < 1024, 'OBSERVATION_EVALUATION_LIMIT')
                    result['evaluations'].append(row)
                    counter = 'accepted_cycles_observed' if outcome['classification'] == 'accepted' else 'degraded_cycles_observed'
                    result[counter] += 1
                    progress('cycle=' + str(count) + ', classification=' + outcome['classification'])
                last_waiting = outcome
            if elapsed >= next_progress:
                progress('elapsed=' + str(int(elapsed)) + 's, completed=' + str(count) + ', phase=' + saved['phase'])
                next_progress = elapsed + 30
            # An evaluation active at the end may finish within its existing
            # deadline. This is bounded observation only, not another trigger.
            if elapsed >= budget and saved['phase'] == 'waiting': break
            require(elapsed < budget + deadline + 20, 'OBSERVATION_FINAL_EVALUATION_TIMEOUT')
            _sleep(min(poll_seconds, max(.01, budget + deadline + 20 - elapsed)))
        source.verify_binding(manifest)
        require(source.manifest() == manifest, 'SERVICE_MANIFEST_CHANGED_DURING_OBSERVATION')
        result.update(binding_verified_at_end=True, same_service_process_at_samples=True,
                      configured_window_completed=True,
                      recovered_by_end=result['last_management']['evaluation_status'] == 'healthy')
        require(result['missing_outcome_cycles'] == 0, 'OBSERVATION_MISSED_CYCLE_OUTCOMES')
        require(result['degraded_cycles_observed'] == 0 and not result['warnings'], 'OBSERVATION_DEGRADED_EVALUATION')
        require(result['accepted_cycles_observed'] >= minimum_cycles, 'OBSERVATION_INSUFFICIENT_NEW_EVALUATIONS')
        result.update(result='passed', observed_evaluations_all_accepted=True, worker_left_running=True,
                      next_step='review_persistence_policy_and_longer_observation_scope')
    except local.ERRORS as exc:
        result['code'] = local.error_code(exc)
        detail = command_details(getattr(exc, 'service_command', None))
        if detail is not None: result['service_command'] = detail
    except KeyboardInterrupt:
        result['code'] = 'OBSERVATION_INTERRUPTED'
    if started is not None: result['total_observer_seconds'] = round(_clock() - started, 3)
    result['finished_at'] = worker.activation.rc.now()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=manage.STATE)
    parser.add_argument('--duration', type=int, help='seconds; default derived from saved interval/deadline (normally 600)')
    parser.add_argument('--minimum-cycles', type=int, default=3)
    parser.add_argument('--poll-seconds', type=int, default=5)
    args = parser.parse_args(argv)
    result = run(args.state_dir, args.duration, args.minimum_cycles, args.poll_seconds,
                 progress=lambda text: print('[ops-observe] ' + text, file=sys.stderr, flush=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
