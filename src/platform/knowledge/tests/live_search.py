#!/usr/bin/env python3
"""Read an existing index, embed one raw question via Model, then test vector search.

Never builds, activates, deletes, or deploys. Saved summaries omit text/vectors.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

MAX_RESPONSE_BYTES = 2 << 20


class APIError(ValueError):
    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(f'HTTP {status}: {code}')


def strict_json(raw):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate response key')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('Nonfinite response number')

    return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=constant)


def api(base, path, method='GET', body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    request = Request(base.rstrip('/') + path, method=method, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=130 if path.endswith('/embeddings') else 70 if path.endswith('/search') and body and body.get('rerank', {}).get('enabled') else 25) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raw = error.read(8192)
        code = 'REQUEST_FAILED'
        try:
            content = strict_json(raw)
            field = content.get('error')
            message = field.get('message', '') if isinstance(field, dict) else field
            candidate = content.get('code') or (message.split(':', 1)[0] if isinstance(message, str) else '')
            if re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', candidate):
                code = candidate
        except (ValueError, AttributeError, TypeError):
            pass
        raise APIError(error.code, code) from None
    except URLError:
        raise ValueError('API connection failed; verify the configured Gateway addresses') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError('API response exceeds 2 MiB')
    result = strict_json(raw)
    if not isinstance(result, dict):
        raise ValueError('API response must be an object')
    return result


def valid_uuid(value):
    try:
        parsed = uuid.UUID(value)
        return parsed.int != 0 and str(parsed) == value
    except (ValueError, AttributeError, TypeError):
        return False


def count(value):
    return type(value) is int and value >= 0


def number(value):
    return type(value) in (float, int) and math.isfinite(value)


def profile_matches(current, pinned, deployment):
    return (
        isinstance(current, dict)
        and current.get('deployment_id') == deployment
        and current.get('embedding_space_id') == pinned.get('embedding_space_id')
        and current.get('dimension') == pinned.get('dimension') == 1024
        and current.get('capability') == 'embedding'
        and current.get('status') == 'ready'
        and current.get('normalization') == 'l2'
    )


def query_vector(result, profile, deployment):
    rows = result.get('data')
    if result.get('role') != 'query' or not profile_matches(result.get('profile'), profile, deployment) or not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('Model query receipt space/role/count differs')
    row = rows[0]
    vector = row.get('embedding')
    if row.get('id') != 'query-1' or type(row.get('index')) is not int or row['index'] != 0 or not isinstance(vector, list) or len(vector) != 1024 or not all(number(x) for x in vector):
        raise ValueError('Model query receipt ID/index/vector differs')
    norm = math.sqrt(sum(x*x for x in vector))
    if not math.isfinite(norm) or abs(norm-1) > 0.001:
        raise ValueError('Model query vector is not L2 normalized')
    tokens = row.get('input_tokens')
    if not count(tokens) or tokens < 1 or tokens > profile['max_input_tokens']:
        raise ValueError('Model input token receipt differs')
    return vector


def validate_result(result, version, body):
    if result.get('index_id') != version['index_id'] or result.get('index_version_id') != version['index_version_id'] or result.get('embedding_space_id') != body['embedding_space_id']:
        raise ValueError('Search result index/version/space differs')
    rows = result.get('results')
    candidates, returned = result.get('candidate_count'), result.get('returned_count')
    if not isinstance(rows, list) or not count(candidates) or not count(returned) or returned != len(rows) or not returned <= candidates <= body['candidate_k'] or returned != min(body['top_k'], candidates):
        raise ValueError('Search result counts differ')
    if result.get('top_k') != body['top_k'] or result.get('candidate_k') != body['candidate_k'] or result.get('filters') != body['filters']:
        raise ValueError('Search result limits or filters differ')
    on = body.get('rerank', {}).get('enabled') is True
    rerank = result.get('rerank')
    if on:
        expected = body['rerank']
        if not isinstance(rerank, dict) or rerank.get('requested') is not True or rerank.get('applied') is not bool(rows) or rerank.get('status') != ('applied' if rows else 'skipped_no_candidates') or rerank.get('deployment_id') != expected['deployment_id'] or rerank.get('reranker_profile_id') != expected['expected_reranker_profile_id']:
            raise ValueError('Requested rerank was not applied or its identity differs')
        rp = rerank.get('profile')
        if not valid_reranker(rp, expected['deployment_id']) or rp['reranker_profile_id'] != expected['expected_reranker_profile_id']:
            raise ValueError('Search reranker profile receipt differs')
    elif rerank != {'requested': False, 'applied': False, 'status': 'disabled'}:
        raise ValueError('Disabled search unexpectedly applied rerank')
    if result.get('score_semantics', {}).get('id') != 'opensearch_lucene_cosinesimil_v1':
        raise ValueError('Search score/rerank contract differs')
    if not rows and result.get('empty_reason') != 'no_matches':
        raise ValueError('Empty results must explain no_matches')
    documents = {d['canonical_id']: d['source_document_id'] for d in version['input_spec']['documents'] if d['canonical_id'] in version['canonical_ids']}
    filters = body['filters']
    seen = set()
    retrieval_ranks = set()
    previous = None
    for rank, hit in enumerate(rows, 1):
        identifier, text = hit.get('chunk_id'), hit.get('text')
        score = hit.get('retrieval_score')
        canonical, source = hit.get('canonical_id'), hit.get('source_document_id')
        retrieval_rank = hit.get('retrieval_rank')
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{64}', identifier) or identifier in seen or not number(score) or not 0 <= score <= 1.000001 or type(hit.get('rank')) is not int or hit['rank'] != rank or type(retrieval_rank) is not int or not 1 <= retrieval_rank <= candidates or retrieval_rank in retrieval_ranks or (not on and retrieval_rank != rank):
            raise ValueError('Search hit ID/rank/score differs')
        if canonical not in documents or source != documents[canonical] or not isinstance(text, str) or not text or hashlib.sha256(text.encode()).hexdigest() != hit.get('content_sha256'):
            raise ValueError('Search hit source or text integrity differs')
        for field, value in (('canonical_ids', canonical), ('source_document_ids', source), ('kinds', hit.get('kind'))):
            if field in filters and value not in filters[field]:
                raise ValueError('Search result escaped requested filters')
        if hit.get('kind') not in ('text', 'table', 'code') or 'quality' not in hit or not hit.get('source_spans'):
            raise ValueError('Search hit provenance is missing')
        for span in hit['source_spans']:
            start, end = span.get('start'), span.get('end')
            a, b = span.get('chunk_start'), span.get('chunk_end')
            if not all(count(x) for x in (start, end, a, b)) or not start < end or not a < b <= len(text) or end-start != b-a or not span.get('block_id'):
                raise ValueError('Search hit source ranges differ')
        if on:
            rr_score, tokens = hit.get('rerank_score'), hit.get('rerank_input_tokens')
            if not number(rr_score) or type(tokens) is not int or not 1 <= tokens <= rp['max_input_tokens']:
                raise ValueError('Rerank score or token receipt is invalid')
            ordering = (-rr_score, retrieval_rank)
        else:
            if 'rerank_score' in hit or 'rerank_input_tokens' in hit:
                raise ValueError('Disabled rerank must not return Model scores')
            ordering = (-score, identifier)
        if previous is not None and ordering < previous:
            raise ValueError('Search result order differs')
        previous = ordering
        seen.add(identifier)
        retrieval_ranks.add(retrieval_rank)
    return rows


def valid_reranker(p, deployment):
    return (isinstance(p, dict) and p.get('deployment_id') == deployment and valid_uuid(p.get('model_version_id'))
            and isinstance(p.get('reranker_profile_id'), str) and re.fullmatch('rp_[a-f0-9]{64}', p['reranker_profile_id'])
            and p.get('capability') == 'reranker' and p.get('status') == 'ready'
            and p.get('score_kind') == 'logit' and p.get('score_direction') == 'higher_is_better' and p.get('truncation') == 'reject'
            and type(p.get('max_candidates')) is int and 1 <= p['max_candidates'] <= 100
            and type(p.get('max_input_tokens')) is int and 1 <= p['max_input_tokens'] <= 32768
            and isinstance(p.get('runtime_container_id'), str) and re.fullmatch('[a-f0-9]{64}', p['runtime_container_id']))


def run(args, call=api):
    if not valid_uuid(args.index_id) or (args.index_version_id and not valid_uuid(args.index_version_id)) or not args.question.strip() or not 1 <= args.top_k <= 50:
        raise ValueError('Valid index/version IDs, a question and top_k 1..50 are required')
    started = time.monotonic()
    index = call(args.knowledge_url, '/api/indexes/' + args.index_id)
    version_id = args.index_version_id or index.get('active_version_id')
    if index.get('index_id') != args.index_id or index.get('state') != 'active' or not valid_uuid(version_id):
        raise ValueError('Index is unavailable or has no selected version')
    version = call(args.knowledge_url, '/api/index-versions/' + version_id)
    if version.get('index_id') != args.index_id or version.get('index_version_id') != version_id or version.get('state') != 'ready' or version.get('reference_state') != 'held' or not count(version.get('chunk_count')) or version['chunk_count'] < 1 or version.get('indexed_count') != version['chunk_count']:
        raise ValueError('Selected index version is not ready and complete')
    profile = version['embedding_profile']
    deployment = args.deployment_id or profile['deployment_id']
    if not valid_uuid(deployment):
        raise ValueError('Embedding deployment must be a UUID')
    current = call(args.model_url, '/api/deployments/' + deployment + '/embedding-profile')
    if not profile_matches(current, profile, deployment):
        raise ValueError('Current Model profile does not match the pinned embedding space')
    embedded = call(args.model_url, '/api/deployments/' + deployment + '/embeddings', 'POST', {
        'expected_embedding_space_id': profile['embedding_space_id'], 'role': 'query',
        'inputs': [{'id': 'query-1', 'text': args.question}],
    })
    vector = query_vector(embedded, current, deployment)
    body = {'index_version_id': version_id, 'embedding_space_id': profile['embedding_space_id'], 'query_embedding': vector, 'top_k': args.top_k, 'candidate_k': args.top_k, 'filters': {}}
    path = '/api/indexes/' + args.index_id + '/search'
    result = call(args.knowledge_url, path, 'POST', body)
    rows = validate_result(result, version, body)
    if not rows:
        raise ValueError('Unfiltered search of the nonempty ready version returned no candidates')
    # Reuse the one query vector for every filter and rejection probe.
    selected = rows[0]
    filtered_body = {**body, 'filters': {'canonical_ids': [selected['canonical_id']], 'kinds': [selected['kind']]}}
    filtered = call(args.knowledge_url, path, 'POST', filtered_body)
    filtered_rows = validate_result(filtered, version, filtered_body)
    if not filtered_rows:
        raise ValueError('Filter excluding no selected hit unexpectedly returned no candidates')
    checks = ['query_document_space_match', 'explicit_version', 'result_integrity', 'candidate_filter', 'score_order']
    if not args.skip_rejections:
        wrong_space = 'es_' + ('f' if body['embedding_space_id'] != 'es_'+'f'*64 else 'a')*64
        probes = [
            ({**body, 'embedding_space_id': wrong_space}, 409, 'SEARCH_SPACE_MISMATCH'),
            ({**body, 'query_embedding': vector[:-1]}, 400, 'SEARCH_DIMENSION_MISMATCH'),
            ({**body, 'filters': {'canonical_ids': []}}, 400, 'SEARCH_INVALID_FILTER'),
            ({**body, 'filters': {'unsupported': ['all']}}, 400, 'SEARCH_INVALID_REQUEST'),
        ]
        for request, expected_status, expected_code in probes:
            try:
                call(args.knowledge_url, path, 'POST', request)
            except APIError as error:
                if error.status != expected_status or error.code != expected_code:
                    raise ValueError('Negative probe returned an unexpected status/code') from None
            else:
                raise ValueError('Search accepted an incompatible or invalid request')
        checks.extend(['space_mismatch_rejected', 'dimension_mismatch_rejected', 'empty_filter_rejected', 'unknown_filter_rejected'])
    return {
        'status': 'passed', 'scope': 'API integration; retrieval quality is not evaluated',
        'index_id': args.index_id, 'index_version_id': version_id,
        'embedding_space_id': profile['embedding_space_id'], 'dimension': len(vector),
        'deployment_id': deployment, 'query_runtime_container_id': embedded['profile'].get('runtime_container_id'),
        'indexed_chunks': version['indexed_count'], 'candidate_count': result['candidate_count'],
        'returned_count': result['returned_count'], 'filtered_count': filtered['returned_count'],
        'query_embedding_calls': 1, 'index_mutations': 0,
        'top_chunk_ids': [hit['chunk_id'] for hit in rows],
        'canonical_ids': sorted({hit['canonical_id'] for hit in rows}),
        'quality_states': sorted({str(hit['quality'].get('state', 'unknown')) for hit in rows if isinstance(hit['quality'], dict)}),
        'checks': checks, 'elapsed_seconds': round(time.monotonic()-started, 3),
    }


def save_summary(path, summary):
    path = path.expanduser()
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--knowledge-url', default='http://127.0.0.1:15100')
    parser.add_argument('--model-url', default='http://127.0.0.1:13000')
    parser.add_argument('--index-id', required=True)
    parser.add_argument('--index-version-id', default='')
    parser.add_argument('--deployment-id', default='', help='Optional explicitly selected deployment in the same embedding space')
    parser.add_argument('--question', required=True, help='Raw text; do not manually prepend the query prefix')
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--skip-rejections', action='store_true')
    parser.add_argument('--summary-file', type=Path)
    args = parser.parse_args()
    summary = run(args)
    if args.summary_file:
        save_summary(args.summary_file, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        if isinstance(error, (KeyError, TypeError)):
            raise SystemExit('FAIL: response does not match the documented contract') from None
        raise SystemExit('FAIL: ' + str(error)) from None
