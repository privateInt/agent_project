#!/usr/bin/env python3
"""Change a prepared Ops HTTP LAN address, preserving keys/cache and restarting only Ops once."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.ops import manage, restart_check as rc
from src.ops.platform.transport import base_url, strict_json

KIND = 'O03-ops-public-url-change'
CONFIGS = ('host-config.json', 'config.json')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def body(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def target_url(value, cfg):
    url = base_url(value)
    try:
        address = ipaddress.IPv4Address(url.hostname)
    except ValueError:
        raise ReadFailure('LAN_IPV4_REQUIRED') from None
    require(url.scheme == 'http' and address.is_private and not address.is_loopback
            and not address.is_unspecified and not address.is_multicast
            and url.port == cfg['port'] and not cfg['tls_cert'] and not cfg['tls_key'], 'SAME_PORT_HTTP_LAN_REQUIRED')
    return 'http://' + str(address) + ':' + str(url.port)


@contextmanager
def change_lock(directory):
    manage.private(directory)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReadFailure('PUBLIC_URL_CHANGE_BUSY') from None
        yield
    finally:
        os.close(fd)


class Docker(rc.Docker):
    def protected(self, exclude):
        ids = self.command(['ps', '-q', '--no-trunc']).split()
        require(all(len(x) == 64 for x in ids), 'INVALID_CONTAINER_ID')
        if not ids: return {}
        fmt = '{"id":{{json .Id}},"started":{{json .State.StartedAt}}}'
        rows = [strict_json(line) for line in self.command(['inspect', '--format', fmt, *ids]).splitlines()]
        return {row['id']: row['started'] for row in rows if row['id'] != exclude}

    def mounted_hash(self, ident):
        return hashlib.sha256(self.command(['exec', ident, 'cat', '/run/ops/config.json']).encode()).hexdigest()


def historical(directory):
    path = directory / 'restart-check/baseline.json'
    return rc.read_file(path, 512*1024, private=True)[1] if path.exists() or path.is_symlink() else None


def cache_guard(cfg, before):
    after = rc.cache(cfg)
    require(all(after[key] == before[key] for key in ('file_mode', 'uid', 'gid')), 'CACHE_METADATA_CHANGED')
    require(all(key in after['success_times'] and rc.date(after['success_times'][key]) >= rc.date(value)
                for key, value in before['success_times'].items()), 'CACHE_ENTRY_LOST_OR_TIME_REVERSED')
    return {'baseline_entries_preserved': len(before['success_times']),
            'current_entries': len(after['success_times']),
            'timestamps_unchanged': after['success_times'] == before['success_times']}


def guard(directory, baseline, allow_partial=False):
    old_cfg = strict_json(baseline['original'][CONFIGS[0]])
    observed = rc.files(directory, old_cfg)
    for name, before in baseline['files'].items():
        expected = dict(before)
        if name in CONFIGS:
            expected['sha256'] = hashlib.sha256(body(baseline['desired'][name]).encode()).hexdigest()
        require(observed[name] == expected or (allow_partial and name in CONFIGS and observed[name] == before),
                'PUBLIC_URL_CONFIG_OR_CREDENTIAL_DRIFT')
    require(historical(directory) == baseline['historical_restart_record'], 'PRIOR_RESTART_RECORD_CHANGED')
    cache_guard(old_cfg, baseline['cache'])


def current(docker, directory, cfg, owner, ident):
    require(docker.discover() == ident, 'OPS_CONTAINER_CHANGED')
    return rc.container(docker.inspect(ident), directory, cfg, owner)


def protect(docker, baseline):
    current_items = docker.protected(baseline['container']['identity']['id'])
    require(all(current_items.get(k) == v for k, v in baseline['protected'].items()), 'OTHER_CONTAINER_CHANGED')


def capture(directory, url, docker, api_factory):
    cfg = manage.existing(directory)
    url = target_url(url, cfg)
    require(urllib.parse.urlsplit(cfg['public_url']).hostname in ('localhost', '127.0.0.1'),
            'EXPECTED_EXISTING_LOOPBACK_URL')
    owner = strict_json(rc.read_file(directory / 'owner.json', private=True)[0])
    original = {name: rc.read_file(directory / name, private=True)[0].decode() for name in CONFIGS}
    desired = {name: dict(strict_json(raw), public_url=url) for name, raw in original.items()}
    before = current(docker, directory, cfg, owner, docker.discover())
    require(before['healthy'], 'OPS_NOT_HEALTHY_BEFORE_CHANGE')
    baseline = {'directory': str(directory), 'target_url': url, 'owner': owner, 'original': original,
                'desired': desired, 'files': rc.files(directory, cfg), 'cache': rc.cache(cfg),
                'container': before, 'protected': docker.protected(before['identity']['id']),
                'historical_restart_record': historical(directory)}
    require(docker.mounted_hash(before['identity']['id']) == baseline['files']['config.json']['sha256'],
            'MOUNTED_CONFIG_DIFFERS')
    api_factory(cfg).health()
    require(current(docker, directory, cfg, owner, before['identity']['id']) == before, 'BASELINE_CHANGED')
    protect(docker, baseline)
    guard(directory, baseline, allow_partial=True)
    return {'schema': 1, 'kind': KIND, 'baseline': baseline, 'baseline_sha256': digest(baseline),
            'restart_intent': None, 'restart_command_completed': False}


def read_record(path, directory, url):
    record = strict_json(rc.read_file(path, 512*1024, private=True)[0])
    baseline = record['baseline']
    require(record.get('schema') == 1 and record.get('kind') == KIND
            and record.get('baseline_sha256') == digest(baseline) and baseline['directory'] == str(directory),
            'PUBLIC_URL_RECORD_INVALID')
    require(target_url(url, baseline['desired']['host-config.json']) == baseline['target_url'], 'PUBLIC_URL_TARGET_DIFFERS')
    return record


def apply_once(directory, record, path, docker):
    baseline = record['baseline']
    guard(directory, baseline, allow_partial=not record['restart_intent'])
    if record['restart_intent']: return
    cfg = strict_json(baseline['original']['host-config.json'])
    ident = baseline['container']['identity']['id']
    require(current(docker, directory, cfg, baseline['owner'], ident) == baseline['container'], 'OPS_CHANGED_BEFORE_UPDATE')
    protect(docker, baseline)
    # Original bytes are durably recorded before either atomic replacement. A partial pair can resume.
    for name in CONFIGS:
        expected = body(baseline['desired'][name])
        if rc.read_file(directory / name, private=True)[0] != expected.encode():
            rc.write_record(directory / name, baseline['desired'][name])
    manage.existing(directory)
    guard(directory, baseline)
    protect(docker, baseline)
    require(current(docker, directory, cfg, baseline['owner'], ident) == baseline['container'], 'OPS_CHANGED_BEFORE_RESTART')
    record['restart_intent'] = {'at': rc.now(), 'id': ident}
    rc.write_record(path, record)
    docker.restart(ident)
    record['restart_command_completed'] = True
    rc.write_record(path, record)


def verify(directory, record, docker, api_factory):
    baseline = record['baseline']
    guard(directory, baseline)
    require(record['restart_intent'], 'PUBLIC_URL_RESTART_NOT_REQUESTED')
    cfg = manage.existing(directory)
    ident = baseline['container']['identity']['id']
    observed = current(docker, directory, cfg, baseline['owner'], ident)
    require(observed['identity'] == baseline['container']['identity'], 'OPS_IDENTITY_CHANGED')
    require(observed['healthy'] and rc.date(observed['started']) > rc.date(baseline['container']['started'])
            and rc.date(observed['started']) >= rc.date(record['restart_intent']['at']), 'OPS_UPDATE_NOT_READY')
    protect(docker, baseline)
    require(docker.mounted_hash(ident) == rc.files(directory, cfg)['config.json']['sha256'], 'MOUNTED_URL_NOT_UPDATED')
    api = api_factory(cfg)
    api.health()
    cookie = api.login()
    api.logout(cookie)
    forbidden = api_factory(dict(cfg, public_url='http://unconfigured.invalid:' + str(cfg['port'])))
    status, result, _ = forbidden.request('GET', '/healthz')
    require(status == 400 and result.get('error', {}).get('code') == 'HOST_NOT_ALLOWED', 'HOST_BOUNDARY_NOT_ENFORCED')
    guard(directory, baseline)
    cache = cache_guard(cfg, baseline['cache'])
    protect(docker, baseline)
    require(current(docker, directory, cfg, baseline['owner'], ident) == observed, 'OPS_CHANGED_DURING_VERIFY')
    return {'stage': KIND, 'result': 'passed', 'checked_at': rc.now(), 'public_url': cfg['public_url'],
            'same_ops_container_image_mounts': True, 'container_id': ident, 'started_after': observed['started'],
            'configuration_change': 'public_url_only_in_host_and_container_config',
            'credentials_preserved': True, 'cache': cache, 'same_access_code_login_and_logout': True,
            'unconfigured_host_rejected': True, 'prior_restart_record_preserved': True,
            'protected_containers_unchanged': len(baseline['protected']),
            'restart_command_completion_recorded': record['restart_command_completed'],
            'build_requested': False, 'new_container_requested': False, 'upstream_api_requests': 0,
            'inference_requested': False, 'browser_acceptance': 'ready_for_user_check'}


def run(action, directory, url, docker=None, api_factory=rc.API, wait_seconds=60):
    directory = manage.safe(directory)
    with change_lock(directory):
        folder = directory / 'public-url-change'
        path = folder / 'baseline.json'
        if folder.exists() or folder.is_symlink(): manage.private(folder)
        docker = docker or Docker(project=manage.prepared_project(directory))
        if path.exists() or path.is_symlink():
            record = read_record(path, directory, url)
        else:
            require(action == 'apply', 'NO_PUBLIC_URL_CHANGE_RECORD')
            record = capture(directory, url, docker, api_factory)
            folder.mkdir(mode=0o700, exist_ok=True)
            rc.write_record(path, record)
        if record.get('report'):
            baseline = record['baseline']
            guard(directory, baseline)
            cfg = manage.existing(directory)
            observed = current(docker, directory, cfg, baseline['owner'], baseline['container']['identity']['id'])
            require(observed == record['accepted_container'], 'ACCEPTED_OPS_CONTAINER_CHANGED')
            return dict(record['report'], evidence_reused=True)
        if action == 'apply': apply_once(directory, record, path, docker)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                report = verify(directory, record, docker, api_factory)
                break
            except (ReadFailure, OSError):
                if time.monotonic() >= deadline: raise
                time.sleep(.5)
        record['report'] = report
        baseline = record['baseline']
        record['accepted_container'] = current(docker, directory, manage.existing(directory), baseline['owner'],
                                               baseline['container']['identity']['id'])
        require(record['accepted_container']['started'] == report['started_after'], 'OPS_CHANGED_AFTER_VERIFY')
        rc.write_record(path, record)
        return dict(report, evidence_reused=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('apply', 'check'))
    p.add_argument('--public-url', required=True)
    p.add_argument('--state-dir', type=Path, default=manage.STATE)
    args = p.parse_args()
    try:
        report = run(args.action, args.state_dir, args.public_url)
    except (ReadFailure, OSError, ValueError, KeyError, TypeError) as exc:
        report = {'stage': KIND, 'result': 'attention_required',
                  'code': exc.code if isinstance(exc, ReadFailure) else 'LOCAL_CONFIG_OR_COMMAND_ERROR',
                  'guidance': 'Keep public-url-change/baseline.json; check is read-only. Do not reset keys or old acceptance.'}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['result'] == 'passed' else 2


if __name__ == '__main__': raise SystemExit(main())
