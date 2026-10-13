"""Bounded Knowledge-only HTTP. Writes and builds require separate opt-ins."""
from http.client import HTTPException
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from . import readiness
from .contracts import ContractError, strict_json, uid, validate_request
from .intake import UploadError, check
from .workflows import canonical


class KnowledgeIndexer:
    def __init__(self, row, execute=False, allow_inference=False, timeout=100,
                 budget_seconds=125, clock=time.monotonic):
        check(row.get('enabled', True) is True, 'KNOWLEDGE_CONNECTION_DISABLED', 503)
        self.origin, self.token_file = readiness.origin(row['url']), row.get('token_file')
        self.writes_enabled = execute is True
        self.build_enabled = self.writes_enabled and allow_inference is True
        self.timeout, self.clock, self.calls = timeout, clock, 0
        self.deadline = clock() + budget_seconds

    def request(self, method, path, body=None, key=None):
        identifier = r'[a-f0-9-]{36}'
        get = method == 'GET' and body is None and key is None and re.fullmatch(
            r'/api/(?:indexes|index-versions)/' + identifier, path)
        create = method == 'POST' and path == '/api/indexes'
        build = method == 'POST' and re.fullmatch(r'/api/indexes/' + identifier + '/builds', path)
        activate = method == 'POST' and re.fullmatch(r'/api/indexes/' + identifier + '/activate', path)
        check(get or create or build or activate, 'KNOWLEDGE_INDEXING_PATH_NOT_ALLOWED', 503)
        if get or build or activate:
            check(uid(path.split('/')[3]), 'KNOWLEDGE_INDEXING_PATH_NOT_ALLOWED', 503)
        if method == 'POST':
            check(self.writes_enabled, 'KNOWLEDGE_WRITE_NOT_AUTHORIZED', 409)
            check(isinstance(body, dict), 'KNOWLEDGE_INDEXING_REQUEST_INVALID', 503)
            if build:
                check(self.build_enabled, 'INDEXING_INFERENCE_NOT_AUTHORIZED', 409)
                validate_request('knowledge.build', canonical(body).encode())
            elif activate:
                check(key is None, 'INDEXING_ACTIVATION_KEY_INVALID', 503)
                validate_request('knowledge.activate', canonical(body).encode())
            else:
                check(set(body) == {'name', 'embedding_deployment_id'}
                      and isinstance(body['name'], str)
                      and re.fullmatch(r'document-qa-[a-f0-9-]{36}', body['name'])
                      and uid(body['embedding_deployment_id']), 'KNOWLEDGE_INDEXING_REQUEST_INVALID', 503)
            if not activate:
                check(uid(key), 'KNOWLEDGE_INDEXING_KEY_INVALID', 503)
        check(self.calls < 8 and self.clock() < self.deadline, 'KNOWLEDGE_INDEXING_BUDGET_EXCEEDED', 504)
        headers = {'Accept': 'application/json'}
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
            if key:
                headers['Idempotency-Key'] = key
        if self.token_file:
            try:
                token = readiness.read_file(self.token_file, 8192, credential=True).decode('ascii').strip()
                check(0 < len(token) <= 4096 and all(32 < ord(char) < 127 for char in token),
                      'KNOWLEDGE_AUTH_UNAVAILABLE', 503)
            except (readiness.ReadinessError, UnicodeError):
                raise UploadError('KNOWLEDGE_AUTH_UNAVAILABLE', 503) from None
            headers['Authorization'] = 'Bearer ' + token
        raw = canonical(body).encode() if body is not None else None
        check(raw is None or len(raw) <= 64 << 10, 'KNOWLEDGE_INDEXING_REQUEST_TOO_LARGE', 503)
        request = Request(self.origin + path, data=raw, headers=headers, method=method)
        self.calls += 1
        expected = 201 if create else 202 if build else 200
        try:
            with build_opener(ProxyHandler({}), readiness.NoRedirect()).open(
                    request, timeout=min(self.timeout, self.deadline - self.clock())) as response:
                check(response.status == expected and response.headers.get_content_type() == 'application/json',
                      'KNOWLEDGE_INDEXING_RESPONSE_INVALID', 502)
                chunks, size = [], 0
                while True:
                    check(self.clock() < self.deadline, 'KNOWLEDGE_INDEXING_BUDGET_EXCEEDED', 504)
                    part = response.read1(min(65536, (64 << 10) + 1 - size))
                    if not part:
                        break
                    chunks.append(part)
                    size += len(part)
                    check(size <= 64 << 10, 'KNOWLEDGE_INDEXING_RESPONSE_TOO_LARGE', 502)
                raw = b''.join(chunks)
                strict_json(raw)
                return response.status, raw
        except HTTPError as exc:
            code = ('KNOWLEDGE_REDIRECT_REFUSED' if 300 <= exc.code < 400 else
                    'KNOWLEDGE_AUTH_REQUIRED' if exc.code in (401, 403) else
                    'KNOWLEDGE_RESOURCE_UNAVAILABLE' if exc.code in (404, 410) else
                    'INDEXING_ACTIVATION_CONFLICT' if activate and exc.code in (409, 412) else
                    'KNOWLEDGE_INDEXING_HTTP_UNCONFIRMED')
            exc.close()
            raise UploadError(code, 502) from None
        except (OSError, URLError, HTTPException, TimeoutError):
            raise UploadError('KNOWLEDGE_INDEXING_OUTCOME_UNKNOWN', 502) from None
        except ContractError:
            raise UploadError('KNOWLEDGE_INDEXING_JSON_INVALID', 502) from None

    def get(self, path):
        return strict_json(self.request('GET', path)[1])
