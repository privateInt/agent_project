"""HTTP contract fixtures mirror checked-in public gateway response shapes, not domain mocks."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from src.ops.platform.client import Client
from src.ops.platform.contracts import ReadFailure, project
from src.ops.platform.state import success_cache
from src.ops.platform.transport import Transport

ID = '11111111-1111-4111-8111-111111111111'
OP = '22222222-2222-4222-8222-222222222222'
VERSION = '33333333-3333-4333-8333-333333333333'
TOOL = '44444444-4444-4444-8444-444444444444'
OTHER = '55555555-5555-4555-8555-555555555555'
STAMP = '2026-09-26T23:07:00.123456789Z'
PRIVATE = 'PRIVATE_PROMPT_DOCUMENT_TOKEN_SENTINEL'


def fixtures(platform):
    operation = {'operation_id': OP, 'status': 'succeeded', 'phase': 'complete',
                 'resource_id': ID, 'kind': 'test.run', 'error_code': '',
                 'error_message': PRIVATE, 'created_at': STAMP, 'completed_at': STAMP,
                 'dispatch_token': PRIVATE}
    catalog = {'/health': {'status': 'ok', 'service': 'model-gateway', 'scope': 'gateway'}}
    if platform == 'model':
        objects = {
            'operations': operation,
            'model-versions': {'model_version_id': ID, 'state': 'ready', 'operation_id': OP},
            'training-runs': {'training_run_id': ID, 'status': 'succeeded', 'operation_id': OP, 'training': {'prompt': PRIVATE}},
            'deployments': {'deployment_id': ID, 'status': 'ready', 'runtime_state': 'ready', 'desired_state': 'running', 'operation_id': OP, 'endpoint': PRIVATE},
        }
    elif platform == 'data':
        objects = {
            'operations': operation,
            'crawl-runs': {'crawl_run_id': ID, 'operation_id': OP, 'submission_status': 'delivered', 'url': PRIVATE, 'created_at': STAMP},
            'refinement-runs': {'refinement_run_id': ID, 'operation_id': OP, 'submission_status': 'delivered', 'ocr': {'private': PRIVATE}, 'created_at': STAMP},
        }
    elif platform == 'knowledge':
        objects = {
            'chunking-runs': {'chunking_run_id': ID, 'operation_id': OP, 'submission_status': 'delivered', 'spec': {'text': PRIVATE}, 'created_at': STAMP},
            'indexes': {'index_id': ID, 'state': 'active', 'active_version_id': VERSION, 'created_at': STAMP},
        }
        # This native operation projection lacks resource/timestamp fields.
        catalog['/api/operations/'+OP] = {'operation_id': OP, 'status': 'succeeded', 'phase': 'complete', 'error_code': '', 'error_message': PRIVATE}
        v = {'index_version_id': VERSION, 'index_id': ID, 'operation_id': OP, 'state': 'ready', 'reference_state': 'held', 'created_at': STAMP, 'input_spec': {'text': PRIVATE}}
        catalog['/api/indexes/'+ID+'/versions'] = {'items': [v], 'next_cursor': ''}
        catalog['/api/index-versions/'+VERSION] = v
    else:
        catalog = {'/healthz': {'platform': 'agent', 'status': 'ok'},
                   '/api/status': {'platform': 'agent', 'ready': True, 'database': 'ready', 'runtime': 'ready', 'request_id': OTHER}}
        objects = {'agent-runs': {
            'agent_run_id': ID, 'agent_version_id': VERSION, 'state': 'failed',
            'request_id': OTHER, 'tool_call_id': TOOL, 'input': {'question': PRIVATE},
            'question': PRIVATE, 'invocation_key': PRIVATE, 'result': {'answer': PRIVATE},
            'error': {'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize', 'message': PRIVATE},
            'caller_context': {'system': 'n8n', 'workflow_id': 'Flow_1', 'execution_id': '5'},
            'created_at': STAMP, 'started_at': STAMP, 'completed_at': STAMP,
        }}
        catalog['/api/agent-runs/'+ID+'/steps'] = {'items': [
            {'step_id': step, 'state': 'failed' if step == 'finalize' else 'succeeded',
             'detail': {'prompt': PRIVATE, 'error_code': 'ANSWER_REFERENCE_INVALID' if step == 'finalize' else ''}}
            for step in ('validate', 'search', 'context', 'generate', 'finalize')]}
        catalog['/api/tool-calls/'+TOOL] = {'tool_call_id': TOOL, 'state': 'succeeded', 'tool_version_id': VERSION,
            'request_id': OTHER, 'parent_agent_run_id': ID, 'parent_step_id': 'search', 'result': {'text': PRIVATE}}
    keys = {'operations':'operation_id', 'model-versions':'model_version_id', 'training-runs':'training_run_id',
            'deployments':'deployment_id', 'crawl-runs':'crawl_run_id', 'refinement-runs':'refinement_run_id',
            'chunking-runs':'chunking_run_id', 'indexes':'index_id', 'agent-runs':'agent_run_id'}
    for name, value in objects.items():
        catalog['/api/'+name] = {'items': [value], 'next_cursor': ''}
        catalog['/api/'+name+'/'+value[keys[name]]] = value
    if platform == 'model':
        detail = deepcopy(operation)
        detail.pop('created_at'); detail.pop('completed_at')
        catalog['/api/operations/'+OP] = detail
    return catalog


@contextmanager
def server(platform, routes=None):
    catalog = fixtures(platform) if routes is None else routes
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            calls.append((self.command, self.path, self.headers.get('Authorization')))
            split = urllib.parse.urlsplit(self.path)
            value = catalog.get(self.path, catalog.get(split.path, (404, {'message': PRIVATE})))
            if callable(value):
                value = value(self.path)
            code, body, headers = 200, value, {}
            if isinstance(value, tuple):
                code, body = value[:2]
                if len(value) > 2:
                    headers = value[2]
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', headers.pop('Content-Type', 'application/json'))
            self.send_header('Content-Length', headers.pop('Content-Length', str(len(raw))))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval':0.01}, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:'+str(httpd.server_port), catalog, calls
    finally:
        httpd.shutdown();httpd.server_close();thread.join()


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = Path(self.tmp.name)/'token'
        self.token.write_text('local-test-auth-0123456789')

    def client(self, platform, url, **kwargs):
        return Client(platform, url, str(self.token) if platform == 'agent' else None, **kwargs)

    def test_four_platform_http_readback_and_failed_business_are_separate(self):
        for platform in ('model','data','knowledge','agent'):
            with self.subTest(platform=platform), server(platform) as (url, routes, calls):
                result = self.client(platform,url).check(agent_run_id=ID if platform=='agent' else None)
                self.assertEqual(result['result'],'passed',result)
                self.assertTrue(all(m=='GET' for m,_,_ in calls))
                self.assertNotIn(PRIVATE,json.dumps(result))
                if platform=='agent':
                    self.assertEqual(result['pinned_agent_run']['data']['state'],'failed')
                    self.assertTrue(all(auth==('Bearer local-test-auth-0123456789' if path.startswith('/api/') else None) for _,path,auth in calls))

    def test_agent_ids_steps_caller_and_tool_parent_preserved(self):
        with server('agent') as (url,_,calls):
            r=self.client('agent',url).detail('agent-runs',ID)
            self.assertEqual(r['data']['identifiers']['request_id'],OTHER)
            self.assertEqual(r['data']['caller_context']['execution_id'],'5')
            self.assertEqual(r['data']['error'],{'code':'ANSWER_REFERENCE_INVALID','stage':'finalize'})
            self.assertEqual(r['related']['steps']['data'][-1]['state'],'failed')
            self.assertEqual(r['related']['tool_call']['data']['parent_step_id'],'search')
            self.assertEqual(len(calls),3)
            self.assertNotIn(PRIVATE,json.dumps(r))

    def test_submission_is_not_execution_success(self):
        with server('data') as (url,routes,_):
            routes['/api/operations/'+OP]['status']='failed'
            routes['/api/operations/'+OP]['error_code']='REFINEMENT_FAILED'
            r=self.client('data',url).detail('refinement-runs',ID)
            self.assertIsNone(r['data']['state'])
            self.assertEqual(r['data']['states']['submission_status'],'delivered')
            self.assertEqual(r['related']['operation']['data']['state'],'failed')

    def test_native_missing_times_are_not_observation_times(self):
        with server('model') as (url,_,_):
            c=self.client('model',url)
            page=c.page('operations')['data']['items'][0]
            item=c.detail('operations',OP)['data']
            self.assertEqual(page['times']['created_at'],STAMP)
            self.assertIsNone(item['times']['created_at'])
            self.assertNotIn('created_at',item['time_fields_present'])

    def test_link_failure_keeps_domain_record_and_reports_partial(self):
        with server('data') as (url,routes,_):
            routes['/api/operations/'+OP]=(503,{'error_message':PRIVATE})
            r=self.client('data',url).detail('crawl-runs',ID)
            self.assertEqual(r['status'],'partial')
            self.assertEqual(r['data']['id'],ID)
            self.assertIsNone(r['related']['operation']['data'])

    def test_tool_wrong_parent_or_step_refused(self):
        for field,value in [('parent_agent_run_id',OTHER),('parent_step_id','generate')]:
            with server('agent') as (url,routes,_):
                routes['/api/tool-calls/'+TOOL][field]=value
                r=self.client('agent',url).detail('agent-runs',ID)
                self.assertEqual(r['status'],'partial')
                self.assertEqual(r['related']['tool_call']['error']['code'],'PARENT_MISMATCH')

    def test_operation_other_resource_refused(self):
        with server('data') as (url,routes,_):
            routes['/api/operations/'+OP]['resource_id']=OTHER
            r=self.client('data',url).detail('crawl-runs',ID)
            self.assertEqual(r['related']['operation']['error']['code'],'RESOURCE_MISMATCH')

    def test_detail_identity_mismatch(self):
        with server('model') as (url,routes,_):
            routes['/api/operations/'+OP]['operation_id']=OTHER
            r=self.client('model',url).detail('operations',OP)
            self.assertEqual(r['status'],'error')
            self.assertEqual(r['error']['code'],'IDENTITY_MISMATCH')
        with server('agent') as (url,routes,_):
            del routes['/api/agent-runs/'+ID]['request_id']
            r=self.client('agent',url).detail('agent-runs',ID)
            self.assertEqual(r['error']['code'],'INVALID_RESPONSE')

    def test_index_version_parent_filter_and_wrong_parent(self):
        with server('knowledge') as (url,routes,_):
            c=self.client('knowledge',url)
            self.assertEqual(c.page('index-versions',parent_id=ID)['status'],'ok')
            routes['/api/indexes/'+ID+'/versions']['items'][0]['index_id']=OTHER
            self.assertEqual(c.page('index-versions',parent_id=ID)['error']['code'],'PARENT_MISMATCH')

    def test_detail_only_resources_do_not_probe_guessed_routes(self):
        with server('knowledge') as (url,_,calls):
            self.assertEqual(self.client('knowledge',url).page('operations')['status'],'unsupported')
            self.assertEqual(calls,[])

    def test_empty_page_is_distinct_from_missing_items(self):
        with server('data') as (url,routes,_):
            c=self.client('data',url)
            routes['/api/operations']={'items':[]}
            r=c.page('operations')
            self.assertEqual(r['data']['page_count'],0)
            self.assertIsNone(r['data']['total_count'])
            routes['/api/operations']={}
            r=c.page('operations')
            self.assertEqual(r['status'],'error')
            self.assertIsNone(r['data'])

    def test_pagination_bounded_and_not_sorted_as_latest(self):
        with server('agent') as (url,routes,_):
            first=deepcopy(routes['/api/agent-runs']['items'][0])
            second=deepcopy(first);second['agent_run_id']=OTHER
            routes['/api/agent-runs?limit=1']={'items':[first],'next_cursor':ID}
            routes['/api/agent-runs?limit=1&cursor='+ID]={'items':[second],'next_cursor':''}
            c=self.client('agent',url)
            self.assertEqual(c.walk('agent-runs',1,1)['coverage'],'page_budget_reached')
            r=c.walk('agent-runs',1,2)
            self.assertEqual([x['id'] for x in r['items']],[ID,OTHER])
            self.assertEqual(r['coverage'],'end_of_cursor_chain')
            self.assertIsNone(r['total_count'])

    def test_later_page_error_preserves_partial_records(self):
        with server('agent') as (url,routes,_):
            item=deepcopy(routes['/api/agent-runs']['items'][0])
            routes['/api/agent-runs?limit=1']={'items':[item],'next_cursor':ID}
            routes['/api/agent-runs?limit=1&cursor='+ID]=(503,{'error':PRIVATE})
            r=self.client('agent',url).walk('agent-runs',1,2)
            self.assertEqual(r['status'],'partial')
            self.assertEqual(len(r['items']),1)
            self.assertEqual(r['failure']['error']['http_status'],503)

    def test_cursor_loop_and_duplicate_page_rejected(self):
        with server('agent') as (url,routes,_):
            r=routes['/api/agent-runs'];r['next_cursor']=ID
            out=self.client('agent',url).walk('agent-runs',1,3)
            self.assertEqual(out['status'],'partial')
            self.assertEqual(out['failure']['error']['code'],'CURSOR_LOOP')

    def test_readiness_failure_is_source_status_not_transport_success(self):
        with server('agent') as (url,routes,_):
            obj=routes['/api/status'];obj['ready']=False;obj['runtime']='unavailable'
            routes['/api/status']=(503,obj)
            report=self.client('agent',url).check()
            self.assertEqual(report['readiness']['status'],'ok')
            self.assertFalse(report['readiness']['data']['ready'])
            self.assertEqual(report['result'],'attention_required')

    def test_last_success_survives_failed_query_and_no_stale_data(self):
        state=Path(self.tmp.name)/'state'/'api.json'
        with server('data') as (url,routes,_):
            with success_cache(state) as times:
                stamp=self.client('data',url,success_times=times).page('operations')['last_success_at']
                self.assertIsNotNone(stamp)
            routes['/api/operations']=(503,{'error':PRIVATE})
            with success_cache(state) as times:
                r=self.client('data',url,success_times=times).page('operations')
            self.assertEqual(r['last_success_at'],stamp)
            self.assertIsNone(r['data']);self.assertIsNone(r['observed_at'])
            self.assertNotIn(PRIVATE,state.read_text())
            self.assertNotIn(url,state.read_text())
            self.assertEqual(state.stat().st_mode & 0o777,0o600)

    def test_auth_errors_are_redacted_and_distinct(self):
        for code,expected in [(401,'UNAUTHENTICATED'),(403,'FORBIDDEN'),(404,'NOT_FOUND'),(429,'RATE_LIMITED')]:
            with server('agent') as (url,routes,_):
                routes['/api/agent-runs']=(code,{'token':PRIVATE})
                r=self.client('agent',url).page('agent-runs')
                self.assertEqual(r['error']['code'],expected)
                self.assertNotIn(PRIVATE,json.dumps(r))

    def test_redirect_is_not_followed_and_proxy_is_ignored(self):
        with server('agent') as (url,routes,calls):
            routes['/api/agent-runs']=(302,{}, {'Location':url+'/capture'})
            with patch.dict(os.environ,{'HTTP_PROXY':'http://invalid.test:1','http_proxy':'http://invalid.test:1','NO_PROXY':''}):
                r=self.client('agent',url).page('agent-runs')
            self.assertEqual(r['error']['code'],'REDIRECT_REFUSED')
            self.assertEqual(len(calls),1)

    def test_bad_urls_and_remote_plaintext_credentials_fail_before_io(self):
        for url in ['http://user:pass@localhost','http://localhost/path','file:///tmp/a','http://localhost?a=1','http://localhost\n']:
            with self.assertRaises(ReadFailure):Transport(url)
        with self.assertRaises(ReadFailure) as ctx:Transport('http://192.0.2.1',str(self.token))
        self.assertEqual(ctx.exception.code,'HTTPS_REQUIRED_FOR_REMOTE_CREDENTIAL')

    def test_response_size_json_duplicates_nonjson_and_malformed(self):
        values=[(b'{"status":"ok","status":"failed"}','INVALID_RESPONSE'),
                (b'{','INVALID_RESPONSE'), (b'[]','INVALID_RESPONSE'),
                (b'{"value":NaN}','INVALID_RESPONSE')]
        for body,code in values:
            with server('data',{'/health':body}) as (url,_,_):
                self.assertEqual(self.client('data',url).health()['error']['code'],code)
        with server('data',{'/health':b' '*100}) as (url,_,_):
            t=Transport(url,max_bytes=20)
            with self.assertRaises(ReadFailure) as ctx:t.get('/health')
            self.assertEqual(ctx.exception.code,'RESPONSE_TOO_LARGE')
        with server('data',{'/health':(200,{}, {'Content-Type':'text/html'})}) as (url,_,_):
            self.assertEqual(self.client('data',url).health()['error']['code'],'INVALID_CONTENT_TYPE')

    def test_timeout_does_not_retry(self):
        def slow(path):time.sleep(0.25);return {'status':'ok'}
        with server('data',{'/health':slow}) as (url,_,calls):
            r=self.client('data',url,timeout=0.1).health()
            self.assertEqual(r['error']['code'],'TIMEOUT')
            self.assertEqual(len(calls),1)

    def test_unreachable_not_zero(self):
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        r=self.client('data','http://127.0.0.1:'+str(port),timeout=0.1).page('operations')
        self.assertEqual(r['status'],'error');self.assertIsNone(r['data'])

    def test_state_symlink_and_concurrent_writer_refused(self):
        state=Path(self.tmp.name)/'state'/'api.json'
        with success_cache(state):
            with self.assertRaises(ReadFailure) as ctx:
                with success_cache(state):pass
            self.assertEqual(ctx.exception.code,'STATE_BUSY')
        state.unlink();state.symlink_to(self.token)
        with self.assertRaises(ReadFailure):
            with success_cache(state):pass

    def test_cli_get_is_readonly_and_saves_no_raw_data(self):
        with server('agent') as (url,_,calls):
            conf=Path(self.tmp.name)/'config.json'
            conf.write_text(json.dumps({'schema':1,'platforms':{'agent':{'url':url,'token_file':str(self.token)}}}))
            cmd=[sys.executable,'-B',str(ROOT/'src/ops/platform/ops/platform_status.py'),'get','--platform','agent','--resource','agent-runs','--id',ID,'--config',str(conf),'--state-file',str(Path(self.tmp.name)/'state'/'times.json')]
            r=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads(r.stdout)['data']['state'],'failed')
            self.assertNotIn(PRIVATE,r.stdout+r.stderr)
            self.assertTrue(all(m=='GET' for m,_,_ in calls))

    def test_invalid_cli_id_does_not_make_request(self):
        with server('data') as (url,_,calls):
            conf=Path(self.tmp.name)/'config.json'
            conf.write_text(json.dumps({'schema':1,'platforms':{'data':{'url':url}}}))
            cmd=[sys.executable,'-B',str(ROOT/'src/ops/platform/ops/platform_status.py'),'get','--platform','data','--resource','operations','--id','../secret','--config',str(conf),'--state-file',str(Path(self.tmp.name)/'state'/'times.json')]
            r=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(r.returncode,1);self.assertEqual(calls,[])

    def test_missing_credential_does_not_hide_public_gateway_health(self):
        with server('agent') as (url,_,calls):
            self.token.unlink()
            c=self.client('agent',url)
            self.assertEqual(c.health()['status'],'ok')
            self.assertEqual(c.page('agent-runs')['error']['code'],'CREDENTIAL_UNAVAILABLE')
            self.assertEqual(len(calls),1)

    def test_truncated_http_response_is_not_success(self):
        with server('data',{'/health':(200,b'{"status":"ok"}',{'Content-Length':'80'})}) as (url,_,_):
            r=self.client('data',url).health()
            self.assertEqual(r['error']['code'],'INCOMPLETE_RESPONSE')

    def test_cli_check_four_platforms_and_failure_isolation(self):
        with ExitStack() as stack:
            conf={'schema':1,'platforms':{}}
            servers={}
            for name in ('model','data','knowledge','agent'):
                url,routes,calls=stack.enter_context(server(name));servers[name]=(routes,calls)
                conf['platforms'][name]={'url':url}
            conf['platforms']['agent']['token_file']=str(self.token)
            path=Path(self.tmp.name)/'config.json';path.write_text(json.dumps(conf))
            state=Path(self.tmp.name)/'state'/'times.json'
            cmd=[sys.executable,'-B',str(ROOT/'src/ops/platform/ops/platform_status.py'),'check','--config',str(path),'--state-file',str(state),'--agent-run-id',ID]
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            out=json.loads(result.stdout)
            self.assertEqual(out['platforms']['agent']['pinned_agent_run']['state'],'failed')
            self.assertNotIn(PRIVATE,result.stdout)
            self.assertEqual(out['platforms']['model']['readiness'],'unsupported')
            servers['data'][0]['/api/operations']=(503,{'error':PRIVATE})
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(result.returncode,2,result.stdout+result.stderr)
            out=json.loads(result.stdout)
            self.assertEqual(out['platforms']['model']['result'],'passed')
            failed=out['platforms']['data']['resources']['operations']
            self.assertEqual(failed['status'],'error')
            self.assertIsNone(failed['sampled_count'])
            self.assertIsNotNone(failed['last_success_at'])
            self.assertTrue(all(method=='GET' for _,calls in servers.values() for method,_,_ in calls))


if __name__=='__main__':
    unittest.main()
