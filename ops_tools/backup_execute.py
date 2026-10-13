#!/usr/bin/env python3
"""Measured, reviewed capture; explicit service recovery and worker return.

The public measure action is blocked pending a reviewed maintenance survey. Plan never authorizes
capture. Capture requires a fresh review and two explicit operator attestations.
No production restore, identity relocation, image pull/build or automatic retry.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops_tools import backup_setup as setup, backup_plan as bp, backup_measure as measure
from ops_tools import maintenance_execute as worker
from ops_candidates.backup_generation_20261001 import generation as g
from ops_candidates.cold_storage_20260930 import storage as s

require = setup.require
KIND = 'reviewed-production-backup-execution-v1'
OPERATION = 'encrypted_generation_capture_explicit_return'
MEASUREMENT_KIND = 'production-backup-size-measurement-v1'
MAX_AGE = 900
LOADED = {name:setup.sha(ROOT/name) for name in ('ops_tools/backup_execute.py','ops_tools/backup_measure.py')}
PINS = {'ops_tools/backup_plan.py':'4463baf035c5b7e362768b2dfe03402910370cdfd9403fae4eaeaf4dd0363603',
        'ops_tools/backup_setup.py':'43e3c124c01ba9276690d37ec809e8e09c2b261f55cbc6081b91c53894b40613',
        'ops_tools/maintenance_execute.py':'28015d8b768ebe0eeb0e5c82fbc218c25cbe683c28e00ce467e6b4c451a6522f'}


def source_pins(root):
    setup.reviewed(root)
    for name, digest in PINS.items():
        require(setup.sha(setup.safe(root/name)) == digest, 'BACKUP_EXECUTION_DEPENDENCY_CHANGED')
    own={name:setup.sha(setup.safe(root/name)) for name in LOADED}
    require(own==LOADED,'BACKUP_EXECUTION_LOADED_SOURCE_CHANGED')
    return dict(PINS, **own)


def binary_identity(path):
    path = Path(path).resolve(); info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o022
            and os.access(path, os.X_OK), 'BACKUP_DOCKER_BINARY_UNSAFE')
    return dict(path=str(path), signature=g.c.signature(info), sha256=setup.sha(path))


@contextmanager
def lock(path):
    path = setup.safe(path)
    fd = os.open(path, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        setup.private(path, False)
        try:fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise setup.Failure('BACKUP_EXECUTION_BUSY') from None
        yield fd
    finally:os.close(fd)


class Docker(measure.MetadataDocker):
    phase='connected_metadata'
    def inspect(self, expected):
        row = super().inspect(expected)
        row['mounts'] = sorted(row['mounts'], key=lambda x:json.dumps(x,sort_keys=True,separators=(',',':')))
        row['state'] = {k:row['state'][k] for k in ('Running','Status','Pid','StartedAt','FinishedAt','ExitCode','OOMKilled')}
        return row


class ConnectedPlan(bp.Plan):
    """Retain the proposals that the existing read-only plan already checked."""
    def snapshot(self):
        result = super().snapshot(); self.captured_generation = deepcopy(result[0]); return result

    def connected(self):
        original = self.review.controller.plan; box = []
        def retain(*args, **kwargs):
            value = original(*args, **kwargs); box[:] = [deepcopy(value)]; return value
        self.review.controller.plan = retain
        try:public = self.plan()
        finally:self.review.controller.plan = original
        require(len(box) == 1, 'BACKUP_EXECUTION_MAINTENANCE_PLAN_REQUIRED')
        generation = self.captured_generation
        contract = bp.contract(generation, {'review_id':public['maintenance_review_id']}, self.preparation)
        contract.update(adapter_sha256=setup.sha(self.root/'ops_tools/backup_plan.py'),
                        setup_sha256=setup.sha(self.root/'ops_tools/backup_setup.py'))
        require(bp.fingerprint(contract) == public['review_id'], 'BACKUP_EXECUTION_CONNECTION_CHANGED')
        binary = binary_identity(self.review.reader.paths['docker'])
        backend = Docker(generation['cold']['host'], binary['path'])
        # The metadata-only plan deliberately projected State. The executor
        # needs FinishedAt/ExitCode/OOM to prove a clean stop, before any effect.
        for role, row in generation['cold']['services'].items():
            current = backend.inspect(row)
            require(s.fixed(current) == s.fixed(row) and current['health'] == row['health']
                    and all(current['state'].get(k) == v for k,v in row['state'].items()),
                    'BACKUP_EXECUTION_SERVICE_CHANGED')
            require(all(k in current['state'] for k in ('FinishedAt','ExitCode','OOMKilled')),
                    'BACKUP_EXECUTION_STATE_INCOMPLETE')
            generation['cold']['services'][role] = current
        generation['cold'].pop('review_id'); generation['cold']['review_id'] = g.c.fingerprint(generation['cold'])
        generation.pop('review_id'); generation['review_id'] = g.c.fingerprint(generation)
        require(s.Operation(generation['cold'], backend, None, generation['cold']['recipients']).inventory()
                == box[0]['inventory'], 'BACKUP_EXECUTION_INVENTORY_CHANGED')
        return public, box[0], deepcopy(self.review.current), generation, binary


def binding(generation):
    value = deepcopy(generation); value.pop('id'); value.pop('review_id')
    value['cold'].pop('id'); value['cold'].pop('review_id')
    return value


def contract(proposal, discovery, generation, measurement, policy, pins, binary):
    return dict(proposal={k:v for k,v in proposal.items() if k!='id'}, discovery=discovery,
                generation=binding(generation), measurement_sha256=g.c.fingerprint(measurement),
                policy=policy, source_pins=pins, docker_binary=binary, operation=OPERATION)


class StreamScope:
    """Lease/control/journal per block; full inventory at least every 2 seconds."""
    def __init__(self, scope, guard):self.scope,self.guard,self.last = scope,guard,0
    @property
    def descriptor(self):return self.scope.descriptor
    @property
    def active(self):return self.scope.active
    def checkpoint(self, force=False):
        require(self.active, 'BACKUP_EXECUTION_SCOPE_CLOSED'); os.fstat(self.descriptor)
        parent, record = self.scope.parent, self.scope.record
        require(parent.store.control() == record['paused_control'] and parent.load(record['id']) == record,
                'BACKUP_EXECUTION_PAUSE_CHANGED')
        if force or time.monotonic()-self.last >= 2:
            self.scope.checkpoint(); self.guard(); self.last = time.monotonic()


class Generation(g.Generation):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs); self.last_full=0
    def guard(self,quiet=True):
        self.scope.checkpoint(); require(time.monotonic()<self.until,'GENERATION_DEADLINE_OR_PAUSE')
        require(g.c.fingerprint(self.p)==self.original['review_id'],'GENERATION_REVIEW_CHANGED')
        if self.raw is not None:
            require(setup.private(self.folder/'generation.json',False).read_bytes()==self.raw,'GENERATION_JOURNAL_CHANGED')
        if time.monotonic()-self.last_full>=2:
            super().guard(quiet); self.last_full=time.monotonic()
    def encrypt(self,*args,**kwargs):
        self.last_full=0; self.scope.checkpoint(True); value=super().encrypt(*args,**kwargs)
        self.last_full=0; self.guard(); self.scope.checkpoint(True); return value
    def __call__(self,scope):
        def boundary(phase):
            if phase=='before_commit':self.last_full=0; self.scope.checkpoint(True); self.guard(False)
        self.hook=boundary; return super().__call__(scope)


class Execute(worker.Execute):
    def __init__(self,directory,langfuse,home,root=ROOT):
        self.connected = ConnectedPlan(directory,langfuse,home,root)
        self.review = self.connected.review; self.m = self.connected.module
        self.root,self.home,self.r,self.core = root,home,self.review.r,self.review.core
        self.directory,self.controller = self.review.directory,self.review.controller
        self.control = Path(self.connected.preparation['paths']['control'])
        self.folder = self.control/'executions'; self.durable={}; self.hook=lambda phase:None
        self.generation=None; self.backend=None; self.binding=None
        self.original_snapshot=self.controller.snapshot

    def source_sha(self):return g.c.fingerprint(source_pins(self.root))

    def load(self,job):
        row=setup.json_read(self.path(job))
        require(row.get('schema')==1 and row.get('kind')==KIND and row.get('operation')==OPERATION
                and row.get('id')==job and row.get('directory')==str(self.directory)
                and row.get('phase') in worker.PHASES,'BACKUP_EXECUTION_RECORD_INVALID')
        require(row['executor_sha256']==self.source_sha(),'BACKUP_EXECUTION_SOURCE_CHANGED')
        require(row['review_id']==g.c.fingerprint(contract(row['proposal'],row['discovery'],row['generation'],
                row['measurement'],row['policy'],source_pins(self.root),row['docker_binary'])),
                'BACKUP_EXECUTION_REVIEW_CHANGED')
        return row

    def lock(self):return lock(self.control/'execution.lock')

    def fresh(self):
        require(all(row['phase'] in ('returned','closed_unsubmitted') for row in self.records()),
                'BACKUP_EXECUTION_UNRESOLVED_JOB')
        require(setup.prepared(self.home,self.root)==self.connected.preparation,'BACKUP_PREPARATION_CHANGED')
        return self.connected.connected()

    def measure(self,seconds=120):
        require(type(seconds) is int and 1<=seconds<=120,'BACKUP_SURVEY_SECONDS_INVALID')
        # An active worker can retain a short-lived survey helper in its
        # protected baseline and then fail when that helper exits. A paused
        # worker alone is also insufficient: the survey needs a separately
        # reviewed maintenance lease and explicit return protocol.
        require(False,'BACKUP_MEASURE_MAINTENANCE_REQUIRED')

    def measurement(self,path,generation,binary):
        path=setup.safe(path); folder=self.control/'measurements'
        require(path.parent==folder and str(uuid.UUID(path.stem))==path.stem and path.suffix=='.json',
                'BACKUP_MEASUREMENT_PATH_INVALID')
        setup.private(folder); row=setup.json_read(path)
        require(set(row)=={'schema','kind','id','measured_at','base_generation','preparation','source_pins',
                           'docker_binary','stores','files','images'} and row['schema']==1
                and row['kind']==MEASUREMENT_KIND and row['id']==path.stem,'BACKUP_MEASUREMENT_INVALID')
        require(type(row['measured_at']) in (int,float) and 0<=time.time()-row['measured_at']<=MAX_AGE,
                'BACKUP_MEASUREMENT_EXPIRED')
        require(row['base_generation']==binding(generation) and row['preparation']==self.connected.preparation
                and row['source_pins']==source_pins(self.root) and row['docker_binary']==binary,
                'BACKUP_MEASUREMENT_BINDING_CHANGED')
        require(set(row['stores'])==set(generation['cold']['stores']) and set(row['images'])==set(generation['images']),
                'BACKUP_MEASUREMENT_SCOPE_CHANGED')
        measure.limits(row); return row

    def space(self,generation,policy):
        envelope=measure.envelope(policy,len(generation['cold']['stores']))
        space=bp.capacity(Path(generation['cold']['destination']),0,self.review.cap.bp)
        fits=space['usable_after_reserve_bytes']>=envelope['reserved_workspace_bytes']
        space.update(actual_source_size_measured=True,actual_archive_size_verified=False,
                     declared_stream_budget_with_allowance_bytes=envelope['reserved_workspace_bytes'],
                     stream_budget_fits_reserved_capacity=fits)
        return space,envelope

    def plan(self,measurement_file,headroom=30):
        _,proposal,discovery,generation,binary=self.fresh()
        measured=self.measurement(measurement_file,generation,binary); policy=measure.limits(measured,headroom)
        cold=generation['cold']; cold.update(max_bytes=policy['per_storage_bytes'],max_entries=policy['max_entries'])
        cold.pop('review_id'); cold['review_id']=g.c.fingerprint(cold)
        generation.update(max_file_bytes=policy['aggregate_file_bytes'],max_image_bytes=policy['aggregate_image_export_bytes'],
                          max_entries=policy['max_entries'])
        generation.pop('review_id'); generation['review_id']=g.c.fingerprint(generation); g.validate(generation)
        space,envelope=self.space(generation,policy); pins=source_pins(self.root)
        review=g.c.fingerprint(contract(proposal,discovery,generation,measured,policy,pins,binary))
        row=dict(schema=1,kind=KIND,id=proposal['id'],directory=str(self.directory),operation=OPERATION,
                 review_id=review,executor_sha256=self.source_sha(),proposal=proposal,discovery=discovery,
                 generation=generation,measurement=measured,policy=policy,docker_binary=binary)
        return dict(result='passed' if space['stream_budget_fits_reserved_capacity'] else 'blocked',review_id=review,
            review_authorizes_execution=False,limits=policy,envelope=envelope,capacity=space,
            capture_available=space['stream_budget_fits_reserved_capacity'],identity_custody_verified=False,
            application_drain_verified=False,source_size_is_live_estimate=True,backup_ready=False,
            next_step='review_identity_custody_and_application_drain_before_explicit_capture'),row

    def configure(self,discovery):
        self.binding=deepcopy(discovery)
        row=self.active_row; generation=row['generation']
        if self.backend is None:self.backend=Docker(generation['cold']['host'],row['docker_binary']['path'])
        def inventory():
            self.metadata_guard(row)
            current=(self.generation.inventory() if self.generation is not None else
                     s.Operation(generation['cold'],self.backend,None,generation['cold']['recipients']).inventory())
            require(current==discovery['inventory'],'BACKUP_EXECUTION_INVENTORY_CHANGED'); return current
        self.controller.inventory=inventory
        self.controller.snapshot=lambda directory:self.paused_snapshot(directory,row)

    def metadata_guard(self,row):
        require(self.load(row['id'])==row,'BACKUP_EXECUTION_RECORD_CHANGED')
        require(binary_identity(row['docker_binary']['path'])==row['docker_binary'],'BACKUP_DOCKER_BINARY_CHANGED')
        require(setup.prepared(self.home,self.root)==self.connected.preparation,'BACKUP_PREPARATION_CHANGED')
        for path,wanted in row['discovery']['files'].items():
            info=setup.safe(Path(path)).stat()
            require([info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]==wanted,
                    'BACKUP_EXECUTION_SOURCE_METADATA_CHANGED')
        g.validate(row['generation'])

    def paused_snapshot(self,directory,row):
        # runtime_snapshot requires ops-web healthy, which cold capture stops.
        # Reconstruct its identical binding from fresh code/owner/daemon/scope
        # metadata; never use the saved binding as the current observation.
        require(directory==self.directory,'BACKUP_EXECUTION_DIRECTORY_CHANGED')
        self.metadata_guard(row); owner=self.r.ws.read(directory/'owner.json')
        require(owner['project_root']==str(self.root) and owner['uid']==os.getuid() and owner['gid']==os.getgid(),
                'BACKUP_EXECUTION_OWNER_CHANGED'); self.backend.verify_host()
        expected=row['generation']['cold']['services']['ops-web']; current=self.backend.inspect(expected)
        require(s.fixed(current)==s.fixed(expected),'BACKUP_EXECUTION_OPS_IDENTITY_CHANGED')
        return dict(directory=str(directory),owner=owner,code_sha256=self.r.worker.code_hash(self.root),
                    python=str(Path(sys.executable).resolve()),python_version=list(sys.version_info[:3]),
                    docker_path=row['docker_binary']['path'],daemon_id=row['generation']['cold']['host']['daemon_id'],
                    ops_container_id=current['container_id'],ops_image_id=current['image_id'])

    def matched(self,row):
        self.active_row=row; job=self.controller.load(row['id'])
        require(all(job.get(k)==v for k,v in row['proposal'].items()),'BACKUP_EXECUTION_JOB_CHANGED')
        self.configure(row['discovery']); self.controller.inventory()
        if row['phase'] in ('paused','recovered','return_requested'):self.ready(row)
        return job

    def disk_guard(self,row):
        self.metadata_guard(row); space,envelope=self.space(row['generation'],row['policy'])
        written=0; folder=self.generation.folder if self.generation is not None else None
        if folder is not None:
            for parent,dirs,names in os.walk(folder,followlinks=False):
                require(not any(Path(parent,name).is_symlink() for name in dirs+names),'BACKUP_GENERATION_SYMLINK_REFUSED')
                for name in names:written+=Path(parent,name).stat().st_size
        required=3*envelope['ciphertext_upper_bytes']+max(0,envelope['ciphertext_upper_bytes']-written)+8*1024**2
        require(space['usable_after_reserve_bytes']>=required,'BACKUP_EXECUTION_CAPACITY_CHANGED')

    def build_generation(self,row):
        prep=self.connected.preparation
        tool=g.a.Age(Path(prep['paths']['tools'])/'age',prep['age_sha256'])
        return Generation(row['generation'],self.backend,tool)

    def live_receipt(self,row):
        gen=self.generation; receipt=setup.json_read(gen.folder/'receipt.json')
        require(gen.record['phase']=='captured' and not gen.op.helpers and not self.backend.owned_helpers
                and receipt['encryption']=='age_v1' and receipt['review_id']==row['generation']['review_id']
                and receipt['artifacts']=={k:{x:v[x] for x in ('path','ciphertext_sha256','ciphertext_bytes')}
                                          for k,v in gen.artifacts.items()},'BACKUP_EXECUTION_LIVE_RECEIPT_CHANGED')
        require(g.inspect(gen.folder,receipt)['result']=='captured_integrity_verified',
                'BACKUP_EXECUTION_CIPHERTEXT_CHANGED')
        return receipt

    def capture(self,expected,measurement_file,headroom=30,*,identity_custody=False,application_drain=False):
        require(identity_custody is True and application_drain is True,'BACKUP_EXECUTION_ATTESTATIONS_REQUIRED')
        public,_=self.plan(measurement_file,headroom)
        require(public['review_id']==expected,'BACKUP_EXECUTION_PLAN_CHANGED')
        require(public['capture_available'],'BACKUP_EXECUTION_CAPACITY_INSUFFICIENT')
        with self.lock():
            public,row=self.plan(measurement_file,headroom)
            require(public['review_id']==expected and public['capture_available'],'BACKUP_EXECUTION_PLAN_CHANGED')
            row['attestations']=dict(identity_custody_acknowledged=True,application_drain_acknowledged=True,
                                     identity_custody_verified=False,application_drain_verified=False)
            self.save(row,'prepared'); self.active_row=row; self.configure(row['discovery']); self.connect_control()
            self.generation=self.build_generation(row)
            self.disk_guard(row); self.save(row,'stop_requested')
            def callback(original):
                scope=StreamScope(original,lambda:self.disk_guard(row))
                try:
                    result=self.generation(scope); scope.checkpoint(True)
                    receipt=self.live_receipt(row)
                    row['trusted_receipt']=receipt; self.save(row,'stop_requested'); return result
                finally:
                    for ident,spec in list(self.backend.owned_helpers.items()):
                        self.backend.helper(ident,spec); self.backend.abort(ident,spec)
                        if self.generation.op:self.generation.op.helpers.pop(ident,None)
            try:result=self.controller.run(row['proposal'],callback)
            finally:self.backend.scope=None; self.backend.until=None
            row['outcome']=deepcopy(result)
            self.save(row,'paused' if result['result']=='passed' else 'attention_required')
            return dict(result,generation_id=row['generation']['id'],explicit_return_required=True,
                        encrypted_capture_completed=result['result']=='passed',backup_ready=False,
                        next_step='explicit_return' if result['result']=='passed' else 'read_only_recovery_plan')

    def services(self,row):
        self.active_row=row; self.configure(row['discovery']); self.controller.inventory()
        return {role:self.backend.inspect(expected) for role,expected in row['generation']['cold']['services'].items()}

    def ready(self,row):
        for role,current in self.services(row).items():
            state=current['state']
            require(state['Running'] is True and state['Pid']>0 and not state['OOMKilled']
                    and current['health'] in ('healthy','none'),'BACKUP_EXECUTION_SERVICES_NOT_READY')
        if 'trusted_receipt' in row:
            folder=Path(row['generation']['cold']['destination'])/row['generation']['id']
            require(g.inspect(folder,row['trusted_receipt'])['result']=='captured_integrity_verified',
                    'BACKUP_EXECUTION_CIPHERTEXT_CHANGED')

    def return_worker(self,job):
        row=self.load(job); self.ready(row)
        return super().return_worker(job)

    def check(self,job):
        row=self.load(job); self.ready(row); return super().check(job)

    def recovery_snapshot(self,row):
        snapshot=super().recovery_snapshot(row)
        return dict(snapshot,services=self.services(row))

    def recovery_plan(self,job):
        public,row,snapshot=super().recovery_plan(job)
        before=self.services(row); after=self.services(row)
        require(before==after==snapshot['services'],'BACKUP_RECOVERY_SERVICE_CHANGED')
        submissions=row.get('service_recovery_submissions',{})
        cold=row['generation']['cold']; journal_path=Path(cold['destination'])/row['generation']['id']/'storage'/cold['id']/'operation.json'
        journal=setup.json_read(journal_path) if journal_path.exists() or journal_path.is_symlink() else None
        if journal is not None:
            require(journal.get('kind')==s.KIND and journal.get('id')==cold['id'] and journal.get('schema')==1
                    and journal.get('helpers')=={},'BACKUP_RECOVERY_HELPER_EXIT_REQUIRED')
            require(isinstance(journal.get('stopped'),list) and isinstance(journal.get('started'),list)
                    and set(journal['stopped'])<=set(cold['services']) and set(journal['started'])<=set(journal['stopped']),
                    'BACKUP_RECOVERY_JOURNAL_INVALID')
            # A failed/unknown create may have no returned ID. Inspect only its
            # recorded exact name; never adopt or remove an old helper.
            if journal.get('helper_name') and hasattr(self.backend,'call'):
                name=journal['helper_name']; require(re.fullmatch('ops-cold-[a-z0-9-]+',name),
                                                    'BACKUP_RECOVERY_JOURNAL_INVALID')
                rc,out,_=self.backend.call(['ps','-aq','--no-trunc','--filter','name=^/'+name+'$'])
                require(rc==0 and not out.strip(),'BACKUP_RECOVERY_HELPER_EXIT_REQUIRED')
        for role,current in before.items():
            state=current['state']; baseline=row['generation']['cold']['services'][role]['state']
            if state['Running']:
                require(state['Pid']>0 and not state['OOMKilled'] and current['health'] in ('healthy','none'),
                        'BACKUP_RECOVERY_SERVICE_UNHEALTHY')
                if role in submissions:
                    require(state['StartedAt']!=submissions[role]['before_started_at'],
                            'BACKUP_RECOVERY_START_OUTCOME_UNKNOWN')
            else:
                require(snapshot['mode']!='close_unsubmitted' and state['Status']=='exited' and state['Pid']==0,
                        'BACKUP_RECOVERY_SERVICE_STATE_REFUSED')
                require(role not in submissions,'BACKUP_RECOVERY_START_OUTCOME_UNKNOWN')
                require(state['StartedAt']==baseline['StartedAt'],'BACKUP_RECOVERY_SERVICE_RESTARTED')
                require(journal is not None and (role in journal['stopped'] or
                        journal.get('service')==role and journal.get('phase') in ('stop_intent','attention_required')),
                        'BACKUP_RECOVERY_STOP_NOT_PROVEN')
                require(role not in journal['started'] and not (journal.get('service')==role and
                        (journal.get('phase')=='start_intent' or journal.get('code')=='COLD_START_OUTCOME_UNKNOWN')),
                        'BACKUP_RECOVERY_START_OUTCOME_UNKNOWN')
        public['worker_recovery_review_id']=public['recovery_review_id']
        public['recovery_review_id']=g.c.fingerprint(dict(worker=public['worker_recovery_review_id'],services=before))
        public.update(services_to_start=[r for r in reversed(row['generation']['cold']['stop_order'])
                                        if not before[r]['state']['Running']],
                      failed_capture_will_be_replayed=False,worker_will_be_started=False)
        snapshot=dict(snapshot,services=before); return public,row,snapshot

    @contextmanager
    def recovery_scope(self,fd,guard,cold):
        scope=measure.Scope(fd,guard)
        self.backend.bind(cold,scope,time.monotonic()+cold['seconds'])
        try:yield scope
        finally:
            scope.active=False; self.backend.scope=None; self.backend.until=None

    def recover(self,job,expected):
        # Start scoped services once under an inherited automatic lease. The
        # worker-only recovery transaction then clears only its exact latch.
        with self.lock():
            with self.r.ws.file_lock(self.directory/'service-management.lock','SERVICE_MANAGEMENT_BUSY'):
                public,row,snapshot=self.recovery_plan(job)
                require(public['recovery_review_id']==expected,'BACKUP_RECOVERY_PLAN_CHANGED')
                if snapshot['mode']!='close_unsubmitted':
                    with self.r.ws.automatic_lease(self.directory) as fd:
                        self.durable[job]=deepcopy(row); self.active_row=row
                        def guard():
                            self.metadata_guard(row); self.controller.inventory()
                            super(Execute,self).recovery_snapshot(row)
                        cold=row['generation']['cold']
                        with self.recovery_scope(fd,guard,cold):
                            for role in public['services_to_start']:
                                guard(); current=self.backend.inspect(cold['services'][role])
                                require(current==snapshot['services'][role],'BACKUP_RECOVERY_SERVICE_CHANGED')
                                row.setdefault('service_recovery_submissions',{})[role]=dict(
                                    before_started_at=current['state']['StartedAt'],review_id=expected)
                                self.save(row,row['phase']); self.backend.start(cold['services'][role])
                                deadline=time.monotonic()+120
                                while True:
                                    guard(); current=self.backend.inspect(cold['services'][role]); state=current['state']
                                    require(state['Running'] and state['Pid']>0 and not state['OOMKilled'],
                                            'BACKUP_RECOVERY_START_OUTCOME_UNKNOWN')
                                    if current['health'] in ('healthy','none'):break
                                    require(current['health']=='starting' and time.monotonic()<deadline,
                                            'BACKUP_RECOVERY_SERVICE_UNHEALTHY'); time.sleep(.05)
                                require(state['StartedAt']!=snapshot['services'][role]['state']['StartedAt'],
                                        'BACKUP_RECOVERY_START_NOT_FRESH')
                            self.ready(row)
        # The inherited transaction dynamically calls our recovery_plan, so
        # bind its fresh combined review after all service observations changed.
        refreshed=self.recovery_plan(job)[0]
        return super().recover(job,refreshed['recovery_review_id'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('measure','plan','capture','status','return','check','recovery-plan','recover'))
    parser.add_argument('--state-dir',type=Path,default=Path.home()/'.local/state/agent-project-ops/server')
    parser.add_argument('--langfuse-state-dir',type=Path,default=Path.home()/'.local/state/agent-project-langfuse')
    parser.add_argument('--measurement-file',type=Path); parser.add_argument('--headroom-percent',type=int,default=30)
    parser.add_argument('--survey-seconds',type=int,default=120)
    parser.add_argument('--review-id'); parser.add_argument('--job-id'); parser.add_argument('--recovery-review-id')
    parser.add_argument('--ack-identity-custody',action='store_true'); parser.add_argument('--ack-application-drain',action='store_true')
    args=parser.parse_args(argv)
    report=dict(schema=1,stage='O04-measured-production-backup-execution',action=args.action,
                backup_ready=False,restore_executed=False,identity_contents_read=False,
                automatic_recovery_supported=False,production_restore_supported=False)
    try:
        require((args.measurement_file is not None)==(args.action in ('plan','capture')),'BACKUP_MEASUREMENT_ARGUMENT_REQUIRED')
        require((args.job_id is not None)==(args.action in ('return','check','recovery-plan','recover')
                or args.action=='status' and args.job_id is not None),'BACKUP_JOB_ARGUMENT_REQUIRED')
        require((args.review_id is not None)==(args.action=='capture'),'BACKUP_REVIEW_ARGUMENT_REQUIRED')
        require((args.recovery_review_id is not None)==(args.action=='recover'),'BACKUP_RECOVERY_ARGUMENT_REQUIRED')
        require(args.action=='capture' or not (args.ack_identity_custody or args.ack_application_drain),
                'BACKUP_ATTESTATION_ARGUMENT_REFUSED')
        executor=Execute(args.state_dir,args.langfuse_state_dir,Path.home())
        if args.action=='measure':result=executor.measure(args.survey_seconds)
        elif args.action=='plan':result=executor.plan(args.measurement_file,args.headroom_percent)[0]
        elif args.action=='capture':result=executor.capture(args.review_id,args.measurement_file,args.headroom_percent,
            identity_custody=args.ack_identity_custody,application_drain=args.ack_application_drain)
        elif args.action=='status':result=executor.status(args.job_id)
        elif args.action=='return':result=executor.return_worker(args.job_id)
        elif args.action=='check':result=executor.check(args.job_id)
        elif args.action=='recovery-plan':result=executor.recovery_plan(args.job_id)[0]
        else:result=executor.recover(args.job_id,args.recovery_review_id)
        report.update(result)
    except Exception as exc:
        code=getattr(exc,'code','BACKUP_EXECUTION_FAILED')
        report.update(result='attention_required',code=code if isinstance(code,str) and re.fullmatch('[A-Z][A-Z0-9_]{0,119}',code)
                      else 'BACKUP_EXECUTION_FAILED',operation_replayed=False)
        diagnostic=measure.safe_diagnostic(getattr(exc,'measure_diagnostic',None))
        if diagnostic is not None:report['measure_diagnostic']=diagnostic
        query=measure.safe_query_diagnostic(getattr(exc,'docker_query_diagnostic',None))
        if query is not None:report['docker_query_diagnostic']=query
        cleanup=measure.safe_cleanup_diagnostic(getattr(exc,'measure_cleanup_diagnostic',None))
        if cleanup is not None:report['measure_cleanup_diagnostic']=cleanup
    print(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True))
    return 0 if report['result'] in ('passed','blocked') else 2


if __name__=='__main__':raise SystemExit(main())
