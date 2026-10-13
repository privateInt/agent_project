"""Real Docker formatter with a loopback synthetic Engine; no native daemon."""
from copy import deepcopy
import io
import json
import os
import sys
import time
import unittest
from unittest.mock import patch
import uuid

from src.ops.tools import backup_measure as m, backup_execute as e
from src.ops.tools.tests import test_cold_storage_acceptance as native
from src.ops.tools.tests.test_backup_measure_inventory import Engine
from src.ops.candidates.cold_storage_20260930.tests import test_storage as fixture


class Diagnostics(unittest.TestCase):
    def test_query_failure_reaches_cli_before_helper_with_no_raw_values_or_retry(self):
        calls=[]
        def runner(argv,env,seconds):
            calls.append((argv,env,seconds))
            return 1,b'PRIVATE_STDOUT',b'template: map has no entry for key "Health" PRIVATE_STDERR'
        backend=e.Docker({'endpoint':'unix:///private/socket'},sys.executable,runner)
        with self.assertRaises(m.setup.Failure) as caught:
            backend.query(['container','inspect','--format',m.s.FORMAT,'a'*64])
        query=caught.exception.docker_query_diagnostic
        self.assertEqual(query['phase'],'connected_metadata')
        self.assertEqual(query['reason'],'TEMPLATE_FIELD_MISSING');self.assertEqual(query['template_field'],'Health')
        error=caught.exception
        fake=type('Executor',(),{'measure':lambda self,seconds:(_ for _ in ()).throw(error)})()
        out=io.StringIO()
        with patch.object(e,'Execute',return_value=fake),patch('sys.stdout',out):code=e.main(['measure'])
        value=json.loads(out.getvalue());self.assertEqual(code,2);self.assertEqual(value['docker_query_diagnostic'],query)
        for secret in ('PRIVATE','a'*64,'/private'):self.assertNotIn(secret,out.getvalue())
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][1],{'PATH':os.defpath,'LC_ALL':'C','HOME':'/nonexistent'})
    def test_timeout_limit_cli_unavailable_and_invalid_json_preserve_codes_without_retry(self):
        args=['info','--format',m.s.HOST_FORMAT]
        cases=[(m.g.a.Failure('COLD_DOCKER_OUTCOME_UNKNOWN'),'COMMAND_TIMEOUT'),
               (m.g.a.Failure('COLD_DOCKER_RESPONSE_LIMIT'),'RESPONSE_LIMIT'),
               (FileNotFoundError('SECRET'),'CLI_UNAVAILABLE'),(None,'INVALID_JSON')]
        for error,reason in cases:
            calls=[]
            def runner(*args):
                calls.append(args)
                if error is not None:raise error
                return 0,b'SECRET invalid JSON',b''
            with self.subTest(reason=reason),self.assertRaises(m.setup.Failure) as caught:
                m.MetadataDocker({'endpoint':'unix:///synthetic/socket'},sys.executable,runner).query(args)
            self.assertEqual(caught.exception.docker_query_diagnostic['reason'],reason)
            self.assertEqual(len(calls),1);self.assertNotIn('SECRET',json.dumps(caught.exception.docker_query_diagnostic))
    def test_fixed_error_signatures_and_projection_reject_unknown_or_unbounded_values(self):
        backend=m.MetadataDocker({},sys.executable)
        for text,reason in ((b'permission denied SECRET','ACCESS_DENIED'),(b'Cannot connect SECRET','CONNECTION_FAILED'),
                            (b'No such image SECRET','IMAGE_NOT_FOUND'),(b'No such container SECRET','CONTAINER_NOT_FOUND'),
                            (b'template parsing error SECRET','TEMPLATE_PARSE_ERROR'),(b'other SECRET','UNKNOWN')):
            error=m.query_failure(backend,['info'],'COLD_DOCKER_METADATA_FAILED',1,text)
            self.assertEqual(error.docker_query_diagnostic['reason'],reason)
            self.assertNotIn('SECRET',json.dumps(error.docker_query_diagnostic))
        valid=error.docker_query_diagnostic
        for key,value in (('phase',[]),('reason',{}),('operation','SECRET'),('exit_code',True),('exit_code',256),
                          ('template_field',[]),('schema',True),('values_returned',True),('retry_requested',True)):
            self.assertIsNone(m.safe_query_diagnostic(dict(valid,**{key:value})))
        self.assertIsNone(m.safe_query_diagnostic(dict(valid,stderr='SECRET')))
    def test_invalid_optional_projection_target_is_refused_before_any_call(self):
        backend=m.MetadataDocker({},sys.executable,lambda *args:self.fail('must not invoke CLI'))
        for args in (['container','inspect','--format',m.s.FORMAT,'latest'],
                     ['image','inspect','--format',m.s.IMAGE_FORMAT,'latest']):
            with self.assertRaises(m.setup.Failure):backend.query(args)
    def test_failed_helper_cleanup_keeps_original_error_and_reports_remaining_current_helper(self):
        f=fixture.Files();f.setUp();self.addCleanup(f.doCleanups)
        engine=Engine(f);backend=m.Docker(f.host,sys.executable,engine)
        scope=m.Scope(f.scope.descriptor,backend.baseline);backend.bind(f.proposal,scope,time.monotonic()+60)
        original=backend.helper
        def helper(ident,spec):
            if ident in backend.owned_helpers:
                raise m.query_failure(backend,['container','inspect'],'COLD_DOCKER_METADATA_FAILED',1,b'permission denied SECRET')
            return original(ident,spec)
        backend.helper=helper
        with self.assertRaises(m.setup.Failure) as caught:m.cold_size(backend,f.proposal,'data',10)
        error=caught.exception;self.assertEqual(error.docker_query_diagnostic['phase'],'helper_create_guard')
        cleanup=m.safe_cleanup_diagnostic(error.measure_cleanup_diagnostic)
        self.assertEqual(cleanup['helpers_remaining'],1)
        self.assertEqual(cleanup['docker_query_diagnostic']['phase'],'helper_cleanup')
        self.assertEqual(len(backend.owned_helpers),1)
        self.assertFalse(any(a[:2] in (['container','kill'],['container','rm'],['container','stop']) for a in engine.calls))
        self.assertNotIn('SECRET',json.dumps(cleanup))
    def test_successful_abort_preserves_original_query_diagnostic_and_removes_only_created_helper(self):
        f=fixture.Files();f.setUp();self.addCleanup(f.doCleanups)
        engine=Engine(f);backend=m.Docker(f.host,sys.executable,engine)
        scope=m.Scope(f.scope.descriptor,backend.baseline);backend.bind(f.proposal,scope,time.monotonic()+60)
        original=backend.helper;first=[True]
        def helper(ident,spec):
            if first[0]:
                first[0]=False
                raise m.query_failure(backend,['container','inspect'],'COLD_DOCKER_METADATA_FAILED',1,b'No such object SECRET')
            return original(ident,spec)
        backend.helper=helper
        with self.assertRaises(m.setup.Failure) as caught:m.cold_size(backend,f.proposal,'data',10)
        self.assertEqual(caught.exception.docker_query_diagnostic['phase'],'helper_create_guard')
        self.assertFalse(backend.owned_helpers);self.assertFalse(engine.helpers)
        self.assertFalse(hasattr(caught.exception,'measure_cleanup_diagnostic'))


