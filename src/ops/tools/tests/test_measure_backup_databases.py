"""Real filesystem/command boundaries; Docker metadata and transport are synthetic."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sizes', ROOT / 'measure_backup_databases.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeDocker:
    def __init__(self, directory, owner, locks):
        self.base = type('Base', (), {'calls': 0})()
        self.calls = self.exec_calls = 0
        self.directory = directory
        self.rows = []
        self.images = {}
        self.hook = lambda row, operation: None
        for i, service in enumerate(('lf-postgres', 'lf-clickhouse'), 1):
            ident = f'{i:064x}'
            image = f'sha256:{i:064x}'
            mounts = [{'Type': 'bind', 'Source': str(directory / 'data' / role),
                       'Destination': target, 'RW': True} for role, (svc, target) in m.ROLES.items() if svc == service]
            self.rows.append(dict(id=ident, image_id=image, image_ref=locks[service]['image'], state='running',
                                  started_at='2026-09-29T00:00:00Z', owner=owner, project='discovered-custom-name',
                                  service=service, mounts=mounts, userns=''))
            self.images[image] = [image, [locks[service]['image']], 'amd64', 10000]

    def host(self):
        return {'docker_root': '/does-not-contain-fixtures', 'architecture': 'x86_64'}

    def containers(self, owner):
        self.calls += 1
        return copy.deepcopy(self.rows)

    def image(self, ident):
        self.calls += 1
        return copy.deepcopy(self.images[ident])

    def execute(self, row, operation, seconds):
        self.exec_calls += 1
        self.hook(row, operation)
        if operation == 'stat':
            s = m.bp.metadata(row['source'], directory=True)
            return f'{s.st_dev}:{s.st_ino}:{s.st_uid}:{s.st_gid}:{s.st_mode:x}'
        return '16\t' + row['target']


class Cases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.top = Path(self.temp.name)
        self.directory = self.top / 'state with spaces'
        self.directory.mkdir(mode=0o700)
        self.owner = str(uuid.uuid4())
        self.marker = self.directory / 'owner.json'
        self.marker.write_text(json.dumps({'owner': self.owner}))
        self.marker.chmod(0o600)
        self.locks = json.loads((m.ROOT / 'src/infra/langfuse/images.lock.json').read_text())['images']
        self.fake = FakeDocker(self.directory, self.owner, self.locks)
        for role in m.ROLES:
            p = self.directory / 'data' / role
            p.mkdir(parents=True, mode=0o700)
            (p / 'secret-content-must-not-be-read').write_text('PRIVATE-SENTINEL')
        # This local executor maps only uid 0. Simulate DB ownership in Docker
        # metadata tests; the separate command tests use actual filesystem stat.
        original_metadata = m.bp.metadata
        def metadata(path, **kwargs):
            st = original_metadata(path, **kwargs)
            if os.getuid() == 0 and Path(path).parent == self.directory / 'data':
                return SimpleNamespace(st_dev=st.st_dev, st_ino=st.st_ino, st_mode=st.st_mode,
                                       st_uid=23456, st_gid=23456)
            return st
        self.metadata_patch = patch.object(m.bp, 'metadata', metadata)
        self.metadata_patch.start()
        self.measurement = m.Measurement(self.directory, self.fake)

    def tearDown(self):
        self.metadata_patch.stop()
        self.temp.cleanup()

    def test_complete_scoped_discovery_no_credentials_or_writes(self):
        def snap():
            return {str(p): (p.stat().st_mtime_ns, p.stat().st_mode, hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in self.directory.rglob('*') if p.is_file()}
        before = snap()
        original = m.bp.read_small
        opened = []
        def guarded(path, *a, **kw):
            opened.append(Path(path).name)
            self.assertIn(Path(path).name, ('owner.json', 'images.lock.json'))
            return original(path, *a, **kw)
        with patch.object(m.bp, 'read_small', guarded):
            result = self.measurement.run()
        self.assertEqual(result['result'], 'passed', result)
        self.assertEqual(result['discovery']['compose_project'], 'discovered-custom-name')
        self.assertEqual(result['measured_allocated_bytes_total'], 3 * 16384)
        self.assertEqual(self.fake.exec_calls, 9)
        self.assertEqual(before, snap())
        self.assertNotIn('PRIVATE-SENTINEL', json.dumps(result))
        self.assertFalse(result['production_credential_contents_read'])
        self.assertTrue(all(x['logical_bytes'] is None for x in result['storage']))

    def test_duplicate_container_blocks_before_exec(self):
        self.fake.rows.append(copy.deepcopy(self.fake.rows[0]))
        r = self.measurement.run()
        self.assertEqual(r['error'], 'DATABASE_CONTAINER_NOT_UNIQUE')
        self.assertEqual(self.fake.exec_calls, 0)

    def test_owner_state_image_userns_and_mount_mismatches_block(self):
        for field, value in [('owner', str(uuid.uuid4())), ('state', 'exited'), ('image_ref', 'foreign'),
                             ('userns', 'private'), ('mounts', []), ('id', '--bad')]:
            with self.subTest(field=field):
                rows = copy.deepcopy(self.fake.rows)
                self.fake.rows[0][field] = value
                self.assertEqual(self.measurement.run()['result'], 'attention_required')
                self.assertEqual(self.fake.exec_calls, 0)
                self.fake.rows = rows

    def test_same_tag_wrong_digest_or_architecture_refused(self):
        key = self.fake.rows[0]['image_id']
        for index, value in ((0, 'sha256:' + 'f'*64), (1, ['other']), (2, 'arm64')):
            old = copy.deepcopy(self.fake.images[key]);self.fake.images[key][index] = value
            self.assertEqual(self.measurement.run()['result'], 'attention_required')
            self.assertEqual(self.fake.exec_calls, 0);self.fake.images[key] = old

    def test_no_root_scan_or_automatic_permission_repair(self):
        original = m.bp.metadata
        def metadata(path, **kwargs):
            st = original(path, **kwargs)
            if Path(path).name == 'postgres':
                return SimpleNamespace(st_dev=st.st_dev,st_ino=st.st_ino,st_mode=st.st_mode,st_uid=0,st_gid=0)
            return st
        with patch.object(m.bp, 'metadata', metadata):
            r = self.measurement.run()
        self.assertEqual(r['error'], 'NONROOT_STORAGE_OWNER_REQUIRED')
        self.assertEqual(self.fake.exec_calls, 0)

    def test_final_restart_invalidates_all_sizes(self):
        def hook(row, op):
            if row['role'] == 'clickhouse-logs' and op == 'du':
                self.fake.rows[0]['started_at'] = 'new-start'
        self.fake.hook = hook
        r = self.measurement.run()
        self.assertFalse(r['discovery_stable'])
        self.assertTrue(all(x['allocated_bytes'] is None for x in r['storage']))

    def test_restart_before_next_command_refuses_further_measurement(self):
        def hook(row, op):
            if row['role'] == 'postgres' and op == 'du':
                self.fake.rows[0]['started_at'] = 'new-start'
        self.fake.hook = hook
        r = self.measurement.run()
        self.assertEqual(self.fake.exec_calls, 3)
        self.assertTrue(all(x['allocated_bytes'] is None for x in r['storage']))

    def test_final_metadata_failure_cannot_endorse_previous_sizes(self):
        original = self.fake.containers
        def rows(owner):
            if self.fake.exec_calls == 9:
                raise OSError('PRIVATE-SENTINEL')
            return original(owner)
        self.fake.containers = rows
        r = self.measurement.run()
        self.assertIsNone(r['discovery_stable'])
        self.assertNotIn('PRIVATE-SENTINEL', json.dumps(r))
        self.assertTrue(all(x['allocated_bytes'] is None for x in r['storage']))

    def test_measurement_failure_keeps_successful_other_roles(self):
        def hook(row, op):
            if row['role'] == 'postgres':
                raise m.bp.Unavailable('MEASUREMENT_FAILED')
        self.fake.hook = hook
        r = self.measurement.run()
        self.assertEqual(r['result'], 'attention_required')
        self.assertIsNone(r['storage'][0]['allocated_bytes'])
        self.assertTrue(all(x['status'] == 'measured' for x in r['storage'][1:]))

    def test_foreign_nested_mount_refused(self):
        mounts = m.bp.mount_table(Path('/proc/self/mountinfo').read_text())
        mounts.append({'mountpoint': str(self.directory / 'data/postgres/nested')})
        with patch.object(m.bp, 'mount_table', return_value=mounts):
            r = self.measurement.run()
        self.assertEqual(r['error'], 'NESTED_MOUNT_REFUSED')
        self.assertEqual(self.fake.exec_calls, 0)

    def test_owner_symlink_refused_before_exec(self):
        real = self.directory / 'saved';self.marker.rename(real);self.marker.symlink_to(real)
        r = self.measurement.run()
        self.assertEqual(r['error'], 'SYMLINK_REFUSED')
        self.assertEqual(self.fake.exec_calls, 0)

    def test_container_stat_mismatch_prevents_du(self):
        original = self.fake.execute
        def execute(row, op, seconds):
            value = original(row, op, seconds)
            return '0:0:0:0:41c0' if op == 'stat' else value
        self.fake.execute = execute
        r = self.measurement.run()
        self.assertEqual(self.fake.exec_calls, 3)
        self.assertTrue(all(x['error'] == 'CONTAINER_STORAGE_IDENTITY_MISMATCH' for x in r['storage']))

    def test_stat_non_directory_and_malformed_rejected(self):
        for text in ('0:0:1:1:81a4', 'PRIVATE-SENTINEL', '0:1:1:1:0\nsecret'):
            with self.assertRaises(m.bp.Unavailable):m.check_stat(text, {'identity': (0,0,1,1,0o644)})

    def test_exact_single_size_line_and_zero(self):
        self.assertEqual(m.parse_size('0\t/data', '/data'), 0)
        for value in ('16 /other', '16 /data\n32 /data', '-1 /data', 'bad', f'{2**63} /data'):
            with self.assertRaises(m.bp.Unavailable):m.parse_size(value, '/data')

    def test_no_root_cli_or_unbounded_deadline(self):
        with patch.object(m.os, 'getuid', return_value=0), patch('builtins.print') as output:
            self.assertEqual(m.main([]), 2)
            self.assertIn('RUN_AS_ORIGINAL', output.call_args.args[0])
        with patch.object(m.os, 'getuid', return_value=1000), patch('builtins.print') as output:
            self.assertEqual(m.main(['--max-seconds-per-store', '61']), 2)
            self.assertIn('INVALID_DEADLINE', output.call_args.args[0])


class CommandCases(unittest.TestCase):
    def test_real_du_stat_sparse_hardlink_and_symlink_without_content_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);file = root/'sparse'
            with file.open('wb') as f:f.truncate(8*1024*1024)
            os.link(file, root/'hardlink')
            (root/'link').symlink_to('/does-not-exist')
            st = root.stat()
            rc, out, err = m.bounded(['timeout','-s','KILL','5','stat','-c',m.STAT_FORMAT,temp], os.environ.copy(), 10)
            self.assertEqual((rc,err),(0,b''))
            m.check_stat(out.decode().strip(), {'identity': m.signature(st)})
            rc, out, err = m.bounded(['timeout','-s','KILL','5','du','-k','-s','-x',temp], os.environ.copy(),10)
            self.assertEqual((rc,err),(0,b''))
            self.assertLess(m.parse_size(out.decode().strip(), temp),8*1024*1024)

    def test_real_timeout_and_bounded_client_output(self):
        started = time.monotonic()
        rc, _, _ = m.bounded(['timeout','-s','KILL','1',sys.executable,'-c','import time;time.sleep(30)'],os.environ.copy(),5)
        self.assertIn(rc, (-9,137));self.assertLess(time.monotonic()-started,4)
        with self.assertRaisesRegex(m.bp.Unavailable,'CLIENT_DEADLINE_EXCEEDED'):
            m.bounded([sys.executable,'-c','import time;time.sleep(30)'],os.environ.copy(),0.1)
        with self.assertRaisesRegex(m.bp.Unavailable,'COMMAND_OUTPUT_LIMIT'):
            m.bounded([sys.executable,'-c','import os;os.write(1,b"x"*300000)'],os.environ.copy(),5)

    def test_fixed_exec_arguments_deadlines_and_error_redaction(self):
        calls = []
        def runner(args, env, seconds):
            calls.append((args,seconds));return 0,b'1\t/var/lib/postgresql/data\n',b''
        docker = m.Docker(runner);docker.base.endpoint = 'unix:///run/docker.sock'
        row = dict(role='postgres',target=m.ROLES['postgres'][1],container_id='a'*64,uid=999,gid=999)
        docker.execute(row,'du',30)
        args,seconds=calls[0]
        self.assertEqual(args[3:9],['exec','--user','999:999','--workdir','/','--env'])
        self.assertEqual(args[-9:],['timeout','-s','KILL','30','du','-k','-s','-x',row['target']])
        self.assertEqual(seconds,40);self.assertNotIn('sh',args)
        for rc,expected in [(127,'TOOLS_UNAVAILABLE'),(137,'DEADLINE_EXCEEDED'),(1,'MEASUREMENT_FAILED')]:
            docker.runner=lambda *a: (rc,b'PRIVATE-SENTINEL',b'PRIVATE-SENTINEL')
            with self.assertRaisesRegex(m.bp.Unavailable,expected) as context:docker.execute(row,'du',30)
            self.assertNotIn('PRIVATE-SENTINEL',str(context.exception))

    def test_exec_and_metadata_command_allowlists(self):
        docker=m.Docker(lambda *a: self.fail('must not launch'))
        docker.base.endpoint='unix:///run/docker.sock'
        row=dict(role='postgres',target=m.ROLES['postgres'][1],container_id='a'*64,uid=999,gid=999)
        for change in ({'target':'/etc'}, {'uid':0}, {'container_id':'--privileged'}, {'role':'unknown'}):
            with self.assertRaises(m.bp.Unavailable):docker.execute(dict(row,**change),'du',30)
        with self.assertRaises(m.bp.Unavailable):docker.execute(row,'rm',30)
        for args in (['restart','x'],['exec','x','sh'],['inspect','x'],['info','--format','{{json .}}']):
            with self.assertRaises(m.bp.Unavailable):docker.read(args)

    def test_remote_daemon_and_user_namespace_refused(self):
        docker=m.Docker()
        with patch.dict(os.environ, {'DOCKER_CONTEXT':'test'}), patch.object(docker.base,'_command', return_value='tcp://other:2375'):
            with self.assertRaisesRegex(m.bp.Unavailable,'LOCAL_DOCKER_REQUIRED'):docker.host()
        with patch.object(docker.base,'host',return_value={}), patch.object(docker,'read',return_value=json.dumps(['/tmp','host','amd64','linux',['name=userns']])):
            with self.assertRaisesRegex(m.bp.Unavailable,'USER_NAMESPACE'):docker.host()


if __name__ == '__main__':
    unittest.main()
