"""D29 M01-M07: private SQLite/files and explicit offline transport/process doubles.

Run this file directly; no legacy acceptance class is registered or discovered.
Stage process doubles validate capture plumbing, never native execution/origin.
"""
import argparse
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).absolute().parents[4]
sys.path.insert(0, str(ROOT))

# Keep the old module behind a module attribute: its TestCase is never exported.
_spec = importlib.util.spec_from_file_location(
    '_d29_legacy_helpers', Path(__file__).with_name('test_codex_owner_cli_controller.py'))
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)
q, turns, transport = legacy.q, legacy.turns, legacy.transport
from src.ops.tools import codex_owner_stage_results as stages

METHODS = tuple('test_M%02d' % index for index in range(1, 8))
TEST_IDS = tuple('MessageCorrelationAcceptance.' + name for name in METHODS)
NEW_TEST = 'src/ops/tools/tests/test_codex_owner_message_correlation.py'
SOURCE_PATHS = tuple(sorted(stages.REQUIRED_SOURCE_PATHS))
STAGE_FLAGS = ('input_origin_verified', 'independent_review_verified', 'trusted_gate_verified',
               'native_workflow_verified', 'native_ready', 'merge_authorized', 'replay_permitted')
OBSERVATIONS = []


@contextlib.contextmanager
def offline_guards():
    with mock.patch.object(socket, 'socket', side_effect=AssertionError('LIVE_SOCKET_FORBIDDEN')), \
         mock.patch.object(transport, '_DeadlineSocket', side_effect=AssertionError('LIVE_SOCKET_SUBCLASS_FORBIDDEN')), \
         mock.patch.object(subprocess, 'Popen', side_effect=legacy.guarded_popen):
        yield


def private_write(path, raw):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    return path


def page(*values, cursor=None):
    return {'data': list(values), 'nextCursor': cursor}


class AutoFixture(legacy.Fixture):
    def __init__(self, *, task_id=None, body='  D29 한글 e\u0301\n`literal` $(not-shell)\n '):
        super().__init__()
        self.intent['task_id'] = task_id or str(uuid.uuid4())
        self.body = body
        self.prompt = q.task_message(self.intent['task_id'], body)
        self.repin()

    def repin(self):
        self.intent['prompt_sha256'] = q.sha(self.prompt.encode('utf8'))
        self.raw = q.canonical(self.intent)
        self.pin = q.sha(self.raw)
        self.key = tuple(self.intent[k] for k in ('project_id', 'batch_id', 'task_id'))

    @property
    def folder(self):
        return self.records / self.pin

    def prepare_auto(self, *, pages=None):
        self.pre_owner = self.owner(pages if pages is not None else [page()])
        self.prepared = self.controller.prepare_automatic(
            self.raw, self.pin, 'test_agent', self.prompt, self.pre_owner)
        self.request_pin = self.prepared['request_sha256']
        self.fence_pin = self.prepared['automatic_fence_sha256']
        return self.prepared

    def submit_auto(self, runner=None, **changes):
        kwargs = {'runner': runner or self.runner, 'automatic_fence_sha256': self.fence_pin}
        kwargs.update(changes)
        self.result = self.controller.submit(self.raw, self.pin, 'test_agent', self.prompt, **kwargs)
        return self.result

    def start_auto(self, *, pages=None):
        self.prepare_auto(pages=pages)
        self.submit_auto()
        return self

    def observe_auto(self, client=None):
        return self.controller.observe_automatic(self.pin, self.request_pin, client or self.owner())

    def named_turn(self, name, *, prompt=None, **kwargs):
        value = self.turn(prompt=prompt, **kwargs)
        value['id'] = 'turn-' + name
        value['items'][0]['id'] = 'user-' + name
        value['items'][1]['id'] = 'final-' + name
        return value


def duplicate_exact_pages(f):
    """Distinct IDs ensure only the exact-candidate-count guard rejects this."""
    return [page(f.named_turn('first'), cursor='later'), page(f.named_turn('second'))]


class CaptureDouble:
    """Explicit Popen replacement; no process is created by this double."""
    def __init__(self, case, fixture, context, payload=None, returncode=0):
        self.case, self.fixture, self.context = case, fixture, context
        self.payload, self.rc, self.calls = payload, returncode, []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append((list(argv), kwargs))
        self.case.assertEqual(argv[:4], ['/usr/bin/python3', '-I', '-S', '-B'])
        self.case.assertEqual(Path(argv[4]), self.fixture.repo / NEW_TEST)
        self.case.assertIn('--context', argv)
        self.case.assertIn('--result', argv)
        context_path = Path(argv[argv.index('--context') + 1])
        result_path = Path(argv[argv.index('--result') + 1])
        self.case.assertEqual(json.loads(context_path.read_bytes()), self.context)
        self.case.assertEqual(argv[argv.index('--result') + 2:], list(TEST_IDS))
        self.case.assertEqual(Path(kwargs['cwd']), self.fixture.repo)
        self.case.assertFalse(kwargs.get('shell', False))
        self.case.assertEqual(context_path.stat().st_mode & 0o777, 0o600)
        self.case.assertTrue(str(result_path).startswith(str(self.fixture.root)))
        payload = self.payload
        if payload is None:
            payload = {'schema': 1, 'stage': 'tests', 'context': self.context,
                       'test_ids': list(TEST_IDS), 'run': 7, 'failures': 0, 'errors': 0, 'skips': 0}
        if payload != 'NO_RESULT':
            raw = payload if type(payload) is bytes else q.canonical(payload)
            # A child can be handed a precreated private result file.
            if result_path.exists():
                result_path.write_bytes(raw)
            else:
                private_write(result_path, raw)
        outer = self

        class Process:
            returncode = outer.rc
            pid = 99999999

            def communicate(self, input=None, timeout=None):
                outer.case.assertIsNotNone(timeout)
                outer.case.assertGreater(timeout, 0)
                outer.case.assertLessEqual(timeout, 60)
                return b'', b''

            def wait(self, timeout=None):
                if timeout is not None:
                    outer.case.assertLessEqual(timeout, 60)
                return self.returncode

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

            def terminate(self):
                self.returncode = -15

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        return Process()


