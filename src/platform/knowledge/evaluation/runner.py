"""Evaluate existing versions via public APIs with explicit failure accounting."""
import copy
from datetime import datetime, timezone
import math
import re
import time

from .common import (ENGINE, require, fields, label, valid_uuid, sha, pins, version_ready, identity, encoded, indexing_config,
                     profile_matches, query_vector, validate_result, valid_reranker, APIError, number)
from .dataset import validate_dataset
from .diagnostics import CandidateMismatch, REPORT_DETAIL_LIMIT, candidate_equal
from .metrics import assign, metrics, summarize, NAMES


def validate_variants(config):
    fields(config, ('schema_version', 'candidate_k', 'top_k', 'variants'))
    require(config['schema_version'] == 'retrieval-variants-v1'
            and type(config['candidate_k']) is int and type(config['top_k']) is int
            and 1 <= config['top_k'] <= config['candidate_k'] <= 50, 'Require 1 <= top_k <= candidate_k <= 50')
    variants = config['variants']
    require(isinstance(variants, list) and 1 <= len(variants) <= 4, 'Require 1..4 variants')
    ids, definitions = set(), set()
    for v in variants:
        fields(v, ('id', 'index_id', 'index_version_id', 'embedding_deployment_id', 'rerank'))
        require(label(v['id']) and v['id'] not in ids and all(valid_uuid(v[k]) for k in ('index_id', 'index_version_id', 'embedding_deployment_id')), 'Invalid variant IDs')
        ids.add(v['id'])
        if v['rerank'] is not None:
            fields(v['rerank'], ('deployment_id', 'expected_reranker_profile_id'))
            require(valid_uuid(v['rerank']['deployment_id']) and isinstance(v['rerank']['expected_reranker_profile_id'], str)
                    and re.fullmatch('rp_[a-f0-9]{64}', v['rerank']['expected_reranker_profile_id']), 'Invalid pinned reranker')
        fingerprint = identity({k: value for k, value in v.items() if k != 'id'})
        require(fingerprint not in definitions, 'Duplicate variant definitions')
        definitions.add(fingerprint)
    return variants


def verified_spans(rows, sources):
    for h in rows:
        doc = sources[h['canonical_id']]
        blocks = {b['id']: b['text'] for b in doc['blocks']}
        for s in h['source_spans']:
            text = blocks.get(s['block_id'])
            require(text is not None and s['end'] <= len(text)
                    and text[s['start']:s['end']] == h['text'][s['chunk_start']:s['chunk_end']], 'Search span text differs from pinned canonical source')


def safe_error(error, stage):
    if isinstance(error, APIError):
        return {'status': 'failed', 'stage': stage, 'error_code': error.code, 'http_status': error.status}
    if isinstance(error, CandidateMismatch):
        return {'status': 'failed', 'stage': stage, 'error_code': str(error),
                'candidate_difference': copy.deepcopy(error.detail)}
    codes = ('CANDIDATE_SET_CHANGED', 'CANDIDATE_IDENTITY_CHANGED', 'EMBEDDING_PROFILE_CHANGED',
             'RERANKER_PROFILE_CHANGED', 'API_CONNECTION_FAILED', 'API_RESPONSE_TOO_LARGE')
    code = str(error) if str(error) in codes else 'RESPONSE_CONTRACT_INVALID'
    return {'status': 'failed', 'stage': stage, 'error_code': code}


def timed(call, *args):
    start = time.monotonic()
    value = call(*args)
    return value, round((time.monotonic() - start) * 1000, 3)


