"""Q02: bounded GET discovery and own defaults; no model or service lifecycle.

inspect() is the reusable readiness API for the later integration server. The
CLI is an operator adapter. Neither starts a server or invokes inference.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

from .contracts import ContractError, strict_json, uid

ROLES = ('data', 'knowledge', 'agent', 'model')
REPOSITORY = 'dragonkue/snowflake-arctic-embed-l-v2.0-ko'
NON_CHAT = {REPOSITORY, 'Dongjin-kr/ko-reranker'}
MAX_RESPONSE = 2 << 20
MAX_PAGES = 10
MAX_CALLS = 100


class ReadinessError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def check(ok, code='RESPONSE_INVALID'):
    if not ok:
        raise ReadinessError(code)


def identifier(value):
    try:
        check(uid(value), 'IDENTITY_MISMATCH')
    except (ContractError, TypeError, ValueError):
        raise ReadinessError('IDENTITY_MISMATCH') from None
    return value


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def digest(value, prefix=''):
    return isinstance(value, str) and re.fullmatch(re.escape(prefix) + r'[a-f0-9]{64}', value) is not None


def origin(value):
    check(isinstance(value, str) and len(value) <= 2048 and not any(ord(c) < 33 for c in value), 'CONNECTION_INVALID')
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError:
        raise ReadinessError('CONNECTION_INVALID') from None
    check(p.scheme in ('http', 'https') and p.hostname and not p.username and not p.password
          and not p.query and not p.fragment and p.path in ('', '/')
          and (port is None or 1 <= port <= 65535), 'CONNECTION_INVALID')
    return value.rstrip('/')


def read_file(path, limit=65536, credential=False):
    """Existing file references only. Do not chmod, follow final symlink or log bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            check(stat.S_ISREG(info.st_mode) and info.st_size <= limit and info.st_nlink == 1,
                  'CREDENTIAL_UNAVAILABLE' if credential else 'CONFIG_INVALID')
            if credential:
                check(info.st_uid == os.getuid() and not info.st_mode & 0o022, 'CREDENTIAL_UNAVAILABLE')
            data = stream.read(limit + 1)
            check(len(data) <= limit, 'CREDENTIAL_UNAVAILABLE' if credential else 'CONFIG_INVALID')
            return data
    except OSError:
        raise ReadinessError('CREDENTIAL_UNAVAILABLE' if credential else 'CONFIG_UNAVAILABLE') from None


def decode(raw):
    try:
        return strict_json(raw)
    except (ContractError, ValueError, RecursionError):
        raise ReadinessError('RESPONSE_INVALID') from None


def connections(rows, relative_to):
    check(isinstance(rows, dict) and set(rows) == set(ROLES), 'CONFIG_INVALID')
    out = {}
    for role, row in rows.items():
        check(isinstance(row, dict) and set(row) <= {'url', 'token_file', 'enabled'} and 'url' in row, 'CONFIG_INVALID')
        check(type(row.get('enabled', True)) is bool, 'CONFIG_INVALID')
        item = {'url': origin(row['url']), 'enabled': row.get('enabled', True)}
        if row.get('token_file'):
            check(isinstance(row['token_file'], str) and '\x00' not in row['token_file'], 'CONFIG_INVALID')
            path = Path(row['token_file'])
            item['token_file'] = str(path if path.is_absolute() else relative_to / path)
        check(role != 'agent' or item.get('token_file'), 'CREDENTIAL_NOT_CONFIGURED')
        out[role] = item
    return out


