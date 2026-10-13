"""Inactive maintenance observation fix. Prior candidate/runtime stay unchanged.

Stop once, retain the shared refresh lease across a caller's scoped operation,
then require explicit return with current readback and a new boot entry. The
initial boot-start journal is never reused or changed. Providers must discover
and validate owned scope identities; protected containers include all others.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import time
import uuid

from src.ops.platform.contracts import require
from src.ops.platform.ops import boot_lifecycle as boot, lifecycle_state as local
from src.ops.platform.ops import refresh_worker as worker, service_lifecycle as service
from src.ops.platform.ops import service_start as observation, worker_state as ws
from src.ops.platform.ops import set_public_url as change
from src.ops.candidates.boot_start_20260929.arming import environment, live_readback, loaded_function

KIND = 'scoped-boot-worker-maintenance'
CODE = '2bf14042a6aa15e4e12b46aa7be494eef2b8834c9b44c4a5587610d5d3a3bb1f'
PHASES = {'prepared', 'stop_requested', 'stopped', 'operation_running', 'ready_to_return',
          'armed', 'start_requested', 'submitted', 'returned', 'attention_required'}
LIMIT = 128


def file_evidence(path):
    raw, _ = worker.activation.rc.read_file(path, 32768, private=True)
    require(path.stat().st_nlink == 1, 'MAINTENANCE_HARDLINK_REFUSED')
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'mode': path.stat().st_mode & 0o777}


def history(directory):
    context = worker.refresh.lineage(directory)
    require(not context['pending'] and context['next'] <= worker.refresh.MAX_GENERATIONS,
            'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
    # The accepted settings and provenance are evidence, never an activation.
    return boot.fingerprint({'fields': context['fields'], 'evidence': context['evidence']})


def ownership(fields):
    return {name: {key: row[key] for key in ('source','value','automatic_future_update_allowed','identity','prometheus_identity')
                   if key in row} for name, row in fields.items()}


def capture_return(directory, record):
    """Authorize only the post-maintenance metadata source, never config changes."""
    context = worker.refresh.lineage(directory)
    require(not context['pending'] and context['next'] <= worker.refresh.MAX_GENERATIONS,
            'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
    require(history(directory) == record['history_sha256'], 'MAINTENANCE_PROVENANCE_CHANGED')
    cfg = worker.manage.existing(directory)
    return {'source_sha256': worker.activation.source_snapshot(),
            'files_sha256': boot.fingerprint(worker.activation.rc.files(directory, cfg)),
            'fields_sha256': boot.fingerprint(ownership(context['fields'])),
            'evidence_sha256': change.digest(context['evidence']), 'generation': context['next']}


def check_return(directory, record, intent=None):
    """Validate at most one metadata-only generation against pre-start evidence."""
    evidence = record.get('return_evidence')
    require(isinstance(evidence, dict) and set(evidence) ==
        {'source_sha256','files_sha256','fields_sha256','evidence_sha256','generation'}, 'MAINTENANCE_RETURN_EVIDENCE_REQUIRED')
    cfg = worker.manage.existing(directory)
    require(boot.fingerprint(worker.activation.rc.files(directory, cfg)) == evidence['files_sha256'],
            'MAINTENANCE_RETURN_CONFIGURATION_CHANGED')
    context = worker.refresh.lineage(directory)
    generation = evidence['generation']
    require(type(generation) is int and 1 <= generation <= worker.refresh.MAX_GENERATIONS,
            'MAINTENANCE_RETURN_EVIDENCE_REQUIRED')
    require(boot.fingerprint(ownership(context['fields'])) == evidence['fields_sha256'], 'MAINTENANCE_PROVENANCE_CHANGED')
    prefix = dict(context['evidence']); new = None
    if len(context['records']) == generation:
        path, new = context['records'][-1]
        require(prefix.pop(str(path.relative_to(directory)), None) is not None, 'MAINTENANCE_PROVENANCE_CHANGED')
        baseline = new['baseline']; plan = baseline['plan']
        require(new['phase'] in ('prepared','accepted') and baseline['refresh_generation'] == generation
                and baseline['source_sha256'] == evidence['source_sha256']
                and change.digest(baseline['additional_evidence']) == evidence['evidence_sha256']
                and boot.fingerprint(baseline['files']) == evidence['files_sha256']
                and boot.fingerprint(ownership(plan['fields'])) == evidence['fields_sha256']
                and plan.get('deployment_metadata_changed') is True and not plan.get('changed_fields')
                and not plan.get('manual_overrides_detected') and plan.get('restart_required') is False,
                'MAINTENANCE_RETURN_GENERATION_CHANGED')
        if new['phase'] == 'accepted':
            report = new.get('report') or {}
            require(report.get('result') == 'passed' and report.get('configuration_changed') is False
                    and report.get('restart_requested') is False and report.get('generation') == generation,
                    'MAINTENANCE_RETURN_GENERATION_CHANGED')
    else:
        require(len(context['records']) == generation - 1 and not context['pending'], 'MAINTENANCE_PROVENANCE_CHANGED')
    require(change.digest(prefix) == evidence['evidence_sha256'], 'MAINTENANCE_PROVENANCE_CHANGED')
    if intent and intent.get('phase') == 'open':
        require(intent.get('schema') == 1 and intent.get('generation') == generation
                and boot.fingerprint(intent.get('prewrite_files')) == evidence['files_sha256']
                and intent.get('prewrite_evidence_sha256') == evidence['evidence_sha256']
                and isinstance(intent.get('baseline_sha256'), str) and len(intent['baseline_sha256']) == 64
                and all(c in '0123456789abcdef' for c in intent['baseline_sha256'])
                and (new is None or new['baseline_sha256'] == intent['baseline_sha256']),
                'MAINTENANCE_RETURN_INTENT_CHANGED')
    else:
        require(not context['pending'], 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
        if new is not None:
            require(intent and intent.get('phase') == 'completed' and intent.get('generation') == generation
                    and intent.get('baseline_sha256') == new['baseline_sha256'], 'MAINTENANCE_RETURN_INTENT_CHANGED')


def inventory(value):
    require(isinstance(value, dict) and set(value) == {'scope', 'protected'}, 'MAINTENANCE_SCOPE_INVALID')
    scoped, protected = value['scope'], value['protected']
    require(isinstance(scoped, dict) and 1 <= len(scoped) <= LIMIT
            and isinstance(protected, dict) and len(protected) <= LIMIT, 'MAINTENANCE_SCOPE_INVALID')
    ids = []
    for role, row in scoped.items():
        require(isinstance(role, str) and 1 <= len(role) <= 64 and isinstance(row, dict)
                and set(row) == {'container_id', 'image_id', 'mounts_sha256', 'owner'}, 'MAINTENANCE_SCOPE_INVALID')
        cid = row['container_id']; ids.append(cid)
        require(isinstance(cid, str) and len(cid) == 64 and all(c in '0123456789abcdef' for c in cid)
                and isinstance(row['image_id'], str) and row['image_id'].startswith('sha256:')
                and len(row['image_id']) == 71 and all(c in '0123456789abcdef' for c in row['image_id'][7:])
                and isinstance(row['mounts_sha256'], str) and len(row['mounts_sha256']) == 64
                and all(c in '0123456789abcdef' for c in row['mounts_sha256'])
                and isinstance(row['owner'], str) and str(uuid.UUID(row['owner'])) == row['owner'],
                'MAINTENANCE_SCOPE_INVALID')
    require(len(set(ids)) == len(ids) and not set(ids) & set(protected), 'MAINTENANCE_SCOPE_INVALID')
    require(all(isinstance(cid, str) and len(cid) == 64 and all(c in '0123456789abcdef' for c in cid)
                and isinstance(started, str) and 1 <= len(started) <= 128 for cid, started in protected.items()),
            'MAINTENANCE_SCOPE_INVALID')
    return deepcopy(value)


class Scope:
    """Operation capability valid only while its actual refresh lease is held.

    A forked storage child inherits this descriptor. Subprocess callers must
    explicitly pass it and prove child exit before returning. This API does
    not launch storage processes or infer that a callback has stopped them.
    """
    def __init__(self, parent, record, descriptor):
        self.parent, self.record, self.descriptor, self.active = parent, record, descriptor, True

    def checkpoint(self):
        require(self.active, 'MAINTENANCE_SCOPE_CLOSED')
        os.fstat(self.descriptor)
        self.parent.guard(self.record, paused=True)


class Maintenance:
    def __init__(self, directory, backend, *, inventory_fn, snapshot_fn=worker.runtime_snapshot,
                 history_fn=history, environment_fn=environment, readback_fn=live_readback,
                 identity_fn=worker.process_identity, status_fn=service.status,
                 clock=time.monotonic, sleep=time.sleep, hook=lambda phase: None,
                 return_capture_fn=capture_return, return_check_fn=check_return):
        self.directory = worker.manage.private(Path(directory))
        self.store, self.backend = ws.Store(self.directory), backend
        self.jobs = self.store.path.parent / 'maintenance'
        self.inventory, self.snapshot, self.history = inventory_fn, snapshot_fn, history_fn
        self.environment, self.readback, self.identity = environment_fn, readback_fn, identity_fn
        self.status_fn, self.clock, self.sleep, self.hook = status_fn, clock, sleep, hook
        self.durable = {}
        self.return_capture, self.return_check = return_capture_fn, return_check_fn

    def path(self, identifier):
        require(isinstance(identifier, str) and str(uuid.UUID(identifier)) == identifier, 'MAINTENANCE_ID_INVALID')
        return self.jobs / (identifier + '.json')

    def records(self):
        if not self.jobs.exists() and not self.jobs.is_symlink(): return []
        worker.manage.private(self.jobs)
        files = list(self.jobs.iterdir())
        require(len(files) < LIMIT, 'MAINTENANCE_HISTORY_LIMIT')
        records = []
        for path in files:
            require(path.suffix == '.json' and self.path(path.stem) == path, 'MAINTENANCE_UNOWNED_RECORD')
            records.append(self.load(path.stem))
        return records

    def load(self, identifier):
        file_evidence(self.path(identifier))
        value = ws.read(self.path(identifier))
        require(value.get('schema') == 1 and value.get('kind') == KIND and value.get('id') == identifier
                and value.get('directory') == str(self.directory) and value.get('phase') in PHASES,
                'MAINTENANCE_RECORD_INVALID')
        return value

    def save(self, record, phase):
        require(phase in PHASES, 'MAINTENANCE_PHASE_INVALID')
        if not self.jobs.exists(): self.jobs.mkdir(mode=0o700)
        worker.manage.private(self.jobs)
        next_record = dict(record, phase=phase, updated_at=boot.worker_now())
        ws.write(self.path(record['id']), next_record)
        record.clear(); record.update(next_record); self.durable[record['id']] = deepcopy(record)
        self.hook('journal:' + phase)

    def fixed(self, observing=False):
        manifest = service.load(self.directory)
        require(manifest['phase'] == 'installed' and manifest['persistence'] == 'boot', 'MAINTENANCE_BOOT_POLICY_REQUIRED')
        boot.policy(manifest)
        require(manifest['binding'] == self.store.manifest()['binding']
                and manifest['binding']['code_sha256'] == worker.code_hash(worker.ROOT) == CODE,
                'MAINTENANCE_BINDING_CHANGED')
        for module, names in ((boot, ('Context.enter', 'Context.guards')),
                              (worker, ('run',)), (service, ('run',))):
            for name in names: loaded_function(module, name, Path(module.__file__).read_bytes())
        ws.transition_gate(self.directory)
        intent = self.store.optional('intent.json')
        if not observing: require(not intent or intent.get('phase') == 'completed', 'WORKER_UNFINISHED_INTENT')
        self.environment(self.directory, manifest)
        service.owned(manifest, self.backend)
        evidence = {}
        for name in ('boot-start.json', 'code-transition.json'):
            path = self.store.path.parent / name
            saved = ws.read(path)
            require(saved.get('phase') == ('accepted' if name == 'boot-start.json' else 'committed')
                    and saved.get('kind') == ('initial-boot-start' if name == 'boot-start.json' else 'stopped-code-policy-transition')
                    and saved.get('directory') == str(self.directory), 'MAINTENANCE_INITIAL_HISTORY_REQUIRED')
            evidence[name] = file_evidence(path)
        return manifest, evidence

    def plan(self, wait_seconds=None):
        require(all(r['phase'] == 'returned' for r in self.records()), 'MAINTENANCE_UNRESOLVED_JOB')
        with change.change_lock(self.directory):
            manifest, fixed = self.fixed()
            require(self.snapshot(self.directory) == manifest['binding'], 'MAINTENANCE_BINDING_CHANGED')
            self.store.gate(); control = self.store.control()
            require(control.get('service_stop_token'), 'MAINTENANCE_STOP_TOKEN_REQUIRED')
            require(service.enabled(manifest), 'BOOT_OWNED_ENABLE_REQUIRED')
            state = self.status_fn(self.directory, self.backend)
            require(state.get('current_invocation_verified') is True and state.get('evaluation_status') == 'healthy'
                    and state['provider_status']['ActiveState'] == 'active', 'MAINTENANCE_HEALTHY_WORKER_REQUIRED')
            receipt = self.store.optional('boot.json') or {}
            last = state['worker']['last_observation']
            require(receipt.get('phase') == 'entered' and receipt.get('manifest_sha256') == boot.fingerprint(manifest)
                    and receipt.get('generation') == boot.policy(manifest)['generation']
                    and receipt.get('service_invocation') == state['provider_status']['InvocationID']
                    and receipt.get('invocation') == last.get('invocation')
                    and receipt.get('identity') == last.get('identity'), 'MAINTENANCE_BOOT_ENTRY_REQUIRED')
            wait = wait_seconds if wait_seconds is not None else (
                boot.policy(manifest)['startup_seconds'] + manifest['deadline_seconds'] + manifest['interval_seconds'] + 30)
            require(type(wait) is int and 1 <= wait <= 1200, 'INVALID_OBSERVATION_LIMIT')
            return {'schema': 1, 'kind': KIND, 'id': str(uuid.uuid4()), 'directory': str(self.directory),
                    'manifest': manifest, 'fixed_evidence': fixed, 'history_sha256': self.history(self.directory),
                    'inventory': inventory(self.inventory()), 'control': control,
                    'prior_receipt': receipt, 'prior_service_invocation': state['provider_status']['InvocationID'],
                    'boot_id': self.identity()['boot_id'], 'wait_seconds': wait}

    def guard(self, record, paused=False, metadata=False, enabled=None, observing=False):
        require(self.load(record['id']) == record, 'MAINTENANCE_RECORD_CHANGED')
        require(not observing or (record['phase'] in ('start_requested','submitted')
                and enabled is True and not paused and record.get('return_evidence')), 'MAINTENANCE_OBSERVATION_SCOPE_INVALID')
        manifest, fixed = self.fixed(observing=observing)
        require(manifest == record['manifest'] and fixed == record['fixed_evidence'], 'MAINTENANCE_FIXED_HISTORY_CHANGED')
        require(self.identity()['boot_id'] == record['boot_id'], 'MAINTENANCE_BOOT_CHANGED')
        if not observing: require(self.history(self.directory) == record['history_sha256'], 'MAINTENANCE_PROVENANCE_CHANGED')
        require(inventory(self.inventory()) == record['inventory'], 'MAINTENANCE_CONTAINER_SCOPE_CHANGED')
        if metadata: require(self.snapshot(self.directory) == manifest['binding'], 'MAINTENANCE_BINDING_CHANGED')
        control = self.store.control()
        require(control.get('service_stop_token') == record['stop_token'] and control['recovery'] is None,
                'MAINTENANCE_STOP_OR_RECOVERY_CHANGED')
        if paused:
            require(control == record['paused_control'] and control['desired'] == 'paused', 'MAINTENANCE_CONTROL_CHANGED')
            current = service.owned(manifest, self.backend)
            require(service.inactive(current) and self.backend.cgroup_empty(current)
                    and not service.enabled(manifest), 'MAINTENANCE_WORKER_EXIT_REQUIRED')
        if enabled is not None: require(service.enabled(manifest) is enabled, 'MAINTENANCE_ENABLE_CHANGED')

    def attention(self, record, code):
        # Keep a newer pause token or existing recovery; never retry an unknown
        # stop/start and never signal a PID from a saved observation.
        with self.store.lock():
            control = self.store.control(); control['desired'] = 'paused'
            if control['recovery'] is None: control['recovery'] = code
            ws.write(self.store.path / 'control.json', control)
        try:
            service.owned(record['manifest'], self.backend)
            if service.set_enabled(record['manifest'], False): self.backend.reload()
        except local.ERRORS: pass
        persisted = self.load(record['id'])
        if persisted != self.durable.get(record['id']):
            # Pause is durable even if a journal was edited or a write's
            # durability is unknown. Never overwrite that conflicting audit.
            return {'result': 'attention_required', 'code': 'MAINTENANCE_RECORD_CHANGED',
                    'job_id': record['id'], 'desired_state': 'paused', 'explicit_recovery_required': True}
        record = deepcopy(persisted); record['code'] = code; record['failure_phase'] = record['phase']
        self.save(record, 'attention_required')
        return {'result': 'attention_required', 'code': code, 'job_id': record['id'],
                'failure_phase': record['failure_phase'],
                'desired_state': 'paused', 'return_not_repeated': True, 'explicit_recovery_required': True}

    def inspect(self, identifier):
        # Read only. A persisted ID/PID does not authorize control or replay.
        record = self.load(identifier)
        return {'result': 'passed' if record['phase'] == 'returned' else 'attention_required',
                'job_id': identifier, 'phase': record['phase'], 'historical_return_only': record['phase'] == 'returned',
                'control': self.store.control(), 'provider_status': self.backend.inspect(), 'files_written': []}

    def run(self, proposal, operation):
        """Stop once and run a caller operation under lease. Return stays paused.

        The callback returns only passed/child_processes_stopped evidence; it
        must restore the scoped services before a separate resume invocation.
        No callback is rerun from a saved job after an interruption.
        """
        record = None
        with ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
            try:
                fresh = self.plan(proposal['wait_seconds']); fresh['id'] = proposal['id']
                self.path(proposal['id'])
                require(fresh == proposal, 'MAINTENANCE_PROPOSAL_CHANGED')
                require(not self.path(proposal['id']).exists() and not self.path(proposal['id']).is_symlink(),
                        'MAINTENANCE_ID_ALREADY_RECORDED')
                record = deepcopy(proposal); record['stop_token'] = uuid.uuid4().hex
                record['prior_status'] = self.store.optional('status.json')
                control = dict(proposal['control'], desired='paused', service_stop_token=record['stop_token'],
                               pause_requested_at=boot.worker_now())
                record['paused_control'] = control
                self.save(record, 'prepared')  # Durable before pause or manager commands.
                with self.store.lock():
                    require(self.store.control() == proposal['control'], 'MAINTENANCE_CONTROL_CHANGED')
                    ws.write(self.store.path / 'control.json', control)
                self.hook('pause_written'); self.save(record, 'stop_requested')
                self.guard(record, metadata=True)
                service.set_enabled(record['manifest'], False); self.backend.reload()
                service.owned(record['manifest'], self.backend)
                self.backend.stop(); self.hook('stop_returned')
                until = self.clock() + record['manifest']['deadline_seconds'] + 30
                while True:
                    current = service.owned(record['manifest'], self.backend)
                    if service.inactive(current) and self.backend.cgroup_empty(current): break
                    require(self.clock() < until, 'MAINTENANCE_STOP_OUTCOME_UNKNOWN')
                    self.sleep(min(.1, max(0, until - self.clock())))
                with ws.automatic_lease(self.directory) as descriptor:
                    self.guard(record, paused=True)
                    record['stopped_status'] = self.store.optional('status.json')
                    self.save(record, 'stopped'); self.save(record, 'operation_running')
                    scope = Scope(self, record, descriptor)
                    try:
                        scope.checkpoint(); outcome = operation(scope); scope.checkpoint()
                    finally: scope.active = False
                    require(outcome == {'result': 'passed', 'child_processes_stopped': True},
                            'MAINTENANCE_OPERATION_EXIT_UNCONFIRMED')
                    record['operation_outcome'] = outcome; self.save(record, 'ready_to_return')
                return {'result': 'passed', 'job_id': record['id'], 'worker_exit_verified': True,
                        'shared_lease_held_during_operation': True, 'desired_state': 'paused',
                        'autostart_enabled': False, 'explicit_return_required': True}
            except BaseException as exc:
                if record is not None and self.path(record['id']).exists():
                    return self.attention(record, local.error_code(exc))
                raise

    def resume(self, identifier):
        """Explicit, one-shot return. A fresh receipt cannot replay old success."""
        record = self.load(identifier)
        require(record['phase'] == 'ready_to_return', 'MAINTENANCE_RETURN_REFUSED')
        self.durable[identifier] = deepcopy(record)
        with ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
            try:
                with ws.automatic_lease(self.directory):
                    with change.change_lock(self.directory):
                        self.guard(record, paused=True, metadata=True)
                        verified = self.readback(self.directory, record['manifest'],
                            lambda: self.guard(record, paused=True, metadata=True))
                        require(verified == {'result': 'passed', 'live_checks_performed': True,
                                            'configuration_written': False, 'restart_requested': False},
                                'MAINTENANCE_LIVE_READBACK_REQUIRED')
                        self.guard(record, paused=True, metadata=True)
                        require(self.store.optional('status.json') == record['stopped_status']
                                and self.store.optional('boot.json') == record['prior_receipt'],
                                'MAINTENANCE_PRIOR_RECORD_CHANGED')
                        record['return_evidence'] = self.return_capture(self.directory, record)
                        current = service.owned(record['manifest'], self.backend)
                        if current['ActiveState'] == 'failed': self.backend.reset_failed()
                        with self.store.lock():
                            require(self.store.control() == record['paused_control'], 'MAINTENANCE_CONTROL_CHANGED')
                            self.store.status(phase='service_starting', completed_cycles=0, maintenance_id=record['id'])
                            running = dict(record['paused_control'], desired='running'); running.pop('boot_exit_request', None)
                            receipt = {'schema': 1, 'phase': 'armed', 'generation': boot.policy(record['manifest'])['generation'],
                                'manifest_sha256': boot.fingerprint(record['manifest']), 'control_sha256': boot.fingerprint(running),
                                'status_sha256': boot.fingerprint(self.store.optional('status.json')),
                                'identity': self.identity(), 'maintenance_id': record['id']}
                            ws.write(self.store.path / 'boot.json', receipt)
                        record['receipt_sha256'] = boot.fingerprint(receipt); self.save(record, 'armed')
                        self.guard(record, paused=True, metadata=True)
                        service.set_enabled(record['manifest'], True); self.hook('enable_link')
                        self.backend.reload(); self.guard(record, metadata=True, enabled=True)
                        self.save(record, 'start_requested')  # Before releasing permission, no implicit retry.
                        with self.store.lock():
                            require(self.store.control() == record['paused_control']
                                    and self.store.optional('boot.json') == receipt, 'MAINTENANCE_CONTROL_CHANGED')
                            ws.write(self.store.path / 'control.json', running)
                        self.hook('running_released')
                require(self.store.control() == running, 'MAINTENANCE_CONTROL_CHANGED')
                service.owned(record['manifest'], self.backend)
                self.backend.start(); self.hook('start_returned'); self.save(record, 'submitted')
                return self._check(record)
            except BaseException as exc: return self.attention(record, local.error_code(exc))

    def check(self, identifier):
        """Observe an uncertain prior submission, never submit it again."""
        with ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
            record = self.load(identifier)
            require(record['phase'] in ('start_requested', 'submitted'), 'MAINTENANCE_CHECK_REFUSED')
            self.durable[identifier] = deepcopy(record)
            try: return self._check(record)
            except local.ERRORS as exc: return self.attention(record, local.error_code(exc))

    def _check(self, record):
        parent = self; manifest = record['manifest']
        class Controller:
            def manifest(self):
                parent.guard(record, enabled=True, observing=True)
                return manifest
            def status(self):
                parent.guard(record, enabled=True, observing=True)
                state = parent.status_fn(parent.directory, parent.backend)
                invocation = state['provider_status'].get('InvocationID')
                require(not invocation or invocation != record['prior_service_invocation'], 'MAINTENANCE_INVOCATION_REUSED')
                intent = state['worker'].get('intent')
                if intent and intent.get('phase') != 'completed':
                    last = state['worker'].get('last_observation') or {}
                    # ws.status reads intent before status. Re-sample once if a
                    # completion raced that read; never accept a waiting open intent.
                    if last.get('phase') != 'evaluating' and parent.store.optional('intent.json') != intent:
                        state = parent.status_fn(parent.directory, parent.backend)
                        intent = state['worker'].get('intent'); last = state['worker'].get('last_observation') or {}
                        invocation = state['provider_status'].get('InvocationID')
                    if intent and intent.get('phase') != 'completed':
                        require(intent.get('phase') == 'open' and state.get('current_invocation_verified') is True
                                and state.get('evaluation_status') == 'in_progress'
                                and state['provider_status']['ActiveState'] == 'active'
                                and invocation and invocation != record['prior_service_invocation']
                                and last.get('service_invocation') == invocation and last.get('phase') == 'evaluating'
                                and type(last.get('completed_cycles')) is int and last['completed_cycles'] >= 0
                                and last.get('deadline_seconds') == manifest['deadline_seconds']
                                and intent.get('evaluation') == last.get('evaluation') ==
                                    str(last.get('invocation')) + ':' + str(last['completed_cycles'] + 1),
                                'WORKER_UNFINISHED_INTENT')
                if state.get('current_invocation_verified'):
                    receipt = parent.store.optional('boot.json') or {}; last = state['worker']['last_observation']
                    require(receipt.get('phase') == 'entered' and receipt.get('previous_receipt_sha256') == record['receipt_sha256']
                            and receipt.get('manifest_sha256') == boot.fingerprint(manifest)
                            and receipt.get('generation') == boot.policy(manifest)['generation']
                            and receipt.get('service_invocation') == invocation and receipt.get('invocation') == last.get('invocation')
                            and receipt.get('identity') == last.get('identity'), 'MAINTENANCE_FRESH_ENTRY_REQUIRED')
                parent.return_check(parent.directory, record, intent)
                return state
        result = {'result': 'attention_required', 'job_id': record['id'], 'fresh_evaluation_verified': False}
        before = {'provider_status': {'ActiveState': 'inactive', 'InvocationID': record['prior_service_invocation']}}
        observation.observe(Controller(), before, manifest, result, record['wait_seconds'], self.clock, self.sleep)
        self.guard(record, metadata=True, enabled=True, observing=True)
        final = Controller().status()
        require(final.get('desired_state') == 'running' and final.get('recovery') is None
                and final.get('current_invocation_verified') is True and final.get('evaluation_status') == 'healthy'
                and final['provider_status']['InvocationID'] == result['management']['provider_status']['InvocationID'],
                'MAINTENANCE_FINAL_OBSERVATION_CHANGED')
        with self.store.lock():
            require(self.store.control()['desired'] == 'running'
                    and self.store.control().get('service_stop_token') == record['stop_token'], 'MAINTENANCE_CONTROL_CHANGED')
            record['acceptance'] = observation.projection(final); self.save(record, 'returned')
        return dict(result, result='passed', worker_left_running=True, same_boot_return_verified=True,
                    actual_reboot_verified=False, initial_start_record_preserved=True)
