"""Inactive leased file-set capture and authenticated isolated file restore.

No production CLI or service/Docker control. A trusted quiescence provider must
prove the declared scope stopped. The caller restores services and explicitly
returns the worker separately. No plaintext capture archive is written.
"""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import time
import uuid

from .age_tool import Age, Pipe, Failure, require, private, recipients, sha

KIND='leased-stopped-files-age-v1'
MAX_ENTRIES=65536
MAX_MANIFEST=16*1024*1024
ROLE=re.compile('[a-z][a-z0-9_-]{0,47}')

def fingerprint(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def identity(s):return [s.st_dev,s.st_ino,s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode)]
def signature(s):return [*identity(s),s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns]

def safe_name(value):
 require(isinstance(value,str) and value and len(value.encode('utf-8'))<=1024
  and not any(ord(c)<32 or ord(c)==127 for c in value),'CAPTURE_NAME_INVALID')
 p=PurePosixPath(value)
 require(not p.is_absolute() and len(p.parts)<=64 and all(x not in ('.','..') for x in value.split('/')),
         'CAPTURE_NAME_INVALID')
 require(str(p)==value,'CAPTURE_NAME_INVALID');return value

def source(path):
 path=Path(path);require(path.is_absolute(),'CAPTURE_ABSOLUTE_PATH_REQUIRED')
 for p in (*reversed(path.parents),path):require(not p.is_symlink(),'CAPTURE_SYMLINK_REFUSED')
 s=path.lstat();require(stat.S_ISDIR(s.st_mode) and not s.st_mode&0o7000,'CAPTURE_SOURCE_INVALID')
 return path

def plan(roots,destination,recipient_values,quiescence_identity,*,seconds=60,max_bytes=1024*1024*1024,max_entries=4096):
 require(type(seconds) is int and 1<=seconds<=3600 and type(max_bytes) is int and 1<=max_bytes<=2**40
   and type(max_entries) is int and 1<=max_entries<=MAX_ENTRIES,'CAPTURE_BUDGET_INVALID')
 require(isinstance(roots,dict) and 1<=len(roots)<=32 and all(isinstance(x,str) and ROLE.fullmatch(x) for x in roots),'CAPTURE_ROOTS_INVALID')
 require(isinstance(quiescence_identity,str) and re.fullmatch('[0-9a-f]{64}',quiescence_identity),'CAPTURE_QUIESCENCE_IDENTITY_INVALID')
 destination=private(Path(destination));rows={};seen=[]
 for role,path in sorted(roots.items()):
  path=source(path)
  require(path not in seen and all(not path.is_relative_to(x) and not x.is_relative_to(path) for x in seen),'CAPTURE_ROOT_OVERLAP')
  require(not destination.is_relative_to(path) and not path.is_relative_to(destination),'CAPTURE_DESTINATION_OVERLAP')
  seen.append(path);rows[role]={'path':str(path),'identity':identity(path.stat())}
 value={'schema':1,'kind':KIND,'id':str(uuid.uuid4()),'roots':rows,'destination':str(destination),
  'destination_identity':identity(destination.stat()),'recipients':recipients(recipient_values),
  'quiescence_identity':quiescence_identity,'seconds':seconds,'max_bytes':max_bytes,'max_entries':max_entries}
 return dict(value,review_id=fingerprint(value))

def validate(proposal):
 value=deepcopy(proposal);review=value.pop('review_id',None)
 require(value.get('schema')==1 and value.get('kind')==KIND and str(uuid.UUID(value['id']))==value['id']
   and fingerprint(value)==review,'CAPTURE_REVIEW_CHANGED')
 p=plan({k:r['path'] for k,r in value['roots'].items()},value['destination'],value['recipients'],value['quiescence_identity'],
  seconds=value['seconds'],max_bytes=value['max_bytes'],max_entries=value['max_entries'])
 p['id']=value['id'];p.pop('review_id');require(p==value,'CAPTURE_PREPARED_METADATA_CHANGED')
 return value

