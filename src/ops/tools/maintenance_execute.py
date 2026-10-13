#!/usr/bin/env python3
"""Reviewed production worker pause, explicit return and non-replaying recovery.

This adapter pauses only the owned boot-policy refresh worker. Scoped business
containers stay running. Storage capture and backup are separate future work.
Plan/status/recovery-plan are metadata-only; return/check reuse the accepted
current-configuration GET/auth readback and new-invocation evaluation contract.
"""
import argparse
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
REVIEW_PIN = '4361ee6f37be74614bb79829849bdc213693d76c9bcb2bb39668f9315a979ca8'
KIND = 'reviewed-worker-only-maintenance-control'
OPERATION = 'worker_pause_only_no_storage_or_business_control'
STAGE = 'O04-reviewed-production-maintenance-control'
PHASES = {'prepared', 'stop_requested', 'paused', 'return_requested', 'returned',
          'attention_required', 'recovery_prepared', 'recovered', 'closed_unsubmitted'}
PRESTART = {'prepared', 'stop_requested', 'stopped', 'operation_running', 'ready_to_return'}
RECORD_LIMIT = 262144


def require(ok, code):
    if not ok: raise Failure(code)


class Failure(Exception):
    def __init__(self, code): self.code = code; super().__init__(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def dependencies(root):
    path = root/'src/ops/tools/maintenance_control.py'
    # Validate source before importing it; the underlying review verifies all
    # runtime/candidate pins before importing project implementations.
    require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
            'MAINTENANCE_EXECUTION_SOURCE_UNSAFE')
    require(path.stat().st_nlink == 1 and path.stat().st_size < 2*1024*1024,
            'MAINTENANCE_EXECUTION_SOURCE_UNSAFE')
    require(hashlib.sha256(path.read_bytes()).hexdigest() == REVIEW_PIN,
            'MAINTENANCE_EXECUTION_REVIEW_SOURCE_CHANGED')
    spec = importlib.util.spec_from_file_location('_maintenance_execution_review', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class Execute:
    def __init__(self, directory, langfuse, root=ROOT):
        self.m = dependencies(root)
        self.review = self.m.Review(directory, langfuse, root)
        self.root, self.r, self.core = root, self.review.r, self.review.core
        self.directory, self.controller = self.review.directory, self.review.controller
        self.folder = self.controller.store.path.parent/'maintenance-control'
        self.binding = None
        self.durable = {}
        self.hook = lambda phase: None

    def source_sha(self):
        raw=self.m.source(self.root/'src/ops/tools/maintenance_execute.py')
        digest=hashlib.sha256(raw).hexdigest()
        if getattr(self,'_verified_source',None)!=digest:
            for name in Execute.__dict__:
                if hasattr(getattr(Execute,name),'__code__'):
                    self.core.loaded_function(sys.modules[__name__],'Execute.'+name,raw)
            for name in ('dependencies','fingerprint','main'):
                self.core.loaded_function(sys.modules[__name__],name,raw)
            self._verified_source=digest
        return digest

    def path(self, job):
        require(isinstance(job, str) and str(uuid.UUID(job)) == job, 'MAINTENANCE_EXECUTION_ID_INVALID')
        return self.folder/(job+'.json')

    def load(self, job):
        path = self.path(job); self.r.manage.private(self.folder)
        raw = self.r.worker.activation.rc.read_file(path, RECORD_LIMIT, private=True)[0]
        require(path.stat().st_nlink == 1, 'MAINTENANCE_EXECUTION_HARDLINK_REFUSED')
        row = self.r.ws.strict_json(raw)
        require(row.get('schema') == 1 and row.get('kind') == KIND and row.get('id') == job
                and row.get('directory') == str(self.directory) and row.get('phase') in PHASES
                and row.get('operation') == OPERATION, 'MAINTENANCE_EXECUTION_RECORD_INVALID')
        require(row.get('executor_sha256') == self.source_sha(), 'MAINTENANCE_EXECUTION_SOURCE_CHANGED')
        require(row.get('review_id') == self.review_hash(row['proposal'], row['discovery'], row['executor_sha256']),
                'MAINTENANCE_EXECUTION_REVIEW_CHANGED')
        return row

    def records(self):
        if not self.folder.exists() and not self.folder.is_symlink(): return []
        self.r.manage.private(self.folder); paths=list(self.folder.iterdir())
        require(len(paths) < self.core.LIMIT, 'MAINTENANCE_EXECUTION_HISTORY_LIMIT')
        require(all(p.suffix == '.json' and self.path(p.stem) == p for p in paths),
                'MAINTENANCE_EXECUTION_FOREIGN_RECORD')
        return [self.load(p.stem) for p in paths]

    def save(self, row, phase):
        require(phase in PHASES, 'MAINTENANCE_EXECUTION_PHASE_INVALID')
        if not self.folder.exists(): self.folder.mkdir(mode=0o700)
        self.r.manage.private(self.folder)
        path = self.path(row['id'])
        if path.exists() or path.is_symlink():
            self.r.manage.private(path); require(path.stat().st_nlink == 1, 'MAINTENANCE_EXECUTION_HARDLINK_REFUSED')
            require(self.load(row['id']) == self.durable.get(row['id']), 'MAINTENANCE_EXECUTION_RECORD_CHANGED')
        next_row = dict(row, phase=phase, updated_at=self.core.boot.worker_now())
        require(len((json.dumps(next_row,ensure_ascii=False,indent=2)+'\n').encode()) <= RECORD_LIMIT,
                'MAINTENANCE_EXECUTION_RECORD_LIMIT')
        self.r.worker.activation.rc.write_record(path,next_row); row.clear(); row.update(next_row)
        self.durable[row['id']]=deepcopy(row)
        self.hook('adapter:'+phase)

    @staticmethod
    def review_hash(proposal, discovery, executor):
        return fingerprint(dict(proposal={k:v for k,v in proposal.items() if k!='id'},
                                discovery=discovery, executor_sha256=executor,
                                review_source_sha256=REVIEW_PIN, operation=OPERATION))

    def configure(self, discovery):
        self.binding = deepcopy(discovery)
        def current_inventory():
            try:current = self.review.inventory.capture()
            except Exception as exc:
                diagnostic=self.m.safe_docker_diagnostic(getattr(exc,'docker_diagnostic',None))
                if diagnostic:self.last_docker_diagnostic=diagnostic
                code=getattr(exc,'code',None)
                self.core.require(False,code if isinstance(code,str) and re.fullmatch('[A-Z][A-Z0-9_]{0,95}',code)
                                  else 'MAINTENANCE_EXECUTION_DISCOVERY_UNAVAILABLE')
            self.core.require(current == self.binding, 'MAINTENANCE_EXECUTION_DISCOVERY_CHANGED')
            return deepcopy(current['inventory'])
        self.controller.inventory = current_inventory
        # Only this derived, existing owned unit can be controlled. Plan keeps
        # the original metadata-only backend; mutation actions explicitly bind
        # the reviewed Systemd adapter after matching the expected review.

    def connect_control(self):
        backend = self.r.adapter.Systemd(self.review.reader, self.review.systemd, self.review.manifest['unit_name'])
        self.review.backend = self.controller.backend = backend

    def plan(self):
        require(all(row['phase'] in ('returned','closed_unsubmitted') for row in self.records()),
                'MAINTENANCE_EXECUTION_UNRESOLVED_JOB')
        proposal = []
        original = self.controller.plan
        def capture(*a, **kw):
            value = original(*a, **kw); proposal[:] = [deepcopy(value)]; return value
        self.controller.plan = capture
        try: public = self.review.plan()
        finally: self.controller.plan = original
        require(len(proposal)==1, 'MAINTENANCE_EXECUTION_PROPOSAL_REQUIRED')
        discovery = deepcopy(self.review.current); digest = self.source_sha()
        public.update(review_id=self.review_hash(proposal[0], discovery, digest),
                      review_authorizes_execution=False, operation=OPERATION,
                      worker_only=True, business_container_stop_supported=False,
                      executor_sha256=digest, next_step='explicit_stop_with_current_review_id')
        return public, proposal[0], discovery

    def status(self, job=None):
        public = self.review.status()
        rows = [self.load(job)] if job else self.records()
        public['execution_jobs'] = [dict(job_id=row['id'], phase=row['phase'],
            operation=row['operation'], saved_state_authorizes_replay=False) for row in rows]
        public['automatic_recovery_supported'] = False
        if any(row['phase'] not in ('returned','closed_unsubmitted') for row in rows):
            public.update(result='attention_required', next_step='inspect_job_then_explicit_return_check_or_recovery_plan')
        return public

    def lock(self):
        return self.r.ws.file_lock(self.directory/'maintenance-execution.lock', 'MAINTENANCE_EXECUTION_BUSY')

    def stop(self, expected):
        require(self.plan()[0]['review_id']==expected, 'MAINTENANCE_EXECUTION_PLAN_CHANGED')
        with self.lock():
            public, proposal, discovery = self.plan()
            require(public['review_id'] == expected, 'MAINTENANCE_EXECUTION_PLAN_CHANGED')
            row = dict(schema=1, kind=KIND, id=proposal['id'], directory=str(self.directory),
                       operation=OPERATION, review_id=expected, executor_sha256=self.source_sha(),
                       proposal=proposal, discovery=discovery)
            require(not self.path(row['id']).exists() and not self.path(row['id']).is_symlink(),
                    'MAINTENANCE_EXECUTION_ID_REUSED')
            self.save(row, 'prepared')
            self.configure(discovery); self.connect_control()
            self.save(row, 'stop_requested')
            def pause_only(scope):
                # No storage process, Docker control or detached job is launched.
                scope.checkpoint()
                require(self.load(row['id']) == row, 'MAINTENANCE_EXECUTION_RECORD_CHANGED')
                return {'result':'passed','child_processes_stopped':True}
            result = self.controller.run(proposal, pause_only)
            row['outcome'] = deepcopy(result)
            self.save(row, 'paused' if result['result']=='passed' else 'attention_required')
            return dict(result, operation=OPERATION, business_containers_stopped=0,
                        backup_executed=False, explicit_return_required=True,
                        next_step='explicit_return_with_job_id' if result['result']=='passed' else 'read_only_recovery_plan')

    def matched(self, row):
        job=self.controller.load(row['id'])
        require(all(job.get(k)==v for k,v in row['proposal'].items()), 'MAINTENANCE_EXECUTION_JOB_CHANGED')
        self.configure(row['discovery'])
        self.controller.inventory()  # Fresh identities/StartedAt/files/context before any effect.
        return job

    def return_worker(self, job):
        with self.lock():
            row=self.load(job)
            self.durable[job]=deepcopy(row)
            require(row['phase'] in ('paused','recovered'), 'MAINTENANCE_EXECUTION_RETURN_REFUSED')
            record=self.matched(row)
            require(record['phase']=='ready_to_return', 'MAINTENANCE_EXECUTION_RETURN_REFUSED')
            self.connect_control(); self.save(row, 'return_requested')
            result=self.controller.resume(job)
            row['return_outcome']=deepcopy(result)
            self.save(row, 'returned' if result['result']=='passed' else 'attention_required')
            return result

    def check(self, job):
        with self.lock():
            row=self.load(job)
            self.durable[job]=deepcopy(row)
            require(row['phase']=='return_requested', 'MAINTENANCE_EXECUTION_CHECK_REFUSED')
            record=self.matched(row)
            if record['phase']=='returned':
                # A controller may die after core acceptance but before the
                # adapter marker. Prove the current accepted invocation; do not
                # replace either receipt or the original acceptance history.
                manifest,fixed=self.controller.fixed()
                state=self.review.state(); control=self.controller.store.control()
                require(manifest==record['manifest'] and fixed==record['fixed_evidence'] and
                        self.controller.snapshot(self.directory)==manifest['binding'] and
                        self.controller.identity()['boot_id']==record['boot_id'] and
                        control['desired']=='running' and control['recovery'] is None and
                        control.get('service_stop_token')==record['stop_token'] and
                        self.r.service.enabled(manifest) and state.get('current_invocation_verified') is True and
                        state.get('evaluation_status')=='healthy' and state['provider_status']['InvocationID']==
                        record['acceptance']['provider_status']['InvocationID'],
                        'MAINTENANCE_EXECUTION_ACCEPTED_STATE_CHANGED')
                self.controller.return_check(self.directory,record,self.controller.store.optional('intent.json'))
                receipt=self.controller.store.optional('boot.json') or {}
                require(receipt.get('phase')=='entered' and receipt.get('previous_receipt_sha256')==
                        record['receipt_sha256'] and receipt.get('service_invocation')==
                        state['provider_status']['InvocationID'],'MAINTENANCE_EXECUTION_RECEIPT_CHANGED')
                self.save(row,'returned')
                return dict(result='passed',job_id=job,prior_submission_observed=True,
                            start_request_submitted=False,current_health_verified=True,
                            worker_left_running=True,historical_success_reused=False)
            require(record['phase'] in ('start_requested','submitted'), 'MAINTENANCE_EXECUTION_CHECK_REFUSED')
            self.connect_control()
            result=self.controller.check(job)  # Observes prior submission; never starts it again.
            row['return_outcome']=deepcopy(result)
            self.save(row, 'returned' if result['result']=='passed' else 'attention_required')
            return result

    def recovery_snapshot(self, row):
        """Prove pre-start, worker-only recovery without submitting control.

        Any arming/start receipt, open refresh intent, newer manual pause, active
        cgroup, foreign unit or identity change blocks. A failed return readback
        can be acknowledged while staying paused, then a separate return does
        fresh readback again. Interrupted known recovery writes can finish only
        their exact before/after states; no stop/start operation is replayed.
        """
        path=self.controller.path(row['id'])
        if not path.exists() and not path.is_symlink():
            require(row['phase'] in ('prepared','stop_requested'), 'MAINTENANCE_EXECUTION_JOB_MISSING')
            self.configure(row['discovery']); self.controller.inventory()
            require(self.controller.store.control()==row['proposal']['control'], 'MAINTENANCE_EXECUTION_CONTROL_CHANGED')
            public=self.review.status()
            state=self.review.state()
            require(public['current_health_verified'] and state['provider_status']['InvocationID']==
                    row['proposal']['prior_service_invocation'], 'MAINTENANCE_EXECUTION_UNSUBMITTED_STATE_CHANGED')
            return dict(mode='close_unsubmitted', row_sha256=fingerprint(row),
                        control=self.controller.store.control(), discovery=row['discovery'])
        record=self.matched(row); store=self.controller.store
        phase=record.get('failure_phase') if record['phase']=='attention_required' else record['phase']
        pending=row.get('recovery_transaction')
        if pending:
            require(row['phase']=='recovery_prepared' and isinstance(pending,dict), 'MAINTENANCE_EXECUTION_RECOVERY_RECORD_INVALID')
            require(fingerprint(record) in (fingerprint(pending['before_job']),fingerprint(pending['after_job'])),
                    'MAINTENANCE_EXECUTION_RECOVERY_JOB_CHANGED')
            original=pending['before_job']
            phase=original.get('failure_phase') if original['phase']=='attention_required' else original['phase']
        require(phase in PRESTART and row['phase'] in ('stop_requested','paused','attention_required','recovery_prepared'),
                'MAINTENANCE_EXECUTION_START_NOT_REPLAYABLE')
        require(record.get('receipt_sha256') is None and record.get('return_evidence') is None,
                'MAINTENANCE_EXECUTION_START_NOT_REPLAYABLE')
        manifest, fixed=self.controller.fixed()
        require(manifest==record['manifest'] and fixed==record['fixed_evidence'] and
                self.controller.identity()['boot_id']==record['boot_id'] and
                self.controller.history(self.directory)==record['history_sha256'] and
                self.controller.snapshot(self.directory)==manifest['binding'], 'MAINTENANCE_EXECUTION_RECOVERY_BINDING_CHANGED')
        current=self.r.service.owned(manifest,self.controller.backend)
        require(self.r.service.inactive(current) and self.controller.backend.cgroup_empty(current)
                and current.get('Job') in ('','0',None) and not self.r.service.enabled(manifest),
                'MAINTENANCE_EXECUTION_WORKER_EXIT_REQUIRED')
        control=store.control(); paused=record['paused_control']
        expected=dict(paused)
        if record['phase']=='attention_required':expected['recovery']=record.get('code')
        if pending:expected=pending['before_control']
        require(control==expected or pending and control==pending['after_control'],
                'MAINTENANCE_EXECUTION_CONTROL_CHANGED')
        require(store.optional('boot.json')==record['prior_receipt'], 'MAINTENANCE_EXECUTION_RECEIPT_CHANGED')
        status=store.optional('status.json')
        if record.get('stopped_status') is not None:
            require(status==record['stopped_status'], 'MAINTENANCE_EXECUTION_STOPPED_STATUS_CHANGED')
        if pending:require(status==pending['status'], 'MAINTENANCE_EXECUTION_STOPPED_STATUS_CHANGED')
        return dict(mode='prepare_explicit_return', row_sha256=fingerprint(row),job_sha256=fingerprint(record),
                    control=control,provider_status=current,status=status,boot_receipt=store.optional('boot.json'),
                    discovery=row['discovery'])

    def recovery_plan(self, job):
        row=self.load(job)
        before=self.recovery_snapshot(row)
        after=self.recovery_snapshot(self.load(job))
        require(before==after,'MAINTENANCE_EXECUTION_RECOVERY_CHANGED')
        contract=dict(job_id=job, snapshot=before, executor_sha256=self.source_sha())
        return dict(result='passed',job_id=job,recovery_review_id=fingerprint(contract),
                    recovery_mode=before['mode'],review_authorizes_execution=False,
                    stop_or_start_resubmission_supported=False,worker_will_be_started=False,
                    next_step='explicit_recover_with_current_recovery_review_id'),row,before

    def recover(self, job, expected):
        with self.lock():
            with self.r.ws.file_lock(self.directory/'service-management.lock','SERVICE_MANAGEMENT_BUSY'):
                public,initial,initial_snapshot=self.recovery_plan(job)
                require(public['recovery_review_id']==expected, 'MAINTENANCE_EXECUTION_RECOVERY_PLAN_CHANGED')
                closing=initial_snapshot['mode']=='close_unsubmitted'
                # An unsubmitted audit closes without touching the running
                # worker, which legitimately holds the automatic lease.
                with nullcontext() if closing else self.r.ws.automatic_lease(self.directory):
                    with nullcontext() if closing else self.r.worker.change.change_lock(self.directory):
                        public,row,snapshot=self.recovery_plan(job)
                        require(public['recovery_review_id']==expected, 'MAINTENANCE_EXECUTION_RECOVERY_PLAN_CHANGED')
                        self.durable[job]=deepcopy(row)
                        if snapshot['mode']=='close_unsubmitted':
                            with self.controller.store.lock():
                                require(self.recovery_snapshot(row)==snapshot,'MAINTENANCE_EXECUTION_RECOVERY_CHANGED')
                                self.save(row,'closed_unsubmitted')
                            return dict(result='passed',job_id=job,phase='closed_unsubmitted',
                                        worker_control_requested=False,worker_started=False,operation_replayed=False)
                        store=self.controller.store
                        with store.lock():
                            require(self.recovery_snapshot(row)==snapshot,'MAINTENANCE_EXECUTION_RECOVERY_CHANGED')
                            tx=row.get('recovery_transaction')
                            if tx is None:
                                before=self.controller.load(job); target=deepcopy(before)
                                target.update(phase='ready_to_return',updated_at=self.core.boot.worker_now(),
                                              stopped_status=deepcopy(snapshot['status']),
                                              operation_outcome={'result':'passed','child_processes_stopped':True},
                                              explicit_recovery={'prior_phase':before['phase'],
                                                  'prior_job_sha256':fingerprint(before),
                                                  'review_id':expected,'no_control_resubmission':True})
                                tx=dict(before_job=before,after_job=target,before_control=snapshot['control'],
                                        after_control=before['paused_control'],status=snapshot['status'])
                                row['recovery_transaction']=tx; self.save(row,'recovery_prepared')
                            current=self.controller.load(job)
                            require(current in (tx['before_job'],tx['after_job']) and
                                    store.control() in (tx['before_control'],tx['after_control']),
                                    'MAINTENANCE_EXECUTION_RECOVERY_CHANGED')
                            if current!=tx['after_job']:self.r.ws.write(self.controller.path(job),tx['after_job'])
                            self.hook('recovery:job_written')
                            require(self.load(job)==row and self.controller.load(job)==tx['after_job'] and
                                    store.control() in (tx['before_control'],tx['after_control']),
                                    'MAINTENANCE_EXECUTION_RECOVERY_CHANGED')
                            if store.control()!=tx['after_control']:self.r.ws.write(store.path/'control.json',tx['after_control'])
                            self.hook('recovery:control_written')
                            require(self.controller.load(job)==tx['after_job'] and store.control()==tx['after_control'],
                                    'MAINTENANCE_EXECUTION_RECOVERY_CHANGED')
                            # Full before/after records remain an audit; no original failure is erased.
                            row['recovery_audit']=row.pop('recovery_transaction'); self.save(row,'recovered')
                        return dict(result='passed',job_id=job,phase='recovered',desired_state='paused',
                                    autostart_enabled=False,worker_started=False,operation_replayed=False,
                                    explicit_return_required=True,next_step='explicit_return_with_fresh_readback')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('plan','status','stop','return','check','recovery-plan','recover'))
    parser.add_argument('--state-dir',type=Path,default=Path.home()/'.local/state/agent-project-ops/server')
    parser.add_argument('--langfuse-state-dir',type=Path,default=Path.home()/'.local/state/agent-project-langfuse')
    parser.add_argument('--job-id')
    parser.add_argument('--expect-plan')
    parser.add_argument('--expect-recovery')
    args=parser.parse_args()
    report=dict(schema=1,stage=STAGE,action=args.action,checked_at=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        operation=OPERATION,worker_only=True,backup_executed=False,restore_executed=False,inference_requested=False,
        business_service_control_requested=False,image_build_or_pull_requested=False,linger_changed=False,
        actual_reboot_acceptance='deferred_by_user',kubernetes_acceptance='deferred',
        capabilities=dict(plan=True,status=True,stop=True,return_worker=True,check=True,recovery_plan=True,recover=True,backup=False),
        production_cli_available=True,activation_supported=True,
        application_readback_requested=args.action in ('return','check'),automatic_recovery_supported=False,
        native_adapter_mutation_acceptance=False)
    executor=None
    try:
        require((args.job_id is not None)==(args.action in ('return','check','recovery-plan','recover') or args.action=='status' and args.job_id is not None),
                'MAINTENANCE_EXECUTION_JOB_ARGUMENT_REQUIRED')
        require((args.expect_plan is not None)==(args.action=='stop'),'MAINTENANCE_EXECUTION_PLAN_ARGUMENT_REQUIRED')
        require((args.expect_recovery is not None)==(args.action=='recover'),'MAINTENANCE_EXECUTION_RECOVERY_ARGUMENT_REQUIRED')
        if args.job_id is not None:
            try:valid_id=str(uuid.UUID(args.job_id))==args.job_id
            except (ValueError,AttributeError):valid_id=False
            require(valid_id,'MAINTENANCE_EXECUTION_ID_INVALID')
        for token in (args.expect_plan,args.expect_recovery):
            require(token is None or re.fullmatch('[0-9a-f]{64}',token),'MAINTENANCE_EXECUTION_REVIEW_ID_INVALID')
        executor=Execute(args.state_dir,args.langfuse_state_dir)
        if args.action=='plan':result=executor.plan()[0]
        elif args.action=='status':result=executor.status(args.job_id)
        elif args.action=='stop':result=executor.stop(args.expect_plan)
        elif args.action=='return':result=executor.return_worker(args.job_id)
        elif args.action=='check':result=executor.check(args.job_id)
        elif args.action=='recovery-plan':result=executor.recovery_plan(args.job_id)[0]
        else:result=executor.recover(args.job_id,args.expect_recovery)
        report.update(result)
    except Exception as exc:
        code=getattr(exc,'code',None)
        report.update(result='attention_required',code=code if isinstance(code,str) and
                      re.fullmatch('[A-Z][A-Z0-9_]{0,95}',code) else 'MAINTENANCE_EXECUTION_UNAVAILABLE',
                      next_step='inspect_status_preserve_journals_do_not_repeat_control')
        if executor:
            diagnostic=executor.m.safe_docker_diagnostic(getattr(exc,'docker_diagnostic',None))
            if diagnostic:report['docker_diagnostic']=diagnostic
            service=executor.r.adapter.command_details(getattr(exc,'service_command',None))
            if service:report['service_command']=service
    if executor:
        report['worker_service_control_commands']=executor.controller.backend.control_commands
        report['metadata_command_counts']=dict(adapter=executor.review.reader.calls+executor.review.docker.calls+
            executor.review.docker.base.calls,includes_core_metadata_queries=False)
        diagnostic=executor.m.safe_docker_diagnostic(getattr(executor,'last_docker_diagnostic',None))
        if diagnostic:report['docker_diagnostic']=diagnostic
    report['lifecycle_writes_requested']=args.action in ('stop','return','check','recover')
    report['runtime_code_write_requested']=False
    if args.action in ('plan','status','recovery-plan'):
        report['effects']=dict(files_written=[],configuration_written=False,service_control_requested=False,
            application_credential_contents_read=False,application_api_requests=0,backup_executed=False,
            restore_executed=False,inference_requested=False,linger_changed=False)
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['result']=='passed' else 2


if __name__=='__main__':sys.exit(main())
