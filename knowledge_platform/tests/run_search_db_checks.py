#!/usr/bin/env python3
"""Run K05 catalog read/deletion tests in a disposable PostgreSQL schema."""
from pathlib import Path
import subprocess
from run_indexing_db_checks import connection


if __name__ == '__main__':
    try:
        root = Path(__file__).resolve().parents[2]
        connection(root / 'knowledge_platform', 'knowledge-service', 'KNOWLEDGE_TEST_POSTGRES_URL',
                   'knowledge-platform-tests:search', ['./service/app', '-run', 'TestSearch|TestDeleteWaitsForBoundedInFlightSearch'])
        print('PASS: K05 PostgreSQL version/space/read-delete/cancellation checks; disposable schemas cleaned. Model/OpenSearch are doubles.')
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit('FAIL: K05 database checks did not complete; inspect test output.') from None