class Set:
 """Descriptor-relative scanner, no links/mount traversal; no ownership repair."""
 def __init__(self,proposal,guard):self.p,self.guard=proposal,guard;self.fds={}
 def __enter__(self):
  try:
   for role,row in self.p['roots'].items():
    fd=os.open(source(row['path']),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    require(identity(os.fstat(fd))==row['identity'],'CAPTURE_SOURCE_CHANGED');self.fds[role]=fd
   return self
  except BaseException:self.__exit__(None,None,None);raise
 def __exit__(self,*args):
  for fd in self.fds.values():os.close(fd)
  self.fds.clear()
 def scan(self):
  rows=[];total=0
  def walk(role,fd,relative):
   nonlocal total
   self.guard();s=os.fstat(fd);root=self.p['roots'][role]
   require(s.st_dev==root['identity'][0],'CAPTURE_CROSS_FILESYSTEM_REFUSED')
   path='data/'+role+('/'+relative if relative else '')
   rows.append(self.row(path,s,True));require(len(rows)<=self.p['max_entries'],'CAPTURE_ENTRY_LIMIT')
   for name in sorted(os.listdir(fd)):
    safe_name(name);self.guard();s=os.stat(name,dir_fd=fd,follow_symlinks=False)
    require(s.st_dev==root['identity'][0],'CAPTURE_CROSS_FILESYSTEM_REFUSED')
    child=name if not relative else relative+'/'+name;safe_name(child)
    if stat.S_ISDIR(s.st_mode):
     sub=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
     try:require(signature(os.fstat(sub))==signature(s),'CAPTURE_SOURCE_CHANGED');walk(role,sub,child)
     finally:os.close(sub)
    else:
     require(stat.S_ISREG(s.st_mode),'CAPTURE_NONREGULAR_REFUSED')
     require(s.st_nlink==1,'CAPTURE_HARDLINK_REFUSED');require(not s.st_mode&0o7000,'CAPTURE_SPECIAL_MODE_REFUSED')
     rows.append(self.row('data/'+role+'/'+child,s,False));total+=s.st_size
     require(total<=self.p['max_bytes'] and len(rows)<=self.p['max_entries'],'CAPTURE_SIZE_OR_ENTRY_LIMIT')
  for role,fd in self.fds.items():walk(role,fd,'')
  return rows
 @staticmethod
 def row(path,s,directory):
  require(not s.st_mode&0o7000,'CAPTURE_SPECIAL_MODE_REFUSED')
  return {'path':safe_name(path),'type':'directory' if directory else 'file','size':0 if directory else s.st_size,
   'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode),'mtime_ns':s.st_mtime_ns,'signature':signature(s)}
 def open(self,row):
  parts=row['path'].split('/');fd=os.dup(self.fds[parts[1]])
  try:
   for part in parts[2:-1]:
    sub=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=sub
   leaf=parts[-1];child=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
   try:require(signature(os.fstat(child))==row['signature'] and stat.S_ISREG(os.fstat(child).st_mode),'CAPTURE_SOURCE_CHANGED')
   except BaseException:os.close(child);raise
   return os.fdopen(child,'rb',buffering=0)
  finally:os.close(fd)

class Hashed:
 def __init__(self,stream,guard):self.stream,self.guard=stream,guard;self.hash=hashlib.sha256();self.bytes=0
 def read(self,n):
  self.guard();data=self.stream.read(min(n,65536));self.hash.update(data);self.bytes+=len(data);return data

def atomic_json(folder,name,value,previous=None):
 private(folder);path=folder/name
 if previous is None:require(not path.exists() and not path.is_symlink(),'CAPTURE_RECORD_EXISTS')
 else:require(private(path,False).read_bytes()==previous,'CAPTURE_JOURNAL_CHANGED')
 raw=(json.dumps(value,sort_keys=True,ensure_ascii=False)+'\n').encode()
 require(len(raw)<=MAX_MANIFEST,'CAPTURE_RECORD_LIMIT')
 temp=folder/('.'+name+'-'+uuid.uuid4().hex)
 fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 try:
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  if previous is not None:require(path.read_bytes()==previous,'CAPTURE_JOURNAL_CHANGED')
  else:require(not path.exists() and not path.is_symlink(),'CAPTURE_RECORD_EXISTS')
  os.replace(temp,path);d=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(d)
  finally:os.close(d)
 finally:
  if temp.exists():temp.unlink()
 return raw

def capture(scope,proposal,quiescence,tool,*,hook=lambda phase:None,test_codec=False):
 """Callback core. Requires live scope/lease and identity-bound stopped proof.

Return is a capture receipt, NOT the maintenance callback's service-return
evidence. Caller must restore services then return the core's exact evidence.
"""
 native=type(tool) is Age
 require(native or test_codec is True,'CAPTURE_NATIVE_AGE_REQUIRED')
 value=validate(proposal);until=time.monotonic()+value['seconds'];process=None
 require(getattr(scope,'active',False) is True and type(scope.descriptor) is int,'CAPTURE_LIVE_SCOPE_REQUIRED')
 def guard():
  scope.checkpoint();os.fstat(scope.descriptor)
  require(time.monotonic()<until,'CAPTURE_PROCESS_DEADLINE')
  require(quiescence()=={'schema':1,'identity_sha256':value['quiescence_identity'],'all_stopped':True},'CAPTURE_QUIESCENCE_REQUIRED')
  for row in value['roots'].values():require(identity(source(row['path']).stat())==row['identity'],'CAPTURE_SOURCE_CHANGED')
  require(identity(private(Path(value['destination'])).stat())==value['destination_identity'],'CAPTURE_DESTINATION_CHANGED')
 guard();folder=Path(value['destination'])/value['id'];require(not folder.exists() and not folder.is_symlink(),'CAPTURE_JOB_ALREADY_EXISTS')
 folder.mkdir(mode=0o700);fixed=identity(folder.stat());record={'schema':1,'kind':KIND,'id':value['id'],'review_id':proposal['review_id'],'phase':'prepared'}
 raw=atomic_json(folder,'job.json',record);partial=folder/'payload.age.partial'
 def save(phase,**extra):
  nonlocal raw
  require(identity(private(folder).stat())==fixed,'CAPTURE_JOB_DIRECTORY_CHANGED')
  record.update(phase=phase,**extra);raw=atomic_json(folder,'job.json',record,raw);hook('journal:'+phase)
 try:
  guard();save('capturing')
  with Set(value,guard) as dataset:
   rows=dataset.scan();guard();fd=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
   try:
    process=tool.spawn(['-e',*[arg for r in value['recipients'] for arg in ('-r',r)]],output_fd=fd,lease=(scope.descriptor,),guard=guard)
    pipe=Pipe(process.stdin.fileno(),'write',until,guard)
    with tarfile.open(fileobj=pipe,mode='w|',format=tarfile.PAX_FORMAT) as archive:
     for row in rows:
      guard();hook('entry:'+row['type']);member=tarfile.TarInfo(row['path']);member.mode=row['mode'];member.uid=row['uid'];member.gid=row['gid']
      member.mtime=row['mtime_ns']//10**9;member.size=row['size']
      if row['type']=='directory':member.type=tarfile.DIRTYPE;archive.addfile(member)
      else:
       with dataset.open(row) as stream:
        hashed=Hashed(stream,guard);archive.addfile(member,hashed)
        require(hashed.bytes==row['size'] and signature(os.fstat(stream.fileno()))==row['signature'],'CAPTURE_SOURCE_CHANGED')
        row['sha256']=hashed.hash.hexdigest()
     guard();before=[{k:v for k,v in r.items() if k!='sha256'} for r in rows]
     require(dataset.scan()==before,'CAPTURE_SOURCE_CHANGED')
     manifest={'schema':1,'kind':KIND,'id':value['id'],'quiescence_identity':value['quiescence_identity'],
      'entries':[{k:v for k,v in r.items() if k!='signature'} for r in rows]}
     manifest_raw=json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
     require(len(manifest_raw)<=MAX_MANIFEST,'CAPTURE_MANIFEST_LIMIT')
     member=tarfile.TarInfo('manifest.json');member.size=len(manifest_raw);member.mode=0o600;archive.addfile(member,io.BytesIO(manifest_raw))
    process.stdin.close();tool.finish(process,until);process=None;os.fsync(fd)
   finally:os.close(fd)
   guard();require(dataset.scan()==before,'CAPTURE_SOURCE_CHANGED')
  guard();hook('before_publish');guard();cipher=folder/'payload.age'
  require(private(folder/'job.json',False).read_bytes()==raw,'CAPTURE_JOURNAL_CHANGED')
  with Set(value,guard) as dataset:require(dataset.scan()==before,'CAPTURE_SOURCE_CHANGED')
  require(not cipher.exists() and not cipher.is_symlink(),'CAPTURE_PAYLOAD_ALREADY_EXISTS')
  os.rename(partial,cipher)
  receipt={'schema':1,'kind':KIND,'id':value['id'],'ciphertext_sha256':sha(cipher),'ciphertext_bytes':cipher.stat().st_size,
   'manifest_sha256':hashlib.sha256(manifest_raw).hexdigest(),'recipient_fingerprints':[hashlib.sha256(r.encode()).hexdigest() for r in value['recipients']],
   'entry_count':len(rows),'logical_bytes':sum(r['size'] for r in rows),'capture_only':True,'restore_verified':False,
   'encryption':'age_v1' if native else 'synthetic_identity_codec'}
  guard();atomic_json(folder,'receipt.json',receipt);save('captured',receipt_sha256=fingerprint(receipt),child_processes_stopped=True)
  return {'result':'captured','job_directory':str(folder),'receipt':receipt,'child_processes_stopped':True,
          'plaintext_archive_written':not native,'separate_plaintext_archive_written':False,'restore_verified':False}
 except BaseException as exc:
  tool.stop(process);process=None
  try:save('attention_required',code=exc.code if isinstance(exc,Failure) else 'CAPTURE_OPERATION_FAILED',child_processes_stopped=True)
  except BaseException:pass  # Never overwrite an altered audit or claim it durable.
  raise
 finally:tool.stop(process)

def restore(job,trusted_receipt,parent,identity_path,tool,*,seconds=60,max_bytes=1024*1024*1024,max_entries=4096,test_codec=False):
 """Explicit isolated restore. Trust receipt separately; consume crypto EOF.

Fresh owned directory only; no extractall, chown, device/link materialization,
or application restore acceptance. Identity is an explicitly supplied private
native key; checker supplies only a newly generated temporary test identity.
"""
 native=type(tool) is Age
 require(native or test_codec is True,'CAPTURE_NATIVE_AGE_REQUIRED')
 require(type(seconds) is int and 1<=seconds<=3600 and type(max_bytes) is int and 0<max_bytes<=2**40
         and type(max_entries) is int and 1<=max_entries<=MAX_ENTRIES,'CAPTURE_BUDGET_INVALID')
 job=private(Path(job));parent=private(Path(parent));key=private(Path(identity_path),False)
 require(not parent.is_relative_to(job) and not job.is_relative_to(parent),'RESTORE_DESTINATION_OVERLAP')
 require(key.stat().st_size<=4096 and not key.is_relative_to(job) and not key.is_relative_to(parent),'RESTORE_IDENTITY_SCOPE_INVALID')
 lines=[l for l in key.read_text().splitlines() if l and not l.startswith('#')]
 require(len(lines)==1 and re.fullmatch('AGE-SECRET-KEY-1[0-9A-Z]{58}',lines[0]),'RESTORE_NATIVE_IDENTITY_REQUIRED')
 receipt_path=private(job/'receipt.json',False);record_path=private(job/'job.json',False)
 require(receipt_path.stat().st_size<=32768 and record_path.stat().st_size<=32768,'RESTORE_RECEIPT_LIMIT')
 actual=json.loads(receipt_path.read_text());record=json.loads(record_path.read_text())
 require(actual==trusted_receipt and actual.get('kind')==KIND and actual.get('schema')==1,'RESTORE_TRUSTED_RECEIPT_REQUIRED')
 require(actual.get('encryption')==('age_v1' if native else 'synthetic_identity_codec'),'RESTORE_CODEC_MISMATCH')
 require(record.get('kind')==KIND and record.get('id')==actual.get('id') and record.get('phase')=='captured'
         and record.get('receipt_sha256')==fingerprint(actual),'RESTORE_CAPTURE_NOT_ACCEPTED')
 cipher=private(job/'payload.age',False);cipher_sig=signature(cipher.stat());key_sig=signature(key.stat());parent_id=identity(parent.stat())
 require(type(actual.get('entry_count')) is int and type(actual.get('logical_bytes')) is int
    and type(actual.get('ciphertext_bytes')) is int and isinstance(actual.get('ciphertext_sha256'),str)
    and re.fullmatch('[0-9a-f]{64}',actual['ciphertext_sha256']),'RESTORE_RECEIPT_INVALID')
 require(0<actual['entry_count']<=max_entries and 0<=actual['logical_bytes']<=max_bytes,'RESTORE_BUDGET_EXCEEDED')
 stream_limit=actual['logical_bytes']+actual['entry_count']*8192+MAX_MANIFEST+65536
 require(0<actual['ciphertext_bytes']<=stream_limit+stream_limit//1024+65536,'RESTORE_CIPHERTEXT_LIMIT')
 require(cipher.stat().st_size==actual['ciphertext_bytes'] and sha(cipher)==actual['ciphertext_sha256'],'RESTORE_CIPHERTEXT_CHANGED')
 until=time.monotonic()+seconds;stage=parent/('restore-'+uuid.uuid4().hex+'.partial');stage.mkdir(mode=0o700);stage_id=identity(stage.stat());process=None
 def guard():
  require(time.monotonic()<until,'CAPTURE_PROCESS_DEADLINE')
  require(identity(private(parent).stat())==parent_id and identity(private(stage).stat())==stage_id,'RESTORE_DIRECTORY_CHANGED')
  require(signature(private(cipher,False).stat())==cipher_sig and signature(private(key,False).stat())==key_sig,'RESTORE_INPUT_CHANGED')
 try:
  fd=os.open(cipher,os.O_RDONLY|os.O_NOFOLLOW);seen={};total=0;manifest=None;manifest_hash=None
  try:
   require(signature(os.fstat(fd))==cipher_sig,'RESTORE_INPUT_CHANGED')
   process=tool.spawn(['-d','-i',str(key),'-o','-'],input_fd=fd,guard=guard)
   reader=Pipe(process.stdout.fileno(),'read',until,guard,read_limit=stream_limit)
   with tarfile.open(fileobj=reader,mode='r|') as archive:
    for member in archive:
     guard();name=safe_name(member.name.rstrip('/') if member.isdir() else member.name)
     require(manifest is None,'RESTORE_MEMBER_AFTER_MANIFEST')
     require(not member.pax_headers or set(member.pax_headers)<={'path','mtime','size','uid','gid'},'RESTORE_PAX_UNSUPPORTED')
     require(not member.issparse() and (member.isfile() or member.isdir()) and not member.linkname,'RESTORE_MEMBER_TYPE_REFUSED')
     if name=='manifest.json':
      require(member.isfile() and 0<member.size<=MAX_MANIFEST,'RESTORE_MANIFEST_INVALID')
      data=archive.extractfile(member).read(MAX_MANIFEST+1);manifest_hash=hashlib.sha256(data).hexdigest();manifest=json.loads(data)
      continue
     require(name.startswith('data/') and len(name.split('/'))>=2 and ROLE.fullmatch(name.split('/')[1]),'RESTORE_MEMBER_PATH_REFUSED')
     require(name not in seen and len(seen)<max_entries,'RESTORE_DUPLICATE_OR_ENTRY_LIMIT')
     require(type(member.size) is int and 0<=member.size<=max_bytes-total and 0<=member.mode<=0o777
             and member.uid==os.getuid() and member.gid==os.getgid(),'RESTORE_METADATA_UNSUPPORTED')
     require(member.mtime>=0 and member.mtime<=2**40,'RESTORE_METADATA_UNSUPPORTED')
     dest=stage/name;dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
     require(not dest.exists() and not dest.is_symlink(),'RESTORE_MEMBER_COLLISION')
     row={'path':name,'type':'directory' if member.isdir() else 'file','size':member.size,'uid':member.uid,'gid':member.gid,'mode':member.mode}
     if member.isdir():require(member.size==0,'RESTORE_DIRECTORY_SIZE_INVALID');dest.mkdir(mode=0o700)
     else:
      total+=member.size;h=hashlib.sha256();stream=archive.extractfile(member)
      outfd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
      with os.fdopen(outfd,'wb') as out:
       remaining=member.size
       while remaining:
        guard();data=stream.read(min(65536,remaining));require(data,'RESTORE_TRUNCATED_MEMBER');remaining-=len(data);h.update(data);out.write(data)
       out.flush();os.fsync(out.fileno())
      row['sha256']=h.hexdigest()
     seen[name]=row
    # Drain the tar reader including its read-ahead until crypto EOF. The end
    # header alone does not establish the final age chunk's authentication.
    while True:
     data=archive.fileobj.read(65536)
     if not data:break
     require(not any(data),'RESTORE_TRAILING_DATA')
   tool.finish(process,until);process.stdout.close();process=None;guard()
  finally:os.close(fd)
  require(isinstance(manifest,dict) and manifest.get('schema')==1 and manifest.get('kind')==KIND
          and manifest.get('id')==actual['id'] and manifest_hash==actual['manifest_sha256'],'RESTORE_MANIFEST_MISMATCH')
  entries=manifest.get('entries');require(isinstance(entries,list) and len(entries)==len(seen)==actual['entry_count']
      and total==actual['logical_bytes'],'RESTORE_SET_MISMATCH')
  names=set()
  for row in entries:
   require(isinstance(row,dict) and set(row)==({'path','type','size','uid','gid','mode','mtime_ns'}|({'sha256'} if row.get('type')=='file' else set())),'RESTORE_MANIFEST_INVALID')
   name=safe_name(row['path']);require(name not in names and name in seen,'RESTORE_SET_MISMATCH');names.add(name)
   expected=seen[name];require({k:v for k,v in row.items() if k!='mtime_ns'}==expected,'RESTORE_HASH_OR_METADATA_MISMATCH')
   require(type(row['mtime_ns']) is int and 0<=row['mtime_ns']<2**63,'RESTORE_METADATA_UNSUPPORTED')
  require(names==set(seen),'RESTORE_SET_MISMATCH')
  # Publish permissions/timestamps only after final age authentication and set verification.
  for row in sorted(entries,key=lambda r:len(PurePosixPath(r['path']).parts),reverse=True):
   guard();dest=stage/row['path'];os.chmod(dest,row['mode']);os.utime(dest,ns=(row['mtime_ns'],row['mtime_ns']))
   observed=dest.stat();require(stat.S_IMODE(observed.st_mode)==row['mode'] and observed.st_mtime_ns==row['mtime_ns']
     and observed.st_uid==row['uid'] and observed.st_gid==row['gid'],'RESTORE_APPLIED_METADATA_MISMATCH')
  guard();final=parent/stage.name.removesuffix('.partial');require(not final.exists(),'RESTORE_DESTINATION_EXISTS')
  os.rename(stage,final);d=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(d)
  finally:os.close(d)
  return {'result':'file_restore_verified','directory':str(final),'entry_count':len(entries),'logical_bytes':total,
   'crypto_eof_authenticated':native,'file_set_and_hashes_verified':True,'permissions_and_mtime_verified':True,
   'uid_gid_restored_by_chown':False,'application_restore_acceptance':False}
 except BaseException:
  tool.stop(process);process=None
  if stage.exists():
   require(identity(stage.lstat())==stage_id and not stage.is_symlink(),'RESTORE_CLEANUP_DIRECTORY_CHANGED')
   # Only this freshly created owned stage; never an existing restore/source tree.
   for p in stage.rglob('*'):
    if p.is_dir() and not p.is_symlink():p.chmod(0o700)
   shutil.rmtree(stage)
  raise
 finally:tool.stop(process)
