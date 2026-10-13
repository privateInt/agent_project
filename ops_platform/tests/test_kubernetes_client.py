"""Kubectl request limits plus the actual native client against an isolated TLS API."""
from copy import deepcopy
import http.server
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ops_platform.contracts import ReadFailure
from ops_platform.ops.kubernetes_client import Kubectl, execute, PROJECT_SELECTOR
from ops_platform.ops.discovery_kubernetes import discover_kubernetes
from ops_platform.transport import Transport
from ops_platform.tests.test_discovery_kubernetes import Objects, kube_metrics
from ops_platform.tests.test_discovery import Fixture as HTTPFixture


class ClientTests(unittest.TestCase):
    def setUp(self): self.objects=Objects();self.client=self.objects.client()

    def test_context_server_pinned_without_raw_credentials_or_mutations(self):
        commands=[]
        def command(args):commands.append(args);return self.objects.command(args)
        client=Kubectl(command=command);client.binding();client.namespace('kube-system');client.services([])
        self.assertEqual(client.bound['server'],self.objects.server)
        self.assertTrue(all('config' in a or a[-3:-1]==['get','--raw'] for a in commands))
        self.assertNotIn('--raw',commands[0]);self.assertNotIn('REDACTED-PRIVATE-FIXTURE',json.dumps(client.binding()))

    def test_context_drift_and_insecure_tls_refused(self):
        self.client.binding();self.objects.server='https://different.invalid'
        with self.assertRaises(ReadFailure) as exc:self.client.binding()
        self.assertEqual(exc.exception.code,'KUBERNETES_CONTEXT_CHANGED')
        self.objects.insecure=True
        with self.assertRaises(ReadFailure) as exc:self.objects.client().binding()
        self.assertEqual(exc.exception.code,'KUBERNETES_VERIFIED_TLS_REQUIRED')

    def test_plaintext_api_refused_before_authentication(self):
        self.objects.server='http://127.0.0.1:8080'
        with self.assertRaises(ReadFailure) as exc:self.client.binding()
        self.assertEqual(exc.exception.code,'KUBERNETES_VERIFIED_TLS_REQUIRED');self.assertFalse(self.objects.calls)

    def test_no_context_and_missing_binary_have_distinct_errors(self):
        client=Kubectl(command=lambda args:b'{"contexts":[],"clusters":[]}')
        with self.assertRaises(ReadFailure) as exc:client.binding()
        self.assertEqual(exc.exception.code,'KUBERNETES_CONTEXT_REQUIRED')
        with self.assertRaises(ReadFailure) as exc:execute(['/nonexistent/kubectl'])
        self.assertEqual(exc.exception.code,'KUBECTL_UNAVAILABLE')

    def test_only_allowlisted_get_paths_and_query_parameters(self):
        self.client.binding()
        for path in ('/api/v1/secrets','/api/v1/nodes','https://other/api/v1/services',
                     '/api/v1/services?watch=true','/api/v1/namespaces/kube-system?x=1',
                     '/api/v1/namespaces/default/pods/name/exec'):
            with self.subTest(path=path),self.assertRaises(ReadFailure) as exc:self.client.get(path)
            self.assertEqual(exc.exception.code,'KUBERNETES_GET_PATH_REFUSED')
        self.assertFalse(self.objects.calls)

    def test_pagination_consumed_without_partial_success(self):
        self.client.binding();calls=[]
        original=self.objects.command
        def command(args):
            if 'config' in args:return original(args)
            calls.append(args[-1])
            n=len(calls)-1
            return json.dumps({'kind':'ServiceList','metadata':{'continue':'next/+?' if n==0 else ''},
                               'items':[deepcopy(self.objects.services[n])]}).encode()
        self.client.command=command
        rows=self.client.services([])
        self.assertEqual(len(rows),2);self.assertIn('continue=next%2F%2B%3F',calls[1])

    def test_typed_list_items_may_omit_typemeta_but_conflicting_types_are_refused(self):
        self.client.binding();item=deepcopy(self.objects.services[0]);item.pop('apiVersion');item.pop('kind')
        body={'apiVersion':'v1','kind':'ServiceList','items':[item]}
        self.client.command=lambda args:json.dumps(body).encode()
        self.assertEqual(self.client.services([])[0]['kind'],'Service')
        item['kind']='Secret'
        with self.assertRaises(ReadFailure) as exc:self.client.services([])
        self.assertEqual(exc.exception.code,'INVALID_KUBERNETES_RESPONSE')

    def test_repeated_page_or_continuation_is_rejected(self):
        self.client.binding()
        raw={'kind':'ServiceList','metadata':{'continue':'again'},'items':[self.objects.services[0]]}
        self.client.command=lambda args:json.dumps(raw).encode()
        with self.assertRaises(ReadFailure) as exc:self.client.services([])
        self.assertEqual(exc.exception.code,'KUBERNETES_DUPLICATE_OBJECT')
        raw['items']=[]
        with self.assertRaises(ReadFailure) as exc:self.client.services([])
        self.assertEqual(exc.exception.code,'KUBERNETES_PAGINATION_INVALID')

    def test_output_limits_and_timeout_terminate_children_without_exposing_stderr(self):
        for code,timeout,limit,expected in [
            ('import sys;sys.stdout.write("x"*10000)',2,100,'KUBERNETES_RESPONSE_LIMIT'),
            ('import sys;sys.stderr.write("PRIVATE"*10000)',2,4096,'KUBERNETES_RESPONSE_LIMIT'),
            ('import time;time.sleep(5)',.05,4096,'KUBERNETES_TIMEOUT'),
            ('import sys;sys.stderr.write("Forbidden PRIVATE-KEY");sys.exit(1)',2,4096,'KUBERNETES_FORBIDDEN')]:
            with self.subTest(expected=expected),self.assertRaises(ReadFailure) as exc:execute([sys.executable,'-c',code],timeout,limit)
            self.assertEqual(exc.exception.code,expected);self.assertNotIn('PRIVATE',str(exc.exception))

    def test_namespace_and_installation_flags_are_data_not_shell_or_api_paths(self):
        for context in ('--server=elsewhere','bad\ncontext'):
            with self.assertRaises(ReadFailure):Kubectl(context)
        self.client.binding()
        for namespace in ('../../secrets','bad/namespace','UPPER','a'*64):
            with self.assertRaises(ReadFailure):self.client.services([namespace])
        self.assertFalse(self.objects.calls)


