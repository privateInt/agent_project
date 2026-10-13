"""Q06a pure gates for pinned Agent resources. No HTTP or GPU dependencies."""
import hashlib
import math
import re

from . import readiness
from .contracts import digest, fingerprint, integer, uid
from .intake import check
from .workflows import canonical


def selection_checked(value):
    fields = {'agent_origin', 'knowledge_connection', 'model_connection',
              'top_k', 'candidate_k', 'timeout_ms'}
    check(isinstance(value, dict) and set(value) == fields, 'SEARCH_TOOL_SELECTION_INVALID', 409)
    result = dict(value)
    result['agent_origin'] = readiness.origin(value['agent_origin'])
    for key in ('knowledge_connection', 'model_connection'):
        row = value[key]
        check(isinstance(row, dict) and set(row) == {'version_id', 'base_url'}
              and uid(row['version_id']), 'SEARCH_TOOL_CONNECTION_INVALID', 409)
        result[key] = {'version_id': row['version_id'], 'base_url': readiness.origin(row['base_url'])}
    check(integer(value['top_k'], 1, 50) and integer(value['candidate_k'], value['top_k'], 100)
          and integer(value['timeout_ms'], 1000, 240000), 'SEARCH_TOOL_LIMIT_INVALID', 409)
    return result


def binding(plan):
    selection, profile = plan['selection'], plan['config']['embedding']
    return {'connection_version_id': selection['knowledge_connection']['version_id'],
            'knowledge': {'model_connection_version_id': selection['model_connection']['version_id'],
                          'index_id': plan['index_id'], 'index_version_id': plan['index_version_id'],
                          'embedding_deployment_id': profile['deployment_id'],
                          'embedding_space_id': profile['embedding_space_id'],
                          'top_k': selection['top_k'], 'candidate_k': selection['candidate_k']}}


def connection_checked(body, expected):
    fields = {'connection_version_id', 'connection_id', 'version', 'base_url', 'auth_type',
              'credential_ref', 'credential_revision', 'api_key_in', 'api_key_name', 'created_at'}
    check(isinstance(body, dict) and set(body) <= fields
          and body.get('connection_version_id') == expected['version_id']
          and uid(body.get('connection_id')) and integer(body.get('version'), 1)
          and readiness.origin(body.get('base_url')) == expected['base_url'],
          'SEARCH_TOOL_CONNECTION_MISMATCH', 502)
    auth = body.get('auth_type')
    check(auth in ('none', 'bearer', 'api_key'), 'SEARCH_TOOL_CONNECTION_MISMATCH', 502)
    ref = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,63}')
    if auth == 'none':
        check(not any(body.get(k) for k in ('credential_ref', 'credential_revision', 'api_key_in', 'api_key_name')),
              'SEARCH_TOOL_CONNECTION_MISMATCH', 502)
    else:
        check(all(isinstance(body.get(k), str) and ref.fullmatch(body[k])
                  for k in ('credential_ref', 'credential_revision')), 'SEARCH_TOOL_CONNECTION_MISMATCH', 502)
        check((auth == 'bearer' and not body.get('api_key_in') and not body.get('api_key_name'))
              or (auth == 'api_key' and body.get('api_key_in') in ('header', 'query')
                  and isinstance(body.get('api_key_name'), str) and ref.fullmatch(body['api_key_name'])),
              'SEARCH_TOOL_CONNECTION_MISMATCH', 502)
    return {k: body[k] for k in sorted(set(body) - {'created_at'})}


def tool_checked(body, expected, tool_id=None):
    check(isinstance(body, dict) and uid(body.get('tool_id'))
          and (tool_id is None or body['tool_id'] == tool_id)
          and body.get('display_name') == expected['display_name']
          and body.get('description') == expected['description'] and body.get('enabled') is True
          and integer(body.get('revision'), 1), 'SEARCH_TOOL_RESOURCE_MISMATCH', 502)
    return {k: body[k] for k in ('tool_id', 'display_name', 'description', 'enabled', 'revision')}


def version_checked(body, plan, tool_id, version_id=None, saved=None):
    check(isinstance(body, dict) and uid(body.get('tool_version_id'))
          and (version_id is None or body['tool_version_id'] == version_id)
          and body.get('tool_id') == tool_id and body.get('kind') == 'knowledge_search_v1'
          and isinstance(body.get('binding'), dict) and canonical(body['binding']) == canonical(binding(plan))
          and type(body.get('timeout_ms')) is int and body['timeout_ms'] == plan['selection']['timeout_ms']
          and integer(body.get('version'), 1, 100) and integer(body.get('revision'), 1)
          and body.get('state') in ('draft', 'published') and digest(body.get('definition_sha256')),
          'SEARCH_TOOL_VERSION_MISMATCH', 502)
    inp, out = body.get('input_schema'), body.get('output_schema')
    check(isinstance(inp, dict) and inp.get('type') == 'object' and inp.get('required') == ['query']
          and inp.get('additionalProperties') is False and isinstance(inp.get('properties'), dict)
          and isinstance(inp['properties'].get('query'), dict) and inp['properties']['query'].get('type') == 'string'
          and isinstance(out, dict) and out.get('type') == 'object'
          and isinstance(out.get('required'), list)
          and all(isinstance(k, str) for k in out['required'])
          and {'index_id', 'index_version_id', 'embedding_space_id', 'returned_count', 'results'} <= set(out['required']),
          'SEARCH_TOOL_SCHEMA_MISMATCH', 502)
    result = {k: body[k] for k in ('tool_version_id', 'tool_id', 'version', 'revision', 'state',
                                  'kind', 'binding', 'timeout_ms', 'definition_sha256', 'input_schema', 'output_schema')}
    if saved:
        check(all(canonical(result[k]) == canonical(saved[k]) for k in result if k not in ('state', 'revision')),
              'SEARCH_TOOL_DEFINITION_CHANGED', 502)
    return result


