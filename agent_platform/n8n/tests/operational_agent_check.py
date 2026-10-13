#!/usr/bin/env python3
"""Called by TestAgentEndToEnd: real n8n HTTP + real Agent services, model HTTP fixture."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from operational_check import free_port
from api_access import Client, connect, read_private

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix='agent-operational-wire-') as td:
        folder = Path(td); port = free_port(); url = 'http://127.0.0.1:' + str(port)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DB_', 'N8N_', 'NODE_FUNCTION_ALLOW_'))}
        env.update(N8N_USER_FOLDER=str(folder / 'n8n'), DB_TYPE='sqlite', N8N_ENCRYPTION_KEY=secrets.token_hex(32),
                   N8N_PORT=str(port), N8N_LISTEN_ADDRESS='127.0.0.1', N8N_RUNNERS_BROKER_PORT=str(free_port()),
                   N8N_SECURE_COOKIE='false', N8N_DIAGNOSTICS_ENABLED='false', N8N_VERSION_NOTIFICATIONS_ENABLED='false',
                   N8N_COMMUNITY_PACKAGES_ENABLED='false', N8N_PERSONALIZATION_ENABLED='false',
                   N8N_BLOCK_ENV_ACCESS_IN_NODE='true', NODE_FUNCTION_ALLOW_BUILTIN='crypto', EXECUTIONS_DATA_PRUNE='false')
        with (folder / 'n8n.log').open('w+') as log:
            process = subprocess.Popen([os.environ['AGENT_TEST_N8N_BIN'], 'start'], env=env, stdout=log, stderr=log)
            try:
                client = Client(url)
                for _ in range(150):
                    try: client.request('GET', '/healthz/readiness'); break
                    except ValueError:
                        if process.poll() is not None: raise ValueError('n8n stopped')
                        time.sleep(.2)
                else: raise ValueError('n8n not ready')
                key = folder / 'api-key.json'
                connect(client, key, setup={'email': 'wire@example.invalid', 'firstName': 'Wire', 'lastName': 'Check', 'password': 'Aa1' + secrets.token_hex(16)})
                session = folder / 'session'
                def command(action, *args):
                    cmd = [sys.executable, str(ROOT / 'n8n/ops/operational.py'), action,
                           '--session-dir', str(session), '--key-file', str(key),
                           '--token-file', os.environ['A07_AGENT_TOKEN_FILE'], *args]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if result.returncode: raise ValueError(result.stdout + result.stderr)
                for _ in range(2):
                    command('prepare', '--agent-version-id', os.environ['A07_AGENT_VERSION'],
                            '--agent-url', os.environ['A07_AGENT_URL'], '--container-agent-url', os.environ['A07_AGENT_URL'])
                command('run', '--question', '  자연어 질문  ')
                first = read_private(session / 'request.result.json')
                command('run', '--question', '  자연어 질문  ')
                second = read_private(session / 'request.result.json')
                assert first['run'] == second['run'] and second['summary']['replay_verified']
                if script := os.environ.get('AGENT_TEST_A09_SCRIPT'):
                    originals = {p.name: p.read_bytes() for p in session.iterdir() if p.is_file()}
                    snapshot = folder / 'acceptance.json'
                    for action in ('capture', 'compare', 'capture'):
                        result = subprocess.run([sys.executable, script, action, '--url', os.environ['A07_AGENT_URL'],
                            '--token-file', os.environ['A07_AGENT_TOKEN_FILE'], '--session-dir', str(session),
                            '--key-file', str(key), '--baseline-file', str(snapshot)], capture_output=True, text=True, timeout=60)
                        if result.returncode:
                            raise ValueError('A09 integration: ' + result.stdout + result.stderr)
                        report = json.loads(result.stdout)
                        assert report['n8n']['delivery_verified'] and report['n8n']['replay_verified']
                        assert report['result'] == ('captured' if action == 'capture' and not (folder / 'seen').exists() else 'preserved')
                        (folder / 'seen').touch()
                        assert report['mvp_acceptance'] == 'incomplete' and not report['inference_executed']
                    assert originals == {p.name: p.read_bytes() for p in session.iterdir() if p.is_file()}
                    print('A09: real n8n delivery/replay, capture/compare/reused capture and untouched operational session verified.')
                command('lookup'); command('status'); command('disable'); command('lookup')
                assert read_private(session / 'request.lookup.json')['result'] == 'passed'
                print(json.dumps({'result': 'passed', 'scope': 'n8n HTTP -> Agent Go/Python/SQL -> Model/Knowledge HTTP fixtures',
                                  'checks': ['CLI prepare twice', 'CLI run twice; exact Agent record replay', 'GET-only lookup/status', 'unpublish and preserved lookup']}, indent=2))
            finally:
                process.terminate()
                try: process.wait(timeout=12)
                except subprocess.TimeoutExpired: process.kill(); process.wait()


if __name__ == '__main__': main()
