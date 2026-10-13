"""Private controller records and a lease shared by both automatic entry points."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.ops import manage, restart_check as rc
from src.ops.platform.transport import strict_json

LIMIT = 32768


def read(path):
    return strict_json(rc.read_file(path, LIMIT, private=True)[0])


def write(path, value):
    require(len(json.dumps(value).encode()) < LIMIT, 'WORKER_RECORD_LIMIT')
    if path.exists(): manage.private(path)
    rc.write_record(path, value)


@contextmanager
def file_lock(path, busy):
    manage.private(path.parent); manage.safe(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077
                and st.st_nlink == 1, 'PRIVATE_STATE_REQUIRED')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ReadFailure(busy) from None
        # Never unlink or replace a lease inode, including at normal shutdown.
        yield fd
    finally: os.close(fd)


def automatic_lease(directory):
    return file_lock(manage.private(directory) / 'automatic-refresh.lock', 'AUTOMATIC_WORKER_BUSY')


def location(directory):
    directory = manage.private(directory)
    owner = read(directory / 'owner.json')
    require(owner.get('uid') == os.getuid() and owner.get('gid') == os.getgid()
            and str(uuid.UUID(owner['owner'])) == owner['owner'], 'OWNER_MISMATCH')
    return manage.safe(directory.parent / 'auto-refresh' / owner['owner'] / 'worker')


class Store:
    def __init__(self, directory, binding=None):
        self.directory = manage.private(directory)
        self.path = location(directory)
        if binding is not None:
            for folder in (self.path.parent.parent, self.path.parent, self.path):
                folder.mkdir(mode=0o700, exist_ok=True); manage.private(folder)
            with self.lock():
                manifest = self.path / 'owner.json'
                if not manifest.exists():
                    require(set(p.name for p in self.path.iterdir()) <= {'control.lock'}, 'UNOWNED_WORKER_STATE')
                    write(manifest, {'schema': 1, 'kind': 'guarded-worker-core', 'binding': binding})
                    write(self.path / 'control.json', {'schema': 1, 'desired': 'running', 'recovery': None})
                require(self.manifest().get('binding') == binding, 'WORKER_BINDING_CHANGED')
        manage.private(self.path)

    def lock(self): return file_lock(self.path / 'control.lock', 'WORKER_CONTROL_BUSY')

    def manifest(self):
        data = read(self.path / 'owner.json')
        require(data.get('schema') == 1 and data.get('kind') == 'guarded-worker-core'
                and isinstance(data.get('binding'), dict), 'WORKER_OWNER_INVALID')
        return data

    def control(self):
        data = read(self.path / 'control.json')
        require(data.get('schema') == 1 and data.get('desired') in ('running', 'paused')
                and (data.get('recovery') is None or isinstance(data['recovery'], str)), 'WORKER_CONTROL_INVALID')
        return data

    def optional(self, name):
        path = self.path / name
        return read(path) if path.exists() or path.is_symlink() else None

    def gate(self):
        transition_gate(self.directory)
        data = self.control()
        require(data['recovery'] is None, 'WORKER_RECOVERY_REQUIRED')
        intent = self.optional('intent.json')
        require(not intent or intent.get('phase') == 'completed', 'WORKER_UNFINISHED_INTENT')
        require(data['desired'] == 'running', 'WORKER_PAUSED')
        require(not data.get('boot_exit_request'), 'WORKER_EXIT_REQUESTED')

    def pause(self, reason=None):
        with self.lock():
            data = self.control(); before = dict(data); data['desired'] = 'paused'
            if before['desired'] != 'paused': data['pause_requested_at'] = rc.now()
            if reason is not None: data['recovery'] = reason
            if data != before: write(self.path / 'control.json', data)
        return data

    def status(self, **values):
        write(self.path / 'status.json', dict(schema=1, checked_at=rc.now(), **values))


def existing_worker_gate(directory):
    path = location(directory)
    if path.exists() or path.is_symlink():
        store = Store(directory); store.manifest(); store.gate()
        # The legacy watch has no controller-intent/deadline protocol. Once a
        # managed core has state, only that core may perform automatic refresh.
        raise ReadFailure('GUARDED_WORKER_REQUIRED')


def status(directory):
    """Local metadata only; never create a directory/lock or signal a saved PID."""
    path = location(directory)
    if not path.exists(): return {'status': 'not_initialized'}
    store = Store(directory); store.manifest()
    return {'status': 'core_only', 'control': store.control(), 'intent': store.optional('intent.json'),
            'last_observation': store.optional('status.json'), 'live_process_verified': False,
            'service_installed': False}


def transition_gate(directory):
    """Partial code/policy migration must never be acknowledged as a refresh."""
    path = location(directory).parent / 'code-transition.json'
    if not path.exists() and not path.is_symlink(): return
    record = read(path)
    require(record.get('schema') == 1 and record.get('kind') == 'stopped-code-policy-transition'
            and record.get('directory') == str(directory), 'CODE_TRANSITION_RECORD_INVALID')
    require(record.get('phase') in ('committed', 'rolled_back'), 'CODE_TRANSITION_RECOVERY_REQUIRED')
