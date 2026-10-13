"""Quiescent local-platform backups, never a live shared-infrastructure snapshot."""
import json
import os
from pathlib import Path
import re
import tarfile
import time
from urllib.parse import urlsplit

from common import DATABASES, ROOT, OpsError, docker, private_json, require, sha256, table_counts, public_container


def s3_client(endpoint, access_key, secret_key):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise OpsError('Install ops/requirements.txt for S3 backup/restore') from None
    parsed = urlsplit(endpoint)
    require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username
            and not parsed.password and parsed.path in ('', '/') and not parsed.query and not parsed.fragment,
            'Expected an S3 API HTTP(S) endpoint without credentials/path')
    return boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key, region_name='us-east-1',
                        config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=60,
                                      retries={'total_max_attempts': 3}, s3={'addressing_style': 'path'}))


def inventory(client, bucket):
    require(client.get_bucket_versioning(Bucket=bucket).get('Status') not in ('Enabled', 'Suspended'),
            'Versioned buckets require a version-aware backup; no partial success: ' + bucket)
    rows = []
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
        for row in page.get('Contents', []):
            rows.append({'key': row['Key'], 'size': row['Size'], 'etag': row['ETag'],
                         'modified': row['LastModified'].isoformat()})
    return sorted(rows, key=lambda r: r['key'])


def save_bucket(client, bucket, root, guard):
    before = inventory(client, bucket)
    rows = []
    for index, item in enumerate(before):
        guard()
        obj = client.get_object(Bucket=bucket, Key=item['key'], IfMatch=item['etag'])
        relative = 'objects/' + bucket + '/' + str(index).zfill(9)
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        try:
            with path.open('xb') as out:
                for block in iter(lambda: obj['Body'].read(4 * 1024 * 1024), b''):
                    out.write(block)
                    received += len(block)
                    guard()
                out.flush()
                os.fsync(out.fileno())
        finally:
            obj['Body'].close()
        require(received == item['size'] == obj['ContentLength'], 'Object length changed during backup')
        rows.append(dict(item, file=relative, sha256=sha256(path),
                         metadata=obj.get('Metadata', {}), content_type=obj.get('ContentType', 'application/octet-stream'),
                         tags=client.get_object_tagging(Bucket=bucket, Key=item['key']).get('TagSet', [])))
    require(inventory(client, bucket) == before, 'Bucket changed during backup: ' + bucket)
    return rows


