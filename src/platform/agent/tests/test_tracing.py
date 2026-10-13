import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import tracing as trace

def uid(): return str(uuid.uuid4())


class TrialAPI:
    url='http://127.0.0.1:16100'
    def __init__(self): self.calls=[];self.effects={};self.lost=True
    def request(self,method,route,body,expected,key):
        self.calls.append((key,copy.deepcopy(body)))
        if key not in self.effects: self.effects[key]={'agent_run_id':uid()}
        if self.lost: self.lost=False;raise ValueError('lost response')
        return self.effects[key]
    def get(self,route): return {'agent_run_id':next(iter(self.effects.values()))['agent_run_id'],'state':'succeeded'}


class TracingOpsTests(unittest.TestCase):
    def test_lost_response_and_replay_preserve_request(self):
        version={'agent_version_id':uid(),'state':'published','display_name':'test'}
        with tempfile.TemporaryDirectory() as folder, patch.object(trace,'versions',return_value=[version]):
            path=Path(folder);api=TrialAPI()
            with self.assertRaises(ValueError): trace.start_trial(api,path,'private question',version['agent_version_id'])
            original=(path/'preparation.json').read_bytes()
            ident=trace.start_trial(api,path,'private question',version['agent_version_id'])
            self.assertEqual(len(api.effects),1);self.assertEqual(api.calls[0],api.calls[1])
            self.assertEqual(trace.start_trial(api,path,replay=True),ident)
            self.assertEqual(len(api.effects),1);self.assertEqual(api.calls[0],api.calls[-1])
            with self.assertRaises(trace.TraceCheckFailure): trace.start_trial(api,path,'changed',version['agent_version_id'])
            self.assertEqual(len(api.calls),3)

    def fixture(self):
        run={'agent_run_id':uid(),'agent_version_id':uid(),'request_id':uid(),'state':'succeeded',
             'started_at':'2026-09-26T12:00:00Z','completed_at':'2026-09-26T12:00:03Z',
             'result':{'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}}}
        steps=[{'step_id':s,'state':'succeeded'} for s in trace.obs.STEPS]
        call={'tool_call_id':uid(),'tool_version_id':uid(),'state':'succeeded','request_id':run['request_id'],
              'parent_agent_run_id':run['agent_run_id'],'parent_step_id':'search'}
        run['tool_call_id']=call['tool_call_id']
        traceid,expected=trace.expected_rows(run,steps,call)
        project='project';items=[]
        for name,(ident,parent,state,request) in expected.items():
            meta={'agent_run_id':run['agent_run_id'],'agent_version_id':run['agent_version_id'],
                  'scope':'runtime_execution','state':state,'request_id':request,
                  'tool_call_id':call['tool_call_id'],'tool_version_id':call['tool_version_id']}
            if name=='model.chat':meta.update({k:str(v) for k,v in run['result']['usage'].items()})
            items.append({'name':name,'id':ident,'parentObservationId':parent,'traceId':traceid,'projectId':project,
                'environment':'agent-platform','metadata':meta,'input':None,'output':None,'level':'DEFAULT',
                'type':{'agent.execute':'AGENT','tool.execute':'TOOL','model.chat':'GENERATION',
                        'model.embedding':'EMBEDDING','knowledge.search':'RETRIEVER'}.get(name,'SPAN'),
                'startTime':run['started_at'],'endTime':run['completed_at']})
        class API:
            def get(self,route): return {'items':steps} if route.endswith('/steps') else call if '/tool-calls/' in route else run
        class LF:
            def request(self,method,path):
                assert method=='GET' and 'traceId='+traceid in path
                return {'data':items,'meta':{}}
        return run,steps,call,items,API(),LF(),{'project_id':project}

    def test_read_only_check_validates_parent_state_ids_privacy_and_usage(self):
        run,steps,call,items,api,lf,config=self.fixture()
        result=trace.inspect(api,lf,config,run['agent_run_id'])
        self.assertEqual((result['result'],result['observations_verified']),('passed',10))
        for row,key,value in [(items[0],'id','changed'),(items[1],'parentObservationId','changed'),
                              (items[2],'input','private text'),(items[0],'type','SPAN'),(items[0],'traceId','changed')]:
            old=row[key];row[key]=value
            with self.assertRaises(trace.TraceCheckFailure):trace.inspect(api,lf,config,run['agent_run_id'])
            row[key]=old
        chat=next(x for x in items if x['name']=='model.chat')
        chat['metadata']['total_tokens']='99'
        with self.assertRaises(trace.TraceCheckFailure):trace.inspect(api,lf,config,run['agent_run_id'])

    def test_missing_does_not_pass_and_wrong_tool_link_rejected(self):
        run,steps,call,items,api,lf,config=self.fixture();items.pop()
        result=trace.inspect(api,lf,config,run['agent_run_id'])
        self.assertEqual(result['result'],'pending');self.assertFalse(result['inference_executed_by_check'])
        call['parent_agent_run_id']=uid()
        with self.assertRaises(trace.TraceCheckFailure):trace.inspect(api,lf,config,run['agent_run_id'])

    def test_prepare_preserves_existing_config_and_copies_only_project_keys(self):
        config={'project_id':'project','public_key':'public-key','secret_key':'secret-key','db_password':'never-copy','password':'never-copy'}
        class LF:
            def __init__(self,_):pass
            def request(self,method,path):
                assert method=='GET';return {'data':[{'id':'project'}]}
        with tempfile.TemporaryDirectory() as folder,patch.object(trace.manage,'load',return_value=config),patch.object(trace,'Client',LF),patch.object(trace,'ROOT',Path(folder)):
            (Path(folder)/'.secrets').mkdir(mode=0o700)
            result=trace.prepare(Path(folder));self.assertFalse(result['reused'])
            path=Path(folder)/'.secrets/langfuse-tracing.json';raw=path.read_bytes()
            self.assertNotIn(b'never-copy',raw);self.assertEqual(path.stat().st_mode & 0o777,0o444)
            self.assertTrue(trace.prepare(Path(folder))['reused']);self.assertEqual(path.read_bytes(),raw)
            config['secret_key']='changed-secret'
            with self.assertRaises(trace.TraceCheckFailure):trace.prepare(Path(folder))
            self.assertEqual(path.read_bytes(),raw)


if __name__=='__main__':unittest.main()
