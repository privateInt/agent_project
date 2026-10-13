"""Reviewed one-store reader. No daemon, key, network or service control.

The source is read as its discovered UID/GID. The script emitted for a private
helper reuses the reviewed descriptor-relative scanner from the capture core.
Its header and bounded final summary frame a tar stream, never a disk archive.
"""
import hashlib
import inspect
import io
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import sys
import tarfile
import time
import uuid

from ops_candidates.backup_capture_20260930 import capture as c
from ops_candidates.backup_capture_20260930.age_tool import require

MAGIC='agent-project-cold-reader-v1'

def stream(config,output,summary):
 require(set(config)=={'schema','id','role','source','identity','uid','gid','seconds','max_bytes','max_entries','quiescence_identity'}
  and config['schema']==1 and str(uuid.UUID(config['id']))==config['id'],'COLD_READER_CONFIG_INVALID')
 require(c.ROLE.fullmatch(config['role']) and type(config['seconds']) is int and 1<=config['seconds']<=3600
  and type(config['max_bytes']) is int and 0<config['max_bytes']<=2**40
  and type(config['max_entries']) is int and 1<=config['max_entries']<=65536,'COLD_READER_BUDGET_INVALID')
 require(os.getuid()==config['uid'] and os.getgid()==config['gid'],'COLD_READER_ACTOR_MISMATCH')
 require(c.identity(c.source(config['source']).stat())==config['identity'],'COLD_READER_SOURCE_CHANGED')
 until=time.monotonic()+config['seconds']
 def guard():
  require(time.monotonic()<until,'COLD_READER_DEADLINE')
  require(c.identity(c.source(config['source']).stat())==config['identity'],'COLD_READER_SOURCE_CHANGED')
 guard();role=config['role'];proposal={'roots':{role:{'path':config['source'],'identity':config['identity']}},
   'max_entries':config['max_entries'],'max_bytes':config['max_bytes']}
 header={'schema':1,'magic':MAGIC,'id':config['id'],'role':role,'identity':config['identity'],'uid':os.getuid(),'gid':os.getgid()}
 output.write((json.dumps(header,sort_keys=True)+'\n').encode());output.flush()
 with c.Set(proposal,guard) as dataset:
  rows=dataset.scan()
  with tarfile.open(fileobj=output,mode='w|',format=tarfile.PAX_FORMAT) as archive:
   for row in rows:
    guard();member=tarfile.TarInfo(row['path']);member.mode=row['mode'];member.uid=row['uid'];member.gid=row['gid']
    member.mtime=row['mtime_ns']//10**9;member.size=row['size']
    if row['type']=='directory':member.type=tarfile.DIRTYPE;archive.addfile(member)
    else:
     with dataset.open(row) as file:
      hashed=c.Hashed(file,guard);archive.addfile(member,hashed)
      require(hashed.bytes==row['size'] and c.signature(os.fstat(file.fileno()))==row['signature'],'COLD_READER_SOURCE_CHANGED')
      row['sha256']=hashed.hash.hexdigest()
   before=[{k:v for k,v in row.items() if k!='sha256'} for row in rows]
   require(dataset.scan()==before,'COLD_READER_SOURCE_CHANGED')
   manifest={'schema':1,'kind':c.KIND,'id':config['id'],'quiescence_identity':config['quiescence_identity'],
             'entries':[{k:v for k,v in row.items() if k!='signature'} for row in rows]}
   raw=json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
   require(len(raw)<=c.MAX_MANIFEST,'COLD_READER_MANIFEST_LIMIT')
   member=tarfile.TarInfo('manifest.json');member.size=len(raw);member.mode=0o600;archive.addfile(member,io.BytesIO(raw))
  output.flush();guard();require(dataset.scan()==before,'COLD_READER_SOURCE_CHANGED')
  value={'schema':1,'magic':MAGIC,'id':config['id'],'role':role,'manifest_sha256':hashlib.sha256(raw).hexdigest(),
         'entry_count':len(rows),'logical_bytes':sum(row['size'] for row in rows),'source_stable':True}
  summary.write((json.dumps(value,sort_keys=True)+'\n').encode());summary.flush()
 return value

def main(config):
 def alarm(*args):raise RuntimeError('COLD_READER_DEADLINE')
 signal.signal(signal.SIGALRM,alarm);signal.alarm(config['seconds'])
 try:stream(config,sys.stdout.buffer,sys.stderr.buffer);return 0
 except BaseException:
  # Never send filenames, credentials or exception text to Docker stderr/logs.
  sys.stderr.buffer.write(b'{"result":"attention_required","code":"COLD_READER_FAILED"}\n');sys.stderr.buffer.flush();return 2
 finally:signal.alarm(0)

def script():
 """A bounded stdlib script, no host path imports or source file bind mount."""
 common='\n'.join(inspect.getsource(x) for x in (c.identity,c.signature,c.safe_name,c.source,c.Set,c.Hashed))
 own='\n'.join(inspect.getsource(x) for x in (stream,main))
 pre='import hashlib,io,json,os,re,signal,stat,sys,tarfile,time,uuid\nfrom pathlib import Path,PurePosixPath\nfrom types import SimpleNamespace\n'
 pre+='class Failure(Exception):\n def __init__(self,code):self.code=code;super().__init__(code)\ndef require(ok,code):\n if not ok:raise Failure(code)\n'
 pre+='MAGIC='+repr(MAGIC)+'\n'
 bind="c=SimpleNamespace(identity=identity,signature=signature,safe_name=safe_name,source=source,Set=Set,Hashed=Hashed,ROLE=re.compile('[a-z][a-z0-9_-]{0,47}'),KIND="+repr(c.KIND)+",MAX_MANIFEST=16*1024*1024)\n"
 code=pre+common+'\n'+bind+own+'\nraise SystemExit(main(json.loads(sys.argv[1])))\n'
 require(len(code.encode())<=32768,'COLD_READER_SCRIPT_LIMIT');return code
