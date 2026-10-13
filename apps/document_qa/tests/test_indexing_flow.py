"""Q05b native SQLite/QA callbacks/Node validation; only synthetic platforms."""
import base64
import copy
from contextlib import redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from apps.document_qa.api import make_server
from apps.document_qa.contracts import fingerprint, ContractError
from apps.document_qa.indexing import IndexingStore
from apps.document_qa.indexing_flow import IndexingFlow, flow_lock, main, MAX_STEPS, FLOW_SECONDS, MAX_GENERATIONS
from apps.document_qa.indexing_flow_template import build, VERIFY, OWNER, RESULT
from apps.document_qa.indexing_runtime import IndexingGate
from apps.document_qa.indexing_connection import main as connection_main
from apps.document_qa.intake import UploadError
from apps.document_qa import readiness
from apps.document_qa.uploads import UploadStore
from apps.document_qa.workflows import canonical, signed_headers
from apps.document_qa.workflow_template import matches
from apps.document_qa.tests.test_indexing import FakeKnowledge, DEFAULTS
from apps.document_qa.tests.test_readiness import Fake as MetadataReader, settings
from apps.document_qa.tests.test_refinement_flow import Fixture as CPUFixture
from apps.document_qa.tests.test_refinement import DATA, running
from apps.document_qa.tests.test_workflows import FakeEngine


class Fixture(CPUFixture):
    def setUp(self):
        super().setUp()
        self.cpu, self.cpu_engine = self.flow, self.engine
        self.indexing = IndexingStore.initialize(self.uploads)
        self.flow = IndexingFlow.initialize(self.uploads)
        self.flow.clock = lambda: self.epoch
        self.engine = FakeEngine(self.flow)
        self.engine.resources = self.entry_engine.resources
        self.engine.observations = []
        raw_request = self.engine.request
        def observed_request(method,route,body=None,headers=None):
            self.engine.observations.append({'method':method,'route':route})
            return raw_request(method,route,body,headers)
        self.engine.request = observed_request
        self.knowledge = FakeKnowledge(self.data, self.uploaded)
        self.factory_calls, self.gate_calls = [], []

    def prepare(self):
        origin = 'http://127.0.0.1:17110'
        self.entry.prepare(origin, self.key, self.entry_engine)
        self.cpu.prepare(origin, self.key, self.cpu_engine)
        self.flow.prepare(origin, self.key, self.engine)

    def grant_for(self, flow, job_id):
        receipt = json.loads(flow.current(job_id)['receipt'])
        grant = {k:receipt[k] for k in ('schema','handoff_id','job_id','generation','workflow_id','definition_sha256','grant_id')}
        return {**grant, 'execution_id':receipt['first_execution_id']}, receipt

    def canonical_job(self, data=DATA):
        self.prepare()
        job = self.job(data)
        self.entry.dispatch(job['job_id'], self.entry_engine)
        self.cpu.dispatch(job['job_id'], self.cpu_engine)
        grant, _ = self.grant_for(self.cpu, job['job_id'])
        self.cpu.step(grant, lambda:self.data)
        run_id = self.refinement.job(job['job_id'])['receipt']['resource_id']
        self.data.runs[run_id].update(status='succeeded',phase='completed')
        self.assertEqual(self.cpu.step(grant, lambda:self.data)['state'],'canonical_verified')
        return job

    def started(self, job=None):
        job = job or self.canonical_job()
        self.flow.dispatch(job['job_id'], self.engine)
        grant, receipt = self.grant_for(self.flow, job['job_id'])
        return job, grant, receipt

    def factory(self, **options):
        self.factory_calls.append(options)
        self.knowledge.writes_enabled = options['execute']
        self.knowledge.build_enabled = options['execute'] and options['allow_inference']
        return self.knowledge

    def gate(self, remaining):
        self.gate_calls.append(remaining)
        return copy.deepcopy(DEFAULTS)

    def step(self, grant, gate=None):
        return self.flow.step(grant, self.factory, gate or self.gate)

    def kb_posts(self, suffix=None):
        return [c for c in self.knowledge.calls if c[0]=='POST' and (suffix is None or c[1].endswith(suffix))]

    def build_job(self):
        job, grant, receipt = self.started()
        self.assertTrue(self.step(grant)['continue_wait'])  # dedicated logical index
        result = self.step(grant)  # immutable build
        self.assertEqual(result['indexing']['state'],'waiting')
        return job, grant, receipt

    def mark_ready(self, job_id):
        version = self.indexing.job(job_id)['index_version_id']
        self.knowledge.versions[version].update(state='ready',reference_state='held',chunk_count=4,indexed_count=4,chunk_set_sha256='c'*64)

    def activate_job(self):
        job, grant, receipt = self.build_job()
        self.mark_ready(job['job_id'])
        self.assertTrue(self.step(grant)['continue_wait'])
        self.assertEqual(self.step(grant)['state'],'indexing_activated')
        return job, grant, receipt

    def resume(self, job_id):
        self.flow.dispatch(job_id, self.engine, resume=True)
        return self.grant_for(self.flow, job_id)[0]


