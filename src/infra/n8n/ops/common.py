"""Narrow Docker operations for infra n8n; never print container environments."""
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'infrastructure'


def environment():
    # Persisted settings are authoritative for volume/key migration.
    result = {k: v for k, v in os.environ.items() if not k.startswith(('COMPOSE_', 'N8N_'))}
    # The n8n subset intentionally shares the infrastructure project with other services.
    result['COMPOSE_IGNORE_ORPHANS'] = 'true'
    return result


def run(args, *, cwd=ROOT, check=True, timeout=60, stream=False):
    p = subprocess.run(args, cwd=cwd, env=environment(), text=True,
                       capture_output=not stream, timeout=timeout)
    if check and p.returncode:
        raise ValueError('Command failed: ' + ' '.join(args[:4]) + '; run src/infra/n8n/ops/diagnose.py')
    return p


def compose(args, *, whole=False, **kwargs):
    root = ROOT.parent if whole else ROOT
    return run(['docker', 'compose', '-p', PROJECT, '--project-directory', str(root),
                '-f', str(root/'docker-compose.yml'), *args], cwd=root, **kwargs)


def config(whole=False):
    return json.loads(compose(['config', '--format', 'json'], whole=whole).stdout)


def containers(filters):
    args = ['docker', 'ps', '-aq']
    for f in filters: args.extend(['--filter', f])
    ids = run(args).stdout.split()
    if not ids: return []
    return json.loads(run(['docker', 'inspect', *ids]).stdout)


def redact(value, roots=None):
    text = str(value)
    for root in roots or [ROOT, ROOT.parents[2]/'src/platform/agent/n8n']:
        for name in ('db-admin', 'db-app', 'encryption-key'):
            try:
                token = (root/'.secrets'/name).read_text().strip()
                if token: text = text.replace(token, '[REDACTED]')
            except OSError: pass
    text = re.sub(r'(postgres(?:ql)?://)[^\s@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(?i)(Bearer\s+)[\w.+/=-]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:password|encryption.?key|api.?key|token)[\"\s]*[:=]\s*)[^\s,}]+', r'\1[REDACTED]', text)
    return text
