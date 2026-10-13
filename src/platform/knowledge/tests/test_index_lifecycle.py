import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
import uuid
from unittest.mock import patch

import live_index_lifecycle as tool


def ident():
    return str(uuid.uuid4())


class World:
    def __init__(self):
        self.index_id, self.version_id, self.canonical, self.run_id, self.deployment = [ident() for _ in range(5)]
        self.spec = {'documents': [{'canonical_id': self.canonical, 'canonical_sha256': 'a' * 64}], 'chunker': {'id': 'structure_v1'}}
        self.chunks = [{'canonical_id': self.canonical, 'text': 'kept', 'source_spans': [{'block_id': 'b1'}]}]
        self.indexes = {self.index_id: {'index_id': self.index_id, 'name': 'baseline', 'embedding_deployment_id': self.deployment,
                                     'state': 'active', 'active_version_id': self.version_id, 'revision': 5}}
        self.versions = {self.version_id: self.new_version(self.version_id, self.index_id)}
        self.calls, self.replays, self.lost = [], {}, set()
        self.loss = None
        self.observe_failure = None
        self.observations = []
        self.hook = None

    def new_version(self, vid, iid):
        return {'index_version_id': vid, 'index_id': iid, 'state': 'ready', 'reference_state': 'held',
                'chunking_run_id': self.run_id, 'chunk_set_sha256': 'b' * 64, 'input_spec': copy.deepcopy(self.spec),
                'canonical_ids': [self.canonical], 'embedding_profile': {'deployment_id': self.deployment},
                'chunk_count': 1, 'indexed_count': 1}

    def __call__(self, path, method='GET', body=None, key=None):
        self.calls.append((path, method, copy.deepcopy(body), key))
        result, tag = None, None
        if method == 'GET':
            if '/versions?' in path:
                iid = path.split('/')[3]
                result = {'items': [v for v in self.versions.values() if v['index_id'] == iid], 'next_cursor': ''}
            elif '/chunks?' in path:
                result = {'chunking_run_id': self.run_id, 'total': len(self.chunks), 'items': self.chunks, 'next_cursor': ''}
            elif path.startswith('/api/chunking-runs/'):
                result = {'chunking_run_id': self.run_id, 'spec': self.spec, 'result_registered': True,
                          'result_sha256': 'b' * 64, 'chunk_count': len(self.chunks)}
            elif path.startswith('/api/index-versions/'):
                result = self.versions[path.rsplit('/', 1)[1]]
            else:
                result = self.indexes[path.rsplit('/', 1)[1]]
        elif key in self.replays:
            result = self.replays[key]
        elif path == '/api/indexes':
            tag = 'create'
            iid = ident()
            result = self.indexes[iid] = {'index_id': iid, **body, 'state': 'active', 'active_version_id': '', 'revision': 1}
        elif path.endswith('/builds'):
            iid = path.split('/')[3]
            tag = 'build-' + str(len([v for v in self.versions.values() if v['index_id'] == iid]) + 1)
            vid = ident()
            result = self.versions[vid] = self.new_version(vid, iid)
            self.indexes[iid]['revision'] += 1
        elif path.endswith('/activate'):
            tag = 'activate'; idx = self.indexes[path.split('/')[3]]
            if idx['revision'] != body['expected_revision'] or idx['active_version_id'] != body['expected_active_version_id']:
                raise tool.APIError(409, 'INDEX_ACTIVATION_CONFLICT')
            idx['active_version_id'] = body['index_version_id']; idx['revision'] += 1; result = idx
        elif method == 'DELETE' and path.startswith('/api/index-versions/'):
            v = self.versions[path.rsplit('/', 1)[1]]; idx = self.indexes[v['index_id']]
            if idx['active_version_id'] == v['index_version_id']:
                raise tool.APIError(409, 'ACTIVE_VERSION')
            tag = 'delete-version'; v['state'] = 'deleted'; v['reference_state'] = 'released'; idx['revision'] += 1; result = v
        elif method == 'DELETE':
            tag = 'delete-index'; idx = self.indexes[path.rsplit('/', 1)[1]]
            if idx['revision'] != body['expected_revision'] or idx['active_version_id'] != body['expected_active_version_id']:
                raise tool.APIError(409, 'INDEX_DELETE_CONFLICT')
            idx.update(state='deleted', active_version_id='', revision=idx['revision'] + 1)
            for v in self.versions.values():
                if v['index_id'] == idx['index_id']:
                    v.update(state='deleted', reference_state='released')
            result = idx
        else:
            raise AssertionError((method, path))
        if key and method != 'GET':
            self.replays[key] = copy.deepcopy(result)
        if tag is not None and tag == self.loss and tag not in self.lost:
            self.lost.add(tag)
            raise ValueError('ack lost')
        if self.hook:
            self.hook(tag)
        return copy.deepcopy(result)

    def observe(self, versions):
        self.observations.append(copy.deepcopy(versions))
        if len(versions) == self.observe_failure:
            raise ValueError('physical/reference observation differs')
        for v in versions:
            assert self.versions[v['index_version_id']]['reference_state'] == v['reference_state']
        return {'status': 'passed', 'versions': len(versions)}

    def mutations(self):
        return [c for c in self.calls if c[1] != 'GET']


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.json'
        self.world = World()
        self.config = {'base_url': 'http://local.test', 'baseline_version_id': self.world.version_id,
                       'canonical_id': self.world.canonical, 'max_chunks': 8}

    def run_check(self, observer=None):
        check = tool.Lifecycle(self.world, observer or self.world.observe, self.path, self.config, timeout=0)
        with contextlib.redirect_stdout(io.StringIO()):
            return check.run()

    def test_full_sequence_preserves_baseline_and_replay_is_read_only(self):
        initial = copy.deepcopy((self.world.indexes[self.world.index_id], self.world.versions[self.world.version_id]))
        receipt = self.run_check()
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(initial, (self.world.indexes[self.world.index_id], self.world.versions[self.world.version_id]))
        count = len(self.world.mutations())
        self.run_check()
        self.assertEqual(len(self.world.mutations()), count)
        self.assertEqual(len(self.world.indexes), 2)
        self.assertEqual(len(self.world.versions), 3)
        self.assertTrue(all(self.world.index_id not in c[0] and self.world.version_id not in c[0] for c in self.world.mutations()))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_lost_ack_resumes_each_mutation_without_duplicate_resources(self):
        for loss in ['create', 'build-1', 'activate', 'build-2', 'delete-version', 'delete-index']:
            with self.subTest(loss=loss):
                self.path.unlink(missing_ok=True)
                self.world = World(); self.world.loss = loss
                self.config.update(baseline_version_id=self.world.version_id, canonical_id=self.world.canonical)
                with self.assertRaisesRegex(ValueError, 'ack lost'):
                    self.run_check()
                self.run_check()
                self.assertEqual(len(self.world.indexes), 2)
                self.assertEqual(len(self.world.versions), 3)

    def test_input_change_and_unrelated_state_refused_without_calls(self):
        self.world.loss = 'create'
        with self.assertRaises(ValueError): self.run_check()
        self.config['canonical_id'] = ident()
        before = len(self.world.calls)
        with self.assertRaisesRegex(ValueError, 'different input'): self.run_check()
        self.assertEqual(len(self.world.calls), before)

    def test_chunk_budget_and_observer_preflight_stop_before_mutations(self):
        self.config['max_chunks'] = 0
        with self.assertRaisesRegex(ValueError, 'maximum'): self.run_check()
        self.assertEqual(self.world.mutations(), [])
        self.config['max_chunks'] = 8
        self.world.observe_failure = 1
        with self.assertRaisesRegex(ValueError, 'observation'): self.run_check()
        self.assertEqual(self.world.mutations(), [])

    def test_storage_failure_preserves_phase_and_resumes(self):
        self.world.observe_failure = 3
        with self.assertRaisesRegex(ValueError, 'observation'): self.run_check()
        self.assertEqual(json.loads(self.path.read_text())['phase'], 2)
        self.assertFalse(any(c[1] == 'DELETE' and c[0].startswith('/api/indexes/') for c in self.world.calls))
        self.world.observe_failure = None
        self.run_check()
        self.assertEqual(len(self.world.versions), 3)

    def test_unrecorded_version_blocks_whole_index_deletion(self):
        self.world.loss = 'delete-version'
        with self.assertRaises(ValueError): self.run_check()
        state = json.loads(self.path.read_text())
        vid = ident(); self.world.versions[vid] = self.world.new_version(vid, state['index_id'])
        with self.assertRaisesRegex(ValueError, 'Unrecorded'): self.run_check()
        self.assertFalse(any(c[1] == 'DELETE' and c[0].startswith('/api/indexes/') for c in self.world.calls))

    def test_baseline_or_test_ownership_changes_stop_before_new_mutations(self):
        self.world.loss = 'build-2'
        with self.assertRaises(ValueError): self.run_check()
        before = len(self.world.mutations())
        self.world.indexes[self.world.index_id]['revision'] += 1
        with self.assertRaisesRegex(ValueError, 'Baseline changed'): self.run_check()
        self.assertEqual(len(self.world.mutations()), before)
        self.world.indexes[self.world.index_id]['revision'] -= 1
        state = json.loads(self.path.read_text()); self.world.indexes[state['index_id']]['name'] = 'user index'
        with self.assertRaisesRegex(ValueError, 'ownership'): self.run_check()
        self.assertEqual(len(self.world.mutations()), before)

    def test_changed_chunk_content_cannot_be_reported_as_passed(self):
        self.world.loss = 'delete-index'
        with self.assertRaises(ValueError): self.run_check()
        self.world.chunks[0]['text'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'ChunkSet changed'): self.run_check()
        self.assertNotIn('receipt', json.loads(self.path.read_text()))

    def test_wait_retries_deleting_without_marking_released(self):
        values = iter([{'state': 'deleting', 'reference_state': 'held'}, {'state': 'deleted', 'reference_state': 'released', 'index_version_id': ident()}])
        check = tool.Lifecycle(self.world, self.world.observe, self.path, self.config, timeout=5, sleep=lambda _: None)
        self.assertEqual(check.wait(lambda: next(values), 'deleted')['reference_state'], 'released')
        with self.assertRaisesRegex(ValueError, 'still holds'):
            check.wait(lambda: {'state': 'deleted', 'reference_state': 'held', 'index_version_id': ident()}, 'deleted')

    def test_client_rejects_redirect_and_invalid_origins(self):
        with self.assertRaisesRegex(ValueError, 'Redirect'): tool.NoRedirect().redirect_request(None)
        for url in ['http://user:pass@host', 'file:///tmp/a', 'http://host/api', 'http://host/?x=1']:
            with self.assertRaises(ValueError): tool.Client(url)

    def test_real_http_contract_and_redirect_refusal(self):
        world = self.world
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def handle_request(self):
                if self.path == '/api/redirect':
                    self.send_response(307)
                    self.send_header('Location', '/api/indexes')
                    self.end_headers()
                    return
                size = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(size)) if size else None
                try:
                    out = world(self.path, self.command, body, self.headers.get('Idempotency-Key'))
                    status = 200
                except tool.APIError as e:
                    status, out = e.status, {'error': {'code': 'FailedPrecondition', 'message': e.code + ': rejected'}}
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(tool.encoded(out))
            do_GET = do_POST = do_DELETE = handle_request
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            api = tool.Client('http://127.0.0.1:' + str(server.server_port))
            with contextlib.redirect_stdout(io.StringIO()):
                result = tool.Lifecycle(api, world.observe, self.path, self.config, timeout=2).run()
            self.assertEqual(result['status'], 'passed')
            before = len(world.mutations())
            with self.assertRaisesRegex(ValueError, 'Redirect'):
                api('/api/redirect', 'POST', {'name': 'never delivered'})
            self.assertEqual(len(world.mutations()), before)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_docker_observer_only_forwards_required_env_and_uses_readonly_network(self):
        inspected = [{'Id': 'container-id', 'State': {'Running': True}, 'Config': {'Env':
                      ['OPENSEARCH_ENDPOINT=https://os:9200', 'OPENSEARCH_USERNAME=admin', 'OPENSEARCH_PASSWORD=private',
                       'OPENSEARCH_CA_BASE64=cert', 'POSTGRES_URL=not-forwarded']}}]
        def fake_run(command, **kw):
            if command[:2] == ['docker', 'inspect']:
                return type('P', (), {'returncode': 0, 'stdout': tool.encoded(inspected)})()
            if command[:3] == ['docker', 'image', 'inspect']:
                return type('P', (), {'returncode': 0, 'stdout': b'[{"Id":"image-id"}]'})()
            self.assertIn('container:container-id', command)
            self.assertIn('--read-only', command)
            self.assertNotIn('private', command)
            self.assertNotIn('POSTGRES_URL', command)
            self.assertEqual(command[-1], 'image-id')
            return type('P', (), {'returncode': 0, 'stdout': b'{"status":"passed","versions":1}'})()
        with patch.object(tool.subprocess, 'run', side_effect=fake_run):
            self.assertEqual(tool.DockerObserver('knowledge-service', 'image')([{}])['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
