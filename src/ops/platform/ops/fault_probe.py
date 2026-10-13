"""Isolated acceptance worker. Only synthetic loopback endpoints and temporary files.

Launched by fault_check.py in a NEW interpreter, never imported into a live server.
No runtime configuration is loaded. Production modules are hash-checked first.
"""
from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.parse
import uuid
from unittest.mock import patch


class ProbeFailure(Exception):
    pass


def check(value, code):
    if not value:
        raise ProbeFailure(code)


def until(predicate, code, seconds=15):
    deadline = time.monotonic() + seconds
    while not predicate():
        check(time.monotonic() < deadline, code)
        time.sleep(.01)


class Guard:
    """Fail closed on Python outbound sockets outside listeners owned by this probe."""
    def __init__(self):
        self.ports = set()
        self.blocked = 0

    def audit(self, event, args):
        if event in ('socket.connect', 'socket.bind'):
            address = args[1]
            valid = (isinstance(address, tuple) and len(address) == 2
                     and address[0] == '127.0.0.1'
                     and (address[1] in self.ports or (event == 'socket.bind' and address[1] == 0)))
            # urllib3 probes IPv6 support by binding then closing ::1:0 on import.
            valid = valid or (event == 'socket.bind' and address == ('::1', 0))
            if not valid:
                self.blocked += 1
                raise ProbeFailure('NON_FIXTURE_NETWORK_REFUSED')
        if event == 'open' and isinstance(args[0], (str, bytes)):
            path = os.fsdecode(args[0])
            if path.startswith(('/run/ops/', '/run/secrets/', '/var/lib/ops/')) or '/.secrets/' in path:
                self.blocked += 1
                raise ProbeFailure('PRODUCTION_STATE_ACCESS_REFUSED')
        if event in ('subprocess.Popen', 'os.system'):
            self.blocked += 1
            raise ProbeFailure('CHILD_PROCESS_REFUSED')


