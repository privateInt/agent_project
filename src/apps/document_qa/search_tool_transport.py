"""Bounded Agent Gateway traffic; Tool execution needs a separate inference opt-in."""
from http.client import HTTPException
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from . import readiness
from .contracts import ContractError, integer, strict_json, uid, validate_request
from .intake import UploadError, check
from .workflows import canonical

MAX_RESPONSE = 2 << 20


def request_checked(method, path, body, key):
    identifier = r'[a-f0-9-]{36}'
    get = method == 'GET' and body is None and key is None and re.fullmatch(
        r'/api/(?:connection-versions|tools|tool-versions|tool-calls)/' + identifier, path)
    create = method == 'POST' and path == '/api/tools'
    version = method == 'POST' and re.fullmatch(r'/api/tools/' + identifier + '/versions', path)
    publish = method == 'POST' and re.fullmatch(r'/api/tool-versions/' + identifier + '/publish', path)
    call = method == 'POST' and path == '/api/tool-calls'
    inspect = method == 'POST' and path == '/api/knowledge/inspect'
    check(get or create or version or publish or call or inspect, 'AGENT_TOOL_PATH_NOT_ALLOWED', 503)
    if get or version or publish:
        check(uid(path.split('/')[3]), 'AGENT_TOOL_PATH_NOT_ALLOWED', 503)
    if method == 'POST':
        check(uid(key) and isinstance(body, dict), 'AGENT_TOOL_REQUEST_INVALID', 503)
        if inspect:
            check(set(body) == {'action', 'connection_version_id', 'model_connection_version_id', 'index_id', 'index_version_id'}
                  and body['action'] == 'resolve' and all(uid(body[k]) for k in body if k != 'action'),
                  'AGENT_TOOL_REQUEST_INVALID', 503)
        elif create:
            check(set(body) == {'display_name', 'description'}
                  and all(isinstance(body[k], str) and not any(c in body[k] for c in ('\0', '\r', '\n'))
                          for k in body)
                  and 0 < len(body['display_name'].encode()) <= 200
                  and len(body['description'].encode()) <= 4000, 'AGENT_TOOL_REQUEST_INVALID', 503)
        elif version:
            check(set(body) == {'expected_revision', 'kind', 'binding', 'timeout_ms'}
                  and integer(body['expected_revision'], 1) and body['kind'] == 'knowledge_search_v1'
                  and integer(body['timeout_ms'], 1000, 240000) and isinstance(body['binding'], dict),
                  'AGENT_TOOL_REQUEST_INVALID', 503)
            b = body['binding']; k = b.get('knowledge')
            check(set(b) == {'connection_version_id', 'knowledge'} and uid(b['connection_version_id'])
                  and isinstance(k, dict) and set(k) == {'model_connection_version_id', 'index_id',
                  'index_version_id', 'embedding_deployment_id', 'embedding_space_id', 'top_k', 'candidate_k'}
                  and all(uid(k[n]) for n in ('model_connection_version_id', 'index_id', 'index_version_id', 'embedding_deployment_id'))
                  and readiness.digest(k['embedding_space_id'], 'es_')
                  and integer(k['top_k'], 1, 50) and integer(k['candidate_k'], k['top_k'], 100),
                  'AGENT_TOOL_REQUEST_INVALID', 503)
        elif publish:
            check(set(body) == {'expected_revision'} and integer(body['expected_revision'], 1),
                  'AGENT_TOOL_REQUEST_INVALID', 503)
        else:
            validate_request('agent.tool_call', canonical(body).encode())
            check(body['mode'] == 'test' and 'caller_context' not in body,
                  'AGENT_TOOL_REQUEST_INVALID', 503)
    return (201 if create or version else 202 if call else 200), bool(call)


class AgentToolGateway:
    check_request = staticmethod(request_checked)
    def __init__(self, row, execute=False, allow_inference=False, timeout=15,
                 budget_seconds=90, clock=time.monotonic):
        check(row.get('enabled', True) is True, 'AGENT_CONNECTION_DISABLED', 503)
        self.origin, self.token_file = readiness.origin(row['url']), row.get('token_file')
        self.writes_enabled = execute is True
        self.inference_enabled = self.writes_enabled and allow_inference is True
        self.clock, self.timeout, self.calls = clock, timeout, 0
        self.deadline = clock() + budget_seconds

    def request(self, method, path, body=None, key=None):
        expected, call = self.check_request(method, path, body, key)
        if method == 'POST' and path not in ('/api/knowledge/inspect', '/api/agent-models/inspect'):
            check(self.writes_enabled, 'AGENT_WRITE_NOT_AUTHORIZED', 409)
            check(not call or self.inference_enabled, 'TOOL_INFERENCE_NOT_AUTHORIZED', 409)
        check(self.calls < 16 and self.clock() < self.deadline, 'AGENT_TOOL_BUDGET_EXCEEDED', 504)
        headers = {'Accept': 'application/json'}
        if method == 'POST':
            headers.update({'Content-Type': 'application/json', 'Idempotency-Key': key})
        if self.token_file:
            try:
                token = readiness.read_file(self.token_file, 8192, credential=True).decode('ascii').strip()
                check(0 < len(token) <= 4096 and all(32 < ord(c) < 127 for c in token), 'AGENT_AUTH_UNAVAILABLE', 503)
            except (readiness.ReadinessError, UnicodeError):
                raise UploadError('AGENT_AUTH_UNAVAILABLE', 503) from None
            headers['Authorization'] = 'Bearer ' + token
        raw = canonical(body).encode() if body is not None else None
        check(raw is None or len(raw) <= 64 << 10, 'AGENT_TOOL_REQUEST_TOO_LARGE', 503)
        request = Request(self.origin + path, data=raw, headers=headers, method=method)
        self.calls += 1
        try:
            with build_opener(ProxyHandler({}), readiness.NoRedirect()).open(
                    request, timeout=min(self.timeout, self.deadline - self.clock())) as response:
                check(response.status == expected and response.headers.get_content_type() == 'application/json',
                      'AGENT_TOOL_RESPONSE_INVALID', 502)
                chunks, size = [], 0
                while True:
                    check(self.clock() < self.deadline, 'AGENT_TOOL_BUDGET_EXCEEDED', 504)
                    part = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                    if not part:
                        break
                    chunks.append(part); size += len(part)
                    check(size <= MAX_RESPONSE, 'AGENT_TOOL_RESPONSE_TOO_LARGE', 502)
                raw = b''.join(chunks)
                strict_json(raw)
                return response.status, raw
        except HTTPError as exc:
            code = ('AGENT_REDIRECT_REFUSED' if 300 <= exc.code < 400 else
                    'AGENT_AUTH_REQUIRED' if exc.code in (401, 403) else
                    'AGENT_RESOURCE_UNAVAILABLE' if exc.code in (404, 410) else
                    'AGENT_TOOL_CONFLICT' if exc.code in (409, 412) else 'AGENT_TOOL_HTTP_UNCONFIRMED')
            exc.close()
            raise UploadError(code, 502) from None
        except (OSError, URLError, HTTPException, TimeoutError):
            raise UploadError('AGENT_TOOL_OUTCOME_UNKNOWN', 502) from None
        except ContractError:
            raise UploadError('AGENT_TOOL_JSON_INVALID', 502) from None

    def get(self, path):
        return strict_json(self.request('GET', path)[1])