def arguments_sha256(arguments):
    # Agent uses Go encoding/json: HTML and U+2028/U+2029 are escaped before hashing.
    raw = canonical(arguments)
    for char, escaped in (('<', '\\u003c'), ('>', '\\u003e'), ('&', '\\u0026'),
                          ('\u2028', '\\u2028'), ('\u2029', '\\u2029')):
        raw = raw.replace(char, escaped)
    return hashlib.sha256(raw.encode()).hexdigest()


def call_checked(body, plan, version, step):
    receipt, request = step['receipt'], step['body']
    check(isinstance(body, dict) and body.get('tool_call_id') == receipt['resource_id']
          and body.get('tool_version_id') == version['tool_version_id'] and body.get('mode') == 'test'
          and body.get('invocation_key') == step['request_key']
          and body.get('input_sha256') == arguments_sha256(request['arguments'])
          and body.get('definition_sha256') == version['definition_sha256'], 'SEARCH_TOOL_CALL_MISMATCH', 502)
    state = body.get('state')
    if state in ('accepted', 'running'):
        return 'waiting', None
    if state in ('failed', 'interrupted', 'unknown'):
        return 'failed', None
    check(state == 'succeeded' and not body.get('error'), 'SEARCH_TOOL_CALL_STATE_INVALID', 502)
    result = body.get('result')
    check(isinstance(result, dict) and result.get('completion') == 'completed'
          and isinstance(result.get('data'), dict) and isinstance(result.get('metadata'), dict),
          'SEARCH_TOOL_RESULT_INVALID', 502)
    data, meta, profile = result['data'], result['metadata'], plan['config']['embedding']
    actual = meta.get('embedding_profile')
    check(meta.get('adapter') == 'knowledge_search_v1'
          and meta.get('connection_version_id') == plan['selection']['knowledge_connection']['version_id']
          and meta.get('model_connection_version_id') == plan['selection']['model_connection']['version_id']
          and meta.get('index_version_id') == plan['index_version_id'] and isinstance(actual, dict)
          and all(type(actual.get(k)) is type(v) and actual.get(k) == v for k, v in profile.items())
          and actual.get('capability') == 'embedding' and actual.get('status') == 'ready'
          and type(actual.get('dimension')) is int and actual['dimension'] == 1024
          and actual.get('normalization') == 'l2' and actual.get('truncation') == 'reject',
          'SEARCH_TOOL_RESULT_IDENTITY_MISMATCH', 502)
    top, candidate = plan['selection']['top_k'], plan['selection']['candidate_k']
    hits = data.get('results')
    check(data.get('index_id') == plan['index_id'] and data.get('index_version_id') == plan['index_version_id']
          and data.get('embedding_space_id') == profile['embedding_space_id']
          and isinstance(hits, list) and integer(data.get('returned_count'), 0, top)
          and data['returned_count'] == len(hits) and type(data.get('top_k')) is int and data['top_k'] == top
          and type(data.get('candidate_k')) is int and data['candidate_k'] == candidate
          and integer(data.get('candidate_count'), len(hits), candidate) and data.get('filters') == {}
          and isinstance(data.get('rerank'), dict) and data['rerank'].get('requested') is False
          and data['rerank'].get('applied') is False and data['rerank'].get('status') == 'disabled',
          'SEARCH_TOOL_RESULT_INVALID', 502)
    documents = {(d['canonical_id'], d['source_document_id']) for d in plan['documents']}
    seen, refs = set(), []
    for rank, hit in enumerate(hits, 1):
        check(isinstance(hit, dict) and digest(hit.get('chunk_id')) and hit['chunk_id'] not in seen
              and uid(hit.get('canonical_id')) and uid(hit.get('source_document_id'))
              and (hit['canonical_id'], hit['source_document_id']) in documents
              and hit.get('kind') in ('text', 'table', 'code')
              and isinstance(hit.get('text'), str) and bool(hit['text'].strip())
              and type(hit.get('rank')) is int and hit['rank'] == rank
              and integer(hit.get('retrieval_rank'), 1, candidate)
              and type(hit.get('retrieval_score')) in (int, float) and math.isfinite(hit['retrieval_score'])
              and 0 <= hit['retrieval_score'] <= 1.000001
              and isinstance(hit.get('source_spans'), list) and bool(hit['source_spans']) and 'quality' in hit,
              'SEARCH_TOOL_RESULT_REFERENCE_MISMATCH', 502)
        seen.add(hit['chunk_id'])
        refs.append({k: hit[k] for k in ('chunk_id', 'canonical_id', 'source_document_id')})
    return 'verified', {'tool_call_id': body['tool_call_id'], 'returned_count': len(hits),
                        'result_sha256': fingerprint(result), 'references': refs,
                        'semantic_quality_verified': False}