class StorageTests(Fixture):
    def test_additive_init_preserves_original_tokens_receipts_and_index_head(self):
        job, grant, receipt = self.started()
        before = self.uploads.library(), self.uploads.job(job['job_id']), self.cpu.job(job['job_id']), self.indexing.head()
        keys = {p.name:p.read_bytes() for p in self.root.iterdir() if 'token' in p.name or 'secret' in p.name}
        flow = IndexingFlow.initialize(UploadStore(self.root))
        self.assertEqual(before,(self.uploads.library(),self.uploads.job(job['job_id']),self.cpu.job(job['job_id']),self.indexing.head()))
        self.assertEqual(keys,{name:(self.root/name).read_bytes() for name in keys})
        self.assertEqual(self.knowledge.calls,[])
        self.assertTrue(all((self.root/name).stat().st_mode&0o777==0o600 for name in (flow.token_name,flow.hook_name)))
        self.assertNotEqual(flow.token_path.read_bytes(), self.cpu.token_path.read_bytes())
        with self.uploads.connect() as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)

    def test_cli_init_status_never_calls_platform_or_changes_index_binding(self):
        for action in ('init','status'):
            stream = io.StringIO()
            with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('no HTTP')),redirect_stdout(stream):
                self.assertEqual(main([action,'--state-dir',str(self.root)]),0)
            self.assertEqual(json.loads(stream.getvalue())['production_api_requests'],0)
        self.assertIsNone(self.indexing.binding())

    def test_init_requires_prior_indexing_and_CPU_schema_without_partial_tables(self):
        root = Path(self.temp.name)/'missing'
        uploads = UploadStore.initialize(root)
        with self.assertRaises(UploadError):IndexingFlow.initialize(uploads)
        with uploads.connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='indexing_flow_metadata'").fetchone())

    def test_prepare_keeps_entry_and_CPU_resources_unchanged_and_reuses_own_resources(self):
        self.prepare()
        entry, cpu = copy.deepcopy(self.entry.setup()), copy.deepcopy(self.cpu.setup())
        resources = copy.deepcopy(self.engine.resources)
        self.prepare()
        self.assertEqual(entry,self.entry.setup());self.assertEqual(cpu,self.cpu.setup())
        self.assertEqual(resources,self.engine.resources)
        self.assertEqual(len(resources['workflows']),3)
        self.assertFalse(self.flow.status()['configuration_live_verified'])

    def test_unprepared_CPU_and_changed_origin_refuse_before_new_remote_creation(self):
        self.entry.prepare('http://127.0.0.1:17110',self.key,self.entry_engine)
        before = len(self.engine.calls)
        with self.assertRaises(UploadError):self.flow.prepare('http://127.0.0.1:17110',self.key,self.engine)
        self.assertFalse(any(c[0]=='POST' for c in self.engine.calls[before:]))
        with self.assertRaises(UploadError):self.flow.prepare('http://127.0.0.1:17111',self.key,self.engine)
        self.assertEqual(self.knowledge.calls,[])

    def test_lost_remote_workflow_create_reconciles_without_duplicate(self):
        self.entry.prepare('http://127.0.0.1:17110',self.key,self.entry_engine)
        self.cpu.prepare('http://127.0.0.1:17110',self.key,self.cpu_engine)
        self.engine.lost_create='workflows'
        with self.assertRaises(UploadError):self.flow.prepare('http://127.0.0.1:17110',self.key,self.engine)
        self.flow.prepare('http://127.0.0.1:17110',self.key,self.engine)
        self.assertEqual(len(self.engine.resources['workflows']),3)

    def test_noncanonical_CPU_job_cannot_create_indexing_handoff(self):
        self.prepare();job=self.job()
        self.entry.dispatch(job['job_id'],self.entry_engine)
        before=len(self.engine.calls)
        with self.assertRaises(UploadError):self.flow.dispatch(job['job_id'],self.engine)
        self.assertEqual(before,len(self.engine.calls));self.assertIsNone(self.flow.current(job['job_id']))

    def test_workflow_version_edit_blocks_trigger_before_POST(self):
        job=self.canonical_job();setup=self.flow.setup()
        self.engine.resources['workflows'][setup['resources']['workflow']['id']]['nodes'][6]['retryOnFail']=True
        with self.assertRaises(UploadError):self.flow.dispatch(job['job_id'],self.engine)
        self.assertEqual([c for c in self.engine.calls if c[1].startswith('/webhook/')],[])


