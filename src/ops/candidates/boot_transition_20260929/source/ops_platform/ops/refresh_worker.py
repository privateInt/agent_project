"""Guarded Linux foreground core. Service installation/production CLI are separate.

Only run from a single-threaded owner process. Each evaluation gets its own
process group, inherited singleton lease and independent wall-clock deadline.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import activate_discovery as activation, manage, refresh_discovery as refresh
from ops_platform.ops import lifecycle_state as local, lifecycle_probe as probe, set_public_url as change
from ops_platform.ops import worker_state as ws
from ops_platform.ops.discovery_compose import ROLES

STAGE = 'O04-guarded-refresh-worker'
ROOT = Path(__file__).resolve().parents[2]
BUSY = {'PUBLIC_URL_CHANGE_BUSY', 'CACHE_BUSY', 'WORKER_CONTROL_BUSY'}


def code_hash(root):
    # Conservative dependency closure: all Ops and observation host Python code.
    # Documentation and tests never force a running worker to reload itself.
    paths = [p for folder in ('ops_platform', 'infra/observability/ops', 'infra/langfuse/ops')
             for p in (root / folder).rglob('*.py') if 'tests' not in p.relative_to(root).parts]
    require(bool(paths) and len(paths) <= 512, 'WORKER_CODE_SET_INVALID')
    digest = hashlib.sha256()
    for path in sorted(paths):
        manage.safe(path); raw = path.read_bytes()
        require(len(raw) <= 2*1024*1024, 'WORKER_CODE_FILE_LIMIT')
        digest.update(str(path.relative_to(root)).encode() + b'\0' + hashlib.sha256(raw).digest())
    return digest.hexdigest()


def runtime_snapshot(directory):
    """Called under the prepared-state lock; no credential contents loaded here."""
    owner = ws.read(directory / 'owner.json')
    require(owner.get('schema') == 1 and owner.get('project_root') == str(ROOT)
            and owner.get('uid') == os.getuid() and owner.get('gid') == os.getgid(), 'OWNER_MISMATCH')
    require(str(uuid.UUID(owner['owner'])) == owner['owner'], 'OWNER_MISMATCH')
    cfg, checks = local.config(directory, owner)
    require(all(row['status'] != 'error' for row in checks), 'WORKER_REFERENCE_INVALID')
    reader = probe.Reader()
    docker = probe.docker(reader, directory, cfg, owner)
    require(docker.get('status') == 'ok' and docker['ops']['status'] == 'ok', 'WORKER_DOCKER_NOT_READY')
    return {'directory': str(directory), 'owner': owner, 'code_sha256': code_hash(ROOT),
            'python': str(Path(sys.executable).resolve()), 'python_version': list(sys.version_info[:3]),
            'docker_path': reader.paths['docker'], 'daemon_id': docker['daemon_id'],
            'ops_container_id': docker['ops']['container_id'], 'ops_image_id': docker['ops']['image_id']}


def transient(code, http_status=None):
    return (code in ('TIMEOUT', 'CONNECTION_FAILED') or
            code == 'UPSTREAM_HTTP_ERROR' and type(http_status) is int and 500 <= http_status <= 599)


def transient_discovery(report):
    """Unknown, mixed, auth, ambiguous and incomplete failures are never retried."""
    if report.get('deployment_metadata_stable') is not True or report.get('error'): return False
    services = report.get('services')
    if not isinstance(services, dict) or set(services) != set(ROLES): return False
    failures = []
    for row in services.values():
        if row.get('status') == 'verified': continue
        if not transient(row.get('error'), row.get('http_status')): return False
        failures.append(row)
    metrics = report.get('metrics')
    if metrics is None:
        if services['prometheus'].get('status') == 'verified': return False
    elif metrics.get('status') != 'verified':
        if not transient(metrics.get('error'), metrics.get('http_status')): return False
        failures.append(metrics)
    return bool(failures)


class TransientDiscovery(ReadFailure):
    def __init__(self): super().__init__('WORKER_DISCOVERY_TRANSIENT')


class Guard:
    def __init__(self, store, binding, snapshot_fn, evaluation):
        self.store, self.binding, self.snapshot_fn, self.evaluation = store, binding, snapshot_fn, evaluation
        self.written = False

    def checkpoint(self):
        with self.store.lock(): self.store.gate()
        require(self.snapshot_fn(self.store.directory) == self.binding, 'WORKER_BINDING_CHANGED')

    def enter(self, context):
        require(not context['pending'], 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
        require(context['next'] <= refresh.MAX_GENERATIONS, 'REFRESH_HISTORY_LIMIT')
        self.checkpoint()

    def discovery(self, function):
        def invoke(cfg):
            try: report = function(cfg)
            except ReadFailure as exc:
                if transient(exc.code, exc.http_status): raise TransientDiscovery() from None
                raise
            if report.get('result') != 'discovered' and transient_discovery(report):
                raise TransientDiscovery()
            return report
        return invoke

    def before_write(self, record):
        self.checkpoint()
        with self.store.lock():
            # Linearization point: a pause written first prevents this intent.
            self.store.gate()
            ws.write(self.store.path / 'intent.json', {'schema': 1, 'phase': 'open',
                'evaluation': self.evaluation, 'generation': record['baseline']['refresh_generation'],
                'baseline_sha256': record['baseline_sha256'], 'created_at': activation.rc.now(),
                'prewrite_files': record['baseline']['files'],
                'prewrite_evidence_sha256': change.digest(record['baseline'].get('additional_evidence', {})),
                'container_before': record['baseline']['container']})
            self.written = True

    def complete(self, report):
        require(report.get('result') == 'passed', 'WORKER_ACCEPTANCE_REQUIRED')
        with self.store.lock():
            intent = self.store.optional('intent.json')
            require(intent and intent['phase'] == 'open' and intent['evaluation'] == self.evaluation
                    and intent['generation'] == report['generation'], 'WORKER_INTENT_MISMATCH')
            intent.update(phase='completed', completed_at=activation.rc.now())
            ws.write(self.store.path / 'intent.json', intent)


def evaluate(store, binding, snapshot_fn, evaluation, options=None):
    """One real refresh; callers must own the automatic lease."""
    guard = Guard(store, binding, snapshot_fn, evaluation)
    kwargs = dict(options or {})
    kwargs['discover_fn'] = guard.discovery(kwargs.get('discover_fn', activation.discover_now))
    try:
        report = refresh.run('apply', store.directory, automatic_guard=guard, **kwargs)
        return {'result': 'passed', 'classification': 'accepted', 'generation': report['generation'],
                'configuration_changed': report['configuration_changed'], 'restart_requested': report['restart_requested']}
    except local.ERRORS as exc:
        code = local.error_code(exc)
        intent = store.optional('intent.json')
        uncertain = bool(intent and intent.get('evaluation') == evaluation and intent.get('phase') != 'completed')
        if uncertain: category = 'recovery_required'
        elif isinstance(exc, TransientDiscovery) or code in BUSY: category = 'retryable_prewrite'
        elif code in ('WORKER_PAUSED', 'WORKER_EXIT_REQUESTED'): category = 'stopped'
        else: category = 'blocked'
        return {'result': 'attention_required', 'classification': category, 'code': code}


def process_identity():
    # Observation only. No code ever signals a PID read from a persisted record.
    raw = Path('/proc/self/stat').read_text()
    return {'pid': os.getpid(), 'proc_pid': int(raw.split(' ', 1)[0]),
            'start_ticks': raw.rsplit(')', 1)[1].split()[19],
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def _kill_group(pid):
    try: os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError: pass


def _active_group(pid):
    for path in Path('/proc').glob('[0-9]*/stat'):
        try: fields = path.read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError): continue
        # Zombies have stopped; the direct child is deliberately kept unreaped.
        if int(fields[2]) == pid and fields[0] != 'Z': return True
    return False


def supervise(store, binding, snapshot_fn, evaluation, deadline_seconds, stopped, evaluate_fn=evaluate,
              boot_exit=False):
    """Pin the unreaped child PID until all group signals have been delivered."""
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        try:
            os.setsid()
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            # Survives a lost supervisor: the child also enforces the deadline.
            signal.signal(signal.SIGALRM, lambda *_: _kill_group(os.getpid()))
            signal.setitimer(signal.ITIMER_REAL, deadline_seconds)
            os.write(ready_write, json.dumps(process_identity()).encode()); os.close(ready_write)
            with open(os.devnull, 'wb') as null:
                os.dup2(null.fileno(), 1); os.dup2(null.fileno(), 2)
            result = evaluate_fn(store, binding, snapshot_fn, evaluation)
            require(isinstance(result, dict), 'WORKER_RESULT_INVALID')
            ws.write(store.path / 'result.json', {'evaluation': evaluation, 'outcome': result})
            os._exit(0)
        except BaseException:
            # No traceback/body/credentials from an untrusted upstream in logs.
            os._exit(1)
    os.close(ready_write)
    end = time.monotonic() + deadline_seconds
    reason = None
    try:
        os.set_blocking(ready_read, False)
        ready = b''; child_identity = None
        while True:
            try:
                block = os.read(ready_read, 1024)
                ready += block
                require(len(ready) <= 1024, 'WORKER_CHILD_IDENTITY_INVALID')
                if ready and child_identity is None:
                    child_identity = json.loads(ready)
                    require(child_identity['pid'] == pid and os.getpgid(pid) == pid, 'WORKER_CHILD_IDENTITY_INVALID')
                    raw = Path('/proc/' + str(child_identity['proc_pid']) + '/stat').read_text()
                    fields = raw.rsplit(')', 1)[1].split()
                    require(fields[19] == child_identity['start_ticks'] and int(fields[2]) == child_identity['proc_pid'],
                            'WORKER_CHILD_IDENTITY_INVALID')
            except BlockingIOError: pass
            exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None: break
            if time.monotonic() >= end:
                reason = 'WORKER_DEADLINE_EXCEEDED'; break
            if stopped() or store.control()['desired'] == 'paused':
                # Prevent a race with before_write by persisting pause first.
                try:
                    if boot_exit and stopped():
                        with store.lock():
                            control = store.control()
                            if control['desired'] == 'running' and control['recovery'] is None:
                                control['boot_exit_request'] = evaluation
                                ws.write(store.path / 'control.json', control)
                    else: store.pause()
                except ReadFailure as exc:
                    if exc.code != 'WORKER_CONTROL_BUSY': raise
                    time.sleep(.02); continue
                intent = store.optional('intent.json')
                if not intent or intent.get('evaluation') != evaluation:
                    reason = 'WORKER_STOPPED_PREWRITE'; break
            time.sleep(.02)
    finally:
        os.close(ready_read)
        # The child is not reaped yet, so its PID/PGID cannot be reused here.
        # Also clean any CLI descendant left by an early child exit.
        _kill_group(pid)
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
        cleanup_end = time.monotonic() + 3
        while True:
            dead = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            # /proc can expose an outer PID namespace in containerized hosts.
            # The child reports its own proc PID before executing any work.
            if dead is not None and (child_identity is None or not _active_group(child_identity['proc_pid'])): break
            require(time.monotonic() < cleanup_end, 'WORKER_CHILD_CLEANUP_UNCONFIRMED')
            time.sleep(.02)
        _, exit_status = os.waitpid(pid, 0)
    result = store.optional('result.json')
    if reason is None and os.waitstatus_to_exitcode(exit_status) == 0 and result and result.get('evaluation') == evaluation:
        outcome = result['outcome']
        require(outcome.get('classification') in ('accepted', 'retryable_prewrite', 'blocked', 'recovery_required', 'stopped'),
                'WORKER_RESULT_INVALID')
        require(child_identity is not None, 'WORKER_CHILD_IDENTITY_INVALID')
        return dict(outcome, child_process_group_stopped=True)
    intent = store.optional('intent.json')
    uncertain = bool(intent and intent.get('evaluation') == evaluation and intent.get('phase') != 'completed')
    category = 'recovery_required' if uncertain else 'stopped' if reason == 'WORKER_STOPPED_PREWRITE' else 'blocked'
    return {'result': 'attention_required', 'classification': category, 'code': reason or 'WORKER_CHILD_INTERRUPTED',
            'child_process_group_stopped': True, 'daemon_operation_cancellation_verified': False}


def run(directory, interval=60, cycles=None, deadline_seconds=180, snapshot_fn=runtime_snapshot,
        evaluate_fn=evaluate, _wait=None, service_invocation=None, boot_context=None):
    """Core API; service entry supplies its manager invocation for live status."""
    require(sys.platform == 'linux' and threading.active_count() == 1, 'SINGLE_THREADED_LINUX_WORKER_REQUIRED')
    require(30 <= interval <= 3600 and 1 <= deadline_seconds <= 600
            and (cycles is None or type(cycles) is int and 1 <= cycles <= 100000), 'INVALID_WORKER_LIMIT')
    directory = manage.private(directory)
    with ws.automatic_lease(directory):
        if boot_context is None:
            with change.change_lock(directory): binding = snapshot_fn(directory)
            store = ws.Store(directory, binding)
            store.gate()
        else:
            require(boot_context.directory == directory, 'BOOT_DIRECTORY_MISMATCH')
            binding = boot_context.binding
            store = ws.Store(directory)
            require(store.manifest()['binding'] == binding, 'SERVICE_BINDING_CHANGED')
        prior = store.optional('status.json')
        if boot_context is None and prior and prior.get('phase') in ('evaluating', 'waiting', 'boot_starting', 'boot_clean_exit'):
            store.pause('WORKER_PREVIOUS_INVOCATION_INTERRUPTED')
            raise ReadFailure('WORKER_PREVIOUS_INVOCATION_INTERRUPTED')
        invocation = str(uuid.uuid4()); identity = process_identity()
        requested_stop = False
        def on_signal(*_):
            nonlocal requested_stop
            requested_stop = True
        old_signals = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
        count = 0; outcome = None
        observations = {key: (prior or {}).get(key, default) for key, default in (
            ('consecutive_failures', 0), ('last_success_at', None), ('last_evaluation_at', None))}
        if service_invocation is not None:
            require(len(service_invocation) == 32 and all(c in '0123456789abcdef' for c in service_invocation),
                    'INVALID_SERVICE_INVOCATION')
            observations['service_invocation'] = service_invocation
        try:
            if boot_context is not None:
                ready = boot_context.enter(store, identity, invocation, service_invocation, lambda: requested_stop)
                observations['boot_readiness'] = ready
            while cycles is None or count < cycles:
                if requested_stop or store.control()['desired'] == 'paused':
                    if boot_context is None or store.control()['desired'] == 'paused': store.pause()
                    break
                store.gate()
                evaluation = invocation + ':' + str(count + 1)
                store.status(phase='evaluating', invocation=invocation, identity=identity,
                             evaluation=evaluation, completed_cycles=count, deadline_seconds=deadline_seconds, **observations)
                outcome = supervise(store, binding, snapshot_fn, evaluation, deadline_seconds,
                                    lambda: requested_stop, evaluate_fn, boot_exit=boot_context is not None)
                count += 1
                observations['last_evaluation_at'] = activation.rc.now()
                if outcome['classification'] == 'accepted':
                    observations.update(consecutive_failures=0, last_success_at=observations['last_evaluation_at'])
                elif outcome['classification'] != 'stopped': observations['consecutive_failures'] += 1
                if outcome['classification'] in ('blocked', 'recovery_required'):
                    store.pause(outcome.get('code', 'WORKER_BLOCKED')); break
                if outcome['classification'] == 'stopped':
                    if not (boot_context is not None and requested_stop and store.control()['desired'] == 'running'):
                        store.pause()
                    break
                if requested_stop or store.control()['desired'] == 'paused':
                    if boot_context is None or store.control()['desired'] == 'paused': store.pause()
                    break
                if cycles is not None and count >= cycles: break
                store.status(phase='waiting', invocation=invocation, identity=identity,
                             completed_cycles=count, outcome=outcome, next_delay_seconds=interval, **observations)
                if _wait is not None: _wait(store)
                else:
                    until = time.monotonic() + interval
                    while time.monotonic() < until and not requested_stop and store.control()['desired'] == 'running':
                        time.sleep(min(.1, max(0, until - time.monotonic())))
            control = store.control()
            phase = 'recovery_required' if control['recovery'] else 'stopped' if control['desired'] == 'paused' else 'bounded_complete'
            if boot_context is not None and requested_stop and control['desired'] == 'running' and not control['recovery']:
                clean = boot_context.finish(store, identity, invocation, service_invocation, count, outcome, observations)
                phase = 'boot_clean_exit' if clean else 'stopped'
                if not clean:
                    store.status(phase=phase, invocation=invocation, identity=identity,
                                 completed_cycles=count, outcome=outcome, **observations)
            else:
                store.status(phase=phase, invocation=invocation, identity=identity,
                             completed_cycles=count, outcome=outcome, **observations)
            degraded = control['recovery'] or outcome and outcome['classification'] == 'retryable_prewrite'
            return {'stage': STAGE, 'result': 'attention_required' if degraded else 'passed',
                    'phase': phase, 'completed_cycles': count, 'last_outcome': outcome,
                    'exit_code': 2 if control['recovery'] else 0, 'worker_returned': True,
                    'service_installed': False, 'inference_requested': False}
        except BaseException as exc:
            # Unexpected local failures must not turn into an automatic retry
            # on a later invocation. Even a failed write leaves evaluating state.
            try:
                store.pause(local.error_code(exc))
                store.status(phase='recovery_required', invocation=invocation, identity=identity,
                             completed_cycles=count, code=local.error_code(exc))
            except local.ERRORS: pass
            raise
        finally:
            for sig, handler in old_signals.items(): signal.signal(sig, handler)


def pause(directory):
    """Durably request pause without waiting for the prepared transaction lock.

    This is acknowledgement of the request, not confirmation of process exit.
    """
    store = ws.Store(directory); store.manifest(); store.pause()
    return {'result': 'pause_requested', 'worker_exit_verified': False}


def acknowledge_recovery(directory, snapshot_fn=runtime_snapshot, options=None, resume_guard=None):
    """Explicit, live, read-only transaction check; no rollback, restart or start.

    Adapter may call after the operator completed the separate recovery tool.
    Configuration/code ownership cannot be rebound here.
    """
    ws.transition_gate(directory)
    with ws.automatic_lease(directory):
        store = ws.Store(directory); binding = store.manifest()['binding']
        with change.change_lock(directory):
            require(snapshot_fn(directory) == binding, 'WORKER_BINDING_CHANGED')
            context = refresh.lineage(directory)
            require(not context['pending'], 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
            require(context['next'] <= refresh.MAX_GENERATIONS, 'REFRESH_HISTORY_LIMIT')
            intent = store.optional('intent.json')
            no_record = False
            if intent and intent.get('phase') != 'completed':
                rows = [record for _, record in context['records']
                        if record['baseline']['refresh_generation'] == intent['generation']]
                if rows:
                    require(len(rows) == 1 and rows[0]['baseline_sha256'] == intent['baseline_sha256']
                            and rows[0]['phase'] in ('accepted', 'rolled_back'), 'WORKER_INTENT_RECOVERY_UNVERIFIED')
                else:
                    no_record = True
                    cfg = manage.existing(directory)
                    require(context['next'] == intent['generation'] and change.digest(context['evidence']) == intent['prewrite_evidence_sha256']
                            and activation.rc.files(directory, cfg) == intent['prewrite_files'],
                            'WORKER_INTENT_RECOVERY_UNVERIFIED')
            kwargs = dict(options or {})
            docker = kwargs.get('docker') or activation.Docker(project=manage.prepared_project(directory))
            api = kwargs.get('api_factory', activation.rc.API)
            source = kwargs.get('source_fn', activation.source_snapshot)
            record = activation.capture(directory, docker, kwargs.get('discover_fn', activation.discover_now),
                kwargs.get('validate_fn', activation.validate_effective), source, api,
                planner=lambda cfg, discovered: refresh.merge(cfg, discovered, context['fields']),
                additional_evidence=context['evidence'])
            if no_record:
                require(record['baseline']['container'] == intent['container_before'], 'WORKER_INTENT_RECOVERY_UNVERIFIED')
            # Check CURRENT settings. Discovery may propose a different value;
            # acknowledging recovery must not install or verify that proposal.
            cfg = manage.existing(directory)
            client = api(cfg); client.health(); cookie = client.login()
            try: kwargs.get('readback_fn', activation.readback)(cfg)
            finally: client.logout(cookie)
            activation.guard(directory, record['baseline'], 'original')
            change.protect(docker, record['baseline'])
            require(snapshot_fn(directory) == binding, 'WORKER_BINDING_CHANGED')
            with store.lock():
                require(store.control()['desired'] == 'paused', 'WORKER_PAUSE_REQUIRED')
                if resume_guard is not None: resume_guard(store)
                if intent and intent.get('phase') != 'completed':
                    intent.update(phase='completed', recovery_verified_at=activation.rc.now())
                    ws.write(store.path / 'intent.json', intent)
                control = store.control(); control.update(desired='running', recovery=None)
                control.pop('boot_exit_request', None)
                ws.write(store.path / 'control.json', control)
                store.status(phase='recovery_acknowledged', completed_cycles=0)
            return {'result': 'passed', 'live_checks_performed': True, 'worker_started': False,
                    'restart_requested': False, 'inference_requested': False}
