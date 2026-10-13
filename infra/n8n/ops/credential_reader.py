"""Read one credential through the installed n8n admin CLI, never echo its data."""
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
# Console output is captured in memory by the caller. Force console-only JSON
# so no plaintext credential can be written by a configured file log transport.
EXPORT_ENV = {'N8N_LOG_LEVEL': 'info', 'N8N_LOG_FORMAT': 'json', 'N8N_LOG_OUTPUT': 'console'}


def export_one(command, credential_id, *, env=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', credential_id):
        raise ValueError('CREDENTIAL_ID_INVALID')
    try:
        result = subprocess.run([*command, 'export:credentials', '--id=' + credential_id,
                                 '--decrypted'],
                                capture_output=True, timeout=60, check=False, env=env)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise ValueError('CREDENTIAL_EXPORT_FAILED')
        found = []
        for line in result.stdout.splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict) and isinstance(value.get('message'), str):
                    value = json.loads(value['message'])
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) and value[0].get('id') == credential_id:
                found.append(value[0])
        if len(found) != 1:
            raise ValueError('CREDENTIAL_EXPORT_MISMATCH')
        return found[0]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        # stdout/stderr may contain plaintext credentials. Never include either in an error.
        raise ValueError('CREDENTIAL_EXPORT_FAILED: check local n8n container access/version') from None


def read_credential(credential_id):
    command = ['docker', 'compose', '--project-directory', str(ROOT), '-f', str(ROOT / 'docker-compose.yml'), 'exec', '-T']
    for key, value in EXPORT_ENV.items():
        command.extend(['-e', key + '=' + value])
    command.extend(['n8n', 'n8n'])
    return export_one(command, credential_id)