class GrantTests(Fixture):
    def test_duplicate_execution_receives_same_receipt_but_cannot_execute(self):
        job,grant,receipt=self.started();body=json.loads(self.flow.current(job['job_id'])['body'])
        duplicate={**body,'workflow_id':receipt['workflow_id'],'definition_sha256':receipt['definition_sha256'],'execution_id':'duplicate'}
        self.assertEqual(self.flow.accept_receipt(duplicate),receipt)
        with self.assertRaises(UploadError):self.step({**grant,'execution_id':'duplicate'})
        self.assertEqual(self.factory_calls,[]);self.assertEqual(self.gate_calls,[])

    def test_grant_cannot_change_job_generation_definition_or_enable_inference(self):
        _,grant,_=self.started()
        for key,value in [('job_id',str(uuid4())),('handoff_id',str(uuid4())),('grant_id',str(uuid4())),
                          ('generation',True),('schema',True),('workflow_id','other'),('execution_id','other'),('definition_sha256','a'*64)]:
            with self.subTest(key=key),self.assertRaises(UploadError):self.step({**grant,key:value})
        with self.assertRaises(UploadError):self.step({**grant,'allow_inference':True})
        self.assertEqual(self.factory_calls,[]);self.assertEqual(self.gate_calls,[])

    def test_early_callback_during_trigger_waits_without_effects(self):
        job,grant,_=self.started()
        with flow_lock(self.root,job['job_id']):result=self.step(grant)
        self.assertTrue(result['continue_wait']);self.assertEqual(result['steps'],0)
        self.assertEqual(self.factory_calls,[]);self.assertEqual(self.gate_calls,[])

    def test_lost_webhook_reply_after_receipt_does_not_repeat_trigger(self):
        job=self.canonical_job();self.engine.lost_reply=True
        result=self.flow.dispatch(job['job_id'],self.engine)
        self.assertEqual(result['delivery_state'],'accepted')
        before=len(self.engine.calls);self.flow.dispatch(job['job_id'],self.engine,retry=True)
        self.assertEqual(before,len(self.engine.calls))

    def test_no_callback_trigger_is_not_auto_replayed_and_retry_keeps_body(self):
        job=self.canonical_job();self.engine.no_callback=True
        self.assertEqual(self.flow.dispatch(job['job_id'],self.engine)['delivery_state'],'confirmation_required')
        body=self.flow.current(job['job_id'])['body'];before=len(self.engine.calls)
        self.flow.dispatch(job['job_id'],self.engine);self.assertEqual(before,len(self.engine.calls))
        self.engine.no_callback=False
        result=self.flow.dispatch(job['job_id'],self.engine,retry=True)
        self.assertEqual(result['delivery_state'],'accepted');self.assertEqual(body,self.flow.current(job['job_id'])['body'])

    def test_expired_generation_requires_resume_and_rejects_old_grant(self):
        job,grant,_=self.started();self.epoch+=FLOW_SECONDS
        result=self.step(grant);self.assertEqual(result['state'],'expired');self.assertEqual(self.factory_calls,[])
        second=self.resume(job['job_id'])
        with self.assertRaises(UploadError):self.step(grant)
        self.assertEqual(second['generation'],2)
        self.assertFalse(self.step(second,IndexingGate({}))['continue_wait'])
        with self.uploads.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM indexing_flow_runs').fetchone()[0],2)

    def test_step_limit_and_generation_limit_are_durable(self):
        job,grant,_=self.started()
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE indexing_flow_runs SET steps=?,generation=?',(MAX_STEPS,MAX_GENERATIONS))
            receipt=json.loads(db.execute('SELECT receipt FROM indexing_flow_runs').fetchone()[0]);receipt['generation']=MAX_GENERATIONS
            db.execute('UPDATE indexing_flow_runs SET receipt=?',(canonical(receipt),))
        grant['generation']=MAX_GENERATIONS
        self.assertEqual(self.step(grant)['state'],'expired');self.assertEqual(self.factory_calls,[])
        with self.assertRaises(UploadError):self.resume(job['job_id'])

    def test_superseded_canonical_and_changed_setup_refuse_before_domain_calls(self):
        job,grant,_=self.started()
        with self.uploads.connect(write=True) as db:
            value=json.loads(db.execute('SELECT value FROM indexing_flow_setup').fetchone()[0]);value['published_version_id']='changed'
            db.execute('UPDATE indexing_flow_setup SET value=?',(canonical(value),))
        with self.assertRaises(UploadError):self.step(grant)
        self.assertEqual(self.factory_calls,[])


