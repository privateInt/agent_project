"""Operational safety/data tests. Docker and S3 calls use controlled substitutes."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import backup
import common
import platform_ops
import restore_drill
import volume_helper


def fixture(root):
    paths = ['configuration/platform.env', 'configuration/resolved-compose.json', 'configuration/source.tar']
    paths += ['databases/' + db + '.dump' for db in common.DATABASES]
    paths += ['volumes/' + k + '.tar' for k in ('mlflow', 'gpu-admission', 'training-outputs')]
    paths += ['objects/model-artifacts/000000000']
    files = []
    for rel in paths:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(('test-' + rel).encode())
        files.append({'path': rel, 'size': p.stat().st_size, 'sha256': common.sha256(p)})
    obj = files[-1]
    manifest = {'format': 1, 'scope': 'quiescent-local-model-platform', 'files': files,
                'databases': {d: {'example': 1} for d in common.DATABASES},
                'objects': {'model-artifacts': [{'file': obj['path'], 'size': obj['size'], 'sha256': obj['sha256'],
                                                'key': '../arbitrary-key', 'metadata': {}, 'content_type': 'text/plain', 'tags': []}],
                            'model-datasets': [], 'model-job-logs': []}}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return manifest


class BackupIntegrityTests(unittest.TestCase):
    def test_complete_backup_and_arbitrary_s3_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = fixture(root)
            self.assertEqual(backup.verify(root), expected)

    def test_incomplete_backup_has_no_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(common.OpsError, 'manifest missing'):
                backup.verify(tmp)

    def test_corruption_extra_file_missing_component_and_duplicate(self):
        for attack in ('changed', 'extra', 'missing', 'duplicate'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = fixture(root)
                if attack == 'changed':
                    (root / manifest['files'][0]['path']).write_bytes(b'corrupt')
                elif attack == 'extra':
                    (root / 'unexpected').write_bytes(b'extra')
                elif attack == 'missing':
                    row = manifest['files'].pop(0)
                    (root / row['path']).unlink()
                else:
                    manifest['files'].append(manifest['files'][0])
                (root / 'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaises(common.OpsError):
                    backup.verify(root)

    def test_traversal_symlink_and_object_mapping_rejected(self):
        for attack in ('traversal', 'symlink', 'mapping'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = fixture(root)
                if attack == 'traversal':
                    manifest['files'][0]['path'] = '../outside'
                elif attack == 'symlink':
                    p = root / manifest['files'][0]['path']
                    p.unlink()
                    p.symlink_to('/etc/passwd')
                else:
                    manifest['objects']['model-artifacts'][0]['sha256'] = '0' * 64
                (root / 'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaises(common.OpsError):
                    backup.verify(root)

    def test_private_report_never_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'report.json'
            common.private_json(p, {'result': 'first'})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                common.private_json(p, {'result': 'second'})
            self.assertEqual(json.loads(p.read_text())['result'], 'first')


class S3Tests(unittest.TestCase):
    def source(self):
        client = Mock()
        client.get_bucket_versioning.return_value = {}
        row = {'Key': '../../untrusted/model', 'Size': 6, 'ETag': 'etag', 'LastModified': datetime.now(timezone.utc)}
        client.get_paginator.return_value.paginate.return_value = [{'Contents': [row]}]
        client.get_object.return_value = {'Body': io.BytesIO(b'model!'), 'ContentLength': 6, 'Metadata': {'origin': 'fixture'}, 'ContentType': 'application/octet-stream'}
        client.get_object_tagging.return_value = {'TagSet': []}
        return client

    def test_copy_uses_opaque_path_and_readback_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self.source()
            rows = backup.save_bucket(client, 'model-artifacts', Path(tmp), lambda: None)
            p = Path(tmp) / rows[0]['file']
            self.assertEqual(os.path.commonpath((str(p), tmp)), tmp)
            self.assertEqual(p.read_bytes(), b'model!')
            self.assertEqual(rows[0]['sha256'], hashlib.sha256(b'model!').hexdigest())
            self.assertEqual(client.get_object.call_args.kwargs['IfMatch'], 'etag')
            self.assertTrue(client.get_object.return_value['Body'].closed)

    def test_versioned_and_short_stream_fail(self):
        for mode in ('Enabled', 'Suspended', 'short'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                client = self.source()
                if mode == 'short':
                    client.get_object.return_value['Body'] = io.BytesIO(b'bad')
                else:
                    client.get_bucket_versioning.return_value = {'Status': mode}
                with self.assertRaises(common.OpsError):
                    backup.save_bucket(client, 'model-artifacts', Path(tmp), lambda: None)

    def test_bucket_changes_during_copy_fail(self):
        client = self.source()
        row = {'key': 'file', 'size': 6, 'etag': 'etag', 'modified': 'fixed'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(backup, 'inventory', side_effect=[[row], []]):
            with self.assertRaisesRegex(common.OpsError, 'changed'):
                backup.save_bucket(client, 'model-artifacts', Path(tmp), lambda: None)


class MaintenanceTests(unittest.TestCase):
    def test_cold_rejects_uncertain_job_and_unfinished_database(self):
        def item(name, owner=None):
            return {'Id': name, 'Name': '/' + name, 'Image': 'sha256:example', 'State': {'Status': 'exited'},
                    'Config': {'Labels': {'model-platform.owner': owner} if owner else {}}, 'Mounts': []}
        ctx = common.Context.__new__(common.Context)
        service, mlflow, pg = item('service'), item('mlflow'), item('postgres')
        pg['State'] = {'Status': 'running', 'Running': True}
        ctx.services, ctx.mlflow, ctx.pg = {'test': service}, mlflow, pg
        ctx.volumes = {}
        ctx.sql = Mock(return_value='0')
        items = [service, mlflow, pg]
        with patch.object(common, 'containers', return_value=items):
            ctx.cold()
            ctx.sql.return_value = '1'
            with self.assertRaisesRegex(common.OpsError, 'Unfinished'):
                ctx.cold()
        ctx.sql.return_value = '0'
        uncertain = item('job', 'operation-dispatcher')
        uncertain['State']['Status'] = 'created'
        with patch.object(common, 'containers', return_value=items + [uncertain]):
            with self.assertRaisesRegex(common.OpsError, 'uncertain'):
                ctx.cold()

    def test_cache_plan_does_not_delete_and_apply_targets_one_id(self):
        ctx = Mock()
        ctx.volumes = {'serving-cache': 'model-serving-cache'}
        ctx.sql.return_value = 'stopped/stopped'
        ctx.helper.return_value = b'{"removed":true}'
        dep = '306f2dc3-f68f-484c-959a-a318f63df2ee'
        result = platform_ops.clean_cache(ctx, dep, False)
        self.assertFalse(result['apply'])
        ctx.helper.assert_not_called()
        platform_ops.clean_cache(ctx, dep, True)
        ctx.helper.assert_called_once_with('model-serving-cache', 'delete-cache', args=(dep,))

    def test_cache_rejects_active_missing_and_noncanonical_id(self):
        dep = '306f2dc3-f68f-484c-959a-a318f63df2ee'
        for value in ('running/ready', 'stopped/stopping', ''):
            ctx = Mock()
            ctx.sql.return_value = value
            with self.assertRaises(common.OpsError):
                platform_ops.clean_cache(ctx, dep, True)
            ctx.helper.assert_not_called()
        with self.assertRaises((common.OpsError, ValueError)):
            platform_ops.clean_cache(Mock(), '../outside', True)

    def test_cold_gate_precedes_destination_creation(self):
        ctx = Mock()
        ctx.cold.side_effect = common.OpsError('running work')
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'backup'
            with self.assertRaisesRegex(common.OpsError, 'running work'):
                backup.create(ctx, target, 'http://127.0.0.1:19000')
            self.assertFalse(target.exists())

    def test_pressure_is_not_summed_and_covers_inode_exhaustion(self):
        stats = {'available_bytes': 200 * 1024 ** 3, 'total_bytes': 4000 * 1024 ** 3,
                 'available_inodes': 800, 'inodes': 1000}
        self.assertEqual(platform_ops.pressure(stats), 'warning')
        self.assertEqual(platform_ops.pressure(dict(stats, available_inodes=1)), 'critical')
        self.assertEqual(platform_ops.pressure(dict(stats, available_bytes=1024)), 'critical')

    def test_only_exited_is_stopped(self):
        for state in ('created', 'running', 'paused', 'restarting', 'dead', 'unknown'):
            self.assertFalse(common.stopped({'State': {'Status': state}}))
        self.assertTrue(common.stopped({'State': {'Status': 'exited'}}))

    def test_tar_rejects_links_special_and_traversal(self):
        for name, kind in (('../outside', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hard', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE)):
            item = tarfile.TarInfo(name)
            item.type = kind
            with self.assertRaises(ValueError):
                volume_helper.safe_member(item)

    def test_volume_fingerprints_detect_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'checkpoint').write_bytes(b'training-state')
            before = volume_helper.fingerprints(root)
            (root / 'checkpoint').write_bytes(b'training-other')
            self.assertNotEqual(before, volume_helper.fingerprints(root))

    def test_restore_refuses_invalid_backup_before_creating_docker_resources(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(restore_drill, 'docker') as call:
            with self.assertRaises(common.OpsError):
                restore_drill.execute(tmp)
            call.assert_not_called()

    def test_subprocess_failure_does_not_leak_stderr_or_arguments(self):
        result = Mock(returncode=1, stderr=b'password=secret', stdout=b'')
        with patch.object(common.subprocess, 'run', return_value=result):
            with self.assertRaises(common.OpsError) as caught:
                common.run(['docker', 'secret-value'])
        self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
