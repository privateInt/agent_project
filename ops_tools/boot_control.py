#!/usr/bin/env python3
"""Reviewed Ops boot upgrade: plan, explicit paused transition, start and recovery.

No host reboot, linger change, Docker service start or business deployment.
Each action is a fresh process; applying source never starts it in that process.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from types import SimpleNamespace

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
STAGE = 'O04-reviewed-boot-lifecycle-control'
BASE = 'a24257d8636b5842167e74f7b02931c467d105519fb9b5f70ceb577437fccca4'
TARGET = '2bf14042a6aa15e4e12b46aa7be494eef2b8834c9b44c4a5587610d5d3a3bb1f'
TX = Path('ops_candidates/boot_transition_20260929')
START = Path('ops_candidates/boot_start_20260929')
PINS = {str(TX/'manifest.json'): 'aace1d36c462631608e88294be21b11bf85dc1b3e83033096fdba124a475426f',
        str(START/'manifest.json'): '6861bf1d553bd5fd86926ca91dbbeef7b71f6fd118c242aa88cd28124cfdc25e'}
RUNTIME = ('ops_platform', 'infra/observability/ops', 'infra/langfuse/ops')
ACTIONS = ('plan', 'status', 'apply', 'finish', 'start', 'check', 'stop', 'cancel', 'rollback', 'resume-session')


class Failure(Exception):
    def __init__(self, code, detail=None):
        self.code = code; self.detail = detail; super().__init__(code)


def require(condition, code):
    if not condition: raise Failure(code)


def sha(raw): return hashlib.sha256(raw).hexdigest()
def fingerprint(value): return sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def source_read(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'BOOT_SOURCE_SYMLINK_REFUSED')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and info.st_size <= 2*1024*1024, 'BOOT_SOURCE_UNSAFE')
        with os.fdopen(os.dup(fd), 'rb') as stream: raw = stream.read(2*1024*1024+1)
        require(len(raw) <= 2*1024*1024, 'BOOT_SOURCE_LIMIT')
        return raw
    finally: os.close(fd)


def reviewed_sources(root):
    """Verify fixed candidate and runtime bytes before importing project code.

    Recovery accepts reviewed mixed before/after bytes, never arbitrary modules.
    The pinned historical manifests describe inactive bundles; this separate
    explicit CLI is the deployment entry for the already accepted bundles.
    """
    docs = {}
    for rel, wanted in PINS.items():
        raw = source_read(root/rel); require(sha(raw) == wanted, 'BOOT_REVIEWED_MANIFEST_CHANGED')
        docs[rel] = json.loads(raw)
    spec = docs[str(TX/'manifest.json')]; arming = docs[str(START/'manifest.json')]
    for folder, doc, name in ((TX, spec, 'transition.py'), (START, arming, 'arming.py')):
        wanted = {row['path']: row['sha256'] for row in doc['auxiliary_files']}[name]
        require(sha(source_read(root/folder/name)) == wanted, 'BOOT_REVIEWED_CONTROLLER_CHANGED')
    require(spec['base_code_sha256'] == BASE and spec['candidate_code_sha256'] == TARGET,
            'BOOT_REVIEWED_IDENTITY_CHANGED')
    base = {row['path']: row['sha256'] for row in spec['base_files']}
    changes = {row['path']: row for row in spec['changes']}
    for rel, row in changes.items():
        require(sha(source_read(root/TX/'source'/rel)) == row['after_sha256'], 'BOOT_REVIEWED_PAYLOAD_CHANGED')
    actual = {str(p.relative_to(root)) for folder in RUNTIME for p in (root/folder).rglob('*.py')
              if 'tests' not in p.relative_to(root).parts}
    require(set(base) <= actual <= set(base)|set(changes), 'BOOT_RUNTIME_SET_CHANGED')
    digest = hashlib.sha256()
    for rel in sorted(actual):
        raw = source_read(root/rel); wanted = {base.get(rel)}
        if rel in changes: wanted.add(changes[rel]['after_sha256'])
        require(sha(raw) in wanted, 'BOOT_RUNTIME_SOURCE_CHANGED')
        digest.update(rel.encode()+b'\0'+hashlib.sha256(raw).digest())
    current = digest.hexdigest()
    return {'code_sha256': current, 'code_state': 'base' if current == BASE else 'candidate' if current == TARGET else 'mixed',
            'code_files': len(actual), 'base_code_sha256': BASE, 'candidate_code_sha256': TARGET,
            'controller_sha256': sha(source_read(root/'ops_tools/boot_control.py'))}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def dependencies(root):
    sys.path.insert(0, str(root))
    from ops_platform.ops import manage, worker_state as ws, refresh_worker as worker
    from ops_platform.ops import service_lifecycle as service, lifecycle_probe as probe, lifecycle_state as local
    from ops_platform.ops import service_systemd as adapter, service_start as observation, auto_refresh
    require(worker.ROOT == root, 'BOOT_FRESH_INTERPRETER_REQUIRED')
    return SimpleNamespace(manage=manage, ws=ws, worker=worker, service=service, probe=probe,
                           local=local, adapter=adapter, observation=observation, preflight=auto_refresh)


class Controller:
    def __init__(self, directory, root=ROOT):
        self.root = root; self.sources = reviewed_sources(root); self.r = dependencies(root)
        self.directory = self.r.manage.private(self.r.manage.safe(directory))
        owner = self.r.ws.read(self.directory/'owner.json')
        require(owner.get('project_root') == str(root) and owner.get('uid') == os.getuid()
                and owner.get('gid') == os.getgid(), 'BOOT_INSTALLATION_OWNER_MISMATCH')
        self.store = self.r.ws.Store(self.directory)
        self.reader = self.r.probe.Reader(); self.backend = None; self.systemd = None
        self.transition_module = load('reviewed_boot_transition', root/TX/'transition.py')
        self.bundle = self.transition_module.Bundle(root/TX)
        self.tx = None
        self.pause_requested = False

    def connect(self, boot=False):
        manifest = self.manifest()
        self.systemd = self.r.probe.systemd(self.reader, manifest['unit_name'])
        require(self.systemd.get('status') == 'ok', 'SYSTEMD_USER_MANAGER_UNAVAILABLE')
        if boot: require(self.systemd.get('linger') is True, 'BOOT_MANAGER_REQUIRED')
        self.backend = self.r.adapter.Systemd(self.reader, self.systemd, manifest['unit_name'])
        self.tx = self.transition_module.Transition(self.directory, self.bundle, self.backend)

    def manifest(self): return self.r.service.load(self.directory)
    def status(self): return self.r.service.status(self.directory, self.backend)
    def project(self, value): return self.r.observation.projection(value)

    def plan(self, startup_seconds, *, clock=None, sleep=None):
        """Wait only for read contention or a current running evaluation.

        No resume/start/stop or journal write occurs here. Each new attempt
        revalidates the complete proposal before producing a review ID.
        The budget bounds retries; an in-flight metadata command retains its
        own timeout. Persistent recovery, ownership and binding errors stop.
        """
        clock = clock or time.monotonic; sleep = sleep or time.sleep
        started = clock(); attempts = 0; last = None
        while True:
            attempts += 1
            try:
                public, proposal = self.plan_once(startup_seconds)
                public['plan_wait'] = {'attempts': attempts, 'retry_budget_seconds': 30,
                                       'elapsed_seconds': round(clock()-started, 3),
                                       'last_retry_code': last}
                return public, proposal
            except Exception as exc:
                code = getattr(exc, 'code', None)
                if (isinstance(exc, Failure) and code == 'BOOT_PREFLIGHT_BLOCKED'
                        and exc.detail.get('blockers') == ['PREPARED_STATE_BUSY']):
                    code = 'PREPARED_STATE_BUSY'
                if code not in ('PREPARED_STATE_BUSY', 'PUBLIC_URL_CHANGE_BUSY', 'BOOT_SESSION_EVALUATING'):
                    raise
                last = code
                remaining = 30 - (clock()-started)
                if remaining <= 0:
                    raise Failure('BOOT_PLAN_WAIT_TIMEOUT', {'attempts': attempts,
                        'retry_budget_seconds': 30, 'elapsed_seconds': round(clock()-started, 3),
                        'last_retry_code': last}) from None
                sleep(min(2, remaining))

    def session_ready(self, value):
        require(value['status'] == 'owned' and value['persistence'] == 'session' and not value['enabled']
                and value['desired_state'] == 'running' and value['recovery'] is None,
                'BOOT_HEALTHY_SESSION_REQUIRED')
        if (value['provider_status']['ActiveState'] in ('active', 'activating')
                and value['evaluation_status'] in ('in_progress', 'first_evaluation_pending')):
            raise Failure('BOOT_SESSION_EVALUATING')
        require(value['current_invocation_verified'] and value['evaluation_status'] == 'healthy',
                'BOOT_HEALTHY_SESSION_REQUIRED')

    def plan_once(self, startup_seconds):
        require(self.sources['code_state'] == 'base', 'BOOT_BASE_CODE_REQUIRED')
        report = self.r.preflight.run('plan', self.directory, 'boot', reader=self.reader)
        if report.get('environment_ready') is not True:
            raise Failure('BOOT_PREFLIGHT_BLOCKED', {key: [item for item in report.get(key, [])
                          if isinstance(item, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,95}', item)][:64]
                          for key in ('blockers', 'warnings')})
        self.connect(boot=True)
        before = self.status()
        self.session_ready(before)
        proposal = self.tx.plan('boot', startup_seconds)
        after = self.status()
        require(after['provider_status']['InvocationID'] == before['provider_status']['InvocationID']
                and after['desired_state'] == 'running' and after['recovery'] is None,
                'BOOT_PLAN_SERVICE_CHANGED')
        self.session_ready(after)
        contract = {key: proposal[key] for key in ('before', 'control_before_stop', 'provenance', 'bundle_sha256')}
        contract.update(startup_seconds=startup_seconds, controller_sha256=self.sources['controller_sha256'],
                        boot_id=self.r.worker.process_identity()['boot_id'],
                        service_invocation=after['provider_status']['InvocationID'])
        public = {'result': 'passed', 'review_id': fingerprint(contract), 'source': self.sources,
                  'state_dir': str(self.directory), 'unit_name': proposal['before']['unit_name'],
                  'policy': {'saved': 'session', 'target': 'boot', 'startup_seconds': startup_seconds},
                  'interval_seconds': proposal['before']['interval_seconds'],
                  'deadline_seconds': proposal['before']['deadline_seconds'],
                  'management': self.project(after), 'manager_version': self.systemd.get('manager_version'),
                  'linger': self.systemd['linger'], 'preflight_warnings': report['warnings'],
                  'transition_end_state': 'paused_and_disabled', 'prepared_state_stable': report['prepared_state_stable'],
                  'application_credentials_read': False, 'application_api_requests': 0,
                  'runtime_files_written': False, 'reboot_readiness_verified': False,
                  'next_step': 'apply_with_review_id_then_start_in_fresh_process'}
        return public, proposal

    def start_adapter(self):
        require(self.sources['code_state'] == 'candidate', 'BOOT_CANDIDATE_CODE_REQUIRED')
        self.connect(boot=True)
        arming = load('reviewed_boot_start', self.root/START/'arming.py')
        return arming.Start(self.tx)

    def overview(self):
        self.connect()
        transition = self.tx.load() if self.tx.path.exists() else None
        path = self.store.path.parent/'boot-start.json'
        arming = self.r.ws.read(path) if path.exists() else None
        if transition:
            require(transition.get('phase') in ('stop_requested', 'prepared', 'writing', 'committed', 'rolling_back', 'rolled_back'),
                    'TRANSITION_RECORD_INVALID')
            if transition['phase'] in ('committed', 'rolled_back') and 'files' in transition:
                self.tx.verify_files(transition, 'after' if transition['phase'] == 'committed' else 'before')
        if arming:
            require(arming.get('phase') in ('prepared', 'armed', 'start_requested', 'submitted', 'accepted', 'cancelling', 'cancelled', 'attention_required'),
                    'BOOT_START_RECORD_INVALID')
        current = self.status()
        rolled_back = transition is not None and transition['phase'] == 'rolled_back'
        recovery = (self.sources['code_state'] == 'mixed' or bool(current.get('recovery'))
                    or transition is not None and transition['phase'] not in ('committed', 'rolled_back')
                    or not rolled_back and arming is not None and arming['phase'] not in ('accepted', 'cancelled'))
        running = current.get('desired_state') == 'running'
        healthy = current.get('current_invocation_verified') is True and current.get('evaluation_status') == 'healthy'
        return {'result': 'attention_required' if recovery or running and not healthy else 'passed', 'diagnostic_status': 'completed',
                'recovery_required': bool(recovery), 'source': self.sources, 'management': self.project(current),
                'current_health_verified': bool(running and healthy),
                'transition_phase': transition.get('phase') if transition else None,
                'initial_start_phase': arming.get('phase') if arming else None,
                'initial_start_is_historical': arming is not None,
                'initial_start_boot_id': arming.get('boot_id') if arming else None,
                'current_boot_id': self.r.worker.process_identity()['boot_id'],
                'application_credentials_read': False, 'application_api_requests': 0,
                'runtime_files_written': False, 'next_step': 'follow_current_phase_and_recovery'}

    def resume_session(self, wait_seconds):
        require(self.sources['code_state'] == 'base', 'BOOT_BASE_CODE_REQUIRED')
        self.connect(); record = self.tx.load()
        require(record['phase'] == 'rolled_back', 'BOOT_ROLLBACK_REQUIRED')
        self.bundle.check_tree(self.root)
        require(self.store.control()['desired'] == 'paused', 'WORKER_PAUSE_REQUIRED')
        self.tx.exit_proof(record)
        require(self.manifest() == record['before'] and self.store.manifest()['binding'] == record['before']['binding'],
                'SERVICE_BINDING_CHANGED')
        before = self.status()
        result = self.r.service.run('resume', self.directory, persistence='session', reader=self.reader, backend=self.backend)
        if result.get('result') != 'passed':
            exc = Failure(result.get('code', 'BOOT_SESSION_RESUME_FAILED'))
            exc.service_command = result.get('service_command'); raise exc
        observed = {'result': 'attention_required', 'fresh_evaluation_verified': False}
        manifest = self.manifest()
        budget = wait_seconds or manifest['interval_seconds']+manifest['deadline_seconds']+30
        self.r.observation.observe(self, before, manifest, observed, budget, time.monotonic, time.sleep)
        observed['next_step'] = 'session_restored_boot_upgrade_rolled_back'
        observed['explicit_recovery_readback_requested'] = True
        return observed

    def execute(self, action, expect, startup_seconds, wait_seconds):
        if action == 'plan': return self.plan(startup_seconds)[0]
        if action == 'status': return self.overview()
        if action == 'apply':
            public, proposal = self.plan(startup_seconds)
            require(expect == public['review_id'], 'BOOT_REVIEW_CHANGED')
            result = self.tx.apply(proposal)
            require(result.get('phase') == 'committed', 'BOOT_TRANSITION_NOT_COMMITTED')
            return dict(result, activation_supported=True, production_cli_available=True,
                        next_step='start_in_fresh_process', source_after=reviewed_sources(self.root))
        if action in ('finish', 'rollback'):
            self.connect(boot=action == 'finish')
            result = getattr(self.tx, action)()
            return dict(result, activation_supported=True, production_cli_available=True,
                        next_step='start_in_fresh_process' if action == 'finish' else 'resume_session_in_fresh_process',
                        source_after=reviewed_sources(self.root))
        if action in ('stop', 'cancel'):
            # Stop remains durable even if the user manager becomes unavailable.
            self.r.service.request_pause(self.directory); self.pause_requested = True
            if action == 'cancel':
                require(self.sources['code_state'] == 'candidate', 'BOOT_CANDIDATE_CODE_REQUIRED')
                self.connect()
                arming = load('reviewed_boot_start', self.root/START/'arming.py')
                result = arming.Start(self.tx).cancel()
            else:
                self.connect(); result = self.r.service.run('stop', self.directory, backend=self.backend)
            return dict(result, next_step='inspect_recovery_then_explicit_rollback_or_finish')
        if action == 'resume-session': return self.resume_session(wait_seconds)
        start = self.start_adapter()
        if action == 'start':
            result = start.apply(start.plan(wait_seconds))
        else:
            # This observes initial submission only. A later successful boot
            # must not be paused by the old receipt's different-boot guard.
            require(start.load()['boot_id'] == self.r.worker.process_identity()['boot_id'],
                    'BOOT_INITIAL_CHECK_DIFFERENT_BOOT_USE_STATUS')
            result = start.check()
        return dict(result, production_cli_available=True,
                    next_step='boot_environment_and_actual_reboot_acceptance' if result.get('result') == 'passed'
                    else 'status_then_explicit_check_or_cancel')


def run(action, directory, expect=None, startup_seconds=300, wait_seconds=None, factory=Controller):
    controller = None
    report = {'schema': 1, 'stage': STAGE, 'action': action, 'result': 'attention_required',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'scope': 'existing_owned_ops_worker_code_and_policy', 'host_reboot_requested': False,
              'linger_changed': False, 'image_build_or_pull_requested': False,
              'business_service_control_requested': False, 'inference_requested': False,
              'logout_or_boot_acceptance': False, 'long_running_acceptance': False,
              'kubernetes_acceptance': 'deferred', 'production_cli_available': True}
    if action in ('plan', 'status'):
        report.update(files_written=[], configuration_written=False, service_control_requested=False,
                      application_credentials_read=False, application_api_requests=0, activation_requested=False)
    try:
        require(action in ACTIONS, 'INVALID_BOOT_ACTION')
        require(type(startup_seconds) is int and 1 <= startup_seconds <= 300, 'INVALID_BOOT_STARTUP_LIMIT')
        require(startup_seconds == 300 or action in ('plan', 'apply'), 'BOOT_STARTUP_OPTION_SCOPE')
        require(expect is None and action != 'apply' or action == 'apply' and isinstance(expect, str)
                and re.fullmatch('[a-f0-9]{64}', expect), 'BOOT_REVIEW_ID_REQUIRED')
        require(wait_seconds is None or action in ('start', 'resume-session') and type(wait_seconds) is int
                and 1 <= wait_seconds <= 1200, 'BOOT_WAIT_OPTION_SCOPE')
        controller = factory(Path(os.path.abspath(Path(directory).expanduser())))
        result = controller.execute(action, expect, startup_seconds, wait_seconds)
        report.update(result, stage=STAGE, action=action)
    except Exception as exc:
        code = getattr(exc, 'code', None)
        report.update(result='attention_required', code=code if isinstance(code, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,95}',code)
                      else 'BOOT_LOCAL_OR_COMMAND_ERROR', next_step='inspect_status_keep_journals_and_backups')
        if isinstance(exc, Failure) and exc.code == 'BOOT_PREFLIGHT_BLOCKED':
            report['preflight'] = exc.detail
        if isinstance(exc, Failure) and exc.code == 'BOOT_PLAN_WAIT_TIMEOUT':
            report['plan_wait'] = exc.detail
        if controller is not None:
            detail = controller.r.adapter.command_details(getattr(exc, 'service_command', None))
            if detail is not None: report['service_command'] = detail
    finally:
        report['worker_service_control_commands'] = getattr(getattr(controller, 'backend', None), 'control_commands', 0)
        report['durable_pause_requested'] = getattr(controller, 'pause_requested', False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=ACTIONS)
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/agent-project-ops/server')
    parser.add_argument('--expect-plan')
    parser.add_argument('--startup-seconds', type=int, default=300)
    parser.add_argument('--wait-seconds', type=int)
    args = parser.parse_args(argv)
    value = run(args.action, args.state_dir, args.expect_plan, args.startup_seconds, args.wait_seconds)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
