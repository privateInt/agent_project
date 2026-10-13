"""Bounded kubectl GET adapter. No cluster writes, shell commands or raw credentials."""
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from urllib.parse import urlencode, urlsplit, parse_qs

from ops_platform.contracts import ReadFailure, require
from ops_platform.transport import base_url, strict_json

PART_OF = 'app.kubernetes.io/part-of'
INSTALLATION = 'ops.agent-project.io/installation'
ROLE = 'ops.agent-project.io/role'
PORT = 'ops.agent-project.io/port'
PROJECT_SELECTOR = PART_OF + '=agent-project'


def name(value):
    require(isinstance(value, str) and len(value) <= 63 and re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', value),
            'INVALID_KUBERNETES_NAME')
    return value


def label(value):
    require(isinstance(value, str) and len(value) <= 63 and re.fullmatch(r'[A-Za-z0-9](?:[-_.A-Za-z0-9]*[A-Za-z0-9])?', value),
            'INVALID_INSTALLATION_LABEL')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def execute(argv, timeout=12, max_bytes=4*1024*1024):
    """Drain both pipes with a deadline and byte caps, never echo command stderr."""
    env = dict(os.environ, KUBECTL_KUBERC='false')
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, start_new_session=True)
    except FileNotFoundError: raise ReadFailure('KUBECTL_UNAVAILABLE') from None
    except OSError: raise ReadFailure('KUBECTL_EXECUTION_FAILED') from None
    complete = False
    chunks = {'out': bytearray(), 'err': bytearray()}
    deadline = time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ, 'out')
            poll.register(process.stderr, selectors.EVENT_READ, 'err')
            while poll.get_map():
                remaining = deadline-time.monotonic(); require(remaining > 0, 'KUBERNETES_TIMEOUT')
                for key, _ in poll.select(min(remaining, .2)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data: poll.unregister(key.fileobj); continue
                    chunks[key.data].extend(data)
                    require(len(chunks[key.data]) <= (max_bytes if key.data == 'out' else 16384), 'KUBERNETES_RESPONSE_LIMIT')
            remaining = deadline-time.monotonic(); require(remaining > 0, 'KUBERNETES_TIMEOUT')
            try: status = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired: raise ReadFailure('KUBERNETES_TIMEOUT') from None
        if status:
            error = chunks['err'].decode('utf-8', errors='replace').lower()
            code = ('KUBERNETES_CONTEXT_REQUIRED' if 'current-context' in error and ('must exist' in error or 'not set' in error) else
                    'KUBERNETES_CONTEXT_INVALID' if 'context' in error and 'does not exist' in error else
                    'KUBERNETES_FORBIDDEN' if 'forbidden' in error else
                    'KUBERNETES_UNAUTHENTICATED' if 'unauthorized' in error or 'must be logged in' in error else
                    'KUBERNETES_API_NOT_FOUND' if 'notfound' in error or 'not found' in error else
                    'KUBERNETES_TIMEOUT' if 'timed out' in error or 'deadline exceeded' in error else 'KUBERNETES_REQUEST_FAILED')
            raise ReadFailure(code)
        complete = True
        return bytes(chunks['out'])
    finally:
        if not complete:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        process.wait()
        process.stdout.close(); process.stderr.close()


class Kubectl:
    def __init__(self, context=None, command=execute, binary='kubectl'):
        if context is not None:
            require(isinstance(context, str) and 0 < len(context) <= 256 and context.isprintable() and not context.startswith('-'),
                    'INVALID_KUBERNETES_CONTEXT')
        self.context, self.command, self.binary = context, command, binary
        self.bound = None
        self.requests = 0

    def binding(self):
        flags = ['--context', self.context] if self.context else []
        raw = strict_json(self.command([self.binary, *flags, 'config', 'view', '--minify', '-o', 'json']))
        require(isinstance(raw, dict), 'KUBERNETES_CONTEXT_INVALID')
        chosen = self.context or raw.get('current-context')
        require(isinstance(chosen, str) and bool(chosen), 'KUBERNETES_CONTEXT_REQUIRED')
        contexts = [c for c in raw.get('contexts', []) if c.get('name') == chosen]
        require(len(contexts) == 1, 'KUBERNETES_CONTEXT_INVALID')
        context = contexts[0]['context']
        clusters = [c for c in raw.get('clusters', []) if c.get('name') == context.get('cluster')]
        require(len(clusters) == 1, 'KUBERNETES_CONTEXT_INVALID')
        cluster = clusters[0]['cluster']; server = cluster['server']; parsed = base_url(server)
        require(parsed.scheme == 'https' and not cluster.get('insecure-skip-tls-verify'), 'KUBERNETES_VERIFIED_TLS_REQUIRED')
        # kubectl config view is redacted; do not request --raw, print users or copy credentials.
        result = {'context': chosen, 'server': server,
                  'fingerprint': digest({'context': chosen, 'cluster_name': context['cluster'], 'cluster': cluster})}
        self.context = chosen
        if self.bound is None: self.bound = result
        require(result == self.bound, 'KUBERNETES_CONTEXT_CHANGED')
        return {'context': chosen, 'api_server_fingerprint': digest(server)}

    def get(self, path):
        require(self.bound is not None, 'KUBERNETES_CONTEXT_REQUIRED')
        parsed = urlsplit(path)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment, 'KUBERNETES_GET_PATH_REFUSED')
        single = re.fullmatch(r'/api/v1/namespaces/([a-z0-9-]+)', parsed.path)
        listing = re.fullmatch(r'/(?:api/v1/(?:namespaces/[a-z0-9-]+/)?services|apis/discovery.k8s.io/v1/namespaces/[a-z0-9-]+/endpointslices)', parsed.path)
        require(bool(single or listing), 'KUBERNETES_GET_PATH_REFUSED')
        query = parse_qs(parsed.query, keep_blank_values=True)
        require(not query if single else set(query) <= {'labelSelector', 'limit', 'continue'} and
                query.get('limit') == ['128'] and len(query.get('labelSelector', [])) == 1,
                'KUBERNETES_GET_PATH_REFUSED')
        self.requests += 1
        require(self.requests <= 512, 'KUBERNETES_REQUEST_LIMIT')
        raw = self.command([self.binary, '--context', self.context, '--server', self.bound['server'],
                            '--request-timeout=8s', '--insecure-skip-tls-verify=false', 'get', '--raw', path])
        body = strict_json(raw)
        require(isinstance(body, dict), 'INVALID_KUBERNETES_RESPONSE')
        return body

    def listing(self, path, selector, kind):
        items, seen, tokens = [], set(), set()
        continuation = None
        for _ in range(16):
            params = {'labelSelector': selector, 'limit': 128}
            if continuation: params['continue'] = continuation
            body = self.get(path + '?' + urlencode(params))
            version = 'discovery.k8s.io/v1' if kind == 'EndpointSlice' else 'v1'
            require(body.get('kind') == kind+'List' and body.get('apiVersion', version) == version
                    and isinstance(body.get('items'), list), 'INVALID_KUBERNETES_RESPONSE')
            for row in body['items']:
                # API list items may omit TypeMeta; the typed list supplies it.
                require(isinstance(row, dict) and row.get('kind', kind) == kind
                        and row.get('apiVersion', version) == version, 'INVALID_KUBERNETES_RESPONSE')
                row = dict(row, kind=kind, apiVersion=version)
                uid = row.get('metadata', {}).get('uid')
                require(isinstance(uid, str) and bool(uid) and uid not in seen, 'KUBERNETES_DUPLICATE_OBJECT')
                seen.add(uid); items.append(row)
            require(len(items) <= 1024, 'KUBERNETES_OBJECT_LIMIT')
            continuation = body.get('metadata', {}).get('continue')
            if not continuation: return items
            require(isinstance(continuation, str) and len(continuation) <= 4096 and continuation not in tokens,
                    'KUBERNETES_PAGINATION_INVALID')
            tokens.add(continuation)
        raise ReadFailure('KUBERNETES_PAGE_LIMIT')

    def services(self, namespaces):
        rows = []
        for ns in namespaces or [None]:
            path = '/api/v1/' + ('namespaces/'+name(ns)+'/' if ns else '') + 'services'
            rows.extend(self.listing(path, PROJECT_SELECTOR, 'Service'))
        require(len(rows) <= 128, 'KUBERNETES_SERVICE_LIMIT')
        return rows

    def slices(self, namespace, service):
        return self.listing('/apis/discovery.k8s.io/v1/namespaces/'+name(namespace)+'/endpointslices',
                            'kubernetes.io/service-name='+name(service), 'EndpointSlice')

    def namespace(self, namespace):
        body = self.get('/api/v1/namespaces/'+name(namespace))
        meta = body.get('metadata', {})
        require(body.get('kind') == 'Namespace' and meta.get('name') == namespace and not meta.get('deletionTimestamp')
                and isinstance(meta.get('uid'), str) and bool(meta['uid']), 'INVALID_NAMESPACE_IDENTITY')
        return meta['uid']
