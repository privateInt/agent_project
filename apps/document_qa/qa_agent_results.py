"""Fixed-QA identity and original-source gates. Success does not prove answer quality."""
import math
import re

from . import readiness
from .contracts import digest, fingerprint, integer, uid
from .intake import check
from .search_tool_results import arguments_sha256, call_checked
from .workflows import canonical

DEFAULTS = {'prompt': '선택한 자료에 근거해 질문에 한국어로 명확하게 답하세요. 자료에 없는 내용은 추측하지 마세요.',
            'context_max_chars': 8000, 'max_tokens': 512, 'temperature': 0.0, 'timeout_ms': 300000}
CHAT_PROFILE = 'model-platform-chat-v1'
NO_EVIDENCE = '선택한 자료에서 질문에 답할 충분한 근거를 찾지 못했습니다.'
MAX_RESULT_BYTES = 512 << 10
MODEL_FIELDS = {'connection_version_id', 'deployment_id', 'model_version_id'}
DEFINITION_FIELDS = {'template_id', 'template_version', 'tool_version_id', 'model'} | set(DEFAULTS)


def question_checked(value):
    check(isinstance(value, str) and bool(value.strip()) and '\0' not in value, 'QUESTION_INVALID')
    try:
        check(len(value.encode('utf-8')) <= 8192, 'QUESTION_TOO_LARGE', 413)
    except UnicodeError:
        check(False, 'QUESTION_INVALID')
    return value


def selection_checked(value):
    required = {'chat_connection', 'chat_deployment_id', 'chat_model_version_id'}
    check(isinstance(value, dict) and required <= set(value) <= required | set(DEFAULTS), 'QA_AGENT_SELECTION_INVALID', 409)
    conn = value['chat_connection']
    check(isinstance(conn, dict) and set(conn) == {'version_id', 'base_url'} and uid(conn['version_id'])
          and uid(value['chat_deployment_id']) and uid(value['chat_model_version_id']), 'QA_AGENT_SELECTION_INVALID', 409)
    result = {**DEFAULTS, **value, 'chat_connection': {'version_id': conn['version_id'],
                                                        'base_url': readiness.origin(conn['base_url'])}}
    definition_checked({'template_id': 'knowledge_qa_v1', 'template_version': 1,
                        'tool_version_id': value['chat_deployment_id'],
                        'model': {'connection_version_id': conn['version_id'],
                                  'deployment_id': value['chat_deployment_id'], 'model_version_id': value['chat_model_version_id']},
                        **{k: result[k] for k in DEFAULTS}})
    return result


def definition_checked(body):
    check(isinstance(body, dict) and set(body) == DEFINITION_FIELDS
          and body['template_id'] == 'knowledge_qa_v1' and type(body['template_version']) is int
          and body['template_version'] == 1 and uid(body['tool_version_id'])
          and isinstance(body['model'], dict) and set(body['model']) == MODEL_FIELDS
          and all(uid(v) for v in body['model'].values()), 'QA_AGENT_DEFINITION_INVALID', 409)
    prompt = body['prompt']
    check(isinstance(prompt, str) and bool(prompt.strip()) and '\0' not in prompt
          and len(prompt.encode()) <= 4000 and integer(body['context_max_chars'], 256, 12000)
          and integer(body['max_tokens'], 1, 512) and type(body['temperature']) in (int, float)
          and math.isfinite(body['temperature']) and 0 <= body['temperature'] <= 1.5
          and integer(body['timeout_ms'], 1000, 600000), 'QA_AGENT_DEFINITION_INVALID', 409)
    return body


def definition(plan):
    s = plan['selection']
    return {'template_id': 'knowledge_qa_v1', 'template_version': 1,
            'tool_version_id': plan['tool']['tool_version_id'],
            'model': {'connection_version_id': s['chat_connection']['version_id'],
                      'deployment_id': s['chat_deployment_id'], 'model_version_id': s['chat_model_version_id']},
            **{k: s[k] for k in DEFAULTS}}


def model_checked(value, plan):
    expected = definition(plan)['model']
    check(isinstance(value, dict) and value.get('model') == expected and isinstance(value.get('identity'), dict),
          'QA_AGENT_MODEL_MISMATCH', 502)
    model = value['identity']
    check(model.get('deployment_id') == expected['deployment_id'] and model.get('model_version_id') == expected['model_version_id']
          and uid(model.get('model_id')) and model.get('served_model_name') == 'deployment-' + expected['deployment_id']
          and integer(model.get('max_model_len'), plan['selection']['max_tokens'] + 1, 1 << 30)
          and isinstance(model.get('repo_id'), str) and bool(model['repo_id'])
          and model['repo_id'] not in readiness.NON_CHAT, 'QA_AGENT_MODEL_MISMATCH', 502)
    # This is our API contract label; Agent does not advertise a chat serving profile_id.
    return {'model': dict(expected), 'chat_profile_id': CHAT_PROFILE,
            'model_id': model['model_id'], 'repo_id': model['repo_id'], 'max_model_len': model['max_model_len']}


