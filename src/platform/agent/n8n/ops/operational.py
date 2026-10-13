#!/usr/bin/env python3
"""Register and call a persistent n8n webhook workflow; Agent owns execution records."""
import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.parse
import uuid

from live_check import API, Journal, ROOT, origin, private_read, question_value, save
from operational_workflow import build, matches

sys.path.insert(0, str(ROOT.parent / 'src/infra/n8n/ops'))
from api_access import Client, DEFAULT_KEY, HTTPFailure, read_private, verify_key

DEFAULT_SESSION = ROOT.parent.parent / 'agent_n8n_workflow'


def webhook_headers(state, body):
    """Short-lived, body-bound JWT; the long-lived signing key never enters execution input."""
    def encoded(value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b'=')
    now = int(time.time())
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    payload = {'iat': now, 'exp': now + 120, 'aud': state['webhook_path'], 'sub': state['agent_version_id'],
               'request_sha256': hashlib.sha256(canonical).hexdigest()}
    message = encoded({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + encoded(payload)
    signature = base64.urlsafe_b64encode(hmac.new(state['hook_secret'].encode(), message, hashlib.sha256).digest()).rstrip(b'=')
    return {'Authorization': 'Bearer ' + (message + b'.' + signature).decode()}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('잘못된 리소스 ID입니다.')
    return value


def create_resource(client, directory, state, slot, collection, body):
    """Reconcile a lost create by an unguessable journalled name, never blind POST retry."""
    resources = state.setdefault('resources', {})
    intent = resources.get(slot)
    if intent and intent.get('id'):
        return client.request('GET', collection + '/' + identifier(intent['id']))
    if intent:
        found = [v for v in client.items(collection) if v.get('name') == body['name']]
        if len(found) == 1:
            intent['id'] = identifier(found[0]['id']); save(directory / 'settings.json', state)
            return client.request('GET', collection + '/' + intent['id'])
        if len(found) > 1: raise ValueError('같은 등록 이름이 중복됩니다. 자동 선택·삭제하지 않습니다.')
        raise ValueError('이전 n8n 등록 응답이 불확실하고 아직 조회되지 않습니다. 같은 prepare로 다시 조회하세요. 상태 폴더를 지우거나 새 등록을 시작하지 마세요.')
    resources[slot] = {'name': body['name'], 'status': 'creating'}
    save(directory / 'settings.json', state)
    try: result = client.request('POST', collection, body)
    except HTTPFailure as exc:
        # Definite input/auth rejections are safe to correct and retry. 5xx remains uncertain.
        if exc.status in (400, 401, 403, 404, 413, 415, 422):
            del resources[slot]; save(directory / 'settings.json', state)
        raise
    resources[slot]['id'] = identifier(result['id'])
    save(directory / 'settings.json', state)
    return client.request('GET', collection + '/' + resources[slot]['id'])


def desired(state):
    return build(state['agent_version_id'], state['container_agent_url'],
                 state['resources']['agent_credential']['id'], state['resources']['hook_credential']['id'],
                 state['webhook_path'], state['workflow_name'])


def checked_workflow(client, state, *, published=False):
    workflow = client.request('GET', '/api/v1/workflows/' + identifier(state['resources']['workflow']['id']))
    if not matches(workflow, desired(state)):
        raise ValueError('n8n 워크플로 내용이 등록 당시와 다릅니다. 덮어쓰거나 자동 실행하지 않습니다.')
    if published and (not workflow.get('active') or workflow.get('activeVersionId') != state.get('published_version_id')):
        raise ValueError('등록한 n8n 버전이 게시된 상태가 아닙니다. prepare로 확인하세요.')
    if published and workflow.get('activeVersion') and not matches(workflow['activeVersion'], desired(state), history=True):
        raise ValueError('n8n 게시 버전의 내용이 달라졌습니다.')
    return workflow


def prepare(args, directory, client, api, token):
    path = directory / 'settings.json'
    if path.exists():
        state = private_read(path)
        if state.get('recovered_from'):
            raise ValueError('복구한 세션은 준비가 끝났습니다. status 또는 run/lookup을 사용하세요. 재등록하지 않습니다.')
        for option, field in [('agent_version_id', 'agent_version_id'), ('instance_url', 'instance_url'), ('name', 'display_name')]:
            value = getattr(args, option, None)
            if value and value != state[field]: raise ValueError('저장한 등록 설정과 다릅니다: ' + option)
        if args.from_live_session:
            old = private_read(args.from_live_session.expanduser() / 'settings.json')
            if old['agent_version']['agent_version_id'] != state['agent_version_id']:
                raise ValueError('이전 시험 세션의 Agent 버전이 다릅니다.')
    else:
        version_id = args.agent_version_id
        if args.from_live_session:
            old = private_read(args.from_live_session.expanduser() / 'settings.json')
            selected = old['agent_version']['agent_version_id']
            if version_id and version_id != selected: raise ValueError('시험 세션과 지정한 Agent 버전이 다릅니다.')
            version_id = selected
        if not version_id: raise ValueError('--from-live-session 또는 --agent-version-id를 지정하세요.')
        if str(uuid.UUID(version_id)) != version_id: raise ValueError('잘못된 Agent 버전 ID입니다.')
        av = api.get('/api/agent-versions/' + version_id)
        if av['state'] == 'draft' and not args.publish_agent:
            raise ValueError('성공한 초안을 발행하려면 prepare에 --publish-agent를 지정하세요.')
        if av['state'] not in ('draft', 'published'): raise ValueError('발행 가능한 Agent 버전이 아닙니다.')
        uid = str(uuid.uuid4())
        state = {'schema': 1, 'session_id': uid, 'agent_version_id': version_id,
                 'definition_sha256': av['definition_sha256'], 'agent_url': origin(args.agent_url),
                 'container_agent_url': origin(args.container_agent_url), 'n8n_url': client.url,
                 'instance_url': origin(args.instance_url or client.url),
                 'display_name': args.name or '문서 질의응답 Flow',
                 'workflow_name': (args.name or '문서 질의응답 Flow') + ' · ' + uid,
                 'webhook_path': 'agent-question-' + uid, 'hook_secret': secrets.token_urlsafe(32),
                 'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'resources': {}}
        save(path, state)
    if state['n8n_url'] != client.url or state['agent_url'] != api.url or state['container_agent_url'] != origin(args.container_agent_url):
        raise ValueError('저장한 서버 주소가 다릅니다. 같은 연결 설정을 사용하세요.')
    if state['token_sha256'] != hashlib.sha256(token.encode()).hexdigest():
        raise ValueError('Agent 토큰이 변경됐습니다. 기존 자격을 자동 덮어쓰지 않습니다. 새 연결 세션을 준비하세요.')
    av = api.get('/api/agent-versions/' + state['agent_version_id'])
    if av['definition_sha256'] != state['definition_sha256']: raise ValueError('고정 Agent 정의가 달라졌습니다.')
    journal = Journal(directory)
    if av['state'] == 'draft':
        if not args.publish_agent: raise ValueError('초안 발행에는 --publish-agent가 필요합니다.')
        journal.post(api, 'publish_agent', '/api/agent-versions/' + av['agent_version_id'] + '/publish',
                     {'expected_revision': av['revision']}, 200)
        av = api.get('/api/agent-versions/' + av['agent_version_id'])
    if av['state'] != 'published': raise ValueError('Agent 버전이 발행 상태가 아닙니다.')
    for slot, label, kind, data in [
        ('agent_credential', 'Agent API', 'httpHeaderAuth', {'name': 'Authorization', 'value': 'Bearer ' + token}),
        ('hook_credential', 'Agent workflow entry', 'jwtAuth', {'keyType': 'passphrase', 'algorithm': 'HS256', 'secret': state['hook_secret']})]:
        name = label + ' · ' + state['session_id']
        credential = create_resource(client, directory, state, slot, '/api/v1/credentials',
                                     {'name': name, 'type': kind, 'data': data})
        if credential.get('name') != name or credential.get('type') != kind:
            raise ValueError('등록 자격 참조가 달라졌습니다.')
    create_resource(client, directory, state, 'workflow', '/api/v1/workflows', desired(state))
    wf = checked_workflow(client, state)
    version = wf['versionId']
    if wf.get('active'):
        if wf.get('activeVersionId') != version: raise ValueError('다른 n8n 버전이 게시돼 있습니다.')
    else:
        client.request('POST', '/api/v1/workflows/' + identifier(wf['id']) + '/publish', {'versionId': version})
    state['published_version_id'] = version
    save(path, state)
    checked_workflow(client, state, published=True)
    link = journal.post(api, 'workflow_link', '/api/workflow-links',
                        {'display_name': state['display_name'], 'instance_url': state['instance_url'],
                         'workflow_id': wf['id'], 'agent_version_id': av['agent_version_id']}, 201)
    state['workflow_link_id'] = link['workflow_link_id']; state['prepared'] = True
    save(path, state)
    return {'result': 'prepared', 'scope': 'operational n8n', 'agent_version_id': av['agent_version_id'],
            'workflow_id': wf['id'], 'workflow_link_id': link['workflow_link_id'],
            'edit_url': link['edit_url'], 'inference_executed': False, 'webhook_authenticated': True}


def request_state(path, state, question=None):
    if path.exists():
        request = private_read(path)
        if request['session_id'] != state['session_id']: raise ValueError('다른 워크플로의 요청 파일입니다.')
        if question is not None and request['body']['question'] != question:
            raise ValueError('질문이 달라졌습니다. 새 질문에는 새 --request-file을 지정하세요.')
        return request
    if question is None: raise ValueError('새 요청에는 --question을 입력하세요.')
    question = question_value(question, True)
    request = {'schema': 1, 'session_id': state['session_id'],
               'body': {'request_id': str(uuid.uuid4()), 'question': question}}
    save(path, request)  # Complete request durable before webhook POST.
    return request


def node_output(execution, name):
    entries = execution.get('data', {}).get('resultData', {}).get('runData', {}).get(name, [])
    for entry in reversed(entries):
        try: return entry['data']['main'][0][0]['json']
        except (KeyError, IndexError, TypeError): pass
    return None


def find_executions(client, state, request):
    workflow_id = state['resources']['workflow']['id']
    found = []
    for item in client.items('/api/v1/executions?workflowId=' + identifier(workflow_id)):
        detail = client.request('GET', '/api/v1/executions/' + identifier(item['id']) + '?includeData=true')
        prepared = node_output(detail, 'Prepare request')
        if prepared and prepared.get('request', {}).get('invocation_key') == request['body']['request_id']:
            found.append(detail)
    return found


def lookup(client, api, state, request, path):
    executions = find_executions(client, state, request)
    receipt = request.get('receipt')
    run_ids = {x['agent_run_id'] for e in executions if (x := node_output(e, 'Run pointer'))}
    if receipt and receipt.get('agent_run_id'): run_ids.add(receipt['agent_run_id'])
    if len(run_ids) > 1: raise ValueError('같은 요청이 서로 다른 Agent 실행에 연결됐습니다.')
    base = {'scope': 'operational n8n', 'workflow_id': state['resources']['workflow']['id'],
            'n8n_execution_ids': [e['id'] for e in executions], 'logical_request_id': request['body']['request_id']}
    if not run_ids:
        failed = any(e.get('status') in ('error', 'crashed', 'canceled') for e in executions)
        return {**base, 'result': 'failed' if failed else 'pending', 'agent_run_id': None,
                'code': 'N8N_BEFORE_ACCEPTANCE_FAILED' if failed else 'ACCEPTANCE_NOT_FOUND', 'inference_executed': 'unknown'}
    run_id = next(iter(run_ids))
    run = api.get('/api/agent-runs/' + identifier(run_id))
    expected_caller = {'system': 'n8n', 'workflow_id': base['workflow_id'],
                       'execution_id': 'request:' + request['body']['request_id'], 'node_id': 'agent-call'}
    if (run['agent_version_id'] != state['agent_version_id'] or run['mode'] != 'run' or
        run['definition_sha256'] != state['definition_sha256'] or run['invocation_key'] != request['body']['request_id'] or
        run['input'] != {'question': request['body']['question']} or run['caller_context'] != expected_caller):
        raise ValueError('Agent 실행과 원래 질문·버전·호출자 정보가 일치하지 않습니다.')
    request.setdefault('receipt', {})['agent_run_id'] = run_id; save(path, request)
    steps = api.get('/api/agent-runs/' + identifier(run_id) + '/steps')['items']
    result = {**base, 'result': 'pending', 'agent_run_id': run_id, 'request_id': run['request_id'],
              'tool_call_id': run.get('tool_call_id'), 'state': run['state'],
              'steps': [{'step': s['step_id'], 'state': s['state']} for s in steps]}
    if run['state'] in ('failed', 'interrupted'):
        result.update(result='failed', error={k: v for k, v in (run.get('error') or {}).items() if k in ('code', 'stage')})
    elif run['state'] == 'succeeded':
        answer = run['result']
        completed = [e for e in executions if e.get('status') == 'success' and node_output(e, 'Answer and sources')]
        expected_execution = request.get('receipt', {}).get('n8n_execution_id')
        delivery_verified = bool(completed) and (not expected_execution or any(e['id'] == expected_execution for e in completed))
        # Success requires the n8n delivery and Agent record to agree, not just HTTP 200.
        for execution in completed:
            output = node_output(execution, 'Answer and sources')
            for key in ('answer', 'citations', 'outcome', 'warnings', 'answer_truncated'):
                if output.get(key) != answer.get(key): raise ValueError('n8n과 Agent의 답변·출처가 다릅니다.')
            for key in ('agent_run_id', 'agent_version_id', 'request_id', 'tool_call_id'):
                if output.get(key) != run.get(key): raise ValueError('n8n 실행 식별자가 다릅니다.')
        result.update(result='passed' if delivery_verified else 'incomplete', outcome=answer['outcome'],
                      citation_count=len(answer['citations']), answer_truncated=answer.get('answer_truncated'),
                      warnings=answer.get('warnings', []), semantic_quality='not_assessed',
                      delivery_verified=delivery_verified, replay_verified=len(completed) >= 2)
        if not delivery_verified: result['code'] = 'N8N_DELIVERY_NOT_VERIFIED'
        save(path.with_name(path.stem + '.result.json'), {'summary': result, 'run': run, 'steps': steps})
        if delivery_verified: print('\n답변:\n' + answer['answer'])
    save(path.with_name(path.stem + '.lookup.json'), result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['prepare', 'run', 'lookup', 'status', 'disable'])
    p.add_argument('--session-dir', type=Path, default=DEFAULT_SESSION)
    p.add_argument('--key-file', type=Path, default=DEFAULT_KEY)
    p.add_argument('--agent-url', default='http://127.0.0.1:16100')
    p.add_argument('--container-agent-url', default='http://agent-gateway:8000')
    p.add_argument('--token-file', type=Path, default=ROOT / '.secrets/api-token')
    p.add_argument('--from-live-session', type=Path); p.add_argument('--agent-version-id')
    p.add_argument('--publish-agent', action='store_true'); p.add_argument('--instance-url'); p.add_argument('--name')
    p.add_argument('--question'); p.add_argument('--request-file', type=Path)
    a = p.parse_args()
    if a.action != 'run' and a.question is not None:
        raise ValueError('--question은 run에서만 사용하세요.')
    if a.action not in ('run', 'lookup') and a.request_file:
        raise ValueError('--request-file은 run/lookup에서만 사용하세요.')
    directory = a.session_dir.expanduser().absolute()
    if directory.is_symlink() or ROOT.parent == directory.resolve() or ROOT.parent in directory.resolve().parents:
        raise ValueError('운영 세션은 프로젝트 밖의 일반 폴더에 보관하세요.')
    if not directory.exists():
        if a.action != 'prepare': raise ValueError('운영 세션 폴더가 없습니다. 기존 Flow가 있으면 recover.py로 복구하세요. 새 Flow만 prepare로 등록하세요.')
        directory.mkdir(mode=0o700)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError('세션 폴더는 현재 사용자 소유·0700 권한이어야 합니다.')
    fd = os.open(directory / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('같은 운영 세션을 다른 프로세스가 사용 중입니다.') from None
        key = read_private(a.key_file.expanduser())
        client = verify_key(key)
        token = a.token_file.expanduser().read_text().strip()
        if len(token) < 16 or '\r' in token or '\n' in token: raise ValueError('Agent 토큰 파일을 확인하세요.')
        if a.action == 'prepare':
            result = prepare(a, directory, client, API(a.agent_url, token), token)
        else:
            if a.from_live_session or a.agent_version_id or a.publish_agent or a.instance_url or a.name:
                raise ValueError('등록 옵션은 prepare에서만 사용하세요.')
            state = private_read(directory / 'settings.json')
            if not state.get('prepared') or state['n8n_url'] != client.url: raise ValueError('같은 연결로 prepare를 완료하세요.')
            api = API(state['agent_url'], token)
            if a.action == 'status':
                wf = checked_workflow(client, state)
                link = api.get('/api/workflow-links/' + identifier(state['workflow_link_id']))
                result = {'result': 'read', 'workflow_id': wf['id'], 'active': wf['active'],
                          'published_version_id': wf.get('activeVersionId'), 'link_enabled': link.get('enabled'),
                          'agent_version_id': state['agent_version_id'], 'workflow_link_id': link['workflow_link_id'],
                          'edit_url': link['edit_url'], 'inference_executed': False}
            elif a.action == 'disable':
                wf = checked_workflow(client, state)
                if wf.get('active'): client.request('POST', '/api/v1/workflows/' + identifier(wf['id']) + '/unpublish')
                if checked_workflow(client, state).get('active'): raise ValueError('워크플로 게시 중지를 확인하지 못했습니다.')
                result = {'result': 'disabled', 'workflow_id': wf['id'], 'records_preserved': True}
            else:
                path = a.request_file.expanduser().absolute() if a.request_file else directory / 'request.json'
                if path.is_symlink() or ROOT.parent == path.resolve() or ROOT.parent in path.resolve().parents:
                    raise ValueError('요청 파일은 프로젝트 밖의 일반 파일이어야 합니다.')
                if a.action == 'lookup' and not path.exists(): raise ValueError('조회할 요청 파일이 없습니다.')
                request = request_state(path, state, a.question if a.action == 'run' else None)
                if a.action == 'run':
                    checked_workflow(client, state, published=True)
                    print('운영 n8n에서 실제 질문을 실행합니다. 같은 요청 파일은 같은 Agent 실행을 재사용합니다.', flush=True)
                    try:
                        receipt = client.request('POST', '/webhook/' + state['webhook_path'], request['body'],
                                                 headers=webhook_headers(state, request['body']), timeout=760)
                        if receipt.get('agent_version_id') != state['agent_version_id']: raise ValueError('잘못된 webhook 응답입니다.')
                        request['receipt'] = {'agent_run_id': identifier(receipt['agent_run_id']),
                                              'n8n_execution_id': identifier(receipt['n8n_execution']['execution_id'])}
                        save(path, request)
                    except ValueError:
                        print('webhook 응답을 확인하지 못해 저장된 실행을 조회합니다. 요청 파일을 보존하세요.', file=sys.stderr)
                result = lookup(client, api, state, request, path)
                if a.action == 'run':
                    # Webhook replies precede the final execution DB commit by a short interval.
                    for _ in range(15):
                        if result['result'] not in ('pending', 'incomplete'): break
                        time.sleep(1)
                        result = lookup(client, api, state, request, path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['result'] in ('prepared', 'passed', 'read', 'disabled') else 2


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, EOFError) as exc:
        raise SystemExit('FAIL: ' + (str(exc) if isinstance(exc, ValueError) else '설정·상태 파일을 확인하세요. 기존 세션을 보존하세요.'))
