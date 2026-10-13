#!/usr/bin/env python3
"""Prepare and run only the independent O03 Ops web service on a Linux Docker host."""
import argparse
import json
import os
import re
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import urllib.parse
import uuid

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parents[2]))
from src.infra.observability.ops.backup_preflight import Unavailable
from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.observations import read_lf_credentials
from src.ops.platform.settings import load
from src.ops.platform.transport import secret, base_url

STATE=Path.home()/'.local/state/agent-project-ops/server'
DEFAULT_PROJECT='agent-project-ops'


def project_name(value):
    require(isinstance(value,str) and re.fullmatch('[a-z0-9][a-z0-9_-]{0,62}',value),'INVALID_COMPOSE_PROJECT')
    return value


def prepared_project(directory):
    owner=json.loads(private(directory/'owner.json').read_text())
    return project_name(owner.get('compose_project',DEFAULT_PROJECT))


def safe(path):
    path=Path(path).expanduser().absolute()
    require(not any(p.is_symlink() for p in (path,*path.parents)),'SYMLINK_REFUSED')
    require(not any(c in str(path) for c in "\r\n\x00$'\\"),'INVALID_PATH')
    return path


def private(path):
    path=safe(path);s=path.stat()
    require(s.st_uid==os.getuid() and not s.st_mode&0o077,'PRIVATE_STATE_REQUIRED')
    return path


def write_new(path,value):
    raw=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:f.write(raw);f.flush();os.fsync(f.fileno())


def state_documents(directory, cfg, owner, compose_project=None):
    """Build both runtime views from one configuration; paths reference final state."""
    cfg=json.loads(json.dumps(cfg))
    cfg.update(access_file=str(directory/'access-key'),state_file=str(directory/'cache/api-success.json'))
    container=json.loads(json.dumps(cfg))
    container.update(langfuse_file='/run/ops/langfuse.json',access_file='/run/ops/access-key',state_file='/var/lib/ops/api-success.json')
    container['platforms']['agent']['token_file']='/run/ops/agent-token'
    env={'OPS_UID':str(os.getuid()),'OPS_GID':str(os.getgid()),'OPS_OWNER':owner,
         'OPS_CONFIG_FILE':str(directory/'config.json'),'OPS_ACCESS_FILE':str(directory/'access-key'),
         'OPS_AGENT_TOKEN_FILE':cfg['platforms']['agent']['token_file'],
         'OPS_LANGFUSE_FILE':cfg['langfuse_file'],'OPS_CACHE_DIR':str(directory/'cache')}
    identity={'schema':1,'owner':owner,'uid':os.getuid(),'gid':os.getgid(),'project_root':str(ROOT.parents[2])}
    if compose_project is not None: identity['compose_project']=project_name(compose_project)
    return {'host-config.json':cfg,'config.json':container,
        'owner.json':identity,
        'compose.env':''.join(k+"='"+v+"'\n" for k,v in env.items())}


def prepare(directory,url=None,agent_token=None,lf_file=None,compose_project=None):
    directory=safe(directory)
    if compose_project is not None: project_name(compose_project)
    if directory.exists():
        config=existing(directory)
        require(compose_project is None or prepared_project(directory)==compose_project,'EXISTING_COMPOSE_PROJECT_DIFFERS')
        require(url is None or config['public_url']==url.rstrip('/'),'EXISTING_PUBLIC_URL_DIFFERS')
        require(agent_token is None or str(safe(agent_token))==config['platforms']['agent']['token_file'],'EXISTING_AGENT_KEY_DIFFERS')
        require(lf_file is None or str(safe(lf_file))==config['langfuse_file'],'EXISTING_LANGFUSE_KEY_DIFFERS')
        return {'result':'reused','url':config['public_url'],'services_started':False}
    # Import only in the new-install branch; reuse never probes Docker or upstreams.
    from src.ops.platform.ops import bootstrap_discovery as bootstrap
    url=url or input('운영 화면 접속 주소 [Enter: http://localhost:17100; LAN은 서버 IP 입력]: ').strip() or 'http://localhost:17100'
    u=base_url(url);require(u.scheme=='http','PREPARE_HTTP_ONLY_USE_TLS_GUIDE')
    port=u.port or 80;require(1024<=port<=65535,'INVALID_PORT')
    agent_token=safe(agent_token or ROOT.parents[2]/'src/platform/agent/.secrets/api-token')
    lf_file=safe(lf_file or ROOT.parents[2]/'src/platform/agent/.secrets/langfuse-tracing.json')
    secret(agent_token);read_lf_credentials(lf_file)
    discovered=bootstrap.propose(ROOT.parents[2])
    cfg=json.loads(json.dumps(discovered['configuration']))
    cfg.update(schema=1,langfuse_file=str(lf_file),public_url=url.rstrip('/'),
               bind='0.0.0.0',port=port,tls_cert=None,tls_key=None)
    cfg['platforms']['agent']['token_file']=str(agent_token)
    bootstrap.publish(directory,cfg,discovered,compose_project=compose_project)
    return {'result':'prepared','url':url.rstrip('/'),'services_started':False,
            'discovery_provider':'docker-compose','managed_fields':sorted(discovered['fields']),
            'provenance_recorded':True,'runtime_acceptance':'pending_first_start_and_readback',
            'credential_scope':'new Ops login; existing upstream keys retained, GET-only client, keys are not read-only roles'}


