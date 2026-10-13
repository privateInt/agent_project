"""Q03a loopback operator API adapter; serving is explicit, never patch-triggered."""
import argparse
import hmac
import ipaddress
import json
import re
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import readiness
from .contracts import strict_json, ContractError, fingerprint
from .intake import MAX_REQUEST, UploadError, check, multipart
from .uploads import UploadStore, DataUploader, file_lock
from .recovery import projection as recovery_projection
from .web_assets import ASSETS, CSP, asset, browser_origin


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, handler, slots=4):
        try:
            ip = ipaddress.IPv4Address(address[0])
        except ipaddress.AddressValueError:
            raise UploadError('BIND_ADDRESS_INVALID') from None
        check(ip.is_loopback or any(ip in ipaddress.IPv4Network(cidr) for cidr in
                                  ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')),
              'PRIVATE_BIND_REQUIRED')
        self.slots = threading.BoundedSemaphore(slots)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Upstream text, document bytes and authentication headers must not reach access logs.
        pass


@contextmanager
def additional_listeners(server, addresses):
    """Explicit private interfaces, sharing the original handler and connection limit."""
    check(isinstance(addresses, (list, tuple)) and len(addresses) <= 4,
          'ADDITIONAL_BIND_LIMIT')
    if not addresses:
        yield []
        return
    primary, port = server.server_address
    seen = {primary}
    for address in addresses:
        check(isinstance(address, str), 'BIND_ADDRESS_INVALID')
        try:
            normalized = str(ipaddress.IPv4Address(address))
        except (ipaddress.AddressValueError, TypeError):
            raise UploadError('BIND_ADDRESS_INVALID') from None
        check(normalized not in seen, 'DUPLICATE_BIND_ADDRESS')
        seen.add(normalized)
    with ExitStack() as stack:
        listeners = []
        # Bind every requested interface before starting any extra request loop.
        # LocalServer keeps the existing private/loopback address restriction.
        for address in addresses:
            listener = stack.enter_context(LocalServer((address, port), server.RequestHandlerClass))
            listener.slots = server.slots
            listeners.append(listener)
        threads = [threading.Thread(target=listener.serve_forever,
                                    kwargs={'poll_interval': 0.25}, daemon=True)
                   for listener in listeners]
        started = []
        try:
            for listener, thread in zip(listeners, threads):
                thread.start()
                started.append((listener, thread))
            yield listeners
        finally:
            for listener, thread in started:
                listener.shutdown()
                thread.join(timeout=2)


def make_server(store, uploader, readiness_query=None, port=17110, workflow=None, bind='127.0.0.1',
                refinement=None, refiner_factory=None, refinement_flow=None,
                indexing=None, indexer_factory=None, indexing_flow=None, indexing_gate=None,
                qa_agent=None, agent_factory=None, question_head=None, head_factory=None,
                question_inference_enabled=False):
    token = readiness.read_file(store.token_path, limit=256, credential=True).decode('ascii').strip()
    check(re.fullmatch(r'[A-Za-z0-9_-]{43}', token), 'API_TOKEN_INVALID', 503)
    workflow_token = None
    if workflow is not None:
        from .workflows import private_secret
        workflow_token = private_secret(workflow.token_path)
    refinement_flow_token = None
    if refinement_flow is not None:
        from .workflows import private_secret
        refinement_flow_token = private_secret(refinement_flow.token_path)
    indexing_flow_token = None
    if indexing_flow is not None:
        from .workflows import private_secret
        indexing_flow_token = private_secret(indexing_flow.token_path)
    operator_slots = threading.BoundedSemaphore(4)
    stage_slots = threading.BoundedSemaphore(4) if indexing_flow is not None else None
    def project_job(job):
        job = workflow.project_job(job) if workflow is not None and 'workflow' not in job else job
        job = refinement.project_job(job) if refinement is not None else job
        job = refinement_flow.project_job(job) if refinement_flow is not None and 'refinement_workflow' not in job else job
        job = {**job, 'indexing': indexing.job(job['job_id'])} if indexing is not None else job
        job = indexing_flow.project_job(job) if indexing_flow is not None else job
        entry_setup = workflow.setup(False) if workflow is not None else None
        cpu_setup = refinement_flow.setup(False) if refinement_flow is not None else None
        capabilities = {
            'upload': uploader is not None,
            'entry_flow': bool((entry_setup or {}).get('prepared')),
            'cpu_flow': bool(refinement_flow is not None and refiner_factory is not None
                             and (cpu_setup or {}).get('prepared')),
            'refiner': refinement is not None and refiner_factory is not None,
            'configuration_revision': fingerprint({'entry': entry_setup, 'cpu': cpu_setup,
                                                    'data_origin': getattr(uploader, 'origin', None)}),
        }
        return {**job, 'recovery': recovery_projection(job, capabilities)}
    def question_policy():
        state = question_head.status() if question_head is not None else {'head_active': False, 'question_ready': False}
        enabled = question_inference_enabled is True and agent_factory is not None
        return {**state, 'question_ready': bool(state['head_active'] and enabled),
                'question_inference_enabled': enabled, 'head_activation_enabled': head_factory is not None,
                'question_API_connected': qa_agent is not None, 'policy_source': 'explicit_server_opt_in'}

    def project_library():
        result = workflow.project_library() if workflow is not None else store.library()
        if question_head is not None:
            policy = question_policy()
            result.update({k: policy[k] for k in ('head_revision', 'head_target_id', 'head_active', 'question_ready')})
        return result
    def dispatch_flow(job):
        if refinement_flow is not None and workflow is not None and workflow.handoff(job['job_id'])['state'] == 'accepted':
            try:
                outcome = refinement_flow.dispatch(job['job_id'])
            except UploadError as exc:
                outcome = {**refinement_flow.job(job['job_id']), 'dispatch_code': exc.code}
            job = {**job, 'refinement_workflow': outcome}
        return job

    def dispatch_indexing(job_id):
        if indexing_flow is None:
            return None
        setup = indexing_flow.setup(False)
        if not setup or not setup.get('prepared'):
            return indexing_flow.job(job_id)
        try:
            return indexing_flow.dispatch(job_id)
        except (UploadError, readiness.ReadinessError) as exc:
            return {**indexing_flow.job(job_id), 'dispatch_code': exc.code}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        server_version = 'DocumentQA'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass

        def reply(self, status, value):
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)

        def authenticate(self):
            values = self.headers.get_all('Authorization', [])
            selected_token = (indexing_flow_token if self.path.startswith('/api/workflow/indexing/')
                              else refinement_flow_token if self.path.startswith('/api/workflow/refinement/')
                              else workflow_token if self.path.startswith('/api/workflow/') else token)
            check(len(values) == 1 and values[0].isascii()
                  and selected_token is not None and hmac.compare_digest(values[0], 'Bearer ' + selected_token),
                  'AUTH_REQUIRED', 401)
            check(self.headers.get('Transfer-Encoding') is None and self.headers.get('Content-Encoding') is None,
                  'REQUEST_ENCODING_UNSUPPORTED')
            check('?' not in self.path and '#' not in self.path and len(self.path) <= 256, 'PATH_INVALID')

        def body(self, limit):
            values = self.headers.get_all('Content-Length', [])
            check(len(values) == 1 and re.fullmatch(r'[0-9]{1,10}', values[0]), 'CONTENT_LENGTH_REQUIRED', 411)
            length = int(values[0])
            check(0 < length <= limit, 'UPLOAD_REQUEST_TOO_LARGE', 413)
            chunks, remaining, deadline = [], length, time.monotonic() + 60
            while remaining:
                check(time.monotonic() < deadline, 'REQUEST_TIMEOUT', 408)
                chunk = self.rfile.read1(min(65536, remaining))
                check(chunk, 'REQUEST_TRUNCATED')
                chunks.append(chunk); remaining -= len(chunk)
            check(time.monotonic() < deadline, 'REQUEST_TIMEOUT', 408)
            return b''.join(chunks)

        def route(self, method):
            self.authenticate()
            browser_origin(self)
            if method == 'GET':
                check(self.headers.get('Content-Length', '0') == '0', 'GET_BODY_UNSUPPORTED')
                if self.path == '/api/library':
                    return 200, project_library()
                if self.path == '/api/jobs':
                    return 200, {'items': [project_job(job) for job in store.jobs()]}
                if self.path == '/api/qa-agent':
                    check(qa_agent is not None, 'QA_AGENT_NOT_INITIALIZED', 503)
                    return 200, qa_agent.status()
                if self.path in ('/api/question-head', '/api/question-policy'):
                    check(question_head is not None, 'QUESTION_HEAD_NOT_INITIALIZED', 503)
                    return 200, question_policy()
                if self.path.startswith('/api/question-head-requests/'):
                    check(question_head is not None, 'QUESTION_HEAD_NOT_INITIALIZED', 503)
                    return 200, question_head.by_request(self.path[len('/api/question-head-requests/'):])
                if self.path == '/api/questions':
                    check(qa_agent is not None, 'QA_AGENT_NOT_INITIALIZED', 503)
                    return 200, qa_agent.question_list()
                if self.path.startswith('/api/questions/'):
                    check(qa_agent is not None, 'QA_AGENT_NOT_INITIALIZED', 503)
                    return 200, qa_agent.question(self.path[len('/api/questions/'):])
                if self.path.startswith('/api/question-requests/'):
                    check(qa_agent is not None, 'QA_AGENT_NOT_INITIALIZED', 503)
                    return 200, qa_agent.by_request(self.path[len('/api/question-requests/'):])
                if self.path == '/api/workflows':
                    check(workflow is not None, 'WORKFLOW_NOT_INITIALIZED', 503)
                    return 200, workflow.status()
                if self.path == '/api/refinements':
                    check(refinement is not None, 'REFINEMENT_NOT_INITIALIZED', 503)
                    return 200, refinement.status()
                if self.path == '/api/refinement-workflows':
                    check(refinement_flow is not None, 'REFINEMENT_FLOW_NOT_INITIALIZED', 503)
                    return 200, refinement_flow.status()
                if self.path == '/api/indexings':
                    check(indexing is not None, 'INDEXING_NOT_INITIALIZED', 503)
                    return 200, indexing.status()
                if self.path == '/api/indexing-workflows':
                    check(indexing_flow is not None, 'INDEXING_FLOW_NOT_INITIALIZED', 503)
                    return 200, indexing_flow.status()
                if self.path == '/api/indexing-policy':
                    return 200, {'schema': 1, 'inference_enabled': getattr(indexing_gate, 'allow_inference', False) is True,
                                 'policy_source': 'explicit_server_opt_in', 'question_ready': False}
                if self.path.startswith('/api/jobs/') and self.path.endswith('/indexing-workflow'):
                    check(indexing_flow is not None, 'INDEXING_FLOW_NOT_INITIALIZED', 503)
                    return 200, indexing_flow.job(self.path[len('/api/jobs/'):-len('/indexing-workflow')])
                if self.path.startswith('/api/jobs/') and self.path.endswith('/indexing'):
                    check(indexing is not None, 'INDEXING_NOT_INITIALIZED', 503)
                    return 200, indexing.job(self.path[len('/api/jobs/'):-len('/indexing')])
                if self.path.startswith('/api/jobs/') and self.path.endswith('/refinement-workflow'):
                    check(refinement_flow is not None, 'REFINEMENT_FLOW_NOT_INITIALIZED', 503)
                    return 200, refinement_flow.job(self.path[len('/api/jobs/'):-len('/refinement-workflow')])
                if self.path.startswith('/api/jobs/') and self.path.endswith('/refinement'):
                    check(refinement is not None, 'REFINEMENT_NOT_INITIALIZED', 503)
                    return 200, refinement.job(self.path[len('/api/jobs/'):-len('/refinement')])
                if self.path.startswith('/api/jobs/') and self.path.endswith('/workflow'):
                    check(workflow is not None, 'WORKFLOW_NOT_INITIALIZED', 503)
                    return 200, workflow.handoff(self.path[len('/api/jobs/'):-len('/workflow')])
                if self.path == '/api/readiness':
                    check(readiness_query is not None, 'READINESS_NOT_CONFIGURED', 503)
                    result = readiness_query()
                    return 200, result  # Logical blockers are carried in the readiness report.
                if self.path.startswith('/api/jobs/'):
                    return 200, project_job(store.job(self.path[len('/api/jobs/'):]))
                if self.path.startswith('/api/upload-requests/'):
                    return 200, project_job(store.by_request(self.path[len('/api/upload-requests/'):]))
                raise UploadError('PATH_NOT_FOUND', 404)
            if method == 'POST' and self.path == '/api/question-head/activate':
                check(question_head is not None and head_factory is not None, 'QUESTION_HEAD_ACTIVATION_NOT_ENABLED', 409)
                types = self.headers.get_all('Content-Type', [])
                keys = self.headers.get_all('Idempotency-Key', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json'
                      and len(keys) == 1, 'QUESTION_HEAD_INPUT_INVALID')
                try:
                    value = strict_json(self.body(2048))
                except ContractError:
                    raise UploadError('QUESTION_HEAD_INPUT_INVALID') from None
                check(set(value) == {'plan_id', 'expected_revision'}, 'QUESTION_HEAD_INPUT_INVALID')
                from .question_head import request_checked
                request_checked(keys[0], value['plan_id'], value['expected_revision'])
                prior = question_head.replay(keys[0], value['plan_id'], value['expected_revision'])
                if prior is not None:
                    return 200, prior  # Recover a committed result even if transport setup now fails.
                agent, knowledge = head_factory()
                return 200, question_head.activate(keys[0], value['plan_id'], value['expected_revision'], agent, knowledge)
            match = re.fullmatch(r'/api/questions/([^/]+)/(dispatch|retry|observe)', self.path)
            if method == 'POST' and (self.path == '/api/questions' or match):
                check(qa_agent is not None, 'QA_AGENT_NOT_INITIALIZED', 503)
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    value = strict_json(self.body(64 << 10))
                except ContractError:
                    raise UploadError('QUESTION_INPUT_INVALID') from None
                if self.path == '/api/questions':
                    keys = self.headers.get_all('Idempotency-Key', [])
                    check(set(value) == {'question'} and len(keys) == 1, 'QUESTION_INPUT_INVALID')
                    return 202, (question_head.accept(keys[0], value['question']) if question_head is not None
                                 else qa_agent.accept(keys[0], value['question']))
                check(value == {} and agent_factory is not None, 'QUESTION_ACTION_NOT_CONFIGURED', 503)
                check(question_head is None or match[2] == 'observe' or question_inference_enabled is True,
                      'QA_AGENT_INFERENCE_NOT_AUTHORIZED', 409)
                agent = agent_factory()
                return 200 if match[2] == 'observe' else 202, (
                    qa_agent.observe_question(match[1], agent) if match[2] == 'observe'
                    else qa_agent.dispatch(match[1], agent, retry=match[2] == 'retry'))
            if method == 'POST' and self.path == '/api/uploads':
                types = self.headers.get_all('Content-Type', [])
                keys = self.headers.get_all('Idempotency-Key', [])
                check(len(types) == 1 and len(keys) == 1, 'UPLOAD_HEADERS_INVALID')
                meta, data = multipart(types[0], self.body(MAX_REQUEST))
                job = store.accept(keys[0], meta['name'], meta['filename'], data, uploader.origin)
                # A duplicate never silently replays a previously ambiguous upstream POST.
                if job['attempts'] == 0:
                    try:
                        job = store.dispatch(job['job_id'], uploader)
                    except UploadError as exc:
                        job = {**store.job(job['job_id']), 'dispatch_code': exc.code}
                if workflow is not None and job['state'] == 'preparation_pending':
                    try:
                        handoff = workflow.dispatch(job['job_id'])
                    except UploadError as exc:
                        handoff = {**workflow.handoff(job['job_id']), 'dispatch_code': exc.code}
                    job = {**job, 'workflow': handoff, 'workflow_started': handoff['workflow_started']}
                return 202, project_job(dispatch_flow(job))
            if method == 'POST' and self.path in ('/api/workflow/refinement/receipts', '/api/workflow/refinement/step'):
                check(refinement_flow is not None, 'REFINEMENT_FLOW_NOT_INITIALIZED', 503)
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    value = strict_json(self.body(8192))
                except ContractError:
                    raise UploadError('REFINEMENT_FLOW_INPUT_INVALID') from None
                if self.path.endswith('/receipts'):
                    return 200, refinement_flow.accept_receipt(value)
                check(refiner_factory is not None, 'REFINEMENT_NOT_INITIALIZED', 503)
                result = refinement_flow.step(value, refiner_factory)
                if result['state'] == 'canonical_verified' and indexing_flow is not None:
                    result = {**result, 'indexing_workflow': dispatch_indexing(value['job_id'])}
                return 200, result
            if method == 'POST' and self.path in ('/api/workflow/indexing/receipts', '/api/workflow/indexing/step'):
                check(indexing_flow is not None, 'INDEXING_FLOW_NOT_INITIALIZED', 503)
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    value = strict_json(self.body(8192))
                except ContractError:
                    raise UploadError('INDEXING_FLOW_INPUT_INVALID') from None
                if self.path.endswith('/receipts'):
                    return 200, indexing_flow.accept_receipt(value)
                check(indexer_factory is not None and indexing_gate is not None, 'INDEXING_NOT_INITIALIZED', 503)
                return 200, indexing_flow.step(value, indexer_factory, indexing_gate)
            if method == 'POST' and self.path == '/api/workflow/receipts':
                check(workflow is not None, 'WORKFLOW_NOT_INITIALIZED', 503)
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    value = strict_json(self.body(8192))
                except ContractError:
                    raise UploadError('HANDOFF_INPUT_INVALID') from None
                return 200, workflow.accept_receipt(value)
            match = re.fullmatch(r'/api/jobs/([^/]+)/recover', self.path)
            if method == 'POST' and match:
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    value = strict_json(self.body(1024))
                except ContractError:
                    raise UploadError('RECOVERY_BODY_INVALID') from None
                check(isinstance(value, dict) and set(value) == {'action', 'revision'}
                      and isinstance(value['action'], str) and isinstance(value['revision'], str)
                      and re.fullmatch(r'[0-9a-f]{64}', value['revision']), 'RECOVERY_BODY_INVALID')
                store.job(match[1])  # Validate existing UUID before creating a bounded lock file.
                with file_lock(store.root / 'locks' / (match[1] + '.recovery.lock')):
                    current = project_job(store.job(match[1]))['recovery']
                    check(hmac.compare_digest(value['revision'], current['revision']), 'RECOVERY_STATE_CHANGED', 409)
                    check(value['action'] in {item['id'] for item in current['actions']}, 'RECOVERY_ACTION_NOT_ALLOWED', 409)
                    action = value['action']
                    if action == 'retry-upload':
                        store.dispatch(match[1], uploader, retry=True)
                    elif action in ('dispatch-workflow', 'retry-workflow'):
                        workflow.dispatch(match[1], retry=action == 'retry-workflow')
                    elif action in ('dispatch-refinement-workflow', 'retry-refinement-workflow', 'resume-refinement-workflow'):
                        refinement_flow.dispatch(match[1], retry=action == 'retry-refinement-workflow',
                                                 resume=action == 'resume-refinement-workflow')
                    elif action == 'retry-refinement':
                        refinement.dispatch(match[1], refiner_factory(), retry=True)
                    elif action == 'observe-refinement':
                        refinement.observe(match[1], refiner_factory())
                    # This route performs only the selected stage. It never hands
                    # off to indexing or modifies a saved inference grant.
                    return 202, {'schema': 1, 'action': action, 'job': project_job(store.job(match[1])),
                                 'inference_requested': False}
            match = re.fullmatch(r'/api/jobs/([^/]+)/(dispatch-workflow|retry-workflow)', self.path)
            if method == 'POST' and match:
                check(workflow is not None, 'WORKFLOW_NOT_INITIALIZED', 503)
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    check(strict_json(self.body(1024)) == {}, 'RETRY_BODY_INVALID')
                except ContractError:
                    raise UploadError('RETRY_BODY_INVALID') from None
                result = workflow.dispatch(match[1], retry=match[2] == 'retry-workflow')
                if refinement_flow is not None:
                    result = {**result, 'refinement_workflow': dispatch_flow(store.job(match[1])).get('refinement_workflow')}
                return 202, result
            match = re.fullmatch(r'/api/jobs/([^/]+)/(dispatch-refinement-workflow|retry-refinement-workflow|resume-refinement-workflow)', self.path)
            if method == 'POST' and match:
                check(refinement_flow is not None, 'REFINEMENT_FLOW_NOT_INITIALIZED', 503)
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    check(strict_json(self.body(1024)) == {}, 'REFINEMENT_FLOW_BODY_INVALID')
                except ContractError:
                    raise UploadError('REFINEMENT_FLOW_BODY_INVALID') from None
                return 202, refinement_flow.dispatch(match[1], retry=match[2] == 'retry-refinement-workflow',
                                                      resume=match[2] == 'resume-refinement-workflow')
            match = re.fullmatch(r'/api/jobs/([^/]+)/(start-refinement|retry-refinement|observe-refinement)', self.path)
            if method == 'POST' and match:
                check(refinement is not None and refiner_factory is not None, 'REFINEMENT_NOT_INITIALIZED', 503)
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    check(strict_json(self.body(1024)) == {}, 'REFINEMENT_BODY_INVALID')
                except ContractError:
                    raise UploadError('REFINEMENT_BODY_INVALID') from None
                api = refiner_factory()
                result = (refinement.observe(match[1],api) if match[2] == 'observe-refinement'
                          else refinement.dispatch(match[1],api,retry=match[2] == 'retry-refinement'))
                return 202, result
            match = re.fullmatch(r'/api/jobs/([^/]+)/(dispatch-indexing-workflow|retry-indexing-workflow|resume-indexing-workflow)', self.path)
            if method == 'POST' and match:
                check(indexing_flow is not None, 'INDEXING_FLOW_NOT_INITIALIZED', 503)
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    check(strict_json(self.body(1024)) == {}, 'INDEXING_FLOW_BODY_INVALID')
                except ContractError:
                    raise UploadError('INDEXING_FLOW_BODY_INVALID') from None
                return 202, indexing_flow.dispatch(match[1], retry=match[2] == 'retry-indexing-workflow',
                                                   resume=match[2] == 'resume-indexing-workflow')
            match = re.fullmatch(r'/api/jobs/([^/]+)/retry-upload', self.path)
            if method == 'POST' and match:
                types = self.headers.get_all('Content-Type', [])
                check(len(types) == 1 and types[0].split(';', 1)[0].strip() == 'application/json', 'JSON_REQUIRED')
                try:
                    check(strict_json(self.body(1024)) == {}, 'RETRY_BODY_INVALID')
                except ContractError:
                    raise UploadError('RETRY_BODY_INVALID') from None
                return 202, project_job(store.dispatch(match[1], uploader, retry=True))
            raise UploadError('PATH_NOT_FOUND', 404)

        def handle_method(self, method):
            # Four operator calls may wait for n8n callbacks. Reserve additional
            # bounded connections so those callbacks can commit their receipts.
            operator = not self.path.startswith('/api/workflow/')
            stage = stage_slots is not None and not operator and self.path.endswith('/step')
            selected_slots = operator_slots if operator else stage_slots if stage else None
            acquired = selected_slots is None or selected_slots.acquire(blocking=False)
            if not acquired:
                self.reply(503, {'code': 'API_CAPACITY_EXCEEDED', 'question_ready': False})
                return
            try:
                status, value = self.route(method)
            except UploadError as exc:
                status, value = exc.status, {'code': exc.code, 'question_ready': False}
            except readiness.ReadinessError as exc:
                status, value = 503, {'code': exc.code, 'question_ready': False}
            except (socket.timeout, TimeoutError):
                status, value = 408, {'code': 'REQUEST_TIMEOUT', 'question_ready': False}
            except (sqlite3.Error, OSError):
                # A failed local commit may already be durable. Resolve by the original client key.
                status, value = 503, {'code': 'LOCAL_STATE_UNCERTAIN', 'question_ready': False}
            except Exception:
                status, value = 503, {'code': 'LOCAL_STATE_UNCERTAIN', 'question_ready': False}
            finally:
                if selected_slots is not None:
                    selected_slots.release()
            try:
                self.reply(status, value)
            except (OSError, ConnectionError):
                pass  # Closing the page cannot erase a committed job or receipt.

        def do_GET(self):
            if self.path in ASSETS:
                try:
                    browser_origin(self, require_host=True)
                    check(self.headers.get('Content-Length', '0') == '0'
                          and self.headers.get('Transfer-Encoding') is None
                          and self.headers.get('Content-Encoding') is None, 'GET_BODY_UNSUPPORTED')
                    content_type, raw = asset(self.path)
                    self.send_response(200)
                    for name, value in (
                        ('Content-Type', content_type), ('Content-Length', str(len(raw))),
                        ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                        ('Content-Security-Policy', CSP), ('Referrer-Policy', 'no-referrer'),
                        ('X-Frame-Options', 'DENY'), ('Connection', 'close')):
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(raw)
                except UploadError as exc:
                    self.reply(exc.status, {'code': exc.code})
                except OSError:
                    try:
                        self.reply(503, {'code': 'SCREEN_ASSET_UNAVAILABLE'})
                    except OSError:
                        pass
                return
            self.handle_method('GET')

        def do_POST(self):
            self.handle_method('POST')

        def do_OPTIONS(self):
            self.reply(405, {'code': 'METHOD_NOT_ALLOWED'})

    return LocalServer((bind, port), Handler, slots=12 if indexing_flow is not None else 8 if workflow is not None else 4)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'status', 'serve'))
    user_home = Path.home()
    parser.add_argument('--state-dir', type=Path, default=user_home / '.local/state/agent-project-document-qa/uploads')
    parser.add_argument('--library-name', default='문서 자료함')
    parser.add_argument('--settings', type=Path, default=user_home / '.local/state/agent-project-document-qa/settings.json')
    parser.add_argument('--connections-from', type=Path, default=user_home / '.local/state/agent-project-ops/server/host-config.json')
    parser.add_argument('--port', type=int, default=17110)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--additional-bind', action='append', default=[],
                        help='Also listen on an explicit private IPv4 address; repeat at most four times')
    parser.add_argument('--allow-indexing-inference', action='store_true',
                        help='Explicit server opt-in for Q05c; omit while GPU verification is deferred')
    parser.add_argument('--allow-question-head-activation', action='store_true',
                        help='Enable operator CAS using existing verified assets; never starts inference')
    parser.add_argument('--allow-question-inference', action='store_true',
                        help='Enable question AgentRun dispatch; independent of indexing opt-in')
    args = parser.parse_args(argv)
    try:
        store = UploadStore.initialize(args.state_dir, args.library_name) if args.action == 'init' else UploadStore(args.state_dir)
        from .workflows import WorkflowStore
        try:
            workflow = WorkflowStore(store)
        except UploadError as exc:
            if exc.code != 'WORKFLOW_NOT_INITIALIZED':
                raise
            workflow = None
        from .refinement import RefinementStore, DataRefiner
        try:
            refinement = RefinementStore(store)
        except UploadError as exc:
            if exc.code != 'REFINEMENT_NOT_INITIALIZED':
                raise
            refinement = None
        from .refinement_flow import RefinementFlow
        try:
            refinement_flow = RefinementFlow(store)
        except UploadError as exc:
            if exc.code != 'REFINEMENT_FLOW_NOT_INITIALIZED':
                raise
            refinement_flow = None
        from .indexing import IndexingStore
        try:
            indexing = IndexingStore(store)
        except UploadError as exc:
            if exc.code != 'INDEXING_NOT_INITIALIZED':
                raise
            indexing = None
        from .indexing_flow import IndexingFlow
        try:
            indexing_flow = IndexingFlow(store)
        except UploadError as exc:
            if exc.code != 'INDEXING_FLOW_NOT_INITIALIZED':
                raise
            indexing_flow = None
        from .question_head import QuestionHeadStore
        try:
            question_head = QuestionHeadStore(store)
        except UploadError as exc:
            if exc.code not in ('SEARCH_TOOL_NOT_INITIALIZED', 'QUESTION_NOT_INITIALIZED',
                                'QA_AGENT_NOT_INITIALIZED', 'QUESTION_HEAD_NOT_INITIALIZED', 'INDEXING_NOT_INITIALIZED'):
                raise
            question_head = None
        qa_agent = question_head.qa if question_head is not None else None
        if args.action != 'serve':
            def project(job):
                job = workflow.project_job(job) if workflow else job
                job = refinement.project_job(job) if refinement else job
                job = refinement_flow.project_job(job) if refinement_flow else job
                job = {**job, 'indexing': indexing.job(job['job_id'])} if indexing else job
                return indexing_flow.project_job(job) if indexing_flow else job
            print(json.dumps({'schema': 1, 'stage': 'Q03a-upload-acceptance', 'action': args.action,
                              'screen_implemented': True, 'screen_version': 'q08.1', 'screen_path': '/console',
                              'screen_recovery_implemented': True, 'screen_recovery_scope': 'upload_and_CPU_only',
                              'screen_questions_implemented': True, 'screen_question_policy_source': '/api/question-policy',
                              'result': 'passed', 'library': workflow.project_library() if workflow else store.library(),
                              'jobs': [project(job) for job in store.jobs()],
                              **({'refinement':refinement.status()} if refinement else {}),
                              **({'refinement_workflow':refinement_flow.status()} if refinement_flow else {}),
                              **({'indexing':indexing.status()} if indexing else {}),
                              **({'indexing_workflow':indexing_flow.status()} if indexing_flow else {}),
                              **({'question_head':question_head.status()} if question_head else {}),
                              'production_api_requests': 0, 'inference_requested': False}, ensure_ascii=False))
            return 0
        check(1 <= args.port <= 65535, 'PORT_INVALID')
        settings, _, _ = readiness.load_settings(args.settings, args.connections_from)
        uploader = DataUploader(settings['platforms']['data'])
        query = lambda: readiness.inspect(settings, readiness.GatewayReader(settings['platforms']))
        refiner_factory = lambda: DataRefiner(settings['platforms']['data'])
        from .indexing_runtime import IndexingGate
        from .indexing_transport import KnowledgeIndexer
        gate = IndexingGate(settings, args.allow_indexing_inference)
        indexer_factory = lambda **options: KnowledgeIndexer(settings['platforms']['knowledge'], **options)
        from .qa_agent_transport import AgentQAGateway
        agent_factory = (lambda: AgentQAGateway(settings['platforms']['agent'],
                         execute=args.allow_question_inference, allow_inference=args.allow_question_inference)) if qa_agent else None
        head_factory = (lambda: (AgentQAGateway(settings['platforms']['agent']),
                                KnowledgeIndexer(settings['platforms']['knowledge']))) if (
                                    question_head is not None and args.allow_question_head_activation) else None
        check(not (args.allow_question_head_activation or args.allow_question_inference) or question_head is not None,
              'QUESTION_HEAD_NOT_INITIALIZED', 503)
        with make_server(store, uploader, query, args.port, workflow, args.bind, refinement, refiner_factory, refinement_flow,
                         indexing, indexer_factory, indexing_flow, gate, qa_agent, agent_factory, question_head,
                         head_factory, args.allow_question_inference) as server, \
                additional_listeners(server, args.additional_bind) as extra_servers:
            print(json.dumps({'result': 'listening', 'bind': args.bind, 'port': args.port,
                              'additional_binds': [item.server_address[0] for item in extra_servers],
                              'screen_version': 'q08.1', 'screen_path': '/console',
                              'screen_recovery_implemented': True, 'screen_recovery_scope': 'upload_and_CPU_only',
                              'screen_questions_implemented': True, 'screen_question_policy_source': '/api/question-policy',
                              'question_ready': bool(question_head is not None and args.allow_question_inference
                                                     and question_head.status()['head_active']), 'workflow_started': False,
                              'indexing_inference_enabled': args.allow_indexing_inference,
                              'question_API_connected': qa_agent is not None,
                              'head_activation_enabled': head_factory is not None,
                              'question_inference_enabled': args.allow_question_inference}), flush=True)
            server.serve_forever(poll_interval=0.25)
    except (UploadError, readiness.ReadinessError) as exc:
        print(json.dumps({'result': 'attention_required', 'code': exc.code, 'question_ready': False}))
        return 2
    except (OSError, sqlite3.Error, UnicodeError):
        print(json.dumps({'result': 'attention_required', 'code': 'LOCAL_STATE_UNCERTAIN', 'question_ready': False}))
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
