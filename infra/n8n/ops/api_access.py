#!/usr/bin/env python3
"""Connect to the pinned n8n instance using its normal account authentication."""
import argparse
import fcntl
import getpass
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
VERSION = '2.40.5'
DEFAULT_KEY = ROOT / '.secrets/agent-integration-api-key.json'
SCOPES = ['credential:list', 'credential:read', 'credential:create',
          'workflow:list', 'workflow:read', 'workflow:create',
          'workflow:activate', 'workflow:deactivate', 'execution:list', 'execution:read']


def origin(value):
    u = urllib.parse.urlsplit(value)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.path not in ('', '/') or u.query or u.fragment or re.search(r'[\s\x00-\x20\x7f]', value):
        raise ValueError('서버 주소는 경로·인증정보 없는 http(s) origin이어야 합니다.')
    local = u.hostname == 'localhost'
    try: local = local or ipaddress.ip_address(u.hostname).is_loopback
    except ValueError: pass
    if u.scheme == 'http' and not local:
        raise ValueError('인증정보 전송에는 HTTPS 또는 서버의 loopback 주소를 사용하세요.')
    return value.rstrip('/')


def read_private(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('현재 사용자 소유의 일반 0600 파일이 필요합니다: ' + path.name)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('상태 파일이 너무 큽니다.')
    return json.loads(path.read_text())


def save_private(path, value):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('안전하지 않은 상태 파일: ' + path.name)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, allow_nan=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally: Path(temporary).unlink(missing_ok=True)


STAGES = {'/rest/owner/setup': 'owner_setup', '/rest/login': 'login',
          '/rest/api-keys': 'api_key_create', '/rest/settings': 'settings'}
STAGE_LABELS = {'owner_setup': '최초 계정 등록', 'login': '기존 계정 로그인',
                'api_key_create': 'API 키 발급', 'settings': '서버 설정 조회',
                'public_api': '공개 API 요청', 'request': '서버 요청'}


def safe_problem(raw):
    """Recognize pinned n8n validation rules; never echo arbitrary response text."""
    try: issue = json.loads(raw)
    except (ValueError, TypeError, UnicodeError): return None, None
    if not isinstance(issue, dict): return None, None
    path = issue.get('path')
    field = path[0] if isinstance(path, list) and path and isinstance(path[0], str) else None
    if field not in ('email', 'firstName', 'lastName', 'password', 'label', 'scopes', 'expiresAt'):
        field = None
    rules = {'too_small': 'minimum', 'too_big': 'maximum', 'invalid_type': 'type',
             'invalid_string': 'format'}
    rule = rules.get(issue.get('code')) if isinstance(issue.get('code'), str) and field else None
    if field == 'password':
        if issue.get('message') == 'Password must contain at least 1 number.': rule = 'password_number'
        if issue.get('message') == 'Password must contain at least 1 uppercase letter.': rule = 'password_uppercase'
    if issue.get('message') == 'Instance owner already setup': return None, 'owner_exists'
    if issue.get('message') == 'Invalid scopes for user role': return 'scopes', 'invalid_scopes'
    return field, rule


class HTTPFailure(ValueError):
    def __init__(self, status, stage='request', raw=b''):
        self.status = status
        self.stage = stage if stage in STAGE_LABELS else 'request'
        self.field, self.rule = safe_problem(raw)
        detail = ', '.join(f'{k}={v}' for k, v in [('field', self.field), ('rule', self.rule)] if v)
        hint = '인증·권한·서버 상태를 확인하세요.'
        if self.field == 'password': hint = '위에 안내한 비밀번호 조건을 확인하세요.'
        elif self.field == 'email': hint = '이메일 형식을 확인하세요.'
        elif self.field in ('firstName', 'lastName'): hint = '이름과 성을 각각 입력하세요. 한글도 사용할 수 있습니다.'
        elif self.rule == 'owner_exists': hint = '기존 계정이 있습니다. 같은 명령을 다시 실행해 로그인하세요.'
        elif self.field == 'scopes': hint = '계정의 API 키 발급 권한을 확인하세요. 기존 계정을 초기화하지 마세요.'
        super().__init__(f'n8n HTTP {status} [{self.stage}: {STAGE_LABELS[self.stage]}]'
                         + (f' ({detail})' if detail else '') + '. ' + hint)


def setup_required(settings):
    value = settings.get('userManagement', {}).get('showSetupOnFirstLoad')
    if not isinstance(value, bool): raise ValueError('n8n 계정 등록 상태를 확인하지 못했습니다.')
    return value


def password_minimum(settings):
    value = settings.get('userManagement', {}).get('passwordMinLength', 8)
    if type(value) is not int or not 8 <= value <= 64:
        raise ValueError('n8n 비밀번호 길이 정책을 확인하지 못했습니다.')
    return value


def password_policy(minimum):
    return f'비밀번호 조건: {minimum}~64자, 영문 대문자(A–Z)와 숫자(0–9) 각각 1개 이상. 특수문자는 필수가 아닙니다.'


def validate_setup(value, minimum):
    for field, label in [('email', '이메일'), ('firstName', '이름'), ('lastName', '성')]:
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f'{label}을 입력하세요. (field={field})')
    password = value.get('password')
    # Match JavaScript string length and ASCII regexes in the pinned n8n schema.
    if (not isinstance(password, str)
            or not minimum <= len(password.encode('utf-16-le', errors='surrogatepass')) // 2 <= 64
            or not re.search('[A-Z]', password) or not re.search('[0-9]', password)):
        raise ValueError('서버에 계정 등록 요청을 보내지 않았습니다. ' + password_policy(minimum))