def agent_checked(body, expected, identifier=None):
    check(isinstance(body, dict) and uid(body.get('agent_id')) and (identifier is None or body['agent_id'] == identifier)
          and body.get('display_name') == expected['display_name'] and body.get('description') == expected['description']
          and body.get('enabled') is True and integer(body.get('revision'), 1), 'QA_AGENT_RESOURCE_MISMATCH', 502)
    return {k: body[k] for k in ('agent_id', 'display_name', 'description', 'enabled', 'revision')}


def version_checked(body, plan, agent_id, version_id=None, saved=None):
    expected = definition(plan)
    check(isinstance(body, dict) and uid(body.get('agent_version_id'))
          and (version_id is None or body['agent_version_id'] == version_id) and body.get('agent_id') == agent_id
          and integer(body.get('version'), 1, 100) and integer(body.get('revision'), 1)
          and body.get('state') in ('draft', 'published') and digest(body.get('definition_sha256'))
          and all(type(body.get(k)) is type(v) and body.get(k) == v for k, v in expected.items()
                  if k != 'temperature')
          and type(body.get('temperature')) in (int, float) and body['temperature'] == expected['temperature'],
          'QA_AGENT_VERSION_MISMATCH', 502)
    inp, out = body.get('input_schema'), body.get('output_schema')
    check(isinstance(inp, dict) and inp.get('type') == 'object' and inp.get('required') == ['question']
          and inp.get('additionalProperties') is False and isinstance(inp.get('properties'), dict)
          and isinstance(inp['properties'].get('question'), dict) and inp['properties']['question'].get('type') == 'string'
          and isinstance(out, dict) and out.get('type') == 'object' and isinstance(out.get('required'), list)
          and all(isinstance(k, str) for k in out['required'])
          and {'answer', 'outcome', 'citations', 'warnings', 'answer_truncated', 'tool_call_id', 'identity'} <= set(out['required']),
          'QA_AGENT_SCHEMA_MISMATCH', 502)
    fields = {'agent_version_id', 'agent_id', 'version', 'revision', 'state', 'definition_sha256',
              'input_schema', 'output_schema'} | DEFINITION_FIELDS
    result = {k: body[k] for k in fields}
    if saved:
        check(all(canonical(result[k]) == canonical(saved[k]) for k in fields - {'state', 'revision'}),
              'QA_AGENT_DEFINITION_CHANGED', 502)
    return result


