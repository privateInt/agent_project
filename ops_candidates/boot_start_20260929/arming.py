"""Inactive initial boot start adapter. No production CLI and no code rebind.

Use only in a fresh interpreter after a committed, paused transition. The
worker consumes the existing boot receipt protocol; this controller is not a
worker dependency. An uncertain start is never resubmitted.
"""
from copy import deepcopy
from pathlib import Path
import time
import types
import uuid

from ops_platform.contracts import require
from ops_platform.ops import boot_lifecycle as boot, service_lifecycle as service
from ops_platform.ops import worker_state as ws, refresh_worker as worker
from ops_platform.ops import lifecycle_probe as probe, lifecycle_state as local
from ops_platform.ops import service_start as observation, set_public_url as change

KIND = 'initial-boot-start'
TERMINAL = {'accepted', 'cancelled', 'attention_required'}


def code_signature(code):
    # Ignore source filename/line placement, which differ in isolated copies.
    return (code.co_code, code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars,
            code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount, code.co_flags,
            tuple(code_signature(c) if isinstance(c, types.CodeType) else c for c in code.co_consts))


def loaded_function(module, name, source):
    actual = module
    expected = compile(source, '<reviewed-candidate>', 'exec', dont_inherit=True)
    for part in name.split('.'):
        actual = getattr(actual, part, None)
        candidates = [c for c in expected.co_consts if isinstance(c, types.CodeType) and c.co_name == part]
        require(len(candidates) == 1, 'BOOT_FRESH_INTERPRETER_REQUIRED')
        expected = candidates[0]
    require(hasattr(actual, '__code__') and code_signature(actual.__code__) == code_signature(expected),
            'BOOT_FRESH_INTERPRETER_REQUIRED')


def environment(directory, manifest):
    report = probe.systemd(probe.Reader(), manifest['unit_name'])
    require(report.get('status') == 'ok' and report.get('linger') is True,
            'BOOT_MANAGER_REQUIRED')


