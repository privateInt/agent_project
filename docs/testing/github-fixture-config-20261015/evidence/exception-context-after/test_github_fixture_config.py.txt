"""L01 approved tests; fresh protected install per method, never old suites."""

import ast
from contextlib import contextmanager, ExitStack, redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock


ROOT = Path(os.environ.get('FIXTURE_CONFIG_TEST_ROOT', tempfile.gettempdir()))
SOURCE = Path(__file__).resolve().parents[4]
FILES = ('src/__init__.py', 'src/ops/__init__.py',
         'src/ops/tools/github_check_audit.py', 'src/ops/tools/github_fixture_collector.py',
         'src/ops/tools/github_fixture_ledger.py', 'src/ops/tools/github_fixture_controller.py',
         'src/ops/tools/github_fixture_config.py')
TRUST = ('input_origin_verified', 'signer_roster_origin_verified', 'issuer_environment_verified',
         'runtime_isolation_verified', 'independent_review_verified', 'trusted_gate_verified',
         'binding_verified', 'native_ready', 'merge_authorized')
VERSION = 'L01-fixture-v1'
OBSERVATIONS = []
HARNESS_RESULTS = []


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def assignment(profile='ok', job='loader-job:1'):
    return dict(schema=1, mode='fixture', job_id=job,
                target=dict(repository='ExampleOrg/fixture', pr_number=1, base_sha='a'*40,
                            head_sha='b'*40, release_sha256='c'*64, plan_sha256='d'*64),
                fixture_profile=profile, timeout_ms=1000, max_stream_bytes=65536)


def tree(root):
    found = {}
    for path in sorted(root.rglob('*')):
        s = path.lstat()
        item = [s.st_mode, s.st_uid, s.st_dev, s.st_ino, s.st_nlink]
        if stat.S_ISREG(s.st_mode):
            item.append(digest(path.read_bytes()))
        elif stat.S_ISLNK(s.st_mode):
            item.append(os.readlink(path))
        found[str(path.relative_to(root))] = item
    return found


@contextmanager
def altered(path, *, raw=None, mode=None, absent=False, symlink=None, fifo=False):
    existed = path.exists()
    saved = path.read_bytes() if existed and path.is_file() else None
    old_mode = stat.S_IMODE(path.stat().st_mode) if existed else None
    if absent or symlink is not None or fifo:
        path.unlink(missing_ok=True)
    if symlink is not None:
        path.symlink_to(symlink)
    elif fifo:
        os.mkfifo(path, 0o600)
    elif raw is not None:
        path.write_bytes(raw)
    if mode is not None:
        path.chmod(mode)
    try:
        yield
    finally:
        if symlink is not None or fifo or absent or (raw is not None and not existed):
            path.unlink(missing_ok=True)
        if saved is not None:
            path.write_bytes(saved)
        if old_mode is not None and path.exists():
            path.chmod(old_mode)


