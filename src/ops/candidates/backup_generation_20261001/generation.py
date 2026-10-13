"""Inactive full-generation callback. No production CLI, discovery or replay.

Collect declared files, cold UID stores and immutable image exports under one
maintenance lease. Publish a receipt only after every artifact and same-ID
service return. The worker remains paused until the separate explicit return.
"""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import time
import uuid

from src.ops.candidates.backup_capture_20260930 import capture as c, age_tool as a
from src.ops.candidates.cold_storage_20260930 import storage as s

require = a.require
KIND = 'encrypted-backup-generation-v1'
IMAGE = re.compile('sha256:[0-9a-f]{64}')


def selected(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts and str(path) != '/', 'GENERATION_PATH_INVALID')
    for p in (*reversed(path.parents), path):
        require(not p.is_symlink(), 'GENERATION_SYMLINK_REFUSED')
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), 'GENERATION_SOURCE_TYPE_REFUSED')
    require(not info.st_mode & 0o7000 and (stat.S_ISDIR(info.st_mode) or info.st_nlink == 1),
            'GENERATION_SOURCE_MODE_OR_LINK_REFUSED')
    return path


def overlapping(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def plan(cold, files, images, *, max_file_bytes=2**30, max_image_bytes=2**34, max_entries=65536, source_dispositions=None):
    """Bind complete declared source roles, exclusions and immutable image IDs.

    Metadata only. An inventory provider must map the full installation before
    production execution; historical capacity is never used as that mapping.
    """
    value = s.validate(cold)
    require(isinstance(files, dict) and 1 <= len(files) <= 64, 'GENERATION_FILES_REQUIRED')
    require(isinstance(images, list) and 1 <= len(images) <= 32 and len(set(images)) == len(images)
            and all(isinstance(x, str) and IMAGE.fullmatch(x) for x in images), 'GENERATION_IMAGES_REQUIRED')
    require({r['image_id'] for r in value['services'].values()} <= set(images), 'GENERATION_SCOPED_IMAGE_MISSING')
    require(type(max_file_bytes) is int and 0 < max_file_bytes <= 2**40
            and type(max_image_bytes) is int and 0 < max_image_bytes <= 2**40
            and type(max_entries) is int and 1 <= max_entries <= c.MAX_ENTRIES, 'GENERATION_BUDGET_INVALID')
    rows = {}; paths = []
    dest = Path(value['destination']); docker = Path(value['host']['docker_root'])
    for role, item in sorted(files.items()):
        require(isinstance(role, str) and c.ROLE.fullmatch(role) and isinstance(item, dict)
                and set(item) == {'path', 'exclude_children'}, 'GENERATION_FILE_ROLE_INVALID')
        path = selected(item['path']); exclusions = item['exclude_children']
        require(isinstance(exclusions, list) and len(exclusions) == len(set(exclusions)) <= 32
                and all(isinstance(x, str) and c.safe_name(x) == x and '/' not in x for x in exclusions)
                and (path.is_dir() or not exclusions), 'GENERATION_EXCLUSIONS_INVALID')
        require(not overlapping(path, dest) and not overlapping(path, docker)
                and not any(overlapping(path, p) for p in paths), 'GENERATION_SOURCE_OVERLAP')
        for store in value['stores'].values():
            other = Path(store['source'])
            # Whole private state roots may deliberately omit their data child.
            disjoint = path.is_dir() and other.is_relative_to(path) and other != path and other.relative_to(path).parts[0] in exclusions
            require(not overlapping(path, other) or disjoint, 'GENERATION_COLD_SOURCE_OVERLAP')
        paths.append(path)
        rows[role] = {'path': str(path), 'identity': c.identity(path.stat()),
                      'type': 'directory' if path.is_dir() else 'file', 'exclude_children': sorted(exclusions),
                      'mount_topology_sha256': s.mount_topology(path),
                      'file_signature': None if path.is_dir() else c.signature(path.stat())}
    result = {'schema': 1, 'kind': KIND, 'id': str(uuid.uuid4()), 'cold': deepcopy(cold), 'files': rows,
              'images': sorted(images), 'max_file_bytes': max_file_bytes, 'max_image_bytes': max_image_bytes,
              'max_entries': max_entries, 'source_dispositions': deepcopy(source_dispositions or {})}
    require(isinstance(result['source_dispositions'],dict) and len(result['source_dispositions'])<=256,
            'GENERATION_DISCOVERY_INVALID')
    return dict(result, review_id=c.fingerprint(result))


def discovered_plan(cold, stores, required_files, image_rows, **budgets):
    """Map fresh capacity-inventory metadata into an exact complete generation.

    No discovery command or size scan here. Reject missing sources; coalesce
    files already included in a declared tree and split runtime subdirectories
    from their host-owned control roots. Export each unique discovered image.
    """
    s.validate(cold); files = {}; covered = {}; names = set()
    require(isinstance(stores,list) and isinstance(required_files,list) and isinstance(image_rows,dict), 'GENERATION_DISCOVERY_INVALID')
    for row in stores:
        name = row['name']; require(c.ROLE.fullmatch(name) and name not in names, 'GENERATION_DISCOVERY_ROLE_INVALID'); names.add(name)
        path = selected(row['path']); exclusions = list(row.get('exclude_children', []))
        exact = [role for role,store in cold['stores'].items() if store['source']==str(path)]
        if exact:
            require(len(exact)==1, 'GENERATION_DISCOVERY_STORE_AMBIGUOUS'); covered[name] = 'storage-'+exact[0]; continue
        runtime = row.get('runtime_subdirectory')
        if runtime:
            runtime_path = Path(runtime['path'])
            matches = [role for role,store in cold['stores'].items() if store['source']==str(runtime_path)
                       and store['service']==runtime['service'] and store['target']==runtime['target']]
            require(len(matches)==1 and runtime_path.parent==path, 'GENERATION_RUNTIME_STORE_MISSING')
            exclusions.append(runtime_path.name); covered[name+':runtime'] = 'storage-'+matches[0]
        files[name] = {'path':str(path),'exclude_children':sorted(set(exclusions))}; covered[name] = 'files:'+name
    require({'storage-'+role for role in cold['stores']} <= set(covered.values()), 'GENERATION_DISCOVERY_STORE_MISSING')
    for row in required_files:
        name = row['name']; require(name not in names and isinstance(name,str), 'GENERATION_DISCOVERY_ROLE_INVALID'); names.add(name)
        require(row.get('status')=='ok', 'GENERATION_DISCOVERY_FILE_UNAVAILABLE')
        path = selected(row['path']); containing = []
        for role,item in files.items():
            parent = Path(item['path'])
            if parent==path or parent.is_dir() and path.is_relative_to(parent) and path.relative_to(parent).parts[0] not in item['exclude_children']:
                containing.append(role)
        if containing:
            require(len(containing)==1, 'GENERATION_DISCOVERY_FILE_AMBIGUOUS'); covered[name] = 'files:'+containing[0]
        else:
            require(c.ROLE.fullmatch(name), 'GENERATION_DISCOVERY_ROLE_INVALID')
            files[name] = {'path':str(path),'exclude_children':[]}; covered[name] = 'files:'+name
    require(image_rows and all(isinstance(r,dict) and IMAGE.fullmatch(r.get('image_id','')) for r in image_rows.values()),
            'GENERATION_DISCOVERY_IMAGE_UNAVAILABLE')
    images = sorted({r['image_id'] for r in image_rows.values()})
    covered.update({'image:'+role:row['image_id'] for role,row in image_rows.items()})
    return plan(cold, files, images, source_dispositions=covered, **budgets)


def validate(proposal):
    value = deepcopy(proposal); digest = value.pop('review_id', None)
    require(value.get('schema') == 1 and value.get('kind') == KIND and c.fingerprint(value) == digest
            and str(uuid.UUID(value['id'])) == value['id'], 'GENERATION_REVIEW_CHANGED')
    fresh = plan(value['cold'], {k: {x: r[x] for x in ('path', 'exclude_children')} for k, r in value['files'].items()},
                 value['images'], max_file_bytes=value['max_file_bytes'], max_image_bytes=value['max_image_bytes'],
                 max_entries=value['max_entries'], source_dispositions=value['source_dispositions'])
    fresh['id'] = value['id']; fresh.pop('review_id')
    require(fresh == value, 'GENERATION_PREPARED_METADATA_CHANGED')
    return value


class Selection:
    """Descriptor-based declared-file traversal; exclusions are reviewed names."""
    def __init__(self, proposal, guard):
        self.p, self.guard, self.fds = proposal, guard, {}

    def __enter__(self):
        try:
            for role, row in self.p['files'].items():
                fd = os.open(selected(row['path']), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                self.fds[role] = fd
                require(c.identity(os.fstat(fd)) == row['identity'], 'GENERATION_SOURCE_CHANGED')
            return self
        except BaseException:
            self.__exit__(); raise

    def __exit__(self, *args):
        for fd in self.fds.values(): os.close(fd)
        self.fds.clear()

    def scan(self):
        rows = []; total = 0
        def walk(role, fd, relative):
            nonlocal total
            self.guard(); info = os.fstat(fd); root = self.p['files'][role]
            require(info.st_dev == root['identity'][0] and not info.st_mode & 0o7000,
                    'GENERATION_MOUNT_OR_MODE_REFUSED')
            require(not os.listxattr(fd), 'GENERATION_XATTR_UNSUPPORTED')
            directory = stat.S_ISDIR(info.st_mode)
            require(directory or stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'GENERATION_ENTRY_TYPE_OR_LINK_REFUSED')
            rows.append(c.Set.row('data/' + role + ('/' + relative if relative else ''), info, directory))
            total += 0 if directory else info.st_size
            require(len(rows) <= self.p['max_entries'] and total <= self.p['max_file_bytes'], 'GENERATION_FILE_BUDGET_EXCEEDED')
            if directory:
                for name in sorted(os.listdir(fd)):
                    c.safe_name(name)
                    if not relative and name in root['exclude_children']: continue
                    before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    require(stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode), 'GENERATION_ENTRY_TYPE_OR_LINK_REFUSED')
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        require(c.signature(os.fstat(child)) == c.signature(before), 'GENERATION_SOURCE_CHANGED')
                        walk(role, child, relative + '/' + name if relative else name)
                    finally: os.close(child)
        for role, fd in self.fds.items(): walk(role, fd, '')
        return rows

    def open(self, row):
        parts = row['path'].split('/'); fd = os.dup(self.fds[parts[1]])
        try:
            for part in parts[2:]:
                child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                os.close(fd); fd = child
            require(c.signature(os.fstat(fd)) == row['signature'] and stat.S_ISREG(os.fstat(fd).st_mode), 'GENERATION_SOURCE_CHANGED')
            stream = os.fdopen(fd, 'rb', buffering=0); fd = None; return stream
        finally:
            if fd is not None: os.close(fd)


class ImageExporter:
    """Exact-ID Docker image save stream. No tags, load, pull, build or output file."""
    FORMAT = '[{{json .Id}},{{json .Architecture}},{{json .Size}}]'
    def __init__(self, backend): self.backend = backend
    def inspect(self, ident):
        require(IMAGE.fullmatch(ident), 'GENERATION_IMAGE_ID_INVALID')
        self.backend.verify_host()
        value = self.backend.query(['image', 'inspect', '--format', self.FORMAT, ident])
        require(isinstance(value, list) and len(value) == 3 and value[0] == ident
                and isinstance(value[1], str) and value[1] and type(value[2]) is int and value[2] >= 0,
                'GENERATION_IMAGE_METADATA_CHANGED')
        return value
    def start(self, ident, scope, guard):
        guard(); require(scope.active and type(scope.descriptor) is int, 'GENERATION_LIVE_SCOPE_REQUIRED')
        return subprocess.Popen(self.backend.args(['image', 'save', ident]), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(scope.descriptor,),
            env={'PATH': os.defpath, 'LC_ALL': 'C', 'HOME': '/nonexistent'})


class Generation:
    def __init__(self, proposal, backend, tool, exporter=None, *, test_codec=False, hook=lambda phase: None):
        self.p = validate(proposal); self.original = deepcopy(proposal); self.backend = backend; self.tool = tool
        self.exporter = exporter or ImageExporter(backend); self.test_codec = test_codec; self.hook = hook
        self.folder = None; self.raw = None; self.record = None; self.artifacts = {}; self.op = None; self.scope = None
        self.image_bytes = 0; self.image_metadata = {}

    def inventory(self):
        """Maintenance provider includes only this invocation's registered helpers."""
        op = self.op or s.Operation(self.p['cold'], self.backend, self.tool, self.p['cold']['recipients'], test_codec=self.test_codec)
        return op.inventory()

    def save(self, phase, **extra):
        self.record.update(phase=phase, **extra)
        self.raw = c.atomic_json(self.folder, 'generation.json', self.record, self.raw)
        self.hook('generation:' + phase)

    def guard(self, quiet=True):
        require(c.fingerprint(self.p) == self.original['review_id'], 'GENERATION_REVIEW_CHANGED')
        self.scope.checkpoint(); os.fstat(self.scope.descriptor)
        require(self.scope.active and time.monotonic() < self.until, 'GENERATION_DEADLINE_OR_PAUSE')
        self.op.guard(True)
        if quiet: self.op.quiet()
        require(c.identity(a.private(self.folder).stat()) == self.folder_id
                and a.private(self.folder/'generation.json', False).read_bytes() == self.raw, 'GENERATION_JOURNAL_CHANGED')
        for row in self.p['files'].values():
            require(c.identity(selected(row['path']).stat()) == row['identity']
                    and s.mount_topology(Path(row['path'])) == row['mount_topology_sha256']
                    and (row['file_signature'] is None or c.signature(Path(row['path']).stat()) == row['file_signature']),
                    'GENERATION_SOURCE_CHANGED')

    def encrypt(self, role, producer):
        self.guard(); require(role not in self.artifacts, 'GENERATION_ARTIFACT_REUSED')
        part = self.folder/(role+'.age.partial'); cipher = self.folder/(role+'.age'); process = None
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            process = self.tool.spawn(['-e', *[x for r in self.p['cold']['recipients'] for x in ('-r', r)]],
                output_fd=fd, lease=(self.scope.descriptor,), guard=self.guard)
            pipe = a.Pipe(process.stdin.fileno(), 'write', self.until, self.guard)
            detail = producer(pipe)
            process.stdin.close(); self.tool.finish(process, self.until); self.tool.stop(process); process = None
            os.fsync(fd); self.guard(); require(not cipher.exists() and not cipher.is_symlink(), 'GENERATION_ARTIFACT_REUSED')
            os.rename(part, cipher)
            self.artifacts[role] = {'path': cipher.name, 'ciphertext_sha256': a.sha(cipher),
                                    'ciphertext_bytes': cipher.stat().st_size, 'detail': detail}
            self.save('collecting', artifacts=deepcopy(self.artifacts)); return self.artifacts[role]
        finally:
            self.tool.stop(process); os.close(fd)

    def files(self):
        def produce(pipe):
            with Selection(self.p, self.guard) as dataset:
                rows = dataset.scan(); before = deepcopy(rows)
                with tarfile.open(fileobj=pipe, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                    for row in rows:
                        self.guard(); member = tarfile.TarInfo(row['path']); member.uid = row['uid']; member.gid = row['gid']
                        member.mode = row['mode']; member.mtime = row['mtime_ns']//10**9; member.size = row['size']
                        if row['type'] == 'directory': member.type = tarfile.DIRTYPE; archive.addfile(member)
                        else:
                            with dataset.open(row) as stream:
                                hashed = c.Hashed(stream, self.guard); archive.addfile(member, hashed)
                                require(hashed.bytes == row['size'] and c.signature(os.fstat(stream.fileno())) == row['signature'], 'GENERATION_SOURCE_CHANGED')
                                row['sha256'] = hashed.hash.hexdigest()
                    require(dataset.scan() == before, 'GENERATION_SOURCE_CHANGED')
                    manifest = {'schema': 1, 'kind': KIND, 'id': self.p['id'], 'entries': [{k:v for k,v in row.items() if k != 'signature'} for row in rows]}
                    data = json.dumps(manifest, sort_keys=True).encode(); require(len(data) <= c.MAX_MANIFEST, 'GENERATION_MANIFEST_LIMIT')
                    member = tarfile.TarInfo('manifest.json'); member.size = len(data); member.mode = 0o600
                    archive.addfile(member, io.BytesIO(data))
                require(dataset.scan() == before, 'GENERATION_SOURCE_CHANGED')
            return {'manifest_sha256': hashlib.sha256(data).hexdigest(), 'entries': len(rows), 'logical_bytes': sum(r['size'] for r in rows)}
        self.encrypt('files', produce)

    def image(self, ident):
        role = 'image-' + ident[7:]; self.guard(); before = self.exporter.inspect(ident)
        require(before == self.image_metadata[ident], 'GENERATION_IMAGE_METADATA_CHANGED')
        def produce(pipe):
            process = None; digest = hashlib.sha256(); count = 0
            try:
                process = self.exporter.start(ident, self.scope, self.guard)
                reader = a.Pipe(process.stdout.fileno(), 'read', self.until, self.guard,
                                read_limit=self.p['max_image_bytes']-self.image_bytes)
                while True:
                    data = reader.read(65536)
                    if not data: break
                    count += len(data); digest.update(data); pipe.write(data)
                a.Age.finish(process, self.until)
                require(count > 0 and self.exporter.inspect(ident) == before, 'GENERATION_IMAGE_EXPORT_INVALID')
                self.image_bytes += count
                return {'image_id': ident, 'metadata': before, 'export_bytes': count, 'export_sha256': digest.hexdigest(), 'archive_reimport_verified': False}
            finally: a.Age.stop(process)
        self.encrypt(role, produce)

    def collect(self):
        self.guard(); self.files()
        for ident in self.p['images']: self.image(ident)
        expected = {'files'} | {'storage-' + role for role in self.p['cold']['stores']} | {'image-' + ident[7:] for ident in self.p['images']}
        require(set(self.artifacts) == expected, 'GENERATION_ARTIFACT_SET_INCOMPLETE')
        catalog = {'schema': 1, 'kind': KIND, 'id': self.p['id'], 'proposal': self.original,
                   'artifacts': deepcopy(self.artifacts), 'application_consistency': 'unverified',
                   'restore_verified': False, 'image_archive_reimport_verified': False}
        data = json.dumps(catalog, sort_keys=True).encode(); require(len(data) <= c.MAX_MANIFEST, 'GENERATION_MANIFEST_LIMIT')
        self.encrypt('catalog', lambda pipe: (pipe.write(data), {'sha256': hashlib.sha256(data).hexdigest()})[1])
        self.save('sealed', artifacts=deepcopy(self.artifacts))

    def __call__(self, scope):
        require(type(self.tool) is a.Age or self.test_codec is True, 'GENERATION_NATIVE_AGE_REQUIRED')
        require(getattr(scope, 'active', False) is True and type(scope.descriptor) is int, 'GENERATION_LIVE_SCOPE_REQUIRED')
        validate(self.original); self.scope = scope; self.until = time.monotonic() + self.p['cold']['seconds']
        self.backend.until = self.until
        scope.checkpoint()
        if type(self.tool) is a.Age: self.tool.check()
        self.image_metadata = {ident:self.exporter.inspect(ident) for ident in self.p['images']}
        self.folder = Path(self.p['cold']['destination'])/self.p['id']
        require(not self.folder.exists() and not self.folder.is_symlink(), 'GENERATION_ALREADY_RECORDED')
        self.folder.mkdir(mode=0o700); self.folder_id = c.identity(self.folder.stat())
        self.record = {'schema': 1, 'kind': KIND, 'id': self.p['id'], 'review_id': self.original['review_id'], 'phase': 'prepared'}
        self.raw = c.atomic_json(self.folder, 'generation.json', self.record)
        stores = self.folder/'storage'; stores.mkdir(mode=0o700)
        cold = self.p['cold']
        derived = s.plan(cold['host'], cold['services'], cold['protected'],
            {k:{x:r[x] for x in ('source','target','service')} for k,r in cold['stores'].items()},
            cold['helper'], cold['dependencies'], stores, public_recipients=cold['recipients'],
            seconds=cold['seconds'], stop_seconds=cold['stop_seconds'], max_bytes=cold['max_bytes'], max_entries=cold['max_entries'])
        derived['id'] = cold['id']; derived.pop('review_id'); derived['review_id'] = c.fingerprint(derived)
        outer = self
        class Cold(s.Operation):
            def guard(self, force=False):
                require(time.monotonic() < outer.until, 'GENERATION_DEADLINE_OR_PAUSE')
                self.backend.until = outer.until
                return super().guard(force)
            def seal(self, role):
                result = super().seal(role)
                receipt = result['receipt']; path = Path(result['job_directory'])/'payload.age'
                require(a.sha(path) == receipt['ciphertext_sha256'] and path.stat().st_size == receipt['ciphertext_bytes'], 'GENERATION_STORAGE_RECEIPT_CHANGED')
                outer.artifacts['storage-'+role] = {'path': str(path.relative_to(outer.folder)),
                    'ciphertext_sha256': receipt['ciphertext_sha256'], 'ciphertext_bytes': receipt['ciphertext_bytes'],
                    'detail': {'receipt': deepcopy(receipt)}}
                # The deterministic derived destination is inside this generation.
                outer.save('collecting', artifacts=deepcopy(outer.artifacts))
                if role == list(self.p['stores'])[-1]: outer.collect()
                return result
        self.op = Cold(derived, self.backend, self.tool, self.p['cold']['recipients'], test_codec=self.test_codec)
        try:
            result = self.op(scope)
            self.guard(False)
            require(result == {'result': 'passed', 'child_processes_stopped': True}
                    and self.op.record['phase'] == 'services_returned' and not self.op.helpers, 'GENERATION_SERVICE_RETURN_UNCONFIRMED')
            self.hook('before_commit'); self.guard(False)
            receipt = {'schema': 1, 'kind': KIND, 'id': self.p['id'], 'review_id': self.original['review_id'],
                'artifacts': {k:{x:v[x] for x in ('path','ciphertext_sha256','ciphertext_bytes')} for k,v in sorted(self.artifacts.items())},
                'encryption': 'age_v1' if type(self.tool) is a.Age else 'synthetic_identity_codec',
                'services_returned': True, 'worker_return_required': True, 'restore_verified': False, 'backup_ready': False}
            c.atomic_json(self.folder, 'receipt.json', receipt)
            self.save('captured', receipt_sha256=c.fingerprint(receipt), child_processes_stopped=True)
            return result
        except BaseException as error:
            try: self.save('attention_required', code=getattr(error, 'code', 'GENERATION_OPERATION_FAILED'), explicit_recovery_required=True)
            except BaseException: pass
            if isinstance(error, BrokenPipeError): raise a.Failure('GENERATION_PIPE_FAILED') from None
            raise


def inspect(folder, trusted_receipt=None):
    """Read-only integrity; saved records never authorize capture or service replay."""
    folder = a.private(Path(folder)); record = json.loads(a.private(folder/'generation.json', False).read_text())
    require(record.get('schema') == 1 and record.get('kind') == KIND, 'GENERATION_RECORD_INVALID')
    if record['phase'] != 'captured':
        return {'result': 'attention_required', 'phase': record['phase'], 'replay_supported': False, 'backup_ready': False}
    receipt = json.loads(a.private(folder/'receipt.json', False).read_text())
    require(trusted_receipt is not None and receipt == trusted_receipt and c.fingerprint(receipt) == record['receipt_sha256'], 'GENERATION_TRUSTED_RECEIPT_REQUIRED')
    require(receipt['id'] == record['id'] and receipt['kind'] == KIND and receipt['review_id'] == record['review_id'], 'GENERATION_RECEIPT_CHANGED')
    require(set(receipt['artifacts']) == set(record['artifacts']), 'GENERATION_ARTIFACT_SET_INCOMPLETE')
    for role, row in receipt['artifacts'].items():
        path = c.safe_name(row['path'])
        target = folder/path
        require(a.private(target, False).stat().st_size == row['ciphertext_bytes'] and a.sha(target) == row['ciphertext_sha256'], 'GENERATION_CIPHERTEXT_CHANGED')
        require({k:record['artifacts'][role][k] for k in row} == row, 'GENERATION_RECEIPT_CHANGED')
    return {'result': 'captured_integrity_verified', 'artifacts': len(receipt['artifacts']),
            'replay_supported': False, 'restore_verified': False, 'backup_ready': False}
