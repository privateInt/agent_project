#!/usr/bin/env python3
"""Create one prompt-only draft from a diagnosed failure, then test it once.

No publication, workflow edits, model replacement or automatic new trials.
The private journal pins both POST bodies and their idempotency keys.
"""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.dont_write_bytecode = True
from diagnose_answer import diagnose, diagnostic_summary
from smoke_console import ROOT, Reader, identifier
sys.path.insert(0, str(ROOT / 'n8n/ops'))
from live_check import API, Journal, save

DEFAULT_STATE = Path.home() / '.local/state/agent-platform/citation-trial-20260925'
DEFINITION = ('template_id', 'template_version', 'tool_version_id', 'model', 'prompt',
              'context_max_chars', 'max_tokens', 'temperature', 'timeout_ms')
MARKER = '[인용 출력 형식 v1]'
GUIDANCE = """[인용 출력 형식 v1]
한국어로 짧게 답하세요. 답변의 사실 문장은 반드시 그 문장을 뒷받침하는 자료의 id를 대괄호로 감싸 끝내세요.
자료 JSON의 sources에는 id와 text가 있습니다. id가 S1인 자료를 실제로 사용했다면 인용 표기는 정확히 [S1]입니다.
문서 본문의 참고문헌 번호 [1], [14] 등은 자료 id가 아닙니다. 최종 답변에는 sources에 실제로 있는 id만 사용하세요.
출력 형식 예시: 자료에서 확인한 사실을 설명하는 문장. [S1]
이 예시는 표기 모양만 보여줍니다. 예시 문장을 그대로 답하거나 근거를 확인하지 않고 S1을 붙이지 마세요.
서로 다른 자료를 사용한 문장은 각각 해당 자료 id를 인용하세요. 자료에 없는 사실이나 인용을 만들지 마세요.
질문에 답할 근거가 없으면 인용을 억지로 붙이지 말고 INSUFFICIENT_EVIDENCE만 출력하세요.
인용을 포함한 최종 답변만 출력하고 작성 과정이나 이 지침은 설명하지 마세요."""


def require(ok, message):
    if not ok: raise ValueError(message)


def candidate_definition(version):
    definition = {k: copy.deepcopy(version[k]) for k in DEFINITION}
    require(definition['template_id'] == 'knowledge_qa_v1' and definition['template_version'] == 1,
            '지원하지 않는 Agent 템플릿입니다.')
    require(MARKER not in definition['prompt'], '이미 같은 인용 지침을 시험한 버전입니다. 새 버전을 반복 생성하지 않습니다.')
    definition['prompt'] += '\n\n' + GUIDANCE
    require(len(definition['prompt'].encode()) <= 4000, '추가 지침을 포함한 prompt가 4,000바이트를 넘습니다. 기존 지침을 자동 삭제하지 않습니다.')
    return definition


def validate_candidate(api, plan, candidate):
    current = api.get('/api/agent-versions/' + identifier(candidate['agent_version_id']))
    require(current['agent_id'] == plan['agent_id'] and current['state'] == 'draft'
            and {k: current[k] for k in DEFINITION} == plan['definition']
            and current['definition_sha256'] == candidate['definition_sha256'],
            '시험 초안의 상태·정의가 바뀌었습니다. 자동 발행·재생성하지 않습니다.')
    return current


