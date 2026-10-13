#!/usr/bin/env python3
"""Native isolated cold-storage acceptance. No production backup or control.

Reuse the existing Ops image and one shared fixture bridge. Two additional
networkless fixture services carry a private, dynamically chosen storage UID.
A constrained root fixture initializer owns only the new run's storage bind.
The reviewed candidate controls existing fixture IDs and streams into host age.
No saved job is replayed; uncertain readers block cleanup after test-unit exit.
"""
from copy import deepcopy
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ops_tools import maintenance_acceptance as a, cold_storage_fixtures as f
from ops_candidates.cold_storage_20260930 import storage
from ops_candidates.backup_capture_20260930 import age_tool, capture

require=a.require
STAGE='O04-isolated-native-cold-storage-acceptance'
ENTRY='ops_tools/cold_storage_acceptance.py'
SPEC='ops_tools/cold_storage_native_sources_20260930.json'
COLD_SPEC='ops_candidates/cold_storage_20260930/manifest.json'
SPEC_SHA256='872c9d2cd1be7bfa7f59319e766a01d11c247b8b21de8505a74189a3907f6d02'
PHASES={'preparation','storage_setup','capture','verify_ciphertext','return_worker','failure_gate','remove_unit','cleanup'}
FORMAT=storage.FORMAT.replace('{{if .State.Health}}{{json .State.Health.Status}}',
    '{{with index .State "Health"}}{{json .Status}}')
IMAGE_FORMAT='[{{json .Id}},{{json (index .Config "Volumes")}}]'
META=a.isolated.META.replace('{{if .State.Health}}{{json .State.Health.Status}}',
    '{{with index .State "Health"}}{{json .Status}}')
FIXTURE_START_FORMAT='[{{json .State}},{{json (index .Config "Healthcheck")}}]'
LEGACY_ENTRY_SHA256='7e6c2cb0b24eea0c140a4e95c3e01c224f9a4a554173c5d8dfad4b194ba74e80'
FIXTURE_REASONS=a.DOCKER_REASONS|{'TEMPLATE_FIELD_MISSING','TEMPLATE_PARSE_ERROR',
    'EXECUTABLE_UNAVAILABLE','BIND_SOURCE_MISSING','OCI_RUNTIME_ERROR','CONTAINER_EXITED',
    'DISABLED_HEALTHCHECK_WAIT'}
FIXTURE_ROLES=a.FIXTURE_ROLES|{'cold_setup','cold_client','cold_db'}
QUERY_OPERATIONS={'host_info','image_inspect','container_inspect','container_list','unknown'}
QUERY_REASONS={'TEMPLATE_FIELD_MISSING','TEMPLATE_PARSE_ERROR','CONNECTION_FAILED','ACCESS_DENIED',
    'IMAGE_NOT_FOUND','CLI_UNAVAILABLE','COMMAND_TIMEOUT','RESPONSE_LIMIT','INVALID_JSON','UNKNOWN'}
CURRENT_PHASE='preparation'
LIFECYCLE_FD=None


def error_report(exc):
    code=getattr(exc,'code','COLD_NATIVE_CHECK_FAILED')
    if not isinstance(code,str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,119}',code):code='COLD_NATIVE_CHECK_FAILED'
    value={'schema':1,'stage':STAGE,'result':'attention_required','code':code}
    phase=getattr(exc,'failure_phase',CURRENT_PHASE)
    if isinstance(phase,str) and phase in PHASES:value['failure_phase']=phase
    diagnostic=a.safe_docker_diagnostic(getattr(exc,'docker_diagnostic',None))
    if diagnostic:value['docker_diagnostic']=diagnostic
    diagnostic=safe_query_diagnostic(getattr(exc,'storage_docker_diagnostic',None))
    if diagnostic:value['storage_docker_diagnostic']=diagnostic
    diagnostic=safe_fixture_diagnostic(getattr(exc,'fixture_docker_diagnostic',None))
    if diagnostic:value['fixture_docker_diagnostic']=diagnostic
    observation=safe_initializer_observation(getattr(exc,'initializer_observation',None))
    if observation:value['initializer_observation']=observation
    progress=getattr(exc,'cleanup_progress',None)
    if isinstance(progress,dict) and set(progress)=={'removed_containers','removed_networks','owned_absence_verified','worker_exit_verified'}:
        if all(type(progress[k]) is int and 0<=progress[k]<=512 for k in ('removed_containers','removed_networks')) \
                and all(type(progress[k]) is bool for k in ('owned_absence_verified','worker_exit_verified')):
            value['cleanup_progress']=deepcopy(progress)
    return value


def safe_fixture_diagnostic(value):
    required={'operation','outcome','reason','exit_code','text_truncated','phase','fixture_role'}
    if not isinstance(value,dict) or set(value)!=required:return None
    if not isinstance(value['operation'],str) or value['operation'] not in a.DOCKER_OPERATIONS:return None
    if not isinstance(value['outcome'],str) or value['outcome'] not in ('failed','timeout','unavailable','response_limit'):return None
    if not isinstance(value['reason'],str) or value['reason'] not in FIXTURE_REASONS:return None
    if value['exit_code'] is not None and not (type(value['exit_code']) is int and -255<=value['exit_code']<=255):return None
    if type(value['text_truncated']) is not bool:return None
    if not isinstance(value['phase'],str) or value['phase'] not in PHASES|{'fixture_preparation','metadata_or_test_operation'}:return None
    if value['fixture_role'] is not None and (not isinstance(value['fixture_role'],str) or value['fixture_role'] not in FIXTURE_ROLES):return None
    return deepcopy(value)


def fixture_reason(out,err):
    reason,truncated=a.docker_reason(out,err)
    if reason!='UNKNOWN':return reason,truncated
    text='\n'.join(x[:32768].lower() for x in (out,err) if isinstance(x,str))
    signatures={'TEMPLATE_FIELD_MISSING':('map has no entry for key',),
        'TEMPLATE_PARSE_ERROR':('template parsing error',),
        'EXECUTABLE_UNAVAILABLE':('executable file not found','exec: "python3":'),
        'BIND_SOURCE_MISSING':('bind source path does not exist',),
        'OCI_RUNTIME_ERROR':('oci runtime create failed','failed to create task for container'),
        'DISABLED_HEALTHCHECK_WAIT':('has no healthcheck configured',),
        'CONTAINER_EXITED':('exited (','container exited',)}
    found={k for k,phrases in signatures.items() if any(p in text for p in phrases)}
    if len(found)>1:found.discard('OCI_RUNTIME_ERROR')
    return (next(iter(found)) if len(found)==1 else 'MULTIPLE_SIGNATURES' if found else 'UNKNOWN'),truncated