def stage_fixture():
    f = AutoFixture().start_auto()
    observed = f.observe_auto()
    if observed.get('status') != 'TURN_COMPLETED':
        raise RuntimeError('STAGE_FIXTURE_COMPLETION_REQUIRED')
    f.repo = f.root / 'source-checkout'
    f.repo.mkdir(mode=0o700)
    manifest = {}
    f.sources = {}
    for name in SOURCE_PATHS:
        raw = (ROOT / name).read_bytes()
        private_write(f.repo / name, raw)
        f.sources[name] = raw
        manifest[name] = q.sha(raw)
    f.manifest = q.canonical(manifest)
    f.context = {'attempt_id': str(uuid.uuid4()), 'repository': f.intent['repository'],
                 'base_sha': f.intent['base_sha'], 'head_sha': 'b' * 40,
                 'plan_sha256': f.intent['plan_sha256'], 'source_manifest_sha256': q.sha(f.manifest)}
    f.expanded = dict(f.context, task_id=f.intent['task_id'], intent_sha256=f.pin,
                      request_sha256=f.request_pin)
    f.stages = stages.StageResults(f.controller, f.pin, f.request_pin, f.context, f.manifest)
    return f


def reviewer_blob(f, tests_record, **changes):
    value = {'schema': 1, 'stage': 'reviewer', 'context': dict(f.expanded), 'decision': 'PASS',
             'tests_artifact_sha256': tests_record['artifact_sha256'], 'findings': []}
    value.update(changes)
    return q.canonical(value)


def draft_blob(f, **changes):
    value = {'head_sha': 'c' * 40, 'base_sha': f.context['base_sha'],
             'head_repo_full_name': f.context['repository'], 'base': 'main',
             'head': 'codex/d29-fixture', 'draft': True, 'state': 'open', 'number': 29,
             'url': 'https://github.com/' + f.context['repository'] + '/pull/29'}
    value.update(changes)
    return q.canonical(value)


