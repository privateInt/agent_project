#!/usr/bin/env python3
"""Inactive isolated complete-generation filesystem restore check.

--fetch-test-age explicitly downloads a locked official release to a temporary
directory only. Never installs a package, reads production state/keys, or runs
Docker/systemctl. All tests, temporary identity, binaries and restored synthetic files are
removed after this invocation. No production backup is offered.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from src.ops.tools.check_boot_candidate import check,safe_read,sha,code_set,code_hash

REL=Path('src/ops/candidates/backup_restore_20261001')
FIXTURES={'src/ops/platform/tests/test_service_lifecycle.py','src/ops/platform/profiles/docker-compose.json'}
AUXILIARY={'src/ops/candidates/maintenance_20260930/tests/test_maintenance.py', 'src/ops/candidates/boot_start_20260929/arming.py', 'src/ops/candidates/backup_restore_20261001/tests/test_restore.py', 'src/ops/candidates/cold_storage_20260930/reader.py', 'src/ops/candidates/maintenance_20260930/maintenance.py', 'src/ops/candidates/cold_storage_20260930/tests/test_storage.py', 'src/ops/candidates/backup_generation_20261001/generation.py', 'src/ops/candidates/backup_capture_20260930/tests/test_capture.py', 'src/ops/candidates/backup_capture_20260930/capture.py', 'src/ops/candidates/backup_restore_20261001/image_archive.py', 'src/ops/candidates/backup_restore_20261001/restore.py', 'src/ops/candidates/cold_storage_20260930/storage.py', 'src/ops/candidates/backup_capture_20260930/age_tool.py', 'src/ops/candidates/maintenance_intent_20260930/maintenance.py', 'src/ops/candidates/backup_generation_20261001/tests/test_generation.py'}
CASES=40

def prepare(root,destination):
 raw=safe_read(root,REL/'manifest.json');spec=json.loads(raw);runtime=code_set(root)
 check(spec.get('schema')==1 and spec.get('activation_supported') is False and spec.get('production_cli_available') is False
       and spec.get('tests_run')==CASES,'RESTORE_CHECK_MANIFEST_INVALID')
 rows=spec['base_files'];extra=spec['auxiliary_files']
 check({r['path'] for r in rows}==set(runtime)|FIXTURES and len(rows)==len(runtime)+len(FIXTURES),'RESTORE_CHECK_BASE_SCOPE_CHANGED')
 check({r['path'] for r in extra}==AUXILIARY and len(extra)==len(AUXILIARY),'RESTORE_CHECK_AUXILIARY_SCOPE_CHANGED')
 files={r['path']:safe_read(root,r['path']) for r in rows+extra}
 check(all(sha(files[r['path']])==r['sha256'] for r in rows+extra),'RESTORE_CHECK_SOURCE_PIN_CHANGED')
 before=code_hash({p:files[p] for p in runtime})
 check(before==spec['active_code_sha256'] and len(runtime)==spec['active_code_files']==61,'RESTORE_CHECK_ACTIVE_SOURCE_CHANGED')
 files[str(REL/'manifest.json')]=raw
 for rel,data in files.items():
  target=destination/rel;missing=[];p=target.parent
  while not p.exists():missing.append(p);p=p.parent
  for p in reversed(missing):p.mkdir(mode=0o700)
  fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'wb') as f:f.write(data)
 return {'active_code_sha256':before,'active_code_files':len(runtime),'copied_source_files':len(files)}

CHILD=r'''
import io,json,os,pathlib,signal,sys,unittest
root=pathlib.Path(sys.argv[1]);tools=pathlib.Path(sys.argv[2]);sys.path.insert(0,str(root));sys.dont_write_bytecode=True
allowed={pathlib.Path(sys.executable).resolve(),tools/'age',tools/'age-keygen'}
def audit(event,args):
 if event=='socket.connect':raise RuntimeError('CHECK_NETWORK_REFUSED')
 if event=='subprocess.Popen' and pathlib.Path(args[0]).resolve() not in allowed:raise RuntimeError('CHECK_COMMAND_REFUSED')
sys.addaudithook(audit)
def stop(*args):raise KeyboardInterrupt()
signal.signal(signal.SIGTERM,stop)
from src.ops.candidates.backup_restore_20261001.tests.test_restore import suite as generation_suite
suite=generation_suite()
names=[case.id() for case in suite]
result=unittest.TextTestRunner(stream=io.StringIO(),verbosity=0).run(suite)
case_id=lambda case:getattr(case,'test_case',case).id()
failed={case_id(c) for c,_ in result.failures+result.errors};skipped={case_id(c) for c,_ in result.skipped}
answer={'result':'passed' if result.wasSuccessful() and not skipped else 'attention_required','tests_run':result.testsRun,
 'passed':len(names)-len(failed)-len(skipped),
 'cases':{n:'failed' if n in failed else 'unavailable' if n in skipped else 'passed' for n in names},
 'failures':[{'case':case_id(c),'detail':m.splitlines()[-1][:200]} for c,m in result.failures+result.errors],
 'unavailable':[{'case':case_id(c),'reason':why} for c,why in result.skipped]}
print(json.dumps(answer));sys.exit(0 if answer['result']=='passed' else 2)
'''

def run(root=ROOT,fetch_test_age=False):
 result={'schema':1,'stage':'O04-staged-complete-generation-restore-check',
  'scope':'synthetic_generation_native_age_actor_uid_filesystem_classic_and_OCI_archive_validation',
  'python_version':sys.version.split()[0],'activation_supported':False,'production_cli_available':False,
  'production_state_read':False,'production_credentials_read':False,'production_service_control_requested':False,
  'production_worker_stopped':False,'active_worker_code_write_requested':False,'docker_commands':0,'systemd_control_commands':0,
  'application_api_requests':0,'production_backup_executed':False,'production_restore_executed':False,
  'synthetic_generation_capture_requested':True,'filesystem_restore_requested':True,'temporary_test_identity_generated':False,
  'official_test_tool_download_requested':fetch_test_age,'system_package_install_requested':False,
  'application_restore_acceptance':False,'foreign_storage_uid_acceptance':False,'host_actor_uid_stream_acceptance':False,
  'native_docker_systemd_stopped_storage_acceptance':False,'image_archive_structure_validation':False,'image_import_restore_acceptance':False,'native_Docker_image_export_acceptance':False,'declared_generation_callback_integration_checked':False,
  'inactive_callback_integration_checked':False,'production_backup_executor_integration':False,'backup_ready':False,'inference_requested':False,'linger_changed':False,
  'actual_reboot_acceptance':'deferred_by_user','kubernetes_acceptance':'deferred'}
 try:
  check(fetch_test_age,'AGE_TEST_TOOLS_REQUIRED_USE_FETCH_TEST_AGE')
  with tempfile.TemporaryDirectory(prefix='ops-restore-check-') as folder:
   private=Path(folder);destination=private/'project';result.update(prepare(root,destination))
   for name in ('tools','home','tmp'):(private/name).mkdir(mode=0o700)
   path=destination/'src/ops/candidates/backup_capture_20260930/age_tool.py'
   spec=importlib.util.spec_from_file_location('_capture_check_tools',path);tools=importlib.util.module_from_spec(spec);spec.loader.exec_module(tools)
   print('[restore-check] fetch pinned official age into temporary tools; no system install',flush=True)
   result['test_tool']=tools.fetch(private/'tools')
   env={'HOME':str(private/'home'),'TMPDIR':str(private/'tmp'),'PATH':os.defpath,'LC_ALL':'C',
    'PYTHONDONTWRITEBYTECODE':'1','OPS_CAPTURE_TEST_AGE_DIR':str(private/'tools')}
   print('[restore-check] fresh isolated filesystem restore, authenticated EOF, metadata and image archive checks',flush=True)
   process=subprocess.Popen([sys.executable,'-I','-B','-c',CHILD,str(destination),str(private/'tools')],cwd=private,
    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True,env=env)
   try:out,_=process.communicate(timeout=180)
   except subprocess.TimeoutExpired:
    os.killpg(process.pid,signal.SIGTERM)
    try:process.communicate(timeout=10)
    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.communicate(timeout=5)
    raise RuntimeError('RESTORE_CHECK_CHECK_CHILD_TIMEOUT') from None
   check(len(out)<=262144,'RESTORE_CHECK_CHECK_REPORT_LIMIT');answer=json.loads(out)
   check(answer['tests_run']==len(answer['cases'])==CASES,'RESTORE_CHECK_CHECK_CASE_COUNT_CHANGED')
   result.update(answer);result['temporary_test_identity_generated']=True
   if answer['result']=='passed':
    result['host_actor_uid_stream_acceptance']=True;result['inactive_callback_integration_checked']=True;result['declared_generation_callback_integration_checked']=True;result['actor_uid_filesystem_restore_acceptance']=True;result['image_archive_structure_validation']=True
  result['temporary_directory_cleanup']='passed'
  current={p:safe_read(root,p) for p in code_set(root)}
  check(code_hash(current)==result['active_code_sha256'],'RESTORE_CHECK_ACTIVE_SOURCE_CHANGED')
  result['active_worker_code_preserved']=True;return result
 except Exception as exc:
  code=getattr(exc,'code',None) or (str(exc) if str(exc).startswith(('RESTORE_CHECK_','AGE_')) else 'RESTORE_CHECK_CHECK_FAILED')
  result.update(result='attention_required',code=code[:120]);return result

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--fetch-test-age',action='store_true')
 args=parser.parse_args();result=run(fetch_test_age=args.fetch_test_age)
 print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['result']=='passed' else 2

if __name__=='__main__':raise SystemExit(main())