def safe_initializer_observation(value):
    if not isinstance(value,dict):return None
    if set(value)=={'status','source_ownership'} and value['status']=='absent' \
            and value['source_ownership'] in ('original_actor','selected_storage_uid'):return deepcopy(value)
    if set(value)!={'status','running','exit_code','oom_killed','error_reason','source_ownership'}:return None
    if value['status'] not in ('created','running','paused','restarting','removing','exited','dead'):return None
    if type(value['running']) is not bool or type(value['oom_killed']) is not bool:return None
    if type(value['exit_code']) is not int or not -255<=value['exit_code']<=255:return None
    if not isinstance(value['error_reason'],str) or value['error_reason'] not in FIXTURE_REASONS|{'NONE'}:return None
    if value['source_ownership'] not in ('original_actor','selected_storage_uid'):return None
    return deepcopy(value)


def safe_query_diagnostic(value):
    """Fixed metadata only. Never publish argv, endpoint, file paths or stderr."""
    if not isinstance(value,dict):return None
    operation=value.get('operation');outcome=value.get('outcome');reason=value.get('reason');rc=value.get('exit_code')
    if not isinstance(operation,str) or operation not in QUERY_OPERATIONS or outcome not in ('failed','unknown') \
            or not isinstance(reason,str) or reason not in QUERY_REASONS:return None
    if rc is not None and (type(rc) is not int or not -255<=rc<=255):return None
    result={'operation':operation,'outcome':outcome,'reason':reason,'exit_code':rc}
    field=value.get('template_field')
    if field in ('Volumes','Health'):result['template_field']=field
    return result


def query_operation(args):
    if args[:2]==['info','--format']:return 'host_info'
    if args[:3]==['image','inspect','--format']:return 'image_inspect'
    if args[:3]==['container','inspect','--format']:return 'container_inspect'
    if args[:2]==['ps','--format']:return 'container_list'
    return 'unknown'


def query_failure(args,code,rc=None,stderr=b''):
    # Candidate call already bounds stderr; cap injected test responses too.
    raw=stderr[:32768].lower() if isinstance(stderr,bytes) else b''
    reason='UNKNOWN';field=None
    if code=='COLD_DOCKER_OUTCOME_UNKNOWN':reason='COMMAND_TIMEOUT'
    elif code=='COLD_DOCKER_RESPONSE_LIMIT':reason='RESPONSE_LIMIT'
    elif code=='COLD_DOCKER_METADATA_INVALID':reason='INVALID_JSON'
    elif code=='COLD_NATIVE_DOCKER_UNAVAILABLE':reason='CLI_UNAVAILABLE'
    elif b'map has no entry for key' in raw:
        reason='TEMPLATE_FIELD_MISSING'
        for name in ('Volumes','Health'):
            if ('key "'+name.lower()+'"').encode() in raw:field=name
    elif b'template parsing error' in raw or b'template:' in raw:reason='TEMPLATE_PARSE_ERROR'
    elif b'permission denied' in raw or b'access is denied' in raw:reason='ACCESS_DENIED'
    elif b'cannot connect' in raw or b'connection refused' in raw or b'error during connect' in raw:reason='CONNECTION_FAILED'
    elif b'no such image' in raw:reason='IMAGE_NOT_FOUND'
    error=a.ReadFailure(code)
    error.storage_docker_diagnostic=safe_query_diagnostic({'operation':query_operation(args),
        'outcome':'unknown' if code=='COLD_DOCKER_OUTCOME_UNKNOWN' else 'failed',
        'reason':reason,'exit_code':rc,'template_field':field})
    return error


def source_read(path):
    path=a.manage.safe(path);info=path.stat()
    require(path.is_file() and info.st_uid==os.getuid() and info.st_nlink==1 and info.st_size<1024**2,
            'COLD_NATIVE_SOURCE_INVALID')
    return path.read_bytes()


def sources(root):
    require(a.worker.code_hash(root)==a.core.CODE,'COLD_NATIVE_RUNTIME_CHANGED')
    raw=source_read(root/SPEC);require(a.digest(raw)==SPEC_SHA256,'COLD_NATIVE_SOURCE_SPEC_CHANGED');spec=a.strict_json(raw)
    require(spec.get('schema')==1 and spec.get('active_code_sha256')==a.core.CODE
            and spec.get('production_backup_available') is False,'COLD_NATIVE_SOURCE_SPEC_INVALID')
    files={ENTRY:source_read(root/ENTRY),'ops_tools/cold_storage_fixtures.py':source_read(root/'ops_tools/cold_storage_fixtures.py'),SPEC:raw}
    cold=a.strict_json(source_read(root/COLD_SPEC))
    required={COLD_SPEC,a.ENTRY}|{r['path'] for r in cold['auxiliary_files']}
    require(set(spec['files'])==required,'COLD_NATIVE_SOURCE_SCOPE_CHANGED')
    for rel,digest in spec['files'].items():
        path=Path(rel);require(not path.is_absolute() and '..' not in path.parts,'COLD_NATIVE_SOURCE_SCOPE_CHANGED')
        raw=source_read(root/path)
        require(a.digest(raw)==digest,'COLD_NATIVE_DEPENDENCY_CHANGED');files[rel]=raw
    for rel in files:source_read(root/rel)
    return files


def create_run(base,engine):
    files=sources(ROOT);directory,record=a.create_run(base,engine)
    folder=directory/'cold-source';folder.mkdir(mode=0o700)
    fixture={'source':str(folder),'identity':f.identity(folder),'uid':f.choose_uid(),'gid':os.getgid()}
    for rel,raw in files.items():
        target=directory/'project'/rel;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        target.write_bytes(raw);target.chmod(0o600)
    path=directory/'project'/f.REL;spec=a.strict_json(a.read(path));original=deepcopy(spec)
    spec['services'].update(f.configs(record,fixture));a.isolated.write(path,spec)
    record['compose'][f.REL]['sha256']=a.digest(a.read(path));a.isolated.write(directory/'run.json',record)
    a.isolated.write(directory/'cold-run.json',{'schema':1,'kind':STAGE,'owner':record['owner'],
        'directory':str(directory),'files':{rel:a.digest(raw) for rel,raw in files.items()},
        'original_langfuse_spec':original,'fixture':fixture,'operations':[]})
    validate(directory)
    return directory,record


