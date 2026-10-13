"""Q05b separately owned indexing flow; domain writes obey server inference policy."""
import argparse
from contextlib import contextmanager
import hmac
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4, uuid5, NAMESPACE_URL

from . import readiness
from .contracts import ContractError, fingerprint, uid
from .intake import check, UploadError
from .uploads import UploadStore, file_lock, now
from .workflows import WorkflowStore, canonical, engine_id, key_client, private_secret, signed_headers, MAX_DELIVERIES
from .indexing import IndexingStore
from .indexing_results import canonical_input, defaults_checked
from .refinement_flow import RefinementFlow
from .indexing_flow_template import build

MAX_STEPS = 80
MAX_GENERATIONS = 16
FLOW_SECONDS = 1200
WAIT_SECONDS = 5
SCHEMA = '''
CREATE TABLE indexing_flow_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL CHECK(version=1));
INSERT INTO indexing_flow_metadata VALUES(1,1);
CREATE TABLE indexing_flow_setup (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL);
CREATE TABLE indexing_flow_runs (
 handoff_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id), generation INTEGER NOT NULL,
 body TEXT NOT NULL, setup_sha256 TEXT NOT NULL,
 delivery_state TEXT NOT NULL CHECK(delivery_state IN ('pending','confirmation_required','accepted')),
 attempts INTEGER NOT NULL DEFAULT 0, receipt TEXT,
 flow_state TEXT NOT NULL DEFAULT 'awaiting_execution', steps INTEGER NOT NULL DEFAULT 0,
 deadline REAL, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(job_id,generation), CHECK((delivery_state='accepted')=(receipt IS NOT NULL))
);
CREATE TABLE indexing_flow_current (job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), handoff_id TEXT NOT NULL REFERENCES indexing_flow_runs(handoff_id));
CREATE TABLE indexing_flow_executions (handoff_id TEXT NOT NULL REFERENCES indexing_flow_runs(handoff_id), execution_id TEXT NOT NULL, PRIMARY KEY(handoff_id,execution_id));
'''


@contextmanager
def flow_lock(root, job_id):
    check(uid(job_id), 'JOB_ID_INVALID')
    try:
        with file_lock(root / 'locks' / (job_id + '.indexing-flow.lock')):
            yield
    except UploadError as exc:
        if exc.code == 'UPLOAD_BUSY':
            raise UploadError('INDEXING_FLOW_BUSY', 409) from None
        raise


