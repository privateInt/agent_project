"""Pure validation shared by the explicit console check and live API tool."""
import hashlib
import json
import math
import re
import uuid

PROFILE = 'ko-reranker-v1'
REPOSITORY = 'Dongjin-kr/ko-reranker'
PAIR_POLICY = 'xlmr-pair-special4-no-prefix-v1'


def identity(profile):
    fields = ['reranker-identity-v1', profile['profile_id'], profile['source_manifest_sha256'],
              profile['tokenizer_sha256'], profile['runtime_image_id'], profile['score_kind'],
              profile['score_direction'], profile['pair_policy'], profile['max_input_tokens'],
              profile['truncation'], 'dtype-auto', 'activation-disabled']
    return 'rp_' + hashlib.sha256(json.dumps(fields, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def validate_profile(profile, deployment_id):
    if not isinstance(profile, dict):
        raise ValueError('Invalid reranker profile')
    expected = {'deployment_id': deployment_id, 'profile_id': PROFILE, 'serving_profile': PROFILE,
                'repo_id': REPOSITORY, 'capability': 'reranker', 'status': 'ready',
                'score_kind': 'logit', 'score_direction': 'higher_is_better', 'pair_policy': PAIR_POLICY,
                'max_input_tokens': 512, 'max_candidates': 100, 'max_input_bytes': 131072,
                'truncation': 'reject', 'served_model_name': 'deployment-' + deployment_id}
    for key, value in expected.items():
        if type(profile.get(key)) is not type(value) or profile[key] != value:
            raise ValueError('Unsupported reranker profile: ' + key)
    for key in ('source_manifest_sha256', 'tokenizer_sha256', 'runtime_container_id'):
        if not isinstance(profile.get(key), str) or not re.fullmatch('[a-f0-9]{64}', profile[key]):
            raise ValueError('Invalid profile identity: ' + key)
    if not isinstance(profile.get('runtime_image_id'), str) or not re.fullmatch('sha256:[a-f0-9]{64}', profile['runtime_image_id']):
        raise ValueError('Invalid runtime image')
    if not isinstance(profile.get('model_version_id'), str) or str(uuid.UUID(profile['model_version_id'])) != profile['model_version_id']:
        raise ValueError('Invalid model version')
    if profile.get('registered_model_type') not in ('reranker', 'llm') or not isinstance(profile.get('resolved_revision'), str) or not profile['resolved_revision']:
        raise ValueError('Missing model provenance')
    if profile.get('reranker_profile_id') != identity(profile):
        raise ValueError('Reranker profile identity mismatch')
    return profile


def validate_result(result, profile, ids):
    if not isinstance(result, dict) or result.get('profile') != profile:
        raise ValueError('Response profile changed')
    rows = result.get('data')
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise ValueError('Response count mismatch')
    summary = []
    for index, (row, candidate_id) in enumerate(zip(rows, ids)):
        if not isinstance(row, dict) or row.get('id') != candidate_id or type(row.get('index')) is not int or row['index'] != index:
            raise ValueError('Response ID/index mismatch')
        score, tokens = row.get('score'), row.get('input_tokens')
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError('Invalid scalar score')
        if type(tokens) is not int or not 6 <= tokens <= profile['max_input_tokens']:
            raise ValueError('Invalid pair token count')
        summary.append({'id': candidate_id, 'index': index, 'score': score, 'input_tokens': tokens})
    return summary
