"""O02: explicit metadata-only OTLP spans, bounded best-effort background export.

No auto-instrumentation, IO bodies, database writes or business retries.
The Agent Service remains authoritative for durable execution state.
"""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import functools
import http.client
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import urllib.parse
import uuid

STEPS = ('validate', 'search', 'context', 'generate', 'finalize')
NAMES = {'agent.execute', 'tool.execute', 'model.chat', 'model.embedding', 'knowledge.search'} | {'step.' + x for x in STEPS}
UUID_KEYS = {'agent_run_id', 'agent_version_id', 'tool_call_id', 'tool_version_id', 'request_id',
             'deployment_id', 'model_version_id', 'connection_version_id', 'index_id', 'index_version_id'}
NUMBER_KEYS = {'returned_count', 'selected_count', 'citation_count', 'input_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens'}
ENUMS = {'state': {'succeeded', 'failed', 'skipped', 'interrupted'},
         'outcome': {'answered', 'no_evidence'}, 'finish_reason': {'stop', 'length'},
         'reason': {'no_search_results', 'runtime_not_reached'},
         'scope': {'runtime_execution'}, 'caller_system': {'n8n'},
         'tool_kind': {'knowledge_search_v1', 'openapi_http_v1', 'diagnostic_echo_v1', 'diagnostic_connection_v1'}}
_current = ContextVar('agent_observation', default=None)
_exporter = None


def valid_id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0
    except (ValueError, TypeError, AttributeError):
        return False


def trace_id(kind, ident):
    if kind not in ('agent', 'tool') or not valid_id(ident):
        raise ValueError('invalid trace identity')
    return hashlib.sha256(('agent-platform.o02:' + kind + ':' + ident).encode()).hexdigest()[:32]


def span_id(trace, key):
    return hashlib.sha256((trace + ':' + key).encode()).hexdigest()[:16]


def filtered(values):
    out = {}
    for key, value in values.items():
        if key in UUID_KEYS and valid_id(value): out[key] = value
        elif key in NUMBER_KEYS and type(value) is int and 0 <= value <= 10**9: out[key] = value
        elif key in ENUMS and isinstance(value, str) and value in ENUMS[key]: out[key] = value
        elif key == 'error_code' and isinstance(value, str) and re.fullmatch(r'(AGENT|TOOL|MODEL|ANSWER|CONTEXT|INPUT|CREDENTIAL)_[A-Z_]{1,60}', value): out[key] = value
        elif key in ('workflow_id', 'execution_id') and isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{1,64}', value): out[key] = value
    return out


def attrs(values):
    return [{'key': k, 'value': {'stringValue': str(v)}} for k, v in values.items()]