class ExecutionTests(Fixture):
    def test_paused_generation_cannot_infer_even_if_server_gate_allows_it(self):
        job=self.canonical_job();self.flow.dispatch(job['job_id'],self.engine,paused=True)
        grant,receipt=self.grant_for(self.flow,job['job_id'])
        result=self.step(grant)
        self.assertEqual(receipt['inference_mode'],'paused_check')
        self.assertEqual(result['state'],'blocked');self.assertEqual(result['error_code'],'GPU_VERIFICATION_DEFERRED')
        self.assertEqual(self.gate_calls,[]);self.assertEqual(self.factory_calls,[])

    def test_default_policy_blocks_before_any_model_reader_or_Knowledge_factory(self):
        job,grant,_=self.started()
        before=self.indexing.head(),self.uploads.job(job['job_id'])
        gate=IndexingGate({},reader_factory=lambda budget:self.fail('no metadata'))
        result=self.step(grant,gate)
        self.assertEqual(result['state'],'blocked');self.assertEqual(result['error_code'],'GPU_VERIFICATION_DEFERRED')
        self.assertFalse(result['continue_wait']);self.assertFalse(result['question_ready'])
        self.assertEqual(self.factory_calls,[]);self.assertIsNone(self.indexing.binding())
        self.assertEqual(before,(self.indexing.head(),self.uploads.job(job['job_id'])))
        self.step(grant);self.assertEqual(self.factory_calls,[])

    def test_missing_defaults_block_without_creating_remote_index(self):
        _,grant,_=self.started()
        result=self.step(grant,IndexingGate({},allow_inference=True))
        self.assertEqual(result['state'],'blocked');self.assertEqual(result['error_code'],'INDEXING_DEFAULTS_NOT_READY')
        self.assertEqual(self.knowledge.calls,[])

    def test_enabling_policy_after_block_requires_explicit_workflow_resume(self):
        job,grant,_=self.started();self.step(grant,IndexingGate({}))
        self.step(grant);self.assertEqual(self.factory_calls,[])
        second=self.resume(job['job_id']);self.step(second)
        self.assertEqual(len(self.kb_posts('/api/indexes')),1)

    def test_authorized_path_creates_builds_observes_and_activates_as_separate_steps(self):
        job,grant,_=self.activate_job()
        self.assertEqual([c[1].rsplit('/',1)[-1] for c in self.kb_posts()],['indexes','builds','activate'])
        self.assertEqual(self.indexing.head()['generation'],1);self.assertFalse(self.uploads.library()['question_ready'])
        before=len(self.knowledge.calls);self.step(grant);self.assertEqual(before,len(self.knowledge.calls))
        self.assertTrue(all(0<r['budget_seconds']<=125 for r in self.factory_calls))
        self.assertTrue(any(not r['execute'] for r in self.factory_calls))
        with self.assertRaises(UploadError):self.resume(job['job_id'])

    def test_waiting_build_is_observed_while_paused_but_activation_is_blocked(self):
        job,grant,_=self.build_job();self.mark_ready(job['job_id'])
        paused=IndexingGate({})
        self.assertTrue(self.step(grant,paused)['continue_wait'])
        self.assertFalse(self.factory_calls[-1]['execute'])
        result=self.step(grant,paused)
        self.assertEqual(result['state'],'blocked');self.assertEqual(self.kb_posts('/activate'),[])
        self.assertEqual(self.indexing.head()['generation'],0)

    def test_lost_build_receipt_resume_never_reposts_build(self):
        job,grant,_=self.started();self.step(grant);self.knowledge.lose_build=True
        self.assertEqual(self.step(grant)['state'],'attention_required')
        count=len(self.kb_posts('/builds'));second=self.resume(job['job_id']);self.step(second)
        self.assertEqual(len(self.kb_posts('/builds')),count)
        self.assertEqual(self.indexing.job(job['job_id'])['state'],'confirmation_required')

    def test_lost_create_receipt_resume_keeps_same_unknown_intent_without_POST(self):
        job,grant,_=self.started();self.knowledge.lose_create=True
        self.assertEqual(self.step(grant)['state'],'attention_required')
        second=self.resume(job['job_id']);self.step(second)
        self.assertEqual(len(self.kb_posts('/api/indexes')),1)
        self.assertIsNone(self.indexing.binding()['index_id'])

    def test_unknown_activation_recovers_by_GET_after_reopen_without_replay(self):
        job,grant,_=self.build_job();self.mark_ready(job['job_id']);self.step(grant)
        self.knowledge.interrupt_activate=True
        with self.assertRaises(KeyboardInterrupt):self.step(grant)
        reopened=IndexingFlow(UploadStore(self.root),clock=lambda:self.epoch)
        result=reopened.step(grant,self.factory,IndexingGate({}))
        self.assertEqual(result['state'],'indexing_activated');self.assertEqual(len(self.kb_posts('/activate')),1)
        self.assertFalse(self.factory_calls[-1]['execute'])

    def test_unknown_uncommitted_activation_is_not_replayed_on_resume(self):
        job,grant,_=self.build_job();self.mark_ready(job['job_id']);self.step(grant)
        self.knowledge.activate_before_loss=False;self.knowledge.lose_activate=True
        self.assertEqual(self.step(grant)['state'],'attention_required')
        second=self.resume(job['job_id']);self.step(second,IndexingGate({}))
        self.assertEqual(len(self.kb_posts('/activate')),1);self.assertEqual(self.indexing.head()['generation'],0)

    def test_two_documents_preserve_old_active_version_when_new_build_fails(self):
        first,_,_=self.activate_job();head=self.indexing.head()
        second,grant,_=self.started(self.canonical_job(DATA+b'second document'));self.step(grant)
        version=self.indexing.job(second['job_id'])['index_version_id']
        self.knowledge.versions[version].update(state='failed',error_code='PRIVATE_FAILURE')
        result=self.step(grant)
        self.assertEqual(result['state'],'attention_required');self.assertEqual(self.indexing.head()['version_id'],head['version_id'])
        self.assertEqual(len(self.indexing.head()['documents']),1);self.assertEqual(len(self.kb_posts('/activate')),1)

    def test_library_lane_waits_without_creating_second_build(self):
        first,first_grant,_=self.build_job();second,grant,_=self.started(self.canonical_job(DATA+b'second document'))
        result=self.step(grant)
        self.assertTrue(result['continue_wait']);self.assertEqual(result['error_code'],'LIBRARY_INDEXING_BUSY')
        self.assertEqual(len(self.kb_posts('/builds')),1)
        self.mark_ready(first['job_id']);self.step(first_grant);self.step(first_grant)
        self.step(grant);self.assertEqual(len(self.kb_posts('/builds')),2)
        request=self.kb_posts('/builds')[-1][2]
        self.assertEqual(len(request['chunking']['canonical_ids']),2)

    def test_deadline_consumed_by_gate_prevents_a_late_write(self):
        _,grant,_=self.started()
        def gate(budget):self.epoch+=FLOW_SECONDS;return copy.deepcopy(DEFAULTS)
        result=self.step(grant,gate)
        self.assertEqual(result['state'],'expired');self.assertEqual(self.knowledge.calls,[])

    def test_metadata_and_domain_share_one_callback_budget(self):
        _,grant,_=self.started()
        def gate(budget):self.epoch+=20;return copy.deepcopy(DEFAULTS)
        self.step(grant,gate)
        self.assertEqual(self.factory_calls[-1]['budget_seconds'],105)

    def test_contract_rejection_ends_flow_instead_of_polling_or_reposting(self):
        _,grant,_=self.started();self.step(grant)
        with patch.object(self.flow.indexing,'dispatch',side_effect=ContractError('INVALID_DOCUMENT_SNAPSHOT')):
            result=self.step(grant)
        self.assertEqual(result['state'],'attention_required');self.assertFalse(result['continue_wait'])
        self.assertEqual(result['error_code'],'INDEXING_CONTRACT_UNVERIFIED')
        self.assertEqual(self.kb_posts('/builds'),[])