def load_settings(path, source):
    """Reuse ONLY platform origins/auth references from the existing Ops host config.

    No Ops imports, discovery, ownership records, service commands or credentials
    other than the selected gateway bearer file. Explicit QA config is portable.
    """
    path, source = Path(path), Path(source)
    if path.exists() or path.is_symlink():
        raw = read_file(path)
        value = decode(raw)
        check(set(value) == {'schema', 'platforms', 'library_name', 'defaults'} and type(value['schema']) is int
              and value['schema'] == 1 and isinstance(value['defaults'], dict), 'CONFIG_INVALID')
        value['platforms'] = connections(value['platforms'], path.parent)
        check(set(value['defaults']) <= {'embedding', 'chat', 'chunker'}, 'CONFIG_INVALID')
        check(all(isinstance(x, dict) and bool(x) for x in value['defaults'].values()), 'CONFIG_INVALID')
        basis = hashlib.sha256(raw).hexdigest()
        provenance = 'document_qa_settings'
    else:
        original = decode(read_file(source))
        value = {'schema': 1, 'platforms': connections(original.get('platforms'), source.parent),
                 'library_name': '문서 자료함', 'defaults': {}}
        basis, provenance = None, 'existing_platform_references'
    check(isinstance(value['library_name'], str) and value['library_name'].strip()
          and len(value['library_name'].encode()) <= 800, 'CONFIG_INVALID')
    return value, basis, provenance


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayReader:
    """GET allowlist, no proxy/redirect/retry, shared time/call/size budgets."""
    def __init__(self, rows, budget_seconds=50, timeout=8, clock=time.monotonic):
        self.rows, self.clock = rows, clock
        self.deadline = clock() + budget_seconds
        self.timeout, self.calls, self.credential_reads = timeout, 0, 0
        self.tokens = {}
        self.opener = build_opener(ProxyHandler({}), NoRedirect)

    def get(self, role, path, query=None):
        allowed = (role == 'data' and path in ('/health', '/api/source-documents')
                   or role == 'knowledge' and path in ('/health', '/api/chunkers')
                   or role == 'agent' and (path in ('/readyz', '/api/connections')
                       or re.fullmatch(r'/api/connection-versions/[a-f0-9-]{36}', path))
                   or role == 'model' and (path in ('/health', '/api/deployments')
                       or re.fullmatch(r'/api/(deployments|model-versions|models)/[a-f0-9-]{36}(?:/embedding-profile)?', path)))
        check(allowed and (not query or path in ('/api/source-documents', '/api/connections', '/api/deployments')),
              'READ_PATH_NOT_ALLOWED')
        check(self.calls < MAX_CALLS and self.deadline > self.clock(), 'DISCOVERY_BUDGET_EXCEEDED')
        row = self.rows[role]
        check(row.get('enabled', True), 'CONNECTION_DISABLED')
        base = origin(row['url'])
        headers = {'Accept': 'application/json'}
        if row.get('token_file'):
            if role not in self.tokens:
                raw = read_file(row['token_file'], 8192, credential=True)
                try:
                    token = raw.decode('ascii').strip()
                except UnicodeError:
                    raise ReadinessError('CREDENTIAL_UNAVAILABLE') from None
                check(bool(re.fullmatch(r'[!-~]{1,4096}', token)), 'CREDENTIAL_UNAVAILABLE')
                self.tokens[role] = token
                self.credential_reads += 1
            headers['Authorization'] = 'Bearer ' + self.tokens[role]
        request = Request(base + path + ('?' + urlencode(query) if query else ''), headers=headers, method='GET')
        self.calls += 1
        try:
            with self.opener.open(request, timeout=min(self.timeout, self.deadline - self.clock())) as response:
                check(response.status == 200, 'HTTP_UNAVAILABLE')
                check(response.headers.get_content_type() == 'application/json', 'RESPONSE_INVALID')
                chunks, total = [], 0
                while True:
                    check(self.deadline > self.clock(), 'DISCOVERY_BUDGET_EXCEEDED')
                    part = response.read1(min(65536, MAX_RESPONSE + 1 - total))
                    if not part:
                        break
                    chunks.append(part)
                    total += len(part)
                    check(total <= MAX_RESPONSE, 'RESPONSE_TOO_LARGE')
                data = b''.join(chunks)
                check(len(data) <= MAX_RESPONSE, 'RESPONSE_TOO_LARGE')
                check(self.deadline > self.clock(), 'DISCOVERY_BUDGET_EXCEEDED')
                return decode(data)
        except HTTPError as exc:
            code = ('AUTH_REQUIRED' if exc.code == 401 else 'PERMISSION_DENIED' if exc.code == 403
                    else 'RESOURCE_NOT_FOUND' if exc.code == 404 else 'MODEL_NOT_READY' if exc.code == 409
                    else 'REDIRECT_REFUSED' if 300 <= exc.code < 400 else 'HTTP_UNAVAILABLE')
            exc.close()
            raise ReadinessError(code) from None
        except (TimeoutError, URLError, OSError, HTTPException):
            raise ReadinessError('CONNECTION_UNAVAILABLE') from None