@unittest.skipUnless(os.environ.get('PREFLIGHT_DOCKER_TEST_BINARY'),'explicit Docker CLI; synthetic loopback Engine only')
class NativeCLI(unittest.TestCase):
    setUp=native.NativeCLI.setUp
    stop=native.NativeCLI.stop
    invoke=native.NativeCLI.invoke
    def backend(self,cls=m.MetadataDocker):
        return cls({'endpoint':'tcp://127.0.0.1:'+str(self.server.server_port),'daemon_id':'fake-daemon'},self.binary)
    def image_config(self,value):
        original=self.server.RequestHandlerClass.do_GET
        def get(handler):
            if '/images/' in handler.path:handler.send({'Id':self.image,'Config':value})
            else:original(handler)
        self.server.RequestHandlerClass.do_GET=get
    def test_previous_healthless_template_fails_and_both_new_adapters_handle_missing_health(self):
        args=['container','inspect','--format',m.s.FORMAT,self.ident]
        old=self.invoke(args);self.assertNotEqual(old.returncode,0)
        self.assertIn(b'map has no entry for key "Health"',old.stderr)
        for cls in (m.Docker,e.Docker):
            self.assertEqual(self.backend(cls).query(args)[5],'none')
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.calls))
    def test_health_status_is_preserved_for_healthy_and_unhealthy_containers(self):
        args=['container','inspect','--format',m.s.FORMAT,self.ident]
        for status in ('healthy','unhealthy'):
            self.value['State']['Health']={'Status':status}
            self.assertEqual(self.backend().query(args)[5],status)
    def test_absent_volumes_reproduces_original_error_and_new_query_preserves_null(self):
        self.image_config({});args=['image','inspect','--format',m.s.IMAGE_FORMAT,self.image]
        old=self.invoke(args);self.assertNotEqual(old.returncode,0)
        self.assertIn(b'map has no entry for key "Volumes"',old.stderr)
        self.assertEqual(self.backend().query(args),[self.image,None])
    def test_null_and_declared_volumes_are_preserved_without_image_pull(self):
        for volumes in (None,{'/data':{}}):
            self.image_config({'Volumes':volumes})
            self.assertEqual(self.backend().query(['image','inspect','--format',m.s.IMAGE_FORMAT,self.image]),[self.image,volumes])
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.calls))
    def create_fixture(self,config):
        f=fixture.Files();f.setUp();self.addCleanup(f.doCleanups)
        self.image=f.helper['image_id'];self.image_config(config)
        proposal=deepcopy(f.proposal);proposal['host']['daemon_id']='fake-daemon'
        proposal['review_id']=m.g.c.fingerprint({k:v for k,v in proposal.items() if k!='review_id'})
        def runner(argv,env,seconds):
            result=self.invoke(argv[3:]);return result.returncode,result.stdout,result.stderr
        backend=m.Docker(proposal['host'],self.binary,runner)
        backend.bind(proposal,f.scope,time.monotonic()+30)
        return backend,m.recipe(proposal,'data',str(uuid.uuid4()),10)
    def test_actual_create_allows_absent_volumes_and_serializes_existing_security_controls(self):
        backend,spec=self.create_fixture({})
        self.assertEqual(backend.create(spec),self.ident);self.assertEqual(len(self.requests),1)
        row=self.requests[0];control=row['HostConfig']
        self.assertEqual(control['NetworkMode'],'none');self.assertTrue(control['ReadonlyRootfs'])
        self.assertEqual(control['CapDrop'],['ALL']);self.assertFalse(control['Privileged'])
        self.assertEqual(control['Mounts'][0]['Source'],spec['source']);self.assertTrue(control['Mounts'][0]['ReadOnly'])
        self.assertTrue(control['Mounts'][0]['BindOptions']['NonRecursive']);self.assertEqual(row['Image'],spec['image_id'])
        self.assertEqual(row['Cmd'],['-I','-B','-c',spec['script'],json.dumps(spec['config'],sort_keys=True)])
    def test_actual_create_refuses_declared_volumes_before_posting_create(self):
        backend,spec=self.create_fixture({'Volumes':{'/data':{}}})
        with self.assertRaisesRegex(m.g.a.Failure,'IMAGE_OR_VOLUMES_CHANGED'):backend.create(spec)
        self.assertFalse(self.requests);self.assertFalse(backend.owned_helpers)
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.calls))
    def test_actual_helper_without_health_keeps_all_security_checks(self):
        backend,spec=self.create_fixture({});backend.create(spec)
        row=self.requests[0];self.value['Image']=spec['image_id'];self.value['Config'].update(row)
        self.value['HostConfig'].update(row['HostConfig'])
        self.value['Mounts']=[{'Type':'bind','Source':spec['source'],'Destination':'/source','RW':False}]
        self.value['State'].update(Running=False,Pid=0,Status='created')
        backend.helper(self.ident,spec)
        self.value['HostConfig']['ReadonlyRootfs']=False
        with self.assertRaisesRegex(m.g.a.Failure,'SECURITY_CHANGED'):backend.helper(self.ident,spec)
    def test_real_template_failure_projects_safe_phase_reason_and_field(self):
        self.image_config({});backend=self.backend(m.Docker);backend.phase='helper_create_guard'
        with patch.object(m,'IMAGE_FORMAT',m.s.IMAGE_FORMAT),self.assertRaises(m.setup.Failure) as caught:
            backend.query(['image','inspect','--format',m.s.IMAGE_FORMAT,self.image])
        value=caught.exception.docker_query_diagnostic
        self.assertEqual(value['reason'],'TEMPLATE_FIELD_MISSING');self.assertEqual(value['template_field'],'Volumes')
        self.assertEqual(value['phase'],'helper_create_guard');self.assertNotIn(self.image,json.dumps(value))


if __name__=='__main__':unittest.main()
