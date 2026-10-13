"""Worker guards around the real refresh journal/configuration transaction."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import refresh_worker as worker, worker_state as ws, refresh_discovery as refresh
from src.ops.platform.ops import activate_discovery as activation
from src.ops.platform.tests.test_discovery_refresh import RefreshTests


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.h = RefreshTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.directory = self.h.directory
        self.binding = {'fixture': 'same-owner-code-runtime'}
        self.snapshot = lambda _: deepcopy(self.binding)
        self.store = ws.Store(self.directory, self.binding)

    def options(self):
        h = self.h
        return dict(docker=h.docker, discover_fn=lambda cfg: deepcopy(h.h.report), validate_fn=h.h.validate,
                    source_fn=lambda: h.h.source, api_factory=lambda cfg: h.h.f.api,
                    readback_fn=h.h.readback, wait_seconds=0)

    def call(self, options=None):
        with ws.automatic_lease(self.directory):
            return worker.evaluate(self.store, self.binding, self.snapshot, 'test:1', options or self.options())

    def test_noop_does_not_write_intent_or_journal(self):
        result = self.call()
        self.assertEqual(result['classification'], 'accepted')
        self.assertFalse(result['restart_requested']); self.assertIsNone(self.store.optional('intent.json'))
        self.assertFalse(self.h.journals())

    def test_forked_cycle_executes_real_refresh_files_and_completes_intent(self):
        self.h.changed()
        def evaluate(store, binding, snapshot, evaluation):
            return worker.evaluate(store, binding, snapshot, evaluation, self.options())
        result = worker.run(self.directory, cycles=1, deadline_seconds=5,
                            snapshot_fn=self.snapshot, evaluate_fn=evaluate)
        self.assertEqual(result['result'], 'passed'); self.assertEqual(result['completed_cycles'], 1)
        self.assertEqual(refresh.lineage(self.directory)['records'][-1][1]['phase'], 'accepted')
        self.assertEqual(self.store.optional('intent.json')['phase'], 'completed')
        self.assertEqual(json.loads((self.directory / 'host-config.json').read_text())['resource_profile']['sources']['host']['job'],
                         'host-next')

    def test_intent_precedes_record_and_configuration_then_completes(self):
        self.h.changed(); save = refresh.save; observed = []
        def check(path, record):
            intent = self.store.optional('intent.json'); observed.append(intent['phase'])
            self.assertEqual(intent['baseline_sha256'], record['baseline_sha256'])
            return save(path, record)
        with patch.object(refresh, 'save', side_effect=check): result = self.call()
        self.assertTrue(result['configuration_changed']); self.assertEqual(observed, ['open', 'open'])
        self.assertEqual(self.store.optional('intent.json')['phase'], 'completed')
        self.assertEqual(self.call()['classification'], 'accepted'); self.assertEqual(len(self.h.journals()), 1)

    def test_pending_manual_operation_is_never_resumed(self):
        self.h.changed(); self.h.failure = 'before'
        self.h.error('DOCKER_TIMEOUT'); before = self.h.journals()[0].read_bytes(); count = self.h.docker.restarts
        self.h.failure = None
        result = self.call()
        self.assertEqual(result['code'], 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
        self.assertEqual(self.h.journals()[0].read_bytes(), before); self.assertEqual(self.h.docker.restarts, count)

    def test_pause_arriving_during_discovery_prevents_first_write(self):
        self.h.changed(); options = self.options()
        def pause(cfg): self.store.pause(); return deepcopy(self.h.h.report)
        options['discover_fn'] = pause
        result = self.call(options)
        self.assertEqual(result['classification'], 'stopped'); self.assertFalse(self.h.journals())
        self.assertEqual(self.h.h.config_snapshot(), self.h.original_configs)

    def test_identity_change_before_write_is_blocked(self):
        self.h.changed(); calls = []
        def snapshot(_):
            calls.append(1)
            return self.binding if len(calls) == 1 else {'fixture': 'new-daemon'}
        self.snapshot = snapshot
        result = self.call(); self.assertEqual(result['code'], 'WORKER_BINDING_CHANGED')
        self.assertFalse(self.h.journals()); self.assertIsNone(self.store.optional('intent.json'))

    def test_after_intent_failure_requires_explicit_recovery_not_retry(self):
        self.h.changed(); self.h.failure = 'after'; result = self.call()
        self.assertEqual(result['classification'], 'recovery_required')
        self.store.pause(result['code']); count = self.h.docker.restarts
        self.h.failure = None
        self.assertEqual(self.call()['classification'], 'recovery_required'); self.assertEqual(self.h.docker.restarts, count)
        with self.assertRaisesRegex(ReadFailure, 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK'):
            worker.acknowledge_recovery(self.directory, self.snapshot, self.options())
        self.h.call('rollback')
        result = worker.acknowledge_recovery(self.directory, self.snapshot, self.options())
        self.assertTrue(result['live_checks_performed']); self.assertFalse(result['worker_started'])
        self.assertEqual(self.store.control()['desired'], 'running')

    def test_accepted_transaction_with_incomplete_controller_is_not_replayed(self):
        self.h.changed()
        with patch.object(worker.Guard, 'complete', side_effect=OSError('lost controller write')): result = self.call()
        self.assertEqual(result['classification'], 'recovery_required'); count = self.h.docker.restarts
        self.assertEqual(refresh.lineage(self.directory)['records'][-1][1]['phase'], 'accepted')
        self.store.pause(result['code'])
        worker.acknowledge_recovery(self.directory, self.snapshot, self.options())
        self.assertEqual(self.h.docker.restarts, count)
        self.assertEqual(self.call()['classification'], 'accepted')

    def test_unwritten_journal_intent_recovery_requires_exact_original_files(self):
        self.h.changed()
        with patch.object(refresh, 'save', side_effect=OSError('disk full')): result = self.call()
        self.assertEqual(result['classification'], 'recovery_required'); self.store.pause(result['code'])
        self.assertFalse(self.h.journals())
        worker.acknowledge_recovery(self.directory, self.snapshot, self.options())
        self.assertEqual(self.store.optional('intent.json')['phase'], 'completed')
        self.assertEqual(self.h.h.config_snapshot(), self.h.original_configs)

    def test_intent_save_failure_prevents_all_refresh_writes(self):
        self.h.changed()
        with patch.object(ws, 'write', side_effect=OSError('disk full')): result = self.call()
        self.assertEqual(result['classification'], 'blocked'); self.assertFalse(self.h.journals())
        self.assertEqual(self.h.h.config_snapshot(), self.h.original_configs)

    def test_singleton_shared_with_legacy_watch(self):
        with ws.automatic_lease(self.directory):
            with self.assertRaisesRegex(ReadFailure, 'AUTOMATIC_WORKER_BUSY'):
                refresh.watch(self.directory, cycles=1)
        with self.assertRaisesRegex(ReadFailure, 'GUARDED_WORKER_REQUIRED'):
            refresh.watch(self.directory, cycles=1)

    def test_automatic_hook_checks_pending_inside_the_transaction_lock(self):
        self.h.changed(); self.h.failure = 'after'; self.h.error('DOCKER_TIMEOUT')
        class NeverEntered:
            def enter(self, context): raise AssertionError('pending must be rejected first')
        with self.assertRaisesRegex(ReadFailure, 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK'):
            self.h.call(automatic_guard=NeverEntered())

    def test_noop_pause_does_not_call_readback(self):
        options = self.options()
        def pause(cfg): self.store.pause(); return deepcopy(self.h.h.report)
        options['discover_fn'] = pause
        self.assertEqual(self.call(options)['classification'], 'stopped')
        self.assertFalse(self.h.h.reads)

    def test_history_limit_blocks_noop_automatic_but_manual_semantics_unchanged(self):
        original = refresh.lineage
        def full(directory): return dict(original(directory), next=257)
        with patch.object(refresh, 'lineage', side_effect=full):
            self.assertEqual(self.call()['code'], 'REFRESH_HISTORY_LIMIT')
            self.assertEqual(self.h.call()['result'], 'passed')

    def test_state_contains_no_fixture_credentials(self):
        self.h.changed(); self.call()
        from src.ops.platform.tests.test_restart_check import TOKEN, LF_SECRET
        raw = ''.join(p.read_text() for p in self.store.path.glob('*.json'))
        for secret in (TOKEN, LF_SECRET, (self.directory / 'access-key').read_text().strip()):
            self.assertNotIn(secret, raw)

    def test_discovery_network_failure_is_retryable_only_before_intent(self):
        options = self.options()
        def failed(cfg): raise ReadFailure('UPSTREAM_HTTP_ERROR', 503)
        options['discover_fn'] = failed
        self.assertEqual(self.call(options)['classification'], 'retryable_prewrite')
        self.assertIsNone(self.store.optional('intent.json')); self.assertFalse(self.h.journals())
        self.h.changed(); options = self.options(); options['readback_fn'] = failed
        self.assertEqual(self.call(options)['classification'], 'recovery_required')

    def test_transaction_lock_contention_is_prewrite_retry_not_recovery(self):
        from src.ops.platform.ops import set_public_url as change
        with change.change_lock(self.directory):
            self.assertEqual(self.call()['classification'], 'retryable_prewrite')
        self.assertIsNone(self.store.optional('intent.json'))


class ClassificationTests(unittest.TestCase):
    def report(self):
        from src.ops.platform.ops.discovery_compose import ROLES
        return {'deployment_metadata_stable': True, 'services': {role: {'status': 'verified'} for role in ROLES},
                'metrics': {'status': 'verified'}}

    def test_known_prewrite_5xx_timeout_and_connection_failure_only(self):
        for code, status in [('TIMEOUT', None), ('CONNECTION_FAILED', None), ('UPSTREAM_HTTP_ERROR', 503)]:
            report = self.report(); report['services']['model'] = {'error': code, 'http_status': status}
            self.assertTrue(worker.transient_discovery(report))
        for code, status in [('UNAUTHENTICATED', 401), ('UPSTREAM_HTTP_ERROR', 422),
                             ('UPSTREAM_HTTP_ERROR', None), ('SERVICE_AMBIGUOUS', None), ('INVALID_JSON', None)]:
            report = self.report(); report['services']['model'] = {'error': code, 'http_status': status}
            self.assertFalse(worker.transient_discovery(report))

    def test_mixed_auth_ambiguity_or_unstable_reports_never_retry(self):
        report = self.report(); report['services']['model'] = {'error': 'TIMEOUT'}
        report['services']['data'] = {'error': 'UNAUTHENTICATED'}
        self.assertFalse(worker.transient_discovery(report))
        report['services']['data'] = {'status': 'verified'}; report['deployment_metadata_stable'] = False
        self.assertFalse(worker.transient_discovery(report))
        self.assertFalse(worker.transient_discovery({'result': 'attention_required'}))

    def test_code_digest_excludes_docs_and_detects_python_change(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'src/ops/platform').mkdir(parents=True)
            code = root / 'src/ops/platform/a.py'; code.write_text('x = 1')
            before = worker.code_hash(root); (root / 'src/ops/platform/README.md').write_text('changed')
            self.assertEqual(worker.code_hash(root), before)
            code.write_text('x = 2'); self.assertNotEqual(worker.code_hash(root), before)


if __name__ == '__main__': unittest.main()
