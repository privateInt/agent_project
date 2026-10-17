"""Seven new package acceptance groups; no historical suite or native RPC."""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
SOURCE = Path(__file__).absolute().parents[1]
REPO = SOURCE.parents[2]
WORKSPACE = Path('/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project')
BASE = '314622f0ef1abcb6821ae7cd504cc3d793021f4d'
UID = os.geteuid()
OBSERVATIONS = []


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


@contextlib.contextmanager
def isolated_imports():
    before_path, before_modules = sys.path[:], dict(sys.modules)
    for name in list(sys.modules):
        if name == 'src' or name.startswith('src.'):
            del sys.modules[name]
    try:
        yield
    finally:
        sys.path[:] = before_path
        for name in list(sys.modules):
            if name not in before_modules or name == 'src' or name.startswith('src.'):
                del sys.modules[name]
        sys.modules.update(before_modules)


class Fixture:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix='agent-execution-package-', dir='/tmp'))
        self.base.chmod(0o700)
        self.output = self.base / 'prepared'
        self.target = self.base / 'candidate'
        self.builder = load('_builder', SOURCE / 'build_agent_execution_package.py')
        self.meta = self.builder.build(REPO, BASE, self.output, self.target, WORKSPACE, UID,
                                      development_launcher=SOURCE / 'agent_execution_package.py', temporary_fixture=True)
        self.bundle = self.output / 'bundle'
        self.pin = self.meta['manifest_sha256']
        self.launcher = load('_entry', self.bundle / 'installation/launch.py')
        self.root = self.bundle
        self.calls = []
        OBSERVATIONS.append({'temporary_root': str(self.base), 'baseline': BASE, 'development_launcher': True})

    def args(self):
        return ['--expected-manifest-sha256', self.pin, '--expected-code-version', BASE, '--expected-uid', str(UID)]

    def cli(self, action, *, root=None, extra=()):
        command = ['/usr/bin/python3', '-I', '-S', '-B', str((root or self.root) / 'installation/launch.py'), action] + self.args() + list(extra)
        self.calls.append(command)
        result = subprocess.run(command, cwd='/', env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, capture_output=True, timeout=8)
        try:
            value = json.loads(result.stdout)
        except Exception:
            raise AssertionError((result.returncode, result.stdout, result.stderr))
        OBSERVATIONS.append({'action': action, 'exit': result.returncode, 'result': value})
        return result.returncode, value

    def ready(self):
        assert self.cli('stage')[0] == 0
        self.root = self.target
        assert self.cli('init-new')[0] == 0
        return self

    def verified(self):
        return self.launcher.verify(self.root, self.pin, BASE, UID, located=self.root == self.target)

    def repin(self, path, raw):
        item = self.root / path
        item.write_bytes(raw)
        manifest = json.loads((self.root / 'manifest.json').read_bytes())
        for entry in manifest['files']:
            if entry['path'] == path:
                entry['sha256'], entry['size'] = hash_bytes(raw), len(raw)
        data = self.builder.canonical(manifest)
        (self.root / 'manifest.json').write_bytes(data)
        self.pin = hash_bytes(data)

    def dbhashes(self):
        return {p.name: hash_bytes(p.read_bytes()) for p in (self.root / 'state').glob('*.sqlite')}


