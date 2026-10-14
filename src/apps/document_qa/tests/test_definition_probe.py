"""Definition probe: synthetic identities, bounded HTTP and private-output checks.

No native Agent, model, state database or inference is used by this suite.
"""
import ast
import copy
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from src.apps.document_qa import definition_probe as q
from src.apps.document_qa import readiness, search_tool_transport
from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.qa_agent_transport import AgentQAGateway


PROJECT = Path(__file__).resolve().parents[4]
SOURCE_PATHS = (
    'src/platform/agent/runtime/agent_runtime/agents.py',
    'src/platform/model/gateway/app/chat.go',
    'src/apps/document_qa/qa_agent_transport.py',
)
TOKEN = 'private-fixture-bearer-definition-probe'
REPORT_FIELDS = {
    'schema', 'status', 'scope', 'run_error_code', 'run_error_stage',
    'both_runs_still_failed', 'run_version_hashes_match', 'definition_hash_recomputed',
    'same_agent', 'same_input', 'model_pin_matches_baseline', 'changed_definition_fields',
    'definitions', 'steps_rechecked', 'reference_diagnostics_rechecked',
    'active_runtime_verified', 'payload_verified', 'template_verified',
    'head_readback_verified', 'answer_quality_verified', 'f0a_accepted',
    'inference_requests', 'write_requests',
}
UNVERIFIED_FIELDS = {
    'definition_hash_recomputed', 'steps_rechecked', 'reference_diagnostics_rechecked',
    'active_runtime_verified', 'payload_verified', 'template_verified',
    'head_readback_verified', 'answer_quality_verified', 'f0a_accepted',
}


def assert_report(test, result, fixture, sources=None):
    fields = REPORT_FIELDS | ({'get_requests', 'repository_source_sha256',
                              'repository_sources_are_active_runtime_evidence'} if sources is not None else set())
    test.assertEqual(set(result), fields)
    test.assertEqual((result['schema'], result['status'], result['scope']),
                     (1, 'OBSERVED', 'pinned_agent_definition_metadata_only'))
    test.assertEqual((result['run_error_code'], result['run_error_stage']),
                     ('ANSWER_REFERENCE_INVALID', 'finalize'))
    test.assertEqual(result['changed_definition_fields'], ['prompt'])
    for field in UNVERIFIED_FIELDS:
        test.assertIs(result[field], False, field)
    for field in ('both_runs_still_failed', 'run_version_hashes_match', 'same_agent',
                  'same_input', 'model_pin_matches_baseline'):
        test.assertIs(result[field], True, field)
    for field in ('inference_requests', 'write_requests'):
        test.assertIs(type(result[field]), int)
        test.assertEqual(result[field], 0)
    test.assertEqual(set(result['definitions']), {'original', 'candidate'})
    for index, label in enumerate(('original', 'candidate')):
        definition = result['definitions'][label]
        test.assertEqual(set(definition), {'state', 'prompt_bytes', 'prompt_sha256',
            'max_tokens', 'temperature', 'context_max_chars', 'timeout_ms'})
        test.assertEqual(definition['state'], 'published' if label == 'original' else 'draft')
        test.assertEqual(definition['prompt_bytes'], len(fixture.prompts[index].encode()))
        test.assertEqual(definition['prompt_sha256'], hashlib.sha256(fixture.prompts[index].encode()).hexdigest())
        for field in ('max_tokens', 'temperature', 'context_max_chars', 'timeout_ms'):
            test.assertEqual(definition[field], fixture.version(label)[field])
    if sources is not None:
        test.assertEqual(result['get_requests'], 4)
        test.assertEqual(result['repository_source_sha256'], sources)
        test.assertIs(result['repository_sources_are_active_runtime_evidence'], False)


def identifier(label):
    return str(uuid5(NAMESPACE_URL, 'definition-probe-test/' + label))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


