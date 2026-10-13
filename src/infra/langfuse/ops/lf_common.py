"""Private local state and bounded, non-redirecting Langfuse HTTP transport."""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'infrastructure-langfuse'
OWNER = 'org.agent-project.langfuse.owner'
DEFAULT_STATE = Path.home() / '.local/state/agent-project-langfuse'
PORT = 16320
API_URL = 'http://127.0.0.1:' + str(PORT)
SERVICES = ('lf-postgres', 'lf-clickhouse', 'lf-redis', 'lf-minio', 'lf-worker', 'lf-web')
STORES = {'postgres': '/var/lib/postgresql/data', 'clickhouse': '/var/lib/clickhouse',
          'clickhouse-logs': '/var/log/clickhouse-server', 'redis': '/data', 'minio': '/data'}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def safe_path(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        require(not part.is_symlink(), '심볼릭 링크 경로는 지원하지 않습니다: ' + str(part))
    return path


def private_read(path):
    path = safe_path(path)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            '비공개 파일 소유자·권한을 확인하세요: ' + str(path))
    return path.read_text()


def read_json(path):
    return json.loads(private_read(path))


def write_private(path, value):
    path = safe_path(path)
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    fd, name = tempfile.mkstemp(prefix='.lf-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@contextlib.contextmanager
def operation_lock():
    path = safe_path(ROOT / '.ops.lock')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('다른 Langfuse infra 작업이 실행 중입니다.')
        yield


class DockerFailure(ValueError):
    """Keep captured output private until the caller can redact known credentials."""
    def __init__(self, args, returncode, stdout='', stderr=''):
        operation = args[0] if args else 'command'
        if operation == 'compose':
            operation += ' ' + next((x for x in args if x in ('up', 'config', 'stop')), 'command')
        self.detail = (stdout or '') + '\n' + (stderr or '')
        self.returncode = returncode
        super().__init__('Docker ' + operation + ' 실패 (exit ' + str(returncode)
                         + '). manage.py diagnose로 비밀을 가린 진단을 확인하세요.')


class Docker:
    def __init__(self):
        require(not os.environ.get('DOCKER_API_VERSION'), 'DOCKER_API_VERSION을 해제하세요.')
        # No inherited Compose or Langfuse substitution can redirect this isolated group.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('COMPOSE_', 'LF_', 'LANGFUSE_'))}

    def run(self, args, timeout=120, combined=False):
        result = subprocess.run(['docker', *args], env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode != 0:
            raise DockerFailure(args, result.returncode, result.stdout, result.stderr)
        return result.stdout + result.stderr if combined else result.stdout

    def json(self, args):
        return json.loads(self.run(args))


class HttpFailure(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__('Langfuse HTTP ' + str(status) + ': 연결·인증·준비 상태를 확인하세요.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, config, url=API_URL):
        # Runtime CLI always uses the local fixed port; url injection is only for HTTP tests.
        self.url = url
        self.auth = base64.b64encode((config['public_key'] + ':' + config['secret_key']).encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, payload=None, authenticated=True):
        require(path.startswith('/api/public/'), '허용되지 않은 Langfuse API 경로입니다.')
        headers = {'Accept': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Basic ' + self.auth
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(',', ':')).encode()
            require(len(data) <= 65536, '합성 추적 요청 크기를 초과했습니다.')
            headers.update({'Content-Type': 'application/json', 'x-langfuse-ingestion-version': '4'})
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                require(len(raw) <= 2 * 1024 * 1024, 'Langfuse 응답이 크기 한도를 초과했습니다.')
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            exc.close()
            raise HttpFailure(status_code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HttpFailure('unreachable') from None
