"""Real Docker CLI templates against an isolated synthetic Engine; safe diagnostic regressions."""
import http.server
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ops_tools import maintenance_control as m
from ops_tools.tests import test_maintenance_control as existing

BINARY = os.environ.get('PREFLIGHT_DOCKER_TEST_BINARY')
SECRET = 'SYNTHETIC-PRIVATE-NEVER-RETURN'
OLD_HOST = '{{json (list .DockerRootDir .Name .Architecture .OSType .SecurityOptions .ID)}}'
OLD_LIVE = '{{json (list .Id .State.Running .State.StartedAt)}}'


class Diagnostics(unittest.TestCase):
    def test_old_template_failure_fixed_labels_only_without_raw_private_text(self):
        for op in m.DOCKER_OPERATIONS:
            result=m.docker_failure(op,1,SECRET.encode(),b'template parsing error: function "list" not defined '+SECRET.encode())
            self.assertEqual(result['reason'],'TEMPLATE_FUNCTION_UNAVAILABLE');self.assertFalse(result['retry_requested'])
            self.assertNotIn(SECRET,json.dumps(result));self.assertEqual(m.safe_docker_diagnostic(result),result)

    def test_each_allowlisted_query_reports_its_operation_and_is_never_retried(self):
        requests=([['info','--format',m.HOST_FORMAT],['ps','-q','--no-trunc'],
                   ['container','inspect','--format',m.LIVE_FORMAT,'a'*64]])
        for args,op in zip(requests,('host_info','running_list','running_inspect')):
            calls=[]
            def runner(*a):calls.append(a);return 1,SECRET.encode(),b'function "list" not defined'
            d=m.docker_type(existing.cap,existing.runtime.probe)(runner=runner);d.base.endpoint='unix:///synthetic/socket'
            with self.assertRaises(m.Failure) as error:d.query(args)
            self.assertEqual(error.exception.docker_diagnostic['operation'],op);self.assertEqual(len(calls),1)
            self.assertNotIn(SECRET,str(error.exception))

    def test_timeout_unavailable_and_output_limit_have_safe_fixed_outcomes(self):
        for code,outcome,reason in (('CLIENT_DEADLINE_EXCEEDED','timeout','COMMAND_TIMEOUT'),
                                   ('COMMAND_UNAVAILABLE','unavailable','CLI_UNAVAILABLE'),
                                   ('COMMAND_OUTPUT_LIMIT','response_limit','RESPONSE_LIMIT')):
            def runner(*a):raise existing.cap.bp.Unavailable(code)
            d=m.docker_type(existing.cap,existing.runtime.probe)(runner=runner);d.base.endpoint='unix:///synthetic/socket'
            with self.assertRaises(m.Failure) as error:d.query(['ps','-q','--no-trunc'])
            report=error.exception.docker_diagnostic
            self.assertEqual((report['outcome'],report['reason'],report['exit_code']),(outcome,reason,None))

    def test_field_connection_permission_missing_unknown_and_ambiguity(self):
        cases=[("can't evaluate field",'TEMPLATE_FIELD_UNAVAILABLE'),('connection refused','DOCKER_CONNECTION_FAILED'),
               ('permission denied','DOCKER_ACCESS_DENIED'),('no such container:','CONTAINER_NOT_FOUND'),
               (SECRET,'UNKNOWN'),('permission denied; connection refused','MULTIPLE_SIGNATURES')]
        for text,reason in cases:self.assertEqual(m.docker_failure('host_info',1,stderr=text)['reason'],reason)

    def test_large_diagnostic_is_bounded_and_never_returns_partial_text(self):
        value=m.docker_failure('host_info',1,stderr='x'*32769+'function "list" not defined '+SECRET)
        self.assertTrue(value['text_truncated']);self.assertEqual(value['reason'],'UNKNOWN')
        self.assertNotIn(SECRET,json.dumps(value))

    def test_foreign_extra_fields_mutating_operation_and_bad_types_not_projected(self):
        value=m.docker_failure('host_info',1)
        for key,bad in (('operation','stop'),('reason',SECRET),('exit_code',True),('retry_requested',True),('text_truncated',1)):
            row=dict(value);row[key]=bad;self.assertIsNone(m.safe_docker_diagnostic(row))
        self.assertIsNone(m.safe_docker_diagnostic(dict(value,stderr=SECRET)))

    def test_unknown_exception_and_partial_output_never_become_success_or_retry(self):
        calls=[]
        def runner(*a):calls.append(a);raise RuntimeError(SECRET)
        d=m.docker_type(existing.cap,existing.runtime.probe)(runner=runner);d.base.endpoint='unix:///synthetic/socket'
        with self.assertRaises(m.Failure) as error:d.query(['info','--format',m.HOST_FORMAT])
        self.assertEqual(error.exception.docker_diagnostic['reason'],'UNKNOWN');self.assertEqual(len(calls),1)
        self.assertNotIn(SECRET,json.dumps(error.exception.docker_diagnostic))

    def test_cli_projects_diagnostic_and_preserves_readonly_effects(self):
        error=m.Failure('MAINTENANCE_DOCKER_METADATA_FAILED',m.docker_failure('host_info',1,stderr='function "list" not defined '+SECRET))
        with patch.object(sys,'argv',['maintenance_control.py','plan']),patch.object(m,'Review',side_effect=error),patch.object(sys,'stdout',new_callable=io.StringIO) as output:
            self.assertEqual(m.main(),2);report=json.loads(output.getvalue())
        self.assertEqual(report['docker_diagnostic']['operation'],'host_info');self.assertNotIn(SECRET,json.dumps(report))
        self.assertEqual(report['effects']['files_written'],[]);self.assertFalse(report['effects']['service_control_requested'])
        self.assertFalse(report['capabilities']['stop'])