class Fixture:
    def __init__(self, prefix='first'):
        self.ids = {name: identifier(prefix + '/' + name) for name in (
            'original-run', 'candidate-run', 'original-version', 'candidate-version',
            'agent', 'tool', 'connection', 'deployment', 'model', 'key', 'request')}
        self.question = 'PRIVATE_QUESTION_연결 질문'
        self.prompts = ('PRIVATE_PROMPT_ORIGINAL_한국어', 'PRIVATE_PROMPT_CANDIDATE_한국어 인용 지침')
        self.hashes = ('1' * 64, '2' * 64)
        self.input_hash = '3' * 64
        self.baseline = {'schema': 1,
            'PC_question': {'agent_run_id': self.ids['original-run'],
                            'agent_version_id': self.ids['original-version']},
            'failed_prompt_trial': {'agent_run_id': self.ids['candidate-run'],
                                    'agent_version_id': self.ids['candidate-version']},
            'models': {'chat': {'deployment_id': self.ids['deployment'],
                                 'model_version_id': self.ids['model']}}}
        self.rows = {}
        for i, name in enumerate(('original', 'candidate')):
            version = {'agent_version_id': self.ids[name + '-version'], 'agent_id': self.ids['agent'],
                'version': i + 1, 'revision': i + 1,
                'state': 'published' if i == 0 else 'draft', 'definition_sha256': self.hashes[i],
                'template_id': 'knowledge_qa_v1', 'template_version': 1,
                'tool_version_id': self.ids['tool'], 'model': {
                    'connection_version_id': self.ids['connection'], 'deployment_id': self.ids['deployment'],
                    'model_version_id': self.ids['model']}, 'prompt': self.prompts[i],
                'context_max_chars': 8000, 'max_tokens': 512, 'temperature': 0.0, 'timeout_ms': 300000,
                'input_schema': {'type': 'object', 'required': ['question'], 'additionalProperties': False,
                                 'properties': {'question': {'type': 'string'}}},
                'output_schema': {'type': 'object', 'required': [
                    'answer', 'outcome', 'citations', 'warnings', 'answer_truncated', 'tool_call_id', 'identity']},
                'unknown_private_metadata': 'PRIVATE_VERSION_METADATA'}
            run = {'agent_run_id': self.ids[name + '-run'], 'agent_version_id': version['agent_version_id'],
                'definition_sha256': self.hashes[i], 'input_sha256': self.input_hash,
                'invocation_key': self.ids['key'], 'request_id': self.ids['request'],
                'input': {'question': self.question, 'filters': {'kinds': ['PRIVATE_FILTER']}},
                'state': 'failed', 'mode': 'run' if i == 0 else 'test',
                'error': {'code': 'ANSWER_REFERENCE_INVALID', 'stage': 'finalize',
                          'message': 'PRIVATE_REMOTE_ERROR'}, 'unknown_private_metadata': 'PRIVATE_RUN_METADATA'}
            self.rows['/api/agent-runs/' + run['agent_run_id']] = run
            self.rows['/api/agent-versions/' + version['agent_version_id']] = version
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path not in self.rows:
            raise AssertionError('unexpected fixture path')
        return copy.deepcopy(self.rows[path])

    def version(self, name):
        return self.rows['/api/agent-versions/' + self.ids[name + '-version']]

    def run(self, name):
        return self.rows['/api/agent-runs/' + self.ids[name + '-run']]

    def forbidden(self):
        return [TOKEN, self.question, *self.prompts, *self.ids.values(), *self.hashes, self.input_hash,
                'PRIVATE_FILTER', 'PRIVATE_REMOTE_ERROR', 'PRIVATE_VERSION_METADATA', 'PRIVATE_RUN_METADATA']


class Clock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value