def run(dataset, packet, config, dataset_hash, knowledge_url, model_url, call, checkpoint=lambda value: None):
    validate_dataset(dataset, packet)
    variants = validate_variants(config)
    require(sha(dataset_hash), 'Dataset file hash is invalid')
    require(len(dataset['cases']) * len(variants) * (config['top_k'] + 5) <= 1000, 'Report detail budget exceeded; reduce questions, variants or top_k')
    sources = {d['canonical_id']: d for d in packet['documents']}
    versions, rerankers, embeddings = {}, {}, {}
    for variant in variants:
        key = (variant['index_id'], variant['index_version_id'])
        if key not in versions:
            index = call(knowledge_url, '/api/indexes/' + key[0])
            require(index.get('index_id') == key[0] and index.get('state') == 'active', 'Index unavailable')
            v = version_ready(call(knowledge_url, '/api/index-versions/' + key[1]), *key)
            require(v['input_spec']['snapshot_id'] == dataset['snapshot_id'] and pins(v) == dataset['documents'], 'Index snapshot or selected source set differs from dataset')
            versions[key] = v
        v = versions[key]
        dep = variant['embedding_deployment_id']
        if dep not in embeddings:
            embeddings[dep] = call(model_url, '/api/deployments/' + dep + '/embedding-profile')
        require(profile_matches(embeddings[dep], v['embedding_profile'], dep), 'Embedding space differs from index')
        rr = variant['rerank']
        if rr:
            rid = rr['deployment_id']
            if rid not in rerankers:
                rerankers[rid] = call(model_url, '/api/deployments/' + rid + '/reranker-profile')
            rp = rerankers[rid]
            require(valid_reranker(rp, rid) and rp['reranker_profile_id'] == rr['expected_reranker_profile_id']
                    and config['candidate_k'] <= rp['max_candidates'], 'Reranker profile or limit differs')
    require(len(embeddings) == 1 and len(rerankers) <= 1, 'Use one query deployment and at most one reranker for a controlled comparison')
    dep, embedding = next(iter(embeddings.items()))
    require(sha(embedding.get('runtime_container_id')), 'Embedding runtime identity missing')
    variant_pins = []
    for v in variants:
        version = versions[(v['index_id'], v['index_version_id'])]
        variant_pins.append({**copy.deepcopy(v), 'chunking_run_id': version['chunking_run_id'], 'chunk_set_sha256': version['chunk_set_sha256'],
                             'chunker': version['input_spec']['chunker'], 'indexed_chunks': version['indexed_count'],
                             'index_embedding_profile': version['embedding_profile'], 'indexing_config': indexing_config(version)})
    report = {'schema_version': 'retrieval-report-v1', 'engine': ENGINE, 'status': 'running', 'created_at': datetime.now(timezone.utc).isoformat(),
              'dataset_sha256': dataset_hash, 'dataset_id': dataset['dataset_id'], 'sources_sha256': dataset['sources_sha256'],
              'snapshot_id': dataset['snapshot_id'], 'documents': dataset['documents'], 'coverage_threshold': dataset['coverage_threshold'],
              'candidate_k': config['candidate_k'], 'top_k': config['top_k'], 'query_profile': copy.deepcopy(embedding),
              'reranker_profiles': copy.deepcopy(rerankers), 'variants': variant_pins, 'planned_questions': len(dataset['cases']),
              'query_embedding_requests': 0, 'search_requests': 0, 'lifecycle_changes': 0, 'cases': [], 'summary': {}}
    ids = [v['id'] for v in variants]
    diagnostic_bytes = 0
    def publish():
        report['summary'] = summarize(report['cases'], ids, report['planned_questions'])
        checkpoint(report)
    publish()
    try:
        for case in dataset['cases']:
            row = {'case_id': case['case_id'], 'variants': {}}
            try:
                report['query_embedding_requests'] += 1
                embedded, ms = timed(call, model_url, '/api/deployments/' + dep + '/embeddings', 'POST', {
                    'expected_embedding_space_id': embedding['embedding_space_id'], 'role': 'query',
                    'inputs': [{'id': 'query-1', 'text': case['question']}]})
                vector = query_vector(embedded, embedding, dep)
                require(embedded['profile'] == embedding, 'EMBEDDING_PROFILE_CHANGED')
                row['query_embedding_ms'] = ms
                row['query_embedding_sha256'] = identity(vector)
            except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
                row['variants'] = {vid: safe_error(error, 'query_embedding') for vid in ids}
                report['cases'].append(row); publish(); continue
            baselines = {}
            for variant in variants:
                key = (variant['index_id'], variant['index_version_id'])
                version = versions[key]
                path = '/api/indexes/' + key[0] + '/search'
                body = {'index_version_id': key[1], 'embedding_space_id': embedding['embedding_space_id'], 'query_embedding': vector,
                        'candidate_k': config['candidate_k'], 'top_k': config['candidate_k'], 'filters': {}, 'rerank': {'enabled': False}}
                stage = 'vector_candidates'
                candidate_info = {}
                try:
                    if key not in baselines:
                        try:
                            report['search_requests'] += 1
                            response, ms = timed(call, knowledge_url, path, 'POST', body)
                            hits = validate_result(response, version, body)
                            verified_spans(hits, sources)
                            baselines[key] = (response, hits, ms)
                        except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
                            baselines[key] = error
                    if isinstance(baselines[key], Exception):
                        raise baselines[key]
                    response, baseline, ms = baselines[key]
                    candidate_assigned = assign(baseline, case['judgments'], dataset['coverage_threshold'])
                    candidate_info = {
                        'candidate_count': len(baseline),
                        'candidate_set_sha256': identity([(h['chunk_id'], h['retrieval_rank'], h['retrieval_score']) for h in baseline]),
                        'candidate_recall': metrics(candidate_assigned, case['judgments'], config['candidate_k'])['recall'],
                    }
                    hits = baseline
                    if variant['rerank']:
                        stage = 'rerank'
                        enabled = {**body, 'query_text': case['question'], 'rerank': {'enabled': True, **variant['rerank']}}
                        report['search_requests'] += 1
                        response, ms = timed(call, knowledge_url, path, 'POST', enabled)
                        hits = validate_result(response, version, enabled)
                        require(response['rerank']['profile'] == rerankers[variant['rerank']['deployment_id']], 'RERANKER_PROFILE_CHANGED')
                        candidate_equal(baseline, hits)
                    assigned = assign(hits, case['judgments'], dataset['coverage_threshold'])
                    timings = response.get('timings', {})
                    require(isinstance(timings, dict) and all(number(v) and v >= 0 for v in timings.values()), 'Invalid API timings')
                    row['variants'][variant['id']] = {
                        'status': 'ok', **candidate_info, 'evaluated_count': min(len(hits), config['top_k']),
                        'metrics': metrics(assigned, case['judgments'], config['top_k']), 'search_wall_ms': ms, 'api_timings': timings,
                        'top_matches': assigned[:config['top_k']],
                        'quality_states': sorted({str(h['quality'].get('state', 'unknown')) for h in hits if isinstance(h['quality'], dict)}),
                    }
                except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
                    failure = {**safe_error(error, stage), **candidate_info}
                    if 'candidate_difference' in failure:
                        size = len(encoded(failure['candidate_difference']))
                        if diagnostic_bytes + size <= REPORT_DETAIL_LIMIT:
                            diagnostic_bytes += size
                        else:
                            failure.pop('candidate_difference')
                            failure['candidate_difference_omitted'] = 'report_detail_budget_exhausted'
                    row['variants'][variant['id']] = failure
            report['cases'].append(row); publish()
        report['status'] = 'completed_with_errors' if any(v['status'] != 'ok' for c in report['cases'] for v in c['variants'].values()) else 'completed'
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
    publish()
    return report