class Harness:
    def __init__(self, spec):
        self.spec = spec
        self.base = Path(spec['base'])
        self.install = self.base/'install'
        self.state = self.base/'state'
        self.ledger_path = self.state/'ledger.sqlite'
        self.control_path = self.state/'control.sqlite'
        sys.path.insert(0, str(self.install))
        from src.ops.tools import github_fixture_config as config
        from src.ops.tools import github_fixture_controller as controller
        from src.ops.tools import github_fixture_collector as collector
        from src.ops.tools import github_fixture_ledger as ledger
        self.q, self.c, self.f, self.l = config, controller, collector, ledger
        self.a = unittest.TestCase()
        self.calls = dict(connect=0, run=0, read=0, collect=0, child=0, initialize=0)
        self.connections, self.opened, self.sql, self.checks, self.observations = [], [], [], [], []
        self.original_connect = sqlite3.connect
        self.setup_initializers = (self.l.initialize_fixture_ledger,self.c.initialize_fixture_controller)
        self.db_setup(self.ledger_path, self.control_path)
        self.cfg = dict(schema=1, installation_root=str(self.install), state_dir=str(self.state),
                        ledger_db=str(self.ledger_path), control_db=str(self.control_path))
        self.expected = dict(expected_code_version=VERSION,
                             expected_source_sha256=dict(spec['pins']), expected_uid=os.geteuid())

    def db_setup(self, lp, cp):
        for path, init, expected in ((lp,self.setup_initializers[0],'LEDGER_READY'),
                                    (cp,self.setup_initializers[1],'CONTROLLER_READY')):
            con = self.original_connect(str(path), isolation_level=None)
            try:
                con.execute('PRAGMA synchronous=FULL')
                con.execute('PRAGMA busy_timeout=1000')
                self.a.assertEqual(init(con)['report']['reason'],expected)
            finally:
                con.close()
            path.chmod(0o600)

    def check(self, label, fn):
        try:
            fn()
        except AssertionError as error:
            self.checks.append(dict(label=label,status='FAIL',error=str(error),traceback=traceback.format_exc()))
        except Exception as error:
            self.checks.append(dict(label=label,status='ERROR',error=type(error).__name__+': '+str(error),
                                    traceback=traceback.format_exc()))
        else:
            self.checks.append(dict(label=label,status='PASS'))

    def load(self, cfg=None, raw=None, **overrides):
        args = dict(self.expected)
        args.update(overrides)
        return self.q.load_fixture_controller(canonical(self.cfg if cfg is None else cfg)
                                             if raw is None else raw, **args)

    def reject(self, action, reason, *, no_connect=False):
        before, calls = tree(self.base), dict(self.calls)
        try:
            handle = action()
        except self.q.FixtureConfigurationError as error:
            self.a.assertEqual(error.reason, reason)
            self.a.assertEqual(str(error), reason)
        else:
            handle.close()
            self.a.fail('expected '+reason+' rejection')
        self.a.assertEqual(tree(self.base), before, 'rejection must preserve bytes/inodes/modes/files')
        for key in ('run','read','collect','child','initialize'):
            self.a.assertEqual(self.calls[key],calls[key],key+' must not occur on rejection')
        if no_connect:
            self.a.assertEqual(self.calls['connect'],calls['connect'])

    @contextmanager
    def observed(self):
        with ExitStack() as stack:
            def counted(key, fn):
                def wrapper(*args, **kwargs):
                    self.calls[key] += 1
                    result = fn(*args,**kwargs)
                    if key == 'connect':
                        self.connections.append(result)
                        result.set_trace_callback(self.sql.append)
                    return result
                return wrapper
            for obj,name,key in ((sqlite3,'connect','connect'),
                                 (self.c,'run_fixture_job','run'),
                                 (self.c,'read_fixture_controller_job','read'),
                                 (self.f,'collect_fixture_assignment','collect'),
                                 (self.f.subprocess,'Popen','child'),
                                 (self.l,'initialize_fixture_ledger','initialize'),
                                 (self.c,'initialize_fixture_controller','initialize')):
                stack.enter_context(mock.patch.object(obj,name,counted(key,getattr(obj,name))))
            real_open = os.open
            def opened(*args,**kwargs):
                fd = real_open(*args,**kwargs)
                self.opened.append(fd)
                return fd
            stack.enter_context(mock.patch.object(os,'open',opened))
            real_dup = os.dup
            def duplicated(fd):
                result=real_dup(fd);self.opened.append(result);return result
            stack.enter_context(mock.patch.object(os,'dup',duplicated))
            yield

    def assert_closed(self, connections=None, fds=None):
        for con in self.connections if connections is None else connections:
            with self.a.assertRaises(sqlite3.ProgrammingError):
                con.execute('SELECT 1')
        for fd in set(self.opened if fds is None else fds):
            with self.a.assertRaises(OSError):
                os.fstat(fd)

    def prepare_assignment(self, profile='ok', job='loader-job:1'):
        value = assignment(profile,job)
        raw = canonical(value)
        path = self.state/'assignment.json'
        path.write_bytes(raw)
        path.chmod(0o600)
        return value, raw, digest(raw)

    def owner_rejection(self, path, reason):
        target=path.stat();real=os.fstat
        def foreign(fd):
            result=real(fd)
            if (result.st_dev,result.st_ino)==(target.st_dev,target.st_ino):
                values=list(result);values[4]=os.geteuid()+77
                return os.stat_result(values)
            return result
        with mock.patch.object(os,'fstat',foreign):
            self.reject(self.load,reason,no_connect=True)
        self.observations.append(dict(case='ownership',target=str(path.relative_to(self.base)),
                                      metadata_injected=True,actual_chown=False))

    def row(self, path, table):
        con = self.original_connect(str(path),isolation_level=None)
        try:
            return con.execute('SELECT * FROM '+table).fetchall()
        finally:
            con.close()

    def bad_cfg(self):
        samples = [b'',b' '*16385,b'\xff',b'\xef\xbb\xbf{}',b'[]',b'{"schema":1,"schema":1}',
                   None,[],{},True,1,'{}']
        for i,raw in enumerate(samples):
            self.check('config-raw-'+str(i),lambda raw=raw:self.reject(
                lambda:self.q.load_fixture_controller(raw,**self.expected),'CONFIGURATION_INVALID',no_connect=True))
        variants=[dict(self.cfg,schema=v) for v in [True,1.0,2,None,float('nan'),float('inf')]]
        variants += [{k:v for k,v in self.cfg.items() if k!='ledger_db'},dict(self.cfg,unknown=1)]
        for key in ('installation_root','state_dir','ledger_db','control_db'):
            variants.extend(dict(self.cfg,**{key:v}) for v in [None,1,[],True])
        for i,cfg in enumerate(variants):
            self.check('config-shape-'+str(i),lambda cfg=cfg:self.reject(lambda:self.load(cfg),
                                                                      'CONFIGURATION_INVALID',no_connect=True))
        def limit():
            raw=canonical(self.cfg)
            with self.load(raw=raw+b' '*(16384-len(raw))) as handle:
                self.a.assertEqual(handle.report['reason'],'CONFIGURATION_VERIFIED')
        self.check('valid-config-at-limit',limit)

    def expectations(self):
        for key, values in [('expected_uid',[None,True,-1,os.geteuid()+1,'1011']),
                            ('expected_code_version',[None,True,'','x'*129,'has space','é']),
                            ('expected_source_sha256',[None,[],{},True])]:
            for i,value in enumerate(values):
                self.check(key+'-'+str(i),lambda key=key,value=value:self.reject(
                    lambda:self.load(**{key:value}),'EXPECTATION_INVALID',no_connect=True))
        for key in self.expected:
            def missing(key=key):
                kwargs={k:v for k,v in self.expected.items() if k!=key}
                self.reject(lambda:self.q.load_fixture_controller(canonical(self.cfg),**kwargs),
                            'EXPECTATION_INVALID',no_connect=True)
            self.check('missing-'+key,missing)
        audit=FILES[2]
        mappings=[{k:v for k,v in self.expected['expected_source_sha256'].items() if k!=audit},
                  dict(self.expected['expected_source_sha256'],**{'src/ops/tools/__init__.py':'a'*64})]
        mappings += [dict(self.expected['expected_source_sha256'],**{audit:v})
                     for v in ['A'*64,'z'*64,'a'*63,'a'*65,1,True,None]]
        for i,mapping in enumerate(mappings):
            self.check('mapping-'+str(i),lambda mapping=mapping:self.reject(
                lambda:self.load(expected_source_sha256=mapping),'EXPECTATION_INVALID',no_connect=True))

    def marker(self):
        p=self.install/'fixture-controller-installation.json'
        for i,raw in enumerate([b'',b' '*2049,b'[]',b'{"schema":1,"schema":1}',
                                canonical(dict(schema=True,code_version=VERSION)),
                                canonical(dict(schema=1,code_version=VERSION,extra=1)),
                                canonical(dict(schema=1,code_version=1))]):
            def case(raw=raw):
                with altered(p,raw=raw):self.reject(self.load,'INSTALLATION_INVALID',no_connect=True)
            self.check('marker-bytes-'+str(i),case)
        for name,kwargs in [('missing',dict(absent=True)),('writable',dict(mode=0o666)),
                            ('symlink',dict(symlink=self.install/FILES[0])),('fifo',dict(fifo=True))]:
            def case(kwargs=kwargs):
                with altered(p,**kwargs):self.reject(self.load,'INSTALLATION_INVALID',no_connect=True)
            self.check('marker-'+name,case)
        def version():
            with altered(p,raw=canonical(dict(schema=1,code_version='other'))):
                self.reject(self.load,'VERSION_MISMATCH',no_connect=True)
        self.check('version-mismatch',version)
        self.check('marker-owner',lambda:self.owner_rejection(p,'INSTALLATION_INVALID'))

    def source_hash(self, only_audit=False):
        for name in [FILES[2]] if only_audit else [name for name in FILES if name!=FILES[2]]:
            def case(name=name):
                mapping=dict(self.expected['expected_source_sha256'])
                mapping[name]=('0' if mapping[name][0]!='0' else '1')+mapping[name][1:]
                self.reject(lambda:self.load(expected_source_sha256=mapping),'SOURCE_HASH_MISMATCH',no_connect=True)
            self.check('wrong-external-pin-'+name,case)
        if only_audit:return
        p=self.install/FILES[2]
        for name,kwargs in [('missing',dict(absent=True)),('writable',dict(mode=0o666)),('fifo',dict(fifo=True))]:
            def case(kwargs=kwargs):
                with altered(p,**kwargs):self.reject(self.load,'INSTALLATION_INVALID',no_connect=True)
            self.check('source-'+name,case)
        self.check('source-owner',lambda:self.owner_rejection(p,'INSTALLATION_INVALID'))

    def origins(self):
        import src, src.ops, src.ops.tools
        import src.ops.tools.github_check_audit as audit
        modules=[src,src.ops,audit,self.f,self.l,self.c,self.q]
        other=self.base/'other';shutil.copytree(self.install,other)
        self.check('same-bytes-other-root',lambda:self.reject(lambda:self.load(
            dict(self.cfg,installation_root=str(other))),'INSTALLATION_MISMATCH',no_connect=True))
        for module in modules:
            for field in ('file','origin'):
                def case(module=module,field=field):
                    obj,attr=(module,'__file__') if field=='file' else (module.__spec__,'origin')
                    with mock.patch.object(obj,attr,str(other/FILES[0])):
                        self.reject(self.load,'INSTALLATION_MISMATCH',no_connect=True)
                self.check(module.__name__+'-'+field,case)
        init=self.install/'src/ops/tools/__init__.py'
        def initializer():
            with altered(init,raw=b''):
                self.reject(self.load,'INSTALLATION_MISMATCH',no_connect=True)
        self.check('namespace-initializer',initializer)
        for field in ('__path__','spec'):
            def case(field=field):
                obj,attr=(src.ops.tools,'__path__') if field=='__path__' else (src.ops.tools.__spec__,'submodule_search_locations')
                with mock.patch.object(obj,attr,[str(self.install/'src/ops/tools'),str(other/'src/ops/tools')]):
                    self.reject(self.load,'INSTALLATION_MISMATCH',no_connect=True)
            self.check('namespace-'+field,case)

    def paths(self):
        for field in ('installation_root','state_dir','ledger_db','control_db'):
            good=self.cfg[field]
            for label,bad in [('relative','relative/path'),('dot',good+'/.'),('dotdot',good+'/../x'),
                              ('slash',good.replace('/tmp/','/tmp//',1)),('trailing',good+'/'),
                              ('nul',good+'\0'),('uri','file:'+good+'?mode=rw')]:
                self.check(field+'-'+label,lambda field=field,bad=bad:self.reject(
                    lambda:self.load(dict(self.cfg,**{field:bad})),'CONFIGURATION_INVALID',no_connect=True))
        for mode in (0o777,0o1777):
            def writable(mode=mode):
                before=stat.S_IMODE(self.base.stat().st_mode)
                self.base.chmod(mode)
                try:self.reject(self.load,'INSTALLATION_INVALID',no_connect=True)
                finally:self.base.chmod(before)
            self.check('unsafe-owned-ancestor-'+oct(mode),writable)
        for field,path,reason in [('installation_root',self.install,'INSTALLATION_INVALID'),
                                  ('state_dir',self.state,'STATE_DIRECTORY_INVALID')]:
            def link(field=field,path=path,reason=reason):
                real=path.with_name(path.name+'-real');path.rename(real);path.symlink_to(real,target_is_directory=True)
                try:self.reject(self.load,reason,no_connect=True)
                finally:path.unlink();real.rename(path)
            self.check('nofollow-'+field,link)
        def middle_link():
            p=self.install/'src/ops';backup=p.with_name('ops-held');p.rename(backup);p.symlink_to(backup)
            try:self.reject(self.load,'INSTALLATION_INVALID',no_connect=True)
            finally:p.unlink();backup.rename(p)
        self.check('nofollow-middle-component',middle_link)
        self.check('real-sticky-namespace-root',lambda:self.load().close())
        self.observations.append(dict(case='L06',euid=os.geteuid(),root_uid=os.stat('/').st_uid,tmp_uid=os.stat('/tmp').st_uid))

    def state_mode(self, only_755=False):
        for mode in [0o755] if only_755 else [0o770,0o1700,0o2700]:
            def case(mode=mode):
                old=stat.S_IMODE(self.state.stat().st_mode);self.state.chmod(mode)
                try:self.reject(self.load,'STATE_DIRECTORY_INVALID',no_connect=True)
                finally:self.state.chmod(old)
            self.check('state-mode-'+oct(mode),case)
        if only_755:return
        target=self.state.stat()
        real=os.fstat
        def foreign(fd):
            result=real(fd)
            if (result.st_dev,result.st_ino)==(target.st_dev,target.st_ino):
                values=list(result);values[4]=os.geteuid()+77
                return os.stat_result(values)
            return result
        def owner():
            with mock.patch.object(os,'fstat',foreign):
                self.reject(self.load,'STATE_DIRECTORY_INVALID',no_connect=True)
        self.check('state-owner-metadata-injection',owner)
        for kind in ('file','fifo','symlink'):
            def invalid_type(kind=kind):
                backup=self.base/'state-held';self.state.rename(backup)
                if kind=='file':self.state.write_bytes(b'fixture')
                elif kind=='fifo':os.mkfifo(self.state,0o600)
                else:self.state.symlink_to(backup,target_is_directory=True)
                try:self.reject(self.load,'STATE_DIRECTORY_INVALID',no_connect=True)
                finally:self.state.unlink();backup.rename(self.state)
            self.check('state-type-'+kind,invalid_type)

    def db_paths(self):
        outside=self.base/'outside.sqlite';shutil.copy2(self.ledger_path,outside)
        nested=self.state/'nested';nested.mkdir(mode=0o700)
        deep=nested/'deep.sqlite';shutil.copy2(self.ledger_path,deep)
        for label,cfg in [('same-path',dict(self.cfg,control_db=str(self.ledger_path))),
                          ('outside',dict(self.cfg,ledger_db=str(outside))),
                          ('nested',dict(self.cfg,ledger_db=str(deep)))]:
            self.check(label,lambda cfg=cfg:self.reject(lambda:self.load(cfg),'DATABASE_PATH_INVALID',no_connect=True))
        for path in (self.ledger_path,self.control_path):
            for name,kwargs in [('missing',dict(absent=True)),('writable',dict(mode=0o666)),
                                ('readable',dict(mode=0o644)),('fifo',dict(fifo=True)),('symlink',dict(symlink=outside))]:
                def case(path=path,kwargs=kwargs):
                    with altered(path,**kwargs):self.reject(self.load,'DATABASE_PATH_INVALID',no_connect=True)
                self.check(path.name+'-'+name,case)
            def hardlink(path=path):
                alias=self.state/'alias.sqlite';os.link(path,alias)
                try:self.reject(self.load,'DATABASE_PATH_INVALID',no_connect=True)
                finally:alias.unlink()
            self.check(path.name+'-nlink2',hardlink)
            self.check(path.name+'-owner',lambda path=path:self.owner_rejection(path,'DATABASE_PATH_INVALID'))
        saved=self.control_path.read_bytes()
        def inode_alias():
            self.control_path.unlink();os.link(self.ledger_path,self.control_path)
            try:self.reject(self.load,'DATABASE_PATH_INVALID',no_connect=True)
            finally:self.control_path.unlink();self.control_path.write_bytes(saved);self.control_path.chmod(0o600)
        self.check('different-names-same-inode',inode_alias)

    def sidecars(self):
        for path in (self.ledger_path,self.control_path):
            for suffix in ('-journal','-wal','-shm','-mjL01'):
                for link in (False,True):
                    def case(path=path,suffix=suffix,link=link):
                        p=Path(str(path)+suffix)
                        with altered(p,symlink=self.ledger_path) if link else altered(p,raw=b''):
                            self.reject(self.load,'DATABASE_PATH_INVALID',no_connect=True)
                    self.check(path.name+suffix+('-link' if link else '-empty'),case)

    def schemas(self):
        for path in (self.ledger_path,self.control_path):
            original=path.read_bytes()
            for label,raw in [('empty',b''),('not-sqlite',b'x'*512),('truncated',original[:99]),
                              ('write-format',original[:18]+b'\x02'+original[19:]),
                              ('read-format',original[:19]+b'\x02'+original[20:])]:
                def case(path=path,raw=raw):
                    with altered(path,raw=raw):self.reject(self.load,'DATABASE_SCHEMA_INVALID',no_connect=True)
                self.check(path.name+'-'+label,case)
            table='fixture_jobs' if path==self.ledger_path else 'fixture_controller_jobs'
            for sql in ('PRAGMA application_id=1','PRAGMA user_version=2','CREATE TABLE extra(x)',
                        'ALTER TABLE '+table+' ADD COLUMN extra'):
                def case(path=path,sql=sql):
                    con=self.original_connect(str(path),isolation_level=None)
                    try:con.execute(sql)
                    finally:con.close()
                    try:self.reject(self.load,'DATABASE_SCHEMA_INVALID')
                    finally:path.write_bytes(original)
                self.check(path.name+'-'+sql,case)
            def wal(path=path):
                con=self.original_connect(str(path),isolation_level=None)
                try:self.a.assertEqual(con.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
                finally:con.close()
                self.a.assertFalse(any(Path(str(path)+s).exists() for s in ('-wal','-shm','-journal')))
                self.a.assertEqual(path.read_bytes()[18:20],b'\x02\x02')
                try:self.reject(self.load,'DATABASE_SCHEMA_INVALID',no_connect=True)
                finally:path.write_bytes(original)
            self.check(path.name+'-closed-WAL-without-sidecar',wal)
        def swapped():
            left,right=self.ledger_path.read_bytes(),self.control_path.read_bytes()
            self.ledger_path.write_bytes(right);self.control_path.write_bytes(left)
            try:self.reject(self.load,'DATABASE_SCHEMA_INVALID')
            finally:self.ledger_path.write_bytes(left);self.control_path.write_bytes(right)
        self.check('swapped-schema-roles',swapped)

    def connection_policy(self):
        before=tree(self.base)
        with self.load() as handle:
            self.a.assertEqual(self.calls['connect'],2)
            for con in self.connections:
                self.a.assertIs(type(con),sqlite3.Connection)
                self.a.assertIsNone(con.isolation_level)
                self.a.assertEqual(con.execute('PRAGMA synchronous').fetchone()[0],2)
                busy=con.execute('PRAGMA busy_timeout').fetchone()[0]
                self.a.assertTrue(type(busy) is int and 0<=busy<=1000)
            self.a.assertEqual(self.calls['run'],0);self.a.assertEqual(self.calls['read'],0)
            self.a.assertEqual(self.calls['initialize'],0)
        self.a.assertEqual(tree(self.base),before)
        self.assert_closed()
        for key,path in [('ledger_db',self.ledger_path),('control_db',self.control_path)]:
            special=path.with_name('literal?mode=ro#%'+path.name);path.rename(special)
            try:
                with mock.patch.object(sqlite3,'connect',wraps=sqlite3.connect) as connected:
                    self.load(dict(self.cfg,**{key:str(special)})).close()
                    for call in connected.call_args_list:
                        self.a.assertIs(call.kwargs['uri'],True)
                        self.a.assertIsNone(call.kwargs['isolation_level'])
                        self.a.assertIn('mode=rw',call.args[0])
                        self.a.assertNotIn('literal?mode=',call.args[0])
            finally:special.rename(path)
        self.observations.append(dict(case='L11',actual_builtin_sqlite=True,connection_options_observed=True))

    def resources(self):
        caller=os.open(self.state,os.O_RDONLY|os.O_DIRECTORY)
        self.opened.remove(caller)
        try:
            h=self.load();h.close();h.close();self.assert_closed()
            self.reject(lambda:h.run('a'*64,b'{}'),'HANDLE_CLOSED')
            self.reject(lambda:h.read('loader-job:1','a'*64),'HANDLE_CLOSED')
            self.a.assertTrue(stat.S_ISDIR(os.fstat(caller).st_mode))
        finally:os.close(caller)
        baseline_connect=sqlite3.connect
        for fail_index in (1,2):
            def case(fail_index=fail_index):
                count=[0]
                def fault(*args,**kwargs):
                    count[0]+=1
                    if count[0]==fail_index:raise sqlite3.OperationalError('PRIVATE_L01_EXCEPTION')
                    return baseline_connect(*args,**kwargs)
                with mock.patch.object(sqlite3,'connect',fault):
                    self.reject(self.load,'DATABASE_UNAVAILABLE')
                self.assert_closed()
            self.check('connect-failure-'+str(fail_index),case)

    def real_collection(self, profile):
        value,raw,pin=self.prepare_assignment(profile)
        with self.load() as handle:
            got=handle.run(pin,raw)
            self.a.assertEqual(got['report']['reason'],'ARTIFACT_STORED')
            artifact=got['artifact'];parsed=json.loads(artifact)
            self.a.assertEqual(parsed['outcome']['returncode'],0 if profile=='ok' else 7)
            self.a.assertEqual(parsed['assignment_sha256'],pin)
            self.a.assertEqual(parsed['target'],value['target'])
            self.a.assertEqual(parsed['job_id'],value['job_id'])
            self.a.assertEqual(got['record']['artifact_sha256'],digest(artifact))
            self.a.assertEqual(got['record']['artifact_bytes'],artifact)
            self.a.assertEqual(got['record']['binding'],dict(value['target'],evidence_sha256=digest(artifact)))
            first=handle.read(value['job_id'],pin)
            self.a.assertEqual(first['artifact'],artifact)
        with self.load() as handle:
            reopened=handle.read(value['job_id'],pin)
            self.a.assertEqual(reopened['artifact'],artifact)
            self.a.assertEqual(reopened['record'],got['record'])
        self.a.assertEqual(self.calls['child'],1)
        self.a.assertEqual(self.row(self.ledger_path,'fixture_jobs')[0][3],1)
        self.a.assertEqual(self.row(self.control_path,'fixture_controller_jobs')[0][2],'CLAIMED')
        self.observations.append(dict(case='L13',profile=profile,actual_child_count=self.calls['child'],
            returncode=parsed['outcome']['returncode'],artifact_sha256=digest(artifact),
            exact_bytes_reopened=True,binding=reopened['record']['binding']))

    def preparation(self):
        value,raw,pin=self.prepare_assignment();(self.state/'assignment.json').unlink()
        with self.load() as h:
            first=h.run(pin,raw)
            self.a.assertEqual(first['report']['reason'],'PREPARATION_FAILED')
            self.a.assertIsNone(first['artifact'])
        self.prepare_assignment()
        with self.load() as h:
            later=h.read(value['job_id'],pin);repeat=h.run(pin,raw)
            self.a.assertEqual(later['report']['reason'],'PREPARATION_FAILED')
            self.a.assertEqual(repeat['report']['preparation_reason'],first['report']['preparation_reason'])
            self.a.assertIsNone(repeat['artifact'])
        self.a.assertEqual(self.calls['collect'],1);self.a.assertEqual(self.calls['child'],0)
        self.a.assertEqual(self.row(self.ledger_path,'fixture_jobs')[0][3],0)
        self.a.assertEqual(self.row(self.control_path,'fixture_controller_jobs')[0][2],'PREPARATION_FAILED')
        self.observations.append(dict(case='L14',actual_missing_assignment=True,child_count=0,
                                      reason=first['report']['preparation_reason'],terminal_reopened=True))

    def unknown_spawn(self):
        value,raw,pin=self.prepare_assignment()
        injected=dict(report=self.f._report('SPAWN_FAILED',verified=True),artifact=None,binding=None)
        with mock.patch.object(self.f,'collect_fixture_assignment',return_value=injected) as stub:
            with self.load() as h:
                got=h.run(pin,raw)
        with self.load() as h:later=h.read(value['job_id'],pin)
        self.a.assertEqual(got['report']['reason'],'OUTCOME_UNKNOWN')
        self.a.assertEqual(later['report']['reason'],'OUTCOME_UNKNOWN')
        self.a.assertIsNone(later['artifact'])
        self.a.assertEqual(self.calls['child'],0)
        self.a.assertEqual(self.row(self.control_path,'fixture_controller_jobs')[0][2],'CLAIMED')
        self.observations.append(dict(case='L15',injected_collector_reason='SPAWN_FAILED',
                                      injected_collector_calls=stub.call_count,actual_child_count=0))

    def unknown_commit(self):
        value,raw,pin=self.prepare_assignment()
        recorded=[False];faults=[0]
        original_record=self.l.record_fixture_artifact;original_commit=self.c._commit
        def record(*args,**kwargs):
            result=original_record(*args,**kwargs)
            self.a.assertEqual(result['report']['reason'],'ARTIFACT_RECORDED')
            recorded[0]=True
            return result
        def lose(connection):
            original_commit(connection)
            if recorded[0] and not faults[0]:
                faults[0]+=1
                raise sqlite3.OperationalError('PRIVATE_L01_AFTER_REAL_COMMIT')
        with self.load() as h:
            with mock.patch.object(self.l,'record_fixture_artifact',record),mock.patch.object(self.c,'_commit',lose):
                got=h.run(pin,raw)
        self.a.assertTrue(recorded[0]);self.a.assertEqual(faults[0],1)
        self.a.assertEqual(got['report']['reason'],'DATABASE_UNCONFIRMED')
        self.a.assertIs(got['report']['artifact_storage_confirmed'],False)
        self.a.assertIs(type(got['artifact']),bytes)
        with self.load() as h:later=h.read(value['job_id'],pin)
        self.a.assertEqual(later['report']['reason'],'ARTIFACT_STORED')
        self.a.assertEqual(later['artifact'],got['artifact'])
        self.a.assertEqual(later['record']['artifact_sha256'],digest(got['artifact']))
        self.a.assertEqual(self.calls['child'],1)
        self.observations.append(dict(case='L15',injected_after_actual_record=True,actual_commit_then_response_loss=True,
                                      actual_child_count=1,artifact_sha256=digest(got['artifact']),reopened_exact=True))

    def forwarding(self):
        with self.load() as h:
            for reason,artifact in [('STATE_CONFLICT',None),('DATABASE_UNCONFIRMED',b'original-fixture-bytes')]:
                result=dict(report=dict(reason=reason),record=None,artifact=artifact)
                with mock.patch.object(self.c,'run_fixture_job',return_value=result) as run:
                    self.a.assertIs(h.run('a'*64,b'fixture-assignment'),result)
                    self.a.assertEqual(run.call_args.args[-2:],('a'*64,b'fixture-assignment'))
                with mock.patch.object(self.c,'read_fixture_controller_job',return_value=result) as read:
                    self.a.assertIs(h.read('job:1','a'*64),result)
                    self.a.assertEqual(read.call_args.args[-2:],('job:1','a'*64))
        other=self.base/'other-state';other.mkdir(mode=0o700)
        lp,cp=other/'ledger.sqlite',other/'control.sqlite';self.db_setup(lp,cp)
        first,second=assignment(),assignment('nonzero')
        for path,value in [(self.ledger_path,first),(lp,second)]:
            con=self.original_connect(str(path),isolation_level=None)
            try:
                con.execute('PRAGMA busy_timeout=1000');con.execute('PRAGMA synchronous=FULL')
                self.a.assertEqual(self.l.assign_fixture_job(con,digest(canonical(value)),canonical(value))['report']['reason'],'ASSIGNED')
            finally:con.close()
        cfg=dict(self.cfg,state_dir=str(other),ledger_db=str(lp),control_db=str(cp))
        with self.load() as a,self.load(cfg) as b:
            for h,value in [(a,first),(b,second)]:
                got=h.read(value['job_id'],digest(canonical(value)))
                self.a.assertEqual(got['record']['assignment_bytes'],canonical(value))
        self.a.assertEqual(self.calls['child'],0)
        self.observations.append(dict(case='L16',actual_two_state_routing=True,controller_boundary_stubs_separate=True))

    def live_changes(self):
        for op in ('run','read'):
            operations=[]
            for name in FILES:
                operations.append((name,'SOURCE_HASH_MISMATCH',self.install/name,'bytes'))
            operations += [('marker','VERSION_MISMATCH',self.install/'fixture-controller-installation.json','version'),
                           ('state','STATE_DIRECTORY_INVALID',self.state,'mode'),
                           ('ledger-mode','DATABASE_PATH_INVALID',self.ledger_path,'mode'),
                           ('ledger-inode','DATABASE_PATH_INVALID',self.ledger_path,'inode'),
                           ('control-inode','DATABASE_PATH_INVALID',self.control_path,'inode'),
                           ('state-inode','STATE_DIRECTORY_INVALID',self.state,'inode'),
                           ('install-inode','INSTALLATION_MISMATCH',self.install,'inode'),
                           ('header','DATABASE_SCHEMA_INVALID',self.control_path,'header'),
                           ('sidecar','DATABASE_PATH_INVALID',Path(str(self.ledger_path)+'-wal'),'sidecar')]
            for label,reason,path,kind in operations:
                def case(op=op,reason=reason,path=path,kind=kind):
                    h=self.load()
                    try:
                        with ExitStack() as stack:
                            if kind=='bytes':stack.enter_context(altered(path,raw=path.read_bytes()+b'\n# fixture change\n'))
                            elif kind=='version':stack.enter_context(altered(path,raw=canonical(dict(schema=1,code_version='other'))))
                            elif kind=='mode':stack.enter_context(altered(path,mode=0o755 if path==self.state else 0o644))
                            elif kind=='header':stack.enter_context(altered(path,raw=path.read_bytes()[:18]+b'\x02\x02'+path.read_bytes()[20:]))
                            elif kind=='sidecar':stack.enter_context(altered(path,raw=b''))
                            else:
                                backup=path.with_name(path.name+'-held');path.rename(backup)
                                if backup.is_dir():shutil.copytree(backup,path)
                                else:shutil.copy2(backup,path)
                                def restore():
                                    if path.is_dir():shutil.rmtree(path)
                                    else:path.unlink()
                                    backup.rename(path)
                                stack.callback(restore)
                            self.reject(lambda:getattr(h,op)('a'*64,b'{}') if op=='run'
                                        else h.read('loader-job:1','a'*64),reason)
                    finally:h.close()
                self.check(op+'-'+label,case)

    def races(self):
        original=sqlite3.connect
        for when in ('before','after'):
            def case(when=when):
                done=[False];backup=self.ledger_path.with_name('ledger-held.sqlite')
                def connect(*args,**kwargs):
                    if not done[0]:
                        done[0]=True
                        if when=='before':
                            self.ledger_path.rename(backup);shutil.copy2(backup,self.ledger_path)
                            return original(*args,**kwargs)
                        con=original(*args,**kwargs)
                        self.ledger_path.rename(backup);shutil.copy2(backup,self.ledger_path)
                        return con
                    return original(*args,**kwargs)
                before_calls=dict(self.calls)
                with mock.patch.object(sqlite3,'connect',connect):
                    try:
                        try:h=self.load()
                        except self.q.FixtureConfigurationError as error:self.a.assertEqual(error.reason,'DATABASE_PATH_INVALID')
                        else:h.close();self.a.fail('race must reject replaced inode')
                    finally:
                        if backup.exists():self.ledger_path.unlink();backup.rename(self.ledger_path)
                self.a.assertTrue(done[0]);self.assert_closed()
                for key in ('run','read','collect','child'):self.a.assertEqual(self.calls[key],before_calls[key])
                self.observations.append(dict(case='L18',rename_at_connect=when,actual_temp_rename=True,controller_calls=0))
            self.check('connect-window-'+when,case)
        for when in ('before','after'):
            def state_case(when=when):
                done=[False];backup=self.base/'state-held'
                def moved():
                    self.state.rename(backup);self.state.symlink_to(backup,target_is_directory=True)
                def connect(*args,**kwargs):
                    if not done[0]:
                        done[0]=True
                        if when=='before':moved();return original(*args,**kwargs)
                        con=original(*args,**kwargs);moved();return con
                    return original(*args,**kwargs)
                before_calls=dict(self.calls)
                with mock.patch.object(sqlite3,'connect',connect):
                    try:
                        try:h=self.load()
                        except self.q.FixtureConfigurationError as error:self.a.assertEqual(error.reason,'STATE_DIRECTORY_INVALID')
                        else:h.close();self.a.fail('connect race must reject state symlink replacement')
                    finally:
                        if backup.exists():self.state.unlink();backup.rename(self.state)
                self.a.assertTrue(done[0]);self.assert_closed()
                for key in ('run','read','collect','child'):self.a.assertEqual(self.calls[key],before_calls[key])
                self.observations.append(dict(case='L18',state_symlink_at_connect=when,
                    actual_temp_rename=True,unchanged_db_inodes=True,controller_calls=0))
            self.check('state-connect-window-'+when,state_case)

    def caller_exception_context(self):
        before=tree(self.base)
        observed=None;read_error_reason=None;identities=[];connections_live=False
        try:
            raise RuntimeError('L01_CALLER_EXCEPTION_CONTEXT')
        except RuntimeError as caller_error:
            identities.append(sys.exc_info()[1] is caller_error)
            handle=self.load()
            identities.append(sys.exc_info()[1] is caller_error)
            try:
                try:
                    observed=handle.read('exception-context:absent','a'*64)
                except self.q.FixtureConfigurationError as read_error:
                    read_error_reason=read_error.reason
                identities.append(sys.exc_info()[1] is caller_error)
                if observed is not None:
                    for connection in self.connections:
                        self.a.assertEqual(connection.execute('SELECT 1').fetchone(),(1,))
                    connections_live=True
            finally:
                handle.close()
        self.assert_closed()
        self.a.assertEqual(tree(self.base),before)
        self.a.assertEqual(self.calls['connect'],2)
        for key in ('collect','child','initialize'):
            self.a.assertEqual(self.calls[key],0)
        self.observations.append(dict(case='L12_EXCEPTION_CONTEXT',caller_exception='RuntimeError',
            caller_exception_identity_preserved=all(identities),identity_checks=identities,
            public_read_attempts_inside_except=1,read_error_reason=read_error_reason,
            read_reason=observed['report']['reason'] if observed is not None else None,
            actual_builtin_connections=2,connections_live_after_read=connections_live,
            owned_resources_closed=True,fixture_tree_unchanged=True,actual_child_count=0))
        self.a.assertTrue(all(identities))
        self.a.assertIsNone(read_error_reason,
            'load/read inside caller except must remain usable: '+str(read_error_reason))
        self.a.assertEqual(observed['report']['reason'],'JOB_NOT_FOUND')
        self.a.assertIsNone(observed['record']);self.a.assertIsNone(observed['artifact'])
        self.a.assertIs(observed['report']['fixture_only'],True)
        for key in ('head_code_executed',*TRUST):
            self.a.assertIs(observed['report'][key],False)
        self.a.assertTrue(connections_live)

    def reports_static(self):
        with self.load() as h:
            report=h.report
            self.a.assertEqual(report['scope'],'FIXTURE_CONFIGURATION_ONLY')
            self.a.assertEqual(report['reason'],'CONFIGURATION_VERIFIED')
            self.a.assertIs(report['fixture_only'],True)
            for key in ('head_code_executed','owner_approval_verified',*TRUST):self.a.assertIs(report[key],False)
        output,errors=io.StringIO(),io.StringIO()
        def fail(*args,**kwargs):raise sqlite3.OperationalError('PRIVATE_L01_EXCEPTION raw SQL '+str(self.base))
        with redirect_stdout(output),redirect_stderr(errors),mock.patch.object(sqlite3,'connect',fail):
            self.reject(self.load,'DATABASE_UNAVAILABLE')
        self.a.assertEqual(output.getvalue(),'');self.a.assertEqual(errors.getvalue(),'')
        raw=(self.install/FILES[-1]).read_text();parsed=ast.parse(raw,feature_version=(3,10))
        forbidden={'exec','eval','__import__','mkdir','makedirs','chmod','Popen','system','initialize_fixture_ledger','initialize_fixture_controller'}
        for node in ast.walk(parsed):
            if isinstance(node,ast.Call):
                name=node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else ''
                self.a.assertNotIn(name,forbidden)
            if isinstance(node,(ast.Import,ast.ImportFrom)):
                names=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or '']
                self.a.assertFalse(any(n.split('.')[0] in {'subprocess','socket','requests','urllib3','http'} for n in names))
        writes=[s for s in self.sql if s.lstrip().split(' ',1)[0].upper() in {'INSERT','UPDATE','DELETE','CREATE','DROP','ALTER','ATTACH'}]
        self.a.assertEqual(writes,[])

    def run(self, case):
        targets={'config':self.bad_cfg,'expectations':self.expectations,'marker':self.marker,
                 'hash-audit':lambda:self.source_hash(True),'hash-other':self.source_hash,
                 'origins':self.origins,'paths':self.paths,'state755':lambda:self.state_mode(True),
                 'state-other':self.state_mode,'dbpaths':self.db_paths,'sidecars':self.sidecars,
                 'schemas':self.schemas,'connection':self.connection_policy,'resources':self.resources,
                 'ok':lambda:self.real_collection('ok'),'exit7':lambda:self.real_collection('nonzero'),
                 'preparation':self.preparation,'unknown-spawn':self.unknown_spawn,'unknown-commit':self.unknown_commit,
                 'forwarding':self.forwarding,'live':self.live_changes,'races':self.races,'reports':self.reports_static,
                 'exception-context':self.caller_exception_context}
        with self.observed():
            self.check(case,targets[case])
        self.observations.append(dict(case=case,calls=self.calls,fixture_only=True,
                                      injected_metadata_or_fault=case in {'state-other','unknown-spawn','unknown-commit','races','origins'}))
        return dict(checks=self.checks,observations=self.observations)


