#!/usr/bin/env python3
"""Isolated native acceptance of the reviewed worker-only execution adapter.

The inventory provider projects actual disposable Docker fixtures. Production
Compose discovery remains a separate read-only acceptance; no production worker
state, key or lifecycle is attached. Existing runtime and adapter bytes are copied.
"""
from copy import deepcopy
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops_tools import maintenance_acceptance as a, maintenance_execute as execution
from ops_platform.contracts import ReadFailure, require

STAGE = 'O04-isolated-native-maintenance-execution-acceptance'
KIND = 'isolated-native-maintenance-execution'
ENTRY = 'ops_tools/maintenance_execute_acceptance.py'
EXECUTOR_SHA = '28015d8b768ebe0eeb0e5c82fbc218c25cbe683c28e00ce467e6b4c451a6522f'
EXECUTION_PHASES = {'prepare','readonly_plan','stop','return','failure_gate','recovery','marker_check','cleanup_unit'}
CURRENT_PHASE = None
FIXED = {ENTRY, 'ops_tools/maintenance_execute.py', 'ops_tools/maintenance_control.py',
         'ops_tools/plan_backup_capacity.py', 'ops_tools/measure_backup_databases.py',
         'ops_tools/boot_control.py', 'ops_candidates/boot_transition_20260929/manifest.json',
         'ops_candidates/boot_transition_20260929/transition.py',
         'ops_candidates/boot_start_20260929/manifest.json'}


def source_set(root):
    a.sources(root)
    require(a.digest(a.rc.read_file(root/'ops_tools/maintenance_execute.py', 1024*1024)[0]) == EXECUTOR_SHA,
            'EXECUTION_ACCEPTANCE_EXECUTOR_CHANGED')
    paths = set(FIXED)
    spec = json.loads(a.rc.read_file(root/'ops_candidates/boot_transition_20260929/manifest.json', 128*1024)[0])
    for row in spec['changes']:
        rel = Path(row['path'])
        require(not rel.is_absolute() and '..' not in rel.parts, 'EXECUTION_ACCEPTANCE_SOURCE_SCOPE')
        paths.add(str(Path('ops_candidates/boot_transition_20260929/source')/rel))
    result = {}
    for rel in sorted(paths):
        path = a.manage.safe(root/rel)
        require(path.is_file() and path.stat().st_uid == os.getuid() and path.stat().st_nlink == 1,
                'EXECUTION_ACCEPTANCE_SOURCE_UNSAFE')
        result[rel] = a.rc.read_file(path, 1024*1024)[0]
    require(a.digest(result['ops_tools/maintenance_control.py']) == execution.REVIEW_PIN,
            'EXECUTION_ACCEPTANCE_REVIEW_CHANGED')
    return result


def preflight():
    value = a.preflight(); source_set(ROOT)
    return dict(value, stage=STAGE, scope='read_only_current_user_manager_and_reviewed_execution_sources',
                native_adapter_mutation_acceptance=False, production_worker_stopped=False)


def create_run(base, engine):
    bodies = source_set(ROOT)
    directory, record = a.create_run(base, engine)
    for rel, raw in bodies.items():
        path = directory/'project'/rel
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(raw); path.chmod(0o600)
    a.isolated.write(directory/'execution-run.json', dict(schema=1, kind=KIND,
        directory=str(directory), owner=record['owner'], executor_sha256=EXECUTOR_SHA,
        host_sources={rel:a.digest(raw) for rel,raw in bodies.items()}))
    validate(directory)
    return directory, record


def validate(directory):
    record = a.validate(directory)
    marker = a.strict_json(a.read(directory/'execution-run.json', 128*1024))
    require(marker.get('schema') == 1 and marker.get('kind') == KIND and
            marker.get('directory') == str(directory) and marker.get('owner') == record['owner'] and
            marker.get('executor_sha256') == EXECUTOR_SHA, 'EXECUTION_ACCEPTANCE_OWNER_CHANGED')
    expected = {rel:a.digest(raw) for rel,raw in source_set(ROOT).items()}
    require(marker.get('host_sources') == expected, 'EXECUTION_ACCEPTANCE_SOURCE_SET_CHANGED')
    for rel, digest in expected.items():
        require(a.digest(a.read(directory/'project'/rel)) == digest, 'EXECUTION_ACCEPTANCE_COPY_CHANGED')
    return record


