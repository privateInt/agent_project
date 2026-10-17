"""Linux/Python3.10 fixture-controller package entry; never a native dispatcher.

Bootstrap is externally hash-pinned before execution. No RPC, CLI queue, automatic
DB initialization/migration, service control, credential access or PAUSE removal.
"""
import argparse
import contextlib
import ctypes
import datetime as dt
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import uuid
from urllib.parse import quote

sys.dont_write_bytecode = True
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
APPROVED_WORKSPACE = Path('/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project')
DBS = (
    ('fixture-ledger.sqlite', 'github_fixture_ledger', 'initialize_fixture_ledger', 'LEDGER_READY'),
    ('fixture-controller.sqlite', 'github_fixture_controller', 'initialize_fixture_controller', 'CONTROLLER_READY'),
    ('intents.sqlite', 'codex_unattended_intent_journal', 'initialize_intent_journal', 'JOURNAL_READY'),
    ('bindings.sqlite', 'codex_unattended_submission_binding', 'initialize_submission_bindings', 'BINDINGS_READY'),
    ('receipts.sqlite', 'codex_unattended_queue_receipts', 'initialize_queue_receipts', 'RECEIPTS_READY'),
)
MUTABLE = {x[0] for x in DBS} | {'init.json', 'init-failed.json', 'run.lock', 'assignment.json', 'checkpoint.json', 'admissions.json', 'PAUSE'}


class PackageError(Exception):
    pass


def need(value, code):
    if not value:
        raise PackageError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def unique(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, 'JSON_DUPLICATE_KEY')
        value[key] = item
    return value


def decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _x: (_ for _ in ()).throw(PackageError('JSON_INVALID')))
    except PackageError:
        raise
    except Exception:
        raise PackageError('JSON_INVALID') from None


def directory(path, uid, *, create=False):
    path = Path(path)
    need(path.is_absolute() and str(path) == os.path.normpath(path) and '..' not in path.parts, 'PATH_INVALID')
    fd = os.open('/', DIR_FLAGS)
    root_uid = os.fstat(fd).st_uid
    try:
        walked = Path('/')
        for part in path.parts[1:]:
            walked = walked / part
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            need(info.st_uid in (uid, root_uid), 'DIRECTORY_UID_INVALID')
            need(not mode & 0o022 or (walked == Path('/tmp') and info.st_uid == root_uid and mode == 0o1777), 'DIRECTORY_MODE_INVALID')
        info = os.fstat(fd)
        need(info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o700, 'DIRECTORY_PRIVATE_REQUIRED')
    finally:
        os.close(fd)
    return path


def read_file(path, uid, *, mode=0o600, maximum=1048576):
    directory(Path(path).parent, uid)
    fd = os.open(path, FILE_FLAGS)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == uid
             and stat.S_IMODE(info.st_mode) == mode and info.st_size <= maximum, 'FILE_METADATA_INVALID')
        raw = os.read(fd, maximum + 1)
        need(len(raw) == info.st_size, 'FILE_SIZE_INVALID')
        return raw
    finally:
        os.close(fd)


def atomic(path, raw, uid):
    path = Path(path)
    if os.path.lexists(path):
        read_file(path, uid)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dfd = os.open(path.parent, DIR_FLAGS)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        # An interrupted temporary is evidence, not silently repaired/deleted.
        raise


def rename_new(source, destination, uid):
    source, destination = Path(source), Path(destination)
    need(source.parent == destination.parent, 'RENAME_PARENT_MISMATCH')
    directory(source.parent, uid)
    need(not source.is_symlink(), 'SOURCE_SYMLINK')
    parent = os.open(source.parent, DIR_FLAGS)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, 'renameat2', None)
        need(function is not None, 'RENAME_NOREPLACE_UNSUPPORTED')
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(parent, os.fsencode(source.name), parent, os.fsencode(destination.name), 1):
            raise PackageError('RENAME_DESTINATION_EXISTS_OR_FAILED')
        os.fsync(parent)
    finally:
        os.close(parent)


