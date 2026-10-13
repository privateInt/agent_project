"""Restart safety checks plus real Uvicorn session/cache lifecycle (Docker adapter mocked)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fastapi
import uvicorn

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import manage
from src.ops.platform.ops import restart_check as rc

IDENT = 'a'*64
TOKEN = 'synthetic-agent-token-for-restart'
LF_SECRET = 'synthetic-lf-secret-for-restart'


class Fixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ops-restart-test-')
        self.root = Path(self.temp.name)
        self.directory = self.root/'server'
        token = self.root/'agent-key'; token.write_text(TOKEN); token.chmod(0o600)
        lf = self.root/'lf.json'; lf.write_text(json.dumps({'project_id':'synthetic-project',
            'public_key':'synthetic-public', 'secret_key':LF_SECRET})); lf.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        # Reproduce a legacy installation without invoking first-install discovery.
        self.directory.mkdir(mode=0o700); (self.directory/'cache').mkdir(mode=0o700)
        manage.write_new(self.directory/'access-key', 'synthetic-ops-access-key-for-tests-123456\n')
        cfg={'schema':1,'platforms':{name:{'url':'http://127.0.0.1:'+str(port)} for name,port in
             (('model',13000),('data',14000),('knowledge',15100),('agent',16100))},
             'prometheus_url':'http://127.0.0.1:16310','langfuse_url':'http://127.0.0.1:16320',
             'langfuse_file':str(lf),'public_url':'http://localhost:'+str(self.port),
             'bind':'0.0.0.0','port':self.port,'timeout':5,'tls_cert':None,'tls_key':None}
        cfg['platforms']['agent']['token_file']=str(token)
        for name,value in manage.state_documents(self.directory,cfg,'12345678-1234-4234-8234-123456789abc').items():
            manage.write_new(self.directory/name,value)
        self.cfg = manage.existing(self.directory)
        self.owner = json.loads((self.directory/'owner.json').read_text())
        self.cache = Path(self.cfg['state_file'])
        self.cache.write_text(json.dumps({'schema':1,'success_times':{
            '0'*64:'2026-09-26T13:36:44.123456Z', '1'*64:'2026-09-26T13:36:47.756475Z'}}))
        self.cache.chmod(0o600)
        lock = Path(str(self.cache)+'.lock'); lock.touch(mode=0o600)
        self.api = FakeAPI(self.cfg)
        self.docker = FakeDocker(self)

    def close(self): self.temp.cleanup()

    def call(self, action='run'):
        return rc.run(action, self.directory, docker=self.docker,
                      api_factory=lambda cfg:self.api, wait_seconds=0)

    @property
    def evidence(self): return self.directory/'restart-check/baseline.json'


class FakeAPI:
    def __init__(self, cfg):
        self.cfg=cfg; self.sessions=set(); self.logins=0
        self.fail_login=False; self.fail_logout=False

    def health(self): pass

    def login(self):
        if self.fail_login: raise ReadFailure('OPS_LOGIN_FAILED')
        self.logins+=1
        cookie='synthetic-session-'+str(self.logins)+'-abcdefghijklmnopqrstuvwxyz'
        self.sessions.add(cookie)
        return cookie

    def expired(self, cookie):
        rc.require(cookie not in self.sessions, 'OLD_SESSION_STILL_VALID')

    def logout(self, cookie):
        if self.fail_logout: raise ReadFailure('TEST_SESSION_LOGOUT_FAILED')
        self.sessions.discard(cookie)


class FakeDocker:
    def __init__(self, fixture):
        self.f=fixture; self.restarts=0; self.after_restart=None
        cfg=fixture.cfg; directory=fixture.directory
        mounts=[(str(directory/'config.json'),'/run/ops/config.json',False),
                (str(directory/'access-key'),'/run/ops/access-key',False),
                (cfg['platforms']['agent']['token_file'],'/run/ops/agent-token',False),
                (cfg['langfuse_file'],'/run/ops/langfuse.json',False),
                (str(directory/'cache'),'/var/lib/ops',True)]
        self.raw={'id':IDENT,'image':'sha256:'+'b'*64,'created':'2026-01-01T00:00:00Z',
            'started':'2026-01-01T00:00:01Z','running':True,'health':'healthy',
            'project':rc.PROJECT,'service':rc.SERVICE,'owner':fixture.owner['owner'],
            'user':str(os.getuid())+':'+str(os.getgid()),'network':'host','readonly':True,
            'mounts':[{'Type':'bind','Source':source,'Destination':target,'RW':rw}
                      for source,target,rw in mounts]}

    def discover(self): return IDENT
    def inspect(self, ident):
        assert ident==IDENT
        return deepcopy(self.raw)

    def restart(self, ident):
        assert ident==IDENT
        self.restarts+=1; self.raw['started']=rc.now(); self.f.api.sessions.clear()
        if self.after_restart: self.after_restart()


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture(); self.addCleanup(self.f.close)

    def error(self, code, action='run'):
        with self.assertRaises(ReadFailure) as caught: self.f.call(action)
        self.assertEqual(caught.exception.code,code)

    def test_success_private_evidence_and_no_second_restart(self):
        result=self.f.call()
        self.assertEqual(result['result'],'passed');self.assertEqual(self.f.docker.restarts,1)
        self.assertEqual(result['cache_entries_preserved'],2)
        self.assertEqual(result['upstream_api_requests'],0)
        self.assertFalse(self.f.api.sessions)
        raw=self.f.evidence.read_text();self.assertNotIn(TOKEN,raw);self.assertNotIn(LF_SECRET,raw)
        self.assertNotIn((self.f.directory/'access-key').read_text().strip(),raw)
        self.assertNotIn('old_cookie',raw)
        self.assertEqual(self.f.evidence.stat().st_mode&0o777,0o600)
        self.assertEqual(self.f.evidence.parent.stat().st_mode&0o777,0o700)
        for action in ('run','check'):
            repeat=self.f.call(action)
            self.assertTrue(repeat['evidence_reused']);self.assertFalse(repeat['live_checks_performed'])
        self.assertEqual(self.f.docker.restarts,1);self.assertEqual(self.f.api.logins,2)

    def test_ownership_and_mount_changes_block_restart(self):
        original=deepcopy(self.f.docker.raw)
        mutations=[('owner','not-this-owner'),('project','another-project'),('user','65532:65532'),
                   ('readonly',False),('network','bridge')]
        for key,value in mutations:
            with self.subTest(key=key):
                self.f.docker.raw=deepcopy(original);self.f.docker.raw[key]=value
                with self.assertRaises(ReadFailure):self.f.call()
        self.f.docker.raw=deepcopy(original);self.f.docker.raw['mounts'][0]['RW']=True
        self.error('MOUNT_MISMATCH')
        self.assertEqual(self.f.docker.restarts,0);self.assertEqual(self.f.api.logins,0)

    def test_empty_cache_does_not_restart_or_invent_a_baseline(self):
        self.f.cache.write_text(json.dumps({'schema':1,'success_times':{}}))
        self.error('EMPTY_OR_OVERSIZE_CACHE');self.assertEqual(self.f.docker.restarts,0)
        self.assertFalse(self.f.evidence.exists())

    def test_config_or_credential_change_after_restart_is_failure(self):
        key=Path(self.f.cfg['platforms']['agent']['token_file'])
        self.f.docker.after_restart=lambda:key.write_text(TOKEN+'changed')
        self.error('CONFIG_OR_CREDENTIAL_CHANGED')
        self.assertTrue(self.f.evidence.exists());self.assertNotIn('report',json.loads(self.f.evidence.read_text()))
        self.error('CONFIG_OR_CREDENTIAL_CHANGED','check');self.assertEqual(self.f.docker.restarts,1)

    def test_cache_loss_and_timestamp_advance_are_not_silent_success(self):
        original=self.f.cache.read_text()
        self.f.docker.after_restart=lambda:self.f.cache.write_text(json.dumps({'schema':1,'success_times':{
            '0'*64:'2026-09-27T00:00:00Z'}}))
        self.error('CACHE_CHANGED_OR_CONCURRENT_READ')
        self.assertEqual(json.loads(self.f.evidence.read_text())['cache']['success_times'],json.loads(original)['success_times'])

    def test_old_session_must_actually_be_invalid(self):
        def keep_session():
            record=json.loads(self.f.evidence.read_text());self.f.api.sessions.add(record['old_cookie'])
        self.f.docker.after_restart=keep_session
        self.error('OLD_SESSION_STILL_VALID');self.assertEqual(self.f.docker.restarts,1)

    def test_new_login_and_logout_failures_remain_failures(self):
        self.f.docker.after_restart=lambda:setattr(self.f.api,'fail_login',True)
        self.error('OPS_LOGIN_FAILED')
        self.f.api.fail_login=False;self.f.api.fail_logout=True
        self.error('TEST_SESSION_LOGOUT_FAILED','check');self.assertEqual(self.f.docker.restarts,1)

    def test_lost_restart_response_resumes_without_second_restart(self):
        def timeout():raise ReadFailure('DOCKER_TIMEOUT')
        self.f.docker.after_restart=timeout
        self.error('DOCKER_TIMEOUT')
        self.assertIn('old_cookie',json.loads(self.f.evidence.read_text()))
        result=self.f.call('check')
        self.assertEqual(result['result'],'passed');self.assertFalse(result['restart_command_completion_recorded'])
        self.assertEqual(self.f.docker.restarts,1)

    def test_unobserved_restart_is_not_retried(self):
        def no_restart(ident):
            self.f.docker.restarts+=1;raise ReadFailure('DOCKER_COMMAND_FAILED')
        self.f.docker.restart=no_restart
        self.error('DOCKER_COMMAND_FAILED')
        self.error('OPS_RESTART_NOT_HEALTHY')
        self.assertEqual(self.f.docker.restarts,1)

    def test_expired_session_proof_does_not_claim_restart_cleared_cookie(self):
        def timeout():raise ReadFailure('DOCKER_TIMEOUT')
        self.f.docker.after_restart=timeout;self.error('DOCKER_TIMEOUT')
        data=json.loads(self.f.evidence.read_text())
        data['created_at']=(datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat().replace('+00:00','Z')
        rc.write_record(self.f.evidence,data)
        self.error('SESSION_PROOF_WINDOW_EXPIRED','check');self.assertEqual(self.f.docker.restarts,1)

    def test_no_baseline_check_and_symlink_are_refused(self):
        self.error('NO_RESTART_BASELINE','check')
        folder=self.f.evidence.parent;folder.mkdir(mode=0o700)
        self.f.evidence.symlink_to(self.f.cache)
        self.error('SYMLINK_REFUSED');self.assertEqual(self.f.docker.restarts,0)

    def test_concurrent_run_is_refused(self):
        with rc.evidence_lock(self.f.directory,True):self.error('RESTART_CHECK_BUSY')
        self.assertEqual(self.f.docker.restarts,0)

    def test_docker_adapter_targets_only_one_id_and_redacts_failures(self):
        calls=[]
        def command(args,**kwargs):
            calls.append((args,kwargs))
            if args[1]=='context':out='unix:///var/run/docker.sock'
            elif args[1]=='ps':out=IDENT
            elif args[1]=='inspect':out=json.dumps(self.f.docker.raw)
            else:out=IDENT
            return SimpleNamespace(returncode=0,stdout=out,stderr='')
        with patch.dict(os.environ,{'OPS_SECRET':'do-not-inherit','COMPOSE_FILE':'unrelated'},clear=False),patch.object(rc.subprocess,'run',side_effect=command):
            docker=rc.Docker();ident=docker.discover();docker.inspect(ident);docker.restart(ident)
        self.assertEqual(calls[-1][0],['docker','container','restart','-t','20',IDENT])
        self.assertNotIn('OPS_SECRET',calls[-1][1]['env']);self.assertNotIn('COMPOSE_FILE',calls[-1][1]['env'])
        self.assertNotIn('.Config.Env',rc.INSPECT)
        with patch.dict(os.environ,{'DOCKER_HOST':'tcp://remote:2375'}):
            with self.assertRaises(ReadFailure) as exc:rc.Docker()
        self.assertEqual(exc.exception.code,'LOCAL_DOCKER_REQUIRED')

    def test_real_process_restart_preserves_cache_and_expires_cookie(self):
        root=Path(__file__).resolve().parents[4]
        env=dict(os.environ,OPS_CONFIG=str(self.f.directory/'host-config.json'),PYTHONPATH=os.pathsep.join(sorted({
            str(Path(module.__file__).resolve().parents[1]) for module in (fastapi,uvicorn)})))
        api=rc.API(self.f.cfg);process=None
        with (self.f.root/'process.log').open('w+') as log:
            def stop():
                nonlocal process
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            def start():
                nonlocal process
                process=subprocess.Popen([sys.executable,'-B','-m','src.ops.platform.server'],cwd=root,env=env,stdout=log,stderr=log)
                deadline=time.monotonic()+10
                while process.poll() is None and time.monotonic()<deadline:
                    try:api.health();return
                    except ReadFailure:time.sleep(.03)
                log.flush();log.seek(0);self.fail(log.read()[-3000:])
            def restart(ident):
                self.assertEqual(ident,IDENT);self.f.docker.restarts+=1
                stop();start();self.f.docker.raw['started']=rc.now()
            try:
                start();self.f.docker.restart=restart
                result=rc.run('run',self.f.directory,self.f.docker,wait_seconds=0)
                self.assertEqual(result['result'],'passed')
                self.assertTrue(result['old_session_rejected']);self.assertTrue(result['same_access_code_login'])
                self.assertTrue(result['test_session_logged_out']);self.assertEqual(result['cache_entries_preserved'],2)
                self.assertEqual(self.f.docker.restarts,1)
            finally:stop()


if __name__=='__main__':unittest.main()
