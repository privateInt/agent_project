#!/usr/bin/env python3
"""Actual n8n authentication checks in NEW isolated SQLite; no platform inference.

AGENT_TEST_N8N_BIN must point to an installed pinned n8n 2.40.5 executable.
This test starts/stops its own server; it cannot target a running instance.
"""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
from api_access import Client, HTTPFailure, connect, diagnose, read_private, verify_key


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); return sock.getsockname()[1]


def checks(url, folder):
    client = Client(url)
    path = folder / 'secrets/api-key.json'
    before = diagnose(client, path)
    assert before['setup_required'] and before['password_min_length'] == 16
    assert not path.parent.exists()
    owner = {'email': 'auth-check@example.invalid', 'firstName': '승훈', 'lastName': '신',
             'password': 'Aa1' + secrets.token_hex(16)}
    for password, rule in [('Aa123456', 'minimum'), ('a1' + 'b' * 14, 'password_uppercase'),
                           ('Ab' + 'c' * 14, 'password_number')]:
        try: client.request('POST', '/rest/owner/setup', {**owner, 'password': password})
        except HTTPFailure as error:
            assert (error.status, error.stage, error.field, error.rule) == (400, 'owner_setup', 'password', rule)
            assert password not in str(error)
        else: raise AssertionError('invalid password accepted')
        assert diagnose(client, path)['setup_required']
    path.parent.mkdir(mode=0o700)
    # Account setup succeeds, then a real validation error rejects the API key.
    class InvalidKey:
        def __init__(self): self.url = client.url
        def request(self, method, route, body=None):
            if route == '/rest/api-keys': body = {**body, 'label': 'x' * 51}
            return client.request(method, route, body)
    try: connect(InvalidKey(), path, setup=owner, minimum=16)
    except HTTPFailure as error:
        assert (error.status, error.stage, error.field, error.rule) == (400, 'api_key_create', 'label', 'maximum')
    else: raise AssertionError('invalid API key label accepted')
    after = diagnose(Client(url), path)
    assert not after['setup_required'] and after['next_step'] == 'login_existing_account'
    assert not path.exists() and not path.with_suffix('.pending.json').exists()
    login = {'emailOrLdapLoginId': owner['email'], 'password': owner['password']}
    assert connect(Client(url), path, login=login)['reused'] is False
    saved = path.read_bytes()
    assert connect(Client(url), path)['reused'] is True
    assert not verify_key(read_private(path)).items('/api/v1/workflows')
    assert diagnose(Client(url), path)['next_step'] == 'reuse_key'
    assert path.read_bytes() == saved
    print(json.dumps({'result': 'passed', 'stage': 'A07-n8n-accessfix',
                      'scope': 'real n8n 2.40.5 HTTP / isolated SQLite / no Agent or model calls',
                      'checks': ['read-only diagnosis creates no local files',
                                 'server minimum 16 and three password validation rules',
                                 'Korean first and last names accepted',
                                 'account setup success followed by real API key HTTP 400',
                                 'existing account login resumes after key rejection; no account reset',
                                 'scoped API key creation, verification and unchanged reuse'],
                      'inference_executed': False}, ensure_ascii=False, indent=2))


def main():
    binary = os.environ['AGENT_TEST_N8N_BIN']
    with tempfile.TemporaryDirectory(prefix='n8n-access-check-') as td:
        folder = Path(td); service_port = port()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DB_', 'N8N_', 'NODE_FUNCTION_ALLOW_'))}
        env.update(N8N_USER_FOLDER=str(folder / 'n8n'), DB_TYPE='sqlite', N8N_ENCRYPTION_KEY=secrets.token_hex(32),
                   N8N_PORT=str(service_port), N8N_LISTEN_ADDRESS='127.0.0.1', N8N_RUNNERS_BROKER_PORT=str(port()),
                   N8N_PASSWORD_MIN_LENGTH='16', N8N_SECURE_COOKIE='false', N8N_DIAGNOSTICS_ENABLED='false',
                   N8N_VERSION_NOTIFICATIONS_ENABLED='false', N8N_COMMUNITY_PACKAGES_ENABLED='false',
                   N8N_PERSONALIZATION_ENABLED='false')
        with (folder / 'n8n.log').open('w') as log:
            process = subprocess.Popen([binary, 'start'], env=env, stdout=log, stderr=log)
            try:
                url = 'http://127.0.0.1:' + str(service_port)
                for _ in range(200):
                    try: Client(url).request('GET', '/healthz/readiness'); break
                    except ValueError:
                        if process.poll() is not None: raise RuntimeError('isolated n8n exited')
                        time.sleep(.2)
                else: raise RuntimeError('isolated n8n readiness timed out')
                checks(url, folder)
            finally:
                process.terminate()
                try: process.wait(timeout=12)
                except subprocess.TimeoutExpired: process.kill(); process.wait()


if __name__ == '__main__': main()