def diagnose(client, key_path):
    """Read-only: no login, key creation, local directory or lock creation."""
    settings = client.request('GET', '/rest/settings')['data']
    initial = setup_required(settings)
    key_exists = key_path.exists()
    pending = key_path.with_suffix('.pending.json').exists()
    return {'result': 'diagnosed', 'setup_required': initial,
            'password_min_length': password_minimum(settings), 'password_max_length': 64,
            'api_key_file_exists': key_exists, 'pending_key_request_exists': pending,
            'next_step': 'reuse_key' if key_exists else ('review_pending_key' if pending else ('register_owner' if initial else 'login_existing_account')),
            'inference_executed': False, 'changes_made': False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class Client:
    def __init__(self, url, key=None):
        self.url, self.key = origin(url), key
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, method, route, body=None, *, headers=None, timeout=40):
        if not route.startswith('/') or route.startswith('//'):
            raise ValueError('잘못된 n8n 상대 경로입니다.')
        h = {'Content-Type': 'application/json'}
        if self.key and route.startswith('/api/v1/'): h['X-N8N-API-KEY'] = self.key
        h.update(headers or {})
        req = urllib.request.Request(self.url + route, method=method, headers=h,
            data=None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode())
        try:
            try: response = self.opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as exc: response = exc
            with response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024: raise ValueError('n8n 응답 크기 제한을 초과했습니다.')
                if not 200 <= response.status < 300:
                    stage = STAGES.get(route.split('?')[0], 'public_api' if route.startswith('/api/v1/') else 'request')
                    raise HTTPFailure(response.status, stage, raw)
                return json.loads(raw) if raw else {}
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise ValueError('n8n 응답을 확인하지 못했습니다. 저장한 상태를 보존하고 같은 명령으로 조회·복구하세요.') from None

    def items(self, route):
        out, cursor, seen = [], None, set()
        for _ in range(100):
            query = {'limit': 100}
            if cursor: query['cursor'] = cursor
            page = self.request('GET', route + ('&' if '?' in route else '?') + urllib.parse.urlencode(query))
            out.extend(page['data']); cursor = page.get('nextCursor')
            if not cursor: return out
            if cursor in seen: raise ValueError('n8n 목록 cursor가 반복됩니다.')
            seen.add(cursor)
        raise ValueError('n8n 목록이 조회 제한을 초과했습니다.')


def verify_key(value):
    client = Client(value['url'], value['api_key'])
    client.request('GET', '/api/v1/workflows?limit=1')
    return client


