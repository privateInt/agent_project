#!/usr/bin/env python3
"""Check inactive stopped transition code in private copies; no activation CLI."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops_tools.check_boot_candidate import Invalid, check, safe_read, sha, code_set, code_hash

RELATIVE = Path('ops_candidates/boot_transition_20260929')
TARGETS = {'ops_platform/ops/' + name for name in
           ('worker_state.py', 'boot_lifecycle.py', 'refresh_worker.py', 'service_lifecycle.py', 'service_entry.py')}
AUXILIARY = {'transition.py', 'tests/test_transition.py'}
FIXTURES = {'ops_platform/tests/test_service_lifecycle.py', 'ops_platform/profiles/docker-compose.json'}


def prepare(root, destination):
    raw_spec = safe_read(root, RELATIVE / 'manifest.json'); spec = json.loads(raw_spec)
    runtime = code_set(root)
    check(spec['schema'] == 1 and spec['activation_supported'] is False, 'TRANSITION_MANIFEST_INVALID')
    check(len(runtime) == 60 and len(spec['base_files']) == 60
          and {r['path'] for r in spec['base_files']} == set(runtime), 'TRANSITION_SOURCE_SET_CHANGED')
    check(len(spec['changes']) == 5 and {r['path'] for r in spec['changes']} == TARGETS, 'TRANSITION_SCOPE_INVALID')
    check(len(spec['auxiliary_files']) == 2 and {r['path'] for r in spec['auxiliary_files']} == AUXILIARY, 'TRANSITION_SCOPE_INVALID')
    check(len(spec['fixtures']) == 2 and {r['path'] for r in spec['fixtures']} == FIXTURES, 'TRANSITION_SCOPE_INVALID')
    files = {r['path']: safe_read(root, r['path']) for r in spec['base_files'] + spec['fixtures']}
    check(all(sha(files[r['path']]) == r['sha256'] for r in spec['base_files'] + spec['fixtures']), 'TRANSITION_BASE_CHANGED')
    before = code_hash({p: files[p] for p in runtime})
    check(before == spec['base_code_sha256'], 'TRANSITION_BASE_HASH_CHANGED')
    after_files = {p: files[p] for p in runtime}
    for row in spec['changes']:
        prior = files.get(row['path'])
        check((sha(prior) if prior is not None else None) == row['before_sha256'], 'TRANSITION_BEFORE_CHANGED')
        path = RELATIVE / 'source' / row['path']; raw = safe_read(root, path)
        check(sha(raw) == row['after_sha256'], 'TRANSITION_PAYLOAD_CHANGED')
        files[str(path)] = raw; after_files[row['path']] = raw
    after = code_hash(after_files)
    check(after == spec['candidate_code_sha256'], 'TRANSITION_CANDIDATE_HASH_CHANGED')
    for row in spec['auxiliary_files']:
        path = RELATIVE / row['path']; raw = safe_read(root, path)
        check(sha(raw) == row['sha256'], 'TRANSITION_AUXILIARY_CHANGED'); files[str(path)] = raw
    files[str(RELATIVE / 'manifest.json')] = raw_spec
    # Nothing has been copied until all paths, base and payload hashes passed.
    for relative, raw in files.items():
        path = destination / relative; path.parent.mkdir(parents=True, exist_ok=True, mode=0o700); path.write_bytes(raw)
    return {'base_code_sha256': before, 'candidate_code_sha256': after,
            'active_code_files': len(runtime), 'candidate_code_files': len(after_files), 'copied_source_files': len(files)}


CHILD = r'''
import io,json,os,pathlib,sys,unittest
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root));sys.dont_write_bytecode=True
def guard(event,args):
 if event=='subprocess.Popen' and pathlib.Path(args[0]).resolve()!=pathlib.Path(sys.executable).resolve():
  raise RuntimeError('NON_PYTHON_COMMAND_REFUSED')
 if event=='socket.connect':raise RuntimeError('NETWORK_CONNECTION_REFUSED')
sys.addaudithook(guard)
path=root/'ops_candidates/boot_transition_20260929/tests'
suite=unittest.defaultTestLoader.discover(str(path),pattern='test_transition.py')
def flatten(group):
 for item in group:
  if isinstance(item,unittest.TestSuite):yield from flatten(item)
  else:yield item
names=[case.id() for case in flatten(suite)]
result=unittest.TextTestRunner(stream=io.StringIO(),verbosity=0).run(suite)
case_id=lambda case:getattr(case,'test_case',case).id()
failed={case_id(case) for case,_ in result.failures+result.errors};skipped={case_id(case) for case,_ in result.skipped}
answer={'result':'passed' if result.wasSuccessful() and not skipped else 'attention_required',
 'tests_run':result.testsRun,'passed':len(names)-len(failed)-len(skipped),
 'cases':{name:'failed' if name in failed else 'unavailable' if name in skipped else 'passed' for name in names},
 'failures':[{'case':case_id(case),'detail':msg.splitlines()[-1][:240]} for case,msg in result.failures+result.errors],
 'unavailable':[{'case':case_id(case),'reason':why} for case,why in result.skipped]}
print(json.dumps(answer));sys.exit(0 if answer['result']=='passed' else 2)
'''


def run(root=ROOT):
    result = {'schema': 1, 'stage': 'O04-staged-stopped-transition-check',
              'scope': 'isolated_file_transactions_real_host_processes_synthetic_manager_and_docker',
              'python_version': sys.version.split()[0], 'activation_supported': False,
              'transition_commit_state': 'paused_and_disabled', 'production_state_read': False,
              'production_credentials_read': False, 'active_worker_code_write_requested': False,
              'production_service_control_requested': False, 'docker_commands': 0,
              'systemd_control_commands': 0, 'application_api_requests': 0, 'inference_requested': False,
              'linger_changed': False, 'production_worker_started': False,
              'native_docker_systemd_acceptance': False, 'logout_or_boot_acceptance': False,
              'initial_boot_arming_implemented': False, 'production_transition_cli_available': False}
    try:
        with tempfile.TemporaryDirectory(prefix='ops-transition-check-') as folder:
            private = Path(folder); destination = private / 'project'; result.update(prepare(root, destination))
            for name in ('home', 'tmp'): (private / name).mkdir(mode=0o700)
            env = {'HOME': str(private / 'home'), 'TMPDIR': str(private / 'tmp'), 'PATH': os.defpath,
                   'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}
            proc = subprocess.Popen([sys.executable, '-I', '-B', '-c', CHILD, str(destination)], cwd=private, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
            try: out, err = proc.communicate(timeout=180)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.communicate(timeout=5)
                raise Invalid('TRANSITION_CHECK_INTERRUPTED') from None
            check(len(out) <= 65536 and len(err) <= 65536 and proc.returncode in (0, 2), 'TRANSITION_CHECK_FAILED')
            answer = json.loads(out)
            check(answer.get('result') == ('passed' if proc.returncode == 0 else 'attention_required'), 'TRANSITION_RESULT_MISMATCH')
            if proc.returncode == 0:
                check(answer.get('tests_run') == answer.get('passed') == len(answer.get('cases', {})) == 33,
                      'TRANSITION_CASE_SET_INCOMPLETE')
            result.update(answer)
            source = {p: safe_read(root, p) for p in code_set(root)}
            check(code_hash(source) == result['base_code_sha256'], 'ACTIVE_CODE_CHANGED_DURING_CHECK')
            result['active_worker_code_preserved'] = True
        result['temporary_directory_cleanup'] = 'passed'
    except (Invalid, OSError, ValueError, KeyError, TypeError) as exc:
        result.update(result='attention_required', code=str(exc) if isinstance(exc, Invalid) else 'TRANSITION_CHECK_LOCAL_ERROR')
    return result


def main():
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = run(); print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
