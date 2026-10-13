import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ops'))
import configure_indexing

spec = importlib.util.spec_from_file_location('live_indexing', Path(__file__).with_name('live_indexing.py'))
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class IndexingTools(unittest.TestCase):
    def retry_fixture(self, directory):
        expected = {'base_url': 'http://127.0.0.1:15100', 'chunking_run_id': 'chunks', 'deployment_id': 'model'}
        index_id = '11111111-1111-4111-8111-111111111111'
        version_id = '22222222-2222-4222-8222-222222222222'
        source = Path(directory) / 'failed.json'
        live.save(source, {'input': expected, 'index_id': index_id, 'index_version_id': version_id, 'build_key': 'old-key'})
        version = {'state': 'failed', 'index_id': index_id, 'index_version_id': version_id,
                   'chunking_run_id': 'chunks', 'embedding_profile': {'deployment_id': 'model'}}
        index = {'index_id': index_id, 'state': 'active', 'active_version_id': '', 'revision': 2}
        return expected, source, Path(directory) / 'retry.json', version, index

    def test_retry_rejects_overwrite_wrong_input_and_nonfailed_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            expected, source, target, version, index = self.retry_fixture(temp)
            before = source.read_bytes()
            with patch.object(live, 'api') as remote:
                with self.assertRaises(ValueError):
                    live.retry_failed_state(expected, source, source)
                with self.assertRaises(ValueError):
                    live.retry_failed_state(dict(expected, deployment_id='other'), source, target)
                remote.assert_not_called()
            for changes in ({'state': 'indexing'}, {'index_id': 'other'}, {'chunking_run_id': 'other'},
                            {'embedding_profile': {'deployment_id': 'other'}}):
                with self.subTest(changes=changes), patch.object(live, 'api', return_value=dict(version, **changes)):
                    with self.assertRaises(ValueError):
                        live.retry_failed_state(expected, source, target)
                    self.assertFalse(target.exists())
            with patch.object(live, 'api', side_effect=[version, dict(index, active_version_id='active')]):
                with self.assertRaises(ValueError):
                    live.retry_failed_state(expected, source, target)
            live.save(target, {'input': expected, 'index_id': index['index_id'], 'retry_of_version': 'different'})
            old_target = target.read_bytes()
            with patch.object(live, 'api') as remote:
                with self.assertRaises(ValueError):
                    live.retry_failed_state(expected, source, target)
                remote.assert_not_called()
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(target.read_bytes(), old_target)

    def test_retry_replays_lost_build_reply_without_new_index_or_mutating_failed_state(self):
        with tempfile.TemporaryDirectory() as temp:
            expected, source, target, failed, index = self.retry_fixture(temp)
            before = source.read_bytes()
            version_id = '33333333-3333-4333-8333-333333333333'
            ready = dict(failed, state='ready', index_version_id=version_id, indexed_count=48,
                         chunk_count=48, reference_state='held', error_code='', error_message='',
                         chunk_set_sha256='hash', input_spec={'chunker': {'id': 'structure_v1'}},
                         canonical_ids=['document'], embedding_profile={'deployment_id': 'model',
                         'dimension': 1024, 'embedding_space_id': 'space', 'runtime_container_id': 'runtime'})
            writes, build_requests = [], []

            def remote(base, path, method='GET', body=None, key=None):
                self.assertEqual(base, expected['base_url'])
                if method == 'POST':
                    writes.append(path)
                    if path.endswith('/builds'):
                        build_requests.append((key, body.copy()))
                        self.assertEqual(json.loads(target.read_text())['build_key'], key)
                        if len(build_requests) == 1:
                            raise ValueError('reply lost after accepted build')
                        return {'index_version_id': version_id}
                    if path.endswith('/activate'):
                        self.assertEqual(body['index_version_id'], version_id)
                        self.assertEqual(body['expected_active_version_id'], '')
                        index['active_version_id'] = version_id
                        return index
                    self.fail('unexpected write: ' + path)
                if path == '/api/index-versions/' + failed['index_version_id']:
                    return failed
                if path == '/api/index-versions/' + version_id:
                    return ready
                if path == '/api/indexes/' + index['index_id']:
                    return index
                self.fail('unexpected read: ' + path)

            argv = ['live_indexing.py', '--chunking-run-id', 'chunks', '--deployment-id', 'model',
                    '--retry-failed-from', str(source), '--state-file', str(target)]
            with patch.object(sys, 'argv', argv), patch.object(live, 'api', side_effect=remote), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'reply lost'):
                    live.main()
                live.main()
                live.main()
            self.assertEqual(len(build_requests), 2)
            self.assertEqual(build_requests[0], build_requests[1])
            self.assertNotEqual(build_requests[0][0], 'old-key')
            self.assertEqual(build_requests[0][1], {'chunking_run_id': 'chunks', 'embedding_deployment_id': 'model'})
            self.assertEqual(writes, ['/api/indexes/' + index['index_id'] + '/builds'] * 2
                             + ['/api/indexes/' + index['index_id'] + '/activate'])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(target.read_text())['receipt']['status'], 'passed')

    def test_retry_does_not_replace_another_version_activated_during_build(self):
        with tempfile.TemporaryDirectory() as temp:
            expected, source, target, failed, index = self.retry_fixture(temp)
            new_id = '33333333-3333-4333-8333-333333333333'
            ready = dict(failed, state='ready', index_version_id=new_id, indexed_count=48,
                         chunk_count=48, reference_state='held', error_code='',
                         embedding_profile={'deployment_id': 'model', 'dimension': 1024})
            replies = [failed, index, {'index_version_id': new_id}, ready,
                       dict(index, active_version_id='another-active-version')]
            argv = ['live_indexing.py', '--chunking-run-id', 'chunks', '--deployment-id', 'model',
                    '--retry-failed-from', str(source), '--state-file', str(target)]
            with patch.object(sys, 'argv', argv), patch.object(live, 'api', side_effect=replies) as remote, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'Another version became active'):
                    live.main()
                self.assertEqual(remote.call_count, 5)
            self.assertNotIn('activation_request', json.loads(target.read_text()))

    def test_connection_is_tls_and_preserves_credential_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            ca = Path(temp) / 'public.pem'
            ca.write_bytes(b'public CA')
            config = {'services': {'opensearch-node1': {'environment': {'OPENSEARCH_INITIAL_ADMIN_PASSWORD': "literal$pass'word"}, 'volumes': [{'type': 'bind', 'source': str(ca), 'target': '/usr/share/opensearch/config/certs/rootCA.pem'}]}}, 'networks': {'opensearch-net': {'name': 'opensearch-net'}}}
            with patch.object(configure_indexing.ssl, 'create_default_context') as validate:
                result = configure_indexing.settings(config)
                validate.assert_called_once_with(cadata='public CA')
            self.assertEqual(result['KNOWLEDGE_OPENSEARCH_ENDPOINT'], 'https://opensearch-node1:9200')
            self.assertEqual(result['KNOWLEDGE_OPENSEARCH_PASSWORD'], "literal$pass'word")
            ca.write_bytes(b'-----BEGIN PRIVATE KEY-----')
            with self.assertRaises(ValueError):
                configure_indexing.settings(config)

    def test_missing_connection_and_injection_are_rejected(self):
        for password in ('', 'line\nsecret', 'nul\x00secret', None):
            with self.assertRaises(ValueError):
                configure_indexing.settings({'services': {'opensearch-node1': {'environment': {'OPENSEARCH_INITIAL_ADMIN_PASSWORD': password}}}})

    def test_replay_journal_is_exact_and_private(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'receipt.json'
            value = {'input': {'deployment_id': 'id'}, 'build_key': 'same-key', 'activation_request': {'expected_revision': 7}}
            live.save(path, value)
            self.assertEqual(json.loads(path.read_text()), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_name('receipt.json.tmp').exists())
            value['index_version_id'] = 'version'
            live.save(path, value)
            self.assertEqual(json.loads(path.read_text()), value)


if __name__ == '__main__':
    unittest.main()
