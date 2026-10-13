"""Scoped reset helper, executed with one dedicated volume mounted at /data."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


def check(ok, message):
    if not ok:
        raise ValueError(message)


def uuid_text(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def inventory(root, kind, allowed_ids):
    check(root.is_dir() and not root.is_symlink(), 'Expected real volume root')
    check(kind in ('training-outputs', 'serving-cache', 'gpu-admission'), 'Unknown reset volume')
    allowed = set(allowed_ids)
    for p in root.iterdir():
        if kind == 'gpu-admission':
            check(p.name in ('admission.lock', 'claims.json') or re.fullmatch(r'claims-[A-Za-z0-9]+\.tmp', p.name),
                  'Unexpected admission file')
        else:
            check(p.name in allowed and uuid_text(p.name) and p.is_dir() and not p.is_symlink(),
                  'Volume directory is not linked to a platform database ID')
    ledger = root / 'claims.json'
    if kind == 'gpu-admission' and ledger.exists():
        check(not ledger.is_symlink() and ledger.stat().st_size <= 16 * 1024 ** 2, 'Unsafe admission ledger')
        saved = json.loads(ledger.read_text())
        check(saved.get('version') == 1 and isinstance(saved.get('claims'), dict), 'Unknown admission format')
        for name, claim in saved['claims'].items():
            check(claim.get('operation_id') in allowed and claim.get('name') == name,
                  'Admission claim belongs to an unknown operation')
        check(set(saved.get('cancelled_operations', {})) <= allowed, 'Unknown admission cancellation')
    rows = []
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(base) / name
            st = p.lstat()
            check(st.st_dev == root.stat().st_dev, 'Nested filesystem in reset volume')
            check(stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode), 'Link or special file in reset volume')
            row = {'path': p.relative_to(root).as_posix(), 'type': 'file' if stat.S_ISREG(st.st_mode) else 'directory'}
            if row['type'] == 'file':
                row.update(size=st.st_size, mtime_ns=st.st_mtime_ns)
            rows.append(row)
    return sorted(rows, key=lambda r: r['path'])


def reset(root, kind, allowed_ids, planned):
    current = inventory(root, kind, allowed_ids)
    expected = {r['path']: r for r in planned}
    check(len(expected) == len(planned), 'Duplicate planned file')
    check(all(expected.get(r['path']) == r for r in current), 'Volume changed after preparation')
    # Delete only freshly observed entries; bottom-up and no recursive path expansion.
    for row in sorted(current, key=lambda r: (len(Path(r['path']).parts), r['path']), reverse=True):
        p = root / row['path']
        check(not p.is_symlink(), 'Volume entry changed to a link')
        if row['type'] == 'file':
            p.unlink()
        else:
            p.rmdir()
    check(not list(root.iterdir()), 'Reset volume is not empty')
    return {'empty': True, 'removed_files': sum(r['type'] == 'file' for r in current),
            'removed_bytes': sum(r.get('size', 0) for r in current)}


def main():
    request = json.load(sys.stdin)
    root = Path('/data')
    if request['mode'] == 'inventory':
        result = inventory(root, request['kind'], request['allowed_ids'])
    elif request['mode'] == 'apply':
        result = reset(root, request['kind'], request['allowed_ids'], request['entries'])
    else:
        raise ValueError('Unknown reset mode')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
