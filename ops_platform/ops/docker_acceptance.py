#!/usr/bin/env python3
"""Isolated Docker first-install/refresh acceptance using an existing Ops image."""
from copy import deepcopy
from contextlib import contextmanager
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from infra.observability.ops.backup_preflight import Unavailable
from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import manage, restart_check as rc, set_public_url as change
from ops_platform.ops.discovery_compose import DockerDiscovery, ROLES, endpoints
from ops_platform.transport import Transport, strict_json

STAGE='O04-isolated-docker-discovery-acceptance'
LABEL='org.agent-project.ops.acceptance'
BASE=Path.home()/'.local/state/agent-project-ops/docker-acceptance'
SCENARIOS={'discovery':('docker_acceptance.py',STAGE,420),
           'watch':('watch_acceptance.py','O04-isolated-docker-watch-acceptance',600),
           'service':('service_acceptance.py','O04-isolated-systemd-docker-acceptance',900)}
META='{"identity":'+rc.INSPECT+',"acceptance":{{json (index .Config.Labels "'+LABEL+'")}},"files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}}}'
PROTECTED='{"id":{{json .Id}},"image":{{json .Image}},"started":{{json .State.StartedAt}},"running":{{json .State.Running}}}'


def write(path, value):
    path=manage.safe(path)
    require(path.parent.is_dir(),'ACCEPTANCE_PARENT_MISSING')
    if path.exists(): manage.private(path)
    rc.write_record(path,value)


@contextmanager
def run_lock(base):
    base=manage.safe(base);base.mkdir(parents=True,mode=0o700,exist_ok=True);manage.private(base)
    path=manage.safe(base/'run.lock')
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        manage.private(path)
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ReadFailure('ACCEPTANCE_RUN_BUSY') from None
        yield
    finally: os.close(fd)


def free_port(exclude=()):
    for _ in range(20):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        if port not in exclude: return port
    raise ReadFailure('ACCEPTANCE_PORT_UNAVAILABLE')


def source_files(root):
    paths=list((root/'ops_platform').glob('*.py'))+list((root/'ops_platform/ops').glob('*.py'))
    for folder in ('web','profiles'):
        paths.extend(p for p in (root/'ops_platform'/folder).rglob('*') if p.is_file())
    # Match the guarded worker's host-code closure, without tests or state.
    for folder in ('infra/observability/ops', 'infra/langfuse/ops'):
        paths.extend((root/folder).rglob('*.py'))
    return sorted(paths)


def copy_source(root, target):
    hashes={}
    for p in source_files(root):
        manage.safe(p); require(p.is_file() and p.stat().st_size < 1024*1024,'ACCEPTANCE_SOURCE_INVALID')
        rel=p.relative_to(root); dest=target/rel; dest.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
        raw=p.read_bytes(); dest.write_bytes(raw); dest.chmod(0o600)
        hashes[str(rel)]=hashlib.sha256(raw).hexdigest()
    return hashes


def validate_run(directory):
    directory=manage.private(directory)
    record=strict_json(rc.read_file(directory/'run.json',512*1024,private=True)[0])
    require(record.get('schema')==1 and record.get('stage')==STAGE and record.get('directory')==str(directory)
            and record.get('uid')==os.getuid() and record.get('gid')==os.getgid(),'ACCEPTANCE_OWNER_MISMATCH')
    require(record.get('scenario','discovery') in SCENARIOS,'ACCEPTANCE_SCENARIO_INVALID')
    require(str(uuid.UUID(record['owner']))==record['owner'] and
            record['prefix']=='ops-accept-'+record['owner'].replace('-','')[:12],'ACCEPTANCE_OWNER_MISMATCH')
    require(record.get('endpoint') in ('unix:///var/run/docker.sock','unix:///run/docker.sock')
            and re.fullmatch('sha256:[a-f0-9]{64}',record.get('image','')),'ACCEPTANCE_IDENTITY_INVALID')
    expected={str(Path(subtree)/('acceptance-'+role+'.json')) for role,(_,subtree) in ROLES.items()}
    expected.add('ops_platform/docker-compose.yml')
    require(set(record['compose'])==expected,'ACCEPTANCE_SPEC_SET_INVALID')
    for rel,item in record['compose'].items():
        path=directory/'project'/rel
        raw=rc.read_file(path,128*1024,private=True)[0]
        require(hashlib.sha256(raw).hexdigest()==item['sha256'],'ACCEPTANCE_COMPOSE_CHANGED')
        require(item['project'].startswith(record['prefix']+'-'),'ACCEPTANCE_PROJECT_MISMATCH')
        manage.project_name(item['project'])
    return record