def connect(client, key_path, *, setup=None, login=None, minimum=8):
    """Only user-supplied account data; no password reset, DB access or auth bypass."""
    if key_path.exists():
        value = read_private(key_path)
        if value['url'] != client.url: raise ValueError('API 키 파일의 서버 주소가 다릅니다.')
        verify_key(value)
        return {'result': 'connected', 'reused': True}
    if setup:
        validate_setup(setup, minimum)
        client.request('POST', '/rest/owner/setup', setup)
    elif login:
        client.request('POST', '/rest/login', login)
    else: raise ValueError('계정 인증정보가 필요합니다.')
    settings = client.request('GET', '/rest/settings')['data']
    if settings.get('versionCli') != VERSION:
        raise ValueError('이 연결 도구는 n8n ' + VERSION + ' 전용입니다.')
    state_path = key_path.with_suffix('.pending.json')
    if state_path.exists():
        # Creation is not idempotent; never silently create another key after a lost response.
        raise ValueError('이전 API 키 발급 응답이 불확실합니다. n8n 계정의 API 키 목록에서 pending 파일의 label을 확인하세요. 상태 파일을 지워 자동 재발급하지 마세요.')
    label = 'Agent flow ' + str(uuid.uuid4())
    save_private(state_path, {'label': label, 'url': client.url, 'created_at': int(time.time())})
    try:
        result = client.request('POST', '/rest/api-keys', {'label': label, 'scopes': SCOPES, 'expiresAt': None})['data']
    except HTTPFailure as exc:
        if exc.status in (400, 401, 403, 404, 413, 415, 422): state_path.unlink()
        raise
    if not isinstance(result.get('rawApiKey'), str) or not result['rawApiKey']:
        raise ValueError('API 키 응답을 확인하지 못했습니다. pending 기록을 보존하세요.')
    value = {'url': client.url, 'api_key': result['rawApiKey'], 'id': result['id'], 'label': label, 'n8n_version': VERSION}
    save_private(key_path, value)
    state_path.unlink()
    verify_key(value)
    return {'result': 'connected', 'reused': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://127.0.0.1:16200')
    p.add_argument('--key-file', type=Path, default=DEFAULT_KEY)
    p.add_argument('--diagnose', action='store_true', help='등록 상태만 조회합니다. 계정·키·파일을 변경하지 않습니다.')
    a = p.parse_args()
    path = a.key_file.expanduser().absolute()
    if a.diagnose:
        print(json.dumps(diagnose(Client(a.url), path), ensure_ascii=False, indent=2))
        return
    if path.parent.is_symlink(): raise ValueError('비밀 폴더에 심볼릭 링크를 사용할 수 없습니다.')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise ValueError('비밀 폴더는 현재 사용자 소유·0700 권한이어야 합니다.')
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('다른 연결 작업이 진행 중입니다.') from None
        client = Client(a.url)
        if path.exists(): result = connect(client, path)
        else:
            settings = client.request('GET', '/rest/settings')['data']
            initial = setup_required(settings)
            minimum = password_minimum(settings)
            print('n8n 최초 소유자 계정을 등록합니다.' if initial else '기존 n8n 소유자 또는 관리자 계정으로 로그인합니다.')
            if initial: print(password_policy(minimum))
            email = input('n8n 이메일: ').strip()
            first = last = None
            if initial:
                first = input('이름: ').strip(); last = input('성: ').strip()
            password = getpass.getpass('n8n 비밀번호: ')
            if initial:
                setup = {'email': email, 'firstName': first, 'lastName': last, 'password': password}
                validate_setup(setup, minimum)
                if getpass.getpass('비밀번호 확인: ') != password: raise ValueError('비밀번호가 일치하지 않습니다.')
                print('최초 계정 등록과 연동 API 키 발급을 진행합니다.')
                result = connect(client, path, setup=setup, minimum=minimum)
            else:
                mfa = getpass.getpass('MFA 코드 (미사용 시 Enter): ')
                result = connect(client, path, login={'emailOrLdapLoginId': email, 'password': password, **({'mfaCode': mfa} if mfa else {})})
        print(json.dumps({**result, 'n8n_version': VERSION, 'password_saved': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyError, TypeError, EOFError):
        # Never print a remote response or credentials through a traceback.
        import sys
        exc = sys.exc_info()[1]
        raise SystemExit('FAIL: ' + (str(exc) if isinstance(exc, ValueError) else '연결 설정을 확인하세요. 비밀번호·키를 로그에 붙이지 마세요.'))
