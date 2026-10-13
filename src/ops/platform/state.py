"""Only successful-read timestamps are persisted; response bodies and credentials are not."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .contracts import ReadFailure, require, timestamp
from .transport import strict_json


@contextmanager
def success_cache(path):
    path = Path(path).expanduser().absolute()
    for parent in [path, *path.parents]:
        require(not parent.is_symlink(), 'STATE_SYMLINK_REFUSED')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.parent.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'STATE_DIRECTORY_NOT_PRIVATE')
    fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077, 'INVALID_STATE_LOCK')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReadFailure('STATE_BUSY') from None
        times = {}
        if path.exists():
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
                    and info.st_size <= 512*1024, 'INVALID_STATE_FILE')
            raw = strict_json(path.read_bytes())
            require(isinstance(raw, dict) and raw.get('schema') == 1 and isinstance(raw.get('success_times'), dict), 'INVALID_STATE_FILE')
            for key, value in raw['success_times'].items():
                require(re.fullmatch('[0-9a-f]{64}', key) is not None and timestamp(value) is not None, 'INVALID_STATE_FILE')
                times[key] = value
        yield times
        # Bound growth from detail IDs/cursors. Sorted ISO UTC values are produced by the client.
        times = dict(sorted(times.items(), key=lambda x: x[1], reverse=True)[:1000])
        raw = (json.dumps({'schema': 1, 'success_times': times}, indent=2)+'\n').encode()
        tmp_fd, name = tempfile.mkstemp(prefix='.api-status-', dir=path.parent)
        try:
            with os.fdopen(tmp_fd, 'wb') as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    finally:
        os.close(fd)
