#!/usr/bin/env python3
"""Check inactive initial boot start in private copies; no production activation CLI."""
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

RELATIVE = Path('ops_candidates/boot_start_20260929')
AUXILIARY = {'arming.py', 'tests/test_arming.py'}


def prepare(root, destination):
    from ops_tools import check_boot_transition as transition
    raw_spec = safe_read(root, RELATIVE / 'manifest.json'); spec = json.loads(raw_spec)
    check(spec['schema'] == 1 and spec['activation_supported'] is False
          and spec['production_cli_available'] is False, 'BOOT_START_MANIFEST_INVALID')
    check(len(spec['auxiliary_files']) == 2 and {r['path'] for r in spec['auxiliary_files']} == AUXILIARY,
          'BOOT_START_SCOPE_INVALID')
    tx_raw = safe_read(root, transition.RELATIVE / 'manifest.json'); tx_spec = json.loads(tx_raw)
    check(sha(tx_raw) == spec['transition_manifest_sha256'], 'BOOT_START_TRANSITION_CHANGED')
    check(all(spec[k] == tx_spec[k] for k in ('base_code_sha256', 'candidate_code_sha256')),
          'BOOT_START_HASH_CHANGED')
    extra = {str(RELATIVE / row['path']): safe_read(root, RELATIVE / row['path']) for row in spec['auxiliary_files']}
    check(all(sha(extra[str(RELATIVE / r['path'])]) == r['sha256'] for r in spec['auxiliary_files']),
          'BOOT_START_AUXILIARY_CHANGED')
    # All new auxiliary and dependency hashes pass before the shared copier
    # validates its own full scope and writes any source into the temporary tree.
    result = transition.prepare(root, destination)
    for row in tx_spec['base_files'] + tx_spec['fixtures']:
        source = destination / row['path']; target = destination / 'baseline' / row['path']
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700); target.write_bytes(source.read_bytes())
    for row in tx_spec['changes']:
        target = destination / row['path']; target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes((destination / transition.RELATIVE / 'source' / row['path']).read_bytes())
    extra[str(RELATIVE / 'manifest.json')] = raw_spec
    for rel, raw in extra.items():
        target = destination / rel; target.parent.mkdir(parents=True, exist_ok=True, mode=0o700); target.write_bytes(raw)
    check(code_hash({p: safe_read(destination, p) for p in code_set(destination)}) == result['candidate_code_sha256'],
          'BOOT_START_TEMPORARY_OVERLAY_CHANGED')
    result['copied_source_files'] += len(tx_spec['base_files']) + len(tx_spec['fixtures']) + len(extra)
    return result


CHILD = r'''
import io,json,os,pathlib,sys,unittest
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root));sys.dont_write_bytecode=True
def guard(event,args):
 if event=='subprocess.Popen' and pathlib.Path(args[0]).resolve()!=pathlib.Path(sys.executable).resolve():
  raise RuntimeError('NON_PYTHON_COMMAND_REFUSED')
 if event=='socket.connect':raise RuntimeError('NETWORK_CONNECTION_REFUSED')
sys.addaudithook(guard)
path=root/'ops_candidates/boot_start_20260929/tests'
suite=unittest.defaultTestLoader.discover(str(path),pattern='test_arming.py')
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
    result = {'schema': 1, 'stage': 'O04-staged-initial-boot-start-check',
              'scope': 'isolated_file_transactions_real_host_processes_synthetic_manager_and_docker',
              'python_version': sys.version.split()[0], 'activation_supported': False,
              'initial_boot_start_adapter_implemented': True, 'production_state_read': False,
              'production_credentials_read': False, 'active_worker_code_write_requested': False,
              'production_service_control_requested': False, 'docker_commands': 0,
              'systemd_control_commands': 0, 'application_api_requests': 0, 'inference_requested': False,
              'linger_changed': False, 'production_worker_started': False,
              'native_docker_systemd_acceptance': False, 'logout_or_boot_acceptance': False,
              'initial_boot_arming_implemented': True, 'production_transition_cli_available': False}
    try:
        with tempfile.TemporaryDirectory(prefix='ops-boot-start-check-') as folder:
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
                raise Invalid('BOOT_START_CHECK_INTERRUPTED') from None
            check(len(out) <= 65536 and len(err) <= 65536 and proc.returncode in (0, 2), 'BOOT_START_CHECK_FAILED')
            answer = json.loads(out)
            check(answer.get('result') == ('passed' if proc.returncode == 0 else 'attention_required'), 'BOOT_START_RESULT_MISMATCH')
            if proc.returncode == 0:
                check(answer.get('tests_run') == answer.get('passed') == len(answer.get('cases', {})) == 32,
                      'BOOT_START_CASE_SET_INCOMPLETE')
            result.update(answer)
            source = {p: safe_read(root, p) for p in code_set(root)}
            check(code_hash(source) == result['base_code_sha256'], 'ACTIVE_CODE_CHANGED_DURING_CHECK')
            result['active_worker_code_preserved'] = True
        result['temporary_directory_cleanup'] = 'passed'
    except (Invalid, OSError, ValueError, KeyError, TypeError) as exc:
        result.update(result='attention_required', code=str(exc) if isinstance(exc, Invalid) else 'BOOT_START_CHECK_LOCAL_ERROR')
    return result


def main():
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = run(); print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
