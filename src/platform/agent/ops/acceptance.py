#!/usr/bin/env python3
"""A09 audit/capture/compare: GET-only inspection of existing Agent and n8n records.

No registration, execution, retry, restart, credentials write or session mutation.
capture creates one private digest baseline outside the project; never overwrites it.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
from smoke_console import ROOT, Reader, identifier as uuid_id

sys.path.insert(0, str(ROOT / 'n8n/ops'))
from operational import DEFAULT_SESSION, checked_workflow, node_output, identifier as n8n_id
from api_access import Client, DEFAULT_KEY, read_private

STEPS = ['validate', 'search', 'context', 'generate', 'finalize']
TERMINAL = {'succeeded', 'failed', 'interrupted'}
DEFAULT_BASELINE = ROOT.parent.parent / 'agent_acceptance_a09.json'
MAX_BASELINE = 256 * 1024


class CheckError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise CheckError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class N8nReader:
    """Guard reused workflow-contract helpers from ever sending a mutation."""
    def __init__(self, key):
        self.client = Client(key['url'], key['api_key'])
        self.url = self.client.url

    def request(self, method, path):
        require(method == 'GET' and path.startswith('/api/v1/'), 'READ_ONLY_BOUNDARY')
        return self.client.request('GET', path, timeout=20)

    def executions(self, workflow_id):
        out, cursor, seen = [], '', set()
        from urllib.parse import urlencode
        for _ in range(5):
            query = {'workflowId': n8n_id(workflow_id), 'limit': 100}
            if cursor:
                query['cursor'] = cursor
            page = self.request('GET', '/api/v1/executions?' + urlencode(query))
            out.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                return out
            require(cursor not in seen, 'N8N_CURSOR_REPEATED')
            seen.add(cursor)
        raise CheckError('N8N_EXECUTION_LIMIT_EXCEEDED')


def selected_runs(items, supplied):
    if supplied:
        require(len(supplied) <= 20, 'RUN_SELECTION_LIMIT')
        return sorted(set(uuid_id(x) for x in supplied))
    ordered = sorted(items, key=lambda r: r['created_at'], reverse=True)
    chosen = []
    for state in ('succeeded', 'failed', 'interrupted'):
        match = next((r for r in ordered if r['state'] == state), None)
        if match:
            chosen.append(uuid_id(match['agent_run_id']))
    return sorted(set(chosen))


def inspect_run(api, run_id):
    run_id = uuid_id(run_id)
    r = api.get('/api/agent-runs/' + run_id)
    require(r['agent_run_id'] == run_id and r['state'] in TERMINAL, 'RUN_NOT_TERMINAL_OR_ID_CHANGED')
    steps = api.get('/api/agent-runs/' + run_id + '/steps')['items']
    require([x['step_id'] for x in steps] == STEPS, 'STEP_SEQUENCE_CHANGED')
    v = api.get('/api/agent-versions/' + uuid_id(r['agent_version_id']))
    require(v['agent_version_id'] == r['agent_version_id'] and
            v['definition_sha256'] == r['definition_sha256'], 'AGENT_DEFINITION_CHANGED')
    record = {'agent_run_id': run_id, 'agent_version_id': r['agent_version_id'],
              'request_id': r['request_id'], 'state': r['state'],
              'run_sha256': digest(r), 'steps_sha256': digest(steps),
              'agent_definition_sha256': v['definition_sha256']}
    record['agent_version_content_sha256'] = digest({k: val for k, val in v.items() if k not in ('state', 'revision')})
    summary = {k: record[k] for k in ('agent_run_id', 'agent_version_id', 'request_id', 'state')}
    summary['steps'] = [{'step': x['step_id'], 'state': x['state']} for x in steps]
    if r.get('error'):
        summary['error'] = {k: r['error'].get(k) for k in ('code', 'stage')}
    child = None
    if r.get('tool_call_id'):
        child = api.get('/api/tool-calls/' + uuid_id(r['tool_call_id']))
        require(child['tool_call_id'] == r['tool_call_id'] and child['parent_agent_run_id'] == run_id
                and child['parent_step_id'] == 'search' and child['request_id'] == r['request_id']
                and child['tool_version_id'] == v['tool_version_id'], 'SEARCH_CORRELATION_CHANGED')
        tv = api.get('/api/tool-versions/' + uuid_id(child['tool_version_id']))
        require(tv['tool_version_id'] == child['tool_version_id'] and tv['kind'] == 'knowledge_search_v1'
                and tv['definition_sha256'] == child['definition_sha256'], 'TOOL_DEFINITION_CHANGED')
        record.update(tool_call_id=child['tool_call_id'], tool_call_sha256=digest(child),
                      tool_definition_sha256=tv['definition_sha256'])
        record['tool_version_content_sha256'] = digest({k: val for k, val in tv.items() if k not in ('state', 'revision')})
        summary.update(tool_call_id=child['tool_call_id'], search_state=child['state'])
    if r['state'] == 'succeeded':
        result = r['result']
        require(child is not None and child['state'] == 'succeeded'
                and result['tool_call_id'] == child['tool_call_id']
                and all(s['state'] in ('succeeded', 'skipped') for s in steps), 'SUCCESS_CHAIN_INVALID')
        require(result['outcome'] in ('answered', 'no_evidence') and isinstance(result['answer'], str)
                and result['answer'].strip() and isinstance(result['citations'], list), 'ANSWER_SHAPE_INVALID')
        identity = result['identity']
        require(identity['tool_version_id'] == v['tool_version_id'] and
                identity['knowledge'] == child['result']['metadata'], 'RESULT_IDENTITY_CHANGED')
        if identity.get('model'):
            require(all(identity['model'].get(k) == v['model'][k] for k in ('deployment_id', 'model_version_id')),
                    'MODEL_IDENTITY_CHANGED')
        if result['outcome'] == 'answered':
            require(bool(identity.get('model')), 'ANSWERED_WITHOUT_MODEL_IDENTITY')
            sources = child['result']['data']['results']
            selected = result['selected_chunk_ids']
            citations = result['citations']
            wanted = set(re.findall(r'\[S([^\]\r\n]*)\]', result['answer']))
            ids = [c['citation_id'] for c in citations]
            require(wanted and len(ids) == len(set(ids)) and
                    all(re.fullmatch('[1-9][0-9]*', x) for x in wanted) and
                    {'S' + x for x in wanted} == set(ids), 'CITATION_REFERENCE_INVALID')
            for c in citations:
                index = int(c['citation_id'][1:]) - 1
                require(0 <= index < len(selected) and c['source']['chunk_id'] == selected[index]
                        and c['source'] in sources, 'CITATION_SOURCE_CHANGED')
        else:
            require(result['citations'] == [], 'NO_EVIDENCE_HAS_CITATIONS')
        summary.update(outcome=result['outcome'], citation_count=len(result['citations']),
                       answer_truncated=result['answer_truncated'])
    else:
        require(bool(r.get('error', {}).get('code')), 'FAILURE_WITHOUT_ERROR')
    # Immutable terminal records should not change while their chain is inspected.
    require(digest(api.get('/api/agent-runs/' + run_id)) == record['run_sha256'], 'RUN_CHANGED_DURING_READ')
    return record, summary, r


def read_session(directory, key_file, request_file, api):
    if not directory.exists():
        return None
    require(directory.is_dir() and not directory.is_symlink() and
            directory.stat().st_uid == os.getuid() and not directory.stat().st_mode & 0o077,
            'SESSION_DIRECTORY_PERMISSIONS')
    paths = [directory / 'settings.json', request_file or directory / 'request.json']
    state, request = (read_private(p) for p in paths)
    key = read_private(key_file)
    client = N8nReader(key)
    require(state.get('prepared') and state['n8n_url'] == client.url and
            state['agent_url'] == api.url and request['session_id'] == state['session_id'], 'SESSION_BINDING_CHANGED')
    checked_workflow(client, state)  # GET only; reject edited content without overwriting it.
    workflow = client.request('GET', '/api/v1/workflows/' + n8n_id(state['resources']['workflow']['id']))
    link = api.get('/api/workflow-links/' + uuid_id(state['workflow_link_id']))
    require(link['workflow_id'] == workflow['id'] and link['agent_version_id'] == state['agent_version_id'],
            'WORKFLOW_LINK_CHANGED')
    return {'state': state, 'request': request, 'client': client, 'workflow': workflow, 'link': link,
            'paths': paths, 'fingerprints': [digest(state), digest(request)]}


def inspect_n8n(session, api, pinned_ids=None):
    if session is None:
        return {'status': 'session_missing', 'delivery_verified': False, 'replay_verified': False}, {}, None
    state, request, client = (session[k] for k in ('state', 'request', 'client'))
    wf = session['workflow']
    request_id = uuid_id(request['body']['request_id'])
    run_ids = set()
    receipt = request.get('receipt') or {}
    if receipt.get('agent_run_id'):
        run_ids.add(uuid_id(receipt['agent_run_id']))
    executions = []
    if pinned_ids is None:
        ids = [n8n_id(x['id']) for x in client.executions(wf['id'])]
    else:
        require(isinstance(pinned_ids, list) and len(pinned_ids) <= 500, 'BASELINE_EXECUTION_IDS_INVALID')
        ids = [n8n_id(x) for x in pinned_ids]
    for eid in sorted(set(ids)):
        e = client.request('GET', '/api/v1/executions/' + eid + '?includeData=true')
        prepared = node_output(e, 'Prepare request')
        if prepared and prepared.get('request', {}).get('invocation_key') == request_id:
            require(str(e['workflowId']) == wf['id'], 'EXECUTION_WORKFLOW_CHANGED')
            executions.append(e)
            pointer = node_output(e, 'Run pointer')
            if pointer:
                run_ids.add(uuid_id(pointer['agent_run_id']))
    require(len(run_ids) <= 1, 'DUPLICATE_AGENT_RUNS_FOR_REQUEST')
    binding = {k: state[k] for k in ('session_id', 'agent_version_id', 'definition_sha256', 'workflow_link_id')}
    binding.update(workflow_id=wf['id'], logical_request_id=request_id,
                   question_sha256=digest(request['body']['question']), n8n_origin_sha256=digest(client.url))
    records = {'binding': binding, 'workflow_sha256': digest({k: wf.get(k) for k in
                ('id', 'name', 'nodes', 'connections', 'settings', 'active', 'activeVersionId', 'versionId')}),
               'link_sha256': digest(session['link']), 'executions': {}}
    for e in executions:
        if e['status'] in ('success', 'error', 'crashed', 'canceled'):
            records['executions'][n8n_id(e['id'])] = digest({k: e.get(k) for k in
                ('id', 'workflowId', 'status', 'startedAt', 'stoppedAt', 'mode', 'data')})
    summary = {'status': 'checked', 'workflow_id': wf['id'], 'workflow_link_id': state['workflow_link_id'],
               'workflow_active': bool(wf.get('active')), 'published_version_matches':
                   bool(wf.get('active')) and wf.get('activeVersionId') == state.get('published_version_id'),
               'execution_states': [{'id': e['id'], 'state': e['status']} for e in executions],
               'delivery_verified': False, 'replay_verified': False,
               'pending_executions': sum(e['status'] not in ('success', 'error', 'crashed', 'canceled') for e in executions)}
    rid = next(iter(run_ids), None)
    if rid:
        run = api.get('/api/agent-runs/' + rid)
        expected = {'system': 'n8n', 'workflow_id': wf['id'], 'execution_id': 'request:' + request_id, 'node_id': 'agent-call'}
        require(run['agent_version_id'] == state['agent_version_id'] and run['mode'] == 'run'
                and run['definition_sha256'] == state['definition_sha256'] and run['invocation_key'] == request_id
                and run['input'] == {'question': request['body']['question']} and run['caller_context'] == expected,
                'OPERATIONAL_REQUEST_CORRELATION_CHANGED')
        summary.update(agent_run_id=rid, agent_state=run['state'])
        if run.get('error'):
            summary['error'] = {k: run['error'].get(k) for k in ('code', 'stage')}
        completed = []
        for e in executions:
            output = node_output(e, 'Answer and sources')
            if e['status'] == 'success' and output:
                require(run['state'] == 'succeeded', 'N8N_SUCCESS_FOR_FAILED_AGENT')
                require(all(output.get(k) == run.get(k) for k in
                            ('agent_run_id', 'agent_version_id', 'request_id', 'tool_call_id')) and
                        all(output.get(k) == run['result'].get(k) for k in
                            ('answer', 'citations', 'outcome', 'warnings', 'answer_truncated')), 'N8N_DELIVERY_CHANGED')
                completed.append(e['id'])
        summary['delivery_verified'] = bool(completed) and (not receipt.get('n8n_execution_id') or receipt['n8n_execution_id'] in completed)
        summary['replay_verified'] = summary['delivery_verified'] and len(set(completed)) >= 2
    require([digest(read_private(p)) for p in session['paths']] == session['fingerprints'], 'SESSION_CHANGED_DURING_READ')
    return summary, records, rid


def collect(api, directory, key_file, request_file=None, supplied=(), baseline=None):
    status = api.get('/api/status')
    require(status.get('ready') and status.get('stage') == 'A07' and status.get('schema_version') == 5,
            'AGENT_NOT_READY_OR_CONTRACT_CHANGED')
    items = api.items('/api/agent-runs')
    # An Agent-only baseline stays Agent-only after recovering a missing session.
    # Broaden inspection with audit or a separately named new baseline, never by overwrite.
    if baseline is not None and not baseline['n8n']:
        n8n = {'status': 'not_captured_in_baseline', 'delivery_verified': False, 'replay_verified': False}
        n8n_records, operational_run = {}, None
    else:
        session = read_session(directory, key_file, request_file, api)
        pinned = list(baseline['n8n'].get('executions', {})) if baseline else None
        n8n, n8n_records, operational_run = inspect_n8n(session, api, pinned)
    chosen = list(baseline['runs']) if baseline else selected_runs(items, supplied)
    if operational_run and not baseline:
        current = api.get('/api/agent-runs/' + operational_run)
        if current['state'] in TERMINAL:
            chosen = sorted(set(chosen + [operational_run]))
    records, summaries = {}, []
    for rid in chosen:
        records[rid], summary, _ = inspect_run(api, rid)
        summaries.append(summary)
    answered = any(r.get('outcome') == 'answered' for r in summaries)
    pending = ['user_page_acceptance_deferred', 'semantic_quality_deferred', 'full_restart_acceptance_not_attested']
    if not answered:
        pending.append('answered_record_not_selected')
    if not n8n['delivery_verified']:
        pending.append('operational_n8n_answer_delivery')
    if not n8n['replay_verified']:
        pending.append('operational_n8n_same_request_replay')
    if not records:
        pending.append('terminal_records_not_available')
    report = {'stage': 'A09', 'result': 'checked', 'record_checks': 'passed' if records else 'not_available', 'mvp_acceptance': 'incomplete',
              'inference_executed': False, 'remote_mutations': False, 'active_agent_runs':
                  sum(r['state'] in ('accepted', 'running') for r in items), 'tool_queue_checked': False,
              'catalog_run_count': len(items), 'tracked_run_count': len(records), 'runs': summaries,
              'n8n': n8n, 'pending': pending}
    snapshot = {'schema': 1, 'agent_origin_sha256': digest(api.url), 'runs': records, 'n8n': n8n_records}
    return report, snapshot


def baseline_path(path):
    path = path.expanduser().absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'BASELINE_SYMLINK')
    require(ROOT.parent != path.resolve() and ROOT.parent not in path.resolve().parents, 'BASELINE_MUST_BE_OUTSIDE_PROJECT')
    require(path.parent.is_dir(), 'BASELINE_PARENT_MISSING')
    return path


def load_baseline(path):
    require(path.stat().st_size <= MAX_BASELINE, 'BASELINE_SIZE_LIMIT')
    value = read_private(path)
    require(value.get('schema') == 1 and isinstance(value.get('runs'), dict)
            and 1 <= len(value['runs']) <= 21 and isinstance(value.get('n8n'), dict), 'BASELINE_INVALID')
    for rid in value['runs']:
        uuid_id(rid)
    return value


def save_baseline(path, value):
    require(value['runs'], 'NO_TERMINAL_RECORDS_TO_CAPTURE')
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    require(len(data) <= MAX_BASELINE, 'BASELINE_SIZE_LIMIT')
    # No existing baseline is ever overwritten, including concurrent captures.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def comparison(before, after):
    changed = []
    for name in ('agent_origin_sha256', 'runs', 'n8n'):
        if before.get(name) != after.get(name):
            changed.append(name)
    return changed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['audit', 'capture', 'compare'])
    p.add_argument('--url', default='http://127.0.0.1:16100')
    p.add_argument('--token-file', type=Path, default=ROOT / '.secrets/api-token')
    p.add_argument('--session-dir', type=Path, default=DEFAULT_SESSION)
    p.add_argument('--key-file', type=Path, default=DEFAULT_KEY)
    p.add_argument('--request-file', type=Path)
    p.add_argument('--baseline-file', type=Path, default=DEFAULT_BASELINE)
    p.add_argument('--run-id', action='append', default=[], help='Select up to 20 existing terminal Agent runs')
    a = p.parse_args()
    path = baseline_path(a.baseline_file) if a.action != 'audit' else None
    before = load_baseline(path) if path and path.exists() else None
    require(a.action != 'compare' or before is not None, 'CAPTURE_REQUIRED')
    require(not (a.run_id and before), 'BASELINE_ALREADY_PINS_RUNS')
    api = Reader(a.url, a.token_file.expanduser().read_text().strip())
    report, current = collect(api, a.session_dir.expanduser().absolute(), a.key_file.expanduser(),
                              a.request_file.expanduser() if a.request_file else None, a.run_id, before)
    if before:
        changed = comparison(before, current)
        report.update(result='changed' if changed else 'preserved', changed_sections=changed,
                      preservation='changed' if changed else 'passed', baseline_file=str(path))
    elif a.action == 'capture':
        require(not report['n8n'].get('pending_executions') and report['n8n'].get('agent_state') not in
                ('accepted', 'running'), 'WAIT_FOR_N8N_EXECUTIONS_BEFORE_CAPTURE')
        current['captured_at'] = datetime.now(timezone.utc).isoformat()
        save_baseline(path, current)
        report.update(result='captured', baseline_file=str(path))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report['result'] == 'changed' else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        # Never echo private file contents, upstream bodies, request data or credentials.
        print(json.dumps({'stage': 'A09', 'result': 'failed', 'code': str(exc) if isinstance(exc, CheckError)
                          else 'READ_OR_CONTRACT_CHECK_FAILED', 'inference_executed': False,
                          'remote_mutations': False, 'hint': 'Check access/settings/retained records; preserve all existing sessions and the baseline.'}))
        raise SystemExit(1)
