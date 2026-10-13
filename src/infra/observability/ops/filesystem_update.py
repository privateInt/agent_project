#!/usr/bin/env python3
"""Add /mnt/nvme1 coverage; replace only pinned Prometheus once, preserve old acceptance."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import alerts as engine
import manage as op
import restart_check as retained

from src.ops.platform.observations import QUERIES

RULE_FILE = 'alerts/o04.rules.yml'
OLD_RULE_HASH = '82b7f967426fa189d2f7024b09eddd3ffdc19d8c50132e1ed3c4f85f42dab99a'
NEW_RULE_HASH = 'b1f3bc8cdfc9c56c32c47766980ae50d7d83efa64884477e8a5fbb9198366fc8'
KIND = 'O04-filesystem-coverage'
MOUNT = '/mnt/nvme1'


def binding(runtime, state):
    value = engine.binding(runtime, state)
    value['files'].update({p: retained.file_hash(runtime.root / p) for p in (
        'alerts/.state.json', 'alerts/filesystem_test.yml', 'ops/filesystem_update.py')})
    return value


def manager(runtime, state):
    items = engine.manager_containers(runtime.docker, state)
    op.require(len(items) == 1, 'FILESYSTEM_ALERTMANAGER_MISSING')
    item = items[0]
    op.require(item['State']['Running'] and item['State'].get('Health', {}).get('Status') == 'healthy',
               'FILESYSTEM_ALERTMANAGER_UNHEALTHY')
    return {'id': item['Id'], 'image_id': item['Image'], 'started_at': item['State']['StartedAt']}


def prior_guard(runtime, state):
    path = Path(runtime.state['data_dir']).parent / 'alerts-deployment-o04.json'
    old = engine.read_record(path)
    op.require(old.get('report', {}).get('result') == 'passed', 'FILESYSTEM_REQUIRES_O04_ACCEPTED')
    current = engine.binding(runtime, state)
    op.require(current['files'][RULE_FILE] == NEW_RULE_HASH
               and old['baseline']['binding']['files'][RULE_FILE] == OLD_RULE_HASH,
               'FILESYSTEM_UNEXPECTED_RULE_VERSION')
    expected = copy.deepcopy(current)
    expected['files'][RULE_FILE] = OLD_RULE_HASH
    op.require(expected == old['baseline']['binding'], 'FILESYSTEM_OTHER_O04_CONFIG_CHANGED')
    op.require(runtime.containers() == old['accepted_containers'], 'FILESYSTEM_PRIOR_CONTAINERS_CHANGED')
    am = manager(runtime, state)
    op.require({k: am[k] for k in ('id', 'started_at')} == old['accepted_manager'],
               'FILESYSTEM_PRIOR_ALERTMANAGER_CHANGED')
    return retained.file_hash(path)


def native_tests(runtime):
    root = runtime.root
    common = ['run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
              '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
              '--user', str(os.getuid()) + ':' + str(os.getgid()), '--memory', '256m',
              '--memory-swap', '256m', '--cpus', '1', '--pids-limit', '64',
              '--tmpfs', '/tmp:size=16777216,mode=1777']
    for source, target in (('alerts/prometheus.yml', '/etc/prometheus/prometheus.yml'),
                           (RULE_FILE, '/etc/prometheus/rules/o04.rules.yml'),
                           ('alerts/filesystem_test.yml', '/etc/prometheus/rules/filesystem_test.yml')):
        common += ['--mount', 'type=bind,src=' + str(root / source) + ',dst=' + target + ',readonly']
    common += ['--workdir', '/etc/prometheus/rules', '--entrypoint', '/bin/promtool',
               runtime.state['images']['obs-prometheus']['digest']]
    print('Filesystem preflight: native config and 8 filesystem rule scenarios.', flush=True)
    runtime.docker.run(common + ['check', 'config', '/etc/prometheus/prometheus.yml'], visible=True)
    runtime.docker.run(common + ['test', 'rules', 'filesystem_test.yml'], visible=True)
    return {'promtool_config': 'passed', 'filesystem_scenarios': 8, 'expectation_times': 24}


def filesystems(runtime):
    rows = runtime.prom.query(QUERIES['filesystems'][2])
    values = op.values(rows)
    op.require(values and all(0 <= value <= 100 for _, value in values), 'FILESYSTEM_NO_VALID_SAMPLES')
    result = sorted([{'labels': labels, 'usage_percent': value} for labels, value in values],
                    key=lambda item: retained.encoded(item['labels']))
    mounts = {row['labels'].get('mountpoint') for row in result}
    op.require({'/', MOUNT} <= mounts, 'FILESYSTEM_NVME_OR_ROOT_NOT_COLLECTED')
    return result


def history(runtime, end):
    value = retained.history(runtime.prom, end)
    value[engine.GPU_QUERY] = retained.matrix(runtime.prom, engine.GPU_QUERY, end)
    return value


def mounted_rule_hash(runtime, ident):
    raw = runtime.docker.run(['exec', ident, 'cat', '/etc/prometheus/rules/o04.rules.yml'])
    return hashlib.sha256(raw.encode()).hexdigest()


def assert_bound(runtime, state, record):
    op.require(record['baseline']['binding'] == binding(runtime, state), 'FILESYSTEM_CONFIG_CHANGED_KEEP_BASELINE')
    prior = Path(runtime.state['data_dir']).parent / 'alerts-deployment-o04.json'
    op.require(retained.file_hash(prior) == record['baseline']['prior_acceptance_sha256'],
               'FILESYSTEM_PRIOR_ACCEPTANCE_CHANGED')


def capture(runtime, state):
    prior_hash = prior_guard(runtime, state)
    before = runtime.containers()
    retained.healthy(before)
    op.require(mounted_rule_hash(runtime, before['obs-prometheus']['id']) in (OLD_RULE_HASH, NEW_RULE_HASH),
               'FILESYSTEM_RUNNING_RULE_CHANGED')
    baseline_binding = binding(runtime, state)
    protected = engine.protected(runtime)
    am = manager(runtime, state)
    samples = filesystems(runtime)  # Missing /mnt data stops before any production control.
    tests = native_tests(runtime)
    end = '%.3f' % (time.time() - 15)
    hist = history(runtime, end)
    op.require(before == runtime.containers() and am == manager(runtime, state)
               and protected == engine.protected(runtime) and baseline_binding == binding(runtime, state),
               'FILESYSTEM_CAPTURE_CHANGED')
    core = {'binding': baseline_binding, 'prior_acceptance_sha256': prior_hash,
            'containers': before, 'manager': am, 'protected': protected,
            'query_time': end, 'history': hist, 'filesystems': samples}
    return {'schema': 1, 'kind': KIND, 'baseline': core, 'baseline_sha256': retained.digest(core),
            'tests': tests, 'intent': None, 'command_completed': False}


def read_record(path):
    op.require(op.no_symlinks(path).stat().st_size <= retained.MAX_BASELINE_BYTES, 'FILESYSTEM_RECORD_TOO_LARGE')
    record = op.read_private(path)
    op.require(record.get('schema') == 1 and record.get('kind') == KIND
               and record.get('baseline_sha256') == retained.digest(record['baseline']), 'FILESYSTEM_RECORD_INVALID')
    return record


def replace_once(runtime, state, record, path):
    if record.get('intent'):
        return
    assert_bound(runtime, state, record)
    op.require(runtime.containers() == record['baseline']['containers']
               and manager(runtime, state) == record['baseline']['manager'], 'FILESYSTEM_CHANGED_BEFORE_REPLACE')
    protected = engine.protected(runtime)
    op.require(all(protected.get(k) == v for k, v in record['baseline']['protected'].items()),
               'FILESYSTEM_PROTECTED_CHANGED_BEFORE_REPLACE')
    op.config_check(runtime.docker, runtime.root, runtime.state)
    record['intent'] = {'at': engine.stamp(), 'old_id': record['baseline']['containers']['obs-prometheus']['id']}
    retained.write_record(path, record)
    op.compose(runtime.docker, runtime.root,
               ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate',
                '--wait', '--wait-timeout', '180', 'obs-prometheus'], visible=True, timeout=240)
    record['command_completed'] = True
    retained.write_record(path, record)


def verify(runtime, state, record):
    assert_bound(runtime, state, record)
    op.require(record.get('intent'), 'FILESYSTEM_REPLACE_NOT_REQUESTED')
    current = runtime.containers()
    retained.healthy(current)
    before = record['baseline']['containers']
    prom = current['obs-prometheus']
    op.require(prom['id'] != before['obs-prometheus']['id']
               and op.scrape_timestamp(prom['started_at']) >= op.scrape_timestamp(record['intent']['at']) - 1,
               'FILESYSTEM_REPLACE_NOT_OBSERVED')
    for name in ('obs-node-exporter', 'obs-cadvisor'):
        op.require(current[name] == before[name], 'FILESYSTEM_OTHER_COLLECTOR_CHANGED')
    op.require(manager(runtime, state) == record['baseline']['manager'], 'FILESYSTEM_ALERTMANAGER_CHANGED')
    protected = engine.protected(runtime)
    op.require(all(protected.get(k) == v for k, v in record['baseline']['protected'].items()),
               'FILESYSTEM_PROTECTED_CONTAINER_CHANGED')
    op.require(mounted_rule_hash(runtime, prom['id']) == NEW_RULE_HASH, 'FILESYSTEM_MOUNTED_RULE_MISMATCH')
    hist = history(runtime, record['baseline']['query_time'])
    op.require(hist == record['baseline']['history'], 'FILESYSTEM_FIXED_HISTORY_CHANGED')
    samples = filesystems(runtime)
    old_labels = {retained.encoded(r['labels']) for r in record['baseline']['filesystems']}
    op.require(old_labels <= {retained.encoded(r['labels']) for r in samples}, 'FILESYSTEM_EXISTING_SAMPLE_MISSING')
    groups = runtime.prom.get('/api/v1/rules?type=alert')['groups']
    groups = [g for g in groups if g.get('name') == engine.SCOPE]
    op.require(len(groups) == 1, 'FILESYSTEM_RULE_GROUP_MISSING')
    rules = groups[0]['rules']
    op.require(len(rules) == 8 and {r['name'] for r in rules} == set(engine.RULES), 'FILESYSTEM_RULE_SET_CHANGED')
    now = time.time()
    for rule in rules:
        evaluated = op.scrape_timestamp(rule['lastEvaluation'])
        op.require(rule.get('health') == 'ok' and not rule.get('lastError')
                   and 0 <= now - evaluated < 90 and evaluated >= op.scrape_timestamp(prom['started_at']),
                   'FILESYSTEM_RULE_NOT_FRESH')
    fs = next(r for r in rules if r['name'] == 'OpsFilesystemHigh')
    query = ''.join(fs['query'].split())
    op.require('mountpoint=~"/|/media/.*|/mnt/nvme1"' in query and fs['duration'] == 900,
               'FILESYSTEM_LIVE_RULE_NOT_UPDATED')
    targets = runtime.prom.query('up{job=~"prometheus|host|containers|gpu|alertmanager"}')
    values = op.values(targets)
    op.require(len(values) == 5 and {lab['job'] for lab, _ in values} ==
               {'prometheus', 'host', 'containers', 'gpu', 'alertmanager'}
               and all(value == 1 for _, value in values), 'FILESYSTEM_TARGET_NOT_UP')
    ages = op.values(runtime.prom.query('time() - timestamp(up{job=~"prometheus|host|containers|gpu|alertmanager"})'))
    op.require(len(ages) == 5 and all(0 <= age < 90 for _, age in ages), 'FILESYSTEM_TARGET_STALE')
    op.require(current == runtime.containers(), 'FILESYSTEM_CHANGED_DURING_VERIFY')
    assert_bound(runtime, state, record)
    return {'schema': 1, 'stage': KIND, 'result': 'passed', 'checked_at': engine.stamp(),
            'same_prometheus_image_and_tsdb': True, 'mounted_rule_hash_verified': True,
            'historical_samples_exactly_preserved': retained.counts(hist),
            'protected_containers_unchanged': len(record['baseline']['protected']),
            'protected_scope': 'engine_update_before_subsequent_ops_rebuild', 'alertmanager_unchanged': True,
            'filesystems': samples, 'rule_state': fs['state'], 'rule_health': fs['health'],
            'hold_seconds': 900, 'target_jobs': 5, 'tests': record['tests'],
            'prometheus_command_completion_recorded': record['command_completed'],
            'native_firing_after_15_minutes': 'not_required_for_coverage_acceptance',
            'old_acceptance_record_preserved': True, 'data_cleanup_requested': False,
            'external_notifications_requested': False, 'business_api_requests': 0, 'inference_requested': False,
            'ops_readback': 'separate_check_filesystems.py', 'browser_acceptance': 'deferred_by_user'}


def run(runtime, action, wait_seconds=150):
    state = op.read_private(runtime.root / 'alerts/.state.json')
    op.require(state.get('enabled') is True and state.get('config_id') == runtime.state['config_id'],
               'FILESYSTEM_O04_NOT_ENABLED')
    engine.config_guard(runtime, state)
    path = Path(runtime.state['data_dir']).parent / 'filesystem-update-o04.json'
    if path.exists() or path.is_symlink():
        record = read_record(path)
        assert_bound(runtime, state, record)
        if record.get('report'):
            op.require(runtime.containers() == record['accepted_containers']
                       and manager(runtime, state) == record['baseline']['manager'], 'FILESYSTEM_ACCEPTED_RUNTIME_CHANGED')
            return dict(record['report'], evidence_reused=True)
    else:
        op.require(action == 'apply', 'FILESYSTEM_NO_UPDATE_RECORD')
        record = capture(runtime, state)
        retained.write_record(path, record)
    if action == 'apply':
        replace_once(runtime, state, record, path)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            report = verify(runtime, state, record)
            break
        except (ValueError, OSError) as exc:
            if time.monotonic() >= deadline:
                raise ValueError('FILESYSTEM_VERIFY_INCOMPLETE: ' + str(exc)) from exc
            time.sleep(2)
    record['report'] = report
    record['accepted_containers'] = runtime.containers()
    retained.write_record(path, record)
    return dict(report, evidence_reused=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('apply', 'check'))
    args = parser.parse_args()
    with op.lock(op.ROOT):
        print(json.dumps(run(retained.Runtime(op.Docker()), args.action), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'result': 'error', 'stage': KIND,
              'reason': str(exc).replace('. 기존 서비스는 변경하지 않습니다.', '.'),
              'instruction': '기준 파일을 유지하고 filesystem_update.py check로 확인하세요. 제어 명령은 자동 반복하지 않습니다.'},
              ensure_ascii=False))
        raise SystemExit(1)
