"""One semantic mutant, expected FAIL=1 ERROR=0 with successful verifier witness."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.dont_write_bytecode = True
p = Path(__file__).with_name('test_agent_execution_package.py')
spec = importlib.util.spec_from_file_location('_package_tests', p)
tests = importlib.util.module_from_spec(spec); spec.loader.exec_module(tests)

REPORT_ROOT = Path(tempfile.mkdtemp(prefix='agent-execution-results-', dir='/tmp'))
REPORT_ROOT.chmod(0o700)

def save(name, value):
    path = REPORT_ROOT / name
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
    path.chmod(0o600)
    print('RESULT_FILE=' + str(path), flush=True)

class SemanticMutant(unittest.TestCase):
    def test_same_size_immutable_hash_tamper_rejected(self):
        f = tests.Fixture()
        launcher = f.root / 'installation/launch.py'
        old = "need(digest(content) == entry['sha256'], 'PAYLOAD_HASH_MISMATCH')"
        raw = launcher.read_bytes()
        self.assertEqual(raw.count(old.encode()), 1)
        mutated = raw.replace(old.encode(), b"need(True, 'PAYLOAD_HASH_MISMATCH')")
        f.repin('installation/launch.py', mutated)
        target = f.root / 'usage.md'; clean = target.read_bytes(); target.write_bytes(b'X' + clean[1:])
        code, value = f.cli('verify-bundle')
        witness = {'mutation': 'bypass-only-full-manifest-payload-hash', 'tamper_same_size': len(target.read_bytes()) == len(clean),
                   'mutant_process_exit': code, 'mutant_response': value, 'temporary_root': str(f.base), 'native_rpc': 0}
        save('mutant-witness.json', witness)
        if code != 0 or value.get('status') != 'BUNDLE_VERIFIED' or not witness['tamper_same_size']:
            raise RuntimeError('MUTANT_PREREQUISITE_NOT_CONFIRMED')
        self.assertEqual(code, 2, 'MUTANT_ACCEPTED_TAMPERED_PAYLOAD')

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SemanticMutant))
    detected = (result.testsRun == 1 and len(result.failures) == 1 and not result.errors
                and 'MUTANT_ACCEPTED_TAMPERED_PAYLOAD' in result.failures[0][1])
    save('mutant-result.json', {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'designated_assertion_detected': detected})
    raise SystemExit(not result.wasSuccessful())
