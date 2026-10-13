#!/usr/bin/env python3
"""Run opt-in integration suites in disposable schemas of configured PostgreSQL."""
import json
import os
from pathlib import Path
import subprocess


def connection(project, service, variable, image, packages):
    config = subprocess.run(['docker', 'compose', 'config', '--format', 'json'], cwd=project, capture_output=True, text=True, timeout=60)
    if config.returncode:
        raise ValueError('Cannot resolve ' + project.name + ' Compose settings')
    rendered = json.loads(config.stdout)
    runtime = rendered['services'][service]
    dsn = runtime['environment']['POSTGRES_URL']
    networks = list(runtime['networks'])
    network = rendered['networks'][networks[0]]['name']
    if not isinstance(dsn, str) or not dsn or not network:
        raise ValueError('Database connection or network is missing')
    environment = dict(os.environ)
    environment[variable] = dsn
    subprocess.run(['docker', 'run', '--rm', '--network', network, '-e', variable, image, 'go', 'test', '-count=1', '-timeout=180s', *packages], env=environment, check=True, timeout=300)


def main():
    root = Path(__file__).resolve().parents[4]
    connection(root / 'src/platform/knowledge', 'knowledge-service', 'KNOWLEDGE_TEST_POSTGRES_URL', 'knowledge-platform-tests:indexing', ['./service/app'])
    connection(root / 'src/platform/data', 'data-refinement', 'DATA_TEST_POSTGRES_URL', 'data-platform-builder:indexing', ['./refinement/app'])
    print('PASS: Knowledge catalog and Data reference integration suites; temporary schemas cleaned by test helpers.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit('FAIL: integration checks did not complete. Review test output; no deployment was started.') from None
