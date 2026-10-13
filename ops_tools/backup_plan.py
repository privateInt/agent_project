#!/usr/bin/env python3
"""Connect the reviewed production inventory to a full encrypted generation plan.

Metadata only. No source tree walk, secret body, container exec, stop/start,
worker mutation, capture or restore. A review ID does not authorize execution.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops_tools import backup_setup as setup

require = setup.require
DETAIL_FORMAT = ('[{{json .Id}},{{json .Image}},'
    '{"Running":{{json .State.Running}},"Status":{{json .State.Status}},'
    '"Pid":{{json .State.Pid}},"StartedAt":{{json .State.StartedAt}},'
    '"Health":{{if index .State "Health"}}{"Status":{{json (index .State "Health").Status}}}{{else}}null{{end}}},'
    '{{json .HostConfig.UsernsMode}},{{json .HostConfig.RestartPolicy.Name}}]')
DEPENDENCIES = {
    'lf-web':['lf-worker','lf-postgres','lf-clickhouse','lf-redis','lf-minio'],
    'lf-worker':['lf-postgres','lf-clickhouse','lf-redis','lf-minio'],
    'lf-postgres':[], 'lf-clickhouse':[], 'lf-redis':[], 'lf-minio':[],
    'ops-web':['obs-prometheus','obs-alertmanager','lf-web'],
    'obs-prometheus':['obs-alertmanager'], 'obs-alertmanager':[]}
PHASES = {'constructor','setup_metadata','source_review','maintenance_constructor','maintenance_review',
          'host_metadata','running_metadata','inventory_construction','load_prometheus','load_alertmanager',
          'load_langfuse','load_ops','container_identity','container_detail','exporter_identity','source_mapping',
          'cold_storage_plan','generation_mapping','snapshot_comparison','worker_verification',
          'preparation_verification','capacity_projection','complete'}
CHANGE_FIELDS = {
    'cold.services.membership','cold.services.identity','cold.services.mounts','cold.services.state',
    'cold.services.other_metadata','cold.stores.membership','cold.stores.binding',
    'cold.stores.identity','cold.stores.mount_topology','cold.stores.other_metadata',
    'cold.host','cold.protected','cold.destination','cold.policy','cold.other_metadata',
    'generation.files.membership','generation.files.binding','generation.files.identity',
    'generation.files.signature','generation.files.mount_topology','generation.files.other_metadata',
    'generation.images','generation.source_dispositions','generation.budgets','generation.other_metadata',
    'maintenance_review','preparation','other_contract_metadata'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name,path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def detail_failure(probe, rc=None, stdout=b'', stderr=b'', exception=None):
    # Reuse fixed reason classification with its existing operation label;
    # keep the new operation local rather than changing the pinned adapter.
    value=probe.docker_failure('running_inspect',rc,stdout,stderr,exception)
    value['operation']='container_inspect'
    return value


def safe_docker_diagnostic(probe, value):
    if isinstance(value,dict) and value.get('operation')=='container_inspect':
        projected=dict(value,operation='running_inspect')
        safe=probe.safe_docker_diagnostic(projected)
        if safe: safe['operation']='container_inspect'
        return safe
    return probe.safe_docker_diagnostic(value)


def plan_diagnostic(plan, exc):
    """Only fixed phase and exception labels; no messages, paths or traceback."""
    phase=getattr(plan,'phase','constructor')
    if phase not in PHASES: phase='constructor'
    label='json_decode_error' if isinstance(exc,json.JSONDecodeError) else (
          'attribute_error' if isinstance(exc,AttributeError) else
          'key_error' if isinstance(exc,KeyError) else
          'type_error' if isinstance(exc,TypeError) else
          'value_error' if isinstance(exc,ValueError) else
          'file_not_found' if isinstance(exc,FileNotFoundError) else
          'permission_denied' if isinstance(exc,PermissionError) else
          'os_error' if isinstance(exc,OSError) else
          'typed_failure' if getattr(exc,'code',None) is not None else 'unknown')
    return dict(phase=phase,exception_kind=label,retry_requested=False)


def snapshot_changes(before, after):
    """Fixed field groups only: no paths, roles, keys, hashes or values."""
    changed=[]
    def field(label,left,right):
        if left!=right: changed.append(label)
    def subset(value,names): return {k:v for k,v in value.items() if k in names}
    def remainder(value,names): return {k:v for k,v in value.items() if k not in names}
    def rows(label,left,right,groups):
        field(label+'.membership',set(left),set(right))
        names={k for fields in groups.values() for k in fields}
        for suffix,fields in groups.items():
            if any(subset(left[k],fields)!=subset(right[k],fields) for k in set(left)&set(right)):
                changed.append(label+'.'+suffix)
        if any(remainder(left[k],names)!=remainder(right[k],names) for k in set(left)&set(right)):
            changed.append(label+'.other_metadata')
    a,b=before['generation'],after['generation'];c,d=a['cold'],b['cold']
    rows('cold.services',c['services'],d['services'],{
        'identity':('container_id','image_id','owner','owner_label','project','service'),
        'mounts':('mounts',),'state':('state','health','userns','restart')})
    rows('cold.stores',c['stores'],d['stores'],{
        'binding':('source','target','service'),'identity':('identity','uid','gid'),
        'mount_topology':('mount_topology_sha256',)})
    for label,fields in {
        'host':('host',),'protected':('protected',),'destination':('destination','destination_identity'),
        'policy':('helper','dependencies','stop_order','seconds','stop_seconds','max_bytes','max_entries',
                  'recipients','reader_script_sha256')}.items():
        field('cold.'+label,subset(c,fields),subset(d,fields))
    cold_known={'services','stores','host','protected','destination','destination_identity','helper',
                'dependencies','stop_order','seconds','stop_seconds','max_bytes','max_entries',
                'recipients','reader_script_sha256'}
    field('cold.other_metadata',remainder(c,cold_known),remainder(d,cold_known))
    rows('generation.files',a['files'],b['files'],{
        'binding':('path','type','exclude_children'),'identity':('identity',),
        'signature':('file_signature',),'mount_topology':('mount_topology_sha256',)})
    field('generation.images',a['images'],b['images'])
    field('generation.source_dispositions',a['source_dispositions'],b['source_dispositions'])
    budgets=('max_file_bytes','max_image_bytes','max_entries')
    field('generation.budgets',subset(a,budgets),subset(b,budgets))
    field('generation.other_metadata',remainder(a,{'cold','files','images','source_dispositions',*budgets}),
          remainder(b,{'cold','files','images','source_dispositions',*budgets}))
    field('maintenance_review',before['maintenance_review_id'],after['maintenance_review_id'])
    field('preparation',before['preparation'],after['preparation'])
    field('other_contract_metadata',remainder(before,{'generation','maintenance_review_id','preparation'}),
          remainder(after,{'generation','maintenance_review_id','preparation'}))
    return dict(changed_fields=sorted(set(changed)),values_returned=False,retry_requested=False)


def safe_snapshot_diagnostic(value):
    if not isinstance(value,dict) or set(value)!={'changed_fields','values_returned','retry_requested'}:
        return None
    fields=value['changed_fields']
    if (not isinstance(fields,list) or not 1<=len(fields)<=len(CHANGE_FIELDS)
        or any(not isinstance(x,str) or x not in CHANGE_FIELDS for x in fields)
        or fields!=sorted(set(fields)) or value['values_returned'] is not False
        or value['retry_requested'] is not False): return None
    return deepcopy(value)


def docker_type(base, probe):
    class MetadataDocker(base):
        def detail(self, ident):
            require(re.fullmatch('[0-9a-f]{64}', ident), 'BACKUP_CONTAINER_ID_INVALID')
            self.calls += 1
            try:
                rc,out,err = self.runner(['docker','--host',self.base.endpoint,'container','inspect',
                                         '--format',DETAIL_FORMAT,ident],self.base.env,20)
            except Exception as exc:
                raise probe.Failure('BACKUP_DOCKER_DETAIL_FAILED',
                                    detail_failure(probe,exception=exc)) from None
            if rc:
                raise probe.Failure('BACKUP_DOCKER_DETAIL_FAILED',detail_failure(probe,rc,out,err))
            value = json.loads(out)
            require(isinstance(value,list) and len(value)==5 and value[0]==ident
                    and isinstance(value[2],dict), 'BACKUP_DOCKER_DETAIL_INVALID')
            return value

        def execute(self, *args, **kwargs):
            raise setup.Failure('BACKUP_CONTAINER_EXEC_REFUSED')

        def logical_size(self, *args, **kwargs):
            raise setup.Failure('BACKUP_SOURCE_SIZE_SCAN_REFUSED')
    return MetadataDocker


def cold_rows(inv, checked, docker, bp, running):
    rows = {}
    for name, service in inv.services.items():
        original = next(x for x in docker.rows[(service['component'],service['owner'])]
                        if x['id'] == checked[name]['id'])
        ident,image,state,userns,restart = docker.detail(original['id'])
        require(image == original['image_id'] and state.get('StartedAt') == original['started_at']
                and running.get(ident) == original['started_at'], 'BACKUP_CONTAINER_CHANGED')
        health = state.get('Health')
        require(health is None or isinstance(health,dict), 'BACKUP_HEALTH_INVALID')
        rows[name] = dict(container_id=ident,image_id=image,owner=service['owner'],
                          owner_label=bp.OWNERS[service['component']], project=original['project'],
                          service=name,mounts=sorted(deepcopy(original['mounts']),
                              key=lambda item:json.dumps(item,sort_keys=True,separators=(',',':'))),
                          userns=userns,restart=restart,
                          state={k:state[k] for k in ('Running','Status','Pid','StartedAt')},
                          health='none' if health is None else health.get('Status'))
    return rows


def extend_sources(inv, review):
    """Same declared capacity scope, with fresh owner and unit bindings."""
    cap = review.cap; bp = cap.bp
    owner = bp.read_small(review.directory/'owner.json')
    management = review.directory.parent/'auto-refresh'/owner['owner']
    require(management.is_dir(), 'BACKUP_MANAGEMENT_RECORDS_REQUIRED')
    inv.store('ops-auto-refresh-records', management, None, None)
    for sub in ('ops_platform','ops_tools','ops_candidates','infra/observability','infra/langfuse'):
        inv.store('source-'+sub.replace('/','-'), review.root/sub, None, None)
    record = bp.read_small(management/'service.json')
    require(record.get('binding',{}).get('owner') == owner
            and record['binding'].get('directory') == str(review.directory), 'BACKUP_SERVICE_OWNER_CHANGED')
    unit = bp.safe(record['unit_path']); info = bp.metadata(unit)
    require(unit.name == review.manifest['unit_name'] and info.st_uid == os.getuid()
            and not info.st_mode & 0o022, 'BACKUP_UNIT_FILE_CHANGED')
    inv.file('owned-user-unit-file',unit,private=False)
    inv.file('owned-service-record',management/'service.json')
    for i,path in enumerate(cap.host_config_paths()):
        inv.file('host-configuration-'+str(i),path,private=False)
    require(all(x.get('status')=='ok' or not x['required'] for x in inv.files),
            'BACKUP_REQUIRED_FILE_METADATA_UNAVAILABLE')


def storage_map(stores):
    result = {}
    for item in stores:
        if item.get('service'):
            result[item['name']] = dict(source=item['path'],target=item['container_target'],service=item['service'])
        runtime = item.get('runtime_subdirectory')
        if runtime:
            result[item['name']+'-runtime'] = dict(source=runtime['path'],target=runtime['target'],service=runtime['service'])
    require(len(result)==8, 'BACKUP_STORAGE_SET_INCOMPLETE')
    return result


def generation_map(generation, cold, inv, images):
    # Metadata files within a cold tree belong to that cold artifact, including
    # Ops cache and Alertmanager owner records. The original mapper coalesces
    # only actor file trees; add the exact cold dispositions here.
    host_files = []; covered = {}
    for row in inv.files:
        if row.get('status') != 'ok':
            require(not row['required'], 'BACKUP_REQUIRED_FILE_METADATA_UNAVAILABLE')
            continue
        matches = [role for role,store in cold['stores'].items()
                   if Path(row['path']).is_relative_to(store['source'])]
        require(len(matches)<=1, 'BACKUP_REQUIRED_FILE_AMBIGUOUS')
        if matches:
            covered[row['name']] = 'storage-'+matches[0]
        else:
            host_files.append(row)
    result = generation.discovered_plan(cold,inv.stores,host_files,images,
                 max_file_bytes=2*1024**3,max_image_bytes=16*1024**3,max_entries=65536)
    result['source_dispositions'].update(covered)
    result.pop('review_id'); result['review_id']=fingerprint(result)
    generation.validate(result)
    return result


def capacity(destination, maximum_plaintext, bp):
    mounts = bp.mount_table(Path('/proc/self/mountinfo').read_text())
    fs = bp.filesystem(destination,mounts)
    require(fs['fstype'] in {'ext4','ext3','ext2','xfs','btrfs','zfs','f2fs'} and not fs['readonly'], 'BACKUP_LOCAL_FILESYSTEM_REQUIRED')
    usage = os.statvfs(destination)
    available = usage.f_bavail*usage.f_frsize
    reserved = (usage.f_blocks*usage.f_frsize+9)//10
    usable = max(0,available-reserved)
    # Two retained ciphertext generations plus staging and isolated restore.
    # Age/frame/catalog allowance is proposed, not an archive measurement.
    upper = (maximum_plaintext*105+99)//100*4
    return dict(available_bytes=available,reserve_bytes=reserved,usable_after_reserve_bytes=usable,
                reserve_percent=10,proposed_generations=2,staging_copies=1,restore_copies=1,
                declared_stream_budget_with_allowance_bytes=upper,
                stream_budget_fits_reserved_capacity=usable>=upper,
                actual_source_size_measured=False,actual_archive_size_verified=False,
                filesystem={k:fs[k] for k in ('mountpoint','source','fstype') if k in fs},
                independence_from_source_disk_verified=False)


def contract(generation, maintenance, preparation):
    fixed = deepcopy(generation)
    fixed.pop('id'); fixed.pop('review_id')
    fixed['cold'].pop('id'); fixed['cold'].pop('review_id')
    return dict(generation=fixed,maintenance_review_id=maintenance['review_id'],
                preparation=preparation)


class Plan:
    def __init__(self, directory, langfuse, home, root=ROOT):
        self.root,self.home,self.langfuse = root,home,Path(langfuse)
        self.phase='setup_metadata'
        self.preparation = setup.prepared(home,root)
        self.phase='source_review'
        self.spec = setup.reviewed(root)
        self.module = load('_production_backup_maintenance_review',root/'ops_tools/maintenance_control.py')
        self.phase='maintenance_constructor'
        self.review = self.module.Review(directory,langfuse,root)
        old = self.review.docker
        self.review.docker = docker_type(type(old),self.module)()
        self.review.inventory.docker = self.review.docker
        from ops_candidates.backup_generation_20261001 import generation
        self.generation = generation

    def snapshot(self):
        r=self.review; cap=r.cap
        self.phase='host_metadata'; host=r.docker.host()
        self.phase='running_metadata'; running=r.docker.running()
        self.phase='inventory_construction'
        inv=cap.Inventory(r.root,self.langfuse,r.directory,r.docker,[]);inv.host=host
        for component in cap.bp.PROJECTS:
            self.phase='load_'+component
            getattr(inv,'load_'+component)()
        self.phase='container_identity'
        checked=inv.containers()
        require(len(checked)==len(inv.services)==9 and all(x['status']=='matched' for x in checked.values()),
                'BACKUP_OWNED_SERVICE_SET_UNVERIFIED')
        self.phase='container_detail'; services=cold_rows(inv,checked,r.docker,cap.bp,running)
        ids={x['container_id'] for x in services.values()}
        protected={k:v for k,v in running.items() if k not in ids}
        self.phase='exporter_identity'; exporters=inv.exporters()
        self.phase='source_mapping'; extend_sources(inv,r)
        images={**checked,**exporters}
        self.phase='cold_storage_plan'
        cold=self.generation.s.plan(dict(endpoint=host['endpoint'],daemon_id=host['daemon_id'],docker_root=host['docker_root']),
                 services,protected,storage_map(inv.stores),
                 dict(image_id=services['ops-web']['image_id'],python='python3'),DEPENDENCIES,
                 self.preparation['paths']['destination'],public_recipients=[self.preparation['recipient']],
                 seconds=3600,stop_seconds=60,max_bytes=8*1024**3,max_entries=65536)
        self.phase='generation_mapping'; result=generation_map(self.generation,cold,inv,images)
        # Do not freeze changing cache file timestamps or completed worker cycles.
        return result, inv, host

    def plan(self):
        r=self.review; self.phase='maintenance_review'; maintenance=r.plan()
        before,inv,host=self.snapshot()
        after,_,_=self.snapshot()
        self.phase='snapshot_comparison'
        first=contract(before,maintenance,self.preparation);second=contract(after,maintenance,self.preparation)
        if first!=second:
            exc=setup.Failure('BACKUP_GENERATION_DISCOVERY_CHANGED')
            exc.snapshot_diagnostic=snapshot_changes(first,second)
            raise exc
        self.phase='worker_verification'
        state=r.state()
        require(state.get('current_invocation_verified') is True and state.get('desired_state')=='running'
                and state.get('recovery') is None and state['provider_status']['InvocationID']
                ==maintenance['management']['provider_status']['InvocationID'], 'BACKUP_WORKER_CHANGED')
        self.phase='preparation_verification'
        require(setup.prepared(self.home,self.root)==self.preparation,'BACKUP_PREPARATION_CHANGED')
        body=contract(after,maintenance,self.preparation)
        body['adapter_sha256']=setup.sha(self.root/'ops_tools/backup_plan.py')
        body['setup_sha256']=setup.sha(self.root/'ops_tools/backup_setup.py')
        # Core limits are per artifact, not generation totals. Include framing
        # allowances rather than treating one tree/image limit as the whole set.
        metadata_allowance=after['max_entries']*8192+16*1024**2
        budget=(len(after['cold']['stores'])*(after['cold']['max_bytes']+metadata_allowance)
                +len(after['files'])*(after['max_file_bytes']+metadata_allowance)
                +len(after['images'])*after['max_image_bytes']+16*1024**2)
        sources=[dict(name=x['name'],path=x['path'],exclude_children=x.get('exclude_children',[])) for x in inv.stores]
        names={x['name'] for x in sources}
        sources += [dict(name=k,path=v['path'],exclude_children=v['exclude_children'])
                    for k,v in after['files'].items() if k not in names]
        cold=after['cold']
        self.phase='capacity_projection'
        space=capacity(Path(cold['destination']),budget,r.cap.bp)
        self.phase='complete'
        return dict(result='passed',diagnostic_status='completed',review_id=fingerprint(body),
                    review_authorizes_execution=False,production_generation_plan_connected=True,
                    destination=self.preparation['paths']['destination'],recipient=self.preparation['recipient'],
                    identity_path=self.preparation['paths']['identity'],identity_contents_read=False,
                    identity_custody_verified=False,setup_ready=True,
                    scoped_services=sorted(cold['services']),scoped_service_count=len(cold['services']),
                    protected_running_count=len(cold['protected']),source_count=len(sources),sources=sources,
                    required_file_dispositions={x['name']:after['source_dispositions'][x['name']]
                                                for x in inv.files if x.get('status')=='ok'},
                    storage=[dict(role=k,path=v['source'],uid=v['uid'],gid=v['gid'],target=v['target'],
                                  service=v['service']) for k,v in cold['stores'].items()],
                    image_role_count=sum(k.startswith('image:') for k in after['source_dispositions']),
                    unique_image_count=len(after['images']),image_ids=after['images'],
                    stop_order=cold['stop_order'],return_order=list(reversed(cold['stop_order'])),
                    dependency_order_is_application_drain_proof=False,
                    stream_budgets=dict(per_storage_bytes=cold['max_bytes'],per_file_tree_bytes=after['max_file_bytes'],
                                        per_image_export_bytes=after['max_image_bytes'],per_artifact_entries=after['max_entries'],
                                        strict_generation_plaintext_envelope_bytes=budget,
                                        capture_seconds=cold['seconds'],per_container_stop_seconds=cold['stop_seconds']),
                    capacity=space,
                    generation_metadata_stable=True,maintenance_review_id=maintenance['review_id'],
                    source=r.sources,current_worker_invocation=state['provider_status']['InvocationID'],
                    current_worker_verified=True,continuous_uptime_verified=False,
                    production_execution_adapter_available=False,backup_ready=False,
                    pending=['reviewed_capture_and_explicit_recovery_executor','separate_identity_custody',
                             'clean_application_drain_and_shutdown','initial_production_capture',
                             'isolated_actual_database_image_and_application_restore'],
                    next_step='implement_reviewed_capture_recovery_executor_using_this_generation_contract')


def run(directory,langfuse,home,factory=Plan):
    report=dict(schema=1,stage='O04-production-encrypted-generation-plan',action='plan',
                effects=dict(files_written=[],service_control_requested=False,application_credential_contents_read=False,
                             identity_contents_read=False,application_api_requests=0,container_exec_commands=0,
                             backup_executed=False,restore_executed=False,inference_requested=False,
                             image_build_or_pull_requested=False,linger_changed=False),
                activation_supported=False,actual_reboot_acceptance='deferred_by_user',kubernetes_acceptance='deferred')
    plan=None
    try:
        plan=factory(directory,langfuse,home);report.update(plan.plan())
    except Exception as exc:
        code=getattr(exc,'code',None)
        report.update(result='attention_required',code=code if isinstance(code,str) and
                      re.fullmatch('[A-Z][A-Z0-9_]{0,95}',code) else 'BACKUP_PRODUCTION_PLAN_UNAVAILABLE',
                      backup_ready=False,next_step='retain_preparation_and_report_readonly_plan_gate')
        report['plan_diagnostic']=plan_diagnostic(plan,exc)
        diagnostic=safe_snapshot_diagnostic(getattr(exc,'snapshot_diagnostic',None))
        if diagnostic: report['snapshot_diagnostic']=diagnostic
        if plan:
            diagnostic=safe_docker_diagnostic(plan.module,getattr(exc,'docker_diagnostic',None))
            if diagnostic:report['docker_diagnostic']=diagnostic
    if plan:
        r=plan.review
        report['metadata_command_counts']=dict(adapter=r.reader.calls+r.docker.calls+r.docker.base.calls,
                                               includes_core_metadata_queries=False)
        report['worker_service_control_commands']=r.backend.control_commands
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('plan',))
    parser.add_argument('--state-dir',type=Path,default=Path.home()/'.local/state/agent-project-ops/server')
    parser.add_argument('--langfuse-state-dir',type=Path,default=Path.home()/'.local/state/agent-project-langfuse')
    args=parser.parse_args(argv)
    report=run(args.state_dir,args.langfuse_state_dir,Path.home())
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['result']=='passed' else 2


if __name__=='__main__':
    sys.exit(main())
