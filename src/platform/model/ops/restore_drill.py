"""Restore only into newly created disposable Docker resources, never source services."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time

from backup import s3_client, verify
from common import DATABASES, ROOT, docker, require, local_daemon
from volume_helper import safe_member


def image_id(value):
    require(isinstance(value, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', value), 'Invalid backup image ID')
    return value


def sql(container, database, query):
    return docker('exec', '-i', container, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                  '-At', '-U', 'postgres', '-d', database, data=query.encode(), timeout=300).decode().strip()


def stream_in(args, path, timeout=3600):
    with Path(path).open('rb') as data:
        result = subprocess.run(['docker', *args], stdin=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
    require(result.returncode == 0, 'Isolated restore command failed; source services were not modified')
    return result.stdout


def restore_volumes(root, manifest, prefix, volumes, containers):
    result = {}
    for kind in ('gpu-admission', 'training-outputs', 'mlflow'):
        archive = root / ('volumes/' + kind + '.tar')
        # Validate before allocating writable targets. No links or special devices.
        expected_files = {}
        with tarfile.open(archive) as tar:
            for item in tar:
                safe_member(item)
                if item.isfile():
                    digest = hashlib.sha256()
                    with tar.extractfile(item) as stream:
                        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                            digest.update(block)
                    expected_files[Path(item.name).as_posix()] = {'size': item.size, 'sha256': digest.hexdigest(),
                                                               'uid': item.uid, 'gid': item.gid, 'mode': item.mode & 0o777}
        volume = prefix + '-' + kind
        docker('volume', 'create', '--label', 'model-platform.restore-drill=' + prefix, volume)
        volumes.append(volume)
        container = prefix + '-unpack-' + kind
        code = (ROOT / 'ops/volume_helper.py').read_text().split("if __name__ == '__main__':")[0] + '''
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as tar:
    for item in tar:
        safe_member(item)
        tar.extract(item, '/data', filter='data')
        p = Path('/data') / item.name
        os.chown(p, item.uid, item.gid)
        os.chmod(p, item.mode & 0o777)
print(json.dumps(fingerprints(Path('/data'))))
'''
        containers.append(container)
        restored_files = json.loads(stream_in(['run', '--rm', '--pull=never', '-i', '--name', container,
                               '--network', 'none', '--read-only', '--user', '0:0',
                               '--mount', 'type=volume,src=' + volume + ',dst=/data',
                               '--entrypoint', 'python', image_id(manifest['helper_image']), '-c', code], archive))
        require(restored_files == expected_files, 'Restored volume file checksum mismatch: ' + kind)
        result[kind] = {'files': len(restored_files), 'readback': True}
        if kind == 'mlflow':
            check = "import sqlite3; c=sqlite3.connect('file:/data/mlflow.db?mode=ro',uri=True); rows=c.execute('PRAGMA integrity_check').fetchall(); assert rows==[('ok',)]; print('ok')"
            docker('run', '--rm', '--pull=never', '--network', 'none', '--user', '0:0',
                   '--mount', 'type=volume,src=' + volume + ',dst=/data,readonly',
                   '--entrypoint', 'python', image_id(manifest['helper_image']), '-c', check)
            result[kind]['sqlite_integrity'] = 'ok'
    return result


def execute(directory):
    root = Path(directory).expanduser().absolute()
    manifest = verify(root)
    local_daemon()
    needed = sum(f['size'] for f in manifest['files'])
    # Do not guess free space if the daemon data root is inaccessible to this account.
    # Probe the current filesystem through a disposable container using the same daemon.
    probe = docker('run', '--rm', '--pull=never', '--network', 'none', '--entrypoint', 'python',
                   image_id(manifest['helper_image']), '-c',
                   "import os,json; s=os.statvfs('/'); print(json.dumps(s.f_bavail*s.f_frsize))")
    require(int(json.loads(probe)) >= needed + 20 * 1024 ** 3, 'Docker storage lacks room for a full isolated restore plus 20 GiB reserve')
    for key in ('postgres_image', 'minio_image', 'helper_image'):
        docker('image', 'inspect', image_id(manifest[key]))  # Never pull a different mutable tag.
    prefix = 'model-restore-' + os.urandom(12).hex()
    names, volumes = [], []
    result = {'scope': 'isolated_restore_drill', 'backup_manifest_sha256': hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest(),
              'databases': {}, 'objects': {}, 'source_services_modified': False}
    try:
        pg_volume = prefix + '-pg'
        docker('volume', 'create', '--label', 'model-platform.restore-drill=' + prefix, pg_volume)
        volumes.append(pg_volume)
        pg = prefix + '-pg'
        names.append(pg)
        docker('run', '-d', '--pull=never', '--name', pg, '--network', 'none',
               '--mount', 'type=volume,src=' + pg_volume + ',dst=/var/lib/postgresql/data',
               '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', image_id(manifest['postgres_image']))
        ready = False
        for _ in range(45):
            p = subprocess.run(['docker', 'exec', pg, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if p.returncode == 0:
                ready = True
                break
            time.sleep(1)
        require(ready, 'Isolated PostgreSQL did not become ready')
        for db in DATABASES:
            docker('exec', pg, 'createdb', '-U', 'postgres', db)
            stream_in(['exec', '-i', pg, 'pg_restore', '--exit-on-error', '--no-owner', '--no-acl',
                       '-U', 'postgres', '-d', db], root / ('databases/' + db + '.dump'))
            expected = manifest['databases'][db]
            tables = sql(pg, db, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").splitlines()
            require(set(tables) == set(expected), 'Restored table set mismatch')
            for table in tables:
                require(re.fullmatch(r'[a-z_][a-z0-9_]*', table), 'Invalid table identifier')
                require(int(sql(pg, db, 'SELECT count(*) FROM public."' + table + '"')) == expected[table],
                        'Restored row count mismatch: ' + db + '.' + table)
            result['databases'][db] = {'tables': len(tables), 'rows': sum(expected.values()), 'restored': True}
        print('[restore] PostgreSQL schema, data and constraints restored', flush=True)
        result['volumes'] = restore_volumes(root, manifest, prefix, volumes, names)
        mv = prefix + '-s3-data'
        docker('volume', 'create', '--label', 'model-platform.restore-drill=' + prefix, mv)
        volumes.append(mv)
        mc = prefix + '-s3'
        names.append(mc)
        access, secret = 'drill' + os.urandom(8).hex(), os.urandom(24).hex()
        docker('run', '-d', '--pull=never', '--name', mc, '--network', 'bridge',
               '-p', '127.0.0.1::9000', '--mount', 'type=volume,src=' + mv + ',dst=/data',
               '-e', 'MINIO_ROOT_USER=' + access, '-e', 'MINIO_ROOT_PASSWORD=' + secret,
               '--entrypoint', 'minio', image_id(manifest['minio_image']), 'server', '/data')
        instance = json.loads(docker('inspect', mc))[0]
        port = instance['NetworkSettings']['Ports']['9000/tcp'][0]
        require(port['HostIp'] == '127.0.0.1', 'Drill endpoint must be local-only')
        client = s3_client('http://127.0.0.1:' + port['HostPort'], access, secret)
        ready = False
        for _ in range(45):
            try:
                client.list_buckets()
                ready = True
                break
            except Exception:
                time.sleep(1)
        require(ready, 'Isolated MinIO did not become ready')
        for bucket, rows in manifest['objects'].items():
            client.create_bucket(Bucket=bucket)
            for row in rows:
                # upload_file supports objects larger than a single PutObject request.
                client.upload_file(str(root / row['file']), bucket, row['key'],
                                   ExtraArgs={'Metadata': row['metadata'], 'ContentType': row['content_type']})
                if row['tags']:
                    client.put_object_tagging(Bucket=bucket, Key=row['key'], Tagging={'TagSet': row['tags']})
                obj = client.get_object(Bucket=bucket, Key=row['key'])
                digest, size = hashlib.sha256(), 0
                try:
                    for block in iter(lambda: obj['Body'].read(4 * 1024 * 1024), b''):
                        digest.update(block)
                        size += len(block)
                finally:
                    obj['Body'].close()
                require(size == row['size'] and digest.hexdigest() == row['sha256'], 'Restored object checksum mismatch')
                require(obj.get('Metadata', {}) == row['metadata'] and obj.get('ContentType') == row['content_type'],
                        'Restored object metadata mismatch')
            result['objects'][bucket] = {'count': len(rows), 'bytes': sum(r['size'] for r in rows), 'readback': True}
            print('[restore] bucket ' + bucket + ' readback complete', flush=True)
        result['passed'] = True
    finally:
        # Cleanup only resources generated in this invocation; never use prune or source names.
        failures = []
        for name in reversed(names):
            p = subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if p.returncode and subprocess.run(['docker', 'inspect', name], stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, timeout=30).returncode == 0:
                failures.append(name)
        for name in reversed(volumes):
            p = subprocess.run(['docker', 'volume', 'rm', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if p.returncode:
                failures.append(name)
        require(not failures, 'Drill cleanup incomplete; inspect only these generated resources: ' + ', '.join(failures))
    result['temporary_resources_removed'] = True
    return result