def pages(reader, role, path, id_key):
    items, seen, cursors, cursor = [], set(), set(), ''
    for _ in range(MAX_PAGES):
        page = reader.get(role, path, {'limit': 100, **({'cursor': cursor} if cursor else {})})
        check(isinstance(page.get('items'), list) and len(page['items']) <= 100)
        for item in page['items']:
            check(isinstance(item, dict))
            identity = identifier(item.get(id_key))
            check(identity not in seen, 'INVENTORY_CHANGED')
            seen.add(identity)
            items.append(item)
        cursor = page.get('next_cursor', '')
        check(isinstance(cursor, str) and (cursor == '' or re.fullmatch(r'[A-Za-z0-9_-]{1,2048}', cursor)))
        if not cursor:
            return items
        check(cursor not in cursors, 'CURSOR_REPEATED')
        cursors.add(cursor)
    raise ReadinessError('INVENTORY_INCOMPLETE')


def live_deployment(dep, expected=None):
    check(isinstance(dep, dict))
    identifier(dep.get('deployment_id'))
    identifier(dep.get('model_version_id'))
    check(expected is None or dep['deployment_id'] == expected, 'IDENTITY_MISMATCH')
    check(dep.get('status') == 'ready' and dep.get('runtime_state') == 'ready'
          and dep.get('desired_state') == 'running' and not dep.get('deleted_at'), 'MODEL_NOT_READY')
    check(dep.get('served_model_name') == 'deployment-' + dep['deployment_id'], 'IDENTITY_MISMATCH')


def embedding_profile(dep, profile):
    """Mirror Knowledge ValidateProfile plus canonical ID and deployment binding."""
    live_deployment(dep)
    check(isinstance(profile, dict))
    check(profile.get('deployment_id') == dep['deployment_id'] and profile.get('model_version_id') == dep['model_version_id'], 'IDENTITY_MISMATCH')
    check(all(profile.get(k) == dep.get(k) for k in ('runtime_image_id', 'runtime_container_id')), 'IDENTITY_MISMATCH')
    check(isinstance(profile.get('resolved_revision'), str) and bool(profile['resolved_revision']), 'EMBEDDING_PROFILE_INCOMPATIBLE')
    exact = {'capability': 'embedding', 'status': 'ready', 'profile_id': 'snowflake-ko-v1', 'repo_id': REPOSITORY,
             'query_prefix': 'query: ', 'document_prefix': '', 'dimension': 1024, 'normalization': 'l2',
             'pooling': 'cls', 'truncation': 'reject', 'served_model_name': dep['served_model_name']}
    # Protojson may omit the empty document_prefix; it has the same proto value.
    check(all(profile.get(k, '' if k == 'document_prefix' else None) == v for k, v in exact.items())
          and type(profile.get('dimension')) is int and digest(profile.get('embedding_space_id'), 'es_')
          and all(digest(profile.get(k)) for k in ('source_manifest_sha256', 'tokenizer_sha256', 'runtime_container_id'))
          and digest(profile.get('runtime_image_id'), 'sha256:')
          and integer(profile.get('max_input_tokens'), 128, 8192)
          and integer(profile.get('max_batch_size'), 1, 16)
          and integer(profile.get('max_input_bytes'), 1, 131072), 'EMBEDDING_PROFILE_INCOMPATIBLE')
    return {k: profile[k] for k in ('deployment_id', 'model_version_id', 'embedding_space_id', 'profile_id',
                                   'max_input_tokens', 'max_input_bytes')}


