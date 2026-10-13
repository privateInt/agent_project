"""Q04a temporary SQLite, fake Data lifecycle, actual loopback HTTP and CPU parser."""
import copy
from dataclasses import asdict
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from src.apps.document_qa.api import make_server
from src.apps.document_qa.canonical_results import checked_result, RULES
from src.apps.document_qa.contracts import Receipt, MAX_BYTES
from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.refinement import DataRefiner, RefinementStore, MAX_ATTEMPTS
from src.apps.document_qa.uploads import UploadStore
from src.apps.document_qa.workflows import WorkflowStore

ORIGIN = 'http://127.0.0.1:12345'
IMAGE = 'sha256:' + 'a'*64
DATA = '문서 QA 안내\r\n질문은 근거를 포함합니다.\n'.encode()


def encoded(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()


class Uploaded:
    origin = ORIGIN
    def __init__(self):
        self.sources, self.bytes = {}, {}
    def upload(self,meta,data,key):
        source = {'document_id':str(uuid4()),'state':'ready','source_type':'upload','name':meta['name'],
                  'original_filename':meta['filename'],'sha256':meta['sha256'],'size_bytes':len(data),
                  'format':{'txt':'text','md':'markdown','markdown':'markdown','pdf':'pdf'}[meta['filename'].rsplit('.',1)[-1].lower()]}
        self.sources[source['document_id']] = source; self.bytes[source['document_id']] = data
        return 200, encoded(source)


class FakeData:
    origin = ORIGIN
    def __init__(self,uploaded):
        self.uploaded,self.calls,self.runs,self.keys,self.meta,self.contents = uploaded,[],{},{},{},{}
        self.lost_reply = False; self.bad_receipt = None; self.enter = self.release = None
    def create(self,body,key):
        if key not in self.keys:
            source = self.uploaded.sources[body['document_id']]
            run_id,op_id,out_id = str(uuid4()),str(uuid4()),str(uuid4())
            run = {'refinement_run_id':run_id,'operation_id':op_id,'output_document_id':out_id,
                   'source_document_id':source['document_id'],'source_sha256':source['sha256'],
                   'source_format':source['format'],'rules_version':RULES,'name':body['name'],
                   'status':'running','phase':'parsing','error_code':'','ocr':None,
                   'submission_status':'delivered','submission_error_code':'','deleted_at':None,'purge_state':''}
            document={'schema_version':'canonical-document-v1','source':{'document_id':source['document_id'],'sha256':source['sha256'],'format':source['format']},
                      'processor':{'version':'cpu-parser-v1','rules_version':RULES,'image_id':IMAGE},
                      'processing':{'version':'refinement-processing-v1','mode':'parser_only'},'cleaning':{'version':RULES},
                      'blocks':[{'id':'b1','order':0,'type':'text','text':'검증할 문서 본문','location':{'line_start':1}}],
                      'pages':[],'quality':{'state':'ready','issues':[]}}
            self.runs[run_id]=run;self.keys[key]=(copy.deepcopy(body),run_id)
            self.set_document(run_id,document)
        previous,run_id=self.keys[key]
        assert previous==body
        run=self.runs[run_id]
        return {'refinement_run_id':run_id,'operation_id':run['operation_id'],'status':'accepted'}
    def set_document(self,run_id,value):
        run=self.runs[run_id];raw=encoded(value);out=run['output_document_id'];self.contents[out]=raw
        self.meta[out]={'document_id':out,'refinement_run_id':run_id,'source_document_id':run['source_document_id'],
                        'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw),'state':value['quality']['state'],
                        'schema_version':value['schema_version'],'processor_version':value['processor']['version'],
                        'rules_version':RULES,'processor_image_id':IMAGE,'block_count':len(value['blocks']),
                        'page_count':len(value['pages']),'artifact_uri':'https://never-fetch.invalid/private'}
    def get(self,path):
        self.calls.append(('GET',path,None,None))
        kind,identifier=path.split('/')[2:4]
        return copy.deepcopy({'source-documents':self.uploaded.sources,'refinement-runs':self.runs,'canonical-documents':self.meta}[kind][identifier])
    def request(self,method,path,body=None,key=None,content_limit=None):
        self.calls.append((method,path,copy.deepcopy(body),key))
        if method=='POST':
            result=self.create(body,key)
            if self.enter:
                self.enter.set();assert self.release.wait(3)
            if self.lost_reply:
                raise UploadError('DATA_REFINEMENT_OUTCOME_UNKNOWN',502)
            return self.bad_receipt or (202,encoded(result))
        raw=self.contents[path.split('/')[3]]
        assert content_limit is None or len(raw)<=content_limit
        return 200,raw


@contextmanager
def running(server):
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True);thread.start()
    try:yield server.server_address[1]
    finally:server.shutdown();server.server_close();thread.join(3)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='qa-refinement-');self.root=Path(self.temp.name)/'uploads'
        self.uploads=UploadStore.initialize(self.root);self.workflow=WorkflowStore.initialize(self.uploads)
        self.refinement=RefinementStore.initialize(self.uploads);self.uploaded=Uploaded();self.api=FakeData(self.uploaded)
    def tearDown(self):self.temp.cleanup()
    def job(self,name='자료',filename='x.txt',data=DATA):
        job=self.uploads.accept(str(uuid4()),name,filename,data,ORIGIN)
        return self.uploads.dispatch(job['job_id'],self.uploaded)
    def accepted(self):
        job=self.job();accepted=self.refinement.dispatch(job['job_id'],self.api)
        run=self.api.runs[accepted['receipt']['resource_id']]
        return job,accepted,run
    def completed(self):
        job,accepted,run=self.accepted();run['status']='succeeded';run['phase']='completed'
        return job,accepted,run
    def posts(self):return [c for c in self.api.calls if c[0]=='POST']


