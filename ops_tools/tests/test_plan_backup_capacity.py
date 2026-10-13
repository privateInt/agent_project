import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
m = load('capacity', ROOT / 'ops_tools/plan_backup_capacity.py')
fixtures = load('old_inventory_tests', ROOT / 'infra/observability/tests/test_backup_preflight.py')


class Fake:
    def __init__(self, fixture):
        self.f = fixture; self.calls = self.exec_calls = 0
        self.base = SimpleNamespace(calls=0)
        self.fail = None
    def host(self): return dict(self.f.host)
    def containers_for(self, component, owner):
        self.calls += 1
        return copy.deepcopy(self.f.rows[component])
    def containers(self, owner):
        rows = self.containers_for('langfuse', owner)
        for row in rows: row['userns'] = ''
        return rows
    def image(self, ident):
        self.calls += 1
        x = self.f.images[ident]
        return [x['id'], x['repo_digests'], x['architecture'], x['size_bytes']]
    def execute(self, row, op, seconds):
        self.exec_calls += 1
        st = m.bp.metadata(row['source'], directory=True)
        return f'{st.st_dev}:{st.st_ino}:{st.st_uid}:{st.st_gid}:{st.st_mode:x}'
    def logical_size(self, row, seconds):
        self.exec_calls += 1
        if self.fail == row['role']: raise m.bp.Unavailable('SYNTHETIC_DB_UNAVAILABLE')
        p = subprocess.run(['timeout','5','du','-b','-s','-x',row['source']],capture_output=True,text=True,check=True)
        return int(p.stdout.split()[0])


