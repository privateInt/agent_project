"""Observe selected serving argv fields with one bounded Docker inspect.

Standalone stdlib helper; does not execute inside the serving container.
Configured argv does not prove effective template, routing, or answer quality.
"""
import argparse
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid

FORMAT = ('{"name":{{json .Name}},"image":{{json .Image}},'
          '"owner":{{json (index .Config.Labels "model-platform.owner")}},'
          '"deployment":{{json (index .Config.Labels "model-platform.deployment_id")}},'
          '"running":{{.State.Running}},"paused":{{.State.Paused}},'
          '"restarting":{{.State.Restarting}},"path":{{json .Path}},"args":{{json .Args}}}')
REASONS = {
    'ARGUMENTS_INVALID': 'arguments',
    'DOCKER_INSPECT_UNCONFIRMED': 'docker_inspect',
    'CONTAINER_IDENTITY_UNCONFIRMED': 'identity',
    'ENTRYPOINT_UNSUPPORTED': 'entrypoint',
    'ARGV_INVALID': 'argv',
    'MODEL_PIN_UNCONFIRMED': 'model_pin',
    'MAX_MODEL_LEN_UNCONFIRMED': 'max_model_len',
    'SERVING_METADATA_UNCONFIRMED': None,
}
PYTHON_PATHS = ('python', 'python3', '/usr/bin/python', '/usr/bin/python3',
                '/usr/local/bin/python', '/usr/local/bin/python3')


class ProbeError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProbeError('ARGUMENTS_INVALID')


def need(ok, reason):
    if not ok:
        raise ProbeError(reason)


def pins_checked(deployment, image):
    try:
        parsed = uuid.UUID(deployment)
        valid = str(parsed) == deployment and parsed.int != 0
    except (TypeError, ValueError, AttributeError):
        valid = False
    need(valid and isinstance(image, str)
         and re.fullmatch(r'sha256:[a-f0-9]{64}', image) is not None, 'ARGUMENTS_INVALID')


def inspect_once(deployment):
    command = ['docker', 'inspect', '--type', 'container', '--format', FORMAT,
               'model-serving-' + deployment]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    data, deadline = bytearray(), time.monotonic() + 8
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                need(remaining > 0 and selector.select(remaining), 'DOCKER_INSPECT_UNCONFIRMED')
                chunk = os.read(proc.stdout.fileno(), min(4096, 65537 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                need(len(data) <= 65536, 'DOCKER_INSPECT_UNCONFIRMED')
        remaining = deadline - time.monotonic()
        need(remaining > 0, 'DOCKER_INSPECT_UNCONFIRMED')
        need(proc.wait(timeout=remaining) == 0, 'DOCKER_INSPECT_UNCONFIRMED')
        return json.loads(data)
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()
        proc.stdout.close()


def summarize(row, deployment, image):
    pins_checked(deployment, image)
    need(isinstance(row, dict), 'CONTAINER_IDENTITY_UNCONFIRMED')
    need(row.get('name') == '/model-serving-' + deployment
         and row.get('owner') == 'deployment-serving'
         and row.get('deployment') == deployment and row.get('image') == image
         and row.get('running') is True and row.get('paused') is False
         and row.get('restarting') is False, 'CONTAINER_IDENTITY_UNCONFIRMED')
    args = row.get('args')
    need(isinstance(args, list) and len(args) <= 256
         and all(isinstance(x, str) and len(x) <= 4096 for x in args), 'ARGV_INVALID')
    if row.get('path') in PYTHON_PATHS:
        need(args[:2] == ['-m', 'vllm.entrypoints.openai.api_server'], 'ENTRYPOINT_UNSUPPORTED')
        offset, family = 2, 'python_module'
    elif row.get('path') in ('vllm', '/usr/local/bin/vllm'):
        need(args[:1] == ['serve'], 'ENTRYPOINT_UNSUPPORTED')
        offset, family = 1, 'vllm_cli'
    else:
        raise ProbeError('ENTRYPOINT_UNSUPPORTED')
    keys = {'--model', '--served-model-name', '--max-model-len', '--chat-template', '--tokenizer'}
    values = {}
    for index, token in enumerate(args[offset:], start=offset):
        key, equal, value = token.partition('=')
        if key not in keys:
            continue
        need(key not in values, 'ARGV_INVALID')
        if not equal:
            need(index + 1 < len(args), 'ARGV_INVALID')
            value = args[index + 1]
        need(bool(value) and not value.startswith('--'), 'ARGV_INVALID')
        values[key] = value
    need(values.get('--model') == '/models/' + deployment + '/model'
         and values.get('--served-model-name') == 'deployment-' + deployment, 'MODEL_PIN_UNCONFIRMED')
    length = values.get('--max-model-len', '')
    need(re.fullmatch(r'[0-9]{1,5}', length) is not None
         and 128 <= int(length) <= 32768, 'MAX_MODEL_LEN_UNCONFIRMED')
    return {'schema': 1, 'status': 'OBSERVED', 'scope': 'configured_argv_only',
            'entrypoint_family': family, 'docker_inspect_calls': 1,
            'image_matches_pin': True, 'model_path_matches_pin': True,
            'served_alias_matches_pin': True, 'max_model_len': int(length),
            'explicit_chat_template_option': '--chat-template' in values,
            'explicit_tokenizer_option': '--tokenizer' in values,
            'active_runtime_verified': False, 'template_verified': False,
            'payload_verified': False, 'head_readback_verified': False,
            'answer_quality_verified': False, 'f0a_accepted': False}


def main(argv=None):
    stage = 'arguments'
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('--deployment', required=True)
        parser.add_argument('--image', required=True)
        args = parser.parse_args(argv)
        pins_checked(args.deployment, args.image)
        stage = 'docker_inspect'
        row = inspect_once(args.deployment)
        stage = 'summary'
        print(json.dumps(summarize(row, args.deployment, args.image), sort_keys=True))
        return 0
    except Exception as exc:
        reason = (exc.reason if isinstance(exc, ProbeError) and isinstance(exc.reason, str)
                  and exc.reason in REASONS else 'SERVING_METADATA_UNCONFIRMED')
        print(json.dumps({'schema': 1, 'status': 'BLOCKED', 'scope': 'configured_argv_only',
                          'reason': reason, 'stage': REASONS[reason] or stage,
                          'f0a_accepted': False}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
