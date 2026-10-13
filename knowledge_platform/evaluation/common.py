"""Bounded files, explicit API access and immutable input identities."""
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from knowledge_platform.tests.live_search import (
    APIError, strict_json, valid_uuid, profile_matches, query_vector, validate_result,
    valid_reranker, count, number,
)

ENGINE = 'source-span-greedy-v1'
PACKET_LIMIT = 512 << 10
DATASET_LIMIT = 256 << 10
REPORT_LIMIT = 384 << 10
PIN_FIELDS = ('canonical_id', 'canonical_sha256', 'source_document_id', 'source_sha256')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')


def identity(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def fields(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= value.keys() <= set(required) | set(optional), 'Invalid or unknown object fields')


def label(value):
    return isinstance(value, str) and re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', value) is not None


def read_json(path, limit):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, 'Input file exceeds size limit')
    return strict_json(raw.decode('utf-8')), digest(raw)


def save_json(path, value, limit):
    raw = encoded(value)
    require(len(raw) <= limit, 'Output exceeds size limit; nothing was truncated')
    path = Path(path).expanduser()
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def pins(version):
    docs = version['input_spec']['documents']
    ids = version['canonical_ids']
    require(isinstance(docs, list) and 1 <= len(docs) <= 20 and isinstance(ids, list)
            and 1 <= len(ids) <= 20 and all(valid_uuid(x) for x in ids)
            and len(set(ids)) == len(ids), 'Invalid index document selection')
    require(all(isinstance(d, dict) and all(k in d for k in PIN_FIELDS) for d in docs), 'Index document pins missing')
    require(len({d['canonical_id'] for d in docs}) == len(docs), 'Duplicate index documents')
    selected = sorted(({k: d[k] for k in PIN_FIELDS} for d in docs if d['canonical_id'] in ids), key=lambda d: d['canonical_id'])
    require(len(selected) == len(ids), 'Selected canonical documents missing')
    for d in selected:
        require(valid_uuid(d['canonical_id']) and valid_uuid(d['source_document_id'])
                and sha(d['canonical_sha256']) and sha(d['source_sha256']), 'Invalid source identities')
    return selected


def version_ready(version, index_id, version_id):
    require(version.get('index_id') == index_id and version.get('index_version_id') == version_id
            and version.get('state') == 'ready' and version.get('reference_state') == 'held'
            and type(version.get('chunk_count')) is int and 1 <= version['chunk_count'] <= 20000
            and version.get('indexed_count') == version['chunk_count'], 'Index version is not ready and complete')
    require(sha(version.get('chunk_set_sha256')) and valid_uuid(version.get('chunking_run_id'))
            and sha(version.get('input_spec', {}).get('snapshot_id')), 'Index lineage is incomplete')
    chunker = version['input_spec'].get('chunker', {})
    require(chunker.get('id') in ('structure_v1', 'recursive_character_v1') and isinstance(chunker.get('version'), str)
            and sha(chunker.get('policy_hash')) and isinstance(chunker.get('options'), dict), 'Index chunker identity missing')
    pins(version)
    indexing_config(version)
    return version


def indexing_config(value):
    """Resolve legacy versions/reports without changing the supplied evidence."""
    if 'indexing_config' not in value:
        return {'schema_version': 'indexing-config-v1', 'method': 'hnsw', 'engine': 'lucene',
                'space_type': 'cosinesimil', 'parameters': {'m': 16, 'ef_construction': 128}}
    config = value['indexing_config']
    fields(config, ('schema_version', 'method', 'engine', 'space_type', 'parameters'))
    require(config['schema_version'] == 'indexing-config-v1' and config['method'] == 'hnsw'
            and config['engine'] == 'lucene' and config['space_type'] == 'cosinesimil', 'Invalid indexing configuration')
    params = config['parameters']
    fields(params, ('m', 'ef_construction'))
    require(type(params['m']) is int and 2 <= params['m'] <= 64
            and type(params['ef_construction']) is int and 16 <= params['ef_construction'] <= 1024,
            'Invalid HNSW configuration')
    return {**config, 'parameters': dict(params)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    """Only public read endpoints and two inference endpoints; no retries."""
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def raw(self, base, path, method='GET', body=None, limit=2 << 20):
        parsed = urlsplit(base)
        require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
                and parsed.path in ('', '/') and not parsed.query and not parsed.fragment, 'Gateway URL must be an HTTP(S) origin')
        read = re.fullmatch(r'/api/(indexes|index-versions)/[0-9a-f-]+|/api/deployments/[0-9a-f-]+/(embedding-profile|reranker-profile)|/api/canonical-documents/[0-9a-f-]+/content', path)
        infer = re.fullmatch(r'/api/(indexes/[0-9a-f-]+/search|deployments/[0-9a-f-]+/embeddings)', path)
        require((method == 'GET' and read and body is None) or (method == 'POST' and infer and isinstance(body, dict)), 'Endpoint is outside evaluation scope')
        req = Request(base.rstrip('/') + path, method=method, data=None if body is None else encoded(body), headers={'Content-Type': 'application/json'})
        timeout = 130 if path.endswith('/embeddings') else 100 if path.endswith('/content') else 70
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read(limit + 1)
        except HTTPError as error:
            code = 'REQUEST_FAILED'
            try:
                payload = strict_json(error.read(8192))
                field = payload.get('error')
                message = field.get('message', '') if isinstance(field, dict) else field
                candidate = payload.get('code') or (message.split(':', 1)[0] if isinstance(message, str) else '')
                if isinstance(candidate, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,79}', candidate):
                    code = candidate
            except (ValueError, TypeError, AttributeError):
                pass
            raise APIError(error.code, code) from None
        except (URLError, TimeoutError, ConnectionError, HTTPException):
            raise ValueError('API_CONNECTION_FAILED') from None
        require(len(raw) <= limit, 'API_RESPONSE_TOO_LARGE')
        return raw

    def __call__(self, base, path, method='GET', body=None):
        value = strict_json(self.raw(base, path, method, body).decode('utf-8'))
        require(isinstance(value, dict), 'API_RESPONSE_INVALID')
        return value
