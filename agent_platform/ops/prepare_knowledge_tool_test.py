#!/usr/bin/env python3
"""Prepare a real Knowledge search Tool input with one query embedding; no rebuilds."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

MAX_BYTES = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def strict_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('Duplicate JSON key')
            out[key] = value
        return out
    def constant(value):
        raise ValueError('Nonfinite JSON number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def api(base, path, method='GET', body=None):
    raw = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    req = Request(base.rstrip('/') + path, method=method, data=raw, headers={'Content-Type': 'application/json'})
    try:
        with build_opener(NoRedirect).open(req, timeout=130 if path.endswith('/embeddings') else 30) as response:
            content = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise ValueError(f'HTTP {exc.code} at {path}; check Gateway/service state') from None
    except (URLError, TimeoutError):
        raise ValueError(f'Cannot reach {path}; check Gateway and deployed embedding model') from None
    if len(content) > MAX_BYTES:
        raise ValueError('API response exceeded 2 MiB')
    result = strict_json(content)
    if not isinstance(result, dict):
        raise ValueError('Expected an object response')
    return result


def valid_uuid(value):
    try:
        return str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0
    except (ValueError, TypeError, AttributeError):
        return False


def validate_base(value, tool=False):
    u = urlsplit(value)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('', '/'):
        raise ValueError('Use an http(s) server origin without path, query or credentials')
    if tool and (u.hostname in ('localhost', '127.0.0.1', '::1') or u.hostname.endswith('.localhost')):
        raise ValueError('Tool base URL must be reachable from agent-runtime; loopback is not allowed')
    return value.rstrip('/')


def same_profile(current, pinned, deployment):
    return (isinstance(current, dict) and current.get('deployment_id') == deployment
            and current.get('embedding_space_id') == pinned.get('embedding_space_id')
            and current.get('dimension') == pinned.get('dimension') == 1024
            and current.get('capability') == 'embedding' and current.get('status') == 'ready'
            and current.get('normalization') == 'l2')


def prepare(args, call=api):
    base = validate_base(args.knowledge_url)
    validate_base(args.tool_base_url, tool=True)
    if not args.question.strip() or '\x00' in args.question or len(args.question.encode()) > 128 * 1024 or not 1 <= args.top_k <= 50:
        raise ValueError('Provide a nonempty question up to 128 KiB and top_k 1..50')
    if args.index_id:
        if not valid_uuid(args.index_id):
            raise ValueError('Invalid index UUID')
        index_id = args.index_id
    else:
        found, cursor = [], ''
        for _ in range(50):
            query = {'limit': 100}
            if cursor:
                query['cursor'] = cursor
            page = call(base, '/api/indexes?' + urlencode(query))
            found.extend(x['index_id'] for x in page.get('items', []) if x.get('name') == args.index_name and x.get('state') == 'active')
            cursor = page.get('next_cursor')
            if not cursor:
                break
        else:
            raise ValueError('Index list limit exceeded; specify --index-id')
        if len(found) != 1:
            raise ValueError('Expected one active index with that name; check --index-name or specify --index-id')
        index_id = found[0]
    if not valid_uuid(index_id):
        raise ValueError('Invalid returned index UUID')
    index = call(base, '/api/indexes/' + index_id)
    version_id = args.index_version_id or index.get('active_version_id')
    if index.get('index_id') != index_id or index.get('state') != 'active' or not valid_uuid(version_id):
        raise ValueError('Index unavailable or no active version; select a ready version with --index-version-id')
    version = call(base, '/api/index-versions/' + version_id)
    if (version.get('index_id') != index_id or version.get('index_version_id') != version_id
            or version.get('state') != 'ready' or version.get('reference_state') != 'held'
            or type(version.get('chunk_count')) is not int or version['chunk_count'] < 1
            or version.get('indexed_count') != version['chunk_count']):
        raise ValueError('Index version must be ready, fully indexed and held')
    pinned = version.get('embedding_profile', {})
    deployment = pinned.get('deployment_id')
    if not valid_uuid(deployment) or not re.fullmatch(r'es_[a-f0-9]{64}', pinned.get('embedding_space_id', '')):
        raise ValueError('Index embedding profile is invalid')
    route = '/console/model/deployments/' + deployment
    current = call(base, route + '/embedding-profile')
    if not same_profile(current, pinned, deployment):
        raise ValueError('Embedding deployment is not ready or its space differs from the index')
    embedded = call(base, route + '/embeddings', 'POST', {
        'expected_embedding_space_id': pinned['embedding_space_id'], 'role': 'query',
        'inputs': [{'id': 'query-1', 'text': args.question}],
    })
    rows = embedded.get('data')
    if embedded.get('role') != 'query' or not same_profile(embedded.get('profile'), pinned, deployment) or not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('Embedding receipt role/profile/count differs')
    row = rows[0]
    vector = row.get('embedding')
    if (row.get('id') != 'query-1' or type(row.get('index')) is not int or row['index'] != 0
            or not isinstance(vector, list) or len(vector) != 1024
            or not all(type(x) in (int, float) and math.isfinite(x) for x in vector)):
        raise ValueError('Embedding receipt ID/index/dimension/value differs')
    norm = math.sqrt(sum(x*x for x in vector))
    if not math.isfinite(norm) or abs(norm - 1) > 0.001:
        raise ValueError('Embedding is not L2 normalized; no normalization is performed by this script')
    body = {'index_version_id': version_id, 'embedding_space_id': pinned['embedding_space_id'],
            'query_embedding': vector, 'top_k': args.top_k, 'candidate_k': args.top_k}
    receipt = {'status': 'prepared', 'index_name': index.get('name', args.index_name), 'index_id': index_id,
               'index_version_id': version_id, 'embedding_space_id': pinned['embedding_space_id'],
               'embedding_deployment_id': deployment, 'query_embedding_calls': 1,
               'lifecycle_writes': 0, 'rerank': False, 'agent_tool_execution': 'not_run'}
    if args.check_search:
        result = call(base, '/api/indexes/' + index_id + '/search', 'POST', body)
        hits, rerank = result.get('results'), result.get('rerank', {})
        if (result.get('index_id') != index_id or result.get('index_version_id') != version_id
                or result.get('embedding_space_id') != pinned['embedding_space_id']
                or not isinstance(hits, list) or type(result.get('returned_count')) is not int
                or result['returned_count'] != len(hits) or len(hits) > args.top_k
                or rerank.get('requested') is not False or rerank.get('applied') is not False or rerank.get('status') != 'disabled'):
            raise ValueError('Baseline search version/space/count/rerank differs')
        receipt['baseline_search'] = {'status': 'passed', 'returned_count': len(hits)}
    return {'path': {'index_id': index_id}, 'body': body}, receipt


def write_outputs(out, arguments, receipt, tool_base):
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    examples = Path(__file__).resolve().parents[1] / 'examples/knowledge-search'
    def write(name, value):
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    write('tool-arguments.json', arguments)
    write('preparation-result.json', receipt)
    for name in ('request-schema.json', 'response-schema.json', 'path-schema.json'):
        write(name, strict_json((examples / name).read_bytes()))
    spec = strict_json((examples / 'openapi.json').read_bytes())
    spec['servers'] = [{'url': tool_base.rstrip('/')}]
    write('openapi.json', spec)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--knowledge-url', default='http://127.0.0.1:15100', help='Gateway accessible from the host running this script')
    parser.add_argument('--tool-base-url', default='http://knowledge-gateway:8000', help='Gateway origin accessible from agent-runtime')
    parser.add_argument('--index-name', default='test1')
    parser.add_argument('--index-id', default='')
    parser.add_argument('--index-version-id', default='')
    parser.add_argument('--question', required=True)
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--check-search', action='store_true', help='Also run one vector search directly as a baseline')
    parser.add_argument('--out', type=Path, required=True, help='New output directory outside the source tree')
    args = parser.parse_args()
    args.out = args.out.expanduser().absolute()
    if args.out.exists() or args.out.is_symlink() or not args.out.parent.is_dir():
        raise ValueError('Output must be a new directory with an existing parent; nothing was called')
    arguments, receipt = prepare(args)
    write_outputs(args.out, arguments, receipt, args.tool_base_url)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    print('Prepared:', args.out)
    print('Use request/response/path schemas for direct registration, or import openapi.json.')
    print('Paste tool-arguments.json into the Tool test JSON input. Agent execution has not run yet.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise SystemExit('FAIL: ' + str(exc))