def chat_identity(reader, dep):
    live_deployment(dep)
    serving = dep.get('serving') or {}
    check(isinstance(serving, dict))
    check(serving.get('engine') == 'vllm' and serving.get('profile', '') == ''
          and integer(serving.get('max_model_len'), 1, 1 << 30), 'CHAT_UNSUPPORTED')
    check(dep.get('internal_endpoint') == 'http://model-serving-' + dep['deployment_id'] + ':8000/v1', 'IDENTITY_MISMATCH')
    version = reader.get('model', '/api/model-versions/' + dep['model_version_id'])
    check(version.get('model_version_id') == dep['model_version_id'], 'IDENTITY_MISMATCH')
    identifier(version.get('model_id'))
    check(isinstance(version.get('repo_id'), str))
    check(version['repo_id'] not in NON_CHAT, 'CHAT_UNSUPPORTED')
    model = reader.get('model', '/api/models/' + version['model_id'])
    check(model.get('model_id') == version['model_id'], 'IDENTITY_MISMATCH')
    check(model.get('model_type') == 'llm' and not model.get('deleted_at'), 'CHAT_UNSUPPORTED')
    name = model.get('name') or version.get('repo_id') or '답변 모델'
    check(isinstance(name, str) and isinstance(version.get('version', ''), str))
    return {'deployment_id': dep['deployment_id'], 'model_version_id': dep['model_version_id'],
            'model_id': model['model_id'], 'max_model_len': serving['max_model_len'],
            'name': name[:200], 'version': version.get('version', '')[:100]}


def select(candidates, pinned=None, choice=None):
    if choice:
        matches = [x for x in candidates if choice in (x['label'], x['name'])]
        check(len(matches) == 1, 'SELECTION_AMBIGUOUS' if len(matches) > 1 else 'SELECTION_UNAVAILABLE')
        return matches[0]
    if pinned:
        check(isinstance(pinned, dict) and 'deployment_id' in pinned, 'CONFIG_INVALID')
        matches = [x for x in candidates if x['deployment_id'] == pinned['deployment_id']]
        check(len(matches) == 1, 'SELECTED_MODEL_UNAVAILABLE')
        current = matches[0]
        check(all(current.get(k) == pinned.get(k) for k in ('model_version_id',)
                  + (('embedding_space_id',) if 'embedding_space_id' in current else ())), 'SELECTED_MODEL_CHANGED')
        return current
    check(len(candidates) == 1, 'NO_COMPATIBLE_MODEL' if not candidates else 'SELECTION_REQUIRED')
    return candidates[0]


def chunker(catalog, pinned, profile):
    check(isinstance(catalog.get('items'), list))
    specs = catalog['items']
    check(all(isinstance(x, dict) and isinstance(x.get('id'), str) for x in specs))
    check(len({x['id'] for x in specs}) == len(specs), 'RESPONSE_INVALID')
    preset = pinned or {'id': 'structure_v1', 'options': {'max_characters': 1000, 'overlap_characters': 100}}
    check(isinstance(preset, dict) and set(preset) <= {'id', 'version', 'options'}, 'CONFIG_INVALID')
    found = [x for x in specs if x['id'] == preset.get('id')]
    check(len(found) == 1, 'CHUNKER_UNAVAILABLE')
    spec, options = found[0], preset.get('options')
    check(isinstance(options, dict) and set(options) == {'max_characters', 'overlap_characters'}, 'CHUNKER_OPTIONS_INVALID')
    check(integer(spec.get('min_characters'), 100, 8000) and integer(spec.get('max_characters'), spec['min_characters'], 8000)
          and isinstance(spec.get('version'), str) and bool(spec['version']))
    check('version' not in preset or spec['version'] == preset['version'], 'CHUNKER_VERSION_CHANGED')
    size, overlap = options['max_characters'], options['overlap_characters']
    check(integer(size, spec['min_characters'], spec['max_characters']) and integer(overlap, 0, 3999)
          and overlap * 2 < size, 'CHUNKER_OPTIONS_INVALID')
    check(4 * size <= profile['max_input_bytes'], 'CHUNKER_BYTE_LIMIT_INCOMPATIBLE')
    return {'id': spec['id'], 'version': spec['version'], 'options': dict(options)}


