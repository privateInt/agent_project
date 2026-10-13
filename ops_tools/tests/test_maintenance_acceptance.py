"""Native runner boundaries with synthetic metadata and actual leases/processes."""
from copy import deepcopy
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops_tools import maintenance_acceptance as a
from ops_platform.contracts import ReadFailure
from ops_platform.tests import test_service_lifecycle as fixture
from ops_platform.service import ReadService
from ops_platform.ops import activate_discovery as activation, docker_acceptance_fixture as http_fixture


class MetadataEngine:
    endpoint = 'unix:///var/run/docker.sock'
    def image(self, root): return 'sha256:' + 'b'*64
    def snapshot(self, ids=None): return {}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
    def create(self): return a.create_run(self.base, MetadataEngine())[0]
    def error(self, code, fn):
        with self.assertRaises(ReadFailure) as caught: fn()
        self.assertEqual(caught.exception.code, code)

    def test_linger_gate_runs_before_resource_creation(self):
        with patch.object(a.native, 'native_preflight', return_value={'linger': False}), patch.object(a, 'Engine') as engine:
            self.error('MAINTENANCE_ACCEPTANCE_LINGER_REQUIRED', lambda: a.run(self.base/'unused'))
        engine.assert_not_called(); self.assertEqual(list(self.base.iterdir()), [])

    def test_readonly_preflight_preserves_policy_and_does_not_claim_boot(self):
        with patch.object(a.native, 'native_preflight', return_value={'linger': True}): value = a.preflight()
        self.assertEqual(value['files_written'], []); self.assertFalse(value['linger_changed'])
        self.assertFalse(value['host_reboot_requested']); self.assertEqual(list(self.base.iterdir()), [])

    def test_private_copy_preserves_current_code_and_fixed_auxiliary_set(self):
        directory = self.create(); record = a.validate(directory)
        self.assertEqual(a.worker.code_hash(directory/'project'), a.core.CODE)
        marker = json.loads((directory/'maintenance-run.json').read_text())
        self.assertEqual(set(marker['auxiliary_hashes']), a.AUXILIARY)
        self.assertEqual(marker['source_set_sha256'], a.change.digest(record['source_hashes']))
        self.assertNotIn('.secrets', json.dumps(record))
        self.assertEqual((directory/'project'/a.ENTRY).stat().st_mode & 0o777, 0o600)

    def test_all_fixture_ports_are_automatically_selected_explicit_unique_loopback_bindings(self):
        directory = self.create(); record = a.validate(directory); ports = []
        for role, (_, subtree) in a.isolated.ROLES.items():
            rel = str(Path(subtree)/('acceptance-'+role+'.json'))
            path = directory/'project'/rel; spec = json.loads(path.read_text())
            self.assertEqual(a.digest(path.read_bytes()), record['compose'][rel]['sha256'])
            for cfg in spec['services'].values():
                binding = cfg['ports'][0]
                self.assertEqual((binding['host_ip'], binding['target'], binding['protocol']), ('127.0.0.1',8080,'tcp'))
                self.assertTrue(0 < binding['published'] <= 65535); ports.append(binding['published'])
        self.assertEqual(len(ports), 8); self.assertEqual(len(set(ports)), 8)
        self.assertNotIn('ports', json.loads((directory/'project/ops_platform/docker-compose.yml').read_text())['services']['ops-web'])

    def test_native_port_projection_requires_configured_and_live_publication_to_match_run_spec(self):
        directory = self.create(); record = a.validate(directory); engine = object.__new__(a.Engine)
        engine.owned = lambda *args: ['c'*64]
        engine.metadata = lambda _: {'identity': {'project':record['prefix']+'-prometheus',
            'service':a.isolated.ROLES['prometheus'][0]}}
        path = directory/'project'/a.isolated.ROLES['prometheus'][1]/'acceptance-prometheus.json'
        port = json.loads(path.read_text())['services'][a.isolated.ROLES['prometheus'][0]]['ports'][0]['published']
        projection = {'configured':[{'HostIp':'127.0.0.1','HostPort':str(port)}], 'published':[{'HostIp':'127.0.0.1','HostPort':str(port)}]}
        commands = []; engine.command = lambda cmd: (commands.append(cmd) or json.dumps(projection))
        self.assertEqual(engine.prometheus_publication(directory,'c'*64)['host_port'],port)
        projection['published'][0]['HostPort'] = str(port+1)
        self.error('MAINTENANCE_ACCEPTANCE_PORT_BINDING_CHANGED',lambda:engine.prometheus_publication(directory,'c'*64))
        self.assertTrue(all(cmd[:2]==['container','inspect'] for cmd in commands))
        self.assertNotIn('.Env',repr(commands)); self.assertNotIn('.Config',repr(commands))

    def test_tampered_tool_or_runtime_never_reaches_child_execution(self):
        directory = self.create()
        for rel in (a.ENTRY, 'ops_candidates/maintenance_intent_20260930/maintenance.py', 'ops_platform/ops/service_entry.py'):
            path = directory/'project'/rel; raw = path.read_bytes(); path.write_bytes(raw+b'\n# changed\n')
            with patch.object(a.subprocess, 'Popen') as spawn:
                with self.assertRaises(ReadFailure): a.invoke(directory)
            spawn.assert_not_called(); path.write_bytes(raw)

    def test_extra_runtime_symlink_and_hardlink_are_rejected(self):
        directory = self.create(); project = directory/'project'
        path = project/'ops_platform/unreviewed.py'; path.write_text(''); path.chmod(0o600)
        self.error('MAINTENANCE_ACCEPTANCE_RUNTIME_CHANGED', lambda: a.validate(directory)); path.unlink()
        path = project/a.ENTRY; raw = path.read_bytes(); path.unlink(); path.symlink_to(a.ROOT/a.ENTRY)
        with self.assertRaises(ReadFailure): a.validate(directory)
        path.unlink(); path.write_bytes(raw); path.chmod(0o600)
        os.link(path, directory/'foreign-link')
        self.error('MAINTENANCE_ACCEPTANCE_HARDLINK_REFUSED', lambda: a.validate(directory))

    def test_wrong_marker_owner_never_reaches_unit_cleanup(self):
        directory = self.create(); marker = json.loads((directory/'maintenance-run.json').read_text())
        marker['owner'] = 'foreign'; a.isolated.write(directory/'maintenance-run.json', marker)
        engine = object.__new__(a.Engine)
        with patch.object(a.native, 'cleanup_unit_subprocess') as cleanup:
            self.error('MAINTENANCE_ACCEPTANCE_RECORD_INVALID', lambda: engine.cleanup(directory))
        cleanup.assert_not_called()

    def test_uncertain_operation_retains_containers_after_unit_exit_proof(self):
        directory = self.create(); record = a.validate(directory)
        a.isolated.write(directory/'maintenance-operation.json', {'schema': 1, 'kind': a.KIND,
            'directory': str(directory), 'owner': record['owner'], 'phase': 'stop_requested'})
        engine = object.__new__(a.Engine)
        with patch.object(a.native, 'cleanup_unit_subprocess', return_value={'result': 'passed'}) as cleanup, \
             patch.object(engine, 'command') as command:
            self.error('MAINTENANCE_ACCEPTANCE_OPERATION_UNCERTAIN', lambda: engine.cleanup(directory))
        cleanup.assert_called_once(); command.assert_not_called()

    def test_failed_unit_cleanup_prevents_container_or_network_control(self):
        directory = self.create(); engine = object.__new__(a.Engine)
        with patch.object(a.native, 'cleanup_unit_subprocess', side_effect=ReadFailure('UNIT_UNCERTAIN')), \
             patch.object(engine, 'command') as command:
            self.error('UNIT_UNCERTAIN', lambda: engine.cleanup(directory))
        command.assert_not_called()

    def test_cleanup_removes_only_owned_resources_and_never_prunes(self):
        directory = self.create(); engine = object.__new__(a.Engine); live = ['c'*64]; calls = []
        engine.owned = lambda *a: list(live); engine.protect = lambda _: None; engine.snapshot = lambda *args: {}
        def command(args, **kw):
            calls.append(args)
            if args[:2] == ['container', 'rm']: live.clear(); return ''
            if args[:2] == ['network', 'ls']: return 'd'*64 if live or not any(cmd[:2]==['network','rm'] for cmd in calls) else ''
            if args[:2] == ['network', 'inspect']:
                return json.dumps({'owner': a.validate(directory)['owner'], 'containers': {}, 'name':a.validate(directory)['fixture_network']['name'],'driver':'bridge'})
            if args[:2] == ['network', 'rm']: return ''
            self.fail(args)
        engine.command = command
        with patch.object(a.native, 'cleanup_unit_subprocess', return_value={'result': 'passed', 'worker_exit_verified': True}):
            value = engine.cleanup(directory)
        self.assertEqual(value['owned_containers_removed'], 1); self.assertEqual(value['owned_networks_removed'], 1)
        self.assertFalse(any(set(cmd) & {'image', 'volume', 'prune'} for cmd in calls))

    def test_readonly_plan_wait_retries_only_specific_contention_codes(self):
        ctl = SimpleNamespace(plan=lambda **kw: (_ for _ in ()).throw(ReadFailure('PUBLIC_URL_CHANGE_BUSY')))
        with patch.object(a.time, 'sleep'), patch.object(a.time, 'monotonic', side_effect=[0, 1]):
            self.error('MAINTENANCE_ACCEPTANCE_PLAN_WAIT_TIMEOUT', lambda: a.plan_wait(ctl, seconds=1))
        ctl.plan = lambda **kw: (_ for _ in ()).throw(ReadFailure('WORKER_RECOVERY_REQUIRED'))
        self.error('WORKER_RECOVERY_REQUIRED', lambda: a.plan_wait(ctl))