class StorageTests(Fixture):
    def test_additive_initialization_preserves_keys_library_jobs_and_schema(self):
        job=self.job();library=self.uploads.library();keys={p.name:p.read_bytes() for p in self.root.iterdir() if 'token' in p.name or 'secret' in p.name}
        RefinementStore.initialize(UploadStore(self.root))
        self.assertEqual(UploadStore(self.root).library(),library);self.assertEqual(self.uploads.job(job['job_id']),job)
        self.assertEqual(keys,{p.name:p.read_bytes() for p in self.root.iterdir() if p.name in keys})
        with self.uploads.connect() as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
        self.assertEqual(self.api.calls,[]);self.assertIsNone(self.refinement.status()['active_job_id'])

    def test_unconfirmed_upload_cannot_start_refinement(self):
        job=self.uploads.accept(str(uuid4()),'未確認','x.txt',DATA,ORIGIN)
        with self.assertRaises(UploadError):self.refinement.dispatch(job['job_id'],self.api)
        self.assertEqual(self.api.calls,[])

    def test_deleted_changed_source_refused_before_post(self):
        job=self.job();source=self.uploaded.sources[job['source_document_id']]
        for field,value in [('deleted_at','date'),('purge_state','purging'),('sha256','b'*64),('format','html'),('size_bytes',True),('original_filename','other.txt')]:
            original=copy.deepcopy(source);source[field]=value
            with self.subTest(field=field),self.assertRaises(UploadError):self.refinement.dispatch(job['job_id'],self.api)
            source.clear();source.update(original)
        self.assertEqual(self.posts(),[]);self.assertEqual(self.refinement.job(job['job_id'])['state'],'not_requested')

    def test_target_changed_refused_before_any_io(self):
        job=self.job();self.api.origin='http://127.0.0.1:54321'
        with self.assertRaises(UploadError):self.refinement.dispatch(job['job_id'],self.api)
        self.assertEqual(self.api.calls,[])

    def test_202_is_waiting_and_never_canonical_or_question_ready(self):
        job,result,run=self.accepted()
        self.assertEqual(result['state'],'waiting');self.assertFalse(result['refinement_ready']);self.assertFalse(result['question_ready'])
        self.assertIsNone(result['canonical']);self.assertEqual(self.refinement.status()['active_job_id'],job['job_id'])
        self.assertNotIn('ocr_deployment_id',self.posts()[0][2]);self.assertEqual(self.posts()[0][2]['rules_version'],RULES)

    def test_unknown_post_only_explicit_retry_with_original_key_body(self):
        job=self.job();self.api.lost_reply=True
        first=self.refinement.dispatch(job['job_id'],self.api);self.assertEqual(first['state'],'confirmation_required')
        reopened=RefinementStore(UploadStore(self.root));reopened.dispatch(job['job_id'],self.api)
        reopened.observe(job['job_id'],self.api);self.assertEqual(len(self.posts()),1)
        self.api.lost_reply=False
        second=reopened.dispatch(job['job_id'],self.api,retry=True)
        self.assertEqual(second['state'],'waiting');self.assertEqual(self.posts()[0][2:],self.posts()[1][2:]);self.assertEqual(len(self.api.runs),1)

    def test_receipt_known_retry_does_not_post_again(self):
        job,first,run=self.accepted();before=len(self.api.calls)
        self.assertEqual(self.refinement.dispatch(job['job_id'],self.api,retry=True),first)
        self.assertEqual(len(self.api.calls),before)

    def test_bad_202_receipt_does_not_confirm(self):
        for code,body in [(200,{}),(202,{'refinement_run_id':str(uuid4()),'operation_id':True,'status':'accepted'}),(202,{'status':'started'})]:
            job=self.job(name=str(uuid4()));self.api.bad_receipt=(code,encoded(body))
            self.assertEqual(self.refinement.dispatch(job['job_id'],self.api)['state'],'confirmation_required')
            # Fixture-only reset between independent malformed upstream cases.
            with self.uploads.connect(write=True) as db:db.execute('UPDATE refinement_lane SET job_id=NULL')

    def test_library_lane_persists_until_known_terminal_result(self):
        first,accepted,run=self.accepted();second=self.job(name='두 번째')
        with self.assertRaises(UploadError) as error:self.refinement.dispatch(second['job_id'],self.api)
        self.assertEqual(error.exception.code,'LIBRARY_REFINEMENT_BUSY');self.assertEqual(len(self.posts()),1)
        run['status']='succeeded';run['phase']='completed'
        self.assertEqual(self.refinement.observe(first['job_id'],self.api)['state'],'canonical_verified')
        self.assertEqual(self.refinement.dispatch(second['job_id'],self.api)['state'],'waiting')

    def test_single_observation_of_running_has_no_loop_or_post(self):
        job,accepted,run=self.accepted();before=len(self.api.calls)
        result=self.refinement.observe(job['job_id'],self.api)
        self.assertEqual(result['state'],'waiting');self.assertEqual(len(self.api.calls)-before,1);self.assertEqual(len(self.posts()),1)

    def test_failed_cancelled_result_is_terminal_without_automatic_new_generation(self):
        job,accepted,run=self.accepted();run['status']='failed';run['error_code']='private upstream error text';run['error_message']='secret'
        result=self.refinement.observe(job['job_id'],self.api)
        self.assertEqual(result['state'],'failed');self.assertEqual(result['error_code'],'DATA_REFINEMENT_FAILED')
        self.assertIsNone(self.refinement.status()['active_job_id']);self.refinement.dispatch(job['job_id'],self.api,retry=True)
        self.assertEqual(len(self.posts()),1);self.assertNotIn('secret',json.dumps(result))

    def test_run_identity_and_cpu_provenance_mismatch_retains_lane(self):
        job,accepted,run=self.completed()
        for field,value in [('source_document_id',str(uuid4())),('operation_id',str(uuid4())),('name','changed'),('source_sha256','b'*64),('rules_version','changed'),('ocr',{'deployment_id':str(uuid4())}),('deleted_at','date')]:
            original=copy.deepcopy(run);run[field]=value
            result=self.refinement.observe(job['job_id'],self.api)
            self.assertEqual(result['state'],'attention_required');self.assertFalse(result['terminal']);self.assertIsNone(result['canonical'])
            self.assertEqual(self.refinement.status()['active_job_id'],job['job_id']);run.clear();run.update(original)
        self.assertEqual(self.refinement.observe(job['job_id'],self.api)['state'],'canonical_verified')

    def test_canonical_verified_reference_no_long_term_text_or_uri(self):
        job,accepted,run=self.completed();result=self.refinement.observe(job['job_id'],self.api)
        self.assertEqual(result['state'],'canonical_verified');self.assertTrue(result['refinement_ready']);self.assertFalse(result['question_ready'])
        self.assertIsNone(self.refinement.status()['active_job_id']);self.assertEqual(self.uploads.library()['question_revision'],0)
        with self.uploads.connect() as db:
            stored=dict(db.execute('SELECT * FROM refinement_tasks').fetchone());self.assertIsNone(db.execute('SELECT original FROM jobs').fetchone()[0])
        self.assertNotIn('검증할 문서 본문',json.dumps(stored,ensure_ascii=False));self.assertNotIn('never-fetch',json.dumps(stored))
        before=len(self.api.calls);self.refinement.observe(job['job_id'],self.api);self.assertEqual(len(self.api.calls),before)

    def test_needs_ocr_or_review_is_attention_no_model_call_and_lane_released(self):
        job,accepted,run=self.completed();out=run['output_document_id'];doc=json.loads(self.api.contents[out])
        doc['quality']={'state':'needs_ocr','issues':[{'code':'scan_requires_ocr','requires_ocr':True}]};doc['blocks']=[]
        self.api.set_document(run['refinement_run_id'],doc)
        result=self.refinement.observe(job['job_id'],self.api)
        self.assertEqual(result['state'],'attention_required');self.assertEqual(result['error_code'],'OCR_DEFERRED');self.assertTrue(result['terminal'])
        self.assertTrue(result['canonical']['requires_ocr']);self.assertFalse(result['refinement_ready']);self.assertIsNone(self.refinement.status()['active_job_id'])
        self.assertTrue(all(c[1].startswith('/api/') for c in self.api.calls));self.assertEqual(len(self.posts()),1)

    def test_verified_empty_cpu_result_is_terminal_attention_not_search_ready(self):
        job,accepted,run=self.completed();doc=json.loads(self.api.contents[run['output_document_id']])
        doc['blocks'][0]['text']=' \n\t';self.api.set_document(run['refinement_run_id'],doc)
        result=self.refinement.observe(job['job_id'],self.api)
        self.assertEqual(result['error_code'],'CANONICAL_EMPTY');self.assertTrue(result['terminal'])
        self.assertFalse(result['refinement_ready']);self.assertIsNone(self.refinement.status()['active_job_id'])

    def test_wrong_canonical_metadata_identity_blocks_content_download(self):
        job,accepted,run=self.completed();self.api.meta[run['output_document_id']]['source_document_id']=str(uuid4())
        result=self.refinement.observe(job['job_id'],self.api)
        self.assertEqual(result['error_code'],'CANONICAL_IDENTITY_MISMATCH')
        self.assertFalse(any(c[1].endswith('/content') for c in self.api.calls))

    def test_succeeded_missing_output_or_error_is_not_completion(self):
        job,accepted,run=self.completed();original=copy.deepcopy(run)
        for field,value in [('output_document_id',''),('error_code','unexpected-error')]:
            run[field]=value;result=self.refinement.observe(job['job_id'],self.api)
            self.assertEqual(result['state'],'attention_required');self.assertFalse(result['terminal'])
            self.assertIsNone(result['canonical']);run.clear();run.update(original)
        self.assertFalse(any(c[1].endswith('/content') for c in self.api.calls))

    def test_process_death_after_post_preserves_intent_and_frees_os_lock(self):
        job=self.job();sources=self.uploaded.sources
        script='''import os,sys,json
from pathlib import Path
from src.apps.document_qa.uploads import UploadStore
from src.apps.document_qa.refinement import RefinementStore
class Crash:
 origin='http://127.0.0.1:12345'
 def get(self,path):return json.loads(sys.argv[3])[path.split('/')[-1]]
 def request(self,*args,**kwargs):os._exit(7)
r=RefinementStore(UploadStore(Path(sys.argv[1])));r.dispatch(sys.argv[2],Crash())
'''
        proc=subprocess.run([sys.executable,'-B','-c',script,str(self.root),job['job_id'],json.dumps(sources)],capture_output=True)
        self.assertEqual(proc.returncode,7);self.assertEqual(self.refinement.job(job['job_id'])['state'],'confirmation_required')
        self.assertEqual(self.refinement.dispatch(job['job_id'],self.api,retry=True)['state'],'waiting')

    def test_remote_io_has_no_db_transaction_and_concurrent_sender_cannot_repeat(self):
        job=self.job();self.api.enter,self.api.release=threading.Event(),threading.Event()
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.refinement.dispatch,job['job_id'],self.api);self.assertTrue(self.api.enter.wait(2))
            with self.uploads.connect(write=True) as db:db.execute('UPDATE library SET name=name')
            with self.assertRaises(UploadError):self.refinement.dispatch(job['job_id'],self.api)
            self.api.release.set();self.assertEqual(future.result(3)['state'],'waiting')
        self.assertEqual(len(self.posts()),1)

    def test_retry_attempt_cap_is_durable(self):
        job=self.job();self.api.lost_reply=True;self.refinement.dispatch(job['job_id'],self.api)
        with self.uploads.connect(write=True) as db:db.execute('UPDATE refinement_tasks SET attempts=?',(MAX_ATTEMPTS,))
        with self.assertRaises(UploadError) as error:self.refinement.dispatch(job['job_id'],self.api,retry=True)
        self.assertEqual(error.exception.code,'REFINEMENT_REVIEW_REQUIRED');self.assertEqual(len(self.posts()),1)

    def test_get_outage_preserves_receipt_and_can_reobserve_without_post(self):
        job,accepted,run=self.completed();get=self.api.get
        def unavailable(path):raise UploadError('DATA_RESOURCE_UNAVAILABLE',502)
        self.api.get=unavailable;result=self.refinement.observe(job['job_id'],self.api)
        self.assertEqual(result['receipt'],accepted['receipt']);self.assertFalse(result['terminal'])
        self.api.get=get;self.assertEqual(self.refinement.observe(job['job_id'],self.api)['state'],'canonical_verified');self.assertEqual(len(self.posts()),1)

    def test_submission_blocked_is_attention_not_an_infinite_success_wait(self):
        job,accepted,run=self.accepted();run.update(status='created',submission_status='blocked',submission_error_code='Unavailable')
        result=self.refinement.observe(job['job_id'],self.api)
        self.assertEqual(result['error_code'],'DATA_SUBMISSION_ATTENTION_REQUIRED');self.assertFalse(result['terminal'])


