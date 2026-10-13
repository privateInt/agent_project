#!/usr/bin/env python3
"""Isolated native complete generation + foreign-UID filesystem restore.

Only disposable source/restore binds, one existing exact-ID image save and a
single test user unit. No production CLI, image import or application restore.
"""
from copy import deepcopy
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from src.ops.tools import cold_storage_acceptance as n
from src.ops.candidates.backup_generation_20261001 import generation as g
from src.ops.candidates.backup_restore_20261001 import restore as r
from src.ops.candidates.backup_restore_uid_20261001 import restore_uid as u

a=n.a;require=n.require
ENTRY='src/ops/tools/backup_generation_acceptance.py'
SPEC='src/ops/tools/backup_generation_native_sources_20261001.json'
SPEC_SHA256='9d8d30cfc6a3abcdfcba69755d99f0b5a61efc7c26ca4511cd41977ff5179178'
STAGE='O04-isolated-native-generation-filesystem-restore'
PHASES={'preparation','storage_setup','capture','restore','return_worker','remove_unit','cleanup'}
CALLBACK_STEPS={'capture','receipt','restore_setup','restore','restore_verification'}
CALLBACK_ERRORS={'KeyError','TypeError','ValueError','AttributeError','OSError','PermissionError',
                 'FileNotFoundError','ReadFailure','Failure','Other'}
GENERATION_PHASES={'unavailable','prepared','collecting','sealed','captured','attention_required'}
CURRENT_PHASE='preparation';LIFECYCLE_FD=None


def sources(root):
    n.sources(root);raw=n.source_read(root/SPEC)
    require(a.digest(raw)==SPEC_SHA256,'GENERATION_NATIVE_SOURCE_SPEC_CHANGED')
    spec=a.strict_json(raw);require(spec['schema']==1 and spec['active_code_sha256']==a.core.CODE,'GENERATION_NATIVE_CODE_CHANGED')
    files=dict(spec['files']);files[SPEC]=SPEC_SHA256
    files[ENTRY]=a.digest(n.source_read(root/ENTRY))
    for rel,digest in files.items():
        require(isinstance(rel,str) and not Path(rel).is_absolute() and '..' not in Path(rel).parts
                and re.fullmatch('[a-f0-9]{64}',digest) and a.digest(n.source_read(root/rel))==digest,'GENERATION_NATIVE_SOURCE_CHANGED')
    return files


def create_run(base,engine):
    files=sources(ROOT);directory,record=n.create_run(base,engine)
    for rel,digest in files.items():
        raw=n.source_read(ROOT/rel);path=directory/'project'/rel;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if path.exists():require(a.digest(a.read(path))==digest,'GENERATION_NATIVE_COPY_CONFLICT')
        else:path.write_bytes(raw);path.chmod(0o600)
    a.isolated.write(directory/'generation-run.json',{'schema':1,'kind':STAGE,'owner':record['owner'],
        'directory':str(directory),'files':files,'uid_helpers':[]})
    validate(directory);return directory,record


def validate(directory):
    record=n.validate(directory);marker=a.strict_json(a.read(directory/'generation-run.json'))
    require(set(marker)=={'schema','kind','owner','directory','files','uid_helpers'} and marker['schema']==1
            and marker['kind']==STAGE and marker['owner']==record['owner'] and marker['directory']==str(directory),
            'GENERATION_NATIVE_RUN_CHANGED')
    require(marker['files']==sources(ROOT),'GENERATION_NATIVE_COPY_CHANGED')
    for rel,digest in marker['files'].items():
        require(a.digest(a.read(directory/'project'/rel))==digest,'GENERATION_NATIVE_COPY_CHANGED')
    require(isinstance(marker['uid_helpers'],list) and len(marker['uid_helpers'])<=12,'GENERATION_NATIVE_HELPER_AUDIT_CHANGED')
    for spec in marker['uid_helpers']:
        value=deepcopy(spec);digest=value.pop('spec_sha256',None);path=Path(spec['source'])
        require(g.c.fingerprint(value)==digest and spec['owner']==record['owner'] and spec['image']==record['image']
                and spec['script']==n.source_read(ROOT/'src/ops/candidates/backup_restore_uid_20261001/writer.py').decode()
                and path.is_relative_to(directory/'generation-restores') and path.parts[-3:]==('storage-storage','data','storage')
                and len(path.relative_to(directory/'generation-restores').parts)==4
                and re.fullmatch('restored-[a-f0-9-]{36}\\.partial',path.relative_to(directory/'generation-restores').parts[0])
                and spec['mode'] in ('init','extract','verify'),'GENERATION_NATIVE_HELPER_AUDIT_CHANGED')
    return record


