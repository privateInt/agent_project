#!/usr/bin/env python3
"""Build parser and Go test images; require all PostgreSQL tests to actually run.

Uses only Python's standard library and Docker. Reads existing Compose settings,
never prints them, changes no running service, and tests in disposable DB schemas.
"""
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run():
    config = subprocess.run(['docker', 'compose', 'config', '--format', 'json'], cwd=ROOT, check=True, capture_output=True, text=True, timeout=60)
    settings = json.loads(config.stdout)
    dsn = settings['services']['data-refinement']['environment']['POSTGRES_URL']
    network = settings['networks']['data-infra']['name']
    if not isinstance(dsn, str) or not dsn or not isinstance(network, str) or not network:
        raise ValueError('Existing Data Compose connection settings are incomplete')
    subprocess.run(['docker', 'build', '--target', 'parser-tests', '-t', 'data-platform-check:refinement-parser', '.'], cwd=ROOT, check=True)
    subprocess.run(['docker', 'build', '--target', 'builder', '-t', 'data-platform-check:refinement', '.'], cwd=ROOT, check=True)
    report = ROOT / 'tests/results/refinement/go-tests.jsonl'
    report.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DATA_TEST_POSTGRES_URL=dsn)
    passed = skipped = failed = 0
    # Environment is passed by name; the DSN never becomes a CLI argument.
    with report.open('w', encoding='utf-8') as output:
        process = subprocess.Popen(['docker', 'run', '--rm', '--network', network, '-e', 'DATA_TEST_POSTGRES_URL', 'data-platform-check:refinement', 'go', 'test', '-count=1', '-json', './...'], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            output.write(line)
            output.flush()
            try: event = json.loads(line)
            except ValueError:
                print(line, end='')
                continue
            test = event.get('Test')
            action = event.get('Action')
            if test and action == 'skip': skipped += 1
            if action == 'fail': failed += 1
            if test and '/' not in test and action == 'pass': passed += 1
            if action in ('pass', 'fail', 'skip') and test:
                print(action.upper(), event.get('Package', ''), test, flush=True)
        code = process.wait()
    print('Go test log:', report)
    if code or skipped or failed or passed == 0:
        raise ValueError('Go/DB gate failed or skipped tests; inspect the saved log before changing services')
    print('PASS: parser tests and Go/PostgreSQL tests, no test skips; service images have not been switched.')

if __name__ == '__main__':
    try: run()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise SystemExit('FAIL: verification did not complete; inspect the preceding test/build output. Connection settings were not printed.') from None