class MessageCorrelationAcceptance(unittest.TestCase):
    def setUp(self):
        self.guards = offline_guards()
        self.guards.__enter__()
        self.addCleanup(self.guards.__exit__, None, None, None)

    def rejected(self, call):
        try:
            result = call()
        except (q.files.PackageError, turns.TurnObservationError, ValueError, TypeError):
            return {'status': 'REJECTED'}
        self.assertIsInstance(result, dict)
        self.assertNotIn(result.get('status'), ('PREPARED', 'TURN_COMPLETED', 'TESTS_PASSED',
                                              'REVIEW_PASSED', 'DRAFT_RECORDED'))
        return result

    def no_terminal(self, f):
        self.assertFalse((f.folder / 'terminal.json').exists())

    def unknown(self, f, count):
        row = f.read_row()
        self.assertEqual((row['state'], row['revision'], row['intent_bytes']),
                         ('SUBMISSION_UNKNOWN', 1, f.raw))
        self.assertEqual(len(f.calls), count)

    def false_turn_flags(self, result):
        for key in ('native_ready', 'native_ack_verified', 'binding_origin_verified',
                    'client_id_identity_verified', 'replay_permitted'):
            self.assertIs(result[key], False, key)

    def false_stage_flags(self, record):
        for key in STAGE_FLAGS:
            self.assertIs(record[key], False, key)
        for key in ('trusted_gate', 'trusted_gate_verified', 'native_ack_verified'):
            if key in record:
                self.assertIs(record[key], False, key)
        self.assertTrue(record['origin'])

    def capture(self, f, payload=None, rc=0):
        double = CaptureDouble(self, f, f.expanded, payload, rc)
        with mock.patch.object(subprocess, 'Popen', side_effect=double):
            record = f.stages.capture_tests(f.repo)
        self.assertEqual(len(double.calls), 1)
        return record

    def ready_review(self, f):
        tests_record = self.capture(f)
        review = f.stages.record_reviewer(reviewer_blob(f, tests_record),
                                           'fixture-independent-review', 'fixture-author')
        self.assertEqual(review['status'], 'REVIEW_PASSED')
        return tests_record, review

    def test_M01(self):
        """Exact message bytes and write-before-dispatch task identity."""
        task = '12345678-1234-4abc-8def-123456789abc'
        prefix = '[agent-project-task:v1:' + task + ']\n'
        for body in (' space \n', '\n한글 e\u0301\r\n', '`literal` $(never-shell)', 'x' * (16384-len(prefix))):
            with self.subTest(body_bytes=len(body.encode('utf8'))):
                message = q.task_message(task, body)
                self.assertEqual(message, prefix + body)
                self.assertLessEqual(len(message.encode('utf8')), 16384)
        for bad_task in (task.upper(), task.replace('-', ''), str(uuid.uuid1()), '', None):
            with self.subTest(bad_task=bad_task):
                with self.assertRaises((q.files.PackageError, ValueError, TypeError)):
                    q.task_message(bad_task, 'body')
        for body in ('', None, b'bytes', 'nul\x00body', prefix + 'again', 'x' * (16385-len(prefix)), '한' * 5500):
            with self.subTest(invalid_body=type(body).__name__):
                with self.assertRaises((q.files.PackageError, ValueError, TypeError)):
                    q.task_message(task, body)
        f = AutoFixture(task_id=task)
        prepared = f.prepare_auto()
        self.assertEqual(prepared['status'], 'PREPARED')
        baseline = f.folder / ('baseline-' + f.fence_pin + '.json')
        self.assertEqual(q.sha(baseline.read_bytes()), f.fence_pin)
        original = baseline.read_bytes()
        mapping = f.records / ('task-' + task + '.json')
        self.assertTrue(mapping.is_file())
        mapping_raw = mapping.read_bytes()
        request_raw = (f.folder / 'request.json').read_bytes()
        self.assertEqual(q.sha(request_raw), f.request_pin)
        self.assertEqual(json.loads(request_raw)['prompt'], f.prompt)
        self.assertNotEqual(task, f.intent['clientUserMessageId'])
        self.assertEqual(q.sha(f.prompt.encode('utf8')), f.intent['prompt_sha256'])

        def ordered_runner(argv):
            self.assertTrue((f.folder / 'dispatch-attempt.json').is_file())
            self.assertEqual(f.read_row()['state'], 'SUBMISSION_UNKNOWN')
            self.assertEqual(mapping.read_bytes(), mapping_raw)
            self.assertEqual(argv[-2:], ['--message', f.prompt])
            return f.runner(argv)

        f.submit_auto(ordered_runner)
        f.submit_auto()
        self.unknown(f, 1)
        self.assertEqual(baseline.read_bytes(), original)
        reopened = f.make_controller(legacy.connect(f.db))
        try:
            self.assertEqual(reopened.read(f.pin, f.request_pin)['status'], 'SUBMISSION_UNKNOWN')
        finally:
            reopened.connection.close()
        # Reuse the same root, changing only the task's batch/context.
        f.intent['batch_id'] += '-conflict'
        f.intent['clientUserMessageId'] += '-conflict'
        f.repin()
        self.rejected(lambda: f.controller.prepare_automatic(f.raw, f.pin, 'test_agent', f.prompt, f.owner([page()])))
        self.assertEqual(mapping.read_bytes(), mapping_raw)
        self.assertEqual(len(f.calls), 1)
        for filename in ('request.json', 'task-' + str(uuid.uuid4()) + '.json'):
            g = AutoFixture()
            actual = 'task-' + g.intent['task_id'] + '.json' if filename.startswith('task-') else filename
            write = q.write_once
            def fail(path, *args, _name=actual, **kwargs):
                if Path(path).name == _name:
                    raise OSError('SYNTHETIC_SAVE_FAILURE')
                return write(path, *args, **kwargs)
            with mock.patch.object(q, 'write_once', side_effect=fail):
                try:
                    self.rejected(g.prepare_auto)
                except OSError:
                    pass
            self.rejected(lambda: g.controller.submit(g.raw, g.pin, 'test_agent', g.prompt,
                runner=g.runner, automatic_fence_sha256='0' * 64))
            self.assertEqual(g.calls, [])

    def test_M02(self):
        """Fresh fences, confirmed claims, loss, contention and legacy compatibility."""
        f = AutoFixture()
        self.rejected(lambda: f.controller.submit(f.raw, f.pin, 'test_agent', f.prompt, runner=f.runner))
        self.assertEqual(f.calls, [])
        f.prepare_auto()
        client = f.owner()
        self.rejected(lambda: f.observe_auto(client))
        self.assertEqual(client.calls, [])
        self.no_terminal(f)
        self.rejected(lambda: f.submit_auto(automatic_fence_sha256='0' * 64))
        self.assertEqual(f.calls, [])
        # A failed prehistory scan or baseline write cannot arm dispatch.
        for mode in ('partial-history', 'write-failure'):
            g = AutoFixture()
            if mode == 'partial-history':
                self.rejected(lambda: g.prepare_auto(pages=[{'data': [], 'nextCursor': 'more'}] * 4))
            else:
                write = q.write_once
                def fail_baseline(path, *args, **kwargs):
                    if Path(path).name.startswith('baseline-'):
                        raise OSError('SYNTHETIC_BASELINE_WRITE_LOST')
                    return write(path, *args, **kwargs)
                with mock.patch.object(q, 'write_once', side_effect=fail_baseline):
                    try:
                        self.rejected(g.prepare_auto)
                    except OSError:
                        pass
            self.assertEqual(g.calls, [])
            self.assertEqual(list(g.folder.glob('baseline-*.json')), [])
        # Either clock expiring or moving backwards invalidates a fence.
        for wall_delta, mono_delta in ((6, 0), (0, 6), (-1, 0), (0, -1)):
            g = AutoFixture()
            with mock.patch.object(q.time, 'time', return_value=1700000000.0), \
                 mock.patch.object(q.time, 'monotonic', return_value=100.0):
                g.prepare_auto()
            with mock.patch.object(q.time, 'time', return_value=1700000000.0 + wall_delta), \
                 mock.patch.object(q.time, 'monotonic', return_value=100.0 + mono_delta):
                self.rejected(g.submit_auto)
            self.assertEqual(g.calls, [])
            self.assertEqual(g.read_row()['state'], 'PREPARED')
        g = AutoFixture()
        with mock.patch.object(q.time, 'time', return_value=1700000000.0), mock.patch.object(q.time, 'monotonic', return_value=100.0):
            g.prepare_auto()
        before = {p: p.read_bytes() for p in g.folder.glob('baseline-*.json')}
        g.prepare_auto()
        self.assertGreater(len(list(g.folder.glob('baseline-*.json'))), len(before))
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        g.submit_auto()
        self.unknown(g, 1)
        saved_fences = {p: p.read_bytes() for p in g.folder.glob('baseline-*.json')}
        no_refresh = g.owner([RuntimeError('CLAIMED_REQUEST_MUST_NOT_REFRESH')])
        existing = g.controller.prepare_automatic(g.raw, g.pin, 'test_agent', g.prompt, no_refresh)
        self.assertEqual(existing['status'], 'SUBMISSION_UNKNOWN')
        self.assertEqual(no_refresh.calls, [])
        self.assertEqual({p: p.read_bytes() for p in g.folder.glob('baseline-*.json')}, saved_fences)
        for after_commit in (False, True):
            g = AutoFixture()
            g.prepare_auto()
            commit, claim = q.journal._commit, q.journal.claim_intent
            def lose(connection):
                if after_commit:
                    commit(connection)
                raise RuntimeError('SYNTHETIC_CONFIRM_LOST')
            def inject(*args, **kwargs):
                with mock.patch.object(q.journal, '_commit', side_effect=lose):
                    return claim(*args, **kwargs)
            with mock.patch.object(q.journal, 'claim_intent', side_effect=inject):
                self.rejected(g.submit_auto)
            self.assertEqual(g.calls, [])
            self.assertEqual(g.read_row()['state'], 'SUBMISSION_UNKNOWN' if after_commit else 'PREPARED')
            if after_commit:
                g.submit_auto()
                self.assertEqual(g.calls, [])
        for filename, call_count in (('dispatch-attempt.json', 0), ('stdout.bin', 1), ('cli-result.json', 1)):
            g = AutoFixture()
            g.prepare_auto()
            write = q.write_once
            def fail(path, *args, **kwargs):
                if Path(path).name == filename:
                    raise OSError('SYNTHETIC_DURABILITY_FAILURE')
                return write(path, *args, **kwargs)
            with mock.patch.object(q, 'write_once', side_effect=fail):
                self.rejected(g.submit_auto)
            g.submit_auto()
            self.unknown(g, call_count)
        g = AutoFixture()
        g.prepare_auto()
        def lost_result(argv):
            g.runner(argv)
            raise RuntimeError('SYNTHETIC_RESULT_LOST')
        g.submit_auto(lost_result)
        g.submit_auto()
        self.unknown(g, 1)
        # Admission and commit delays must be rechecked at the claim boundary.
        for delayed_phase, state in (('admission','PREPARED'),('claim','SUBMISSION_UNKNOWN')):
            g = AutoFixture()
            g.prepare_auto()
            delay = [0.0]
            monotonic, wall = q.time.monotonic, q.time.time
            admit, claim = q.files.admit, q.journal.claim_intent
            def slow_admit(*args, **kwargs):
                answer = admit(*args, **kwargs)
                if delayed_phase == 'admission': delay[0] = 6.0
                return answer
            def slow_claim(*args, **kwargs):
                answer = claim(*args, **kwargs)
                if delayed_phase == 'claim': delay[0] = 6.0
                return answer
            with mock.patch.object(q.time,'monotonic',side_effect=lambda:monotonic()+delay[0]), \
                 mock.patch.object(q.time,'time',side_effect=lambda:wall()+delay[0]), \
                 mock.patch.object(q.files,'admit',side_effect=slow_admit), \
                 mock.patch.object(q.journal,'claim_intent',side_effect=slow_claim):
                self.rejected(g.submit_auto)
            self.assertEqual(g.read_row()['state'],state)
            self.assertEqual(g.calls,[])
        # Two borrowed connections contend for one freshly fenced claim.
        h = AutoFixture()
        h.prepare_auto()
        barrier, answers, errors = threading.Barrier(2), [], []
        def contender():
            connection = legacy.connect(h.db)
            try:
                controller = h.make_controller(connection)
                barrier.wait(timeout=3)
                answers.append(controller.submit(h.raw, h.pin, 'test_agent', h.prompt,
                    runner=h.runner, automatic_fence_sha256=h.fence_pin))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        workers = [threading.Thread(target=contender) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(answers), 2)
        self.unknown(h, 1)
        # One necessary legacy path, not any old C01-C06 group.
        old = legacy.Fixture().start()
        self.assertEqual(old.observe()['status'], 'TURN_COMPLETED')
        old.submit()
        self.assertEqual(len(old.calls), 1)

    def test_M03(self):
        """Unique exact messages, full scan, literal client IDs and status rules."""
        for literal in (None, 'missing', 'arbitrary-client-literal'):
            f = AutoFixture().start_auto()
            turn = f.turn(client=literal)
            if literal == 'missing':
                del turn['items'][0]['clientId']
            client = f.owner([page(cursor='last'), page(turn)])
            result = f.observe_auto(client)
            self.assertEqual(result['status'], 'TURN_COMPLETED')
            self.assertEqual(len(client.calls), 2)
            self.false_turn_flags(result)
            self.unknown(f, 1)
            self.assertTrue((f.folder / 'automatic-binding.json').is_file())
            self.assertTrue(list(f.folder.glob('observation-*.json')))
            terminal = (f.folder / 'terminal.json').read_bytes()
            no_read = f.owner([RuntimeError('TERMINAL_MUST_NOT_READ')])
            self.assertEqual(f.observe_auto(no_read), result)
            self.assertEqual(no_read.calls, [])
            self.assertEqual((f.folder / 'terminal.json').read_bytes(), terminal)
        for change in ('space', 'unicode', 'extra-content', 'extra-field', 'text-elements', 'other-body', 'error', 'no-final', 'two-final'):
            with self.subTest(change=change):
                f = AutoFixture().start_auto()
                turn = f.turn()
                item = turn['items'][0]
                if change == 'space':
                    item['content'][0]['text'] += ' '
                elif change == 'unicode':
                    item['content'][0]['text'] = f.prompt.replace('e\u0301', '\u00e9')
                elif change == 'extra-content':
                    item['content'].append({'type': 'text', 'text': ''})
                elif change == 'extra-field':
                    item['content'][0]['unexpected'] = True
                elif change == 'text-elements':
                    item['content'][0]['text_elements'] = [{'synthetic': True}]
                elif change == 'other-body':
                    item['content'][0]['text'] = q.task_message(f.intent['task_id'], 'different body')
                elif change == 'error':
                    turn['error'] = {'message': 'synthetic error'}
                elif change == 'no-final':
                    turn['items'].pop()
                elif change == 'two-final':
                    turn['items'].append(dict(turn['items'][1], id='second-final'))
                self.rejected(lambda: f.observe_auto(f.owner(turn=turn)))
                self.no_terminal(f)
        f = AutoFixture()
        self.rejected(lambda: f.prepare_auto(pages=[page(f.turn())]))
        self.assertEqual(f.calls, [])
        f = AutoFixture().start_auto()
        other = f.named_turn('other-body', prompt=q.task_message(f.intent['task_id'], 'different'))
        self.rejected(lambda: f.observe_auto(f.owner([page(f.turn(), other)])))
        self.no_terminal(f)
        for status, expected in (('failed', 'TURN_FAILED'), ('interrupted', 'TURN_INTERRUPTED'), ('inProgress', 'TURN_IN_PROGRESS')):
            f = AutoFixture().start_auto()
            self.assertEqual(f.observe_auto(f.owner(turn=f.turn(status=status)))['status'], expected)

    def test_M04(self):
        """Pagination completeness and stable prehistory user projections."""
        f = AutoFixture().start_auto()
        client = f.owner(duplicate_exact_pages(f))
        result = f.observe_auto(client)
        self.assertEqual(result['status'], 'UNCONFIRMED')
        self.assertEqual(len(client.calls), 2)
        self.no_terminal(f)
        self.assertFalse((f.folder / 'automatic-binding.json').exists())
        for kind in ('duplicate-turn', 'duplicate-item', 'cycle', 'page-cap', 'partial', 'bad-page', 'missing-cursor', 'byte-cap', 'oversized-page', 'rpc-error'):
            with self.subTest(kind=kind):
                f = AutoFixture().start_auto()
                candidate = f.turn()
                other = f.named_turn('unrelated', prompt='ordinary unrelated text')
                if kind == 'duplicate-turn':
                    data = [page(candidate, cursor='next'), page(candidate)]
                elif kind == 'duplicate-item':
                    other['items'].append(copy.deepcopy(other['items'][0]))
                    data = [page(candidate, cursor='next'), page(other)]
                elif kind == 'cycle':
                    data = [page(candidate, cursor='loop'), page(cursor='loop')]
                elif kind == 'page-cap':
                    data = [page(candidate, cursor='1')] + [page(cursor=str(i)) for i in range(2, 5)]
                elif kind == 'partial':
                    other['itemsView'] = 'summary'
                    data = [page(candidate, cursor='next'), page(other)]
                elif kind == 'bad-page':
                    data = [page(candidate, cursor='next'), {'data': None, 'nextCursor': None}]
                elif kind == 'missing-cursor':
                    data = [page(candidate, cursor='next'), {'data': []}]
                elif kind == 'byte-cap':
                    data = [dict(page(candidate), backwardsCursor='x' * 1048576)]
                elif kind == 'oversized-page':
                    data = [page(*[f.named_turn(str(i), prompt='unrelated') for i in range(21)])]
                else:
                    data = [page(candidate, cursor='next'), RuntimeError('SYNTHETIC_OWNER_READ_LOST')]
                client = f.owner(data)
                self.rejected(lambda: f.observe_auto(client))
                self.assertLessEqual(len(client.calls), 4)
                self.no_terminal(f)
                self.unknown(f, 1)
        # A malformed user input on a noncandidate page makes uniqueness unconfirmed.
        for content in ([],[{'type':'text'}],[{'type':'text','text':3}],
                        [{'type':'image'}],[{'type':'localImage'}],[{'type':'audio'}],
                        [{'type':'text','text':'old','text_elements':[{'byteRange':{'start':False,'end':1}}]}]):
            g=AutoFixture().start_auto()
            other=g.named_turn('malformed',prompt='old')
            other['items'][0]['content']=content
            self.rejected(lambda:g.observe_auto(g.owner([page(g.turn(),cursor='next'),page(other)])))
            self.no_terminal(g)
        # Snapshot projections cover every old user item, including non-candidates.
        f = AutoFixture()
        prior = f.named_turn('baseline', prompt='old user text', status='inProgress')
        owner = f.owner([page(prior)])
        snapshot = turns.history_snapshot(owner, legacy.ROLES['test_agent'])
        self.assertEqual(snapshot['turn_ids'], [prior['id']])
        self.assertEqual(snapshot['users'][prior['id']][prior['items'][0]['id']],
                         q.sha(q.canonical(prior['items'][0])))
        self.assertEqual(snapshot['page_hashes'], [q.sha(q.canonical(page(prior)))])
        for kind in ('removed', 'edited', 'removed-item', 'normal-status-change'):
            g = AutoFixture()
            old = g.named_turn('baseline', prompt='old user text', status='inProgress')
            g.start_auto(pages=[page(old)])
            changed = copy.deepcopy(old)
            if kind == 'edited':
                changed['items'][0]['content'][0]['text'] += ' changed'
            elif kind == 'removed-item':
                changed['items'].pop(0)
            else:
                changed['status'] = 'completed'
                changed['items'].append({'id': 'new-output', 'type': 'agentMessage', 'phase': 'commentary', 'text': 'normal update'})
            data = page(g.turn()) if kind == 'removed' else page(g.turn(), changed)
            if kind == 'normal-status-change':
                self.assertEqual(g.observe_auto(g.owner([data]))['status'], 'TURN_COMPLETED')
            else:
                self.rejected(lambda: g.observe_auto(g.owner([data])))
                self.no_terminal(g)
        f = AutoFixture()
        with self.assertRaises(turns.TurnObservationError):
            turns.history_snapshot(f.owner([{'data': [], 'nextCursor': 'repeat'}] * 4), legacy.ROLES['test_agent'])
        for arguments in ({'timeout': True}, {'timeout': float('nan')}, {'timeout': float('inf')},
                          {'timeout': 0}, {'max_pages': True}, {'max_pages': 0}, {'max_pages': 5}):
            client = f.owner([page()])
            with self.assertRaises(turns.TurnObservationError):
                turns.history_snapshot(client, legacy.ROLES['test_agent'], **arguments)
            self.assertEqual(client.calls, [])
        for malformed in ({'data': [], 'nextCursor': True}, {'data': [], 'nextCursor': ''},
                          {'data': [], 'nextCursor': None, 'unknown': True}):
            with self.assertRaises(turns.TurnObservationError):
                turns.history_snapshot(f.owner([malformed]), legacy.ROLES['test_agent'])
        for field, value in (('read_only', False), ('completion_reads', False), ('allowed_threads', frozenset())):
            owner = f.owner([page()])
            setattr(owner, field, value)
            with self.assertRaises(turns.TurnObservationError):
                turns.history_snapshot(owner, legacy.ROLES['test_agent'])
            self.assertEqual(owner.calls, [])
        ticks = iter((0.0, 0.0, 21.0))
        with mock.patch.object(turns.time, 'monotonic', side_effect=lambda: next(ticks, 21.0)):
            with self.assertRaises(turns.TurnObservationError):
                turns.history_snapshot(f.owner([page()]), legacy.ROLES['test_agent'])

    def test_M05(self):
        """Strict recorder schema, exact contexts, byte hashes and immutable records."""
        f = stage_fixture()
        tests_record = self.capture(f)
        self.assertEqual(tests_record['status'], 'TESTS_PASSED')
        self.false_stage_flags(tests_record)
        self.assertEqual(f.stages.read('tests'), tests_record)
        # Returned digest is the hash of persisted bytes, without a self-cycle.
        records = [p for p in f.folder.rglob('*.json') if q.sha(p.read_bytes()) == tests_record['record_sha256']]
        self.assertEqual(len(records), 1)
        self.assertNotIn('record_sha256', json.loads(records[0].read_bytes()))
        reopened = stages.StageResults(f.controller, f.pin, f.request_pin, f.context, f.manifest)
        self.assertEqual(reopened.read('tests'), tests_record)
        good = reviewer_blob(f, tests_record)
        malformed = (good[:-1], good + b' trailing', good.replace(b'"schema":1', b'"schema":1,"schema":1'),
                     good.replace(b'"schema":1', b'"schema":true'),
                     good.replace(b'"findings":[]', b'"findings":NaN'),
                     q.canonical(dict(json.loads(good), unknown='forbidden')),
                     q.canonical(dict(json.loads(good), origin='LOCALLY_CAPTURED')),
                     q.canonical({k: v for k, v in json.loads(good).items() if k != 'context'}),
                     b' ' * 1048577)
        for raw in malformed:
            with self.subTest(malformed_bytes=len(raw)):
                self.rejected(lambda: f.stages.record_reviewer(raw, 'review', 'author'))
        for key in f.expanded:
            bad_context = dict(f.expanded)
            bad_context[key] = 'wrong-' + key
            self.rejected(lambda: f.stages.record_reviewer(
                reviewer_blob(f, tests_record, context=bad_context), 'review', 'author'))
        for context in (dict(f.context, extra=True), {k: v for k, v in f.context.items() if k != 'head_sha'},
                        dict(f.context, attempt_id=str(uuid.uuid1())), dict(f.context, head_sha='B' * 40),
                        dict(f.context, source_manifest_sha256='0' * 64)):
            with self.assertRaises((q.files.PackageError, ValueError, TypeError)):
                stages.StageResults(f.controller, f.pin, f.request_pin, context, f.manifest)
        for manifest in (b'{"../escape":"' + b'a' * 64 + b'"}', b'{} trailing',
                         b'{"a":"' + b'a' * 64 + b'","a":"' + b'a' * 64 + b'"}'):
            with self.assertRaises((q.files.PackageError, ValueError, TypeError)):
                stages.StageResults(f.controller, f.pin, f.request_pin,
                    dict(f.context, source_manifest_sha256=q.sha(manifest)), manifest)
        for missing in (NEW_TEST,'src/ops/tools/codex_owner_cli_controller.py',
                        'src/ops/tools/tests/test_codex_owner_cli_controller.py',
                        'src/ops/tools/tests/fixtures/codex_unattended_admission/flex_gate.py.txt'):
            deficient=json.loads(f.manifest);deficient.pop(missing)
            deficient_raw=q.canonical(deficient)
            with self.assertRaises(q.files.PackageError):
                stages.StageResults(f.controller,f.pin,f.request_pin,
                    dict(f.context,attempt_id=str(uuid.uuid4()),source_manifest_sha256=q.sha(deficient_raw)),deficient_raw)
        review = f.stages.record_reviewer(good, 'fixture-review', 'fixture-author')
        self.assertEqual(review['artifact_sha256'], q.sha(good))
        self.assertEqual(review['previous_stage_sha256'], tests_record['record_sha256'])
        self.assertEqual(review['origin'], 'SUPPLIED_TOOL_RESPONSE')
        before = {p: p.read_bytes() for p in f.folder.rglob('*') if p.is_file()}
        self.assertEqual(f.stages.record_reviewer(good, 'fixture-review', 'fixture-author'), review)
        self.rejected(lambda: f.stages.record_reviewer(reviewer_blob(f, tests_record, decision='REQUEST_CHANGES'), 'fixture-review', 'fixture-author'))
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        # Actual raw artifact corruption must be detected on read, not trusted by declaration.
        artifacts = [p for p in f.folder.rglob('*') if p.is_file() and q.sha(p.read_bytes()) == review['artifact_sha256']]
        self.assertTrue(artifacts)
        artifacts[0].write_bytes(good + b' ')
        self.rejected(lambda: f.stages.read('reviewer'))
        g = stage_fixture()
        t = self.capture(g)
        with mock.patch.object(q.os, 'fsync', side_effect=OSError('SYNTHETIC_FSYNC_LOST')):
            try:
                self.rejected(lambda: g.stages.record_reviewer(reviewer_blob(g, t), 'review', 'author'))
            except OSError:
                pass

    def test_M06(self):
        """Fixed subprocess capture counts/source pins and explicit reviewer linkage."""
        for kind in ('normal', 'failure', 'error', 'skip', 'short-count', 'bool-count', 'wrong-ids', 'stale-context', 'missing-json', 'truncated', 'nonzero-exit'):
            with self.subTest(kind=kind):
                f = stage_fixture()
                payload = {'schema': 1, 'stage': 'tests', 'context': dict(f.expanded),
                           'test_ids': list(TEST_IDS), 'run': 7, 'failures': 0, 'errors': 0, 'skips': 0}
                rc = 0
                if kind in ('failure', 'error', 'skip'):
                    payload[{'failure': 'failures', 'error': 'errors', 'skip': 'skips'}[kind]] = 1
                elif kind == 'short-count':
                    payload['run'] = 6
                elif kind == 'bool-count':
                    payload['run'] = True
                elif kind == 'wrong-ids':
                    payload['test_ids'][-1] = 'legacy.ControllerAcceptance'
                elif kind == 'stale-context':
                    payload['context']['head_sha'] = 'd' * 40
                elif kind == 'missing-json':
                    payload = 'NO_RESULT'
                elif kind == 'truncated':
                    payload = b'{"schema":1'
                elif kind == 'nonzero-exit':
                    rc = 7
                record = self.capture(f, payload, rc)
                self.assertEqual(record['status'], 'TESTS_PASSED' if kind == 'normal' else 'TESTS_FAILED')
                self.false_stage_flags(record)
                self.assertEqual(f.stages.capture_tests(f.repo), record)
                if kind != 'normal':
                    self.rejected(lambda: f.stages.record_reviewer(reviewer_blob(f, record), 'review', 'author'))
        f = stage_fixture()
        with self.assertRaises(TypeError):
            f.stages.capture_tests(f.repo, runner=lambda: {'status': 'PASS'})
        for selected in ([], ['ControllerAcceptance'], [TEST_IDS[0], TEST_IDS[0]]):
            with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('INVALID_SELECTION_MUST_NOT_SPAWN')) as popen:
                self.rejected(lambda: f.stages.capture_tests(f.repo, test_ids=selected))
                popen.assert_not_called()
        changed = f.repo / SOURCE_PATHS[0]
        changed.write_bytes(changed.read_bytes() + b'\n# source drift\n')
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('SOURCE_PIN_MUST_PRECEDE_SPAWN')) as popen:
            self.rejected(lambda: f.stages.capture_tests(f.repo))
            popen.assert_not_called()
        f = stage_fixture()
        t = self.capture(f)
        for execution, author in (('', 'author'), ('review', ''), ('same', 'same')):
            self.rejected(lambda: f.stages.record_reviewer(reviewer_blob(f, t), execution, author))
        self.rejected(lambda: f.stages.record_reviewer(reviewer_blob(f, t, tests_artifact_sha256='0' * 64), 'review', 'author'))
        review = f.stages.record_reviewer(reviewer_blob(f, t), 'fixture-review', 'fixture-author')
        self.assertEqual(review['status'], 'REVIEW_PASSED')
        self.assertEqual(review['previous_stage_sha256'], t['record_sha256'])
        self.false_stage_flags(review)
        g = stage_fixture()
        t = self.capture(g)
        pending = g.stages.record_reviewer(reviewer_blob(g, t, decision='REQUEST_CHANGES', findings=['needs revision']), 'review', 'author')
        self.assertEqual(pending['status'], 'REVIEW_PENDING')

    def test_M07(self):
        """Draft structure, publication head and tested source/previous-stage binding."""
        f = stage_fixture()
        t, review = self.ready_review(f)
        raw = draft_blob(f)
        record = f.stages.record_draft(raw, expected_head_sha='c' * 40, published_sources=f.sources, expected_pr_number=29, expected_base_branch='main', expected_head_branch='codex/d29-fixture')
        self.assertEqual(record['status'], 'DRAFT_RECORDED')
        self.assertEqual(record['artifact_sha256'], q.sha(raw))
        self.assertEqual(record['previous_stage_sha256'], review['record_sha256'])
        self.assertEqual(record['origin'], 'SUPPLIED_TOOL_RESPONSE')
        self.assertNotEqual(f.context['head_sha'], 'c' * 40)
        self.false_stage_flags(record)
        self.assertEqual(f.stages.read('draft_pr'), record)
        self.assertEqual(f.stages.record_draft(raw, expected_head_sha='c' * 40, published_sources=f.sources, expected_pr_number=29, expected_base_branch='main', expected_head_branch='codex/d29-fixture'), record)
        for key, value in (('draft', False), ('state', 'closed'), ('head_repo_full_name', 'other/agent_project'),
                           ('base_sha', 'e' * 40), ('head_sha', 'e' * 40), ('number', True), ('number', 0),
                           ('url', 'https://github.com/other/agent_project/pull/29'), ('base', ''), ('head', ''), ('base', 'other-base'), ('head', 'other-head'),
                           ('check_runs', [{'conclusion': 'success'}])):
            with self.subTest(field=key):
                g = stage_fixture()
                self.ready_review(g)
                self.rejected(lambda: g.stages.record_draft(draft_blob(g, **{key: value}),
                    expected_head_sha='c' * 40, published_sources=g.sources, expected_pr_number=29, expected_base_branch='main', expected_head_branch='codex/d29-fixture'))
        for kind in ('missing-source', 'changed-source', 'extra-source', 'wrong-publication-pin', 'missing-review', 'malformed'):
            with self.subTest(kind=kind):
                g = stage_fixture()
                if kind == 'missing-review':
                    self.capture(g)
                else:
                    self.ready_review(g)
                sources = dict(g.sources)
                if kind == 'missing-source':
                    sources.pop(NEW_TEST)
                elif kind == 'changed-source':
                    sources[NEW_TEST] += b'\nchanged'
                elif kind == 'extra-source':
                    sources['unexpected.py'] = b'pass\n'
                pin = 'e' * 40 if kind == 'wrong-publication-pin' else 'c' * 40
                raw = b'{"draft":true' if kind == 'malformed' else draft_blob(g)
                self.rejected(lambda: g.stages.record_draft(raw, expected_head_sha=pin, published_sources=sources, expected_pr_number=29, expected_base_branch='main', expected_head_branch='codex/d29-fixture'))
        for invalid in (b',"additions":1e9999',b',"additions":true',b',"mergeable":"yes"',
                        b',"user":[]',b',"requested_reviewers":{}'):
            g=stage_fixture();self.ready_review(g)
            malformed=draft_blob(g)[:-1]+invalid+b'}'
            self.rejected(lambda:g.stages.record_draft(malformed,expected_head_sha='c'*40,
                published_sources=g.sources,expected_pr_number=29,expected_base_branch='main',
                expected_head_branch='codex/d29-fixture'))
        # A self-consistent different PR URL/number must still fail the external PR pin.
        wrong_pr = stage_fixture()
        self.ready_review(wrong_pr)
        self.rejected(lambda: wrong_pr.stages.record_draft(
            draft_blob(wrong_pr, number=30, url='https://github.com/privateInt/agent_project/pull/30'),
            expected_head_sha='c' * 40, published_sources=wrong_pr.sources,
            expected_pr_number=29, expected_base_branch='main', expected_head_branch='codex/d29-fixture'))
        # Tampering with the previous immutable record cannot satisfy the chain.
        g = stage_fixture()
        _, review = self.ready_review(g)
        stored = [p for p in g.folder.rglob('*.json') if q.sha(p.read_bytes()) == review['record_sha256']]
        self.assertEqual(len(stored), 1)
        stored[0].write_bytes(stored[0].read_bytes() + b' ')
        self.rejected(lambda: g.stages.record_draft(draft_blob(g), expected_head_sha='c' * 40, published_sources=g.sources, expected_pr_number=29, expected_base_branch='main', expected_head_branch='codex/d29-fixture'))
        OBSERVATIONS.append({'groups': 'M01-M07', 'history': 'explicit fixture',
                             'stage_process': 'explicit Popen double', 'actual_native_calls': 0,
                             'native_workflow': 'NOT_RUN', 'independent_review_verified': False})


