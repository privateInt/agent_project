"""Q04b own SQLite, real QA HTTP callbacks and Node evaluation; no n8n/GPU."""
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from apps.document_qa.api import make_server
from apps.document_qa.contracts import fingerprint
from apps.document_qa.intake import UploadError, encode_multipart
from apps.document_qa.uploads import UploadStore
from apps.document_qa.workflows import WorkflowStore, signed_headers, canonical
from apps.document_qa.refinement import RefinementStore
from apps.document_qa.refinement_flow import RefinementFlow, flow_lock, FLOW_SECONDS, MAX_STEPS, MAX_GENERATIONS
from apps.document_qa.refinement_flow_template import build, VERIFY, OWNER, RESULT
from apps.document_qa.workflow_template import matches
from apps.document_qa.tests.test_workflows import FakeEngine
from apps.document_qa.tests.test_refinement import Uploaded, FakeData, ORIGIN, DATA, running


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='qa-refinement-flow-');self.root=Path(self.temp.name)/'uploads'
        self.uploads=UploadStore.initialize(self.root);self.entry=WorkflowStore.initialize(self.uploads)
        self.refinement=RefinementStore.initialize(self.uploads);self.flow=RefinementFlow.initialize(self.uploads)
        self.uploaded=Uploaded();self.data=FakeData(self.uploaded)
        self.entry_engine=FakeEngine(self.entry);self.engine=FakeEngine(self.flow)
        self.engine.resources=self.entry_engine.resources
        self.key=Path(self.temp.name)/'key.json';self.key.write_text(json.dumps({'url':self.engine.origin,'api_key':'test-only-key'}));self.key.chmod(0o600)
        self.epoch=1000.0;self.flow.clock=lambda:self.epoch
    def tearDown(self):self.temp.cleanup()
    def prepare(self):
        self.entry.prepare('http://127.0.0.1:17100',self.key,self.entry_engine)
        self.flow.prepare('http://127.0.0.1:17100',self.key,self.engine)
    def job(self,data=DATA):
        job=self.uploads.accept(str(uuid4()),'자료','x.txt',data,ORIGIN)
        return self.uploads.dispatch(job['job_id'],self.uploaded)
    def started(self,job=None):
        self.prepare();job=job or self.job();self.entry.dispatch(job['job_id'],self.entry_engine)
        self.flow.dispatch(job['job_id'],self.engine)
        row=self.flow.current(job['job_id']);receipt=json.loads(row['receipt'])
        grant={k:receipt[k] for k in ('schema','handoff_id','job_id','generation','workflow_id','definition_sha256','grant_id')}
        grant['execution_id']=receipt['first_execution_id']
        return job,grant,receipt
    def posts(self):return [c for c in self.data.calls if c[0]=='POST']
    def engine_posts(self):return [c for c in self.engine.calls if c[0]=='POST']
    def complete(self,grant):
        run_id=self.refinement.job(grant['job_id'])['receipt']['resource_id']
        self.data.runs[run_id].update(status='succeeded',phase='completed')
        return self.flow.step(grant,lambda:self.data)