def audit_helpers(directory,history):
    marker=a.strict_json(a.read(directory/'generation-run.json'))
    require(marker['uid_helpers']==history[:len(marker['uid_helpers'])],'GENERATION_NATIVE_HELPER_AUDIT_CHANGED')
    marker['uid_helpers']=deepcopy(history);a.isolated.write(directory/'generation-run.json',marker);validate(directory)


class Engine(n.Engine):
    def cleanup(self,directory):
        global CURRENT_PHASE
        CURRENT_PHASE='cleanup';record=validate(directory)
        # Saved IDs/specs are audit only. Unknown live helpers block cleanup;
        # they cannot be adopted or killed by a later invocation.
        unit=a.native.cleanup_unit_subprocess(directory)
        require(unit.get('worker_exit_verified') is True,'GENERATION_NATIVE_UNIT_EXIT_UNKNOWN')
        require(not self.ids(['label='+u.LABEL+'='+record['owner']]),'GENERATION_NATIVE_UID_HELPER_CLEANUP_REQUIRED')
        return super().cleanup(directory)


def preflight():
    sources(ROOT);value=n.preflight();value.update(stage=STAGE,backup_ready=False,
        native_image_import_acceptance=False,application_restore_acceptance=False);return value


def inventory(gen,helper=None):
    """Only live verified cold readers/UID writers are inventory exceptions."""
    backend=gen.backend;p=gen.p['cold'];backend.verify_host();scope={}
    for role,expected in p['services'].items():
        row=backend.inspect(expected);require(n.storage.fixed(row)==n.storage.fixed(expected),'COLD_SERVICE_IDENTITY_CHANGED')
        scope[role]={'container_id':row['container_id'],'image_id':row['image_id'],'owner':row['owner'],
                     'mounts_sha256':g.c.fingerprint(row['mounts'])}
    cold=gen.op.helpers if gen.op else {}
    for ident,spec in cold.items():backend.helper(ident,spec)
    uid=helper.owned if helper else {}
    for ident,spec in uid.items():helper.inspect(ident,spec)
    running=backend.running();excluded={row['container_id'] for row in p['services'].values()}|set(cold)|set(uid)
    protected={key:value for key,value in running.items() if key not in excluded}
    require(protected==p['protected'],'COLD_PROTECTED_CONTAINER_CHANGED')
    return {'scope':scope,'protected':protected}


class StreamScope:
    """Local pause/lease checks per block; full manager/inventory every 2s.

Full checks are mandatory at component boundaries and before publication.
The native image stream would otherwise spawn all metadata queries per 64KiB.
This is sampled inventory evidence, never continuous uptime evidence.
"""
    def __init__(self,scope):self.scope=scope;self.last=0
    @property
    def descriptor(self):return self.scope.descriptor
    @property
    def active(self):return self.scope.active
    def checkpoint(self,force=False):
        require(self.active,'MAINTENANCE_SCOPE_CLOSED');os.fstat(self.descriptor)
        parent=self.scope.parent;record=self.scope.record
        require(parent.store.control()==record['paused_control'],'MAINTENANCE_CONTROL_CHANGED')
        require(parent.load(record['id'])==record,'MAINTENANCE_RECORD_CHANGED')
        if force or time.monotonic()-self.last>=2:
            self.scope.checkpoint();self.last=time.monotonic()


class NativeGeneration(g.Generation):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.last_full=0
    def guard(self,quiet=True):
        self.scope.checkpoint();require(time.monotonic()<self.until,'GENERATION_DEADLINE_OR_PAUSE')
        require(g.c.fingerprint(self.p)==self.original['review_id'],'GENERATION_REVIEW_CHANGED')
        if self.raw is not None:
            require(a.read(self.folder/'generation.json')==self.raw,'GENERATION_JOURNAL_CHANGED')
        if time.monotonic()-self.last_full>=2:
            super().guard(quiet);self.last_full=time.monotonic()
    def encrypt(self,*args,**kwargs):
        self.last_full=0;self.scope.checkpoint(True)
        value=super().encrypt(*args,**kwargs)
        self.last_full=0;self.guard();self.scope.checkpoint(True);return value
    def __call__(self,scope):
        def boundary(phase):
            if phase=='before_commit':self.last_full=0;self.scope.checkpoint(True);self.guard(False)
        self.hook=boundary;return super().__call__(scope)