class RuntimeGateTests(unittest.TestCase):
    def reader(self):
        reader=MetadataReader()
        reader.deps=[reader.f['embedding']]
        for row in [reader.f['embedding'],reader.f['profile']]:
            row.update(deployment_id=DEFAULTS['embedding']['deployment_id'],model_version_id=DEFAULTS['embedding']['model_version_id'],served_model_name='deployment-'+DEFAULTS['embedding']['deployment_id'])
        reader.f['profile']['max_input_tokens']=8192
        return reader
    def configured(self,reader):
        value=settings();value['defaults']=copy.deepcopy(DEFAULTS)
        return IndexingGate(value,True,lambda _:reader)
    def test_selected_embedding_and_chunker_use_only_three_GETs(self):
        reader=self.reader();self.assertEqual(self.configured(reader)(125),DEFAULTS)
        self.assertEqual(reader.calls,3)
    def test_stopped_model_blocks_before_profile_and_Knowledge_read(self):
        reader=self.reader();reader.f['embedding']['runtime_state']='stopped'
        with self.assertRaises(readiness.ReadinessError):self.configured(reader)(125)
        self.assertEqual(reader.calls,1)
    def test_changed_space_or_chunker_version_never_selects_another_candidate(self):
        reader=self.reader();reader.f['profile']['embedding_space_id']='es_'+'f'*64
        with self.assertRaises(UploadError):self.configured(reader)(125)
        self.assertEqual(reader.calls,2)
        reader=self.reader()
        next(item for item in reader.f['chunkers']['items'] if item['id']=='structure_v1')['version']='changed'
        with self.assertRaises(readiness.ReadinessError):self.configured(reader)(125)
        self.assertEqual(reader.calls,3)