@unittest.skipUnless(os.environ.get('KUBECTL_TEST_BINARY'),'native kubectl opt-in')
class NativeKubectlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ops-native-kube-');self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);cert,key=root/'cert.pem',root/'key.pem'
        result=subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1',
                               '-addext','subjectAltName=IP:127.0.0.1','-keyout',str(key),'-out',str(cert)],capture_output=True)
        self.assertEqual(result.returncode,0)
        self.objects=Objects();self.requests=[];self.forbidden=False;owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.requests.append((self.command,self.path,self.headers.get('Authorization')=='Bearer fixture-kube-auth'))
                if owner.forbidden:
                    code=403;body={'kind':'Status','apiVersion':'v1','status':'Failure','message':'Forbidden PRIVATE-FIXTURE','reason':'Forbidden','code':403}
                else:
                    code=200;body=owner.objects.response(self.path)
                    for item in body.get('items',[]):item.pop('kind',None);item.pop('apiVersion',None)
                raw=json.dumps(body).encode();self.send_response(code);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key)
        self.server.socket=ctx.wrap_socket(self.server.socket,server_side=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        config={'apiVersion':'v1','kind':'Config','current-context':'native-context',
                'clusters':[{'name':'native-cluster','cluster':{'server':'https://127.0.0.1:'+str(self.server.server_port),'certificate-authority':str(cert)}}],
                'contexts':[{'name':'native-context','context':{'cluster':'native-cluster','user':'native-user'}}],
                'users':[{'name':'native-user','user':{'token':'fixture-kube-auth'}}]}
        self.config=root/'kubeconfig.json';self.config.write_text(json.dumps(config));self.config.chmod(0o600)
        env=patch.dict(os.environ,{'KUBECONFIG':str(self.config)});env.start();self.addCleanup(env.stop)
        self.client=Kubectl(binary=os.environ['KUBECTL_TEST_BINARY'])

    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join()

    def test_actual_kubectl_tls_authentication_and_full_read_only_discovery(self):
        http=HTTPFixture();self.addCleanup(http.close);kube_metrics(http)
        result=discover_kubernetes(client=self.client,transport_factory=lambda url,**kwargs:Transport(http.url,**kwargs))
        self.assertEqual(result['result'],'discovered',result)
        self.assertTrue(all(method=='GET' and auth for method,path,auth in self.requests))
        self.assertTrue(all(not any(s in path for s in ('/secrets','/pods','/nodes','/proxy','/exec')) for _,path,_ in self.requests))
        self.assertTrue(all(auth is None for _,path,auth in http.calls))
        self.assertNotIn('fixture-kube-auth',json.dumps(result));self.assertNotIn('certificate-authority',json.dumps(result))
        self.assertEqual(result['kubernetes']['binding']['context'],'native-context')

    def test_native_rbac_denial_and_tls_failure_remain_errors(self):
        self.forbidden=True
        result=discover_kubernetes(client=self.client)
        self.assertEqual(result['error'],'KUBERNETES_FORBIDDEN');self.assertNotIn('PRIVATE',json.dumps(result))
        cfg=json.loads(self.config.read_text());cfg['clusters'][0]['cluster'].pop('certificate-authority')
        self.config.write_text(json.dumps(cfg));before=len(self.requests)
        result=discover_kubernetes(client=Kubectl(binary=os.environ['KUBECTL_TEST_BINARY']))
        self.assertEqual(result['error'],'KUBERNETES_REQUEST_FAILED');self.assertEqual(len(self.requests),before)
        self.config.write_text(json.dumps({'apiVersion':'v1','kind':'Config','contexts':[],'clusters':[],'users':[]}))
        result=discover_kubernetes(client=Kubectl(binary=os.environ['KUBECTL_TEST_BINARY']))
        self.assertEqual(result['error'],'KUBERNETES_CONTEXT_REQUIRED');self.assertEqual(len(self.requests),before)


if __name__=='__main__':unittest.main()
