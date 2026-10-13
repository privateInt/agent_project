"""Migrate A07 n8n ownership and start infra without replacing persistent data."""
import json
import os
from pathlib import Path
from common import ROOT, PROJECT, compose, config, containers, run
from configure import OLD_VOLUMES, NEW_VOLUMES, configure
from preflight import verify

OWNER_LABEL = 'org.agent-project.n8n.config-id'


def volume_inventory():
    return set(run(['docker','volume','ls','--format','{{.Name}}']).stdout.splitlines())


def validate_container(c, state, legacy_root):
    labels = c.get('Config', {}).get('Labels', {})
    project, service = labels.get('com.docker.compose.project'), labels.get('com.docker.compose.service')
    if project not in (PROJECT, 'agent-flows') or service not in ('n8n','flows-postgres'):
        raise ValueError('An unrelated container uses an n8n volume; no services were stopped')
    expected = state['volumes']['flows-db' if service == 'flows-postgres' else 'flows-data']
    target = '/var/lib/postgresql/data' if service == 'flows-postgres' else '/home/node/.n8n'
    if not any(m.get('Type') == 'volume' and m.get('Name') == expected and m.get('Destination') == target for m in c.get('Mounts', [])):
        raise ValueError('n8n container volume mismatch')
    if project == 'agent-flows':
        if state['origin'] != 'a07' or Path(labels.get('com.docker.compose.project.working_dir', '')) != legacy_root:
            raise ValueError('Unexpected legacy n8n project location')
        names = ['db-admin','db-app'] if service == 'flows-postgres' else ['db-app','encryption-key']
        mounts = {m.get('Destination'): m.get('Source') for m in c.get('Mounts', []) if m.get('Type') == 'bind'}
        mapping = {'db-admin':'flows-db-admin','db-app':'flows-db-app','encryption-key':'flows-encryption'}
        for name in names:
            source = Path(mounts.get('/run/secrets/'+mapping[name], '/not-a-secret'))
            if source != legacy_root/'.secrets'/name or not source.is_file():
                raise ValueError('Legacy credential mount mismatch; no containers were stopped')
            if source.read_bytes() != (ROOT/'.secrets'/name).read_bytes():
                raise ValueError('Legacy and infra credentials differ; refusing migration')
    return project, service


def storage_plan(state, legacy_root):
    existing = volume_inventory()
    if state['origin'] == 'new' and existing.intersection(OLD_VOLUMES.values()):
        raise ValueError('A07 storage still exists; restore/import its original settings before starting n8n')
    marker = ROOT/'.storage.json'
    if marker.is_symlink(): raise ValueError('Unsafe storage marker')
    provisioned = marker.is_file()
    if provisioned and json.loads(marker.read_text()) != {'config_id':state['config_id'],'volumes':state['volumes']}:
        raise ValueError('Storage ownership marker mismatch')
    pending = []
    seen = {}
    for key, name in state['volumes'].items():
        if name not in existing:
            if state['origin'] == 'a07' or provisioned: raise ValueError('Missing original n8n volume '+name+'; refusing to create an empty replacement')
            pending.append(name)
            continue
        volume = json.loads(run(['docker','volume','inspect',name]).stdout)[0]
        labels = volume.get('Labels') or {}
        if state['origin'] == 'a07':
            if labels.get('com.docker.compose.project') != 'agent-flows' or labels.get('com.docker.compose.volume') != key:
                raise ValueError('Legacy volume ownership mismatch')
        elif labels.get(OWNER_LABEL) != state['config_id']:
            raise ValueError('Existing volume belongs to another n8n setup; refusing new keys')
        for c in containers(['volume='+name]): seen[c['Id']] = c
    for c in containers(['label=com.docker.compose.project=agent-flows']):
        service = c.get('Config',{}).get('Labels',{}).get('com.docker.compose.service')
        if service in ('n8n','flows-postgres'): seen[c['Id']] = c
    old = []
    for c in seen.values():
        project, service = validate_container(c, state, legacy_root)
        if project == 'agent-flows': old.append(c)
    return pending, sorted(old, key=lambda c: c['Config']['Labels']['com.docker.compose.service'] != 'n8n')