class FixtureConfigurationTests(unittest.TestCase):
    def case(self, case):
        with tempfile.TemporaryDirectory(prefix='l01-',dir=ROOT) as directory:
            base=Path(directory);install=base/'install';install.mkdir(mode=0o700)
            pins={}
            for name in FILES:
                source=SOURCE/name
                if name==FILES[-1] and os.environ.get('L01_MUTANT_LOADER'):
                    source=Path(os.environ['L01_MUTANT_LOADER'])
                raw=source.read_bytes();target=install/name;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(raw);target.chmod(0o644);pins[name]=digest(raw)
            for directory_path in install.rglob('*'):
                if directory_path.is_dir():directory_path.chmod(0o755)
            marker=install/'fixture-controller-installation.json'
            marker.write_bytes(canonical(dict(schema=1,code_version=VERSION)));marker.chmod(0o644)
            (base/'state').mkdir(mode=0o700)
            spec=base/'spec.json';spec.write_bytes(canonical(dict(base=str(base),pins=pins)))
            completed=subprocess.run([sys.executable,'-I','-B',str(Path(__file__).resolve()),'--case',case,str(spec)],
                                     capture_output=True,text=True,timeout=45)
            self.assertEqual(completed.returncode,0,'fresh harness error: '+completed.stderr)
            result=json.loads(completed.stdout)
            HARNESS_RESULTS.append(dict(case=case,**result))
            OBSERVATIONS.extend(result['observations'])
            for row in result['checks']:
                with self.subTest(case=case,check=row['label']):
                    if row['status']=='FAIL':self.fail(row['traceback'])
                    if row['status']=='ERROR':raise RuntimeError(row['traceback'])