def aggregate_matches(actual, expected):
    """Allow rounding noise only in derived floats, never counts or structure.

    Python versions use different float summation algorithms. Old JSON reports
    must remain usable without rewriting their original bytes or identities.
    """
    if type(expected) is dict:
        return (type(actual) is dict and actual.keys() == expected.keys()
                and all(aggregate_matches(actual[k], expected[k]) for k in expected))
    if type(expected) is float:
        return (type(actual) in (int, float) and math.isfinite(actual)
                and math.isfinite(expected)
                and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12))
    return type(actual) is type(expected) and actual == expected


def compare(left, right, left_id, right_id):
    """Compare fixed-denominator means; failures stay in each report's means."""
    for r in (left, right):
        require(r.get('schema_version') == 'retrieval-report-v1' and r.get('engine') == ENGINE
                and r.get('status') in ('completed', 'completed_with_errors'), 'Only completed reports can be compared')
        ids = [v['id'] for v in r['variants']]
        for variant in r['variants']:
            indexing_config(variant)
        require(len(ids) == len(set(ids)) and len(r['cases']) == r['planned_questions'] > 0, 'Report case count differs')
        require(all(set(c['variants']) == set(ids) and all(e.get('status') in ('ok', 'failed') for e in c['variants'].values()) for c in r['cases']), 'Report variant outcomes missing')
        require(len({c['case_id'] for c in r['cases']}) == len(r['cases']), 'Duplicate report cases')
        for case in r['cases']:
            for outcome in case['variants'].values():
                if 'candidate_recall' in outcome:
                    require(number(outcome['candidate_recall']) and 0 <= outcome['candidate_recall'] <= 1, 'Invalid candidate recall')
                if outcome['status'] == 'ok':
                    require(set(outcome['metrics']) == set(NAMES) and all(number(n) and 0 <= n <= 1 for n in outcome['metrics'].values())
                            and number(outcome['candidate_recall']) and 0 <= outcome['candidate_recall'] <= 1
                            and number(outcome['search_wall_ms']) and outcome['search_wall_ms'] >= 0, 'Invalid report metrics')
        # Recompute rather than trusting a stale aggregate.
        require(aggregate_matches(r['summary'], summarize(r['cases'], ids, r['planned_questions'])), 'Report aggregate differs')
    keys = ('dataset_sha256', 'dataset_id', 'sources_sha256', 'snapshot_id', 'documents', 'coverage_threshold', 'candidate_k', 'top_k', 'query_profile')
    require(all(left[k] == right[k] for k in keys), 'Reports are not a controlled comparison: dataset/input/budget/query profile differs')
    require([c['case_id'] for c in left['cases']] == [c['case_id'] for c in right['cases']], 'Report questions differ')
    if left['reranker_profiles'] and right['reranker_profiles']:
        require(left['reranker_profiles'] == right['reranker_profiles'], 'Reranker profile changed between reports')
    require(left_id in left['summary'] and right_id in right['summary'], 'Selected variant is missing')
    comparisons = []
    for a in [v for v in left['variants'] if v['id'] == left_id]:
        for b in [v for v in right['variants'] if v['id'] == right_id]:
            if a['index_version_id'] == b['index_version_id']:
                require(indexing_config(a) == indexing_config(b), 'Same index version has different indexing configuration')
                for lc, rc in zip(left['cases'], right['cases']):
                    le, re = lc['variants'][left_id], rc['variants'][right_id]
                    if le['status'] == re['status'] == 'ok':
                        require(le['candidate_set_sha256'] == re['candidate_set_sha256'], 'Candidate sets changed between reports')
            sa, sb = left['summary'][a['id']], right['summary'][b['id']]
            comparisons.append({'left': a['id'], 'right': b['id'], 'left_chunker': a['chunker'], 'right_chunker': b['chunker'],
                                'left_indexing_config': indexing_config(a), 'right_indexing_config': indexing_config(b),
                                'right_minus_left': {m: sb['all_questions_metrics'][m] - sa['all_questions_metrics'][m] for m in NAMES},
                                'left_failure_rate': sa['failure_rate'], 'right_failure_rate': sb['failure_rate']})
    return {'schema_version': 'retrieval-comparison-v1', 'engine': ENGINE, 'dataset_sha256': left['dataset_sha256'],
            'left_report_sha256': identity(left), 'right_report_sha256': identity(right),
            'scope': 'Fixed judged evidence and budgets; no significance or latency claims', 'comparisons': comparisons}
