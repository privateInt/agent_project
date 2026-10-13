import copy
import json
import unittest
import uuid
from unittest.mock import patch
from agent_runtime import agents


def uid(): return str(uuid.uuid4())

class Context:
    def is_active(self): return True
    def time_remaining(self): return 60

class AgentGraphTest(unittest.TestCase):
    def setUp(self):
        self.dep, self.mv, self.mid, self.cv, self.tv, self.call_id = [uid() for _ in range(6)]
        self.version = {'template_id':'knowledge_qa_v1','template_version':1,'tool_version_id':self.tv,
            'model':{'deployment_id':self.dep,'model_version_id':self.mv,'connection_version_id':self.cv},
            'prompt':'한국어로 답하세요.','context_max_chars':8000,'max_tokens':512,'temperature':0.2}
        self.snapshot={'version':self.version,'tool':{'version':{'tool_version_id':self.tv}},'model_connection':{'connection_version_id':self.cv}}
        self.hit={'chunk_id':'b'*64,'text':'생산 과정에서 물과 에너지를 사용했다.','source_document_id':uid(),'canonical_id':uid(),
                  'source_spans':[{'metadata':{'location':{'page':3}}}],'quality':{'needs_review':True},'future_field':'preserved'}
        self.call={'tool_call_id':self.call_id,'tool_version_id':self.tv,'state':'succeeded','result':{'completion':'completed','data':{'results':[self.hit]},'metadata':{'index_version_id':uid()}}}
        self.answer='물과 에너지를 사용했습니다. [S1]';self.finish='stop';self.events=[];self.http=[]
    def command(self,op,step,detail):
        self.events.append((op,step,detail));return self.call if op=='search' else {'ok':True}
    def api(self,connection,path,context,body=None,**kwargs):
        self.http.append((path,body))
        if path=='/api/deployments/'+self.dep:
            return {'deployment_id':self.dep,'model_version_id':self.mv,'status':'ready','runtime_state':'ready','desired_state':'running',
                    'serving':{'engine':'vllm','profile':'','max_model_len':4096},'served_model_name':'deployment-'+self.dep,'runtime_container_id':'fixture'}
        if path=='/api/model-versions/'+self.mv:return {'model_version_id':self.mv,'model_id':self.mid,'version':'v1'}
        if path=='/api/models/'+self.mid:return {'model_id':self.mid,'model_type':'llm','name':'Fixture Chat'}
        if path.endswith('/chat'):
            self.assertNotIn('tools',body)
            self.assertEqual([m['role'] for m in body['messages']],['system','user'])
            data=json.loads(body['messages'][1]['content']);self.assertEqual(data['question'],'  질문 원문  ')
            return {'deployment_id':self.dep,'model':'deployment-'+self.dep,'content':self.answer,'finish_reason':self.finish,
                    'usage':{'prompt_tokens':40,'completion_tokens':20,'total_tokens':60}}
        raise AssertionError(path)
    def run_graph(self):
        with patch.object(agents,'api',self.api):return agents.execute(self.snapshot,{'question':'  질문 원문  '},self.command,Context())
    def test_answer_sources_steps_and_single_model_call(self):
        result=self.run_graph();self.assertEqual(result['outcome'],'answered');self.assertEqual(result['citations'][0]['source'],self.hit)
        self.assertEqual(result['tool_call_id'],self.call_id);self.assertEqual(len([p for p,_ in self.http if p.endswith('/chat')]),1)
        self.assertEqual([step for op,step,_ in self.events if op=='start'],['validate','search','context','generate','finalize'])
    def test_empty_search_skips_all_model_lookups_and_generation(self):
        self.call['result']['data']['results']=[];result=self.run_graph()
        self.assertEqual(result['outcome'],'no_evidence');self.assertEqual(result['citations'],[]);self.assertEqual(self.http,[])
        self.assertIn(('skip','generate',{'reason':'no_search_results'}),self.events)
    def test_model_can_report_insufficient_evidence_without_fake_citations(self):
        self.answer='INSUFFICIENT_EVIDENCE';r=self.run_graph();self.assertEqual(r['outcome'],'no_evidence');self.assertEqual(r['citations'],[])
    def test_unknown_and_missing_citations_fail_without_retry(self):
        for answer in ['unknown [S2]','missing source','bad [S0]','bad [S01]']:
            with self.subTest(answer=answer):
                self.http=[];self.answer=answer
                with self.assertRaises(agents.AgentError) as c:self.run_graph()
                self.assertEqual(c.exception.code,'ANSWER_REFERENCE_INVALID')
                self.assertEqual(sum(p.endswith('/chat') for p,_ in self.http),1)
    def test_truncated_answer_is_marked(self):
        self.finish='length';self.assertTrue(self.run_graph()['answer_truncated'])

    def test_reference_diagnostics_survive_finalize_failure_without_answer_text(self):
        cases = [
            ('private-answer [1]', 'MISSING_REFERENCE'),
            ('private-answer [S01]', 'MALFORMED_REFERENCE'),
            ('private-answer [S2]', 'REFERENCE_OUT_OF_RANGE'),
            ('private-answer [S' + '9'*5000 + ']', 'REFERENCE_OUT_OF_RANGE'),
        ]
        for answer, issue in cases:
            with self.subTest(issue=issue, length=len(answer)):
                self.answer=answer;self.events=[];self.http=[];self.finish='length'
                with self.assertRaises(agents.AgentError) as caught:self.run_graph()
                self.assertEqual((caught.exception.code,caught.exception.stage),('ANSWER_REFERENCE_INVALID','finalize'))
                detail=next(d for op,s,d in self.events if op=='finish' and s=='generate')
                report=detail['reference_diagnostics']
                self.assertEqual(report['issues'],[issue]);self.assertEqual(report['status'],'invalid')
                self.assertEqual(report['selected_source_count'],1);self.assertEqual(report['finish_reason'],'length')
                self.assertEqual(report['completion_tokens'],20)
                encoded=json.dumps(report)
                self.assertLess(len(encoded),1024)
                for private in ('private-answer',self.hit['text'],self.hit['chunk_id']):self.assertNotIn(private,encoded)
                self.assertEqual(sum(p.endswith('/chat') for p,_ in self.http),1)
                self.assertFalse(any(op=='finish' and s=='finalize' for op,s,_ in self.events))

    def test_reference_diagnostic_grammar_matches_finalization(self):
        for answer, status in [('valid [S1] [S1]','valid'), ('mixed [S1] [S3] [Sx]','invalid'),
                               ('[s1]','invalid'), ('[S１]','invalid'), ('[S0]','invalid'),
                               ('partial [S1','invalid'), ('INSUFFICIENT_EVIDENCE','no_evidence_signal')]:
            with self.subTest(answer=answer):
                self.answer=answer;self.events=[]
                if status=='invalid':
                    with self.assertRaises(agents.AgentError):self.run_graph()
                else:
                    result=self.run_graph()
                    if status=='valid':self.assertEqual(result['citations'],[{'citation_id':'S1','source':self.hit}])
                    else:self.assertEqual(result['outcome'],'no_evidence')
                report=next(d['reference_diagnostics'] for op,s,d in self.events if op=='finish' and s=='generate')
                self.assertEqual(report['status'],status)

    def test_reference_diagnostic_cannot_promote_bad_response_or_usage(self):
        for content in (None, [], '', 'x'*65537, '\ud800', 'x\0'):
            report=agents.reference_diagnostics({'content':content,'finish_reason':'stop'},1,512)
            self.assertEqual(report['status'],'not_checked')
        def wrong_usage(connection,path,context,**kwargs):
            value=self.api(connection,path,context,**kwargs)
            if path.endswith('/chat'):value['usage']['total_tokens']=999
            return value
        with patch.object(agents,'api',wrong_usage), self.assertRaises(agents.AgentError) as caught:
            agents.execute(self.snapshot,{'question':'  질문 원문  '},self.command,Context())
        self.assertEqual(caught.exception.code,'MODEL_RESPONSE_INVALID')
        self.assertEqual(next(d['reference_diagnostics']['status'] for op,s,d in self.events if op=='finish' and s=='generate'),'valid')

    def test_grpc_persists_diagnostic_before_error_and_retries_only_callback(self):
        from concurrent.futures import ThreadPoolExecutor
        import os
        import grpc
        from agent.platform.v1 import platform_pb2 as pb, platform_pb2_grpc as rpc
        from agent_runtime.server import make_server
        token='d'*64;rid=uid();run_id=uid();av=uid();lease=uid();received=[]
        owner=self
        class Service(rpc.AgentServiceServicer):
            def AgentRuntimeCommand(self, request, context):
                metadata=dict(context.invocation_metadata())
                owner.assertEqual(metadata['authorization'],'Bearer '+token)
                owner.assertEqual(metadata['x-request-id'],rid)
                owner.assertEqual((request.agent_run_id,request.lease_token),(run_id,lease))
                if request.operation=='finish' and request.step_id=='generate':
                    received.append(request.json)
                    if len(received)==1:
                        context.abort(grpc.StatusCode.UNAVAILABLE,'lost callback response')
                return pb.ToolResponse(json=json.dumps(owner.command(request.operation,request.step_id,json.loads(request.json))).encode())
        service=grpc.server(ThreadPoolExecutor(max_workers=2))
        rpc.add_AgentServiceServicer_to_server(Service(),service)
        port=service.add_insecure_port('127.0.0.1:0');service.start()
        runtime,_,runtime_port=make_server(token,'127.0.0.1:0');runtime.start()
        self.answer='private generated answer without references'
        self.snapshot['version']['agent_version_id']=av
        try:
            with grpc.insecure_channel('127.0.0.1:'+str(runtime_port)) as channel, \
                    patch.dict(os.environ,{'AGENT_SERVICE_ADDRESS':'127.0.0.1:'+str(port)}), \
                    patch.object(agents,'api',self.api):
                result=rpc.RuntimeServiceStub(channel).ExecuteAgent(pb.AgentExecution(
                    agent_run_id=run_id,agent_version_id=av,lease_token=lease,
                    snapshot_json=json.dumps(self.snapshot).encode(),
                    input_json=json.dumps({'question':'  질문 원문  '}).encode()),
                    metadata=[('authorization','Bearer '+token),('x-request-id',rid)],timeout=10)
            self.assertEqual((result.error_code,result.error_stage),('ANSWER_REFERENCE_INVALID','finalize'))
            self.assertFalse(result.result_json)
            self.assertEqual(len(received),2);self.assertEqual(received[0],received[1])
            self.assertNotIn(self.answer.encode(),received[0])
            self.assertEqual(json.loads(received[0])['reference_diagnostics']['issues'],['MISSING_REFERENCE'])
            self.assertEqual(sum(p.endswith('/chat') for p,_ in self.http),1)
        finally:
            runtime.stop(0).wait();service.stop(0).wait()
    def test_full_chunk_budget_and_original_order(self):
        large=copy.deepcopy(self.hit);large.update(chunk_id='c'*64,text='x'*8001)
        small=copy.deepcopy(self.hit);small.update(chunk_id='d'*64,text='data')
        selected,excluded,body=agents.select_context(self.version,'q',[self.hit,large,small])
        self.assertEqual(selected,[self.hit,small]);self.assertEqual(excluded,[{'chunk_id':'c'*64,'reason':'context_budget'}]);self.assertTrue(agents.payload_fits(body))
        with self.assertRaises(agents.AgentError) as e:agents.select_context(self.version,'q',[large])
        self.assertEqual(e.exception.code,'CONTEXT_TOO_LARGE')
    def test_prompt_and_byte_budget_are_bounded(self):
        with self.assertRaises(agents.AgentError) as e:agents.select_context(self.version,'😀'*17000,[])
        self.assertEqual(e.exception.code,'INPUT_TOO_LARGE')
    def test_model_version_mismatch_prevents_chat(self):
        self.version['model']['model_version_id']=uid()
        with self.assertRaises(agents.AgentError) as e:self.run_graph()
        self.assertEqual(e.exception.code,'MODEL_IDENTITY_MISMATCH');self.assertEqual(len(self.http),1)
    def test_search_failure_does_not_generate(self):
        self.call['state']='failed'
        with self.assertRaises(agents.AgentError) as e:self.run_graph()
        self.assertEqual(e.exception.code,'AGENT_SEARCH_FAILED');self.assertEqual(self.http,[])
    def test_uncertain_tool_effect_interrupts_graph(self):
        for state in ['unknown','external_pending','interrupted']:
            self.call['state']=state
            with self.assertRaises(agents.AgentError) as e:self.run_graph()
            self.assertEqual(e.exception.code,'AGENT_EXECUTION_INTERRUPTED')
    def test_fenced_step_never_starts_external_call(self):
        def fenced(*args):raise agents.AgentError('AGENT_EXECUTION_INTERRUPTED')
        with patch.object(agents,'api',self.api),self.assertRaises(agents.AgentError):agents.execute(self.snapshot,{'question':'q'},fenced,Context())
        self.assertEqual(self.http,[])
    def test_legacy_non_chat_repositories_cannot_resolve_or_generate(self):
        for repo in ('dragonkue/snowflake-arctic-embed-l-v2.0-ko', 'Dongjin-kr/ko-reranker'):
            with self.subTest(repo=repo):
                def api(connection, path, context, **kwargs):
                    value = self.api(connection, path, context, **kwargs)
                    if path == '/api/model-versions/' + self.mv: value['repo_id'] = repo
                    return value
                self.http = []
                with patch.object(agents, 'api', api), self.assertRaises(agents.AgentError) as caught:
                    agents.model_identity(self.snapshot['model_connection'], self.dep, Context())
                self.assertEqual(caught.exception.code, 'MODEL_NOT_READY')
                self.assertFalse(any(path.endswith('/chat') for path, _ in self.http))

    def test_legacy_embedding_deployment_is_not_listed_as_answer_model(self):
        def api(connection, path, context, **kwargs):
            if path == '/api/deployments':
                return {'items': [{'deployment_id': self.dep, 'serving': {'engine': 'vllm'}}], 'next_cursor': ''}
            value = self.api(connection, path, context, **kwargs)
            if path == '/api/model-versions/' + self.mv: value['repo_id'] = 'dragonkue/snowflake-arctic-embed-l-v2.0-ko'
            return value
        with patch.object(agents, 'api', api):
            result = agents.inspect({'action': 'deployments', 'connection_version_id': self.cv}, self.snapshot['model_connection'], Context())
        self.assertEqual(result['items'], [])
        self.assertFalse(any(path.endswith('/chat') for path, _ in self.http))

    def test_display_name_does_not_define_chat_capability(self):
        def api(connection, path, context, **kwargs):
            value = self.api(connection, path, context, **kwargs)
            if path == '/api/model-versions/' + self.mv: value['repo_id'] = 'fixture/chat-model'
            if path == '/api/models/' + self.mid: value['name'] = '한국어 embedding'
            return value
        with patch.object(agents, 'api', api):
            info = agents.model_identity(self.snapshot['model_connection'], self.dep, Context())
        self.assertEqual(info['repo_id'], 'fixture/chat-model')

    def test_model_http_input_rejection_mapping(self):
        with patch.object(agents.http_tools,'execute',side_effect=agents.ToolError('TOOL_HTTP_ERROR',{'status':400})):
            with self.assertRaises(agents.AgentError) as e:agents.api({'connection_version_id':self.cv},'/owned',Context(),body={})
        self.assertEqual(e.exception.code,'MODEL_INPUT_REJECTED')

if __name__=='__main__':unittest.main()
