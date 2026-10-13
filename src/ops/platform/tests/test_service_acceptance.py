"""Native acceptance orchestration with explicitly synthetic manager/Docker metadata."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.ops import docker_acceptance as base, service_acceptance as check
from src.ops.platform.ops import service_lifecycle as service, refresh_worker as worker, worker_state as ws
from src.ops.platform.ops import lifecycle_probe as probe, refresh_discovery as refresh
from src.ops.platform.tests.test_docker_acceptance import MetadataEngine
from src.ops.platform.tests import test_docker_acceptance as acceptance_tests
from src.ops.platform.tests.test_service_lifecycle import Manager


class SimulatedController(check.Controller):
    """Real lifecycle/refresh/HTTP transactions; no native user manager or timers."""
    def __init__(self, state):
        self.state = state; self.manager_version = 'synthetic'
        owner = ws.read(state / 'owner.json')
        self.binding = {'directory': str(state), 'owner': owner, 'code_sha256': worker.code_hash(Path(owner['project_root'])),
                        'python': str(Path(sys.executable).resolve()), 'python_version': list(sys.version_info[:3]),
                        'docker_path': '/synthetic/docker', 'daemon_id': 'synthetic',
                        'ops_container_id': 'b'*64, 'ops_image_id': 'sha256:'+'c'*64}
        outer = self
        class Backend(Manager):
            def __init__(self, *a): super().__init__(*a); self.failed = False
            def inspect(self):
                result = super().inspect()
                if self.failed: result.update(ActiveState='failed', SubState='failed', Result='exit-code', ExecMainStatus='2')
                return result
            def start(self):
                super().start(); self.failed = False
                self.pid = str(worker.process_identity()['proc_pid']); outer.cycles = 0
            def stop(self):
                super().stop(); self.failed = False
                ws.Store(state).status(phase='stopped', completed_cycles=outer.cycles)
            def reset_failed(self): super().reset_failed(); self.failed = False
        config = state.parent / 'test-config'
        self.reader = probe.Reader(env={'XDG_CONFIG_HOME': str(config), 'PATH': os.defpath})
        self.backend = Backend(config, 'agent-project-ops-refresh-' + owner['owner'].replace('-', '') + '.service')
        self.cycles = 0

    def call(self, action):
        def preflight(*a, **kw):
            blockers = ['REFRESH_PENDING_USE_CHECK_OR_ROLLBACK'] if refresh.lineage(self.state)['pending'] else []
            return {'blockers': blockers, 'docker': {'endpoint': 'unix:///synthetic/docker.sock'},
                    'persistence': {'selected': 'session', 'linger': False}}
        return service.run(action, self.state, persistence='session', interval=30, reader=self.reader, backend=self.backend,
                           preflight_fn=preflight, snapshot_fn=lambda _: deepcopy(self.binding),
                           recovery_fn=lambda directory, **kw: worker.acknowledge_recovery(
                               directory, snapshot_fn=lambda _: deepcopy(self.binding), **kw))

    def wait(self, predicate, timeout=220):
        for _ in range(5):
            if self.backend.active:
                store = ws.Store(self.state); self.cycles += 1
                with ws.automatic_lease(self.state):
                    outcome = worker.evaluate(store, self.binding, lambda _: deepcopy(self.binding),
                                              self.backend.invocation + ':' + str(self.cycles))
                blocked = outcome['classification'] in ('blocked', 'recovery_required')
                if blocked:
                    store.pause(outcome['code']); self.backend.active = False; self.backend.failed = True; self.backend.pid = '0'
                store.status(phase='recovery_required' if blocked else 'waiting', completed_cycles=self.cycles,
                             identity=worker.process_identity(), outcome=outcome, service_invocation=self.backend.invocation)
            value = self.status()
            if predicate(value): return value
        raise ReadFailure('SYNTHETIC_PREDICATE_NOT_REACHED')

    def stable_failed(self, expected, guard, seconds=check.NO_RESTART_SECONDS):
        for _ in range(2):
            require(self.status()['provider_status'] == expected['provider_status'], 'SYNTHETIC_STATE_CHANGED'); guard()
        return {'seconds': 0, 'samples': 2, 'real_time': False}


class IntegratedFlowTests(unittest.TestCase):
    scenario = acceptance_tests.RealLifecycleTests.scenario

    def test_actual_http_refresh_lifecycle_pending_rollback_with_synthetic_manager(self):
        controllers = []
        def exercise(directory, engine, **kwargs):
            def factory(state):
                result = SimulatedController(state); controllers.append(result); return result
            with patch.object(worker, 'ROOT', directory/'project'):
                return check.exercise(directory, engine, controller_factory=factory, **kwargs)
        report, docker = self.scenario(exercise, 'service')
        self.assertEqual(report['result'], 'passed')
        self.assertEqual(len(report['cases']), 11)
        self.assertEqual(docker.restarts, 3)
        self.assertEqual(controllers[0].backend.starts, 3)
        self.assertFalse(controllers[0].backend.path.exists())
        self.assertFalse(report['failed_unit_observation']['real_time'])
        self.assertFalse(report['production_worker_started'])
        self.assertFalse(report['logout_or_boot_acceptance'])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-service-guard-'); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def create(self, scenario='service'): return base.create_run(self.base, MetadataEngine(), scenario)

    def test_preflight_unavailable_creates_no_resources(self):
        with patch.object(check, 'native_preflight', side_effect=ReadFailure('SYSTEMD_USER_MANAGER_UNAVAILABLE')), \
             patch.object(base, 'Engine') as engine:
            with self.assertRaisesRegex(ReadFailure, 'SYSTEMD_USER_MANAGER_UNAVAILABLE'): base.isolated_run(self.base, 'service')
        engine.assert_not_called(); self.assertEqual(list(self.base.iterdir()), [])

    def test_cleanup_cannot_use_foreign_scenario(self):
        directory, _ = self.create('watch')
        with patch.object(check.subprocess, 'run') as run:
            with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_SCENARIO_INVALID'): check.cleanup_unit_subprocess(directory)
        run.assert_not_called()

    def test_copied_entry_and_service_entry_import_without_test_dependencies(self):
        directory, _ = self.create()
        self.assertEqual(worker.code_hash(directory/'project'), worker.code_hash(base.ROOT))
        for name in ('service_acceptance.py', 'service_entry.py'):
            script = directory/'project/src/ops/platform/ops'/name
            p = subprocess.run([sys.executable, '-I', '-B', str(script), '--help'], cwd=directory, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_modified_copied_cleanup_code_refuses_execution(self):
        directory, _ = self.create(); path = directory/'project/src/ops/platform/ops/service_entry.py'
        path.write_text(path.read_text() + '\n# changed\n')
        with patch.object(check.subprocess, 'run') as run:
            with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_SOURCE_CHANGED'): check.cleanup_unit_subprocess(directory)
        run.assert_not_called()

    def test_cleanup_child_runs_isolated_and_preserves_explicit_session_environment(self):
        directory, _ = self.create()
        def invoke(args, **kw):
            self.assertEqual(args[1:3], ['-I', '-B'])
            self.assertEqual(args[4], 'cleanup-unit')
            self.assertNotIn('PYTHONPATH', kw['env']); self.assertNotIn('OPS_ACCESS_CODE', kw['env'])
            self.assertEqual(kw['env']['XDG_RUNTIME_DIR'], '/explicit/user/runtime')
            return subprocess.CompletedProcess(args, 0, json.dumps({'result':'passed','worker_exit_verified':True}), '')
        with patch.object(check.subprocess, 'run', side_effect=invoke), \
             patch.dict(os.environ, {'PYTHONPATH':'/foreign','OPS_ACCESS_CODE':'never-print','XDG_RUNTIME_DIR':'/explicit/user/runtime'}):
            self.assertEqual(check.cleanup_unit_subprocess(directory)['result'], 'passed')

    def test_cleanup_timeout_is_uncertain_not_success(self):
        directory, _ = self.create()
        with patch.object(check.subprocess, 'run', side_effect=subprocess.TimeoutExpired('unit-cleanup', 280)):
            with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_UNIT_CLEANUP_UNCERTAIN'): check.cleanup_unit_subprocess(directory)

    def test_failed_cleanup_prevents_any_container_removal_even_via_generic_engine(self):
        directory, _ = self.create()
        engine = object.__new__(base.Engine)
        with patch.object(check, 'cleanup_unit_subprocess', side_effect=ReadFailure('SERVICE_STILL_RUNNING')), \
             patch.object(engine, 'owned') as owned, patch.object(engine, 'command') as command:
            with self.assertRaisesRegex(ReadFailure, 'SERVICE_STILL_RUNNING'): engine.cleanup(directory)
        owned.assert_not_called(); command.assert_not_called()

    def test_unit_cleanup_precedes_docker_and_is_in_report(self):
        directory, _ = self.create(); events=[]
        engine=object.__new__(base.Engine)
        def unit(_): events.append('unit-exit-verified'); return {'result':'passed','worker_exit_verified':True}
        def owned(*_): events.append('containers'); return []
        with patch.object(check, 'cleanup_unit_subprocess', side_effect=unit), patch.object(engine, 'owned', side_effect=owned), \
             patch.object(engine, 'command', return_value=''), patch.object(engine, 'protect'):
            result=engine.cleanup(directory)
        self.assertEqual(events[0], 'unit-exit-verified'); self.assertTrue(result['service']['worker_exit_verified'])

    def test_cleanup_without_preparation_creates_no_unit_or_worker(self):
        directory, _ = self.create()
        with patch.object(check, 'ROOT', directory/'project'), patch.object(check, 'Controller') as ctl:
            result=check.cleanup_unit(directory)
        self.assertFalse(result['unit_created']); ctl.assert_not_called()

    def test_foreign_owner_refused_before_manager_control(self):
        directory, record = self.create(); state=directory/'state';state.mkdir(mode=0o700)
        ws.write(state/'owner.json', {'project_root':'/production','compose_project':record['prefix']+'-ops',
                                     'uid':os.getuid(),'gid':os.getgid()})
        with patch.object(check, 'ROOT', directory/'project'):
            with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_SERVICE_OWNER_MISMATCH'): check.cleanup_unit(directory)

    def test_wrong_source_root_refuses_cleanup(self):
        directory, _ = self.create()
        with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_WORKER_SOURCE_MISMATCH'): check.cleanup_unit(directory)

    def test_run_cli_chooses_service_scenario(self):
        with patch.object(sys, 'argv', ['service_acceptance.py','run','--state-dir',str(self.base)]), \
             patch.object(base, 'run', return_value={'result':'passed'}) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(check.main(), 0)
        run.assert_called_once_with(self.base, scenario='service')

    def test_cli_rejects_run_with_existing_run_directory(self):
        with patch.object(sys, 'argv', ['service_acceptance.py','run','--run-dir',str(self.base)]), \
             patch.object(base, 'run') as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check.main(), 2)
        run.assert_not_called(); self.assertEqual(json.loads(output.getvalue())['code'], 'ACCEPTANCE_RUN_DIR_NOT_ALLOWED')

    def test_wait_times_out_without_mistaking_old_observation_for_current(self):
        c=object.__new__(check.Controller)
        with patch.object(c, 'status', return_value={'current_invocation_verified':False}), \
             patch.object(check.time, 'monotonic', side_effect=[0,1]):
            with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_SERVICE_WAIT_TIMEOUT'): c.wait(check.healthy, timeout=0)

    def test_stability_window_rejects_changed_invocation_or_status(self):
        c=object.__new__(check.Controller)
        expected={'provider_status':{'InvocationID':'a'},'worker':{'last_observation':{'n':1}}}
        changed=deepcopy(expected);changed['provider_status']['InvocationID']='b'
        with patch.object(c, 'status', return_value=changed):
            with self.assertRaisesRegex(ReadFailure, 'ACCEPTANCE_FAILED_UNIT_RESTARTED'): c.stable_failed(expected,lambda:None)

    def installed(self):
        directory, record = self.create(); state=directory/'state'; state.mkdir(mode=0o700)
        owner={'schema':1,'owner':record['owner'],'project_root':str(directory/'project'),
               'compose_project':record['prefix']+'-ops','uid':os.getuid(),'gid':os.getgid()}
        ws.write(state/'owner.json',owner)
        controller=object.__new__(check.Controller); controller.state=state
        config=directory/'test-config'
        controller.reader=probe.Reader(env={'XDG_CONFIG_HOME':str(config),'PATH':os.defpath})
        controller.backend=Manager(config,'agent-project-ops-refresh-'+owner['owner'].replace('-','')+'.service')
        binding={'directory':str(state),'owner':owner,'python':sys.executable,'docker_path':'/synthetic/docker'}
        report={'docker':{'endpoint':'unix:///synthetic/docker.sock'},'persistence':{'selected':'session'}}
        manifest,_=service.install(state,report,controller.reader,controller.backend,30,lambda _:binding)
        return directory,state,manifest,controller

    def test_owned_inactive_unit_cleanup_and_repeat_preserve_audit(self):
        directory,state,manifest,controller=self.installed()
        with patch.object(check,'ROOT',directory/'project'), patch.object(worker,'ROOT',directory/'project'):
            first=check.cleanup_unit(directory,lambda _:controller)
            second=check.cleanup_unit(directory,lambda _:controller)
        self.assertTrue(first['unit_absence_verified']);self.assertTrue(second['worker_exit_verified'])
        self.assertFalse(controller.backend.path.exists());self.assertEqual(service.load(state)['phase'],'removed')
        self.assertTrue(ws.location(state).is_dir());self.assertTrue(service.manifest_path(state).exists())

    def test_changed_unit_pauses_but_refuses_removal(self):
        directory,state,manifest,controller=self.installed();controller.backend.path.write_text('foreign content')
        with patch.object(check,'ROOT',directory/'project'), patch.object(worker,'ROOT',directory/'project'):
            with self.assertRaisesRegex(ReadFailure,'SERVICE_UNIT_CHANGED'): check.cleanup_unit(directory,lambda _:controller)
        self.assertEqual(controller.backend.path.read_text(),'foreign content')
        self.assertEqual(ws.Store(state).control()['desired'],'paused')

    def test_partial_install_with_no_unit_can_clean_containers_without_erasing_audit(self):
        directory,state,manifest,controller=self.installed();controller.backend.path.unlink()
        manifest['phase']='install_failed';ws.write(service.manifest_path(state),manifest)
        with patch.object(check,'ROOT',directory/'project'):
            report=check.cleanup_unit(directory,lambda _:controller)
        self.assertTrue(report['unit_absence_verified']);self.assertEqual(service.load(state)['phase'],'install_failed')

    def test_partial_install_with_remaining_unit_requires_review(self):
        directory,state,manifest,controller=self.installed()
        manifest['phase']='installing';ws.write(service.manifest_path(state),manifest)
        with patch.object(check,'ROOT',directory/'project'):
            with self.assertRaisesRegex(ReadFailure,'ACCEPTANCE_PARTIAL_UNIT_REQUIRES_REVIEW'):
                check.cleanup_unit(directory,lambda _:controller)
        self.assertTrue(controller.backend.path.exists())


class SupervisorTests(unittest.TestCase):
    def scenario(self, failure=None, cleanup_failure=False, child_report=None):
        temp=tempfile.TemporaryDirectory(prefix='native-service-parent-');self.addCleanup(temp.cleanup)
        events=[];test=self
        class Engine(MetadataEngine):
            def compose(self,*_): events.append('compose')
            def cleanup(self,directory):
                events.append('unit-cleanup')
                if cleanup_failure: raise ReadFailure('SERVICE_STILL_RUNNING')
                events.append('container-cleanup');return {'service':{'worker_exit_verified':True}}
        class Child:
            def __init__(self,args,**kw):
                events.append('worker');test.assertTrue(args[2].endswith('/service_acceptance.py'))
                test.assertTrue(kw['start_new_session'])
                base.write(Path(args[-1])/'worker-report.json',child_report or {'stage':check.STAGE,'result':'passed'})
            def wait(self,timeout):
                test.assertEqual(timeout,900)
                if failure: raise failure
                return 0
        with patch.object(check,'native_preflight',side_effect=lambda:events.append('preflight')), \
             patch.object(base,'Engine',Engine), patch.object(base.subprocess,'Popen',Child), \
             patch.object(base,'stop_worker',side_effect=lambda _:events.append('orchestrator-stop')):
            result=base.run(Path(temp.name),'service')
        return result,events

    def test_actual_scenario_selected_and_unit_cleanup_precedes_containers(self):
        result,events=self.scenario()
        self.assertEqual(result['result'],'passed');self.assertEqual(events[0],'preflight')
        self.assertEqual(events[-2:],['unit-cleanup','container-cleanup'])

    def test_timeout_stops_orchestrator_then_cleans_independent_service(self):
        result,events=self.scenario(subprocess.TimeoutExpired('worker',900))
        self.assertEqual(result['result'],'attention_required')
        self.assertEqual(events[-3:],['orchestrator-stop','unit-cleanup','container-cleanup'])
        self.assertTrue(result['test_service_requested']);self.assertFalse(result['production_worker_started'])

    def test_keyboard_interrupt_still_attempts_unit_cleanup(self):
        result,events=self.scenario(KeyboardInterrupt())
        self.assertEqual(result['code'],'ACCEPTANCE_INTERRUPTED')
        self.assertEqual(events[-3:],['orchestrator-stop','unit-cleanup','container-cleanup'])

    def test_cleanup_failure_cannot_return_passed_or_remove_containers(self):
        result,events=self.scenario(cleanup_failure=True)
        self.assertEqual(result['result'],'attention_required');self.assertNotIn('container-cleanup',events)
        self.assertEqual(result['cleanup']['code'],'SERVICE_STILL_RUNNING')

    def test_worker_command_failure_context_survives_parent_cleanup_without_raw_fields(self):
        report={'stage':check.STAGE,'result':'attention_required','code':'SERVICE_COMMAND_FAILED',
                'failure_context':{'action':'resume','stderr':'secret',
                 'service_command':{'operation':'reset_failed','outcome':'nonzero_exit','returncode':1,'args':['secret']}}}
        result,events=self.scenario(child_report=report)
        self.assertEqual(result['code'],'SERVICE_COMMAND_FAILED')
        self.assertEqual(result['failure_context'],{'action':'resume',
            'service_command':{'operation':'reset_failed','outcome':'nonzero_exit','returncode':1}})
        self.assertNotIn('secret',json.dumps(result))
        self.assertEqual(events[-2:],['unit-cleanup','container-cleanup'])


if __name__ == '__main__': unittest.main()