def inspect(settings, reader, choices=None):
    """Return safe projections only. An incomplete inventory never auto-selects."""
    choices = choices or {}
    report = {'schema': 1, 'stage': 'Q02-connections-and-defaults', 'result': 'attention_required',
              'defaults_ready': False, 'question_ready': False, 'library_name': settings['library_name'],
              'connections': {}, 'candidates': {'embedding': [], 'chat': []}, 'excluded_models': [],
              'defaults': {}, 'blockers': [], 'agent_connection_names': [],
              'deferred': ['n8n_document_preparation_Q03b', 'agent_runtime_connections_Q06',
                           'optional_OCR_Q04', 'optional_reranker_Q06', 'token_fit_on_actual_chunks_Q05'],
              'effects': {'configuration_written': False, 'service_control_requested': False,
                          'model_lifecycle_requested': False, 'inference_requested': False,
                          'upload_requested': False, 'api_methods': ['GET']}}
    def blocked(scope, exc):
        report['blockers'].append({'scope': scope, 'code': exc.code})
    for role in ROLES:
        try:
            health = reader.get(role, '/readyz' if role == 'agent' else '/health')
            check(health.get('status') == ('ready' if role == 'agent' else 'ok'))
            if role == 'data':
                listing = reader.get(role, '/api/source-documents', {'limit': 1})
                check(isinstance(listing.get('items'), list) and len(listing['items']) <= 1)
            report['connections'][role] = {'status': 'reachable'}
        except ReadinessError as exc:
            report['connections'][role] = {'status': 'unavailable', 'code': exc.code}
            blocked(role, exc)
    if report['connections']['agent']['status'] == 'reachable':
        try:
            items = pages(reader, 'agent', '/api/connections', 'connection_id')
            for item in items:
                identifier(item.get('current_version_id'))
                check(isinstance(item.get('display_name'), str))
                report['agent_connection_names'].append({'name': item['display_name'][:200],
                                                        'connection_version_id': item['current_version_id']})
        except ReadinessError as exc:
            blocked('agent_connections', exc)
    inventory_complete = False
    if report['connections']['model']['status'] == 'reachable':
        try:
            deps = pages(reader, 'model', '/api/deployments', 'deployment_id')
            for listed in deps:
                deployment_id = listed['deployment_id']
                if (listed.get('status'), listed.get('runtime_state'), listed.get('desired_state')) != ('ready', 'ready', 'running'):
                    report['excluded_models'].append({'deployment_id': deployment_id, 'code': 'MODEL_NOT_READY'})
                    continue
                dep = reader.get('model', '/api/deployments/' + deployment_id)
                live_deployment(dep, deployment_id)
                check(dep['model_version_id'] == listed.get('model_version_id'), 'INVENTORY_CHANGED')
                serving = dep.get('serving') or {}
                check(isinstance(serving, dict))
                if serving.get('engine') != 'vllm':
                    continue
                # A legacy embedding deployment may have an empty serving profile.
                if serving.get('profile', '') in ('', 'snowflake-ko-v1'):
                    try:
                        p = reader.get('model', '/api/deployments/' + deployment_id + '/embedding-profile')
                    except ReadinessError as exc:
                        if exc.code != 'MODEL_NOT_READY':
                            raise
                    else:
                        try:
                            item = embedding_profile(dep, p)
                        except ReadinessError as exc:
                            if exc.code != 'EMBEDDING_PROFILE_INCOMPATIBLE':
                                raise
                            report['excluded_models'].append({'deployment_id': deployment_id, 'code': exc.code})
                        else:
                            item.update(name=REPOSITORY, version=p.get('resolved_revision', '')[:100])
                            report['candidates']['embedding'].append(item)
                if serving.get('profile', '') == '':
                    try:
                        item = chat_identity(reader, dep)
                    except ReadinessError as exc:
                        if exc.code != 'CHAT_UNSUPPORTED':
                            raise
                    else:
                        report['candidates']['chat'].append(item)
            inventory_complete = True
        except ReadinessError as exc:
            blocked('model_inventory', exc)
    for role in ('embedding', 'chat'):
        items = report['candidates'][role]
        for item in items:
            item['label'] = item['name'] + ' / ' + item['version'] + ' / ' + item['deployment_id'][:8]
        items.sort(key=lambda x: x['deployment_id'])
        if inventory_complete:
            try:
                report['defaults'][role] = copy.deepcopy(select(items, settings['defaults'].get(role), choices.get(role)))
            except ReadinessError as exc:
                blocked(role, exc)
    if report['connections']['knowledge']['status'] == 'reachable':
        try:
            catalog = reader.get('knowledge', '/api/chunkers')
            if 'embedding' in report['defaults']:
                report['defaults']['chunker'] = chunker(catalog, settings['defaults'].get('chunker'), report['defaults']['embedding'])
        except ReadinessError as exc:
            blocked('chunker', exc)
    ready = not report['blockers'] and set(report['defaults']) == {'embedding', 'chat', 'chunker'}
    report.update(defaults_ready=ready, result='passed' if ready else 'attention_required',
                  next_step='Q03a_upload_and_job_acceptance' if ready else 'resolve_reported_defaults_or_connections')
    report['effects'].update(api_requests=getattr(reader, 'calls', 0),
                             credential_contents_read=getattr(reader, 'credential_reads', 0) > 0)
    return report


