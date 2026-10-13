#!/usr/bin/env python3
"""Read-only production maintenance scope and recovery review.

No stop/return/recovery submission, backup, application API or key contents.
A successful plan is a current metadata review, not an execution permission.
"""
import argparse
from copy import deepcopy
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

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
STAGE = 'O04-production-maintenance-review'
PINS = {
    'src/ops/tools/boot_control.py': '9943a3c35dd0d30782506f29f750ac7331def01a76909f6ade1dd9f6c0c93cab',
    'src/ops/tools/plan_backup_capacity.py': 'b3acbc5ffa39c90f980953f1391a3fdd82dbd32a8ecf51ee4eeb477990f88c44',
    'src/ops/tools/measure_backup_databases.py': '2060a3911fe8edc45423b82cd8f0b0a00b8d9c062855ed517d861ffcd747b305',
    'src/ops/candidates/maintenance_intent_20260930/manifest.json': '0b0c6c62f13fafd45643a3acda8d15b3f87c5fa5f189c30e823c93e192f291d2',
}
SPEC = 'src/ops/candidates/maintenance_intent_20260930/manifest.json'
CORE = 'src/ops/candidates/maintenance_intent_20260930/maintenance.py'
LIMIT = 128
HEX = re.compile(r'[0-9a-f]{64}')
LIVE_FORMAT = '[' + ','.join('{{json '+field+'}}' for field in
                           ('.Id', '.State.Running', '.State.StartedAt')) + ']'
HOST_FORMAT = '[' + ','.join('{{json '+field+'}}' for field in
                           ('.DockerRootDir', '.Name', '.Architecture', '.OSType', '.SecurityOptions', '.ID')) + ']'
DOCKER_OPERATIONS = {'host_info', 'running_list', 'running_inspect'}
DOCKER_REASONS = {'TEMPLATE_FUNCTION_UNAVAILABLE', 'TEMPLATE_FIELD_UNAVAILABLE',
                  'DOCKER_ACCESS_DENIED', 'DOCKER_CONNECTION_FAILED', 'CONTAINER_NOT_FOUND',
                  'COMMAND_TIMEOUT', 'CLI_UNAVAILABLE', 'RESPONSE_LIMIT', 'MULTIPLE_SIGNATURES', 'UNKNOWN'}


class Failure(Exception):
    def __init__(self, code, docker_diagnostic=None):
        self.code, self.docker_diagnostic = code, docker_diagnostic; super().__init__(code)


