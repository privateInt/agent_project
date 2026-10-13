"""Read-only Langfuse startup diagnostics. Raw env, logs and secrets never become output."""
import base64
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import urllib.parse

from lf_common import (PROJECT, OWNER, SERVICES, STORES, DockerFailure, require, safe_path, read_json)

SECRET_NAME = re.compile(r'password|secret|token|credential|salt|(?:^|_)auth$|authorization|(?:^|_)key(?:$|_)|public_key|s3_key|encryption_key|email', re.I)
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


class Redactor:
    def __init__(self, settings, items):
        values = [str(v) for k, v in settings.items() if SECRET_NAME.search(k) and isinstance(v, str) and v]
        for item in items:
            for entry in item.get('Config', {}).get('Env', []) or []:
                key, sep, value = entry.partition('=')
                if sep and value and (SECRET_NAME.search(key) or re.search(r'://[^/]*@', value)):
                    values.append(value)
                    # URI logging may print just the password, including its encoded form.
                    try:
                        parsed = urllib.parse.urlsplit(value)
                        if parsed.password:
                            values.extend([parsed.password, urllib.parse.unquote(parsed.password)])
                    except ValueError:
                        pass
        if settings.get('public_key') and settings.get('secret_key'):
            values.append(base64.b64encode((settings['public_key'] + ':' + settings['secret_key']).encode()).decode())
        variants = set()
        for value in values:
            variants.update((value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1],
                             urllib.parse.quote(value, safe=''), urllib.parse.quote_plus(value), repr(value)[1:-1]))
        self.values = sorted((v for v in variants if v), key=len, reverse=True)

    def text(self, value, limit=2400):
        text = str(value or '')
        for secret in self.values:
            text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/<>"\']*@', r'\1[REDACTED]@', text)
        text = re.sub(r'(?i)\b(?:Basic|Bearer)\s+[A-Za-z0-9+/=._-]+', '[REDACTED_AUTH]', text)
        # Whole assignment lines also cover credentials not in the saved settings.
        text = re.sub(r'(?im)(\b[\w.-]*(?:password|secret|token|credential|authorization|api[_-]?key|access[_-]?key)[\w.-]*["\']?\s*[:=]\s*)[^\r\n]+',
                      r'\1[REDACTED]', text)
        text = ANSI.sub('', text)
        text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)
        return text[:limit] + ('\n[truncated]' if len(text) > limit else '')


def excerpt(raw, redactor):
    # Redact before selecting/truncating lines so credential fragments cannot survive truncation.
    cleaned = redactor.text(raw, 65536).splitlines()
    pattern = re.compile(r'error|fatal|exception|failed|permission|denied|cannot|can.t|invalid|not found|unhealthy|panic|no space|timeout|migration|zod', re.I)
    hits = [i for i, line in enumerate(cleaned) if pattern.search(line)]
    selected = set()
    for i in hits[-5:]:
        selected.update(range(max(0, i - 1), min(len(cleaned), i + 4)))
    if not selected:
        selected.update(range(max(0, len(cleaned) - 12), len(cleaned)))
    return redactor.text('\n'.join(cleaned[i] for i in sorted(selected)), 3000)


def read_settings(root):
    root = safe_path(root)
    info = root.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            'Langfuse 비공개 상태 폴더 소유자/권한을 확인하세요.')
    settings = read_json(root / 'settings.json')
    require(settings.get('schema') == 1 and settings.get('state_dir') == str(root)
            and settings.get('uid') == os.getuid(), 'Langfuse 상태의 경로/사용자가 일치하지 않습니다.')
    require(read_json(root / 'owner.json') == {'owner': settings.get('owner')}, 'Langfuse 소유권 표식이 일치하지 않습니다.')
    return root, settings


def report(docker, root, failure=None):
    root, settings = read_settings(root)
    ids = docker.run(['ps', '-aq', '--filter', 'label=com.docker.compose.project=' + PROJECT]).split()
    require(all(re.fullmatch(r'[0-9a-f]{12,64}', ident) for ident in ids), 'Docker 컨테이너 ID 형식이 다릅니다.')
    items = docker.json(['inspect', *ids]) if ids else []
    # Foreign instances are never logged or adopted, even when their project label matches.
    for item in items:
        labels = item.get('Config', {}).get('Labels') or {}
        require(labels.get(OWNER) == settings['owner'] and labels.get('com.docker.compose.project') == PROJECT
                and labels.get('com.docker.compose.service') in SERVICES
                and re.fullmatch(r'[0-9a-f]{12,64}', item.get('Id', '')),
                '소유권이 다른 컨테이너가 있습니다. 해당 로그를 읽거나 인수하지 않습니다.')
    redactor = Redactor(settings, items)
    rows = {name: {'state': 'not_created', 'health': None} for name in SERVICES}
    seen = set()
    for item in items:
        name = item['Config']['Labels']['com.docker.compose.service']
        require(name not in seen, '서비스 컨테이너가 중복됐습니다. 자동 변경하지 않습니다.')
        seen.add(name)
        state = item['State']
        health = state.get('Health') or {}
        row = {'container_id': item['Id'][:12], 'state': state.get('Status'), 'health': health.get('Status'),
               'exit_code': state.get('ExitCode'), 'oom_killed': state.get('OOMKilled', False),
               'restart_count': item.get('RestartCount', 0), 'user': item.get('Config', {}).get('User', ''),
               'state_error': redactor.text(state.get('Error'), 1500)}
        row['recent_health_checks'] = [{'exit_code': entry.get('ExitCode'),
                                       'output': redactor.text(entry.get('Output'), 1200)}
                                      for entry in (health.get('Log') or [])[-2:]]
        if state.get('Status') != 'running' or health.get('Status') != 'healthy':
            try:
                logs = docker.run(['logs', '--tail', '60', item['Id']], timeout=15, combined=True)
                row['recent_log_excerpt'] = excerpt(logs, redactor)
            except DockerFailure as exc:
                row['logs_unavailable'] = redactor.text(exc.detail, 1000)
            except (OSError, subprocess.SubprocessError):
                row['logs_unavailable'] = 'read failed or timed out'
        rows[name] = row
    stores = {}
    for name in STORES:
        try:
            path = safe_path(root / 'data' / name)
            info = path.stat()
            stores[name] = {'exists': True, 'directory': path.is_dir(), 'uid': info.st_uid, 'gid': info.st_gid,
                            'mode': oct(stat.S_IMODE(info.st_mode)),
                            'original_binding': [info.st_dev, info.st_ino] == settings.get('storage', {}).get(name)}
        except (ValueError, OSError):
            stores[name] = {'exists_or_accessible': False}
    result = {'stage': 'O01-Langfuse-diagnose', 'result': 'diagnosed',
              'scope': 'read-only startup snapshot; not acceptance', 'services': rows,
              'not_healthy': [name for name, row in rows.items() if row['state'] != 'running' or row['health'] != 'healthy'],
              'storage_directories': stores, 'secrets_redacted': True, 'changes_made': False,
              'inference_executed': False, 'restart_executed': False}
    if failure is not None:
        result['compose_failure'] = {'exit_code': failure.returncode, 'detail': excerpt(failure.detail, redactor)}
    else:
        result['original_compose_output_available'] = False
    return result
