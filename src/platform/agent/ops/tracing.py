#!/usr/bin/env python3
"""Prepare optional tracing; one durable trial/replay; read Agent and Langfuse evidence."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import urllib.parse

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'runtime'), str(ROOT/'n8n/ops'), str(ROOT.parents[2]/'src/infra/langfuse/ops')]
from agent_runtime import observability as obs
from live_check import API, Journal, save, versions, choose
from lf_common import Client, DEFAULT_STATE, read_json, require, HttpFailure, safe_path
from trace_check import millis, stamp
import manage

TRIAL = Path.home()/'.local/state/agent-platform/trace-trial-o02'


class TraceCheckFailure(ValueError): pass


def require(ok, message):
    if not ok: raise TraceCheckFailure(message)


def prepare(state_dir):
    config = manage.load(state_dir)
    projects = Client(config).request('GET','/api/public/projects')['data']
    require(any(x['id'] == config['project_id'] for x in projects), 'Langfuse 프로젝트 연결이 다릅니다.')
    # Copy only the existing project API pair for this initial qualified trial.
    # Never copy the infra admin/database secrets or change its settings/baseline.
    target = safe_path(ROOT/'.secrets/langfuse-tracing.json')
    info = target.parent.stat()
    require(info.st_uid == os.getuid() and stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o077,
            'Agent .secrets 소유자/0700 권한을 확인하세요.')
    value = {'schema':1,'enabled':True,'url':'http://host.docker.internal:16320',
             'project_id':config['project_id'],'public_key':config['public_key'],'secret_key':config['secret_key']}
    reused = target.exists()
    if reused:
        require(target.is_file() and target.stat().st_uid == os.getuid() and target.stat().st_mode & 0o222 == 0
                and json.loads(target.read_text()) == value, '기존 Agent 추적 설정이 다릅니다. 자동 교체하지 않습니다.')
    else:
        fd = os.open(target, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd,'w') as f:
            json.dump(value,f); f.write('\n');f.flush();os.fsync(f.fileno())
        target.chmod(0o444)  # Non-root Compose file-secret; private host parent remains 0700.
    return {'result':'prepared','reused':reused,'project_id':config['project_id'],
            'credential_scope':'existing project API key for initial trial; not read-only',
            'metadata_only':True,'runtime_recreated':False,'inference_executed':False}


def idle():
    sql = "SELECT json_build_object('active_agent_runs',(SELECT count(*) FROM agent.agent_runs WHERE state IN ('accepted','running')),'active_tool_calls',(SELECT count(*) FROM agent.tool_calls WHERE state IN ('accepted','running')));"
    p = subprocess.run(['docker','compose','exec','-T','agent-postgres','psql','-X','-v','ON_ERROR_STOP=1',
                        '-U','agent_admin','-d','agent_platform','-At','-c',sql],cwd=ROOT,capture_output=True,text=True,timeout=15)
    require(p.returncode == 0,'대기열 조회 실패. 서비스를 재시작하지 마세요.')
    value = json.loads(p.stdout)
    require(set(value) == {'active_agent_runs','active_tool_calls'} and all(type(x) is int and x >= 0 for x in value.values()), '대기열 응답 형식이 다릅니다.')
    return {'result':'busy' if any(value.values()) else 'idle_at_check',**value,'restart_executed':False,'inference_executed':False}


def start_trial(api, directory, question=None, wanted=None, replay=False):
    journal = Journal(directory)
    plan = journal.data.get('plan')
    if plan is None:
        require(not replay, '재사용할 시험 요청이 없습니다. 먼저 run을 실행하세요.')
        candidates = [v for v in versions(api,'agents') if v['state']=='published']
        version = choose(candidates,'agent_version_id',lambda v: v['display_name']+' · 버전 '+str(v['version'])+' · '+v['agent_version_id'],wanted)
        question = question or input('자료에 질문할 내용: ').strip()
        require(bool(question.strip()) and len(question.encode()) <= 8192 and '\0' not in question, '질문을 확인하세요.')
        plan = {'agent_url':api.url,'body':{'agent_version_id':version['agent_version_id'],'mode':'run','input':{'question':question}}}
        journal.data['plan'] = plan
        save(journal.path,journal.data)
    require(plan['agent_url']==api.url and (wanted is None or wanted == plan['body']['agent_version_id'])
            and (question is None or question==plan['body']['input']['question']), '원래 시험 요청과 입력이 다릅니다. 기존 파일을 보존하세요.')
    accepted = journal.post(api,'run','/api/agent-runs',plan['body'],202)
    require(obs.valid_id(accepted['agent_run_id']), 'Agent 실행 식별자가 다릅니다.')
    if replay:
        operation=journal.data['operations']['run']
        original=api.get('/api/agent-runs/'+accepted['agent_run_id'])
        require(original['state'] not in ('accepted','running'), '원래 실행이 완료된 후 replay하세요.')
        again=api.request('POST',operation['route'],operation['body'],202,operation['key'])
        require(again['agent_run_id']==accepted['agent_run_id'] and api.get('/api/agent-runs/'+accepted['agent_run_id'])==original,
                '동일 요청의 원래 실행 기록이 달라졌습니다.')
    return accepted['agent_run_id']


def expected_rows(run, steps, call):
    trace = obs.trace_id('agent',run['agent_run_id'])
    rows = {'agent.execute':(obs.span_id(trace,'agent.execute'),None,run['state'],run['request_id'])}
    for s in steps:
        rows['step.'+s['step_id']] = (obs.span_id(trace,'step.'+s['step_id']),rows['agent.execute'][0],s['state'],run['request_id'])
    if call:
        prefix='tool:'+call['tool_call_id']+':'
        rows['tool.execute'] = (obs.span_id(trace,prefix+'tool.execute'),rows['step.search'][0],call['state'],call['request_id'])
        if call['state']=='succeeded':
            for name in ('model.embedding','knowledge.search'):
                rows[name]=(obs.span_id(trace,prefix+name),rows['tool.execute'][0],'succeeded',call['request_id'])
    # A completed generate step necessarily received a response; failures before
    # the HTTP call can legitimately have no generation observation.
    if next(s for s in steps if s['step_id']=='generate')['state']=='succeeded':
        rows['model.chat']=(obs.span_id(trace,'model.chat'),rows['step.generate'][0],'succeeded',run['request_id'])
    return trace,rows


def inspect(api, lf, config, run_id):
    require(obs.valid_id(run_id),'실행 ID가 올바르지 않습니다.')
    run=api.get('/api/agent-runs/'+run_id)
    require(run['agent_run_id']==run_id,'조회 실행 ID가 다릅니다.')
    if run['state'] in ('accepted','running'): return {'result':'pending','business_state':run['state'],'agent_run_id':run_id}
    steps=api.get('/api/agent-runs/'+run_id+'/steps')['items']
    require([s['step_id'] for s in steps]==list(obs.STEPS),'실행 단계 목록이 다릅니다.')
    call=api.get('/api/tool-calls/'+run['tool_call_id']) if run.get('tool_call_id') else None
    if call:
        require(call.get('parent_agent_run_id')==run_id and call.get('parent_step_id')=='search','Tool 부모 실행 연결이 다릅니다.')
    trace, expected=expected_rows(run,steps,call)
    start=millis(run.get('started_at') or run['created_at'])-60000
    end=millis(run.get('completed_at') or run['deadline_at'])+60000
    query=urllib.parse.urlencode({'traceId':trace,'limit':32,'fields':'core,basic,metadata,io,usage,trace_context',
                                 'fromStartTime':stamp(start),'toStartTime':stamp(end)})
    response=lf.request('GET','/api/public/v2/observations?'+query)
    items=response['data']
    require(isinstance(items,list) and not response.get('meta',{}).get('cursor') and len(items)<=16,'추적 응답 개수/페이지를 확인하세요.')
    rows={x['name']:x for x in items}
    require(len(rows)==len(items) and len({x['id'] for x in items})==len(items),'추적 span이 중복됐습니다.')
    allowed=set(expected)|{'model.embedding','knowledge.search','model.chat'}
    require(set(rows)<=allowed,'예상하지 않은 span이 있습니다.')
    for name,row in rows.items():
        require(row['traceId']==trace and row['projectId']==config['project_id'],'Langfuse 프로젝트/trace 연결이 다릅니다.')
        meta=row.get('metadata') or {}
        require(meta.get('agent_run_id')==run_id and meta.get('scope')=='runtime_execution','원래 실행 메타데이터가 다릅니다.')
        require(row.get('input') in (None,'','null') and row.get('output') in (None,'','null'),'추적에 입력/출력 본문이 있습니다.')
        require(row.get('environment')=='agent-platform','추적 환경이 다릅니다.')
        expected_type={'agent.execute':'AGENT','tool.execute':'TOOL','model.chat':'GENERATION',
                       'model.embedding':'EMBEDDING','knowledge.search':'RETRIEVER'}.get(name,'SPAN')
        require(row.get('type')==expected_type,'추적 종류가 다릅니다.')
        require(row.get('startTime') and row.get('endTime') and start<=millis(row['startTime'])<=millis(row['endTime'])<=end,'추적 시간 범위가 다릅니다.')
        if name in expected:
            ident,parent,state,request_id=expected[name]
            require(row['id']==ident and (row.get('parentObservationId') or None)==parent
                    and meta.get('state')==state and meta.get('request_id')==request_id,'원래 ID/단계 상태/부모 연결이 다릅니다.')
            require(row.get('level')==('ERROR' if state in ('failed','interrupted') else 'DEFAULT'),'span 오류 수준이 다릅니다.')
        else:
            require(name=='model.chat' or call is not None,'부모 Tool 기록이 없습니다.')
            prefix='' if name=='model.chat' else 'tool:'+call['tool_call_id']+':'
            parent=obs.span_id(trace,'step.generate') if name=='model.chat' else expected['tool.execute'][0]
            require(row['id']==obs.span_id(trace,prefix+name) and row.get('parentObservationId')==parent,
                    '호출 span ID/부모 연결이 다릅니다.')
        if name in ('tool.execute','model.embedding','knowledge.search') and call:
            require(meta.get('tool_call_id')==call['tool_call_id'] and meta.get('tool_version_id')==call['tool_version_id'],'Tool 실행/버전 연결이 다릅니다.')
        elif name.startswith(('agent.','step.','model.chat')):
            require(meta.get('agent_version_id')==run['agent_version_id'],'Agent 버전 연결이 다릅니다.')
        if name=='agent.execute' and run.get('error'):
            require(meta.get('error_code')==run['error']['code'],'Agent 오류 코드가 다릅니다.')
        if name=='model.chat' and run['state']=='succeeded':
            for key,value in (run.get('result') or {}).get('usage',{}).items():
                if key in ('prompt_tokens','completion_tokens','total_tokens'):
                    require(meta.get(key)==str(value),'원래 모델 토큰 사용량이 다릅니다.')
    missing=sorted(set(expected)-set(rows))
    return {'stage':'O02-Agent-tracing','result':'pending' if missing else 'passed',
            'scope':'runtime observations compared to authoritative Agent API; not full platform acceptance',
            'agent_run_id':run_id,'request_id':run['request_id'],'trace_id':trace,'project_id':config['project_id'],
            'business_state':run['state'],'observations_verified':len(rows),'missing_spans':missing,
            'metadata_only':True,'inference_executed_by_check':False,'trace_post_submitted_by_check':False,
            'pending_reason':'not instrumented, delayed or dropped; never rerun business automatically' if missing else None}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','idle','run','replay','check'])
    p.add_argument('--state-dir',type=Path,default=DEFAULT_STATE)
    p.add_argument('--trial-dir',type=Path,default=TRIAL)
    p.add_argument('--url',default='http://127.0.0.1:16100')
    p.add_argument('--run-id');p.add_argument('--agent-version-id');p.add_argument('--question')
    p.add_argument('--wait-seconds',type=int,default=120)
    a=p.parse_args();require(0<=a.wait_seconds<=610,'대기 시간 범위는 0~610초입니다.')
    if a.action=='prepare': result=prepare(a.state_dir)
    elif a.action=='idle': result=idle()
    else:
        api=API(a.url,(ROOT/'.secrets/api-token').read_text().strip())
        config=manage.load(a.state_dir);client=Client(config)
        directory=safe_path(a.trial_dir)
        if a.action in ('run','replay'):
            directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            require(directory.stat().st_uid==os.getuid() and not directory.stat().st_mode & 0o077,'시험 폴더 0700 권한을 확인하세요.')
            fd=os.open(directory/'lock',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                print('같은 시험 기록은 같은 Agent 요청을 재사용합니다. 최초 run은 실제 임베딩·검색·LLM을 호출할 수 있습니다.',flush=True)
                run_id=start_trial(api,directory,a.question,a.agent_version_id,a.action=='replay')
        else:
            if a.run_id: run_id=a.run_id
            else:
                journal=read_json(directory/'preparation.json')
                run_id=journal['operations']['run']['result']['agent_run_id']
        deadline=time.monotonic()+a.wait_seconds
        while True:
            result=inspect(api,client,config,run_id)
            if result['result']!='pending' or time.monotonic()>=deadline: break
            time.sleep(min(2,max(0,deadline-time.monotonic())))
        result['action']=a.action
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['result'] not in ('pending','busy') else 2


if __name__=='__main__':
    try: sys.exit(main())
    except TraceCheckFailure as exc:
        print('FAIL: '+str(exc),file=sys.stderr);sys.exit(1)
    except (ValueError,KeyError,OSError,TypeError,subprocess.SubprocessError):
        print('FAIL: 추적 연결/기록/검증을 확인하세요. 원래 시험 파일을 보존하고 check로 조회하세요. 비공개 원문은 출력하지 않습니다.',file=sys.stderr)
        sys.exit(1)