class InventoryTests(GuardTests):
    # Inherit setup/helpers only; inherited guard cases are removed below.
    def engine(self, directory):
        record = a.validate(directory); owner = record['owner']; rows = {}
        for role, number, service in (('ops', 1, 'ops-web'), ('prometheus', 2, a.isolated.ROLES['prometheus'][0])):
            rel = 'ops_platform/docker-compose.yml' if role == 'ops' else 'infra/observability/acceptance-prometheus.json'
            rows[format(number, '064x')] = {'acceptance': owner, 'files': str(directory/'project'/rel),
                'identity': {'id': format(number, '064x'), 'image': record['image'], 'service': service,
                    'project': record['prefix']+'-'+role, 'started': 'start-1', 'running': True, 'health': 'healthy',
                    'mounts': [{'Type': 'bind', 'Source': str(directory/'project'), 'Destination': '/source', 'RW': False}]}}
        rows[format(1, '064x')]['identity']['mounts'].append({'Type': 'tmpfs', 'Destination': '/tmp', 'RW': True, 'Source': ''})
        engine = SimpleNamespace(owned=lambda *args: list(rows), metadata=lambda ident: deepcopy(rows[ident]),
            snapshot=lambda: {format(3, '064x'): {'id': format(3, '064x'), 'started': 'protected-start', 'running': True}})
        return engine, rows

    def test_dynamic_metadata_scope_handles_ops_tmpfs_and_protects_other_targets(self):
        directory = self.create(); engine, rows = self.engine(directory)
        value = a.Inventory(directory, engine)()
        self.assertEqual(set(value['scope']), {'ops', 'prometheus'}); self.assertEqual(len(value['protected']), 1)
        original = deepcopy(value)
        for row in rows.values(): row['identity'].update(started='start-2', running=False)
        self.assertEqual(a.Inventory(directory, engine)(), original)

    def test_mount_image_owner_change_or_missing_service_is_not_adopted(self):
        directory = self.create(); engine, rows = self.engine(directory); key = format(1, '064x')
        value = a.Inventory(directory, engine)(); original = deepcopy(rows[key])
        rows[key]['identity']['mounts'][0]['RW'] = True
        self.assertNotEqual(a.Inventory(directory, engine)(), value); rows[key] = deepcopy(original)
        rows[key]['acceptance'] = 'foreign'
        self.error('MAINTENANCE_ACCEPTANCE_SCOPE_AMBIGUOUS', lambda: a.Inventory(directory, engine)())
        rows[key] = deepcopy(original); rows[key]['identity']['image'] = 'sha256:'+'c'*64
        self.error('MAINTENANCE_ACCEPTANCE_SCOPE_AMBIGUOUS', lambda: a.Inventory(directory, engine)())
        rows.pop(key)
        self.error('MAINTENANCE_ACCEPTANCE_SCOPE_MISSING', lambda: a.Inventory(directory, engine)())

    def test_mount_outside_run_and_ambiguous_service_are_rejected(self):
        directory = self.create(); engine, rows = self.engine(directory); key = format(1, '064x')
        rows[key]['identity']['mounts'][0]['Source'] = '/production/state'
        self.error('MAINTENANCE_ACCEPTANCE_MOUNT_OUTSIDE_RUN', lambda: a.Inventory(directory, engine)())
        rows[key]['identity']['mounts'][0]['Source'] = str(directory/'project')
        duplicate = deepcopy(rows[key]); duplicate['identity']['id'] = format(4, '064x'); rows[format(4, '064x')] = duplicate
        self.error('MAINTENANCE_ACCEPTANCE_SCOPE_AMBIGUOUS', lambda: a.Inventory(directory, engine)())

    def test_target_or_protected_change_between_collections_is_not_stable(self):
        directory = self.create(); engine, rows = self.engine(directory); calls = []
        def snapshot():
            calls.append(1)
            return {format(3, '064x'): {'id': format(3, '064x'), 'started': str(len(calls)), 'running': True}}
        engine.snapshot = snapshot
        self.error('MAINTENANCE_ACCEPTANCE_INVENTORY_UNSTABLE', lambda: a.Inventory(directory, engine)())


