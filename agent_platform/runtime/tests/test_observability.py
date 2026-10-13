import json
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from agent_runtime import observability as obs, agents, tools
import test_agents as graph_fixture
import test_knowledge_tools as knowledge_fixture


def uid(): return str(uuid.uuid4())
def metadata(span):
    return {a['key'].removeprefix('langfuse.observation.metadata.'): a['value']['stringValue']
            for a in span['attributes'] if a['key'].startswith('langfuse.observation.metadata.')}


class Capture:
    def __init__(self): self.items = []
    def submit(self, item): self.items.append(item)


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.capture = Capture()
        self.patcher = patch.object(obs, '_exporter', self.capture)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.ident = uid()

    def test_graph_success_and_privacy(self):
        f = graph_fixture.AgentGraphTest(); f.setUp()
        with obs.execution('agent', self.ident, agent_run_id=self.ident, request_id=uid()): result = f.run_graph()
        rows = {x['name']: x for x in self.capture.items}
        self.assertEqual(set(rows), {'agent.execute', 'model.chat'} | {'step.' + x for x in obs.STEPS})
        trace = obs.trace_id('agent', self.ident)
        self.assertTrue(all(x['traceId'] == trace for x in rows.values()))
        self.assertEqual(rows['model.chat']['parentSpanId'], rows['step.generate']['spanId'])
        self.assertEqual(rows['step.search']['parentSpanId'], rows['agent.execute']['spanId'])
        self.assertEqual(metadata(rows['step.search'])['tool_call_id'], f.call_id)
        self.assertEqual(metadata(rows['model.chat'])['total_tokens'], '60')
        raw = json.dumps(self.capture.items, ensure_ascii=False)
        for private in (f.answer, f.hit['text'], f.hit['chunk_id'], '질문 원문', f.version['prompt']): self.assertNotIn(private, raw)
        self.assertEqual(result['outcome'], 'answered')
        for row in rows.values(): self.assertGreaterEqual(int(row['endTimeUnixNano']), int(row['startTimeUnixNano']))

    def test_no_evidence_and_failure_have_all_steps_without_extra_model_call(self):
        f = graph_fixture.AgentGraphTest(); f.setUp(); f.call['result']['data']['results'] = []
        with obs.execution('agent', self.ident): result = f.run_graph()
        rows = {x['name']: x for x in self.capture.items}
        self.assertEqual(metadata(rows['step.generate'])['state'], 'skipped')
        self.assertNotIn('model.chat', rows); self.assertEqual(f.http, [])
        self.assertEqual(result['outcome'], 'no_evidence')
        self.capture.items.clear(); f.setUp(); f.answer = 'private invalid answer'
        with self.assertRaises(agents.AgentError), obs.execution('agent', self.ident): f.run_graph()
        rows = {x['name']: x for x in self.capture.items}
        self.assertEqual(metadata(rows['step.finalize'])['error_code'], 'ANSWER_REFERENCE_INVALID')
        self.assertEqual(metadata(rows['agent.execute'])['state'], 'failed')
        self.assertEqual(sum(p.endswith('/chat') for p, _ in f.http), 1)

    def test_interrupted_steps_and_export_failure_do_not_change_business(self):
        f = graph_fixture.AgentGraphTest(); f.setUp()
        def fenced(*_): raise agents.AgentError('AGENT_EXECUTION_INTERRUPTED')
        with patch.object(agents, 'api', f.api), self.assertRaises(agents.AgentError), obs.execution('agent', self.ident):
            agents.execute(f.snapshot, {'question': 'secret'}, fenced, graph_fixture.Context())
        rows = {x['name']: x for x in self.capture.items}
        self.assertEqual(metadata(rows['step.validate'])['state'], 'interrupted')
        for step in obs.STEPS[1:]: self.assertEqual(metadata(rows['step.'+step])['state'], 'skipped')
        with patch.object(self.capture, 'submit', side_effect=RuntimeError('private-export-secret')):
            with obs.execution('agent', uid()): result = f.run_graph()
        self.assertEqual(result['outcome'], 'answered'); self.assertIsNone(obs._current.get())

    def test_real_http_embedding_search_and_cross_thread_parent(self):
        f = knowledge_fixture.KnowledgeTests()
        f.setUpClass(); f.setUp()
        self.addCleanup(f.tearDownClass); self.addCleanup(f.doCleanups)
        call = uid(); request = uid()
        def execute():
            with obs.execution('tool', call, parent_run=self.ident, tool_call_id=call, request_id=request): f.run_tool()
        thread = threading.Thread(target=execute); thread.start(); thread.join(10)
        self.assertFalse(thread.is_alive())
        rows = {x['name']: x for x in self.capture.items}
        self.assertEqual(set(rows), {'tool.execute', 'model.embedding', 'knowledge.search'})
        self.assertEqual(rows['tool.execute']['parentSpanId'], obs.span_id(obs.trace_id('agent', self.ident), 'step.search'))
        for name in ('model.embedding','knowledge.search'): self.assertEqual(rows[name]['parentSpanId'], rows['tool.execute']['spanId'])
        self.assertEqual(metadata(rows['model.embedding'])['input_tokens'], '6')
        self.assertEqual(metadata(rows['knowledge.search'])['returned_count'], '1')
        raw = json.dumps(self.capture.items, ensure_ascii=False)
        self.assertNotIn('질문', raw); self.assertNotIn(f.f['hits'][0]['text'], raw); self.assertNotIn('query_embedding', raw)
        self.assertEqual(sum(p.endswith('/embeddings') for _, p, _ in f.f['calls']), 1)

    def test_stable_ids_distinct_runs_and_no_fake_unknown_usage(self):
        for _ in range(2):
            with obs.execution('agent', self.ident):
                with obs.span('model.chat', kind='generation', prompt_tokens=1, completion_tokens=2, total_tokens=99,
                              question='secret', secret_key='private'): pass
        self.assertEqual(self.capture.items[0]['spanId'], self.capture.items[7]['spanId'])
        self.assertNotEqual(obs.trace_id('agent', self.ident), obs.trace_id('agent', uid()))
        self.assertFalse(any(a['key'] == 'langfuse.observation.usage_details' for a in self.capture.items[0]['attributes']))
        self.assertNotIn('secret', json.dumps(self.capture.items))

    def test_disabled_and_bad_config_do_not_start_export_or_break_runtime(self):
        with patch.object(obs, '_exporter', None):
            with obs.execution('agent', self.ident): f = graph_fixture.AgentGraphTest(); f.setUp(); f.run_graph()
        self.assertFalse(self.capture.items)
        with patch.dict('os.environ', {'AGENT_TRACE_CONFIG_FILE': '/missing-private-file'}), patch.object(obs, '_exporter', None):
            obs.initialize(); self.assertIsNone(obs._exporter)

    def test_grpc_failed_result_and_callback_retry_keep_one_generation(self):
        f=graph_fixture.AgentGraphTest();f.setUp()
        f.test_grpc_persists_diagnostic_before_error_and_retries_only_callback()
        rows={x['name']:x for x in self.capture.items}
        self.assertEqual(len(rows),len(self.capture.items))
        self.assertEqual(metadata(rows['agent.execute'])['error_code'],'ANSWER_REFERENCE_INVALID')
        self.assertEqual(metadata(rows['step.finalize'])['state'],'failed')
        self.assertEqual(metadata(rows['model.chat'])['state'],'succeeded')
        self.assertEqual(len(rows),7)

    def test_tool_rpc_authentication_and_persisted_parent_metadata(self):
        import grpc
        from agent.platform.v1 import platform_pb2 as pb, platform_pb2_grpc as rpc
        from agent_runtime.server import make_server
        runtime,_,port=make_server('z'*64,'127.0.0.1:0');runtime.start()
        call_id=uid()
        try:
            with grpc.insecure_channel('127.0.0.1:'+str(port)) as channel:
                stub=rpc.RuntimeServiceStub(channel)
                request=pb.ToolExecution(tool_call_id=call_id,tool_version_id=uid(),lease_token=uid(),
                    kind='diagnostic_echo_v1',binding_json=b'{}',arguments_json=b'{"text":"PRIVATE-INPUT"}')
                with self.assertRaises(grpc.RpcError): stub.ExecuteTool(request,timeout=3)
                self.assertFalse(self.capture.items)
                out=stub.ExecuteTool(request,metadata=[('authorization','Bearer '+'z'*64),('x-request-id',uid()),
                    ('x-agent-parent-run-id',self.ident),('x-agent-parent-step-id','search')],timeout=3)
                self.assertFalse(out.error_code)
            row=self.capture.items[0]
            self.assertEqual(row['traceId'],obs.trace_id('agent',self.ident))
            self.assertEqual(row['parentSpanId'],obs.span_id(row['traceId'],'step.search'))
            self.assertEqual(metadata(row)['tool_call_id'],call_id)
            self.assertNotIn('PRIVATE-INPUT',json.dumps(row))
        finally: runtime.stop(0).wait()


