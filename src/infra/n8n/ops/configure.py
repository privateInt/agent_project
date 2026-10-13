#!/usr/bin/env python3
"""Prepare infra-owned n8n settings, preserving the complete A07 configuration."""
import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = ('db-admin', 'db-app', 'encryption-key')
OLD_VOLUMES = {'flows-db': 'agent-flows_flows-db', 'flows-data': 'agent-flows_flows-data'}
NEW_VOLUMES = {'flows-db': 'ops-n8n-db', 'flows-data': 'ops-n8n-data'}


def public_url(value):
    u = urlsplit(value)
    if (u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password
            or u.path not in ('', '/') or u.query or u.fragment
            or re.search(r'[\s\x00-\x20\x7f$#]', value)):
        raise ValueError('Use an http(s) public origin without credentials/path/query')
    return value.rstrip('/')


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key in values or not re.fullmatch(r'N8N_[A-Z_]+', key):
            raise ValueError('Unsupported/duplicate n8n setting; preserve the original file and review it')
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            value = value[1:-1]
        if re.search(r'[\x00-\x20\x7f$#]', value):
            raise ValueError('Unsupported interpolation/whitespace in n8n settings')
        values[key] = value
    return values


def check_files(root):
    for p in (root, root/'.env', root/'.secrets', *(root/'.secrets'/n for n in SECRET_NAMES)):
        if p.is_symlink():
            raise ValueError('Symlink settings are not supported')
    if not (root/'.env').is_file() or not (root/'.secrets').is_dir():
        raise ValueError('Partial n8n configuration; nothing was replaced')
    for name in SECRET_NAMES:
        path = root/'.secrets'/name
        if not path.is_file() or len(path.read_bytes().strip()) < 32:
            raise ValueError('Missing/invalid n8n secret; existing keys will not be regenerated')
    return read_env(root/'.env')


def validate(values):
    if not 1024 <= int(values['N8N_PORT']) <= 65535:
        raise ValueError('Port must be 1024..65535')
    if ipaddress.ip_address(values['N8N_BIND_HOST']).version != 4:
        raise ValueError('Use an IPv4 bind address')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', values['N8N_AGENT_NETWORK']):
        raise ValueError('Invalid network')
    public_url(values['N8N_PUBLIC_URL'])
    if values['N8N_SECURE_COOKIE'] not in ('true', 'false'):
        raise ValueError('Invalid secure cookie setting')


def configure(root=ROOT, port=16200, bind='0.0.0.0', network='ops-infra', url=None, legacy=None):
    root = Path(root)
    state_path = root/'.state.json'
    existing = any(p.exists() or p.is_symlink() for p in (root/'.env', root/'.secrets', state_path))
    if existing:
        values = check_files(root)
        validate(values)
        if state_path.is_symlink() or not state_path.is_file():
            raise ValueError('Missing infra n8n state; will not guess the volume or encryption key')
        state = json.loads(state_path.read_text())
        if state.get('format') != 1 or state.get('origin') not in ('new', 'a07'):
            raise ValueError('Unsupported n8n state')
        if {k: values.get(v) for k, v in [('flows-db', 'N8N_DB_VOLUME'), ('flows-data', 'N8N_DATA_VOLUME')]} != state['volumes']:
            raise ValueError('Volume configuration changed; refusing to initialize a different database')
        return state
    legacy = Path(legacy) if legacy else root.parents[2]/'src/platform/agent/n8n'
    legacy_exists = any(p.exists() or p.is_symlink() for p in (legacy/'.env', legacy/'.secrets'))
    if legacy_exists:
        values = check_files(legacy)
        validate(values)
        if 'N8N_DB_VOLUME' in values or 'N8N_DATA_VOLUME' in values:
            raise ValueError('Custom A07 volume configuration requires explicit migration review')
        contents = (legacy/'.env').read_bytes().rstrip(b'\n') + b'\n'
        data = {n: (legacy/'.secrets'/n).read_bytes() for n in SECRET_NAMES}
        origin, volumes = 'a07', OLD_VOLUMES.copy()
    else:
        url = public_url(url or f'http://localhost:{port}')
        values = dict(N8N_PORT=str(port), N8N_BIND_HOST=bind, N8N_AGENT_NETWORK=network,
                      N8N_PUBLIC_URL=url, N8N_SECURE_COOKIE=str(url.startswith('https:')).lower())
        validate(values)
        contents = ''.join(f'{k}={v}\n' for k, v in values.items()).encode()
        data = {n: (secrets.token_hex(32)+'\n').encode() for n in SECRET_NAMES}
        origin, volumes = 'new', NEW_VOLUMES.copy()
    contents += f"N8N_DB_VOLUME={volumes['flows-db']}\nN8N_DATA_VOLUME={volumes['flows-data']}\n".encode()
    state = {'format': 1, 'origin': origin, 'config_id': str(uuid.uuid4()), 'volumes': volumes}
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.setup-', dir=root))
    written = []
    try:
        (staging/'.env').write_bytes(contents); (staging/'.env').chmod(0o600)
        (staging/'.secrets').mkdir(mode=0o700)
        for n, value in data.items():
            p = staging/'.secrets'/n
            p.write_bytes(value); p.chmod(0o444)
        (staging/'.state.json').write_text(json.dumps(state, indent=2)+'\n')
        (staging/'.state.json').chmod(0o600)
        for name in ('.secrets', '.env', '.state.json'):
            target = root/name
            if target.exists() or target.is_symlink():
                raise ValueError('Configuration changed concurrently')
            os.rename(staging/name, target)
            written.append(target)
    except BaseException:
        for p in reversed(written):
            if p.is_dir(): shutil.rmtree(p)
            else: p.unlink()
        raise
    finally:
        shutil.rmtree(staging)
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=16200)
    p.add_argument('--bind-host', default='0.0.0.0')
    p.add_argument('--network', default='ops-infra')
    p.add_argument('--public-url')
    a = p.parse_args()
    state = configure(port=a.port, bind=a.bind_host, network=a.network, url=a.public_url)
    print('n8n configuration preserved/prepared; origin=' + state['origin'])
    print('Start with: python3 src/infra/ops/up.py --n8n-only (from the project root)')

if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyError) as e: raise SystemExit('FAIL: '+str(e))
