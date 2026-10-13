"""Public REST mappings grounded in the four source gateways, schema version 1."""
from dataclasses import dataclass
from datetime import datetime
import re
import uuid


class ReadFailure(Exception):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.code, self.http_status = code, http_status


def require(ok, code='INVALID_RESPONSE'):
    if not ok:
        raise ReadFailure(code)


def identifier(value):
    try:
        require(isinstance(value, str) and str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError):
        raise ReadFailure('INVALID_RESPONSE') from None
    return value


def machine(value):
    if value is None or value == '':
        return None
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value))
    return value


def timestamp(value):
    if value is None or value == '':
        return None
    require(isinstance(value, str) and len(value) <= 40)
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})', value) is not None)
    try:
        # Validate Go/PostgreSQL nanoseconds on Python 3.9 too, retaining the original text.
        normalized = re.sub(r'\.(\d+)', lambda m: '.'+(m.group(1)+'000000')[:6], value)
        dt = datetime.fromisoformat(normalized.replace('Z', '+00:00'))
        require(dt.tzinfo is not None)
    except ValueError:
        raise ReadFailure('INVALID_RESPONSE') from None
    return value


@dataclass(frozen=True)
class Resource:
    id_key: str
    state_key: str = 'status'
    listable: bool = True
    parent: bool = False


RESOURCES = {
    'model': {
        'operations': Resource('operation_id'),
        'model-versions': Resource('model_version_id', 'state'),
        'training-runs': Resource('training_run_id'),
        'deployments': Resource('deployment_id'),
    },
    'data': {
        'operations': Resource('operation_id'),
        'crawl-runs': Resource('crawl_run_id', ''),
        'refinement-runs': Resource('refinement_run_id', ''),
    },
    'knowledge': {
        'operations': Resource('operation_id', listable=False),
        'chunking-runs': Resource('chunking_run_id', ''),
        'indexes': Resource('index_id', 'state'),
        'index-versions': Resource('index_version_id', 'state', parent=True),
    },
    'agent': {
        'agent-runs': Resource('agent_run_id', 'state'),
        'tool-calls': Resource('tool_call_id', 'state', listable=False),
    },
}

IDENTIFIERS = (
    'operation_id', 'resource_id', 'request_id', 'model_id', 'model_version_id',
    'source_model_version_id', 'output_model_version_id', 'training_run_id',
    'deployment_id', 'source_document_id', 'chunking_run_id', 'index_id',
    'active_version_id', 'parent_version_id', 'embedding_deployment_id',
    'agent_id', 'agent_version_id', 'tool_version_id', 'tool_call_id', 'parent_agent_run_id',
)
TIMES = ('created_at', 'updated_at', 'started_at', 'completed_at', 'execution_started_at')
STEPS = ('validate', 'search', 'context', 'generate', 'finalize')


def resource(platform, name):
    require(platform in RESOURCES and name in RESOURCES[platform], 'UNSUPPORTED_RESOURCE')
    return RESOURCES[platform][name]


def project(platform, name, raw, expected_id=None):
    """Allowlist scalars; never return prompts, results, URLs, specs or error messages."""
    spec = resource(platform, name)
    require(isinstance(raw, dict))
    ident = identifier(raw.get(spec.id_key))
    if expected_id is not None:
        require(ident == expected_id, 'IDENTITY_MISMATCH')
        if platform == 'agent':
            identifier(raw.get('request_id'))
            identifier(raw.get('agent_version_id' if name == 'agent-runs' else 'tool_version_id'))
    if not spec.state_key:
        identifier(raw.get('operation_id'))
    state = machine(raw.get(spec.state_key)) if spec.state_key else None
    if spec.state_key:
        require(state is not None)
    error = raw.get('error') or {}
    require(isinstance(error, dict))
    codes = {k: machine(raw.get(k)) for k in ('error_code', 'submission_error_code', 'runtime_error_code', 'cancellation_error_code')}
    code = machine(error.get('code'))
    if code:
        codes['error_code'] = code
    return {
        'platform': platform, 'resource': name, 'id': ident,
        'source_path': '/api/'+name+'/'+ident,
        'state': state, 'state_field': spec.state_key or None,
        'phase': machine(raw.get('phase')),
        'error': {'code': codes['error_code'], 'stage': machine(error.get('stage'))},
        'related_errors': {k: v for k, v in codes.items() if k != 'error_code' and v},
        'parent_step_id': machine(raw.get('parent_step_id')),
        'caller_context': caller(raw.get('caller_context')),
        'states': {k: machine(raw.get(k)) for k in ('submission_status', 'runtime_state', 'desired_state', 'apply_status', 'reference_state') if k in raw},
        'kind': machine(raw.get('kind')), 'resource_type': machine(raw.get('resource_type')),
        'identifiers': {k: identifier(raw[k]) for k in IDENTIFIERS if raw.get(k)},
        'times': {k: timestamp(raw.get(k)) for k in TIMES},
        'time_fields_present': [k for k in TIMES if k in raw],
        'state_support': 'source_field' if spec.state_key else 'read_linked_operation',
        'quality': 'not_assessed',
    }


def caller(value):
    if value is None:
        return None
    require(isinstance(value, dict))
    # Only identifiers already recorded by the source; no URL or invented n8n state.
    result = {}
    for key in ('system', 'workflow_id', 'execution_id', 'node_id'):
        entry = value.get(key)
        if entry is None or entry == '':
            continue
        require(isinstance(entry, str) and len(entry) <= 256 and entry.isprintable())
        result[key] = entry
    return result


def project_steps(raw):
    require(isinstance(raw, dict) and isinstance(raw.get('items'), list))
    rows = raw['items']
    require([r.get('step_id') for r in rows if isinstance(r, dict)] == list(STEPS) and len(rows) == 5)
    result = []
    for row in rows:
        detail = row.get('detail') or {}
        require(isinstance(detail, dict))
        state = machine(row.get('state'))
        require(state is not None)
        result.append({'step_id': row['step_id'], 'state': state,
                       'started_at': timestamp(row.get('started_at')),
                       'completed_at': timestamp(row.get('completed_at')),
                       'error_code': machine(detail.get('error_code'))})
    return result
