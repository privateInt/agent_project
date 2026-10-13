"""Inactive stopped code/policy transaction. No production CLI is exposed.

The backend is the existing owned service adapter. Commit and recovery always
leave desired=paused and the enable link absent; neither starts any service.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

from src.ops.platform.contracts import require
from src.ops.platform.ops import manage, worker_state as ws, refresh_worker as worker
from src.ops.platform.ops import service_lifecycle as service, set_public_url as change

BASE = 'a24257d8636b5842167e74f7b02931c467d105519fb9b5f70ceb577437fccca4'
RUNTIME = ('src/ops/platform', 'src/infra/observability/ops', 'src/infra/langfuse/ops')
ORDER = ('worker_state.py', 'boot_lifecycle.py', 'refresh_worker.py', 'service_lifecycle.py', 'service_entry.py')
TARGETS = tuple('src/ops/platform/ops/' + name for name in ORDER)
KIND = 'stopped-code-policy-transition'
PENDING = 'CODE_TRANSITION_PENDING'


def sha(raw): return hashlib.sha256(raw).hexdigest()
def encode(value): return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
def fingerprint(value): return change.digest(value)


def read(path, missing=False):
    path = manage.safe(path)
    if missing and not path.exists(): return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and info.st_size <= 2 * 1024 * 1024, 'TRANSITION_FILE_UNSAFE')
        with os.fdopen(os.dup(fd), 'rb') as stream: raw = stream.read(2 * 1024 * 1024 + 1)
        require(len(raw) <= 2 * 1024 * 1024, 'TRANSITION_FILE_LIMIT')
        return {'raw': raw, 'sha256': sha(raw), 'mode': stat.S_IMODE(info.st_mode)}
    finally: os.close(fd)


def metadata(value):
    return None if value is None else {key: value[key] for key in ('sha256', 'mode')}


def replace(path, raw, mode):
    """Owner-preserving durable replace; callers check the exact before/after."""
    path = manage.safe(path)
    fd, temporary = tempfile.mkstemp(prefix='.code-transition-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path); service.fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def runtime_paths(root):
    return sorted(str(p.relative_to(root)) for folder in RUNTIME
                  for p in (root / folder).rglob('*.py') if 'tests' not in p.relative_to(root).parts)


def code_hash(files):
    h = hashlib.sha256()
    for path, raw in sorted(files.items()): h.update(path.encode() + b'\0' + hashlib.sha256(raw).digest())
    return h.hexdigest()


class Bundle:
    """Fixed reviewed code set; no arbitrary source or credential paths."""
    def __init__(self, folder):
        self.folder = manage.safe(folder)
        self.spec = json.loads(read(self.folder / 'manifest.json')['raw'])
        s = self.spec
        require(s.get('schema') == 1 and s.get('activation_supported') is False
                and s.get('base_code_sha256') == BASE, 'TRANSITION_BUNDLE_INVALID')
        base = s['base_files']; changes = s['changes']
        require(len(base) == 60 and len({r['path'] for r in base}) == 60, 'TRANSITION_BUNDLE_INVALID')
        require(len(changes) == len(TARGETS) and {r['path'] for r in changes} == set(TARGETS), 'TRANSITION_SCOPE_INVALID')
        for row in base:
            p = Path(row['path'])
            require(not p.is_absolute() and '..' not in p.parts and p.suffix == '.py' and 'tests' not in p.parts
                    and any(p.is_relative_to(folder) for folder in RUNTIME), 'TRANSITION_SCOPE_INVALID')
        self.payload = {}
        for row in changes:
            raw = read(self.folder / 'source' / row['path'])['raw']
            require(sha(raw) == row['after_sha256'], 'TRANSITION_PAYLOAD_CHANGED')
            self.payload[row['path']] = raw
        self.base = {r['path']: r['sha256'] for r in base}
        require('src/ops/platform/ops/boot_lifecycle.py' not in self.base
                and set(TARGETS) - {'src/ops/platform/ops/boot_lifecycle.py'} <= set(self.base), 'TRANSITION_SCOPE_INVALID')
        require(all(r['before_sha256'] == self.base.get(r['path']) for r in changes), 'TRANSITION_BEFORE_INVALID')
        self.identity = fingerprint(s)

    def check_tree(self, root, mode='base'):
        paths = set(runtime_paths(root)); expected = set(self.base) | set(self.payload)
        require(paths <= expected and set(self.base) <= paths, 'TRANSITION_CODE_SET_CHANGED')
        files = {}
        for path in sorted(expected):
            current = read(root / path, missing=True)
            actual = None if current is None else current['sha256']
            before = self.base.get(path)
            after = sha(self.payload[path]) if path in self.payload else before
            allowed = {before} if mode == 'base' else {after} if mode == 'candidate' else {before, after}
            require(actual in allowed, 'TRANSITION_CODE_CHANGED')
            if current is not None: files[path] = current['raw']
        actual = code_hash(files)
        if mode != 'mixed': require(actual == self.spec['base_code_sha256' if mode == 'base' else 'candidate_code_sha256'], 'TRANSITION_HASH_MISMATCH')
        if mode == 'base':
            require(code_hash(dict(files, **self.payload)) == self.spec['candidate_code_sha256'], 'TRANSITION_PAYLOAD_HASH_MISMATCH')
        return actual


class Transition:
    def __init__(self, directory, bundle, backend, *, snapshot=worker.runtime_snapshot,
                 lineage=worker.refresh.lineage, hook=lambda stage: None):
        self.directory = manage.private(directory)
        self.bundle, self.backend = bundle, backend
        self.snapshot, self.lineage, self.hook = snapshot, lineage, hook
        self.store = ws.Store(self.directory)
        self.root = manage.safe(ws.read(self.directory / 'owner.json')['project_root'])
        self.path = self.store.path.parent / 'code-transition.json'

    def history(self):
        value = self.lineage(self.directory)
        require(not value['pending'], 'REFRESH_PENDING_USE_CHECK_OR_ROLLBACK')
        # records contain local Path objects; evidence already pins their bytes.
        return fingerprint({k: v for k, v in value.items() if k != 'records'})

    def no_intent(self):
        intent = self.store.optional('intent.json')
        require(not intent or intent.get('phase') == 'completed', 'WORKER_UNFINISHED_INTENT')

    def unchanged_identity(self, binding):
        current = self.snapshot(self.directory)
        require({k: v for k, v in current.items() if k != 'code_sha256'} ==
                {k: v for k, v in binding.items() if k != 'code_sha256'}, 'TRANSITION_RUNTIME_IDENTITY_CHANGED')
        require(current['code_sha256'] == worker.code_hash(self.root), 'TRANSITION_RUNTIME_CODE_MISMATCH')
        return current

    def plan(self, persistence='boot', startup_seconds=300):
        require(persistence in ('boot', 'session') and type(startup_seconds) is int
                and 1 <= startup_seconds <= 300, 'TRANSITION_POLICY_INVALID')
        require(not self.path.exists() and not self.path.is_symlink(), 'TRANSITION_ALREADY_RECORDED')
        with change.change_lock(self.directory):
            before = service.load(self.directory)
            require(before['phase'] == 'installed' and before['persistence'] == 'session', 'TRANSITION_BASE_POLICY_REQUIRED')
            require(before['binding'] == self.store.manifest()['binding'] == self.snapshot(self.directory), 'SERVICE_BINDING_CHANGED')
            require(before['binding']['code_sha256'] == BASE and self.root == worker.ROOT, 'TRANSITION_BASE_CHANGED')
            self.bundle.check_tree(self.root)
            control = self.store.control(); require(control['recovery'] is None, 'WORKER_RECOVERY_REQUIRED')
            self.no_intent(); provenance = self.history()
            service.owned(before, self.backend); service.enabled(before)
            target = deepcopy(before)
            target['binding']['code_sha256'] = self.bundle.spec['candidate_code_sha256']
            target['persistence'] = persistence
            if persistence == 'boot':
                target['boot_contract'] = {'schema': 1, 'generation': str(uuid.uuid4()), 'startup_seconds': startup_seconds}
            require(service.render(target) == service.render(before), 'TRANSITION_UNIT_CHANGE_UNSUPPORTED')
            return {'schema': 1, 'kind': KIND, 'directory': str(self.directory), 'bundle_sha256': self.bundle.identity,
                    'id': str(uuid.uuid4()), 'before': before, 'after': target,
                    'control_before_stop': control, 'provenance': provenance,
                    'requested_persistence': persistence, 'final_desired': 'paused',
                    'activation_supported': False, 'boot_ready': False}

    def load(self):
        value = ws.read(self.path)
        require(value.get('schema') == 1 and value.get('kind') == KIND and value.get('directory') == str(self.directory)
                and value.get('bundle_sha256') == self.bundle.identity, 'TRANSITION_RECORD_INVALID')
        require(str(uuid.UUID(value['id'])) == value['id'], 'TRANSITION_RECORD_INVALID')
        before, after = value['before'], value['after']
        require(before['binding']['owner'] == ws.read(self.directory / 'owner.json')
                and before['binding']['directory'] == str(self.directory)
                and before['binding']['code_sha256'] == BASE
                and before['persistence'] == 'session', 'TRANSITION_RECORD_INVALID')
        expected = deepcopy(before); expected['binding']['code_sha256'] = self.bundle.spec['candidate_code_sha256']
        expected['persistence'] = value['requested_persistence']
        require(expected['persistence'] in ('boot', 'session'), 'TRANSITION_RECORD_INVALID')
        if expected['persistence'] == 'boot':
            contract = after['boot_contract']
            require(set(contract) == {'schema', 'generation', 'startup_seconds'} and contract['schema'] == 1
                    and str(uuid.UUID(contract['generation'])) == contract['generation']
                    and type(contract['startup_seconds']) is int and 1 <= contract['startup_seconds'] <= 300,
                    'TRANSITION_RECORD_INVALID')
            expected['boot_contract'] = contract
        require(after == expected and service.render(before) == service.render(after), 'TRANSITION_RECORD_INVALID')
        return value

    def save(self, record, phase):
        record['phase'] = phase; record['updated_at'] = worker.activation.rc.now(); ws.write(self.path, record)
        self.hook('journal:' + phase)

    @contextmanager
    def locks(self):
        with ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
            with ws.automatic_lease(self.directory):
                with change.change_lock(self.directory): yield

    def exit_proof(self, record):
        # Stale saved PIDs are not control targets. Observe the current owned unit.
        actual = self.backend.inspect()
        require(service.inactive(actual) and self.backend.cgroup_empty(actual), 'SERVICE_STILL_RUNNING')
        require(not actual.get('DropInPaths') and actual.get('FragmentPath') == record['before']['unit_path']
                and actual.get('NeedDaemonReload', 'no') == 'no' and actual.get('UnitFileState') == 'disabled', 'TRANSITION_UNIT_CHANGED')
        require(service.unit_bytes(Path(record['before']['unit_path'])) == service.render(record['before'])
                and not service.enabled(record['before']), 'TRANSITION_UNIT_CHANGED')
        require(self.store.control()['desired'] == 'paused', 'WORKER_PAUSE_REQUIRED')
        self.no_intent()
        require(self.history() == record['provenance'], 'TRANSITION_PROVENANCE_CHANGED')
        self.unchanged_identity(record['before']['binding'])

    def apply(self, proposal, *, stop=None):
        require(not self.path.exists() and not self.path.is_symlink(), 'TRANSITION_ALREADY_RECORDED')
        require(str(uuid.UUID(proposal['id'])) == proposal['id'], 'TRANSITION_PROPOSAL_CHANGED')
        # Rebuild the read-only proposal immediately before recording any intent.
        fresh = self.plan(proposal['requested_persistence'], proposal['after'].get('boot_contract', {}).get('startup_seconds', 300))
        for key in ('before', 'control_before_stop', 'provenance', 'bundle_sha256'):
            require(fresh[key] == proposal[key], 'TRANSITION_PROPOSAL_STALE')
        expected = deepcopy(fresh['after'])
        if 'boot_contract' in expected:
            require(str(uuid.UUID(proposal['after']['boot_contract']['generation'])) == proposal['after']['boot_contract']['generation'], 'TRANSITION_POLICY_INVALID')
            expected['boot_contract']['generation'] = proposal['after']['boot_contract']['generation']
        require(expected == proposal['after'] and proposal['directory'] == str(self.directory), 'TRANSITION_PROPOSAL_CHANGED')
        record = deepcopy(proposal)
        with ws.file_lock(self.directory / 'service-management.lock', 'SERVICE_MANAGEMENT_BUSY'):
            require(not self.path.exists(), 'TRANSITION_ALREADY_RECORDED')
            self.save(record, 'stop_requested')
        # Existing code persists pause before contacting the manager. Never
        # repeat an uncertain stop automatically: explicit finish proves exit.
        result = (stop or (lambda: service.run('stop', self.directory, backend=self.backend)))()
        require(result.get('result') == 'passed' and result.get('worker_exit_verified') is True, 'TRANSITION_STOP_UNCONFIRMED')
        self.hook('stop_returned')
        return self.finish()

    def targets(self, record):
        result = {path: self.root / path for path in TARGETS}
        result['service'] = service.manifest_path(self.directory)
        result['worker'] = self.store.path / 'owner.json'
        return result

    def backup_dir(self, record):
        return manage.safe(self.store.path.parent / 'code-transitions' / record['id'])

    def capture(self, record):
        self.bundle.check_tree(self.root)
        require(service.load(self.directory) == record['before']
                and self.store.manifest()['binding'] == record['before']['binding'], 'SERVICE_BINDING_CHANGED')
        control = self.store.control(); require(control['recovery'] is None, 'WORKER_RECOVERY_REQUIRED')
        require(control.get('service_stop_token'), 'TRANSITION_STOP_TOKEN_REQUIRED')
        record['control_stopped'] = control
        folder = self.backup_dir(record)
        for directory in (folder.parent, folder):
            directory.mkdir(mode=0o700, exist_ok=True); manage.private(directory)
        rows = []
        for index, (key, path) in enumerate(self.targets(record).items()):
            before = read(path, missing=True)
            raw = (self.bundle.payload[key] if key in self.bundle.payload else encode(record['after']) if key == 'service'
                   else encode(dict(self.store.manifest(), binding=record['after']['binding'])))
            mode = before['mode'] if before is not None else 0o600
            row = {'key': key, 'before': metadata(before), 'after': {'sha256': sha(raw), 'mode': mode}, 'index': index}
            for suffix, value in (('before', before['raw'] if before is not None else None), ('after', raw)):
                if value is None: continue
                target = folder / (str(index) + '.' + suffix)
                if target.exists(): require(read(target)['raw'] == value, 'TRANSITION_BACKUP_CHANGED')
                else: replace(target, value, 0o600)
            rows.append(row)
        record['files'] = rows
        self.save(record, 'prepared')

    def verify_files(self, record, wanted=None, skip=()):
        targets = self.targets(record)
        require([row['key'] for row in record['files']] == list(targets), 'TRANSITION_RECORD_INVALID')
        self.bundle.check_tree(self.root, 'mixed')
        for index, row in enumerate(record['files']):
            require(type(row['index']) is int and row['index'] == index, 'TRANSITION_RECORD_INVALID')
            if row['key'] in self.bundle.payload:
                require((row['before']['sha256'] if row['before'] else None) == self.bundle.base.get(row['key'])
                        and row['after']['sha256'] == sha(self.bundle.payload[row['key']]), 'TRANSITION_RECORD_INVALID')
            actual = metadata(read(targets[row['key']], missing=True))
            require(actual in [row[wanted]] if wanted and row['key'] not in skip else actual in [row['before'], row['after']], 'TRANSITION_EXTERNAL_EDIT')
            for suffix in ('before', 'after'):
                if row[suffix] is None: continue
                backup = read(self.backup_dir(record) / (str(row['index']) + '.' + suffix))
                require(backup['sha256'] == row[suffix]['sha256'] and backup['mode'] == 0o600, 'TRANSITION_BACKUP_CHANGED')
                if row['key'] == 'service':
                    require(json.loads(backup['raw']) == record[suffix], 'TRANSITION_RECORD_INVALID')
                elif row['key'] == 'worker':
                    data = json.loads(backup['raw'])
                    require(data == {'schema': 1, 'kind': 'guarded-worker-core', 'binding': record[suffix]['binding']}, 'TRANSITION_RECORD_INVALID')

    def control_guard(self, record, rollback=False):
        control = self.store.control(); baseline = record['control_stopped']
        require(control['desired'] == 'paused' and control['recovery'] in (None, PENDING), 'TRANSITION_CONTROL_CHANGED')
        if not rollback:
            require(control == baseline or control == dict(baseline, recovery=PENDING), 'TRANSITION_STOP_REQUEST_CHANGED')
        return control

    def mutate(self, record, wanted, rollback=False, skip=()):
        self.verify_files(record)
        rows = record['files'] if wanted == 'after' else list(reversed(record['files']))
        for row in rows:
            if row['key'] in skip: continue
            self.exit_proof(record)
            with self.store.lock():
                self.control_guard(record, rollback)
                path = self.targets(record)[row['key']]
                actual = metadata(read(path, missing=True))
                require(actual in [row['before'], row['after']], 'TRANSITION_EXTERNAL_EDIT')
                if actual != row[wanted]:
                    if row[wanted] is None:
                        path.unlink(); service.fsync_directory(path.parent)
                    else:
                        raw = read(self.backup_dir(record) / (str(row['index']) + '.' + wanted))['raw']
                        require(sha(raw) == row[wanted]['sha256'], 'TRANSITION_BACKUP_CHANGED')
                        replace(path, raw, row[wanted]['mode'])
            self.hook('file:' + row['key'])
        self.verify_files(record, wanted, skip)

    def rollback_binding(self, record, wanted):
        row = next(row for row in record['files'] if row['key'] == 'worker')
        with self.store.lock():
            self.control_guard(record, True)
            path = self.store.path / 'owner.json'
            actual = metadata(read(path))
            require(actual in [row['before'], row['after']], 'TRANSITION_EXTERNAL_EDIT')
            if actual != row[wanted]:
                raw = read(self.backup_dir(record) / (str(row['index']) + '.' + wanted))['raw']
                require(sha(raw) == row[wanted]['sha256'], 'TRANSITION_BACKUP_CHANGED')
                replace(path, raw, row[wanted]['mode'])
        self.hook('rollback:binding_' + wanted)

    def finish(self):
        with self.locks():
            record = self.load()
            require(record['phase'] in ('stop_requested', 'prepared', 'writing', 'committed'), 'TRANSITION_FINISH_REFUSED')
            self.exit_proof(record)
            if record['phase'] == 'stop_requested': self.capture(record)
            self.verify_files(record)
            with self.store.lock():
                control = self.control_guard(record)
                if record['phase'] != 'committed': ws.write(self.store.path / 'control.json', dict(control, recovery=PENDING))
            if record['phase'] != 'committed':
                self.save(record, 'writing'); self.mutate(record, 'after')
                require(self.unchanged_identity(record['before']['binding']) == record['after']['binding'], 'SERVICE_BINDING_CHANGED')
                self.bundle.check_tree(self.root, 'candidate')
                with self.store.lock(): self.control_guard(record); self.save(record, 'committed')
            self.verify_files(record, 'after')
            with self.store.lock():
                control = self.control_guard(record)
                if control['recovery'] == PENDING: ws.write(self.store.path / 'control.json', dict(control, recovery=None))
            self.hook('committed_paused')
            return self.result(record)

    def rollback(self):
        with self.locks():
            record = self.load()
            require(record['phase'] in ('stop_requested', 'prepared', 'writing', 'committed', 'rolling_back', 'rolled_back'), 'TRANSITION_ROLLBACK_REFUSED')
            self.exit_proof(record)
            if record['phase'] == 'stop_requested' or record['phase'] == 'rolled_back' and 'files' not in record:
                # No code/binding writes began. Do not create backups or adopt
                # a changed baseline when cancelling an uncertain stop.
                self.bundle.check_tree(self.root)
                require(service.load(self.directory) == record['before']
                        and self.store.manifest()['binding'] == record['before']['binding'], 'SERVICE_BINDING_CHANGED')
                require(self.store.control()['recovery'] is None, 'WORKER_RECOVERY_REQUIRED')
                if record['phase'] != 'rolled_back': self.save(record, 'rolled_back')
                return self.result(record)
            self.verify_files(record)
            with self.store.lock():
                control = self.control_guard(record, True)
                if record['phase'] != 'rolled_back': ws.write(self.store.path / 'control.json', dict(control, recovery=PENDING))
            if record['phase'] != 'rolled_back':
                self.save(record, 'rolling_back')
                # Hold the new binding until all old code/service bytes and the
                # terminal journal are durable. Old resume code does not know
                # this journal, but must refuse that binding mismatch.
                self.rollback_binding(record, 'after')
                self.mutate(record, 'before', True, skip={'worker'})
                require(self.unchanged_identity(record['before']['binding']) == record['before']['binding'], 'SERVICE_BINDING_CHANGED')
                self.bundle.check_tree(self.root)
                with self.store.lock(): self.control_guard(record, True); self.save(record, 'rolled_back')
            self.verify_files(record, 'before', skip={'worker'})
            self.rollback_binding(record, 'before')
            self.verify_files(record, 'before')
            with self.store.lock():
                control = self.control_guard(record, True)
                if control['recovery'] == PENDING: ws.write(self.store.path / 'control.json', dict(control, recovery=None))
            return self.result(record)

    def result(self, record):
        return {'result': 'passed', 'phase': record['phase'], 'transition_id': record['id'],
                'desired_state': 'paused', 'worker_started': False, 'enable_link_created': False,
                'unit_bytes_changed': False, 'linger_changed': False, 'activation_supported': False,
                'boot_ready': False, 'prior_worker_observation_preserved': True}
