"""Real Linux process/lease/deadline checks, synthetic evaluation and private temp files.

Uses the standard library only. No Docker commands, real prepared state,
application credentials or network requests are used by these checks.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from ops_platform.contracts import ReadFailure
from ops_platform.ops import refresh_worker as worker, worker_state as ws, manage


def accepted(*_): return {'result': 'passed', 'classification': 'accepted', 'generation': None}


def running(pid):
    try: return Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError: return False


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ops-worker-check-'); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'server'; self.directory.mkdir(mode=0o700)
        manage.write_new(self.directory / 'owner.json', {'schema': 1, 'uid': os.getuid(), 'gid': os.getgid(),
            'owner': str(uuid.uuid4()), 'project_root': str(worker.ROOT)})
        self.binding = {'scope': 'isolated-synthetic-process-check', 'directory': str(self.directory)}
        self.snapshot = lambda _: self.binding

    def call(self, evaluate_fn=accepted, cycles=1, **kwargs):
        return worker.run(self.directory, cycles=cycles, deadline_seconds=kwargs.pop('deadline_seconds', 2),
            snapshot_fn=self.snapshot, evaluate_fn=evaluate_fn, _wait=lambda store: None, **kwargs)

    def test_bounded_cycles_and_new_invocation_without_stale_replay(self):
        result = self.call(cycles=3)
        self.assertEqual(result['completed_cycles'], 3); self.assertEqual(result['phase'], 'bounded_complete')
        self.assertTrue(result['last_outcome']['child_process_group_stopped'])
        self.assertEqual(self.call()['completed_cycles'], 1)

    def test_real_lease_is_inherited_and_blocks_a_second_process(self):
        def child(store, *_):
            try:
                with ws.automatic_lease(store.directory): raise AssertionError('duplicate owner')
            except ReadFailure as exc:
                if exc.code != 'AUTOMATIC_WORKER_BUSY': raise
            return accepted()
        self.assertEqual(self.call(child)['result'], 'passed')

    def test_deadline_kills_cycle_and_subprocess_and_latches(self):
        def child(store, *_):
            command = "from pathlib import Path; import time; Path('child-pid').write_text(Path('/proc/self/stat').read_text().split(' ',1)[0]); time.sleep(20)"
            subprocess.Popen([sys.executable, '-B', '-c', command], cwd=store.directory)
            time.sleep(20)
        start = time.monotonic(); result = self.call(child, deadline_seconds=1)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(result['exit_code'], 2)
        self.assertIn(result['last_outcome']['code'], ('WORKER_DEADLINE_EXCEEDED', 'WORKER_CHILD_INTERRUPTED'))
        self.assertFalse(running(int((self.directory / 'child-pid').read_text())))
        with self.assertRaisesRegex(ReadFailure, 'WORKER_RECOVERY_REQUIRED'): self.call()

    def test_post_intent_timeout_is_recovery_required(self):
        def child(store, binding, snapshot, evaluation):
            ws.write(store.path / 'intent.json', {'phase': 'open', 'evaluation': evaluation, 'generation': 1})
            time.sleep(20)
        result = self.call(child, deadline_seconds=1)
        self.assertEqual(result['last_outcome']['classification'], 'recovery_required')
        self.assertEqual(ws.Store(self.directory).optional('intent.json')['phase'], 'open')

    def test_durable_prewrite_pause_cancels_child_and_prevents_restart(self):
        def child(store, *_): store.pause(); time.sleep(20)
        result = self.call(child, cycles=3)
        self.assertEqual(result['phase'], 'stopped'); self.assertEqual(result['completed_cycles'], 1)
        self.assertEqual(result['last_outcome']['code'], 'WORKER_STOPPED_PREWRITE')
        with self.assertRaisesRegex(ReadFailure, 'WORKER_PAUSED'): self.call()

    def test_sigterm_during_post_intent_finishes_one_transaction_then_stops(self):
        def child(store, binding, snapshot, evaluation):
            intent = {'phase': 'open', 'evaluation': evaluation, 'generation': 1}
            ws.write(store.path / 'intent.json', intent)
            os.kill(os.getppid(), signal.SIGTERM); time.sleep(.12)
            intent['phase'] = 'completed'; ws.write(store.path / 'intent.json', intent)
            return accepted()
        result = self.call(child, cycles=3)
        self.assertEqual(result['phase'], 'stopped'); self.assertEqual(result['completed_cycles'], 1)
        self.assertEqual(result['last_outcome']['classification'], 'accepted')

    def test_wait_wakes_on_persistent_pause_without_another_cycle(self):
        result = worker.run(self.directory, cycles=4, snapshot_fn=self.snapshot, evaluate_fn=accepted,
                            _wait=lambda store: store.pause())
        self.assertEqual(result['completed_cycles'], 1); self.assertEqual(result['phase'], 'stopped')

    def test_retryable_prewrite_degrades_then_recovers_without_latch(self):
        def child(store, binding, snapshot, evaluation):
            if evaluation.endswith(':1'):
                return {'result': 'attention_required', 'classification': 'retryable_prewrite', 'code': 'TIMEOUT'}
            return accepted()
        result = self.call(child, cycles=2)
        self.assertEqual(result['result'], 'passed'); self.assertEqual(result['completed_cycles'], 2)
        self.assertIsNone(ws.Store(self.directory).control()['recovery'])

    def test_unknown_child_exit_latches_and_does_not_reuse_old_result(self):
        self.call()
        result = self.call(lambda *_: os._exit(0), cycles=3)
        self.assertEqual(result['completed_cycles'], 1); self.assertEqual(result['exit_code'], 2)
        self.assertEqual(result['last_outcome']['code'], 'WORKER_CHILD_INTERRUPTED')

    def test_stale_invocation_cannot_be_interpreted_as_clean_restart(self):
        store = ws.Store(self.directory, self.binding)
        store.status(phase='evaluating', invocation='old', identity={'pid': 1, 'boot_id': 'old'}, completed_cycles=0)
        with self.assertRaisesRegex(ReadFailure, 'WORKER_PREVIOUS_INVOCATION_INTERRUPTED'): self.call()
        self.assertIsNotNone(store.control()['recovery'])

    def test_readonly_status_creates_nothing_and_does_not_trust_saved_pid(self):
        before = list(self.directory.parent.iterdir())
        self.assertEqual(ws.status(self.directory), {'status': 'not_initialized'})
        self.assertEqual(list(self.directory.parent.iterdir()), before)
        self.call(); result = ws.status(self.directory)
        self.assertFalse(result['live_process_verified']); self.assertFalse(result['service_installed'])

    def test_supervisor_loss_keeps_lease_until_child_deadline_then_blocks_reentry(self):
        def evaluation(store, binding, snapshot, evaluation_id):
            ws.write(store.path / 'intent.json', {'phase': 'open', 'evaluation': evaluation_id, 'generation': 1})
            (store.directory / 'ready').write_text('ready')
            time.sleep(20)
        supervisor = os.fork()
        if supervisor == 0:
            try: self.call(evaluation, deadline_seconds=1)
            finally: os._exit(0)
        try:
            deadline = time.monotonic() + 3
            while not (self.directory / 'ready').exists():
                self.assertLess(time.monotonic(), deadline); time.sleep(.01)
            os.kill(supervisor, signal.SIGKILL)
            # The cycle inherits the same open lease description.
            with self.assertRaisesRegex(ReadFailure, 'AUTOMATIC_WORKER_BUSY'):
                with ws.automatic_lease(self.directory): pass
            while True:
                try:
                    with ws.automatic_lease(self.directory): break
                except ReadFailure as exc:
                    self.assertEqual(exc.code, 'AUTOMATIC_WORKER_BUSY')
                    self.assertLess(time.monotonic(), deadline); time.sleep(.02)
            with self.assertRaisesRegex(ReadFailure, 'WORKER_UNFINISHED_INTENT'): self.call()
        finally:
            # Direct child remains unreaped; this PID cannot refer to another process.
            os.kill(supervisor, signal.SIGKILL); os.waitpid(supervisor, 0)

    def test_lease_symlink_and_unowned_files_are_rejected(self):
        path = self.directory / 'automatic-refresh.lock'; path.symlink_to(self.directory / 'owner.json')
        with self.assertRaisesRegex(ReadFailure, 'SYMLINK_REFUSED'): self.call()
        path.unlink(); path.touch(mode=0o600); path.chmod(0o644)
        with self.assertRaisesRegex(ReadFailure, 'PRIVATE_STATE_REQUIRED'): self.call()


if __name__ == '__main__': unittest.main()