def captured_receipt(gen):
    """Bind the durable receipt to this live capture and its original review.

    Generation.validate returns the plan body without review_id. The digest
    remains in original, alongside the reviewed proposal, and in the receipt.
    """
    require(gen.record['phase']=='captured' and not gen.op.helpers and not gen.backend.owned_helpers,
            'GENERATION_NATIVE_CAPTURE_UNCONFIRMED')
    trust=r.read_json(gen.folder/'receipt.json')
    expected={key:{field:row[field] for field in ('path','ciphertext_sha256','ciphertext_bytes')}
              for key,row in gen.artifacts.items()}
    require(trust['artifacts']==expected and trust['id']==gen.p['id']
            and trust['review_id']==gen.original['review_id']==g.c.fingerprint(gen.p)
            and gen.record['receipt_sha256']==g.c.fingerprint(trust),'GENERATION_NATIVE_LIVE_RECEIPT_CHANGED')
    require(g.inspect(gen.folder,trust)['result']=='captured_integrity_verified','GENERATION_NATIVE_RECEIPT_CHANGED')
    return trust


def safe_callback_diagnostic(value):
    if not isinstance(value,dict) or set(value)!={'step','exception_type','generation_phase','code'}:return None
    if not all(isinstance(item,str) for item in value.values()):return None
    if value['step'] not in CALLBACK_STEPS or value['exception_type'] not in CALLBACK_ERRORS \
            or value['generation_phase'] not in GENERATION_PHASES \
            or not re.fullmatch('[A-Z][A-Z0-9_]{0,119}',value['code']):return None
    return dict(value)


def callback_diagnostic(exc,step,gen):
    name=type(exc).__name__;phase=(gen.record or {}).get('phase','unavailable')
    code=getattr(exc,'code',a.core.local.error_code(exc))
    if not isinstance(code,str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,119}',code):code='INVALID_LOCAL_STATE'
    return safe_callback_diagnostic({'step':step,'exception_type':name if name in CALLBACK_ERRORS else 'Other',
                                    'generation_phase':phase if isinstance(phase,str) and phase in GENERATION_PHASES else 'unavailable',
                                    'code':code})


def progress(directory,engine,phase,message):
    global CURRENT_PHASE
    require(phase in PHASES,'GENERATION_NATIVE_PHASE_INVALID');CURRENT_PHASE=phase
    engine.protect(validate(directory)['protected']);print('[generation-acceptance] '+message,file=sys.stderr,flush=True)


