#!/usr/bin/env python3
"""Verify a staged boot core in a private temporary copy; never install it."""
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[3]
RELATIVE=Path('src/ops/candidates/boot_lifecycle_20260929')
RUNTIME=('src/ops/platform','src/infra/observability/ops','src/infra/langfuse/ops')
FIXTURES={'src/ops/platform/tests/test_service_lifecycle.py','src/ops/platform/tests/test_worker_process.py',
          'src/ops/platform/profiles/docker-compose.json'}
CHANGES={'src/ops/platform/ops/boot_lifecycle.py','src/ops/platform/ops/refresh_worker.py',
         'src/ops/platform/ops/worker_state.py','src/ops/platform/ops/service_entry.py',
         'src/ops/platform/tests/test_service_lifecycle.py'}
TEST='src/ops/platform/tests/test_boot_lifecycle.py'


class Invalid(Exception):pass


def check(ok,code):
    if not ok:raise Invalid(code)


def safe_read(root,relative):
    path=Path(relative)
    check(not path.is_absolute() and path.parts and '..' not in path.parts,'CANDIDATE_PATH_INVALID')
    current=root
    for part in path.parts:
        current=current/part;check(not current.is_symlink(),'CANDIDATE_SYMLINK_REFUSED')
    fd=os.open(current,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        check(stat.S_ISREG(info.st_mode) and info.st_size<=2*1024*1024,'CANDIDATE_SOURCE_INVALID')
        data=b''
        while len(data)<=2*1024*1024:
            block=os.read(fd,65536)
            if not block:break
            data+=block
        check(len(data)<=2*1024*1024,'CANDIDATE_SOURCE_LIMIT')
        return data
    finally:os.close(fd)


def sha(raw):return hashlib.sha256(raw).hexdigest()


def code_set(root):
    paths=sorted(str(p.relative_to(root)) for folder in RUNTIME for p in (root/folder).rglob('*.py')
                 if 'tests' not in p.relative_to(root).parts)
    check(0<len(paths)<=512,'CANDIDATE_SOURCE_SET_INVALID')
    return paths


def code_hash(files):
    digest=hashlib.sha256()
    for path in sorted(files):digest.update(path.encode()+b'\0'+hashlib.sha256(files[path]).digest())
    return digest.hexdigest()


def prepare(root,destination):
    candidate=root/RELATIVE
    spec=json.loads(safe_read(root,RELATIVE/'manifest.json'))
    check(spec['schema']==1 and spec['activation_supported'] is False,'CANDIDATE_MANIFEST_INVALID')
    runtime=code_set(root);base=spec['base_files'];changes=spec['changes']
    check(len({r['path'] for r in base})==len(base) and {r['path'] for r in base}==set(runtime)|FIXTURES,
          'CANDIDATE_SOURCE_SET_CHANGED')
    check(len(changes)==len(CHANGES) and {r['path'] for r in changes}==CHANGES,'CANDIDATE_SCOPE_INVALID')
    check(spec['test']['path']==TEST,'CANDIDATE_TEST_SCOPE_INVALID')
    files={r['path']:safe_read(root,r['path']) for r in base}
    check(all(sha(files[r['path']])==r['sha256'] for r in base),'CANDIDATE_BASE_CHANGED')
    before=code_hash({p:files[p] for p in runtime})
    check(before==spec['base_code_sha256'],'CANDIDATE_BASE_CODE_CHANGED')
    for row in changes+[spec['test']]:
        old=files.get(row['path'])
        check((sha(old) if old is not None else None)==row['before_sha256'],'CANDIDATE_BEFORE_CHANGED')
        raw=safe_read(candidate,Path('source')/row['path'])
        check(sha(raw)==row['after_sha256'],'CANDIDATE_PAYLOAD_CHANGED');files[row['path']]=raw
    after=code_hash({p:raw for p,raw in files.items() if p.endswith('.py') and 'tests' not in Path(p).parts})
    check(after==spec['candidate_code_sha256'],'CANDIDATE_AFTER_CODE_CHANGED')
    # Validate everything before writing even the private temporary tree.
    for relative,raw in files.items():
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        target.write_bytes(raw)
    return {'base_code_sha256':before,'candidate_code_sha256':after,
            'active_code_files':len(runtime),'candidate_code_files':len(runtime)+1,
            'copied_source_files':len(files)}


CHILD=r'''
import io,json,os,pathlib,sys,unittest
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root));sys.dont_write_bytecode=True
temporary=pathlib.Path(os.environ['TMPDIR']).resolve()
def guard(event,args):
    if event=='subprocess.Popen':
        if pathlib.Path(args[0]).resolve()!=pathlib.Path(sys.executable).resolve():
            raise RuntimeError('NON_PYTHON_COMMAND_REFUSED')
    if event=='socket.connect':
        target=args[1]
        if not isinstance(target,str) or not pathlib.Path(target).is_relative_to(temporary):
            raise RuntimeError('NON_FIXTURE_CONNECTION_REFUSED')
sys.addaudithook(guard)
from src.ops.platform.tests import test_boot_lifecycle as new, test_worker_process as old
classes=[new.CoreTests,new.ReadinessTests,new.PolicyTests,old.ProcessTests]
suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in classes)
names=[c.id() for group in suite for c in group]
result=unittest.TextTestRunner(stream=io.StringIO(),verbosity=0).run(suite)
failed={c.id() for c,_ in result.failures+result.errors};skipped={c.id() for c,_ in result.skipped}
print(json.dumps({'result':'passed' if result.wasSuccessful() else 'attention_required',
 'tests_run':result.testsRun,'passed':result.testsRun-len(failed)-len(skipped),
 'cases':{name:'failed' if name in failed else 'unavailable' if name in skipped else 'passed' for name in names},
 'unavailable':[{'case':c.id(),'reason':why} for c,why in result.skipped],
 'failures':[{'case':c.id(),'detail':msg.splitlines()[-1][:200]} for c,msg in result.failures+result.errors]}))
sys.exit(0 if result.wasSuccessful() else 2)
'''


def run(root=ROOT):
    result={'schema':1,'stage':'O04-staged-boot-core-check','scope':'isolated_candidate_source_synthetic_boot_manager_and_docker',
            'activation_supported':False,'active_worker_code_write_requested':False,'production_state_read':False,
            'production_credentials_read':False,'production_service_control_requested':False,
            'docker_commands':0,'systemd_control_commands':0,'application_api_requests':0,'inference_requested':False,
            'linger_changed':False,'logout_or_boot_acceptance':False,'native_docker_systemd_acceptance':False,
            'policy_transition_implemented':False,'guarded_code_rebind_implemented':False,
            'python_version':sys.version.split()[0]}
    try:
        with tempfile.TemporaryDirectory(prefix='ops-boot-candidate-') as folder:
            private=Path(folder);destination=private/'project'
            result.update(prepare(root,destination))
            for name in ('home','tmp'): (private/name).mkdir(mode=0o700)
            env={'HOME':str(private/'home'),'TMPDIR':str(private/'tmp'),'PATH':os.defpath,'LC_ALL':'C','PYTHONDONTWRITEBYTECODE':'1'}
            proc=subprocess.Popen([sys.executable,'-I','-B','-c',CHILD,str(destination)],cwd=private,env=env,
                                  stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
            try:out,err=proc.communicate(timeout=120)
            except (subprocess.TimeoutExpired,KeyboardInterrupt):
                os.killpg(proc.pid,signal.SIGKILL);proc.communicate(timeout=5)
                # Evaluation descendants enforce <=5 second alarms independently.
                import time
                time.sleep(6)
                raise Invalid('CANDIDATE_CHECK_INTERRUPTED') from None
            check(len(out)<=65536 and len(err)<=65536,'CANDIDATE_OUTPUT_LIMIT')
            check(proc.returncode in (0,2) and out.strip().startswith('{'),'CANDIDATE_CHECK_FAILED')
            result.update(json.loads(out))
            result['native_private_socket_probe']=('unavailable' if any(
                row['case'].endswith('test_native_private_socket_refused_then_ready') for row in result.get('unavailable',[]))
                else result['cases'].get('src.ops.platform.tests.test_boot_lifecycle.ReadinessTests.test_native_private_socket_refused_then_ready'))
            check(result['result']=='passed' if proc.returncode==0 else result['result']=='attention_required','CANDIDATE_RESULT_MISMATCH')
            # Verify the original files after the test. Do not trust child status for this claim.
            original={p:safe_read(root,p) for p in code_set(root)}
            check(code_hash(original)==result['base_code_sha256'],'ACTIVE_CODE_CHANGED_DURING_CHECK')
            result['active_worker_code_preserved']=True
        result['temporary_directory_cleanup']='passed'
    except (Invalid,OSError,ValueError,KeyError,TypeError) as exc:
        result.update(result='attention_required',code=str(exc) if isinstance(exc,Invalid) else 'CANDIDATE_LOCAL_ERROR')
    return result


def main():
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
    result=run();print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['result']=='passed' else 2


if __name__=='__main__':raise SystemExit(main())