class CanonicalTests(Fixture):
    def data(self):
        job,accepted,run=self.completed();out=run['output_document_id']
        return job,run,Receipt(**accepted['receipt']),self.api.meta[out],json.loads(self.api.contents[out])
    def test_byte_digest_size_and_metadata_identity_refused(self):
        job,run,receipt,meta,doc=self.data();raw=encoded(doc)
        for field,value in [('size_bytes',True),('size_bytes',len(raw)+1),('sha256','b'*64),('document_id',str(uuid4())),('refinement_run_id',str(uuid4())),('source_document_id',str(uuid4()))]:
            with self.subTest(field=field),self.assertRaises(UploadError):checked_result({**meta,field:value},raw,receipt,job,run)
    def test_duplicate_json_nan_and_nonfinite_nested_values_refused(self):
        job,run,receipt,meta,doc=self.data()
        for raw in [b'{"schema_version":1,"schema_version":2}',b'{"bad":NaN}',encoded({**doc,'extra':1e300}).replace(b'1e+300',b'1e999')]:
            bad={**meta,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)}
            with self.assertRaises(UploadError):checked_result(bad,raw,receipt,job,run)
    def test_processor_image_rules_and_unexpected_ocr_refused(self):
        job,run,receipt,meta,doc=self.data()
        variants=[]
        for field,value in [('version','cpu-parser-surya-v1'),('rules_version','changed'),('image_id','sha256:'+'b'*64)]:
            changed=copy.deepcopy(doc);changed['processor'][field]=value;variants.append(changed)
        variants.extend([{**doc,'ocr':{}},{**doc,'processing':{'version':'refinement-processing-v1','mode':'ocr_priority'}}])
        for value in variants:
            raw=encoded(value);updated={**meta,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)}
            with self.assertRaises(UploadError):checked_result(updated,raw,receipt,job,run)
    def test_source_hash_and_format_cannot_change(self):
        job,run,receipt,meta,doc=self.data()
        for field,value in [('sha256','b'*64),('format','html'),('document_id',str(uuid4()))]:
            changed=copy.deepcopy(doc);changed['source'][field]=value;raw=encoded(changed)
            with self.assertRaises(UploadError):checked_result({**meta,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)},raw,receipt,job,run)
    def test_block_order_type_counts_and_empty_body_refused(self):
        job,run,receipt,meta,doc=self.data()
        variants=[]
        for field,value in [('order',True),('id','b2'),('type',[]),('text',123)]:
            changed=copy.deepcopy(doc);changed['blocks'][0][field]=value;variants.append(changed)
        variants.append({**doc,'blocks':[]})
        for value in variants:
            raw=encoded(value);updated={**meta,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)}
            with self.assertRaises(UploadError):checked_result(updated,raw,receipt,job,run)
    def test_quality_state_and_requires_ocr_type_must_match(self):
        job,run,receipt,meta,doc=self.data()
        for quality in [{'state':'ready','issues':[{'code':'scan','requires_ocr':True}]},
                        {'state':'ready','issues':[{'code':'scan','requires_ocr':'false'}]}, {'state':'ready','issues':None}]:
            value={**doc,'quality':quality};raw=encoded(value)
            with self.assertRaises(UploadError):checked_result({**meta,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)},raw,receipt,job,run)
    def test_valid_needs_review_preserves_reference_without_accepting_for_search(self):
        job,run,receipt,meta,doc=self.data();doc['quality']={'state':'needs_review','issues':[{'code':'unhandled_structure','requires_ocr':False}]};raw=encoded(doc)
        summary,error=checked_result({**meta,'state':'needs_review','sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)},raw,receipt,job,run)
        self.assertEqual(error,'CANONICAL_REVIEW_REQUIRED');self.assertEqual(summary['issue_count'],1);self.assertNotIn('blocks',summary)
    def test_old_immutable_cpu_output_without_processing_is_readable(self):
        job,run,receipt,meta,doc=self.data();doc.pop('processing');raw=encoded(doc)
        self.assertIsNone(checked_result({**meta,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)},raw,receipt,job,run)[1])


class TransportTests(Fixture):
    @contextmanager
    def http(self,mode='valid'):
        fake=self.api;seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                seen.append((self.command,self.path,self.headers.get('Authorization'),self.headers.get('Idempotency-Key')))
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));code,raw=fake.request('POST',self.path,body,self.headers['Idempotency-Key']);self.reply(code,raw)
            def do_GET(self):
                seen.append((self.command,self.path,self.headers.get('Authorization'),None))
                if mode=='redirect':
                    self.send_response(302);self.send_header('Location',ORIGIN+'/api/refinement-runs');self.end_headers();return
                if mode=='large':self.reply(200,b' '*((64<<10)+1));return
                if mode=='duplicate':self.reply(200,b'{"x":1,"x":2}');return
                if self.path.endswith('/content'):self.reply(200,fake.contents[self.path.split('/')[3]])
                else:self.reply(200,encoded(fake.get(self.path)))
            def reply(self,code,raw):
                self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        with running(ThreadingHTTPServer(('127.0.0.1',0),Handler)) as port:
            client=DataRefiner({'url':'http://127.0.0.1:'+str(port)})
            yield client,seen
    def test_real_http_cpu_lifecycle_and_fixed_canonical_download_origin(self):
        job=self.job()
        with self.http() as (api,seen):
            with self.uploads.connect(write=True) as db:db.execute('UPDATE jobs SET data_origin=?',(api.origin,))
            result=self.refinement.dispatch(job['job_id'],api);self.api.runs[result['receipt']['resource_id']]['status']='succeeded'
            result=self.refinement.observe(job['job_id'],api)
            self.assertEqual(result['state'],'canonical_verified');self.assertEqual(len([x for x in seen if x[0]=='POST']),1)
            self.assertTrue(any(x[1].endswith('/content') for x in seen));self.assertLessEqual(api.calls,8)
    def test_existing_token_ref_read_only_and_only_bound_data_host(self):
        job=self.job();token=Path(self.temp.name)/'data-token';token.write_text('fixture-key');token.chmod(0o444)
        with self.http() as (api,seen):
            api.token_file=token;api.get('/api/source-documents/'+job['source_document_id'])
            self.assertEqual(seen[0][2],'Bearer fixture-key');self.assertEqual(token.stat().st_mode&0o777,0o444)
    def test_redirect_response_limit_and_duplicate_json_are_refused(self):
        job=self.job()
        for mode in ('redirect','large','duplicate'):
            with self.subTest(mode=mode),self.http(mode) as (api,seen),self.assertRaises(UploadError):api.get('/api/source-documents/'+job['source_document_id'])
    def test_forbidden_model_source_download_and_caller_ocr_are_refused_without_http(self):
        api=DataRefiner({'url':ORIGIN});identifier=str(uuid4())
        for method,path,body,key in [('GET','/api/deployments',None,None),('GET','/api/source-documents/'+identifier+'/content',None,None),
                                     ('POST','/api/refinement-runs',{'name':'document-qa-'+identifier,'document_id':identifier,'rules_version':RULES,'ocr_deployment_id':identifier},identifier)]:
            with self.assertRaises(UploadError):api.request(method,path,body,key)
        self.assertEqual(api.calls,0)
    def test_shared_deadline_and_call_budget_are_checked_before_request(self):
        clock=[0];api=DataRefiner({'url':ORIGIN},clock=lambda:clock[0]);clock[0]=61
        with self.assertRaises(UploadError):api.get('/api/source-documents/'+str(uuid4()))
        self.assertEqual(api.calls,0)
    def test_canonical_content_limit_is_metadata_bound(self):
        job,accepted,run=self.completed()
        with self.http() as (api,seen),self.assertRaises(UploadError):api.request('GET','/api/canonical-documents/'+run['output_document_id']+'/content',content_limit=1)


