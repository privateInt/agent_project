#!/usr/bin/env python3
"""Check existing Knowledge search off/on; do not deploy, index or stop models."""
import argparse
import copy
import json
from pathlib import Path
import time

from live_search import api, APIError, valid_uuid, valid_reranker, profile_matches, query_vector, validate_result, save_summary


def run(args, call=api):
    if not valid_uuid(args.index_id) or not valid_uuid(args.reranker_deployment_id) or (args.index_version_id and not valid_uuid(args.index_version_id)) or not args.question.strip() or len(args.question.encode()) > 8192 or not 1 <= args.top_k <= 50 or not args.top_k <= args.candidate_k <= 50:
        raise ValueError('Valid IDs/question and 1 <= top_k <= candidate_k <= 50 required for full candidate comparison')
    started = time.monotonic()
    index = call(args.knowledge_url, '/api/indexes/' + args.index_id)
    vid = args.index_version_id or index.get('active_version_id')
    if index.get('index_id') != args.index_id or index.get('state') != 'active' or not valid_uuid(vid):
        raise ValueError('Selected index or version is unavailable')
    version = call(args.knowledge_url, '/api/index-versions/' + vid)
    if version.get('index_id') != args.index_id or version.get('index_version_id') != vid or version.get('state') != 'ready' or version.get('reference_state') != 'held':
        raise ValueError('Selected version is not ready with held references')
    pinned = version['embedding_profile']
    dep = args.embedding_deployment_id or pinned['deployment_id']
    if not valid_uuid(dep):
        raise ValueError('Invalid embedding deployment ID')
    current = call(args.model_url, '/api/deployments/' + dep + '/embedding-profile')
    if not profile_matches(current, pinned, dep):
        raise ValueError('Current embedding model space differs from index')
    rp = call(args.model_url, '/api/deployments/' + args.reranker_deployment_id + '/reranker-profile')
    if not valid_reranker(rp, args.reranker_deployment_id) or args.candidate_k > rp['max_candidates']:
        raise ValueError('Selected reranker profile or candidate limit is unavailable')
    embedded = call(args.model_url, '/api/deployments/' + dep + '/embeddings', 'POST', {
        'expected_embedding_space_id': pinned['embedding_space_id'], 'role': 'query', 'inputs': [{'id': 'query-1', 'text': args.question}]})
    vector = query_vector(embedded, current, dep)
    path = '/api/indexes/' + args.index_id + '/search'
    # Return all vector candidates so the reranked subset can be checked against
    # real source text/provenance, while reusing exactly one question embedding.
    body = {'index_version_id': vid, 'embedding_space_id': pinned['embedding_space_id'], 'query_embedding': vector,
            'top_k': args.candidate_k, 'candidate_k': args.candidate_k, 'filters': {}, 'rerank': {'enabled': False}}
    off = call(args.knowledge_url, path, 'POST', body)
    vector_rows = validate_result(off, version, body)
    if not vector_rows:
        raise ValueError('No vector candidates to check rerank against')
    enabled = {**body, 'top_k': args.top_k, 'query_text': args.question, 'rerank': {'enabled': True,
               'deployment_id': rp['deployment_id'], 'expected_reranker_profile_id': rp['reranker_profile_id']}}
    on = call(args.knowledge_url, path, 'POST', enabled)
    rows = validate_result(on, version, enabled)
    original = {h['chunk_id']: h for h in vector_rows}
    if on['candidate_count'] != off['candidate_count']:
        raise ValueError('Candidate sets changed between searches; result is inconclusive')
    for hit in rows:
        before = original.get(hit['chunk_id'])
        if before is None or any(before.get(k) != hit.get(k) for k in ('text','content_sha256','source_document_id','canonical_id','source_spans','quality','structure','retrieval_rank','retrieval_score')):
            raise ValueError('Candidate provenance or vector order changed between searches; result is inconclusive')
    checks = ['one_query_embedding', 'explicit_index_version', 'rerank_disabled_receipt', 'rerank_applied_receipt',
              'candidate_identity_and_provenance', 'finite_logits_and_tokens', 'rerank_order_and_ties', 'separate_scores']
    wrong = 'rp_' + ('f' if rp['reranker_profile_id'] != 'rp_'+'f'*64 else 'a')*64
    probes = [(dict(enabled, query_text=' '), 400, 'SEARCH_INVALID_REQUEST'),
              (dict(enabled, rerank={**enabled['rerank'], 'expected_reranker_profile_id': wrong}), 409, 'RERANK_PROFILE_MISMATCH')]
    for request, wanted_status, wanted_code in probes:
        try:
            call(args.knowledge_url, path, 'POST', request)
        except APIError as error:
            if (error.status, error.code) != (wanted_status, wanted_code):
                raise ValueError('Unexpected status/code in negative rerank probe') from None
        else:
            raise ValueError('Search accepted invalid rerank input or profile')
    checks += ['blank_query_rejected', 'profile_mismatch_rejected']
    return {'status': 'passed', 'scope': 'Knowledge optional rerank API integration; retrieval quality is not evaluated',
            'index_id': args.index_id, 'index_version_id': vid, 'embedding_space_id': pinned['embedding_space_id'],
            'embedding_deployment_id': dep, 'reranker_profile': copy.deepcopy(on['rerank']['profile']),
            'candidate_count': on['candidate_count'], 'returned_count': on['returned_count'],
            'vector_top_chunk_ids': [h['chunk_id'] for h in vector_rows[:args.top_k]], 'reranked_top_chunk_ids': [h['chunk_id'] for h in rows],
            'query_embedding_calls': 1, 'valid_rerank_search_requests': 1, 'lifecycle_changes': 0,
            'scores': [{'chunk_id': h['chunk_id'], 'retrieval_rank': h['retrieval_rank'], 'rank': h['rank'],
                        'retrieval_score': h['retrieval_score'], 'rerank_score': h['rerank_score'], 'input_tokens': h['rerank_input_tokens']} for h in rows],
            'timings': on.get('timings'), 'checks': checks, 'elapsed_seconds': round(time.monotonic()-started, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--knowledge-url', default='http://127.0.0.1:15100')
    parser.add_argument('--model-url', default='http://127.0.0.1:13000')
    parser.add_argument('--index-id', required=True)
    parser.add_argument('--index-version-id', default='')
    parser.add_argument('--embedding-deployment-id', default='')
    parser.add_argument('--reranker-deployment-id', required=True)
    parser.add_argument('--question', required=True)
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--candidate-k', type=int, default=20)
    parser.add_argument('--summary-file', type=Path)
    args = parser.parse_args(); result = run(args)
    if args.summary_file:
        save_summary(args.summary_file, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        if isinstance(error, (KeyError, TypeError)):
            raise SystemExit('FAIL: response does not match the documented contract') from None
        raise SystemExit('FAIL: ' + str(error)) from None
