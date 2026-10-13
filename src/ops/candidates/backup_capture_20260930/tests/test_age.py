"""Mandatory real age CLI encryption/EOF/decryption cases in private fixtures."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest

from src.ops.candidates.backup_capture_20260930 import capture as c, age_tool as a
from src.ops.candidates.backup_capture_20260930.tests.test_capture import Files

class NativeAgeTests(Files):
 def setUp(self):
  super().setUp()
  value=os.environ.get('OPS_CAPTURE_TEST_AGE_DIR')
  if not value:self.skipTest('ISOLATED_NATIVE_AGE_TOOLS_NOT_PROVIDED')
  self.tools=Path(value);self.codec=a.Age(self.tools/'age',a.sha(self.tools/'age'))
  self.generate(self.identity);self.recipient=self.public(self.identity)
  self.proposal=c.plan({'store':self.src},self.out,[self.recipient],'a'*64)
 def generate(self,path):
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
  try:subprocess.run([str(self.tools/'age-keygen')],stdin=subprocess.DEVNULL,stdout=fd,stderr=subprocess.DEVNULL,check=True,timeout=10,env={'PATH':os.defpath,'LC_ALL':'C','HOME':str(self.keys)})
  finally:os.close(fd)
 def public(self,path):
  p=subprocess.run([str(self.tools/'age-keygen'),'-y',str(path)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True,timeout=10,env={'PATH':os.defpath,'LC_ALL':'C','HOME':str(self.keys)})
  value=p.stdout.decode().strip();a.recipients([value]);return value
 def change_cipher(self,result,data):
  job=Path(result['job_directory']);p=job/'payload.age';p.write_bytes(data)
  result['receipt'].update(ciphertext_bytes=len(data),ciphertext_sha256=hashlib.sha256(data).hexdigest())
  self.seal_test_receipt(result)
 def test_real_public_recipient_roundtrip_verifies_complete_file_set_and_crypto_eof(self):
  result=self.capture();verified=self.verify(result)
  self.assertTrue(verified['crypto_eof_authenticated']);self.assertTrue(verified['file_set_and_hashes_verified'])
  self.assertEqual((Path(verified['directory'])/'data/store/nested/record').read_bytes(),(self.src/'nested/record').read_bytes())
 def test_native_release_version_matches_reviewed_release(self):
  result=subprocess.run([str(self.tools/'age'),'--version'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True,timeout=5,env={'PATH':os.defpath,'LC_ALL':'C'})
  self.assertEqual(result.stdout.decode().strip(),a.VERSION)
 def test_real_age_child_inherits_lease_until_it_exits(self):
  import fcntl,time
  process=self.codec.spawn(['-e','-r',self.recipient],lease=(self.scope.descriptor,))
  try:
   self.scope.close();fd=os.open(self.root/'lease',os.O_RDWR)
   try:
    with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    process.stdin.close();self.codec.finish(process,time.monotonic()+5)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   finally:os.close(fd)
  finally:self.codec.stop(process)
 def test_ciphertext_contains_age_header_without_file_plaintext_or_manifest_names(self):
  result=self.capture();cipher=(Path(result['job_directory'])/'payload.age').read_bytes()
  self.assertTrue(cipher.startswith(b'age-encryption.org/v1\n'));self.assertNotIn(b'synthetic-content',cipher)
  self.assertNotIn(b'data/store/nested/record',cipher);self.assertNotIn(b'manifest.json',cipher)
 def test_wrong_temporary_identity_fails_and_removes_new_plaintext_stage(self):
  result=self.capture();self.generate(self.identity)
  with self.assertRaises(Exception):self.verify(result)
  self.assertEqual(list(self.restore.iterdir()),[])
 def test_truncated_final_crypto_chunk_never_publishes_even_with_matching_cipher_receipt(self):
  result=self.capture();p=Path(result['job_directory'])/'payload.age';self.change_cipher(result,p.read_bytes()[:-16])
  with self.assertRaises(Exception):self.verify(result)
  self.assertEqual(list(self.restore.iterdir()),[])
 def test_modified_final_crypto_tag_never_publishes_even_with_matching_cipher_receipt(self):
  result=self.capture();p=Path(result['job_directory'])/'payload.age';data=bytearray(p.read_bytes());data[-1]^=1;self.change_cipher(result,bytes(data))
  with self.assertRaises(Exception):self.verify(result)
  self.assertEqual(list(self.restore.iterdir()),[])
 def test_appended_ciphertext_garbage_is_not_accepted_as_complete_authentication(self):
  result=self.capture();p=Path(result['job_directory'])/'payload.age';self.change_cipher(result,p.read_bytes()+b'garbage')
  with self.assertRaises(Exception):self.verify(result)
  self.assertEqual(list(self.restore.iterdir()),[])
 def test_ciphertext_randomized_between_two_generations_same_source(self):
  first=self.capture();self.proposal=c.plan({'store':self.src},self.out,[self.recipient],'a'*64);second=self.capture()
  self.assertNotEqual(first['receipt']['ciphertext_sha256'],second['receipt']['ciphertext_sha256'])
  self.verify(first);self.verify(second)
 def test_capture_does_not_open_identity_and_can_restore_with_second_explicit_recipient(self):
  second=self.keys/'second';self.generate(second);other=self.public(second)
  self.proposal=c.plan({'store':self.src},self.out,[self.recipient,other],'a'*64)
  original=Path.read_text
  def reject_identity(path,*args,**kwargs):
   if path in (self.identity,second):raise AssertionError('capture read identity')
   return original(path,*args,**kwargs)
  from unittest.mock import patch
  with patch.object(Path,'read_text',reject_identity):result=self.capture()
  self.identity=second;self.verify(result);self.assertEqual(len(result['receipt']['recipient_fingerprints']),2)
 def test_changed_tool_binary_is_refused_before_spawn(self):
  self.codec.digest='f'*64
  with self.assertRaisesRegex(a.Failure,'AGE_BINARY_CHANGED'):self.capture()
  self.assertFalse((self.out/self.proposal['id']/'receipt.json').exists())

class FetchTests(unittest.TestCase):
 def test_architecture_and_pinned_release_digest_are_explicit(self):
  self.assertEqual(set(a.RELEASES),{'x86_64','aarch64'})
  for arch,size,digest in a.RELEASES.values():
   self.assertIn(arch,('amd64','arm64'));self.assertGreater(size,1);self.assertRegex(digest,'^[0-9a-f]{64}$')
 def test_download_size_and_digest_fail_before_any_binary_install(self):
  from unittest.mock import patch
  import io,tempfile
  class Response(io.BytesIO):
   def geturl(self):return 'https://fixture.invalid/reviewed'
  with tempfile.TemporaryDirectory() as temporary:
   directory=Path(temporary)
   for raw in (b'x',b'x'*20):
    with patch.object(a.platform,'machine',return_value='x86_64'),patch.object(a.urllib.request,'urlopen',return_value=Response(raw)):
     with self.assertRaisesRegex(a.Failure,'AGE_TEST_DOWNLOAD_DIGEST'):a.fetch(directory)
    self.assertEqual(list(directory.iterdir()),[])
 def test_fetch_refuses_nonempty_directory_without_network_or_repair(self):
  import tempfile
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as temporary:
   directory=Path(temporary);(directory/'existing').write_bytes(b'x')
   with patch.object(a.urllib.request,'urlopen',side_effect=AssertionError('network')):
    with self.assertRaisesRegex(a.Failure,'AGE_TEST_DIRECTORY_NOT_EMPTY'):a.fetch(directory)