class Engine:
    def __init__(self, endpoint=None):
        docker=DockerDiscovery(); docker.host()
        require(endpoint is None or endpoint==docker.endpoint,'ACCEPTANCE_DAEMON_CHANGED')
        self.endpoint,self.env=docker.endpoint,docker.env
        self.env={k:v for k,v in self.env.items() if not k.startswith(('OPS_','COMPOSE_'))}
    def command(self,args,timeout=30):
        try:
            result=subprocess.run(['docker','--host',self.endpoint,*args],env=self.env,
                capture_output=True,text=True,timeout=timeout,check=False)
        except subprocess.TimeoutExpired: raise ReadFailure('ACCEPTANCE_COMMAND_UNCERTAIN') from None
        except OSError: raise ReadFailure('ACCEPTANCE_DOCKER_UNAVAILABLE') from None
        require(result.returncode==0,'ACCEPTANCE_DOCKER_COMMAND_FAILED')
        require(len(result.stdout)<=1024*1024,'ACCEPTANCE_DOCKER_RESPONSE_LIMIT')
        return result.stdout
    def ids(self,filters=(),all_containers=True):
        args=['ps','-aq' if all_containers else '-q','--no-trunc']
        for item in filters: args+=['--filter',item]
        ids=self.command(args).split()
        require(len(ids)<=512 and all(re.fullmatch('[a-f0-9]{64}',i) for i in ids),'ACCEPTANCE_CONTAINER_LIMIT')
        return ids
    def metadata(self,ident):
        require(re.fullmatch('[a-f0-9]{64}',ident),'INVALID_CONTAINER_ID')
        return strict_json(self.command(['inspect','--type','container','--format',META,ident]))
    def snapshot(self,ids=None):
        ids=self.ids(all_containers=False) if ids is None else ids
        if not ids: return {}
        values=self.command(['inspect','--format',PROTECTED,*ids])
        rows=[strict_json(line) for line in values.splitlines()]
        require(len(rows)==len(ids),'ACCEPTANCE_PROTECTED_LIST_CHANGED')
        return {row['id']:row for row in rows}
    def protect(self,before):
        require(self.snapshot(list(before))==before,'ACCEPTANCE_PROTECTED_CONTAINER_CHANGED')
    def image(self,root):
        candidates=[]
        for ident in self.ids(['label=com.docker.compose.service=ops-web']):
            row=self.metadata(ident)
            if row['files']==str(root/'ops_platform/docker-compose.yml') and row['identity']['running']:
                candidates.append(row['identity']['image'])
        require(len(candidates)==1,'ACCEPTANCE_EXISTING_OPS_IMAGE_NOT_UNIQUE')
        image=candidates[0]
        require(re.fullmatch('sha256:[a-f0-9]{64}',image),'INVALID_IMAGE')
        require(self.command(['image','inspect','--format','{{.Id}}',image]).strip()==image,'ACCEPTANCE_IMAGE_UNAVAILABLE')
        return image
    def owned(self,directory,record):
        ids=self.ids(['label='+LABEL+'='+record['owner']])
        allowed={item['project']:str(directory/'project'/rel) for rel,item in record['compose'].items()}
        for ident in ids:
            row=self.metadata(ident); identity=row['identity']
            require(row['acceptance']==record['owner'] and identity['image']==record['image']
                    and identity['project'] in allowed and row['files']==allowed[identity['project']],
                    'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
        return ids
    def compose(self,directory,relative,service,recreate=False):
        record=validate_run(directory); item=record['compose'][relative]
        path=directory/'project'/relative
        spec=strict_json(path.read_bytes()); require(service in spec['services'],'ACCEPTANCE_UNKNOWN_SERVICE')
        # Validate the complete owned configuration on every invocation, including after a port change.
        for cfg in spec['services'].values():
            require(cfg.get('image')==record['image'] and cfg.get('pull_policy')=='never'
                    and 'build' not in cfg and cfg.get('labels',{}).get(LABEL)==record['owner'],
                    'ACCEPTANCE_UNSAFE_COMPOSE')
        for ident in self.ids(['label=com.docker.compose.project='+item['project']]):
            require(ident in self.owned(directory,record),'ACCEPTANCE_PROJECT_COLLISION')
        args=['compose','-p',item['project'],'-f',str(path),'up','--no-build','--pull','never',
              '-d','--no-deps','--wait','--wait-timeout','45']
        if recreate: args+=['--force-recreate']
        self.command(args+[service],timeout=60)
    def cleanup(self,directory):
        record=validate_run(directory)
        unit_cleanup=None
        if record.get('scenario')=='service':
            # A systemd worker outlives the orchestrator's process group.
            # Prove it stopped before removing any test upstream container.
            from ops_platform.ops.service_acceptance import cleanup_unit_subprocess
            unit_cleanup=cleanup_unit_subprocess(directory)
        owned=self.owned(directory,record)
        for ident in owned:
            # Repeat ownership check immediately before control; no volume/image pruning.
            require(ident in self.owned(directory,record),'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
            self.command(['container','rm','-f',ident],timeout=30)
        networks=self.command(['network','ls','--no-trunc','-q','--filter','label='+LABEL+'='+record['owner']]).split()
        require(len(networks)<=8 and all(re.fullmatch('[a-f0-9]{64}',v) for v in networks),'ACCEPTANCE_NETWORK_LIMIT')
        for ident in networks:
            fmt='{"owner":{{json (index .Labels "'+LABEL+'")}},"containers":{{json .Containers}}}'
            net=strict_json(self.command(['network','inspect','--format',fmt,ident]))
            require(net['owner']==record['owner'] and not net['containers'],'ACCEPTANCE_NETWORK_IN_USE')
            self.command(['network','rm',ident])
        require(not self.owned(directory,record),'ACCEPTANCE_CLEANUP_INCOMPLETE')
        self.protect(record['protected'])
        result={'owned_containers_removed':len(owned),'owned_networks_removed':len(networks),
                'protected_containers_unchanged':len(record['protected']),'images_or_volumes_removed':False}
        if unit_cleanup is not None: result['service']=unit_cleanup
        return result


def mount(source,target,readonly=True):
    return {'type':'bind','source':str(source),'target':target,'read_only':readonly,
            'bind':{'create_host_path':False}}


def fixture_spec(directory,record,role):
    service,subtree=ROLES[role]
    base={'image':record['image'],'pull_policy':'never','user':str(os.getuid())+':'+str(os.getgid()),
          'labels':{LABEL:record['owner']},'restart':'no','read_only':True,'cap_drop':['ALL'],
          'security_opt':['no-new-privileges:true'],'cpus':0.2,'mem_limit':'64m','pids_limit':32,
          'logging':{'driver':'json-file','options':{'max-size':'1m','max-file':'1'}},
          'volumes':[mount(directory/'project','/fixture-source'),mount(directory/'control','/fixture-control')],
          'ports':[{'target':8080,'host_ip':'127.0.0.1','protocol':'tcp'}],
          'healthcheck':{'test':['CMD','python3','-B','-c',
              "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/fixture/ready',timeout=2).read()"],
              'interval':'1s','timeout':'3s','retries':20},
          'command':['python3','-B','/fixture-source/ops_platform/ops/docker_acceptance_fixture.py',role,
                     '/fixture-control/control.json']}
    services={service:base}
    if role=='model':
        services['manual-model']=deepcopy(base)
        services['manual-model']['command'][-2]='manual-model'
    return {'services':services,'networks':{'default':{'labels':{LABEL:record['owner']}}}}


def ops_spec(record):
    service={'image':record['image'],'pull_policy':'never','user':'${OPS_UID:?}:${OPS_GID:?}',
             'labels':{LABEL:record['owner'],'org.agent-project.ops.owner':'${OPS_OWNER:?}'},
             'network_mode':'host','restart':'no','read_only':True,'cap_drop':['ALL'],
             'security_opt':['no-new-privileges:true'],'cpus':1,'mem_limit':'384m','pids_limit':128,
             'tmpfs':['/tmp:size=16777216,mode=1777'],'environment':{'OPS_CONFIG':'/run/ops/config.json'},
             'logging':{'driver':'json-file','options':{'max-size':'1m','max-file':'1'}},
             'healthcheck':{'test':['CMD','python3','-B','/app/ops_platform/healthcheck.py'],
                            'interval':'1s','timeout':'5s','retries':30},
             'volumes':[mount('${'+key+':?}',target,readonly) for key,target,readonly in (
                 ('OPS_CONFIG_FILE','/run/ops/config.json',True),('OPS_ACCESS_FILE','/run/ops/access-key',True),
                 ('OPS_AGENT_TOKEN_FILE','/run/ops/agent-token',True),('OPS_LANGFUSE_FILE','/run/ops/langfuse.json',True),
                 ('OPS_CACHE_DIR','/var/lib/ops',False))]}
    return {'services':{'ops-web':service}}


def create_run(base,engine,scenario='discovery'):
    require(scenario in SCENARIOS,'ACCEPTANCE_SCENARIO_INVALID')
    base=manage.safe(base); base.mkdir(parents=True,mode=0o700,exist_ok=True); manage.private(base)
    owner=str(uuid.uuid4()); directory=base/('run-'+owner); directory.mkdir(mode=0o700)
    project=directory/'project'; project.mkdir(mode=0o700)
    image=engine.image(ROOT); protected=engine.snapshot()
    record={'schema':1,'stage':STAGE,'directory':str(directory),'uid':os.getuid(),'gid':os.getgid(),
            'owner':owner,'prefix':'ops-accept-'+owner.replace('-','')[:12],'image':image,'endpoint':engine.endpoint,
            'protected':protected,'source_hashes':copy_source(ROOT,project),'compose':{},'created_at':rc.now(),
            'scenario':scenario}
    (directory/'control').mkdir(mode=0o700); write(directory/'control/control.json',{'version':0})
    for role,(_,subtree) in ROLES.items():
        folder=project/subtree; folder.mkdir(parents=True,mode=0o700,exist_ok=True)
        rel=str(Path(subtree)/('acceptance-'+role+'.json')); write(project/rel,fixture_spec(directory,record,role))
        record['compose'][rel]={'project':record['prefix']+'-'+role,'sha256':hashlib.sha256((project/rel).read_bytes()).hexdigest()}
    rel='ops_platform/docker-compose.yml'; write(project/rel,ops_spec(record))
    record['compose'][rel]={'project':record['prefix']+'-ops','sha256':hashlib.sha256((project/rel).read_bytes()).hexdigest()}
    (directory/'keys').mkdir(mode=0o700)
    manage.write_new(directory/'keys/agent',secrets.token_urlsafe(32))
    manage.write_new(directory/'keys/langfuse.json',{'project_id':'synthetic-'+secrets.token_hex(12),
        'public_key':'pk-lf-'+secrets.token_hex(16),'secret_key':'sk-lf-'+secrets.token_hex(24)})
    write(directory/'run.json',record)
    validate_run(directory)
    return directory,record


def change_port(directory,engine):
    record=validate_run(directory); rel='model_platform/acceptance-model.json'; path=directory/'project'/rel
    spec=strict_json(path.read_bytes())
    old=model_endpoint(engine,directory)
    port=free_port((int(old.rsplit(':',1)[1]),))
    spec['services']['gateway']['ports'][0]['published']=str(port)
    write(path,spec); record['compose'][rel]['sha256']=hashlib.sha256(path.read_bytes()).hexdigest(); write(directory/'run.json',record)
    engine.compose(directory,rel,'gateway',True)
    require(model_endpoint(engine,directory)!=old,'ACCEPTANCE_PORT_UNCHANGED')


def model_endpoint(engine,directory,manual=False):
    record=validate_run(directory)
    service='manual-model' if manual else 'gateway'
    ids=engine.ids(['label=com.docker.compose.project='+record['prefix']+'-model','label=com.docker.compose.service='+service])
    require(len(ids)==1,'ACCEPTANCE_EXPECTED_ONE_MODEL')
    raw=strict_json(engine.command(['inspect','--format','{{json .NetworkSettings.Ports}}',ids[0]]))
    return endpoints({'ports':raw,'network_mode':'bridge'},'model')[0]


def runtime_hashes(engine,ident,record):
    expected={k:v for k,v in record['source_hashes'].items() if k.startswith('ops_platform/') and '/ops/' not in k}
    code='import hashlib,json,pathlib; names='+repr(sorted(expected))+'; print(json.dumps({n:hashlib.sha256((pathlib.Path("/app")/n).read_bytes()).hexdigest() for n in names}))'
    actual=strict_json(engine.command(['exec',ident,'python3','-B','-c',code]))
    require(actual==expected,'ACCEPTANCE_IMAGE_SOURCE_MISMATCH')
    return len(expected)


def wait_health(directory,timeout=40):
    deadline=time.monotonic()+timeout
    while True:
        try: rc.API(manage.existing(directory)).health(); return
        except ReadFailure:
            if time.monotonic()>=deadline: raise
            time.sleep(.3)


def manual_configs(directory, documents):
    """Simulate a coherent external edit in the disposable state, retaining bind inodes."""
    for name in ('host-config.json','config.json'):
        path=manage.private(directory/name)
        fd=os.open(path,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
        with os.fdopen(fd,'w') as out:
            json.dump(documents[name],out,ensure_ascii=False,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())


def stop_worker(child):
    """Stop the isolated worker and its CLI children before attempting scoped cleanup."""
    try: os.killpg(child.pid,signal.SIGTERM)
    except ProcessLookupError: pass
    try: child.wait(timeout=10)
    except subprocess.TimeoutExpired: pass
    try: os.killpg(child.pid,signal.SIGKILL)
    except ProcessLookupError: pass
    child.wait(timeout=5)


def prepare_test_ops(directory,engine,read_hashes=runtime_hashes):
    """Real tool functions. Inject only test fixtures/lifecycle in local unit tests."""
    from ops_platform.ops import bootstrap_discovery as bootstrap, refresh_discovery as refresh, activate_discovery as activation
    record=validate_run(directory)
    require(ROOT==directory/'project','ACCEPTANCE_WORKER_SOURCE_MISMATCH')
    for rel,wanted in record['source_hashes'].items():
        require(hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()==wanted,'ACCEPTANCE_SOURCE_CHANGED')
    state=directory/'state'; project=record['prefix']+'-ops'; control=directory/'control/control.json'
    engine.protect(record['protected'])
    url='http://127.0.0.1:'+str(free_port())
    prepared=manage.prepare(state,url,directory/'keys/agent',directory/'keys/langfuse.json',compose_project=project)
    require(prepared['result']=='prepared' and len(prepared['managed_fields'])==8,'ACCEPTANCE_PREPARE_FAILED')
    # Bind only loopback in this test. The normal public URL default is not used.
    cfg=manage.existing(state); cfg['bind']='127.0.0.1'
    owner=strict_json((state/'owner.json').read_bytes())
    for name,doc in manage.state_documents(state,cfg,owner['owner'],project).items():
        if name in ('host-config.json','config.json'): write(state/name,doc)
    started=manage.compose(state,'up',build=False)
    require(started.get('first_install_readback')=='passed','ACCEPTANCE_FIRST_READBACK_FAILED')
    docker=activation.Docker(project=project); ident=docker.discover()
    require(ident in engine.owned(directory,record),'ACCEPTANCE_OPS_NOT_OWNED')
    hashes=read_hashes(engine,ident,record)
    initial=change.current(docker,state,manage.existing(state),owner,ident)
    anchor=(state/bootstrap.RELATIVE).read_bytes(); initial_cache=rc.cache(manage.existing(state))
    credentials={p:hashlib.sha256((directory/p).read_bytes()).hexdigest() for p in ('state/access-key','keys/agent','keys/langfuse.json')}
    return dict(directory=directory,record=record,state=state,project=project,control=control,docker=docker,ident=ident,
                owner=owner,hashes=hashes,initial=initial,anchor=anchor,initial_cache=initial_cache,credentials=credentials)


def preserve_test_ops(context,engine):
    from ops_platform.ops import bootstrap_discovery as bootstrap
    c=context; directory=c['directory'];state=c['state']
    require((state/bootstrap.RELATIVE).read_bytes()==c['anchor'],'ACCEPTANCE_BOOTSTRAP_RECORD_CHANGED')
    for p,wanted in c['credentials'].items():
        require(hashlib.sha256((directory/p).read_bytes()).hexdigest()==wanted,'ACCEPTANCE_CREDENTIAL_CHANGED')
    change.cache_guard(manage.existing(state),c['initial_cache'])
    last=change.current(c['docker'],state,manage.existing(state),c['owner'],c['ident'])
    require(c['initial']['identity']==last['identity'],'ACCEPTANCE_OPS_IDENTITY_CHANGED')
    engine.protect(c['record']['protected'])


def exercise(directory,engine,changed_port=change_port,read_hashes=runtime_hashes):
    from ops_platform.ops import refresh_discovery as refresh, activate_discovery as activation
    require(validate_run(directory).get('scenario','discovery')=='discovery','ACCEPTANCE_SCENARIO_INVALID')
    print('[docker-acceptance] first installation',file=sys.stderr,flush=True)
    c=prepare_test_ops(directory,engine,read_hashes)
    record,state,project,control=(c[k] for k in ('record','state','project','control'))
    docker,ident,owner,hashes,initial_cache=(c[k] for k in ('docker','ident','owner','hashes','initial_cache'))
    def phase(name): print('[docker-acceptance] '+name,file=sys.stderr,flush=True); engine.protect(record['protected'])
    def update(**kwargs): write(control,kwargs)
    def config_files(): return {n:(state/n).read_bytes() for n in ('host-config.json','config.json','owner.json','compose.env','access-key')}
    def refresh_call(action='apply',**kw): return refresh.run(action,state,**kw)
    first=refresh_call(); require(not first['restart_requested'] and not first['provenance_journal_written'],'ACCEPTANCE_NOOP_FAILED')
    phase('changed endpoint and metric job')
    changed_port(directory,engine); update(version=1)
    cookie=rc.API(manage.existing(state)).login()
    changed=refresh_call()
    require(changed['changed_fields']==['platforms.model.url','resource_profile'] and changed['restart_requested']
            and changed['same_ops_container_image_mounts'],'ACCEPTANCE_CHANGED_REFRESH_FAILED')
    rc.API(manage.existing(state)).expired(cookie)
    again=refresh_call(); require(not again['restart_requested'] and not again['provenance_journal_written'],'ACCEPTANCE_FOLLOWUP_FAILED')
    phase('discovery failure keeps accepted configuration')
    before=config_files(); history=len(refresh.lineage(state)['records']); before_started=docker.inspect(ident)['started']
    update(version=1,health_fault=True)
    try: refresh_call(); raise ReadFailure('ACCEPTANCE_EXPECTED_DISCOVERY_FAILURE')
    except ReadFailure as exc: require(exc.code=='VERIFIED_DISCOVERY_REQUIRED','ACCEPTANCE_WRONG_DISCOVERY_FAILURE')
    require(before==config_files() and len(refresh.lineage(state)['records'])==history
            and docker.inspect(ident)['started']==before_started,'ACCEPTANCE_FAILURE_MUTATED_STATE')
    update(version=1)
    phase('readback failure and explicit rollback')
    before=config_files(); update(version=2,rules_fault=True)
    try: refresh_call(); raise ReadFailure('ACCEPTANCE_EXPECTED_READBACK_FAILURE')
    except ReadFailure as exc: require(exc.code=='OPS_ALERT_READBACK_FAILED','ACCEPTANCE_WRONG_READBACK_FAILURE')
    require(refresh.lineage(state)['pending'] is not None,'ACCEPTANCE_PENDING_RECORD_MISSING')
    update(version=1)
    recovered=refresh_call('rollback')
    require(recovered['result']=='rolled_back' and before==config_files(),'ACCEPTANCE_ROLLBACK_FAILED')
    activation.readback(manage.existing(state))
    phase('manual override remains user owned')
    cfg=manage.existing(state); cfg['platforms']['model']['url']=model_endpoint(engine,directory,manual=True)
    docs=manage.state_documents(state,cfg,owner['owner'],project)
    manual_configs(state,docs)
    manual=refresh_call()
    require(manual['manual_overrides_detected']==['platforms.model.url'] and manual['restart_requested']
            and 'platforms.model.url' not in manual['managed_fields'],'ACCEPTANCE_OVERRIDE_FAILED')
    final=refresh_call(); require(not final['restart_requested'] and not final['provenance_journal_written'],'ACCEPTANCE_FINAL_NOOP_FAILED')
    require(manage.existing(state)['platforms']['model']['url']==cfg['platforms']['model']['url'],'ACCEPTANCE_OVERRIDE_LOST')
    preserve_test_ops(c,engine)
    phase('completed')
    return {'schema':1,'stage':STAGE,'result':'passed','checked_at':rc.now(),
            'scope':'native_docker_lifecycle_with_synthetic_upstreams','image_id':record['image'],
            'runtime_source_hashes_verified':hashes,'new_install_managed_fields':8,
            'cases':{'first_install_and_readback':'passed','unchanged_refresh':'passed',
                     'endpoint_and_job_change':'passed','old_session_rejected':'passed','unchanged_followup':'passed',
                     'prevalidation_failure_preserves_state':'passed','readback_failure_pending_record':'passed',
                     'explicit_rollback_and_readback':'passed','manual_override_releases_ownership':'passed',
                     'final_noop':'passed'},
            'same_test_ops_container_image_mounts':True,'synthetic_credentials_preserved':True,
            'initial_cache_entries_preserved':len(initial_cache['success_times']),
            'bootstrap_record_preserved':True,'managed_fields_after_manual_override':len(final['managed_fields']),
            'production_credentials_read':False,'production_application_api_requests':0,'inference_requested':False,
            'production_service_control_requested':False,'image_build_or_pull_requested':False,
            'upstreams':'synthetic_GET_only_HTTP','promql_engine_acceptance':False,
            'fresh_business_platform_installation_acceptance':False,'watch_started':False,'kubernetes_acceptance':'deferred'}


def run(base,scenario='discovery'):
    with run_lock(base): return isolated_run(base,scenario)


def isolated_run(base,scenario='discovery'):
    require(scenario in SCENARIOS,'ACCEPTANCE_SCENARIO_INVALID')
    worker_name,stage,timeout=SCENARIOS[scenario]
    if scenario=='service':
        from ops_platform.ops.service_acceptance import native_preflight
        native_preflight()
    engine=Engine(); directory,record=create_run(base,engine,scenario)
    print('[docker-acceptance] evidence: '+str(directory),file=sys.stderr,flush=True)
    result=None; error=None; failure_context={}
    try:
        for role,(service,subtree) in ROLES.items():
            engine.compose(directory,str(Path(subtree)/('acceptance-'+role+'.json')),service)
            if role=='model': engine.compose(directory,str(Path(subtree)/('acceptance-'+role+'.json')),'manual-model')
        env={k:v for k,v in os.environ.items() if not k.startswith(('OPS_','COMPOSE_','PYTHON'))
             and k not in ('DOCKER_CONTEXT','DOCKER_API_VERSION','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}
        env.update(DOCKER_HOST=engine.endpoint,PYTHONDONTWRITEBYTECODE='1')
        worker=directory/'project/ops_platform/ops'/worker_name
        with (directory/'worker.log').open('w') as log:
            (directory/'worker.log').chmod(0o600)
            child=subprocess.Popen([sys.executable,'-B',str(worker),'worker','--run-dir',str(directory)],
                                   env=env,cwd=directory/'project',stdout=log,start_new_session=True)
            try: code=child.wait(timeout=timeout)
            except BaseException:
                stop_worker(child)
                raise
        require((directory/'worker.log').stat().st_size<=1024*1024,'ACCEPTANCE_LOG_LIMIT')
        report=strict_json(rc.read_file(directory/'worker-report.json',128*1024,private=True)[0])
        if scenario=='service':
            from ops_platform.ops.service_acceptance import failure_context as safe_context
            failure_context=safe_context(report.get('failure_context'))
        require(code==0 and report.get('result')=='passed',report.get('code','ACCEPTANCE_WORKER_FAILED'))
        require(report.get('stage')==stage,'ACCEPTANCE_REPORT_STAGE_MISMATCH')
        result=report
    except (ReadFailure,Unavailable,OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired,KeyboardInterrupt) as exc:
        error=exc.code if isinstance(exc,ReadFailure) else 'ACCEPTANCE_INTERRUPTED' if isinstance(exc,KeyboardInterrupt) else 'ACCEPTANCE_EXECUTION_UNAVAILABLE'
        result={'schema':1,'stage':stage,'result':'attention_required','code':error}
        if failure_context: result['failure_context']=failure_context
        if scenario=='service': result.update(test_service_requested=True,production_worker_started=False)
        elif scenario=='watch': result.update(test_watch_requested=True,production_watch_started=False)
        else: result['watch_started']=False
    finally:
        try: cleanup=engine.cleanup(directory)
        except (ReadFailure,Unavailable,OSError,ValueError,KeyError,TypeError) as exc:
            cleanup={'result':'attention_required','code':exc.code if isinstance(exc,ReadFailure) else 'ACCEPTANCE_CLEANUP_UNAVAILABLE'}
            if result is None: result={'schema':1,'stage':stage,'result':'attention_required'}
            result['result']='attention_required'
        result.update(run_directory=str(directory),cleanup=cleanup)
        write(directory/'report.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('action',choices=('run','cleanup','worker'))
    p.add_argument('--state-dir',type=Path,default=BASE);p.add_argument('--run-dir',type=Path)
    args=p.parse_args()
    try:
        if args.action=='run':
            require(args.run_dir is None,'ACCEPTANCE_RUN_DIR_NOT_ALLOWED'); report=run(args.state_dir)
        else:
            require(args.run_dir is not None,'ACCEPTANCE_RUN_DIR_REQUIRED')
            directory=manage.safe(args.run_dir);record=validate_run(directory);engine=Engine(record['endpoint'])
            if args.action=='cleanup':
                with run_lock(directory.parent):
                    report={'stage':STAGE,'result':'cleaned','cleanup':engine.cleanup(directory),'run_directory':str(directory)}
            else:
                try: report=exercise(directory,engine)
                except (ReadFailure,Unavailable,OSError,ValueError,KeyError,TypeError) as exc:
                    report={'stage':STAGE,'result':'attention_required','code':exc.code if isinstance(exc,ReadFailure) else 'ACCEPTANCE_WORKER_UNAVAILABLE'}
                write(directory/'worker-report.json',report)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0 if report['result'] in ('passed','cleaned') else 2
    except (ReadFailure,Unavailable,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'stage':STAGE,'result':'attention_required','code':exc.code if isinstance(exc,ReadFailure) else 'ACCEPTANCE_PREFLIGHT_UNAVAILABLE'}))
        return 2


if __name__=='__main__':
    def interrupted(signum,frame): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupted)
    raise SystemExit(main())
