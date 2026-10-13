"""Synthetic Docker metadata, real reader/codec pipes and leased host files.

The native age cases test the new stream boundary, not a Docker daemon or
foreign storage UID. No production configuration, services or keys are read.
"""
from copy import deepcopy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from ops_candidates.cold_storage_20260930 import storage as s, reader
from ops_candidates.backup_capture_20260930 import capture as c, age_tool as a
from ops_candidates.backup_capture_20260930.tests.test_capture import Scope,Codec,bech32

class Backend:
 def __init__(self,rows,protected):
  self.rows=deepcopy(rows);self.protected=deepcopy(protected);self.helpers={};self.calls=[]
  self.stop_code=0;self.stop_unknown=False;self.start_unknown=False;self.bad_probe=False;self.bad_summary=False
  self.bad_header=False;self.reader_fail=False;self.reader_hang=False;self.bad_helper_exit=False;self.unhealthy=False
  self.remove_fail=False;self.image_changed=False;self.helpers_created=0;self.scope=None;self.until=None
 def bind(self,proposal,scope,until):self.scope,self.until=scope,until
 def verify_host(self):
  if self.image_changed:raise a.Failure('COLD_DAEMON_OR_NAMESPACE_CHANGED')
 def inspect(self,row):return deepcopy(self.rows[row['service']])
 def running(self):
  result={**self.protected,**{r['container_id']:r['state']['StartedAt'] for r in self.rows.values() if r['state']['Running']}}
  for ident,value in self.helpers.items():
   if value.get('child') and value['child'].poll() is None:result[ident]='synthetic-helper-start'
  return result
 def stop(self,row,seconds):
  self.calls.append(('stop',row['service']));state=self.rows[row['service']]['state']
  state.update(Status='exited',Running=False,Pid=0,ExitCode=self.stop_code,FinishedAt='synthetic-finished-'+uuid.uuid4().hex)
  if self.stop_unknown:raise a.Failure('COLD_STOP_OUTCOME_UNKNOWN')
 def start(self,row):
  self.calls.append(('start',row['service']));self.rows[row['service']]['state'].update(Status='running',Running=True,Pid=os.getpid(),StartedAt=uuid.uuid4().hex)
  self.rows[row['service']]['health']='unhealthy' if self.unhealthy else 'healthy'
  if self.start_unknown:raise a.Failure('COLD_START_OUTCOME_UNKNOWN')
 def create(self,spec):
  ident=hashlib.sha256(str(self.helpers_created).encode()).hexdigest();self.helpers_created+=1
  self.helpers[ident]={'spec':deepcopy(spec)};self.calls.append(('create','probe' if spec['probe'] else 'reader'));return ident
 def helper(self,ident,spec):
  value=self.helpers[ident];assert value['spec']==spec;child=value.get('child')
  rc=child.poll() if child is not None else None
  return {'Status':'created' if child is None else 'running' if rc is None else 'exited','Running':child is not None and rc is None,
   'Pid':child.pid if child is not None and rc is None else 0,'ExitCode':rc if rc is not None else 0,'OOMKilled':self.bad_helper_exit}
 def attach(self,ident,spec):
  cfg=deepcopy(spec['config']);cfg['source']=spec['source'];code=spec['script']
  if spec['probe']:
   code=code.replace('Path("/source")','Path('+repr(spec['source'])+')')
   if self.bad_probe:code='print("{}")'
  else:
   if self.reader_fail:code='import sys;sys.exit(2)'
   elif self.reader_hang:code='import time;time.sleep(30)'
   elif self.bad_header:code='print("{}")'
   elif self.bad_summary:code=code.replace("'source_stable':True","'source_stable':False")
  process=subprocess.Popen([sys.executable,'-I','-B','-c',code,json.dumps(cfg)],stdin=subprocess.DEVNULL,
   stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=(self.scope.descriptor,),start_new_session=True,
   env={'PATH':os.defpath,'LC_ALL':'C'})
  self.helpers[ident]['child']=process;return process
 def remove(self,ident,spec):
  if self.remove_fail:raise a.Failure('COLD_READER_REMOVE_UNCONFIRMED')
  self.helper(ident,spec);self.helpers.pop(ident);self.calls.append(('remove',ident))
 def abort(self,ident,spec):
  if ident not in self.helpers:return
  value=self.helpers[ident];a.Age.stop(value.get('child'));self.remove(ident,spec)

