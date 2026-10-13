#!/usr/bin/env python3
"""Use an existing deployment through the official Model API; never deploy/stop it."""
import argparse
import json
import math
import sys
import uuid
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.parse import urlsplit


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_result(result, profile, role, ids):
    actual = result.get('profile') or {}
    for key in ('deployment_id', 'embedding_space_id', 'source_manifest_sha256', 'tokenizer_sha256',
                'runtime_image_id', 'runtime_container_id', 'model_version_id', 'dimension'):
        if actual.get(key) != profile.get(key):
            raise ValueError('Response profile changed: ' + key)
    rows = result.get('data') or []
    if result.get('role') != role or len(rows) != len(ids):
        raise ValueError('Response role/count mismatch')
    summary = []
    for index, (row, input_id) in enumerate(zip(rows, ids)):
        if row.get('id') != input_id or row.get('index') != index:
            raise ValueError('Response ID/index mismatch')
        vector = row.get('embedding') or []
        if len(vector) != 1024 or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vector):
            raise ValueError('Invalid vector dimension or values')
        norm = math.sqrt(sum(x*x for x in vector))
        tokens = row.get('input_tokens')
        if not math.isfinite(norm) or abs(norm-1) > 0.001 or not isinstance(tokens, int) or isinstance(tokens, bool) or not 0 < tokens <= profile['max_input_tokens']:
            raise ValueError('Invalid normalization/token count')
        summary.append({'id': input_id, 'dimension': len(vector), 'finite': True, 'l2_norm': round(norm, 6), 'input_tokens': tokens})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:13000')
    parser.add_argument('--deployment-id', default='44247297-5184-4a36-9225-7791c80d3add')
    args = parser.parse_args()
    dep = str(uuid.UUID(args.deployment_id))
    base = args.base_url.rstrip('/')
    url = urlsplit(base)
    if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password or url.path or url.query or url.fragment:
        raise ValueError('base-url must be an HTTP(S) origin')
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(suffix, body=None, expected=200, reason=None):
        raw = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        request = Request(base + '/api/deployments/' + dep + suffix, data=raw, headers={'Content-Type': 'application/json'})
        try:
            response = opener.open(request, timeout=135)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(2*1024*1024+1)
            if len(raw) > 2*1024*1024:
                raise ValueError('Oversized API response')
            result = json.loads(raw)
            if response.code != expected or (reason and result.get('code') != reason):
                raise ValueError(f"API {suffix}: expected {expected}/{reason or 'success'}, got {response.code}/{result.get('code', '')}")
            return result

    profile = call('/embedding-profile')
    if (profile.get('deployment_id') != dep or profile.get('capability') != 'embedding'
            or profile.get('status') != 'ready' or profile.get('dimension') != 1024
            or profile.get('query_prefix') != 'query: ' or profile.get('document_prefix') != ''
            or profile.get('truncation') != 'reject' or not 128 <= profile.get('max_input_tokens', 0) <= 8192):
        raise ValueError('Unexpected embedding profile')
    base_request = {'expected_embedding_space_id': profile['embedding_space_id'], 'role': 'query',
                    'inputs': [{'id': 'q1', 'text': '대한민국의 수도는 어디인가?'}]}
    wrong = dict(base_request, expected_embedding_space_id='es_' + ('0' if profile['embedding_space_id'] != 'es_' + '0'*64 else '1')*64)
    call('/embeddings', wrong, 409, 'EMBEDDING_SPACE_MISMATCH')
    call('/embeddings', dict(base_request, inputs=[{'id': 'long', 'text': 'hello '*(profile['max_input_tokens']+1)}]), 400, 'EMBEDDING_INPUT_TOO_LONG')
    query = call('/embeddings', base_request)
    documents = [{'id': 'd1', 'text': '대한민국의 수도는 서울이다.'}, {'id': 'd2', 'text': '프랑스의 수도는 파리이다.'}]
    document = call('/embeddings', dict(base_request, role='document', inputs=documents))
    summary = {'status': 'passed', 'scope': 'existing runtime official API; no lifecycle changes or retrieval quality claim',
               'profile': profile, 'query': validate_result(query, profile, 'query', ['q1']),
               'document': validate_result(document, profile, 'document', ['d1', 'd2']),
               'space_mismatch_rejected': True, 'overlong_input_rejected': True}
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