def _method(case):
    def method(self):self.case(case)
    return method


for _name,_case in [
    ('test_l01_config','config'),('test_l02_expectations','expectations'),('test_l03_marker','marker'),
    ('test_l04_external_audit_pin','hash-audit'),('test_l04_other_source_pins','hash-other'),
    ('test_l05_runtime_origins','origins'),('test_l06_paths_ancestors','paths'),
    ('test_l07_state_0755','state755'),('test_l07_other_state_guards','state-other'),
    ('test_l08_database_paths','dbpaths'),('test_l09_sidecars','sidecars'),('test_l10_headers_schemas','schemas'),
    ('test_l11_connection_policy','connection'),('test_l12_resource_lifecycle','resources'),
    ('test_l12_load_read_inside_caller_except','exception-context'),
    ('test_l13_real_ok','ok'),('test_l13_real_exit7','exit7'),('test_l14_preparation_failure','preparation'),
    ('test_l15_spawn_uncertainty','unknown-spawn'),('test_l15_commit_response_loss','unknown-commit'),
    ('test_l16_forwarding_isolation','forwarding'),('test_l17_postload_changes','live'),
    ('test_l18_connect_races','races'),('test_l19_reports_static','reports')]:
    setattr(FixtureConfigurationTests,_name,_method(_case))


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--case':
        spec=json.loads(Path(sys.argv[3]).read_text())
        harness=Harness(spec)
        print(json.dumps(harness.run(sys.argv[2])))
    else:
        unittest.main()
