"""Local operational primitives. Command errors never echo credentials or SQL rows."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ('playground', 'gateway', 'dispatcher', 'reconciler', 'gc',
            'deployment', 'training', 'management', 'operation')
DATABASES = ('model_operations', 'model_management', 'model_training', 'model_deployment')
OWNERS = ('operation-dispatcher', 'deployment-serving')


class OpsError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise OpsError(message)


def run(args, *, data=None, output=None, timeout=60):
    try:
        result = subprocess.run(args, input=data, stdout=output or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise OpsError('Command timed out; inspect the target service locally') from None
    except OSError:
        raise OpsError('Required command is unavailable: ' + args[0]) from None
    require(result.returncode == 0, 'Command failed: ' + args[0] + ' (exit ' + str(result.returncode) + ')')
    return result.stdout


def docker(*args, **kwargs):
    return run(['docker', *args], **kwargs)


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,180}', value),
            'Unsafe Docker identifier')
    return value


def containers():
    ids = docker('ps', '-aq').decode().split()
    result = []
    for start in range(0, len(ids), 100):
        result.extend(json.loads(docker('inspect', *ids[start:start + 100])))
    return result


def stopped(item):
    state = item.get('State', {})
    return state.get('Status') == 'exited' and not any(state.get(k) for k in ('Running', 'Paused', 'Restarting'))


def public_container(item):
    return {'name': item['Name'].lstrip('/'), 'id': item['Id'], 'image_id': item['Image'],
            'state': item['State']['Status'], 'started_at': item['State'].get('StartedAt'),
            'finished_at': item['State'].get('FinishedAt'),
            'health': item['State'].get('Health', {}).get('Status', 'not_configured')}


def mount(item, destination):
    matches = [m for m in item.get('Mounts', []) if m['Destination'] == destination]
    require(len(matches) == 1 and matches[0]['Type'] == 'volume', 'Expected named volume at ' + destination)
    return identifier(matches[0]['Name'])


def local_daemon():
    endpoint = json.loads(docker('context', 'inspect'))[0]['Endpoints']['docker']['Host']
    require(not os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT')
            and endpoint == 'unix:///var/run/docker.sock', 'Use the local Docker socket/context')


class Context:
    def __init__(self):
        # Explicit local socket; no inherited remote Docker context may select another host.
        local_daemon()
        self.config = json.loads(docker('compose', '--project-directory', str(ROOT),
                                       '-f', str(ROOT / 'docker-compose.yml'),
                                       '--profile', 'playground', 'config', '--format', 'json'))
        self.items = containers()
        self.by_name = {c['Name'].lstrip('/'): c for c in self.items}
        self.services = {}
        for service in SERVICES:
            name = self.config['services'][service]['container_name']
            require(name in self.by_name, 'Missing platform container: ' + name)
            item = self.by_name[name]
            labels = item['Config'].get('Labels') or {}
            require(labels.get('com.docker.compose.project') == self.config['name']
                    and labels.get('com.docker.compose.service') == service,
                    'Container does not belong to this Compose project: ' + name)
            self.services[service] = item
        self.pg = self.by_name.get('postgres')
        self.mlflow = self.by_name.get('ops-mlflow')
        require(self.pg is not None and self.mlflow is not None, 'Expected local postgres and ops-mlflow containers')
        pg_url = urlsplit(self.config['services']['operation']['environment']['POSTGRES_URL'])
        require(pg_url.hostname == 'postgres' and pg_url.port in (None, 5432),
                'Backup tooling supports the local postgres:5432 service')
        for service, item in self.services.items():
            actual = dict(v.split('=', 1) for v in item['Config'].get('Env', []) if '=' in v)
            for key, value in self.config['services'][service].get('environment', {}).items():
                require(actual.get(key) == str(value), 'Compose/runtime configuration drift in ' + service + ': ' + key)
            require(not actual.get('EXECUTION_NODES_FILE'), 'Remote execution configuration is outside local maintenance scope')
        self.volumes = {'gpu-admission': mount(self.services['training'], '/gpu-admission'),
                        'training-outputs': mount(self.services['training'], '/training-outputs'),
                        'serving-cache': mount(self.services['deployment'], '/serving-cache'),
                        'mlflow': mount(self.mlflow, '/mlflow')}
        for name in self.volumes.values():
            info = json.loads(docker('volume', 'inspect', name))[0]
            require(info['Driver'] == 'local' and not info.get('Options'), 'Only ordinary local named volumes are supported')
        self.helper_image = self.services['playground']['Image']
        require(self.helper_image.startswith('sha256:'), 'Pinned local helper image required')

    def sql(self, database, sql):
        require(database in DATABASES, 'Unexpected database')
        return docker('exec', '-i', self.pg['Id'], 'sh', '-c',
                      'exec psql -X -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$1"',
                      'sh', database, data=sql.encode(), timeout=300).decode().strip()

    def helper(self, volume, mode, *, args=(), output=None, timeout=300):
        require(volume in self.volumes.values(), 'Unrecognized volume')
        # --mount refuses a missing source volume; it cannot silently back up an empty replacement.
        docker('volume', 'inspect', volume)
        name = 'model-maintenance-' + os.urandom(12).hex()
        try:
            return docker('run', '--rm', '--pull=never', '--name', name,
                          '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE',
                          '--security-opt', 'no-new-privileges', '--user', '0:0',
                          '--mount', 'type=volume,src=' + volume + ',dst=/data' + ('' if mode == 'delete-cache' else ',readonly'),
                          '--entrypoint', 'python', self.helper_image, '-c',
                          (ROOT / 'ops/volume_helper.py').read_text(), mode, *args,
                          output=output, timeout=timeout)
        finally:
            # Only the unique helper created here; never a platform Job or source volume.
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)

    def cold(self):
        now = containers()
        current = {c['Id']: c for c in now}
        required = list(self.services.values()) + [self.mlflow]
        for old in required:
            item = current.get(old['Id'])
            require(item is not None and stopped(item), 'Stop platform services and MLflow before offline maintenance')
            require(public_container(item) == public_container(old), 'A service changed during offline maintenance')
        for item in now:
            if (item['Config'].get('Labels') or {}).get('model-platform.owner') in OWNERS:
                require(stopped(item), 'A platform Job/runtime is still active or uncertain')
            if not stopped(item):
                require(not any(m.get('Name') in self.volumes.values() and m.get('RW') for m in item.get('Mounts', [])),
                        'A running container still has writable access to a retained platform volume')
        require(self.pg['Id'] in current and current[self.pg['Id']]['State']['Running'], 'PostgreSQL must remain running')
        # Refuse data restored into a fresh Kafka while old work could execute.
        for db, sql in (
            ('model_operations', "SELECT count(*) FROM operations WHERE status NOT IN ('succeeded','failed','cancelled')"),
            ('model_operations', "SELECT count(*) FROM operation_outbox WHERE published_at IS NULL"),
            ('model_training', "SELECT count(*) FROM training_runs WHERE status NOT IN ('succeeded','failed','cancelled')"),
            ('model_training', "SELECT count(*) FROM training_output_recoveries WHERE status IN ('queued','running')"),
            ('model_management', "SELECT count(*) FROM model_versions WHERE state NOT IN ('ready','failed')"),
            ('model_deployment', "SELECT count(*) FROM deployments WHERE engine IN ('vllm','vllm-lora') AND (desired_state <> 'stopped' OR runtime_state <> 'stopped')"),
        ):
            require(self.sql(db, sql) == '0', 'Unfinished state in ' + db + '; finish/cancel through the API before maintenance')
        for db in DATABASES[1:]:
            require(self.sql(db, "SELECT count(*) FROM operation_submissions WHERE status <> 'delivered'") == '0',
                    'Undelivered submission in ' + db)


def table_counts(ctx, database):
    names = ctx.sql(database, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").splitlines()
    result = {}
    for name in names:
        require(re.fullmatch(r'[a-z_][a-z_0-9]*', name), 'Unexpected table identifier')
        result[name] = int(ctx.sql(database, 'SELECT count(*) FROM public."' + name + '"'))
    require(result, 'Empty application schema in ' + database)
    return result
