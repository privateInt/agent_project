"""Stdlib-only constrained UID materializer for one fresh /restore bind.

Init touches only an empty reviewed directory. Extract/verify run as its UID,
without capabilities/network. No key, source mount, image load or app start.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import signal
import sys
import tarfile
import time

KIND='leased-stopped-files-age-v1'

def require(ok,code):
    if not ok: raise RuntimeError(code)

def identity(path):
    s=path.lstat(); require(stat.S_ISDIR(s.st_mode),'UID_ROOT_TYPE')
    return [s.st_dev,s.st_ino,s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode)]

def safe(name):
    require(isinstance(name,str) and name and len(name.encode())<=1024 and len(name.split('/'))<=64
            and not name.startswith('/') and all(x not in ('','.','..') for x in name.split('/'))
            and not any(ord(x)<32 or ord(x)==127 for x in name),'UID_PATH_REFUSED')
    return name

def document(raw):
    def pairs(items):
        value={}
        for key,item in items:
            require(key not in value,'UID_DUPLICATE_JSON');value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=pairs)

class BoundedInfo(tarfile.TarInfo):
    def _proc_member(self,archive):
        require(self.type!=tarfile.GNUTYPE_SPARSE,'UID_SPARSE_REFUSED')
        if self.type in (tarfile.XHDTYPE,tarfile.XGLTYPE,tarfile.SOLARIS_XHDTYPE,tarfile.GNUTYPE_LONGNAME,tarfile.GNUTYPE_LONGLINK):
            require(0<=self.size<=65536,'UID_EXTENSION_LIMIT')
        return super()._proc_member(archive)

def run(config,mode,root=Path('/restore'),stream=None):
    require(mode in ('init','extract','verify') and set(config)=={'schema','role','uid','gid','identity','seconds','detail'},'UID_CONFIG')
    require(config['schema']==1 and isinstance(config['role'],str) and re.fullmatch('[a-z][a-z0-9_-]{0,47}',config['role'])
            and all(type(config[x]) is int and 0<=config[x]<2**31 for x in ('uid','gid'))
            and type(config['seconds']) is int and 1<=config['seconds']<=600,'UID_CONFIG')
    until=time.monotonic()+config['seconds']; role=config['role']; detail=config['detail']
    require(not root.is_symlink() and identity(root)==config['identity'],'UID_ROOT_CHANGED')
    def guard():
        require(time.monotonic()<until,'UID_DEADLINE');require(identity(root)==config['identity'],'UID_ROOT_CHANGED')
    if mode=='init':
        require(os.getuid()==0 and config['identity'][4]==0o700 and not os.listdir(root),'UID_INITIALIZER_NOT_EMPTY')
        os.chown(root,config['uid'],config['gid'],follow_symlinks=False)
        result=identity(root);require(result==[*config['identity'][:2],config['uid'],config['gid'],0o700],'UID_MAPPING_CHANGED')
        return {'schema':1,'mode':'init','identity':result}
    require(os.getuid()==config['uid'] and os.getgid()==config['gid'] and config['identity'][2:]==[config['uid'],config['gid'],0o700],
            'UID_ACTOR_OR_ROOT_MISMATCH')
    require(isinstance(detail,dict) and detail.get('schema')==1 and detail.get('kind')==KIND
            and type(detail.get('entry_count')) is int and 0<detail['entry_count']<=4096
            and type(detail.get('logical_bytes')) is int and 0<=detail['logical_bytes']<=2**32
            and re.fullmatch('[0-9a-f]{64}',detail.get('manifest_sha256','')),'UID_RECEIPT_INVALID')
    prefix='data/'+role; seen={}; total=0; manifest=None
    def relative(name):
        name=safe(name); require(name==prefix or name.startswith(prefix+'/'),'UID_ROLE_ESCAPE')
        return name[len(prefix):].lstrip('/')
    def entry(row):
        require(isinstance(row,dict) and set(row)=={'path','type','size','uid','gid','mode','mtime_ns'}|
                ({'sha256'} if row.get('type')=='file' else set()),'UID_MANIFEST_FIELDS')
        name=safe(row['path']); relative(name)
        require(row['type'] in ('directory','file') and all(type(row[x]) is int for x in ('uid','gid','mode','size','mtime_ns'))
                and row['uid']==config['uid'] and row['gid']==config['gid'] and 0<=row['size']<=detail['logical_bytes']
                and 0<=row['mode']<=0o777 and 0<=row['mtime_ns']<2**63,'UID_ENTRY_METADATA')
        # Read-only verification as the same UID must remain possible. Refuse
        # unsupported unreadable trees; never grant permissions to old sources.
        require((row['mode']&0o700)==0o700 if row['type']=='directory' else bool(row['mode']&0o400),'UID_UNREADABLE_METADATA')
        if name==prefix: require(row['type']=='directory' and row['mode']==0o700,'UID_ROOT_METADATA')
        if row['type']=='file':require(re.fullmatch('[0-9a-f]{64}',row.get('sha256','')),'UID_HASH_INVALID')
    if mode=='extract':
        require(not os.listdir(root),'UID_DESTINATION_NOT_EMPTY')
        with tarfile.open(fileobj=stream or sys.stdin.buffer,mode='r|',tarinfo=BoundedInfo) as archive:
            for member in archive:
                guard();name=safe(member.name.rstrip('/') if member.isdir() else member.name)
                require(manifest is None and (member.isfile() or member.isdir()) and not member.linkname and not member.issparse()
                        and (not member.pax_headers or set(member.pax_headers)<={'path','mtime','size','uid','gid'}),'UID_TAR_MEMBER')
                if name=='manifest.json':
                    require(member.isfile() and 0<member.size<=2**20,'UID_MANIFEST_LIMIT')
                    raw=archive.extractfile(member).read(2**20+1)
                    require(hashlib.sha256(raw).hexdigest()==detail['manifest_sha256'],'UID_MANIFEST_DIGEST')
                    manifest=document(raw);continue
                rel=relative(name);require(name not in seen and len(seen)<detail['entry_count'],'UID_DUPLICATE_OR_ENTRY_LIMIT')
                require(member.uid==config['uid'] and member.gid==config['gid'] and 0<=member.mode<=0o777
                        and 0<=member.size<=detail['logical_bytes']-total,'UID_TAR_METADATA')
                dest=root/rel
                row={'path':name,'type':'directory' if member.isdir() else 'file','size':member.size,
                     'uid':member.uid,'gid':member.gid,'mode':member.mode}
                if rel:
                    parent='/'.join(name.split('/')[:-1]);require(parent in seen and seen[parent]['type']=='directory','UID_PARENT_UNDECLARED')
                    require(not dest.exists() and not dest.is_symlink(),'UID_COLLISION')
                else:require(member.isdir() and member.size==0 and member.mode==0o700,'UID_ROOT_METADATA')
                if member.isdir():
                    require(member.size==0,'UID_DIRECTORY_SIZE')
                    if rel: dest.mkdir(mode=0o700)
                else:
                    total+=member.size;h=hashlib.sha256();payload=archive.extractfile(member);remaining=member.size
                    fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    with os.fdopen(fd,'wb') as out:
                        while remaining:
                            guard();data=payload.read(min(65536,remaining));require(data,'UID_TRUNCATED');remaining-=len(data);h.update(data);out.write(data)
                        out.flush();os.fsync(out.fileno())
                    row['sha256']=h.hexdigest()
                seen[name]=row
            while True:
                guard();data=archive.fileobj.read(65536)
                if not data:break
                require(not any(data),'UID_TRAILING_DATA')
    else:
        raw=bytearray();stream=stream or sys.stdin.buffer
        while True:
            guard();data=stream.read(65536)
            if not data:break
            raw.extend(data);require(len(raw)<=2**20,'UID_MANIFEST_LIMIT')
        require(hashlib.sha256(raw).hexdigest()==detail['manifest_sha256'],'UID_MANIFEST_DIGEST');manifest=document(raw)
    require(isinstance(manifest,dict) and manifest.get('schema')==1 and manifest.get('kind')==KIND
            and manifest.get('id')==detail['id'] and isinstance(manifest.get('entries'),list)
            and len(manifest['entries'])==detail['entry_count'],'UID_MANIFEST_BINDING')
    rows=manifest['entries'];by={}
    for row in rows:
        entry(row);require(row['path'] not in by,'UID_DUPLICATE_MANIFEST');by[row['path']]=row
    require(prefix in by,'UID_ROOT_MISSING')
    if mode=='extract':
        require(set(by)==set(seen) and total==detail['logical_bytes'],'UID_SET_MISMATCH')
        for name,row in by.items():require({k:v for k,v in row.items() if k!='mtime_ns'}==seen[name],'UID_PAYLOAD_MISMATCH')
        for row in sorted(rows,key=lambda x:len(x['path'].split('/')),reverse=True):
            guard();dest=root/relative(row['path']);fd=os.open(dest,os.O_RDONLY|os.O_NOFOLLOW)
            try:os.fchmod(fd,row['mode']);os.utime(fd,ns=(row['mtime_ns'],row['mtime_ns']));os.fsync(fd)
            finally:os.close(fd)
    actual={prefix};measured=0
    for folder,dirs,files in os.walk(root,followlinks=False):
        guard()
        for name in dirs+files:
            path=Path(folder)/name;info=path.lstat();require((stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    and info.st_dev==config['identity'][0] and (stat.S_ISDIR(info.st_mode) or info.st_nlink==1),'UID_RESTORED_TYPE')
            actual.add(prefix+'/'+path.relative_to(root).as_posix())
    require(actual==set(by),'UID_RESTORED_SET')
    for name,row in by.items():
        guard();path=root/relative(name);s=path.stat(follow_symlinks=False)
        require(s.st_uid==row['uid'] and s.st_gid==row['gid'] and stat.S_IMODE(s.st_mode)==row['mode']
                and s.st_mtime_ns==row['mtime_ns'] and (stat.S_ISDIR(s.st_mode) if row['type']=='directory' else stat.S_ISREG(s.st_mode)),
                'UID_RESTORED_METADATA')
        if row['type']=='file':
            digest=hashlib.sha256();fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                while True:
                    guard();data=os.read(fd,65536)
                    if not data:break
                    measured+=len(data);require(measured<=detail['logical_bytes'],'UID_RESTORED_BYTE_LIMIT');digest.update(data)
            finally:os.close(fd)
            require(s.st_size==row['size'] and digest.hexdigest()==row['sha256'],'UID_RESTORED_HASH')
    require(measured==detail['logical_bytes'],'UID_RESTORED_BYTES')
    return {'schema':1,'mode':mode,'identity':identity(root),'manifest':manifest,'manifest_sha256':detail['manifest_sha256'],
            'entries':len(rows),'logical_bytes':measured,'filesystem_metadata_and_hashes_verified':True}

if __name__=='__main__':
    try:
        cfg=document(sys.argv[2]);require(type(cfg.get('seconds')) is int and 1<=cfg['seconds']<=600,'UID_CONFIG')
        def alarm(*args):raise RuntimeError('UID_DEADLINE')
        signal.signal(signal.SIGALRM,alarm);signal.alarm(cfg['seconds'])
        print(json.dumps(run(cfg,sys.argv[1]),sort_keys=True))
    except BaseException:
        print('UID_MATERIALIZER_FAILED',file=sys.stderr);raise SystemExit(2)

    finally: signal.alarm(0)