def verify(root, expected_manifest, expected_version, uid, *, located=True):
    need(type(uid) is int and uid == os.geteuid(), 'UID_EXPECTATION_MISMATCH')
    need(re.fullmatch('[0-9a-f]{64}', expected_manifest or '') is not None, 'MANIFEST_EXPECTATION_REQUIRED')
    need(re.fullmatch('[0-9a-f]{40}', expected_version or '') is not None, 'VERSION_EXPECTATION_REQUIRED')
    root = directory(Path(root), uid)
    raw = read_file(root / 'manifest.json', uid)
    need(digest(raw) == expected_manifest, 'MANIFEST_HASH_MISMATCH')
    manifest = decode(raw)
    need(type(manifest) is dict and set(manifest) == {'schema', 'code_version', 'expected_uid', 'files', 'development_launcher'}, 'MANIFEST_INVALID')
    need(manifest['schema'] == 1 and manifest['code_version'] == expected_version and manifest['expected_uid'] == uid, 'VERSION_OR_UID_MISMATCH')
    entries = manifest['files']
    need(type(entries) is list and 10 <= len(entries) <= 64, 'MANIFEST_INVALID')
    names = set()
    for entry in entries:
        need(type(entry) is dict and set(entry) == {'path', 'sha256', 'size', 'mode', 'uid', 'origin'}, 'MANIFEST_INVALID')
        name = entry['path']
        need(type(name) is str and name and not name.startswith('/') and PurePosixPath(name).as_posix() == name
             and all(p not in ('', '.', '..') for p in name.split('/')) and name not in names
             and name != 'manifest.json' and name.split('/')[0] != 'state', 'MANIFEST_PATH_INVALID')
        names.add(name)
        need(entry['uid'] == uid and type(entry['mode']) is int and entry['mode'] in (0o600, 0o644)
             and type(entry['size']) is int and 0 <= entry['size'] <= 1048576
             and type(entry['sha256']) is str and re.fullmatch('[0-9a-f]{64}', entry['sha256']) is not None, 'MANIFEST_INVALID')
        content = read_file(root / name, uid, mode=entry['mode'])
        need(len(content) == entry['size'], 'PAYLOAD_SIZE_MISMATCH')
        need(digest(content) == entry['sha256'], 'PAYLOAD_HASH_MISMATCH')
    actual = set()
    for base, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            p = Path(base) / name
            need(not p.is_symlink(), 'DIRECTORY_SYMLINK')
            if p == root / 'state':
                directories.remove(name)
            else:
                directory(p, uid)
        actual.update(str((Path(base) / name).relative_to(root)) for name in files)
    need(actual == names | {'manifest.json'}, 'UNREGISTERED_INSTALLATION_FILE')
    config = decode(read_file(root / 'runtime.json', uid))
    need(type(config) is dict and set(config) == {'schema', 'project_id', 'repository', 'workspace', 'target_base', 'state_dir', 'installation_root', 'pause_paths', 'schedule', 'fixture_jobs', 'role_threads', 'native_dispatch', 'temporary_fixture'}, 'CONFIG_INVALID')
    need(config['schema'] == 1 and config['project_id'] == 'agent_project' and config['repository'] == 'privateInt/agent_project'
         and config['native_dispatch'] is False, 'PROJECT_OR_NATIVE_EXECUTION_BLOCKED')
    workspace = Path(config['workspace'])
    need(workspace == APPROVED_WORKSPACE, 'WORKSPACE_INVALID')
    directory(workspace, uid)
    target = Path(config['target_base'])
    need(target.is_absolute() and str(target) == os.path.normpath(target) and (target.is_relative_to(workspace) or (config['temporary_fixture'] is True and target.is_relative_to(Path('/tmp'))))
         and target != workspace and Path(config['installation_root']) == target / 'installation'
         and Path(config['state_dir']) == target / 'state', 'TARGET_PATH_INVALID')
    fixture_config = decode(read_file(root / 'fixture-controller.json', uid))
    need(fixture_config == {'schema': 1, 'installation_root': str(target / 'installation'),
         'state_dir': str(target / 'state'), 'ledger_db': str(target / 'state/fixture-ledger.sqlite'),
         'control_db': str(target / 'state/fixture-controller.sqlite')}, 'CONTROLLER_CONFIG_BINDING_INVALID')
    if located:
        need(root == target and Path(config['installation_root']) == root / 'installation', 'INSTALLATION_LOCATION_MISMATCH')
    need(type(config['pause_paths']) is list and len(config['pause_paths']) == 3
         and all(type(p) is str and Path(p).is_absolute() for p in config['pause_paths']), 'PAUSE_CONFIG_INVALID')
    return manifest, config


