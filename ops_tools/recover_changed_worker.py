#!/usr/bin/env python3
"""Explicit review of an OTHER_CONTAINER_CHANGED failed worker, on the same boot.

Plan reads metadata only. Apply is a separate, reviewed one-shot owned worker
start with durable old failure evidence and authenticated configuration readback.
No production service repair, helper adoption/removal, backup or restore.
The exact container that triggered the historical guard is not identified.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops_tools import recover_boot_worker as legacy, maintenance_control as maintenance
from ops_tools import backup_measure as measure
from ops_platform.contracts import ReadFailure

REASON = 'OTHER_CONTAINER_CHANGED'
KIND = legacy.KIND
STAGE = 'O04-reviewed-changed-container-worker-recovery'
PINS = {'ops_tools/recover_boot_worker.py': '3e1661bb299b51181479a473a18b9f45951ea2d1ba6022497274e5809b737566', 'ops_tools/maintenance_control.py': '4361ee6f37be74614bb79829849bdc213693d76c9bcb2bb39668f9315a979ca8', 'ops_tools/backup_measure.py': '8e53c16af0546eb89855f5f9020e4b5a5227edb4d2e8b98b04669aa39c741a23'}
LOADED_SELF = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
RUNNING_PHASES = {'plan_first_inventory', 'plan_second_inventory', 'pre_start_guard',
                  'observation_guard', 'unavailable'}
RUNNING_REASONS = {'list_ids_invalid', 'row_count_differs', 'response_json_invalid',
                   'response_row_invalid', 'duplicate_ids', 'unexpected_ids',
                   'listed_container_not_running', 'metadata_query_failed', 'discovery_changed'}
RUNNING_COUNTS = {'listed_running_count', 'response_rows_count', 'stopped_rows_count',
                  'missing_id_count', 'duplicate_id_count', 'unexpected_id_count', 'invalid_row_count',
                  'added_count', 'removed_count', 'started_at_changed_count'}


def safe_running_diagnostic(value):
    fields = {'schema', 'phase', 'reason', 'counts', 'values_returned', 'retry_requested'}
    if (not isinstance(value, dict) or set(value) != fields
        or type(value['schema']) is not int or value['schema'] != 1
        or not isinstance(value['phase'], str) or value['phase'] not in RUNNING_PHASES
        or not isinstance(value['reason'], str) or value['reason'] not in RUNNING_REASONS
        or value['values_returned'] is not False or value['retry_requested'] is not False
        or not isinstance(value['counts'], dict) or set(value['counts']) != RUNNING_COUNTS
        or any(v is not None and (type(v) is not int or not 0 <= v <= maintenance.LIMIT)
               for v in value['counts'].values())):
        return None
    return deepcopy(value)


def running_failure(code, reason, counts, phase='unavailable'):
    bounded = {name:(counts.get(name) if type(counts.get(name)) is int
                    and 0 <= counts[name] <= maintenance.LIMIT else None) for name in RUNNING_COUNTS}
    exc = ReadFailure(code)
    exc.running_inventory_diagnostic = safe_running_diagnostic(dict(schema=1, phase=phase,
        reason=reason, counts=bounded, values_returned=False, retry_requested=False))
    return exc


def running_snapshot(docker):
    """Same reviewed ps/batch-inspect gates; no retry, filter, or rebinding.

    Classify before refusing, without returning IDs, times or raw metadata.
    Only this recovery instance uses the enriched projection.
    """
    counts = {}
    def query(args):
        try: return docker.query(args)
        except Exception as exc:
            code = getattr(exc, 'code', 'MAINTENANCE_DOCKER_METADATA_FAILED')
            if not isinstance(code, str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,95}', code):
                code = 'MAINTENANCE_DOCKER_METADATA_FAILED'
            error = running_failure(code, 'metadata_query_failed', counts)
            error.docker_diagnostic = maintenance.safe_docker_diagnostic(getattr(exc, 'docker_diagnostic', None))
            raise error from None
    ids = query(['ps', '-q', '--no-trunc']).split()
    counts['listed_running_count'] = len(ids)
    if (len(ids) > maintenance.LIMIT or len(set(ids)) != len(ids)
        or not all(maintenance.HEX.fullmatch(x) for x in ids)):
        raise running_failure('MAINTENANCE_RUNNING_SET_INVALID', 'list_ids_invalid', counts)
    if not ids: return {}
    lines = query(['container', 'inspect', '--format', maintenance.LIVE_FORMAT, *sorted(ids)]).splitlines()
    counts['response_rows_count'] = len(lines)
    # Never process an unbounded or incomplete inspect response as a new set.
    if len(lines) != len(ids):
        raise running_failure('MAINTENANCE_RUNNING_SET_CHANGED', 'row_count_differs', counts)
    result = {}; seen = set(); stopped = duplicate = unexpected = invalid = bad_json = 0
    for line in lines:
        try: row = json.loads(line)
        except (ValueError, TypeError):
            bad_json += 1; invalid += 1; continue
        if (not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str)
            or not maintenance.HEX.fullmatch(row[0]) or type(row[1]) is not bool
            or not isinstance(row[2], str) or not 1 <= len(row[2]) <= 128):
            invalid += 1; continue
        if row[0] in seen: duplicate += 1
        seen.add(row[0])
        if row[0] not in ids: unexpected += 1
        if row[1] is False: stopped += 1
        result[row[0]] = row[2]
    counts.update(stopped_rows_count=stopped, duplicate_id_count=duplicate,
                  unexpected_id_count=unexpected, invalid_row_count=invalid,
                  missing_id_count=len(set(ids)-seen))
    reason = ('response_json_invalid' if bad_json else 'response_row_invalid' if invalid
              else 'duplicate_ids' if duplicate else 'unexpected_ids' if unexpected
              else 'listed_container_not_running' if stopped else None)
    if reason:
        raise running_failure('MAINTENANCE_RUNNING_SET_CHANGED', reason, counts)
    return result


def source_pins(root=ROOT):
    result = {name:hashlib.sha256(maintenance.source(root/name)).hexdigest() for name in PINS}
    maintenance.require(result == PINS, 'CHANGED_RECOVERY_DEPENDENCY_CHANGED')
    own = hashlib.sha256(maintenance.source(root/'ops_tools/recover_changed_worker.py')).hexdigest()
    maintenance.require(own == LOADED_SELF, 'CHANGED_RECOVERY_LOADED_SOURCE_CHANGED')
    result['ops_tools/recover_changed_worker.py'] = own
    return result


class CurrentInventory:
    def __init__(self, directory, langfuse, root=ROOT):
        self.review = maintenance.Review(directory, langfuse, root)
        docker = self.review.docker
        maintenance.require(self.review.inventory.docker is docker, 'CHANGED_RECOVERY_DOCKER_ADAPTER_MISMATCH')
        docker.running = lambda: running_snapshot(docker)

    def capture(self, manifest):
        review = self.review
        # A historical maintenance failure must not be bypassed by worker recovery.
        maintenance.require(all(row['phase'] == 'returned' for row in review.controller.records()),
                            'CHANGED_RECOVERY_MAINTENANCE_UNRESOLVED')
        snapshot = review.inventory.capture()
        binding = manifest['binding']
        ops = snapshot['inventory']['scope']['ops-web']
        maintenance.require(snapshot['host']['daemon_id'] == binding['daemon_id'],
                            'MAINTENANCE_DAEMON_BINDING_CHANGED')
        maintenance.require(ops['container_id'] == binding['ops_container_id']
                            and ops['image_id'] == binding['ops_image_id'], 'MAINTENANCE_OPS_BINDING_CHANGED')
        # All helpers with the reviewed reader label, including exited ones,
        # block recovery. Never inspect/adopt/delete a stored or foreign helper.
        binary = Path(review.reader.paths['docker']).resolve()
        info = binary.stat()
        maintenance.require(binary.is_file() and info.st_nlink == 1 and not info.st_mode & 0o022
                            and os.access(binary, os.X_OK), 'BACKUP_DOCKER_BINARY_UNSAFE')
        identity = dict(path=str(binary), dev=info.st_dev, ino=info.st_ino,
                        size=info.st_size, mtime_ns=info.st_mtime_ns,
                        sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
        backend = measure.MetadataDocker(snapshot['host'], binary)
        backend.verify_host()
        try:
            ids = backend.metadata(['ps', '-aq', '--no-trunc', '--filter', 'label='+measure.s.LABEL]).decode().split()
        except UnicodeError:
            raise maintenance.Failure('CHANGED_RECOVERY_HELPER_METADATA_INVALID') from None
        maintenance.require(len(ids) <= 160 and len(ids) == len(set(ids))
                            and all(measure.s.HEX.fullmatch(x) for x in ids),
                            'CHANGED_RECOVERY_HELPER_METADATA_INVALID')
        maintenance.require(not ids, 'CHANGED_RECOVERY_BACKUP_HELPERS_PRESENT')
        snapshot.update(docker_binary=identity, backup_helpers_absent=True)
        return snapshot


class Recovery(legacy.Recovery):
    def __init__(self, initial, *, snapshot_fn, pins_fn=source_pins, **options):
        super().__init__(initial, **options)
        self.snapshot, self.pins = snapshot_fn, pins_fn
        self.running_inventory_diagnostic = None
        self.docker_diagnostic = None

    def sample(self, manifest, phase):
        self.running_inventory_diagnostic = None
        self.docker_diagnostic = None
        try: return self.snapshot(manifest)
        except Exception as exc:
            value = safe_running_diagnostic(getattr(exc, 'running_inventory_diagnostic', None))
            if value is not None:
                value['phase'] = phase
                self.running_inventory_diagnostic = safe_running_diagnostic(value)
            self.docker_diagnostic = maintenance.safe_docker_diagnostic(getattr(exc, 'docker_diagnostic', None))
            # The original one-shot controller recognizes runtime ReadFailure.
            # Preserve validated adapter codes across that boundary, including
            # late readback/observation failures, without retaining raw text.
            code = getattr(exc, 'code', None)
            if not isinstance(exc, ReadFailure) and isinstance(code, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,95}', code):
                failure = ReadFailure(code)
                failure.running_inventory_diagnostic = self.running_inventory_diagnostic
                failure.docker_diagnostic = maintenance.safe_docker_diagnostic(getattr(exc, 'docker_diagnostic', None))
                raise failure from None
            raise

    def compare_discovery(self, before, after, phase):
        if before == after: return
        counts = {}
        # These are current review samples, not the lost historical baseline.
        prior = before.get('inventory', {}).get('protected')
        current = after.get('inventory', {}).get('protected')
        if isinstance(prior, dict) and isinstance(current, dict):
            counts.update(added_count=len(set(current)-set(prior)), removed_count=len(set(prior)-set(current)),
                          started_at_changed_count=sum(prior[k] != current[k] for k in set(prior)&set(current)))
        exc = running_failure('CHANGED_RECOVERY_DISCOVERY_CHANGED', 'discovery_changed', counts, phase)
        self.running_inventory_diagnostic = exc.running_inventory_diagnostic
        raise exc

    def plan(self, wait_seconds=270, *, held=False):
        self.require(type(wait_seconds) is int and 1 <= wait_seconds <= 1200, 'INVALID_OBSERVATION_LIMIT')
        pins = self.pins()
        transition, manifest, history, initial = self.common()
        control, status, receipt = (self.store.control(), self.store.optional('status.json'),
                                     self.store.optional('boot.json'))
        current = self.service.owned(manifest, self.backend)
        self.require(control['desired'] == 'paused' and control['recovery'] == REASON
                     and control.get('service_stop_token') and not control.get('boot_exit_request'),
                     'BOOT_RECOVERY_REASON_NOT_SUPPORTED')
        self.require(current['ActiveState'] == 'failed' and current['MainPID'] == '0'
                     and current.get('Job', '') in ('', '0') and current.get('Result') == 'exit-code'
                     and current.get('ExecMainStatus') == '2' and self.backend.cgroup_empty(current),
                     'BOOT_FAILED_WORKER_EXIT_REQUIRED')
        self.require(status and status.get('phase') == 'recovery_required'
                     and status.get('outcome', {}).get('code') == REASON
                     and status['outcome'].get('classification') == 'blocked'
                     and status['outcome'].get('child_process_group_stopped') is True
                     and type(status.get('completed_cycles')) is int and status['completed_cycles'] >= 1,
                     'BOOT_FAILURE_EVIDENCE_REQUIRED')
        self.require(receipt and receipt.get('phase') == 'entered'
                     and receipt.get('manifest_sha256') == self.boot.fingerprint(manifest)
                     and receipt.get('generation') == self.boot.policy(manifest)['generation']
                     and receipt.get('invocation') == status.get('invocation')
                     and receipt.get('identity') == status.get('identity')
                     and receipt.get('service_invocation') == status.get('service_invocation') == current['InvocationID']
                     and receipt.get('identity', {}).get('boot_id') == self.identity()['boot_id'],
                     'BOOT_FAILURE_RECEIPT_REQUIRED')
        if not held: legacy.readonly_lease(self.directory, self.manage, self.require)
        identifier = self.boot.fingerprint([current['InvocationID'], status['invocation']])
        path = self.journal_path(identifier)
        self.require(not path.exists() and not path.is_symlink(), 'BOOT_RECOVERY_ALREADY_RECORDED_USE_CHECK')
        if self.folder.exists() or self.folder.is_symlink():
            self.manage.private(self.folder)
            rows = list(self.folder.iterdir())
            self.require(len(rows) <= 64, 'BOOT_RECOVERY_HISTORY_LIMIT')
            for prior in rows:
                self.require(prior.is_file() and re.fullmatch('[a-f0-9]{64}\\.json', prior.name),
                             'BOOT_RECOVERY_HISTORY_INVALID')
                old = self.read_record(prior)
                self.require(old.get('kind') == KIND and old.get('directory') == str(self.directory)
                             and old.get('id') == prior.stem and old.get('phase') == 'accepted',
                             'BOOT_RECOVERY_UNFINISHED')
        proposal = dict(schema=1, kind=KIND, id=identifier, directory=str(self.directory),
                        manifest=manifest, transition_sha256=self.boot.fingerprint(transition),
                        initial_history_sha256=self.boot.fingerprint(initial), history=history,
                        control=control, failure_status=status, failure_receipt=receipt,
                        prior_service_invocation=current['InvocationID'], boot_id=self.identity()['boot_id'],
                        wait_seconds=wait_seconds,
                        adapter_sha256=hashlib.sha256(Path(legacy.__file__).read_bytes()).hexdigest())
        # A final sample prevents a mixed plan being published after a stop.
        self.require(self.store.control() == control and self.store.optional('status.json') == status
                     and self.store.optional('boot.json') == receipt, 'BOOT_RECOVERY_STATE_CHANGED')
        before = self.sample(manifest, 'plan_first_inventory')
        after = self.sample(manifest, 'plan_second_inventory')
        self.compare_discovery(before, after, 'plan_second_inventory')
        self.require(self.pins() == pins, 'CHANGED_RECOVERY_SOURCE_CHANGED')
        self.require(self.store.control() == control and self.store.optional('status.json') == status
                     and self.store.optional('boot.json') == receipt, 'BOOT_RECOVERY_STATE_CHANGED')
        proposal.update(recovery_reason=REASON, current_discovery=before, source_pins=pins,
                        historical_changed_container_identified=False)
        return proposal

    def guard(self, record, *, observing=False):
        manifest = super().guard(record, observing=observing)
        self.require(record.get('recovery_reason') == REASON, 'CHANGED_RECOVERY_RECORD_REASON_INVALID')
        self.require(self.pins() == record['source_pins'], 'CHANGED_RECOVERY_SOURCE_CHANGED')
        phase = 'observation_guard' if observing else 'pre_start_guard'
        self.compare_discovery(record['current_discovery'], self.sample(manifest, phase), phase)
        self.require(self.pins() == record['source_pins'], 'CHANGED_RECOVERY_SOURCE_CHANGED')
        self.require(self.store.control().get('service_stop_token') == record['control']['service_stop_token'],
                     'SERVICE_STOP_REQUEST_CHANGED')
        return manifest

    def observe_record(self, record):
        result = super().observe_record(record)
        result['next_step'] = 'reviewed_maintenance_size_survey_required'
        return result

    def check(self, identifier):
        record = self.read_record(self.journal_path(identifier))
        self.require(record.get('recovery_reason') == REASON and 'current_discovery' in record
                     and 'source_pins' in record, 'CHANGED_RECOVERY_CHECK_REFUSED')
        return super().check(identifier)


def controller(directory, langfuse=None):
    source_pins()
    old = legacy.controller(directory)
    probe = CurrentInventory(directory, langfuse or Path.home()/'.local/state/agent-project-langfuse')
    return Recovery(old.initial, snapshot_fn=probe.capture)


def run(action, directory, expect=None, identifier=None, wait_seconds=270, factory=controller):
    report = dict(schema=1, stage=STAGE, action=action, result='attention_required',
        checked_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), reason=REASON,
        scope='existing_owned_failed_boot_worker_same_boot_current_inventory_review',
        configuration_written=False, business_service_control_requested=False,
        code_or_policy_written=False, linger_changed=False, image_build_or_pull_requested=False,
        inference_requested=False, actual_reboot_acceptance='deferred_by_user', backup_executed=False,
        restore_executed=False, identity_contents_read=False, backup_ready=False,
        historical_changed_container_identified=False,
        live_configuration_readback_requested=action == 'apply')
    if action in ('plan', 'check'):
        report.update(application_credentials_read=False, application_api_requests=0, files_written=[])
    recovery = None
    try:
        require = maintenance.require
        require(action in ('plan', 'apply', 'check'), 'INVALID_BOOT_RECOVERY_ACTION')
        require((action == 'apply' and isinstance(expect, str) and re.fullmatch('[a-f0-9]{64}', expect)
                 or action != 'apply' and expect is None), 'BOOT_RECOVERY_REVIEW_REQUIRED')
        require((action == 'check' and isinstance(identifier, str) and re.fullmatch('[a-f0-9]{64}', identifier)
                 or action != 'check' and identifier is None), 'BOOT_RECOVERY_ID_INVALID')
        recovery = factory(Path(os.path.abspath(Path(directory).expanduser())))
        if action == 'plan':
            proposal = recovery.plan(wait_seconds)
            discovery = proposal['current_discovery']
            report.update(result='passed', review_id=recovery.review(proposal), recovery_id=proposal['id'],
                persistence='boot', autostart_enabled=True,
                failure_checked_at=proposal['failure_status']['checked_at'],
                prior_completed_cycles=proposal['failure_status']['completed_cycles'], wait_seconds=wait_seconds,
                scoped_container_count=len(discovery['inventory']['scope']),
                protected_running_count=len(discovery['inventory']['protected']),
                current_inventory_sha256=recovery.boot.fingerprint(discovery), current_inventory_stable=True,
                backup_helpers_absent=True, original_failure_retained=True, review_authorizes_execution=False,
                next_step='review_result_before_separate_explicit_one_shot_apply')
        elif action == 'apply': report.update(recovery.apply(expect, wait_seconds))
        else: report.update(recovery.check(identifier))
    except Exception as exc:
        code = getattr(exc, 'code', None)
        report.update(result='attention_required', code=code if isinstance(code, str)
                      and re.fullmatch('[A-Z][A-Z0-9_]{0,95}', code) else 'CHANGED_RECOVERY_LOCAL_ERROR',
                      next_step='retain_failure_and_recovery_audits_and_report')
        diagnostic = safe_running_diagnostic(getattr(exc, 'running_inventory_diagnostic', None))
        if diagnostic is not None: report['running_inventory_diagnostic'] = diagnostic
        query = maintenance.safe_docker_diagnostic(getattr(exc, 'docker_diagnostic', None))
        if query is not None: report['docker_diagnostic'] = query
    diagnostic = safe_running_diagnostic(getattr(recovery, 'running_inventory_diagnostic', None))
    if diagnostic is not None: report['running_inventory_diagnostic'] = diagnostic
    query = maintenance.safe_docker_diagnostic(getattr(recovery, 'docker_diagnostic', None))
    if query is not None: report['docker_diagnostic'] = query
    report['worker_service_control_commands'] = getattr(getattr(recovery, 'backend', None), 'control_commands', 0)
    report['live_configuration_readback_attempted'] = getattr(recovery, 'readback_attempted', False)
    report['live_configuration_readback_verified'] = getattr(recovery, 'readback_verified', False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply', 'check'))
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/agent-project-ops/server')
    parser.add_argument('--langfuse-state-dir', type=Path, default=Path.home()/'.local/state/agent-project-langfuse')
    parser.add_argument('--expect-plan'); parser.add_argument('--recovery-id')
    parser.add_argument('--wait-seconds', type=int, default=270)
    args = parser.parse_args(argv)
    value = run(args.action, args.state_dir, args.expect_plan, args.recovery_id, args.wait_seconds,
                factory=lambda directory: controller(directory, args.langfuse_state_dir))
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
