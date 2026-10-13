"""O03 routes and observation joins exercised against local HTTP source fixtures."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import tempfile
import contextlib
import io
import socket
import threading
import time
import unittest
import urllib.parse
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.ops.platform.client import Client
from src.ops.platform.contracts import ReadFailure
from src.ops.platform.observations import Langfuse, Prometheus, QUERIES, trace_id, span_id
from src.ops.platform.server import create_app
from src.ops.platform.ops.manage import prepare, existing
from src.ops.platform.tests.test_status import server, fixtures, ID, OP, VERSION, TOOL, OTHER, PRIVATE

OPS_KEY='ops-test-access-key-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ'
AGENT_KEY='agent-test-key-0123456789'
LF_KEY='sk-lf-test-0123456789'
PROJECT='agent-traces-fixture-project'
STAMP='2026-09-26T13:36:44.123456Z'


def observations():
    rows=[];trace=trace_id(ID)
    parents={'agent.execute':None,**{'step.'+s:'agent.execute' for s in ('validate','search','context','generate','finalize')},
             'tool.execute':'step.search','model.embedding':'tool.execute','knowledge.search':'tool.execute','model.chat':'step.generate'}
    for name,parent in parents.items():
        is_tool=name in ('tool.execute','model.embedding','knowledge.search')
        prefix='tool:'+TOOL+':' if is_tool else ''
        pp='tool:'+TOOL+':' if parent=='tool.execute' else ''
        state='failed' if name in ('agent.execute','step.finalize') else 'succeeded'
        meta={'agent_run_id':ID,'scope':'runtime_execution','request_id':OTHER,'state':state,
              'tool_call_id':TOOL,'tool_version_id':VERSION,'agent_version_id':VERSION,'private':PRIVATE}
        if state=='failed':meta['error_code']='ANSWER_REFERENCE_INVALID'
        if name=='model.chat':meta.update(prompt_tokens='500',completion_tokens='176',total_tokens='676')
        rows.append({'id':span_id(trace,prefix+name),'traceId':trace,'projectId':PROJECT,'name':name,
            'parentObservationId':span_id(trace,pp+parent) if parent else None,'metadata':meta,
            'environment':'agent-platform','startTime':STAMP,'endTime':'2026-09-26T13:36:45.123456Z',
            'type':{'agent.execute':'AGENT','tool.execute':'TOOL','model.embedding':'EMBEDDING','knowledge.search':'RETRIEVER','model.chat':'GENERATION'}.get(name,'SPAN'),
            'level':'ERROR' if state=='failed' else 'DEFAULT','input':PRIVATE,'output':PRIVATE})
    return {'data':rows,'meta':{'cursor':None}}


@contextlib.contextmanager
def managed_discovery_fixture():
    """Use the existing loopback discovery fixture for first-install checks."""
    from src.ops.platform.tests.test_discovery_bootstrap import BootstrapTests
    fixture=BootstrapTests()
    try:
        fixture.setUp()
        yield fixture
    finally:
        fixture.doCleanups()


class Environment:
    def __init__(self,port=17100):
        self.stack=ExitStack();self.tmp=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.sources={};self.routes={};self.calls={}
        for name in ('model','data','knowledge','agent'):
            url,routes,calls=self.stack.enter_context(server(name))
            self.sources[name]={'url':url};self.routes[name]=routes;self.calls[name]=calls
        self.token=self.tmp/'agent-key';self.token.write_text(AGENT_KEY);self.token.chmod(0o600)
        self.sources['agent']['token_file']=str(self.token)
        for s in self.routes['agent']['/api/agent-runs/'+ID+'/steps']['items']:
            s.update(started_at=STAMP,completed_at='2026-09-26T13:36:45.123456Z')
        self.access=self.tmp/'access';self.access.write_text(OPS_KEY);self.access.chmod(0o600)
        self.lf=self.tmp/'langfuse.json';self.lf.write_text(json.dumps({'schema':1,'project_id':PROJECT,'public_key':'pk-lf-test-0123456789','secret_key':LF_KEY}));self.lf.chmod(0o600)
        self.metric_value='42.5'
        self.metric_label='fixture-container'
        self.metric_empty=False
        self.metric_warnings=False
        self.trace_rows=observations()
        def metrics(path):
            q=urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            assert set(q)=={'query','timeout','limit'}
            expression=q['query'][0]
            assert expression in [v[2] for v in QUERIES.values()]
            labels={'instance':'127.0.0.1:16311','name':self.metric_label,'private':PRIVATE}
            if 'DCGM' in expression:labels.update(__name__='DCGM_FI_DEV_GPU_UTIL',modelName='RTX 4090',gpu='0')
            if expression.startswith('up'):labels.update(job='host')
            value='1' if expression.startswith('up') else self.metric_value
            return {'status':'success','warnings':['private upstream warning '+PRIVATE] if self.metric_warnings else [],
                'data':{'resultType':'vector','result':[] if self.metric_empty else [{'metric':labels,'value':[time.time(),value]}]}}
        purl,self.prom_routes,self.prom_calls=self.stack.enter_context(server('prometheus',{'/api/v1/query':metrics}))
        def trace(path):
            q=urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            assert q['fields']==['core,basic,metadata'] and q['traceId']==[trace_id(ID)]
            assert 'fromStartTime' in q and 'toStartTime' in q
            return self.trace_rows
        lurl,self.lf_routes,self.lf_calls=self.stack.enter_context(server('langfuse',{'/api/public/v2/observations':trace}))
        self.config={'schema':1,'platforms':self.sources,'prometheus_url':purl,'langfuse_url':lurl,'langfuse_file':str(self.lf),
            'access_file':str(self.access),'state_file':str(self.tmp/'cache/api-success.json'),'public_url':'http://localhost:'+str(port),
            'bind':'127.0.0.1','port':port,'timeout':0.2,'tls_cert':None,'tls_key':None}
        self.app=create_app(self.config)

    def close(self):self.stack.close()
    def client(self):return self.stack.enter_context(TestClient(self.app,base_url=self.config['public_url']))


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.env=Environment();self.addCleanup(self.env.close);self.client=self.env.client()
        self.auth={'Authorization':'Bearer '+OPS_KEY}

    def get(self,path):return self.client.get(path,headers=self.auth)

    def login(self,password=OPS_KEY,origin=None):
        return self.client.post('/api/session',json={'password':password},headers={'Origin':origin or self.env.config['public_url']})

    def test_login_cookie_logout_and_separate_ops_authority(self):
        self.assertEqual(self.client.get('/api/overview').status_code,401)
        self.assertEqual(self.client.get('/api/overview',headers={'Authorization':'Bearer '+AGENT_KEY}).status_code,401)
        r=self.login();self.assertEqual(r.status_code,200)
        self.assertIn('HttpOnly',r.headers['set-cookie']);self.assertIn('SameSite=strict',r.headers['set-cookie'])
        self.assertEqual(self.client.get('/api/overview').status_code,200)
        cookie=self.client.cookies.get('ops_session')
        self.assertEqual(self.client.delete('/api/session',headers={'Origin':self.env.config['public_url']}).status_code,200)
        self.assertFalse(self.env.app.state.auth.check(cookie))
        self.assertEqual(self.client.get('/api/overview').status_code,401)

    def test_host_origin_and_external_browser_are_rejected(self):
        self.assertEqual(self.client.get('/healthz',headers={'Host':'malicious.example'}).status_code,400)
        self.assertEqual(self.login(origin='https://malicious.example').status_code,403)
        self.assertEqual(self.client.post('/api/session',json={'password':OPS_KEY}).status_code,403)
        self.assertEqual(self.client.options('/api/overview',headers={'Origin':'https://malicious.example'}).status_code,405)

    def test_login_failure_rate_limit_body_bounds_and_no_input_echo(self):
        for i in range(10):self.assertEqual(self.login(PRIVATE).status_code,401)
        r=self.login();self.assertEqual(r.status_code,429);self.assertNotIn(OPS_KEY,r.text)
        self.env.app.state.auth.attempts.clear()
        r=self.client.post('/api/session',content='x'*3000,headers={'Content-Type':'application/json','Origin':self.env.config['public_url']})
        self.assertEqual(r.status_code,400);self.assertNotIn('x'*30,r.text)
        r=self.client.post('/api/session',json={'password':{'private':PRIVATE}},headers={'Origin':self.env.config['public_url']})
        self.assertEqual(r.status_code,401);self.assertNotIn(PRIVATE,r.text)

    def test_sessions_expire_and_cookie_is_secure_on_https(self):
        self.login();cookie=self.client.cookies.get('ops_session')
        with patch('src.ops.platform.server.time.monotonic',return_value=time.monotonic()+9*3600):
            self.assertFalse(self.env.app.state.auth.check(cookie))
        cfg=dict(self.env.config,public_url='https://localhost:17100')
        with TestClient(create_app(cfg),base_url=cfg['public_url']) as c:
            r=c.post('/api/session',json={'password':OPS_KEY},headers={'Origin':cfg['public_url']})
            self.assertIn('Secure',r.headers['set-cookie'])

    def test_static_security_headers_and_health_no_upstreams(self):
        for path in ('/','/app.js','/style.css','/healthz'):
            r=self.client.get(path);self.assertEqual(r.status_code,200)
            self.assertEqual(r.headers['cache-control'],'no-store')
            self.assertIn("frame-ancestors 'none'",r.headers['content-security-policy'])
        self.assertFalse(any(self.env.calls.values()))
        self.assertEqual(self.client.get('/settings.py').status_code,404)
        self.assertEqual(self.client.get('/docs').status_code,404)

    def test_four_sources_resource_catalog_and_metadata_only(self):
        overview=self.get('/api/overview').json()
        self.assertEqual(len(overview['platforms']),4)
        self.assertTrue(overview['platforms']['agent']['readiness']['data']['ready'])
        self.assertEqual(overview['platforms']['model']['readiness']['status'],'unsupported')
        self.assertEqual(len(self.get('/api/catalog').json()['platforms']),4)
        for name,source in self.env.calls.items():
            self.assertTrue(all(m=='GET' for m,p,a in source))
            self.assertTrue(all(a==('Bearer '+AGENT_KEY if name=='agent' and p.startswith('/api/') else None) for m,p,a in source))

    def test_gpu_fields_sharing_labels_keep_values_units(self):
        from src.ops.platform.observations import METRIC_UNITS
        values=[0,24576,4096,20480,46,72.5]
        labels={'job':'gpu','instance':'127.0.0.1:16313','UUID':'GPU-synthetic',
                'gpu':'0','device':'nvidia0','modelName':'RTX 4090'}
        # HTTP projection check only; actual PromQL evaluation has a separate promtool fixture.
        self.env.prom_routes['/api/v1/query']={'status':'success','data':{
            'resultType':'vector','result':[
                {'metric':dict(labels,__name__=name,private='excluded-source-label'),
                 'value':[time.time(),str(value)]}
                for name,value in zip(METRIC_UNITS,values)]}}
        result=self.env.app.state.reads.read(lambda times:
            Prometheus(self.env.config['prometheus_url'],times).metric('gpu'))
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['data']['count'],6)
        for row,(name,unit),value in zip(result['data']['items'],METRIC_UNITS.items(),values):
            self.assertEqual(row['labels']['__name__'],name)
            self.assertEqual(row['unit'],unit)
            self.assertEqual(row['value'],value)
            self.assertEqual(row['value_status'],'observed')
            self.assertNotIn('private',row['labels'])

    def test_business_failed_is_a_successful_read(self):
        r=self.get('/api/platforms/agent/agent-runs/'+ID).json()
        self.assertEqual(r['status'],'ok');self.assertEqual(r['data']['state'],'failed')
        self.assertEqual(r['data']['error']['code'],'ANSWER_REFERENCE_INVALID')
        self.assertEqual(r['related']['steps']['data'][-1]['state'],'failed')
        self.assertNotIn(PRIVATE,json.dumps(r))

    def test_bad_arguments_arbitrary_queries_and_mutations_do_not_reach_sources(self):
        for path in ('/api/platforms/model/unknown','/api/platforms/model/operations/not-a-uuid',
                     '/api/platforms/model/operations?limit=0','/api/platforms/model/operations?url=http://evil',
                     '/api/platforms/model/operations?limit=1&limit=2','/api/resources?query=up'):
            self.assertEqual(self.get(path).status_code,400,path)
        for verb in ('post','delete','patch','put'):
            r=getattr(self.client,verb)('/api/platforms/agent/agent-runs',headers=self.auth)
            self.assertEqual(r.status_code,405)
        self.assertFalse(any(self.env.calls.values()))

    def test_unsupported_and_empty_list_are_distinct(self):
        r=self.get('/api/platforms/agent/tool-calls').json();self.assertEqual(r['status'],'unsupported')
        self.env.routes['model']['/api/training-runs']={'items':[],'next_cursor':''}
        r=self.get('/api/platforms/model/training-runs').json();self.assertEqual(r['status'],'ok');self.assertEqual(r['data']['items'],[])

    def test_cursor_and_parent_are_forwarded_without_global_count(self):
        self.env.routes['model']['/api/operations']={'items':[],'next_cursor':''}
        r=self.get('/api/platforms/model/operations?cursor=opaque%3D%3D').json()
        self.assertEqual(r['status'],'ok');self.assertIsNone(r['data']['total_count'])
        self.assertIn('cursor=opaque%3D%3D',self.env.calls['model'][-1][1])
        r=self.get('/api/platforms/knowledge/index-versions?parent_id='+ID).json();self.assertEqual(r['status'],'ok')

    def test_outage_is_isolated_and_last_success_survives(self):
        first=self.get('/api/overview').json()['platforms']['model']['gateway']['last_success_at']
        self.env.routes['model']['/health']=(503,{'secret':PRIVATE})
        r=self.get('/api/overview').json()
        failed=r['platforms']['model']['gateway']
        self.assertEqual(failed['status'],'error');self.assertIsNone(failed['data']);self.assertEqual(failed['last_success_at'],first)
        self.assertEqual(r['platforms']['agent']['gateway']['status'],'ok')
        state=Path(self.env.config['state_file']).read_text();self.assertNotIn(PRIVATE,state);self.assertNotIn('http',state)

    def test_metrics_projection_units_nan_empty_warnings_and_get_only(self):
        data=self.get('/api/resources').json();self.assertEqual(len(data['metrics']),7)
        self.assertEqual(data['metrics']['gpu']['data']['items'][0]['unit'],'%')
        self.assertNotIn(PRIVATE,json.dumps(data));self.assertTrue(all(m=='GET' and a is None for m,p,a in self.env.prom_calls))
        self.env.metric_value='NaN';data=self.get('/api/resources').json();self.assertIsNone(data['metrics']['host_cpu']['data']['items'][0]['value'])
        self.env.metric_empty=True;data=self.get('/api/resources').json();self.assertEqual(data['metrics']['gpu']['data']['count'],0)
        self.env.metric_warnings=True;data=self.get('/api/resources').json();self.assertEqual(data['metrics']['gpu']['status'],'partial')
        self.assertNotIn(PRIVATE,json.dumps(data))

    def test_prometheus_failure_does_not_become_zero(self):
        self.env.prom_routes['/api/v1/query']=(503,{'error':PRIVATE})
        d=self.get('/api/resources').json()
        self.assertTrue(all(v['status']=='error' and v['data'] is None for v in d['metrics'].values()))

    def test_trace_ten_spans_original_failure_and_no_content(self):
        d=self.get('/api/agent-runs/'+ID+'/trace').json()
        self.assertEqual(d['status'],'ok',d);self.assertEqual(len(d['data']['items']),10)
        self.assertEqual(d['data']['business_state'],'failed');self.assertEqual(d['data']['missing_spans'],[])
        self.assertNotIn(PRIVATE,json.dumps(d));self.assertNotIn(LF_KEY,json.dumps(d))
        self.assertTrue(all(m=='GET' and a.startswith('Basic ') for m,p,a in self.env.lf_calls))
        self.assertEqual(next(r for r in d['data']['items'] if r['name']=='model.chat')['counts']['completion_tokens'],176)

    def test_trace_empty_partial_and_upstream_auth_failure_distinct(self):
        self.env.trace_rows={'data':[],'meta':{}}
        self.assertEqual(self.get('/api/agent-runs/'+ID+'/trace').json()['status'],'not_observed')
        self.env.trace_rows=observations();self.env.trace_rows['data'].pop()
        d=self.get('/api/agent-runs/'+ID+'/trace').json();self.assertEqual(d['status'],'partial');self.assertIn('model.chat',d['data']['missing_spans'])
        self.env.lf_routes['/api/public/v2/observations']=(401,{'secret':PRIVATE})
        d=self.get('/api/agent-runs/'+ID+'/trace').json();self.assertEqual(d['status'],'error');self.assertEqual(d['error']['code'],'UNAUTHENTICATED')

    def test_wrong_trace_identity_parent_version_state_type_are_refused(self):
        for field,value in [('traceId','0'*32),('projectId','wrong-project'),('parentObservationId','0'*16),('type','SPAN')]:
            self.env.trace_rows=observations();self.env.trace_rows['data'][0][field]=value
            d=self.get('/api/agent-runs/'+ID+'/trace').json();self.assertEqual(d['status'],'error',(field,d));self.assertIsNone(d['data'])
        for field,value in [('agent_version_id',ID),('request_id',ID),('state','succeeded')]:
            self.env.trace_rows=observations();self.env.trace_rows['data'][0]['metadata'][field]=value
            self.assertEqual(self.get('/api/agent-runs/'+ID+'/trace').json()['status'],'error')

    def test_trace_does_not_query_without_source_record_or_times(self):
        self.env.routes['agent']['/api/agent-runs/'+ID]=(404,{})
        d=self.get('/api/agent-runs/'+ID+'/trace').json();self.assertEqual(d['error']['code'],'AGENT_RECORD_UNAVAILABLE')
        self.assertEqual(self.env.lf_calls,[])

    def test_trace_duplicate_or_pagination_never_claims_complete(self):
        self.env.trace_rows['data'].append(deepcopy(self.env.trace_rows['data'][0]))
        self.assertEqual(self.get('/api/agent-runs/'+ID+'/trace').json()['error']['code'],'DUPLICATE_IDENTITY')
        self.env.trace_rows=observations();self.env.trace_rows['meta']['cursor']='more'
        self.assertEqual(self.get('/api/agent-runs/'+ID+'/trace').json()['status'],'partial')

    def test_prepare_is_idempotent_keys_not_copied_and_changed_url_refused(self):
        directory=self.env.tmp/'managed'
        with managed_discovery_fixture():
            r=prepare(directory,'http://localhost:17100',self.env.token,self.env.lf)
            self.assertEqual(r['result'],'prepared')
            key=(directory/'access-key').read_text()
            r=prepare(directory);self.assertEqual(r['result'],'reused');self.assertEqual((directory/'access-key').read_text(),key)
            with self.assertRaises(ReadFailure):prepare(directory,'http://localhost:17101')
            data=''.join(p.read_text() for p in directory.iterdir() if p.is_file())
            self.assertNotIn(AGENT_KEY,data);self.assertNotIn(LF_KEY,data)
            self.assertEqual((directory/'access-key').stat().st_mode&0o777,0o600)

    def test_live_uvicorn_checker_and_readiness_failure_exit(self):
        import uvicorn
        from src.ops.platform.ops.check_console import main
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        cfg=dict(self.env.config,port=port,public_url='http://localhost:'+str(port))
        cfgfile=self.env.tmp/'checker-config.json';cfgfile.write_text(json.dumps(cfg))
        serving=uvicorn.Server(uvicorn.Config(create_app(cfg),host='127.0.0.1',port=port,log_level='error',access_log=False,proxy_headers=False))
        thread=threading.Thread(target=serving.run,daemon=True);thread.start()
        try:
            for _ in range(100):
                if serving.started:break
                time.sleep(0.01)
            self.assertTrue(serving.started)
            def check():
                out=io.StringIO()
                with patch('sys.argv',['check_console','--config',str(cfgfile),'--agent-run-id',ID]),contextlib.redirect_stdout(out):code=main()
                return code,json.loads(out.getvalue())
            code,result=check();self.assertEqual(code,0,result);self.assertEqual(result['trace']['span_count'],10)
            self.assertEqual(result['agent']['state'],'failed')
            self.env.routes['agent']['/api/status']=(503,{'platform':'agent','ready':False,'database':'unavailable','runtime':'ready','request_id':OTHER})
            code,result=check();self.assertEqual(code,2);self.assertFalse(result['overview']['agent']['readiness']['ready'])
            self.env.routes['agent']['/api/status']=(401,{})
            code,result=check();self.assertEqual(code,2)
        finally:
            serving.should_exit=True;thread.join(5)

    def test_managed_mount_environment_tampering_is_refused(self):
        directory=self.env.tmp/'managed'
        with managed_discovery_fixture():
            prepare(directory,'http://localhost:17100',self.env.token,self.env.lf)
            with (directory/'compose.env').open('a') as f:f.write("OPS_AGENT_TOKEN_FILE='/other/secret'\n")
            with self.assertRaises(ReadFailure) as exc:existing(directory)
            self.assertEqual(exc.exception.code,'COMPOSE_ENV_MISMATCH')


if __name__=='__main__':unittest.main()