def run_checked(body, plan, version, step, child=None):
    receipt, request = step['receipt'], step['body']
    check(isinstance(body, dict) and body.get('agent_run_id') == receipt['resource_id']
          and body.get('agent_version_id') == version['agent_version_id'] and body.get('mode') == request['mode']
          and body.get('input') == request['input'] and body.get('input_sha256') == arguments_sha256(request['input'])
          and body.get('invocation_key') == step['request_key']
          and body.get('definition_sha256') == version['definition_sha256'] and body.get('caller_context') == {},
          'QA_AGENT_RUN_MISMATCH', 502)
    state = body.get('state')
    if state in ('accepted', 'running'):
        check(not body.get('result') and not body.get('error'), 'QA_AGENT_RUN_STATE_INVALID', 502)
        return 'waiting', None
    if state in ('failed', 'interrupted'):
        error = body.get('error')
        check(not body.get('result') and isinstance(error, dict) and isinstance(error.get('code'), str)
              and re.fullmatch('[A-Z][A-Z0-9_]{0,99}', error['code']), 'QA_AGENT_RUN_STATE_INVALID', 502)
        return 'failed', {'agent_run_id': body['agent_run_id'], 'error_code': error['code']}
    check(state == 'succeeded' and not body.get('error'), 'QA_AGENT_RUN_STATE_INVALID', 502)
    result = body.get('result')
    check(isinstance(result, dict) and len(canonical(result).encode()) <= MAX_RESULT_BYTES
          and uid(result.get('tool_call_id')) and body.get('tool_call_id') == result['tool_call_id']
          and isinstance(child, dict) and child.get('tool_call_id') == result['tool_call_id']
          and child.get('parent_agent_run_id') == body['agent_run_id'] and child.get('parent_step_id') == 'search'
          and child.get('mode') == 'run' and child.get('caller_context') == {} and uid(child.get('invocation_key')),
          'QA_AGENT_CHILD_MISMATCH', 502)
    # Reuse Q06a's full search/profile/document gates after checking child ownership.
    child_step = {'receipt': {'resource_id': child['tool_call_id']},
                  'body': {'arguments': {'query': request['input']['question']}}, 'request_key': child['invocation_key']}
    child_state, _ = call_checked({**child, 'mode': 'test'}, plan['source'], plan['tool'], child_step)
    check(child_state == 'verified', 'QA_AGENT_SEARCH_NOT_COMPLETE', 502)
    identity = result.get('identity')
    check(isinstance(identity, dict) and set(identity) == {'template_id', 'template_version', 'tool_version_id', 'model', 'knowledge'}
          and identity['template_id'] == 'knowledge_qa_v1' and type(identity['template_version']) is int
          and identity['template_version'] == 1 and identity['tool_version_id'] == version['tool_version_id']
          and identity['knowledge'] == child['result']['metadata'], 'QA_AGENT_RESULT_IDENTITY_MISMATCH', 502)
    hits = child['result']['data']['results']
    lookup = {h['chunk_id']: h for h in hits}
    selected, excluded = result.get('selected_chunk_ids'), result.get('excluded_sources')
    check(isinstance(selected, list) and all(isinstance(k, str) and k in lookup for k in selected)
          and len(set(selected)) == len(selected) and selected == [h['chunk_id'] for h in hits if h['chunk_id'] in selected]
          and isinstance(excluded, list), 'QA_AGENT_CONTEXT_MISMATCH', 502)
    excluded_ids = []
    for item in excluded:
        check(isinstance(item, dict) and set(item) == {'chunk_id', 'reason'} and item['reason'] == 'context_budget'
              and isinstance(item['chunk_id'], str) and item['chunk_id'] in lookup, 'QA_AGENT_CONTEXT_MISMATCH', 502)
        excluded_ids.append(item['chunk_id'])
    check(len(set(excluded_ids)) == len(excluded_ids) and not set(selected) & set(excluded_ids)
          and set(selected) | set(excluded_ids) == set(lookup) and (not hits or selected)
          and sum(len(lookup[k]['text']) for k in selected) <= version['context_max_chars'], 'QA_AGENT_CONTEXT_MISMATCH', 502)
    usage = result.get('usage')
    if selected:
        model_checked({'model': version['model'], 'identity': identity['model']}, plan)
        check(isinstance(usage, dict) and set(usage) == {'prompt_tokens', 'completion_tokens', 'total_tokens'}
              and all(integer(v, 0, 10000000) for v in usage.values())
              and usage['completion_tokens'] <= version['max_tokens']
              and usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'], 'QA_AGENT_USAGE_INVALID', 502)
    else:
        check(identity['model'] == {} and usage == {}, 'QA_AGENT_NO_EVIDENCE_INVALID', 502)
    answer, citations, warnings = result.get('answer'), result.get('citations'), result.get('warnings')
    check(isinstance(answer, str) and bool(answer.strip()) and '\0' not in answer and len(answer.encode()) <= 65536
          and isinstance(citations, list) and len(citations) <= 50 and isinstance(warnings, list)
          and all(isinstance(w, str) and w in ('CONTEXT_SOURCES_EXCLUDED', 'ANSWER_TRUNCATED', 'CHECK_SOURCE_QUALITY') for w in warnings)
          and len(warnings) == len(set(warnings)) and type(result.get('answer_truncated')) is bool
          and result['answer_truncated'] == ('ANSWER_TRUNCATED' in warnings)
          and bool(excluded) == ('CONTEXT_SOURCES_EXCLUDED' in warnings), 'QA_AGENT_RESULT_INVALID', 502)
    refs = re.findall(r'\[S([^\]\r\n]*)\]', answer)
    allowed = {'S' + str(i): lookup[k] for i, k in enumerate(selected, 1)}
    check(all('S' + r in allowed for r in refs), 'QA_AGENT_CITATION_INVALID', 502)
    expected_refs = [r for r in allowed if r in {'S' + n for n in refs}]
    actual_refs = []
    for citation in citations:
        check(isinstance(citation, dict) and set(citation) == {'citation_id', 'source'}
              and isinstance(citation['citation_id'], str) and citation['citation_id'] in allowed
              and citation['source'] == allowed[citation['citation_id']], 'QA_AGENT_CITATION_INVALID', 502)
        actual_refs.append(citation['citation_id'])
    check(actual_refs == expected_refs, 'QA_AGENT_CITATION_INVALID', 502)
    if result.get('outcome') == 'no_evidence':
        check(not refs and not citations and not result['answer_truncated'] and answer == NO_EVIDENCE,
              'QA_AGENT_NO_EVIDENCE_INVALID', 502)
    else:
        check(result.get('outcome') == 'answered' and bool(refs) and bool(citations), 'QA_AGENT_CITATION_INVALID', 502)
    return 'verified', {'agent_run_id': body['agent_run_id'], 'result_sha256': fingerprint(result),
                        'result': result, 'semantic_quality_verified': False}
