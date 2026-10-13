"""Executed inside a short-lived local CPU container; one mount at /data."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import uuid


def scan(root):
    size = allocated = count = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(base) / name
            st = p.lstat()
            if p.is_symlink():
                raise ValueError('Symlink in retained volume')
            if p.is_file():
                size += st.st_size
                allocated += st.st_blocks * 512
                count += 1
    return {'files': count, 'bytes': size, 'allocated_bytes': allocated}


def safe_member(item):
    p = Path(item.name)
    if p.is_absolute() or '..' in p.parts or not (item.isfile() or item.isdir()):
        raise ValueError('Unsafe archive member')
    if not (0 <= item.uid < 2 ** 32 - 1 and 0 <= item.gid < 2 ** 32 - 1):
        raise ValueError('Invalid archive ownership')
    return item


def fingerprints(root):
    rows = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            if (Path(base) / name).is_symlink():
                raise ValueError('Symlink in restored volume')
        for name in files:
            p = Path(base) / name
            digest = hashlib.sha256()
            with p.open('rb') as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                    digest.update(block)
            info = p.stat()
            rows[p.relative_to(root).as_posix()] = {'size': info.st_size, 'sha256': digest.hexdigest(),
                                                  'uid': info.st_uid, 'gid': info.st_gid, 'mode': info.st_mode & 0o777}
    return rows


def archive(root):
    def checked(item):
        safe_member(item)
        # Numeric ownership matters for MLflow or another non-root volume consumer.
        item.uname = item.gname = ''
        return item
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:
        tar.add(root, arcname='.', filter=checked)


def main():
    root = Path('/data')
    mode = sys.argv[1]
    if mode == 'scan':
        st = os.statvfs(root)
        result = scan(root)
        result.update(total_bytes=st.f_blocks * st.f_frsize, available_bytes=st.f_bavail * st.f_frsize,
                      inodes=st.f_files, available_inodes=st.f_favail)
        print(json.dumps(result))
    elif mode == 'archive':
        archive(root)
    elif mode == 'delete-cache':
        value = sys.argv[2]
        if str(uuid.UUID(value)) != value:
            raise ValueError('Canonical deployment ID required')
        target = root / value
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError('Unsafe cache directory')
        before = scan(target) if target.exists() else {'files': 0, 'bytes': 0, 'allocated_bytes': 0}
        if target.exists():
            shutil.rmtree(target)
        print(json.dumps({'deployment_id': value, 'removed': not target.exists(), 'before': before}))
    else:
        raise ValueError('Unknown volume operation')


if __name__ == '__main__':
    main()
