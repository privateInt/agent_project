"""Inactive cold storage operation. Explicit reviewed providers, never a CLI.

Rootful local Docker only. Stop/start existing IDs, one constrained UID reader
at a time, host age streaming, durable nonreplayable audit. A storage result
does not resume a worker or authorize a database/application restore.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import time
import uuid

from ops_candidates.backup_capture_20260930 import capture as c, age_tool as a
from . import reader

require=a.require
LABEL='org.agent-project.backup.reader.job'
SPEC_LABEL='org.agent-project.backup.reader.spec'
HEX=re.compile('[0-9a-f]{64}')
KIND='scoped-cold-storage-v1'
FIELDS=('id','image','labels','mounts','state','health','userns','restart')
FORMAT='['+','.join('{{json '+field+'}}' for field in (
 '.Id','.Image','.Config.Labels','.Mounts','.State',
 '.State.Health.Status','.HostConfig.UsernsMode','.HostConfig.RestartPolicy.Name'))+']'
# Health may be absent; use a template condition rather than dereferencing nil.
FORMAT=FORMAT.replace('{{json .State.Health.Status}}','{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}')
HELPER_FORMAT='['+','.join('{{json '+field+'}}' for field in (
 '.Config.User','.Config.Entrypoint','.Config.Cmd','.Config.Tty','.HostConfig.ReadonlyRootfs',
 '.HostConfig.NetworkMode','.HostConfig.CapDrop','.HostConfig.SecurityOpt','.HostConfig.Privileged',
 '.HostConfig.PidMode','.HostConfig.IpcMode','.HostConfig.LogConfig.Type',
 '.HostConfig.PidsLimit','.HostConfig.Memory','.HostConfig.NanoCpus'))+']'
HOST_FORMAT='[{{json .ID}},{{json .OSType}},{{json .SecurityOptions}}]'
IMAGE_FORMAT='[{{json .Id}},{{json .Config.Volumes}}]'

def valid_path(value):
 path=c.source(value)
 require(str(path)!='/' and '..' not in path.parts and not any(path.is_relative_to(x) for x in ('/proc','/sys','/dev','/run'))
  and ',' not in str(path),'COLD_SOURCE_SCOPE_INVALID')
 return path

def mount_topology(path):
 rows=[]
 for line in Path('/proc/self/mountinfo').read_text().splitlines():
  raw=line.split(' - ',1)[0].split()[4]
  mounted=Path(re.sub(r'\\([0-7]{3})',lambda match:chr(int(match[1],8)),raw))
  if mounted.is_relative_to(path):rows.append(str(mounted))
 require(all(Path(x)==path for x in rows),'COLD_NESTED_MOUNT_REFUSED')
 return c.fingerprint(sorted(rows))

def fixed(row):
 return {key:deepcopy(row[key]) for key in ('container_id','image_id','owner','owner_label','project','service','mounts','userns','restart')}

def validate_row(role,row):
 require(c.ROLE.fullmatch(role) and HEX.fullmatch(row['container_id']) and re.fullmatch('sha256:[0-9a-f]{64}',row['image_id']),
         'COLD_CONTAINER_IDENTITY_INVALID')
 require(str(uuid.UUID(row['owner']))==row['owner'] and re.fullmatch('org\\.agent-project\\.[a-z.-]+\\.owner',row['owner_label'])
  and isinstance(row['project'],str) and row['project'] and row['service']==role,'COLD_CONTAINER_OWNER_INVALID')
 require(row['userns'] in ('','host') and row['restart'] in ('no','unless-stopped','always','on-failure'),
         'COLD_NAMESPACE_OR_RESTART_UNSUPPORTED')
 require(isinstance(row['mounts'],list) and len(row['mounts'])<=32,'COLD_MOUNTS_INVALID')
 require(row['state']['Running'] is True and row['state']['Status']=='running' and row['state']['Pid']>0
   and row['health'] in ('healthy','none'),'COLD_BASELINE_NOT_HEALTHY')

def stop_order(services,dependencies):
 require(set(dependencies)==set(services) and all(isinstance(v,list) and len(v)==len(set(v))
  and set(v)<=set(services)-{k} for k,v in dependencies.items()),'COLD_DEPENDENCIES_INVALID')
 left=set(services);order=[]
 while left:
  frontier=sorted(k for k in left if not any(k in dependencies[x] for x in left))
  require(frontier,'COLD_DEPENDENCY_CYCLE');order+=frontier;left-=set(frontier)
 return order

def plan(host,services,protected,stores,helper,dependencies,destination,*,public_recipients,seconds=300,stop_seconds=30,max_bytes=1024**3,max_entries=4096):
 """Metadata only; caller supplies discovered owner/Compose projections.