class ApiTests(Fixture):
    def request(self,port,method,path,value=None,workflow_auth=False):
        token=(self.workflow.token_path if workflow_auth else self.uploads.token_path).read_text().strip()
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=4)
        try:
            connection.request(method,path,None if value is None else json.dumps(value),{'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            response=connection.getresponse();return response.status,json.loads(response.read())
        finally:connection.close()
    def server(self):return make_server(self.uploads,self.uploaded,port=0,workflow=self.workflow,refinement=self.refinement,refiner_factory=lambda:self.api)
    def test_operator_api_start_observe_and_local_projection(self):
        job=self.job()
        with running(self.server()) as port:
            status,result=self.request(port,'POST','/api/jobs/'+job['job_id']+'/start-refinement',{})
            self.assertEqual((status,result['state']),(202,'waiting'));self.api.runs[result['receipt']['resource_id']]['status']='succeeded'
            status,result=self.request(port,'POST','/api/jobs/'+job['job_id']+'/observe-refinement',{})
            self.assertEqual((status,result['state']),(202,'canonical_verified'))
            self.assertTrue(self.request(port,'GET','/api/jobs/'+job['job_id'])[1]['refinement']['refinement_ready'])
            self.assertEqual(self.request(port,'GET','/api/jobs/'+job['job_id']+'/refinement')[1],result)
            self.assertFalse(self.request(port,'GET','/api/refinements')[1]['n8n_refinement_connected'])
    def test_n8n_receipt_credential_does_not_authorize_domain_execution(self):
        job=self.job()
        with running(self.server()) as port:
            status,result=self.request(port,'POST','/api/jobs/'+job['job_id']+'/start-refinement',{},workflow_auth=True)
            self.assertEqual(status,401);self.assertEqual(self.posts(),[])
    def test_caller_cannot_supply_model_url_body_or_new_key(self):
        job=self.job()
        with running(self.server()) as port:
            for body in ({'ocr_deployment_id':str(uuid4())},{'url':'https://invalid'},{'rules_version':'changed'},{'name':'changed'}):
                self.assertEqual(self.request(port,'POST','/api/jobs/'+job['job_id']+'/start-refinement',body)[0],400)
        self.assertEqual(self.api.calls,[])
    def test_local_status_does_not_call_any_platform(self):
        with running(self.server()) as port:
            self.assertEqual(self.request(port,'GET','/api/refinements')[0],200)
        self.assertEqual(self.api.calls,[])


class ExistingParserCompatibility(Fixture):
    def test_actual_existing_CPU_text_markdown_output_passes_new_reference_gate(self):
        path=Path(__file__).resolve().parents[4]/'src/platform/data/jobs/refinement/parse/parser.py'
        spec=importlib.util.spec_from_file_location('qa_test_existing_parser',path);parser=importlib.util.module_from_spec(spec);spec.loader.exec_module(parser)
        for filename in ('안내.txt','안내.md'):
            job=self.job(name=filename,filename=filename);accepted=self.refinement.dispatch(job['job_id'],self.api);run=self.api.runs[accepted['receipt']['resource_id']]
            value=json.loads(parser.parse(DATA,{'document_id':job['source_document_id'],'sha256':job['sha256'],'format':run['source_format'],'rules_version':RULES,'image_id':IMAGE}))
            self.api.set_document(run['refinement_run_id'],value);run['status']='succeeded'
            result=self.refinement.observe(job['job_id'],self.api)
            self.assertEqual(result['state'],'canonical_verified');self.assertEqual(result['canonical']['processor_version'],'cpu-parser-v1')
            self.assertFalse(result['question_ready'])


if __name__=='__main__':unittest.main()