class IndexingFlow(WorkflowStore):
    metadata_table = 'indexing_flow_metadata'
    setup_table = 'indexing_flow_setup'
    token_name = 'indexing-workflow-api-token'
    hook_name = 'indexing-workflow-hook-secret'
    init_lock = 'indexing-flow-init.lock'
    schema = SCHEMA

    def __init__(self, uploads, clock=time.time):
        with uploads.connect() as db:
            check(db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (self.metadata_table,)).fetchone()[0] == 1,
                  'INDEXING_FLOW_NOT_INITIALIZED', 503)
        super().__init__(uploads)
        self.entry = WorkflowStore(uploads)
        self.indexing = IndexingStore(uploads)
        self.cpu = RefinementFlow(uploads)
        self.clock = clock

    @classmethod
    def initialize(cls, uploads):
        WorkflowStore(uploads)
        IndexingStore(uploads)
        RefinementFlow(uploads)
        return super().initialize(uploads)

    def desired(self, setup):
        return build(setup['qa_origin'], setup['path'], setup['resources']['qa_credential']['id'],
                     setup['resources']['hook_credential']['id'])

    def prepare(self, qa_origin, key_file, client=None):
        # Q03b handoffs and its published workflow are never rewritten.
        entry = self.entry.setup()
        check(entry.get('prepared'), 'ENTRY_WORKFLOW_NOT_CONFIGURED', 503)
        api = client or key_client(Path(key_file))
        check(entry['qa_origin'] == readiness.origin(qa_origin) and entry['engine_origin'] == api.origin,
              'INDEXING_FLOW_TARGET_CHANGED', 409)
        self.entry.verify_setup(entry, api)
        cpu = self.cpu.setup()
        check(cpu.get('prepared') and cpu['qa_origin'] == entry['qa_origin']
              and cpu['engine_origin'] == api.origin, 'CPU_WORKFLOW_NOT_CONFIGURED', 503)
        self.cpu.verify_setup(cpu, api)
        return super().prepare(qa_origin, key_file, api)

    def current(self, job_id):
        self.uploads.job(job_id)
        with self.uploads.connect() as db:
            return db.execute('SELECT r.* FROM indexing_flow_runs r JOIN indexing_flow_current c ON c.handoff_id=r.handoff_id WHERE c.job_id=?', (job_id,)).fetchone()

    def projected(self, row):
        if row is None:
            return {'state':'not_requested','workflow_started':False,'question_ready':False}
        receipt = json.loads(row['receipt']) if row['receipt'] else None
        state = row['flow_state']
        if state in ('awaiting_execution','running') and row['deadline'] is not None and self.clock() >= row['deadline']:
            state = 'expired'
        return {'handoff_id':row['handoff_id'],'job_id':row['job_id'],'generation':row['generation'],
                'delivery_state':row['delivery_state'],'state':state,'attempts':row['attempts'],
                'steps':row['steps'],'deadline':row['deadline'],'error_code':row['error_code'],
                'receipt':{k:v for k,v in receipt.items() if k != 'grant_id'} if receipt else None,
                'workflow_started':bool(receipt),'question_ready':False}

    def job(self, job_id):
        return {'job_id':job_id,**self.projected(self.current(job_id))}

    def project_job(self, job):
        return {**job,'indexing_workflow':self.job(job['job_id'])}

    def status(self):
        setup = self.setup(False)
        with self.uploads.connect() as db:
            counts = {r[0]:r[1] for r in db.execute('SELECT flow_state,count(*) FROM indexing_flow_runs GROUP BY flow_state')}
        return {'schema':1,'indexing_flow_schema':1,'upload_schema':1,
                'workflow_configured':bool(setup and setup.get('prepared')),'configuration_live_verified':False,
                'n8n_indexing_flow_implemented':True,'flow_counts':counts,'question_ready':False,
                'inference_policy':'explicit_server_opt_in_required',
                'max_steps':MAX_STEPS,'wait_seconds':WAIT_SECONDS,'budget_seconds':FLOW_SECONDS,
                'next_step':'Q05b_paused_live_callback_acceptance' if setup and setup.get('prepared') else 'explicit_indexing_flow_setup'}

    def source_job(self, job_id):
        job = self.uploads.job(job_id)
        handoff = self.entry.handoff(job_id)
        check(handoff['state'] == 'accepted' and handoff['receipt']['source_document_id'] == job['source_document_id'],
              'ENTRY_RECEIPT_REQUIRED', 409)
        cpu = self.cpu.job(job_id)
        check(cpu.get('delivery_state') == 'accepted' and cpu.get('state') == 'canonical_verified',
              'CPU_WORKFLOW_CANONICAL_REQUIRED', 409)
        item = canonical_input(job, self.indexing.refinement.job(job_id))
        return {**job, 'canonical_id':item['canonical_id']}

    def intent(self, job_id, resume=False, paused=False):
        job, setup = self.source_job(job_id), self.setup()
        check(setup.get('prepared'), 'INDEXING_FLOW_NOT_CONFIGURED', 503)
        previous = self.current(job_id)
        if previous and not resume:
            check(previous['setup_sha256'] == fingerprint(setup), 'INDEXING_FLOW_INTENT_CHANGED', 409)
            return previous
        generation = 1
        if previous:
            state = self.projected(previous)['state']
            check(state in ('blocked','attention_required','expired'), 'INDEXING_FLOW_STILL_ACTIVE', 409)
            check(not self.indexing.job(job_id).get('indexing_active'), 'INDEXING_ALREADY_COMPLETE', 409)
            generation = previous['generation'] + 1
        check(generation <= MAX_GENERATIONS, 'INDEXING_FLOW_REVIEW_REQUIRED', 409)
        handoff_id = str(uuid5(NAMESPACE_URL,'document-qa-q05b/' + job_id + '/' + str(generation)))
        body = {'schema':1,'handoff_id':handoff_id,'job_id':job_id,'generation':generation,
                'library_id':self.uploads.library()['library_id'],'sequence':job['sequence'],
                'source_document_id':job['source_document_id'],'canonical_id':job['canonical_id'],
                'inference_mode':'paused_check' if paused else 'server_policy'}
        with self.uploads.connect(write=True) as db:
            stamp = now()
            db.execute('INSERT INTO indexing_flow_runs(handoff_id,job_id,generation,body,setup_sha256,delivery_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       (handoff_id,job_id,generation,canonical(body),fingerprint(setup),'pending',stamp,stamp))
            db.execute('INSERT INTO indexing_flow_current VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET handoff_id=excluded.handoff_id',(job_id,handoff_id))
        return self.current(job_id)

    def dispatch(self, job_id, client=None, retry=False, resume=False, paused=False):
        with flow_lock(self.root, job_id):
            row = self.current(job_id)
            if row and not resume and (row['receipt'] or row['attempts'] and not retry):
                return self.job(job_id)
            row = self.intent(job_id,resume,paused)
            setup = self.setup()
            api = client or key_client(Path(setup['engine_key_file']))
            self.verify_setup(setup,api)
            with self.uploads.connect(write=True) as db:
                check(row['attempts'] < MAX_DELIVERIES,'INDEXING_FLOW_REVIEW_REQUIRED',409)
                # A lost trigger is replayed only explicitly, using this same saved body.
                db.execute("UPDATE indexing_flow_runs SET delivery_state='confirmation_required',attempts=attempts+1,error_code='INDEXING_FLOW_OUTCOME_UNKNOWN',updated_at=? WHERE handoff_id=?",(now(),row['handoff_id']))
            body = json.loads(row['body'])
            try:
                response = api.request('POST','/webhook/' + setup['path'],body,
                                       signed_headers(private_secret(self.hook_path),setup['path'],body))
                with self.uploads.connect() as db:
                    accepted = db.execute('SELECT receipt FROM indexing_flow_runs WHERE handoff_id=?',(row['handoff_id'],)).fetchone()[0]
                check(accepted is not None and canonical(response) == accepted,'INDEXING_FLOW_RECEIPT_UNCONFIRMED',502)
            except UploadError as exc:
                with self.uploads.connect(write=True) as db:
                    db.execute("UPDATE indexing_flow_runs SET error_code=?,updated_at=? WHERE handoff_id=? AND delivery_state!='accepted'",(exc.code,now(),row['handoff_id']))
            return self.job(job_id)

    def accept_receipt(self,value):
        expected = {'schema','handoff_id','job_id','generation','library_id','sequence','source_document_id','canonical_id','inference_mode','workflow_id','execution_id','definition_sha256'}
        check(isinstance(value,dict) and set(value) == expected,'INDEXING_FLOW_INPUT_INVALID')
        check(all(uid(value[k]) for k in ('handoff_id','job_id','library_id','source_document_id','canonical_id')),
              'INDEXING_FLOW_INPUT_INVALID')
        execution_id = engine_id(value['execution_id'])
        setup = self.setup()
        check(value['workflow_id'] == setup['resources']['workflow']['id'] and value['definition_sha256'] == setup['definition_sha256'],
              'INDEXING_FLOW_TARGET_CHANGED',409)
        check(self.source_job(value['job_id'])['canonical_id'] == value['canonical_id'],
              'INDEXING_CANONICAL_CHANGED', 409)
        with self.uploads.connect(write=True) as db:
            row = db.execute('SELECT r.* FROM indexing_flow_runs r JOIN indexing_flow_current c ON c.handoff_id=r.handoff_id WHERE r.handoff_id=? AND c.job_id=?',(value['handoff_id'],value['job_id'])).fetchone()
            check(row is not None,'INDEXING_FLOW_SUPERSEDED',409)
            body = {k:value[k] for k in expected - {'workflow_id','execution_id','definition_sha256'}}
            check(canonical(body) == row['body'] and fingerprint(setup) == row['setup_sha256'] and row['attempts'] > 0,
                  'INDEXING_FLOW_INTENT_CHANGED',409)
            seen = db.execute('SELECT 1 FROM indexing_flow_executions WHERE handoff_id=? AND execution_id=?',(row['handoff_id'],execution_id)).fetchone()
            if not seen:
                check(db.execute('SELECT count(*) FROM indexing_flow_executions WHERE handoff_id=?',(row['handoff_id'],)).fetchone()[0] < MAX_DELIVERIES,
                      'INDEXING_FLOW_REVIEW_REQUIRED',409)
                db.execute('INSERT INTO indexing_flow_executions VALUES(?,?)',(row['handoff_id'],execution_id))
            if row['receipt']:
                return json.loads(row['receipt'])
            receipt = {**body,'workflow_id':value['workflow_id'],'definition_sha256':value['definition_sha256'],
                       'published_version_id':setup['published_version_id'],'first_execution_id':execution_id,
                       'grant_id':str(uuid4()),'phase':'indexing','may_execute_next_stage':True,
                       'may_enable_inference':False,'question_ready':False}
            db.execute("UPDATE indexing_flow_runs SET delivery_state='accepted',receipt=?,flow_state='running',deadline=?,error_code=NULL,updated_at=? WHERE handoff_id=?",(canonical(receipt),self.clock()+FLOW_SECONDS,now(),row['handoff_id']))
            return receipt

    def grant(self,value):
        expected = {'schema','handoff_id','job_id','generation','workflow_id','execution_id','definition_sha256','grant_id'}
        check(isinstance(value,dict) and set(value) == expected and value.get('schema') == 1 and type(value['schema']) is int,
              'INDEXING_GRANT_INVALID',403)
        check(uid(value.get('job_id')) and uid(value.get('handoff_id')) and uid(value.get('grant_id')),'INDEXING_GRANT_INVALID',403)
        row = self.current(value['job_id'])
        check(row is not None and row['handoff_id'] == value['handoff_id'] and row['receipt'],'INDEXING_GRANT_INVALID',403)
        receipt = json.loads(row['receipt'])
        check(all(value[k] == receipt[k] and type(value[k]) is type(receipt[k]) for k in
                  ('schema','handoff_id','job_id','generation','workflow_id','definition_sha256'))
              and value['execution_id'] == receipt['first_execution_id']
              and hmac.compare_digest(value['grant_id'],receipt['grant_id']), 'INDEXING_GRANT_INVALID',403)
        check(self.source_job(value['job_id'])['canonical_id'] == receipt['canonical_id'],
              'INDEXING_CANONICAL_CHANGED', 409)
        check(row['setup_sha256'] == fingerprint(self.setup()), 'INDEXING_FLOW_INTENT_CHANGED', 409)
        return row

    def step(self, value, api_factory, gate):
        self.grant(value)
        try:
            with flow_lock(self.root, value['job_id']):
                row = self.grant(value)
                return self.step_locked(row, value, api_factory, gate)
        except UploadError as exc:
            if exc.code != 'INDEXING_FLOW_BUSY':
                raise
            return self.result(self.current(value['job_id']), value['execution_id'])

    def step_locked(self, row, value, api_factory, gate):
        if row['flow_state'] != 'running':
            return self.result(row, value['execution_id'])
        if self.clock() >= row['deadline'] or row['steps'] >= MAX_STEPS:
            self.finish(row['handoff_id'], 'expired', 'INDEXING_FLOW_BUDGET_EXCEEDED')
            return self.result(self.current(value['job_id']), value['execution_id'])
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE indexing_flow_runs SET steps=steps+1,updated_at=? WHERE handoff_id=?',
                       (now(), row['handoff_id']))
        action_deadline = min(row['deadline'], self.clock() + 125)
        def remaining():
            budget = action_deadline - self.clock()
            check(budget > 0, 'INDEXING_FLOW_BUDGET_EXCEEDED', 504)
            return budget
        def api(write=False):
            return api_factory(execute=write, allow_inference=write, budget_seconds=remaining())
        def authorized():
            check(json.loads(row['body'])['inference_mode'] == 'server_policy', 'GPU_VERIFICATION_DEFERRED', 409)
            defaults = defaults_checked(gate(remaining()))
            binding = self.indexing.binding()
            check(not binding or binding['config'] == canonical(defaults), 'INDEXING_BINDING_CHANGED', 409)
            remaining()
            return defaults
        state, code = 'running', None
        try:
            job_id = value['job_id']
            current = self.indexing.job(job_id)
            if current.get('indexing_active'):
                state = 'indexing_activated'
            elif current['state'] in ('confirmation_required', 'attention_required', 'failed'):
                # Resume controls the workflow only. It never replays a domain POST.
                state, code = 'attention_required', current.get('error_code') or 'INDEXING_DOMAIN_REVIEW_REQUIRED'
            elif current['state'] == 'activation_confirmation_required':
                current = self.indexing.observe(job_id, api())
                state = 'indexing_activated' if current.get('indexing_active') else 'attention_required'
                code = current.get('error_code')
            elif current.get('index_version_id') and current['state'] != 'index_ready':
                current = self.indexing.observe(job_id, api())
                if current['state'] in ('attention_required', 'failed'):
                    state, code = 'attention_required', current.get('error_code')
            elif current['state'] == 'index_ready':
                authorized()
                current = self.indexing.activate(job_id, api(True))
                state = 'indexing_activated' if current.get('indexing_active') else 'attention_required'
                code = current.get('error_code')
            else:
                defaults = authorized()
                binding = self.indexing.binding()
                if not binding or not binding['index_id']:
                    configured = self.indexing.configure(api(True), defaults)
                    if not configured['index_id']:
                        state, code = 'attention_required', configured.get('error_code') or 'INDEX_CREATE_OUTCOME_UNKNOWN'
                else:
                    current = self.indexing.dispatch(job_id, api(True))
                    if current['state'] in ('confirmation_required', 'attention_required', 'failed'):
                        state, code = 'attention_required', current.get('error_code')
        except UploadError as exc:
            state = ('running' if exc.code in ('LIBRARY_INDEXING_BUSY', 'INDEXING_BUSY') else
                     'blocked' if exc.code in ('GPU_VERIFICATION_DEFERRED', 'INDEXING_DEFAULTS_NOT_READY') else
                     'expired' if exc.code == 'INDEXING_FLOW_BUDGET_EXCEEDED' else 'attention_required')
            code = exc.code
        except readiness.ReadinessError as exc:
            state, code = 'blocked', exc.code
        except ContractError:
            state, code = 'attention_required', 'INDEXING_CONTRACT_UNVERIFIED'
        self.finish(row['handoff_id'], state, code)
        return self.result(self.current(value['job_id']), value['execution_id'])

    def finish(self,handoff_id,state,code):
        with self.uploads.connect(write=True) as db:
            db.execute('UPDATE indexing_flow_runs SET flow_state=?,error_code=?,updated_at=? WHERE handoff_id=?',(state,code,now(),handoff_id))

    def result(self,row,execution_id):
        projected = self.projected(row)
        return {'schema':1,'handoff_id':row['handoff_id'],'job_id':row['job_id'],'generation':row['generation'],
                'execution_id':execution_id,'state':projected['state'],'steps':row['steps'],
                'continue_wait':projected['state']=='running','error_code':row['error_code'],
                'indexing':{'indexing_ready':False,'indexing_active':False,**self.indexing.job(row['job_id'])},
                'question_ready':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('init','status','prepare','dispatch','retry','resume','export'))
    parser.add_argument('--state-dir',type=Path,default=Path.home()/'.local/state/agent-project-document-qa/uploads')
    parser.add_argument('--qa-origin')
    parser.add_argument('--engine-key-file',type=Path,default=Path(__file__).resolve().parents[2]/'infra/n8n/.secrets/agent-integration-api-key.json')
    parser.add_argument('--job-id')
    parser.add_argument('--paused',action='store_true',help='Freeze a GPU-free connection check into this workflow generation')
    args=parser.parse_args(argv)
    try:
        uploads=UploadStore(args.state_dir)
        flow=IndexingFlow.initialize(uploads) if args.action=='init' else IndexingFlow(uploads)
        if args.action in ('init','status'):
            result={'result':'passed',**flow.status(),'production_api_requests':0,'inference_requested':False}
        elif args.action=='prepare':
            check(args.qa_origin,'QA_ORIGIN_REQUIRED')
            result={'result':'prepared',**flow.prepare(args.qa_origin,args.engine_key_file),'inference_requested':False}
        elif args.action=='export':
            result=flow.desired(flow.setup())[0];result['active']=False
        else:
            result=flow.dispatch(args.job_id,retry=args.action=='retry',resume=args.action=='resume',paused=args.paused)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return 2 if result.get('delivery_state')=='confirmation_required' else 0
    except (UploadError,readiness.ReadinessError) as exc:
        print(json.dumps({'result':'attention_required','code':exc.code,'question_ready':False}));return 2
    except (OSError,sqlite3.Error,UnicodeError,ContractError):
        print(json.dumps({'result':'attention_required','code':'LOCAL_INDEXING_FLOW_STATE_UNCERTAIN','question_ready':False}));return 2
    except (KeyError,ValueError,TypeError,AttributeError):
        print(json.dumps({'result':'attention_required','code':'INDEXING_FLOW_RESPONSE_INVALID','question_ready':False}));return 2


if __name__=='__main__':
    raise SystemExit(main())
