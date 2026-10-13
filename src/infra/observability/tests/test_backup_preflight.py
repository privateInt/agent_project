"""Host-only inventory regression tests. No daemon, service, API or real secrets."""
import contextlib
import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('backup_preflight', SOURCE / 'src/infra/observability/ops/backup_preflight.py')
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)
SECRET = 'PRIVATE-SENTINEL-never-print-me'


def write(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value) if isinstance(value, (dict, list)) else value)
    path.chmod(mode)


def snapshot(root):
    return {str(p.relative_to(root)): (stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest()) for p in root.rglob('*') if p.is_file()}


class Fixture:
    def __init__(self, base):
        self.root = base / 'project'
        for relative in ('src/infra/observability', 'src/infra/langfuse', 'src/ops/platform/docker-compose.yml'):
            source = SOURCE / relative
            dest = self.root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, dest, ignore=shutil.ignore_patterns('__pycache__', 'tests'))
            else:
                shutil.copyfile(source, dest)
        self.obs = self.root / 'src/infra/observability'
        self.lf = base / 'langfuse'
        self.ops = base / 'ops'
        self.docker_root = base / 'docker'
        self.dest = base / 'candidate'
        self.docker_root.mkdir()
        self.dest.mkdir()
        self.host = {'docker_root': str(self.docker_root), 'hostname': 'fixture-host', 'context': 'default',
                     'architecture': 'x86_64', 'endpoint': 'unix:///var/run/docker.sock'}
        self.owner = str(uuid.uuid4())
        uid, gid = os.getuid(), os.getgid()
        op = bp.load_module('_fixture_obs', self.obs / 'ops/manage.py')
        data = base / 'observability/prometheus'
        data.mkdir(parents=True, mode=0o700)
        self.prom = dict(schema=1, config_id=self.owner, uid=uid, gid=gid, data_dir=str(data),
                         docker_root=str(self.docker_root), images={})
        for i, (service, tag) in enumerate(op.TAGS.items()):
            self.prom['images'][service] = {'tag': tag, 'digest': tag.rsplit(':', 1)[0] + '@sha256:' + str(i + 1) * 64,
                                             'image_id': 'sha256:' + str(i + 4) * 64}
        write(data / 'wal', 'data')
        write(data.parent / '.owner.json', {'config_id': self.owner})
        write(self.obs / '.state.json', self.prom)
        write(self.obs / '.env', op.environment(self.prom))
        write(self.obs / 'gpu/.state.json', {'schema': 1})
        write(self.obs / 'gpu/.env', 'metadata-only')
        amdata = data.parent / 'alertmanager'
        amdata.mkdir(mode=0o700)
        am = {'schema': 1, 'enabled': True, 'config_id': self.owner, 'data_dir': str(amdata),
              'image': {'tag': 'quay.io/prometheus/alertmanager:v0.32.2',
                        'digest': 'quay.io/prometheus/alertmanager@sha256:' + 'a' * 64,
                        'image_id': 'sha256:' + 'b' * 64}}
        write(amdata / '.owner.json', {'config_id': self.owner})
        write(amdata / 'silences', '')
        write(self.obs / 'alerts/.state.json', am)
        amenv = {'OBS_ALERTS_IMAGE': am['image']['digest'], 'OBS_CONFIG_ID': self.owner,
                 'OBS_UID': uid, 'OBS_GID': gid, 'OBS_ALERTS_DATA': str(amdata)}
        write(self.obs / 'alerts/.env', ''.join(k + "='" + str(v) + "'\n" for k, v in amenv.items()))
        self.lf.mkdir(mode=0o700)
        sys.modules.pop('lf_common', None)
        sys.path.insert(0, str(self.root / 'src/infra/langfuse/ops'))
        try:
            lfmod = bp.load_module('_fixture_lf', self.root / 'src/infra/langfuse/ops/manage.py')
        finally:
            sys.path.pop(0)
        settings = {'schema': 1, 'owner': self.owner, 'uid': uid, 'gid': gid, 'state_dir': str(self.lf),
                    'images': lfmod.IMAGES, 'host': {k: self.host[k] for k in ('docker_root', 'context', 'hostname')},
                    'storage': {}, 'public_url': 'http://localhost:16320'}
        for field in ('db_password', 'ch_password', 'redis_password', 's3_key', 's3_secret', 'salt', 'encryption_key',
                      'nextauth_secret', 'org_id', 'project_id', 'public_key', 'secret_key', 'email', 'user_name', 'password'):
            settings[field] = SECRET
        for name in bp.STORES:
            p = self.lf / 'data' / name
            p.mkdir(parents=True, mode=0o700)
            st = p.stat()
            settings['storage'][name] = [st.st_dev, st.st_ino]
            write(p / 'fixture.bin', 'data-' + name)
        write(self.lf / 'settings.json', settings)
        write(self.lf / 'owner.json', {'owner': self.owner})
        for name, content in lfmod.environments(settings).items():
            write(self.lf / name, content)
        self.ops.mkdir(mode=0o700)
        (self.ops / 'cache').mkdir(mode=0o700)
        write(self.ops / 'owner.json', dict(schema=1, owner=self.owner, uid=uid, gid=gid, project_root=str(self.root)))
        agent = self.root / 'src/platform/agent/.secrets/api-token'
        lfkey = self.root / 'src/platform/agent/.secrets/langfuse-tracing.json'
        write(agent, SECRET)
        write(lfkey, {'key': SECRET})
        cfg = {'schema': 1, 'platforms': {'agent': {'token_file': str(agent)}},
               'langfuse_file': str(lfkey), 'access_file': str(self.ops / 'access-key'),
               'state_file': str(self.ops / 'cache/api-success.json'), 'tls_cert': None, 'tls_key': None}
        write(self.ops / 'host-config.json', cfg)
        c = copy.deepcopy(cfg)
        c.update(langfuse_file='/run/ops/langfuse.json', access_file='/run/ops/access-key', state_file='/var/lib/ops/api-success.json')
        c['platforms']['agent']['token_file'] = '/run/ops/agent-token'
        write(self.ops / 'config.json', c)
        write(self.ops / 'access-key', SECRET)
        write(self.ops / 'cache/api-success.json', {'times': {'example': 100}})
        env = {'OPS_UID': str(uid), 'OPS_GID': str(gid), 'OPS_OWNER': self.owner,
               'OPS_CONFIG_FILE': str(self.ops / 'config.json'), 'OPS_ACCESS_FILE': cfg['access_file'],
               'OPS_AGENT_TOKEN_FILE': str(agent), 'OPS_LANGFUSE_FILE': str(lfkey), 'OPS_CACHE_DIR': str(self.ops / 'cache')}
        write(self.ops / 'compose.env', ''.join(k + "='" + v + "'\n" for k, v in env.items()))
        self.mounts = bp.mount_table(Path('/proc/self/mountinfo').read_text())
        self.calls = 0
        self.rows = {}
        self.images = {}
        inventory = self.inventory()
        inventory.host = self.host
        for component in bp.PROJECTS:
            getattr(inventory, 'load_' + component)()
        for i, (name, value) in enumerate(inventory.services.items()):
            ident = value['image_id'] or 'sha256:' + format(i + 16, '064x')
            row = dict(id=format(i + 50, '064x'), image_id=ident, image_ref=value['image'], state='running',
                       started_at='2026-09-28T00:00:00Z', user=value['user'] or '', owner=value['owner'],
                       project=bp.PROJECTS[value['component']], service=name,
                       mounts=[{'Type': 'bind', 'Destination': target, 'Source': source, 'RW': rw}
                               for target, (source, rw) in value['mounts'].items()])
            self.rows.setdefault(value['component'], []).append(row)
            self.images[ident] = {'id': ident, 'repo_digests': [value['image']] if '@sha256:' in value['image'] else [],
                                  'architecture': 'amd64', 'size_bytes': 1000}

    def inventory(self, **kwargs):
        return bp.ComposeInventory(self.root, self.lf, self.ops, self, self.mounts, **kwargs)

    def containers(self, component):
        self.calls += 1
        return copy.deepcopy(self.rows.get(component, []))

    def image(self, ident):
        self.calls += 1
        return copy.deepcopy(self.images[ident])

    def host_read(self):
        self.calls += 1
        return dict(self.host)

    def run(self, **kwargs):
        # The fake retains the same public boundary as the fixed read-only Docker client.
        inv = self.inventory(**kwargs)
        inv.docker = type('FakeDocker', (), {'host': lambda _: self.host_read(),
            'containers': lambda _, c: self.containers(c), 'image': lambda _, i: self.image(i),
            'calls': property(lambda _: self.calls)})()
        return inv.run([self.dest])


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.f = Fixture(self.base)

    def tearDown(self):
        sys.modules.pop('lf_common', None)
        self.temp.cleanup()

    def test_complete_fixture_no_mutation_or_secret_leak(self):
        before = snapshot(self.base)
        report = self.f.run()
        self.assertEqual(report['result'], 'inventory_complete', report['components'])
        self.assertEqual(len(report['containers']), 9)
        self.assertTrue(all(x['status'] == 'matched' for x in report['containers'].values()))
        self.assertTrue(report['container_metadata_stable_during_scan'])
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertEqual(json.loads(bp.render(report)), report)
        self.assertLess(len(bp.render(report).splitlines()), 120)
        self.assertEqual(before, snapshot(self.base))
        self.assertFalse(report['backup_ready'])
        self.assertIsNone(report['capacity']['total_required_bytes'])
        self.assertTrue(report['capacity']['measured_store_logical_bytes'] > 0)

    def test_mount_drift_is_not_accepted(self):
        self.f.rows['langfuse'][0]['mounts'].append({'Type': 'bind', 'Source': '/etc', 'Destination': '/unexpected', 'RW': True})
        r = self.f.run()
        self.assertEqual(r['result'], 'attention_required')
        self.assertEqual(r['containers']['langfuse_issue']['error'], 'CONTAINER_MOUNT_MISMATCH')

    def test_duplicate_or_unexpected_container(self):
        for mutation in ('duplicate', 'unexpected'):
            with self.subTest(mutation=mutation):
                row = copy.deepcopy(self.f.rows['ops'][0])
                if mutation == 'unexpected':
                    row['service'] = 'foreign'
                self.f.rows['ops'].append(row)
                self.assertIn('ops_issue', self.f.run()['containers'])
                self.f.rows['ops'].pop()

    def test_wrong_owner_image_digest_and_architecture(self):
        for field, value in (('owner', 'foreign'), ('image_ref', 'agent-project-ops:wrong'), ('image_id', 'sha256:' + '0' * 64)):
            with self.subTest(field=field):
                old = self.f.rows['prometheus'][0][field]
                self.f.rows['prometheus'][0][field] = value
                self.assertIn('prometheus_issue', self.f.run()['containers'])
                self.f.rows['prometheus'][0][field] = old
        image = next(iter(self.f.images.values()))
        image['architecture'] = 'arm64'
        self.assertEqual(self.f.run()['containers']['prometheus_issue']['error'], 'IMAGE_ARCHITECTURE_MISMATCH')

    def test_private_mode_missing_key_and_no_key_content_read(self):
        key = self.f.ops / 'access-key'
        key.chmod(0o644)
        r = self.f.run()
        self.assertEqual(r['result'], 'attention_required')
        self.assertEqual(next(f for f in r['required_files'] if f['name'] == 'ops-access-key')['error'], 'PRIVATE_OWNER_MODE_MISMATCH')
        key.unlink()
        self.assertEqual(self.f.run()['result'], 'attention_required')

    def test_env_drift_private_error_redacted(self):
        write(self.f.lf / 'redis.env', SECRET + '-changed')
        r = self.f.run()
        self.assertEqual(r['components']['langfuse']['status'], 'unavailable')
        self.assertNotIn(SECRET, json.dumps(r))

    def test_langfuse_binding_inode_drift(self):
        path = self.f.lf / 'settings.json'
        s = json.loads(path.read_text())
        s['storage']['postgres'][1] += 1
        write(path, s)
        self.assertEqual(self.f.run()['components']['langfuse']['status'], 'unavailable')

    def test_missing_and_symlink_destinations_never_created_or_followed(self):
        inv = self.f.inventory()
        inv.host = self.f.host
        missing = self.base / 'missing' / 'new'
        self.assertEqual(inv.destination(missing)['error'], 'MISSING')
        self.assertFalse(missing.parent.exists())
        link = self.base / 'link'
        link.symlink_to(self.f.dest, target_is_directory=True)
        self.assertEqual(inv.destination(link)['error'], 'SYMLINK_REFUSED')

    def test_same_filesystem_and_unknown_topology_not_independent(self):
        r = self.f.run()
        self.assertEqual(set(r['destinations'][0]['source_relations'].values()), {'same_filesystem'})
        self.assertFalse(r['destinations'][0]['write_probe_executed'])
        self.assertFalse(r['destinations'][0]['selected'])

    def test_exfat_different_device_is_candidate_only_unknown_capacity(self):
        inv = self.f.inventory()
        inv.host = self.f.host
        inv.stores = [{'name': 'store', 'path': str(self.f.ops),
                       'filesystem': {'device': '8:1', 'physical_disks': ['sda']}}]
        fs = dict(device='8:17', physical_disks=['sdb'], fstype='exfat', readonly=False,
                  mountpoint=str(self.f.dest), available_bytes=10000, total_bytes=20000)
        with patch.object(bp, 'filesystem', return_value=fs):
            r = inv.destination(self.f.dest)
        self.assertEqual(r['source_relations']['store'], 'different_block_devices_same_host')
        self.assertIsNone(r['capacity_sufficient'])
        self.assertEqual(r['restore_directory_suitable'], 'unverified')
        self.assertTrue(r['archive_encryption_required'])
        fs['physical_disks'] = None
        with patch.object(bp, 'filesystem', return_value=fs):
            self.assertEqual(inv.destination(self.f.dest)['source_relations']['store'], 'unknown')

    def test_destination_overlap_with_source_or_docker(self):
        self.f.dest = self.f.ops
        r = self.f.run()
        self.assertTrue(r['destinations'][0]['source_overlap'])
        self.assertEqual(r['result'], 'attention_required')
        self.f.dest = self.f.docker_root
        self.assertTrue(self.f.run()['destinations'][0]['source_overlap'])

    def test_scan_permission_failure_reports_unknown_not_zero(self):
        target = self.f.lf / 'data/postgres'
        original = bp.open_directory
        def deny(p):
            if Path(p) == target:
                raise PermissionError(errno.EACCES, SECRET)
            return original(p)
        with patch.object(bp, 'open_directory', side_effect=deny):
            r = self.f.run()
        store = next(s for s in r['storage'] if s['name'] == 'langfuse-postgres')
        self.assertEqual(store['size']['status'], 'unavailable')
        self.assertIsNone(store['size']['logical_bytes'])
        self.assertEqual(store['size']['issues'], {'PERMISSION_DENIED': 1})
        self.assertEqual(r['result'], 'attention_required')
        self.assertNotIn(SECRET, json.dumps(r))

    def test_config_and_container_change_during_scan(self):
        original = bp.measure
        changed = False
        def changing(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                write(self.f.ops / 'cache/api-success.json', {'times': {'new': 200}})
                self.f.rows['ops'][0]['started_at'] = '2026-09-28T01:00:00Z'
            return original(*args, **kwargs)
        with patch.object(bp, 'measure', side_effect=changing):
            r = self.f.run()
        self.assertFalse(r['container_metadata_stable_during_scan'])
        self.assertIn('ops-cache/api-success.json', r['files_changed_during_scan'])
        self.assertEqual(r['result'], 'attention_required')

    def test_identical_failed_inspections_are_unavailable_not_stable(self):
        with patch.object(self.f, 'containers', side_effect=bp.Unavailable('DOCKER_READ_FAILED')):
            r = self.f.run()
        self.assertIsNone(r['container_metadata_stable_during_scan'])
        self.assertEqual(r['container_comparison_status'], 'unavailable')
        self.assertEqual(r['result'], 'attention_required')

    def test_readonly_compose_secrets_require_private_parent(self):
        folder = self.f.root / 'src/platform/agent/.secrets'
        for p in folder.iterdir(): p.chmod(0o444)
        before = snapshot(folder)
        report = self.f.run()
        keys = [f for f in report['required_files'] if f['name'].endswith('-reference')]
        self.assertTrue(all(f['status'] == 'ok' and f['protection'] == 'readonly_file_in_private_parent' for f in keys))
        self.assertEqual(before, snapshot(folder))
        folder.chmod(0o755)
        report = self.f.run()
        self.assertTrue(all(f['status'] == 'unavailable' for f in report['required_files'] if f['name'].endswith('-reference')))

    def test_readonly_key_alias_is_not_assumed_private(self):
        p = self.f.root / 'src/platform/agent/.secrets/api-token'
        p.chmod(0o444)
        os.link(p, self.base / 'exposed-alias')
        report = self.f.run()
        key = next(f for f in report['required_files'] if f['name'] == 'agent-token-reference')
        self.assertEqual(key['error'], 'CREDENTIAL_PROTECTION_UNVERIFIED')


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_sparse_hardlink_and_no_content_open(self):
        p = self.root / 'sparse'
        with p.open('wb') as f:
            f.truncate(10 * 1024 * 1024)
        os.link(p, self.root / 'alias')
        r = bp.measure(self.root, [])
        self.assertEqual(r['status'], 'complete')
        self.assertEqual(r['logical_bytes'], 10 * 1024 * 1024)
        self.assertLess(r['allocated_bytes'], r['logical_bytes'])
        self.assertEqual(r['counts']['hardlink_duplicates'], 1)

    def test_symlink_fifo_and_nested_mount_report_partial(self):
        write(self.root / 'regular', 'one')
        (self.root / 'link').symlink_to('/etc')
        os.mkfifo(self.root / 'pipe')
        nested = self.root / 'nested'
        nested.mkdir()
        write(nested / 'omitted', 'x' * 500)
        r = bp.measure(self.root, [{'mountpoint': str(nested)}])
        self.assertEqual(r['status'], 'partial')
        self.assertIsNone(r['logical_bytes'])
        self.assertEqual(r['observed_logical_bytes'], 3)
        self.assertEqual(set(r['issues']), {'SYMLINK_NOT_FOLLOWED', 'SPECIAL_FILE_NOT_SCANNED', 'NESTED_MOUNT_NOT_SCANNED'})

    def test_entry_limit_and_explicit_exclusion(self):
        for n in range(3):
            write(self.root / str(n), 'x')
        r = bp.measure(self.root, [], entries=1)
        self.assertEqual(r['status'], 'partial')
        self.assertEqual(r['issues'], {'SCAN_LIMIT': 1})
        r = bp.measure(self.root, [], exclude_children=['0'])
        self.assertEqual(r['status'], 'complete')
        self.assertEqual(r['logical_bytes'], 2)

    def test_parent_symlink_and_nonregular_config_refused(self):
        (self.root / 'link').symlink_to('/etc')
        with self.assertRaises(bp.Unavailable):
            bp.read_small(self.root / 'link/passwd')
        os.mkfifo(self.root / 'pipe')
        with self.assertRaises(bp.Unavailable):
            bp.read_small(self.root / 'pipe')

    def test_oversized_config_refused_without_reading_body(self):
        p = self.root / 'large'
        with p.open('wb') as f:
            f.truncate(bp.MAX_JSON + 1)
        p.chmod(0o600)
        with self.assertRaisesRegex(bp.Unavailable, 'INVALID_CONFIG_FILE'):
            bp.read_small(p)

    def test_mount_escape_and_readonly_super_options(self):
        rows = bp.mount_table('20 1 8:1 / /media/disk\\040one rw - ext4 /dev/sda1 ro\n')
        self.assertEqual(rows[0]['mountpoint'], '/media/disk one')
        self.assertTrue(rows[0]['readonly'])

    def test_physical_disk_partitions_and_unknown_virtual(self):
        disk = self.root / 'devices/pci/block/sda'
        part = disk / 'sda1'
        part.mkdir(parents=True)
        (disk / 'device').mkdir()
        write(part / 'partition', '1')
        links = self.root / 'dev/block'
        links.mkdir(parents=True)
        (links / '8:1').symlink_to(part, target_is_directory=True)
        self.assertEqual(bp.physical_disks('8:1', self.root), ['sda'])
        self.assertIsNone(bp.physical_disks('0:999', self.root))


class DockerTests(unittest.TestCase):
    def test_remote_context_refused_before_info(self):
        commands = []
        def run(args, **kw):
            commands.append(args)
            return type('Result', (), dict(returncode=0, stdout='tcp://remote:2376', stderr=SECRET))()
        with patch.dict(os.environ, {'DOCKER_CONTEXT': 'remote'}, clear=True), patch.object(bp.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(bp.Unavailable, 'LOCAL_DOCKER_REQUIRED'):
                bp.DockerReadOnly().host()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][1:3], ['context', 'inspect'])

    def test_context_precedence_and_safe_projection(self):
        calls = []
        def run(args, **kw):
            calls.append((args, kw))
            response = 'unix:///var/run/docker.sock' if 'context' in args else json.dumps(['/var/lib/docker', 'host', 'x86_64', 'linux', []])
            return type('Result', (), dict(returncode=0, stdout=response, stderr=SECRET))()
        with patch.dict(os.environ, {'DOCKER_CONTEXT': 'default', 'DOCKER_HOST': 'tcp://ignored:2376'}, clear=True), patch.object(bp.subprocess, 'run', side_effect=run):
            d = bp.DockerReadOnly()
            self.assertEqual(d.host()['context'], 'default')
        self.assertEqual(calls[-1][0][1:3], ['--host', 'unix:///var/run/docker.sock'])
        self.assertNotIn('DOCKER_HOST', calls[-1][1]['env'])
        self.assertNotIn('{{json .}}', calls[-1][0])

    def test_mutation_and_full_env_refused(self):
        d = bp.DockerReadOnly()
        d.endpoint = 'unix:///var/run/docker.sock'
        with patch.object(bp.subprocess, 'run') as run:
            for args in (['restart', 'x'], ['run', 'x'], ['compose', 'up'], ['image', 'save', 'x'],
                         ['container', 'inspect', '--format', '{{json .Config.Env}}', 'a' * 64]):
                with self.assertRaisesRegex(bp.Unavailable, 'READ_ONLY_DOCKER_COMMAND_REQUIRED'):
                    d._command(args)
            run.assert_not_called()

    def test_failed_docker_stderr_never_forwarded(self):
        d = bp.DockerReadOnly()
        with patch.object(bp.subprocess, 'run', return_value=type('Result', (), dict(returncode=1, stdout=SECRET, stderr=SECRET))()):
            with self.assertRaisesRegex(bp.Unavailable, '^DOCKER_READ_FAILED$'):
                d.host()

    def test_project_inspect_projection_and_image_parser(self):
        d = bp.DockerReadOnly()
        d.endpoint = 'unix:///var/run/docker.sock'
        ident, image = 'a' * 64, 'sha256:' + 'b' * 64
        commands = []
        def run(args, **kwargs):
            commands.append(args)
            if 'ps' in args:
                value = ident
            elif 'container' in args:
                value = json.dumps([ident, image, 'agent-project-ops:o04.2', 'running', 'date', '1000:1000',
                                    'agent-project-ops', 'ops-web', 'owner', []])
            else:
                value = json.dumps([image, [], 'amd64', 1234])
            return type('Result', (), dict(returncode=0, stdout=value, stderr=SECRET))()
        with patch.object(bp.subprocess, 'run', side_effect=run):
            rows = d.containers('ops')
            info = d.image(rows[0]['image_id'])
        self.assertEqual(rows[0]['service'], 'ops-web')
        self.assertEqual(info['size_bytes'], 1234)
        self.assertEqual(d.calls, 3)
        self.assertNotIn('.Env', repr(commands))
        self.assertNotIn('{{json .}}', repr(commands))

    def test_cli_root_refused_without_docker_or_writes(self):
        output = io.StringIO()
        with patch.object(bp.os, 'getuid', return_value=0), patch.object(bp.subprocess, 'run') as run, contextlib.redirect_stdout(output):
            status = bp.main([])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'RUN_AS_ORIGINAL_NONROOT_USER_NO_SUDO')
        run.assert_not_called()

    def test_kubernetes_provider_never_falls_back_to_local_docker(self):
        output = io.StringIO()
        with patch.object(bp.subprocess, 'run') as run, contextlib.redirect_stdout(output):
            status = bp.main(['--provider', 'kubernetes'])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'KUBERNETES_INVENTORY_NOT_IMPLEMENTED')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
