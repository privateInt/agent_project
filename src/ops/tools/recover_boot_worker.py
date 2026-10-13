#!/usr/bin/env python3
"""Reviewed, one-shot recovery of a failed boot worker's Docker metadata gate.

The read-only plan binds the exact failed invocation. Apply checks the live
configuration under both existing leases, retains the failure evidence, arms
the existing boot receipt protocol and submits one owned worker start. It
does not repair Docker, change code/policy, or restart business services.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
BOOT_CONTROL_SHA = '9943a3c35dd0d30782506f29f750ac7331def01a76909f6ade1dd9f6c0c93cab'
REASON = 'WORKER_DOCKER_NOT_READY'
KIND = 'reviewed-boot-worker-recovery'
STAGE = 'O04-production-boot-worker-recovery'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def readonly_lease(directory, manage, require):
    """Probe the existing inode without creating, truncating or replacing it."""
    path = manage.safe(directory / 'automatic-refresh.lock')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        require(stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid()
                and s.st_nlink == 1 and not s.st_mode & 0o077, 'PRIVATE_STATE_REQUIRED')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: require(False, 'AUTOMATIC_WORKER_BUSY')
    finally: os.close(fd)


class Recovery:
    def __init__(self, initial, *, identity_fn=None, readback_fn=None,
                 clock=time.monotonic, sleep=time.sleep, hook=lambda phase: None):
        self.initial = initial
        self.tx, self.store, self.backend = initial.transition, initial.store, initial.backend
        self.directory = initial.directory
        self.identity = identity_fn or initial.identity
        self.readback = readback_fn or initial.readback
        self.clock, self.sleep, self.hook = clock, sleep, hook
        # Runtime imports happen only after the controller's source review.
        from src.ops.platform.contracts import require
        from src.ops.platform.ops import boot_lifecycle, worker_state, service_lifecycle, manage
        from src.ops.platform.ops import service_start, set_public_url, lifecycle_state
        self.require, self.boot, self.ws, self.service = require, boot_lifecycle, worker_state, service_lifecycle
        self.manage, self.observe, self.change, self.local = manage, service_start, set_public_url, lifecycle_state
        self.folder = self.store.path.parent / 'boot-worker-recoveries'
        self.expected = None
        self.readback_attempted = False
        self.readback_verified = False

    def common(self):
        transition, manifest = self.initial.committed()
        self.require(manifest['phase'] == 'installed', 'SERVICE_RECORD_INVALID')
        self.initial.environment(self.directory, manifest)
        self.require(self.service.enabled(manifest), 'BOOT_OWNED_ENABLE_REQUIRED')
        self.require(self.tx.snapshot(self.directory) == manifest['binding'], 'SERVICE_BINDING_CHANGED')
        self.require(self.store.manifest()['binding'] == manifest['binding'], 'SERVICE_BINDING_CHANGED')
        self.require(self.store.optional('intent.json') is None, 'WORKER_UNFINISHED_INTENT')
        history = self.tx.history()
        initial = self.ws.read(self.initial.path)
        self.require(initial.get('phase') == 'accepted' and initial.get('manifest') == manifest
                     and initial.get('transition_sha256') == self.boot.fingerprint(transition),
                     'BOOT_INITIAL_HISTORY_REQUIRED')
        return transition, manifest, history, initial

    def journal_path(self, identifier):
        self.require(isinstance(identifier, str) and re.fullmatch('[a-f0-9]{64}', identifier),
                     'BOOT_RECOVERY_ID_INVALID')
        return self.manage.safe(self.folder / (identifier + '.json'))

    def read_record(self, path):
        info = self.manage.safe(path).lstat()
        self.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'BOOT_RECOVERY_RECORD_UNSAFE')
        return self.ws.read(path)

    def plan(self, wait_seconds=270, *, held=False):
        self.require(type(wait_seconds) is int and 1 <= wait_seconds <= 1200, 'INVALID_OBSERVATION_LIMIT')
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
        if not held: readonly_lease(self.directory, self.manage, self.require)
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
                        adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        # A final sample prevents a mixed plan being published after a stop.
        self.require(self.store.control() == control and self.store.optional('status.json') == status
                     and self.store.optional('boot.json') == receipt, 'BOOT_RECOVERY_STATE_CHANGED')
        return proposal

    def review(self, proposal): return self.boot.fingerprint(proposal)

    def save(self, record, phase):
        path = self.journal_path(record['id'])
        if self.expected is not None:
            self.require(self.read_record(path) == self.expected, 'BOOT_RECOVERY_JOURNAL_CHANGED')
        record['phase'] = phase; record['updated_at'] = self.boot.worker_now()
        self.ws.write(path, record); self.expected = deepcopy(record)
        self.hook('journal:' + phase)

    def guard(self, record, *, observing=False):
        transition, manifest, history, initial = self.common() if not observing else self.observing_common()
        self.require(manifest == record['manifest'] and (observing or history == record['history'])
                     and self.boot.fingerprint(transition) == record['transition_sha256']
                     and self.boot.fingerprint(initial) == record['initial_history_sha256'],
                     'BOOT_RECOVERY_BINDING_CHANGED')
        self.require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == record['adapter_sha256'],
                     'BOOT_RECOVERY_ADAPTER_CHANGED')
        self.require(self.identity()['boot_id'] == record['boot_id'], 'BOOT_RECOVERY_BOOT_CHANGED')
        control = self.store.control()
        self.require(control.get('service_stop_token') == record['control']['service_stop_token'],
                     'SERVICE_STOP_REQUEST_CHANGED')
        if not observing:
            self.require(control == record['control'], 'BOOT_RECOVERY_STATE_CHANGED')
            current = self.service.owned(manifest, self.backend)
            self.require(self.service.inactive(current) and self.backend.cgroup_empty(current), 'SERVICE_STILL_RUNNING')
        return manifest

    def observing_common(self):
        # A new legitimate evaluation may own an open intent; observation
        # validates its completed outcome instead of rejecting active work.
        transition, manifest = self.initial.committed()
        self.initial.environment(self.directory, manifest)
        self.require(self.service.enabled(manifest), 'BOOT_OWNED_ENABLE_REQUIRED')
        self.require(self.store.manifest()['binding'] == manifest['binding'], 'SERVICE_BINDING_CHANGED')
        initial = self.ws.read(self.initial.path)
        return transition, manifest, None, initial

    def attention(self, record, code):
        with self.store.lock():
            control = self.store.control()
            if control.get('service_stop_token') == record['control']['service_stop_token']:
                before = deepcopy(control); control['desired'] = 'paused'
                if control['recovery'] is None: control['recovery'] = code
                if before != control: self.ws.write(self.store.path / 'control.json', control)
        # Never repair a changed journal or clear a newer stop/recovery.
        record['code'] = code; self.save(record, 'attention_required')
        return dict(result='attention_required', code=code, recovery_id=record['id'],
                    fresh_evaluation_verified=False, start_not_repeated=True,
                    next_step='retain_audit_use_check_or_explicit_owned_stop_and_report')

    def apply(self, expect, wait_seconds=270):
        record = None
        with self.ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
            with self.ws.automatic_lease(self.directory):
                with self.change.change_lock(self.directory):
                    proposal = self.plan(wait_seconds, held=True)
                    self.require(self.review(proposal) == expect, 'BOOT_RECOVERY_REVIEW_CHANGED')
                    self.folder.mkdir(mode=0o700, exist_ok=True); self.manage.private(self.folder)
                    self.require(len(json.dumps(proposal).encode()) < self.ws.LIMIT - 4096, 'WORKER_RECORD_LIMIT')
                    record = deepcopy(proposal)
                    self.save(record, 'prepared')  # Durable old failure before any mutation.
                    try:
                        self.readback_attempted = True
                        verified = self.readback(self.directory, record['manifest'], lambda: self.guard(record))
                        self.require(verified == dict(result='passed', live_checks_performed=True,
                                     configuration_written=False, restart_requested=False), 'BOOT_LIVE_READBACK_REQUIRED')
                        self.readback_verified = True
                        self.guard(record)
                        self.require(self.store.optional('status.json') == record['failure_status']
                                     and self.store.optional('boot.json') == record['failure_receipt'],
                                     'BOOT_RECOVERY_STATE_CHANGED')
                        self.save(record, 'readback_verified')
                        self.backend.reset_failed(); self.guard(record)
                        with self.store.lock():
                            self.require(self.store.control() == record['control'], 'BOOT_RECOVERY_STATE_CHANGED')
                            self.store.status(phase='service_starting', completed_cycles=0, recovery_id=record['id'])
                            running = dict(record['control'], desired='running', recovery=None)
                            receipt = dict(schema=1, phase='armed', generation=self.boot.policy(record['manifest'])['generation'],
                                manifest_sha256=self.boot.fingerprint(record['manifest']),
                                control_sha256=self.boot.fingerprint(running),
                                status_sha256=self.boot.fingerprint(self.store.optional('status.json')),
                                identity=self.identity(), recovery_id=record['id'])
                            self.require(receipt['identity']['boot_id'] == record['boot_id'], 'BOOT_RECOVERY_BOOT_CHANGED')
                            self.ws.write(self.store.path / 'boot.json', receipt)
                        record['receipt_sha256'] = self.boot.fingerprint(receipt); self.save(record, 'armed')
                        self.guard(record); self.save(record, 'start_requested')
                        with self.store.lock():
                            self.require(self.store.control() == record['control'] and
                                         self.store.optional('boot.json') == receipt, 'BOOT_RECOVERY_STATE_CHANGED')
                            self.ws.write(self.store.path / 'control.json', running)
                        self.hook('running_released')
                    except BaseException as exc:
                        return self.attention(record, self.local.error_code(exc))
            try:
                self.require(self.store.control() == running, 'BOOT_RECOVERY_STATE_CHANGED')
                self.service.owned(record['manifest'], self.backend)
                self.backend.start(); self.hook('start_returned'); self.save(record, 'submitted')
                result = self.observe_record(record)
                self.save(record, 'accepted')
                return result
            except BaseException as exc:
                return self.attention(record, self.local.error_code(exc))

    def observe_record(self, record):
        parent = self
        class Controller:
            def manifest(self): return parent.guard(record, observing=True)
            def status(self):
                parent.guard(record, observing=True)
                state = parent.initial.status_fn(parent.directory, parent.backend)
                invocation = state['provider_status'].get('InvocationID')
                if state['provider_status']['ActiveState'] in ('active', 'activating') and invocation:
                    parent.require(invocation != record['prior_service_invocation'], 'BOOT_MANAGER_INVOCATION_REUSED')
                if state.get('current_invocation_verified'):
                    receipt = parent.store.optional('boot.json') or {}
                    last = state['worker']['last_observation']
                    parent.require(receipt.get('phase') == 'entered'
                        and receipt.get('previous_receipt_sha256') == record['receipt_sha256']
                        and receipt.get('manifest_sha256') == parent.boot.fingerprint(record['manifest'])
                        and receipt.get('generation') == parent.boot.policy(record['manifest'])['generation']
                        and receipt.get('service_invocation') == invocation
                        and receipt.get('invocation') == last.get('invocation')
                        and receipt.get('identity') == last.get('identity'), 'BOOT_ENTRY_EVIDENCE_INVALID')
                return state
        result = dict(result='attention_required', recovery_id=record['id'], start_request_submitted=True,
                      fresh_evaluation_verified=False, actual_reboot_verified=False)
        before = dict(provider_status=dict(ActiveState='inactive', InvocationID=record['prior_service_invocation']))
        self.observe.observe(Controller(), before, record['manifest'], result, record['wait_seconds'], self.clock, self.sleep)
        self.require(self.tx.snapshot(self.directory) == record['manifest']['binding'], 'SERVICE_BINDING_CHANGED')
        final = Controller().status()
        self.require(final.get('desired_state') == 'running' and final.get('recovery') is None
                     and final.get('current_invocation_verified') is True and final.get('evaluation_status') == 'healthy'
                     and final['provider_status']['InvocationID'] == result['management']['provider_status']['InvocationID'],
                     'BOOT_FINAL_OBSERVATION_CHANGED')
        result['next_step'] = 'read_only_backup_plan_without_repeating_setup'
        return result

    def check(self, identifier):
        record = self.read_record(self.journal_path(identifier))
        self.require(record.get('schema') == 1 and record.get('kind') == KIND
                     and record.get('id') == identifier and record.get('directory') == str(self.directory)
                     and record.get('phase') in ('start_requested', 'submitted', 'accepted'), 'BOOT_RECOVERY_CHECK_REFUSED')
        # No new lock, journal update, pause, reset, or start in this action.
        return self.observe_record(record)


def controller(directory):
    path = ROOT / 'src/ops/tools/boot_control.py'
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('reviewed controller symlink')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BOOT_CONTROL_SHA:
        raise ValueError('reviewed controller changed')
    ctl = load('recovery_reviewed_boot_control', path)
    ctl.source_read(path)
    value = ctl.Controller(directory)
    value.require = ctl.require
    ctl.require(value.sources['code_state'] == 'candidate', 'BOOT_CANDIDATE_CODE_REQUIRED')
    value.connect(boot=True)
    initial = ctl.load('recovery_reviewed_initial_start', ROOT / ctl.START / 'arming.py').Start(value.tx)
    return Recovery(initial)


def run(action, directory, expect=None, identifier=None, wait_seconds=270, factory=controller):
    report = dict(schema=1, stage=STAGE, action=action, result='attention_required',
                  checked_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                  scope='existing_owned_failed_boot_worker_same_boot',
                  configuration_written=False, business_service_control_requested=False,
                  code_or_policy_written=False, linger_changed=False, image_build_or_pull_requested=False,
                  inference_requested=False, actual_reboot_acceptance='deferred_by_user', backup_executed=False,
                  identity_contents_read=False, live_configuration_readback_requested=action == 'apply')
    if action in ('plan', 'check'):
        report.update(application_credentials_read=False, application_api_requests=0, files_written=[])
    recovery = None
    try:
        from src.ops.platform.contracts import require
        require(action in ('plan', 'apply', 'check'), 'INVALID_BOOT_RECOVERY_ACTION')
        require((action == 'apply' and isinstance(expect, str) and re.fullmatch('[a-f0-9]{64}', expect)
                 or action != 'apply' and expect is None), 'BOOT_RECOVERY_REVIEW_REQUIRED')
        require((action == 'check' and isinstance(identifier, str) and re.fullmatch('[a-f0-9]{64}', identifier)
                 or action != 'check' and identifier is None), 'BOOT_RECOVERY_ID_INVALID')
        recovery = factory(Path(os.path.abspath(Path(directory).expanduser())))
        if action == 'plan':
            proposal = recovery.plan(wait_seconds)
            report.update(result='passed', review_id=recovery.review(proposal), recovery_id=proposal['id'],
                          reason=REASON, persistence='boot', autostart_enabled=True,
                          failure_checked_at=proposal['failure_status']['checked_at'],
                          prior_completed_cycles=proposal['failure_status']['completed_cycles'],
                          wait_seconds=wait_seconds, files_written=[], application_api_requests=0,
                          next_step='apply_once_with_review_id')
        elif action == 'apply': report.update(recovery.apply(expect, wait_seconds))
        else: report.update(recovery.check(identifier))
    except Exception as exc:
        code = getattr(exc, 'code', None)
        report.update(result='attention_required', code=code if isinstance(code, str)
                      and re.fullmatch('[A-Z][A-Z0-9_]{0,95}', code) else 'BOOT_RECOVERY_LOCAL_ERROR',
                      next_step='retain_failure_and_recovery_audits_and_report')
    report['worker_service_control_commands'] = getattr(getattr(recovery, 'backend', None), 'control_commands', 0)
    report['live_configuration_readback_attempted'] = getattr(recovery, 'readback_attempted', False)
    report['live_configuration_readback_verified'] = getattr(recovery, 'readback_verified', False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply', 'check'))
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/agent-project-ops/server')
    parser.add_argument('--expect-plan'); parser.add_argument('--recovery-id')
    parser.add_argument('--wait-seconds', type=int, default=270)
    args = parser.parse_args(argv)
    value = run(args.action, args.state_dir, args.expect_plan, args.recovery_id, args.wait_seconds)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
