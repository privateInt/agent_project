#!/usr/bin/env python3
"""Prepare/run/resume a real n8n -> Agent check without opening a browser."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / 'n8n/live'
DEFAULT_SESSION = ROOT.parent.parent / 'agent_n8n_live_check'


def origin(value):
    u = urllib.parse.urlsplit(value)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('', '/') or re.search(r'[\s\x00-\x20\x7f]', value):
        raise ValueError('서버 주소는 인증정보·경로 없는 http(s) 주소와 포트로 입력하세요.')
    return value.rstrip('/')


def private_read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('일반 파일이 아닌 상태 경로: ' + path.name)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('상태 파일은 현재 사용자 소유·0600 권한이어야 합니다: ' + path.name)
    return json.loads(path.read_text())


def save(path, value):
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError('안전하지 않은 상태 파일: ' + path.name)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class API:
    def __init__(self, url, token):
        self.url, self.token = origin(url), token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, route, body=None, expected=200, key=None):
        rid = str(uuid.uuid4())
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json', 'X-Request-ID': rid}
        if key: headers['Idempotency-Key'] = key
        request = urllib.request.Request(self.url + route, method=method, headers=headers,
            data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None)
        try:
            try: response = self.opener.open(request, timeout=45)
            except urllib.error.HTTPError as exc: response = exc
            with response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024: raise ValueError('Agent 응답이 제한을 초과했습니다.')
                if response.status != expected:
                    code = ''
                    try:
                        value = json.loads(raw)
                        candidate = value.get('code', (value.get('error') or {}).get('code', ''))
                        if isinstance(candidate, str) and re.fullmatch(r'[A-Z0-9_]{1,80}', candidate): code = ' · ' + candidate
                    except (ValueError, AttributeError, TypeError): pass
                    raise ValueError(f'Agent {method} {route}: HTTP {response.status}{code}; 같은 세션으로 복구하세요.')
                if response.headers.get('X-Request-ID') != rid: raise ValueError('Agent 요청 식별자가 일치하지 않습니다.')
                return json.loads(raw)
        except (urllib.error.URLError, TimeoutError):
            raise ValueError('Agent 연결 실패. 주소·기동 상태를 확인하고 같은 세션으로 다시 실행하세요.') from None

    def get(self, route): return self.request('GET', route)
    def inspect(self, route, body): return self.request('POST', route, body, key=str(uuid.uuid4()))


def all_items(api, route):
    result, cursor, seen = [], '', set()
    for _ in range(50):
        query = {'limit': 100}
        if cursor: query['cursor'] = cursor
        page = api.get(route + '?' + urllib.parse.urlencode(query))
        result.extend(page['items']); cursor = page.get('next_cursor', '')
        if not cursor: return result
        if cursor in seen: raise ValueError('목록의 다음 페이지가 반복됩니다.')
        seen.add(cursor)
    raise ValueError('목록이 너무 큽니다. 버전 ID를 직접 지정하세요.')


class Journal:
    def __init__(self, directory):
        self.path = directory / 'preparation.json'
        self.data = private_read(self.path) if self.path.exists() else {'schema': 1, 'operations': {}}

    def post(self, api, name, route, body, expected):
        op = self.data['operations'].get(name)
        if op is None:
            op = {'route': route, 'body': body, 'key': str(uuid.uuid4())}
            self.data['operations'][name] = op
            save(self.path, self.data)  # durable before every mutating request
        elif op['route'] != route or op['body'] != body:
            raise ValueError('저장한 준비 요청이 달라졌습니다. 기존 세션을 보존하고 별도 세션을 사용하세요.')
        if 'result' not in op:
            op['result'] = api.request('POST', route, body, expected, op['key'])
            save(self.path, self.data)
        return op['result']


def choose(items, field, label, wanted=None, noninteractive=False):
    if wanted:
        found = [x for x in items if x.get(field) == wanted or x.get('display_name') == wanted]
        if len(found) != 1: raise ValueError('선택한 항목이 없거나 이름이 중복됩니다. 버전 ID를 지정하세요.')
        return found[0]
    if not items: raise ValueError('선택할 항목이 없습니다.')
    for n, item in enumerate(items, 1): print(f'  {n}. {label(item)}')
    if noninteractive:
        if len(items) == 1: return items[0]
        raise ValueError('여러 항목이 있습니다. 선택 ID 옵션을 지정하세요.')
    value = input('번호 선택: ').strip()
    if not value.isdigit() or not 1 <= int(value) <= len(items): raise ValueError('표시된 번호를 입력하세요.')
    return items[int(value) - 1]


def versions(api, kind):
    result = []
    for parent in all_items(api, '/api/' + kind):
        if not parent.get('enabled'): continue
        detail = api.get('/api/' + kind + '/' + parent['agent_id' if kind == 'agents' else 'tool_id'])
        for v in detail.get('versions', []):
            if v['state'] not in ('draft', 'published'): continue
            if kind == 'tools' and v['kind'] != 'knowledge_search_v1': continue
            result.append({**v, 'display_name': parent['display_name']})
    return result


def model_choices(api, connection, embedding_deployment=None):
    out, cursor, seen = [], '', set()
    for _ in range(100):
        page = api.inspect('/api/agent-models/inspect', {'action': 'deployments', 'connection_version_id': connection, **({'cursor': cursor} if cursor else {})})
        out.extend(item for item in page['items'] if item['deployment_id'] != embedding_deployment); cursor = page.get('next_cursor', '')
        if not cursor: return out
        if cursor in seen: raise ValueError('모델 목록의 다음 페이지가 반복됩니다.')
        seen.add(cursor)
    raise ValueError('모델 목록이 너무 큽니다.')


def inspect_resources(api, tool, model):
    if tool['kind'] != 'knowledge_search_v1' or tool['state'] not in ('draft', 'published'):
        raise ValueError('사용 가능한 자연어 검색 도구 버전이 필요합니다.')
    b, k = tool['binding'], tool['binding']['knowledge']
    if model['deployment_id'] == k['embedding_deployment_id']:
        raise ValueError('임베딩 배포는 답변 모델로 사용할 수 없습니다. 별도 답변 LLM을 선택하세요.')
    req = {'action': 'resolve', 'connection_version_id': b['connection_version_id'],
           'model_connection_version_id': k['model_connection_version_id'], 'index_id': k['index_id'], 'index_version_id': k['index_version_id']}
    if k.get('rerank'): req['rerank_deployment_id'] = k['rerank']['deployment_id']
    knowledge = api.inspect('/api/knowledge/inspect', req)
    resolved = knowledge['binding']['knowledge']
    for field in ['index_id', 'index_version_id', 'embedding_space_id', 'embedding_deployment_id']:
        if resolved.get(field) != k.get(field): raise ValueError('검색 도구의 고정 지식·임베딩과 현재 준비 상태가 다릅니다: ' + field)
    if k.get('rerank') != resolved.get('rerank'): raise ValueError('고정 재정렬 모델과 현재 준비 상태가 다릅니다.')
    answer = api.inspect('/api/agent-models/inspect', {'action': 'resolve', 'connection_version_id': model['connection_version_id'], 'deployment_id': model['deployment_id']})
    if answer['model'] != model: raise ValueError('답변 모델의 고정 버전이 달라졌습니다.')
    return {'knowledge_name': knowledge['index_name'], 'chunk_count': knowledge['chunk_count'], 'answer_model': answer['identity']}


def question_value(value, noninteractive):
    if value is None:
        if noninteractive: raise ValueError('--question을 입력하세요.')
        value = input('자료에 질문할 내용: ')
    if not value.strip() or '\0' in value or len(value.encode()) > 8192:
        raise ValueError('질문은 공백이 아닌 UTF-8 8 KiB 이내 문자열이어야 합니다.')
    return value


def check_selection(args, value, prepared=False):
    agent = value['agent_version']
    model = agent['model'] if prepared else value['model']
    tool = value['tool_version']
    created = value.get('created_agent', False) if prepared else agent is None
    if args.new_agent and not created:
        raise ValueError('기존 세션은 기존 Agent를 선택했습니다. 새 Agent는 별도 세션을 사용하세요.')
    if args.agent_version_id and (created or not agent or args.agent_version_id != agent['agent_version_id']):
        raise ValueError('저장한 Agent 선택과 다릅니다.')
    for requested, accepted in [
        (args.tool_version_id, [tool['tool_version_id'], tool.get('display_name')]),
        (args.deployment_id, [model['deployment_id'], value['resources']['answer_model'].get('display_name')]),
        (args.model_connection_version_id, [model['connection_version_id']]),
        (args.name, [value['name']]),
    ]:
        if requested is not None and requested not in accepted:
            raise ValueError('저장한 도구·모델·이름 선택과 다릅니다. 별도 세션을 사용하세요.')


def prepare(args, api, directory):
    settings_path = directory / 'settings.json'
    if settings_path.exists():
        s = private_read(settings_path)
        for key, value in [('host_agent_url', origin(args.agent_url)), ('agent_url', origin(args.container_agent_url)), ('question', args.question)]:
            if value is not None and s[key] != value: raise ValueError('기존 세션과 입력이 다릅니다. 새 질문은 별도 --session-dir을 사용하세요.')
        check_selection(args, s, prepared=True)
        print_summary(s); return s
    status = api.get('/api/status')
    if not status.get('ready') or (status.get('stage'), status.get('schema_version')) != ('A07', 5): raise ValueError('A07 서비스/schema가 준비되지 않았습니다.')
    journal = Journal(directory)
    plan = journal.data.get('plan')
    if not plan:
        question = question_value(args.question, args.non_interactive)
        agent = api.get('/api/agent-versions/' + args.agent_version_id) if args.agent_version_id else None
        if agent is None and not args.new_agent:
            choices = versions(api, 'agents')
            if choices:
                choices.append({'agent_version_id': 'new', 'display_name': '새 시험 Agent 만들기', 'state': '', 'version': ''})
                chosen = choose(choices, 'agent_version_id', lambda x: f"{x['display_name']} · 버전 {x['version']} · {x['state']}", noninteractive=args.non_interactive)
                if chosen['agent_version_id'] != 'new': agent = api.get('/api/agent-versions/' + chosen['agent_version_id'])
        if agent:
            parent = api.get('/api/agents/' + agent['agent_id'])
            if not parent['enabled'] or agent['state'] not in ('draft', 'published'): raise ValueError('선택한 Agent 버전을 실행할 수 없습니다.')
            tool = api.get('/api/tool-versions/' + agent['tool_version_id']);model = agent['model'];publish_tool = False
            resources = inspect_resources(api, tool, model)
            name = parent['display_name']
        else:
            print('자연어 검색 도구를 선택하세요. 초안은 기존 성공 시험 기록이 있어야 발행할 수 있습니다.')
            choices = versions(api, 'tools')
            if not choices: raise ValueError('등록된 자연어 검색 도구가 없습니다. 먼저 지식 검색 도구를 등록해야 합니다.')
            tool = choose(choices, 'tool_version_id', lambda x: f"{x['display_name']} · 버전 {x['version']} · {x['state']}", args.tool_version_id, args.non_interactive)
            publish_tool = tool['state'] == 'draft'
            if publish_tool and not args.publish_tool:
                if args.non_interactive: raise ValueError('선택한 도구는 초안입니다. 발행하려면 --publish-tool을 명시하세요.')
                if input('이 검색 도구 버전을 발행해 Agent에 연결할까요? [y/N]: ').strip().lower() != 'y': raise ValueError('도구를 변경하지 않았습니다.')
            connection = args.model_connection_version_id or tool['binding']['knowledge']['model_connection_version_id']
            print('준비된 답변 모델을 선택하세요.')
            models = model_choices(api, connection, tool['binding']['knowledge']['embedding_deployment_id'])
            if not models: raise ValueError('준비된 답변 LLM이 없습니다. 임베딩 배포는 답변 후보에서 제외합니다. Model Platform에서 답변 생성용 LLM을 준비하세요.')
            selected = choose(models, 'deployment_id', lambda x: f"{x['display_name']} · {x.get('version','')} · {x.get('repo_id') or '저장소 정보 없음'} · {x['deployment_id'][:8]}", args.deployment_id, args.non_interactive)
            model = {'connection_version_id': connection, 'deployment_id': selected['deployment_id'], 'model_version_id': selected['model_version_id']}
            resources = inspect_resources(api, tool, model)
            name = args.name or 'n8n 시험 · ' + resources['knowledge_name']
        plan = {'host_agent_url': origin(args.agent_url), 'agent_url': origin(args.container_agent_url), 'question': question, 'agent_version': agent, 'tool_version': tool, 'model': model, 'resources': resources, 'publish_tool': publish_tool, 'name': name, 'session_id': str(uuid.uuid4())}
        journal.data['plan'] = plan;save(journal.path, journal.data)
    else:
        if origin(args.agent_url) != plan['host_agent_url'] or origin(args.container_agent_url) != plan['agent_url'] or (args.question is not None and args.question != plan['question']): raise ValueError('복구 중인 준비 요청의 주소·질문을 바꾸지 마세요.')
    check_selection(args, plan)
    tool = plan['tool_version']
    if plan['publish_tool']:
        tool = journal.post(api, 'publish_tool', '/api/tool-versions/' + tool['tool_version_id'] + '/publish', {'expected_revision': tool['revision']}, 200)
    if tool['state'] != 'published': raise ValueError('Agent에 연결할 검색 도구는 발행 상태여야 합니다.')
    agent = plan['agent_version']
    if agent is None:
        created = journal.post(api, 'create_agent', '/api/agents', {'display_name': plan['name'], 'description': 'n8n 실제 API 연결 검증용 초안'}, 201)
        definition = {'expected_revision': created['revision'], 'template_id': 'knowledge_qa_v1', 'template_version': 1, 'tool_version_id': tool['tool_version_id'], 'model': plan['model'], 'prompt': '선택한 자료에 근거해 질문에 한국어로 명확하게 답하세요. 자료에 없는 내용은 추측하지 마세요.', 'context_max_chars': 8000, 'max_tokens': 512, 'temperature': 0.2, 'timeout_ms': 300000}
        agent = journal.post(api, 'create_version', '/api/agents/' + created['agent_id'] + '/versions', definition, 201)
    tool = {**tool, 'display_name': plan['tool_version'].get('display_name')}
    settings = {'created_agent': plan['agent_version'] is None, 'schema': 1, 'session_id': plan['session_id'], 'host_agent_url': plan['host_agent_url'], 'agent_url': plan['agent_url'], 'question': plan['question'], 'name': plan['name'], 'mode': 'test' if agent['state'] == 'draft' else 'run', 'agent_version': agent, 'tool_version': tool, 'resources': plan['resources']}
    save(settings_path, settings);print_summary(settings);return settings


def print_summary(s):
    print(json.dumps({'result': 'prepared', 'inference_executed': False, 'agent_name': s['name'], 'mode': s['mode'], 'knowledge': s['resources']['knowledge_name'], 'answer_model': s['resources']['answer_model']['display_name'], 'question': s['question'], 'agent_version_id': s['agent_version']['agent_version_id'], 'agent_published_automatically': False}, ensure_ascii=False, indent=2))


def docker_env(directory):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('COMPOSE_', 'N8N_', 'AGENT_CHECK_'))}
    network = 'ops-infra'
    p = ROOT / '.env'
    if p.exists():
        for line in p.read_text().splitlines():
            if line.startswith('AGENT_NETWORK='): network = line.split('=', 1)[1].strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', network): raise ValueError('AGENT_NETWORK 설정을 확인하세요.')
    env.update(AGENT_CHECK_DIR=str(directory), AGENT_CHECK_UID=str(os.getuid()), AGENT_CHECK_GID=str(os.getgid()), AGENT_NETWORK=network)
    return env


def diagnose(directory, token):
    """Read only Agent records. Never invokes a workflow, model or inspection POST."""
    s = private_read(directory / 'settings.json')
    pointer = private_read(directory / 'accepted.json')
    run_id = str(uuid.UUID(pointer['agent_run_id']))
    api = API(s['host_agent_url'], token)
    run = api.get('/api/agent-runs/' + run_id)
    if run['agent_run_id'] != run_id or run['agent_version_id'] != s['agent_version']['agent_version_id']:
        raise ValueError('저장된 실행·Agent 버전과 응답이 다릅니다.')
    steps = api.get('/api/agent-runs/' + run_id + '/steps')['items']
    def error(value):
        value = value or {}
        return {key: value[key] for key in ('code', 'stage') if key in value}
    report = {
        'diagnostic': 'records_read', 'inference_executed': False,
        'agent_run_id': run_id, 'request_id': run.get('request_id'), 'state': run['state'],
        'error': error(run.get('error')),
        'steps': [{'step_id': step['step_id'], 'state': step['state']} for step in steps],
        'answer_model': {'display_name': s['resources']['answer_model'].get('display_name'),
                         **s['agent_version']['model']},
        'embedding_deployment_id': s['tool_version']['binding']['knowledge']['embedding_deployment_id'],
    }
    report['answer_uses_embedding_deployment'] = report['answer_model']['deployment_id'] == report['embedding_deployment_id']
    if run.get('tool_call_id'):
        call_id = str(uuid.UUID(run['tool_call_id']))
        child = api.get('/api/tool-calls/' + call_id)
        if child['tool_call_id'] != call_id or child.get('parent_agent_run_id') != run_id:
            raise ValueError('검색 기록의 실행 연결이 다릅니다.')
        report['search'] = {'tool_call_id': call_id, 'state': child['state'], 'error': error(child.get('error'))}
    report['next_action'] = ('원인을 해결한 뒤 별도 세션에서 새 시험 Agent를 준비하세요. 기존 실행은 보존합니다.'
        if run['state'] in ('failed', 'interrupted') else '진행 중이면 lookup으로 조회하세요. 새 실행을 만들지 않습니다.'
        if run['state'] in ('accepted', 'running') else 'Agent는 성공했습니다. n8n 실행·결과 처리 오류를 확인하세요.')
    save(directory / 'diagnostic.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def run_container(directory, token, action):
    if os.getuid() == 0: raise ValueError('run/lookup은 Docker 사용 권한이 있는 일반 사용자로 실행하세요.')
    s = private_read(directory / 'settings.json')
    if action == 'run': print('실제 임베딩·검색·답변 모델을 호출합니다. 같은 세션 재실행은 원래 요청을 재사용합니다.', flush=True)
    cmd = ['docker', 'compose', '--project-directory', str(LIVE), '-f', str(LIVE / 'docker-compose.yml'), 'run', '--build', '--rm', '-T', '--name', 'agent-live-' + s['session_id'], 'live-check']
    result = subprocess.run(cmd, env=docker_env(directory), input=json.dumps({'token': token, 'action': action}), text=True)
    report = directory / ('lookup.json' if action == 'lookup' else 'result.json')
    if result.returncode == 0 and report.exists():
        data = private_read(report)
        if data.get('run', {}).get('result'):
            result_data = data['run']['result']
            print('\n답변:\n' + result_data['answer'])
            for citation in result_data['citations']:
                source = citation['source']
                pages = sorted({str(span.get('metadata', {}).get('location', {}).get('page')) for span in source.get('source_spans', []) if span.get('metadata', {}).get('location', {}).get('page') is not None})
                print(f"[{citation['citation_id']}] 청크 {source['chunk_id']}" + (' · 원문 ' + ', '.join(pages) + '쪽' if pages else ''))
        print('\n상세 결과:', report)
    elif result.returncode:
        if (directory / 'accepted.json').exists():
            try: diagnose(directory, token)
            except (ValueError, OSError, KeyError, TypeError):
                print('추가 진단 조회에 실패했습니다. 같은 세션에서 diagnose를 실행하세요.', file=sys.stderr)
        else:
            print('세션을 지우지 마세요. 같은 run 명령으로 원래 요청을 재개하세요.', file=sys.stderr)
    return result.returncode


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['prepare', 'run', 'lookup', 'diagnose'])
    p.add_argument('--session-dir', type=Path, default=DEFAULT_SESSION)
    p.add_argument('--agent-url', default='http://127.0.0.1:16100')
    p.add_argument('--container-agent-url', default='http://agent-gateway:8000')
    p.add_argument('--token-file', type=Path, default=ROOT / '.secrets/api-token')
    p.add_argument('--question');p.add_argument('--agent-version-id');p.add_argument('--new-agent', action='store_true')
    p.add_argument('--tool-version-id');p.add_argument('--deployment-id');p.add_argument('--model-connection-version-id')
    p.add_argument('--name');p.add_argument('--publish-tool', action='store_true');p.add_argument('--non-interactive', action='store_true')
    args = p.parse_args()
    if args.new_agent and args.agent_version_id: raise ValueError('--new-agent와 --agent-version-id 중 하나만 사용하세요.')
    directory = args.session_dir.expanduser().absolute()
    if directory.is_symlink(): raise ValueError('세션 경로에 심볼릭 링크를 사용할 수 없습니다.')
    if not directory.exists():
        if args.action != 'prepare': raise ValueError('먼저 prepare를 실행하세요.')
        directory.mkdir(mode=0o700)
    if not directory.is_dir() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError('세션 폴더는 현재 사용자 소유·0700 권한이어야 합니다.')
    if ROOT.parent in directory.resolve().parents or directory.resolve() == ROOT.parent:
        raise ValueError('질문·결과·비밀이 소스 압축에 섞이지 않도록 세션은 프로젝트 밖에 두세요.')
    lockpath = directory / '.lock'
    fd = os.open(lockpath, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('같은 세션이 실행 중입니다.') from None
        token = args.token_file.expanduser().read_text().strip()
        if len(token) < 16 or '\n' in token or '\r' in token: raise ValueError('Agent API 토큰 파일을 확인하세요.')
        if args.action == 'prepare': prepare(args, API(args.agent_url, token), directory);return 0
        if args.question or args.agent_version_id or args.new_agent or args.tool_version_id or args.deployment_id or args.publish_tool or args.model_connection_version_id or args.name:
            raise ValueError('run/lookup/diagnose는 저장한 설정을 사용합니다. 선택 옵션은 prepare에만 지정하세요.')
        if args.action == 'diagnose': diagnose(directory, token);return 0
        return run_container(directory, token, args.action)


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, EOFError) as exc: raise SystemExit('FAIL: ' + str(exc))