def endpoint(value):
    p = urllib.parse.urlsplit(value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('', '/'):
        raise ValueError('invalid tracing endpoint')
    _ = p.port
    return p


class Transport:
    def __init__(self, config):
        self.url = endpoint(config['url'])
        pair = config['public_key'] + ':' + config['secret_key']
        if not all(isinstance(config[k], str) and 8 <= len(config[k]) <= 256 and not any(x.isspace() for x in config[k]) for k in ('public_key', 'secret_key')):
            raise ValueError('invalid tracing keys')
        self.authorization = 'Basic ' + base64.b64encode(pair.encode()).decode()

    def send(self, body):
        # One connection, no redirects, proxies, response bodies in logs or retries here.
        cls = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
        conn = cls(self.url.hostname, self.url.port, timeout=2)
        try:
            conn.request('POST', '/api/public/otel/v1/traces', body, {
                'Authorization': self.authorization, 'Content-Type': 'application/json',
                'x-langfuse-ingestion-version': '4'})
            reply = conn.getresponse()
            # Bound bytes; close rather than drain an untrusted slow/large response.
            raw = reply.read(8193)
            if reply.status != 200:
                return 'retry' if reply.status in (408, 429, 500, 502, 503, 504) else 'rejected'
            if len(raw) > 8192: return 'rejected'
            result = json.loads(raw or b'{}')
            partial = result.get('partialSuccess') or result.get('partial_success') or {}
            rejected = int(partial.get('rejectedSpans', partial.get('rejected_spans', 0)))
            return 'rejected' if rejected else 'accepted'
        finally:
            conn.close()


class Exporter:
    def __init__(self, transport, capacity=256):
        self.transport = transport
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.counts = dict(queued=0, accepted=0, dropped=0, rejected=0, failed=0, retry=0, internal_error=0)
        self.thread = threading.Thread(target=self.work, name='agent-otlp', daemon=True)
        self.thread.start()

    def count(self, key, n=1):
        with self.lock: self.counts[key] += n

    def submit(self, item):
        if self.stop.is_set(): self.count('dropped'); return
        try:
            self.queue.put_nowait(item)
            self.count('queued')
        except queue.Full: self.count('dropped')

    def snapshot(self):
        with self.lock: return dict(self.counts, pending=self.queue.qsize(), enabled=True)

    def work(self):
        last_log = 0
        while not self.stop.is_set() or not self.queue.empty():
            batch = []
            try:
                batch.append(self.queue.get(timeout=.2))
            except queue.Empty: pass
            while batch and len(batch) < 16:
                try: batch.append(self.queue.get_nowait())
                except queue.Empty: break
            if batch:
                try:
                    body = json.dumps({'resourceSpans': [{'resource': {'attributes': attrs({'service.name': 'agent-platform-runtime'})},
                        'scopeSpans': [{'scope': {'name': 'agent.platform.explicit', 'version': 'o02.1'}, 'spans': batch}]}]}, separators=(',', ':')).encode()
                    status = 'retry'
                    for attempt in range(2):
                        try: status = self.transport.send(body)
                        except Exception: status = 'retry'
                        if status != 'retry': break
                        if attempt == 0:
                            self.count('retry', len(batch))
                            if self.stop.wait(.2): break
                    self.count('accepted' if status == 'accepted' else 'rejected' if status == 'rejected' else 'failed', len(batch))
                except Exception: self.count('internal_error', len(batch))
                finally:
                    for _ in batch: self.queue.task_done()
            if time.monotonic() - last_log >= 30:
                last_log = time.monotonic()
                try: print(json.dumps({'event': 'agent_trace_export', **self.snapshot()}), flush=True)
                except Exception: pass

    def close(self, seconds=2):
        self.stop.set()
        self.thread.join(seconds)
        # A stuck DNS/socket never delays business completion or process shutdown.
        # Remaining memory-only spans may be lost; counters are process-local.
        return self.snapshot()


class Span:
    def __init__(self, session, name, kind, parent, key, values):
        self.session, self.name, self.kind, self.parent = session, name, kind, parent
        self.id = span_id(session.trace, key)
        self.start, self.clock = time.time_ns(), time.monotonic_ns()
        self.values = dict(session.base)
        self.update(**values)
        self.closed = False

    def update(self, **values):
        try: self.values.update(filtered(values))
        except Exception: pass

    def finish(self, exc=None):
        if self.closed: return
        self.closed = True
        if exc is not None:
            code = getattr(exc, 'code', None)
            if not isinstance(code, str): code = 'AGENT_EXECUTION_INTERRUPTED'
            self.update(state='interrupted' if code == 'AGENT_EXECUTION_INTERRUPTED' else 'failed', error_code=code)
        elif 'state' not in self.values: self.update(state='succeeded')
        values = {'langfuse.trace.name': 'agent.execute' if self.session.agent else 'tool.execute',
                  'langfuse.environment': 'agent-platform', 'langfuse.observation.type': self.kind}
        values.update({'langfuse.observation.metadata.' + k: v for k, v in self.values.items()})
        if 'agent_run_id' in self.values: values['langfuse.trace.metadata.agent_run_id'] = self.values['agent_run_id']
        # Deployment-specific model identity avoids guessed public-model pricing.
        if self.kind == 'generation' and 'deployment_id' in self.values:
            values['langfuse.observation.model.name'] = 'deployment-' + self.values['deployment_id']
        if self.kind == 'generation' and all(k in self.values for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
            if self.values['prompt_tokens'] + self.values['completion_tokens'] == self.values['total_tokens']:
                values['langfuse.observation.usage_details'] = json.dumps({'input': self.values['prompt_tokens'], 'output': self.values['completion_tokens'], 'total': self.values['total_tokens']})
        failed = self.values.get('state') in ('failed', 'interrupted')
        item = {'traceId': self.session.trace, 'spanId': self.id, 'name': self.name, 'kind': 1,
                'startTimeUnixNano': str(self.start), 'endTimeUnixNano': str(self.start + max(0, time.monotonic_ns() - self.clock)),
                'attributes': attrs(values), 'status': {'code': 2 if failed else 1, 'message': self.values.get('error_code', self.values.get('state', 'succeeded'))}}
        if self.parent: item['parentSpanId'] = self.parent
        self.session.exporter.submit(item)


class NullSpan:
    def update(self, **values): pass


class Session:
    def __init__(self, kind, ident, exporter, values, parent_run=None):
        self.agent = kind == 'agent' or valid_id(parent_run)
        self.trace = trace_id('agent', parent_run) if valid_id(parent_run) else trace_id(kind, ident)
        self.exporter, self.base = exporter, filtered(dict(values, scope='runtime_execution'))
        if valid_id(parent_run): self.base['agent_run_id'] = parent_run
        self.prefix = '' if kind == 'agent' else 'tool:' + ident + ':'
        self.parent = span_id(self.trace, 'step.search') if valid_id(parent_run) else None
        self.root_name = kind + '.execute'
        self.seen = set()
        self.lock = threading.Lock()

    def start(self, name, kind, parent, values):
        if name not in NAMES: raise ValueError('unsupported span')
        with self.lock:
            if name in self.seen or len(self.seen) >= 16: return None
            self.seen.add(name)
        return Span(self, name, kind, parent, self.prefix + name, values)


@contextmanager
def span(name, kind='span', **values):
    saved = _current.get()
    item = token = None
    try:
        if saved:
            session, parent = saved
            item = session.start(name, kind, parent, values)
            if item: token = _current.set((session, item.id))
    except Exception: pass
    error = None
    try:
        yield item or NullSpan()
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            if item: item.finish(error)
        except Exception: pass
        finally:
            if token is not None: _current.reset(token)


@contextmanager
def execution(kind, ident, parent_run=None, **values):
    session = token = None
    try:
        if _exporter:
            session = Session(kind, ident, _exporter, values, parent_run)
            token = _current.set((session, session.parent))
    except Exception: pass
    try:
        with span(kind + '.execute', kind='agent' if kind == 'agent' else 'tool') as root:
            try: yield root
            finally:
                if kind == 'agent' and session:
                    for step in STEPS:
                        if 'step.' + step not in session.seen:
                            with span('step.' + step, state='skipped', reason='runtime_not_reached'): pass
    finally:
        if token is not None: _current.reset(token)


def initialize():
    global _exporter
    try:
        path = os.environ.get('AGENT_TRACE_CONFIG_FILE')
        if not path: return
        raw = Path(path).read_bytes()
        if len(raw) > 4096: raise ValueError()
        config = json.loads(raw)
        if config.get('enabled') is not True: return
        _exporter = Exporter(Transport(config))
        print(json.dumps({'event': 'agent_trace_config', 'enabled': True, 'metadata_only': True}), flush=True)
    except Exception:
        _exporter = None
        print(json.dumps({'event': 'agent_trace_config', 'enabled': False, 'error_code': 'TRACE_CONFIG_INVALID'}), flush=True)


def shutdown():
    global _exporter
    exporter, _exporter = _exporter, None
    if exporter:
        try: print(json.dumps({'event': 'agent_trace_shutdown', **exporter.close()}), flush=True)
        except Exception: pass


def rpc(kind):
    def decorate(fn):
        @functools.wraps(fn)
        def call(self, request, context):
            ident = getattr(request, 'agent_run_id' if kind == 'agent' else 'tool_call_id', '')
            if not valid_id(ident): return fn(self, request, context)
            metadata = dict(context.invocation_metadata())
            values = {'request_id': metadata.get('x-request-id'),
                      ('agent_run_id' if kind == 'agent' else 'tool_call_id'): ident,
                      ('agent_version_id' if kind == 'agent' else 'tool_version_id'): getattr(request, 'agent_version_id' if kind == 'agent' else 'tool_version_id')}
            parent = None
            if kind == 'tool':
                values['tool_kind'] = request.kind
                if metadata.get('x-agent-parent-step-id') == 'search': parent = metadata.get('x-agent-parent-run-id')
            else:
                try:
                    caller = json.loads(request.snapshot_json).get('caller_context', {})
                    if caller.get('system') == 'n8n':
                        values.update(caller_system='n8n', workflow_id=caller.get('workflow_id'), execution_id=caller.get('execution_id'))
                except Exception: pass
            with execution(kind, ident, parent_run=parent, **values) as observation:
                result = fn(self, request, context)
                code = result.error_code
                observation.update(state=('interrupted' if code == 'AGENT_EXECUTION_INTERRUPTED' else 'failed') if code else 'succeeded', error_code=code)
                return result
        return call
    return decorate