# Avoid rerunning inherited GuardTests when loading the whole module.
for name in [n for n in vars(GuardTests) if n.startswith('test_')]: setattr(InventoryTests, name, None)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name); self.state = self.directory/'state'; self.state.mkdir(mode=0o700)

    def test_child_inherits_actual_refresh_and_orchestration_leases_and_exits(self):
        checkpoints = []
        with a.lifecycle_lock(self.directory) as outer, a.ws.automatic_lease(self.state) as descriptor:
            scope = SimpleNamespace(parent=SimpleNamespace(directory=self.state), descriptor=descriptor,
                checkpoint=lambda: checkpoints.append(1))
            self.assertEqual(a.leased_child(scope, self.directory, outer), {'result': 'passed', 'child_processes_stopped': True})
        self.assertEqual(len(checkpoints), 2)
        with a.lifecycle_lock(self.directory), a.ws.automatic_lease(self.state): pass

    def test_real_surviving_child_blocks_cleanup_after_supervisor_descriptor_closes(self):
        with a.lifecycle_lock(self.directory) as outer, a.ws.automatic_lease(self.state) as descriptor:
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c', a.LEASE_CHILD, str(self.state),
                str(self.directory), str(descriptor), str(outer), '0.5'], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, pass_fds=(descriptor, outer))
        try:
            with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_ACCEPTANCE_BUSY'):
                with a.lifecycle_lock(self.directory): self.fail('orphan lease was not held')
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err); self.assertTrue(json.loads(out)['both_inherited_leases_verified'])
        finally:
            if child.poll() is None: child.kill(); child.wait()
        with a.lifecycle_lock(self.directory), a.ws.automatic_lease(self.state): pass

    def test_unlocked_or_wrong_descriptor_cannot_claim_inherited_lease(self):
        path = self.state/'automatic-refresh.lock'; fd = os.open(path, os.O_CREAT|os.O_RDWR, 0o600)
        try:
            with a.lifecycle_lock(self.directory) as outer:
                scope = SimpleNamespace(parent=SimpleNamespace(directory=self.state), descriptor=fd, checkpoint=lambda: None)
                with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_ACCEPTANCE_CHILD_FAILED'):
                    a.leased_child(scope, self.directory, outer)
        finally: os.close(fd)


