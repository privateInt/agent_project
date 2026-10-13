#!/usr/bin/env python3
"""Bounded fixture service checks and offline unit parsing; no real unit control."""
import io
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.ops.platform.tests.test_service_lifecycle import ServiceTests, AdapterTests, ProcessServiceTests, NativeUnitTests


def main():
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (ServiceTests, AdapterTests, ProcessServiceTests, NativeUnitTests))
    names = [case._testMethodName for group in suite for case in group]
    log = io.StringIO(); result = unittest.TextTestRunner(stream=log, verbosity=0).run(suite)
    failed = {case._testMethodName for case, _ in result.failures + result.errors}
    skipped = {case._testMethodName for case, _ in result.skipped}
    passed = result.wasSuccessful() and not skipped
    print(json.dumps({'schema': 1, 'stage': 'O04-service-adapter-isolated-check',
        'result': 'passed' if passed else 'attention_required',
        'scope': 'synthetic_manager_real_worker_processes_offline_unit_parser',
        'python_version': sys.version.split()[0], 'cases': {name.removeprefix('test_'):
            'failed' if name in failed else 'unavailable' if name in skipped else 'passed' for name in names},
        'production_state_read': False, 'production_credentials_read': False,
        'docker_commands': 0, 'upstream_api_requests': 0, 'inference_requested': False,
        'production_worker_started': False, 'systemd_control_commands': 0,
        'service_unit_or_timer_installed': False, 'native_systemd_lifecycle_acceptance': False,
        'native_docker_configuration_acceptance': False, 'logout_or_boot_acceptance': False,
        'temporary_directory_cleanup': 'passed' if result.wasSuccessful() else 'inspect_failed_cases',
        'failures': [{'case': case._testMethodName.removeprefix('test_'), 'detail': message.splitlines()[-1][:200]}
                     for case, message in result.failures + result.errors],
        'unavailable': [{'case': case._testMethodName.removeprefix('test_'), 'reason': reason}
                        for case, reason in result.skipped]}, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == '__main__': raise SystemExit(main())
