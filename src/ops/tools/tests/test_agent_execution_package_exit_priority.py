"""Only the review-found FAILED + OUTCOME_UNKNOWN exit priority regression."""
import contextlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
sys.dont_write_bytecode = True
p=Path(__file__).with_name('test_agent_execution_package.py')
spec=importlib.util.spec_from_file_location('_package_tests',p)
tests=importlib.util.module_from_spec(spec);spec.loader.exec_module(tests)
EVIDENCE=tests.REPO.parent/'transition-evidence/execution-package-20261017'

class ExitPriority(unittest.TestCase):
    def test_P08_stored_failure_plus_unknown_returns_three_without_collection(self):
        f=tests.Fixture()
        self.assertEqual(f.cli('stage')[0],0);f.root=f.target
        source=EVIDENCE/'fixture-state-preserved'
        source_before={p.name:tests.hash_bytes(p.read_bytes()) for p in source.iterdir()}
        state=f.root/'state';state.mkdir(mode=0o700)
        for p in source.iterdir():
            with (state/p.name).open('xb') as stream:stream.write(p.read_bytes())
            (state/p.name).chmod(0o600)
        manifest,config=f.verified()
        config['fixture_jobs'].reverse()
        self.assertEqual(config['fixture_jobs'][0]['assignment']['fixture_profile'],'nonzero')
        unknown_job=config['fixture_jobs'][1]['assignment']['job_id']
        with contextlib.closing(sqlite3.connect(state/'fixture-ledger.sqlite')) as db:
            db.execute('update fixture_jobs set revision=0,artifact=NULL,artifact_sha256=NULL where job_id=?',(unknown_job,));db.commit()
        f.repin('runtime.json',f.builder.canonical(config));manifest,config=f.verified()
        before=f.dbhashes();admissions=(state/'admissions.json').read_bytes();assignment=(state/'assignment.json').read_bytes()
        with tests.isolated_imports():
            f.launcher.modules(f.root)
            collector=sys.modules['src.ops.tools.github_fixture_collector']
            with tests.mock.patch.object(collector,'collect_fixture_assignment',side_effect=AssertionError('COLLECTOR_MUST_NOT_RUN')) as calls:
                direct=f.launcher.workflow(f.root,config,manifest,tests.UID,execute=True)
                self.assertEqual(calls.call_count,0)
        code,result=f.cli('run')
        self.assertEqual(code,3)
        self.assertEqual([x['status'] for x in result['jobs']],['FAILED','OUTCOME_UNKNOWN'])
        self.assertEqual(result,direct)
        self.assertEqual(f.dbhashes(),before)
        self.assertEqual((state/'admissions.json').read_bytes(),admissions)
        self.assertEqual((state/'assignment.json').read_bytes(),assignment)
        self.assertEqual({p.name:tests.hash_bytes(p.read_bytes()) for p in source.iterdir()},source_before)
        tests.OBSERVATIONS.append({'P08':True,'statuses':['FAILED','OUTCOME_UNKNOWN'],'exit':code,'new_collector_calls':0,'retained_source_state_unchanged':True})

if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2,failfast=True).run(unittest.defaultTestLoader.loadTestsFromTestCase(ExitPriority))
    root=Path(tempfile.mkdtemp(prefix='agent-execution-results-',dir='/tmp'));root.chmod(0o700);p=root/'normal.json'
    with p.open('x') as stream:json.dump({'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'observations':tests.OBSERVATIONS,'native_rpc':0},stream,indent=2)
    p.chmod(0o600);print('RESULT_FILE='+str(p),flush=True)
    raise SystemExit(not result.wasSuccessful())