class StorageTests(Fixture):
    def test_initialization_is_local_additive_and_preserves_all_existing_keys(self):
        keys={p.name:p.read_bytes() for p in self.root.iterdir() if 'token' in p.name or 'secret' in p.name}
        before=self.uploads.library();again=RefinementFlow.initialize(UploadStore(self.root))
        self.assertEqual(before,self.uploads.library());self.assertEqual(keys,{p.name:p.read_bytes() for p in self.root.iterdir() if p.name in keys})
        self.assertFalse(again.status()['workflow_configured']);self.assertEqual(self.engine.calls,[])
        with self.uploads.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual([db.execute('SELECT version FROM '+table).fetchone()[0] for table in ('workflow_metadata','refinement_metadata','refinement_flow_metadata')],[1,1,1])
        self.assertTrue(all((self.root/n).stat().st_mode&0o777==0o600 for n in ('refinement-workflow-api-token','refinement-workflow-hook-secret')))

    def test_flow_requires_initialized_entry_and_refinement_before_writing(self):
        root=Path(self.temp.name)/'uninitialized';uploads=UploadStore.initialize(root)
        with self.assertRaises(UploadError):RefinementFlow.initialize(uploads)
        with uploads.connect() as db:self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='refinement_flow_metadata'").fetchone())

    def test_prepare_preserves_entry_definition_setup_credentials_and_handoffs(self):
        self.entry.prepare('http://127.0.0.1:17100',self.key,self.entry_engine)
        job=self.job();self.entry.dispatch(job['job_id'],self.entry_engine)
        before=copy.deepcopy(self.entry.setup());handoff=self.entry.handoff(job['job_id'])
        resources=copy.deepcopy(self.entry_engine.resources)
        self.flow.prepare('http://127.0.0.1:17100',self.key,self.engine)
        self.assertEqual(before,self.entry.setup());self.assertEqual(handoff,self.entry.handoff(job['job_id']))
        for kind in resources:
            for key,value in resources[kind].items():self.assertEqual(value,self.engine.resources[kind][key])
        self.assertEqual(len(self.engine.resources['workflows']),2)

    def test_unprepared_entry_or_changed_origin_blocks_remote_registration(self):
        with self.assertRaises(UploadError):self.flow.prepare('http://127.0.0.1:17100',self.key,self.engine)
        self.assertEqual(self.engine_posts(),[])
        self.entry.prepare('http://127.0.0.1:17100',self.key,self.entry_engine)
        with self.assertRaises(UploadError):self.flow.prepare('http://127.0.0.1:17101',self.key,self.engine)
        self.assertEqual(self.engine_posts(),[])

    def test_repeat_prepare_reuses_owned_flow_and_keys(self):
        self.prepare();before=copy.deepcopy(self.flow.setup());posts=len(self.engine_posts())
        self.prepare();self.assertEqual(before,self.flow.setup());self.assertEqual(posts,len(self.engine_posts()))

    def test_lost_create_reconciles_owned_flow_without_duplicate(self):
        self.entry.prepare('http://127.0.0.1:17100',self.key,self.entry_engine)
        self.engine.lost_create='workflows'
        with self.assertRaises(UploadError):self.flow.prepare('http://127.0.0.1:17100',self.key,self.engine)
        self.flow.prepare('http://127.0.0.1:17100',self.key,self.engine)
        self.assertEqual(len(self.engine.resources['workflows']),2)

    def test_changed_published_definition_blocks_delivery_without_overwrite(self):
        self.prepare();job=self.job();self.entry.dispatch(job['job_id'],self.entry_engine)
        flow=self.engine.resources['workflows'][self.flow.setup()['resources']['workflow']['id']]
        flow['nodes'][6]['parameters']['url']='https://never-call.invalid'
        with self.assertRaises(UploadError):self.flow.dispatch(job['job_id'],self.engine)
        self.assertFalse(any(c[1].startswith('/webhook/') for c in self.engine_posts()))
        self.assertEqual(self.data.calls,[])

    def test_unconfirmed_source_or_entry_receipt_cannot_create_intent(self):
        self.prepare();job=self.job()
        with self.assertRaises(UploadError):self.flow.dispatch(job['job_id'],self.engine)
        self.assertIsNone(self.flow.current(job['job_id']));self.assertEqual(self.data.calls,[])

    def test_unknown_trigger_never_automatically_reposts_and_explicit_retry_reuses_body(self):
        self.prepare();job=self.job();self.entry.dispatch(job['job_id'],self.entry_engine)
        self.engine.no_callback=True;self.flow.dispatch(job['job_id'],self.engine)
        body=json.loads(self.flow.current(job['job_id'])['body']);count=len(self.engine_posts())
        self.flow.dispatch(job['job_id'],self.engine);self.assertEqual(count,len(self.engine_posts()))
        self.engine.no_callback=False;self.flow.dispatch(job['job_id'],self.engine,retry=True)
        self.assertEqual(body,json.loads(self.flow.current(job['job_id'])['body']))
        self.assertEqual(self.flow.job(job['job_id'])['delivery_state'],'accepted');self.assertEqual(self.data.calls,[])

    def test_lost_reply_after_callback_retains_receipt_without_new_execution(self):
        self.prepare();job=self.job();self.entry.dispatch(job['job_id'],self.entry_engine)
        self.engine.lost_reply=True;self.flow.dispatch(job['job_id'],self.engine)
        count=len(self.engine_posts());self.flow.dispatch(job['job_id'],self.engine,retry=True)
        self.assertEqual(count,len(self.engine_posts()));self.assertEqual(self.flow.job(job['job_id'])['delivery_state'],'accepted')

    def test_duplicate_execution_gets_same_receipt_but_no_domain_grant(self):
        job,grant,receipt=self.started();body=json.loads(self.flow.current(job['job_id'])['body'])
        duplicate={**body,'workflow_id':receipt['workflow_id'],'definition_sha256':receipt['definition_sha256'],'execution_id':'duplicate'}
        self.assertEqual(self.flow.accept_receipt(duplicate),receipt)
        with self.assertRaises(UploadError):self.flow.step({**grant,'execution_id':'duplicate'},lambda:self.data)
        self.assertEqual(self.data.calls,[])

    def test_wrong_grant_job_source_execution_and_generation_refused_before_Data(self):
        job,grant,receipt=self.started()
        for key,value in [('job_id',str(uuid4())),('grant_id',str(uuid4())),('handoff_id',str(uuid4())),
                          ('generation',True),('workflow_id','other'),('execution_id','other'),('definition_sha256','f'*64),('schema',True)]:
            with self.subTest(key=key),self.assertRaises(UploadError):self.flow.step({**grant,key:value},lambda:self.data)
        with self.assertRaises(UploadError):self.flow.step({**grant,'ocr':{}},lambda:self.data)
        self.assertEqual(self.data.calls,[])

    def test_owner_starts_once_then_only_observes_and_retains_terminal_reference(self):
        job,grant,_=self.started();first=self.flow.step(grant,lambda:self.data)
        self.assertTrue(first['continue_wait']);self.assertEqual(len(self.posts()),1)
        self.flow.step(grant,lambda:self.data);self.assertEqual(len(self.posts()),1)
        result=self.complete(grant);self.assertEqual(result['state'],'canonical_verified');self.assertFalse(result['continue_wait'])
        count=len(self.data.calls);self.flow.step(grant,lambda:self.data)
        self.assertEqual(len(self.data.calls),count);self.assertFalse(result['question_ready'])
        public=canonical(self.flow.project_job(self.uploads.job(job['job_id'])))
        self.assertNotIn(grant['grant_id'],public);self.assertNotIn('검증할 문서 본문',public)

    def test_early_step_while_webhook_reply_is_in_flight_waits_without_effects(self):
        job,grant,_=self.started()
        with flow_lock(self.root,job['job_id']):
            result=self.flow.step(grant,lambda:self.data)
        self.assertTrue(result['continue_wait']);self.assertEqual(result['steps'],0);self.assertEqual(self.data.calls,[])

    def test_library_lane_wait_does_not_start_second_document(self):
        job,grant,_=self.started();self.flow.step(grant,lambda:self.data)
        other=self.job(DATA+b"second");self.entry.dispatch(other['job_id'],self.entry_engine);self.flow.dispatch(other['job_id'],self.engine)
        receipt=json.loads(self.flow.current(other['job_id'])['receipt'])
        second={k:receipt[k] for k in ('schema','handoff_id','job_id','generation','workflow_id','definition_sha256','grant_id')};second['execution_id']=receipt['first_execution_id']
        result=self.flow.step(second,lambda:self.data)
        self.assertTrue(result['continue_wait']);self.assertEqual(result['error_code'],'LIBRARY_REFINEMENT_BUSY');self.assertEqual(len(self.posts()),1)
        self.complete(grant);self.flow.step(second,lambda:self.data);self.assertEqual(len(self.posts()),2)

    def test_unknown_Data_POST_stops_and_resume_does_not_replay_it(self):
        job,grant,_=self.started();self.data.lost_reply=True
        first=self.flow.step(grant,lambda:self.data);self.assertEqual(first['state'],'attention_required');self.assertFalse(first['continue_wait'])
        self.flow.dispatch(job['job_id'],self.engine,resume=True)
        receipt=json.loads(self.flow.current(job['job_id'])['receipt'])
        second={k:receipt[k] for k in ('schema','handoff_id','job_id','generation','workflow_id','definition_sha256','grant_id')};second['execution_id']=receipt['first_execution_id']
        self.flow.step(second,lambda:self.data);self.assertEqual(len(self.posts()),1)
        self.assertEqual(self.refinement.job(job['job_id'])['state'],'confirmation_required')

    def test_time_budget_ends_without_extra_request_and_resume_reuses_Data_run(self):
        job,grant,_=self.started();self.flow.step(grant,lambda:self.data)
        run_id=self.refinement.job(job['job_id'])['receipt']['resource_id'];before=len(self.data.calls)
        self.epoch+=FLOW_SECONDS;result=self.flow.step(grant,lambda:self.data)
        self.assertEqual(result['state'],'expired');self.assertEqual(before,len(self.data.calls))
        self.flow.dispatch(job['job_id'],self.engine,resume=True)
        with self.assertRaises(UploadError):self.flow.step(grant,lambda:self.data)
        receipt=json.loads(self.flow.current(job['job_id'])['receipt']);second={k:receipt[k] for k in ('schema','handoff_id','job_id','generation','workflow_id','definition_sha256','grant_id')};second['execution_id']=receipt['first_execution_id']
        self.complete(second);self.assertEqual(len(self.posts()),1)
        self.assertEqual(self.refinement.job(job['job_id'])['receipt']['resource_id'],run_id)
        with self.uploads.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM refinement_flow_runs').fetchone()[0],2)

    def test_step_budget_is_durable_and_does_not_trigger_an_extra_Data_call(self):
        job,grant,_=self.started();self.flow.step(grant,lambda:self.data)
        with self.uploads.connect(write=True) as db:db.execute('UPDATE refinement_flow_runs SET steps=?',(MAX_STEPS,))
        before=len(self.data.calls);result=self.flow.step(grant,lambda:self.data)
        self.assertEqual(result['state'],'expired');self.assertEqual(before,len(self.data.calls))

    def test_live_or_completed_flow_cannot_be_resumed_as_new_Data_work(self):
        job,grant,_=self.started()
        with self.assertRaises(UploadError):self.flow.dispatch(job['job_id'],self.engine,resume=True)
        self.flow.step(grant,lambda:self.data);self.complete(grant)
        with self.assertRaises(UploadError):self.flow.dispatch(job['job_id'],self.engine,resume=True)
        self.assertEqual(len(self.posts()),1)

    def test_partial_OCR_result_ends_flow_without_search_readiness(self):
        job,grant,_=self.started();self.flow.step(grant,lambda:self.data)
        run_id=self.refinement.job(job['job_id'])['receipt']['resource_id'];run=self.data.runs[run_id]
        doc=json.loads(self.data.contents[run['output_document_id']]);doc['quality']={'state':'needs_ocr','issues':[{'code':'OCR_REQUIRED','requires_ocr':True}]}
        self.data.set_document(run_id,doc);run.update(status='succeeded',phase='completed')
        result=self.flow.step(grant,lambda:self.data)
        self.assertEqual(result['state'],'attention_required');self.assertEqual(result['error_code'],'OCR_DEFERRED')
        self.assertFalse(result['continue_wait']);self.assertFalse(result['refinement']['refinement_ready'])

    def test_process_death_releases_flow_lock_and_retains_step_intent(self):
        job,grant,_=self.started()
        script="from pathlib import Path;import json,os;from apps.document_qa.uploads import UploadStore;from apps.document_qa.refinement_flow import RefinementFlow;flow=RefinementFlow(UploadStore(Path(os.environ['QA_TEST_ROOT'])));flow.step(json.loads(os.environ['QA_TEST_GRANT']),lambda:os._exit(7))"
        # Use the actual wall clock for this subprocess's deadline check.
        import time
        with self.uploads.connect(write=True) as db:db.execute('UPDATE refinement_flow_runs SET deadline=?',(time.time()+600,))
        result=subprocess.run([sys.executable,'-B','-c',script],env={**os.environ,'QA_TEST_ROOT':str(self.root),'QA_TEST_GRANT':json.dumps(grant)},capture_output=True,timeout=15)
        self.assertEqual(result.returncode,7,result.stderr)
        again=RefinementFlow(UploadStore(self.root));again.step(grant,lambda:self.data)
        self.assertEqual(len(self.posts()),1);self.assertEqual(again.job(job['job_id'])['steps'],2)

    def test_attempt_and_duplicate_execution_limits_refuse_unbounded_history(self):
        job,grant,receipt=self.started();row=self.flow.current(job['job_id']);body=json.loads(row['body'])
        for i in range(15):
            self.flow.accept_receipt({**body,'workflow_id':receipt['workflow_id'],'definition_sha256':receipt['definition_sha256'],'execution_id':'duplicate_'+str(i)})
        with self.assertRaises(UploadError):
            self.flow.accept_receipt({**body,'workflow_id':receipt['workflow_id'],'definition_sha256':receipt['definition_sha256'],'execution_id':'one_too_many'})
        self.assertEqual(self.data.calls,[])
        with self.uploads.connect(write=True) as db:
            db.execute("UPDATE refinement_flow_runs SET attempts=16,receipt=NULL,delivery_state='confirmation_required' WHERE handoff_id=?",(row['handoff_id'],))
        before=len(self.engine_posts())
        with self.assertRaises(UploadError):self.flow.dispatch(job['job_id'],self.engine,retry=True)
        self.assertEqual(before,len(self.engine_posts()))

    def test_generation_limit_preserves_current_run_and_receipt(self):
        job,grant,_=self.started();self.flow.finish(grant['handoff_id'],'attention_required','TEST_ONLY')
        with self.uploads.connect(write=True) as db:db.execute('UPDATE refinement_flow_runs SET generation=?',(MAX_GENERATIONS,))
        before=self.flow.job(job['job_id'])
        with self.assertRaises(UploadError):self.flow.dispatch(job['job_id'],self.engine,resume=True)
        self.assertEqual(before,self.flow.job(job['job_id']));self.assertEqual(self.data.calls,[])

    def test_changed_source_or_callback_sequence_cannot_receive_a_grant(self):
        job,grant,receipt=self.started();body=json.loads(self.flow.current(job['job_id'])['body'])
        for field,value in [('source_document_id',str(uuid4())),('sequence',True),('generation',1.0),('library_id',str(uuid4()))]:
            callback={**body,'workflow_id':receipt['workflow_id'],'definition_sha256':receipt['definition_sha256'],'execution_id':'other',field:value}
            with self.subTest(field=field),self.assertRaises(UploadError):self.flow.accept_receipt(callback)
        self.assertEqual(self.data.calls,[])


