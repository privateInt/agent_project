"""Reset boundary, changed-data, retry and partial-failure tests; no live infrastructure."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import reset_test_data as reset
import reset_volume
import verify_reset
from common import OpsError

OP = '11111111-1111-4111-8111-111111111111'
RUN = '22222222-2222-4222-8222-222222222222'
MODEL = '33333333-3333-4333-8333-333333333333'
VERSION = '44444444-4444-4444-8444-444444444444'
DEP = '55555555-5555-4555-8555-555555555555'
DATA = '66666666-6666-4666-8666-666666666666'
TOKEN = '77777777-7777-4777-8777-777777777777'
ML = 'a' * 32
CID = 'b' * 64
DIGEST = 'c' * 64
IDS = {'operations': [{'operation_id': OP, 'resource_id': RUN, 'execution_type': 'training.run'}],
       'training_runs': [{'training_run_id': RUN, 'mlflow_run_id': ML, 'mlflow_experiment_id': '1'}],
       'model_versions': [{'model_id': MODEL, 'model_version_id': VERSION}],
       'datasets': [{'dataset_id': DATA}], 'deployments': [{'deployment_id': DEP}]}


def job():
    return {'Id': CID, 'Name': '/model-op-' + OP + '-' + TOKEN, 'Image': 'sha256:' + DIGEST,
            'Config': {'Labels': {'model-platform.owner': 'operation-dispatcher', 'model-platform.operation_id': OP,
                                  'model-platform.execution_type': 'training.run', 'model-platform.dispatch_token': TOKEN,
                                  'model-platform.execution_node': 'local', 'model-platform.requested_node': ''}},
            'State': {'Status': 'exited'}, 'Mounts': []}


def database(empty=False):
    return {db: {t: {'count': 0 if empty else 1, 'digest': 'empty' if empty else 'old'} for t in tables}
            for db, tables in reset.TABLES.items()}


class ResetScopeTests(unittest.TestCase):
    def test_job_requires_database_name_labels_and_local_node(self):
        self.assertEqual(reset.owned_jobs([job()], IDS, 'local')[0]['id'], CID)
        for field, value in [('model-platform.operation_id', TOKEN), ('model-platform.dispatch_token', RUN),
                             ('model-platform.execution_type', 'deployment.apply'), ('model-platform.execution_node', 'remote')]:
            with self.subTest(field=field):
                c = job(); c['Config']['Labels'][field] = value
                with self.assertRaises(OpsError): reset.owned_jobs([c], IDS, 'local')
        c = job(); c['Name'] = '/unrelated'
        with self.assertRaises(OpsError): reset.owned_jobs([c], IDS, 'local')

    def test_serving_uses_operation_resource_link_and_unrelated_containers_are_untouched(self):
        ids = copy.deepcopy(IDS); ids['operations'][0].update(resource_id=DEP, execution_type='deployment.serve')
        c = job(); c['Name'] = '/model-serving-' + DEP
        c['Config']['Labels'].update({'model-platform.owner': 'deployment-serving', 'model-platform.deployment_id': DEP})
        other = {'Name': '/contextrag-backend', 'Config': {'Labels': {}}}
        self.assertEqual(len(reset.owned_jobs([c, other], ids, 'local')), 1)
        ids['operations'][0]['resource_id'] = RUN
        with self.assertRaises(OpsError): reset.owned_jobs([c], ids, 'local')

    def test_shared_volume_mount_blocks_even_if_other_container_is_stopped(self):
        ctx = Mock(); ctx.services = {}; ctx.volumes = dict(zip(reset.KINDS, reset.KINDS))
        c = job(); c['Mounts'] = [{'Name': 'training-outputs', 'RW': True}]
        with self.assertRaises(OpsError): reset.check_mounts(ctx, [c], [])
        reset.check_mounts(ctx, [c], [{'id': CID}])

    def test_s3_keys_must_link_to_the_correct_database_resource(self):
        keys = {'models': f'models/{MODEL}/versions/{VERSION}/outputs/{DIGEST}/adapter_model.safetensors',
                'datasets': f'datasets/{DATA}/sha256/{DIGEST}/train.jsonl',
                'logs': f'operations/{OP}/{TOKEN}/{CID}/{DIGEST}/metadata.json'}
        for kind, key in keys.items():
            with self.subTest(kind=kind):
                self.assertTrue(reset.valid_key(kind, key, IDS))
                self.assertFalse(reset.valid_key(kind, key.replace(MODEL if kind == 'models' else DATA if kind == 'datasets' else OP, RUN), IDS))
                self.assertFalse(reset.valid_key(kind, key + '/../other', IDS))
                self.assertFalse(reset.valid_key(kind, 'shared-data/file', IDS))

    def test_mlflow_requires_both_training_and_operation_identity(self):
        info = {'run_id': ML}; tags = {'src.platform.model.training_run_id': RUN, 'src.platform.model.operation_id': OP}
        self.assertTrue(reset.matches_run(info, tags, IDS))
        for bad in ({}, {**tags, 'src.platform.model.operation_id': TOKEN}, {**tags, 'src.platform.model.training_run_id': MODEL}):
            self.assertFalse(reset.matches_run(info, bad, IDS))
        self.assertFalse(reset.matches_run({'run_id': '../other'}, tags, IDS))

    def test_database_replay_allows_empty_database_but_never_replaced_rows(self):
        old = database(); current = copy.deepcopy(old)
        reset.subset_state(current, old)
        current['model_operations'] = database(True)['model_operations']
        reset.subset_state(current, old)
        current['model_management']['models']['digest'] = 'new-row-same-count'
        with self.assertRaises(OpsError): reset.subset_state(current, old)

    def test_inactive_does_not_accept_paused_restarting_or_running_containers(self):
        for state in ('created', 'exited'): self.assertTrue(reset.inactive({'State': {'Status': state}}))
        for state in ('paused', 'restarting', 'running', 'dead'): self.assertFalse(reset.inactive({'State': {'Status': state}}))
        self.assertFalse(reset.inactive({'State': {'Status': 'exited', 'Running': True}}))

    def test_endpoint_uses_only_published_loopback_or_all_ipv4_binding(self):
        c = {'Name': '/minio', 'NetworkSettings': {'Ports': {'9000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '19000'}, {'HostIp': '::', 'HostPort': '19000'}]}}}
        self.assertEqual(reset.endpoint(c, 9000), 'http://127.0.0.1:19000')
        c['NetworkSettings']['Ports']['9000/tcp'][0]['HostIp'] = '192.168.0.1'
        with self.assertRaises(OpsError): reset.endpoint(c, 9000)

    def test_concurrent_reset_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'lock'
            with reset.reset_lock(p):
                with self.assertRaises(OpsError):
                    with reset.reset_lock(p): pass
            with reset.reset_lock(p): pass


class DatabaseInventoryTests(unittest.TestCase):
    def context(self, extras=(), missing=(), trigger=False):
        ctx = Mock()
        def sql(db, statement):
            self.assertTrue(statement.startswith('SELECT '), statement)
            if 'FROM pg_tables' in statement:
                rows = {('public', t) for t in reset.TABLES[db]}
                if db == 'model_management':
                    rows.update(extras)
                    rows.difference_update(missing)
                return json.dumps([{'schemaname': s, 'tablename': t} for s, t in sorted(rows)])
            if 'FROM pg_trigger' in statement:
                return '1' if trigger and db == 'model_management' else '0'
            self.assertIn('count(*) AS count', statement)
            legacy = db == 'model_management' and 'FROM public."operations" t' in statement
            return json.dumps([{'count': 7 if legacy else 2, 'digest': 'legacy' if legacy else 'current'}])
        ctx.sql.side_effect = sql
        return ctx

    def test_inventory_supports_fresh_database_and_counts_present_legacy_table(self):
        fresh = reset.db_inventory(self.context())
        self.assertNotIn('operations', fresh['model_management'])
        actual = reset.db_inventory(self.context(extras=[('public', 'operations')]))
        self.assertEqual(actual['model_management']['operations'], {'count': 7, 'digest': 'legacy'})
        self.assertEqual(actual['model_operations'], fresh['model_operations'])

    def test_other_tables_schemas_and_missing_required_tables_still_fail(self):
        cases = [([('public', 'customer_data')], [], 'public.customer_data'),
                 ([('other', 'operations')], [], 'other.operations'),
                 ([('public', 'operations'), ('public', 'operation_outbox')], [], 'public.operation_outbox'),
                 ([], [('public', 'models')], 'public.models')]
        for extra, missing, detail in cases:
            with self.subTest(detail=detail):
                with self.assertRaisesRegex(OpsError, detail):
                    reset.db_inventory(self.context(extra, missing))
        with self.assertRaises(OpsError):
            reset.checked_tables('model_training', {('public', t) for t in reset.TABLES['model_training']} | {('public', 'operations')})

    def test_legacy_table_does_not_bypass_user_trigger_check(self):
        with self.assertRaisesRegex(OpsError, 'User trigger in model_management'):
            reset.db_inventory(self.context(extras=[('public', 'operations')], trigger=True))

    def test_empty_retry_requires_the_same_optional_table_set(self):
        planned = database()
        planned['model_management']['operations'] = {'count': 7, 'digest': 'legacy'}
        empty = database(True)
        empty['model_management']['operations'] = {'count': 0, 'digest': 'empty'}
        reset.subset_state(empty, planned)
        for current, old in [(database(True), planned), (empty, database())]:
            with self.subTest(current_tables=list(current['model_management'])):
                with self.assertRaisesRegex(OpsError, 'table set changed'):
                    reset.subset_state(current, old)
        changed = copy.deepcopy(planned)
        changed['model_management']['operations']['digest'] = 'replaced-row'
        with self.assertRaisesRegex(OpsError, 'rows changed'):
            reset.subset_state(changed, planned)

    def test_schema_failure_prevents_prepare_from_stopping_services(self):
        ctx, backend = Mock(), Mock()
        backend.node = 'local'
        with patch.object(reset, 'identities', return_value=IDS), patch.object(reset, 'containers', return_value=[]), \
                patch.object(reset, 'check_mounts'), patch.object(reset, 'db_inventory', side_effect=OpsError('schema mismatch')), \
                patch.object(reset, 'docker') as docker:
            with self.assertRaisesRegex(OpsError, 'schema mismatch'):
                reset.prepare(ctx, backend, 'unused-plan.json')
        docker.assert_not_called()


class VolumeResetTests(unittest.TestCase):
    def fixture(self, root):
        p = root / RUN / TOKEN / 'output'
        p.mkdir(parents=True)
        (p / 'weights').write_bytes(b'weights')
        (p / 'tokenizer').write_bytes(b'tokenizer')
        return reset_volume.inventory(root, 'training-outputs', [RUN])

    def test_only_selected_root_is_emptied_and_repeat_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td); root = parent / 'data'; root.mkdir()
            sibling = parent / 'other-project'; sibling.write_text('keep')
            entries = self.fixture(root)
            out = reset_volume.reset(root, 'training-outputs', [RUN], entries)
            self.assertTrue(out['empty']); self.assertEqual(sibling.read_text(), 'keep')
            self.assertTrue(reset_volume.reset(root, 'training-outputs', [RUN], entries)['empty'])

    def test_changed_file_and_new_directory_reject_before_any_deletion(self):
        for mode in ('file', 'directory', 'symlink'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                root = Path(td); entries = self.fixture(root)
                target = root / RUN / TOKEN / 'output/weights'
                if mode == 'file': target.write_bytes(b'changed-data')
                elif mode == 'directory': (root / DATA).mkdir()
                else: (root / RUN / 'link').symlink_to('/tmp')
                with self.assertRaises(ValueError): reset_volume.reset(root, 'training-outputs', [RUN], entries)
                self.assertTrue(target.exists())

    def test_partial_file_deletion_can_resume_using_same_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); entries = self.fixture(root)
            (root / RUN / TOKEN / 'output/weights').unlink()
            self.assertTrue(reset_volume.reset(root, 'training-outputs', [RUN], entries)['empty'])

    def test_admission_claim_of_another_operation_blocks_reset(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            body = {'version': 1, 'claims': {}, 'cancelled_operations': {TOKEN: True}}
            (root / 'claims.json').write_text(json.dumps(body))
            with self.assertRaises(ValueError): reset_volume.inventory(root, 'gpu-admission', [OP])
            body['cancelled_operations'] = {OP: True}
            (root / 'claims.json').write_text(json.dumps(body))
            entries = reset_volume.inventory(root, 'gpu-admission', [OP])
            self.assertTrue(reset_volume.reset(root, 'gpu-admission', [OP], entries)['empty'])


class BackendTests(unittest.TestCase):
    def bare(self):
        b = reset.Backend.__new__(reset.Backend)
        b.buckets = {'models': 'model-artifacts', 'datasets': 'model-datasets', 'logs': 'model-job-logs'}
        b.client = Mock(); b.topic = 'ops.operations.requested'; b.group = 'model-platform-dispatchers'
        return b

    def objects(self):
        return {k: {'objects': [{'key': 'selected', 'size': 7, 'etag': 'fixed'}] if k == 'models' else [], 'uploads': []} for k in ('models', 'datasets', 'logs')}

    def test_partial_s3_error_is_not_success_and_other_buckets_are_not_deleted(self):
        b = self.bare(); old = self.objects(); b.objects = Mock(return_value=old)
        b.client.delete_objects.return_value = {'Errors': [{'Key': 'selected', 'Code': 'AccessDenied'}]}
        with self.assertRaises(OpsError): b.clear_objects(old, IDS, lambda: None)
        self.assertEqual(b.client.delete_objects.call_count, 1)
        self.assertEqual(b.client.delete_objects.call_args.kwargs['Bucket'], 'model-artifacts')
        b.client.delete_bucket.assert_not_called()

    def test_changed_s3_etag_is_rejected_without_mutation(self):
        b = self.bare(); old = self.objects(); now = copy.deepcopy(old)
        now['models']['objects'][0]['etag'] = 'replacement'
        b.objects = Mock(return_value=now)
        with self.assertRaises(OpsError): b.clear_objects(old, IDS, lambda: None)
        b.client.delete_objects.assert_not_called()

    def test_kafka_new_records_block_delete_and_offset_reset(self):
        b = self.bare(); b.kafka_inventory = Mock(return_value={'topic_id': 'id', 'end': {'0': 12}})
        b.kcmd = Mock()
        with self.assertRaises(OpsError): b.clear_kafka({'topic_id': 'id', 'end': {'0': 11}})
        b.kcmd.assert_not_called()

    def test_kafka_targets_exact_offsets_and_checks_group_result(self):
        b = self.bare(); planned = {'topic_id': 'id', 'end': {'0': 12, '1': 0}}
        b.kafka_inventory = Mock(return_value=planned)
        b.offsets = Mock(return_value=planned['end'])
        b.kcmd = Mock(side_effect=['done', '\nGROUP TOPIC PARTITION NEW-OFFSET\nmodel-platform-dispatchers ops.operations.requested 0 12\nmodel-platform-dispatchers ops.operations.requested 1 0\n'])
        out = b.clear_kafka(planned)
        self.assertFalse(out['old_records_readable'])
        payload = json.loads(b.kcmd.call_args_list[0].kwargs['data'])
        self.assertEqual(payload['partitions'][0], {'topic': b.topic, 'partition': 0, 'offset': 12})
        self.assertNotIn('--all-topics', b.kcmd.call_args_list[1].args)

    def test_active_kafka_group_is_rejected_before_record_deletion(self):
        b = self.bare()
        b.kcmd = Mock(side_effect=[
            'Topic: ops.operations.requested TopicId: abc PartitionCount: 1',
            'GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG CONSUMER-ID HOST CLIENT-ID',
            'model-platform-dispatchers\n',
            'GROUP COORDINATOR (ID) ASSIGNMENT-STRATEGY STATE #MEMBERS\nmodel-platform-dispatchers localhost (1) range Stable 1',
        ])
        with self.assertRaisesRegex(OpsError, 'still active'): b.kafka_inventory()
        self.assertFalse(any(c.args[0] == 'kafka-delete-records' for c in b.kcmd.call_args_list))

    def test_mlflow_soft_delete_never_deletes_experiment_or_shared_store(self):
        b = self.bare(); b.mlflow_endpoint = 'http://127.0.0.1:5000'
        row = {'run_id': ML}; b.mlflow_runs = Mock(return_value=[row])
        active = {'run': {'info': {'run_id': ML, 'lifecycle_stage': 'active'}, 'data': {'tags': [
            {'key': 'src.platform.model.training_run_id', 'value': RUN}, {'key': 'src.platform.model.operation_id', 'value': OP}]}}}
        deleted = copy.deepcopy(active); deleted['run']['info']['lifecycle_stage'] = 'deleted'
        with patch.object(reset, 'http', side_effect=[active, {}, deleted]) as http:
            out = b.clear_mlflow([row], IDS, lambda: None)
        self.assertEqual(out['mode'], 'soft-delete')
        self.assertEqual(http.call_args_list[1].args[1:], ('/api/2.0/mlflow/runs/delete', {'run_id': ML}))
        self.assertTrue(all('experiment' not in c.args[1] for c in http.call_args_list))


class ApplyFailureTests(unittest.TestCase):
    def setup_apply(self):
        ctx = Mock(); backend = Mock(); binding = {'project': 'test'}
        backend.binding.return_value = binding
        backend.node = 'local'
        objects = {k: {'objects': [], 'uploads': []} for k in ('models', 'datasets', 'logs')}
        backend.objects.return_value = objects; backend.volume.return_value = []
        backend.mlflow_runs.return_value = []; backend.kafka_inventory.return_value = {'topic_id': 'id', 'end': {'0': 1}}
        plan = {'format': 1, 'scope': 'local-platform-test-reset', 'binding': binding, 'identities': IDS,
                'jobs': [], 'databases': database(), 'buckets': objects, 'volumes': {k: [] for k in reset.KINDS},
                'mlflow_runs': [], 'kafka': backend.kafka_inventory.return_value}
        return ctx, backend, plan

    def test_preflight_failure_never_starts_destructive_steps(self):
        ctx, backend, plan = self.setup_apply()
        backend.objects.return_value = {**plan['buckets'], 'models': {'objects': [{'key': 'unplanned'}], 'uploads': []}}
        with tempfile.TemporaryDirectory() as td, patch.object(reset, 'cold'), patch.object(reset, 'db_inventory', return_value=database()), patch.object(reset, 'containers', return_value=[]):
            with self.assertRaises(OpsError): reset.apply(ctx, backend, plan, Path(td) / 'receipt')
        backend.clear_kafka.assert_not_called(); backend.clear_mlflow.assert_not_called()
        backend.clear_objects.assert_not_called(); ctx.sql.assert_not_called()

    def test_partial_failure_records_false_and_does_not_truncate_or_restart(self):
        ctx, backend, plan = self.setup_apply()
        backend.clear_kafka.return_value = {'ok': True}
        backend.clear_mlflow.side_effect = OpsError('injected MLflow error')
        with tempfile.TemporaryDirectory() as td, patch.object(reset, 'cold'), patch.object(reset, 'db_inventory', return_value=database()), patch.object(reset, 'containers', return_value=[]), patch.object(reset, 'docker') as docker:
            p = Path(td) / 'receipt'
            with self.assertRaises(OpsError): reset.apply(ctx, backend, plan, p)
            result = json.loads(p.read_text())
            self.assertFalse(result['passed']); self.assertEqual(result['phase'], 'kafka')
        ctx.sql.assert_not_called(); docker.assert_not_called(); backend.clear_objects.assert_not_called()

    def test_success_removes_runtimes_and_volumes_before_atomic_database_reset(self):
        ctx, backend, plan = self.setup_apply(); db = database()
        backend.clear_kafka.return_value = {}; backend.clear_mlflow.return_value = {}; backend.clear_objects.return_value = {}
        backend.offsets.return_value = plan['kafka']['end']
        events = []
        def clear(db_name, statement):
            events.append(db_name)
            self.assertIn('BEGIN;', statement); self.assertIn('COMMIT;', statement)
            self.assertIn('TRUNCATE TABLE', statement); self.assertNotIn('CASCADE', statement)
            db[db_name] = database(True)[db_name]
        ctx.sql.side_effect = clear
        with tempfile.TemporaryDirectory() as td, patch.object(reset, 'cold'), patch.object(reset, 'db_inventory', side_effect=lambda ctx: copy.deepcopy(db)), patch.object(reset, 'containers', return_value=[]), patch.object(reset, 'docker'):
            result = reset.apply(ctx, backend, plan, Path(td) / 'receipt')
        self.assertTrue(result['passed']); self.assertEqual(events, list(reset.DATABASES))
        self.assertEqual([k['step'] for k in result['completed'][:6]], ['kafka', 'mlflow', 'objects', 'runtimes', 'training-outputs', 'serving-cache'])

    def test_apply_empties_planned_legacy_table_in_management_transaction(self):
        ctx, backend, plan = self.setup_apply()
        plan['databases']['model_management']['operations'] = {'count': 7, 'digest': 'legacy'}
        db = copy.deepcopy(plan['databases'])
        backend.clear_kafka.return_value = {}; backend.clear_mlflow.return_value = {}; backend.clear_objects.return_value = {}
        backend.offsets.return_value = plan['kafka']['end']
        def clear(db_name, statement):
            self.assertIn('BEGIN;', statement); self.assertIn('COMMIT;', statement)
            self.assertNotIn('DROP ', statement); self.assertNotIn('CASCADE', statement)
            names = statement.split('TRUNCATE TABLE ', 1)[1].split(' RESTART IDENTITY;', 1)[0]
            self.assertEqual(set(names.split(',')), {'public."' + t + '"' for t in plan['databases'][db_name]})
            for row in db[db_name].values(): row.update(count=0, digest='empty')
        ctx.sql.side_effect = clear
        with tempfile.TemporaryDirectory() as td, patch.object(reset, 'cold'), \
                patch.object(reset, 'db_inventory', side_effect=lambda ctx: copy.deepcopy(db)), \
                patch.object(reset, 'containers', return_value=[]), patch.object(reset, 'docker'):
            result = reset.apply(ctx, backend, plan, Path(td) / 'receipt')
        self.assertTrue(result['passed'])
        self.assertEqual(db['model_management']['operations']['count'], 0)

    def test_new_optional_table_after_prepare_blocks_all_deletions(self):
        ctx, backend, plan = self.setup_apply()
        now = database(True)
        now['model_management']['operations'] = {'count': 0, 'digest': 'empty'}
        with tempfile.TemporaryDirectory() as td, patch.object(reset, 'cold'), patch.object(reset, 'db_inventory', return_value=now):
            receipt = Path(td) / 'receipt'
            with self.assertRaisesRegex(OpsError, 'table set changed'):
                reset.apply(ctx, backend, plan, receipt)
            self.assertFalse(receipt.exists())
        backend.clear_kafka.assert_not_called(); backend.clear_mlflow.assert_not_called()
        backend.clear_objects.assert_not_called(); ctx.sql.assert_not_called()


class VerifyResetTests(unittest.TestCase):
    def test_capacity_warning_is_separate_from_successful_reset(self):
        ctx = Mock(); backend = Mock()
        backend.binding.return_value = {'config_sha256': 'config'}
        backend.objects.return_value = {k: {'objects': [], 'uploads': []} for k in ('models', 'datasets', 'logs')}
        backend.volume.return_value = []; backend.offsets.return_value = {'0': 12}
        plan = {'identities': IDS, 'binding': {'config_sha256': 'config'}, 'kafka': {'end': {'0': 12}}, 'mlflow_runs': []}
        receipt = {'passed': True, 'phase': 'complete', 'plan_sha256': reset.hash_json(plan)}
        audit = {'passed': False, 'volumes': {'serving-cache': {'pressure': 'critical'}}, 'minio_paths': [{'pressure': 'critical'}]}
        with patch.object(verify_reset, 'empty_catalog', return_value={}), patch.object(verify_reset, 'db_inventory', return_value=database(True)), patch.object(verify_reset, 'containers', return_value=[]), patch.object(verify_reset, 'audit', return_value=audit):
            out = verify_reset.verify(ctx, backend, plan, receipt, 'local')
        self.assertTrue(out['reset_passed']); self.assertFalse(out['capacity_passed']); self.assertFalse(out['passed'])

    def test_empty_catalog_checks_all_six_and_rejects_remaining_rows(self):
        def reply(api, path): return {'datasets': []} if '/datasets?' in path else {'items': []}
        with patch.object(verify_reset, 'api_get', side_effect=reply) as get:
            self.assertEqual(len(verify_reset.empty_catalog('local')), 6)
            self.assertEqual(get.call_count, 6)
        with patch.object(verify_reset, 'api_get', return_value={'items': [{'name': 'remaining'}]}):
            with self.assertRaises(OpsError): verify_reset.empty_catalog('local')


if __name__ == '__main__':
    unittest.main()