class HTTPTests(Fixture):
    def server(self,gate=None):
        return make_server(self.uploads,self.uploaded,port=0,workflow=self.entry,
            refinement=self.refinement,refiner_factory=lambda:self.data,refinement_flow=self.cpu,
            indexing=self.indexing,indexer_factory=self.factory,indexing_flow=self.flow,indexing_gate=gate or IndexingGate({}))
    def request(self,port,path,body=None,token=None,method='POST'):
        con=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
        auth=token or self.uploads.token_path.read_text().strip()
        con.request(method,path,json.dumps(body).encode() if body is not None else None,{'Authorization':'Bearer '+auth,'Content-Type':'application/json'})
        response=con.getresponse();value=json.loads(response.read());con.close();return response.status,value
    def test_indexing_callbacks_reject_operator_entry_and_CPU_tokens(self):
        _,grant,_=self.started()
        with running(self.server()) as port:
            for token in [self.uploads.token_path,self.entry.token_path,self.cpu.token_path]:
                code,_=self.request(port,'/api/workflow/indexing/step',grant,token.read_text().strip())
                self.assertEqual(code,401)
            code,result=self.request(port,'/api/workflow/indexing/step',grant,self.flow.token_path.read_text().strip())
            self.assertEqual(code,200);self.assertEqual(result['state'],'blocked')
        self.assertEqual(self.factory_calls,[])
    def test_indexing_key_cannot_call_upload_CPU_or_operator_routes(self):
        self.prepare()
        with running(self.server()) as port:
            token=self.flow.token_path.read_text().strip()
            for path in ['/api/uploads','/api/workflow/refinement/step','/api/workflow/receipts','/api/indexing-policy']:
                code,_=self.request(port,path,{},token);self.assertEqual(code,401)
    def test_readonly_operator_routes_do_not_start_work_or_expose_grant(self):
        job,grant,_=self.started()
        with running(self.server()) as port:
            for path in ['/api/jobs/'+job['job_id'],'/api/jobs/'+job['job_id']+'/indexing','/api/jobs/'+job['job_id']+'/indexing-workflow','/api/indexings','/api/indexing-workflows','/api/indexing-policy']:
                code,value=self.request(port,path,method='GET');self.assertEqual(code,200)
                self.assertNotIn(grant['grant_id'],canonical(value))
        self.assertEqual(self.factory_calls,[])
    def test_operator_handoff_cannot_enable_inference_in_request(self):
        job,_,_=self.started()
        with running(self.server()) as port:
            code,_=self.request(port,'/api/jobs/'+job['job_id']+'/dispatch-indexing-workflow',{'allow_inference':True})
            self.assertEqual(code,400)
        self.assertEqual(self.factory_calls,[])
    def test_CPU_completion_automatically_hands_off_to_separate_indexing_flow(self):
        self.prepare();job=self.job();self.entry.dispatch(job['job_id'],self.entry_engine);self.cpu.dispatch(job['job_id'],self.cpu_engine)
        cpu_grant,_=self.grant_for(self.cpu,job['job_id']);self.cpu.step(cpu_grant,lambda:self.data)
        run_id=self.refinement.job(job['job_id'])['receipt']['resource_id'];self.data.runs[run_id].update(status='succeeded',phase='completed')
        with running(self.server()) as port:
            token=self.flow.token_path.read_text().strip()
            self.engine.callback=lambda value:self.request(port,'/api/workflow/indexing/receipts',value,token)[1]
            with patch('apps.document_qa.indexing_flow.key_client',return_value=self.engine):
                code,result=self.request(port,'/api/workflow/refinement/step',cpu_grant,self.cpu.token_path.read_text().strip())
                self.assertEqual(code,200);self.assertEqual(result['state'],'canonical_verified')
                self.assertEqual(result['indexing_workflow']['delivery_state'],'accepted')
                count=len(self.engine.calls)
                self.request(port,'/api/workflow/refinement/step',cpu_grant,self.cpu.token_path.read_text().strip())
                self.assertEqual(len(self.engine.calls),count)
            grant,_=self.grant_for(self.flow,job['job_id'])
            code,result=self.request(port,'/api/workflow/indexing/step',grant,token)
            self.assertEqual(code,200);self.assertEqual(result['error_code'],'GPU_VERIFICATION_DEFERRED')
        self.assertEqual(self.factory_calls,[])