def live_readback(directory, manifest, guard, options=None):
    """Current configuration readback only; never activate a discovery proposal."""
    activation, refresh = worker.activation, worker.refresh
    kwargs = dict(options or {})
    guard()
    context = refresh.lineage(directory)
    require(not context['pending'] and context['next'] <= refresh.MAX_GENERATIONS,
            'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
    docker = kwargs.get('docker') or activation.Docker(project=worker.manage.prepared_project(directory))
    api = kwargs.get('api_factory', activation.rc.API)
    record = activation.capture(directory, docker, kwargs.get('discover_fn', activation.discover_now),
        kwargs.get('validate_fn', activation.validate_effective),
        kwargs.get('source_fn', activation.source_snapshot), api,
        planner=lambda cfg, discovered: refresh.merge(cfg, discovered, context['fields']),
        additional_evidence=context['evidence'])
    guard()
    cfg = worker.manage.existing(directory)
    client = api(cfg); client.health(); cookie = client.login()
    try:
        guard(); kwargs.get('readback_fn', activation.readback)(cfg); guard()
    finally: client.logout(cookie)
    activation.guard(directory, record['baseline'], 'original')
    change.protect(docker, record['baseline']); guard()
    return {'result': 'passed', 'live_checks_performed': True,
            'configuration_written': False, 'restart_requested': False}


class Start:
    def __init__(self, transition, *, environment_fn=environment, readback_fn=live_readback,
                 identity_fn=worker.process_identity, status_fn=service.status,
                 clock=time.monotonic, sleep=time.sleep, hook=lambda phase: None):
        self.transition = transition
        self.directory, self.backend, self.store = transition.directory, transition.backend, transition.store
        self.path = self.store.path.parent / 'boot-start.json'
        self.environment, self.readback, self.identity = environment_fn, readback_fn, identity_fn
        self.status_fn, self.clock, self.sleep, self.hook = status_fn, clock, sleep, hook

    def committed(self):
        record = self.transition.load()
        require(record['phase'] == 'committed', 'BOOT_COMMITTED_TRANSITION_REQUIRED')
        manifest = service.load(self.directory)
        require(manifest == record['after'] and manifest['persistence'] == 'boot', 'BOOT_TRANSITION_BINDING_CHANGED')
        boot.policy(manifest)
        self.transition.verify_files(record, 'after')
        self.transition.bundle.check_tree(self.transition.root, 'candidate')
        require(self.store.manifest()['binding'] == manifest['binding'], 'SERVICE_BINDING_CHANGED')
        # The interpreter must already contain the target runtime, not old
        # modules imported before replacing their files on disk.
        for module, functions in ((boot, ('Context.enter', 'Context.guards')),
                                  (worker, ('run', 'acknowledge_recovery')),
                                  (ws, ('transition_gate', 'Store.gate')),
                                  (service, ('run',))):
            expected = self.transition.bundle.payload['ops_platform/ops/' + Path(module.__file__).name]
            for name in functions: loaded_function(module, name, expected)
        return record, manifest

    def paused(self, manifest, control):
        self.transition.no_intent()
        require(self.store.control() == control and control['desired'] == 'paused'
                and control['recovery'] is None and control.get('service_stop_token'), 'BOOT_CONTROL_CHANGED')
        require(self.transition.snapshot(self.directory) == manifest['binding'], 'SERVICE_BINDING_CHANGED')
        self.environment(self.directory, manifest)
        current = service.owned(manifest, self.backend)
        require(service.inactive(current) and self.backend.cgroup_empty(current), 'SERVICE_STILL_RUNNING')
        return current

    def plan(self, wait_seconds=None):
        require(not self.path.exists() and not self.path.is_symlink(), 'BOOT_START_ALREADY_RECORDED')
        record, manifest = self.committed()
        control = self.store.control()
        self.transition.exit_proof(record)
        current = self.paused(manifest, control)
        require(self.store.optional('boot.json') is None, 'BOOT_RECEIPT_ALREADY_EXISTS')
        wait = wait_seconds if wait_seconds is not None else (
            boot.policy(manifest)['startup_seconds'] + manifest['deadline_seconds'] + manifest['interval_seconds'] + 30)
        require(type(wait) is int and 1 <= wait <= 1200, 'INVALID_OBSERVATION_LIMIT')
        return {'schema': 1, 'kind': KIND, 'id': str(uuid.uuid4()), 'directory': str(self.directory),
                'transition_sha256': boot.fingerprint(record), 'manifest': manifest,
                'control': control, 'prior_status': self.store.optional('status.json'),
                'prior_service_invocation': current.get('InvocationID', ''),
                'boot_id': self.identity()['boot_id'], 'wait_seconds': wait,
                'production_cli_available': False, 'logout_or_boot_acceptance': False}

    def save(self, record, phase):
        record['phase'] = phase; record['updated_at'] = boot.worker_now()
        ws.write(self.path, record); self.hook('journal:' + phase)

    def load(self):
        record = ws.read(self.path)
        require(record.get('schema') == 1 and record.get('kind') == KIND
                and record.get('directory') == str(self.directory)
                and str(uuid.UUID(record['id'])) == record['id'], 'BOOT_START_RECORD_INVALID')
        require(record.get('phase') in TERMINAL | {'prepared', 'armed', 'start_requested', 'submitted', 'cancelling'},
                'BOOT_START_RECORD_INVALID')
        return record

    def guard(self, record, enabled=None):
        transition, manifest = self.committed()
        require(boot.fingerprint(transition) == record['transition_sha256']
                and manifest == record['manifest'], 'BOOT_START_RECORD_CHANGED')
        require(self.identity()['boot_id'] == record['boot_id'], 'BOOT_START_BOOT_CHANGED')
        self.paused(manifest, record['control'])
        require(self.transition.history() == transition['provenance'], 'TRANSITION_PROVENANCE_CHANGED')
        if enabled is not None:
            require(service.enabled(manifest) is enabled, 'BOOT_ENABLE_CHANGED')

    def attention(self, record, code):
        # A late queued start meets pause. No saved PID is signalled and no
        # unknown service command is repeated. Preserve a newer stop/recovery.
        require(ws.read(self.path) == record, 'BOOT_START_RECORD_CHANGED')
        with self.store.lock():
            control = self.store.control(); control['desired'] = 'paused'
            if control['recovery'] is None: control['recovery'] = code
            ws.write(self.store.path / 'control.json', control)
        try:
            service.owned(record['manifest'], self.backend)
            if service.set_enabled(record['manifest'], False): self.backend.reload()
        except local.ERRORS: pass
        record['code'] = code; self.save(record, 'attention_required')
        return {'result': 'attention_required', 'code': code, 'start_not_repeated': True,
                'worker_exit_verified': False, 'explicit_stop_required': True,
                'fresh_evaluation_verified': False}

    def apply(self, proposal):
        record = None
        try:
            # The management lock is retained across submission; release only
            # the inherited worker lease before asking the manager to start.
            with ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
                with ws.automatic_lease(self.directory):
                    with change.change_lock(self.directory):
                        fresh = self.plan(proposal['wait_seconds'])
                        require(str(uuid.UUID(proposal['id'])) == proposal['id'], 'BOOT_START_PROPOSAL_CHANGED')
                        fresh['id'] = proposal['id']
                        require(fresh == proposal, 'BOOT_START_PROPOSAL_CHANGED')
                        verified = self.readback(self.directory, proposal['manifest'], lambda: self.guard(proposal, False))
                        require(verified == {'result': 'passed', 'live_checks_performed': True,
                                            'configuration_written': False, 'restart_requested': False}, 'BOOT_LIVE_READBACK_REQUIRED')
                        self.guard(proposal, False)
                        require(self.store.optional('status.json') == proposal['prior_status']
                                and self.store.optional('boot.json') is None, 'BOOT_START_PRIOR_RECORD_CHANGED')
                        record = deepcopy(proposal); self.save(record, 'prepared')
                        current = service.owned(record['manifest'], self.backend)
                        if current['ActiveState'] == 'failed': self.backend.reset_failed()
                        self.guard(record, False)
                        with self.store.lock():
                            require(self.store.control() == record['control'], 'BOOT_CONTROL_CHANGED')
                            self.store.status(phase='service_starting', completed_cycles=0, boot_start_id=record['id'])
                            running = dict(record['control'], desired='running')
                            running.pop('boot_exit_request', None)
                            receipt = {'schema': 1, 'phase': 'armed', 'generation': boot.policy(record['manifest'])['generation'],
                                'manifest_sha256': boot.fingerprint(record['manifest']),
                                'control_sha256': boot.fingerprint(running),
                                'status_sha256': boot.fingerprint(self.store.optional('status.json')),
                                'identity': self.identity(), 'boot_start_id': record['id']}
                            require(receipt['identity']['boot_id'] == record['boot_id'], 'BOOT_START_BOOT_CHANGED')
                            ws.write(self.store.path / 'boot.json', receipt)
                        record['receipt_sha256'] = boot.fingerprint(receipt); self.save(record, 'armed')
                        self.guard(record, False)
                        service.set_enabled(record['manifest'], True); self.hook('enable_link')
                        self.backend.reload(); self.guard(record, True)
                        self.save(record, 'start_requested')
                        with self.store.lock():
                            require(self.store.control() == record['control'], 'BOOT_CONTROL_CHANGED')
                            require(self.store.optional('boot.json') == receipt, 'BOOT_RECEIPT_CHANGED')
                            ws.write(self.store.path / 'control.json', running)
                        self.hook('running_released')
                # Stop persists before acquiring management lock. Never clear
                # that intent if it arrives between release and submission.
                with self.store.lock():
                    require(self.store.control() == running, 'BOOT_CONTROL_CHANGED')
                service.owned(record['manifest'], self.backend)
                self.backend.start(); self.hook('start_returned')
                self.save(record, 'submitted')
            return self.check()
        except local.ERRORS as exc:
            if record is not None and self.path.exists(): return self.attention(record, local.error_code(exc))
            raise

    def check(self):
        """Observe a prior request without resubmission or automatic recovery."""
        with ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
            return self._check()

    def _check(self):
        record = self.load()
        require(record['phase'] in ('start_requested', 'submitted', 'accepted'), 'BOOT_START_CHECK_REFUSED')
        manifest = record['manifest']
        parent = self
        class Controller:
            def manifest(self):
                transition, current = parent.committed()
                require(boot.fingerprint(transition) == record['transition_sha256'], 'BOOT_START_RECORD_CHANGED')
                require(parent.identity()['boot_id'] == record['boot_id'], 'BOOT_START_BOOT_CHANGED')
                return current
            def status(self):
                parent.environment(parent.directory, manifest)
                require(service.enabled(manifest), 'BOOT_OWNED_ENABLE_REQUIRED')
                state = parent.status_fn(parent.directory, parent.backend)
                require(parent.store.control().get('service_stop_token') == record['control']['service_stop_token'],
                        'SERVICE_STOP_REQUEST_CHANGED')
                current = state['provider_status']; invocation = current.get('InvocationID')
                if current['ActiveState'] in ('active', 'activating') and invocation:
                    require(invocation != record['prior_service_invocation'], 'BOOT_MANAGER_INVOCATION_REUSED')
                if state.get('current_invocation_verified') and state.get('evaluation_status') == 'healthy':
                    saved = parent.store.optional('boot.json') or {}
                    latest = state['worker']['last_observation']
                    require(saved.get('phase') == 'entered' and saved.get('previous_receipt_sha256') == record['receipt_sha256']
                            and saved.get('manifest_sha256') == boot.fingerprint(manifest)
                            and saved.get('generation') == boot.policy(manifest)['generation']
                            and saved.get('service_invocation') == invocation
                            and saved.get('invocation') == latest.get('invocation')
                            and saved.get('identity') == latest.get('identity'), 'BOOT_ENTRY_EVIDENCE_INVALID')
                return state
        result = {'schema': 1, 'stage': 'O04-staged-initial-boot-start',
                  'start_request_submitted': True, 'fresh_evaluation_verified': False,
                  'linger_changed': False, 'logout_or_boot_acceptance': False,
                  'production_cli_available': False}
        before = {'provider_status': {'ActiveState': 'inactive', 'InvocationID': record['prior_service_invocation']}}
        try:
            observation.observe(Controller(), before, manifest, result, record['wait_seconds'], self.clock, self.sleep)
            require(self.transition.snapshot(self.directory) == manifest['binding'], 'SERVICE_BINDING_CHANGED')
            # Stop can arrive during final readback; success needs a current
            # accepted observation after the binding verification as well.
            final = Controller().status()
            require(final.get('desired_state') == 'running' and final.get('recovery') is None
                    and final.get('current_invocation_verified') is True
                    and final.get('evaluation_status') == 'healthy'
                    and final['provider_status']['InvocationID'] == result['management']['provider_status']['InvocationID'],
                    'BOOT_FINAL_OBSERVATION_CHANGED')
            record['accepted_service_invocation'] = final['provider_status']['InvocationID']
            record['acceptance'] = observation.projection(final)
            with self.store.lock():
                control = self.store.control()
                require(control['desired'] == 'running' and control['recovery'] is None
                        and control.get('service_stop_token') == record['control']['service_stop_token'], 'BOOT_CONTROL_CHANGED')
                self.save(record, 'accepted')
            result['management'] = record['acceptance']
            result.update(next_step='isolated_native_unit_docker_acceptance', same_boot_initial_start_verified=True,
                          actual_reboot_verified=False)
            return result
        except local.ERRORS as exc: return self.attention(record, local.error_code(exc))

    def cancel(self, stop_fn=None):
        """Explicit stop/disable, preserving all code, policy and failure evidence."""
        record = self.load()
        require(record['phase'] != 'cancelled', 'BOOT_START_ALREADY_CANCELLED')
        # Persist user stop intent first, including when manager is unavailable.
        service.request_pause(self.directory)
        with ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
            self.save(record, 'cancelling')
        result = (stop_fn or (lambda: service.run('stop', self.directory, backend=self.backend)))()
        require(result.get('result') == 'passed' and result.get('worker_exit_verified') is True, 'BOOT_CANCEL_EXIT_UNCONFIRMED')
        with ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
            with ws.automatic_lease(self.directory):
                current = service.owned(record['manifest'], self.backend)
                require(service.inactive(current) and self.backend.cgroup_empty(current)
                        and not service.enabled(record['manifest'])
                        and self.store.control()['desired'] == 'paused', 'BOOT_CANCEL_EXIT_UNCONFIRMED')
                self.save(record, 'cancelled')
        return {'result': 'passed', 'worker_exit_verified': True, 'desired_state': 'paused',
                'autostart_enabled': False, 'recovery_and_audit_preserved': True}