def save_settings(path, settings, report, expected_sha256):
    """CAS and atomic write in this application's own config only."""
    check(report['defaults_ready'], 'DEFAULTS_NOT_READY')
    path = Path(path)
    check(path.name == 'settings.json', 'SETTINGS_PATH_INVALID')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    check(not path.parent.is_symlink() and not path.parent.stat().st_mode & 0o077, 'SETTINGS_DIRECTORY_NOT_PRIVATE')
    lock = path.parent / 'settings.lock'
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise ReadinessError('SETTINGS_BUSY') from None
    os.close(fd)
    temporary = None
    try:
        current = hashlib.sha256(read_file(path)).hexdigest() if path.exists() or path.is_symlink() else None
        check(current == expected_sha256, 'SETTINGS_CHANGED')
        output = copy.deepcopy(settings)
        output['defaults'] = copy.deepcopy(report['defaults'])
        raw = (json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
        if current == hashlib.sha256(raw).hexdigest():
            return False
        fd, temporary = tempfile.mkstemp(prefix='.settings-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'configure'))
    parser.add_argument('--settings', type=Path, default=Path.home() / '.local/state/agent-project-document-qa/settings.json')
    parser.add_argument('--connections-from', type=Path, default=Path.home() / '.local/state/agent-project-ops/server/host-config.json')
    parser.add_argument('--embedding', help='exact candidate name or displayed label')
    parser.add_argument('--chat', help='exact candidate name or displayed label')
    args = parser.parse_args(argv)
    try:
        settings, expected, provenance = load_settings(args.settings, args.connections_from)
        reader = GatewayReader(settings['platforms'])
        report = inspect(settings, reader, {'embedding': args.embedding, 'chat': args.chat})
        report['configuration_source'] = provenance
        if args.action == 'configure' and report['defaults_ready']:
            report['effects']['configuration_written'] = save_settings(args.settings, settings, report, expected)
        report['action'] = args.action
    except (ReadinessError, OSError) as exc:
        report = {'schema': 1, 'stage': 'Q02-connections-and-defaults', 'action': args.action,
                  'result': 'attention_required', 'defaults_ready': False, 'question_ready': False,
                  'code': exc.code if isinstance(exc, ReadinessError) else 'LOCAL_SETTINGS_WRITE_UNCERTAIN',
                  'configuration_written': None if isinstance(exc, OSError) else False}
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['result'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
