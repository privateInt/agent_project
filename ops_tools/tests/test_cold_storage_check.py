import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('_test_cold_check',ROOT/'ops_tools/check_cold_storage.py')
checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)

class CopyTests(unittest.TestCase):
 def test_reviewed_copy_preserves_active_code_and_no_source_is_activated(self):
  with tempfile.TemporaryDirectory() as temporary:
   result=checker.prepare(ROOT,Path(temporary)/'project')
   self.assertEqual(result['active_code_files'],61)
   self.assertEqual(result['active_code_sha256'],'2bf14042a6aa15e4e12b46aa7be494eef2b8834c9b44c4a5587610d5d3a3bb1f')
   dest=Path(temporary)/'project';self.assertEqual((dest/checker.REL/'storage.py').read_bytes(),(ROOT/checker.REL/'storage.py').read_bytes())
 def test_tampered_candidate_fails_before_any_copy(self):
  original=checker.safe_read
  def edit(root,rel):
   data=original(root,rel)
   return data+b'\n# edited' if str(rel).endswith('storage.py') else data
  with tempfile.TemporaryDirectory() as temporary,patch.object(checker,'safe_read',side_effect=edit):
   dest=Path(temporary)/'project'
   with self.assertRaisesRegex(Exception,'COLD_CHECK_SOURCE_PIN_CHANGED'):checker.prepare(ROOT,dest)
   self.assertFalse(dest.exists())
 def test_extra_scope_and_duplicate_manifest_row_fail_before_copy(self):
  original=checker.safe_read
  for duplicate in (False,True):
   def edit(root,rel):
    data=original(root,rel)
    if str(rel)==str(checker.REL/'manifest.json'):
     value=json.loads(data)
     value['auxiliary_files'].append(value['auxiliary_files'][0] if duplicate else {'path':'unknown.py','sha256':'a'*64})
     return json.dumps(value).encode()
    return data
   with self.subTest(duplicate=duplicate),tempfile.TemporaryDirectory() as temporary,patch.object(checker,'safe_read',side_effect=edit):
    dest=Path(temporary)/'project'
    with self.assertRaisesRegex(Exception,'COLD_CHECK_AUXILIARY_SCOPE_CHANGED'):checker.prepare(ROOT,dest)
    self.assertFalse(dest.exists())
 def test_no_fetch_flag_requests_no_network_or_tests(self):
  with patch.object(checker,'prepare',side_effect=AssertionError('copy')):
   result=checker.run(ROOT,False)
  self.assertEqual(result['code'],'AGE_TEST_TOOLS_REQUIRED_USE_FETCH_TEST_AGE');self.assertFalse(result['system_package_install_requested'])
  self.assertFalse(result['production_service_control_requested'])
 def test_prior_active_worker_hash_is_explicit_and_candidate_not_production(self):
  value=json.loads((ROOT/checker.REL/'manifest.json').read_text())
  self.assertFalse(value['activation_supported']);self.assertFalse(value['production_cli_available'])
  self.assertEqual(value['tests_run'],45)