def exercise(directory,engine,tools_dir):
    require(ROOT==directory/'project','GENERATION_NATIVE_COPIED_ENTRY_REQUIRED');record=validate(directory)
    tool=n.age_tool.Age(tools_dir/'age',n.age_tool.sha(tools_dir/'age'));identity,recipient=n.age_tool.test_identity(tools_dir)
    progress(directory,engine,'preparation','prepare disposable Ops and fresh boot-policy test worker')
    context=a.isolated.prepare_test_ops(directory,engine);state=a.checked_state(directory);ctl=a.native.Controller(state)
    first=a.initialize_boot_fixture(state,ctl);store=a.ws.Store(state);manifest=a.service.load(state)
    fixed={name:a.read(store.path.parent/name,32768) for name in ('boot-start.json','code-transition.json','service.json')}
    configs={name:a.read(state/name) for name in ('config.json','host-config.json','owner.json','access-key','compose.env')}
    source=directory/'generation-source';source.mkdir(mode=0o700)
    payload=b'isolated-generation-settings\x00\xff\n';(source/'settings').write_bytes(payload);(source/'settings').chmod(0o600)
    parent=directory/'generation-restores';parent.mkdir(mode=0o700)
    op=n.operation(directory,engine,tool,recipient)
    image_metadata=g.ImageExporter(op.backend).inspect(record['image'])
    image_budget=min(2**33,max(2**28,image_metadata[2]*2+2**26))
    available=shutil.disk_usage(directory).free;required=3*image_budget+2**29+2**30
    require(available>=required,'GENERATION_NATIVE_FREE_SPACE_INSUFFICIENT')
    plan=g.plan(op.original,{'configuration':{'path':str(source),'exclude_children':[]}},[record['image']],
                max_file_bytes=1024**2,max_image_bytes=image_budget,max_entries=128)
    gen=NativeGeneration(plan,op.backend,tool);box={};helper_box={}
    controller=a.core.Maintenance(state,ctl.backend,inventory_fn=lambda:inventory(gen,helper_box.get('helper')),
                                  readback_fn=a.ReadbackProbe(directory))
    proposal=a.plan_wait(controller)
    def callback(original_scope):
        scope=StreamScope(original_scope);helper=None;step='capture'
        try:
            a.leased_child(original_scope,directory,LIFECYCLE_FD)
            progress(directory,engine,'capture','pause test worker; capture storage, files, exact-ID image and encrypted catalog')
            gen(scope);scope.checkpoint(True)
            step='receipt';trust=captured_receipt(gen)
            progress(directory,engine,'restore','authenticate all components; restore actor files and storage UID into fresh private directories')
            step='restore_setup'
            helper=u.UIDHelper(gen.backend,scope,record['image'],record['owner'],
                n.source_read(ROOT/'src/ops/candidates/backup_restore_uid_20261001/writer.py').decode(),scope.checkpoint,
                audit=lambda rows:audit_helpers(directory,rows));helper_box['helper']=helper
            rp=r.plan(gen.folder,trust,parent,identity,seconds=300,max_bytes=2**34,max_entries=65536)
            step='restore'
            restored=u.Restore(rp,tool,helper,gen.p['cold']['stores']).run();scope.checkpoint(True);helper.empty()
            step='restore_verification'
            require(restored['crypto_eof_authenticated'] and restored['foreign_uid_materialization']
                    and restored['foreign_uid_readonly_verification'] and len(restored['image_archives'])==1,
                    'GENERATION_NATIVE_RESTORE_UNCONFIRMED')
            require((Path(restored['directory'])/'files'/'data'/'configuration'/'settings').read_bytes()==payload,
                    'GENERATION_NATIVE_ACTOR_CONTENT_MISMATCH')
            box.update(restored=restored,trusted=trust)
            return {'result':'passed','child_processes_stopped':True}
        except BaseException as exc:
            box['callback_diagnostic']=callback_diagnostic(exc,step,gen)
            raise
        finally:
            if helper is not None:helper.abort_all()
            for ident,spec in list(gen.backend.owned_helpers.items()):
                gen.backend.helper(ident,spec);gen.backend.abort(ident,spec)
                if gen.op:gen.op.helpers.pop(ident,None)
    result=controller.run(proposal,callback)
    if result.get('result')!='passed':
        exc=a.ReadFailure(result.get('code','GENERATION_NATIVE_CALLBACK_FAILED'))
        exc.callback_diagnostic=box.get('callback_diagnostic');raise exc
    require(store.control()['desired']=='paused' and not a.service.enabled(manifest),'GENERATION_NATIVE_CALLBACK_RETURNED_WORKER')
    require(gen.op.record['stopped']==['cold-client','cold-db'] and gen.op.record['started']==['cold-db','cold-client'],
            'GENERATION_NATIVE_SERVICE_ORDER_FAILED')
    progress(directory,engine,'return_worker','explicit Ops readback, new receipt/invocation/evaluation; duplicate return refuses replay')
    returned=controller.resume(proposal['id']);require(returned.get('fresh_evaluation_verified') is True,
                                                       returned.get('code','GENERATION_NATIVE_WORKER_RETURN_FAILED'))
    current=ctl.status();require(a.native.healthy(current) and a.native.invocation(current)!=a.native.invocation(first),'GENERATION_NATIVE_STALE_WORKER')
    count=ctl.backend.control_commands;a.rejected(lambda:controller.resume(proposal['id']),{'MAINTENANCE_RETURN_REFUSED'})
    require(count==ctl.backend.control_commands,'GENERATION_NATIVE_DUPLICATE_START')
    follow=ctl.wait(lambda value:a.native.healthy(value,a.native.observation(current)['completed_cycles']+1))
    require(a.native.invocation(follow)==a.native.invocation(current) and not a.native.observation(follow)['outcome']['restart_requested'],
            'GENERATION_NATIVE_DUPLICATE_REFRESH')
    require({name:a.read(store.path.parent/name,32768) for name in fixed}==fixed and {name:a.read(state/name) for name in configs}==configs
            and a.worker.code_hash(ROOT)==a.core.CODE,'GENERATION_NATIVE_FIXED_STATE_CHANGED')
    a.isolated.preserve_test_ops(context,engine)
    progress(directory,engine,'remove_unit','remove owned test unit; retain encrypted generation, private restore and audit')
    removed=a.native.cleanup_unit(directory);require(removed.get('worker_exit_verified') and removed.get('unit_absence_verified'),
                                                    'GENERATION_NATIVE_UNIT_REMOVAL_FAILED')
    cases={name:'passed' for name in ('dynamic_storage_uid_exact_source_and_service_discovery','real_inherited_host_leases',
        'native_dependency_stop_and_same_id_service_return','native_complete_encrypted_generation',
        'native_exact_id_image_export','all_component_age_authenticated_eof','actor_filesystem_restore',
        'foreign_uid_filesystem_materialization','separate_readonly_uid_hash_metadata_verification',
        'native_image_archive_config_layer_validation','callback_keeps_worker_paused',
        'explicit_worker_readback_receipt_new_invocation_evaluation','duplicate_return_no_resubmission',
        'unchanged_followup','policy_code_credentials_cache_history_preserved','owned_unit_exit_and_removal')}
    return {'schema':1,'stage':STAGE,'action':'worker','result':'passed','checked_at':a.rc.now(),'cases':cases,
        'scope':'native_rootful_docker_synthetic_files_foreign_uid_filesystem_actual_image_export_user_unit_same_boot',
        'active_code_sha256':a.core.CODE,'manager_version':ctl.manager_version,'image_id':record['image'],
        'runtime_source_hashes_verified':context['hashes'],'generation_components':len(box['trusted']['artifacts']),
        'generation_directory':str(gen.folder),'restored_directory':box['restored']['directory'],
        'generation_capture_seconds_budget':gen.p['cold']['seconds'],'restore_seconds_budget':300,
        'image_export_bytes_budget':image_budget,'free_space_before_callback':available,'required_free_space':required,
        'inventory_sample_seconds':2,'continuous_inventory_verified':False,
        'foreign_storage_uid':gen.p['cold']['stores']['storage']['uid'],'foreign_storage_gid':gen.p['cold']['stores']['storage']['gid'],
        'filesystem_restore_acceptance':True,'foreign_uid_filesystem_restore_acceptance':True,
        'native_Docker_image_export_acceptance':True,'image_archive_structure_validation':True,
        'image_import_restore_acceptance':False,'application_restore_acceptance':False,
        'test_unit_removed':True,'test_worker_exit_verified':True,'maintenance_jobs':1,
        'synthetic_credentials_preserved':True,'initial_cache_entries_preserved':len(context['initial_cache']['success_times']),
        'production_state_read':False,'production_credentials_read':False,'production_worker_stopped':False,
        'production_service_control_requested':False,'production_application_api_requests':0,'production_worker_lifecycle_observed':False,
        'production_backup_executed':False,'production_restore_executed':False,'production_backup_executor_integration':False,
        'backup_ready':False,'inference_requested':False,'image_build_or_pull_requested':False,
        'linger_changed':False,'actual_reboot_acceptance':'deferred_by_user','kubernetes_acceptance':'deferred',
        'long_running_acceptance':False,'fixture_source_and_restore_retained':'private_run_no_original_source_permission_repair'}