def validate(directory,legacy_cleanup=False):
    record=a.validate(directory);marker=a.strict_json(a.read(directory/'cold-run.json'))
    files=sources(ROOT)
    expected={k:a.digest(v) for k,v in files.items()}
    if legacy_cleanup:expected[ENTRY]=LEGACY_ENTRY_SHA256
    require(marker.get('schema')==1 and marker.get('kind')==STAGE and marker.get('owner')==record['owner']
            and marker.get('directory')==str(directory) and marker.get('files')==expected,
            'COLD_NATIVE_RUN_CHANGED')
    for rel,digest in marker['files'].items():require(a.digest(a.read(directory/'project'/rel))==digest,'COLD_NATIVE_COPY_CHANGED')
    fixture=marker['fixture']
    require(set(fixture)=={'source','identity','uid','gid'} and fixture['source']==str(directory/'cold-source')
            and type(fixture['uid']) is int and 10000<=fixture['uid']<600000 and fixture['uid']!=record['uid']
            and fixture['gid']==record['gid'],'COLD_NATIVE_STORAGE_SCOPE_CHANGED')
    prior=fixture['identity'];now=f.identity(directory/'cold-source')
    require(prior[:2]==now[:2] and now[2:] in ([record['uid'],record['gid'],0o700],[fixture['uid'],fixture['gid'],0o700]),
            'COLD_NATIVE_SOURCE_CHANGED')
    require(prior[2:]==[record['uid'],record['gid'],0o700],'COLD_NATIVE_SOURCE_CHANGED')
    original=marker['original_langfuse_spec'];cfg=original['services']
    require(set(cfg)=={a.isolated.ROLES['langfuse'][0]} and original['networks']=={'default':{'name':record['fixture_network']['name'],'external':True}},
            'COLD_NATIVE_BASE_SPEC_CHANGED')
    # Reconstruct the upstream spec rather than trusting the editable record.
    base=a.isolated.fixture_spec(directory,record,'langfuse')
    base['networks']=original['networks']
    port=cfg[a.isolated.ROLES['langfuse'][0]]['ports'][0]['published']
    base['services'][a.isolated.ROLES['langfuse'][0]]['ports'][0]['published']=port
    require(type(port) is int and 0<port<=65535 and base==original,'COLD_NATIVE_BASE_SPEC_CHANGED')
    expected=deepcopy(original);expected['services'].update(f.configs(record,fixture))
    require(a.strict_json(a.read(directory/'project'/f.REL))==expected,'COLD_NATIVE_FIXTURE_RECIPE_CHANGED')
    jobs=marker['operations'];plan_files={str(x.name) for x in directory.glob('cold-plan-*.json')}
    require(isinstance(jobs,list) and all(isinstance(x,str) for x in jobs) and
            plan_files=={'cold-plan-'+x+'.json' for x in jobs},'COLD_NATIVE_JOB_SET_INVALID')
    require(isinstance(jobs,list) and len(jobs)<=2 and len(set(jobs))==len(jobs),'COLD_NATIVE_JOB_SET_INVALID')
    for job in jobs:
        require(isinstance(job,str) and re.fullmatch('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',job),'COLD_NATIVE_JOB_SET_INVALID')
        p=a.strict_json(a.read(directory/('cold-plan-'+job+'.json')))
        # Never use this saved proposal to resubmit a mutation.
        require(p['id']==job and p['destination']==str(directory/'cold-encrypted') and
                set(p['services'])==set(f.ROLES) and p['stores']['storage']['source']==fixture['source'],
                'COLD_NATIVE_JOB_SCOPE_CHANGED')
        require(capture.fingerprint({k:v for k,v in p.items() if k!='review_id'})==p['review_id'],'COLD_NATIVE_JOB_CHANGED')
    return record


def preflight():
    value=a.preflight();sources(ROOT)
    return dict(value,stage=STAGE,production_backup_available=False,native_acceptance_pending=True)


