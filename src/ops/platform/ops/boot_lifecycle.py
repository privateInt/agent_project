"""Boot handoff and bounded metadata readiness. Policy/code migration is separate.

An exit signal is not evidence of shutdown. Only a different boot, a matching
clean handoff, owned enabled policy, unchanged stop token and no pending work
permit automatic entry. This module never enables linger or starts Docker/Ops.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import uuid

from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.ops import lifecycle_probe as probe, lifecycle_state as local, manage
from src.ops.platform.ops import refresh_discovery as refresh, set_public_url as change, worker_state as ws


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def policy(manifest):
    value = manifest.get('boot_contract')
    require(manifest.get('persistence') == 'boot' and isinstance(value, dict)
            and set(value) == {'schema', 'generation', 'startup_seconds'}, 'BOOT_CONTRACT_REQUIRED')
    require(value['schema'] == 1 and type(value['startup_seconds']) is int
            and 1 <= value['startup_seconds'] <= 300, 'BOOT_CONTRACT_INVALID')
    try: valid = str(uuid.UUID(value['generation'])) == value['generation']
    except (ValueError, TypeError, AttributeError): valid = False
    require(valid, 'BOOT_CONTRACT_INVALID')
    return value


class Budget:
    def __init__(self, seconds, stopped, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep, self.stopped = clock, sleep, stopped
        self.started = clock(); self.until = self.started + seconds
        self.attempts = 0; self.last_delay = None

    def remaining(self):
        require(not self.stopped(), 'BOOT_STARTUP_INTERRUPTED')
        value = self.until - self.clock()
        require(value > 0, 'BOOT_STARTUP_DEADLINE')
        return value

    def runner(self, runner):
        def invoke(args, **kwargs):
            kwargs['timeout'] = min(kwargs.get('timeout', 5), self.remaining())
            result = runner(args, **kwargs)
            self.remaining()
            return result
        return invoke

    def wait(self, delay):
        until = min(self.until, self.clock() + delay)
        while self.clock() < until:
            self.sleep(min(.1, until - self.clock(), self.remaining()))
        self.remaining()


class NotReady(ReadFailure):
    pass


def socket_ready(endpoint, seconds):
    path = Path(endpoint[len('unix://'):])
    try:
        require(stat.S_ISSOCK(path.lstat().st_mode), 'BOOT_DOCKER_SOCKET_INVALID')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(min(1, seconds)); connection.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        raise NotReady('BOOT_DOCKER_SOCKET_WAIT') from None
    except (PermissionError, socket.timeout):
        raise ReadFailure('BOOT_DOCKER_SOCKET_UNAVAILABLE') from None


def snapshot(directory, binding, budget, reader_factory=probe.Reader, connect=socket_ready):
    from src.ops.platform.ops import refresh_worker as worker
    owner = ws.read(directory / 'owner.json')
    require(owner == binding['owner'] and owner.get('project_root') == str(worker.ROOT)
            and owner.get('uid') == os.getuid() and owner.get('gid') == os.getgid(), 'OWNER_MISMATCH')
    require(str(Path(sys.executable).resolve()) == binding['python']
            and list(sys.version_info[:3]) == binding['python_version']
            and worker.code_hash(worker.ROOT) == binding['code_sha256'], 'WORKER_BINDING_CHANGED')
    cfg, checks = local.config(directory, owner)
    require(all(row['status'] != 'error' for row in checks), 'WORKER_REFERENCE_INVALID')
    reader = reader_factory(); reader.runner = budget.runner(reader.runner)
    require(reader.paths['docker'] == binding['docker_path'], 'WORKER_BINDING_CHANGED')
    context = reader.original.get('DOCKER_CONTEXT')
    endpoint = reader.original.get('DOCKER_HOST') if not context else None
    if not endpoint:
        context = context or reader.query('docker', ['context', 'show'])
        endpoint = reader.query('docker', ['context', 'inspect', context, '--format', probe.CONTEXT])
    endpoint = probe.local_endpoint(endpoint)
    connect(endpoint, budget.remaining())
    reader.original['DOCKER_HOST'] = endpoint; reader.original.pop('DOCKER_CONTEXT', None)
    result = probe.docker(reader, directory, cfg, owner)
    require(result['daemon_id'] == binding['daemon_id'], 'WORKER_BINDING_CHANGED')
    ops = result['ops']
    require(ops.get('candidate_count') == 1 and ops.get('owner_and_mounts_verified') is True,
            ops.get('code', 'BOOT_OPS_IDENTITY_UNVERIFIED'))
    require(ops['container_id'] == binding['ops_container_id'] and ops['image_id'] == binding['ops_image_id'],
            'WORKER_BINDING_CHANGED')
    if ops.get('health') == 'unhealthy': raise ReadFailure('BOOT_OPS_UNHEALTHY')
    if not ops['running'] or ops.get('health') == 'starting': raise NotReady('BOOT_OPS_STARTING')
    require(ops.get('health') == 'healthy', 'BOOT_OPS_HEALTH_UNVERIFIED')
    budget.remaining()
    return binding


def local_policy(directory, manifest):
    from src.ops.platform.ops import service_lifecycle as service
    require(service.load(directory) == manifest, 'BOOT_POLICY_CHANGED')
    require(service.unit_bytes(Path(manifest['unit_path'])) == service.render(manifest), 'SERVICE_UNIT_CHANGED')
    require(service.enabled(manifest), 'BOOT_OWNED_ENABLE_REQUIRED')


def manager_policy(directory, manifest, invocation, identity, budget):
    from src.ops.platform.ops import service_lifecycle as service
    from src.ops.platform.ops.service_systemd import Systemd
    local_policy(directory, manifest)
    reader = probe.Reader(); reader.runner = budget.runner(reader.runner)
    report = probe.systemd(reader, manifest['unit_name'])
    budget.remaining()
    require(report.get('status') == 'ok' and report.get('linger') is True, 'BOOT_MANAGER_REQUIRED')
    backend = Systemd(reader, report, manifest['unit_name'])
    value = service.owned(manifest, backend)
    require(value['ActiveState'] in ('active', 'activating') and value['InvocationID'] == invocation
            and value['MainPID'] == str(identity['proc_pid']) and value.get('UnitFileState') == 'enabled',
            'BOOT_MANAGER_IDENTITY_MISMATCH')


def no_pending(directory):
    require(not refresh.lineage(directory)['pending'], 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')


class Context:
    """Called only inside worker.run's inherited singleton lease.

    The future transition adapter must produce the explicit 'armed' receipt
    while paused, prove exit, then deliberately release the same stop token.
    This class cannot arm itself or change persistence/bindings.
    """
    def __init__(self, directory, manifest, *, policy_check=local_policy,
                 manager_check=manager_policy, pending_check=no_pending, snapshot_fn=snapshot,
                 clock=time.monotonic, sleep=time.sleep):
        self.directory, self.manifest = directory, manifest
        self.contract = policy(manifest); self.binding = manifest['binding']
        self.policy_check, self.manager_check = policy_check, manager_check
        self.pending_check, self.snapshot_fn = pending_check, snapshot_fn
        self.clock, self.sleep = clock, sleep
        self.entry = None; self.token = None

    def guards(self, store, allow_exit=False):
        control = store.control()
        require(control['desired'] == 'running', 'WORKER_PAUSED')
        require(control['recovery'] is None, 'WORKER_RECOVERY_REQUIRED')
        require(control.get('service_stop_token') == self.token, 'SERVICE_STOP_REQUEST_CHANGED')
        self.policy_check(self.directory, self.manifest)
        require(store.manifest()['binding'] == self.binding, 'SERVICE_BINDING_CHANGED')
        require(allow_exit or not control.get('boot_exit_request'), 'WORKER_EXIT_REQUESTED')
        intent = store.optional('intent.json')
        require(not intent or intent.get('phase') == 'completed', 'WORKER_UNFINISHED_INTENT')
        return control

    def enter(self, store, identity, invocation, service_invocation, stopped):
        require(re.fullmatch('[0-9a-f]{32}', service_invocation or '') is not None, 'SERVICE_MANAGER_INVOCATION_REQUIRED')
        budget = Budget(self.contract['startup_seconds'],
                        lambda: stopped() or store.control()['desired'] == 'paused', self.clock, self.sleep)
        self.token = store.control().get('service_stop_token')
        with store.lock(): self.guards(store, allow_exit=True)
        self.manager_check(self.directory, self.manifest, service_invocation, identity, budget)
        with change.change_lock(self.directory):
            self.pending_check(self.directory)
            with store.lock():
                control = self.guards(store, allow_exit=True)
                handoff = store.optional('boot.json'); prior = store.optional('status.json')
                require(isinstance(handoff, dict) and handoff.get('schema') == 1
                        and handoff.get('phase') in ('armed', 'clean_exit'), 'BOOT_HANDOFF_REQUIRED')
                require(handoff.get('generation') == self.contract['generation']
                        and handoff.get('manifest_sha256') == fingerprint(self.manifest)
                        and handoff.get('control_sha256') == fingerprint(control)
                        and handoff.get('status_sha256') == fingerprint(prior), 'BOOT_HANDOFF_MISMATCH')
                require(isinstance(handoff.get('identity'), dict) and handoff['identity'].get('boot_id'), 'BOOT_HANDOFF_INVALID')
                if handoff['phase'] == 'armed':
                    require(prior and prior.get('phase') == 'service_starting'
                            and handoff['identity']['boot_id'] == identity['boot_id'], 'BOOT_EXPLICIT_START_REQUIRED')
                else:
                    require(prior and prior.get('phase') == 'boot_clean_exit'
                            and prior.get('identity') == handoff['identity']
                            and handoff.get('child_process_group_stopped') is True
                            and prior.get('service_invocation') == handoff.get('service_invocation'), 'BOOT_HANDOFF_INVALID')
                    require(handoff['identity']['boot_id'] != identity['boot_id'], 'BOOT_SAME_BOOT_REENTRY_REFUSED')
                    require(handoff.get('service_invocation') != service_invocation, 'BOOT_MANAGER_INVOCATION_REUSED')
                self.entry = {'schema':1, 'phase':'entered', 'generation':self.contract['generation'],
                    'manifest_sha256':fingerprint(self.manifest), 'identity':identity,
                    'invocation':invocation, 'service_invocation':service_invocation,
                    'previous_receipt_sha256':fingerprint(handoff), 'entered_at':worker_now()}
                # Consume first. Interruption before status/control writes cannot replay the old receipt.
                ws.write(store.path / 'boot.json', self.entry)
                control.pop('boot_exit_request', None); ws.write(store.path / 'control.json', control)
                store.status(phase='boot_starting', identity=identity, invocation=invocation,
                             service_invocation=service_invocation, completed_cycles=0)
        try:
            delays = (1, 2, 5, 10)
            while True:
                budget.remaining(); budget.attempts += 1
                with change.change_lock(self.directory):
                    self.pending_check(self.directory)
                    with store.lock(): self.guards(store)
                    try: value = self.snapshot_fn(self.directory, self.binding, budget)
                    except NotReady as exc:
                        require(exc.code in ('BOOT_DOCKER_SOCKET_WAIT', 'BOOT_OPS_STARTING'), 'BOOT_DELAY_REASON_INVALID')
                        budget.last_delay = exc.code
                    else:
                        require(value == self.binding, 'WORKER_BINDING_CHANGED')
                        self.manager_check(self.directory, self.manifest, service_invocation, identity, budget)
                        self.pending_check(self.directory)
                        with store.lock(): self.guards(store)
                        budget.remaining()
                        return {'attempts':budget.attempts, 'elapsed_seconds':round(self.clock()-budget.started,3),
                                'last_delay':budget.last_delay, 'budget_seconds':self.contract['startup_seconds']}
                budget.wait(delays[min(budget.attempts-1, len(delays)-1)])
        except BaseException as exc:
            ws.write(store.path / 'boot-readiness.json', {'schema':1, 'result':'attention_required',
                'code':local.error_code(exc), 'attempts':budget.attempts, 'last_delay':budget.last_delay,
                'budget_seconds':self.contract['startup_seconds'], 'elapsed_seconds':round(self.clock()-budget.started,3)})
            raise

    def finish(self, store, identity, invocation, service_invocation, count, outcome, observations):
        require(self.entry is not None and outcome is not None
                and outcome.get('child_process_group_stopped') is True, 'BOOT_CHILD_EXIT_UNVERIFIED')
        with change.change_lock(self.directory):
            self.pending_check(self.directory)
            with store.lock():
                if store.control()['desired'] == 'paused': return False
                control = self.guards(store, allow_exit=True)
                require(store.optional('boot.json') == self.entry, 'BOOT_RECEIPT_CHANGED')
                store.status(phase='boot_clean_exit', identity=identity, invocation=invocation,
                             completed_cycles=count, outcome=outcome, **observations)
                handoff = dict(self.entry, phase='clean_exit', control_sha256=fingerprint(control),
                    status_sha256=fingerprint(store.optional('status.json')), completed_cycles=count,
                    child_process_group_stopped=True, finished_at=worker_now(),
                    termination_cause='signal_without_shutdown_attribution')
                ws.write(store.path / 'boot.json', handoff)
                return True


def worker_now():
    from src.ops.platform.ops.restart_check import now
    return now()