class PackageAcceptance(unittest.TestCase):
    def test_P01_complete_pinned_bundle(self):
        f = Fixture()
        self.assertEqual(f.cli('verify-bundle')[0], 0)
        manifest, config = f.verified()
        self.assertFalse(config['native_dispatch'])
        self.assertEqual(config['workspace'], str(WORKSPACE))
        for e in manifest['files']:
            if e['origin'].startswith(BASE + ':'):
                expected = f.builder.git_bytes(REPO, BASE, e['origin'].split(':', 1)[1])
                self.assertEqual((f.root / e['path']).read_bytes(), expected)
        with isolated_imports():
            modules = f.launcher.modules(f.root)
            for _file, name, _api, _reason in f.launcher.DBS:
                self.assertTrue(Path(modules[name].__file__).is_relative_to(f.root / 'installation'))
        self.assertFalse(list(f.base.rglob('__pycache__')))
        self.assertFalse((f.target / 'state').exists())

    def test_P02_explicit_five_database_initialization(self):
        f = Fixture().ready()
        self.assertEqual(f.cli('verify')[0], 0)
        state = f.root / 'state'
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        schemas = json.loads((f.root / 'schema-contracts.json').read_bytes())
        identities = set()
        for name, schema in schemas.items():
            path = state / name
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            identities.add(path.stat().st_ino)
            with contextlib.closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as db:
                self.assertEqual(db.execute('pragma application_id').fetchone()[0], schema['application_id'])
                self.assertEqual(db.execute('pragma user_version').fetchone()[0], 1)
                self.assertEqual([r[0] for r in db.execute('select sql from sqlite_master where sql is not null')], [schema['ddl']])
        self.assertEqual(len(identities), 5)
        before = f.dbhashes()
        self.assertEqual(f.cli('init-new')[1]['reason'], 'STATE_ALREADY_EXISTS')
        self.assertEqual(before, f.dbhashes())

    def test_P03_real_fixture_collection_reopen_and_reuse(self):
        f = Fixture().ready()
        manifest, config = f.verified()
        with isolated_imports():
            f.launcher.modules(f.root)
            collector = sys.modules['src.ops.tools.github_fixture_collector']
            with mock.patch.object(collector, 'collect_fixture_assignment', wraps=collector.collect_fixture_assignment) as calls:
                report = f.launcher.workflow(f.root, config, manifest, UID, execute=True)
                self.assertEqual(calls.call_count, 2)
                self.assertEqual([j['status'] for j in report['jobs']], ['SUCCEEDED', 'FAILED'])
                before = f.dbhashes()
                again = f.launcher.workflow(f.root, config, manifest, UID, execute=True)
                self.assertEqual(report, again)
                self.assertEqual(calls.call_count, 2)
                self.assertEqual(before, f.dbhashes())
        self.assertEqual(f.cli('status')[1], report)
        self.assertEqual(f.cli('run')[0], 1)
        with contextlib.closing(sqlite3.connect(f.root / 'state/fixture-ledger.sqlite')) as db:
            rows = db.execute('select job_id,assignment,assignment_sha256,revision,artifact,artifact_sha256 from fixture_jobs order by job_id').fetchall()
        for row, job, code in zip(rows, config['fixture_jobs'], (0, 7)):
            self.assertEqual(bytes(row[1]), f.builder.canonical(job['assignment']))
            self.assertEqual(hash_bytes(row[1]), row[2])
            self.assertEqual(row[3], 1)
            self.assertEqual(hash_bytes(row[4]), row[5])
            artifact = json.loads(row[4])
            self.assertEqual(artifact['target'], job['assignment']['target'])
            self.assertEqual(artifact['assignment_sha256'], job['sha256'])
            self.assertEqual(artifact['outcome']['returncode'], code)
        self.assertEqual((f.root / 'state/assignment.json').read_bytes(), f.builder.canonical(config['fixture_jobs'][1]['assignment']))
        self.assertEqual((f.root / 'state/assignment.json').stat().st_mode & 0o777, 0o600)
        OBSERVATIONS.append({'actual_collector_calls': 2, 'repeat_calls': 0, 'exit_codes': [0, 7], 'db_reopen': True})

    def test_P04_manifest_identity_and_boundary_rejections(self):
        f = Fixture()
        original = (f.root / 'usage.md').read_bytes()
        (f.root / 'usage.md').write_bytes(b'X' + original[1:])
        self.assertEqual(f.cli('verify-bundle')[1]['reason'], 'PAYLOAD_HASH_MISMATCH')
        (f.root / 'usage.md').write_bytes(original)
        self.assertEqual(f.cli('verify-bundle', extra=['--expected-uid', str(UID + 1)])[0], 2)
        self.assertEqual(f.cli('verify-bundle', extra=['--expected-code-version', 'f' * 40])[0], 2)
        original_manifest = (f.root / 'manifest.json').read_bytes()
        m = json.loads(original_manifest)
        for path in ('/absolute', '../escape', m['files'][1]['path']):
            changed = json.loads(original_manifest); changed['files'][0]['path'] = path
            raw = f.builder.canonical(changed); (f.root / 'manifest.json').write_bytes(raw); f.pin = hash_bytes(raw)
            self.assertEqual(f.cli('verify-bundle')[0], 2)
        (f.root / 'manifest.json').write_bytes(original_manifest); f.pin = hash_bytes(original_manifest)
        (f.root / 'extra').write_bytes(b'x'); self.assertEqual(f.cli('verify-bundle')[0], 2); (f.root / 'extra').unlink()
        p = f.root / 'usage.md'
        p.chmod(0o644); self.assertEqual(f.cli('verify-bundle')[0], 2); p.chmod(0o600)
        external = f.base / 'external'; p.rename(external); p.symlink_to(external)
        self.assertEqual(f.cli('verify-bundle')[0], 2); p.unlink(); external.rename(p)
        os.link(p, f.base / 'alias'); self.assertEqual(f.cli('verify-bundle')[0], 2); (f.base / 'alias').unlink()
        installation = f.root / 'installation'; installation.rename(f.base / 'linked-installation'); installation.symlink_to(f.base / 'linked-installation', target_is_directory=True)
        self.assertEqual(f.cli('verify-bundle')[0], 2); installation.unlink(); (f.base / 'linked-installation').rename(installation)
        f.base.chmod(0o770); self.assertEqual(f.cli('verify-bundle')[0], 2); f.base.chmod(0o700)
        config = json.loads((f.root / 'runtime.json').read_bytes())
        for key, bad in [('project_id', 'vms_project'), ('native_dispatch', True), ('workspace', '/tmp/other/agent_project')]:
            changed = dict(config); changed[key] = bad
            f.repin('runtime.json', f.builder.canonical(changed))
            self.assertEqual(f.cli('verify-bundle')[0], 2)
        f.repin('runtime.json', f.builder.canonical(config))
        changed = dict(config); changed['installation_root'] = str(f.target / 'other')
        f.repin('runtime.json', f.builder.canonical(changed))
        self.assertEqual(f.cli('stage')[0], 2); self.assertFalse(f.target.exists())
        f.repin('runtime.json', f.builder.canonical(config))
        controller = json.loads((f.root / 'fixture-controller.json').read_bytes()); controller['ledger_db'] = '/tmp/wrong.sqlite'
        f.repin('fixture-controller.json', f.builder.canonical(controller))
        self.assertEqual(f.cli('verify-bundle')[1]['reason'], 'CONTROLLER_CONFIG_BINDING_INVALID')
        real_fstat = os.fstat
        def foreign_file(fd):
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode):
                values = list(info); values[4] = UID + 1
                return os.stat_result(values)
            return info
        with mock.patch.object(f.launcher.os, 'fstat', side_effect=foreign_file):
            with self.assertRaisesRegex(f.launcher.PackageError, 'FILE_METADATA_INVALID'):
                f.launcher.read_file(f.root / 'usage.md', UID)
        with mock.patch.object(f.launcher.os, 'geteuid', return_value=UID + 1):
            with self.assertRaises(f.launcher.PackageError):
                f.launcher.verify(f.root, f.pin, BASE, UID, located=False)
        self.assertFalse(f.target.exists())

    def test_P05_all_databases_fail_closed_and_partial_init(self):
        f = Fixture().ready(); state = f.root / 'state'
        pristine = {p.name: p.read_bytes() for p in state.glob('*.sqlite')}
        for name, raw in pristine.items():
            p = state / name
            for corruption in ('missing', 'empty', 'bad-schema', 'application-id', 'version', 'extra-table'):
                p.write_bytes(raw)
                if corruption == 'missing': p.unlink()
                elif corruption == 'empty': p.write_bytes(b'')
                else:
                    with contextlib.closing(sqlite3.connect(p)) as db:
                        if corruption == 'application-id': db.execute('pragma application_id=123')
                        elif corruption == 'version': db.execute('pragma user_version=2')
                        elif corruption == 'bad-schema':
                            table = db.execute("select name from sqlite_master where type='table'").fetchone()[0]
                            db.execute('drop table ' + table)
                        else: db.execute('create table extra(x)')
                        db.commit()
                before = f.dbhashes()
                for action in ('verify', 'run', 'status'):
                    self.assertEqual(f.cli(action)[0], 2, (name, corruption, action))
                    self.assertEqual(f.dbhashes(), before)
                p.write_bytes(raw); p.chmod(0o600)
        g = Fixture(); self.assertEqual(g.cli('stage')[0], 0); g.root = g.target
        manifest, config = g.verified()
        with isolated_imports():
            imports = g.launcher.modules(g.root)
            with mock.patch.object(imports['codex_unattended_submission_binding'], 'initialize_submission_bindings', side_effect=RuntimeError('injected')):
                with self.assertRaisesRegex(g.launcher.PackageError, 'INIT_FAILED_PARTIAL_STATE_PRESERVED'):
                    g.launcher.init_new(g.root, config, UID)
        self.assertTrue((g.root / 'state/init-failed.json').exists())
        before = g.dbhashes()
        self.assertEqual(g.cli('init-new')[1]['reason'], 'STATE_ALREADY_EXISTS')
        self.assertEqual(g.cli('verify')[0], 2)
        self.assertEqual(before, g.dbhashes())

    def test_P06_pause_schedule_and_unknown_no_collection(self):
        f = Fixture().ready(); manifest, config = f.verified(); before = f.dbhashes()
        for name in config['pause_paths']:
            pause = Path(name); pause.write_bytes(b''); pause.chmod(0o600)
            self.assertEqual(f.cli('run')[1]['reason'], 'PAUSED')
            self.assertEqual(f.dbhashes(), before)
            self.assertFalse((f.root / 'state/assignment.json').exists())
            self.assertFalse((f.root / 'state/admissions.json').exists())
            pause.unlink()
        for schedule in ({'invalid': True}, {'version': 2, 'default': {'enabled': False, 'start': '23:00', 'end': '07:00', 'max_starts': 3}, 'dates': {}}):
            changed = dict(config); changed['schedule'] = schedule
            f.repin('runtime.json', f.builder.canonical(changed))
            self.assertEqual(f.cli('run')[0], 2)
            self.assertEqual(f.dbhashes(), before)
            self.assertFalse((f.root / 'state/assignment.json').exists())
        f.repin('runtime.json', f.builder.canonical(config)); manifest, config = f.verified()
        state = f.root / 'state'
        admissions = state / 'admissions.json'
        admissions.write_bytes(f.builder.canonical({'clock_floor_ms': 9999999999999, 'starts': []})); admissions.chmod(0o600)
        self.assertEqual(f.cli('run')[1]['reason'], 'CLOCK_OR_ADMISSIONS_INVALID'); admissions.unlink()
        import time
        admissions.write_bytes(f.builder.canonical({'clock_floor_ms': 0, 'starts': [int(time.time()*1000)]*3})); admissions.chmod(0o600)
        self.assertEqual(f.cli('run')[1]['reason'], 'START_CAP_REACHED'); admissions.unlink()
        job = config['fixture_jobs'][0]
        with contextlib.closing(sqlite3.connect(state / 'fixture-ledger.sqlite')) as db:
            db.execute('insert into fixture_jobs(job_id,assignment,assignment_sha256,revision) values(?,?,?,0)', (job['assignment']['job_id'], f.builder.canonical(job['assignment']), job['sha256'])); db.commit()
        with contextlib.closing(sqlite3.connect(state / 'fixture-controller.sqlite')) as db:
            db.execute("insert into fixture_controller_jobs(job_id,assignment_sha256,state,preparation_reason) values(?,?,'CLAIMED',NULL)", (job['assignment']['job_id'], job['sha256'])); db.commit()
        before = f.dbhashes()
        self.assertEqual(f.cli('run')[0], 3)
        self.assertEqual(f.cli('run')[1]['jobs'][0]['reason'], 'OUTCOME_UNKNOWN')
        self.assertEqual(f.dbhashes(), before)
        self.assertFalse((state / 'assignment.json').exists())
        self.assertFalse((state / 'admissions.json').exists())

    def test_P07_atomic_stage_runbook_and_preserving_rollback(self):
        f = Fixture()
        commands = (f.output / 'COMMANDS.md').read_text().split('```sh\n')[1].split('```')[0]
        result = subprocess.run(['/bin/sh', '-c', commands], capture_output=True, timeout=20, cwd='/')
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        f.root = f.target
        self.assertEqual(f.cli('status')[0], 0)
        before = f.dbhashes()
        self.assertEqual(f.cli('stage', root=f.bundle)[0], 2)
        self.assertEqual(f.dbhashes(), before)
        retired = f.target.with_name(f.target.name + '.retired')
        retired.mkdir(mode=0o700)
        self.assertEqual(f.cli('rollback', extra=['--rollback-to', str(retired)])[0], 2)
        self.assertEqual(f.dbhashes(), before); retired.rmdir()
        with f.launcher.lock(f.root / 'state', UID):
            self.assertEqual(f.cli('rollback', extra=['--rollback-to', str(retired)])[1]['reason'], 'ALREADY_RUNNING')
        self.assertEqual(f.cli('rollback', extra=['--rollback-to', str(retired)])[0], 0)
        f.root = retired; self.assertEqual(f.dbhashes(), before)
        self.assertFalse(f.target.exists())
        g = Fixture(); m, cfg = g.verified()
        original_open = Path.open
        copies = []
        def interrupted_copy(path, *args, **kwargs):
            if args and args[0] == 'xb' and '.staging-' in str(path):
                copies.append(str(path))
                if len(copies) == 2:
                    raise OSError('injected second file copy failure')
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', interrupted_copy):
            with self.assertRaises(OSError): g.launcher.stage(g.root, cfg, g.pin, BASE, UID)
        self.assertEqual(len(copies), 2)
        self.assertFalse(g.target.exists()); self.assertTrue(list(g.base.glob('.candidate.staging-*')))
        cfg['target_base'] = str(g.root / 'nested')
        with self.assertRaisesRegex(g.launcher.PackageError, 'STAGING_PATH_OVERLAP'):
            g.launcher.stage(g.root, cfg, g.pin, BASE, UID)
        # External launcher hash failure must stop the documented shell block.
        h = Fixture(); original = (h.bundle / 'installation/launch.py').read_bytes()
        (h.bundle / 'installation/launch.py').write_bytes(b'#tamper\n' + original)
        shell = (h.output / 'COMMANDS.md').read_text().split('```sh\n')[1].split('```')[0]
        failure = subprocess.run(['/bin/sh', '-c', shell], capture_output=True, timeout=8, cwd='/')
        self.assertNotEqual(failure.returncode, 0); self.assertFalse(h.target.exists())
        OBSERVATIONS.append({'runbook_executed': True, 'rollback_db_preserved': True, 'bootstrap_failure_stopped': True})


if __name__ == '__main__':
    selected = sys.argv[1:] or ['PackageAcceptance']
    suite = unittest.defaultTestLoader.loadTestsFromNames(selected, sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2, failfast=True).run(suite)
    report_root = Path(tempfile.mkdtemp(prefix='agent-execution-results-', dir='/tmp'))
    report_root.chmod(0o700)
    record = report_root / 'normal.json'
    with record.open('x') as stream:
        json.dump({'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'observations': OBSERVATIONS, 'native_rpc': 0}, stream, indent=2)
    record.chmod(0o600)
    print('RESULT_FILE=' + str(record), flush=True)
    raise SystemExit(not result.wasSuccessful())
