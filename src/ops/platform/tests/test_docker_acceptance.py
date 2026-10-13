"""Isolation guards and real HTTP/web lifecycle; native Docker execution remains separate."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fastapi
import uvicorn
from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import docker_acceptance as a, docker_acceptance_fixture as fixture
from src.ops.platform.ops import manage, bootstrap_discovery as bootstrap, discover, activate_discovery as activation
from src.ops.platform.ops import restart_check as rc, set_public_url as change
from src.ops.platform.ops.discovery_compose import ROLES
from src.ops.platform.tests.test_discovery import DockerFixture
from src.ops.platform.tests.test_restart_check import FakeDocker, FakeAPI, Fixture, IDENT


class MetadataEngine:
    endpoint='unix:///var/run/docker.sock'
    def image(self,root): return 'sha256:'+'b'*64
    def snapshot(self,ids=None): return {}
    def protect(self,before): pass


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ops-docker-check-'); self.addCleanup(self.temp.cleanup)
        self.directory,self.record=a.create_run(Path(self.temp.name),MetadataEngine())
    def error(self,code,fn):
        with self.assertRaises(ReadFailure) as caught: fn()
        self.assertEqual(caught.exception.code,code)
    def test_generated_specs_are_private_use_pinned_image_and_no_production_credentials(self):
        self.assertEqual(len(self.record['compose']),8)
        for rel in self.record['compose']:
            p=self.directory/'project'/rel; spec=json.loads(p.read_text())
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            for service in spec['services'].values():
                self.assertEqual(service['image'],self.record['image']); self.assertNotIn('build',service)
                self.assertEqual(service['pull_policy'],'never'); self.assertEqual(service['restart'],'no')
                self.assertEqual(service['labels'][a.LABEL],self.record['owner'])
                for mount in service['volumes']:
                    self.assertTrue(mount['source'].startswith((str(self.directory),'${OPS_')))
                for port in service.get('ports',[]): self.assertEqual(port['host_ip'],'127.0.0.1')
        self.assertNotIn('.secrets',json.dumps(self.record))
        from src.ops.platform.observations import read_lf_credentials
        self.assertTrue(read_lf_credentials(self.directory/'keys/langfuse.json')['project_id'].startswith('synthetic-'))
    def test_copied_worker_imports_without_original_project_or_test_dependencies(self):
        env={k:v for k,v in os.environ.items() if not k.startswith('PYTHON')}
        for script in ('docker_acceptance.py','docker_acceptance_fixture.py'):
            code='import runpy; runpy.run_path('+repr(str(self.directory/'project/src/ops/platform/ops'/script))+',run_name="import_only")'
            result=subprocess.run([sys.executable,'-B','-c',code],cwd=self.directory,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
    def test_compose_tamper_and_symlink_refused_before_control(self):
        p=self.directory/'project/src/platform/model/acceptance-model.json'; original=p.read_bytes()
        p.write_text('{}');self.error('ACCEPTANCE_COMPOSE_CHANGED',lambda:a.validate_run(self.directory))
        p.unlink(); q=self.directory/'foreign.json';q.write_bytes(original);q.chmod(0o600);p.symlink_to(q)
        self.error('SYMLINK_REFUSED',lambda:a.validate_run(self.directory))
    def test_wrong_run_directory_or_owner_refused(self):
        record=deepcopy(self.record);record['directory']='/another/run';a.write(self.directory/'run.json',record)
        self.error('ACCEPTANCE_OWNER_MISMATCH',lambda:a.validate_run(self.directory))
    def test_cleanup_refuses_foreign_owner_without_removing_container(self):
        engine=object.__new__(a.Engine);engine.endpoint=self.record['endpoint']
        engine.ids=lambda *args,**kw:[IDENT]
        engine.metadata=lambda ident:{'acceptance':'foreign','identity':{'image':self.record['image'],'project':self.record['prefix']+'-model'},'files':str(self.directory/'project/src/platform/model/acceptance-model.json')}
        with patch.object(engine,'command') as command:
            self.error('ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED',lambda:engine.cleanup(self.directory))
            command.assert_not_called()
    def test_cleanup_only_removes_verified_container_and_empty_owned_network(self):
        engine=object.__new__(a.Engine);engine.endpoint=self.record['endpoint'];live=[IDENT];network='c'*64;calls=[]
        engine.ids=lambda *args,**kw:list(live)
        engine.metadata=lambda ident:{'acceptance':self.record['owner'],'identity':{'image':self.record['image'],'project':self.record['prefix']+'-model'},'files':str(self.directory/'project/src/platform/model/acceptance-model.json')}
        engine.protect=lambda before:self.assertEqual(before,{})
        def command(args,timeout=30):
            calls.append(args)
            if args[:2]==['container','rm']:live.clear();return ''
            if args[:2]==['network','ls']:return network
            if args[:2]==['network','inspect']:return json.dumps({'owner':self.record['owner'],'containers':{}})
            if args[:2]==['network','rm']:return ''
            self.fail(args)
        engine.command=command
        result=engine.cleanup(self.directory)
        self.assertEqual(result['owned_containers_removed'],1);self.assertEqual(result['owned_networks_removed'],1)
        self.assertFalse(any('prune' in cmd or 'volume' in cmd or 'image' in cmd for cmd in calls))
    def test_cleanup_refuses_network_with_unowned_attached_container(self):
        engine=object.__new__(a.Engine);engine.endpoint=self.record['endpoint'];engine.ids=lambda *args,**kw:[]
        calls=[]
        def command(args,timeout=30):
            calls.append(args)
            if args[:2]==['network','ls']:return 'c'*64
            return json.dumps({'owner':self.record['owner'],'containers':{'foreign':{}}})
        engine.command=command
        self.error('ACCEPTANCE_NETWORK_IN_USE',lambda:engine.cleanup(self.directory))
        self.assertFalse(any(cmd[:2]==['network','rm'] for cmd in calls))
    def test_protected_container_change_is_failure(self):
        engine=object.__new__(a.Engine);engine.snapshot=lambda ids:{IDENT:{'started':'changed'}}
        self.error('ACCEPTANCE_PROTECTED_CONTAINER_CHANGED',lambda:engine.protect({IDENT:{'started':'original'}}))
    def test_image_source_mismatch_is_not_accepted(self):
        engine=SimpleNamespace(command=lambda args:'{}')
        self.error('ACCEPTANCE_IMAGE_SOURCE_MISMATCH',lambda:a.runtime_hashes(engine,IDENT,self.record))
    def test_image_digest_covers_runtime_and_excludes_host_cli_after_ops_move(self):
        runtime={k:v for k,v in self.record['source_hashes'].items()
                 if k.startswith('src/ops/platform/') and
                 not k.startswith('src/ops/platform/ops/')}
        self.assertTrue(runtime)
        self.assertTrue(any(k.startswith('src/ops/platform/ops/')
                            for k in self.record['source_hashes']))
        engine=SimpleNamespace(command=lambda args:json.dumps(runtime))
        self.assertEqual(a.runtime_hashes(engine,IDENT,self.record),len(runtime))
    def test_empty_image_source_closure_is_refused(self):
        engine=SimpleNamespace(command=lambda args:self.fail('must not query container'))
        self.error('ACCEPTANCE_IMAGE_SOURCE_EMPTY',
                   lambda:a.runtime_hashes(engine,IDENT,{'source_hashes':{}}))
    def test_second_run_and_cleanup_cannot_overlap_active_run(self):
        with a.run_lock(self.directory.parent):
            with self.assertRaisesRegex(ReadFailure,'ACCEPTANCE_RUN_BUSY'):
                with a.run_lock(self.directory.parent): self.fail('overlapping run admitted')
    def test_existing_image_not_found_stops_without_mutation(self):
        engine=object.__new__(a.Engine);engine.ids=lambda *args:[]
        with patch.object(engine,'command') as command:
            self.error('ACCEPTANCE_EXISTING_OPS_IMAGE_NOT_UNIQUE',lambda:engine.image(a.ROOT))
            command.assert_not_called()


class ProjectBindingTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
    def test_legacy_project_default_preserves_original_owner_bytes(self):
        raw=(self.f.directory/'owner.json').read_bytes()
        self.assertEqual(manage.prepared_project(self.f.directory),rc.PROJECT)
        self.assertEqual(manage.prepare(self.f.directory)['result'],'reused')
        self.assertEqual((self.f.directory/'owner.json').read_bytes(),raw)
    def test_explicit_project_is_selected_by_compose_without_build(self):
        owner=deepcopy(self.f.owner);owner['compose_project']='isolated-ops';a.write(self.f.directory/'owner.json',owner)
        calls=[]
        def docker(args,capture=False):
            calls.append(args)
            return 'unix:///var/run/docker.sock' if args[0]=='context' else ''
        with patch.object(manage,'docker',side_effect=docker): manage.compose(self.f.directory,'up',build=False)
        cmd=calls[-1];self.assertEqual(cmd[cmd.index('-p')+1],'isolated-ops')
        self.assertIn('--no-build',cmd);self.assertNotIn('--build',cmd)
        raw=deepcopy(self.f.docker.raw);raw['project']='isolated-ops'
        self.assertTrue(rc.container(raw,self.f.directory,self.f.cfg,owner)['healthy'])
        with self.assertRaisesRegex(ReadFailure,'CONTAINER_OWNER_MISMATCH'):
            rc.container(self.f.docker.raw,self.f.directory,self.f.cfg,owner)
    def test_existing_project_cannot_be_retargeted_by_prepare_option(self):
        with self.assertRaisesRegex(ReadFailure,'EXISTING_COMPOSE_PROJECT_DIFFERS'):
            manage.prepare(self.f.directory,compose_project='foreign-project')
    def test_invalid_project_names_refused(self):
        for name in ('','-ops','../ops','UPPER','one\ntwo','a'*64,None):
            with self.assertRaisesRegex(ReadFailure,'INVALID_COMPOSE_PROJECT'):manage.project_name(name)
    def test_activation_command_allowlist_uses_bound_project(self):
        docker=object.__new__(activation.Docker);docker.project='isolated-ops';docker.endpoint='unix:///var/run/docker.sock';docker.env={}
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=IDENT)) as command:
            self.assertEqual(docker.discover(),IDENT)
            self.assertIn('label=com.docker.compose.project=isolated-ops',command.call_args.args[0])
            old=['ps','-a','--no-trunc','--filter','label=com.docker.compose.project='+rc.PROJECT,'--filter','label=com.docker.compose.service='+rc.SERVICE,'--format','{{.ID}}']
            with self.assertRaisesRegex(ReadFailure,'ACTIVATION_DOCKER_COMMAND_REFUSED'):docker.command(old)


class RealLifecycleTests(unittest.TestCase):
    def scenario(self, exercise, scenario='discovery'):
        temp=tempfile.TemporaryDirectory(prefix='ops-native-scenario-');self.addCleanup(temp.cleanup)
        directory,record=a.create_run(Path(temp.name),MetadataEngine(),scenario);root=directory/'project';state=directory/'state'
        servers={}
        def server(role):
            http=fixture.serve(role,directory/'control/control.json','127.0.0.1',0)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            self.addCleanup(lambda:(http.shutdown(),http.server_close(),thread.join()))
            return http.server_port
        for role in (*ROLES,'manual-model'): servers[role]=server(role)
        rows=[]
        for n,(role,(service,subtree)) in enumerate(ROLES.items()):
            rows.append({'id':format(n+1,'064x'),'image':record['image'],'state':'running','started_at':rc.now(),
                         'project':record['prefix']+'-'+role,'service':service,'working_dir':str(root/subtree),
                         'config_files':str(root/subtree/('acceptance-'+role+'.json')),'network_mode':'bridge',
                         'ports':{'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':str(servers[role])}]},'listen_flags':[None]})
        source=DockerFixture(rows);engine=MetadataEngine();engine.owned=lambda directory,record:[IDENT]
        process=None;docker=None;log=(directory/'web.log').open('w+');self.addCleanup(log.close)
        env=dict(os.environ,OPS_CONFIG=str(state/'host-config.json'),
                 PYTHONPATH=os.pathsep.join(sorted({str(Path(m.__file__).resolve().parents[1]) for m in (fastapi,uvicorn)})))
        def stop():
            nonlocal process
            if process is not None and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        self.addCleanup(stop)
        def start():
            nonlocal process
            process=subprocess.Popen([sys.executable,'-B','-m','src.ops.platform.server'],cwd=root,env=env,stdout=log,stderr=log)
            try:a.wait_health(state,10)
            except Exception:log.flush();log.seek(0);self.fail(log.read()[-3000:])
        def docker_factory(project):
            nonlocal docker
            if docker is None:
                cfg=manage.existing(state);owner=json.loads((state/'owner.json').read_text())
                f=SimpleNamespace(cfg=cfg,directory=state,owner=owner,api=FakeAPI(cfg))
                docker=FakeDocker(f);docker.raw['project']=project
                docker.protected=lambda ident:{}
                docker.mounted_hash=lambda ident:hashlib.sha256((state/'config.json').read_bytes()).hexdigest()
                def restart(ident):
                    docker.restarts+=1;stop();start();docker.raw['started']=rc.now()
                docker.restart=restart
            return docker
        def command(args,capture=False):
            if args[0]=='context':return 'unix:///var/run/docker.sock'
            if 'ps' in args:return ''
            self.assertIn('--no-build',args);self.assertEqual(args[args.index('-p')+1],record['prefix']+'-ops')
            start();return ''
        def change_port(directory,engine):
            rows[0]['ports']['8080/tcp'][0]['HostPort']=str(server('model'));rows[0]['id']='f'*64
        with patch.object(a,'ROOT',root),patch.object(manage,'ROOT',root/'src/ops/platform'),patch.object(discover,'ROOT',root), \
             patch.object(bootstrap,'DockerDiscovery',return_value=source),patch.object(activation,'DockerDiscovery',return_value=source), \
             patch.object(manage,'docker',side_effect=command),patch.object(activation,'Docker',side_effect=docker_factory), \
             patch.object(a,'model_endpoint',side_effect=lambda eng,path,manual=False:'http://127.0.0.1:'+str(servers['manual-model'])):
            result=exercise(directory,engine,changed_port=change_port,read_hashes=lambda *args:0)
        return result, docker

    def test_full_scenario_with_real_http_and_web_process_docker_metadata_is_synthetic(self):
        result, docker = self.scenario(a.exercise)
        self.assertEqual(result['result'],'passed');self.assertEqual(len(result['cases']),10)
        self.assertEqual(docker.restarts,4)  # changed, failed readback, rollback, override reload
        self.assertEqual(result['managed_fields_after_manual_override'],7)
        self.assertFalse(result['watch_started']);self.assertFalse(result['production_credentials_read'])


@unittest.skipUnless(os.environ.get('PREFLIGHT_DOCKER_TEST_BINARY'),'native Docker CLI opt-in; synthetic Engine')
class NativeProjectionTests(unittest.TestCase):
    def test_ownership_projection_executes_with_native_docker_without_environment(self):
        path=Path(__file__).resolve().parents[4]/'src/infra/observability/tests/test_backup_docker_template.py'
        spec=importlib.util.spec_from_file_location('acceptance_engine',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        engine=module.NativeTemplateTests();engine.setUp();self.addCleanup(engine.tearDown)
        engine.value['Created']='2026-09-28T00:00:00Z'
        engine.value['HostConfig']={'NetworkMode':'bridge','ReadonlyRootfs':True}
        engine.value['State'].update(Running=True,Health={'Status':'healthy'})
        engine.value['Config']['Labels'].update({a.LABEL:'synthetic-owner',
            'com.docker.compose.project.config_files':'/synthetic/project/src/platform/model/acceptance-model.json'})
        result=engine.invoke(a.META);self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout);self.assertEqual(value['acceptance'],'synthetic-owner')
        self.assertNotIn('environment',result.stdout.lower())


if __name__=='__main__':unittest.main()
