"""Recovery must preserve identity, failures, private keys and existing baselines."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'n8n/ops'))
import recover
import operational as op
from operational_workflow import build
from credential_reader import export_one, read_credential
from test_acceptance import API, uid, output

TOKEN = 'private-agent-token-0123456789'
SECRET = 'private-hook-secret-01234567890123456789'


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.sid = uid(16)
        self.path = 'agent-question-' + self.sid
        self.wf = build(uid(2), 'http://agent-gateway:8000', 'agent1', 'hook1', self.path, 'QA · ' + self.sid)
        self.wf.update(id='flow1', active=True, versionId='v1', activeVersionId='v1')
        self.wf['activeVersion'] = copy.deepcopy(self.wf)
        caller = {'system': 'n8n', 'workflow_id': 'flow1', 'execution_id': 'request:' + uid(15), 'node_id': 'agent-call'}
        self.api.run['caller_context'] = caller
        envelope = {'invocation_key': uid(15), 'body': {'agent_version_id': uid(2), 'mode': 'run',
                    'input': self.api.run['input'], 'caller_context': caller}}
        prepared = {'base_url': 'http://agent-gateway:8000', 'request': envelope}
        self.execution = {'id': '1', 'workflowId': 'flow1', 'status': 'success', 'workflowData': copy.deepcopy(self.wf),
                          'data': {'resultData': {'runData': {'Prepare request': output(prepared),
                            'Run pointer': output({**prepared, 'agent_run_id': uid(1)})}}}}
        self.link = {'workflow_link_id': uid(17), 'workflow_id': 'flow1', 'agent_version_id': uid(2),
                     'display_name': 'QA', 'instance_url': 'http://127.0.0.1:16200', 'enabled': True}
        self.api.routes['/api/workflow-links/' + uid(17)] = self.link
        outer = self
        class Client:
            url = 'http://127.0.0.1:16200'
            def request(self, method, path):
                assert method == 'GET'
                return copy.deepcopy(outer.wf if path == '/api/v1/workflows/flow1' else outer.execution)
        self.client = Client()
        self.creds = {'agent1': {'id': 'agent1', 'type': 'httpHeaderAuth', 'name': 'Agent API · ' + self.sid,
                                'data': {'name': 'Authorization', 'value': 'Bearer ' + TOKEN}},
                      'hook1': {'id': 'hook1', 'type': 'jwtAuth', 'name': 'Agent workflow entry · ' + self.sid,
                               'data': {'keyType': 'passphrase', 'algorithm': 'HS256', 'secret': SECRET}}}

    def collect(self):
        return recover.collect(self.api, self.client, uid(17), '1', TOKEN)

    def restore(self, folder, bundle=None, reader=None):
        return recover.restore(self.api, self.client, bundle or self.collect(), folder, TOKEN,
                               reader or (lambda cid: copy.deepcopy(self.creds[cid])))

    def test_restore_and_repeated_restore_preserve_exact_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'session'
            self.assertEqual(self.restore(p), 'restored')
            self.assertEqual(p.stat().st_mode & 0o777, 0o700)
            contents = {f.name: f.read_bytes() for f in p.iterdir()}
            for f in p.iterdir(): self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            state = json.loads(contents['settings.json'])
            self.assertEqual(state['hook_secret'], SECRET)
            self.assertEqual(state['resources']['workflow']['id'], 'flow1')
            self.assertEqual(json.loads(contents['request.json'])['body']['request_id'], uid(15))
            self.assertEqual(self.restore(p, reader=lambda _: self.fail('must not export again')), 'already_restored')
            self.assertEqual(contents, {f.name: f.read_bytes() for f in p.iterdir()})

    def test_existing_empty_or_unrelated_directory_not_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'session'; p.mkdir(mode=0o700)
            with self.assertRaises((ValueError, OSError)):
                self.restore(p)
            self.assertEqual(list(p.iterdir()), [])

    def test_symlink_not_followed(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / 'original'; target.mkdir()
            p = Path(td) / 'session'; p.symlink_to(target)
            with self.assertRaisesRegex(ValueError, 'SESSION_SYMLINK'): self.restore(p)
            self.assertEqual(list(target.iterdir()), [])

    def test_failed_agent_stays_failed(self):
        self.api.run.update(state='failed', error={'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize'})
        self.api.run.pop('result'); self.api.steps[-1]['state'] = 'failed'; self.execution['status'] = 'error'
        bundle = self.collect()
        self.assertEqual(bundle['summary']['agent_state'], 'failed')
        self.assertEqual(bundle['summary']['error']['code'], 'ANSWER_REFERENCE_INVALID')
        text = json.dumps(bundle['summary'])
        for secret in (TOKEN, SECRET, 'PRIVATE_QUESTION'): self.assertNotIn(secret, text)

    def test_changed_workflow_or_published_or_historical_nodes_rejected(self):
        original = copy.deepcopy(self.wf)
        for target in (self.wf, self.wf['activeVersion'], self.execution['workflowData']):
            old = target['nodes'][0]['parameters']['path']; target['nodes'][0]['parameters']['path'] = 'changed'
            with self.assertRaises(ValueError): self.collect()
            target['nodes'][0]['parameters']['path'] = old
        self.assertEqual(original, self.wf)

    def test_changed_logical_request_or_caller_rejected(self):
        self.api.run['invocation_key'] = uid(99)
        with self.assertRaisesRegex(ValueError, 'AGENT_EXECUTION_BINDING_CHANGED'): self.collect()

    def test_only_exact_n8n_snapshot_defaults_are_accepted(self):
        expected = op.desired(self.collect()['state'])
        actual = copy.deepcopy(expected)
        node = next(n for n in actual['nodes'] if n['name'] == 'Question settings')
        node['parameters'].update(mode='runOnceForAllItems', language='javaScript', notice='')
        self.assertTrue(recover.historical_matches(actual, expected))
        node['parameters']['language'] = 'python'
        self.assertFalse(recover.historical_matches(actual, expected))
        actual = copy.deepcopy(expected)
        node = next(n for n in actual['nodes'] if n['name'] == 'Read Agent run')
        response = node['parameters']['options']['response']['response']
        response['neverError'] = False
        self.assertTrue(recover.historical_matches(actual, expected))
        for wrong in (True, 0, 'false'):
            response['neverError'] = wrong
            self.assertFalse(recover.historical_matches(actual, expected))

    def test_pruned_execution_cannot_reconstruct_request(self):
        self.execution['data'] = {}
        with self.assertRaisesRegex(ValueError, 'EXECUTION_DATA_NOT_RETAINED'): self.collect()

    def test_different_instance_credential_or_agent_token_rejected_before_write(self):
        for slot, field, val in [('hook1', 'id', 'other'), ('hook1', 'name', 'other'), ('agent1', 'data', {'name':'Authorization','value':'wrong'})]:
            with tempfile.TemporaryDirectory() as td:
                old = copy.deepcopy(self.creds[slot]); self.creds[slot][field] = val
                p = Path(td) / 'session'
                with self.assertRaises(ValueError): self.restore(p)
                self.assertFalse(p.exists()); self.creds[slot] = old

    def test_remote_changes_during_credential_read_leave_no_session(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'session'
            def reader(cid):
                self.wf['versionId'] = 'changed'
                return self.creds[cid]
            with self.assertRaisesRegex(ValueError, 'REMOTE_RECORD_CHANGED'): self.restore(p, reader=reader)
            self.assertFalse(p.exists())

    def test_original_agent_only_baseline_stays_agent_only(self):
        _, before = recover.check.collect(self.api, Path('/missing-session'), Path('/unused'))
        with patch.object(recover.check, 'read_session', side_effect=AssertionError('baseline excludes n8n')):
            report, after = recover.check.collect(self.api, Path('/recovered-session'), Path('/unused'), baseline=before)
        self.assertEqual(recover.check.comparison(before, after), [])
        self.assertEqual(report['n8n']['status'], 'not_captured_in_baseline')
        self.assertFalse(report['n8n']['delivery_verified'])

    def test_cli_exact_credential_no_plaintext_in_errors_or_command(self):
        secret = json.dumps([self.creds['hook1']]).encode()
        with patch('credential_reader.subprocess.run', return_value=subprocess.CompletedProcess([], 0, secret, b'')) as run:
            self.assertEqual(read_credential('hook1')['data']['secret'], SECRET)
            cmd = run.call_args.args[0]
            self.assertIn('--id=hook1', cmd); self.assertNotIn('--all', cmd); self.assertNotIn(SECRET, ' '.join(cmd))
            self.assertIn('N8N_LOG_OUTPUT=console', cmd)
        with patch('credential_reader.subprocess.run', return_value=subprocess.CompletedProcess([], 1, secret, secret)):
            with self.assertRaises(ValueError) as caught: export_one(['n8n'], 'hook1')
            self.assertNotIn(SECRET, str(caught.exception))

    def test_cli_structured_log_extracts_only_one_exact_credential(self):
        raw = b'startup\n' + json.dumps({'level': 'info', 'message': json.dumps([self.creds['hook1']])}).encode() + b'\n'
        with patch('credential_reader.subprocess.run', return_value=subprocess.CompletedProcess([], 0, raw, b'')):
            self.assertEqual(export_one(['n8n'], 'hook1')['data']['secret'], SECRET)
        with patch('credential_reader.subprocess.run', return_value=subprocess.CompletedProcess([], 0, raw + raw, b'')):
            with self.assertRaises(ValueError): export_one(['n8n'], 'hook1')

    def test_recovered_session_prepare_cannot_duplicate_link(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'session'; self.restore(p)
            with self.assertRaisesRegex(ValueError, '재등록하지 않습니다'):
                op.prepare(None, p, None, None, TOKEN)


if __name__ == '__main__': unittest.main()