class ConnectionTests(Fixture):
    server=HTTPTests.server
    request=HTTPTests.request

    def connection(self,action,port,job_id=None):
        args=[action,'--state-dir',str(self.root),'--qa-origin','http://127.0.0.1:'+str(port),
              '--engine-key-file',str(self.key)]
        if job_id:args+=['--job-id',job_id]
        out=io.StringIO()
        with redirect_stdout(out),patch('apps.document_qa.indexing_connection.key_client',return_value=self.engine):
            result=connection_main(args)
        return result,json.loads(out.getvalue())

    def test_enabled_server_policy_is_refused_before_any_remote_registration(self):
        with running(self.server(IndexingGate({},True))) as port:
            code,result=self.connection('prepare-paused',port)
        self.assertEqual(code,2);self.assertEqual(result['code'],'PAUSED_API_POLICY_MISMATCH')
        self.assertEqual(self.engine.calls,[]);self.assertEqual(self.knowledge.calls,[])

    def test_missing_stage_API_is_refused_before_registration(self):
        old=make_server(self.uploads,self.uploaded,port=0,workflow=self.entry)
        with running(old) as port:code,result=self.connection('prepare-paused',port)
        self.assertEqual(code,2);self.assertEqual(self.engine.calls,[])
        self.assertEqual(self.knowledge.calls,[])

    def test_preflight_verifies_three_GETs_without_reading_model_or_creating_handoff(self):
        with running(self.server()) as port:code,result=self.connection('check',port)
        self.assertEqual(code,0);self.assertEqual(result['qa_GET_requests'],3)
        self.assertTrue(result['server_inference_disabled_verified'])
        self.assertEqual(self.engine.calls,[]);self.assertEqual(self.knowledge.calls,[])

    def test_paused_acceptance_commits_real_HTTP_grant_and_never_builds(self):
        # Registration pins the current loopback server address.
        self.epoch=time.time()
        server=self.server();port=server.server_address[1]
        origin='http://127.0.0.1:'+str(port)
        self.entry.prepare(origin,self.key,self.entry_engine)
        self.cpu.prepare(origin,self.key,self.cpu_engine)
        job=self.job();self.entry.dispatch(job['job_id'],self.entry_engine);self.cpu.dispatch(job['job_id'],self.cpu_engine)
        grant,_=self.grant_for(self.cpu,job['job_id']);self.cpu.step(grant,lambda:self.data)
        run_id=self.refinement.job(job['job_id'])['receipt']['resource_id'];self.data.runs[run_id].update(status='succeeded',phase='completed')
        self.cpu.step(grant,lambda:self.data)
        workers=[]
        with running(server):
            code,result=self.connection('prepare-paused',port)
            self.assertEqual(code,0);self.assertTrue(result['published_definition_readback_verified'])
            token=self.flow.token_path.read_text().strip()
            def callback(value):
                receipt=self.request(port,'/api/workflow/indexing/receipts',value,token)[1]
                grant={k:receipt[k] for k in ('schema','handoff_id','job_id','generation','workflow_id','definition_sha256','grant_id')}
                grant['execution_id']=receipt['first_execution_id']
                def owned_steps():
                    import time
                    for _ in range(100):
                        code,result=self.request(port,'/api/workflow/indexing/step',grant,token)
                        if code==200 and not result['continue_wait']:return
                        time.sleep(0.01)
                    self.fail('owned step failed to finish')
                worker=threading.Thread(target=owned_steps);worker.start();workers.append(worker)
                return receipt
            self.engine.callback=callback
            code,result=self.connection('accept-paused',port,job['job_id'])
            for worker in workers:worker.join(5);self.assertFalse(worker.is_alive())
            self.assertEqual(code,0,result);self.assertTrue(result['n8n_callback_execution_verified'])
            self.assertEqual(result['workflow']['state'],'blocked');self.assertFalse(result['existing_handoff_reused'])
            before=len(self.engine.calls)
            code,result=self.connection('accept-paused',port,job['job_id'])
            self.assertEqual(code,0);self.assertTrue(result['existing_handoff_reused'])
            self.assertEqual(len(self.engine.calls),before)
        self.assertEqual(self.knowledge.calls,[]);self.assertEqual(self.factory_calls,[])