def require(ok, code):
    if not ok: raise Failure(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def docker_failure(operation, rc=None, stdout=b'', stderr=b'', exception=None):
    """Emit fixed labels only. Never include argv, raw text, paths or exception strings."""
    require(operation in DOCKER_OPERATIONS, 'MAINTENANCE_DIAGNOSTIC_OPERATION_INVALID')
    parts = [x.decode('utf-8','replace') if isinstance(x,bytes) else x if isinstance(x,str) else ''
             for x in (stdout,stderr)]
    text = '\n'.join(x[:32768].lower() for x in parts)
    patterns = {
        'TEMPLATE_FUNCTION_UNAVAILABLE': ('function "list" not defined',),
        'TEMPLATE_FIELD_UNAVAILABLE': ("can't evaluate field", 'map has no entry for key', 'nil pointer evaluating'),
        'DOCKER_ACCESS_DENIED': ('permission denied', 'access is denied'),
        'DOCKER_CONNECTION_FAILED': ('cannot connect to the docker daemon', 'connection refused', 'error during connect'),
        'CONTAINER_NOT_FOUND': ('no such object:', 'no such container:'),
    }
    matches = {name for name,phrases in patterns.items() if any(phrase in text for phrase in phrases)}
    code = getattr(exception,'code',None)
    # The bounded backup client uses literal exception arguments instead of a
    # .code attribute. Accept only its three known labels, never exception text.
    args = getattr(exception,'args',())
    if code is None and args and isinstance(args[0],str) and args[0] in (
            'CLIENT_DEADLINE_EXCEEDED','COMMAND_UNAVAILABLE','COMMAND_OUTPUT_LIMIT'):
        code = args[0]
    outcome, forced = {'CLIENT_DEADLINE_EXCEEDED': ('timeout','COMMAND_TIMEOUT'),
                       'COMMAND_UNAVAILABLE': ('unavailable','CLI_UNAVAILABLE'),
                       'COMMAND_OUTPUT_LIMIT': ('response_limit','RESPONSE_LIMIT')}.get(code,('failed',None))
    reason = forced or (next(iter(matches)) if len(matches)==1 else 'MULTIPLE_SIGNATURES' if matches else 'UNKNOWN')
    return dict(operation=operation,outcome=outcome,reason=reason,
                exit_code=rc if type(rc) is int and -255<=rc<=255 else None,
                text_truncated=any(len(x)>32768 for x in parts), retry_requested=False)


def safe_docker_diagnostic(value):
    if not isinstance(value,dict) or set(value)!= {'operation','outcome','reason','exit_code','text_truncated','retry_requested'}:
        return None
    if (not isinstance(value['operation'],str) or value['operation'] not in DOCKER_OPERATIONS or
        not isinstance(value['reason'],str) or value['reason'] not in DOCKER_REASONS or
        not isinstance(value['outcome'],str) or value['outcome'] not in ('failed','timeout','unavailable','response_limit') or
        type(value['text_truncated']) is not bool or value['retry_requested'] is not False or
        value['exit_code'] is not None and not (type(value['exit_code']) is int and -255<=value['exit_code']<=255)):
        return None
    return deepcopy(value)


def source(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'MAINTENANCE_SOURCE_SYMLINK')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and info.st_size <= 2*1024*1024, 'MAINTENANCE_SOURCE_UNSAFE')
        with os.fdopen(os.dup(fd), 'rb') as stream: raw = stream.read(2*1024*1024+1)
        require(len(raw) <= 2*1024*1024, 'MAINTENANCE_SOURCE_LIMIT')
        return raw
    finally: os.close(fd)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def dependencies(root):
    for rel, wanted in PINS.items():
        require(hashlib.sha256(source(root/rel)).hexdigest() == wanted, 'MAINTENANCE_REVIEWED_SOURCE_CHANGED')
    bc = load('_maintenance_boot_review', root/'src/ops/tools/boot_control.py')
    reviewed = bc.reviewed_sources(root)
    require(reviewed['code_state'] == 'candidate', 'MAINTENANCE_BOOT_CODE_REQUIRED')
    doc = json.loads(source(root/SPEC))
    for rel in (CORE, 'src/ops/candidates/boot_start_20260929/arming.py'):
        wanted = next(row['sha256'] for row in doc['auxiliary_files'] if row['path'] == rel)
        require(hashlib.sha256(source(root/rel)).hexdigest() == wanted, 'MAINTENANCE_REVIEWED_CORE_CHANGED')
    runtime = bc.dependencies(root)
    core = load('_maintenance_reviewed_core', root/CORE)
    cap = load('_maintenance_scope_inventory', root/'src/ops/tools/plan_backup_capacity.py')
    return bc, reviewed, runtime, core, cap


