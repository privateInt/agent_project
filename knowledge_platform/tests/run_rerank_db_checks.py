#!/usr/bin/env python3
"""Run K07 optional rerank and existing search locking in disposable schemas."""
from pathlib import Path
import subprocess
from run_indexing_db_checks import connection

if __name__ == '__main__':
    try:
        root = Path(__file__).resolve().parents[2]
        connection(root / 'knowledge_platform', 'knowledge-service', 'KNOWLEDGE_TEST_POSTGRES_URL',
                   'knowledge-platform-tests:rerank', ['./service/app', '-run', 'TestSearch|TestDeleteWaitsForBoundedInFlightSearch'])
        print('PASS: K07 PostgreSQL rerank off/on/empty/error/cancel/deadline/read-delete checks; Model and OpenSearch are doubles.')
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit('FAIL: K07 database checks did not complete; inspect test output.') from None