@unittest.skipUnless(shutil.which('node'),'Node required for real n8n Code-node validation')
class TemplateTests(Fixture):
    def js(self,code,input,values=None,workflow='flow',execution='execution'):
        source="const x=JSON.parse(process.argv[1]);const i={first:()=>({json:x.input})};const $=n=>({first:()=>({json:x.values[n]})});try{console.log(JSON.stringify({result:new Function('$input','$','$workflow','$execution','require',x.code)(i,$,{id:x.workflow},{id:x.execution},require)}));}catch(e){console.log(JSON.stringify({error:e.message}));}"
        result=subprocess.run(['node','-e',source,json.dumps({'code':code,'input':input,'values':values or {},'workflow':workflow,'execution':execution})],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
    def test_definition_uses_separate_callbacks_early_owner_gate_and_limits(self):
        flow,_=build('http://127.0.0.1:17110','document-qa-'+str(uuid4()),'qa','hook')
        self.assertEqual(len(flow['nodes']),9);self.assertEqual(flow['settings']['executionTimeout'],1200)
        self.assertEqual(flow['nodes'][4]['type'],'n8n-nodes-base.respondToWebhook')
        self.assertEqual(flow['nodes'][5]['name'],'Own execution grant')
        self.assertFalse(any(n.get('retryOnFail') for n in flow['nodes']))
        self.assertEqual(flow['nodes'][6]['parameters']['options']['timeout'],135000)
        urls={n['parameters']['url'] for n in flow['nodes'] if n['type'].endswith('httpRequest')}
        self.assertEqual(urls,{'http://127.0.0.1:17110/api/workflow/indexing/receipts','http://127.0.0.1:17110/api/workflow/indexing/step'})
        actual=copy.deepcopy(flow);actual['nodes'][-1]['webhookId']=str(uuid4())
        self.assertTrue(matches(actual,flow))
    def test_JWT_validator_binds_canonical_and_generation_and_rejects_extra_fields(self):
        job,grant,_=self.started();setup=self.flow.setup();flow,_=self.flow.desired(setup)
        body=json.loads(self.flow.current(job['job_id'])['body'])
        jwt=signed_headers(self.flow.hook_path.read_text().strip(),setup['path'],body)['Authorization'].split('.')[1]
        claims=json.loads(base64.urlsafe_b64decode(jwt+'='*(-len(jwt)%4)))
        code=flow['nodes'][1]['parameters']['jsCode']
        self.assertIn('result',self.js(code,{'body':body,'jwtPayload':claims}))
        for changes in [{'generation':0},{'generation':True},{'canonical_id':str(uuid4())},{'allow_inference':True}]:
            self.assertIn('error',self.js(code,{'body':{**body,**changes},'jwtPayload':claims}))
    def test_receipt_owner_is_first_execution_and_cannot_enable_inference(self):
        job,grant,receipt=self.started();body=json.loads(self.flow.current(job['job_id'])['body'])
        received={**body,'workflow_id':receipt['workflow_id'],'definition_sha256':receipt['definition_sha256'],'execution_id':receipt['first_execution_id']}
        self.assertEqual(self.js(VERIFY,{'statusCode':200,'body':receipt},{'Validate handoff':received})['result'],[{'json':receipt}])
        self.assertEqual(self.js(OWNER,receipt,{'Validate handoff':received})['result'],[{'json':grant}])
        self.assertEqual(self.js(OWNER,receipt,{'Validate handoff':{**received,'execution_id':'duplicate'}})['result'],[])
        self.assertIn('error',self.js(VERIFY,{'statusCode':200,'body':{**receipt,'may_enable_inference':True}},{'Validate handoff':received}))
    def test_result_gate_accepts_blocked_and_ready_but_only_loops_running(self):
        job,grant,_=self.started();blocked=self.step(grant,IndexingGate({}))
        self.assertEqual(self.js(RESULT,{'statusCode':200,'body':blocked},{'Own execution grant':grant})['result'],[])
        self.assertIn('error',self.js(RESULT,{'statusCode':200,'body':{**blocked,'continue_wait':True}},{'Own execution grant':grant}))
        second=self.resume(job['job_id']);pending=self.step(second)
        self.assertEqual(self.js(RESULT,{'statusCode':200,'body':pending},{'Own execution grant':second})['result'],[{'json':pending}])

    def test_ready_index_is_not_activated_success_until_readback_completes(self):
        job,grant,_=self.build_job();self.mark_ready(job['job_id'])
        pending=self.step(grant)
        self.assertEqual(pending['indexing']['state'],'index_ready')
        self.assertEqual(self.js(RESULT,{'statusCode':200,'body':pending},{'Own execution grant':grant})['result'],[{'json':pending}])
        done=self.step(grant)
        self.assertEqual(self.js(RESULT,{'statusCode':200,'body':done},{'Own execution grant':grant})['result'],[])
        bad={**pending,'state':'indexing_activated','continue_wait':False}
        self.assertIn('error',self.js(RESULT,{'statusCode':200,'body':bad},{'Own execution grant':grant}))