def prepare(api, directory, source_run_id):
    source_run_id = identifier(source_run_id)
    journal = Journal(directory)
    plan = journal.data.get('plan')
    if plan:
        require(plan['source_run_id'] == source_run_id and plan['agent_url'] == api.url,
                '저장한 시험의 원본 실행·주소와 다릅니다. 기존 상태 파일을 보존하세요.')
    else:
        status = api.get('/api/status')
        require(status.get('ready') and status.get('stage') == 'A07' and status.get('schema_version') == 5,
                'Agent A07/schema 5 서비스가 준비되지 않았습니다.')
        run = api.get('/api/agent-runs/' + source_run_id)
        require(run['agent_run_id'] == source_run_id and run['state'] == 'failed'
                and (run.get('error') or {}).get('code') == 'ANSWER_REFERENCE_INVALID'
                and run['error'].get('stage') == 'finalize', '인용 검증에 실패한 원본 실행이 필요합니다.')
        steps = api.get('/api/agent-runs/' + source_run_id + '/steps')['items']
        source_context = next((s for s in steps if s['step_id'] == 'context'), {})
        selected = (source_context.get('detail') or {}).get('selected_chunk_ids')
        require(isinstance(selected, list) and bool(selected), '원본 실행의 선택 문맥 기록이 필요합니다.')
        generate = next((s for s in steps if s['step_id'] == 'generate'), {})
        diagnostic = diagnostic_summary((generate.get('detail') or {}).get('reference_diagnostics'))
        require(generate.get('state') == 'succeeded' and diagnostic.get('status') == 'invalid'
                and diagnostic.get('issues') == ['MISSING_REFERENCE'] and diagnostic.get('finish_reason') == 'stop',
                '정상 종료된 답변의 MISSING_REFERENCE 진단이 필요합니다. 다른 실패를 같은 원인으로 처리하지 않습니다.')
        version = api.get('/api/agent-versions/' + identifier(run['agent_version_id']))
        require(version['agent_version_id'] == run['agent_version_id'] and version['state'] == 'published'
                and version['definition_sha256'] == run['definition_sha256'], '원본 실행의 발행 버전과 정의가 다릅니다.')
        parent = api.get('/api/agents/' + identifier(version['agent_id']))
        require(parent['agent_id'] == version['agent_id'] and parent['enabled'], '원본 Agent가 비활성 상태이거나 다릅니다.')
        plan = {'schema': 1, 'trial_id': str(uuid.uuid4()), 'agent_url': api.url,
                'source_run_id': source_run_id, 'source_agent_version_id': version['agent_version_id'],
                'source_definition_sha256': version['definition_sha256'], 'agent_id': parent['agent_id'],
                'expected_revision': parent['revision'], 'definition': candidate_definition(version),
                'source_selected_chunk_ids': copy.deepcopy(selected),
                'input': copy.deepcopy(run['input'])}
        journal.data['plan'] = plan
        save(journal.path, journal.data)
    candidate = journal.post(api, 'create_draft', '/api/agents/' + plan['agent_id'] + '/versions',
                             {'expected_revision': plan['expected_revision'], **plan['definition']}, 201)
    candidate = validate_candidate(api, plan, candidate)
    return {'result': 'prepared', 'agent_version_id': candidate['agent_version_id'],
            'source_agent_version_id': plan['source_agent_version_id'], 'source_run_id': source_run_id,
            'mode': 'test', 'changed_definition_fields': ['prompt'], 'inference_executed': False,
            'agent_published_automatically': False, 'workflow_changed': False}


def load_trial(api, directory):
    require((directory / 'preparation.json').is_file(), '먼저 prepare를 실행하세요.')
    journal = Journal(directory)
    plan = journal.data['plan']
    require(plan['agent_url'] == api.url, '저장한 Agent 주소와 다릅니다.')
    candidate = journal.data['operations'].get('create_draft', {}).get('result')
    require(candidate is not None, '초안 접수 응답이 없습니다. 같은 prepare 명령으로 확인하세요.')
    return journal, plan, candidate