def existing(directory):
    private(directory)
    owner=json.loads(private(directory/'owner.json').read_text())
    project_name(owner.get('compose_project',DEFAULT_PROJECT))
    require(owner.get('uid')==os.getuid() and owner.get('gid')==os.getgid() and owner.get('project_root')==str(ROOT.parents[2]),'OWNER_MISMATCH')
    require(str(uuid.UUID(owner['owner']))==owner['owner'],'OWNER_MISMATCH')
    cfg=load(private(directory/'host-config.json'))
    private(directory/'access-key');private(directory/'config.json');private(directory/'compose.env')
    private(directory/'cache')
    # Explicit config consistency: do not recreate a container using a silently divergent config.
    container=json.loads((directory/'config.json').read_text());expected=json.loads(json.dumps(cfg))
    expected.update(langfuse_file='/run/ops/langfuse.json',access_file='/run/ops/access-key',state_file='/var/lib/ops/api-success.json')
    expected['platforms']['agent']['token_file']='/run/ops/agent-token'
    require(container==expected,'HOST_CONTAINER_CONFIG_MISMATCH')
    env={'OPS_UID':str(os.getuid()),'OPS_GID':str(os.getgid()),'OPS_OWNER':owner['owner'],
         'OPS_CONFIG_FILE':str(directory/'config.json'),'OPS_ACCESS_FILE':str(directory/'access-key'),
         'OPS_AGENT_TOKEN_FILE':cfg['platforms']['agent']['token_file'],'OPS_LANGFUSE_FILE':cfg['langfuse_file'],
         'OPS_CACHE_DIR':str(directory/'cache')}
    require((directory/'compose.env').read_text()==''.join(k+"='"+v+"'\n" for k,v in env.items()),'COMPOSE_ENV_MISMATCH')
    require(cfg['access_file']==str(directory/'access-key') and cfg['state_file']==str(directory/'cache/api-success.json'),'STATE_PATH_MISMATCH')
    return cfg


def docker(args,capture=False):
    env={k:v for k,v in os.environ.items() if not k.startswith(('COMPOSE_','OPS_'))}
    require(not env.get('DOCKER_HOST') or env['DOCKER_HOST'].startswith('unix://'),'LOCAL_DOCKER_REQUIRED')
    result=subprocess.run(['docker',*args],env=env,cwd=ROOT,text=True,capture_output=capture)
    require(result.returncode==0,'DOCKER_COMMAND_FAILED')
    return result.stdout if capture else ''


def compose(directory,action,build=True):
    cfg=existing(directory)
    bootstrap_state=(directory/'discovery-bootstrap').exists() or (directory/'discovery-bootstrap').is_symlink()
    if action=='up' and bootstrap_state:
        from src.ops.platform.ops import bootstrap_discovery as bootstrap
        bootstrap.load_anchor(directory)
    endpoint=docker(['context','inspect','--format','{{.Endpoints.docker.Host}}'],True).strip()
    require(endpoint.startswith('unix://'),'LOCAL_DOCKER_REQUIRED')
    args=['compose','--env-file',str(directory/'compose.env'),'-f',str(ROOT/'docker-compose.yml'),'-p',prepared_project(directory)]
    owner=json.loads((directory/'owner.json').read_text())['owner']
    ids=docker(args+['ps','-aq','ops-web'],True).split()
    for ident in ids:
        existing_owner=docker(['inspect','--format','{{index .Config.Labels "org.agent-project.ops.owner"}}',ident],True).strip()
        require(existing_owner==owner,'CONTAINER_OWNER_MISMATCH')
    if action=='up':
        if not ids:
            with socket.socket() as sock:
                try:sock.bind((cfg['bind'],cfg['port']))
                except OSError:raise ReadFailure('OPS_PORT_IN_USE') from None
        docker(args+['up','--build' if build else '--no-build','-d','--no-deps','--wait','--wait-timeout','60','ops-web'])
    elif action=='stop':docker(args+['stop','ops-web'])
    else:docker(args+['ps','ops-web'])
    result={'result':action,'scope':'ops-web only','url':cfg['public_url'],'business_mutations_requested':False}
    if action=='up' and bootstrap_state:
        from src.ops.platform.ops import activate_discovery as activation
        result['ops_readback']=activation.readback(cfg)
        result['first_install_readback']='passed'
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','up','status','stop','credentials'))
    p.add_argument('--state-dir',type=Path,default=STATE)
    p.add_argument('--public-url');p.add_argument('--agent-token',type=Path);p.add_argument('--langfuse-file',type=Path)
    p.add_argument('--compose-project');p.add_argument('--no-build',action='store_true')
    a=p.parse_args();directory=safe(a.state_dir)
    require(a.action=='prepare' or not any((a.public_url,a.agent_token,a.langfuse_file,a.compose_project)),'PREPARE_OPTIONS_ONLY')
    require(not a.no_build or a.action=='up','NO_BUILD_UP_ONLY')
    if a.action=='prepare':result=prepare(directory,a.public_url,a.agent_token,a.langfuse_file,a.compose_project)
    elif a.action=='credentials':
        cfg=existing(directory)
        print('접속 주소: '+cfg['public_url'])
        print('운영 화면 접속 코드: '+secret(cfg['access_file']))
        return
    else:result=compose(directory,a.action,build=not a.no_build)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except (ReadFailure,Unavailable,OSError,ValueError,KeyError,TypeError) as exc:
        result={'result':'error','code':exc.code if isinstance(exc,ReadFailure) else str(exc) if isinstance(exc,Unavailable) else 'LOCAL_CONFIG_OR_COMMAND_ERROR'}
        if getattr(exc,'details',None):result['details']=exc.details
        print(json.dumps(result))
        raise SystemExit(1)
