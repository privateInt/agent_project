"""Service projection into the existing read-only installation preflight."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from ops_platform.ops import service_lifecycle as service, service_systemd, worker_state as ws
from ops_platform.tests import test_auto_refresh as fixtures
from ops_platform.tests import test_service_lifecycle as synthetic


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.PreflightTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.directory = self.h.f.directory
        owner = ws.read(self.directory / 'owner.json')
        config = self.h.f.root / 'unit-config'
        self.h.reader.original['XDG_CONFIG_HOME'] = str(config)
        self.backend = synthetic.Manager(config, 'agent-project-ops-refresh-' + owner['owner'].replace('-', '') + '.service')
        self.binding = {'directory': str(self.directory), 'owner': owner, 'code_sha256': 'a'*64,
            'python': str(Path(sys.executable).resolve()), 'python_version': list(sys.version_info[:3]),
            'docker_path': '/synthetic/bin/docker', 'daemon_id': 'synthetic', 'ops_container_id': 'b'*64, 'ops_image_id': 'sha256:'+'c'*64}
        patched = patch.object(service_systemd, 'Systemd', return_value=self.backend)
        patched.start(); self.addCleanup(patched.stop)
        r = service.run('install', self.directory, reader=self.h.reader, backend=self.backend,
                        snapshot_fn=lambda _: self.binding)
        self.assertEqual(r['result'], 'passed', r)

    def test_owned_inactive_unit_status_is_readonly_and_separates_capability(self):
        before = fixtures.tree(self.h.f.root); controls = self.backend.control_commands
        report = self.h.run_probe('status')
        self.assertEqual(report['result'], 'passed', report)
        self.assertEqual(report['management']['status'], 'owned')
        self.assertEqual(report['desired_state'], 'paused')
        self.assertEqual(report['evaluation_status'], 'not_running')
        self.assertFalse(report['native_service_acceptance_verified'])
        self.assertEqual(before, fixtures.tree(self.h.f.root)); self.assertEqual(controls, self.backend.control_commands)

    def test_recovery_latch_and_open_intent_are_preserved_as_blockers(self):
        store = ws.Store(self.directory); store.pause('SYNTHETIC_RECOVERY')
        report = self.h.run_probe()
        self.assertIn('WORKER_RECOVERY_REQUIRED', report['blockers'])
        ws.write(store.path / 'intent.json', {'phase': 'open'})
        report = self.h.run_probe()
        self.assertIn('WORKER_UNFINISHED_INTENT', report['blockers'])
        self.assertEqual(store.optional('intent.json')['phase'], 'open')

    def test_manual_unit_edit_makes_plan_conflict_without_repair(self):
        self.backend.path.write_text('manual contents')
        report = self.h.run_probe()
        self.assertIn('SERVICE_UNIT_CHANGED', report['blockers'])
        self.assertEqual(self.backend.path.read_text(), 'manual contents')

    def test_real_recovery_transaction_obeys_stop_token_before_clearing_intent(self):
        from ops_platform.contracts import ReadFailure
        from ops_platform.ops import refresh_worker as worker
        from ops_platform.tests import test_refresh_worker as cases
        h = cases.GuardTests(); h.setUp(); self.addCleanup(h.doCleanups)
        h.h.changed()
        with patch.object(worker.Guard, 'complete', side_effect=OSError('synthetic lost write')):
            result = h.call()
        h.store.pause(result['code']); service.request_pause(h.directory)
        intent = h.store.optional('intent.json'); control = h.store.control(); restarts = h.h.docker.restarts
        with self.assertRaisesRegex(ReadFailure, 'SERVICE_STOP_REQUEST_CHANGED'):
            worker.acknowledge_recovery(h.directory, h.snapshot, h.options(),
                resume_guard=lambda store: service.stop_token_unchanged(store, None))
        self.assertEqual(h.store.optional('intent.json'), intent)
        self.assertEqual(h.store.control(), control); self.assertEqual(h.h.docker.restarts, restarts)
