#!/usr/bin/env python3
"""Restart only the prepared Ops container once; retain private evidence for resume."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ops_platform.contracts import ReadFailure, require, timestamp
from ops_platform.ops import manage
from ops_platform.transport import secret, strict_json

PROJECT = 'agent-project-ops'
SERVICE = 'ops-web'
COOKIE = 'ops_session'
INSPECT = '''{"id":{{json .Id}},"image":{{json .Image}},"created":{{json .Created}},
"started":{{json .State.StartedAt}},"running":{{json .State.Running}},
"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},
"project":{{json (index .Config.Labels "com.docker.compose.project")}},
"service":{{json (index .Config.Labels "com.docker.compose.service")}},
"owner":{{json (index .Config.Labels "org.agent-project.ops.owner")}},
"user":{{json .Config.User}},"network":{{json .HostConfig.NetworkMode}},
"readonly":{{json .HostConfig.ReadonlyRootfs}},"mounts":{{json .Mounts}}}'''


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def date(value):
    require(timestamp(value), 'INVALID_TIMESTAMP')
    normalized = re.sub(r'\.(\d+)', lambda m: '.'+(m.group(1)+'000000')[:6], value)
    return datetime.fromisoformat(normalized.replace('Z', '+00:00'))


def read_file(path, limit=65536, private=False):
    path = manage.safe(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'INVALID_LOCAL_FILE')
    if private:
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'PRIVATE_STATE_REQUIRED')
    raw = path.read_bytes()
    require(len(raw) <= limit, 'INVALID_LOCAL_FILE')
    return raw, {'sha256': hashlib.sha256(raw).hexdigest(), 'mode': stat.S_IMODE(info.st_mode),
                 'uid': info.st_uid, 'gid': info.st_gid}


def files(directory, cfg):
    paths = {name: directory/name for name in
             ('host-config.json', 'config.json', 'compose.env', 'owner.json', 'access-key')}
    paths.update(agent_token=Path(cfg['platforms']['agent']['token_file']),
                 langfuse_credentials=Path(cfg['langfuse_file']))
    return {name: read_file(path, private=name not in ('agent_token', 'langfuse_credentials'))[1]
            for name, path in paths.items()}


def cache(cfg):
    path = manage.safe(cfg['state_file'])
    manage.private(path.parent)
    lock = manage.private(Path(str(path)+'.lock'))
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReadFailure('CACHE_BUSY') from None
        raw, metadata = read_file(path, 512*1024, private=True)
        data = strict_json(raw)
        require(isinstance(data, dict) and set(data) == {'schema', 'success_times'}
                and data['schema'] == 1 and isinstance(data['success_times'], dict), 'INVALID_CACHE')
        times = data['success_times']
        require(0 < len(times) <= 1000, 'EMPTY_OR_OVERSIZE_CACHE')
        require(all(re.fullmatch('[0-9a-f]{64}', k) and timestamp(v) for k, v in times.items()), 'INVALID_CACHE')
        return {'success_times': times, 'file_mode': metadata['mode'],
                'uid': metadata['uid'], 'gid': metadata['gid']}
    finally:
        os.close(fd)


def write_record(path, value):
    require(not path.is_symlink(), 'SYMLINK_REFUSED')
    fd, temp = tempfile.mkstemp(prefix='.restart-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, indent=2); out.write('\n')
            out.flush(); os.fsync(out.fileno())
        os.replace(temp, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temp): os.unlink(temp)


@contextmanager
def evidence_lock(directory, create):
    folder = manage.safe(directory/'restart-check')
    if create: folder.mkdir(mode=0o700, exist_ok=True)
    require(folder.is_dir(), 'NO_RESTART_BASELINE')
    manage.private(folder)
    lock = folder/'lock'
    require(not lock.is_symlink(), 'SYMLINK_REFUSED')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077, 'PRIVATE_STATE_REQUIRED')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReadFailure('RESTART_CHECK_BUSY') from None
        yield folder/'baseline.json'
    finally:
        os.close(fd)


class Docker:
    def __init__(self, project=PROJECT):
        self.project = manage.project_name(project)
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(('COMPOSE_', 'OPS_'))}
        require(not self.env.get('DOCKER_HOST') or self.env['DOCKER_HOST'].startswith('unix://'), 'LOCAL_DOCKER_REQUIRED')
        endpoint = self.command(['context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).strip()
        require(endpoint.startswith('unix://'), 'LOCAL_DOCKER_REQUIRED')

    def command(self, args, timeout=5):
        try:
            result = subprocess.run(['docker', *args], env=self.env, text=True,
                                    capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ReadFailure('DOCKER_TIMEOUT') from None
        require(result.returncode == 0, 'DOCKER_COMMAND_FAILED')
        require(len(result.stdout) <= 128*1024, 'DOCKER_RESPONSE_TOO_LARGE')
        return result.stdout

    def discover(self):
        ids = self.command(['ps', '-a', '--no-trunc', '--filter', 'label=com.docker.compose.project='+getattr(self,'project',PROJECT),
                            '--filter', 'label=com.docker.compose.service='+SERVICE, '--format', '{{.ID}}']).split()
        require(len(ids) == 1 and re.fullmatch('[a-f0-9]{64}', ids[0]), 'EXPECTED_ONE_OPS_CONTAINER')
        return ids[0]

    def inspect(self, ident):
        require(re.fullmatch('[a-f0-9]{64}', ident), 'INVALID_CONTAINER_ID')
        return strict_json(self.command(['inspect', '--type', 'container', '--format', INSPECT, ident]))

    def restart(self, ident):
        require(re.fullmatch('[a-f0-9]{64}', ident), 'INVALID_CONTAINER_ID')
        self.command(['container', 'restart', '-t', '20', ident], timeout=45)


def container(raw, directory, cfg, owner):
    require(isinstance(raw, dict) and re.fullmatch('[a-f0-9]{64}', raw.get('id', '')), 'INVALID_CONTAINER')
    require(raw.get('project') == manage.project_name(owner.get('compose_project',PROJECT)) and raw.get('service') == SERVICE
            and raw.get('owner') == owner['owner'], 'CONTAINER_OWNER_MISMATCH')
    require(raw.get('user') == str(owner['uid'])+':'+str(owner['gid'])
            and raw.get('network') == 'host' and raw.get('readonly') is True, 'CONTAINER_CONFIG_MISMATCH')
    expected = {
        '/run/ops/config.json': (str(directory/'config.json'), False),
        '/run/ops/access-key': (str(directory/'access-key'), False),
        '/run/ops/agent-token': (cfg['platforms']['agent']['token_file'], False),
        '/run/ops/langfuse.json': (cfg['langfuse_file'], False),
        '/var/lib/ops': (str(directory/'cache'), True),
    }
    mounts = raw.get('mounts'); require(isinstance(mounts, list), 'INVALID_MOUNTS')
    actual = {}
    for mount in mounts:
        if mount.get('Type') == 'tmpfs' and mount.get('Destination') == '/tmp': continue
        require(mount.get('Type') == 'bind' and mount.get('Destination') not in actual, 'UNEXPECTED_MOUNT')
        require(type(mount.get('RW')) is bool, 'INVALID_MOUNTS')
        actual[mount['Destination']] = (mount.get('Source'), mount['RW'])
    require(actual == expected, 'MOUNT_MISMATCH')
    require(isinstance(raw.get('image'), str) and re.fullmatch('sha256:[a-f0-9]{64}', raw['image']), 'INVALID_IMAGE')
    date(raw.get('created')); date(raw.get('started'))
    identity = {key:raw[key] for key in ('id','image','created','project','service','owner','user','network','readonly')}
    identity['mounts'] = {k:list(v) for k,v in sorted(actual.items())}
    return {'identity':identity, 'started':raw['started'], 'healthy':raw.get('running') is True and raw.get('health') == 'healthy'}


class API:
    def __init__(self, cfg):
        self.cfg = cfg
        require(urllib.parse.urlsplit(cfg['public_url']).scheme == 'http'
                and not cfg['tls_cert'] and not cfg['tls_key'], 'RESTART_CHECK_HTTP_LOOPBACK_ONLY')

    def request(self, method, path, cookie=None, body=None):
        require((method == 'GET' and path in ('/healthz','/api/session','/api/catalog'))
                or (method in ('POST','DELETE') and path == '/api/session'), 'RESTART_CHECK_PATH_REFUSED')
        headers = {'Accept':'application/json', 'Connection':'close',
                   'Host':urllib.parse.urlsplit(self.cfg['public_url']).netloc}
        if cookie:
            require(re.fullmatch('[A-Za-z0-9_-]{32,128}', cookie), 'INVALID_SESSION_COOKIE')
            headers['Cookie'] = COOKIE+'='+cookie
        if method != 'GET': headers['Origin'] = self.cfg['public_url']
        if body is not None: headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection('127.0.0.1', self.cfg['port'], timeout=3)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse(); raw = response.read(65537)
            require(len(raw) <= 65536, 'OPS_RESPONSE_TOO_LARGE')
            value = strict_json(raw); require(isinstance(value, dict), 'INVALID_OPS_RESPONSE')
            return response.status, value, response.getheader('Set-Cookie')
        except (OSError, http.client.HTTPException):
            raise ReadFailure('OPS_CONNECTION_FAILED') from None
        finally:
            connection.close()

    def health(self):
        status, data, _ = self.request('GET', '/healthz')
        require(status == 200 and data == {'status':'ok','platform':'ops','scope':'web_process_only'}, 'OPS_NOT_HEALTHY')

    def login(self):
        status, data, header = self.request('POST', '/api/session', body={'password':secret(self.cfg['access_file'])})
        require(status == 200 and data.get('authenticated') is True and header, 'OPS_LOGIN_FAILED')
        jar = SimpleCookie(); jar.load(header)
        require(COOKIE in jar and re.fullmatch('[A-Za-z0-9_-]{32,128}', jar[COOKIE].value), 'INVALID_SESSION_COOKIE')
        cookie = jar[COOKIE].value
        self.authenticated(cookie)
        return cookie

    def authenticated(self, cookie):
        status, data, _ = self.request('GET', '/api/session', cookie)
        require(status == 200 and data.get('authenticated') is True, 'SESSION_NOT_AUTHENTICATED')
        status, data, _ = self.request('GET', '/api/catalog', cookie)
        require(status == 200 and {x['id'] for x in data.get('platforms', [])}
                == {'model','data','knowledge','agent'}, 'AUTHENTICATED_CATALOG_FAILED')

    def expired(self, cookie):
        status, data, _ = self.request('GET', '/api/session', cookie)
        require(status == 200 and data.get('authenticated') is False, 'OLD_SESSION_STILL_VALID')
        status, data, _ = self.request('GET', '/api/catalog', cookie)
        require(status == 401 and data.get('error', {}).get('code') == 'LOGIN_REQUIRED', 'OLD_SESSION_NOT_REJECTED')

    def logout(self, cookie):
        status, data, _ = self.request('DELETE', '/api/session', cookie)
        require(status == 200 and data.get('authenticated') is False, 'TEST_SESSION_LOGOUT_FAILED')
        self.expired(cookie)


def verify(record, path, directory, cfg, owner, docker, api, wait_seconds):
    require(0 <= (date(now())-date(record['created_at'])).total_seconds() < 900, 'SESSION_PROOF_WINDOW_EXPIRED')
    require(files(directory, cfg) == record['files'], 'CONFIG_OR_CREDENTIAL_CHANGED')
    ident = record['container']['identity']['id']
    deadline = time.monotonic()+wait_seconds
    while True:
        current = container(docker.inspect(ident), directory, cfg, owner)
        require(current['identity'] == record['container']['identity'], 'CONTAINER_IDENTITY_CHANGED')
        if current['healthy'] and date(current['started']) > date(record['container']['started']):
            api.health(); break
        if time.monotonic() >= deadline: raise ReadFailure('OPS_RESTART_NOT_HEALTHY')
        time.sleep(min(0.5, max(0, deadline-time.monotonic())))
    # No upstream GETs are issued: compare the old persisted times before any refresh.
    require(cache(cfg) == record['cache'], 'CACHE_CHANGED_OR_CONCURRENT_READ')
    api.expired(record['old_cookie'])
    new_cookie = api.login()
    api.logout(new_cookie)
    require(files(directory, cfg) == record['files'], 'CONFIG_OR_CREDENTIAL_CHANGED')
    require(cache(cfg) == record['cache'], 'CACHE_CHANGED_OR_CONCURRENT_READ')
    report = {'stage':'O03-ops-restart-acceptance', 'result':'passed', 'checked_at':now(),
              'scope':'same Ops container restart, configuration, persisted cache and auth lifecycle',
              'container_id':ident, 'image_id':current['identity']['image'],
              'started_before':record['container']['started'], 'started_after':current['started'],
              'same_container_image_mounts':True, 'configuration_and_credentials_preserved':True,
              'cache_entries_preserved':len(record['cache']['success_times']), 'cache_times_exactly_preserved':True,
              'old_session_rejected':True, 'same_access_code_login':True, 'test_session_logged_out':True,
              'restart_command_completion_recorded':record.get('restart_command_completed', False),
              'business_inference_requested':False, 'upstream_api_requests':0,
              'other_service_control_requested':False, 'browser_acceptance':'deferred_by_user',
              'evidence_reused':False}
    record['report'] = report
    record.pop('old_cookie', None)
    write_record(path, record)
    return report


def run(action, directory, docker=None, api_factory=API, wait_seconds=45):
    directory = manage.safe(directory)
    cfg = manage.existing(directory)
    owner = strict_json(read_file(directory/'owner.json', private=True)[0])
    with evidence_lock(directory, action == 'run') as path:
        if path.exists():
            record = strict_json(read_file(path, 512*1024, private=True)[0])
            require(isinstance(record, dict) and record.get('schema') == 1 and record.get('directory') == str(directory)
                    and record.get('owner') == owner['owner'], 'BASELINE_IDENTITY_MISMATCH')
            require(isinstance(record.get('report', {}), dict), 'INVALID_BASELINE')
            if record.get('report', {}).get('result') == 'passed':
                # Preserve historical acceptance; do not silently execute another restart/check.
                return dict(record['report'], evidence_reused=True, live_checks_performed=False)
        else:
            require(action == 'run', 'NO_RESTART_BASELINE')
            docker = docker or Docker(project=manage.prepared_project(directory)); api = api_factory(cfg)
            before_files = files(directory, cfg); before_cache = cache(cfg)
            before = container(docker.inspect(docker.discover()), directory, cfg, owner)
            require(before['healthy'], 'OPS_NOT_HEALTHY_BEFORE_RESTART')
            api.health(); old_cookie = api.login()
            try:
                require(files(directory, cfg) == before_files and cache(cfg) == before_cache, 'BASELINE_CHANGED')
                require(container(docker.inspect(before['identity']['id']), directory, cfg, owner) == before, 'BASELINE_CHANGED')
                record = {'schema':1,'directory':str(directory),'owner':owner['owner'],'created_at':now(),
                          'files':before_files,'cache':before_cache,'container':before,
                          'old_cookie':old_cookie,'restart_intent_saved':True,'restart_command_completed':False}
                write_record(path, record)
            except Exception:
                if not path.exists(): api.logout(old_cookie)
                raise
            # From this point, run/check never automatically issue a second restart.
            docker.restart(before['identity']['id'])
            record['restart_command_completed'] = True
            write_record(path, record)
            return verify(record, path, directory, cfg, owner, docker, api, wait_seconds)
        docker = docker or Docker(project=manage.prepared_project(directory)); api = api_factory(cfg)
        return verify(record, path, directory, cfg, owner, docker, api, wait_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run','check'))
    parser.add_argument('--state-dir', type=Path, default=manage.STATE)
    args = parser.parse_args()
    try:
        report = run(args.action, args.state_dir)
    except (ReadFailure, OSError, ValueError, KeyError, TypeError) as exc:
        report = {'stage':'O03-ops-restart-acceptance','result':'attention_required',
                  'error':{'code':exc.code if isinstance(exc, ReadFailure) else 'LOCAL_CHECK_ERROR'},
                  'baseline_policy':'retain; check never restarts; do not delete to rerun',
                  'business_inference_requested':False,'other_service_control_requested':False}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['result'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