def docker_type(cap, probe):
    class Docker(cap.Docker):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw); self.rows = {}

        def query(self, args):
            allowed = (args == ['info', '--format', HOST_FORMAT] or args == ['ps', '-q', '--no-trunc'] or
                       (5 <= len(args) <= 4+LIMIT and args[:4] == ['container', 'inspect', '--format', LIVE_FORMAT]
                        and all(HEX.fullmatch(x) for x in args[4:])))
            require(allowed and self.base.endpoint is not None, 'MAINTENANCE_DOCKER_COMMAND_REFUSED')
            self.calls += 1
            operation = 'host_info' if args[0]=='info' else 'running_list' if args[0]=='ps' else 'running_inspect'
            try:
                rc, out, err = self.runner(['docker', '--host', self.base.endpoint, *args], self.base.env, 20)
            except Exception as exc:
                raise Failure('MAINTENANCE_DOCKER_METADATA_FAILED',docker_failure(operation,exception=exc)) from None
            if rc != 0:
                raise Failure('MAINTENANCE_DOCKER_METADATA_FAILED',docker_failure(operation,rc,out,err))
            return out.decode().strip()

        def host(self):
            require(not os.environ.get('DOCKER_API_VERSION'), 'DOCKER_API_OVERRIDE_REFUSED')
            if not os.environ.get('DOCKER_CONTEXT') and os.environ.get('DOCKER_HOST'):
                endpoint = os.environ['DOCKER_HOST']; context = 'explicit_host'
            else:
                context = os.environ.get('DOCKER_CONTEXT') or self.base._command(['context', 'show'], True)
                require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}', context), 'INVALID_DOCKER_CONTEXT')
                endpoint = self.base._command(['context', 'inspect', context, '--format', '{{.Endpoints.docker.Host}}'], True)
            self.base.endpoint = probe.local_endpoint(endpoint)
            root, name, arch, system, security, daemon = json.loads(self.query(['info', '--format', HOST_FORMAT]))
            require(system == 'linux' and isinstance(security, list)
                    and not any('rootless' in x or 'userns' in x for x in security), 'ROOTFUL_LINUX_REQUIRED')
            require(isinstance(daemon, str) and 1 <= len(daemon) <= 128, 'DOCKER_DAEMON_ID_REQUIRED')
            return dict(docker_root=str(cap.bp.safe(root)), hostname=name, architecture=arch,
                        context=context, endpoint=endpoint, daemon_id=daemon)

        def containers_for(self, component, owner):
            rows = super().containers_for(component, owner)
            self.rows[(component, owner)] = deepcopy(rows)
            return rows

        def running(self):
            ids = self.query(['ps', '-q', '--no-trunc']).split()
            require(len(ids) <= LIMIT and len(set(ids)) == len(ids)
                    and all(HEX.fullmatch(x) for x in ids), 'MAINTENANCE_RUNNING_SET_INVALID')
            if not ids: return {}
            lines = self.query(['container', 'inspect', '--format', LIVE_FORMAT, *sorted(ids)]).splitlines()
            require(len(lines) == len(ids), 'MAINTENANCE_RUNNING_SET_CHANGED')
            result = {}
            for line in lines:
                row = json.loads(line)
                require(isinstance(row, list) and len(row) == 3 and row[0] in ids and row[0] not in result
                        and row[1] is True and isinstance(row[2], str) and 1 <= len(row[2]) <= 128,
                        'MAINTENANCE_RUNNING_SET_CHANGED')
                result[row[0]] = row[2]
            return result
    return Docker


class Inventory:
    def __init__(self, root, langfuse, directory, cap, docker):
        self.root, self.langfuse, self.directory, self.cap, self.docker = root, langfuse, directory, cap, docker

    def capture(self):
        host = self.docker.host()
        running = self.docker.running()
        # Metadata only: never traverse a store or calculate capacity here.
        inv = self.cap.Inventory(self.root, self.langfuse, self.directory, self.docker, [])
        inv.host = host
        for component in self.cap.bp.PROJECTS: getattr(inv, 'load_'+component)()
        checked = inv.containers()
        require(len(checked) == len(inv.services) == 9 and all(x['status'] == 'matched' for x in checked.values()),
                'MAINTENANCE_OWNED_SCOPE_UNVERIFIED')
        require(all(x.get('status') == 'ok' or not x.get('required') for x in inv.files),
                'MAINTENANCE_CONFIGURATION_METADATA_UNAVAILABLE')
        scoped, evidence = {}, {}
        for name, service in inv.services.items():
            rows = self.docker.rows[(service['component'], service['owner'])]
            row = next(x for x in rows if x['id'] == checked[name]['id'])
            require(row['id'] in running and running[row['id']] == row['started_at'],
                    'MAINTENANCE_SCOPED_CONTAINER_CHANGED')
            mounts = sorted(row['mounts'], key=lambda x: json.dumps(x, sort_keys=True))
            scoped[name] = dict(container_id=row['id'], image_id=row['image_id'],
                                owner=row['owner'], mounts_sha256=fingerprint(mounts))
            evidence[name] = dict(component=service['component'], compose_project=row['project'],
                                  started_at=row['started_at'])
        ids = {x['container_id'] for x in scoped.values()}
        require(len(ids) == 9, 'MAINTENANCE_SCOPE_OVERLAP')
        files = {x['path']: x['metadata_signature'] for x in inv.files
                 if x.get('metadata_signature') and x['path'] != str(self.directory/'cache/api-success.json')}
        return dict(inventory={'scope': scoped, 'protected': {k:v for k,v in running.items() if k not in ids}},
                    evidence=evidence, files=files,
                    host={k:host[k] for k in ('context','endpoint','daemon_id')})