No key contents or source body scan. Does not select a destination or invoke a
daemon/service. A plan authorizes no execution without a live callback scope.
"""
 require(type(seconds) is int and 5<=seconds<=3600 and type(stop_seconds) is int and 1<=stop_seconds<=120
  and type(max_bytes) is int and 0<max_bytes<=2**40 and type(max_entries) is int and 1<=max_entries<=65536,'COLD_BUDGET_INVALID')
 require(isinstance(host,dict) and set(host)=={'endpoint','daemon_id','docker_root'} and
  re.fullmatch('unix:///[^\x00\n]+',host['endpoint']) and isinstance(host['daemon_id'],str) and host['daemon_id'],
  'COLD_LOCAL_DAEMON_REQUIRED')
 require(isinstance(services,dict) and 1<=len(services)<=32 and len({r['container_id'] for r in services.values()})==len(services),
  'COLD_SERVICE_SCOPE_INVALID')
 for role,row in services.items():validate_row(role,row)
 require(isinstance(protected,dict) and len(protected)<=128 and all(HEX.fullmatch(k) and isinstance(v,str) and v for k,v in protected.items())
  and not set(protected)&{r['container_id'] for r in services.values()},'COLD_PROTECTED_SCOPE_INVALID')
 require(set(helper)=={'image_id','python'} and re.fullmatch('sha256:[0-9a-f]{64}',helper['image_id']) and
  re.fullmatch('(?:/[a-zA-Z0-9_./-]+/)?python3(?:\\.[0-9]+)?',helper['python']) and '..' not in helper['python']
  and helper['image_id'] in {r['image_id'] for r in services.values()},'COLD_EXISTING_READER_IMAGE_REQUIRED')
 destination=a.private(Path(destination));root=Path(host['docker_root']);rows={};seen=[]
 require(isinstance(stores,dict) and 1<=len(stores)<=32,'COLD_STORES_INVALID')
 for role,store in sorted(stores.items()):
  require(c.ROLE.fullmatch(role) and set(store)=={'source','target','service'} and store['service'] in services,'COLD_STORE_ROLE_INVALID')
  path=valid_path(store['source']);require(not path.is_relative_to(root) and not root.is_relative_to(path),'COLD_DOCKER_ROOT_REFUSED')
  require(not any(path.is_relative_to(p) or p.is_relative_to(path) for p in seen)
   and not destination.is_relative_to(path) and not path.is_relative_to(destination),'COLD_SOURCE_DESTINATION_OVERLAP')
  seen.append(path);mounts=services[store['service']]['mounts']
  require(sum(m.get('Type')=='bind' and m.get('Source')==str(path) and m.get('Destination')==store['target'] and m.get('RW') is True for m in mounts)==1,
   'COLD_STORE_BINDING_NOT_UNIQUE')
  info=path.stat();rows[role]={**deepcopy(store),'source':str(path),'identity':c.identity(info),'uid':info.st_uid,'gid':info.st_gid,
    'mount_topology_sha256':mount_topology(path)}
 value={'schema':1,'kind':KIND,'id':str(uuid.uuid4()),'host':deepcopy(host),'services':deepcopy(services),'protected':deepcopy(protected),
  'stores':rows,'helper':deepcopy(helper),'dependencies':deepcopy(dependencies),'stop_order':stop_order(services,dependencies),
  'destination':str(destination),'destination_identity':c.identity(destination.stat()),'seconds':seconds,'stop_seconds':stop_seconds,
  'max_bytes':max_bytes,'max_entries':max_entries,'recipients':a.recipients(public_recipients),
  'reader_script_sha256':hashlib.sha256(reader.script().encode()).hexdigest()}
 return dict(value,review_id=c.fingerprint(value))

def validate(proposal):
 value=deepcopy(proposal);review=value.pop('review_id',None)
 require(c.fingerprint(value)==review and value['kind']==KIND,'COLD_REVIEW_CHANGED')
 fresh=plan(value['host'],value['services'],value['protected'],
  {k:{x:r[x] for x in ('source','target','service')} for k,r in value['stores'].items()},
  value['helper'],value['dependencies'],value['destination'],public_recipients=value['recipients'],seconds=value['seconds'],stop_seconds=value['stop_seconds'],
  max_bytes=value['max_bytes'],max_entries=value['max_entries'])
 fresh['id']=value['id'];fresh.pop('review_id');require(fresh==value,'COLD_PREPARED_METADATA_CHANGED');return value

def config(p,role,identifier,seconds):
 row=p['stores'][role]
 return {'schema':1,'id':identifier,'role':role,'source':'/source','identity':row['identity'],'uid':row['uid'],'gid':row['gid'],
  'seconds':seconds,'max_bytes':p['max_bytes'],'max_entries':p['max_entries'],'quiescence_identity':c.fingerprint(p)}

def recipe(p,role,identifier,seconds,probe=False):
 cfg=config(p,role,identifier,seconds);code=reader.script()
 if probe:
  code='import json,os,sys\nfrom pathlib import Path\np=json.loads(sys.argv[1]);s=Path("/source").stat()\nprint(json.dumps({"identity":[s.st_dev,s.st_ino,s.st_uid,s.st_gid,s.st_mode&0o7777],"uid":os.getuid(),"gid":os.getgid(),"python":list(sys.version_info[:2])}))\n'
 value={'name':'ops-cold-'+identifier.replace('-',''),'job':p['id'],'image_id':p['helper']['image_id'],'python':p['helper']['python'],
  'source':p['stores'][role]['source'],'uid':cfg['uid'],'gid':cfg['gid'],'script':code,'config':cfg,'probe':probe}
 return dict(value,spec_sha256=c.fingerprint(value))

class Docker:
 """Constrained native CLI provider; not reachable from a production CLI."""
 def __init__(self,host,binary,runner=None):
  self.host=deepcopy(host);self.binary=str(Path(binary).resolve());self.runner=runner
  self.scope=None;self.until=None;self.contract=None;self.owned_helpers={}
 def bind(self,proposal,scope,until):
  self.contract=validate(proposal);require(self.contract['host']==self.host,'COLD_DAEMON_OR_NAMESPACE_CHANGED')
  self.scope,self.until=scope,until
 def mutation(self,row=None,spec=None):
  require(self.contract is not None and self.scope is not None and self.scope.active and type(self.scope.descriptor) is int,
   'COLD_LIVE_SCOPE_REQUIRED');os.fstat(self.scope.descriptor);self.scope.checkpoint()
  require(time.monotonic()<self.until,'COLD_OPERATION_DEADLINE')
  if row is not None:
   require(row['service'] in self.contract['services'] and row==self.contract['services'][row['service']],'COLD_SERVICE_SCOPE_CHANGED')
  if spec is not None:
   role=spec['config']['role'];require(role in self.contract['stores'] and recipe(self.contract,role,spec['config']['id'],
    spec['config']['seconds'],spec['probe'])==spec,'COLD_READER_RECIPE_CHANGED')
 def args(self,args):return [self.binary,'--host',self.host['endpoint'],*args]
 def call(self,args,seconds=20):
  env={'PATH':os.defpath,'LC_ALL':'C','HOME':'/nonexistent'}
  if self.until is not None:seconds=min(seconds,max(.01,self.until-time.monotonic()))
  if self.runner is not None:return self.runner(self.args(args),env,seconds)
  process=subprocess.Popen(self.args(args),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
   env=env,start_new_session=True,pass_fds=(self.scope.descriptor,) if self.scope else ())
  try:
   try:out,err=process.communicate(timeout=seconds)
   except subprocess.TimeoutExpired:raise a.Failure('COLD_DOCKER_OUTCOME_UNKNOWN') from None
   require(len(out)<=1024**2 and len(err)<=32768,'COLD_DOCKER_RESPONSE_LIMIT');return process.returncode,out,err
  finally:
   if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
   for pipe in (process.stdout,process.stderr):pipe.close()
 def query(self,args):
  rc,out,_=self.call(args);require(rc==0,'COLD_DOCKER_METADATA_FAILED');return json.loads(out)
 def verify_host(self):
  ident,system,security=self.query(['info','--format',HOST_FORMAT])
  require(ident==self.host['daemon_id'] and system=='linux' and
   not any('rootless' in x or 'userns' in x for x in security or []),'COLD_DAEMON_OR_NAMESPACE_CHANGED')
 def inspect(self,expected):
  require(HEX.fullmatch(expected['container_id']),'COLD_CONTAINER_IDENTITY_INVALID')
  values=self.query(['container','inspect','--format',FORMAT,expected['container_id']]);require(len(values)==8,'COLD_DOCKER_METADATA_INVALID')
  ident,image,labels,mounts,state,health,userns,restart=values;labels=labels or {}
  return {'container_id':ident,'image_id':image,'owner':labels.get(expected['owner_label']),
   'owner_label':expected['owner_label'],'project':labels.get('com.docker.compose.project'),
   'service':labels.get('com.docker.compose.service'),'mounts':mounts,'state':state,'health':health,'userns':userns,'restart':restart}
 def running(self):
  rc,out,_=self.call(['ps','-q','--no-trunc']);require(rc==0,'COLD_DOCKER_METADATA_FAILED');ids=out.decode().split()
  require(len(ids)<=160 and all(HEX.fullmatch(x) for x in ids),'COLD_RUNNING_SCOPE_LIMIT')
  result={}
  for ident in ids:
   value=self.query(['container','inspect','--format','[{{json .Id}},{{json .State.StartedAt}}]',ident]);result[value[0]]=value[1]
  return result
 def stop(self,row,seconds):
  self.mutation(row=row)
  require(fixed(self.inspect(row))==fixed(row),'COLD_SERVICE_IDENTITY_CHANGED')
  rc,_,_=self.call(['container','stop','--timeout',str(seconds),row['container_id']],seconds+5)
  require(rc==0,'COLD_STOP_OUTCOME_UNKNOWN')
 def start(self,row):
  self.mutation(row=row)
  require(fixed(self.inspect(row))==fixed(row),'COLD_SERVICE_IDENTITY_CHANGED')
  rc,_,_=self.call(['container','start',row['container_id']]);require(rc==0,'COLD_START_OUTCOME_UNKNOWN')
 def create(self,spec):
  self.mutation(spec=spec)
  self.verify_host();image,volumes=self.query(['image','inspect','--format',IMAGE_FORMAT,spec['image_id']])
  require(image==spec['image_id'] and not volumes,'COLD_READER_IMAGE_OR_VOLUMES_CHANGED')
  args=['container','create','--name',spec['name'],'--pull','never','--network','none','--read-only','--restart','no',
   '--log-driver','none','--cap-drop','ALL','--security-opt','no-new-privileges=true','--user',f"{spec['uid']}:{spec['gid']}",
   '--ipc','none','--pids-limit','32','--memory','256m','--cpus','1','--no-healthcheck',
   '--label',LABEL+'='+spec['job'],'--label',SPEC_LABEL+'='+spec['spec_sha256'],
   '--mount','type=bind,src='+spec['source']+',dst=/source,readonly,bind-recursive=disabled',
   '--entrypoint',spec['python'],spec['image_id'],'-I','-B','-c',spec['script'],json.dumps(spec['config'],sort_keys=True)]
  rc,out,_=self.call(args);require(rc==0,'COLD_READER_CREATE_OUTCOME_UNKNOWN');ident=out.decode().strip()
  require(HEX.fullmatch(ident),'COLD_READER_ID_INVALID');self.owned_helpers[ident]=deepcopy(spec);return ident
 def helper(self,ident,spec):
  values=self.query(['container','inspect','--format',FORMAT,ident]);cid,image,labels,mounts,state,health,userns,restart=values
  require(cid==ident and image==spec['image_id'] and labels.get(LABEL)==spec['job'] and labels.get(SPEC_LABEL)==spec['spec_sha256']
   and userns in ('','host') and restart=='no' and len(mounts)==1
   and mounts[0]['Type']=='bind' and mounts[0]['Source']==spec['source'] and mounts[0]['Destination']=='/source'
   and mounts[0]['RW'] is False,'COLD_READER_BINDING_CHANGED')
  controls=self.query(['container','inspect','--format',HELPER_FORMAT,ident])
  require(controls==[f"{spec['uid']}:{spec['gid']}",[spec['python']],['-I','-B','-c',spec['script'],json.dumps(spec['config'],sort_keys=True)],
   False,True,'none',['ALL'],['no-new-privileges=true'],False,'','none','none',32,268435456,1000000000],
   'COLD_READER_SECURITY_CHANGED');return state
 def attach(self,ident,spec):
  self.mutation(spec=spec)
  self.helper(ident,spec)
  return subprocess.Popen(self.args(['container','start','--attach',ident]),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
   stderr=subprocess.PIPE,pass_fds=(self.scope.descriptor,),start_new_session=True,
   env={'PATH':os.defpath,'LC_ALL':'C','HOME':'/nonexistent'})
 def remove(self,ident,spec):
  require(self.owned_helpers.get(ident)==spec and self.scope is not None,'COLD_READER_NOT_THIS_INVOCATION')
  os.fstat(self.scope.descriptor);old=self.until;self.until=max(old,time.monotonic()+15)
  try:
   state=self.helper(ident,spec);require(state['Running'] is False and state['Pid']==0 and state['Status'] in ('exited','created'),
    'COLD_READER_EXIT_UNCONFIRMED')
   rc,_,_=self.call(['container','rm',ident]);require(rc==0,'COLD_READER_REMOVE_UNCONFIRMED')
   rc,out,_=self.call(['ps','-aq','--no-trunc','--filter','label='+LABEL+'='+spec['job']]);require(rc==0 and ident not in out.decode().split(),
    'COLD_READER_ABSENCE_UNCONFIRMED');self.owned_helpers.pop(ident)
  finally:self.until=old
 def abort(self,ident,spec):
  # A pause cancels work but must not prevent disposal of this invocation's
  # proven reader. No saved PID/record adoption; labels, image, mount and command
  # are rechecked before killing/removing only the created helper ID.
  require(self.owned_helpers.get(ident)==spec and self.scope is not None,'COLD_READER_NOT_THIS_INVOCATION')
  os.fstat(self.scope.descriptor);old=self.until;self.until=max(old,time.monotonic()+15)
  try:
   state=self.helper(ident,spec)
   if state['Running']:
    rc,_,_=self.call(['container','kill',ident]);require(rc==0,'COLD_READER_KILL_OUTCOME_UNKNOWN')
   self.remove(ident,spec)
  finally:self.until=old

class Operation:
 """One callback, not stop/copy/start commands in separate invocations.

