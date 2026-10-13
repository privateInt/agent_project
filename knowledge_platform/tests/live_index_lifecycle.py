#!/usr/bin/env python3
"""Opt-in: build two small, owned versions, verify deletion and preserve the baseline.

Only this journal's test index may be mutated. Keep the journal for safe replay.
The Docker observer reads OpenSearch and Data independently; no Model inference
is performed by the observer. No original/canonical/ChunkSet deletion is called.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

from live_search import APIError, strict_json, valid_uuid

SCHEMA = 'knowledge-lifecycle-check-v1'
MAX_STATE = 96 << 10
VERSION_FIELDS = ('index_version_id', 'index_id', 'chunking_run_id', 'chunk_set_sha256',
                  'input_spec', 'canonical_ids', 'embedding_profile', 'state',
                  'chunk_count', 'indexed_count', 'reference_state')
INDEX_FIELDS = ('index_id', 'name', 'embedding_deployment_id', 'state', 'active_version_id', 'revision')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def select(value, fields):
    return {k: value[k] for k in fields}


def save(path, state):
    raw = encoded(state)
    require(len(raw) <= MAX_STATE, 'Journal exceeds 96 KiB')
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.lifecycle-', delete=False) as f:
            name = f.name
            f.write(raw + b'\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        name = None
    finally:
        if name:
            Path(name).unlink(missing_ok=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirect refused; no mutation was redirected')


class Client:
    def __init__(self, base):
        u = urlsplit(base)
        require(u.scheme in ('http', 'https') and u.hostname and not u.username and not u.password
                and u.path in ('', '/') and not u.query and not u.fragment, 'Invalid Gateway origin')
        self.base = base.rstrip('/')
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def __call__(self, path, method='GET', body=None, key=None):
        require(path.startswith('/api/') and not path.startswith('//'), 'Invalid API path')
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Idempotency-Key'] = key
        request = Request(self.base + path, method=method, headers=headers,
                          data=None if body is None else encoded(body))
        try:
            with self.opener.open(request, timeout=105) as response:
                raw = response.read((2 << 20) + 1)
        except HTTPError as e:
            code = 'REQUEST_FAILED'
            try:
                message = strict_json(e.read(8192)).get('error', {}).get('message', '')
                if isinstance(message, str) and ':' in message:
                    token = message.split(':', 1)[0]
                    if token.replace('_', '').isalnum() and len(token) <= 80:
                        code = token
            except (ValueError, AttributeError):
                pass
            raise APIError(e.code, code) from None
        except URLError:
            raise ValueError('API result unconfirmed; repeat with the SAME journal') from None
        require(len(raw) <= 2 << 20, 'Response exceeds 2 MiB')
        result = strict_json(raw)
        require(isinstance(result, dict), 'Response must be an object')
        return result


class DockerObserver:
    """Use the service's network and selected env privately, never its database."""
    def __init__(self, container, image):
        self.container, self.image = container, image
        p = subprocess.run(['docker', 'inspect', container], capture_output=True, timeout=20)
        require(p.returncode == 0, 'Cannot inspect Knowledge service container')
        rows = strict_json(p.stdout)
        require(len(rows) == 1 and rows[0]['State']['Running'], 'Knowledge service is not running')
        self.container = rows[0]['Id']  # Pin the observed container, not a mutable name.
        env = dict(x.split('=', 1) for x in rows[0]['Config']['Env'] if '=' in x)
        self.env = dict(os.environ)
        self.keys = ['OPENSEARCH_ENDPOINT', 'OPENSEARCH_USERNAME', 'OPENSEARCH_PASSWORD', 'OPENSEARCH_CA_BASE64']
        require(all(env.get(k) for k in self.keys), 'Indexing settings are missing in Knowledge service')
        for k in ('DATA_REFINEMENT_GRPC_ADDRESS', 'DATA_COLLECTION_GRPC_ADDRESS'):
            if env.get(k):
                self.keys.append(k)
        for k in self.keys:
            self.env[k] = env[k]
        p = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, timeout=20)
        require(p.returncode == 0, 'Build the cleanup-verifier image first')
        self.image = strict_json(p.stdout)[0]['Id']

    def __call__(self, versions):
        command = ['docker', 'run', '--rm', '-i', '--read-only', '--cap-drop=ALL',
                   '--security-opt=no-new-privileges', '--network', 'container:' + self.container]
        for k in self.keys:
            command += ['-e', k]
        command.append(self.image)
        p = subprocess.run(command, input=encoded({'versions': versions}), capture_output=True,
                           env=self.env, timeout=120)
        if p.returncode:
            reason = p.stderr.decode(errors='replace').strip()
            safe = reason if reason.startswith('FAIL: ') and '\n' not in reason and len(reason) <= 240 else 'observer did not complete'
            raise ValueError('Independent storage/Data observation failed: ' + safe)
        receipt = strict_json(p.stdout)
        require(receipt.get('status') == 'passed' and receipt.get('versions') == len(versions),
                'Observer receipt differs')
        return receipt