def error_report(exc):
    value=n.error_report(exc);value['stage']=STAGE
    phase=getattr(exc,'failure_phase',CURRENT_PHASE)
    if isinstance(phase,str) and phase in PHASES:value['failure_phase']=phase
    else:value.pop('failure_phase',None)
    diagnostic=safe_callback_diagnostic(getattr(exc,'callback_diagnostic',None))
    if diagnostic:value['callback_diagnostic']=diagnostic
    return value


def invoke(directory,tools_dir,timeout=850):
    record=validate(directory);project=directory/'project'
    env={key:value for key,value in os.environ.items() if not key.startswith(('OPS_','COMPOSE_','PYTHON'))
         and key not in ('DOCKER_CONTEXT','DOCKER_API_VERSION','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}
    env.update(DOCKER_HOST=record['endpoint'],PYTHONDONTWRITEBYTECODE='1');path=directory/'generation-worker.log'
    with path.open('w') as out:
        path.chmod(0o600);child=subprocess.Popen([sys.executable,'-I','-B',str(project/ENTRY),'worker',
            '--run-dir',str(directory),'--test-tools-dir',str(tools_dir)],cwd=project,env=env,
            stdin=subprocess.DEVNULL,stdout=out,start_new_session=True)
        try:code=child.wait(timeout=timeout)
        except BaseException:a.isolated.stop_worker(child);raise
    report=a.strict_json(a.read(path,128*1024))
    if code!=0 or report.get('result')!='passed':
        exc=a.ReadFailure(report.get('code','GENERATION_NATIVE_CHILD_FAILED'))
        for key in ('docker_diagnostic','storage_docker_diagnostic','fixture_docker_diagnostic','failure_phase','callback_diagnostic'):
            setattr(exc,key,report.get(key))
        raise exc
    require(report.get('stage')==STAGE and report.get('action')=='worker','GENERATION_NATIVE_REPORT_CHANGED');return report


def run(base,fetch_test_age=False):
    require(fetch_test_age,'GENERATION_NATIVE_FETCH_TEST_AGE_REQUIRED');preflight()
    with a.isolated.run_lock(base):
        engine=Engine();directory,record=create_run(base,engine)
        print('[generation-acceptance] evidence: '+str(directory),file=sys.stderr,flush=True)
        result={'schema':1,'stage':STAGE,'result':'attention_required','production_backup_executed':False,
                'production_worker_stopped':False,'production_service_control_requested':False,'inference_requested':False,'backup_ready':False}
        try:
            with tempfile.TemporaryDirectory(prefix='ops-generation-native-tools-') as folder:
                tools_dir=Path(folder);tool_info=n.age_tool.fetch(tools_dir);engine.create_fixture_network(directory)
                for role,(name,subtree) in a.isolated.ROLES.items():
                    print('[generation-acceptance] fixture preparation: '+role,file=sys.stderr,flush=True)
                    rel=str(Path(subtree)/('acceptance-'+role+'.json'));engine.compose(directory,rel,name)
                    if role=='model':engine.compose(directory,rel,'manual-model')
                progress(directory,engine,'storage_setup','initialize only fresh fixture storage; remove root initializer')
                engine.initialize_storage(directory);result=invoke(directory,tools_dir);result['test_tool']=tool_info
            result['temporary_tools_and_identity_cleanup']='passed'
        except BaseException as exc:
            result.update(error_report(exc));result['temporary_tools_and_identity_cleanup']='passed_if_created_context_closed'
        finally:
            try:cleanup=engine.cleanup(directory)
            except BaseException as exc:cleanup=error_report(exc);result['result']='attention_required'
            if cleanup.get('result')!='passed':result['result']='attention_required'
            if cleanup.get('prior_run_protected_baseline_match') is False:
                result.update(result='attention_required',code='ACCEPTANCE_PROTECTED_CONTAINER_CHANGED')
            result.update(run_directory=str(directory),cleanup=cleanup);a.isolated.write(directory/'generation-report.json',result)
        return result


def main():
    global LIFECYCLE_FD
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('preflight','run','worker','cleanup'))
    parser.add_argument('--run-dir',type=Path);parser.add_argument('--state-dir',type=Path,default=a.isolated.BASE)
    parser.add_argument('--fetch-test-age',action='store_true');parser.add_argument('--test-tools-dir',type=Path);args=parser.parse_args()
    try:
        if args.action in ('preflight','run'):
            require(args.run_dir is None and args.test_tools_dir is None,'GENERATION_NATIVE_RUN_REFERENCE_INVALID')
            result=preflight() if args.action=='preflight' else run(args.state_dir,args.fetch_test_age)
        else:
            require(args.run_dir is not None,'ACCEPTANCE_RUN_DIR_REQUIRED');directory=a.manage.safe(args.run_dir);record=validate(directory)
            if args.action=='worker':
                require(ROOT==directory/'project' and args.test_tools_dir is not None,'GENERATION_NATIVE_COPIED_ENTRY_REQUIRED')
                n.age_tool.private(args.test_tools_dir)
                with a.lifecycle_lock(directory) as fd:
                    LIFECYCLE_FD=fd
                    try:result=exercise(directory,Engine(record['endpoint']),args.test_tools_dir)
                    finally:LIFECYCLE_FD=None
            else:
                require(args.test_tools_dir is None,'GENERATION_NATIVE_RUN_REFERENCE_INVALID')
                with a.isolated.run_lock(directory.parent):
                    result={'schema':1,'stage':STAGE,'result':'passed','run_directory':str(directory),
                            'cleanup':Engine(record['endpoint']).cleanup(directory),'acceptance_result_not_reused':True,
                            'production_worker_stopped':False,'production_service_control_requested':False}
    except BaseException as exc:result=error_report(exc)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['result']=='passed' else 2

if __name__=='__main__':raise SystemExit(main())
