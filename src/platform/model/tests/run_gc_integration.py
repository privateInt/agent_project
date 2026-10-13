"""Opt-in GC acceptance: one CPU fixture + unique MinIO bucket; no production DB writes.

Use the current builder image. Credentials are forwarded via the subprocess
environment, never placed in shell text, command arguments or printed output.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def parsed(args, label):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(label + ' failed; inspect the command locally')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--training-logs', action='store_true',
                        help='verify Training log reader before and after isolated GC removal')
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != root:
        raise RuntimeError('run from ~/workspace/agent_project/model_platform')
    config = json.loads(parsed(['docker', 'compose', 'config', '--format', 'json'], 'Compose config'))
    settings = config['services']['gc']['environment']
    env = dict(os.environ)
    for key in ('MINIO_ENDPOINT', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'MINIO_USE_SSL'):
        value = str(settings[key])
        if not value or '${' in value:
            raise RuntimeError('GC setting is unresolved: ' + key)
        env['GC_TEST_' + key] = value
    # Reuse the current local Operation runtime (Debian + /bin/sh); no pull/GPU.
    image = parsed(['docker', 'inspect', '--type', 'container', '--format', '{{.Image}}',
                    config['services']['operation']['container_name']], 'Operation image lookup').strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise RuntimeError('Operation did not resolve to an immutable image ID')
    env['GC_TEST_IMAGE'] = image
    env['GC_TEST_NETWORK'] = config['networks']['ops-infra']['name']
    keys = ['GC_TEST_' + key for key in ('MINIO_ENDPOINT', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'MINIO_USE_SSL', 'IMAGE', 'NETWORK')]
    args = ['docker', 'run', '--rm', '--pull=never', '--network', env['GC_TEST_NETWORK'],
            '-v', '/var/run/docker.sock:/var/run/docker.sock']
    for key in keys:
        args += ['-e', key]
    # builder has Go; gc-test additionally contains Docker CLI.
    test = 'TestLiveTrainingLogArchiveFallback' if options.training_logs else 'TestLiveGCArchiveAndRemoval'
    image_tag = 'model-platform-check:training-log-archive' if options.training_logs else 'model-platform-check:gc'
    args += [image_tag, 'go', 'test', '-count=1', '-v', './gc', '-run', '^' + test + '$']
    print('Running isolated CPU Docker/MinIO GC acceptance (no model or training request).', flush=True)
    result = subprocess.run(args, env=env, timeout=240, capture_output=True, text=True)
    print(result.stdout, end='', flush=True)
    print(result.stderr, end='', file=sys.stderr, flush=True)
    if result.returncode or not re.search(r'^--- PASS: ' + re.escape(test) + r' \(', result.stdout, re.MULTILINE):
        raise RuntimeError('GC integration did not pass; inspect the test/cleanup errors and confirm the current test image')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit('FAIL: ' + str(exc)) from None