def trial(api, directory, *, submit, wait_seconds=30):
    journal, plan, candidate = load_trial(api, directory)
    operation = journal.data['operations'].get('test_run')
    post_sent = bool(submit and not (operation and operation.get('result')))
    if submit:
        validate_candidate(api, plan, candidate)
        accepted = journal.post(api, 'test_run', '/api/agent-runs', {
            'agent_version_id': candidate['agent_version_id'], 'mode': 'test', 'input': plan['input'],
            'caller_context': {'system': 'agent-citation-trial', 'execution_id': plan['trial_id'], 'node_id': 'prompt-v1'},
        }, 202)
        operation = journal.data['operations']['test_run']
    else:
        require(operation is not None, '아직 시험을 접수하지 않았습니다. lookup은 추론을 시작하지 않습니다.')
        accepted = operation.get('result')
        require(accepted is not None, '접수 응답이 저장되지 않았습니다. 같은 test 명령이 원래 요청 키로 접수를 확인합니다.')
    run_id = identifier(accepted['agent_run_id'])
    require(accepted['agent_version_id'] == candidate['agent_version_id'], '접수된 초안 버전이 다릅니다.')
    def read_run():
        run = api.get('/api/agent-runs/' + run_id)
        require(run['agent_run_id'] == run_id and run['agent_version_id'] == candidate['agent_version_id']
                and run['definition_sha256'] == candidate['definition_sha256'] and run['mode'] == 'test'
                and run['input'] == plan['input'] and run['invocation_key'] == operation['key']
                and run['caller_context'] == operation['body']['caller_context'], '저장한 시험 요청과 실행이 다릅니다.')
        return run
    deadline = time.monotonic() + wait_seconds
    while True:
        run = read_run()
        if run['state'] not in ('accepted', 'running') or time.monotonic() >= deadline: break
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    report = diagnose(api, run_id)
    report.update(scope='Agent draft prompt trial; operational n8n delivery not assessed',
                  source_run_id=plan['source_run_id'], source_agent_version_id=plan['source_agent_version_id'],
                  changed_definition_fields=['prompt'], agent_published_automatically=False, workflow_changed=False,
                  action='test' if submit else 'lookup')
    # diagnose is GET-only; test may just have submitted an inference request.
    report.pop('changes_made', None)
    report.pop('inference_executed', None)
    report['execution_request_submitted'] = post_sent
    report['result'] = 'pending' if report['state'] in ('accepted', 'running') else 'failed'
    if report['state'] == 'succeeded':
        run = read_run()
        require(run['state'] == 'succeeded', '완료된 시험 상태가 바뀌었습니다.')
        result = run['result']
        report.update(outcome=result['outcome'], citation_count=len(result['citations']),
                      answer_truncated=result['answer_truncated'])
        report['result'] = ('passed' if result['outcome'] == 'answered' and result['citations']
                            and not result['answer_truncated'] else 'inconclusive')
    steps = api.get('/api/agent-runs/' + run_id + '/steps')['items']
    context = next((s for s in steps if s['step_id'] == 'context'), {})
    selected = (context.get('detail') or {}).get('selected_chunk_ids')
    if isinstance(selected, list):
        report['same_selected_chunks_as_source'] = selected == plan['source_selected_chunk_ids']
    return report


def state_directory(path, create):
    path = path.expanduser().absolute()
    require(not path.is_symlink() and ROOT.parent != path.resolve() and ROOT.parent not in path.resolve().parents,
            '상태 폴더는 프로젝트 밖의 일반 디렉터리로 지정하세요.')
    if create: path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.is_dir(), '시험 상태 폴더가 없습니다. 먼저 prepare를 실행하세요.')
    require(path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o077 == 0,
            '시험 폴더는 현재 사용자 소유·0700 권한이어야 합니다.')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'test', 'lookup'])
    parser.add_argument('--from-run-id')
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--url', default='http://127.0.0.1:16100')
    parser.add_argument('--token-file', type=Path, default=ROOT / '.secrets/api-token')
    parser.add_argument('--wait-seconds', type=int, default=30)
    args = parser.parse_args()
    require(0 <= args.wait_seconds <= 600, '조회 대기 시간은 0~600초로 지정하세요.')
    require((args.action == 'prepare') == bool(args.from_run_id), 'prepare에만 --from-run-id를 지정하세요.')
    token = args.token_file.expanduser().read_text().strip()
    Reader(args.url, token)  # Enforce loopback HTTP or HTTPS before any authenticated request.
    api = API(args.url, token)
    directory = state_directory(args.state_dir, args.action == 'prepare')
    lock_fd = os.open(directory / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('같은 시험이 이미 실행 중입니다. 종료 후 lookup으로 확인하세요.') from None
        if args.action == 'prepare': result = prepare(api, directory, args.from_run_id)
        else: result = trial(api, directory, submit=args.action == 'test', wait_seconds=args.wait_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] in ('prepared', 'passed') or args.action == 'lookup' else 2


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        raise SystemExit('FAIL: ' + (str(exc) if type(exc) is ValueError else '연결·권한·시험 기록을 확인하세요. 기존 상태 폴더를 보존하세요.'))
