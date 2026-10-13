#!/usr/bin/env python3
"""Run isolated adapter file/lease tests; never inspect a production installation."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
STAGE='O04-maintenance-execution-isolated-check'


class Results(unittest.TestResult):
    def __init__(self):super().__init__();self.cases={}
    def addSuccess(self,test):super().addSuccess(test);self.cases[test.id()]='passed'
    def addFailure(self,test,err):super().addFailure(test,err);self.cases[test.id()]='failed'
    def addError(self,test,err):super().addError(test,err);self.cases[test.id()]='error'
    def addSkip(self,test,reason):super().addSkip(test,reason);self.cases[test.id()]='unavailable'


def main():
    paths=[];original=tempfile.TemporaryDirectory
    class Tracked(original):
        def __init__(self,*a,**kw):super().__init__(*a,**kw);paths.append(Path(self.name))
    report=dict(schema=1,stage=STAGE,scope='isolated_private_files_real_child_lease_synthetic_manager_inventory_readback',
        python_version=sys.version.split()[0],production_state_read=False,production_credentials_read=False,
        production_worker_stopped=False,production_worker_started=False,production_service_control_requested=False,
        docker_commands=0,systemd_control_commands=0,application_api_requests=0,backup_executed=False,
        restore_executed=False,inference_requested=False,linger_changed=False,
        native_docker_systemd_acceptance=False,backup_executor_integration=False,
        actual_reboot_acceptance='deferred_by_user',kubernetes_acceptance='deferred')
    try:
        with patch.object(tempfile,'TemporaryDirectory',Tracked):
            suite=unittest.defaultTestLoader.loadTestsFromName('ops_tools.tests.test_maintenance_execute')
            result=Results();suite.run(result)
        cleaned=all(not path.exists() for path in paths)
        report.update(result='passed' if result.wasSuccessful() and not result.skipped and cleaned else 'attention_required',
            tests_run=result.testsRun,cases=result.cases,
            temporary_directory_cleanup='passed' if cleaned else 'inspect_failed_cases',
            failures=[dict(case=test.id(),kind='assertion') for test,_ in result.failures]+
                     [dict(case=test.id(),kind='exception') for test,_ in result.errors],
            unavailable=[test.id() for test,_ in result.skipped])
    except Exception:
        report.update(result='attention_required',code='ISOLATED_EXECUTION_CHECK_UNAVAILABLE')
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['result']=='passed' else 2


if __name__=='__main__':sys.exit(main())
