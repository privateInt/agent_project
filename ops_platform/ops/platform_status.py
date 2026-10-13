#!/usr/bin/env python3
"""Read existing platform APIs without creating jobs, inference, or remote changes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from ops_platform.client import now, new_client
from ops_platform.contracts import RESOURCES, ReadFailure, identifier, require
from ops_platform.state import success_cache
from ops_platform.transport import strict_json


def config(path):
    path = path.expanduser().resolve()
    require(path.stat().st_size <= 65536, 'INVALID_CONFIG')
    raw = strict_json(path.read_bytes())
    require(isinstance(raw, dict) and set(raw) == {'schema', 'platforms'} and raw['schema'] == 1, 'INVALID_CONFIG')
    sources = raw['platforms']
    require(isinstance(sources, dict) and set(sources) <= set(RESOURCES), 'INVALID_CONFIG')
    result = {}
    for name, value in sources.items():
        require(isinstance(value, dict) and set(value) <= {'url', 'token_file', 'enabled'}, 'INVALID_CONFIG')
        require(type(value.get('enabled', True)) is bool, 'INVALID_CONFIG')
        result[name] = dict(value)
        if value.get('token_file') is not None:
            require(isinstance(value['token_file'], str) and bool(value['token_file']), 'INVALID_CONFIG')
            token_path = Path(value['token_file']).expanduser()
            if not token_path.is_absolute():
                token_path = path.parent/token_path
            # Never copy token contents into this configuration or outputs.
            result[name]['token_file'] = str(token_path)
    return result


def sample(envelope):
    data = envelope.get('data') or {}
    result = {k: envelope.get(k) for k in ('status', 'last_success_at', 'error')}
    if data:
        result.update({k: data.get(k) for k in ('id', 'state', 'phase', 'identifiers')})
        result['business_error'] = data.get('error')
        if 'operation' in envelope.get('related', {}):
            result['operation'] = sample(envelope['related']['operation'])
        steps = envelope.get('related', {}).get('steps')
        if steps:
            result['steps'] = ({'status': steps['status'], 'error': steps.get('error')}
                               if steps['status'] != 'ok' else steps['data'])
        tool = envelope.get('related', {}).get('tool_call')
        if tool:
            result['tool_call'] = sample(tool)
    return result


def compact(report):
    if report.get('result') == 'unavailable':
        return report
    ready = report['readiness']
    result = {'result': report['result'], 'gateway': report['gateway']['status'],
              'gateway_scope': 'gateway_only', 'gateway_error': report['gateway'].get('error'),
              'readiness': ready.get('data') if ready['status'] == 'ok' else ready['status'],
              'readiness_error': ready.get('error'), 'resources': {}}
    for name, item in report['resources'].items():
        row = {k: v for k, v in item.items() if k not in ('sample_detail', 'source_path', 'checked_at')}
        if 'sample_detail' in item:
            row['sample'] = sample(item['sample_detail'])
        result['resources'][name] = row
    if 'pinned_agent_run' in report:
        result['pinned_agent_run'] = sample(report['pinned_agent_run'])
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('check', 'list', 'get'))
    p.add_argument('--config', type=Path, default=ROOT/'config.example.json')
    p.add_argument('--state-file', type=Path, default=Path.home()/'.local/state/agent-project-ops/api-last-success.json')
    p.add_argument('--platform', choices=tuple(RESOURCES))
    p.add_argument('--resource')
    p.add_argument('--id')
    p.add_argument('--parent-id')
    p.add_argument('--cursor', default='')
    p.add_argument('--limit', type=int, default=3)
    p.add_argument('--max-pages', type=int, default=1)
    p.add_argument('--timeout', type=float, default=5)
    p.add_argument('--agent-run-id', help='add a GET-only readback of this existing AgentRun')
    p.add_argument('--full', action='store_true', help='include complete normalized envelopes in check output')
    a = p.parse_args(argv)
    require(1 <= a.limit <= 100 and 1 <= a.max_pages <= 20 and 0.1 <= a.timeout <= 30, 'INVALID_ARGUMENT')
    if a.agent_run_id:
        identifier(a.agent_run_id)
    require(a.action == 'check' or (a.platform is not None and a.resource is not None), 'PLATFORM_RESOURCE_REQUIRED')
    require(a.action != 'get' or a.id is not None, 'ID_REQUIRED')
    require(a.action != 'check' or (a.resource is None and a.id is None and a.parent_id is None and not a.cursor and a.max_pages == 1), 'INVALID_ARGUMENT')
    require(not a.agent_run_id or (a.action == 'check' and a.platform in (None, 'agent')), 'INVALID_ARGUMENT')
    require(a.action != 'get' or (not a.cursor and a.parent_id is None and a.max_pages == 1), 'INVALID_ARGUMENT')
    require(a.action != 'list' or a.id is None, 'INVALID_ARGUMENT')
    require(not a.cursor or a.max_pages == 1, 'CURSOR_WITH_WALK_UNSUPPORTED')
    sources = config(a.config)
    with success_cache(a.state_file) as times:
        if a.action == 'check':
            names = [a.platform] if a.platform else list(RESOURCES)
            # Each worker receives a separate timestamp map; merge after the joined reads.
            def run(name):
                local_times = dict(times)
                try:
                    value = new_client(name, sources, a.timeout, local_times).check(a.limit, a.agent_run_id)
                except ReadFailure as exc:
                    value = {'platform': name, 'result': 'unavailable', 'error': {'code': exc.code}}
                return value, local_times
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(run, names))
            platforms = {}
            for name, (value, updated) in zip(names, results):
                for key, stamp in updated.items():
                    if stamp > times.get(key, ''):
                        times[key] = stamp
                platforms[name] = value if a.full else compact(value)
            passed = all(value['result'] == 'passed' for value, _ in results)
            out = {'stage': 'O02-platform-api-readback', 'result': 'passed' if passed else 'attention_required',
                   'checked_at': now(), 'scope': 'bounded_api_readback_not_full_platform_acceptance',
                   'platforms_checked': names, 'platforms': platforms,
                   'business_success_assessed': False, 'inference_requested': False,
                   'remote_mutations_requested': False, 'method': 'GET',
                   'global_counts_supported': False, 'ops_api_ui_implemented': True,
                   'ops_api_ui_running': 'not_checked_by_this_O02_cli'}
        else:
            client = new_client(a.platform, sources, a.timeout, times)
            if a.action == 'get':
                out = client.detail(a.resource, a.id)
            elif a.max_pages > 1:
                out = client.walk(a.resource, a.limit, a.max_pages, a.parent_id)
            else:
                out = client.page(a.resource, a.limit, a.cursor, a.parent_id)
            passed = out['status'] == 'ok'
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ReadFailure as exc:
        print(json.dumps({'result': 'error', 'error': {'code': exc.code},
                          'remote_mutations_requested': False}), file=sys.stderr)
        sys.exit(1)
    except OSError:
        print('{"result":"error","error":{"code":"LOCAL_FILE_UNAVAILABLE"}}', file=sys.stderr)
        sys.exit(1)