class HTTPFixture:
    def __init__(self, guard, response):
        self.guard, self.response = guard, response
        self.mode = 'ok'
        self.port = 0
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.start()

    @property
    def url(self):
        return 'http://127.0.0.1:' + str(self.port)

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.reply()

            def do_POST(self):
                self.reply()

            def reply(self):
                mode = owner.mode
                raw = self.rfile.read(min(int(self.headers.get('Content-Length', '0')), 262144))
                owner.calls.append((self.command, self.path, raw))
                owner.entered.set()
                if mode == 'timeout':
                    owner.release.wait(2.5)
                status = {'http_503': 503, 'http_401': 401}.get(mode, 200)
                data = owner.response(self.path) if status == 200 else {'private_error': 'FAULT_PRIVATE_SENTINEL'}
                encoded = b'{invalid FAULT_PRIVATE_SENTINEL' if mode == 'invalid_json' else json.dumps(data).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def server_bind(self):
                # HTTPServer.getfqdn is unnecessary for a numeric, private loopback fixture.
                socket.socket.setsockopt(self.socket, socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.socket.bind(self.server_address)
                self.server_address = self.socket.getsockname()
                self.server_name, self.server_port = self.server_address

        self.server = Server(('127.0.0.1', self.port), Handler)
        self.port = self.server.server_port
        self.guard.ports.add(self.port)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()

    def pause(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def close(self):
        self.release.set()
        self.pause()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@contextmanager
def fault(source, mode):
    source.entered.clear()
    source.release.clear()
    if mode == 'connection_refused':
        source.pause()
    else:
        source.mode = mode
    try:
        yield
    finally:
        source.mode = 'ok'
        source.release.set()
        if mode == 'connection_refused':
            source.start()


MODES = {'http_503': 'UPSTREAM_HTTP_ERROR', 'http_401': 'UNAUTHENTICATED',
         'invalid_json': 'INVALID_RESPONSE', 'timeout': 'TIMEOUT', 'connection_refused': 'CONNECTION_FAILED'}


def ops_probe(guard):
    from src.ops.platform.server import create_app
    from src.ops.platform.service import ReadService
    from src.ops.platform.observations import trace_id, span_id, QUERIES
    import uvicorn

    ident, version, request = (str(uuid.uuid4()) for _ in range(3))
    start, end = '2026-09-27T00:00:00Z', '2026-09-27T00:00:01Z'
    project = 'fault-project-synthetic'
    steps = ('validate', 'search', 'context', 'generate', 'finalize')
    run = {'agent_run_id': ident, 'agent_version_id': version, 'request_id': request,
           'state': 'failed', 'error': {'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize'}}
    routes = {'/health': {'status': 'ok', 'service': 'model-gateway'},
              '/healthz': {'platform': 'agent', 'status': 'ok'},
              '/api/status': {'platform': 'agent', 'ready': True, 'database': 'ready', 'runtime': 'ready', 'request_id': request},
              '/api/agent-runs/' + ident: run,
              '/api/agent-runs/' + ident + '/steps': {'items': [
                  {'step_id': s, 'state': 'failed' if s == 'finalize' else 'succeeded',
                   'started_at': start, 'completed_at': end, 'detail': {}} for s in steps]}}
    trace = trace_id(ident)
    observations = []
    for name in ['agent.execute'] + ['step.' + s for s in steps] + ['model.chat']:
        failed = name in ('agent.execute', 'step.finalize')
        parent = None if name == 'agent.execute' else 'step.generate' if name == 'model.chat' else 'agent.execute'
        observations.append({'id': span_id(trace, name), 'traceId': trace, 'projectId': project, 'name': name,
            'parentObservationId': span_id(trace, parent) if parent else None,
            'type': 'AGENT' if name == 'agent.execute' else 'GENERATION' if name == 'model.chat' else 'SPAN',
            'environment': 'agent-platform', 'startTime': start, 'endTime': end,
            'level': 'ERROR' if failed else 'DEFAULT',
            'metadata': {'agent_run_id': ident, 'agent_version_id': version, 'request_id': request,
                         'scope': 'runtime_execution', 'state': 'failed' if failed else 'succeeded',
                         'error_code': 'ANSWER_REFERENCE_INVALID' if failed else ''}})

    with ExitStack() as stack:
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='ops-fault-', dir='/tmp')))
        gateway = stack.enter_context(HTTPFixture(guard, lambda p: deepcopy(routes[p])))

        def metric(path):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            check(q.get('query', [''])[0] in [v[2] for v in QUERIES.values()], 'UNEXPECTED_METRIC_QUERY')
            return {'status': 'success', 'data': {'resultType': 'vector', 'result': [
                {'metric': {'job': 'host', 'instance': 'synthetic'}, 'value': [time.time(), '1']}]}}

        prom = stack.enter_context(HTTPFixture(guard, metric))
        lf = stack.enter_context(HTTPFixture(guard, lambda _: {'data': observations, 'meta': {}}))
        for name, content in [('access', 'synthetic-ops-code-0123456789'), ('agent', 'synthetic-agent-key-0123456789'),
            ('lf', json.dumps({'project_id': project, 'public_key': 'synthetic-public-key', 'secret_key': 'synthetic-secret-key'}))]:
            (folder/name).write_text(content)
            (folder/name).chmod(0o600)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        guard.ports.add(port)
        stack.callback(listener.close)
        cfg = {'platforms': {k: {'url': gateway.url} for k in ('model', 'data', 'knowledge', 'agent')},
               'access_file': str(folder/'access'), 'langfuse_file': str(folder/'lf'),
               'state_file': str(folder/'cache/success.json'), 'prometheus_url': prom.url, 'langfuse_url': lf.url,
               'timeout': .2, 'port': port, 'bind': '127.0.0.1', 'public_url': 'http://127.0.0.1:' + str(port),
               'tls_cert': None, 'tls_key': None}
        cfg['platforms']['agent']['token_file'] = str(folder/'agent')
        serving = uvicorn.Server(uvicorn.Config(create_app(cfg), log_level='critical', access_log=False, proxy_headers=False))
        thread = threading.Thread(target=lambda: serving.run(sockets=[listener]), daemon=True)
        thread.start()

        def stop_web():
            serving.should_exit = True
            thread.join(5)
            check(not thread.is_alive(), 'PROBE_WEB_DID_NOT_STOP')
        stack.callback(stop_web)
        until(lambda: serving.started, 'PROBE_WEB_NOT_READY', 5)

        def get(path):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                conn.request('GET', path, headers={'Authorization': 'Bearer synthetic-ops-code-0123456789'})
                response = conn.getresponse()
                body = response.read(1024*1024)
                check(response.status == 200 and len(body) < 1024*1024, 'PROBE_API_FAILED')
                check(b'FAULT_PRIVATE_SENTINEL' not in body and b'synthetic-secret-key' not in body, 'PRIVATE_RESPONSE_LEAK')
                return json.loads(body)
            finally:
                conn.close()

        def cache():
            return json.loads((folder/'cache/success.json').read_text())['success_times']

        def unaffected():
            overview = get('/api/overview')['platforms']
            check(all(v['gateway']['status'] == 'ok' for v in overview.values()), 'OTHER_SOURCE_FAILED')
            detail = get('/api/platforms/agent/agent-runs/' + ident)
            check(detail['status'] == 'ok' and detail['data']['state'] == 'failed'
                  and detail['data']['error']['code'] == 'ANSWER_REFERENCE_INVALID', 'BUSINESS_STATE_CHANGED')

        def successful(value):
            check(value['status'] == 'ok' and value['freshness'] == 'fresh' and value['last_success_at'], 'FIXTURE_BASELINE_FAILED')

        cases = []
        for kind, source in [('prometheus', prom), ('langfuse', lf)]:
            def read():
                if kind == 'prometheus':
                    return get('/api/resources')['metrics']
                return {'trace': get('/api/agent-runs/' + ident + '/trace')}
            for mode, expected_code in MODES.items():
                before = read()
                for envelope in before.values():
                    successful(envelope)
                stored = cache()
                tracked = {k: v for k, v in stored.items() if v in {x['last_success_at'] for x in before.values()}}
                check(len(tracked) == len(before), 'BASELINE_CACHE_NOT_FOUND')
                started = time.monotonic()
                with fault(source, mode):
                    bad = read()
                    for key, envelope in bad.items():
                        check(envelope['status'] == 'error' and envelope['data'] is None
                              and envelope['error']['code'] == expected_code
                              and envelope['observed_at'] is None and envelope['freshness'] == 'unavailable'
                              and envelope['last_success_at'] == before[key]['last_success_at'], 'FAULT_ENVELOPE_INCORRECT')
                    current = cache()
                    check(all(current.get(k) == v for k, v in tracked.items()), 'FAILURE_OVERWROTE_CACHE')
                    # A fresh reader must load persisted times even while its source is down.
                    # This is an isolated reader with the temporary cache, not a live restart.
                    reloaded = ReadService(cfg)
                    try:
                        cold = reloaded.resources()['metrics'] if kind == 'prometheus' else {'trace': reloaded.trace(ident)}
                        check(all(value['status'] == 'error' and value['last_success_at'] == before[key]['last_success_at']
                                  for key, value in cold.items()), 'PERSISTED_CACHE_NOT_RELOADED')
                    finally:
                        reloaded.close()
                    unaffected()
                elapsed = time.monotonic() - started
                check(elapsed < 5, 'FAULT_READ_NOT_BOUNDED')
                after = read()
                for key, envelope in after.items():
                    successful(envelope)
                    check(envelope['last_success_at'] > before[key]['last_success_at'], 'RECOVERY_TIME_NOT_ADVANCED')
                cases.append({'source': kind, 'fault': mode, 'result': 'passed', 'error_code': expected_code,
                              'last_success_preserved': True, 'persisted_cache_reloaded': True, 'recovery_verified': True})
        check(all(method == 'GET' for source in (gateway, prom, lf) for method, _, _ in source.calls), 'SOURCE_MUTATION')
        check('synthetic-' not in (folder/'cache/success.json').read_text(), 'CACHE_CONTENT_LEAK')
        return {'result': 'passed', 'cases': cases, 'actual_ops_http_server': True,
                'fixture_source_requests': sum(len(s.calls) for s in (gateway, prom, lf)),
                'synthetic_agent_failure_preserved': True, 'persistent_timestamp_cache_verified': True}