class Response:
    def __init__(self, raw=b'{}', content_type='application/json', on_read=None):
        self.raw = io.BytesIO(raw)
        self.headers = Message()
        self.headers['Content-Type'] = content_type
        self.status = 200
        self.on_read = on_read

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.close()

    def read1(self, size):
        if self.on_read:
            self.on_read()
        return self.raw.read(size)


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response or Response(), error, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class ProbeTests(unittest.TestCase):
    def checked(self, fixture):
        return q.baseline_checked(copy.deepcopy(fixture.baseline))

    def assert_private(self, output, fixture):
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        for secret in fixture.forbidden():
            self.assertNotIn(secret, text)
        self.assertNotIn('raw_answer', text)
        self.assertNotIn('traceback', text.lower())

    def test_two_independent_identity_sets_compare_without_mutating_inputs(self):
        for prefix in ('first', 'second'):
            with self.subTest(prefix=prefix):
                f = Fixture(prefix)
                baseline, rows = copy.deepcopy((f.baseline, f.rows))
                expected = self.checked(f)
                self.assertEqual(q.allowed_paths(expected), frozenset(f.rows))
                result = q.probe(f, expected)
                self.assertEqual(set(f.calls), set(f.rows))
                self.assertEqual(len(f.calls), 4)
                self.assertEqual((f.baseline, f.rows), (baseline, rows))
                assert_report(self, result, f)
                self.assert_private(result, f)
                safe = json.dumps(result, ensure_ascii=False)
                for prompt in f.prompts:
                    self.assertIn(hashlib.sha256(prompt.encode()).hexdigest(), safe)
                self.assertNotIn('semantic_quality_verified": true', safe)

    def test_baseline_invalid_or_duplicate_identity_is_rejected_before_reads(self):
        mutations = [
            lambda b: b['PC_question'].pop('agent_run_id'),
            lambda b: b['PC_question'].update(agent_run_id='PRIVATE_INVALID_UUID'),
            lambda b: b['failed_prompt_trial'].update(agent_run_id=b['PC_question']['agent_run_id']),
            lambda b: b['failed_prompt_trial'].update(agent_version_id=b['PC_question']['agent_version_id']),
            lambda b: b['models']['chat'].update(model_version_id=False),
        ]
        for mutate in mutations:
            f = Fixture()
            mutate(f.baseline)
            with self.subTest(baseline=f.baseline), self.assertRaises(q.ProbeError):
                q.baseline_checked(f.baseline)
            self.assertEqual(f.calls, [])

    def test_run_identity_terminal_error_input_and_hash_mismatch_are_rejected(self):
        mutations = [
            lambda r: r.update(agent_run_id=identifier('foreign-run')),
            lambda r: r.update(agent_version_id=identifier('foreign-version')),
            lambda r: r.update(state='succeeded'),
            lambda r: r.update(result={'answer': 'PRIVATE_FAKE_SUCCESS'}),
            lambda r: r['error'].update(code='MODEL_TIMEOUT'),
            lambda r: r['error'].update(stage='generate'),
            lambda r: r.update(definition_sha256='f' * 64),
            lambda r: r['input'].update(question='PRIVATE_DIFFERENT_QUESTION'),
            lambda r: r['input'].update(filters={'kinds': ['different']}),
            lambda r: r.update(input={'question': '', 'filters': {}}),
            lambda r: r['input'].update(filters=[]),
            lambda r: r['input'].update(unknown='PRIVATE_UNKNOWN_INPUT'),
        ]
        for number, mutate in enumerate(mutations):
            f = Fixture()
            mutate(f.run('candidate'))
            before = copy.deepcopy(f.rows)
            with self.subTest(case=number), self.assertRaises((q.ProbeError, UploadError, UnicodeError)):
                q.probe(f, self.checked(f))
            self.assertLessEqual(len(f.calls), 4)
            self.assertEqual(f.rows, before)

    def test_version_identity_schema_and_baseline_model_pin_are_checked(self):
        mutations = [
            lambda v: v.update(agent_version_id=identifier('foreign-version')),
            lambda v: v.update(agent_id=identifier('foreign-agent')),
            lambda v: v.update(state='published'),
            lambda v: v.update(definition_sha256='PRIVATE_BAD_HASH'),
            lambda v: v['model'].update(model_version_id=identifier('different-model')),
            lambda v: v['input_schema'].update(additionalProperties=True),
            lambda v: v['input_schema'].update(required=[]),
            lambda v: v['input_schema']['properties']['question'].update(type='number'),
            lambda v: v['output_schema'].update(required=['answer']),
            lambda v: v.pop('input_schema'),
            lambda v: v.pop('output_schema'),
            lambda v: v['input_schema']['properties']['question'].update(minLength=1),
            lambda v: v['output_schema']['required'].append('extra-output-field'),
            lambda v: v.update(max_tokens=True),
            lambda v: v.update(temperature=float('nan')),
            lambda v: v.update(prompt='PRIVATE_BAD_UNICODE_\ud800'),
            lambda v: v.pop('context_max_chars'),
        ]
        for number, mutate in enumerate(mutations):
            f = Fixture()
            mutate(f.version('candidate'))
            with self.subTest(case=number), self.assertRaises((q.ProbeError, UploadError, UnicodeError)):
                q.probe(f, self.checked(f))
            self.assertLessEqual(len(f.calls), 4)
        f = Fixture()
        for name in ('original', 'candidate'):
            f.version(name)['model']['deployment_id'] = identifier('same-but-wrong-deployment')
        with self.assertRaises(q.ProbeError):
            q.probe(f, self.checked(f))

    def test_service_hash_is_compared_without_python_definition_rehash(self):
        f = Fixture()
        for name, digest in (('original', 'd' * 64), ('candidate', 'e' * 64)):
            f.run(name)['definition_sha256'] = digest
            f.version(name)['definition_sha256'] = digest
        self.assert_private(q.probe(f, self.checked(f)), f)

    def test_question_only_and_filter_order_are_supported(self):
        for filters in (False, True):
            f = Fixture()
            if not filters:
                for name in ('original', 'candidate'):
                    f.run(name)['input'].pop('filters')
            else:
                f.run('original')['input']['filters'] = {'kinds': ['text'], 'documents': []}
                f.run('candidate')['input']['filters'] = {'documents': [], 'kinds': ['text']}
            q.probe(f, self.checked(f))
            self.assertEqual(len(f.calls), 4)

    def test_non_prompt_change_and_identical_prompt_cannot_report_prompt_only(self):
        for key, value in (('max_tokens', 256), ('temperature', 0.2),
                           ('timeout_ms', 200000), ('context_max_chars', 4096),
                           ('tool_version_id', identifier('different-tool'))):
            f = Fixture()
            f.version('candidate')[key] = value
            with self.subTest(field=key):
                try:
                    result = q.probe(f, self.checked(f))
                except q.ProbeError as error:
                    self.assertEqual(error.code, 'DEFINITION_COMPARISON_INVALID')
                else:
                    safe = json.dumps(result)
                    self.assertIn('BLOCKED', safe)
                    self.assert_private(result, f)
        f = Fixture()
        f.version('candidate')['prompt'] = f.prompts[0]
        try:
            result = q.probe(f, self.checked(f))
        except q.ProbeError as error:
            self.assertEqual(error.code, 'DEFINITION_COMPARISON_INVALID')
        else:
            self.assertIn('BLOCKED', json.dumps(result))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.expected = q.baseline_checked(self.f.baseline)
        self.paths = q.allowed_paths(self.expected)
        self.row = {'url': 'http://127.0.0.1:1', 'enabled': True}

    def test_exact_get_guard_rejects_broad_gateway_paths_before_delegating(self):
        parent = '/api/agents/' + self.f.ids['agent']
        run = '/api/agent-runs/' + self.f.ids['original-run']
        cases = [('GET', parent, None, None), ('POST', '/api/agent-models/inspect', {}, self.f.ids['key']),
                 ('GET', run + '/steps', None, None), ('GET', run + '?limit=1', None, None),
                 ('GET', '/api/agent-runs/' + identifier('other-run'), None, None),
                 ('GET', run, {}, None), ('GET', run, None, self.f.ids['key'])]
        with patch.object(AgentQAGateway, 'request', return_value=(200, b'{}')) as delegate:
            for method, path, body, key in cases:
                with self.subTest(method=method, path=path), self.assertRaises(q.ProbeError):
                    q.FixedReader(self.row, self.paths).request(method, path, body, key)
            delegate.assert_not_called()

    def test_four_unique_paths_are_only_requests_and_duplicates_are_not_retried(self):
        reader = q.FixedReader(self.row, self.paths)
        with patch.object(AgentQAGateway, 'request', return_value=(200, b'{}')) as delegate:
            for path in sorted(self.paths):
                reader.get(path)
            self.assertEqual(delegate.call_count, 4)
            with self.assertRaises(q.ProbeError):
                reader.get(sorted(self.paths)[0])
            self.assertEqual(delegate.call_count, 4)
        reader = q.FixedReader(self.row, self.paths)
        with patch.object(AgentQAGateway, 'request', side_effect=UploadError('PRIVATE_REMOTE_ERROR', 502)) as delegate:
            with self.assertRaises(Exception):
                reader.get(sorted(self.paths)[0])
            with self.assertRaises(q.ProbeError):
                reader.get(sorted(self.paths)[0])
            self.assertEqual(delegate.call_count, 1)
        reader = q.FixedReader(self.row, self.paths)
        reader.calls = 4
        with patch.object(AgentQAGateway, 'request') as delegate:
            with self.assertRaises(q.ProbeError):
                reader.get(sorted(self.paths)[0])
            delegate.assert_not_called()

    def test_request_timeout_is_bounded_by_eight_seconds_and_shared_deadline(self):
        clock = Clock()
        reader = q.FixedReader(self.row, self.paths, clock=clock)
        self.assertEqual(reader.timeout, 8)
        self.assertEqual(reader.deadline, 60)
        clock.value = 57
        opener = Opener()
        with patch.object(search_tool_transport, 'build_opener', return_value=opener):
            reader.get(sorted(self.paths)[0])
        self.assertEqual(len(opener.calls), 1)
        self.assertLessEqual(opener.calls[0][1], 3)
        clock.value = 60
        with patch.object(search_tool_transport, 'build_opener') as build:
            with self.assertRaises((q.ProbeError, UploadError)):
                reader.get(sorted(self.paths)[1])
            build.assert_not_called()

    def test_invalid_json_content_type_size_and_timeout_are_not_retried(self):
        cases = [(b'{"duplicate":1,"duplicate":2}', 'application/json', None),
                 (b'not-json PRIVATE_REMOTE_BODY', 'application/json', None),
                 (b'{}', 'text/html', None),
                 (b' ' * ((2 << 20) + 1), 'application/json', None),
                 (b'{}', 'application/json', TimeoutError('PRIVATE_TIMEOUT'))]
        for raw, kind, error in cases:
            reader = q.FixedReader(self.row, self.paths)
            opener = Opener(Response(raw, kind), error)
            with self.subTest(kind=kind, size=len(raw), timeout=bool(error)):
                with patch.object(search_tool_transport, 'build_opener', return_value=opener):
                    with self.assertRaises((q.ProbeError, UploadError)):
                        reader.get(sorted(self.paths)[0])
                self.assertEqual(len(opener.calls), 1)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.project = self.directory / 'project'
        self.project.mkdir()
        self.f = Fixture()
        self.baseline = self.project / 'baseline.json'
        self.baseline.write_bytes(encoded(self.f.baseline))
        self.settings = self.directory / 'settings.json'
        self.token = self.directory / 'token'
        self.token.write_text(TOKEN)
        self.token.chmod(0o600)
        self.home = self.directory / 'empty-home'
        self.home.mkdir()
        for relative in SOURCE_PATHS:
            destination = self.project / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((PROJECT / relative).read_bytes())

    def settings_for(self, url):
        value = {'schema': 1, 'library_name': '합성 자료함', 'defaults': {},
                 'platforms': {role: {'url': url if role == 'agent' else 'http://unused.invalid',
                    'enabled': True, **({'token_file': str(self.token)} if role == 'agent' else {})}
                    for role in readiness.ROLES}}
        self.settings.write_bytes(encoded(value))
        self.settings.chmod(0o600)
        return value

    def snapshot(self):
        return {str(p.relative_to(self.directory)): (p.stat().st_mode & 0o777, p.read_bytes() if p.is_file() else None)
                for p in self.directory.rglob('*') if not p.is_symlink()}

    def args(self):
        return ['--project-root', str(self.project), '--baseline', 'baseline.json', '--settings', str(self.settings)]

    def invoke(self, args=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = q.main(self.args() if args is None else args)
        return code, out.getvalue(), err.getvalue()

    def assert_private(self, text):
        for value in self.f.forbidden() + [str(self.directory), str(self.settings), str(self.token)]:
            self.assertNotIn(value, text)
        self.assertNotIn('traceback', text.lower())

    def test_missing_settings_cannot_load_ops_fallback_or_make_state(self):
        ops = self.directory / 'valid-ops-config.json'
        self.settings_for('http://127.0.0.1:1')
        ops.write_bytes(self.settings.read_bytes())
        self.settings.unlink()
        before = self.snapshot()
        with patch.object(readiness, 'load_settings') as load, patch.object(q, 'FixedReader') as reader:
            code, out, err = self.invoke()
            load.assert_not_called()
            reader.assert_not_called()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, '')
        self.assertIsInstance(json.loads(err), dict)
        self.assert_private(out + err)
        self.assertEqual(before, self.snapshot())

    def test_settings_and_credential_rejections_never_expose_paths_or_modify_files(self):
        self.settings_for('http://127.0.0.1:1')
        original = self.settings.read_bytes()
        cases = ['symlink', 'oversized', 'invalid', 'disabled', 'missing-token', 'symlink-token']
        for case in cases:
            if self.settings.is_symlink() or self.settings.exists():
                self.settings.unlink()
            self.settings.write_bytes(original)
            if self.token.is_symlink() or self.token.exists():
                self.token.unlink()
            self.token.write_text(TOKEN)
            self.token.chmod(0o600)
            if case == 'symlink':
                target = self.directory / 'settings-target'
                target.write_bytes(original)
                self.settings.unlink()
                self.settings.symlink_to(target)
            elif case == 'oversized':
                self.settings.write_bytes(b'P' * 65537)
            elif case == 'invalid':
                self.settings.write_bytes(b'{PRIVATE_CONFIG')
            elif case == 'disabled':
                value = json.loads(original)
                value['platforms']['agent']['enabled'] = False
                self.settings.write_bytes(encoded(value))
            elif case == 'missing-token':
                self.token.unlink()
            elif case == 'symlink-token':
                target = self.directory / 'token-target'
                target.write_text(TOKEN)
                target.chmod(0o600)
                self.token.unlink()
                self.token.symlink_to(target)
            before = self.snapshot()
            with self.subTest(case=case), patch.object(search_tool_transport, 'build_opener') as opener:
                code, out, err = self.invoke()
                self.assertNotEqual(code, 0)
                self.assertEqual(out, '')
                self.assertIsInstance(json.loads(err), dict)
                self.assert_private(err)
                opener.assert_not_called()
            self.assertEqual(before, self.snapshot())

    def test_arbitrary_remote_exception_is_code_only_without_traceback(self):
        self.settings_for('http://127.0.0.1:1')
        before = self.snapshot()
        with patch.object(q, 'probe', side_effect=RuntimeError('PRIVATE_EXCEPTION_' + TOKEN)):
            code, out, err = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, '')
        self.assertIsInstance(json.loads(err), dict)
        self.assert_private(out + err)
        self.assertNotIn('PRIVATE_EXCEPTION', err)
        self.assertEqual(before, self.snapshot())

    def test_invalid_cli_arguments_never_echo_private_values(self):
        for args in (['--PRIVATE_UNKNOWN_FLAG', 'PRIVATE_ARGUMENT_VALUE'],
                     ['--settings'], ['--baseline'], ['--project-root']):
            with self.subTest(args=args):
                code, out, err = self.invoke(args)
                self.assertNotEqual(code, 0)
                self.assertEqual(out, '')
                value = json.loads(err)
                self.assertEqual(value['status'], 'BLOCKED')
                self.assertNotIn('PRIVATE', err)
                self.assertNotIn('traceback', err.lower())

    def test_source_hashes_are_repository_snapshots_and_python310_syntax_is_valid(self):
        hashes = q.local_source_hashes(self.project)
        self.assertEqual(set(hashes), set(SOURCE_PATHS))
        for name in SOURCE_PATHS:
            self.assertEqual(hashes[name], hashlib.sha256((self.project / name).read_bytes()).hexdigest())
        ast.parse(Path(q.__file__).read_text(), feature_version=(3, 10))
        ast.parse(Path(__file__).read_text(), feature_version=(3, 10))

    def test_import_does_not_create_files_in_empty_user_state(self):
        before = self.snapshot()
        result = subprocess.run([sys.executable, '-B', '-c',
            'import src.apps.document_qa.definition_probe; print("IMPORT_OK")'],
            cwd=self.directory, env=dict(os.environ, HOME=str(self.home), PYTHONPATH=str(PROJECT)),
            capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'IMPORT_OK\n', ''))
        self.assertEqual(before, self.snapshot())

    def server(self, status=200, raw=None, kind='application/json', location=None):
        seen, fixture = [], self.f
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.command, self.path, dict(self.headers)))
                if status == 200 and raw is None:
                    if self.headers.get('Authorization') != 'Bearer ' + TOKEN or self.path not in fixture.rows:
                        self.send_error(403)
                        return
                    body = encoded(fixture.rows[self.path])
                else:
                    body = raw if raw is not None else b'PRIVATE_REMOTE_BODY'
                self.send_response(status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                if location:
                    self.send_header('Location', location)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                seen.append((self.command, self.path, dict(self.headers)))
                self.send_error(405)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:' + str(server.server_port), seen

    def test_standalone_cli_uses_four_authenticated_gets_without_proxy_or_file_changes(self):
        url, seen = self.server()
        trap, trapped = self.server(status=500)
        self.settings_for(url)
        helper = self.directory / 'downloaded-definition-probe.py'
        shutil.copyfile(Path(q.__file__), helper)
        before = self.snapshot()
        env = dict(os.environ, HOME=str(self.home), PYTHONPATH=str(PROJECT), NO_PROXY='', no_proxy='')
        for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'all_proxy'):
            env[key] = trap
        result = subprocess.run([sys.executable, '-B', str(helper), *self.args()], cwd=self.directory,
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        assert_report(self, report, self.f, {
            name: hashlib.sha256((self.project / name).read_bytes()).hexdigest() for name in SOURCE_PATHS})
        self.assertEqual(result.stderr, '')
        self.assert_private(result.stdout)
        self.assertEqual(len(seen), 4)
        self.assertEqual({path for _, path, _ in seen}, set(self.f.rows))
        self.assertTrue(all(method == 'GET' for method, _, _ in seen))
        self.assertTrue(all('Idempotency-Key' not in headers for _, _, headers in seen))
        self.assertTrue(all(headers.get('Content-Length') in (None, '0') for _, _, headers in seen))
        self.assertEqual(trapped, [])
        self.assertEqual(before, self.snapshot())

    def test_http_errors_json_failures_and_redirect_stop_without_leaking(self):
        trap, trapped = self.server(status=500)
        for status, raw, kind in ((302, b'PRIVATE_REDIRECT', 'application/json'),
                                   (401, b'PRIVATE_AUTH_BODY', 'application/json'),
                                   (403, b'PRIVATE_AUTH_BODY', 'application/json'),
                                   (404, b'PRIVATE_MISSING_BODY', 'application/json'),
                                   (500, b'PRIVATE_ERROR_BODY', 'application/json'),
                                   (200, b'{PRIVATE_BAD_JSON', 'application/json'),
                                   (200, b'{"x":1,"x":2}', 'application/json'),
                                   (200, b'{}', 'text/html')):
            url, seen = self.server(status=status, raw=raw, kind=kind, location=trap)
            self.settings_for(url)
            before = self.snapshot()
            with self.subTest(status=status, kind=kind):
                code, out, err = self.invoke()
                self.assertNotEqual(code, 0)
                self.assertEqual(out, '')
                self.assertIsInstance(json.loads(err), dict)
                self.assert_private(err)
                self.assertNotIn('PRIVATE', err)
                self.assertEqual(len(seen), 1)
                self.assertEqual(trapped, [])
                self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
