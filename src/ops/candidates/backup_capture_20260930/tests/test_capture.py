"""Private files and actual child pipes/leases; fake codec explicitly unencrypted.

Native age tests are separate and mandatory in the host checker with fetch.
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

from src.ops.candidates.backup_capture_20260930 import capture as c, age_tool as a

TEST_RECIPIENT='age1'+a.CHARSET[0]*58

def bech32():
 # Fixed test public syntax; native key generation is used in actual age tests.
 data=[0]*52;chk=1
 for digit in [3,3,3,0,1,7,5,*data,*([0]*6)]:
  high=chk>>25;chk=((chk&0x1ffffff)<<5)^digit
  for bit,p in enumerate((0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3)):
   if high&(1<<bit):chk^=p
 chk^=1;return 'age1'+''.join(a.CHARSET[x] for x in data+[(chk>>(5*(5-i)))&31 for i in range(6)])

class Scope:
 def __init__(self,path):
  self.descriptor=os.open(path,os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(self.descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB);self.active=True
 def checkpoint(self):
  a.require(self.active,'CAPTURE_TEST_PAUSED');os.fstat(self.descriptor)
 def close(self):
  self.active=False
  if self.descriptor>=0:os.close(self.descriptor);self.descriptor=-1

class Codec:
 """Test-only identity pipe. Never used as encryption by the product core."""
 mode='copy'
 spawn_count=0
 def spawn(self,args,*,input_fd=None,output_fd=None,lease=(),guard=lambda:None):
  guard();self.spawn_count+=1
  code="import sys,os,time\n"
  if self.mode=='hang':code+='time.sleep(30)\n'
  elif self.mode=='fail':code+='sys.exit(3)\n'
  else:code+='while True:\n b=os.read(0,65536)\n if not b:break\n v=memoryview(b)\n while v:v=v[os.write(1,v):]\n'
  return subprocess.Popen([sys.executable,'-I','-B','-c',code],stdin=input_fd if input_fd is not None else subprocess.PIPE,
    stdout=output_fd if output_fd is not None else subprocess.PIPE,stderr=subprocess.DEVNULL,pass_fds=lease,start_new_session=True,
    env={'PATH':os.defpath,'LC_ALL':'C'})
 finish=staticmethod(a.Age.finish);stop=staticmethod(a.Age.stop)

class Files(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.src=self.root/'source';self.out=self.root/'out';self.restore=self.root/'restore';self.keys=self.root/'keys'
  for p in (self.src,self.out,self.restore,self.keys):p.mkdir(mode=0o700)
  (self.src/'nested').mkdir(mode=0o700);(self.src/'empty').mkdir(mode=0o750)
  (self.src/'nested'/'record').write_bytes(b'synthetic-content\x00\n'*8000);(self.src/'nested'/'record').chmod(0o640)
  (self.src/'zero').write_bytes(b'');(self.src/'zero').chmod(0o600)
  self.identity=self.keys/'identity';self.identity.write_text('AGE-SECRET-KEY-1'+'Q'*58+'\n');self.identity.chmod(0o600)
  self.scope=Scope(self.root/'lease');self.addCleanup(self.scope.close);self.codec=Codec()
  self.quiet={'schema':1,'identity_sha256':'a'*64,'all_stopped':True}
  self.proposal=self.plan()
 def plan(self,**kwargs):return c.plan({'store':self.src},self.out,[bech32()],'a'*64,**kwargs)
 def capture(self,**kwargs):return c.capture(self.scope,self.proposal,lambda:deepcopy(self.quiet),self.codec,test_codec=True,**kwargs)
 def verify(self,result,**kwargs):return c.restore(result['job_directory'],result['receipt'],self.restore,self.identity,self.codec,test_codec=True,**kwargs)
 def seal_test_receipt(self,result):
  job=Path(result['job_directory']);(job/'receipt.json').write_text(json.dumps(result['receipt']))
  record=json.loads((job/'job.json').read_text());record['receipt_sha256']=c.fingerprint(result['receipt']);(job/'job.json').write_text(json.dumps(record))
 def malformed(self,members,manifest=None):
  result=self.capture();job=Path(result['job_directory']);manifest=manifest or {'schema':1,'kind':c.KIND,'id':result['receipt']['id'],'entries':[]}
  raw=json.dumps(manifest,separators=(',',':')).encode();buffer=io.BytesIO()
  with tarfile.open(fileobj=buffer,mode='w') as archive:
   for member,data in members:archive.addfile(member,io.BytesIO(data) if data is not None else None)
   m=tarfile.TarInfo('manifest.json');m.size=len(raw);archive.addfile(m,io.BytesIO(raw))
  data=buffer.getvalue();(job/'payload.age').write_bytes(data)
  receipt=result['receipt'];receipt.update(ciphertext_bytes=len(data),ciphertext_sha256=hashlib.sha256(data).hexdigest(),manifest_sha256=hashlib.sha256(raw).hexdigest())
  self.seal_test_receipt(result);return result

class CaptureTests(Files):
 def test_plan_is_readonly_and_does_not_read_source_contents(self):
  before={str(p):(p.stat().st_mtime_ns,p.stat().st_size) for p in self.root.rglob('*') if p.is_file()}
  with patch.object(Path,'read_bytes',side_effect=AssertionError('contents')):self.plan()
  self.assertEqual(before,{str(p):(p.stat().st_mtime_ns,p.stat().st_size) for p in self.root.rglob('*') if p.is_file()})
  self.assertEqual(list(self.out.iterdir()),[])
 def test_modified_review_refuses_before_directory_or_process(self):
  self.proposal['max_bytes']+=1
  with self.assertRaisesRegex(a.Failure,'CAPTURE_REVIEW_CHANGED'):self.capture()
  self.assertEqual(list(self.out.iterdir()),[]);self.assertEqual(self.codec.spawn_count,0)
 def test_source_root_inode_change_refuses_before_write(self):
  self.src.rename(self.root/'old');self.src.mkdir(mode=0o700)
  with self.assertRaisesRegex(a.Failure,'CAPTURE_PREPARED_METADATA_CHANGED'):self.capture()
  self.assertEqual(list(self.out.iterdir()),[])
 def test_destination_change_refuses_before_write(self):
  self.out.rename(self.root/'old');self.out.mkdir(mode=0o700)
  with self.assertRaisesRegex(a.Failure,'CAPTURE_PREPARED_METADATA_CHANGED'):self.capture()
 def test_root_overlap_and_destination_inside_source_refused(self):
  for roots,dest in (({'one':self.src,'two':self.src/'empty'},self.out),({'one':self.src},self.src/'empty')):
   with self.subTest(dest=dest),self.assertRaises(a.Failure):c.plan(roots,dest,[bech32()],'a'*64)
 def test_recipient_plugins_identities_duplicates_and_invalid_checksum_refused(self):
  for values in ([bech32(),bech32()],['age1yubikey-secret'],['AGE-SECRET-KEY-1Q'],[TEST_RECIPIENT],[]):
   with self.subTest(values=values),self.assertRaises(a.Failure):a.recipients(values)
 def test_quiescence_and_scope_are_mandatory_before_directory(self):
  self.quiet['all_stopped']=False
  with self.assertRaisesRegex(a.Failure,'CAPTURE_QUIESCENCE_REQUIRED'):self.capture()
  self.quiet['all_stopped']=True;self.scope.active=False
  with self.assertRaisesRegex(a.Failure,'CAPTURE_LIVE_SCOPE_REQUIRED'):self.capture()
  self.assertEqual(list(self.out.iterdir()),[])
 def test_closed_or_real_lease_descriptor_is_not_saved_pid_evidence(self):
  fd=os.dup(self.scope.descriptor);os.close(fd);self.scope.descriptor,old=fd,self.scope.descriptor
  try:
   with self.assertRaises(OSError):self.capture()
  finally:self.scope.descriptor=old
 def test_capture_is_once_and_saved_capture_is_not_restore_acceptance(self):
  result=self.capture();self.assertEqual(result['result'],'captured');self.assertFalse(result['restore_verified'])
  with self.assertRaisesRegex(a.Failure,'CAPTURE_JOB_ALREADY_EXISTS'):self.capture()
  self.assertEqual(self.codec.spawn_count,1)
 def test_no_plaintext_archive_file_or_paths_in_external_receipt(self):
  result=self.capture();job=Path(result['job_directory'])
  self.assertEqual({p.name for p in job.iterdir()},{'payload.age','receipt.json','job.json'})
  self.assertNotIn(str(self.src),json.dumps(result['receipt']));self.assertNotIn('record',json.dumps(result['receipt']))
 def test_symlinks_fifo_and_hardlinks_refused(self):
  for kind in ('symlink','fifo','hardlink'):
   p=self.src/'unsupported'
   if kind=='symlink':p.symlink_to(self.src/'zero')
   elif kind=='fifo':os.mkfifo(p)
   else:os.link(self.src/'zero',p)
   self.proposal=self.plan()
   with self.subTest(kind=kind),self.assertRaises(a.Failure):self.capture()
   p.unlink()
 def test_special_permissions_refused_without_repair(self):
  p=self.src/'zero';p.chmod(0o4600);self.proposal=self.plan()
  with self.assertRaisesRegex(a.Failure,'CAPTURE_SPECIAL_MODE_REFUSED'):self.capture()
  self.assertEqual(p.stat().st_mode&0o7777,0o4600)
 def test_entry_and_logical_byte_limits_reject_before_crypto(self):
  for bounds in ({'max_entries':1},{'max_bytes':1}):
   self.proposal=self.plan(**bounds)
   with self.subTest(bounds=bounds),self.assertRaises(a.Failure):self.capture()
  self.assertEqual(self.codec.spawn_count,0)
 def test_file_changed_during_copy_leaves_only_unaccepted_evidence(self):
  fired=[]
  def change(phase):
   if phase=='entry:file' and not fired:fired.append(1);(self.src/'nested'/'record').write_bytes(b'changed')
  with self.assertRaisesRegex(a.Failure,'CAPTURE_SOURCE_CHANGED'):self.capture(hook=change)
  folder=self.out/self.proposal['id'];self.assertFalse((folder/'receipt.json').exists());self.assertEqual(json.loads((folder/'job.json').read_text())['phase'],'attention_required')
 def test_new_file_at_publish_refuses_before_receipt(self):
  # The quiescence provider must detect scope writes; final catalog detects any drift too.
  def change(phase):
   if phase=='entry:directory' and not (self.src/'late').exists():(self.src/'late').write_bytes(b'x')
  with self.assertRaisesRegex(a.Failure,'CAPTURE_SOURCE_CHANGED'):self.capture(hook=change)
 def test_manual_pause_wins_before_publish_without_receipt(self):
  def pause(phase):
   if phase=='before_publish':self.scope.active=False
  with self.assertRaisesRegex(a.Failure,'CAPTURE_TEST_PAUSED'):self.capture(hook=pause)
  self.assertFalse((self.out/self.proposal['id']/'receipt.json').exists())
 def test_child_exit_failure_preserves_partial_and_no_replay(self):
  self.codec.mode='fail'
  with self.assertRaises(Exception):self.capture()
  with self.assertRaisesRegex(a.Failure,'CAPTURE_JOB_ALREADY_EXISTS'):self.capture()
  self.assertEqual(self.codec.spawn_count,1)
 def test_real_pipe_deadline_kills_child_before_lease_can_be_released(self):
  self.codec.mode='hang';self.proposal=self.plan(seconds=1);before=time.monotonic()
  with self.assertRaisesRegex(a.Failure,'CAPTURE_PROCESS_DEADLINE'):self.capture()
  self.assertLess(time.monotonic()-before,4)
  with self.assertRaises(BlockingIOError):
   fd=os.open(self.root/'lease',os.O_RDWR)
   try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   finally:os.close(fd)
 def test_edited_journal_is_not_overwritten(self):
  folder=self.out/self.proposal['id']
  def edit(phase):
   if phase=='before_publish':(folder/'job.json').write_text('manual-edit')
  with self.assertRaisesRegex(a.Failure,'CAPTURE_JOURNAL_CHANGED'):self.capture(hook=edit)
  self.assertEqual((folder/'job.json').read_text(),'manual-edit')
  self.assertFalse((folder/'receipt.json').exists())
 def test_new_source_file_at_final_publication_is_not_accepted(self):
  def edit(phase):
   if phase=='before_publish':(self.src/'late-file').write_bytes(b'late')
  with self.assertRaisesRegex(a.Failure,'CAPTURE_SOURCE_CHANGED'):self.capture(hook=edit)
 def test_quiescence_identity_change_midstream_cancels_child(self):
  def edit(phase):
   if phase=='entry:file':self.quiet['identity_sha256']='b'*64
  with self.assertRaisesRegex(a.Failure,'CAPTURE_QUIESCENCE_REQUIRED'):self.capture(hook=edit)

class RestoreTests(Files):
 def test_roundtrip_files_empty_directories_hashes_modes_and_ns_times(self):
  original=self.src/'nested'/'record';stamp=1700000000123456789;os.utime(original,ns=(stamp,stamp))
  result=self.capture();verified=self.verify(result);target=Path(verified['directory'])/'data/store'
  self.assertEqual((target/'nested/record').read_bytes(),original.read_bytes());self.assertTrue((target/'empty').is_dir())
  self.assertEqual((target/'nested/record').stat().st_mode&0o777,0o640);self.assertEqual((target/'nested/record').stat().st_mtime_ns,stamp)
  self.assertFalse(verified['crypto_eof_authenticated']);self.assertFalse(verified['application_restore_acceptance'])
 def test_ciphertext_tamper_refuses_before_restore_write(self):
  result=self.capture();p=Path(result['job_directory'])/'payload.age';data=bytearray(p.read_bytes());data[100]^=1;p.write_bytes(data)
  with self.assertRaisesRegex(a.Failure,'RESTORE_CIPHERTEXT_CHANGED'):self.verify(result)
  self.assertEqual(list(self.restore.iterdir()),[])
 def test_untrusted_receipt_is_not_adopted(self):
  result=self.capture();result['receipt']['manifest_sha256']='a'*64
  with self.assertRaisesRegex(a.Failure,'RESTORE_TRUSTED_RECEIPT_REQUIRED'):self.verify(result)
 def test_full_manifest_hash_mismatch_removes_only_new_plaintext_stage(self):
  result=self.capture();job=Path(result['job_directory']);receipt=result['receipt'];receipt['manifest_sha256']='a'*64
  self.seal_test_receipt(result);unrelated=self.restore/'keep';unrelated.write_text('unrelated')
  with self.assertRaisesRegex(a.Failure,'RESTORE_MANIFEST_MISMATCH'):self.verify(result)
  self.assertEqual({p.name for p in self.restore.iterdir()},{'keep'});self.assertEqual(unrelated.read_text(),'unrelated')
 def test_restore_budgets_fail_before_materialization(self):
  result=self.capture()
  for bounds in ({'max_bytes':1},{'max_entries':1}):
   with self.subTest(bounds=bounds),self.assertRaisesRegex(a.Failure,'RESTORE_BUDGET_EXCEEDED'):self.verify(result,**bounds)
  self.assertEqual(list(self.restore.iterdir()),[])
 def test_archive_path_traversal_is_refused(self):
  for name in ('../escape','/absolute','data/store/../escape'):
   member=tarfile.TarInfo(name);member.size=1
   result=self.malformed([(member,b'x')])
   with self.subTest(name=name),self.assertRaises(a.Failure):self.verify(result)
   self.assertFalse((self.root/'escape').exists());self.proposal=self.plan()
 def test_archive_symlink_hardlink_device_and_sparse_refused(self):
  for kind in (tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.CHRTYPE,tarfile.FIFOTYPE):
   member=tarfile.TarInfo('data/store/bad');member.type=kind;member.linkname='escape' if kind in (tarfile.SYMTYPE,tarfile.LNKTYPE) else ''
   result=self.malformed([(member,None)])
   with self.subTest(kind=kind),self.assertRaisesRegex(a.Failure,'RESTORE_MEMBER_TYPE_REFUSED'):self.verify(result)
   self.proposal=self.plan()
 def test_duplicate_member_and_special_mode_rejected(self):
  member=tarfile.TarInfo('data/store');member.type=tarfile.DIRTYPE;member.uid=os.getuid();member.gid=os.getgid()
  result=self.malformed([(member,None),(member,None)])
  with self.assertRaisesRegex(a.Failure,'RESTORE_DUPLICATE_OR_ENTRY_LIMIT'):self.verify(result)
  self.proposal=self.plan();member.mode=0o4700;result=self.malformed([(member,None)])
  with self.assertRaisesRegex(a.Failure,'RESTORE_METADATA_UNSUPPORTED'):self.verify(result)
 def test_different_owner_cannot_trigger_chown(self):
  member=tarfile.TarInfo('data/store');member.type=tarfile.DIRTYPE;member.uid=os.getuid()+1;member.gid=os.getgid()
  result=self.malformed([(member,None)])
  with self.assertRaisesRegex(a.Failure,'RESTORE_METADATA_UNSUPPORTED'):self.verify(result)
 def test_restore_identity_is_explicit_private_native_and_outside_backup(self):
  result=self.capture();self.identity.chmod(0o644)
  with self.assertRaisesRegex(a.Failure,'CAPTURE_PRIVATE_PATH_REQUIRED'):self.verify(result)
  self.identity.chmod(0o600);self.identity.write_text('AGE-PLUGIN-YUBIKEY-secret')
  with self.assertRaisesRegex(a.Failure,'RESTORE_NATIVE_IDENTITY_REQUIRED'):self.verify(result)
 def test_zero_padding_after_manifest_is_consumed_until_eof(self):
  result=self.capture();job=Path(result['job_directory']);p=job/'payload.age';p.write_bytes(p.read_bytes()+b'nonzero-trailer')
  result['receipt'].update(ciphertext_bytes=p.stat().st_size,ciphertext_sha256=a.sha(p));self.seal_test_receipt(result)
  with self.assertRaisesRegex(a.Failure,'RESTORE_TRAILING_DATA'):self.verify(result)
  self.assertEqual(list(self.restore.iterdir()),[])
 def test_receipt_cannot_authorize_unaccepted_capture_marker(self):
  result=self.capture();job=Path(result['job_directory']);record=json.loads((job/'job.json').read_text());record['phase']='capturing';(job/'job.json').write_text(json.dumps(record))
  with self.assertRaisesRegex(a.Failure,'RESTORE_CAPTURE_NOT_ACCEPTED'):self.verify(result)
  self.assertEqual(list(self.restore.iterdir()),[])

class IntegrationTests(unittest.TestCase):
 def test_actual_maintenance_callback_holds_lease_across_capture_and_explicit_return(self):
  from src.ops.candidates.maintenance_intent_20260930.tests.test_intent import MaintenanceTests
  fixture=MaintenanceTests('test_plan_readonly_healthy_boot_history_and_new_scope');fixture.setUp()
  try:
   with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary);src=root/'src';out=root/'out';src.mkdir(mode=0o700);out.mkdir(mode=0o700);(src/'data').write_bytes(b'fixture')
    proposal=c.plan({'store':src},out,[bech32()],'a'*64);captured=[]
    def operation(scope):
     from src.ops.platform.ops import worker_state as ws
     with self.assertRaisesRegex(Exception,'AUTOMATIC_WORKER_BUSY'):
      with ws.automatic_lease(fixture.directory):pass
     captured.append(c.capture(scope,proposal,lambda:{'schema':1,'identity_sha256':'a'*64,'all_stopped':True},Codec(),test_codec=True))
     return {'result':'passed','child_processes_stopped':True}
    result=fixture.controller.run(fixture.plan,operation);self.assertEqual(result['result'],'passed',result)
    self.assertEqual(captured[0]['result'],'captured');fixture.assert_paused()
    self.assertEqual(fixture.controller.resume(result['job_id'])['result'],'passed')
    self.assertEqual((fixture.manager.stops,fixture.manager.starts),(1,1))
  finally:fixture.doCleanups()
 def test_capture_failure_latches_actual_maintenance_and_cannot_auto_return(self):
  from src.ops.candidates.maintenance_intent_20260930.tests.test_intent import MaintenanceTests
  fixture=MaintenanceTests('test_plan_readonly_healthy_boot_history_and_new_scope');fixture.setUp()
  try:
   with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary);src=root/'src';out=root/'out';src.mkdir(mode=0o700);out.mkdir(mode=0o700);os.mkfifo(src/'fifo')
    proposal=c.plan({'store':src},out,[bech32()],'a'*64)
    result=fixture.controller.run(fixture.plan,lambda scope:c.capture(scope,proposal,lambda:{'schema':1,'identity_sha256':'a'*64,'all_stopped':True},Codec(),test_codec=True))
    self.assertEqual(result['result'],'attention_required');fixture.assert_paused();self.assertEqual(fixture.manager.starts,0)
    with self.assertRaisesRegex(Exception,'MAINTENANCE_RETURN_REFUSED'):fixture.controller.resume(result['job_id'])
  finally:fixture.doCleanups()