def write_source(root):
    exclusions = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', 'node_modules',
                  '.venv', 'venv', 'results', '.ipynb_checkpoints'}
    with tarfile.open(root / 'configuration/source.tar', 'w') as tar:
        for folder in (ROOT, ROOT.parent / 'docs'):
            for p in sorted(folder.rglob('*')):
                rel = p.relative_to(ROOT.parent)
                if set(rel.parts) & exclusions or p.is_symlink() or not p.is_file():
                    continue
                if p.name.startswith('.env') or p.suffix in ('.pyc', '.tar', '.gz', '.zip') or '.bak' in p.name:
                    continue
                info = tar.gettarinfo(str(p), arcname=rel.as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                with p.open('rb') as stream:
                    tar.addfile(info, stream)


def create(ctx, destination, endpoint, minimum_free_gib=20):
    # A new directory only: no resume/overwrite can accidentally validate a partial backup.
    dest = Path(destination).expanduser().absolute()
    require(not dest.exists() and dest.parent.is_dir(), 'Use a new backup directory in an existing destination filesystem')
    require(ROOT.parent not in dest.parents, 'Store backups outside agent_project')
    ctx.cold()
    require(160000 <= int(ctx.sql('model_operations', 'SHOW server_version_num')) < 170000,
            'This backup/restore procedure is for the deployed PostgreSQL 16 layout')
    require('sqlite:////mlflow/mlflow.db' in (ctx.mlflow['Config'].get('Cmd') or []),
            'Expected the current MLflow SQLite backend')
    env = ctx.config['services']['training']['environment']
    client = s3_client(endpoint, env['MINIO_ACCESS_KEY'], env['MINIO_SECRET_KEY'])
    buckets = sorted(set((env['MINIO_BUCKET'], env['DATASET_BUCKET'], env['GC_LOG_BUCKET'])))
    require(len(buckets) == 3 and all(re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', b) for b in buckets),
            'Three separate named platform buckets required')
    listed = {}
    for bucket in buckets:
        listed[bucket] = inventory(client, bucket)  # Missing buckets are not silently created/skipped.
    scans = {kind: json.loads(ctx.helper(ctx.volumes[kind], 'scan'))
             for kind in ('gpu-admission', 'training-outputs', 'mlflow')}
    estimate = sum(row['size'] for rows in listed.values() for row in rows)
    estimate += sum(r['bytes'] for r in scans.values())
    estimate += sum(p.stat().st_size for folder in (ROOT, ROOT.parent / 'docs')
                    for p in folder.rglob('*') if p.is_file() and not p.is_symlink())
    # DB dumps are usually small relative to model data, but still count source database sizes.
    estimate += sum(int(ctx.sql(db, 'SELECT pg_database_size(current_database())')) for db in DATABASES)
    stat = os.statvfs(dest.parent)
    require(stat.f_bavail * stat.f_frsize >= estimate + minimum_free_gib * 1024 ** 3,
            'Backup destination does not have the estimated data size plus free-space reserve')
    dest.mkdir(mode=0o700)
    old_umask = os.umask(0o077)
    try:
        (dest / 'configuration').mkdir()
        (dest / 'databases').mkdir()
        (dest / 'volumes').mkdir()
        private_json(dest / 'configuration/resolved-compose.json', ctx.config)
        env_file = ROOT / '.env'
        require(env_file.is_file() and not env_file.is_symlink(), 'Original platform .env required')
        (dest / 'configuration/platform.env').write_bytes(env_file.read_bytes())
        write_source(dest)
        counts = {}
        for db in DATABASES:
            ctx.cold()
            counts[db] = table_counts(ctx, db)
            with (dest / ('databases/' + db + '.dump')).open('xb') as out:
                docker('exec', ctx.pg['Id'], 'sh', '-c',
                       'exec pg_dump -Fc -U "$POSTGRES_USER" -d "$1"', 'sh', db,
                       output=out, timeout=3600)
        for kind in scans:
            ctx.cold()
            with (dest / ('volumes/' + kind + '.tar')).open('xb') as out:
                ctx.helper(ctx.volumes[kind], 'archive', output=out, timeout=3600)
        last_check = [0.0]
        def guard():
            if time.monotonic() - last_check[0] >= 30:
                ctx.cold()
                last_check[0] = time.monotonic()
        objects = {}
        for bucket in buckets:
            print('[backup] bucket ' + bucket, flush=True)
            objects[bucket] = save_bucket(client, bucket, dest, guard)
            require(inventory(client, bucket) == listed[bucket], 'Bucket changed since the backup began')
        ctx.cold()
        for db in DATABASES:
            require(counts[db] == table_counts(ctx, db), 'Database changed during backup')
        for bucket in buckets:
            require(inventory(client, bucket) == listed[bucket], 'Bucket changed during backup')
        files = []
        for p in sorted(dest.rglob('*')):
            if p.is_file():
                files.append({'path': p.relative_to(dest).as_posix(), 'size': p.stat().st_size, 'sha256': sha256(p)})
        require(files, 'Empty backup')
        # Only a completely written and rechecked backup receives a manifest.
        minio = ctx.by_name.get('ops-minio1')
        require(minio is not None, 'Local ops-minio1 image required for the isolated drill')
        manifest = {'format': 1, 'scope': 'quiescent-local-model-platform', 'created_at_unix': int(time.time()),
                    'files': files, 'databases': counts, 'objects': objects, 'volumes': ctx.volumes,
                    'postgres_image': ctx.pg['Image'], 'minio_image': minio['Image'],
                    'helper_image': ctx.helper_image,
                    'containers': [public_container(c) for c in ctx.services.values()],
                    'excluded': ['serving-cache', 'Docker container layers/logs not archived by GC',
                                 'Kafka storage', 'other shared databases/buckets', 'bucket IAM/policies/version history']}
        private_json(dest / 'manifest.json', manifest)
        return verify(dest)
    finally:
        os.umask(old_umask)


def verify(directory):
    root = Path(directory).expanduser().absolute()
    require(root.is_dir() and not root.is_symlink(), 'Backup must be a real directory')
    manifest_path = root / 'manifest.json'
    require(manifest_path.is_file() and not manifest_path.is_symlink(), 'Incomplete backup: manifest missing')
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('format') == 1 and manifest.get('scope') == 'quiescent-local-model-platform', 'Unknown backup format')
    require(set(manifest['databases']) == set(DATABASES), 'Database backup set incomplete')
    seen = {}
    for row in manifest['files']:
        rel = Path(row['path'])
        require(not rel.is_absolute() and '..' not in rel.parts and row['path'] == rel.as_posix()
                and row['path'] not in seen and row['path'] != 'manifest.json', 'Unsafe/duplicate backup path')
        current = root
        for part in rel.parts:
            current /= part
            require(not current.is_symlink(), 'Symlink in backup')
        require(current.is_file() and current.stat().st_size == row['size'] and sha256(current) == row['sha256'],
                'Backup file missing or changed: ' + row['path'])
        seen[row['path']] = row
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    require(actual == set(seen) | {'manifest.json'}, 'Unexpected files in backup')
    required = {'configuration/platform.env', 'configuration/resolved-compose.json', 'configuration/source.tar'}
    required.update('databases/' + db + '.dump' for db in DATABASES)
    required.update('volumes/' + kind + '.tar' for kind in ('mlflow', 'gpu-admission', 'training-outputs'))
    require(required <= set(seen), 'Backup component missing')
    require(len(manifest['objects']) == 3, 'Expected three platform buckets')
    object_files = set()
    for bucket, objects in manifest['objects'].items():
        require(re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket), 'Invalid bucket name')
        keys = set()
        for row in objects:
            require(isinstance(row['key'], str) and row['key'] not in keys, 'Duplicate S3 key')
            keys.add(row['key'])
            require(row['file'].startswith('objects/' + bucket + '/') and row['file'] not in object_files,
                    'Invalid object file mapping')
            f = seen.get(row['file'])
            require(f is not None and f['size'] == row['size'] and f['sha256'] == row['sha256'], 'Object manifest mismatch')
            object_files.add(row['file'])
    require(object_files == {p for p in seen if p.startswith('objects/')}, 'Unmapped object data')
    return manifest
