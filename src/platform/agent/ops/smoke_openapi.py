#!/usr/bin/env python3
"""A04 deployed gateway/import checks; no target API execution."""
import argparse
import json
from pathlib import Path
import urllib.request
import uuid
from smoke_tools import API, ROOT


def check(api):
    status = api.get('/api/status')
    if not status.get('ready') or (status.get('stage'), status.get('schema_version')) not in (('A04', 3), ('A05', 3), ('A06', 4), ('A07', 5)):
        raise ValueError('A04 service/schema not ready')
    for path, mime in [('/console', 'text/html'), ('/console/app.js', 'text/javascript'), ('/console/style.css', 'text/css')]:
        with api.opener.open(api.base + path, timeout=10) as response:
            if response.status != 200 or mime not in response.headers.get('Content-Type', ''):
                raise ValueError('console asset missing')
            if "frame-ancestors 'none'" not in response.headers.get('Content-Security-Policy', ''):
                raise ValueError('console policy missing')
    with api.opener.open(api.base + '/console/example.json', timeout=10) as response:
        spec = json.load(response)
    spec['paths']['/unsupported'] = {'get': {'responses': {'202': {'description': 'later'}}}}
    key = str(uuid.uuid4())
    imported = api.post('/api/openapi-imports', spec, key=key)
    replay, headers = api.request('POST', '/api/openapi-imports', spec, expected=201, key=key)
    if imported != replay or headers.get('Idempotency-Replayed') != 'true':
        raise ValueError('import replay mismatch')
    detail = api.get('/api/openapi-imports/' + imported['import_id'])
    if detail != imported:
        raise ValueError('stored import changed')
    operations = detail['analysis']['operations']
    if sum(o['supported'] for o in operations) != 1 or sum(not o['supported'] and bool(o['problems']) for o in operations) != 1:
        raise ValueError('operation support reporting mismatch')
    if 'source' in detail:
        raise ValueError('raw import exposed')
    print(json.dumps({'result':'passed', 'stage':'A04', 'mode':'gateway_and_import', 'import_id':imported['import_id'],
                      'checks':['console_assets','import','replay','persisted_analysis','unsupported_visible'], 'target_http_calls':0}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:16100')
    parser.add_argument('--token-file', type=Path, default=ROOT / '.secrets/api-token')
    args = parser.parse_args()
    check(API(args.url, args.token_file.read_text().strip()))

if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError) as exc:
        raise SystemExit('FAIL: ' + str(exc))