class Lifecycle:
    def __init__(self, api, observer, path, config, timeout=600, sleep=time.sleep):
        self.api, self.observer, self.path, self.config = api, observer, path, config
        self.timeout, self.sleep = timeout, sleep
        if path.exists():
            require(path.is_file() and path.stat().st_size <= MAX_STATE, 'Invalid journal')
            self.s = strict_json(path.read_bytes())
            require(self.s.get('schema') == SCHEMA and self.s.get('input') == config,
                    'Journal belongs to different input; no mutation made')
            require(valid_uuid(self.s.get('run_id')) and self.s.get('name') == 'lifecycle-check-' + self.s['run_id'],
                    'Invalid journal ownership')
            require(type(self.s.get('phase')) is int and 0 <= self.s['phase'] <= 6, 'Invalid journal phase')
            require(set(self.s.get('keys', {})) == {'create', 'a', 'b'}
                    and all(valid_uuid(k) for k in self.s['keys'].values()), 'Invalid replay keys')
        else:
            self.s = None

    def persist(self):
        save(self.path, self.s)

    def checkpoint(self, phase):
        self.s['phase'] = phase
        self.persist()
        print(json.dumps({'phase': phase, 'test_index_id': self.s.get('index_id', '')}), flush=True)

    def version(self, ident):
        require(valid_uuid(ident), 'Invalid version ID')
        v = self.api('/api/index-versions/' + ident)
        require(v.get('index_version_id') == ident and valid_uuid(v.get('index_id')), 'Version identity differs')
        return v

    def chunk_snapshot(self, run_id):
        run = self.api('/api/chunking-runs/' + run_id)
        require(run.get('chunking_run_id') == run_id and run.get('result_registered'), 'Stored chunks unavailable')
        cursor, seen, chunks = '', set(), []
        while True:
            from urllib.parse import urlencode
            page = self.api('/api/chunking-runs/' + run_id + '/chunks?' + urlencode({'limit': 100, 'cursor': cursor}))
            require(isinstance(page.get('items'), list) and page.get('chunking_run_id') == run_id
                    and page.get('total') == run['chunk_count'], 'Invalid chunk page')
            chunks.extend(page['items'])
            require(len(chunks) <= 20000, 'Too many chunks')
            cursor = page.get('next_cursor', '')
            if not cursor:
                break
            require(cursor not in seen and len(seen) < 200, 'Repeated chunk cursor')
            seen.add(cursor)
        require(len(chunks) == run['chunk_count'], 'Chunk count differs')
        return run, chunks

    def baseline(self):
        v = self.version(self.config['baseline_version_id'])
        i = self.api('/api/indexes/' + v['index_id'])
        require(i.get('index_id') == v['index_id'] and i.get('state') == 'active'
                and v['state'] == 'ready' and v['reference_state'] == 'held'
                and 0 < v['chunk_count'] == v['indexed_count'], 'Baseline is not ready/held')
        if self.s:
            require(select(i, INDEX_FIELDS) == self.s['baseline_index']
                    and select(v, VERSION_FIELDS) == self.s['baseline_version'], 'Baseline changed; stopping')
        return i, v

    @staticmethod
    def observed(v):
        return {'index_version_id': v['index_version_id'], 'reference_state': v['reference_state'],
                'chunk_count': v['chunk_count'], 'documents': [d for d in v['input_spec']['documents']
                                                            if d['canonical_id'] in v['canonical_ids']]}

    def own_index(self):
        ident = self.s.get('index_id')
        require(valid_uuid(ident) and ident != self.s['baseline_index']['index_id'], 'Not an owned test index')
        i = self.api('/api/indexes/' + ident)
        require(i.get('index_id') == ident and i.get('name') == self.s['name']
                and i.get('embedding_deployment_id') == self.s['deployment_id'], 'Test index ownership differs')
        return i

    def own_version(self, label):
        v = self.version(self.s['versions'][label])
        require(v['index_id'] == self.own_index()['index_id']
                and v['index_version_id'] != self.config['baseline_version_id']
                and v['chunking_run_id'] == self.s['baseline_version']['chunking_run_id']
                and v['chunk_set_sha256'] in ('', self.s['baseline_version']['chunk_set_sha256'])
                and v['canonical_ids'] == [self.config['canonical_id']]
                and v['input_spec'] == self.s['baseline_version']['input_spec']
                and v['embedding_profile'] == self.s['baseline_version']['embedding_profile'],
                'Test version ownership/input/model differs')
        return v

    def own_versions(self):
        i = self.own_index()
        page = self.api('/api/indexes/' + i['index_id'] + '/versions?limit=100')
        require(not page.get('next_cursor') and isinstance(page.get('items'), list), 'Unexpected test versions')
        actual = [v['index_version_id'] for v in page['items']]
        require(len(actual) == len(set(actual)) and set(actual) == set(self.s['versions'].values()),
                'Unrecorded version in test index; deletion refused')
        return {label: self.own_version(label) for label in self.s['versions']}

    def wait(self, read, target):
        deadline = time.monotonic() + self.timeout
        while True:
            value = read()
            if value['state'] == target:
                if target == 'ready':
                    require(value['reference_state'] == 'held' and value['chunk_count'] == value['indexed_count'] == self.s['selected_chunks'],
                            'Ready receipt differs')
                elif 'index_version_id' in value:
                    require(value['reference_state'] == 'released', 'Deleted version still holds references')
                return value
            require(value['state'] != 'failed', 'Build failed; preserve journal and report error: ' + value.get('error_code', ''))
            require(time.monotonic() < deadline, 'Wait timed out; repeat using the SAME journal')
            self.sleep(2)

    def build(self, label):
        i = self.own_index()
        if label not in self.s['versions']:
            require(i['state'] == 'active', 'Test index no longer accepts builds')
            v = self.api('/api/indexes/' + i['index_id'] + '/builds', 'POST',
                         {'chunking_run_id': self.s['baseline_version']['chunking_run_id'],
                          'canonical_ids': [self.config['canonical_id']],
                          'embedding_deployment_id': self.s['deployment_id']}, self.s['keys'][label])
            require(valid_uuid(v.get('index_version_id')) and v.get('index_id') == i['index_id'], 'Build acceptance differs')
            require(v['index_version_id'] not in self.s['versions'].values(), 'Build replay returned another test version')
            self.s['versions'][label] = v['index_version_id']
            self.persist()
        return self.wait(lambda: self.own_version(label), 'ready')

    def run(self):
        i, v = self.baseline()
        if self.s is None:
            require(self.config['canonical_id'] in v['canonical_ids'], 'Selected document is outside baseline')
            run, chunks = self.chunk_snapshot(v['chunking_run_id'])
            require(run['result_sha256'] == v['chunk_set_sha256'] and run['spec'] == v['input_spec'], 'Baseline ChunkSet differs')
            selected = [c for c in chunks if c['canonical_id'] == self.config['canonical_id']]
            require(0 < len(selected) <= self.config['max_chunks'], 'Select a smaller document; maximum allowed chunks exceeded')
            run_id = str(uuid.uuid4())
            self.s = {'schema': SCHEMA, 'input': self.config, 'run_id': run_id, 'name': 'lifecycle-check-' + run_id,
                      'phase': 0, 'keys': {k: str(uuid.uuid4()) for k in ('create', 'a', 'b')}, 'versions': {},
                      'baseline_index': select(i, INDEX_FIELDS), 'baseline_version': select(v, VERSION_FIELDS),
                      'chunks_digest': digest(chunks), 'selected_chunks': len(selected),
                      'deployment_id': v['embedding_profile']['deployment_id']}
            self.persist()
        self.observer([self.observed(v)])  # Fail preflight before any mutation if unavailable.
        if self.s['phase'] < 1:
            if not self.s.get('index_id'):
                new = self.api('/api/indexes', 'POST', {'name': self.s['name'], 'embedding_deployment_id': self.s['deployment_id']}, self.s['keys']['create'])
                require(valid_uuid(new.get('index_id')) and new.get('name') == self.s['name']
                        and new['index_id'] != i['index_id'], 'Index acceptance differs')
                self.s['index_id'] = new['index_id']
                self.persist()
            self.build('a')
            self.checkpoint(1)
        if self.s['phase'] < 2:
            a = self.own_version('a'); idx = self.own_index()
            if 'activation' not in self.s:
                require(idx['active_version_id'] == '', 'Unexpected active test version')
                self.s['activation'] = {'index_version_id': a['index_version_id'], 'expected_active_version_id': '', 'expected_revision': idx['revision']}
                self.persist()
            if idx['active_version_id'] != a['index_version_id']:
                self.api('/api/indexes/' + idx['index_id'] + '/activate', 'POST', self.s['activation'])
            idx = self.own_index()
            require(idx['active_version_id'] == a['index_version_id'] and idx['revision'] == self.s['activation']['expected_revision'] + 1,
                    'Activation receipt differs')
            self.own_versions()
            try:
                self.api('/api/index-versions/' + a['index_version_id'], 'DELETE', {})
            except APIError as e:
                require(e.status == 409 and e.code == 'ACTIVE_VERSION', 'Unexpected active-delete rejection')
            else:
                raise ValueError('Active version deletion was accepted; stop and preserve journal')
            require(self.own_version('a')['state'] == 'ready', 'Active version was changed')
            self.checkpoint(2)
        if self.s['phase'] < 3:
            self.build('b')
            versions = self.own_versions()
            self.observer([self.observed(v), *[self.observed(x) for x in versions.values()]])
            self.checkpoint(3)
        if self.s['phase'] < 4:
            versions = self.own_versions(); idx = self.own_index()
            require(idx['state'] == 'active' and idx['active_version_id'] == versions['a']['index_version_id'], 'Active test version changed')
            b = versions['b']
            if b['state'] not in ('deleting', 'deleted'):
                self.api('/api/index-versions/' + b['index_version_id'], 'DELETE', {})
            self.wait(lambda: self.own_version('b'), 'deleted')
            versions = self.own_versions()
            require(versions['a']['state'] == 'ready' and versions['a']['reference_state'] == 'held', 'Shared owner was released early')
            self.observer([self.observed(v), *[self.observed(x) for x in versions.values()]])
            self.checkpoint(4)
        if self.s['phase'] < 5:
            self.baseline(); self.own_versions(); idx = self.own_index()
            if 'deletion' not in self.s:
                require(idx['state'] == 'active' and idx['active_version_id'] == self.s['versions']['a'], 'Unexpected test index state')
                self.s['deletion'] = {'expected_active_version_id': idx['active_version_id'], 'expected_revision': idx['revision']}
                self.persist()
            if idx['state'] == 'active':
                self.api('/api/indexes/' + idx['index_id'], 'DELETE', self.s['deletion'])
            self.wait(lambda: self.own_version('a'), 'deleted')
            self.wait(self.own_index, 'deleted')
            self.checkpoint(5)
        # Repeated success runs only observe; they never create fresh resources.
        self.baseline()
        versions = self.own_versions()
        require(self.own_index()['state'] == 'deleted' and self.own_index()['active_version_id'] == '', 'Test index deletion incomplete')
        require(all(x['state'] == 'deleted' and x['reference_state'] == 'released' for x in versions.values()) and len(versions) == 2,
                'Test version cleanup incomplete')
        self.observer([self.observed(v), *[self.observed(x) for x in versions.values()]])
        run, chunks = self.chunk_snapshot(v['chunking_run_id'])
        require(run['result_sha256'] == v['chunk_set_sha256'] and run['spec'] == v['input_spec']
                and digest(chunks) == self.s['chunks_digest'], 'Shared ChunkSet changed')
        receipt = {'status': 'passed', 'scope': 'Two owned versions; live API + independent OpenSearch/Data reads; not retrieval quality',
                   'index_id': self.s['index_id'], 'versions': self.s['versions'], 'baseline_index_id': i['index_id'],
                   'baseline_version_id': v['index_version_id'], 'baseline_unchanged': True,
                   'selected_chunks_per_version': self.s['selected_chunks'], 'test_versions_deleted': 2,
                   'physical_indexes_absent': True, 'data_references': 'released', 'canonical_and_chunks_preserved': True}
        self.s['receipt'] = receipt
        self.checkpoint(6)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:15100')
    parser.add_argument('--baseline-index-version-id', required=True)
    parser.add_argument('--canonical-id', required=True)
    parser.add_argument('--state-file', type=Path, required=True)
    parser.add_argument('--max-chunks', type=int, default=8)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--service-container', default='knowledge-service')
    parser.add_argument('--observer-image', default='knowledge-platform-cleanup-verifier:20260921')
    args = parser.parse_args()
    require(valid_uuid(args.baseline_index_version_id) and valid_uuid(args.canonical_id), 'Invalid input ID')
    require(1 <= args.max_chunks <= 16 and 10 <= args.timeout <= 1800, 'Invalid test budget')
    config = {'base_url': args.base_url.rstrip('/'), 'baseline_version_id': args.baseline_index_version_id,
              'canonical_id': args.canonical_id, 'max_chunks': args.max_chunks}
    path = args.state_file.expanduser().absolute()
    require(not path.is_symlink(), 'Journal must not be a symbolic link')
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + '.lock')
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('This journal is already in use') from None
        api = Client(args.base_url)
        check = Lifecycle(api, None, path, config, args.timeout)
        check.observer = DockerObserver(args.service_container, args.observer_image)
        print(json.dumps(check.run(), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit('FAIL: ' + str(exc)) from None
