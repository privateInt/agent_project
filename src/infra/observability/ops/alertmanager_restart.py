#!/usr/bin/env python3
"""Restart the accepted production Alertmanager once; retain evidence and use GET-only readback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.request

import alerts as engine
import filesystem_update as filesystem
import manage as op
import restart_check as retained

KIND = 'O04-alertmanager-restart'
PATHS = ('/-/ready', '/api/v2/status', '/api/v2/silences',
         '/api/v2/alerts?active=true&silenced=true&inhibited=true&unprocessed=true')
SNAPSHOTS = ('silences', 'nflog')
MAX_BYTES = 4 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('AM_RESTART_REDIRECT_REFUSED')


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, path):
        op.require(path in PATHS, 'AM_RESTART_HTTP_PATH_REFUSED')
        with self.opener.open('http://127.0.0.1:16330' + path, timeout=5) as response:
            data = response.read(MAX_BYTES + 1)
        op.require(len(data) <= MAX_BYTES, 'AM_RESTART_RESPONSE_TOO_LARGE')
        return True if path == '/-/ready' else json.loads(data)


def state_guard(runtime):
    state = op.read_private(runtime.root / 'alerts/.state.json')
    op.require(state.get('schema') == 1 and state.get('enabled') is True
               and state.get('config_id') == runtime.state['config_id']
               and state.get('image', {}).get('tag') == engine.TAG, 'AM_RESTART_STATE_MISMATCH')
    data = op.no_symlinks(Path(state['data_dir']))
    info = data.stat()
    op.require(data == Path(runtime.state['data_dir']).parent / 'alertmanager'
               and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
               and op.read_private(data / '.owner.json') == {'config_id': state['config_id']},
               'AM_RESTART_STORAGE_OWNER_MISMATCH')
    engine.config_guard(runtime, state)
    return state


def manager(runtime, state):
    items = engine.manager_containers(runtime.docker, state)
    op.require(len(items) == 1, 'AM_RESTART_CONTAINER_MISSING')
    item = items[0]
    op.require(re.fullmatch('[0-9a-f]{64}', item['Id']), 'AM_RESTART_INVALID_ID')
    running = item['State']
    op.require(running['Running'] and not running.get('Restarting') and not running.get('OOMKilled')
               and running.get('Health', {}).get('Status') == 'healthy', 'AM_RESTART_NOT_HEALTHY')
    config, host = item['Config'], item['HostConfig']
    op.require(host.get('NetworkMode') == 'host' and host.get('ReadonlyRootfs') is True
               and config.get('User') == str(runtime.state['uid']) + ':' + str(runtime.state['gid'])
               and config.get('Cmd') == ['--config.file=/etc/alertmanager/alertmanager.yml',
                   '--web.listen-address=127.0.0.1:16330', '--cluster.listen-address=',
                   '--storage.path=/alertmanager', '--data.retention=120h'], 'AM_RESTART_RUNTIME_CONFIG_CHANGED')
    mounts = sorted([{'source': m['Source'], 'destination': m['Destination'], 'type': m['Type'], 'rw': m['RW']}
                     for m in item['Mounts'] if m['Destination'] != '/tmp'], key=lambda m: m['destination'])
    expected = sorted([
        {'source': state['data_dir'], 'destination': '/alertmanager', 'type': 'bind', 'rw': True},
        {'source': str(runtime.root / 'alerts/alertmanager.yml'),
         'destination': '/etc/alertmanager/alertmanager.yml', 'type': 'bind', 'rw': False}],
        key=lambda m: m['destination'])
    op.require(mounts == expected, 'AM_RESTART_RUNTIME_MOUNTS_CHANGED')
    mounted = runtime.docker.run(['exec', item['Id'], 'cat', '/etc/alertmanager/alertmanager.yml'])
    op.require(hashlib.sha256(mounted.encode()).hexdigest() == retained.file_hash(runtime.root / 'alerts/alertmanager.yml'),
               'AM_RESTART_MOUNTED_CONFIG_CHANGED')
    return {'id': item['Id'], 'image_id': item['Image'], 'started_at': running['StartedAt'],
            'configuration_sha256': retained.digest({'config': config, 'host': host, 'mounts': mounts})}


def protected(runtime, manager_id):
    ids = runtime.docker.run(['ps', '-q']).split()
    return {item['Id']: item['State']['StartedAt']
            for item in (runtime.docker.json(['inspect', *ids]) if ids else []) if item['Id'] != manager_id}


def prior_path(runtime):
    return Path(runtime.state['data_dir']).parent / 'filesystem-update-o04.json'


def binding(runtime, state):
    value = filesystem.binding(runtime, state)
    value['files']['ops/alertmanager_restart.py'] = retained.file_hash(runtime.root / 'ops/alertmanager_restart.py')
    data = Path(state['data_dir'])
    info = data.stat()
    value['alertmanager_storage'] = {'path': str(data), 'device': info.st_dev, 'inode': info.st_ino,
                                    'owner_sha256': retained.file_hash(data / '.owner.json')}
    value['prior_acceptance_sha256'] = retained.file_hash(prior_path(runtime))
    # The filesystem record binds the original O04 record. Keep both original bytes.
    value['original_acceptance_sha256'] = retained.file_hash(data.parent / 'alerts-deployment-o04.json')
    return value


def snapshots(state):
    result = {}
    for name in SNAPSHOTS:
        path = op.no_symlinks(Path(state['data_dir']) / name)
        if not path.exists():
            result[name] = {'exists': False}
            continue
        info = path.stat()
        op.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                   and info.st_size <= MAX_BYTES, 'AM_RESTART_SNAPSHOT_UNSAFE')
        # These are observations, not a raw-byte equality contract: maintenance may rewrite snapshots.
        body = path.read_bytes()
        op.require(len(body) <= MAX_BYTES, 'AM_RESTART_SNAPSHOT_TOO_LARGE')
        result[name] = {'exists': True, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
                        'mtime_ns': info.st_mtime_ns}
    return result


def silence_state(client):
    items = client.get('/api/v2/silences')
    op.require(isinstance(items, list) and len(items) <= 1000, 'AM_RESTART_SILENCE_RESPONSE_INVALID')
    result = {}
    for row in items:
        op.require(isinstance(row, dict) and isinstance(row.get('id'), str)
                   and row['id'] not in result and row.get('status', {}).get('state') in ('active', 'pending', 'expired'),
                   'AM_RESTART_SILENCE_RESPONSE_INVALID')
        end = op.scrape_timestamp(row['endsAt'])
        op.scrape_timestamp(row['startsAt'])
        # Exclude only derived status, retain all persistent fields without exposing comments/matchers.
        value = {k: v for k, v in row.items() if k != 'status'}
        result[row['id']] = {'sha256': retained.digest(value), 'ends_at': end,
                             'state': row['status']['state']}
    return result


def compare_silences(before, after, now):
    required = {key: value for key, value in before.items() if value['ends_at'] > now}
    op.require(all(key in after and after[key]['sha256'] == value['sha256'] for key, value in required.items()),
               'AM_RESTART_UNEXPIRED_SILENCE_CHANGED')
    return {'before': len(before), 'after': len(after), 'unexpired_preserved': len(required),
            'expired_outside_comparison': len(before) - len(required),
            'nonempty_unexpired_preservation_verified': bool(required)}


def status(client):
    client.get('/-/ready')
    data = client.get('/api/v2/status')
    op.require(data.get('versionInfo', {}).get('version') == engine.TAG.rsplit('v', 1)[1]
               and isinstance(data.get('config', {}).get('original'), str) and data['config']['original'],
               'AM_RESTART_STATUS_INVALID')
    return {'config_sha256': retained.digest(data['config']['original']),
            'version': data['versionInfo']['version']}


def alert_readback(runtime, client, started_at=None):
    groups = [g for g in runtime.prom.get('/api/v1/rules?type=alert')['groups'] if g.get('name') == engine.SCOPE]
    op.require(len(groups) == 1 and len(groups[0]['rules']) == len(engine.RULES)
               and {r['name'] for r in groups[0]['rules']} == set(engine.RULES), 'AM_RESTART_RULE_SET_CHANGED')
    firing = set()
    now = time.time()
    for rule in groups[0]['rules']:
        evaluated = op.scrape_timestamp(rule['lastEvaluation'])
        op.require(rule.get('health') == 'ok' and not rule.get('lastError') and 0 <= now - evaluated < 90
                   and (started_at is None or evaluated >= op.scrape_timestamp(started_at)), 'AM_RESTART_RULES_NOT_FRESH')
        for alert in rule.get('alerts', []):
            if alert.get('state') == 'firing':
                labels = alert['labels']
                op.require(labels.get('scope') == engine.SCOPE, 'AM_RESTART_ALERT_SCOPE_INVALID')
                firing.add(retained.encoded(labels))
    items = client.get(PATHS[3])
    op.require(isinstance(items, list) and len(items) <= 1000, 'AM_RESTART_ALERT_RESPONSE_INVALID')
    received = set()
    for item in items:
        op.require(isinstance(item.get('labels'), dict) and item.get('status', {}).get('state') in
                   ('active', 'suppressed', 'unprocessed'), 'AM_RESTART_ALERT_RESPONSE_INVALID')
        if item['labels'].get('scope') == engine.SCOPE and op.scrape_timestamp(item['endsAt']) > now:
            received.add(retained.encoded(item['labels']))
    op.require(firing <= received, 'AM_RESTART_FIRING_ALERT_NOT_RECEIVED')
    values = op.values(runtime.prom.query('up{job="alertmanager"}'))
    scraped = op.values(runtime.prom.query('timestamp(up{job="alertmanager"})'))
    op.require(len(values) == len(scraped) == 1 and values[0][1] == 1
               and 0 <= time.time() - scraped[0][1] < 90
               and (started_at is None or scraped[0][1] >= op.scrape_timestamp(started_at)),
               'AM_RESTART_SCRAPE_NOT_READY')
    # Label hashes provide bounded private comparisons, no raw alert content in the evidence.
    return {'firing': sorted(retained.digest(json.loads(v)) for v in firing),
            'received_count': len(received), 'rule_count': len(engine.RULES)}


def assert_bound(runtime, state, record):
    op.require(record['baseline']['binding'] == binding(runtime, state), 'AM_RESTART_CONFIG_CHANGED_KEEP_BASELINE')


def stable(runtime, state, baseline, expected_manager):
    op.require(manager(runtime, state) == expected_manager, 'AM_RESTART_CONTAINER_CHANGED')
    op.require(runtime.containers() == baseline['collectors'], 'AM_RESTART_COLLECTORS_CHANGED')
    current = protected(runtime, expected_manager['id'])
    op.require(all(current.get(k) == v for k, v in baseline['protected'].items()), 'AM_RESTART_OTHER_CONTAINER_CHANGED')


def capture(runtime, state, client):
    prior = filesystem.read_record(prior_path(runtime))
    filesystem.assert_bound(runtime, state, prior)
    op.require(prior.get('report', {}).get('result') == 'passed'
               and runtime.containers() == prior['accepted_containers'], 'AM_RESTART_FILESYSTEM_ACCEPTANCE_REQUIRED')
    am = manager(runtime, state)
    op.require({k: am[k] for k in ('id', 'image_id', 'started_at')} == prior['baseline']['manager'],
               'AM_RESTART_PRIOR_MANAGER_CHANGED')
    core = {'binding': binding(runtime, state), 'manager': am, 'collectors': runtime.containers(),
            'protected': protected(runtime, am['id']), 'status': status(client),
            'silences': silence_state(client), 'snapshots': snapshots(state),
            'alerts': alert_readback(runtime, client)}
    retained.healthy(core['collectors'])
    stable(runtime, state, core, am)
    record = {'schema': 1, 'kind': KIND, 'baseline': core, 'baseline_sha256': retained.digest(core),
              'intent': None, 'command_completed': False}
    assert_bound(runtime, state, record)
    return record


def read_record(path):
    op.require(op.no_symlinks(path).stat().st_size <= MAX_BYTES, 'AM_RESTART_RECORD_TOO_LARGE')
    record = op.read_private(path)
    op.require(record.get('schema') == 1 and record.get('kind') == KIND
               and record.get('baseline_sha256') == retained.digest(record['baseline']), 'AM_RESTART_RECORD_INVALID')
    return record


def restart_once(runtime, state, client, record, path):
    if record.get('intent'):
        return  # A lost response is reconciled with GET/inspect, never another restart.
    assert_bound(runtime, state, record)
    baseline = record['baseline']
    stable(runtime, state, baseline, baseline['manager'])
    op.require(status(client) == baseline['status'] and silence_state(client) == baseline['silences'],
               'AM_RESTART_STATE_CHANGED_BEFORE_RESTART')
    record['intent'] = {'at': engine.stamp(), 'id': baseline['manager']['id']}
    retained.write_record(path, record)  # fsync file and parent before controlling this exact ID.
    runtime.docker.run(['restart', '--time', '30', baseline['manager']['id']], timeout=90)
    record['command_completed'] = True
    retained.write_record(path, record)


def verify(runtime, state, client, record):
    assert_bound(runtime, state, record)
    op.require(record.get('intent'), 'AM_RESTART_NOT_REQUESTED')
    baseline = record['baseline']
    am = manager(runtime, state)
    before = baseline['manager']
    op.require(all(am[k] == before[k] for k in ('id', 'image_id', 'configuration_sha256')), 'AM_RESTART_ID_IMAGE_CONFIG_CHANGED')
    op.require(am['started_at'] != before['started_at']
               and op.scrape_timestamp(am['started_at']) >= op.scrape_timestamp(record['intent']['at']) - 1,
               'AM_RESTART_NOT_OBSERVED')
    stable(runtime, state, baseline, am)
    op.require(status(client) == baseline['status'], 'AM_RESTART_LOADED_CONFIG_CHANGED')
    silence_report = compare_silences(baseline['silences'], silence_state(client), time.time())
    observed = snapshots(state)
    for name in SNAPSHOTS:
        op.require(observed[name]['exists'] and observed[name]['mtime_ns'] / 1e9 >=
                   op.scrape_timestamp(record['intent']['at']) - 1, 'AM_RESTART_SNAPSHOT_WRITE_NOT_OBSERVED')
    # A nonempty log becoming empty requires review; never silently accept possible loss.
    op.require(not baseline['snapshots']['nflog'].get('bytes') or observed['nflog']['bytes'] > 0,
               'AM_RESTART_NONEMPTY_NFLOG_BECAME_EMPTY_REVIEW_REQUIRED')
    alerts = alert_readback(runtime, client, am['started_at'])
    stable(runtime, state, baseline, am)
    assert_bound(runtime, state, record)
    snapshot_report = {name: {'before_exists': baseline['snapshots'][name]['exists'],
        'after_bytes': observed[name]['bytes'], 'write_observed_after_intent': True,
        'bytes_equal_to_pre_restart': baseline['snapshots'][name].get('sha256') == observed[name]['sha256']}
        for name in SNAPSHOTS}
    return {'schema': 1, 'stage': KIND, 'result': 'passed', 'checked_at': engine.stamp(),
            'scope': 'same_container_configuration_storage_binding_snapshot_files_silence_readback',
            'container_id': am['id'], 'image_id': am['image_id'],
            'started_before': before['started_at'], 'started_after': am['started_at'],
            'same_container_image_mounts': True, 'configuration_preserved': True,
            'same_storage_directory_and_owner': True, 'snapshots': snapshot_report, 'silences': silence_report,
            'notification_log_semantic_preservation': 'not_verified_no_production_notification_probe',
            'alerts': {'rules_healthy': alerts['rule_count'], 'current_firing_received': len(alerts['firing']),
                       'manager_active_count': alerts['received_count'],
                       'baseline_firing_no_longer_firing': len(set(baseline['alerts']['firing']) - set(alerts['firing'])),
                       'delivery_sample_verified': bool(alerts['firing']),
                       'scope': 'current_prometheus_firing_readback_not_disk_restore'},
            'protected_running_containers_unchanged': len(baseline['protected']),
            'original_acceptance_records_preserved': True,
            'restart_command_completion_recorded': record['command_completed'],
            'synthetic_alert_or_silence_writes': 0, 'new_container_requested': False,
            'other_service_control_requested': False, 'business_api_requests': 0, 'inference_requested': False,
            'external_delivery': 'unconfigured', 'backup_restore_acceptance': False,
            'browser_acceptance': 'deferred_by_user'}


def run(runtime, action, wait_seconds=180, client=None):
    state = state_guard(runtime)
    client = client or Client()
    path = Path(runtime.state['data_dir']).parent / 'alertmanager-restart-o04.json'
    if path.exists() or path.is_symlink():
        record = read_record(path)
        assert_bound(runtime, state, record)
        if record.get('report'):
            stable(runtime, state, record['baseline'], record['accepted_manager'])
            return dict(record['report'], evidence_reused=True)
    else:
        op.require(action == 'run', 'AM_RESTART_NO_RECORD')
        record = capture(runtime, state, client)
        retained.write_record(path, record)
    if action == 'run':
        restart_once(runtime, state, client, record, path)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            report = verify(runtime, state, client, record)
            break
        except (ValueError, OSError) as exc:
            if time.monotonic() >= deadline:
                raise ValueError('AM_RESTART_VERIFY_INCOMPLETE: ' + str(exc)) from exc
            time.sleep(2)
    accepted = manager(runtime, state)
    op.require(accepted['started_at'] == report['started_after'], 'AM_RESTART_CHANGED_AFTER_VERIFY')
    record.update(report=report, accepted_manager=accepted)
    retained.write_record(path, record)
    return dict(report, evidence_reused=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'check'))
    args = parser.parse_args()
    with op.lock(op.ROOT):
        print(json.dumps(run(retained.Runtime(op.Docker()), args.action), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'result': 'error', 'stage': KIND, 'code': str(exc) if isinstance(exc, ValueError)
                          else type(exc).__name__, 'guidance': 'Keep the original acceptance record; check is read-only. Do not delete/reset intent.'}), file=sys.stderr)
        sys.exit(1)
