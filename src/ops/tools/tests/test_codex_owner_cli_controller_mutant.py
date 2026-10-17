"""One exact-prompt predicate mutant; expected designated FAIL1/ERROR0."""
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
sys.dont_write_bytecode=True
path=Path(__file__).with_name('test_codex_owner_cli_controller.py')
spec=importlib.util.spec_from_file_location('_owner_cli_tests',path);tests=importlib.util.module_from_spec(spec);spec.loader.exec_module(tests)
REPORT=Path(tempfile.mkdtemp(prefix='agent-owner-cli-results-',dir='/tmp'));REPORT.chmod(0o700)

def save(name,value):
 p=REPORT/name
 with p.open('x') as stream:json.dump(value,stream,indent=2)
 p.chmod(0o600);print('RESULT_FILE='+str(p),flush=True)

class PromptMutant(unittest.TestCase):
 def test_wrong_prompt_cannot_be_a_completed_bound_turn(self):
  source=(tests.ROOT/'src/ops/tools/codex_owner_turns.py').read_text()
  needle="need(content[0].get('text') == prompt, 'PROMPT_MISMATCH')"
  self.assertEqual(source.count(needle),1)
  mutated=source.replace(needle,"need(True, 'PROMPT_MISMATCH')")
  module=types.ModuleType('_semantic_prompt_mutant');exec(compile(mutated,'<one-predicate-prompt-mutant>','exec'),module.__dict__)
  f=tests.Fixture().start();owner=f.owner(turn=f.turn(prompt=f.prompt+' '))
  with mock.patch.object(tests.q,'observe_bound_turn',module.observe_bound_turn):observed=f.observe(owner)
  terminal=f.records/f.pin/'terminal.json'
  witness={'mutation':'bypass-exact-prompt-comparison-only','result':observed,'terminal_record_written':terminal.exists(),'terminal_sha256':tests.q.sha(terminal.read_bytes()) if terminal.exists() else None,'actual_codex_queue':0,'native_rpc':0}
  save('mutant-witness.json',witness)
  if observed.get('status')!='TURN_COMPLETED' or not terminal.exists():raise RuntimeError('MUTANT_PREREQUISITE_NOT_CONFIRMED')
  self.assertNotEqual(observed['status'],'TURN_COMPLETED','MUTANT_ACCEPTED_WRONG_PROMPT_TERMINAL')

if __name__=='__main__':
 with mock.patch.object(socket,'socket',side_effect=AssertionError('LIVE_SOCKET_FORBIDDEN')),mock.patch.object(tests.transport,'_DeadlineSocket',side_effect=AssertionError('LIVE_SOCKET_SUBCLASS_FORBIDDEN')),mock.patch.object(subprocess,'Popen',side_effect=tests.guarded_popen):
  result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PromptMutant))
 detected=result.testsRun==1 and len(result.failures)==1 and not result.errors and 'MUTANT_ACCEPTED_WRONG_PROMPT_TERMINAL' in result.failures[0][1]
 save('mutant-result.json',{'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'designated_assertion_detected':detected})
 raise SystemExit(not result.wasSuccessful())
