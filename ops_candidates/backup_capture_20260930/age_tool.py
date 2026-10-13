"""Native X25519 age process adapter and explicit temporary test-tool fetch.

No package/system installation, plugins, identity discovery or crypto fallback.
Only the isolated checker calls fetch; capture accepts public recipients only.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import select
import signal
import stat
import subprocess
import tarfile
import time
import urllib.request

VERSION='v1.3.2'
RELEASES={
 'x86_64':('amd64',19405817,'cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10'),
 'aarch64':('arm64',17771923,'6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4')
}
CHARSET='qpzry9x8gf2tvdw0s3jn54khce6mua7l'

class Failure(Exception):
 def __init__(self,code):self.code=code;super().__init__(code)

def require(ok,code):
 if not ok:raise Failure(code)

def recipients(values):
 require(isinstance(values,list) and 1<=len(values)<=8,'CAPTURE_RECIPIENT_REQUIRED')
 seen=set()
 for value in values:
  require(isinstance(value,str) and len(value)==62 and value.startswith('age1') and all(c in CHARSET for c in value[4:]),'CAPTURE_RECIPIENT_INVALID')
  digits=[CHARSET.index(c) for c in value[4:]];check=1
  for digit in [3,3,3,0,1,7,5,*digits]:
   high=check>>25;check=((check&0x1ffffff)<<5)^digit
   for bit,poly in enumerate((0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3)):
    if high&(1<<bit):check^=poly
  require(check==1 and digits[-7]&15==0,'CAPTURE_RECIPIENT_INVALID')
  require(value not in seen,'CAPTURE_RECIPIENT_DUPLICATE');seen.add(value)
 return sorted(seen)

def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for block in iter(lambda:f.read(65536),b''):h.update(block)
 return h.hexdigest()

def private(path,directory=True):
 path=Path(path)
 require(path.is_absolute(),'CAPTURE_ABSOLUTE_PATH_REQUIRED')
 for p in (*reversed(path.parents),path):require(not p.is_symlink(),'CAPTURE_SYMLINK_REFUSED')
 s=path.lstat()
 require((stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)) and s.st_uid==os.getuid()
         and not s.st_mode&0o077 and not s.st_mode&0o7000,'CAPTURE_PRIVATE_PATH_REQUIRED')
 if not directory:require(s.st_nlink==1,'CAPTURE_HARDLINK_REFUSED')
 return path

def fetch(directory):
 """Explicitly fetch one reviewed release into an empty private temporary dir."""
 directory=private(directory);require(not list(directory.iterdir()),'AGE_TEST_DIRECTORY_NOT_EMPTY')
 require(platform.system()=='Linux' and platform.machine() in RELEASES,'AGE_TEST_ARCHITECTURE_UNAVAILABLE')
 arch,size,digest=RELEASES[platform.machine()]
 url='https://github.com/FiloSottile/age/releases/download/'+VERSION+'/age-'+VERSION+'-linux-'+arch+'.tar.gz'
 until=time.monotonic()+90;parts=[];length=0
 try:
  with urllib.request.urlopen(url,timeout=10) as response:
   require(response.geturl().startswith('https://'),'AGE_TEST_REDIRECT_INVALID')
   while True:
    require(time.monotonic()<until,'AGE_TEST_DOWNLOAD_TIMEOUT')
    block=response.read(65536)
    if not block:break
    length+=len(block);require(length<=size,'AGE_TEST_DOWNLOAD_SIZE');parts.append(block)
 except Failure:raise
 except Exception:raise Failure('AGE_TEST_DOWNLOAD_FAILED') from None
 raw=b''.join(parts)
 require(len(raw)==size and hashlib.sha256(raw).hexdigest()==digest,'AGE_TEST_DOWNLOAD_DIGEST')
 wanted={'age/age':'age','age/age-keygen':'age-keygen'};found={}
 with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
  for member in archive:
   if member.name in wanted:
    require(member.isfile() and member.name not in found and 0<member.size<=64*1024*1024,'AGE_TEST_ARCHIVE_INVALID')
    found[member.name]=archive.extractfile(member).read(member.size+1)
 require(set(found)==set(wanted),'AGE_TEST_ARCHIVE_INVALID')
 for name,data in found.items():
  require(len(data)>0,'AGE_TEST_ARCHIVE_INVALID')
  fd=os.open(directory/wanted[name],os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o700)
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
 return {'version':VERSION,'archive_sha256':digest,'architecture':arch,'temporary_only':True,
         'binaries':{name:sha(directory/name) for name in wanted.values()},'sigsum_proof_verified':False}

class Pipe:
 def __init__(self,fd,direction,deadline,guard,read_limit=None):
  self.fd,self.direction,self.deadline,self.guard=fd,direction,deadline,guard
  self.read_limit,self.read_bytes=read_limit,0
  os.set_blocking(fd,False)
 def wait(self):
  self.guard();left=self.deadline-time.monotonic();require(left>0,'CAPTURE_PROCESS_DEADLINE')
  ready=select.select([self.fd] if self.direction=='read' else [],[self.fd] if self.direction=='write' else [],[],min(left,.1))
  return bool(ready[0] or ready[1])
 def write(self,data):
  view=memoryview(data);total=len(view)
  while view:
   if self.wait():
    try:view=view[os.write(self.fd,view):]
    except BlockingIOError:pass
  return total
 def read(self,size=-1):
  require(0<=size<=1024*1024,'CAPTURE_STREAM_READ_LIMIT')
  while True:
   if self.wait():
    try:
     data=os.read(self.fd,size);self.read_bytes+=len(data)
     require(self.read_limit is None or self.read_bytes<=self.read_limit,'RESTORE_PLAINTEXT_STREAM_LIMIT')
     return data
    except BlockingIOError:pass

class Age:
 def __init__(self,path,digest):
  self.path=private(Path(path),False);self.digest=digest;self.check()
 def check(self):
  private(self.path,False);require(re.fullmatch('[0-9a-f]{64}',self.digest) and sha(self.path)==self.digest,'AGE_BINARY_CHANGED')
  require(os.access(self.path,os.X_OK),'AGE_BINARY_NOT_EXECUTABLE')
 def spawn(self,args,*,input_fd=None,output_fd=None,lease=(),guard=lambda:None):
  self.check();guard()
  return subprocess.Popen([str(self.path),*args],stdin=input_fd if input_fd is not None else subprocess.PIPE,
   stdout=output_fd if output_fd is not None else subprocess.PIPE,stderr=subprocess.DEVNULL,
   close_fds=True,pass_fds=lease,start_new_session=True,env={'PATH':os.defpath,'LC_ALL':'C','HOME':'/nonexistent'})
 @staticmethod
 def finish(process,deadline):
  try:rc=process.wait(timeout=max(.001,deadline-time.monotonic()))
  except subprocess.TimeoutExpired:raise Failure('CAPTURE_PROCESS_DEADLINE') from None
  require(rc==0,'AGE_PROCESS_FAILED')
 @staticmethod
 def stop(process):
  if process is None:return
  # This PID is a live child created by this invocation, never a saved PID.
  if process.poll() is None:
   try:os.killpg(process.pid,signal.SIGKILL)
   except ProcessLookupError:pass
  try:process.wait(timeout=5)
  except subprocess.TimeoutExpired:raise Failure('CAPTURE_CHILD_EXIT_UNCONFIRMED') from None
  for stream in (process.stdin,process.stdout):
   if stream is not None:stream.close()

def test_identity(directory):
 """Temporary synthetic identity only; never inspect existing user keys."""
 directory=private(directory);keygen=directory/'age-keygen';private(keygen,False)
 identity=directory/'test-identity';fd=os.open(identity,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:
  subprocess.run([str(keygen)],stdin=subprocess.DEVNULL,stdout=fd,stderr=subprocess.DEVNULL,check=True,timeout=10,env={'PATH':os.defpath,'LC_ALL':'C','HOME':str(directory)})
 finally:os.close(fd)
 result=subprocess.run([str(keygen),'-y',str(identity)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True,timeout=10,env={'PATH':os.defpath,'LC_ALL':'C','HOME':str(directory)})
 value=result.stdout.decode().strip();recipients([value]);return identity,value
