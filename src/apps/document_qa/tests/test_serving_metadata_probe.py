"""Synthetic serving metadata and fake Docker; no native daemon or inference."""
import ast
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5

from src.apps.document_qa import serving_metadata_probe as q


PRIVATE = 'PRIVATE_SERVING_METADATA_비공개'
PINS = [(str(uuid5(NAMESPACE_URL, 'serving-probe-test/' + name)),
         'sha256:' + hashlib.sha256(name.encode()).hexdigest()) for name in ('first', 'second')]
FIELDS = {'schema', 'status', 'scope', 'entrypoint_family', 'docker_inspect_calls',
          'image_matches_pin', 'model_path_matches_pin', 'served_alias_matches_pin',
          'max_model_len', 'explicit_chat_template_option', 'explicit_tokenizer_option',
          'active_runtime_verified', 'template_verified', 'payload_verified',
          'head_readback_verified', 'answer_quality_verified', 'f0a_accepted'}
FALSE_FIELDS = {'active_runtime_verified', 'template_verified', 'payload_verified',
                'head_readback_verified', 'answer_quality_verified', 'f0a_accepted'}


def fixture(deployment, image, path='vllm', equal=False, chat=False, tokenizer=False, length='4096'):
    pairs = [('--model', '/models/' + deployment + '/model'),
             ('--served-model-name', 'deployment-' + deployment), ('--max-model-len', length)]
    if chat:
        pairs.append(('--chat-template', '/private/' + PRIVATE + '/template.jinja'))
    if tokenizer:
        pairs.append(('--tokenizer', '/private/' + PRIVATE + '/tokenizer'))
    prefix = ['serve'] if path in ('vllm', '/usr/local/bin/vllm') else ['-m', 'vllm.entrypoints.openai.api_server']
    args = list(prefix)
    for key, value in pairs:
        args.extend([key + '=' + value] if equal else [key, value])
    return {'name': '/model-serving-' + deployment, 'image': image, 'owner': 'deployment-serving',
            'deployment': deployment, 'running': True, 'paused': False, 'restarting': False,
            'path': path, 'args': args, 'unknown_private': PRIVATE}


def synthetic_eighteen_args(deployment, image):
    """CreateArgs's 17 option tokens plus serve; not captured operator argv."""
    row = fixture(deployment, image)
    row['args'].extend(['--host', '0.0.0.0', '--port', '8000', '--dtype', 'auto',
                       '--gpu-memory-utilization', '0.5', '--tensor-parallel-size', '1', '--enforce-eager'])
    assert len(row['args']) == 18
    return row


def invoke(argv, row=None, error=None):
    out, err = io.StringIO(), io.StringIO()
    with patch.object(q, 'inspect_once', side_effect=error, return_value=row) as inspector:
        with redirect_stdout(out), redirect_stderr(err):
            code = q.main(argv)
    return code, out.getvalue(), err.getvalue(), inspector


def assert_private(test, text, deployment, image):
    for value in (PRIVATE, deployment, image, '/models/', '/private/', 'PRIVATE_DOCKER_STDERR', 'Traceback'):
        test.assertNotIn(value, text)


def assert_report(test, result, family, chat=False, tokenizer=False, length=4096):
    test.assertEqual(set(result), FIELDS)
    test.assertEqual((result['schema'], result['status'], result['scope']),
                     (1, 'OBSERVED', 'configured_argv_only'))
    test.assertEqual(result['entrypoint_family'], family)
    test.assertIs(type(result['docker_inspect_calls']), int)
    test.assertEqual(result['docker_inspect_calls'], 1)
    test.assertIs(type(result['max_model_len']), int)
    test.assertEqual(result['max_model_len'], length)
    test.assertIs(result['explicit_chat_template_option'], chat)
    test.assertIs(result['explicit_tokenizer_option'], tokenizer)
    for key in ('image_matches_pin', 'model_path_matches_pin', 'served_alias_matches_pin'):
        test.assertIs(result[key], True)
    for key in FALSE_FIELDS:
        test.assertIs(result[key], False)


