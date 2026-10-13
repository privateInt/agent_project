import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from agent_runtime import http_tools
from agent_runtime.tools import ToolError

class Context:
    def __init__(self,seconds=2): self.deadline=time.monotonic()+seconds
    def time_remaining(self): return max(0,self.deadline-time.monotonic())
    def is_active(self): return self.time_remaining()>0

class Handler(BaseHTTPRequestHandler):
    calls=0
    def log_message(self,*args): pass
    def do_GET(self): self.handle_api()
    def do_POST(self): self.handle_api()
    def handle_api(self):
        type(self).calls+=1
        path=urlsplit(self.path).path
        status=200
        body=json.dumps({'path':path,'query':parse_qs(urlsplit(self.path).query),'auth':self.headers.get('Authorization'),'key':self.headers.get('Idempotency-Key')}).encode()
        if path.endswith('/slow'):time.sleep(.2)
        if path.endswith('/accepted'):status=202;body=b'{}'
        if path.endswith('/redirect'):status=302
        if path.endswith('/bad'):body=b'{bad'
        if path.endswith('/large'):body=b'x'*(http_tools.MAX_BODY+1)
        if path.endswith('/reject'):status=401
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        if status in (202,302):self.send_header('Location','http://example.test/jobs/one?token=secret-value')
        self.send_header('Content-Length',str(len(body)+(20 if path.endswith('/truncated') else 0)));self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def invoke(self,path='/echo',method='GET',effect='read',timeout=2):
        binding={'connection_version_id':'version','effect':effect,'forward_idempotency':True,'http':{'method':method,'path':path,'parameters':[],'input_schema':{'properties':{}}}}
        connection={'connection_version_id':'version','base_url':f'http://api.example.test:{self.server.server_port}/v1','auth_type':'bearer'}
        with patch.object(http_tools,'resolve',return_value='127.0.0.1'),patch.object(http_tools,'credential',return_value=('v1','secret-value')):
            return http_tools.execute(binding,{},connection,Context(timeout),'12345678-1234-4234-8234-123456789abc')
    def test_dns_blocks_mixed_and_metadata(self):
        for address in ('127.0.0.1','::1','::ffff:127.0.0.1','169.254.169.254','100.100.100.200','168.63.129.16','fd00:ec2::254','224.0.0.1','0.0.0.0'):
            self.assertFalse(http_tools.permitted_ip(address),address)
        self.assertTrue(http_tools.permitted_ip('192.168.0.10'))
        with patch.object(http_tools.socket,'getaddrinfo',return_value=[(2,1,6,'',('192.168.0.10',80)),(2,1,6,'',('127.0.0.1',80))]):
            with self.assertRaisesRegex(ToolError,'TOOL_TARGET_BLOCKED'):http_tools.resolve('mixed.test',80,1)
    def test_no_retry_and_secret_redaction(self):
        before=Handler.calls;result=self.invoke();self.assertEqual(Handler.calls,before+1)
        self.assertEqual(result['data']['auth'],'Bearer [REDACTED]');self.assertNotIn('secret-value',json.dumps(result));self.assertEqual(result['data']['key'],'12345678-1234-4234-8234-123456789abc')
    def test_async_keeps_safe_reference(self):
        with self.assertRaises(ToolError) as caught:self.invoke('/accepted','POST','write')
        self.assertEqual(caught.exception.code,'TOOL_ASYNC_UNSUPPORTED');self.assertEqual(caught.exception.result['external_reference'],'http://example.test/jobs/one')
    def test_redirect_is_not_followed(self):
        before=Handler.calls
        with self.assertRaisesRegex(ToolError,'TOOL_HTTP_ERROR'):self.invoke('/redirect')
        self.assertEqual(Handler.calls,before+1)
    def test_bad_response_and_limits(self):
        for path,code in [('/bad','TOOL_RESPONSE_INVALID'),('/truncated','TOOL_RESPONSE_INVALID'),('/large','TOOL_RESPONSE_TOO_LARGE'),('/reject','TOOL_HTTP_ERROR')]:
            with self.assertRaisesRegex(ToolError,code):self.invoke(path)
    def test_write_uncertainty_and_explicit_rejection(self):
        for path in ('/bad','/slow'):
            with self.assertRaisesRegex(ToolError,'TOOL_EFFECT_UNKNOWN'):self.invoke(path,'POST','write',.05 if path=='/slow' else 2)
        with self.assertRaisesRegex(ToolError,'TOOL_HTTP_ERROR'):self.invoke('/reject','POST','write')
    def test_api_key_mapping(self):
        binding={'connection_version_id':'v','http':{'method':'GET','path':'/echo','parameters':[], 'input_schema':{'properties':{}}}}
        for where in ('header','query'):
            connection={'connection_version_id':'v','base_url':'https://api.example.test/v1','auth_type':'api_key','api_key_in':where,'api_key_name':'X-Key'}
            with patch.object(http_tools,'credential',return_value=('v1','key-value')):
                prepared=http_tools.prepare(binding,{},connection,'')
            if where=='header':self.assertEqual(prepared[3]['X-Key'],'key-value')
            else:self.assertEqual(parse_qs(urlsplit(prepared[2]).query)['X-Key'],['key-value'])
    def test_path_serialization_and_injection(self):
        b={'connection_version_id':'v','http':{'method':'GET','path':'/item/{id}','parameters':[{'name':'id','in':'path','required':True},{'name':'q','in':'query','required':False}], 'input_schema':{'properties':{'path':{},'query':{}}}}}
        c={'connection_version_id':'v','base_url':'https://api.example.test/v1','auth_type':'none'}
        out=http_tools.prepare(b,{'path':{'id':'가 나'},'query':{'q':['a b','x&y']}},c,'')
        self.assertIn('%EA%B0%80%20%EB%82%98',out[2]);self.assertIn('q=a+b&q=x%26y',out[2])
        for value in ('..','a/b','%2e%2e','x\\y','x\r\n'):
            with self.assertRaisesRegex(ToolError,'TOOL_INPUT_INVALID'):http_tools.prepare(b,{'path':{'id':value}},c,'')

if __name__=='__main__':unittest.main()
