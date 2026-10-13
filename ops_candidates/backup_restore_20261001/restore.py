"""Inactive isolated complete-generation restore, with explicit trusted receipt.

Fresh private staging only. Sources need not exist. No Docker/service command,
chown, arbitrary extraction, automatic start or application-restore claim.
Foreign UID/GID is refused before publication; it is not silently mapped.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import time
import uuid

from ops_candidates.backup_capture_20260930 import capture as c, age_tool as a
from ops_candidates.backup_generation_20261001 import generation as g
from . import image_archive as image

require = a.require
KIND = 'isolated-generation-restore-v1'
HASH = re.compile('[0-9a-f]{64}')


def read_json(path, limit=c.MAX_MANIFEST):
    path = a.private(Path(path), False)
    require(0 < path.stat().st_size <= limit, 'RESTORE_JSON_LIMIT')
    return image.json_value(path.read_bytes())


def plan(folder, trusted_receipt, parent, identity_path, *, seconds=300, max_bytes=2**34, max_entries=65536):
    """Metadata review only; identity/source bodies are not read by this plan."""
    require(type(seconds) is int and 1 <= seconds <= 3600 and type(max_bytes) is int and 0 < max_bytes <= 2**40
            and type(max_entries) is int and 1 <= max_entries <= c.MAX_ENTRIES, 'RESTORE_BUDGET_INVALID')
    folder = a.private(Path(folder)); parent = a.private(Path(parent)); key = a.private(Path(identity_path), False)
    require(not g.overlapping(folder, parent) and not key.is_relative_to(folder) and not key.is_relative_to(parent)
            and key.stat().st_size <= 4096, 'RESTORE_DESTINATION_OR_KEY_OVERLAP')
    receipt = read_json(folder/'receipt.json', 65536); record = read_json(folder/'generation.json')
    require(receipt == trusted_receipt and receipt.get('schema') == 1 and receipt.get('kind') == g.KIND
            and receipt.get('encryption') == 'age_v1' and receipt.get('id') == str(uuid.UUID(receipt['id']))
            and isinstance(receipt.get('review_id'), str) and HASH.fullmatch(receipt['review_id']), 'RESTORE_TRUSTED_RECEIPT_REQUIRED')
    require(record.get('schema') == 1 and record.get('kind') == g.KIND and record.get('phase') == 'captured'
            and record.get('id') == receipt['id'] and record.get('review_id') == receipt['review_id']
            and record.get('receipt_sha256') == c.fingerprint(receipt), 'RESTORE_GENERATION_NOT_ACCEPTED')
    artifacts = receipt.get('artifacts')
    require(isinstance(artifacts, dict) and 3 <= len(artifacts) <= 98 and {'files', 'catalog'} <= set(artifacts)
            and set(artifacts) == set(record['artifacts']), 'RESTORE_ARTIFACT_SET')
    inputs = {}; total = 0; paths = set()
    for role, row in artifacts.items():
        require(isinstance(role, str) and (role in ('files', 'catalog') or role.startswith('storage-') and c.ROLE.fullmatch(role[8:])
                or role.startswith('image-') and HASH.fullmatch(role[6:])), 'RESTORE_ARTIFACT_ROLE')
        require(isinstance(row, dict) and set(row) == {'path', 'ciphertext_sha256', 'ciphertext_bytes'}
                and isinstance(row['ciphertext_sha256'], str) and HASH.fullmatch(row['ciphertext_sha256'])
                and type(row['ciphertext_bytes']) is int and 0 < row['ciphertext_bytes'] <= max_bytes,
                'RESTORE_ARTIFACT_INVALID')
        name = c.safe_name(row['path']); require(name not in paths, 'RESTORE_ARTIFACT_COLLISION'); paths.add(name)
        path = a.private(folder/name, False); total += row['ciphertext_bytes']
        require(path.stat().st_size == row['ciphertext_bytes'] and total <= max_bytes
                and {k: record['artifacts'][role][k] for k in row} == row, 'RESTORE_ARTIFACT_SIZE_OR_RECORD')
        inputs[role] = c.signature(path.stat())
    value = {'schema': 1, 'kind': KIND, 'id': receipt['id'], 'folder': str(folder), 'parent': str(parent), 'key': str(key),
             'folder_identity': c.identity(folder.stat()), 'parent_identity': c.identity(parent.stat()),
             'key_signature': c.signature(key.stat()), 'input_signatures': inputs, 'receipt': deepcopy(receipt),
             'seconds': seconds, 'max_bytes': max_bytes, 'max_entries': max_entries}
    return dict(value, review_id=c.fingerprint(value))


class Restore:
    def __init__(self, proposal, tool, *, hook=lambda phase: None):
        self.p = deepcopy(proposal); self.tool = tool; self.hook = hook; self.stage = None; self.process = None
        self.bytes = 0; self.entries = 0; self.metadata = []; self.raw = None
        self.materialized = {}; self.image_files = {}

    def guard(self):
        require(time.monotonic() < self.until, 'RESTORE_DEADLINE')
        value = deepcopy(self.p); review = value.pop('review_id', None)
        require(review == c.fingerprint(value), 'RESTORE_REVIEW_CHANGED')
        require(c.identity(a.private(self.parent).stat()) == self.p['parent_identity']
                and c.identity(a.private(self.folder).stat()) == self.p['folder_identity'], 'RESTORE_DIRECTORY_CHANGED')
        require(c.signature(a.private(self.key, False).stat()) == self.p['key_signature'], 'RESTORE_IDENTITY_CHANGED')
        if self.stage is not None:
            require(c.identity(a.private(self.stage).stat()) == self.stage_id, 'RESTORE_STAGE_CHANGED')

    def account(self, count):
        self.bytes += count; require(self.bytes <= self.p['max_bytes'], 'RESTORE_PLAINTEXT_BUDGET')

    def input(self, role):
        self.guard(); row = self.p['receipt']['artifacts'][role]; path = a.private(self.folder/row['path'], False)
        signature = self.p['input_signatures'][role]
        require(c.signature(path.stat()) == signature, 'RESTORE_INPUT_CHANGED')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW); digest = hashlib.sha256()
        try:
            require(c.signature(os.fstat(fd)) == signature, 'RESTORE_INPUT_CHANGED')
            while True:
                self.guard(); data = os.read(fd, 65536)
                if not data: break
                digest.update(data)
            require(digest.hexdigest() == row['ciphertext_sha256'] and c.signature(os.fstat(fd)) == signature,
                    'RESTORE_CIPHERTEXT_CHANGED')
            os.lseek(fd, 0, os.SEEK_SET)
            return fd
        except BaseException:
            os.close(fd); raise

    def decrypt(self, role, consume, limit):
        fd = self.input(role)
        try:
            self.process = self.tool.spawn(['-d', '-i', str(self.key), '-o', '-'], input_fd=fd, guard=self.guard)
            pipe = a.Pipe(self.process.stdout.fileno(), 'read', self.until, self.guard, read_limit=limit)
            result = consume(pipe)
            # Consumers must read through authenticated EOF, not stop at tar
            # termination or a parseable JSON prefix.
            require(not pipe.read(1), 'RESTORE_CONSUMER_EOF_REQUIRED')
            self.tool.finish(self.process, self.until); self.guard()
            path = self.folder/self.p['receipt']['artifacts'][role]['path']
            require(c.signature(a.private(path, False).stat()) == self.p['input_signatures'][role], 'RESTORE_INPUT_CHANGED')
            self.process.stdout.close(); self.process = None; return result
        finally:
            os.close(fd); self.tool.stop(self.process); self.process = None

    def catalog(self):
        def consume(pipe):
            parts = []; total = 0
            while True:
                data = pipe.read(65536)
                if not data: break
                total += len(data); require(total <= c.MAX_MANIFEST, 'RESTORE_CATALOG_LIMIT'); parts.append(data)
            raw = b''.join(parts); self.account(len(raw)); return image.json_value(raw)
        value = self.decrypt('catalog', consume, c.MAX_MANIFEST)
        require(isinstance(value, dict) and value.get('schema') == 1 and value.get('kind') == g.KIND
                and value.get('id') == self.p['id'] and isinstance(value.get('proposal'), dict), 'RESTORE_CATALOG_INVALID')
        proposal = deepcopy(value['proposal']); review = proposal.pop('review_id', None)
        require(review == self.p['receipt']['review_id'] == c.fingerprint(proposal) and proposal.get('id') == self.p['id']
                and proposal.get('schema') == 1 and proposal.get('kind') == g.KIND, 'RESTORE_CATALOG_REVIEW')
        require(isinstance(proposal.get('files'), dict) and 1 <= len(proposal['files']) <= 64
                and all(c.ROLE.fullmatch(k) for k in proposal['files']) and isinstance(proposal.get('cold'), dict)
                and isinstance(proposal['cold'].get('stores'), dict) and 1 <= len(proposal['cold']['stores']) <= 32
                and all(c.ROLE.fullmatch(k) for k in proposal['cold']['stores']) and isinstance(proposal.get('images'), list)
                and 1 <= len(proposal['images']) <= 32 and all(g.IMAGE.fullmatch(x) for x in proposal['images'])
                and len(set(proposal['images'])) == len(proposal['images']), 'RESTORE_CATALOG_SOURCE_SET')
        expected = {'files'} | {'storage-'+x for x in proposal['cold']['stores']} | {'image-'+x[7:] for x in proposal['images']}
        require(set(value.get('artifacts', {})) == expected and set(self.p['receipt']['artifacts']) == expected | {'catalog'},
                'RESTORE_CATALOG_ARTIFACT_SET')
        for role, row in value['artifacts'].items():
            require(isinstance(row, dict) and set(row) == {'path', 'ciphertext_bytes', 'ciphertext_sha256', 'detail'}
                    and {k: row[k] for k in ('path', 'ciphertext_bytes', 'ciphertext_sha256')} == self.p['receipt']['artifacts'][role]
                    and isinstance(row['detail'], dict), 'RESTORE_CATALOG_RECEIPT_DISAGREES')
        # Only lexical source paths are consulted. No original source file,
        # service, unit, saved PID, intent or boot receipt is activated/read.
        paths = [row['path'] for row in proposal['files'].values()] + [row['source'] for row in proposal['cold']['stores'].values()]
        paths += [proposal['cold']['host']['docker_root']]
        require(all(isinstance(x, str) and Path(x).is_absolute() and '..' not in Path(x).parts for x in paths), 'RESTORE_SOURCE_PATH_INVALID')
        require(all(not g.overlapping(self.parent, Path(x)) for x in paths), 'RESTORE_ORIGINAL_SOURCE_OVERLAP')
        return value

    def tree(self, role, detail, roots, kind, ident):
        target = self.stage/role; target.mkdir(mode=0o700)
        count = detail.get('entries', detail.get('entry_count')); logical = detail.get('logical_bytes')
        require(type(count) is int and 0 < count <= self.p['max_entries']-self.entries
                and type(logical) is int and 0 <= logical <= self.p['max_bytes']-self.bytes
                and HASH.fullmatch(detail.get('manifest_sha256', '')), 'RESTORE_TREE_BUDGET_OR_MANIFEST')
        seen = {}; total = 0; manifest = None
        def consume(pipe):
            nonlocal total, manifest
            with tarfile.open(fileobj=pipe, mode='r|', tarinfo=image.BoundedInfo) as archive:
                for member in archive:
                    self.guard(); name = c.safe_name(member.name.rstrip('/') if member.isdir() else member.name)
                    require(manifest is None, 'RESTORE_MEMBER_AFTER_MANIFEST')
                    require((member.isfile() or member.isdir()) and not member.linkname and not member.issparse()
                            and (not member.pax_headers or set(member.pax_headers) <= {'path', 'mtime', 'size', 'uid', 'gid'}),
                            'RESTORE_MEMBER_TYPE_REFUSED')
                    if name == 'manifest.json':
                        require(member.isfile() and 0 < member.size <= c.MAX_MANIFEST, 'RESTORE_MANIFEST_INVALID')
                        raw = archive.extractfile(member).read(c.MAX_MANIFEST+1)
                        require(hashlib.sha256(raw).hexdigest() == detail['manifest_sha256'], 'RESTORE_MANIFEST_DIGEST')
                        manifest = image.json_value(raw); self.account(len(raw)); continue
                    parts = name.split('/')
                    require(len(parts) >= 2 and parts[0] == 'data' and parts[1] in roots and name not in seen
                            and len(seen) < count, 'RESTORE_MEMBER_PATH_OR_DUPLICATE')
                    require(type(member.size) is int and 0 <= member.size <= logical-total and type(member.mode) is int
                            and 0 <= member.mode <= 0o777 and member.uid == os.getuid() and member.gid == os.getgid()
                            and type(member.mtime) in (int, float) and 0 <= member.mtime < 2**40,
                            'RESTORE_FOREIGN_UID_OR_METADATA_UNSUPPORTED')
                    dest = target/name
                    if len(parts) == 2:
                        (target/'data').mkdir(mode=0o700, exist_ok=True)
                    else:
                        require('/'.join(parts[:-1]) in seen and seen['/'.join(parts[:-1])]['type'] == 'directory',
                                'RESTORE_PARENT_NOT_DECLARED')
                    require(not dest.exists() and not dest.is_symlink(), 'RESTORE_MEMBER_COLLISION')
                    row = {'path': name, 'type': 'directory' if member.isdir() else 'file', 'size': member.size,
                           'uid': member.uid, 'gid': member.gid, 'mode': member.mode}
                    if member.isdir():
                        require(member.size == 0, 'RESTORE_DIRECTORY_SIZE'); dest.mkdir(mode=0o700)
                    else:
                        total += member.size; self.account(member.size); digest = hashlib.sha256()
                        stream = archive.extractfile(member)
                        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, 'wb') as out:
                            left = member.size
                            while left:
                                self.guard(); data = stream.read(min(left, 65536)); require(data, 'RESTORE_MEMBER_TRUNCATED')
                                left -= len(data); digest.update(data); out.write(data)
                            out.flush(); os.fsync(out.fileno())
                        row['sha256'] = digest.hexdigest()
                    seen[name] = row
                    self.materialized[dest] = c.identity(dest.stat())
                while True:
                    data = archive.fileobj.read(65536)
                    if not data: break
                    require(not any(data), 'RESTORE_TRAILING_DATA')
        self.decrypt(role, consume, logical+count*8192+c.MAX_MANIFEST+65536)
        require(isinstance(manifest, dict) and manifest.get('schema') == 1 and manifest.get('kind') == kind
                and manifest.get('id') == ident and isinstance(manifest.get('entries'), list), 'RESTORE_MANIFEST_INVALID')
        rows = manifest['entries']; require(len(rows) == len(seen) == count and total == logical, 'RESTORE_SET_MISMATCH')
        names = set()
        for row in rows:
            require(isinstance(row, dict) and set(row) == {'path', 'type', 'size', 'uid', 'gid', 'mode', 'mtime_ns'} |
                    ({'sha256'} if row.get('type') == 'file' else set()), 'RESTORE_MANIFEST_FIELDS')
            require(all(type(row[k]) is int for k in ('size', 'uid', 'gid', 'mode', 'mtime_ns')),
                    'RESTORE_MANIFEST_NUMERIC_FIELDS')
            name = c.safe_name(row['path'])
            require(name not in names and name in seen and {k: v for k, v in row.items() if k != 'mtime_ns'} == seen[name],
                    'RESTORE_HASH_OR_METADATA_MISMATCH'); names.add(name)
            require(type(row['mtime_ns']) is int and 0 <= row['mtime_ns'] < 2**63, 'RESTORE_TIMESTAMP_INVALID')
            self.metadata.append((target/name, row))
        require({name.split('/')[1] for name in names} == set(roots), 'RESTORE_ROOT_SET_MISMATCH')
        for name, expected in roots.items():
            require('data/'+name in seen and seen['data/'+name]['type'] == expected, 'RESTORE_ROOT_TYPE_MISMATCH')
        self.entries += count

    def image(self, role, detail):
        ident = 'sha256:'+role[6:]
        require(detail.get('image_id') == ident and type(detail.get('export_bytes')) is int and detail['export_bytes'] > 0
                and HASH.fullmatch(detail.get('export_sha256', '')), 'RESTORE_IMAGE_DETAIL_INVALID')
        path = self.stage/'images'/ (ident[7:]+'.tar'); path.parent.mkdir(mode=0o700, exist_ok=True)
        digest = hashlib.sha256(); count = 0
        def consume(pipe):
            nonlocal count
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as out:
                while True:
                    data = pipe.read(65536)
                    if not data: break
                    count += len(data); self.account(len(data)); digest.update(data); out.write(data)
                out.flush(); os.fsync(out.fileno())
        self.decrypt(role, consume, min(detail['export_bytes'], self.p['max_bytes']-self.bytes))
        require(count == detail['export_bytes'] and digest.hexdigest() == detail['export_sha256'], 'RESTORE_IMAGE_EXPORT_HASH')
        result = image.verify(path, ident, max_bytes=self.p['max_bytes'], max_entries=self.p['max_entries'], guard=self.guard)
        require(isinstance(detail.get('metadata'), list) and len(detail['metadata']) == 3 and detail['metadata'][0] == ident
                and detail['metadata'][1] == result['architecture'], 'RESTORE_IMAGE_ARCHITECTURE_MISMATCH')
        self.image_files[path] = (c.signature(path.stat()), digest.hexdigest())
        return result

    def staged_set(self):
        expected = {self.stage/'restore.json'} | set(self.materialized) | set(self.image_files)
        for path in tuple(expected):
            expected.update(p for p in path.parents if p.is_relative_to(self.stage) and p != self.stage)
        actual = set()
        for folder, directories, files in os.walk(self.stage, followlinks=False):
            self.guard()
            for name in directories+files:
                path = Path(folder)/name; info = path.lstat()
                require((stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) and info.st_uid == os.getuid()
                        and (stat.S_ISDIR(info.st_mode) or info.st_nlink == 1), 'RESTORE_STAGED_TYPE_OR_OWNER_CHANGED')
                actual.add(path)
        require(actual == expected, 'RESTORE_STAGED_SET_CHANGED')
        for path, (signature, digest) in self.image_files.items():
            require(c.signature(a.private(path, False).stat()) == signature, 'RESTORE_STAGED_IMAGE_CHANGED')
            with path.open('rb') as stream:
                observed = hashlib.sha256()
                while True:
                    self.guard(); data = stream.read(65536)
                    if not data: break
                    observed.update(data)
            require(observed.hexdigest() == digest, 'RESTORE_STAGED_IMAGE_CHANGED')

    def save(self, phase, **extra):
        self.record.update(phase=phase, **extra)
        self.raw = c.atomic_json(self.stage, 'restore.json', self.record, self.raw)
        self.hook(phase)

    def run(self):
        require(type(self.tool) is a.Age, 'RESTORE_NATIVE_AGE_REQUIRED')
        self.until = time.monotonic()+self.p['seconds']; self.parent = Path(self.p['parent'])
        self.folder = Path(self.p['folder']); self.key = Path(self.p['key']); self.guard(); self.tool.check()
        fresh = plan(self.folder, self.p['receipt'], self.parent, self.key, seconds=self.p['seconds'],
                     max_bytes=self.p['max_bytes'], max_entries=self.p['max_entries'])
        require(fresh == self.p, 'RESTORE_REVIEW_CHANGED')
        lines = [x for x in self.key.read_text().splitlines() if x and not x.startswith('#')]
        require(len(lines) == 1 and re.fullmatch('AGE-SECRET-KEY-1[0-9A-Z]{58}', lines[0]), 'RESTORE_NATIVE_IDENTITY_REQUIRED')
        final = self.parent/('restored-'+self.p['id']); stage = self.parent/(final.name+'.partial')
        require(not final.exists() and not final.is_symlink() and not stage.exists() and not stage.is_symlink(), 'RESTORE_ALREADY_RECORDED')
        catalog = self.catalog(); self.guard()
        stage.mkdir(mode=0o700); self.stage = stage; self.stage_id = c.identity(stage.stat())
        self.record = {'schema': 1, 'kind': KIND, 'id': self.p['id'], 'review_id': self.p['review_id'],
                       'trusted_generation_receipt_sha256': c.fingerprint(self.p['receipt']), 'phase': 'prepared'}
        try:
            self.save('prepared'); artifacts = catalog['artifacts']; proposal = catalog['proposal']
            self.tree('files', artifacts['files']['detail'], {k: v['type'] for k, v in proposal['files'].items()}, g.KIND, self.p['id'])
            for role in sorted(proposal['cold']['stores']):
                detail = artifacts['storage-'+role]['detail']['receipt']
                require(detail.get('kind') == c.KIND and detail.get('encryption') == 'age_v1', 'RESTORE_STORAGE_RECEIPT_INVALID')
                self.tree('storage-'+role, detail, {role: 'directory'}, c.KIND, detail['id'])
            images = [self.image('image-'+ident[7:], artifacts['image-'+ident[7:]]['detail']) for ident in proposal['images']]
            self.save('content_verified', entries=self.entries, logical_and_archive_bytes=self.bytes)
            self.staged_set()
            # Only after every component's crypto EOF and complete-set checks.
            for path, row in sorted(self.metadata, key=lambda x: len(x[0].parts), reverse=True):
                self.guard()
                for parent in path.parents:
                    if parent == self.stage: break
                    require(parent.is_dir() and not parent.is_symlink(), 'RESTORE_STAGED_PARENT_CHANGED')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(fd)
                    require(c.identity(info) == self.materialized[path], 'RESTORE_STAGED_ENTRY_CHANGED')
                    if row['type'] == 'file':
                        require(info.st_size == row['size'] and info.st_nlink == 1, 'RESTORE_STAGED_ENTRY_CHANGED')
                        digest = hashlib.sha256()
                        while True:
                            self.guard(); data = os.read(fd, 65536)
                            if not data: break
                            digest.update(data)
                        require(digest.hexdigest() == row['sha256'], 'RESTORE_STAGED_ENTRY_CHANGED')
                    os.fchmod(fd, row['mode']); os.utime(fd, ns=(row['mtime_ns'], row['mtime_ns'])); os.fsync(fd)
                    observed = os.fstat(fd)
                    require(stat.S_IMODE(observed.st_mode) == row['mode'] and observed.st_mtime_ns == row['mtime_ns']
                            and observed.st_uid == row['uid'] and observed.st_gid == row['gid'], 'RESTORE_APPLIED_METADATA_MISMATCH')
                finally: os.close(fd)
            result = {'schema': 1, 'kind': KIND, 'id': self.p['id'], 'result': 'isolated_generation_restore_verified',
                      'crypto_eof_authenticated': True, 'file_set_hashes_permissions_mtime_uid_gid_verified': True,
                      'entries': self.entries, 'image_archives': images, 'filesystem_restore_performed': True,
                      'foreign_uid_materialization': False, 'image_import_restore_acceptance': False,
                      'application_restore_acceptance': False, 'backup_ready': False, 'production_restore_executed': False}
            c.atomic_json(stage, 'receipt.json', result); self.save('verified'); self.guard()
            require(not final.exists() and not final.is_symlink(), 'RESTORE_DESTINATION_EXISTS')
            os.rename(stage, final); self.stage = final
            fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
            return dict(result, directory=str(final))
        except BaseException as error:
            self.tool.stop(self.process); self.process = None
            # Failed plaintext is retained only under this fresh private stage.
            # Never publish it, delete the generation, repair a source, or start
            # an application. Failures of the audit itself cannot claim success.
            try: self.save('attention_required', code=getattr(error, 'code', 'RESTORE_OPERATION_FAILED'))
            except BaseException: pass
            raise
        finally: self.tool.stop(self.process)