class OperationTests(unittest.TestCase):
    def scenario(self, failure=False, port_change=False):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); directory = Path(temp.name)
        record = {'owner': '00000000-0000-0000-0000-000000000001'}
        target = {role: {'container_id': char*64} for role, char in (('ops', 'b'), ('prometheus', 'c'))}
        states = {row['container_id']: {'running': True, 'health': 'healthy', 'started': 'before'} for row in target.values()}
        commands = []
        def command(args, **kw):
            commands.append(args)
            if failure: raise ReadFailure('ACCEPTANCE_COMMAND_UNCERTAIN')
            states[args[-1]].update(running=args[1]=='start', started='after' if args[1]=='start' else 'before')
            return ''
        engine = SimpleNamespace(metadata=lambda ident: {'identity': deepcopy(states[ident])},
            owned=lambda *a: list(states), command=command)
        publications = []
        def publication(*args):
            publications.append(1)
            return {'host_ip':'127.0.0.1','host_port':35000+int(port_change and len(publications)>1),'container_port':8080}
        engine.prometheus_publication = publication
        scope = SimpleNamespace(record={'inventory': {'scope': target}, 'id': 'job'}, checkpoint=lambda: None)
        with patch.object(a, 'validate', return_value=record), patch.object(a, 'leased_child', return_value={'result': 'passed', 'child_processes_stopped': True}):
            if failure:
                with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_COMMAND_UNCERTAIN'): a.cycle_scoped(scope, directory, engine)
            elif port_change:
                with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_ACCEPTANCE_PORT_BINDING_CHANGED'): a.cycle_scoped(scope, directory, engine)
            else: self.assertEqual(a.cycle_scoped(scope, directory, engine)['result'], 'passed')
        return commands, json.loads((directory/'maintenance-operation.json').read_text())

    def test_scoped_stop_start_records_command_intents_and_actual_started_time_change(self):
        commands, journal = self.scenario()
        self.assertEqual([cmd[1] for cmd in commands], ['stop','stop','start','start'])
        self.assertEqual(journal['phase'], 'completed'); self.assertTrue(journal['child_processes_stopped'])
        self.assertTrue(all(v=='after' for v in journal['started_after'].values()))
        self.assertTrue(journal['prometheus_publication_preserved'])

    def test_uncertain_stop_is_not_repeated_or_followed_by_start(self):
        commands, journal = self.scenario(True)
        self.assertEqual(len(commands), 1); self.assertEqual(journal['phase'], 'stop_requested')
        self.assertEqual(len(journal['commands']), 1)

    def test_changed_publication_after_completed_commands_never_claims_readback_success_and_can_clean(self):
        commands,journal = self.scenario(port_change=True)
        self.assertEqual(len(commands),4); self.assertEqual(journal['phase'],'completed')
        self.assertNotIn('prometheus_publication_preserved',journal)


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name); self.directory.chmod(0o700)
        self.servers = []; self.addCleanup(self.close)
        self.control = self.directory/'control.json'; a.isolated.write(self.control,{'version':0})

    def close(self):
        for server,thread in reversed(self.servers):
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def serve(self,role,port=0):
        server = http_fixture.serve(role,self.control,host='127.0.0.1',port=port)
        thread = threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True)
        thread.start(); self.servers.append((server,thread)); return server

    def stop(self,server):
        entry = next(value for value in self.servers if value[0] is server)
        server.shutdown(); server.server_close(); entry[1].join(timeout=3); self.servers.remove(entry)

    def setup_ops(self):
        prometheus = self.serve('prometheus'); manager = self.serve('alertmanager')
        access = self.directory/'access-key'; a.manage.write_new(access,'synthetic-access-key')
        cfg = {'prometheus_url':'http://127.0.0.1:'+str(prometheus.server_port),
            'alertmanager_url':'http://127.0.0.1:'+str(manager.server_port), 'access_file':str(access),
            'timeout':.2,'state_file':str(self.directory/'cache.json')}
        service = ReadService(cfg); self.addCleanup(service.close)
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                if self.headers.get('Authorization')!='Bearer synthetic-access-key': self.send_error(401); return
                if self.path=='/api/resources': body=service.resources()
                elif self.path=='/api/alerts': body=service.alerts()
                else: self.send_error(404); return
                raw=json.dumps(body).encode(); self.send_response(200)
                self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
        ops = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=lambda:ops.serve_forever(poll_interval=.01),daemon=True)
        thread.start(); self.servers.append((ops,thread)); cfg['port']=ops.server_port
        return cfg,service,prometheus

    def test_real_http_changed_upstream_port_breaks_current_config_then_original_port_restores_readback(self):
        cfg,service,prometheus = self.setup_ops(); before = deepcopy(cfg); port = prometheus.server_port
        self.assertEqual(set(activation.readback(cfg)['resources']),set(a.RESOURCE_GROUPS))
        self.stop(prometheus)
        replacement = self.serve('prometheus',a.isolated.free_port(exclude={port}))
        # A fresh discovery can reach the replacement while the accepted config still points at the old port.
        self.assertEqual(a.Transport('http://127.0.0.1:'+str(replacement.server_port)).get('/api/v1/status/buildinfo')[0],200)
        with self.assertRaisesRegex(ReadFailure,'OPS_RESOURCE_READBACK_FAILED'): activation.readback(cfg)
        failed = a.resource_summary(service.resources())
        self.assertTrue(all(row['status']=='error' and row['error_code']=='CONNECTION_FAILED' for row in failed.values()))
        self.stop(replacement); self.serve('prometheus',port)
        self.assertEqual(set(activation.readback(cfg)['resources']),set(a.RESOURCE_GROUPS))
        self.assertEqual(cfg,before)

    def test_failed_probe_preserves_gate_and_projects_only_seven_bounded_resource_rows(self):
        cfg,service,prometheus = self.setup_ops(); self.stop(prometheus)
        probe = a.ReadbackProbe(self.directory); checks = []
        with patch.object(a,'validate'),patch.object(a,'checked_state',return_value=self.directory), \
             patch.object(a.manage,'existing',return_value=cfg), \
             patch.object(a.core,'live_readback',side_effect=ReadFailure('OPS_RESOURCE_READBACK_FAILED')):
            with self.assertRaisesRegex(ReadFailure,'OPS_RESOURCE_READBACK_FAILED'):
                probe(self.directory,{},lambda:checks.append(1))
        self.assertEqual(len(checks),2); self.assertEqual(set(probe.summary),set(a.RESOURCE_GROUPS))
        exc = ReadFailure('OPS_RESOURCE_READBACK_FAILED'); exc.resource_readback = probe.summary
        report = a.error_report(exc)
        self.assertEqual(report['resource_readback'],probe.summary)
        raw = json.dumps(report)
        for private in ('synthetic-access-key',cfg['prometheus_url'],'labels','items','value','query'):
            self.assertNotIn(private,raw)

    def test_failed_probe_does_not_replace_original_error_if_diagnostic_is_unavailable(self):
        probe = a.ReadbackProbe(self.directory)
        with patch.object(a,'validate'),patch.object(a,'checked_state',return_value=self.directory), \
             patch.object(a.core,'live_readback',side_effect=ReadFailure('OPS_RESOURCE_READBACK_FAILED')), \
             patch.object(a.manage,'existing',side_effect=ReadFailure('PREPARED_STATE_BUSY')):
            with self.assertRaisesRegex(ReadFailure,'OPS_RESOURCE_READBACK_FAILED'): probe(self.directory,{},lambda:None)
        self.assertIsNone(probe.summary)

    def test_untrusted_child_diagnostic_cannot_forward_extra_fields_values_or_unbounded_errors(self):
        value = a.resource_summary({'metrics':{'targets':{'status':'ok','data':{'count':4,'coverage':'query_result','items':[{'labels':{'secret':'value'}}]}}}})
        self.assertEqual(a.safe_summary(value),value)
        for key,bad in (('error_code','secret-token '+('x'*200)),('count',True),('items',['raw'])):
            changed = deepcopy(value); changed['targets'][key]=bad
            self.assertIsNone(a.safe_summary(changed))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ServiceTests('test_install_inactive_private_idempotent_without_keys')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.binding['code_sha256'] = a.core.CODE
        self.directory, self.manager = self.f.directory, self.f.manager
        self.child = None; self.addCleanup(self.close)

    def close(self):
        if self.child is not None:
            try: os.kill(self.child, signal.SIGTERM)
            except ProcessLookupError: pass
            os.waitpid(self.child, 0); self.child = None

    def start(self):
        self.manager.starts += 1; self.manager.control_commands += 1
        self.manager.invocation = uuid_hex = os.urandom(16).hex(); self.manager.active = True
        pipe, ready = os.pipe(); child = os.fork()
        if child == 0:
            os.close(pipe)
            # Model a fresh unit: only its automatic lease is inherited by its
            # evaluation children, never the controller's management lock.
            for path in Path('/proc/self/fd').iterdir():
                try:
                    if os.readlink(path).endswith('service-management.lock'): os.close(int(path.name))
                except (OSError, ValueError): pass
            try:
                os.write(ready, str(a.worker.process_identity()['proc_pid']).encode()); os.close(ready)
                manifest = a.service.load(self.directory)
                context = a.boot.Context(self.directory, manifest, policy_check=lambda *args: None,
                    manager_check=lambda *args: None, pending_check=lambda *args: None,
                    snapshot_fn=lambda *args: deepcopy(self.f.binding))
                result = a.worker.run(self.directory, interval=30, deadline_seconds=3,
                    service_invocation=uuid_hex, boot_context=context,
                    snapshot_fn=lambda *args: deepcopy(self.f.binding), evaluate_fn=lambda *args:
                    {'result':'passed','classification':'accepted','configuration_changed':False,'restart_requested':False})
                os._exit(result['exit_code'])
            except BaseException: os._exit(2)
        os.close(ready); self.manager.pid = os.read(pipe, 100).decode(); os.close(pipe); self.child = child

    def stop(self):
        self.close(); self.manager.active = False; self.manager.pid = '0'
        self.manager.stops += 1; self.manager.control_commands += 1

    def controller(self):
        self.manager.start = self.start; self.manager.stop = self.stop
        ctl = object.__new__(a.native.Controller); ctl.state = self.directory; ctl.backend = self.manager
        ctl.good = self.f.good
        return ctl

    def test_real_boot_worker_fixture_scoped_stop_fresh_return_and_failed_readback_pause(self):
        ctl = self.controller()
        with patch.object(a.worker, 'runtime_snapshot', return_value=deepcopy(self.f.binding)):
            first = a.initialize_boot_fixture(self.directory, ctl)
        store = a.ws.Store(self.directory); before = {name:(store.path.parent/name).read_bytes() for name in ('boot-start.json','code-transition.json','service.json')}
        inventory = {'scope': {'ops': {'container_id':'b'*64,'image_id':'sha256:'+'c'*64,'mounts_sha256':'d'*64,'owner':self.f.owner['owner']}}, 'protected':{}}
        readback = {'result':'passed','live_checks_performed':True,'configuration_written':False,'restart_requested':False}
        controller = a.core.Maintenance(self.directory, self.manager, inventory_fn=lambda:deepcopy(inventory),
            snapshot_fn=lambda _:deepcopy(self.f.binding), environment_fn=lambda *args:None,
            history_fn=lambda _:'e'*64, readback_fn=lambda directory,manifest,guard:(guard() or deepcopy(readback)))
        controller.return_capture=lambda *args:{'synthetic':True}
        controller.return_check=lambda *args:None
        with a.lifecycle_lock(self.f.root) as outer:
            proposal = controller.plan(wait_seconds=5)
            result = controller.run(proposal, lambda scope:a.leased_child(scope, self.f.root, outer))
            self.assertEqual(result['result'], 'passed', result)
            self.assertIsNone(self.child); self.assertEqual(store.control()['desired'], 'paused')
            returned = controller.resume(proposal['id'])
            self.assertEqual(returned['result'], 'passed', returned); self.assertTrue(returned['fresh_evaluation_verified'])
            self.assertNotEqual(a.native.invocation(first), a.native.invocation(ctl.status()))
            with self.assertRaisesRegex(ReadFailure, 'MAINTENANCE_RETURN_REFUSED'): controller.resume(proposal['id'])
            second = controller.plan(wait_seconds=5)
            self.assertEqual(controller.run(second, lambda scope:a.leased_child(scope, self.f.root, outer))['result'], 'passed')
            controller.readback = lambda *args: (_ for _ in ()).throw(ReadFailure('OPS_ALERT_READBACK_FAILED'))
            starts = self.manager.starts
            denied = controller.resume(second['id']); self.assertEqual(denied['code'], 'OPS_ALERT_READBACK_FAILED')
            self.assertEqual(self.manager.starts, starts); self.assertIsNone(self.child)
            self.assertEqual(store.control()['desired'], 'paused'); self.assertFalse(a.service.enabled(a.service.load(self.directory)))
            self.assertEqual(before, {name:(store.path.parent/name).read_bytes() for name in before})


class DockerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.engine = object.__new__(a.Engine)
        self.engine.endpoint = 'unix:///var/run/docker.sock'; self.engine.env = {}
        self.engine._docker_context = {'phase':'fixture_preparation','fixture_role':'langfuse'}

    def diagnostic(self, **changes):
        return dict({'operation':'compose_up','outcome':'failed','reason':'UNKNOWN',
            'exit_code':1,'text_truncated':False,'phase':'fixture_preparation','fixture_role':'langfuse'}, **changes)

    def test_known_error_signatures_are_bounded_labels_not_server_cause_claims(self):
        for text, expected in (
            ('failed to create network synthetic: all predefined address pools have been fully subnetted','ADDRESS_POOL_EXHAUSTED'),
            ('could not find an available, non-overlapping IPv4 address pool','ADDRESS_POOL_EXHAUSTED'),
            ('Bind for 127.0.0.1:30000 failed: port is already allocated','HOST_PORT_BUSY'),
            ('listen tcp: address already in use','HOST_PORT_BUSY'),
            ('write /private/path: no space left on device','STORAGE_FULL'),
            ('disk quota exceeded','STORAGE_FULL'),
            ('container synthetic is unhealthy','CONTAINER_UNHEALTHY'),
            ('failed to create network: unexpected response','NETWORK_SETUP_FAILED'),
            ('permission denied','DOCKER_ACCESS_DENIED'),
            ('Cannot connect to the Docker daemon','DOCKER_CONNECTION_FAILED'),
            ('No such image: sha256:private','IMAGE_UNAVAILABLE'),
            ('unknown error raw-token','UNKNOWN')):
            self.assertEqual(a.docker_reason('', text), (expected,False))
        self.assertEqual(a.docker_reason('no such image','no space left on device')[0],'MULTIPLE_SIGNATURES')
        self.assertEqual(a.docker_reason('',b'container synthetic is unhealthy')[0],'CONTAINER_UNHEALTHY')
        self.assertEqual(a.docker_reason('',('x'*40000)+'no space left on device'),('UNKNOWN',True))

    def test_failure_preserves_code_deadline_environment_and_never_retries(self):
        response = SimpleNamespace(returncode=9,stdout='synthetic-secret-value',
            stderr='failed to create network /private/path: all predefined address pools have been fully subnetted')
        with patch.object(a.subprocess,'run',return_value=response) as run:
            with self.assertRaises(ReadFailure) as caught: self.engine.command(['compose','up','lf-web'],timeout=60)
        self.assertEqual(caught.exception.code,'ACCEPTANCE_DOCKER_COMMAND_FAILED'); run.assert_called_once()
        self.assertEqual(run.call_args.kwargs['timeout'],60); self.assertIs(run.call_args.kwargs['env'],self.engine.env)
        report = a.error_report(caught.exception)
        self.assertEqual(report['docker_diagnostic'],self.diagnostic(reason='ADDRESS_POOL_EXHAUSTED',exit_code=9))
        self.assertEqual(report['stage'],a.STAGE)
        for private in ('synthetic-secret-value','/private/path','lf-web','stderr','argv'):
            self.assertNotIn(private,json.dumps(report))

    def test_timeout_unavailable_and_response_limit_remain_distinct_and_do_not_forward_output(self):
        for raised, expected, reason in ((subprocess.TimeoutExpired(['private'],.1,output=b'raw-token',stderr=b'no space left on device'),
                'ACCEPTANCE_COMMAND_UNCERTAIN','COMMAND_TIMEOUT'),
                (PermissionError('/private/key'), 'ACCEPTANCE_DOCKER_UNAVAILABLE','CLI_UNAVAILABLE')):
            with patch.object(a.subprocess,'run',side_effect=raised) as run:
                with self.assertRaises(ReadFailure) as caught: self.engine.command(['container','start','b'*64])
            run.assert_called_once(); self.assertEqual(caught.exception.code,expected)
            report = a.error_report(caught.exception)
            self.assertEqual(report['docker_diagnostic']['reason'],reason)
            self.assertIsNone(report['docker_diagnostic']['exit_code']); self.assertNotIn('raw-token',json.dumps(report))
        with patch.object(a.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='x'*(1024*1024+1),stderr='')):
            with self.assertRaisesRegex(ReadFailure,'ACCEPTANCE_DOCKER_RESPONSE_LIMIT') as caught:
                self.engine.command(['network','ls'])
        self.assertEqual(caught.exception.docker_diagnostic['outcome'],'response_limit')

    def test_success_is_unchanged_even_if_stderr_contains_an_error_signature(self):
        result=SimpleNamespace(returncode=0,stdout='accepted projection',stderr='permission denied warning')
        with patch.object(a.subprocess,'run',return_value=result) as run:
            self.assertEqual(self.engine.command(['ps','-aq']),'accepted projection')
        run.assert_called_once()

    def test_projection_rejects_raw_fields_types_unbounded_values_and_unknown_labels(self):
        self.assertEqual(a.safe_docker_diagnostic(self.diagnostic()),self.diagnostic())
        for name, value in (('argv',['raw-key']),('stderr','raw-key'),('operation',['compose_up']),
                ('reason',{'raw':'token'}),('outcome','success'),('exit_code',True),('exit_code',9999),
                ('text_truncated',0),('phase',['fixture_preparation']),('fixture_role','foreign-service'),
                ('fixture_role',['langfuse'])):
            candidate=self.diagnostic(); candidate[name]=value
            self.assertIsNone(a.safe_docker_diagnostic(candidate),name)
            exc=ReadFailure('ACCEPTANCE_DOCKER_COMMAND_FAILED');exc.docker_diagnostic=candidate
            self.assertNotIn('docker_diagnostic',a.error_report(exc))

    def test_compose_role_is_validated_before_control_and_context_is_restored_on_failure(self):
        seen=[]
        def compose(engine,*args):
            seen.append(deepcopy(engine._docker_context))
            return engine.command(['compose','up',args[2]],60)
        error=SimpleNamespace(returncode=1,stdout='',stderr='port is already allocated')
        with patch.object(a.Engine,'verify_fixture_network'),patch.object(a.isolated.Engine,'compose',compose),patch.object(a.subprocess,'run',return_value=error):
            with self.assertRaises(ReadFailure) as caught:
                self.engine.compose(self.base,'model_platform/acceptance-model.json','manual-model')
            self.assertEqual(caught.exception.docker_diagnostic['fixture_role'],'manual_model')
            self.assertEqual(self.engine._docker_context,{'phase':'fixture_preparation','fixture_role':'langfuse'})
            with self.assertRaisesRegex(ReadFailure,'MAINTENANCE_ACCEPTANCE_FIXTURE_ROLE_INVALID'):
                self.engine.compose(self.base,'foreign/project.json','lf-web')
        self.assertEqual(len(seen),1)

    def stub(self, body):
        path=self.base/'docker';path.write_text('#!'+sys.executable+'\n'+body);path.chmod(0o700)
        self.engine.env={'PATH':str(self.base)}

    def test_real_cli_process_failure_and_secret_output_are_projected_without_raw_text(self):
        self.stub("import sys\nprint('private-key-and-path')\nprint('all predefined address pools have been fully subnetted',file=sys.stderr)\nraise SystemExit(7)\n")
        with self.assertRaises(ReadFailure) as caught:self.engine.command(['compose','up','lf-web'],2)
        report=a.error_report(caught.exception)
        self.assertEqual(report['docker_diagnostic']['exit_code'],7)
        self.assertEqual(report['docker_diagnostic']['reason'],'ADDRESS_POOL_EXHAUSTED')
        self.assertNotIn('private-key',json.dumps(report));self.assertEqual(list(self.base.iterdir()),[self.base/'docker'])

    def test_real_cli_timeout_is_uncertain_and_not_resubmitted(self):
        marker=self.base/'starts'
        self.stub('import time\nfrom pathlib import Path\np=Path('+repr(str(marker))+')\np.write_text(p.read_text()+"1" if p.exists() else "1")\ntime.sleep(2)\n')
        with self.assertRaisesRegex(ReadFailure,'ACCEPTANCE_COMMAND_UNCERTAIN') as caught:
            self.engine.command(['compose','up','lf-web'],.2)
        self.assertEqual(marker.read_text(),'1');self.assertEqual(caught.exception.docker_diagnostic['outcome'],'timeout')

    def test_child_failure_projection_survives_parent_boundary_without_extra_fields(self):
        directory=a.create_run(self.base,MetadataEngine())[0]
        diagnostic=self.diagnostic(reason='CONTAINER_UNHEALTHY')
        def spawn(*args,**kwargs):
            kwargs['stdout'].write(json.dumps({'result':'attention_required','code':'ACCEPTANCE_DOCKER_COMMAND_FAILED',
                'docker_diagnostic':diagnostic,'stderr':'raw-key'}));kwargs['stdout'].flush()
            return SimpleNamespace(wait=lambda **kwargs:2)
        with patch.object(a.subprocess,'Popen',side_effect=spawn):
            with self.assertRaises(ReadFailure) as caught:a.invoke(directory)
        self.assertEqual(a.error_report(caught.exception)['docker_diagnostic'],diagnostic)
        self.assertNotIn('raw-key',json.dumps(a.error_report(caught.exception)))

    def test_failed_preparation_keeps_original_diagnostic_during_scoped_cleanup(self):
        directory,record=a.create_run(self.base,MetadataEngine())
        commands=[]
        class FakeEngine:
            def create_fixture_network(self,directory): pass
            def compose(self,directory,rel,name):
                commands.append(name)
                if len(commands)==2:
                    exc=ReadFailure('ACCEPTANCE_DOCKER_COMMAND_FAILED')
                    exc.docker_diagnostic=outer.diagnostic(fixture_role='manual_model',reason='STORAGE_FULL')
                    raise exc
            def cleanup(self,directory):
                commands.append('scoped_cleanup')
                return {'result':'passed','service':{'unit_created':False},'owned_containers_removed':1}
        outer=self
        with patch.object(a,'preflight'),patch.object(a,'Engine',return_value=FakeEngine()), \
                patch.object(a,'create_run',return_value=(directory,record)),patch.object(a,'invoke') as invoke:
            report=a.run(self.base)
        invoke.assert_not_called();self.assertEqual(commands,['gateway','manual-model','scoped_cleanup'])
        self.assertEqual(report['docker_diagnostic']['reason'],'STORAGE_FULL')
        self.assertEqual(report['cleanup']['result'],'passed')
        saved=json.loads((directory/'maintenance-report.json').read_text())
        self.assertEqual(saved['docker_diagnostic'],report['docker_diagnostic'])
        self.assertFalse(report['production_worker_stopped'])


if __name__ == '__main__': unittest.main()
