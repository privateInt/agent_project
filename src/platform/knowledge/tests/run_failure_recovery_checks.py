#!/usr/bin/env python3
"""Opt-in catalog recovery checks in disposable PostgreSQL schemas.

Build knowledge-platform-tests:failure-recovery first. Its tests target runs
the loopback gRPC/HTTP and reconciliation suites without live dependencies.
This command adds real PostgreSQL transactions, with Model/Data/OpenSearch
replaced by test doubles. It does not restart any platform service.
"""
from pathlib import Path
import subprocess

from run_indexing_db_checks import connection


def main():
    root = Path(__file__).resolve().parents[4]
    connection(
        root / "src/platform/knowledge",
        "knowledge-service",
        "KNOWLEDGE_TEST_POSTGRES_URL",
        "knowledge-platform-tests:failure-recovery",
        ["-v", "./service/app"],
    )
    print(
        "PASS: Knowledge catalog recovery checks completed in temporary schemas. "
        "No live Model/Data/OpenSearch calls or service restarts."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit(
            "FAIL: recovery checks did not complete; inspect the test output. "
            "No platform service was restarted."
        ) from None