class Engine(a.Engine):
    def create_fixture_network(self, directory):
        validate(directory); return super().create_fixture_network(directory)

    def compose(self, directory, relative, service, recreate=False):
        validate(directory); return super().compose(directory, relative, service, recreate)

    def cleanup(self, directory):
        validate(directory); return super().cleanup(directory)


class FixtureInventory:
    """Native Docker identities; fixture roles rather than production metadata."""
    def __init__(self, directory, engine, review):
        self.directory, self.engine, self.review = directory, engine, review

    def once(self):
        record = validate(self.directory)
        state, _ = a.native.checked_state(self.directory)
        wanted = {(record['prefix']+'-'+role, service):'fixture-'+role
                  for role,(service,_) in a.isolated.ROLES.items()}
        wanted[(record['prefix']+'-model','manual-model')] = 'fixture-manual-model'
        wanted[(record['prefix']+'-ops','ops-web')] = 'ops-web'
        owned = self.engine.owned(self.directory, record)
        require(len(owned) == len(wanted) == 9, 'EXECUTION_ACCEPTANCE_FIXTURE_SET_CHANGED')
        active = self.engine.snapshot(); scoped, evidence = {}, {}
        for ident in owned:
            meta = self.engine.metadata(ident); row = meta['identity']
            role = wanted.get((row['project'], row['service']))
            require(role is not None and role not in scoped and row['id'] == ident and
                    meta['acceptance'] == record['owner'] and row['image'] == record['image'] and
                    row['running'] is True and row.get('health') == 'healthy' and ident in active and
                    active[ident]['started'] == row['started'], 'EXECUTION_ACCEPTANCE_FIXTURE_IDENTITY_CHANGED')
            projection = []
            require(isinstance(row.get('mounts'), list) and row['mounts'], 'EXECUTION_ACCEPTANCE_MOUNTS_INVALID')
            for mount in row['mounts']:
                if mount.get('Type') == 'tmpfs':
                    require(role == 'ops-web' and mount.get('Destination') == '/tmp' and mount.get('RW') is True,
                            'EXECUTION_ACCEPTANCE_MOUNTS_INVALID')
                    projection.append({'Type':'tmpfs','Destination':'/tmp','RW':True})
                else:
                    source = Path(mount.get('Source',''))
                    require(mount.get('Type') == 'bind' and source.is_absolute() and
                            source.is_relative_to(self.directory) and str(source) == os.path.normpath(str(source)),
                            'EXECUTION_ACCEPTANCE_MOUNT_OUTSIDE_RUN')
                    projection.append({key:mount[key] for key in ('Type','Source','Destination','RW')})
            require(len({x['Destination'] for x in projection}) == len(projection), 'EXECUTION_ACCEPTANCE_MOUNTS_INVALID')
            scoped[role] = dict(container_id=ident, image_id=row['image'], owner=record['owner'],
                mounts_sha256=execution.fingerprint(sorted(projection,key=lambda x:x['Destination'])))
            evidence[role] = dict(component='disposable-fixture', compose_project=row['project'], started_at=row['started'])
        require(set(scoped) == set(wanted.values()), 'EXECUTION_ACCEPTANCE_FIXTURE_SET_CHANGED')
        cfg = a.manage.existing(state)
        files = {}
        paths = [state/name for name in ('owner.json','host-config.json','config.json','compose.env','access-key')]
        paths += [Path(cfg['platforms']['agent']['token_file']), Path(cfg['langfuse_file'])]
        for path in paths:
            require(path.is_relative_to(self.directory), 'EXECUTION_ACCEPTANCE_STATE_OUTSIDE_RUN')
            a.manage.private(path); st = path.stat()
            require(st.st_nlink == 1, 'EXECUTION_ACCEPTANCE_FILE_HARDLINK')
            files[str(path)] = [st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
        host = self.review.docker.host()
        require(host['endpoint'] == record['endpoint'], 'EXECUTION_ACCEPTANCE_DAEMON_CHANGED')
        ids = {x['container_id'] for x in scoped.values()}
        require(all(x.get('id') == ident and x.get('running') is True for ident,x in active.items()),
                'EXECUTION_ACCEPTANCE_RUNNING_SET_INVALID')
        return dict(inventory={'scope':scoped,'protected':{ident:x['started'] for ident,x in active.items() if ident not in ids}},
                    evidence=evidence, files=files, host={k:host[k] for k in ('context','endpoint','daemon_id')})

    def capture(self):
        first, second = self.once(), self.once()
        require(first == second, 'EXECUTION_ACCEPTANCE_INVENTORY_UNSTABLE')
        return first


def executor(directory, engine):
    validate(directory)
    require(ROOT == directory/'project', 'EXECUTION_ACCEPTANCE_COPIED_ENTRY_REQUIRED')
    state, _ = a.native.checked_state(directory)
    value = execution.Execute(state, directory/'keys', root=ROOT)
    # Explicit test inventory provider only. Real reviewed adapter, core,
    # current-unit Systemd backend, readback, receipts and worker remain intact.
    value.review.inventory = FixtureInventory(directory, engine, value.review)
    return value


def rejected(fn, codes):
    try: fn()
    except Exception as exc:
        code = getattr(exc,'code',None)
        require(code in codes, code if isinstance(code,str) and re.fullmatch('[A-Z][A-Z0-9_]{0,95}',code)
                else 'EXECUTION_ACCEPTANCE_UNKNOWN_REFUSAL')
        return code
    raise ReadFailure('EXECUTION_ACCEPTANCE_REFUSAL_MISSING')


def paused(x):
    manifest = a.service.load(x.directory)
    current = a.service.owned(manifest,x.controller.backend)
    require(a.service.inactive(current) and x.controller.backend.cgroup_empty(current) and
            not a.service.enabled(manifest) and x.controller.store.control()['desired'] == 'paused',
            'EXECUTION_ACCEPTANCE_PAUSE_NOT_VERIFIED')
    with a.ws.automatic_lease(x.directory): pass


def preserve_inventory(x, expected):
    require(x.review.inventory.capture() == expected, 'EXECUTION_ACCEPTANCE_CONTAINER_OR_FILES_CHANGED')


def progress(directory, engine, phase, message):
    global CURRENT_PHASE
    require(phase in EXECUTION_PHASES, 'EXECUTION_ACCEPTANCE_PHASE_INVALID')
    CURRENT_PHASE = phase
    engine.protect(validate(directory)['protected'])
    print('[execution-acceptance] '+message, file=sys.stderr, flush=True)


def exercise(directory, engine):
    validate(directory)
    require(ROOT == directory/'project', 'EXECUTION_ACCEPTANCE_COPIED_ENTRY_REQUIRED')
    progress(directory, engine, 'prepare', 'prepare disposable Ops and real boot-policy worker for new execution adapter')
    c = a.isolated.prepare_test_ops(directory, engine); state, _ = a.native.checked_state(directory)
    ctl = a.native.Controller(state); first = a.initialize_boot_fixture(state, ctl)
    store = a.ws.Store(state); manifest = a.service.load(state)
    fixed = {name:a.read(store.path.parent/name,32768) for name in ('boot-start.json','code-transition.json','service.json')}
    progress(directory, engine, 'readonly_plan', 'verify reviewed copied adapter and read-only fixture plan')
    x = executor(directory, engine); before = x.review.inventory.capture()
    plan = x.plan()[0]
    require(plan['result'] == 'passed' and not plan['review_authorizes_execution'] and
            not x.folder.exists() and x.controller.backend.control_commands == 0,
            'EXECUTION_ACCEPTANCE_PLAN_MUTATED')
    cases = {'reviewed_sources_and_readonly_fixture_plan':'passed'}
    rejected(lambda:x.stop('0'*64), {'MAINTENANCE_EXECUTION_PLAN_CHANGED'})
    require(not x.folder.exists() and x.controller.backend.control_commands == 0,
            'EXECUTION_ACCEPTANCE_WRONG_REVIEW_MUTATED')
    cases['wrong_review_refused_without_pause_or_control'] = 'passed'
    progress(directory, engine, 'stop', 'new adapter stop once; nine fixture containers remain running')
    stopped = x.stop(plan['review_id'])
    require(stopped.get('result') == 'passed' and stopped.get('business_containers_stopped') == 0,
            stopped.get('code','EXECUTION_ACCEPTANCE_STOP_FAILED'))
    job = stopped['job_id']; paused(x); preserve_inventory(x,before)
    cases['native_adapter_stop_cgroup_free_lease_and_disabled_unit'] = 'passed'
    cases['all_fixture_containers_and_started_times_preserved'] = 'passed'
    commands = x.controller.backend.control_commands
    rejected(lambda:x.stop(plan['review_id']), {'MAINTENANCE_EXECUTION_UNRESOLVED_JOB'})
    require(x.controller.backend.control_commands == commands, 'EXECUTION_ACCEPTANCE_DUPLICATE_STOP')
    cases['duplicate_stop_does_not_resubmit'] = 'passed'
    progress(directory, engine, 'return', 'explicit adapter return requires live readback and fresh receipt/invocation/evaluation')
    x = executor(directory, engine); returned = x.return_worker(job)
    require(returned.get('result') == 'passed' and returned.get('fresh_evaluation_verified') is True,
            returned.get('code','EXECUTION_ACCEPTANCE_RETURN_FAILED'))
    current = ctl.wait(a.native.healthy)
    require(a.native.invocation(current) != a.native.invocation(first) and x.load(job)['phase'] == 'returned',
            'EXECUTION_ACCEPTANCE_STALE_RETURN')
    cases['native_adapter_return_new_receipt_invocation_and_evaluation'] = 'passed'
    commands = x.controller.backend.control_commands
    rejected(lambda:x.return_worker(job), {'MAINTENANCE_EXECUTION_RETURN_REFUSED'})
    require(x.controller.backend.control_commands == commands, 'EXECUTION_ACCEPTANCE_DUPLICATE_START')
    cases['returned_job_cannot_resubmit_start'] = 'passed'
    journals = len(a.refresh.lineage(state)['records'])
    follow = ctl.wait(lambda v:a.native.healthy(v,a.native.observation(current)['completed_cycles']+1))
    require(a.native.invocation(follow) == a.native.invocation(current) and
            not a.native.observation(follow)['outcome']['restart_requested'] and
            len(a.refresh.lineage(state)['records']) == journals, 'EXECUTION_ACCEPTANCE_FOLLOWUP_CHANGED')
    preserve_inventory(x,before)
    cases['unchanged_followup_without_duplicate_restart_or_journal'] = 'passed'
    progress(directory, engine, 'failure_gate', 'second adapter job: actual failed alert readback keeps pause and blocks repeat return')
    x = executor(directory, engine); second = x.stop(x.plan()[0]['review_id'])
    require(second.get('result') == 'passed',second.get('code','EXECUTION_ACCEPTANCE_SECOND_STOP_FAILED'))
    second_job = second['job_id']; paused(x)
    a.isolated.write(c['control'],{'version':0,'rules_fault':True})
    try:
        x = executor(directory, engine); denied = x.return_worker(second_job)
        require(denied.get('result') == 'attention_required' and denied.get('code') == 'OPS_ALERT_READBACK_FAILED',
                denied.get('code','EXECUTION_ACCEPTANCE_READBACK_GATE_FAILED'))
        paused(x); commands = x.controller.backend.control_commands
        rejected(lambda:x.return_worker(second_job), {'MAINTENANCE_EXECUTION_RETURN_REFUSED'})
        require(x.controller.backend.control_commands == commands, 'EXECUTION_ACCEPTANCE_FAILURE_REPLAYED')
        failed_job = deepcopy(x.controller.load(second_job))
    finally: a.isolated.write(c['control'],{'version':0})
    cases['native_failed_readback_keeps_pause_without_automatic_recovery'] = 'passed'
    progress(directory, engine, 'recovery', 'read-only recovery review; explicit recovery stays paused and retains failure audit')
    x = executor(directory, engine); recovery = x.recovery_plan(second_job)[0]
    require(recovery['recovery_mode'] == 'prepare_explicit_return' and
            not recovery['worker_will_be_started'] and x.controller.backend.control_commands == 0,
            'EXECUTION_ACCEPTANCE_RECOVERY_PLAN_MUTATED')
    rejected(lambda:x.recover(second_job,'0'*64), {'MAINTENANCE_EXECUTION_RECOVERY_PLAN_CHANGED'})
    recovered = x.recover(second_job,recovery['recovery_review_id'])
    require(recovered.get('result') == 'passed' and recovered.get('worker_started') is False and
            x.controller.backend.control_commands == 0 and
            x.load(second_job)['recovery_audit']['before_job'] == failed_job,
            'EXECUTION_ACCEPTANCE_RECOVERY_REPLAYED')
    paused(x); preserve_inventory(x,before)
    cases['native_explicit_prestart_recovery_preserves_pause_and_failure_audit'] = 'passed'
    progress(directory, engine, 'marker_check', 'fresh return after recovery; simulate lost adapter marker and check prior accepted submission')
    x = executor(directory, engine); save = x.save
    def lose_marker(row, phase):
        if phase == 'returned': raise OSError('isolated acceptance marker write interruption')
        return save(row,phase)
    x.save = lose_marker
    try: x.return_worker(second_job)
    except OSError: pass
    else: raise ReadFailure('EXECUTION_ACCEPTANCE_MARKER_FAULT_MISSING')
    require(x.controller.load(second_job)['phase'] == 'returned' and x.load(second_job)['phase'] == 'return_requested',
            'EXECUTION_ACCEPTANCE_MARKER_BOUNDARY_WRONG')
    fresh = ctl.wait(a.native.healthy); submitted_invocation = a.native.invocation(fresh)
    require(submitted_invocation != a.native.invocation(current), 'EXECUTION_ACCEPTANCE_RECOVERY_STALE_RETURN')
    x = executor(directory, engine); checked = x.check(second_job)
    require(checked.get('result') == 'passed' and checked.get('start_request_submitted') is False and
            checked.get('current_health_verified') is True and x.controller.backend.control_commands == 0 and
            a.native.invocation(ctl.status()) == submitted_invocation,
            'EXECUTION_ACCEPTANCE_CHECK_REPLAYED')
    rejected(lambda:x.check(second_job), {'MAINTENANCE_EXECUTION_CHECK_REFUSED'})
    rejected(lambda:x.return_worker(second_job), {'MAINTENANCE_EXECUTION_RETURN_REFUSED'})
    rejected(lambda:x.recovery_plan(second_job), {'MAINTENANCE_EXECUTION_START_NOT_REPLAYABLE'})
    cases['recovered_job_requires_fresh_readback_and_new_evaluation'] = 'passed'
    cases['lost_adapter_acceptance_marker_check_does_not_start_again'] = 'passed'
    cases['completed_check_return_and_recovery_cannot_replay'] = 'passed'
    preserve_inventory(x,before); a.isolated.preserve_test_ops(c,engine)
    require({name:a.read(store.path.parent/name,32768) for name in fixed} == fixed and
            a.worker.code_hash(ROOT) == a.core.CODE, 'EXECUTION_ACCEPTANCE_FIXED_HISTORY_CHANGED')
    cases['policy_code_credentials_cache_and_initial_history_preserved'] = 'passed'
    progress(directory, engine, 'cleanup_unit', 'remove owned test unit; preserve both adapter/core job histories')
    removed = a.native.cleanup_unit(directory)
    require(removed.get('worker_exit_verified') and removed.get('unit_absence_verified'),
            'EXECUTION_ACCEPTANCE_UNIT_REMOVAL_FAILED')
    require(x.load(job)['phase'] == x.load(second_job)['phase'] == 'returned' and
            x.load(second_job)['recovery_audit']['before_job'] == failed_job,
            'EXECUTION_ACCEPTANCE_AUDIT_CHANGED')
    cases['owned_unit_removed_and_adapter_core_audits_preserved'] = 'passed'
    return dict(schema=1,stage=STAGE,action='worker',result='passed',checked_at=a.rc.now(),cases=cases,
        scope='native_user_manager_and_docker_fixture_inventory_provider_synthetic_upstreams_same_boot',
        inventory_provider='disposable_Docker_owner_and_Compose_metadata_with_private_file_metadata',
        adapter_entry='reviewed_Execute_methods_with_explicit_fixture_inventory_provider',
        production_compose_inventory_acceptance=False, native_adapter_mutation_acceptance=True,
        executor_sha256=EXECUTOR_SHA, manager_version=ctl.manager_version, interval_seconds=30,
        fixture_containers=9, fixture_container_stop_or_start_commands=0, adapter_jobs=2,
        job_phases=['returned','returned'], known_prestart_recovery_executed=True,
        injected_adapter_marker_write_failure=True, check_did_not_resubmit_start=True,
        image_id=c['record']['image'],runtime_source_hashes_verified=c['hashes'],active_code_sha256=a.core.CODE,
        same_test_ops_container_image_mounts=True,initial_cache_entries_preserved=len(c['initial_cache']['success_times']),
        synthetic_credentials_preserved=True,bootstrap_record_preserved=True,initial_history='isolated_fixture_with_real_fresh_evaluation',
        refresh_journal_count=len(a.refresh.lineage(state)['records']),test_unit_name=manifest['unit_name'],
        test_unit_removed=True,test_worker_exit_verified=True,
        production_state_read=False,production_credentials_read=False,production_worker_stopped=False,
        production_installation_files_read=False,production_container_metadata_read_for_protection=True,
        production_worker_lifecycle_observed=False,
        production_worker_started=False,production_service_control_requested=False,production_application_api_requests=0,
        backup_executed=False,restore_executed=False,backup_executor_integration=False,inference_requested=False,
        image_build_or_pull_requested=False,linger_changed=False,host_reboot_requested=False,
        actual_reboot_acceptance='deferred_by_user',kubernetes_acceptance='deferred',
        long_running_acceptance=False,promql_engine_acceptance=False)


def error_report(exc):
    value = a.error_report(exc); value['stage'] = STAGE
    phase = getattr(exc,'execution_phase',None)
    if phase in EXECUTION_PHASES: value['execution_phase'] = phase
    return value


def invoke(directory, timeout=850):
    record = validate(directory); project = directory/'project'
    env = {k:v for k,v in os.environ.items() if not k.startswith(('OPS_','COMPOSE_','PYTHON')) and
           k not in ('DOCKER_CONTEXT','DOCKER_API_VERSION','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}
    env.update(DOCKER_HOST=record['endpoint'],PYTHONDONTWRITEBYTECODE='1')
    path = directory/'execution-worker.log'
    if path.exists(): a.manage.private(path)
    with path.open('w') as stream:
        path.chmod(0o600)
        child = subprocess.Popen([sys.executable,'-I','-B',str(project/ENTRY),'worker','--run-dir',str(directory)],
            cwd=project,env=env,stdin=subprocess.DEVNULL,stdout=stream,start_new_session=True)
        try: code = child.wait(timeout=timeout)
        except BaseException: a.isolated.stop_worker(child); raise
    answer = a.strict_json(a.read(path,128*1024))
    require(answer.get('stage') == STAGE and answer.get('action') == 'worker', 'EXECUTION_ACCEPTANCE_CHILD_REPORT_INVALID')
    if code != 0 or answer.get('result') != 'passed':
        exc = ReadFailure(answer.get('code','EXECUTION_ACCEPTANCE_CHILD_FAILED'))
        exc.docker_diagnostic = a.safe_docker_diagnostic(answer.get('docker_diagnostic'))
        exc.execution_phase = answer.get('execution_phase')
        raise exc
    return answer


def run(base):
    preflight()
    with a.isolated.run_lock(base):
        engine = Engine(); directory, record = create_run(base,engine)
        print('[execution-acceptance] evidence: '+str(directory),file=sys.stderr,flush=True)
        report = dict(schema=1,stage=STAGE,result='attention_required',production_worker_stopped=False,
            production_service_control_requested=False,production_credentials_read=False,backup_executed=False,
            restore_executed=False,inference_requested=False,linger_changed=False)
        try:
            engine.create_fixture_network(directory)
            for role,(name,subtree) in a.isolated.ROLES.items():
                rel = str(Path(subtree)/('acceptance-'+role+'.json'))
                print('[execution-acceptance] fixture preparation: '+role,file=sys.stderr,flush=True)
                engine.compose(directory,rel,name)
                if role == 'model': engine.compose(directory,rel,'manual-model')
            report = invoke(directory)
        except (KeyboardInterrupt,subprocess.TimeoutExpired): report['code'] = 'EXECUTION_ACCEPTANCE_INTERRUPTED'
        except Exception as exc: report.update(error_report(exc))
        finally:
            try: cleanup = engine.cleanup(directory)
            except Exception as exc: cleanup = error_report(exc)
            if cleanup.get('result') != 'passed': report['result'] = 'attention_required'
            if cleanup.get('prior_run_protected_baseline_match') is False and report.get('result') == 'passed':
                report.update(result='attention_required',code='ACCEPTANCE_PROTECTED_CONTAINER_CHANGED')
            report.update(run_directory=str(directory),cleanup=cleanup)
            a.isolated.write(directory/'execution-report.json',report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('preflight','run','worker','cleanup'))
    parser.add_argument('--state-dir',type=Path,default=a.isolated.BASE)
    parser.add_argument('--run-dir',type=Path)
    args = parser.parse_args()
    try:
        if args.action in ('preflight','run'):
            require(args.run_dir is None,'ACCEPTANCE_RUN_DIR_NOT_ALLOWED')
            report = preflight() if args.action == 'preflight' else run(args.state_dir)
        else:
            require(args.run_dir is not None,'ACCEPTANCE_RUN_DIR_REQUIRED')
            directory = a.manage.safe(args.run_dir); record = validate(directory)
            engine = Engine(record['endpoint'])
            if args.action == 'cleanup':
                with a.isolated.run_lock(directory.parent):
                    report = dict(schema=1,stage=STAGE,action='cleanup',result='passed',run_directory=str(directory),
                        cleanup=engine.cleanup(directory),production_worker_stopped=False,production_service_control_requested=False,
                        acceptance_result_not_reused=True)
                    a.isolated.write(directory/'execution-cleanup.json',report)
            else:
                require(ROOT == directory/'project','EXECUTION_ACCEPTANCE_COPIED_ENTRY_REQUIRED')
                with a.lifecycle_lock(directory):
                    try: report = exercise(directory,engine)
                    except Exception as exc:
                        exc.execution_phase = CURRENT_PHASE
                        report = dict(error_report(exc),action='worker')
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0 if report['result'] == 'passed' else 2
    except Exception as exc:
        print(json.dumps(dict(error_report(exc),schema=1,action=args.action,production_worker_stopped=False,
            production_service_control_requested=False,linger_changed=False),ensure_ascii=False,indent=2))
        return 2


if __name__ == '__main__':
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
