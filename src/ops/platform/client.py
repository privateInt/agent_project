"""Source-specific observation envelopes reusable by the later O03 API."""
from datetime import datetime, timezone
import hashlib
import urllib.parse

from .contracts import (RESOURCES, ReadFailure, require, resource, project,
                        identifier, machine, project_steps)
from .transport import Transport


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def cursor_value(value):
    require(isinstance(value, str) and len(value) <= 2048
            and all(33 <= ord(c) <= 126 for c in value), 'INVALID_CURSOR')
    return value


class Client:
    def __init__(self, platform, url, token_file=None, timeout=5, success_times=None):
        require(platform in RESOURCES, 'INVALID_CONFIG')
        require(platform != 'agent' or bool(token_file), 'CREDENTIAL_UNAVAILABLE')
        self.platform = platform
        self.transport = Transport(url, token_file, timeout)
        self.success_times = success_times if success_times is not None else {}
        self.binding = hashlib.sha256((platform+'|'+url.rstrip('/')).encode()).hexdigest()

    def _read(self, path, convert, allow_unready=False):
        observed = now()
        key = hashlib.sha256((self.binding+'|'+path).encode()).hexdigest()
        result = {'status': 'error', 'source_platform': self.platform, 'source_path': path,
                  'checked_at': observed, 'observed_at': None,
                  'last_success_at': self.success_times.get(key), 'freshness': 'unavailable',
                  'data': None, 'error': None}
        try:
            http_status, raw = self.transport.get(path, allow_unready=allow_unready)
            data = convert(raw, http_status)
            completed = now()
            self.success_times[key] = completed
            result.update(status='ok', data=data, observed_at=completed, last_success_at=completed,
                          freshness='fresh', http_status=http_status)
        except ReadFailure as exc:
            result['error'] = {'code': exc.code, 'http_status': exc.http_status}
        return result

    def health(self):
        path = '/healthz' if self.platform == 'agent' else '/health'
        def convert(raw, code):
            require(raw.get('status') == 'ok')
            if self.platform == 'agent':
                require(raw.get('platform') == 'agent', 'IDENTITY_MISMATCH')
            if self.platform == 'model':
                require(raw.get('service') == 'model-gateway', 'IDENTITY_MISMATCH')
            return {'gateway': 'responding', 'scope': 'gateway_only', 'business_success': 'not_assessed'}
        return self._read(path, convert)

    def readiness(self):
        if self.platform != 'agent':
            return {'status': 'unsupported', 'source_platform': self.platform,
                    'data': None, 'reason': 'NO_DEPENDENCY_READINESS_ENDPOINT',
                    'last_success_at': None}
        def convert(raw, status):
            require(raw.get('platform') == 'agent' and type(raw.get('ready')) is bool)
            require(status == (200 if raw['ready'] else 503))
            return {'ready': raw['ready'], 'scope': 'agent_service_database_runtime',
                    'database': machine(raw.get('database')), 'runtime': machine(raw.get('runtime')),
                    'request_id': identifier(raw.get('request_id'))}
        return self._read('/api/status', convert, allow_unready=True)

    def page(self, name, limit=20, cursor='', parent_id=None):
        spec = resource(self.platform, name)
        require(type(limit) is int and 1 <= limit <= 100, 'INVALID_LIMIT')
        cursor_value(cursor)
        if not spec.listable:
            return {'status': 'unsupported', 'data': None, 'reason': 'DETAIL_ONLY_API', 'last_success_at': None}
        if spec.parent:
            require(parent_id is not None, 'PARENT_REQUIRED')
            identifier(parent_id)
            path = '/api/indexes/'+parent_id+'/versions'
        else:
            require(parent_id is None, 'UNEXPECTED_PARENT')
            path = '/api/'+name
        params = {'limit': limit}
        if cursor:
            params['cursor'] = cursor
        path += '?'+urllib.parse.urlencode(params)
        def convert(raw, code):
            require(isinstance(raw.get('items'), list) and len(raw['items']) <= limit)
            items = [project(self.platform, name, item) for item in raw['items']]
            require(len({i['id'] for i in items}) == len(items), 'DUPLICATE_IDENTITY')
            if parent_id:
                require(all(i['identifiers'].get('index_id') == parent_id for i in items), 'PARENT_MISMATCH')
            next_cursor = cursor_value(raw.get('next_cursor', ''))
            require(not next_cursor or (bool(items) and next_cursor != cursor), 'CURSOR_LOOP')
            return {'items': items, 'next_cursor': next_cursor, 'page_count': len(items),
                    'has_more': bool(next_cursor), 'total_count': None,
                    'order': 'source_order', 'scope': 'one_page_not_global_totals'}
        return self._read(path, convert)

    def detail(self, name, ident, related=True):
        spec = resource(self.platform, name)
        identifier(ident)
        path = '/api/'+name+'/'+ident
        out = self._read(path, lambda raw, code: project(self.platform, name, raw, ident))
        if out['status'] != 'ok' or not related:
            return out
        linked = {}
        item = out['data']
        operation_id = item['identifiers'].get('operation_id')
        if name != 'operations' and operation_id and self.platform in ('model', 'data', 'knowledge'):
            linked['operation'] = self.detail('operations', operation_id, related=False)
            op = linked['operation']
            if op['status'] == 'ok':
                # Do not join an operation which explicitly identifies another resource.
                op_resource = op['data']['identifiers'].get('resource_id')
                if op_resource and op_resource != ident:
                    linked['operation'] = self._mismatch(op, 'RESOURCE_MISMATCH')
        if self.platform == 'agent' and name == 'agent-runs':
            linked['steps'] = self._read(path+'/steps', lambda raw, code: project_steps(raw))
            tool_id = item['identifiers'].get('tool_call_id')
            if tool_id:
                tool = self.detail('tool-calls', tool_id, related=False)
                if tool['status'] == 'ok' and (tool['data']['identifiers'].get('parent_agent_run_id') != ident
                                               or tool['data']['parent_step_id'] != 'search'):
                    tool = self._mismatch(tool, 'PARENT_MISMATCH')
                linked['tool_call'] = tool
        out['related'] = linked
        if any(r['status'] != 'ok' for r in linked.values()):
            out['status'] = 'partial'
        out['snapshot_consistency'] = 'sequential_reads_not_transactional'
        return out

    @staticmethod
    def _mismatch(envelope, code):
        # A fetched record may be valid by itself but cannot be attached here.
        return {**envelope, 'status': 'error', 'data': None, 'freshness': 'unavailable',
                'error': {'code': code, 'http_status': envelope.get('http_status')}}

    def walk(self, name, limit=20, max_pages=5, parent_id=None):
        """Bounded traversal. Partial coverage is explicit, including on later-page failure."""
        require(type(max_pages) is int and 1 <= max_pages <= 20, 'INVALID_PAGE_BUDGET')
        items, ids, cursors, cursor, pages = [], set(), set(), '', 0
        while pages < max_pages:
            page = self.page(name, limit, cursor, parent_id)
            if page['status'] != 'ok':
                return {'status': 'partial' if items else page['status'], 'items': items,
                        'pages_read': pages, 'coverage': 'incomplete', 'total_count': None,
                        'next_cursor': cursor, 'failure': page}
            pages += 1
            data = page['data']
            duplicate = any(i['id'] in ids for i in data['items'])
            next_cursor = data['next_cursor']
            if duplicate or (next_cursor and next_cursor in cursors):
                return {'status': 'partial', 'items': items, 'pages_read': pages,
                        'coverage': 'incomplete', 'total_count': None, 'next_cursor': None,
                        'error': {'code': 'CATALOG_CHANGED_OR_CURSOR_LOOP'}}
            items.extend(data['items'])
            ids.update(i['id'] for i in data['items'])
            if not next_cursor:
                return {'status': 'ok', 'items': items, 'pages_read': pages,
                        'coverage': 'end_of_cursor_chain', 'total_count': None, 'next_cursor': None,
                        'snapshot_consistency': 'not_transactional'}
            cursors.add(next_cursor)
            cursor = next_cursor
        return {'status': 'partial', 'items': items, 'pages_read': pages,
                'coverage': 'page_budget_reached', 'total_count': None, 'next_cursor': cursor}

    def check(self, limit=3, agent_run_id=None):
        health, readiness = self.health(), self.readiness()
        report = {'platform': self.platform, 'gateway': health, 'readiness': readiness,
                  'resources': {}, 'scope': 'bounded_api_readback_not_full_platform_acceptance',
                  'control_requests_sent': False, 'inference_requested': False}
        parent = None
        for name, spec in RESOURCES[self.platform].items():
            if not spec.listable:
                report['resources'][name] = {'status': 'unsupported', 'reason': 'DETAIL_ONLY_API'}
                continue
            if spec.parent and parent is None:
                report['resources'][name] = {'status': 'not_checked', 'reason': 'NO_SAMPLED_INDEX_PARENT'}
                continue
            page = self.page(name, limit=limit, parent_id=parent if spec.parent else None)
            row = {'status': page['status'], 'source_path': page.get('source_path'),
                   'checked_at': page.get('checked_at'), 'last_success_at': page.get('last_success_at'),
                   'sampled_count': None, 'has_more': None, 'total_count': None,
                   'error': page.get('error')}
            if page['status'] == 'ok':
                data = page['data']
                row.update(sampled_count=data['page_count'], has_more=data['has_more'])
                if data['items']:
                    first_id = data['items'][0]['id']
                    detail = self.detail(name, first_id)
                    row['sample_detail'] = detail
                    if detail['status'] != 'ok':
                        row['status'] = 'partial'
                    if name == 'indexes':
                        parent = first_id
                else:
                    row['detail_status'] = 'not_checked_empty_page'
            report['resources'][name] = row
        if agent_run_id is not None and self.platform == 'agent':
            report['pinned_agent_run'] = self.detail('agent-runs', agent_run_id)
        bad = health['status'] != 'ok' or readiness['status'] == 'error'
        if readiness['status'] == 'ok' and not readiness['data']['ready']:
            bad = True
        bad = bad or any(v['status'] in ('error', 'partial') for v in report['resources'].values())
        bad = bad or report.get('pinned_agent_run', {}).get('status', 'ok') != 'ok'
        report['result'] = 'attention_required' if bad else 'passed'
        return report


def new_client(name, sources, timeout, times):
    """Shared client construction for the web runtime and the host CLI."""
    value = sources.get(name)
    require(value is not None and value.get('enabled', True), 'NOT_CONFIGURED')
    require('url' in value, 'INVALID_CONFIG')
    return Client(name, value['url'], value.get('token_file'), timeout, times)
