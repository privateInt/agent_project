"""Direct measurement inventory/CLI tests; synthetic daemon, real helper pipe."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

from src.ops.tools import backup_measure as m, backup_execute as e
from src.ops.candidates.cold_storage_20260930.tests import test_storage as fixture


class Engine:
    """Protocol seam for native Docker methods, never a real Docker daemon."""
    def __init__(self,f):
        self.f=f;self.rows=deepcopy(f.rows);self.protected=deepcopy(f.protected)
        self.helpers={};self.calls=[];self.mode='good';self.bad_helper=False
    def state(self,ident):
        if ident in self.helpers:
            row=self.helpers[ident]
            if row.get('child'):
                child=row['child'];code=child.poll()
                return dict(Running=code is None,Status='running' if code is None else 'exited',
                            Pid=child.pid if code is None else 0,ExitCode=code or 0,OOMKilled=False,StartedAt='helper-start')
            return row['state']
        if ident in self.protected:
            return dict(Running=True,StartedAt=self.protected[ident])
        return next(row['state'] for row in self.rows.values() if row['container_id']==ident)
    def __call__(self,argv,env,seconds):
        self.assert_endpoint=argv[1:3]==['--host',self.f.host['endpoint']]
        assert self.assert_endpoint
        args=argv[3:];self.calls.append(args)
        def data(value):return 0,json.dumps(value).encode(),b''
        if args[0]=='info':return data([self.f.host['daemon_id'],'linux',[]])
        if args[:3]==['image','inspect','--format']:return data([args[-1],None])
        if args==['ps','-q','--no-trunc']:
            if self.mode=='ps_error':return 2,b'sensitive-response',b'sensitive-error'
            ids=list(self.protected)+[r['container_id'] for r in self.rows.values() if r['state']['Running']]
            ids += [ident for ident in self.helpers if self.state(ident)['Running']]
            if self.mode=='duplicate_list':ids+=ids[:1]
            if self.mode=='too_many':ids=[format(i,'064x') for i in range(161)]
            return 0,'\n'.join(ids).encode(),b''
        if args[:3]==['container','inspect','--format']:
            fmt=args[3];ids=args[4:]
            if fmt==m.LIVE_FORMAT:
                if self.mode=='inspect_error':return 2,b'sensitive-response',b'sensitive-error'
                rows=[[ident,self.state(ident)['Running'],self.state(ident)['StartedAt']] for ident in reversed(ids)]
                if self.mode=='exited_between_samples':rows[0][1]=False
                if self.mode=='scope_restart_between_samples':
                    next(row for row in rows if row[0]==self.f.rows['database']['container_id'])[2]='late-scoped-restart'
                if self.mode=='duplicate_row':rows[-1]=rows[0]
                if self.mode=='bad_timestamp':rows[0][2]=[]
                if self.mode=='bad_running_type':rows[0][1]=1
                if self.mode=='missing_row':rows.pop()
                return 0,'\n'.join(json.dumps(row) for row in rows).encode(),b''
            ident=ids[0]
            if ident in self.helpers:
                helper=self.helpers[ident];spec=helper['spec']
                if fmt==e.s.HELPER_FORMAT:
                    controls=[f"{spec['uid']}:{spec['gid']}",[spec['python']],
                        ['-I','-B','-c',spec['script'],json.dumps(spec['config'],sort_keys=True)],False,True,'none',
                        ['ALL'],['no-new-privileges=true'],False,'','none','none',32,268435456,1000000000]
                    if self.bad_helper:controls[4]=False
                    return data(controls)
                assert fmt==m.CONTAINER_FORMAT
                return data([ident,spec['image_id'],{e.s.LABEL:spec['job'],e.s.SPEC_LABEL:spec['spec_sha256']},
                    [{'Type':'bind','Source':spec['source'],'Destination':'/source','RW':False}],
                    self.state(ident),None,'','no'])
            assert fmt==m.CONTAINER_FORMAT
            row=next(r for r in self.rows.values() if r['container_id']==ident)
            labels={row['owner_label']:row['owner'],'com.docker.compose.project':row['project'],
                    'com.docker.compose.service':row['service']}
            return data([ident,row['image_id'],labels,row['mounts'],row['state'],row['health'],row['userns'],row['restart']])
        if args[:2]==['container','create']:
            config=json.loads(args[-1]);spec=m.recipe(self.f.proposal,config['role'],config['id'],config['seconds'])
            assert args[args.index('--name')+1]==spec['name']
            ident='c'*64;self.helpers[ident]={'spec':spec,'state':dict(Running=False,Status='created',Pid=0,ExitCode=0,OOMKilled=False,StartedAt='')}
            return 0,(ident+'\n').encode(),b''
        if args[:2]==['container','rm']:
            self.helpers.pop(args[-1]);return 0,b'',b''
        if args[:2]==['container','kill']:
            e.g.a.Age.stop(self.helpers[args[-1]].get('child'));return 0,b'',b''
        if args[:4]==['ps','-aq','--no-trunc','--filter']:
            return 0,'\n'.join(self.helpers).encode(),b''
        raise AssertionError('unreviewed test daemon command')


class Inventory(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Files();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.engine=Engine(self.f);self.backend=m.Docker(self.f.host,sys.executable,runner=self.engine)
        self.backend.phase='pre_helper_baseline'
        self.scope=m.Scope(self.f.scope.descriptor,self.backend.baseline)
        self.backend.bind(self.f.proposal,self.scope,time.monotonic()+60)
        self.addCleanup(self.stop_children)
    def stop_children(self):
        for helper in self.engine.helpers.values():
            child=helper.get('child');e.g.a.Age.stop(child)
            if child is not None and child.stderr is not None:child.stderr.close()
    def fail(self,code):
        with self.assertRaisesRegex(e.setup.Failure,code) as caught:self.backend.baseline()
        value=m.safe_diagnostic(caught.exception.measure_diagnostic);self.assertIsNotNone(value)
        rendered=json.dumps(value)
        self.assertNotIn('sensitive',rendered);self.assertNotIn('f'*64,rendered)
        self.assertFalse(value['values_returned']);self.assertFalse(value['retry_requested'])
        return value
    def test_stable_baseline_uses_single_running_inspect_batch_and_no_mutation(self):
        self.backend.baseline()
        batches=[a for a in self.engine.calls if a[:4]==['container','inspect','--format',m.LIVE_FORMAT]]
        self.assertEqual(len(batches),1);self.assertEqual(len(batches[0][4:]),3)
        self.assertFalse(any(a[:2] in (['container','create'],['container','stop'],['container','start']) for a in self.engine.calls))
    def test_added_protected_container_fails_before_helper_creation(self):
        self.engine.protected['d'*64]='new-unrelated'
        value=self.fail('PROTECTED_CHANGED');self.assertEqual(value['counts']['added_count'],1)
        self.assertEqual(value['counts']['removed_count'],0);self.assertFalse(self.engine.helpers)
    def test_missing_or_restarted_protected_container_remains_blocked(self):
        self.engine.protected={};value=self.fail('PROTECTED_CHANGED')
        self.assertEqual(value['counts']['removed_count'],1)
        self.engine.protected=deepcopy(self.f.protected);self.engine.protected['f'*64]='new-start'
        value=self.fail('PROTECTED_CHANGED');self.assertEqual(value['counts']['started_at_changed_count'],1)
    def test_unknown_helper_shaped_container_is_still_protected(self):
        # A plausible name/label is never sufficient for exclusion/adoption.
        self.engine.protected['a'*64]='ops-measure-unknown'
        self.fail('PROTECTED_CHANGED');self.assertFalse(self.backend.owned_helpers)
    def test_current_created_helper_excluded_only_after_security_verification(self):
        spec=m.recipe(self.f.proposal,'data',str(uuid.uuid4()),10);ident=self.backend.create(spec)
        self.engine.helpers[ident]['state'].update(Running=True,Status='running',Pid=123,StartedAt='helper-start')
        self.backend.baseline();self.assertEqual(len(self.backend.owned_helpers),1)
        self.engine.bad_helper=True
        with self.assertRaisesRegex(e.g.a.Failure,'SECURITY_CHANGED'):self.backend.baseline()
    def test_reviewed_and_validated_plan_produce_identical_helper_recipe(self):
        identifier=str(uuid.uuid4());reviewed=m.recipe(self.f.proposal,'data',identifier,10)
        validated=m.recipe(self.backend.contract,'data',identifier,10)
        self.assertEqual(reviewed,validated)
        self.assertEqual(reviewed['config']['quiescence_identity'],self.f.proposal['review_id'])
    def test_modified_reviewed_plan_rejected_before_helper_recipe_or_create(self):
        cold=deepcopy(self.f.proposal);cold['max_bytes']+=1
        with self.assertRaisesRegex(e.setup.Failure,'COLD_REVIEW_CHANGED'):
            m.recipe(cold,'data',str(uuid.uuid4()),10)
        self.assertFalse(self.engine.helpers);self.assertFalse(self.engine.calls)
    def test_scope_mount_or_service_restart_blocks_comparison_without_rebind(self):
        row=self.engine.rows['database'];old=row['state']['StartedAt'];row['state']['StartedAt']='restarted'
        with self.assertRaisesRegex(e.setup.Failure,'SERVICE_CHANGED'):self.backend.baseline()
        row['state']['StartedAt']=old;row['mounts'][0]['RW']=False
        with self.assertRaisesRegex(e.setup.Failure,'SERVICE_CHANGED'):self.backend.baseline()
    def test_exited_between_list_and_inspect_fails_with_snapshot_reason(self):
        self.engine.mode='exited_between_samples';value=self.fail('RUNNING_SNAPSHOT_CHANGED')
        self.assertEqual(value['reason'],'listed_container_not_running')
        self.assertIsNone(value['counts']['removed_count'])
    def test_scope_restart_during_running_batch_blocks_before_helper_creation(self):
        self.engine.mode='scope_restart_between_samples'
        with self.assertRaisesRegex(e.setup.Failure,'SERVICE_CHANGED'):self.backend.baseline()
        self.assertFalse(self.engine.helpers)
    def test_invalid_or_partial_native_metadata_is_never_treated_as_baseline(self):
        for mode in ('duplicate_list','too_many','duplicate_row','bad_timestamp','bad_running_type','missing_row'):
            self.engine.mode=mode
            with self.subTest(mode=mode):self.fail('RUNNING_SNAPSHOT_CHANGED')
    def test_metadata_command_errors_disclose_no_raw_stdout_or_stderr(self):
        for mode in ('ps_error','inspect_error'):
            self.engine.mode=mode
            with self.subTest(mode=mode):self.fail('RUNNING_SNAPSHOT_CHANGED')
    def attach(self,ident,spec):
        self.backend.mutation(spec=spec);self.backend.helper(ident,spec)
        script=spec['script'].replace("os.open('/source',",'os.open('+repr(spec['source'])+',')
        child=subprocess.Popen([sys.executable,'-I','-B','-c',script,json.dumps(spec['config'])],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            pass_fds=(self.scope.descriptor,),start_new_session=True)
        self.engine.helpers[ident]['child']=child;return child
    def test_full_helper_pipe_runs_actual_baseline_at_create_attach_and_after_cleanup(self):
        self.backend.attach=self.attach
        result=m.cold_size(self.backend,self.f.proposal,'data',10)
        self.assertEqual(result['logical_bytes'],(self.f.src/'record').stat().st_size)
        self.assertFalse(self.backend.owned_helpers);self.assertFalse(self.engine.helpers)
        self.assertEqual(self.backend.phase,'post_helper_baseline')
        batches=[a for a in self.engine.calls if len(a)>3 and a[3]==m.LIVE_FORMAT]
        self.assertEqual(len(batches),4)
    def test_protected_change_after_helper_cleanup_preserves_failure_and_no_service_effects(self):
        def attach(ident,spec):
            child=self.attach(ident,spec);self.engine.protected['d'*64]='late-new';return child
        self.backend.attach=attach
        with self.assertRaisesRegex(e.setup.Failure,'PROTECTED_CHANGED') as caught:
            m.cold_size(self.backend,self.f.proposal,'data',10)
        value=caught.exception.measure_diagnostic
        self.assertEqual(value['phase'],'post_helper_baseline');self.assertEqual(value['counts']['added_count'],1)
        self.assertFalse(self.backend.owned_helpers);self.assertFalse(self.engine.helpers)
        self.assertFalse(any(a[:2] in (['container','stop'],['container','start']) for a in self.engine.calls))
    def test_failure_diagnostic_reaches_cli_without_traceback_or_identifiers(self):
        self.engine.protected['d'*64]='new'
        try:self.backend.baseline()
        except e.setup.Failure as exc:error=exc
        fake=type('Executor',(),{'measure':lambda self,seconds:(_ for _ in ()).throw(error)})()
        out=io.StringIO()
        with patch.object(e,'Execute',return_value=fake),patch('sys.stdout',out):result=e.main(['measure'])
        self.assertEqual(result,2);report=json.loads(out.getvalue())
        self.assertEqual(report['measure_diagnostic']['counts']['added_count'],1)
        self.assertNotIn('d'*64,out.getvalue());self.assertFalse(report['operation_replayed'])
    def test_diagnostic_filter_rejects_arbitrary_values_and_unhashable_inputs(self):
        self.engine.protected['d'*64]='new';value=self.fail('PROTECTED_CHANGED')
        for key,new in (('phase',[]),('reason',{}),('schema',True),('counts',{'container_id':'f'*64}),('values_returned',True)):
            changed=deepcopy(value);changed[key]=new
            self.assertIsNone(m.safe_diagnostic(changed))
        changed=deepcopy(value);changed['counts']['added_count']=True;self.assertIsNone(m.safe_diagnostic(changed))
        changed=deepcopy(value);changed['counts']['added_count']=161;self.assertIsNone(m.safe_diagnostic(changed))


if __name__=='__main__':unittest.main()
