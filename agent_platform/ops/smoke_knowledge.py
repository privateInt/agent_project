#!/usr/bin/env python3
"""Run one published natural-language Tool and verify REST/MCP share its call.

A private state file keeps the selected version and invocation key BEFORE POST.
Re-running the same command resumes/replays the same call, without new inference.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from smoke_tools import API, ROOT, PROTOCOL


def wait(api, call_id):
    until = time.monotonic() + 250
    while time.monotonic() < until:
        call = api.get('/api/tool-calls/' + call_id)
        if call['state'] not in ('accepted', 'running'):
            if call['state'] != 'succeeded':
                error = call.get('error', {})
                raise ValueError(f"{call_id}: {error.get('stage')}: {error.get('code')}; same key will only replay this outcome")
            return call
        time.sleep(1)
    raise ValueError('Still processing; keep the state file and rerun: ' + call_id)


def validate(call):
    result = call.get('result', {})
    meta, data = result.get('metadata', {}), result.get('data', {})
    if (meta.get('adapter') != 'knowledge_search_v1' or result.get('completion') != 'completed'
            or meta.get('embedding_calls') != 1 or meta.get('search_calls') != 1
            or data.get('index_version_id') != meta.get('index_version_id')
            or data.get('embedding_space_id') != meta.get('embedding_profile', {}).get('embedding_space_id')
            or not isinstance(data.get('results'), list) or data.get('returned_count') != len(data['results'])):
        raise ValueError('Unexpected natural-language search result')
    return {'result': 'passed', 'stage': 'A05', 'tool_call_id': call['tool_call_id'],
            'tool_version_id': call['tool_version_id'], 'request_id': call['request_id'],
            'index_version_id': data['index_version_id'], 'returned_count': data['returned_count']}


def published(api, name):
    found, cursor = [], ''
    for _ in range(50):
        page = api.get('/api/tools?limit=100' + ('&cursor='+cursor if cursor else ''))
        found.extend(t for t in page['items'] if t['display_name'] == name and t['enabled'])
        cursor = page.get('next_cursor', '')
        if not cursor:
            break
    else:
        raise ValueError('Tool list limit exceeded')
    if len(found) != 1:
        raise ValueError('Expected one enabled Tool with that name')
    tool = api.get('/api/tools/' + found[0]['tool_id'])
    versions = [v for v in tool['versions'] if v['kind'] == 'knowledge_search_v1' and v['state'] == 'published']
    if not versions:
        raise ValueError('First finish a successful test and press 사용 시작 in the console')
    version = max(versions, key=lambda v: v['version'])
    return version['tool_version_id'], 't_'+tool['tool_id'].replace('-', '')+'_v'+str(version['version'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://127.0.0.1:16100')
    p.add_argument('--token-file', type=Path, default=ROOT/'.secrets/api-token')
    p.add_argument('--tool-name', default='test1 검색')
    p.add_argument('--question')
    p.add_argument('--state-file', type=Path, help='Private checkpoint outside source; reuse for retries')
    p.add_argument('--call-id', help='Lookup only after restart; no execution or registration')
    p.add_argument('--request-id', help='Original request ID to compare in lookup mode')
    args = p.parse_args()
    api = API(args.url, args.token_file.expanduser().read_text().strip())
    status = api.get('/api/status')
    if not status.get('ready') or (status.get('stage'), status.get('schema_version')) not in (('A05', 3), ('A06', 4), ('A07', 5)):
        raise ValueError('A05 service/schema not ready')
    if args.call_id:
        if str(uuid.UUID(args.call_id)) != args.call_id:
            raise ValueError('Invalid call ID')
        call = wait(api, args.call_id)
        if args.request_id and call['request_id'] != args.request_id:
            raise ValueError('Original request ID changed')
        print(json.dumps(dict(validate(call), mode='lookup'), ensure_ascii=False, indent=2))
        return
    if not args.question or not args.question.strip() or len(args.question.encode()) > 8192 or '\0' in args.question or not args.state_file:
        raise ValueError('Provide --question (up to 8 KiB) and --state-file')
    path = args.state_file.expanduser().absolute()
    if path.is_symlink() or not path.parent.is_dir():
        raise ValueError('State file must have an existing parent and not be a symlink')
    fingerprint = hashlib.sha256(json.dumps([args.url, args.tool_name, args.question], ensure_ascii=False).encode()).hexdigest()
    if path.exists():
        state = json.loads(path.read_text())
        if state.get('fingerprint') != fingerprint:
            raise ValueError('This state file belongs to different inputs; preserve it and choose a new file')
    else:
        version, name = published(api, args.tool_name)
        state = {'fingerprint': fingerprint, 'tool_version_id': version, 'mcp_name': name, 'invocation_key': str(uuid.uuid4())}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(state, out); out.flush(); os.fsync(out.fileno())
    body = {'tool_version_id': state['tool_version_id'], 'arguments': {'query': args.question}, 'mode': 'run'}
    key = state['invocation_key']
    accepted = api.post('/api/tool-calls', body, expected=202, key=key)
    print('Accepted ToolCall:', accepted['tool_call_id'], flush=True)
    call = wait(api, accepted['tool_call_id'])
    repeated, headers = api.request('POST', '/api/tool-calls', body, expected=202, key=key)
    if repeated['tool_call_id'] != call['tool_call_id'] or headers.get('Idempotency-Replayed') != 'true':
        raise ValueError('REST replay changed')
    api.mcp('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': 'a05-smoke', 'version': '1'}})
    result = api.mcp('tools/call', {'name': state['mcp_name'], 'arguments': {'invocation_key': key, 'arguments': body['arguments']}})
    if result.get('isError') or result.get('structuredContent') != call:
        raise ValueError('REST/MCP did not return the same persisted result')
    print(json.dumps(dict(validate(call), mode='rest_and_mcp', invocation_key=key), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise SystemExit('FAIL: '+str(exc))
