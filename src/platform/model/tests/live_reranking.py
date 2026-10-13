#!/usr/bin/env python3
"""Check an explicitly selected ready reranker; never deploy, stop or index."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'playground'))
from reranking_validation import validate_profile, validate_result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('Nonfinite JSON value')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def check(call, deployment_id):
    profile = validate_profile(call('/reranker-profile'), deployment_id)
    body = {'expected_reranker_profile_id': profile['reranker_profile_id'],
            'query': '대한민국의 수도는 어디인가?', 'candidates': [
                {'id': 'relevant', 'text': '대한민국의 수도는 서울이다.'},
                {'id': 'unrelated', 'text': '사과는 과일이다.'}]}
    wrong = 'rp_' + ('0' if profile['reranker_profile_id'] != 'rp_' + '0'*64 else '1')*64
    call('/rerank', dict(body, expected_reranker_profile_id=wrong), 409, 'RERANK_PROFILE_MISMATCH')
    call('/rerank', dict(body, candidates=[]), 400, 'RERANK_INVALID_REQUEST')
    call('/rerank', dict(body, candidates=[body['candidates'][0]]*2), 400, 'RERANK_INVALID_REQUEST')
    call('/rerank', dict(body, top_k=1), 400, 'RERANK_INVALID_REQUEST')
    call('/rerank', dict(body, candidates=[{'id': 'long', 'text': 'hello ' * 600}]), 400, 'RERANK_INPUT_TOO_LONG')
    rows = validate_result(call('/rerank', body), profile, ['relevant', 'unrelated'])
    if rows[0]['score'] <= rows[1]['score']:
        raise ValueError('Known short relevance pair did not rank above unrelated text')
    # Reordering candidates must preserve IDs and pair scores (allow dtype noise).
    reversed_rows = validate_result(call('/rerank', dict(body, candidates=list(reversed(body['candidates'])))), profile, ['unrelated', 'relevant'])
    for a, b in zip(rows, reversed(reversed_rows)):
        if abs(a['score'] - b['score']) > 0.05:
            raise ValueError('Candidate reorder changed pair scores beyond smoke tolerance')
    return {'status': 'passed', 'scope': 'Model reranker API and one short relevance pair; not retrieval quality evaluation',
            'profile': profile, 'scores': rows, 'valid_scoring_requests': 2, 'lifecycle_changes': 0,
            'checks': ['profile_identity', 'input_id_order', 'finite_single_logit', 'pair_token_limit',
                       'short_relevance_pair', 'candidate_reorder', 'profile_mismatch_rejected',
                       'empty_candidates_rejected', 'duplicate_ids_rejected', 'unknown_field_rejected', 'overlong_pair_rejected']}


def save_summary(path, result):
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.reranking-check-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        temporary = None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:13000')
    parser.add_argument('--deployment-id', required=True)
    parser.add_argument('--summary-file', type=Path)
    args = parser.parse_args()
    deployment_id = str(uuid.UUID(args.deployment_id))
    if deployment_id != args.deployment_id or uuid.UUID(deployment_id).int == 0:
        raise ValueError('Canonical deployment UUID required')
    base = args.base_url.rstrip('/')
    url = urlsplit(base)
    if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password or url.path or url.query or url.fragment:
        raise ValueError('base-url must be an HTTP(S) origin')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    def call(suffix, body=None, expected=200, reason=None):
        raw = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        req = Request(base + '/api/deployments/' + deployment_id + suffix, data=raw, headers={'Content-Type': 'application/json'})
        try:
            response = opener.open(req, timeout=135)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(2*1024*1024 + 1)
            if len(raw) > 2*1024*1024:
                raise ValueError('Oversized API response')
            result = decode(raw)
            if not isinstance(result, dict):
                raise ValueError('Invalid API response')
            if response.code != expected or (reason and result.get('code') != reason):
                code = result.get('code', '')
                safe = code if isinstance(code, str) and re.fullmatch('[A-Za-z_]{1,80}', code) else 'UNRECOGNIZED'
                raise ValueError(f'API {suffix}: HTTP {response.code}/{safe}')
            return result
    summary = check(call, deployment_id)
    if args.summary_file:
        save_summary(args.summary_file, summary)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError) as exc:
        raise SystemExit('FAIL: ' + str(exc)) from None
    except OSError:
        raise SystemExit('FAIL: connection or summary-file operation failed') from None
