#!/usr/bin/env python3
"""Verify changed Model/Data packages using isolated PostgreSQL test schemas.

No service restart, model import, inference or GPU allocation occurs here.
Uses Python standard library and the existing Compose connection settings.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess

PROJECT = Path(__file__).resolve().parents[2]


def compose(root):
    result = subprocess.run(['docker', 'compose', 'config', '--format', 'json'], cwd=root,
                            capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)


def go_gate(root, image, network, variable, dsn, packages, report):
    env = dict(os.environ)
    env[variable] = dsn
    report.parent.mkdir(parents=True, exist_ok=True)
    passed = skipped = failed = 0
    with report.open('w') as output:
        proc = subprocess.Popen(['docker', 'run', '--rm', '--network', network, '-e', variable, image,
                                 'go', 'test', '-count=1', '-json', *packages], cwd=root, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            output.write(line); output.flush()
            try:
                event = json.loads(line)
            except ValueError:
                print(line, end=''); continue
            action, test = event.get('Action'), event.get('Test')
            if action == 'fail': failed += 1
            if test and action == 'skip': skipped += 1
            if test and action == 'pass' and '/' not in test: passed += 1
            if test and action in ('pass', 'fail', 'skip'):
                print(action.upper(), event.get('Package', ''), test, flush=True)
        code = proc.wait()
    if code or failed or skipped or not passed:
        raise ValueError('Go/DB gate failed or skipped tests: ' + str(report))
    print('PASS:', root.name, passed, 'tests; no skips; log:', report)


def main():
    model, data = PROJECT / 'model_platform', PROJECT / 'data_platform'
    left = (model / 'contracts/ocr/v1/ocr.proto').read_bytes()
    right = (data / 'contracts/ocr/v1/ocr.proto').read_bytes()
    if hashlib.sha256(left).digest() != hashlib.sha256(right).digest():
        raise ValueError('Model/Data OCR contracts differ')
    m, d = compose(model), compose(data)
    if m['networks']['ops-infra']['name'] != d['networks']['data-infra']['name']:
        raise ValueError('Managed OCR requires Model/Data services on the same Docker network')
    for root, target, image in [(data, 'parser-tests', 'data-platform-check:ocr-parser'),
                                (model, 'builder', 'model-platform-check:ocr'),
                                (data, 'builder', 'data-platform-check:ocr')]:
        subprocess.run(['docker', 'build', '--target', target, '-t', image, '.'], cwd=root, check=True)
    logs = data / 'tests/results/ocr'
    go_gate(model, 'model-platform-check:ocr', m['networks']['ops-infra']['name'],
            'OPERATION_TEST_POSTGRES_URL', m['services']['deployment']['environment']['POSTGRES_URL'],
            ['./deployment/...', './gateway/app'], logs / 'model-go-tests.jsonl')
    go_gate(data, 'data-platform-check:ocr', d['networks']['data-infra']['name'],
            'DATA_TEST_POSTGRES_URL', d['services']['data-refinement']['environment']['POSTGRES_URL'],
            ['./refinement/...', './gateway/...', './internal/canonical', './internal/ocr',
             './jobs/refinement/parse', './dispatcher/...', './reconciler/...'], logs / 'data-go-tests.jsonl')
    print('PASS: OCR parser, Model/Data contracts and Go/PostgreSQL gates; ready to rebuild affected services.')


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise SystemExit('FAIL: OCR verification did not complete; inspect the preceding test/build output. Connection settings were not printed.') from None
