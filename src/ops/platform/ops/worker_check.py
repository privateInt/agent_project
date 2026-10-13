#!/usr/bin/env python3
"""Bounded isolated host-process acceptance; never starts a production worker."""
import io
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.ops.platform.tests.test_worker_process import ProcessTests


def main():
    # Fixed test list, no user-supplied target, directory, credentials or daemon.
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProcessTests)
    names = [case._testMethodName for case in suite]
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
    failed = {case._testMethodName for case, _ in result.failures + result.errors}
    print(json.dumps({'schema': 1, 'stage': 'O04-guarded-worker-process-check',
        'result': 'passed' if result.wasSuccessful() else 'attention_required',
        'scope': 'isolated_native_host_processes_with_synthetic_evaluations',
        'python_version': sys.version.split()[0],
        'cases': {name.removeprefix('test_'): 'failed' if name in failed else 'passed' for name in names},
        'production_state_read': False, 'production_credentials_read': False,
        'docker_commands': 0, 'upstream_api_requests': 0, 'inference_requested': False,
        'production_worker_started': False, 'service_unit_or_timer_installed': False,
        'configuration_activation_acceptance': False, 'systemd_lifecycle_acceptance': False,
        'temporary_directory_cleanup': 'passed' if result.wasSuccessful() else 'inspect_failed_cases',
        'failures': [{'case': case._testMethodName.removeprefix('test_'), 'detail': message.splitlines()[-1][:200]}
                     for case, message in result.failures + result.errors]}, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() else 2


if __name__ == '__main__': raise SystemExit(main())