def agent_probe(guard):
    from agent_runtime import agents, observability as obs, server
    from agent.platform.v1 import platform_pb2 as pb

    identifiers = [str(uuid.uuid4()) for _ in range(10)]
    run, av, lease, request, dep, mv, mid, cv, tv, tool = identifiers
    version = {'agent_version_id': av, 'template_id': 'knowledge_qa_v1', 'template_version': 1,
               'tool_version_id': tv, 'model': {'deployment_id': dep, 'model_version_id': mv, 'connection_version_id': cv},
               'prompt': 'SYNTHETIC_PRIVATE_PROMPT', 'context_max_chars': 8000, 'max_tokens': 512, 'temperature': .2}
    snapshot = {'version': version, 'tool': {'version': {'tool_version_id': tv}}, 'model_connection': {'connection_version_id': cv}}
    hit = {'chunk_id': 'b'*64, 'text': 'SYNTHETIC_PRIVATE_DOCUMENT', 'source_document_id': run, 'canonical_id': lease}
    call = {'tool_call_id': tool, 'tool_version_id': tv, 'state': 'succeeded',
            'result': {'completion': 'completed', 'data': {'results': [hit]}, 'metadata': {'index_version_id': cv}}}
    execution = pb.AgentExecution(agent_run_id=run, agent_version_id=av, lease_token=lease,
        snapshot_json=json.dumps(snapshot).encode(), input_json=json.dumps({'question': 'SYNTHETIC_PRIVATE_QUESTION'}).encode())
    context = SimpleNamespace(is_active=lambda: True, time_remaining=lambda: 60,
                              invocation_metadata=lambda: [('x-request-id', request)])
    runtime = server.Runtime('synthetic-runtime-token-0123456789')
    responses = {'success': 'SYNTHETIC_PRIVATE_ANSWER [S1]', 'missing_reference': 'SYNTHETIC_PRIVATE_ANSWER'}

    def business(kind):
        events, model_calls = [], []

        def callback(message, **kwargs):
            check(message.agent_run_id == run and message.lease_token == lease, 'CALLBACK_IDENTITY_CHANGED')
            events.append((message.operation, message.step_id, json.loads(message.json)))
            return pb.ToolResponse(json=json.dumps(call if message.operation == 'search' else {'ok': True}).encode())

        def api(connection, path, ctx, body=None, **kwargs):
            model_calls.append(path)
            if path == '/api/deployments/' + dep:
                return {'deployment_id': dep, 'model_version_id': mv, 'status': 'ready', 'runtime_state': 'ready',
                        'desired_state': 'running', 'serving': {'engine': 'vllm', 'profile': '', 'max_model_len': 4096},
                        'served_model_name': 'deployment-' + dep, 'runtime_container_id': 'fixture'}
            if path == '/api/model-versions/' + mv:
                return {'model_version_id': mv, 'model_id': mid, 'version': 'v1'}
            if path == '/api/models/' + mid:
                return {'model_id': mid, 'model_type': 'llm', 'name': 'Fixture Chat'}
            check(path == '/api/deployments/' + dep + '/chat', 'UNEXPECTED_BUSINESS_CALL')
            return {'deployment_id': dep, 'model': 'deployment-' + dep, 'content': responses[kind], 'finish_reason': 'stop',
                    'usage': {'prompt_tokens': 40, 'completion_tokens': 20, 'total_tokens': 60}}

        with patch.object(agents, 'api', api), \
             patch.object(server.grpc, 'insecure_channel', return_value=SimpleNamespace(close=lambda: None)), \
             patch.object(server.platform_pb2_grpc, 'AgentServiceStub', return_value=SimpleNamespace(AgentRuntimeCommand=callback)):
            result = runtime.ExecuteAgent(execution, context)
        check(sum(p.endswith('/chat') for p in model_calls) == 1, 'MODEL_STUB_RETRIED')
        check(obs._current.get() is None, 'TRACE_CONTEXT_LEAK')
        if kind == 'missing_reference':
            check((result.error_code, result.error_stage) == ('ANSWER_REFERENCE_INVALID', 'finalize')
                  and not result.result_json, 'BUSINESS_FAILURE_CHANGED')
        else:
            check(not result.error_code and json.loads(result.result_json)['outcome'] == 'answered', 'BUSINESS_SUCCESS_CHANGED')
        return result.SerializeToString(deterministic=True), events, model_calls

    with patch.object(obs, '_exporter', None):
        baseline = {kind: business(kind) for kind in responses}
    cases = []
    with HTTPFixture(guard, lambda _: {}) as source:
        for mode in MODES:
            exporter = obs.Exporter(obs.Transport({'url': source.url, 'public_key': 'synthetic-public', 'secret_key': 'synthetic-secret'}))
            try:
                with fault(source, mode), patch.object(obs, '_exporter', exporter):
                    for kind in responses:
                        check(business(kind) == baseline[kind], 'OBSERVATION_CHANGED_BUSINESS')
                    until(lambda: sum(exporter.snapshot()[k] for k in ('accepted', 'failed', 'rejected')) == exporter.snapshot()['queued'],
                          'EXPORT_DID_NOT_SETTLE')
                    stats = exporter.snapshot()
                    terminal = 'rejected' if mode == 'http_401' else 'failed'
                    check(stats[terminal] == stats['queued'] and stats['queued'] == 14
                          and stats['accepted'] == 0 and stats['internal_error'] == 0, 'EXPORT_FAULT_NOT_OBSERVED')
                with patch.object(obs, '_exporter', exporter):
                    for kind in responses:
                        check(business(kind) == baseline[kind], 'RECOVERY_CHANGED_BUSINESS')
                    until(lambda: exporter.snapshot()['accepted'] == 14, 'EXPORT_RECOVERY_FAILED')
                cases.append({'fault': mode, 'result': 'passed', 'failed_or_rejected_spans': stats[terminal],
                              'recovered_spans': 14, 'business_results_and_callbacks_unchanged': True})
            finally:
                exporter.close(3)
                check(not exporter.thread.is_alive(), 'EXPORT_THREAD_DID_NOT_STOP')

        # Hold a real HTTP reply until AFTER business returns; the small queue must drop.
        source.mode = 'timeout'
        source.entered.clear()
        source.release.clear()
        exporter = obs.Exporter(obs.Transport({'url': source.url, 'public_key': 'synthetic-public', 'secret_key': 'synthetic-secret'}), capacity=2)
        try:
            exporter.submit({'name': 'synthetic.queue.probe'})
            check(source.entered.wait(2), 'BLOCKED_EXPORT_NOT_REACHED')
            blocked_at = time.monotonic()
            for _ in range(50):
                exporter.submit({'name': 'synthetic.queue.probe'})
            with patch.object(obs, '_exporter', exporter):
                for kind in responses:
                    check(business(kind) == baseline[kind], 'FULL_QUEUE_CHANGED_BUSINESS')
            check(not source.release.is_set() and exporter.snapshot()['dropped'] > 0
                  and time.monotonic() - blocked_at < 1.5, 'QUEUE_BOUND_NOT_EXERCISED')
            started = time.monotonic()
            exporter.close(.01)
            check(time.monotonic() - started < .5, 'EXPORT_CLOSE_BLOCKED_BUSINESS')
            dropped = exporter.snapshot()['dropped']
        finally:
            source.mode = 'ok'
            source.release.set()
            exporter.close(3)
            check(not exporter.thread.is_alive(), 'QUEUE_EXPORT_DID_NOT_STOP')
        raw = b''.join(body for _, _, body in source.calls)
        for secret in ('SYNTHETIC_PRIVATE_', 'synthetic-secret', runtime.token):
            check(secret.encode() not in raw, 'TRACE_BODY_LEAK')
        check(all(method == 'POST' and path == '/api/public/otel/v1/traces' for method, path, _ in source.calls), 'UNEXPECTED_TRACE_CALL')
    return {'result': 'passed', 'cases': cases, 'queue_pressure': {'result': 'passed', 'dropped': dropped,
            'business_returned_before_reply_release': True, 'bounded_close': True},
            'production_runtime_rpc_method_and_graph': True, 'service_callbacks': 'in_memory_fixture',
            'grpc_transport_or_database_acceptance': False, 'model_inference_requested': False,
            'metadata_only_export_verified': True}


def main(kind, expected):
    try:
        check(kind in ('ops', 'agent') and bool(expected), 'INVALID_PROBE_SPEC')
        for path, digest in expected.items():
            check(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, 'RUNTIME_SOURCE_MISMATCH')
        # Import-only environment: do not initialize the deployed application's tracing/config.
        os.environ.clear()
        os.environ.update(LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false', PYTHONDONTWRITEBYTECODE='1')
        guard = Guard()
        sys.addaudithook(guard.audit)
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(ProbeFailure('PROBE_DEADLINE')))
        signal.alarm(75)
        with redirect_stdout(io.StringIO()):
            result = ops_probe(guard) if kind == 'ops' else agent_probe(guard)
        check(guard.blocked == 0, 'PROBE_GUARD_TRIGGERED')
        result.update(component=kind, source_hashes_verified=len(expected), synthetic_only=True,
                      production_credentials_read=False, production_api_requests=0)
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, ProbeFailure) else 'PROBE_INTERNAL_ERROR'
        print(json.dumps({'result': 'attention_required', 'component': kind, 'code': code}), flush=True)
        return 2
    finally:
        signal.alarm(0)