class Engine(a.Engine):
    def metadata(self,ident):
        require(re.fullmatch('[a-f0-9]{64}',ident),'INVALID_CONTAINER_ID')
        return a.strict_json(self.command(['inspect','--type','container','--format',META,ident]))

    def command(self,args,timeout=30):
        until=getattr(self,'_fixture_start_until',None)
        if until is not None:
            remaining=until-time.monotonic()
            require(remaining>0,'COLD_NATIVE_FIXTURE_START_TIMEOUT')
            timeout=min(timeout,remaining)
        context=getattr(self,'_cold_context',None) or getattr(self,'_docker_context',None) or {}
        def fail(code,outcome,reason,rc=None,truncated=False):
            error=a.ReadFailure(code)
            value={'operation':a.docker_operation(args),'outcome':outcome,'reason':reason,'exit_code':rc,
                'text_truncated':truncated,'phase':context.get('phase','metadata_or_test_operation'),
                'fixture_role':context.get('fixture_role')}
            error.fixture_docker_diagnostic=safe_fixture_diagnostic(value)
            if value['phase'] in PHASES:error.failure_phase=value['phase']
            # Keep the old diagnostic when every value is in its narrower schema.
            error.docker_diagnostic=a.safe_docker_diagnostic(value)
            raise error from None
        try:
            result=subprocess.run(['docker','--host',self.endpoint,*args],env=self.env,
                capture_output=True,text=True,timeout=timeout,check=False)
        except subprocess.TimeoutExpired:fail('ACCEPTANCE_COMMAND_UNCERTAIN','timeout','COMMAND_TIMEOUT')
        except OSError:fail('ACCEPTANCE_DOCKER_UNAVAILABLE','unavailable','CLI_UNAVAILABLE')
        if result.returncode!=0:
            reason,truncated=fixture_reason(result.stdout,result.stderr)
            fail('ACCEPTANCE_DOCKER_COMMAND_FAILED','failed',reason,result.returncode,truncated)
        if len(result.stdout)>1024*1024:fail('ACCEPTANCE_DOCKER_RESPONSE_LIMIT','response_limit','RESPONSE_LIMIT',0,True)
        return result.stdout

    def compose(self,directory,relative,service,recreate=False):
        validate(directory)
        if relative==f.REL and service in (*f.ROLES,f.SETUP):
            require(not recreate,'COLD_NATIVE_NO_RECREATE')
            previous=getattr(self,'_cold_context',None)
            self._cold_context={'phase':'storage_setup','fixture_role':service.replace('-','_')}
            try:
                self.verify_fixture_network(directory)
                self.start_healthless_fixture(directory,service)
            finally:self._cold_context=previous
            return
        return super().compose(directory,relative,service,recreate)

    def start_healthless_fixture(self,directory,service):
        # Compose --wait treats disabled healthchecks as missing in older
        # releases. Only these exact networkless fixtures use our state gate.
        # A failed/unknown up is never accepted from an already running state.
        require(service in (*f.ROLES,f.SETUP),'COLD_NATIVE_FIXTURE_ROLE_INVALID')
        record=validate(directory);item=record['compose'][f.REL]
        path=directory/'project'/f.REL;spec=a.strict_json(a.read(path))
        require(spec['services'][service].get('healthcheck')=={'disable':True}
                and spec['services'][service].get('network_mode')=='none','COLD_NATIVE_FIXTURE_RECIPE_CHANGED')
        for cfg in spec['services'].values():
            require(cfg.get('image')==record['image'] and cfg.get('pull_policy')=='never'
                    and 'build' not in cfg and cfg.get('labels',{}).get(a.isolated.LABEL)==record['owner'],
                    'ACCEPTANCE_UNSAFE_COMPOSE')
        owned=self.owned(directory,record)
        for ident in self.ids(['label=com.docker.compose.project='+item['project']]):
            require(ident in owned,'ACCEPTANCE_PROJECT_COLLISION')
        require(validate(directory)==record,'COLD_NATIVE_RUN_CHANGED')
        self.command(['compose','-p',item['project'],'-f',str(path),'up','--no-build','--pull','never',
            '-d','--no-deps','--no-recreate',service],timeout=60)
        self.wait_healthless_fixture(directory,record,service)

    def wait_healthless_fixture(self,directory,record,service):
        previous=getattr(self,'_fixture_start_until',None)
        until=time.monotonic()+45;self._fixture_start_until=until;bound=None
        try:
            while True:
                require(time.monotonic()<until,'COLD_NATIVE_FIXTURE_START_TIMEOUT')
                require(validate(directory)==record,'COLD_NATIVE_RUN_CHANGED');found=[]
                for ident in self.owned(directory,record):
                    row=self.metadata(ident)['identity']
                    if row['project']==record['compose'][f.REL]['project'] and row['service']==service:
                        found.append((ident,row))
                require(len(found)==1,'COLD_NATIVE_FIXTURE_NOT_UNIQUE')
                ident,row=found[0]
                require(bound is None or ident==bound,'COLD_NATIVE_FIXTURE_REPLACED');bound=ident
                state,health=self.fixture_start_state(ident)
                require(isinstance(health,dict) and health.get('Test')==['NONE'],
                        'COLD_NATIVE_FIXTURE_HEALTHCHECK_CHANGED')
                require(isinstance(state,dict) and type(state.get('Running')) is bool
                        and type(state.get('Pid')) is int and type(state.get('ExitCode')) is int
                        and type(state.get('OOMKilled')) is bool and isinstance(state.get('Error'),str),
                        'COLD_NATIVE_FIXTURE_STATE_INVALID')
                require(not state['OOMKilled'] and not state['Error'] and state['ExitCode']==0
                        and state.get('Health') is None,'COLD_NATIVE_FIXTURE_START_FAILED')
                if state.get('Status')=='running' and state['Running'] and state['Pid']>0:
                    require(ident in self.owned(directory,record),'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
                    current=self.metadata(ident)['identity']
                    require(current['running'] is True and current['project']==row['project']
                            and current['service']==service,'COLD_NATIVE_FIXTURE_START_FAILED')
                    require(validate(directory)==record,'COLD_NATIVE_RUN_CHANGED')
                    require(time.monotonic()<until,'COLD_NATIVE_FIXTURE_START_TIMEOUT')
                    return
                require(state.get('Status')=='created' and not state['Running'] and state['Pid']==0,
                        'COLD_NATIVE_FIXTURE_START_FAILED')
                remaining=until-time.monotonic()
                require(remaining>0,'COLD_NATIVE_FIXTURE_START_TIMEOUT');time.sleep(min(.1,remaining))
        finally:self._fixture_start_until=previous

    def fixture_start_state(self,ident):
        require(re.fullmatch('[a-f0-9]{64}',ident),'INVALID_CONTAINER_ID')
        return a.strict_json(self.command(['container','inspect','--format',FIXTURE_START_FORMAT,ident]))

    def fixture_id(self,directory,role):
        record=validate(directory);found=[]
        for ident in self.owned(directory,record):
            row=self.metadata(ident)['identity']
            if row['project']==record['prefix']+'-langfuse' and row['service']==role:found.append(ident)
        require(len(found)==1,'COLD_NATIVE_FIXTURE_NOT_UNIQUE');return found[0]

    def initialize_storage(self,directory):
        record=validate(directory);fixture=a.strict_json(a.read(directory/'cold-run.json'))['fixture']
        backend,host=backend_for(directory,self);backend.verify_host()
        image,volumes=backend.query(['image','inspect','--format',storage.IMAGE_FORMAT,record['image']])
        require(image==record['image'] and not volumes,'COLD_NATIVE_IMAGE_VOLUME_UNSUPPORTED')
        source=Path(fixture['source']);root=Path(host['docker_root'])
        require(not source.is_relative_to(root) and not root.is_relative_to(source),'COLD_DOCKER_ROOT_REFUSED')
        self.compose(directory,f.REL,f.SETUP);ident=self.fixture_id(directory,f.SETUP)
        # Metadata only: root is used solely to initialize this new fixture.
        fmt='[{{json .Config.User}},{{json .HostConfig.NetworkMode}},{{json .HostConfig.ReadonlyRootfs}},{{json .Mounts}},{{json .HostConfig.Privileged}}]'
        user,net,readonly,mounts,privileged=a.strict_json(self.command(['container','inspect','--format',fmt,ident]))
        require(user=='0:0' and net=='none' and readonly is True and privileged is False
                and len(mounts)==1 and mounts[0]['Type']=='bind' and mounts[0]['Source']==fixture['source']
                and mounts[0]['Destination']=='/data' and mounts[0]['RW'] is True,'COLD_NATIVE_INITIALIZER_SCOPE_CHANGED')
        code='import json,os; s=os.stat("/data");print(json.dumps([s.st_dev,s.st_ino,s.st_uid,s.st_gid,s.st_mode&0o7777]))'
        until=time.monotonic()+20
        while True:
            current=a.strict_json(self.command(['exec',ident,'python3','-I','-B','-c',code]))
            if current==[*fixture['identity'][:2],fixture['uid'],fixture['gid'],0o700]:break
            require(time.monotonic()<until,'COLD_NATIVE_INITIALIZER_TIMEOUT');time.sleep(.1)
        require(f.identity(directory/'cold-source')==current,'COLD_NATIVE_UID_MAPPING_CHANGED')
        require(ident in self.owned(directory,record),'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
        self.command(['container','stop','--timeout','5',ident])
        state=a.strict_json(self.command(['container','inspect','--format','{{json .State}}',ident]))
        require(state['Status']=='exited' and state['ExitCode']==0 and state['Pid']==0 and not state['OOMKilled'],'COLD_NATIVE_INITIALIZER_EXIT_UNKNOWN')
        require(ident in self.owned(directory,record),'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
        self.command(['container','rm',ident])
        require(ident not in self.owned(directory,record),'COLD_NATIVE_INITIALIZER_REMOVAL_UNKNOWN')
        for role in f.ROLES:self.compose(directory,f.REL,role)
        return current

    def initializer_observation(self,directory,record):
        marker=a.strict_json(a.read(directory/'cold-run.json'));fixture=marker['fixture']
        source=f.identity(directory/'cold-source')
        require(source[:2]==fixture['identity'][:2],'COLD_NATIVE_SOURCE_CHANGED')
        require(source[2:] in (fixture['identity'][2:],[fixture['uid'],fixture['gid'],0o700]),'COLD_NATIVE_SOURCE_CHANGED')
        ownership='original_actor' if source[2:]==fixture['identity'][2:] else 'selected_storage_uid'
        found=[]
        for ident in self.owned(directory,record):
            row=self.metadata(ident)['identity']
            if row['project']==record['prefix']+'-langfuse' and row['service']==f.SETUP:found.append(ident)
        require(len(found)<=1,'COLD_NATIVE_FIXTURE_NOT_UNIQUE')
        if not found:return {'status':'absent','source_ownership':ownership}
        fmt='[{{json .State.Status}},{{json .State.Running}},{{json .State.ExitCode}},{{json .State.OOMKilled}},{{json .State.Error}}]'
        status,running,exit_code,oom,error=a.strict_json(self.command(['container','inspect','--format',fmt,found[0]]))
        reason='NONE' if not error else fixture_reason('',error)[0]
        value={'status':status,'running':running,'exit_code':exit_code,'oom_killed':oom,
            'error_reason':reason,'source_ownership':ownership}
        require(safe_initializer_observation(value) is not None,'COLD_NATIVE_INITIALIZER_METADATA_INVALID')
        return value

    def cleanup(self,directory,legacy_cleanup=False):
        record=validate(directory,legacy_cleanup=legacy_cleanup)
        previous=getattr(self,'_cold_context',None)
        self._cold_context={'phase':'cleanup','fixture_role':None}
        observation=None
        # An unknown Docker reader cannot be killed/adopted from a saved ID.
        # Prove unit exit first; preserve the run if any reader is unaccounted for.
        try:
            with a.lifecycle_lock(directory):
                unit=a.native.cleanup_unit_subprocess(directory)
                require(unit.get('worker_exit_verified') is True,'COLD_NATIVE_UNIT_EXIT_UNKNOWN')
                marker=a.strict_json(a.read(directory/'cold-run.json'))
                for job in marker['operations']:
                    require(not self.ids(['label='+storage.LABEL+'='+job]),'COLD_NATIVE_READER_CLEANUP_REQUIRED')
                observation=self.initializer_observation(directory,record)
            value=super().cleanup(directory)
            value['initializer_observation_before_cleanup']=observation
            return value
        except BaseException as exc:
            exc.initializer_observation=observation
            exc.failure_phase='cleanup'
            raise
        finally:self._cold_context=previous


class NativeDocker(storage.Docker):
    """Optional-field formatting and bounded diagnostics; candidate gates intact."""
    def query(self,args):
        if len(args)==5 and args[:3]==['container','inspect','--format'] and args[3]==storage.FORMAT:
            require(storage.HEX.fullmatch(args[4]),'COLD_CONTAINER_IDENTITY_INVALID')
            args=[*args[:3],FORMAT,args[4]]
        elif len(args)==5 and args[:3]==['image','inspect','--format'] and args[3]==storage.IMAGE_FORMAT:
            require(re.fullmatch('sha256:[0-9a-f]{64}',args[4]),'COLD_NATIVE_IMAGE_IDENTITY_INVALID')
            args=[*args[:3],IMAGE_FORMAT,args[4]]
        try:rc,out,err=self.call(args)
        except age_tool.Failure as exc:
            if exc.code not in ('COLD_DOCKER_OUTCOME_UNKNOWN','COLD_DOCKER_RESPONSE_LIMIT'):raise
            raise query_failure(args,exc.code) from None
        except OSError:raise query_failure(args,'COLD_NATIVE_DOCKER_UNAVAILABLE') from None
        if rc!=0:raise query_failure(args,'COLD_DOCKER_METADATA_FAILED',rc,err) from None
        try:return json.loads(out)
        except (ValueError,UnicodeError):raise query_failure(args,'COLD_DOCKER_METADATA_INVALID',rc) from None


def backend_for(directory,engine):
    record=validate(directory)
    ident,system,security=a.strict_json(engine.command(['info','--format',storage.HOST_FORMAT]))
    root=engine.command(['info','--format','{{.DockerRootDir}}']).strip()
    require(system=='linux' and not any('rootless' in x or 'userns' in x for x in security or [])
            and Path(root).is_absolute(),'COLD_NATIVE_ROOTFUL_LOCAL_REQUIRED')
    host={'endpoint':record['endpoint'],'daemon_id':ident,'docker_root':root}
    binary=shutil.which('docker');require(binary is not None,'COLD_NATIVE_DOCKER_UNAVAILABLE')
    return NativeDocker(host,binary),host


def operation(directory,engine,tool,recipient):
    record=validate(directory);backend,host=backend_for(directory,engine)
    services=f.discover(directory,record,engine,backend)
    image,volumes=backend.query(['image','inspect','--format',storage.IMAGE_FORMAT,record['image']])
    require(image==record['image'] and not volumes,'COLD_NATIVE_IMAGE_VOLUME_UNSUPPORTED')
    active=backend.running();scope={r['container_id'] for r in services.values()}
    protected={k:v for k,v in active.items() if k not in scope}
    destination=directory/'cold-encrypted';destination.mkdir(mode=0o700,exist_ok=True)
    p=storage.plan(host,services,protected,{'storage':{'source':str(directory/'cold-source'),'target':'/data','service':'cold-db'}},
        {'image_id':record['image'],'python':'python3'},{'cold-client':['cold-db'],'cold-db':[]},destination,
        public_recipients=[recipient],seconds=300,stop_seconds=5,max_bytes=1024**2,max_entries=16)
    marker=a.strict_json(a.read(directory/'cold-run.json'))
    a.isolated.write(directory/('cold-plan-'+p['id']+'.json'),p)
    marker['operations'].append(p['id']);a.isolated.write(directory/'cold-run.json',marker)
    validate(directory)
    return storage.Operation(p,backend,tool,[recipient])


def callback(op,directory):
    def apply(scope):
        a.leased_child(scope,directory,LIFECYCLE_FD)
        try:return op(scope)
        finally:
            # Use only helpers created by this live invocation and exact recipes.
            # This covers verification failure between create and registration.
            for ident,spec in list(op.backend.owned_helpers.items()):
                op.backend.helper(ident,spec);op.backend.abort(ident,spec)
                op.helpers.pop(ident,None)
    return apply


def verify_ciphertext(op,tool,identity_path):
    require(op.record['phase']=='services_returned' and len(op.record['captures'])==1,'COLD_NATIVE_CAPTURE_UNACCEPTED')
    item=op.record['captures'][0];folder=Path(item['job_directory']);receipt=item['receipt']
    require(a.strict_json(a.read(folder/'receipt.json'))==receipt and receipt['capture_only'] is True
            and receipt['restore_verified'] is False and age_tool.sha(folder/'payload.age')==receipt['ciphertext_sha256'],
            'COLD_NATIVE_RECEIPT_CHANGED')
    fd=os.open(folder/'payload.age',os.O_RDONLY|os.O_NOFOLLOW)
    child=None
    try:
        child=tool.spawn(['-d','-i',str(identity_path)],input_fd=fd,lease=(LIFECYCLE_FD,) if LIFECYCLE_FD is not None else ())
        until=time.monotonic()+30
        pipe=age_tool.Pipe(child.stdout.fileno(),'read',until,lambda:None,read_limit=1024**2)
        chunks=[]
        while True:
            block=pipe.read(65536)
            if not block:break
            chunks.append(block)
        tool.finish(child,until);out=b''.join(chunks)
    finally:tool.stop(child);os.close(fd)
    # Bounded in-memory verification. No extract, chown or production restore.
    with tarfile.open(fileobj=io.BytesIO(out),mode='r:') as archive:
        members=archive.getmembers();require(len(members)==3,'COLD_NATIVE_TAR_SCOPE_INVALID')
        by={x.name:x for x in members}
        require(set(by)=={'data/storage','data/storage/payload.bin','manifest.json'},'COLD_NATIVE_TAR_SCOPE_INVALID')
        require(by['data/storage'].isdir() and by['data/storage/payload.bin'].isfile() and by['manifest.json'].isfile(),'COLD_NATIVE_TAR_SCOPE_INVALID')
        raw=archive.extractfile(by['manifest.json']).read();manifest=json.loads(raw)
        payload=archive.extractfile(by['data/storage/payload.bin']).read()
        require(payload==f.PAYLOAD and hashlib.sha256(raw).hexdigest()==receipt['manifest_sha256'],'COLD_NATIVE_CONTENT_MISMATCH')
        rows={x['path']:x for x in manifest['entries']}
        uid,gid=op.p['stores']['storage']['uid'],op.p['stores']['storage']['gid']
        require(manifest['id']==receipt['id'] and set(rows)=={'data/storage','data/storage/payload.bin'} and
            all(x['uid']==uid and x['gid']==gid for x in rows.values()) and
            rows['data/storage/payload.bin']['sha256']==hashlib.sha256(payload).hexdigest() and
            by['data/storage/payload.bin'].uid==uid and by['data/storage/payload.bin'].gid==gid and by['data/storage/payload.bin'].mode==0o600,
            'COLD_NATIVE_UID_MANIFEST_MISMATCH')
        require(manifest['quiescence_identity']==capture.fingerprint(op.p) and receipt['entry_count']==2
                and receipt['logical_bytes']==len(payload),'COLD_NATIVE_MANIFEST_BINDING_CHANGED')
    return {'ciphertext_integrity_verified':True,'plaintext_verified_in_memory_only':True,'storage_uid':uid,'storage_gid':gid,
            'foreign_uid_restore_performed':False}


def progress(directory,engine,phase,message):
    global CURRENT_PHASE
    require(phase in PHASES,'COLD_NATIVE_PHASE_INVALID');CURRENT_PHASE=phase
    engine.protect(validate(directory)['protected'])
    print('[cold-acceptance] '+message,file=sys.stderr,flush=True)


def exercise(directory,engine,tools_dir):
    require(ROOT==directory/'project','COLD_NATIVE_COPIED_ENTRY_REQUIRED');validate(directory)
    tool=age_tool.Age(tools_dir/'age',age_tool.sha(tools_dir/'age'))
    identity_path,recipient=age_tool.test_identity(tools_dir)
    progress(directory,engine,'preparation','prepare isolated Ops and boot-policy worker')
    context=a.isolated.prepare_test_ops(directory,engine);state=a.checked_state(directory);ctl=a.native.Controller(state)
    first=a.initialize_boot_fixture(state,ctl);store=a.ws.Store(state);manifest=a.service.load(state)
    fixed={name:a.read(store.path.parent/name,32768) for name in ('boot-start.json','code-transition.json','service.json')}
    configs={name:a.read(state/name) for name in ('config.json','host-config.json','owner.json','access-key','compose.env')}
    progress(directory,engine,'capture','pause test worker; cleanly stop two storage fixtures; foreign-UID reader to age; return same service IDs')
    op=operation(directory,engine,tool,recipient);controller=a.core.Maintenance(state,ctl.backend,inventory_fn=op.inventory,readback_fn=a.ReadbackProbe(directory))
    proposal=a.plan_wait(controller);result=controller.run(proposal,callback(op,directory))
    require(result.get('result')=='passed',op.record.get('code') if op.record else result.get('code','COLD_NATIVE_CALLBACK_FAILED'))
    require(store.control()['desired']=='paused' and not a.service.enabled(manifest) and not op.helpers and not op.backend.owned_helpers,
            'COLD_NATIVE_CALLBACK_RETURNED_WORKER')
    require(op.record['stopped']==['cold-client','cold-db'] and op.record['started']==['cold-db','cold-client'],'COLD_NATIVE_DEPENDENCY_ORDER_FAILED')
    progress(directory,engine,'verify_ciphertext','verify ciphertext receipt and decrypted fixture bytes/UID metadata in memory')
    verification=verify_ciphertext(op,tool,identity_path)
    progress(directory,engine,'return_worker','explicit return with real Ops readback and fresh worker receipt/evaluation')
    result=controller.resume(proposal['id']);require(result.get('fresh_evaluation_verified') is True,result.get('code','COLD_NATIVE_WORKER_RETURN_FAILED'))
    current=ctl.status();require(a.native.healthy(current) and a.native.invocation(current)!=a.native.invocation(first),'COLD_NATIVE_STALE_WORKER')
    controls=ctl.backend.control_commands
    a.rejected(lambda:controller.resume(proposal['id']),{'MAINTENANCE_RETURN_REFUSED'})
    require(ctl.backend.control_commands==controls,'COLD_NATIVE_DUPLICATE_START')
    follow=ctl.wait(lambda v:a.native.healthy(v,a.native.observation(current)['completed_cycles']+1))
    require(a.native.invocation(follow)==a.native.invocation(current) and not a.native.observation(follow)['outcome']['restart_requested'],
            'COLD_NATIVE_DUPLICATE_REFRESH')
    progress(directory,engine,'failure_gate','second job: fixture symlink makes reader fail; no automatic service or worker return')
    db=engine.fixture_id(directory,'cold-db');record=validate(directory)
    require(db in engine.owned(directory,record),'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
    engine.command(['exec',db,'python3','-I','-B','-c','import os;os.symlink("payload.bin","/data/rejected-link")'])
    bad=operation(directory,engine,tool,recipient);other=a.core.Maintenance(state,ctl.backend,inventory_fn=bad.inventory,readback_fn=a.ReadbackProbe(directory))
    second=a.plan_wait(other);failed=other.run(second,callback(bad,directory))
    require(failed.get('result')=='attention_required' and bad.record['phase']=='attention_required'
            and len(bad.record['stopped'])==2 and bad.record['started']==[] and not bad.helpers and not bad.backend.owned_helpers,
            'COLD_NATIVE_FAILURE_GATE_MISSING')
    for row in bad.p['services'].values():
        actual=bad.backend.inspect(row)['state'];require(actual['Running'] is False and actual['Pid']==0,'COLD_NATIVE_FAILED_SERVICE_RETURNED')
    controls=ctl.backend.control_commands
    a.rejected(lambda:other.resume(second['id']),{'MAINTENANCE_RETURN_REFUSED'})
    require(ctl.backend.control_commands==controls and store.control()['desired']=='paused' and not a.service.enabled(manifest),
            'COLD_NATIVE_FAILURE_REPLAYED')
    require({name:a.read(store.path.parent/name,32768) for name in fixed}==fixed and {name:a.read(state/name) for name in configs}==configs
            and a.worker.code_hash(ROOT)==a.core.CODE,'COLD_NATIVE_FIXED_STATE_CHANGED')
    a.isolated.preserve_test_ops(context,engine)
    progress(directory,engine,'remove_unit','remove owned unit before parent verifies reader absence and Docker cleanup')
    removed=a.native.cleanup_unit(directory)
    require(removed.get('worker_exit_verified') and removed.get('unit_absence_verified'),'COLD_NATIVE_UNIT_REMOVAL_FAILED')
    cases={key:'passed' for key in ('dynamic_foreign_uid_and_exact_mount_discovery','native_clean_stop_dependency_order',
        'real_inherited_host_leases','networkless_readonly_uid_probe_and_reader','native_age_ciphertext_and_receipt',
        'decrypted_bytes_hash_and_uid_metadata','same_container_reverse_service_return','callback_keeps_worker_paused',
        'explicit_readback_receipt_new_invocation_evaluation','duplicate_return_cannot_start_again','unchanged_followup',
        'native_reader_failure_keeps_services_and_worker_stopped','failed_job_cannot_return_or_replay',
        'policy_code_credentials_cache_history_preserved','owned_unit_exit_and_removal')}
    return {'schema':1,'stage':STAGE,'action':'worker','result':'passed','checked_at':a.rc.now(),'cases':cases,
        'scope':'native_rootful_docker_foreign_uid_synthetic_files_user_unit_same_boot','verification':verification,
        'manager_version':ctl.manager_version,'image_id':record['image'],'runtime_source_hashes_verified':context['hashes'],
        'active_code_sha256':a.core.CODE,'maintenance_jobs':2,'job_phases':['returned','attention_required'],
        'scoped_services':2,'test_ops_restart_requested':False,'test_unit_removed':True,'test_worker_exit_verified':True,
        'synthetic_credentials_preserved':True,'initial_cache_entries_preserved':len(context['initial_cache']['success_times']),
        'production_state_read':False,'production_credentials_read':False,'production_worker_stopped':False,
        'production_service_control_requested':False,'production_application_api_requests':0,
        'production_backup_executed':False,'production_restore_executed':False,'application_restore_acceptance':False,
        'foreign_uid_restore_acceptance':False,'image_export_restore_acceptance':False,'backup_ready':False,
        'production_backup_executor_integration':False,'inference_requested':False,'image_build_or_pull_requested':False,
        'linger_changed':False,'actual_reboot_acceptance':'deferred_by_user','kubernetes_acceptance':'deferred',
        'long_running_acceptance':False,'initial_boot_history':'fixture_only_with_fresh_evaluation',
        'fixture_source_retained':'foreign_owned_inside_private_run_no_permission_repair'}


def invoke(directory,tools_dir,timeout=850):
    record=validate(directory);project=directory/'project'
    env={k:v for k,v in os.environ.items() if not k.startswith(('OPS_','COMPOSE_','PYTHON'))
         and k not in ('DOCKER_CONTEXT','DOCKER_API_VERSION','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}
    env.update(DOCKER_HOST=record['endpoint'],PYTHONDONTWRITEBYTECODE='1')
    path=directory/'cold-worker.log'
    with path.open('w') as out:
        path.chmod(0o600)
        child=subprocess.Popen([sys.executable,'-I','-B',str(project/ENTRY),'worker','--run-dir',str(directory),
            '--test-tools-dir',str(tools_dir)],cwd=project,env=env,stdin=subprocess.DEVNULL,stdout=out,start_new_session=True)
        try:code=child.wait(timeout=timeout)
        except BaseException:a.isolated.stop_worker(child);raise
    report=a.strict_json(a.read(path,128*1024))
    if code!=0 or report.get('result')!='passed':
        exc=a.ReadFailure(report.get('code','COLD_NATIVE_CHILD_FAILED'));exc.docker_diagnostic=report.get('docker_diagnostic')
        exc.storage_docker_diagnostic=safe_query_diagnostic(report.get('storage_docker_diagnostic'))
        exc.fixture_docker_diagnostic=safe_fixture_diagnostic(report.get('fixture_docker_diagnostic'))
        phase=report.get('failure_phase')
        if isinstance(phase,str) and phase in PHASES:exc.failure_phase=phase
        raise exc
    require(report.get('stage')==STAGE and report.get('action')=='worker','COLD_NATIVE_REPORT_CHANGED')
    return report


def run(base,fetch_test_age=False):
    require(fetch_test_age,'COLD_NATIVE_FETCH_TEST_AGE_REQUIRED');preflight()
    with a.isolated.run_lock(base):
        engine=Engine();directory,record=create_run(base,engine)
        print('[cold-acceptance] evidence: '+str(directory),file=sys.stderr,flush=True)
        result={'schema':1,'stage':STAGE,'result':'attention_required','production_backup_executed':False,
                'production_worker_stopped':False,'production_service_control_requested':False,'inference_requested':False}
        try:
            with tempfile.TemporaryDirectory(prefix='ops-cold-native-tools-') as folder:
                tools_dir=Path(folder);tool_info=age_tool.fetch(tools_dir)
                engine.create_fixture_network(directory)
                for role,(name,subtree) in a.isolated.ROLES.items():
                    rel=str(Path(subtree)/('acceptance-'+role+'.json'));engine.compose(directory,rel,name)
                    if role=='model':engine.compose(directory,rel,'manual-model')
                progress(directory,engine,'storage_setup','initialize only new private storage, then remove root fixture initializer')
                engine.initialize_storage(directory)
                result=invoke(directory,tools_dir);result['test_tool']=tool_info
            result['temporary_tools_and_identity_cleanup']='passed'
        except BaseException as exc:
            result.update(error_report(exc));result['temporary_tools_and_identity_cleanup']='passed_if_created_context_closed'
        finally:
            try:cleanup=engine.cleanup(directory)
            except BaseException as exc:cleanup=error_report(exc);result['result']='attention_required'
            if cleanup.get('result')!='passed':result['result']='attention_required'
            if cleanup.get('prior_run_protected_baseline_match') is False:
                result.update(result='attention_required',code='ACCEPTANCE_PROTECTED_CONTAINER_CHANGED')
            result.update(run_directory=str(directory),cleanup=cleanup)
            a.isolated.write(directory/'cold-report.json',result)
        return result


def main():
    global LIFECYCLE_FD
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('preflight','run','worker','cleanup'));parser.add_argument('--run-dir',type=Path)
    parser.add_argument('--state-dir',type=Path,default=a.isolated.BASE)
    parser.add_argument('--fetch-test-age',action='store_true');parser.add_argument('--test-tools-dir',type=Path)
    parser.add_argument('--legacy-cleanup',action='store_true',help='cleanup only the exact reviewed previous cold runner copy')
    args=parser.parse_args()
    try:
        require(not args.legacy_cleanup or args.action=='cleanup','COLD_NATIVE_LEGACY_CLEANUP_ONLY')
        if args.action in ('preflight','run'):
            require(args.run_dir is None and args.test_tools_dir is None,'COLD_NATIVE_RUN_REFERENCE_INVALID')
            result=preflight() if args.action=='preflight' else run(args.state_dir,args.fetch_test_age)
        else:
            require(args.run_dir is not None,'ACCEPTANCE_RUN_DIR_REQUIRED');directory=a.manage.safe(args.run_dir)
            record=validate(directory,legacy_cleanup=args.legacy_cleanup)
            if args.action=='worker':
                require(ROOT==directory/'project' and args.test_tools_dir is not None,'COLD_NATIVE_COPIED_ENTRY_REQUIRED')
                age_tool.private(args.test_tools_dir)
                with a.lifecycle_lock(directory) as fd:
                    LIFECYCLE_FD=fd
                    try:result=exercise(directory,Engine(record['endpoint']),args.test_tools_dir)
                    finally:LIFECYCLE_FD=None
            else:
                require(args.test_tools_dir is None,'COLD_NATIVE_RUN_REFERENCE_INVALID')
                with a.isolated.run_lock(directory.parent):
                    result={'schema':1,'stage':STAGE,'result':'passed','run_directory':str(directory),
                            'cleanup':Engine(record['endpoint']).cleanup(directory,legacy_cleanup=args.legacy_cleanup),
                            'legacy_source_reviewed':args.legacy_cleanup,'acceptance_result_not_reused':True,
                            'production_worker_stopped':False,'production_service_control_requested':False}
    except BaseException as exc:result=error_report(exc)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['result']=='passed' else 2

if __name__=='__main__':raise SystemExit(main())