class ExportTests(unittest.TestCase):
    def test_bounded_queue_shutdown_and_no_business_wait(self):
        entered, release = threading.Event(), threading.Event()
        class Slow:
            def send(self, body): entered.set(); release.wait(3); return 'accepted'
        exporter = obs.Exporter(Slow(), capacity=2)
        try:
            exporter.submit({}); self.assertTrue(entered.wait(1))
            started = time.monotonic()
            for _ in range(100): exporter.submit({})
            self.assertLess(time.monotonic()-started, .2)
            self.assertGreater(exporter.snapshot()['dropped'], 0)
            started = time.monotonic(); exporter.close(.01)
            self.assertLess(time.monotonic()-started, .2)
        finally: release.set(); exporter.thread.join(2)

    def test_real_http_retry_preserves_bytes_and_partial_success_is_not_success(self):
        bodies, auth, statuses = [], [], [503, 200, 200]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                bodies.append(self.rfile.read(int(self.headers['Content-Length'])))
                auth.append((self.headers['Authorization'], self.headers['x-langfuse-ingestion-version'], self.path))
                self.send_response(statuses[len(bodies)-1]); self.end_headers()
                self.wfile.write(b'{}' if len(bodies)<3 else b'{"partialSuccess":{"rejectedSpans":"1"}}')
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        exporter = obs.Exporter(obs.Transport({'url':'http://127.0.0.1:'+str(server.server_port),'public_key':'public-key','secret_key':'secret-key'}))
        try:
            exporter.submit({'spanId':'1'})
            until=time.monotonic()+3
            while exporter.snapshot()['accepted']!=1 and time.monotonic()<until: time.sleep(.01)
            self.assertEqual(exporter.snapshot()['accepted'],1); self.assertEqual(bodies[0], bodies[1])
            exporter.submit({'spanId':'2'})
            until=time.monotonic()+3
            while exporter.snapshot()['rejected']!=1 and time.monotonic()<until: time.sleep(.01)
            self.assertEqual(exporter.snapshot()['rejected'],1)
            self.assertTrue(all(v[1:] == ('4','/api/public/otel/v1/traces') for v in auth))
        finally: exporter.close();server.shutdown();server.server_close();thread.join()


if __name__ == '__main__': unittest.main()