Use inventory() as the maintenance inventory provider: only already verified
helpers declared in this invocation are excluded from protected inventory.
An interrupted journal is never replayed or automatically recovered.
"""
 def __init__(self,proposal,backend,tool,public_recipients,*,test_codec=False,hook=lambda phase:None):
  self.p=validate(proposal);self.original=deepcopy(proposal);self.backend=backend;self.tool=tool
  self.recipients=a.recipients(public_recipients);self.test_codec=test_codec;self.hook=hook
  require(self.recipients==self.p['recipients'],'COLD_RECIPIENT_REVIEW_CHANGED')
  self.helpers={};self.scope=None;self.until=None;self.folder=None;self.raw=None;self.record=None;self.last_guard=0
 def inventory(self):
  self.backend.verify_host();scope={}
  for role,expected in self.p['services'].items():
   row=self.backend.inspect(expected);require(fixed(row)==fixed(expected),'COLD_SERVICE_IDENTITY_CHANGED')
   scope[role]={'container_id':row['container_id'],'image_id':row['image_id'],'owner':row['owner'],'mounts_sha256':c.fingerprint(row['mounts'])}
  running=self.backend.running()
  for ident,spec in self.helpers.items():self.backend.helper(ident,spec)
  excluded={r['container_id'] for r in self.p['services'].values()}|set(self.helpers)
  protected={k:v for k,v in running.items() if k not in excluded}
  require(protected==self.p['protected'],'COLD_PROTECTED_CONTAINER_CHANGED')
  return {'scope':scope,'protected':protected}
 def guard(self,force=False):
  require(c.fingerprint(self.p)==self.original['review_id'],'COLD_REVIEW_CHANGED')
  require(self.scope is not None and self.scope.active and type(self.scope.descriptor) is int,'COLD_LIVE_SCOPE_REQUIRED')
  os.fstat(self.scope.descriptor);require(time.monotonic()<self.until,'COLD_OPERATION_DEADLINE')
  if force or time.monotonic()-self.last_guard>=.25:
   self.scope.checkpoint();self.inventory();self.last_guard=time.monotonic()
   for row in self.p['stores'].values():
    path=valid_path(row['source']);require(c.identity(path.stat())==row['identity'] and mount_topology(path)==row['mount_topology_sha256'],'COLD_STORAGE_CHANGED')
   require(c.identity(a.private(Path(self.p['destination'])).stat())==self.p['destination_identity'],'COLD_DESTINATION_CHANGED')
  if self.folder is not None:
   require(c.identity(a.private(self.folder).stat())==self.folder_id and a.private(self.folder/'operation.json',False).read_bytes()==self.raw,
    'COLD_OPERATION_JOURNAL_CHANGED')
 def save(self,phase,**extra):
  self.record.update(phase=phase,**extra);self.raw=c.atomic_json(self.folder,'operation.json',self.record,self.raw);self.hook('journal:'+phase)
 def clean_helper(self,ident,spec,abort=False):
  (self.backend.abort if abort else self.backend.remove)(ident,spec)
  self.helpers.pop(ident);self.save(self.record['phase'],helpers={k:v['spec_sha256'] for k,v in self.helpers.items()})
 def launch(self,role,identifier,probe):
  self.guard(True);seconds=max(1,int(self.until-time.monotonic()))
  spec=recipe(self.p,role,identifier,seconds,probe)
  self.save('reader_create_intent',helper_name=spec['name'],helper_spec_sha256=spec['spec_sha256'])
  ident=self.backend.create(spec);self.backend.helper(ident,spec);self.helpers[ident]=spec
  try:
   self.save('reader_created',helpers={k:v['spec_sha256'] for k,v in self.helpers.items()});self.guard(True)
   return ident,spec,self.backend.attach(ident,spec)
  except BaseException:
   # Cancellation after create must dispose only the helper already proven in
   # this invocation. Disposal may run after pause; it never returns services.
   self.clean_helper(ident,spec,True);raise
 def probe(self,role):
  ident,spec,child=self.launch(role,str(uuid.uuid4()),True)
  try:
   out,err=child.communicate(timeout=max(.01,self.until-time.monotonic()));require(child.returncode==0 and not err and len(out)<4096,'COLD_READER_PROBE_FAILED')
   value=json.loads(out);row=self.p['stores'][role]
   require(value['identity']==row['identity'] and value['uid']==row['uid'] and value['gid']==row['gid'] and value['python']>=[3,10],
    'COLD_READER_PROBE_MISMATCH')
   state=self.backend.helper(ident,spec);require(state['Status']=='exited' and state['ExitCode']==0 and not state['OOMKilled'] and state['Pid']==0,
    'COLD_READER_PROBE_EXIT_UNCONFIRMED');self.clean_helper(ident,spec)
  except BaseException:
   a.Age.stop(child);self.clean_helper(ident,spec,True);raise
  finally:
   a.Age.stop(child)
   if child.stderr is not None:child.stderr.close()
 def quiet(self):
  self.guard()
  for role,row in self.p['services'].items():
   state=self.backend.inspect(row)['state'];require(state['Status']=='exited' and state['Running'] is False and state['Pid']==0
    and state['ExitCode']==0 and not state['OOMKilled'],'COLD_ALL_SERVICES_NOT_CLEANLY_STOPPED')
 def seal(self,role):
  require(type(self.tool) is a.Age or self.test_codec is True,'COLD_NATIVE_AGE_REQUIRED')
  self.quiet();identifier=str(uuid.uuid4());store=self.folder/identifier;store.mkdir(mode=0o700)
  raw=c.atomic_json(store,'job.json',{'schema':1,'kind':c.KIND,'id':identifier,'phase':'capturing'})
  partial=store/'payload.age.partial';fd=os.open(partial,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
  child=None;crypto=None;ident=None;spec=None;stderr=bytearray()
  try:
   ident,spec,child=self.launch(role,identifier,False);header=bytearray()
   input_pipe=a.Pipe(child.stdout.fileno(),'read',self.until,self.guard)
   while not header.endswith(b'\n'):
    block=input_pipe.read(1);require(block and len(header)<2048,'COLD_READER_HEADER_INVALID');header.extend(block)
   require(json.loads(header)=={'schema':1,'magic':reader.MAGIC,'id':identifier,'role':role,'identity':self.p['stores'][role]['identity'],
    'uid':self.p['stores'][role]['uid'],'gid':self.p['stores'][role]['gid']},'COLD_READER_HEADER_MISMATCH')
   self.quiet();crypto=self.tool.spawn(['-e',*[x for recipient in self.recipients for x in ('-r',recipient)]],output_fd=fd,
    lease=(self.scope.descriptor,),guard=self.guard)
   writer=a.Pipe(crypto.stdin.fileno(),'write',self.until,self.guard);total=0
   os.set_blocking(child.stderr.fileno(),False)
   limit=self.p['max_bytes']+self.p['max_entries']*8192+c.MAX_MANIFEST+65536
   while True:
    try:part=os.read(child.stderr.fileno(),4096);stderr.extend(part)
    except BlockingIOError:pass
    require(len(stderr)<=4096,'COLD_READER_SUMMARY_LIMIT')
    block=input_pipe.read(65536)
    if not block:break
    total+=len(block);require(total<=limit,'COLD_READER_STREAM_LIMIT');writer.write(block)
   crypto.stdin.close();self.tool.finish(crypto,self.until);crypto=None
   require(child.wait(timeout=max(.01,self.until-time.monotonic()))==0,'COLD_READER_PROCESS_FAILED')
   while True:
    try:block=os.read(child.stderr.fileno(),4096)
    except BlockingIOError:continue
    if not block:break
    stderr.extend(block);require(len(stderr)<=4096,'COLD_READER_SUMMARY_LIMIT')
   summary=json.loads(stderr)
   require(set(summary)=={'schema','magic','id','role','manifest_sha256','entry_count','logical_bytes','source_stable'}
    and summary['schema']==1 and summary['magic']==reader.MAGIC and summary['id']==identifier and summary['role']==role
    and summary['source_stable'] is True and HEX.fullmatch(summary['manifest_sha256'])
    and type(summary['entry_count']) is int and 0<summary['entry_count']<=self.p['max_entries']
    and type(summary['logical_bytes']) is int and 0<=summary['logical_bytes']<=self.p['max_bytes'],'COLD_READER_SUMMARY_INVALID')
   state=self.backend.helper(ident,spec);require(state['Status']=='exited' and state['Running'] is False and state['Pid']==0
    and state['ExitCode']==0 and not state['OOMKilled'],'COLD_READER_EXIT_UNCONFIRMED')
   self.clean_helper(ident,spec);ident=None;self.quiet();self.guard(True);os.fsync(fd);os.rename(partial,store/'payload.age')
   receipt={'schema':1,'kind':c.KIND,'id':identifier,'ciphertext_sha256':a.sha(store/'payload.age'),'ciphertext_bytes':(store/'payload.age').stat().st_size,
    'manifest_sha256':summary['manifest_sha256'],'entry_count':summary['entry_count'],'logical_bytes':summary['logical_bytes'],
    'recipient_fingerprints':[hashlib.sha256(x.encode()).hexdigest() for x in self.recipients],
    'capture_only':True,'restore_verified':False,'encryption':'age_v1' if type(self.tool) is a.Age else 'synthetic_identity_codec'}
   self.guard(True);c.atomic_json(store,'receipt.json',receipt)
   c.atomic_json(store,'job.json',{'schema':1,'kind':c.KIND,'id':identifier,'phase':'captured','receipt_sha256':c.fingerprint(receipt),
    'child_processes_stopped':True},raw)
   return {'role':role,'job_directory':str(store),'receipt':receipt}
  finally:
   try:
    self.tool.stop(crypto);a.Age.stop(child)
    if child is not None and child.stderr is not None:child.stderr.close()
    if ident is not None:self.clean_helper(ident,spec,True)
   finally:os.close(fd)
 def __call__(self,scope):
  require(type(self.tool) is a.Age or self.test_codec is True,'COLD_NATIVE_AGE_REQUIRED')
  require(getattr(scope,'active',False) is True and type(scope.descriptor) is int,'COLD_LIVE_SCOPE_REQUIRED')
  validate(self.original);self.scope=scope;self.until=time.monotonic()+self.p['seconds'];self.backend.bind(self.original,scope,self.until)
  self.guard(True);folder=Path(self.p['destination'])/self.p['id'];require(not folder.exists() and not folder.is_symlink(),'COLD_OPERATION_ALREADY_RECORDED')
  folder.mkdir(mode=0o700);self.folder=folder;self.folder_id=c.identity(folder.stat())
  self.record={'schema':1,'kind':KIND,'id':self.p['id'],'review_id':self.original['review_id'],'phase':'prepared','stopped':[],'started':[],'helpers':{}}
  self.raw=c.atomic_json(folder,'operation.json',self.record)
  try:
   # Prove image Python and UID read access to metadata before any service stop.
   for role in self.p['stores']:self.probe(role)
   self.save('probes_passed')
   for role in self.p['stop_order']:
    self.guard(True);row=self.p['services'][role];current=self.backend.inspect(row)
    require(current['state']['Running'] and current['state']['StartedAt']==row['state']['StartedAt'],'COLD_SERVICE_ALREADY_CHANGED')
    self.save('stop_intent',service=role);seconds=min(self.p['stop_seconds'],max(1,int(self.until-time.monotonic())))
    self.backend.stop(row,seconds);state=self.backend.inspect(row)['state']
    require(state['Status']=='exited' and state['Running'] is False and state['Pid']==0 and state['ExitCode']==0 and not state['OOMKilled']
     and state['FinishedAt']!=row['state']['FinishedAt'],'COLD_SERVICE_NOT_CLEANLY_STOPPED')
    self.record['stopped'].append(role);self.save('service_stopped');self.guard(True)
   self.quiet();captures=[self.seal(role) for role in self.p['stores']]
   self.save('captured',captures=captures);require(not self.helpers,'COLD_READER_EXIT_UNCONFIRMED')
   for role in reversed(self.p['stop_order']):
    self.guard(True);row=self.p['services'][role];state=self.backend.inspect(row)['state']
    require(state['Status']=='exited' and state['Running'] is False and state['ExitCode']==0 and not state['OOMKilled'],
     'COLD_RETURN_STATE_CHANGED')
    self.save('start_intent',service=role);self.backend.start(row)
    while True:
     self.guard(True);current=self.backend.inspect(row)
     require(current['state']['Running'] and current['state']['Pid']>0 and not current['state']['OOMKilled'],'COLD_SERVICE_RETURN_FAILED')
     if current['health'] in ('healthy','none'):break
     require(current['health']=='starting','COLD_SERVICE_RETURN_UNHEALTHY');time.sleep(.05)
    require(current['state']['StartedAt']!=row['state']['StartedAt'],'COLD_SERVICE_RETURN_NOT_FRESH')
    self.record['started'].append(role);self.save('service_returned')
   self.guard(True);require(not self.helpers and len(self.record['started'])==len(self.p['services']),'COLD_RETURN_INCOMPLETE')
   self.save('services_returned',worker_return_required=True,application_restore_verified=False)
   return {'result':'passed','child_processes_stopped':True}
  except BaseException as error:
   try:self.save('attention_required',code=getattr(error,'code','COLD_OPERATION_FAILED'),explicit_recovery_required=True)
   except BaseException:pass
   raise
