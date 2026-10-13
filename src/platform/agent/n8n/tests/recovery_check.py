#!/usr/bin/env python3
"""Real n8n HTTP + credential export CLI, isolated SQLite, complete Agent HTTP fixture."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from operational_check import Fixture, serve, free_port
import operational as op
from api_access import Client, connect, read_private
import recover
from credential_reader import export_one, EXPORT_ENV
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
from test_acceptance import API as Records


class RecoveryFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.records = Records()
        self.version.update(model=self.records.version['model'], tool_version_id=self.records.tv['tool_version_id'])

    def route(self, method, path, body, headers):
        if method == 'GET' and headers.get('Authorization') == 'Bearer ' + self.token:
            if path == '/api/status': return 200, {'ready': True, 'stage': 'A07', 'schema_version': 5}
            if path == '/api/agent-runs': return 200, {'items': list(self.runs.values())}
            if path.startswith('/api/tool-versions/'): return 200, self.records.tv
            if path.startswith('/api/tool-calls/'):
                r = next(r for r in self.runs.values() if r['tool_call_id'] == path.rsplit('/', 1)[-1])
                return 200, {**self.records.child, 'tool_call_id': r['tool_call_id'],
                             'parent_agent_run_id': r['agent_run_id'], 'request_id': r['request_id']}
        status, value = super().route(method, path, body, headers)
        if method == 'POST' and path == '/api/agent-runs':
            for r in self.runs.values():
                r['created_at'] = '2026-09-25T00:00:00Z'
                if r['state'] == 'failed':
                    r['error'] = {'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize'}
                else:
                    r['result'] = {**copy.deepcopy(self.records.run['result']), 'tool_call_id': r['tool_call_id']}
        if method == 'GET' and path.endswith('/steps'):
            r = self.runs[path.split('/')[-2]]
            value = {'items': [{'step_id': s, 'state': 'failed' if r['state'] == 'failed' and s == 'finalize' else 'succeeded'}
                               for s in recover.check.STEPS]}
        return status, value


def main():
    fixture = RecoveryFixture(); server = serve(fixture)
    api_url = 'http://127.0.0.1:' + str(server.server_port)
    api = recover.check.Reader(api_url, fixture.token)
    with tempfile.TemporaryDirectory(prefix='n8n-recovery-test-') as td:
        folder = Path(td); url = 'http://127.0.0.1:' + str(free_port())
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DB_', 'N8N_', 'NODE_FUNCTION_ALLOW_'))}
        env.update(N8N_USER_FOLDER=str(folder/'n8n'), DB_TYPE='sqlite', N8N_ENCRYPTION_KEY=secrets.token_hex(32),
                   N8N_PORT=url.rsplit(':',1)[-1], N8N_LISTEN_ADDRESS='127.0.0.1', N8N_RUNNERS_BROKER_PORT=str(free_port()),
                   N8N_SECURE_COOKIE='false', N8N_DIAGNOSTICS_ENABLED='false', N8N_VERSION_NOTIFICATIONS_ENABLED='false',
                   N8N_COMMUNITY_PACKAGES_ENABLED='false', N8N_PERSONALIZATION_ENABLED='false',
                   N8N_BLOCK_ENV_ACCESS_IN_NODE='true', NODE_FUNCTION_ALLOW_BUILTIN='crypto', EXECUTIONS_DATA_PRUNE='false')
        token_file = folder/'token'; token_file.write_text(fixture.token); token_file.chmod(0o600)
        key = folder/'api-key.json'; session = folder/'session'
        def command(action, *args, failed=False):
            result = subprocess.run([sys.executable, '-B', str(op.ROOT/'n8n/ops/operational.py'), action,
                '--session-dir', str(session), '--key-file', str(key), '--token-file', str(token_file), *args],
                capture_output=True, text=True, timeout=90)
            assert result.returncode == (2 if failed else 0), (result.stdout, result.stderr)
        with (folder/'n8n.log').open('w+') as log:
            process = subprocess.Popen([os.environ['AGENT_TEST_N8N_BIN'], 'start'], env=env, stdout=log, stderr=log)
            try:
                client = Client(url)
                for _ in range(150):
                    try: client.request('GET', '/healthz/readiness'); break
                    except ValueError:
                        if process.poll() is not None: raise ValueError('n8n stopped')
                        time.sleep(.2)
                else: raise ValueError('n8n not ready')
                connect(client, key, setup={'email':'recovery@example.invalid','firstName':'Recovery','lastName':'Test',
                                            'password':'Aa1'+secrets.token_hex(16)})
                command('prepare', '--agent-version-id', fixture.version['agent_version_id'], '--publish-agent',
                        '--agent-url', api_url, '--container-agent-url', api_url)
                command('run', '--question', 'fixture-failure', failed=True)
                state = read_private(session/'settings.json'); old_request = read_private(session/'request.json')
                client = recover.check.N8nReader(read_private(key))
                executions = client.executions(state['resources']['workflow']['id'])
                assert len(executions) == 1 and executions[0]['status'] == 'error'
                original_wf = client.client.items('/api/v1/workflows')
                original_credentials = client.client.items('/api/v1/credentials')
                session.rename(folder/'preserved-original-session')
                _, baseline = recover.check.collect(api, session, key)
                before_effects = fixture.effects
                export = lambda cid: export_one([os.environ['AGENT_TEST_N8N_BIN']], cid, env={**env, **EXPORT_ENV})
                bundle = recover.collect(api, client, state['workflow_link_id'], executions[0]['id'], fixture.token)
                assert bundle['summary']['error']['code'] == 'ANSWER_REFERENCE_INVALID'
                assert recover.restore(api, client, bundle, session, fixture.token, export) == 'restored'
                restored = read_private(session/'settings.json')
                assert restored['hook_secret'] == state['hook_secret']
                assert read_private(session/'request.json')['body'] == old_request['body']
                originals = {p.name:p.read_bytes() for p in session.iterdir()}
                bundle = recover.collect(api, client, state['workflow_link_id'], executions[0]['id'], fixture.token)
                assert recover.restore(api, client, bundle, session, fixture.token, export) == 'already_restored'
                assert originals == {p.name:p.read_bytes() for p in session.iterdir()}
                assert fixture.effects == before_effects
                assert original_wf == client.client.items('/api/v1/workflows')
                assert original_credentials == client.client.items('/api/v1/credentials')
                report, after = recover.check.collect(api, session, key, baseline=baseline)
                assert recover.check.comparison(baseline, after) == []
                assert report['n8n']['status'] == 'not_captured_in_baseline'
                report, _ = recover.check.collect(api, session, key)
                assert report['n8n']['agent_state'] == 'failed' and not report['n8n']['delivery_verified']
                command('run', failed=True)
                assert fixture.effects == before_effects
                new_request = session/'request-new.json'
                command('run', '--question', 'new acceptance question', '--request-file', str(new_request))
                command('run', '--request-file', str(new_request))
                assert fixture.effects == before_effects + 1
                report, full = recover.check.collect(api, session, key, new_request)
                assert report['n8n']['delivery_verified'] and report['n8n']['replay_verified']
                report, again = recover.check.collect(api, session, key, new_request, baseline=full)
                assert recover.check.comparison(full, again) == []
                print(json.dumps({'result':'passed','scope':'real n8n 2.40.5 HTTP/CLI; isolated SQLite; Agent HTTP fixture',
                                  'effects':fixture.effects,'checks':['failed request reconstruction', 'same workflow and credential IDs/data',
                                  'no inference in recovery', 'idempotent local recovery', 'original Agent-only baseline preserved',
                                  'old failed request replay has no new effect', 'new request delivery/replay has one effect',
                                  'expanded baseline capture/compare']}, indent=2))
            except Exception:
                log.flush(); log.seek(0); print(log.read()[-3000:], file=sys.stderr)
                raise
            finally:
                process.terminate()
                try: process.wait(timeout=12)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
                server.shutdown(); server.server_close()


if __name__ == '__main__': main()
