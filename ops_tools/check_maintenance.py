#!/usr/bin/env python3
"""Check inactive maintenance in private copies; never stop a production worker."""
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

RELATIVE = Path('ops_candidates/maintenance_20260930')
FIXTURES = {'ops_platform/tests/test_service_lifecycle.py', 'ops_platform/profiles/docker-compose.json'}
AUXILIARY = {str(RELATIVE / 'maintenance.py'), str(RELATIVE / 'tests/test_maintenance.py'),
             'ops_candidates/boot_start_20260929/arming.py'}
CASES = 33


def prepare(root, destination):
    raw = safe_read(root, RELATIVE / 'manifest.json'); spec = json.loads(raw)
    check(spec['schema'] == 1 and spec['activation_supported'] is False
          and spec['production_cli_available'] is False and spec['tests_run'] == CASES, 'MAINTENANCE_MANIFEST_INVALID')
    runtime = code_set(root); rows = spec['base_files']; extra = spec['auxiliary_files']
    check(len(rows) == len(runtime) + len(FIXTURES)
          and {r['path'] for r in rows} == set(runtime) | FIXTURES, 'MAINTENANCE_SOURCE_SET_CHANGED')
    check(len(extra) == len(AUXILIARY) and {r['path'] for r in extra} == AUXILIARY, 'MAINTENANCE_AUXILIARY_SCOPE_CHANGED')
    files = {r['path']: safe_read(root, r['path']) for r in rows + extra}
    check(all(sha(files[r['path']]) == r['sha256'] for r in rows + extra), 'MAINTENANCE_SOURCE_CHANGED')
    before = code_hash({p: files[p] for p in runtime})
    check(before == spec['active_code_sha256'] and len(runtime) == spec['active_code_files'] == 61,
          'MAINTENANCE_ACTIVE_CODE_CHANGED')
    files[str(RELATIVE / 'manifest.json')] = raw
    # All source/scope checks complete before any file is copied.
    for relative, data in files.items():
        target = destination / relative
        missing = []; current = target.parent
        while not current.exists(): missing.append(current); current = current.parent
        for folder in reversed(missing): folder.mkdir(mode=0o700)
        target.write_bytes(data)
    return {'active_code_sha256': before, 'active_code_files': len(runtime), 'copied_source_files': len(files)}


CHILD = r'''
import io,json,os,pathlib,sys,unittest
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root));sys.dont_write_bytecode=True
def guard(event,args):
 if event=='subprocess.Popen' and pathlib.Path(args[0]).resolve()!=pathlib.Path(sys.executable).resolve():
  raise RuntimeError('NON_PYTHON_COMMAND_REFUSED')
 if event=='socket.connect':raise RuntimeError('NETWORK_CONNECTION_REFUSED')
sys.addaudithook(guard)
from ops_candidates.maintenance_20260930.tests.test_maintenance import MaintenanceTests
suite=unittest.defaultTestLoader.loadTestsFromTestCase(MaintenanceTests)
names=[case.id() for case in suite]
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
    result = {'schema': 1, 'stage': 'O04-staged-boot-maintenance-check',
              'scope': 'isolated_private_files_real_leases_worker_processes_synthetic_manager_inventory_readback',
              'python_version': sys.version.split()[0], 'activation_supported': False,
              'production_cli_available': False, 'production_state_read': False,
              'production_credentials_read': False, 'active_worker_code_write_requested': False,
              'production_service_control_requested': False, 'production_worker_stopped': False,
              'docker_commands': 0, 'systemd_control_commands': 0, 'application_api_requests': 0,
              'backup_executed': False, 'restore_executed': False, 'inference_requested': False,
              'linger_changed': False, 'native_docker_systemd_acceptance': False,
              'backup_executor_integration': False, 'actual_reboot_acceptance': 'deferred_by_user'}
    try:
        with tempfile.TemporaryDirectory(prefix='ops-maintenance-check-') as folder:
            private = Path(folder); destination = private / 'project'; result.update(prepare(root, destination))
            for name in ('home', 'tmp'): (private / name).mkdir(mode=0o700)
            env = {'HOME': str(private / 'home'), 'TMPDIR': str(private / 'tmp'), 'PATH': os.defpath,
                   'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}
            proc = subprocess.Popen([sys.executable, '-I', '-B', '-c', CHILD, str(destination)], cwd=private, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try: out, err = proc.communicate(timeout=180)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.communicate(timeout=5); raise Invalid('MAINTENANCE_CHECK_INTERRUPTED') from None
            check(len(out) <= 65536 and len(err) <= 65536 and proc.returncode in (0, 2), 'MAINTENANCE_CHECK_FAILED')
            answer = json.loads(out)
            check(answer.get('result') == ('passed' if proc.returncode == 0 else 'attention_required'), 'MAINTENANCE_RESULT_MISMATCH')
            if proc.returncode == 0:
                check(answer.get('tests_run') == answer.get('passed') == len(answer.get('cases', {})) == CASES,
                      'MAINTENANCE_CASE_SET_INCOMPLETE')
            result.update(answer)
            check(code_hash({p: safe_read(root, p) for p in code_set(root)}) == result['active_code_sha256'],
                  'ACTIVE_CODE_CHANGED_DURING_CHECK')
            result['active_worker_code_preserved'] = True
        result['temporary_directory_cleanup'] = 'passed'
    except (Invalid, OSError, ValueError, KeyError, TypeError) as exc:
        result.update(result='attention_required', code=str(exc) if isinstance(exc, Invalid) else 'MAINTENANCE_CHECK_LOCAL_ERROR')
    return result


def main():
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = run(); print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