def assert_blocked(test, code, out, err, reason, stage):
    test.assertEqual(code, 2)
    test.assertEqual(out, '')
    test.assertEqual(json.loads(err), {'schema': 1, 'status': 'BLOCKED',
        'scope': 'configured_argv_only', 'reason': reason, 'stage': stage, 'f0a_accepted': False})


class SummaryTests(unittest.TestCase):
    def test_python_vllm_spellings_optional_values_and_external_pins(self):
        for deployment, image in PINS:
            for path in ('python3', 'vllm', '/usr/local/bin/vllm'):
                for equal in (False, True):
                    for chat, tokenizer in ((False, False), (True, False), (False, True), (True, True)):
                        with self.subTest(path=path, equal=equal, chat=chat, tokenizer=tokenizer):
                            row = fixture(deployment, image, path, equal, chat, tokenizer)
                            before = copy.deepcopy(row)
                            result = q.summarize(row, deployment, image)
                            assert_report(self, result, 'python_module' if path == 'python3' else 'vllm_cli', chat, tokenizer)
                            assert_private(self, json.dumps(result), deployment, image)
                            self.assertEqual(row, before)
        deployment, image = PINS[0]
        for path in q.PYTHON_PATHS:
            assert_report(self, q.summarize(fixture(deployment, image, path), deployment, image), 'python_module')
        for length in ('128', '32768'):
            assert_report(self, q.summarize(fixture(deployment, image, length=length), deployment, image),
                          'vllm_cli', length=int(length))

    def test_vllm_serve_eighteen_arguments_main_is_observed(self):
        deployment, image = PINS[0]
        row = synthetic_eighteen_args(deployment, image)
        self.assertEqual(row['path'], 'vllm')
        self.assertEqual(len(row['args']), 18)
        code, out, err, inspector = invoke(['--deployment', deployment, '--image', image], row)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, '')
        inspector.assert_called_once_with(deployment)
        assert_report(self, json.loads(out), 'vllm_cli')
        assert_private(self, out, deployment, image)

    def test_identity_image_and_state_mismatches_are_blocked(self):
        deployment, image = PINS[0]
        changes = [('name', '/' + PRIVATE), ('owner', PRIVATE), ('deployment', PINS[1][0]),
                   ('image', PINS[1][1]), ('running', False), ('running', 1),
                   ('paused', True), ('paused', 0), ('restarting', True), ('restarting', 'false')]
        for key, value in changes:
            row = fixture(deployment, image)
            row[key] = value
            with self.subTest(field=key):
                code, out, err, inspector = invoke(['--deployment', deployment, '--image', image], row)
                assert_blocked(self, code, out, err, 'CONTAINER_IDENTITY_UNCONFIRMED', 'identity')
                inspector.assert_called_once_with(deployment)
                assert_private(self, err, deployment, image)
        for key in ('name', 'owner', 'deployment', 'image', 'running', 'paused', 'restarting'):
            row = fixture(deployment, image)
            row.pop(key)
            with self.subTest(missing=key), self.assertRaises(q.ProbeError):
                q.summarize(row, deployment, image)
        for row in (None, [], PRIVATE):
            with self.subTest(kind=type(row).__name__), self.assertRaises(q.ProbeError):
                q.summarize(row, deployment, image)

    def test_duplicate_missing_empty_and_flag_shaped_selected_options(self):
        deployment, image = PINS[0]
        for equal in (False, True):
            for key in ('--model', '--served-model-name', '--max-model-len', '--chat-template', '--tokenizer'):
                row = fixture(deployment, image, equal=equal, chat=True, tokenizer=True)
                row['args'].extend([key + '=' + PRIVATE] if equal else [key, PRIVATE])
                with self.subTest(equal=equal, duplicate=key), self.assertRaises(q.ProbeError) as caught:
                    q.summarize(row, deployment, image)
                self.assertEqual(caught.exception.reason, 'ARGV_INVALID')
        for key, reason in (('--model', 'MODEL_PIN_UNCONFIRMED'),
                            ('--served-model-name', 'MODEL_PIN_UNCONFIRMED'),
                            ('--max-model-len', 'MAX_MODEL_LEN_UNCONFIRMED')):
            row = fixture(deployment, image)
            index = row['args'].index(key)
            del row['args'][index:index + 2]
            with self.subTest(missing=key), self.assertRaises(q.ProbeError) as caught:
                q.summarize(row, deployment, image)
            self.assertEqual(caught.exception.reason, reason)
        for key in ('--chat-template', '--tokenizer'):
            for tail in ([key], [key + '='], [key, '--another-option']):
                row = fixture(deployment, image)
                row['args'].extend(tail)
                with self.subTest(invalid=tail), self.assertRaises(q.ProbeError) as caught:
                    q.summarize(row, deployment, image)
                self.assertEqual(caught.exception.reason, 'ARGV_INVALID')
        for key in ('--model', '--served-model-name'):
            row = fixture(deployment, image)
            row['args'][row['args'].index(key) + 1] = PRIVATE
            with self.subTest(mismatched=key), self.assertRaises(q.ProbeError) as caught:
                q.summarize(row, deployment, image)
            self.assertEqual(caught.exception.reason, 'MODEL_PIN_UNCONFIRMED')

    def test_invalid_lengths_unicode_and_argv_shape_limits(self):
        deployment, image = PINS[0]
        for length in ('127', '32769', '0', '-128', '+4096', '4096.0', ' 4096', '4096 ',
                       '４０９６', '٤٠٩٦', '1' * 100):
            with self.subTest(length=length), self.assertRaises(q.ProbeError) as caught:
                q.summarize(fixture(deployment, image, length=length), deployment, image)
            self.assertEqual(caught.exception.reason, 'MAX_MODEL_LEN_UNCONFIRMED')
        for args in (None, PRIVATE, ['serve', 7], fixture(deployment, image)['args'] + ['x'] * 257,
                     fixture(deployment, image)['args'] + ['x' * 4097]):
            row = fixture(deployment, image)
            row['args'] = args
            with self.subTest(kind=type(args).__name__), self.assertRaises(q.ProbeError) as caught:
                q.summarize(row, deployment, image)
            self.assertEqual(caught.exception.reason, 'ARGV_INVALID')

    def test_shell_unknown_entrypoints_and_wrong_prefixes(self):
        deployment, image = PINS[0]
        for path in ('sh', '/bin/sh', 'bash', '/bin/bash', '/private/entrypoint.sh', '/usr/bin/vllm', 'python3.13'):
            row = fixture(deployment, image)
            row['path'] = path
            with self.subTest(path=path), self.assertRaises(q.ProbeError) as caught:
                q.summarize(row, deployment, image)
            self.assertEqual(caught.exception.reason, 'ENTRYPOINT_UNSUPPORTED')
        for path, args in (('python3', ['-c', PRIVATE]), ('python3', ['-m', 'unknown.module']),
                           ('vllm', ['unknown', PRIVATE]), ('vllm', [])):
            row = fixture(deployment, image, path)
            row['args'] = args
            with self.subTest(path=path, prefix=args), self.assertRaises(q.ProbeError) as caught:
                q.summarize(row, deployment, image)
            self.assertEqual(caught.exception.reason, 'ENTRYPOINT_UNSUPPORTED')

    def test_cli_private_arguments_and_exception_text_are_not_printed(self):
        deployment, image = PINS[0]
        invalid = [[], ['--deployment', deployment], ['--image', image], ['--deployment'],
                   ['--deployment', PRIVATE, '--image', image],
                   ['--deployment', deployment, '--image', PRIVATE],
                   ['--deployment', deployment, '--image', image, '--PRIVATE_UNKNOWN', PRIVATE]]
        for argv in invalid:
            with self.subTest(argv=argv):
                code, out, err, inspector = invoke(argv)
                assert_blocked(self, code, out, err, 'ARGUMENTS_INVALID', 'arguments')
                inspector.assert_not_called()
                assert_private(self, err, deployment, image)
        for bad_deployment, bad_image in (('00000000-0000-0000-0000-000000000000', image),
                (deployment.upper(), image), (deployment, 'sha256:' + 'A' * 64),
                (deployment, 'sha256:' + 'a' * 63)):
            with self.assertRaises(q.ProbeError):
                q.pins_checked(bad_deployment, bad_image)
        row = fixture(deployment, image, chat=True, tokenizer=True)
        row['args'].extend(['--api-key', PRIVATE, '--ignored=' + PRIVATE])
        code, out, err, _ = invoke(['--deployment', deployment, '--image', image], row)
        self.assertEqual((code, err), (0, ''))
        assert_private(self, out, deployment, image)
        for exception, reason in ((RuntimeError(PRIVATE), 'SERVING_METADATA_UNCONFIRMED'),
                                  (q.ProbeError(PRIVATE), 'SERVING_METADATA_UNCONFIRMED'),
                                  (q.ProbeError('DOCKER_INSPECT_UNCONFIRMED'), 'DOCKER_INSPECT_UNCONFIRMED')):
            code, out, err, _ = invoke(['--deployment', deployment, '--image', image], error=exception)
            assert_blocked(self, code, out, err, reason, 'docker_inspect')
            assert_private(self, err, deployment, image)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.deployment, self.image = PINS[0]
        self.payload, self.record = self.root / 'payload', self.root / 'commands'
        self.home = self.root / 'empty-home'
        self.home.mkdir()
        docker = self.root / 'docker'
        docker.write_text('#!' + sys.executable + '\n'
            'import json,os,sys\n'
            'with open(os.environ["SM_TEST_RECORD"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
            'sys.stderr.write("PRIVATE_DOCKER_STDERR_NEVER_PRINT")\n'
            'sys.stdout.buffer.write(open(os.environ["SM_TEST_PAYLOAD"],"rb").read())\n'
            'sys.stdout.buffer.flush()\n'
            'sys.exit(int(os.environ.get("SM_TEST_EXIT","0")))\n')
        docker.chmod(0o700)
        self.environment = {'PATH': str(self.root) + os.pathsep + os.environ.get('PATH', ''),
            'SM_TEST_RECORD': str(self.record), 'SM_TEST_PAYLOAD': str(self.payload), 'SM_TEST_EXIT': '0',
            'HOME': str(self.home), 'PYTHONPATH': str(self.root / 'absent-import-root')}

    def one_inspect(self):
        commands = [json.loads(line) for line in self.record.read_text().splitlines()]
        self.assertEqual(commands, [['inspect', '--type', 'container', '--format', q.FORMAT,
                                     'model-serving-' + self.deployment]])
        fields = set(re.findall(r'\{\{(.*?)\}\}', q.FORMAT))
        self.assertEqual(fields, {'json .Name', 'json .Image',
            'json (index .Config.Labels "model-platform.owner")',
            'json (index .Config.Labels "model-platform.deployment_id")',
            '.State.Running', '.State.Paused', '.State.Restarting', 'json .Path', 'json .Args'})

    def main(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.environment), redirect_stdout(out), redirect_stderr(err):
            code = q.main(['--deployment', self.deployment, '--image', self.image])
        return code, out.getvalue(), err.getvalue()

    def test_standalone_isolated_cli_uses_one_inspect_without_repository_imports(self):
        self.payload.write_bytes(json.dumps(synthetic_eighteen_args(self.deployment, self.image)).encode())
        helper = self.root / 'standalone-probe.py'
        shutil.copyfile(q.__file__, helper)
        helper_before = helper.read_bytes()
        result = subprocess.run([sys.executable, '-I', '-B', str(helper), '--deployment', self.deployment,
                                 '--image', self.image], cwd=self.home,
                                env=dict(os.environ, **self.environment), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        assert_report(self, json.loads(result.stdout), 'vllm_cli')
        assert_private(self, result.stdout, self.deployment, self.image)
        self.one_inspect()
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(helper.read_bytes(), helper_before)
        self.assertEqual(list(self.root.rglob('__pycache__')), [])

    def test_nonzero_and_invalid_json_discard_private_output_without_retry(self):
        for raw, exit_code, reason in ((json.dumps(fixture(self.deployment, self.image)).encode(), '17', 'DOCKER_INSPECT_UNCONFIRMED'),
                                      (b'PRIVATE_PARTIAL_INVALID_JSON', '0', 'SERVING_METADATA_UNCONFIRMED')):
            self.payload.write_bytes(raw)
            self.environment['SM_TEST_EXIT'] = exit_code
            if self.record.exists():
                self.record.unlink()
            with self.subTest(exit_code=exit_code):
                code, out, err = self.main()
                assert_blocked(self, code, out, err, reason, 'docker_inspect')
                assert_private(self, err, self.deployment, self.image)
                self.one_inspect()

    def test_exact_capture_limit_and_limit_plus_one_detection(self):
        row = fixture(self.deployment, self.image)
        row['padding'] = ''
        row['padding'] = 'P' * (65536 - len(json.dumps(row).encode()))
        raw = json.dumps(row).encode()
        self.assertEqual(len(raw), 65536)
        for raw, expected, reason in ((raw, 0, None), (b'P' * 100000, 2, 'DOCKER_INSPECT_UNCONFIRMED')):
            self.payload.write_bytes(raw)
            if self.record.exists():
                self.record.unlink()
            chunks, requests, real_read = [], [], os.read
            def bounded_read(fd, size):
                requests.append(size)
                data = real_read(fd, size)
                chunks.append(len(data))
                return data
            safe_os = types.SimpleNamespace(read=bounded_read, killpg=os.killpg)
            with self.subTest(expected=expected), patch.object(q, 'os', safe_os):
                code, out, err = self.main()
                self.assertEqual(code, expected)
                self.assertEqual(sum(chunks), 65536 if expected == 0 else 65537)
                self.assertTrue(all(0 < size <= 4096 for size in requests))
                if expected == 0:
                    assert_report(self, json.loads(out), 'vllm_cli')
                    self.assertEqual(err, '')
                else:
                    assert_blocked(self, code, out, err, reason, 'docker_inspect')
                self.one_inspect()

    def test_timeout_deadline_kill_wait_and_pipe_cleanup_without_real_sleep(self):
        for times, selected in (([0, 9], [(1, 1)]), ([0, 0], []), ([0, 0, 9], [(1, 1)])):
            proc = Mock()
            proc.stdout = Mock()
            proc.stdout.fileno.return_value = 99
            proc.pid = 12345
            proc.poll.return_value = None
            proc.wait.return_value = -9
            selector = Mock()
            selector.select.return_value = selected
            context = Mock()
            context.__enter__ = Mock(return_value=selector)
            context.__exit__ = Mock(return_value=None)
            killer = Mock()
            safe_os = types.SimpleNamespace(read=Mock(return_value=b''), killpg=killer)
            with self.subTest(times=times), patch.object(q.subprocess, 'Popen', return_value=proc) as popen, \
                    patch.object(q.selectors, 'DefaultSelector', return_value=context), \
                    patch.object(q.time, 'monotonic', side_effect=times), patch.object(q, 'os', safe_os):
                with self.assertRaises(q.ProbeError) as caught:
                    q.inspect_once(self.deployment)
            self.assertEqual(caught.exception.reason, 'DOCKER_INSPECT_UNCONFIRMED')
            self.assertEqual(popen.call_count, 1)
            self.assertIs(popen.call_args.kwargs['stderr'], q.subprocess.DEVNULL)
            self.assertIs(popen.call_args.kwargs['start_new_session'], True)
            killer.assert_called_once_with(12345, signal.SIGKILL)
            proc.wait.assert_called_once_with()
            proc.stdout.close.assert_called_once()
            if len(times) == 2 and selected:
                selector.select.assert_not_called()
            else:
                selector.select.assert_called_once_with(8)

    def test_python310_syntax_is_distinct_from_actual_python310_execution(self):
        ast.parse(Path(q.__file__).read_text(), feature_version=(3, 10))
        ast.parse(Path(__file__).read_text(), feature_version=(3, 10))


if __name__ == '__main__':
    unittest.main()
