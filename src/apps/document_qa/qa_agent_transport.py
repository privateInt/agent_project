"""Restricted fixed-QA Agent API; no lifecycle or inference without explicit opt-in."""
import re

from .contracts import integer, uid, validate_request
from .intake import check
from .qa_agent_results import definition_checked
from .search_tool_transport import AgentToolGateway, request_checked as tool_request_checked
from .workflows import canonical


def request_checked(method, path, body, key):
    identifier = r'[a-f0-9-]{36}'
    read = method == 'GET' and body is None and key is None and re.fullmatch(
        r'/api/(?:agents|agent-versions|agent-runs)/' + identifier, path)
    create = method == 'POST' and path == '/api/agents'
    version = method == 'POST' and re.fullmatch(r'/api/agents/' + identifier + '/versions', path)
    publish = method == 'POST' and re.fullmatch(r'/api/agent-versions/' + identifier + '/publish', path)
    run = method == 'POST' and path == '/api/agent-runs'
    inspect = method == 'POST' and path == '/api/agent-models/inspect'
    if method == 'GET' and path.startswith(('/api/tools/', '/api/tool-versions/', '/api/tool-calls/', '/api/connection-versions/')):
        return tool_request_checked(method, path, body, key)
    check(read or create or version or publish or run or inspect, 'QA_AGENT_PATH_NOT_ALLOWED', 503)
    if read or version or publish:
        check(uid(path.split('/')[3]), 'QA_AGENT_PATH_NOT_ALLOWED', 503)
    if method == 'POST':
        check(uid(key) and isinstance(body, dict), 'QA_AGENT_REQUEST_INVALID', 503)
        if inspect:
            check(set(body) == {'action', 'connection_version_id', 'deployment_id'}
                  and body['action'] == 'resolve' and uid(body['connection_version_id']) and uid(body['deployment_id']),
                  'QA_AGENT_REQUEST_INVALID', 503)
        elif create:
            check(set(body) == {'display_name', 'description'}
                  and all(isinstance(v, str) and not any(c in v for c in ('\0', '\r', '\n')) for v in body.values())
                  and 0 < len(body['display_name'].encode()) <= 200 and len(body['description'].encode()) <= 4000,
                  'QA_AGENT_REQUEST_INVALID', 503)
        elif version:
            check(set(body) == {'expected_revision', 'template_id', 'template_version', 'tool_version_id',
                               'model', 'prompt', 'context_max_chars', 'max_tokens', 'temperature', 'timeout_ms'}
                  and integer(body['expected_revision'], 1), 'QA_AGENT_REQUEST_INVALID', 503)
            definition_checked({k: v for k, v in body.items() if k != 'expected_revision'})
        elif publish:
            check(set(body) == {'expected_revision'} and integer(body['expected_revision'], 1), 'QA_AGENT_REQUEST_INVALID', 503)
        else:
            validate_request('agent.run', canonical(body).encode())
            check('caller_context' not in body, 'QA_AGENT_REQUEST_INVALID', 503)
    return (201 if create or version else 202 if run else 200), bool(run)


class AgentQAGateway(AgentToolGateway):
    check_request = staticmethod(request_checked)

    def request(self, method, path, body=None, key=None):
        request_checked(method, path, body, key)
        if method == 'POST' and path == '/api/agent-runs':
            check(self.writes_enabled and self.inference_enabled, 'QA_AGENT_INFERENCE_NOT_AUTHORIZED', 409)
        return super().request(method, path, body, key)