def modules(root):
    installation = root / 'installation'
    sys.path.insert(0, str(installation))
    result = {}
    for _file, name, _api, _reason in DBS:
        mod = importlib.import_module('src.ops.tools.' + name)
        need(Path(mod.__file__).absolute() == installation / 'src/ops/tools' / (name + '.py'), 'MODULE_ORIGIN_MISMATCH')
        result[name] = mod
    result['github_fixture_config'] = importlib.import_module('src.ops.tools.github_fixture_config')
    spec = importlib.util.spec_from_file_location('_package_schedule', installation / 'schedule_policy.py')
    schedule = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schedule)
    result['schedule'] = schedule
    return result


def state_verify(root, config, uid):
    state = directory(Path(config['state_dir']), uid)
    need((state / 'init.json').exists() and not (state / 'init-failed.json').exists(), 'STATE_INITIALIZATION_INCOMPLETE')
    for path in state.iterdir():
        need(path.name in MUTABLE, 'UNREGISTERED_STATE_FILE')
        read_file(path, uid, maximum=4194304)
    schemas = decode(read_file(root / 'schema-contracts.json', uid))
    identities = []
    for name, _module, _api, _reason in DBS:
        p = state / name
        raw = read_file(p, uid, maximum=4194304)
        need(raw[:16] == b'SQLite format 3\0' and raw[18:20] == b'\x01\x01', 'DATABASE_HEADER_INVALID')
        info = p.stat()
        identities.append((info.st_dev, info.st_ino))
        with contextlib.closing(sqlite3.connect('file:' + quote(str(p), safe='/') + '?mode=ro', uri=True, isolation_level=None)) as conn:
            conn.execute('PRAGMA query_only=ON')
            actual = [row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")]
            need(actual == [schemas[name]['ddl']] and conn.execute('PRAGMA application_id').fetchone()[0] == schemas[name]['application_id']
                 and conn.execute('PRAGMA user_version').fetchone()[0] == 1, 'DATABASE_SCHEMA_INVALID')
    need(len(set(identities)) == 5, 'DATABASE_ALIAS')
    return state


def init_new(root, config, uid):
    state = Path(config['state_dir'])
    need(not os.path.lexists(state), 'STATE_ALREADY_EXISTS')
    os.mkdir(state, 0o700)
    created = []
    try:
        imports = modules(root)
        for filename, name, api, reason in DBS:
            path = state / filename
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            created.append(filename)
            with contextlib.closing(sqlite3.connect(path, isolation_level=None, timeout=0.1)) as conn:
                conn.execute('PRAGMA synchronous=FULL')
                conn.execute('PRAGMA busy_timeout=100')
                result = getattr(imports[name], api)(conn)
                need(result['report']['reason'] == reason, 'INITIALIZER_NOT_CONFIRMED')
        atomic(state / 'init.json', canonical({'schema': 1, 'created': created}), uid)
        state_verify(root, config, uid)
        return {'status': 'INITIALIZED', 'databases': created}
    except Exception:
        atomic(state / 'init-failed.json', canonical({'status': 'INIT_FAILED', 'created': created}), uid)
        raise PackageError('INIT_FAILED_PARTIAL_STATE_PRESERVED') from None


@contextlib.contextmanager
def lock(state, uid):
    path = state / 'run.lock'
    if os.path.lexists(path):
        read_file(path, uid)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        need(os.fstat(fd).st_nlink == 1 and os.fstat(fd).st_uid == uid and stat.S_IMODE(os.fstat(fd).st_mode) == 0o600, 'LOCK_INVALID')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise PackageError('ALREADY_RUNNING') from None
    finally:
        os.close(fd)


def job_bytes(config, job):
    need(type(job) is dict and set(job) == {'assignment', 'sha256'}, 'JOB_INVALID')
    raw = canonical(job['assignment'])
    value = job['assignment']
    need(digest(raw) == job['sha256'] and value.get('mode') == 'fixture'
         and value.get('target', {}).get('repository') == config['repository'], 'JOB_BINDING_INVALID')
    return raw


def admit(state, config, policy, uid):
    need(not any(os.path.lexists(p) for p in config['pause_paths']), 'PAUSED')
    try:
        policy.validate_config(config['schedule'])
        now = dt.datetime.now(dt.timezone.utc)
        windows = policy.windows(config['schedule'], now)
        need(bool(windows), 'SCHEDULE_CLOSED')
        start, end, cap, source_day = windows[0]
        need(now + dt.timedelta(seconds=122) < end, 'WINDOW_MARGIN')
    except PackageError:
        raise
    except Exception:
        raise PackageError('SCHEDULE_INVALID') from None
    path = state / 'admissions.json'
    history = decode(read_file(path, uid)) if path.exists() else {'clock_floor_ms': 0, 'starts': []}
    milliseconds = int(now.timestamp() * 1000)
    need(milliseconds >= history['clock_floor_ms'] and len(history['starts']) <= 64, 'CLOCK_OR_ADMISSIONS_INVALID')
    need(sum(start.timestamp() * 1000 <= x < end.timestamp() * 1000 for x in history['starts']) < cap, 'START_CAP_REACHED')
    history['starts'].append(milliseconds)
    history['clock_floor_ms'] = milliseconds
    atomic(path, canonical(history), uid)
    return source_day


def workflow(root, config, manifest, uid, *, execute):
    state = state_verify(root, config, uid)
    imports = modules(root)
    imports['schedule'].validate_config(config['schedule'])
    need(type(config['fixture_jobs']) is list and len(config['fixture_jobs']) <= 32, 'JOBS_INVALID')
    jobs = [(job, job_bytes(config, job)) for job in config['fixture_jobs']]
    ids = [job['assignment']['job_id'] for job, _raw in jobs]
    need(len(set(ids)) == len(ids), 'DUPLICATE_JOB')
    pins = {e['path'].removeprefix('installation/'): e['sha256'] for e in manifest['files'] if e['path'].removeprefix('installation/') in imports['github_fixture_config']._SOURCE_FILES}
    fixture_config = read_file(root / 'fixture-controller.json', uid)
    observed = []
    with lock(state, uid), imports['github_fixture_config'].load_fixture_controller(
            fixture_config, expected_code_version=manifest['code_version'], expected_source_sha256=pins, expected_uid=uid) as controller:
        for job, raw in jobs:
            assignment = job['assignment']
            result = controller.read(assignment['job_id'], job['sha256'])
            if execute and result['report']['reason'] == 'JOB_NOT_FOUND':
                admit(state, config, imports['schedule'], uid)
                atomic(state / 'assignment.json', raw, uid)
                need(read_file(state / 'assignment.json', uid) == raw, 'ASSIGNMENT_FILE_MISMATCH')
                result = controller.run(job['sha256'], raw)
            reason = result['report']['reason']
            artifact = result.get('artifact')
            status = ('SUCCEEDED' if result['report'].get('collection_reason') == 'EXIT_ZERO' else 'FAILED') if reason == 'ARTIFACT_STORED' else reason
            row = {'job_id': assignment['job_id'], 'assignment_sha256': job['sha256'], 'reason': reason,
                   'status': status, 'artifact_sha256': digest(artifact) if type(artifact) is bytes else None}
            observed.append(row)
            if execute:
                atomic(state / 'checkpoint.json', canonical({'jobs': observed, 'next_index': len(observed), 'native_ready': False}), uid)
            if execute and reason != 'ARTIFACT_STORED':
                break
    return {'status': 'OBSERVED', 'jobs': observed, 'native_ready': False, 'native_dispatch': False}


def stage(root, config, pin, version, uid):
    destination = Path(config['target_base'])
    need(not destination.is_relative_to(root) and not root.is_relative_to(destination), 'STAGING_PATH_OVERLAP')
    directory(destination.parent, uid, create=True)
    temporary = destination.with_name('.' + destination.name + '.staging-' + uuid.uuid4().hex)
    os.mkdir(temporary, 0o700)
    for parent, directories, files in os.walk(root):
        rel = Path(parent).relative_to(root)
        need('state' not in rel.parts, 'STAGING_STATE_FORBIDDEN')
        for name in directories:
            need(name != 'state', 'STAGING_STATE_FORBIDDEN')
            (temporary / rel / name).mkdir(mode=0o700)
        for name in files:
            source = Path(parent) / name
            target = temporary / rel / name
            with target.open('xb') as stream:
                stream.write(source.read_bytes())
            target.chmod(stat.S_IMODE(source.stat().st_mode))
    verify(temporary, pin, version, uid, located=False)
    rename_new(temporary, destination, uid)
    verify(destination, pin, version, uid)
    return {'status': 'STAGED', 'destination': str(destination), 'state_initialized': False}


def rollback(root, config, uid, destination):
    need(Path(config['target_base']) == root, 'ROLLBACK_LOCATION_INVALID')
    target = Path(destination)
    need(target.parent == root.parent and target != root, 'ROLLBACK_DESTINATION_INVALID')
    state = state_verify(root, config, uid)
    with lock(state, uid):
        rename_new(root, target, uid)
    return {'status': 'RETIRED_PRESERVED', 'destination': str(target), 'db_deleted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['verify-bundle', 'stage', 'verify', 'init-new', 'run', 'status', 'rollback'])
    parser.add_argument('--expected-manifest-sha256', required=True)
    parser.add_argument('--expected-code-version', required=True)
    parser.add_argument('--expected-uid', required=True, type=int)
    parser.add_argument('--rollback-to')
    args = parser.parse_args()
    root = Path(__file__).absolute().parent.parent
    try:
        manifest, config = verify(root, args.expected_manifest_sha256, args.expected_code_version, args.expected_uid,
                                  located=args.action not in ('stage', 'verify-bundle'))
        if args.action == 'verify-bundle':
            result = {'status': 'BUNDLE_VERIFIED', 'code_version': manifest['code_version'], 'native_ready': False}
        elif args.action == 'stage':
            result = stage(root, config, args.expected_manifest_sha256, args.expected_code_version, args.expected_uid)
        elif args.action == 'init-new':
            result = init_new(root, config, args.expected_uid)
        elif args.action == 'verify':
            state_verify(root, config, args.expected_uid)
            result = {'status': 'VERIFIED', 'db_count': 5, 'native_ready': False}
        elif args.action == 'rollback':
            need(args.rollback_to is not None, 'ROLLBACK_DESTINATION_REQUIRED')
            result = rollback(root, config, args.expected_uid, args.rollback_to)
        else:
            result = workflow(root, config, manifest, args.expected_uid, execute=args.action == 'run')
        print(json.dumps(result, sort_keys=True))
        if args.action == 'run':
            if any(x['reason'] != 'ARTIFACT_STORED' for x in result['jobs']):
                return 3
            if any(x['status'] == 'FAILED' for x in result['jobs']):
                return 1
        return 0
    except Exception as error:
        code = str(error) if type(error) is PackageError else 'UNCONFIRMED_LOCAL_FAILURE'
        print(json.dumps({'status': 'REJECTED', 'reason': code, 'native_ready': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
