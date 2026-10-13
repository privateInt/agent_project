"""Owned legacy recovery, bounded disappearance reads and one dynamic bridge."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops_tools import maintenance_acceptance as a
from ops_tools.tests.test_maintenance_acceptance import MetadataEngine
from ops_platform.contracts import ReadFailure


class NetworkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.directory,self.record=a.create_run(self.base,MetadataEngine())
        self.engine=object.__new__(a.Engine);self.calls=[]

    def legacy(self):
        # This is an exact reviewed older runner copy, not a self-declared hash.
        old=a.ROOT/'ops_tools/fixtures/maintenance_docker_diagnostics_20260930.txt'
        raw=old.read_bytes();self.assertEqual(a.digest(raw),a.LEGACY_CLEANUP_SHA256)
        (self.directory/'project'/a.ENTRY).write_bytes(raw)
        self.record.pop('fixture_network')
        for role,(_,subtree) in a.isolated.ROLES.items():
            rel=str(Path(subtree)/('acceptance-'+role+'.json'));p=self.directory/'project'/rel
            spec=json.loads(p.read_text());spec['networks']={'default':{'labels':{a.isolated.LABEL:self.record['owner']}}}
            a.isolated.write(p,spec);self.record['compose'][rel]['sha256']=a.digest(p.read_bytes())
        a.isolated.write(self.directory/'run.json',self.record)
        p=self.directory/'maintenance-run.json';marker=json.loads(p.read_text())
        marker['auxiliary_hashes'][a.ENTRY]=a.LEGACY_CLEANUP_SHA256;a.isolated.write(p,marker)

    def command(self,args,**kwargs):
        self.calls.append(args)
        if args[:2]==['network','ls']:return ''
        if args[:2]==['network','create']:return 'd'*64
        if args[:2]==['network','inspect']:return json.dumps(self.record['fixture_network'])
        self.fail(args)

    def test_all_seven_compose_projects_use_one_dynamic_owned_external_bridge(self):
        self.assertEqual(self.record['fixture_network'],{'name':self.record['prefix']+'-fixtures','driver':'bridge','owner':self.record['owner']})
        names=set();ports=[]
        for role,(_,subtree) in a.isolated.ROLES.items():
            spec=json.loads((self.directory/'project'/subtree/('acceptance-'+role+'.json')).read_text())
            self.assertEqual(spec['networks'],{'default':{'name':self.record['fixture_network']['name'],'external':True}})
            names.add(spec['networks']['default']['name'])
            ports.extend(s['ports'][0]['published'] for s in spec['services'].values())
        self.assertEqual(len(names),1);self.assertEqual(len(ports),8);self.assertEqual(len(set(ports)),8)
        self.assertEqual(json.loads((self.directory/'project/ops_platform/docker-compose.yml').read_text())['services']['ops-web']['network_mode'],'host')
        self.engine.command=self.command;self.engine.create_fixture_network(self.directory)
        creation=[x for x in self.calls if x[:2]==['network','create']]
        self.assertEqual(len(creation),1);self.assertNotIn('--subnet',creation[0])
        self.assertIn(a.isolated.LABEL+'='+self.record['owner'],creation[0])
        self.assertFalse(any('prune' in x or 'connect' in x for x in self.calls))

    def test_existing_owner_network_and_foreign_network_are_not_adopted(self):
        self.engine.command=lambda args,**kw:(self.calls.append(args) or 'd'*64)
        with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_ACCEPTANCE_NETWORK_ALREADY_PRESENT'):
            self.engine.create_fixture_network(self.directory)
        self.assertEqual(len(self.calls),1)
        row=dict(self.record['fixture_network'],owner='foreign')
        self.engine.command=lambda *args,**kw:json.dumps(row)
        with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_ACCEPTANCE_NETWORK_OWNERSHIP_CHANGED'):
            self.engine.verify_fixture_network(self.directory)

    def test_network_plan_tampering_is_rejected_even_with_updated_compose_digest(self):
        rel='model_platform/acceptance-model.json';p=self.directory/'project'/rel
        value=json.loads(p.read_text());value['networks']['default']['name']='foreign-network'
        a.isolated.write(p,value);self.record['compose'][rel]['sha256']=a.digest(p.read_bytes())
        a.isolated.write(self.directory/'run.json',self.record)
        with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_ACCEPTANCE_NETWORK_PLAN_CHANGED'):a.validate(self.directory)

    def test_unknown_network_create_outcome_does_not_retry_or_start_compose(self):
        def command(args,**kw):
            self.calls.append(args)
            if args[:2]==['network','ls']:return ''
            raise ReadFailure('ACCEPTANCE_COMMAND_UNCERTAIN')
        self.engine.command=command
        with self.assertRaisesRegex(ReadFailure,'ACCEPTANCE_COMMAND_UNCERTAIN'):
            self.engine.create_fixture_network(self.directory)
        self.assertEqual([c[:2] for c in self.calls],[['network','ls'],['network','create']])

    def test_only_exact_reviewed_legacy_copy_can_be_cleaned_and_never_started(self):
        self.legacy();self.assertEqual(a.validate(self.directory,legacy_cleanup=True)['owner'],self.record['owner'])
        with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_ACCEPTANCE_TOOL_CHANGED'):a.validate(self.directory)
        p=self.directory/'project'/a.ENTRY;p.write_bytes(p.read_bytes()+b'\n# modified\n')
        marker=json.loads((self.directory/'maintenance-run.json').read_text());marker['auxiliary_hashes'][a.ENTRY]=a.digest(p.read_bytes())
        a.isolated.write(self.directory/'maintenance-run.json',marker)
        with patch.object(a.native,'cleanup_unit_subprocess') as unit:
            with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_ACCEPTANCE_TOOL_CHANGED'):
                self.engine.cleanup(self.directory,legacy=True)
        unit.assert_not_called()

    def test_legacy_flag_cannot_be_used_for_worker_run_or_preflight(self):
        for action in ('worker','run','preflight'):
            with patch.object(sys,'argv',['maintenance_acceptance.py',action,'--legacy-cleanup']),patch.object(a,'preflight') as preflight:
                with patch('builtins.print') as output:self.assertEqual(a.main(),2)
            preflight.assert_not_called();self.assertIn('MAINTENANCE_LEGACY_CLEANUP_ONLY',str(output.call_args))

    def cleanup_engine(self,old_changed=False,protect_failure=False):
        self.engine.owned=lambda *args:[]
        row={'id':'e'*64,'started':'current','image':'sha256:'+'f'*64,'running':True}
        current={'e'*64:row};self.engine.snapshot=lambda *args:deepcopy(current)
        self.record['protected']={} if old_changed else deepcopy(current)
        a.isolated.write(self.directory/'run.json',self.record)
        self.engine.command=lambda args,**kw:(self.calls.append(args) or '')
        def protect(before):
            self.assertEqual(before,current)
            if protect_failure:raise ReadFailure('ACCEPTANCE_PROTECTED_CONTAINER_CHANGED')
        self.engine.protect=protect

    def test_legacy_cleanup_protects_current_containers_reports_history_difference_and_keeps_original_report(self):
        self.legacy();self.cleanup_engine(old_changed=True)
        p=self.directory/'maintenance-report.json';raw=b'{"result":"attention_required","code":"old_failure"}\n';p.write_bytes(raw);p.chmod(0o600)
        with patch.object(a.native,'cleanup_unit_subprocess',return_value={'result':'passed','unit_created':False,'worker_exit_verified':True}):
            answer=self.engine.cleanup(self.directory,legacy=True)
            again=self.engine.cleanup(self.directory,legacy=True)
        self.assertEqual(answer['result'],'passed');self.assertTrue(answer['owned_absence_verified'])
        self.assertEqual(answer['protection_scope'],'current_running_containers_during_cleanup')
        self.assertEqual(answer['prior_run_protected_difference']['added_running'],1)
        self.assertEqual(p.read_bytes(),raw);self.assertEqual(again['owned_containers_removed'],0)
        self.assertFalse(any('rm' in cmd or 'prune' in cmd for cmd in self.calls))

    def test_missing_historical_protected_object_is_not_inspected_or_claimed_preserved(self):
        self.cleanup_engine()
        self.record['protected']['b'*64]={'id':'b'*64,'started':'old','image':'sha256:'+'c'*64,'running':True}
        a.isolated.write(self.directory/'run.json',self.record)
        with patch.object(a.native,'cleanup_unit_subprocess',return_value={'result':'passed','worker_exit_verified':True}):
            answer=self.engine.cleanup(self.directory)
        self.assertFalse(answer['prior_run_protected_baseline_match'])
        self.assertEqual(answer['prior_run_protected_difference']['missing_or_stopped'],1)
        self.assertNotIn('b'*64,repr(self.calls))

    def test_unexpected_network_name_driver_or_attached_container_is_not_removed(self):
        for change,code in (({'name':'unrelated'},'MAINTENANCE_ACCEPTANCE_NETWORK_OWNERSHIP_CHANGED'),
                ({'driver':'overlay'},'MAINTENANCE_ACCEPTANCE_NETWORK_OWNERSHIP_CHANGED'),
                ({'containers':{'foreign':{}}},'ACCEPTANCE_NETWORK_IN_USE')):
            self.calls=[];self.cleanup_engine()
            net={'owner':self.record['owner'],'name':self.record['fixture_network']['name'],
                'driver':'bridge','containers':{}};net.update(change)
            def command(args,**kw):
                self.calls.append(args)
                if args[:2]==['network','ls']:return 'd'*64
                if args[:2]==['network','inspect']:return json.dumps(net)
                self.fail(args)
            self.engine.command=command
            with patch.object(a.native,'cleanup_unit_subprocess',return_value={'result':'passed','worker_exit_verified':True}):
                with self.assertRaisesRegex(ReadFailure,code):self.engine.cleanup(self.directory)
            self.assertFalse(any(cmd[:2]==['network','rm'] for cmd in self.calls))

    def test_failure_after_owned_absence_preserves_cleanup_progress_and_no_saved_pid_signal(self):
        self.cleanup_engine(protect_failure=True)
        with patch.object(a.native,'cleanup_unit_subprocess',return_value={'result':'passed','worker_exit_verified':True}):
            with self.assertRaises(ReadFailure) as caught:self.engine.cleanup(self.directory)
        report=a.error_report(caught.exception)
        self.assertTrue(report['cleanup_progress']['owned_absence_verified']);self.assertTrue(report['cleanup_progress']['worker_exit_verified'])
        self.assertFalse(any('kill' in cmd or 'prune' in cmd for cmd in self.calls))

    def test_confirmed_disappearing_owned_container_only_retries_reads_once(self):
        failure=ReadFailure('ACCEPTANCE_DOCKER_COMMAND_FAILED');failure.docker_diagnostic={'reason':'CONTAINER_NOT_FOUND'}
        self.engine.ids=lambda *args,**kw:['b'*64]
        with patch.object(a.isolated.Engine,'owned',side_effect=[failure,[]]) as collect:
            self.assertEqual(self.engine.owned(self.directory,self.record),[]);self.assertEqual(collect.call_count,2)
        with patch.object(a.isolated.Engine,'snapshot',side_effect=[failure,{}]) as collect:
            self.assertEqual(self.engine.snapshot(),{});self.assertEqual(collect.call_count,2)
        with patch.object(a.isolated.Engine,'snapshot',side_effect=failure) as collect:
            with self.assertRaises(ReadFailure):self.engine.snapshot(['b'*64])
            self.assertEqual(collect.call_count,1)

    def test_unknown_inspect_error_and_repeated_disappearance_are_never_silently_skipped(self):
        for reason,expected_calls in (('UNKNOWN',1),('CONTAINER_NOT_FOUND',2)):
            failure=ReadFailure('ACCEPTANCE_DOCKER_COMMAND_FAILED');failure.docker_diagnostic={'reason':reason}
            with patch.object(a.isolated.Engine,'owned',side_effect=failure) as collect:
                with self.assertRaises(ReadFailure):self.engine.owned(self.directory,self.record)
                self.assertEqual(collect.call_count,expected_calls)

    def test_historical_protection_change_cannot_turn_native_acceptance_into_pass(self):
        class Engine:
            def create_fixture_network(self,*args):pass
            def compose(self,*args):pass
            def cleanup(self,*args):return {'result':'passed','prior_run_protected_baseline_match':False,'owned_absence_verified':True}
        with patch.object(a,'preflight'),patch.object(a,'Engine',return_value=Engine()),\
                patch.object(a,'create_run',return_value=(self.directory,self.record)),\
                patch.object(a,'invoke',return_value={'result':'passed','stage':a.STAGE}):
            value=a.run(self.base)
        self.assertEqual(value['result'],'attention_required');self.assertEqual(value['code'],'ACCEPTANCE_PROTECTED_CONTAINER_CHANGED')


if __name__=='__main__':unittest.main()