def load_tests(loader, tests, pattern):
    # unittest discovery, when explicitly requested later, still selects only us.
    return loader.loadTestsFromNames(TEST_IDS, sys.modules[__name__])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', type=Path)
    parser.add_argument('--result', type=Path)
    parser.add_argument('test_ids', nargs='*')
    options = parser.parse_args(argv)
    selected = options.test_ids or list(TEST_IDS)
    if len(set(selected)) != len(selected) or any(name not in TEST_IDS for name in selected):
        parser.error('Only the seven explicit MessageCorrelationAcceptance method IDs are allowed')
    if bool(options.context) != bool(options.result):
        parser.error('--context and --result must be supplied together')
    context = json.loads(options.context.read_bytes()) if options.context else {}
    suite = unittest.defaultTestLoader.loadTestsFromNames(selected, sys.modules[__name__])
    with offline_guards():
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'schema': 1, 'stage': 'tests', 'context': context, 'test_ids': selected,
              'run': result.testsRun, 'failures': len(result.failures),
              'errors': len(result.errors), 'skips': len(result.skipped)}
    if options.result:
        output = options.result
    else:
        folder = Path(tempfile.mkdtemp(prefix='agent-message-correlation-result-', dir='/tmp'))
        folder.chmod(0o700)
        output = folder / 'result.json'
    private_write(output, q.canonical(report))
    print('RESULT_FILE=' + str(output), flush=True)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
