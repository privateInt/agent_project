"""Persistence diagnostics: synthetic CLI metadata and private real state files."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from src.ops.tools import check_persistence as check
from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import service_lifecycle as service, worker_state as ws, refresh_worker as worker
from src.ops.platform.ops import lifecycle_probe as probe
from src.ops.platform.tests import test_auto_refresh as preflight, test_service_lifecycle as lifecycle


def capture():
    binding = {'fixture': 'unchanged'}
    manifest = {'persistence': 'session', 'binding': binding}
    manager = {'status': 'owned', 'unit_name': 'fixture', 'persistence': 'session', 'enabled': False,
        'desired_state': 'running', 'recovery': None, 'current_invocation_verified': True,
        'evaluation_status': 'healthy', 'provider_status': {'ActiveState': 'active', 'MainPID': '10', 'InvocationID': 'b'*32},
        'worker': {'control': {'desired': 'running', 'recovery': None}, 'intent': None,
                   'last_observation': {'completed_cycles': 3, 'checked_at': 'then'}}}
    return {'manifest': manifest, 'worker_manifest': {'binding': deepcopy(binding)}, 'actual_binding': deepcopy(binding),
        'code_sha256': check.REVIEWED_CODE_SHA256, 'errors': [],
        'restart_policy': {'status': 'ok', 'name': 'unless-stopped', 'daemon_restart_candidate': True,
                           'requires_container_not_deliberately_stopped': True, 'actual_boot_start_verified': False},
        'report': {'installation': {'status': 'ok', 'configuration': {'files_fingerprint': 'same'},
                                    'provenance': {'record_fingerprint': 'same'}},
            'management': manager, 'docker': {'status': 'ok', 'ops': {'status': 'ok'}},
            'systemd': {'status': 'ok', 'linger': False, 'manager_version': 249, 'environment': {'source': 'discovered'}},
            'persistence': {'selected': 'session'}, 'blockers': [], 'warnings': ['SESSION_SCOPE_ONLY'],
            'result': 'passed', 'prepared_state_stable': True}}


class ProjectionTests(unittest.TestCase):
    def setUp(self): self.first = capture(); self.after = None
    def invoke(self, target='boot'):
        rows = iter([deepcopy(self.first), deepcopy(self.after or self.first)])
        source = SimpleNamespace(take=lambda: next(rows))
        return check.run('/synthetic/server', target, source_factory=lambda _: source)

    def test_boot_attention_is_completed_diagnosis_not_failed_service(self):
        r=self.invoke(); self.assertEqual((r['result'],r['diagnostic_status']),('attention_required','completed'))
        self.assertEqual(r['current_service']['evaluation_status'],'healthy')
        self.assertEqual(r['blocking_reasons']['implementation'],list(check.GAPS))
        self.assertFalse(r['boot_ready']); self.assertFalse(r['activation_supported'])
        self.assertTrue(r['snapshot_stable']); self.assertEqual(r['policy']['effective_saved_policy'],'session')

    def test_linger_true_does_not_override_saved_policy_or_claim_boot_ready(self):
        self.first['report']['systemd']['linger']=True
        self.first['report']['persistence']['selected']='boot'
        r=self.invoke(); self.assertTrue(r['environment']['boot_manager_available'])
        self.assertEqual(r['policy']['saved'],'session'); self.assertEqual(r['policy']['environment_proposal'],'boot')
        self.assertIn('SAVED_POLICY_DIFFERS_FROM_REQUEST',r['blockers']); self.assertFalse(r['boot_ready'])

    def test_unknown_linger_and_unavailable_manager_remain_distinct(self):
        self.first['report']['systemd'].update(linger='unknown',status='unavailable')
        r=self.invoke(); self.assertIn('LINGER_UNDETERMINED',r['blockers']); self.assertIn('SYSTEMD_USER_MANAGER_UNAVAILABLE',r['blockers'])

    def test_existing_boot_links_still_do_not_prove_worker_boot_support(self):
        self.first['manifest']['persistence']='boot'; self.first['report']['management'].update(persistence='boot',enabled=True)
        self.first['report']['systemd']['linger']=True
        r=self.invoke(); self.assertFalse(r['policy']['transition_required']); self.assertFalse(r['boot_ready'])
        self.assertIn('BOOT_REENTRY_NOT_IMPLEMENTED',r['blockers'])

    def test_matching_session_can_pass_only_metadata_scope(self):
        r=self.invoke('session'); self.assertEqual(r['result'],'passed'); self.assertFalse(r['boot_ready'])
        self.assertFalse(r['logout_or_boot_acceptance']); self.assertFalse(r['long_running_acceptance'])

    def test_pause_recovery_open_intent_not_cleared(self):
        c=self.first['report']['management']; c.update(desired_state='paused',recovery='FIXTURE')
        c['worker']['intent']={'phase':'open'}
        r=self.invoke('session'); self.assertTrue({'WORKER_PAUSED','WORKER_RECOVERY_REQUIRED','WORKER_UNFINISHED_INTENT'}<=set(r['blockers']))
        self.assertEqual(c['worker']['intent']['phase'],'open')

    def test_unknown_code_review_never_claims_known_capabilities(self):
        self.first['code_sha256']='f'*64
        r=self.invoke(); self.assertIsNone(r['worker_capabilities']['boot_reentry'])
        self.assertIn('WORKER_BOOT_CAPABILITIES_UNREVIEWED',r['blockers'])
        self.assertNotIn('BOOT_REENTRY_NOT_IMPLEMENTED',r['blockers'])

    def test_both_persisted_bindings_and_runtime_are_compared(self):
        self.first['worker_manifest']['binding']={'fixture':'wrong'}
        self.first['actual_binding']={'fixture':'changed'}
        r=self.invoke(); self.assertIn('SERVICE_WORKER_BINDING_MISMATCH',r['blockers']); self.assertIn('SERVICE_BINDING_CHANGED',r['blockers'])

    def test_policy_control_provenance_and_process_changes_invalidate_snapshot(self):
        mutations=[lambda x:x['manifest'].update(persistence='boot'),
            lambda x:x['report']['management']['worker']['control'].update(service_stop_token='new'),
            lambda x:x['report']['installation']['provenance'].update(record_fingerprint='new'),
            lambda x:x['report']['management']['provider_status'].update(InvocationID='c'*32),
            lambda x:x['restart_policy'].update(name='always')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.after=deepcopy(self.first); mutation(self.after)
                r=self.invoke(); self.assertFalse(r['snapshot_stable']); self.assertIn('PERSISTENCE_SNAPSHOT_CHANGED',r['blockers'])

    def test_advancing_heartbeat_and_cycles_do_not_invalidate_policy_snapshot(self):
        self.after=deepcopy(self.first)
        self.after['report']['management']['worker']['last_observation'].update(completed_cycles=4,checked_at='now')
        self.after['report']['management']['evaluation_status']='in_progress'
        r=self.invoke(); self.assertTrue(r['snapshot_stable']); self.assertEqual(r['last_service']['evaluation_status'],'in_progress')

    def test_observation_warning_preserved(self):
        self.first['report']['warnings'].append('EXTERNAL_REFERENCE_PRIVATE_MODE_DIFFERS')
        self.first['report']['management']['evaluation_status']='degraded_retrying'
        r=self.invoke(); self.assertIn('EXTERNAL_REFERENCE_PRIVATE_MODE_DIFFERS',r['warnings']); self.assertIn('WORKER_EVALUATION_DEGRADED',r['warnings'])

    def test_degradation_at_second_read_is_not_hidden_by_first_healthy_read(self):
        self.after=deepcopy(self.first); self.after['report']['management']['evaluation_status']='degraded_retrying'
        r=self.invoke(); self.assertEqual(r['initial_service']['evaluation_status'],'healthy')
        self.assertEqual(r['current_service']['evaluation_status'],'degraded_retrying')
        self.assertIn('WORKER_EVALUATION_DEGRADED',r['warnings'])

    def test_missing_service_and_nonboot_restart_policy_are_explicit(self):
        self.first.update(manifest=None,worker_manifest=None,actual_binding=None)
        self.first['report']['management']={'status':'not_installed'}
        self.first['restart_policy'].update(name='on-failure',daemon_restart_candidate=False)
        r=self.invoke(); self.assertIsNone(r['policy']['saved']); self.assertIn('OWNED_SERVICE_REQUIRED',r['blockers'])
        self.assertIn('OPS_RESTART_POLICY_NOT_BOOT_CANDIDATE',r['blockers'])

    def test_failure_after_first_read_retains_projection_without_raw_error(self):
        count=0
        def take():
            nonlocal count
            count+=1
            if count==2:raise OSError('fixture-private-text')
            return deepcopy(self.first)
        r=check.run('/synthetic',source_factory=lambda _:SimpleNamespace(take=take))
        self.assertEqual(r['diagnostic_status'],'incomplete'); self.assertEqual(r['code'],'INVALID_LOCAL_STATE')
        self.assertEqual(r['policy']['saved'],'session'); self.assertNotIn('fixture-private-text',json.dumps(r))

    def test_invalid_target_never_constructs_source(self):
        r=check.run('/synthetic','invalid',source_factory=lambda _:self.fail('source opened'))
        self.assertEqual(r['code'],'INVALID_PERSISTENCE_REQUEST')


class MetadataIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.h=preflight.PreflightTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.f=self.h.f; self.directory=self.f.directory
        self.owner=ws.read(self.directory/'owner.json')
        self.unit='agent-project-ops-refresh-'+self.owner['owner'].replace('-','')+'.service'
        self.manager=lifecycle.Manager(self.f.root/'config-units',self.unit)
        self.h.reader.original['XDG_CONFIG_HOME']=str(self.manager.root)
        raw=self.h.commands.raw
        self.binding={'directory':str(self.directory),'owner':self.owner,'code_sha256':worker.code_hash(worker.ROOT),
            'python':str(Path(sys.executable).resolve()),'python_version':list(sys.version_info[:3]),
            'docker_path':self.h.reader.paths['docker'],'daemon_id':'synthetic-daemon',
            'ops_container_id':raw['id'],'ops_image_id':raw['image']}
        report={'blockers':[],'docker':{'endpoint':self.h.commands.context_endpoint},'persistence':{'selected':'session','linger':True}}
        r=service.run('install',self.directory,reader=self.h.reader,backend=self.manager,
            preflight_fn=lambda *a,**kw:deepcopy(report),snapshot_fn=lambda _:deepcopy(self.binding))
        self.assertEqual(r['result'],'passed',r)
        self.manager.active=True; self.manager.pid=str(worker.process_identity()['proc_pid']); self.manager.invocation=uuid.uuid4().hex
        store=ws.Store(self.directory); control=store.control(); control['desired']='running'; ws.write(store.path/'control.json',control)
        store.status(phase='waiting',identity=worker.process_identity(),service_invocation=self.manager.invocation,
            invocation='fixture-worker',completed_cycles=3,outcome={'classification':'accepted','result':'passed','child_process_group_stopped':True})
        self.policy='unless-stopped'; self.reply_override=None; self.extra_calls=[]
        def runner(args,**kwargs):
            tool=Path(args[0]).name; tail=args[1:]
            if tool=='systemctl' and tail[-1]==self.unit:
                fields=tail[-2].split('=',1)[1].split(','); row=self.manager.inspect()
                return SimpleNamespace(returncode=0,stdout='\n'.join(k+'='+row.get(k,'') for k in fields),stderr='')
            if tool=='docker' and check.extra_query(tail):
                self.extra_calls.append(tail)
                if self.reply_override is not None:return self.reply_override(args,kwargs)
                return SimpleNamespace(returncode=0,stdout=json.dumps({'id':raw['id'],'image':raw['image'],
                    'restart_policy':{'Name':self.policy,'MaximumRetryCount':0}}),stderr='')
            return self.h.commands(args,**kwargs)
        self.h.reader.runner=runner
    def invoke(self,target='boot',directory=None):
        return check.run(directory or self.directory,target,source_factory=lambda d:check.Source(d,reader=self.h.reader))

    def test_real_state_and_adapter_queries_preserve_all_files_and_never_read_secrets(self):
        before=preflight.tree(self.f.root); controls=self.manager.control_commands
        original=os.open; original_io=io.open
        forbidden={'access-key','agent-key','lf.json','api-success.json'}
        def guarded(path,flags,*args,**kwargs):
            self.assertFalse(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
            if isinstance(path,(str,Path)):self.assertNotIn(Path(path).name,forbidden)
            return original(path,flags,*args,**kwargs)
        def guarded_io(path,mode='r',*args,**kwargs):
            if isinstance(path,(str,Path)):self.assertNotIn(Path(path).name,forbidden)
            self.assertFalse(any(c in mode for c in 'wax+'))
            return original_io(path,mode,*args,**kwargs)
        with (patch('os.open',side_effect=guarded), patch.object(ws,'write',side_effect=AssertionError('write')),
              patch('io.open',side_effect=guarded_io),
              patch('src.ops.platform.transport.secret',side_effect=AssertionError('secret')),
              patch('src.ops.platform.transport.Transport.get',side_effect=AssertionError('API'))):
            r=self.invoke()
        self.assertEqual(r['diagnostic_status'],'completed',r); self.assertTrue(r['snapshot_stable'],r)
        self.assertEqual(r['binding']['status'],'verified',r); self.assertTrue(r['current_service']['current_invocation_verified'])
        self.assertEqual(before,preflight.tree(self.f.root)); self.assertEqual(self.manager.control_commands,controls)
        self.assertEqual(len(self.extra_calls),2); self.assertNotIn('synthetic-ops-access-key',json.dumps(r))

    def test_real_missing_state_creates_no_directory(self):
        directory=self.f.root/'missing'/'server'; r=self.invoke(directory=directory)
        self.assertIn('PREPARED_STATE_NOT_FOUND',r['blockers']); self.assertFalse(directory.parent.exists()); self.assertFalse(self.extra_calls)

    def test_unit_edit_not_repaired(self):
        self.manager.path.write_text('manual unit')
        r=self.invoke(); self.assertIn('SERVICE_UNIT_CHANGED',r['blockers']); self.assertEqual(self.manager.path.read_text(),'manual unit')

    def test_foreign_enable_link_not_repaired(self):
        manifest=service.load(self.directory); link=service.enabled_path(manifest)
        link.parent.mkdir(mode=0o700);link.symlink_to('/synthetic/foreign.service')
        r=self.invoke();self.assertIn('SERVICE_ENABLE_LINK_CONFLICT',r['blockers']);self.assertEqual(os.readlink(link),'/synthetic/foreign.service')

    def test_unsafe_private_owner_mode_is_not_changed(self):
        p=self.directory/'owner.json';p.chmod(0o644)
        r=self.invoke();self.assertIn('PRIVATE_STATE_REQUIRED',r['blockers']);self.assertEqual(p.stat().st_mode&0o777,0o644)

    def test_owner_symlink_is_refused_without_dereference_or_repair(self):
        p=self.directory/'owner.json';target=self.f.root/'owner-original';p.rename(target);p.symlink_to(target)
        before=target.read_bytes();r=self.invoke()
        self.assertEqual(r['result'],'attention_required');self.assertFalse(self.extra_calls)
        self.assertTrue(p.is_symlink());self.assertEqual(target.read_bytes(),before)

    def test_boolean_retry_count_and_unknown_policy_are_refused(self):
        raw=self.h.commands.raw
        for name,count in [('always',True),('invalid',0)]:
            with self.subTest(name=name):
                self.reply_override=lambda a,k:SimpleNamespace(returncode=0,stdout=json.dumps({'id':raw['id'],'image':raw['image'],
                    'restart_policy':{'Name':name,'MaximumRetryCount':count}}),stderr='')
                self.assertIn('INVALID_DOCKER_RESTART_POLICY',self.invoke()['blockers'])

    def test_all_restart_policies_are_metadata_only(self):
        for name,candidate in [('no',False),('on-failure',False),('always',True),('unless-stopped',True)]:
            with self.subTest(name=name):
                self.policy=name; r=self.invoke()
                self.assertEqual(r['environment']['ops_restart_policy']['daemon_restart_candidate'],candidate)
                self.assertFalse(r['boot_ready'])

    def test_container_identity_swap_and_invalid_reply_refused(self):
        for stdout,code in [('{}','DOCKER_IDENTITY_CHANGED'),('not-json','INVALID_RESPONSE')]:
            with self.subTest(stdout=stdout):
                self.reply_override=lambda a,k:SimpleNamespace(returncode=0,stdout=stdout,stderr='private')
                r=self.invoke(); self.assertIn(code,r['blockers']); self.assertNotIn('private',json.dumps(r))

    def test_timeout_nonzero_and_response_limit_keep_bounded_diagnostics(self):
        for mode,code in [('timeout','DOCKER_RESTART_POLICY_QUERY_TIMEOUT'),('fail','DOCKER_RESTART_POLICY_QUERY_FAILED'),('large','DOCKER_RESTART_POLICY_RESPONSE_TOO_LARGE')]:
            with self.subTest(mode=mode):
                def reply(args,kwargs):
                    if mode=='timeout':raise subprocess.TimeoutExpired(args,5,stderr='private-timeout')
                    return SimpleNamespace(returncode=1 if mode=='fail' else 0,stdout='x'*16385 if mode=='large' else '',stderr='private-stderr')
                self.reply_override=reply;r=self.invoke();self.assertIn(code,r['blockers']);self.assertNotIn('private-stderr',json.dumps(r))

    def test_mismatched_worker_binding_detected_without_repair(self):
        store=ws.Store(self.directory); path=store.path/'owner.json'; row=store.manifest()
        row['binding']['python_version']=[9,9,9];ws.write(path,row);before=path.read_bytes()
        r=self.invoke();self.assertIn('SERVICE_WORKER_BINDING_MISMATCH',r['blockers']);self.assertEqual(path.read_bytes(),before)

    def test_readonly_runner_rejects_mutations_before_dispatch(self):
        guard=check.ReadOnlyRunner(lambda *a,**kw:self.fail('dispatched'));guard.paths=dict(self.h.reader.paths)
        for tool,args in [('systemctl',['--user','start',self.unit]),('systemctl',['--user','daemon-reload']),
            ('loginctl',['enable-linger',str(os.getuid())]),('docker',['--host','unix:///fixture.sock','update','--restart','always','x']),
            ('docker',['--host','unix:///fixture.sock','exec','x','true']),('docker',['inspect','x'])]:
            with self.subTest(args=args), self.assertRaises(ReadFailure):guard([guard.paths[tool],*args])

    def test_discovered_nondefault_paths_and_parent_environment_preserved(self):
        before=dict(os.environ);r=self.invoke()
        self.assertEqual(dict(os.environ),before);self.assertEqual(r['installation']['installation_id'],self.owner['owner'])
        self.assertEqual(r['environment']['docker']['daemon_id'],'synthetic-daemon')
        self.assertTrue(all(args[1]==self.h.commands.context_endpoint for args in self.extra_calls))

    def test_docker_unavailable_retains_service_policy_without_false_binding_success(self):
        self.h.commands.fail=lambda tool,args:tool=='docker' and 'info' in args
        r=self.invoke();self.assertEqual(r['diagnostic_status'],'completed',r)
        self.assertEqual(r['policy']['saved'],'session');self.assertIn('DOCKER_QUERY_FAILED',r['blockers'])
        self.assertEqual(r['binding']['status'],'mismatch');self.assertFalse(self.extra_calls)

    def test_help_in_isolated_python(self):
        r=subprocess.run([sys.executable,'-I','-B',str(Path(check.__file__)),'--help'],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr);self.assertIn('--target',r.stdout)


if __name__=='__main__':unittest.main()
