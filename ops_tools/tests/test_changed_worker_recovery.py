"""Same-boot one-shot recovery, real leases/records and metadata-only inventory.

Docker/systemd projections are synthetic. No production daemon or service action.
"""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ops_platform.contracts import ReadFailure
from ops_platform.ops import worker_state as ws, service_lifecycle as service
from ops_platform.ops import set_public_url
from ops_tools import recover_changed_worker as changed
from ops_tools.tests import test_boot_worker_recovery as previous
from ops_tools.tests import test_maintenance_control as inventory_tests


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        previous.RecoveryTests.setUp(self)
        status = self.store.optional('status.json')
        status['outcome']['code'] = changed.REASON
        ws.write(self.store.path/'status.json', status)
        self.store.pause(changed.REASON)
        self.manager.control_commands = 0  # Exclude the fixture's earlier installation.
        self.inventory = dict(inventory=deepcopy(self.f.inventory), files={}, host={},
                              evidence={}, backup_helpers_absent=True)
        self.adapter = self.make()
        self.original = {name:(self.store.path/name).read_bytes()
                         for name in ('control.json', 'status.json', 'boot.json')}

    def make(self, **options):
        return changed.Recovery(self.initial, snapshot_fn=lambda _:deepcopy(self.inventory),
                                clock=lambda:self.now, sleep=self.sleep, **options)

    def readback(self, directory, manifest, guard):
        self.readbacks += 1; guard()
        self.assertEqual(self.store.control()['recovery'], changed.REASON)
        self.assertTrue(service.enabled(manifest))
        return deepcopy(previous.fixtures.READBACK)

    sleep = previous.RecoveryTests.sleep
    files = previous.RecoveryTests.files
    code = previous.RecoveryTests.code
    proposal = previous.RecoveryTests.proposal
    good = previous.RecoveryTests.good

    # The unchanged one-shot receipt, lock and submission protocol must also
    # hold for this reason-specific adapter. Reuse its existing adversarial cases.
    test_wrong_review = previous.RecoveryTests.test_wrong_review_creates_no_journal_and_does_not_reset_or_readback
    test_unfinished_intent = previous.RecoveryTests.test_open_or_completed_intent_is_not_adopted
    test_exit_and_child_proof = previous.RecoveryTests.test_unconfirmed_exit_or_missing_child_proof_refuses_plan
    test_receipt_and_boot_binding = previous.RecoveryTests.test_receipt_invocation_identity_and_generation_changes_refused
    test_binding_and_history = previous.RecoveryTests.test_binding_or_initial_history_change_refuses_plan
    test_enable_link = previous.RecoveryTests.test_disabled_or_foreign_enable_link_never_repaired
    test_unit_and_dropin = previous.RecoveryTests.test_modified_unit_or_dropin_never_repaired
    test_real_busy_lease = previous.RecoveryTests.test_real_child_lease_prevents_even_readonly_plan
    test_stop_token_drift = previous.RecoveryTests.test_changed_stop_token_after_review_never_mutates
    test_original_failure_durable_before_reset = previous.RecoveryTests.test_durable_failed_invocation_and_live_readback_before_reset
    test_failed_live_readback_retains_latch = previous.RecoveryTests.test_readback_failure_preserves_original_latch_status_and_receipt
    test_failed_journal_retains_latch = previous.RecoveryTests.test_journal_write_failure_never_clears_recovery_or_requests_control
    test_newer_stop_wins_before_start = previous.RecoveryTests.test_newer_stop_during_readback_or_release_prevents_start
    test_invocation_not_reused = previous.RecoveryTests.test_old_manager_invocation_or_modified_entry_cannot_pass
    test_fresh_receipt_required = previous.RecoveryTests.test_changed_new_receipt_refuses_even_a_healthy_observation
    test_newer_stop_during_observation = previous.RecoveryTests.test_newer_stop_during_observation_or_final_binding_check_wins
    test_owned_generation_may_advance = previous.RecoveryTests.test_evaluation_may_commit_a_new_owned_generation_after_handoff
    test_manual_or_hardlinked_record = previous.RecoveryTests.test_changed_or_hardlinked_recovery_record_never_overwritten
    test_stale_pid = previous.RecoveryTests.test_stale_saved_pid_cannot_claim_fresh_evaluation
    test_unknown_start_not_replayed = previous.RecoveryTests.test_unknown_start_latches_and_duplicate_apply_never_replays
    test_duplicate_and_readonly_check = previous.RecoveryTests.test_duplicate_success_and_readonly_check_never_submit_or_write
    test_death_after_start_not_replayed = previous.RecoveryTests.test_process_death_after_start_can_be_checked_without_second_start
    test_lost_submitted_marker = previous.RecoveryTests.test_lost_submitted_marker_check_uses_current_entry_without_resubmission

    def test_plan_has_no_write_controls_api_or_secret_values(self):
        before = self.files()
        result = changed.run('plan', self.directory, wait_seconds=3, factory=lambda _:self.adapter)
        self.assertEqual(result['result'], 'passed', result)
        self.assertEqual(result['reason'], changed.REASON)
        self.assertFalse(result['historical_changed_container_identified'])
        self.assertFalse(result['review_authorizes_execution'])
        self.assertEqual(result['worker_service_control_commands'], 0)
        self.assertEqual(result['application_api_requests'], 0)
        self.assertEqual(result['files_written'], [])
        self.assertEqual(self.readbacks, 0); self.assertEqual(before, self.files())
        self.assertNotIn('current_discovery', result)
        self.assertFalse(self.adapter.folder.exists())

    def test_wrong_reason_is_never_cleared_or_reinterpreted(self):
        self.store.pause(previous.recovery.REASON)
        self.code('BOOT_RECOVERY_REASON_NOT_SUPPORTED', self.proposal)
        self.assertEqual(self.store.control()['recovery'], previous.recovery.REASON)

    def test_inventory_drift_invalidates_review_before_any_mutation(self):
        proposal = self.proposal()
        self.inventory['inventory']['protected']['new-container'] = 'new-start'
        before = self.files()
        self.code('BOOT_RECOVERY_REVIEW_CHANGED', lambda:self.adapter.apply(self.adapter.review(proposal), 3))
        self.assertEqual(before, self.files()); self.assertEqual(self.manager.starts, 0)
        self.assertEqual(self.readbacks, 0)

    def test_mixed_discovery_does_not_publish_a_review(self):
        after = deepcopy(self.inventory); after['files']['config'] = 'changed'
        self.adapter.snapshot = Mock(side_effect=[self.inventory, after])
        self.code('CHANGED_RECOVERY_DISCOVERY_CHANGED', self.proposal)
        self.assertEqual(self.manager.control_commands, 0)

    def test_inventory_change_during_live_readback_preserves_failed_worker(self):
        def changed_readback(d, m, guard):
            self.inventory['inventory']['protected']['e'*64] = 'changed-start'
            guard(); return previous.fixtures.READBACK
        self.adapter.readback = changed_readback
        p = self.proposal(); result = self.adapter.apply(self.adapter.review(p), 3)
        self.assertEqual(result['code'], 'CHANGED_RECOVERY_DISCOVERY_CHANGED')
        self.assertEqual(self.store.control()['recovery'], changed.REASON)
        self.assertEqual(self.manager.starts, 0); self.assertEqual(self.manager.resets, 0)

    def test_source_change_during_readback_prevents_reset_and_start(self):
        pins = changed.source_pins()
        def readback(d, m, guard):
            self.adapter.pins = lambda:dict(pins, manual='changed')
            guard(); return previous.fixtures.READBACK
        self.adapter.readback = readback
        p = self.proposal(); result = self.adapter.apply(self.adapter.review(p), 3)
        self.assertEqual(result['code'], 'CHANGED_RECOVERY_SOURCE_CHANGED')
        self.assertEqual(self.manager.control_commands, 0)

    def test_check_cannot_adopt_a_legacy_or_incomplete_record(self):
        self.adapter.folder.mkdir(mode=0o700)
        ident = 'f'*64
        ws.write(self.adapter.journal_path(ident), {'kind':changed.KIND, 'phase':'accepted'})
        self.code('CHANGED_RECOVERY_CHECK_REFUSED', lambda:self.adapter.check(ident))
        self.assertEqual(self.manager.control_commands, 0)

    def test_prepared_interruption_is_not_replayed(self):
        class Interrupted(Exception): pass
        def hook(phase):
            if phase == 'journal:prepared': raise Interrupted('synthetic interruption')
        obj = self.make(hook=hook); p = obj.plan(3)
        with self.assertRaises(Interrupted): obj.apply(obj.review(p), 3)
        self.code('BOOT_RECOVERY_ALREADY_RECORDED_USE_CHECK', self.proposal)
        self.assertEqual(self.store.control()['recovery'], changed.REASON)
        self.assertEqual(self.manager.starts, 0)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = inventory_tests.InventoryTests('runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        self.rows = []; self.calls = []
        self.probe = changed.CurrentInventory.__new__(changed.CurrentInventory)
        self.probe.review = SimpleNamespace(inventory=fixture.inv,
            controller=SimpleNamespace(records=lambda:[]),
            reader=SimpleNamespace(paths={'docker':sys.executable}))
        snapshot = fixture.inv.capture(); ops = snapshot['inventory']['scope']['ops-web']
        self.manifest = dict(binding=dict(daemon_id=snapshot['host']['daemon_id'],
            ops_container_id=ops['container_id'], ops_image_id=ops['image_id']))
        self.backend = SimpleNamespace(verify_host=lambda:self.calls.append('verify_host'), metadata=self.metadata)

    def metadata(self, args):
        self.calls.append(args)
        self.assertEqual(args, ['ps','-aq','--no-trunc','--filter','label='+changed.measure.s.LABEL])
        return ('\n'.join(self.rows)).encode()

    def capture(self):
        with patch.object(changed.measure, 'MetadataDocker', return_value=self.backend):
            return self.probe.capture(self.manifest)

    def test_real_compose_inventory_is_metadata_only_and_cache_excluded(self):
        before = inventory_tests.fixtures.snapshot(self.fixture.f.root)
        a = self.capture(); b = self.capture()
        self.assertEqual(a, b); self.assertEqual(len(a['inventory']['scope']), 9)
        self.assertTrue(a['backup_helpers_absent'])
        self.assertEqual(before, inventory_tests.fixtures.snapshot(self.fixture.f.root))
        self.assertEqual(self.fixture.docker.exec_calls, 0)
        self.assertNotIn(inventory_tests.fixtures.SECRET, json.dumps(a))

    def test_any_running_or_exited_reader_helper_blocks_without_adoption(self):
        self.rows = ['a'*64]
        with self.assertRaisesRegex(changed.maintenance.Failure, 'BACKUP_HELPERS_PRESENT'): self.capture()
        self.assertEqual(len(self.calls), 2)

    def test_invalid_duplicate_or_oversize_helper_list_refused(self):
        for rows in (['bad-id'], ['a'*64]*2, ['a'*64]*161):
            self.rows = rows
            with self.assertRaisesRegex(changed.maintenance.Failure, 'HELPER_METADATA_INVALID'): self.capture()

    def test_daemon_or_owned_ops_identity_cannot_be_rebound(self):
        for field in ('daemon_id', 'ops_container_id', 'ops_image_id'):
            old = self.manifest['binding'][field]; self.manifest['binding'][field] = 'changed'
            with self.assertRaisesRegex(changed.maintenance.Failure, 'BINDING_CHANGED'): self.capture()
            self.manifest['binding'][field] = old
        self.assertEqual(self.calls, [])

    def test_unreturned_maintenance_record_blocks_worker_recovery(self):
        self.probe.review.controller.records = lambda:[{'phase':'attention_required'}]
        with self.assertRaisesRegex(changed.maintenance.Failure, 'MAINTENANCE_UNRESOLVED'): self.capture()
        self.assertEqual(self.calls, [])


class Contracts(unittest.TestCase):
    def test_ephemeral_helper_removal_can_trigger_original_unchanged_guard(self):
        baseline = dict(container=dict(identity=dict(id='ops')), protected={'service':'start', 'helper':'start'})
        docker = SimpleNamespace(protected=lambda _: {'service':'start'})
        with self.assertRaisesRegex(ReadFailure, 'OTHER_CONTAINER_CHANGED'):
            set_public_url.protect(docker, baseline)

    def test_cli_never_leaks_exception_text_or_performs_implicit_apply(self):
        def failed(_): raise OSError('private credential contents and Docker stderr')
        report = changed.run('plan', '/unread', factory=failed)
        self.assertEqual(report['code'], 'CHANGED_RECOVERY_LOCAL_ERROR')
        self.assertNotIn('private credential', str(report))
        self.assertEqual(report['worker_service_control_commands'], 0)

    def test_loaded_dependency_pins_and_original_reason_are_unchanged(self):
        pins = changed.source_pins()
        self.assertEqual({k:pins[k] for k in changed.PINS}, changed.PINS)
        self.assertEqual(previous.recovery.REASON, 'WORKER_DOCKER_NOT_READY')

    def test_apply_requires_concrete_review_and_check_requires_id_before_factory(self):
        factory = Mock(side_effect=AssertionError('must not connect'))
        for action, code in (('apply','BOOT_RECOVERY_REVIEW_REQUIRED'), ('check','BOOT_RECOVERY_ID_INVALID')):
            result = changed.run(action, '/unread', factory=factory)
            self.assertEqual(result['code'], code)
        factory.assert_not_called()

    def test_cli_plan_safe_json(self):
        out = io.StringIO()
        with patch('sys.stdout', out), patch.object(changed, 'controller', side_effect=OSError('private contents')):
            code = changed.main(['plan'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['code'], 'CHANGED_RECOVERY_LOCAL_ERROR')


class RunningSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.ids = ['a'*64, 'b'*64]
        self.rows = [json.dumps([ident, True, 'private-started-at-value']) for ident in self.ids]
        self.calls = []
        self.docker = SimpleNamespace(query=self.query)

    def query(self, args):
        self.calls.append(args)
        if args == ['ps', '-q', '--no-trunc']: return '\n'.join(self.ids)
        self.assertEqual(args, ['container','inspect','--format',changed.maintenance.LIVE_FORMAT,*sorted(self.ids)])
        return '\n'.join(self.rows)

    def refused(self, reason):
        with self.assertRaises(ReadFailure) as caught:
            changed.running_snapshot(self.docker)
        value = caught.exception.running_inventory_diagnostic
        self.assertEqual(value['reason'], reason)
        self.assertEqual(changed.safe_running_diagnostic(value), value)
        self.assertFalse(value['retry_requested']); self.assertFalse(value['values_returned'])
        text = json.dumps(value)
        self.assertNotIn('a'*64, text); self.assertNotIn('b'*64, text)
        self.assertNotIn('private-started', text)
        return caught.exception, value['counts']

    def test_valid_projection_matches_original_reviewed_running_gate(self):
        expected = {ident:'private-started-at-value' for ident in self.ids}
        self.assertEqual(changed.running_snapshot(self.docker), expected)
        cls = changed.maintenance.docker_type(inventory_tests.cap, inventory_tests.runtime.probe)
        original = cls(); original.query = self.query
        self.assertEqual(original.running(), expected)
        self.assertEqual(len(self.calls), 4)

    def test_stopped_after_ps_is_refused_and_counted_without_retry(self):
        self.rows[1] = json.dumps([self.ids[1], False, 'private-started-at-value'])
        exc, counts = self.refused('listed_container_not_running')
        self.assertEqual(exc.code, 'MAINTENANCE_RUNNING_SET_CHANGED')
        self.assertEqual(counts['listed_running_count'], 2)
        self.assertEqual(counts['response_rows_count'], 2)
        self.assertEqual(counts['stopped_rows_count'], 1)
        self.assertEqual(counts['missing_id_count'], 0)
        self.assertEqual(len(self.calls), 2)

    def test_missing_inspect_row_is_not_adopted_as_new_running_set(self):
        self.rows.pop()
        _, counts = self.refused('row_count_differs')
        self.assertEqual(counts['response_rows_count'], 1)
        self.assertIsNone(counts['missing_id_count'])
        self.assertEqual(len(self.calls), 2)

    def test_duplicate_unexpected_json_and_row_shape_are_distinguished(self):
        cases = [(self.rows[:1]*2, 'duplicate_ids'),
                 ([self.rows[0], json.dumps(['c'*64, True, 'private-started-at-value'])], 'unexpected_ids'),
                 ([self.rows[0], 'private credential and malformed JSON'], 'response_json_invalid'),
                 ([self.rows[0], json.dumps([self.ids[1], 1, 'started'])], 'response_row_invalid')]
        for rows, reason in cases:
            self.rows = rows; self.calls.clear()
            with self.subTest(reason=reason):
                self.refused(reason); self.assertEqual(len(self.calls), 2)

    def test_invalid_list_does_not_inspect_and_oversize_counts_are_null(self):
        self.ids = ['invalid-id']; _, counts = self.refused('list_ids_invalid')
        self.assertEqual(len(self.calls), 1); self.assertEqual(counts['listed_running_count'], 1)
        self.ids = ['a'*64]*129; self.calls.clear(); _, counts = self.refused('list_ids_invalid')
        self.assertIsNone(counts['listed_running_count']); self.assertEqual(len(self.calls), 1)

    def test_no_running_containers_needs_no_inspect(self):
        self.ids = []
        self.assertEqual(changed.running_snapshot(self.docker), {})
        self.assertEqual(self.calls, [['ps','-q','--no-trunc']])

    def test_query_failure_keeps_only_safe_metadata_labels(self):
        def failed(args):
            raise changed.maintenance.Failure('MAINTENANCE_DOCKER_METADATA_FAILED',
                changed.maintenance.docker_failure('running_list', rc=1, stderr=b'private docker stderr'))
        self.docker.query = failed
        exc, counts = self.refused('metadata_query_failed')
        self.assertEqual(exc.code, 'MAINTENANCE_DOCKER_METADATA_FAILED')
        self.assertEqual(exc.docker_diagnostic['operation'], 'running_list')
        self.assertNotIn('private', json.dumps(exc.docker_diagnostic))
        self.assertTrue(all(x is None for x in counts.values()))

    def test_constructor_adds_projection_only_to_current_recovery_instance(self):
        review = SimpleNamespace(docker=self.docker, inventory=SimpleNamespace(docker=self.docker))
        with patch.object(changed.maintenance, 'Review', return_value=review):
            probe = changed.CurrentInventory(Path('/unread'), Path('/unread-langfuse'))
        self.assertIs(probe.review, review)
        self.assertEqual(review.docker.running(), {ident:'private-started-at-value' for ident in self.ids})

    def test_public_diagnostic_rejects_extra_raw_fields_and_unbounded_types(self):
        value = changed.running_failure('TEST', 'response_row_invalid', {}).running_inventory_diagnostic
        cases = [dict(value, stderr='private'), dict(value, phase='private raw phase'),
                 dict(value, reason='private raw reason'), dict(value, retry_requested=True)]
        for count in (True, -1, 129, 'private raw count'):
            counts = dict(value['counts'], stopped_rows_count=count); cases.append(dict(value, counts=counts))
        for bad in cases: self.assertIsNone(changed.safe_running_diagnostic(bad))


class DiagnosticPropagationTests(unittest.TestCase):
    setUp = RecoveryTests.setUp
    make = RecoveryTests.make
    readback = RecoveryTests.readback
    sleep = RecoveryTests.sleep
    files = RecoveryTests.files
    code = RecoveryTests.code
    proposal = RecoveryTests.proposal

    def test_plan_apply_failure_reports_phase_before_journal_readback_or_start(self):
        original = self.proposal()
        before = self.files()
        def failed(_):
            raise changed.running_failure('MAINTENANCE_RUNNING_SET_CHANGED', 'listed_container_not_running',
                dict(listed_running_count=64,response_rows_count=64,stopped_rows_count=1))
        self.adapter.snapshot = failed
        result = changed.run('apply', self.directory, expect=self.adapter.review(original),
                             wait_seconds=3, factory=lambda _:self.adapter)
        self.assertEqual(result['code'], 'MAINTENANCE_RUNNING_SET_CHANGED')
        self.assertEqual(result['running_inventory_diagnostic']['phase'], 'plan_first_inventory')
        self.assertEqual(result['worker_service_control_commands'], 0)
        self.assertFalse(result['live_configuration_readback_attempted'])
        self.assertFalse(self.adapter.folder.exists()); self.assertEqual(before, self.files())

    def test_discovery_difference_counts_current_samples_and_refuses_review(self):
        after = deepcopy(self.inventory)
        after['inventory']['protected']['e'*64] = 'restarted'
        after['inventory']['protected']['f'*64] = 'added'
        self.adapter.snapshot = Mock(side_effect=[self.inventory, after])
        result = changed.run('plan', self.directory, wait_seconds=3, factory=lambda _:self.adapter)
        diagnostic = result['running_inventory_diagnostic']
        self.assertEqual(diagnostic['phase'], 'plan_second_inventory')
        self.assertEqual(diagnostic['counts']['added_count'], 1)
        self.assertEqual(diagnostic['counts']['removed_count'], 0)
        self.assertEqual(diagnostic['counts']['started_at_changed_count'], 1)
        self.assertNotIn('review_id', result); self.assertEqual(self.manager.starts, 0)

    def test_late_guard_failure_still_preserves_diagnostic_after_attention_latch(self):
        p = self.proposal()
        def readback(d,m,guard):
            def failed(_):
                raise changed.running_failure('MAINTENANCE_RUNNING_SET_CHANGED', 'row_count_differs',
                    dict(listed_running_count=64,response_rows_count=63))
            self.adapter.snapshot = failed
            guard(); return previous.fixtures.READBACK
        self.adapter.readback = readback
        result = changed.run('apply', self.directory, expect=self.adapter.review(p),
                             wait_seconds=3, factory=lambda _:self.adapter)
        self.assertEqual(result['code'], 'MAINTENANCE_RUNNING_SET_CHANGED')
        self.assertEqual(result['running_inventory_diagnostic']['phase'], 'pre_start_guard')
        self.assertTrue(result['live_configuration_readback_attempted'])
        self.assertEqual(self.store.control()['recovery'], changed.REASON)
        self.assertEqual(self.manager.resets, 0); self.assertEqual(self.manager.starts, 0)

    def test_unknown_diagnostic_is_not_returned(self):
        def failed(_):
            exc = changed.maintenance.Failure('MAINTENANCE_RUNNING_SET_CHANGED')
            exc.running_inventory_diagnostic = {'raw':'private credential'}
            raise exc
        self.adapter.snapshot = failed
        result = changed.run('plan', self.directory, factory=lambda _:self.adapter)
        self.assertNotIn('running_inventory_diagnostic', result)
        self.assertNotIn('private credential', json.dumps(result))

    def test_late_original_adapter_failure_preserves_code_and_safe_query_diagnostic(self):
        p = self.proposal()
        def readback(d,m,guard):
            def failed(_):
                raise changed.maintenance.Failure('MAINTENANCE_DOCKER_METADATA_FAILED',
                    changed.maintenance.docker_failure('running_inspect',rc=1,stderr=b'private credential stderr'))
            self.adapter.snapshot = failed
            guard(); return previous.fixtures.READBACK
        self.adapter.readback = readback
        result = changed.run('apply',self.directory,expect=self.adapter.review(p),
                             wait_seconds=3,factory=lambda _:self.adapter)
        self.assertEqual(result['code'],'MAINTENANCE_DOCKER_METADATA_FAILED')
        self.assertEqual(result['docker_diagnostic']['operation'],'running_inspect')
        self.assertNotIn('private credential',json.dumps(result))
        self.assertEqual(self.store.control()['recovery'],changed.REASON)
        self.assertEqual(self.manager.control_commands,0)




if __name__ == '__main__': unittest.main()