class ApiTests(Fixture):
    def request(self,port,path,value=None,token=None,method='POST'):
        con=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
        headers={'Authorization':'Bearer '+(token or self.uploads.token_path.read_text().strip()),'Content-Type':'application/json'}
        con.request(method,path,None if value is None else json.dumps(value),headers)
        response=con.getresponse();result=(response.status,json.loads(response.read()));con.close();return result
    def server(self):return make_server(self.uploads,self.uploaded,port=0,workflow=self.entry,refinement=self.refinement,refiner_factory=lambda:self.data,refinement_flow=self.flow)

    def test_only_dedicated_flow_credential_and_correct_grant_can_execute_step(self):
        job,grant,_=self.started()
        with running(self.server()) as port:
            for token in (self.uploads.token_path.read_text().strip(),self.entry.token_path.read_text().strip()):
                status,_=self.request(port,'/api/workflow/refinement/step',grant,token);self.assertEqual(status,401)
            token=self.flow.token_path.read_text().strip()
            self.assertEqual(self.request(port,'/api/workflow/refinement/step',{**grant,'grant_id':str(uuid4())},token)[0],403)
            self.assertEqual(self.request(port,'/api/workflow/refinement/step',grant,token)[0],200)
        self.assertEqual(len(self.posts()),1)

    def test_flow_credential_cannot_upload_or_use_operator_or_Q03b_receipt_routes(self):
        with running(self.server()) as port:
            token=self.flow.token_path.read_text().strip()
            for path in ('/api/uploads','/api/workflow/receipts','/api/jobs/'+str(uuid4())+'/start-refinement','/api/library'):
                self.assertEqual(self.request(port,path,{},token)[0],401)
        self.assertEqual(self.data.calls,[])

    def test_authenticated_operator_status_is_local_and_hides_grant(self):
        job,grant,_=self.started()
        with running(self.server()) as port:
            for path in ('/api/refinement-workflows','/api/jobs/'+job['job_id']+'/refinement-workflow','/api/jobs/'+job['job_id']):
                status,result=self.request(port,path,method='GET');self.assertEqual(status,200);self.assertNotIn(grant['grant_id'],canonical(result))
        self.assertEqual(self.data.calls,[])

    def test_upload_automatically_delivers_both_flows_and_handles_early_step_callback(self):
        self.prepare()
        with running(self.server()) as port:
            self.entry_engine.callback=lambda value:self.request(port,'/api/workflow/receipts',value,self.entry.token_path.read_text().strip())[1]
            def callback(value):
                receipt=self.request(port,'/api/workflow/refinement/receipts',value,self.flow.token_path.read_text().strip())[1]
                grant={k:receipt[k] for k in ('schema','handoff_id','job_id','generation','workflow_id','definition_sha256','grant_id')};grant['execution_id']=receipt['first_execution_id']
                status,result=self.request(port,'/api/workflow/refinement/step',grant,self.flow.token_path.read_text().strip())
                self.assertEqual(status,200);self.assertTrue(result['continue_wait']);self.assertEqual(result['steps'],0)
                self.grant=grant;return receipt
            self.engine.callback=callback
            with patch('apps.document_qa.workflows.key_client',return_value=self.entry_engine),patch('apps.document_qa.refinement_flow.key_client',return_value=self.engine):
                content_type,raw=encode_multipart({'name':'자료','filename':'x.txt'},DATA,str(uuid4()))
                con=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                con.request('POST','/api/uploads',raw,{'Authorization':'Bearer '+self.uploads.token_path.read_text().strip(),'Content-Type':content_type,'Idempotency-Key':str(uuid4())})
                response=con.getresponse();value=json.loads(response.read());con.close();self.assertEqual(response.status,202)
                self.assertTrue(value['workflow_started']);self.assertEqual(value['refinement_workflow']['delivery_state'],'accepted')
            self.assertEqual(self.data.calls,[])
            status,value=self.request(port,'/api/workflow/refinement/step',self.grant,self.flow.token_path.read_text().strip())
            self.assertEqual(status,200);self.assertEqual(len(self.posts()),1)
            self.complete(self.grant)


