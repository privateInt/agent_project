"""Disposable foreign-UID storage fixtures; never a production provider."""
from copy import deepcopy
import json
import os
from pathlib import Path
import pwd
import secrets
import stat

from src.ops.platform.contracts import require
from src.ops.candidates.cold_storage_20260930 import storage

OWNER_LABEL = 'org.agent-project.cold-fixture.owner'
REL = 'src/infra/langfuse/acceptance-langfuse.json'
ROLES = ('cold-client', 'cold-db')
SETUP = 'cold-setup'
PAYLOAD = b'isolated-cold-storage\x00\xff\n' * 2048
LOOP = 'import signal,sys,time\nsignal.signal(signal.SIGTERM,lambda *_:sys.exit(0))\nwhile True:time.sleep(.1)\n'
SETUP_CODE = '''import json,os,signal,sys,time
p=json.loads(sys.argv[1]);s=os.stat('/data',follow_symlinks=False)
assert [s.st_dev,s.st_ino,s.st_uid,s.st_gid,s.st_mode&0o7777]==p['identity']
assert not os.listdir('/data') and os.getuid()==0
fd=os.open('/data/payload.bin',os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'wb') as f:f.write(b'isolated-cold-storage\\x00\\xff\\n'*2048);f.flush();os.fsync(f.fileno())
os.chown('/data/payload.bin',p['uid'],p['gid'],follow_symlinks=False)
os.chown('/data',p['uid'],p['gid'],follow_symlinks=False)
signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
while True:time.sleep(.1)
'''


def choose_uid():
    for _ in range(64):
        candidate = 10000 + secrets.randbelow(590000)
        if candidate == os.getuid(): continue
        try: pwd.getpwuid(candidate)
        except KeyError: return candidate
    raise RuntimeError('COLD_FIXTURE_UID_UNAVAILABLE')


def identity(path):
    s=path.lstat()
    require(stat.S_ISDIR(s.st_mode) and not path.is_symlink(), 'COLD_FIXTURE_SOURCE_INVALID')
    return [s.st_dev,s.st_ino,s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode)]


def configs(record, fixture):
    source=fixture['source']; labels={OWNER_LABEL:record['owner'], 'org.agent-project.ops.acceptance':record['owner']}
    base={'image':record['image'],'pull_policy':'never','network_mode':'none','read_only':True,
          'restart':'no','logging':{'driver':'none'},'security_opt':['no-new-privileges:true'],
          'cap_drop':['ALL'],'healthcheck':{'disable':True},'entrypoint':['python3'],
          'labels':labels,'pids_limit':32,'mem_limit':'256m','cpus':1,'ipc':'none'}
    mount={'type':'bind','source':source,'target':'/data','bind':{'create_host_path':False}}
    result={}
    result[ROLES[0]]={**deepcopy(base),'user':f"{record['uid']}:{record['gid']}",'command':['-I','-B','-c',LOOP]}
    result[ROLES[1]]={**deepcopy(base),'user':f"{fixture['uid']}:{fixture['gid']}",'volumes':[mount],
                     'command':['-I','-B','-c',LOOP]}
    result[SETUP]={**deepcopy(base),'user':'0:0','cap_add':['CHOWN','DAC_OVERRIDE','FOWNER'],'volumes':[mount],
        'command':['-I','-B','-c',SETUP_CODE,json.dumps({k:fixture[k] for k in ('identity','uid','gid')},sort_keys=True)]}
    return result


def discover(directory, record, engine, backend):
    expected={role:[] for role in ROLES}
    for ident in engine.owned(directory,record):
        meta=engine.metadata(ident);r=meta['identity'];role=r['service']
        if role not in expected:continue
        require(r['project']==record['prefix']+'-langfuse' and meta['files']==str(directory/'project'/REL)
                and r['image']==record['image'], 'COLD_FIXTURE_IDENTITY_CHANGED')
        seed={'container_id':ident,'owner_label':OWNER_LABEL}
        row=backend.inspect(seed)
        require(row['owner']==record['owner'] and row['project']==r['project'] and row['service']==role,
                'COLD_FIXTURE_OWNER_CHANGED')
        storage.validate_row(role,row)
        expected[role].append(row)
    require(all(len(v)==1 for v in expected.values()),'COLD_FIXTURE_SCOPE_NOT_UNIQUE')
    return {k:v[0] for k,v in expected.items()}
