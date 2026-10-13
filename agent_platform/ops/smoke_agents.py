#!/usr/bin/env python3
"""Run one published Agent, or look up an existing run without inference.

Keep the same state file and input after an uncertain response. The invocation
key is persisted before POST, so retrying resumes the same accepted record.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from smoke_tools import API, ROOT


def wait(api, run_id):
    until = time.monotonic() + 610
    while time.monotonic() < until:
        run = api.get('/api/agent-runs/' + run_id)
        if run['state'] not in ('accepted', 'running'):
            if run['state'] != 'succeeded':
                err = run.get('error', {})
                raise ValueError(f"{run_id}: {err.get('stage')}: {err.get('code')}; same key only replays this outcome")
            return run
        time.sleep(1)
    raise ValueError('Still processing. Keep the state file and run ID: ' + run_id)


def validate(api, run):
    result = run.get('result', {})
    steps = api.get('/api/agent-runs/' + run['agent_run_id'] + '/steps')['items']
    if (run.get('state') != 'succeeded' or result.get('outcome') not in ('answered', 'no_evidence')
            or not isinstance(result.get('answer'), str) or not result['answer'].strip()
            or not isinstance(result.get('citations'), list) or not result.get('tool_call_id')
            or result['tool_call_id'] != run.get('tool_call_id')
            or [s['step_id'] for s in steps] != ['validate', 'search', 'context', 'generate', 'finalize']
            or any(s['state'] not in ('succeeded', 'skipped') for s in steps)):
        raise ValueError('Unexpected Agent result or step records')
    if result['outcome'] == 'answered' and not result['citations']:
        raise ValueError('Answered result has no citations')
    child = api.get('/api/tool-calls/' + result['tool_call_id'])
    if child.get('parent_agent_run_id') != run['agent_run_id'] or child.get('parent_step_id') != 'search' or child.get('state') != 'succeeded' or child.get('request_id') != run['request_id']:
        raise ValueError('Search ToolCall correlation changed')
    return {'result':'passed', 'stage':'A06', 'agent_run_id':run['agent_run_id'],
            'agent_version_id':run['agent_version_id'], 'request_id':run['request_id'],
            'tool_call_id':run['tool_call_id'], 'outcome':result['outcome'],
            'citation_count':len(result['citations']), 'answer_truncated':result['answer_truncated']}


def published(api, name):
    found, cursor = [], ''
    for _ in range(50):
        page = api.get('/api/agents?limit=100' + ('&cursor='+cursor if cursor else ''))
        found.extend(a for a in page['items'] if a['display_name'] == name and a['enabled'])
        cursor = page.get('next_cursor', '')
        if not cursor: break
    else: raise ValueError('Agent list limit exceeded')
    if len(found) != 1: raise ValueError('Expected one enabled Agent with that name')
    agent = api.get('/api/agents/' + found[0]['agent_id'])
    versions = [v for v in agent['versions'] if v['state'] == 'published']
    if not versions: raise ValueError('First complete a successful test and press 사용 시작')
    return max(versions, key=lambda v:v['version'])['agent_version_id']


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://127.0.0.1:16100')
    p.add_argument('--token-file', type=Path, default=ROOT/'.secrets/api-token')
    p.add_argument('--agent-name', default='문서 답변 도우미')
    p.add_argument('--question')
    p.add_argument('--state-file', type=Path)
    p.add_argument('--run-id', help='Lookup only; no new inference or registration')
    p.add_argument('--request-id', help='Original request ID to verify in lookup mode')
    args = p.parse_args()
    api = API(args.url, args.token_file.expanduser().read_text().strip())
    status = api.get('/api/status')
    if not status.get('ready') or (status.get('stage'), status.get('schema_version')) not in (('A06', 4), ('A07', 5)):
        raise ValueError('A06/A07 service/schema not ready')
    if args.run_id:
        if str(uuid.UUID(args.run_id)) != args.run_id: raise ValueError('Invalid run ID')
        run = wait(api, args.run_id)
        if args.request_id and run['request_id'] != args.request_id: raise ValueError('Original request ID changed')
        print(json.dumps(dict(validate(api, run), mode='lookup'), ensure_ascii=False, indent=2)); return
    if not args.question or not args.question.strip() or len(args.question.encode()) > 8192 or '\0' in args.question or not args.state_file:
        raise ValueError('Provide --question (up to 8 KiB) and --state-file')
    path = args.state_file.expanduser().absolute()
    if path.is_symlink() or not path.parent.is_dir(): raise ValueError('Use an existing directory and a regular state file')
    fingerprint = hashlib.sha256(json.dumps([args.url, args.agent_name, args.question], ensure_ascii=False).encode()).hexdigest()
    if path.exists():
        state = json.loads(path.read_text())
        if state.get('fingerprint') != fingerprint: raise ValueError('State belongs to different inputs; preserve it and choose a new file')
    else:
        state = {'fingerprint':fingerprint, 'agent_version_id':published(api, args.agent_name), 'invocation_key':str(uuid.uuid4())}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(state, out); out.flush(); os.fsync(out.fileno())
    body = {'agent_version_id':state['agent_version_id'], 'input':{'question':args.question}, 'mode':'run'}
    accepted = api.post('/api/agent-runs', body, expected=202, key=state['invocation_key'])
    print('Accepted AgentRun:', accepted['agent_run_id'], flush=True)
    run = wait(api, accepted['agent_run_id'])
    replay, headers = api.request('POST', '/api/agent-runs', body, expected=202, key=state['invocation_key'])
    if replay['agent_run_id'] != run['agent_run_id'] or headers.get('Idempotency-Replayed') != 'true': raise ValueError('REST replay changed')
    if api.get('/api/agent-runs/' + run['agent_run_id']) != run: raise ValueError('Persisted result changed')
    print(json.dumps(dict(validate(api, run), mode='rest_replay'), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyError, TypeError) as exc: raise SystemExit('FAIL: '+str(exc))