class Files(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.src=self.root/'source';self.out=self.root/'out';self.target=self.root/'restores'
  for p in (self.src,self.out,self.target):p.mkdir(mode=0o700)
  (self.src/'empty').mkdir(mode=0o750);(self.src/'record').write_bytes(b'cold-synthetic-file\n'*4000);(self.src/'record').chmod(0o640)
  self.host={'endpoint':'unix:///synthetic/socket','daemon_id':'synthetic-daemon','docker_root':str(self.root/'docker')}
  self.rows={}
  for role,digit in (('database','1'),('frontend','2')):
   self.rows[role]={'container_id':digit*64,'image_id':'sha256:'+'3'*64,'owner':str(uuid.uuid4()),'owner_label':'org.agent-project.synthetic.owner',
    'project':'synthetic-project','service':role,'mounts':([{'Type':'bind','Source':str(self.src),'Destination':'/data','RW':True}] if role=='database' else []),
    'userns':'','restart':'unless-stopped','health':'healthy',
    'state':{'Status':'running','Running':True,'Pid':os.getpid(),'ExitCode':0,'OOMKilled':False,'StartedAt':'original-'+role,'FinishedAt':'old-finished'}}
  self.protected={'f'*64:'protected-original'};self.stores={'data':{'source':str(self.src),'target':'/data','service':'database'}}
  self.helper={'image_id':self.rows['frontend']['image_id'],'python':'python3'}
  self.dependencies={'frontend':['database'],'database':[]};self.recipient=bech32();self.codec=Codec()
  self.proposal=self.plan();self.backend=Backend(self.rows,self.protected)
  self.scope=Scope(self.root/'lease');self.addCleanup(self.scope.close)
  self.addCleanup(self.cleanup_children)
 def cleanup_children(self):
  for value in list(self.backend.helpers.values()):
   process=value.get('child');a.Age.stop(process)
   if process is not None and process.stderr is not None:process.stderr.close()
 def plan(self,**changes):
  return s.plan(self.host,self.rows,self.protected,self.stores,self.helper,self.dependencies,self.out,public_recipients=[self.recipient],**changes)
 def operation(self,**kw):return s.Operation(self.proposal,self.backend,self.codec,[self.recipient],test_codec=True,**kw)
 def run_operation(self,**kw):
  op=self.operation(**kw);self.assertEqual(op(self.scope),{'result':'passed','child_processes_stopped':True});return op
 def record(self):return json.loads((self.out/self.proposal['id']/'operation.json').read_text())

class Plans(Files):
 def test_readonly_plan_reads_metadata_not_file_body_or_keys(self):
  with patch.object(Path,'read_bytes',side_effect=AssertionError('body')):proposal=self.plan()
  self.assertEqual(proposal['stores']['data']['uid'],os.getuid());self.assertEqual(proposal['stores']['data']['gid'],os.getgid())
  self.assertEqual(list(self.out.iterdir()),[])
 def test_metadata_uid_gid_is_not_a_fixed_database_uid(self):
  self.assertEqual(s.config(s.validate(self.proposal),'data',str(uuid.uuid4()),10)['uid'],self.src.stat().st_uid)
 def test_modified_review_refused_before_directory_or_stop(self):
  self.proposal['max_bytes']+=1
  with self.assertRaisesRegex(a.Failure,'COLD_REVIEW_CHANGED'):self.operation()
  self.assertEqual(self.backend.calls,[]);self.assertEqual(list(self.out.iterdir()),[])
 def test_source_inode_change_refused_before_stop(self):
  self.src.rename(self.root/'old');self.src.mkdir(mode=0o700)
  with self.assertRaisesRegex(a.Failure,'COLD_PREPARED_METADATA_CHANGED'):self.operation()
 def test_destination_identity_change_refused_before_stop(self):
  self.out.rename(self.root/'old');self.out.mkdir(mode=0o700)
  with self.assertRaisesRegex(a.Failure,'COLD_PREPARED_METADATA_CHANGED'):self.operation()
 def test_dependency_cycle_and_unknown_roles_refused(self):
  for graph in ({'frontend':['database'],'database':['frontend']},{'frontend':['foreign'],'database':[]}):
   self.dependencies=graph
   with self.assertRaises(a.Failure):self.plan()
 def test_owner_and_mount_mismatch_refused(self):
  for key,value in (('owner','not-uuid'),('mounts',[]),('userns','private')):
   original=deepcopy(self.rows['database']);self.rows['database'][key]=value
   with self.assertRaises(Exception):self.plan()
   self.rows['database']=original
 def test_source_overlap_and_docker_root_refused(self):
  self.host['docker_root']=str(self.src)
  with self.assertRaisesRegex(a.Failure,'COLD_DOCKER_ROOT_REFUSED'):self.plan()
 def test_nested_mount_boundary_refused_even_same_device(self):
  text='1 0 0:1 / '+str(self.src/'empty')+' rw - tmpfs tmpfs rw\n'
  with patch.object(Path,'read_text',return_value=text),self.assertRaisesRegex(a.Failure,'COLD_NESTED_MOUNT_REFUSED'):self.plan()
 def test_public_recipient_change_is_not_adopted(self):
  value=deepcopy(self.proposal);value['recipients']=[]
  with self.assertRaises(a.Failure):s.Operation(value,self.backend,self.codec,[self.recipient],test_codec=True)
  with self.assertRaisesRegex(a.Failure,'CAPTURE_RECIPIENT_REQUIRED'):s.Operation(self.proposal,self.backend,self.codec,[],test_codec=True)
 def test_host_remote_endpoint_and_foreign_helper_image_refused(self):
  self.host['endpoint']='tcp://unreviewed:2375'
  with self.assertRaises(a.Failure):self.plan()
  self.host['endpoint']='unix:///synthetic/socket';self.helper['image_id']='sha256:'+'9'*64
  with self.assertRaisesRegex(a.Failure,'COLD_EXISTING_READER_IMAGE_REQUIRED'):self.plan()
 def test_runtime_script_is_stdlib_and_python310_syntax(self):
  import ast
  script=reader.script();ast.parse(script,feature_version=(3,10))
  self.assertNotIn('from ops_',script);self.assertNotIn('subprocess',script)

class Processes(Files):
 def test_full_callback_orders_stops_and_reverse_returns_without_worker_resume(self):
  self.run_operation();self.assertEqual([x for x in self.backend.calls if x[0] in ('stop','start')],
   [('stop','frontend'),('stop','database'),('start','database'),('start','frontend')])
  self.assertEqual(self.record()['phase'],'services_returned');self.assertTrue(self.record()['worker_return_required']);self.assertEqual(self.backend.helpers,{})
 def test_complete_stream_roundtrip_preserves_metadata_and_trusted_receipt(self):
  self.run_operation();capture=self.record()['captures'][0]
  key=self.root/'identity';key.write_text('AGE-SECRET-KEY-1'+'Q'*58+'\n');key.chmod(0o600)
  answer=c.restore(capture['job_directory'],capture['receipt'],self.target,key,self.codec,test_codec=True)
  restored=Path(answer['directory'])/'data/data/record'
  self.assertEqual(restored.read_bytes(),(self.src/'record').read_bytes());self.assertEqual(restored.stat().st_mode&0o777,0o640)
  self.assertEqual(restored.stat().st_mtime_ns,(self.src/'record').stat().st_mtime_ns)
 def test_reader_probe_failure_before_any_service_stop(self):
  self.backend.bad_probe=True
  with self.assertRaises(Exception):self.operation()(self.scope)
  self.assertFalse(any(x[0]=='stop' for x in self.backend.calls));self.assertEqual(self.backend.helpers,{})
 def test_forced_or_oom_shutdown_never_captures_or_returns(self):
  self.backend.stop_code=137
  with self.assertRaisesRegex(a.Failure,'COLD_SERVICE_NOT_CLEANLY_STOPPED'):self.operation()(self.scope)
  self.assertEqual(self.codec.spawn_count,0);self.assertFalse(any(x[0]=='start' for x in self.backend.calls))
 def test_stop_unknown_is_not_replayed_from_journal(self):
  self.backend.stop_unknown=True
  with self.assertRaisesRegex(a.Failure,'COLD_STOP_OUTCOME_UNKNOWN'):self.operation()(self.scope)
  count=len(self.backend.calls)
  with self.assertRaisesRegex(a.Failure,'COLD_OPERATION_ALREADY_RECORDED'):self.operation()(self.scope)
  self.assertEqual(len(self.backend.calls),count)
 def test_reader_process_failure_keeps_services_stopped_and_pause_audit(self):
  self.backend.reader_fail=True
  with self.assertRaises(Exception):self.operation()(self.scope)
  self.assertEqual(self.record()['phase'],'attention_required');self.assertFalse(any(x[0]=='start' for x in self.backend.calls))
  self.assertEqual(self.backend.helpers,{})
 def test_bad_header_refused_before_encryption(self):
  self.backend.bad_header=True
  with self.assertRaisesRegex(a.Failure,'COLD_READER_HEADER_MISMATCH'):self.operation()(self.scope)
  self.assertEqual(self.codec.spawn_count,0)
 def test_bad_summary_does_not_publish_a_captured_receipt(self):
  self.backend.bad_summary=True
  with self.assertRaisesRegex(a.Failure,'COLD_READER_SUMMARY_INVALID'):self.operation()(self.scope)
  self.assertEqual(list((self.out/self.proposal['id']).glob('*/receipt.json')),[])
 def test_helper_exit_oom_rejected_before_service_stop(self):
  self.backend.bad_helper_exit=True
  with self.assertRaisesRegex(a.Failure,'COLD_READER_PROBE_EXIT_UNCONFIRMED'):self.operation()(self.scope)
  self.assertFalse(any(x[0]=='stop' for x in self.backend.calls))
 def test_parent_pause_during_stream_prevents_service_return(self):
  def pause(phase):
   if phase=='journal:reader_created' and any(x[0]=='stop' for x in self.backend.calls):self.scope.active=False
  with self.assertRaisesRegex(a.Failure,'COLD_LIVE_SCOPE_REQUIRED'):self.operation(hook=pause)(self.scope)
  self.assertFalse(any(x[0]=='start' for x in self.backend.calls))
  self.assertEqual(self.backend.helpers,{})
 def test_unconfirmed_reader_removal_blocks_first_service_stop(self):
  self.backend.remove_fail=True
  with self.assertRaisesRegex(a.Failure,'COLD_READER_REMOVE_UNCONFIRMED'):self.operation()(self.scope)
  self.assertFalse(any(x[0]=='stop' for x in self.backend.calls))
  self.assertEqual(self.record()['phase'],'attention_required')
 def test_protected_container_change_prevents_first_stop(self):
  self.backend.protected['f'*64]='changed'
  with self.assertRaisesRegex(a.Failure,'COLD_PROTECTED_CONTAINER_CHANGED'):self.operation()(self.scope)
  self.assertEqual(self.backend.calls,[])
 def test_foreign_new_running_container_is_not_filtered(self):
  self.backend.protected['e'*64]='new'
  with self.assertRaisesRegex(a.Failure,'COLD_PROTECTED_CONTAINER_CHANGED'):self.operation()(self.scope)
 def test_explicitly_registered_helper_is_the_only_inventory_exception(self):
  op=self.run_operation();self.assertEqual(op.inventory()['protected'],self.protected)
 def test_return_image_change_never_starts_replacement(self):
  def change(phase):
   if phase=='journal:captured':self.backend.rows['database']['image_id']='sha256:'+'9'*64
  with self.assertRaisesRegex(a.Failure,'COLD_SERVICE_IDENTITY_CHANGED'):self.operation(hook=change)(self.scope)
  self.assertFalse(any(x[0]=='start' for x in self.backend.calls))
 def test_unknown_start_is_not_repeated(self):
  self.backend.start_unknown=True
  with self.assertRaisesRegex(a.Failure,'COLD_START_OUTCOME_UNKNOWN'):self.operation()(self.scope)
  self.assertEqual(len([x for x in self.backend.calls if x[0]=='start']),1)
 def test_unhealthy_return_never_claims_callback_success(self):
  self.backend.unhealthy=True
  with self.assertRaisesRegex(a.Failure,'COLD_SERVICE_RETURN_UNHEALTHY'):self.operation()(self.scope)
  self.assertEqual(self.record()['phase'],'attention_required')
 def test_modified_audit_preserved_and_not_overwritten(self):
  def change(phase):
   if phase=='journal:captured':(self.out/self.proposal['id']/'operation.json').write_text('manual-change')
  with self.assertRaisesRegex(a.Failure,'COLD_OPERATION_JOURNAL_CHANGED'):self.operation(hook=change)(self.scope)
  self.assertEqual((self.out/self.proposal['id']/'operation.json').read_text(),'manual-change')
 def test_symlink_source_entry_rejected_and_no_return(self):
  (self.src/'link').symlink_to(self.src/'record')
  with self.assertRaises(Exception):self.operation()(self.scope)
  self.assertFalse(any(x[0]=='start' for x in self.backend.calls))
 def test_closed_scope_descriptor_rejected_before_state_write(self):
  self.scope.close()
  with self.assertRaisesRegex(a.Failure,'COLD_LIVE_SCOPE_REQUIRED'):self.operation()(self.scope)
  self.assertEqual(list(self.out.iterdir()),[])
 def test_reader_deadline_reaps_actual_child_and_retains_unaccepted_partial(self):
  self.proposal=self.plan(seconds=5);self.backend.reader_hang=True;started=time.monotonic()
  with self.assertRaises(a.Failure):self.operation()(self.scope)
  self.assertLess(time.monotonic()-started,9);self.assertEqual(self.backend.helpers,{})
  self.assertEqual(self.record()['phase'],'attention_required')
 def test_actual_reader_child_inherits_real_lease(self):
  op=self.operation();op.scope=self.scope;op.until=time.monotonic()+10;self.backend.bind(self.proposal,self.scope,op.until)
  spec=s.recipe(op.p,'data',str(uuid.uuid4()),10);ident=self.backend.create(spec);child=self.backend.attach(ident,spec)
  try:
   self.scope.close()
   check=subprocess.run([sys.executable,'-I','-B','-c','import fcntl,os,sys\nf=os.open(sys.argv[1],os.O_RDWR)\ntry:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError:sys.exit(7)',str(self.root/'lease')],timeout=3)
   self.assertEqual(check.returncode,7)
  finally:a.Age.stop(child);child.stderr.close();self.backend.helpers.pop(ident)
 def test_proposal_in_memory_edit_is_detected_before_mutation(self):
  op=self.operation();op.p['seconds']+=1
  with self.assertRaisesRegex(a.Failure,'COLD_REVIEW_CHANGED'):op(self.scope)
  self.assertEqual(self.backend.calls,[])

class MaintenanceIntegration(Files):
 def controller(self,op):
  from ops_candidates.maintenance_20260930.tests.test_maintenance import MaintenanceTests
  from ops_candidates.maintenance_intent_20260930 import maintenance
  prior=MaintenanceTests('test_plan_readonly_healthy_boot_history_and_new_scope')
  prior.setUp();self.addCleanup(prior.doCleanups)
  controller=maintenance.Maintenance(prior.directory,prior.manager,inventory_fn=op.inventory,
   snapshot_fn=lambda _:deepcopy(prior.fixture.binding),history_fn=lambda _:prior.history,
   environment_fn=lambda *args:None,readback_fn=prior.readback,clock=time.monotonic,sleep=lambda _:None,
   return_capture_fn=lambda *args:{'synthetic_metadata_only':True},return_check_fn=lambda *args:None)
  return prior,controller
 def test_actual_maintenance_lease_callback_stays_paused_until_explicit_return(self):
  from ops_platform.ops import worker_state as ws
  from ops_platform.contracts import ReadFailure
  op=self.operation();prior,controller=self.controller(op);proposal=controller.plan(wait_seconds=1)
  def leased(scope):
   with self.assertRaisesRegex(ReadFailure,'AUTOMATIC_WORKER_BUSY'):
    with ws.automatic_lease(prior.directory):pass
   return op(scope)
  answer=controller.run(proposal,leased)
  self.assertEqual(answer['result'],'passed',str(answer));prior.assert_paused()
  self.assertEqual((prior.manager.stops,prior.manager.starts),(1,0))
  self.assertEqual(self.record()['phase'],'services_returned')
  returned=controller.resume(answer['job_id'])
  self.assertEqual(returned['result'],'passed',str(returned));self.assertTrue(returned['fresh_evaluation_verified'])
  self.assertEqual((prior.manager.stops,prior.manager.starts),(1,1))
 def test_actual_maintenance_refuses_return_after_unconfirmed_storage_stop(self):
  self.backend.stop_code=137;op=self.operation();prior,controller=self.controller(op)
  answer=controller.run(controller.plan(wait_seconds=1),op)
  self.assertEqual(answer['result'],'attention_required');prior.assert_paused()
  self.assertEqual((prior.manager.stops,prior.manager.starts),(1,0))
  with self.assertRaisesRegex(Exception,'MAINTENANCE_RETURN_REFUSED'):controller.resume(answer['job_id'])
  self.assertFalse(any(x[0]=='start' for x in self.backend.calls))

class DockerContracts(Files):
 def docker(self):
  calls=[]
  def runner(argv,env,seconds):
   calls.append((argv,env,seconds));args=argv[3:]
   if args[:2]==['info','--format']:return 0,json.dumps([self.host['daemon_id'],'linux',[]]).encode(),b''
   if args[:3]==['image','inspect','--format']:return 0,json.dumps([self.helper['image_id'],None]).encode(),b''
   if args[:2]==['container','create']:return 0,('9'*64).encode(),b''
   raise AssertionError('unexpected command')
  backend=s.Docker(self.host,sys.executable,runner);backend.bind(self.proposal,self.scope,time.monotonic()+30)
  return backend,calls
 def test_create_recipe_pins_uid_readonly_network_and_disables_logs_pull(self):
  backend,calls=self.docker();spec=s.recipe(s.validate(self.proposal),'data',str(uuid.uuid4()),10)
  backend.create(spec);argv=calls[-1][0]
  for flag,value in (('--user',f'{os.getuid()}:{os.getgid()}'),('--network','none'),('--pull','never'),('--log-driver','none'),('--cap-drop','ALL')):
   self.assertEqual(argv[argv.index(flag)+1],value)
  self.assertIn('--read-only',argv);self.assertIn('readonly,bind-recursive=disabled',argv[argv.index('--mount')+1])
  self.assertNotIn('--privileged',argv);self.assertNotIn('--env-file',argv);self.assertNotIn('--publish',argv)
 def test_mutation_without_live_bound_scope_is_refused(self):
  backend=s.Docker(self.host,sys.executable,lambda *a: (_ for _ in ()).throw(AssertionError('command')))
  with self.assertRaisesRegex(a.Failure,'COLD_LIVE_SCOPE_REQUIRED'):backend.stop(self.rows['database'],10)
 def test_changed_helper_script_or_source_never_creates(self):
  backend,calls=self.docker();spec=s.recipe(s.validate(self.proposal),'data',str(uuid.uuid4()),10);spec['script']='foreign'
  with self.assertRaisesRegex(a.Failure,'COLD_READER_RECIPE_CHANGED'):backend.create(spec)
  self.assertEqual(calls,[])
 def test_changed_scoped_service_id_never_controls(self):
  backend,calls=self.docker();row=deepcopy(self.rows['database']);row['container_id']='8'*64
  with self.assertRaisesRegex(a.Failure,'COLD_SERVICE_SCOPE_CHANGED'):backend.stop(row,10)
  self.assertEqual(calls,[])
 def test_rootless_userns_or_daemon_change_refused_before_create(self):
  backend,calls=self.docker();backend.runner=lambda *args:(0,json.dumps(['changed','linux',['name=rootless']]).encode(),b'')
  with self.assertRaisesRegex(a.Failure,'COLD_DAEMON_OR_NAMESPACE_CHANGED'):backend.create(s.recipe(s.validate(self.proposal),'data',str(uuid.uuid4()),10))
 def test_foreign_helper_id_cannot_be_killed_or_removed(self):
  backend,calls=self.docker();spec=s.recipe(s.validate(self.proposal),'data',str(uuid.uuid4()),10)
  with self.assertRaisesRegex(a.Failure,'COLD_READER_NOT_THIS_INVOCATION'):backend.abort('8'*64,spec)
  self.assertEqual(calls,[])

@unittest.skipUnless(os.environ.get('OPS_CAPTURE_TEST_AGE_DIR'),'explicit temporary native age tools required')
class NativeStreams(Files):
 def setUp(self):
  super().setUp();directory=Path(os.environ['OPS_CAPTURE_TEST_AGE_DIR'])
  self.age=a.Age(directory/'age',a.sha(directory/'age'));self.key=self.root/'temporary-identity'
  with self.key.open('xb') as file:
   subprocess.run([str(directory/'age-keygen')],stdout=file,stderr=subprocess.DEVNULL,check=True,timeout=10)
  self.key.chmod(0o600)
  self.recipient=subprocess.check_output([str(directory/'age-keygen'),'-y',str(self.key)],timeout=10).decode().strip();self.proposal=self.plan()
 def test_native_reader_to_age_roundtrip_uses_new_stream_boundary(self):
  op=s.Operation(self.proposal,self.backend,self.age,[self.recipient]);self.assertEqual(op(self.scope),{'result':'passed','child_processes_stopped':True})
  capture=self.record()['captures'][0];answer=c.restore(capture['job_directory'],capture['receipt'],self.target,self.key,self.age)
  self.assertTrue(answer['crypto_eof_authenticated']);self.assertEqual((Path(answer['directory'])/'data/data/record').read_bytes(),(self.src/'record').read_bytes())
 def test_native_reader_failure_never_publishes_an_encrypted_success(self):
  self.backend.bad_summary=True
  with self.assertRaisesRegex(a.Failure,'COLD_READER_SUMMARY_INVALID'):s.Operation(self.proposal,self.backend,self.age,[self.recipient])(self.scope)
  self.assertEqual(list((self.out/self.proposal['id']).glob('*/receipt.json')),[])
