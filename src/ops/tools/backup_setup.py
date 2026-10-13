#!/usr/bin/env python3
"""Prepare a private local recovery destination and a new age identity, once.

No production container, worker or application access. Existing keys, partial
preparations and conflicting paths are never overwritten or repaired.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
SPEC = 'src/ops/tools/backup-production-plan-20261001.json'
SPEC_SHA256 = '61b4a85589e9ad6abed491c004c7307c12e79fb0059d24d066fc0172292c6328'


class Failure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(ok, code):
    if not ok:
        raise Failure(code)


def safe(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts and str(path) != '/'
            and not any(ord(c) < 32 or ord(c) == 127 for c in str(path)), 'BACKUP_PATH_INVALID')
    for parent in (*reversed(path.parents), path):
        require(not parent.is_symlink(), 'BACKUP_SYMLINK_REFUSED')
    return path


def private(path, directory=True):
    path = safe(path)
    info = path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and info.st_uid == os.getuid() and not info.st_mode & 0o7077,
            'BACKUP_PRIVATE_PATH_REQUIRED')
    require(directory or info.st_nlink == 1, 'BACKUP_HARDLINK_REFUSED')
    return path


def signature(path):
    s = private(path, False).stat()
    return [s.st_dev, s.st_ino, s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode),
            s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def dir_identity(path):
    s = private(path).stat()
    return [s.st_dev, s.st_ino, s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def json_read(path):
    path = private(path, False)
    require(path.stat().st_size <= 262144, 'BACKUP_RECORD_LIMIT')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'BACKUP_DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=unique)


def durable_new(path, value):
    fd = os.open(safe(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, sort_keys=True, indent=2)
        out.write('\n'); out.flush(); os.fsync(out.fileno())
    fsync_dir(Path(path).parent)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def paths(home):
    home = safe(home)
    parent = home / '.local/state/agent-project-ops'
    control = parent / 'backup-control'
    return dict(parent=parent, control=control, destination=parent/'backups',
                tools=control/'tools', keys=control/'keys', identity=control/'keys/identity.agekey',
                public=control/'recipient.json', config=control/'prepared.json')


def reviewed(root=ROOT):
    spec = safe(safe(root)/SPEC)
    info = spec.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not info.st_mode & 0o022, 'BACKUP_SOURCE_SPEC_INVALID')
    require(sha(spec) == SPEC_SHA256, 'BACKUP_REVIEWED_SOURCE_CHANGED')
    doc = json.loads(spec.read_text())
    require(doc['schema'] == 1, 'BACKUP_SOURCE_SPEC_INVALID')
    for rel, wanted in doc['source_pins'].items():
        path = safe(root/rel)
        s = path.lstat()
        require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and sha(path) == wanted,
                'BACKUP_REVIEWED_SOURCE_CHANGED')
    return doc


def age_module(root=ROOT):
    reviewed(root)
    spec = importlib.util.spec_from_file_location('_backup_setup_age',
            root/'src/ops/candidates/backup_capture_20260930/age_tool.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepared(home, root=ROOT):
    """Read public configuration, tool bodies and identity metadata only."""
    p = paths(home)
    for name in ('parent', 'control', 'destination', 'tools', 'keys'):
        private(p[name])
    value = json_read(p['config'])
    age = age_module(root)
    pinned = reviewed(root)['tools']
    require(value.get('schema') == 1 and value.get('kind') == 'local-backup-preparation-v1'
            and value.get('paths') == {k:str(v) for k,v in p.items()}
            and value.get('uid') == os.getuid() and value.get('gid') == os.getgid(),
            'BACKUP_PREPARATION_BINDING_CHANGED')
    age.recipients([value['recipient']])
    require(value['identity_signature'] == signature(p['identity']), 'BACKUP_IDENTITY_METADATA_CHANGED')
    require(value['public_signature'] == signature(p['public']) and
            json_read(p['public']) == dict(recipient=value['recipient'],identity_signature=value['identity_signature']),
            'BACKUP_PUBLIC_RECIPIENT_CHANGED')
    require(value['destination_identity'] == dir_identity(p['destination'])
            and value['control_identity'] == dir_identity(p['control']), 'BACKUP_DESTINATION_CHANGED')
    require(set(value['tools']['binaries']) == {'age', 'age-keygen'}, 'BACKUP_TOOL_RECORD_INVALID')
    require(all(value['tools'].get(k) == v for k,v in pinned.items()), 'BACKUP_TOOL_RELEASE_CHANGED')
    require(value['tools'].get('temporary_only') is False and value['tools'].get('sigsum_proof_verified') is False
            and value['tools'].get('scope') == 'private_selected_backup_control_no_system_install',
            'BACKUP_TOOL_RECORD_INVALID')
    require(value['tools']['version'] == age.VERSION and value['tools']['archive_sha256']
            in {row[2] for row in age.RELEASES.values()}, 'BACKUP_TOOL_RELEASE_CHANGED')
    for name, digest in value['tools']['binaries'].items():
        private(p['tools']/name, False)
        require(sha(p['tools']/name) == digest and os.access(p['tools']/name, os.X_OK), 'BACKUP_TOOL_CHANGED')
    require(sha(p['tools']/'age') == value['age_sha256'], 'BACKUP_TOOL_CHANGED')
    return value


def public_result(value, changed):
    return dict(result='passed', setup_ready=True, new_identity_created=changed,
                existing_identity_preserved=not changed, destination=value['paths']['destination'],
                destination_role='local_recovery_same_source_filesystem_possible',
                recipient=value['recipient'], identity_path=value['paths']['identity'],
                identity_contents_returned=False, identity_custody_verified=False,
                tools=value['tools'], production_backup_executed=False, backup_ready=False,
                next_step='keep_identity_in_separate_custody_then_read_only_backup_plan')


def prepare(home, root=ROOT, fetch=None):
    age = age_module(root)
    require(platform.system() == 'Linux' and platform.machine() == 'x86_64', 'BACKUP_TOOL_ARCHITECTURE_UNAVAILABLE')
    pinned = reviewed(root)['tools']
    p = paths(home)
    # Create only selected actor directories; never chmod an existing path.
    for folder in (p['parent'].parent.parent, p['parent'].parent, p['parent']):
        safe(folder)
        if not folder.exists():
            folder.mkdir(mode=0o700); fsync_dir(folder.parent)
    private(p['parent'])
    if not p['control'].exists():
        p['control'].mkdir(mode=0o700); fsync_dir(p['parent'])
    private(p['control'])
    lock = p['control']/'prepare.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private(lock, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('BACKUP_PREPARATION_BUSY') from None
        if p['config'].exists():
            return public_result(prepared(home, root), False)
        require(set(x.name for x in p['control'].iterdir()) == {'prepare.lock'},
                'BACKUP_PREPARATION_INCOMPLETE_KEEP_EXISTING_KEY')
        if p['destination'].exists():
            private(p['destination'])
            require(not list(p['destination'].iterdir()), 'BACKUP_UNREVIEWED_DESTINATION_NOT_EMPTY')
        else:
            p['destination'].mkdir(mode=0o700); fsync_dir(p['parent'])
        for name in ('tools', 'keys'):
            p[name].mkdir(mode=0o700)
        fsync_dir(p['control'])
        tool = (fetch or age.fetch)(p['tools'])
        require(all(tool.get(k) == v for k,v in pinned.items()), 'BACKUP_TOOL_RELEASE_CHANGED')
        tool = dict(tool, temporary_only=False, scope='private_selected_backup_control_no_system_install')
        keygen = private(p['tools']/'age-keygen', False)
        private(p['tools']/'age', False)
        require(set(tool['binaries']) == {'age', 'age-keygen'} and
                all(sha(p['tools']/k) == v for k,v in tool['binaries'].items()), 'BACKUP_TOOL_CHANGED')
        pending = p['keys']/'identity.pending'
        keyfd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        env = {'PATH':os.defpath, 'LC_ALL':'C', 'HOME':'/nonexistent'}
        try:
            subprocess.run([str(keygen)], stdin=subprocess.DEVNULL, stdout=keyfd,
                           stderr=subprocess.DEVNULL, env=env, check=True, timeout=10)
            os.fsync(keyfd)
        finally:
            os.close(keyfd)
        require(0 < pending.stat().st_size <= 4096, 'BACKUP_NEW_IDENTITY_SIZE_INVALID')
        result = subprocess.run([str(keygen), '-y', str(pending)], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, check=True, timeout=10)
        require(len(result.stdout) <= 128, 'BACKUP_PUBLIC_RECIPIENT_INVALID')
        recipient = result.stdout.decode().strip(); age.recipients([recipient])
        # No existing identity can be present in this exclusively locked fresh preparation.
        require(not p['identity'].exists(), 'BACKUP_EXISTING_IDENTITY_REFUSED')
        pending.rename(p['identity']); fsync_dir(p['keys'])
        durable_new(p['public'],dict(recipient=recipient,identity_signature=signature(p['identity'])))
        value = dict(schema=1, kind='local-backup-preparation-v1', uid=os.getuid(), gid=os.getgid(),
                     paths={k:str(v) for k,v in p.items()}, recipient=recipient,
                     identity_signature=signature(p['identity']), destination_identity=dir_identity(p['destination']),
                     control_identity=dir_identity(p['control']), tools=tool,
                     age_sha256=sha(p['tools']/'age'),public_signature=signature(p['public']))
        durable_new(p['config'], value)
        return public_result(prepared(home, root), True)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'status'))
    args = parser.parse_args()
    report = dict(schema=1, stage='O04-production-backup-preparation', action=args.action,
                  production_service_control_requested=False, production_credentials_read=False,
                  application_api_requests=0, inference_requested=False, system_package_install_requested=False)
    try:
        report.update(prepare(Path.home()) if args.action == 'prepare'
                      else public_result(prepared(Path.home()), False))
    except Exception as exc:
        code = getattr(exc, 'code', None)
        report.update(result='attention_required', code=code if isinstance(code,str) and
                      re.fullmatch('[A-Z][A-Z0-9_]{0,95}',code) else 'BACKUP_PREPARATION_FAILED',
                      setup_ready=False, backup_ready=False, identity_contents_returned=False,
                      next_step='retain_partial_preparation_never_overwrite_or_regenerate_identity')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['result'] == 'passed' else 2


if __name__ == '__main__':
    sys.exit(main())