def save_handoff(old):
    path = ROOT/'.handoff.json'
    if path.is_symlink(): raise ValueError('Unsafe handoff path')
    if path.exists(): return
    records = [{'id':c['Id'], 'name':c['Name'], 'restart_policy':c['HostConfig'].get('RestartPolicy'),
                'was_running':c.get('State',{}).get('Running',False)} for c in old]
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f: json.dump({'previous_project':'agent-flows','containers':records},f,indent=2)


def recover_unhealthy_n8n():
    # A07 can remain stuck during initialization after the permission error.
    # Healthy n8n is left running; stop an unhealthy instance so up starts it again.
    current = containers(['label=com.docker.compose.project='+PROJECT,
                          'label=com.docker.compose.service=n8n'])
    if any(c.get('State',{}).get('Running') and
           c.get('State',{}).get('Health',{}).get('Status') == 'unhealthy' for c in current):
        compose(['stop','--timeout','30','n8n'], timeout=45, stream=True)


def start(*, n8n_only=False):
    legacy = ROOT.parents[1]/'agent_platform/n8n'
    existing = volume_inventory()  # Check Docker before creating settings.
    has_config = (ROOT/'.env').exists() or (legacy/'.env').exists()
    if not has_config and existing.intersection(set(OLD_VOLUMES.values()) | set(NEW_VOLUMES.values())):
        raise ValueError('Existing n8n storage found without its settings/keys; restore the original settings first')
    state = configure(legacy=legacy)
    own = config(); verify(own)
    if not n8n_only:
        whole = config(whole=True)
        for section, names in [('services',('n8n','flows-postgres')),('volumes',('flows-db','flows-data')),('secrets',tuple(own['secrets']))]:
            for name in names:
                if whole[section][name] != own[section][name]:
                    raise ValueError('Root infra configuration changes n8n '+section+': '+name)
    pending, old = storage_plan(state, legacy)
    print('n8n plan: owner=infrastructure; origin='+state['origin']+'; volumes='+','.join(state['volumes'].values()), flush=True)
    # Download before stopping the legacy services, so network failures do not interrupt them.
    compose(['pull','n8n','flows-postgres'], timeout=900, stream=True)
    network = own['networks']['flows-api']['name']
    networks = run(['docker','network','ls','--format','{{.Name}}']).stdout.splitlines()
    if network not in networks: run(['docker','network','create',network])
    # Revalidate immediately before touching running services or creating storage.
    pending, old = storage_plan(state, legacy)
    for name in pending:
        run(['docker','volume','create','--label',OWNER_LABEL+'='+state['config_id'],name])
    marker = ROOT/'.storage.json'
    if not marker.exists():
        fd = os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f: json.dump({'config_id':state['config_id'],'volumes':state['volumes']},f)
    if old:
        save_handoff(old)
        for c in old:
            run(['docker','update','--restart=no',c['Id']])
            run(['docker','stop','--time','30',c['Id']], timeout=45)
        for c in old:
            now = json.loads(run(['docker','inspect',c['Id']]).stdout)[0]
            if now['State'].get('Running') or now['HostConfig']['RestartPolicy']['Name'] != 'no':
                raise ValueError('Legacy n8n did not stop; refusing a second writer')
    compose(['up','-d','--wait','--wait-timeout','180','flows-postgres'], timeout=210, stream=True)
    # Existing volumes skip Docker's initialization scripts. Repair only this DB's privilege.
    compose(['exec','-T','flows-postgres','psql','-X','--set','ON_ERROR_STOP=1',
             '--username','flows_admin','--dbname','flows','--file','/opt/n8n/repair-privileges.sql'],
            timeout=60, stream=True)
    # Authenticate as the application role and check the exact n8n bootstrap statement.
    compose(['exec','-T','flows-postgres','sh','/opt/n8n/check-db.sh'], timeout=60, stream=True)
    recover_unhealthy_n8n()
    args = ['up','-d','--wait','--wait-timeout','360']
    if n8n_only: args += ['n8n','flows-postgres']
    else: args += ['--build']
    compose(args, whole=not n8n_only, timeout=900, stream=True)
    print('PASS: infra n8n ready; original volumes/keys preserved; no Agent rebuild required.', flush=True)
