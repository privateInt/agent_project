#!/usr/bin/env python3
"""A08 read-only checks: console assets, catalog metadata and existing execution records.

No POST/PATCH, model call, n8n login, workflow change, or request/session file write.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class Reader:
    def __init__(self, url, token):
        parsed = urllib.parse.urlsplit(url)
        local = parsed.hostname == 'localhost'
        try: local = local or ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError: pass
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or re.search(r'[\s\\\x00-\x20\x7f]', url)
                or (parsed.scheme == 'http' and not local)):
            raise ValueError('Use a loopback HTTP or remote HTTPS origin without credentials or path.')
        if len(token) < 16 or any(c.isspace() for c in token): raise ValueError('Invalid API token file.')
        self.url, self.token = url.rstrip('/'), token
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path, text=False):
        if not path.startswith('/') or path.startswith('//'): raise ValueError('Invalid relative path.')
        request = urllib.request.Request(self.url + path, method='GET',
            headers={'Authorization': 'Bearer ' + self.token} if path.startswith('/api/') else {})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024: raise ValueError('Response limit exceeded.')
                return raw.decode() if text else json.loads(raw)
        except urllib.error.HTTPError as exc: raise ValueError('Read-only request rejected: HTTP ' + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError): raise ValueError('Cannot read the Agent service.') from None

    def items(self, path):
        items, cursor, seen = [], '', set()
        for _ in range(50):
            page = self.get(path + '?limit=100' + ('&cursor=' + urllib.parse.quote(cursor) if cursor else ''))
            items.extend(page['items']); cursor = page.get('next_cursor')
            if not cursor: return items
            if cursor in seen: raise ValueError('Repeated list cursor.')
            seen.add(cursor)
        raise ValueError('Read-only list limit exceeded.')


def identifier(value):
    if str(uuid.UUID(value)) != value: raise ValueError('Canonical UUID required.')
    return value


def check(reader, *, before=False, run_id=None, link_id=None, expected_error=None):
    status = reader.get('/api/status')
    if status.get('stage') != 'A07' or status.get('schema_version') != 5:
        raise ValueError('A07 execution contract/schema 5 required for A08 UI.')
    runs = reader.items('/api/agent-runs')
    if before:
        active = sum(r['state'] in ('accepted', 'running') for r in runs)
        if active: raise ValueError('Active Agent runs exist. Wait for completion before replacing Service/Gateway.')
        return {'result': 'ready_to_update', 'scope': 'read-only Agent queue check', 'active_agent_runs': 0,
                'tool_queue_checked': False, 'inference_executed': False}
    html = reader.get('/console', text=True)
    if 'data-console-version="a08.1"' not in html or '/console/workflows.js' not in html:
        raise ValueError('A08 console HTML is not deployed.')
    for path in ['/console/app.js', '/console/agents.js', '/console/workflows.js', '/console/style.css']:
        if not reader.get(path, text=True).strip(): raise ValueError('Empty console asset.')
    for r in runs:
        if not {'agent_id', 'agent_version', 'caller_context', 'error', 'completed_at'} <= r.keys():
            raise ValueError('A08 Service run catalog metadata is missing.')
    links = reader.items('/api/workflow-links')
    for w in links:
        if not {'agent_id', 'agent_display_name', 'agent_enabled', 'agent_version', 'agent_version_state'} <= w.keys():
            raise ValueError('A08 Service workflow catalog metadata is missing.')
    result = {'result': 'passed', 'stage': 'A08', 'scope': 'read-only assets/catalog/stored records; no answer generation',
              'console_version': 'a08.1', 'agent_run_count': len(runs), 'workflow_link_count': len(links),
              'inference_executed': False, 'page_acceptance': 'deferred', 'answer_quality': 'not_assessed'}
    if link_id:
        link = reader.get('/api/workflow-links/' + identifier(link_id))
        if link.get('workflow_link_id') != link_id: raise ValueError('Workflow link identity changed.')
        result['workflow'] = {k: link[k] for k in ('workflow_link_id', 'workflow_id', 'agent_version_id', 'enabled')}
    if run_id:
        run = reader.get('/api/agent-runs/' + identifier(run_id))
        if run.get('agent_run_id') != run_id: raise ValueError('Agent run identity changed.')
        steps = reader.get('/api/agent-runs/' + run_id + '/steps')['items']
        if [s['step_id'] for s in steps] != ['validate', 'search', 'context', 'generate', 'finalize']:
            raise ValueError('Stored Agent steps changed.')
        if expected_error and (run.get('state') != 'failed' or (run.get('error') or {}).get('code') != expected_error):
            raise ValueError('Stored failure no longer matches the expected code.')
        if link_id and (run['agent_version_id'] != link['agent_version_id'] or
                        run.get('caller_context', {}).get('system') != 'n8n' or
                        run.get('caller_context', {}).get('workflow_id') != link['workflow_id']):
            raise ValueError('Run does not match the linked n8n workflow and Agent version.')
        result['stored_run'] = {k: run.get(k) for k in ('agent_run_id', 'agent_version_id', 'request_id', 'tool_call_id', 'state')}
        result['stored_run']['error'] = {k: v for k, v in (run.get('error') or {}).items() if k in ('code', 'stage')}
        result['stored_run']['steps'] = [{'step': s['step_id'], 'state': s['state']} for s in steps]
    elif expected_error: raise ValueError('--expect-error requires --run-id.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:16100')
    parser.add_argument('--token-file', type=Path, default=ROOT / '.secrets/api-token')
    parser.add_argument('--before-update', action='store_true')
    parser.add_argument('--run-id'); parser.add_argument('--workflow-link-id'); parser.add_argument('--expect-error')
    args = parser.parse_args()
    result = check(Reader(args.url, args.token_file.expanduser().read_text().strip()), before=args.before_update,
                   run_id=args.run_id, link_id=args.workflow_link_id, expected_error=args.expect_error)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise SystemExit('FAIL: ' + (str(exc) if isinstance(exc, ValueError) else 'Check connection settings and preserved records.'))