def read_only_type(adapter):
    class ReadOnly(adapter):
        def call(self, args, control=False, accept=()):
            require(not control, 'MAINTENANCE_CONTROL_NOT_AVAILABLE')
            return super().call(args, control=False, accept=accept)

        def verify(self, manifest):
            # Offline verification creates a temporary unit; it is unnecessary
            # when reviewing the already installed, byte-verified owned unit.
            raise Failure('MAINTENANCE_CONTROL_NOT_AVAILABLE')
    return ReadOnly


class Review:
    def __init__(self, directory, langfuse, root=ROOT):
        self.root = root
        self.bc, self.sources, self.r, self.core, self.cap = dependencies(root)
        self.directory = self.r.manage.private(self.r.manage.safe(directory))
        owner = self.r.ws.read(self.directory/'owner.json')
        require(owner.get('project_root') == str(root) and owner.get('uid') == os.getuid()
                and owner.get('gid') == os.getgid(), 'MAINTENANCE_INSTALLATION_OWNER_MISMATCH')
        self.reader = self.r.probe.Reader()
        self.manifest = self.r.service.load(self.directory)
        self.systemd = self.r.probe.systemd(self.reader, self.manifest['unit_name'])
        require(self.systemd.get('status') == 'ok', 'SYSTEMD_USER_MANAGER_UNAVAILABLE')
        ReadOnly = read_only_type(self.r.adapter.Systemd)
        self.backend = ReadOnly(self.reader, self.systemd, self.manifest['unit_name'])
        self.docker = docker_type(self.cap, self.r.probe)()
        self.inventory = Inventory(root, langfuse, self.directory, self.cap, self.docker)
        self.current = None
        self.controller = self.core.Maintenance(self.directory, self.backend,
                                                inventory_fn=lambda: deepcopy(self.current['inventory']))

    def state(self): return self.r.service.status(self.directory, self.backend)

    def status(self):
        records = self.controller.records()
        state = self.state()
        intent = self.controller.store.optional('intent.json')
        jobs = sorted((dict(job_id=x['id'], phase=x['phase'], historical_return_only=x['phase']=='returned')
                       for x in records), key=lambda x:x['job_id'])
        unresolved = sum(x['phase'] != 'returned' for x in records)
        evaluating = (state.get('current_invocation_verified') is True and
                      state.get('evaluation_status') == 'in_progress' and
                      state.get('desired_state') == 'running' and state.get('recovery') is None)
        recovery = bool(unresolved or state.get('recovery') or
                        (intent and intent.get('phase') != 'completed' and not evaluating))
        blocked = bool(unresolved or state.get('recovery') or state.get('desired_state') != 'running'
                       or (intent and intent.get('phase') != 'completed')
                       or not state.get('current_invocation_verified') or state.get('evaluation_status') != 'healthy')
        return dict(result='attention_required' if blocked else 'passed', diagnostic_status='completed',
                    management=self.r.observation.projection(state), jobs=jobs, unresolved_jobs=unresolved,
                    intent_phase=(intent or {}).get('phase'), recovery_required=recovery,
                    review_blocked=blocked, evaluation_in_progress=evaluating,
                    saved_records_authorize_replay=False, current_health_verified=
                    state.get('current_invocation_verified') is True and state.get('evaluation_status')=='healthy',
                    next_step='review_reported_phase_without_replay' if blocked else 'read_only_scope_plan')

    def plan_once(self):
        before = self.inventory.capture(); self.current = before
        proposal = self.controller.plan()
        require(before['host']['daemon_id'] == proposal['manifest']['binding']['daemon_id'],
                'MAINTENANCE_DAEMON_BINDING_CHANGED')
        ops = before['inventory']['scope']['ops-web']
        require(ops['container_id'] == proposal['manifest']['binding']['ops_container_id']
                and ops['image_id'] == proposal['manifest']['binding']['ops_image_id'],
                'MAINTENANCE_OPS_BINDING_CHANGED')
        after = self.inventory.capture()
        require(before == after, 'MAINTENANCE_DISCOVERY_CHANGED')
        state = self.state()
        require(state.get('current_invocation_verified') is True and state.get('evaluation_status') == 'healthy'
                and state['provider_status']['InvocationID'] == proposal['prior_service_invocation']
                and self.controller.store.control() == proposal['control'], 'MAINTENANCE_WORKER_CHANGED')
        require(self.controller.fixed()[1] == proposal['fixed_evidence']
                and self.controller.history(self.directory) == proposal['history_sha256'],
                'MAINTENANCE_HISTORY_CHANGED')
        contract = {k:v for k,v in proposal.items() if k != 'id'}
        contract.update(discovery=before, adapter_sha256=hashlib.sha256(source(self.root/'src/ops/tools/maintenance_control.py')).hexdigest())
        return dict(result='passed', review_id=fingerprint(contract), review_authorizes_execution=False,
                    scope={name:dict(row, **before['evidence'][name]) for name,row in before['inventory']['scope'].items()},
                    scoped_container_count=len(before['inventory']['scope']), protected_running_count=len(before['inventory']['protected']),
                    protected_inventory_sha256=fingerprint(before['inventory']['protected']),
                    discovery_stable=True, binding_verified=True, initial_history_verified=True,
                    current_health_verified=True, management=self.r.observation.projection(state),
                    source=self.sources, candidate_sha256=PINS[SPEC],
                    next_step='implement_reviewed_stop_return_and_explicit_recovery_adapter')

    def plan(self, clock=time.monotonic, sleep=time.sleep):
        start = clock(); attempts = 0; last = None
        while True:
            attempts += 1
            try:
                result = self.plan_once()
                result['plan_wait'] = dict(attempts=attempts, retry_budget_seconds=30,
                                          elapsed_seconds=round(clock()-start,3), last_retry_code=last)
                return result
            except Exception as exc:
                code = getattr(exc, 'code', None)
                retry = code in ('PUBLIC_URL_CHANGE_BUSY','PREPARED_STATE_BUSY')
                if code in ('WORKER_UNFINISHED_INTENT','MAINTENANCE_HEALTHY_WORKER_REQUIRED','MAINTENANCE_WORKER_CHANGED'):
                    state = self.state()
                    retry = (state.get('current_invocation_verified') is True and
                             state.get('evaluation_status') == 'in_progress' and
                             state.get('desired_state') == 'running' and state.get('recovery') is None)
                if not retry or clock()-start >= 30: raise
                last = code; sleep(min(1, max(0,30-(clock()-start))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan','status'))
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/agent-project-ops/server')
    parser.add_argument('--langfuse-state-dir', type=Path, default=Path.home()/'.local/state/agent-project-langfuse')
    args = parser.parse_args()
    report = dict(schema=1, stage=STAGE, action=args.action,
                  checked_at=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
                  scope='read_only_existing_owned_installation_metadata',
                  effects=dict(files_written=[], service_control_requested=False, configuration_written=False,
                               application_credential_contents_read=False, application_api_requests=0,
                               backup_executed=False, restore_executed=False, inference_requested=False,
                               image_build_or_pull_requested=False, linger_changed=False),
                  capabilities=dict(plan=True,status=True,stop=False,return_worker=False,recover=False,backup=False),
                  activation_supported=False, actual_reboot_acceptance='deferred_by_user', kubernetes_acceptance='deferred')
    review = None
    try:
        review = Review(args.state_dir, args.langfuse_state_dir)
        report.update(review.plan() if args.action=='plan' else review.status())
    except Exception as exc:
        code = getattr(exc,'code',None)
        report.update(result='attention_required', code=code if isinstance(code,str) and
                      re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}',code) else 'MAINTENANCE_REVIEW_UNAVAILABLE',
                      next_step='inspect_read_only_status_keep_journals_and_backups')
        diagnostic = safe_docker_diagnostic(getattr(exc,'docker_diagnostic',None))
        if diagnostic is not None: report['docker_diagnostic'] = diagnostic
    if review:
        report['metadata_command_counts'] = dict(adapter=review.reader.calls+review.docker.calls+review.docker.base.calls,
                                               includes_core_metadata_queries=False)
        report['worker_service_control_commands'] = review.backend.control_commands
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['result']=='passed' else 2


if __name__ == '__main__': sys.exit(main())