@unittest.skipUnless(BINARY,'Set PREFLIGHT_DOCKER_TEST_BINARY to an existing real Docker CLI')
class NativeTemplates(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.calls=[]
        self.ids=['a'*64,'b'*64];self.started='2026-09-30T04:00:00Z'
        self.host=dict(ID='synthetic-daemon',DockerRootDir='/synthetic/storage',Name='synthetic-host',
                       Architecture='x86_64',OSType='linux',SecurityOptions=['name=seccomp,profile=builtin'],
                       Containers=2,Images=1,ServerVersion='29.0.4',Driver='overlay2',MemTotal=1024*1024)
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_HEAD(self):
                owner.calls.append(('HEAD',self.path));self.send_response(200);self.send_header('API-Version','1.51');self.end_headers()
            def do_GET(self):
                owner.calls.append(('GET',self.path))
                if self.path.endswith('/info'):value=owner.host
                else:
                    ident=self.path.split('/')[-2]
                    value=dict(Id=ident,Image='sha256:'+'c'*64,State=dict(Running=True,Status='running',StartedAt=owner.started),
                               Config=dict(Image='synthetic:fixture',Env=['PRIVATE='+SECRET]),Mounts=[])
                raw=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def invoke(self,args):
        env={k:v for k,v in os.environ.items() if not k.startswith('DOCKER_')}
        return subprocess.run([BINARY,'--config',str(self.root/'client'),'--host','tcp://127.0.0.1:'+str(self.server.server_port),*args],
                              capture_output=True,text=True,env=env,timeout=10)

    def check_reads(self,result):
        self.assertNotIn(SECRET,result.stdout+result.stderr)
        self.assertTrue(all(method in ('HEAD','GET') for method,_ in self.calls))

    def test_previous_host_template_reproduces_real_function_failure(self):
        result=self.invoke(['info','--format',OLD_HOST]);self.assertNotEqual(result.returncode,0)
        self.assertIn('function "list" not defined',result.stderr);self.check_reads(result)

    def test_previous_running_template_reproduces_real_function_failure(self):
        result=self.invoke(['container','inspect','--format',OLD_LIVE,*self.ids]);self.assertNotEqual(result.returncode,0)
        self.assertIn('function "list" not defined',result.stderr);self.check_reads(result)

    def test_corrected_host_template_evaluates_in_real_cli(self):
        result=self.invoke(['info','--format',m.HOST_FORMAT]);self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),[self.host[k] for k in ('DockerRootDir','Name','Architecture','OSType','SecurityOptions','ID')])
        self.check_reads(result)

    def test_corrected_running_template_emits_one_json_row_per_container(self):
        result=self.invoke(['container','inspect','--format',m.LIVE_FORMAT,*self.ids]);self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual([json.loads(x) for x in result.stdout.splitlines()],[[i,True,self.started] for i in self.ids]);self.check_reads(result)


if __name__=='__main__':unittest.main()