class Workflow(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(); self.top=Path(self.t.name)
        self.f = fixtures.Fixture(self.top)
        pins={n:self.f.prom['images'][n] for n in ('obs-node-exporter','obs-cadvisor')}
        pins['obs-gpu']={'image_id':'sha256:'+'c'*64,'digest':'nvcr.io/nvidia/k8s/dcgm-exporter@sha256:'+'d'*64}
        fixtures.write(self.f.obs/'gpu/.state.json',{'schema':1,'config_id':self.f.owner,'enabled':True,'image':pins['obs-gpu']})
        for i,(name,pin) in enumerate(pins.items()):
            self.f.rows['prometheus'].append({'id':f'{100+i:064x}','image_id':pin['image_id'],'image_ref':pin['digest'],
                'state':'running','started_at':'synthetic-start','project':'observability','service':name,'owner':self.f.owner})
            self.f.images[pin['image_id']]={'id':pin['image_id'],'repo_digests':[pin['digest']],'architecture':'amd64','size_bytes':2000}
        for relative in ('ops_tools','ops_candidates'):
            shutil.copytree(ROOT/relative,self.f.root/relative,ignore=shutil.ignore_patterns('__pycache__'))
        self.fake=Fake(self.f)
        for rows in self.f.rows.values():
            for row in rows: row['project']='discovered-'+row['project']
        original=m.bp.metadata
        def metadata(path, *args, **kw):
            s=original(path,*args,**kw)
            if os.getuid()==0 and Path(path).parent==self.f.lf/'data' and Path(path).name in m.db.ROLES:
                return SimpleNamespace(st_dev=s.st_dev,st_ino=s.st_ino,st_mode=s.st_mode,st_uid=999,st_gid=999)
            return s
        self.p=patch.object(m.bp,'metadata',metadata);self.p.start()
        self.candidates=patch.object(m,'find_candidates',return_value={'scan_complete':True,'candidates':[{'status':'candidate'}]})
        self.candidates.start()
        self.host_configs=patch.object(m,'host_config_paths',return_value=[]);self.host_configs.start()
    def tearDown(self):
        self.host_configs.stop();self.candidates.stop();self.p.stop();self.t.cleanup();sys.modules.pop('lf_common',None)
    def run_plan(self): return m.run(self.f.root,self.f.lf,self.f.ops,self.fake)
    def test_full_real_metadata_workflow_no_mutation_or_secret_read(self):
        before=fixtures.snapshot(self.top)
        original=m.bp.read_small; opened=[]
        def reader(path,*a,**kw):
            opened.append(str(path))
            self.assertNotEqual(Path(path),self.f.lf/'settings.json')
            self.assertFalse(Path(path).parent==self.f.lf and str(path).endswith('.env'))
            self.assertNotIn(Path(path).name,('access-key','api-token','langfuse-tracing.json'))
            return original(path,*a,**kw)
        with patch.object(m.bp,'read_small',reader):r=self.run_plan()
        self.assertEqual(r['result'],'planned',r)
        self.assertEqual(self.fake.exec_calls,9)
        self.assertEqual(r['capacity']['status'],'estimated')
        self.assertEqual(len(r['exporters']),3)
        self.assertEqual(r['capacity']['unique_image_count'],12)
        self.assertIsNone(r['capacity']['total_required_bytes'])
        self.assertFalse(r['backup_ready']);self.assertFalse(r['destination_selected'])
        self.assertNotIn(fixtures.SECRET,json.dumps(r))
        self.assertEqual(before,fixtures.snapshot(self.top))
        self.assertTrue(all(x['project'].startswith('discovered-') for x in r['containers'].values()))
    def test_db_failure_does_not_use_old_allocated_measurement(self):
        self.fake.fail='postgres';r=self.run_plan()
        self.assertEqual(r['capacity']['status'],'incomplete')
        self.assertIn('langfuse-postgres',r['capacity']['missing_sources'])
        self.assertNotIn('all_on_one_filesystem_estimated_bytes',r['capacity'])
    def test_mount_order_changes_after_postgres_full_plan_still_measures_all_three(self):
        original=self.fake.containers;count=0
        def reordered(owner):
            nonlocal count
            count+=1;rows=original(owner)
            if count>=3:
                for row in rows:
                    if row['service']=='lf-clickhouse':row['mounts'].reverse()
            return rows
        self.fake.containers=reordered
        before=fixtures.snapshot(self.top);r=self.run_plan()
        self.assertEqual(r['result'],'planned',r);self.assertEqual(self.fake.exec_calls,9)
        self.assertTrue(all(r['identity_checks'].values()))
        self.assertEqual(len(r['database_discovery']['checks']),4)
        self.assertTrue(all(x['status']=='stable' for x in r['database_discovery']['checks']))
        self.assertEqual(before,fixtures.snapshot(self.top))
    def test_actual_mount_change_is_diagnosed_without_clickhouse_exec(self):
        original=self.fake.logical_size
        def changed(row,seconds):
            value=original(row,seconds)
            if row['role']=='postgres':
                next(x for x in self.f.rows['langfuse'] if x['service']=='lf-clickhouse')['mounts'][0]['Propagation']='changed'
            return value
        self.fake.logical_size=changed;r=self.run_plan()
        self.assertEqual(r['error'],'INVENTORY_CHANGED_DURING_PLAN');self.assertEqual(self.fake.exec_calls,3)
        self.assertFalse(r['identity_checks']['database_discovery']);self.assertNotIn('capacity',r)
        self.assertEqual(r['database_discovery']['checks'][1]['changed_fields'],['containers.lf-clickhouse.mounts'])
    def test_invalid_mount_still_rejected_by_original_discovery_validation(self):
        original=self.fake.logical_size
        def changed(row,seconds):
            value=original(row,seconds)
            if row['role']=='postgres':
                next(x for x in self.f.rows['langfuse'] if x['service']=='lf-clickhouse')['mounts'][0]['RW']=False
            return value
        self.fake.logical_size=changed;r=self.run_plan()
        self.assertEqual(self.fake.exec_calls,3);self.assertNotIn('capacity',r)
        self.assertFalse(r['identity_checks']['core_containers'])
        self.assertEqual(r['database_discovery']['checks'][1]['error'],'DATABASE_MOUNT_MISMATCH')
    def test_transient_changed_snapshot_cannot_be_hidden_by_final_return_to_baseline(self):
        original=self.fake.containers;count=0
        def changed(owner):
            nonlocal count
            count+=1;rows=original(owner)
            if count==3:
                next(x for x in rows if x['service']=='lf-clickhouse')['started_at']='transient-restart'
            return rows
        self.fake.containers=changed;r=self.run_plan()
        self.assertEqual(r['error'],'INVENTORY_CHANGED_DURING_PLAN');self.assertNotIn('capacity',r)
        self.assertEqual(r['database_discovery']['checks'][-1]['status'],'stable')
        self.assertFalse(r['identity_checks']['database_discovery'])
    def test_duplicate_or_wrong_owner_blocks_before_db_exec(self):
        self.f.rows['langfuse'][0]['owner']='foreign'
        r=self.run_plan();self.assertEqual(r['error'],'INVENTORY_IDENTITY_INCOMPLETE')
        self.assertEqual(self.fake.exec_calls,0)
    def test_final_restart_invalidates_plan(self):
        original=self.fake.logical_size
        def changed(row,seconds):
            v=original(row,seconds)
            if row['role']=='clickhouse-logs':self.f.rows['ops'][0]['started_at']='new-start'
            return v
        self.fake.logical_size=changed;r=self.run_plan()
        self.assertEqual(r['error'],'INVENTORY_CHANGED_DURING_PLAN');self.assertNotIn('capacity',r)
    def test_destination_phase_restart_invalidates_estimate(self):
        def discover(*a,**kw):
            self.f.rows['ops'][0]['started_at']='later'
            return {'scan_complete':True,'candidates':[{'status':'candidate','fits_colocated_estimate':True}]}
        with patch.object(m,'find_candidates',discover):r=self.run_plan()
        self.assertEqual(r['capacity']['status'],'invalidated')
        self.assertIsNone(r['destinations']['candidates'][0]['fits_colocated_estimate'])
    def test_unreadable_regular_store_stays_missing(self):
        original=m.bp.measure
        def measure(path,*a,**kw):
            if Path(path).name=='redis':return {'status':'unavailable','logical_bytes':None,'issues':{'PERMISSION_DENIED':1}}
            return original(path,*a,**kw)
        with patch.object(m.bp,'measure',measure):r=self.run_plan()
        self.assertEqual(r['capacity']['missing_sources'],['langfuse-redis'])
    def test_required_metadata_missing_blocks_exec(self):
        (self.f.lf/'postgres.env').unlink();r=self.run_plan()
        self.assertEqual(r['error'],'REQUIRED_FILE_METADATA_UNAVAILABLE');self.assertEqual(self.fake.exec_calls,0)
    def test_configuration_change_cannot_be_accepted(self):
        original=self.fake.logical_size
        def changed(row,seconds):
            value=original(row,seconds)
            if row['role']=='clickhouse-logs':fixtures.write(self.f.ops/'host-config.json',{'changed':True})
            return value
        self.fake.logical_size=changed;r=self.run_plan()
        self.assertEqual(r['error'],'INVENTORY_CHANGED_DURING_PLAN')
    def test_mount_change_invalidates_candidates(self):
        original=m.bp.mount_table;calls=0
        def mounts(text):
            nonlocal calls
            calls+=1;v=original(text)
            if calls==2:v[0]['readonly']=not v[0]['readonly']
            return v
        with patch.object(m.bp,'mount_table',mounts):r=self.run_plan()
        self.assertEqual(r['error'],'MOUNT_TABLE_CHANGED')
        self.assertEqual(r['capacity']['status'],'invalidated')
    def test_worker_records_and_custom_unit_path_are_included_without_systemctl(self):
        management=self.f.ops.parent/'auto-refresh'/self.f.owner
        management.mkdir(parents=True,mode=0o700)
        unit=self.top/'custom-user-units'/('agent-project-ops-refresh-'+self.f.owner.replace('-','')+'.service')
        fixtures.write(unit,'[Service]\nExecStart=/test\n')
        owner=json.loads((self.f.ops/'owner.json').read_text())
        fixtures.write(management/'service.json',{'unit_path':str(unit),'binding':{'owner':owner,'directory':str(self.f.ops)}})
        r=self.run_plan();self.assertEqual(r['result'],'planned',r)
        paths={x['path'] for x in r['sources']}
        self.assertIn(str(unit),paths);self.assertIn(str(management),paths)
        self.assertFalse(r['effects']['service_control_requested'])
    def test_exporter_image_mismatch_blocks_before_exec_without_mount_scan(self):
        self.f.rows['prometheus'][-1]['image_id']='sha256:'+'f'*64
        r=self.run_plan();self.assertEqual(r['error'],'EXPORTER_IDENTITY_MISMATCH');self.assertEqual(self.fake.exec_calls,0)
    def test_host_config_metadata_included_without_contents(self):
        p=self.top/'daemon.json';fixtures.write(p,'SECRET-HOST-CONFIG')
        with patch.object(m,'host_config_paths',return_value=[p]):r=self.run_plan()
        self.assertEqual(r['result'],'planned');self.assertIn(str(p),[x['path'] for x in r['sources']])
        self.assertNotIn('SECRET-HOST-CONFIG',json.dumps(r))


class SnapshotComparison(unittest.TestCase):
    def setUp(self):
        self.raw={'directory_identity':(1,2,3,4,0o700),'owner':{'owner':'fixture'},
            'containers':[{'service':svc,'id':svc,'image_id':'image','started_at':'start',
                           'mounts':[{'Type':'bind','Source':'/one','Destination':'/a','RW':True},
                                     {'Type':'bind','Source':'/two','Destination':'/b','RW':True}]}
                          for svc in ('lf-postgres','lf-clickhouse')],
            'stores':[{'role':role,'identity':(1,2,3,4,0o700),'uid':3,'gid':4} for role in m.db.ROLES]}
        self.base=m.canonical_db_snapshot(self.raw)
    def changes(self,raw):return m.db_changed_fields(self.base,m.canonical_db_snapshot(raw))
    def test_all_collection_permutations_equal_without_mutation(self):
        import itertools
        before=copy.deepcopy(self.raw)
        for containers in itertools.permutations(self.raw['containers']):
            for stores in itertools.permutations(self.raw['stores']):
                for reverse in itertools.product((False,True),repeat=2):
                    new=copy.deepcopy(self.raw);new['containers']=copy.deepcopy(list(containers));new['stores']=list(stores)
                    for row,flip in zip(new['containers'],reverse):
                        if flip:row['mounts'].reverse()
                    self.assertEqual(self.changes(new),[])
        self.assertEqual(self.raw,before)
    def test_every_mount_field_and_unknown_metadata_retained(self):
        for field,value in (('Type','volume'),('Source','/changed'),('Destination','/changed'),('RW',False),
                            ('Propagation','rshared'),('Mode','ro'),('SECRET-KEY','SECRET-VALUE')):
            with self.subTest(field=field):
                new=copy.deepcopy(self.raw);new['containers'][0]['mounts'][0][field]=value
                fields=self.changes(new);self.assertEqual(fields,['containers.lf-postgres.mounts'])
                self.assertNotIn('SECRET',json.dumps(fields))
    def test_duplicates_and_missing_collections_never_deduplicated(self):
        for collection in ('containers','stores'):
            for duplicate in (False,True):
                new=copy.deepcopy(self.raw)
                if duplicate:new[collection].append(copy.deepcopy(new[collection][0]))
                else:new[collection].pop()
                self.assertEqual(self.changes(new),[collection+'.membership'])
        new=copy.deepcopy(self.raw);new['containers'][0]['mounts'].append(copy.deepcopy(new['containers'][0]['mounts'][0]))
        self.assertEqual(self.changes(new),['containers.lf-postgres.mounts'])
    def test_identity_owner_modes_and_runtime_fields_remain_significant(self):
        for collection,fields in (('containers',('id','image_id','image_ref','started_at','state','owner','project','user','userns')),
                                  ('stores',('source','target','container_id','image_id','uid','gid','optional_diagnostic'))):
            for field in fields:
                new=copy.deepcopy(self.raw);new[collection][0][field]='changed'
                self.assertTrue(self.changes(new),(collection,field))
        for place in ('directory_identity','store'):
            for index in range(5):
                new=copy.deepcopy(self.raw)
                identity=list(new['directory_identity'] if place=='directory_identity' else new['stores'][0]['identity'])
                identity[index]+=1
                if place=='directory_identity':new[place]=tuple(identity)
                else:new['stores'][0]['identity']=tuple(identity)
                self.assertTrue(self.changes(new),(place,index))
        new=copy.deepcopy(self.raw);new['owner']={'owner':'changed'}
        self.assertEqual(self.changes(new),['owner'])
    def test_unknown_fields_are_compared_but_values_or_names_not_exposed(self):
        for place in ('root','containers','stores'):
            new=copy.deepcopy(self.raw)
            target=new if place=='root' else new[place][0]
            target['SECRET-KEY']='SECRET-VALUE'
            changes=self.changes(new);self.assertTrue(changes);self.assertNotIn('SECRET',json.dumps(changes))
    def test_discovery_exception_is_unavailable_not_equal(self):
        probe=SimpleNamespace(discover=lambda:(_ for _ in ()).throw(m.bp.Unavailable('DATABASE_MOUNT_MISMATCH')))
        result=m.compare_db_discovery(probe,self.base,'before_capacity')
        self.assertEqual(result['status'],'unavailable');self.assertEqual(result['error'],'DATABASE_MOUNT_MISMATCH')


class Estimates(unittest.TestCase):
    def test_formula_and_image_dedup_exact_integer_rounding(self):
        rows=[{'name':'a','status':'complete','logical_bytes':1001}]
        images={s:{'image_id':'same','image_size_bytes':999} for s in ('a','b')}
        r=m.estimate(rows,images)
        self.assertEqual(r['unique_image_count'],1)
        self.assertEqual(r['per_generation_estimated_bytes'],2400)
        self.assertEqual(r['destination_with_staging_estimated_bytes'],7200)
        self.assertEqual(r['all_on_one_filesystem_estimated_bytes'],9600)
        self.assertIsNone(r['capacity_sufficient'])
    def test_missing_measurements_never_become_zero(self):
        r=m.estimate([{'name':'a','status':'partial','logical_bytes':None}],{})
        self.assertEqual(r['status'],'incomplete');self.assertEqual(r['missing_sources'],['a'])
    def test_same_image_conflicting_sizes_rejected(self):
        with self.assertRaises(m.bp.Unavailable):m.estimate([],{'a':{'image_id':'same','image_size_bytes':1},'b':{'image_id':'same','image_size_bytes':2}})
    def test_limits_and_large_integer_not_float(self):
        for g,h in ((0,20),(31,20),(2,-1),(2,101)):
            with self.assertRaises(m.bp.Unavailable):m.estimate([],{},g,h)
        n=2**60+3;r=m.estimate([{'name':'x','status':'complete','logical_bytes':n}],{'x':{'image_id':'id','image_size_bytes':1}},1,1)
        self.assertEqual(r['per_generation_estimated_bytes'],((n+1)*101+99)//100)


class Destinations(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.top=Path(self.t.name);self.parent=self.top/'mounted'
        self.parent.mkdir();self.source=self.top/'source';self.source.mkdir();self.docker=self.top/'docker';self.docker.mkdir()
        self.mounts=[{'mountpoint':'/','fstype':'ext4','device':'1:1','source':'/dev/fixture','readonly':False}]
        self.fs={'device':'2:1','fstype':'ext4','readonly':False,'physical_disks':['disk-b'],'available_bytes':8000,'total_bytes':10000}
        self.v=SimpleNamespace(f_flag=0,f_favail=1000,f_files=10000)
        def filesystem(p,mounts):
            if Path(p)==self.source:return {**self.fs,'device':'1:1','physical_disks':['disk-a']}
            return dict(self.fs)
        self.p=patch.object(m.bp,'filesystem',filesystem);self.p.start()
        self.q=patch.object(m.os,'statvfs',return_value=self.v);self.q.start()
        self.a=patch.object(m.os,'access',return_value=True);self.a.start()
        self.capacity={'all_on_one_filesystem_estimated_bytes':6000}
    def tearDown(self):self.a.stop();self.q.stop();self.p.stop();self.t.cleanup()
    def candidate(self):return m.candidate(self.parent,{'data':str(self.source)},str(self.docker),self.mounts,self.capacity)
    def test_candidate_proposal_never_creates_and_device_relation(self):
        r=self.candidate();self.assertEqual(r['status'],'candidate',r)
        self.assertFalse(Path(r['proposed_path']).exists());self.assertFalse(r['selected']);self.assertFalse(r['write_probe_executed'])
        self.assertEqual(r['source_relations']['data'],'different_block_devices_same_host')
        self.assertTrue(r['fits_colocated_estimate']);self.assertIsNone(r['capacity_sufficient'])
    def test_readonly_and_access_reasons_not_repaired(self):
        self.fs['readonly']=True
        with patch.object(m.os,'access',return_value=False):r=self.candidate()
        self.assertIn('READ_ONLY_FILESYSTEM',r['reasons']);self.assertIn('PARENT_NOT_WRITABLE_BY_CURRENT_USER',r['reasons'])
    def test_statvfs_readonly_also_rejects(self):
        self.v.f_flag=os.ST_RDONLY
        self.assertIn('READ_ONLY_FILESYSTEM',self.candidate()['reasons'])
    def test_fat32_large_archive_not_treated_as_fitting(self):
        self.fs['fstype']='vfat';self.capacity['per_generation_estimated_bytes']=2**32
        self.assertIn('FAT32_ARCHIVE_SIZE_LIMIT_UNRESOLVED',self.candidate()['reasons'])
    def test_candidate_parent_change_clears_fit_claim(self):
        original=m.bp.metadata;count=0
        def meta(p,*a,**kw):
            nonlocal count
            s=original(p,*a,**kw);count+=1
            if count==3:
                return SimpleNamespace(st_dev=s.st_dev,st_ino=s.st_ino+1,st_uid=s.st_uid,st_gid=s.st_gid,st_mode=s.st_mode)
            return s
        with patch.object(m.bp,'metadata',meta):r=self.candidate()
        self.assertEqual(r['error'],'DESTINATION_METADATA_CHANGED');self.assertIsNone(r['fits_colocated_estimate'])
    def test_source_and_docker_overlap(self):
        target=self.parent/'agent-project-observability-backups'
        for source in (self.parent,target,target/'nested'):
            r=m.candidate(self.parent,{},str(source),self.mounts,self.capacity)
            self.assertIn('SOURCE_OR_DOCKER_PATH_OVERLAP',r['reasons'])
    def test_reserve_and_inodes(self):
        self.fs['available_bytes']=6999;self.v.f_favail=0
        r=self.candidate();self.assertFalse(r['fits_colocated_estimate'])
        self.assertIn('INSUFFICIENT_FOR_PROPOSED_ESTIMATE',r['reasons']);self.assertIn('NO_AVAILABLE_INODES',r['reasons'])
    def test_unknown_capacity_and_topology_are_not_success_claims(self):
        self.capacity={};self.fs['physical_disks']=None
        r=self.candidate();self.assertIsNone(r['fits_colocated_estimate']);self.assertEqual(r['source_relations']['data'],'unknown')
    def test_symlink_and_regular_file_destination_refused(self):
        p=self.parent/'agent-project-observability-backups';p.symlink_to(self.source)
        self.assertEqual(self.candidate()['error'],'SYMLINK_REFUSED');p.unlink();p.write_text('x')
        self.assertEqual(self.candidate()['error'],'DESTINATION_NOT_DIRECTORY')
    def test_network_mount_not_probed(self):
        mounts=[{**self.mounts[0],'fstype':'nfs'}]
        with patch.object(m.bp,'safe',side_effect=AssertionError('must not lstat network')):
            r=m.candidate(self.parent,{},str(self.docker),mounts,{})
        self.assertEqual(r['error'],'UNSUPPORTED_DESTINATION_FILESYSTEM')
    def test_automatic_discovery_bounded_children_and_no_fixed_disk(self):
        (self.parent/'my-backups').mkdir();(self.parent/'hidden-link').symlink_to(self.source)
        mounts=[{**self.mounts[0],'mountpoint':str(self.parent)}]
        r=m.find_candidates(mounts,self.top,{'source':str(self.source)},str(self.docker),{})
        parents={x['parent'] for x in r['candidates']}
        self.assertIn(str(self.parent/'my-backups'),parents);self.assertNotIn(str(self.parent/'hidden-link'),parents)
        self.assertFalse(r['automatic_selection']);self.assertTrue(r['scan_complete'])
        for i in range(70):(self.parent/str(i)).mkdir()
        self.assertFalse(m.find_candidates(mounts,self.top,{},str(self.docker),{},limit=4)['scan_complete'])


class Commands(unittest.TestCase):
    def test_fixed_logical_du_command_and_deadline(self):
        seen=[]
        def runner(argv,env,seconds):seen.append((argv,seconds));return 0,b'123\t/var/lib/clickhouse\n',b''
        d=m.Docker(runner);d.base.endpoint='unix:///run/docker.sock'
        row={'role':'clickhouse','target':'/var/lib/clickhouse','container_id':'a'*64,'uid':101,'gid':101}
        self.assertEqual(d.logical_size(row,15),123)
        argv,seconds=seen[0];self.assertEqual(argv[-5:],['du','-b','-s','-x','/var/lib/clickhouse']);self.assertEqual(seconds,25)
        self.assertNotIn('sh',argv);self.assertIn('101:101',argv)
        for changed in ({'target':'/etc'},{'uid':0},{'container_id':'--bad'}):
            with self.assertRaises(m.bp.Unavailable):d.logical_size({**row,**changed},15)
    def test_deadline_failure_and_partial_output_not_accepted(self):
        row={'role':'postgres','target':'/var/lib/postgresql/data','container_id':'a'*64,'uid':999,'gid':999}
        for rc,out,err in ((124,b'',b''),(1,b'123\t/var/lib/postgresql/data',b'private'),(0,b'123\t/etc',b'')):
            d=m.Docker(lambda *a:(rc,out,err));d.base.endpoint='unix:///run/docker.sock'
            with self.assertRaises(m.bp.Unavailable):d.logical_size(row,15)
    def test_command_allowlist_no_arbitrary_exec_or_env(self):
        d=m.Docker(lambda *a:(0,b'[]',b''));d.base.endpoint='unix:///run/docker.sock'
        for args in (['exec','anything'],['container','inspect','--format','{{json .Config.Env}}','a'*64],['ps','-aq','--no-trunc','--filter','label=unowned=value']):
            with self.assertRaises(m.bp.Unavailable):d.metadata(args)
    def test_actual_sparse_apparent_size_exceeds_allocation(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'sparse'
            with p.open('wb') as f:f.truncate(8*1024*1024)
            logical=int(subprocess.check_output(['du','-b','-s','-x',t]).split()[0])
            allocated=int(subprocess.check_output(['du','-k','-s','-x',t]).split()[0])*1024
            self.assertGreater(logical,allocated)


if __name__=='__main__':unittest.main()
