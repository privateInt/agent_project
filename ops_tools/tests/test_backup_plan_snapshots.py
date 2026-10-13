"""Metadata ordering and real discovery changes in the read-only backup plan."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops_tools import backup_plan as p
from ops_tools.tests import test_backup_production_plan as previous


@unittest.skipUnless((previous.TOOLS/'age-keygen').is_file(),'cached test age unavailable')
class Snapshots(unittest.TestCase):
    def setUp(self):
        self.mapping=previous.ProductionMapping('runTest');self.mapping.setUp()
        self.addCleanup(self.mapping.doCleanups)
        self.plan=self.mapping.plan

    def reverse(self):
        for rows in self.mapping.f.rows.values():
            for row in rows:
                if 'mounts' in row: row['mounts']=list(reversed(row['mounts']))

    def between(self,edit):
        snapshot=self.plan.snapshot;count=0
        def sample():
            nonlocal count
            if count==1: edit()
            value=snapshot();count+=1;return value
        self.plan.snapshot=sample

    def failure(self):
        with self.assertRaisesRegex(previous.s.Failure,'BACKUP_GENERATION_DISCOVERY_CHANGED') as caught:
            self.plan.plan()
        self.assertEqual(self.plan.phase,'snapshot_comparison')
        return caught.exception.snapshot_diagnostic

    def test_reordered_mounts_make_identical_complete_contracts(self):
        before,_,_=self.plan.snapshot();self.reverse();after,_,_=self.plan.snapshot()
        self.assertEqual(p.contract(before,self.mapping.maintenance,self.plan.preparation),
                         p.contract(after,self.mapping.maintenance,self.plan.preparation))
        previous.g.validate(before);previous.g.validate(after)

    def test_whole_plan_accepts_two_real_inventory_samples_with_reordered_mounts(self):
        before=previous.captest.fixtures.snapshot(self.mapping.f.root)
        self.between(self.reverse)
        with patch.object(p,'capacity',return_value={'synthetic':True}):result=self.plan.plan()
        self.assertEqual(result['result'],'passed');self.assertEqual(result['source_count'],22)
        self.assertFalse(result['backup_ready']);self.assertFalse(result['review_authorizes_execution'])
        self.assertEqual(self.mapping.docker.exec_calls,0)
        self.assertEqual(before,previous.captest.fixtures.snapshot(self.mapping.f.root))

    def test_sorting_preserves_every_field_and_does_not_modify_original_rows(self):
        self.reverse();original=deepcopy(self.mapping.f.rows)
        value,_,_=self.plan.snapshot()
        for service in value['cold']['services'].values():
            row=next(x for rows in original.values() for x in rows if x['id']==service['container_id'])
            key=lambda x:json.dumps(x,sort_keys=True,separators=(',',':'))
            self.assertEqual(service['mounts'],sorted(row['mounts'],key=key))
        self.assertEqual(self.mapping.f.rows,original)

    def test_changed_mount_source_target_type_or_write_permission_still_refused(self):
        row=self.mapping.f.rows['ops'][0];old=deepcopy(row['mounts'])
        for field,value in [('Source','/foreign-source'),('Destination','/foreign-target'),('Type','volume'),('RW',True)]:
            row['mounts']=deepcopy(old)
            mount=next(x for x in row['mounts'] if x['RW'] is False);mount[field]=value
            with self.subTest(field=field),self.assertRaisesRegex(previous.s.Failure,'OWNED_SERVICE_SET_UNVERIFIED'):
                self.plan.snapshot()
        row['mounts']=old

    def test_duplicate_mount_is_not_deduplicated_or_accepted(self):
        row=self.mapping.f.rows['ops'][0];row['mounts'].append(deepcopy(row['mounts'][0]))
        with self.assertRaisesRegex(previous.s.Failure,'OWNED_SERVICE_SET_UNVERIFIED'):self.plan.snapshot()

    def test_single_file_change_keeps_gate_and_reports_signature_group_only(self):
        first,_,_=self.plan.snapshot()
        role=next(k for k in first['files'] if k.startswith('host-configuration-'))
        source=Path(first['files'][role]['path'])
        self.between(lambda:source.write_text('changed ordinary synthetic configuration'))
        diag=self.failure()
        self.assertEqual(diag['changed_fields'],['generation.files.signature'])
        self.assertNotIn(str(source),json.dumps(diag));self.assertFalse(diag['values_returned'])

    def test_file_replacement_still_reports_identity_and_signature_change(self):
        unit=self.mapping.unit
        def replace():
            new=unit.with_suffix('.new');previous.captest.fixtures.write(new,'[Service]\n',0o600);new.replace(unit)
        self.between(replace);diag=self.failure()
        self.assertIn('generation.files.identity',diag['changed_fields'])
        self.assertIn('generation.files.signature',diag['changed_fields'])

    def test_restarted_container_is_refused_before_snapshot_comparison(self):
        original=self.mapping.docker.detail
        def changed(ident):
            row=original(ident);row[2]['StartedAt']='changed';return row
        self.mapping.docker.detail=changed
        with self.assertRaisesRegex(previous.s.Failure,'BACKUP_CONTAINER_CHANGED'):self.plan.snapshot()

    def test_protected_inventory_change_remains_blocking(self):
        snapshot=self.plan.snapshot;count=0
        def changed():
            nonlocal count
            result,inv,host=snapshot();count+=1
            if count==2:result['cold']['protected']['a'*64]='synthetic-new-start'
            return result,inv,host
        self.plan.snapshot=changed;diag=self.failure()
        self.assertEqual(diag['changed_fields'],['cold.protected'])

    def contracts(self):
        value,_,_=self.plan.snapshot()
        before=p.contract(value,self.mapping.maintenance,self.plan.preparation)
        return before,deepcopy(before)

    def test_changed_service_mount_and_state_fields_are_reported_without_values(self):
        for field,expected in [('mounts','cold.services.mounts'),('state','cold.services.state'),('owner','cold.services.identity')]:
            before,after=self.contracts();after['generation']['cold']['services']['ops-web'][field]='PRIVATE_VALUE'
            diag=p.snapshot_changes(before,after)
            self.assertEqual(diag['changed_fields'],[expected]);self.assertNotIn('PRIVATE',json.dumps(diag))

    def test_changed_storage_source_uid_and_topology_remain_distinct(self):
        for field,expected in [('source','cold.stores.binding'),('uid','cold.stores.identity'),('mount_topology_sha256','cold.stores.mount_topology')]:
            before,after=self.contracts();role=next(iter(after['generation']['cold']['stores']))
            after['generation']['cold']['stores'][role][field]='PRIVATE_VALUE'
            self.assertEqual(p.snapshot_changes(before,after)['changed_fields'],[expected])

    def test_changed_recipient_image_and_destination_are_not_ignored(self):
        for collection,field,expected in [('cold','recipients','cold.policy'),('generation','images','generation.images'),
                                          ('cold','destination','cold.destination')]:
            before,after=self.contracts()
            item=after['generation']['cold'] if collection=='cold' else after['generation']
            item[field]=['PRIVATE_VALUE']
            self.assertEqual(p.snapshot_changes(before,after)['changed_fields'],[expected])

    def test_unknown_roles_or_fields_are_reported_as_fixed_membership_or_metadata_groups(self):
        before,after=self.contracts();after['generation']['files']['PRIVATE_PATH_ROLE']={'PRIVATE_KEY':'PRIVATE_VALUE'}
        after['generation']['cold']['PRIVATE_KEY']='PRIVATE_VALUE';after['PRIVATE_KEY']='PRIVATE_VALUE'
        diag=p.snapshot_changes(before,after)
        self.assertEqual(diag['changed_fields'],['cold.other_metadata','generation.files.membership','other_contract_metadata'])
        self.assertNotIn('PRIVATE',json.dumps(diag));self.assertIsNotNone(p.safe_snapshot_diagnostic(diag))


class DiagnosticBoundary(unittest.TestCase):
    def test_report_keeps_comparison_gate_and_safe_change_fields(self):
        module=previous.mt.m
        exc=previous.s.Failure('BACKUP_GENERATION_DISCOVERY_CHANGED')
        exc.snapshot_diagnostic=dict(changed_fields=['cold.protected'],values_returned=False,retry_requested=False)
        def failed(): raise exc
        review=SimpleNamespace(reader=SimpleNamespace(calls=1),docker=SimpleNamespace(calls=2,base=SimpleNamespace(calls=3)),
                               backend=SimpleNamespace(control_commands=0))
        plan=SimpleNamespace(plan=failed,review=review,module=module,phase='snapshot_comparison')
        report=p.run(Path('/unread'),Path('/unread'),Path('/unread'),factory=lambda *args:plan)
        self.assertEqual(report['code'],'BACKUP_GENERATION_DISCOVERY_CHANGED')
        self.assertEqual(report['snapshot_diagnostic'],exc.snapshot_diagnostic)
        self.assertEqual(report['worker_service_control_commands'],0)

    def test_arbitrary_diagnostic_values_and_fields_are_dropped(self):
        good=dict(changed_fields=['cold.protected'],values_returned=False,retry_requested=False)
        for value in [dict(good,secret='PRIVATE'),dict(good,changed_fields=['PRIVATE']),
                      dict(good,changed_fields=['cold.protected','cold.protected']),
                      dict(good,values_returned=True),dict(good,retry_requested=True),
                      dict(good,changed_fields=[]),dict(good,changed_fields='cold.protected')]:
            self.assertIsNone(p.safe_snapshot_diagnostic(value))
        self.assertEqual(p.safe_snapshot_diagnostic(good),good)


if __name__=='__main__':unittest.main()
