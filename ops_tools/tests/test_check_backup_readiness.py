"""Readiness contracts: synthetic Docker metadata, real files/processes; no production access."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


m = load('readiness', ROOT / 'ops_tools/check_backup_readiness.py')
old = load('capacity_readiness_fixtures', ROOT / 'ops_tools/tests/test_plan_backup_capacity.py')
f = old.fixtures
PUBLIC = 'age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p'
SECRET = 'AGE-SECRET-KEY-1-DO-NOT-PRINT'
OBSERVED_AGE = {'status': 'observed', 'version_commands': 1, 'version': 'v1.2.0',
                'binary_authenticity_verified': False, 'backup_compatibility_verified': False}


class Fake(old.Fake):
    def execute(self, *args, **kwargs): raise AssertionError('no exec permitted')
    def logical_size(self, *args, **kwargs): raise AssertionError('no size scan permitted')
    def resources(self, row):
        self.calls += 1
        return {'memory_limit_bytes': 1024*1024, 'cpu_limit': 1.0}


class Workflow(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(); self.top = Path(self.t.name)
        self.f = f.Fixture(self.top)
        shutil.copytree(ROOT / 'ops_platform', self.f.root / 'ops_platform', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', 'tests'))
        history = 'docs/testing/ops-backup-capacity-server-20260929.json'
        dest = self.f.root / history; dest.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / history, dest)
        pins = {n: self.f.prom['images'][n] for n in ('obs-node-exporter', 'obs-cadvisor')}
        pins['obs-gpu'] = {'image_id': 'sha256:'+'c'*64, 'digest': 'nvcr.io/nvidia/k8s/dcgm-exporter@sha256:'+'d'*64}
        f.write(self.f.obs/'gpu/.state.json', {'schema': 1, 'config_id': self.f.owner, 'enabled': True, 'image': pins['obs-gpu']})
        for i, (name, pin) in enumerate(pins.items()):
            self.f.rows['prometheus'].append({'id': f'{100+i:064x}', 'image_id': pin['image_id'], 'image_ref': pin['digest'],
                'state': 'running', 'started_at': 'fixture-start', 'project': 'discovered-observation', 'service': name, 'owner': self.f.owner})
            self.f.images[pin['image_id']] = {'id': pin['image_id'], 'repo_digests': [pin['digest']], 'architecture': 'amd64', 'size_bytes': 2000}
        for rows in self.f.rows.values():
            for row in rows: row['project'] = 'detected-project-' + row['project']
        self.fake = Fake(self.f)
        self.management = self.f.ops.parent/'auto-refresh'/self.f.owner
        owner = m.bp.read_small(self.f.ops/'owner.json')
        binding = {'owner': owner, 'directory': str(self.f.ops), 'code_sha256': m.code_hash(self.f.root)[0]}
        self.service = {'schema': 1, 'provider': 'systemd-user', 'binding': binding, 'persistence': 'boot',
                        'unit_name': 'agent-project-ops-refresh-'+self.f.owner.replace('-', '')+'.service'}
        f.write(self.management/'service.json', self.service)
        f.write(self.management/'worker/owner.json', {'schema': 1, 'kind': 'guarded-worker-core', 'binding': binding})
        f.write(self.management/'worker/control.json', {'schema': 1, 'desired': 'running', 'recovery': None})
        self.destinations = {'scan_complete': True, 'discovery_scope': 'fixture', 'candidates': [
            {'parent': str(self.f.dest), 'proposed_path': str(self.f.dest/'agent-project-observability-backups'),
             'status': 'candidate', 'reasons': [], 'filesystem': {'fstype': 'ext4', 'device': '1:1', 'available_bytes': 1000000000},
             'source_relations': {'data': 'same_filesystem'}, 'write_access_advisory': True,
             'readonly': False, 'available_after_proposed_reserve_bytes': 900000000}]}
        self.finder = patch.object(m.cap, 'find_candidates', return_value=self.destinations)
        self.finder.start()

    def tearDown(self):
        self.finder.stop(); self.t.cleanup(); sys.modules.pop('lf_common', None)

    def run_check(self, **kwargs):
        return m.run(self.f.root, self.f.lf, self.f.ops, docker=self.fake,
                     age_probe=kwargs.pop('age_probe', lambda: copy.deepcopy(OBSERVED_AGE)), **kwargs)

    def test_complete_default_inspection_no_writes_secrets_exec_or_live_health_claim(self):
        before = f.snapshot(self.top); original = m.bp.read_small; opened=[]
        def read(path, *args, **kwargs):
            opened.append(str(path))
            self.assertNotIn(Path(path).name, ('access-key', 'api-token', 'langfuse-tracing.json'))
            self.assertFalse(Path(path).parent == self.f.lf and (Path(path).suffix == '.env' or Path(path).name == 'settings.json'))
            return original(path, *args, **kwargs)
        with patch.object(m.bp, 'read_small', read), patch.object(m.bp, 'measure', side_effect=AssertionError('no scan')):
            r = self.run_check()
        self.assertEqual(r['diagnostic_status'], 'completed', r)
        self.assertEqual(r['result'], 'attention_required'); self.assertFalse(r['backup_ready'])
        self.assertEqual(r['worker']['source_review'], 'reviewed')
        self.assertEqual(r['blockers']['implementation'], list(m.GAPS))
        self.assertEqual(r['installation']['core_container_count'], 9)
        self.assertEqual(r['installation']['exporter_count'], 3)
        self.assertFalse(r['worker']['live_health_checked'])
        self.assertFalse(r['capacity_reference']['reused_as_current'])
        self.assertFalse(r['destinations']['capacity_estimates_applied'])
        self.assertEqual(self.fake.exec_calls, 0)
        self.assertNotIn(f.SECRET, json.dumps(r)); self.assertEqual(before, f.snapshot(self.top))

    def test_optional_public_recipient_and_budgets_are_not_activation(self):
        r = self.run_check(recipients=[PUBLIC], outage_seconds=600, worker_pause_seconds=3600)
        self.assertEqual(r['recipients']['status'], 'syntax_valid')
        self.assertFalse(r['time_budgets']['persisted']); self.assertFalse(r['backup_ready'])
        self.assertNotIn(PUBLIC, json.dumps(r)); self.assertFalse(r['recipients']['key_possession_verified'])
        self.assertEqual(r['result'], 'attention_required')

    def test_invalid_time_budget_rejected_before_age_or_docker(self):
        probe = lambda: self.fail('must not probe')
        for fields in ({'outage_seconds': 20, 'worker_pause_seconds': 10}, {'outage_seconds': 0}, {'worker_pause_seconds': True}):
            r = self.run_check(age_probe=probe, **fields)
            self.assertEqual(r['error'], 'INVALID_TIME_BUDGET'); self.assertEqual(self.fake.calls, 0)

    def test_saved_recovery_and_pause_reported_without_reset(self):
        f.write(self.management/'worker/control.json', {'schema': 1, 'desired': 'paused', 'recovery': f.SECRET})
        before=f.snapshot(self.top);r=self.run_check()
        self.assertEqual(r['diagnostic_status'], 'completed')
        self.assertTrue(r['worker']['recovery_present']); self.assertIn('WORKER_SAVED_STATE_REQUIRES_REVIEW', r['blockers']['environment'])
        self.assertNotIn(f.SECRET, json.dumps(r)); self.assertEqual(before,f.snapshot(self.top))

    def test_control_change_during_read_is_partial_and_does_not_publish_matched(self):
        original=self.fake.resources
        def resources(row):
            f.write(self.management/'worker/control.json', {'schema': 1, 'desired': 'paused', 'recovery': None})
            return original(row)
        self.fake.resources=resources;r=self.run_check()
        self.assertEqual(r['error'], 'WORKER_METADATA_CHANGED');self.assertNotIn('worker',r)
        self.assertEqual(r['diagnostic_status'],'partial')

    def test_container_change_is_detected_after_projection(self):
        original=self.fake.resources
        def resources(row):
            self.f.rows['ops'][0]['started_at']='changed';return original(row)
        self.fake.resources=resources;r=self.run_check()
        self.assertEqual(r['error'],'CONTAINER_IDENTITY_CHANGED');self.assertNotIn('installation',r)

    def test_static_config_change_is_not_hidden_by_cache_exception(self):
        original=self.fake.resources
        def resources(row):
            f.write(self.f.ops/'host-config.json', {'modified': True});return original(row)
        self.fake.resources=resources;r=self.run_check()
        self.assertEqual(r['error'],'PREPARED_METADATA_CHANGED')

    def test_live_cache_update_is_allowed_without_opening_cache(self):
        original=self.fake.resources
        def resources(row):
            f.write(self.f.ops/'cache/api-success.json', {'live': 'advanced'});return original(row)
        self.fake.resources=resources;r=self.run_check()
        self.assertEqual(r['diagnostic_status'],'completed',r)

    def test_history_missing_does_not_rerun_sizes(self):
        (self.f.root/'docs/testing/ops-backup-capacity-server-20260929.json').unlink()
        r=self.run_check();self.assertEqual(r['diagnostic_status'],'completed')
        self.assertEqual(r['capacity_reference']['status'],'unavailable');self.assertEqual(self.fake.exec_calls,0)

    def test_unreviewed_source_does_not_inherit_implementation_claim(self):
        p=self.f.root/'ops_platform/ops/new_future_module.py';p.write_text('# independent fixture change\n')
        value=self.service['binding'];value['code_sha256']=m.code_hash(self.f.root)[0]
        f.write(self.management/'service.json', self.service)
        f.write(self.management/'worker/owner.json', {'schema':1,'kind':'guarded-worker-core','binding':value})
        r=self.run_check();self.assertEqual(r['diagnostic_status'],'completed')
        self.assertEqual(r['blockers']['implementation'],['IMPLEMENTATION_SOURCE_REVIEW_REQUIRED'])
        self.assertIsNone(r['worker']['maintenance_return_supported'])

    def test_source_binding_mismatch_is_partial(self):
        (self.f.root/'ops_platform/ops/new.py').write_text('# change\n')
        r=self.run_check();self.assertEqual(r['error'],'WORKER_SOURCE_BINDING_MISMATCH')

    def test_source_roots_and_no_capacity_values_reach_destination_discovery(self):
        with patch.object(m.cap,'find_candidates',return_value=self.destinations) as finder:
            r=self.run_check()
        self.assertEqual(r['diagnostic_status'],'completed')
        args=finder.call_args.args
        self.assertIn(str(self.f.root),args[2].values());self.assertIn(str(self.f.ops.parent),args[2].values())
        self.assertEqual(args[4],{})

    def test_no_candidate_is_completed_diagnosis_not_successful_backup(self):
        self.destinations['candidates'][0].update(status='rejected',reasons=['READ_ONLY_FILESYSTEM'],readonly=True)
        r=self.run_check();self.assertEqual(r['diagnostic_status'],'completed')
        self.assertIn('NO_USABLE_LOCAL_DESTINATION_CANDIDATE',r['blockers']['environment'])

    def test_exhausted_reserve_cannot_hide_no_usable_destination(self):
        self.destinations['candidates'][0]['available_after_proposed_reserve_bytes']=0
        before=f.snapshot(self.top);original=copy.deepcopy(self.destinations);r=self.run_check()
        self.assertEqual(r['diagnostic_status'],'completed')
        self.assertIn('NO_USABLE_LOCAL_DESTINATION_CANDIDATE',r['blockers']['environment'])
        row=r['destinations']['candidates'][0]
        self.assertEqual(row['status'],'rejected');self.assertIsNone(row['role_proposal'])
        self.assertEqual(row['reasons'],['NO_SPACE_AFTER_PROPOSED_RESERVE'])
        self.assertIsNone(row['capacity_sufficient']);self.assertFalse(r['backup_ready'])
        self.assertEqual(self.destinations,original);self.assertEqual(f.snapshot(self.top),before)

    def test_server_observed_positive_home_and_two_exhausted_disks(self):
        budgets=(124931736371,0,0)
        rows=[]
        for i,budget in enumerate(budgets):
            row=copy.deepcopy(self.destinations['candidates'][0])
            row.update(parent=str(self.f.dest/str(i)),available_after_proposed_reserve_bytes=budget)
            rows.append(row)
        self.destinations['candidates']=rows;r=self.run_check()
        self.assertEqual(r['diagnostic_status'],'completed')
        result=r['destinations']['candidates']
        self.assertEqual([x['status'] for x in result],['candidate','rejected','rejected'])
        self.assertNotIn('NO_USABLE_LOCAL_DESTINATION_CANDIDATE',r['blockers']['environment'])
        self.assertTrue(all(x['capacity_sufficient'] is None and not x['selected'] for x in result))
        self.assertFalse(r['destinations']['capacity_estimates_applied'])

    def test_missing_invalid_reserve_is_partial_without_destination_publication(self):
        for budget in (None,-1,False):
            self.destinations['candidates'][0]['available_after_proposed_reserve_bytes']=budget
            r=self.run_check()
            self.assertEqual(r['diagnostic_status'],'partial')
            self.assertEqual(r['error'],'DESTINATION_RESERVE_METADATA_INVALID')
            self.assertNotIn('destinations',r)

    def test_worker_absence_creates_nothing(self):
        shutil.rmtree(self.management)
        before=f.snapshot(self.top);r=self.run_check()
        self.assertEqual(r['worker']['status'],'not_installed');self.assertEqual(before,f.snapshot(self.top))

    def test_symlink_record_is_rejected_without_following(self):
        p=self.management/'service.json';p.unlink();p.symlink_to(self.f.lf/'settings.json')
        r=self.run_check();self.assertEqual(r['error'],'SYMLINK_REFUSED');self.assertNotIn(f.SECRET,json.dumps(r))


class PureAndProcess(unittest.TestCase):
    def test_native_age_public_example_and_bad_checksum_padding_private_values(self):
        self.assertEqual(m.recipient_metadata([PUBLIC])['status'],'syntax_valid')
        for value in (PUBLIC[:-1]+'q',PUBLIC.upper(),SECRET,'ssh-ed25519 abc','age1'+'q'*58, PUBLIC+'\n'):
            r=m.recipient_metadata([value]);self.assertEqual(r['status'],'invalid')
            self.assertNotIn(value,json.dumps(r))
        self.assertEqual(m.recipient_metadata([PUBLIC,PUBLIC])['error'],'DUPLICATE_PUBLIC_RECIPIENT')

    def test_age_missing_never_invokes_runner(self):
        r=m.inspect_age(which=lambda _:None,runner=lambda *a:self.fail('not called'))
        self.assertEqual(r['status'],'not_installed');self.assertEqual(r['version_commands'],0)

    def executable(self, directory, body):
        path=Path(directory)/'age';path.write_text('#!/usr/bin/python3\n'+body+'\n');path.chmod(0o700);return path

    def test_real_version_process_and_clean_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            p=self.executable(temp,"import os,sys; assert sys.argv[1:]==['--version']; assert 'LEAK_SENTINEL' not in os.environ; print('v1.2.0')")
            before=f.snapshot(Path(temp))
            with patch.dict(os.environ,{'LEAK_SENTINEL':SECRET}):r=m.inspect_age(which=lambda _:str(p))
            self.assertEqual(r['status'],'observed',r);self.assertFalse(r['binary_authenticity_verified'])
            self.assertEqual(before,f.snapshot(Path(temp)))

    def test_real_version_bad_stderr_is_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            p=self.executable(temp,"import sys; print('"+SECRET+"',file=sys.stderr); print('v1.2.0')")
            r=m.inspect_age(which=lambda _:str(p));self.assertEqual(r['error'],'AGE_VERSION_QUERY_FAILED')
            self.assertNotIn(SECRET,json.dumps(r))

    def test_binary_metadata_unsafe_refused_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            p=self.executable(temp,"raise RuntimeError('never run')");p.chmod(0o777)
            r=m.inspect_age(which=lambda _:str(p),runner=lambda *a:self.fail('not called'))
            self.assertEqual(r['error'],'AGE_BINARY_METADATA_UNSAFE')

    def test_version_timeout_and_local_descendant_termination(self):
        with tempfile.TemporaryDirectory() as temp:
            p=self.executable(temp,'import time; time.sleep(30)')
            def short(args,env,seconds):return m.cap.db.bounded(args,env,0.1)
            r=m.inspect_age(which=lambda _:str(p),runner=short)
            self.assertEqual(r['error'],'CLIENT_DEADLINE_EXCEEDED')

    def test_resource_projection_only_identity_limits_and_no_exec(self):
        calls=[];ident='a'*64;image='sha256:'+'b'*64
        def runner(args,env,seconds):
            calls.append(args);return 0,json.dumps([ident,image,100,0,50000,100000]).encode(),b''
        d=m.Docker(runner);d.base.endpoint='unix:///run/docker.sock'
        r=d.resources({'id':ident,'image_id':image});self.assertEqual(r,{'memory_limit_bytes':100,'cpu_limit':0.5})
        self.assertEqual(calls[0][3:6],['container','inspect','--format'])
        self.assertNotIn('.Env',calls[0][6])
        for function in (d.execute,d.logical_size):
            with self.assertRaises(m.bp.Unavailable):function({})
        with self.assertRaises(m.bp.Unavailable):d.metadata(['restart',ident])
        self.assertEqual(len(calls),1)

    def test_resource_identity_and_invalid_types_refused(self):
        ident='a'*64;image='sha256:'+'b'*64
        for data in (['c'*64,image,1,0,0,0],[ident,image,True,0,0,0],[ident,image,1,0,2,0], [ident,image,1,1,2,100]):
            d=m.Docker(lambda *a:(0,json.dumps(data).encode(),b''));d.base.endpoint='unix:///run/docker.sock'
            with self.assertRaises(m.bp.Unavailable):d.resources({'id':ident,'image_id':image})

    def test_memory_limits_and_affinity_are_advisory(self):
        text='MemTotal: 100000 kB\nMemAvailable: 50000 kB\n'
        r=m.resource_metadata({'one':{'memory_limit_bytes':45000*1024,'cpu_limit':None}},text,4)
        self.assertFalse(r['fits_existing_limits_estimate']);self.assertIsNone(r['capacity_sufficient'])
        r=m.resource_metadata({'one':{'memory_limit_bytes':None,'cpu_limit':None}},text,4)
        self.assertIsNone(r['configured_restore_memory_limits_sum_bytes'])
        with self.assertRaises(m.bp.Unavailable):m.resource_metadata({},'MemTotal: 100 kB\n',4)

    def test_parser_redacts_unknown_arguments_and_returns_two(self):
        output=io.StringIO()
        with redirect_stdout(output):code=m.main(['--identity',SECRET])
        self.assertEqual(code,2);self.assertNotIn(SECRET,output.getvalue())

    def test_completed_attention_exits_zero_partial_exits_two(self):
        for state,expected in (('completed',0),('partial',2)):
            report={'diagnostic_status':state,'result':'attention_required','backup_ready':False}
            with patch.object(m.os,'getuid',return_value=1000),patch.object(m,'run',return_value=report),redirect_stdout(io.StringIO()):
                self.assertEqual(m.main([]),expected)


if __name__ == '__main__': unittest.main()
