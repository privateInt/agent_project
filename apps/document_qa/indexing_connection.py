"""Q05b explicit API check, owned n8n registration and immutable paused callback acceptance."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError, URLError
from http.client import HTTPException

from . import readiness
from .contracts import ContractError, strict_json, uid
from .intake import UploadError, check
from .uploads import UploadStore
from .workflows import qa_endpoint, private_secret, key_client
from .indexing_flow import IndexingFlow


class PausedAPI:
    def __init__(self, uploads, origin):
        self.uploads, self.origin = uploads, qa_endpoint(origin)
        self.calls = 0
        self.deadline = time.monotonic()+20

    def get(self, route):
        check(route in ('/api/library','/api/indexing-policy','/api/indexing-workflows'), 'PAUSED_API_ROUTE_INVALID')
        check(self.calls < 3 and time.monotonic() < self.deadline, 'PAUSED_API_BUDGET_EXCEEDED', 504)
        token = private_secret(self.uploads.token_path)
        request = Request(self.origin+route,method='GET',headers={'Accept':'application/json','Authorization':'Bearer '+token})
        self.calls += 1
        try:
            with build_opener(ProxyHandler({}),readiness.NoRedirect()).open(request,timeout=min(5,self.deadline-time.monotonic())) as response:
                check(response.status==200 and response.headers.get_content_type()=='application/json', 'PAUSED_API_UNCONFIRMED',502)
                raw=response.read(65537)
                check(len(raw)<=65536 and time.monotonic()<self.deadline,'PAUSED_API_RESPONSE_INVALID',502)
                return strict_json(raw)
        except HTTPError as exc:
            code = 'QA_API_RELOAD_REQUIRED' if exc.code == 404 and route != '/api/library' else 'PAUSED_API_UNCONFIRMED'
            exc.close();raise UploadError(code,502) from None
        except (OSError,URLError,HTTPException,ContractError):
            raise UploadError('PAUSED_API_UNCONFIRMED',502) from None

    def verify(self):
        library=self.get('/api/library')
        check(library.get('library_id')==self.uploads.library()['library_id'] and library.get('question_ready') is False,
              'PAUSED_API_LIBRARY_MISMATCH',409)
        policy=self.get('/api/indexing-policy')
        check(policy.get('inference_enabled') is False and policy.get('policy_source')=='explicit_server_opt_in',
              'PAUSED_API_POLICY_MISMATCH',409)
        workflow=self.get('/api/indexing-workflows')
        check(workflow.get('indexing_flow_schema')==1 and workflow.get('n8n_indexing_flow_implemented') is True,
              'PAUSED_API_CODE_UNCONFIRMED',409)
        return {'qa_origin':self.origin,'authenticated_library_verified':True,'server_inference_disabled_verified':True,
                'qa_GET_requests':self.calls,'question_ready':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','prepare-paused','accept-paused'))
    parser.add_argument('--state-dir',type=Path,default=Path.home()/'.local/state/agent-project-document-qa/uploads')
    parser.add_argument('--qa-origin',required=True)
    parser.add_argument('--engine-key-file',type=Path,default=Path(__file__).resolve().parents[2]/'infra/n8n/.secrets/agent-integration-api-key.json')
    parser.add_argument('--job-id')
    args=parser.parse_args(argv)
    try:
        uploads=UploadStore(args.state_dir)
        flow=IndexingFlow(uploads)
        probe=PausedAPI(uploads,args.qa_origin)
        result=probe.verify()
        setup=flow.setup(False)
        check(not setup or setup['qa_origin']==probe.origin,'INDEXING_FLOW_TARGET_CHANGED',409)
        result.update(schema=1,stage='Q05b-paused-connection',action=args.action,result='passed',
                      inference_requested=False,GPU_requested=False,Data_POST_requests=0,Knowledge_POST_requests=0)
        if args.action=='prepare-paused':
            api=key_client(args.engine_key_file)
            flow.prepare(probe.origin,args.engine_key_file,api)
            result.update(workflow_registered=True,published_definition_readback_verified=True,
                          n8n_GET_requests=sum(c['method']=='GET' for c in api.observations),
                          n8n_POST_requests=sum(c['method']=='POST' for c in api.observations),
                          n8n_callback_execution_verified=False)
        elif args.action=='accept-paused':
            check(uid(args.job_id),'JOB_ID_INVALID')
            # This acceptance is the first paused handoff, with no existing build to observe.
            check(flow.indexing.binding() is None and flow.indexing.head()['generation']==0
                  and flow.indexing.row(args.job_id) is None,'PAUSED_ACCEPTANCE_SCOPE_CHANGED',409)
            previous=flow.current(args.job_id)
            check(not previous or json.loads(previous['body'])['inference_mode']=='paused_check',
                  'PAUSED_ACCEPTANCE_SCOPE_CHANGED',409)
            api=key_client(args.engine_key_file)
            flow.dispatch(args.job_id,api,paused=True)
            deadline=time.monotonic()+30
            job=flow.job(args.job_id)
            while job['state'] in ('awaiting_execution','running') and time.monotonic()<deadline:
                time.sleep(0.5)
                job=flow.job(args.job_id)
            row=flow.current(args.job_id)
            check(job['delivery_state']=='accepted' and job['state']=='blocked'
                  and job['error_code']=='GPU_VERIFICATION_DEFERRED' and row['steps']>=1,
                  'PAUSED_CALLBACK_UNCONFIRMED',409)
            receipt=json.loads(row['receipt'])
            check(receipt['inference_mode']=='paused_check' and receipt['may_enable_inference'] is False
                  and flow.indexing.binding() is None and flow.indexing.row(args.job_id) is None
                  and flow.indexing.head()['generation']==0,'PAUSED_ACCEPTANCE_SCOPE_CHANGED',409)
            result.update(job_id=args.job_id,workflow=job,n8n_callback_execution_verified=True,
                          saved_paused_receipt_verified=True,existing_handoff_reused=bool(previous),
                          n8n_GET_requests=sum(c['method']=='GET' for c in api.observations),
                          n8n_POST_requests=sum(c['method']=='POST' for c in api.observations),
                          production_indexing_verified=False,next_step='record_paused_callback_then_Q05c_when_GPU_available')
        print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0
    except (UploadError,readiness.ReadinessError) as exc:
        code=exc.code
    except (OSError,sqlite3.Error,UnicodeError,ContractError,ValueError,TypeError,KeyError):
        code='PAUSED_CONNECTION_UNCONFIRMED'
    print(json.dumps({'schema':1,'stage':'Q05b-paused-connection','action':args.action,
                      'result':'attention_required','code':code,'question_ready':False}));return 2


if __name__=='__main__':
    raise SystemExit(main())