@unittest.skipUnless(shutil.which('node'),'Node required to evaluate n8n Code nodes')
class TemplateTests(Fixture):
    def js(self,code,input,values=None,workflow='flow',execution='execution'):
        source="const crypto=require('crypto');const x=JSON.parse(process.argv[1]);const $input={first:()=>({json:x.input})};const $=n=>({first:()=>({json:x.values[n]})});const $workflow={id:x.workflow};const $execution={id:x.execution};try { console.log(JSON.stringify({result:new Function('$input','$','$workflow','$execution','require',x.code)($input,$,$workflow,$execution,require)})); } catch(e) { console.log(JSON.stringify({error:e.message})); }"
        result=subprocess.run(['node','-e',source,json.dumps({'code':code,'input':input,'values':values or {},'workflow':workflow,'execution':execution})],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
    def test_fixed_definition_has_early_response_then_owner_gate_and_bounded_loop(self):
        flow,digest=build('http://127.0.0.1:17100','document-qa-'+str(uuid4()),'qa','hook')
        self.assertEqual(len(flow['nodes']),9);self.assertEqual(flow['nodes'][4]['type'],'n8n-nodes-base.respondToWebhook')
        self.assertEqual(flow['nodes'][5]['name'],'Own execution grant');self.assertEqual(flow['nodes'][-1]['parameters']['amount'],5)
        self.assertEqual(flow['settings']['executionTimeout'],600)
        self.assertEqual({n['parameters']['url'] for n in flow['nodes'] if n['type'].endswith('httpRequest')},
                         {'http://127.0.0.1:17100/api/workflow/refinement/receipts','http://127.0.0.1:17100/api/workflow/refinement/step'})
        self.assertTrue(matches(flow,flow));changed=copy.deepcopy(flow);changed['nodes'][6]['retryOnFail']=True;self.assertFalse(matches(changed,flow))
    def test_validate_generation_and_JWT_request_binding(self):
        job,grant,receipt=self.started();setup=self.flow.setup();flow,_=self.flow.desired(setup)
        body=json.loads(self.flow.current(job['job_id'])['body']);header=signed_headers(self.flow.hook_path.read_text().strip(),setup['path'],body)['Authorization'].split()[1]
        claims=header.split('.')[1];jwt=json.loads(base64.urlsafe_b64decode(claims+'='*(-len(claims)%4)))
        code=flow['nodes'][1]['parameters']['jsCode'];result=self.js(code,{'body':body,'jwtPayload':jwt})
        self.assertIn('result',result)
        for generation in (0,17,True):
            self.assertIn('error',self.js(code,{'body':{**body,'generation':generation},'jwtPayload':jwt}))
    def test_receipt_verification_and_duplicate_owner_gate(self):
        job,grant,receipt=self.started();row=self.flow.current(job['job_id']);p={**json.loads(row['body']),'workflow_id':receipt['workflow_id'],'definition_sha256':receipt['definition_sha256'],'execution_id':receipt['first_execution_id']}
        self.assertEqual(self.js(VERIFY,{'statusCode':200,'body':receipt},{'Validate handoff':p})['result'],[{'json':receipt}])
        self.assertEqual(self.js(OWNER,receipt,{'Validate handoff':p})['result'],[{'json':grant}])
        self.assertEqual(self.js(OWNER,receipt,{'Validate handoff':{**p,'execution_id':'duplicate'}})['result'],[])
        self.assertIn('error',self.js(VERIFY,{'statusCode':200,'body':{**receipt,'source_document_id':str(uuid4())}},{'Validate handoff':p}))
    def test_result_gate_waits_only_pending_and_stops_completed_attention_or_expired(self):
        job,grant,_=self.started();result=self.flow.step(grant,lambda:self.data)
        self.assertEqual(self.js(RESULT,{'statusCode':200,'body':result},{'Own execution grant':grant})['result'],[{'json':result}])
        done=self.complete(grant);self.assertEqual(self.js(RESULT,{'statusCode':200,'body':done},{'Own execution grant':grant})['result'],[])
        self.assertIn('error',self.js(RESULT,{'statusCode':200,'body':{**done,'continue_wait':True}},{'Own execution grant':grant}))
        for state in ('attention_required','expired'):
            value={**result,'state':state,'continue_wait':False};self.assertEqual(self.js(RESULT,{'statusCode':200,'body':value},{'Own execution grant':grant})['result'],[])


if __name__=='__main__':unittest.main()
